/* clcd_preview.c — off-target render of the CLCD §8 status screen.
 *
 * Runs the REAL renderer (firmware/clcd/clcd.c reformat(), via the
 * clcd_test_render() hook) against seeded platform state and prints the exact
 * 40x15 character grid — no simulator, no panel. This is the expected-output
 * reference for board bring-up (docs/CLCD_STATUS_DISPLAY_PLAN.md §8) and a
 * layout check: it catches field overflow / collisions before silicon.
 *
 * It does NOT prove font pixels or panel timing (the cocotb bench does the
 * former for glyph 'A'; the panel proves the rest). It proves the LAYOUT and
 * the field FORMATTERS, from real code, across the states that matter.
 *
 * Build (same host pattern as firmware/test/test_clcd):
 *   gcc -DMPS3_HAL_MOCK -DMPS3_CLCD_TEST_HOOKS -Ifirmware/common -Ifirmware/test \
 *       firmware/clcd/tools/clcd_preview.c firmware/clcd/clcd.c \
 *       firmware/clcd/hx8347_init.c firmware/test/mock_regs.c -o /tmp/clcd_preview
 *   /tmp/clcd_preview          # human view
 *   /tmp/clcd_preview --json   # machine view (for an artifact / CI)
 *   /tmp/clcd_preview --usd    # row 4 only, once per D13 user-microSD state
 *   /tmp/clcd_preview --roles  # human view + each row's colour-role codes
 *   /tmp/clcd_preview --png DIR  # every scenario as DIR/<name>.png (+ @2x)
 *   /tmp/clcd_preview --only NAME[,NAME]  # restrict any view to these scenarios
 *
 * THE ALIGNED LOOK (Harness Manager CLCD_ALIGNMENT R4): the "aligned-*" scenarios
 * render with clcd_theme_aligned -- HM's palette (the vendored clcd_palette.h),
 * HM's words, the status glyphs, the R5 progress bar -- through the strong seam
 * providers this file supplies the way mps3-harnessd does (a Harness Manager
 * session row, the lease badge, a lease-request overlay, identify). The other
 * scenarios are the default theme and print exactly as before.
 *
 * --png draws PIXELS, not the text grid: each scenario runs clcd_init() and the
 * real clcd_poll() render loop over mock registers, and a small GRAM model takes
 * every CMD/DATA byte the driver streams (window 0x02-0x09, RAMWR 0x22, RGB565
 * high byte first) -- so the PNG is what the driver would put on the glass,
 * colours and glyphs included. The 1x file is the panel's 320x240; @2x doubles
 * it for review next to HM's docs/design/clcd/mockup.html.
 *
 * In the human view a status glyph prints as a one-column stand-in (HM's ASCII
 * fallbacks where one character): ok '✓', err '✕', warn '!', held '#',
 * user '@', unk '?', dot '•'. --json keeps the real byte as \u0080..\u0086.
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Use the REAL shell struct definitions — clcd.c is compiled against these, so
 * a hand-rolled forward decl (e.g. int vs bool fields) would read every field
 * at the wrong offset. Mirror test_clcd.c's include set exactly. */
#include "platform_regs.h"
#include "net_proto.h"
#include "diag.h"
#include "../../coordinator/coordinator.h"
#include "../../coordinator/swap_fsm.h"
#include "mock_regs.h"
#include "../clcd.h"

/* === symbols clcd.c links against (the firmware/test/ fake pattern) ========= */
mps3_shell_state_t   g_shell_state;
volatile mps3_diag_t g_mps3_diag;

static mps3_swap_result_t s_swap_res;
const mps3_swap_result_t *swap_fsm_last_result(void) { return &s_swap_res; }

static uint32_t s_icap_bytes;
uint32_t swap_fsm_icap_bytes(void) { return s_icap_bytes; }

static int      s_link_up;
static uint16_t s_anlpar;
int smsc911x_link_up(void) { return s_link_up; }
int smsc911x_mii_read(uint32_t reg, uint16_t *val_out)
{ if (val_out) *val_out = (reg == 0x05u) ? s_anlpar : 0u; return 0; }

static uint8_t s_mac[6] = { 0x02, 0x00, 0x00, 0x4D, 0x50, 0x53 };
void mps3_platform_mac(uint8_t mac[6]) { memcpy(mac, s_mac, 6); }

/* The overlay store's D13 exports (lane I-FW) -- STRONG here, so they beat
 * clcd.c's weak "no hw" fallback and the preview can pose any card state on
 * row 4 right ("USD : <text>", clcd.h). The text is the store's `text` field
 * verbatim (net-protocol.md "usd"). */
static const char *s_usd_text = "none";
const char *overlay_store_usd_text(void)         { return s_usd_text; }
uint32_t    overlay_store_usd_change_count(void) { return 0u; }

/* === the seams mps3-harnessd provides (strong here, switchable per scenario) ===
 * All off by default: a default-theme scenario renders exactly as the bare-metal
 * image (the weak defaults' behaviour; firmware/test/test_clcd_roles.c proves the
 * two equal, byte for byte). */
static const mps3_clcd_theme_t *s_theme = &clcd_theme_today;
const mps3_clcd_theme_t *mps3_clcd_palette(void) { return s_theme; }

static int s_badge_on;
static mps3_clcd_badge_t s_badge;
int mps3_clcd_title_right(mps3_clcd_badge_t *out) { if (!s_badge_on) return 0; *out = s_badge; return 1; }

static int s_sess_on;
static clcd_line_t s_sess;
int mps3_clcd_session_row(clcd_line_t *out) { if (!s_sess_on) return 0; *out = s_sess; return 1; }

static int s_ovl_on;
static mps3_clcd_overlay_t s_ovl;
int mps3_clcd_overlay(mps3_clcd_overlay_t *out) { if (!s_ovl_on) return 0; *out = s_ovl; return 1; }

static const char *s_locate_who;   /* NULL = no locate */
int mps3_clcd_locate(char *who, unsigned cap)
{
    if (!s_locate_who) return 0;
    snprintf(who, cap, "%s", s_locate_who);
    return 1;
}

static int s_engine_on;
int mps3_clcd_engine(mps3_clcd_engine_t *out)
{
    if (!s_engine_on) return 0;
    out->impl = "linux";
    out->claimed = 1;
    memcpy(out->fpr, "AbCdEfGh", 9);
    return 1;
}

static mps3_swap_progress_t s_prog;
void swap_fsm_progress(mps3_swap_progress_t *out) { *out = s_prog; if (!out->rm) out->rm = ""; }

static void seams_off(void)
{
    s_theme = &clcd_theme_today;
    s_badge_on = s_sess_on = s_ovl_on = s_engine_on = 0;
    s_locate_who = 0;
    memset(&s_prog, 0, sizeof(s_prog));
}

/* CLCD FIFO: reformat() never touches it, but seed a benign STATUS anyway. */
static int fifo_hook(void *c, int wr, uint32_t b, uint32_t off, uint32_t *v)
{ (void)c;(void)b; if (!wr) *v = (off==CLCD_STATUS)? CLCD_STATUS_FIFO_EMPTY : 0u; return 1; }

/* === the LIVE resident-RM source (DFXCTL CSR) ==============================
 * The panel reads the RESIDENT rm_id out of hardware, not out of
 * g_shell_state.current_rm_id (see clcd.h). So the preview seeds the CSR --
 * seeding the cache would render a screen the board will never show. */
static void seed_rm_live(uint32_t rm_id)      /* coupled, released, id settled */
{
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,    0u);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_ID,     rm_id);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS, DFXCTL_RM_STATUS_RM_ID_VALID);
}

/* === scenarios ============================================================= */
static void base_healthy(void)
{
    mock_regs_reset();
    memset(&g_shell_state, 0, sizeof(g_shell_state));
    memset((void *)&g_mps3_diag, 0, sizeof(g_mps3_diag));
    memset(&s_swap_res, 0, sizeof(s_swap_res));
    g_shell_state.static_id     = 0x14E1A2D8u;   /* 1 MiB shell (53f0241) */
    /* rm_id v2: { major[31:24], minor[23:16], design_id[15:0] } -- so this is
     * design 0x0001 (nanosoc) at v1.0 (docs/VERSIONING_PLAN.md §3.2). Seeded
     * into the DFXCTL CSR because that is what the glass reads; the firmware
     * cache below is set to AGREE only so the preview shows a steady-state
     * board (the panel would render identically if it disagreed). */
    seed_rm_live(0x01000001u);                   /* nanosoc v1.0, resident */
    g_shell_state.current_rm_id = 0x01000001u;   /* what the swap FSM verified */
    s_icap_bytes = 1835072u;
    s_link_up = 1;
    s_anlpar  = (1u << 8);                        /* 100BASE-TX full duplex */
    s_swap_res.valid = 1; s_swap_res.ok = 1; s_swap_res.verified = 1;
    s_swap_res.rm_id = 0x01000001u;
    g_mps3_diag.rx_drop_frames = 0;
    g_mps3_diag.tx_errors      = 0;
    mock_regs_poke(MPS3_CLKRST_BASE, CLKRST_STATUS,
                   CLKRST_STATUS_MMCM_LOCKED | CLKRST_STATUS_DUT_CLK_ALIVE);
    mock_regs_poke(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL, CLKRST_RESET_CTRL_DUT_RESETN);
    mock_regs_set_hook(MPS3_CLCD_BASE, fifo_hook, 0);
    mock_time_set_ms(171907000u);                /* uptime 001:23:45:07 */
    /* clcd_init() seeds this from MPS3_BOARD_NAME (clcd.h); set it through the
     * same seam with the same macro, since the preview renders without walking
     * the init FSM. Do NOT hardcode the string here -- that is how the preview
     * would keep showing an old name after a rename. */
    clcd_set_board_name(MPS3_BOARD_NAME);
    clcd_test_set_banner(0);   /* default: the status screen, not the OSD */
    clcd_test_set_page(CLCD_PAGE_STATUS);
    s_usd_text = "none";       /* default: no user microSD in the slot     */
    seams_off();               /* default: the bare-metal look             */
}
static void sc_healthy(void)        { base_healthy(); }
static void sc_swap_failed(void)    { base_healthy(); s_swap_res.ok = 0; s_swap_res.verified = 0; }
static void sc_link_down(void)      { base_healthy(); s_link_up = 0; s_anlpar = 0; }
static void sc_clock_dead(void)     { base_healthy();
    mock_regs_poke(MPS3_CLKRST_BASE, CLKRST_STATUS, 0u); }      /* !locked, !alive */
static void sc_unprovisioned(void)  { base_healthy(); g_shell_state.static_id = 0u; }
static void sc_greybox_boot(void)   { base_healthy();
    seed_rm_live(0u);                                     /* design 0, v0.0    */
    g_shell_state.current_rm_id = 0u; s_icap_bytes = 0;
    memset(&s_swap_res, 0, sizeof(s_swap_res)); mock_time_set_ms(3000u); }
static void sc_eth_ss(void)         { base_healthy();       /* MAC DUT, no CPU */
    seed_rm_live(0x01000002u);
    g_shell_state.current_rm_id = 0x01000002u; s_swap_res.rm_id = 0x01000002u; }

/* THE row-2 overflow case: "nanosoc_multicore" is 17 chars -- the longest name
 * in the table, and the one that silently ate the field beside it under the old
 * layout (name at col 6, a second field at col 21 => 14 columns for the name).
 * Render it next to its version; both must fit, clear of each other. */
static void sc_multicore(void)      { base_healthy();
    seed_rm_live(0x01000003u);
    g_shell_state.current_rm_id = 0x01000003u; s_swap_res.rm_id = 0x01000003u;
    s_icap_bytes = 2100480u; }

/* An id no table entry claims -- the glass must show it RAW rather than invent
 * a name. Also the shape a PRE-v2 partial takes once ids are re-encoded. */
static void sc_unknown_rm(void)     { base_healthy();
    seed_rm_live(0x0100DEADu);
    g_shell_state.current_rm_id = 0x0100DEADu; s_swap_res.rm_id = 0x0100DEADu; }

/* ===== the three states the LIVE read exists for =========================== */

/* THE SILICON BUG. The multicore RM is JTAG-loaded straight into the RP: the
 * firmware never ran a swap, so its cache still says greybox (0) -- and the old
 * panel rendered the cache. The RP is what it is; the glass must say so. */
static void sc_jtag_swapped(void)   { base_healthy();
    seed_rm_live(0x01000003u);                   /* the RP: multicore v1.0     */
    g_shell_state.current_rm_id = 0u;            /* the cache: still "greybox" */
    memset(&s_swap_res, 0, sizeof(s_swap_res));  /* this fw ran no swap at all */
    s_icap_bytes = 0; }

/* Mid-swap: the RP is isolated, so RM_ID reads the decoupler's clamp (0x0).
 * "0 == greybox" is a SOFTWARE convention -- the hardware is telling us nothing
 * at all here, and the glass must not turn a clamp into a design name. */
static void sc_rp_decoupled(void)   { base_healthy();
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,    DFXCTL_STATUS_DECOUPLED);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_ID,     0u);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS, 0u); }

/* Coupled and released, but dfx_ctl's settle detector has not qualified the id
 * (a reconfiguration in flight -- or a shell that never asserts the bit). The
 * value on the bus may even LOOK like a real design. We still don't know. */
static void sc_rm_id_unstable(void) { base_healthy();
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,    0u);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_ID,     0x01000003u);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS, 0u); }   /* !rm_id_valid */

/* ===== D13: the user microSD on row 4, cols 18..39 ========================
 * "USD : " + the store's text. None of these may raise the banner (rows 10-12):
 * a missing or foreign card is not a harness fault. */
static void sc_usd_default(void)    { base_healthy(); s_usd_text = "nanosoc [A]"; }
static void sc_usd_widest(void)     { base_healthy(); seed_rm_live(0x01000003u);
    g_shell_state.current_rm_id = 0x01000003u; s_swap_res.rm_id = 0x01000003u;
    s_usd_text = "nanosoc_mult [B]"; }                    /* 12 + 4 = 16: col 39 */
static void sc_usd_foreign(void)    { base_healthy(); s_usd_text = "foreign"; }
/* PB1 held at power-up: the boot hook skipped the card's default, so this is a
 * fresh greybox boot with no swap -- listed with the other no-swap scenarios. */
static void sc_usd_skipped(void)    { sc_greybox_boot(); s_usd_text = "skipped"; }
static void sc_usd_no_hw(void)      { base_healthy(); s_usd_text = "no hw"; }

/* The KVM handover OSD: the last thing on the glass before the DUT takes over,
 * so the user knows why the status screen vanished and how to get it back. */
static void sc_dut_banner(void)     { base_healthy(); clcd_test_set_banner(1); }

/* ===== the Applications & Ports page (Phase 1), per resident RM ============
 * The service list + attach command is keyed on the LIVE rm_id, so it changes
 * with the RP: a CPU RM shows its console UARTs + debug bridges, eth_ss/greybox
 * show only the shell-side services. */
static void sc_apps_nanosoc(void)   { base_healthy(); seed_rm_live(0x01000001u);
    g_shell_state.current_rm_id = 0x01000001u; clcd_test_set_page(CLCD_PAGE_APPS); }
static void sc_apps_multicore(void) { base_healthy(); seed_rm_live(0x01000003u);
    g_shell_state.current_rm_id = 0x01000003u; clcd_test_set_page(CLCD_PAGE_APPS); }
static void sc_apps_eth_ss(void)    { base_healthy(); seed_rm_live(0x01000002u);
    g_shell_state.current_rm_id = 0x01000002u; clcd_test_set_page(CLCD_PAGE_APPS); }
static void sc_apps_greybox(void)   { base_healthy(); seed_rm_live(0u);
    g_shell_state.current_rm_id = 0u; clcd_test_set_page(CLCD_PAGE_APPS); }

/* ===== THE ALIGNED LOOK (the Linux harness, HM CLCD_ALIGNMENT R4/R5/R2) =====
 * The same board as "healthy", drawn in clcd_theme_aligned, with the seams a
 * Harness Manager session feeds (HM mock-up: docs/design/clcd/mockup.html). */
static void aligned_base(void)
{
    base_healthy();
    s_theme = &clcd_theme_aligned;
    clcd_set_board_name("mps3-01");                  /* the N1 name, from hello */
    s_usd_text = "nanosoc [A]";
    s_engine_on = 1;
    s_badge_on = 1;                                  /* the lease a holder relayed */
    snprintf(s_badge.text, sizeof(s_badge.text), CLCD_GS_HELD " alice 1h12m, 1 waiting");
    s_badge.role = CLCD_ROLE_TITLE_HELD;
    s_sess_on = 1;                                   /* who is connected */
    clcd_line_clear(&s_sess);
    clcd_line_put(&s_sess, 0, "hm", CLCD_ROLE_LABEL);
    unsigned c = clcd_line_put(&s_sess, 7, CLCD_GS_USER "alice@lab-pc01", CLCD_ROLE_VALUE);
    clcd_line_put(&s_sess, c + 2u, "+1 watching", CLCD_ROLE_LABEL);
}
static void sc_al_status(void)    { aligned_base(); }
static void sc_al_multicore(void) { aligned_base(); seed_rm_live(0x01000003u);
    g_shell_state.current_rm_id = 0x01000003u; s_swap_res.rm_id = 0x01000003u;
    s_icap_bytes = 2100480u; }
static void sc_al_apps(void)      { aligned_base(); clcd_test_set_page(CLCD_PAGE_APPS); }
/* Mid-programming: the RP is decoupled, the partial is streaming, 42 % of the
 * clearing + partial bytes are in the ICAP (R5: the board's own count). */
static void sc_al_program(void)   { aligned_base();
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS, DFXCTL_STATUS_DECOUPLED);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_ID, 0u);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS, 0u);
    memset(&s_swap_res, 0, sizeof(s_swap_res));      /* swap_fsm_start() cleared it */
    s_prog.active = 1; s_prog.state = SWAP_AWAIT_PARTIAL; s_prog.rm = "nanosoc";
    s_prog.total = 2057216u; s_prog.done = 864031u; }   /* 42 % */
static void sc_al_identify(void)  { aligned_base(); s_locate_who = "alice@lab-pc01"; }
static void sc_al_request(void)   { aligned_base();
    s_ovl_on = 1; memset(&s_ovl, 0, sizeof(s_ovl));
    snprintf(s_ovl.line[0], sizeof(s_ovl.line[0]), CLCD_GS_HELD " bob@lab-pc02 wants this board");
    snprintf(s_ovl.line[1], sizeof(s_ovl.line[1]), "held by alice  1:43 to answer");
    snprintf(s_ovl.line[2], sizeof(s_ovl.line[2]), "tap: tell alice you are here");
    s_ovl.role = CLCD_ROLE_BANNER_HELD; s_ovl.on = CLCD_ON_REQUEST; }
static void sc_al_link_down(void) { aligned_base(); s_link_up = 0; s_anlpar = 0; }
static void sc_al_dut(void)       { aligned_base(); clcd_test_set_banner(1); }

struct scenario { const char *name; const char *note; void (*seed)(void); };
/* ORDER MATTERS: the swap counter is a cumulative static (it counts LOADED
 * edges), so every scenario in which THIS FIRMWARE has run no swap must render
 * before any scenario that seeds a completed one -- otherwise the count carries
 * over and a "SWAP: IDLE (boot)" row renders next to a nonzero "#001". That is
 * an artifact of replaying scenarios in one process (on the board the counter is
 * genuinely cumulative and right), but it is exactly the kind of plausible-
 * looking wrong number this batch exists to stamp out, so: no-swap first. */
static const struct scenario SCENARIOS[] = {
    { "greybox-boot",   "fresh boot, greybox, no swap yet",              sc_greybox_boot },
    { "usd-skipped",    "user uSD: PB1 held at power-up -> greybox",     sc_usd_skipped },
    { "jtag-swapped",   "RP JTAG-loaded behind the fw -> LIVE id wins",  sc_jtag_swapped },
    { "healthy",        "nanoSoC loaded + verified, link up, no banner", sc_healthy },
    { "multicore",      "LONGEST name (17ch) + version -- the overflow", sc_multicore },
    { "rp-decoupled",   "RP isolated -> clamped 0x0 is NOT 'greybox'",   sc_rp_decoupled },
    { "rm-id-unstable", "rm_id_valid clear -> say so, never a name",     sc_rm_id_unstable },
    { "eth_ss",         "ethernet-MAC DUT loaded (no CPU, no UART)",     sc_eth_ss },
    { "unknown-rm",     "unrecognised rm_id -> raw hex, never a lie",    sc_unknown_rm },
    { "usd-default",    "user uSD: default loaded from slot A (row 4)",  sc_usd_default },
    { "usd-widest",     "user uSD: 12-char name + [B] = 16, to col 39",  sc_usd_widest },
    { "usd-foreign",    "user uSD: PC card, never written, no banner",   sc_usd_foreign },
    { "usd-no-hw",      "shell without usd_spi (weak fallback text)",    sc_usd_no_hw },
    { "swap-failed",    "last swap FAILED -> banner",                    sc_swap_failed },
    { "link-down",      "network link down -> banner (highest rank)",    sc_link_down },
    { "clock-dead",     "DUT clock not running -> banner",               sc_clock_dead },
    { "unprovisioned",  "static_id 0 -> banner",                         sc_unprovisioned },
    { "dut-banner",     "KVM handover OSD: DUT is taking the panel",     sc_dut_banner },
    { "apps-nanosoc",   "Apps page: nanosoc (M0) services + commands",   sc_apps_nanosoc },
    { "apps-multicore", "Apps page: multicore (all 9 services)",         sc_apps_multicore },
    { "apps-eth_ss",    "Apps page: eth_ss (shell services only)",       sc_apps_eth_ss },
    { "apps-greybox",   "Apps page: greybox (shell services only)",      sc_apps_greybox },
    { "aligned-status",    "ALIGNED (Linux): HM words, colours, hm row, lease",  sc_al_status },
    { "aligned-multicore", "ALIGNED: longest name + DUT IP row",                 sc_al_multicore },
    { "aligned-apps",      "ALIGNED: apps page, chrome footer, ssh, no SWD",     sc_al_apps },
    { "aligned-program",   "ALIGNED: R5 mid-programming, 42 % + the bar",        sc_al_program },
    { "aligned-identify",  "ALIGNED: locate banner (busy)",                      sc_al_identify },
    { "aligned-request",   "ALIGNED: lease-request overlay (held)",              sc_al_request },
    { "aligned-link-down", "ALIGNED: fault banner outranks the hm row",          sc_al_link_down },
    { "aligned-dut",       "ALIGNED: the KVM OSD in the held colour",            sc_al_dut },
};
#define NSCEN ((int)(sizeof(SCENARIOS)/sizeof(SCENARIOS[0])))

static void render(char grid[CLCD_NCELLS], uint8_t inv[CLCD_ROWS])
{
    /* Two renders: reformat() diffs against the previous frame, so the first
     * call from a fresh state settles the shadow; the second is steady-state. */
    clcd_test_render(grid, inv);
    clcd_test_render(grid, inv);
}

/* --only NAME[,NAME...]: NULL = every scenario. */
static const char *s_only;
static int selected(const char *name)
{
    if (!s_only) return 1;
    size_t n = strlen(name);
    for (const char *p = s_only; *p; ) {
        const char *e = strchr(p, ',');
        size_t len = e ? (size_t)(e - p) : strlen(p);
        if (len == n && strncmp(p, name, n) == 0) return 1;
        if (!e) break;
        p = e + 1;
    }
    return 0;
}

/* A cell for the human view: printable ASCII as itself, a status glyph as a
 * one-column UTF-8 stand-in (see the header), anything else a space. */
static void put_cell(char ch)
{
    static const char *const G[7] = { "✓", "✕", "!", "#", "@", "?", "•" };
    unsigned char u = (unsigned char)ch;
    if (u >= CLCD_GLYPH_FIRST && u <= CLCD_GLYPH_LAST) fputs(G[u - CLCD_GLYPH_FIRST], stdout);
    else putchar((u >= 0x20 && u < 0x7F) ? ch : ' ');
}

static void print_human(int with_roles)
{
    for (int s = 0; s < NSCEN; s++) {
        char grid[CLCD_NCELLS]; uint8_t inv[CLCD_ROWS];
        if (!selected(SCENARIOS[s].name)) continue;
        SCENARIOS[s].seed(); render(grid, inv);
        char codes[CLCD_NCELLS + 1];
        (void)clcd_frame_rows(0, CLCD_ROWS, 0, codes);
        printf("\n== %-14s %s\n", SCENARIOS[s].name, SCENARIOS[s].note);
        printf("    +%.*s+\n", (int)CLCD_COLS, "----------------------------------------");
        for (unsigned r = 0; r < CLCD_ROWS; r++) {
            printf("%s%02u |", inv[r] ? "INV" : "   ", r);
            for (unsigned c = 0; c < CLCD_COLS; c++) put_cell(grid[r * CLCD_COLS + c]);
            printf("|%s", inv[r] ? "  <<" : "");
            if (with_roles) printf("%s %.40s", inv[r] ? "" : "    ", codes + r * CLCD_COLS);
            putchar('\n');
        }
        printf("    +%.*s+\n", (int)CLCD_COLS, "----------------------------------------");
    }
    if (with_roles) {
        printf("\nrole codes ('a' + enum clcd_role, clcd_palette.h):");
        for (unsigned r = 0; r < CLCD_ROLE_COUNT; r++)
            printf("%s %c=%s", (r % 7u) ? "" : "\n ", CLCD_ROLE_CODE(r), clcd_role_name(r));
        printf("\n");
    }
}

static void print_json(void)
{
    int first = 1;
    printf("[\n");
    for (int s = 0; s < NSCEN; s++) {
        char grid[CLCD_NCELLS]; uint8_t inv[CLCD_ROWS];
        if (!selected(SCENARIOS[s].name)) continue;
        SCENARIOS[s].seed(); render(grid, inv);
        char codes[CLCD_NCELLS + 1];
        (void)clcd_frame_rows(0, CLCD_ROWS, 0, codes);
        clcd_panel_state_t ps;
        clcd_panel_state(&ps);
        printf("%s  {\"name\":\"%s\",\"note\":\"%s\",\"theme\":\"%s\",\"rows\":[",
               first ? "" : ",\n", SCENARIOS[s].name, SCENARIOS[s].note, ps.theme);
        first = 0;
        for (unsigned r = 0; r < CLCD_ROWS; r++) {
            /* clcd_fmt_row_json: '"' and '\\' escaped (the heartbeat spinner is a
             * backslash one frame in four, which once made this document
             * unparseable), a status glyph as \u0080..\u0086. */
            char line[CLCD_COLS * 6u + 1u];
            (void)clcd_fmt_row_json(line, sizeof(line), grid + r * CLCD_COLS, CLCD_COLS);
            printf("%s{\"t\":\"%s\",\"inv\":%s,\"roles\":\"%.40s\"}", r ? "," : "", line,
                   inv[r] ? "true" : "false", codes + r * CLCD_COLS);
        }
        printf("]}");
    }
    printf("\n]\n");
}

/* === --png: the real render loop over mock registers into a GRAM model ====== */
static uint16_t s_gram[240][320];
static struct { uint8_t idx, hi, reg[16]; int ramwr, have_hi; unsigned x, y; } G;
static unsigned gx0(void) { return ((unsigned)G.reg[2] << 8) | G.reg[3]; }
static unsigned gx1(void) { return ((unsigned)G.reg[4] << 8) | G.reg[5]; }
static unsigned gy0(void) { return ((unsigned)G.reg[6] << 8) | G.reg[7]; }
static unsigned gy1(void) { return ((unsigned)G.reg[8] << 8) | G.reg[9]; }
static void gram_byte(int rs, uint8_t v)
{
    if (!rs) {
        G.idx = v; G.ramwr = (v == 0x22u); G.have_hi = 0;
        if (G.ramwr) { G.x = gx0(); G.y = gy0(); }
        return;
    }
    if (G.ramwr) {
        if (!G.have_hi) { G.hi = v; G.have_hi = 1; return; }
        G.have_hi = 0;
        if (G.x < 320u && G.y < 240u) s_gram[G.y][G.x] = (uint16_t)((G.hi << 8) | v);
        if (++G.x > gx1()) { G.x = gx0(); if (++G.y > gy1()) G.y = gy0(); }
        return;
    }
    if (G.idx >= 0x02u && G.idx <= 0x09u) G.reg[G.idx] = v;
}
static int gram_hook(void *c, int wr, uint32_t b, uint32_t off, uint32_t *v)
{
    (void)c; (void)b;
    if (!wr) { *v = (off == CLCD_STATUS) ? CLCD_STATUS_FIFO_EMPTY : 0u; return 1; }
    if (off == CLCD_CMD || off == CLCD_DATA) gram_byte(off == CLCD_DATA, (uint8_t)*v);
    return 1;
}

static uint32_t crc_tab[256];
static uint32_t crc32_png(uint32_t c, const uint8_t *p, size_t n)
{
    if (!crc_tab[1])
        for (uint32_t i = 0; i < 256u; i++) {
            uint32_t k = i;
            for (int j = 0; j < 8; j++) k = (k >> 1) ^ (0xEDB88320u & (0u - (k & 1u)));
            crc_tab[i] = k;
        }
    c = ~c;
    while (n--) c = crc_tab[(c ^ *p++) & 0xFFu] ^ (c >> 8);
    return ~c;
}
static void be32(uint8_t *p, uint32_t v) { p[0] = (uint8_t)(v >> 24); p[1] = (uint8_t)(v >> 16); p[2] = (uint8_t)(v >> 8); p[3] = (uint8_t)v; }
static void chunk(FILE *f, const char *tag, const uint8_t *d, uint32_t n)
{
    uint8_t h[8];
    be32(h, n); memcpy(h + 4, tag, 4);
    fwrite(h, 1, 8, f);
    if (n) fwrite(d, 1, n, f);
    uint32_t c = crc32_png(0u, (const uint8_t *)tag, 4);
    c = crc32_png(c, d, n);
    be32(h, c);
    fwrite(h, 1, 4, f);
}

/* A stdlib-only PNG: 8-bit RGB, one IDAT holding a zlib stream of STORED deflate
 * blocks (no compression library needed; 320x240 is ~230 KB). */
static int write_png(const char *path, unsigned scale)
{
    unsigned w = 320u * scale, h = 240u * scale;
    size_t raw_n = (size_t)h * (1u + 3u * w);
    uint8_t *raw = malloc(raw_n);
    size_t nblk = (raw_n + 65534u) / 65535u;
    uint8_t *z = malloc(2u + raw_n + 5u * nblk + 4u);
    if (!raw || !z) { free(raw); free(z); return -1; }
    size_t k = 0;
    for (unsigned y = 0; y < h; y++) {
        raw[k++] = 0;                                  /* filter: none */
        for (unsigned x = 0; x < w; x++) {
            uint16_t v = s_gram[y / scale][x / scale];
            unsigned r5 = (v >> 11) & 31u, g6 = (v >> 5) & 63u, b5 = v & 31u;
            raw[k++] = (uint8_t)((r5 << 3) | (r5 >> 2));   /* bit replication, */
            raw[k++] = (uint8_t)((g6 << 2) | (g6 >> 4));   /* as HM's rgb565_hex */
            raw[k++] = (uint8_t)((b5 << 3) | (b5 >> 2));
        }
    }
    size_t zn = 0;
    z[zn++] = 0x78; z[zn++] = 0x01;
    uint32_t a = 1, b = 0;
    for (size_t off = 0; off < raw_n; ) {
        size_t len = raw_n - off > 65535u ? 65535u : raw_n - off;
        z[zn++] = (uint8_t)(off + len == raw_n);
        z[zn++] = (uint8_t)len; z[zn++] = (uint8_t)(len >> 8);
        z[zn++] = (uint8_t)~len; z[zn++] = (uint8_t)(~len >> 8);
        memcpy(z + zn, raw + off, len);
        for (size_t i = 0; i < len; i++) { a = (a + raw[off + i]) % 65521u; b = (b + a) % 65521u; }
        zn += len; off += len;
    }
    be32(z + zn, (b << 16) | a); zn += 4;
    FILE *f = fopen(path, "wb");
    if (!f) { free(raw); free(z); return -1; }
    static const uint8_t sig[8] = { 0x89, 'P', 'N', 'G', '\r', '\n', 0x1A, '\n' };
    uint8_t ihdr[13];
    be32(ihdr, w); be32(ihdr + 4, h);
    ihdr[8] = 8; ihdr[9] = 2; ihdr[10] = 0; ihdr[11] = 0; ihdr[12] = 0;
    fwrite(sig, 1, 8, f);
    chunk(f, "IHDR", ihdr, 13);
    chunk(f, "IDAT", z, (uint32_t)zn);
    chunk(f, "IEND", 0, 0);
    fclose(f);
    free(raw); free(z);
    return 0;
}

static int print_png(const char *dir)
{
    int rc = 0;
    for (int s = 0; s < NSCEN; s++) {
        if (!selected(SCENARIOS[s].name)) continue;
        clcd_init();                               /* software state only: no MMIO */
        SCENARIOS[s].seed();                       /* resets the mock registers    */
        mock_regs_set_hook(MPS3_CLCD_BASE, gram_hook, 0);
        memset(s_gram, 0, sizeof(s_gram));
        memset(&G, 0, sizeof(G));
        int quiet = 0, drawn = 0;
        for (int i = 0; i < 400000 && !drawn; i++) {
            clcd_poll();
            mock_time_advance_ms(1);
            quiet = (clcd_test_state() == CLCD_ST_IDLE && clcd_test_dirty_count() == 0u) ? quiet + 1 : 0;
            drawn = quiet >= 3;
        }
        char path[512];
        snprintf(path, sizeof(path), "%s/%s.png", dir, SCENARIOS[s].name);
        rc |= !drawn || write_png(path, 1u) != 0;
        snprintf(path, sizeof(path), "%s/%s@2x.png", dir, SCENARIOS[s].name);
        rc |= write_png(path, 2u) != 0;
        printf("%s %s\n", drawn ? "wrote" : "FAILED", path);
    }
    return rc;
}

/* Row 4 only, once per state string the store can emit (HANDOVER_USD_OVERLAY_
 * STORE.md section 1 / net-protocol.md "usd"). The layout check for the field:
 * every text must show IN FULL -- put_at() clips silently at col 40. */
static void print_usd(void)
{
    static const char *const STATES[] = {
        "none", "no hw", "init", "unsupported", "ERR 30", "foreign", "empty",
        "led [A]", "nanosoc_mult [B]", "stale key", "bad", "skipped",
    };
    printf("    +%.*s+\n", (int)CLCD_COLS, "----------------------------------------");
    for (unsigned i = 0; i < sizeof(STATES) / sizeof(STATES[0]); i++) {
        char grid[CLCD_NCELLS]; uint8_t inv[CLCD_ROWS];
        char line[CLCD_COLS + 1];
        base_healthy();
        s_usd_text = STATES[i];
        render(grid, inv);
        for (unsigned c = 0; c < CLCD_COLS; c++) {
            char ch = grid[CLCD_USD_ROW * CLCD_COLS + c];
            line[c] = (ch >= 0x20 && ch < 0x7F) ? ch : ' ';
        }
        line[CLCD_COLS] = '\0';
        printf("   %02u |%s|  %s\n", (unsigned)CLCD_USD_ROW, line,
               (inv[10] || inv[11] || inv[12]) ? "BANNER (wrong!)" : "");
    }
    printf("    +%.*s+\n", (int)CLCD_COLS, "----------------------------------------");
}

int main(int argc, char **argv)
{
    const char *mode = "", *png_dir = 0;
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--only") == 0 && i + 1 < argc)      s_only = argv[++i];
        else if (strcmp(argv[i], "--png") == 0 && i + 1 < argc)  { mode = "--png"; png_dir = argv[++i]; }
        else                                                     mode = argv[i];
    }
    if (strcmp(mode, "--json") == 0)       print_json();
    else if (strcmp(mode, "--usd") == 0)   print_usd();
    else if (strcmp(mode, "--roles") == 0) print_human(1);
    else if (strcmp(mode, "--png") == 0)   return print_png(png_dir);
    else                                   print_human(0);
    return 0;
}
