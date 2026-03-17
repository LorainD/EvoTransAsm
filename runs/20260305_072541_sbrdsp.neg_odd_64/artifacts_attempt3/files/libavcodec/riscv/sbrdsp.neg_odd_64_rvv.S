.text
.option norvc

# void ff_sbr_neg_odd_64_rvv(float *x)

.globl ff_sbr_neg_odd_64_rvv
.type  ff_sbr_neg_odd_64_rvv, @function
ff_sbr_neg_odd_64_rvv:
    # a0 = x
    li      t0, 64              # remaining elements
    mv      t1, a0              # ptr

    # Prepare scalar sign-bit mask (0x80000000) in a vector register
    li      t4, 0x80000000
    vmv.v.x v2, t4

1:
    beqz    t0, 2f
    vsetvli t2, t0, e32, m1, ta, ma

    # Load float vector
    vle32.v v3, (t1)

    # Create lane indices [0..vl-1]
    vid.v   v1
    # (idx & 1)
    vand.vi v1, v1, 1

    # mask for odd lanes
    vmsne.vi v0, v1, 0

    # flip sign bit on odd lanes: x ^= 0x80000000
    vxor.vv v3, v3, v2, v0.t

    vse32.v v3, (t1)

    slli    t3, t2, 2           # bytes processed
    add     t1, t1, t3
    sub     t0, t0, t2
    j       1b
2:
    ret
.size ff_sbr_neg_odd_64_rvv, .-ff_sbr_neg_odd_64_rvv
