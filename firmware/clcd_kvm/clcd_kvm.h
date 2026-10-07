/*
 * clcd_kvm.h -- the harness side of the CLCD KVM (CSR block @ MPS3_CLCDKVM_BASE,
 * 0x44AD_0000). Thin, non-blocking accessors over the FROZEN register contract
 * in fpga/shell/ip/clcd_kvm/README.md §5 and docs/contracts/shell-regmap.md v0.5
 * (CLCDKVM). The handover POLICY lives in firmware/clcd/clcd.c; this file is the
 * mechanism, and nothing more.
 *
 * ============================ HAZARD (read this) ============================
 * There is NO AXI slave at 0x44AD on the shipped shell (shell_bd.tcl's
 * assign_bd_address stops at CLCD @ 0x44AC, NUM_MI = 15). A MicroBlaze access
 * there neither traps (bus exceptions are off) nor works -- STATUS reads back
 * garbage. The slave, the USER_nPB1 pad and a fresh static_id (re-keying all 8
 * overlays) land together in the Wave-4 static rebuild.
 *
 * So this module is gated behind MPS3_HAS_CLCD_KVM, and that define is OFF by
 * default. WITH IT OFF, EVERY FUNCTION BELOW IS A COMPILE-TIME NO-OP (an inline
 * stub that touches no register and folds away): clcd.c can therefore call them
 * unconditionally, with no #ifdef in the driver body, and a CLCD=1 build for
 * TODAY's bitstream is behaviourally identical to the one on the board now.
 * ===========================================================================
 *
 * THE TWO RULES THIS MODULE EXISTS TO ENFORCE
 * -------------------------------------------
 * 1. NEVER read-modify-write CTRL without going through clcd_kvm_ctrl_update().
 *    CTRL[0] src_sel is write-gated by CTRL[16] src_sel_we: a plain RMW (say, to
 *    set the backlight) that happened to write back a stale [0] WOULD SILENTLY
 *    CLOBBER an ownership change made by a concurrent USER_nPB1 press -- the
 *    button is sampled in HARDWARE and can move tgt_owner between our read and
 *    our write. ctrl_update() never sets src_sel_we, so it CANNOT move
 *    ownership; clcd_kvm_request_owner() is the one and only path that can.
 *
 * 2. EVENT is W1C, NOT read-to-clear. clcd_kvm_take_events() reads it and writes
 *    back EXACTLY THE BITS IT READ -- so a bit the KVM sets between the read and
 *    the write survives (we write 0 to it, and a 0 leaves a W1C bit set). Never
 *    blind-write CLCDKVM_EVENT_ALL: that would destroy an event you never saw.
 */
#ifndef MPS3_CLCD_KVM_H
#define MPS3_CLCD_KVM_H

#include <stdint.h>

#include "../common/platform_regs.h"

#ifdef __cplusplus
extern "C" {
#endif

#ifdef MPS3_HAS_CLCD_KVM
#define CLCD_KVM_PRESENT 1
#else
#define CLCD_KVM_PRESENT 0
#endif

#if CLCD_KVM_PRESENT

/* One-time setup. Writes CTRL ONCE with the whole steady state
 * {bl_rst_src=1, backlight=1, panel_rst_n=1, timeout_en=1, pb_en=1} -- one write,
 * because staging it (bl_rst_src first, backlight after) would blank the panel
 * and hold it in reset for the gap. Then clears any stale EVENT bits.
 *
 * bl_rst_src = 1 hands CLCD_BL / CLCD_RST to the KVM (README §10). It is what the
 * KVM contract asks firmware to do, and it is REQUIRED if the harness ever wants
 * to blank the panel while the DUT owns it. Note the consequence, which clcd.c
 * depends on: with bl_rst_src = 1 the CLCD block's own CTRL[1]/CTRL[2] NO LONGER
 * REACH THE PADS, so the legacy driver-owned reset pulse is INERT and the panel
 * reset must come from the KVM's hardware sequencer (clcd_kvm_panel_reset()). */
void clcd_kvm_init(void);

/* Undo the bl_rst_src=1 handover: CLCD_BL/CLCD_RST go back to following clcd_0's
 * CTRL[1]/[2] -- i.e. exactly today's shipped behaviour. This is the SAFE
 * DEGRADATION path: if the KVM's reset sequencer never reports back (a wedged or
 * mis-decoded KVM), clcd.c calls this and falls back to its own legacy reset
 * pulse, so the worst case is "the panel still lights, the KVM is just not
 * arbitrating" rather than "the board's only screen is dark forever". */
void clcd_kvm_release_bl_rst(void);

/* RO snapshot of STATUS (0x04). No side effects, at any offset in the page. */
uint32_t clcd_kvm_status(void);

/* Read EVENT (0x08) and clear EXACTLY the bits read (W1C). Returns them.
 * Bits the KVM sets between the read and the write-back are PRESERVED. */
uint32_t clcd_kvm_take_events(void);

/* Read-modify-write of CTRL that CANNOT move ownership: src_sel_we is never set,
 * and the three W1P bits (panel_rst_pulse / force_switch / src_sel_we) are
 * stripped from the read value before the write, so a RMW can never re-arm a
 * one-shot either. Use this for the backlight, dut_req_en, force_harness, ... */
void clcd_kvm_ctrl_update(uint32_t set_bits, uint32_t clr_bits);

/* THE ONLY PATH THAT MOVES OWNERSHIP. Writes CTRL with src_sel = owner AND
 * src_sel_we = 1 (and preserves every other RW bit). `owner` is
 * CLCDKVM_OWNER_HARNESS or CLCDKVM_OWNER_DUT.
 *
 * This sets the TARGET. The KVM then drains the outgoing owner, hard-resets the
 * panel, settles it and grants it to the incoming one (~7-9 ms). It does NOT
 * complete here, and there is nothing to wait for: the completion arrives as
 * EVENT.harness_gained / EVENT.panel_reset_done on a later poll. */
void clcd_kvm_request_owner(unsigned owner);

/* Arm ONE full hardware panel-reset sequence (S_RST -> S_SETTLE -> S_GRANT) with
 * no owner change (CTRL.panel_rst_pulse, W1P). Completion is reported as
 * EVENT.panel_reset_done, and the owner must then re-init and repaint -- the same
 * rule, and the same code path, as a real handover. This is firmware's
 * panel-recovery lever and the harness's COLD-BOOT reset (with bl_rst_src = 1 it
 * is the ONLY thing that can reset the panel). */
void clcd_kvm_panel_reset(void);

/* KVM-sourced CLCD_BL (active-high; used while bl_rst_src = 1). Safe RMW. */
void clcd_kvm_set_backlight(int on);

/* Opt the DUT's tunnelled `req` bit in as a request source (CTRL.dut_req_en,
 * reset 0 ON PURPOSE: an unprovisioned or garbage RM must not be able to grab the
 * panel at power-on, before the harness has painted anything). The USER_nPB1
 * button works regardless -- it is hardware. Safe RMW. */
void clcd_kvm_set_dut_req_en(int en);

#else  /* !CLCD_KVM_PRESENT -- compile-time no-ops; the page is UNMAPPED */

static inline void     clcd_kvm_init(void)                  { }
static inline void     clcd_kvm_release_bl_rst(void)        { }
static inline uint32_t clcd_kvm_status(void)                { return 0u; }
static inline uint32_t clcd_kvm_take_events(void)           { return 0u; }
static inline void     clcd_kvm_ctrl_update(uint32_t s, uint32_t c) { (void)s; (void)c; }
static inline void     clcd_kvm_request_owner(unsigned o)   { (void)o; }
static inline void     clcd_kvm_panel_reset(void)           { }
static inline void     clcd_kvm_set_backlight(int on)       { (void)on; }
static inline void     clcd_kvm_set_dut_req_en(int en)      { (void)en; }

#endif /* CLCD_KVM_PRESENT */

/* ---- pure helpers over a STATUS word (no MMIO; always available) ---------- */

/* The COMMITTED owner -- what is actually reaching the pads right now. */
static inline int clcd_kvm_owner_is_dut(uint32_t status)
{
    return (status & CLCDKVM_STATUS_OWNER) ? 1 : 0;
}

/* A switch is in flight (tgt_owner != owner). While this is set the harness
 * should GO QUIET: the KVM's outgoing gate (S_DRAIN) waits for
 * `h_fifo_empty && !h_busy`, so every byte we push extends the wait -- and if we
 * push for longer than TIMEOUT.timeout_us (1 ms) the KVM preempts us and cuts the
 * stream mid-FIFO. Going quiet closes the gate in ~14 us (a full 128-entry FIFO
 * at ~110 ns/byte) and makes the handover clean and fast. */
static inline int clcd_kvm_switch_pending(uint32_t status)
{
    return (status & CLCDKVM_STATUS_SWITCH_PENDING) ? 1 : 0;
}

/* NEITHER source reaches the pads: the KVM is driving its own idle pattern
 * (S_RST / S_SETTLE / S_GRANT). Bytes pushed now are DISCARDED -- do not stream
 * the init table, and do not start a repaint, until this is clear. */
static inline int clcd_kvm_drives_pads(uint32_t status)
{
    return (status & CLCDKVM_STATUS_KVM_DRIVES_PADS) ? 1 : 0;
}

#ifdef __cplusplus
}
#endif

#endif /* MPS3_CLCD_KVM_H */
