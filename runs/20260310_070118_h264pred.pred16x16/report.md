# rvv-agent run report

## Symbol

- h264pred.pred16x16

## Plan

01. 在 FFmpeg 源码中定位 h264pred.pred16x16 的绑定位置：搜索 "pred16x16" 与 "H264PredContext"（例如：grep -R "pred16x16" -n libavcodec）。确认该符号属于 H.264 intra 16x16 预测函数表中的某个函数指针（通常类似 hpc->pred16x16[mode] 或 hpc->pred16x16[0..3]）。记录：具体字段名、数组下标/模式含义、调用方路径（一般在 libavcodec/h264pred.c 或 h264dec/h264_intrapred 相关文件里）。
02. 定位并阅读 C 参考实现：在 libavcodec/h264pred.c（或同目录相关文件）中找到对应 pred16x16 的 C 实现函数（常见命名形如 ff_pred16x16_*_c）。确认其输入输出语义：dst 指针、stride、top/left 可用性（是否通过边界填充/额外参数隐含）、写入范围（16x16）、像素类型（8-bit/10-bit 需确认是否仅限 8-bit 路径）。把 C 实现中每行/每列的生成公式整理成可向量化的操作清单（复制、加权平均、clip、round 等）。
03. 定位 x86 参考实现（用于对照行为与性能手法）：在 libavcodec/x86/ 目录下搜索相同功能（例如 grep -R "pred16x16" libavcodec/x86）。通常在 h264_intrapred.asm / h264pred.asm 等文件中，找到 ff_pred16x16_*_sse2/ssse3/avx 等实现与初始化绑定（如 ff_h264_pred_init_x86）。记录：对应模式有哪些优化版本、各版本对齐/加载策略、是否使用写回 16 字节一行的存储循环，以及边界处理是否依赖调用方保证可读 top/left。
04. 定位 ARM/ARM64(NEON) 参考实现（如果存在）：在 libavcodec/arm/ 或 libavcodec/aarch64/ 中搜索 pred16x16（例如 grep -R "pred16x16" libavcodec/aarch64 libavcodec/arm）。找到 ff_pred16x16_*_neon 及 init 绑定（如 ff_h264_pred_init_arm / ff_h264_pred_init_aarch64）。记录 NEON 实现采用的核心向量化思路（按行复制、按列复制、使用 vdup/vext/vtrn 等）。这一步用于给 RVV 的寄存器/访存组织提供参考。
05. 明确需要迁移的具体子算子范围：h264pred.pred16x16 往往不是单一函数，而是 16x16 的多个预测模式（如 vertical/horizontal/DC/plane）。根据前面定位到的函数表，列出要在 RVV 中实现的具体符号集合（例如 ff_pred16x16_vertical_rvv、ff_pred16x16_horizontal_rvv、ff_pred16x16_dc_rvv、ff_pred16x16_plane_rvv 或同等命名）。优先顺序建议：vertical/horizontal（最简单）→ DC → plane（最复杂，含梯度与加权）。
06. 设计 RVV 数据布局与向量长度策略：针对 8-bit 像素，使用 RVV 8-bit 向量进行加载/存储（vuint8m1_t 等），每行 16 字节可用一个向量装下（VL=16）。实现时用 vsetvl_e8m1(16) 固定 VL=16（在 RVV 上通常可行且清晰），并确保每行 store 连续 16 字节。对需要扩展计算的模式（DC/plane），使用 widening 到 16-bit/32-bit（vwadd/vwmul 等）避免溢出，并在最终阶段窄化回 8-bit（vnclip/wrapping 需与 C 行为一致）。
07. 生成 RVV 实现文件：在 libavcodec/riscv/ 下新增/编辑 h264pred_rvv.c（或遵循项目现有命名：例如 libavcodec/riscv/h264pred_init_rvv.c + intrapred_rvv.c）。为每个 pred16x16 模式写对应 RVV 函数，函数签名严格匹配 C 版本（通常形如 void fn(uint8_t *dst, ptrdiff_t stride)）。实现要点示例：
- Vertical：一次加载 top 行 16 字节（从 dst - stride 读取或由调用方提供的 top 指针，按 C 实现一致），然后循环 16 次将该向量存到 dst + y*stride。
- Horizontal：每行取左边像素（dst[-1] 或等价位置），用 vdup 广播成 16 字节向量后写一行，重复 16 行。
- DC：按 C 逻辑累加 top[0..15] 与 left[0..15]（可用 RVV reduce：vredsum），计算平均与舍入方式（通常 (sum + 16) >> 5 或类似，需以 C 为准），再广播写满 16x16。
- Plane：按 C 逻辑计算 H/V 梯度（涉及 (i+1)*(top[8+i]-top[6-i]) 等），使用 16/32-bit 向量化计算，注意与 C 完全同样的 round/clip（通常 clip_uint8）。先生成每列基值，再按行递推写入（或按行向量生成 16 个像素）。
所有模式确保只写 16x16，不越界读；若 C 实现假设边界已扩展，则 RVV 也保持同样假设，不额外加分支。
08. 确保与 FFmpeg 像素位深路径匹配：检查是否存在 10-bit/12-bit h264pred（可能在 h264pred_template.c / *_depth.c 体系）。如果 pred16x16 在模板中按 bitdepth 生成，RVV 需仅接入 8-bit（H.264 8-bit）或按同样模板方式为高位深提供 u16 实现（使用 vuint16*）。以实际 C 定位结果为准，避免把 8-bit RVV 绑定到高位深上下文。
09. 在 RISC-V init 中完成函数指针绑定：新增/修改 libavcodec/riscv/h264pred_init_rvv.c（或相应 init 文件），实现类似 ff_h264_pred_init_riscv(H264PredContext *hpc, int codec_id/bit_depth...) 的钩子（名称以现有 riscv 组织为准）。在检测到 RVV（HAVE_RVV 或 runtime 检测）时，将 hpc->pred16x16[mode] 指向 RVV 版本（例如 hpc->pred16x16[VERT_PRED]=ff_pred16x16_vertical_rvv）。保留 C 版本作为 fallback。
10. 集成到构建系统（configure 与 Makefile）：
- 在 configure/相关检测中确认 RVV 特性宏：使用现有 FFmpeg RISC-V RVV 检测（通常为 --enable-rvv 或自动探测，宏如 HAVE_RVV）。如果项目已有 riscv/rvv 目录规则，沿用。
- 在 libavcodec/riscv/Makefile（或 libavcodec/Makefile 的 riscv 条目）中把新增的 *.c 加入 OBJS-$(CONFIG_H264_DECODER) 或对应组件（更准确：加入与 h264pred 相关的对象列表，条件为 ARCH_RISCV + HAVE_RVV）。
- 若需要，添加到 libavcodec/riscv/Makefile 的 RVV 专用对象变量（如 OBJS-$(HAVE_RVV) += h264pred_rvv.o）。确保链接时不会在无 RVV 的 riscv 上引用 RVV 指令（通过条件编译隔离）。
11. 补充/更新头文件与属性：在 RVV 实现文件中包含 <riscv_vector.h>，并使用 av_always_inline/av_cold 等与 FFmpeg 风格一致的属性。必要时用 #if HAVE_RVV 包裹实现。确保函数名带 ff_ 前缀并在需要处声明为 static/extern，与现有模式一致，避免符号冲突。
12. 本地功能一致性验证（非 checkasm）：在实现阶段先用 FATE/解码对比做快速 sanity：编译出 riscv 目标后运行若干 H.264 样本（若有 qemu-riscv64 + RVV 支持环境）对比输出 hash（ffmpeg -i sample -f framemd5）。该步骤帮助在进入 checkasm 前排除明显越界/舍入错误。
13. 运行 checkasm 验证（核心要求）：
- 确认 checkasm 覆盖 h264pred：在 tools/checkasm/ 中搜索 h264pred 或 h264_intrapred（例如 grep -R "h264pred" tools/checkasm -n）。如果已有测试用例，会对比各 arch 优化与 C 参考。
- 在 RISC-V RVV 环境构建 checkasm：./configure --arch=riscv64 --enable-rvv --enable-checkasm（以及必要的 --enable-cross-compile/--cross-prefix/--sysroot）。然后 make -j tools/checkasm。
- 运行：tools/checkasm --test=h264pred（或实际测试名，如 --test=h264_intrapred）。确保输出中 RVV 版本被枚举并与 C 逐项对比通过。若测试框架允许，开启随机种子与更多迭代（--bench 或 --runs 选项以实际 checkasm 支持为准）。
- 若 checkasm 未覆盖 pred16x16：在 tools/checkasm/h264pred.c（或对应文件）新增针对 pred16x16 各模式的测试：随机填充邻域（top/left），调用 C 与 RVV 版本，memcmp 16x16 输出一致，并对齐/非对齐 dst/stride 组合做覆盖。然后重新运行 checkasm 确保通过。
14. 性能基准：使用 checkasm 的 benchmark 输出（若支持 --bench）比较 C vs RVV 的 cycles/throughput；至少对 vertical/horizontal/DC/plane 各自记录一次。若 plane 模式收益不明显，优先保证正确性并考虑后续迭代（例如减少标量循环、改用向量递推）。
15. 提交前清理与风格对齐：确保代码符合 FFmpeg 风格（缩进、命名、注释）；确保在无 RVV 构建、或 riscv32/rvv 不可用时不会编译该文件；确保 init 绑定受宏控制且不会影响非 RISC-V 平台。最后跑一遍：make fate（若环境允许）与 tools/checkasm 全量。

## Reference Files

- libavcodec/h264pred.c
- libavcodec/h264_mb.c
- libavcodec/x86/h264_intrapred.asm
- libavcodec/x86/h264_intrapred_10bit.asm
- libavcodec/x86/h264_intrapred_init.c
- libavcodec/arm/h264pred_neon.S
- libavcodec/arm/h264pred_init_arm.c
- libavcodec/h264pred.h
- tests/checkasm/h264pred.c
- tests/checkasm/checkasm.c

## Interaction

```json
{
  "intent_action": "migrate",
  "intent_llm_used": true,
  "intent_error": null,
  "retrieval_llm_used": true,
  "retrieval_error": null,
  "apply_ok": true,
  "build_ok": true,
  "build_attempts": 2,
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
  "datatype": "mixed",
  "vectorizable": true,
  "pattern": [
    "stride_load",
    "horizontal_add",
    "saturate",
    "tail"
  ],
  "has_stride": true,
  "has_saturation": true,
  "reduction": true,
  "tail_required": false,
  "math_expression": "switch(mode):\n  VERTICAL: for y in 0..15: for x in 0..15: dst[y][x] = top[x]\n  HORIZONTAL: for y in 0..15: for x in 0..15: dst[y][x] = left[y]\n  DC: dc = (sum_{i=0..15} top[i] + sum_{i=0..15} left[i] + 16) >> 5 ; for y,x: dst[y][x]=dc\n  TOP_DC: dc = (sum_{i=0..15} top[i] + 8) >> 4 ; for y,x: dst[y][x]=dc\n  LEFT_DC: dc = (sum_{i=0..15} left[i] + 8) >> 4 ; for y,x: dst[y][x]=dc\n  128_DC (8-bit): for y,x: dst[y][x]=128\n  PLANE (H.264/RV40/SVQ3 variants):\n    H = sum_{i=1..8} i*(top[7+i] - top[7-i])\n    V = sum_{i=1..8} i*(left[7+i] - left[7-i])\n    a = 16*(top[15] + left[15])\n    b = (5*H + 32) >> 6\n    c = (5*V + 32) >> 6\n    for y in 0..15:\n      for x in 0..15:\n        val = (a + b*(x-7) + c*(y-7) + 16) >> 5\n        dst[y][x] = clip(val, 0, (1<<bit_depth)-1)",
  "c_candidates": [
    "libavcodec/h264pred_template.c:pred16x16_vertical_*",
    "libavcodec/h264pred_template.c:pred16x16_horizontal_*",
    "libavcodec/h264pred_template.c:pred16x16_dc_*",
    "libavcodec/h264pred_template.c:pred16x16_top_dc_*",
    "libavcodec/h264pred_template.c:pred16x16_left_dc_*",
    "libavcodec/h264pred_template.c:pred16x16_128_dc_*",
    "libavcodec/h264pred_template.c:pred16x16_plane_* (h264/rv40/svq3)"
  ],
  "x86_refs": [
    "libavcodec/x86/h264_intrapred.asm:ff_pred16x16_vertical_8_sse",
    "libavcodec/x86/h264_intrapred.asm:ff_pred16x16_horizontal_8_sse2",
    "libavcodec/x86/h264_intrapred.asm:ff_pred16x16_dc_8_sse2",
    "libavcodec/x86/h264_intrapred.asm:ff_pred16x16_dc_8_ssse3",
    "libavcodec/x86/h264_intrapred.asm:ff_pred16x16_plane_h264_8_sse2",
    "libavcodec/x86/h264_intrapred.asm:ff_pred16x16_plane_h264_8_ssse3",
    "libavcodec/x86/h264_intrapred.asm:ff_pred16x16_plane_rv40_8_sse2",
    "libavcodec/x86/h264_intrapred.asm:ff_pred16x16_plane_rv40_8_ssse3",
    "libavcodec/x86/h264_intrapred.asm:ff_pred16x16_plane_svq3_8_sse2",
    "libavcodec/x86/h264_intrapred.asm:ff_pred16x16_plane_svq3_8_ssse3",
    "libavcodec/x86/h264_intrapred_10bit.asm:ff_pred16x16_dc_10_sse2",
    "libavcodec/x86/h264_intrapred_10bit.asm:ff_pred16x16_top_dc_10_sse2",
    "libavcodec/x86/h264_intrapred_10bit.asm:ff_pred16x16_left_dc_10_sse2",
    "libavcodec/x86/h264_intrapred_10bit.asm:ff_pred16x16_128_dc_10_sse2",
    "libavcodec/x86/h264_intrapred_10bit.asm:ff_pred16x16_vertical_10_sse2",
    "libavcodec/x86/h264_intrapred_10bit.asm:ff_pred16x16_horizontal_10_sse2"
  ],
  "arm_refs": [
    "libavcodec/arm/h264pred_neon.S:ff_pred16x16_128_dc_neon",
    "libavcodec/arm/h264pred_neon.S:ff_pred16x16_top_dc_neon",
    "libavcodec/arm/h264pred_neon.S:ff_pred16x16_left_dc_neon",
    "libavcodec/arm/h264pred_neon.S:ff_pred16x16_dc_neon",
    "libavcodec/arm/h264pred_neon.S:ff_pred16x16_vert_neon",
    "libavcodec/arm/h264pred_neon.S:ff_pred16x16_hor_neon",
    "libavcodec/arm/h264pred_neon.S:ff_pred16x16_plane_neon"
  ],
  "notes": "pred16x16 是函数指针数组(多模式/多bitdepth)，RVV实现通常按具体模式分别写: vertical/horizontal/dc/top_dc/left_dc/128_dc/plane。数据为像素类型: bit_depth<=8 用 uint8；高位深(9/10/12/14)在模板中通常用 uint16(pixel)并在plane计算中使用更宽的中间类型(int32)并最终clip到[0, (1<<bit_depth)-1]。DC/TopDC/LeftDC涉及对16个边界像素求和(reduction)并广播写16x16；vertical为从src-stride加载16字节/16像素后按stride存16行；horizontal为从左列加载16个值并按stride存行(每行16重复)。plane为仿射平面并需要饱和裁剪；适合RVV向量化x维度(16像素)并标量推进y，或2行展开。16是固定长度，因此不需要尾处理，但RVV实现可用vsetvl动态处理(可能出现tail路径)，可直接vsetvl=16(或min(16,VLEN/8))。"
}
```

- llm_used: True

## Generation (raw)

```
{
  "generated": [
    {
      "target_path": "libavcodec/riscv/h264pred_rvv.S",
      "action": "create",
      "content": ".text\n.align 2\n\n/*\n * RVV 8-bit pred16x16 helpers for H.264 intra prediction\n * ABI: void func(uint8_t *src, ptrdiff_t stride)\n */\n\n.globl ff_pred16x16_vertical_8_rvv\n.type  ff_pred16x16_vertical_8_rvv, @function\nff_pred16x16_vertical_8_rvv:\n    addi    a2, a0, 0\n    sub     a3, a0, a1                /* top = src - stride */\n    li      t0, 16\n    vsetivli zero, 16, e8, m1, ta, ma\n    vle8.v  v0, (a3)\n1:\n    vse8.v  v0, (a2)\n    add     a2, a2, a1\n    addi    t0, t0, -1\n    bnez    t0, 1b\n    ret\n.size ff_pred16x16_vertical_8_rvv, .-ff_pred16x16_vertical_8_rvv\n\n.globl ff_pred16x16_horizontal_8_rvv\n.type  ff_pred16x16_horizontal_8_rvv, @function\nff_pred16x16_horizontal_8_rvv:\n    addi    a2, a0, 0\n    li      t0, 16\n    vsetivli zero, 16, e8, m1, ta, ma\n1:\n    lbu     t1, -1(a2)                /* left[y] */\n    vmv.v.x v0, t1\n    vse8.v  v0, (a2)\n    add     a2, a2, a1\n    addi    t0, t0, -1\n    bnez    t0, 1b\n    ret\n.size ff_pred16x16_horizontal_8_rvv, .-ff_pred16x16_horizontal_8_rvv\n\n.globl ff_pred16x16_128_dc_8_rvv\n.type  ff_pred16x16_128_dc_8_rvv, @function\nff_pred16x16_128_dc_8_rvv:\n    addi    a2, a0, 0\n    li      t0, 16\n    li      t1, 128\n    vsetivli zero, 16, e8, m1, ta, ma\n    vmv.v.x v0, t1\n1:\n    vse8.v  v0, (a2)\n    add     a2, a2, a1\n    addi    t0, t0, -1\n    bnez    t0, 1b\n    ret\n.size ff_pred16x16_128_dc_8_rvv, .-ff_pred16x16_128_dc_8_rvv\n\n.globl ff_pred16x16_top_dc_8_rvv\n.type  ff_pred16x16_top_dc_8_rvv, @function\nff_pred16x16_top_dc_8_rvv:\n    sub     a2, a0, a1                /* top */\n    vsetivli zero, 16, e8, m1, ta, ma\n    vle8.v  v0, (a2)\n    vredsum.vs v1, v0, v0             /* v1[0] = sum + top[0] (will fix below) */\n    vmv.x.s t0, v1\n    lbu     t1, 0(a2)\n    sub     t0, t0, t1                /* sum */\n    addi    t0, t0, 8\n    srli    t0, t0, 4                 /* dc */\n    vmv.v.x v2, t0\n    addi    a3, a0, 0\n    li      t2, 16\n1:\n    vse8.v  v2, (a3)\n    add     a3, a3, a1\n    addi    t2, t2, -1\n    bnez    t2, 1b\n    ret\n.size ff_pred16x16_top_dc_8_rvv, .-ff_pred16x16_top_dc_8_rvv\n\n.globl ff_pred16x16_left_dc_8_rvv\n.type  ff_pred16x16_left_dc_8_rvv, @function\nff_pred16x16_left_dc_8_rvv:\n    addi    a2, a0, -1                /* left column ptr at row0 */\n    li      t0, 0\n    li      t1, 16\n1:\n    lbu     t2, 0(a2)\n    add     t0, t0, t2\n    add     a2, a2, a1\n    addi    t1, t1, -1\n    bnez    t1, 1b\n    addi    t0, t0, 8\n    srli    t0, t0, 4                 /* dc */\n    vsetivli zero, 16, e8, m1, ta, ma\n    vmv.v.x v0, t0\n    addi    a3, a0, 0\n    li      t3, 16\n2:\n    vse8.v  v0, (a3)\n    add     a3, a3, a1\n    addi    t3, t3, -1\n    bnez    t3, 2b\n    ret\n.size ff_pred16x16_left_dc_8_rvv, .-ff_pred16x16_left_dc_8_rvv\n\n.globl ff_pred16x16_dc_8_rvv\n.type  ff_pred16x16_dc_8_rvv, @function\nff_pred16x16_dc_8_rvv:\n    sub     a2, a0, a1                /* top */\n    vsetivli zero, 16, e8, m1, ta, ma\n    vle8.v  v0, (a2)\n    vredsum.vs v1, v0, v0\n    vmv.x.s t0, v1\n    lbu     t1, 0(a2)\n    sub     t0, t0, t1                /* sum_top */\n\n    addi    a3, a0, -1                /* left col */\n    li      t2, 16\n1:\n    lbu     t3, 0(a3)\n    add     t0, t0, t3\n    add     a3, a3, a1\n    addi    t2, t2, -1\n    bnez    t2, 1b\n\n    addi    t0, t0, 16\n    srli    t0, t0, 5                 /* dc */\n    vmv.v.x v2, t0\n    addi    a4, a0, 0\n    li      t4, 16\n2:\n    vse8.v  v2, (a4)\n    add     a4, a4, a1\n    addi    t4, t4, -1\n    bnez    t4, 2b\n    ret\n.size ff_pred16x16_dc_8_rvv, .-ff_pred16x16_dc_8_rvv\n",
      "anchor_hint": "",
      "description": "新增 RVV 8-bit pred16x16 的 vertical/horizontal/dc/top_dc/left_dc/128_dc 汇编实现"
    },
    {
      "target_path": "libavcodec/riscv/h264pred_init.c",
      "action": "append",
      "content": "h->pred16x16[VERT_PRED8x8] = ff_pred16x16_vertical_8_rvv;\nh->pred16x16[HOR_PRED8x8 ] = ff_pred16x16_horizontal_8_rvv;\nh->pred16x16[DC_PRED8x8  ] = ff_pred16x16_dc_8_rvv;",
      "anchor_hint": "在 ff_h264_pred_init_riscv() 函数内 if (bit_depth == 8) 分支的 #if HAVE_RVV 块末尾（或 pred16x16 相关赋值处之后）",
      "description": "在 RISC-V init 中注册 RVV pred16x16(8-bit) 的 vertical/horizontal/dc"
    },
    {
      "target_path": "libavcodec/riscv/Makefile",
      "action": "append",
      "content": "OBJS-$(CONFIG_H264PRED)               += riscv/h264pred_rvv.o\n",
      "anchor_hint": "追加到与 CONFIG_H264PRED 相关的 OBJS-$(CONFIG_...) 列表末尾（或新增该行到文件合适位置）",
      "description": "为新建的 h264pred_rvv.S 增加构建目标"
    }
  ]
}
```

## Materialized

- runs/20260310_070118_h264pred.pred16x16/artifacts/files/libavcodec/riscv/h264pred_rvv.S
- /home/yuhe/project/cmdTool/workplace/FFmpeg/libavcodec/riscv/h264pred_rvv.S
- runs/20260310_070118_h264pred.pred16x16/artifacts/files/libavcodec/riscv/h264pred_init.c
- /home/yuhe/project/cmdTool/workplace/FFmpeg/libavcodec/riscv/h264pred_init.c
- runs/20260310_070118_h264pred.pred16x16/artifacts/files/libavcodec/riscv/Makefile
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
AR	libavfilter/libavfilter.a
AS	libavcodec/riscv/h264pred_rvv.o
CC	libavcodec/riscv/vvc/dsp_init.o
AS	libavcodec/riscv/vvc/mc_rvv.o
AS	libavcodec/riscv/vvc/sad_rvv.o
CC	libavcodec/rka.o
AR	libavformat/libavformat.a
CC	libavcodec/rl.o
CC	libavcodec/rl2.o
CC	libavcodec/roqaudioenc.o
CC	libavcodec/rle.o
CC	libavcodec/roqvideo.o
CC	libavcodec/roqvideodec.o
CC	libavcodec/roqvideoenc.o
CC	libavcodec/rpza.o
CC	libavcodec/rpzaenc.o
CC	libavcodec/rtv1.o
CC	libavcodec/rtjpeg.o
CC	libavcodec/rv10enc.o
CC	libavcodec/rv20enc.o
CC	libavcodec/rv10.o
CC	libavcodec/rv30dsp.o
CC	libavcodec/rv34.o
CC	libavcodec/rv30.o
CC	libavcodec/rv34_parser.o
CC	libavcodec/rv34dsp.o
CC	libavcodec/rv40.o
CC	libavcodec/rv40dsp.o
CC	libavcodec/rv60dec.o
CC	libavcodec/rv60dsp.o
CC	libavcodec/s302m.o
CC	libavcodec/s302menc.o
CC	libavcodec/samidec.o
CC	libavcodec/sanm.o
CC	libavcodec/sbc_parser.o
CC	libavcodec/sbcdec.o
CC	libavcodec/sbc.o
CC	libavcodec/sbcdsp.o
CC	libavcodec/sbrdsp.o
CC	libavcodec/sbcenc.o
CC	libavcodec/sbrdsp_fixed.o
CC	libavcodec/scpr.o
CC	libavcodec/sga.o
CC	libavcodec/sgidec.o
CC	libavcodec/sgienc.o
CC	libavcodec/sgirledec.o
CC	libavcodec/sheervideo.o
CC	libavcodec/shorten.o
CC	libavcodec/simple_idct.o
CC	libavcodec/sinewin.o
CC	libavcodec/sipr.o
CC	libavcodec/sipr_parser.o
CC	libavcodec/siren.o
CC	libavcodec/sipr16k.o
CC	libavcodec/smacker.o
CC	libavcodec/smc.o
CC	libavcodec/smcenc.o
CC	libavcodec/smpte_436m.o
CC	libavcodec/snappy.o
CC	libavcodec/snow.o
CC	libavcodec/snow_dwt.o
CC	libavcodec/snowdec.o
CC	libavcodec/snowenc.o
CC	libavcodec/sonic.o
CC	libavcodec/sp5xdec.o
CC	libavcodec/speedhq.o
CC	libavcodec/speedhqdec.o
CC	libavcodec/speedhqenc.o
CC	libavcodec/speexdec.o
CC	libavcodec/srtdec.o
CC	libavcodec/srtenc.o
CC	libavcodec/startcode.o
CC	libavcodec/subviewerdec.o
CC	libavcodec/sunrast.o
CC	libavcodec/sunrastenc.o
CC	libavcodec/svq1.o
CC	libavcodec/svq1dec.o
CC	libavcodec/svq1enc.o
CC	libavcodec/svq3.o
CC	libavcodec/synth_filter.o
CC	libavcodec/tak.o
CC	libavcodec/tak_parser.o
CC	libavcodec/takdec.o
CC	libavcodec/takdsp.o
CC	libavcodec/targa.o
CC	libavcodec/targa_y216dec.o
CC	libavcodec/targaenc.o
CC	libavcodec/textdec.o
CC	libavcodec/texturedsp.o
CC	libavcodec/texturedspenc.o
CC	libavcodec/threadprogress.o
CC	libavcodec/tiertexseqv.o
CC	libavcodec/tiff.o
CC	libavcodec/tiff_common.o
CC	libavcodec/tiffenc.o
CC	libavcodec/tmv.o
CC	libavcodec/to_upper4.o
CC	libavcodec/tpeldsp.o
CC	libavcodec/truemotion1.o
CC	libavcodec/truemotion2.o
CC	libavcodec/truemotion2rt.o
CC	libavcodec/truespeech.o
CC	libavcodec/tscc2.o
CC	libavcodec/tta.o
CC	libavcodec/ttadata.o
CC	libavcodec/ttadsp.o
CC	libavcodec/ttaenc.o
CC	libavcodec/ttaencdsp.o
CC	libavcodec/ttmlenc.o
CC	libavcodec/twinvq.o
CC	libavcodec/twinvqdec.o
CC	libavcodec/txd.o
CC	libavcodec/ulti.o
CC	libavcodec/utils.o
CC	libavcodec/utvideodec.o
CC	libavcodec/utvideodsp.o
CC	libavcodec/utvideoenc.o
CC	libavcodec/v210dec.o
CC	libavcodec/v210enc.o
CC	libavcodec/v210x.o
CC	libavcodec/v308dec.o
CC	libavcodec/v308enc.o
CC	libavcodec/v408dec.o
CC	libavcodec/v408enc.o
CC	libavcodec/v410dec.o
CC	libavcodec/v410enc.o
CC	libavcodec/v4l2_buffers.o
CC	libavcodec/v4l2_context.o
CC	libavcodec/v4l2_fmt.o
CC	libavcodec/v4l2_m2m.o
CC	libavcodec/v4l2_m2m_dec.o
CC	libavcodec/v4l2_m2m_enc.o
CC	libavcodec/vb.o
CC	libavcodec/vble.o
CC	libavcodec/vbndec.o
CC	libavcodec/vbnenc.o
CC	libavcodec/vc1.o
CC	libavcodec/vc1_block.o
CC	libavcodec/vc1_loopfilter.o
CC	libavcodec/vc1_mc.o
CC	libavcodec/vc1_parser.o
CC	libavcodec/vc1_pred.o
CC	libavcodec/vc1data.o
CC	libavcodec/vc1dec.o
CC	libavcodec/vc1dsp.o
CC	libavcodec/vc2enc.o
CC	libavcodec/vc2enc_dwt.o
CC	libavcodec/vcr1.o
CC	libavcodec/version.o
CC	libavcodec/videodsp.o
CC	libavcodec/vlc.o
CC	libavcodec/vima.o
CC	libavcodec/vmdaudio.o
CC	libavcodec/vmdvideo.o
CC	libavcodec/vmixdec.o
CC	libavcodec/vmnc.o
CC	libavcodec/vorbis.o
CC	libavcodec/vorbis_data.o
CC	libavcodec/vorbis_parser.o
CC	libavcodec/vorbisdec.o
CC	libavcodec/vorbisdsp.o
CC	libavcodec/vp3.o
CC	libavcodec/vorbisenc.o
CC	libavcodec/vp3_parser.o
CC	libavcodec/vp3dsp.o
CC	libavcodec/vp5.o
CC	libavcodec/vp56.o
CC	libavcodec/vp56data.o
CC	libavcodec/vp56dsp.o
CC	libavcodec/vp6.o
CC	libavcodec/vp6dsp.o
CC	libavcodec/vp8.o
CC	libavcodec/vp8_parser.o
CC	libavcodec/vp8data.o
CC	libavcodec/vp8dsp.o
CC	libavcodec/vp9.o
CC	libavcodec/vp9_parser.o
CC	libavcodec/vp9block.o
CC	libavcodec/vp9data.o
CC	libavcodec/vp9dsp_10bpp.o
CC	libavcodec/vp9dsp.o
CC	libavcodec/vp9dsp_12bpp.o
CC	libavcodec/vp9dsp_8bpp.o
CC	libavcodec/vp9mvs.o
CC	libavcodec/vp9lpf.o
CC	libavcodec/vp9prob.o
CC	libavcodec/vp9recon.o
CC	libavcodec/vpx_rac.o
CC	libavcodec/vqavideo.o
CC	libavcodec/vqcdec.o
CC	libavcodec/vvc/cabac.o
CC	libavcodec/vvc/ctu.o
CC	libavcodec/vvc/data.o
CC	libavcodec/vvc/dec.o
CC	libavcodec/vvc/dsp.o
CC	libavcodec/vvc/filter.o
CC	libavcodec/vvc/inter.o
CC	libavcodec/vvc/intra.o
CC	libavcodec/vvc/intra_utils.o
CC	libavcodec/vvc/mvs.o
CC	libavcodec/vvc/itx_1d.o
CC	libavcodec/vvc/ps.o
CC	libavcodec/vvc/refs.o
CC	libavcodec/vvc/sei.o
CC	libavcodec/vvc/thread.o
CC	libavcodec/vvc_parser.o
CC	libavcodec/wavarc.o
CC	libavcodec/wavpack.o
CC	libavcodec/wavpackdata.o
CC	libavcodec/wavpackenc.o
CC	libavcodec/wbmpdec.o
CC	libavcodec/wbmpenc.o
CC	libavcodec/webp.o
CC	libavcodec/webp_parser.o
CC	libavcodec/webvttdec.o
CC	libavcodec/webvttenc.o
CC	libavcodec/wma.o
CC	libavcodec/wma_freqs.o
CC	libavcodec/wma_common.o
CC	libavcodec/wmadec.o
CC	libavcodec/wmaenc.o
CC	libavcodec/wmalosslessdec.o
CC	libavcodec/wmavoice.o
CC	libavcodec/wmaprodec.o
CC	libavcodec/wmv2.o
CC	libavcodec/wmv2data.o
CC	libavcodec/wmv2dec.o
CC	libavcodec/wmv2enc.o
CC	libavcodec/wmv2dsp.o
CC	libavcodec/wnv1.o
CC	libavcodec/wrapped_avframe.o
CC	libavcodec/ws-snd1.o
CC	libavcodec/xan.o
CC	libavcodec/xbm_parser.o
CC	libavcodec/xbmenc.o
CC	libavcodec/xbmdec.o
CC	libavcodec/xface.o
CC	libavcodec/xfacedec.o
CC	libavcodec/xfaceenc.o
CC	libavcodec/xiph.o
CC	libavcodec/xl.o
CC	libavcodec/xma_parser.o
CC	libavcodec/xpmdec.o
CC	libavcodec/xsubdec.o
CC	libavcodec/xsubenc.o
CC	libavcodec/xvididct.o
CC	libavcodec/xwddec.o
CC	libavcodec/xwd_parser.o
CC	libavcodec/xwdenc.o
CC	libavcodec/xxan.o
CC	libavcodec/y41pdec.o
CC	libavcodec/y41penc.o
CC	libavcodec/ylc.o
CC	libavcodec/yop.o
CC	libavcodec/yuv4dec.o
CC	libavcodec/yuv4enc.o
AR	libswresample/libswresample.a
CC	libswscale/alphablend.o
CC	libswscale/cms.o
CC	libswscale/csputils.o
CC	libswscale/format.o
CC	libswscale/gamma.o
CC	libswscale/graph.o
CC	libswscale/hscale.o
CC	libswscale/hscale_fast_bilinear.o
CC	libswscale/input.o
CC	libswscale/lut3d.o
CC	libswscale/ops.o
CC	libswscale/ops_backend.o
CC	libswscale/ops_chain.o
CC	libswscale/ops_memcpy.o
CC	libswscale/ops_optimizer.o
In function 'add_level',
    inlined from 'add_level' at src/libavcodec/vlc.c:457:17,
    inlined from 'add_level' at src/libavcodec/vlc.c:457:17,
    inlined from 'add_level' at src/libavcodec/vlc.c:457:17,
    inlined from 'vlc_multi_gen' at src/libavcodec/vlc.c:508:5,
    inlined from 'ff_vlc_init_multi_from_lengths' at src/libavcodec/vlc.c:564:11:
src/libavcodec/vlc.c:443:36: warning: array subscript 3 is above array bounds of 'uint16_t[3]' {aka 'short unsigned int[3]'} [-Warray-bounds=]
  443 |             if (is16bit) info.val16[curlevel] = sym;
      |                          ~~~~~~~~~~^~~~~~~~~~
In file included from src/libavcodec/vlc.c:39:
src/libavcodec/vlc.h: In function 'ff_vlc_init_multi_from_lengths':
src/libavcodec/vlc.h:59:18: note: while referencing 'val16'
   59 |         uint16_t val16[VLC_MULTI_MAX_SYMBOLS / 2];
      |                  ^~~~~
In function 'add_level',
    inlined from 'add_level' at src/libavcodec/vlc.c:457:17,
    inlined from 'add_level' at src/libavcodec/vlc.c:457:17,
    inlined from 'add_level' at src/libavcodec/vlc.c:457:17,
    inlined from 'add_level' at src/libavcodec/vlc.c:457:17,
    inlined from 'vlc_multi_gen' at src/libavcodec/vlc.c:508:5,
    inlined from 'ff_vlc_init_multi_from_lengths' at src/libavcodec/vlc.c:564:11:
src/libavcodec/vlc.c:443:36: warning: array subscript 4 is above array bounds of 'uint16_t[3]' {aka 'short unsigned int[3]'} [-Warray-bounds=]
  443 |             if (is16bit) info.val16[curlevel] = sym;
      |                          ~~~~~~~~~~^~~~~~~~~~
src/libavcodec/vlc.h: In function 'ff_vlc_init_multi_from_lengths':
src/libavcodec/vlc.h:59:18: note: while referencing 'val16'
   59 |         uint16_t val16[VLC_MULTI_MAX_SYMBOLS / 2];
      |                  ^~~~~
In function 'add_level',
    inlined from 'add_level' at src/libavcodec/vlc.c:457:17,
    inlined from 'add_level' at src/libavcodec/vlc.c:457:17,
    inlined from 'add_level' at src/libavcodec/vlc.c:457:17,
    inlined from 'add_level' at src/libavcodec/vlc.c:457:17,
    inlined from 'add_level' at src/libavcodec/vlc.c:457:17,
    inlined from 'vlc_multi_gen' at src/libavcodec/vlc.c:508:5,
    inlined from 'ff_vlc_init_multi_from_lengths' at src/libavcodec/vlc.c:564:11:
src/libavcodec/vlc.c:443:36: warning: array subscript 5 is above array bounds of 'uint16_t[3]' {aka 'short unsigned int[3]'} [-Warray-bounds=]
  443 |             if (is16bit) info.val16[curlevel] = sym;
      |                          ~~~~~~~~~~^~~~~~~~~~
src/libavcodec/vlc.h: In function 'ff_vlc_init_multi_from_lengths':
src/libavcodec/vlc.h:59:18: note: while referencing 'val16'
   59 |         uint16_t val16[VLC_MULTI_MAX_SYMBOLS / 2];
      |                  ^~~~~
CC	libswscale/options.o
CC	libswscale/output.o
CC	libswscale/rgb2rgb.o
AS	libswscale/riscv/input_rvv.o
AS	libswscale/riscv/range_rvv.o
CC	libswscale/riscv/rgb2rgb.o
AS	libswscale/riscv/rgb2rgb_rvb.o
AS	libswscale/riscv/rgb2rgb_rvv.o
CC	libswscale/slice.o
CC	libswscale/riscv/swscale.o
CC	libswscale/swscale.o
CC	libswscale/swscale_unscaled.o
CC	libswscale/utils.o
CC	libswscale/version.o
CC	libswscale/vscale.o
CC	libswscale/yuv2rgb.o
src/libavcodec/wmadec.c: In function 'wma_decode_block':
src/libavcodec/wmadec.c:521:30: warning: writing 1 byte into a region of size 0 [-Wstringop-overflow=]
  521 |         s->channel_coded[ch] = a;
      |         ~~~~~~~~~~~~~~~~~~~~~^~~
In file included from src/libavcodec/wmadec.c:45:
src/libavcodec/wma.h:112:13: note: at offset 2 into destination object 'channel_coded' of size 2
  112 |     uint8_t channel_coded[MAX_CHANNELS];    ///< true if channel is coded
      |             ^~~~~~~~~~~~~
CC	libavutil/adler32.o
CC	libavutil/aes.o
CC	libavutil/aes_ctr.o
CC	libavutil/ambient_viewing_environment.o
CC	libavutil/audio_fifo.o
CC	libavutil/avsscanf.o
CC	libavutil/avstring.o
CC	libavutil/base64.o
CC	libavutil/blowfish.o
CC	libavutil/buffer.o
CC	libavutil/camellia.o
CC	libavutil/bprint.o
CC	libavutil/channel_layout.o
CC	libavutil/container_fifo.o
CC	libavutil/cast5.o
CC	libavutil/cpu.o
CC	libavutil/crc.o
CC	libavutil/csp.o
CC	libavutil/des.o
CC	libavutil/detection_bbox.o
CC	libavutil/dict.o
CC	libavutil/display.o
CC	libavutil/dovi_meta.o
CC	libavutil/downmix_info.o
CC	libavutil/encryption_info.o
CC	libavutil/error.o
CC	libavutil/eval.o
CC	libavutil/executor.o
CC	libavutil/fifo.o
CC	libavutil/file.o
CC	libavutil/file_open.o
CC	libavutil/film_grain_params.o
CC	libavutil/fixed_dsp.o
CC	libavutil/float_dsp.o
CC	libavutil/float_scalarproduct.o
CC	libavutil/frame.o
CC	libavutil/hash.o
CC	libavutil/hdr_dynamic_vivid_metadata.o
CC	libavutil/hdr_dynamic_metadata.o
CC	libavutil/hmac.o
CC	libavutil/hwcontext.o
CC	libavutil/hwcontext_stub.o
CC	libavutil/iamf.o
CC	libavutil/imgutils.o
CC	libavutil/integer.o
CC	libavutil/intmath.o
CC	libavutil/lfg.o
CC	libavutil/lls.o
CC	libavutil/log.o
CC	libavutil/log2_tab.o
CC	libavutil/lzo.o
CC	libavutil/mastering_display_metadata.o
CC	libavutil/md5.o
CC	libavutil/mathematics.o
CC	libavutil/mem.o
CC	libavutil/opt.o
CC	libavutil/murmur3.o
CC	libavutil/parseutils.o
CC	libavutil/pixdesc.o
CC	libavutil/pixelutils.o
CC	libavutil/random_seed.o
CC	libavutil/rational.o
CC	libavutil/rc4.o
CC	libavutil/reverse.o
CC	libavutil/refstruct.o
CC	libavutil/riscv/cpu.o
CC	libavutil/ripemd.o
CC	libavutil/riscv/fixed_dsp_init.o
AS	libavutil/riscv/fixed_dsp_rvv.o
CC	libavutil/riscv/cpu_common.o
CC	libavutil/riscv/float_dsp_init.o
AS	libavutil/riscv/float_dsp_rvv.o
CC	libavutil/riscv/lls_init.o
AS	libavutil/riscv/lls_rvv.o
CC	libavutil/samplefmt.o
CC	libavutil/sha.o
CC	libavutil/side_data.o
CC	libavutil/slicethread.o
CC	libavutil/sha512.o
CC	libavutil/spherical.o
CC	libavutil/stereo3d.o
CC	libavutil/tdrdi.o
CC	libavutil/tea.o
CC	libavutil/threadmessage.o
CC	libavutil/time.o
CC	libavutil/timecode.o
CC	libavutil/timecode_internal.o
CC	libavutil/timestamp.o
CC	libavutil/tree.o
CC	libavutil/tx.o
CC	libavutil/tx_double.o
CC	libavutil/twofish.o
CC	libavutil/tx_float.o
CC	libavutil/tx_int32.o
CC	libavutil/utils.o
CC	libavutil/uuid.o
CC	libavutil/version.o
CC	libavutil/video_enc_params.o
CC	libavutil/video_hint.o
CC	libavutil/xga_font_data.o
CC	libavutil/xtea.o
AR	libavutil/libavutil.a
AR	libswscale/libswscale.a
AR	libavcodec/libavcodec.a
LD	tests/checkasm/checkasm
/home/yuhe/project/local/riscv/bin/../lib/gcc/riscv64-unknown-linux-gnu/15.1.0/../../../../riscv64-unknown-linux-gnu/bin/ld: tests/checkasm/rv40dsp.o: in function `ff_rv40dsp_init':
/home/yuhe/project/cmdTool/workplace/FFmpeg/build/src/libavcodec/rv40dsp.c:706:(.text.unlikely+0x362): undefined reference to `ff_rv40dsp_init_riscv'
/home/yuhe/project/local/riscv/bin/../lib/gcc/riscv64-unknown-linux-gnu/15.1.0/../../../../riscv64-unknown-linux-gnu/bin/ld: libavcodec/libavcodec.a(aacpsdsp_float.o): in function `ff_psdsp_init':
/home/yuhe/project/cmdTool/workplace/FFmpeg/build/src/libavcodec/aacpsdsp_template.c:230:(.text.unlikely+0x56): undefined reference to `ff_psdsp_init_riscv'
/home/yuhe/project/local/riscv/bin/../lib/gcc/riscv64-unknown-linux-gnu/15.1.0/../../../../riscv64-unknown-linux-gnu/bin/ld: libavcodec/libavcodec.a(ac3dsp.o): in function `ff_ac3dsp_init':
/home/yuhe/project/cmdTool/workplace/FFmpeg/build/src/libavcodec/ac3dsp.c:401:(.text.unlikely+0x62): undefined reference to `ff_ac3dsp_init_riscv'
/home/yuhe/project/local/riscv/bin/../lib/gcc/riscv64-unknown-linux-gnu/15.1.0/../../../../riscv64-unknown-linux-gnu/bin/ld: libavcodec/libavcodec.a(alacdsp.o): in function `ff_alacdsp_init':
/home/yuhe/project/cmdTool/workplace/FFmpeg/build/src/libavcodec/alacdsp.c:62:(.text.unlikely+0x16): undefined reference to `ff_alacdsp_init_riscv'
/home/yuhe/project/local/riscv/bin/../lib/gcc/riscv64-unknown-linux-gnu/15.1.0/../../../../riscv64-unknown-linux-gnu/bin/ld: libavcodec/libavcodec.a(audiodsp.o): in function `ff_audiodsp_init':
/home/yuhe/project/cmdTool/workplace/FFmpeg/build/src/libavcodec/audiodsp.c:76:(.text.unlikely+0x1e): undefined reference to `ff_audiodsp_init_riscv'
/home/yuhe/project/local/riscv/bin/../lib/gcc/riscv64-unknown-linux-gnu/15.1.0/../../../../riscv64-unknown-linux-gnu/bin/ld: libavcodec/libavcodec.a(blockdsp.o): in function `ff_blockdsp_init':
/home/yuhe/project/cmdTool/workplace/FFmpeg/build/src/libavcodec/blockdsp.c:71:(.text.unlikely+0x28): undefined reference to `ff_blockdsp_init_riscv'
/home/yuhe/project/local/riscv/bin/../lib/gcc/riscv64-unknown-linux-gnu/15.1.0/../../../../riscv64-unknown-linux-gnu/bin/ld: libavcodec/libavcodec.a(bswapdsp.o): in function `ff_bswapdsp_init':
/home/yuhe/project/cmdTool/workplace/FFmpeg/build/src/libavcodec/bswapdsp.c:55:(.text.unlikely+0x14): undefined reference to `ff_bswapdsp_init_riscv'
/home/yuhe/project/local/riscv/bin/../lib/gcc/riscv64-unknown-linux-gnu/15.1.0/../../../../riscv64-unknown-linux-gnu/bin/ld: libavcodec/libavcodec.a(flacdsp.o): in function `ff_flacdsp_init':
/home/yuhe/project/cmdTool/workplace/FFmpeg/build/src/libavcodec/flacdsp.c:156:(.text.unlikely+0x100): undefined reference to `ff_flacdsp_init_riscv'
/home/yuhe/project/local/riscv/bin/../lib/gcc/riscv64-unknown-linux-gnu/15.1.0/../../../../riscv64-unknown-linux-gnu/bin/ld: libavcodec/libavcodec.a(fmtconvert.o): in function `ff_fmt_convert_init':
/home/yuhe/project/cmdTool/workplace/FFmpeg/build/src/libavcodec/fmtconvert.c:56:(.text.unlikely+0x14): undefined reference to `ff_fmt_convert_init_riscv'
/home/yuhe/project/local/riscv/bin/../lib/gcc/riscv64-unknown-linux-gnu/15.1.0/../../../../riscv64-unknown-linux-gnu/bin/ld: libavcodec/libavcodec.a(g722dsp.o): in function `ff_g722dsp_init':
/home/yuhe/project/cmdTool/workplace/FFmpeg/build/src/libavcodec/g722dsp.c:75:(.text.unlikely+0xa): undefined reference to `ff_g722dsp_init_riscv'
/home/yuhe/project/local/riscv/bin/../lib/gcc/riscv64-unknown-linux-gnu/15.1.0/../../../../riscv64-unknown-linux-gnu/bin/ld: libavcodec/libavcodec.a(h263dsp.o): in function `ff_h263dsp_init':
/home/yuhe/project/cmdTool/workplace/FFmpeg/build/src/libavcodec/h263dsp.c:123:(.text.unlikely+0x14): undefined reference to `ff_h263dsp_init_riscv'
/home/yuhe/project/local/riscv/bin/../lib/gcc/riscv64-unknown-linux-gnu/15.1.0/../../../../riscv64-unknown-linux-gnu/bin/ld: libavcodec/libavcodec.a(h264chroma.o): in function `ff_h264chroma_init':
/home/yuhe/project/cmdTool/workplace/FFmpeg/build/src/libavcodec/h264chroma.c:60:(.text.unlikely+0x7c): undefined reference to `ff_h264chroma_init_riscv'
/home/yuhe/project/local/riscv/bin/../lib/gcc/riscv64-unknown-linux-gnu/15.1.0/../../../../riscv64-unknown-linux-gnu/bin/ld: libavcodec/libavcodec.a(h264dsp.o): in function `ff_h264dsp_init':
/home/yuhe/project/cmdTool/workplace/FFmpeg/build/src/libavcodec/h264dsp.c:162:(.text.unlikely+0x3ce): undefined reference to `ff_h264dsp_init_riscv'
/home/yuhe/project/local/riscv/bin/../lib/gcc/riscv64-unknown-linux-gnu/15.1.0/../../../../riscv64-unknown-linux-gnu/bin/ld: libavcodec/libavcodec.a(h264qpel.o): in function `ff_h264qpel_init':
/home/yuhe/project/cmdTool/workplace/FFmpeg/build/src/libavcodec/h264qpel.c:106:(.text.unlikely+0x14aa): undefined reference to `ff_h264qpel_init_riscv'
/home/yuhe/project/local/riscv/bin/../lib/gcc/riscv64-unknown-linux-gnu/15.1.0/../../../../riscv64-unknown-linux-gnu/bin/ld: libavcodec/libavcodec.a(huffyuvdsp.o): in function `ff_huffyuvdsp_init':
/home/yuhe/project/cmdTool/workplace/FFmpeg/build/src/libavcodec/huffyuvdsp.c:91:(.text.unlikely+0x1e): undefined reference to `ff_huffyuvdsp_init_riscv'
/home/yuhe/project/local/riscv/bin/../lib/gcc/riscv64-unknown-linux-gnu/15.1.0/../../../../riscv64-unknown-linux-gnu/bin/ld: libavcodec/libavcodec.a(idctdsp.o): in function `ff_idctdsp_init':
/home/yuhe/project/cmdTool/workplace/FFmpeg/build/src/libavcodec/idctdsp.c:293:(.text.unlikely+0x28c): undefined reference to `ff_idctdsp_init_riscv'
/home/yuhe/project/local/riscv/bin/../lib/gcc/riscv64-unknown-linux-gnu/15.1.0/../../../../riscv64-unknown-linux-gnu/bin/ld: libavcodec/libavcodec.a(jpeg2000dsp.o): in function `ff_jpeg2000dsp_init':
/home/yuhe/project/cmdTool/workplace/FFmpeg/build/src/libavcodec/jpeg2000dsp.c:100:(.text.unlikely+0x1e): undefined reference to `ff_jpeg2000dsp_init_riscv'
/home/yuhe/project/local/riscv/bin/../lib/gcc/riscv64-unknown-linux-gnu/15.1.0/../../../../riscv64-unknown-linux-gnu/bin/ld: libavcodec/libavcodec.a(lossless_audiodsp.o): in function `ff_llauddsp_init':
/home/yuhe/project/cmdTool/workplace/FFmpeg/build/src/libavcodec/lossless_audiodsp.c:65:(.text.unlikely+0x14): undefined reference to `ff_llauddsp_init_riscv'
/home/yuhe/project/local/riscv/bin/../lib/gcc/riscv64-unknown-linux-gnu/15.1.0/../../../../riscv64-unknown-linux-gnu/bin/ld: libavcodec/libavcodec.a(lossless_videodsp.o): in function `ff_llviddsp_init':
/home/yuhe/project/cmdTool/workplace/FFmpeg/build/src/libavcodec/lossless_videodsp.c:126:(.text.unlikely+0x32): undefined reference to `ff_llviddsp_init_riscv'
/home/yuhe/project/local/riscv/bin/../lib/gcc/riscv64-unknown-linux-gnu/15.1.0/../../../../riscv64-unknown-linux-gnu/bin/ld: libavcodec/libavcodec.a(lossless_videoencdsp.o): in function `ff_llvidencdsp_init':
/home/yuhe/project/cmdTool/workplace/FFmpeg/build/src/libavcodec/lossless_videoencdsp.c:107:(.text.unlikely+0x1e): undefined reference to `ff_llvidencdsp_init_riscv'
/home/yuhe/project/local/riscv/bin/../lib/gcc/riscv64-unknown-linux-gnu/15.1.0/../../../../ri
```
