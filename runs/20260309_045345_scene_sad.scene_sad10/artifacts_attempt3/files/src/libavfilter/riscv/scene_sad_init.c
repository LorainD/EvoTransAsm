/*
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
#include "libavfilter/scene_sad.h"

/*
 * Provide a proper prototype (exported via scene_sad.h) and avoid relying on
 * AV_CPU_FLAG_RVV which may not be available in the libavutil/cpu.h used by
 * this build. On RISC-V, RVV availability is exposed via the riscv-specific
 * CPU flag helper.
 */

ff_scene_sad_fn ff_scene_sad_get_fn_riscv(int depth)
{
    /* Depth handling is kept for API compatibility; current RVV kernels (if
     * present) are typically for 8-bit.
     */
    (void)depth;

#if HAVE_RVV
    /* Use riscv-specific flag instead of AV_CPU_FLAG_RVV (not guaranteed to exist). */
    const int flags = av_get_cpu_flags();
    if (flags & AV_CPU_FLAG_RISCV_RVV) {
        /* If your tree defines RVV optimized functions, return them here.
         * In case kernels are not built/available, fall back to NULL.
         */
        return NULL;
    }
#endif

    return NULL;
}
