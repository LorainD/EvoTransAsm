/*
 * Copyright (c) 2023
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

#include "libavutil/attributes.h"
#include "libavutil/cpu.h"
#include "libavutil/riscv/cpu.h"

#include "libavcodec/h264pred.h"

/*
 * Some FFmpeg trees (or downstream forks) may not expose AV_CPU_FLAG_RVV
 * publicly. Prefer the flag from libavutil/riscv/cpu.h when available,
 * otherwise compile the file without RVV dispatch.
 */
#ifndef AV_CPU_FLAG_RVV
#if defined(AV_CPU_FLAG_RISCV_V)
#define AV_CPU_FLAG_RVV AV_CPU_FLAG_RISCV_V
#else
#define AV_CPU_FLAG_RVV 0
#endif
#endif

/* Ensure there is a visible prototype to satisfy -Wmissing-prototypes. */
av_cold void ff_h264_pred_init_riscv(H264PredContext *h, int codec_id);

av_cold void ff_h264_pred_init_riscv(H264PredContext *h, int codec_id)
{
    int flags = av_get_cpu_flags();

    /* If we don't have a usable RVV flag in this tree, this becomes a no-op. */
    if (!(flags & AV_CPU_FLAG_RVV))
        return;

    /*
     * RVV-optimized function assignments would go here.
     *
     * Keep this file buildable across FFmpeg versions even if the RVV
     * implementations are not present.
     */
    (void)h;
    (void)codec_id;
}
