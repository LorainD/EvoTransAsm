from __future__ import annotations

import datetime as dt
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CmdResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str


def now_id() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", text.strip())
    return s[:120] if len(s) > 120 else s


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def write_text(p: Path, content: str) -> None:
    ensure_dir(p.parent)
    p.write_text(content, encoding="utf-8")


def write_json(p: Path, obj: object) -> None:
    write_text(p, json.dumps(obj, ensure_ascii=False, indent=2))


def fmt_argv(argv: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)


def run_cmd(argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> CmdResult:
    merged = os.environ.copy()
    if env:
        merged.update(env)

    p = subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        env=merged,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    return CmdResult(argv=list(argv), returncode=p.returncode, stdout=p.stdout, stderr=p.stderr)


def run_cmd_stream(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> CmdResult:
    """Run a command, printing stdout/stderr in real time and returning full captured output."""
    import sys
    merged = os.environ.copy()
    if env:
        merged.update(env)

    proc = subprocess.Popen(
        argv,
        cwd=str(cwd) if cwd else None,
        env=merged,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # merge stderr into stdout for unified stream
    )

    out_lines: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        out_lines.append(line)
    proc.wait()
    combined = "".join(out_lines)
    return CmdResult(argv=list(argv), returncode=proc.returncode, stdout=combined, stderr="")


# ---------------------------------------------------------------------------
# Terminal color helpers
# ---------------------------------------------------------------------------
import sys as _sys

_ANSI_RED    = "\033[31;1m"
_ANSI_YELLOW = "\033[33;1m"
_ANSI_RESET  = "\033[0m"


def _color_supported() -> bool:
    """Return True when the terminal likely supports ANSI colors."""
    # Honour NO_COLOR convention; also skip if piped
    import os
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return _sys.stdout.isatty()
    except Exception:
        return False


def print_red(msg: str) -> None:
    """Print *msg* in bold red to stdout (falls back to plain if no TTY)."""
    if _color_supported():
        print(f"{_ANSI_RED}{msg}{_ANSI_RESET}")
    else:
        print(msg)


def print_yellow(msg: str) -> None:
    """Print *msg* in bold yellow to stdout."""
    if _color_supported():
        print(f"{_ANSI_YELLOW}{msg}{_ANSI_RESET}")
    else:
        print(msg)


def print_llm_error(err: Exception | str, stage: str = "") -> None:
    """Classify *err* and print an actionable red-text diagnosis.

    Distinguishes between:
    - connection / timeout failures → remind user to check network / endpoint
    - auth errors (401/403)         → remind user to refresh API key
    - rate limit (429)              → suggest waiting or switching key
    - other errors                  → generic LLM failure message
    """
    import urllib.error

    msg = str(err)
    stage_tag = f"[{stage}] " if stage else ""

    if isinstance(err, urllib.error.URLError) or "urlopen error" in msg or "Connection" in msg or "Timeout" in msg or "timed out" in msg.lower():
        print_red(
            f"\n{'='*60}\n"
            f"  {stage_tag}⚠  LLM 网络连接失败 / 超时，无法访问接口！\n"
            f"  请检查：① 网络连通性  ② rvv_agent.toml 中的 base_url\n"
            f"  错误详情：{msg[:300]}\n"
            f"{'='*60}\n"
        )
    elif "401" in msg or "403" in msg or "Unauthorized" in msg or "Forbidden" in msg:
        print_red(
            f"\n{'='*60}\n"
            f"  {stage_tag}⚠  LLM 认证失败！API key 无效或已过期。\n"
            f"  请更新环境变量 (export API_KEY=...) 或 rvv_agent.toml 配置。\n"
            f"  错误详情：{msg[:300]}\n"
            f"{'='*60}\n"
        )
    elif "429" in msg or "rate limit" in msg.lower() or "quota" in msg.lower():
        print_red(
            f"\n{'='*60}\n"
            f"  {stage_tag}⚠  LLM 速率限制 / 配额耗尽！\n"
            f"  请稍等片刻后重试，或更换 API key / endpoint。\n"
            f"  错误详情：{msg[:300]}\n"
            f"{'='*60}\n"
        )
    elif "Missing API key" in msg or "api_key_env" in msg:
        print_red(
            f"\n{'='*60}\n"
            f"  {stage_tag}⚠  未找到 API key！\n"
            f"  请设置对应的环境变量（见 rvv_agent.toml 中的 api_key_env 配置）。\n"
            f"  错误详情：{msg[:300]}\n"
            f"{'='*60}\n"
        )
    else:
        print_red(
            f"\n{'='*60}\n"
            f"  {stage_tag}⚠  LLM 调用失败！\n"
            f"  如多次出现，请检查 endpoint_url / API key / 网络。\n"
            f"  错误详情：{msg[:300]}\n"
            f"{'='*60}\n"
        )


# ---------------------------------------------------------------------------
# Smart build-error extractor
# ---------------------------------------------------------------------------
import re as _re

_ERROR_PATTERNS = _re.compile(
    r"(error:|fatal error:|undefined reference|ld returned|cannot find|"
    r"no such file|implicit declaration|conflicting types|"
    r"note:|warning:.*error|make\[\d+\].*Error)",
    _re.IGNORECASE,
)


def extract_build_errors(output: str, tail_lines: int = 60, max_chars: int = 4000) -> str:
    """Return the most diagnostically useful portion of a build log.

    Strategy (in priority order):
    1. Collect every line that matches a known compiler/linker error pattern.
    2. Always include the last *tail_lines* lines (errors appear at the end).
    3. Deduplicate while preserving original order.
    4. Cap the result at *max_chars* characters (taken from the **end**,
       so the most recent errors are never truncated).
    """
    lines = output.splitlines()
    if not lines:
        return output[:max_chars]

    seen: set[int] = set()
    selected: list[tuple[int, str]] = []

    # Pass 1 – error-pattern lines
    for i, ln in enumerate(lines):
        if _ERROR_PATTERNS.search(ln):
            seen.add(i)
            selected.append((i, ln))

    # Pass 2 – tail lines
    tail_start = max(0, len(lines) - tail_lines)
    for i in range(tail_start, len(lines)):
        if i not in seen:
            seen.add(i)
            selected.append((i, lines[i]))

    # Sort by original line number to restore context order
    selected.sort(key=lambda t: t[0])
    result = "\n".join(ln for _, ln in selected)

    # Cap from the end so the most recent diagnostics are always present
    if len(result) > max_chars:
        result = result[-max_chars:]
    return result


# ---------------------------------------------------------------------------
# LLM response JSON extraction (shared across all agent modules)
# ---------------------------------------------------------------------------

def _iter_balanced_json_objects(text: str) -> list[str]:
    """Extract balanced top-level JSON object substrings from text."""
    objs: list[str] = []
    in_str = False
    esc = False
    depth = 0
    start = -1

    for idx, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue

        if ch == '"':
            in_str = True
            continue

        if ch == "{":
            if depth == 0:
                start = idx
            depth += 1
            continue

        if ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                objs.append(text[start: idx + 1])
                start = -1

    return objs


def _cleanup_json_candidate(s: str) -> str:
    # Remove trailing commas before closing braces/brackets.
    s = _re.sub(r",\s*([}\]])", r"\1", s)
    # Normalize common smart quotes that occasionally leak from LLM outputs.
    s = s.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    return s


def extract_json_from_llm(raw: str) -> dict:
    """Extract a JSON object from an LLM response.

    Handles markdown fences, leading/trailing text, and minor formatting noise.
    """
    raw = (raw or "").strip()
    if not raw:
        raise json.JSONDecodeError("empty response", raw, 0)

    candidates: list[str] = []

    # 1) fenced code blocks first
    fence_blocks = _re.findall(r"```(?:json)?\s*([\s\S]*?)\s*```", raw, flags=_re.IGNORECASE)
    for block in fence_blocks:
        b = block.strip()
        if b:
            candidates.append(b)

    # 2) whole text
    candidates.append(raw)

    # 3) balanced json object slices
    candidates.extend(_iter_balanced_json_objects(raw))

    last_err: Exception | None = None
    seen: set[str] = set()
    for cand in candidates:
        c = cand.strip()
        if not c or c in seen:
            continue
        seen.add(c)

        for variant in (c, _cleanup_json_candidate(c)):
            try:
                data = json.loads(variant)
                if isinstance(data, dict):
                    return data
            except Exception as e:
                last_err = e
                continue

    if isinstance(last_err, Exception):
        raise last_err
    return json.loads(raw)


def keep_dataclass_fields(payload: dict, cls: type) -> dict:
    """Return payload filtered by dataclass field names of cls."""
    if not isinstance(payload, dict):
        return {}
    allowed = {f.name for f in fields(cls)}
    return {k: v for k, v in payload.items() if k in allowed}


def keep_dataclass_fields_list(items: list, cls: type) -> list[dict]:
    """Filter each dict item in list by dataclass field names of cls."""
    if not isinstance(items, list):
        return []
    cleaned: list[dict] = []
    for item in items:
        if isinstance(item, dict):
            cleaned.append(keep_dataclass_fields(item, cls))
    return cleaned


def snippet_exists(existing: str, snippet: str) -> bool:
    """Check if the meaningful lines of *snippet* are already in *existing*."""
    for line in snippet.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith(("//", "/*", "#", ";")):
            return stripped in existing
    return False


def snapshot_file(apply_dir: Path, src_file: Path) -> None:
    """Save a copy of *src_file* into ``apply_dir/snapshot/`` for audit."""
    try:
        snap_dir = apply_dir / "snapshot"
        parts = src_file.parts
        rel = Path(*parts[-3:]) if len(parts) >= 3 else Path(src_file.name)
        snap = snap_dir / rel
        ensure_dir(snap.parent)
        write_text(snap, src_file.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# RVV code validity check
# ---------------------------------------------------------------------------

_RVV_INSTRUCTION_RE = _re.compile(
    r"\b(vset[iv]?vli?|vle?\d+|vse?\d+|vadd|vsub|vmul|vdiv|vfadd|vfsub|vfmul|vfdiv|"
    r"vfneg|vand|vor|vxor|vsll|vsrl|vsra|vmerge|vmv|vlse?\d+|vsse?\d+|"
    r"vslide|vredsum|vredmax|vredmin|vfredosum|vfwredosum|vmacc|vnmsac|"
    r"vfmacc|vfnmacc|vfmsac|vfnmsac|vwmul|vwmacc|vzext|vsext|vncvt|"
    r"vnsrl|vnsra|vsetvli|vsetivli)\b",
    _re.IGNORECASE,
)


def has_real_rvv_instructions(generate_plan: dict) -> bool:
    """Check if a generate_plan contains actual RVV vector instructions.

    Returns False if the generated code is just a placeholder (e.g. only 'ret').
    """
    for item in generate_plan.get("generated", []):
        content = item.get("content", "")
        target = item.get("target_path", "")
        if not target.endswith(".S"):
            continue
        if _RVV_INSTRUCTION_RE.search(content):
            return True
    return False
