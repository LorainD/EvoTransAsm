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
from ..core.ir import default_ir
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
    kb_reflection_prompt,
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
    load_reference_code_artifact,
)
from ..core.util import (
    ensure_dir,
    extract_json_from_llm,
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
from .context_builder import ContextBuilder


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
    from .search import build_context_from_files_multi

    file_search = task.load_artifact("SEARCH_FILE")
    selected_files = file_search.get("selected_files", [])

    try:
        func_discover = task.load_artifact("FUNC_DISCOVER")
        discovered = [f.name for f in load_func_discover_artifact(func_discover).functions if f.name]
    except Exception:
        discovered = []

    symbols: list[str] = []
    if discovered:
        symbols.extend(discovered)
    elif task.target.current_function:
        symbols.append(task.target.current_function)
    elif task.target.symbol:
        symbols.append(task.target.symbol)
    symbols = list(dict.fromkeys(s for s in symbols if s))

    context_map = build_context_from_files_multi(task.ffmpeg_root, symbols=symbols, files=selected_files)
    combined_parts: list[str] = []
    for name, code_context in context_map.items():
        combined_parts.append(f"=== {name} ===")
        combined_parts.append(code_context)
    write_text(task.run_dir / "context.txt", "\n\n".join(combined_parts))

    function_contexts: dict[str, dict] = {}
    for name in symbols:
        function_contexts[name] = {
            "function_name": name,
            "code_context": context_map.get(name, ""),
            "matched_symbols": [task.target.symbol, name],
            "reference_files": selected_files,
        }

    ref_id = now_id()
    artifact = ReferenceCodeArtifact(
        reference_code_id=ref_id,
        file_search_id=str(task.artifacts.file_search_id or file_search.get("file_search_id", "")),
        reference_files=selected_files,
        function_contexts=function_contexts,
        existing_rvv=[str(x) for x in file_search.get("selected_json", {}).get("existing_rvv", [])],
        raw_text="",
        llm_used=False,
    )

    aid = task.save_artifact("BUILD_REFERENCE", artifact)
    task.artifacts.reference_code_id = aid
    task.current_state = TaskState.PLAN
    return task


def _decide_function_migration(func, fa) -> tuple[int, str]:
    """Heuristic migration decision: 1 migrate, 0 skip."""
    name = (func.name or "").lower()
    note = (fa.notes or "").lower()
    ref_count = len(fa.x86_refs or []) + len(fa.arm_refs or []) + len(fa.c_candidates or [])
    ir = fa.ir if isinstance(fa.ir, dict) else {}
    parallelism = ir.get("parallelism", {}) if isinstance(ir.get("parallelism"), dict) else {}
    computation = ir.get("computation", {}) if isinstance(ir.get("computation"), dict) else {}
    vectorizable = bool(parallelism.get("vectorizable", False))
    comp_type = str(computation.get("type", "unknown") or "unknown")

    if any(k in name for k in ("init", "register", "config", "setup")) and ref_count == 0:
        return 0, "初始化/注册类函数且缺少可复用SIMD参考，当前阶段跳过"

    if not vectorizable and ref_count == 0 and comp_type == "unknown":
        return 0, "向量化收益低（无明显SIMD模式且缺少参考实现）"

    if "wrapper" in note or "trivial" in note:
        return 0, "语义上为包装/轻量胶水函数，迁移收益较低"

    return 1, "保留迁移：具备向量化收益或参考价值"


def handle_analyze(task: TaskContext) -> TaskContext:
    """ANALYZE handler: per-function semantic analysis for current group."""
    from .analyze import analyze_with_llm, collect_ir_summary
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
        task.current_state = TaskState.KB_UPDATE
        return task

    current_group = plan_data.groups[current_group_idx]
    group_functions = current_group.functions

    # 仅在切组时重置迭代计数
    active_group = getattr(task.artifacts, "active_group_id", "")
    if active_group != current_group.group_id:
        task.artifacts.group_iteration_count = 0
        task.artifacts.prebuild_generate_retries = 0
        task.artifacts.active_group_id = current_group.group_id

    print(f"\n正在分析 Group [{current_group_idx+1}/{len(plan_data.groups)}]: {current_group.group_id}")
    print(f"  函数: {', '.join(f.name for f in group_functions if f.name)}")

    reference = load_reference_code_artifact(task.load_artifact("BUILD_REFERENCE"))
    function_contexts = reference.function_contexts if isinstance(reference.function_contexts, dict) else {}

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

        ctx_entry = function_contexts.get(func_name, {})
        code_context = str(ctx_entry.get("code_context", "")) if isinstance(ctx_entry, dict) else ""
        if not code_context:
            # Fallback to the first non-empty function context in this artifact.
            for _, v in function_contexts.items():
                if isinstance(v, dict) and str(v.get("code_context", "")).strip():
                    code_context = str(v.get("code_context", ""))
                    break

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
    ir_summary = collect_ir_summary(merged.per_function_analysis)

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
        "ir_summary": ir_summary,
        "function_analysis": {name: asdict(obj) for name, obj in merged.per_function_analysis.items()},
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
        task.artifacts.prebuild_generate_retries = 0
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
                    task.artifacts.prebuild_generate_retries = 0
                    task.artifacts.active_group_id = ""
                print(
                    f"\n继续执行已有 PLAN：Group [{existing.current_group_idx+1}/{len(existing.groups)}] "
                    f"{existing.groups[existing.current_group_idx].group_id}"
                )
                task.current_state = TaskState.ANALYZE
            else:
                print("\nPLAN 已无待处理 group，进入 KB_UPDATE")
                task.current_state = TaskState.KB_UPDATE
            return task
    except Exception as e:
        # Avoid silent fallback: corrupted/incompatible PLAN artifacts should be visible.
        print(f"[PLAN][WARN] 读取已有 PLAN 失败，将重新生成: {e}")
        record_trajectory_action("plan_warn", f"failed_to_load_existing_plan:{e}")

    discovered_functions: list[DiscoveredFunction] = []
    try:
        func_discover = task.load_artifact("FUNC_DISCOVER")
        discovered_functions = load_func_discover_artifact(func_discover).functions
    except Exception:
        discovered_functions = []

    if not discovered_functions:
        fallback_names = task.target.functions or [symbol]
        discovered_functions = [DiscoveredFunction(name=name, role="core") for name in fallback_names if name]

    reference_files_for_plan: list[str] = []
    kb_short_rules_for_plan: list[str] = []
    try:
        plan_ctx = ContextBuilder(task, kb).build_plan_prompt_context()
        reference_files_for_plan = list(plan_ctx.reference_files)
        kb_short_rules_for_plan = list(getattr(plan_ctx, "kb_short_rules", []) or [])
    except Exception:
        reference_files_for_plan = []
        kb_short_rules_for_plan = []

    print("\n正在生成迁移计划…")
    try:
        plan = llm_plan(
            task.cfg,
            symbol,
            functions=discovered_functions,
            reference_files=reference_files_for_plan,
            kb_short_rules=kb_short_rules_for_plan,
        )
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
    task.artifacts.prebuild_generate_retries = 0
    task.artifacts.active_group_id = ""

    task.current_state = TaskState.ANALYZE
    return task


def handle_patch(task: TaskContext, kb: KnowledgeBase | None = None) -> TaskContext:
    """PATCH handler: delegates to classic or tool-use PATCH stage."""
    from .patch import run_patch_stage, run_patch_stage_tools

    kb_patterns = None
    if kb:
        # Try IR-based matching first using latest migratable function.
        search_tags: list[str] = []
        selected_ir: dict | None = None
        try:
            analysis = task.load_artifact("ANALYZE")
            analysis_json = analysis.get("analysis_json", {})
            group_funcs = analysis_json.get("migratable_functions", []) if isinstance(analysis_json, dict) else []
            per_func = analysis.get("per_function_analysis", {}) if isinstance(analysis, dict) else {}
            if isinstance(group_funcs, list):
                for fname in group_funcs:
                    fobj = per_func.get(str(fname), {}) if isinstance(per_func, dict) else {}
                    if isinstance(fobj, dict) and isinstance(fobj.get("ir"), dict):
                        selected_ir = fobj.get("ir")
                        break
                    if hasattr(fobj, "ir") and isinstance(getattr(fobj, "ir"), dict):
                        selected_ir = getattr(fobj, "ir")
                        break
            if isinstance(selected_ir, dict):
                comp = selected_ir.get("computation", {}) if isinstance(selected_ir.get("computation"), dict) else {}
                mem = selected_ir.get("memory", {}) if isinstance(selected_ir.get("memory"), dict) else {}
                search_tags.extend([
                    f"comp:{comp.get('type', 'unknown')}",
                    f"mem:{mem.get('access_pattern', 'contiguous')}",
                ])
        except Exception:
            pass

        found = []
        if selected_ir:
            ranked = kb.match_patterns_by_ir(selected_ir, max_results=3)
            found = [item.get("pattern") for item in ranked if item.get("pattern") is not None]
        elif search_tags:
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
        task.current_state = TaskState.KB_UPDATE
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
    checkasm_sources: list[str] = []
    try:
        search_art = task.load_artifact("SEARCH_FILE")
        if isinstance(search_art, dict):
            selected_json = search_art.get("selected_json", {})
            if isinstance(selected_json, dict):
                v = selected_json.get("checkasm", [])
                if isinstance(v, list):
                    checkasm_sources = [str(x).strip() for x in v if str(x).strip()]
            if not checkasm_sources:
                selected_files = search_art.get("selected_files", [])
                if isinstance(selected_files, list):
                    checkasm_sources = [
                        str(x).strip()
                        for x in selected_files
                        if str(x).strip().replace("\\", "/").startswith("tests/checkasm/")
                        and str(x).strip().endswith(".c")
                    ]
    except Exception:
        checkasm_sources = []

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

    cmds = build_board_commands(task.cfg, task.ffmpeg_root, module, checkasm_sources)
    local_bin = local_checkasm_path(task.ffmpeg_root, str(task.cfg.ffmpeg.build_dir))
    checked_paths = [str(p) for p in local_checkasm_candidates(task.ffmpeg_root, str(task.cfg.ffmpeg.build_dir))]

    print(f"\ncheckasm 测试目标: --test={cmds.test_name or module}")
    if cmds.test_source and cmds.test_source != "fallback":
        print(f"测试名来源: {cmds.test_source}")
    else:
        print("测试名来源: fallback(module)")

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
            "test_name": cmds.test_name,
            "test_name_source": cmds.test_source,
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
                "test_name": cmds.test_name,
                "test_name_source": cmds.test_source,
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
                "test_name": cmds.test_name,
                "test_name_source": cmds.test_source,
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
                "test_name": cmds.test_name,
                "test_name_source": cmds.test_source,
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
        "test_name": cmds.test_name,
        "test_name_source": cmds.test_source,
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


def _evaluate_kb_outcome(task: TaskContext) -> tuple[bool, str, str]:
    """Evaluate success from latest BUILD/TEST artifacts for KB 分流。"""
    build_status = "missing"
    test_status = "not_required"

    build_ok = False
    if task.artifacts.build_run_ids:
        try:
            latest_build = task.load_artifact("BUILD", sub_id=task.artifacts.build_run_ids[-1])
            exitcode = int(latest_build.get("exitcode", -1))
            success_flag = bool(latest_build.get("success", False))
            err_type = str(latest_build.get("error_type", "") or "")
            build_ok = (success_flag or exitcode == 0) and err_type != "rvv_missing"
            build_status = "success" if build_ok else f"failed:{err_type or f'rc={exitcode}'}"
        except Exception as e:
            build_status = f"failed:artifact_error:{e}"
    else:
        build_status = "missing"

    test_ok = True
    if is_board_enabled(task.cfg):
        try:
            test_artifact = task.load_artifact("TEST")
            test_status = str(test_artifact.get("status", "") or "missing")
            test_ok = test_status == "success"
        except Exception:
            test_status = "missing"
            test_ok = False

    return build_ok and test_ok, build_status, test_status


def _extract_success_patterns(task: TaskContext, kb: KnowledgeBase) -> tuple[list[dict], list[dict]]:
    """Successful migration path: extract Pattern + supplemental ErrorRecord."""
    try:
        analysis = task.load_artifact("ANALYZE")
    except Exception:
        analysis = {}
    try:
        file_search = task.load_artifact("SEARCH_FILE")
    except Exception:
        file_search = {}

    analysis_json = analysis.get("analysis_json", {})
    per_func = analysis.get("per_function_analysis", {}) if isinstance(analysis, dict) else {}
    selected_files = file_search.get("selected_files", [])
    symbol = task.target.symbol

    references: dict[str, list[str]] = {"x86": [], "arm": [], "riscv": []}
    for f in selected_files:
        fl = str(f).lower()
        if "/x86/" in fl or "_sse" in fl or "_avx" in fl:
            references.setdefault("x86", []).append(str(f))
        elif "/aarch64/" in fl or "/arm/" in fl or "_neon" in fl:
            references.setdefault("arm", []).append(str(f))
        elif "/riscv/" in fl or "_rvv" in fl:
            references.setdefault("riscv", []).append(str(f))

    c_paths = [str(f) for f in selected_files if str(f).endswith((".c", ".h"))]

    rep_ir = None
    migratable = analysis_json.get("migratable_functions", []) if isinstance(analysis_json, dict) else []
    if isinstance(migratable, list):
        for fname in migratable:
            fobj = per_func.get(str(fname), {}) if isinstance(per_func, dict) else {}
            if isinstance(fobj, dict) and isinstance(fobj.get("ir"), dict):
                rep_ir = fobj.get("ir")
                break
    if not isinstance(rep_ir, dict):
        rep_ir = default_ir()

    rep_simd_features = {
        "has_saturation": False,
        "has_widening": False,
        "has_narrowing": False,
    }
    if isinstance(migratable, list):
        for fname in migratable:
            fobj = per_func.get(str(fname), {}) if isinstance(per_func, dict) else {}
            if isinstance(fobj, dict) and isinstance(fobj.get("simd_features"), dict):
                rep_simd_features = fobj.get("simd_features")
                break

    debug_history_parts: list[str] = []
    for debug_run_id in task.artifacts.debug_run_ids:
        try:
            dbg = task.load_artifact("DEBUG", sub_id=debug_run_id)
        except Exception:
            continue
        if not isinstance(dbg, dict):
            continue
        err_text = str(dbg.get("error_text", "") or "").strip()
        llm_sugg = str(dbg.get("llm_suggestion", "") or "").strip()
        fix_actions = dbg.get("fix_actions", []) if isinstance(dbg.get("fix_actions", []), list) else []
        fix_text = llm_sugg or "; ".join(str(x).strip() for x in fix_actions if str(x).strip())
        if not err_text and not fix_text:
            continue
        debug_history_parts.append(f"报错: {err_text[:300]}\\n当时的尝试修复: {fix_text[:300]}")

    final_code_str = ""
    if task.artifacts.patch_ids:
        try:
            latest_patch_ref = task.artifacts.patch_ids[-1]
            sub_id = latest_patch_ref.split("/", 1)[1] if "/" in latest_patch_ref else latest_patch_ref
            latest_patch = task.load_artifact("PATCH", sub_id=sub_id)
            if isinstance(latest_patch, dict):
                final_code_str = json.dumps(latest_patch.get("generate_plan", {}), ensure_ascii=False)[:1000]
        except Exception:
            final_code_str = ""

    extracted_notes = f"Auto-extracted from migration of {symbol}"
    new_pattern = Pattern(
        pattern_id=f"{symbol}_{task.task_id}",
        source={"symbol": symbol, "c_paths": c_paths},
        ir=rep_ir,
        simd_features=rep_simd_features if isinstance(rep_simd_features, dict) else {},
        references=references,
        meta={"weight": 0.5, "stats": {"success_count": 1, "fail_count": 0}},
        notes=extracted_notes,
    )

    ranked = kb.match_patterns_by_ir(rep_ir, max_results=5)
    for item in ranked:
        pid = str(item.get("pattern_id", ""))
        if pid and pid != new_pattern.pattern_id:
            kb.update_weight(pid, success=True)

    new_errors: list[dict] = []
    if debug_history_parts and task.cfg:
        print("\n[KB_UPDATE] 正在通过 LLM 提炼轨迹与排错经验...")
        prompt = kb_reflection_prompt(symbol, "\n---\n".join(debug_history_parts), final_code_str)
        messages = [
            LlmMessage(role="system", content="You are a knowledge extraction agent."),
            LlmMessage(role="user", content=prompt),
        ]
        try:
            raw = chat_completion_with_retry(task.cfg.llm, messages, max_tokens=1000, stage="kb_reflection", max_retries=2)
            reflection_data = extract_json_from_llm(raw)

            if isinstance(reflection_data.get("migration_patterns"), list) and reflection_data.get("migration_patterns"):
                first_pattern = reflection_data["migration_patterns"][0]
                if isinstance(first_pattern, dict):
                    extracted_notes = str(first_pattern.get("notes", "") or "").strip() or extracted_notes

            candidates = reflection_data.get("error_diagnostics", [])
            for err in candidates if isinstance(candidates, list) else []:
                if not isinstance(err, dict):
                    continue
                err_class = str(err.get("error_class", "") or "").strip() or "unknown"
                pattern_text = str(err.get("pattern", "") or "").strip()[:200]
                fix_strategy = str(err.get("fix_strategy", "") or "").strip() or "未知修复方案"
                if not pattern_text:
                    continue
                record = ErrorRecord(
                    error_class=err_class,
                    pattern=pattern_text,
                    fix_strategy=fix_strategy,
                    example=pattern_text[:500],
                    count=1,
                )
                kb.add_error(record, cfg=task.cfg)
                new_errors.append({"error_class": err_class, "pattern": pattern_text, "fix_strategy": fix_strategy})
        except Exception as e:
            print(f"[KB_UPDATE] 轨迹提炼失败，降级为结构化 DEBUG 写入: {e}")

    if not new_errors:
        seen_error_keys: set[tuple[str, str]] = set()
        for debug_run_id in task.artifacts.debug_run_ids:
            try:
                dbg = task.load_artifact("DEBUG", sub_id=debug_run_id)
            except Exception:
                continue
            if not isinstance(dbg, dict):
                continue

            error_text = str(dbg.get("error_text", "") or "").strip()
            if not error_text:
                continue
            err_class_name = str(dbg.get("error_note", "") or "").strip() or "unknown"

            fix_actions = dbg.get("fix_actions", []) if isinstance(dbg.get("fix_actions", []), list) else []
            fix_steps = [str(x).strip() for x in fix_actions if str(x).strip()]
            llm_suggestion = str(dbg.get("llm_suggestion", "") or "").strip()
            if fix_steps:
                fix_strategy = "; ".join(fix_steps)
            elif llm_suggestion:
                fix_strategy = llm_suggestion[:300]
            else:
                fix_strategy = "需人工复盘该错误根因"

            pattern_text = error_text[:200]
            key = (err_class_name, pattern_text)
            if key in seen_error_keys:
                continue
            seen_error_keys.add(key)

            record = ErrorRecord(
                error_class=err_class_name,
                pattern=pattern_text,
                fix_strategy=fix_strategy,
                example=error_text[:500],
            )
            kb.add_error(record, cfg=task.cfg)
            new_errors.append(
                {
                    "error_class": err_class_name,
                    "pattern": pattern_text,
                    "fix_strategy": fix_strategy,
                }
            )

    new_pattern.notes = extracted_notes
    kb.add_pattern(new_pattern)
    return [{"pattern_id": new_pattern.pattern_id, "symbol": symbol}], new_errors


def _extract_failure_lessons(task: TaskContext, kb: KnowledgeBase) -> list[dict]:
    """Failed task path: post-mortem -> ErrorRecord."""
    fatal_error = ""
    if task.all_build_errors:
        fatal_error = str(task.all_build_errors[-1])[:2000]

    if not fatal_error and task.artifacts.build_run_ids:
        try:
            latest_build = task.load_artifact("BUILD", sub_id=task.artifacts.build_run_ids[-1])
            fatal_error = extract_build_errors(
                str(latest_build.get("stdout", "")) + "\n" + str(latest_build.get("stderr", ""))
            )[:2000]
        except Exception:
            fatal_error = ""

    if not fatal_error:
        return []

    debug_history = ""
    for did in task.artifacts.debug_run_ids[-2:]:
        try:
            art = task.load_artifact("DEBUG", sub_id=did)
            debug_history += f"- 尝试了：{art.get('fix_actions', [])}\\n"
        except Exception:
            pass

    prompt = f"""
你是一个底层的 RVV 汇编迁移专家。
刚才我们尝试迁移函数 `{task.target.symbol}`，但最终彻底失败了。

【最终致命报错日志】：
{fatal_error}

【我们尝试过但无济于事的修复方案】：
{debug_history}

为了避免未来的自动化系统在遇到同样的报错时重蹈覆辙，请你总结一条高价值的防坑经验。
要求：
1. 不要输出无意义内容。
2. 深刻指出导致该报错的根本原因（Root Cause）。
3. 明确指出哪些思路是死胡同。

请严格输出以下 JSON 格式：
{{
  "error_class": "compile_error | link_error | runtime_error | test_mismatch",
  "pattern": "提取报错日志中最核心的 1-2 句特征（不要包含具体临时文件路径）",
  "fix_strategy": "用一两句话总结根本原因和未来遇到此问题的排查方向"
}}
""".strip()

    messages = [
        LlmMessage(role="system", content=system_prompt()),
        LlmMessage(role="user", content=prompt),
    ]

    try:
        raw = chat_completion_with_retry(task.cfg.llm, messages, stage="kb_post_mortem", max_tokens=500, max_retries=2)
        data = extract_json_from_llm(raw)
        pattern_text = str(data.get("pattern", "") or "").strip()
        fix_strategy = str(data.get("fix_strategy", "") or "").strip()
        if not pattern_text or not fix_strategy:
            return []

        err_class = str(data.get("error_class", "") or "").strip() or "compile_error"
        record = ErrorRecord(
            error_class=err_class,
            pattern=pattern_text[:200],
            fix_strategy=f"[事后复盘总结] {fix_strategy[:400]}",
            example=fatal_error[:500],
            count=1,
        )
        kb.add_error(record, cfg=task.cfg)
        print(f"[KB_UPDATE] 成功提取避坑指南: {fix_strategy[:80]}")
        return [{"error_class": err_class, "pattern": pattern_text[:200], "fix_strategy": record.fix_strategy}]
    except Exception as e:
        print(f"[KB_UPDATE] 事后复盘提取失败: {e}")
        return []


def handle_kb_update(task: TaskContext, kb: KnowledgeBase | None = None) -> TaskContext:
    """KB_UPDATE handler: success/failed 分流，并在收尾前沉淀知识。"""
    print("\n[KB_UPDATE] 正在进行经验总结与知识库沉淀...")

    if kb is None:
        task.save_artifact("KB_UPDATE", KBUpdateArtifact())
        task.current_state = TaskState.TASK_UPDATE
        return task

    is_success, build_status, test_status = _evaluate_kb_outcome(task)
    new_patterns: list[dict] = []
    new_errors: list[dict] = []

    if is_success:
        print("[KB_UPDATE] 任务成功，提取成功迁移 Pattern 与 IR...")
        new_patterns, new_errors = _extract_success_patterns(task, kb)
    else:
        print(f"[KB_UPDATE] 任务失败，启动事后复盘 (build={build_status}, test={test_status})...")
        new_errors = _extract_failure_lessons(task, kb)

    artifact = KBUpdateArtifact(new_patterns=new_patterns, new_errors=new_errors)
    task.save_artifact("KB_UPDATE", artifact)
    record_trajectory_action(
        "kb_update",
        f"KB updated: success={is_success}, patterns={len(new_patterns)}, errors={len(new_errors)}",
    )

    if not is_success:
        task.current_state = TaskState.TASK_UPDATE
        return task

    # 成功时按 group 推进；无后续 group 时再进入 TASK_UPDATE。
    try:
        plan_data = load_plan_artifact(task.load_artifact("PLAN"))
        current_group_id = ""
        if plan_data.current_group_idx < len(plan_data.groups):
            current_group_id = plan_data.groups[plan_data.current_group_idx].group_id
            if current_group_id and current_group_id not in plan_data.completed_groups:
                plan_data.completed_groups.append(current_group_id)

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
        task.artifacts.prebuild_generate_retries = 0
        task.artifacts.active_group_id = ""
        task.current_state = TaskState.ANALYZE
    except Exception:
        task.current_state = TaskState.TASK_UPDATE

    return task


def _quick_build_health_check(task: TaskContext) -> tuple[bool, str]:
    """Best-effort quick build check after rollback."""
    from ..tool.exec import run_configure, run_make_checkasm

    ffmpeg_root = task.ffmpeg_root
    build_dir = ffmpeg_root / task.cfg.ffmpeg.build_dir
    ensure_dir(build_dir)

    jobs = max(1, min(max(1, os.cpu_count() or 1), 2))

    cfg_result = run_configure(task.cfg, ffmpeg_root, build_dir)
    if cfg_result.returncode != 0:
        return False, f"rollback_health: configure_rc={cfg_result.returncode}"

    make_result = run_make_checkasm(task.cfg, build_dir, jobs)
    return make_result.returncode == 0, (
        f"rollback_health: configure_rc=0 checkasm_build_rc={make_result.returncode}"
    )


def _rollback_on_failure(task: TaskContext, reason: str) -> tuple[bool, str]:
    """Rollback all apply snapshots and run a quick health check."""
    from .patch import rollback_all_applies

    restored = 0
    try:
        restored = rollback_all_applies(task)
    except Exception as e:
        return False, f"rollback_failed: {e}"

    if restored > 0:
        print(f"[TASK_UPDATE] 回滚触发({reason})，已恢复 {restored} 个文件，开始健康检查…")

    try:
        ok, summary = _quick_build_health_check(task)
        if ok:
            print(f"[TASK_UPDATE] 回滚后健康检查通过: {summary}")
        else:
            print(f"[TASK_UPDATE][WARN] 回滚后健康检查失败: {summary}")
        return ok, summary
    except Exception as e:
        return False, f"rollback_health_exception: {e}"


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
    test_status = "not_required"
    if is_board_enabled(task.cfg):
        try:
            test_artifact = task.load_artifact("TEST")
            test_status = str(test_artifact.get("status", "") or "missing")
            # Strict policy: with board enabled, only explicit success keeps injected changes.
            test_ok = test_status == "success"
        except Exception:
            test_ok = False
            test_status = "missing"

    overall_ok = build_ok and test_ok

    rollback_triggered = False
    rollback_health_check = True
    rollback_health_summary = "not_needed"

    # On failure, roll back all workspace changes so ffmpeg stays compilable.
    if not overall_ok:
        rollback_triggered = True
        rollback_health_check, rollback_health_summary = _rollback_on_failure(task, "task_update_failed")

    now_ts = datetime.now().isoformat(timespec="seconds")
    task.task.finished_at = now_ts
    task.task.status = TaskStatus.SUCCEEDED if overall_ok else TaskStatus.FAILED
    task.task.summary = {
        "build_success": build_ok,
        "test_success": test_ok,
        "test_status": test_status,
        "debug_cycles": len(task.artifacts.debug_run_ids),
        "patch_count": len(task.artifacts.patch_ids),
        "rollback_triggered": rollback_triggered,
        "rollback_health_check": rollback_health_check,
        "rollback_health_summary": rollback_health_summary,
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
            task.task.status = TaskStatus.FAILED
            task.task.finished_at = datetime.now().isoformat(timespec="seconds")
            try:
                rollback_ok, rollback_summary = _rollback_on_failure(task, "state_machine_exception")
                task.task.summary = {
                    **(task.task.summary or {}),
                    "rollback_triggered": True,
                    "rollback_health_check": rollback_ok,
                    "rollback_health_summary": rollback_summary,
                }
            except Exception as rollback_e:
                print_yellow(f"异常兜底回滚失败: {rollback_e}")

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
