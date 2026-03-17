h->pred16x16[VERT_PRED8x8   ] = ff_pred16x16_vertical_8_rvv;
h->pred16x16[HOR_PRED8x8    ] = ff_pred16x16_horizontal_8_rvv;
h->pred16x16[DC_128_PRED8x8 ] = ff_pred16x16_128_dc_8_rvv;