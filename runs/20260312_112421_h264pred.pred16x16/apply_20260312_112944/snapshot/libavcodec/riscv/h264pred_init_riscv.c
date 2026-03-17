
#if HAVE_RVV
void ff_pred16x16_vertical_rvv(uint8_t *src, ptrdiff_t stride);
void ff_pred16x16_horizontal_rvv(uint8_t *src, ptrdiff_t stride);
void ff_pred16x16_plane_rvv(uint8_t *src, ptrdiff_t stride);
void ff_pred16x16_dc_rvv(uint8_t *src, ptrdiff_t stride);
void ff_pred16x16_128_dc_rvv(uint8_t *src, ptrdiff_t stride);
void ff_pred16x16_left_dc_rvv(uint8_t *src, ptrdiff_t stride);
void ff_pred16x16_top_dc_rvv(uint8_t *src, ptrdiff_t stride);
#endif
#if HAVE_RVV
    if (bit_depth == 8 && ff_rvvcpu_has_rvv()) {
        h->pred16x16[VERT_PRED16x16   ] = ff_pred16x16_vertical_rvv;
        h->pred16x16[HOR_PRED16x16    ] = ff_pred16x16_horizontal_rvv;
        h->pred16x16[PLANE_PRED16x16  ] = ff_pred16x16_plane_rvv;
        h->pred16x16[DC_PRED16x16     ] = ff_pred16x16_dc_rvv;
        h->pred16x16[DC_128_PRED16x16 ] = ff_pred16x16_128_dc_rvv;
        h->pred16x16[LEFT_DC_PRED16x16] = ff_pred16x16_left_dc_rvv;
        h->pred16x16[TOP_DC_PRED16x16 ] = ff_pred16x16_top_dc_rvv;
    }
#endif
