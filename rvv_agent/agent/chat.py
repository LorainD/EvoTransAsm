"""agent.chat — Interactive chat mode (state-machine driven).

Refactored from the original monolithic run_chat() into a state-machine
architecture.  Each migration stage is an independent handler function.
Normal chat (non-migrate) still uses a simple multi-turn conversation loop.

State machine flow:
  INTENT → SEARCH_FILE → FUNC_DISCOVER → BUILD_REFERENCE → PLAN → ANALYZE → PATCH → BUILD → (TEST) → (KB_UPDATE) → DONE
  BUILD failure → DEBUG → PATCH (retry)
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..core.config import AppConfig, is_board_enabled
from ..core.llm import (
    LlmMessage,
    chat_completion_with_retry,
    get_trajectory_dict,
    probe_llm,
    record_trajectory_action,
    reset_trajectory,
)
from ..core.prompts import (
    files_refine_prompt,
    plan_refine_prompt,
    system_prompt,
)
from ..core.statemachine import StateMachine
from ..core.task import (
    AnalysisArtifact,
    BuildArtifact,
    DiscoveredFunction,
    FileSearchArtifact,
    FuncDiscoverArtifact,
    KBUpdateArtifact,
    MigrationTarget,
    MigrationTask,
    PlanArtifact,
    ReferenceCodeArtifact,
    TaskContext,
    TaskState,
    TaskStatus,
    TaskUpdateArtifact,
    load_func_discover_artifact,
    load_analysis_artifact,
    load_plan_artifact,
)
from ..core.util import (
    ensure_dir,
    extract_build_errors,
    fmt_argv,
    now_id,
    print_llm_error,
    print_red,
    print_yellow,
    slug,
    write_json,
    write_text,
)
from dataclasses import asdict
from ..memory.knowledge_base import KnowledgeBase, Pattern, ErrorRecord
from ..tool.interactive import prompt_text, prompt_yes_no


# ---------------------------------------------------------------------------
# LLM probe helper
# ---------------------------------------------------------------------------

def _print_llm_probe(cfg: AppConfig) -> None:
    """Print best-effort LLM health status without crashing on probe errors."""

    print("LLM status:")
    try:
        st = probe_llm(cfg.llm)
    except Exception as e:
        endpoint = getattr(cfg.llm, "base_url", "")
        model = getattr(cfg.llm, "model", "")
        print(f"- endpoint_url: {endpoint}")
        print(f"- model: {model}")
        print("- probe_ok: False")
        print(f"- probe_error: {e}")
        return

    if not isinstance(st, dict):
        endpoint = getattr(cfg.llm, "base_url", "")
        model = getattr(cfg.llm, "model", "")
        print(f"- endpoint_url: {endpoint}")
        print(f"- model: {model}")
        print("- probe_ok: False")
        print(f"- probe_error: unexpected probe_llm() return type: {type(st).__name__}")
        return

    endpoint = st.get("endpoint_url", getattr(cfg.llm, "base_url", ""))
    model = st.get("model", getattr(cfg.llm, "model", ""))

    print(f"- endpoint_url: {endpoint}")
    print(f"- model: {model}")
    print(f"- api_key_present: {st.get('api_key_present')}")
    print(f"- probe_ok: {st.get('probe_ok')}")
    if st.get("probe_ok"):
        print(f"- probe_reply: {st.get('probe_reply')}")
    else:
        print(f"- probe_error: {st.get('probe_error')}")


def _prompt_symbol() -> str:
    """Ask user for a symbol name interactively."""
    follow = prompt_text(
        "请直接输入要迁移的算子/函数名（或输入 /cancel 取消）： "
    ).strip()
    if follow.lower() in {"/cancel", "/c"}:
        return ""
    m = re.search(r"([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)", follow)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# State handlers
# ---------------------------------------------------------------------------

def handle_intent(task: TaskContext) -> TaskContext:
    """INTENT handler: confirm symbol, persist intent."""
    symbol = task.target.symbol
    if not symbol:
        symbol = _prompt_symbol()
        if not symbol:
            print("未提供有效 symbol，取消迁移。")
            task.current_state = TaskState.DONE
            return task
        task.target.symbol = symbol
        if "." in symbol:
            task.target.module = symbol.split(".")[0]
        else:
            task.target.module = symbol

    print(f"\n迁移目标: {task.target.symbol} (module: {task.target.module})")
    task.save_artifact("INTENT", {
        "module": task.target.module,
        "symbol": task.target.symbol,
        "functions": task.target.functions,
    })
    record_trajectory_action("intent", f"Target confirmed: {task.target.symbol}")
    task.current_state = TaskState.SEARCH_FILE
    return task


def handle_retrieve(task: TaskContext) -> TaskContext:
    """SEARCH_FILE handler: search symbol + select reference files + user refinement."""
    from .search import select_references

    ffmpeg_root = task.ffmpeg_root
    symbol = task.target.symbol
    # select_references 内部会自动执行 symbol+module 合并检索
    file_search = select_references(task.cfg, ffmpeg_root, symbol)

    write_text(task.run_dir / "retrieval_raw.txt", file_search.raw_text + "\n")
    selected = file_search.selected_json

    def _list(key: str) -> list[str]:
        v = selected.get(key, [])
        return [str(x) for x in v] if isinstance(v, list) else []

    selected_files: list[str] = []
    for k in ("c", "x86", "arm", "riscv", "headers", "makefiles", "checkasm"):
        selected_files.extend(_list(k))
    selected_files = list(dict.fromkeys(selected_files))

    existing_rvv = _list("existing_rvv")
    for r in existing_rvv:
        if r not in selected_files:
            selected_files.append(r)

    print("\n检索/选择出的参考文件：")
    for p in selected_files:
        tag = "[existing-rvv] " if p in existing_rvv else ""
        print(f"  {tag}{p}")

    if not prompt_yes_no("\n确认进入分析/生成阶段？", default=True):
        selected_files = _refine_files(task.cfg, symbol, selected_files)
        if not selected_files:
            print("已取消，本轮结束。")
            task.current_state = TaskState.DONE
            return task

    record_trajectory_action(
        "select_refs",
        f"Reference files confirmed ({len(selected_files)} files)",
        detail="\n".join(selected_files),
        event_type="human_output",
    )

    file_search.selected_files = selected_files
    aid = task.save_artifact("SEARCH_FILE", file_search)
    task.artifacts.file_search_id = aid

    task.current_state = TaskState.FUNC_DISCOVER
    return task


def handle_func_discover(task: TaskContext) -> TaskContext:
    """FUNC_DISCOVER handler: identify all migratable functions in the module."""
    from .analyze import discover_functions
    from .search import build_context_from_files

    file_search = task.load_artifact("SEARCH_FILE")
    selected_files = file_search.get("selected_files", [])
    code_context = build_context_from_files(task.ffmpeg_root, symbol=task.target.symbol, files=selected_files)

    print("\n正在识别可迁移的函数…")
    artifact = discover_functions(task.cfg, code_context, task.target)

    for f in artifact.functions:
        print(f"  - {f.name}: {f.semantic_hint[:60]}")

    record_trajectory_action(
        "func_discover",
        f"Discovered {len(artifact.functions)} function(s)",
        detail="\n".join(f.name for f in artifact.functions),
    )

    task.save_artifact("FUNC_DISCOVER", artifact)
    task.current_state = TaskState.BUILD_REFERENCE
    return task


def handle_build_reference(task: TaskContext) -> TaskContext:
    """BUILD_REFERENCE handler: materialize function-scoped reference code context."""
    from .search import build_context_from_files

    file_search = task.load_artifact("SEARCH_FILE")
    selected_files = file_search.get("selected_files", [])

    try:
        func_discover = task.load_artifact("FUNC_DISCOVER")
        discovered = [f.name for f in load_func_discover_artifact(func_discover).functions if f.name]
    except Exception:
        discovered = []

    function_name = task.target.current_function or (discovered[0] if discovered else task.target.symbol)
    function_id = f"{task.task_id}:{function_name}"
    code_context = build_context_from_files(task.ffmpeg_root, symbol=function_name, files=selected_files)
    write_text(task.run_dir / "context.txt", code_context)

    ref_id = now_id()
    artifact = ReferenceCodeArtifact(
        reference_code_id=ref_id,
        file_search_id=str(task.artifacts.file_search_id or file_search.get("file_search_id", "")),
        function_id=function_id,
        function_name=function_name,
        reference_files=selected_files,
        matched_symbols=[task.target.symbol, function_name],
        code_context=code_context,
        existing_rvv=[str(x) for x in file_search.get("selected_json", {}).get("existing_rvv", [])],
        raw_text="",
        llm_used=False,
    )

    sub_id = slug(function_id)
    aid = task.save_artifact("BUILD_REFERENCE", artifact, sub_id=sub_id)
    task.artifacts.reference_code_ids.append(aid)
    task.current_state = TaskState.PLAN
    return task


def _decide_function_migration(func, fa) -> tuple[int, str]:
    """Heuristic migration decision: 1 migrate, 0 skip."""
    name = (func.name or "").lower()
    note = (fa.notes or "").lower()
    ref_count = len(fa.x86_refs or []) + len(fa.arm_refs or []) + len(fa.c_candidates or [])

    if any(k in name for k in ("init", "register", "config", "setup")) and ref_count == 0:
        return 0, "初始化/注册类函数且缺少可复用SIMD参考，当前阶段跳过"

    if not fa.vectorizable and ref_count == 0 and not (fa.pattern or []):
        return 0, "向量化收益低（无明显SIMD模式且缺少参考实现）"

    if "wrapper" in note or "trivial" in note:
        return 0, "语义上为包装/轻量胶水函数，迁移收益较低"

    return 1, "保留迁移：具备向量化收益或参考价值"


def handle_analyze(task: TaskContext) -> TaskContext:
    """ANALYZE handler: per-function semantic analysis for current group."""
    from .analyze import analyze_with_llm, collect_arch_simd_experience
    from .context_builder import ContextBuilder, ContextConfig
    from .search import Discovery

    try:
        plan_data = load_plan_artifact(task.load_artifact("PLAN"))
    except Exception:
        print("无法加载 PLAN，结束迁移")
        task.current_state = TaskState.DONE
        return task

    current_group_idx = plan_data.current_group_idx
    if current_group_idx >= len(plan_data.groups):
        print("所有 group 已处理，迁移完成")
        task.current_state = TaskState.TASK_UPDATE
        return task

    current_group = plan_data.groups[current_group_idx]
    group_functions = current_group.functions

    # 仅在切组时重置迭代计数
    active_group = getattr(task.artifacts, "active_group_id", "")
    if active_group != current_group.group_id:
        task.artifacts.group_iteration_count = 0
        task.artifacts.active_group_id = current_group.group_id

    print(f"\n正在分析 Group [{current_group_idx+1}/{len(plan_data.groups)}]: {current_group.group_id}")
    print(f"  函数: {', '.join(f.name for f in group_functions if f.name)}")

    if task.artifacts.reference_code_ids:
        sub = task.artifacts.reference_code_ids[-1].split("/", 1)[-1]
        reference = task.load_artifact("BUILD_REFERENCE", sub_id=sub)
    else:
        reference = task.load_artifact("BUILD_REFERENCE")
    code_context = reference.get("code_context", "")

    discovery = Discovery(symbol=task.target.symbol, matches=[])

    kb = None
    try:
        kb_path = task.run_dir.parent / "knowledge_base.json"
        if kb_path.exists():
            from ..memory.knowledge_base import KnowledgeBase
            kb = KnowledgeBase(kb_path)
            kb.load()
    except Exception:
        pass

    builder = ContextBuilder(task, kb)
    merged = AnalysisArtifact(
        per_function_analysis={},
        symbol=task.target.symbol,
        raw_text="",
        llm_used=False,
    )

    aggregated_groups: dict[str, dict] = {}
    try:
        prev = load_analysis_artifact(task.load_artifact("ANALYZE"))
        merged.per_function_analysis.update(prev.per_function_analysis)
        merged.llm_used = merged.llm_used or prev.llm_used
        if isinstance(prev.analysis_json, dict):
            prev_groups = prev.analysis_json.get("groups", {})
            if isinstance(prev_groups, dict):
                aggregated_groups.update(prev_groups)
    except Exception:
        pass

    raw_parts: list[str] = []
    migratable: list[str] = []
    skipped_reasons: dict[str, str] = {}

    for func in group_functions:
        func_name = func.name
        if not func_name:
            continue

        ctx_dict = builder.build_analyze_context(
            code_context=code_context,
            function_name=func_name,
            config=ContextConfig(include_kb=True, include_errors=True, include_prior_analysis=True),
        )

        prior = ctx_dict.get("prior_analysis")
        prior_map = {func_name: prior} if prior else None

        func_artifact = analyze_with_llm(
            task.cfg,
            discovery,
            functions=[func],
            context_override=ctx_dict.get("code", code_context),
            prior_analysis=prior_map,
            build_errors=ctx_dict.get("prior_errors"),
            kb=kb,
        )
        merged.per_function_analysis.update(func_artifact.per_function_analysis)
        merged.llm_used = merged.llm_used or func_artifact.llm_used
        if func_artifact.raw_text:
            raw_parts.append(func_artifact.raw_text)

        fa = merged.per_function_analysis.get(func_name)
        if fa is None:
            continue
        migrate, reason = _decide_function_migration(func, fa)
        fa.migrate = int(migrate)
        fa.migrate_reason = reason
        if fa.notes:
            fa.notes = f"{fa.notes} | migrate={fa.migrate} reason={reason}"
        else:
            fa.notes = f"migrate={fa.migrate} reason={reason}"

        if fa.migrate == 1:
            migratable.append(func_name)
        else:
            skipped_reasons[func_name] = reason

    group_view = {
        "group_id": current_group.group_id,
        "group_functions": migratable,
        "all_group_functions": [f.name for f in group_functions if f.name],
        "migratable_functions": migratable,
        "skipped_functions": skipped_reasons,
    }
    aggregated_groups[current_group.group_id] = group_view
    arch_simd_experience = collect_arch_simd_experience(merged.per_function_analysis)

    # Keep top-level fields for backward compatibility while preserving full aggregation.
    merged.analysis_json = {
        "symbol": task.target.symbol,
        "current_group_id": current_group.group_id,
        "current_group": group_view,
        "groups": aggregated_groups,
        "group_id": current_group.group_id,
        "group_functions": migratable,
        "all_group_functions": group_view["all_group_functions"],
        "migratable_functions": migratable,
        "skipped_functions": skipped_reasons,
        "arch_simd_experience": arch_simd_experience,
    }
    merged.raw_text = "\n\n".join(raw_parts)

    record_trajectory_action(
        "analyze",
        f"Analysis complete for group {current_group.group_id} (migratable={len(migratable)})",
        detail=json.dumps(group_view, ensure_ascii=False)[:2000],
        event_type="human_output",
    )

    aid = task.save_artifact("ANALYZE", merged)
    task.artifacts.analysis_ids.append(aid)
    write_json(task.run_dir / "analysis.json", asdict(merged))

    if not migratable:
        print("[ANALYZE] 本组无高价值迁移函数，跳过 PATCH/BUILD：")
        for fn, reason in skipped_reasons.items():
            print(f"  - {fn}: {reason}")

        # 直接推进到下一组，并回到 PLAN 做继续判定
        plan_data.completed_groups.append(current_group.group_id)
        plan_data.current_group_idx += 1
        task.save_artifact("PLAN", plan_data)
        task.artifacts.group_iteration_count = 0
        task.artifacts.active_group_id = ""
        task.current_state = TaskState.PLAN
        return task

    task.current_state = TaskState.PATCH
    return task

def _refine_plan(cfg: AppConfig, symbol: str, steps: list[str],
                 history: list[dict] | None = None) -> list[str]:
    """Interactive plan refinement loop."""
    while True:
        feedback = prompt_text(
            "请描述修改意见（直接回车接受，输入 /skip 跳过）：\n> "
        ).strip()
        if not feedback:
            return steps
        if feedback.lower() in {"/skip", "/cancel"}:
            return []
        try:
            raw = chat_completion_with_retry(
                cfg.llm,
                [
                    LlmMessage(role="system", content=system_prompt()),
                    LlmMessage(role="user", content=plan_refine_prompt(symbol, steps, feedback)),
                ],
                max_tokens=600,
                stage="chat_refine_plan",
                max_retries=3,
            )
            raw = raw.strip()
            start, end = raw.find("{"), raw.rfind("}")
            if start != -1 and end > start:
                data = json.loads(raw[start: end + 1])
                new_steps = [str(s).strip() for s in data.get("steps", []) if str(s).strip()]
                if new_steps:
                    steps = new_steps
                    if history is not None:
                        history.append({"stage": "plan", "feedback": feedback})
        except Exception as e:
            print(f"（LLM refine 失败：{e}，保留当前计划）")

        print("\n修改后的 Plan：")
        for i, s in enumerate(steps, 1):
            print(f"{i:02d}. {s}")
        if prompt_yes_no("\n确认这份计划？", default=True):
            return steps


def _refine_files(cfg: AppConfig, symbol: str, files: list[str],
                  history: list[dict] | None = None) -> list[str]:
    """Interactive reference file list refinement loop."""
    while True:
        feedback = prompt_text(
            "请描述修改意见（直接回车接受，输入 /skip 跳过）：\n> "
        ).strip()
        if not feedback:
            return files
        if feedback.lower() in {"/skip", "/cancel"}:
            return []
        try:
            raw = chat_completion_with_retry(
                cfg.llm,
                [
                    LlmMessage(role="system", content=system_prompt()),
                    LlmMessage(role="user", content=files_refine_prompt(symbol, files, feedback)),
                ],
                max_tokens=600,
                stage="chat_refine_files",
                max_retries=3,
            )
            raw = raw.strip()
            start, end = raw.find("{"), raw.rfind("}")
            if start != -1 and end > start:
                data = json.loads(raw[start: end + 1])
                new_files: list[str] = []
                for k in ("c", "x86", "arm", "riscv", "headers", "makefiles", "checkasm"):
                    v = data.get(k, [])
                    if isinstance(v, list):
                        new_files.extend(str(x) for x in v)
                new_files = list(dict.fromkeys(new_files))
                if new_files:
                    files = new_files
                    if history is not None:
                        history.append({"stage": "files", "feedback": feedback})
        except Exception as e:
            print(f"（LLM refine 失败：{e}，保留当前文件列表）")

        print("\n修改后的参考文件：")
        for f in files:
            print(f"  - {f}")
        if prompt_yes_no("\n确认这份文件列表？", default=True):
            return files


def handle_plan(task: TaskContext, kb: KnowledgeBase | None = None) -> TaskContext:
    """PLAN handler: generate+refine plan, and resume existing plan when present."""
    from .plan import llm_plan, refine_plan_interactive

    symbol = task.target.symbol

    # If PLAN already exists, do not regenerate. Decide next step from current_group_idx.
    try:
        existing = load_plan_artifact(task.load_artifact("PLAN"))
        if existing.groups:
            if existing.current_group_idx < len(existing.groups):
                next_group_id = existing.groups[existing.current_group_idx].group_id
                if getattr(task.artifacts, "active_group_id", "") != next_group_id:
                    task.artifacts.group_iteration_count = 0
                    task.artifacts.active_group_id = ""
                print(
                    f"\n继续执行已有 PLAN：Group [{existing.current_group_idx+1}/{len(existing.groups)}] "
                    f"{existing.groups[existing.current_group_idx].group_id}"
                )
                task.current_state = TaskState.ANALYZE
            else:
                print("\nPLAN 已无待处理 group，进入 TASK_UPDATE")
                task.current_state = TaskState.TASK_UPDATE
            return task
    except Exception:
        pass

    discovered_functions: list[DiscoveredFunction] = []
    try:
        func_discover = task.load_artifact("FUNC_DISCOVER")
        discovered_functions = load_func_discover_artifact(func_discover).functions
    except Exception:
        discovered_functions = []

    if not discovered_functions:
        fallback_names = task.target.functions or [symbol]
        discovered_functions = [DiscoveredFunction(name=name, role="core") for name in fallback_names if name]

    print("\n正在生成迁移计划…")
    try:
        plan = llm_plan(task.cfg, symbol, functions=discovered_functions)
    except Exception as e:
        print(f"[PLAN] 计划生成已取消: {e}")
        task.current_state = TaskState.DONE
        return task
    plan_steps = plan.steps

    print("\nPlan：")
    migrate_mode = "多函数/分组迁移" if any(len(g.functions) > 1 for g in plan.groups) else "单函数顺序迁移"
    print(f"  模式: {migrate_mode}")
    for i, s in enumerate(plan_steps, 1):
        print(f"  {i}. {s}")

    if plan.function_order:
        print("\n函数迁移顺序：")
        for i, f in enumerate(plan.function_order, 1):
            print(f"  {i}. {f}")

    if plan.groups:
        print("\n函数分组策略：")
        for group in sorted(plan.groups, key=lambda g: g.order):
            names = ", ".join(f.name for f in group.functions if f.name)
            print(f"  - [{group.order}] {group.group_id} ({group.group_type or 'single'}): {names}")

    if plan.rationale:
        print("\nPlan rationale：")
        print(f"  {plan.rationale}")

    if not prompt_yes_no("\n确认按该 plan 继续？", default=True):
        if prompt_yes_no("是否进入 plan 修改模式？", default=False):
            plan = refine_plan_interactive(task.cfg, symbol, plan, discovered_functions)
            plan_steps = plan.steps
        else:
            print("已取消，本轮结束。")
            task.current_state = TaskState.DONE
            return task

    record_trajectory_action(
        "plan", f"Plan confirmed for {symbol}",
        detail="\n".join(plan_steps), event_type="human_output",
    )

    # Persist with group execution state
    artifact = PlanArtifact(
        plan_id=plan.plan_id,
        steps=plan_steps,
        function_order=plan.function_order,
        groups=plan.groups,
        acceptance_criteria=plan.acceptance_criteria or {"build_ok": True, "functionally_valid": True},
        refine_history=plan.refine_history,
        rationale=plan.rationale,
        current_group_idx=0,
        completed_groups=[],
        failed_groups=[],
    )
    aid = task.save_artifact("PLAN", artifact)
    task.artifacts.plan_id = aid
    task.task.plan_id = aid
    task.artifacts.group_iteration_count = 0
    task.artifacts.active_group_id = ""

    task.current_state = TaskState.ANALYZE
    return task


def handle_patch(task: TaskContext, kb: KnowledgeBase | None = None) -> TaskContext:
    """PATCH handler: delegates to classic or tool-use PATCH stage."""
    from .patch import run_patch_stage, run_patch_stage_tools

    kb_patterns = None
    if kb:
        # Try tag-based search first using analysis results
        search_tags: list[str] = []
        try:
            analysis = task.load_artifact("ANALYZE")
            analysis_json = analysis.get("analysis_json", {})
            # Extract tags from analysis: pattern list + datatype
            if isinstance(analysis_json.get("pattern"), list):
                search_tags.extend(str(t) for t in analysis_json["pattern"])
            elif analysis_json.get("pattern"):
                search_tags.append(str(analysis_json["pattern"]))
            if analysis_json.get("datatype") and analysis_json["datatype"] != "unknown":
                search_tags.append(str(analysis_json["datatype"]))
        except Exception:
            pass

        found = []
        if search_tags:
            found = kb.search_patterns(tags=search_tags, max_results=3)
        # Fallback to symbol-based search if tag search yields nothing
        if not found:
            found = kb.search_patterns(symbol=task.target.symbol, max_results=3)
        if found:
            from dataclasses import asdict
            kb_patterns = [asdict(p) for p in found]

    use_tools = False
    try:
        use_tools = bool(getattr(task.cfg, "features", None) and task.cfg.features.enable_tool_use_patch_loop)
    except Exception:
        use_tools = False

    if use_tools:
        return run_patch_stage_tools(task, kb_patterns=kb_patterns)
    return run_patch_stage(task, kb_patterns=kb_patterns)




def handle_build(task: TaskContext) -> TaskContext:
    """BUILD handler: configure + make checkasm."""
    from ..tool.exec import (
        ExecResult,
        configure_argv,
        make_checkasm_argv,
        run_configure,
        run_make_checkasm,
    )

    ffmpeg_root = task.ffmpeg_root
    build_dir = ffmpeg_root / task.cfg.ffmpeg.build_dir
    jobs = max(1, os.cpu_count() or 1)
    patch_id = task.artifacts.patch_ids[-1] if task.artifacts.patch_ids else ""
    iteration_no = len(task.artifacts.build_run_ids) + 1

    # Check human policy
    exec_ok = task.cfg.human.exec_ok
    if exec_ok is None:
        print("\n交叉编译计划：")
        print(f"  build 目录 : {build_dir}")
        print(f"  configure  : {fmt_argv(configure_argv(task.cfg, ffmpeg_root))}")
        print(f"  make       : {fmt_argv(make_checkasm_argv(jobs=jobs))}")
        exec_ok = prompt_yes_no("\n是否现在执行 configure + 构建 checkasm？")
        task.cfg.human.exec_ok = exec_ok

    if not exec_ok:
        print("跳过构建阶段。")
        task.current_state = TaskState.TASK_UPDATE
        return task

    ensure_dir(build_dir)

    # --- configure ---
    print(f"\n正在运行 configure…")
    cfg_result = run_configure(task.cfg, ffmpeg_root, build_dir)

    build_artifact = BuildArtifact(
        run_id=now_id(),
        patch_id=patch_id,
        cmd=fmt_argv(configure_argv(task.cfg, ffmpeg_root)),
        stdout=cfg_result.stdout,
        stderr=cfg_result.stderr,
        exitcode=cfg_result.returncode,
        phase="configure",
        success=cfg_result.returncode == 0,
        error_type="configure_error" if cfg_result.returncode != 0 else "",
        iteration_no=iteration_no,
    )

    if cfg_result.returncode != 0:
        print(f"\nconfigure 失败 (rc={cfg_result.returncode})")
        # Save build log for debugging
        error_extract = extract_build_errors(cfg_result.stdout + cfg_result.stderr)
        write_text(task.run_dir / "build_log.txt",
                   f"=== configure (rc={cfg_result.returncode}) ===\n{error_extract}\n")
        aid = task.save_artifact("BUILD", build_artifact, sub_id=build_artifact.run_id)
        task.artifacts.build_run_ids.append(build_artifact.run_id)
        task.current_state = TaskState.DEBUG
        return task

    # --- make checkasm ---
    print("\nconfigure 完成，开始构建 checkasm…")
    make_result = run_make_checkasm(task.cfg, build_dir, jobs)

    build_artifact = BuildArtifact(
        run_id=now_id(),
        patch_id=patch_id,
        cmd=fmt_argv(make_checkasm_argv(jobs=jobs)),
        stdout=make_result.stdout,
        stderr=make_result.stderr,
        exitcode=make_result.returncode,
        phase="make",
        artifact_path=str(build_dir / "tests" / "checkasm" / "checkasm"),
        success=make_result.returncode == 0,
        error_type="build_error" if make_result.returncode != 0 else "",
        iteration_no=iteration_no,
    )
    aid = task.save_artifact("BUILD", build_artifact, sub_id=build_artifact.run_id)
    task.artifacts.build_run_ids.append(build_artifact.run_id)

    if make_result.returncode == 0:
        # Validate that generated code contains real RVV instructions
        from ..core.util import has_real_rvv_instructions
        has_rvv = False
        if task.artifacts.patch_ids:
            try:
                sub = task.artifacts.patch_ids[-1].split("/")[-1]
                latest_patch = task.load_artifact("PATCH", sub_id=sub)
                has_rvv = has_real_rvv_instructions(latest_patch.get("generate_plan", {}))
            except Exception:
                pass
        if not has_rvv:
            print("\n[WARN] 构建通过但未检测到有效 RVV 指令，可能是空壳实现")
            record_trajectory_action("build_warn", "Build passed but no real RVV instructions detected")
            build_artifact.error_type = "rvv_missing"
            task.save_artifact("BUILD", build_artifact, sub_id=build_artifact.run_id)
            task.current_state = TaskState.DEBUG
        else:
            print(f"\n构建成功 ✓")
            record_trajectory_action("build_success", "Build succeeded")
            task.current_state = TaskState.TEST
    else:
        print(f"\n构建失败 (rc={make_result.returncode})")
        # Save build log with extracted errors
        error_extract = extract_build_errors(make_result.stdout + make_result.stderr)
        build_log_path = task.run_dir / "build_log.txt"
        # Append to existing log (may have configure output from earlier runs)
        existing_log = ""
        if build_log_path.exists():
            existing_log = build_log_path.read_text(encoding="utf-8", errors="replace")
        write_text(build_log_path,
                   existing_log + f"\n=== make (rc={make_result.returncode}) ===\n{error_extract}\n")
        record_trajectory_action("build_fail", "Build failed")
        task.current_state = TaskState.DEBUG

    return task


def handle_debug(task: TaskContext, kb: KnowledgeBase | None = None) -> TaskContext:
    """DEBUG handler: delegates to structured debug module."""
    from .debug import run_debug_handler
    return run_debug_handler(task, kb=kb)


def handle_test(task: TaskContext) -> TaskContext:
    """TEST handler: scp checkasm to board and run module-scoped test."""
    from ..tool.board import (
        analyze_checkasm_output,
        build_board_commands,
        is_infra_failure,
        local_checkasm_candidates,
        local_checkasm_path,
        run_with_sshpass,
    )

    checkasm_timeout_sec = 300

    test_id = now_id()
    module = task.target.module.strip()

    # Use centralised helper instead of the raw boolean flag so that
    # board tests are not accidentally skipped when connection
    # parameters are present but ``enabled`` was left at its default.
    if not is_board_enabled(task.cfg):
        print("\n未启用 board 配置，跳过板端测试。")
        task.current_state = TaskState.KB_UPDATE
        return task

    # 高层一次性确认：是否在本轮执行板端测试。
    run_board = task.cfg.human.run_onboard_ok
    if run_board is None:
        print("\n已检测到 board 配置，准备执行板端 checkasm 测试：")
        run_board = prompt_yes_no("是否在本轮执行板端测试？", default=True)
        task.cfg.human.run_onboard_ok = run_board

    if not run_board:
        print("\n已根据用户选择跳过板端测试。")
        task.save_artifact("TEST", {
            "test_id": test_id,
            "status": "skipped",
            "phase": "precheck",
            "module": module,
            "run_reason": "user_opt_out",
        })
        task.current_state = TaskState.KB_UPDATE
        return task

    cmds = build_board_commands(task.cfg, task.ffmpeg_root, module)
    local_bin = local_checkasm_path(task.ffmpeg_root, str(task.cfg.ffmpeg.build_dir))
    checked_paths = [str(p) for p in local_checkasm_candidates(task.ffmpeg_root, str(task.cfg.ffmpeg.build_dir))]

    if not local_bin.exists():
        print("\n本地 checkasm 不存在，无法执行板端测试。")
        for p in checked_paths:
            print(f"- {p}")
        task.save_artifact("TEST", {
            "test_id": test_id,
            "status": "failed",
            "reason": "local_checkasm_missing",
            "checked_paths": checked_paths,
            "module": module,
        })
        task.all_build_errors.append(
            "board_test_error: local checkasm not found; checked paths:\n" + "\n".join(checked_paths)
        )
        task.current_state = TaskState.DEBUG
        return task

    print("\n将在测试板创建本轮目录后再上传 checkasm：")
    print("- " + fmt_argv(cmds.ssh_prepare_argv))
    print("- " + fmt_argv(cmds.scp_argv))

    scp_ok = task.cfg.human.scp_ok
    if scp_ok is None:
        scp_ok = prompt_yes_no("是否现在执行 scp？", default=True)
        task.cfg.human.scp_ok = scp_ok

    password = task.cfg.human.scp_password or ""
    prepare_rc: int | None = None
    prepare_stdout = ""
    prepare_stderr = ""
    scp_rc: int | None = None
    scp_stdout = ""
    scp_stderr = ""
    if scp_ok:
        res_prepare = run_with_sshpass(cmds.ssh_prepare_argv, password)
        prepare_rc = res_prepare.returncode
        prepare_stdout = res_prepare.stdout
        prepare_stderr = res_prepare.stderr
        write_text(task.run_dir / "board_prepare_stdout.txt", res_prepare.stdout)
        write_text(task.run_dir / "board_prepare_stderr.txt", res_prepare.stderr)
        if res_prepare.returncode != 0:
            print(f"\n板端目录创建失败 (rc={res_prepare.returncode})")
            task.save_artifact("TEST", {
                "test_id": test_id,
                "status": "failed",
                "phase": "prepare",
                "module": module,
                "local_path": str(local_bin),
                "remote_dir": cmds.remote_work_dir,
                "prepare_rc": res_prepare.returncode,
                "prepare_stdout": prepare_stdout,
                "prepare_stderr": prepare_stderr,
            })
            task.all_build_errors.append(
                f"board_test_error: remote prepare failed (rc={res_prepare.returncode})\n{prepare_stdout}\n{prepare_stderr}"
            )
            task.current_state = TaskState.DEBUG
            return task

        res_scp = run_with_sshpass(cmds.scp_argv, password)
        scp_rc = res_scp.returncode
        scp_stdout = res_scp.stdout
        scp_stderr = res_scp.stderr
        write_text(task.run_dir / "scp_stdout.txt", res_scp.stdout)
        write_text(task.run_dir / "scp_stderr.txt", res_scp.stderr)
        if res_scp.returncode != 0:
            print(f"\nSCP 失败 (rc={res_scp.returncode})")
            task.save_artifact("TEST", {
                "test_id": test_id,
                "status": "failed",
                "phase": "scp",
                "module": module,
                "local_path": str(local_bin),
                "remote_dir": cmds.remote_work_dir,
                "prepare_rc": prepare_rc,
                "prepare_stdout": prepare_stdout,
                "prepare_stderr": prepare_stderr,
                "scp_rc": res_scp.returncode,
                "scp_stdout": scp_stdout,
                "scp_stderr": scp_stderr,
            })
            task.all_build_errors.append(
                f"board_test_error: scp failed (rc={res_scp.returncode})\n{scp_stdout}\n{scp_stderr}"
            )
            task.current_state = TaskState.DEBUG
            return task

    run_ok = task.cfg.human.run_onboard_ok
    if run_ok is None:
        run_ok = prompt_yes_no("是否在测试板上运行 checkasm？", default=True)
        task.cfg.human.run_onboard_ok = run_ok

    run_rc: int | None = None
    run_stdout = ""
    run_stderr = ""
    if run_ok:
        res_run = run_with_sshpass(cmds.ssh_run_argv, password, timeout_sec=checkasm_timeout_sec)
        run_rc = res_run.returncode
        run_stdout = res_run.stdout
        run_stderr = res_run.stderr
        write_text(task.run_dir / "board_stdout.txt", res_run.stdout)
        write_text(task.run_dir / "board_stderr.txt", res_run.stderr)
        write_text(
            task.run_dir / "checkasm_output_snapshot.txt",
            "=== stdout ===\n" + run_stdout + "\n\n=== stderr ===\n" + run_stderr + "\n",
        )
        run_eval = analyze_checkasm_output(run_stdout, run_stderr, run_rc)
        if not run_eval.success:
            timeout_hint = "（超时，已中断 ssh）" if run_eval.reason == "checkasm_timeout" else ""
            print(f"\n板端 checkasm 运行失败 {timeout_hint} (rc={res_run.returncode}, reason={run_eval.reason})")
            task.save_artifact("TEST", {
                "test_id": test_id,
                "status": "failed",
                "phase": "run",
                "module": module,
                "local_path": str(local_bin),
                "remote_dir": cmds.remote_work_dir,
                "scp_ok": scp_ok,
                "run_ok": run_ok,
                "prepare_rc": prepare_rc,
                "prepare_stdout": prepare_stdout,
                "prepare_stderr": prepare_stderr,
                "scp_rc": scp_rc,
                "run_rc": res_run.returncode,
                "run_reason": run_eval.reason,
                "run_stdout": run_stdout,
                "run_stderr": run_stderr,
                "timeout_sec": checkasm_timeout_sec,
            })
            task.all_build_errors.append(
                f"board_test_error: run failed (reason={run_eval.reason}, rc={res_run.returncode})\n"
                f"{run_stdout}\n{run_stderr}"
            )
            if is_infra_failure(run_eval.reason):
                print("[TEST] 检测到板端连通/认证类失败，转入 DEBUG 让 LLM 进行诊断并保留审阅证据。")
                task.current_state = TaskState.DEBUG
            else:
                task.current_state = TaskState.DEBUG
            return task

    task.save_artifact("TEST", {
        "test_id": test_id,
        "status": "success",
        "module": module,
        "local_path": str(local_bin),
        "remote_dir": cmds.remote_work_dir,
        "scp_ok": scp_ok,
        "run_ok": run_ok,
        "prepare_rc": prepare_rc,
        "prepare_stdout": prepare_stdout,
        "prepare_stderr": prepare_stderr,
        "scp_rc": scp_rc,
        "run_rc": run_rc,
        "scp_stdout": scp_stdout,
        "scp_stderr": scp_stderr,
        "run_stdout": run_stdout,
        "run_stderr": run_stderr,
        "timeout_sec": checkasm_timeout_sec,
    })
    print("\n板端测试成功 ✓")
    record_trajectory_action("test_success", f"Board checkasm test succeeded for module={module}")
    task.current_state = TaskState.KB_UPDATE
    return task


def handle_kb_update(task: TaskContext, kb: KnowledgeBase | None = None) -> TaskContext:
    """KB_UPDATE handler: extract patterns from successful migration, advance to next group."""
    if kb is None:
        task.save_artifact("KB_UPDATE", KBUpdateArtifact())
        # Advance to next group
        try:
            plan_data = load_plan_artifact(task.load_artifact("PLAN"))
            plan_data.completed_groups.append(plan_data.groups[plan_data.current_group_idx].group_id)
            plan_data.current_group_idx += 1
            task.save_artifact("PLAN", plan_data)
            task.artifacts.group_iteration_count = 0
            task.artifacts.active_group_id = ""
            if plan_data.current_group_idx < len(plan_data.groups):
                task.current_state = TaskState.ANALYZE
            else:
                task.current_state = TaskState.TASK_UPDATE
        except Exception:
            task.current_state = TaskState.TASK_UPDATE
        return task

    # Load analysis and file-search for richer extraction
    try:
        analysis = task.load_artifact("ANALYZE")
    except Exception:
        analysis = {}
    try:
        file_search = task.load_artifact("SEARCH_FILE")
    except Exception:
        file_search = {}

    analysis_json = analysis.get("analysis_json", {})
    selected_files = file_search.get("selected_files", [])
    symbol = task.target.symbol

    # Build architecture field from file presence
    arch_info: dict[str, list[str]] = {}
    for f in selected_files:
        fl = f.lower()
        if "/x86/" in fl or "_sse" in fl or "_avx" in fl:
            arch_info.setdefault("x86", []).append(f)
        elif "/aarch64/" in fl or "/arm/" in fl or "_neon" in fl:
            arch_info.setdefault("neon", []).append(f)
        elif "/riscv/" in fl or "_rvv" in fl:
            arch_info.setdefault("rvv", []).append(f)

    # Build source field with c_paths
    c_paths = [f for f in selected_files if f.endswith((".c", ".h"))]

    # Extract semantic tags from analysis
    algo_class = "unknown"
    tags: list[str] = []
    if isinstance(analysis_json.get("pattern"), list):
        tags = [str(t) for t in analysis_json["pattern"]]
        algo_class = tags[0] if tags else "unknown"
    elif analysis_json.get("pattern"):
        algo_class = str(analysis_json["pattern"])
        tags = [algo_class]

    # Create a pattern from this successful migration
    new_pattern = Pattern(
        pattern_id=f"{symbol}_{task.task_id}",
        source={"symbol": symbol, "c_paths": c_paths},
        semantic_ir={
            "algorithm_class": algo_class,
            "tags": tags,
            "loop": analysis_json.get("loop_structure", ""),
            "memory_pattern": analysis_json.get("memory_access", ""),
        },
        simd_strategy={
            "vectorize": analysis_json.get("vectorizable", True),
            "reduction": analysis_json.get("reduction", False),
            "tail_handling": "mask" if analysis_json.get("tail_required") else "none",
            "unroll": analysis_json.get("unroll_factor", 1),
        },
        architecture=arch_info,
        metadata={"weight": 0.5, "success_count": 1, "fail_count": 0},
        notes=f"Auto-extracted from migration of {symbol}",
    )
    kb.add_pattern(new_pattern)

    # Update weight for any patterns that were used during PLAN/PATCH
    # (build succeeded if we reached KB_UPDATE)
    for pid in task.artifacts.patch_ids:
        # The pattern_id format is "{symbol}_{task_id}", try to find matching
        existing = kb.search_patterns(symbol=symbol, max_results=5)
        for p in existing:
            if p.pattern_id != new_pattern.pattern_id:
                kb.update_weight(p.pattern_id, success=True)

    # Record any debug errors as error patterns
    new_errors: list[dict] = []
    for err_text in task.all_build_errors:
        from .debug import classify_error
        err_class = classify_error(err_text)
        err_class_name = str(getattr(err_class, "value", err_class))
        record = ErrorRecord(
            error_class=err_class_name,
            pattern=err_text[:200],
            fix_strategy="auto-fixed during migration",
            example=err_text[:500],
        )
        kb.add_error(record)
        new_errors.append({"error_class": err_class_name, "pattern": err_text[:200]})

    artifact = KBUpdateArtifact(
        new_patterns=[{"pattern_id": new_pattern.pattern_id, "symbol": symbol}],
        new_errors=new_errors,
    )
    task.save_artifact("KB_UPDATE", artifact)
    record_trajectory_action("kb_update", f"KB updated: 1 pattern, {len(new_errors)} errors")

    # Advance to next group
    try:
        plan_data = load_plan_artifact(task.load_artifact("PLAN"))
        plan_data.completed_groups.append(plan_data.groups[plan_data.current_group_idx].group_id)
        plan_data.current_group_idx += 1
        task.save_artifact("PLAN", plan_data)
        task.artifacts.group_iteration_count = 0
        task.artifacts.active_group_id = ""

        if plan_data.current_group_idx < len(plan_data.groups):
            task.current_state = TaskState.ANALYZE
        else:
            task.current_state = TaskState.TASK_UPDATE
    except Exception:
        task.current_state = TaskState.TASK_UPDATE

    return task


def handle_task_update(task: TaskContext) -> TaskContext:
    """TASK_UPDATE handler: finalize task lifecycle and persist summary."""
    if task.artifacts.build_run_ids:
        try:
            last = task.load_artifact("BUILD", sub_id=task.artifacts.build_run_ids[-1])
            build_ok = last.get("exitcode", -1) == 0 and str(last.get("error_type", "") or "") != "rvv_missing"
        except Exception:
            build_ok = False
    else:
        build_ok = False

    test_ok = True
    if is_board_enabled(task.cfg):
        try:
            test_artifact = task.load_artifact("TEST")
            test_ok = str(test_artifact.get("status", "") or "") in {"success", "completed", "skipped"}
        except Exception:
            test_ok = False

    overall_ok = build_ok and test_ok

    # On failure, roll back all workspace changes so ffmpeg stays compilable
    if not overall_ok:
        try:
            from .patch import rollback_all_applies
            rollback_all_applies(task)
        except Exception as e:
            print(f"[TASK_UPDATE] 回滚失败: {e}")

    now_ts = datetime.now().isoformat(timespec="seconds")
    task.task.finished_at = now_ts
    task.task.status = TaskStatus.SUCCEEDED if overall_ok else TaskStatus.FAILED
    task.task.summary = {
        "build_success": build_ok,
        "test_success": test_ok,
        "debug_cycles": len(task.artifacts.debug_run_ids),
        "patch_count": len(task.artifacts.patch_ids),
    }

    artifact = TaskUpdateArtifact(
        task_id=task.task_id,
        status=task.task.status.value,
        finished_at=task.task.finished_at,
        summary=task.task.summary,
    )
    task.save_artifact("TASK_UPDATE", artifact)
    task.current_state = TaskState.DONE
    return task


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_chat(cfg: AppConfig) -> int:
    """Interactive chat mode with state-machine driven migration."""
    print("rvv-agent chat：自由对话 + 迁移任务触发模式")
    print("- 普通问题：直接提问即可（会保留上下文）。")
    print("- 触发迁移：FFmpeg/libav 语境 + 迁移/rvv/simd/checkasm 等关键词。")
    print("- 退出：按 Ctrl+C，或输入 /exit。\n")

    _print_llm_probe(cfg)
    print("")

    # Load knowledge base
    kb = KnowledgeBase(Path("knowledge_base.json"))
    kb.load()

    # Chat history for normal conversation
    history: list[LlmMessage] = [LlmMessage(role="system", content=system_prompt())]

    while True:
        try:
            user_text = prompt_text("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            return 0

        if not user_text:
            continue
        if user_text.lower() in {"/exit", "/quit", ":q"}:
            return 0

        # Parse intent
        from .intent import parse_intent
        intent = parse_intent(cfg, user_text)

        if intent.action != "migrate":
            # Normal chat
            history.append(LlmMessage(role="user", content=user_text))
            if len(history) > 1 + 16:
                history = [history[0], *history[-16:]]
            try:
                reply = chat_completion_with_retry(cfg.llm, history, max_tokens=800, stage="chat", max_retries=3).strip()
                print(reply + "\n")
                history.append(LlmMessage(role="assistant", content=reply))
            except Exception as e:
                print_llm_error(e, "chat")
                print(f"错误：{e}\n")
            continue

        # ===== Migrate workflow (state machine) =====
        symbol = intent.symbol
        if not symbol:
            print("已识别为迁移任务，但没从输入中抽取到算子/函数名。")
            symbol = _prompt_symbol()
            if not symbol:
                print("已取消或未提供有效 symbol，本轮结束。\n")
                continue

        # Build MigrationTarget
        target = intent.target or MigrationTarget(
            module=symbol.split(".")[0] if "." in symbol else symbol,
            symbol=symbol,
        )

        # Create TaskContext
        task_id = now_id()
        run_dir = Path("runs") / f"{task_id}_{slug(symbol)}"
        ensure_dir(run_dir)

        task = TaskContext(
            task=MigrationTask(
                task_id=task_id,
                target=target,
                status=TaskStatus.RUNNING,
                created_at=datetime.now().isoformat(timespec="seconds"),
            ),
            current_state=TaskState.INTENT,
            run_dir=run_dir,
            cfg=cfg,
            ffmpeg_root=cfg.ffmpeg.root.expanduser().resolve(),
        )

        # Save user input
        write_text(run_dir / "user_input.txt", user_text + "\n")
        reset_trajectory()

        # Register state handlers
        # Flow: INTENT → SEARCH_FILE → FUNC_DISCOVER → BUILD_REFERENCE → PLAN → ANALYZE → PATCH → BUILD → ...
        handlers = {
            TaskState.INTENT: handle_intent,
            TaskState.SEARCH_FILE: handle_retrieve,
            TaskState.FUNC_DISCOVER: handle_func_discover,
            TaskState.BUILD_REFERENCE: handle_build_reference,
            TaskState.PLAN: lambda t: handle_plan(t, kb),
            TaskState.ANALYZE: handle_analyze,
            TaskState.PATCH: lambda t: handle_patch(t, kb),
            TaskState.BUILD: handle_build,
            TaskState.DEBUG: lambda t: handle_debug(t, kb),
            TaskState.TEST: handle_test,
            TaskState.KB_UPDATE: lambda t: handle_kb_update(t, kb),
            TaskState.TASK_UPDATE: handle_task_update,
        }

        # Run state machine
        sm = StateMachine(task, handlers)
        try:
            task = sm.run()
        except Exception as e:
            print_red(f"\n迁移过程出错: {e}")
            import traceback
            traceback.print_exc()

        # Generate report
        try:
            from .report import write_chat_report
            rpt = write_chat_report(task)
            print(f"报告已生成: {rpt}")
        except Exception as e:
            print_yellow(f"报告生成失败: {e}")

        # Save trajectory
        traj = get_trajectory_dict(model=cfg.llm.model, endpoint=cfg.llm.base_url)
        write_json(run_dir / "trajectory.json", traj)
        tot = traj.get("totals", {})
        print(
            f"\n[trajectory] calls={tot.get('num_calls', 0)}"
            f"  in={tot.get('input_tokens', 0)}"
            f"  out={tot.get('output_tokens', 0)}"
            f"  cost=${tot.get('cost_usd', 0.0):.6f}"
        )

        # Save KB
        kb.save()

        print(f"\n本轮完成：run_dir = {run_dir}\n")
