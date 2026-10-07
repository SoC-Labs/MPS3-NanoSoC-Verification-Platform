/*
 * clcd_core.h — Linux port of the bare-metal CLCD status-display driver
 * (main repo firmware/clcd/clcd.{h,c}, the code behind the panel that is LIT
 * on silicon since 2026-07-14 — docs/CLCD_PANEL_FACTS.md).
 *
 * What ports verbatim (same bytes on the wire):
 *   - the HX8347-D init table (hx8347_init.{h,c}, copied unchanged, provenance
 *     retained; CLCD_ROTATE_180 default ON — panel mounted upside-down),
 *   - the 40x15 / 8x16 text geometry and font (font8x16.h, copied unchanged),
 *   - the per-cell GRAM window + RAMWR + RGB565-MSB-first cell stream (273
 *     bytes/cell), the dirty-cell diff, and the FIFO discipline: writes are
 *     DROPPED on full by the block, so try_push() polls STATUS.fifo_full and
 *     never writes into a full FIFO; every pass is budget-bounded.
 *
 * What deliberately changes under Linux (documented deviations):
 *   - SOURCES. The bare-metal reformat() read live firmware state (lwIP,
 *     swap_fsm, DFXCTL CSRs). Under Linux the DFXCTL page is owned by
 *     mps3_dfx.ko and MUST NOT be mmap'd concurrently by a display app, so the
 *     resident-RM row shows the driver's sysfs view (rm_id = last VERIFIED,
 *     plus the engine state) instead of the live-CSR sandwich read. When no
 *     driver is loaded the row says so — it never guesses. All sources are
 *     gathered by status_linux.c into clcd_status_t; the frame builder here is
 *     PURE (fully host-testable with mocked registers).
 *   - Row 7 shows the kernel identity instead of the CLKRST clock/reset bits:
 *     rp_resetn ownership belongs to the DFX driver (SERVICE_DISPOSITION §3.6)
 *     and this app does not touch CLKRST. Carried as an open item.
 *   - No KVM servicing: CLCDKVM is a Wave-4 block that has never been built.
 *     The daemon presence-gates on the DT/UIO node and never touches the page
 *     (under the MBV a DECERR is a real S-mode fault — DRIVER_MATRIX §2.9).
 *
 * MMIO goes through the clcd_io_t seam: a UIO backend in the daemon, a mock
 * register model in the host tests.
 */
#ifndef MPS3_APPS_CLCD_CORE_H
#define MPS3_APPS_CLCD_CORE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ---- text geometry: 320x240 panel / 8x16 font => 40 cols x 15 rows ------- */
#define CLCD_COLS     40u
#define CLCD_ROWS     15u
#define CLCD_NCELLS   (CLCD_COLS * CLCD_ROWS)   /* 600 */
#define CLCD_GLYPH_W  8u
#define CLCD_GLYPH_H  16u

/* One cell on the wire: 8 window-reg writes (cmd+data) + RAMWR + 8*16 px * 2 B */
#define CLCD_CELL_PREAMBLE 17u
#define CLCD_CELL_PIXELS   (CLCD_GLYPH_W * CLCD_GLYPH_H * 2u)   /* 256 */
#define CLCD_CELL_BYTES    (CLCD_CELL_PREAMBLE + CLCD_CELL_PIXELS) /* 273 */

/* Per-poll byte budget. The bare-metal 256 existed to protect lwIP's timers;
 * under Linux the constraint is only CPU/pacing, so the default is larger but
 * the discipline (bounded pass, FIFO-full poll) is identical. */
#ifndef CLCD_BYTES_PER_PASS
#define CLCD_BYTES_PER_PASS 4096u
#endif

#ifndef CLCD_REFRESH_MS
#define CLCD_REFRESH_MS 250u
#endif

#ifndef MPS3_BOARD_NAME
#define MPS3_BOARD_NAME "MPS3-01"   /* fpgahub node mps3_01 */
#endif

#define CLCD_RESET_PULSE_MS 2u

/* ---- rm_id v2 helpers (docs/VERSIONING_PLAN.md §3.2) — key on DESIGN half - */
#define CLCD_RM_DESIGN(rm_id)     ((uint32_t)(rm_id) & 0xFFFFu)
#define CLCD_RM_VER_MAJOR(rm_id)  (((uint32_t)(rm_id) >> 24) & 0xFFu)
#define CLCD_RM_VER_MINOR(rm_id)  (((uint32_t)(rm_id) >> 16) & 0xFFu)

/* Row-2 field geometry — same declared bounds as the firmware (overflow was a
 * real, silicon-visible bug there; the tests re-assert the bounds here). */
#define CLCD_RM_NAME_COL  6u
#define CLCD_RM_NAME_MAX 20u
#define CLCD_RM_VER_COL  27u
#define CLCD_RM_VER_MAX   8u

#define CLCD_RM_CAPS_UNKNOWN   "unknown -- no valid rm_id from RP"
#define CLCD_RM_CAPS_NODRIVER  "unknown -- dfx driver not loaded"

/* ---- frozen swap-FSM state numbering (drivers/icap/mps3_swap_transitions.h,
 * itself the verbatim port of the bare-metal table; values are FROZEN). ----- */
enum {
    CLCD_SWAP_IDLE = 0, CLCD_SWAP_GATE, CLCD_SWAP_DECOUPLE_ASSERT,
    CLCD_SWAP_STREAM_CLEARING, CLCD_SWAP_AWAIT_INCOMING_CLEARING,
    CLCD_SWAP_AWAIT_PARTIAL, CLCD_SWAP_STREAM_PARTIAL, CLCD_SWAP_VERIFY,
    CLCD_SWAP_CACHE_CLEARING, CLCD_SWAP_RELEASE, CLCD_SWAP_DONE,
    CLCD_SWAP_FAILED, CLCD_SWAP_REISOLATE, CLCD_SWAP_NSTATES
};

/* ---- the status snapshot the frame is built from (the Linux seam) -------- */
typedef struct {
    char     board_name[24];
    uint32_t static_id;
    uint64_t uptime_ms;

    char     ip[20];        /* dotted quad, "" = no address                  */
    int      link_up;       /* 1/0, -1 = unknown (no such netdev)            */
    int      speed_mbps;    /* -1 = unknown                                  */
    int      full_duplex;   /* 1/0, -1 = unknown                             */
    uint8_t  mac[6];
    int      mac_valid;

    int      dfx_present;   /* mps3_dfx.ko sysfs found                       */
    int      swap_state;    /* CLCD_SWAP_*, -1 = unknown                     */
    uint32_t rm_id;         /* driver's LAST-VERIFIED rm_id                  */
    int      rm_id_valid;   /* 1 = the rm_id field above may be interpreted  */
    uint64_t icap_bytes;

    uint64_t rx_dropped;    /* netdev statistics — the honest substitutes    */
    uint64_t tx_errors;     /* for the diag counters the firmware showed     */

    char     kernel[32];    /* uname -r, for row 7                           */
} clcd_status_t;

/* ---- MMIO seam ------------------------------------------------------------ */
typedef struct {
    uint32_t (*read32)(void *ctx, uint32_t off);
    void     (*write32)(void *ctx, uint32_t off, uint32_t v);
    void     *ctx;
} clcd_io_t;

/* ---- pure formatters (unit-tested directly) ------------------------------- */
void clcd_fmt_uptime(char *out, uint64_t ms);        /* "ddd:hh:mm:ss", >=13 B */
void clcd_fmt_hex32(char *out, uint32_t v);          /* "0x%08X", >=11 B       */
const char *clcd_rm_name(uint32_t rm_id);            /* <= CLCD_RM_NAME_MAX    */
const char *clcd_rm_caps(uint32_t rm_id);            /* <= 34 chars            */
void clcd_fmt_rm_version(char *out, uint32_t rm_id); /* "v%u.%u", >=9 B        */
const char *clcd_swap_state_name(int state);         /* frozen-state -> short  */

/* Build one 40x15 frame + per-row inversion flags from a status snapshot.
 * PURE: no MMIO, no clocks — everything rendered comes from *st and tick
 * (heartbeat spinner phase). Layout is the firmware's §8 screen with the
 * documented Linux deviations above. */
void clcd_build_frame(const clcd_status_t *st, unsigned tick,
                      char out[/* CLCD_NCELLS */], uint8_t inv_out[/* CLCD_ROWS */]);

/* Dirty-cell diff — ported verbatim. `dirty` is a CLCD_NCELLS-bit bitmap. */
#define CLCD_DIRTY_BYTES ((CLCD_NCELLS + 7u) / 8u)   /* 75 */
unsigned clcd_diff_cells(const char *cur, const char *next,
                         const uint8_t *cur_inv, const uint8_t *next_inv,
                         uint8_t *dirty);

/* ---- driver FSM (reset -> init table -> idle/render loop) ----------------- */
void clcd_core_init(const clcd_io_t *io, const char *board_name);
void clcd_core_update_status(const clcd_status_t *st);  /* copied in        */
void clcd_core_poll(uint64_t now_ms);   /* one bounded pass, never blocks   */
int  clcd_core_idle(void);              /* 1 = IDLE with nothing dirty      */

/* ---- introspection (tests + --preview) ------------------------------------ */
enum {
    CLCD_ST_RESET = 0, CLCD_ST_RST_WAIT, CLCD_ST_INIT, CLCD_ST_INIT_WAIT,
    CLCD_ST_IDLE, CLCD_ST_RENDER,
};
int      clcd_core_state(void);
uint32_t clcd_core_bytes_last_pass(void);
uint64_t clcd_core_bytes_total(void);
unsigned clcd_core_dirty_count(void);
void     clcd_core_shadow(char out[/* CLCD_NCELLS */], uint8_t inv[/* CLCD_ROWS */]);

#ifdef __cplusplus
}
#endif

#endif /* MPS3_APPS_CLCD_CORE_H */
