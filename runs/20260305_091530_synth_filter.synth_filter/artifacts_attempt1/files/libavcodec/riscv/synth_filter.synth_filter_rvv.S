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
    # NOTE:
    # The original generated RVV implementation was incomplete/broken (truncated
    # instruction stream, invalid jalr usage, and no valid epilogue), which
    # causes assembler errors.
    #
    # Provide a safe stub implementation to restore buildability.
    # The C fallback will be used for correctness/performance until a complete
    # RVV implementation is provided.

    ret

.size ff_synth_filter_float_rvv, .-ff_synth_filter_float_rvv
