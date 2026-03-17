# rvv-agent run report

## Symbol

- sbrdsp.neg_odd_64

## Plan

01. 在 FFmpeg 源码树中定位符号声明与调用链：使用 `rg -n "neg_odd_64" -S`（或 `grep -R`）查找 `sbrdsp.neg_odd_64` 的赋值位置（通常在 `ff_sbrdsp_init_*()` 或 `sbrdsp_init.c`/`sbrdsp.c` 中），记录函数指针所属的上下文结构体（如 `SBRDSPContext`）以及在哪些编解码路径被调用（继续向上追 `ctx->neg_odd_64(` 的调用点）。
02. 定位并理解 C 基准实现：对 `rg -n "static.*neg_odd_64|void.*neg_odd_64"` 命中的 C 函数实现进行阅读，确认函数签名（参数类型、对齐假设、stride/长度是否固定为 64、是否 in-place）、数据类型（int16/int32/float 等）和精确语义（"odd" 的索引集合：1,3,5... 还是与块/子带相关），并整理成可测的伪代码（例如：for i in 0..63 step 2: dst[i] = -src[i] 或对奇数位取负）。
03. 定位参考 SIMD 实现（x86/ARM）：用 `rg -n "neg_odd_64" libavcodec/x86 libavcodec/arm libavcodec/aarch64 -S` 查找是否存在同名/等价实现；若没有同名，查找 SBR DSP 的 init 文件里对 `neg_odd_64` 的平台赋值（如 `ff_sbrdsp_init_x86` / `ff_sbrdsp_init_arm` / `ff_sbrdsp_init_aarch64`），并打开对应汇编/内联实现文件（可能在 `libavcodec/x86/sbrdsp.asm`、`libavcodec/arm/sbrdsp_neon.S`、`libavcodec/aarch64/sbrdsp_neon.S`）。记录：寄存器宽度、向量分块策略、是否假设 16-byte 对齐、如何处理 odd lane（mask/shuffle/xor sign bit 等）。
04. 补齐测试入口（如缺失）：检查 `tests/checkasm/sbrdsp.c`（或相近文件）是否已有对 `neg_odd_64` 的 checkasm 用例。若没有，新增一个专门的 checkasm case：构造输入缓冲（覆盖负数/正数/极值/随机）、分别调用 C 实现与优化实现（通过函数指针）并逐元素比较 64 长度输出；若是 in-place，需拷贝两份输入并比较最终缓冲。确保测试覆盖对齐与非对齐地址（例如偏移 0..31）以捕捉 RVV 的非对齐访问问题。
05. 设计 RVV 数据布局与矢量化方案（针对长度=64 与 odd 索引）：根据 C 语义确定处理模式：A) 若仅对奇数元素取负且偶数不变，可使用 RVV mask（odd mask）对向量做有条件取负；B) 若输出只包含奇数位结果（压缩存储），则需要 `vrgather`/压缩存储策略。针对固定 64：按元素类型选择 LMUL 与 SEW（例如 int16 用 e16，int32 用 e32），采用动态 VL 循环：`for (i=0; i<64; i+=vl)`，用 `vsetvl_eXXmY(64-i)` 处理尾部。构造 odd mask：`vid.v` 生成索引，`vand`/`vmsne` 生成 (idx & 1)==1 的 mask；对取负：用 `vneg.v` 生成全取负，再用 `vmerge.vvm` 在 mask 下选择负值与原值，或用 `vxor`+`vsub` 等价实现（按数据类型）。
06. 编写 RVV 实现文件：在 `libavcodec/riscv/` 下新增/编辑 `sbrdsp_rvv.c`（或按项目现有命名如 `*_rvv.c`），实现一个与 C 版本同签名的函数（例如 `static void neg_odd_64_rvv(type *dst, const type *src)` 或 in-place 版本），包含 `<riscv_vector.h>` 并使用 RVV intrinsic。实现要点：1) 支持 src==dst（若 C 允许）；2) 使用 `vleXX`/`vseXX`，避免越界；3) mask 生成基于当前 vl 的 `vid`；4) 若仅奇数位取负，偶数位保持原样；5) 保持与参考实现相同的溢出/饱和语义（若 C 使用有符号取负且可能遇到 INT_MIN，需要确认是否依赖二补溢出；intrinsic 应保持同样行为）。
07. 在 RISC-V init 中挂接函数指针：定位 `ff_sbrdsp_init_riscv()`（若不存在则新建，例如 `libavcodec/riscv/sbrdsp_init.c`），在检测到 RVV 可用（`av_get_cpu_flags()` + `AV_CPU_FLAG_RVV` 或 FFmpeg 现有 riscv 特性宏）时，将 `c->neg_odd_64 = neg_odd_64_rvv;`。同时确保保留 C 回退路径（未开启 RVV 时不赋值）。
08. 集成到构建系统：修改 `libavcodec/riscv/Makefile`（或 `libavcodec/riscv/Makefile.objs`/`riscv/Makefile` 取决于树结构）将 `sbrdsp_rvv.o`、`sbrdsp_init.o`（若新增）加入对象列表；检查 `configure` 中对 RVV 的检测与 `HAVE_RVV`/`HAVE_RISCVV` 宏定义是否已存在，确保编译该文件需要的 CFLAGS（可能需要 `-march=rv64gcv` 或由 configure 自动注入）。必要时在对应 `*_deps` 中声明对 `rvv` 的依赖。
09. 添加/更新头文件与宏保护：在 RVV 源文件中用 `#if HAVE_RVV`（或 FFmpeg riscv 目录通用宏）包裹 RVV 代码；在 init 文件中同样用宏保护，避免非 RVV 编译失败。若需要，包含 `libavutil/riscv/cpu.h` 或同类头来读取 CPU flags。
10. 构建与本地验证：用启用 RVV 的工具链构建（示例：`./configure --arch=riscv64 --enable-cross-compile --cross-prefix=riscv64-linux-gnu- --enable-rvv`（以项目实际选项为准）并 `make -j`）。运行一次目标相关的单元/回归（至少 `make fate` 中相关子集）确保无编译与链接问题。
11. 运行 checkasm 验证：在可运行 RISC-V RVV 的环境（qemu-riscv64 + RVV 支持或真机）执行 `./ffmpeg -version` 确认启用 RVV；然后运行 `tests/checkasm/checkasm --list | rg sbrdsp` 确认用例存在，执行 `tests/checkasm/checkasm sbrdsp`（或 `make checkasm` / `make checkasm-riscv` 取决于构建系统）。若失败：对比 C 与 RVV 的输出差异，重点检查 odd mask 计算、尾部 vl、对齐/别名、以及 INT_MIN 取负行为。
12. 与参考实现对齐与性能回归：将 RVV 实现与 x86/NEON 的策略对照，确保语义一致（尤其是是否 in-place、是否对偶数位保持不变）。用 `checkasm` 的 benchmark 模式（若可用 `--bench`）或在调用路径上做 micro-bench，记录提升；若性能不佳，尝试调整 LMUL、减少 merge 次数（例如通过构造符号掩码直接 `vxor` 改符号位，若类型与语义允许）、减少 load/store 次数。
13. 提交前清理与文档：确保新增文件有正确的 license header（与周边文件一致），新增符号命名符合 FFmpeg 约定（如 `ff_neg_odd_64_rvv` 或 `neg_odd_64_rvv`），并在 commit message 中标注影响范围：`riscv/rvv: add sbrdsp neg_odd_64`。

## Reference Files

- libavcodec/sbrdsp_template.c
- libavcodec/x86/sbrdsp.asm
- libavcodec/x86/sbrdsp_init.c
- libavcodec/arm/sbrdsp_neon.S
- libavcodec/arm/sbrdsp_init_arm.c
- libavcodec/aarch64/sbrdsp_neon.S
- libavcodec/aarch64/sbrdsp_init_aarch64.c
- libavcodec/riscv/sbrdsp_init.c
- libavcodec/sbrdsp.h
- tests/checkasm/sbrdsp.c
- tests/checkasm/checkasm.c
- libavcodec/riscv/sbrdsp_rvv.S

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
  "build_attempts": 5,
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
- libavcodec/x86/sbrdsp.asm
- libavcodec/x86/celt_pvq_search.asm

### arm_refs

- libavcodec/arm/sbrdsp_init_arm.c
- libavcodec/arm/sbrdsp_neon.S

### aarch64_refs

- libavcodec/aarch64/sbrdsp_init_aarch64.c
- libavcodec/aarch64/sbrdsp_neon.S

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
- libavcodec/x86/sbrdsp.asm:233: cglobal sbr_neg_odd_64, 1,2,4,z
- libavcodec/aarch64/sbrdsp_init_aarch64.c:57: s->neg_odd_64 = ff_sbr_neg_odd_64_neon;
- libavcodec/aarch64/sbrdsp_neon.S:75: function ff_sbr_neg_odd_64_neon, export=1
- libavcodec/arm/sbrdsp_init_arm.c:60: s->neg_odd_64 = ff_sbr_neg_odd_64_neon;
- libavcodec/arm/sbrdsp_neon.S:59: function ff_sbr_neg_odd_64_neon, export=1
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
- libavcodec/x86/celt_pvq_search.asm:347: %if ARCH_X86_64 == 0    ; sbrdsp
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
  "datatype": "float32",
  "vectorizable": true,
  "pattern": [
    "tail"
  ],
  "has_stride": false,
  "has_saturation": false,
  "reduction": false,
  "tail_required": true,
  "math_expression": "for i in 0..63: if (i & 1) x[i] = -x[i]",
  "c_candidates": [
    "libavcodec/sbrdsp_template.c:ff_sbr_neg_odd_64_c (in template, exact line unknown from provided snippet)"
  ],
  "x86_refs": [
    "libavcodec/x86/sbrdsp.asm:ff_sbr_neg_odd_64_sse (line unknown from provided snippet)"
  ],
  "arm_refs": [
    "libavcodec/arm/sbrdsp_neon.S:ff_sbr_neg_odd_64_neon (line unknown from provided snippet)",
    "libavcodec/aarch64/sbrdsp_neon.S:ff_sbr_neg_odd_64_neon (line unknown from provided snippet)"
  ],
  "notes": "INTFLOAT resolves to float in this build context (x86/arm/riscv init prototypes all use float*). Operation is in-place sign flip of odd-indexed elements among 64 scalars; RVV can implement with vector load/store and XOR sign-bit mask on lanes corresponding to odd indices, or multiply by alternating {+1,-1} factors. Fixed trip count=64 but RVV still needs tail handling for arbitrary VL."
}
```

- llm_used: True

## Generation (raw)

```
{
  "files": [
    {
      "path": "libavcodec/riscv/sbrdsp.neg_odd_64_rvv.S",
      "content": ".text\n.option norvc\n\n# void ff_sbr_neg_odd_64_rvv(float *x)\n\n.globl ff_sbr_neg_odd_64_rvv\n.type  ff_sbr_neg_odd_64_rvv, @function\nff_sbr_neg_odd_64_rvv:\n    # a0 = x\n    li      t0, 64              # remaining elements\n    mv      t1, a0              # ptr\n\n    # Prepare scalar sign-bit mask (0x80000000) in a vector register\n    li      t4, 0x80000000\n    vmv.v.x v2, t4\n\n1:\n    beqz    t0, 2f\n    vsetvli t2, t0, e32, m1, ta, ma\n\n    # Load float vector\n    vle32.v v3, (t1)\n\n    # Create lane indices [0..vl-1]\n    vid.v   v1\n    # (idx & 1)\n    vand.vi v1, v1, 1\n\n    # mask for odd lanes\n    vmsne.vi v0, v1, 0\n\n    # flip sign bit on odd lanes: x ^= 0x80000000\n    vxor.vv v3, v3, v2, v0.t\n\n    vse32.v v3, (t1)\n\n    slli    t3, t2, 2           # bytes processed\n    add     t1, t1, t3\n    sub     t0, t0, t2\n    j       1b\n2:\n    ret\n.size ff_sbr_neg_odd_64_rvv, .-ff_sbr_neg_odd_64_rvv\n"
    }
  ],
  "patches": [
    {
      "path": "libavcodec/riscv/sbrdsp.neg_odd_64_rvv.S",
      "diff": "--- a/libavcodec/riscv/sbrdsp.neg_odd_64_rvv.S\n+++ b/libavcodec/riscv/sbrdsp.neg_odd_64_rvv.S\n@@ -1,46 +1,44 @@\n .text\n .option norvc\n \n # void ff_sbr_neg_odd_64_rvv(float *x)\n \n .globl ff_sbr_neg_odd_64_rvv\n .type  ff_sbr_neg_odd_64_rvv, @function\n ff_sbr_neg_odd_64_rvv:\n     # a0 = x\n     li      t0, 64              # remaining elements\n     mv      t1, a0              # ptr\n \n     # Prepare scalar sign-bit mask (0x80000000) in a vector register\n     li      t4, 0x80000000\n     vmv.v.x v2, t4\n \n 1:\n     beqz    t0, 2f\n     vsetvli t2, t0, e32, m1, ta, ma\n \n     # Load float vector\n-    vle32.v v0, (t1)\n+    vle32.v v3, (t1)\n \n     # Create lane indices [0..vl-1]\n     vid.v   v1\n     # (idx & 1)\n     vand.vi v1, v1, 1\n \n-    # mask for odd lanes: v0 = (v1 != 0)\n-    # (destination is the mask register v0; do not use v0.t here)\n-    vmsne.vi v0, v1, 0\n+    # mask for odd lanes\n+    vmsne.vi v0, v1, 0\n \n     # flip sign bit on odd lanes: x ^= 0x80000000\n-    vxor.vv v0, v0, v2, v0.t\n+    vxor.vv v3, v3, v2, v0.t\n \n-    vse32.v v0, (t1)\n+    vse32.v v3, (t1)\n \n     slli    t3, t2, 2           # bytes processed\n     add     t1, t1, t3\n     sub     t0, t0, t2\n     j       1b\n 2:\n     ret\n .size ff_sbr_neg_odd_64_rvv, .-ff_sbr_neg_odd_64_rvv\n"
    }
  ]
}
```

## Materialized

- runs/20260305_072541_sbrdsp.neg_odd_64/artifacts/files/libavcodec/riscv/sbrdsp.neg_odd_64_rvv.S
- /home/yuhe/project/cmdTool/workplace/FFmpeg/libavcodec/riscv/sbrdsp.neg_odd_64_rvv.S
- runs/20260305_072541_sbrdsp.neg_odd_64/artifacts/files/libavcodec/riscv/sbrdsp_init.c
- /home/yuhe/project/cmdTool/workplace/FFmpeg/libavcodec/riscv/sbrdsp_init.c
- runs/20260305_072541_sbrdsp.neg_odd_64/artifacts/files/libavcodec/riscv/Makefile
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
AS	libavcodec/riscv/sbrdsp.neg_odd_64_rvv.o
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
CC	libavcodec/targa.o
CC	libavcodec/takdsp.o
CC	libavcodec/targaenc.o
CC	libavcodec/targa_y216dec.o
CC	libavcodec/textdec.o
CC	libavcodec/texturedsp.o
CC	libavcodec/texturedspenc.o
CC	libavcodec/threadprogress.o
CC	libavcodec/tiertexseqv.o
CC	libavcodec/tiff_common.o
CC	libavcodec/tiffenc.o
CC	libavcodec/tiff.o
CC	libavcodec/tmv.o
CC	libavcodec/to_upper4.o
CC	libavcodec/tpeldsp.o
CC	libavcodec/truemotion1.o
CC	libavcodec/truemotion2.o
CC	libavcodec/truemotion2rt.o
CC	libavcodec/truespeech.o
src/libavcodec/riscv/sbrdsp.neg_odd_64_rvv.S: Assembler messages:
src/libavcodec/riscv/sbrdsp.neg_odd_64_rvv.S:23: Error: illegal operands `vmsne.vi v0.t,v1,0'
src/libavcodec/riscv/sbrdsp.neg_odd_64_rvv.S:26: Error: bad value for vector immediate field, value must be -16...15
src/libavcodec/riscv/sbrdsp.neg_odd_64_rvv.S:45: Error: symbol `ff_sbr_neg_odd_64_rvv' is already defined
src/libavcodec/riscv/sbrdsp.neg_odd_64_rvv.S:61: Error: illegal operands `vmsne.vi v0.t,v1,0'
src/libavcodec/riscv/sbrdsp.neg_odd_64_rvv.S:86: Error: symbol `ff_sbr_neg_odd_64_rvv' is already defined
src/libavcodec/riscv/sbrdsp.neg_odd_64_rvv.S:131: Error: symbol `ff_sbr_neg_odd_64_rvv' is already defined
make: *** [src/ffbuild/common.mak:102: libavcodec/riscv/sbrdsp.neg_odd_64_rvv.o] Error 1
make: *** Waiting for unfinished jobs....
CC	libavcodec/tscc2.o
CC	libavcodec/ttadata.o
CC	libavcodec/tta.o
```
