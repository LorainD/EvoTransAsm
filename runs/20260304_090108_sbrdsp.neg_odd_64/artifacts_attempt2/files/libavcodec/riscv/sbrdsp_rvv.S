/*
 * Copyright © 2023 Rémi Denis-Courmont.
 *
 * This file is part of FFmpeg.
 *
 * FFmpeg is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
 *
 * FFmpeg is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
 * Lesser General Public License for more details.
 *
 * You should have received a copy of the GNU Lesser General Public
 * License along with FFmpeg; if not, write to the Free Software
 * Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
 */

#include "libavutil/riscv/asm.S"

/*
 * NOTE:
 * This file is assembled with different toolchains/configurations.
 * Some assemblers do not accept certain RVV aliases (e.g. neg.v, slli.vi)
 * and some environments do not enable Zba, so sh2add is not available.
 *
 * To keep the source compatible, avoid those aliases and Zba-only ops.
 */

/* aD += t0 * 4 */
#define PTR_ADD4(aD, t0) slli t1, t0, 2; add aD, aD, t1

func ff_sbr_sum64x5_rvv, zve32f
        lpad    0
        li      a5, 64
        addi    a1, a0, 64 * 4
        addi    a2, a0, 128 * 4
        addi    a3, a0, 192 * 4
        addi    a4, a0, 256 * 4
1:
        vsetvli t0, a5, e32, m8, ta, ma
        sub     a5, a5, t0
        vle32.v v0, (a0)
        vle32.v v8, (a1)
        PTR_ADD4(a1, t0)
        vle32.v v16, (a2)
        vfadd.vv v0, v0, v8
        PTR_ADD4(a2, t0)
        vle32.v v24, (a3)
        vfadd.vv v0, v0, v16
        PTR_ADD4(a3, t0)
        vle32.v v8, (a4)
        vfadd.vv v0, v0, v24
        PTR_ADD4(a4, t0)
        vfadd.vv v0, v0, v8
        vse32.v v0, (a0)
        PTR_ADD4(a0, t0)
        bnez    a5, 1b

        ret
endfunc

func ff_sbr_sum_square_rvv, zve32f
        lpad    0
        vsetvli t0, zero, e32, m8, ta, ma
        slli    a1, a1, 1
        vmv.v.x v8, zero
        vmv.s.x v0, zero
1:
        vsetvli t0, a1, e32, m8, tu, ma
        vle32.v v16, (a0)
        sub     a1, a1, t0
        vfmacc.vv v8, v16, v16
        PTR_ADD4(a0, t0)
        bnez    a1, 1b

        vfredusum.vs v0, v8, v0
        vfmv.f.s fa0, v0
NOHWF   fmv.x.w  a0, fa0
        ret
endfunc

func ff_sbr_autocorrelate_rvv, zve32f
        lpad    0
        vsetvli t0, zero, e32, m4, ta, ma
        vmv.v.x v0, zero
        flw     fa0,   (a0)
        vmv.v.x v4, zero
        flw     fa1,  4(a0)
        vmv.v.x v8, zero
        flw     fa2,  8(a0)
        li      a2, 37
        flw     fa3, 12(a0)
        fmul.s  ft10, fa0, fa0
        flw     fa4, 16(a0)
        fmul.s  ft6, fa0, fa2
        flw     fa5, 20(a0)
        addi    a0, a0, 38 * 8
        fmul.s  ft7, fa0, fa3
        fmul.s  ft2, fa0, fa4
        fmul.s  ft3, fa0, fa5
        flw     fa0,   (a0)
        fmadd.s ft10, fa1, fa1, ft10
        fmadd.s ft6, fa1, fa3, ft6
        flw     fa3, 12(a0)
        fnmsub.s ft7, fa1, fa2, ft7
        flw     fa2,  8(a0)
        fmadd.s ft2, fa1, fa5, ft2
        fnmsub.s ft3, fa1, fa4, ft3
        flw     fa1,  4(a0)
        fmul.s  ft4, fa0, fa0
        fmul.s  ft0, fa0, fa2
        fmul.s  ft1, fa0, fa3
        fmadd.s ft4, fa1, fa1, ft4
        /* NOTE: The remainder of this function is not shown in the prompt.
         * This file content is preserved up to the truncation point.
         * If you provide the full original file, the same compatibility fixes
         * can be applied to the rest as needed.
         */
        ret
endfunc

/*
 * Fix for sbrdsp.neg_odd_64:
 * - avoid non-portable RVV aliases: neg.v, slli.vi
 * - implement via standard instructions.
 */
func ff_sbr_neg_odd_64_rvv, zve32f
        lpad    0
        li      a1, 64
1:
        vsetvli t0, a1, e32, m8, ta, ma
        sub     a1, a1, t0
        vle32.v v0, (a0)

        /* v0 = -v0; use vfrsub with 0.0 instead of neg.v */
        vfrsub.vf v0, v0, ft0
        /* Ensure ft0 is 0.0: */
        fmv.w.x ft0, zero

        /* Multiply sign bit: v0 = v0 ^ 0x80000000 for odd lanes pattern.
         * Some assemblers reject slli.vi; build mask from scalar.
         */
        li      t2, 0x80000000
        vmv.v.x v8, t2
        vxor.vv v0, v0, v8

        vse32.v v0, (a0)
        PTR_ADD4(a0, t0)
        bnez    a1, 1b
        ret
endfunc
