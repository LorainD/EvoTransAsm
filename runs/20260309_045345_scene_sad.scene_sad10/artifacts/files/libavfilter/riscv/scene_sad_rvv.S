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

#include "libavutil/riscv/asm.S"

/*
 * void ff_scene_sad10_rvv(const uint8_t *src1, ptrdiff_t stride1,
 *                         const uint8_t *src2, ptrdiff_t stride2,
 *                         ptrdiff_t width, ptrdiff_t height, uint64_t *sum);
 *
 * width is in pixels (uint16 elements), stride is in bytes.
 */

    .text
    .align  2
    .globl  ff_scene_sad10_rvv
    .type   ff_scene_sad10_rvv, @function
ff_scene_sad10_rvv:
    /* a0=src1 a1=stride1 a2=src2 a3=stride2 a4=width a5=height a6=sum */

    /* sad accumulator in t0 (uint64) */
    li      t0, 0

    /* handle height==0 early */
    beqz    a5, 3f

1:  /* for each row */
    mv      t1, a0              /* row src1 */
    mv      t2, a2              /* row src2 */
    mv      t3, a4              /* remaining width (pixels) */

2:  /* vector loop over row */
    beqz    t3, 4f

    /* set VL in elements of e16 */
    vsetvli t4, t3, e16, m1, ta, ma

    vle16.v v0, (t1)
    vle16.v v1, (t2)

    /* abs diff -> u16 */
    vsub.vv v2, v0, v1
    vabs.v  v2, v2

    /* widen to u32 and reduce to scalar */
    vwcvtu.x.x.v v4, v2         /* u16 -> u32 */
    vsetvli zero, t4, e32, m2, ta, ma
    vmv.v.i v6, 0
    vredsum.vs v6, v4, v6
    vmv.x.s t5, v6

    add     t0, t0, t5

    /* advance pointers by vl * 2 bytes */
    slli    t6, t4, 1
    add     t1, t1, t6
    add     t2, t2, t6

    sub     t3, t3, t4
    j       2b

4:
    /* next row */
    add     a0, a0, a1
    add     a2, a2, a3
    addi    a5, a5, -1
    bnez    a5, 1b

3:
    sd      t0, 0(a6)
    ret

    .size   ff_scene_sad10_rvv, .-ff_scene_sad10_rvv
