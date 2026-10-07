/*
 * lcdmirror_vectors.c -- the 6940 WIRE VECTORS (Harness Manager's H1): the exact
 * bytes the board's builders (lcdmirror_enc.c, the code mps3-lcdmirror sends
 * with) produce for a HELLO, one UPDATE per tile encoding, a keyframe split
 * across three UPDATEs, and the two refusal lines.
 *
 *   lcdmirror_vectors OUTDIR
 *
 * Checked in under tests/fixtures/lcdmirror_wire/ with manifest.json, which
 * says what each file must decode to -- as CRC-32s of the SOURCE pixels (the
 * frame before encoding), so a decoder is judged on the picture, not on these
 * bytes. tests/test_lcdmirror_e2e.py regenerates them (the builders must not
 * drift from the fixtures) and decodes them with the reference client.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "lcdmirror.h"

static uint32_t crc32_le(const void *p, size_t n)
{
    const uint8_t *b = (const uint8_t *)p;
    uint32_t c = 0xFFFFFFFFu;
    for (size_t i = 0; i < n; i++) {
        c ^= b[i];
        for (int k = 0; k < 8; k++) c = (c >> 1) ^ (0xEDB88320u & (0u - (c & 1u)));
    }
    return ~c;
}

static char s_dir[1024];
static FILE *s_man;
static int s_first = 1;

static void put_file(const char *name, const void *p, size_t n)
{
    char path[1200];
    snprintf(path, sizeof(path), "%s/%s", s_dir, name);
    FILE *f = fopen(path, "wb");
    if (!f || fwrite(p, 1, n, f) != n) {
        perror(path);
        exit(1);
    }
    fclose(f);
}

static uint32_t tile_crc(const uint16_t *frame, unsigned t)
{
    uint16_t px[LCDM_TILE_PX];
    uint8_t le[512];
    lcdm_tile_get(frame, t, px);
    for (unsigned i = 0; i < 256u; i++) { le[2 * i] = (uint8_t)px[i]; le[2 * i + 1] = (uint8_t)(px[i] >> 8); }
    return crc32_le(le, sizeof(le));
}

static uint32_t frame_crc(const uint16_t *frame)
{
    static uint8_t le[LCDM_NPX * 2u];
    for (unsigned i = 0; i < LCDM_NPX; i++) { le[2 * i] = (uint8_t)frame[i]; le[2 * i + 1] = (uint8_t)(frame[i] >> 8); }
    return crc32_le(le, sizeof(le));
}

static void man_begin(const char *file, const char *kind)
{
    fprintf(s_man, "%s\n  {\"file\": \"%s\", \"kind\": \"%s\"", s_first ? "" : ",", file, kind);
    s_first = 0;
}

static void hdr_for(lcdm_snaphdr_t *h, const uint32_t valid[LCDM_MAP_WORDS], int key)
{
    memset(h, 0, sizeof(*h));
    h->t_ms = 123456u;
    h->frames = 77u;
    h->resets = 1u;
    /* rst_n bl display_on fmt_ok + text_only: a sw-mode harness screen */
    h->status = LCDM_ST_RST_N | LCDM_ST_BL | LCDM_ST_DISPLAY_ON | LCDM_ST_FMT_OK | LCDM_S_TEXT_ONLY;
    h->owner = LCDM_OWNER_HARNESS;
    for (unsigned i = 0; i < LCDM_MAP_BYTES; i++) h->valid[i] = (uint8_t)(valid[i >> 2] >> (8u * (i & 3u)));
    for (unsigned i = 0; i < 256u; i++) h->regs[i] = key ? (uint8_t)(i * 7u) : 0u;
    h->regs[0x16] = 0x20; h->regs[0x17] = 0x05; h->regs[0x36] = 0x09; h->regs[0x28] = 0x3C;
    h->mode = 0x20u | (0x05u << 8) | (0x09u << 16);
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: lcdmirror_vectors OUTDIR\n");
        return 2;
    }
    snprintf(s_dir, sizeof(s_dir), "%s", argv[1]);
    char mp[1200];
    snprintf(mp, sizeof(mp), "%s/manifest.json", s_dir);
    s_man = fopen(mp, "w");
    if (!s_man) { perror(mp); return 1; }
    fprintf(s_man, "{\"what\": \"LCD mirror 6940 wire vectors (lcdmirror_vectors.c)\", \"vectors\": [");

    static uint8_t msg[LCDM_MAX_MSG];
    static uint8_t recs[LCDM_NTILES * LCDM_REC_MAX];
    static uint16_t frame[LCDM_NPX];

    /* 1. HELLO */
    size_t n = lcdm_build_hello(msg, sizeof(msg), 0x5A5A0001u, LCDM_MODE_SW, LCDM_MAX_MSG,
                                "00000000-0000-0000-0000-000000000000", LCDM_RATE_DEFAULT);
    put_file("hello.bin", msg, n);
    man_begin("hello.bin", "hello");
    fprintf(s_man, "}");

    /* 2. one UPDATE per encoding: a single tile each, non-key */
    static const struct { const char *file; unsigned tile; uint8_t enc; } one[] = {
        { "update_fill.bin", 0u, LCDM_ENC_FILL },  { "update_pal1.bin", 21u, LCDM_ENC_PAL1 },
        { "update_pal2.bin", 42u, LCDM_ENC_PAL2 }, { "update_rle16.bin", 63u, LCDM_ENC_RLE16 },
        { "update_raw.bin", 299u, LCDM_ENC_RAW },
    };
    for (unsigned k = 0; k < 5u; k++) {
        memset(frame, 0, sizeof(frame));
        unsigned t = one[k].tile, x0 = (t % 20u) * 16u, y0 = (t / 20u) * 16u;
        for (unsigned i = 0; i < 256u; i++) {
            unsigned x = i % 16u, y = i / 16u;
            uint16_t v;
            switch (one[k].enc) {
            case LCDM_ENC_FILL:  v = 0xF800u; break;                                    /* red */
            case LCDM_ENC_PAL1:  v = ((x * 3u + y * 5u) % 7u < 3u) ? 0xFFFFu : 0x0000u; break;
            case LCDM_ENC_PAL2:  v = (uint16_t)(0x001Fu << (((x + 2u * y) % 3u) * 5u)); break;
            case LCDM_ENC_RLE16: v = i < 7u ? (uint16_t)(0x1000u + i) : (i < 200u ? 0x07E0u : 0x001Fu); break;
            default:             v = (uint16_t)(i * 0x0101u + 3u); break;               /* 256 distinct */
            }
            frame[(y0 + y) * LCDM_W + x0 + x] = v;
        }
        uint32_t tiles[LCDM_MAP_WORDS] = { 0 };
        tiles[t >> 5] |= 1u << (t & 31u);
        unsigned nt = 0;
        size_t rl = lcdm_encode_records(frame, tiles, recs, &nt);
        if (recs[2] != one[k].enc) {
            fprintf(stderr, "%s: encoded as %u, not %u\n", one[k].file, recs[2], one[k].enc);
            return 1;
        }
        uint32_t valid[LCDM_MAP_WORDS];
        memset(valid, 0xFF, sizeof(valid));
        valid[9] = 0xFFFu;
        lcdm_snaphdr_t h;
        hdr_for(&h, valid, 0);
        n = lcdm_build_update(msg, 10u + k, &h, LCDM_S_SNAP_LAST, recs, rl, nt);
        put_file(one[k].file, msg, n);
        man_begin(one[k].file, "update");
        fprintf(s_man, ", \"seq\": %u, \"t_ms\": %u, \"status\": %u, \"owner\": 0, \"tiles\": "
                       "[[%u, %u, %u, %u]]}", 10u + k, h.t_ms, h.status | LCDM_S_SNAP_LAST, t,
                one[k].enc, (unsigned)(rl - 5u), (unsigned)tile_crc(frame, t));
    }

    /* 3. a keyframe of noise (incompressible: 300 RAW tiles) split across UPDATEs */
    uint32_t x = 0x2545F491u;
    for (unsigned i = 0; i < LCDM_NPX; i++) {
        x ^= x << 13; x ^= x >> 17; x ^= x << 5;
        frame[i] = (uint16_t)x;
    }
    uint32_t all[LCDM_MAP_WORDS];
    memset(all, 0xFF, sizeof(all));
    all[9] = 0xFFFu;
    unsigned nt = 0;
    size_t rl = lcdm_encode_records(frame, all, recs, &nt);
    lcdm_snaphdr_t h;
    hdr_for(&h, all, 1);
    size_t off = 0, budget = lcdm_part_budget(LCDM_MAX_MSG, 1);
    unsigned part = 0;
    char names[8][32];
    while (off < rl) {
        unsigned k = 0;
        size_t end = lcdm_next_part(recs, rl, off, budget, &k);
        int last = end >= rl;
        uint32_t flags = LCDM_S_KEY | (part == 0 ? LCDM_S_KEY_FIRST : 0u) |
                         (last ? (LCDM_S_KEY_LAST | LCDM_S_SNAP_LAST) : 0u);
        n = lcdm_build_update(msg, 100u + part, &h, flags, recs + off, end - off, k);
        if (n > LCDM_MAX_MSG) { fprintf(stderr, "part %u is %zu B > max_msg\n", part, n); return 1; }
        snprintf(names[part], sizeof(names[part]), "key_split_%u.bin", part + 1u);
        put_file(names[part], msg, n);
        off = end;
        part++;
    }
    man_begin("key_split_*.bin", "keyframe");
    fprintf(s_man, ", \"parts\": [");
    for (unsigned p = 0; p < part; p++) fprintf(s_man, "%s\"%s\"", p ? ", " : "", names[p]);
    fprintf(s_man, "], \"seq_first\": 100, \"valid_count\": 300, \"frame_crc\": %u}",
            (unsigned)frame_crc(frame));

    /* 4. the refusal lines */
    char line[128];
    n = lcdm_refusal_line(line, sizeof(line), LCDM_REFUSE_BUSY);
    put_file("refusal_busy.txt", line, n);
    man_begin("refusal_busy.txt", "refusal");
    fprintf(s_man, "}");
    n = lcdm_refusal_line(line, sizeof(line), LCDM_REFUSE_NOT_LOOPBACK);
    put_file("refusal_not_loopback.txt", line, n);
    man_begin("refusal_not_loopback.txt", "refusal");
    fprintf(s_man, "}\n]}\n");
    fclose(s_man);
    printf("lcdmirror_vectors: %u keyframe parts, %zu record bytes\n", part, rl);
    return 0;
}
