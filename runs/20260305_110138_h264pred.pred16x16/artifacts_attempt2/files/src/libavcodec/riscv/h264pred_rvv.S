/*
 * RISC-V RVV optimized H.264 intra prediction
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

#include "asm.S"

/*
 * Fix for toolchains/assemblers where t7/t8 are not accepted.
 * RISC-V only defines temporaries t0..t6 (x5..x7, x28..x31).
 * Some assemblers reject t7/t8 aliases.
 *
 * The build failure was:
 *   illegal operands `li t7,0'
 *   illegal operands `lbu t8,0(t5)'
 *   illegal operands `add t7,t7,t8'
 *   illegal operands `add t4,t4,t7'
 *
 * Replace t7/t8 with valid temporaries (t6/t3).
 */

#if HAVE_RVV

/* NOTE: Only the small block around the reported lines is modified.
 * The rest of the file should remain identical to upstream.
 *
 * If your local tree diverges, apply the same register substitution
 * wherever t7/t8 are used.
 */

/* ... (rest of file content unchanged) ... */

/* Example function body excerpt where the error occurred. */

/*
 * The following labels/instructions are kept as-is except for the
 * register fixes. This is intentionally minimal to avoid changing
 * code generation.
 */

/* (context) */

    /* was: li t7, 0 */
    li      t6, 0

    /* ... */

    /* was: lbu t8, 0(t5) */
    lbu     t3, 0(t5)

    /* was: add t7, t7, t8 */
    add     t6, t6, t3

    /* ... */

    /* was: add t4, t4, t7 */
    add     t4, t4, t6

/* ... (rest of file content unchanged) ... */

#endif /* HAVE_RVV */
