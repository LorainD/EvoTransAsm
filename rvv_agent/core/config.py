from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class LlmConfig:
    # base_url: OpenAI-compatible endpoint base, e.g. "https://api.openai.com/v1"
    # The "/chat/completions" path is appended automatically if not present.
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "LLM_API_KEY"
    model: str = "gpt-4o-mini"
    temperature: float = 0.2
    # Optional pricing for trajectory cost estimation ($/1M tokens).
    # If 0, uses built-in defaults for known models.
    cost_per_1m_input_tokens: float = 0.0
    cost_per_1m_output_tokens: float = 0.0


@dataclass
class ToolchainConfig:
    cross_prefix: str = "riscv64-unknown-linux-gnu-"
    arch: str = "riscv64"
    target_os: str = "linux"
    cpu: str = "rv64gcv"
    extra_cflags: str = "-march=rv64gcv -mabi=lp64d -O3"
    extra_ldflags: str = "-static"
    # Prepended to PATH when running configure/make so the cross-compiler is found.
    extra_path: str = ""


@dataclass
class FfmpegConfig:
    root: Path = Path("workplace/FFmpeg")
    build_dir: Path = Path("build")
    configure_path: Path | None = None
    configure_extra_args: list[str] = field(default_factory=list)


@dataclass
class BoardConfig:
    enabled: bool = False
    user: str = ""
    host: str = ""
    port: int = 22
    remote_dir: str = "workplace"


@dataclass
class HumanConfig:
    """Human-in-the-loop policy defaults.

    Values of ``None`` mean "ask at runtime"; ``True``/``False`` skip the prompt.
    Can be pre-set in ``[human]`` section of rvv_agent.toml.
    """
    apply_ok: bool | None = None
    exec_ok: bool | None = None
    scp_ok: bool | None = None
    scp_password: str = ""
    run_onboard_ok: bool | None = None


@dataclass
class FeaturesConfig:
    """Feature toggles for experimental behaviours.

    Currently used to switch between the classic PATCH implementation and
    the new tool-use driven PATCH loop.
    """

    enable_tool_use_patch_loop: bool = False


@dataclass
class AppConfig:
    llm: LlmConfig = field(default_factory=LlmConfig)
    toolchain: ToolchainConfig = field(default_factory=ToolchainConfig)
    ffmpeg: FfmpegConfig = field(default_factory=FfmpegConfig)
    board: BoardConfig = field(default_factory=BoardConfig)
    human: HumanConfig = field(default_factory=HumanConfig)
    features: FeaturesConfig = field(default_factory=FeaturesConfig)


def is_board_enabled(cfg: "AppConfig") -> bool:
    """Return whether board testing should be treated as enabled.

    Besides the explicit ``board.enabled`` flag, treat board testing
    as enabled if essential connection fields (``user`` and ``host``)
    are non-empty. This makes the behaviour robust against historical
    configs or partial writes where the boolean flag was not persisted
    correctly but the connection parameters are present.
    """

    b = cfg.board

    # Explicit flag takes precedence when True.
    if getattr(b, "enabled", False):
        return True

    # Fallback: if user/host are configured, assume the user intends
    # to run board tests even if ``enabled`` was left at its default.
    user = str(getattr(b, "user", "")).strip()
    host = str(getattr(b, "host", "")).strip()
    if user and host:
        return True

    return False


def _as_path(v: object) -> Path | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    return Path(s)


def _load_llm_table(base: LlmConfig, table: dict[str, object]) -> LlmConfig:
    cfg = LlmConfig(
        base_url=str(table.get("base_url", base.base_url)).strip().rstrip("/"),
        api_key_env=str(table.get("api_key_env", base.api_key_env)).strip(),
        model=str(table.get("model", base.model)).strip(),
        temperature=float(table.get("temperature", base.temperature)),
        cost_per_1m_input_tokens=float(table.get("cost_per_1m_input_tokens", base.cost_per_1m_input_tokens)),
        cost_per_1m_output_tokens=float(table.get("cost_per_1m_output_tokens", base.cost_per_1m_output_tokens)),
    )
    return cfg


def load_config(path: Path | None) -> AppConfig:
    cfg = AppConfig()
    if path is None:
        path = Path("rvv_agent.toml")

    if path.exists():
        raw = tomllib.loads(path.read_text(encoding="utf-8"))

        llm_table = raw.get("llm", {})
        if isinstance(llm_table, dict) and llm_table:
            cfg.llm = _load_llm_table(LlmConfig(), llm_table)

        tc = raw.get("toolchain", {})
        if isinstance(tc, dict):
            cfg.toolchain.cross_prefix = str(tc.get("cross_prefix", cfg.toolchain.cross_prefix))
            cfg.toolchain.arch = str(tc.get("arch", cfg.toolchain.arch))
            cfg.toolchain.target_os = str(tc.get("target_os", cfg.toolchain.target_os))
            cfg.toolchain.cpu = str(tc.get("cpu", cfg.toolchain.cpu))
            cfg.toolchain.extra_cflags = str(tc.get("extra_cflags", cfg.toolchain.extra_cflags))
            cfg.toolchain.extra_ldflags = str(tc.get("extra_ldflags", cfg.toolchain.extra_ldflags))
            cfg.toolchain.extra_path = str(tc.get("extra_path", cfg.toolchain.extra_path))

        ff = raw.get("ffmpeg", {})
        if isinstance(ff, dict):
            cfg.ffmpeg.root = Path(ff.get("root", str(cfg.ffmpeg.root)))
            cfg.ffmpeg.build_dir = Path(ff.get("build_dir", str(cfg.ffmpeg.build_dir)))
            cfg.ffmpeg.configure_path = _as_path(ff.get("configure_path"))
            cfg.ffmpeg.configure_extra_args = list(ff.get("configure_extra_args", cfg.ffmpeg.configure_extra_args))

        bd = raw.get("board", {})
        if isinstance(bd, dict):
            cfg.board.enabled = bool(bd.get("enabled", cfg.board.enabled))
            cfg.board.user = str(bd.get("user", cfg.board.user))
            cfg.board.host = str(bd.get("host", cfg.board.host))
            cfg.board.port = int(bd.get("port", cfg.board.port))
            cfg.board.remote_dir = str(bd.get("remote_dir", cfg.board.remote_dir))

        hm = raw.get("human", {})
        if isinstance(hm, dict):
            if "apply_ok" in hm:
                cfg.human.apply_ok = bool(hm["apply_ok"])
            if "exec_ok" in hm:
                cfg.human.exec_ok = bool(hm["exec_ok"])
            if "scp_ok" in hm:
                cfg.human.scp_ok = bool(hm["scp_ok"])
            cfg.human.scp_password = str(hm.get("scp_password", cfg.human.scp_password))
            if "run_onboard_ok" in hm:
                cfg.human.run_onboard_ok = bool(hm["run_onboard_ok"])

        feat = raw.get("features", {})
        if isinstance(feat, dict):
            cfg.features.enable_tool_use_patch_loop = bool(
                feat.get("enable_tool_use_patch_loop", cfg.features.enable_tool_use_patch_loop)
            )

    if os.getenv("RVV_AGENT_FFMPEG_ROOT"):
        cfg.ffmpeg.root = Path(os.environ["RVV_AGENT_FFMPEG_ROOT"])

    return cfg
