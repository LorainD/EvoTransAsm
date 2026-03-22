"""core.task — TaskContext thin manifest + stage artifact definitions.

All data types that flow through the state-machine pipeline are defined here.
Each pipeline stage reads its inputs from previously-persisted artifacts and
writes its outputs as a new artifact JSON under ``run_dir/state/``.

TaskContext itself is a *thin manifest*: it stores only the task identity,
current state, and an ArtifactIndex that points to the per-stage JSON files.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Task state enum
# ---------------------------------------------------------------------------

class TaskState(Enum):
    INTENT = "INTENT"
    RETRIEVE = "RETRIEVE"
    FUNC_DISCOVER = "FUNC_DISCOVER"
    ANALYZE = "ANALYZE"
    PLAN = "PLAN"
    PATCH = "PATCH"
    BUILD = "BUILD"
    TEST = "TEST"
    KB_UPDATE = "KB_UPDATE"
    DONE = "DONE"
    DEBUG = "DEBUG"


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


# ---------------------------------------------------------------------------
# Stage artifacts — each persisted independently under state/<STAGE>.json
# ---------------------------------------------------------------------------

@dataclass
class RetrievalArtifact:
    """Output of RETRIEVE stage."""
    discovery_json: dict = field(default_factory=dict)
    selected_files: list[str] = field(default_factory=list)
    selected_json: dict = field(default_factory=dict)   # raw LLM selection result
    code_context: str = ""
    existing_rvv: list[str] = field(default_factory=list)
    raw_text: str = ""
    llm_used: bool = False
    error: str | None = None


@dataclass
class FuncDiscoverArtifact:
    """Output of FUNC_DISCOVER stage — discovered functions for migration."""
    functions: list[dict] = field(default_factory=list)  # [{name, signature, file, line}]
    raw_text: str = ""
    llm_used: bool = False


@dataclass
class AnalysisArtifact:
    """Output of ANALYZE stage — the 'migration contract'."""
    analysis_json: dict = field(default_factory=dict)
    raw_text: str = ""
    llm_used: bool = False
    error: str | None = None


@dataclass
class PlanArtifact:
    """Output of PLAN stage."""
    steps: list[str] = field(default_factory=list)
    function_order: list[str] = field(default_factory=list)
    acceptance_criteria: dict = field(default_factory=dict)
    refine_history: list[dict] = field(default_factory=list)


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
    cmd: str = ""
    stdout: str = ""
    stderr: str = ""
    exitcode: int = -1
    phase: str = ""          # "configure" | "make"
    artifact_path: str = ""  # e.g. path to checkasm binary


@dataclass
class DebugArtifact:
    """Output of DEBUG stage."""
    run_id: str = ""
    error_class: str = ""       # compile_error | link_error | runtime_error | test_mismatch
    error_text: str = ""
    rollback_target: str = ""   # "locate" | "design" | "generate"
    fix_actions: list[str] = field(default_factory=list)
    llm_suggestion: str = ""


@dataclass
class KBUpdateArtifact:
    """Output of KB_UPDATE stage."""
    new_patterns: list[dict] = field(default_factory=list)
    new_errors: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Artifact index — pointers into state/ directory
# ---------------------------------------------------------------------------

@dataclass
class ArtifactIndex:
    retrieval_id: str | None = None
    analysis_ids: list[str] = field(default_factory=list)
    plan_id: str | None = None
    patch_ids: list[str] = field(default_factory=list)
    build_run_ids: list[str] = field(default_factory=list)
    debug_run_ids: list[str] = field(default_factory=list)
    kb_update_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# TaskContext — thin manifest
# ---------------------------------------------------------------------------

@dataclass
class TaskContext:
    """Thin manifest threaded through the state machine.

    Runtime-only fields (cfg, ffmpeg_root) are NOT serialised.
    """
    task_id: str = ""
    target: MigrationTarget = field(default_factory=lambda: MigrationTarget("", ""))
    current_state: TaskState = TaskState.INTENT
    run_dir: Path = field(default_factory=lambda: Path("."))
    artifacts: ArtifactIndex = field(default_factory=ArtifactIndex)

    # Accumulated build errors across DEBUG cycles (fed to LLM for context)
    all_build_errors: list[str] = field(default_factory=list)

    # Rollback hint from DEBUG handler: "locate" | "design" | "generate" | ""
    # PATCH handler reads this to skip earlier sub-steps on retry.
    rollback_hint: str = ""

    # Build parallelism (0 = auto-detect via os.cpu_count)
    jobs: int = 0

    # Runtime references — not serialised
    cfg: Any = field(default=None, repr=False)
    ffmpeg_root: Path = field(default_factory=lambda: Path("."))

    # ── persistence ──────────────────────────────────────────────────────

    def _state_dir(self) -> Path:
        d = self.run_dir / "state"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save(self) -> None:
        """Persist task manifest to ``run_dir/state/task.json``."""
        data = {
            "task_id": self.task_id,
            "target": asdict(self.target),
            "current_state": self.current_state.value,
            "run_dir": str(self.run_dir),
            "artifacts": asdict(self.artifacts),
            "all_build_errors": self.all_build_errors,
            "rollback_hint": self.rollback_hint,
            "jobs": self.jobs,
        }
        p = self._state_dir() / "task.json"
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, run_dir: Path, cfg: Any = None) -> "TaskContext":
        """Restore from ``run_dir/state/task.json``."""
        p = run_dir / "state" / "task.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        target = MigrationTarget(**data["target"])
        artifacts = ArtifactIndex(**data.get("artifacts", {}))
        return cls(
            task_id=data["task_id"],
            target=target,
            current_state=TaskState(data["current_state"]),
            run_dir=Path(data["run_dir"]),
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
