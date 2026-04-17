"""agent: Agent 层 — 意图解析、搜索/检索、分析、代码生成、构建修复、报告、交互对话。

子模块分工
----------
analyze  — 函数发现 + LLM 语义分析（discover_functions / AnalysisResult / analyze_with_llm）
chat     — 交互式 human-in-the-loop 迁移循环（状态机驱动）
debug    — 结构化构建错误诊断 + 回滚决策
intent   — 用户意图解析（Intent / parse_intent）
patch    — PATCH 阶段（生成/应用/回退自愈，chat 模式）
plan     — 迁移计划生成（Plan / fixed_plan / llm_plan）
report   — 运行报告落盘（write_report）
search   — 源码搜索、参考文件检索、上下文构建
"""

from .analyze import analyze_with_llm, discover_functions
from .chat import run_chat
from .debug import run_debug_handler
from .intent import Intent, parse_intent
from .plan import Plan, fixed_plan, llm_plan
from .report import write_report, write_chat_report
from .search import (
    Discovery,
    Match,
    build_context_from_files,
    find_symbol,
    find_symbol_multi,
    select_references,
)

__all__ = [
    # analyze
     "analyze_with_llm", "discover_functions",
    # chat
    "run_chat",
    # debug
    "run_debug_handler",
    # intent
    "Intent", "parse_intent",
    # plan
    "Plan", "fixed_plan", "llm_plan",
    # report
    "write_report", "write_chat_report",
    # search
    "Discovery", "Match",
    "build_context_from_files", "find_symbol", "find_symbol_multi", "select_references",
]
