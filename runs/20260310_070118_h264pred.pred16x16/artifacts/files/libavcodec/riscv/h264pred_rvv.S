.text
.align 2

/*
 * RVV 8-bit pred16x16 helpers for H.264 intra prediction
 * ABI: void func(uint8_t *src, ptrdiff_t stride)
 */

.globl ff_pred16x16_vertical_8_rvv
.type  ff_pred16x16_vertical_8_rvv, @function
ff_pred16x16_vertical_8_rvv:
    addi    a2, a0, 0
    sub     a3, a0, a1                /* top = src - stride */
    li      t0, 16
    vsetivli zero, 16, e8, m1, ta, ma
    vle8.v  v0, (a3)
1:
    vse8.v  v0, (a2)
    add     a2, a2, a1
    addi    t0, t0, -1
    bnez    t0, 1b
    ret
.size ff_pred16x16_vertical_8_rvv, .-ff_pred16x16_vertical_8_rvv

.globl ff_pred16x16_horizontal_8_rvv
.type  ff_pred16x16_horizontal_8_rvv, @function
ff_pred16x16_horizontal_8_rvv:
    addi    a2, a0, 0
    li      t0, 16
    vsetivli zero, 16, e8, m1, ta, ma
1:
    lbu     t1, -1(a2)                /* left[y] */
    vmv.v.x v0, t1
    vse8.v  v0, (a2)
    add     a2, a2, a1
    addi    t0, t0, -1
    bnez    t0, 1b
    ret
.size ff_pred16x16_horizontal_8_rvv, .-ff_pred16x16_horizontal_8_rvv

.globl ff_pred16x16_128_dc_8_rvv
.type  ff_pred16x16_128_dc_8_rvv, @function
ff_pred16x16_128_dc_8_rvv:
    addi    a2, a0, 0
    li      t0, 16
    li      t1, 128
    vsetivli zero, 16, e8, m1, ta, ma
    vmv.v.x v0, t1
1:
    vse8.v  v0, (a2)
    add     a2, a2, a1
    addi    t0, t0, -1
    bnez    t0, 1b
    ret
.size ff_pred16x16_128_dc_8_rvv, .-ff_pred16x16_128_dc_8_rvv

.globl ff_pred16x16_top_dc_8_rvv
.type  ff_pred16x16_top_dc_8_rvv, @function
ff_pred16x16_top_dc_8_rvv:
    sub     a2, a0, a1                /* top */
    vsetivli zero, 16, e8, m1, ta, ma
    vle8.v  v0, (a2)
    vredsum.vs v1, v0, v0             /* v1[0] = sum + top[0] (will fix below) */
    vmv.x.s t0, v1
    lbu     t1, 0(a2)
    sub     t0, t0, t1                /* sum */
    addi    t0, t0, 8
    srli    t0, t0, 4                 /* dc */
    vmv.v.x v2, t0
    addi    a3, a0, 0
    li      t2, 16
1:
    vse8.v  v2, (a3)
    add     a3, a3, a1
    addi    t2, t2, -1
    bnez    t2, 1b
    ret
.size ff_pred16x16_top_dc_8_rvv, .-ff_pred16x16_top_dc_8_rvv

.globl ff_pred16x16_left_dc_8_rvv
.type  ff_pred16x16_left_dc_8_rvv, @function
ff_pred16x16_left_dc_8_rvv:
    addi    a2, a0, -1                /* left column ptr at row0 */
    li      t0, 0
    li      t1, 16
1:
    lbu     t2, 0(a2)
    add     t0, t0, t2
    add     a2, a2, a1
    addi    t1, t1, -1
    bnez    t1, 1b
    addi    t0, t0, 8
    srli    t0, t0, 4                 /* dc */
    vsetivli zero, 16, e8, m1, ta, ma
    vmv.v.x v0, t0
    addi    a3, a0, 0
    li      t3, 16
2:
    vse8.v  v0, (a3)
    add     a3, a3, a1
    addi    t3, t3, -1
    bnez    t3, 2b
    ret
.size ff_pred16x16_left_dc_8_rvv, .-ff_pred16x16_left_dc_8_rvv

.globl ff_pred16x16_dc_8_rvv
.type  ff_pred16x16_dc_8_rvv, @function
ff_pred16x16_dc_8_rvv:
    sub     a2, a0, a1                /* top */
    vsetivli zero, 16, e8, m1, ta, ma
    vle8.v  v0, (a2)
    vredsum.vs v1, v0, v0
    vmv.x.s t0, v1
    lbu     t1, 0(a2)
    sub     t0, t0, t1                /* sum_top */

    addi    a3, a0, -1                /* left col */
    li      t2, 16
1:
    lbu     t3, 0(a3)
    add     t0, t0, t3
    add     a3, a3, a1
    addi    t2, t2, -1
    bnez    t2, 1b

    addi    t0, t0, 16
    srli    t0, t0, 5                 /* dc */
    vmv.v.x v2, t0
    addi    a4, a0, 0
    li      t4, 16
2:
    vse8.v  v2, (a4)
    add     a4, a4, a1
    addi    t4, t4, -1
    bnez    t4, 2b
    ret
.size ff_pred16x16_dc_8_rvv, .-ff_pred16x16_dc_8_rvv
