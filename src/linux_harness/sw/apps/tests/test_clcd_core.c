/*
 * test_clcd_core.c — host tests for the ported CLCD driver, against the
 * mocked register block (mock_clcd.c). What is proven here:
 *
 *  1. Reset sequence: proven TIMING (4/4/2) programmed; CTRL sequence is
 *     enable+fifo_reset (panel held in reset) then enable+reset_n+backlight.
 *  2. The HX8347 init table is streamed byte-exact, in order, with correct
 *     RS, BEFORE any GRAM traffic.
 *  3. FIFO discipline: with a non-draining FIFO the driver never triggers
 *     the block's drop policy (mock drops == 0, level capped at depth) and
 *     resumes cleanly when draining restarts.
 *  4. Per-pass byte budget is never exceeded.
 *  5. Full-screen paint = exactly 600 cells x 273 bytes after the init
 *     stream, and the GRAM-decoded framebuffer matches the shadow text
 *     glyph-for-glyph (windowing, RAMWR auto-increment, RGB565 MSB-first).
 *  6. Screen content: SID/NET/UP/DUT rows render the seeded status; the
 *     error banner appears inverted (RED background) when link is down,
 *     and the healthy screen contains no red pixel anywhere.
 *  7. Steady state: an unchanged status repaints only the heartbeat cell.
 *  8. Formatter bounds: every rm name/caps/version fits its declared field
 *     over all 65536 design ids (the firmware's own overflow regression).
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "../clcd_core.h"
#include "../clcd_regs.h"
#include "../hx8347_init.h"
#include "../font8x16.h"
#include "mock_clcd.h"

#define RGB_WHITE 0xFFFFu
#define RGB_BLACK 0x0000u
#define RGB_RED   0xF800u

static mock_clcd_t g_mock;
static int g_checks;

#define CHECK(cond, ...) do { \
    g_checks++; \
    if (!(cond)) { \
        fprintf(stderr, "FAIL %s:%d: ", __FILE__, __LINE__); \
        fprintf(stderr, __VA_ARGS__); \
        fprintf(stderr, "\n"); \
        return 1; \
    } \
} while (0)

static clcd_status_t healthy_status(void)
{
    clcd_status_t st;
    memset(&st, 0, sizeof(st));
    snprintf(st.board_name, sizeof(st.board_name), "MPS3-01");
    st.static_id = 0x14E1A2D8u;              /* the 1 MiB shell */
    st.uptime_ms = (uint64_t)((1*86400u + 2*3600u + 3*60u + 4u)) * 1000u;
    snprintf(st.ip, sizeof(st.ip), "192.168.10.101");
    st.link_up = 1;
    st.speed_mbps = 100;
    st.full_duplex = 1;
    st.mac[0]=0x02; st.mac[1]=0x00; st.mac[2]=0x00;
    st.mac[3]=0x4D; st.mac[4]=0x50; st.mac[5]=0x53;
    st.mac_valid = 1;
    st.dfx_present = 1;
    st.swap_state = 10;                       /* SWAP_DONE (frozen value) */
    st.rm_id = 0x010000B2u;                   /* regdemo_b v1.0 */
    st.rm_id_valid = 1;
    st.icap_bytes = 123456;
    st.rx_dropped = 7;
    st.tx_errors = 1;
    snprintf(st.kernel, sizeof(st.kernel), "6.18.7");
    return st;
}

/* Run the driver until idle, draining freely. Returns passes used. The
 * per-pass budget is tracked in g_max_pass_bytes and asserted by main(). */
static unsigned g_max_pass_bytes;

static unsigned run_to_idle(uint64_t *now, unsigned max_passes)
{
    unsigned pass, quiet = 0;
    for (pass = 0; pass < max_passes; pass++) {
        clcd_core_poll(*now);
        if (clcd_core_bytes_last_pass() > g_max_pass_bytes)
            g_max_pass_bytes = clcd_core_bytes_last_pass();
        mock_clcd_drain(&g_mock, 100000);
        *now += 1;   /* 1 ms steps: a full paint stays inside one refresh
                      * period, so "first paint" is exactly one frame */
        if (clcd_core_state() == CLCD_ST_IDLE &&
            clcd_core_dirty_count() == 0 &&
            clcd_core_bytes_last_pass() == 0) {
            if (++quiet >= 3 && pass > 5)
                break;
        } else {
            quiet = 0;
        }
    }
    return pass;
}

static int frame_has(const char *frame, const char *needle)
{
    /* search row-wise (rows are not NUL-terminated) */
    size_t nl = strlen(needle);
    for (unsigned r = 0; r < CLCD_ROWS; r++) {
        const char *row = frame + r * CLCD_COLS;
        for (unsigned c = 0; c + nl <= CLCD_COLS; c++)
            if (memcmp(row + c, needle, nl) == 0)
                return 1;
    }
    return 0;
}

/* Compare one fb cell against the glyph the shadow says is there. */
static int cell_matches(const mock_clcd_t *m, const char *shadow,
                        const uint8_t *inv, unsigned cell)
{
    unsigned r = cell / CLCD_COLS, c = cell % CLCD_COLS;
    unsigned ch = (unsigned char)shadow[cell];
    if (ch < CLCD_FONT_FIRST || ch > CLCD_FONT_LAST)
        ch = ' ';
    const uint8_t *glyph = font8x16[ch - CLCD_FONT_FIRST];
    uint16_t fg = RGB_WHITE;
    uint16_t bg = inv[r] ? RGB_RED : RGB_BLACK;
    for (unsigned yy = 0; yy < CLCD_GLYPH_H; yy++)
        for (unsigned xx = 0; xx < CLCD_GLYPH_W; xx++) {
            uint16_t want = (glyph[yy] & (0x80u >> xx)) ? fg : bg;
            if (m->fb[r * CLCD_GLYPH_H + yy][c * CLCD_GLYPH_W + xx] != want)
                return 0;
        }
    return 1;
}

int main(void)
{
    uint64_t now = 1000;

    /* ---- boot with a healthy status ------------------------------------ */
    mock_clcd_init(&g_mock);
    clcd_io_t io = { mock_clcd_read32, mock_clcd_write32, &g_mock };
    clcd_core_init(&io, "MPS3-01");
    clcd_status_t st = healthy_status();
    clcd_core_update_status(&st);

    /* first poll = ST_RESET register writes */
    clcd_core_poll(now);
    CHECK(g_mock.timing == CLCD_TIMING_PROVEN,
          "TIMING not the proven 4/4/2: 0x%08x", g_mock.timing);
    CHECK(g_mock.ctrl == CLCD_CTRL_ENABLE,
          "CTRL after reset-entry should be ENABLE (reset held): 0x%08x",
          g_mock.ctrl);

    /* reset pulse elapses -> release + backlight */
    now += 5;
    clcd_core_poll(now);
    CHECK(g_mock.ctrl == (CLCD_CTRL_ENABLE | CLCD_CTRL_RESET_N | CLCD_CTRL_BACKLIGHT),
          "CTRL after release: 0x%08x", g_mock.ctrl);

    /* run through init + first full paint */
    unsigned passes = run_to_idle(&now, 100000);
    CHECK(passes < 100000, "never reached idle");
    CHECK(g_mock.drops == 0, "driver triggered FIFO drops: %u", g_mock.drops);
    CHECK(g_max_pass_bytes <= CLCD_BYTES_PER_PASS,
          "budget exceeded: %u", g_max_pass_bytes);

    /* ---- init table byte-exact prefix ----------------------------------- */
    {
        unsigned si = 0;
        for (unsigned i = 0; i < hx8347_init_len; i++) {
            if (hx8347_init[i].op == HX_DLY)
                continue;
            CHECK(si < g_mock.len, "stream shorter than init table");
            CHECK(g_mock.rs[si] == (hx8347_init[i].op == HX_DAT ? 1 : 0),
                  "init[%u]: RS mismatch at stream %u", i, si);
            CHECK(g_mock.val[si] == hx8347_init[i].val,
                  "init[%u]: byte mismatch %02x != %02x",
                  i, g_mock.val[si], hx8347_init[i].val);
            si++;
        }
        /* full first paint = init prefix + 600 cells x 273 bytes */
        CHECK(g_mock.len == si + CLCD_NCELLS * CLCD_CELL_BYTES,
              "first paint stream length %u != %u + %u*273",
              g_mock.len, si, CLCD_NCELLS);
    }

    /* ---- decoded framebuffer == shadow, glyph for glyph ------------------ */
    {
        char shadow[CLCD_NCELLS];
        uint8_t inv[CLCD_ROWS];
        clcd_core_shadow(shadow, inv);

        for (unsigned cell = 0; cell < CLCD_NCELLS; cell++)
            CHECK(cell_matches(&g_mock, shadow, inv, cell),
                  "fb mismatch at cell %u ('%c')", cell,
                  shadow[cell] >= 0x20 && shadow[cell] < 0x7f ? shadow[cell] : '?');

        /* screen content spot checks */
        CHECK(frame_has(shadow, "SID : 0x14E1A2D8"), "SID row wrong");
        CHECK(frame_has(shadow, "192.168.10.101  UP 100/FD"), "NET row wrong");
        CHECK(frame_has(shadow, "UP  : 001:02:03:04"), "UP row wrong");
        CHECK(frame_has(shadow, "regdemo_b"), "DUT name missing");
        CHECK(frame_has(shadow, "v1.0"), "DUT version missing");
        CHECK(frame_has(shadow, "SWAP: DONE"), "SWAP row wrong");
        CHECK(frame_has(shadow, "MAC 02:00:00:4D:50:53"), "MAC row wrong");
        CHECK(frame_has(shadow, "OS  : Linux 6.18.7"), "OS row wrong");
        CHECK(frame_has(shadow, "no CPU  AHB register demo"), "CFG row wrong");
        CHECK(!frame_has(shadow, "NETWORK LINK DOWN"), "banner on healthy screen");

        /* healthy screen: not one red pixel anywhere (banner absent) */
        for (unsigned y = 0; y < 240; y++)
            for (unsigned x = 0; x < 320; x++)
                CHECK(g_mock.fb[y][x] != RGB_RED,
                      "red pixel on healthy screen at %u,%u", x, y);
    }

    /* ---- steady state: only the heartbeat cell repaints ------------------- */
    {
        unsigned len_before = g_mock.len;
        now += CLCD_REFRESH_MS + 1;
        clcd_core_poll(now);              /* reformat: hb spinner moved */
        mock_clcd_drain(&g_mock, 100000);
        now += 10;
        clcd_core_poll(now);              /* render the dirty cell(s) */
        mock_clcd_drain(&g_mock, 100000);
        unsigned delta = g_mock.len - len_before;
        CHECK(delta == CLCD_CELL_BYTES,
              "steady-state repaint pushed %u bytes (want one cell = %u)",
              delta, (unsigned)CLCD_CELL_BYTES);
    }

    /* ---- FIFO-full: driver must stall, never drop ------------------------- */
    {
        st.uptime_ms += 61000;           /* several cells change */
        st.icap_bytes += 999;
        clcd_core_update_status(&st);
        g_mock.auto_drain = 0;           /* panel stalls: nothing drains */
        unsigned drained_frozen = g_mock.drained;

        /* no draining at all for many passes */
        for (unsigned i = 0; i < 200; i++) {
            now += CLCD_REFRESH_MS + 1;
            clcd_core_poll(now);
            CHECK(g_mock.drops == 0, "drop with full FIFO at pass %u", i);
            CHECK(g_mock.level <= MOCK_FIFO_DEPTH, "level overran depth");
        }
        CHECK(g_mock.level == MOCK_FIFO_DEPTH,
              "FIFO should be exactly full while stalled (level %u)",
              g_mock.level);
        CHECK(g_mock.drained == drained_frozen, "mock drained by itself");

        /* drain resumes -> render completes cleanly */
        g_mock.auto_drain = 16;
        unsigned p = run_to_idle(&now, 100000);
        CHECK(p < 100000, "never recovered from FIFO stall");
        CHECK(g_mock.drops == 0, "drops after recovery: %u", g_mock.drops);

        char shadow[CLCD_NCELLS];
        uint8_t inv[CLCD_ROWS];
        clcd_core_shadow(shadow, inv);
        CHECK(frame_has(shadow, "UP  : 001:02:04:05"), "uptime not repainted");
        for (unsigned cell = 0; cell < CLCD_NCELLS; cell++)
            CHECK(cell_matches(&g_mock, shadow, inv, cell),
                  "fb mismatch after stall-recovery at cell %u", cell);
    }

    /* ---- banner: link down -> inverted red rows --------------------------- */
    {
        st.link_up = 0;
        clcd_core_update_status(&st);
        now += CLCD_REFRESH_MS + 1;
        unsigned p = run_to_idle(&now, 100000);
        CHECK(p < 100000, "banner render never settled");

        char shadow[CLCD_NCELLS];
        uint8_t inv[CLCD_ROWS];
        clcd_core_shadow(shadow, inv);
        CHECK(frame_has(shadow, "NETWORK LINK DOWN"), "banner text missing");
        CHECK(inv[10] && inv[11] && inv[12], "banner rows not inverted");
        for (unsigned cell = 0; cell < CLCD_NCELLS; cell++)
            CHECK(cell_matches(&g_mock, shadow, inv, cell),
                  "fb mismatch with banner at cell %u", cell);
        /* the banner row background really is RED on the glass */
        CHECK(g_mock.fb[11 * 16 + 8][0] == RGB_RED, "banner bg not red");
    }

    /* ---- no-driver / not-provisioned honesty ------------------------------ */
    {
        clcd_status_t s2 = healthy_status();
        s2.dfx_present = 0;
        s2.static_id = 0;
        char frame[CLCD_NCELLS];
        uint8_t inv[CLCD_ROWS];
        clcd_build_frame(&s2, 0, frame, inv);
        CHECK(frame_has(frame, "NO DFX DRIVER"), "no-driver row missing");
        CHECK(frame_has(frame, "SWAP: no dfx driver"), "no-driver swap row");
        CHECK(frame_has(frame, CLCD_RM_CAPS_NODRIVER), "no-driver caps row");
        CHECK(frame_has(frame, "STATIC_ID NOT PROVISIONED"),
              "unprovisioned banner missing");

        s2 = healthy_status();
        s2.dfx_present = 1;
        s2.rm_id_valid = 0;
        s2.swap_state = 11;   /* SWAP_FAILED */
        clcd_build_frame(&s2, 0, frame, inv);
        CHECK(frame_has(frame, "RM ID NOT VALID"), "unqualified id row");
        CHECK(frame_has(frame, "LAST SWAP FAILED - RM NOT LOADED"),
              "failed-swap banner missing");
        CHECK(frame_has(frame, "SWAP: FAILED"), "failed swap row");
    }

    /* ---- formatter bounds over ALL design ids (firmware regression) ------- */
    {
        for (uint32_t d = 0; d <= 0xFFFFu; d++) {
            uint32_t rm = 0x01000000u | d;   /* v1.0, any design */
            CHECK(strlen(clcd_rm_name(rm)) <= CLCD_RM_NAME_MAX,
                  "rm name too long for design 0x%04x", d);
            CHECK(strlen(clcd_rm_caps(rm)) <= 34u,
                  "rm caps too long for design 0x%04x", d);
        }
        char v[16];
        clcd_fmt_rm_version(v, 0xFFFF0000u | 0x1234u);
        CHECK(strlen(v) <= CLCD_RM_VER_MAX, "version too long: %s", v);
        CHECK(!strcmp(clcd_rm_name(0x010000A1u), "regdemo_a"), "rm_id A name");
        CHECK(!strcmp(clcd_rm_name(0x02050003u), "nanosoc_multicore"),
              "re-versioned id must keep its name");
        CHECK(!strcmp(clcd_rm_name(0x4543484Fu), "rm?4543484F"),
              "pre-v2 uart_echo must render raw");
    }

    /* ---- uptime formatter edges ------------------------------------------- */
    {
        char b[16];
        clcd_fmt_uptime(b, 0);
        CHECK(!strcmp(b, "000:00:00:00"), "uptime 0: %s", b);
        clcd_fmt_uptime(b, 49u * 86400000ull + 3600000u * 23u);
        CHECK(!strcmp(b, "049:23:00:00"), "uptime 49d: %s", b);
        clcd_fmt_uptime(b, 1000ull * 86400000ull);   /* > 999 days clamps */
        CHECK(!strncmp(b, "999:", 4), "uptime clamp: %s", b);
        clcd_fmt_hex32(b, 0xE4B1C44Au);
        CHECK(!strcmp(b, "0xE4B1C44A"), "hex32: %s", b);
    }

    printf("test_clcd_core: PASS (%d checks)\n", g_checks);
    return 0;
}
