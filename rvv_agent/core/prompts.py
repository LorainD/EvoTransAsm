from __future__ import annotations

import json


def system_prompt() -> str:
    return (
        "你是一个面向 FFmpeg 的 RISC-V Vector (RVV) SIMD 迁移专家，同时也可以进行普通技术对话。"
        "当且仅当用户明确要做迁移/生成/编译/运行等动作时，你才进入迁移任务流程。"
        "在涉及修改代码、编译、scp、远程运行前，必须让人类确认。"
    )


def kb_reflection_prompt(symbol: str, debug_history: str, final_code: str) -> str:
    return f"""
你是一个专业的底层编译器与汇编优化专家。
以下是系统刚刚成功完成的 RISC-V Vector (RVV) 汇编迁移任务轨迹。

【迁移目标】: {symbol}
【Debug 排错历史】:
{debug_history}
【最终成功的代码片段 (Patch)】:
{final_code}

请分析上述轨迹，提炼出具有复用价值的经验，并严格按照以下 JSON 格式输出：
{{
  "migration_patterns": [
    {{
      "notes": "提炼的 RVV 汇编迁移策略（例如：如何处理结尾元素、使用了什么特定的指令组合）"
    }}
  ],
  "error_diagnostics": [
    {{
      "error_class": "compile_error | link_error | runtime_error | test_mismatch",
      "pattern": "提取原始报错日志中最核心的 100-200 个字符",
      "fix_strategy": "总结解决该报错的根本方法（例如：Makefile 遗漏依赖、寄存器复用冲突等）"
    }}
  ]
}}
请确保只输出纯 JSON，不要包含任何 markdown 标记。
"""


def intent_prompt(user_text: str) -> str:
    return f"""你将解析用户意图，并输出严格 JSON（不要额外文字）。

用户输入：{user_text}

输出 JSON schema：
{{
  \"action\": \"chat\" | \"migrate\",
  \"symbol\": \"任意 C 函数/算子标识符\" | \"\" ,
  \"notes\": \"...\"
}}

判定规则（尽量保守）：
- 只有当用户明确表达“迁移/生成 RVV/做 SIMD 优化/改 FFmpeg 代码/编译/跑 checkasm/把算子迁移到 RVV”等意图时，action=\"migrate\"。
- 否则 action=\"chat\"。
- 如果 action=migrate 且能从输入中确定要迁移的函数/算子名（可能不是 ff_*），symbol 填该标识符；否则 symbol 为空串。
"""


def retrieval_prompt(symbol: str, grouped: dict, matches: list) -> str:
    grouped_s = json.dumps(grouped, ensure_ascii=False)
    matches_s = "\n".join(str(m) for m in matches)

    return f"""你将根据检索分组结果，选择最相关的参考文件列表，并输出严格 JSON（不要额外文字）。

目标 symbol：{symbol}

候选分组（文件路径列表，JSON）：
{grouped_s}

部分命中行：
{matches_s}

输出 JSON schema：
{{
  \"symbol\": \"{symbol}\",
  \"c\": [\"...\"],
  \"x86\": [\"...\"],
  \"arm\": [\"...\"],
  \"riscv\": [\"...\"],
  \"headers\": [\"...\"],
  \"makefiles\": [\"...\"],
  \"existing_rvv\": [\"...\"],
  \"checkasm\": [\"...\"],
  \"notes\": \"...\"
}}

要求：
- 每个列表最多 5 个文件。
- **x86** 和 **arm** 列表中必须优先包含 .S / .asm 等实际 SIMD 实现文件（如 sbrdsp.asm、sbrdsp_neon.S），
  而不仅仅是 *_init*.c 注册文件——*_init*.c 只是函数指针赋值，真正的向量实现在汇编文件里。
- 若 x86_refs 或 arm_refs 中同时有 init.c 和 .S/.asm，请把 .S/.asm 放在前面。
- 当 x86_refs 明显偏少时，可优先利用模块近义词（如 h264pred -> h264）补充 x86 汇编文件。

【父模块与共享文件检索规则（CRITICAL）】
FFmpeg 中很多子模块（如 hevc_pel, h264_qpel）没有独立的 init.c，它们共享父模块初始化文件与 Makefile 声明。
1. 若目标 symbol/module 包含下划线（xxx_yyy），必须主动回溯父前缀（xxx）并检索对应 `libxx/riscv/` 目录下共享文件（如 `xxxdsp_init.c`、`xxx*_rvv.S`）。
2. 只要定位到共享的 `*_init.c` 或 `*.S`，必须显式放入 `existing_rvv` 列表，供后续 PATCH 阶段复用。
3. 必须将该模块所属库的 `libxx/riscv/Makefile` 选入 `makefiles` 列表，供后续检查是否已注册。
4. 禁止因为子模块命名而臆造新的 `xxx_yyy_init.c`；优先复用父模块共享 init 文件。
"""


def retrieval_alias_prompt(symbol: str, module: str, base_terms: list[str]) -> str:
    base_s = json.dumps(base_terms, ensure_ascii=False)
    return f"""你将为 FFmpeg 检索阶段生成少量命名别名词（alias），用于补充 x86/arm/aarch64 汇编文件召回。

目标 symbol：{symbol}
目标 module：{module}
已有检索词：{base_s}

要求：
- 只输出 0-5 个 alias，尽量短（如 h264、pred、intra）。
- alias 仅用于补充汇编文件检索，不要输出通用噪声词。
- 若目标是子模块（如 xxx_yyy），可补充父前缀（如 xxx）及其常见缩写，用于召回共享 init/汇编文件。
- 若没有高置信 alias，返回空数组。

严格输出 JSON：
{{"aliases": ["..."]}}
"""


def analysis_prompt(
    symbol: str,
    context: str,
    *,
    prior_analysis: dict | None = None,
    build_errors: str | None = None,
) -> str:
    """生成算子语义分析 prompt。

    Args:
        symbol: 要迁移的算子/函数名。
        context: 从源码提取的相关代码片段（完整函数体）。
        prior_analysis: 上轮分析 JSON（refine 时传入，LLM 可在此基础上修正）。
        build_errors: 历次构建错误文本（供 LLM 参考以调整分析）。
    """
    prior_section = ""
    if prior_analysis:
        import json as _json
        prior_section = f"""
# 上轮分析结果（请在此基础上修正，确保字段完整）：
```json
{_json.dumps(prior_analysis, ensure_ascii=False, indent=2)}
```
"""

    errors_section = ""
    if build_errors:
        errors_section = f"""
# 历次构建错误（参考以修正分析中对数据类型/向量长度/饱和运算等的判断）：
{build_errors}
"""

    return f"""任务：对 {symbol} 做结构化 IR 分析，输出可用于 RVV 代码生成与知识库匹配的 JSON。

⚠️ 输出必须是严格 JSON，禁止额外解释文字。

输出格式：
{{
  "symbol": "{symbol}",
  "ir": {{
    "computation": {{
      "type": "elementwise|reduction|convolution|unknown",
      "expression_tree": {{
        "op": "...",
        "inputs": [ ... ],
        "params": {{ ... }}
      }},
      "math_expression": "结构化数学伪公式，示例: sum_top = ReduceSum(vld(src - stride, vlen)); sum_left = ReduceSum(vld(src - 1, vlen)); avg = (sum_top + sum_left) >> 4; Broadcast(avg) to all 16x16 elements."
    }},
    "memory": {{
      "access_pattern": "contiguous|stride|gather|scatter",
      "stride": "none|fixed|variable",
      "alignment": "aligned|unaligned|unknown",
      "layout": "1D|2D"
    }},
    "parallelism": {{
      "vectorizable": true|false,
      "reduction": true|false,
      "dependency": "none|loop_carried|unknown",
      "tail_policy": "none|required"
    }},
    "experience": {{
      "arch_simd_experience": {{
        "x86": ["从x86 SIMD实现抽取的可迁移经验", ...],
        "arm": ["从ARM/NEON实现抽取的可迁移经验", ...],
        "aarch64": ["从AArch64实现抽取的可迁移经验", ...]
      }}
    }}
  }},
  "simd_features": {{
    "has_saturation": true|false,
    "has_widening": true|false,
    "has_narrowing": true|false
  }},
  "references": {{
    "c": ["path:line", ...],
    "x86": ["path:line", ...],
    "arm": ["path:line", ...]
  }},
  "notes": "...",
  "confidence": 0.0
}}

规则：
- expression_tree 必须是结构化 AST；math_expression 作为可读公式补充，不能替代 AST；
- 所有语义必须通过 ir 三层表达，禁止输出 pattern 字段；
- math_expression 必须给出可执行风格伪公式，禁止留空；
- 默认禁止 unknown，除非源码信息确实不足；若使用 unknown，必须在 notes 中写明具体原因；
- x86/arm 引用优先 .S/.asm 的实际 SIMD 实现。
{prior_section}{errors_section}
上下文（完整函数体，带行号）：
{context}
"""

def _number_lines(text: str, max_lines: int = 120) -> str:
    """为文本内容加上行号，便于 LLM 精确定位。"""
    lines = text.splitlines()[:max_lines]
    return "\n".join(f"{i:4d}: {l}" for i, l in enumerate(lines))


def function_analysis_prompt(
    function_name: str,
    code_context: str,
    kb_patterns: list[dict] | None = None,
    prior_analysis: dict | None = None,
    build_errors: str | None = None,
) -> str:
    """为单个 function 生成分析 prompt。

    Args:
        function_name: 函数名
        code_context: 函数代码上下文
        kb_patterns: 从 KB 检索到的相关 pattern 列表
        prior_analysis: 上轮分析结果（refine 时传入）
        build_errors: 历次构建错误
    """
    prior_section = ""
    if prior_analysis:
        import json as _json
        prior_section = f"""
# 上轮分析结果（请在此基础上修正，确保字段完整）：
```json
{_json.dumps(prior_analysis, ensure_ascii=False, indent=2)}
```
"""

    errors_section = ""
    if build_errors:
        errors_section = f"""
# 历次构建错误（参考以修正分析中对数据类型/向量长度/饱和运算等的判断）：
{build_errors}
"""

    kb_section = ""
    if kb_patterns:
        import json as _json
        kb_section = f"""
# 知识库中的相关 pattern（可参考这些已验证的实现策略）：
```json
{_json.dumps(kb_patterns, ensure_ascii=False, indent=2)}
```
"""

    return f"""任务：对函数 {function_name} 做结构化分析，构建可用于 RVV 生成与 KB 匹配的 IR。

⚠️ 输出必须是严格 JSON，禁止额外解释文字。

输出格式：
{{
  "function_name": "{function_name}",
  "ir": {{
    "computation": {{
      "type": "elementwise|reduction|convolution|unknown",
      "expression_tree": {{
        "op": "...",
        "inputs": [ ... ],
        "params": {{ ... }}
      }},
      "math_expression": "结构化数学伪公式，示例: sum_top = ReduceSum(vld(src - stride, vlen)); sum_left = ReduceSum(vld(src - 1, vlen)); avg = (sum_top + sum_left) >> 4; Broadcast(avg) to all 16x16 elements."
    }},
    "memory": {{
      "access_pattern": "contiguous|stride|gather|scatter",
      "stride": "none|fixed|variable",
      "alignment": "aligned|unaligned|unknown",
      "layout": "1D|2D"
    }},
    "parallelism": {{
      "vectorizable": true|false,
      "reduction": true|false,
      "dependency": "none|loop_carried|unknown",
      "tail_policy": "none|required"
    }},
    "experience": {{
      "arch_simd_experience": {{
        "x86": ["从x86 SIMD实现抽取的可迁移经验", ...],
        "arm": ["从ARM/NEON实现抽取的可迁移经验", ...],
        "aarch64": ["从AArch64实现抽取的可迁移经验", ...]
      }}
    }}
  }},
  "simd_features": {{
    "has_saturation": true|false,
    "has_widening": true|false,
    "has_narrowing": true|false
  }},
  "references": {{
    "c": ["path:line", ...],
    "x86": ["path:line", ...],
    "arm": ["path:line", ...]
  }},
  "kb_match": {{
    "matched_pattern_ids": ["pattern_id", ...],
    "match_reason": "基于 computation/memory/parallelism 的匹配依据"
  }},
  "notes": "...",
  "confidence": 0.0
}}

规则：
- expression_tree 必须是结构化 AST；math_expression 作为可读公式补充，不能替代 AST；
- 所有语义必须通过 ir 三层表达，禁止输出 pattern 字段；
- 至少返回 0~3 个最相似 KB pattern，并写明匹配依据；
- math_expression 必须给出可执行风格伪公式，禁止留空；
- 默认禁止 unknown，除非源码信息确实不足；若使用 unknown，必须在 notes 中写明具体原因；
- x86/arm 引用优先 .S/.asm 的实际 SIMD 实现。
{prior_section}{errors_section}{kb_section}
上下文（完整函数体，带行号）：
{code_context}
"""


def generation_prompt(symbol: str, analysis_json: str, existing_files_map: dict | None = None) -> str:
    existing_section = ""
    if existing_files_map:
        parts = ["\n以下文件在 FFmpeg workspace 中已存在（供参考，勿在 content 字段中输出完整文件）：\n"]
        for path, cnt in existing_files_map.items():
            parts.append(f"--- 已有文件: {path} ---")
            parts.append(cnt[:6000])
            parts.append("--- 文件结束 ---\n")
        existing_section = "\n".join(parts)

    return f"""基于下面的 JSON 分析，为 {symbol} 生成 RVV 代码片段（不要解释）。

★★ 核心原则：输出的每个 item 只包含"新增的片段"，不要输出完整已有文件内容。
   每个 item 的 content 只含本次新增的代码。

要求：
0) target_path中的 libxx 必须参考reference_files中当前函数所在c文件的路径结构，保持一致性。
1) .S 汇编实现（target_path 示例：libxx/riscv/<module>_rvv.S）
   - 若模块 .S 文件**不存在**：action="create"，content 为完整新 .S 文件
     （含 .text / .align / .globl / .type / label / .size / ret 等）。
   - 若模块 .S 文件**已存在**：action="append"，content 仅含新增函数
     （从 .text 起到最后 .size 结束），不含已有函数。
2) init.c 注册（target_path 示例：libxx/riscv/<module>_init.c）
   - **检查共享文件（CRITICAL）**：若 existing_files/existing_rvv 已存在父模块 init（如 hevcdsp_init.c），绝对不要新建 `hevc_pel_init.c` 等同义文件。
   - 必须对已存在的父模块 init 文件使用 action="append"，并把新增函数指针赋值插入其现有注册函数中。
   - action="append" 时，content 仅含新增赋值语句（1-3 行），
     如：c_func(ff_xxx) = ff_xxx_rvv;
   - anchor_hint：指出应插入到哪个函数内的哪个位置，如
     "在 ff_sbrdsp_init_riscv() 函数内 #if HAVE_RVV 块末尾"。
3) Makefile（target_path 示例：libxx/riscv/Makefile）
   - 仅当本次 generated 中存在 action="create" 的新文件时才考虑输出 Makefile 变更。
   - action="append"，content 仅含新增的 .o 行（1-2 行），
     如：sbrnewfunc_rvv.o \\
   - anchor_hint：如 "追加到 OBJS-$(CONFIG_AAC_DECODER) 块末尾"。

输出格式必须是严格 JSON（不要额外文字）：
{{{{
  "generated": [
    {{{{
      "target_path": "libxx/riscv/...",
      "action": "create" | "append",
      "content": "仅新增代码",
      "anchor_hint": "...",
      "description": "一句话说明"
    }}}}
  ]
}}}}
{existing_section}
analysis_json:
{analysis_json}
"""


def injection_locator_prompt(
    target_path: str,
    existing_content: str,
    snippet: str,
    anchor_hint: str,
) -> str:
    return f"""你是代码注入专家。请根据以下信息，输出严格 JSON，指出应将代码片段
插入到目标文件的哪一行之后（0-based 行索引）。

目标文件路径：{target_path}

anchor_hint（生成器提供的插入位置提示）：
{anchor_hint}

待插入代码片段：
```
{snippet[:1000]}
```

目标文件现有内容（含行号）：
{_number_lines(existing_content, max_lines=120)}

输出 JSON schema（不要额外文字）：
{{{{
  "strategy": "insert_after_line" | "append_at_end",
  "line": <0-based 行索引，若 strategy=append_at_end 则填 -1>,
  "reason": "一句话说明"
}}}}

规则：
- 优先依据 anchor_hint 找到最合适的插入位置。
- 若无法确定，使用 "append_at_end"。
- 绝对不要删除或覆盖现有内容。
"""


def plan_prompt(
  symbol: str,
  functions: list[dict] | None = None,
  reference_files: list[str] | None = None,
  kb_short_rules: list[str] | None = None,
) -> str:
    func_section = ""
    if functions:
        func_lines: list[str] = []
        for idx, func in enumerate(functions, start=1):
            deps = func.get("dependencies", []) if isinstance(func, dict) else []
            deps_s = ", ".join(str(x) for x in deps) if deps else "无"
            func_lines.append(
                f"- #{idx} name={func.get('name', '')}; role={func.get('role', '') or 'unknown'}; "
                f"dependencies={deps_s}; semantic_hint={func.get('semantic_hint', '')}"
            )
        func_section = "\n已发现的待迁移函数：\n" + "\n".join(func_lines) + "\n"

    refs_section = ""
    if reference_files:
        ref_lines = [f"- {str(p)}" for p in reference_files if str(p).strip()]
        if ref_lines:
            refs_section = "\n检索/选择出的参考文件：\n" + "\n".join(ref_lines) + "\n"

    kb_rules_section = ""
    if kb_short_rules:
      lines = [f"- {str(x)}" for x in kb_short_rules if str(x).strip()]
      if lines:
        kb_rules_section = "\n知识库工程禁忌（短规则，仅用于规划防偏）：\n" + "\n".join(lines) + "\n"

    return f"""你是 FFmpeg RVV SIMD 迁移助手。请为迁移算子 {symbol} 生成一份具体、可执行的迁移计划。
  {func_section}{refs_section}{kb_rules_section}
目标：根据函数发现结果，判断应当：
- 一次迁移单函数，还是一次迁移多个函数；
- 哪些函数存在依赖关系，应放入同一组或前后顺序约束；
- 哪些函数更简单，应优先迁移（先易后难）。

要求：
- 步骤必须具体针对 {symbol}，而不是泛泛模板。
- 必须结合函数发现结果给出 function_order。
- 必须给出 groups，每个 group 说明是一组单函数、依赖函数组、相似函数组还是困难函数组。
- group 内函数应来自已发现函数列表，不要虚构函数。
- 优先让 core 计算函数先于 dependency/注册函数。
- init.c 每次只注册目前正在生成的函数，不要一次性注册所有已发现函数。
- 如果某些函数彼此依赖且拆开迁移风险高，可以放入同一 group。
- 如果某些函数语义相似、难度低，可以建议批量迁移。
- 如果某函数明显更复杂，应该放在更后。
- 如果生成了新的必要文件，需要更改makefile，针对makefile的修改只能添加，不能删除已有内容。
- 若参考文件中出现 "[existing-rvv] libxx/riscv/..._rvv.S" 或 "[existing-rvv] ..._init.c"，
  计划应明确优先在现有 RVV 文件上 append/增量扩展，而不是重复创建新的 .S 文件，同时一定要考虑不需要修改makefile和源文件riscv入口的可能性。
- 输出严格 JSON（不要额外文字）。

输出格式：
{{
  "symbol": "{symbol}",
  "steps": ["步骤1", "步骤2"],
  "function_order": ["func1", "func2"],
  "groups": [
    {{
      "group_id": "group_1",
      "group_type": "single|dependency|similar|hard",
      "order": 1,
      "functions": [
        {{
          "name": "func1",
          "role": "core|dependency",
          "dependencies": ["func0"],
          "semantic_hint": "..."
        }}
      ]
    }}
  ],
  "acceptance_criteria": {{
    "build_ok": true,
    "functionally_valid": true
  }},
  "rationale": "说明为何这样分组、为何该顺序能体现依赖关系与先易后难策略"
}}
"""


def plan_refine_prompt(symbol: str, current_steps: list[str], user_feedback: str) -> str:
    steps_s = "\n".join(f"{i+1}. {s}" for i, s in enumerate(current_steps))
    return f"""当前为算子 {symbol} 生成的迁移计划如下：

{steps_s}

用户反馈/修改意见：
{user_feedback}

请根据反馈修改计划，输出严格 JSON（不要额外文字）：
{{
  "symbol": "{symbol}",
  "steps": ["..."],
  "notes": "..."
}}
"""


def build_plan_refine_prompt(
    symbol: str,
    current_groups: list[dict],
    all_functions: list[dict],
    user_feedback: str,
) -> str:
    """Build prompt for group-level plan refine.

    Args:
        symbol: 当前目标符号。
        current_groups: 当前 plan 的 groups（可 JSON 序列化的 dict 列表）。
        all_functions: 全部待迁移函数（可 JSON 序列化的 dict 列表）。
        user_feedback: 用户的交互式修改意见。
    """
    return f"""你是 FFmpeg RVV SIMD 迁移助手。根据用户反馈修改迁移计划。

目标符号：{symbol}

当前 Plan Groups：
{json.dumps(current_groups, ensure_ascii=False, indent=2)}

所有待迁移函数：
{json.dumps(all_functions, ensure_ascii=False, indent=2)}

用户反馈：
{user_feedback}

请根据反馈修改 plan，输出严格 JSON（不要额外文字）：
{{
  "groups": [
    {{
      "group_id": "group_1",
      "type": "single|dependency|similar|hard",
      "functions": ["func_name1", "func_name2"],
      "rationale": "为什么这样分组"
    }}
  ],
  "notes": "修改说明"
}}

要求：
- 保持所有函数都被分配到某个 group
- 尊重函数依赖关系
- 优先让简单函数先迁移
"""


def files_refine_prompt(symbol: str, current_files: list[str], user_feedback: str) -> str:
    files_s = "\n".join(f"- {f}" for f in current_files)
    return f"""当前为算子 {symbol} 选择的参考文件如下：

{files_s}

用户反馈/修改意见：
{user_feedback}

请根据反馈调整文件列表，输出严格 JSON（不要额外文字）：
{{
  "symbol": "{symbol}",
  "c": ["..."],
  "x86": ["..."],
  "arm": ["..."],
  "riscv": ["..."],
  "headers": ["..."],
  "makefiles": ["..."],
  "checkasm": ["..."],
  "notes": "..."
}}
"""


def build_fix_prompt(
    symbol: str,
    build_error: str,
    generated_files: list[dict],
    *,
    analysis: dict | None = None,
    all_prior_errors: str | None = None,
) -> str:
    """生成构建修复 prompt。

    Args:
        symbol: 算子名。
        build_error: 当前这次构建的错误文本。
        generated_files: 已生成的代码文件列表。
        analysis: 完整的算子语义分析 JSON（从 DynamicContext 传入，不可省略）。
        all_prior_errors: 所有历次构建错误的汇总文本（帮助 LLM 避免重复错误）。
    """
    import json as _json
    files_s = ""
    for f in generated_files[:4]:
        path = f.get("path", "?")
        content = f.get("content", "")[:3000]
        files_s += f"\n--- {path} ---\n{content}\n--- end ---\n"

    analysis_section = ""
    if analysis:
        analysis_section = f"""
# 算子语义分析 JSON（请以此为依据修复类型/指令选择等问题）：
```json
{_json.dumps(analysis, ensure_ascii=False, indent=2)}
```
"""

    history_section = ""
    if all_prior_errors:
        history_section = f"""
# 历次构建错误汇总（避免重复已修复/未修复的问题）：
{all_prior_errors}
"""

    return f"""构建 {symbol} 时发生编译错误，请根据编译错误信息对相应的代码进行修改。
{analysis_section}{history_section}
# 本次编译错误信息：
{build_error[:3000]}

# 已生成的文件（参考当前状态）：
{files_s}

请输出修复后的完整文件，格式为严格 JSON（不要额外文字）：
{{
  "files": [{{"path": "...", "content": "..."}}, ...],
  "patches": [{{"path": "...", "diff": "..."}}, ...]
}}
"""


def function_discovery_prompt(symbol: str, code_context: str) -> str:
    """Prompt for FUNC_DISCOVER stage: identify all functions to migrate."""
    return f"""你是 FFmpeg 源码分析专家。

目标模块/算子: {symbol}

## 任务
分析下面的代码上下文，找出所有属于 {symbol} 算子且适合迁移到 RVV (RISC-V Vector) 的 C 函数。函数名应与{symbol}相关

##函数来源文件
1.重点观察libxx/.h文件中与 {symbol} 相关的函数定义和声明。
2.重点观察libxx/架构目录下的.c文件中与 {symbol} 相关的函数定义和声明。重点迁移x86/ARM上已有向量化实现的函数

## 提取规则与约束（CRITICAL）
1. 你提取的一定是实际存在的函数名
2. 你的目标是提取需要被翻译为 RISC-V Vector (RVV) 的核心 C 语言标量函数。
3. 纯量函数后缀识别：可以提取以 `_c` 结尾的函数，不提取以`_init`, `_sse`, `_sse2`, `_avx`, `_avx2`, `_neon`, `_vfp`, `_altivec`, `_mmi` 等特定架构后缀结尾的通用 C 函数。
4. 提取的函数一定包含 {symbol} 部分字段，但不要求完全匹配，如{symbol}末尾_num 可能是该函数的num bit实现
5.函数必须包含可向量化的计算（循环中的数组操作、SIMD 风格运算等）

你不仅要识别函数名，还要判断：
- 该函数是否是核心计算函数（core）还是依赖/辅助函数（dependency）
- 它依赖哪些同模块函数
- 它的迁移难度是容易还是困难
- 是否适合和其它函数一起批量迁移
- 如果多个函数语义相近、可共享向量化模板，请在 semantic_hint 中说明 similar
- 如果函数依赖另一个函数才能完整落地，请把被依赖函数放到 dependencies
- 如果函数明显更复杂，请在 semantic_hint 中说明 hard/complex
- 不要有重复的函数，同名函数请去重


对每个发现的函数，输出：
- name: 函数名（如 ff_sbr_neg_odd_64）
- signature: 完整函数签名
- file: 所在源文件的相对路径
- line: 函数定义起始行号
- role: core | dependency
- dependencies: 依赖的同模块函数名列表
- semantic_hint: 简短说明，如 easy / similar-to-xxx / hard / init-last / helper

输出严格 JSON（不要额外文字）：
{{
  "symbol": "{symbol}",
  "functions": [
    {{
      "name": "...",
      "signature": "...",
      "file": "...",
      "line": 0,
      "role": "core|dependency",
      "dependencies": ["..."],
      "semantic_hint": "..."
    }}
  ],
  "notes": "..."
}}

代码上下文：
{code_context[:12000]}
"""
