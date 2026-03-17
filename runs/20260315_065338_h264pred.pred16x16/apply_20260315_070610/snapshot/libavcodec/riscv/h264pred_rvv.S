/*
 * RVV optimized H.264 16x16 intra prediction (8-bit)
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
    .option arch, +v

/* void ff_pred16x16_vertical_rvv(uint8_t *src, ptrdiff_t stride);
 * a0=src, a1=stride
 */
    .globl ff_pred16x16_vertical_rvv
    .type  ff_pred16x16_vertical_rvv, @function
ff_pred16x16_vertical_rvv:
    addi    t0, a0, 0
    sub     t1, a0, a1          /* top row base: src - stride */
    li      t2, 16
1:
    vsetvli t3, t2, e8, m1, ta, ma
    vle8.v  v0, (t1)
    vse8.v  v0, (t0)
    add     t0, t0, a1
    add     t1, t1, a1          /* keep t1 for completeness; same top reused not needed */
    addi    t2, t2, -1
    bnez    t2, 1b
    ret
    .size ff_pred16x16_vertical_rvv, .-ff_pred16x16_vertical_rvv

/* void ff_pred16x16_horizontal_rvv(uint8_t *src, ptrdiff_t stride);
 * Each row filled with left[y] = src[y*stride-1]
 */
    .globl ff_pred16x16_horizontal_rvv
    .type  ff_pred16x16_horizontal_rvv, @function
ff_pred16x16_horizontal_rvv:
    li      t2, 16
2:
    lbu     t0, -1(a0)          /* left pixel */
    vsetvli t3, zero, e8, m1, ta, ma
    vmv.v.x v0, t0
    vse8.v  v0, (a0)
    add     a0, a0, a1
    addi    t2, t2, -1
    bnez    t2, 2b
    ret
    .size ff_pred16x16_horizontal_rvv, .-ff_pred16x16_horizontal_rvv

/* Helper macro: horizontal sum of 16 u8 in memory -> a2
 * uses v0, v1 and temporaries.
 */
.macro SUM16_U8_TO_X dst, ptr
    li      t0, 16
    vsetvli t1, t0, e8, m1, ta, ma
    vle8.v  v0, (\ptr)
    vwaddu.vx v1, v0, zero      /* widen to u16 */
    vsetvli t1, t0, e16, m2, ta, ma
    vredsum.vs v2, v1, v0       /* v0 as zero seed (low bits ignored) */
    vmv.x.s \dst, v2
.endm

/* void ff_pred16x16_dc_rvv(uint8_t *src, ptrdiff_t stride);
 * dc = (sum(top[0..15]) + sum(left[0..15]) + 16) >> 5
 */
    .globl ff_pred16x16_dc_rvv
    .type  ff_pred16x16_dc_rvv, @function
ff_pred16x16_dc_rvv:
    /* sum top */
    sub     t3, a0, a1
    SUM16_U8_TO_X a2, t3

    /* sum left (gather scalar) */
    mv      t0, a0
    li      t1, 16
    li      t2, 0
3:
    lbu     t4, -1(t0)
    add     t2, t2, t4
    add     t0, t0, a1
    addi    t1, t1, -1
    bnez    t1, 3b

    add     a2, a2, t2
    addi    a2, a2, 16
    srli    a2, a2, 5

    /* fill 16x16 */
    li      t1, 16
    vsetvli t0, zero, e8, m1, ta, ma
    vmv.v.x v0, a2
4:
    vse8.v  v0, (a0)
    add     a0, a0, a1
    addi    t1, t1, -1
    bnez    t1, 4b
    ret
    .size ff_pred16x16_dc_rvv, .-ff_pred16x16_dc_rvv

/* void ff_pred16x16_top_dc_rvv(uint8_t *src, ptrdiff_t stride);
 * dc = (sum(top) + 8) >> 4
 */
    .globl ff_pred16x16_top_dc_rvv
    .type  ff_pred16x16_top_dc_rvv, @function
ff_pred16x16_top_dc_rvv:
    sub     t3, a0, a1
    SUM16_U8_TO_X a2, t3
    addi    a2, a2, 8
    srli    a2, a2, 4

    li      t1, 16
    vsetvli t0, zero, e8, m1, ta, ma
    vmv.v.x v0, a2
5:
    vse8.v  v0, (a0)
    add     a0, a0, a1
    addi    t1, t1, -1
    bnez    t1, 5b
    ret
    .size ff_pred16x16_top_dc_rvv, .-ff_pred16x16_top_dc_rvv

/* void ff_pred16x16_left_dc_rvv(uint8_t *src, ptrdiff_t stride);
 * dc = (sum(left) + 8) >> 4
 */
    .globl ff_pred16x16_left_dc_rvv
    .type  ff_pred16x16_left_dc_rvv, @function
ff_pred16x16_left_dc_rvv:
    mv      t0, a0
    li      t1, 16
    li      a2, 0
6:
    lbu     t2, -1(t0)
    add     a2, a2, t2
    add     t0, t0, a1
    addi    t1, t1, -1
    bnez    t1, 6b

    addi    a2, a2, 8
    srli    a2, a2, 4

    li      t1, 16
    vsetvli t0, zero, e8, m1, ta, ma
    vmv.v.x v0, a2
7:
    vse8.v  v0, (a0)
    add     a0, a0, a1
    addi    t1, t1, -1
    bnez    t1, 7b
    ret
    .size ff_pred16x16_left_dc_rvv, .-ff_pred16x16_left_dc_rvv

/* void ff_pred16x16_128_dc_rvv(uint8_t *src, ptrdiff_t stride);
 */
    .globl ff_pred16x16_128_dc_rvv
    .type  ff_pred16x16_128_dc_rvv, @function
ff_pred16x16_128_dc_rvv:
    li      a2, 128
    li      t1, 16
    vsetvli t0, zero, e8, m1, ta, ma
    vmv.v.x v0, a2
8:
    vse8.v  v0, (a0)
    add     a0, a0, a1
    addi    t1, t1, -1
    bnez    t1, 8b
    ret
    .size ff_pred16x16_128_dc_rvv, .-ff_pred16x16_128_dc_rvv

/* void ff_pred16x16_plane_rvv(uint8_t *src, ptrdiff_t stride);
 * Implements H.264 plane mode for 8-bit.
 */
    .globl ff_pred16x16_plane_rvv
    .type  ff_pred16x16_plane_rvv, @function
ff_pred16x16_plane_rvv:
    addi    sp, sp, -64
    sd      ra, 56(sp)
    sd      s0, 48(sp)
    sd      s1, 40(sp)
    sd      s2, 32(sp)
    sd      s3, 24(sp)
    sd      s4, 16(sp)
    sd      s5, 8(sp)

    mv      s0, a0              /* src */
    mv      s1, a1              /* stride */

    /* top base = src - stride; left reads from src + y*stride - 1 */
    sub     s2, s0, s1          /* top */

    /* Compute H = sum_{i=1..8} i*(top[7+i]-top[7-i]) */
    li      s3, 0               /* H */
    li      t0, 1
9:
    addi    t1, t0, 7           /* 7+i */
    sub     t2, zero, t0
    addi    t2, t2, 7           /* 7-i */
    add     t3, s2, t1
    lbu     t4, 0(t3)
    add     t3, s2, t2
    lbu     t5, 0(t3)
    sub     t6, t4, t5
    mul     t6, t6, t0
    add     s3, s3, t6
    addi    t0, t0, 1
    li      t7, 9
    blt     t0, t7, 9b

    /* Compute V = sum_{i=1..8} i*(left[7+i]-left[7-i]) */
    li      s4, 0               /* V */
    li      t0, 1
10:
    addi    t1, t0, 7           /* 7+i */
    sub     t2, zero, t0
    addi    t2, t2, 7           /* 7-i */

    /* left[7+i] */
    mul     t3, t1, s1
    add     t3, s0, t3
    lbu     t4, -1(t3)
    /* left[7-i] */
    mul     t3, t2, s1
    add     t3, s0, t3
    lbu     t5, -1(t3)

    sub     t6, t4, t5
    mul     t6, t6, t0
    add     s4, s4, t6

    addi    t0, t0, 1
    li      t7, 9
    blt     t0, t7, 10b

    /* a = 16*(top[15] + left[15]) */
    lbu     t0, 15(s2)
    mul     t1, 15, s1
    add     t1, s0, t1
    lbu     t1, -1(t1)
    add     t0, t0, t1
    slli    s5, t0, 4           /* a */

    /* b = (5*H + 32) >> 6 ; c = (5*V + 32) >> 6 */
    li      t0, 5
    mul     t1, s3, t0
    addi    t1, t1, 32
    srai    s3, t1, 6           /* b in s3 */

    mul     t1, s4, t0
    addi    t1, t1, 32
    srai    s4, t1, 6           /* c in s4 */

    /* Prepare x vector (x-7) for 0..15: [-7..8] in i16 */
    li      t0, 16
    vsetvli t1, t0, e16, m1, ta, ma
    vid.v   v0                  /* 0.. */
    li      t2, 7
    vsub.vx v0, v0, t2          /* x-7 */

    /* bx = b*(x-7) */
    vmul.vx v1, v0, s3          /* i16 */

    /* loop y */
    li      t3, 0               /* y */
11:
    /* yterm = c*(y-7) */
    addi    t4, t3, -7
    mul     t4, t4, s4

    /* base = a + yterm + 16 */
    add     t5, s5, t4
    addi    t5, t5, 16

    /* val = (base + bx) >> 5 */
    vadd.vx v2, v1, t5
    vsra.vi v2, v2, 5

    /* clip to [0,255] (8-bit) */
    vmax.vx v2, v2, zero
    li      t6, 255
    vmin.vx v2, v2, t6

    /* narrow to u8 and store */
    vsetvli t1, t0, e8, m1, ta, ma
    vnclipu.wi v3, v2, 0

    mul     t6, t3, s1
    add     t6, s0, t6
    vse8.v  v3, (t6)

    addi    t3, t3, 1
    li      t7, 16
    blt     t3, t7, 11b

    ld      ra, 56(sp)
    ld      s0, 48(sp)
    ld      s1, 40(sp)
    ld      s2, 32(sp)
    ld      s3, 24(sp)
    ld      s4, 16(sp)
    ld      s5, 8(sp)
    addi    sp, sp, 64
    ret
    .size ff_pred16x16_plane_rvv, .-ff_pred16x16_plane_rvv

#endif /* HAVE_RVV */
