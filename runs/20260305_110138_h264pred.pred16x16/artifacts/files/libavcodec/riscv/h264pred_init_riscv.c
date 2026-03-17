/*
 * RISC-V optimizations for H.264 intra prediction
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
#include "libavutil/riscv/cpu.h"
#include "libavcodec/h264pred.h"

#if HAVE_RVV
void ff_pred16x16_dc_8_rvv(uint8_t *src, ptrdiff_t stride);
#endif

av_cold void ff_h264_pred_init_riscv(H264PredContext *h, int codec_id,
                                     const int bit_depth,
                                     const int chroma_format_idc)
{
#if HAVE_RVV
    int cpu_flags = av_get_cpu_flags();

    if (bit_depth == 8 && (cpu_flags & AV_CPU_FLAG_RVV)) {
        /* pred16x16 only: register DC mode */
        h->pred16x16[DC_PRED8x8] = ff_pred16x16_dc_8_rvv;
    }
#endif
}
