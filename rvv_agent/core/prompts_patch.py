"""core.prompts_patch — Prompt templates for PATCH and DEBUG stages.

Separated from core/prompts.py to keep the original prompts untouched
(pipeline mode still uses them).
"""
from __future__ import annotations

import json


def patch_generate_prompt(
    symbol: str,
    analysis_json: dict,
    target_files: dict,
    repository_knowledge_entry: dict | None = None,
    existing_files_map: dict[str, str] | None = None,
    build_errors: str | None = None,
    debug_suggestions: list[str] | None = None,
    previous_code: dict | None = None,
    kb_errors: list[dict] | None = None,
    validation_feedback: list[str] | None = None,
) -> str:
    """Prompt for PATCH generation.

    This prompt is contract-driven and asks the model to output ready-to-apply
    file-role units with structured action types.
    """
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
                f"- [{er.get('error_class', '?')}] {er.get('pattern', '')[:80]} → 修复: {er.get('fix_strategy', '')}"
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
        fix_section += "\n请根据以上错误信息修正代码，而不是从头重新生成。\n"

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
            + json.dumps(repository_knowledge_entry, ensure_ascii=False, indent=2)[:5000]
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

## 输出 JSON（严格）
{{
    "generate_plan": {{
        "patches": [
            {{
                "target_path": "...",
                "action": "create|append|replace",
                "content": "..."
            }}
        ]
    }}
}}"""


def debug_classify_prompt(error_text: str, current_patch: dict | None = None) -> str:
    """Prompt for DEBUG stage: classify error and suggest rollback target."""
    patch_section = ""
    if current_patch:
        patch_section = f"\n## 当前 Patch 信息\n{json.dumps(current_patch, ensure_ascii=False, indent=2)[:3000]}"

    return f"""你是构建错误诊断专家。

## 构建错误
{error_text[:4000]}
{patch_section}

## 任务
1. 将错误分类为: compile_error | link_error | runtime_error | test_mismatch
2. 确定回滚目标:
   - "locate": 锚点漂移或 patch 应用位置错误
   - "design": 构建系统问题（Makefile 未添加文件、缺少头文件包含等）
   - "generate": 代码本身有语法/逻辑错误
3. 给出具体修复建议

严格输出 JSON:
{{"error_class": "...", "rollback_target": "...", "fix_actions": ["..."], "suggestion": "..."}}"""
