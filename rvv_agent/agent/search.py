"""agent.search — 搜索/检索 Agent

负责：
  - ffmpeg 源树 symbol 定位（Match / Discovery）
  - LLM 辅助筛选参考文件（RetrievalResult / select_references）
  - 从文件片段构建 LLM 上下文（build_context_from_files）
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from ..core.config import AppConfig
from ..core.llm import LlmError, LlmMessage, chat_completion_with_retry
from ..core.prompts import retrieval_prompt, system_prompt
from ..tool.interactive import prompt_yes_no

if TYPE_CHECKING:
    from .chat import Intent

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Match:
    file: str
    line: int
    text: str


@dataclass(frozen=True)
class Discovery:
    symbol: str
    matches: list[Match]

# ---------------------------------------------------------------------------
# Source file iteration
# ---------------------------------------------------------------------------

def _iter_source_files(ffmpeg_root: Path) -> Iterable[Path]:
    ex_dirs = {".git", "build"}
    for path in ffmpeg_root.rglob("*"):
        if path.is_dir():
            continue
        if any(part in ex_dirs for part in path.parts):
            continue
        if path.suffix not in {".c", ".h", ".S", ".s", ".inc", ".cpp", ".asm"}:
            continue
        yield path

# ---------------------------------------------------------------------------
# Symbol search
# ---------------------------------------------------------------------------

def find_symbol_multi(
    ffmpeg_root: Path,
    terms: list[str],
    *,
    primary: str | None = None,
    max_matches: int = 400,
) -> Discovery:
    """多关键词搜索并合并（按 file+line 去重）。"""
    seen_lines: dict[str, set[int]] = {}
    all_matches: list[Match] = []
    for term in terms:
        token_re = re.compile(r"\b" + re.escape(term) + r"\b")
        for file in _iter_source_files(ffmpeg_root):
            rel = str(file.relative_to(ffmpeg_root)).replace("\\", "/")
            try:
                text = file.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if term not in text:
                continue
            is_asm_file = file.suffix in {".S", ".s", ".asm"}
            lines_seen = seen_lines.setdefault(rel, set())
            for i, line in enumerate(text.splitlines(), start=1):
                if i in lines_seen:
                    continue
                if (is_asm_file and term in line) or (not is_asm_file and token_re.search(line)):
                    lines_seen.add(i)
                    all_matches.append(Match(file=rel, line=i, text=line.strip()))
                    if len(all_matches) >= max_matches:
                        return Discovery(symbol=primary or terms[0], matches=all_matches)
    return Discovery(symbol=primary or terms[0], matches=all_matches)


def find_symbol(ffmpeg_root: Path, symbol: str, *, max_matches: int = 400) -> Discovery:
    token_re = re.compile(r"\b" + re.escape(symbol) + r"\b")
    matches: list[Match] = []
    for file in _iter_source_files(ffmpeg_root):
        try:
            text = file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if symbol not in text:
            continue
        is_asm_file = file.suffix in {".S", ".s", ".asm"}
        for i, line in enumerate(text.splitlines(), start=1):
            if (is_asm_file and symbol in line) or (not is_asm_file and token_re.search(line)):
                matches.append(Match(
                    file=str(file.relative_to(ffmpeg_root)).replace("\\", "/"),
                    line=i,
                    text=line.strip(),
                ))
                if len(matches) >= max_matches:
                    return Discovery(symbol=symbol, matches=matches)
    return Discovery(symbol=symbol, matches=matches)


def group_files(discovery: Discovery) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {
        "c_candidates": [], "x86_refs": [], "arm_refs": [],
        "aarch64_refs": [], "riscv_refs": [], "headers": [], "other": [],
    }
    seen: set[str] = set()
    for m in discovery.matches:
        f = m.file
        if f in seen:
            continue
        seen.add(f)
        if f.endswith(".h") or f.endswith(".inc"):
            groups["headers"].append(f)
        elif "/x86/" in f:
            groups["x86_refs"].append(f)
        elif "/arm/" in f:
            groups["arm_refs"].append(f)
        elif "/aarch64/" in f:
            groups["aarch64_refs"].append(f)
        elif "/riscv/" in f:
            groups["riscv_refs"].append(f)
        elif f.endswith(".c") or f.endswith(".cpp"):
            groups["c_candidates"].append(f)
        else:
            groups["other"].append(f)
    return groups


def build_llm_context(discovery: Discovery, *, max_lines: int = 160) -> str:
    lines: list[str] = []
    for m in discovery.matches[:max_lines]:
        lines.append(f"{m.file}:{m.line}: {m.text}")
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Intelligent function body extraction helpers
# ---------------------------------------------------------------------------

_C_FUNC_DEF = re.compile(
    r"^[a-zA-Z_][a-zA-Z0-9_\s\*]+\s+[a-zA-Z_][a-zA-Z0-9_]*\s*\("
)
_ASM_LABEL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*:")
_ASM_PROC_START = re.compile(
    r"^\s*(?:func|endfunc|\.globl|SYM_FUNC_START|SYM_FUNC_END|\.proc)\b"
)


def _extract_c_function(lines: list[str], hit_idx: int) -> tuple[int, int]:
    """提取包含 hit_idx 行的完整 C 函数体（从函数签名到对应的 \'}\'）。

    向上寻找函数定义行（返回类型+函数名+左括号），向下通过花括号计数找到函数结束。
    返回 (start_idx, end_idx_exclusive) 两个 0-based 行索引。
    """
    # ── 向上找函数签名起点 ──────────────────────────────────────────────
    func_start = hit_idx
    for i in range(hit_idx, max(-1, hit_idx - 80), -1):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("//") or stripped.startswith("*"):
            continue
        if "(" in stripped and not stripped.startswith(("if", "while", "for",
                                                          "switch", "return",
                                                          "else", "#")):
            if _C_FUNC_DEF.match(stripped) or (
                i > 0 and _C_FUNC_DEF.match(lines[i - 1].strip() + " " + stripped)
            ):
                func_start = i
                break
            if i < hit_idx and re.match(r"^[a-zA-Z_][a-zA-Z0-9_\s\*]*\s*\(", stripped):
                func_start = i
                break

    # ── 向下找函数体结束（花括号计数）──────────────────────────────────
    depth = 0
    func_end = min(hit_idx + 1, len(lines))
    found_open = False
    for i in range(func_start, len(lines)):
        for ch in lines[i]:
            if ch == "{":
                depth += 1
                found_open = True
            elif ch == "}":
                depth -= 1
        if found_open and depth == 0:
            func_end = i + 1
            break

    return func_start, func_end


def _extract_asm_procedure(lines: list[str], hit_idx: int) -> tuple[int, int]:
    """提取包含 hit_idx 行的完整汇编过程（从 func/globl/label 到 endfunc/.size）。

    返回 (start_idx, end_idx_exclusive) 两个 0-based 行索引。
    """
    # ── 向上找过程/函数起点 ─────────────────────────────────────────────
    proc_start = hit_idx
    for i in range(hit_idx, max(-1, hit_idx - 120), -1):
        line = lines[i].strip()
        if _ASM_PROC_START.match(lines[i]) or _ASM_LABEL.match(line):
            proc_start = i
            break

    # ── 向下找过程结束（endfunc / .size / 下一个 .globl / 等）──────────
    proc_end = len(lines)
    for i in range(hit_idx + 1, len(lines)):
        line = lines[i].strip()
        if "endfunc" in line or "SYM_FUNC_END" in line:
            proc_end = i + 1
            break
        if ".size" in line:
            proc_end = i + 1
            break
        if _ASM_LABEL.match(line) and not lines[i].startswith(" ") and i > hit_idx + 2:
            proc_end = i
            break

    return proc_start, proc_end


def _dedupe_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并重叠或相邻的行范围。"""
    if not ranges:
        return []
    sorted_r = sorted(ranges)
    merged: list[tuple[int, int]] = [sorted_r[0]]
    for s, e in sorted_r[1:]:
        ms, me = merged[-1]
        if s <= me:
            merged[-1] = (ms, max(me, e))
        else:
            merged.append((s, e))
    return merged


# ---------------------------------------------------------------------------
# Context builder from files — 智能函数体提取版
# ---------------------------------------------------------------------------

def build_context_from_files(
    ffmpeg_root: Path,
    *,
    symbol: str,
    files: list[str],
    max_total_chars: int = 40000,
    max_funcs_per_file: int = 6,
) -> str:
    """根据算子/函数名，从指定文件列表中提取相关性最高的完整函数体。

    改进点（相比旧版简单截取头N行）：
    - **C 文件**：定位包含 symbol 的函数，提取完整函数体（从返回类型到对应 \'}\'）。
    - **汇编文件**：提取包含 symbol 的完整汇编过程（从 func label 到 endfunc/.size）。
    - **头文件 / Makefile**：仍取文件头（通常不大），但展示完整内容。
    - 所有文件内容均带行号，便于 LLM 精确定位。
    """
    func_name = symbol.split(".")[-1] if "." in symbol else symbol
    token_re = re.compile(r"\b" + re.escape(func_name) + r"\b")
    module = symbol.split(".")[0] if "." in symbol else ""
    module_re = re.compile(r"\b" + re.escape(module) + r"\b") if module else None

    chunks: list[str] = []
    total = 0

    for rel in files:
        p = ffmpeg_root / rel
        if not p.exists() or not p.is_file():
            continue
        try:
            raw_text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        lines = raw_text.splitlines()
        suffix = p.suffix.lower()
        is_asm = suffix in {".s", ".asm"}
        is_c = suffix in {".c", ".cpp", ".h", ".inc"}
        is_make = p.name in {"Makefile", "makefile"} or suffix in {".mk", ".mak"}

        if is_make or (not is_asm and not is_c):
            block_lines = [f"{i+1:5d}: {l}" for i, l in enumerate(lines[:200])]
            block = f"--- {rel} (full/{len(lines)} lines) ---\n" + "\n".join(block_lines) + "\n"
            chunks.append(block)
            total += len(block)
            continue

        hits: list[int] = []
        for i, line in enumerate(lines):
            if token_re.search(line):
                hits.append(i)
            elif module_re and module_re.search(line):
                hits.append(i)

        if not hits:
            head = lines[:60]
            block = (
                f"--- {rel} (no direct match, showing head {len(head)} lines) ---\n"
                + "\n".join(f"{i+1:5d}: {l}" for i, l in enumerate(head))
                + "\n"
            )
            chunks.append(block)
            total += len(block)
            continue

        ranges: list[tuple[int, int]] = []
        seen_starts: set[int] = set()
        for hit in hits[:max_funcs_per_file * 3]:
            if is_asm:
                s, e = _extract_asm_procedure(lines, hit)
            else:
                s, e = _extract_c_function(lines, hit)
            if s not in seen_starts:
                seen_starts.add(s)
                ranges.append((s, e))
            if len(ranges) >= max_funcs_per_file:
                break

        merged = _dedupe_ranges(ranges)

        func_blocks: list[str] = []
        for s, e in merged:
            func_lines = lines[s:e]
            snippet = "\n".join(f"{s+i+1:5d}: {l}" for i, l in enumerate(func_lines))
            func_blocks.append(snippet)

        block = (
            f"--- {rel} ({len(merged)} function(s) containing \'{func_name}\') ---\n"
            + "\n\n".join(func_blocks)
            + "\n"
        )
        chunks.append(block)
        total += len(block)

    result = "\n".join(chunks)
    if total > max_total_chars:
        result = result[:max_total_chars]
    return result
# ---------------------------------------------------------------------------
# Retrieval result + LLM-assisted reference selection
# ---------------------------------------------------------------------------
from ..core.task import FileSearchArtifact

from ..core.util import extract_json_from_llm, now_id

# Alias for backward compat within this module
_extract_retrieval_json = extract_json_from_llm


def _fallback_selection(discovery: Discovery) -> dict:
    g = group_files(discovery)
    return {
        "symbol": discovery.symbol,
        "c": g["c_candidates"][:5],
        "x86": g["x86_refs"][:5],
        "arm": g["arm_refs"][:5] + g["aarch64_refs"][:5],
        "riscv": g["riscv_refs"][:5],
        "headers": g["headers"][:5],
        "makefiles": ["libavcodec/riscv/Makefile"],
        "checkasm": ["tests/checkasm/checkasm.c"],
        "notes": "LLM 未运行或解析失败，使用 fallback 选择。",
    }


def _scan_existing_rvv(ffmpeg_root: Path, module: str) -> list[str]:
    """Scan all lib*/riscv directories for existing RVV files matching module."""
    module_lower = module.lower()
    found: list[str] = []
    for lib_dir in sorted(ffmpeg_root.glob("lib*")):
        riscv_dir = lib_dir / "riscv"
        if not riscv_dir.is_dir():
            continue
        for f in sorted(riscv_dir.iterdir()):
            if f.is_file() and module_lower in f.name.lower():
                found.append(str(f.relative_to(ffmpeg_root)).replace("\\", "/"))
    return found


def select_references(
    cfg: AppConfig,
    ffmpeg_root: Path,
    intent_or_symbol: "Intent | str",
) -> FileSearchArtifact:
    """LLM 辅助筛选参考文件。"""
    if isinstance(intent_or_symbol, str):
        symbol = intent_or_symbol
        module = intent_or_symbol
        terms = [symbol]
    else:
        symbol = intent_or_symbol.symbol
        module = intent_or_symbol.module
        terms = intent_or_symbol.search_terms

    existing_rvv = _scan_existing_rvv(ffmpeg_root, module)
    if len(terms) > 1:
        discovery = find_symbol_multi(ffmpeg_root, terms, primary=symbol)
    else:
        discovery = find_symbol(ffmpeg_root, symbol)
    grouped = group_files(discovery)

    file_search_id = now_id()

    messages = [
        LlmMessage(role="system", content=system_prompt()),
        LlmMessage(role="user", content=retrieval_prompt(symbol, grouped, discovery.matches[:120])),
    ]
    try:
        raw = chat_completion_with_retry(cfg.llm, messages, max_tokens=900, stage="retrieve", max_retries=3)
        data = _extract_retrieval_json(raw)
        if not isinstance(data, dict):
            raise ValueError("retrieval json is not dict")
        data["existing_rvv"] = existing_rvv
        data["_discovery"] = {"symbol": discovery.symbol, "matches": [m.__dict__ for m in discovery.matches[:200]]}
        return FileSearchArtifact(
            file_search_id=file_search_id,
            module=module,
            symbol=symbol,
            selected_json=data,
            raw_text=raw,
            llm_used=True,
        )
    except LlmError as e:
        print(f"[SEARCH] LLM 检索失败: {e}")
        if not prompt_yes_no("是否使用 fallback 检索结果继续？", default=False):
            raise
        fb = _fallback_selection(discovery)
        fb["existing_rvv"] = existing_rvv
        fb["_discovery"] = {"symbol": discovery.symbol, "matches": [m.__dict__ for m in discovery.matches[:200]]}
        return FileSearchArtifact(
            file_search_id=file_search_id,
            module=module,
            symbol=symbol,
            selected_json=fb,
            raw_text=str(e),
            llm_used=False,
            error=str(e),
        )
    except Exception as e:
        print(f"[SEARCH] LLM 检索异常: {e}")
        if not prompt_yes_no("是否使用 fallback 检索结果继续？", default=False):
            raise
        fb = _fallback_selection(discovery)
        fb["existing_rvv"] = existing_rvv
        fb["_discovery"] = {"symbol": discovery.symbol, "matches": [m.__dict__ for m in discovery.matches[:200]]}
        return FileSearchArtifact(
            file_search_id=file_search_id,
            module=module,
            symbol=symbol,
            selected_json=fb,
            raw_text=repr(e),
            llm_used=False,
            error=repr(e),
        )


# ---------------------------------------------------------------------------
# Context-aware stage wrapper (DEPRECATED — 已被状态机架构替代)
# ---------------------------------------------------------------------------
# def search(ctx: "MigrationContext") -> "MigrationContext":
#     """Context-aware search stage.
#
#     .. deprecated::
#         Pipeline now uses state-machine handlers. Kept for backward compat.
#
#     Updates
#     -------
#     ``ctx.discovery``   — full :class:`Discovery` result.
#     ``ctx.source_file`` — primary source file path (first match, if any).
#     """
#     disc = find_symbol(ctx.repo_root, ctx.operator)
#     ctx.discovery = disc
#     if disc.matches:
#         ctx.source_file = disc.matches[0].file
#     return ctx
