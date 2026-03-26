"""core: 基础层 — 配置、工具函数、LLM 客户端、Prompt 模板、状态机。
不依赖 agent/tool/memory 中的任何模块。
"""

from .config import (
    AppConfig, BoardConfig, FfmpegConfig, HumanConfig, LlmConfig, ToolchainConfig, load_config,
)
from .llm import (
    LlmError, LlmMessage, api_key_present, chat_completion,
    get_trajectory_dict, probe_llm, reset_trajectory,
)
from .util import (
    CmdResult, ensure_dir, extract_build_errors, fmt_argv, now_id,
    print_llm_error, print_red, print_yellow,
    run_cmd, run_cmd_stream, slug, write_json, write_text,
)
from .task import (
    ArtifactIndex, TaskContext, TaskState, TaskStatus, MigrationTarget,
    FunctionTask, MigrationTask,
    FileSearchArtifact, ReferenceCodeArtifact, DiscoveredFunction, FunctionGroup,
    FuncDiscoverArtifact, AnalysisArtifact, PlanArtifact,
    PatchPoint, PatchDesign, PatchArtifact,
    BuildArtifact, DebugArtifact, KBUpdateArtifact, TaskUpdateArtifact,
    load_func_discover_artifact, load_plan_artifact,
)
from .statemachine import StateMachine

__all__ = [
    # config
    "AppConfig", "BoardConfig", "FfmpegConfig", "HumanConfig",
    "LlmConfig", "ToolchainConfig", "load_config",
    # llm
    "LlmError", "LlmMessage", "api_key_present", "chat_completion",
    "get_trajectory_dict", "probe_llm", "reset_trajectory",
    # util
    "CmdResult", "ensure_dir", "extract_build_errors", "fmt_argv", "now_id",
    "print_llm_error", "print_red", "print_yellow",
    "run_cmd", "run_cmd_stream", "slug", "write_json", "write_text",
    # task (state machine)
    "ArtifactIndex", "TaskContext", "TaskState", "TaskStatus", "MigrationTarget",
    "FunctionTask", "MigrationTask",
    "FileSearchArtifact", "ReferenceCodeArtifact", "DiscoveredFunction", "FunctionGroup",
    "FuncDiscoverArtifact", "AnalysisArtifact", "PlanArtifact",
    "PatchPoint", "PatchDesign", "PatchArtifact",
    "BuildArtifact", "DebugArtifact", "KBUpdateArtifact", "TaskUpdateArtifact",
    "load_func_discover_artifact", "load_plan_artifact",
    # statemachine
    "StateMachine",
]
