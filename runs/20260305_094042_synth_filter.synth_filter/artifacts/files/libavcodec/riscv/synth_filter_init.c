/*
 * RISC-V optimizations for synth_filter
 *
 * This file is part of FFmpeg.
 *
 * FFmpeg is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
 */

#include "config.h"

#include "libavutil/attributes.h"
#include "libavutil/cpu.h"
#include "libavutil/riscv/cpu.h"

#include "libavcodec/synth_filter.h"

#if HAVE_RVV
void ff_synth_filter_float_rvv(AVTXContext *imdct,
                               float *synth_buf_ptr, int *synth_buf_offset,
                               float synth_buf2[32], const float window[512],
                               float out[32], float in[32],
                               float scale, av_tx_fn imdct_fn);
#endif

av_cold void ff_synth_filter_init_riscv(SynthFilterContext *s)
{
#if HAVE_RVV
    int flags = av_get_cpu_flags();

    if (have_rvv(flags))
        s->synth_filter_float = ff_synth_filter_float_rvv;
#endif
}
