/*
 * clcd.h -- cooperative firmware driver + software text renderer for the MPS3
 * on-board QVGA (320x240) HX8347-D colour LCD, driven through the shell's
 * AXI4-Lite->8080 byte-streaming bus master (CLCD block @ MPS3_CLCD_BASE,
 * docs/contracts/shell-regmap.md v0.4 "CLCD").
 *
 * See docs/CLCD_STATUS_DISPLAY_PLAN.md (esp. sections 4/7/8/9) and
 * fpga/shell/ip/clcd/README.md (the FROZEN port + register contract, and the
 * init-table seam). This file holds NO panel register values: the HX8347-D
 * init/GRAM sequence is a ported firmware data table consumed through the
 * hx8347_init.h seam (a sibling agent owns firmware/clcd/hx8347_init.{h,c});
 * the driver streams it and never inspects the values.
 *
 * ======================= THE TWO BUILD GATES (read this) ====================
 * MPS3_HAS_CLCD -- the CLCD block @ 0x44AC. The slave EXISTS on the shipped
 *   shell (shell_bd.tcl NUM_MI=15, clcd_0 is master index 14, b4afe3e) and the
 *   panel is LIT and rendering this screen on the board (docs/CLCD_PANEL_FACTS
 *   .md, 2026-07-14). But the DRIVER is still a build-time choice: `CLCD ?=` is
 *   empty by default (firmware/platform/Makefile), so a plain `make elf` links
 *   no CLCD at all. The working board runs `make CLCD=1`. Every
 *   clcd_init()/clcd_poll() call site stays behind #ifdef MPS3_HAS_CLCD.
 *   (This note used to say "there is NO AXI slave at 0x44AC ... NUM_MI=14".
 *   That was true when it was written and is false now -- b4afe3e landed the
 *   slave and the board proved it.)
 *
 * MPS3_HAS_CLCD_KVM -- the KVM @ 0x44AD. STILL RESERVED: there is NO slave at
 *   that page today (assign_bd_address stops at 0x44AC), so an access neither
 *   traps nor works. It lands in the Wave-4 static rebuild, which re-mints
 *   static_id and re-keys all 8 overlays. This driver calls into
 *   firmware/clcd_kvm/ UNCONDITIONALLY -- with MPS3_HAS_CLCD_KVM undefined every
 *   one of those calls is an inline no-op that touches no register and folds
 *   away, so a CLCD=1 build for TODAY's bitstream is behaviourally unchanged.
 *   Do NOT define MPS3_HAS_CLCD_KVM until the KVM-bearing bitstream is on the
 *   board: the next reflash would poke a DECERR'ing void every superloop pass,
 *   on a board whose only ingress is that same firmware.
 * ===========================================================================
 *
 * Cooperative, NEVER-BLOCKING: clcd_poll() runs in the same superloop as the
 * lwIP TCP/ARP timers and swap_fsm_poll(). It does one bounded step per call
 * (all waits are timed poll-and-return deadlines off mps3_sys_now_ms(); pushes
 * are FIFO-backpressured and capped at CLCD_BYTES_PER_PASS bytes), exactly the
 * chunking discipline swap_fsm uses. A busy-wait here would drop the network,
 * the board's only ingress.
 */
#ifndef MPS3_CLCD_H
#define MPS3_CLCD_H

#include <stdint.h>

/* The front panel's colour roles (enum clcd_role, CLCD_ROLE_COUNT, CLCD_PALETTE_INIT):
 * VENDORED unchanged from Harness Manager (tools/gen_tokens.py, design/tokens.json;
 * HM commit 0ba5000) -- see firmware/clcd/HM_VENDORED.md. */
#include "clcd_palette.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ---- text geometry: 320x240 panel / 8x16 font => 40 cols x 15 rows -------- */
#define CLCD_COLS     40u
#define CLCD_ROWS     15u
#define CLCD_NCELLS   (CLCD_COLS * CLCD_ROWS)   /* 600 */
#define CLCD_GLYPH_W  8u
#define CLCD_GLYPH_H  16u

/* Per-clcd_poll() byte budget: the hard bound on bytes pushed into the CLCD
 * FIFO in a single pass (command + data + pixel bytes all counted). Tuned well
 * below lwIP's timer slack so a full-screen redraw spreads across many passes
 * instead of stalling the superloop. Overridable at build time. */
#ifndef CLCD_BYTES_PER_PASS
#define CLCD_BYTES_PER_PASS 256u
#endif

/* Reformat/diff cadence (ms). The panel ceiling is 20fps (50 ms); 250 ms is
 * comfortable and, in steady state, only the uptime-seconds + heartbeat cells
 * change, so a handful of dirty cells per tick. Overridable for tests. */
#ifndef CLCD_REFRESH_MS
#define CLCD_REFRESH_MS 250u
#endif

/* Touch sampling period (TOUCH=1 builds). clcd_poll() used to call touch_poll()
 * on EVERY superloop pass, and a pass is ~0.7 ms: a held finger meant ~1400
 * STMPE811 samples a second, each several I2C transactions, and on silicon
 * (2026-09-22) a held touch drove clcd to 65-150 ms per pass and starved the
 * network services. A UI needs nowhere near that: at 10 ms the two-sample
 * debounce still lands a tap within ~20 ms, and the I2C cost falls ~14x. */
#ifndef CLCD_TOUCH_PERIOD_MS
#define CLCD_TOUCH_PERIOD_MS 10u
#endif

/* Board identity shown top-left on row 0 (plan section 9). This is the
 * DEFAULT, seeded in clcd_init(); clcd_set_board_name() still overrides it at
 * runtime and remains the seam for a future host-pushed name.
 *
 * "MPS3-01" is this board's real identity -- the name its board hub uses
 * for it. It supersedes the earlier "MPS3-<last IP octet>" derivation,
 * which rendered "MPS3-101" from MPS3_DEFAULT_IP_D=101: a name nothing else in
 * the fleet calls this board, and no less compile-time than this string (the IP
 * is a build constant too, net_proto.h:35). It also matches the plan's own
 * section-8 mock-up, which shows "MPS3-01". Override per board with
 * -DMPS3_BOARD_NAME='"MPS3-02"' (<= 23 chars, the s_board_name slot). */
#ifndef MPS3_BOARD_NAME
#define MPS3_BOARD_NAME "MPS3-01"
#endif

/* ------------------------------------------------------------------------
 * Public driver API -- the two superloop hooks (main.c, behind MPS3_HAS_CLCD).
 * ------------------------------------------------------------------------ */

/* Software init: seed the render state, derive the board-name fallback, and
 * arm the reset->init state machine. Does no blocking work and returns at once;
 * the panel reset pulse, init-table stream and settle are all driven by the
 * subsequent clcd_poll() passes. Call once, after the network is up. */
void clcd_init(void);

/* One bounded, non-blocking step of the reset/init/render state machine. Call
 * once per superloop iteration. Pushes at most CLCD_BYTES_PER_PASS bytes and
 * only while the FIFO is not full, then returns; it never spins and never
 * blocks on a timed wait. */
void clcd_poll(void);

/* THE DRAIN (mps3-harnessd; bare metal never calls it). clcd_poll() repeated
 * while there is streamable work -- the last call spent its whole
 * CLCD_BYTES_PER_PASS, or it just started a render -- until `budget_us` has
 * passed on mps3_sys_now_us(). Always calls clcd_poll() at least once. A timed
 * wait, a FIFO-full or an idle panel stops it at once, so it never spins.
 * The overshoot past `budget_us` is at most one clcd_poll(). Returns the bytes
 * pushed.
 *
 * WHY: CLCD_BYTES_PER_PASS (256) is one glyph -- bare metal's pacing for a
 * ~0.7 ms superloop. A harnessd pass carries ~50 syscalls on top (rv32 has no
 * vDSO, so every mps3_sys_now_us() is one), and a full page is ~640 calls:
 * one glyph per pass is what the glass showed. */
uint32_t clcd_poll_drain(uint32_t budget_us);

/* THE BUS SEAM (lane CLCD-SPEED). Push up to `n` {rs,byte} pairs, in order, into
 * the CLCD FIFO (rs[i] 0 = CLCD_CMD, 1 = CLCD_DATA), NEVER writing into a full
 * FIFO; returns how many were pushed (< n only when the FIFO filled). The glyph
 * renderer hands it each contiguous run of a cell's stream.
 *
 * clcd.c's WEAK default is the pre-seam loop exactly -- per byte: read
 * CLCD_STATUS, stop on fifo_full, write -- so a bare-metal image makes the same
 * accesses in the same order as before. mps3-harnessd supplies the strong one
 * (hal_front.c): ONE STATUS read buys 128 - STATUS.level writes (only harnessd
 * fills the FIFO, and it only drains in between, so that can never overflow
 * it), the bytes go straight to the UIO window, and the LCD mirror's tap sees
 * every one of them, in order, right after. Under Linux the per-byte STATUS
 * read, the register-HAL call chain and the per-byte tap were ~5 us a byte on
 * the 100 MHz MBV (write-through D-cache: every stack spill is a DDR write) --
 * the character-by-character repaint david saw on rc2_v6 (2026-09-28). */
uint32_t clcd_bus_push(const uint8_t *rs, const uint8_t *val, uint32_t n);

/* Board-name hook. v1 shows the deterministic MPS3_BOARD_NAME default (above),
 * seeded in clcd_init(). This setter is the clearly-marked seam for a future
 * HOST-PUSHED name: a coordinator verb could call it once such a verb exists,
 * and it overrides the build-time default at any time. It is deliberately NOT
 * wired to any control verb here -- doing so would change
 * docs/contracts/net-protocol.md and coordinator.c, both outside this module's
 * scope (plan section 9). */
void clcd_set_board_name(const char *name);

/* ------------------------------------------------------------------------
 * KVM handover (fpga/shell/ip/clcd_kvm/README.md). The harness no longer owns
 * the panel unconditionally: press USER_nPB[1] and the DUT drives it instead.
 * clcd_poll() drives these from the KVM's STATUS/EVENT every pass (no-op when
 * MPS3_HAS_CLCD_KVM is not built); they are also called directly by the tests.
 * ------------------------------------------------------------------------ */

/* About to lose the panel: paint the "DUT has the display -- press PB1 to
 * return" OSD as the last thing on the glass. Idempotent. */
void clcd_lose(void);

/* Just regained the panel, which the KVM HARD-RESET during the handover:
 * re-run the init table and repaint EVERY cell (not just the dirty ones -- the
 * panel's GRAM is gone). This is the production full-repaint path. */
void clcd_regain(void);

/* ------------------------------------------------------------------------
 * MULTI-PAGE NAVIGATION (docs/planning/CLCD_APPS_PORTS_PAGE_PLAN.md, Phase 1).
 *
 * The display is no longer a single status screen: reformat() branches on the
 * current page exactly as it already branches on the KVM OSD banner. Page cycling
 * is driven by USER_nPB[1] short-press (KVM builds only; see clcd.c pb_service()),
 * and -- once the touchscreen lands (Phase 2) -- by tapping the on-screen buttons
 * whose hit-boxes are declared below. The banner still overrides EVERY page.
 *
 * PB1 held at power-up is the D13 "skip the card's default load" escape hatch,
 * not a gesture: pb_service() ignores a press that is already down when it first
 * runs and acts only on edges after the first release, so that hold never
 * changes the page or hands the panel to the DUT (test_clcd_kvm.c).
 * ------------------------------------------------------------------------ */
typedef enum {
    CLCD_PAGE_STATUS = 0,   /* the §8 status screen (default)                */
    CLCD_PAGE_APPS,         /* "Applications & Ports" -- services + commands */
    CLCD_PAGE__COUNT,
} clcd_page_t;

/* Advance to the next page (wraps), and force a repaint. */
void clcd_page_next(void);
/* Set / query the current page. */
void        clcd_page_set(clcd_page_t page);
clcd_page_t clcd_page_get(void);

/* ------------------------------------------------------------------------
 * Services the CURRENTLY-LOADED RM exposes -- what the Apps page lists, keyed on
 * the DESIGN half (CLCD_RM_DESIGN) like clcd_rm_name()/clcd_rm_caps(). The
 * shell-side services (control/TFTP/push/XVC) are always present; the per-DUT
 * ones (console UARTs, SWD/JTAG debug) depend on the RM's makeup. Ports are the
 * net_proto.h constants (single source of truth). Pure; unit-tested.
 * ------------------------------------------------------------------------ */
enum {
    CLCD_SVC_CTRL  = 1u << 0,   /* TCP 6900 control/status  */
    CLCD_SVC_TFTP  = 1u << 1,   /* UDP 69   TFTP push       */
    CLCD_SVC_PUSH  = 1u << 2,   /* TCP 6910 raw partial push*/
    CLCD_SVC_XVC   = 1u << 3,   /* TCP 2542 XVC debug bridge*/
    CLCD_SVC_SWD   = 1u << 4,   /* TCP 6920 OpenOCD SWD     */
    CLCD_SVC_JTAG  = 1u << 5,   /* TCP 6921 OpenOCD JTAG    */
    CLCD_SVC_UART0 = 1u << 6,   /* TCP 6930 DUT UART0       */
    CLCD_SVC_UART1 = 1u << 7,   /* TCP 6931 DUT UART1       */
    CLCD_SVC_SWO   = 1u << 8,   /* TCP 6932 SWO/ITM trace   */
};
uint32_t clcd_rm_services(uint32_t rm_id);

/* ------------------------------------------------------------------------
 * On-screen button hit-boxes. Cells, not pixels: {x,y} top-left cell, {w,h}
 * span. clcd_hittest() converts a raw touch pixel (touch_poll(), once per
 * contact) to a cell and returns the action of the box it lands in
 * (CLCD_ACT_NONE if none). It is PAGE-AWARE (HM CLCD_ALIGNMENT R2): each page
 * has its own table, a banner's target exists only while the banner shows, and
 * every hit writes one entry to the event ring -- see "PAGE-AWARE HIT TEST + THE
 * EVENT RING" below for the full rule. `arg` of a box is its event's CLCD_ON_*.
 * ------------------------------------------------------------------------ */
typedef enum {
    CLCD_ACT_NONE = 0,
    CLCD_ACT_NEXT_PAGE,     /* cycle to the next page          */
    CLCD_ACT_GIVE_DUT,      /* hand the panel to the DUT (KVM) */
    CLCD_ACT_SELECT_SVC,    /* a service row (arg = svc index) */
} clcd_action_t;

typedef struct {
    uint8_t x, y, w, h;     /* cell rectangle */
    uint8_t action;         /* clcd_action_t  */
    uint8_t arg;            /* action-specific */
} clcd_hitbox_t;

/* Map a raw touch pixel to the action of the on-screen button it hits on the
 * current page (or CLCD_ACT_BANNER, below); CLCD_ACT_NONE if it misses every
 * box. A hit pushes one event (O(1), no I/O). */
int clcd_hittest(unsigned x_px, unsigned y_px);

/* ------------------------------------------------------------------------
 * Pure formatters -- no MMIO except where noted; unit-tested directly.
 * ------------------------------------------------------------------------ */

/* uptime -> "ddd:hh:mm:ss" (12 chars + NUL). Wrap-safe: `ms` is the raw 32-bit
 * millisecond counter (wraps ~49.7 days); days can reach 049. `out` must hold
 * >= 13 bytes. */
void clcd_fmt_uptime(char *out, uint32_t ms);

/* v -> "0x" + 8 uppercase hex digits (10 chars + NUL). `out` >= 11 bytes. */
void clcd_fmt_hex32(char *out, uint32_t v);

/* ------------------------------------------------------------------------
 * rm_id v2 encoding (docs/VERSIONING_PLAN.md §3.2) -- { major[31:24],
 * minor[23:16], design_id[15:0] }. The 32-bit rm_id bus across the partition
 * boundary was always wide enough; v2 spends its 16 idle high bits on a design
 * version, at zero boundary cost and with NO static re-key.
 *
 * EVERY rm_id -> meaning lookup MUST key on the DESIGN half only. The version
 * bits change on every RM release; a lookup that keyed on the whole 32-bit word
 * would stop recognising a design the moment it was re-versioned, and the glass
 * would silently fall back to "rm?<hex>". Mask first, always.
 *
 * Consequences worth knowing, both deliberate:
 *   - greybox stays exactly 0x00000000 (design 0, v0.0) -- an inert tie-off is
 *     not a versioned design, and this preserves the DFX decoupler's
 *     DECOUPLED_VALUE 0x0 and the firmware's "0 = greybox" convention.
 *   - A PRE-v2 partial renders as v0.0 (its high half is zero). That is honest:
 *     v0.0 means "carries no version", and it is visibly distinct from v1.0.
 *     The one exception is the old uart_echo id 0x4543484F (ASCII "ECHO", which
 *     used all 32 bits): its design half is 0x484F, not the new 0x0004, so a
 *     stale uart_echo partial renders as its raw id rather than by name. That is
 *     correct -- under v2 it IS an unrecognised design -- and it is the exact
 *     cosmetic-only symptom VERSIONING_PLAN.md §6 step 4 predicts.
 * ------------------------------------------------------------------------ */
#define CLCD_RM_DESIGN(rm_id)     ((uint32_t)(rm_id) & 0xFFFFu)
#define CLCD_RM_VER_MAJOR(rm_id)  (((uint32_t)(rm_id) >> 24) & 0xFFu)
#define CLCD_RM_VER_MINOR(rm_id)  (((uint32_t)(rm_id) >> 16) & 0xFFu)

/* ------------------------------------------------------------------------
 * THE RESIDENT RM IS READ LIVE FROM HARDWARE, NEVER FROM A FIRMWARE CACHE.
 *
 * The glass used to render g_shell_state.current_rm_id -- "the last id this
 * firmware VERIFIED". That cache is correct and deliberate for the control
 * path (coordinator.c's `ping` reports it on purpose, and swap_fsm sets it),
 * but it only knows about reconfigurations THIS FIRMWARE PERFORMED. A JTAG- or
 * ICAP-driven load bypasses the firmware entirely, so the cache goes stale and
 * the panel confidently names a design that is not in the RP. Observed on
 * silicon: DFXCTL.RM_ID read 0x01000003 (nanosoc_multicore v1.0) while the LCD
 * still said "DUT : greybox   v0.0".
 *
 * So row 2 (name+version) and row 9 (the makeup line) both come from
 * DFXCTL.RM_ID @ 0x44A1_0010 -- the RP's rm_id partition pin, the same ground
 * truth swap_fsm.c's step_verify() compares against -- qualified by
 * DFXCTL.RM_STATUS.rm_id_valid.
 *
 * rm_id_valid IS A GATE + STABILITY DETECTOR, NOT A ZERO-CHECK (dfx_ctl.sv:
 * `rm_id_valid = rm_id_gate & (stable_cnt >= RM_ID_SETTLE_CYCLES)`, with
 * `rm_id_gate = ~decouple_en & ~decoupled & ~rp_in_reset`). "0 == greybox" is
 * only a SOFTWARE convention -- the hardware clamps rm_id to the decoupler's
 * DECOUPLED_VALUE (0x0) whenever the RP is isolated, which is exactly why
 * dfx_ctl gates the valid bit rather than letting the settle detector latch a
 * clamped 0 as a real id. Treating a clamped 0 as "greybox" would resurrect the
 * same lie in a new place, so:
 *
 *   rm_id_valid set   -> trust and display the CSR word.
 *   rm_id_valid clear -> display an explicit TRANSIENT/UNKNOWN state
 *                        (clcd_rm_src_text), never a name, never a version.
 * ------------------------------------------------------------------------ */
typedef enum {
    CLCD_RM_LIVE = 0,     /* rm_id_valid: the CSR word IS the resident RM     */
    CLCD_RM_DECOUPLED,    /* RP isolated -- the id is the decoupler's clamp   */
    CLCD_RM_RP_RESET,     /* RP held in reset -- the id is not meaningful     */
    CLCD_RM_SETTLING,     /* coupled+released but rm_id_valid is not (yet) set:
                           * mid-reconfiguration, an id still settling, or a
                           * shell whose DFXCTL never asserts it. We do not know. */
} clcd_rm_src_t;

/* Pure classifier over the two DFXCTL status words (DFXCTL_STATUS @0x08 and
 * DFXCTL_RM_STATUS @0x14). Isolation is checked BEFORE the valid bit on
 * purpose: dfx_ctl.sv already makes `decoupled && rm_id_valid` impossible, but
 * if a shell ever did report that pair, degrading to "RP DECOUPLED" (an honest
 * "don't know") beats rendering the clamped 0x0 as "greybox" (a confident lie).
 * Unit-tested. */
clcd_rm_src_t clcd_rm_classify(uint32_t dfx_status, uint32_t dfx_rm_status);

/* The row-2 name-field text for a NON-live state; "" for CLCD_RM_LIVE (which
 * must render a real name instead). Always <= CLCD_RM_NAME_MAX chars. */
const char *clcd_rm_src_text(clcd_rm_src_t src);

/* The row-9 makeup-line text when the resident RM is not knowable. Fits the
 * 40-col row after "CFG : " (see clcd_rm_caps). */
#define CLCD_RM_CAPS_UNKNOWN "unknown -- no valid rm_id from RP"

/* LIVE read of the resident RM (4 single-beat AXI4-Lite reads, tens of cycles;
 * safe in the cooperative superloop -- it neither blocks nor polls).
 *
 * Reads RM_ID, then the qualifiers, then RM_ID AGAIN, and demotes a
 * would-be-LIVE result to CLCD_RM_SETTLING if the id moved between the two --
 * so the qualifier is sampled strictly INSIDE the data's stable window and a
 * reconfiguration racing the read can never be rendered as a settled id. (The
 * next 250 ms refresh picks up the new one.)
 *
 * *rm_id_out is always written with the CSR word as read -- it is ONLY
 * meaningful when the return is CLCD_RM_LIVE. Callers must not name, version or
 * otherwise interpret it in any other state. */
clcd_rm_src_t clcd_rm_live(uint32_t *rm_id_out);

/* ------------------------------------------------------------------------
 * Row-2 field geometry -- "DUT : <name>   v<maj>.<min>".
 *
 * These exist because row 2 SILENTLY OVERFLOWED. The name was written at col 6
 * and a second field at col 21, giving the name ~14 columns; clcd_rm_name()
 * already returned "nanosoc_multicore" (17). put_at() clips at the 40-col row
 * edge but does NOT stop one field from overwriting the next, so the name ate
 * its neighbour's first three columns. It had simply never been seen, because
 * the glass had only ever shown "greybox" (7).
 *
 * So the two fields are now declared, not assumed, and test_clcd.c asserts --
 * exhaustively, over every one of the 65536 possible design ids -- that every
 * string clcd_rm_name() can return fits CLCD_RM_NAME_MAX, that every version
 * fits CLCD_RM_VER_MAX, and that the two fields cannot touch. A name added to
 * the table without room for it is now a test failure, not a corrupted screen.
 *
 * The raw "rm_id 0x%08X" hex that used to sit at col 21 is GONE from row 2 --
 * it is what the name field was overflowing into, and there is no arrangement
 * of 40 columns that holds a 17-char name, a 10-char hex id and a version.
 * Name + version is the strictly more useful pair (VERSIONING_PLAN.md §4.1
 * reaches the same conclusion), the raw id is still one `ping` away on 6900,
 * and an UNRECOGNISED id still renders its full 32-bit hex right here in the
 * name field -- so the glass never hides an id it cannot name.
 * ------------------------------------------------------------------------ */
#define CLCD_RM_NAME_COL  6u    /* after "DUT : "                            */
#define CLCD_RM_NAME_MAX 20u    /* cols 6..25 -- fits "nanosoc_multicore" (17)
                                 * and "rm?01000003" (11), with headroom     */
#define CLCD_RM_VER_COL  27u    /* >= NAME_COL + NAME_MAX, so they cannot touch */
#define CLCD_RM_VER_MAX   8u    /* "v255.255" -- cols 27..34, inside 40      */

/* ------------------------------------------------------------------------
 * Row-4 right half -- the USER microSD (D13): "USD : <text>".
 *
 *   col  0         1         2         3
 *        0123456789012345678901234567890123456789
 *        SID : 0xAAF21306  USD : nanosoc_mult [B]
 *
 * "SID : 0x........" owns cols 0..15; the USD label starts at col 18 (two
 * blanks of separation) and its text owns cols 24..39, i.e. EXACTLY to the row
 * edge. put_at() clips silently at col 40, so the text is clipped to
 * CLCD_USD_TEXT_MAX here, by the formatter, and test_clcd_usd.c render-tests
 * every state string against the field.
 *
 * The text is whatever the store says it is -- docs/contracts/net-protocol.md
 * "User microSD (usd)": the `text` field of {"op":"usd"} IS what this row
 * shows after "USD : ". The CLCD never interprets it: no USD state ever raises
 * the fault banner (rows 10-12). A missing or foreign card is not a harness
 * fault (HANDOVER_USD_OVERLAY_STORE.md section 1).
 * ------------------------------------------------------------------------ */
#define CLCD_USD_ROW        4u
#define CLCD_USD_COL       18u  /* "USD : " -- clear of "SID : 0x........" (0..15) */
#define CLCD_USD_TEXT_COL  24u  /* CLCD_USD_COL + strlen("USD : ")               */
#define CLCD_USD_TEXT_MAX  16u  /* cols 24..39 -- the whole rest of the row       */

/* The two provider symbols the status page reads, exported by the overlay
 * store (lane I-FW). clcd.c carries __attribute__((weak)) fallbacks returning
 * "no hw" and 0 -- the same seam as coordinator.c's weak mps3_shell_static_id()
 * -- so a build without the store links and shows "USD : no hw". The store's
 * definitions MUST be strong (not weak) to win the link.
 *
 * overlay_store_usd_change_count() is polled on EVERY idle clcd_poll() pass
 * (~1400/s) to trigger an immediate refresh when the card state moves, so it
 * must be a plain O(1) getter: no MMIO, no SPI, no card I/O. The text is read
 * only when the page reformats (at most every CLCD_REFRESH_MS, or at once on a
 * change-count move); it must be <= CLCD_USD_TEXT_MAX printable ASCII chars and
 * stay valid until the next call. */
const char *overlay_store_usd_text(void);
uint32_t    overlay_store_usd_change_count(void);

/* Provider text -> the row-4 field: copies at most CLCD_USD_TEXT_MAX chars,
 * maps any non-printable byte to '?', and renders a NULL text as "?" (an honest
 * "don't know" -- never a plausible state). `out` must hold
 * >= CLCD_USD_TEXT_MAX + 1 bytes. Pure; unit-tested. */
void clcd_fmt_usd(char *out, const char *text);

/* rm_id -> human RM name, keyed on the DESIGN half (CLCD_RM_DESIGN) so it is
 * immune to re-versioning. An unrecognised design renders as "rm?<8 hex of the
 * FULL rm_id>" -- the glass never lies about an id it does not know, and shows
 * the version bits too, since those are part of what makes it unrecognised.
 * Returns a pointer to a static string, always <= CLCD_RM_NAME_MAX chars
 * (test_clcd.c proves this exhaustively). */
const char *clcd_rm_name(uint32_t rm_id);

/* rm_id -> one-line design makeup (cores / ethernet / serial), sourced from
 * each RM's wrapper header in this repo (see clcd.c). Keyed on the DESIGN half,
 * like clcd_rm_name(). <= 34 chars ("CFG : " + 34 = the full 40-col row). */
const char *clcd_rm_caps(uint32_t rm_id);

/* True iff the resident design carries an ethernet MAC -- eth_ss (0x0002) and
 * nanosoc_multicore (0x0003) only; single-core nanosoc (0x0001) has none. Keyed
 * on the DESIGN half like clcd_rm_name()/_caps(). Gates the status page's DUT-IP
 * row -- only an ethernet RM can source an observed egress IP. Pure. */
int clcd_rm_has_eth(uint32_t rm_id);

/* rm_id -> "v<major>.<minor>" from the HIGH half (VERSIONING_PLAN.md §3.2):
 * "v1.0" .. "v255.255", so <= CLCD_RM_VER_MAX chars. `out` >= 9 bytes.
 *
 * v0.0 is rendered, not suppressed -- it is what the bits say (greybox, and any
 * pre-v2 partial), and blanking it would be the shell editorialising about data
 * it was given. Patch and git-SHA are deliberately absent: they do not fit in
 * 32 bits and live host-side in the manifest (VERSIONING_PLAN.md §3.2). */
void clcd_fmt_rm_version(char *out, uint32_t rm_id);

/* The board's static management IP as a bare dotted quad ("192.168.10.101").
 * Factored out of clcd_fmt_net() so the Apps page can build "<tool> <ip> <port>"
 * command strings from the same source. `out` must hold >= 16 bytes. Pure. */
void clcd_fmt_ip(char *out);

/* THE BOARD-IDENTITY SEAMS (v0.16, lane IDENT). clcd_fmt_ip() (row 5 NET, the
 * apps page) asks mps3_clcd_ident_ip() first and row 14 (MAC) asks
 * mps3_clcd_ident_mac(); each returns 1 when it filled the value. clcd.c's WEAK
 * defaults return 0, so a bare-metal frame is byte-identical (the compile-time
 * MPS3_DEFAULT_IP_* and mps3_platform_mac()). mps3-harnessd returns this boot's
 * RESOLVED identity (identity_linux.c) and sets row 0's name through
 * clcd_set_board_name(). `out` is a dotted quad, NUL-terminated, <= 15 chars. */
int mps3_clcd_ident_ip(char out[16]);
int mps3_clcd_ident_mac(uint8_t mac[6]);

/* THE IDENTIFY BANNER (v0.16, the Harness Manager's `locate`, R3). While an
 * engine runs a locate, mps3_clcd_locate() returns 1 and fills `who` (<= cap-1
 * printable chars; "" = anonymous): the frame then shows rows 10-12 inverted with
 * "IDENTIFY: <who>" centred on row 11 -- on the status page only when no FAULT
 * banner is up (faults outrank it; the DIP and engine rows yield to it), and over
 * the apps page's rows 10-12. The backlight blink is the engine's (it drives
 * clcd_kvm_set_backlight from its clcd service tick), not this file's.
 * mps3_clcd_tap(x, y) is asked FIRST on every tap clcd_hittest() sees: 1 = the
 * engine took it (a tap during a locate is "found it") and it is not also a nav
 * action. WEAK 0 in clcd.c: bare metal draws no banner and its taps are unchanged. */
int mps3_clcd_locate(char *who, unsigned cap);
int mps3_clcd_tap(unsigned x_px, unsigned y_px);

/* Network line "IP  UP 100/FD" / "IP  DOWN" into out (cap bytes). Reads the
 * link state via smsc911x_link_up() and speed/duplex via smsc911x_mii_read()
 * (ANLPAR), so it is exercised against seeded mock registers in the test. */
void clcd_fmt_net(char *out, unsigned cap);

/* The DUT's last-observed egress IPv4 as a dotted quad into out (cap bytes),
 * read LIVE from GENCHK.DUT_STATUS/DUT_IP (MPS3_GENCHK_BASE). Emits "--" until
 * GENCHK_DUT_STATUS.IP_SEEN latches -- a CPU-less eth RM (eth_ss) never sources
 * one. This is the DUT's IP, distinct from clcd_fmt_ip()/_net()'s harness IP. */
void clcd_fmt_dut_ip(char *out, unsigned cap);

/* ------------------------------------------------------------------------
 * THE ENGINE ROW (status page row 12; Linux harness plan §10 "TOFU", L2).
 * ------------------------------------------------------------------------
 * Which engine serves this board, and whether its SSH has been claimed -- the
 * two facts a person at the bench needs during a standalone first install
 * (docs/LINUX_HARNESS.md): "is this the Linux harness?" and "did MY key land?".
 *
 *   "SYS : linux  ssh claimed SHA256:AbCdEfGh"     (exactly 40 columns)
 *   "SYS : linux  ssh unclaimed"
 *
 * The fingerprint prefix is the CLAIMED key's (the first key in the claim),
 * OpenSSH style, so it compares directly with `ssh-keygen -lf ~/.ssh/id_*.pub`.
 *
 * WHICH ROW, AND WHY 12: rows 0-9 are full; D13 owns row 4 cols 18-39 (the
 * user-microSD state); rows 10-12 are the error-banner reservation, and the
 * banner-free row 10 is the DUT's IP. Row 12 is the last free one. It YIELDS to
 * the banner exactly as row 10's DIP does (an inverted 10-12 is the banner's
 * signal alone), and it is not inverted itself.
 *
 * THE SEAM: mps3_clcd_engine() fills the facts. clcd.c's WEAK default returns 0
 * -- the bare-metal image has no OS, no sshd and no claim -- and then row 12 is
 * left exactly as it was before this existed (blank), so a bare-metal frame is
 * byte-identical. mps3-harnessd supplies the strong one (tofu_linux.c). */
#define CLCD_ENGINE_ROW   12u
#define CLCD_ENGINE_IMPL_MAX 6u     /* cols 6..11 */
typedef struct {
    const char *impl;               /* "linux" (the version.impl value)            */
    int         claimed;            /* an authorized_keys claim exists             */
    char        fpr[12];            /* first 8 chars after "SHA256:" ("" = unknown) */
} mps3_clcd_engine_t;
int mps3_clcd_engine(mps3_clcd_engine_t *out);

/* The row-12 text after "SYS : " into out (cap bytes, <= 34 chars + NUL). Pure;
 * unit-tested. impl is clipped to CLCD_ENGINE_IMPL_MAX, fpr to 8. */
void clcd_fmt_engine(char *out, unsigned cap, const mps3_clcd_engine_t *e);

/* Decode an ANLPAR (MII reg 0x05) word into speed/duplex booleans, most-
 * capable advertised mode wins. Pure; unit-tested. */
void clcd_link_speed_duplex(uint16_t anlpar, int *speed100, int *full_duplex);

/* ------------------------------------------------------------------------
 * Shadow-buffer diff (pure). Marks a dirty bit for every cell whose glyph
 * changed between `cur` and `next` (both CLCD_NCELLS flat char arrays) OR whose
 * row-inversion attribute changed, and returns the count of newly-dirtied
 * cells. `dirty` is a CLCD_NCELLS-bit bitmap (ceil(NCELLS/8) bytes); bits are
 * OR-ed in (never cleared) so partial renders accumulate. `cur_inv`/`next_inv`
 * are per-ROW inversion flags (CLCD_ROWS bytes) or NULL to ignore inversion.
 * ------------------------------------------------------------------------ */
#define CLCD_DIRTY_BYTES ((CLCD_NCELLS + 7u) / 8u)   /* 75 */
unsigned clcd_diff_cells(const char *cur, const char *next,
                         const uint8_t *cur_inv, const uint8_t *next_inv,
                         uint8_t *dirty);

/* ------------------------------------------------------------------------
 * THE ALIGNED FRONT PANEL (Harness Manager docs/design/CLCD_ALIGNMENT.md §5.3,
 * §7.2: R4 colour roles + glyphs, R5 programming progress, the clcd.c half of
 * R2 -- seams, page-aware hit test, event ring, frame access). Published to the
 * PANEL-PROTO lane as clcd_seams.h; every provider seam is WEAK 0 here, so the
 * bare-metal frame (no provider) is byte-identical to the one before
 * (firmware/test/test_clcd_roles.c, the golden).
 * ------------------------------------------------------------------------ */
/* ==========================================================================
 * R4 -- STATUS GLYPHS (firmware/clcd/clcd_glyphs.h, vendored unchanged from
 * HM's design/generated/: 8x16 stand-ins for HM's lucide icons). A cell
 * holding one of these bytes draws the glyph; bare metal never emits them.
 * The seven codes below are spelt EXACTLY as clcd_glyphs.h spells them (0x80,
 * no suffix): clcd.c includes both, so a code HM moves is a macro-redefinition
 * error under -Werror, never a silent mismatch. The array stays out of this
 * header (one copy, in clcd.c).
 * Use the _S string forms by ADJACENT-LITERAL concatenation only
 * (CLCD_GS_USER "david", never "\x84david": \x84da would parse as one escape).
 * ========================================================================== */
#define CLCD_GLYPH_FIRST 0x80u
#define CLCD_GLYPH_OK   0x80     /* circle-check  -> tick      */
#define CLCD_GLYPH_ERR  0x81     /* circle-x      -> cross     */
#define CLCD_GLYPH_WARN 0x82     /* triangle-alert            */
#define CLCD_GLYPH_HELD 0x83     /* lock                      */
#define CLCD_GLYPH_USER 0x84     /* user                      */
#define CLCD_GLYPH_UNK  0x85     /* circle-help   -> ?         */
#define CLCD_GLYPH_DOT  0x86     /* a filled dot              */
#define CLCD_GLYPH_LAST  0x86u
#define CLCD_GS_OK   "\x80"
#define CLCD_GS_ERR  "\x81"
#define CLCD_GS_WARN "\x82"
#define CLCD_GS_HELD "\x83"
#define CLCD_GS_USER "\x84"
#define CLCD_GS_UNK  "\x85"
#define CLCD_GS_DOT  "\x86"

/* ==========================================================================
 * R4 -- THE ROLE PLANE + THE PALETTE SEAM
 *
 * Every committed cell carries an enum clcd_role beside its glyph (600 B, the
 * role plane). The renderer draws a cell as pal[role] = {fg, bg} RGB565, through
 * the normal bus path (so the LCD mirror sees it like any other pixel).
 *
 * A theme is a palette plus layout flags. mps3_clcd_palette() is asked on every
 * reformat (<= 4 Hz); a changed pointer repaints the whole panel.
 *   clcd_theme_today    today's pixels EXACTLY: every non-banner role is white on
 *                       black, every CLCD_ROLE_BANNER_* is white on red (0xF800).
 *                       Today's layout and words. The WEAK default.
 *   clcd_theme_aligned  pal = CLCD_PALETTE_INIT (HM tokens), flags =
 *                       CLCD_THEME_ALIGNED: HM's words and layout (decision D3 a:
 *                       design / prog / shell / card / net / up / dut / icap /
 *                       cfg / sys, muted labels, status colours, title bar),
 *                       status glyphs, and the R5 programming progress.
 * With the today theme the role plane is still filled: CLCD_ROLE_TEXT for a
 * plain cell, CLCD_ROLE_BANNER_ERR for a cell on an inverted row (and the
 * overlay's own banner role for its rows), so `panel frame` roles are always
 * meaningful; report clcd_panel_state().theme beside them.
 * ========================================================================== */
#define CLCD_THEME_ALIGNED 0x1u

typedef struct {
    const uint16_t (*pal)[2];   /* [CLCD_ROLE_COUNT][2] = {fg, bg}, RGB565      */
    uint32_t        flags;      /* CLCD_THEME_*                                 */
    const char     *name;       /* "today" | "aligned"                          */
} mps3_clcd_theme_t;

extern const mps3_clcd_theme_t clcd_theme_today;
extern const mps3_clcd_theme_t clcd_theme_aligned;

/* WEAK in clcd.c: returns &clcd_theme_today. harnessd's strong one is one line:
 *     const mps3_clcd_theme_t *mps3_clcd_palette(void) { return &clcd_theme_aligned; }
 * NULL, or a theme with pal == NULL, also means today. Must be O(1), no I/O. */
const mps3_clcd_theme_t *mps3_clcd_palette(void);

/* The tokens.json panel.roles key of a role ("text", "banner-held", ...), "" if
 * out of range. The `panel frame` role CODE of role r is CLCD_ROLE_CODE(r):
 * 'a' + r, so 'a' = text ... 'u' = banner-held (21 roles). */
#define CLCD_ROLE_CODE(r) ((char)('a' + (int)(r)))
const char *clcd_role_name(unsigned role);

/* ==========================================================================
 * R2 -- A ROLE-COLOURED LINE (what the session-row seam fills). text is 40
 * cells + NUL (a NUL before col 40 = spaces after it); role[i] colours cell i.
 * Helpers clip at col 40 and are pure.
 * ========================================================================== */
typedef struct {
    char    text[CLCD_COLS + 1];
    uint8_t role[CLCD_COLS];
} clcd_line_t;

void     clcd_line_clear(clcd_line_t *l);        /* 40 spaces, all CLCD_ROLE_TEXT */
/* Write s at col with role; returns the column after the last cell written. */
unsigned clcd_line_put(clcd_line_t *l, unsigned col, const char *s, unsigned role);
/* Right-align s so it ends `pad` cells before col 40. */
void     clcd_line_right(clcd_line_t *l, const char *s, unsigned role, unsigned pad);

/* ==========================================================================
 * R2 -- THE PANEL SEAMS (WEAK 0 in clcd.c; harnessd supplies them)
 *
 * RANKING on rows 10-12, highest first -- only one draws:
 *   1. the FAULT banner (link down, DUT clock, swap failed, static_id 0)
 *   2. the IDENTIFY banner (mps3_clcd_locate(), v0.16; unchanged)
 *   3. mps3_clcd_overlay()  (a lease request)
 *   4. [aligned theme] the R5 programming bar on row 10
 *   5. the plain rows: DIP (10), mps3_clcd_session_row() (11), the engine row (12)
 * Each lower item YIELDS its rows to a higher one (it is not drawn at all).
 * ========================================================================== */

/* Row 0, right side: the lease badge. 1 = drawn right-aligned, one cell of pad,
 * in `role` (CLCD_ROLE_TITLE_HELD / _TITLE_WARN in the aligned theme). It never
 * overwrites the board name: it is clipped to leave one blank cell after it.
 * With the today theme a badge replaces "nanoSoC harness". 0 = today's row 0. */
#define CLCD_TITLE_RIGHT_MAX 24u
typedef struct {
    char    text[CLCD_TITLE_RIGHT_MAX + 1];
    uint8_t role;
} mps3_clcd_badge_t;
int mps3_clcd_title_right(mps3_clcd_badge_t *out);

/* Row 11: the "hm" row (who is connected). 1 = draw `out` on row 11 (status page
 * only). The provider owns all 40 cells, labels included. 0 = row 11 as today. */
#define CLCD_SESSION_ROW 11u
int mps3_clcd_session_row(clcd_line_t *out);

/* Rows 10-12: a lease-request (or any engine) banner, ranked BELOW the fault and
 * identify banners, on both pages. Each line is centred on its row (glyphs
 * allowed, "" = a blank banner row); all three rows take `role` (a
 * CLCD_ROLE_BANNER_*; request = CLCD_ROLE_BANNER_HELD). `on` names what a tap on
 * rows 10-12 means while it shows (CLCD_ON_REQUEST, ...; CLCD_ON_NONE = no tap
 * target): clcd_hittest() then returns CLCD_ACT_BANNER and pushes that event. */
typedef struct {
    char    line[3][CLCD_COLS + 1];
    uint8_t role;
    uint8_t on;
} mps3_clcd_overlay_t;
int mps3_clcd_overlay(mps3_clcd_overlay_t *out);

/* ==========================================================================
 * R2 -- PAGE-AWARE HIT TEST + THE EVENT RING
 *
 * clcd_hittest(x, y) (existing signature) now dispatches on the page and the
 * banner on the committed frame:
 *   - mps3_clcd_tap() is still asked first (R3 locate: a tap = "found it"); when
 *     it takes the tap -> event {on: CLCD_ON_IDENTIFY}, returns CLCD_ACT_NONE.
 *   - the DUT OSD (KVM banner mode) has no targets: CLCD_ACT_NONE, no event.
 *   - an overlay with on != CLCD_ON_NONE: a tap on rows 10-12 -> event {on},
 *     returns CLCD_ACT_BANNER (never a page change).
 *   - each page's own boxes: row 14 = CLCD_ACT_NEXT_PAGE on the status and apps
 *     pages (the nav strip; the only touch path between pages) -> event
 *     {on: CLCD_ON_NAV}.
 *   - a miss: CLCD_ACT_NONE, no event.
 * One call = at most one event: touch_poll() calls it once per contact (edge),
 * and the ring write is O(1) with no I/O -- the touch path gains no cost.
 * ========================================================================== */
#define CLCD_ACT_BANNER 4        /* clcd_action_t gains: a banner target was tapped */

#define CLCD_EVENT_RING 8u
enum { CLCD_EV_TAP = 1 };
enum { CLCD_ON_NONE = 0, CLCD_ON_NAV, CLCD_ON_IDENTIFY, CLCD_ON_REQUEST };

typedef struct {
    uint32_t seq;        /* 1, 2, 3 ... since clcd_init(); never reused        */
    uint32_t t_ms;       /* mps3_sys_now_ms() at the tap; ms_ago = now - t_ms  */
    uint8_t  kind;       /* CLCD_EV_*                                          */
    uint8_t  on;         /* CLCD_ON_*                                          */
    uint8_t  page;       /* clcd_page_t at the tap                             */
    uint8_t  col, row;   /* the cell tapped                                    */
} clcd_event_t;

/* The newest event's seq (0 = none since clcd_init()). */
uint32_t    clcd_event_seq(void);
/* Copy the events with seq > after, OLDEST first, at most max (<= 8: the ring
 * keeps the last 8; older ones are gone and seq shows the gap). Returns the
 * count. Nothing is acknowledged or removed: two readers both see every tap. */
unsigned    clcd_events_since(uint32_t after, clcd_event_t *out, unsigned max);
/* "nav" | "identify" | "request" | "" (CLCD_ON_NONE / unknown). */
const char *clcd_event_on_name(unsigned on);

/* ==========================================================================
 * R2 -- FRAME + ROLES FOR `panel frame` (by row range: HM asks rows 0-7, 8-14)
 * ========================================================================== */
/* Copy rows [first, first+count) of the COMMITTED frame (what is on, or about to
 * be on, the glass; while the DUT owns the panel, the last harness frame = the
 * DUT OSD). rows[i] = 40 raw cells + NUL: printable ASCII, or a CLCD_GLYPH_*
 * byte (0x80-0x86); roles = count*40 role CODES (CLCD_ROLE_CODE) + NUL, so it
 * holds count*40+1 bytes. Either pointer may be NULL. The range is clipped to
 * rows 0-14; returns the rows copied (0 for first >= 15). Pure read, O(600). */
unsigned clcd_frame_rows(unsigned first, unsigned count,
                         char (*rows)[CLCD_COLS + 1], char *roles);

/* One row's cells as a JSON string BODY (no quotes): '"' and '\\' escaped, a
 * glyph byte as \u0080..\u0086 (Python json.loads -> chr(0x80), the key HM's
 * font uses), any other byte outside 0x20-0x7E as a space. NUL-terminated;
 * returns the length, or 0 (and out = "") if cap is too small. A row needs at
 * most 40*6+1 = 241 bytes (all glyphs); a typical row about 41. */
unsigned clcd_fmt_row_json(char *out, unsigned cap, const char *cells, unsigned n);

/* ==========================================================================
 * R2 -- WHAT THE PANEL SHOWS (for `hello`/`panel` replies)
 * ========================================================================== */
enum {
    CLCD_BANNER_NONE = 0,
    CLCD_BANNER_FAULT,        /* rows 10-12: a harness fault          */
    CLCD_BANNER_LOCATE,       /* rows 10-12: IDENTIFY: <who>          */
    CLCD_BANNER_OVERLAY,      /* rows 10-12: mps3_clcd_overlay()      */
    CLCD_BANNER_DUT,          /* the KVM OSD: the DUT has the panel   */
};

typedef struct {
    uint8_t     page;          /* clcd_page_t                                     */
    uint8_t     banner;        /* CLCD_BANNER_* on the committed frame            */
    uint8_t     relinquished;  /* 1 = the harness does not drive the pads (DUT
                                * owns the panel, or the KVM is mid-handover)     */
    uint8_t     prog_pct;      /* R5: 0-100 while a swap is in flight and its
                                * total is known; 255 otherwise                   */
    char        banner_text[CLCD_COLS + 1]; /* the banner's main line, glyphs and
                                * edge spaces stripped; "" = none                 */
    const char *theme;         /* the theme's name: "today" | "aligned"           */
    uint32_t    frame_seq;     /* +1 each reformat that changed any cell or role  */
    uint32_t    event_seq;     /* == clcd_event_seq()                             */
} clcd_panel_state_t;

void        clcd_panel_state(clcd_panel_state_t *out);
/* "status" | "apps" ("" out of range); and back: clcd_page_t, or -1 unknown. */
const char *clcd_page_name(unsigned page);
int         clcd_page_by_name(const char *name);
/* (existing) clcd_page_set()/clcd_page_get()/clcd_set_board_name() are unchanged;
 * clcd_page_set() while relinquished just records the page for the regain. */

/* ==========================================================================
 * R5 -- BOARD-SIDE PROGRAMMING PROGRESS (aligned theme only)
 *   row 3:  "prog  <phase> <overlay>          42%"   (CLCD_ROLE_BUSY)
 *   row 10: a 40-cell bar, CLCD_ROLE_BAR filled / CLCD_ROLE_TRACK empty
 * phase: clearing | receiving | pushing | verifying | finishing. No percent and
 * an empty track while the total is unknown. The bar yields to every banner.
 * ========================================================================== */
#define CLCD_PROG_ROW 3u
#define CLCD_BAR_ROW  10u
/* done/total as 0-100 (clamped), or 255 when total == 0 (unknown). Pure. */
unsigned clcd_prog_pct(uint32_t done, uint32_t total);
/* Filled bar cells for pct (0-100): round(40 * pct / 100); 0 for 255. Pure. */
unsigned clcd_bar_cells(unsigned pct);

/* ------------------------------------------------------------------------
 * Test-only introspection (host harness). Never compiled into the target.
 * ------------------------------------------------------------------------ */
#ifdef MPS3_CLCD_TEST_HOOKS
enum {
    CLCD_ST_RESET = 0,
    CLCD_ST_RST_WAIT,
    CLCD_ST_INIT,
    CLCD_ST_INIT_WAIT,
    CLCD_ST_IDLE,
    CLCD_ST_RENDER,
};
int      clcd_test_state(void);          /* current CLCD_ST_* */
uint32_t clcd_test_bytes_last_pass(void);/* bytes pushed by the most recent clcd_poll() */
uint32_t clcd_test_bytes_total(void);    /* bytes pushed since clcd_init() */
unsigned clcd_test_dirty_count(void);    /* cells currently marked dirty */
uint32_t clcd_test_swap_count(void);     /* locally-counted LOADED transitions */
void     clcd_test_force_reformat(void); /* run one reformat+diff immediately (IDLE) */
int      clcd_test_relinquished(void);   /* 1 = we have given up the pads to the DUT */
int      clcd_test_banner_mode(void);    /* 1 = reformat() is painting the OSD banner */
void     clcd_test_set_banner(int on);   /* force the OSD banner on/off (preview tool) */
void     clcd_test_set_page(int page);   /* force the current page (preview/tests)     */
/* Render one frame of the §8 layout from the current sources into the caller's
 * buffers (out = 600 cells row-major; inv_out = per-row inversion flag). Runs
 * the real reformat(); used by the off-target screen preview. */
void     clcd_test_render(char out[CLCD_NCELLS], uint8_t inv_out[CLCD_ROWS]);
/* Copy what the driver last committed to the shadow (what is, or is about to
 * be, on the glass) WITHOUT running reformat() -- so a test can observe whether
 * clcd_poll() itself refreshed the frame (e.g. on a USD change-count move). */
void     clcd_test_shadow(char out[CLCD_NCELLS], uint8_t inv_out[CLCD_ROWS]);
/* The committed role plane (enum clcd_role per cell), beside clcd_test_shadow(). */
void     clcd_test_roles(uint8_t out[CLCD_NCELLS]);
#endif

#ifdef __cplusplus
}
#endif

#endif /* MPS3_CLCD_H */
