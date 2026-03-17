# rvv-agent run report

## Symbol

- h264pred.pred16x16

## Plan

01. 在 FFmpeg 源码中定位 h264pred.pred16x16 的 C 侧实现与调用路径：
- grep 符号：git grep -n "pred16x16" libavcodec
- 重点文件通常在 libavcodec/h264pred_template.c、libavcodec/h264pred.c（不同版本可能略有差异）；定位对应的 16x16 预测函数（如 pred16x16_vertical/horizontal/dc/plane 等）以及它们如何被填入 H264PredContext（hpc->pred16x16[mode]）。
- 同时定位写入像素的存储形式（uint8_t *dst, ptrdiff_t stride），确认是否对齐、是否允许 unaligned 访问、是否有边界读（参考实现可能会读 top/left 邻居像素）。
02. 确定 h264pred.pred16x16 的“算子边界”和具体要迁移的子模式集合：
- 在 H.264 Intra16x16 中通常包含 4 种模式：Vertical/Horizontal/DC/Plane；在当前 FFmpeg 版本中确认 pred16x16 数组长度与索引枚举（例如 0..3）。
- 明确本次迁移范围：实现整个 h264pred.pred16x16（即一组函数指针表），还是仅实现最常用的某几种模式；建议一次性覆盖 4 种模式，避免运行时回退与 checkasm 覆盖不全。
03. 定位参考 SIMD 实现（x86/ARM）以复用策略与边界处理：
- x86：git grep -n "pred16x16" libavcodec/x86 ；常见文件为 libavcodec/x86/h264_intrapred.asm 或 h264_intrapred_init.c。
- ARM/NEON：git grep -n "pred16x16" libavcodec/arm ；常见文件为 libavcodec/arm/h264pred_neon.S / h264pred_neon.c。
- 逐个对照 Vertical/Horizontal/DC/Plane 的算法细节：
  - Vertical：把 top[0..15] 复制到 16 行；
  - Horizontal：把 left[0..15] 按行广播到 16 列；
  - DC：对 top 与 left 求和，按规范做 (sum + 16) >> 5（或按 FFmpeg 现有实现的 rounding/可用邻居规则），全块填充；
  - Plane：使用 top[-1], top[0..15], left[-1], left[0..15] 计算 a/b/c 并生成像素；确认是否使用 16 位/32 位中间精度以及 clamp 到 [0,255] 的方式。
- 记录参考实现里对“缺失邻居”的处理（例如在上边或左边不可用时的 DC 取值），以保证 bitexact。
04. 为 RVV 选择实现布局并编写 RVV 代码（以 per-mode 函数实现并填表）：
- 新建文件：libavcodec/riscv/h264pred_rvv.c（或 .S 若偏汇编；建议先用 intrinsics 的 .c 便于调试）。
- 为每个模式实现一个 RVV 版本函数，签名与 C 版本一致（通常为 void fn(uint8_t *dst, ptrdiff_t stride, const uint8_t *top, const uint8_t *left) 或 FFmpeg 当前版本使用的参数形式；以你定位到的实际签名为准）。
- Vertical RVV：
  - vsetvl_e8m1(16)，vlse8 或 vle8 载入 top[0..15] 到 v；
  - 循环 16 次：vse8 写一行到 dst，dst += stride。也可用 unroll + pointer arithmetic；写入量固定 16 字节。
- Horizontal RVV：
  - 对每行 i：读取 left[i] 标量；用 vmv.v.x 广播到 v8(16 lanes)；vse8 写 16 字节；dst += stride。
- DC RVV：
  - 载入 top[0..15]、left[0..15]（若该模式在当前代码会在邻居缺失时改用 128 或只用一侧，需按 C 逻辑分支）；
  - 使用 vwaddu.vx / vwadd.wv 做 widen sum 到 16 位或 32 位，再用 vredsum 求和；
  - 计算 dc = (sum + 16) >> 5（以 C 实现为准）；广播 dc 到向量并写 16x16。
- Plane RVV（重点）：
  - 严格复刻 C/参考 SIMD 的整数公式与舍入方式：通常会计算 H、V 梯度（加权差分），再得到 b,c，以及 a=16*(top[15]+left[15])；
  - RVV 侧采用 16 位/32 位向量中间结果：
    - 生成 x = [0..15]，y = [0..15] 的索引向量（iota），用于 (b*(x-7) + c*(y-7) + a + 16) >> 5 之类的表达式（以你的 C 公式为准）。
    - 对每行 y：计算 row = base + c*(y-7)，再加 b*(x-7) 得到 16 个像素；
    - 算术右移并用 vmax/vmin 做 clamp 到 [0,255]，最后 narrow 到 u8 存储。
  - 确保与参考实现一致的溢出/截断：中间使用 32 位，最终 clamp，再窄化。
- 注意 RVV 的 vsetvl：本算子固定宽度 16，直接 vsetvl(16)；避免可变 VL 带来的循环复杂性。
05. 为 RVV 添加 init 绑定（让 h264pred.pred16x16 指向 RVV 实现）：
- 定位初始化函数：通常为 ff_h264_pred_init(H264PredContext *h, int codec_id, int bit_depth, const int chroma_format_idc) 或类似；在 riscv 目录下创建/修改 init：libavcodec/riscv/h264pred_init_rvv.c。
- 按 FFmpeg 习惯做特性检测：
  - 使用 av_get_cpu_flags() / ff_get_rv_cpu_flags()（以当前 riscv 端实现为准）检测 RVV；
  - 若支持 RVV，则将 h->pred16x16[mode] 覆盖为你的 rvv 函数（四个模式分别赋值）。
- 确保仅在 8-bit 像素路径启用（如果当前 FFmpeg 版本对 10/12-bit 另有代码路径，避免误绑定）。
06. 集成到构建系统：
- 在 libavcodec/riscv/Makefile 中添加对象文件：
  - OBJS-$(CONFIG_H264PRED) += riscv/h264pred_rvv.o riscv/h264pred_init_rvv.o（按现有模式：可能是 OBJS-$(HAVE_RVV) 或在 riscv/Makefile 的 RVV 条件块中添加）。
- 在 configure/arch 相关脚本确认 RVV 检测宏：
  - 确认 HAVE_RVV / HAVE_RVV_INTRINSICS 或 FFmpeg 当前使用的 riscv 特性宏；必要时更新 configure 的 feature test（例如检查编译器支持 -march=rv64gcv）。
- 确保编译参数包含 RVV：在适配的情况下通过 --extra-cflags='-march=rv64gcv -mabi=lp64d'（或 rv32 对应）进行构建；并保证不会影响非 RVV 目标（通过条件编译 #if HAVE_RVV）。
07. 添加/更新 checkasm 用例并运行验证（必须覆盖 pred16x16 的各模式）：
- 定位 checkasm：tests/checkasm/h264pred.c（或 tests/checkasm/h264pred.c/h264pred_init.c，按版本）。
- 确认 checkasm 已经对 pred16x16 做了循环测试：若已有，则确保你的 RVV init 能被 checkasm 选中（checkasm 会调用 ff_h264_pred_init 并比较 C vs optimized）。
- 若缺少覆盖：
  - 添加对 16x16 模式函数指针的测试，构造随机 dst/stride，随机 top/left（并包含边界值 0/255），调用 C 参考和 RVV 实现，比较输出一致。
  - 对 plane 模式增加更多随机向量与固定模式（全 0、全 255、梯度）以捕捉舍入差异。
- 运行：
  - make fate-checkasm（或 ./tests/checkasm/checkasm --list | grep h264pred 后单独运行 ./tests/checkasm/checkasm h264pred）。
  - 在 RISC-V RVV 设备/模拟器上运行（qemu-user 若支持 RVV 需匹配版本；优先真机）。
- 对性能：使用 checkasm 的 benchmark 输出（--bench）对比 C vs RVV，确认无回退且有收益。
08. 调试与 bitexact 收敛策略（针对 pred16x16 易出错点）：
- 若仅 plane 失败：对照 C 与参考 SIMD 逐步打印/断点验证 a/b/c、每行 base、移位与 rounding 常数；重点检查 (x-7)/(y-7) 的偏移与右移前加的常数。
- 若 DC 失败：检查邻居可用性分支（仅 top、仅 left、均可用、均不可用）以及 sum 的位宽与 rounding。
- 若垂直/水平失败：检查 stride 递增与存储长度是否固定 16；确认未误用 vl=VLEN 导致写多。
09. 提交前整理与维护：
- 在代码中标注：该 RVV 仅适配 8-bit H.264 intra16x16；注明需要 RVV。
- 确保与 FFmpeg 风格一致：使用 av_assert2/av_cold/av_always_inline（如需要），并通过 clang-format（若项目使用）。
- 运行额外回归：fate-h264（若可在目标上跑）或至少跑一个解码样例确认无花屏。

## Reference Files

- libavcodec/h264pred.c
- libavcodec/h264_mb.c
- libavcodec/svq3.c
- libavcodec/x86/h264_intrapred.asm
- libavcodec/x86/h264_intrapred_10bit.asm
- libavcodec/x86/h264_intrapred_init.c
- libavcodec/arm/h264pred_neon.S
- libavcodec/arm/h264pred_init_arm.c
- libavcodec/h264pred.h
- libavcodec/h264dec.h
- tests/checkasm/h264pred.c
- tests/checkasm/checkasm.c

## Interaction

```json
{
  "intent_action": "migrate",
  "intent_llm_used": false,
  "intent_error": null,
  "retrieval_llm_used": true,
  "retrieval_error": null,
  "apply_ok": true,
  "build_ok": true,
  "build_attempts": 4,
  "scp_ok": false,
  "run_on_board_ok": false,
  "board_enabled": false
}
```

## Discovery

### c_candidates

- libavcodec/svq3.c
- libavcodec/vp8.c
- libavcodec/rv34.c
- libavcodec/h264pred.c
- libavcodec/vaapi_vp8.c
- libavcodec/h264_mb.c
- libavcodec/loongarch/h264_intrapred_init_loongarch.c
- libavcodec/mips/h264pred_init_mips.c
- tests/checkasm/h264pred.c
- libavcodec/h264_parse.c
- libavcodec/vp9dsp_template.c
- libavcodec/mips/h264pred_mmi.c
- tests/checkasm/checkasm.c

### x86_refs

- libavcodec/x86/h264_intrapred.asm
- libavcodec/x86/h264_intrapred_init.c
- libavcodec/x86/h264_intrapred_10bit.asm

### arm_refs

- libavcodec/arm/h264pred_init_arm.c
- libavcodec/arm/h264pred_neon.S

### aarch64_refs

- libavcodec/aarch64/h264pred_init.c
- libavcodec/aarch64/h264pred_neon.S

### riscv_refs

- (none)

### headers

- libavcodec/h264pred.h
- libavcodec/vp8.h
- libavcodec/vp8data.h
- libavcodec/rv34.h
- libavcodec/h264dec.h
- libavcodec/mips/h264pred_mips.h

### other

- libavcodec/loongarch/h264intrapred.S

## Matches (first 200)

- libavcodec/svq3.c:661: s->hpc.pred16x16[s->intra16x16_pred_mode](dest_y, linesize);
- libavcodec/vp8.c:473: s->prob->pred16x16[i] = vp89_rac_get_uint(c, 8);
- libavcodec/vp8.c:615: memcpy(s->prob->pred16x16, vp8_pred16x16_prob_inter,
- libavcodec/vp8.c:616: sizeof(s->prob->pred16x16));
- libavcodec/vp8.c:782: memcpy(s->prob->pred16x16, vp8_pred16x16_prob_inter,
- libavcodec/vp8.c:783: sizeof(s->prob->pred16x16));
- libavcodec/vp8.c:1331: s->prob->pred16x16);
- libavcodec/vp8.c:1705: s->hpc.pred16x16[mode](dst[0], s->linesize);
- libavcodec/rv34.c:1064: r->h.pred16x16[itype](dst, s->linesize);
- libavcodec/h264pred.c:515: h->pred16x16[DC_PRED8x8     ]= FUNCC(pred16x16_dc                     , depth);\
- libavcodec/h264pred.c:516: h->pred16x16[VERT_PRED8x8   ]= FUNCC(pred16x16_vertical               , depth);\
- libavcodec/h264pred.c:517: h->pred16x16[HOR_PRED8x8    ]= FUNCC(pred16x16_horizontal             , depth);\
- libavcodec/h264pred.c:518: h->pred16x16[PLANE_PRED8x8  ]= FUNCC(pred16x16_plane                  , depth);\
- libavcodec/h264pred.c:519: h->pred16x16[LEFT_DC_PRED8x8]= FUNCC(pred16x16_left_dc                , depth);\
- libavcodec/h264pred.c:520: h->pred16x16[TOP_DC_PRED8x8 ]= FUNCC(pred16x16_top_dc                 , depth);\
- libavcodec/h264pred.c:521: h->pred16x16[DC_128_PRED8x8 ]= FUNCC(pred16x16_128_dc                 , depth);\
- libavcodec/h264pred.c:559: h->pred16x16[PLANE_PRED8x8    ] = FUNCD(pred16x16_plane_svq3);
- libavcodec/h264pred.c:568: h->pred16x16[PLANE_PRED8x8    ] = FUNCD(pred16x16_plane_rv40);
- libavcodec/h264pred.c:583: h->pred16x16[PLANE_PRED8x8 ] = FUNCD(pred16x16_tm_vp8);
- libavcodec/h264pred.c:584: h->pred16x16[DC_127_PRED8x8] = FUNCC(pred16x16_127_dc, 8);
- libavcodec/h264pred.c:585: h->pred16x16[DC_129_PRED8x8] = FUNCC(pred16x16_129_dc, 8);
- libavcodec/vaapi_vp8.c:114: pp.y_mode_probs[i] = s->prob->pred16x16[i];
- libavcodec/h264pred.h:100: void(*pred16x16[4 + 3 + 2])(uint8_t *src, ptrdiff_t stride);
- libavcodec/vp8.h:289: uint8_t pred16x16[4];
- libavcodec/h264_mb.c:705: h->hpc.pred16x16[sl->intra16x16_pred_mode](dest_y, linesize);
- libavcodec/loongarch/h264intrapred.S:195: /* void ff_h264_pred16x16_plane_h264_8_lsx(uint8_t *src, ptrdiff_t stride)
- libavcodec/loongarch/h264intrapred.S:197: function ff_h264_pred16x16_plane_h264_8_lsx
- libavcodec/loongarch/h264intrapred.S:212: /* void ff_h264_pred16x16_plane_rv40_8_lsx(uint8_t *src, ptrdiff_t stride)
- libavcodec/loongarch/h264intrapred.S:214: function ff_h264_pred16x16_plane_rv40_8_lsx
- libavcodec/loongarch/h264intrapred.S:227: /* void ff_h264_pred16x16_plane_svq3_8_lsx(uint8_t *src, ptrdiff_t stride)
- libavcodec/loongarch/h264intrapred.S:229: function ff_h264_pred16x16_plane_svq3_8_lsx
- libavcodec/loongarch/h264intrapred.S:248: /* void ff_h264_pred16x16_plane_h264_8_lasx(uint8_t *src, ptrdiff_t stride)
- libavcodec/loongarch/h264intrapred.S:250: function ff_h264_pred16x16_plane_h264_8_lasx
- libavcodec/loongarch/h264intrapred.S:265: /* void ff_h264_pred16x16_plane_rv40_8_lasx(uint8_t *src, ptrdiff_t stride)
- libavcodec/loongarch/h264intrapred.S:267: function ff_h264_pred16x16_plane_rv40_8_lasx
- libavcodec/loongarch/h264intrapred.S:280: /* void ff_h264_pred16x16_plane_svq3_8_lasx(uint8_t *src, ptrdiff_t stride)
- libavcodec/loongarch/h264intrapred.S:282: function ff_h264_pred16x16_plane_svq3_8_lasx
- libavcodec/loongarch/h264_intrapred_init_loongarch.c:41: h->pred16x16[PLANE_PRED8x8] = ff_h264_pred16x16_plane_svq3_8_lsx;
- libavcodec/loongarch/h264_intrapred_init_loongarch.c:43: h->pred16x16[PLANE_PRED8x8] = ff_h264_pred16x16_plane_rv40_8_lsx;
- libavcodec/loongarch/h264_intrapred_init_loongarch.c:45: h->pred16x16[PLANE_PRED8x8] = ff_h264_pred16x16_plane_h264_8_lsx;
- libavcodec/loongarch/h264_intrapred_init_loongarch.c:57: h->pred16x16[PLANE_PRED8x8] = ff_h264_pred16x16_plane_svq3_8_lasx;
- libavcodec/loongarch/h264_intrapred_init_loongarch.c:59: h->pred16x16[PLANE_PRED8x8] = ff_h264_pred16x16_plane_rv40_8_lasx;
- libavcodec/loongarch/h264_intrapred_init_loongarch.c:61: h->pred16x16[PLANE_PRED8x8] = ff_h264_pred16x16_plane_h264_8_lasx;
- libavcodec/x86/h264_intrapred.asm:48: ; void ff_pred16x16_vertical_8(uint8_t *src, ptrdiff_t stride)
- libavcodec/x86/h264_intrapred.asm:52: cglobal pred16x16_vertical_8, 2,3
- libavcodec/x86/h264_intrapred.asm:68: ; void ff_pred16x16_horizontal_8(uint8_t *src, ptrdiff_t stride)
- libavcodec/x86/h264_intrapred.asm:72: cglobal pred16x16_horizontal_8, 2,3
- libavcodec/x86/h264_intrapred.asm:105: ; void ff_pred16x16_dc_8(uint8_t *src, ptrdiff_t stride)
- libavcodec/x86/h264_intrapred.asm:109: cglobal pred16x16_dc_8, 2,7
- libavcodec/x86/h264_intrapred.asm:156: ; void ff_pred16x16_tm_vp8_8(uint8_t *src, ptrdiff_t stride)
- libavcodec/x86/h264_intrapred.asm:160: cglobal pred16x16_tm_vp8_8, 2,6,6
- libavcodec/x86/h264_intrapred.asm:197: cglobal pred16x16_tm_vp8_8, 2, 4, 5, dst, stride, stride3, iteration
- libavcodec/x86/h264_intrapred.asm:233: ; void ff_pred16x16_plane_*_8(uint8_t *src, ptrdiff_t stride)
- libavcodec/x86/h264_intrapred.asm:237: cglobal pred16x16_plane_%1_8, 2,9,7
- libavcodec/x86/h264_intrapred_init.c:202: h->pred16x16[VERT_PRED8x8] = ff_pred16x16_vertical_8_sse;
- libavcodec/x86/h264_intrapred_init.c:206: h->pred16x16[HOR_PRED8x8          ] = ff_pred16x16_horizontal_8_sse2;
- libavcodec/x86/h264_intrapred_init.c:207: h->pred16x16[DC_PRED8x8           ] = ff_pred16x16_dc_8_sse2;
- libavcodec/x86/h264_intrapred_init.c:216: h->pred16x16[PLANE_PRED8x8    ] = ff_pred16x16_tm_vp8_8_sse2;
- libavcodec/x86/h264_intrapred_init.c:222: h->pred16x16[PLANE_PRED8x8] = ff_pred16x16_plane_svq3_8_sse2;
- libavcodec/x86/h264_intrapred_init.c:224: h->pred16x16[PLANE_PRED8x8] = ff_pred16x16_plane_rv40_8_sse2;
- libavcodec/x86/h264_intrapred_init.c:226: h->pred16x16[PLANE_PRED8x8] = ff_pred16x16_plane_h264_8_sse2;
- libavcodec/x86/h264_intrapred_init.c:232: h->pred16x16[HOR_PRED8x8          ] = ff_pred16x16_horizontal_8_ssse3;
- libavcodec/x86/h264_intrapred_init.c:233: h->pred16x16[DC_PRED8x8           ] = ff_pred16x16_dc_8_ssse3;
- libavcodec/x86/h264_intrapred_init.c:253: h->pred16x16[PLANE_PRED8x8] = ff_pred16x16_plane_svq3_8_ssse3;
- libavcodec/x86/h264_intrapred_init.c:255: h->pred16x16[PLANE_PRED8x8] = ff_pred16x16_plane_rv40_8_ssse3;
- libavcodec/x86/h264_intrapred_init.c:257: h->pred16x16[PLANE_PRED8x8] = ff_pred16x16_plane_h264_8_ssse3;
- libavcodec/x86/h264_intrapred_init.c:264: h->pred16x16[PLANE_PRED8x8    ] = ff_pred16x16_tm_vp8_8_avx2;
- libavcodec/x86/h264_intrapred_init.c:297: h->pred16x16[DC_PRED8x8        ] = ff_pred16x16_dc_10_sse2;
- libavcodec/x86/h264_intrapred_init.c:298: h->pred16x16[TOP_DC_PRED8x8    ] = ff_pred16x16_top_dc_10_sse2;
- libavcodec/x86/h264_intrapred_init.c:299: h->pred16x16[DC_128_PRED8x8    ] = ff_pred16x16_128_dc_10_sse2;
- libavcodec/x86/h264_intrapred_init.c:300: h->pred16x16[LEFT_DC_PRED8x8   ] = ff_pred16x16_left_dc_10_sse2;
- libavcodec/x86/h264_intrapred_init.c:301: h->pred16x16[VERT_PRED8x8      ] = ff_pred16x16_vertical_10_sse2;
- libavcodec/x86/h264_intrapred_init.c:302: h->pred16x16[HOR_PRED8x8       ] = ff_pred16x16_horizontal_10_sse2;
- libavcodec/x86/h264_intrapred_10bit.asm:978: ; void ff_pred16x16_vertical_10(pixel *src, ptrdiff_t stride)
- libavcodec/x86/h264_intrapred_10bit.asm:986: cglobal pred16x16_vertical_10, 2, 3
- libavcodec/x86/h264_intrapred_10bit.asm:1000: ; void ff_pred16x16_horizontal_10(pixel *src, ptrdiff_t stride)
- libavcodec/x86/h264_intrapred_10bit.asm:1003: cglobal pred16x16_horizontal_10, 2, 3
- libavcodec/x86/h264_intrapred_10bit.asm:1018: ; void ff_pred16x16_dc_10(pixel *src, ptrdiff_t stride)
- libavcodec/x86/h264_intrapred_10bit.asm:1021: cglobal pred16x16_dc_10, 2, 6
- libavcodec/x86/h264_intrapred_10bit.asm:1054: ; void ff_pred16x16_top_dc_10(pixel *src, ptrdiff_t stride)
- libavcodec/x86/h264_intrapred_10bit.asm:1057: cglobal pred16x16_top_dc_10, 2, 3
- libavcodec/x86/h264_intrapred_10bit.asm:1076: ; void ff_pred16x16_left_dc_10(pixel *src, ptrdiff_t stride)
- libavcodec/x86/h264_intrapred_10bit.asm:1079: cglobal pred16x16_left_dc_10, 2, 6
- libavcodec/x86/h264_intrapred_10bit.asm:1107: ; void ff_pred16x16_128_dc_10(pixel *src, ptrdiff_t stride)
- libavcodec/x86/h264_intrapred_10bit.asm:1110: cglobal pred16x16_128_dc_10, 2,3
- libavcodec/aarch64/h264pred_init.c:89: h->pred16x16[DC_PRED8x8     ] = ff_pred16x16_dc_neon;
- libavcodec/aarch64/h264pred_init.c:90: h->pred16x16[VERT_PRED8x8   ] = ff_pred16x16_vert_neon;
- libavcodec/aarch64/h264pred_init.c:91: h->pred16x16[HOR_PRED8x8    ] = ff_pred16x16_hor_neon;
- libavcodec/aarch64/h264pred_init.c:92: h->pred16x16[LEFT_DC_PRED8x8] = ff_pred16x16_left_dc_neon;
- libavcodec/aarch64/h264pred_init.c:93: h->pred16x16[TOP_DC_PRED8x8 ] = ff_pred16x16_top_dc_neon;
- libavcodec/aarch64/h264pred_init.c:94: h->pred16x16[DC_128_PRED8x8 ] = ff_pred16x16_128_dc_neon;
- libavcodec/aarch64/h264pred_init.c:97: h->pred16x16[PLANE_PRED8x8  ] = ff_pred16x16_plane_neon;
- libavcodec/aarch64/h264pred_init.c:118: h->pred16x16[DC_PRED8x8     ] = ff_pred16x16_dc_neon_10;
- libavcodec/aarch64/h264pred_init.c:119: h->pred16x16[VERT_PRED8x8   ] = ff_pred16x16_vert_neon_10;
- libavcodec/aarch64/h264pred_init.c:120: h->pred16x16[HOR_PRED8x8    ] = ff_pred16x16_hor_neon_10;
- libavcodec/aarch64/h264pred_init.c:121: h->pred16x16[TOP_DC_PRED8x8 ] = ff_pred16x16_top_dc_neon_10;
- libavcodec/aarch64/h264pred_init.c:124: h->pred16x16[PLANE_PRED8x8  ] = ff_pred16x16_plane_neon_10;
- libavcodec/aarch64/h264pred_neon.S:48: function ff_pred16x16_128_dc_neon, export=1
- libavcodec/aarch64/h264pred_neon.S:50: b               .L_pred16x16_dc_end
- libavcodec/aarch64/h264pred_neon.S:53: function ff_pred16x16_top_dc_neon, export=1
- libavcodec/aarch64/h264pred_neon.S:59: b               .L_pred16x16_dc_end
- libavcodec/aarch64/h264pred_neon.S:62: function ff_pred16x16_left_dc_neon, export=1
- libavcodec/aarch64/h264pred_neon.S:68: b               .L_pred16x16_dc_end
- libavcodec/aarch64/h264pred_neon.S:71: function ff_pred16x16_dc_neon, export=1
- libavcodec/aarch64/h264pred_neon.S:81: .L_pred16x16_dc_end:
- libavcodec/aarch64/h264pred_neon.S:90: function ff_pred16x16_hor_neon, export=1
- libavcodec/aarch64/h264pred_neon.S:100: function ff_pred16x16_vert_neon, export=1
- libavcodec/aarch64/h264pred_neon.S:112: function ff_pred16x16_plane_neon, export=1
- libavcodec/aarch64/h264pred_neon.S:380: function ff_pred16x16_128_dc_neon_10, export=1
- libavcodec/aarch64/h264pred_neon.S:383: b               .L_pred16x16_dc_10_end
- libavcodec/aarch64/h264pred_neon.S:387: function ff_pred16x16_top_dc_neon_10, export=1
- libavcodec/aarch64/h264pred_neon.S:397: b               .L_pred16x16_dc_10_end
- libavcodec/aarch64/h264pred_neon.S:402: function ff_pred16x16_left_dc_neon_10, export=1
- libavcodec/aarch64/h264pred_neon.S:412: b               .L_pred16x16_dc_10_end
- libavcodec/aarch64/h264pred_neon.S:416: function ff_pred16x16_dc_neon_10, export=1
- libavcodec/aarch64/h264pred_neon.S:431: .L_pred16x16_dc_10_end:
- libavcodec/aarch64/h264pred_neon.S:441: function ff_pred16x16_hor_neon_10, export=1
- libavcodec/aarch64/h264pred_neon.S:454: function ff_pred16x16_vert_neon_10, export=1
- libavcodec/aarch64/h264pred_neon.S:469: function ff_pred16x16_plane_neon_10, export=1
- libavcodec/arm/h264pred_init_arm.c:76: h->pred16x16[DC_PRED8x8     ] = ff_pred16x16_dc_neon;
- libavcodec/arm/h264pred_init_arm.c:77: h->pred16x16[VERT_PRED8x8   ] = ff_pred16x16_vert_neon;
- libavcodec/arm/h264pred_init_arm.c:78: h->pred16x16[HOR_PRED8x8    ] = ff_pred16x16_hor_neon;
- libavcodec/arm/h264pred_init_arm.c:79: h->pred16x16[LEFT_DC_PRED8x8] = ff_pred16x16_left_dc_neon;
- libavcodec/arm/h264pred_init_arm.c:80: h->pred16x16[TOP_DC_PRED8x8 ] = ff_pred16x16_top_dc_neon;
- libavcodec/arm/h264pred_init_arm.c:81: h->pred16x16[DC_128_PRED8x8 ] = ff_pred16x16_128_dc_neon;
- libavcodec/arm/h264pred_init_arm.c:84: h->pred16x16[PLANE_PRED8x8  ] = ff_pred16x16_plane_neon;
- libavcodec/arm/h264pred_neon.S:45: function ff_pred16x16_128_dc_neon, export=1
- libavcodec/arm/h264pred_neon.S:47: b               .L_pred16x16_dc_end
- libavcodec/arm/h264pred_neon.S:50: function ff_pred16x16_top_dc_neon, export=1
- libavcodec/arm/h264pred_neon.S:56: b               .L_pred16x16_dc_end
- libavcodec/arm/h264pred_neon.S:59: function ff_pred16x16_left_dc_neon, export=1
- libavcodec/arm/h264pred_neon.S:66: b               .L_pred16x16_dc_end
- libavcodec/arm/h264pred_neon.S:69: function ff_pred16x16_dc_neon, export=1
- libavcodec/arm/h264pred_neon.S:83: .L_pred16x16_dc_end:
- libavcodec/arm/h264pred_neon.S:92: function ff_pred16x16_hor_neon, export=1
- libavcodec/arm/h264pred_neon.S:102: function ff_pred16x16_vert_neon, export=1
- libavcodec/arm/h264pred_neon.S:113: function ff_pred16x16_plane_neon, export=1
- libavcodec/mips/h264pred_init_mips.c:44: h->pred16x16[DC_PRED8x8             ] = ff_pred16x16_dc_8_mmi;
- libavcodec/mips/h264pred_init_mips.c:45: h->pred16x16[VERT_PRED8x8           ] = ff_pred16x16_vertical_8_mmi;
- libavcodec/mips/h264pred_init_mips.c:46: h->pred16x16[HOR_PRED8x8            ] = ff_pred16x16_horizontal_8_mmi;
- libavcodec/mips/h264pred_init_mips.c:53: h->pred16x16[PLANE_PRED8x8      ] = ff_pred16x16_plane_svq3_8_mmi;
- libavcodec/mips/h264pred_init_mips.c:56: h->pred16x16[PLANE_PRED8x8      ] = ff_pred16x16_plane_rv40_8_mmi;
- libavcodec/mips/h264pred_init_mips.c:62: h->pred16x16[PLANE_PRED8x8      ] = ff_pred16x16_plane_h264_8_mmi;
- libavcodec/mips/h264pred_init_mips.c:116: h->pred16x16[DC_PRED8x8] = ff_h264_intra_pred_dc_16x16_msa;
- libavcodec/mips/h264pred_init_mips.c:117: h->pred16x16[VERT_PRED8x8] = ff_h264_intra_pred_vert_16x16_msa;
- libavcodec/mips/h264pred_init_mips.c:118: h->pred16x16[HOR_PRED8x8] = ff_h264_intra_pred_horiz_16x16_msa;
- libavcodec/mips/h264pred_init_mips.c:126: h->pred16x16[7] = ff_vp8_pred16x16_127_dc_8_msa;
- libavcodec/mips/h264pred_init_mips.c:127: h->pred16x16[8] = ff_vp8_pred16x16_129_dc_8_msa;
- libavcodec/mips/h264pred_init_mips.c:130: h->pred16x16[PLANE_PRED8x8] =
- libavcodec/mips/h264pred_init_mips.c:135: h->pred16x16[LEFT_DC_PRED8x8] = ff_h264_intra_pred_dc_left_16x16_msa;
- libavcodec/mips/h264pred_init_mips.c:136: h->pred16x16[TOP_DC_PRED8x8] = ff_h264_intra_pred_dc_top_16x16_msa;
- libavcodec/mips/h264pred_init_mips.c:137: h->pred16x16[DC_128_PRED8x8] = ff_h264_intra_pred_dc_128_16x16_msa;
- tests/checkasm/h264pred.c:190: if (check_pred_func(h->pred16x16[pred_mode], "16x16", pred16x16_modes[codec][pred_mode])) {
- tests/checkasm/h264pred.c:241: { check_pred16x16, "pred16x16" },
- libavcodec/h264_parse.c:24: #include "h264pred.h"
- libavcodec/svq3.c:56: #include "h264pred.h"
- libavcodec/vp8data.h:31: #include "h264pred.h"
- libavcodec/rv34.h:36: #include "h264pred.h"
- libavcodec/h264pred.c:33: #include "h264pred.h"
- libavcodec/h264dec.h:41: #include "h264pred.h"
- libavcodec/vp8.h:35: #include "h264pred.h"
- libavcodec/vp9dsp_template.c:31: // back with h264pred.[ch]
- libavcodec/loongarch/h264_intrapred_init_loongarch.c:23: #include "libavcodec/h264pred.h"
- libavcodec/x86/h264_intrapred_init.c:28: #include "libavcodec/h264pred.h"
- libavcodec/aarch64/h264pred_init.c:26: #include "libavcodec/h264pred.h"
- libavcodec/arm/h264pred_init_arm.c:26: #include "libavcodec/h264pred.h"
- libavcodec/mips/h264pred_mmi.c:2: * Loongson SIMD optimized h264pred
- libavcodec/mips/h264pred_mips.h:25: #include "libavcodec/h264pred.h"
- tests/checkasm/checkasm.c:179: { "h264pred", checkasm_check_h264pred },
- tests/checkasm/h264pred.c:24: #include "libavcodec/h264pred.h"

## Analysis JSON

```json
{
  "symbol": "h264pred.pred16x16",
  "datatype": "uint8",
  "vectorizable": true,
  "pattern": [
    "horizontal_add",
    "stride_load",
    "tail"
  ],
  "has_stride": true,
  "has_saturation": false,
  "reduction": true,
  "tail_required": false,
  "math_expression": "mode-specific intra 16x16 prediction writing a 16x16 block: for y in 0..15, x in 0..15: dst[y*stride+x] = f_mode(top[0..15], left[0..15], topleft, (optional) top_right); where f_mode includes cases: VERT: dst[y][x]=top[x]; HOR: dst[y][x]=left[y]; DC: dc=(sum(top)+sum(left)+16)>>5, dst[y][x]=dc; TOP_DC: dc=(sum(top)+8)>>4; LEFT_DC: dc=(sum(left)+8)>>4; 128_DC: dst[y][x]=128; PLANE: dst[y][x]=clip((a + b*(x-7) + c*(y-7) + 16)>>5) (bitdepth-dependent)",
  "c_candidates": [
    "libavcodec/h264pred.c:36-54",
    "libavcodec/h264pred.c:515-521"
  ],
  "x86_refs": [
    "libavcodec/x86/h264_intrapred.asm:48-60",
    "libavcodec/x86/h264_intrapred_init.c:90-114",
    "libavcodec/x86/h264_intrapred_init.c:201-259",
    "libavcodec/x86/h264_intrapred_init.c:297-303"
  ],
  "arm_refs": [
    "libavcodec/arm/h264pred_neon.S:45-57",
    "libavcodec/arm/h264pred_neon.S:50-57",
    "libavcodec/arm/h264pred_neon.S:59-60",
    "libavcodec/arm/h264pred_init_arm.c:28-35"
  ],
  "notes": "pred16x16 is a function-pointer table (multiple modes) generated via h264pred_template.c for bit depths 8/9/10/12/14; core data is pixel (uint8 for 8-bit, uint16 for >8-bit) but this task targets h264pred.pred16x16 in the shown 8-bit context. Many modes are pure fills/replications (VERT/HOR/128_DC) and are trivially RVV-vectorizable with strided stores; DC/TOP_DC/LEFT_DC require horizontal sum reduction of 16 samples (use widening add then reduction). Tail handling not required for width=16, but RVV implementation should handle generic VL and unaligned addresses."
}
```

- llm_used: True

## Generation (raw)

```
{
  "files": [
    {
      "path": "libavcodec/riscv/h264pred_rvv.S",
      "content": ".text\n.align 2\n\n# RVV intrapred for H.264 16x16 (8-bit)\n# void pred16x16_*(uint8_t *src, ptrdiff_t stride);\n# src points to top-left of destination block.\n# Top samples are at src - stride, left samples at src - 1.\n\n.globl ff_pred16x16_vertical_8_rvv\n.type  ff_pred16x16_vertical_8_rvv, @function\nff_pred16x16_vertical_8_rvv:\n    addi    a2, a0, -1\n    sub     a2, a2, a1\n\n    li      t0, 16\n    vsetvli t0, t0, e8, m1, ta, ma\n    vle8.v  v0, 1(a2)              # top[0..15]\n\n    li      t1, 16\n1:\n    vse8.v  v0, (a0)\n    add     a0, a0, a1\n    addi    t1, t1, -1\n    bnez    t1, 1b\n    ret\n.size ff_pred16x16_vertical_8_rvv, .-ff_pred16x16_vertical_8_rvv\n\n.globl ff_pred16x16_horizontal_8_rvv\n.type  ff_pred16x16_horizontal_8_rvv, @function\nff_pred16x16_horizontal_8_rvv:\n    li      t0, 16\n    vsetvli t0, t0, e8, m1, ta, ma\n\n    li      t1, 16\n1:\n    lbu     t2, -1(a0)\n    vmv.v.x v0, t2\n    vse8.v  v0, (a0)\n    add     a0, a0, a1\n    addi    t1, t1, -1\n    bnez    t1, 1b\n    ret\n.size ff_pred16x16_horizontal_8_rvv, .-ff_pred16x16_horizontal_8_rvv\n\n.globl ff_pred16x16_128_dc_8_rvv\n.type  ff_pred16x16_128_dc_8_rvv, @function\nff_pred16x16_128_dc_8_rvv:\n    li      t0, 16\n    vsetvli t0, t0, e8, m1, ta, ma\n    li      t2, 128\n    vmv.v.x v0, t2\n\n    li      t1, 16\n1:\n    vse8.v  v0, (a0)\n    add     a0, a0, a1\n    addi    t1, t1, -1\n    bnez    t1, 1b\n    ret\n.size ff_pred16x16_128_dc_8_rvv, .-ff_pred16x16_128_dc_8_rvv\n\n.globl ff_pred16x16_top_dc_8_rvv\n.type  ff_pred16x16_top_dc_8_rvv, @function\nff_pred16x16_top_dc_8_rvv:\n    # a2 = src - stride - 1 (points to top[-1])\n    addi    a2, a0, -1\n    sub     a2, a2, a1\n\n    li      t0, 16\n    vsetvli t0, t0, e8, m1, ta, ma\n    vle8.v  v0, 1(a2)              # top[0..15]\n\n    # Widen to u16 and reduce sum (sum <= 4080)\n    vwaddu.vx v1, v0, zero\n    vsetvli t0, t0, e16, m2, ta, ma\n    vredsum.vs v2, v1, vzero\n    vmv.x.s t3, v2\n\n    addi    t3, t3, 8\n    srli    t3, t3, 4\n\n    li      t0, 16\n    vsetvli t0, t0, e8, m1, ta, ma\n    vmv.v.x v0, t3\n\n    li      t1, 16\n1:\n    vse8.v  v0, (a0)\n    add     a0, a0, a1\n    addi    t1, t1, -1\n    bnez    t1, 1b\n    ret\n.size ff_pred16x16_top_dc_8_rvv, .-ff_pred16x16_top_dc_8_rvv\n\n.globl ff_pred16x16_left_dc_8_rvv\n.type  ff_pred16x16_left_dc_8_rvv, @function\nff_pred16x16_left_dc_8_rvv:\n    mv      t6, a0\n    li      t3, 0\n\n    li      t1, 16\n1:\n    lbu     t2, -1(t6)\n    add     t3, t3, t2\n    add     t6, t6, a1\n    addi    t1, t1, -1\n    bnez    t1, 1b\n\n    addi    t3, t3, 8\n    srli    t3, t3, 4\n\n    li      t0, 16\n    vsetvli t0, t0, e8, m1, ta, ma\n    vmv.v.x v0, t3\n\n    li      t1, 16\n2:\n    vse8.v  v0, (a0)\n    add     a0, a0, a1\n    addi    t1, t1, -1\n    bnez    t1, 2b\n    ret\n.size ff_pred16x16_left_dc_8_rvv, .-ff_pred16x16_left_dc_8_rvv\n\n.globl ff_pred16x16_dc_8_rvv\n.type  ff_pred16x16_dc_8_rvv, @function\nff_pred16x16_dc_8_rvv:\n    # sum(top[0..15])\n    addi    a2, a0, -1\n    sub     a2, a2, a1\n\n    li      t0, 16\n    vsetvli t0, t0, e8, m1, ta, ma\n    vle8.v  v0, 1(a2)              # top[0..15]\n    vwaddu.vx v1, v0, zero\n\n    vsetvli t0, t0, e16, m2, ta, ma\n    vredsum.vs v2, v1, vzero\n    vmv.x.s t3, v2                  # t3 = sum(top)\n\n    # sum(left[0..15])\n    mv      t6, a0\n    li      t4, 0\n    li      t1, 16\n1:\n    lbu     t2, -1(t6)\n    add     t4, t4, t2\n    add     t6, t6, a1\n    addi    t1, t1, -1\n    bnez    t1, 1b\n\n    add     t3, t3, t4\n    addi    t3, t3, 16\n    srli    t3, t3, 5\n\n    li      t0, 16\n    vsetvli t0, t0, e8, m1, ta, ma\n    vmv.v.x v0, t3\n\n    li      t1, 16\n2:\n    vse8.v  v0, (a0)\n    add     a0, a0, a1\n    addi    t1, t1, -1\n    bnez    t1, 2b\n    ret\n.size ff_pred16x16_dc_8_rvv, .-ff_pred16x16_dc_8_rvv\n"
    }
  ],
  "patches": [
    {
      "path": "libavcodec/riscv/h264pred_rvv.S",
      "diff": "*** Begin Patch\n*** Delete File: libavcodec/riscv/h264pred_rvv.S\n*** End Patch"
    },
    {
      "path": "libavcodec/riscv/h264pred_rvv.S",
      "diff": "*** Begin Patch\n*** Add File: libavcodec/riscv/h264pred_rvv.S\n+.text\n+.align 2\n+\n+# RVV intrapred for H.264 16x16 (8-bit)\n+# void pred16x16_*(uint8_t *src, ptrdiff_t stride);\n+# src points to top-left of destination block.\n+# Top samples are at src - stride, left samples at src - 1.\n+\n+.globl ff_pred16x16_vertical_8_rvv\n+.type  ff_pred16x16_vertical_8_rvv, @function\n+ff_pred16x16_vertical_8_rvv:\n+    addi    a2, a0, -1\n+    sub     a2, a2, a1\n+\n+    li      t0, 16\n+    vsetvli t0, t0, e8, m1, ta, ma\n+    vle8.v  v0, 1(a2)              # top[0..15]\n+\n+    li      t1, 16\n+1:\n+    vse8.v  v0, (a0)\n+    add     a0, a0, a1\n+    addi    t1, t1, -1\n+    bnez    t1, 1b\n+    ret\n+.size ff_pred16x16_vertical_8_rvv, .-ff_pred16x16_vertical_8_rvv\n+\n+.globl ff_pred16x16_horizontal_8_rvv\n+.type  ff_pred16x16_horizontal_8_rvv, @function\n+ff_pred16x16_horizontal_8_rvv:\n+    li      t0, 16\n+    vsetvli t0, t0, e8, m1, ta, ma\n+\n+    li      t1, 16\n+1:\n+    lbu     t2, -1(a0)\n+    vmv.v.x v0, t2\n+    vse8.v  v0, (a0)\n+    add     a0, a0, a1\n+    addi    t1, t1, -1\n+    bnez    t1, 1b\n+    ret\n+.size ff_pred16x16_horizontal_8_rvv, .-ff_pred16x16_horizontal_8_rvv\n+\n+.globl ff_pred16x16_128_dc_8_rvv\n+.type  ff_pred16x16_128_dc_8_rvv, @function\n+ff_pred16x16_128_dc_8_rvv:\n+    li      t0, 16\n+    vsetvli t0, t0, e8, m1, ta, ma\n+    li      t2, 128\n+    vmv.v.x v0, t2\n+\n+    li      t1, 16\n+1:\n+    vse8.v  v0, (a0)\n+    add     a0, a0, a1\n+    addi    t1, t1, -1\n+    bnez    t1, 1b\n+    ret\n+.size ff_pred16x16_128_dc_8_rvv, .-ff_pred16x16_128_dc_8_rvv\n+\n+.globl ff_pred16x16_top_dc_8_rvv\n+.type  ff_pred16x16_top_dc_8_rvv, @function\n+ff_pred16x16_top_dc_8_rvv:\n+    # a2 = src - stride - 1 (points to top[-1])\n+    addi    a2, a0, -1\n+    sub     a2, a2, a1\n+\n+    li      t0, 16\n+    vsetvli t0, t0, e8, m1, ta, ma\n+    vle8.v  v0, 1(a2)              # top[0..15]\n+\n+    # Widen to u16 and reduce sum (sum <= 4080)\n+    vwaddu.vx v1, v0, zero\n+    vsetvli t0, t0, e16, m2, ta, ma\n+    vredsum.vs v2, v1, vzero\n+    vmv.x.s t3, v2\n+\n+    addi    t3, t3, 8\n+    srli    t3, t3, 4\n+\n+    li      t0, 16\n+    vsetvli t0, t0, e8, m1, ta, ma\n+    vmv.v.x v0, t3\n+\n+    li      t1, 16\n+1:\n+    vse8.v  v0, (a0)\n+    add     a0, a0, a1\n+    addi    t1, t1, -1\n+    bnez    t1, 1b\n+    ret\n+.size ff_pred16x16_top_dc_8_rvv, .-ff_pred16x16_top_dc_8_rvv\n+\n+.globl ff_pred16x16_left_dc_8_rvv\n+.type  ff_pred16x16_left_dc_8_rvv, @function\n+ff_pred16x16_left_dc_8_rvv:\n+    mv      t6, a0\n+    li      t3, 0\n+\n+    li      t1, 16\n+1:\n+    lbu     t2, -1(t6)\n+    add     t3, t3, t2\n+    add     t6, t6, a1\n+    addi    t1, t1, -1\n+    bnez    t1, 1b\n+\n+    addi    t3, t3, 8\n+    srli    t3, t3, 4\n+\n+    li      t0, 16\n+    vsetvli t0, t0, e8, m1, ta, ma\n+    vmv.v.x v0, t3\n+\n+    li      t1, 16\n+2:\n+    vse8.v  v0, (a0)\n+    add     a0, a0, a1\n+    addi    t1, t1, -1\n+    bnez    t1, 2b\n+    ret\n+.size ff_pred16x16_left_dc_8_rvv, .-ff_pred16x16_left_dc_8_rvv\n+\n+.globl ff_pred16x16_dc_8_rvv\n+.type  ff_pred16x16_dc_8_rvv, @function\n+ff_pred16x16_dc_8_rvv:\n+    # sum(top[0..15])\n+    addi    a2, a0, -1\n+    sub     a2, a2, a1\n+\n+    li      t0, 16\n+    vsetvli t0, t0, e8, m1, ta, ma\n+    vle8.v  v0, 1(a2)              # top[0..15]\n+    vwaddu.vx v1, v0, zero\n+\n+    vsetvli t0, t0, e16, m2, ta, ma\n+    vredsum.vs v2, v1, vzero\n+    vmv.x.s t3, v2                  # t3 = sum(top)\n+\n+    # sum(left[0..15])\n+    mv      t6, a0\n+    li      t4, 0\n+    li      t1, 16\n+1:\n+    lbu     t2, -1(t6)\n+    add     t4, t4, t2\n+    add     t6, t6, a1\n+    addi    t1, t1, -1\n+    bnez    t1, 1b\n+\n+    add     t3, t3, t4\n+    addi    t3, t3, 16\n+    srli    t3, t3, 5\n+\n+    li      t0, 16\n+    vsetvli t0, t0, e8, m1, ta, ma\n+    vmv.v.x v0, t3\n+\n+    li      t1, 16\n+2:\n+    vse8.v  v0, (a0)\n+    add     a0, a0, a1\n+    addi    t1, t1, -1\n+    bnez    t1, 2b\n+    ret\n+.size ff_pred16x16_dc_8_rvv, .-ff_pred16x16_dc_8_rvv\n+\n*** End Patch"
    }
  ]
}
```

## Materialized

- /home/yuhe/project/cmdTool/workplace/FFmpeg/libavcodec/riscv/h264pred_rvv.S
- /home/yuhe/project/cmdTool/workplace/FFmpeg/libavcodec/riscv/h264pred_init.c
- /home/yuhe/project/cmdTool/workplace/FFmpeg/libavcodec/riscv/Makefile

## configure

```
$ /home/yuhe/project/cmdTool/workplace/FFmpeg/configure --cross-prefix=riscv64-unknown-linux-gnu- --arch=riscv64 --target-os=linux --enable-cross-compile --cpu=rv64gcv '--extra-cflags=-march=rv64gcv -mabi=lp64d -O3' --extra-ldflags=-static --disable-shared --enable-static
(rc=0)
```

### stdout

```
install prefix            /usr/local
source path               src
C compiler                riscv64-unknown-linux-gnu-gcc
C library                 glibc
host C compiler           gcc
host C library            glibc
ARCH                      riscv (rv64gcv)
big-endian                no
runtime cpu detection     yes
RISC-V CBO Prefetch       yes
RISC-V Vector enabled     yes
debug symbols             yes
strip symbols             yes
optimize for size         no
optimizations             yes
static                    yes
shared                    no
network support           yes
threading support         pthreads
safe bitstream reader     yes
texi2html enabled         no
perl enabled              yes
pod2man enabled           yes
makeinfo enabled          no
makeinfo supports HTML    no
experimental features     yes
xmllint enabled           yes

External libraries:
iconv

External libraries providing hardware acceleration:
v4l2_m2m

Libraries:
avcodec                 avformat                swscale
avdevice                avutil
avfilter                swresample

Programs:
ffmpeg                  ffprobe

Enabled decoders:
aac                     ffwavesynth             pfm
aac_fixed               fic                     pgm
aac_latm                fits                    pgmyuv
aasc                    flac                    pgssub
ac3                     flic                    pgx
ac3_fixed               flv                     phm
acelp_kelvin            fmvc                    photocd
adpcm_4xm               fourxm                  pictor
adpcm_adx               fraps                   pixlet
adpcm_afc               frwu                    pjs
adpcm_agm               ftr                     ppm
adpcm_aica              g723_1                  prores
adpcm_argo              g728                    prores_raw
adpcm_ct                g729                    prosumer
adpcm_dtk               gdv                     psd
adpcm_ea                gem                     ptx
adpcm_ea_maxis_xa       gif                     qcelp
adpcm_ea_r1             gremlin_dpcm            qdm2
adpcm_ea_r2             gsm                     qdmc
adpcm_ea_r3             gsm_ms                  qdraw
adpcm_ea_xas            h261                    qoa
adpcm_g722              h263                    qoi
adpcm_g726              h263_v4l2m2m            qpeg
adpcm_g726le            h263i                   qtrle
adpcm_ima_acorn         h263p                   r10k
adpcm_ima_alp           h264                    r210
adpcm_ima_amv           h264_v4l2m2m            ra_144
adpcm_ima_apc           hap                     ra_288
adpcm_ima_apm           hca                     ralf
adpcm_ima_cunning       hcom                    rawvideo
adpcm_ima_dat4          hdr                     realtext
adpcm_ima_dk3           hevc                    rka
adpcm_ima_dk4           hevc_v4l2m2m            rl2
adpcm_ima_ea_eacs       hnm4_video              roq
adpcm_ima_ea_sead       hq_hqa                  roq_dpcm
adpcm_ima_iss           hqx                     rpza
adpcm_ima_moflex        huffyuv                 rtv1
adpcm_ima_mtf           hymt                    rv10
adpcm_ima_oki           iac                     rv20
adpcm_ima_qt            idcin                   rv30
adpcm_ima_rad           idf                     rv40
adpcm_ima_smjpeg        iff_ilbm                rv60
adpcm_ima_ssi           ilbc                    s302m
adpcm_ima_wav           imc                     sami
adpcm_ima_ws            imm4                    sanm
adpcm_ima_xbox          imm5                    sbc
adpcm_ms                indeo2                  scpr
adpcm_mtaf              indeo3                  sdx2_dpcm
adpcm_psx               indeo4                  sga
adpcm_sanyo             indeo5                  sgi
adpcm_sbpro_2           interplay_acm           sgirle
adpcm_sbpro_3           interplay_dpcm          sheervideo
adpcm_sbpro_4           interplay_video         shorten
adpcm_swf               ipu                     simbiosis_imx
adpcm_thp               jacosub                 sipr
adpcm_thp_le            jpeg2000                siren
adpcm_vima              jpegls                  smackaud
adpcm_xa                jv                      smacker
adpcm_xmd               kgv1                    smc
adpcm_yamaha            kmvc                    smvjpeg
adpcm_zork              lagarith                snow
agm                     lead                    sol_dpcm
aic                     loco                    sonic
alac                    m101                    sp5x
alias_pix               mace3                   speedhq
als                     mace6                   speex
amrnb                   magicyuv                srt
amrwb                   mdec                    ssa
amv                     media100                stl
anm                     metasound               subrip
ansi                    microdvd                subviewer
anull                   mimic                   subviewer1
apac                    misc4                   sunrast
ape                     mjpeg                   svq1
aptx                    mjpegb                  svq3
aptx_hd                 mlp                     tak
apv                     mmvideo                 targa
arbc                    mobiclip                targa_y216
argo                    motionpixels            text
ass                     movtext                 theora
asv1                    mp1                     thp
asv2                    mp1float                tiertexseqvideo
atrac1                  mp2                     tiff
atrac3                  mp2float                tmv
atrac3al                mp3                     truehd
atrac3p                 mp3adu                  truemotion1
atrac3pal               mp3adufloat             truemotion2
atrac9                  mp3float                truemotion2rt
aura                    mp3on4                  truespeech
aura2                   mp3on4float             tscc2
av1                     mpc7                    tta
avrn                    mpc8                    twinvq
avrp                    mpeg1_v4l2m2m           txd
avs                     mpeg1video              ulti
avui                    mpeg2_v4l2m2m           utvideo
bethsoftvid             mpeg2video              v210
bfi                     mpeg4                   v210x
bink                    mpeg4_v4l2m2m           v308
binkaudio_dct           mpegvideo               v408
binkaudio_rdft          mpl2                    v410
bintext                 msa1                    vb
bitpacked               msmpeg4v1               vble
bmp                     msmpeg4v2               vbn
bmv_audio               msmpeg4v3               vc1
bmv_video               msnsiren                vc1_v4l2m2m
bonk                    msp2                    vc1image
brender_pix             msrle                   vcr1
c93                     mss1                    vmdaudio
cavs                    mss2                    vmdvideo
cbd2_dpcm               msvideo1                vmix
ccaption                mszh                    vmnc
cdgraphics              mts2                    vnull
cdtoons                 mv30                    vorbis
cdxl                    mvc1                    vp3
cfhd                    mvc2                    vp4
cinepak                 mvdv                    vp5
clearvideo              mxpeg                   vp6
cljr                    nellymoser              vp6a
cllc                    notchlc                 vp6f
comfortnoise            nuv                     vp7
cook                    on2avc                  vp8
cpia                    opus                    vp8_v4l2m2m
cri                     osq                     vp9
cscd                    paf_audio               vp9_v4l2m2m
cyuv                    paf_video               vplayer
dca                     pam                     vqa
dds                     pbm                     vqc
derf_dpcm               pcm_alaw                vvc
dfa                     pcm_bluray              wady_dpcm
dfpwm                   pcm_dvd                 wavarc
dirac                   pcm_f16le               wavpack
dnxhd                   pcm_f24le               wbmp
dolby_e                 pcm_f32be               webp
dpx                     pcm_f32le               webvtt
dsd_lsbf                pcm_f64be               wmalossless
dsd_lsbf_planar         pcm_f64le               wmapro
dsd_msbf                pcm_lxf                 wmav1
dsd_msbf_planar         pcm_mulaw               wmav2
dsicinaudio             pcm_s16be               wmavoice
dsicinvideo             pcm_s16be_planar        wmv1
dss_sp                  pcm_s16le               wmv2
dst                     pcm_s16le_planar        wmv3
dvaudio                 pcm_s24be               wmv3image
dvbsub                  pcm_s24daud             wnv1
dvdsub                  pcm_s24le               wrapped_avframe
dvvideo                 pcm_s24le_planar        ws_snd1
dxtory                  pcm_s32be               xan_dpcm
dxv                     pcm_s32le               xan_wc3
eac3                    pcm_s32le_planar        xan_wc4
eacmv                   pcm_s64be               xbin
eamad                   pcm_s64le               xbm
eatgq                   pcm_s8                  xface
eatgv                   pcm_s8_planar           xl
eatqi                   pcm_sga                 xma1
eightbps                pcm_u16be               xma2
eightsvx_exp            pcm_u16le               xpm
eightsvx_fib            pcm_u24be               xsub
escape124               pcm_u24le               xwd
escape130               pcm_u32be               y41p
evrc                    pcm_u32le               ylc
fastaudio               pcm_u8                  yop
ffv1                    pcm_vidc                yuv4
ffvhuff                 pcx                     zero12v

Enabled encoders:
a64multi                hdr                     pgmyuv
a64multi5               hevc_v4l2m2m            phm
aac                     huffyuv                 ppm
ac3                     jpeg2000                prores
ac3_fixed               jpegls                  prores_aw
adpcm_adx               ljpeg                   prores_ks
adpcm_argo              magicyuv                qoi
adpcm_g722              mjpeg                   qtrle
adpcm_g726              mlp                     r10k
adpcm_g726le            movtext                 r210
adpcm_ima_alp           mp2                     ra_144
adpcm_ima_amv           mp2fixed                rawvideo
adpcm_ima_apm           mpeg1video              roq
adpcm_ima_qt            mpeg2video              roq_dpcm
adpcm_ima_ssi           mpeg4                   rpza
adpcm_ima_wav           mpeg4_v4l2m2m           rv10
adpcm_ima_ws            msmpeg4v2               rv20
adpcm_ms                msmpeg4v3               s302m
adpcm_swf               msrle                   sbc
adpcm_yamaha            msvideo1                sgi
alac                    nellymoser              smc
alias_pix               opus                    snow
amv                     pam                     speedhq
anull                   pbm                     srt
aptx                    pcm_alaw                ssa
aptx_hd                 pcm_bluray              subrip
ass                     pcm_dvd                 sunrast
asv1                    pcm_f32be               svq1
asv2                    pcm_f32le               targa
avrp                    pcm_f64be               text
avui                    pcm_f64le               tiff
bitpacked               pcm_mulaw               truehd
bmp                     pcm_s16be               tta
cfhd                    pcm_s16be_planar        ttml
cinepak                 pcm_s16le               utvideo
cljr                    pcm_s16le_planar        v210
comfortnoise            pcm_s24be               v308
dca                     pcm_s24daud             v408
dfpwm                   pcm_s24le               v410
dnxhd                   pcm_s24le_planar        vbn
dpx                     pcm_s32be               vc2
dvbsub                  pcm_s32le               vnull
dvdsub                  pcm_s32le_planar        vorbis
dvvideo                 pcm_s64be               vp8_v4l2m2m
dxv                     pcm_s64le               wavpack
eac3                    pcm_s8                  wbmp
ffv1                    pcm_s8_planar           webvtt
ffvhuff                 pcm_u16be               wmav1
fits                    pcm_u16le               wmav2
flac                    pcm_u24be               wmv1
flv                     pcm_u24le               wmv2
g723_1                  pcm_u32be               wrapped_avframe
gif                     pcm_u32le               xbm
h261                    pcm_u8                  xface
h263                    pcm_vidc                xsub
h263_v4l2m2m            pcx                     xwd
h263p                   pfm                     y41p
h264_v4l2m2m            pgm                     yuv4

Enabled hwaccels:

Enabled parsers:
aac                     dvdsub                  mpegvideo
aac_latm                evc                     opus
ac3                     ffv1                    png
adx                     flac                    pnm
amr                     ftr                     prores
apv                     g723_1                  prores_raw
av1                     g729                    qoi
avs2                    gif                     rv34
avs3                    gsm                     sbc
bmp                     h261                    sipr
cavsvideo               h263                    tak
cook                    h264                    vc1
cri                     hdr                     vorbis
dca                     hevc                    vp3
dirac                   ipu                     vp8
dnxhd                   jpeg2000                vp9
dnxuc                   jpegxl                  vvc
dolby_e                 misc4                   webp
dpx                     mjpeg                   xbm
dvaudio                 mlp                     xma
dvbsub                  mpeg4video              xwd
dvd_nav                 mpegaudio

Enabled demuxers:
aa                      ico                     pcm_mulaw
aac                     idcin                   pcm_s16be
aax                     idf                     pcm_s16le
ac3                     iff                     pcm_s24be
ac4                     ifv                     pcm_s24le
ace                     ilbc                    pcm_s32be
acm                     image2                  pcm_s32le
act                     image2_alias_pix        pcm_s8
adf                     image2_brender_pix      pcm_u16be
adp                     image2pipe              pcm_u16le
ads                     image_bmp_pipe          pcm_u24be
adx                     image_cri_pipe          pcm_u24le
aea                     image_dds_pipe          pcm_u32be
afc                     image_dpx_pipe          pcm_u32le
aiff                    image_exr_pipe          pcm_u8
aix                     image_gem_pipe          pcm_vidc
alp                     image_gif_pipe          pdv
amr                     image_hdr_pipe          pjs
amrnb                   image_j2k_pipe          pmp
amrwb                   image_jpeg_pipe         pp_bnk
anm                     image_jpegls_pipe       pva
apac                    image_jpegxl_pipe       pvf
apc                     image_pam_pipe          qcp
ape                     image_pbm_pipe          qoa
apm                     image_pcx_pipe          r3d
apng                    image_pfm_pipe          rawvideo
aptx                    image_pgm_pipe          rcwt
aptx_hd                 image_pgmyuv_pipe       realtext
apv                     image_pgx_pipe          redspark
aqtitle                 image_phm_pipe          rka
argo_asf                image_photocd_pipe      rl2
argo_brp                image_pictor_pipe       rm
argo_cvg                image_png_pipe          roq
asf                     image_ppm_pipe          rpl
asf_o                   image_psd_pipe          rsd
ass                     image_qdraw_pipe        rso
ast                     image_qoi_pipe          rtp
au                      image_sgi_pipe          rtsp
av1                     image_sunrast_pipe      s337m
avi                     image_svg_pipe          sami
avr                     image_tiff_pipe         sap
avs                     image_vbn_pipe          sbc
avs2                    image_webp_pipe         sbg
avs3                    image_xbm_pipe          scc
bethsoftvid             image_xpm_pipe          scd
bfi                     image_xwd_pipe          sdns
bfstm                   ingenient               sdp
bink                    ipmovie                 sdr2
binka                   ipu                     sds
bintext                 ircam                   sdx
bit                     iss                     segafilm
bitpacked               iv8                     ser
bmv                     ivf                     sga
boa                     ivr                     shorten
bonk                    jacosub                 siff
brstm                   jpegxl_anim             simbiosis_imx
c93                     jv                      sln
caf                     kux                     smacker
cavsvideo               kvag                    smjpeg
cdg                     laf                     smush
cdxl                    lc3                     sol
cine                    live_flv                sox
codec2                  lmlm4                   spdif
codec2raw               loas                    srt
concat                  lrc                     stl
data                    luodat                  str
daud                    lvf                     subviewer
dcstr                   lxf                     subviewer1
derf                    m4v                     sup
dfa                     matroska                svag
dfpwm                   mca                     svs
dhav                    mcc                     swf
dirac                   mgsts                   tak
dnxhd                   microdvd                tedcaptions
dsf                     mjpeg                   thp
dsicin                  mjpeg_2000              threedostr
dss                     mlp                     tiertexseq
dts                     mlv                     tmv
dtshd                   mm                      truehd
dv                      mmf                     tta
dvbsub                  mods                    tty
dvbtxt                  moflex                  txd
dxa                     mov                     ty
ea                      mp3                     usm
ea_cdata                mpc                     v210
eac3                    mpc8                    v210x
epaf                    mpegps                  vag
evc                     mpegts                  vc1
ffmetadata              mpegtsraw               vc1t
filmstrip               mpegvideo               vividas
fits                    mpjpeg                  vivo
flac                    mpl2                    vmd
flic                    mpsub                   vobsub
flv                     msf                     voc
fourxm                  msnwc_tcp               vpk
frm                     msp                     vplayer
fsb                     mtaf                    vqf
fwse                    mtv                     vvc
g722                    musx                    w64
g723_1                  mv                      wady
g726                    mvi                     wav
g726le                  mxf                     wavarc
g728                    mxg                     wc3
g729                    nc                      webm_dash_manifest
gdv                     n
```

## make checkasm

```
$ make -j128 tests/checkasm/checkasm
(rc=2)
```

### stdout

```
AR	libavcodec/libavcodec.a
riscv64-unknown-linux-gnu-ar: libavcodec/h264pred_rvv.o: No such file or directory
make: *** [src/ffbuild/library.mak:39: libavcodec/libavcodec.a] Error 1
```
