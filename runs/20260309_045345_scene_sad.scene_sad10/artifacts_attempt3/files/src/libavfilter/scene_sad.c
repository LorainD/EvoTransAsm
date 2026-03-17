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

#include "config.h"

#include "libavutil/attributes.h"
#include "libavutil/cpu.h"

#include "scene_sad.h"

#if ARCH_RISCV
/* Avoid implicit declaration: provide prototype when riscv is enabled.
 * The implementation lives in libavfilter/riscv/scene_sad_init.c.
 */
ff_scene_sad_fn ff_scene_sad_get_fn_riscv(int depth);
#endif

ff_scene_sad_fn ff_scene_sad_get_fn(int depth)
{
    ff_scene_sad_fn sad = NULL;

#if ARCH_X86
    sad = ff_scene_sad_get_fn_x86(depth);
#elif ARCH_AARCH64
    sad = ff_scene_sad_get_fn_aarch64(depth);
#elif ARCH_RISCV
    sad = ff_scene_sad_get_fn_riscv(depth);
#endif

    return sad;
}
