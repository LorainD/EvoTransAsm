"""agent.plan — Plan Agent

负责：
  - 迁移计划生成（Plan / fixed_plan / llm_plan）
"""
from __future__ import annotations

from ..core.config import AppConfig
from ..core.llm import LlmError, LlmMessage, chat_completion
from ..core.prompts import plan_prompt, system_prompt
from ..core.task import PlanArtifact
from ..core.util import extract_json_from_llm

# Backward-compatible alias: historical callers import Plan from this module.
Plan = PlanArtifact


def fixed_plan(symbol: str, functions: list[str] | None = None) -> PlanArtifact:
    """LLM 不可用时的硬编码兜底计划。"""
    return PlanArtifact(
        steps=[
            f"意图解析：迁移 {symbol}",
            "定位 C 实现",
            "定位 x86 / ARM 参考实现",
            "语义抽象（结构化任务描述 JSON）",
            f"调用 LLM 生成 {symbol} 的 RVV asm + init + Makefile patch（落到 runs/）",
            "(可选）把补丁应用到 workspace",
            "(可选）交叉 configure + build checkasm",
            "生成 run 报告（轨迹、输入输出、命令、摘要）",
        ],
        function_order=functions or [symbol],
        acceptance_criteria={"build_ok": True},
    )


def llm_plan(cfg: AppConfig, symbol: str, functions: list[str] | None = None) -> PlanArtifact:
    """调用 LLM 生成针对 symbol 的迁移计划，失败时回退到 fixed_plan。"""
    messages = [
        LlmMessage(role="system", content=system_prompt()),
        LlmMessage(role="user", content=plan_prompt(symbol, functions)),
    ]
    try:
        raw = chat_completion(cfg.llm, messages, max_tokens=800, stage="plan")
        data = extract_json_from_llm(raw)
        steps = data.get("steps", [])
        if not isinstance(steps, list) or not steps:
            raise ValueError("empty steps")
        func_order = data.get("function_order", [])
        if not isinstance(func_order, list):
            func_order = []
        func_order = [str(f).strip() for f in func_order if str(f).strip()]
        # Fallback: use discovered functions or symbol
        if not func_order:
            func_order = functions or [symbol]
        return PlanArtifact(
            steps=[str(s).strip() for s in steps if str(s).strip()],
            function_order=func_order,
            acceptance_criteria={"build_ok": True},
        )
    except (LlmError, Exception):
        return fixed_plan(symbol, functions)
