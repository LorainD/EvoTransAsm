"""agent.context_builder - Centralized context construction for LLM stages."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..core.task import TaskContext
from ..core.config import is_board_enabled
from ..memory.knowledge_base import KnowledgeBase


@dataclass
class ContextConfig:
    """Context construction configuration."""
    max_code_lines: int = 1000
    max_error_lines: int = 500
    max_kb_patterns: int = 3
    include_kb: bool = True
    include_errors: bool = True
    include_prior_analysis: bool = True


@dataclass
class PatchContext:
    """Context for PATCH stage prompt generation."""
    symbol: str
    analysis_json: dict
    target_files: dict
    repository_knowledge_entry: dict | None = None
    existing_files_map: dict[str, str] | None = None
    build_errors: str | None = None
    debug_suggestions: list[str] | None = None
    previous_code: dict | None = None
    kb_errors: list[dict] | None = None
    validation_feedback: list[str] | None = None


@dataclass
class DebugContext:
    """Context for DEBUG stage prompt generation."""
    error_text: str
    current_patch: dict | None = None
    error_history: list[str] = field(default_factory=list)
    known_fixes: list[dict] = field(default_factory=list)


@dataclass
class PlanPromptContext:
    """Context for PLAN stage prompt generation."""
    reference_files: list[str] = field(default_factory=list)
    existing_rvv_files: list[str] = field(default_factory=list)
    has_existing_rvv: bool = False


class ContextBuilder:
    """Builds stage-specific LLM context payloads from task artifacts."""

    def __init__(self, task: TaskContext, kb: KnowledgeBase | None = None):
        self.task = task
        self.kb = kb

    def build_analyze_context(
        self,
        code_context: str,
        function_name: str,
        config: ContextConfig | None = None,
    ) -> dict:
        config = config or ContextConfig()
        ctx: dict = {}

        ctx["code"] = self._format_code(code_context, config.max_code_lines)

        if config.include_kb and self.kb:
            patterns = self.kb.search_patterns(tags=[function_name], max_results=config.max_kb_patterns)
            ctx["kb_patterns"] = [
                {
                    "pattern_id": p.pattern_id,
                    "description": p.notes,
                    "ir": p.ir,
                    "simd_features": p.simd_features,
                }
                for p in patterns
            ]

        if config.include_errors and self.task.all_build_errors:
            ctx["prior_errors"] = "\n".join(self.task.all_build_errors[-3:])[: config.max_error_lines]

        if config.include_prior_analysis:
            try:
                analysis = self.task.load_artifact("ANALYZE")
                per_func = analysis.get("per_function_analysis", {}) if isinstance(analysis, dict) else {}
                prior = per_func.get(function_name)
                if prior:
                    ctx["prior_analysis"] = prior
            except Exception:
                pass

        return ctx

    def build_patch_analysis_context(self) -> dict:
        """Build a group-scoped analysis view for PATCH stage prompts."""
        try:
            analysis = self.task.load_artifact("ANALYZE")
        except Exception:
            return {}

        analysis_json = analysis.get("analysis_json", {}) if isinstance(analysis, dict) else {}
        per_func = analysis.get("per_function_analysis", {}) if isinstance(analysis, dict) else {}

        group_id = ""
        all_group_functions: list[str] = []
        try:
            plan = self.task.load_artifact("PLAN")
            groups = plan.get("groups", []) if isinstance(plan, dict) else []
            idx = int(plan.get("current_group_idx", 0)) if isinstance(plan, dict) else 0
            if 0 <= idx < len(groups):
                g = groups[idx] if isinstance(groups[idx], dict) else {}
                group_id = str(g.get("group_id", "") or "")
                funcs = g.get("functions", []) if isinstance(g, dict) else []
                all_group_functions = [
                    str(f.get("name", ""))
                    for f in funcs
                    if isinstance(f, dict) and f.get("name")
                ]
        except Exception:
            pass

        if not all_group_functions and isinstance(analysis_json, dict):
            all_group_functions = [str(x) for x in analysis_json.get("all_group_functions", []) if str(x)]
            group_id = group_id or str(analysis_json.get("group_id", "") or "")

        scoped_map: dict[str, dict] = {}
        migratable: list[str] = []
        skipped: dict[str, str] = {}
        for name in all_group_functions:
            f = per_func.get(name)
            if not isinstance(f, dict):
                continue
            scoped_map[name] = f
            migrate = int(f.get("migrate", 1))
            if migrate == 1:
                migratable.append(name)
            else:
                skipped[name] = str(f.get("migrate_reason", "") or f.get("notes", ""))

        if not all_group_functions and isinstance(analysis_json, dict):
            return analysis_json

        return {
            "group_id": group_id,
            "group_functions": migratable,
            "all_group_functions": all_group_functions,
            "migratable_functions": migratable,
            "skipped_functions": skipped,
            "function_analysis": scoped_map,
            "symbol": analysis.get("symbol", "") if isinstance(analysis, dict) else "",
        }

    def build_plan_prompt_context(self) -> PlanPromptContext:
        """Build PLAN prompt context from SEARCH_FILE artifact.

        Returns an ordered reference file list and existing RVV file hints.
        Existing RVV files are tagged with "[existing-rvv]" in reference_files
        for direct prompt consumption.
        """
        try:
            search_art = self.task.load_artifact("SEARCH_FILE")
        except Exception:
            return PlanPromptContext()

        if not isinstance(search_art, dict):
            return PlanPromptContext()

        selected_json = search_art.get("selected_json", {})
        ordered_refs: list[str] = []
        existing_rvv_files: list[str] = []
        seen: set[str] = set()

        def _append_ref(path_text: str) -> None:
            p = str(path_text).strip()
            if not p or p in seen:
                return
            seen.add(p)
            ordered_refs.append(p)

        if isinstance(selected_json, dict):
            for key in ("c", "x86", "arm", "riscv", "headers", "makefiles", "checkasm"):
                v = selected_json.get(key, [])
                if isinstance(v, list):
                    for p in v:
                        _append_ref(str(p))

            rvv = selected_json.get("existing_rvv", [])
            if isinstance(rvv, list):
                for p in rvv:
                    s = str(p).strip()
                    if not s:
                        continue
                    if s not in existing_rvv_files:
                        existing_rvv_files.append(s)
                    _append_ref(f"[existing-rvv] {s}")

        if not ordered_refs:
            selected_files = search_art.get("selected_files", [])
            if isinstance(selected_files, list):
                for p in selected_files:
                    _append_ref(str(p))

        return PlanPromptContext(
            reference_files=ordered_refs,
            existing_rvv_files=existing_rvv_files,
            has_existing_rvv=bool(existing_rvv_files),
        )

    def build_debug_context(
        self,
        error_text: str,
        config: ContextConfig | None = None,
    ) -> dict:
        config = config or ContextConfig()
        ctx = {"current_error": error_text[: config.max_error_lines]}

        if self.task.all_build_errors:
            ctx["error_history"] = "\n---\n".join(self.task.all_build_errors[-5:])[: config.max_error_lines]

        if config.include_kb and self.kb:
            from .debug import classify_error

            error_class = classify_error(error_text)
            known_fixes = self.kb.search_errors_semantic(
                error_text,
                self.task.cfg,
                error_class=error_class.value,
                max_results=3,
            ) if self.task.cfg else self.kb.search_errors(error_class=error_class.value, max_results=3)
            ctx["known_fixes"] = [
                {
                    "pattern": f.pattern[:100],
                    "fix_strategy": f.fix_strategy,
                }
                for f in known_fixes
            ]

        return ctx

    def build_checkasm_debug_context(
        self,
        test_artifact: dict,
        error_text: str,
        config: ContextConfig | None = None,
    ) -> dict:
        """Build context for board/checkasm failure analysis."""
        config = config or ContextConfig()

        ctx: dict = {
            "target": {
                "symbol": self.task.target.symbol,
                "module": self.task.target.module,
            },
            "board": {
                "enabled": bool(is_board_enabled(self.task.cfg)) if self.task.cfg else False,
                "host": str(getattr(self.task.cfg.board, "host", "")) if self.task.cfg else "",
                "port": int(getattr(self.task.cfg.board, "port", 22)) if self.task.cfg else 22,
                "remote_dir": str(getattr(self.task.cfg.board, "remote_dir", "")) if self.task.cfg else "",
            },
            "test_result": {
                "status": str(test_artifact.get("status", "")),
                "phase": str(test_artifact.get("phase", "")),
                "module": str(test_artifact.get("module", "")),
                "run_reason": str(test_artifact.get("run_reason", "")),
                "run_rc": test_artifact.get("run_rc", "n/a"),
                "scp_rc": test_artifact.get("scp_rc", "n/a"),
            },
            "current_error": error_text[: config.max_error_lines],
            "checkasm_stdout": str(test_artifact.get("run_stdout", "") or "")[:8000],
            "checkasm_stderr": str(test_artifact.get("run_stderr", "") or "")[:8000],
        }

        if self.task.all_build_errors:
            ctx["error_history"] = "\n---\n".join(self.task.all_build_errors[-5:])[: config.max_error_lines]

        if config.include_kb and self.kb:
            known = self.kb.search_errors_semantic(
                error_text,
                self.task.cfg,
                error_class="test_mismatch",
                max_results=3,
            ) if self.task.cfg else self.kb.search_errors(error_class="test_mismatch", max_results=3)
            ctx["known_fixes"] = [
                {
                    "pattern": f.pattern[:100],
                    "fix_strategy": f.fix_strategy,
                }
                for f in known
            ]

        return ctx

    def build_patch_context(
        self,
        symbol: str,
        analysis_json: dict,
        target_files: dict,
        config: ContextConfig | None = None,
    ) -> PatchContext:
        """Build complete PATCH stage context from task artifacts.

        Collects analysis, errors, KB patterns, and prior code into a single dataclass.
        """
        config = config or ContextConfig()

        # Collect build errors
        build_errors = None
        if config.include_errors and self.task.all_build_errors:
            build_errors = "\n".join(self.task.all_build_errors[-3:])

        # Collect KB errors
        kb_errors = None
        if config.include_kb and self.kb:
            from .debug import classify_error

            if build_errors:
                error_class = classify_error(build_errors)
                kb_matches = self.kb.search_errors_semantic(
                    build_errors,
                    self.task.cfg,
                    error_class=error_class.value,
                    max_results=config.max_kb_patterns,
                ) if self.task.cfg else self.kb.search_errors(error_class=error_class.value, max_results=config.max_kb_patterns)
                kb_errors = [
                    {
                        "error_class": error_class.value,
                        "pattern": f.pattern[:100],
                        "fix_strategy": f.fix_strategy,
                    }
                    for f in kb_matches
                ]

        # Collect prior patch if exists
        previous_code = None
        try:
            patch = self.task.load_artifact("PATCH")
            if isinstance(patch, dict):
                previous_code = patch
        except Exception:
            pass

        # Collect repository knowledge
        repository_knowledge_entry = None
        try:
            repo_analyze = self.task.load_artifact("REPO_ANALYZE")
            if isinstance(repo_analyze, dict):
                repository_knowledge_entry = repo_analyze
        except Exception:
            pass

        return PatchContext(
            symbol=symbol,
            analysis_json=analysis_json,
            target_files=target_files,
            repository_knowledge_entry=repository_knowledge_entry,
            build_errors=build_errors,
            kb_errors=kb_errors,
            previous_code=previous_code,
        )

    def build_debug_context_full(
        self,
        error_text: str,
        current_patch: dict | None = None,
        config: ContextConfig | None = None,
    ) -> DebugContext:
        """Build complete DEBUG stage context as dataclass.

        Collects error history and known fixes from KB.
        """
        config = config or ContextConfig()

        # Collect error history
        error_history = []
        if self.task.all_build_errors:
            error_history = self.task.all_build_errors[-5:]

        # Collect known fixes from KB
        known_fixes = []
        if config.include_kb and self.kb:
            from .debug import classify_error

            error_class = classify_error(error_text)
            kb_matches = self.kb.search_errors_semantic(
                error_text,
                self.task.cfg,
                error_class=error_class.value,
                max_results=3,
            ) if self.task.cfg else self.kb.search_errors(error_class=error_class.value, max_results=3)
            known_fixes = [
                {
                    "pattern": f.pattern[:100],
                    "fix_strategy": f.fix_strategy,
                }
                for f in kb_matches
            ]

        return DebugContext(
            error_text=error_text,
            current_patch=current_patch,
            error_history=error_history,
            known_fixes=known_fixes,
        )

    @staticmethod
    def _format_code(code: str, max_lines: int) -> str:
        lines = code.splitlines()[:max_lines]
        return "\n".join(f"{i + 1:4d}: {line}" for i, line in enumerate(lines))

    @staticmethod
    def _format_section(title: str, content: str, max_chars: int = 5000) -> str:
        """Format a context section with title and truncation marker."""
        truncated = content[:max_chars]
        if len(content) > max_chars:
            truncated += f"\n... (截断，原长 {len(content)} 字符)"
        return f"\n## {title}\n{truncated}\n"
