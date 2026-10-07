/*
 * lcdmirror_enc.c -- the 6940 tile codec and the wire's message builders
 * (LCD_MIRROR_FPGA.md §6.2; byte layout in net-protocol.md "LCD mirror (TCP
 * 6940)"). Pure functions: the server (lcdmirror_main.c), the C tests and the
 * wire-vector generator (tests/lcdmirror_vectors.c) all call these.
 *
 * A tile is 16x16 pixels, row-major, x fastest. Every pixel on the wire is
 * RGB565 LITTLE-endian (amendment 1). Encodings (Harness Manager's reading,
 * adopted byte for byte, so its encoder and this one emit identical records):
 *   FILL   2 B   one colour
 *   PAL1  36 B   colours c0 c1, then 16 u16 rows: bit x of row y set = c1
 *   PAL2  72 B   colours c0..c3 (a 3-colour tile repeats c0 as c3), then 16 u32
 *                rows: pixel x = bits [2x+1:2x], the palette index
 *   RLE16 var.   PackBits over u16 pixels, row-major across the whole tile:
 *                0x80|(n-1) then ONE u16 = a run of n; n-1 then n u16 = literals;
 *                1 <= n <= 128. Greedy: a repeat of >= 2 is a run.
 *   RAW  512 B   256 colours
 * The choice: FILL for one colour; otherwise RLE16 if shorter than 512 bytes
 * (else RAW); then PAL1 (exactly 2 colours) or PAL2 (3 or 4) replaces it only
 * if STRICTLY shorter. Palette entries are in order of first appearance.
 */
#include <stdio.h>
#include <string.h>

#include "lcdmirror.h"

int lcdm_mutation;   /* LCDM_MUT_*; only a host test build ever sets it */

static void put16(uint8_t *p, uint16_t v)
{
    if (lcdm_mutation == LCDM_MUT_PX_ENDIAN) {
        p[0] = (uint8_t)(v >> 8);
        p[1] = (uint8_t)v;
    } else {
        p[0] = (uint8_t)v;
        p[1] = (uint8_t)(v >> 8);
    }
}

static void put16w(uint8_t *p, uint16_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static void put32w(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24);
}

static uint16_t get16(const uint8_t *p)
{
    return (uint16_t)(p[0] | ((unsigned)p[1] << 8));
}

void lcdm_tile_get(const volatile uint16_t *frame, unsigned t, uint16_t px[LCDM_TILE_PX])
{
    unsigned x0 = (t % LCDM_TX) * LCDM_TILE, y0 = (t / LCDM_TX) * LCDM_TILE;
    for (unsigned yy = 0; yy < LCDM_TILE; yy++) {
        const volatile uint16_t *row = frame + (y0 + yy) * LCDM_W + x0;
        for (unsigned xx = 0; xx < LCDM_TILE; xx++) {
            px[yy * LCDM_TILE + xx] = row[xx];
        }
    }
}

/* PackBits over u16 (HM's _rle_py, line for line). out may be NULL (length only). */
static unsigned packbits(const uint16_t *a, uint8_t *out)
{
    unsigned o = 0, i = 0, n = LCDM_TILE_PX;
    while (i < n) {
        unsigned j = i + 1u;
        while (j < n && a[j] == a[i] && j - i < 128u) j++;
        if (j - i >= 2u) {
            if (out) {
                unsigned hdr = 0x80u | (j - i - 1u);
                if (lcdm_mutation == LCDM_MUT_RLE_LEN) hdr = 0x80u | ((j - i) & 0x7Fu);
                out[o] = (uint8_t)hdr;
                put16(out + o + 1u, a[i]);
            }
            o += 3u;
            i = j;
        } else {
            unsigned s = i, k = i;
            while (k < n && k - s < 128u) {
                if (k + 1u < n && a[k + 1u] == a[k]) break;
                k++;
            }
            if (k == s) k = s + 1u;
            if (out) {
                out[o] = (uint8_t)(k - s - 1u);
                for (unsigned q = s; q < k; q++) put16(out + o + 1u + 2u * (q - s), a[q]);
            }
            o += 1u + 2u * (k - s);
            i = k;
        }
    }
    return o;
}

unsigned lcdm_encode_tile(const uint16_t px[LCDM_TILE_PX], uint8_t *out, uint8_t *enc)
{
    uint16_t pal[4];
    unsigned npal = 0;
    for (unsigned i = 0; i < LCDM_TILE_PX && npal <= 4u; i++) {
        unsigned k = 0;
        while (k < npal && pal[k] != px[i]) k++;
        if (k == npal) {
            if (npal == 4u) { npal = 5u; break; }   /* more than PAL2 can carry */
            pal[npal++] = px[i];
        }
    }
    if (npal == 1u) {
        *enc = LCDM_ENC_FILL;
        put16(out, pal[0]);
        return 2u;
    }
    unsigned best = LCDM_ENC_RAW, best_len = 512u;
    unsigned rle = packbits(px, 0);
    if (rle < 512u) { best = LCDM_ENC_RLE16; best_len = rle; }
    if (npal == 2u && 36u < best_len)                 { best = LCDM_ENC_PAL1; best_len = 36u; }
    else if (npal >= 3u && npal <= 4u && 72u < best_len) { best = LCDM_ENC_PAL2; best_len = 72u; }
    *enc = (uint8_t)best;
    switch (best) {
    case LCDM_ENC_PAL1:
        put16(out, pal[0]);
        put16(out + 2, pal[1]);
        for (unsigned y = 0; y < 16u; y++) {
            unsigned row = 0;
            for (unsigned x = 0; x < 16u; x++) {
                if (px[y * 16u + x] == pal[1]) {
                    row |= 1u << ((lcdm_mutation == LCDM_MUT_PAL_BITS) ? 15u - x : x);
                }
            }
            put16w(out + 4 + 2u * y, (uint16_t)row);
        }
        return 36u;
    case LCDM_ENC_PAL2:
        for (unsigned k = npal; k < 4u; k++) pal[k] = pal[0];
        for (unsigned k = 0; k < 4u; k++) put16(out + 2u * k, pal[k]);
        for (unsigned y = 0; y < 16u; y++) {
            uint32_t row = 0;
            for (unsigned x = 0; x < 16u; x++) {
                unsigned k = 0;
                while (pal[k] != px[y * 16u + x]) k++;
                row |= (uint32_t)k << (2u * x);
            }
            put32w(out + 8 + 4u * y, row);
        }
        return 72u;
    case LCDM_ENC_RLE16:
        return packbits(px, out);
    default:
        for (unsigned i = 0; i < LCDM_TILE_PX; i++) put16(out + 2u * i, px[i]);
        return 512u;
    }
}

int lcdm_decode_tile(uint8_t enc, const uint8_t *in, unsigned len, uint16_t px[LCDM_TILE_PX])
{
    switch (enc) {
    case LCDM_ENC_FILL:
        if (len != 2u) return -1;
        for (unsigned i = 0; i < LCDM_TILE_PX; i++) px[i] = get16(in);
        return 0;
    case LCDM_ENC_PAL1:
        if (len != 36u) return -1;
        for (unsigned y = 0; y < 16u; y++) {
            unsigned row = get16(in + 4 + 2u * y);
            for (unsigned x = 0; x < 16u; x++) px[y * 16u + x] = get16(in + 2u * ((row >> x) & 1u));
        }
        return 0;
    case LCDM_ENC_PAL2:
        if (len != 72u) return -1;
        for (unsigned y = 0; y < 16u; y++) {
            const uint8_t *r = in + 8 + 4u * y;
            uint32_t row = (uint32_t)r[0] | ((uint32_t)r[1] << 8) | ((uint32_t)r[2] << 16) |
                           ((uint32_t)r[3] << 24);
            for (unsigned x = 0; x < 16u; x++) {
                px[y * 16u + x] = get16(in + 2u * ((row >> (2u * x)) & 3u));
            }
        }
        return 0;
    case LCDM_ENC_RLE16: {
        unsigned o = 0, i = 0;
        while (i < len) {
            unsigned t = in[i++];
            if (t & 0x80u) {
                unsigned n = (t & 0x7Fu) + 1u;
                if (i + 2u > len || o + n > LCDM_TILE_PX) return -1;
                uint16_t c = get16(in + i);
                i += 2u;
                while (n--) px[o++] = c;
            } else {
                unsigned n = t + 1u;
                if (i + 2u * n > len || o + n > LCDM_TILE_PX) return -1;
                for (unsigned q = 0; q < n; q++) px[o++] = get16(in + i + 2u * q);
                i += 2u * n;
            }
        }
        return o == LCDM_TILE_PX ? 0 : -1;
    }
    case LCDM_ENC_RAW:
        if (len != 512u) return -1;
        for (unsigned i = 0; i < LCDM_TILE_PX; i++) px[i] = get16(in + 2u * i);
        return 0;
    default:
        return -1;
    }
}

/* ---- the wire ---------------------------------------------------------------- */
void lcdm_put_hdr(uint8_t *out, uint8_t type, uint32_t len)
{
    out[0] = 'L';
    out[1] = 'M';
    out[2] = type;
    out[3] = 0u;
    put32w(out + 4, len);
}

size_t lcdm_encode_records(const uint16_t *frame, const uint32_t tiles[LCDM_MAP_WORDS],
                           uint8_t *out, unsigned *ntiles)
{
    size_t o = 0;
    unsigned n = 0;
    uint16_t px[LCDM_TILE_PX];
    for (unsigned t = 0; t < LCDM_NTILES; t++) {
        if (!((tiles[t >> 5] >> (t & 31u)) & 1u)) continue;
        lcdm_tile_get(frame, t, px);
        uint8_t enc = 0;
        unsigned len = lcdm_encode_tile(px, out + o + 5u, &enc);
        unsigned idx = (lcdm_mutation == LCDM_MUT_TILE_IDX) ? (t + 1u) % LCDM_NTILES : t;
        put16w(out + o, (uint16_t)idx);
        out[o + 2] = enc;
        put16w(out + o + 3, (uint16_t)len);
        o += 5u + len;
        n++;
    }
    *ntiles = n;
    return o;
}

size_t lcdm_part_budget(unsigned max_msg, int key)
{
    return (size_t)max_msg - (LCDM_HDR + LCDM_UPD_FIXED + (key ? 256u : 4u) + 2u);
}

size_t lcdm_next_part(const uint8_t *recs, size_t len, size_t start, size_t budget, unsigned *n)
{
    size_t i = start;
    unsigned k = 0;
    while (i < len) {
        size_t size = 5u + get16(recs + i + 3u);
        if (i + size - start > budget && k) break;
        i += size;
        k++;
    }
    *n = k;
    return i;
}

size_t lcdm_build_update(uint8_t *out, uint32_t seq, const lcdm_snaphdr_t *h, uint32_t flags,
                         const uint8_t *recs, size_t rlen, unsigned ntiles)
{
    uint8_t *b = out + LCDM_HDR;
    size_t o = 0;
    put32w(b + o, seq);                  o += 4;
    put32w(b + o, h->t_ms);              o += 4;
    put32w(b + o, h->frames);            o += 4;
    put32w(b + o, h->resets);            o += 4;
    put32w(b + o, h->status | flags);    o += 4;
    b[o++] = h->owner;
    memcpy(b + o, h->valid, LCDM_MAP_BYTES);
    o += LCDM_MAP_BYTES;
    if (flags & LCDM_S_KEY_FIRST) {
        memcpy(b + o, h->regs, 256);
        o += 256;
    } else {
        put32w(b + o, h->mode);
        o += 4;
    }
    put16w(b + o, (uint16_t)ntiles);     o += 2;
    if (rlen) memcpy(b + o, recs, rlen);
    o += rlen;
    lcdm_put_hdr(out, LCDM_MSG_UPDATE, (uint32_t)o);
    return LCDM_HDR + o;
}

size_t lcdm_build_hello(uint8_t *out, size_t cap, uint32_t static_id, unsigned mode,
                        unsigned max_msg, const char *boot_id, unsigned rate)
{
    char esc[64];
    size_t e = 0;
    for (const char *p = boot_id ? boot_id : ""; *p && e + 1u < sizeof(esc); p++) {
        char c = *p;
        esc[e++] = (c == '"' || c == '\\' || (unsigned char)c < 0x20u) ? '?' : c;
    }
    esc[e] = '\0';
    int n = snprintf((char *)out + LCDM_HDR, cap > LCDM_HDR ? cap - LCDM_HDR : 0,
                     "{\"proto\":%u,\"w\":%u,\"h\":%u,\"fmt\":\"rgb565le\",\"tile\":%u,"
                     "\"mode\":\"%s\",\"static_id\":\"0x%08x\",\"max_msg\":%u,"
                     "\"boot_id\":\"%s\",\"rate\":%u,\"rate_max\":%u,\"clients_max\":%u}",
                     LCDM_PROTO, LCDM_W, LCDM_H, LCDM_TILE, mode == LCDM_MODE_HW ? "hw" : "sw",
                     (unsigned)static_id, max_msg, esc, rate, LCDM_RATE_MAX, LCDM_MAX_CLIENTS);
    if (n < 0 || (size_t)n + LCDM_HDR > cap) return 0;
    lcdm_put_hdr(out, LCDM_MSG_HELLO, (uint32_t)n);
    return LCDM_HDR + (size_t)n;
}

size_t lcdm_refusal_line(char *out, size_t cap, const char *why)
{
    int n = snprintf(out, cap, "{\"ok\":false,\"err\":\"lcd_mirror: %s\"}\n", why);
    return (n < 0 || (size_t)n >= cap) ? 0u : (size_t)n;
}
