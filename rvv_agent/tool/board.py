from __future__ import annotations

import shlex
import shutil
import re
import json
from dataclasses import dataclass
from pathlib import Path

from ..core.config import AppConfig
from ..core.util import CmdResult, now_id, run_cmd


@dataclass(frozen=True)
class BoardCommands:
    remote_work_dir: str
    test_name: str
    test_source: str
    ssh_prepare_argv: list[str]
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


def _module_hint_name(module: str) -> str:
    """Normalize module input to a likely checkasm --test token."""
    s = (module or "").strip().strip("/")
    if not s:
        return ""
    if "/" in s:
        s = s.split("/")[-1]
    if s.endswith(".c"):
        s = s[:-2]
    return s


def _parse_checkasm_name_from_content(content: str) -> str:
    """Extract test name from checkasm source content.

    Example:
      void checkasm_check_synth_filter(void) -> synth_filter
    """
    m = re.search(r"\bcheckasm_check_([A-Za-z0-9_]+)\s*\(", content)
    return m.group(1) if m else ""


# 匹配 tests[] 数组中的条目，如 { "opusdsp", checkasm_check_opusdsp },
_TESTS_ENTRY_RE = re.compile(
    r'\{\s*"([A-Za-z0-9_]+)"\s*,\s*checkasm_check_([A-Za-z0-9_]+)\s*\}'
)


def _match_test_name_in_main(content: str, module_hint: str) -> str:
    """从 checkasm.c 主入口的 tests[] 数组中按 module 名匹配 --test 名。

    解析形如 { "opusdsp", checkasm_check_opusdsp } 的条目，
    找到与 module_hint 最接近的 test name。
    """
    if not module_hint:
        return ""
    hint = module_hint.lower().replace("_", "")
    entries = _TESTS_ENTRY_RE.findall(content)  # list of (name, func_suffix)
    if not entries:
        return ""

    # 精确匹配
    for name, _ in entries:
        if name.lower() == module_hint.lower():
            return name

    # 去下划线后精确匹配
    for name, _ in entries:
        if name.lower().replace("_", "") == hint:
            return name

    # 子串包含匹配（双向）
    for name, _ in entries:
        n = name.lower().replace("_", "")
        if hint in n or n in hint:
            return name

    return ""


def resolve_checkasm_test_name(
    ffmpeg_root: Path,
    checkasm_file_paths: list[str] | None,
    fallback_module: str,
) -> tuple[str, str]:
    """Resolve real checkasm --test name from checkasm source files.

    Returns:
      (test_name, source_path)
      source_path 为 "fallback" 表示未解析成功而使用兜底值。
    """
    fallback = _module_hint_name(fallback_module)
    candidates = [str(p).strip() for p in (checkasm_file_paths or []) if str(p).strip()]
    if not candidates:
        return fallback, "fallback"

    # Prefer paths with tests/checkasm and whose filename is close to module hint.
    hint = fallback.lower()

    def _score(rel: str) -> tuple[int, int]:
        low = rel.lower().replace("\\", "/")
        in_checkasm = 1 if "tests/checkasm" in low else 0
        name_match = 1 if hint and hint in Path(low).name else 0
        return (in_checkasm, name_match)

    ordered = sorted(candidates, key=_score, reverse=True)
    for rel in ordered:
        full = ffmpeg_root / rel
        if not full.exists() or not full.is_file():
            continue
        try:
            content = full.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        # 如果是 checkasm.c 主入口，用 tests[] 数组按 module 名匹配
        if Path(rel).name == "checkasm.c":
            matched = _match_test_name_in_main(content, fallback)
            if matched:
                return matched, rel
            # 主入口没匹配到，跳过，不要用 re.search 取第一个
            continue

        name = _parse_checkasm_name_from_content(content)
        if name:
            return name, rel

    return fallback, "fallback"


def build_board_commands(
    cfg: AppConfig,
    ffmpeg_root: Path,
    module: str,
    checkasm_file_paths: list[str] | None = None,
) -> BoardCommands:
    local_bin = local_checkasm_path(ffmpeg_root, str(cfg.ffmpeg.build_dir))
    ssh_target = f"{cfg.board.user}@{cfg.board.host}"

    base_remote_dir = cfg.board.remote_dir.strip().rstrip("/") or "workplace"
    remote_work_dir = f"{base_remote_dir}/{now_id()}"
    remote_work_dir_quoted = shlex.quote(remote_work_dir)
    remote = f"{cfg.board.user}@{cfg.board.host}:{remote_work_dir}/checkasm"

    ssh_prepare_argv = [
        "ssh", "-p", str(cfg.board.port), ssh_target,
        f"mkdir -p {remote_work_dir_quoted}",
    ]
    scp_argv = [
        "scp", "-P", str(cfg.board.port), str(local_bin), remote,
    ]
    test_name, test_source = resolve_checkasm_test_name(ffmpeg_root, checkasm_file_paths, module)
    test_arg = f" --test={shlex.quote(test_name)}" if test_name else ""
    ssh_run_argv = [
        "ssh", "-p", str(cfg.board.port), ssh_target,
        f"cd {remote_work_dir_quoted} && chmod +x checkasm && ./checkasm{test_arg}",
    ]
    return BoardCommands(
        remote_work_dir=remote_work_dir,
        test_name=test_name,
        test_source=test_source,
        ssh_prepare_argv=ssh_prepare_argv,
        scp_argv=scp_argv,
        ssh_run_argv=ssh_run_argv,
    )


def run_with_sshpass(argv: list[str], password: str, *, timeout_sec: int | None = None) -> CmdResult:
    sshpass = shutil.which("sshpass")
    if not sshpass or not password:
        return run_cmd(argv, timeout_sec=timeout_sec)
    return run_cmd([sshpass, "-p", password, *argv], timeout_sec=timeout_sec)


def analyze_checkasm_output(
    stdout: str,
    stderr: str,
    returncode: int,
    *,
    expected_symbol: str = "",
) -> CheckasmResult:
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

    # 关键防线："all 0 tests passed" 并不代表真正跑到了目标测试。
    if re.search(r"checkasm:\s*all\s*0\s*tests\s*passed", combined) or "no tests to perform" in combined:
        return CheckasmResult(success=False, reason="zero_tests_executed")

    # 正向语义：必须看到明确的非零测试通过，或常见 OK 标记。
    passed = False
    if re.search(r"checkasm:\s*all\s*[1-9][0-9]*\s*tests\s*passed", combined):
        passed = True
    elif re.search(r"\bcheckasm\b.*\bok\b", combined):
        passed = True

    if not passed:
        # 非零 rc 之外的未知输出也按失败处理，避免假阳性。
        return CheckasmResult(success=False, reason="unrecognized_test_output")

    # ── 检查目标函数是否真的被测试到 ──
    if expected_symbol:
        # 从 symbol 中提取函数名部分，如 "sbrdsp.neg_odd_64" -> "neg_odd_64"
        func_name = expected_symbol.split(".")[-1] if "." in expected_symbol else expected_symbol
        func_name_lower = func_name.lower().strip()
        if func_name_lower and func_name_lower not in combined:
            return CheckasmResult(
                success=False,
                reason=f"target_function_not_tested:{func_name}",
            )

    return CheckasmResult(success=True, reason="ok")


def is_infra_failure(reason: str) -> bool:
    """Return True for board connectivity/auth issues instead of checkasm mismatch."""
    if reason == "checkasm_timeout":
        return True
    if reason.startswith("checkasm_nonzero_exit:"):
        suffix = reason.split(":", 1)[1].strip()
        return suffix in {"255", "127"}
    return False


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
