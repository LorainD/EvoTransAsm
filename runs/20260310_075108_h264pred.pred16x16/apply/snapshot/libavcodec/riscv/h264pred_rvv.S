.text
.align 2

.globl ff_pred16x16_vertical_8_rvv
.type  ff_pred16x16_vertical_8_rvv, @function
ff_pred16x16_vertical_8_rvv:
    addi    a2, a0, -1
    sub     a2, a2, a1

    li      t0, 16
    vsetvli t0, t0, e8, m1, ta, ma
    vle8.v  v0, (a2)

    li      t1, 16
1:
    vse8.v  v0, (a0)
    add     a0, a0, a1
    addi    t1, t1, -1
    bnez    t1, 1b
    ret
.size ff_pred16x16_vertical_8_rvv, .-ff_pred16x16_vertical_8_rvv

.globl ff_pred16x16_horizontal_8_rvv
.type  ff_pred16x16_horizontal_8_rvv, @function
ff_pred16x16_horizontal_8_rvv:
    li      t0, 16
    vsetvli t0, t0, e8, m1, ta, ma

    li      t1, 16
1:
    lbu     t2, -1(a0)
    vmv.v.x v0, t2
    vse8.v  v0, (a0)
    add     a0, a0, a1
    addi    t1, t1, -1
    bnez    t1, 1b
    ret
.size ff_pred16x16_horizontal_8_rvv, .-ff_pred16x16_horizontal_8_rvv

.globl ff_pred16x16_128_dc_8_rvv
.type  ff_pred16x16_128_dc_8_rvv, @function
ff_pred16x16_128_dc_8_rvv:
    li      t0, 16
    vsetvli t0, t0, e8, m1, ta, ma
    li      t2, 128
    vmv.v.x v0, t2

    li      t1, 16
1:
    vse8.v  v0, (a0)
    add     a0, a0, a1
    addi    t1, t1, -1
    bnez    t1, 1b
    ret
.size ff_pred16x16_128_dc_8_rvv, .-ff_pred16x16_128_dc_8_rvv

.globl ff_pred16x16_top_dc_8_rvv
.type  ff_pred16x16_top_dc_8_rvv, @function
ff_pred16x16_top_dc_8_rvv:
    addi    a2, a0, -1
    sub     a2, a2, a1

    li      t0, 16
    vsetvli t0, t0, e8, m1, ta, ma
    vle8.v  v0, (a2)
    vwcvtu.x.x.v v1, v0
    vsetvli t0, t0, e16, m2, ta, ma
    vredsum.vs v2, v1, vzero
    vmv.x.s t3, v2

    addi    t3, t3, 8
    srli    t3, t3, 4

    li      t0, 16
    vsetvli t0, t0, e8, m1, ta, ma
    vmv.v.x v0, t3

    li      t1, 16
1:
    vse8.v  v0, (a0)
    add     a0, a0, a1
    addi    t1, t1, -1
    bnez    t1, 1b
    ret
.size ff_pred16x16_top_dc_8_rvv, .-ff_pred16x16_top_dc_8_rvv

.globl ff_pred16x16_left_dc_8_rvv
.type  ff_pred16x16_left_dc_8_rvv, @function
ff_pred16x16_left_dc_8_rvv:
    mv      t6, a0
    li      t0, 16
    vsetvli t0, t0, e16, m2, ta, ma
    vmv.v.i v1, 0

    li      t1, 16
1:
    lbu     t2, -1(t6)
    vsetvli t0, t0, e16, m2, ta, ma
    vadd.vx v1, v1, t2
    add     t6, t6, a1
    addi    t1, t1, -1
    bnez    t1, 1b

    vredsum.vs v2, v1, vzero
    vmv.x.s t3, v2
    addi    t3, t3, 8
    srli    t3, t3, 4

    li      t0, 16
    vsetvli t0, t0, e8, m1, ta, ma
    vmv.v.x v0, t3

    li      t1, 16
2:
    vse8.v  v0, (a0)
    add     a0, a0, a1
    addi    t1, t1, -1
    bnez    t1, 2b
    ret
.size ff_pred16x16_left_dc_8_rvv, .-ff_pred16x16_left_dc_8_rvv

.globl ff_pred16x16_dc_8_rvv
.type  ff_pred16x16_dc_8_rvv, @function
ff_pred16x16_dc_8_rvv:
    addi    a2, a0, -1
    sub     a2, a2, a1

    li      t0, 16
    vsetvli t0, t0, e8, m1, ta, ma
    vle8.v  v0, (a2)
    vwcvtu.x.x.v v1, v0
    vsetvli t0, t0, e16, m2, ta, ma
    vredsum.vs v2, v1, vzero
    vmv.x.s t3, v2

    mv      t6, a0
    li      t0, 16
    vsetvli t0, t0, e16, m2, ta, ma
    vmv.v.i v4, 0

    li      t1, 16
1:
    lbu     t2, -1(t6)
    vsetvli t0, t0, e16, m2, ta, ma
    vadd.vx v4, v4, t2
    add     t6, t6, a1
    addi    t1, t1, -1
    bnez    t1, 1b

    vredsum.vs v5, v4, vzero
    vmv.x.s t4, v5

    add     t3, t3, t4
    addi    t3, t3, 16
    srli    t3, t3, 5

    li      t0, 16
    vsetvli t0, t0, e8, m1, ta, ma
    vmv.v.x v0, t3

    li      t1, 16
2:
    vse8.v  v0, (a0)
    add     a0, a0, a1
    addi    t1, t1, -1
    bnez    t1, 2b
    ret
.size ff_pred16x16_dc_8_rvv, .-ff_pred16x16_dc_8_rvv
