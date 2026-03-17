/*
 * Copyright (c) FFmpeg
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

#include "libavfilter/scene_sad.h"

/*
 * Some FFmpeg versions/toolchains may not expose AV_CPU_FLAG_RVV yet.
 * Use it if available; otherwise fall back to 0 so code compiles.
 */
#ifndef AV_CPU_FLAG_RVV
#define AV_CPU_FLAG_RVV 0
#endif

/* Exported prototype is declared in libavfilter/scene_sad.h */

#if HAVE_RVV
/* These functions are implemented in riscv/scene_sad_rvv.S */
void ff_scene_sad16_rvv(const uint8_t *src1, ptrdiff_t stride1,
                        const uint8_t *src2, ptrdiff_t stride2,
                        ptrdiff_t w, ptrdiff_t h, uint64_t *sum);
#endif

ff_scene_sad_fn ff_scene_sad_get_fn_riscv(int depth)
{
    const int flags = av_get_cpu_flags();

#if HAVE_RVV
    if (AV_CPU_FLAG_RVV && (flags & AV_CPU_FLAG_RVV)) {
        /* scene_sad currently only uses 8-bit paths; keep depth gate conservative */
        if (depth <= 8)
            return ff_scene_sad16_rvv;
    }
#endif

    return NULL;
}
