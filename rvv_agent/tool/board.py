from __future__ import annotations

import shlex
import shutil
import re
import json
from dataclasses import dataclass
from pathlib import Path

from ..core.config import AppConfig
from ..core.util import CmdResult, run_cmd


@dataclass(frozen=True)
class BoardCommands:
    scp_argv: list[str]
    ssh_run_argv: list[str]


@dataclass(frozen=True)
class CheckasmResult:
    success: bool
    reason: str


@dataclass(frozen=True)
class CheckasmLlmAnalysis:
    ok: bool
    error_class: str
    rollback_target: str
    fix_actions: list[str]
    suggestion: str
    raw: str


def local_checkasm_candidates(ffmpeg_root: Path, build_dir_name: str) -> list[Path]:
    return [
        ffmpeg_root / "tests" / "checkasm" / "riscv" / "checkasm",
        ffmpeg_root / build_dir_name / "tests" / "checkasm" / "checkasm",
    ]


def local_checkasm_path(ffmpeg_root: Path, build_dir_name: str) -> Path:
    candidates = local_checkasm_candidates(ffmpeg_root, build_dir_name)
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def build_board_commands(cfg: AppConfig, ffmpeg_root: Path, module: str) -> BoardCommands:
    local_bin = local_checkasm_path(ffmpeg_root, str(cfg.ffmpeg.build_dir))
    remote = f"{cfg.board.user}@{cfg.board.host}:{cfg.board.remote_dir}/checkasm"

    scp_argv = [
        "scp", "-P", str(cfg.board.port), str(local_bin), remote,
    ]
    ssh_target = f"{cfg.board.user}@{cfg.board.host}"
    remote_dir_quoted = shlex.quote(cfg.board.remote_dir)
    module = module.strip()
    test_arg = f" --test={shlex.quote(module)}" if module else ""
    ssh_run_argv = [
        "ssh", "-p", str(cfg.board.port), ssh_target,
        f"cd {remote_dir_quoted} && chmod +x checkasm && ./checkasm{test_arg}",
    ]
    return BoardCommands(scp_argv=scp_argv, ssh_run_argv=ssh_run_argv)


def run_with_sshpass(argv: list[str], password: str, *, timeout_sec: int | None = None) -> CmdResult:
    sshpass = shutil.which("sshpass")
    if not sshpass or not password:
        return run_cmd(argv, timeout_sec=timeout_sec)
    return run_cmd([sshpass, "-p", password, *argv], timeout_sec=timeout_sec)


def analyze_checkasm_output(stdout: str, stderr: str, returncode: int) -> CheckasmResult:
    combined = f"{stdout}\n{stderr}".lower()

    if returncode == 124 or "timed out" in combined:
        return CheckasmResult(success=False, reason="checkasm_timeout")

    if returncode != 0:
        return CheckasmResult(success=False, reason=f"checkasm_nonzero_exit:{returncode}")

    # checkasm 常见失败格式："N failed"，0 failed 视为成功。
    if re.search(r"\b([1-9][0-9]*)\s+failed\b", combined):
        return CheckasmResult(success=False, reason="checkasm_reported_failures")

    # 兜底关键字：避免误判 0 failed。
    if "fail" in combined and "0 failed" not in combined:
        return CheckasmResult(success=False, reason="checkasm_failure_keyword_detected")

    if "mismatch" in combined or "segmentation fault" in combined or "sigsegv" in combined:
        return CheckasmResult(success=False, reason="checkasm_runtime_failure")

    return CheckasmResult(success=True, reason="ok")


def llm_analyze_checkasm_failure(cfg: AppConfig, context: dict) -> CheckasmLlmAnalysis:
    """Call LLM to analyze checkasm failure with structured context."""
    try:
        from ..core.llm import LlmMessage, chat_completion_with_retry
        from ..core.prompts import system_prompt
        from ..core.prompts_patch import checkasm_debug_prompt

        messages = [
            LlmMessage(role="system", content=system_prompt()),
            LlmMessage(role="user", content=checkasm_debug_prompt(context)),
        ]
        raw = chat_completion_with_retry(
            cfg.llm,
            messages,
            max_tokens=900,
            stage="checkasm_debug",
            max_retries=3,
        ).strip()

        s = raw.find("{")
        e = raw.rfind("}")
        if s == -1 or e <= s:
            return CheckasmLlmAnalysis(
                ok=False,
                error_class="test_mismatch",
                rollback_target="generate",
                fix_actions=[],
                suggestion="",
                raw=raw,
            )
        data = json.loads(raw[s:e + 1])
        return CheckasmLlmAnalysis(
            ok=True,
            error_class=str(data.get("error_class", "test_mismatch") or "test_mismatch"),
            rollback_target=str(data.get("rollback_target", "generate") or "generate"),
            fix_actions=[str(x) for x in data.get("fix_actions", []) if str(x).strip()],
            suggestion=str(data.get("suggestion", "") or ""),
            raw=raw,
        )
    except Exception as e:
        return CheckasmLlmAnalysis(
            ok=False,
            error_class="test_mismatch",
            rollback_target="generate",
            fix_actions=[],
            suggestion=f"llm_checkasm_analysis_failed: {e}",
            raw="",
        )
