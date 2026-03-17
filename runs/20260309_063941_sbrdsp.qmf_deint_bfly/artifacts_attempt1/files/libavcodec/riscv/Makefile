#
# This file is part of FFmpeg.
#
# Fix: GNU make requires recipe lines to start with a TAB character,
# not spaces. This Makefile intentionally contains no recipe lines;
# it only declares object lists for the riscv subdir.
#

# RISC-V optimized objects
OBJS-$(CONFIG_RV34DSP)                  += riscv/rv34dsp.o
OBJS-$(CONFIG_RV40DSP)                  += riscv/rv40dsp.o

OBJS-$(CONFIG_HEVC_DECODER)             += riscv/hevc_dsp.o
OBJS-$(CONFIG_H264DSP)                  += riscv/h264dsp.o

OBJS-$(CONFIG_AAC_DECODER)              += riscv/aacpsdsp.o
OBJS-$(CONFIG_SBRDSP)                   += riscv/sbrdsp.o

OBJS-$(CONFIG_VP8DSP)                   += riscv/vp8dsp.o
OBJS-$(CONFIG_VP9DSP)                   += riscv/vp9dsp.o
