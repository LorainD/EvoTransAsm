/*
 * Copyright (c) 2026
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
#include "libavutil/riscv/cpu.h"

#include "libavcodec/synth_filter.h"

void ff_synth_filter_float_rvv(AVTXContext *imdct,
                               float *synth_buf_ptr, int *synth_buf_offset,
                               float synth_buf2[32], const float window[512],
                               float out[32], float in[32], float scale,
                               av_tx_fn imdct_fn);

/* Used to avoid implicit declaration if the helper is not provided by
 * the included headers on some configurations.
 */
static av_always_inline int ff_rvv_available(int cpu_flags)
{
#if HAVE_RVV
#   ifdef HAVE_RVV
    /* prefer the canonical helper if available */
#   if defined(have_rvv)
    return have_rvv(cpu_flags);
#   else
    /* Fallback to the public flag bit if the helper macro/function isn't visible. */
    return cpu_flags & AV_CPU_FLAG_RVV;
#   endif
#   else
    return cpu_flags & AV_CPU_FLAG_RVV;
#   endif
#else
    (void)cpu_flags;
    return 0;
#endif
}

av_cold void ff_synth_filter_init_riscv(SynthFilterContext *s);

av_cold void ff_synth_filter_init_riscv(SynthFilterContext *s)
{
#if HAVE_RVV
    int cpu_flags = av_get_cpu_flags();

    if (ff_rvv_available(cpu_flags))
        s->synth_filter_float = ff_synth_filter_float_rvv;
#else
    (void)s;
#endif
}
