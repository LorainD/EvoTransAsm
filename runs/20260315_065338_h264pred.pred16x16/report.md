# rvv-agent chat report

## Symbol

- h264pred.pred16x16
- task_id: 20260315_065338

## Plan

01. 定位 C 实现（作为 bitexact 金标准）：在 FFmpeg 源码中查找 pred16x16_*_c 的定义位置（通常在 libavcodec/h264pred_template.c 通过模板展开生成；实际展开点在 libavcodec/h264pred.c 或同目录相关文件）。命令：rg -n "pred16x16_(dc|vertical|horizontal|plane|left_dc|top_dc|128_dc)_(8|9|10|12|14)_c" libavcodec/。逐 bitdepth 记录：像素类型/stride 单位/边界像素读取方式（top/left 可用性）。
02. 定位函数注册点（建立 mode->symbol 映射）：在 libavcodec/h264pred.c（或 h264pred_init*.c）定位 h264_pred_init()/ff_h264_pred_init() 对 pred16x16 的函数指针表赋值位置（hpc->pred16x16[mode] = ...）。确认 8bpc 与 high bit depth（9/10/12/14）是否走不同 init 分支，并导出每个 mode（DC/V/H/Plane/LeftDC/TopDC/128DC）最终绑定的符号名列表。
03. 以 x86/ARM 实现为主挑选“应该迁移的函数”（优先迁移已在其它 SIMD 架构优化过、且收益高/复杂度低的子集）：
- 全局搜索：rg -n "pred16x16" libavcodec/x86 libavcodec/arm libavcodec/aarch64。
- 归档每个架构实际提供的 pred16x16 优化符号（含 8bpc 与 high bit depth 版本）。
- 迁移选择规则：
  (a) x86 与 arm/aarch64 都有的：优先迁移（说明价值高且行为清晰）；
  (b) 仅某一架构有但实现简单（vertical/horizontal/128_dc）：也迁移；
  (c) 仅某一架构有且复杂（plane 或特殊 DC 变体）：延后；
  (d) 若其它架构根本未优化某个变体（例如某些 left_dc/top_dc/plane 的 high bitdepth 版本缺失），则 RVV 首版可不做该变体，保留 C 回退。
04. 输出“目标函数清单”（本计划的迁移范围）并分批次：
- 批次 A（通常 x86/arm 都覆盖且最易实现）：pred16x16_vertical_*, pred16x16_horizontal_*, pred16x16_128_dc_*。
- 批次 B（其它架构常见优化、但需要归约）：pred16x16_dc_*（以及若 x86/arm 优化了则包含 left_dc/top_dc）。
- 批次 C（高风险/最后，只有在 x86/arm 已明确有 SIMD 优化且收益显著时纳入）：pred16x16_plane_*（以及其对应 high bitdepth 版本）。
说明：若在步骤 3 发现 x86/arm 对某些 bitdepth/模式未提供 SIMD，则 RVV 对应项降级为“不迁移/后续再做”。
05. 确定 RVV 文件与命名规范：新增/编辑 libavcodec/riscv/h264pred_rvv.S（或与现有 riscv SIMD 文件命名一致）。为步骤 4 的“目标函数清单”逐一生成 RVV 符号；命名需与 riscv init 注册习惯一致（例如 ff_pred16x16_vertical_8_rvv / ff_pred16x16_vertical_10_rvv 等），并与步骤 2 中的符号映射保持可一一替换。
06. 生成 RVV 实现（通用策略，对齐不假设）：以 16 像素宽为自然向量长度，vsetvl=16；8bpc 用 e8，high bit depth 用 e16（像素为 uint16_t）。每行一次向量写回，循环 16 行；stride 以字节为单位正确前进。先确保单函数可编译链接，但暂不注册到 init。
07. 实现批次 A-1：pred16x16_vertical_*（仅当 x86/arm 至少一方提供 SIMD 或其为必备高频路径）：从 C 行为确认 vertical=复制 top[0..15] 到 16 行。RVV：一次加载 top 向量（vle8/vle16），循环 16 次 vse 写回，每次 dst+=stride。
08. 实现批次 A-2：pred16x16_horizontal_*（同上挑选规则）：horizontal=用 left[y] 填充该行 16 像素。RVV：每行加载标量 left[y]，广播到向量（vmv.v.x），vse 存 16 像素；循环 16 行。注意 left 步进为 1 像素（8bpc 1 字节，>8bpc 2 字节）。
09. 实现批次 A-3：pred16x16_128_dc_*（同上挑选规则）：常量填充（8bpc=128，>8bpc=1<<(bitdepth-1)）。RVV：构造常量、广播、16 行写回。
10. 实现批次 B：pred16x16_dc_*（仅在步骤 3 证实 x86/arm 对应模式/bitdepth 有 SIMD 或该模式在 pred16x16 中占比高）：严格按 C 的 rounding/shift 与 top/left 可用性规则实现。RVV：对 top 和 left 分别向量归约求和（e8->u16 累加、e16->u32 累加），按 C 规则生成 dc 值并广播写 16x16。逐 bitdepth 核对偏置与右移位数（如 +16>>5 或变体）。
11. 可选实现（取决于步骤 3 的 x86/arm 覆盖情况）：pred16x16_left_dc_* 与 pred16x16_top_dc_*：分别只用 left 或 top 求均值。RVV：单边归约求和，按 C 的 rounding/shift（常见 +8>>4）生成值并广播写回。若 x86/arm 未优化这些变体，则本轮可不迁移，保留 C 回退。
12. 批次 C（最后且可选）：pred16x16_plane_*：只有在步骤 3 确认 x86/arm 对 plane 有成熟 SIMD 且收益显著时才纳入首轮迁移；否则延后。实现时从 C 逐行确认 H/V 梯度与 a/b/c 公式、读取 top[-1]/left[-1] 等邻点、最终 clip。RVV：可先标量算 H/V 与每行 base，再用 vid.v + vmacc 生成等差序列并向量写回；中间用 e32 防溢出，最后裁剪并窄化（8bpc）。
13. 为每个已迁移函数写独立 RVV 单元并逐个验证：每实现一个 RVV 函数，先只加入 riscv 对象编译列表（不注册），本地编译确保无未定义符号/ABI 错误；然后运行 checkasm（tests/checkasm/checkasm --test=h264pred 或可用的过滤方式）确保与 C bitexact。
14. 集成到构建系统（仅添加对象，不注册）：修改 libavcodec/riscv/Makefile（或对应 *.inc），把 h264pred_rvv.S 加入 OBJS-$(CONFIG_H264PRED) 且受 HAVE_RVV/相应条件控制。确保未启用 RVV 时不编译该文件。
15. 逐个注册到 init（按“目标函数清单”与批次顺序）：在 libavcodec/riscv/h264pred_init.c（或现有 riscv init 文件）中添加 RVV 运行时检测（使用项目既有方式），检测通过后仅替换已实现且已通过 checkasm 的 mode/bitdepth 对应函数指针为 *_rvv。遵循：先批次 A，再批次 B，再批次 C；每替换一组提交一次，避免链接/回归风险。
16. 运行回归与覆盖：构建带 checkasm（--enable-checkasm 并启用 RVV）后运行 tests/checkasm/checkasm --test=h264pred；若 checkasm 未覆盖 high bitdepth 的某些模式，则补充 fate/自定义用例或扩展 checkasm 后再注册相应 high bitdepth 入口。
17. 性能与回退策略：若 plane 或某些 DC 变体未迁移（依据 x86/arm 覆盖情况裁剪范围），则保持 C 回退；已迁移函数先保证正确性，后续再逐步优化归约/plane 的向量化程度。

### Refine History

- [plan] 参x86和arm架构下实现的函数，挑选应该迁移的函数

## Reference Files

- libavcodec/h264pred.c
- libavcodec/h264_parse.c
- libavcodec/svq3.c
- libavcodec/x86/h264_intrapred_init.c
- libavcodec/arm/h264pred_init_arm.c
- libavcodec/aarch64/h264pred_init.c
- libavcodec/h264dec.h
- libavcodec/mips/h264pred_mips.h
- libavcodec/vp8.h
- tests/checkasm/h264pred.c
- tests/checkasm/checkasm.c

## Analysis

```json
{
  "symbol": "h264pred.pred16x16",
  "datatype": "mixed",
  "vectorizable": true,
  "pattern": [
    "butterfly",
    "horizontal_add",
    "stride_load",
    "saturate",
    "tail"
  ],
  "has_stride": true,
  "has_saturation": true,
  "reduction": true,
  "tail_required": true,
  "math_expression": "Given dst points to a 16x16 block with row stride 'stride' (in bytes) and pixel bit depth bd (8/9/10/12/14). Let P be pixel type: uint8 if bd<=8 else uint16 (stored via uint8_t* buffer). Let top[x]=P(dst[-stride + x]) for x=0..15 and left[y]=P(dst[y*stride - 1]) for y=0..15.\n\nMode VERT: for y=0..15, for x=0..15: P(dst[y*stride + x]) = top[x].\nMode HOR:  for y=0..15, for x=0..15: P(dst[y*stride + x]) = left[y].\nMode DC:   dc = (sum_{x=0..15} top[x] + sum_{y=0..15} left[y] + 16) >> 5; for y=0..15, for x=0..15: P(dst[y*stride + x]) = dc.\nMode TOP_DC:  dc = (sum_{x=0..15} top[x] + 8) >> 4;  for y=0..15, for x=0..15: P(dst[y*stride + x]) = dc.\nMode LEFT_DC: dc = (sum_{y=0..15} left[y] + 8) >> 4; for y=0..15, for x=0..15: P(dst[y*stride + x]) = dc.\nMode 128_DC:  v = 1<<(bd-1); for y=0..15, for x=0..15: P(dst[y*stride + x]) = v.\nMode PLANE (H.264 plane):\n  H = sum_{i=1..8} i * (top[7+i] - top[7-i]);\n  V = sum_{i=1..8} i * (left[7+i] - left[7-i]);\n  a = 16 * (top[15] + left[15]);\n  b = (5*H + 32) >> 6;\n  c = (5*V + 32) >> 6;\n  for y=0..15, for x=0..15:\n    val = (a + b*(x-7) + c*(y-7) + 16) >> 5;\n    P(dst[y*stride + x]) = clip(val, 0, (1<<bd)-1).",
  "c_candidates": [
    "libavcodec/h264pred.c:515",
    "libavcodec/h264pred.c:516",
    "libavcodec/h264pred.c:517",
    "libavcodec/h264pred.c:518",
    "libavcodec/h264pred.c:519",
    "libavcodec/h264pred.c:520",
    "libavcodec/h264pred.c:521"
  ],
  "x86_refs": [
    "libavcodec/x86/h264_intrapred_init.c:90",
    "libavcodec/x86/h264_intrapred_init.c:94",
    "libavcodec/x86/h264_intrapred_init.c:101",
    "libavcodec/x86/h264_intrapred_init.c:159",
    "libavcodec/x86/h264_intrapred_init.c:201",
    "libavcodec/x86/h264_intrapred_init.c:205",
    "libavcodec/x86/h264_intrapred_init.c:231",
    "libavcodec/x86/h264_intrapred_init.c:267"
  ],
  "arm_refs": [
    "libavcodec/arm/h264pred_init_arm.c:28",
    "libavcodec/arm/h264pred_init_arm.c:48",
    "libavcodec/arm/h264pred_init_arm.c:76"
  ],
  "notes": "pred16x16 is a function-pointer table populated per bit_depth and codec_id; actual C implementations come from h264pred_template.c (included multiple times with BIT_DEPTH=8/9/10/12/14), but its function bodies/line numbers are not present in the provided context. 8-bit has NEON hooks; high-bit-depth returns early in the NEON init (no NEON high-depth here). x86 has many 8-bit and 10-bit intrapred implementations selected via init, but this context only shows prototypes/assignments (not the .asm/.S bodies); to guide RVV, locate the corresponding x86 intrapred assembly source in the tree (commonly under libavcodec/x86/). RVV should handle fixed width 16 with vsetvl: if VLEN<16, loop over x in chunks (tail/last-iteration handling needed). Plane mode requires signed weighted sums (H,V), then per-pixel affine expression and clipping to [0, (1<<bd)-1]."
}
```

## Patch: h264pred.pred16x16

- /home/yuhe/project/cmdTool/workplace/FFmpeg/libavcodec/h264pred.c
- /home/yuhe/project/cmdTool/workplace/FFmpeg/libavcodec/h264pred.c
- /home/yuhe/project/cmdTool/workplace/FFmpeg/libavcodec/riscv/h264pred_init.c
- /home/yuhe/project/cmdTool/workplace/FFmpeg/libavcodec/riscv/h264pred_rvv.S
- /home/yuhe/project/cmdTool/workplace/FFmpeg/libavcodec/riscv/Makefile

## Build (make, rc=2)

```
$ make -j128 tests/checkasm/checkasm
```

## Result

- build_success: False
- debug_cycles: 0
