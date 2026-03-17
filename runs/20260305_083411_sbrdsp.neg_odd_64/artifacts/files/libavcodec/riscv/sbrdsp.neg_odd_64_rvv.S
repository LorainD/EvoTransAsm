.text
.option arch, +v,+f

.globl ff_sbr_neg_odd_64_rvv
.type  ff_sbr_neg_odd_64_rvv, @function
ff_sbr_neg_odd_64_rvv:
    # a0 = float *x
    li      t0, 64
    slli    t1, t0, 3              # bytes = 64 * (2 floats) * 4 = 512
    add     t2, a0, t1             # end pointer
    li      t3, 0x80000000         # sign bit mask

1:
    bgeu    a0, t2, 2f
    sub     t4, t2, a0             # remaining bytes
    srli    t4, t4, 3              # remaining pairs
    vsetvli t5, t4, e32, m1, ta, ma

    vlseg2e32.v v0, (a0)           # v0=even lanes, v1=odd lanes
    vmv.v.x   v2, t3
    vxor.vv   v1, v1, v2           # flip sign on odd elements
    vsseg2e32.v v0, (a0)

    slli    t6, t5, 3              # pairs * 8 bytes
    add     a0, a0, t6
    j       1b

2:
    ret
.size ff_sbr_neg_odd_64_rvv, .-ff_sbr_neg_odd_64_rvv
