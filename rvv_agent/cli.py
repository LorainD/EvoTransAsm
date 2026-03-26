from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .agent.chat import run_chat
from .core.config import load_config
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


def cmd_migrate(args: argparse.Namespace) -> int:
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "plan":
        return cmd_plan(args)
    if args.cmd == "migrate":
        return cmd_migrate(args)
    if args.cmd == "chat":
        return cmd_chat(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
