from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from .llm import LlmMessage, chat_completion_with_retry
from .prompts import system_prompt
from .prompts_linkage import linkage_trace_prompt
from .util import extract_json_from_llm
from .linkage_trace import collect_linkage_evidence


@dataclass
class LinkagePlan:
    library_root: str = ""
    arch: str = "riscv"

    module: str = ""
    target_symbol: str = ""

    leaf_reference_files: list[str] = field(default_factory=list)
    arch_init_reference_files: list[str] = field(default_factory=list)
    public_dispatch_files: list[str] = field(default_factory=list)
    makefile_files: list[str] = field(default_factory=list)

    riscv_init_file: str = ""
    riscv_impl_file: str = ""
    riscv_makefile: str = ""

    init_function: str = ""
    init_signature: str = ""

    binding_field: str = ""
    rvv_symbol: str = ""
    rvv_signature: str = ""

    required_objects: list[str] = field(default_factory=list)
    allowed_files: list[str] = field(default_factory=list)

    evidence_chain: list[dict] = field(default_factory=list)

    confidence: Literal["high", "medium", "low"] = "low"
    blocked_reason: str = ""


def _default_plan(module: str, symbol: str, blocked_reason: str = "") -> dict:
    plan = asdict(
        LinkagePlan(
            module=module,
            target_symbol=symbol,
            blocked_reason=blocked_reason,
        )
    )
    return plan


def _normalize_list_str(v: object) -> list[str]:
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if str(x).strip()]


def run_linkage_plan(
    llm,
    ffmpeg_root: Path,
    module: str,
    symbol: str,
    build_errors: str = "",
) -> dict:
    evidence = collect_linkage_evidence(ffmpeg_root, module, symbol)
    if not evidence.strip():
        return _default_plan(module, symbol, blocked_reason="empty_linkage_evidence")

    prompt = linkage_trace_prompt(module, symbol, evidence, build_errors)
    messages = [
        LlmMessage(role="system", content=system_prompt()),
        LlmMessage(role="user", content=prompt),
    ]

    try:
        raw = chat_completion_with_retry(
            llm,
            messages,
            max_tokens=4000,
            stage="linkage_trace",
            max_retries=3,
        )
        data = extract_json_from_llm(raw)
        if not isinstance(data, dict):
            return _default_plan(module, symbol, blocked_reason="invalid_linkage_trace_json")

        merged = _default_plan(module, symbol)
        merged.update(data)
        merged["module"] = module
        merged["target_symbol"] = symbol
        merged["arch"] = str(merged.get("arch", "riscv") or "riscv")

        confidence = str(merged.get("confidence", "low")).lower().strip()
        if confidence not in {"high", "medium", "low"}:
            merged["confidence"] = "low"

        for key in (
            "leaf_reference_files",
            "arch_init_reference_files",
            "public_dispatch_files",
            "makefile_files",
            "required_objects",
            "allowed_files",
        ):
            merged[key] = _normalize_list_str(merged.get(key, []))

        if not isinstance(merged.get("evidence_chain"), list):
            merged["evidence_chain"] = []

        # Backward compatibility: if old field exists, map it to riscv_impl_file when needed.
        old_asm = str(merged.get("riscv_asm_file", "") or "").strip()
        if not merged.get("riscv_impl_file") and old_asm:
            merged["riscv_impl_file"] = old_asm

        return merged
    except Exception as e:
        return _default_plan(module, symbol, blocked_reason=f"linkage_trace_failed:{type(e).__name__}")
