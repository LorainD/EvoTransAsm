"""agent.plan — Plan Agent

负责：
  - 迁移计划生成（Plan / fixed_plan / llm_plan）
"""
from __future__ import annotations

from collections import defaultdict
import json
from dataclasses import asdict

from ..core.config import AppConfig
from ..core.llm import LlmError, LlmMessage, chat_completion_with_retry
from ..core.prompts import build_plan_refine_prompt, plan_prompt, system_prompt
from ..core.task import DiscoveredFunction, FunctionGroup, PlanArtifact, load_plan_artifact
from ..core.util import extract_json_from_llm, keep_dataclass_fields, keep_dataclass_fields_list, now_id
from ..tool.interactive import prompt_text, prompt_yes_no

# Backward-compatible alias: historical callers import Plan from this module.
Plan = PlanArtifact


def _sanitize_plan_payload(data: dict) -> dict:
    """Keep only dataclass-defined fields to avoid unknown-key crashes."""
    if not isinstance(data, dict):
        return {}

    cleaned = keep_dataclass_fields(data, PlanArtifact)
    raw_groups = cleaned.get("groups", [])
    if not isinstance(raw_groups, list):
        cleaned["groups"] = []
        return cleaned

    groups: list[dict] = []
    for group in raw_groups:
        group_clean = keep_dataclass_fields(group, FunctionGroup)
        raw_functions = group_clean.get("functions", [])
        group_clean["functions"] = keep_dataclass_fields_list(raw_functions, DiscoveredFunction)
        groups.append(group_clean)

    cleaned["groups"] = groups
    return cleaned


def _difficulty_score(func: DiscoveredFunction) -> int:
    hint = (func.semantic_hint or "").lower()
    score = 0
    if func.role == "dependency":
        score += 2
    if any(k in func.name.lower() for k in ("init", "config", "setup", "register")):
        score += 10
    if any(k in hint for k in ("hard", "complex", "difficult")):
        score += 6
    if any(k in hint for k in ("easy", "simple")):
        score -= 2
    if any(k in hint for k in ("similar", "shared pattern", "template")):
        score -= 1
    score += len(func.dependencies)
    return score


def _build_function_map(functions: list[DiscoveredFunction]) -> dict[str, DiscoveredFunction]:
    return {f.name: f for f in functions if f.name}


def _dependency_closure(start: str, fmap: dict[str, DiscoveredFunction], seen: set[str] | None = None) -> list[str]:
    seen = seen or set()
    if start in seen or start not in fmap:
        return []
    seen.add(start)
    ordered = [start]
    for dep in fmap[start].dependencies:
        ordered.extend(_dependency_closure(dep, fmap, seen))
    return ordered


def _similarity_key(func: DiscoveredFunction) -> str:
    hint = (func.semantic_hint or "").lower()
    if "similar" in hint:
        return hint
    name = func.name.lower()
    parts = [p for p in name.replace(".", "_").split("_") if p and not p.isdigit()]
    return "_".join(parts[:2]) if len(parts) >= 2 else name


def _heuristic_groups(functions: list[DiscoveredFunction]) -> list[FunctionGroup]:
    fmap = _build_function_map(functions)
    if not fmap:
        return []

    core = [f for f in functions if f.role != "dependency" and "init" not in f.name.lower()]
    deps_only = [f for f in functions if f.role == "dependency" and f.name in fmap]
    init_like = [f for f in functions if any(k in f.name.lower() for k in ("init", "config", "setup", "register"))]

    similar_buckets: dict[str, list[DiscoveredFunction]] = defaultdict(list)
    for func in core:
        similar_buckets[_similarity_key(func)].append(func)

    groups: list[FunctionGroup] = []
    assigned: set[str] = set()
    order = 1

    # 1) easy + similar core functions can migrate together
    for bucket in similar_buckets.values():
        bucket = sorted(bucket, key=lambda f: (_difficulty_score(f), f.name))
        easy_bucket = [f for f in bucket if _difficulty_score(f) <= 1 and f.name not in assigned]
        if len(easy_bucket) >= 2:
            for f in easy_bucket:
                assigned.add(f.name)
            groups.append(FunctionGroup(
                group_id=f"group_{order}",
                functions=easy_bucket,
                group_type="similar",
                order=order,
            ))
            order += 1

    # 2) dependency closures: core function with tightly-coupled helpers
    for func in sorted(core, key=lambda f: (_difficulty_score(f), f.name)):
        if func.name in assigned:
            continue
        closure_names = [name for name in _dependency_closure(func.name, fmap) if name not in assigned]
        closure_funcs = [fmap[name] for name in closure_names if name in fmap]
        if len(closure_funcs) > 1:
            for item in closure_funcs:
                assigned.add(item.name)
            groups.append(FunctionGroup(
                group_id=f"group_{order}",
                functions=sorted(closure_funcs, key=lambda f: (_difficulty_score(f), f.name)),
                group_type="dependency",
                order=order,
            ))
            order += 1

    # 3) remaining core functions as single/hard groups, easy first
    remaining_core = [f for f in core if f.name not in assigned]
    for func in sorted(remaining_core, key=lambda f: (_difficulty_score(f), f.name)):
        assigned.add(func.name)
        gtype = "hard" if _difficulty_score(func) >= 5 else "single"
        groups.append(FunctionGroup(group_id=f"group_{order}", functions=[func], group_type=gtype, order=order))
        order += 1

    # 4) remaining dependency-only helpers after core groups
    for func in sorted([f for f in deps_only if f.name not in assigned], key=lambda f: (_difficulty_score(f), f.name)):
        assigned.add(func.name)
        groups.append(FunctionGroup(group_id=f"group_{order}", functions=[func], group_type="dependency", order=order))
        order += 1

    # 5) init/register/glue always last
    for func in sorted([f for f in init_like if f.name not in assigned], key=lambda f: f.name):
        assigned.add(func.name)
        groups.append(FunctionGroup(group_id=f"group_{order}", functions=[func], group_type="hard", order=order))
        order += 1

    # 6) fallback for any unassigned function
    for func in functions:
        if func.name and func.name not in assigned:
            assigned.add(func.name)
            groups.append(FunctionGroup(group_id=f"group_{order}", functions=[func], group_type="single", order=order))
            order += 1

    return groups


def _flatten_group_order(groups: list[FunctionGroup], fallback_symbol: str) -> list[str]:
    ordered = [f.name for g in sorted(groups, key=lambda g: g.order) for f in g.functions if f.name]
    return ordered or [fallback_symbol]


def _validate_or_rebuild_plan(artifact: PlanArtifact, discovered: list[DiscoveredFunction], symbol: str) -> PlanArtifact:
    names = {f.name for f in discovered if f.name}
    valid_groups: list[FunctionGroup] = []
    for group in artifact.groups:
        funcs = [f for f in group.functions if f.name in names] if names else list(group.functions)
        if funcs:
            valid_groups.append(FunctionGroup(
                group_id=group.group_id or f"group_{group.order}",
                functions=funcs,
                group_type=group.group_type or ("dependency" if len(funcs) > 1 else "single"),
                order=group.order,
            ))
    artifact.groups = valid_groups

    valid_order = [name for name in artifact.function_order if (not names) or name in names]
    if artifact.groups:
        artifact.function_order = _flatten_group_order(artifact.groups, symbol)
    elif valid_order:
        artifact.function_order = valid_order
    else:
        artifact.groups = _heuristic_groups(discovered)
        artifact.function_order = _flatten_group_order(artifact.groups, symbol)

    if not artifact.groups and discovered:
        artifact.groups = _heuristic_groups(discovered)
        artifact.function_order = _flatten_group_order(artifact.groups, symbol)

    artifact.acceptance_criteria = artifact.acceptance_criteria or {}
    artifact.acceptance_criteria.setdefault("build_ok", True)
    artifact.acceptance_criteria.setdefault("functionally_valid", True)
    if not artifact.plan_id:
        artifact.plan_id = f"llm:{symbol}"
    if not artifact.rationale:
        artifact.rationale = "按核心优先、依赖成组、相似易函数可批量、init/注册最后、复杂函数后置的策略规划。"
    return artifact


def fixed_plan(symbol: str, functions: list[DiscoveredFunction] | None = None) -> PlanArtifact:
    """LLM 不可用时的硬编码兜底计划。"""
    discovered = list(functions or [])
    if not discovered:
        discovered = [DiscoveredFunction(name=symbol, role="core")]
    groups = _heuristic_groups(discovered)
    function_order = _flatten_group_order(groups, symbol)
    multi_group_count = sum(1 for g in groups if len(g.functions) > 1)
    return PlanArtifact(
        plan_id=f"fixed:{symbol}",
        steps=[
            f"梳理 {symbol} 相关函数，优先识别核心计算函数、依赖 helper 与 init/注册逻辑",
            "若存在语义相近且难度低的函数则批量迁移；若存在强依赖链则按依赖组一起处理",
            "整体执行顺序遵循先易后难、核心优先、依赖与注册后置",
            f"按 {len(groups)} 个函数组推进迁移，其中可批量迁移的组数为 {multi_group_count}，每组完成后再做构建验证",
            "最后补充 init/注册与 Makefile 集成，并做 configure + build + checkasm 验证",
        ],
        function_order=function_order,
        groups=groups,
        acceptance_criteria={"build_ok": True, "functionally_valid": True},
        rationale="fallback 计划：会主动区分单函数迁移与多函数批量迁移，依赖函数优先与核心函数合组，简单相似函数优先，init/注册逻辑最后处理。",
    )

def _repair_plan_json_with_llm(
    cfg: AppConfig,
    symbol: str,
    raw_output: str,
    functions: list[DiscoveredFunction],
) -> dict:
    """Ask LLM to convert invalid output into strict plan JSON."""
    func_names = [f.name for f in functions if f.name]
    repair_prompt = f"""你上一次输出的 PLAN 不是合法 JSON。请修复为严格 JSON，仅输出 JSON，不要解释。\n\n目标符号: {symbol}\n函数候选: {func_names}\n\n上一次原始输出:\n{raw_output[:12000]}\n\n输出 schema:\n{{\n  \"symbol\": \"{symbol}\",\n  \"steps\": [\"...\"],\n  \"function_order\": [\"...\"],\n  \"groups\": [\n    {{\n      \"group_id\": \"group_1\",\n      \"group_type\": \"single|dependency|similar|hard\",\n      \"order\": 1,\n      \"functions\": [\n        {{\n          \"name\": \"...\",\n          \"signature\": \"\",\n          \"file\": \"\",\n          \"line\": -1,\n          \"role\": \"core|dependency\",\n          \"dependencies\": [],\n          \"semantic_hint\": \"\"\n        }}\n      ]\n    }}\n  ],\n  \"acceptance_criteria\": {{\"build_ok\": true, \"functionally_valid\": true}},\n  \"rationale\": \"...\"\n}}\n"""

    repaired_raw = chat_completion_with_retry(
        cfg.llm,
        [
            LlmMessage(role="system", content=system_prompt()),
            LlmMessage(role="user", content=repair_prompt),
        ],
        max_tokens=1400,
        stage="plan_repair_json",
        max_retries=2,
    )
    return extract_json_from_llm(repaired_raw)


def llm_plan(
    cfg: AppConfig,
    symbol: str,
    functions: list[DiscoveredFunction] | None = None,
    reference_files: list[str] | None = None,
    kb_short_rules: list[str] | None = None,
) -> PlanArtifact:
    """调用 LLM 生成针对 symbol 的迁移计划，失败时回退到 fixed_plan。"""
    discovered = list(functions or [])
    # Short-circuit: no discovered functions => do not call LLM.
    # Avoid wasting tokens and hallucination risk on empty context.
    if not discovered:
        return fixed_plan(symbol, discovered)
    prompt_functions = [asdict(f) for f in discovered]
    messages = [
        LlmMessage(role="system", content=system_prompt()),
        LlmMessage(role="user", content=plan_prompt(symbol, prompt_functions, reference_files, kb_short_rules)),
    ]

    raw = ""
    try:
        raw = chat_completion_with_retry(cfg.llm, messages, max_tokens=1200, stage="plan", max_retries=3)
        try:
            data = extract_json_from_llm(raw)
        except json.JSONDecodeError:
            print("[PLAN] 首次 JSON 解析失败，尝试自动修复输出…")
            data = _repair_plan_json_with_llm(cfg, symbol, raw, discovered)

        data = _sanitize_plan_payload(data)
        artifact = load_plan_artifact(data)
        if not artifact.steps:
            raise ValueError("empty steps")
        return _validate_or_rebuild_plan(artifact, discovered, symbol)
    except (LlmError, Exception) as e:
        print(f"[PLAN] LLM 生成计划失败: {e}")
        # Non-interactive pipeline safety: always fall back instead of prompting/raising.
        return fixed_plan(symbol, discovered)


def _print_plan_groups(plan: PlanArtifact) -> None:
    print("\n当前 Plan Groups：")
    for i, group in enumerate(sorted(plan.groups, key=lambda g: g.order)):
        names = ", ".join(f.name for f in group.functions if f.name)
        print(f"  [{i}] {group.group_id} ({group.group_type or 'single'}): {names}")


def _llm_refine_plan(
    cfg: AppConfig,
    symbol: str,
    plan: PlanArtifact,
    functions: list[DiscoveredFunction],
    feedback: str,
) -> PlanArtifact:
    current_groups = []
    for group in sorted(plan.groups, key=lambda g: g.order):
        func_names = [f.name for f in group.functions if f.name]
        current_groups.append(
            {
                "group_id": group.group_id,
                "type": group.group_type,
                "functions": func_names,
            }
        )

    all_functions = [
        {
            "name": f.name,
            "role": f.role,
            "dependencies": f.dependencies,
            "semantic_hint": f.semantic_hint,
        }
        for f in functions
    ]

    prompt_text = build_plan_refine_prompt(symbol, current_groups, all_functions, feedback)
    messages = [
        LlmMessage(role="system", content=system_prompt()),
        LlmMessage(role="user", content=prompt_text),
    ]

    raw = chat_completion_with_retry(
        cfg.llm,
        messages,
        max_tokens=1200,
        stage="plan_refine",
        max_retries=3,
    )
    data = extract_json_from_llm(raw)

    function_map = {f.name: f for f in functions if f.name}
    assigned: set[str] = set()
    new_groups: list[FunctionGroup] = []
    order = 1
    for group_data in data.get("groups", []):
        raw_funcs = group_data.get("functions", [])
        if not isinstance(raw_funcs, list):
            raw_funcs = []
        # Accept both ["func_a"] and [{"name": "func_a"}] shapes.
        func_names = []
        for n in raw_funcs:
            if isinstance(n, dict):
                name = str(n.get("name", "") or "").strip()
            else:
                name = str(n or "").strip()
            if name and name in function_map:
                func_names.append(name)
        if not func_names:
            continue
        group_functions = [function_map[name] for name in func_names]
        assigned.update(func_names)
        new_groups.append(
            FunctionGroup(
                group_id=str(group_data.get("group_id") or f"group_{order}"),
                functions=group_functions,
                group_type=str(group_data.get("type") or "single"),
                order=order,
            )
        )
        order += 1

    # Ensure no function is dropped by refined output.
    for f in functions:
        if f.name and f.name not in assigned:
            new_groups.append(
                FunctionGroup(
                    group_id=f"group_{order}",
                    functions=[f],
                    group_type="single",
                    order=order,
                )
            )
            order += 1

    new_plan = load_plan_artifact(asdict(plan))
    new_plan.groups = new_groups
    new_plan.function_order = _flatten_group_order(new_groups, symbol)
    return _validate_or_rebuild_plan(new_plan, functions, symbol)


def refine_plan_interactive(
    cfg: AppConfig,
    symbol: str,
    plan: PlanArtifact,
    functions: list[DiscoveredFunction],
) -> PlanArtifact:
    """Interactive plan refine loop for group ordering and composition."""
    while True:
        _print_plan_groups(plan)
        feedback = prompt_text(
            "\n请描述修改意见（调整顺序、合并/拆分 group 等），或输入 /done 完成：\n> "
        ).strip()

        if feedback.lower() in {"/done", "/skip", ""}:
            return plan

        try:
            plan = _llm_refine_plan(cfg, symbol, plan, functions, feedback)
            plan.refine_history.append(
                {
                    "stage": "plan_refine",
                    "feedback": feedback,
                    "timestamp": now_id(),
                }
            )
            print("✓ Plan 已更新")
        except Exception as e:
            print(f"修改失败: {e}，请重试")
