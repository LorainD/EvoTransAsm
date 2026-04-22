# EvoTransAsm

面向 FFmpeg 的 RVV (RISC-V Vector) 迁移命令行工具，采用状态机驱动的多阶段流水线。

当前支持命令：

- chat：交互式 human-in-the-loop 迁移
- migrate：非交互式批处理迁移
- plan：打印固定迁移计划模板
- repo_analyze / analyze：分析已有 RISC-V 实现并生成仓库知识 JSON

工具覆盖从参考文件检索、函数发现、计划生成、语义分析、补丁生成与注入、构建验证、失败调试、板端测试到知识库更新的闭环流程，并将产物写入 runs 目录。

## 核心能力

- 状态机执行：统一使用 StateMachine 管理阶段跳转
- 双模式入口：chat 与 migrate 共用一套核心 handler 体系
- 函数级迁移：支持函数发现、依赖建模、分组迁移顺序
- 计划生成：chat 以 llm_plan 为主、fixed_plan 兜底；migrate 使用 fixed_plan
- 构建与调试：支持 configure + make checkasm，失败进入 DEBUG 自动分类与重试
- 失败兜底：异常或失败状态可触发补丁回滚与快速健康检查
- 板端测试：board 启用时可在 TEST 阶段执行远端准备、传输与运行
- 知识沉淀：KB_UPDATE 将成功模式和错误修复经验写入知识库
- 轨迹记录：每次运行保留 trajectory.json、报告与分阶段 artifact

## 仓库结构

```text
bin/
  rvv-agent

rvv_agent/
  __init__.py
  cli.py
  pipeline.py
  repo_analyze.py
  agent/
    analyze.py
    chat.py
    context_builder.py
    debug.py
    intent.py
    patch.py
    plan.py
    report.py
    search.py
  core/
    config.py
    ir.py
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

rvv_agent.toml
runs/
workplace/
```

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
  -> TEST (board 启用时)
  -> DEBUG (失败时重试)
  -> KB_UPDATE
  -> TASK_UPDATE
  -> DONE
```

### migrate 模式

```text
INTENT
  -> SEARCH_FILE
  -> FUNC_DISCOVER
  -> BUILD_REFERENCE
  -> PLAN
  -> ANALYZE
  -> PATCH
  -> BUILD
  -> TEST (board 启用时)
  -> DEBUG (失败时重试)
  -> KB_UPDATE
  -> TASK_UPDATE
  -> DONE
```

说明：状态迁移由各 handler 设置 task.current_state 决定，handler 注册顺序不等于执行顺序。

## 配置

编辑 rvv_agent.toml：

```toml
[llm]
base_url = "https://your-endpoint/v1"
model = "gpt-4o-mini"
api_key_env = "LLM_API_KEY"
temperature = 0.2
default_max_tokens = 4096

[llm.stage_max_tokens]
intent = 1024
retrieve = 2048
func_discover = 2048
analyze = 8192
plan = 4096
patch_generate = 8192
debug = 4096
chat = 2048

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

[board]
enabled = false
user = ""
host = ""
port = 22
remote_dir = "workplace"

[human]
# 配置为 true/false 表示跳过询问，未配置则运行时交互确认
# apply_ok = true
# exec_ok = false
# scp_ok = false
# run_onboard_ok = false
scp_password = ""

[features]
enable_tool_use_patch_loop = false
```

设置 API Key：

```bash
export LLM_API_KEY='sk-...'
```

Windows PowerShell：

```powershell
$env:LLM_API_KEY = "sk-..."
```

## 使用方式

### 1) chat 交互模式

```bash
./bin/rvv-agent chat
# 或
python -m rvv_agent.cli chat
```

### 2) migrate 非交互模式

```bash
./bin/rvv-agent migrate <symbol>
./bin/rvv-agent migrate <symbol> --apply
./bin/rvv-agent migrate <symbol> --exec
./bin/rvv-agent migrate <symbol> --apply --exec -j 16
./bin/rvv-agent migrate <symbol> --ffmpeg-root /path/to/FFmpeg
```

参数说明：

- --apply：将补丁应用到 FFmpeg 工作区，否则仅写入 runs
- --exec：执行 configure + make checkasm
- --ffmpeg-root：临时覆盖 ffmpeg.root
- -j/--jobs：构建并行度，<=0 时自动使用 CPU 核数

### 3) plan 子命令

```bash
./bin/rvv-agent plan <symbol>
# 或
python -m rvv_agent.cli plan <symbol>
```

说明：该命令当前可正常工作，会打印 fixed_plan 生成的步骤列表。

### 4) repo_analyze / analyze

```bash
./bin/rvv-agent repo_analyze <symbol>
./bin/rvv-agent analyze <symbol>
./bin/rvv-agent repo_analyze --all-riscv --output repo_analyze.json
```

参数说明：

- <symbol>：目标符号或模块（与 --all-riscv 二选一）
- --all-riscv：扫描 FFmpeg 中已有的全部 RISC-V 实现并批量分析
- --output：输出 JSON 路径，默认 repo_analyze.json

## 运行产物

每次执行会在如下目录生成产物：

```text
runs/<task_id>_<symbol_or_cmd>/
```

常见文件：

- session_print.txt：本次执行标准输出镜像日志
- retrieval_raw.txt：文件检索原始输出
- context.txt：参考代码拼接上下文
- analysis.json：语义分析结果
- build_log.txt：构建失败摘要（存在失败时）
- report.md：最终报告
- trajectory.json：LLM 与 action 轨迹、token/cost 统计
- state/task.json：状态机运行 manifest
- state/PLAN.json、state/SEARCH_FILE.json 等阶段 artifact
- state/PATCH/<id>.json、state/BUILD/<id>.json、state/DEBUG/<id>.json 等分轮次 artifact

## 核心数据结构

定义集中在 rvv_agent/core/task.py。主要 dataclass 包括：

- MigrationTarget、MigrationTask、FunctionTask
- DiscoveredFunction、FunctionGroup
- FileSearchArtifact、ReferenceCodeArtifact、FuncDiscoverArtifact
- FunctionAnalysis、AnalysisArtifact、PlanArtifact
- PatchArtifact、BuildArtifact、DebugArtifact
- KBUpdateArtifact、TaskUpdateArtifact
- ArtifactIndex、TaskContext

其中 PlanArtifact 已包含分组迁移与进度字段（groups、current_group_idx、completed_groups、failed_groups），可直接支持多函数分组推进。

## 已知限制

- migrate 的 PLAN 阶段当前默认使用 fixed_plan，未接入 llm_plan
- LLM 生成结果仍需人工审核，特别是 ABI 约束、寄存器使用、尾处理和精度相关逻辑
- board 测试依赖远端环境可用性，网络与工具链问题会影响 TEST 阶段稳定性
