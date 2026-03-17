h->pred16x16[VERT_PRED8x8   ] = ff_pred16x16_vertical_8_rvv;
h->pred16x16[HOR_PRED8x8    ] = ff_pred16x16_horizontal_8_rvv;
h->pred16x16[DC_128_PRED8x8 ] = ff_pred16x16_128_dc_8_rvv;
#include "libavcodec/h264pred.h"
#include "config.h"

void ff_h264pred_init_riscv(H264PredContext *h, int codec_id, const int bit_depth)
{
    if (bit_depth != 8)
        return;

#if HAVE_RVV
    h->pred16x16[VERT_PRED8x8   ] = ff_pred16x16_vertical_8_rvv;
    h->pred16x16[HOR_PRED8x8    ] = ff_pred16x16_horizontal_8_rvv;
    h->pred16x16[DC_PRED8x8     ] = ff_pred16x16_dc_8_rvv;
    h->pred16x16[TOP_DC_PRED8x8 ] = ff_pred16x16_top_dc_8_rvv;
    h->pred16x16[LEFT_DC_PRED8x8] = ff_pred16x16_left_dc_8_rvv;
    h->pred16x16[DC_128_PRED8x8 ] = ff_pred16x16_128_dc_8_rvv;
#endif
}
