from __future__ import annotations

import re
from pathlib import Path


def validate_patch_scope(patch_items: list[dict], plan: dict) -> list[str]:
    allowed = set(str(x) for x in plan.get("allowed_files", []) if str(x).strip())
    if not allowed:
        return []

    errors: list[str] = []
    for item in patch_items:
        path = str(item.get("target_path") or item.get("path") or "").strip()
        if not path:
            errors.append("patch item missing target_path")
            continue
        if path not in allowed:
            errors.append(f"file not allowed by linkage_plan: {path}")
    return errors


def normalize_decl(s: str) -> str:
    s = s.strip().rstrip(";")
    s = " ".join(s.replace("\n", " ").split())
    return s + ";"


def validate_init_signature(ffmpeg_root: Path, plan: dict) -> list[str]:
    init_file = str(plan.get("riscv_init_file", "")).strip()
    init_func = str(plan.get("init_function", "")).strip()
    expected = str(plan.get("init_signature", "")).strip()

    if not init_file or not init_func or not expected:
        return []

    path = ffmpeg_root / init_file
    if not path.exists():
        return [f"missing init file: {init_file}"]

    text = path.read_text(encoding="utf-8", errors="replace")
    expected_norm = normalize_decl(expected)

    pattern = rf"(?:av_cold\s+)?void\s+{re.escape(init_func)}\s*\([^{{]+\)"
    m = re.search(pattern, text, re.S)
    if not m:
        return [f"missing init function implementation: {init_func}"]

    actual_norm = normalize_decl(m.group(0))
    if expected_norm != actual_norm:
        return [f"init signature mismatch: expected `{expected_norm}`, got `{actual_norm}`"]
    return []


def validate_rvv_symbol_export(ffmpeg_root: Path, plan: dict) -> list[str]:
    asm_file = str(plan.get("riscv_impl_file", "") or plan.get("riscv_asm_file", "")).strip()
    sym = str(plan.get("rvv_symbol", "")).strip()

    if not asm_file or not sym:
        return []

    path = ffmpeg_root / asm_file
    if not path.exists():
        return [f"missing asm file: {asm_file}"]

    text = path.read_text(encoding="utf-8", errors="replace")
    if re.search(rf"\bfunc\s+{re.escape(sym)}\b", text):
        return []
    if re.search(rf"^\s*{re.escape(sym)}:", text, re.M):
        return []

    return [f"rvv symbol not exported: {sym}"]


def validate_makefile_objects(ffmpeg_root: Path, plan: dict) -> list[str]:
    makefile = str(plan.get("riscv_makefile", "")).strip()
    required = plan.get("required_objects", [])

    if not makefile or not isinstance(required, list) or not required:
        return []

    path = ffmpeg_root / makefile
    if not path.exists():
        return [f"missing makefile: {makefile}"]

    text = path.read_text(encoding="utf-8", errors="replace")
    errors: list[str] = []
    for obj in required:
        obj = str(obj).strip()
        if not obj:
            continue
        base = obj.split("/")[-1]
        if obj not in text and base not in text:
            errors.append(f"missing Makefile object: {obj}")
    return errors


def validate_linkage_after_patch(ffmpeg_root: Path, plan: dict) -> list[str]:
    errors: list[str] = []
    errors += validate_init_signature(ffmpeg_root, plan)
    errors += validate_rvv_symbol_export(ffmpeg_root, plan)
    errors += validate_makefile_objects(ffmpeg_root, plan)
    return errors
