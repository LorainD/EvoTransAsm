"""agent.debug — Structured build-error diagnosis and rollback.

New state-machine version:
  - Classifies errors (compile / link / runtime / test_mismatch)
  - Determines rollback target (locate / design / generate)
  - Produces DebugArtifact for persistence

Legacy pipeline helpers (run_fix_loop, DebugContext, DebugResult, debug())
are preserved at the bottom for backward compatibility with pipeline.py.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..core.config import AppConfig
from ..core.llm import LlmError, LlmMessage, chat_completion_with_retry, record_trajectory_action
from ..core.prompts import system_prompt
from ..core.prompts_patch import debug_classify_prompt
from ..core.task import DebugArtifact, TaskContext, TaskState, load_plan_artifact
from ..core.util import extract_build_errors, now_id, print_llm_error, write_json
from ..memory.knowledge_base import KnowledgeBase


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

class ErrorClass(Enum):
    COMPILE_ERROR = "compile_error"
    LINK_ERROR = "link_error"
    RUNTIME_ERROR = "runtime_error"
    TEST_MISMATCH = "test_mismatch"


class RollbackTarget(Enum):
    LOCATE = "locate"       # anchor drift / patch applied at wrong position
    DESIGN = "design"       # build system issue (Makefile, missing include)
    GENERATE = "generate"   # code syntax / logic error


def classify_error(build_output: str) -> ErrorClass:
    """Rule-based error classification."""
    lower = build_output.lower()
    if "undefined reference" in lower or "ld returned" in lower:
        return ErrorClass.LINK_ERROR
    if "fail" in lower and ("mismatch" in lower or "checkasm" in lower):
        return ErrorClass.TEST_MISMATCH
    if "segfault" in lower or "sigsegv" in lower:
        return ErrorClass.RUNTIME_ERROR
    return ErrorClass.COMPILE_ERROR


def determine_rollback(
    error_class: ErrorClass,
    error_text: str,
    cfg: AppConfig | None = None,
) -> RollbackTarget:
    """Determine where to roll back based on error class and content.

    Uses rules first; falls back to LLM if cfg is provided.
    """
    lower = error_text.lower()

    # Rule-based heuristics
    if error_class == ErrorClass.LINK_ERROR:
        if "undefined reference" in lower and "init_riscv" in lower:
            return RollbackTarget.DESIGN  # Makefile didn't add the file
        if "no such file" in lower:
            return RollbackTarget.LOCATE
        return RollbackTarget.DESIGN

    if error_class == ErrorClass.COMPILE_ERROR:
        if "no such file or directory" in lower:
            return RollbackTarget.LOCATE
        if "makefile" in lower or "no rule to make" in lower:
            return RollbackTarget.DESIGN
        return RollbackTarget.GENERATE

    if error_class == ErrorClass.TEST_MISMATCH:
        return RollbackTarget.GENERATE

    if error_class == ErrorClass.RUNTIME_ERROR:
        return RollbackTarget.GENERATE

    return RollbackTarget.GENERATE


# ---------------------------------------------------------------------------
# LLM-assisted debug (optional enhancement)
# ---------------------------------------------------------------------------

def _llm_classify(
    cfg: AppConfig,
    error_text: str,
    current_patch: dict | None = None,
    debug_context: dict[str, Any] | None = None,
    *,
    patch_id: str = "",
    build_run_id: str = "",
    iteration_no: int = 0,
    root_cause: str = "",
) -> DebugArtifact | None:
    """Call LLM for structured error diagnosis. Returns None on failure."""
    enriched_error_text = error_text
    if debug_context:
        enriched_error_text = (
            f"{error_text}\n\n## 调试上下文\n"
            f"{json.dumps(debug_context, ensure_ascii=False, indent=2)[:2500]}"
        )

    from .context_builder import DebugContext

    debug_ctx = DebugContext(
        error_text=enriched_error_text,
        current_patch=current_patch,
    )

    messages = [
        LlmMessage(role="system", content=system_prompt()),
        LlmMessage(role="user", content=debug_classify_prompt(debug_ctx)),
    ]
    try:
        raw = chat_completion_with_retry(
            cfg.llm,
            messages,
            max_tokens=800,
            stage="debug_classify",
            max_retries=3,
        )
        raw = raw.strip()
        s = raw.find("{")
        e = raw.rfind("}")
        if s == -1 or e <= s:
            return None
        data = json.loads(raw[s:e + 1])
        return DebugArtifact(
            run_id=now_id(),
            patch_id=patch_id,
            build_run_id=build_run_id,
            test_id="",
            iteration_no=iteration_no,
            error_class=str(data.get("error_class", "compile_error")),
            error_text=error_text[:4000],
            root_cause=str(data.get("root_cause", root_cause)),
            rollback_target=str(data.get("rollback_target", "generate")),
            fix_actions=[str(a) for a in data.get("fix_actions", [])],
            llm_suggestion=str(data.get("suggestion", "")),
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# State-machine DEBUG handler
# ---------------------------------------------------------------------------

_MAX_DEBUG_CYCLES = 3
_MAX_GROUP_ITERATIONS = 3


def _move_to_next_group_or_finish(task: TaskContext) -> TaskContext:
    """Mark current group failed and return PLAN or TASK_UPDATE."""
    try:
        plan_data = load_plan_artifact(task.load_artifact("PLAN"))
    except Exception:
        task.current_state = TaskState.TASK_UPDATE
        return task

    if not plan_data.groups:
        task.current_state = TaskState.TASK_UPDATE
        return task

    if plan_data.current_group_idx < len(plan_data.groups):
        gid = plan_data.groups[plan_data.current_group_idx].group_id
        if gid and gid not in plan_data.failed_groups:
            plan_data.failed_groups.append(gid)

    blocked = set(plan_data.completed_groups) | set(plan_data.failed_groups)
    next_idx = None
    for idx, group in enumerate(plan_data.groups):
        if group.group_id not in blocked:
            next_idx = idx
            break

    if next_idx is None:
        task.save_artifact("PLAN", plan_data)
        task.current_state = TaskState.TASK_UPDATE
        return task

    plan_data.current_group_idx = next_idx
    task.save_artifact("PLAN", plan_data)
    task.artifacts.group_iteration_count = 0
    task.artifacts.active_group_id = ""
    task.current_state = TaskState.PLAN
    return task


def run_debug_handler(task: TaskContext, kb: KnowledgeBase | None = None) -> TaskContext:
    """DEBUG handler for the state machine.

    当单 group 达到阈值后，不结束 session，回到 PLAN 选择下一组继续。
    """
    build_ids = task.artifacts.build_run_ids
    if not build_ids:
        latest_patch_error = ""
        if task.artifacts.patch_ids:
            try:
                latest_patch_id = task.artifacts.patch_ids[-1].split("/")[-1]
                latest_patch = task.load_artifact("PATCH", sub_id=latest_patch_id)
                latest_patch_error = str(latest_patch.get("error", ""))
            except Exception:
                latest_patch_error = ""

        if latest_patch_error.startswith("generate_validation_failed"):
            task.artifacts.group_iteration_count += 1
            print(f"[DEBUG] pre-build generate contract failure, retry PATCH(generate), group_iter={task.artifacts.group_iteration_count}")
            record_trajectory_action("debug", "No build artifact; route generate_validation_failed back to PATCH")
            if task.artifacts.group_iteration_count >= _MAX_GROUP_ITERATIONS:
                print(f"[DEBUG] 当前 group 已达最大迭代次数 ({_MAX_GROUP_ITERATIONS})，回到 PLAN 选择下一组")
                return _move_to_next_group_or_finish(task)
            task.rollback_hint = RollbackTarget.GENERATE.value
            task.current_state = TaskState.PATCH
            return task

        print("[DEBUG] No build artifacts found and no pre-build recoverable patch error, moving to PLAN")
        task.current_state = TaskState.PLAN
        return task

    latest_build = task.load_artifact("BUILD", sub_id=build_ids[-1])
    build_run_id = build_ids[-1]
    patch_id = str(latest_build.get("patch_id", ""))
    iteration_no = int(latest_build.get("iteration_no", len(build_ids)))
    root_cause = str(latest_build.get("error_type", "") or "build_failure")
    error_text = extract_build_errors(latest_build.get("stdout", "") + latest_build.get("stderr", ""))

    test_artifact: dict[str, Any] = {}
    test_id = ""
    try:
        test_artifact = task.load_artifact("TEST")
        test_id = str(test_artifact.get("test_id", ""))
    except Exception:
        test_artifact = {}

    test_status = str(test_artifact.get("status", "") or "")
    if test_status == "failed":
        test_reason = str(test_artifact.get("run_reason", "") or test_artifact.get("reason", "test_failed"))
        phase = str(test_artifact.get("phase", "") or "run")
        run_rc = test_artifact.get("run_rc", "n/a")
        scp_rc = test_artifact.get("scp_rc", "n/a")
        test_out = str(test_artifact.get("run_stdout", "") or test_artifact.get("scp_stdout", ""))
        test_err = str(test_artifact.get("run_stderr", "") or test_artifact.get("scp_stderr", ""))
        root_cause = f"test_failure:{phase}:{test_reason}"
        extracted = extract_build_errors(test_out + "\n" + test_err)
        error_text = (
            f"Board test failed: phase={phase}, reason={test_reason}, run_rc={run_rc}, scp_rc={scp_rc}\n"
            f"{extracted}"
        ).strip()

    if not error_text.strip():
        if root_cause == "rvv_missing":
            error_text = (
                "Build passed but rvv_missing: no real RVV instructions detected in generated implementation.\n"
                "Treat this as semantic build failure and regenerate RVV implementation instead of empty shell."
            )
            print("[DEBUG] Detected rvv_missing semantic failure, forcing PATCH retry")
        else:
            print("[DEBUG] No errors found in build output, moving to TASK_UPDATE")
            task.current_state = TaskState.TASK_UPDATE
            return task

    error_class = classify_error(error_text)
    if root_cause.startswith("test_failure:"):
        error_class = ErrorClass.TEST_MISMATCH
    rollback = determine_rollback(error_class, error_text, task.cfg)

    print(f"\n[DEBUG] 错误分类: {error_class.value}")
    print(f"[DEBUG] 回滚目标: {rollback.value}")

    kb_hints: list[str] = []
    debug_context: dict[str, Any] = {}
    checkasm_llm_analysis = None
    try:
        from .context_builder import ContextBuilder, ContextConfig
        builder = ContextBuilder(task, kb)
        if root_cause.startswith("test_failure:"):
            debug_context = builder.build_checkasm_debug_context(
                test_artifact,
                error_text,
                config=ContextConfig(include_kb=True, include_errors=True),
            )
            write_json(task.run_dir / "checkasm_debug_context.json", debug_context)

            if task.cfg:
                from ..tool.board import llm_analyze_checkasm_failure

                checkasm_llm_analysis = llm_analyze_checkasm_failure(task.cfg, debug_context)
                write_json(
                    task.run_dir / "checkasm_debug_llm_result.json",
                    {
                        "ok": bool(checkasm_llm_analysis.ok),
                        "error_class": checkasm_llm_analysis.error_class,
                        "rollback_target": checkasm_llm_analysis.rollback_target,
                        "fix_actions": checkasm_llm_analysis.fix_actions,
                        "suggestion": checkasm_llm_analysis.suggestion,
                        "raw": checkasm_llm_analysis.raw,
                    },
                )
        else:
            debug_context = builder.build_debug_context(
                error_text,
                config=ContextConfig(include_kb=True, include_errors=True),
            )
    except Exception:
        debug_context = {}

    if kb:
        known = kb.search_errors(error_class=error_class.value, max_results=3)
        for rec in known:
            if rec.fix_strategy:
                kb_hints.append(f"[KB] {rec.pattern[:80]} -> {rec.fix_strategy}")
                print(f"[DEBUG] 已知修复: {rec.fix_strategy[:100]}")

    artifact: DebugArtifact | None = None
    if task.cfg:
        latest_patch_id = task.artifacts.patch_ids[-1] if task.artifacts.patch_ids else None
        if latest_patch_id and "/" in latest_patch_id:
            latest_patch_id = latest_patch_id.split("/", 1)[1]
        current_patch = task.load_artifact("PATCH", sub_id=latest_patch_id) if latest_patch_id else None
        artifact = _llm_classify(
            task.cfg,
            error_text,
            current_patch,
            debug_context,
            patch_id=patch_id,
            build_run_id=build_run_id,
            iteration_no=iteration_no,
            root_cause=root_cause,
        )

    if artifact is None:
        artifact = DebugArtifact(
            run_id=now_id(),
            patch_id=patch_id,
            build_run_id=build_run_id,
            test_id=test_id,
            iteration_no=iteration_no,
            error_class=error_class.value,
            error_text=error_text[:4000],
            root_cause=root_cause,
            rollback_target=rollback.value,
            fix_actions=kb_hints,
            llm_suggestion="",
        )
    elif kb_hints:
        artifact.fix_actions = kb_hints + artifact.fix_actions

    if checkasm_llm_analysis and checkasm_llm_analysis.ok:
        if checkasm_llm_analysis.error_class:
            artifact.error_class = checkasm_llm_analysis.error_class
        if checkasm_llm_analysis.rollback_target:
            artifact.rollback_target = checkasm_llm_analysis.rollback_target
        if checkasm_llm_analysis.fix_actions:
            artifact.fix_actions = checkasm_llm_analysis.fix_actions + artifact.fix_actions
        if checkasm_llm_analysis.suggestion:
            artifact.llm_suggestion = checkasm_llm_analysis.suggestion

    if not artifact.patch_id:
        artifact.patch_id = patch_id
    if not artifact.build_run_id:
        artifact.build_run_id = build_run_id
    if artifact.iteration_no <= 0:
        artifact.iteration_no = iteration_no
    if not artifact.root_cause:
        artifact.root_cause = root_cause
    if not artifact.test_id:
        artifact.test_id = test_id

    if artifact.fix_actions:
        print("[DEBUG] 修复建议:")
        for action in artifact.fix_actions:
            print(f"  - {action}")
    if artifact.llm_suggestion:
        print(f"[DEBUG] LLM 建议: {artifact.llm_suggestion[:200]}")

    task.all_build_errors.append(error_text)
    task.save_artifact("DEBUG", artifact, sub_id=artifact.run_id)
    task.artifacts.debug_run_ids.append(artifact.run_id)

    record_trajectory_action(
        "debug",
        f"Error classified: {artifact.error_class}, rollback to {artifact.rollback_target}",
    )

    # 按组迭代计数：每次进入 DEBUG 视为一次本组尝试。
    task.artifacts.group_iteration_count += 1
    print(f"[DEBUG] 本 group 迭代次数: {task.artifacts.group_iteration_count}")  # Debug log for iteration count

    if task.artifacts.group_iteration_count >= _MAX_GROUP_ITERATIONS:
        print(f"\n[DEBUG] 当前 group 已达最大迭代次数 ({_MAX_GROUP_ITERATIONS})，回到 PLAN 选择下一组")
        return _move_to_next_group_or_finish(task)
    task.rollback_hint = artifact.rollback_target
    task.current_state = TaskState.PATCH
    return task


# ---------------------------------------------------------------------------
# Legacy pipeline helpers (DEPRECATED — 已被状态机架构替代)
# ---------------------------------------------------------------------------

# @dataclass
# class DebugContext:
#     symbol: str
#     current_plan: dict
#     max_retries: int = 3


# @dataclass
# class DebugResult:
#     success: bool
#     final_plan: dict
#     attempts: int
#     errors: list


# def run_fix_loop(
#     cfg: AppConfig,
#     ctx: DebugContext,
#     get_error_fn,
#     apply_fn=None,
# ) -> DebugResult:
#     """Generic build-fix loop.
#
#     .. deprecated::
#         Pipeline now uses state-machine handlers (run_debug_handler).
#     """
#     from .generate import GenerationResult, _has_real_rvv_functions, fix_generation_with_llm
#
#     plan = ctx.current_plan
#     errors = []
#
#     for attempt in range(ctx.max_retries + 1):
#         ok, err_text = get_error_fn()
#         if ok:
#             return DebugResult(success=True, final_plan=plan,
#                                attempts=attempt, errors=errors)
#         errors.append(err_text)
#
#         if attempt >= ctx.max_retries:
#             break
#
#         fix: GenerationResult = fix_generation_with_llm(
#             cfg, ctx.symbol, err_text, plan
#         )
#         if fix.error:
#             print_llm_error(fix.error, f"debug/fix#{attempt + 1}")
#             break
#         if not _has_real_rvv_functions(fix.generate_plan):
#             break
#
#         plan = fix.generate_plan
#         if apply_fn is not None:
#             apply_fn(plan)
#
#     return DebugResult(success=False, final_plan=plan,
#                        attempts=attempt + 1, errors=errors)


# def debug(ctx: "MigrationContext") -> "MigrationContext":
#     """Context-aware single-attempt LLM fix (legacy, used by pipeline.py).
#
#     .. deprecated::
#         Pipeline now uses state-machine handlers (run_debug_handler).
#     """
#     from .generate import _has_real_rvv_functions, fix_generation_with_llm
#
#     if ctx.build_log is None or ctx.current_gen is None:
#         return ctx
#     if "error" not in ctx.build_log.lower():
#         return ctx
#
#     fixed_gen = fix_generation_with_llm(
#         ctx.cfg, ctx.operator, ctx.build_log,
#         ctx.current_gen.generate_plan,
#     )
#     if fixed_gen.llm_used and _has_real_rvv_functions(fixed_gen.generate_plan):
#         ctx.current_gen = fixed_gen
#     return ctx
