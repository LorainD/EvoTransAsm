# Context Builder 重构计划

## 现状分析

### 问题
1. **双轨制混乱**：`prompts_patch.py` 和 `prompts.py` 中的 prompt 生成函数直接拼接字符串，没有统一的上下文组织
2. **ContextBuilder 未充分利用**：`context_builder.py` 存在但只在部分地方使用，大多数 agent 仍直接调用 prompt 函数
3. **上下文重复**：多个 prompt 函数重复处理相同的上下文片段（错误、KB、代码等）
4. **缺乏规范**：没有明确的上下文构建流程和数据结构规范

### 当前调用关系
- `analyze.py`：直接调用 `analysis_prompt()` 等，未用 ContextBuilder
- `patch.py`：直接调用 `patch_generate_prompt()`，未用 ContextBuilder
- `debug.py`：直接调用 `debug_classify_prompt()`，未用 ContextBuilder
- `context_builder.py`：存在但只有 `build_analyze_context()` 等方法，未被充分调用

## 重构目标

建立**统一的 Context Builder 体系**：
1. 所有 prompt 生成都通过 ContextBuilder 完成
2. 上下文数据结构规范化（dataclass）
3. 支持灵活的上下文组合（模块化）
4. 易于扩展和维护

## 重构步骤

### Phase 1：扩展 ContextBuilder 核心功能
**文件**：`rvv_agent/agent/context_builder.py`

1. 新增 dataclass：
   - `PatchContext`：PATCH 阶段所需的全部上下文
   - `DebugContext`：DEBUG 阶段所需的全部上下文
   - `IntentContext`：INTENT 阶段所需的全部上下文

2. 新增方法：
   - `build_patch_context()`：整合 analysis、errors、KB、prior code
   - `build_debug_context()`：整合 error、history、KB fixes、current patch
   - `build_intent_context()`：整合 user input、task state

3. 新增工具方法：
   - `_format_section()`：统一的上下文段落格式化
   - `_truncate_with_marker()`：智能截断（保留关键信息）

### Phase 2：重构 prompts_patch.py
**文件**：`rvv_agent/core/prompts_patch.py`

1. 改造 `patch_generate_prompt()`：
   - 接收 `PatchContext` dataclass 而非多个参数
   - 内部调用 ContextBuilder 的格式化方法
   - 保持输出 prompt 不变

2. 改造 `debug_classify_prompt()`：
   - 接收 `DebugContext` dataclass
   - 同上

### Phase 3：更新 agent 调用
**文件**：`rvv_agent/agent/patch.py`、`debug.py`、`analyze.py` 等

1. `patch.py`：
   - 创建 ContextBuilder 实例
   - 调用 `build_patch_context()` 收集数据
   - 传递给 `patch_generate_prompt()`

2. `debug.py`：
   - 创建 ContextBuilder 实例
   - 调用 `build_debug_context()`
   - 传递给 `debug_classify_prompt()`

3. `analyze.py`：
   - 已有 `build_analyze_context()` 调用，保持不变或优化

### Phase 4：验证和优化
1. 运行现有测试，确保输出 prompt 不变
2. 检查上下文截断是否合理
3. 性能测试（KB 查询、文件读取）

## 预期收益

- ✅ 上下文构建逻辑集中，易于维护
- ✅ 支持灵活的上下文组合（可选 KB、可选错误等）
- ✅ 减少代码重复
- ✅ 便于后续添加新的上下文源（如 repo_analyze、session state 等）
- ✅ 便于测试和调试

## 实施顺序

1. Phase 1：扩展 ContextBuilder（新增方法和 dataclass）
2. Phase 2：改造 prompts_patch.py（接收 dataclass）
3. Phase 3：更新 patch.py、debug.py 调用
4. Phase 4：验证测试

---

**预计工作量**：中等（2-3 小时）
**风险**：低（改造是内部重构，输出 prompt 保持不变）
