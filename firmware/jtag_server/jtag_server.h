/*
 * jtag_server.h — TCP 6921 OpenOCD remote_bitbang JTAG server -> JTAG pins.
 *
 *   ***  STAGED / UNVALIDATED SCAFFOLDING (2026-07-28) — NOT COMPILED.  ***
 *
 * "Test-tomorrow" ground-work: the JTAG analogue of swd_server (firmware/
 * swd_server/swd_server.h). It has NOT been built against a BSP or the
 * host-gcc test harness. It is written to be compile-SHAPED (a one-for-one
 * mirror of swd_server) but nothing here claims to compile or link.
 *
 * See docs/planning/MPS3_JTAG_DEBUG_DESIGN.md for the whole design, the
 * OpenOCD remote_bitbang JTAG protocol mapping (§5), the integration points
 * (§6), and the proven-vs-unproven ledger (§7).
 *
 * A6 DONE (SWD->JTAG boundary re-mint 0xCD74B6AE landed): the JTAGBB base +
 * register offsets/fields have been FOLDED into common/platform_regs.h, beside
 * the SWDBB block (they are the same 0x44A7 slot). Consumers (jtag_server.c,
 * test_jtag_server.c) include platform_regs.h directly. Only MPS3_PORT_JTAG
 * remains local below, still awaiting its net_proto.h port-map fold.
 */
#ifndef MPS3_JTAG_SERVER_H
#define MPS3_JTAG_SERVER_H

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ==========================================================================
 * LOCAL contract placeholders (TODO: fold into net_proto.h / platform_regs.h)
 * ==========================================================================
 * TODO(A6): move MPS3_PORT_JTAG into common/net_proto.h's port map, beside
 * MPS3_PORT_SWD (6920). 6921 matches nanosoc_mps3_jtag.cfg's RBB_PORT default. */
#ifndef MPS3_PORT_JTAG
#define MPS3_PORT_JTAG 6921u /* TCP OpenOCD remote_bitbang JTAG -> jtag_server */
#endif

/* MPS3_JTAGBB_BASE + the JTAGBB_DRIVE/SAMPLE offsets and field bits now live in
 * common/platform_regs.h (A6 fold — see the header note above). jtag_bb and
 * axi_jtag (0x44AF) remain MUTUALLY-EXCLUSIVE debug options; jtag_bb reuses the
 * former SWDBB slot 0x44A7, NOT the 0x44B1 keep-both staging address. */

/* ==========================================================================
 * Server API (mirrors swd_server.h)
 * ========================================================================== */

/* Current pin state as this module understands it -- mirrors what is written
 * to / read from MPS3_JTAGBB_BASE. Kept as a struct (as swd_server does) so the
 * byte-dispatch logic is exercisable host-side even without real registers. */
typedef struct {
    uint8_t tck;   /* 0/1 */
    uint8_t tms;   /* 0/1 */
    uint8_t tdi;   /* 0/1 */
    uint8_t tdo;   /* last-sampled TDO input value */
    uint8_t srst;  /* dbg_resetn sense: 1 = asserted (held in reset) --
                    * same mapping/caveat as swd_server (CLKRST "1=released",
                    * so asserting srst CLEARS CLKRST_RESET_CTRL_DBG_RESETN). */
} jtag_pin_state_t;

extern jtag_pin_state_t g_jtag_pins;

void jtag_server_init(void);

/* Non-blocking poll -- accept a connection if none is open, drain and dispatch
 * available bytes one remote_bitbang JTAG command at a time, write any required
 * reply byte ('R' sample -> '0'/'1'). No-ops entirely while a swap has gated
 * DUT-debug access (mirrors swd_server_poll's g_shell_state.swd_gated check --
 * see the design note §6). */
void jtag_server_poll(void);

/* THE CLAIM LOCK on 6921 -- xvc_server.h's rule, the jtag line:
 *   {"ok":false,"err":"jtag locked: board claimed (use ssh)","code":"locked"}
 * WEAK default 0 (bare metal); mps3-harnessd's is in slot_linux.c. */
struct mps3_net_conn;
int mps3_jtag_refuse_peer(struct mps3_net_conn *conn);
#define MPS3_JTAG_LOCKED_LINE \
    "{\"ok\":false,\"err\":\"jtag locked: board claimed (use ssh)\",\"code\":\"locked\"}\n"

/* Dispatch a single remote_bitbang JTAG protocol byte. *reply_out receives the
 * reply byte for 'R' (sample: '0'/'1'), else 0 (no reply expected). Returns
 * 0 = handled, 1 = 'Q' quit (caller closes the connection), -1 = unrecognized
 * byte (caller drops the connection, fail closed). Exposed separately (as in
 * swd_server) so the JTAG bit-order mapping is concentrated in one place and
 * a conformance test can re-derive every byte from the OpenOCD formulas. */
int jtag_server_handle_byte(uint8_t c, uint8_t *reply_out);

#ifdef __cplusplus
}
#endif

#endif /* MPS3_JTAG_SERVER_H */
