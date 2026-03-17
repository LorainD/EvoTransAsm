/*
 * Copyright (c) 2024
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

#include "libavfilter/scene_sad.h"

/*
 * Provide a prototype in this TU to satisfy -Wmissing-prototypes.
 * The public prototype is also added in libavfilter/scene_sad.h.
 */
ff_scene_sad_fn ff_scene_sad_get_fn_riscv(int depth);

/* RVV implementations (if built) */
#if HAVE_RVV
void ff_scene_sad8_rvv(const uint8_t *p1, ptrdiff_t stride1,
                       const uint8_t *p2, ptrdiff_t stride2,
                       ptrdiff_t width, ptrdiff_t height,
                       uint64_t *sum);
#endif

ff_scene_sad_fn ff_scene_sad_get_fn_riscv(int depth)
{
    /* Use the same RVV feature flag definition as libavutil/riscv/cpu.h */
    int flags = av_get_cpu_flags();

#if HAVE_RVV
    if (flags & AV_CPU_FLAG_V) {
        switch (depth) {
        case 8:
            return ff_scene_sad8_rvv;
        default:
            break;
        }
    }
#endif

    return NULL;
}
