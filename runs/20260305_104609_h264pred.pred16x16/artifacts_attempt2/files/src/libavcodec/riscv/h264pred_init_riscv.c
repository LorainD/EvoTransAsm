/*
 * Copyright (c) 2022-2024
 *
 * This file is part of FFmpeg.
 *
 * FFmpeg is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
 *
 * FFmpeg is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
 * Lesser General Public License for more details.
 *
 * You should have received a copy of the GNU Lesser General Public
 * License along with FFmpeg; if not, write to the Free Software
 * Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
 */

#include "config.h"

#include "libavutil/attributes.h"
#include "libavutil/cpu.h"

#include "libavcodec/h264pred.h"
#include "libavcodec/riscv/h264pred.h"

/*
 * Some trees/versions may not expose AV_CPU_FLAG_RVV in libavutil/cpu.h.
 * Provide a safe fallback so this file can compile across configurations.
 */
#ifndef AV_CPU_FLAG_RVV
#define AV_CPU_FLAG_RVV 0
#endif

av_cold void ff_h264_pred_init_riscv(H264PredContext *h, int codec_id,
                                    int bit_depth, const int chroma_format_idc)
{
    int flags = av_get_cpu_flags();

    if (bit_depth == 8 && (flags & AV_CPU_FLAG_RVV)) {
        ff_h264_pred_init_riscv_rvv(h, codec_id, bit_depth, chroma_format_idc);
    }
}
