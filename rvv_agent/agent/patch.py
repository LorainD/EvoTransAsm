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
    load_reference_code_artifact,
    load_plan_artifact,
)
from ..core.util import ensure_dir, now_id, write_json, write_text, extract_build_errors, fmt_argv
from ..tool.interactive import prompt_yes_no
from ..tool.exec import run_configure, run_make_checkasm, configure_argv, make_checkasm_argv


def _extract_root_cause_and_dead_end(fix_strategy: str) -> tuple[str, str]:
    """Derive concise root-cause and dead-end hints from fix_strategy text."""
    text = str(fix_strategy or "").strip()
    if not text:
        return "先核对构建链路一致性（符号/宏/条件编译/注册）。", "不要盲目重生成并叠加补丁。"

    root_cause = text.split("。", 1)[0].split(";", 1)[0].strip()
    dead_end = ""
    for sep in ["。", "；", ";", "\n"]:
        for part in text.split(sep):
            p = part.strip()
            low = p.lower()
            if any(k in low for k in ["不要", "死胡同", "avoid", "do not", "别"]):
                dead_end = p
                break
        if dead_end:
            break

    if not root_cause:
        root_cause = text[:100]
    if not dead_end:
        dead_end = "不要先改算术细节，先核对 Makefile/初始化注册/架构宏体系是否匹配。"

    return root_cause[:120], dead_end[:120]


def _select_patch_kb_errors(task: TaskContext, max_results: int = 5) -> list[dict] | None:
    """Select medium-detail KB error lessons for PATCH generation.

    Strategy:
    1) current error semantic match with class filter
    2) symbol/module keyword match
    3) class-only fallback
    """
    try:
        from ..memory.knowledge_base import KnowledgeBase
        from .debug import classify_error

        kb = KnowledgeBase()
        kb.load()
    except Exception:
        return None

    symbol = str(task.target.symbol or "").strip()
    module = str(task.target.module or "").strip()
    symbol_leaf = symbol.split(".")[-1] if symbol else ""

    current_error = str(task.all_build_errors[-1] if task.all_build_errors else "").strip()
    error_class = classify_error(current_error) if current_error else None

    candidates: list = []
    if current_error:
        try:
            sem = kb.search_errors_semantic(
                current_error,
                task.cfg,
                error_class=error_class,
                max_results=max(4, max_results),
                min_score=0.55,
            ) if task.cfg else kb.search_errors(error_class=error_class, max_results=max(4, max_results))
            candidates.extend(sem)
        except Exception:
            pass

    for kw in [symbol, symbol_leaf, module]:
        if not kw:
            continue
        try:
            candidates.extend(kb.search_errors(error_class=error_class, keyword=kw, max_results=max_results))
        except Exception:
            continue

    if not candidates:
        try:
            candidates.extend(kb.search_errors(error_class=error_class, max_results=max_results))
        except Exception:
            pass

    if not candidates:
        return None

    # Dedupe while preserving rough relevance order.
    merged: list = []
    seen: set[tuple[str, str]] = set()
    for rec in candidates:
        key = (str(rec.error_class), str(rec.pattern))
        if key in seen:
            continue
        seen.add(key)
        merged.append(rec)
        if len(merged) >= max_results:
            break

    kb_error_dicts: list[dict] = []
    for rec in merged[:max_results]:
        root_cause, dead_end = _extract_root_cause_and_dead_end(str(rec.fix_strategy or ""))
        kb_error_dicts.append(
            {
                "error_class": str(rec.error_class or "unknown"),
                "pattern": str(rec.pattern or "")[:160],
                "root_cause": root_cause,
                "dead_end": dead_end,
                "fix_strategy": str(rec.fix_strategy or "")[:280],
                "count": int(getattr(rec, "count", 1) or 1),
            }
        )

    return kb_error_dicts or None


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


# ---------------------------------------------------------------------------
# Group-advance helper (avoid PLAN<->PATCH infinite loops)
# ---------------------------------------------------------------------------

def _move_to_next_group_or_finish(task: TaskContext, *, outcome: str = "failed") -> TaskContext:
    """Advance PLAN group index, marking current group completed/failed.

    This mirrors DEBUG's group-advance behavior to prevent state-machine loops
    where PATCH sets current_state=PLAN but doesn't advance current_group_idx.
    """
    try:
        plan_data = load_plan_artifact(task.load_artifact("PLAN"))
    except Exception:
        task.current_state = TaskState.TASK_UPDATE
        return task

    if not plan_data.groups:
        task.current_state = TaskState.TASK_UPDATE
        return task

    outcome = (outcome or "failed").strip().lower()
    if outcome not in {"failed", "completed", "skipped"}:
        outcome = "failed"

    current_group_id = ""
    if 0 <= int(plan_data.current_group_idx) < len(plan_data.groups):
        gid = plan_data.groups[int(plan_data.current_group_idx)].group_id
        current_group_id = gid or ""
        if current_group_id:
            if outcome == "failed":
                if current_group_id not in plan_data.failed_groups:
                    plan_data.failed_groups.append(current_group_id)
            else:
                if current_group_id not in plan_data.completed_groups:
                    plan_data.completed_groups.append(current_group_id)

    # Best-effort rollback group changes before moving on (same as DEBUG).
    if current_group_id:
        try:
            rollback_group_applies(task, current_group_id)
        except Exception as e:  # noqa: BLE001
            print(f"[PATCH][WARN] 跳组前回滚失败(group_id={current_group_id}): {e}")

    blocked = set(plan_data.completed_groups) | set(plan_data.failed_groups)
    next_idx: int | None = None
    for idx, group in enumerate(plan_data.groups):
        if group.group_id not in blocked:
            next_idx = idx
            break

    if next_idx is None:
        task.save_artifact("PLAN", plan_data)
        task.current_state = TaskState.TASK_UPDATE
        return task

    plan_data.current_group_idx = next_idx
    task.save_artifact("PLAN", plan_data)
    task.artifacts.group_iteration_count = 0
    task.artifacts.prebuild_generate_retries = 0
    task.artifacts.active_group_id = ""
    task.current_state = TaskState.PLAN
    return task


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


# Regex: matches lines like  OBJS-$(CONFIG_H264DSP) += h264dsp.o  (with flexible whitespace)
_CONFIG_TAG_RE = re.compile(
    r"^\s*\w*OBJS-\$\((CONFIG_[A-Z0-9_]+)\)\s*\+="
)


def lookup_config_tag(ffmpeg_root: Path, module: str, lib_root: str) -> str | None:
    """Look up the correct CONFIG_ tag from the parent directory's Makefile.

    Searches ``{lib_root}/Makefile`` (e.g. ``libavcodec/Makefile``) for an
    OBJS line that compiles the module's C source, and extracts the
    ``CONFIG_XXX`` variable from ``$(CONFIG_XXX)``.

    This is the **only** correct data source for CONFIG_ tags — never invent
    new tags.  For example, searching ``h264dsp.o`` yields ``CONFIG_H264DSP``.

    Returns the tag string (e.g. ``"CONFIG_H264DSP"``) or ``None`` if the
    parent Makefile doesn't exist or no matching line is found.
    """
    parent_makefile = ffmpeg_root / lib_root / "Makefile"
    if not parent_makefile.exists():
        return None

    try:
        text = parent_makefile.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    mod = module.lower().rstrip("_")

    # Build candidate .o names to search for, ordered from most specific to
    # least specific so we prefer an exact match.
    candidates = [
        f"{mod}.o",
        f"{mod}dsp.o",
    ]

    for line in text.splitlines():
        line_lower = line.lower()
        for cand in candidates:
            if cand in line_lower:
                m = _CONFIG_TAG_RE.match(line)
                if m:
                    return m.group(1)

    # Broader fallback: any OBJS line whose .o list contains the module stem.
    for line in text.splitlines():
        if module.lower() in line.lower() and "+=" in line:
            m = _CONFIG_TAG_RE.match(line)
            if m:
                return m.group(1)

    return None


def _infer_lib_root(module: str, selected_files: list[str], existing_rvv: list[str]) -> str:
    """Infer target lib root (libavcodec/libswscale/...) from selected references."""
    mod = (module or "").lower()
    candidates = [str(p or "") for p in (existing_rvv or [])] + [str(p or "") for p in (selected_files or [])]

    for rel in candidates:
        p = rel.replace("\\", "/")
        parts = p.split("/")
        if len(parts) < 3:
            continue
        if not parts[0].startswith("lib"):
            continue
        if parts[1] != "riscv":
            continue
        if mod and mod in parts[-1].lower():
            return parts[0]

    for rel in candidates:
        p = rel.replace("\\", "/")
        parts = p.split("/")
        if len(parts) >= 3 and parts[0].startswith("lib") and parts[1] == "riscv":
            return parts[0]

    return "libavcodec"


def _check_source_has_riscv_decl(ffmpeg_root: Path, module: str, lib_root: str) -> bool:
    """Check source C files for #if ARCH_RISCV declaration block."""
    candidates = [
        f"{lib_root}/{module}dsp_init.c",
        f"{lib_root}/{module}.c",
        f"{lib_root}/{module}dsp.c",
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
    reference = load_reference_code_artifact(task.load_artifact("BUILD_REFERENCE"))

    analysis_json = _build_group_scoped_analysis(task)
    selected_files = file_search.get("selected_files", [])
    existing_rvv = list(reference.existing_rvv or [])

    grouped_functions = analysis_json.get("all_group_functions", []) if isinstance(analysis_json, dict) else []
    if not isinstance(grouped_functions, list):
        grouped_functions = []

    combined_context_parts: list[str] = []
    contexts = reference.function_contexts if isinstance(reference.function_contexts, dict) else {}
    for name in grouped_functions:
        key = str(name or "").strip()
        if not key:
            continue
        entry = contexts.get(key, {})
        code = str(entry.get("code_context", "")) if isinstance(entry, dict) else ""
        if code:
            combined_context_parts.append(f"=== {key} ===\n{code}")
    if not combined_context_parts:
        for key, entry in contexts.items():
            if not isinstance(entry, dict):
                continue
            code = str(entry.get("code_context", ""))
            if code:
                combined_context_parts.append(f"=== {key} ===\n{code}")
                break

    lib_root = _infer_lib_root(task.target.module, selected_files, existing_rvv)

    module = task.target.module
    riscv_dir = task.ffmpeg_root / lib_root / "riscv"
    rvv_path = f"{lib_root}/riscv/{module}_rvv.S"
    init_path = f"{lib_root}/riscv/{module}_init.c"
    makefile_path = f"{lib_root}/riscv/Makefile"

    # Look up the authoritative CONFIG_ tag from the parent Makefile.
    config_tag = lookup_config_tag(task.ffmpeg_root, module, lib_root)

    return {
        "analysis_json": analysis_json,
        "selected_files": selected_files,
        "code_context": "\n\n".join(combined_context_parts),
        "existing_rvv": existing_rvv,
        "module": module,
        "symbol": task.target.symbol,
        "target_files": {
            "lib_root": lib_root,
            "rvv": rvv_path,
            "init": init_path,
            "makefile": makefile_path,
            "config_tag": config_tag,
            "rvv_exists": (task.ffmpeg_root / rvv_path).exists(),
            "init_exists": (task.ffmpeg_root / init_path).exists(),
            "makefile_exists": (task.ffmpeg_root / makefile_path).exists(),
            "riscv_dir_exists": riscv_dir.exists(),
            "init_has_rvv_block": _check_has_rvv_block(task.ffmpeg_root / init_path),
            "makefile_has_module": _check_makefile_has_module(task.ffmpeg_root / makefile_path, module),
            "source_has_riscv_decl": _check_source_has_riscv_decl(task.ffmpeg_root, module, lib_root),
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
# Anchor-aware append helper
# ---------------------------------------------------------------------------

def _apply_append_with_anchor(existing: str, content: str, anchor_hint: str, target_path: str) -> str:
    """Merge *content* into *existing* at the position indicated by *anchor_hint*.

    Supported anchor_hint values
    ----------------------------
    ``file_start``
        Prepend content before everything else.  Used for Makefile declarations
        that must appear at the top of the file.

    ``before_endif``
        Insert content just before the **last** ``#endif`` line in the file.
        Used to inject ``#elif ARCH_RISCV`` blocks into source C/H files that
        already contain other architecture guards.

    ``before_arch_chain_endif``
        Insert content just before the closing ``#endif`` of the **last**
        ``#if/#elif ARCH_*`` preprocessor chain (i.e. right where other
        architecture branches live). This is safer than ``before_endif`` for
        files that contain other unrelated ``#endif`` blocks (header guards,
        feature checks, etc.).

    ``before_arch_endif``
        Alias for ``before_endif`` — kept for backward compatibility with older
        LLM outputs.

    ``after_arch_block``
        Insert content right after the last ``#elif ARCH_*`` / ``#if ARCH_*``
        block's closing ``#endif``.  Falls back to ``before_endif`` when the
        pattern is not found.

    *(empty / anything else)*
        Classic tail-append: ``existing + "\\n\\n" + content``.
    """
    if not existing:
        return content

    hint = (anchor_hint or "").strip().lower()

    # ------------------------------------------------------------------ #
    # file_start — prepend (Makefile declarations)
    # ------------------------------------------------------------------ #
    if hint == "file_start":
        return content.rstrip("\n") + "\n\n" + existing.lstrip("\n")

    # ------------------------------------------------------------------ #
    # before_endif / before_arch_endif — inject before last #endif
    # Used for ARCH_RISCV blocks in C/H dispatcher functions.
    # ------------------------------------------------------------------ #
    if hint in ("before_endif", "before_arch_endif"):
        lines = existing.splitlines(keepends=True)
        # Find the last line that is a bare #endif (possibly with comment)
        last_endif_idx = -1
        for i in range(len(lines) - 1, -1, -1):
            stripped = lines[i].strip()
            if re.match(r"^#\s*endif\b", stripped):
                last_endif_idx = i
                break
        if last_endif_idx == -1:
            # No #endif found — fall back to tail append
            return existing.rstrip("\n") + "\n\n" + content.lstrip("\n")
        injected = content.rstrip("\n") + "\n"
        lines.insert(last_endif_idx, injected)
        return "".join(lines)

    # ------------------------------------------------------------------ #
    # before_arch_chain_endif — inject before the closing #endif of the
    # last ARCH_* chain (preferred for ARCH_RISCV branch injection).
    # ------------------------------------------------------------------ #
    if hint == "before_arch_chain_endif":
        lines = existing.splitlines(keepends=True)
        # Locate the last #if/#elif ARCH_* line.
        chain_start = -1
        for i in range(len(lines) - 1, -1, -1):
            if re.search(r"#\s*(if|elif)\s+ARCH_", lines[i]):
                chain_start = i
                break
        if chain_start == -1:
            # No ARCH chain found — fall back to before_endif
            return _apply_append_with_anchor(existing, content, "before_endif", target_path)

        # Walk forward to find the closing #endif for the chain_start's #if.
        # Note: chain_start can be "#elif ARCH_*" (the chain's opening "#if"
        # is above it). In that case we must treat the chain as already open,
        # otherwise a nested "#if ... #endif" inside the branch could be
        # mistaken as the chain's closing "#endif".
        start_line = lines[chain_start].strip()
        depth = 1 if re.match(r"^#\s*elif\b", start_line) else 0
        closing_endif_idx = -1
        for i in range(chain_start, len(lines)):
            stripped = lines[i].strip()
            if re.match(r"^#\s*if\b", stripped):
                depth += 1
            elif re.match(r"^#\s*endif\b", stripped):
                # Close one level.
                if depth > 0:
                    depth -= 1
                # When we return to zero, we found the chain's closing endif.
                if depth == 0:
                    closing_endif_idx = i
                    break

        if closing_endif_idx == -1:
            return _apply_append_with_anchor(existing, content, "before_endif", target_path)

        injected = content.rstrip("\n") + "\n"
        lines.insert(closing_endif_idx, injected)
        return "".join(lines)

    # ------------------------------------------------------------------ #
    # after_arch_block — insert after the closing #endif of the last
    # ARCH_* preprocessor block.
    # ------------------------------------------------------------------ #
    if hint == "after_arch_block":
        lines = existing.splitlines(keepends=True)
        # Locate the last #if ARCH_* or #elif ARCH_* line
        arch_block_start = -1
        for i in range(len(lines) - 1, -1, -1):
            if re.search(r"#\s*(if|elif)\s+ARCH_", lines[i]):
                arch_block_start = i
                break
        if arch_block_start == -1:
            # No ARCH block found — fall back to before_endif
            return _apply_append_with_anchor(existing, content, "before_endif", target_path)
        # Find the matching #endif after arch_block_start
        depth = 0
        insert_after = -1
        for i in range(arch_block_start, len(lines)):
            stripped = lines[i].strip()
            if re.match(r"^#\s*if\b", stripped):
                depth += 1
            elif re.match(r"^#\s*endif\b", stripped):
                if depth > 0:
                    depth -= 1
                else:
                    insert_after = i
                    break
        if insert_after == -1:
            return _apply_append_with_anchor(existing, content, "before_endif", target_path)
        injected = "\n" + content.rstrip("\n") + "\n"
        lines.insert(insert_after + 1, injected)
        return "".join(lines)

    # ------------------------------------------------------------------ #
    # Default: tail append
    # ------------------------------------------------------------------ #
    return existing.rstrip("\n") + "\n\n" + content.lstrip("\n")


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
        # Pipeline safety: never raise from PATCH generation.
        # Return a structured error so the caller can route differently from
        # content-validation failures.
        err = f"llm_generate_failed:{type(e).__name__}:{str(e)[:240]}"
        record_trajectory_action("patch_generate_error", err)
        return {"generated": [], "error": err}


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
    skipped_missing_path = 0
    skipped_empty_content = 0

    for item in _generated_items(generate_plan):
        target_path = str(item.get("target_path", "")).strip()
        content = str(item.get("content", ""))
        action = str(item.get("action", "create")).strip().lower()

        if not target_path:
            skipped_missing_path += 1
            logs.append({"target_path": "", "action": action, "success": False, "error": "skip_missing_target_path"})
            continue
        if not content.strip():
            skipped_empty_content += 1
            logs.append({"target_path": target_path, "action": action, "success": False, "error": "skip_empty_content"})
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

                anchor_hint = str(item.get("anchor_hint", "")).strip().lower()
                # Heuristic fallback: if LLM forgot anchor_hint, infer safe insertion.
                # - Makefile: declarations must be at file start.
                # - ARCH_RISCV injection: place inside the architecture chain.
                if not anchor_hint:
                    low_path = target_path.lower().replace("\\", "/")
                    low_content = content.lower()
                    if low_path.endswith("/makefile") or low_path.endswith("makefile"):
                        anchor_hint = "file_start"
                    elif re.search(r"#\s*(if|elif)\s+arch_riscv\b", low_content):
                        anchor_hint = "before_arch_chain_endif"
                merged = _apply_append_with_anchor(existing, content, anchor_hint, target_path)
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
        if skipped_missing_path or skipped_empty_content:
            error = (
                "apply_failed: no files written; "
                f"skipped_missing_path={skipped_missing_path}; "
                f"skipped_empty_content={skipped_empty_content}"
            )
        else:
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
            return _move_to_next_group_or_finish(task, outcome="completed")
    except Exception:
        pass

    if hint:
        _rollback_previous_apply(task)

    planning_bundle = _build_planning_bundle(task)
    record_trajectory_action("patch_plan", "planning_bundle_ready")

    kb_error_dicts: list[dict] | None = _select_patch_kb_errors(task, max_results=5)

    print("\n[PATCH] Step 1/2: 生成代码…（可能需要 20-60 秒）")
    gen_plan = generate_code(task, kb_errors=kb_error_dicts, planning_bundle=planning_bundle)
    gen_error = str(gen_plan.get("error", "") or "") if isinstance(gen_plan, dict) else ""
    if gen_error:
        # Infrastructure failure (LLM/network/JSON extraction) — do not treat as contract failure.
        task.artifacts.prebuild_generate_retries += 1
        record_trajectory_action(
            "patch_route_decision",
            f"patch_generate_infra_error -> retry_or_skip, iter={task.artifacts.prebuild_generate_retries}, error={gen_error}",
        )
        if task.artifacts.prebuild_generate_retries >= _MAX_PREBUILD_PATCH_RETRIES:
            print(f"[PATCH] 生成基础设施错误重试已达上限({_MAX_PREBUILD_PATCH_RETRIES})，跳过当前组")
            return _move_to_next_group_or_finish(task, outcome="failed")
        task.rollback_hint = "generate"
        task.current_state = TaskState.PATCH
        return task
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
        gen_error = str(gen_plan.get("error", "") or "") if isinstance(gen_plan, dict) else ""
        if gen_error:
            task.artifacts.prebuild_generate_retries += 1
            record_trajectory_action(
                "patch_route_decision",
                f"patch_generate_infra_error_after_validation -> retry_or_skip, iter={task.artifacts.prebuild_generate_retries}, error={gen_error}",
            )
            if task.artifacts.prebuild_generate_retries >= _MAX_PREBUILD_PATCH_RETRIES:
                print(f"[PATCH] 生成基础设施错误重试已达上限({_MAX_PREBUILD_PATCH_RETRIES})，跳过当前组")
                return _move_to_next_group_or_finish(task, outcome="failed")
            task.rollback_hint = "generate"
            task.current_state = TaskState.PATCH
            return task
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
            return _move_to_next_group_or_finish(task, outcome="failed")

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
            return _move_to_next_group_or_finish(task, outcome="failed")
        if not task.rollback_hint:
            task.rollback_hint = "generate"
        task.current_state = TaskState.PATCH
        return task

    if next_state == TaskState.PLAN:
        return _move_to_next_group_or_finish(task, outcome="failed")

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
            return _move_to_next_group_or_finish(task, outcome="failed")
        if not task.rollback_hint:
            task.rollback_hint = "generate"
        task.current_state = TaskState.PATCH
        return task

    if next_state == TaskState.PLAN:
        return _move_to_next_group_or_finish(task, outcome="failed")

    task.current_state = TaskState.DEBUG
    return task
