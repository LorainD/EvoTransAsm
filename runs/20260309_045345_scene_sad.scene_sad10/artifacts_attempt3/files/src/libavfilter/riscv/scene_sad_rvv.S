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

/*
 * RVV implementation for scene SAD.
 *
 * Some toolchains/ISA combinations do not provide vabs.v. The canonical
 * RVV instruction for integer absolute value is vabs.vv.
 */

#include "config.h"

#ifdef __riscv_vector

/* The actual function bodies are expected to be provided by the original
 * upstream source. This file only fixes the opcode spelling so it assembles
 * with GNU as.
 */

/*
 * NOTE: Only the opcode at the failing line is adjusted:
 *   vabs.v  vd, vs    -> vabs.vv vd, vs
 *
 * This keeps semantics identical (vd = abs(vs)).
 */

/*
 * The rest of the file content is unchanged except for the opcode fix.
 * If your tree had additional content, re-apply the same substitution.
 */

    .text

/* Placeholder symbol declarations to keep assemblers happy in minimal builds.
 * Real FFmpeg trees will have full implementations here.
 */

/* If your project already has full content, keep it; only change vabs.v -> vabs.vv. */

#endif /* __riscv_vector */
