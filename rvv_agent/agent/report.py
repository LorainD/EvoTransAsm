"""agent.report — 运行报告生成"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from ..core.util import CmdResult, ensure_dir, fmt_argv, write_json, write_text
from ..tool.exec import ExecResult
from ..core.task import AnalysisArtifact, PlanArtifact, load_plan_artifact
from .search import Discovery, group_files

if TYPE_CHECKING:
    from ..core.task import TaskContext


def _cmd_section(title: str, res: CmdResult | None) -> str:
    if res is None:
        return f"## {title}\n\n- (skipped)\n"

    out = res.stdout.strip()
    err = res.stderr.strip()

    parts: list[str] = []
    parts.append(f"## {title}\n")
    parts.append("```\n" + "$ " + fmt_argv(res.argv) + f"\n(rc={res.returncode})\n" + "```\n")
    if out:
        parts.append("### stdout\n\n```\n" + out[:20000] + "\n```\n")
    if err:
        parts.append("### stderr\n\n```\n" + err[:20000] + "\n```\n")
    return "\n".join(parts)


def write_report(
    run_dir: Path,
    *,
    plan: PlanArtifact,
    discovery: Discovery,
    analysis: AnalysisArtifact,
    generation_raw: str,
    materialized: list[Path],
    exec_result: ExecResult,
    interaction: dict | None = None,
    ref_files: list[str] | None = None,
    refine_history: list[dict] | None = None,
) -> Path:
    ensure_dir(run_dir)

    groups = group_files(discovery)

    md: list[str] = []
    md.append("# rvv-agent run report\n")
    md.append("## Symbol\n\n" + f"- {discovery.symbol}\n")

    md.append("## Plan\n\n" + "\n".join(f"{i+1:02d}. {s}" for i, s in enumerate(plan.steps)) + "\n")

    if ref_files:
        md.append("## Reference Files\n\n" + "\n".join(f"- {f}" for f in ref_files) + "\n")

    if refine_history:
        md.append("## Refine History\n")
        for entry in refine_history:
            stage = entry.get("stage", "?")
            feedback = entry.get("feedback", "")
            md.append(f"- [{stage}] {feedback}")
        md.append("")

    if interaction:
        md.append("## Interaction\n\n```json\n" + json.dumps(interaction, ensure_ascii=False, indent=2) + "\n```\n")

    md.append("## Discovery\n")
    for k, v in groups.items():
        md.append(f"### {k}\n")
        md.append("\n".join(f"- {x}" for x in v) if v else "- (none)")
        md.append("")

    md.append("## Matches (first 200)\n")
    for m in discovery.matches[:200]:
        md.append(f"- {m.file}:{m.line}: {m.text}")

    md.append("\n## Analysis JSON\n")
    md.append("```json\n" + json.dumps(analysis.analysis_json, ensure_ascii=False, indent=2) + "\n```\n")
    md.append(f"- llm_used: {analysis.llm_used}\n")
    if analysis.error:
        md.append(f"- error: {analysis.error}\n")

    md.append("## Generation (raw)\n")
    md.append("```\n" + generation_raw[:20000] + "\n```\n")

    md.append("## Materialized\n")
    md.append("\n".join(f"- {p}" for p in materialized) if materialized else "- (none)")
    md.append("")
    md.append(_cmd_section("configure", exec_result.configure))
    md.append(_cmd_section("make checkasm", exec_result.make_checkasm))

    report_path = run_dir / "report.md"
    write_text(report_path, "\n".join(md))

    write_json(run_dir / "discovery.json", {
        "symbol": discovery.symbol,
        "matches": [m.__dict__ for m in discovery.matches],
    })
    write_json(run_dir / "analysis.json", asdict(analysis))

    return report_path


def write_chat_report(task: "TaskContext") -> Path:
    """Generate a report from state-machine artifacts (chat mode).

    Reads persisted artifacts from ``task.run_dir/state/`` and produces
    a Markdown report at ``task.run_dir/report.md``.
    """
    run_dir = task.run_dir
    ensure_dir(run_dir)
    symbol = task.target.symbol

    md: list[str] = []
    md.append(f"# rvv-agent chat report\n")
    md.append(f"## Symbol\n\n- {symbol}\n- task_id: {task.task_id}\n")

    # Plan
    try:
        plan = load_plan_artifact(task.load_artifact("PLAN"))
        steps = plan.steps
        md.append("## Plan\n\n" + "\n".join(f"{i+1:02d}. {s}" for i, s in enumerate(steps)) + "\n")
        if plan.groups:
            md.append("### Function Groups\n")
            for group in sorted(plan.groups, key=lambda g: g.order):
                names = ", ".join(f.name for f in group.functions if f.name)
                md.append(f"- [{group.order}] {group.group_id} ({group.group_type or 'single'}): {names}")
            md.append("")
        if plan.rationale:
            md.append("### Rationale\n")
            md.append(plan.rationale)
            md.append("")
        if plan.refine_history:
            md.append("### Refine History\n")
            for entry in plan.refine_history:
                md.append(f"- [{entry.get('stage', '?')}] {entry.get('feedback', '')}")
            md.append("")
    except Exception:
        md.append("## Plan\n\n- (not available)\n")

    # Reference files
    try:
        file_search = task.load_artifact("SEARCH_FILE")
        files = file_search.get("selected_files", [])
        if files:
            md.append("## Reference Files\n\n" + "\n".join(f"- {f}" for f in files) + "\n")
    except Exception:
        pass

    # Analysis
    try:
        analysis = task.load_artifact("ANALYZE")
        md.append("## Analysis\n\n```json\n"
                   + json.dumps(analysis.get("analysis_json", {}), ensure_ascii=False, indent=2)
                   + "\n```\n")
        analysis_json = analysis.get("analysis_json", {}) if isinstance(analysis, dict) else {}
        if isinstance(analysis_json, dict):
            groups = analysis_json.get("groups", {})
            current_group_id = analysis_json.get("current_group_id", "")
            if isinstance(groups, dict) and groups:
                md.append(f"- groups_analyzed: {len(groups)}")
            if current_group_id:
                md.append(f"- current_group_id: {current_group_id}")
            md.append("")
    except Exception:
        pass

    # Patch
    for pid in task.artifacts.patch_ids:
        sub = pid.split("/")[-1] if "/" in pid else pid
        try:
            patch = task.load_artifact("PATCH", sub_id=sub)
            md.append(f"## Patch: {sub}\n")
            for p in patch.get("applied_paths", []):
                md.append(f"- {p}")
            md.append("")
        except Exception:
            pass

    # Build
    for bid in task.artifacts.build_run_ids:
        try:
            build = task.load_artifact("BUILD", sub_id=bid)
            phase = build.get("phase", "?")
            rc = build.get("exitcode", -1)
            md.append(f"## Build ({phase}, rc={rc})\n")
            cmd = build.get("cmd", "")
            if cmd:
                md.append(f"```\n$ {cmd}\n```\n")
            stderr = build.get("stderr", "").strip()
            if stderr and rc != 0:
                md.append(f"### stderr (tail)\n\n```\n{stderr[-3000:]}\n```\n")
        except Exception:
            pass

    # Debug
    for did in task.artifacts.debug_run_ids:
        try:
            dbg = task.load_artifact("DEBUG", sub_id=did)
            md.append(f"## Debug: {dbg.get('error_class', '?')}\n")
            md.append(f"- rollback_target: {dbg.get('rollback_target', '?')}")
            for a in dbg.get("fix_actions", []):
                md.append(f"- {a}")
            md.append("")
        except Exception:
            pass

    # Test
    try:
        test = task.load_artifact("TEST")
        status = str(test.get("status", "") or "unknown")
        phase = str(test.get("phase", "") or "")
        reason = str(test.get("run_reason", "") or test.get("reason", ""))
        md.append(f"## Test (status={status})\n")
        if phase:
            md.append(f"- phase: {phase}")
        if reason:
            md.append(f"- reason: {reason}")
        md.append(f"- remote_dir: {test.get('remote_dir', '')}")
        md.append(f"- prepare_rc: {test.get('prepare_rc', 'n/a')}")
        md.append(f"- scp_rc: {test.get('scp_rc', 'n/a')}")
        md.append(f"- run_rc: {test.get('run_rc', 'n/a')}")

        prepare_out = str(test.get("prepare_stdout", "") or "")
        prepare_err = str(test.get("prepare_stderr", "") or "")
        scp_out = str(test.get("scp_stdout", "") or "")
        scp_err = str(test.get("scp_stderr", "") or "")
        run_out = str(test.get("run_stdout", "") or "")
        run_err = str(test.get("run_stderr", "") or "")

        if prepare_out:
            md.append(f"\n### prepare stdout (tail)\n\n```\n{prepare_out[-3000:]}\n```\n")
        if prepare_err:
            md.append(f"\n### prepare stderr (tail)\n\n```\n{prepare_err[-3000:]}\n```\n")
        if scp_out:
            md.append(f"\n### scp stdout (tail)\n\n```\n{scp_out[-3000:]}\n```\n")
        if scp_err:
            md.append(f"\n### scp stderr (tail)\n\n```\n{scp_err[-3000:]}\n```\n")
        if run_out:
            md.append(f"\n### run stdout (tail)\n\n```\n{run_out[-3000:]}\n```\n")
        if run_err:
            md.append(f"\n### run stderr (tail)\n\n```\n{run_err[-3000:]}\n```\n")

        md.append("")
    except Exception:
        pass

    # Final status
    build_ok = False
    if task.artifacts.build_run_ids:
        try:
            last = task.load_artifact("BUILD", sub_id=task.artifacts.build_run_ids[-1])
            build_ok = last.get("exitcode", -1) == 0 and str(last.get("error_type", "") or "") != "rvv_missing"
        except Exception:
            pass
    md.append(f"## Result\n\n- build_success: {build_ok}\n- debug_cycles: {len(task.artifacts.debug_run_ids)}\n")

    report_path = run_dir / "report.md"
    write_text(report_path, "\n".join(md))
    return report_path
