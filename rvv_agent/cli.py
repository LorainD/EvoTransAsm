from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from .agent.chat import run_chat
from .core.config import load_config
from .core.util import ensure_dir, install_print_tee, now_id
from .agent.plan import fixed_plan
from .pipeline import run_migrate


def _resolve_path(p: str | None) -> Path | None:
    if not p:
        return None
    return Path(p).expanduser().resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rvv-agent",
        description="Agent-style CLI for FFmpeg RVV asm migration automation",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to rvv_agent.toml (default: ./rvv_agent.toml)",
    )

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_plan = sub.add_parser("plan", help="Print the fixed migration plan template")
    p_plan.add_argument("symbol", help="Target symbol/function name")

    p_mig = sub.add_parser(
        "migrate",
        help="Run pipeline: search -> LLM analysis -> LLM generate -> (optional) exec",
    )
    p_mig.add_argument("symbol", help="Target symbol/function name")
    p_mig.add_argument(
        "--ffmpeg-root",
        default=None,
        help="FFmpeg root dir (default from config)",
    )
    p_mig.add_argument(
        "--apply",
        action="store_true",
        help="Apply generated files into FFmpeg workspace (default: no)",
    )
    p_mig.add_argument(
        "--exec",
        action="store_true",
        help="Run configure + build checkasm (may take long)",
    )
    p_mig.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=0,
        help="make jobs (default: cpu_count)",
    )

    p_chat = sub.add_parser("chat", help="Interactive chat mode (human-in-the-loop)")
    p_chat.add_argument(
        "--ffmpeg-root",
        default=None,
        help="FFmpeg root dir (default from config)",
    )

    return parser


def cmd_plan(args: argparse.Namespace) -> int:
    plan = fixed_plan(args.symbol)
    for i, step in enumerate(plan.steps, start=1):
        print(f"{i:02d}. {step}")
    return 0


def cmd_migrate(args: argparse.Namespace, session_log: Path) -> int:
    cfg = load_config(_resolve_path(args.config))

    ffmpeg_root = Path(args.ffmpeg_root) if args.ffmpeg_root else cfg.ffmpeg.root
    ffmpeg_root = ffmpeg_root.expanduser().resolve()

    if not ffmpeg_root.exists():
        print(f"error: ffmpeg_root not found: {ffmpeg_root}", file=sys.stderr)
        return 2

    jobs = args.jobs
    if jobs <= 0:
        jobs = max(1, os.cpu_count() or 1)

    result = run_migrate(
        cfg,
        symbol=args.symbol,
        ffmpeg_root=ffmpeg_root,
        do_exec=args.exec,
        jobs=jobs,
        apply=args.apply,
    )

    # Move session_print.txt into the actual run_dir so all artifacts are together
    _move_session_log(session_log, result.run_dir)

    print(f"run_dir: {result.run_dir}")
    print(f"report:  {result.report_path}")
    if result.exec_summary:
        print(result.exec_summary)

    if result.exec_failed:
        return 10
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    cfg = load_config(_resolve_path(args.config))
    if args.ffmpeg_root:
        cfg.ffmpeg.root = Path(args.ffmpeg_root)
    return run_chat(cfg)


def _move_session_log(src: Path, run_dir: Path) -> None:
    """Move session_print.txt from temp location into run_dir."""
    try:
        if src.exists() and run_dir.exists():
            dst = run_dir / "session_print.txt"
            shutil.move(str(src), str(dst))
            # Remove the now-empty temp dir if possible
            try:
                src.parent.rmdir()
            except Exception:
                pass
    except Exception:
        pass


def _force_utf8_stdio() -> None:
    """Best-effort UTF-8 stdio setup across Linux/Windows terminals."""
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    args = build_parser().parse_args(argv)

    # Write session log to a temp location first; migrate will move it into run_dir
    session_log_dir = Path("runs") / f"{now_id()}_{args.cmd}_session"
    ensure_dir(session_log_dir)
    session_log_path = session_log_dir / "session_print.txt"
    restore_print = install_print_tee(session_log_path)

    try:
        print(f"[session] print log: {session_log_path}")
        if args.cmd == "plan":
            return cmd_plan(args)
        if args.cmd == "migrate":
            return cmd_migrate(args, session_log_path)
        if args.cmd == "chat":
            return cmd_chat(args)
        return 1
    finally:
        restore_print()


if __name__ == "__main__":
    raise SystemExit(main())
