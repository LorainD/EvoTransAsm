"""pipeline — Non-interactive migration pipeline (state-machine driven).

Rewritten to share the same StateMachine infrastructure as chat mode.
Four handlers are reused from chat.py (ANALYZE, PATCH, DEBUG, KB_UPDATE);
four are pipeline-specific non-interactive variants (INTENT, SEARCH_FILE, PLAN, BUILD).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .agent.chat import (
    handle_analyze,
    handle_build_reference,
    handle_debug,
    handle_func_discover,
    handle_kb_update,
    handle_patch,
    handle_test,
    handle_task_update,
)
from .agent.plan import fixed_plan
from .agent.report import write_chat_report
from .agent.search import select_references
from .core.llm import get_trajectory_dict, record_trajectory_action, reset_trajectory
from .core.statemachine import StateMachine
from .core.task import (
    BuildArtifact,
    DiscoveredFunction,
    MigrationTarget,
    MigrationTask,
    PlanArtifact,
    FileSearchArtifact,
    TaskContext,
    TaskState,
    TaskStatus,
    load_func_discover_artifact,
)
from .core.config import is_board_enabled
from .core.util import (
    ensure_dir,
    extract_build_errors,
    fmt_argv,
    has_real_rvv_instructions,
    now_id,
    slug,
    write_json,
    write_text,
)
from .memory.knowledge_base import KnowledgeBase
from .tool.exec import configure_argv, make_checkasm_argv, run_configure, run_make_checkasm


@dataclass
class MigrateResult:
    run_dir: Path
    report_path: Path
    exec_failed: bool
    exec_summary: str


# ---------------------------------------------------------------------------
# Pipeline-specific handlers (non-interactive)
# ---------------------------------------------------------------------------

def _handle_intent_pipeline(task: TaskContext) -> TaskContext:
    """INTENT: validate symbol, persist, advance."""
    symbol = task.target.symbol
    if not symbol:
        raise ValueError("pipeline mode requires a symbol argument")
    print(f"\n[pipeline] 迁移目标: {symbol} (module: {task.target.module})")
    task.save_artifact("INTENT", {
        "module": task.target.module,
        "symbol": symbol,
        "functions": task.target.functions,
    })
    record_trajectory_action("intent", f"Target confirmed: {symbol}")
    task.current_state = TaskState.SEARCH_FILE
    return task


def _handle_retrieve_pipeline(task: TaskContext) -> TaskContext:
    """SEARCH_FILE: search + select references, no user interaction."""
    ffmpeg_root = task.ffmpeg_root
    symbol = task.target.symbol
    # select_references 内部会自动执行 symbol+module 合并检索
    file_search = select_references(task.cfg, ffmpeg_root, symbol)

    write_text(task.run_dir / "retrieval_raw.txt", file_search.raw_text + "\n")
    selected = file_search.selected_json

    def _list(key: str) -> list[str]:
        v = selected.get(key, [])
        return [str(x) for x in v] if isinstance(v, list) else []

    selected_files: list[str] = []
    for k in ("c", "x86", "arm", "riscv", "headers", "makefiles", "checkasm"):
        selected_files.extend(_list(k))
    selected_files = list(dict.fromkeys(selected_files))

    for r in _list("existing_rvv"):
        if r not in selected_files:
            selected_files.append(r)

    print(f"[pipeline] 参考文件: {len(selected_files)} 个")
    record_trajectory_action(
        "select_refs",
        f"Reference files selected ({len(selected_files)} files)",
        detail="\n".join(selected_files),
        event_type="human_output",
    )

    file_search.selected_files = selected_files
    aid = task.save_artifact("SEARCH_FILE", file_search)
    task.artifacts.file_search_id = aid
    task.current_state = TaskState.FUNC_DISCOVER
    return task


def _handle_plan_pipeline(task: TaskContext) -> TaskContext:
    """PLAN: use fixed_plan and synchronize function_order from FUNC_DISCOVER."""
    symbol = task.target.symbol

    discovered_functions: list[DiscoveredFunction] = []
    try:
        func_discover = load_func_discover_artifact(task.load_artifact("FUNC_DISCOVER"))
        discovered_functions = func_discover.functions
    except Exception:
        discovered_functions = []

    if not discovered_functions:
        fallback_names = task.target.functions or [symbol]
        discovered_functions = [DiscoveredFunction(name=name, role="core") for name in fallback_names if name]

    plan = fixed_plan(symbol, discovered_functions)
    print(f"[pipeline] Plan: {len(plan.steps)} steps / {len(plan.groups)} groups")

    artifact = plan
    artifact.acceptance_criteria = artifact.acceptance_criteria or {"build_ok": True, "functionally_valid": True}
    aid = task.save_artifact("PLAN", artifact)
    task.artifacts.plan_id = aid
    task.task.plan_id = aid
    record_trajectory_action("plan", f"Fixed plan for {symbol} ({len(discovered_functions)} funcs)")
    task.current_state = TaskState.ANALYZE
    return task


def _handle_build_pipeline(task: TaskContext) -> TaskContext:
    """BUILD: configure + make checkasm, no user prompts."""
    if not task.cfg.human.exec_ok:
        print("[pipeline] 跳过构建（--exec 未指定）")
        task.current_state = TaskState.TASK_UPDATE
        return task

    ffmpeg_root = task.ffmpeg_root
    build_dir = ffmpeg_root / task.cfg.ffmpeg.build_dir
    jobs = task.jobs if task.jobs > 0 else max(1, os.cpu_count() or 1)
    patch_id = task.artifacts.patch_ids[-1] if task.artifacts.patch_ids else ""
    iteration_no = len(task.artifacts.build_run_ids) + 1
    ensure_dir(build_dir)

    # --- configure ---
    print(f"\n[pipeline] configure…")
    cfg_result = run_configure(task.cfg, ffmpeg_root, build_dir)

    build_artifact = BuildArtifact(
        run_id=now_id(),
        patch_id=patch_id,
        cmd=fmt_argv(configure_argv(task.cfg, ffmpeg_root)),
        stdout=cfg_result.stdout,
        stderr=cfg_result.stderr,
        exitcode=cfg_result.returncode,
        phase="configure",
        success=cfg_result.returncode == 0,
        error_type="configure_error" if cfg_result.returncode != 0 else "",
        iteration_no=iteration_no,
    )

    if cfg_result.returncode != 0:
        print(f"[pipeline] configure 失败 (rc={cfg_result.returncode})")
        error_extract = extract_build_errors(cfg_result.stdout + cfg_result.stderr)
        write_text(task.run_dir / "build_log.txt",
                   f"=== configure (rc={cfg_result.returncode}) ===\n{error_extract}\n")
        task.save_artifact("BUILD", build_artifact, sub_id=build_artifact.run_id)
        task.artifacts.build_run_ids.append(build_artifact.run_id)
        task.current_state = TaskState.DEBUG
        return task

    # --- make checkasm ---
    print(f"[pipeline] make checkasm (jobs={jobs})…")
    make_result = run_make_checkasm(task.cfg, build_dir, jobs)

    build_artifact = BuildArtifact(
        run_id=now_id(),
        patch_id=patch_id,
        cmd=fmt_argv(make_checkasm_argv(jobs=jobs)),
        stdout=make_result.stdout,
        stderr=make_result.stderr,
        exitcode=make_result.returncode,
        phase="make",
        artifact_path=str(build_dir / "tests" / "checkasm" / "checkasm"),
        success=make_result.returncode == 0,
        error_type="build_error" if make_result.returncode != 0 else "",
        iteration_no=iteration_no,
    )
    task.save_artifact("BUILD", build_artifact, sub_id=build_artifact.run_id)
    task.artifacts.build_run_ids.append(build_artifact.run_id)

    if make_result.returncode == 0:
        has_rvv = False
        if task.artifacts.patch_ids:
            try:
                sub = task.artifacts.patch_ids[-1].split("/")[-1]
                latest_patch = task.load_artifact("PATCH", sub_id=sub)
                has_rvv = has_real_rvv_instructions(latest_patch.get("generate_plan", {}))
            except Exception:
                pass

        if not has_rvv:
            print("[pipeline][WARN] 构建通过但未检测到有效 RVV 指令，可能是空壳实现")
            record_trajectory_action("build_warn", "Build passed but no real RVV instructions detected")
            build_artifact.error_type = "rvv_missing"
            task.save_artifact("BUILD", build_artifact, sub_id=build_artifact.run_id)
            task.current_state = TaskState.DEBUG
        else:
            print("[pipeline] 构建成功 ✓")
            record_trajectory_action("build_success", "Build succeeded")
            task.current_state = TaskState.TEST
    else:
        print(f"[pipeline] 构建失败 (rc={make_result.returncode})")
        error_extract = extract_build_errors(make_result.stdout + make_result.stderr)
        build_log_path = task.run_dir / "build_log.txt"
        existing_log = ""
        if build_log_path.exists():
            existing_log = build_log_path.read_text(encoding="utf-8", errors="replace")
        write_text(build_log_path,
                   existing_log + f"\n=== make (rc={make_result.returncode}) ===\n{error_extract}\n")
        record_trajectory_action("build_fail", "Build failed")
        task.current_state = TaskState.DEBUG

    return task


# ---------------------------------------------------------------------------
# Helper: derive MigrateResult from TaskContext
# ---------------------------------------------------------------------------

def _derive_exec_result(task: TaskContext) -> tuple[bool, str]:
    """Read BuildArtifacts to produce exec_failed + exec_summary."""
    if not task.artifacts.build_run_ids:
        return False, "skipped_build: exec not requested or no valid generation"

    configure_rc: int | str = "skipped"
    make_rc: int | str = "skipped"
    for bid in task.artifacts.build_run_ids:
        try:
            b = task.load_artifact("BUILD", sub_id=bid)
        except Exception:
            continue
        if b.get("phase") == "configure":
            configure_rc = b.get("exitcode", -1)
        elif b.get("phase") == "make":
            make_rc = b.get("exitcode", -1)

    last_build = task.load_artifact("BUILD", sub_id=task.artifacts.build_run_ids[-1])
    build_failed = last_build.get("exitcode", -1) != 0 or str(last_build.get("error_type", "") or "") == "rvv_missing"

    test_status = "skipped"
    test_rc: int | str = "skipped"
    if is_board_enabled(task.cfg):
        try:
            test_artifact = task.load_artifact("TEST")
            test_status = str(test_artifact.get("status", "") or "missing")
            run_rc = test_artifact.get("run_rc", None)
            if isinstance(run_rc, int):
                test_rc = run_rc
        except Exception:
            test_status = "missing"

    exec_failed = build_failed or (is_board_enabled(task.cfg) and test_status != "success")
    exec_summary = f"configure_rc={configure_rc} checkasm_build_rc={make_rc} test_status={test_status} test_rc={test_rc}"
    return exec_failed, exec_summary


def _quick_build_health_check(task: TaskContext) -> tuple[bool, str]:
    """Best-effort quick build check after rollback."""
    ffmpeg_root = task.ffmpeg_root
    build_dir = ffmpeg_root / task.cfg.ffmpeg.build_dir
    ensure_dir(build_dir)

    # Keep this check lightweight but still meaningful.
    jobs = max(1, min(task.jobs if task.jobs > 0 else max(1, os.cpu_count() or 1), 2))

    cfg_result = run_configure(task.cfg, ffmpeg_root, build_dir)
    if cfg_result.returncode != 0:
        return False, f"rollback_health: configure_rc={cfg_result.returncode}"

    make_result = run_make_checkasm(task.cfg, build_dir, jobs)
    ok = make_result.returncode == 0
    return ok, f"rollback_health: configure_rc=0 checkasm_build_rc={make_result.returncode}"


def _rollback_on_failure(task: TaskContext, reason: str) -> None:
    """Rollback all apply snapshots and run a quick health check."""
    if not task.artifacts.patch_ids:
        return

    from .agent.patch import rollback_all_applies

    print(f"[pipeline] 失败兜底回滚触发: {reason}")
    rollback_all_applies(task)

    try:
        ok, summary = _quick_build_health_check(task)
        if ok:
            print(f"[pipeline] 回滚后健康检查通过: {summary}")
        else:
            print(f"[pipeline][WARN] 回滚后健康检查失败: {summary}")
    except Exception as e:
        print(f"[pipeline][WARN] 回滚后健康检查异常: {e}")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_migrate(
    cfg: "AppConfig",
    *,
    symbol: str,
    ffmpeg_root: Path,
    do_exec: bool,
    jobs: int,
    apply: bool,
    run_dir: Path | None = None,
    task_id: str | None = None,
) -> MigrateResult:
    """Non-interactive migration pipeline (state-machine driven).

    Signature and return type are unchanged from the original implementation
    to maintain CLI compatibility.
    """
    from .core.config import AppConfig  # deferred to avoid circular

    # 1. Pre-configure HumanConfig for non-interactive mode
    cfg.human.apply_ok = apply
    cfg.human.exec_ok = do_exec
    if is_board_enabled(cfg):
        if cfg.human.scp_ok is None:
            cfg.human.scp_ok = True
        if cfg.human.run_onboard_ok is None:
            cfg.human.run_onboard_ok = True

    # 2. Build MigrationTarget
    module = symbol.split(".")[0] if "." in symbol else symbol
    target = MigrationTarget(module=module, symbol=symbol)

    # 3. Create TaskContext
    resolved_task_id = task_id or now_id()
    if run_dir is None:
        run_dir = Path("runs") / f"{resolved_task_id}_{slug(symbol)}"
    ensure_dir(run_dir)

    task = TaskContext(
        task=MigrationTask(
            task_id=resolved_task_id,
            target=target,
            status=TaskStatus.RUNNING,
        ),
        current_state=TaskState.INTENT,
        run_dir=run_dir,
        cfg=cfg,
        ffmpeg_root=ffmpeg_root.expanduser().resolve(),
        jobs=jobs,
    )

    # 4. Load KB
    kb = KnowledgeBase(Path("knowledge_base.json"))
    kb.load()

    # 5. Reset trajectory
    reset_trajectory()

    # 6. Register handlers
    handlers = {
        TaskState.INTENT:          _handle_intent_pipeline,
        TaskState.SEARCH_FILE:     _handle_retrieve_pipeline,
        TaskState.FUNC_DISCOVER:   handle_func_discover,
        TaskState.BUILD_REFERENCE: handle_build_reference,
        TaskState.ANALYZE:         handle_analyze,
        TaskState.PLAN:      _handle_plan_pipeline,
        TaskState.PATCH:     lambda t: handle_patch(t, kb),
        TaskState.BUILD:     _handle_build_pipeline,
        TaskState.DEBUG:     lambda t: handle_debug(t, kb),
        TaskState.TEST:      handle_test,
        TaskState.KB_UPDATE: lambda t: handle_kb_update(t, kb),
        TaskState.TASK_UPDATE: handle_task_update,
    }

    # 7. Run state machine
    sm = StateMachine(task, handlers)
    rollback_done = False

    def _guarded_rollback(reason: str) -> None:
        nonlocal rollback_done
        if rollback_done:
            return
        _rollback_on_failure(task, reason)
        rollback_done = True

    try:
        task = sm.run()
    except KeyboardInterrupt:
        _guarded_rollback("keyboard_interrupt")
        raise
    except Exception:
        _guarded_rollback("state_machine_exception")
        raise

    # 8. Generate report
    report_path = write_chat_report(task)

    # 9. Save trajectory + KB
    traj = get_trajectory_dict(model=cfg.llm.model, endpoint=cfg.llm.base_url)
    write_json(run_dir / "trajectory.json", traj)
    kb.save()

    # 10. Derive result
    exec_failed, exec_summary = _derive_exec_result(task)

    if task.task.status != TaskStatus.SUCCEEDED:
        _guarded_rollback("final_status_failed")

    return MigrateResult(
        run_dir=run_dir,
        report_path=report_path,
        exec_failed=exec_failed,
        exec_summary=exec_summary,
    )
