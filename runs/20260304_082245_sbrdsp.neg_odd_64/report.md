# rvv-agent run report

## Symbol

- sbrdsp.neg_odd_64

## Plan

01. 在源码中定位符号注册与调用点：使用 ripgrep 搜索 `neg_odd_64`、`sbrdsp`、`SBRDSPContext`、`ff_sbrdsp_init`，确认该函数指针的字段名、签名(参数类型/对齐/stride)以及在哪些 SBR 路径触发调用；记录对应的 C 文件路径与初始化逻辑分支(如按 bitdepth/cpu_flags 选择)。
02. 定位并阅读 C 标量实现：在 `libavcodec/` 下继续搜索 `neg_odd_64` 的具体函数体或宏展开来源；若是 static inline/宏生成，向上追溯到定义处，整理精确定义的语义(对 64 个样本/系数的处理方式、'odd' 的含义：奇数索引/奇数子带/奇相位、是否原地写回、饱和/截断规则、是否仅取反或带偏置)。输出一份“逐元素公式 + 访存模式 + 边界条件”的语义说明，作为 RVV 正确性基准。
03. 定位参考 SIMD 实现：分别在 `libavcodec/x86/`、`libavcodec/arm/`、`libavcodec/aarch64/` 搜索 `neg_odd_64` 或相近命名(例如 `*_neg_odd_*`、`sbrdsp_*`)，并沿 `ff_sbrdsp_init_x86`/`ff_sbrdsp_init_arm`/`ff_sbrdsp_init_aarch64` 查找被赋值的实现；若找不到同名函数，则定位实现该语义的等价内核(可能合并在更大函数中或由宏生成)，提取其向量化策略(成对加载/步长访问/奇偶掩码/交织解交织方式)。
04. 确定数据类型与向量化颗粒：基于 C 实现确认元素类型(常见为 `int32_t`/`int16_t`/`float` 之一)与长度恒为 64；据此选择 RVV 元素宽度(SEW)与 LMUL，并决定一次处理多少元素(例如每次 `vl` 取 `vsetvl_e32m1` 循环直到覆盖 64)；若 'odd' 需要对奇数索引操作，明确用 index 递增步长(2)还是用掩码对奇位 lane 生效，并比较两种方案在 RVV 上的复杂度。
05. 设计 RVV 访存方案以匹配“odd”语义：如果操作对象是连续数组但仅处理奇数下标，优先评估 `vlse`/`vsse`(stride=2*elem_size) 的步长加载/存储；如果需要在原数组中仅修改奇位并保留偶位，则评估“读-改-写 + 掩码 store”(mask store)或“先 load 全量再用 mask blend”；若数据在内存中本就以奇偶分离(两个指针或 planar)，则使用连续 `vle`/`vse`。
06. 生成 RVV intrinsic 实现文件：在 `libavcodec/riscv/` 新增或扩展 `sbrdsp_rvv.c`(或与现有命名风格一致的文件)，实现 `ff_neg_odd_64_rvv(...)`(函数名按 FFmpeg riscv 习惯，如 `ff_sbrdsp_neg_odd_64_rvv`)，严格复刻 C 语义；核心循环结构为：`for (i=0; i<64; i+=vl)` + `vsetvl` + load(连续/步长) + negate(例如 `vneg_v_i32m?` 或 `vsub(vzero, v)`；若有饱和/截断则用相应 widening/narrowing 指令) + store；若需要“只作用于奇位”，实现对应 mask 生成(例如利用 `vid` 生成 index，`vid % 2` 形成 mask)或用步长 load/store。
07. 处理对齐/别名/性能细节：若 C 实现暗含对齐(例如 16/32 字节)或 restrict 语义，在 RVV 版本中保持相同的指针限定与 `__builtin_assume_aligned`(若工程风格允许)；避免未定义行为(例如对 signed overflow 的依赖)，必要时使用无符号运算或显式饱和路径与 C 保持一致。
08. 在 riscv 初始化中挂接：在 `libavcodec/riscv/sbrdsp_init_riscv.c`(或实际存在的 riscv init 文件)中为 `SBRDSPContext` 对应字段赋值：当 `av_get_cpu_flags()` 命中 RVV(通常 `AV_CPU_FLAG_RVV`)时，将 `c->neg_odd_64 = ff_*_rvv`；并确保有回退到 C 的默认实现。
09. 集成到构建系统：将新增的 RVV 源文件加入 `libavcodec/riscv/Makefile`(或 `ffbuild/common.mak` 相关片段)的对象列表；必要时添加/更新 `OBJS-$(CONFIG_SBRDSP)`、`RISCVV-OBJS` 等条件；确保仅在 `HAVE_RVV`/`CONFIG_RISCV` 条件下编译，并与现有 riscv 文件的编译开关一致(例如 `-march=rv64gcv` 由 configure 生成)。
10. 补充/更新头文件与可见性：如需要在 init 文件引用 RVV 函数声明，按 FFmpeg 习惯在对应 `*_init_riscv.c` 顶部添加 `void ff_*_rvv(...)` 原型或在 `libavcodec/riscv/sbrdsp_rvv.h` 声明；保持函数为 `static` 仅限本 TU 或按需要导出 `ff_` 前缀。
11. 运行并通过 checkasm：在支持 RVV 的交叉/本机环境下编译开启 checkasm(例如 `--enable-checkasm --enable-riscv-rvv`/默认由 configure 探测)；执行 `tests/checkasm/checkasm --list | grep sbrdsp` 确认存在对应测试项；运行 `tests/checkasm/checkasm sbrdsp` 或全量 checkasm，观察 `neg_odd_64` 的 C vs RVV 比对结果；若无现成测试覆盖 `neg_odd_64`，在 `tests/checkasm/sbrdsp.c` 增加专门用例：随机填充输入、调用 C 与 RVV 两路径、比对输出与未触碰区域(偶位/其它缓冲)保持一致，并将其纳入 `checkasm_check_sbrdsp()`。
12. 回归与性能验证：在通过 checkasm 后，运行与 SBR 相关的 FATE 子集(如 `fate-aac*`/实际依赖 SBR 的用例)确保端到端一致；使用 `checkasm` 的 benchmark 模式或 `perf` 采样比较 RVV vs C 的速度，必要时迭代优化(选择 stride load vs mask、调整 LMUL、减少 `vsetvl` 次数、展开固定 64 长度循环)。

## Reference Files

- libavcodec/sbrdsp.c
- libavcodec/sbrdsp_template.c
- libavcodec/sbrdsp_fixed.c
- libavcodec/aacsbr_template.c
- libavcodec/aacsbr_fixed.c
- libavcodec/x86/sbrdsp_init.c
- libavcodec/arm/sbrdsp_init_arm.c
- libavcodec/aarch64/sbrdsp_init_aarch64.c
- libavcodec/riscv/sbrdsp_init.c
- libavcodec/sbrdsp.h
- libavcodec/sbr.h
- libavcodec/riscv/Makefile
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
  "pattern": [],
  "has_stride": false,
  "has_saturation": false,
  "reduction": false,
  "tail_required": false,
  "math_expression": "unknown",
  "c_candidates": [
    "libavcodec/aacsbr_template.c:1438",
    "libavcodec/sbrdsp_template.c:84",
    "libavcodec/x86/sbrdsp_init.c:63",
    "libavcodec/aarch64/sbrdsp_init_aarch64.c:57",
    "libavcodec/arm/sbrdsp_init_arm.c:60",
    "tests/checkasm/sbrdsp.c:271",
    "tests/checkasm/sbrdsp.c:273",
    "libavcodec/aacsbr_fixed.c:64",
    "libavcodec/sbrdsp.c:28",
    "libavcodec/sbrdsp_fixed.c:31",
    "libavcodec/aacsbr_template.c:1366",
    "libavcodec/aacsbr_template.c:1378",
    "libavcodec/aacsbr_template.c:1379",
    "libavcodec/aacsbr_template.c:1396",
    "libavcodec/aacsbr_template.c:1409",
    "libavcodec/aacsbr_template.c:1411",
    "libavcodec/aacsbr_template.c:1436",
    "libavcodec/aacsbr_template.c:1441",
    "libavcodec/aacsbr.c:35",
    "libavcodec/riscv/sbrdsp_init.c:25"
  ],
  "x86_refs": [
    "libavcodec/x86/sbrdsp_init.c"
  ],
  "arm_refs": [
    "libavcodec/arm/sbrdsp_init_arm.c"
  ],
  "notes": "LLM 未运行或解析失败，使用 fallback。"
}
```

- llm_used: False

- error: HTTP 500 from https://chatbox.isrc.ac.cn/api/chat/completions: {"error":{"message":"分组 mini 下模型 gpt-5.2 的可用渠道不存在（retry） (request id: 202603040830206703724702pVZk3H3)","type":"new_api_error","param":"","code":"get_channel_failed"}}

## Generation (raw)

```
HTTP 503 from https://chatbox.isrc.ac.cn/api/chat/completions: {"error":{"code":"model_not_found","message":"No available channel for model gpt-5.2 under group mini (distributor) (request id: 20260304083050451890736PjtmbU7B)","type":"new_api_error"}}
```

## Materialized

- runs/20260304_082245_sbrdsp.neg_odd_64/artifacts/files/libavcodec/riscv/sbrdsp.neg_odd_64_rvv.S
- /home/yuhe/project/cmdTool/workplace/FFmpeg/libavcodec/riscv/sbrdsp.neg_odd_64_rvv.S

## configure

```
$ /home/yuhe/project/cmdTool/workplace/FFmpeg/configure --cross-prefix=riscv64-unknown-linux-gnu- --arch=riscv64 --target-os=linux --enable-cross-compile --cpu=rv64gcv '--extra-cflags=-march=rv64gcv -mabi=lp64d -O3' --extra-ldflags=-static --disable-shared --enable-static
(rc=0)
```

### stdout

```
install prefix            /usr/local
source path               /home/yuhe/project/cmdTool/workplace/FFmpeg
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
g729                    nc                      webm
```

## make checkasm

```
$ make -j128 tests/checkasm/checkasm
(rc=0)
```

### stdout

```
GEN	libavutil/libavutil.version
GEN	libavcodec/libavcodec.version
GEN	libavformat/libavformat.version
GEN	libavfilter/libavfilter.version
GEN	libavdevice/libavdevice.version
GEN	libswresample/libswresample.version
GEN	libswscale/libswscale.version
CC	libavdevice/alldevices.o
CC	libavdevice/fbdev_common.o
CC	libavdevice/avdevice.o
CC	libavdevice/fbdev_dec.o
CC	libavdevice/fbdev_enc.o
CC	libavdevice/lavfi.o
CC	libavdevice/oss.o
CC	libavdevice/oss_dec.o
CC	libavdevice/oss_enc.o
CC	libavdevice/timefilter.o
CC	libavdevice/utils.o
CC	libavdevice/v4l2-common.o
CC	libavdevice/v4l2.o
CC	libavdevice/version.o
CC	libavdevice/v4l2enc.o
CC	libavformat/3dostr.o
CC	libavformat/a64.o
CC	libavformat/4xm.o
CC	libavformat/aadec.o
CC	libavformat/aacdec.o
CC	libavformat/aaxdec.o
CC	libavformat/ac3dec.o
CC	libavformat/ac4dec.o
CC	libavformat/ac4enc.o
CC	libavformat/acedec.o
CC	libavformat/acm.o
CC	libavformat/act.o
CC	libavformat/adp.o
CC	libavformat/ads.o
CC	libavformat/adtsenc.o
CC	libavformat/adxdec.o
CC	libavformat/aeadec.o
CC	libavformat/aeaenc.o
CC	libavformat/afc.o
CC	libavformat/aiff.o
CC	libavformat/aiffdec.o
CC	libavformat/aixdec.o
CC	libavformat/aiffenc.o
CC	libavformat/allformats.o
CC	libavformat/alp.o
CC	libavformat/amr.o
CC	libavformat/amvenc.o
CC	libavformat/anm.o
CC	libavformat/apac.o
CC	libavformat/apc.o
CC	libavformat/ape.o
CC	libavformat/apetag.o
CC	libavformat/apm.o
CC	libavformat/apngdec.o
CC	libavformat/apngenc.o
CC	libavformat/aptxdec.o
CC	libavformat/apv.o
CC	libavformat/apvdec.o
CC	libavformat/apvenc.o
CC	libavformat/aqtitledec.o
CC	libavformat/argo_asf.o
CC	libavformat/argo_brp.o
CC	libavformat/argo_cvg.o
CC	libavformat/asf.o
CC	libavformat/asf_tags.o
CC	libavformat/asfcrypt.o
CC	libavformat/asfdec_f.o
CC	libavformat/asfdec_o.o
CC	libavformat/asfenc.o
CC	libavformat/assdec.o
CC	libavformat/assenc.o
CC	libavformat/ast.o
CC	libavformat/astdec.o
CC	libavformat/astenc.o
CC	libavformat/async.o
CC	libavformat/au.o
CC	libavformat/av1.o
CC	libavformat/av1dec.o
CC	libavformat/avc.o
CC	libavformat/avformat.o
CC	libavformat/avidec.o
CC	libavformat/avienc.o
CC	libavformat/avio.o
CC	libavformat/aviobuf.o
CC	libavformat/avlanguage.o
CC	libavformat/avr.o
CC	libavformat/avs.o
CC	libavformat/avs2dec.o
CC	libavformat/avs3dec.o
CC	libavformat/bethsoftvid.o
CC	libavformat/bfi.o
CC	libavformat/bink.o
CC	libavformat/binka.o
CC	libavformat/bintext.o
CC	libavformat/bit.o
CC	libavformat/bmv.o
CC	libavformat/boadec.o
CC	libavformat/bonk.o
CC	libavformat/brstm.o
CC	libavformat/c93.o
CC	libavformat/cache.o
CC	libavformat/caf.o
CC	libavformat/cafdec.o
CC	libavformat/cafenc.o
CC	libavformat/cavsvideodec.o
CC	libavformat/cbs.o
CC	libavformat/cbs_apv.o
CC	libavformat/cbs_av1.o
CC	libavformat/cdg.o
CC	libavformat/cdxl.o
CC	libavformat/cinedec.o
CC	libavformat/codec2.o
CC	libavformat/codecstring.o
CC	libavformat/concat.o
CC	libavformat/concatdec.o
CC	libavformat/crcenc.o
CC	libavformat/crypto.o
CC	libavformat/dash.o
CC	libavformat/dashenc.o
CC	libavformat/data_uri.o
CC	libavformat/dauddec.o
CC	libavformat/daudenc.o
CC	libavformat/dcstr.o
CC	libavformat/demux.o
CC	libavformat/demux_utils.o
CC	libavformat/derf.o
CC	libavformat/dfa.o
CC	libavformat/dfpwmdec.o
CC	libavformat/dhav.o
CC	libavformat/diracdec.o
CC	libavformat/dnxhddec.o
CC	libavformat/dovi_isom.o
CC	libavformat/dsfdec.o
CC	libavformat/dsicin.o
CC	libavformat/dss.o
CC	libavformat/dtsdec.o
CC	libavformat/dtshddec.o
CC	libavformat/dump.o
CC	libavformat/dv.o
CC	libavformat/dvbsub.o
CC	libavformat/dvdclut.o
CC	libavformat/dvenc.o
CC	libavformat/dvbtxt.o
CC	libavformat/dxa.o
CC	libavformat/eacdata.o
CC	libavformat/electronicarts.o
CC	libavformat/epafdec.o
CC	libavformat/evc.o
CC	libavformat/evcdec.o
CC	libavformat/ffmetadec.o
CC	libavformat/ffmetaenc.o
CC	libavformat/fifo.o
CC	libavformat/file.o
CC	libavformat/filmstripdec.o
CC	libavformat/fitsdec.o
CC	libavformat/fitsenc.o
CC	libavformat/filmstripenc.o
CC	libavformat/flac_picture.o
CC	libavformat/flacdec.o
CC	libavformat/flacenc_header.o
CC	libavformat/flic.o
CC	libavformat/flacenc.o
CC	libavformat/flvdec.o
CC	libavformat/flvenc.o
CC	libavformat/format.o
CC	libavformat/framecrcenc.o
CC	libavformat/framehash.o
CC	libavformat/frmdec.o
CC	libavformat/fsb.o
CC	libavformat/ftp.o
CC	libavformat/fwse.o
CC	libavformat/g722.o
CC	libavformat/g723_1.o
CC	libavformat/g726.o
CC	libavformat/g728dec.o
CC	libavformat/gdv.o
CC	libavformat/genh.o
CC	libavformat/g729dec.o
CC	libavformat/gif.o
CC	libavformat/gifdec.o
CC	libavformat/gopher.o
CC	libavformat/gsmdec.o
CC	libavformat/gxf.o
CC	libavformat/gxfenc.o
CC	libavformat/h261dec.o
CC	libavformat/h263dec.o
CC	libavformat/h264dec.o
CC	libavformat/hashenc.o
CC	libavformat/hca.o
CC	libavformat/hcom.o
CC	libavformat/hdsenc.o
CC	libavformat/hevc.o
CC	libavformat/hevcdec.o
CC	libavformat/hls.o
CC	libavformat/hls_sample_encryption.o
CC	libavformat/hlsenc.o
CC	libavformat/hlsplaylist.o
CC	libavformat/hlsproto.o
CC	libavformat/hnm.o
CC	libavformat/http.o
CC	libavformat/httpauth.o
CC	libavformat/hxvs.o
CC	libavformat/iamf_parse.o
CC	libavformat/iamf.o
CC	libavformat/iamf_reader.o
CC	libavformat/iamf_writer.o
CC	libavformat/iamfdec.o
CC	libavformat/iamfenc.o
CC	libavformat/icecast.o
CC	libavformat/icodec.o
CC	libavformat/icoenc.o
CC	libavformat/id3v1.o
CC	libavformat/id3v2.o
CC	libavformat/id3v2enc.o
CC	libavformat/idcin.o
CC	libavformat/idroqdec.o
CC	libavformat/idroqenc.o
CC	libavformat/iff.o
CC	libavformat/ifv.o
CC	libavformat/img2.o
CC	libavformat/img2_alias_pix.o
CC	libavformat/ilbc.o
CC	libavformat/img2dec.o
CC	libavformat/img2_brender_pix.o
CC	libavformat/img2enc.o
CC	libavformat/imx.o
CC	libavformat/ingenientdec.o
CC	libavformat/ip.o
CC	libavformat/ipmovie.o
CC	libavformat/ipudec.o
CC	libavformat/ircam.o
CC	libavformat/ircamdec.o
CC	libavformat/ircamenc.o
CC	libavformat/isom.o
CC	libavformat/isom_tags.o
CC	libavformat/iv8.o
CC	libavformat/iss.o
CC	libavformat/ivfdec.o
CC	libavformat/ivfenc.o
CC	libavformat/jacosubdec.o
CC	libavformat/jacosubenc.o
CC	libavformat/jpegxl_anim_dec.o
CC	libavformat/jvdec.o
CC	libavformat/kvag.o
CC	libavformat/lafdec.o
CC	libavformat/latmenc.o
CC	libavformat/lc3.o
CC	libavformat/lmlm4.o
CC	libavformat/loasdec.o
CC	libavformat/lrc.o
CC	libavformat/lrcenc.o
CC	libavformat/lrcdec.o
CC	libavformat/luodatdec.o
CC	libavformat/lvfdec.o
CC	libavformat/lxfdec.o
CC	libavformat/m4vdec.o
CC	libavformat/matroska.o
CC	libavformat/matroskadec.o
CC	libavformat/matroskaenc.o
CC	libavformat/mca.o
CC	libavformat/mccdec.o
CC	libavformat/mccenc.o
CC	libavformat/md5proto.o
CC	libavformat/metadata.o
CC	libavformat/mgsts.o
CC	libavformat/microdvddec.o
CC	libavformat/microdvdenc.o
CC	libavformat/mj2kdec.o
CC	libavformat/mkvtimestamp_v2.o
CC	libavformat/mlpdec.o
CC	libavformat/mlvdec.o
CC	libavformat/mm.o
CC	libavformat/mmf.o
CC	libavformat/mms.o
CC	libavformat/mmsh.o
CC	libavformat/mmst.o
CC	libavformat/mods.o
CC	libavformat/moflex.o
CC	libavformat/mov.o
CC	libavformat/mov_chan.o
CC	libavformat/mov_esds.o
CC	libavformat/movenc.o
CC	libavformat/movenc_ttml.o
CC	libavformat/movenccenc.o
CC	libavformat/movenchint.o
CC	libavformat/mp3dec.o
CC	libavformat/mp3enc.o
CC	libavformat/mpc.o
CC	libavformat/mpc8.o
CC	libavformat/mpeg.o
CC	libavformat/mpegenc.o
CC	libavformat/mpegts.o
CC	libavformat/mpegtsenc.o
CC	libavformat/mpegvideodec.o
CC	libavformat/mpjpeg.o
CC	libavformat/mpjpegdec.o
CC	libavformat/mpl2dec.o
CC	libavformat/mpsubdec.o
CC	libavformat/msf.o
CC	libavformat/msnwc_tcp.o
CC	libavformat/mtaf.o
CC	libavformat/mspdec.o
CC	libavformat/mtv.o
CC	libavformat/musx.o
CC	libavformat/mux.o
CC	libavformat/mux_utils.o
CC	libavformat/mvdec.o
CC	libavformat/mvi.o
CC	libavformat/mxf.o
CC	libavformat/mxfdec.o
CC	libavformat/mxfenc.o
CC	libavformat/mxg.o
CC	libavformat/nal.o
CC	libavformat/ncdec.o
CC	libavformat/network.o
CC	libavformat/nistspheredec.o
CC	libavformat/nspdec.o
CC	libavformat/nullenc.o
CC	libavformat/nsvdec.o
CC	libavformat/nut.o
CC	libavformat/nutenc.o
CC	libavformat/nutdec.o
CC	libavformat/oggdec.o
CC	libavformat/nuv.o
CC	libavformat/oggenc.o
CC	libavformat/oggparsedirac.o
CC	libavformat/oggparseflac.o
CC	libavformat/oggparseogm.o
CC	libavformat/oggparsecelt.o
CC	libavformat/oggparseopus.o
CC	libavformat/oggparsespeex.o
CC	libavformat/oggparseskeleton.o
CC	libavformat/oggparsetheora.o
CC	libavformat/oggparsevorbis.o
CC	libavformat/oggparsevp8.o
CC	libavformat/oma.o
CC	libavformat/omaenc.o
CC	libavformat/options.o
CC	libavformat/omadec.o
CC	libavformat/os_support.o
CC	libavformat/osq.o
CC	libavformat/paf.o
src/libavformat/dashenc.c: In function 'dash_init':
src/libavformat/dashenc.c:1449:65: warning: '-stream' directive output may be truncated writing 7 bytes into a region of size between 1 and 1024 [-Wformat-truncation=]
 1449 |                 snprintf(os->initfile, sizeof(os->initfile), "%s-stream%d.%s", basename, i, os->format_name);
      |                                                                 ^~~~~~~
src/libavformat/dashenc.c:1449:62: note: directive argument in the range [0, 2147483647]
 1449 |                 snprintf(os->initfile, sizeof(os->initfile), "%s-stream%d.%s", basename, i, os->format_name);
      |                                                              ^~~~~~~~~~~~~~~~
src/libavformat/dashenc.c:1449:17: note: 'snprintf' output 10 or more bytes (assuming 1042) into a destination of size 1024
 1449 |                 snprintf(os->initfile, sizeof(os->initfile), "%s-stream%d.%s", basename, i, os->format_name);
      |                 ^~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
src/libavformat/dashenc.c:1453:49: warning: '%s' directive output may be truncated writing up to 1023 bytes into a region of size between 1 and 1024 [-Wformat-truncation=]
 1453 |         snprintf(filename, sizeof(filename), "%s%s", c->dirname, os->initfile);
      |                                                 ^~
src/libavformat/dashenc.c:1453:9: note: 'snprintf' output between 1 and 2047 bytes into a destination of size 1024
 1453 |         snprintf(filename, sizeof(filename), "%s%s", c->dirname, os->initfile);
      |         ^~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
CC	libavformat/pcm.o
CC	libavformat/pcmdec.o
CC	libavformat/pcmenc.o
CC	libavformat/pdvdec.o
CC	libavformat/pjsdec.o
CC	libavformat/pmpdec.o
CC	libavformat/prompeg.o
CC	libavformat/protocols.o
CC	libavformat/pp_bnk.o
CC	libavformat/psxstr.o
CC	libavformat/pva.o
CC	libavformat/pvfdec.o
CC	libavformat/qcp.o
CC	libavformat/qoadec.o
CC	libavformat/qtpalette.o
CC	libavformat/r3d.o
CC	libavformat/rawdec.o
CC	libavformat/rawenc.o
CC	libavformat/rawutils.o
CC	libavformat/rawvideodec.o
CC	libavformat/rcwtdec.o
CC	libavformat/rcwtenc.o
CC	libavformat/rdt.o
CC	libavformat/realtextdec.o
CC	libavformat/redspark.o
CC	libavformat/replaygain.o
CC	libavformat/riff.o
CC	libavformat/riffdec.o
CC	libavformat/riffenc.o
CC	libavformat/rka.o
CC	libavformat/rm.o
CC	libavformat/rmdec.o
CC	libavformat/rmenc.o
CC	libavformat/rl2.o
CC	libavformat/rmsipr.o
CC	libavformat/rpl.o
CC	libavformat/rsd.o
CC	libavformat/rso.o
CC	libavformat/rsodec.o
CC	libavformat/rsoenc.o
CC	libavformat/rtmpdigest.o
CC	libavformat/rtmphttp.o
CC	libavformat/rtmppkt.o
CC	libavformat/rtmpproto.o
CC	libavformat/rtp.o
CC	libavformat/rtpdec.o
CC	libavformat/rtpdec_ac3.o
CC	libavformat/rtpdec_amr.o
CC	libavformat/rtpdec_asf.o
CC	libavformat/rtpdec_av1.o
CC	libavformat/rtpdec_dv.o
CC	libavformat/rtpdec_g726.o
CC	libavformat/rtpdec_h261.o
CC	libavformat/rtpdec_h263.o
CC	libavformat/rtpdec_h263_rfc2190.o
CC	libavformat/rtpdec_h264.o
CC	libavformat/rtpdec_hevc.o
CC	libavformat/rtpdec_ilbc.o
CC	libavformat/rtpdec_jpeg.o
CC	libavformat/rtpdec_latm.o
CC	libavformat/rtpdec_mpa_robust.o
CC	libavformat/rtpdec_mpeg12.o
CC	libavformat/rtpdec_mpeg4.o
CC	libavformat/rtpdec_mpegts.o
CC	libavformat/rtpdec_opus.o
CC	libavformat/rtpdec_qcelp.o
CC	libavformat/rtpdec_qdm2.o
src/libavformat/dashenc.c: In function 'flush_init_segment':
src/libavformat/dashenc.c:465:49: warning: '%s' directive output may be truncated writing up to 1023 bytes into a region of size between 1 and 1024 [-Wformat-truncation=]
  465 |         snprintf(filename, sizeof(filename), "%s%s", c->dirname, os->initfile);
      |                                                 ^~
src/libavformat/dashenc.c:465:9: note: 'snprintf' output between 1 and 2047 bytes into a destination of size 1024
  465 |         snprintf(filename, sizeof(filename), "%s%s", c->dirname, os->initfile);
      |         ^~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
CC	libavformat/rtpdec_qt.o
CC	libavformat/rtpdec_rfc4175.o
CC	libavformat/rtpdec_svq3.o
CC	libavformat/rtpdec_vp8.o
CC	libavformat/rtpdec_vp9.o
CC	libavformat/rtpdec_xiph.o
CC	libavformat/rtpenc.o
CC	libavformat/rtpdec_vc2hq.o
CC	libavformat/rtpenc_aac.o
CC	libavformat/rtpenc_amr.o
CC	libavformat/rtpenc_chain.o
CC	libavformat/rtpenc_h261.o
CC	libavformat/rtpenc_h263.o
CC	libavformat/rtpenc_h263_rfc2190.o
src/libavformat/dashenc.c: In function 'write_hls_media_playlist':
src/libavformat/dashenc.c:359:49: warning: 'media_' directive output may be truncated writing 6 bytes into a region of size between 1 and 1024 [-Wformat-truncation=]
  359 |         snprintf(playlist_name, string_size, "%smedia_%d.m3u8", base_url, id);
      |                                                 ^~~~~~
In function 'get_hls_playlist_name',
    inlined from 'write_hls_media_playlist' at src/libavformat/dashenc.c:395:5:
src/libavformat/dashenc.c:359:9: note: 'snprintf' output between 13 and 1046 bytes into a destination of size 1024
  359 |         snprintf(playlist_name, string_size, "%smedia_%d.m3u8", base_url, id);
      |         ^~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
CC	libavformat/rtpenc_av1.o
CC	libavformat/rtpenc_h264_hevc.o
CC	libavformat/rtpenc_jpeg.o
CC	libavformat/rtpenc_latm.o
CC	libavformat/rtpenc_mpegts.o
CC	libavformat/rtpenc_mpv.o
CC	libavformat/rtpenc_rfc4175.o
CC	libavformat/rtpenc_vc2hq.o
CC	libavformat/rtpenc_vp8.o
CC	libavformat/rtpenc_vp9.o
CC	libavformat/rtpenc_xiph.o
CC	libavformat/rtpproto.o
CC	libavformat/rtsp.o
CC	libavformat/rtspdec.o
CC	libavformat/rtspenc.o
CC	libavformat/s337m.o
CC	libavformat/samidec.o
CC	libavformat/sapenc.o
CC	libavformat/sapdec.o
CC	libavformat/sauce.o
CC	libavformat/sbcdec.o
CC	libavformat/sbgdec.o
CC	libavformat/sccenc.o
CC	libavformat/sccdec.o
CC	libavformat/scd.o
CC	libavformat/sdns.o
CC	libavformat/sdp.o
CC	libavformat/sdr2.o
CC	libavformat/sdsdec.o
CC	libavformat/sdxdec.o
CC	libavformat/seek.o
CC	libavformat/segafilm.o
CC	libavformat/segafilmenc.o
CC	libavformat/segment.o
CC	libavformat/serdec.o
CC	libavformat/sga.o
CC	libavformat/shortendec.o
CC	libavformat/sierravmd.o
CC	libavformat/siff.o
CC	libavformat/smacker.o
CC	libavformat/smjpeg.o
CC	libavformat/smjpegdec.o
CC	libavformat/smjpegenc.o
CC	libavformat/smoothstreamingenc.o
CC	libavformat/smush.o
CC	libavformat/sol.o
CC	libavformat/soxdec.o
CC	libavformat/soxenc.o
CC	libavformat/spdif.o
CC	libavformat/spdifdec.o
CC	libavformat/spdifenc.o
CC	libavformat/srtdec.o
CC	libavformat/srtenc.o
CC	libavformat/srtp.o
CC	libavformat/srtpproto.o
CC	libavformat/stldec.o
CC	libavformat/subfile.o
CC	libavformat/subtitles.o
CC	libavformat/subviewer1dec.o
CC	libavformat/subviewerdec.o
CC	libavformat/supdec.o
CC	libavformat/supenc.o
CC	libavformat/svag.o
CC	libavformat/svs.o
CC	libavformat/swf.o
CC	libavformat/swfenc.o
CC	libavformat/takdec.o
CC	libavformat/tcp.o
CC	libavformat/swfdec.o
CC	libavformat/tedcaptionsdec.o
CC	libavformat/tee_common.o
CC	libavformat/teeproto.o
CC	libavformat/thp.o
CC	libavformat/tee.o
CC	libavformat/tiertexseq.o
CC	libavformat/tmv.o
CC	libavformat/tta.o
CC	libavformat/ttaenc.o
CC	libavformat/ttmlenc.o
CC	libavformat/tty.o
CC	libavformat/ty.o
CC	libavformat/txd.o
CC	libavformat/uncodedframecrcenc.o
CC	libavformat/udp.o
CC	libavformat/unix.o
CC	libavformat/url.o
CC	libavformat/urldecode.o
CC	libavformat/usmdec.o
CC	libavformat/utils.o
CC	libavformat/vag.o
CC	libavformat/vc1dec.o
CC	libavformat/vc1test.o
CC	libavformat/vc1testenc.o
CC	libavformat/version.o
CC	libavformat/vivo.o
CC	libavformat/voc.o
CC	libavformat/voc_packet.o
CC	libavformat/vocdec.o
CC	libavformat/vorbiscomment.o
CC	libavformat/vocenc.o
CC	libavformat/vividas.o
CC	libavformat/vpcc.o
CC	libavformat/vpk.o
CC	libavformat/vplayerdec.o
CC	libavformat/vqf.o
CC	libavformat/vvcdec.o
CC	libavformat/vvc.o
CC	libavformat/wady.o
CC	libavformat/w64.o
CC	libavformat/wavarc.o
CC	libavformat/wavdec.o
CC	libavformat/wc3movie.o
CC	libavformat/wavenc.o
CC	libavformat/webmdashenc.o
CC	libavformat/webpenc.o
CC	libavformat/webvttdec.o
CC	libavformat/webm_chunk.o
CC	libavformat/webvttenc.o
CC	libavformat/westwood_aud.o
CC	libavformat/westwood_audenc.o
CC	libavformat/westwood_vqa.o
CC	libavformat/wsddec.o
CC	libavformat/wtv_common.o
CC	libavformat/wtvenc.o
CC	libavformat/wtvdec.o
CC	libavformat/wv.o
CC	libavformat/wvdec.o
CC	libavformat/wvedec.o
CC	libavformat/xa.o
CC	libavformat/xmd.o
CC	libavformat/xmv.o
CC	libavformat/wvenc.o
CC	libavformat/xvag.o
CC	libavformat/yop.o
CC	libavformat/yuv4mpegdec.o
CC	libavformat/xwma.o
CC	libavformat/yuv4mpegenc.o
src/libavformat/vorbiscomment.c: In function 'ff_vorbiscomment_write':
src/libavformat/vorbiscomment.c:103:63: warning: '%03d' directive output may be truncated writing between 3 and 10 bytes into a region of size 4 [-Wformat-truncation=]
  103 |             snprintf(chapter_number, sizeof(chapter_number), "%03d", i);
      |                                                               ^~~~
src/libavformat/vorbiscomment.c:103:62: note: directive argument in the range [0, 2147483647]
  103 |             snprintf(chapter_number, sizeof(chapter_number), "%03d", i);
      |                                                              ^~~~~~
src/libavformat/vorbiscomment.c:103:13: note: 'snprintf' output between 4 and 11 bytes into a destination of size 4
  103 |             snprintf(chapter_number, sizeof(chapter_number), "%03d", i);
      |             ^~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
src/libavformat/vorbiscomment.c:104:69: warning: '%02d' directive output may be truncated writing between 2 and 3 bytes into a region of size between 1 and 7 [-Wformat-truncation=]
  104 |             snprintf(chapter_time, sizeof(chapter_time), "%02d:%02d:%02d.%03d", h, m, s, ms);
      |                                                                     ^~~~
src/libavformat/vorbiscomment.c:104:58: note: directive argument in the range [-59, 59]
  104 |             snprintf(chapter_time, sizeof(chapter_time), "%02d:%02d:%02d.%03d", h, m, s, ms);
      |                                                          ^~~~~~~~~~~~~~~~~~~~~
src/libavformat/vorbiscomment.c:104:58: note: directive argument in the range [-999, 999]
src/libavformat/vorbiscomment.c:104:13: note: 'snprintf' output between 13 and 21 bytes into a destination of size 13
  104 |             snprintf(chapter_time, sizeof(chapter_time), "%02d:%02d:%02d.%03d", h, m, s, ms);
      |             ^~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
CC	libswresample/audioconvert.o
CC	libswresample/dither.o
CC	libswresample/options.o
CC	libswresample/rematrix.o
CC	libswresample/resample.o
CC	libswresample/resample_dsp.o
CC	libswresample/swresample.o
CC	libswresample/swresample_frame.o
CC	libswresample/version.o
CC	libavutil/float2half.o
CC	libavutil/half2float.o
CC	tests/checkasm/aacencdsp.o
CC	tests/checkasm/aacpsdsp.o
CC	tests/checkasm/ac3dsp.o
CC	tests/checkasm/aes.o
CC	tests/checkasm/af_afir.o
CC	tests/checkasm/alacdsp.o
src/libavformat/smoothstreamingenc.c: In function 'ism_flush':
CC	tests/checkasm/apv_dsp.o
src/libavformat/smoothstreamingenc.c:512:49: warning: '/temp' directive output may be truncated writing 5 bytes into a region of size between 1 and 1024 [-Wformat-truncation=]
  512 |         snprintf(filename, sizeof(filename), "%s/temp", os->dirname);
      |                                         
```
