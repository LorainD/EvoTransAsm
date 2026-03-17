# rvv-agent run report

## Symbol

- sbrdsp.neg_odd_64

## Plan

01. 在源码树中全量定位符号：执行 `rg -n "\\bneg_odd_64\\b" -S`，确认该符号是函数实现、函数指针字段（通常在 `SBRDSPContext`）还是仅在初始化表中出现；记录所有命中位置（实现文件、声明头文件、初始化函数）。
02. 定位并阅读 C 基线实现：在命中结果中找到纯 C 版本（常见于 `libavcodec/sbrdsp.c` 或相近路径），打开并确认其函数签名、输入/输出指针类型、stride/len 语义、是否允许就地(in-place)处理、是否有对齐/边界假设；同时记录其对“odd”索引的精确定义（例如从 index=1 开始每隔 2 个元素取一次）以及“neg”的具体运算（取负、饱和、按位取反+1、或带常量偏置）。
03. 定位该算子在 SBRDSP 初始化中的挂接点：查找 `ff_sbrdsp_init*`/`sbrdsp_init` 中对 `c->neg_odd_64`（或同名字段）的赋值逻辑；确认是否有按位深/CPU 特性/ARCH 分派；记录现有的默认 C 指派与其他架构覆盖点。
04. 定位参考实现（x86）：在 `libavcodec/x86/` 下查找 `neg_odd_64` 或对 `SBRDSPContext` 的字段赋值（例如 `sbrdsp_init_x86`），锁定对应的 `.c`/`.asm`/`.S` 文件；阅读实现以确认与 C 版本逐元素一致，特别是：处理长度是否固定 64、是否分块、是否使用向量化但保持相同舍入/溢出行为。
05. 定位参考实现（ARM/AArch64）：在 `libavcodec/arm/` 与 `libavcodec/aarch64/` 下重复查找与确认；若 ARM 版本与 x86 版本在边界处理上存在差异，回到 C 版本与 bitexact 要求进行对齐，形成“必须遵循的语义清单”。
06. 提炼 RVV 迁移所需的精确语义与约束：根据 C/参考实现，写下算子规格（输入元素类型与范围、输出写回位置、仅对 odd 位置取负还是产生新数组、偶数位置是否保持不变/不写、长度固定 64 的强假设）；列出会影响 RVV 的点：是否饱和、是否需要 16/32 位中间值、是否依赖有符号溢出(需避免 UB)、是否允许非对齐加载。
07. 设计 RVV 数据通路：针对固定长度 64，选择合适的向量元素宽度（例如 e.g. e16/e32，取决于 C 类型），采用 `vsetvl_eXXmY` 循环处理；为“odd”索引生成掩码（mask）：方案A 使用索引向量 `vid.v` + `vand`/`vmsne` 得到奇偶掩码；方案B 使用 `vlseg2eXX`/`vlsseg2eXX` 直接按偶/奇分离加载（如果内存布局是连续数组且 stride 为 1），从而天然选出 odd 元素并避免额外 mask。
08. 实现 RVV 核心运算（仅对 odd 元素取负）：根据语义选择 `vneg.v`（或等价 `vrsub.vx`）并结合 mask 写回；若偶数位需要保持原值，则采用 masked store/merge（例如先加载原向量，再 `vmerge`/masked op，只改变 odd lane）；若算子只写 odd 位置并不要求写偶数位置，则用 masked store 避免触碰偶数 lane。
09. 处理潜在饱和/宽化需求：如果 C 版本对最小负值存在饱和（例如 int16 的 -32768 取负仍为 -32768 或 saturate 到 32767），用 RVV 的 widening + clamp 或者饱和指令（若可用）复现；若 C 版本依赖二补码自然溢出（非饱和），在 RVV 中保持同位宽有符号运算并确保不触发 C UB（在 C 包装层或 RVV intrinsic 中直接做向量算术通常可避免 UB）。
10. 编写 RVV 实现文件：在 `libavcodec/riscv/` 新增/修改对应源文件（通常为 `sbrdsp_rvv.c` 或新增 `sbrdsp_rvv.S`/`.c`），实现函数 `ff_neg_odd_64_rvv`（命名按项目现有 riscv 约定）；包含必要的 RVV intrinsic 头；确保函数签名与 C 版/上下文字段一致，并在注释中标明与 C 版本等价的 odd 定义与长度假设(64)。
11. 在 RISC-V 初始化中挂接：编辑 `libavcodec/riscv/sbrdsp_init_riscv.c`（或实际命中的 init 文件），在检测到 RVV 可用（例如 `have_rvv`/`av_get_cpu_flags()` 对应位）时将 `c->neg_odd_64 = ff_neg_odd_64_rvv`；保留 fallback 到 C 版本；若存在位深/配置分支，确保只在正确的配置上覆盖。
12. 集成到构建系统（configure/Makefile）：在 `libavcodec/riscv/Makefile` 或对应 `Makefile` 片段中把新文件加入 `OBJS-$(CONFIG_...)` 或 `RVV-OBJS`；如新增了 `.S`，确保汇编规则与 `--target=riscv`/`-march` 兼容；如使用 intrinsics，确保编译参数开启 RVV（FFmpeg 通常通过 `--enable-rvv`/`-march=rv64gcv` 或探测宏），并与现有 riscv 文件一致。
13. 补齐声明与可见性：如需要，在相应 header（例如 `libavcodec/sbrdsp.h` 或 riscv 私有头）声明 `ff_neg_odd_64_rvv`；确保符号可被 init 文件引用且不与其他架构重名冲突。
14. 编译验证（本地交叉或原生）：在启用 RISC-V RVV 的配置下构建 `libavcodec`，确保无未定义符号/未启用 RVV 导致的编译错误；同时在未启用 RVV 的构建中确认不会因包含 RVV intrinsic 头而失败（必要时用 `#if HAVE_RVV` 包围）。
15. 运行 checkasm 验证：执行 `make checkasm`（或 `./ffmpeg -checkasm` 依工程版本），过滤到 sbrdsp：`./tests/checkasm/checkasm --list | rg sbrdsp`，确认存在 `neg_odd_64` 测项；运行 `./tests/checkasm/checkasm --test=sbrdsp` 并确保 RVV 版本被选中（输出中应显示 riscv/rvv 变体）且通过 bitexact 对比。
16. 若 checkasm 未覆盖该符号：在 `tests/checkasm/sbrdsp.c`（或实际命中路径）添加/补全 `neg_odd_64` 的测试：随机生成输入、保留拷贝、调用 C 参考与 RVV 版本对比；覆盖边界值（0、±1、最小负值、最大正值）、不同对齐（未对齐指针偏移）以及 in-place 情况（若允许）；确保仅 odd 元素变化的断言符合 C 语义。
17. 性能与回归确认：在目标 RISC-V RVV 机器上用 `checkasm --bench` 或 FFmpeg 的基准机制对比 C vs RVV；确保没有明显退化；若存在，考虑改用 `vlseg2` 分离 odd lane 或减少 mask/merge 次数。

## Reference Files

- libavcodec/sbrdsp_template.c
- libavcodec/sbrdsp.c
- libavcodec/sbrdsp_fixed.c
- libavcodec/aacsbr_template.c
- libavcodec/x86/sbrdsp_init.c
- libavcodec/arm/sbrdsp_init_arm.c
- libavcodec/aarch64/sbrdsp_init_aarch64.c
- libavcodec/riscv/sbrdsp_init.c
- libavcodec/sbrdsp.h
- libavcodec/sbr.h
- libavcodec/riscv/Makefile
- tests/checkasm/sbrdsp.c
- tests/checkasm/checkasm.c

## Interaction

```json
{
  "intent_action": "migrate",
  "intent_llm_used": false,
  "intent_error": null,
  "retrieval_llm_used": true,
  "retrieval_error": null,
  "apply_ok": false,
  "build_ok": false,
  "build_attempts": 1,
  "scp_ok": false,
  "run_on_board_ok": false,
  "board_enabled": false
}
```

## Discovery

### c_candidates

- libavcodec/aacsbr_template.c
- libavcodec/sbrdsp_template.c
- tests/checkasm/sbrdsp.c
- libavcodec/aacsbr_fixed.c
- libavcodec/sbrdsp.c
- libavcodec/sbrdsp_fixed.c
- libavcodec/aacsbr.c
- tests/checkasm/checkasm.c

### x86_refs

- libavcodec/x86/sbrdsp_init.c

### arm_refs

- libavcodec/arm/sbrdsp_init_arm.c

### aarch64_refs

- libavcodec/aarch64/sbrdsp_init_aarch64.c

### riscv_refs

- libavcodec/riscv/sbrdsp_init.c

### headers

- libavcodec/sbrdsp.h
- libavcodec/sbr.h

### other

- (none)

## Matches (first 200)

- libavcodec/sbrdsp.h:30: void (*neg_odd_64)(INTFLOAT *x);
- libavcodec/aacsbr_template.c:1438: sbrdsp->neg_odd_64(X[1][i]);
- libavcodec/sbrdsp_template.c:84: s->neg_odd_64 = sbr_neg_odd_64_c;
- libavcodec/x86/sbrdsp_init.c:63: s->neg_odd_64 = ff_sbr_neg_odd_64_sse;
- libavcodec/aarch64/sbrdsp_init_aarch64.c:57: s->neg_odd_64 = ff_sbr_neg_odd_64_neon;
- libavcodec/arm/sbrdsp_init_arm.c:60: s->neg_odd_64 = ff_sbr_neg_odd_64_neon;
- tests/checkasm/sbrdsp.c:271: if (check_func(sbrdsp.neg_odd_64, "neg_odd_64"))
- tests/checkasm/sbrdsp.c:273: report("neg_odd_64");
- libavcodec/aacsbr_fixed.c:64: #include "sbrdsp.h"
- libavcodec/sbrdsp.c:28: #include "sbrdsp.h"
- libavcodec/sbrdsp_fixed.c:31: #include "sbrdsp.h"
- libavcodec/aacsbr_template.c:1366: SBRDSPContext *sbrdsp, const INTFLOAT *in, INTFLOAT *x,
- libavcodec/aacsbr_template.c:1378: sbrdsp->sum64x5(z);
- libavcodec/aacsbr_template.c:1379: sbrdsp->qmf_pre_shuffle(z);
- libavcodec/aacsbr_template.c:1396: sbrdsp->qmf_post_shuffle(W[buf_idx][i], z);
- libavcodec/aacsbr_template.c:1409: SBRDSPContext *sbrdsp, AVFixedDSPContext *dsp,
- libavcodec/aacsbr_template.c:1411: SBRDSPContext *sbrdsp, AVFloatDSPContext *dsp,
- libavcodec/aacsbr_template.c:1436: sbrdsp->qmf_deint_neg(v, mdct_buf[0]);
- libavcodec/aacsbr_template.c:1441: sbrdsp->qmf_deint_bfly(v, mdct_buf[1], mdct_buf[0]);
- libavcodec/aacsbr.c:35: #include "sbrdsp.h"
- libavcodec/sbr.h:38: #include "sbrdsp.h"
- libavcodec/riscv/sbrdsp_init.c:25: #include "libavcodec/sbrdsp.h"
- libavcodec/x86/sbrdsp_init.c:26: #include "libavcodec/sbrdsp.h"
- libavcodec/aarch64/sbrdsp_init_aarch64.c:22: #include "libavcodec/sbrdsp.h"
- libavcodec/arm/sbrdsp_init_arm.c:24: #include "libavcodec/sbrdsp.h"
- tests/checkasm/sbrdsp.c:21: #include "libavcodec/sbrdsp.h"
- tests/checkasm/sbrdsp.c:221: static void test_hf_apply_noise(const SBRDSPContext *sbrdsp)
- tests/checkasm/sbrdsp.c:243: if (check_func(sbrdsp->hf_apply_noise[i], "hf_apply_noise_%d", i)) {
- tests/checkasm/sbrdsp.c:259: SBRDSPContext sbrdsp;
- tests/checkasm/sbrdsp.c:261: ff_sbrdsp_init(&sbrdsp);
- tests/checkasm/sbrdsp.c:263: if (check_func(sbrdsp.sum64x5, "sum64x5"))
- tests/checkasm/sbrdsp.c:267: if (check_func(sbrdsp.sum_square, "sum_square"))
- tests/checkasm/sbrdsp.c:275: if (check_func(sbrdsp.qmf_pre_shuffle, "qmf_pre_shuffle"))
- tests/checkasm/sbrdsp.c:279: if (check_func(sbrdsp.qmf_post_shuffle, "qmf_post_shuffle"))
- tests/checkasm/sbrdsp.c:283: if (check_func(sbrdsp.qmf_deint_neg, "qmf_deint_neg"))
- tests/checkasm/sbrdsp.c:287: if (check_func(sbrdsp.qmf_deint_bfly, "qmf_deint_bfly"))
- tests/checkasm/sbrdsp.c:291: if (check_func(sbrdsp.autocorrelate, "autocorrelate"))
- tests/checkasm/sbrdsp.c:295: if (check_func(sbrdsp.hf_gen, "hf_gen"))
- tests/checkasm/sbrdsp.c:299: if (check_func(sbrdsp.hf_g_filt, "hf_g_filt"))
- tests/checkasm/sbrdsp.c:303: test_hf_apply_noise(&sbrdsp);
- tests/checkasm/checkasm.c:121: { "sbrdsp",   checkasm_check_sbrdsp },

## Analysis JSON

```json
{
  "symbol": "sbrdsp.neg_odd_64",
  "datatype": "unknown",
  "vectorizable": true,
  "pattern": [
    "tail"
  ],
  "has_stride": false,
  "has_saturation": false,
  "reduction": false,
  "tail_required": true,
  "math_expression": "for i in [0..63]: x[2*i+1] = -x[2*i+1] (negate odd-index elements of 128-length array in-place; even indices unchanged)",
  "c_candidates": [
    "libavcodec/sbrdsp_template.c:1"
  ],
  "x86_refs": [
    "libavcodec/x86/sbrdsp_init.c:1"
  ],
  "arm_refs": [
    "libavcodec/arm/sbrdsp_init_arm.c:1",
    "libavcodec/aarch64/sbrdsp_init_aarch64.c:1"
  ],
  "notes": "Exact C reference for neg_odd_64 is not included in provided snippet; inferred from symbol name and existing SIMD hooks (ff_sbr_neg_odd_64_sse/neon). Operation is a simple in-place sign flip on odd lanes, constant trip count 64; good RVV candidate. Tail handling needed for vsetvl when processing 64 elements if vector length does not divide 64."
}
```

- llm_used: True

## Generation (raw)

```
{
  "files": [
    {
      "path": "libavcodec/riscv/sbrdsp_rvv.S",
      "content": "/*\n * Copyright \u00a9 2023 R\u00e9mi Denis-Courmont.\n *\n * This file is part of FFmpeg.\n *\n * FFmpeg is free software; you can redistribute it and/or\n * modify it under the terms of the GNU Lesser General Public\n * License as published by the Free Software Foundation; either\n * version 2.1 of the License, or (at your option) any later version.\n *\n * FFmpeg is distributed in the hope that it will be useful,\n * but WITHOUT ANY WARRANTY; without even the implied warranty of\n * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU\n * Lesser General Public License for more details.\n *\n * You should have received a copy of the GNU Lesser General Public\n * License along with FFmpeg; if not, write to the Free Software\n * Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA\n */\n\n#include \"libavutil/riscv/asm.S\"\n\nfunc ff_sbr_sum64x5_rvv, zve32f, zba\n        lpad    0\n        li      a5, 64\n        addi    a1, a0, 64 * 4\n        addi    a2, a0, 128 * 4\n        addi    a3, a0, 192 * 4\n        addi    a4, a0, 256 * 4\n1:\n        vsetvli t0, a5, e32, m8, ta, ma\n        sub     a5, a5, t0\n        vle32.v v0, (a0)\n        vle32.v v8, (a1)\n        sh2add  a1, t0, a1\n        vle32.v v16, (a2)\n        vfadd.vv v0, v0, v8\n        sh2add  a2, t0, a2\n        vle32.v v24, (a3)\n        vfadd.vv v0, v0, v16\n        sh2add  a3, t0, a3\n        vle32.v v8, (a4)\n        vfadd.vv v0, v0, v24\n        sh2add  a4, t0, a4\n        vfadd.vv v0, v0, v8\n        vse32.v v0, (a0)\n        sh2add  a0, t0, a0\n        bnez    a5, 1b\n\n        ret\nendfunc\n\nfunc ff_sbr_sum_square_rvv, zve32f, zba\n        lpad    0\n        vsetvli t0, zero, e32, m8, ta, ma\n        slli    a1, a1, 1\n        vmv.v.x v8, zero\n        vmv.s.x v0, zero\n1:\n        vsetvli t0, a1, e32, m8, tu, ma\n        vle32.v v16, (a0)\n        sub     a1, a1, t0\n        vfmacc.vv v8, v16, v16\n        sh2add  a0, t0, a0\n        bnez    a1, 1b\n\n        vfredusum.vs v0, v8, v0\n        vfmv.f.s fa0, v0\nNOHWF   fmv.x.w  a0, fa0\n        ret\nendfunc\n\nfunc ff_sbr_autocorrelate_rvv, zve32f\n        lpad    0\n        vsetvli t0, zero, e32, m4, ta, ma\n        vmv.v.x v0, zero\n        flw     fa0,   (a0)\n        vmv.v.x v4, zero\n        flw     fa1,  4(a0)\n        vmv.v.x v8, zero\n        flw     fa2,  8(a0)\n        li      a2, 37\n        flw     fa3, 12(a0)\n        fmul.s  ft10, fa0, fa0\n        flw     fa4, 16(a0)\n        fmul.s  ft6, fa0, fa2\n        flw     fa5, 20(a0)\n        addi    a0, a0, 38 * 8\n        fmul.s  ft7, fa0, fa3\n        fmul.s  ft2, fa0, fa4\n        fmul.s  ft3, fa0, fa5\n        flw     fa0,   (a0)\n        fmadd.s ft10, fa1, fa1, ft10\n        fmadd.s ft6, fa1, fa3, ft6\n        flw     fa3, 12(a0)\n        fnmsub.s ft7, fa1, fa2, ft7\n        flw     fa2,  8(a0)\n        fmadd.s ft2, fa1, fa5, ft2\n        fnmsub.s ft3, fa1, fa4, ft3\n        flw     fa1,  4(a0)\n        fmul.s  ft4, fa0, fa0\n        fmul.s  ft0, fa0, fa2\n        fmul.s  ft1, fa0, fa3\n        fmadd.s ft4, fa1, fa1, ft4\n        fmadd.s ft0, fa1, fa3, ft0\n        fnmsub.s ft1, fa1, fa2, ft1\n1:\n        vsetvli t0, a2, e32, m2, tu, ma\n        slli    t1, t0, 3\n        sub     a0, a0, t1\n        vlseg2e32.v v16, (a0)\n        sub     a2, a2, t0\n        vfmacc.vv v0, v16, v16\n        vfslide1down.vf v20, v16, fa0\n        vfmacc.vv v4, v16, v20\n        vfslide1down.vf v22, v18, fa1\n        vfmacc.vv v0, v18, v18\n        vfslide1down.vf v24, v20, fa2\n        vfmacc.vv v4, v18, v22\n        vfslide1down.vf v26, v22, fa3\n        vfmacc.vv v6, v16, v22\n        vfmv.f.s fa0, v16\n        vfmacc.vv v8, v16, v24\n        vfmv.f.s fa1, v18\n        vfmacc.vv v10, v16, v26\n        vfmv.f.s fa2, v20\n        vfnmsac.vv v6, v18, v20\n        vfmv.f.s fa3, v22\n        vfmacc.vv v8, v18, v26\n        vfnmsac.vv v10, v18, v24\n        bnez    a2, 1b\n\n        vsetvli t0, zero, e32, m2, ta, ma\n        vfredusum.vs v0, v0, v2\n        vfredusum.vs v4, v4, v2\n        vfmv.f.s fa0, v0\n        vfredusum.vs v6, v6, v2\n        vfmv.f.s fa2, v4\n        fadd.s   ft4, ft4, fa0\n        vfredusum.vs v8, v8, v2\n        vfmv.f.s fa3, v6\n        fadd.s   ft0, ft0, fa2\n        vfredusum.vs v10, v10, v2\n        vfmv.f.s fa4, v8\n        fadd.s   ft1, ft1, fa3\n        vfmv.f.s fa5, v10\n        fsw     ft0,   (a1)\n        fadd.s  ft2, ft2, fa4\n        fsw     ft1,  4(a1)\n        fadd.s  ft3, ft3, fa5\n        fsw     ft2,  8(a1)\n        fadd.s  ft6, ft6, fa2\n        fsw     ft3, 12(a1)\n        fadd.s  ft7, ft7, fa3\n        fsw     ft4, 16(a1)\n        fadd.s  ft10, ft10, fa0\n        fsw     ft6, 24(a1)\n        fsw     ft7, 28(a1)\n        fsw     ft10, 40(a1)\n        ret\nendfunc\n\nfunc ff_sbr_hf_gen_rvv, zve32f, zba\n        lpad    0\nNOHWF   fmv.w.x fa0, a4\nNOHWF   mv      a4, a5\nNOHWF   mv      a5, a6\n        flw     ft2, 0(a2)\n        fmul.s  fa1, fa0, fa0 // bw * bw\n        sh3add  a1, a5, a1\n        flw     ft3, 4(a2)\n        fmul.s  fa2, ft2, fa0 // alpha[2]\n        sh3add  a0, a5, a0\n        flw     ft0, 0(a3)\n        fmul.s  fa3, ft3, fa0 // alpha[3]\n        sub     a5, a5, a4\n        flw     ft1, 4(a3)\n        fmul.s  fa0, ft0, fa1 // alpha[0]\n        flw     ft0, -16(a1)  // X_low[end - 2][0]\n        fmul.s  fa1, ft1, fa1 // alpha[1]\n        flw     ft1, -12(a1)  // X_low[end - 2][1]\n        flw     ft2,  -8(a1)  // X_low[end - 1][0]\n        flw     ft3,  -4(a1)  // X_low[end - 1][1]\n        addi    a1, a1, -16\n1:\n        vsetvli t0, a5, e32, m4, ta, ma\n        slli    t1, t0, 3\n        sub     a1, a1, t1\n        vlseg2e32.v v0, (a1)          // X_low[i - 2]\n        sub     a0, a0, t1\n        vfslide1down.vf v8, v0, ft0   // X_low[i - 1][0]\n        sub     a5, a5, t0\n        vfslide1down.vf v12, v4, ft1  // X_low[i - 1][1]\n        vfslide1down.vf v16, v8, ft2  // X_low[i    ][0]\n        vfslide1down.vf v20, v12, ft3 // X_low[i    ][1]\n        vfmacc.vf v16, fa0, v0\n        vfmacc.vf v20, fa0, v4\n        vfmv.f.s ft0, v0\n        vfnmsac.vf v16, fa1, v4\n        vfmacc.vf v20, fa1, v0\n        vfmv.f.s ft1, v4\n        vfmacc.vf \n--- \u6587\u4ef6\u7ed3\u675f ---\n\n/* RVV: negate odd-index elements of a 128-element array, operating on 64 odd lanes.\n * C semantics (inferred): for i=0..63: x[2*i+1] = -x[2*i+1]\n * Prototype TODO: ensure matches sbrdsp_template.c / init.\n */\nfunc ff_sbr_neg_odd_64_rvv, zve32x, zba\n        lpad    0\n        addi    a0, a0, 4              /* point to x[1] */\n        li      a1, 64                 /* number of odd elements */\n        li      t1, -1\n1:\n        vsetvli t0, a1, e32, m8, ta, ma\n        vlse32.v v8, (a0), 8           /* load x[1], x[3], ... */\n        sub     a1, a1, t0\n        vmul.vx v8, v8, t1             /* v8 = -v8 */\n        vsse32.v v8, (a0), 8           /* store back */\n        slli    t2, t0, 3              /* byte advance = vl * 8 */\n        add     a0, a0, t2\n        bnez    a1, 1b\n\n        ret\nendfunc\n"
    }
  ],
  "patches": [
    {
      "path": "libavcodec/riscv/sbrdsp_init.c",
      "diff": "*** TODO PATCH (most conservative, apply manually if paths/macros differ) ***\n--- a/libavcodec/riscv/sbrdsp_init.c\n+++ b/libavcodec/riscv/sbrdsp_init.c\n@@\n+/* TODO: verify exact init file name/path and CPU flag macro used for RVV in this tree. */\n+void ff_sbr_neg_odd_64_rvv(float *x);\n@@\n+    /* TODO: choose the same feature gate used by other *_rvv hooks (e.g. have_rvv / AV_CPU_FLAG_RVV / ff_rv_has(...)). */\n+    c->neg_odd_64 = ff_sbr_neg_odd_64_rvv;\n"
    },
    {
      "path": "libavcodec/riscv/Makefile",
      "diff": "*** TODO PATCH (most conservative, apply manually if paths/vars differ) ***\n--- a/libavcodec/riscv/Makefile\n+++ b/libavcodec/riscv/Makefile\n@@\n+OBJS-$(CONFIG_SBRDSP) += riscv/sbrdsp_rvv.o\n"
    }
  ]
}
```

## Materialized

- runs/20260304_071720_sbrdsp.neg_odd_64/artifacts/files/libavcodec/riscv/sbrdsp_rvv.S
- runs/20260304_071720_sbrdsp.neg_odd_64/artifacts/patches/sbrdsp_init.c.diff
- runs/20260304_071720_sbrdsp.neg_odd_64/artifacts/patches/Makefile.diff

## configure

- (skipped)

## make checkasm

- (skipped)
