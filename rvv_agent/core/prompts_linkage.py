from __future__ import annotations


def linkage_trace_prompt(
    module: str,
    symbol: str,
    evidence: str,
    build_errors: str = "",
) -> str:
    return f"""
You are reconstructing FFmpeg architecture linkage.

Do not generate code.
Do not assume the library is libavcodec.
Infer the library root from evidence.

Target module: {module}
Target symbol: {symbol}

Evidence:
```text
{evidence}
```

Build errors:
```text
{build_errors}
```

Task:
Starting from the lowest-level optimized implementation in x86/arm/aarch64/etc.,
trace upward:

Find the leaf SIMD implementation symbol.
Find where that symbol is declared in an architecture init file.
Find where it is assigned to a function pointer or dispatch table.
Find the architecture init function that owns this assignment.
Find where that architecture init function is declared.
Find where the public/common code calls architecture init.
Find the Makefile rule that builds both init object and implementation object.
Infer the corresponding RISC-V files and symbols.

Output only JSON.

JSON shape:
{{
  "library_root": "libavcodec | libswscale | libavfilter | libavutil | libswresample | ...",
  "arch": "riscv",

  "module": "...",
  "target_symbol": "...",

  "leaf_reference_files": ["..."],
  "arch_init_reference_files": ["..."],
  "public_dispatch_files": ["..."],
  "makefile_files": ["..."],

  "riscv_init_file": "...",
  "riscv_impl_file": "...",
  "riscv_makefile": "...",

  "init_function": "...",
  "init_signature": "...",

  "binding_field": "...",
  "rvv_symbol": "...",
  "rvv_signature": "...",

  "required_objects": ["..."],
  "allowed_files": ["..."],

  "evidence_chain": [
    {{
      "step": "leaf_impl | arch_init | public_dispatch | makefile | riscv_target",
      "file": "...",
      "evidence": "short exact evidence"
    }}
  ],

  "confidence": "high | medium | low",
  "blocked_reason": ""
}}

Rules:
- Do not hardcode libavcodec.
- Do not invent init signatures.
- If FFmpeg uses include-based architecture dispatch, record the include file and the included architecture entry.
- If evidence is insufficient, set confidence to low.
- allowed_files must be minimal and evidence-backed.
"""


def linkage_plan_prompt(module: str, symbol: str, linkage_context: str, build_errors: str = "") -> str:
    """Backward-compatible alias for older callers."""
    return linkage_trace_prompt(module, symbol, linkage_context, build_errors)
