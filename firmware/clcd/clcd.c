/*
 * clcd.c -- cooperative HX8347-D status-display driver + software text renderer.
 * See clcd.h (public API + the HAZARD note), docs/CLCD_STATUS_DISPLAY_PLAN.md
 * (sections 4/7/8/9) and fpga/shell/ip/clcd/README.md (FROZEN port/register
 * contract + init-table seam).
 *
 * Split of responsibilities:
 *   - The CLCD RTL block is a protocol-agnostic AXI4-Lite->8080 byte streamer.
 *     This driver pushes {RS,byte} pairs through CLCD_CMD / CLCD_DATA and paces
 *     itself on CLCD_STATUS.fifo_full. It NEVER back-pressures the bus.
 *   - The HX8347-D *panel* init/GRAM/pixel-format sequence is a ported data
 *     table consumed through the hx8347_init.h seam (a sibling agent owns
 *     firmware/clcd/hx8347_init.{h,c}). This driver streams it verbatim and
 *     never inspects the values; it tolerates hx8347_init_len == 0.
 *   - The per-cell GRAM windowing this renderer emits (below) uses the standard
 *     HX8347-D column/row-address + RAMWR register opcodes. Those ARE panel
 *     facts, but they are a runtime addressing operation, not part of the init
 *     table, so they live here -- flagged UNPROVEN until the panel lights
 *     (plan sections 2 / 13-Q2/Q6). The bench proves the block streams an
 *     arbitrary sequence faithfully; that these opcodes are right is a board
 *     fact.
 */
#include <stdint.h>
#include <string.h>

#include "clcd.h"
#include "font8x16.h"
#include "clcd_glyphs.h"                /* status glyphs 0x80-0x86 (HM, vendored)  */
#include "hx8347_init.h"                 /* the FROZEN init-table seam */
#include "../clcd_kvm/clcd_kvm.h"        /* the KVM handover mechanism (no-op w/o KVM)*/
#ifdef MPS3_HAS_TOUCH
#include "../touch/touch.h"              /* resistive-touch page nav (dormant w/o TOUCH)*/
#endif

#include "../common/platform_regs.h"     /* MPS3_CLCD_BASE, CLCD_*, CLKRST_*   */
#include "../common/timebase.h"          /* mps3_sys_now_ms (wrap-safe subtract)*/
#include "../common/service.h"           /* mps3_sys_now_us (clcd_poll_drain)   */
#include "../common/net_proto.h"         /* MPS3_DEFAULT_IP_* (board-name/IP)   */
#include "../common/diag.h"              /* g_mps3_diag (read via the SYMBOL)   */
#include "../coordinator/coordinator.h"  /* g_shell_state.static_id/current_rm_id*/
#include "../coordinator/swap_fsm.h"     /* swap_fsm_last_result / _icap_bytes  */
#include "../smsc911x/smsc911x.h"        /* smsc911x_link_up / _mii_read        */

/* MAC for the bottom row. Declared here rather than via net_if_lwip.h (that is
 * a platform-private lwIP header -- firmware modules must not include it). On
 * target this links against net_if_lwip.c's weak mps3_platform_mac(); the host
 * test provides its own definition. */
void mps3_platform_mac(uint8_t mac[6]);

/* ==========================================================================
 * Tunables
 * ========================================================================== */
/* Reset-pulse width, active-low. This is the ONLY panel timing the driver owns,
 * and only because CLCD_RST is a CTRL register bit it toggles directly (not a
 * FIFO byte the init table can carry). The HX8347-D minimum reset pulse tRESW
 * is ~10 us (datasheet v02, 2009); a couple of ms at our ms poll granularity is
 * safely above it. The post-reset settle (datasheet floor 5 ms) is NOT owned
 * here -- it is the init table's leading {HX_DLY,5} entry (the driver walks the
 * table and arms a timed deadline per HX_DLY). There is deliberately no
 * "sleep-out" wait: the HX8347-D has no MIPI sleep-out command; the widely
 * quoted 120 ms is tREST (reset-complete/blank window), encoded in the table. */
#ifndef CLCD_RESET_PULSE_MS
#define CLCD_RESET_PULSE_MS 2u
#endif

/* ---- RGB565 colours ------------------------------------------------------ */
#define CLCD_RGB565_WHITE 0xFFFFu
#define CLCD_RGB565_BLACK 0x0000u
#define CLCD_RGB565_RED   0xF800u   /* error-banner background (inverted rows) */

/* ---- R4: themes (clcd.h "THE ROLE PLANE + THE PALETTE SEAM") ---------------
 * TODAY: exactly the three colours above. Every role a plain cell can carry is
 * white on black; every banner role is white on red -- so a role plane built by
 * the today layout's one rule (inverted row -> its banner role) draws the very
 * pixels the pre-R4 renderer drew (test_clcd_roles.c's golden). Listed role by
 * role on purpose: a role HM adds must be placed here by hand, and the static
 * assert below stops a re-vendored palette from silently drawing black on black.
 * ALIGNED: Harness Manager's palette, generated from its tokens (clcd_palette.h,
 * vendored unchanged -- never copy a value out of it into this file). */
#define TODAY_PLAIN  { CLCD_RGB565_WHITE, CLCD_RGB565_BLACK }
#define TODAY_BANNER { CLCD_RGB565_WHITE, CLCD_RGB565_RED }
_Static_assert(CLCD_ROLE_COUNT == 21, "clcd_palette.h changed its roles: update PAL_TODAY and ROLE_NAMES");
static const uint16_t PAL_TODAY[CLCD_ROLE_COUNT][2] = {
    [CLCD_ROLE_TEXT]        = TODAY_PLAIN,
    [CLCD_ROLE_LABEL]       = TODAY_PLAIN,
    [CLCD_ROLE_VALUE]       = TODAY_PLAIN,
    [CLCD_ROLE_RULE]        = TODAY_PLAIN,
    [CLCD_ROLE_CHROME]      = TODAY_PLAIN,
    [CLCD_ROLE_TITLE]       = TODAY_PLAIN,
    [CLCD_ROLE_TITLE_HELD]  = TODAY_PLAIN,
    [CLCD_ROLE_TITLE_WARN]  = TODAY_PLAIN,
    [CLCD_ROLE_OK]          = TODAY_PLAIN,
    [CLCD_ROLE_WARN]        = TODAY_PLAIN,
    [CLCD_ROLE_ERR]         = TODAY_PLAIN,
    [CLCD_ROLE_BUSY]        = TODAY_PLAIN,
    [CLCD_ROLE_UNK]         = TODAY_PLAIN,
    [CLCD_ROLE_HELD]        = TODAY_PLAIN,
    [CLCD_ROLE_BAR]         = TODAY_PLAIN,
    [CLCD_ROLE_TRACK]       = TODAY_PLAIN,
    [CLCD_ROLE_BANNER_ERR]  = TODAY_BANNER,
    [CLCD_ROLE_BANNER_WARN] = TODAY_BANNER,
    [CLCD_ROLE_BANNER_OK]   = TODAY_BANNER,
    [CLCD_ROLE_BANNER_BUSY] = TODAY_BANNER,
    [CLCD_ROLE_BANNER_HELD] = TODAY_BANNER,
};
static const uint16_t PAL_ALIGNED[CLCD_ROLE_COUNT][2] = CLCD_PALETTE_INIT;

const mps3_clcd_theme_t clcd_theme_today   = { PAL_TODAY,   0u,                 "today"   };
const mps3_clcd_theme_t clcd_theme_aligned = { PAL_ALIGNED, CLCD_THEME_ALIGNED, "aligned" };

/* tokens.json panel.roles keys, by enum clcd_role (the `panel frame` legend). */
static const char *const ROLE_NAMES[CLCD_ROLE_COUNT] = {
    [CLCD_ROLE_TEXT] = "text",               [CLCD_ROLE_LABEL] = "label",
    [CLCD_ROLE_VALUE] = "value",             [CLCD_ROLE_RULE] = "rule",
    [CLCD_ROLE_CHROME] = "chrome",           [CLCD_ROLE_TITLE] = "title",
    [CLCD_ROLE_TITLE_HELD] = "title-held",   [CLCD_ROLE_TITLE_WARN] = "title-warn",
    [CLCD_ROLE_OK] = "ok",                   [CLCD_ROLE_WARN] = "warn",
    [CLCD_ROLE_ERR] = "err",                 [CLCD_ROLE_BUSY] = "busy",
    [CLCD_ROLE_UNK] = "unk",                 [CLCD_ROLE_HELD] = "held",
    [CLCD_ROLE_BAR] = "bar",                 [CLCD_ROLE_TRACK] = "track",
    [CLCD_ROLE_BANNER_ERR] = "banner-err",   [CLCD_ROLE_BANNER_WARN] = "banner-warn",
    [CLCD_ROLE_BANNER_OK] = "banner-ok",     [CLCD_ROLE_BANNER_BUSY] = "banner-busy",
    [CLCD_ROLE_BANNER_HELD] = "banner-held",
};

const char *clcd_role_name(unsigned role)
{
    return (role < CLCD_ROLE_COUNT && ROLE_NAMES[role]) ? ROLE_NAMES[role] : "";
}

/* The palette seam. WEAK: today's colours and words; mps3-harnessd returns
 * &clcd_theme_aligned. */
__attribute__((weak)) const mps3_clcd_theme_t *mps3_clcd_palette(void)
{
    return &clcd_theme_today;
}

/* ---- HX8347-D GRAM addressing (runtime windowing -- UNPROVEN, see header) - */
#define HX_REG_COL_START_HI 0x02u
#define HX_REG_COL_START_LO 0x03u
#define HX_REG_COL_END_HI   0x04u
#define HX_REG_COL_END_LO   0x05u
#define HX_REG_ROW_START_HI 0x06u
#define HX_REG_ROW_START_LO 0x07u
#define HX_REG_ROW_END_HI   0x08u
#define HX_REG_ROW_END_LO   0x09u
#define HX_REG_RAMWR        0x22u

/* One cell's worth of {RS,byte}: 8 window-register writes (cmd+data each) + the
 * RAMWR command + 8*16 pixels * 2 bytes RGB565. */
#define CLCD_CELL_PREAMBLE 17u                         /* 16 + RAMWR            */
#define CLCD_CELL_PIXELS   (CLCD_GLYPH_W * CLCD_GLYPH_H * 2u)  /* 256           */
#define CLCD_CELL_BYTES    (CLCD_CELL_PREAMBLE + CLCD_CELL_PIXELS)  /* 273      */

/* ==========================================================================
 * State
 * ========================================================================== */
typedef enum {
    ST_RESET = 0,
    ST_RST_WAIT,
    ST_INIT,
    ST_INIT_WAIT,
    ST_IDLE,
    ST_RENDER,
} clcd_state_t;

static clcd_state_t s_state;
static uint32_t     s_t0;              /* deadline base (ms)                    */
static unsigned     s_init_idx;        /* cursor into hx8347_init[]             */
static uint32_t     s_delay_ms;        /* armed HX_DLY deadline                 */
static uint32_t     s_last_refresh;    /* last reformat time (ms)               */
static int          s_force_refresh;   /* force the next IDLE reformat          */
static unsigned     s_refresh_tick;    /* heartbeat/spinner phase               */

static char    s_shadow[CLCD_NCELLS];  /* what is currently on the glass        */
static uint8_t s_inv[CLCD_ROWS];       /* per-row inversion currently on glass  */
static uint8_t s_dirty[CLCD_DIRTY_BYTES];
static unsigned s_dirty_count;

/* Render cursor: the cell currently being streamed, resumable across passes. */
static uint8_t  s_cell_val[CLCD_CELL_BYTES];
static uint8_t  s_cell_rs[CLCD_CELL_BYTES];   /* 0 = command, 1 = data         */
static unsigned s_cell_len;
static unsigned s_cell_pos;

static char     s_board_name[24];
static uint32_t s_swap_count;          /* locally counted LOADED transitions    */
static int      s_prev_swap_valid;
static uint32_t s_usd_seen;            /* USD change count the last reformat saw */

/* R4/R2/R5 panel state. s_role is the ROLE PLANE (enum clcd_role per committed
 * cell, beside s_shadow); s_theme the theme it was drawn in (NULL until the first
 * reformat). The rest describes the committed frame for clcd_panel_state() and
 * the page-aware hit test. */
static uint8_t  s_role[CLCD_NCELLS];
static const mps3_clcd_theme_t *s_theme;
static uint8_t  s_banner_kind;         /* CLCD_BANNER_* on the committed frame   */
static uint8_t  s_overlay_on;          /* the overlay's tap target (CLCD_ON_*)   */
static char     s_banner_text[CLCD_COLS + 1];
static uint32_t s_frame_seq;
static uint8_t  s_prog_pct = 255u;     /* R5, 255 = no swap / total unknown      */
static clcd_event_t s_events[CLCD_EVENT_RING];
static uint32_t s_event_seq;           /* the newest event's seq (0 = none)      */

/* Test-visible byte accounting. */
static uint32_t s_bytes_last_pass;
static uint32_t s_bytes_total;

/* ---- KVM handover state (all inert unless MPS3_HAS_CLCD_KVM is built) ------
 * s_kvm_setup: the one-time KVM setup (clcd_kvm_init() + the pb_en clear) has
 *   run -- once, at the top of the FIRST kvm_service(), whatever the KVM reports.
 * s_relinquished: WE (harness) do not currently reach the pads -- the DUT owns
 *   the panel, or the KVM is mid-handover driving its idle pattern. While set,
 *   clcd_poll() pushes NOTHING and does not reformat: it does not spin on a
 *   STATUS that is no longer meaningful, and it does not waste bytes on a KVM
 *   that is discarding them. It clears the instant we regain the panel.
 * s_banner_mode: reformat() paints the "DUT has the display" banner instead of
 *   the status screen -- set the moment a switch AWAY from the harness is seen,
 *   so the last thing the user sees explains itself. */
static int s_relinquished;
static int s_banner_mode;
static clcd_page_t s_page;             /* current display page (nav layer)      */
#ifdef MPS3_HAS_CLCD_KVM
static int s_kvm_setup;
/* USER_nPB[1] press-duration FSM. Firmware owns the button (pb_en cleared at
 * init, so the hardware no longer auto-toggles ownership): a SHORT press cycles
 * the page, a LONG hold hands the panel to the DUT, and while the DUT owns it any
 * press asks for the panel back. */
static uint32_t s_pb_down_ms;
static int      s_pb_was_down;
static int      s_pb_long_fired;
/* 0 until PB1 has been seen RELEASED once since clcd_init(). A press that is
 * already down when the service starts is NOT ours: it is the D13 power-up
 * escape hatch ("hold PB1 through power-up = skip the card's default load",
 * sampled as a level by the boot hook). Until the first release pb_service()
 * acts on nothing, so that hold never flips the panel or changes the page. */
static int      s_pb_armed;
#ifndef CLCD_PB_LONG_MS
#define CLCD_PB_LONG_MS 800u           /* short/long threshold (ms)             */
#endif
#endif
#ifdef MPS3_HAS_TOUCH
static int s_touch_setup;              /* touch_init() has run (once, first poll) */
static uint32_t s_touch_last_ms;       /* last touch_poll() (CLCD_TOUCH_PERIOD_MS) */
static int      s_touch_sampled;       /* touch_poll() has run at least once       */
#endif

/* ==========================================================================
 * Small formatting helpers (no libc printf on target)
 * ========================================================================== */
static const char HEXU[] = "0123456789ABCDEF";

static char *ap_str(char *p, const char *s)
{
    while (*s) *p++ = *s++;
    return p;
}
static char *ap_u32(char *p, uint32_t v)
{
    char tmp[10];
    int n = 0;
    if (v == 0) { *p++ = '0'; return p; }
    while (v) { tmp[n++] = (char)('0' + (v % 10u)); v /= 10u; }
    while (n) *p++ = tmp[--n];
    return p;
}
static char *ap_hex2(char *p, uint8_t b)
{
    *p++ = HEXU[(b >> 4) & 0xF];
    *p++ = HEXU[b & 0xF];
    return p;
}

/* ==========================================================================
 * Public pure formatters (unit-tested directly)
 * ========================================================================== */
void clcd_fmt_uptime(char *out, uint32_t ms)
{
    /* Wrap-safe by construction: `ms` is the raw 32-bit counter; we only ever
     * divide it, never compare two timestamps. Days can reach 049 (~49.7d). */
    uint32_t sec  = ms / 1000u;
    uint32_t days = sec / 86400u; sec %= 86400u;
    uint32_t hh   = sec / 3600u;  sec %= 3600u;
    uint32_t mm   = sec / 60u;
    uint32_t ss   = sec % 60u;
    out[0]  = (char)('0' + (days / 100u) % 10u);
    out[1]  = (char)('0' + (days / 10u) % 10u);
    out[2]  = (char)('0' + days % 10u);
    out[3]  = ':';
    out[4]  = (char)('0' + (hh / 10u) % 10u);
    out[5]  = (char)('0' + hh % 10u);
    out[6]  = ':';
    out[7]  = (char)('0' + (mm / 10u) % 10u);
    out[8]  = (char)('0' + mm % 10u);
    out[9]  = ':';
    out[10] = (char)('0' + (ss / 10u) % 10u);
    out[11] = (char)('0' + ss % 10u);
    out[12] = '\0';
}

void clcd_fmt_hex32(char *out, uint32_t v)
{
    out[0] = '0';
    out[1] = 'x';
    for (int i = 0; i < 8; i++)
        out[2 + i] = HEXU[(v >> ((7 - i) * 4)) & 0xF];
    out[10] = '\0';
}

/* Keyed on the DESIGN half (clcd.h's rm_id-v2 block): the version bits move on
 * every RM release, so matching the whole 32-bit word would lose the name the
 * moment a design was re-versioned. design_id values are the LOW 16 bits of
 * each RM's wrapper constant (docs/VERSIONING_PLAN.md §3.2's table), which is
 * why every id except uart_echo's survives the re-encoding untouched.
 *
 * EVERY string here must fit CLCD_RM_NAME_MAX -- test_clcd.c sweeps all 65536
 * design ids and asserts it, so a name too long for row 2 fails the build's
 * test step instead of quietly eating the field beside it. */
const char *clcd_rm_name(uint32_t rm_id)
{
    switch (CLCD_RM_DESIGN(rm_id)) {
    case 0x0000u: return "greybox";
    case 0x0001u: return "nanosoc";
    case 0x0002u: return "eth_ss";
    case 0x0003u: return "nanosoc_multicore";   /* 17 -- the overflow that was */
    case 0x0004u: return "uart_echo";           /* re-numbered from 0x4543484F */
    case 0x001Eu: return "led";
    case 0x00A1u: return "regdemo_a";
    case 0x00B2u: return "regdemo_b";
    default: {
        /* Never lie about an unrecognised id: show it raw -- and show the WHOLE
         * 32-bit word, version bits included, because under v2 an unknown high
         * half is part of why we could not name it. "rm?01000003" = 11 chars. */
        static char b[16];
        char *p = b;
        *p++ = 'r'; *p++ = 'm'; *p++ = '?';
        for (int i = 0; i < 8; i++)
            *p++ = HEXU[(rm_id >> ((7 - i) * 4)) & 0xF];
        *p = '\0';
        return b;
    }
    }
}

void clcd_fmt_rm_version(char *out, uint32_t rm_id)
{
    char *p = out;
    *p++ = 'v';
    p = ap_u32(p, CLCD_RM_VER_MAJOR(rm_id));
    *p++ = '.';
    p = ap_u32(p, CLCD_RM_VER_MINOR(rm_id));
    *p = '\0';
}

/* ==========================================================================
 * The LIVE resident RM (clcd.h's "read from hardware, never from a cache"
 * block). This is the whole point of the DUT row: what is IN the RP right now,
 * not what this firmware last put there.
 * ========================================================================== */
clcd_rm_src_t clcd_rm_classify(uint32_t dfx_status, uint32_t dfx_rm_status)
{
    /* Isolation first -- see clcd.h. While the RP is decoupled or in reset the
     * rm_id bus is the decoupler's DECOUPLED_VALUE (0x0), NOT a design. */
    if (dfx_status & DFXCTL_STATUS_DECOUPLED)      return CLCD_RM_DECOUPLED;
    if (dfx_status & DFXCTL_STATUS_RP_IN_RESET)    return CLCD_RM_RP_RESET;
    if (!(dfx_rm_status & DFXCTL_RM_STATUS_RM_ID_VALID)) return CLCD_RM_SETTLING;
    return CLCD_RM_LIVE;
}

/* Every string <= CLCD_RM_NAME_MAX (test_clcd.c asserts it over the whole enum,
 * so one too long for row 2 fails the test step instead of eating the field
 * beside it -- the same discipline clcd_rm_name() is held to). */
const char *clcd_rm_src_text(clcd_rm_src_t src)
{
    switch (src) {
    case CLCD_RM_DECOUPLED: return "RP DECOUPLED";
    case CLCD_RM_RP_RESET:  return "RP IN RESET";
    case CLCD_RM_SETTLING:  return "RM ID NOT VALID";
    case CLCD_RM_LIVE:      return "";     /* render the real name instead */
    default:                return "RM ID NOT VALID";
    }
}

clcd_rm_src_t clcd_rm_live(uint32_t *rm_id_out)
{
    /* RM_ID -- qualifiers -- RM_ID: the sandwich in clcd.h. A single ordering
     * (id-then-valid, or valid-then-id) has a window in which a reconfiguration
     * lands between the two reads and we qualify one id while displaying
     * another; reading the id on BOTH sides and requiring it to have held closes
     * it, for one extra AXI read. */
    uint32_t id_a      = mps3_reg_read32(MPS3_DFXCTL_BASE, DFXCTL_RM_ID);
    uint32_t status    = mps3_reg_read32(MPS3_DFXCTL_BASE, DFXCTL_STATUS);
    uint32_t rm_status = mps3_reg_read32(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS);
    uint32_t id_b      = mps3_reg_read32(MPS3_DFXCTL_BASE, DFXCTL_RM_ID);

    if (rm_id_out)
        *rm_id_out = id_b;

    clcd_rm_src_t src = clcd_rm_classify(status, rm_status);
    if (src == CLCD_RM_LIVE && id_a != id_b)
        return CLCD_RM_SETTLING;   /* it moved under us -- say so, don't guess */
    return src;
}

/* One-line makeup of the loaded RM's design: cores / ethernet / serial.
 * These are FACTS from each RM's wrapper header in this repo, NOT invented:
 *   nanosoc  -- fpga/rp/nanosoc/rp_nanosoc_wrapper.sv:4-11,124 + README
 *               "REAL single-core nanoSoC", "single-core nanosoc has NO
 *               ethernet MAC", cmsdk UART2 console (uart_axis_shim), GPIO16.
 *   eth_ss   -- fpga/rp/eth_ss/rp_eth_ss_wrapper.sv header + README:
 *               AHB-MAC + PTP subsystem, "deliberately NO CPU, NO console
 *               UART". The MAC is the point.
 *   nanosoc_multicore -- fpga/rp/nanosoc_multicore/rp_nanosoc_multicore_wrapper
 *               .sv (RM_ID_NANOSOC_MULTICORE, design 0x0003): the two-core
 *               nanoSoC + ethernet/PTP subsystem. It is also the ONLY RM that
 *               actually drives dut_lockup (:206); the others hard-tie it to 0.
 *   greybox / led / regdemo_a|b -- single-file OOC RMs (fpga/dfx/rms/<rm>.sv),
 *               no CPU; led drives LEDs, regdemo is an AHB register block.
 *   uart_echo -- fpga/dfx/rms/rm_uart_echo: a UART loopback, no CPU.
 * Keyed on the DESIGN half, exactly like clcd_rm_name() (clcd.h's rm_id-v2
 * block) -- a re-versioned RM must not lose its makeup line either.
 * Kept <= 34 chars so "CFG : " + this fits the 40-col row (test_clcd.c sweeps
 * all 65536 design ids and asserts the bound). */
const char *clcd_rm_caps(uint32_t rm_id)
{
    switch (CLCD_RM_DESIGN(rm_id)) {
    case 0x0000u: return "greybox: empty tie-off";
    case 0x0001u: return "1x Cortex-M0  no ETH  1x UART";
    case 0x0002u: return "no CPU  ETH MAC+PTP  no UART";
    case 0x0003u: return "2x CPU  ETH MAC+PTP  2x UART";
    case 0x0004u: return "no CPU  1x UART (echo)";
    case 0x001Eu: return "no CPU  LED demo";
    case 0x00A1u: return "no CPU  AHB register demo";
    case 0x00B2u: return "no CPU  AHB register demo";
    default:      return "design unknown";
    }
}

/* Which services the Apps page lists for the resident RM, keyed on the DESIGN
 * half (like clcd_rm_name/_caps). The shell-side services (control 6900, TFTP,
 * raw push, XVC) are present for EVERY RM -- they are the static shell's, not the
 * DUT's -- so an unknown/transient id still shows those. The per-DUT services
 * mirror clcd_rm_caps()'s makeup: a CPU exposes SWD/JTAG debug + the SWO trace +
 * a boot-monitor UART0; a two-UART design (multicore) adds UART1; the CPU-less
 * uart_echo still bridges its one UART. This is a FACT table, same provenance as
 * clcd_rm_caps(); test_clcd.c sweeps it. */
uint32_t clcd_rm_services(uint32_t rm_id)
{
    uint32_t s = CLCD_SVC_CTRL | CLCD_SVC_TFTP | CLCD_SVC_PUSH | CLCD_SVC_XVC;
    switch (CLCD_RM_DESIGN(rm_id)) {
    case 0x0001u:   /* nanosoc: 1x Cortex-M0, 1x UART */
        s |= CLCD_SVC_SWD | CLCD_SVC_JTAG | CLCD_SVC_UART0 | CLCD_SVC_SWO;
        break;
    case 0x0003u:   /* nanosoc_multicore: 2x CPU, 2x UART */
        s |= CLCD_SVC_SWD | CLCD_SVC_JTAG | CLCD_SVC_UART0 | CLCD_SVC_UART1 |
             CLCD_SVC_SWO;
        break;
    case 0x0004u:   /* uart_echo: a UART loopback, no CPU */
        s |= CLCD_SVC_UART0;
        break;
    default:        /* greybox / eth_ss / led / regdemo / unknown: shell-only */
        break;
    }
    return s;
}

/* Which RMs carry an ethernet MAC -- eth_ss (0x0002) and nanosoc_multicore
 * (0x0003) only. Keyed on the DESIGN half like clcd_rm_name()/clcd_rm_caps(), so
 * a re-versioned RM keeps its verdict. single-core nanosoc (0x0001) has NO MAC
 * (clcd_rm_caps(): "no ETH"), so it -- like every non-eth RM -- is false. This is
 * the licence for the status page's DUT-IP row: only an ethernet RM can have an
 * egress IP for the gen_checker to observe. */
int clcd_rm_has_eth(uint32_t rm_id)
{
    switch (CLCD_RM_DESIGN(rm_id)) {
    case 0x0002u:   /* eth_ss:            no CPU  ETH MAC+PTP */
    case 0x0003u:   /* nanosoc_multicore: 2x CPU  ETH MAC+PTP */
        return 1;
    default:
        return 0;
    }
}

void clcd_link_speed_duplex(uint16_t anlpar, int *speed100, int *full_duplex)
{
    /* ANLPAR (MII reg 0x05) technology ability, most-capable wins:
     * [8]100BASE-TX FD [7]100BASE-TX [6]10BASE-T FD [5]10BASE-T. */
    int sp = 0, fd = 0;
    if      (anlpar & (1u << 8)) { sp = 1; fd = 1; }
    else if (anlpar & (1u << 7)) { sp = 1; fd = 0; }
    else if (anlpar & (1u << 6)) { sp = 0; fd = 1; }
    else                         { sp = 0; fd = 0; }
    if (speed100)    *speed100 = sp;
    if (full_duplex) *full_duplex = fd;
}

/* The board-identity seams (v0.16, lane IDENT). WEAK and 0 here: the bare-metal
 * image shows its compile-time MPS3_DEFAULT_IP_* and the MAC its platform reports,
 * byte-identical to before. mps3-harnessd supplies this boot's RESOLVED identity
 * (identity_linux.c: /run/mps3/identity), so the NET and MAC rows show what the
 * board actually answers on, not the image's constant. */
__attribute__((weak)) int mps3_clcd_ident_ip(char out[16])
{
    (void)out;
    return 0;
}

__attribute__((weak)) int mps3_clcd_ident_mac(uint8_t mac[6])
{
    (void)mac;
    return 0;
}

void clcd_fmt_ip(char *out)
{
    char *p = out;
    if (mps3_clcd_ident_ip(out)) {
        return;
    }
    p = ap_u32(p, MPS3_DEFAULT_IP_A); *p++ = '.';
    p = ap_u32(p, MPS3_DEFAULT_IP_B); *p++ = '.';
    p = ap_u32(p, MPS3_DEFAULT_IP_C); *p++ = '.';
    p = ap_u32(p, MPS3_DEFAULT_IP_D);
    *p = '\0';
}

void clcd_fmt_net(char *out, unsigned cap)
{
    char buf[48];
    char *p;
    clcd_fmt_ip(buf);
    p = buf + strlen(buf);
    if (smsc911x_link_up() == 1) {
        uint16_t anlpar = 0;
        int s100 = 0, fd = 0;
        (void)smsc911x_mii_read(0x05u, &anlpar);   /* ANLPAR */
        clcd_link_speed_duplex(anlpar, &s100, &fd);
        p = ap_str(p, "  UP ");
        p = ap_str(p, s100 ? "100" : "10");
        *p++ = '/';
        p = ap_str(p, fd ? "FD" : "HD");
    } else {
        p = ap_str(p, "  DOWN");
    }
    *p = '\0';
    /* copy, NUL-terminated within cap */
    unsigned n = (unsigned)(p - buf);
    if (cap == 0) return;
    if (n > cap - 1u) n = cap - 1u;
    memcpy(out, buf, n);
    out[n] = '\0';
}

/* The engine-row seam (clcd.h "THE ENGINE ROW"). WEAK and 0 here: the bare-metal
 * image has no OS beneath it, no sshd and no claim, so row 12 stays blank and its
 * frame is byte-identical to the one before the row existed. */
__attribute__((weak)) int mps3_clcd_engine(mps3_clcd_engine_t *out)
{
    (void)out;
    return 0;
}

void clcd_fmt_engine(char *out, unsigned cap, const mps3_clcd_engine_t *e)
{
    char buf[48];
    char *p = buf;
    unsigned i;
    const char *impl = (e && e->impl) ? e->impl : "?";
    for (i = 0; impl[i] != '\0' && i < CLCD_ENGINE_IMPL_MAX; i++) {
        *p++ = impl[i];
    }
    for (; i < CLCD_ENGINE_IMPL_MAX + 1u; i++) {
        *p++ = ' ';                                   /* pad to col 13: "ssh" aligns */
    }
    p = ap_str(p, (e && e->claimed) ? "ssh claimed" : "ssh unclaimed");
    if (e && e->claimed && e->fpr[0] != '\0') {
        p = ap_str(p, " SHA256:");
        for (i = 0; e->fpr[i] != '\0' && i < 8u; i++) {
            *p++ = e->fpr[i];
        }
    }
    *p = '\0';
    unsigned n = (unsigned)(p - buf);
    if (cap == 0) return;
    if (n > cap - 1u) n = cap - 1u;
    memcpy(out, buf, n);
    out[n] = '\0';
}

/* The DUT's last-observed egress IPv4, read LIVE from the shell's gen_checker
 * (GENCHK.DUT_STATUS qualifies GENCHK.DUT_IP). Mirrors clcd_fmt_net()'s cap-safe
 * copy. Emits "--" until IP_SEEN latches -- a CPU-less ethernet RM (eth_ss) has no
 * IP stack, so it never sources a frame; multicore is the real target. The word
 * is network order: first octet in bits[31:24]. */
void clcd_fmt_dut_ip(char *out, unsigned cap)
{
    char buf[16];
    char *p = buf;
    uint32_t st = mps3_reg_read32(MPS3_GENCHK_BASE, GENCHK_DUT_STATUS);
    if (!(st & GENCHK_DUT_STATUS_IP_SEEN)) {
        p = ap_str(p, "--");
    } else {
        uint32_t ip = mps3_reg_read32(MPS3_GENCHK_BASE, GENCHK_DUT_IP);
        p = ap_u32(p, (ip >> 24) & 0xFFu); *p++ = '.';
        p = ap_u32(p, (ip >> 16) & 0xFFu); *p++ = '.';
        p = ap_u32(p, (ip >>  8) & 0xFFu); *p++ = '.';
        p = ap_u32(p,  ip        & 0xFFu);
    }
    *p = '\0';
    /* copy, NUL-terminated within cap */
    unsigned n = (unsigned)(p - buf);
    if (cap == 0) return;
    if (n > cap - 1u) n = cap - 1u;
    memcpy(out, buf, n);
    out[n] = '\0';
}

/* ==========================================================================
 * User microSD (D13) -- the row-4 "USD : <text>" field (clcd.h).
 * ========================================================================== */

/* WEAK fallbacks for the overlay store's two exports (lane I-FW), the same seam
 * as coordinator.c's weak mps3_shell_static_id(): a build that links no store
 * -- today's image, every CLCD host test that does not override them, the
 * preview -- still links, and the glass says the honest thing, "no hw", with a
 * change count that never moves (so it never triggers a refresh). The store's
 * own definitions are strong and win the link. */
__attribute__((weak)) const char *overlay_store_usd_text(void)
{
    return "no hw";
}

__attribute__((weak)) uint32_t overlay_store_usd_change_count(void)
{
    return 0u;
}

void clcd_fmt_usd(char *out, const char *text)
{
    unsigned n = 0;
    if (!text)
        text = "?";            /* a broken provider: say "don't know", not a state */
    while (text[n] && n < CLCD_USD_TEXT_MAX) {
        char ch = text[n];
        out[n] = (ch >= 0x20 && ch < 0x7F) ? ch : '?';   /* 8x16 font: ASCII only */
        n++;
    }
    out[n] = '\0';
}

/* ==========================================================================
 * Shadow diff (pure)
 * ========================================================================== */
unsigned clcd_diff_cells(const char *cur, const char *next,
                         const uint8_t *cur_inv, const uint8_t *next_inv,
                         uint8_t *dirty)
{
    unsigned n = 0;
    for (unsigned i = 0; i < CLCD_NCELLS; i++) {
        unsigned row = i / CLCD_COLS;
        int changed = (cur[i] != next[i]);
        if (cur_inv && next_inv && cur_inv[row] != next_inv[row])
            changed = 1;
        if (changed) {
            uint8_t bit = (uint8_t)(1u << (i & 7u));
            if (!(dirty[i >> 3] & bit)) {
                dirty[i >> 3] |= bit;
                n++;
            }
        }
    }
    return n;
}

/* The role plane's half of the diff: a cell whose colour role changed is dirty
 * even when its glyph did not (the R5 bar is all spaces). Same contract as
 * clcd_diff_cells(): bits OR-ed in, returns the newly dirtied count. */
static unsigned diff_roles(const uint8_t *cur, const uint8_t *next, uint8_t *dirty)
{
    unsigned n = 0;
    for (unsigned i = 0; i < CLCD_NCELLS; i++) {
        if (cur[i] != next[i]) {
            uint8_t bit = (uint8_t)(1u << (i & 7u));
            if (!(dirty[i >> 3] & bit)) {
                dirty[i >> 3] |= bit;
                n++;
            }
        }
    }
    return n;
}

/* ==========================================================================
 * FIFO push -- the single choke point that enforces the byte budget and never
 * writes into a full FIFO (drops are the block's policy; we simply never
 * trigger one). Return: 1 pushed, 0 budget exhausted, -1 FIFO full.
 * ========================================================================== */
/* THE BUS SEAM's bare-metal default (clcd.h): try_push()'s STATUS-then-write,
 * byte by byte -- the exact access sequence the renderer made before the seam.
 * mps3-harnessd's strong definition (hal_front.c) replaces it. */
__attribute__((weak)) uint32_t clcd_bus_push(const uint8_t *rs, const uint8_t *val, uint32_t n)
{
    uint32_t i = 0;
    for (; i < n; i++) {
        if (mps3_reg_read32(MPS3_CLCD_BASE, CLCD_STATUS) & CLCD_STATUS_FIFO_FULL)
            break;
        mps3_reg_write32(MPS3_CLCD_BASE, rs[i] ? CLCD_DATA : CLCD_CMD, (uint32_t)val[i]);
    }
    return i;
}

static int try_push(uint32_t *budget, int is_data, uint8_t val)
{
    if (*budget == 0)
        return 0;
    uint32_t st = mps3_reg_read32(MPS3_CLCD_BASE, CLCD_STATUS);
    if (st & CLCD_STATUS_FIFO_FULL)
        return -1;
    mps3_reg_write32(MPS3_CLCD_BASE, is_data ? CLCD_DATA : CLCD_CMD, (uint32_t)val);
    (*budget)--;
    s_bytes_total++;
    return 1;
}

/* ==========================================================================
 * The next frame: cells, per-row inversion and the ROLE PLANE
 *
 * A frame is built whole (40x15) by reformat() and then diffed into the shadow.
 * Every cell carries an enum clcd_role (R4). The today-theme layout below is
 * written exactly as before -- characters and per-row inversion -- and its roles
 * come from ONE rule applied after the layout: a cell on an inverted row takes
 * that row's banner role (rrole[r], CLCD_ROLE_BANNER_ERR unless the banner said
 * otherwise), every other cell keeps CLCD_ROLE_TEXT. Every banner role is white
 * on red in clcd_theme_today, and every other role white on black, so the today
 * theme's pixels are exactly the old renderer's (test_clcd_roles.c's golden).
 * The aligned layout writes its roles directly (rput and friends).
 * ========================================================================== */
typedef struct {
    char    ch[CLCD_NCELLS];
    uint8_t inv[CLCD_ROWS];
    uint8_t rrole[CLCD_ROWS];       /* the banner role of an inverted row */
    uint8_t role[CLCD_NCELLS];
} frame_t;

static void frame_clear(frame_t *f)
{
    memset(f->ch, ' ', sizeof(f->ch));
    memset(f->inv, 0, sizeof(f->inv));
    memset(f->rrole, CLCD_ROLE_BANNER_ERR, sizeof(f->rrole));
    memset(f->role, CLCD_ROLE_TEXT, sizeof(f->role));
}

static void row_fill(char *next, unsigned r, char ch)
{
    memset(next + r * CLCD_COLS, ch, CLCD_COLS);
}
static void put_at(char *next, unsigned r, unsigned c, const char *s)
{
    unsigned i = c;
    while (*s && i < CLCD_COLS) {
        next[r * CLCD_COLS + i] = *s++;
        i++;
    }
}

/* Centre `s` on row `r` of `next`. */
static void put_centered(char *next, unsigned r, const char *s)
{
    unsigned len = 0;
    while (s[len]) len++;
    unsigned col = (len < CLCD_COLS) ? (CLCD_COLS - len) / 2u : 0u;
    put_at(next, r, col, s);
}

/* ---- role-aware writers (the aligned layout, the seams) ------------------- */
/* Write s at (r, c) in `role`, stopping before column cmax; returns the column
 * after the last cell written (c itself when nothing fit). */
static unsigned rput_clip(frame_t *f, unsigned r, unsigned c, const char *s,
                          unsigned role, unsigned cmax)
{
    if (cmax > CLCD_COLS) cmax = CLCD_COLS;
    while (*s && c < cmax) {
        f->ch[r * CLCD_COLS + c]   = *s++;
        f->role[r * CLCD_COLS + c] = (uint8_t)role;
        c++;
    }
    return c;
}
static unsigned rput(frame_t *f, unsigned r, unsigned c, const char *s, unsigned role)
{
    return rput_clip(f, r, c, s, role, CLCD_COLS);
}
/* Colour columns [c0, c1) of row r; the characters stay. */
static void rfill(frame_t *f, unsigned r, unsigned role, unsigned c0, unsigned c1)
{
    if (c1 > CLCD_COLS) c1 = CLCD_COLS;
    for (unsigned c = c0; c < c1; c++)
        f->role[r * CLCD_COLS + c] = (uint8_t)role;
}
/* The whole row: one character, one role. */
static void rrow(frame_t *f, unsigned r, char ch, unsigned role)
{
    row_fill(f->ch, r, ch);
    rfill(f, r, role, 0u, CLCD_COLS);
}
/* Right-align s to end `pad` cells before the edge; returns its first column. */
static unsigned rright(frame_t *f, unsigned r, const char *s, unsigned role, unsigned pad)
{
    unsigned len = (unsigned)strlen(s);
    unsigned end = (pad < CLCD_COLS) ? CLCD_COLS - pad : 0u;
    unsigned c = (len < end) ? end - len : 0u;
    rput_clip(f, r, c, s, role, end);
    return c;
}
static void rcentre(frame_t *f, unsigned r, const char *s, unsigned role)
{
    unsigned len = (unsigned)strlen(s);
    rput(f, r, (len < CLCD_COLS) ? (CLCD_COLS - len) / 2u : 0u, s, role);
}
/* Rows [r0, r0+n): blank, inverted, in banner role `role`. */
static void banner_rows(frame_t *f, unsigned r0, unsigned n, unsigned role)
{
    for (unsigned r = r0; r < r0 + n && r < CLCD_ROWS; r++) {
        rrow(f, r, ' ', role);
        f->inv[r]   = 1;
        f->rrole[r] = (uint8_t)role;
    }
}

/* Provider text -> a cell string: printable ASCII and the status glyphs kept,
 * anything else '?'. dst holds cap bytes (NUL included). */
static void sanitize(char *dst, const char *src, unsigned cap)
{
    unsigned n = 0;
    if (cap == 0u) return;
    while (src && src[n] && n + 1u < cap) {
        unsigned char ch = (unsigned char)src[n];
        dst[n] = ((ch >= 0x20u && ch < 0x7Fu) ||
                  (ch >= CLCD_GLYPH_FIRST && ch <= CLCD_GLYPH_LAST)) ? (char)ch : '?';
        n++;
    }
    dst[n] = '\0';
}

static int is_banner_role(unsigned role)
{
    return role >= CLCD_ROLE_BANNER_ERR && role <= CLCD_ROLE_BANNER_HELD;
}

/* The banner's main line for clcd_panel_state(): glyphs dropped, edge spaces
 * trimmed. */
static void set_banner(unsigned kind, const char *line)
{
    char *p = s_banner_text;
    s_banner_kind = (uint8_t)kind;
    for (const char *s = line ? line : ""; *s && p < s_banner_text + CLCD_COLS; s++) {
        unsigned char ch = (unsigned char)*s;
        if (ch >= 0x20u && ch < 0x7Fu && !(ch == ' ' && p == s_banner_text))
            *p++ = (char)ch;
    }
    while (p > s_banner_text && p[-1] == ' ') p--;
    *p = '\0';
}

/* ==========================================================================
 * The theme seam (R4)
 * ========================================================================== */
static const mps3_clcd_theme_t *theme_now(void)
{
    const mps3_clcd_theme_t *t = mps3_clcd_palette();
    return (t && t->pal) ? t : &clcd_theme_today;
}
static int aligned(void)
{
    return s_theme && (s_theme->flags & CLCD_THEME_ALIGNED);
}

/* ==========================================================================
 * The panel seams (R2). WEAK and 0 here: bare metal has no Harness Manager
 * session, no lease and no overlay, so row 0, row 11 and rows 10-12 are exactly
 * as they were. mps3-harnessd supplies the strong ones.
 * ========================================================================== */
__attribute__((weak)) int mps3_clcd_title_right(mps3_clcd_badge_t *out)
{
    (void)out;
    return 0;
}

__attribute__((weak)) int mps3_clcd_session_row(clcd_line_t *out)
{
    (void)out;
    return 0;
}

__attribute__((weak)) int mps3_clcd_overlay(mps3_clcd_overlay_t *out)
{
    (void)out;
    return 0;
}

/* A link without swap_fsm.c (the host tests, the preview) still links: no swap
 * is ever in flight. swap_fsm.c's definition is strong and wins. */
__attribute__((weak)) void swap_fsm_progress(mps3_swap_progress_t *out)
{
    if (!out) return;
    memset(out, 0, sizeof(*out));
    out->rm = "";
}

void clcd_line_clear(clcd_line_t *l)
{
    memset(l->text, ' ', CLCD_COLS);
    l->text[CLCD_COLS] = '\0';
    memset(l->role, CLCD_ROLE_TEXT, sizeof(l->role));
}

unsigned clcd_line_put(clcd_line_t *l, unsigned col, const char *s, unsigned role)
{
    while (s && *s && col < CLCD_COLS) {
        l->text[col] = *s++;
        l->role[col] = (uint8_t)role;
        col++;
    }
    return col;
}

void clcd_line_right(clcd_line_t *l, const char *s, unsigned role, unsigned pad)
{
    unsigned len = s ? (unsigned)strlen(s) : 0u;
    unsigned end = (pad < CLCD_COLS) ? CLCD_COLS - pad : 0u;
    unsigned col = (len < end) ? end - len : 0u;
    while (s && *s && col < end) {
        l->text[col] = *s++;
        l->role[col] = (uint8_t)role;
        col++;
    }
}

/* Row 0's right side: the badge, right-aligned one cell in from the edge, clipped
 * so one blank cell always separates it from the board name (name_end = the
 * first column after the name). 1 = drawn. */
static int title_badge(frame_t *f, unsigned name_end)
{
    mps3_clcd_badge_t b;
    char text[CLCD_TITLE_RIGHT_MAX + 1];
    memset(&b, 0, sizeof(b));
    if (!mps3_clcd_title_right(&b))
        return 0;
    b.text[CLCD_TITLE_RIGHT_MAX] = '\0';
    sanitize(text, b.text, sizeof(text));
    unsigned len = (unsigned)strlen(text);
    unsigned role = (b.role < CLCD_ROLE_COUNT) ? b.role : CLCD_ROLE_TEXT;
    unsigned start = (len < CLCD_COLS - 1u) ? CLCD_COLS - 1u - len : 0u;
    if (start < name_end + 1u)
        start = name_end + 1u;              /* the name wins; the badge is clipped */
    rput_clip(f, 0, start, text, role, CLCD_COLS - 1u);
    return 1;
}

/* Row 11 (status page, no banner): the provider's whole line. */
static void session_row(frame_t *f)
{
    clcd_line_t l;
    clcd_line_clear(&l);
    if (!mps3_clcd_session_row(&l))
        return;
    l.text[CLCD_COLS] = '\0';
    int ended = 0;
    for (unsigned c = 0; c < CLCD_COLS; c++) {
        unsigned char ch = ended ? ' ' : (unsigned char)l.text[c];
        if (ch == '\0') { ended = 1; ch = ' '; }
        if (!((ch >= 0x20u && ch < 0x7Fu) || (ch >= CLCD_GLYPH_FIRST && ch <= CLCD_GLYPH_LAST)))
            ch = '?';
        f->ch[CLCD_SESSION_ROW * CLCD_COLS + c]   = (char)ch;
        f->role[CLCD_SESSION_ROW * CLCD_COLS + c] =
            (l.role[c] < CLCD_ROLE_COUNT) ? l.role[c] : (uint8_t)CLCD_ROLE_TEXT;
    }
}

/* Rows 10-12: the provider's banner (a lease request), ranked below the fault
 * and identify banners by the caller. 1 = drawn. */
static int overlay_rows(frame_t *f)
{
    mps3_clcd_overlay_t o;
    char line[CLCD_COLS + 1];
    memset(&o, 0, sizeof(o));
    if (!mps3_clcd_overlay(&o))
        return 0;
    unsigned role = is_banner_role(o.role) ? o.role : CLCD_ROLE_BANNER_HELD;
    banner_rows(f, 10, 3, role);
    const char *main_line = "";
    for (unsigned i = 0; i < 3u; i++) {
        o.line[i][CLCD_COLS] = '\0';
        sanitize(line, o.line[i], sizeof(line));
        rcentre(f, 10u + i, line, role);
        if (!main_line[0] && line[0])
            main_line = o.line[i];
    }
    s_overlay_on = (o.on <= CLCD_ON_REQUEST) ? o.on : (uint8_t)CLCD_ON_NONE;
    set_banner(CLCD_BANNER_OVERLAY, main_line);
    return 1;
}

/* The "DUT has the display" OSD -- painted the moment a switch AWAY from the
 * harness is seen, so the last thing on the glass before the handover EXPLAINS
 * the handover (that is the whole point of an OSD on a KVM). Kept compact and
 * high-contrast (inverted rows), and it deliberately does NOT read any DUT-only
 * source, so it renders identically whether or not the DUT has taken over yet. */
static void build_banner(frame_t *f)
{
    char *next = f->ch;
    row_fill(next, 0, '-');
    put_centered(next, 0, " nanoSoC harness ");
    put_centered(next, 6, "DUT HAS THE DISPLAY");
    put_centered(next, 8, "PRESS  PB1  TO RETURN");
    f->inv[6] = f->inv[7] = f->inv[8] = 1;   /* white-on-red, unmissable */
    f->rrole[6] = f->rrole[7] = f->rrole[8] = CLCD_ROLE_BANNER_HELD;
    row_fill(next, 14, '-');
    set_banner(CLCD_BANNER_DUT, "DUT HAS THE DISPLAY");
}

/* The aligned OSD: the same facts in the held colour ("someone else has it"),
 * HM's title bar and footer chrome. */
static void build_banner_aligned(frame_t *f)
{
    mps3_clcd_engine_t eng;
    char title[24];
    memset(&eng, 0, sizeof(eng));
    rrow(f, 0, ' ', CLCD_ROLE_TITLE);
    unsigned c = rput_clip(f, 0, 1, s_board_name, CLCD_ROLE_TITLE, 17);
    char *p = title;
    if (mps3_clcd_engine(&eng) && eng.impl && eng.impl[0]) {
        for (unsigned i = 0; eng.impl[i] && i < CLCD_ENGINE_IMPL_MAX; i++) *p++ = eng.impl[i];
        *p++ = ' ';
    }
    p = ap_str(p, "harness");
    *p = '\0';
    if (c + 1u + strlen(title) < CLCD_COLS)
        rright(f, 0, title, CLCD_ROLE_CHROME, 1);
    banner_rows(f, 6, 3, CLCD_ROLE_BANNER_HELD);
    rcentre(f, 6, CLCD_GS_HELD " DUT HAS THE PANEL", CLCD_ROLE_BANNER_HELD);
    rcentre(f, 8, "press PB1 to take it back", CLCD_ROLE_BANNER_HELD);
    rrow(f, 14, ' ', CLCD_ROLE_CHROME);
    rput(f, 14, 1, "the DUT draws next; PB1 takes it back", CLCD_ROLE_CHROME);
    set_banner(CLCD_BANNER_DUT, "DUT HAS THE PANEL");
}

/* ==========================================================================
 * Multi-page navigation (docs/planning/CLCD_APPS_PORTS_PAGE_PLAN.md, Phase 1)
 * ========================================================================== */
void clcd_page_set(clcd_page_t page)
{
    if ((unsigned)page >= (unsigned)CLCD_PAGE__COUNT)
        page = CLCD_PAGE_STATUS;
    if (s_page != page) {
        s_page = page;
        s_force_refresh = 1;    /* whole screen changes -- repaint next IDLE */
    }
}
clcd_page_t clcd_page_get(void) { return s_page; }
void clcd_page_next(void)
{
    clcd_page_set((clcd_page_t)(((unsigned)s_page + 1u) % (unsigned)CLCD_PAGE__COUNT));
}

const char *clcd_page_name(unsigned page)
{
    switch (page) {
    case CLCD_PAGE_STATUS: return "status";
    case CLCD_PAGE_APPS:   return "apps";
    default:               return "";
    }
}

int clcd_page_by_name(const char *name)
{
    for (unsigned p = 0; p < (unsigned)CLCD_PAGE__COUNT; p++) {
        if (name && strcmp(name, clcd_page_name(p)) == 0)
            return (int)p;
    }
    return -1;
}

/* The Apps-page service table: label (cols 0..4) + a "<tool> <ip><sep><port>"
 * command (from col 6). Ports are the net_proto.h constants -- the SAME source
 * the shell servers bind, so the screen can never advertise a wrong port. Widest
 * rendered command ("ocd rbb " + 15-char quad + ':' + 4-digit port = 28) sits at
 * col 6..33, well inside the 40-col row (test_clcd.c asserts it). */
static const struct {
    uint32_t    bit;
    const char *label;
    const char *cmd_pre;
    char        sep;        /* ' ' -> "ip port" | ':' -> "ip:port" */
    uint16_t    port;
} SERVICES[] = {
    { CLCD_SVC_CTRL,  "CTRL",  "nc ",      ' ', MPS3_PORT_CONTROL  },
    { CLCD_SVC_TFTP,  "TFTP",  "tftp ",    ' ', MPS3_PORT_TFTP     },
    { CLCD_SVC_PUSH,  "PUSH",  "nc ",      ' ', MPS3_PORT_RAW_PUSH },
    { CLCD_SVC_XVC,   "XVC",   "xvc ",     ':', MPS3_PORT_XVC      },
    { CLCD_SVC_SWD,   "SWD",   "ocd rbb ", ':', MPS3_PORT_SWD      },
    { CLCD_SVC_JTAG,  "JTAG",  "ocd rbb ", ':', MPS3_PORT_JTAG     },
    { CLCD_SVC_UART0, "UART0", "nc ",      ' ', MPS3_PORT_UART0    },
    { CLCD_SVC_UART1, "UART1", "nc ",      ' ', MPS3_PORT_UART1    },
    { CLCD_SVC_SWO,   "SWO",   "nc ",      ' ', MPS3_PORT_SWO      },
};
#define CLCD_NSERVICES (sizeof(SERVICES) / sizeof(SERVICES[0]))

/* "<tool> <ip><sep><port>" for service i into out (>= 32 bytes). */
static void service_cmd(char *out, unsigned i, const char *ip)
{
    char *p = out;
    p = ap_str(p, SERVICES[i].cmd_pre);
    p = ap_str(p, ip);
    *p++ = SERVICES[i].sep;
    p = ap_u32(p, SERVICES[i].port);
    *p = '\0';
}

/* Render the "Applications & Ports" page for the RESIDENT RM (read LIVE, like the
 * status page). An unknown/transient id still shows the shell-side services. */
static void reformat_apps(frame_t *f)
{
    char *next = f->ch;
    char scratch[48];
    char ip[16];

    uint32_t      rm_id  = 0;
    clcd_rm_src_t rm_src = clcd_rm_live(&rm_id);
    int           rm_known = (rm_src == CLCD_RM_LIVE);

    clcd_fmt_ip(ip);

    /* Row 0: title + which RM these services belong to. */
    put_at(next, 0, 0, "APPS & PORTS");
    put_at(next, 0, 13, rm_known ? clcd_rm_name(rm_id) : clcd_rm_src_text(rm_src));

    /* Row 1: rule. */
    row_fill(next, 1, '-');

    /* Rows 2..12: one PRESENT service per row. Shell services show even for an
     * unknown/transient RM (clcd_rm_services(0) = the shell-only set). */
    uint32_t svcs = clcd_rm_services(rm_known ? rm_id : 0u);
    unsigned r = 2;
    for (unsigned i = 0; i < CLCD_NSERVICES && r <= 12u; i++) {
        if (!(svcs & SERVICES[i].bit))
            continue;
        service_cmd(scratch, i, ip);
        put_at(next, r, 0, SERVICES[i].label);
        put_at(next, r, 6, scratch);
        r++;
    }

    /* Row 13: rule. */
    row_fill(next, 13, '-');

    /* Row 14: nav bar -- a whole-row inverse "button" (per-row inversion, which
     * the renderer already supports; the sub-row highlight for individual service
     * rows is a Phase-2 enhancement). */
    put_at(next, 14, 0, "PB1 tap:next page   hold:give DUT");
    f->inv[14] = 1;
}

/* The aligned Apps page (HM mock-up "aligned_apps"): HM's title bar, lower-case
 * muted labels, the footer as neutral chrome rather than the fault red. SWD 6920
 * is left off: it is dormant since the JTAG cutover (HM CLCD_ALIGNMENT §1.1), and
 * the Linux harness serves `ssh root@<ip>` instead (§3, "put it on the apps
 * page"). The bare-metal page above is unchanged. */
static void reformat_apps_aligned(frame_t *f)
{
    char scratch[48];
    char label[8];
    char ip[16];

    uint32_t      rm_id  = 0;
    clcd_rm_src_t rm_src = clcd_rm_live(&rm_id);
    int           rm_known = (rm_src == CLCD_RM_LIVE);

    clcd_fmt_ip(ip);

    rrow(f, 0, ' ', CLCD_ROLE_TITLE);
    rput(f, 0, 1, "apps & ports", CLCD_ROLE_TITLE);
    rright(f, 0, rm_known ? clcd_rm_name(rm_id) : clcd_rm_src_text(rm_src), CLCD_ROLE_CHROME, 1);
    rrow(f, 1, '-', CLCD_ROLE_RULE);

    uint32_t svcs = clcd_rm_services(rm_known ? rm_id : 0u) & ~(uint32_t)CLCD_SVC_SWD;
    unsigned r = 2;
    for (unsigned i = 0; i < CLCD_NSERVICES && r <= 12u; i++) {
        if (!(svcs & SERVICES[i].bit))
            continue;
        unsigned k = 0;
        for (; SERVICES[i].label[k] && k + 1u < sizeof(label); k++) {
            char ch = SERVICES[i].label[k];
            label[k] = (ch >= 'A' && ch <= 'Z') ? (char)(ch - 'A' + 'a') : ch;
        }
        label[k] = '\0';
        service_cmd(scratch, i, ip);
        rput(f, r, 0, label, CLCD_ROLE_LABEL);
        rput(f, r, 7, scratch, CLCD_ROLE_VALUE);
        r++;
    }
    if (r <= 12u) {
        char *p = ap_str(scratch, "ssh root@");
        p = ap_str(p, ip);
        *p = '\0';
        rput(f, r, 0, "ssh", CLCD_ROLE_LABEL);
        rput(f, r, 7, scratch, CLCD_ROLE_VALUE);
    }
    rrow(f, 13, '-', CLCD_ROLE_RULE);
    rrow(f, 14, ' ', CLCD_ROLE_CHROME);
    rput(f, 14, 1, "PB1 tap: next page   hold: give DUT", CLCD_ROLE_CHROME);
}

/* ==========================================================================
 * Touch targets (page-aware, R2) + the event ring
 * ========================================================================== */
/* Each page's own boxes; `arg` is the event's CLCD_ON_*. Row 14 is the nav strip
 * on both pages -- the only touch path between them (PB1 is the other). A tap
 * elsewhere on a page is nothing, and no event. */
static const clcd_hitbox_t STATUS_BOXES[] = {
    { 0u, 14u, (uint8_t)CLCD_COLS, 1u, CLCD_ACT_NEXT_PAGE, CLCD_ON_NAV },
};
static const clcd_hitbox_t APPS_BOXES[] = {
    { 0u, 14u, (uint8_t)CLCD_COLS, 1u, CLCD_ACT_NEXT_PAGE, CLCD_ON_NAV },
};

/* O(1), no I/O: the touch path's only new cost. */
static void event_push(unsigned on, unsigned col, unsigned row)
{
    clcd_event_t *e = &s_events[s_event_seq % CLCD_EVENT_RING];
    s_event_seq++;
    if (s_event_seq == 0u) s_event_seq = 1u;          /* 0 means "none"; never reused */
    e->seq  = s_event_seq;
    e->t_ms = mps3_sys_now_ms();
    e->kind = CLCD_EV_TAP;
    e->on   = (uint8_t)on;
    e->page = (uint8_t)s_page;
    e->col  = (uint8_t)(col < CLCD_COLS ? col : CLCD_COLS - 1u);
    e->row  = (uint8_t)(row < CLCD_ROWS ? row : CLCD_ROWS - 1u);
}

uint32_t clcd_event_seq(void) { return s_event_seq; }

unsigned clcd_events_since(uint32_t after, clcd_event_t *out, unsigned max)
{
    unsigned n = 0;
    if (!out || s_event_seq == 0u || after >= s_event_seq)
        return 0;
    uint32_t first = after + 1u;
    if (s_event_seq - first >= CLCD_EVENT_RING)          /* older ones are gone */
        first = s_event_seq - (CLCD_EVENT_RING - 1u);
    for (uint32_t seq = first; n < max; seq++) {
        out[n++] = s_events[(seq - 1u) % CLCD_EVENT_RING];
        if (seq == s_event_seq) break;
    }
    return n;
}

const char *clcd_event_on_name(unsigned on)
{
    switch (on) {
    case CLCD_ON_NAV:      return "nav";
    case CLCD_ON_IDENTIFY: return "identify";
    case CLCD_ON_REQUEST:  return "request";
    default:               return "";
    }
}

/* The locate seams (v0.16, HM R3 `locate`; clcd.h "THE IDENTIFY BANNER"). WEAK
 * and 0 here: bare metal has no locate verb, so no banner is drawn and every tap
 * goes to the hit boxes -- its frames and its touch behaviour are unchanged. */
__attribute__((weak)) int mps3_clcd_locate(char *who, unsigned cap)
{
    (void)who;
    (void)cap;
    return 0;
}

__attribute__((weak)) int mps3_clcd_tap(unsigned x_px, unsigned y_px)
{
    (void)x_px;
    (void)y_px;
    return 0;
}

/* Rows 10-12, inverted, "IDENTIFY: <who>" centred on row 11 -- while a locate
 * runs. Returns 1 when it drew (the caller's engine row then yields, like it
 * yields to a fault banner). The fault banner outranks it: the status page asks
 * only when it raised none. The aligned theme draws it in the busy banner colour
 * with the user glyph (and, with a touchscreen, the tap hint on row 12). */
static int locate_overlay(frame_t *f)
{
    char who[CLCD_COLS + 1];
    char line[CLCD_COLS + 3];
    who[0] = '\0';
    if (!mps3_clcd_locate(who, sizeof(who)))
        return 0;
    who[sizeof(who) - 1u] = '\0';
    char *p = line;
    if (aligned()) {
        p = ap_str(p, CLCD_GS_USER);
        *p++ = ' ';
    }
    p = ap_str(p, "IDENTIFY");
    if (who[0]) {
        p = ap_str(p, ": ");
        for (const char *w = who; *w && p < line + CLCD_COLS; w++)
            *p++ = (*w >= ' ' && *w <= '~') ? *w : '?';
    }
    *p = '\0';
    if (p > line + CLCD_COLS) line[CLCD_COLS] = '\0';
    unsigned len = (unsigned)strlen(line);
    if (aligned()) {
        banner_rows(f, 10, 3, CLCD_ROLE_BANNER_BUSY);
        rput(f, 11, (len < CLCD_COLS) ? (CLCD_COLS - len) / 2u : 0u, line, CLCD_ROLE_BANNER_BUSY);
#ifdef MPS3_HAS_TOUCH
        rcentre(f, 12, "tap here to say you found it", CLCD_ROLE_BANNER_BUSY);
#endif
    } else {
        row_fill(f->ch, 10, ' ');
        row_fill(f->ch, 11, ' ');
        row_fill(f->ch, 12, ' ');
        put_at(f->ch, 11, (len < CLCD_COLS) ? (CLCD_COLS - len) / 2u : 0u, line);
        f->inv[10] = f->inv[11] = f->inv[12] = 1;
        f->rrole[10] = f->rrole[11] = f->rrole[12] = CLCD_ROLE_BANNER_BUSY;
    }
    set_banner(CLCD_BANNER_LOCATE, line);
    return 1;
}

int clcd_hittest(unsigned x_px, unsigned y_px)
{
    unsigned col = x_px / CLCD_GLYPH_W;
    unsigned row = y_px / CLCD_GLYPH_H;
    /* A tap while a locate runs is its "found it" (HM R3): the engine takes it,
     * and it is not also a page change. */
    if (mps3_clcd_tap(x_px, y_px)) {
        event_push(CLCD_ON_IDENTIFY, col, row);
        return CLCD_ACT_NONE;
    }
    /* The KVM OSD offers nothing to tap: the next press belongs to PB1. */
    if (s_banner_kind == CLCD_BANNER_DUT || col >= CLCD_COLS || row >= CLCD_ROWS)
        return CLCD_ACT_NONE;
    /* A banner target exists only while its banner shows (rows 10-12). */
    if (s_banner_kind == CLCD_BANNER_OVERLAY && s_overlay_on != CLCD_ON_NONE &&
        row >= 10u && row <= 12u) {
        event_push(s_overlay_on, col, row);
        return CLCD_ACT_BANNER;
    }
    const clcd_hitbox_t *boxes = STATUS_BOXES;
    unsigned nboxes = (unsigned)(sizeof(STATUS_BOXES) / sizeof(STATUS_BOXES[0]));
    if (s_page == CLCD_PAGE_APPS) {
        boxes  = APPS_BOXES;
        nboxes = (unsigned)(sizeof(APPS_BOXES) / sizeof(APPS_BOXES[0]));
    }
    for (unsigned i = 0; i < nboxes; i++) {
        const clcd_hitbox_t *b = &boxes[i];
        if (col >= b->x && col < (unsigned)(b->x + b->w) &&
            row >= b->y && row < (unsigned)(b->y + b->h)) {
            event_push(b->arg, col, row);
            return (int)b->action;
        }
    }
    return CLCD_ACT_NONE;
}

/* ==========================================================================
 * The status page
 * ========================================================================== */
typedef struct {
    uint32_t                  now, static_id, rm_id;
    clcd_rm_src_t             rm_src;
    int                       rm_known, locked, alive, dut_released, link_up;
    const mps3_swap_result_t *sr;
    mps3_swap_progress_t      pg;
} status_src_t;

/* Gather the status page's sources -- the same reads, in the same order, as
 * before the page grew a second theme. */
static void status_sources(status_src_t *s)
{
    s->now       = mps3_sys_now_ms();
    s->static_id = g_shell_state.static_id;

    /* THE RESIDENT RM -- LIVE FROM THE DFXCTL CSR, NOT g_shell_state.
     * current_rm_id. See clcd.h: that cache is the last id this FIRMWARE
     * verified, so a JTAG/ICAP load (which bypasses the firmware) leaves it
     * stale and the glass names a design that is not in the RP. `rm_known` is
     * the licence to name/version/describe the id at all; without it rows 2 and
     * 9 say so explicitly rather than fall back to a plausible-looking name.
     * static_id above stays firmware-side: it is a property of the STATIC shell
     * this firmware is part of, not of the RP, and no CSR reports it (see
     * coordinator.h's mps3_shell_static_id() seam). */
    s->rm_id    = 0;
    s->rm_src   = clcd_rm_live(&s->rm_id);
    s->rm_known = (s->rm_src == CLCD_RM_LIVE);

    uint32_t clk_st   = mps3_reg_read32(MPS3_CLKRST_BASE, CLKRST_STATUS);
    uint32_t rst_ctrl = mps3_reg_read32(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL);
    s->locked       = (clk_st & CLKRST_STATUS_MMCM_LOCKED)   ? 1 : 0;
    s->alive        = (clk_st & CLKRST_STATUS_DUT_CLK_ALIVE) ? 1 : 0;
    s->dut_released = (rst_ctrl & CLKRST_RESET_CTRL_DUT_RESETN) ? 1 : 0;
    s->link_up      = (smsc911x_link_up() == 1);

    s->sr = swap_fsm_last_result();

    /* Locally count LOADED transitions (no swap counter exists; the plan's
     * "add one to swap_fsm.c" is out of scope). Edge-detect the latched
     * result's valid 0->1 with ok. */
    {
        int v = (s->sr && s->sr->valid) ? 1 : 0;
        if (v && !s_prev_swap_valid && s->sr->ok)
            s_swap_count++;
        s_prev_swap_valid = v;
    }

}

/* Rows 10-12: error banner (highest diagnostic value; hidden when healthy).
 * All conditions are firmware-evaluable; static_id keyed-overlay mismatch
 * surfaces as a failed swap (its #1 cause) -- see plan section 8. */
static const char *fault_banner(const status_src_t *s)
{
    if (!s->link_up)                  return "NETWORK LINK DOWN";
    if (!s->alive || !s->locked)      return "DUT CLOCK NOT RUNNING";
    if (s->sr && s->sr->valid && !s->sr->ok)
                                      return "LAST SWAP FAILED - RM NOT LOADED";
    if (s->static_id == 0)            return "STATIC_ID NOT PROVISIONED";
    return 0;
}

static void swap_count_text(char *out)
{
    out[0] = '#';
    out[1] = (char)('0' + (s_swap_count / 100u) % 10u);
    out[2] = (char)('0' + (s_swap_count / 10u) % 10u);
    out[3] = (char)('0' + s_swap_count % 10u);
    out[4] = '\0';
}

static void mac_text(char *out, int lower_label)
{
    uint8_t mac[6];
    if (!mps3_clcd_ident_mac(mac)) {
        mps3_platform_mac(mac);
    }
    char *p = ap_str(out, lower_label ? "mac " : "MAC ");
    for (int i = 0; i < 6; i++) {
        p = ap_hex2(p, mac[i]);
        if (i < 5) *p++ = ':';
    }
    *p = '\0';
}

static void heartbeat_text(char *out)
{
    static const char spin[4] = { '|', '/', '-', '\\' };
    out[0] = 'h'; out[1] = 'b'; out[2] = ' ';
    out[3] = spin[s_refresh_tick & 3u];
    out[4] = '\0';
}

/* The status page in TODAY's words and layout (the default theme, bare metal).
 * Every statement that draws is the pre-R4 renderer's; the seams it asks (badge,
 * overlay, session row) are WEAK 0 on bare metal. */
static void reformat_status(frame_t *f, const status_src_t *s)
{
    char *next = f->ch;
    char  scratch[64];
    char *p;
    const mps3_swap_result_t *sr = s->sr;

    /* Row 0: board name + platform (or the lease badge, when a session gave one) */
    put_at(next, 0, 0, s_board_name);
    if (!title_badge(f, (unsigned)strlen(s_board_name)))
        put_at(next, 0, 20, "nanoSoC harness");

    /* Row 1: rule --------------------------------------------------------- */
    row_fill(next, 1, '-');

    /* Row 2: DUT design name + design VERSION ------------------------------
     * "DUT : nanosoc_multicore   v1.0". Both fields are declared in clcd.h
     * (CLCD_RM_NAME_COL/_MAX, CLCD_RM_VER_COL/_MAX) and their non-overlap is
     * asserted by test_clcd.c, because this row used to overflow: the name went
     * in at col 6 and a raw "rm_id 0x%08X" at col 21, leaving the name 14 cols
     * for a string that could be 17. The hex id is gone (clcd.h explains why);
     * an unrecognised id still shows its full raw hex, in the name field. */
    put_at(next, 2, 0, "DUT : ");
    if (s->rm_known) {
        put_at(next, 2, CLCD_RM_NAME_COL, clcd_rm_name(s->rm_id));
        clcd_fmt_rm_version(scratch, s->rm_id);
        put_at(next, 2, CLCD_RM_VER_COL, scratch);
    } else {
        /* Not knowable: name the STATE, and leave the version field BLANK. A
         * version for a design we cannot identify would be a second confident
         * claim stacked on top of the first (and "v0.0" from a clamped 0x0
         * would look exactly like a real greybox). */
        put_at(next, 2, CLCD_RM_NAME_COL, clcd_rm_src_text(s->rm_src));
    }

    /* Row 3: swap state + verified + count + last result ------------------ */
    p = ap_str(scratch, "SWAP: ");
    if (sr && sr->valid) {
        p = ap_str(p, sr->ok ? "LOADED " : "FAILED ");
        p = ap_str(p, sr->verified ? "VERIFIED " : "UNVERIF  ");
    } else {
        p = ap_str(p, "IDLE (boot)   ");
    }
    *p = '\0';
    put_at(next, 3, 0, scratch);
    swap_count_text(scratch);
    put_at(next, 3, 26, scratch);
    if (sr && sr->valid)
        put_at(next, 3, 32, sr->ok ? "last OK" : "last ERR");

    /* Row 4: static_id (overlay key) -------------------------------------- */
    p = ap_str(scratch, "SID : ");
    clcd_fmt_hex32(p, s->static_id);
    put_at(next, 4, 0, scratch);

    /* Row 4, right: the user microSD (D13), cols 18..39 -- see clcd.h. The
     * store's `text` verbatim (net-protocol.md "usd"), clipped to its field by
     * clcd_fmt_usd(). Deliberately NOT an input to the banner below: no USD
     * state is a harness fault. */
    put_at(next, CLCD_USD_ROW, CLCD_USD_COL, "USD : ");
    clcd_fmt_usd(scratch, overlay_store_usd_text());
    put_at(next, CLCD_USD_ROW, CLCD_USD_TEXT_COL, scratch);

    /* Row 5: IP + link/speed/duplex --------------------------------------- */
    put_at(next, 5, 0, "NET : ");
    clcd_fmt_net(scratch, sizeof(scratch));
    put_at(next, 5, 6, scratch);

    /* Row 6: uptime ------------------------------------------------------- */
    put_at(next, 6, 0, "UP  : ");
    clcd_fmt_uptime(scratch, s->now);
    put_at(next, 6, 6, scratch);

    /* Row 7: DUT reset + clk-alive + mmcm-lock ---------------------------- */
    put_at(next, 7, 0, "DUT : ");
    put_at(next, 7, 6,  s->dut_released ? "RST-REL " : "RST-HOLD");
    put_at(next, 7, 15, s->alive ? "CLK-ALIVE " : "CLK-DEAD  ");
    put_at(next, 7, 27, s->locked ? "MMCM-LOCK" : "MMCM-UNL ");

    /* Row 8: ICAP bytes + diag counters (diag read via g_mps3_diag SYMBOL) - */
    p = ap_str(scratch, "ICAP: ");
    p = ap_u32(p, swap_fsm_icap_bytes());
    p = ap_str(p, " B  rxdrop ");
    p = ap_u32(p, g_mps3_diag.rx_drop_frames);
    p = ap_str(p, " txerr ");
    p = ap_u32(p, g_mps3_diag.tx_errors);
    *p = '\0';
    put_at(next, 8, 0, scratch);

    /* Row 9: loaded-design makeup (cores / ethernet / serial) -------------
     * Same licence as row 2: describing the makeup of an id we could not
     * qualify is the same defect one row down (a clamped 0x0 would render as
     * "greybox: empty tie-off" -- confidently, and wrongly). */
    put_at(next, 9, 0, "CFG : ");
    put_at(next, 9, 6, s->rm_known ? clcd_rm_caps(s->rm_id) : CLCD_RM_CAPS_UNKNOWN);

    /* Rows 10-12, highest rank first (clcd.h "THE PANEL SEAMS"): the fault
     * banner, the identify banner, the overlay; only one draws, and the plain
     * rows below yield to it. */
    const char *banner = fault_banner(s);
    int rows_taken = 1;
    if (banner) {
        unsigned len = (unsigned)strlen(banner);
        unsigned col = (len < CLCD_COLS) ? (CLCD_COLS - len) / 2u : 0u;
        row_fill(next, 10, ' ');
        row_fill(next, 11, ' ');
        row_fill(next, 12, ' ');
        put_at(next, 11, col, banner);
        f->inv[10] = f->inv[11] = f->inv[12] = 1;
        set_banner(CLCD_BANNER_FAULT, banner);
    } else if (locate_overlay(f)) {
        /* v0.16 locate: the identify banner, ranked BELOW the fault banner. */
    } else if (overlay_rows(f)) {
        /* a lease request (HM R2), ranked below both. */
    } else {
        rows_taken = 0;
        if (s->rm_known && clcd_rm_has_eth(s->rm_id)) {
            /* Row 10 (banner-free): the DUT's OWN observed egress IP. Rows 10-12
             * are the error-banner reservation and stay BLANK when healthy, so
             * this line lives at the top of that reservation and YIELDS the
             * instant a banner is raised. Only an ethernet RM can source one;
             * eth_ss (no CPU/IP stack) reads "--" until a frame is seen,
             * multicore is the real target. Distinct from row 5's "NET :", the
             * harness's OWN management IP. No inversion -- an inverted row 10 is
             * the banner's signal alone. */
            put_at(next, 10, 0, "DIP : ");
            clcd_fmt_dut_ip(scratch, sizeof(scratch));
            put_at(next, 10, 6, scratch);
        }
    }

    if (!rows_taken) {
        /* Row 11 (banner-free): the Harness Manager session row (HM R2). */
        session_row(f);
        /* Row 12 (banner-free): the ENGINE row -- which engine, and whether its
         * SSH is claimed (clcd.h "THE ENGINE ROW"). Yields to the banner like
         * DIP; left untouched when no provider speaks (bare metal:
         * byte-identical frame). */
        mps3_clcd_engine_t eng;
        memset(&eng, 0, sizeof(eng));
        if (mps3_clcd_engine(&eng)) {
            put_at(next, CLCD_ENGINE_ROW, 0, "SYS : ");
            clcd_fmt_engine(scratch, sizeof(scratch), &eng);
            put_at(next, CLCD_ENGINE_ROW, 6, scratch);
        }
    }

    /* Row 13: rule -------------------------------------------------------- */
    row_fill(next, 13, '-');

    /* Row 14: MAC + heartbeat spinner ------------------------------------- */
    mac_text(scratch, 0);
    put_at(next, 14, 0, scratch);
    heartbeat_text(scratch);
    put_at(next, 14, 34, scratch);
}

/* The aligned words for a resident RM we cannot name (HM's "partition"). */
static const char *aligned_src_text(clcd_rm_src_t src)
{
    switch (src) {
    case CLCD_RM_DECOUPLED: return "partition decoupled";
    case CLCD_RM_RP_RESET:  return "partition in reset";
    default:                return "design id not valid";
    }
}

/* R5: what the swap is doing, in HM's words. */
static const char *prog_phase(unsigned state)
{
    switch (state) {
    case SWAP_GATE:
    case SWAP_DECOUPLE_ASSERT:         return "starting";
    case SWAP_STREAM_CLEARING:         return "clearing";
    case SWAP_AWAIT_INCOMING_CLEARING: return "receiving";
    case SWAP_AWAIT_PARTIAL:
    case SWAP_STREAM_PARTIAL:          return "pushing";
    case SWAP_VERIFY:                  return "verifying";
    case SWAP_CACHE_CLEARING:
    case SWAP_RELEASE:
    case SWAP_DONE:                    return "finishing";
    default:                           return "stopping";   /* FAILED, REISOLATE */
    }
}

unsigned clcd_prog_pct(uint32_t done, uint32_t total)
{
    if (total == 0u)
        return 255u;
    if (done >= total)
        return 100u;
    return (unsigned)(((uint64_t)done * 100u) / total);
}

unsigned clcd_bar_cells(unsigned pct)
{
    if (pct > 100u)
        return 0u;
    return (pct * CLCD_COLS + 50u) / 100u;
}

/* The status page in HM's words (decision D3 a; HM mock-up "aligned_status"):
 * a title bar, a muted key and a value per row (HM's .tile-kv), the status word
 * in its state colour, the R5 progress and the HM session row. Same facts as the
 * today page, from the same sources. */
static void reformat_status_aligned(frame_t *f, const status_src_t *s)
{
    enum { VC = 7 };                    /* the value column: after "design " */
    char scratch[64];
    char *p;
    const mps3_swap_result_t *sr = s->sr;
    const mps3_swap_progress_t *pg = &s->pg;
    unsigned pct = pg->active ? clcd_prog_pct(pg->done, pg->total) : 255u;

    /* Row 0: the title bar -- the board on the left, the lease badge right. */
    rrow(f, 0, ' ', CLCD_ROLE_TITLE);
    unsigned name_end = rput_clip(f, 0, 1, s_board_name, CLCD_ROLE_TITLE, 17);
    (void)title_badge(f, name_end);
    rrow(f, 1, '-', CLCD_ROLE_RULE);

    /* Row 2: design -- what the partition holds, and whether the last program
     * of THAT design verified. */
    rput(f, 2, 0, "design", CLCD_ROLE_LABEL);
    if (s->rm_known) {
        unsigned right = CLCD_COLS - 1u;
        if (sr && sr->valid && sr->rm_id == s->rm_id)
            right = rright(f, 2, sr->ok && sr->verified ? CLCD_GS_OK "verified"
                                                        : CLCD_GS_WARN "unverified",
                           sr->ok && sr->verified ? CLCD_ROLE_OK : CLCD_ROLE_WARN, 1);
        unsigned c = rput_clip(f, 2, VC, clcd_rm_name(s->rm_id), CLCD_ROLE_VALUE, right - 1u);
        clcd_fmt_rm_version(scratch, s->rm_id);
        rput_clip(f, 2, c + 1u, scratch, CLCD_ROLE_LABEL, right - 1u);
    } else {
        rput(f, 2, VC, aligned_src_text(s->rm_src), CLCD_ROLE_UNK);
    }

    /* Row 3: prog -- a swap in flight (R5), else the count and the last result. */
    rput(f, CLCD_PROG_ROW, 0, "prog", CLCD_ROLE_LABEL);
    if (pg->active) {
        unsigned right = CLCD_COLS - 1u;
        if (pct <= 100u) {
            p = scratch;
            *p++ = (char)(pct >= 100u ? '1' : ' ');
            *p++ = (char)(pct >= 10u ? '0' + (pct / 10u) % 10u : ' ');
            *p++ = (char)('0' + pct % 10u);
            *p++ = '%';
            *p = '\0';
            right = rright(f, CLCD_PROG_ROW, scratch, CLCD_ROLE_BUSY, 1);
        }
        p = ap_str(scratch, prog_phase(pg->state));
        if (pg->rm && pg->rm[0]) {
            *p++ = ' ';
            for (const char *r = pg->rm; *r && p < scratch + 40; r++)
                *p++ = (*r >= ' ' && *r <= '~') ? *r : '?';
        }
        *p = '\0';
        rput_clip(f, CLCD_PROG_ROW, VC, scratch, CLCD_ROLE_BUSY, right - 1u);
    } else if (sr && sr->valid) {
        swap_count_text(scratch);
        unsigned c = rput(f, CLCD_PROG_ROW, VC, scratch, CLCD_ROLE_VALUE);
        rput(f, CLCD_PROG_ROW, c + 1u, "loaded", CLCD_ROLE_VALUE);
        rright(f, CLCD_PROG_ROW, sr->ok ? "last ok" : "last failed",
               sr->ok ? CLCD_ROLE_OK : CLCD_ROLE_ERR, 1);
    } else {
        swap_count_text(scratch);
        unsigned c = rput(f, CLCD_PROG_ROW, VC, scratch, CLCD_ROLE_VALUE);
        rput(f, CLCD_PROG_ROW, c + 1u, "idle (boot)", CLCD_ROLE_LABEL);
    }

    /* Row 4: shell + card (D13). */
    rput(f, 4, 0, "shell", CLCD_ROLE_LABEL);
    clcd_fmt_hex32(scratch, s->static_id);
    rput(f, 4, VC, scratch, CLCD_ROLE_VALUE);
    rput(f, 4, 18, "card", CLCD_ROLE_LABEL);
    clcd_fmt_usd(scratch, overlay_store_usd_text());
    rput(f, 4, 23, scratch, CLCD_ROLE_VALUE);

    /* Row 5: net -- this board's address, and the link. */
    rput(f, 5, 0, "net", CLCD_ROLE_LABEL);
    clcd_fmt_ip(scratch);
    rput(f, 5, VC, scratch, CLCD_ROLE_VALUE);
    if (s->link_up) {
        uint16_t anlpar = 0;
        int s100 = 0, fd = 0;
        (void)smsc911x_mii_read(0x05u, &anlpar);
        clcd_link_speed_duplex(anlpar, &s100, &fd);
        p = ap_str(scratch, "up ");
        p = ap_str(p, s100 ? "100" : "10");
        *p++ = '/';
        p = ap_str(p, fd ? "FD" : "HD");
        *p = '\0';
        rright(f, 5, scratch, CLCD_ROLE_OK, 1);
    } else {
        rright(f, 5, "down", CLCD_ROLE_ERR, 1);
    }

    /* Row 6: up. */
    rput(f, 6, 0, "up", CLCD_ROLE_LABEL);
    clcd_fmt_uptime(scratch, s->now);
    rput(f, 6, VC, scratch, CLCD_ROLE_VALUE);

    /* Row 7: dut -- reset, clock, MMCM, each in its state colour. */
    rput(f, 7, 0, "dut", CLCD_ROLE_LABEL);
    rput(f, 7, VC, s->dut_released ? "rst-rel" : "rst-hold",
         s->dut_released ? CLCD_ROLE_OK : CLCD_ROLE_WARN);
    rput(f, 7, 16, s->alive ? "clk-alive" : "clk-dead", s->alive ? CLCD_ROLE_OK : CLCD_ROLE_ERR);
    rput(f, 7, 27, s->locked ? "mmcm-lock" : "mmcm-unl", s->locked ? CLCD_ROLE_OK : CLCD_ROLE_ERR);

    /* Row 8: icap bytes + the two diag counters. */
    rput(f, 8, 0, "icap", CLCD_ROLE_LABEL);
    p = ap_u32(scratch, swap_fsm_icap_bytes());
    p = ap_str(p, " B");
    *p = '\0';
    unsigned c = rput(f, 8, VC, scratch, CLCD_ROLE_VALUE);
    p = ap_str(scratch, "rxdrop ");
    p = ap_u32(p, g_mps3_diag.rx_drop_frames);
    *p = '\0';
    c = rput(f, 8, (c + 2u > 18u) ? c + 2u : 18u, scratch, CLCD_ROLE_LABEL);
    p = ap_str(scratch, "txerr ");
    p = ap_u32(p, g_mps3_diag.tx_errors);
    *p = '\0';
    rput(f, 8, (c + 2u > 28u) ? c + 2u : 28u, scratch, CLCD_ROLE_LABEL);

    /* Row 9: cfg -- the design's makeup. */
    rput(f, 9, 0, "cfg", CLCD_ROLE_LABEL);
    if (s->rm_known)
        rput(f, 9, VC, clcd_rm_caps(s->rm_id), CLCD_ROLE_VALUE);
    else
        rput(f, 9, VC, "unknown: no valid design id", CLCD_ROLE_UNK);

    /* Rows 10-12, the same ranking as the today page, plus the R5 bar. */
    const char *banner = fault_banner(s);
    int rows_taken = 1;
    if (banner) {
        banner_rows(f, 10, 3, CLCD_ROLE_BANNER_ERR);
        p = ap_str(scratch, CLCD_GS_ERR " ");
        p = ap_str(p, banner);
        *p = '\0';
        rcentre(f, 11, scratch, CLCD_ROLE_BANNER_ERR);
        set_banner(CLCD_BANNER_FAULT, banner);
    } else if (locate_overlay(f)) {
    } else if (overlay_rows(f)) {
    } else {
        rows_taken = 0;
        if (pg->active) {
            /* R5: the bar on row 10 -- it yields to every banner above. */
            rrow(f, CLCD_BAR_ROW, ' ', CLCD_ROLE_TRACK);
            rfill(f, CLCD_BAR_ROW, CLCD_ROLE_BAR, 0u, clcd_bar_cells(pct));
        } else if (s->rm_known && clcd_rm_has_eth(s->rm_id)) {
            rput(f, 10, 0, "dut ip", CLCD_ROLE_LABEL);
            clcd_fmt_dut_ip(scratch, sizeof(scratch));
            rput(f, 10, VC, scratch, CLCD_ROLE_VALUE);
        }
    }
    if (!rows_taken) {
        session_row(f);
        mps3_clcd_engine_t eng;
        memset(&eng, 0, sizeof(eng));
        if (mps3_clcd_engine(&eng)) {
            const char *impl = eng.impl ? eng.impl : "?";
            rput(f, CLCD_ENGINE_ROW, 0, "sys", CLCD_ROLE_LABEL);
            p = scratch;
            for (unsigned i = 0; impl[i] && i < CLCD_ENGINE_IMPL_MAX; i++) *p++ = impl[i];
            *p = '\0';
            c = rput(f, CLCD_ENGINE_ROW, VC, scratch, CLCD_ROLE_VALUE);
            if (eng.claimed) {
                p = ap_str(scratch, "ssh claimed");
                if (eng.fpr[0]) {
                    p = ap_str(p, " SHA256:");
                    for (unsigned i = 0; eng.fpr[i] && i < 8u; i++) *p++ = eng.fpr[i];
                }
                *p = '\0';
                rput(f, CLCD_ENGINE_ROW, c + 1u, scratch, CLCD_ROLE_VALUE);
            } else {
                rput(f, CLCD_ENGINE_ROW, c + 1u, "ssh unclaimed", CLCD_ROLE_WARN);
            }
        }
    }

    /* Rows 13-14: the rule and the footer chrome (the MAC, the heartbeat). */
    rrow(f, 13, '-', CLCD_ROLE_RULE);
    rrow(f, 14, ' ', CLCD_ROLE_CHROME);
    mac_text(scratch, 1);
    rput(f, 14, 1, scratch, CLCD_ROLE_CHROME);
    heartbeat_text(scratch);
    rright(f, 14, scratch, CLCD_ROLE_CHROME, 1);
}

/* Build the whole next frame (40x15) + per-row inversion + roles, then diff it
 * into the shadow and mark dirty cells. Bounded work; called at most every
 * REFRESH_MS. */
static void reformat(void)
{
    static frame_t f;       /* 1.3 KB: static, off the (small) target stack */

    /* Latch the USD change count on EVERY reformat, whatever the page -- the
     * ST_IDLE trigger compares against it, so a page that did not record it
     * would reformat on every pass. Latched BEFORE the text is read below: a
     * change landing between the two reads then re-triggers on the next idle
     * pass instead of being lost. */
    s_usd_seen = overlay_store_usd_change_count();

    /* R4: the theme. A different one repaints EVERY cell -- the same palette
     * index can mean a different colour now, which no cell diff would see. */
    const mps3_clcd_theme_t *t = theme_now();
    if (t != s_theme) {
        s_theme = t;
        memset(s_shadow, 0, sizeof(s_shadow));   /* NUL != any glyph: all dirty */
    }

    /* R5: the programming progress, on every page (counters only, no register
     * access); the aligned status page draws it, clcd_panel_state() reports it. */
    mps3_swap_progress_t pg;
    swap_fsm_progress(&pg);
    s_prog_pct = pg.active ? (uint8_t)clcd_prog_pct(pg.done, pg.total) : 255u;

    frame_clear(&f);
    s_banner_kind   = CLCD_BANNER_NONE;
    s_banner_text[0] = '\0';
    s_overlay_on    = CLCD_ON_NONE;

    if (s_banner_mode) {
        /* Handover OSD: replace the whole screen with the banner (§ KVM handover,
         * clcd.h). Diffs into the shadow like any other frame. */
        if (aligned()) build_banner_aligned(&f);
        else           build_banner(&f);
    } else if (s_page == CLCD_PAGE_APPS) {
        /* Non-status pages branch here (the banner above still overrides them). */
        if (aligned()) reformat_apps_aligned(&f);
        else           reformat_apps(&f);
        /* Over service rows 10-12, while they run: identify, else the overlay. */
        if (!locate_overlay(&f))
            (void)overlay_rows(&f);
    } else {
        status_src_t s;
        status_sources(&s);
        s.pg = pg;
        if (aligned()) reformat_status_aligned(&f, &s);
        else           reformat_status(&f, &s);
    }

    /* The one role rule: a cell on an inverted row takes its row's banner role. */
    for (unsigned r = 0; r < CLCD_ROWS; r++) {
        if (f.inv[r])
            memset(f.role + r * CLCD_COLS, f.rrole[r], CLCD_COLS);
    }

    /* Diff into the shadow, mark dirty, then adopt the new frame ----------- */
    unsigned n = clcd_diff_cells(s_shadow, f.ch, s_inv, f.inv, s_dirty) +
                 diff_roles(s_role, f.role, s_dirty);
    s_dirty_count += n;
    if (n) s_frame_seq++;
    memcpy(s_shadow, f.ch, sizeof(s_shadow));
    memcpy(s_inv, f.inv, sizeof(s_inv));
    memcpy(s_role, f.role, sizeof(s_role));
    s_refresh_tick++;
}

/* ==========================================================================
 * What the panel shows: frame + roles, state (R2, for `hello` / `panel`)
 * ========================================================================== */
unsigned clcd_frame_rows(unsigned first, unsigned count,
                         char (*rows)[CLCD_COLS + 1], char *roles)
{
    if (first >= CLCD_ROWS)
        count = 0;
    else if (count > CLCD_ROWS - first)
        count = CLCD_ROWS - first;
    for (unsigned i = 0; i < count; i++) {
        unsigned base = (first + i) * CLCD_COLS;
        if (rows) {
            memcpy(rows[i], s_shadow + base, CLCD_COLS);
            rows[i][CLCD_COLS] = '\0';
        }
        if (roles) {
            for (unsigned c = 0; c < CLCD_COLS; c++)
                roles[i * CLCD_COLS + c] = CLCD_ROLE_CODE(s_role[base + c] < CLCD_ROLE_COUNT
                                                          ? s_role[base + c] : 0u);
        }
    }
    if (roles)
        roles[count * CLCD_COLS] = '\0';
    return count;
}

unsigned clcd_fmt_row_json(char *out, unsigned cap, const char *cells, unsigned n)
{
    static const char hx[] = "0123456789abcdef";
    unsigned k = 0;
    if (!out || cap == 0u)
        return 0;
    for (unsigned i = 0; cells && i < n; i++) {
        unsigned char ch = (unsigned char)cells[i];
        char esc[7];
        unsigned m = 0;
        if (ch == '"' || ch == '\\') {
            esc[m++] = '\\';
            esc[m++] = (char)ch;
        } else if (ch >= CLCD_GLYPH_FIRST && ch <= CLCD_GLYPH_LAST) {
            esc[m++] = '\\'; esc[m++] = 'u'; esc[m++] = '0'; esc[m++] = '0';
            esc[m++] = hx[ch >> 4];
            esc[m++] = hx[ch & 0xFu];
        } else {
            esc[m++] = (ch >= 0x20u && ch < 0x7Fu) ? (char)ch : ' ';
        }
        if (k + m + 1u > cap) {
            out[0] = '\0';
            return 0;
        }
        memcpy(out + k, esc, m);
        k += m;
    }
    out[k] = '\0';
    return k;
}

void clcd_panel_state(clcd_panel_state_t *out)
{
    if (!out) return;
    memset(out, 0, sizeof(*out));
    out->page         = (uint8_t)s_page;
    out->banner       = s_banner_kind;
    out->relinquished = (uint8_t)(s_relinquished ? 1 : 0);
    out->prog_pct     = s_prog_pct;
    memcpy(out->banner_text, s_banner_text, sizeof(out->banner_text));
    out->theme        = (s_theme ? s_theme : theme_now())->name;
    out->frame_seq    = s_frame_seq;
    out->event_seq    = s_event_seq;
}

/* ==========================================================================
 * Renderer -- expand one dirty cell to its GRAM window + RGB565 pixel bytes
 * ========================================================================== */
static void push_pair(unsigned *n, int rs, uint8_t val)
{
    s_cell_rs[*n]  = (uint8_t)rs;
    s_cell_val[*n] = val;
    (*n)++;
}

static void build_cell(unsigned cell)
{
    unsigned r = cell / CLCD_COLS;
    unsigned c = cell % CLCD_COLS;
    unsigned x0 = c * CLCD_GLYPH_W, x1 = x0 + CLCD_GLYPH_W - 1u;
    unsigned y0 = r * CLCD_GLYPH_H, y1 = y0 + CLCD_GLYPH_H - 1u;

    /* The glyph: ASCII from font8x16, the status glyphs 0x80-0x86 from HM's
     * clcd_glyphs.h font8x16_ext (R4), anything else a space. */
    unsigned ch = (unsigned char)s_shadow[cell];
    const uint8_t *glyph = font8x16[0];
    if (ch >= CLCD_FONT_FIRST && ch <= CLCD_FONT_LAST)
        glyph = font8x16[ch - CLCD_FONT_FIRST];
    else if (ch >= CLCD_FONT_EXT_FIRST && ch < CLCD_FONT_EXT_FIRST + CLCD_FONT_EXT_COUNT)
        glyph = font8x16_ext[ch - CLCD_FONT_EXT_FIRST];

    /* The colours: the cell's ROLE in the theme it was committed under. In the
     * today theme that is white on black, or white on red on an inverted row --
     * the pre-R4 renderer's two pairs, pixel for pixel. */
    const mps3_clcd_theme_t *t = s_theme ? s_theme : &clcd_theme_today;
    unsigned role = (s_role[cell] < CLCD_ROLE_COUNT) ? s_role[cell] : (unsigned)CLCD_ROLE_TEXT;
    uint16_t fg = t->pal[role][0];
    uint16_t bg = t->pal[role][1];

    unsigned n = 0;
    /* GRAM address window (command byte then its data byte).
     *
     * ORIENTATION-INDEPENDENT BY CONSTRUCTION. x0..x1 / y0..y1 are in the 320x240
     * LOGICAL frame with the origin at the logical top-left, and they stay that
     * way whatever MADCTL (0x16) the init table programs -- including the
     * CLCD_ROTATE_180 flip (hx8347_init.h). MADCTL's MY/MX/MV map the command
     * coordinate space onto the glass as ONE transform, applied alike to this
     * window and to the 0x22 auto-increment that fills it, so the composed image
     * rotates as a whole. Mirroring these coordinates (col -> W-1-col) to
     * "compensate" for the flip would un-rotate cell POSITIONS while leaving the
     * glyph CONTENT rotated: garbage. Leave this math alone. */
    push_pair(&n, 0, HX_REG_COL_START_HI); push_pair(&n, 1, (uint8_t)(x0 >> 8));
    push_pair(&n, 0, HX_REG_COL_START_LO); push_pair(&n, 1, (uint8_t)(x0 & 0xFF));
    push_pair(&n, 0, HX_REG_COL_END_HI);   push_pair(&n, 1, (uint8_t)(x1 >> 8));
    push_pair(&n, 0, HX_REG_COL_END_LO);   push_pair(&n, 1, (uint8_t)(x1 & 0xFF));
    push_pair(&n, 0, HX_REG_ROW_START_HI); push_pair(&n, 1, (uint8_t)(y0 >> 8));
    push_pair(&n, 0, HX_REG_ROW_START_LO); push_pair(&n, 1, (uint8_t)(y0 & 0xFF));
    push_pair(&n, 0, HX_REG_ROW_END_HI);   push_pair(&n, 1, (uint8_t)(y1 >> 8));
    push_pair(&n, 0, HX_REG_ROW_END_LO);   push_pair(&n, 1, (uint8_t)(y1 & 0xFF));
    push_pair(&n, 0, HX_REG_RAMWR);        /* GRAM write; pixels follow as data */

    for (unsigned yy = 0; yy < CLCD_GLYPH_H; yy++) {
        uint8_t bits = glyph[yy];
        for (unsigned xx = 0; xx < CLCD_GLYPH_W; xx++) {
            uint16_t px = (bits & (0x80u >> xx)) ? fg : bg;
            push_pair(&n, 1, (uint8_t)(px >> 8));   /* RGB565 high byte first  */
            push_pair(&n, 1, (uint8_t)(px & 0xFF));
        }
    }
    s_cell_len = n;    /* == CLCD_CELL_BYTES */
    s_cell_pos = 0;
}

/* The render's scan cursor (lane CLCD-SPEED). A render only CLEARS dirty bits
 * (reformat() runs in ST_IDLE, never mid-render), so the lowest dirty cell is at
 * or after the last one picked: starting there returns exactly what a scan from
 * 0 returns, without re-walking the clean prefix every cell (a full screen was
 * 180,000 bit tests, ~14 ms on the 100 MHz MBV). Reset to 0 where a render
 * starts (ST_IDLE); the wrap keeps it correct whatever sets a bit. */
static unsigned s_scan;

static int next_dirty_cell(void)
{
    unsigned i = (s_scan < CLCD_NCELLS) ? s_scan : 0u;
    for (unsigned n = 0; n < CLCD_NCELLS; n++, i++) {
        if (i == CLCD_NCELLS)
            i = 0;
        if (s_dirty[i >> 3] & (1u << (i & 7u))) {
            s_scan = i;
            return (int)i;
        }
    }
    return -1;
}

/* ==========================================================================
 * Reset / init streaming
 * ========================================================================== */
static void write_ctrl(uint32_t bits)
{
    mps3_reg_write32(MPS3_CLCD_BASE, CLCD_CTRL, bits);
}

static void enter_idle_first_draw(void)
{
    s_state        = ST_IDLE;
    s_force_refresh = 1;     /* draw the whole screen at once on entry */
}

/* Stream up to `budget` init-table bytes; arm INIT_WAIT on an HX_DLY. */
static void step_init(uint32_t *budget)
{
    while (s_init_idx < hx8347_init_len) {
        const hx8347_entry_t *e = &hx8347_init[s_init_idx];
        if (e->op == HX_DLY) {
            s_delay_ms = e->val;
            s_t0       = mps3_sys_now_ms();
            s_init_idx++;
            s_state = ST_INIT_WAIT;
            return;
        }
        int rc = try_push(budget, (e->op == HX_DAT) ? 1 : 0, e->val);
        if (rc == 1) { s_init_idx++; continue; }
        return;   /* budget exhausted or FIFO full -- resume next pass */
    }
    enter_idle_first_draw();
}

/* ==========================================================================
 * KVM handover (fpga/shell/ip/clcd_kvm/README.md §5/§7; clcd.h). All three
 * functions are safe to call with no KVM present -- the clcd_kvm_* accessors are
 * compile-time no-ops there, and s_relinquished/s_banner_mode simply never move.
 * ========================================================================== */

/* Mark every cell dirty by voiding the shadow -- the PRODUCTION full-repaint
 * path (the counterpart of the test-only clcd_test_force_reformat()). The next
 * reformat() then differs from every printable cell, so the WHOLE screen is
 * pushed, not just the handful that changed. Used after a panel reset: the
 * panel's GRAM is gone, so a dirty-cell diff would leave most of the screen
 * blank. */
static void mark_all_dirty(void)
{
    memset(s_shadow, 0, sizeof(s_shadow));   /* NUL != any printable glyph */
    memset(s_inv,    0, sizeof(s_inv));
    memset(s_role,   0, sizeof(s_role));
    memset(s_dirty,  0, sizeof(s_dirty));
    s_dirty_count   = 0;
    s_cell_len      = 0;
    s_cell_pos      = 0;
    s_force_refresh = 1;
}

/* We are about to lose the panel to the DUT: paint the OSD banner as the last
 * thing on the glass, then let the normal render pipeline push it during the
 * KVM's S_DRAIN window (the drain gate waits for our FIFO to empty, so whatever
 * we push before we go quiet reaches the panel). Idempotent. */
void clcd_lose(void)
{
    if (s_banner_mode)
        return;
    s_banner_mode = 1;
    if (s_state == ST_IDLE || s_state == ST_RENDER)
        s_force_refresh = 1;      /* reformat() will now paint the banner */
}

/* We just regained the panel -- and the KVM HARD-RESET it during the handover,
 * so its GRAM, window, MADCTL and pixel format are all gone. Re-run the init
 * table and repaint EVERY cell. We do NOT re-pulse CLCD_RST: the KVM already
 * reset the panel (and with bl_rst_src=1 the CLCD block's reset bit is inert),
 * so we go straight to streaming the init sequence. */
void clcd_regain(void)
{
    s_banner_mode  = 0;
    s_relinquished = 0;
    s_init_idx     = 0;
    s_state        = ST_INIT;   /* re-stream the init table into the fresh panel */
    mark_all_dirty();           /* ...then repaint the whole screen, not a diff  */
}

#ifdef MPS3_HAS_CLCD_KVM
/* Firmware interpreter for USER_nPB[1]. We clear the KVM's pb_en once, at the
 * top of the first kvm_service() (on EVERY path, relinquished at start or not),
 * so the hardware no longer auto-toggles ownership and the button is
 * ours to time. Uses PB_LEVEL (the debounced level, always valid) -- NOT
 * PB_TOGGLE, which pb_en gates:
 *   harness owns the panel:  SHORT press -> next page;  LONG hold -> hand to DUT.
 *   DUT owns the panel:      ANY press   -> request the panel back (the hardware
 *                            no longer does this for us once pb_en is clear).
 * Ignored while the KVM drives the pads (mid-handover: the button is not ours).
 *
 * POWER-UP HOLD (D13): a press that is ALREADY DOWN when this service first runs
 * is ignored until PB1 is released once -- it is the "skip the card's default
 * load" escape hatch, sampled as a level by the D13 boot hook, not a UI gesture.
 * Without this gate the first pass would see pb=1 with s_pb_was_down=0, call it
 * a press edge, and 800 ms later hand the panel to the DUT. After the first
 * release every edge is acted on as usual; the release itself does nothing (it
 * is not the end of a short press). */
static void pb_service(uint32_t st, int owner_dut, int kvm_pads)
{
    int      pb  = (st & CLCDKVM_STATUS_PB_LEVEL) ? 1 : 0;
    uint32_t now = mps3_sys_now_ms();

    if (!s_pb_armed) {                  /* held since before we started: not ours */
        if (!pb)
            s_pb_armed = 1;             /* first release -- edges count from here */
        s_pb_was_down = pb;
        return;
    }
    if (kvm_pads) {                     /* mid-handover -- not our button now    */
        s_pb_was_down = pb;
        return;
    }
    if (owner_dut) {                    /* DUT owns it -- any press returns it   */
        if (pb && !s_pb_was_down)
            clcd_kvm_request_owner(CLCDKVM_OWNER_HARNESS);
        s_pb_was_down = pb;
        return;
    }
    /* Harness owns the panel. */
    if (pb && !s_pb_was_down) {         /* press edge: arm the duration timer    */
        s_pb_down_ms    = now;
        s_pb_long_fired = 0;
    }
    if (pb && s_pb_was_down && !s_pb_long_fired &&
        (uint32_t)(now - s_pb_down_ms) >= CLCD_PB_LONG_MS) {
        clcd_kvm_request_owner(CLCDKVM_OWNER_DUT);   /* long hold -> hand over   */
        s_pb_long_fired = 1;
    }
    if (!pb && s_pb_was_down && !s_pb_long_fired)     /* short release -> cycle   */
        clcd_page_next();
    s_pb_was_down = pb;
}

/* Run once per clcd_poll(), before the render FSM. Reads the KVM's STATUS and
 * takes (W1C) its EVENTs, and drives the three handover actions. Folds to
 * nothing when the KVM is not built. */
static void kvm_service(void)
{
    /* ONE-TIME SETUP, FIRST, UNCONDITIONALLY. This used to live in ST_RESET --
     * but ST_RESET is reached only if the first pass does NOT relinquish. When
     * the DUT already owns the panel on the first clcd_poll() (a hardware PB1
     * toggle before this firmware ran, or a harness restart while the DUT held
     * it), the driver relinquishes at once; on regain clcd_regain() goes straight
     * to ST_INIT. So clcd_kvm_init() and the pb_en clear NEVER ran: bl_rst_src
     * stayed 0 and pb_en stayed 1, and every PB1 press was acted on TWICE -- the
     * hardware toggled ownership AND pb_service() did its own thing. Here, before
     * STATUS/EVENT are read, it runs exactly once on every path.
     *
     * clcd_kvm_init() also W1C-clears whatever the KVM latched before we first
     * looked. That loses nothing: the first pass after clcd_init() is already a
     * full re-init + repaint (ST_RESET, or relinquished and then clcd_regain()). */
    if (!s_kvm_setup) {
        clcd_kvm_init();   /* bl_rst_src=1 + backlight + panel released, ONE write */
        /* Take the button: with pb_en CLEAR the hardware no longer toggles
         * ownership on a press, so pb_service() can time PB_LEVEL for page
         * navigation (short) vs handover (long) and is the ONLY actor on PB1.
         * ctrl_update() is the button-safe RMW that never moves ownership. */
        clcd_kvm_ctrl_update(0u, CLCDKVM_CTRL_PB_EN);
        s_kvm_setup = 1;
    }

    uint32_t st = clcd_kvm_status();
    uint32_t ev = clcd_kvm_take_events();   /* W1C: clears exactly what it read */

    int owner_dut = clcd_kvm_owner_is_dut(st);
    int pending   = clcd_kvm_switch_pending(st);
    int kvm_pads  = clcd_kvm_drives_pads(st);

    /* Firmware button interpreter (page nav + return-from-DUT). Must run before
     * the relinquish handling below, so a press while the DUT owns the panel can
     * still ask for it back. */
    pb_service(st, owner_dut, kvm_pads);

    /* (1) LOSING. A switch away from the harness is pending and we still drive
     * the pads (the KVM is in S_DRAIN: owner still HARNESS, kvm_drives_pads
     * still 0). Paint the OSD now -- this is the only window in which it can
     * reach the glass. */
    if (!owner_dut && !kvm_pads && pending)
        clcd_lose();

    /* (2) RELINQUISH. The DUT owns the pads, or the KVM is mid-handover driving
     * its idle pattern. Stop pushing and stop reformatting: pushing into a KVM
     * that is discarding us is pointless, and spinning on a STATUS that is no
     * longer ours is exactly what a cooperative poll must not do. */
    if (owner_dut || kvm_pads)
        s_relinquished = 1;

    /* (3) REGAIN / panel-reset-under-us. THE handover rule
     * (harness_gained | panel_reset_done) -- NOT harness_gained alone, because a
     * DFX interlock firing mid-handover resets the panel and hands it back with
     * no owner change (README §7). Only act once we actually hold the pads
     * again, so the init stream lands in a panel that is ours. */
    if (!owner_dut && !kvm_pads && (ev & CLCDKVM_EVENT_REINIT_MASK))
        clcd_regain();
}
#else
static void kvm_service(void) { }   /* no KVM: nothing to service */
#endif

/* ==========================================================================
 * Public API
 * ========================================================================== */
void clcd_set_board_name(const char *name)
{
    unsigned i = 0;
    if (!name) return;
    while (name[i] && i < sizeof(s_board_name) - 1u) {
        s_board_name[i] = name[i];
        i++;
    }
    s_board_name[i] = '\0';
    s_force_refresh = 1;   /* identity changed -- redraw */
}

void clcd_init(void)
{
    /* Software state only -- all MMIO happens in clcd_poll() so init cannot
     * block. The reset pulse, init stream and settle are driven by later
     * passes (the panel comes up AFTER the network; it gets no blocking
     * license, plan section 7). */
    s_init_idx       = 0;
    s_delay_ms       = 0;
    s_last_refresh   = 0;
    s_force_refresh  = 1;
    s_refresh_tick   = 0;
    s_dirty_count    = 0;
    s_cell_len       = 0;
    s_cell_pos       = 0;
    s_swap_count     = 0;
    s_prev_swap_valid = 0;
    s_usd_seen        = overlay_store_usd_change_count();  /* O(1) getter; no MMIO */
    s_bytes_last_pass = 0;
    s_bytes_total     = 0;
    s_relinquished    = 0;
    s_banner_mode     = 0;
    s_page            = CLCD_PAGE_STATUS;
    s_theme           = 0;       /* the first reformat asks mps3_clcd_palette() */
    s_banner_kind     = CLCD_BANNER_NONE;
    s_overlay_on      = CLCD_ON_NONE;
    s_banner_text[0]  = '\0';
    s_frame_seq       = 0;
    s_prog_pct        = 255u;
    s_event_seq       = 0;
    memset(s_events, 0, sizeof(s_events));
#ifdef MPS3_HAS_CLCD_KVM
    s_kvm_setup       = 0;
    s_pb_down_ms      = 0;
    s_pb_was_down     = 0;
    s_pb_long_fired   = 0;
    s_pb_armed        = 0;   /* ignore a PB1 already held at power-up (pb_service) */
#endif
#ifdef MPS3_HAS_TOUCH
    s_touch_setup     = 0;
    s_touch_sampled   = 0;
    s_touch_last_ms   = 0;
#endif

    /* Shadow starts NUL so the first reformat differs from every printable
     * cell -> the whole screen is drawn once. */
    memset(s_shadow, 0, sizeof(s_shadow));
    memset(s_inv, 0, sizeof(s_inv));
    memset(s_role, 0, sizeof(s_role));
    memset(s_dirty, 0, sizeof(s_dirty));

    /* Deterministic board name: the MPS3_BOARD_NAME build-time default (clcd.h),
     * "MPS3-01" -- this board's fpgahub node is mps3_01. A host-pushed name still
     * wins at any time through the clcd_set_board_name() seam (not wired to a
     * control verb here). Seeded THROUGH that same setter so there is exactly one
     * path that writes s_board_name (and one bound check). */
    clcd_set_board_name(MPS3_BOARD_NAME);

    s_state = ST_RESET;
    s_t0    = mps3_sys_now_ms();
}

void clcd_poll(void)
{
    uint32_t budget = CLCD_BYTES_PER_PASS;
    uint32_t now    = mps3_sys_now_ms();

    /* Handover supervision runs first, every pass. It may paint the OSD, set
     * s_relinquished, or restart the init stream on regain. No-op without a KVM. */
    kvm_service();

#ifdef MPS3_HAS_TOUCH
    /* ONE-TIME touch bring-up (AXI IIC + STMPE811; MMIO, so here and not in
     * clcd_init()), on the FIRST pass, BEFORE the relinquish return below. It
     * used to live in ST_RESET -- the same trap as the KVM setup: when the DUT
     * already owns the panel on the first clcd_poll(), the driver relinquishes
     * at once and clcd_regain() later jumps straight to ST_INIT, so ST_RESET
     * never ran and touch stayed dead for the whole boot (touch_poll() is inert
     * until touch_init() succeeds). The touch controller is shell-side I2C, not
     * part of the panel mux, so bringing it up while the DUT owns the glass is
     * harmless; touch_poll() itself still waits until we hold the panel.
     * Dormant unless TOUCH=1 -- the IIC block only exists after the fabric mint. */
    if (!s_touch_setup) { touch_init(); s_touch_setup = 1; }
#endif

    /* We do not own the pads: push nothing, reformat nothing, do not spin on a
     * STATUS that is not ours. We just wait here for kvm_service() to clear the
     * flag on regain. (Never reached without a KVM: s_relinquished stays 0.) */
    if (s_relinquished) {
        s_bytes_last_pass = 0;
        return;
    }

#ifdef MPS3_HAS_TOUCH
    /* On-screen nav: a tap on the bottom nav button cycles the page, exactly as
     * the USER_nPB1 short-press does. touch_poll() is a single register read when
     * there is no touch. Dormant unless TOUCH=1 (the AXI IIC + TSC only exist
     * after the fabric mint). */
    /* RATE-LIMITED to one sample per CLCD_TOUCH_PERIOD_MS (clcd.h): a held
     * finger must not cost a burst of I2C transactions on every ~0.7 ms pass. */
    if (!s_touch_sampled ||
        (uint32_t)(now - s_touch_last_ms) >= CLCD_TOUCH_PERIOD_MS) {
        s_touch_sampled = 1;
        s_touch_last_ms = now;
        if (touch_poll() == CLCD_ACT_NEXT_PAGE)
            clcd_page_next();
    }
#endif

    switch (s_state) {

    case ST_RESET:
        /* With a KVM present, kvm_service() has already handed BL/RST to it
         * (bl_rst_src=1, the one-time setup at its top), so the CLCD block's own
         * reset bit below is INERT -- harmless to write, but the real panel reset
         * comes from the KVM's hardware sequencer at every handover. Touch is
         * brought up at the top of clcd_poll(), not here (see there). */
        /* Assert panel reset (CLCD_RST active-low: reset_n=0 = held in reset),
         * enable the block + flush its FIFO, backlight off. Also (re)program the
         * 8080 strobe timing to its sane default. */
        mps3_reg_write32(MPS3_CLCD_BASE, CLCD_TIMING,
                         (4u << CLCD_TIMING_WR_LO_SHIFT) |
                         (4u << CLCD_TIMING_WR_HI_SHIFT) |
                         (2u << CLCD_TIMING_CS_SETUP_SHIFT));
        write_ctrl(CLCD_CTRL_ENABLE | CLCD_CTRL_FIFO_RESET);  /* reset_n=0 held */
        s_t0    = now;
        s_state = ST_RST_WAIT;
        break;

    case ST_RST_WAIT:
        if ((uint32_t)(now - s_t0) >= CLCD_RESET_PULSE_MS) {
            /* Release reset (reset_n=1) and turn the backlight on, then go
             * straight to streaming the init table -- its leading {HX_DLY,5}
             * supplies the datasheet post-reset settle, so the driver owns no
             * settle constant. CLCD_BL is ACTIVE-HIGH and CLCD_RST ACTIVE-LOW --
             * BOTH confirmed on the board 2026-07-14 (docs/CLCD_PANEL_FACTS.md
             * §4): the panel is lit, and it could not be unless BL=1 lights it
             * and RST=1 releases it. (This comment previously said the polarity
             * was "UNVERIFIED ... to be confirmed at bring-up"; it has been.)
             * With a KVM present and bl_rst_src=1 these CTRL bits are inert --
             * the KVM drives the pads -- but writing them is harmless. */
            write_ctrl(CLCD_CTRL_ENABLE | CLCD_CTRL_RESET_N | CLCD_CTRL_BACKLIGHT);
            s_init_idx = 0;
            s_state    = ST_INIT;
        }
        break;

    case ST_INIT:
        step_init(&budget);
        break;

    case ST_INIT_WAIT:
        if ((uint32_t)(now - s_t0) >= s_delay_ms)
            s_state = ST_INIT;
        break;

    case ST_IDLE:
        /* A USD change-count move (card in/out, state change) refreshes AT ONCE
         * rather than on the next 250 ms tick; the diff keeps the cost to the
         * few cells that changed. The getter is O(1) (clcd.h). */
        if (s_force_refresh ||
            overlay_store_usd_change_count() != s_usd_seen ||
            (uint32_t)(now - s_last_refresh) >= CLCD_REFRESH_MS) {
            s_force_refresh = 0;
            s_last_refresh  = now;
            reformat();
            if (s_dirty_count > 0) {
                s_state = ST_RENDER;   /* start pushing next pass */
                s_scan  = 0;           /* lowest dirty cell first, as ever */
            }
        }
        break;

    case ST_RENDER:
        for (;;) {
            if (s_cell_pos >= s_cell_len) {
                int cell = next_dirty_cell();
                if (cell < 0) {
                    s_state = ST_IDLE;   /* all dirty cells rendered */
                    break;
                }
                build_cell((unsigned)cell);
                /* Clear the bit now so it is not re-picked; if this pass is
                 * interrupted mid-cell the active-cell cursor resumes it. */
                s_dirty[(unsigned)cell >> 3] &= (uint8_t)~(1u << ((unsigned)cell & 7u));
                if (s_dirty_count) s_dirty_count--;
            }
            /* The cell's next contiguous run, capped by this pass's budget,
             * through the bus seam (clcd.h) -- one call, not one per byte. */
            uint32_t n = s_cell_len - s_cell_pos;
            if (n > budget)
                n = budget;
            if (n == 0)
                break;   /* budget spent -- resume next pass */
            uint32_t got = clcd_bus_push(&s_cell_rs[s_cell_pos], &s_cell_val[s_cell_pos], n);
            s_cell_pos    += got;
            budget        -= got;
            s_bytes_total += got;
            if (got < n)
                break;   /* FIFO full -- resume next pass */
        }
        break;

    default:
        s_state = ST_RESET;
        break;
    }

    s_bytes_last_pass = CLCD_BYTES_PER_PASS - budget;
}

/* See clcd.h (THE DRAIN). */
uint32_t clcd_poll_drain(uint32_t budget_us)
{
    const uint32_t t0 = mps3_sys_now_us();
    uint32_t total = 0;
    for (;;) {
        clcd_state_t before = s_state;
        clcd_poll();
        total += s_bytes_last_pass;
        int more = (s_bytes_last_pass == CLCD_BYTES_PER_PASS) ||
                   (before != ST_RENDER && s_state == ST_RENDER);
        if (!more || (uint32_t)(mps3_sys_now_us() - t0) >= budget_us)
            return total;
    }
}

/* ==========================================================================
 * Test-only introspection
 * ========================================================================== */
#ifdef MPS3_CLCD_TEST_HOOKS
int      clcd_test_state(void)           { return (int)s_state; }
uint32_t clcd_test_bytes_last_pass(void) { return s_bytes_last_pass; }
uint32_t clcd_test_bytes_total(void)     { return s_bytes_total; }
unsigned clcd_test_dirty_count(void)     { return s_dirty_count; }
uint32_t clcd_test_swap_count(void)      { return s_swap_count; }
void     clcd_test_force_reformat(void)  { s_force_refresh = 1; }
int      clcd_test_relinquished(void)    { return s_relinquished; }
int      clcd_test_banner_mode(void)     { return s_banner_mode; }
void     clcd_test_set_banner(int on)    { s_banner_mode = on ? 1 : 0; s_force_refresh = 1; }
void     clcd_test_set_page(int page)    { clcd_page_set((clcd_page_t)page); s_force_refresh = 1; }

/* Build one frame from the CURRENT sources straight into the caller's buffers,
 * bypassing the FSM. Faithful: it runs the same reformat() the superloop runs,
 * which commits the frame into s_shadow/s_inv (clcd.c:452-455). Used by the
 * off-target screen preview (firmware/clcd/tools/clcd_preview.c) to render the
 * exact §8 layout for a seeded platform state, no simulator or panel needed. */
void clcd_test_render(char out[CLCD_NCELLS], uint8_t inv_out[CLCD_ROWS])
{
    reformat();
    memcpy(out,     s_shadow, (size_t)CLCD_NCELLS);
    memcpy(inv_out, s_inv,    (size_t)CLCD_ROWS);
}

void clcd_test_shadow(char out[CLCD_NCELLS], uint8_t inv_out[CLCD_ROWS])
{
    memcpy(out,     s_shadow, (size_t)CLCD_NCELLS);
    memcpy(inv_out, s_inv,    (size_t)CLCD_ROWS);
}

void clcd_test_roles(uint8_t out[CLCD_NCELLS])
{
    memcpy(out, s_role, (size_t)CLCD_NCELLS);
}
#endif
