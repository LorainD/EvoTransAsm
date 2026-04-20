"""core.task — TaskContext thin manifest + stage artifact definitions.

All data types that flow through the state-machine pipeline are defined here.
Each pipeline stage reads its inputs from previously-persisted artifacts and
writes its outputs as a new artifact JSON under ``run_dir/state/``.

TaskContext itself is a thin runtime manifest. Business lifecycle fields live in
MigrationTask and are embedded as ``task``.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar


T = TypeVar("T")



class TaskState(Enum):
    INTENT = "INTENT"
    SEARCH_FILE = "SEARCH_FILE"          # SEARCH1
    FUNC_DISCOVER = "FUNC_DISCOVER"      # SEARCH2
    BUILD_REFERENCE = "BUILD_REFERENCE"  # SEARCH3
    PLAN = "PLAN"
    ANALYZE = "ANALYZE"
    PATCH = "PATCH"
    BUILD = "BUILD"
    DEBUG = "DEBUG"
    TEST = "TEST"
    KB_UPDATE = "KB_UPDATE"
    TASK_UPDATE = "TASK_UPDATE"
    DONE = "DONE"


class TaskStatus(Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


# ---------------------------------------------------------------------------
# Migration target
# ---------------------------------------------------------------------------

@dataclass
class MigrationTarget:
    """What to migrate: module + symbol, with optional function list."""
    module: str                                         # e.g. "sbrdsp"
    symbol: str                                         # e.g. "sbrdsp.neg_odd_64"
    functions: list[str] = field(default_factory=list)  # filled in FUNC_DISCOVER stage
    current_function: str = ""                          # current function being migrated


@dataclass
class FunctionTask:
    function_id: str = ""
    function_name: str = ""
    status: str = TaskStatus.CREATED.value
    created_at: str = ""
    finished_at: str = ""
    summary: dict = field(default_factory=dict)


@dataclass
class MigrationTask:
    task_id: str = ""
    target: MigrationTarget = field(default_factory=lambda: MigrationTarget("", ""))
    status: TaskStatus = TaskStatus.CREATED
    created_at: str = ""
    finished_at: str = ""
    function_tasks: list[FunctionTask] = field(default_factory=list)
    plan_id: str | None = None
    summary: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Stage artifacts — each persisted independently under state/<STAGE>.json
# ---------------------------------------------------------------------------

@dataclass
class FileSearchArtifact:
    """Output of SEARCH_FILE stage."""
    file_search_id: str = ""
    module: str = ""
    symbol: str = ""
    selected_files: list[str] = field(default_factory=list)
    selected_json: dict = field(default_factory=dict)
    raw_text: str = ""
    llm_used: bool = False
    error: str | None = None


@dataclass
class ReferenceCodeArtifact:
    """Output of BUILD_REFERENCE stage."""
    reference_code_id: str = ""
    file_search_id: str = ""
    function_id: str = ""
    function_name: str = ""
    reference_files: list[str] = field(default_factory=list)
    matched_symbols: list[str] = field(default_factory=list)
    code_context: str = ""
    existing_rvv: list[str] = field(default_factory=list)
    raw_text: str = ""
    llm_used: bool = False
    error: str | None = None


@dataclass
class DiscoveredFunction:
    name: str
    signature: str = ""
    file: str = ""
    line: int = -1
    role: str = ""              # core / dependency
    dependencies: list[str] = field(default_factory=list)
    semantic_hint: str = ""
    migrate: int = 1
    skip_reason: str = ""


@dataclass
class FunctionGroup:
    group_id: str
    functions: list[DiscoveredFunction] = field(default_factory=list)
    group_type: str = ""       # single / dependency / similar / hard
    order: int = 0


@dataclass
class FuncDiscoverArtifact:
    """Output of FUNC_DISCOVER stage — discovered functions for migration."""
    functions: list[DiscoveredFunction] = field(default_factory=list)
    raw_text: str = ""
    llm_used: bool = False


@dataclass
class FunctionAnalysis:
    """Per-function analysis result."""
    function_name: str = ""
    ir: dict = field(default_factory=dict)
    simd_features: dict = field(default_factory=dict)
    c_candidates: list[str] = field(default_factory=list)
    x86_refs: list[str] = field(default_factory=list)
    arm_refs: list[str] = field(default_factory=list)
    kb_match: dict = field(default_factory=dict)
    notes: str = ""
    kb_pattern_ids: list[str] = field(default_factory=list)
    kb_error_classes: list[str] = field(default_factory=list)
    migrate: int = 1
    migrate_reason: str = ""


@dataclass
class AnalysisArtifact:
    """Output of ANALYZE stage — the migration contract."""
    analysis_json: dict = field(default_factory=dict)
    per_function_analysis: dict[str, FunctionAnalysis] = field(default_factory=dict)
    symbol: str = ""
    raw_text: str = ""
    llm_used: bool = False
    error: str | None = None


@dataclass
class PlanArtifact:
    """Output of PLAN stage."""
    plan_id: str = ""
    steps: list[str] = field(default_factory=list)
    function_order: list[str] = field(default_factory=list)
    groups: list[FunctionGroup] = field(default_factory=list)
    acceptance_criteria: dict = field(default_factory=dict)
    refine_history: list[dict] = field(default_factory=list)
    rationale: str = ""
    current_group_idx: int = 0
    completed_groups: list[str] = field(default_factory=list)
    failed_groups: list[str] = field(default_factory=list)


@dataclass
class PatchPoint:
    """A precise anchor for code insertion."""
    file: str = ""
    line: int = -1
    surrounding_hash: str = ""
    rationale: str = ""


@dataclass
class PatchDesign:
    """High-level change plan produced by the design sub-step."""
    changes: list[dict] = field(default_factory=list)
    rationale: str = ""


@dataclass
class PatchArtifact:
    """Output of PATCH stage (one per function)."""
    patch_id: str = ""
    group_id: str = ""
    func: str = ""
    points: list[dict] = field(default_factory=list)
    design: dict = field(default_factory=dict)
    generate_plan: dict = field(default_factory=dict)
    applied_paths: list[str] = field(default_factory=list)
    diffs: list[dict] = field(default_factory=list)
    success: bool = False
    error: str = ""


@dataclass
class BuildArtifact:
    """Output of BUILD stage (one per build run)."""
    run_id: str = ""
    patch_id: str = ""
    cmd: str = ""
    stdout: str = ""
    stderr: str = ""
    exitcode: int = -1
    phase: str = ""          # "configure" | "make"
    artifact_path: str = ""  # e.g. path to checkasm binary
    success: bool = False
    error_type: str = ""
    iteration_no: int = 0


@dataclass
class DebugArtifact:
    """Output of DEBUG stage."""
    run_id: str = ""
    patch_id: str = ""
    build_run_id: str = ""
    test_id: str = ""
    iteration_no: int = 0
    # High-level error tag, decided by the stage where the error surfaced
    # (configure_error | build_error | test_error | patch_error).
    error_class: str = ""
    error_text: str = ""
    root_cause: str = ""
    # Free-form note from LLM or rule-based fallback describing fine-grained
    # classification details (compile/link/runtime/test_mismatch, anchor drift,
    # Makefile issues, inject_error hints, etc.).
    error_note: str = ""
    rollback_target: str = ""   # generate
    fix_actions: list[str] = field(default_factory=list)
    llm_suggestion: str = ""



def _coerce_dataclass(cls: type[T], data: Any) -> T:
    if isinstance(data, cls):
        return data
    if not isinstance(data, dict):
        raise TypeError(f"expected {cls.__name__} dict, got {type(data).__name__}")
    return cls(**data)


def load_func_discover_artifact(data: Any) -> FuncDiscoverArtifact:
    artifact = _coerce_dataclass(FuncDiscoverArtifact, data)
    artifact.functions = [_coerce_dataclass(DiscoveredFunction, item) for item in artifact.functions]
    return artifact


def load_plan_artifact(data: Any) -> PlanArtifact:
    artifact = _coerce_dataclass(PlanArtifact, data)
    artifact.groups = [_coerce_dataclass(FunctionGroup, item) for item in artifact.groups]
    for group in artifact.groups:
        group.functions = [_coerce_dataclass(DiscoveredFunction, item) for item in group.functions]
    return artifact


def load_analysis_artifact(data: Any) -> AnalysisArtifact:
    artifact = _coerce_dataclass(AnalysisArtifact, data)
    if artifact.per_function_analysis:
        artifact.per_function_analysis = {
            fname: _coerce_dataclass(FunctionAnalysis, fdata)
            for fname, fdata in artifact.per_function_analysis.items()
        }
    return artifact


@dataclass
class KBUpdateArtifact:
    """Output of KB_UPDATE stage."""
    new_patterns: list[dict] = field(default_factory=list)
    new_errors: list[dict] = field(default_factory=list)


@dataclass
class TaskUpdateArtifact:
    """Output of TASK_UPDATE stage."""
    task_id: str = ""
    status: str = ""
    finished_at: str = ""
    summary: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Artifact index — pointers into state/ directory
# ---------------------------------------------------------------------------

@dataclass
class ArtifactIndex:
    # Deprecated field kept for backward compatibility with historical task.json.
    retrieval_id: str | None = None
    file_search_id: str | None = None
    reference_code_ids: list[str] = field(default_factory=list)
    analysis_ids: list[str] = field(default_factory=list)
    plan_id: str | None = None
    patch_ids: list[str] = field(default_factory=list)
    build_run_ids: list[str] = field(default_factory=list)
    debug_run_ids: list[str] = field(default_factory=list)
    kb_update_ids: list[str] = field(default_factory=list)
    # DEBUG-only retry budget within a group.
    group_iteration_count: int = 0
    # PATCH pre-build retry budget (generate/apply self-healing only).
    prebuild_generate_retries: int = 0
    active_group_id: str = ""


# ---------------------------------------------------------------------------
# TaskContext — runtime manifest
# ---------------------------------------------------------------------------

@dataclass
class TaskContext:
    """Runtime context threaded through the state machine."""
    task: MigrationTask = field(default_factory=MigrationTask)
    current_state: TaskState = TaskState.INTENT
    current_function_id: str = ""
    run_dir: Path = field(default_factory=lambda: Path("."))
    artifacts: ArtifactIndex = field(default_factory=ArtifactIndex)

    # Accumulated build errors across DEBUG cycles (fed to LLM for context)
    all_build_errors: list[str] = field(default_factory=list)

    # Rollback hint from DEBUG handler: "generate" | ""
    # PATCH handler reads this to skip earlier sub-steps on retry.
    rollback_hint: str = ""

    # Build parallelism (0 = auto-detect via os.cpu_count)
    jobs: int = 0

    # Runtime references — not serialised
    cfg: Any = field(default=None, repr=False)
    ffmpeg_root: Path = field(default_factory=lambda: Path("."))

    # Backward-compatible convenience accessors
    @property
    def task_id(self) -> str:
        return self.task.task_id

    @task_id.setter
    def task_id(self, value: str) -> None:
        self.task.task_id = value

    @property
    def target(self) -> MigrationTarget:
        return self.task.target

    @target.setter
    def target(self, value: MigrationTarget) -> None:
        self.task.target = value

    # ── persistence ──────────────────────────────────────────────────────

    def _state_dir(self) -> Path:
        d = self.run_dir / "state"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save(self) -> None:
        """Persist task manifest to ``run_dir/state/task.json``."""
        data = {
            "task": {
                **asdict(self.task),
                "status": self.task.status.value if isinstance(self.task.status, TaskStatus) else str(self.task.status),
            },
            "current_state": self.current_state.value,
            "current_function_id": self.current_function_id,
            "run_dir": str(self.run_dir),
            "artifacts": asdict(self.artifacts),
            "all_build_errors": self.all_build_errors,
            "rollback_hint": self.rollback_hint,
            "jobs": self.jobs,
            # Legacy fields for compatibility with older tools
            "task_id": self.task.task_id,
            "target": asdict(self.task.target),
        }
        p = self._state_dir() / "task.json"
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, run_dir: Path, cfg: Any = None) -> "TaskContext":
        """Restore from ``run_dir/state/task.json``.

        Supports both new schema and legacy schema used before MigrationTask split.
        """
        p = run_dir / "state" / "task.json"
        data = json.loads(p.read_text(encoding="utf-8"))

        if "task" in data:
            task_data = data["task"]
            target = MigrationTarget(**task_data.get("target", {}))
            function_tasks = [FunctionTask(**ft) for ft in task_data.get("function_tasks", [])]
            status_raw = task_data.get("status", TaskStatus.CREATED.value)
            try:
                status = TaskStatus(status_raw)
            except Exception:
                status = TaskStatus.CREATED
            mtask = MigrationTask(
                task_id=task_data.get("task_id", data.get("task_id", "")),
                target=target,
                status=status,
                created_at=task_data.get("created_at", ""),
                finished_at=task_data.get("finished_at", ""),
                function_tasks=function_tasks,
                plan_id=task_data.get("plan_id"),
                summary=task_data.get("summary", {}),
            )
        else:
            # Legacy schema fallback
            target = MigrationTarget(**data.get("target", {}))
            mtask = MigrationTask(
                task_id=data.get("task_id", ""),
                target=target,
                status=TaskStatus.RUNNING,
            )

        artifacts = ArtifactIndex(**data.get("artifacts", {}))

        state_raw = data.get("current_state", TaskState.INTENT.value)
        if state_raw == "RETRIEVE":
            # Legacy state alias
            state_raw = TaskState.SEARCH_FILE.value
        current_state = TaskState(state_raw)

        return cls(
            task=mtask,
            current_state=current_state,
            current_function_id=data.get("current_function_id", ""),
            run_dir=Path(data.get("run_dir", str(run_dir))),
            artifacts=artifacts,
            all_build_errors=data.get("all_build_errors", []),
            rollback_hint=data.get("rollback_hint", ""),
            jobs=data.get("jobs", 0),
            cfg=cfg,
            ffmpeg_root=cfg.ffmpeg.root.expanduser().resolve() if cfg else Path("."),
        )

    # ── artifact I/O helpers ─────────────────────────────────────────────

    def save_artifact(self, stage: str, artifact: Any, *, sub_id: str = "") -> str:
        """Save a stage artifact and return its ID (filename stem).

        Layout:
            state/<STAGE>.json          — when sub_id is empty
            state/<STAGE>/<sub_id>.json — when sub_id is given (e.g. per-func)
        """
        if sub_id:
            d = self._state_dir() / stage
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"{sub_id}.json"
            artifact_id = f"{stage}/{sub_id}"
        else:
            p = self._state_dir() / f"{stage}.json"
            artifact_id = stage
        obj = asdict(artifact) if hasattr(artifact, "__dataclass_fields__") else artifact
        p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        return artifact_id

    def load_artifact(self, stage: str, *, sub_id: str = "") -> dict:
        """Load a previously-saved artifact JSON."""
        if sub_id:
            p = self._state_dir() / stage / f"{sub_id}.json"
        else:
            p = self._state_dir() / f"{stage}.json"
        return json.loads(p.read_text(encoding="utf-8"))
