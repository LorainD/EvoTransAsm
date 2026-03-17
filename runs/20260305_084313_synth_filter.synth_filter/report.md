# rvv-agent run report

## Symbol

- synth_filter.synth_filter

## Plan

01. 在 FFmpeg 源码中定位 synth_filter.synth_filter 的 C 实现：使用 ripgrep 在 libavcodec/ 下搜索符号与其赋值（例如 `rg -n "synth_filter\.(synth_filter|init)" libavcodec`、`rg -n "synth_filter" libavcodec -S`），找到对应的 DSP/函数指针初始化位置（通常在 *dsp.c、*dsp_template.c 或具体 codec 目录的 init 函数中），记录：函数签名、调用方、输入输出缓冲区形状（样本数/stride/对齐）、以及使用到的常量表（window/tab 等）。
02. 确定该符号对应的真实 C 函数名与所在文件：从 init 中的 `c->synth_filter = xxx;` 追到 `xxx` 的定义（例如 `static void xxx(...)`），把该 C 版本作为 RVV 语义基准；同时确认是否存在多个变体（float/fixed、s16/s32、stereo/mono）。
03. 定位参考 SIMD 实现（x86/ARM）：在 libavcodec/x86/、libavcodec/arm/、libavcodec/aarch64/ 下搜索同名或同用途的实现与 init 绑定（例如 `rg -n "synth_filter" libavcodec/x86 libavcodec/arm libavcodec/aarch64`；再检查对应 init 文件如 `*_dsp_init.c`、`*_dsp_init.S`、`*_init.c`）。若没有完全同名符号，改为搜索 C 实现函数名 `xxx` 以及其在 init 里赋值的字段名 `synth_filter`，并记录：SIMD 版本支持的 CPU 特性（SSE2/AVX2/NEON 等）、处理的向量宽度/展开方式、以及边界处理（尾部 samples）。
04. 建立 RVV 迁移策略（针对 synth_filter 的计算结构）：基于 C 实现梳理核心计算模式（常见为：对固定长度子带/窗口做 MAC、加偏移、右移/舍入、clip 到 int16/float，可能包含反交织/重排）；把其拆成 RVV 友好的块：1) 向量化 MAC（vwmacc/vwmaccu 或 vfmacc 视数据类型），2) 舍入与移位（vnclip/vnclipu 或 vssra），3) 打包/交织（vslide/vrgather/zip 类操作或通过标量处理少量 lane），4) 尾部处理（vsetvl 动态 VL）。输出一个“逐行对应”的映射表：C 循环变量 -> RVV 向量循环变量，C 的常量表访问方式 -> RVV 加载方式（unit-stride/strided/gather）。
05. 实现 RVV 版本源文件：在 libavcodec/riscv/ 新增 `synth_filter_rvv.c`（或更贴近现有目录命名规则的文件名），包含 `#include "libavutil/riscv/rvv.h"`/`#include <riscv_vector.h>`（按 FFmpeg 现有 RVV 封装选择），实现函数例如 `ff_synth_filter_rvv(...)`，保证：a) 函数签名与 C 版本完全一致；b) 仅做等价变换不改算法；c) 使用 `size_t vl = __riscv_vsetvl_e16m1(n)`/对应元素宽度动态处理样本；d) 对可能的非对齐 load 使用允许的 unaligned 方式或先对齐处理；e) 尾部通过 vsetvl 收缩处理，禁止越界读写。
06. 针对 synth_filter 的关键细节做 RVV 实现对齐：1) 舍入方式（加偏置后算术右移 vs round-to-nearest）必须与 C/SSE/NEON 保持一致；2) 饱和/clip 行为（例如 int16 饱和）使用 RVV 的 narrowing + saturation 指令或显式 min/max；3) 若 C 里存在双通道交织写（LRLR...），确保 RVV 写回顺序与 stride 匹配；4) 若使用查表 window，确保 RVV 载入与标量版本完全相同的索引序（必要时用 vrgather 或分块顺序加载）。
07. 把 RVV 实现接入该 codec/dsp 的 init：在对应的 riscv init 文件中添加初始化（常见为 `libavcodec/riscv/<codec>_dsp_init.c` 或统一的 `libavcodec/riscv/dsp_init.c`）。步骤：a) 新增 `#include "synth_filter_rvv.h"` 或声明 `void ff_synth_filter_rvv(...);`；b) 在 `ff_<codec>_dsp_init_riscv(<ctx> *c)` 中基于 CPU flags 绑定 `c->synth_filter = ff_synth_filter_rvv;`；c) 使用 `av_get_cpu_flags()` + `AV_CPU_FLAG_RVV`（或 FFmpeg 当前 riscv feature 宏）做门控，避免在无 RVV 的核上绑定。
08. 集成到构建系统：编辑 `libavcodec/riscv/Makefile`（或 `libavcodec/riscv/Makefile.inc`，以仓库实际为准）把 `synth_filter_rvv.o` 加入相应对象列表（通常是 `OBJS-$(CONFIG_<codec>_DECODER)` 或 `RVV-OBJS` 之类）；若需要头文件，确保加入正确 include 路径；同时检查 `configure` 是否需要开启 RVV（例如 `--enable-rvv` 或由 `--arch=riscv64 --cpu=...` 自动检测），保证该对象只在 riscv 构建中编译。
09. 为 checkasm 添加/扩展专用测试：在 `tests/checkasm/` 中搜索是否已有 synth_filter 测试（`rg -n "synth_filter" tests/checkasm`）。若已有：在现有测试里把新函数加入对比（通过 checkfunc 宏对比 C vs RVV），并确保随机输入覆盖：不同 block 长度、不同对齐、不同边界（最小/最大样本、全零、全满幅）；若没有：新建 `tests/checkasm/synth_filter.c`，实现 `check_synth_filter()`：1) 通过对应的 dsp init 获取函数指针；2) 强制调用 C 参考与 RVV 版本分别输出到不同缓冲；3) 使用 `memcmp`/逐样本比较（对 float 允许 ULP/epsilon）；4) 加入 `bench_new` 以获得性能数据。
10. 运行 checkasm 验证（riscv64 + RVV）：使用交叉编译或原生 riscv64 环境配置：`./configure --arch=riscv64 --enable-cross-compile --cross-prefix=riscv64-linux-gnu- --target-os=linux --enable-gpl --enable-version3 --enable-nonfree`（按你的环境调整）并确保启用 RVV；编译后运行：`make -j && make checkasm` 或 `tests/checkasm/checkasm --report`，重点观察：1) synth_filter 用例是否被执行；2) C vs RVV 是否全通过；3) 若失败，按 seed 复现（checkasm 输出通常包含 seed），定位到具体 case，回到 RVV 代码修正舍入/饱和/越界。
11. 回归与性能验证：在通过 checkasm 后，运行相关 FATE/解码回归（若该 synth_filter 属于特定 codec，如 MP3/AC3 等，跑对应 fate 集），并用 `bench_new`/实际解码基准对比 C vs RVV 的耗时；若性能不达预期，优先优化：减少 vrgather、合并 load/store、提升向量化粒度、避免频繁 vsetvl（按固定块大小循环）。

## Reference Files

- libavcodec/synth_filter.c
- libavcodec/x86/synth_filter.asm
- libavcodec/x86/synth_filter_init.c
- libavcodec/arm/synth_filter_neon.S
- libavcodec/arm/synth_filter_vfp.S
- libavcodec/arm/synth_filter_init_arm.c
- libavcodec/dcadsp.h
- libavcodec/dca_core.h
- tests/checkasm/synth_filter.c
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
  "build_attempts": 5,
  "scp_ok": false,
  "run_on_board_ok": false,
  "board_enabled": false
}
```

## Discovery

### c_candidates

- libavcodec/synth_filter.c
- tests/checkasm/checkasm.c
- tests/checkasm/synth_filter.c

### x86_refs

- libavcodec/x86/synth_filter.asm
- libavcodec/x86/synth_filter_init.c

### arm_refs

- libavcodec/arm/synth_filter_neon.S
- libavcodec/arm/synth_filter_init_arm.c
- libavcodec/arm/synth_filter_vfp.S

### aarch64_refs

- libavcodec/aarch64/synth_filter_neon.S
- libavcodec/aarch64/synth_filter_init.c

### riscv_refs

- (none)

### headers

- libavcodec/dca_core.h
- libavcodec/dcadsp.h

### other

- (none)

## Matches (first 200)

- libavcodec/dca_core.h:37: #include "synth_filter.h"
- libavcodec/dcadsp.h:28: #include "synth_filter.h"
- libavcodec/synth_filter.c:24: #include "synth_filter.h"
- libavcodec/x86/synth_filter.asm:110: ; void ff_synth_filter_inner_<opt>(float *synth_buf, float synth_buf2[32],
- libavcodec/x86/synth_filter.asm:114: cglobal synth_filter_inner, 0, 6 + 4 * ARCH_X86_64, 7 + 6 * ARCH_X86_64, \
- libavcodec/x86/synth_filter_init.c:24: #include "libavcodec/synth_filter.h"
- libavcodec/aarch64/synth_filter_neon.S:43: function ff_synth_filter_float_neon, export=1
- libavcodec/aarch64/synth_filter_init.c:26: #include "libavcodec/synth_filter.h"
- libavcodec/arm/synth_filter_neon.S:23: function ff_synth_filter_float_neon, export=1
- libavcodec/arm/synth_filter_init_arm.c:26: #include "libavcodec/synth_filter.h"
- libavcodec/arm/synth_filter_vfp.S:118: /* void ff_synth_filter_float_vfp(FFTContext *imdct,
- libavcodec/arm/synth_filter_vfp.S:123: function ff_synth_filter_float_vfp, export=1
- tests/checkasm/checkasm.c:149: { "synth_filter", checkasm_check_synth_filter },
- tests/checkasm/synth_filter.c:32: #include "libavcodec/synth_filter.h"
- tests/checkasm/synth_filter.c:125: report("synth_filter");

## Analysis JSON

```json
{
  "symbol": "synth_filter.synth_filter",
  "datatype": "float32",
  "vectorizable": true,
  "pattern": [
    "butterfly",
    "stride_load",
    "tail"
  ],
  "has_stride": true,
  "has_saturation": false,
  "reduction": false,
  "tail_required": false,
  "math_expression": "imdct_fn(imdct, synth_buf, in, sizeof(float)); for i in 0..15: a=synth_buf2[i]; b=synth_buf2[i+16]; c=0; /* then use synth_buf (ring buffer) and window[512] to compute out[0..31] via pairwise add/sub (butterfly) and windowed MACs; update synth_buf2 */",
  "c_candidates": [
    "libavcodec/synth_filter.c:static void synth_filter_float(AVTXContext *imdct, float *synth_buf_ptr, int *synth_buf_offset, float synth_buf2[32], const float window[512], float out[32], float in[32], float scale, av_tx_fn imdct_fn)"
  ],
  "x86_refs": [
    "libavcodec/x86/synth_filter.asm:SECTION .text"
  ],
  "arm_refs": [
    "libavcodec/arm/synth_filter_neon.S:function ff_synth_filter_float_neon, export=1",
    "libavcodec/arm/synth_filter_vfp.S:IMDCT         .req    r0"
  ],
  "notes": "核心是DCA synth filter的32点输出生成：先IMDCT写入synth_buf（环形缓冲，offset按块更新/回绕），再对synth_buf2上下半区做butterfly（a/b）并结合window[512]做窗口加权的点积/累加生成out[32]，同时更新synth_buf2。可用RVV对windowed MAC与butterfly分段向量化；存在环形缓冲导致非连续/跨界访问（has_stride=true），但长度固定32/16，通常不需要尾处理。无饱和、无归约到单标量。x86/arm均有现成SIMD汇编实现可参考其数据重排与环绕处理。"
}
```

- llm_used: True

## Generation (raw)

```
{
  "files": [
    {
      "path": "libavcodec/riscv/synth_filter_init.c",
      "content": "/*\n * Copyright (c) 2026\n *\n * This file is part of FFmpeg.\n *\n * FFmpeg is free software; you can redistribute it and/or\n * modify it under the terms of the GNU Lesser General Public\n * License as published by the Free Software Foundation; either\n * version 2.1 of the License, or (at your option) any later version.\n *\n * FFmpeg is distributed in the hope that it will be useful,\n * but WITHOUT ANY WARRANTY; without even the implied warranty of\n * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU\n * Lesser General Public License for more details.\n *\n * You should have received a copy of the GNU Lesser General Public\n * License along with FFmpeg; if not, write to the Free Software\n * Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA\n */\n\n#include \"config.h\"\n\n#include \"libavutil/attributes.h\"\n#include \"libavutil/cpu.h\"\n#include \"libavutil/riscv/cpu.h\"\n\n#include \"libavcodec/synth_filter.h\"\n\nvoid ff_synth_filter_float_rvv(AVTXContext *imdct,\n                               float *synth_buf_ptr, int *synth_buf_offset,\n                               float synth_buf2[32], const float window[512],\n                               float out[32], float in[32], float scale,\n                               av_tx_fn imdct_fn);\n\n/*\n * Determine RVV availability.\n *\n * Use the public CPU flag when available in this FFmpeg revision.\n * Avoid calling internal helpers that may not exist across versions.\n */\nstatic av_always_inline int ff_rvv_available(void)\n{\n#if HAVE_RVV\n    return av_get_cpu_flags() & AV_CPU_FLAG_RVV;\n#else\n    return 0;\n#endif\n}\n\nav_cold void ff_synth_filter_init_riscv(SynthFilterContext *s);\n\nav_cold void ff_synth_filter_init_riscv(SynthFilterContext *s)\n{\n#if HAVE_RVV\n    if (ff_rvv_available())\n        s->synth_filter_float = ff_synth_filter_float_rvv;\n#else\n    (void)s;\n#endif\n}\n"
    }
  ],
  "patches": [
    {
      "path": "libavcodec/riscv/synth_filter_init.c",
      "diff": "--- a/libavcodec/riscv/synth_filter_init.c\n+++ b/libavcodec/riscv/synth_filter_init.c\n@@ -1,75 +1,71 @@\n /*\n  * Copyright (c) 2026\n  *\n  * This file is part of FFmpeg.\n  *\n  * FFmpeg is free software; you can redistribute it and/or\n  * modify it under the terms of the GNU Lesser General Public\n  * License as published by the Free Software Foundation; either\n  * version 2.1 of the License, or (at your option) any later version.\n  *\n  * FFmpeg is distributed in the hope that it will be useful,\n  * but WITHOUT ANY WARRANTY; without even the implied warranty of\n  * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU\n  * Lesser General Public License for more details.\n  *\n  * You should have received a copy of the GNU Lesser General Public\n  * License along with FFmpeg; if not, write to the Free Software\n  * Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA\n  */\n \n #include \"config.h\"\n \n #include \"libavutil/attributes.h\"\n #include \"libavutil/cpu.h\"\n #include \"libavutil/riscv/cpu.h\"\n \n #include \"libavcodec/synth_filter.h\"\n \n void ff_synth_filter_float_rvv(AVTXContext *imdct,\n                                float *synth_buf_ptr, int *synth_buf_offset,\n                                float synth_buf2[32], const float window[512],\n                                float out[32], float in[32], float scale,\n                                av_tx_fn imdct_fn);\n \n /*\n  * Determine RVV availability.\n  *\n- * Do not reference AV_CPU_FLAG_RVV directly: depending on the FFmpeg version\n- * and configuration, the public flag may not exist (leading to compilation\n- * errors). The canonical helper in libavutil/riscv/cpu.h is the preferred API.\n+ * Use the public CPU flag when available in this FFmpeg revision.\n+ * Avoid calling internal helpers that may not exist across versions.\n  */\n static av_always_inline int ff_rvv_available(void)\n {\n #if HAVE_RVV\n-    /* libavutil/riscv/cpu.h provides this helper when RVV support is enabled */\n-    return ff_rvv_have_rvv();\n+    return av_get_cpu_flags() & AV_CPU_FLAG_RVV;\n #else\n     return 0;\n #endif\n }\n \n av_cold void ff_synth_filter_init_riscv(SynthFilterContext *s);\n \n av_cold void ff_synth_filter_init_riscv(SynthFilterContext *s)\n {\n #if HAVE_RVV\n     if (ff_rvv_available())\n         s->synth_filter_float = ff_synth_filter_float_rvv;\n #else\n     (void)s;\n #endif\n }\n"
    }
  ]
}
```

## Materialized

- runs/20260305_084313_synth_filter.synth_filter/artifacts/files/libavcodec/riscv/synth_filter.synth_filter_rvv.S
- /home/yuhe/project/cmdTool/workplace/FFmpeg/libavcodec/riscv/synth_filter.synth_filter_rvv.S
- runs/20260305_084313_synth_filter.synth_filter/artifacts/files/libavcodec/riscv/synth_filter_init.c
- /home/yuhe/project/cmdTool/workplace/FFmpeg/libavcodec/riscv/synth_filter_init.c
- runs/20260305_084313_synth_filter.synth_filter/artifacts/files/libavcodec/riscv/Makefile
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
CC	libavcodec/riscv/synth_filter_init.o
src/libavcodec/riscv/synth_filter_init.c: In function 'ff_rvv_available':
src/libavcodec/riscv/synth_filter_init.c:44:33: error: 'AV_CPU_FLAG_RVV' undeclared (first use in this function); did you mean 'AV_CPU_FLAG_RVB'?
   44 |     return av_get_cpu_flags() & AV_CPU_FLAG_RVV;
      |                                 ^~~~~~~~~~~~~~~
      |                                 AV_CPU_FLAG_RVB
src/libavcodec/riscv/synth_filter_init.c:44:33: note: each undeclared identifier is reported only once for each function it appears in
make: *** [src/ffbuild/common.mak:90: libavcodec/riscv/synth_filter_init.o] Error 1
```
