.text
.align 2

.globl ff_sbr_qmf_deint_bfly_rvv
.type  ff_sbr_qmf_deint_bfly_rvv, @function
ff_sbr_qmf_deint_bfly_rvv:
    mv      t0, a0                  # v
    mv      t1, a1                  # src0
    mv      t2, a2                  # src1
    li      t3, 64                  # n
1:
    beqz    t3, 2f
    vsetvli t4, t3, e32, m1, ta, ma
    vle32.v v0, (t1)
    vle32.v v1, (t2)
    vfadd.vv v2, v0, v1
    vfsub.vv v3, v0, v1
    vsseg2e32.v v2, (t0)
    slli    t5, t4, 2               # bytes = vl*4
    add     t1, t1, t5
    add     t2, t2, t5
    slli    t6, t4, 3               # bytes = vl*8 (2 floats per i)
    add     t0, t0, t6
    sub     t3, t3, t4
    j       1b
2:
    ret
.size ff_sbr_qmf_deint_bfly_rvv, .-ff_sbr_qmf_deint_bfly_rvv
