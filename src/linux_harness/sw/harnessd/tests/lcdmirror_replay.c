/*
 * lcdmirror_replay.c -- an 8080 byte stream through the C GRAM model, the
 * aperture out: the shared-vector check of LCD_MIRROR_FPGA.md §8.1 (the C port
 * and the Python golden model on the same recorded streams).
 *
 *   lcdmirror_replay [--ctrl N] [--bulk] IN OUT
 *     IN   {rs, byte} pairs: rs 0 = index, 1 = data, 2 = a clcd_0 CTRL write
 *          (bit 2 RESET_N: 0 holds the panel in reset), 3 = a completed reset
 *          pulse (the KVM's handover sequence)
 *     OUT  the 256 KiB aperture (lcdmirror.h layout) after the stream
 *     --ctrl N   the model's CTRL (bit 0 ac_load, bit 1 flip_conv)
 *     --bulk     feed the bytes through lcdm_model_bytes() in runs of <= 128 --
 *                what the CLCD bulk tap does (lane CLCD-SPEED); the e2e test
 *                replays every shared vector both ways and wants one aperture
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "lcdmirror.h"

static uint32_t s_ap[LCDM_APERTURE / 4u];

int main(int argc, char **argv)
{
    uint32_t ctrl = 0;
    int a = 1, bulk = 0;
    if (a + 1 < argc && strcmp(argv[a], "--ctrl") == 0) {
        ctrl = (uint32_t)strtoul(argv[a + 1], 0, 0);
        a += 2;
    }
    if (a < argc && strcmp(argv[a], "--bulk") == 0) {
        bulk = 1;
        a++;
    }
    if (argc - a != 2) {
        fprintf(stderr, "usage: lcdmirror_replay [--ctrl N] [--bulk] IN OUT\n");
        return 2;
    }
    FILE *in = fopen(argv[a], "rb");
    if (!in) {
        perror(argv[a]);
        return 1;
    }
    lcdm_model_t m;
    lcdm_model_init(&m, s_ap, 0);
    lcdm_model_set_ctrl(&m, ctrl);
    int rs, v;
    uint8_t brs[128], bv[128];
    uint32_t bn = 0;
    while ((rs = fgetc(in)) != EOF && (v = fgetc(in)) != EOF) {
        if (bulk && rs < 2) {
            brs[bn] = (uint8_t)(rs & 1);
            bv[bn] = (uint8_t)v;
            if (++bn == sizeof(brs)) {
                lcdm_model_bytes(&m, brs, bv, bn);
                bn = 0;
            }
            continue;
        }
        if (bn) {
            lcdm_model_bytes(&m, brs, bv, bn);
            bn = 0;
        }
        if (rs == 2) {
            lcdm_model_set_reset(&m, !(v & 0x04));
        } else if (rs == 3) {
            lcdm_model_pulse_reset(&m);
        } else {
            lcdm_model_byte(&m, rs & 1, (uint8_t)v);
        }
    }
    if (bn) {
        lcdm_model_bytes(&m, brs, bv, bn);
    }
    fclose(in);
    lcdm_model_publish(&m);
    FILE *out = fopen(argv[a + 1], "wb");
    if (!out || fwrite(s_ap, 1, sizeof(s_ap), out) != sizeof(s_ap)) {
        perror(argv[a + 1]);
        return 1;
    }
    fclose(out);
    return 0;
}
