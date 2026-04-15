"""agent.intent — 用户意图解析"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..core.config import AppConfig
from ..core.llm import LlmError, LlmMessage, api_key_present, chat_completion_with_retry
from ..core.prompts import intent_prompt, system_prompt
from ..core.task import MigrationTarget
from ..tool.interactive import prompt_yes_no


@dataclass
class Intent:
    action: str  # chat | migrate
    raw: str
    llm_used: bool
    error: str | None = None
    target: MigrationTarget | None = None

    @property
    def symbol(self) -> str:
        """Derived from target to avoid duplicate storage."""
        return self.target.symbol if self.target else ""

    @property
    def module(self) -> str:
        if self.target:
            return self.target.module
        if "." in self.symbol:
            return self.symbol.split(".")[0]
        return self.symbol

    @property
    def func_name(self) -> str:
        if self.target:
            return self.target.functions[0] if self.target.functions else self.target.symbol
        if "." in self.symbol:
            return self.symbol.split(".", 1)[1]
        return self.symbol

    @property
    def search_terms(self) -> list[str]:
        if "." in self.symbol:
            return [self.func_name, self.module, self.symbol]
        return [self.symbol]


from ..core.util import extract_json_from_llm

# Alias for backward compat within this module
_extract_json = extract_json_from_llm


_DOT_IDENT = r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"


def _extract_symbol_heuristic(user_text: str) -> str:
    m = re.search(
        r"(?:\b(?:symbol|operator|kernel|function)\b|算子|函数|符号)"
        r"\s*[:：]?\s*`?(" + _DOT_IDENT + r")`?",
        user_text, flags=re.IGNORECASE,
    )
    if m:
        return m.group(1)
    m = re.search(r"\b(ff_[A-Za-z0-9_.]+)\b", user_text)
    if m:
        return m.group(1)
    m = re.search(
        r"(?:迁移|移植|migrate|port)\s+(" + _DOT_IDENT + r")(?:\s|$|到|的|为|至)",
        user_text, flags=re.IGNORECASE,
    )
    if m:
        return m.group(1)
    m = re.search(
        r"(" + _DOT_IDENT + r")\s*(?:算子|函数|符号|\boperator\b|\bfunction\b|\bkernel\b)",
        user_text, flags=re.IGNORECASE,
    )
    if m:
        return m.group(1)
    t = user_text.strip()
    if re.fullmatch(_DOT_IDENT, t):
        if "_" in t or "." in t or len(t) >= 8:
            return t
    return ""


def _has_ffmpeg_context(user_text: str) -> bool:
    t = user_text.lower()
    return ("ffmpeg" in t) or ("libav" in t) or ("checkasm" in t)


def _looks_like_migrate(user_text: str) -> bool:
    t = user_text.strip().lower()
    keywords = ["迁移", "移植", "rvv", "riscv", "simd", "优化", "汇编",
                "checkasm", "编译", "生成", "patch", "migrate", "port"]
    return any(k in t for k in keywords)


def _build_target(symbol: str) -> MigrationTarget | None:
    """Construct a MigrationTarget from a symbol string."""
    if not symbol:
        return None
    if "." in symbol:
        module = symbol.split(".")[0]
    else:
        module = symbol
    return MigrationTarget(module=module, symbol=symbol)

#TODO：llm识别target和module的优先级应该高于硬编码，硬编码作为fallback
def parse_intent(cfg: AppConfig, user_text: str) -> Intent:
    wants_migrate = _has_ffmpeg_context(user_text) and _looks_like_migrate(user_text)
    sym = _extract_symbol_heuristic(user_text)

    # 1) 若有 LLM，可用时优先走 LLM 解析；启发式仅作为兜底
    if api_key_present(cfg.llm):
        messages = [
            LlmMessage(role="system", content=system_prompt()),
            LlmMessage(role="user", content=intent_prompt(user_text)),
        ]
        try:
            raw = chat_completion_with_retry(cfg.llm, messages, max_tokens=220, stage="intent", max_retries=3)
            data = _extract_json(raw)
            action = str(data.get("action", "chat")).strip().lower()
            if action not in {"chat", "migrate"}:
                action = "chat"
            # LLM 解析出的 symbol 优先，其次再回退到启发式识别的 symbol
            symbol = str(data.get("symbol", "")).strip() or sym
            if wants_migrate and action != "migrate":
                action = "migrate"
            target = _build_target(symbol) if action == "migrate" else None
            return Intent(action=action, raw=raw,
                          llm_used=True, target=target)
        except LlmError as e:
            print(f"[INTENT] LLM 解析失败: {e}")
            if not prompt_yes_no("是否使用启发式意图识别继续？", default=False):
                raise
            action = "migrate" if wants_migrate else "chat"
            return Intent(action=action, raw=str(e),
                          llm_used=False, error=str(e), target=_build_target(sym) if action == "migrate" else None)
        except Exception as e:
            print(f"[INTENT] LLM 解析异常: {e}")
            if not prompt_yes_no("是否使用启发式意图识别继续？", default=False):
                raise
            action = "migrate" if wants_migrate else "chat"
            return Intent(action=action, raw=repr(e),
                          llm_used=False, error=repr(e), target=_build_target(sym) if action == "migrate" else None)

    # 2) 无可用 LLM 时，再使用启发式规则作为主流程
    if sym and user_text.strip() == sym:
        return Intent(action="migrate", raw="heuristic:symbol_only",
                      llm_used=False, target=_build_target(sym))
    if wants_migrate and sym:
        return Intent(action="migrate", raw="heuristic:migrate",
                      llm_used=False, target=_build_target(sym))

    action = "migrate" if wants_migrate else "chat"
    return Intent(action=action, raw="no_llm",
                  llm_used=False, target=_build_target(sym) if action == "migrate" else None)
#FIXME：出现没有llm的情况下，应该重连，结合patch.py，应该重写一个llm的错误处理机制，或者说在chat.py中就处理好这个问题，不要把没有llm的情况传递到后续阶段了