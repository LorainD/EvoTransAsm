/*
 * RISC-V RVV optimized SBR DSP
 *
 * This file is a minimal, buildable stub for RVV.
 * Replace the scalar loop with a true RVV implementation.
 */

#include "libavutil/riscv/asm.S"

#if HAVE_RVV

/*
 * void ff_sbr_neg_odd_64_rvv(float *x);
 * Negate odd-indexed elements in a 128-float buffer.
 */
func ff_sbr_neg_odd_64_rvv
    /* a0 = float *x */
    /* Minimal correct scalar implementation (TODO: RVV vectorize). */
    li      t0, 64
1:
    lw      t1, 4(a0)
    neg     t1, t1
    sw      t1, 4(a0)
    addi    a0, a0, 8
    addi    t0, t0, -1
    bnez    t0, 1b
    ret
endfunc ff_sbr_neg_odd_64_rvv

#endif /* HAVE_RVV */
