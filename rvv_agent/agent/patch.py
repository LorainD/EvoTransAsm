"""agent.patch — 2-step PATCH stage for the state-machine pipeline.

PATCH now follows a minimal closed loop:
  1. generate_code  — LLM produces concrete file actions
  2. apply_patch    — tool applies actions to repo and validates outcome
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict
from pathlib import Path

from ..core.config import AppConfig
from ..core.llm import (
    LlmError,
    LlmMessage,
    ToolSpec,
    chat_completion_with_retry,
    record_trajectory_action,
    run_tool_use_loop,
)
from ..core.prompts import system_prompt
from ..core.prompts_patch import (
    debug_classify_prompt,
    patch_generate_prompt,
)
from ..core.task import (
    PatchArtifact,
    TaskContext,
    TaskState,
    load_plan_artifact,
)
from ..core.util import ensure_dir, now_id, write_json, write_text, extract_build_errors, fmt_argv
from ..tool.interactive import prompt_yes_no
from ..tool.exec import run_configure, run_make_checkasm, configure_argv, make_checkasm_argv


def _current_group_id(task: TaskContext) -> str:
    """Best-effort current group id from PLAN artifact."""
    try:
        plan = load_plan_artifact(task.load_artifact("PLAN"))
    except Exception:
        return ""
    idx = int(plan.current_group_idx)
    if idx < 0 or idx >= len(plan.groups):
        return ""
    return plan.groups[idx].group_id or ""

# ---------------------------------------------------------------------------
# Shared helpers (re-exported from generate.py / inject.py)
# ---------------------------------------------------------------------------

from ..core.util import extract_json_from_llm, snippet_exists, snapshot_file

# Aliases for backward compat within this module
_extract_gen_json = extract_json_from_llm
_snippet_already_present = snippet_exists
_snapshot = snapshot_file

# Keep PATCH self-healing bounded when BUILD has not started yet.
_MAX_PREBUILD_PATCH_RETRIES = 3
_PATCH_HARNESS_CACHE: str | None = None


def _load_patch_harness_text() -> str:
    """Load full patch_harness.md text without truncation for PATCH prompts."""
    global _PATCH_HARNESS_CACHE
    if _PATCH_HARNESS_CACHE is not None:
        return _PATCH_HARNESS_CACHE

    harness_path = Path(__file__).resolve().parents[2] / "patch_harness.md"

def rollback_group_applies(task: TaskContext, group_id: str) -> int:
    """Rollback apply snapshots that belong to a specific group.

    For historical PATCH artifacts without group_id, fallback to rolling back the
    most recent apply once to reduce cross-group blast radius.
    """
    if not group_id:
        return 0

    sub_ids: list[str] = []
    for pid in task.artifacts.patch_ids:
        sub = pid.split("/", 1)[1] if "/" in pid else pid
        if sub:
            sub_ids.append(sub)

    if not sub_ids:
        return 0

    restored = 0
    has_legacy = False
    for sub in reversed(sub_ids):
        try:
            patch_art = task.load_artifact("PATCH", sub_id=sub)
        except Exception:
            continue
        patch_group = str(patch_art.get("group_id", "") or "")
        patch_id = str(patch_art.get("patch_id", "") or "")
        if not patch_id:
            continue
        if not patch_group:
            has_legacy = True
            continue
        if patch_group != group_id:
            continue

        pre_dir = task.run_dir / f"apply_{patch_id}" / "pre_injection"
        if pre_dir.exists():
            restored += _rollback_apply_dir(pre_dir, task.ffmpeg_root)

    if restored:
        print(f"[PATCH] 已按 group 回滚 {restored} 个文件 (group_id={group_id})")
        return restored

    if has_legacy:
        print("[PATCH][WARN] 检测到旧版 PATCH artifact 缺少 group_id，降级为回滚最近一次 apply")
        _rollback_previous_apply(task)
    return restored
    if not harness_path.exists():
        raise FileNotFoundError(f"patch harness not found: {harness_path}")

    text = harness_path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        raise RuntimeError(f"patch harness is empty: {harness_path}")

    _PATCH_HARNESS_CACHE = text
    return _PATCH_HARNESS_CACHE


def _patch_harness_system_message() -> LlmMessage:
    """Build a strict system message carrying full patch harness content."""
    text = _load_patch_harness_text()
    return LlmMessage(
        role="system",
        content=(
            "以下是 PATCH 阶段必须完整遵循的 patch_harness.md 全文。"
            "不得省略、不得摘要、不得忽略其中约束。\n\n"
            + text
        ),
    )


def _save_pre_injection(apply_dir: Path, ffmpeg_root: Path, dst: Path) -> None:
    """Save the ORIGINAL content of dst before injection (for rollback)."""
    try:
        pre_dir = apply_dir / "pre_injection"
        try:
            rel = dst.resolve().relative_to(ffmpeg_root.resolve())
        except Exception:
            rel = Path(dst.name)
        pre = pre_dir / rel
        ensure_dir(pre.parent)
        if dst.exists():
            write_text(pre, dst.read_text(encoding="utf-8", errors="replace"))
        else:
            # Mark as "did not exist" so rollback can delete it
            write_text(pre, "")
            (pre.parent / (pre.name + ".__new__")).touch()
    except Exception:
        pass


def _rollback_apply_dir(pre_dir: Path, ffmpeg_root: Path) -> int:
    """Restore files from a single pre_injection dir. Returns count of restored files."""
    restored = 0
    for pre_file in pre_dir.rglob("*"):
        if not pre_file.is_file():
            continue
        if pre_file.name.endswith(".__new__"):
            continue
        rel = pre_file.relative_to(pre_dir)
        marker = pre_file.parent / (pre_file.name + ".__new__")
        dst = ffmpeg_root / rel
        if marker.exists():
            if dst.exists():
                dst.unlink()
                restored += 1
        else:
            original = pre_file.read_text(encoding="utf-8", errors="replace")
            ensure_dir(dst.parent)
            write_text(dst, original)
            restored += 1
    return restored


def _rollback_previous_apply(task: TaskContext) -> None:
    """Restore files modified by the most recent apply to their pre-injection state."""
    if not task.artifacts.patch_ids:
        return
    try:
        sub = task.artifacts.patch_ids[-1].split("/")[-1]
        prev_patch = task.load_artifact("PATCH", sub_id=sub)
        patch_id = prev_patch.get("patch_id", "")
    except Exception:
        return
    if not patch_id:
        return

    pre_dir = task.run_dir / f"apply_{patch_id}" / "pre_injection"
    if not pre_dir.exists():
        return

    restored = _rollback_apply_dir(pre_dir, task.ffmpeg_root)
    if restored:
        print(f"[PATCH] 已回滚 {restored} 个文件到注入前状态")


def rollback_all_applies(task: TaskContext) -> int:
    """Restore ALL files modified during this session to their pre-injection state.

    Called on final session failure to ensure ffmpeg workspace remains compilable.
    Iterates all apply_<id> directories in run_dir, applying each pre_injection
    snapshot in reverse order so the earliest state is restored last (wins).
    """
    # Collect all apply dirs, sorted newest-first so earlier originals win
    apply_dirs = sorted(
        task.run_dir.glob("apply_*/pre_injection"),
        key=lambda p: p.parent.name,
        reverse=True,
    )
    if not apply_dirs:
        return 0

    total = 0
    for pre_dir in apply_dirs:
        total += _rollback_apply_dir(pre_dir, task.ffmpeg_root)

    if total:
        print(f"[PATCH] session 失败，已将 ffmpeg 工作区回滚 {total} 个文件到本次侵入前状态")
    return total


# ---------------------------------------------------------------------------
# Tool-use helpers (first batch of tools for PATCH/BUILD/ROLLBACK)
# ---------------------------------------------------------------------------


def _build_patch_tools(task: TaskContext) -> list[ToolSpec]:
    """Construct ToolSpec list for PATCH/BUILD tool-use loop.

    这一批工具专门为后续的 run_patch_with_tools 设计，目前覆盖：
    - read_file: 在 ffmpeg_root 沙箱内读取文件内容；
    - write_patch: 利用 apply_patch 的核心逻辑对单个 patch 进行写入；
    - run_build: 触发一次 configure + make checkasm 构建（与 pipeline BUILD 一致）；
    - rollback_last_apply: 回滚最近一次 apply_ 快照。

    注意：ToolSpec.func 通过闭包捕获 TaskContext，确保所有文件操作
    都限制在 task.ffmpeg_root 下，不越界到工作区之外。
    """

    ffmpeg_root = task.ffmpeg_root

    def _tool_read_file(args: dict) -> dict:
        rel = str(args.get("path", "")).strip()
        max_chars = int(args.get("max_chars", 4000) or 4000)
        if not rel:
            return {"ok": False, "error": "missing path"}
        full = ffmpeg_root / rel
        if not full.exists() or not full.is_file():
            return {"ok": False, "error": "not_found", "exists": False}
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"read_failed: {e}"}
        if len(text) > max_chars:
            text = text[:max_chars] + "\n... (truncated)"
        return {"ok": True, "exists": True, "path": rel, "content": text}

    def _tool_write_patch(args: dict) -> dict:
        """Apply a single patch item using apply_patch semantics."""
        raw_item = {
            "target_path": str(args.get("target_path", "")),
            "action": str(args.get("action", "create") or "create"),
            "content": str(args.get("content", "")),
            "anchor_hint": str(args.get("anchor_hint", "")),
            "description": str(args.get("description", "")),
        }
        plan = {"generate_plan": {"patches": [raw_item]}, "generated": [raw_item]}
        artifact = apply_patch(task, plan)
        return {
            "ok": artifact.success,
            "patch_id": artifact.patch_id,
            "applied_paths": artifact.applied_paths,
            "error": artifact.error,
        }

    def _tool_run_build(args: dict) -> dict:
        build_dir = ffmpeg_root / task.cfg.ffmpeg.build_dir
        ensure_dir(build_dir)
        jobs = int(args.get("jobs") or task.jobs or (os.cpu_count() or 1))
        jobs = max(1, jobs)

        cfg_res = run_configure(task.cfg, ffmpeg_root, build_dir)
        cfg_err = extract_build_errors(cfg_res.stdout + cfg_res.stderr)
        if cfg_res.returncode != 0:
            return {
                "phase": "configure",
                "ok": False,
                "exitcode": cfg_res.returncode,
                "cmd": fmt_argv(configure_argv(task.cfg, ffmpeg_root)),
                "errors": cfg_err,
            }

        make_res = run_make_checkasm(task.cfg, build_dir, jobs)
        make_err = extract_build_errors(make_res.stdout + make_res.stderr)
        return {
            "phase": "make",
            "ok": make_res.returncode == 0,
            "exitcode": make_res.returncode,
            "cmd": fmt_argv(make_checkasm_argv(jobs=jobs)),
            "errors": make_err,
        }

    def _tool_rollback_last_apply(args: dict) -> dict:  # noqa: ARG001
        before_ids = list(task.artifacts.patch_ids)
        _rollback_previous_apply(task)
        after_ids = list(task.artifacts.patch_ids)
        return {
            "ok": True,
            "note": "rollback_previous_apply executed",
            "patch_ids_before": before_ids,
            "patch_ids_after": after_ids,
        }

    return [
        ToolSpec(
            name="read_file",
            description="读取 FFmpeg 工作区内指定相对路径的文件内容",
            parameters={"path": {"type": "string"}, "max_chars": {"type": "integer", "optional": True}},
            func=_tool_read_file,
        ),
        ToolSpec(
            name="write_patch",
            description="将生成的单个 patch 应用到 ffmpeg_root 下（create/append/replace）",
            parameters={
                "target_path": {"type": "string"},
                "action": {"type": "string"},
                "content": {"type": "string"},
                "anchor_hint": {"type": "string", "optional": True},
                "description": {"type": "string", "optional": True},
            },
            func=_tool_write_patch,
        ),
        ToolSpec(
            name="run_build",
            description="在当前 ffmpeg_root 下执行 configure + make checkasm 构建",
            parameters={"jobs": {"type": "integer", "optional": True}},
            func=_tool_run_build,
        ),
        ToolSpec(
            name="rollback_last_apply",
            description="回滚最近一次 apply 对工作区的修改",
            parameters={},
            func=_tool_rollback_last_apply,
        ),
    ]


def _build_group_scoped_analysis(task: TaskContext) -> dict:
    """Get PATCH analysis view scoped to the current group."""
    try:
        from .context_builder import ContextBuilder

        scoped = ContextBuilder(task).build_patch_analysis_context()
        if isinstance(scoped, dict) and scoped:
            return scoped
    except Exception:
        pass

    try:
        analysis = task.load_artifact("ANALYZE")
        if isinstance(analysis, dict):
            az = analysis.get("analysis_json", {})
            if isinstance(az, dict):
                return az
    except Exception:
        pass

    return {}



def _check_has_rvv_block(path: Path) -> bool:
    """Check whether file has #if HAVE_RVV or #if CONFIG_RVV block."""
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    return bool(re.search(r"#\s*if\s+(HAVE_RVV|CONFIG_RVV)", text))


def _check_makefile_has_module(path: Path, module: str) -> bool:
    """Check whether Makefile has OBJS entry for module with _rvv."""
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    return module.upper() in text.upper() and "_rvv" in text.lower()


def _check_source_has_riscv_decl(ffmpeg_root: Path, module: str) -> bool:
    """Check source C files for #if ARCH_RISCV declaration block."""
    candidates = [
        f"libavcodec/{module}dsp_init.c",
        f"libavcodec/{module}.c",
        f"libavcodec/{module}dsp.c",
    ]
    for rel in candidates:
        src = ffmpeg_root / rel
        if not src.exists():
            continue
        text = src.read_text(encoding="utf-8", errors="replace")
        if re.search(r"#\s*if\s+ARCH_RISCV", text):
            return True
    return False


def _build_planning_bundle(task: TaskContext) -> dict:
    """Build a shared context bundle for PATCH generation and retries."""
    file_search = task.load_artifact("SEARCH_FILE")
    if task.artifacts.reference_code_ids:
        sub = task.artifacts.reference_code_ids[-1].split("/", 1)[-1]
        reference = task.load_artifact("BUILD_REFERENCE", sub_id=sub)
    else:
        reference = task.load_artifact("BUILD_REFERENCE")

    analysis_json = _build_group_scoped_analysis(task)

    module = task.target.module
    riscv_dir = task.ffmpeg_root / "libavcodec" / "riscv"
    rvv_path = f"libavcodec/riscv/{module}_rvv.S"
    init_path = f"libavcodec/riscv/{module}_init.c"
    makefile_path = "libavcodec/riscv/Makefile"

    return {
        "analysis_json": analysis_json,
        "selected_files": file_search.get("selected_files", []),
        "code_context": reference.get("code_context", ""),
        "existing_rvv": reference.get("existing_rvv", []),
        "module": module,
        "symbol": task.target.symbol,
        "target_files": {
            "rvv": rvv_path,
            "init": init_path,
            "makefile": makefile_path,
            "rvv_exists": (task.ffmpeg_root / rvv_path).exists(),
            "init_exists": (task.ffmpeg_root / init_path).exists(),
            "makefile_exists": (task.ffmpeg_root / makefile_path).exists(),
            "riscv_dir_exists": riscv_dir.exists(),
            "init_has_rvv_block": _check_has_rvv_block(task.ffmpeg_root / init_path),
            "makefile_has_module": _check_makefile_has_module(task.ffmpeg_root / makefile_path, module),
            "source_has_riscv_decl": _check_source_has_riscv_decl(task.ffmpeg_root, module),
        },
    }


def _load_repository_knowledge_entry(task: TaskContext) -> dict | None:
    """Load optional repository knowledge entry for current symbol/module."""
    candidates = [
        Path("repo_analyze.json"),
        task.run_dir / "repo_analyze.json",
        Path("repository_knowledge.json"),
        task.run_dir / "repository_knowledge.json",
    ]
    data = None
    for p in candidates:
        try:
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                break
        except Exception:
            continue

    if not isinstance(data, dict):
        return None

    # Support single-entry schema directly.
    if "entries" not in data:
        return data

    entries = data.get("entries", [])
    if not isinstance(entries, list):
        return None

    symbol = str(task.target.symbol or "")
    module = str(task.target.module or "")

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("symbol", "")) == symbol:
            return entry
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("module", "")) == module:
            return entry
    return None


def _normalize_generate_plan(raw_plan: dict) -> dict:
    """Normalize LLM output to an internal `generated` list.

    Supports:
    - new schema: {"generate_plan": {"patches": [...]}}
    - old schema: {"generated": [...]} / {"files": [...]}.
    """
    if not isinstance(raw_plan, dict):
        return {"generated": []}

    legacy_action_map = {
        "inject_rvv_block": "append",
        "inject_objs": "append",
        "inject_arch_decl": "append",
    }

    if isinstance(raw_plan.get("generate_plan"), dict):
        patches = raw_plan.get("generate_plan", {}).get("patches", [])
        if isinstance(patches, list):
            generated = []
            for p in patches:
                if not isinstance(p, dict):
                    continue
                action = str(p.get("action", "create") or "create").strip().lower()
                action = legacy_action_map.get(action, action)
                generated.append(
                    {
                        "target_path": str(p.get("target_path", "")),
                        "action": action,
                        "content": str(p.get("content", "")),
                        "anchor_hint": str(p.get("anchor_hint", "")),
                        "description": str(p.get("description", "")),
                    }
                )
            return {"generate_plan": raw_plan.get("generate_plan", {}), "generated": generated}

    if isinstance(raw_plan.get("generated"), list):
        generated = []
        for p in raw_plan.get("generated", []):
            if not isinstance(p, dict):
                continue
            action = str(p.get("action", "create") or "create").strip().lower()
            action = legacy_action_map.get(action, action)
            generated.append(
                {
                    "target_path": str(p.get("target_path", "")),
                    "action": action,
                    "content": str(p.get("content", "")),
                    "anchor_hint": str(p.get("anchor_hint", "")),
                    "description": str(p.get("description", "")),
                }
            )
        return {"generated": generated}

    if "files" in raw_plan:
        return {
            "generated": [
                {
                    "target_path": f.get("path", ""),
                    "action": "create",
                    "content": f.get("content", ""),
                    "anchor_hint": "",
                    "description": "",
                }
                for f in raw_plan.get("files", [])
                if isinstance(f, dict)
            ]
        }

    return {"generated": []}


def _generated_items(plan: dict) -> list[dict]:
    return plan.get("generated", []) if isinstance(plan.get("generated", []), list) else []


def _infer_generated_roles(generated: list[dict]) -> set[str]:
    """Infer generated roles from target path/description/content."""
    roles: set[str] = set()
    for item in generated:
        path = str(item.get("target_path", "")).lower()
        desc = str(item.get("description", "")).lower()
        content = str(item.get("content", "")).lower()
        role = str(item.get("role", "")).lower()

        if role in {"impl", "register", "build", "header", "arch_glue"}:
            roles.add(role)

        if path.endswith((".s", ".asm")):
            roles.add("impl")

        # Support common FFmpeg registration filenames like *_init_riscv.c.
        if (
            path.endswith("_init.c")
            or path.endswith("_init_riscv.c")
            or (path.endswith(".c") and "init" in path and "riscv" in path)
            or "register" in desc
            or "av_cpu_flag_rvv" in content
            or ("= ff_" in content and "_rvv" in content)
        ):
            roles.add("register")

        if path.endswith("makefile") or "objs-$(" in content:
            roles.add("build")
        if path.endswith(".h"):
            roles.add("header")
    return roles


def _validate_generate_plan(gen_plan: dict, target_files: dict) -> tuple[bool, list[str]]:
    """Validate generated plan with repository constraint checks."""
    issues: list[str] = []
    generated = _generated_items(gen_plan)
    if not generated:
        return False, ["generate_plan contains no patches"]

    analysis_json = target_files if isinstance(target_files, dict) else {}
    constraints = analysis_json.get("repository_constraints", {})
    if not isinstance(constraints, dict):
        constraints = {}

    required_includes = [str(x) for x in constraints.get("required_includes", []) if str(x).strip()]
    required_directives = [str(x) for x in constraints.get("required_directives", []) if str(x).strip()]

    for patch in generated:
        path = str(patch.get("target_path", "")).lower()
        content = str(patch.get("content", ""))

        action = str(patch.get("action", "")).lower().strip()
        if action and action not in {"create", "append", "replace"}:
            issues.append(f"unsupported_action:{action}")

        if path.endswith((".s", ".asm")):
            for req in required_includes:
                if req not in content:
                    issues.append(f"Missing required include in ASM: {req}")
            for req in required_directives:
                if req not in content:
                    issues.append(f"Missing required directive in ASM: {req}")
        elif path.endswith(".c"):
            if "{" not in content or "}" not in content:
                issues.append(f"C file appears structurally invalid (missing braces): {path}")

    return (len(issues) == 0, issues)


def _extract_expected_symbols(generate_plan: dict) -> set[str]:
    """Extract concrete RVV function symbols expected after apply.

    Only track explicit RVV function tokens to avoid false positives like
    generic group labels (e.g. pred8x8) from high-level symbols.
    """
    syms: set[str] = set()
    for item in _generated_items(generate_plan):
        text = str(item.get("content", ""))
        for m in re.findall(r"\bff_[A-Za-z0-9_]+_rvv\b", text):
            syms.add(m)
        for m in re.findall(r"\.globl\s+([A-Za-z0-9_]+)", text):
            if m.endswith("_rvv"):
                syms.add(m)
    return syms


def _contains_symbol_like(content: str, symbol: str) -> bool:
    """Check whether a symbol appears as plain token or prefixed ff_* token."""
    if not symbol:
        return False
    pats = [
        rf"\b{re.escape(symbol)}\b",
        rf"\bff_{re.escape(symbol)}(_rvv)?\b",
    ]
    return any(re.search(p, content) for p in pats)


def _route_apply_failure_with_llm(
    task: TaskContext,
    artifact: PatchArtifact,
) -> tuple[TaskState, str]:
    """Route apply failures via LLM suggestion, with safe fallbacks."""
    error_text = artifact.error or "patch_apply_failed"
    current_patch = {
        "func": task.target.symbol,
        "design": {},
        "generate_plan": artifact.generate_plan,
        "error": artifact.error,
    }

    if task.cfg is None:
        return TaskState.DEBUG, "no_cfg_fallback_debug"

    try:
        from .context_builder import ContextBuilder, DebugContext

        ctx_builder = ContextBuilder(task, kb=None)
        debug_ctx = DebugContext(
            error_text=error_text,
            current_patch=current_patch,
        )

        messages = [
            LlmMessage(role="system", content=system_prompt()),
            _patch_harness_system_message(),
            LlmMessage(role="user", content=debug_classify_prompt(debug_ctx)),
        ]
        raw = chat_completion_with_retry(
            task.cfg.llm,
            messages,
            max_tokens=600,
            stage="patch_apply_route",
            max_retries=2,
        ).strip()
        s_pos = raw.find("{")
        e_pos = raw.rfind("}")
        data = json.loads(raw[s_pos:e_pos + 1]) if s_pos != -1 and e_pos > s_pos else json.loads(raw)

        target = str(data.get("rollback_target", "")).strip().lower()
        suggestion = str(data.get("suggestion", "")).strip()
        if target in {"generate"}:
            task.rollback_hint = target
            reason = f"llm_route_patch:{target}"
            if suggestion:
                reason += f"; suggestion={suggestion[:160]}"
            return TaskState.PATCH, reason

        reason = "llm_route_debug"
        if suggestion:
            reason += f"; suggestion={suggestion[:160]}"
        return TaskState.DEBUG, reason
    except Exception as e:
        return TaskState.DEBUG, f"route_exception_debug:{e}"
# ---------------------------------------------------------------------------
# Step 1: Generate code
# ---------------------------------------------------------------------------


def generate_code(task: TaskContext,
                   kb_errors: list[dict] | None = None,
                   planning_bundle: dict | None = None,
                   validation_feedback: list[str] | None = None) -> dict:
    """LLM generates actual code based on the design. Returns generate_plan dict.

    On retry (after DEBUG), includes build errors, debug suggestions, and the
    previous failing code in the prompt so the LLM can produce a targeted fix.
    """
    bundle = planning_bundle or _build_planning_bundle(task)
    analysis_json = bundle.get("analysis_json", {})
    selected_files = bundle.get("selected_files", [])
    existing_rvv = bundle.get("existing_rvv", [])
    repository_knowledge_entry = _load_repository_knowledge_entry(task)

    # Build existing_files_map for incremental merge
    # Include .S files so LLM can see existing RVV implementations
    existing_map: dict[str, str] = {}
    for rel in selected_files:
        full = task.ffmpeg_root / rel
        if full.exists() and full.is_file():
            try:
                content = full.read_text(encoding="utf-8", errors="replace")
                # For large .S files, only include relevant portions
                if rel.endswith(".S") and len(content) > 6000:
                    content = content[:6000] + "\n... (truncated)"
                existing_map[rel] = content
            except Exception:
                pass

    # Also include existing RVV files from retrieval
    for rel in existing_rvv:
        if rel not in existing_map:
            full = task.ffmpeg_root / rel
            if full.exists() and full.is_file():
                try:
                    content = full.read_text(encoding="utf-8", errors="replace")
                    if len(content) > 6000:
                        content = content[:6000] + "\n... (truncated)"
                    existing_map[rel] = content
                except Exception:
                    pass

    # Collect retry context from previous DEBUG cycles
    build_errors_text: str | None = None
    debug_suggestions: list[str] | None = None
    previous_code: dict | None = None
#TODO：如果generate和debug是耦合的，那么是否加入了知识库中的错误经验？
    if task.all_build_errors:
        build_errors_text = "\n---\n".join(task.all_build_errors[-2:])  # last 2 errors

    if task.artifacts.debug_run_ids:
        try:
            latest_debug = task.load_artifact(
                "DEBUG", sub_id=task.artifacts.debug_run_ids[-1]
            )
            debug_suggestions = latest_debug.get("fix_actions", [])
            llm_sug = latest_debug.get("llm_suggestion", "")
            if llm_sug:
                debug_suggestions = (debug_suggestions or []) + [llm_sug]
        except Exception:
            pass

    if task.artifacts.patch_ids:
        try:
            sub = task.artifacts.patch_ids[-1].split("/")[-1]
            prev_patch = task.load_artifact("PATCH", sub_id=sub)
            previous_code = prev_patch.get("generate_plan")
        except Exception:
            pass

    # Build PATCH context using ContextBuilder
    from .context_builder import ContextBuilder, PatchContext

    ctx_builder = ContextBuilder(task, kb=None)  # KB will be loaded in build_patch_context if needed
    patch_ctx = PatchContext(
        symbol=task.target.symbol,
        analysis_json=analysis_json,
        target_files=bundle.get("target_files", {}),
        repository_knowledge_entry=repository_knowledge_entry,
        existing_files_map=existing_map or None,
        build_errors=build_errors_text,
        debug_suggestions=debug_suggestions,
        previous_code=previous_code,
        kb_errors=kb_errors,
        validation_feedback=validation_feedback,
    )

    messages = [
        LlmMessage(role="system", content=system_prompt()),
        _patch_harness_system_message(),
        LlmMessage(role="user", content=patch_generate_prompt(patch_ctx)),
    ]
    try:
        raw = chat_completion_with_retry(task.cfg.llm, messages, max_tokens=2800, stage="patch_generate", max_retries=3)
        data = _normalize_generate_plan(_extract_gen_json(raw))
        record_trajectory_action(
            "patch_generate",
            f"Generated {len(_generated_items(data))} files",
        )
        return data
    except (LlmError, Exception) as e: #TODO：错误处理应该是重连而不是直接使用placeholder
        print(f"[patch] generate failed: {e}")
        if not prompt_yes_no("PATCH 代码生成失败，是否使用 placeholder 继续？", default=False):
            raise
        print("[patch] 使用 placeholder 继续")
        return _normalize_generate_plan({
            "generate_plan": {"patches": [{
                "target_path": f"libavcodec/riscv/{task.target.module}_rvv.S",
                "action": "create",
                "content": (
                    f"/* TODO: placeholder (LLM failed: {e}) */\n"
                    ".text\n.align 2\n"
                    f".globl {task.target.symbol}\n"
                    f".type {task.target.symbol}, @function\n"
                    f"{task.target.symbol}:\n\tret\n"
                ),
                "anchor_hint": "",
                "description": "placeholder",
            }]}
        })


# ---------------------------------------------------------------------------
# Step 4: Apply patch
# ---------------------------------------------------------------------------

def apply_patch(task: TaskContext, generate_plan: dict) -> PatchArtifact:
    """Write generated code to the FFmpeg repo, record diffs and snapshots."""
    apply_ok = True
    if task.cfg and task.cfg.human.apply_ok is not None:
        apply_ok = task.cfg.human.apply_ok

    patch_id = now_id()
    apply_dir = task.run_dir / f"apply_{patch_id}"
    ensure_dir(apply_dir)

    logs: list[dict] = []
    applied_paths: list[str] = []
    diffs: list[dict] = []

    for item in _generated_items(generate_plan):
        target_path = str(item.get("target_path", "")).strip()
        content = str(item.get("content", ""))
        action = str(item.get("action", "create")).strip().lower()

        if not target_path or not content:
            continue
        if action in ("delete", "remove", "overwrite"):
            logs.append({"target_path": target_path, "action": action,
                         "success": False, "error": f"action {action} blocked"})
            continue
        if action not in {"create", "append", "replace"}:
            logs.append({"target_path": target_path, "action": action,
                         "success": False, "error": f"unsupported action: {action}"})
            continue

        dst = task.ffmpeg_root / target_path

        # Guardrail: if file already exists, downgrade create to append to avoid duplicate fragments.
        if action == "create" and dst.exists():
            action = "append"
            item["action"] = "append"
            record_trajectory_action("patch_apply_guard", f"downgrade create->append for {target_path}")

        # Read before for diff
        before = ""
        if dst.exists():
            try:
                before = dst.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass

        if not apply_ok:
            log = {"target_path": target_path, "action": action,
                   "applied_at": "dry_run", "success": True}
        else:
            _save_pre_injection(apply_dir, task.ffmpeg_root, dst)
            ensure_dir(dst.parent)

            if action == "replace" or action == "create":
                write_text(dst, content)
                applied_at = action
            else:
                existing = ""
                if dst.exists():
                    try:
                        existing = dst.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        existing = ""

                if _snippet_already_present(existing, content):
                    log = {
                        "target_path": target_path,
                        "action": action,
                        "applied_at": "skipped_duplicate",
                        "success": True,
                    }
                    logs.append(log)
                    continue

                merged = existing.rstrip("\n") + "\n\n" + content.lstrip("\n") if existing else content
                write_text(dst, merged)
                applied_at = "append"

            _snapshot(apply_dir, dst)
            log = {
                "target_path": target_path,
                "action": action,
                "applied_at": applied_at,
                "success": True,
            }

        logs.append(log)
        if log.get("success") and log.get("applied_at") not in ("dry_run", "skipped_duplicate"):
            applied_paths.append(str(dst))
            # Record diff
            after = ""
            if dst.exists():
                try:
                    after = dst.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass
            if before != after:
                diffs.append({"file": target_path, "before_len": len(before), "after_len": len(after)})

    write_json(apply_dir / "log.json", logs)
    record_trajectory_action("patch_apply", f"Applied {len(applied_paths)} file(s)")

    # Only run post-apply symbol checks when files were actually written.
    # If nothing was applied, the failure is an apply error — not a symbol error.
    missing_post_checks: list[str] = []
    if applied_paths:
        expected_symbols = _extract_expected_symbols(generate_plan)
        impl_text = ""
        register_text = ""
        for item in _generated_items(generate_plan):
            target_path = str(item.get("target_path", ""))
            full = task.ffmpeg_root / target_path
            if not full.exists():
                continue
            try:
                text = full.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            low = target_path.lower()
            if low.endswith((".s", ".asm")):
                impl_text += "\n" + text
            if low.endswith("_init.c") or low.endswith("init.c"):
                register_text += "\n" + text

        for sym in sorted(expected_symbols):
            if impl_text and not _contains_symbol_like(impl_text, sym):
                missing_post_checks.append(f"missing_impl_symbol:{sym}")
            if register_text and not _contains_symbol_like(register_text, sym):
                missing_post_checks.append(f"missing_register_symbol:{sym}")

        if missing_post_checks:
            record_trajectory_action("patch_validate", f"post_apply_missing={missing_post_checks}")

    success = (len(applied_paths) > 0 or not apply_ok) and not missing_post_checks
    error = ""
    if not applied_paths and apply_ok:
        error = "apply_failed: no files written"
    elif missing_post_checks:
        error = "; ".join(missing_post_checks)

    return PatchArtifact(
        patch_id=patch_id,
        group_id=_current_group_id(task),
        func=task.target.symbol,
        points=[],
        design={},
        generate_plan=generate_plan,
        applied_paths=applied_paths,
        diffs=diffs,
        success=success,
        error=error,
    )


# ---------------------------------------------------------------------------
# Combined PATCH handler for the state machine
# ---------------------------------------------------------------------------

def run_patch_stage(task: TaskContext, kb_patterns: list[dict] | None = None) -> TaskContext:
    """PATCH handler: generate + apply with controlled retries and routing."""
    hint = task.rollback_hint or ""
    task.rollback_hint = ""
    if hint and hint != "generate":
        print(f"[PATCH][WARN] unsupported rollback_hint={hint}, degrade to generate")
        hint = "generate"

    try:
        analysis = task.load_artifact("ANALYZE")
        az = analysis.get("analysis_json", {}) if isinstance(analysis, dict) else {}
        migratable = [str(x) for x in az.get("migratable_functions", [])] if isinstance(az, dict) else []
        if isinstance(az, dict) and "migratable_functions" in az and not migratable:
            print("[PATCH] 当前组无待迁移函数，跳过 PATCH，回到 PLAN")
            task.current_state = TaskState.PLAN
            return task
    except Exception:
        pass

    if hint:
        _rollback_previous_apply(task)

    planning_bundle = _build_planning_bundle(task)
    record_trajectory_action("patch_plan", "planning_bundle_ready")

    kb_error_dicts: list[dict] | None = None
    if task.all_build_errors:
        try:
            from ..memory.knowledge_base import KnowledgeBase
            from dataclasses import asdict as _asdict
            kb = KnowledgeBase()
            kb.load()
            error_records = kb.search_errors(keyword=task.target.symbol, max_results=5)
            if not error_records:
                error_records = kb.search_errors(max_results=3)
            if error_records:
                kb_error_dicts = [_asdict(er) for er in error_records]
        except Exception:
            pass

    print("\n[PATCH] Step 1/2: 生成代码…（可能需要 20-60 秒）")
    gen_plan = generate_code(task, kb_errors=kb_error_dicts, planning_bundle=planning_bundle)
    ok_generate, generate_issues = _validate_generate_plan(gen_plan, planning_bundle.get("analysis_json", {}))
    record_trajectory_action("patch_validate", f"generate_ok={ok_generate}; issues={generate_issues}")
    if not ok_generate:
        print(f"[PATCH] 生成闭环校验失败，自动重试一次: {generate_issues}")
        gen_plan = generate_code(
            task,
            kb_errors=kb_error_dicts,
            planning_bundle=planning_bundle,
            validation_feedback=generate_issues,
        )
        ok_generate, generate_issues = _validate_generate_plan(gen_plan, planning_bundle.get("analysis_json", {}))
        record_trajectory_action("patch_validate", f"retry_generate_ok={ok_generate}; issues={generate_issues}")

    for item in _generated_items(gen_plan):
        print(f"  → {item.get('target_path')} ({item.get('action')})")

    if not ok_generate:
        print(f"[PATCH] 生成闭环校验仍失败，回到 PATCH(generate): {generate_issues}")
        artifact = PatchArtifact(
            patch_id=now_id(),
            group_id=_current_group_id(task),
            func=task.target.symbol,
            points=[],
            design={},
            generate_plan=gen_plan,
            applied_paths=[],
            diffs=[],
            success=False,
            error="generate_validation_failed: " + "; ".join(generate_issues),
        )
        aid = task.save_artifact("PATCH", artifact, sub_id=task.target.symbol)
        task.artifacts.patch_ids.append(aid)

        task.artifacts.prebuild_generate_retries += 1
        record_trajectory_action(
            "patch_route_decision",
            f"pre_build_generate_fail -> PATCH(generate), iter={task.artifacts.prebuild_generate_retries}, issues={generate_issues}",
        )

        if task.artifacts.prebuild_generate_retries >= _MAX_PREBUILD_PATCH_RETRIES:
            print(f"[PATCH] 当前 group 预构建重试已达上限({_MAX_PREBUILD_PATCH_RETRIES})，回到 PLAN")
            task.current_state = TaskState.PLAN
            return task

        task.rollback_hint = "generate"
        task.current_state = TaskState.PATCH
        return task

    print("\n[PATCH] Step 2/2: 应用到工作区…")
    artifact = apply_patch(task, gen_plan)

    aid = task.save_artifact("PATCH", artifact, sub_id=task.target.symbol)
    task.artifacts.patch_ids.append(aid)

    if artifact.success:
        task.artifacts.prebuild_generate_retries = 0
        for ap in artifact.applied_paths:
            print(f"  ✓ {ap}")
        record_trajectory_action("patch_route_decision", "patch_apply_success -> BUILD")
        task.current_state = TaskState.BUILD
        return task

    print(f"  ✗ apply failed: {artifact.error}")
    next_state, route_reason = _route_apply_failure_with_llm(task, artifact)
    record_trajectory_action(
        "patch_route_decision",
        f"patch_apply_fail -> {next_state.value}, reason={route_reason}, error={artifact.error}",
    )

    if next_state == TaskState.PATCH:
        task.artifacts.prebuild_generate_retries += 1
        if task.artifacts.prebuild_generate_retries >= _MAX_PREBUILD_PATCH_RETRIES:
            print(f"[PATCH] apply 失败重试已达上限({_MAX_PREBUILD_PATCH_RETRIES})，回到 PLAN")
            task.current_state = TaskState.PLAN
            return task
        if not task.rollback_hint:
            task.rollback_hint = "generate"
        task.current_state = TaskState.PATCH
        return task

    if next_state == TaskState.PLAN:
        task.current_state = TaskState.PLAN
        return task

    task.current_state = TaskState.DEBUG
    return task


# ---------------------------------------------------------------------------
# Experimental: Tool-use driven PATCH entrypoint (not yet wired by default)
# ---------------------------------------------------------------------------


def run_patch_with_tools(task: TaskContext) -> PatchArtifact:
    """Experimental PATCH implementation backed by the tool-use loop.

    当前版本仅构建 prompt + tools 并运行一次工具循环，将最终结果视为
    generate_plan，并复用 apply_patch 落地。尚未接入状态机，由上层在
    试验阶段显式调用，用于对比传统 run_patch_stage 的行为。
    """

    planning_bundle = _build_planning_bundle(task)
    analysis_json = planning_bundle.get("analysis_json", {})
    repository_knowledge_entry = _load_repository_knowledge_entry(task)

    from .context_builder import ContextBuilder, PatchContext

    ctx_builder = ContextBuilder(task, kb=None)
    patch_ctx = PatchContext(
        symbol=task.target.symbol,
        analysis_json=analysis_json,
        target_files=planning_bundle.get("target_files", {}),
        repository_knowledge_entry=repository_knowledge_entry,
        existing_files_map=None,
        build_errors=None,
        debug_suggestions=None,
        previous_code=None,
        kb_errors=None,
        validation_feedback=None,
    )

    tools = _build_patch_tools(task)

    messages = [
        LlmMessage(role="system", content=system_prompt()),
        _patch_harness_system_message(),
        LlmMessage(
            role="user",
            content=(
                "你现在处于 PATCH 阶段，可以通过 JSON 调用工具来完成迁移。\n"
                "请遵循以下协议：\n\n"
                "1. 如需调用工具，请严格输出：\n"
                "   {\"tool_call\": {\"name\": \"<tool_name>\", \"arguments\": { ... }}}\n"
                "2. 完成全部修改后，请输出：\n"
                "   {\"final\": {\"generate_plan\": {\"patches\": [ ... ]}}}\n\n"
                "下面是当前 PATCH 上下文：\n\n" + patch_generate_prompt(patch_ctx)
            ),
        ),
    ]

    _, final_result = run_tool_use_loop(
        task.cfg.llm,
        messages,
        tools,
        max_rounds=6,
        max_tokens=2600,
        timeout_seconds=180.0,
        stage="patch_tools",
    )

    gen_plan: dict
    if isinstance(final_result, dict) and isinstance(final_result.get("final"), dict):
        inner = final_result["final"]
        if isinstance(inner.get("generate_plan"), dict):
            gen_plan = _normalize_generate_plan(inner)
        else:
            gen_plan = _normalize_generate_plan(inner)
    elif isinstance(final_result, dict):
        gen_plan = _normalize_generate_plan(final_result)
    else:
        # 回退：让模型自然语言输出再走一次普通 generate_code
        record_trajectory_action("patch_tools", "fallback_to_generate_code")
        gen_plan = generate_code(task, planning_bundle=planning_bundle)

    ok_generate, generate_issues = _validate_generate_plan(gen_plan, planning_bundle.get("analysis_json", {}))
    record_trajectory_action("patch_validate", f"tools_generate_ok={ok_generate}; issues={generate_issues}")

    artifact = apply_patch(task, gen_plan)
    aid = task.save_artifact("PATCH", artifact, sub_id=task.target.symbol)
    task.artifacts.patch_ids.append(aid)
    return artifact


def run_patch_stage_tools(task: TaskContext, kb_patterns: list[dict] | None = None) -> TaskContext:  # noqa: ARG001
    """Alternate PATCH handler that delegates generation to tool-use.

    生成与应用逻辑由 ``run_patch_with_tools`` 完成，这里只负责根据
    PatchArtifact 的结果更新状态机，与传统 run_patch_stage 的尾部逻辑保持一致。
    """

    artifact = run_patch_with_tools(task)

    if artifact.success:
        task.artifacts.prebuild_generate_retries = 0
        for ap in artifact.applied_paths:
            print(f"  ✓ {ap}")
        record_trajectory_action("patch_route_decision", "patch_apply_success_tools -> BUILD")
        task.current_state = TaskState.BUILD
        return task

    print(f"  ✗ apply failed (tools): {artifact.error}")
    next_state, route_reason = _route_apply_failure_with_llm(task, artifact)
    record_trajectory_action(
        "patch_route_decision",
        f"patch_apply_fail_tools -> {next_state.value}, reason={route_reason}, error={artifact.error}",
    )

    if next_state == TaskState.PATCH:
        task.artifacts.prebuild_generate_retries += 1
        if task.artifacts.prebuild_generate_retries >= _MAX_PREBUILD_PATCH_RETRIES:
            print(f"[PATCH] apply 失败重试已达上限({_MAX_PREBUILD_PATCH_RETRIES})，回到 PLAN")
            task.current_state = TaskState.PLAN
            return task
        if not task.rollback_hint:
            task.rollback_hint = "generate"
        task.current_state = TaskState.PATCH
        return task

    if next_state == TaskState.PLAN:
        task.current_state = TaskState.PLAN
        return task

    task.current_state = TaskState.DEBUG
    return task
