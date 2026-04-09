"""agent.patch — 4-step PATCH stage for the state-machine pipeline.

Replaces the old generate+inject flow with a structured sequence:
  1. locate_patch_points  — LLM finds precise insertion anchors
  2. design_patch         — LLM decides *what* to change
  3. generate_code        — LLM produces the actual code
  4. apply_patch          — tool writes code to repo, records diffs

Core injection helpers are imported from agent/inject.py (shared with
pipeline mode).  Code generation JSON parsing is imported from
agent/generate.py.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from ..core.config import AppConfig
from ..core.llm import LlmError, LlmMessage, chat_completion_with_retry, record_trajectory_action
from ..core.prompts import system_prompt
from ..core.prompts_patch import (
    patch_design_prompt,
    patch_generate_prompt,
    patch_locate_prompt,
)
from ..core.task import (
    PatchArtifact,
    PatchDesign,
    PatchPoint,
    TaskContext,
    TaskState,
)
from ..core.util import ensure_dir, now_id, write_json, write_text
from ..tool.interactive import prompt_yes_no


# ---------------------------------------------------------------------------
# Shared helpers (re-exported from generate.py / inject.py)
# ---------------------------------------------------------------------------

from ..core.util import extract_json_from_llm, snippet_exists, snapshot_file

# Aliases for backward compat within this module
_extract_gen_json = extract_json_from_llm
_snippet_already_present = snippet_exists
_snapshot = snapshot_file


def _save_pre_injection(apply_dir: Path, dst: Path) -> None:
    """Save the ORIGINAL content of dst before injection (for rollback)."""
    try:
        pre_dir = apply_dir / "pre_injection"
        parts = dst.parts
        rel = Path(*parts[-3:]) if len(parts) >= 3 else Path(dst.name)
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


def _rollback_previous_apply(task: TaskContext) -> None:
    """Restore files modified by the most recent apply to their pre-injection state.

    Reads from ``apply_<id>/pre_injection/`` and writes back to ffmpeg_root.
    Files that didn't exist before injection are deleted.
    """
    if not task.artifacts.patch_ids:
        return
    # Find the most recent apply directory
    try:
        sub = task.artifacts.patch_ids[-1].split("/")[-1]
        prev_patch = task.load_artifact("PATCH", sub_id=sub)
        patch_id = prev_patch.get("patch_id", "")
    except Exception:
        return
    if not patch_id:
        return

    apply_dir = task.run_dir / f"apply_{patch_id}"
    pre_dir = apply_dir / "pre_injection"
    if not pre_dir.exists():
        return

    restored = 0
    for pre_file in pre_dir.rglob("*"):
        if not pre_file.is_file():
            continue
        if pre_file.name.endswith(".__new__"):
            continue
        # Reconstruct the target path
        rel = pre_file.relative_to(pre_dir)
        # Check if this was a newly created file
        marker = pre_file.parent / (pre_file.name + ".__new__")
        dst = task.ffmpeg_root / rel
        if marker.exists():
            # File didn't exist before — delete it
            if dst.exists():
                dst.unlink()
                restored += 1
        else:
            # Restore original content
            original = pre_file.read_text(encoding="utf-8", errors="replace")
            if dst.exists():
                write_text(dst, original)
                restored += 1

    if restored:
        print(f"[PATCH] 已回滚 {restored} 个文件到注入前状态")


def _inject_asm_file(dst: Path, content: str, apply_dir: Path) -> dict:
    """Inject content into a .S file (create or append)."""
    rel = str(dst)
    _save_pre_injection(apply_dir, dst)
    if not dst.exists():
        ensure_dir(dst.parent)
        write_text(dst, content)
        _snapshot(apply_dir, dst)
        return {"target_path": rel, "action": "create", "applied_at": "new_file", "success": True}
    existing = dst.read_text(encoding="utf-8", errors="replace")
    if _snippet_already_present(existing, content):
        return {"target_path": rel, "action": "append", "applied_at": "skipped_duplicate", "success": True}
    merged = existing.rstrip("\n") + "\n\n" + content.lstrip("\n")
    write_text(dst, merged)
    _snapshot(apply_dir, dst)
    return {"target_path": rel, "action": "append", "applied_at": "end", "success": True}


def _inject_text_file(dst: Path, content: str, anchor_hint: str,
                       apply_dir: Path, cfg: AppConfig | None) -> dict:
    """Inject content into a .c/.h/Makefile (create, or LLM-located insert)."""
    rel = str(dst)
    _save_pre_injection(apply_dir, dst)
    if not dst.exists():
        ensure_dir(dst.parent)
        write_text(dst, content)
        _snapshot(apply_dir, dst)
        return {"target_path": rel, "action": "create", "applied_at": "new_file", "success": True}
    existing = dst.read_text(encoding="utf-8", errors="replace")
    if _snippet_already_present(existing, content):
        return {"target_path": rel, "action": "append", "applied_at": "skipped_duplicate", "success": True}

    insert_after = -1
    applied_at = "end"
    if cfg is not None and anchor_hint:
        try:
            insert_after = _locate_insertion_line_llm(cfg, rel, existing, content, anchor_hint)
        except Exception:
            insert_after = -1
    if insert_after >= 0:
        lines = existing.splitlines(keepends=True)
        pos = min(insert_after + 1, len(lines))
        inject = content.lstrip("\n")
        if not inject.endswith("\n"):
            inject += "\n"
        lines.insert(pos, inject)
        new_content = "".join(lines)
        applied_at = f"line:{insert_after}"
    else:
        new_content = existing.rstrip("\n") + "\n" + content.lstrip("\n")
    write_text(dst, new_content)
    _snapshot(apply_dir, dst)
    return {"target_path": rel, "action": "inject", "applied_at": applied_at, "success": True}


def _locate_insertion_line_llm(cfg: AppConfig, target_path: str,
                                existing_content: str, snippet: str,
                                anchor_hint: str) -> int:
    """Call Locator LLM to find 0-based line to insert after. Returns -1 for end."""
    from ..core.prompts import injection_locator_prompt
    messages = [
        LlmMessage(role="system", content=system_prompt()),
        LlmMessage(role="user", content=injection_locator_prompt(
            target_path=target_path,
            existing_content=existing_content,
            snippet=snippet,
            anchor_hint=anchor_hint,
        )),
    ]
    try:
        raw = chat_completion_with_retry(cfg.llm, messages, max_tokens=300, stage="patch_locate_line", max_retries=3)
        raw = raw.strip()
        s = raw.find("{")
        e = raw.rfind("}")
        data = json.loads(raw[s:e + 1]) if s != -1 and e > s else json.loads(raw)
        if data.get("strategy") == "insert_after_line":
            return int(data.get("line", -1))
        return -1
    except Exception:
        return -1


# ---------------------------------------------------------------------------
# Step 1: Locate patch points
# ---------------------------------------------------------------------------

def locate_patch_points(task: TaskContext) -> list[PatchPoint]:
    """LLM determines precise insertion anchors for the migration."""
    file_search = task.load_artifact("SEARCH_FILE")
    if task.artifacts.reference_code_ids:
        sub = task.artifacts.reference_code_ids[-1].split("/", 1)[-1]
        reference = task.load_artifact("BUILD_REFERENCE", sub_id=sub)
    else:
        reference = task.load_artifact("BUILD_REFERENCE")
    analysis = task.load_artifact("ANALYZE")

    messages = [
        LlmMessage(role="system", content=system_prompt()),
        LlmMessage(role="user", content=patch_locate_prompt(
            symbol=task.target.symbol,
            analysis_json=analysis.get("analysis_json", {}),
            selected_files=file_search.get("selected_files", []),
            code_context=reference.get("code_context", ""),
        )),
    ]
    try:
        raw = chat_completion_with_retry(task.cfg.llm, messages, max_tokens=1200, stage="patch_locate", max_retries=3)
        data = _extract_gen_json(raw)
        points = []
        for pp in data.get("patch_points", []):
            points.append(PatchPoint(
                file=str(pp.get("file", "")),
                line=int(pp.get("line", -1)),
                surrounding_hash=hashlib.md5(
                    str(pp.get("file", "")).encode()
                ).hexdigest()[:8],
                rationale=str(pp.get("rationale", "")),
            ))
        record_trajectory_action("patch_locate", f"Located {len(points)} patch points")
        return points
    except (LlmError, Exception) as e:  #TODO：错误处理应该是重连而不是直接使用fallback
        print(f"[patch] locate failed: {e}")
        if not prompt_yes_no("PATCH 定位失败，是否使用 fallback 锚点继续？", default=False):
            raise
        print("[patch] 使用 fallback 锚点继续")
        return [PatchPoint(
            file=f"libavcodec/riscv/{task.target.module}_rvv.S",
            line=-1,
            rationale="fallback: new RVV assembly file",
        )]


# ---------------------------------------------------------------------------
# Step 2: Design patch
# ---------------------------------------------------------------------------

def design_patch(task: TaskContext, points: list[PatchPoint],
                 kb_patterns: list[dict] | None = None) -> PatchDesign:
    """LLM decides what changes to make (without generating code yet)."""
    analysis = task.load_artifact("ANALYZE")

    messages = [
        LlmMessage(role="system", content=system_prompt()),
        LlmMessage(role="user", content=patch_design_prompt(
            symbol=task.target.symbol,
            analysis_json=analysis.get("analysis_json", {}),
            patch_points=[asdict(p) for p in points],
            kb_patterns=kb_patterns,
        )),
    ]
    try:
        raw = chat_completion_with_retry(task.cfg.llm, messages, max_tokens=1200, stage="patch_design", max_retries=3)
        data = _extract_gen_json(raw)
        design = PatchDesign(
            changes=data.get("changes", []),
            rationale=data.get("rationale", ""),
        )
        record_trajectory_action("patch_design", f"Designed {len(design.changes)} changes")
        return design
    except (LlmError, Exception) as e:
        print(f"[patch] design failed: {e}")
        if not prompt_yes_no("PATCH 设计失败，是否使用 fallback 设计继续？", default=False):
            raise
        print("[patch] 使用 fallback 设计继续")
        return PatchDesign(
            changes=[{
                "type": "create_file",
                "file": f"libavcodec/riscv/{task.target.module}_rvv.S",
                "description": "RVV assembly implementation",
                "code_items": [task.target.symbol],
            }],
            rationale="fallback design",
        )


# ---------------------------------------------------------------------------
# Step 3: Generate code
# ---------------------------------------------------------------------------

def generate_code(task: TaskContext, design: PatchDesign,
                   kb_errors: list[dict] | None = None) -> dict:
    """LLM generates actual code based on the design. Returns generate_plan dict.

    On retry (after DEBUG), includes build errors, debug suggestions, and the
    previous failing code in the prompt so the LLM can produce a targeted fix.
    """
    analysis = task.load_artifact("ANALYZE")
    file_search = task.load_artifact("SEARCH_FILE")
    if task.artifacts.reference_code_ids:
        sub = task.artifacts.reference_code_ids[-1].split("/", 1)[-1]
        reference = task.load_artifact("BUILD_REFERENCE", sub_id=sub)
    else:
        reference = task.load_artifact("BUILD_REFERENCE")

    # Build existing_files_map for incremental merge
    # Include .S files so LLM can see existing RVV implementations
    existing_map: dict[str, str] = {}
    for rel in file_search.get("selected_files", []):
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
    for rel in reference.get("existing_rvv", []):
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

    messages = [
        LlmMessage(role="system", content=system_prompt()),
        LlmMessage(role="user", content=patch_generate_prompt(
            symbol=task.target.symbol,
            analysis_json=analysis.get("analysis_json", {}),
            design=asdict(design),
            existing_files_map=existing_map or None,
            build_errors=build_errors_text,
            debug_suggestions=debug_suggestions,
            previous_code=previous_code,
            kb_errors=kb_errors,
        )),
    ]
    try:
        raw = chat_completion_with_retry(task.cfg.llm, messages, max_tokens=2800, stage="patch_generate", max_retries=3)
        data = _extract_gen_json(raw)
        # Normalize legacy format
        if "files" in data and "generated" not in data:
            data = {
                "generated": [
                    {
                        "target_path": f.get("path", ""),
                        "action": "create",
                        "content": f.get("content", ""),
                        "anchor_hint": "",
                        "description": "",
                    }
                    for f in data.get("files", [])
                ]
            }
        record_trajectory_action(
            "patch_generate",
            f"Generated {len(data.get('generated', []))} files",
        )
        return data
    except (LlmError, Exception) as e: #TODO：错误处理应该是重连而不是直接使用placeholder
        print(f"[patch] generate failed: {e}")
        if not prompt_yes_no("PATCH 代码生成失败，是否使用 placeholder 继续？", default=False):
            raise
        print("[patch] 使用 placeholder 继续")
        return {
            "generated": [{
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
            }]
        }


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

    for item in generate_plan.get("generated", []):
        target_path = str(item.get("target_path", "")).strip()
        content = str(item.get("content", ""))
        anchor_hint = str(item.get("anchor_hint", ""))
        action = str(item.get("action", "create")).strip()

        if not target_path or not content:
            continue
        if action.lower() in ("delete", "replace", "remove", "overwrite"):
            logs.append({"target_path": target_path, "action": action,
                         "success": False, "error": f"action '{action}' blocked"})
            continue

        dst = task.ffmpeg_root / target_path

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
        elif Path(target_path).suffix == ".S":
            log = _inject_asm_file(dst, content, apply_dir)
        else:
            log = _inject_text_file(dst, content, anchor_hint, apply_dir, task.cfg)

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

    return PatchArtifact(
        patch_id=patch_id,
        func=task.target.symbol,
        points=[],
        design={},
        generate_plan=generate_plan,
        applied_paths=applied_paths,
        diffs=diffs,
        success=len(applied_paths) > 0 or not apply_ok,
    )


# ---------------------------------------------------------------------------
# Helpers: reload previous PATCH sub-step results for partial retry
# ---------------------------------------------------------------------------

def _load_previous_points(task: TaskContext) -> list[PatchPoint]:
    """Load PatchPoints from the most recent PatchArtifact."""
    if not task.artifacts.patch_ids:
        return []
    try:
        prev = task.load_artifact("PATCH", sub_id=task.artifacts.patch_ids[-1].split("/")[-1])
        points: list[PatchPoint] = []
        for pp in prev.get("points", []):
            if not isinstance(pp, dict):
                continue
            points.append(
                PatchPoint(
                    file=str(pp.get("file", "")),
                    line=int(pp.get("line", -1)),
                    surrounding_hash=str(pp.get("surrounding_hash", "")),
                    rationale=str(pp.get("rationale", "")),
                )
            )
        return points
    except Exception:
        return []


def _load_previous_design(task: TaskContext) -> PatchDesign:
    """Load PatchDesign from the most recent PatchArtifact."""
    if not task.artifacts.patch_ids:
        return PatchDesign(changes=[], rationale="fallback")
    try:
        prev = task.load_artifact("PATCH", sub_id=task.artifacts.patch_ids[-1].split("/")[-1])
        d = prev.get("design", {})
        return PatchDesign(changes=d.get("changes", []), rationale=d.get("rationale", ""))
    except Exception:
        return PatchDesign(changes=[], rationale="fallback")


# ---------------------------------------------------------------------------
# Combined PATCH handler for the state machine
# ---------------------------------------------------------------------------

def run_patch_stage(task: TaskContext, kb_patterns: list[dict] | None = None) -> TaskContext:
    """PATCH handler: runs sub-steps based on rollback hint, persists PatchArtifact.

    On first run (no rollback_hint), all 4 steps execute.
    After DEBUG, rollback_hint controls which sub-steps to re-run:
      - "locate"   → re-run all 4 (full retry)
      - "design"   → skip locate, re-run design/generate/apply
      - "generate" → skip locate+design, re-run generate/apply
    """
    hint = task.rollback_hint or ""
    task.rollback_hint = ""  # consume the hint

    # Respect ANALYZE migration decision: only migrate functions marked as migrate=1.
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

    # If this is a retry (after DEBUG), rollback previous injection first
    is_retry = bool(hint)
    if is_retry:
        _rollback_previous_apply(task)

    # Determine which sub-steps to run
    run_locate = hint in ("", "locate")
    run_design = hint in ("", "locate", "design")

    # --- Step 1: Locate ---
    if run_locate:
        print("\n[PATCH] Step 1/4: 定位锚点…")
        points = locate_patch_points(task)
        for p in points:
            print(f"  {p.file}:{p.line} — {p.rationale}")
    else:
        # Reuse points from previous patch artifact
        points = _load_previous_points(task)
        print(f"\n[PATCH] Step 1/4: 复用上次锚点 ({len(points)} 个)")

    # --- Step 2: Design ---
    if run_design:
        print("\n[PATCH] Step 2/4: 设计变更方案…")
        design = design_patch(task, points, kb_patterns=kb_patterns)
        for c in design.changes:
            print(f"  [{c.get('type')}] {c.get('file')} — {c.get('description', '')[:60]}")
    else:
        # Reuse design from previous patch artifact
        design = _load_previous_design(task)
        print(f"\n[PATCH] Step 2/4: 复用上次设计方案")

    # --- Step 3: Generate (always re-run when entering PATCH) ---
    # Retrieve KB error records for the current symbol
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

    print("\n[PATCH] Step 3/4: 生成代码…（可能需要 20-60 秒）")
    gen_plan = generate_code(task, design, kb_errors=kb_error_dicts)
    for item in gen_plan.get("generated", []):
        print(f"  → {item.get('target_path')} ({item.get('action')})")

    # --- Step 4: Apply ---
    print("\n[PATCH] Step 4/4: 应用到工作区…")
    artifact = apply_patch(task, gen_plan)
    artifact.points = [asdict(p) for p in points]
    artifact.design = asdict(design)

    # Persist
    aid = task.save_artifact("PATCH", artifact, sub_id=task.target.symbol)
    task.artifacts.patch_ids.append(aid)

    if artifact.success:
        for p in artifact.applied_paths:
            print(f"  ✓ {p}")
    else:
        print(f"  ✗ apply failed: {artifact.error}")

    task.current_state = TaskState.BUILD
    return task
