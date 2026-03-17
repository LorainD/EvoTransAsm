/*
 * Copyright © 2024 RISC-V Vector Extension Implementation
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

.text

func ff_opus_deemphasis_rvv, export=1
        flw     fa1, (a3)               # c = weights[0]
        fmv.s   fa0, fa2                # coeff

1:
        vsetvli t0, a4, e32, m8, ta, ma
        vle32.v v8, (a1)                # load x[i]
        sub     a4, a4, t0
        vfmul.vf v16, v8, fa1           # x[i] * c
        sh2add  a1, t0, a1
        vfadd.vf v16, v16, fa0          # x[i] * c + coeff
        vfmv.f.s fa0, v16               # update coeff from v16[0]
        vse32.v v16, (a0)               # store y[i]
        sh2add  a0, t0, a0
        bnez    a4, 1b

        ret
endfunc
