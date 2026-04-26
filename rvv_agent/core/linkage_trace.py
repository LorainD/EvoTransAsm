from __future__ import annotations

from pathlib import Path


FFMPEG_LIB_ROOTS = [
    "libavcodec",
    "libavfilter",
    "libavformat",
    "libavutil",
    "libswscale",
    "libswresample",
    "libpostproc",
]

ARCH_HINTS = ["x86", "arm", "aarch64", "loongarch", "mips", "ppc", "riscv"]


def extract_relevant_lines(text: str, keywords: list[str], window: int = 30) -> str:
    lines = text.splitlines()
    if not lines:
        return ""

    hit_lines: set[int] = set()
    for i, line in enumerate(lines):
        if any(k and k in line for k in keywords):
            start = max(0, i - window)
            end = min(len(lines), i + window + 1)
            for idx in range(start, end):
                hit_lines.add(idx)

    if not hit_lines:
        return ""

    ordered = sorted(hit_lines)
    return "\n".join(f"{i + 1}: {lines[i]}" for i in ordered)


def _score_file(rel: str, module: str, symbol: str) -> tuple[int, int, int, int]:
    low = rel.lower()
    mod = module.lower().strip()
    sym = symbol.lower().strip()

    # Higher score first:
    # 1) architecture leaves/init, 2) make/build, 3) module/symbol affinity.
    arch_score = 2 if any(f"/{a}/" in low for a in ARCH_HINTS) else 0
    init_score = 1 if "_init" in low or low.endswith("dsp_init.c") else 0
    build_score = 2 if low.endswith("/makefile") or low.endswith("/meson.build") else 0
    mod_score = 1 if mod and mod in low else 0
    sym_score = 1 if sym and sym.replace(".", "_") in low else 0

    return (arch_score + init_score, build_score, mod_score, sym_score)


def _is_candidate_path(rel: str, module: str) -> bool:
    low = rel.lower()
    mod = module.lower().strip()

    if low.endswith((".s", ".asm", ".c", ".h", "makefile", "meson.build")):
        pass
    else:
        return False

    if any(f"/{a}/" in low for a in ARCH_HINTS):
        return True
    if "_init" in low or low.endswith("dsp_init.c"):
        return True
    if low.endswith("/makefile") or low.endswith("/meson.build"):
        return True
    if mod and mod in low:
        return True
    return False


def collect_linkage_evidence(
    ffmpeg_root: Path,
    module: str,
    symbol: str,
    max_files: int = 40,
) -> str:
    """Collect linkage evidence across FFmpeg roots; no inference is performed."""
    module = str(module or "").strip()
    symbol = str(symbol or "").strip()
    max_files = max(1, int(max_files or 40))

    keywords = [
        symbol,
        symbol.replace(".", "_") if symbol else "",
        module,
        f"{module}_init" if module else "",
        f"ff_{module}_init" if module else "",
        "_init_riscv",
        "_init_x86",
        "_init_arm",
        "_init_aarch64",
        "ARCH_RISCV",
        "AV_CPU_FLAG_RVV",
        "OBJS-$(CONFIG_",
    ]
    keywords = [k for k in keywords if k]

    files: list[Path] = []
    for lib_root in FFMPEG_LIB_ROOTS:
        root = ffmpeg_root / lib_root
        if not root.exists() or not root.is_dir():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            try:
                rel = p.relative_to(ffmpeg_root).as_posix()
            except Exception:
                continue
            if _is_candidate_path(rel, module):
                files.append(p)

    # Prioritize likely linkage files but keep coverage broad.
    files = sorted(
        files,
        key=lambda p: _score_file(p.relative_to(ffmpeg_root).as_posix(), module, symbol),
        reverse=True,
    )

    chunks: list[str] = []
    used = 0
    for path in files:
        if used >= max_files:
            break
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        if keywords and not any(k in text for k in keywords):
            continue

        excerpt = extract_relevant_lines(text, keywords, window=30)
        if not excerpt.strip():
            continue

        rel = path.relative_to(ffmpeg_root).as_posix()
        chunks.append(f"### FILE: {rel}\n{excerpt}")
        used += 1

    return "\n\n".join(chunks)
