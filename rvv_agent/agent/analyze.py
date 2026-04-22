"""agent.analyze — Analysis Agent

负责：
  - 函数发现（discover_functions）— FUNC_DISCOVER 阶段
  - LLM 语义分析（AnalysisResult / analyze_with_llm）— ANALYZE 阶段
"""
from __future__ import annotations

import json
from dataclasses import asdict

from ..core.config import AppConfig
from ..core.ir import default_ir, extract_ir_tags, normalize_ir
from ..core.llm import LlmError, LlmMessage, chat_completion_with_retry
from ..core.prompts import analysis_prompt, function_analysis_prompt, function_discovery_prompt, system_prompt
from ..core.task import AnalysisArtifact, FuncDiscoverArtifact, FunctionAnalysis, MigrationTarget, DiscoveredFunction, load_func_discover_artifact
from ..core.util import extract_json_from_llm, keep_dataclass_fields_list
from .search import Discovery, build_llm_context, group_files
from ..memory.knowledge_base import KnowledgeBase
from ..tool.interactive import prompt_yes_no

# Backward-compatible alias: historical callers import AnalysisResult.
# AnalysisResult = AnalysisArtifact


def collect_arch_simd_experience(per_function_analysis: dict[str, FunctionAnalysis]) -> dict[str, list[str]]:
    """Aggregate architecture-specific SIMD经验 from IR and refs."""
    out: dict[str, list[str]] = {"x86": [], "arm": [], "aarch64": []}
    seen: dict[str, set[str]] = {"x86": set(), "arm": set(), "aarch64": set()}

    def _push(arch: str, value: object) -> None:
        s = str(value).strip()
        if not s or s in seen[arch]:
            return
        seen[arch].add(s)
        out[arch].append(s)

    for fa in per_function_analysis.values():
        ir = normalize_ir(fa.ir)
        exp = ir.get("experience", {}) if isinstance(ir.get("experience"), dict) else {}
        arch_exp = exp.get("arch_simd_experience", {}) if isinstance(exp.get("arch_simd_experience"), dict) else {}

        for arch in ("x86", "arm", "aarch64"):
            for item in arch_exp.get(arch, []) if isinstance(arch_exp.get(arch), list) else []:
                _push(arch, item)

        for ref in fa.x86_refs:
            _push("x86", f"x86_ref:{ref}")
        for ref in fa.arm_refs:
            r = str(ref)
            target_arch = "aarch64" if "/aarch64/" in r.replace("\\", "/") else "arm"
            _push(target_arch, f"{target_arch}_ref:{r}")

    return out


def _count_unknown_fields(ir: dict) -> tuple[int, int]:
    comp = ir.get("computation", {}) if isinstance(ir.get("computation"), dict) else {}
    mem = ir.get("memory", {}) if isinstance(ir.get("memory"), dict) else {}
    par = ir.get("parallelism", {}) if isinstance(ir.get("parallelism"), dict) else {}

    values: list[str] = [
        str(comp.get("type", "")).strip(),
        str(mem.get("access_pattern", "")).strip(),
        str(mem.get("stride", "")).strip(),
        str(mem.get("alignment", "")).strip(),
        str(mem.get("layout", "")).strip(),
        str(par.get("dependency", "")).strip(),
        str(par.get("tail_policy", "")).strip(),
    ]
    math_expr = str(comp.get("math_expression", "")).strip()

    total = len(values) + 1
    unknown = 0
    for v in values:
        if not v or v.lower() == "unknown":
            unknown += 1
    if not math_expr or math_expr.lower() == "unknown":
        unknown += 1
    return unknown, total


def _normalize_ir_from_data(data: dict) -> dict:
    ir_payload = data.get("ir", {}) if isinstance(data.get("ir"), dict) else {}
    if "experience" not in ir_payload and isinstance(data.get("experience"), dict):
        ir_payload = {**ir_payload, "experience": data.get("experience")}
    return normalize_ir(ir_payload)


def collect_ir_summary(per_function_analysis: dict[str, FunctionAnalysis]) -> dict[str, object]:
    """Aggregate per-function IR into compact repo/group summary."""
    computation_types: list[str] = []
    memory_patterns: list[str] = []
    vectorizable_count = 0
    reduction_count = 0
    unknown_field_count = 0
    total_field_count = 0

    for fa in per_function_analysis.values():
        ir = normalize_ir(fa.ir)
        comp_type = str(ir["computation"].get("type", "unknown"))
        mem_pattern = str(ir["memory"].get("access_pattern", "contiguous"))
        if comp_type and comp_type not in computation_types:
            computation_types.append(comp_type)
        if mem_pattern and mem_pattern not in memory_patterns:
            memory_patterns.append(mem_pattern)
        if bool(ir["parallelism"].get("vectorizable", False)):
            vectorizable_count += 1
        if bool(ir["parallelism"].get("reduction", False)):
            reduction_count += 1
        u, t = _count_unknown_fields(ir)
        unknown_field_count += u
        total_field_count += t

    arch_simd_experience = collect_arch_simd_experience(per_function_analysis)

    return {
        "computation_types": computation_types,
        "memory_patterns": memory_patterns,
        "vectorizable_count": vectorizable_count,
        "reduction_count": reduction_count,
        "arch_simd_experience": arch_simd_experience,
        "unknown_field_count": unknown_field_count,
        "unknown_ratio": round((unknown_field_count / total_field_count), 4) if total_field_count > 0 else 0.0,
    }


def collect_riscv_simd_experience(
    existing_rvv_files: list[str],
    per_function_analysis: dict[str, FunctionAnalysis],
) -> dict[str, object]:
    """Aggregate RISC-V SIMD经验，供 repo_analyze JSON 复用。"""
    tags: list[str] = []
    seen_tags: set[str] = set()
    for fa in per_function_analysis.values():
        for t in extract_ir_tags(fa.ir):
            s = str(t).strip()
            if not s or s in seen_tags:
                continue
            seen_tags.add(s)
            tags.append(s)

    return {
        "existing_rvv_files": [str(x) for x in existing_rvv_files],
        "inferred_ir_tags": tags,
    }


def _sanitize_discovered_functions(data: dict) -> list[dict]:
    """Keep only DiscoveredFunction fields from LLM payload."""
    if not isinstance(data, dict):
        return []
    raw_functions = data.get("functions", [])
    return keep_dataclass_fields_list(raw_functions, DiscoveredFunction)


def discover_functions(
    cfg: AppConfig,
    code_context: str,
    target: MigrationTarget,
) -> FuncDiscoverArtifact:
    """FUNC_DISCOVER: identify all migratable functions for the target symbol.

    Calls LLM to analyze code context and discover function signatures.
    Updates target.functions with the discovered function names.
    """
    messages = [
        LlmMessage(role="system", content=system_prompt()),
        LlmMessage(role="user", content=function_discovery_prompt(target.symbol, code_context)),
    ]
    try:
        raw = chat_completion_with_retry(cfg.llm, messages, max_tokens=1200, stage="func_discover", max_retries=3)
        data = extract_json_from_llm(raw)
        functions = _sanitize_discovered_functions(data)
        artifact = load_func_discover_artifact(
            FuncDiscoverArtifact(
                functions=[DiscoveredFunction(**f) for f in functions],
                raw_text=raw,
                llm_used=True,
            )
        )
        for func in artifact.functions:
            if not func.role:
                func.role = "dependency" if func.dependencies else "core"
            if not func.semantic_hint:
                func.semantic_hint = "helper" if func.role == "dependency" else ""
        func_names = [f.name for f in artifact.functions if f.name]
        if func_names:
            target.functions = func_names
        return artifact
    except (LlmError, Exception) as e:
        print(f"[ANALYZE] 函数发现失败: {e}")
        if not prompt_yes_no("函数发现失败，是否使用 fallback（仅迁移目标 symbol）继续？", default=False):
            raise
        # Fallback: use the symbol itself as the only function
        if not target.functions:
            target.functions = [target.symbol]
        return FuncDiscoverArtifact(
            functions=[DiscoveredFunction(name=target.symbol, role="core", semantic_hint="fallback")],
            raw_text=str(e),
            llm_used=False,
        )


def _fallback_analysis(discovery: Discovery) -> dict:
    g = group_files(discovery)
    ir = default_ir()
    ir["computation"]["math_expression"] = "unresolved_from_fallback_context"
    ir["experience"]["arch_simd_experience"]["x86"] = [f"x86_ref:{x}" for x in g["x86_refs"][:5]]
    ir["experience"]["arch_simd_experience"]["arm"] = [f"arm_ref:{x}" for x in g["arm_refs"][:5]]
    ir["experience"]["arch_simd_experience"]["aarch64"] = [f"aarch64_ref:{x}" for x in g["aarch64_refs"][:5]]
    return {
        "symbol": discovery.symbol,
        "ir": ir,
        "simd_features": {
            "has_saturation": False,
            "has_widening": False,
            "has_narrowing": False,
        },
        "references": {
            "c": [f"{m.file}:{m.line}" for m in discovery.matches if m.file.endswith(".c")][:20],
            "x86": g["x86_refs"],
            "arm": g["arm_refs"],
        },
        "kb_match": {"matched_pattern_ids": [], "match_reason": "fallback-no-llm"},
        "notes": "LLM 未运行或解析失败，使用 fallback。",
        "confidence": 0.0,
    }


def _to_ref_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(x) for x in value if str(x).strip()]


def _build_kb_match(kb: KnowledgeBase | None, ir: dict) -> tuple[list[str], dict, list[dict]]:
    if kb is None:
        return [], {"matched_pattern_ids": [], "match_reason": "kb-unavailable"}, []
    ranked = kb.match_patterns_by_ir(ir, max_results=3)
    ids = [str(x.get("pattern_id", "")) for x in ranked if str(x.get("pattern_id", "")).strip()]
    reason = "; ".join(f"{x.get('pattern_id')}: {x.get('reason')} ({x.get('score', 0):.2f})" for x in ranked)
    kb_match = {
        "matched_pattern_ids": ids,
        "match_reason": reason or "no-match",
    }
    kb_patterns_list = [asdict(x["pattern"]) for x in ranked if x.get("pattern") is not None]
    return ids, kb_match, kb_patterns_list


def _enrich_ir_experience_with_refs(ir: dict, x86_refs: list[str], arm_refs: list[str]) -> dict:
    out = normalize_ir(ir)
    arch_exp = out["experience"]["arch_simd_experience"]

    if not arch_exp["x86"]:
        arch_exp["x86"] = [f"x86_ref:{x}" for x in x86_refs[:5]]

    if not arch_exp["arm"] and not arch_exp["aarch64"]:
        for r in arm_refs[:8]:
            rr = str(r)
            if "/aarch64/" in rr.replace("\\", "/"):
                arch_exp["aarch64"].append(f"aarch64_ref:{rr}")
            else:
                arch_exp["arm"].append(f"arm_ref:{rr}")
    return out


def analyze_with_llm(
    cfg: AppConfig,
    discovery: Discovery,
    functions: list[DiscoveredFunction] | None = None,
    *,
    context_override: str | None = None,
    prior_analysis: dict[str, dict] | None = None,
    build_errors: str | None = None,
    kb: KnowledgeBase | None = None,
) -> AnalysisArtifact:
    """调用 LLM 进行算子语义分析。

    Args:
        cfg: 应用配置。
        discovery: 符号检索结果。
        functions: 待分析的 function 列表（若为 None，回退到 symbol 级别分析）。
        context_override: 用完整函数体替换默认上下文。
        prior_analysis: 上轮已有分析 JSON（per-function dict，refine 时传入）。
        build_errors: 所有历次构建错误文本。
        kb: 知识库实例（用于 RAG）。
    """
    # 若无 functions，回退到原有 symbol 级别分析
    if not functions:
        ctx = context_override if context_override is not None else build_llm_context(discovery)
        messages = [
            LlmMessage(role="system", content=system_prompt()),
            LlmMessage(
                role="user",
                content=analysis_prompt(
                    discovery.symbol,
                    ctx,
                    prior_analysis=prior_analysis.get(discovery.symbol) if prior_analysis else None,
                    build_errors=build_errors,
                ),
            ),
        ]
        try:
            raw = chat_completion_with_retry(cfg.llm, messages, max_tokens=1600, stage="analyze", max_retries=3)
            data = extract_json_from_llm(raw)
            normalized = {
                "symbol": discovery.symbol,
                "ir": _normalize_ir_from_data(data),
                "simd_features": data.get("simd_features", {}) if isinstance(data.get("simd_features"), dict) else {},
                "references": data.get("references", {}) if isinstance(data.get("references"), dict) else {},
                "notes": str(data.get("notes", "")),
                "confidence": float(data.get("confidence", 0.0) or 0.0),
            }
            return AnalysisArtifact(
                analysis_json=normalized,
                symbol=discovery.symbol,
                raw_text=raw,
                llm_used=True,
            )
        except LlmError as e:
            print(f"[ANALYZE] 分析失败: {e}")
            if not prompt_yes_no("分析失败，是否使用 fallback 分析继续？", default=False):
                raise
            fb = _fallback_analysis(discovery)
            return AnalysisArtifact(
                analysis_json=fb,
                symbol=discovery.symbol,
                raw_text=str(e),
                llm_used=False,
                error=str(e),
            )
        except Exception as e:
            print(f"[ANALYZE] 分析异常: {e}")
            if not prompt_yes_no("分析异常，是否使用 fallback 分析继续？", default=False):
                raise
            fb = _fallback_analysis(discovery)
            return AnalysisArtifact(
                analysis_json=fb,
                symbol=discovery.symbol,
                raw_text=repr(e),
                llm_used=False,
                error=repr(e),
            )

    # Function 粒度分析
    per_function_analysis: dict[str, FunctionAnalysis] = {}
    llm_used_any = False
    for func in functions:
        func_name = func.name
        ctx = context_override if context_override is not None else build_llm_context(discovery)

        # KB RAG：搜索相关 pattern
        kb_pattern_ids: list[str] = []
        kb_error_classes: list[str] = []
        kb_patterns_list: list[dict] = []
        if kb:
            seed_patterns = kb.search_patterns(symbol=discovery.symbol, max_results=3)
            kb_patterns_list = [asdict(p) for p in seed_patterns]
            errors = kb.search_errors(keyword=func_name, max_results=2)
            kb_error_classes = list(set(e.error_class for e in errors if e.error_class))

        messages = [
            LlmMessage(role="system", content=system_prompt()),
            LlmMessage(
                role="user",
                content=function_analysis_prompt(
                    func_name,
                    ctx,
                    kb_patterns=kb_patterns_list if kb_patterns_list else None,
                    prior_analysis=prior_analysis.get(func_name) if prior_analysis else None,
                    build_errors=build_errors,
                ),
            ),
        ]
        try:
            raw = chat_completion_with_retry(
                cfg.llm,
                messages,
                max_tokens=1600,
                stage=f"analyze_{func_name}",
                max_retries=3,
            )
            data = extract_json_from_llm(raw)
            ir = _normalize_ir_from_data(data)
            matched_ids, kb_match, ranked_patterns = _build_kb_match(kb, ir)
            if matched_ids:
                kb_pattern_ids = matched_ids
            if ranked_patterns:
                kb_patterns_list = ranked_patterns

            refs = data.get("references", {}) if isinstance(data.get("references"), dict) else {}
            c_refs = _to_ref_list(refs.get("c", []))
            x86_refs = _to_ref_list(refs.get("x86", []))
            arm_refs = _to_ref_list(refs.get("arm", []))
            ir = _enrich_ir_experience_with_refs(ir, x86_refs, arm_refs)

            func_analysis = FunctionAnalysis(
                function_name=func_name,
                ir=ir,
                simd_features=data.get("simd_features", {}) if isinstance(data.get("simd_features"), dict) else {},
                c_candidates=c_refs,
                x86_refs=x86_refs,
                arm_refs=arm_refs,
                kb_match=kb_match,
                notes=str(data.get("notes", "")),
                kb_pattern_ids=kb_pattern_ids,
                kb_error_classes=kb_error_classes,
                migrate=int(data.get("migrate", 1)),
                migrate_reason=str(data.get("migrate_reason", "")),
            )
            per_function_analysis[func_name] = func_analysis
            llm_used_any = True
        except (LlmError, Exception) as e:
            print(f"[ANALYZE] 函数 {func_name} 分析失败: {e}")
            if not prompt_yes_no(f"函数 {func_name} 分析失败，是否使用该函数 fallback 继续？", default=False):
                raise
            func_analysis = FunctionAnalysis(
                function_name=func_name,
                ir=default_ir(),
                simd_features={
                    "has_saturation": False,
                    "has_widening": False,
                    "has_narrowing": False,
                },
                kb_match={"matched_pattern_ids": [], "match_reason": "analyze-failed"},
                kb_pattern_ids=kb_pattern_ids,
                kb_error_classes=kb_error_classes,
                notes=f"分析失败: {str(e)[:100]}",
                migrate=0,
                migrate_reason="分析失败，默认跳过该函数",
            )
            per_function_analysis[func_name] = func_analysis

    summary = collect_ir_summary(per_function_analysis)
    analysis_json = {
        "symbol": discovery.symbol,
        "ir_summary": summary,
        "function_analysis": {name: asdict(obj) for name, obj in per_function_analysis.items()},
    }

    return AnalysisArtifact(
        analysis_json=analysis_json,
        per_function_analysis=per_function_analysis,
        symbol=discovery.symbol,
        raw_text="",
        llm_used=llm_used_any,
    )

### 下面两个函数没有被调用
def update_function_analysis(
    artifact: AnalysisArtifact,
    function_name: str,
    updates: dict,
) -> AnalysisArtifact:
    """允许 plan refine 阶段修改 analyze 结果中的特定字段。

    Args:
        artifact: 原有 AnalysisArtifact
        function_name: 要修改的 function
        updates: 要修改的字段字典

    Returns:
        修改后的 artifact
    """
    if function_name not in artifact.per_function_analysis:
        return artifact

    func_analysis = artifact.per_function_analysis[function_name]
    for key, value in updates.items():
        if hasattr(func_analysis, key):
            setattr(func_analysis, key, value)

    return artifact


def get_function_analysis_for_context(
    artifact: AnalysisArtifact,
    function_name: str,
) -> dict:
    """提取单个 function 的分析结果，用于 context_builder 构建 LLM prompt。

    Returns:
        FunctionAnalysis 的 dict 形式
    """
    if function_name not in artifact.per_function_analysis:
        return {}

    func_analysis = artifact.per_function_analysis[function_name]
    return asdict(func_analysis)
