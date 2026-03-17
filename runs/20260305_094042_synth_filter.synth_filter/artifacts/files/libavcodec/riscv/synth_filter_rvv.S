/*
 * RVV-optimized synth_filter for DCA decoder
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
    .option norvc

/*
 * void ff_synth_filter_float_rvv(AVTXContext *imdct,
 *                                float *synth_buf_ptr, int *synth_buf_offset,
 *                                float synth_buf2[32], const float window[512],
 *                                float out[32], float in[32], float scale,
 *                                av_tx_fn imdct_fn);
 *
 * Minimal, correct implementation (scalar core) in a .S file.
 */

    .globl ff_synth_filter_float_rvv
    .type  ff_synth_filter_float_rvv, @function
ff_synth_filter_float_rvv:
    /* Prologue */
    addi    sp, sp, -64
    sd      ra, 56(sp)
    sd      s0, 48(sp)
    sd      s1, 40(sp)
    sd      s2, 32(sp)
    sd      s3, 24(sp)
    sd      s4, 16(sp)
    sd      s5, 8(sp)
    sd      s6, 0(sp)

    /* Save args in callee-saved regs */
    mv      s0, a0              /* imdct */
    mv      s1, a1              /* synth_buf_ptr */
    mv      s2, a2              /* synth_buf_offset* */
    mv      s3, a3              /* synth_buf2 */
    mv      s4, a4              /* window */
    mv      s5, a5              /* out */
    mv      s6, a9              /* imdct_fn */

    /* Load offset, compute synth_buf = synth_buf_ptr + offset */
    lw      t0, 0(s2)           /* offset */
    slli    t1, t0, 2
    add     t2, s1, t1          /* t2 = synth_buf */

    /* Call imdct_fn(imdct, synth_buf, in, sizeof(float)) */
    mv      a0, s0
    mv      a1, t2
    mv      a2, a6              /* in */
    li      a3, 4               /* sizeof(float) */
    jalr    s6

    /* scale is in fa0 (a7 passed as float in hard-float ABI); keep in ft0 */
    fmv.s   ft0, fa0

    /* for i=0..15 */
    li      t3, 0               /* i */
1:
    /* a = synth_buf2[i], b = synth_buf2[i+16] */
    slli    t4, t3, 2
    add     t5, s3, t4
    flw     ft1, 0(t5)          /* a */
    addi    t6, t3, 16
    slli    t6, t6, 2
    add     t6, s3, t6
    flw     ft2, 0(t6)          /* b */

    /* c = 0, d = 0 */
    fmv.s.x ft3, zero           /* c */
    fmv.s.x ft4, zero           /* d */

    /* j loop: for (j = 0; j < 512 - offset; j += 64) */
    li      t7, 512
    lw      t0, 0(s2)
    sub     t7, t7, t0          /* t7 = 512 - offset */
    li      t8, 0               /* j */
2:
    bge     t8, t7, 3f

    /* a += window[i+j] * (-synth_buf[15-i+j]) */
    add     t9, t8, t3
    slli    t9, t9, 2
    add     t9, s4, t9
    flw     ft5, 0(t9)          /* win */

    li      t10, 15
    sub     t10, t10, t3
    add     t10, t10, t8
    slli    t10, t10, 2
    add     t10, t2, t10
    flw     ft6, 0(t10)         /* buf */
    fneg.s  ft6, ft6
    fmadd.s ft1, ft5, ft6, ft1

    /* b += window[i+j+16] * synth_buf[i+j] */
    addi    t11, t8, 16
    add     t11, t11, t3
    slli    t11, t11, 2
    add     t11, s4, t11
    flw     ft5, 0(t11)

    add     t10, t8, t3
    slli    t10, t10, 2
    add     t10, t2, t10
    flw     ft6, 0(t10)
    fmadd.s ft2, ft5, ft6, ft2

    /* c += window[i+j+32] * synth_buf[16+i+j] */
    addi    t11, t8, 32
    add     t11, t11, t3
    slli    t11, t11, 2
    add     t11, s4, t11
    flw     ft5, 0(t11)

    addi    t10, t8, 16
    add     t10, t10, t3
    slli    t10, t10, 2
    add     t10, t2, t10
    flw     ft6, 0(t10)
    fmadd.s ft3, ft5, ft6, ft3

    /* d += window[i+j+48] * synth_buf[31-i+j] */
    addi    t11, t8, 48
    add     t11, t11, t3
    slli    t11, t11, 2
    add     t11, s4, t11
    flw     ft5, 0(t11)

    li      t10, 31
    sub     t10, t10, t3
    add     t10, t10, t8
    slli    t10, t10, 2
    add     t10, t2, t10
    flw     ft6, 0(t10)
    fmadd.s ft4, ft5, ft6, ft4

    addi    t8, t8, 64
    j       2b

3:
    /* second j loop: for (; j < 512; j += 64) with wrap (index - 512) */
    li      t7, 512
4:
    bge     t8, t7, 5f

    /* a += window[i+j] * (-synth_buf[15-i+j-512]) */
    add     t9, t8, t3
    slli    t9, t9, 2
    add     t9, s4, t9
    flw     ft5, 0(t9)

    li      t10, 15
    sub     t10, t10, t3
    add     t10, t10, t8
    addi    t10, t10, -512
    slli    t10, t10, 2
    add     t10, t2, t10
    flw     ft6, 0(t10)
    fneg.s  ft6, ft6
    fmadd.s ft1, ft5, ft6, ft1

    /* b += window[i+j+16] * synth_buf[i+j-512] */
    addi    t11, t8, 16
    add     t11, t11, t3
    slli    t11, t11, 2
    add     t11, s4, t11
    flw     ft5, 0(t11)

    add     t10, t8, t3
    addi    t10, t10, -512
    slli    t10, t10, 2
    add     t10, t2, t10
    flw     ft6, 0(t10)
    fmadd.s ft2, ft5, ft6, ft2

    /* c += window[i+j+32] * synth_buf[16+i+j-512] */
    addi    t11, t8, 32
    add     t11, t11, t3
    slli    t11, t11, 2
    add     t11, s4, t11
    flw     ft5, 0(t11)

    addi    t10, t8, 16
    add     t10, t10, t3
    addi    t10, t10, -512
    slli    t10, t10, 2
    add     t10, t2, t10
    flw     ft6, 0(t10)
    fmadd.s ft3, ft5, ft6, ft3

    /* d += window[i+j+48] * synth_buf[31-i+j-512] */
    addi    t11, t8, 48
    add     t11, t11, t3
    slli    t11, t11, 2
    add     t11, s4, t11
    flw     ft5, 0(t11)

    li      t10, 31
    sub     t10, t10, t3
    add     t10, t10, t8
    addi    t10, t10, -512
    slli    t10, t10, 2
    add     t10, t2, t10
    flw     ft6, 0(t10)
    fmadd.s ft4, ft5, ft6, ft4

    addi    t8, t8, 64
    j       4b

5:
    /* out[i] = a*scale, out[i+16] = b*scale */
    fmul.s  ft5, ft1, ft0
    add     t9, s5, t4
    fsw     ft5, 0(t9)

    fmul.s  ft5, ft2, ft0
    addi    t10, t3, 16
    slli    t10, t10, 2
    add     t9, s5, t10
    fsw     ft5, 0(t9)

    /* synth_buf2[i] = c; synth_buf2[i+16] = d */
    fsw     ft3, 0(t5)
    fsw     ft4, 0(t6)

    addi    t3, t3, 1
    li      t0, 16
    blt     t3, t0, 1b

    /* *synth_buf_offset = (*synth_buf_offset - 32) & 511; */
    lw      t0, 0(s2)
    addi    t0, t0, -32
    andi    t0, t0, 511
    sw      t0, 0(s2)

    /* Epilogue */
    ld      ra, 56(sp)
    ld      s0, 48(sp)
    ld      s1, 40(sp)
    ld      s2, 32(sp)
    ld      s3, 24(sp)
    ld      s4, 16(sp)
    ld      s5, 8(sp)
    ld      s6, 0(sp)
    addi    sp, sp, 64
    ret

    .size ff_synth_filter_float_rvv, .-ff_synth_filter_float_rvv

#endif /* HAVE_RVV */
