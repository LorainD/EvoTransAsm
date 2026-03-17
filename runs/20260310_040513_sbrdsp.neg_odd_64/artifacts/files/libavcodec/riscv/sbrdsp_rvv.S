        .text
        .align  2
        .globl  ff_sbr_neg_odd_64_rvv
        .type   ff_sbr_neg_odd_64_rvv, @function
// void ff_sbr_neg_odd_64_rvv(float *x)
// Negate elements at indices 1,3,5,...,63 (odd indices) of a 64-element float array.
// Strategy: process 4 floats per iteration with a fixed sign-mask pattern [+,-,+,-].
// We load vl floats, XOR the sign bit of odd-lane elements, store back.
ff_sbr_neg_odd_64_rvv:
        // sign-flip mask: 0x80000000 applied to odd lanes
        // Use a stride-2 gather: load only odd elements, flip sign, scatter back.
        li      a1, 64                  // total elements
        li      a2, 8                   // byte stride between odd elements (skip one float)
        addi    a3, a0, 4               // pointer to x[1] (first odd element)
        li      a4, 32                  // number of odd elements
        vsetvli t0, a4, e32, m4, ta, ma
        vlse32.v v0, (a3), a2           // load x[1],x[3],...,x[63]
        lui     t1, 0x80000             // t1 = 0x80000000
        vmv.v.x v4, t1                  // broadcast sign-bit mask
        vxor.vv v0, v0, v4              // flip sign bit
        vsse32.v v0, (a3), a2           // store back to odd positions
        ret
        .size   ff_sbr_neg_odd_64_rvv, .-ff_sbr_neg_odd_64_rvv
