from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re

from .agent.analyze import (
    analyze_with_llm,
    collect_arch_simd_experience,
    collect_riscv_simd_experience,
    discover_functions,
)
from .agent.search import (
    Discovery,
    Match,
    build_context_from_files,
    enrich_repo_analyze_note_if_needed,
    scan_riscv_implementation_index,
    select_references,
)
from .core.config import AppConfig
from .core.task import DiscoveredFunction, MigrationTarget
from .core.util import ensure_dir, now_id, slug, write_json


@dataclass
class RepoAnalyzeResult:
    run_dir: Path
    output_path: Path
    symbol_count: int
    function_count: int


def _discover_riscv_modules(ffmpeg_root: Path) -> list[str]:
    return sorted(scan_riscv_implementation_index(ffmpeg_root).keys())


def _selected_files_from_json(selected: dict) -> list[str]:
    selected_files: list[str] = []

    def _extend(key: str) -> None:
        v = selected.get(key, [])
        if isinstance(v, list):
            selected_files.extend(str(x) for x in v)

    for k in ("c", "x86", "arm", "riscv", "headers", "makefiles", "checkasm"):
        _extend(k)
    _extend("existing_rvv")

    # Keep order while removing duplicates.
    seen: set[str] = set()
    deduped: list[str] = []
    for f in selected_files:
        if f in seen:
            continue
        seen.add(f)
        deduped.append(f)
    return deduped


def _build_discovery_from_selected_json(symbol: str, selected_json: dict) -> Discovery:
    raw = selected_json.get("_discovery", {}) if isinstance(selected_json, dict) else {}
    matches_raw = raw.get("matches", []) if isinstance(raw, dict) else []
    matches: list[Match] = []
    for item in matches_raw:
        if not isinstance(item, dict):
            continue
        try:
            matches.append(
                Match(
                    file=str(item.get("file", "")),
                    line=int(item.get("line", 0)),
                    text=str(item.get("text", "")),
                )
            )
        except Exception:
            continue
    return Discovery(symbol=symbol, matches=matches)


def _extract_repository_constraints(ffmpeg_root: Path, existing_rvv_files: list[str]) -> dict:
    include_counter: dict[str, int] = {}
    directive_counter: dict[str, int] = {}

    include_re = re.compile(r"#\s*include\s*[<\"]([^>\"]+)[>\"]")
    directive_re = re.compile(r"^\s*(\.[A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)

    for rel in existing_rvv_files:
        p = ffmpeg_root / rel
        if not p.exists() or not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for m in include_re.findall(text):
            include_counter[m] = include_counter.get(m, 0) + 1
        for d in directive_re.findall(text):
            directive_counter[d] = directive_counter.get(d, 0) + 1

    required_includes = [
        k for k, _ in sorted(include_counter.items(), key=lambda kv: (-kv[1], kv[0]))[:8]
    ]
    if "libavutil/riscv/rvv_asm.h" not in required_includes:
        required_includes.append("libavutil/riscv/rvv_asm.h")
    if "config.h" not in required_includes:
        required_includes.append("config.h")

    required_directives = [
        k for k, _ in sorted(directive_counter.items(), key=lambda kv: (-kv[1], kv[0]))
        if k in {".text", ".globl", ".global", ".type", ".size", ".align"}
    ]
    if ".text" not in required_directives:
        required_directives.append(".text")
    if ".globl" not in required_directives and ".global" not in required_directives:
        required_directives.append(".globl")

    return {
        "required_includes": required_includes,
        "required_directives": required_directives,
    }


def _system_analyze_summary(symbol: str, analysis_json: dict, per_func: dict, existing_rvv_count: int) -> str:
    datatype = analysis_json.get("datatype", "unknown") if isinstance(analysis_json, dict) else "unknown"
    vectorizable = bool(analysis_json.get("vectorizable", False)) if isinstance(analysis_json, dict) else False
    migratable = [name for name, v in per_func.items() if int(v.get("migrate", 0)) == 1]
    return (
        f"symbol={symbol}; datatype={datatype}; vectorizable={vectorizable}; "
        f"existing_rvv_files={existing_rvv_count}; migratable_functions={len(migratable)}"
    )


def _analyze_one_symbol(cfg: AppConfig, ffmpeg_root: Path, symbol: str) -> dict:
    module = symbol.split(".")[0] if "." in symbol else symbol
    target = MigrationTarget(module=module, symbol=symbol)

    file_search = select_references(cfg, ffmpeg_root, symbol)
    selected_json = file_search.selected_json if isinstance(file_search.selected_json, dict) else {}
    selected_files = _selected_files_from_json(selected_json)

    code_context = build_context_from_files(
        ffmpeg_root,
        symbol=symbol,
        files=selected_files,
    )

    discovered = discover_functions(cfg, code_context, target)
    functions = discovered.functions or [DiscoveredFunction(name=symbol, role="core")]

    discovery = _build_discovery_from_selected_json(symbol, selected_json)
    analyzed = analyze_with_llm(
        cfg,
        discovery,
        functions=functions,
        context_override=code_context,
    )

    per_func = {}
    for fname, fobj in analyzed.per_function_analysis.items():
        per_func[fname] = {
            "datatype": fobj.datatype,
            "vectorizable": fobj.vectorizable,
            "pattern": fobj.pattern,
            "math_expression": fobj.math_expression,
            "migrate": fobj.migrate,
            "migrate_reason": fobj.migrate_reason,
            "x86_refs": fobj.x86_refs,
            "arm_refs": fobj.arm_refs,
            "notes": fobj.notes,
        }

    migratable_functions = [
        fname for fname, meta in per_func.items() if int(meta.get("migrate", 0)) == 1
    ]

    existing_rvv = selected_json.get("existing_rvv", []) if isinstance(selected_json.get("existing_rvv"), list) else []
    repository_constraints = _extract_repository_constraints(ffmpeg_root, [str(x) for x in existing_rvv])

    riscv_simd_experience = collect_riscv_simd_experience(
        [str(x) for x in existing_rvv],
        analyzed.per_function_analysis,
    )
    arch_simd_experience = collect_arch_simd_experience(analyzed.per_function_analysis)

    return {
        "symbol": symbol,
        "module": module,
        "migratable_functions": migratable_functions,
        "semantic_analysis": {
            "analysis_json": analyzed.analysis_json,
            "per_function": per_func,
            "arch_simd_experience": arch_simd_experience,
        },
        "repository_constraints": repository_constraints,
        "riscv_simd_experience": riscv_simd_experience,
        "references": {
            "selected_files": selected_files,
            "existing_rvv": existing_rvv,
        },
        "system_analyze": _system_analyze_summary(
            symbol,
            analyzed.analysis_json,
            per_func,
            existing_rvv_count=len(existing_rvv),
        ),
    }


def run_repo_analyze(
    cfg: AppConfig,
    *,
    ffmpeg_root: Path,
    symbol: str,
    all_riscv: bool,
    output_path: Path,
) -> RepoAnalyzeResult:
    task_id = now_id()
    run_dir = Path("runs") / f"{task_id}_{slug(symbol or 'repo_analyze')}"
    ensure_dir(run_dir)

    symbols: list[str]
    if all_riscv:
        symbols = _discover_riscv_modules(ffmpeg_root)
    else:
        symbols = [symbol]

    symbols = [s for s in symbols if str(s).strip()]
    results: list[dict] = []

    for sym in symbols:
        print(f"[analyze] processing: {sym}")
        try:
            results.append(_analyze_one_symbol(cfg, ffmpeg_root, sym))
        except Exception as e:
            results.append(
                {
                    "symbol": sym,
                    "module": sym.split(".")[0] if "." in sym else sym,
                    "error": str(e),
                }
            )

    function_count = 0
    for item in results:
        mf = item.get("migratable_functions", []) if isinstance(item, dict) else []
        if isinstance(mf, list):
            function_count += len(mf)

    payload = {
        "schema_version": "1.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_ffmpeg_root": str(ffmpeg_root),
        "symbol_count": len(symbols),
        "function_count": function_count,
        "entries": results,
        "note": "",
    }

    payload = enrich_repo_analyze_note_if_needed(cfg, ffmpeg_root, payload)

    # Keep one copy in run_dir as an artifact and one in requested output path.
    artifact_path = run_dir / "repo_analyze.json"
    write_json(artifact_path, payload)
    write_json(output_path, payload)

    return RepoAnalyzeResult(
        run_dir=run_dir,
        output_path=output_path,
        symbol_count=len(symbols),
        function_count=function_count,
    )
