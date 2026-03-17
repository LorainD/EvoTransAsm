.text

.option arch, "+v"

# void ff_synth_filter_float_rvv(AVTXContext *imdct,
#                               float *synth_buf_ptr, int *synth_buf_offset,
#                               float synth_buf2[32], const float window[512],
#                               float out[32], float in[32], float scale,
#                               av_tx_fn imdct_fn)

.globl ff_synth_filter_float_rvv
.type  ff_synth_filter_float_rvv, @function
ff_synth_filter_float_rvv:
    addi sp, sp, -64
    sd   ra, 56(sp)
    sd   s0, 48(sp)
    sd   s1, 40(sp)
    sd   s2, 32(sp)
    sd   s3, 24(sp)
    sd   s4, 16(sp)
    sd   s5, 8(sp)

    # Save frequently used pointers
    mv   s0, a1              # synth_buf_ptr
    mv   s1, a2              # synth_buf_offset ptr
    mv   s2, a3              # synth_buf2
    mv   s3, a4              # window
    mv   s4, a5              # out

    # Call imdct_fn(imdct, synth_buf_ptr + *offset, in, sizeof(float))
    lw   t0, 0(s1)           # offset
    slli t1, t0, 2
    add  t2, s0, t1          # synth_buf = synth_buf_ptr + offset

    mv   a0, a0              # imdct
    mv   a1, t2              # synth_buf
    mv   a2, a6              # in
    li   a3, 4               # sizeof(float)
    jalr zero, a7, 0         # call imdct_fn

    # Reload offset and synth_buf
    lw   t0, 0(s1)
    slli t1, t0, 2
    add  s5, s0, t1          # s5 = synth_buf

    # We'll use f0 = scale
    fmv.s f0, fa0

    li   t3, 0               # i = 0
1:
    # a = synth_buf2[i], b = synth_buf2[i+16]
    slli t4, t3, 2
    add  t5, s2, t4
    flw  f1, 0(t5)           # a
    flw  f2, 64(t5)          # b (16*4)

    # Vector length = 16 (taps)
    li   t6, 16
    vsetivli zero, 16, e32, m1, ta, ma

    # Load 16 window coeffs for w0 = window[16*i + j]
    slli t7, t3, 6           # 16*i*4 = i*64 bytes
    add  t8, s3, t7
    vle32.v v0, (t8)         # v0 = w0[0..15]

    # Load 16 window coeffs for w1 = window[16*i + 256 + j]
    addi t9, t8, 1024        # 256*4
    vle32.v v1, (t9)         # v1 = w1[0..15]

    # Load v0 = synth_buf[2*i + j]
    slli t10, t3, 3          # 2*i*4 = i*8 bytes
    add  t11, s5, t10
    vle32.v v2, (t11)        # v2 = sb[2*i + 0..15]

    # Load v1 = synth_buf[2*i + 32 + j]
    addi t12, t11, 128       # 32*4
    vle32.v v3, (t12)        # v3 = sb[2*i + 32 + 0..15]

    # acc = v2*w0 + v3*w1
    vfmul.vv v4, v2, v0
    vfmacc.vv v4, v3, v1

    # Horizontal sum to scalar c in f3
    vfmv.s.f f3, v4
    vsetivli zero, 8, e32, m1, ta, ma
    vslidedown.vi v5, v4, 8
    vfmv.s.f f4, v5
    fadd.s f3, f3, f4

    vsetivli zero, 4, e32, m1, ta, ma
    vslidedown.vi v5, v4, 4
    vfmv.s.f f4, v5
    fadd.s f3, f3, f4

    vsetivli zero, 2, e32, m1, ta, ma
    vslidedown.vi v5, v4, 2
    vfmv.s.f f4, v5
    fadd.s f3, f3, f4

    vsetivli zero, 1, e32, m1, ta, ma
    vslidedown.vi v5, v4, 1
    vfmv.s.f f4, v5
    fadd.s f3, f3, f4

    # out[i]     = scale*(a + c)
    # out[31-i]  = scale*(a - c)
    fadd.s f5, f1, f3
    fsub.s f6, f1, f3
    fmul.s f5, f5, f0
    fmul.s f6, f6, f0

    add  t13, s4, t4
    fsw  f5, 0(t13)

    li   t14, 31
    sub  t14, t14, t3
    slli t14, t14, 2
    add  t15, s4, t14
    fsw  f6, 0(t15)

    # Update synth_buf2: synth_buf2[i] = b; synth_buf2[i+16] = c
    fsw  f2, 0(t5)
    fsw  f3, 64(t5)

    addi t3, t3, 1
    li   t16, 16
    blt  t3, t16, 1b

    # *synth_buf_offset = (*synth_buf_offset - 32) & 511
    lw   t0, 0(s1)
    addi t0, t0, -32
    andi t0, t0, 511
    sw   t0, 0(s1)

    ld   ra, 56(sp)
    ld   s0, 48(sp)
    ld   s1, 40(sp)
    ld   s2, 32(sp)
    ld   s3, 24(sp)
    ld   s4, 16(sp)
    ld   s5, 8(sp)
    addi sp, sp, 64
    ret
.size ff_synth_filter_float_rvv, .-ff_synth_filter_float_rvv
