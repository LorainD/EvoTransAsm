/*
 * RISC-V H.264 intra prediction init
 *
 * This file is part of FFmpeg.
 *
 * FFmpeg is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
 */

#include <stddef.h>
#include <stdint.h>

#include "config.h"
#include "libavutil/attributes.h"
#include "libavutil/cpu.h"
#include "libavcodec/codec_id.h"
#include "libavcodec/h264pred.h"

#if HAVE_RVV
void ff_pred16x16_vertical_rvv(uint8_t *src, ptrdiff_t stride);
void ff_pred16x16_horizontal_rvv(uint8_t *src, ptrdiff_t stride);
void ff_pred16x16_dc_rvv(uint8_t *src, ptrdiff_t stride);
void ff_pred16x16_top_dc_rvv(uint8_t *src, ptrdiff_t stride);
void ff_pred16x16_left_dc_rvv(uint8_t *src, ptrdiff_t stride);
void ff_pred16x16_128_dc_rvv(uint8_t *src, ptrdiff_t stride);
void ff_pred16x16_plane_rvv(uint8_t *src, ptrdiff_t stride);
#endif

av_cold void ff_h264_pred_init_riscv(H264PredContext *h, int codec_id,
                                    int bit_depth, int chroma_format_idc)
{
#if HAVE_RVV
    int flags = av_get_cpu_flags();

    /* 首期仅覆盖 8-bit；高位深仍走通用 C 实现 */
    if (bit_depth > 8)
        return;

    /* 遵循 FFmpeg 约定：用 RVV I32 flag 作为“具备 RVV”判定 */
    if (!(flags & AV_CPU_FLAG_RVV_I32))
        return;

    (void)chroma_format_idc;

    h->pred16x16[VERT_PRED16x16]   = ff_pred16x16_vertical_rvv;
    h->pred16x16[HOR_PRED16x16]    = ff_pred16x16_horizontal_rvv;

    /* DC/TOP/LEFT/128：对所有 codec_id 适用 */
    h->pred16x16[DC_PRED16x16]     = ff_pred16x16_dc_rvv;
    h->pred16x16[TOP_DC_PRED16x16] = ff_pred16x16_top_dc_rvv;
    h->pred16x16[LEFT_DC_PRED16x16]= ff_pred16x16_left_dc_rvv;
    h->pred16x16[DC_128_PRED16x16] = ff_pred16x16_128_dc_rvv;

    /* PLANE：VP7/VP8 不使用 plane 预测（与 arm init 逻辑一致） */
    if (codec_id != AV_CODEC_ID_VP7 && codec_id != AV_CODEC_ID_VP8)
        h->pred16x16[PLANE_PRED16x16] = ff_pred16x16_plane_rvv;
#else
    (void)h;
    (void)codec_id;
    (void)bit_depth;
    (void)chroma_format_idc;
#endif
}
