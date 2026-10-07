/*
 * test_lcdmirror.c -- the LCD mirror's model and codec, board-free
 * (LCD_MIRROR_FPGA.md §2 / §6.2 / §7 / §8).
 *
 *   1. SYNTHETIC VECTORS through the GRAM model (lcdmirror_model.c): a window
 *      fill + the address-counter wrap + FRAMES, all EIGHT MADCTL geometries
 *      against §2.4 transcribed independently below (0x20 = identity, 0xE0 =
 *      180 degrees), both flip conventions, 18 bpp truncation + STATUS.approx,
 *      an unsupported COLMOD, a split 0x22, both ac_load conventions, an
 *      out-of-range window (OOB), a mid-stream panel reset (defaults, VALID
 *      cleared, pixels kept, bytes ignored while held), the register file, and
 *      the dirty-map hand-off (publish ORs, SNAP exchanges, a later write marks
 *      the tile again).
 *   2. THE REAL RENDERER: firmware/clcd/clcd.c + the real init table, driven
 *      through mock registers, every CMD/DATA byte into the model; the model's
 *      frame must equal, PIXEL FOR PIXEL, the font8x16 rendering of clcd.c's
 *      own cell shadow (clcd_test_shadow) -- boot screen, a link-up repaint,
 *      the DUT banner, the apps page. Negative control: the same stream with
 *      MADCTL patched to 0xE0 must NOT match, and must match the oracle rotated
 *      by 180 degrees.
 *   2b. THE ALIGNED THEME (Harness Manager CLCD_ALIGNMENT R4): the same real
 *      renderer switched to clcd_theme_aligned -- HM's palette, the status glyphs
 *      0x80-0x86 -- must still reach the model pixel-exactly: the frame equals a
 *      ROLE-aware oracle (cells + clcd.c's role plane, drawn here from the fonts
 *      and HM's CLCD_PALETTE_INIT), on the status page, the apps page and the
 *      DUT OSD. Negative control: the white/black/red oracle must NOT match it.
 *   3. THE CODEC round trip on every encoding, the smallest-encoding choice,
 *      and the mutation controls (a broken bit order / run length / pixel byte
 *      order MUST fail the round trip).
 *   4. COST: the boot stream replayed through the model in harnessd-sized
 *      passes (256 bytes + one publish): ns/byte and us/pass, asserted.
 *
 * `test_lcdmirror --dump-stream PATH` also writes the boot stream as
 * {rs,byte} pairs (rs 2 = a CLCD CTRL write) for the golden-model cross-check.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "../../../../../firmware/clcd/clcd.h"
#include "../../../../../firmware/clcd/font8x16.h"
#include "../../../../../firmware/clcd/clcd_glyphs.h"
#include "../../../../../firmware/common/platform_regs.h"
#include "../../../../../firmware/common/diag.h"
#include "../../../../../firmware/coordinator/coordinator.h"
#include "../../../../../firmware/coordinator/swap_fsm.h"
#include "../../../../../firmware/smsc911x/smsc911x.h"
#include "../../../../../firmware/test/mock_regs.h"
#include "lcdmirror.h"

static int s_checks, s_fails;
#define CHECK(c) do { s_checks++; if (!(c)) { s_fails++; \
    fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #c); } } while (0)

static uint32_t s_ap[LCDM_APERTURE / 4u] __attribute__((aligned(64)));
static lcdm_model_t M;

#define APW(off) (s_ap[(off) / 4u])
static uint16_t fbpx(unsigned x, unsigned y)
{
    return ((const uint16_t *)((const uint8_t *)s_ap + LCDM_FB))[y * LCDM_W + x];
}
static const uint16_t *fb(void) { return (const uint16_t *)((const uint8_t *)s_ap + LCDM_FB); }

/* ---- bus helpers ------------------------------------------------------------ */
static void cmd(uint8_t i) { lcdm_model_byte(&M, 0, i); }
static void dat(uint8_t d) { lcdm_model_byte(&M, 1, d); }
static void reg(uint8_t i, uint8_t d) { cmd(i); dat(d); }
static void px16(uint16_t v) { dat((uint8_t)(v >> 8)); dat((uint8_t)v); }
static void window(unsigned sc, unsigned ec, unsigned sp, unsigned ep)
{
    reg(0x02, (uint8_t)(sc >> 8)); reg(0x03, (uint8_t)sc);
    reg(0x04, (uint8_t)(ec >> 8)); reg(0x05, (uint8_t)ec);
    reg(0x06, (uint8_t)(sp >> 8)); reg(0x07, (uint8_t)sp);
    reg(0x08, (uint8_t)(ep >> 8)); reg(0x09, (uint8_t)ep);
}
static void fresh(void)
{
    memset(s_ap, 0, sizeof(s_ap));
    lcdm_model_init(&M, s_ap, 0);
    reg(0x17, 0x05);            /* RGB565 */
    reg(0x16, 0x20);            /* the board-proven MADCTL: identity */
}
static uint16_t h16(unsigned x, unsigned y) { return (uint16_t)((x * 40503u) ^ (y * 2654435761u >> 7) ^ 0x5A5Au); }

/* §2.4, transcribed from the doc text (flip_conv 0), with O2's other reading. */
static int doc_map(unsigned mad, int conv, unsigned x, unsigned y, unsigned *vx, unsigned *vy)
{
    int mv = (mad & 0x20) != 0, mx = (mad & 0x40) != 0, my = (mad & 0x80) != 0;
    unsigned g0 = mv ? x : y, s0 = mv ? y : x;
    if (g0 >= 320u || s0 >= 240u) return 0;
    int fg = my, fs = mx;
    if (conv && mv) { fg = mx; fs = my; }
    *vx = fg ? 319u - g0 : g0;
    *vy = fs ? 239u - s0 : s0;
    return 1;
}

/* ==========================================================================
 * 1. synthetic vectors
 * ========================================================================== */
static void t_window_fill_wrap_frames(void)
{
    fresh();
    window(16, 31, 32, 35);             /* 16 x 4 at (16,32) */
    cmd(0x22);
    for (unsigned i = 0; i < 16u * 4u * 2u + 3u; i++) {   /* two full windows + 3 */
        px16((uint16_t)(0x1000u + i));
    }
    lcdm_model_publish(&M);
    /* the second pass overwrote the first; 3 more wrapped to the start */
    CHECK(fbpx(16, 32) == 0x1000u + 128u);
    CHECK(fbpx(18, 32) == 0x1000u + 130u);
    CHECK(fbpx(19, 32) == 0x1000u + 64u + 3u);
    CHECK(fbpx(31, 35) == 0x1000u + 127u);
    CHECK(fbpx(15, 32) == 0u && fbpx(32, 32) == 0u && fbpx(16, 36) == 0u);
    CHECK(APW(LCDM_FRAMES) == 2u);
    CHECK(APW(LCDM_SEQ) == 131u);
    CHECK(APW(LCDM_RAMWR) == 1u);
    CHECK(APW(LCDM_WIN_X) == ((31u << 16) | 16u) && APW(LCDM_WIN_Y) == ((35u << 16) | 32u));
    CHECK(APW(LCDM_AC) == ((32u << 16) | 19u));
    /* tiles (1,2) and (1,2)... x 16..31 = tile col 1, y 32..35 = tile row 2 */
    unsigned t = 2u * 20u + 1u;
    CHECK(APW(LCDM_VALID + 4u * (t >> 5)) == (1u << (t & 31u)));
    CHECK((APW(LCDM_STATUS) & LCDM_ST_IN_GRAM) != 0u);
    CHECK((APW(LCDM_STATUS) & (LCDM_ST_FMT_OK | LCDM_ST_APPROX)) == LCDM_ST_FMT_OK);
}

static void t_eight_geometries(void)
{
    static uint16_t want[LCDM_NPX];
    for (int conv = 0; conv < 2; conv++) {
        for (unsigned mad = 0; mad < 0x100u; mad += 0x20u) {
            fresh();
            lcdm_model_set_ctrl(&M, conv ? LCDM_CTRL_FLIP_CONV : 0u);
            reg(0x16, (uint8_t)mad);
            unsigned lw = (mad & 0x20u) ? 320u : 240u, lh = (mad & 0x20u) ? 240u : 320u;
            window(0, lw - 1u, 0, lh - 1u);
            cmd(0x22);
            memset(want, 0, sizeof(want));
            for (unsigned y = 0; y < lh; y++) {
                for (unsigned x = 0; x < lw; x++) {
                    unsigned vx = 0, vy = 0;
                    px16(h16(x, y));
                    if (doc_map(mad, conv, x, y, &vx, &vy)) want[vy * 320u + vx] = h16(x, y);
                }
            }
            CHECK(memcmp(fb(), want, sizeof(want)) == 0);
            CHECK(APW(LCDM_OOB) == 0u && M.frames == 1u);
        }
    }
    /* the two anchors, stated semantically */
    fresh();
    window(0, 319, 0, 239);
    cmd(0x22);
    px16(0xABCD);
    CHECK(fbpx(0, 0) == 0xABCDu);                        /* 0x20: identity */
    fresh();
    reg(0x16, 0xE0);
    window(0, 319, 0, 239);
    cmd(0x22);
    px16(0xABCD);
    CHECK(fbpx(319, 239) == 0xABCDu);                    /* 0xE0: 180 degrees */
    /* O2: the conventions differ ONLY for a single flip under MV */
    fresh();
    reg(0x16, 0x60);                                     /* MV|MX */
    window(0, 319, 0, 239);
    cmd(0x22);
    px16(0x1111);
    CHECK(fbpx(0, 239) == 0x1111u);                      /* conv 0: MX flips s (vy) */
    fresh();
    lcdm_model_set_ctrl(&M, LCDM_CTRL_FLIP_CONV);
    reg(0x16, 0x60);
    window(0, 319, 0, 239);
    cmd(0x22);
    px16(0x2222);
    CHECK(fbpx(319, 0) == 0x2222u);                      /* conv 1: MX flips g (vx) */
}

static void t_formats(void)
{
    fresh();
    reg(0x17, 0x06);                                     /* 18 bpp */
    window(0, 319, 0, 239);
    cmd(0x22);
    dat(0xFC); dat(0x80); dat(0x08);                     /* R=63 G=32 B=1 (6 bits, MSB-aligned) */
    lcdm_model_publish(&M);
    CHECK(fbpx(0, 0) == (uint16_t)((0xFCu >> 3) << 11 | (0x80u >> 2) << 5 | (0x08u >> 3)));
    CHECK((APW(LCDM_STATUS) & (LCDM_ST_FMT_OK | LCDM_ST_APPROX)) == (LCDM_ST_FMT_OK | LCDM_ST_APPROX));
    reg(0x17, 0x03);                                     /* not a format this panel has */
    lcdm_model_publish(&M);
    CHECK((APW(LCDM_STATUS) & LCDM_ST_FMT_OK) == 0u);
    CHECK(((APW(LCDM_MODE) >> 8) & 0xFFu) == 0x03u);
}

static void t_split_ramwr_and_ac_load(void)
{
    fresh();
    window(0, 319, 0, 239);
    cmd(0x22);
    dat(0x12);                                           /* half a pixel...        */
    cmd(0x22);                                           /* ...discarded by an index */
    px16(0x3456);
    CHECK(fbpx(0, 0) == 0x3456u && M.seq == 1u);
    /* ac_load = 0 (reset): a start-register write loads AC, 0x22 does not */
    window(100, 109, 50, 59);
    px16(0x7777);                                        /* still streaming (idx 0x22 is gone) */
    cmd(0x22);
    px16(0x1111);
    CHECK(fbpx(100, 50) == 0x1111u);
    px16(0x2222);                                        /* AC advanced */
    reg(0x03, 104);                                      /* AC.x <- 104, at once */
    cmd(0x22);
    px16(0x3333);
    CHECK(fbpx(101, 50) == 0x2222u && fbpx(104, 50) == 0x3333u);
    /* ac_load = 1: registers do not move AC; 0x22 loads it from (SC, SP) */
    fresh();
    lcdm_model_set_ctrl(&M, LCDM_CTRL_AC_LOAD);
    window(10, 19, 10, 19);
    cmd(0x22);
    px16(0x4444);
    px16(0x5555);
    reg(0x03, 15);                                       /* SC <- 15, AC untouched */
    cmd(0x2C);                                           /* some other index */
    cmd(0x22);                                           /* AC <- (15, 10) */
    px16(0x6666);
    CHECK(fbpx(10, 10) == 0x4444u && fbpx(11, 10) == 0x5555u && fbpx(15, 10) == 0x6666u);
}

static void t_oob(void)
{
    fresh();
    window(318, 321, 0, 0);                              /* runs off the right edge */
    cmd(0x22);
    for (int i = 0; i < 4; i++) px16((uint16_t)(0xA000u + (unsigned)i));
    lcdm_model_publish(&M);
    CHECK(fbpx(318, 0) == 0xA000u && fbpx(319, 0) == 0xA001u);
    CHECK(APW(LCDM_OOB) == 2u && (APW(LCDM_STATUS) & LCDM_ST_OOB));
    CHECK(APW(LCDM_SEQ) == 2u);                          /* SEQ counts WRITTEN pixels */
    lcdm_model_set_ctrl(&M, LCDM_CTRL_CLR_STICKY);
    lcdm_model_publish(&M);
    CHECK(!(APW(LCDM_STATUS) & LCDM_ST_OOB));
}

static void t_reset(void)
{
    fresh();
    window(0, 15, 0, 15);
    cmd(0x22);
    for (int i = 0; i < 256; i++) px16(0xBEEF);
    reg(0x28, 0x3C);
    lcdm_model_publish(&M);
    CHECK(APW(LCDM_VALID) & 1u);
    CHECK(APW(LCDM_STATUS) & LCDM_ST_DISPLAY_ON);
    uint32_t resets = APW(LCDM_RESETS);
    lcdm_model_set_reset(&M, 1);
    reg(0x16, 0xE0);                                     /* ignored: held in reset */
    cmd(0x22);
    px16(0x0001);
    lcdm_model_publish(&M);
    CHECK(APW(LCDM_RESETS) == resets + 1u);
    CHECK(APW(LCDM_VALID) == 0u);                        /* VALID cleared...        */
    CHECK(fbpx(0, 0) == 0xBEEFu && fbpx(15, 15) == 0xBEEFu);   /* ...pixels kept    */
    CHECK(!(APW(LCDM_STATUS) & LCDM_ST_RST_N));
    lcdm_model_set_reset(&M, 0);
    lcdm_model_publish(&M);
    /* defaults (O3, the golden model's): portrait window, MADCTL 0, COLMOD 0x06,
     * display off, R1F STB=1 -- and the raw REGS log survives the reset */
    CHECK(APW(LCDM_WIN_X) == ((239u << 16) | 0u) && APW(LCDM_WIN_Y) == ((319u << 16) | 0u));
    CHECK(APW(LCDM_MODE) == 0x00000600u);
    CHECK(!(APW(LCDM_STATUS) & LCDM_ST_DISPLAY_ON) && (APW(LCDM_STATUS) & LCDM_ST_RST_N));
    CHECK(APW(LCDM_STATUS) & LCDM_ST_STANDBY);
    CHECK(APW(LCDM_AC) == 0u);
    CHECK(((const uint8_t *)s_ap)[LCDM_REGS + 0x28] == 0x3C);
    CHECK(((const uint8_t *)s_ap)[LCDM_REGS + 0x16] == 0x20);
    CHECK(APW(LCDM_BYTES) == M.bytes && M.bytes > 0u);
    lcdm_model_pulse_reset(&M);
    CHECK(M.resets == resets + 2u);
}

static void t_regs_and_dirty_handoff(void)
{
    fresh();
    reg(0x36, 0x09);
    reg(0x01, 0x00);
    reg(0x1F, 0x91);                                     /* STB = 1 */
    reg(0xE8, 0x40);
    lcdm_model_publish(&M);
    const uint8_t *r = (const uint8_t *)s_ap + LCDM_REGS;
    CHECK(r[0x36] == 0x09 && r[0xE8] == 0x40 && r[0x16] == 0x20 && r[0x17] == 0x05);
    CHECK(APW(LCDM_MODE) == (0x20u | (0x05u << 8) | (0x09u << 16)));
    CHECK(APW(LCDM_STATUS) & LCDM_ST_STANDBY);
    CHECK(APW(LCDM_ID) == LCDM_ID_VALUE && APW(LCDM_GEOM) == ((240u << 16) | 320u));
    /* dirty: publish ORs the tiles, SNAP (an exchange) takes them, a write after
     * the exchange marks the tile again -- the §2.7 convergence rule */
    window(40, 40, 20, 20);                              /* tile (2,1) = 22 */
    cmd(0x22);
    px16(0x1234);
    lcdm_model_publish(&M);
    uint32_t live = __atomic_exchange_n(&s_ap[LCDM_SW_LIVE / 4u], 0u, __ATOMIC_ACQ_REL);
    CHECK(live == (1u << 22));
    CHECK(s_ap[LCDM_SW_LIVE / 4u] == 0u);
    px16(0x5678);                                        /* AC wrapped: (40,20) again */
    lcdm_model_publish(&M);
    CHECK(s_ap[LCDM_SW_LIVE / 4u] == (1u << 22));
    /* nothing new: a publish is a no-op (does not bump PUBS) */
    uint32_t pubs = APW(LCDM_SW_PUBS);
    lcdm_model_publish(&M);
    CHECK(APW(LCDM_SW_PUBS) == pubs);
    CHECK((APW(LCDM_SW_PUBSEQ) & 1u) == 0u);
}

/* ==========================================================================
 * 2. the real renderer, through mock registers
 * ========================================================================== */
mps3_shell_state_t   g_shell_state;
volatile mps3_diag_t g_mps3_diag;
static mps3_swap_result_t s_swap_res;
const mps3_swap_result_t *swap_fsm_last_result(void) { return &s_swap_res; }
uint32_t swap_fsm_icap_bytes(void) { return 0u; }
static int s_link_up;
int smsc911x_link_up(void) { return s_link_up; }
int smsc911x_mii_read(uint32_t r, uint16_t *v) { if (v) *v = (r == 5u) ? 0x01E1u : 0u; return 0; }
void mps3_platform_mac(uint8_t mac[6]) { static const uint8_t m[6] = { 2, 0, 0, 0x4D, 0x50, 0x53 }; memcpy(mac, m, 6); }
/* The palette seam, as mps3-harnessd supplies it: today's theme for section 2
 * (so its oracle and streams are unchanged), the aligned one for section 2b. */
static const mps3_clcd_theme_t *s_theme = &clcd_theme_today;
const mps3_clcd_theme_t *mps3_clcd_palette(void) { return s_theme; }

static struct { uint8_t *rs, *v; size_t n, cap; int on; } S;
static void rec(uint8_t rs, uint8_t v)
{
    if (!S.on) return;
    if (S.n == S.cap) {
        S.cap = S.cap ? 2u * S.cap : 65536u;
        S.rs = realloc(S.rs, S.cap);
        S.v = realloc(S.v, S.cap);
    }
    S.rs[S.n] = rs;
    S.v[S.n] = v;
    S.n++;
}

static int clcd_hook(void *ctx, int is_write, uint32_t base, uint32_t off, uint32_t *val)
{
    (void)ctx; (void)base;
    if (!is_write) {
        *val = (off == CLCD_STATUS) ? CLCD_STATUS_FIFO_EMPTY : 0u;   /* never full */
        return 1;
    }
    if (off == CLCD_CMD || off == CLCD_DATA) {
        rec(off == CLCD_DATA, (uint8_t)*val);
        lcdm_model_byte(&M, off == CLCD_DATA, (uint8_t)*val);
        return 1;
    }
    if (off == CLCD_CTRL) {
        /* no KVM in this build: clcd_0's CTRL drives the panel's RST pad */
        rec(2, (uint8_t)*val);
        lcdm_model_set_reset(&M, !(*val & CLCD_CTRL_RESET_N));
        return 1;
    }
    return 0;
}

static void oracle(uint16_t *out)
{
    char cells[CLCD_NCELLS];
    uint8_t inv[CLCD_ROWS];
    clcd_test_shadow(cells, inv);
    for (unsigned r = 0; r < CLCD_ROWS; r++) {
        for (unsigned c = 0; c < CLCD_COLS; c++) {
            unsigned ch = (unsigned char)cells[r * CLCD_COLS + c];
            if (ch < CLCD_FONT_FIRST || ch > CLCD_FONT_LAST) ch = ' ';
            const uint8_t *g = font8x16[ch - CLCD_FONT_FIRST];
            for (unsigned yy = 0; yy < 16u; yy++) {
                for (unsigned xx = 0; xx < 8u; xx++) {
                    out[(r * 16u + yy) * 320u + c * 8u + xx] =
                        (g[yy] & (0x80u >> xx)) ? 0xFFFFu : (inv[r] ? 0xF800u : 0x0000u);
                }
            }
        }
    }
}

/* Poll until clcd.c has pushed something AND sits IDLE with nothing dirty on
 * two consecutive passes (a pending forced refresh runs on the first of them). */
static int run_until_drawn(void)
{
    uint32_t b0 = clcd_test_bytes_total();
    int quiet = 0;
    for (int i = 0; i < 400000; i++) {
        clcd_poll();
        mock_time_advance_ms(1);
        int idle = clcd_test_state() == CLCD_ST_IDLE && clcd_test_dirty_count() == 0u;
        quiet = idle ? quiet + 1 : 0;
        if (quiet >= 2 && clcd_test_bytes_total() != b0) {
            return 1;
        }
    }
    return 0;
}

static unsigned diff_px(const uint16_t *a, const uint16_t *b)
{
    unsigned n = 0;
    for (unsigned i = 0; i < LCDM_NPX; i++) n += a[i] != b[i];
    return n;
}

static int map_full(void)
{
    for (unsigned w = 0; w < LCDM_MAP_WORDS; w++) {
        uint32_t want = (w == 9u) ? 0x00000FFFu : 0xFFFFFFFFu;   /* 300 = 9*32 + 12 */
        if (APW(LCDM_VALID + 4u * w) != want) return 0;
    }
    return 1;
}

static uint16_t s_want[LCDM_NPX], s_boot_want[LCDM_NPX], s_today[LCDM_NPX];

static void t_real_renderer(const char *dump)
{
    memset(s_ap, 0, sizeof(s_ap));
    lcdm_model_init(&M, s_ap, 0);
    mock_regs_reset();
    mock_regs_set_hook(MPS3_CLCD_BASE, clcd_hook, 0);
    memset(&g_shell_state, 0, sizeof(g_shell_state));
    g_shell_state.static_id = 0x5A5A0001u;
    s_link_up = 0;                                       /* the red NETWORK LINK DOWN rows */
    S.on = 1;
    clcd_init();
    CHECK(run_until_drawn());
    S.on = 0;
    lcdm_model_publish(&M);
    oracle(s_want);
    unsigned d = diff_px(fb(), s_want);
    printf("  boot screen: %zu bus bytes, %u px differ from the font oracle\n", S.n, d);
    CHECK(d == 0u);
    CHECK(map_full());
    CHECK((APW(LCDM_MODE) & 0xFFFFu) == 0x0520u);        /* MADCTL 0x20, COLMOD 0x05 */
    memcpy(s_boot_want, s_want, sizeof(s_want));
    CHECK(APW(LCDM_STATUS) & LCDM_ST_DISPLAY_ON);
    if (dump) {
        FILE *f = fopen(dump, "wb");
        CHECK(f != 0);
        if (f) {
            for (size_t i = 0; i < S.n; i++) { fputc(S.rs[i], f); fputc(S.v[i], f); }
            fclose(f);
        }
    }

    /* incremental repaints: link up (banner rows go), the OSD banner, apps page */
    const char *what[3] = { "link-up repaint", "DUT banner", "apps page" };
    for (int k = 0; k < 3; k++) {
        if (k == 0) { s_link_up = 1; clcd_test_force_reformat(); }
        if (k == 1) clcd_test_set_banner(1);
        if (k == 2) { clcd_test_set_banner(0); clcd_test_set_page(CLCD_PAGE_APPS); }
        CHECK(run_until_drawn());
        lcdm_model_publish(&M);
        oracle(s_want);
        d = diff_px(fb(), s_want);
        printf("  %s: %u px differ\n", what[k], d);
        CHECK(d == 0u);
    }

    /* NEGATIVE CONTROL: the boot stream with MADCTL patched to 0xE0 must not
     * match the oracle -- and must match it rotated by 180 degrees. */
    memset(s_ap, 0, sizeof(s_ap));
    lcdm_model_init(&M, s_ap, 0);
    for (size_t i = 0; i < S.n; i++) {
        if (S.rs[i] == 2u) { lcdm_model_set_reset(&M, !(S.v[i] & CLCD_CTRL_RESET_N)); continue; }
        uint8_t v = S.v[i];
        if (S.rs[i] == 1u && i > 0 && S.rs[i - 1] == 0u && S.v[i - 1] == 0x16u) v = 0xE0;
        lcdm_model_byte(&M, S.rs[i], v);
    }
    lcdm_model_publish(&M);
    unsigned straight = diff_px(fb(), s_boot_want), rotated = 0;
    for (unsigned y = 0; y < 240u; y++)
        for (unsigned x = 0; x < 320u; x++)
            rotated += fbpx(319u - x, 239u - y) != s_boot_want[y * 320u + x];
    printf("  NEG MADCTL 0xE0: %u px differ straight (must be > 0), %u rotated (must be 0)\n",
           straight, rotated);
    CHECK(straight > 1000u);
    CHECK(rotated == 0u);
}

/* THE ROLE-AWARE ORACLE: clcd.c's committed cells + role plane, drawn from the
 * fonts and HM's palette as this test states it (never clcd.c's pixel code). */
static const uint16_t HM_PAL[CLCD_ROLE_COUNT][2] = CLCD_PALETTE_INIT;
static void oracle_roles(uint16_t *out)
{
    char cells[CLCD_NCELLS];
    uint8_t inv[CLCD_ROWS], roles[CLCD_NCELLS];
    clcd_test_shadow(cells, inv);
    clcd_test_roles(roles);
    for (unsigned i = 0; i < CLCD_NCELLS; i++) {
        unsigned r = i / CLCD_COLS, c = i % CLCD_COLS;
        unsigned ch = (unsigned char)cells[i];
        const uint8_t *g = font8x16[0];
        if (ch >= CLCD_FONT_FIRST && ch <= CLCD_FONT_LAST) g = font8x16[ch - CLCD_FONT_FIRST];
        else if (ch >= CLCD_FONT_EXT_FIRST && ch < CLCD_FONT_EXT_FIRST + CLCD_FONT_EXT_COUNT)
            g = font8x16_ext[ch - CLCD_FONT_EXT_FIRST];
        unsigned role = roles[i] < CLCD_ROLE_COUNT ? roles[i] : 0u;
        for (unsigned yy = 0; yy < 16u; yy++)
            for (unsigned xx = 0; xx < 8u; xx++)
                out[(r * 16u + yy) * 320u + c * 8u + xx] =
                    HM_PAL[role][(g[yy] & (0x80u >> xx)) ? 0 : 1];
    }
}

static void t_aligned_theme(void)
{
    /* Section 2's negative control left the model rotated (MADCTL 0xE0): give it
     * back the real boot stream, then carry on with the same renderer state (link
     * up, apps page). The theme switch repaints every cell. */
    memset(s_ap, 0, sizeof(s_ap));
    lcdm_model_init(&M, s_ap, 0);
    for (size_t i = 0; i < S.n; i++) {
        if (S.rs[i] == 2u) { lcdm_model_set_reset(&M, !(S.v[i] & CLCD_CTRL_RESET_N)); continue; }
        lcdm_model_byte(&M, S.rs[i], S.v[i]);
    }
    clcd_test_set_page(CLCD_PAGE_STATUS);
    s_theme = &clcd_theme_aligned;
    const char *what[3] = { "aligned status", "aligned apps", "aligned DUT OSD" };
    for (int k = 0; k < 3; k++) {
        if (k == 1) clcd_test_set_page(CLCD_PAGE_APPS);
        if (k == 2) clcd_test_set_banner(1);
        clcd_test_force_reformat();
        CHECK(run_until_drawn());
        lcdm_model_publish(&M);
        oracle_roles(s_want);
        unsigned d = diff_px(fb(), s_want);
        /* today's colours: must NOT match. Its own buffer -- s_boot_want stays
         * the BOOT screen's oracle, which section 4's bulk decoder replays. */
        oracle(s_today);
        unsigned neg = diff_px(fb(), s_today);
        printf("  %s: %u px differ from the role oracle; %u from the white/red one (must be > 0)\n",
               what[k], d, neg);
        CHECK(d == 0u);
        CHECK(neg > 1000u);
    }
    /* a status glyph reached the model: the OSD's lock is on row 6 */
    char cells[CLCD_NCELLS];
    uint8_t inv[CLCD_ROWS];
    clcd_test_shadow(cells, inv);
    CHECK(memchr(cells + 6u * CLCD_COLS, CLCD_GLYPH_HELD, CLCD_COLS) != 0);
    clcd_test_set_banner(0);
    clcd_test_set_page(CLCD_PAGE_STATUS);
    s_theme = &clcd_theme_today;
}

/* ==========================================================================
 * 3. the codec
 * ========================================================================== */
static int roundtrip(const uint16_t *px, uint8_t *enc_out, unsigned *len_out)
{
    uint8_t buf[LCDM_TILE_MAX_PAYLOAD];
    uint16_t back[LCDM_TILE_PX];
    uint8_t enc = 0xFF;
    unsigned n = lcdm_encode_tile(px, buf, &enc);
    if (enc_out) *enc_out = enc;
    if (len_out) *len_out = n;
    if (lcdm_decode_tile(enc, buf, n, back) != 0) return 0;
    return memcmp(back, px, sizeof(back)) == 0;
}

/* PackBits length, re-derived here from the contract text (runs of >= 2 are one
 * 3-byte packet of <= 128; everything else goes in literal packets of <= 128). */
static unsigned packbits_len(const uint16_t *a)
{
    unsigned len = 0, i = 0;
    while (i < 256u) {
        unsigned r = 1;
        while (i + r < 256u && a[i + r] == a[i] && r < 128u) r++;
        if (r >= 2u) { len += 3u; i += r; continue; }
        unsigned lit = 0;
        while (i + lit < 256u && lit < 128u && !(i + lit + 1u < 256u && a[i + lit + 1u] == a[i + lit])) lit++;
        if (lit == 0u) lit = 1u;
        len += 1u + 2u * lit;
        i += lit;
    }
    return len;
}

static void t_codec(void)
{
    uint16_t px[LCDM_TILE_PX];
    uint8_t enc;
    unsigned n;
    for (unsigned i = 0; i < 256u; i++) px[i] = 0xF800u;
    CHECK(roundtrip(px, &enc, &n) && enc == LCDM_ENC_FILL && n == 2u);
    /* a glyph: 2 colours, many runs -> PAL1 */
    for (unsigned i = 0; i < 256u; i++) px[i] = ((i * 7u) % 5u < 2u) ? 0xFFFFu : 0x0000u;
    CHECK(roundtrip(px, &enc, &n) && enc == LCDM_ENC_PAL1 && n == 36u);
    /* 2 colours in 3 stretches -> RLE16: runs 100 + 20 + 128 + 8 = 4 packets, 12 B */
    for (unsigned i = 0; i < 256u; i++) px[i] = (i >= 100u && i < 120u) ? 0x07E0u : 0x001Fu;
    CHECK(roundtrip(px, &enc, &n) && enc == LCDM_ENC_RLE16 && n == 12u);
    /* 3 and 4 colours, no repeats -> PAL2 */
    for (unsigned i = 0; i < 256u; i++) px[i] = (uint16_t)(0x1111u * ((i * 11u) % 3u));
    CHECK(roundtrip(px, &enc, &n) && enc == LCDM_ENC_PAL2 && n == 72u);
    for (unsigned i = 0; i < 256u; i++) px[i] = (uint16_t)(0x1234u + ((i * 13u) % 4u));
    CHECK(roundtrip(px, &enc, &n) && enc == LCDM_ENC_PAL2 && n == 72u);
    /* 256 distinct colours: PackBits is 514 B -> RAW */
    for (unsigned i = 0; i < 256u; i++) px[i] = (uint16_t)(i * 257u);
    CHECK(roundtrip(px, &enc, &n) && enc == LCDM_ENC_RAW && n == 512u);
    /* 8 colours in runs of 32 -> RLE16, 8 packets */
    for (unsigned i = 0; i < 256u; i++) px[i] = (uint16_t)(i / 32u);
    CHECK(roundtrip(px, &enc, &n) && enc == LCDM_ENC_RLE16 && n == 24u);
    /* literals then a run: one literal packet + one run packet */
    for (unsigned i = 0; i < 256u; i++) px[i] = i < 5u ? (uint16_t)(100u + i) : 7u;
    CHECK(roundtrip(px, &enc, &n) && enc == LCDM_ENC_RLE16 && n == 1u + 10u + 3u + 3u);
    /* the choice over 3000 random tiles of every shape: HM's rule, exactly */
    uint32_t seed = 12345u;
    for (int k = 0; k < 3000; k++) {
        unsigned ncol = 1u + (unsigned)k % 7u, run = 1u + (unsigned)(k / 7) % 40u;
        uint16_t pal[7];
        for (unsigned c = 0; c < 7u; c++) { seed = seed * 1103515245u + 12345u; pal[c] = (uint16_t)(seed >> 8); }
        for (unsigned i = 0; i < 256u; i++) {
            seed = seed * 1103515245u + 12345u;
            px[i] = (i % run == 0u || i == 0u) ? pal[(seed >> 16) % ncol] : px[i - 1u];
        }
        CHECK(roundtrip(px, &enc, &n));
        unsigned colours = 0;
        uint16_t seen[8];
        for (unsigned i = 0; i < 256u; i++) {
            unsigned j = 0;
            while (j < colours && seen[j] != px[i]) j++;
            if (j == colours && colours < 8u) seen[colours++] = px[i];
        }
        unsigned best;
        if (colours == 1u) {
            best = 2u;
        } else {
            unsigned r = packbits_len(px);
            best = r < 512u ? r : 512u;
            if (colours == 2u && 36u < best) best = 36u;
            else if (colours >= 3u && colours <= 4u && 72u < best) best = 72u;
        }
        CHECK(n == best);
    }
    /* malformed payloads are rejected */
    uint16_t back[LCDM_TILE_PX];
    uint8_t bad[9] = { 0x80, 1, 0, 0x80, 1, 0, 0x80, 1, 0 };   /* 3 runs of 1 = 3 pixels */
    CHECK(lcdm_decode_tile(LCDM_ENC_RLE16, bad, 9, back) != 0);
    uint8_t trunc[2] = { 0x05, 1 };                            /* literals past the end */
    CHECK(lcdm_decode_tile(LCDM_ENC_RLE16, trunc, 2, back) != 0);
    CHECK(lcdm_decode_tile(LCDM_ENC_PAL1, bad, 9, back) != 0);
    CHECK(lcdm_decode_tile(7, bad, 2, back) != 0);

    /* MUTATION CONTROLS: each broken encoder must FAIL the round trip */
    static const struct { int mut; const char *name; } muts[] = {
        { LCDM_MUT_PAL_BITS, "pal_bits" }, { LCDM_MUT_RLE_LEN, "rle_len" },
        { LCDM_MUT_PX_ENDIAN, "px_endian" },
    };
    for (unsigned k = 0; k < sizeof(muts) / sizeof(muts[0]); k++) {
        lcdm_mutation = muts[k].mut;
        int caught = 0;
        for (unsigned i = 0; i < 256u; i++) px[i] = ((i * 7u) % 5u < 2u) ? 0xFFFFu : 0x0000u;   /* PAL1 */
        caught |= !roundtrip(px, 0, 0);
        for (unsigned i = 0; i < 256u; i++) px[i] = (i >= 100u && i < 120u) ? 0x07E0u : 0x001Fu; /* RLE */
        caught |= !roundtrip(px, 0, 0);
        for (unsigned i = 0; i < 256u; i++) px[i] = (uint16_t)(i * 257u);                         /* RAW */
        caught |= !roundtrip(px, 0, 0);
        printf("  mutation %-9s -> round trip %s\n", muts[k].name, caught ? "FAILS (caught)" : "PASSES (NOT caught)");
        CHECK(caught);
        lcdm_mutation = LCDM_MUT_NONE;
    }

    /* the wire builders: header, part budget, one UPDATE */
    uint8_t msg[LCDM_MAX_MSG];
    lcdm_snaphdr_t h;
    memset(&h, 0, sizeof(h));
    h.t_ms = 1234u; h.frames = 5u; h.resets = 1u; h.status = 0x4Bu | LCDM_S_TEXT_ONLY;
    h.valid[0] = 1u;
    h.mode = 0x00090520u;
    static uint16_t frame[LCDM_NPX];
    for (unsigned i = 0; i < LCDM_NPX; i++) frame[i] = 0x1234u;
    uint32_t tiles[LCDM_MAP_WORDS] = { 1u };
    static uint8_t recs[LCDM_NTILES * LCDM_REC_MAX];
    unsigned nt = 0;
    size_t rl = lcdm_encode_records(frame, tiles, recs, &nt);
    CHECK(nt == 1u && rl == 7u && recs[2] == LCDM_ENC_FILL);
    size_t ml = lcdm_build_update(msg, 42u, &h, LCDM_S_SNAP_LAST, recs, rl, nt);
    CHECK(ml == 8u + 59u + 4u + 2u + 7u);
    CHECK(msg[0] == 'L' && msg[1] == 'M' && msg[2] == LCDM_MSG_UPDATE && msg[3] == 0u);
    CHECK(msg[4] == (uint8_t)(ml - 8u) && msg[8] == 42u);
    CHECK(lcdm_part_budget(LCDM_MAX_MSG, 1) == LCDM_MAX_MSG - (8u + 59u + 256u + 2u));
}

/* ==========================================================================
 * 4. cost: harnessd-sized passes through the model
 * ========================================================================== */
static double now_s(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

static void t_cost(void)
{
    if (S.n == 0u) return;
    enum { REPS = 20 };
    double best = 1e9;
    double worst_pass = 0.0;
    unsigned passes = 0;
    for (int rep = 0; rep < REPS; rep++) {
        lcdm_model_init(&M, s_ap, 1);
        double t0 = now_s();
        size_t i = 0;
        while (i < S.n) {
            double p0 = now_s();
            unsigned b = 0;
            for (; i < S.n && b < CLCD_BYTES_PER_PASS; i++) {
                if (S.rs[i] == 2u) { lcdm_model_set_reset(&M, !(S.v[i] & CLCD_CTRL_RESET_N)); continue; }
                lcdm_model_byte(&M, S.rs[i], S.v[i]);
                b++;
            }
            lcdm_model_publish(&M);
            double dp = now_s() - p0;
            if (rep == REPS - 1) { if (dp > worst_pass) worst_pass = dp; passes++; }
        }
        double dt = now_s() - t0;
        if (dt < best) best = dt;
    }
    double ns_byte = best * 1e9 / (double)S.n;
    double us_pass = best * 1e6 / (double)((S.n + CLCD_BYTES_PER_PASS - 1u) / CLCD_BYTES_PER_PASS);
    printf("  COST (host): %.2f ns/byte, %.2f us per %u-byte pass incl. publish "
           "(best of %d; worst single pass %.1f us over %u passes)\n",
           ns_byte, us_pass, (unsigned)CLCD_BYTES_PER_PASS, REPS, worst_pass * 1e6, passes);
    /* Generous bounds (a loaded build host): the MBV runs ~30-60x slower, and a
     * pass there has a 30 ms clcd budget (main_linux.c). */
    CHECK(ns_byte < 100.0);
    CHECK(us_pass < 50.0);
}

/* ==========================================================================
 * 5. THE BULK DECODER (lane CLCD-SPEED): lcdm_model_bytes() == lcdm_model_byte()
 *    n times, bit for bit -- the model state AND the whole aperture -- on the
 *    real renderer's boot stream and on random streams (every geometry, both
 *    COLMODs, odd windows, OOB, resets and publishes mid-stream, pixel pairs
 *    split across calls). Mutation control: one flipped pixel byte must show.
 * ========================================================================== */
static uint32_t s_ap2[LCDM_APERTURE / 4u] __attribute__((aligned(64)));
static lcdm_model_t M2;

static int model_eq(const lcdm_model_t *a, const lcdm_model_t *b)
{
    return a->ctrl == b->ctrl && a->live == b->live && a->sticky == b->sticky &&
           a->seq == b->seq && a->frames == b->frames && a->ramwr == b->ramwr &&
           a->resets == b->resets && a->bytes == b->bytes && a->oob == b->oob &&
           a->sc == b->sc && a->ec == b->ec && a->sp == b->sp && a->ep == b->ep &&
           a->acx == b->acx && a->acy == b->acy && a->idx == b->idx && a->nb == b->nb &&
           memcmp(a->pb, b->pb, sizeof(a->pb)) == 0 && a->mv == b->mv &&
           a->flip_g == b->flip_g && a->flip_s == b->flip_s && a->in_reset == b->in_reset &&
           a->regs_dirty == b->regs_dirty && a->changed == b->changed && a->r01 == b->r01 &&
           a->r16 == b->r16 && a->r17 == b->r17 && a->r1f == b->r1f && a->r28 == b->r28 &&
           a->r36 == b->r36 && memcmp(a->regs, b->regs, sizeof(a->regs)) == 0 &&
           memcmp(a->pend, b->pend, sizeof(a->pend)) == 0 &&
           memcmp(a->valid, b->valid, sizeof(a->valid)) == 0;
}

static uint32_t s_rng = 0x2545F491u;
static uint32_t rnd(void) { s_rng ^= s_rng << 13; s_rng ^= s_rng >> 17; s_rng ^= s_rng << 5; return s_rng; }

/* Replay {rs, v} (rs 2 = a CTRL write: the RST pad; rs 3 = a publish) into M
 * byte by byte and into M2 in random-length bulk runs. */
static void replay_both2(const uint8_t *rs, const uint8_t *v, const uint8_t *v2, size_t n,
                         uint32_t ctrl);
static void replay_both(const uint8_t *rs, const uint8_t *v, size_t n, uint32_t ctrl)
{
    replay_both2(rs, v, v, n, ctrl);
}

/* M gets v byte by byte, M2 gets v2 in bulk runs (v2 == v but for a mutation). */
static void replay_both2(const uint8_t *rs, const uint8_t *v, const uint8_t *v2, size_t n,
                         uint32_t ctrl)
{
    memset(s_ap, 0, sizeof(s_ap));
    memset(s_ap2, 0, sizeof(s_ap2));
    lcdm_model_init(&M, s_ap, 0);
    lcdm_model_init(&M2, s_ap2, 0);
    lcdm_model_set_ctrl(&M, ctrl);
    lcdm_model_set_ctrl(&M2, ctrl);
    for (size_t i = 0; i < n; i++) {
        if (rs[i] == 2u) lcdm_model_set_reset(&M, !(v[i] & CLCD_CTRL_RESET_N));
        else if (rs[i] == 3u) lcdm_model_publish(&M);
        else lcdm_model_byte(&M, rs[i], v[i]);
    }
    size_t i = 0;
    while (i < n) {
        if (rs[i] >= 2u) {
            if (rs[i] == 2u) lcdm_model_set_reset(&M2, !(v[i] & CLCD_CTRL_RESET_N));
            else lcdm_model_publish(&M2);
            i++;
            continue;
        }
        size_t j = i, lim = i + 1u + rnd() % 700u;
        while (j < n && j < lim && rs[j] < 2u) j++;
        lcdm_model_bytes(&M2, rs + i, v2 + i, (uint32_t)(j - i));
        i = j;
    }
    lcdm_model_publish(&M);
    lcdm_model_publish(&M2);
}

static int both_equal(void)
{
    return model_eq(&M, &M2) && memcmp(s_ap, s_ap2, sizeof(s_ap)) == 0;
}

static size_t fuzz_stream(uint8_t *rs, uint8_t *v, size_t cap)
{
    static const uint8_t mad[8] = { 0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0, 0xE0 };
    size_t n = 0;
#define PUT(r, b) do { if (n < cap) { rs[n] = (uint8_t)(r); v[n] = (uint8_t)(b); n++; } } while (0)
    PUT(0, 0x17); PUT(1, (rnd() & 3u) ? 0x05 : 0x06);
    PUT(0, 0x16); PUT(1, mad[rnd() & 7u]);
    while (n + 1400u < cap) {
        unsigned op = rnd() % 16u;
        if (op < 6) {                               /* a window, in-range or not */
            unsigned sc = rnd() % 340u, sp = rnd() % 340u;
            unsigned ec = (rnd() & 7u) ? sc + rnd() % 24u : rnd() % 512u;
            unsigned ep = (rnd() & 7u) ? sp + rnd() % 24u : rnd() % 512u;
            unsigned w[4] = { sc & 0x1FFu, ec & 0x1FFu, sp & 0x1FFu, ep & 0x1FFu };
            for (unsigned k = 0; k < 4u; k++) {
                PUT(0, 0x02 + 2u * k); PUT(1, w[k] >> 8);
                PUT(0, 0x03 + 2u * k); PUT(1, w[k] & 0xFFu);
            }
        } else if (op < 12) {                       /* RAMWR + a pixel run, any length */
            PUT(0, 0x22);
            unsigned len = 1u + rnd() % 1300u;
            for (unsigned k = 0; k < len; k++) PUT(1, rnd());
        } else if (op == 12) {                      /* a geometry / format change */
            PUT(0, 0x16); PUT(1, mad[rnd() & 7u]);
            if (rnd() & 1u) { PUT(0, 0x17); PUT(1, (rnd() & 3u) ? 0x05 : ((rnd() & 1u) ? 0x06 : rnd())); }
        } else if (op == 13) {                      /* the panel reset pad */
            PUT(2, 0); PUT(1, rnd()); PUT(0, 0x22); PUT(1, rnd()); PUT(2, CLCD_CTRL_RESET_N);
        } else if (op == 14) {                      /* a publish mid-stream */
            PUT(3, 0);
        } else {                                    /* stray data / other registers */
            PUT(0, rnd()); unsigned len = rnd() % 5u;
            for (unsigned k = 0; k < len; k++) PUT(1, rnd());
        }
    }
#undef PUT
    return n;
}

static void t_bulk_decoder(void)
{
    /* the real renderer's boot stream (section 2 recorded it) */
    CHECK(S.n > 160000u);
    replay_both(S.rs, S.v, S.n, 0u);
    CHECK(both_equal());
    unsigned d = diff_px((const uint16_t *)((const uint8_t *)s_ap2 + LCDM_FB), s_boot_want);
    CHECK(d == 0u);                                  /* pixel-exact vs the font oracle */

    /* random streams */
    enum { NSTREAM = 300, CAP = 24000 };
    static uint8_t rs[CAP], v[CAP];
    unsigned ok = 0;
    for (int t = 0; t < NSTREAM; t++) {
        size_t n = fuzz_stream(rs, v, CAP);
        uint32_t ctrl = rnd() & (LCDM_CTRL_AC_LOAD | LCDM_CTRL_FLIP_CONV);
        replay_both(rs, v, n, ctrl);
        ok += both_equal() ? 1u : 0u;
    }
    printf("  bulk decoder: boot stream %zu B identical (0 px off the oracle); %u/%d random "
           "streams identical\n", S.n, ok, NSTREAM);
    CHECK(ok == (unsigned)NSTREAM);

    /* MUTATION CONTROL: the comparison has teeth -- one pixel byte changed on the
     * bulk side only must make the two differ. */
    size_t k = 0;
    for (size_t i = 1; i < S.n; i++)             /* a glyph's first pixel byte */
        if (S.rs[i] == 1u && S.rs[i - 1] == 0u && S.v[i - 1] == 0x22u && i > S.n / 2u) { k = i; break; }
    CHECK(k != 0u);
    uint8_t *mut = malloc(S.n);
    CHECK(mut != 0);
    if (mut) {
        memcpy(mut, S.v, S.n);
        mut[k] ^= 0x5Au;
        replay_both2(S.rs, S.v, mut, S.n, 0u);
        CHECK(!both_equal());
        free(mut);
    }
}

/* The same boot stream through the BULK decoder in the runs the bus seam hands
 * it (a clcd.c cell's contiguous stream, <= 128 bytes a STATUS read). */
static void t_cost_bulk(void)
{
    if (S.n == 0u) return;
    enum { REPS = 20 };
    double best = 1e9, best1 = 1e9;
    for (int rep = 0; rep < REPS; rep++) {
        lcdm_model_init(&M, s_ap, 1);
        double t0 = now_s();
        size_t i = 0;
        while (i < S.n) {
            if (S.rs[i] == 2u) { lcdm_model_set_reset(&M, !(S.v[i] & CLCD_CTRL_RESET_N)); i++; continue; }
            size_t j = i;
            while (j < S.n && j - i < 128u && S.rs[j] < 2u) j++;
            lcdm_model_bytes(&M, S.rs + i, S.v + i, (uint32_t)(j - i));
            i = j;
        }
        double dt = now_s() - t0;
        if (dt < best) best = dt;
        lcdm_model_init(&M, s_ap, 1);
        t0 = now_s();
        for (i = 0; i < S.n; i++) {
            if (S.rs[i] == 2u) { lcdm_model_set_reset(&M, !(S.v[i] & CLCD_CTRL_RESET_N)); continue; }
            lcdm_model_byte(&M, S.rs[i], S.v[i]);
        }
        dt = now_s() - t0;
        if (dt < best1) best1 = dt;
    }
    printf("  COST (host): bulk %.2f ns/byte vs per-byte %.2f ns/byte (%.1fx)\n",
           best * 1e9 / (double)S.n, best1 * 1e9 / (double)S.n, best1 / best);
    CHECK(best * 1e9 / (double)S.n < 100.0);
}

int main(int argc, char **argv)
{
    const char *dump = (argc == 3 && strcmp(argv[1], "--dump-stream") == 0) ? argv[2] : 0;
    t_window_fill_wrap_frames();
    t_eight_geometries();
    t_formats();
    t_split_ramwr_and_ac_load();
    t_oob();
    t_reset();
    t_regs_and_dirty_handoff();
    t_real_renderer(dump);
    t_aligned_theme();
    t_codec();
    t_cost();
    t_bulk_decoder();
    t_cost_bulk();
    printf("test_lcdmirror: %d checks, %d failed\n", s_checks, s_fails);
    return s_fails ? 1 : 0;
}
