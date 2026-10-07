/*
 * test_clcd_roles.c -- the front panel's colour roles, palette seam, status
 * glyphs, programming progress, page-aware hit test, event ring and frame access
 * (Harness Manager docs/design/CLCD_ALIGNMENT.md §5.3 / §7.2: R4, R5, the clcd.c
 * half of R2). Host gcc, the REAL clcd.c + init table over mock registers.
 *
 * Every frame is checked where it matters -- as PIXELS, through the bus: a small
 * GRAM model below takes every CMD/DATA byte clcd.c pushes (window registers
 * 0x02-0x09, RAMWR 0x22, RGB565 high byte first) and holds the 320x240 image the
 * panel would show. Nothing here reads the renderer's pixel code.
 *
 * Two builds of this one file (firmware/test/Makefile):
 *   test_clcd_golden  -DTEST_GOLDEN_ONLY: NO provider of any seam, so clcd.c's
 *                     WEAK defaults run -- the bare-metal image. It replays six
 *                     cumulative scenes and asserts the bus stream (CRC-32 of the
 *                     {rs,byte} pairs + the byte count) and the final picture
 *                     (CRC-32 of the GRAM) against GOLDEN[] below, which was
 *                     captured from the UNMODIFIED clcd.c at bb69f61 (build with
 *                     -DTEST_GOLDEN_PRINT to print a fresh table). This is the
 *                     "the default palette keeps today's pixels" gate: any change
 *                     to a default-theme pixel, byte or byte ORDER fails it.
 *   test_clcd_roles   the strong providers (theme, badge, session row, overlay,
 *                     locate/tap, swap progress) as mps3-harnessd would supply
 *                     them, switchable per test; the golden again with the
 *                     provider returning &clcd_theme_today and NULL, a NEGATIVE
 *                     CONTROL (a one-bit palette change must break the golden),
 *                     and the R2/R4/R5 unit tests.
 */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../clcd/clcd.h"
#include "../clcd/font8x16.h"
#include "../common/platform_regs.h"
#include "../common/net_proto.h"
#include "../common/diag.h"
#include "../coordinator/coordinator.h"
#include "../coordinator/swap_fsm.h"
#include "../smsc911x/smsc911x.h"
#include "mock_regs.h"
#ifndef TEST_PRISTINE
#include "../clcd/clcd_glyphs.h"
#endif

static int s_checks = 0, s_fails = 0;
#define CHECK(c) do { s_checks++; if (!(c)) { s_fails++; \
    fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #c); } } while (0)

/* ==========================================================================
 * The symbols clcd.c links against (the firmware/test pattern)
 * ========================================================================== */
mps3_shell_state_t   g_shell_state;
volatile mps3_diag_t g_mps3_diag;

static mps3_swap_result_t s_swap_res;
const mps3_swap_result_t *swap_fsm_last_result(void) { return &s_swap_res; }
static uint32_t s_icap_bytes;
uint32_t swap_fsm_icap_bytes(void) { return s_icap_bytes; }

static int s_link_up;
int smsc911x_link_up(void) { return s_link_up; }
int smsc911x_mii_read(uint32_t r, uint16_t *v) { if (v) *v = (r == 5u) ? (1u << 8) : 0u; return 0; }
void mps3_platform_mac(uint8_t mac[6])
{
    static const uint8_t m[6] = { 0x02, 0x00, 0x00, 0x4D, 0x50, 0x53 };
    memcpy(mac, m, 6);
}

static const char *s_usd = "none";
const char *overlay_store_usd_text(void)         { return s_usd; }
uint32_t    overlay_store_usd_change_count(void) { return 0u; }

/* ==========================================================================
 * The bus: CRC of the stream + a GRAM model
 * ========================================================================== */
static uint32_t crc32_upd(uint32_t c, const uint8_t *p, size_t n)
{
    c = ~c;
    while (n--) {
        c ^= *p++;
        for (int k = 0; k < 8; k++) c = (c >> 1) ^ (0xEDB88320u & (0u - (c & 1u)));
    }
    return ~c;
}

static uint16_t s_gram[240][320];
static struct {
    uint8_t  idx;          /* last command byte (register index)        */
    int      ramwr;        /* in a 0x22 pixel burst                     */
    int      have_hi;      /* holding the high byte of a pixel          */
    uint8_t  hi;
    uint8_t  reg[16];      /* 0x02..0x09 window registers               */
    unsigned x, y;         /* write cursor                              */
    uint32_t crc;          /* CRC-32 over {rs,byte}                      */
    uint32_t n;            /* bytes on the bus                          */
    uint32_t px;           /* pixel words written                       */
} B;

static unsigned wx0(void) { return ((unsigned)B.reg[2] << 8) | B.reg[3]; }
static unsigned wx1(void) { return ((unsigned)B.reg[4] << 8) | B.reg[5]; }
static unsigned wy0(void) { return ((unsigned)B.reg[6] << 8) | B.reg[7]; }
static unsigned wy1(void) { return ((unsigned)B.reg[8] << 8) | B.reg[9]; }

static void bus_byte(int rs, uint8_t v)
{
    uint8_t pair[2] = { (uint8_t)rs, v };
    B.crc = crc32_upd(B.crc, pair, 2);
    B.n++;
    if (!rs) {                           /* a command ends any pixel burst */
        B.idx = v;
        B.ramwr = (v == 0x22u);
        B.have_hi = 0;
        if (B.ramwr) { B.x = wx0(); B.y = wy0(); }
        return;
    }
    if (B.ramwr) {
        if (!B.have_hi) { B.hi = v; B.have_hi = 1; return; }
        B.have_hi = 0;
        if (B.x < 320u && B.y < 240u) s_gram[B.y][B.x] = (uint16_t)((B.hi << 8) | v);
        B.px++;
        if (++B.x > wx1()) { B.x = wx0(); if (++B.y > wy1()) B.y = wy0(); }
        return;
    }
    if (B.idx >= 0x02u && B.idx <= 0x09u) B.reg[B.idx] = v;
}

static int clcd_hook(void *ctx, int is_write, uint32_t base, uint32_t off, uint32_t *val)
{
    (void)ctx; (void)base;
    if (!is_write) {
        *val = (off == CLCD_STATUS) ? CLCD_STATUS_FIFO_EMPTY : 0u;   /* never full */
        return 1;
    }
    if (off == CLCD_CMD || off == CLCD_DATA) {
        bus_byte(off == CLCD_DATA, (uint8_t)*val);
        return 1;
    }
    return 1;                            /* CTRL / TIMING: not part of the picture */
}

static uint32_t gram_crc(void)
{
    return crc32_upd(0u, (const uint8_t *)s_gram, sizeof(s_gram));
}

/* Poll until clcd.c has drawn and sits IDLE with nothing dirty, twice running.
 * Asserts the per-pass byte budget on every pass. */
static uint32_t s_max_pass;
static int run_until_drawn(void)
{
    int quiet = 0;
    for (int i = 0; i < 400000; i++) {
        clcd_poll();
        if (clcd_test_bytes_last_pass() > s_max_pass) s_max_pass = clcd_test_bytes_last_pass();
        mock_time_advance_ms(1);
        int idle = clcd_test_state() == CLCD_ST_IDLE && clcd_test_dirty_count() == 0u;
        quiet = idle ? quiet + 1 : 0;
        if (quiet >= 3) return 1;
    }
    return 0;
}

static void seed_rm_live(uint32_t rm_id)
{
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,    0u);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_ID,     rm_id);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS, DFXCTL_RM_STATUS_RM_ID_VALID);
}

static void seed_healthy_clocks(void)
{
    mock_regs_poke(MPS3_CLKRST_BASE, CLKRST_STATUS,
                   CLKRST_STATUS_MMCM_LOCKED | CLKRST_STATUS_DUT_CLK_ALIVE);
    mock_regs_poke(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL, CLKRST_RESET_CTRL_DUT_RESETN);
}

/* A fresh panel: registers, GRAM, clocks, the board as it boots. */
static void fresh_board(void)
{
    mock_regs_reset();
    mock_regs_set_hook(MPS3_CLCD_BASE, clcd_hook, 0);
    memset(&B, 0, sizeof(B));
    memset(s_gram, 0, sizeof(s_gram));
    memset(&g_shell_state, 0, sizeof(g_shell_state));
    memset((void *)&g_mps3_diag, 0, sizeof(g_mps3_diag));
    memset(&s_swap_res, 0, sizeof(s_swap_res));
    g_shell_state.static_id = 0x14E1A2D8u;
    s_icap_bytes = 0;
    s_link_up = 0;                               /* boots into NETWORK LINK DOWN */
    s_usd = "none";
    seed_rm_live(0u);
    seed_healthy_clocks();
    mock_time_set_ms(5000u);
    s_max_pass = 0;
}

/* ==========================================================================
 * THE GOLDEN: six cumulative scenes on one panel, default theme.
 * ========================================================================== */
typedef struct { const char *name; uint32_t stream_crc, nbytes, frame_crc; } golden_t;

/* Captured from the UNMODIFIED firmware/clcd/clcd.c at bb69f61 (feat/linux-harness)
 * with `-DTEST_PRISTINE -DTEST_GOLDEN_ONLY -DTEST_GOLDEN_PRINT`. Do not refresh
 * this table to make a default-theme change pass: the point is that the bare-metal
 * picture does not move. */
static const golden_t GOLDEN[] = {
    { "boot-link-down",  0xBD5128AEu, 164193u, 0x5A734B14u },
    { "healthy",         0x58347DFFu, 57876u, 0x4D6280DDu },
    { "multicore-card",  0xAB6CD7E8u, 15834u, 0xED53CE2Du },
    { "dut-osd",         0xFE4D86F6u, 113022u, 0xE541DF03u },
    { "apps",            0x1DABB265u, 116025u, 0x33E9A3B2u },
    { "status-1s",       0x9CCAB153u, 93366u, 0xE1F9F761u },
};
#define NGOLDEN ((int)(sizeof(GOLDEN) / sizeof(GOLDEN[0])))

static void scene(int k)
{
    switch (k) {
    case 0:                                          /* boot: link down banner */
        clcd_init();
        break;
    case 1:                                          /* link up, nanosoc verified */
        s_link_up = 1;
        seed_rm_live(0x01000001u);
        g_shell_state.current_rm_id = 0x01000001u;
        s_swap_res.valid = 1; s_swap_res.ok = 1; s_swap_res.verified = 1;
        s_swap_res.rm_id = 0x01000001u;
        s_icap_bytes = 1835072u;
        clcd_test_force_reformat();
        break;
    case 2:                                          /* multicore (DIP row) + a card */
        seed_rm_live(0x01000003u);
        s_usd = "nanosoc [A]";
        s_icap_bytes = 2100480u;
        clcd_test_force_reformat();
        break;
    case 3:                                          /* the KVM OSD */
        clcd_test_set_banner(1);
        break;
    case 4:                                          /* apps page */
        clcd_test_set_banner(0);
        clcd_test_set_page(CLCD_PAGE_APPS);
        break;
    default:                                         /* status again, 1 s later */
        clcd_test_set_page(CLCD_PAGE_STATUS);
        mock_time_advance_ms(1000u);
        clcd_test_force_reformat();
        break;
    }
}

/* Replays the scenes; fills got[] (stream CRC and bytes per scene, frame CRC
 * after it). Returns 1 when every scene drew and settled. */
static int replay(golden_t got[NGOLDEN])
{
    fresh_board();
    int ok = 1;
    for (int k = 0; k < NGOLDEN; k++) {
        B.crc = 0; B.n = 0;
        scene(k);
        ok &= run_until_drawn();
        got[k].name = GOLDEN[k].name;
        got[k].stream_crc = B.crc;
        got[k].nbytes = B.n;
        got[k].frame_crc = gram_crc();
    }
    return ok;
}

static int golden_matches(const golden_t got[NGOLDEN], int verbose)
{
    int all = 1;
    for (int k = 0; k < NGOLDEN; k++) {
        int m = got[k].stream_crc == GOLDEN[k].stream_crc && got[k].nbytes == GOLDEN[k].nbytes &&
                got[k].frame_crc == GOLDEN[k].frame_crc;
        if (verbose && !m)
            fprintf(stderr, "  golden %-15s stream %08X/%u frame %08X, want %08X/%u %08X\n",
                    got[k].name, got[k].stream_crc, got[k].nbytes, got[k].frame_crc,
                    GOLDEN[k].stream_crc, GOLDEN[k].nbytes, GOLDEN[k].frame_crc);
        all &= m;
    }
    return all;
}

static void t_golden(const char *label)
{
    golden_t got[NGOLDEN];
    CHECK(replay(got));
    CHECK(s_max_pass <= CLCD_BYTES_PER_PASS);
#ifdef TEST_GOLDEN_PRINT
    for (int k = 0; k < NGOLDEN; k++)
        printf("    { \"%s\",%*s 0x%08Xu, %uu, 0x%08Xu },\n", got[k].name,
               (int)(15 - strlen(got[k].name)), "", got[k].stream_crc, got[k].nbytes,
               got[k].frame_crc);
#endif
    int m = golden_matches(got, 1);
    printf("  golden (%s): %s\n", label, m ? "today's bytes and pixels, unchanged" : "DIFFERS");
    CHECK(m);
}

#ifdef TEST_GOLDEN_ONLY
int main(void)
{
    printf("test_clcd_golden: the default palette/theme = today's panel (weak seams)\n");
    t_golden("weak defaults");
    printf("test_clcd_golden: %d checks, %d failed\n", s_checks, s_fails);
    return s_fails ? 1 : 0;
}
#endif

#ifndef TEST_GOLDEN_ONLY
/* ==========================================================================
 * The strong providers, as mps3-harnessd would supply them -- each switchable.
 * ========================================================================== */
static const mps3_clcd_theme_t *s_theme_p;    /* NULL = the seam returns NULL */
const mps3_clcd_theme_t *mps3_clcd_palette(void) { return s_theme_p; }

static int s_badge_on;
static mps3_clcd_badge_t s_badge;
int mps3_clcd_title_right(mps3_clcd_badge_t *out) { if (!s_badge_on) return 0; *out = s_badge; return 1; }

static int s_sess_on;
static clcd_line_t s_sess;
int mps3_clcd_session_row(clcd_line_t *out) { if (!s_sess_on) return 0; *out = s_sess; return 1; }

static int s_ovl_on;
static mps3_clcd_overlay_t s_ovl;
int mps3_clcd_overlay(mps3_clcd_overlay_t *out) { if (!s_ovl_on) return 0; *out = s_ovl; return 1; }

static int s_loc_on, s_tap_take;
static const char *s_loc_who = "";
int mps3_clcd_locate(char *who, unsigned cap)
{
    if (!s_loc_on) return 0;
    snprintf(who, cap, "%s", s_loc_who);
    return 1;
}
int mps3_clcd_tap(unsigned x, unsigned y) { (void)x; (void)y; return s_loc_on && s_tap_take; }

static mps3_swap_progress_t s_prog;
void swap_fsm_progress(mps3_swap_progress_t *out) { *out = s_prog; if (!out->rm) out->rm = ""; }

static int s_eng_on;
int mps3_clcd_engine(mps3_clcd_engine_t *out)
{
    if (!s_eng_on) return 0;
    out->impl = "linux";
    out->claimed = 1;
    memcpy(out->fpr, "AbCdEfGh", 9);
    return 1;
}

static void providers_off(void)
{
    s_theme_p = &clcd_theme_today;
    s_badge_on = s_sess_on = s_ovl_on = s_loc_on = s_tap_take = s_eng_on = 0;
    memset(&s_prog, 0, sizeof(s_prog));
}

/* A healthy board, drawn and settled, on a fresh panel. */
static void healthy_drawn(const mps3_clcd_theme_t *theme)
{
    fresh_board();
    s_theme_p = theme;
    s_link_up = 1;
    seed_rm_live(0x01000001u);
    g_shell_state.current_rm_id = 0x01000001u;
    s_swap_res.valid = 1; s_swap_res.ok = 1; s_swap_res.verified = 1;
    s_swap_res.rm_id = 0x01000001u;
    s_icap_bytes = 1835072u;
    s_usd = "nanosoc [A]";
    clcd_init();
    CHECK(run_until_drawn());
}

static void redraw(void)
{
    clcd_test_force_reformat();
    CHECK(run_until_drawn());
}

static const char *row_of(unsigned r)
{
    static char rows[1][CLCD_COLS + 1];
    CHECK(clcd_frame_rows(r, 1, rows, 0) == 1u);
    return rows[0];
}
static unsigned role_at(unsigned r, unsigned c)
{
    uint8_t roles[CLCD_NCELLS];
    clcd_test_roles(roles);
    return roles[r * CLCD_COLS + c];
}
static int row_all_role(unsigned r, unsigned role)
{
    for (unsigned c = 0; c < CLCD_COLS; c++) if (role_at(r, c) != role) return 0;
    return 1;
}
static unsigned count_role(unsigned r, unsigned role)
{
    unsigned n = 0;
    for (unsigned c = 0; c < CLCD_COLS; c++) n += role_at(r, c) == role;
    return n;
}

/* THE ORACLE: what the glass must show for the committed cells + role codes,
 * drawn here from the fonts and a palette this test states itself -- HM's
 * CLCD_PALETTE_INIT for the aligned theme, and the three literal colours of the
 * pre-R4 renderer for today. It never calls the renderer's pixel code. */
static const uint16_t HM_PAL[CLCD_ROLE_COUNT][2] = CLCD_PALETTE_INIT;
static uint16_t oracle_px(int aligned_theme, unsigned role, int on)
{
    if (aligned_theme) return HM_PAL[role][on ? 0 : 1];
    if (on) return 0xFFFFu;
    return (role >= CLCD_ROLE_BANNER_ERR && role <= CLCD_ROLE_BANNER_HELD) ? 0xF800u : 0x0000u;
}
static unsigned oracle_diff(int aligned_theme)
{
    char rows[CLCD_ROWS][CLCD_COLS + 1];
    char codes[CLCD_NCELLS + 1];
    unsigned bad = 0;
    CHECK(clcd_frame_rows(0, CLCD_ROWS, rows, codes) == CLCD_ROWS);
    for (unsigned r = 0; r < CLCD_ROWS; r++) {
        for (unsigned c = 0; c < CLCD_COLS; c++) {
            unsigned ch = (unsigned char)rows[r][c];
            unsigned role = (unsigned)(codes[r * CLCD_COLS + c] - 'a');
            const uint8_t *g = font8x16[0];
            if (ch >= 0x20u && ch <= 0x7Eu) g = font8x16[ch - 0x20u];
            else if (ch >= 0x80u && ch <= 0x86u) g = font8x16_ext[ch - 0x80u];
            for (unsigned y = 0; y < 16u; y++)
                for (unsigned x = 0; x < 8u; x++)
                    bad += s_gram[r * 16u + y][c * 8u + x] !=
                           oracle_px(aligned_theme, role, (g[y] >> (7u - x)) & 1u);
        }
    }
    return bad;
}

/* ==========================================================================
 * The golden, through the strong seam (today / NULL) + the NEGATIVE CONTROL
 * ========================================================================== */
static void t_golden_through_the_seam(void)
{
    providers_off();
    t_golden("seam = &clcd_theme_today");
    s_theme_p = 0;
    t_golden("seam = NULL");

    /* NEGATIVE CONTROL: one bit of one colour of the default theme. If the golden
     * cannot see this, it proves nothing about the default palette. */
    static uint16_t mut[CLCD_ROLE_COUNT][2];
    memcpy(mut, clcd_theme_today.pal, sizeof(mut));
    mut[CLCD_ROLE_TEXT][0] ^= 0x0001u;
    static const mps3_clcd_theme_t mutant = { (const uint16_t (*)[2])mut, 0u, "today-mutant" };
    s_theme_p = &mutant;
    golden_t got[NGOLDEN];
    CHECK(replay(got));
    int m = golden_matches(got, 0);
    printf("  NEG: text fg 0xFFFF -> 0xFFFE: golden %s (must differ)\n", m ? "MATCHES" : "differs");
    CHECK(!m);

    /* ...and the today theme's table is today's three colours, role by role. */
    for (unsigned r = 0; r < CLCD_ROLE_COUNT; r++) {
        int banner = r >= CLCD_ROLE_BANNER_ERR && r <= CLCD_ROLE_BANNER_HELD;
        CHECK(clcd_theme_today.pal[r][0] == 0xFFFFu);
        CHECK(clcd_theme_today.pal[r][1] == (banner ? 0xF800u : 0x0000u));
    }
    providers_off();
}

/* ==========================================================================
 * The role plane (today theme): TEXT, and a banner role on an inverted row
 * ========================================================================== */
static void t_role_plane_today(void)
{
    providers_off();
    fresh_board();                             /* link down: the fault banner */
    clcd_init();
    CHECK(run_until_drawn());
    for (unsigned r = 0; r < CLCD_ROWS; r++)
        CHECK(row_all_role(r, (r >= 10u && r <= 12u) ? CLCD_ROLE_BANNER_ERR : CLCD_ROLE_TEXT));
    char codes[3 * CLCD_COLS + 1];
    CHECK(clcd_frame_rows(10, 3, 0, codes) == 3u);
    CHECK(codes[0] == 'q' && CLCD_ROLE_CODE(CLCD_ROLE_BANNER_ERR) == 'q');
    CHECK(strcmp(clcd_role_name(CLCD_ROLE_BANNER_ERR), "banner-err") == 0);
    CHECK(strcmp(clcd_role_name(CLCD_ROLE_BANNER_HELD), "banner-held") == 0);
    CHECK(clcd_role_name(CLCD_ROLE_COUNT)[0] == '\0');
    CHECK(oracle_diff(0) == 0u);

    /* the apps footer (inverted, the fault red today) and the DUT OSD rows */
    s_link_up = 1;
    clcd_test_set_page(CLCD_PAGE_APPS);
    CHECK(run_until_drawn());
    CHECK(row_all_role(14, CLCD_ROLE_BANNER_ERR) && row_all_role(13, CLCD_ROLE_TEXT));
    clcd_test_set_banner(1);
    CHECK(run_until_drawn());
    CHECK(row_all_role(6, CLCD_ROLE_BANNER_HELD) && row_all_role(8, CLCD_ROLE_BANNER_HELD));
    CHECK(oracle_diff(0) == 0u);                 /* banner-held is red today */
    clcd_test_set_banner(0);
    clcd_test_set_page(CLCD_PAGE_STATUS);

    /* the identify banner: banner-busy rows, still today's red */
    s_loc_on = 1; s_loc_who = "alice";
    CHECK(run_until_drawn());
    CHECK(row_all_role(11, CLCD_ROLE_BANNER_BUSY));
    CHECK(strstr(row_of(11), "IDENTIFY: alice") != 0);
    CHECK(oracle_diff(0) == 0u);
    providers_off();
}

/* ==========================================================================
 * The aligned theme: HM's words, roles and pixels; a full repaint on the switch
 * ========================================================================== */
static void t_aligned_status(void)
{
    providers_off();
    healthy_drawn(&clcd_theme_today);
    uint32_t b0 = clcd_test_bytes_total();
    s_theme_p = &clcd_theme_aligned;             /* the switch: every cell again */
    s_eng_on = 1;
    redraw();
    uint32_t pushed = clcd_test_bytes_total() - b0;
    printf("  theme switch: %u bytes (>= 600 cells x 273)\n", pushed);
    CHECK(pushed >= 600u * 273u);
    CHECK(s_max_pass <= CLCD_BYTES_PER_PASS);

    CHECK(strncmp(row_of(0), " MPS3-01", 8) == 0 && row_all_role(0, CLCD_ROLE_TITLE));
    CHECK(row_all_role(1, CLCD_ROLE_RULE) && row_of(1)[39] == '-');
    CHECK(strncmp(row_of(2), "design nanosoc v1.0", 19) == 0);
    CHECK(role_at(2, 0) == CLCD_ROLE_LABEL && role_at(2, 7) == CLCD_ROLE_VALUE &&
          role_at(2, 15) == CLCD_ROLE_LABEL);
    CHECK((unsigned char)row_of(2)[30] == CLCD_GLYPH_OK && strncmp(row_of(2) + 31, "verified", 8) == 0);
    CHECK(role_at(2, 30) == CLCD_ROLE_OK && role_at(2, 38) == CLCD_ROLE_OK);
    CHECK(strncmp(row_of(3), "prog   #001 loaded", 18) == 0 && strncmp(row_of(3) + 32, "last ok", 7) == 0);
    CHECK(strncmp(row_of(4), "shell  0x14E1A2D8 card nanosoc [A]", 34) == 0);
    CHECK(strncmp(row_of(5), "net    192.168.10.101", 21) == 0 && strncmp(row_of(5) + 30, "up 100/FD", 9) == 0);
    CHECK(role_at(5, 31) == CLCD_ROLE_OK);
    CHECK(strncmp(row_of(6), "up     000:00:", 14) == 0);
    CHECK(strncmp(row_of(7), "dut    rst-rel  clk-alive  mmcm-lock", 36) == 0);
    CHECK(strncmp(row_of(8), "icap   1835072 B  rxdrop 0  txerr 0", 35) == 0);
    CHECK(strncmp(row_of(9), "cfg    1x Cortex-M0  no ETH  1x UART", 36) == 0);
    CHECK(strncmp(row_of(12), "sys    linux ssh claimed SHA256:AbCdEfGh", 40) == 0);
    CHECK(row_all_role(13, CLCD_ROLE_RULE) && row_all_role(14, CLCD_ROLE_CHROME));
    CHECK(strncmp(row_of(14), " mac 02:00:00:4D:50:53", 22) == 0 && strncmp(row_of(14) + 35, "hb ", 3) == 0);
    unsigned d = oracle_diff(1);
    printf("  aligned status: %u px differ from the HM-palette oracle\n", d);
    CHECK(d == 0u);
    clcd_panel_state_t ps;
    clcd_panel_state(&ps);
    CHECK(strcmp(ps.theme, "aligned") == 0 && ps.banner == CLCD_BANNER_NONE && ps.prog_pct == 255u);

    /* the fault banner, aligned: banner-err + the cross glyph, same facts */
    s_link_up = 0;
    redraw();
    CHECK(row_all_role(10, CLCD_ROLE_BANNER_ERR) && row_all_role(12, CLCD_ROLE_BANNER_ERR));
    CHECK(strstr(row_of(11), CLCD_GS_ERR " NETWORK LINK DOWN") != 0);
    CHECK(strncmp(row_of(5) + 35, "down", 4) == 0 && role_at(5, 36) == CLCD_ROLE_ERR);
    clcd_panel_state(&ps);
    CHECK(ps.banner == CLCD_BANNER_FAULT && strcmp(ps.banner_text, "NETWORK LINK DOWN") == 0);
    CHECK(oracle_diff(1) == 0u);

    /* the apps page, aligned: lower-case labels, no SWD, ssh, chrome footer */
    s_link_up = 1;
    seed_rm_live(0x01000003u);
    clcd_test_set_page(CLCD_PAGE_APPS);
    CHECK(run_until_drawn());
    CHECK(strncmp(row_of(0), " apps & ports", 13) == 0);
    CHECK(strncmp(row_of(2), "ctrl   nc 192.168.10.101 6900", 29) == 0);
    int swd = 0, ssh = 0;
    for (unsigned r = 2; r <= 12u; r++) {
        swd |= strncmp(row_of(r), "swd", 3) == 0;
        ssh |= strncmp(row_of(r), "ssh    ssh root@192.168.10.101", 30) == 0;
    }
    CHECK(!swd && ssh);
    CHECK(row_all_role(14, CLCD_ROLE_CHROME) && strstr(row_of(14), "PB1 tap: next page") != 0);
    CHECK(oracle_diff(1) == 0u);

    /* the DUT OSD, aligned: held rows + the lock glyph */
    clcd_test_set_banner(1);
    CHECK(run_until_drawn());
    CHECK(row_all_role(7, CLCD_ROLE_BANNER_HELD) && strstr(row_of(6), CLCD_GS_HELD " DUT HAS THE PANEL"));
    CHECK(strstr(row_of(0), "linux harness") != 0);
    clcd_panel_state(&ps);
    CHECK(ps.banner == CLCD_BANNER_DUT);
    CHECK(oracle_diff(1) == 0u);
    clcd_test_set_banner(0);
    providers_off();
}

/* ==========================================================================
 * R5: programming progress -- row 3 + the bar, at 0 / 50 / 100 % and unknown
 * ========================================================================== */
static unsigned bar_px_accent(void)       /* row 10's pixels in the bar colour */
{
    unsigned n = 0;
    for (unsigned y = 160u; y < 176u; y++)
        for (unsigned x = 0; x < 320u; x++) n += s_gram[y][x] == CLCD_RGB565_BAR_BG;
    return n;
}

static void t_progress(void)
{
    providers_off();
    healthy_drawn(&clcd_theme_aligned);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS, DFXCTL_STATUS_DECOUPLED);  /* mid-swap */
    s_prog.active = true;
    s_prog.state = SWAP_AWAIT_PARTIAL;
    s_prog.rm = "nanosoc";
    s_prog.total = 1000000u;
    static const struct { uint32_t done; const char *pct; unsigned cells; } P[] = {
        { 0u, "  0%", 0u }, { 500000u, " 50%", 20u }, { 1000000u, "100%", 40u },
    };
    for (unsigned i = 0; i < 3u; i++) {
        s_prog.done = P[i].done;
        redraw();
        CHECK(strncmp(row_of(3), "prog   pushing nanosoc", 22) == 0);
        CHECK(strncmp(row_of(3) + 35, P[i].pct, 4) == 0);
        CHECK(role_at(3, 7) == CLCD_ROLE_BUSY && role_at(3, 38) == CLCD_ROLE_BUSY);
        CHECK(count_role(CLCD_BAR_ROW, CLCD_ROLE_BAR) == P[i].cells);
        CHECK(count_role(CLCD_BAR_ROW, CLCD_ROLE_TRACK) == 40u - P[i].cells);
        for (unsigned c = 0; c < P[i].cells; c++) CHECK(role_at(CLCD_BAR_ROW, c) == CLCD_ROLE_BAR);
        CHECK(bar_px_accent() == P[i].cells * 8u * 16u);          /* the glass, too */
        CHECK(strncmp(row_of(2), "design partition decoupled", 26) == 0);
        clcd_panel_state_t ps;
        clcd_panel_state(&ps);
        CHECK(ps.prog_pct == (i == 0 ? 0u : i == 1 ? 50u : 100u));
        CHECK(oracle_diff(1) == 0u);
        printf("  progress %s: row 3 \"%.40s\", %u bar cells\n", P[i].pct, row_of(3), P[i].cells);
    }
    /* the total not known yet: no percentage, an empty track, no guess */
    s_prog.total = 0u; s_prog.done = 123456u; s_prog.state = SWAP_AWAIT_INCOMING_CLEARING;
    redraw();
    CHECK(strncmp(row_of(3), "prog   receiving nanosoc", 24) == 0 && strchr(row_of(3), '%') == 0);
    CHECK(count_role(CLCD_BAR_ROW, CLCD_ROLE_TRACK) == 40u);
    clcd_panel_state_t ps;
    clcd_panel_state(&ps);
    CHECK(ps.prog_pct == 255u);

    /* pure helpers */
    CHECK(clcd_prog_pct(0, 0) == 255u && clcd_prog_pct(1, 3) == 33u && clcd_prog_pct(9, 3) == 100u);
    CHECK(clcd_prog_pct(0xFFFFFFFFu, 0xFFFFFFFFu) == 100u && clcd_prog_pct(0x7FFFFFFFu, 0xFFFFFFFEu) == 50u);
    CHECK(clcd_bar_cells(0) == 0u && clcd_bar_cells(1) == 0u && clcd_bar_cells(2) == 1u &&
          clcd_bar_cells(50) == 20u && clcd_bar_cells(100) == 40u && clcd_bar_cells(255) == 0u);

    /* the today theme never draws it (bare metal's row 3 is unchanged) */
    s_prog.total = 1000000u; s_prog.done = 500000u;
    s_theme_p = &clcd_theme_today;
    redraw();
    CHECK(strncmp(row_of(3), "SWAP: ", 6) == 0 && count_role(CLCD_BAR_ROW, CLCD_ROLE_BAR) == 0u);
    providers_off();
}

/* NEGATIVE CONTROL for the bar: it must never overdraw a banner. Each banner in
 * turn, during a swap at 50 %: rows 10-12 are the banner's role, and no cell on
 * the panel carries the bar's roles. Then the banner goes and the bar is back --
 * so this cannot pass by never drawing a bar. */
static void t_bar_yields_to_banners(void)
{
    providers_off();
    healthy_drawn(&clcd_theme_aligned);
    s_prog.active = true; s_prog.state = SWAP_STREAM_PARTIAL; s_prog.rm = "nanosoc";
    s_prog.done = 500000u; s_prog.total = 1000000u;
    for (int k = 0; k < 3; k++) {
        unsigned want = CLCD_ROLE_BANNER_ERR;
        if (k == 0) s_link_up = 0;
        if (k == 1) { s_loc_on = 1; s_loc_who = "alice@lab-pc01"; want = CLCD_ROLE_BANNER_BUSY; }
        if (k == 2) {
            s_ovl_on = 1;
            memset(&s_ovl, 0, sizeof(s_ovl));
            snprintf(s_ovl.line[0], sizeof(s_ovl.line[0]), CLCD_GS_HELD " bob@lab-pc02 wants this board");
            s_ovl.role = CLCD_ROLE_BANNER_HELD; s_ovl.on = CLCD_ON_REQUEST;
            want = CLCD_ROLE_BANNER_HELD;
        }
        redraw();
        unsigned bar = 0;
        for (unsigned r = 0; r < CLCD_ROWS; r++)
            bar += count_role(r, CLCD_ROLE_BAR) + count_role(r, CLCD_ROLE_TRACK);
        CHECK(row_all_role(10, want) && row_all_role(11, want) && row_all_role(12, want));
        CHECK(bar == 0u);
        CHECK(k != 0 || strstr(row_of(11), "NETWORK LINK DOWN") != 0);   /* the text, too */
        CHECK(k != 1 || strstr(row_of(11), "IDENTIFY: alice@lab-pc01") != 0);
        CHECK(k != 2 || strstr(row_of(10), "bob@lab-pc02 wants this board") != 0);
        CHECK(bar_px_accent() == 0u);
        CHECK(oracle_diff(1) == 0u);
        if (k == 0) s_link_up = 1;
        if (k == 1) s_loc_on = 0;
    }
    s_ovl_on = 0;
    redraw();
    CHECK(count_role(CLCD_BAR_ROW, CLCD_ROLE_BAR) == 20u);    /* the control is live */
    providers_off();
}

/* ==========================================================================
 * R2: the seams -- badge, session row, overlay; ranking and yielding
 * ========================================================================== */
static void t_seams(void)
{
    providers_off();
    healthy_drawn(&clcd_theme_aligned);

    s_badge_on = 1;
    snprintf(s_badge.text, sizeof(s_badge.text), CLCD_GS_HELD " alice 1h12m, 1 waiting");
    s_badge.role = CLCD_ROLE_TITLE_HELD;
    redraw();
    CHECK(strncmp(row_of(0) + 15, CLCD_GS_HELD " alice 1h12m, 1 waiting", 24) == 0);
    CHECK(role_at(0, 15) == CLCD_ROLE_TITLE_HELD && role_at(0, 38) == CLCD_ROLE_TITLE_HELD);
    CHECK(role_at(0, 39) == CLCD_ROLE_TITLE && row_of(0)[39] == ' ');
    clcd_set_board_name("abcdefghijklmnop");                  /* 16: the N1 maximum */
    redraw();
    CHECK(strncmp(row_of(0), " abcdefghijklmnop ", 18) == 0);  /* the name wins      */
    CHECK((unsigned char)row_of(0)[18] == CLCD_GLYPH_HELD && row_of(0)[39] == ' ');
    clcd_set_board_name("mps3-01");

    s_sess_on = 1;
    clcd_line_clear(&s_sess);
    clcd_line_put(&s_sess, 0, "hm", CLCD_ROLE_LABEL);
    unsigned c = clcd_line_put(&s_sess, 7, CLCD_GS_USER "alice@lab-pc01", CLCD_ROLE_VALUE);
    clcd_line_put(&s_sess, c + 2u, "+1 watching", CLCD_ROLE_LABEL);
    clcd_line_right(&s_sess, "x", CLCD_ROLE_OK, 1);
    redraw();
    CHECK(strncmp(row_of(11), "hm     " CLCD_GS_USER "alice@lab-pc01  +1 watching", 39 - 13) == 0);
    CHECK(role_at(11, 0) == CLCD_ROLE_LABEL && role_at(11, 7) == CLCD_ROLE_VALUE &&
          role_at(11, 24) == CLCD_ROLE_LABEL && row_of(11)[38] == 'x' && role_at(11, 38) == CLCD_ROLE_OK);
    CHECK(oracle_diff(1) == 0u);

    /* the overlay outranks the session row and the engine row; faults outrank it */
    s_ovl_on = 1;
    memset(&s_ovl, 0, sizeof(s_ovl));
    snprintf(s_ovl.line[0], sizeof(s_ovl.line[0]), CLCD_GS_HELD " bob@lab-pc02 wants this board");
    snprintf(s_ovl.line[1], sizeof(s_ovl.line[1]), "held by alice  1:43 to answer");
    snprintf(s_ovl.line[2], sizeof(s_ovl.line[2]), "tap: tell alice you are here");
    s_ovl.role = CLCD_ROLE_TEXT;                              /* not a banner role: */
    s_ovl.on = CLCD_ON_REQUEST;
    redraw();
    CHECK(row_all_role(10, CLCD_ROLE_BANNER_HELD));           /* ...held is used    */
    CHECK(strstr(row_of(11), "held by alice") != 0 && strstr(row_of(12), "tap: tell alice") != 0);
    clcd_panel_state_t ps;
    clcd_panel_state(&ps);
    CHECK(ps.banner == CLCD_BANNER_OVERLAY && strcmp(ps.banner_text, "bob@lab-pc02 wants this board") == 0);
    s_loc_on = 1; s_loc_who = "alice";                        /* identify outranks it */
    redraw();
    clcd_panel_state(&ps);
    CHECK(ps.banner == CLCD_BANNER_LOCATE && row_all_role(11, CLCD_ROLE_BANNER_BUSY));
    CHECK(strstr(row_of(11), CLCD_GS_USER " IDENTIFY: alice") != 0);
    s_link_up = 0;                                            /* ...and a fault both */
    redraw();
    clcd_panel_state(&ps);
    CHECK(ps.banner == CLCD_BANNER_FAULT);
    s_link_up = 1; s_loc_on = 0; s_ovl_on = 0;
    redraw();
    CHECK(strncmp(row_of(11), "hm", 2) == 0);                 /* the row is back */

    /* the today theme with a badge: it replaces "nanoSoC harness" */
    s_theme_p = &clcd_theme_today;
    redraw();
    CHECK(strstr(row_of(0), "nanoSoC") == 0 && strstr(row_of(0), "alice 1h12m") != 0);
    CHECK(oracle_diff(0) == 0u);
    providers_off();
}

/* ==========================================================================
 * R2: the page-aware hit test + the event ring
 * ========================================================================== */
#define PX(col, row) ((col) * 8u + 3u), ((row) * 16u + 5u)

static void t_hittest_and_events(void)
{
    providers_off();
    healthy_drawn(&clcd_theme_aligned);
    clcd_event_t ev[CLCD_EVENT_RING];
    CHECK(clcd_event_seq() == 0u && clcd_events_since(0, ev, 8) == 0u);

    /* status page: row 14 = next page (nav), elsewhere nothing, no event */
    CHECK(clcd_hittest(PX(3, 14)) == CLCD_ACT_NEXT_PAGE);
    CHECK(clcd_event_seq() == 1u && clcd_events_since(0, ev, 8) == 1u);
    CHECK(ev[0].seq == 1u && ev[0].on == CLCD_ON_NAV && ev[0].kind == CLCD_EV_TAP &&
          ev[0].page == CLCD_PAGE_STATUS && ev[0].col == 3u && ev[0].row == 14u);
    CHECK(clcd_hittest(PX(3, 5)) == CLCD_ACT_NONE && clcd_hittest(PX(20, 11)) == CLCD_ACT_NONE);
    CHECK(clcd_event_seq() == 1u);
    CHECK(clcd_hittest(400u, 100u) == CLCD_ACT_NONE && clcd_event_seq() == 1u);   /* off-glass */

    /* apps page: its own table */
    clcd_test_set_page(CLCD_PAGE_APPS);
    CHECK(run_until_drawn());
    CHECK(clcd_hittest(PX(39, 14)) == CLCD_ACT_NEXT_PAGE);
    CHECK(clcd_events_since(1, ev, 8) == 1u && ev[0].page == CLCD_PAGE_APPS && ev[0].on == CLCD_ON_NAV);

    /* the KVM OSD: nothing to tap, no event */
    clcd_test_set_banner(1);
    CHECK(run_until_drawn());
    CHECK(clcd_hittest(PX(3, 14)) == CLCD_ACT_NONE && clcd_event_seq() == 2u);
    clcd_test_set_banner(0);
    clcd_test_set_page(CLCD_PAGE_STATUS);
    CHECK(run_until_drawn());

    /* an overlay with a target: rows 10-12 = CLCD_ACT_BANNER + {on: request} */
    s_ovl_on = 1;
    memset(&s_ovl, 0, sizeof(s_ovl));
    snprintf(s_ovl.line[1], sizeof(s_ovl.line[1]), "bob wants this board");
    s_ovl.role = CLCD_ROLE_BANNER_HELD; s_ovl.on = CLCD_ON_REQUEST;
    redraw();
    mock_time_set_ms(900000u);
    CHECK(clcd_hittest(PX(12, 11)) == CLCD_ACT_BANNER);
    CHECK(clcd_events_since(2, ev, 8) == 1u && ev[0].on == CLCD_ON_REQUEST && ev[0].row == 11u &&
          ev[0].t_ms == 900000u);
    CHECK(strcmp(clcd_event_on_name(ev[0].on), "request") == 0);
    CHECK(clcd_hittest(PX(12, 14)) == CLCD_ACT_NEXT_PAGE);      /* the nav still works */
    s_ovl.on = CLCD_ON_NONE;                                    /* no target: no event */
    redraw();
    uint32_t before = clcd_event_seq();
    CHECK(clcd_hittest(PX(12, 11)) == CLCD_ACT_NONE && clcd_event_seq() == before);
    s_ovl_on = 0;

    /* a locate: the engine takes every tap ("found it") -> {on: identify} */
    s_loc_on = 1; s_tap_take = 1; s_loc_who = "alice";
    redraw();
    CHECK(clcd_hittest(PX(3, 14)) == CLCD_ACT_NONE);
    CHECK(clcd_events_since(before, ev, 8) == 1u && ev[0].on == CLCD_ON_IDENTIFY);
    CHECK(strcmp(clcd_event_on_name(CLCD_ON_IDENTIFY), "identify") == 0 &&
          strcmp(clcd_event_on_name(CLCD_ON_NAV), "nav") == 0 && clcd_event_on_name(99)[0] == '\0');
    s_loc_on = 0; s_tap_take = 0;
    redraw();

    /* THE RING: wrap past 8, seq keeps rising, oldest first, readers never consume */
    uint32_t base = clcd_event_seq();
    for (unsigned i = 0; i < 11u; i++) {
        mock_time_set_ms(1000000u + i);
        CHECK(clcd_hittest(PX(i, 14)) == CLCD_ACT_NEXT_PAGE);
    }
    CHECK(clcd_event_seq() == base + 11u);
    unsigned n = clcd_events_since(0, ev, 8);
    CHECK(n == 8u);
    for (unsigned i = 0; i < n; i++) {
        CHECK(ev[i].seq == base + 4u + i);                     /* the last 8 */
        CHECK(ev[i].t_ms == 1000000u + 3u + i && ev[i].col == 3u + i);
    }
    CHECK(clcd_events_since(base + 9u, ev, 8) == 2u && ev[0].seq == base + 10u && ev[1].seq == base + 11u);
    CHECK(clcd_events_since(base + 9u, ev, 8) == 2u);          /* a second reader: same */
    CHECK(clcd_events_since(0, ev, 3) == 3u && ev[0].seq == base + 4u && ev[2].seq == base + 6u);
    CHECK(clcd_events_since(base + 11u, ev, 8) == 0u);
    CHECK(clcd_events_since(base + 50u, ev, 8) == 0u);         /* a reader from before a restart */
    clcd_panel_state_t ps;
    clcd_panel_state(&ps);
    CHECK(ps.event_seq == base + 11u);
    providers_off();
}

/* ==========================================================================
 * R2: frame + roles by row range (HM asks rows 0-7 and 8-14), JSON, state
 * ========================================================================== */
static void t_frame_access(void)
{
    providers_off();
    healthy_drawn(&clcd_theme_aligned);
    char rows[CLCD_ROWS][CLCD_COLS + 1];
    char codes[CLCD_NCELLS + 1];
    char cells[CLCD_NCELLS];
    uint8_t inv[CLCD_ROWS], roles[CLCD_NCELLS];
    clcd_test_shadow(cells, inv);
    clcd_test_roles(roles);

    CHECK(clcd_frame_rows(0, 8, rows, codes) == 8u && strlen(codes) == 320u);
    for (unsigned r = 0; r < 8u; r++) {
        CHECK(memcmp(rows[r], cells + r * CLCD_COLS, CLCD_COLS) == 0 && rows[r][CLCD_COLS] == '\0');
        for (unsigned c = 0; c < CLCD_COLS; c++)
            CHECK(codes[r * CLCD_COLS + c] == CLCD_ROLE_CODE(roles[r * CLCD_COLS + c]));
    }
    CHECK(clcd_frame_rows(8, 7, rows, codes) == 7u && strlen(codes) == 280u);
    CHECK(memcmp(rows[0], cells + 8 * CLCD_COLS, CLCD_COLS) == 0);
    CHECK(clcd_frame_rows(8, 8, rows, codes) == 7u && strlen(codes) == 280u);   /* clipped */
    CHECK(clcd_frame_rows(15, 1, rows, codes) == 0u && codes[0] == '\0');
    CHECK(clcd_frame_rows(0, 15, 0, 0) == 15u);
    CHECK(clcd_frame_rows(0, 15, 0, codes) == 15u && strlen(codes) == 600u);
    for (unsigned i = 0; i < 600u; i++) CHECK(codes[i] >= 'a' && codes[i] <= 'u');

    char j[CLCD_COLS * 6u + 1u];
    const char in[6] = { 'a', '"', '\\', (char)0x80, (char)0x01, (char)0x86 };
    CHECK(clcd_fmt_row_json(j, sizeof(j), in, 6) == 18u);
    CHECK(strcmp(j, "a\\\"\\\\\\u0080 \\u0086") == 0);
    CHECK(clcd_fmt_row_json(j, 10, in, 6) == 0u && j[0] == '\0');   /* cap too small */
    char all[CLCD_COLS];
    memset(all, (char)0x83, sizeof(all));
    CHECK(clcd_fmt_row_json(j, sizeof(j), all, CLCD_COLS) == 240u);  /* the worst row */

    /* frame_seq: moves on a change, not on an identical frame (the apps page) */
    clcd_test_set_page(CLCD_PAGE_APPS);
    CHECK(run_until_drawn());
    clcd_panel_state_t a, b;
    clcd_panel_state(&a);
    redraw();
    clcd_panel_state(&b);
    CHECK(a.frame_seq == b.frame_seq && b.page == CLCD_PAGE_APPS);
    s_usd = "led [B]";
    clcd_test_set_page(CLCD_PAGE_STATUS);
    CHECK(run_until_drawn());
    clcd_panel_state(&b);
    CHECK(b.frame_seq > a.frame_seq && b.page == CLCD_PAGE_STATUS && b.relinquished == 0u);
    CHECK(strcmp(clcd_page_name(CLCD_PAGE_APPS), "apps") == 0 && clcd_page_name(9)[0] == '\0');
    CHECK(clcd_page_by_name("status") == CLCD_PAGE_STATUS && clcd_page_by_name("apps") == CLCD_PAGE_APPS &&
          clcd_page_by_name("nope") == -1 && clcd_page_by_name(0) == -1);

    /* line helpers clip at 40 */
    clcd_line_t l;
    clcd_line_clear(&l);
    CHECK(clcd_line_put(&l, 35, "abcdefgh", CLCD_ROLE_OK) == 40u && l.text[39] == 'e' && l.text[40] == '\0');
    CHECK(l.role[39] == CLCD_ROLE_OK && l.role[34] == CLCD_ROLE_TEXT);
    providers_off();
}

int main(void)
{
    printf("test_clcd_roles: R4 roles/palette/glyphs, R5 progress, R2 seams/hit test/events/frame\n");
    t_golden_through_the_seam();
    t_role_plane_today();
    t_aligned_status();
    t_progress();
    t_bar_yields_to_banners();
    t_seams();
    t_hittest_and_events();
    t_frame_access();
    printf("test_clcd_roles: %d checks, %d failed\n", s_checks, s_fails);
    return s_fails ? 1 : 0;
}
#endif
