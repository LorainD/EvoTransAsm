"""agent.analyze — Analysis Agent

负责：
  - 函数发现（discover_functions）— FUNC_DISCOVER 阶段
  - LLM 语义分析（AnalysisResult / analyze_with_llm）— ANALYZE 阶段
"""
from __future__ import annotations

import json

from ..core.config import AppConfig
from ..core.llm import LlmError, LlmMessage, chat_completion
from ..core.prompts import analysis_prompt, function_discovery_prompt, system_prompt
from ..core.task import AnalysisArtifact, FuncDiscoverArtifact, MigrationTarget
from ..core.util import extract_json_from_llm
from .search import Discovery, build_llm_context, group_files

# Backward-compatible alias: historical callers import AnalysisResult.
# AnalysisResult = AnalysisArtifact


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
        raw = chat_completion(cfg.llm, messages, max_tokens=1200, stage="func_discover")
        data = extract_json_from_llm(raw)
        functions = data.get("functions", [])
        # Update target.functions with discovered names
        func_names = [str(f.get("name", "")).strip() for f in functions if f.get("name")]
        if func_names:
            target.functions = func_names
        return FuncDiscoverArtifact(
            functions=functions,
            raw_text=raw,
            llm_used=True,
        )
    except (LlmError, Exception) as e:
        # Fallback: use the symbol itself as the only function
        if not target.functions:
            target.functions = [target.symbol]
        return FuncDiscoverArtifact(
            functions=[{"name": target.symbol, "reason": "fallback"}],
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
    *,
    context_override: str | None = None,
    prior_analysis: dict | None = None,
    build_errors: str | None = None,
) -> AnalysisArtifact:
    """调用 LLM 进行算子语义分析。

    Args:
        cfg: 应用配置。
        discovery: 符号检索结果。
        context_override: 用完整函数体替换默认上下文。
        prior_analysis: 上轮已有分析 JSON（refine 时传入，LLM 可在此基础上修正）。
        build_errors: 所有历次构建错误文本（帮助 LLM 修正数据类型/指令判断）。
    """
    ctx = context_override if context_override is not None else build_llm_context(discovery)
    messages = [
        LlmMessage(role="system", content=system_prompt()),
        LlmMessage(
            role="user",
            content=analysis_prompt(
                discovery.symbol,
                ctx,
                prior_analysis=prior_analysis,
                build_errors=build_errors,
            ),
        ),
    ]
    try:
        raw = chat_completion(cfg.llm, messages, max_tokens=1600, stage="analyze")
        data = json.loads(raw)
        return AnalysisArtifact(analysis_json=data, raw_text=raw, llm_used=True)
    except LlmError as e:
        fb = _fallback_analysis(discovery)
        return AnalysisArtifact(analysis_json=fb, raw_text=str(e), llm_used=False, error=str(e))
    except Exception as e:
        fb = _fallback_analysis(discovery)
        return AnalysisArtifact(analysis_json=fb, raw_text=repr(e), llm_used=False, error=repr(e))
