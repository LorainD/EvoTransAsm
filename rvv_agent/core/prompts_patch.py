"""core.prompts_patch — Prompt templates for PATCH and DEBUG stages.

Separated from core/prompts.py to keep the original prompts untouched
(pipeline mode still uses them).

Now uses dataclass-based context (PatchContext, DebugContext) for cleaner API.
Backward-compatible wrappers provided for legacy callers.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..agent.context_builder import PatchContext, DebugContext


def patch_generate_prompt(
    context: PatchContext | str,
    analysis_json: dict | None = None,
    target_files: dict | None = None,
    repository_knowledge_entry: dict | None = None,
    existing_files_map: dict[str, str] | None = None,
    build_errors: str | None = None,
    debug_suggestions: list[str] | None = None,
    previous_code: dict | None = None,
    kb_errors: list[dict] | None = None,
    validation_feedback: list[str] | None = None,
) -> str:
    """Prompt for PATCH generation.

    Accepts either:
    - New API: PatchContext dataclass (recommended)
    - Legacy API: individual parameters (for backward compatibility)

    This prompt is contract-driven and asks the model to output ready-to-apply
    file-role units with structured action types.
    """
    # Handle both new dataclass API and legacy parameter API
    if isinstance(context, str):
        # Legacy API: context is actually symbol
        symbol = context
        ctx_dict = {
            "symbol": symbol,
            "analysis_json": analysis_json or {},
            "target_files": target_files or {},
            "repository_knowledge_entry": repository_knowledge_entry,
            "existing_files_map": existing_files_map,
            "build_errors": build_errors,
            "debug_suggestions": debug_suggestions,
            "previous_code": previous_code,
            "kb_errors": kb_errors,
            "validation_feedback": validation_feedback,
        }
    else:
        # New API: context is PatchContext dataclass
        ctx_dict = {
            "symbol": context.symbol,
            "analysis_json": context.analysis_json,
            "target_files": context.target_files,
            "repository_knowledge_entry": context.repository_knowledge_entry,
            "existing_files_map": context.existing_files_map,
            "build_errors": context.build_errors,
            "debug_suggestions": context.debug_suggestions,
            "previous_code": context.previous_code,
            "kb_errors": context.kb_errors,
            "validation_feedback": context.validation_feedback,
        }

    symbol = ctx_dict["symbol"]
    analysis_json = ctx_dict["analysis_json"]
    target_files = ctx_dict["target_files"]
    repository_knowledge_entry = ctx_dict["repository_knowledge_entry"]
    existing_files_map = ctx_dict["existing_files_map"]
    build_errors = ctx_dict["build_errors"]
    debug_suggestions = ctx_dict["debug_suggestions"]
    previous_code = ctx_dict["previous_code"]
    kb_errors = ctx_dict["kb_errors"]
    validation_feedback = ctx_dict["validation_feedback"]

    existing_section = ""
    if existing_files_map:
        parts = []
        for path, content in existing_files_map.items():
            parts.append(f"### {path}\n```\n{content[:3000]}\n```")
        existing_section = "\n## 现有文件内容（需要做增量合并）\n" + "\n".join(parts)

    kb_section = ""
    if kb_errors:
        kb_parts = []
        for er in kb_errors:
            kb_parts.append(
                "\n".join(
                    [
                        f"- [{er.get('error_class', '?')}] {er.get('pattern', '')[:100]}",
                        f"  根因: {er.get('root_cause', '')}",
                        f"  死胡同: {er.get('dead_end', '')}",
                        f"  修复方向: {er.get('fix_strategy', '')}",
                    ]
                )
            )
        kb_section = "\n## 历史错误经验（来自知识库，请避免重复这些错误）\n" + "\n".join(kb_parts) + "\n"

    fix_section = ""
    if build_errors:
        fix_section += f"\n## 上次构建错误（必须修复）\n```\n{build_errors[:4000]}\n```\n"
        if debug_suggestions:
            fix_section += "\n## 诊断建议\n" + "\n".join(f"- {s}" for s in debug_suggestions) + "\n"
        if previous_code:
            prev_items = previous_code.get("generated", [])
            if not prev_items and isinstance(previous_code.get("generate_plan"), dict):
                prev_items = previous_code.get("generate_plan", {}).get("patches", [])
            prev_parts = []
            for item in prev_items:
                tp = item.get("target_path", "?")
                code = item.get("content", "")
                prev_parts.append(f"### {tp}\n```\n{code[:3000]}\n```")
            if prev_parts:
                fix_section += "\n## 上次生成的代码（有错误，需要修正）\n" + "\n".join(prev_parts) + "\n"
        fix_section += "\n请根据以上错误信息修正代码\n"

    validation_section = ""
    if validation_feedback:
        validation_section = (
            "\n## 上一轮校验失败原因（必须修复）\n"
            + "\n".join(f"- {x}" for x in validation_feedback)
            + "\n"
        )

    repo_section = ""
    if repository_knowledge_entry:
        repo_section = (
            "\n## 仓库实现经验（repository_knowledge）\n"
            + json.dumps(repository_knowledge_entry, ensure_ascii=False, indent=2)[:1000]
            + "\n"
        )

    return f"""你是 FFmpeg RVV 迁移专家。请生成可直接注入的完整变更单元。

目标算子: {symbol}

## 语义分析（当前分组）
{json.dumps(analysis_json, ensure_ascii=False, indent=2)}

## 注入目标状态（工具扫描结果，确定性）
{json.dumps(target_files, ensure_ascii=False, indent=2)}
{repo_section}{existing_section}{kb_section}{fix_section}{validation_section}

## 合法 action 类型
- create  : 新建文件（完整内容）
- append  : 追加内容到已有文件（若不存在则创建）
- replace : 覆盖整个目标文件（完整内容）

禁止输出其他 action。

## 强约束
1. 按文件单位输出，不要只给零散片段。
2. RVV .S 要遵循 FFmpeg 现有模式（命名、宏、.globl/.type/ret/.size）。
3. init.c/Makefile 内容必须与生成实现保持一致，避免声明-实现脱节。
4. 如果对 Makefile 使用 append（只允许输出新增 `+=` 行），你必须设置 `anchor_hint: "file_start"` 以确保新增声明被插入到文件开头。
5. 如果在通用 C/H 文件里注入 `#elif ARCH_RISCV` 分支，你必须设置 `anchor_hint: "before_arch_chain_endif"`，把分支插入到与其他 `ARCH_*` 分支同一条预处理链里（即插在该链 closing `#endif` 之前），严禁追加到文件末尾。
6. 当你需要对已有文件执行 append 或 replace 时，必须先通过 view_file 工具查看该文件的当前内容（带行号，最大4000行），了解文件结构后再生成 patch，禁止在未查看的情况下直接修改已有文件。
7. 遇到实现困难或需要修复 debug 错误时，建议先用 view_file 查看语义分析中列出的其他架构参考文件（arm/aarch64/x86 的 .S 文件），参考其函数签名、寄存器用法和算法逻辑来优化 RVV 实现。
8. ★★ 严禁生成空壳实现。.S 文件中每个函数必须包含真实的 RVV 向量指令（如 vle/vse/vadd/vmul 等 v 开头指令），仅有 ret 或仅做标量 load/store 的函数视为空壳，会被系统检测并拒绝。

## 输出 JSON（严格）
{{
    "generate_plan": {{
        "patches": [
            {{
                "target_path": "...",
                "action": "create|append|replace",
                "content": "...",
                "anchor_hint": ""
            }}
        ]
    }}
}}"""


def debug_classify_prompt(
    context: DebugContext | str,
    current_patch: dict | None = None,
) -> str:
    """Prompt for DEBUG stage: classify error and suggest rollback target.

    Accepts either:
    - New API: DebugContext dataclass (recommended)
    - Legacy API: error_text string + current_patch dict (for backward compatibility)
    """
    # Handle both new dataclass API and legacy parameter API
    if isinstance(context, str):
        # Legacy API: context is actually error_text
        error_text = context
    else:
        # New API: context is DebugContext dataclass
        error_text = context.error_text
        current_patch = context.current_patch

    patch_section = ""
    if current_patch:
        patch_section = f"\n## 当前 Patch 信息\n{json.dumps(current_patch, ensure_ascii=False, indent=2)[:3000]}"

    return f"""你是构建错误诊断专家。

## 构建错误
{error_text[:4000]}
{patch_section}

## 任务
1. 判定错误出现的阶段（高层 tag）: configure_error | build_error | test_error | patch_error
2. 在 error_note 中给出细粒度分类和原因说明，例如:
    - compile_error / link_error / runtime_error / test_mismatch
    - 是 Makefile/注册问题，还是没有真正插入代码（inject_error）
    - 是 rvv_missing 这类语义错误，还是纯编译/链接错误
3. 确定回滚目标 rollback_target:
    - "generate": 唯一合法值。即便根因是锚点/构建系统问题，也请在 fix_actions 中描述，回滚目标仍输出 generate。
4. 给出具体修复建议（fix_actions）和一个简短的建议总结（suggestion）

严格输出 JSON:
{{"error_class": "...", "error_note": "...", "rollback_target": "...", "fix_actions": ["..."], "suggestion": "..."}}"""


def checkasm_debug_prompt(context: dict) -> str:
    """Prompt for board-side checkasm failure analysis."""
    return f"""你是 FFmpeg RVV/checkasm 调试专家。

请基于下面的板测上下文分析失败原因，并给出可执行修复建议。

## checkasm 失败上下文
{json.dumps(context, ensure_ascii=False, indent=2)[:12000]}

## 任务
1. 判断错误分类: compile_error | link_error | runtime_error | test_mismatch
2. 判断回滚目标:
    - generate: 唯一合法值。若你认为是 locate/design 类问题，请把该诊断写入 fix_actions，而不是更改 rollback_target。
3. 给出 3-8 条 fix_actions（每条尽量具体到文件或修改方向）
4. 输出一段简短建议 suggestion

严格输出 JSON：
{{"error_class":"...","rollback_target":"...","fix_actions":["..."],"suggestion":"..."}}"""
