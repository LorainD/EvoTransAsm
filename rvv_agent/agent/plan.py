"""agent.plan — Plan Agent

负责：
  - 迁移计划生成（Plan / fixed_plan / llm_plan）
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict

from ..core.config import AppConfig
from ..core.llm import LlmError, LlmMessage, chat_completion
from ..core.prompts import plan_prompt, system_prompt
from ..core.task import DiscoveredFunction, FunctionGroup, PlanArtifact, load_plan_artifact
from ..core.util import extract_json_from_llm

# Backward-compatible alias: historical callers import Plan from this module.
Plan = PlanArtifact


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


def llm_plan(cfg: AppConfig, symbol: str, functions: list[DiscoveredFunction] | None = None) -> PlanArtifact:
    """调用 LLM 生成针对 symbol 的迁移计划，失败时回退到 fixed_plan。"""
    discovered = list(functions or [])
    prompt_functions = [asdict(f) for f in discovered]
    messages = [
        LlmMessage(role="system", content=system_prompt()),
        LlmMessage(role="user", content=plan_prompt(symbol, prompt_functions)),
    ]
    try:
        raw = chat_completion(cfg.llm, messages, max_tokens=1200, stage="plan")
        data = extract_json_from_llm(raw)
        artifact = load_plan_artifact(data)
        if not artifact.steps:
            raise ValueError("empty steps")
        return _validate_or_rebuild_plan(artifact, discovered, symbol)
    except (LlmError, Exception):
        return fixed_plan(symbol, discovered)
