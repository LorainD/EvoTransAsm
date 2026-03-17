# rvv-agent chat report

## Symbol

- h264pred.pred16x16
- task_id: 20260313_073723

## Plan

01. 定位 C 实现入口与具体子算子：在 FFmpeg 源码中搜索 "pred16x16" 和 "H264PredContext"，确认 pred16x16 的实际含义是 H264PredContext.pred16x16[4]（16x16 luma intra 预测的 4 个 mode：VERT/HOR/DC/PLANE）。重点定位：libavcodec/h264pred.c（初始化/函数指针赋值）、libavcodec/h264pred_template.c（常见会以模板实现 8-bit/高位深版本；检查是否存在 16x16 的各 mode C 版本，如 pred16x16_vertical_c 等），并记录每个 mode 的函数签名（通常形如 void (*)(uint8_t *src, ptrdiff_t stride) 或含 top/left 引用的变体）。
02. 定位参考 SIMD 实现（x86/ARM）以对齐语义与边界条件：在 libavcodec/x86/ 下搜索 "h264pred"/"pred16x16"（常见文件名：h264pred.asm、h264pred_init.c），确认 x86 优化覆盖了哪些 mode（vert/hor/dc/plane）以及是否区分 8-bit/10-bit。再在 libavcodec/arm/ 下搜索 neon 版本（常见文件名：h264pred_neon.S、h264pred_init_arm.c）。逐个对照其输入依赖（top 行、left 列、top-left 像素、stride）、DC 取平均的取整方式、plane 模式的系数计算与裁剪（clip_uint8/clip_pixel），并记录任何对边界（缺 top/left）的处理是否在上层已保证。
03. 明确要迁移的精确目标集合：将 "h264pred.pred16x16" 拆解为 4 个 RVV 目标函数（pred16x16_vertical_rvv、pred16x16_horizontal_rvv、pred16x16_dc_rvv、pred16x16_plane_rvv），并确定先覆盖 8-bit (H.264 8-bit) 路径；若模板同时支持 high bit depth（10/12-bit），则为后续预留对应实现（例如使用 e16 向量与不同 clip）。同时确认这些函数最终应赋值到 H264PredContext.pred16x16[mode]（而不是直接导出单一符号）。
04. 设计 RVV 向量化策略（按 mode 精确到操作）：(1) VERT：读取 top 行 16 字节，使用 RVV vle8/vse8 直接存到连续的 16 字节块，并循环写 16 行（或利用 strided store：vsse8 以 stride 写 16 行；若实现更复杂可选择行循环 + vse8）。(2) HOR：逐行读取对应 left 像素（通常位于 src[-1 + y*stride]），对每行用 vdup (vmv.v.x / vfmv) 生成 16 个相同字节再 vse8。 (3) DC：按 spec 计算 top[16] 与 left[16] 之和并按规则 (sum + 16) >> 5（若上下均可用）或其他分支，使用 RVV 做向量 reduce-sum（vredsum）或手动分块累加（例如 16x u8 扩展到 u16/u32），得到 dc 值后同 HOR 用 vdup+vse8 填充 16x16。 (4) PLANE：实现参考 SIMD 的数学流程：计算 H/V 梯度（top/right、left/bottom 的加权差）、a/b/c 系数、按 x/y 生成像素并 clip 到 [0,255]；RVV 中可用 i16/i32 向量做 x 方向的递增序列（vid.v 生成 0..15），计算每行 base + x*b，再随 y 更新 base（+c），最后 clip 并转 u8 存储。对照 x86/NEON 保证舍入、右移与 clip 一致。
05. 生成 RVV 实现文件：在 libavcodec/riscv/ 新增实现文件（建议 C intrinsics：h264pred_rvv.c；或手写汇编：h264pred_rvv.S）。在文件内包含必要头（riscv_vector.h、config.h、libavutil/attributes.h 等），为每个 mode 写静态函数，使用 vsetvl_e8m1(16) 或 vsetvl_e16m1(16) 确保固定 16 lane 逻辑；处理 stride 为任意值（非对齐/非 16 倍数）时，避免假设对齐；plane 中使用更宽类型避免溢出，并在最终收敛到 u8 前做 clip（可用 min/max 向量指令模拟 clip）。
06. 在 RISC-V init 中挂接到 h264pred.pred16x16：定位或创建 libavcodec/riscv/h264pred_init.c（若已有 riscv 初始化文件则复用）。实现类似 ff_h264_pred_init_riscv(H264PredContext *h, int codec_id, int bit_depth, const int chroma_format_idc) 的函数（以现有架构 init 形式为准），在满足条件（bit_depth==8 且 RVV 可用）时，将 h->pred16x16[VERT/HOR/DC/PLANE] 指向 *_rvv 版本；若只实现部分 mode，保留未实现的 mode 继续指向 C 版本以保证功能完整。
07. 集成到构建系统（Makefile + configure）：(1) 在 libavcodec/riscv/Makefile 中为目标对象添加条目，例如 OBJS-$(CONFIG_H264PRED) += riscv/h264pred_rvv.o riscv/h264pred_init.o（按 FFmpeg 现有分组规则可能是 OBJS-$(HAVE_RVV) 或 OBJS-$(CONFIG_H264_DECODER) 的更细粒度项）。(2) 确认 configure 能检测 RVV（通常通过 --enable-rvv 或基于 -march=rv64gcv 的编译测试设置 HAVE_RVV），必要时在 configure/arch 逻辑里为 riscv 添加 rvv 相关开关。 (3) 确保在 libavcodec/allcodecs.c 或架构 init 调用链中，riscv 的 ff_h264_pred_init_riscv 会被调用（通常在 libavcodec/h264pred.c 的 ff_h264_pred_init 里按架构条件调用）。
08. 补齐/更新 checkasm 覆盖并验证：定位 tests/checkasm/ 中的 h264pred 测试文件（常见为 tests/checkasm/h264pred.c）。确认测试确实会覆盖 H264PredContext.pred16x16 的 4 个 mode，并在 RISC-V 上能走到 RVV 指针（通常 checkasm 会通过 init 获取函数指针并对比 ref/opt 输出）。若当前测试未覆盖 16x16 plane/DC 或缺少随机边界数据，扩展测试：为 stride 设置多种值、为 src 周围填充 guard 区、为 top/left 数据生成随机值并确保不越界。运行方式：交叉或本机编译后执行 "make checkasm" 或 "./ffmpeg_g -hide_banner -loglevel error -cpuflags rvv -checkasm"（以工程实际 checkasm 入口为准），并确保对比基准为 C 实现且输出一致。
09. 性能与回归检查（针对 pred16x16 的指标化）：在同一 RISC-V 平台上分别运行开启/关闭 RVV 的 checkasm benchmark（checkasm 通常带 timing 选项），观察 pred16x16 四个 mode 的周期改善；并用 H.264 解码回归（FATE 或本地样本）确认画面无块效/条纹（plane 误差最敏感）。若出现差异，优先对齐 x86/NEON 的舍入与 clip 细节，特别是 DC 的加法偏置与 plane 的移位/偏置常量。

## Reference Files

- libavcodec/h264pred.c
- libavcodec/x86/h264pred.asm
- libavcodec/x86/h264pred_init.c
- libavcodec/arm/h264pred_init_arm.c
- libavcodec/arm/h264pred_neon.S
- libavcodec/arm/h264pred_arm.S
- libavcodec/riscv/h264pred_init.c
- libavcodec/riscv/h264pred_rvv.S
- libavcodec/h264pred.h
- libavcodec/avcodec.h
- libavutil/attributes.h
- libavutil/common.h
- libavutil/cpu.h
- libavcodec/Makefile
- libavcodec/x86/Makefile
- libavcodec/arm/Makefile
- libavcodec/riscv/Makefile
- tests/checkasm/checkasm.c
- tests/checkasm/h264pred.c
- tests/checkasm/Makefile

## Analysis

```json
{
  "symbol": "h264pred.pred16x16",
  "datatype": "mixed",
  "vectorizable": true,
  "pattern": [
    "horizontal_add",
    "stride_load",
    "saturate"
  ],
  "has_stride": true,
  "has_saturation": true,
  "reduction": true,
  "tail_required": false,
  "math_expression": "DC: dc = (sum_{i=0..15}top[i] + sum_{i=0..15}left[i] + 16) >> 5; for y=0..15 for x=0..15 dst[y*stride+x]=dc. TOP_DC: dc=(sum_{i=0..15}top[i]+8)>>4; fill 16x16 with dc. LEFT_DC: dc=(sum_{i=0..15}left[i]+8)>>4; fill 16x16 with dc. 128_DC: fill 16x16 with 128. VERT: for y=0..15 memcpy(dst+y*stride, top, 16). HOR: for y=0..15 for x=0..15 dst[y*stride+x]=left[y]. PLANE: a=16*(top[15]+left[15]); b=(5*sum_{i=0..7}(i+1)*(top[8+i]-top[6-i])+32)>>6; c=(5*sum_{i=0..7}(i+1)*(left[8+i]-left[6-i])+32)>>6; for y=0..15 for x=0..15 val=(a + b*(x-7) + c*(y-7) + 16)>>5; dst[y*stride+x]=clip(val,0,(1<<bit_depth)-1)",
  "c_candidates": [
    "libavcodec/h264pred.c:36",
    "libavcodec/h264pred.c:40",
    "libavcodec/h264pred.c:44",
    "libavcodec/h264pred.c:48",
    "libavcodec/h264pred.c:52",
    "libavcodec/h264pred.c:515",
    "libavcodec/h264pred.c:516",
    "libavcodec/h264pred.c:517",
    "libavcodec/h264pred.c:518",
    "libavcodec/h264pred.c:519",
    "libavcodec/h264pred.c:520",
    "libavcodec/h264pred.c:521"
  ],
  "x86_refs": [],
  "arm_refs": [
    "libavcodec/arm/h264pred_neon.S:45",
    "libavcodec/arm/h264pred_neon.S:50",
    "libavcodec/arm/h264pred_neon.S:59"
  ],
  "notes": "pred16x16 是一组 16x16 帧内预测模式（DC/VERT/HOR/PLANE/LEFT_DC/TOP_DC/128_DC 等），在 h264pred_template.c 内按 BIT_DEPTH=8/9/10/12/14 生成不同实现；计算通常用更宽的整型做累加/梯度（reduction），最终写回像素类型（8bit 为 uint8_t，高位深常为 uint16_t）且 PLANE 需要按 bit_depth 做 clip。该类函数的向量化关键点：1) 16-byte 顶边/左边加载；2) 对 16 元素求和（水平归约）；3) 填充 16x16（按 stride 存储）；4) PLANE 模式可向量化 x 方向的线性表达式并按行递推，注意裁剪到 [0, (1<<bit_depth)-1]。"
}
```

## Patch: h264pred.pred16x16

- /home/yuhe/project/cmdTool/workplace/FFmpeg/libavcodec/riscv/h264pred_rvv.S

## Build (make, rc=0)

```
$ make -j128 tests/checkasm/checkasm
```

## Result

- build_success: True
- debug_cycles: 0
