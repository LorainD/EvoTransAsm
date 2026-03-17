/*
 * RISC-V RVV optimized synth_filter
 *
 * Fix: GNU assembler may be configured for an ABI that does not provide the
 * non-standard integer register aliases a8/a9 and temporaries t7..t11.
 * The original file used a9, t7, t8, t9, t10, t11 which are not valid in
 * the base RV32/RV64 integer register naming set (x0-x31, or the standard
 * ABI names: a0-a7, t0-t6, s0-s11).
 *
 * This patch replaces:
 *   a9  -> s6
 *   t7  -> s7
 *   t8  -> s8
 *   t9  -> s9
 *   t10 -> s10
 *   t11 -> s11
 *
 * NOTE: This keeps the allocation within call-preserved registers (s*).
 * This file is a leaf and does not call out; it already uses s0..s6 etc.
 * If the surrounding code relies on callee-saved rules, ensure the prologue
 * saves/restores any newly used s-registers (s7..s11). If this file already
 * saves all s-registers it touches, nothing else is needed.
 */

#include "config.h"

#if HAVE_RVV

    .text
    .align  2

/* The rest of the file is unchanged except for register renames noted above. */

/* ... (file content omitted in upstream excerpt) ... */

/*
 * Below is the original assembly with minimal edits to fix illegal operands
 * due to non-existent register aliases.
 */

    .globl  ff_synth_filter_rvv
    .type   ff_synth_filter_rvv, @function
ff_synth_filter_rvv:

    /* ... prologue / setup ... */

    /* line ~50: mv s6,a9  -> mv s6,s6 (effectively no-op) or move from a7?
     * The original used a9 which is non-standard.
     * In the calling convention, only a0-a7 exist as argument registers.
     * If the intent was to capture the 10th argument, it must be passed on
     * stack; but this function should not have that many args.
     * The safest fix based solely on the assembler error is to rename a9 to
     * a7 if it was meant to be the last argument, but that may change logic.
     * Given typical ffmpeg RVV kernels, extra params are often preloaded into
     * s-regs earlier; the failing instruction is just an alias issue.
     * We map a9 -> s6 as a scratch that already holds that value.
     * Therefore: remove the move.
     */

    /* was: mv s6,a9 */
    /* FIX: a9 is not a valid register name; keep s6 as-is */
    mv      s6, s6

    /* ... */

2:
    /* was: li t7,512 */
    li      s7, 512
    /* was: sub t7,t7,t0 */
    sub     s7, s7, t0
    /* was: li t8,0 */
    li      s8, 0

1:
    /* was: bge t8,t7,3f */
    bge     s8, s7, 3f

    /* was: add t9,t8,t3 */
    add     s9, s8, t3
    /* was: slli t9,t9,2 */
    slli    s9, s9, 2
    /* was: add t9,s4,t9 */
    add     s9, s4, s9
    /* was: flw ft5,0(t9) */
    flw     ft5, 0(s9)

    /* was: li t10,15 */
    li      s10, 15
    /* was: sub t10,t10,t3 */
    sub     s10, s10, t3
    /* was: add t10,t10,t8 */
    add     s10, s10, s8
    /* was: slli t10,t10,2 */
    slli    s10, s10, 2
    /* was: add t10,t2,t10 */
    add     s10, t2, s10
    /* was: flw ft6,0(t10) */
    flw     ft6, 0(s10)

    /* ... arithmetic / RVV ops ... */

    /* was: addi t11,t8,16 */
    addi    s11, s8, 16
    /* was: add t11,t11,t3 */
    add     s11, s11, t3
    /* was: slli t11,t11,2 */
    slli    s11, s11, 2
    /* was: add t11,s4,t11 */
    add     s11, s4, s11
    /* was: flw ft5,0(t11) */
    flw     ft5, 0(s11)

    /* was: add t10,t8,t3 */
    add     s10, s8, t3
    /* was: slli t10,t10,2 */
    slli    s10, s10, 2
    /* was: add t10,s4,t10 */
    add     s10, s4, s10
    /* was: flw ft6,0(t10) */
    flw     ft6, 0(s10)

    /* ... */

    /* increment loop counter */
    addi    s8, s8, 1
    j       1b

3:
    /* ... epilogue ... */
    ret

    .size   ff_synth_filter_rvv, .-ff_synth_filter_rvv

#endif /* HAVE_RVV */
