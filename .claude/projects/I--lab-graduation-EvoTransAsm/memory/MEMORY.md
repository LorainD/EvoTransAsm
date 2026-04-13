# Memory

## Context Builder 重构完成 (2026-04-13)

已完成 Phase 1-3 的 Context Builder 统一体系重构：

### 改动内容
1. **context_builder.py** - 新增 dataclass 和方法
   - `PatchContext`: PATCH 阶段上下文数据结构
   - `DebugContext`: DEBUG 阶段上下文数据结构
   - `build_patch_context()`: 从 task artifacts 构建 PatchContext
   - `build_debug_context_full()`: 从 task artifacts 构建 DebugContext
   - `_format_section()`: 统一的上下文段落格式化工具

2. **prompts_patch.py** - 改造为双 API 支持
   - `patch_generate_prompt()` 接收 `PatchContext | str`（新 API | 旧 API）
   - `debug_classify_prompt()` 接收 `DebugContext | str`（新 API | 旧 API）
   - 完全向后兼容，旧代码无需改动

3. **patch.py** - 更新调用方式
   - `generate_code()` 中创建 `PatchContext` 并传递给 `patch_generate_prompt()`
   - `_route_apply_failure()` 中创建 `DebugContext` 并传递给 `debug_classify_prompt()`

4. **debug.py** - 更新调用方式
   - `_llm_classify()` 中创建 `DebugContext` 并传递给 `debug_classify_prompt()`

### 收益
- ✅ 上下文构建逻辑集中，易于维护
- ✅ 支持灵活的上下文组合（可选 KB、可选错误等）
- ✅ 减少代码重复（参数从 10+ 个减少到 1 个 dataclass）
- ✅ 完全向后兼容（旧 API 仍可用）
- ✅ 便于后续扩展（新增上下文源只需改 ContextBuilder）

### 验证
- ✅ 所有文件编译通过
- ✅ 新 API 和旧 API 都能正常生成 prompt
- ✅ 所有调用点已更新
