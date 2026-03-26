# EvoTransAsm

面向 FFmpeg 的 RVV (RISC-V Vector) 迁移命令行工具。当前版本采用状态机驱动的多阶段流水线，支持：

- `chat`：交互式 human-in-the-loop 迁移
- `migrate`：非交互式批处理迁移
- `plan`：打印固定计划模板的辅助命令（当前实现存在已知问题，见下文）

工具覆盖从参考文件检索、函数发现、计划生成、语义分析、补丁生成与注入、构建验证、失败调试到知识库更新的完整闭环，并将过程产物完整写入 `runs/`。

## 核心能力

- **状态机执行**：统一使用 `StateMachine` 驱动迁移阶段流转。
- **双模式入口**：支持交互式 `chat` 与非交互式 `migrate`。
- **分层文件检索**：围绕 `symbol/module` 选择 C、x86、ARM、RISC-V、头文件、Makefile、checkasm 等参考文件。
- **函数发现**：在 `FUNC_DISCOVER` 阶段识别模块内可迁移函数，并生成 `function_order`。
- **计划阶段**：
  - `chat` 模式优先尝试 `llm_plan()`，失败则回退到 `fixed_plan()`。
  - `migrate` 模式当前直接使用 `fixed_plan()`。
- **语义分析**：调用 LLM 生成结构化分析 JSON，描述向量化模式、数据类型、尾处理需求等。
- **补丁生成**：`PATCH` 阶段采用 `locate -> design -> generate -> apply` 四步流程。
- **失败调试**：`BUILD` 失败后进入 `DEBUG`，分类错误并给出回滚目标，驱动补丁重试。
- **知识库积累**：成功 pattern 与错误修复经验会写入 `knowledge_base.json`。
- **完整落盘**：每轮执行都在 `runs/<task_id>_<symbol>/` 下保留状态文件、日志、分析结果、报告和轨迹。

## 当前仓库结构

```text
bin/
  rvv-agent

rvv_agent/
  __init__.py
  cli.py
  pipeline.py
  agent/
    analyze.py
    chat.py
    debug.py
    inject.py
    intent.py
    patch.py
    plan.py
    report.py
    search.py
  core/
    config.py
    llm.py
    prompts.py
    prompts_patch.py
    statemachine.py
    task.py
    util.py
  memory/
    knowledge_base.py
    pattern_lib.py
  tool/
    board.py
    exec.py
    interactive.py

knowledge_base.json
rvv_agent.toml
runs/
workplace/
```

说明：仓库当前实际代码中不存在 `core/context.py`、`core/task.py` 之外的独立 artifact schema 文件；README 内容以现有源码为准。

## 状态机流程

### chat 模式

```text
INTENT
  -> SEARCH_FILE
  -> FUNC_DISCOVER
  -> BUILD_REFERENCE
  -> PLAN
  -> ANALYZE
  -> PATCH
  -> BUILD
  -> DEBUG (失败时重试)
  -> KB_UPDATE
  -> TASK_UPDATE
  -> DONE
```

### migrate 模式

`migrate` 与 `chat` 共享同一套状态机基础设施，但 handler 组合不同：

```text
INTENT
  -> SEARCH_FILE
  -> FUNC_DISCOVER
  -> BUILD_REFERENCE
  -> ANALYZE
  -> PLAN
  -> PATCH
  -> BUILD
  -> DEBUG
  -> KB_UPDATE
  -> TASK_UPDATE
```

注意：在 `pipeline.py` 中注册 handler 的顺序不影响状态迁移本身，真正流向由各 handler 内部设置的 `task.current_state` 决定。

## 安装与配置

### 1. 配置文件

编辑 `rvv_agent.toml`：

```toml
[llm]
base_url    = "https://your-endpoint/v1"   # 若未以 /chat/completions 结尾，会自动补全
model       = "gpt-4o-mini"
api_key_env = "LLM_API_KEY"
temperature = 0.2
# 可选：用于 trajectory 成本估算
# cost_per_1m_input_tokens = 0
# cost_per_1m_output_tokens = 0

[toolchain]
cross_prefix = "riscv64-unknown-linux-gnu-"
arch = "riscv64"
target_os = "linux"
cpu = "rv64gcv"
extra_cflags = "-march=rv64gcv -mabi=lp64d -O3"
extra_ldflags = "-static"
extra_path = "/path/to/riscv-toolchain/bin"

[ffmpeg]
root = "workplace/FFmpeg"
build_dir = "build"
# configure_path = "workplace/FFmpeg/configure"
# configure_extra_args = ["--disable-everything"]

[board]
enabled = false
user = ""
host = ""
port = 22
remote_dir = "workplace"

[human]
# null 表示运行时询问；true/false 表示跳过询问直接执行
# apply_ok = true
# exec_ok = false
# scp_ok = false
# run_onboard_ok = false
scp_password = ""
```

### 2. 配置 API Key

```bash
export LLM_API_KEY='sk-...'
```

## 使用方式

### 1. 交互模式

```bash
./bin/rvv-agent chat
# 或
python -m rvv_agent.cli chat
```

特点：

- 保留多轮问答上下文。
- 用户输入触发迁移后进入状态机。
- 在参考文件确认、plan 确认等阶段支持人工介入。
- 每轮结束后生成 `report.md` 与 `trajectory.json`。

### 2. 非交互模式

```bash
./bin/rvv-agent migrate <symbol>
./bin/rvv-agent migrate <symbol> --apply
./bin/rvv-agent migrate <symbol> --exec
./bin/rvv-agent migrate <symbol> --apply --exec -j 16
```

参数说明：

- `--apply`：把补丁应用到 FFmpeg workspace；否则只写入 `runs/`。
- `--exec`：执行 `configure + make checkasm`。
- `--ffmpeg-root`：临时覆盖 `ffmpeg.root`。
- `-j/--jobs`：并行构建线程数；`<=0` 时自动取 CPU 核数。

### 3. plan 子命令

```bash
./bin/rvv-agent plan <symbol>
# 或
python -m rvv_agent.cli plan <symbol>
```

**当前已知问题**：`cli.py` 中 `cmd_plan()` 调用了 `fixed_plan()`，但该函数未被导入，因此命令会触发：

```text
NameError: name 'fixed_plan' is not defined
```

也就是说：

- `plan` 子命令的设计意图是“打印固定计划模板”；
- 但当前仓库实现里它**不能正常工作**；
- `migrate` 流程本身不受此问题影响，因为 `pipeline.py` 已正确从 `agent.plan` 导入 `fixed_plan`。

## Plan 阶段实现现状

### chat 模式：偏软编码

`rvv_agent/agent/chat.py` 的 `handle_plan()` 会：

1. 从 `FUNC_DISCOVER` 读取发现的函数列表；
2. 调用 `rvv_agent/agent/plan.py` 中的 `llm_plan()`；
3. 若 LLM 失败或返回格式异常，再回退到 `fixed_plan()`；
4. 支持用户对 plan 做 refine，并将 `refine_history` 写入 artifact。

因此 `chat` 中的 `PLAN` 阶段是：

- **以 LLM 动态生成计划为主**；
- **以固定模板作为兜底**；
- 属于“**软编码为主，硬编码兜底**”。

### migrate 模式：偏硬编码

`rvv_agent/pipeline.py` 中的 `_handle_plan_pipeline()` 当前直接：

1. 读取 `FUNC_DISCOVER` 的函数名；
2. 若为空则退化为 `task.target.functions` 或 `[symbol]`；
3. 调用 `fixed_plan(symbol)`；
4. 再把 `function_order` 覆盖为发现到的函数列表。

因此 `migrate` 中的 `PLAN` 阶段是：

- **固定模板主导**；
- 没有调用 `llm_plan()`；
- 属于“**硬编码 plan + 动态函数顺序同步**”。

### 结论

如果问“现有 plan 阶段是硬编码还是软编码”，准确答案是：

- **整体上是混合式设计**；
- **chat 模式偏软编码**；
- **migrate 模式偏硬编码**；
- **兜底策略明确依赖固定模板**。

## 运行产物

每次运行目录示例：

```text
runs/<task_id>_<symbol>/
```

常见文件：

- `retrieval_raw.txt`：参考文件筛选原始输出。
- `context.txt`：供分析/生成使用的代码上下文。
- `analysis.json`：语义分析结果。
- `build_log.txt`：构建失败摘要，可随调试轮次追加。
- `report.md`：最终运行报告。
- `trajectory.json`：LLM 与 action 轨迹、token/cost 统计。
- `state/task.json`：状态机运行时 manifest。
- `state/PLAN.json`、`state/SEARCH_FILE.json` 等：阶段 artifact。
- `state/BUILD/<id>.json`、`state/PATCH/<id>.json`：按子任务或轮次持久化的阶段输出。

## 核心数据结构现状

定义集中在 `rvv_agent/core/task.py`。

当前已存在的关键 dataclass 包括：

- `MigrationTarget`
- `MigrationTask`
- `FileSearchArtifact`
- `ReferenceCodeArtifact`
- `FuncDiscoverArtifact`
- `AnalysisArtifact`
- `PlanArtifact`
- `PatchArtifact`
- `BuildArtifact`
- `DebugArtifact`
- `KBUpdateArtifact`
- `TaskUpdateArtifact`
- `ArtifactIndex`
- `TaskContext`

其中：

- `PlanArtifact` **已经是 `@dataclass`**；
- `FuncDiscoverArtifact` **也已经是 `@dataclass`**；
- 但 `FuncDiscoverArtifact.functions` 当前类型是 `list[dict]`，并未收敛到独立的 `DiscoveredFunction` 类；
- `PlanArtifact` 当前字段较精简，仅包含：
  - `steps`
  - `function_order`
  - `acceptance_criteria`
  - `refine_history`

## 是否有必要把 PlanArtifact 和 function 相关类改成你给出的 dataclass 版本？

### 1. PlanArtifact：不属于“必须重构”

因为当前仓库里的 `PlanArtifact` 本身已经是 dataclass，所以问题不在“要不要 dataclass 化”，而在于：

- 是否要**扩展 schema**；
- 是否要为未来的 plan 编排保留更强表达能力。

你给出的版本新增了：

- `plan_id`
- `groups`
- `rationale`

这些字段在当前代码路径里：

- **没有被核心逻辑消费**；
- `task.save_artifact("PLAN", ...)` 也不依赖这些字段；
- 报告与后续分析阶段目前主要只读 `steps / function_order / refine_history / acceptance_criteria`。

所以：

- **短期没有强必要修改**；
- 如果你准备把 `PLAN` 阶段升级为“函数分组编排 / 多函数依赖调度 / 可解释 plan rationale”，那么扩展 dataclass 是合理的；
- 否则当前改动主要是“schema 预埋”，收益有限。

### 2. DiscoveredFunction / FunctionGroup：有一定价值，但属于“结构优化”

你给出的：

- `DiscoveredFunction`
- `FuncDiscoverArtifact(functions: list[DiscoveredFunction])`
- `FunctionGroup`

相比当前 `list[dict]` 的优势：

- 字段更明确，类型更稳定；
- `discover_functions()` / `handle_func_discover()` / `handle_plan()` 中少写 `f.get(...)`；
- 为依赖分析、函数分组、排序、报告展示提供统一结构；
- `TaskContext.save_artifact()` 已支持 dataclass 的 `asdict()`，落盘兼容性较好。

但当前代码中大量读取方式是：

```python
for f in artifact.functions:
    name = f.get("name", "")
```

以及：

```python
func_discover.get("functions", [])
```

所以如果引入新的 dataclass 列表，通常还需要同步修改：

- `agent/analyze.py::discover_functions`
- `agent/chat.py::handle_func_discover`
- `agent/chat.py::handle_plan`
- `pipeline.py::_handle_plan_pipeline`
- 可能还包括报告、调试或未来新增逻辑

因此它是：

- **值得做的类型收敛优化**；
- 但**不是当前功能正确性的阻塞项**；
- 更适合在你准备继续演进“函数分组 / 依赖编排 / multi-function migration”时一并做。

### 3. 推荐判断

#### 建议暂不修改的情况

如果你的目标只是：

- 修正文档；
- 判断 plan 是硬编码还是软编码；
- 保持当前 pipeline 稳定；

那么建议：

- **先不要改 PlanArtifact / function schema**；
- 先修复 `cli.py` 的 `fixed_plan` 导入问题；
- 如果要增强 plan，再做一轮结构升级。

#### 建议可以修改的情况

如果你的下一步计划包括：

- 一个 symbol 下迁移多个函数；
- 按依赖/相似性对函数分组；
- plan 阶段生成可解释 rationale；
- 后续让 patch/build/debug 以 group 为粒度推进；

那就建议：

- **把 `DiscoveredFunction` / `FunctionGroup` / 扩展版 `PlanArtifact` 一起引入**；
- 不只是“为了 dataclass 而 dataclass”，而是让 plan/function schema 服务于后续编排能力。

## 建议的优先级

按投入产出比，建议优先级如下：

1. **高优先级**：修复 `cli.py` 中 `plan` 子命令未导入 `fixed_plan` 的问题。
2. **中优先级**：如果 plan 要继续演进，先把 `FuncDiscoverArtifact.functions` 从 `list[dict]` 收敛为 `list[DiscoveredFunction]`。
3. **中/低优先级**：只有在真正需要“函数分组编排”时，再扩展 `PlanArtifact.groups / rationale / plan_id`。
4. **低优先级**：如果近期不会消费这些字段，就不要为了形式统一而扩大 schema。

## 已知限制

- `plan` 子命令当前不可用，需修复导入。
- `migrate` 模式的计划阶段仍主要依赖 `fixed_plan()`，可解释性和适应性弱于 `chat` 模式。
- `FuncDiscoverArtifact` 仍使用 `list[dict]`，类型约束较弱。
- LLM 生成的补丁与分析结果仍需人工审核，尤其是汇编 ABI、寄存器约束、尾处理和精度相关细节。

## 建议的下一步

如果你准备继续改仓库，我建议按这个顺序推进：

1. 修复 `cli.py` 对 `fixed_plan` 的导入；
2. 决定 `migrate` 的 `PLAN` 阶段是否也要接入 `llm_plan()`；
3. 若要做多函数/依赖调度，再引入 `DiscoveredFunction` / `FunctionGroup` / 扩展版 `PlanArtifact`；
4. 为新 schema 补充 artifact 兼容读取逻辑与 README 同步说明。
