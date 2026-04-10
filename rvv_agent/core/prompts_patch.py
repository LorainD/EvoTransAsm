"""core.prompts_patch — Prompt templates for PATCH and DEBUG stages.

Separated from core/prompts.py to keep the original prompts untouched
(pipeline mode still uses them).
"""
from __future__ import annotations

import json


def patch_locate_prompt(
    symbol: str,
    analysis_json: dict,
    selected_files: list[str],
    code_context: str,
) -> str:
    """Prompt for Step 1: locate precise patch points."""
    return f"""你是 FFmpeg RVV 迁移专家。

目标算子: {symbol}

## 语义分析（当前分组）
{json.dumps(analysis_json, ensure_ascii=False, indent=2)}

## 参考文件列表
{json.dumps(selected_files, ensure_ascii=False)}

## 代码上下文
{code_context[:6000]}

## 锚点定位技能
你需要识别以下类型的锚点：
- 函数声明/定义：C 源文件中的函数签名行，用于确定 RVV 替代目标
- #include 行：头文件中需要添加 RVV 函数声明的位置
- 条件编译块：`#if HAVE_RVV` / `if (flags & AV_CPU_FLAG_RVV_...)`，用于注册 RVV 实现
- Makefile 规则：`OBJS-$(CONFIG_...)` 块，用于添加新的 .o 目标
- 汇编文件末尾：已有 .S 文件的最后一个 `.size` 之后，用于追加新函数

## 任务
分析上述信息，确定需要修改/创建的文件及精确插入位置。

对每个需要变更的文件，输出:
- file: 相对路径
- line: 插入行号（0-based，-1 表示新建文件或追加到末尾）
- rationale: 为什么在这里插入

严格输出 JSON:
{{"patch_points": [{{"file": "...", "line": -1, "rationale": "..."}}]}}"""


def patch_design_prompt(
    symbol: str,
    analysis_json: dict,
    patch_points: list[dict],
    kb_patterns: list[dict] | None = None,
) -> str:
    """Prompt for Step 2: design the patch (what to change, not the code)."""
    kb_section = ""
    if kb_patterns:
        kb_section = f"\n## 知识库中的相关模式\n{json.dumps(kb_patterns, ensure_ascii=False, indent=2)}"

    return f"""你是 FFmpeg RVV 迁移专家。

目标算子: {symbol}

## 语义分析（当前分组）
{json.dumps(analysis_json, ensure_ascii=False, indent=2)}

## 锚点定位结果
{json.dumps(patch_points, ensure_ascii=False, indent=2)}
{kb_section}

## 变更类型技能
你可以使用以下变更类型，每种类型有不同语义：
- create_file: 创建全新文件（常用于 module_rvv.S）
- append_function: 在已有文件末尾追加新函数
- inject_init: 在 init.c 的 RVV 注册块内注入函数指针赋值
- inject_header: 在头文件中添加函数声明
- inject_makefile: 在 Makefile 的 OBJS 列表中添加 .o 目标

## 设计契约（必须满足）
1. 必须形成闭环：至少包含 impl + register。
2. 若新建 .S 文件，必须同时给出 register 路径；build 变更按需，但必须给出 needs_build_change 与 reason。
3. 必须明确 create_vs_append 决策依据（文件存在性、符号是否已存在）。
4. 优先复用 FFmpeg 已有模式，不要自由发散。

## 输出要求
请输出以下 JSON 字段：
- strategy: "append_existing" | "create_new" | "mixed"
- invariants: ["..."]
- needs_build_change: true|false
- build_change_reason: "..."
- changes: [
  {{
    "type": "...",
    "file": "...",
    "role": "impl|register|build|header",
    "description": "...",
    "code_items": ["..."]
  }}
]
- rationale: "..."

严格输出 JSON。"""


def patch_generate_prompt(
    symbol: str,
    analysis_json: dict,
    design: dict,
    existing_files_map: dict[str, str] | None = None,
    build_errors: str | None = None,
    debug_suggestions: list[str] | None = None,
    previous_code: dict | None = None,
    kb_errors: list[dict] | None = None,
    validation_feedback: list[str] | None = None,
) -> str:
    """Prompt for Step 3: generate actual code based on design.

    When ``build_errors`` is provided (retry after DEBUG), the prompt includes
    error text, debug suggestions, and previous failing code for targeted fixes.
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
            prev_parts = []
            for item in previous_code.get("generated", []):
                tp = item.get("target_path", "?")
                code = item.get("content", "")
                prev_parts.append(f"### {tp}\n```\n{code[:3000]}\n```")
            if prev_parts:
                fix_section += "\n## 上次生成的代码（有错误，需要修正）\n" + "\n".join(prev_parts) + "\n"
        fix_section += "\n请根据以上错误信息修正代码，而不是从头重新生成。\n"

    validation_section = ""
    if validation_feedback:
        validation_section = (
            "\n## 上一轮闭环校验失败原因（必须修复）\n"
            + "\n".join(f"- {x}" for x in validation_feedback)
            + "\n"
        )

    return f"""你是 FFmpeg RVV 迁移专家。请根据设计生成可直接注入的完整变更单元。

目标算子: {symbol}

## 语义分析（当前分组）
{json.dumps(analysis_json, ensure_ascii=False, indent=2)}

## 变更设计
{json.dumps(design, ensure_ascii=False, indent=2)}
{existing_section}{kb_section}{fix_section}{validation_section}

## 强约束
1. 产物必须覆盖 impl + register。仅当 design.needs_build_change=true 时，必须同时覆盖 build。
2. 按文件单位输出，不要只给零散片段。
3. RVV .S 要遵循 FFmpeg 现有模式：
   - 推荐包含：`#include "libavutil/riscv/asm.S"`（按目标目录实际模板）
   - 统一函数命名与宏风格，包含 `.globl/.type/ret/.size`
4. init.c 必须形成声明/注册闭环，避免只声明不注册或只注册未实现。
5. 仅在需要时改 Makefile、头文件或原始 C 文件；禁止无依据改主流程逻辑。

## 输出 JSON（严格）
{{
  "generated": [
    {{
      "target_path": "...",
      "role": "impl|register|build|header",
      "action": "create|append|inject",
      "content": "...",
      "anchor_hint": "...",
      "description": "..."
    }}
  ]
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
