
/*
 * H.264 16x16 intra prediction (8-bit) RVV optimizations
 *
 * This file is part of FFmpeg.
 */

#include "config.h"

#if HAVE_RVV

#include "libavutil/riscv/asm.S"

/*
 * C prototypes (as used by H264PredContext::pred16x16):
 *   void ff_pred16x16_<mode>_rvv(uint8_t *src, ptrdiff_t stride);
 *
 * Edges are located relative to src like the generic C code:
 *   top  = src - stride
 *   left = src - 1 (for each row: src[y*stride - 1])
 */

.macro pred16x16_store_rows vreg
    .set r, 0
    .rept 16
        vse8.v \vreg, (a0)
        add     a0, a0, a1
        .set r, r+1
    .endr
.endm

/* ------------------------------------------------------------ */

func ff_pred16x16_vertical_rvv
    /* a0=src, a1=stride */
    vsetivli t0, 16, e8, m1, ta, ma
    sub     t1, a0, a1              /* t1 = top row pointer */
    vle8.v  v0, (t1)                /* v0 = top[0..15] */
    pred16x16_store_rows v0
    ret
endfunc

func ff_pred16x16_horizontal_rvv
    /* a0=src, a1=stride */
    vsetivli t0, 16, e8, m1, ta, ma
    mv      t2, a0                  /* row ptr */
    li      t3, 16
1:
    lbu     t1, -1(t2)              /* left[y] */
    vmv.v.x v0, t1                  /* splat */
    vse8.v  v0, (t2)
    add     t2, t2, a1
    addi    t3, t3, -1
    bnez    t3, 1b
    ret
endfunc

/* Sum 16 bytes at address in t1 into a 32-bit scalar return in t2.
 * Clobbers v0,v1.
 */
.macro sum16_u8_to_x t1, t2
    vsetivli t0, 16, e8, m1, ta, ma
    vle8.v    v0, (\t1)
    vwaddu.vx v1, v0, zero          /* u16 widen */
    vsetivli t0, 16, e16, m1, ta, ma
    vwredsumu.vs v0, v1, v0         /* reduce into v0[0] (u16) */
    vmv.x.s   \t2, v0
.endm

func ff_pred16x16_top_dc_rvv
    /* dc = (sum(top[0..15]) + 8) >> 4 */
    sub     t1, a0, a1
    sum16_u8_to_x t1, t2
    addi    t2, t2, 8
    srli    t2, t2, 4
    vsetivli t0, 16, e8, m1, ta, ma
    vmv.v.x v0, t2
    pred16x16_store_rows v0
    ret
endfunc

func ff_pred16x16_left_dc_rvv
    /* sum left[0..15] */
    /* load left bytes via strided scalar loop (16 iters) */
    mv      t3, a0
    li      t2, 0
    li      t4, 16
1:
    lbu     t1, -1(t3)
    add     t2, t2, t1
    add     t3, t3, a1
    addi    t4, t4, -1
    bnez    t4, 1b
    addi    t2, t2, 8
    srli    t2, t2, 4
    vsetivli t0, 16, e8, m1, ta, ma
    vmv.v.x v0, t2
    pred16x16_store_rows v0
    ret
endfunc

func ff_pred16x16_dc_rvv
    /* dc = (sum(top)+sum(left) + 16) >> 5 */
    sub     t1, a0, a1
    sum16_u8_to_x t1, t2            /* t2 = sum(top) */

    mv      t3, a0
    li      t5, 0                   /* t5 = sum(left) */
    li      t4, 16
1:
    lbu     t1, -1(t3)
    add     t5, t5, t1
    add     t3, t3, a1
    addi    t4, t4, -1
    bnez    t4, 1b

    add     t2, t2, t5
    addi    t2, t2, 16
    srli    t2, t2, 5
    vsetivli t0, 16, e8, m1, ta, ma
    vmv.v.x v0, t2
    pred16x16_store_rows v0
    ret
endfunc

func ff_pred16x16_128_dc_rvv
    vsetivli t0, 16, e8, m1, ta, ma
    li      t1, 128
    vmv.v.x v0, t1
    pred16x16_store_rows v0
    ret
endfunc

/* PLANE: scalar reference-style for H/V and coefficients, RVV for row stores.
 * clip to [0,255].
 */
func ff_pred16x16_plane_rvv
    /* a0=src, a1=stride */
    /* Compute H */
    sub     t0, a0, a1              /* top ptr */
    li      t2, 0                   /* H */
    li      t3, 1
1:
    /* top[7+i] - top[7-i] */
    add     t4, t0, t3
    addi    t4, t4, 7
    lbu     t5, 0(t4)               /* top[7+i] */
    addi    t6, t0, 7
    sub     t6, t6, t3
    lbu     t7, 0(t6)               /* top[7-i] */
    sub     t5, t5, t7
    mul     t5, t5, t3
    add     t2, t2, t5
    addi    t3, t3, 1
    li      t1, 9
    blt     t3, t1, 1b

    /* Compute V */
    li      t8, 0                   /* V */
    mv      t9, a0                  /* row ptr */
    li      t3, 1
2:
    /* left[7+i] - left[7-i] */
    /* address of row (7+i): a0 + (7+i)*stride - 1 */
    li      t1, 7
    add     t1, t1, t3
    mul     t4, t1, a1
    add     t4, a0, t4
    lbu     t5, -1(t4)
    /* row (7-i) */
    li      t1, 7
    sub     t1, t1, t3
    mul     t6, t1, a1
    add     t6, a0, t6
    lbu     t7, -1(t6)
    sub     t5, t5, t7
    mul     t5, t5, t3
    add     t8, t8, t5
    addi    t3, t3, 1
    li      t1, 9
    blt     t3, t1, 2b

    /* a = 16*(left[15] + top[15]) */
    /* left[15] */
    li      t1, 15
    mul     t4, t1, a1
    add     t4, a0, t4
    lbu     t5, -1(t4)
    /* top[15] */
    addi    t6, t0, 15
    lbu     t7, 0(t6)
    add     t5, t5, t7
    slli    t5, t5, 4               /* a in t5 */

    /* b = (5*H + 32) >> 6 ; c = (5*V + 32) >> 6 */
    li      t1, 5
    mul     t2, t2, t1
    addi    t2, t2, 32
    srai    t2, t2, 6               /* b */
    mul     t8, t8, t1
    addi    t8, t8, 32
    srai    t8, t8, 6               /* c */

    /* for y=0..15, x=0..15:
     * val = (a + b*(x-7) + c*(y-7) + 16) >> 5
     */
    vsetivli t0, 16, e16, m1, ta, ma
    /* Prepare (x-7) vector in v1 (i16): [ -7..8 ] */
    vid.v   v1
    li      t1, 7
    vsub.vx v1, v1, t1

    mv      t9, a0                  /* dst row ptr */
    li      t3, 0                   /* y */
3:
    addi    t4, t3, -7              /* (y-7) */
    mul     t6, t8, t4              /* c*(y-7) */

    /* base = a + c*(y-7) + 16 */
    add     t6, t6, t5
    addi    t6, t6, 16

    /* v2 = b*(x-7) */
    vmul.vx v2, v1, t2
    /* v2 = base + v2 */
    vadd.vx v2, v2, t6
    /* >>5 */
    vsra.vi v2, v2, 5

    /* clip to [0,255] and store as u8 */
    vsetivli t0, 16, e16, m1, ta, ma
    vmax.vx v2, v2, zero
    li      t1, 255
    vmin.vx v2, v2, t1
    vsetivli t0, 16, e8, m1, ta, ma
    vnclipu.wi v0, v2, 0
    vse8.v  v0, (t9)

    add     t9, t9, a1
    addi    t3, t3, 1
    li      t1, 16
    blt     t3, t1, 3b

    ret
endfunc

#endif /* HAVE_RVV */
