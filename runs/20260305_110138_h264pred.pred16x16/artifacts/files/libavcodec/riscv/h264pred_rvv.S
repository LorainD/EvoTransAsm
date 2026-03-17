/*
 * RISC-V RVV optimized H.264 intra prediction
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

/*
 * void ff_pred16x16_dc_8_rvv(uint8_t *src, ptrdiff_t stride);
 * Computes DC from top[0..15] (src - stride) and left[0..15] (src[-1 + y*stride])
 * and fills 16x16.
 */
    .globl ff_pred16x16_dc_8_rvv
    .type  ff_pred16x16_dc_8_rvv, @function
ff_pred16x16_dc_8_rvv:
    /* a0=src, a1=stride */
    addi    t0, a0, 0               /* base dst */
    sub     t1, a0, a1              /* top row address */

    /* set vl=16 bytes */
    li      t2, 16
    vsetvli t2, t2, e8, m1, ta, ma

    /* sum top[0..15] */
    vle8.v  v0, (t1)
    vwaddu.vx v1, v0, zero          /* u16 */
    vsetvli t3, zero, e16, m1, ta, ma
    vredsum.vs v2, v1, v2
    vmv.x.s t4, v2                  /* t4 = sum_top */

    /* sum left[0..15] scalar loads */
    addi    t5, a0, -1              /* left column pointer */
    li      t6, 16
    li      t7, 0
1:
    lbu     t8, 0(t5)
    add     t7, t7, t8
    add     t5, t5, a1
    addi    t6, t6, -1
    bnez    t6, 1b

    add     t4, t4, t7              /* sum_top + sum_left */
    addi    t4, t4, 16
    srli    t4, t4, 5               /* dc */

    /* broadcast dc to v0 (u8) */
    vsetvli t2, zero, e8, m1, ta, ma
    vmv.v.x v0, t4

    /* store 16 bytes per row, 16 rows */
    li      t6, 16
2:
    vse8.v  v0, (t0)
    add     t0, t0, a1
    addi    t6, t6, -1
    bnez    t6, 2b

    ret
    .size ff_pred16x16_dc_8_rvv, .-ff_pred16x16_dc_8_rvv

#endif /* HAVE_RVV */
