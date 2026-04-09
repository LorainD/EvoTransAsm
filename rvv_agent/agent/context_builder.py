"""agent.context_builder - Centralized context construction for LLM stages."""
from __future__ import annotations

from dataclasses import dataclass

from ..core.task import TaskContext
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
                    "implementation": str(p.simd_strategy)[:500],
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
            known_fixes = self.kb.search_errors(error_class=error_class.value, max_results=3)
            ctx["known_fixes"] = [
                {
                    "pattern": f.pattern[:100],
                    "fix_strategy": f.fix_strategy,
                }
                for f in known_fixes
            ]

        return ctx

    @staticmethod
    def _format_code(code: str, max_lines: int) -> str:
        lines = code.splitlines()[:max_lines]
        return "\n".join(f"{i + 1:4d}: {line}" for i, line in enumerate(lines))
