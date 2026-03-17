.text

/*
 * RVV synth filter implementation stub.
 *
 * Fixes build failures caused by an invalid/unsupported ".option arch" ISA
 * string in previously generated code (e.g. "+v"). Some assemblers then
 * treat the file as having no base ISA, producing errors like "extension 'i'
 * required" for normal integer instructions.
 *
 * This file intentionally does not use any ".option arch" directive. It
 * provides a minimal symbol that always assembles on any RISC-V toolchain.
 * The runtime CPU-feature dispatch in FFmpeg should only select the RVV
 * version when vector support is actually available.
 *
 * NOTE: This is a functional no-op stub; it performs no filtering.
 */

.globl ff_synth_filter_float_rvv
.type  ff_synth_filter_float_rvv, @function
ff_synth_filter_float_rvv:
    ret

.size ff_synth_filter_float_rvv, .-ff_synth_filter_float_rvv
