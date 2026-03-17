/*
 * RISC-V Vector (RVV) H.264 intra prediction
 *
 * This file is part of FFmpeg.
 *
 * FFmpeg is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
 */

#include "config.h"

#if HAVE_RVV

    .text
    .align  2

/*
 * void ff_pred16x16_vertical_8_rvv(uint8_t *src, ptrdiff_t stride)
 * a0 = src, a1 = stride
 */
    .globl  ff_pred16x16_vertical_8_rvv
    .type   ff_pred16x16_vertical_8_rvv, @function
ff_pred16x16_vertical_8_rvv:
    add     t0, a0, x0          /* cur = src */
    sub     t1, a0, a1          /* top = src - stride */

    /* Set VL=16 bytes */
    li      t2, 16
    vsetvli t2, t2, e8, m1, ta, ma

    vle8.v  v0, (t1)            /* load top[0..15] */

    li      t3, 16              /* rows */
1:
    vse8.v  v0, (t0)            /* store row */
    add     t0, t0, a1
    addi    t3, t3, -1
    bnez    t3, 1b
    ret
    .size   ff_pred16x16_vertical_8_rvv, .-ff_pred16x16_vertical_8_rvv

#endif /* HAVE_RVV */
