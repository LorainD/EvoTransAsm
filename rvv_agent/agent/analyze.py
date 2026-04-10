"""agent.analyze — Analysis Agent

负责：
  - 函数发现（discover_functions）— FUNC_DISCOVER 阶段
  - LLM 语义分析（AnalysisResult / analyze_with_llm）— ANALYZE 阶段
"""
from __future__ import annotations

import json
from dataclasses import asdict

from ..core.config import AppConfig
from ..core.llm import LlmError, LlmMessage, chat_completion_with_retry
from ..core.prompts import analysis_prompt, function_analysis_prompt, function_discovery_prompt, system_prompt
from ..core.task import AnalysisArtifact, FuncDiscoverArtifact, FunctionAnalysis, MigrationTarget, DiscoveredFunction, load_func_discover_artifact
from ..core.util import extract_json_from_llm, keep_dataclass_fields_list
from .search import Discovery, build_llm_context, group_files
from ..memory.knowledge_base import KnowledgeBase
from ..tool.interactive import prompt_yes_no

# Backward-compatible alias: historical callers import AnalysisResult.
# AnalysisResult = AnalysisArtifact


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
    return {
        "symbol": discovery.symbol,
        "datatype": "unknown",
        "vectorizable": True,
        "pattern": [],
        "has_stride": False,
        "has_saturation": False,
        "reduction": False,
        "tail_required": False,
        "math_expression": "unknown",
        "c_candidates": [f"{m.file}:{m.line}" for m in discovery.matches if m.file.endswith(".c")][:20],
        "x86_refs": g["x86_refs"],
        "arm_refs": g["arm_refs"],
        "notes": "LLM 未运行或解析失败，使用 fallback。",
    }


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
            return AnalysisArtifact(
                analysis_json=data,
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
            tags = [func.semantic_hint] if func.semantic_hint else []
            if func.role:
                tags.append(func.role)
            patterns = kb.search_patterns(tags=tags, max_results=3) if tags else []
            kb_pattern_ids = [p.pattern_id for p in patterns]
            kb_patterns_list = [asdict(p) for p in patterns]

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
            func_analysis = FunctionAnalysis(
                function_name=func_name,
                datatype=data.get("datatype", ""),
                vectorizable=data.get("vectorizable", False),
                pattern=data.get("pattern", []),
                has_stride=data.get("has_stride", False),
                has_saturation=data.get("has_saturation", False),
                reduction=data.get("reduction", False),
                tail_required=data.get("tail_required", False),
                math_expression=data.get("math_expression", ""),
                c_candidates=data.get("c_candidates", []),
                x86_refs=data.get("x86_refs", []),
                arm_refs=data.get("arm_refs", []),
                notes=data.get("notes", ""),
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
                datatype="unknown",
                vectorizable=False,
                kb_pattern_ids=kb_pattern_ids,
                kb_error_classes=kb_error_classes,
                notes=f"分析失败: {str(e)[:100]}",
                migrate=0,
                migrate_reason="分析失败，默认跳过该函数",
            )
            per_function_analysis[func_name] = func_analysis

    return AnalysisArtifact(
        per_function_analysis=per_function_analysis,
        symbol=discovery.symbol,
        raw_text="",
        llm_used=llm_used_any,
    )


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
