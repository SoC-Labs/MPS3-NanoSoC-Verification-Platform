/*
 * coordinator.c — main loop + control-channel dispatch.
 *
 * See coordinator/README.md for the design rationale (why every _poll() is
 * non-blocking) and firmware/README.md for the assumed lwIP RAW-API build
 * model. This file only shapes the flow; every I/O body is TODO(A3).
 */
#include <string.h>
#include <stdio.h>
#include <inttypes.h>
#include "coordinator.h"
#include "swap_fsm.h"
#include "../config_agent/config_agent.h"
#include "../overlay_store/overlay_store.h"
#include "../jtag_server/jtag_server.h"
#include "../xvc_server/xvc_server.h"
#include "../uart_over_eth/uart_over_eth.h"
#include "../clkrst/clkrst.h"
#include "../clcd_kvm/clcd_kvm.h"
#include "../common/diag.h"
#include "../common/log_ring.h"   /* v0.11 `log`                                  */
#include "../common/service.h"    /* v0.11 `stats` window + `reboot` kick inhibit */
#include "../common/timebase.h"   /* mps3_sys_now_ms -- stats.up_ms, reboot delay */
#ifdef MPS3_HAS_TOUCH
#include "../touch/touch.h"       /* v0.11 `touch_cal`; `stats` touch_* (D1)      */
#endif
/* The harness-version SEAM only (mps3_version.h is hand-written and pulls in
 * nothing but <stdint.h>/<stdbool.h>) -- NOT a platform/lwIP/Xilinx header,
 * so the firmware/platform/ containment rule in firmware/README.md holds.
 * firmware/test/test_version.c includes it the same way. */
#include "../platform/mps3_version.h"
/* TODO(A3): #include "../smsc911x/smsc911x.h" once the LAN9220 driver is
 * ported (see smsc911x/README.md -- porting notes only exist today, no
 * header/.c yet, so no smsc911x_*() calls below are real function calls
 * yet either -- shaped as comments in coordinator_init() below). */

mps3_shell_state_t g_shell_state;

/* WEAK fallback for the static_id seam (see coordinator.h): 0 = "not
 * provisioned". The real shell build links a generated strong override
 * (W-DFX-ART's static_id scheme); firmware/test/ binaries and ctrl_echo
 * override it with a known test constant. Weak-linking keeps this file
 * identical between the target build and every test build. */
__attribute__((weak)) uint32_t mps3_shell_static_id(void)
{
    return 0u;
}

/* WEAK "never refuse" for the swap/commit refusal seam (coordinator.h). */
__attribute__((weak)) const char *mps3_swap_refusal(void)
{
    return 0;
}

/* WEAK "no slots here" for the v0.14 `slot` provider (coordinator.h). Every act
 * declines, with the same text, so a tool can tell "this engine has no boot
 * slots" from "the act failed" without parsing anything else. */
__attribute__((weak)) const char *mps3_slot_op(int act, int sel, mps3_slot_status_t *st)
{
    (void)act;
    (void)sel;
    (void)st;
    return "slot not supported";
}

/* WEAK "no debug-port lock" for the `xvc_lock` feature bit (coordinator.h). */
__attribute__((weak)) int mps3_debug_lock_supported(void)
{
    return 0;
}

/* WEAK "no claim, never locked" for the store's claim-lock seam (coordinator.h). */
__attribute__((weak)) int mps3_claim_refuses_active_peer(const char *what)
{
    (void)what;
    return 0;
}

/* WEAK "no slots here" for the `slot` feature bit (coordinator.h): 0 = no
 * "slot" in version.features, exactly as before the bit existed. */
__attribute__((weak)) int mps3_slot_supported(void)
{
    return 0;
}

/* WEAK "no identity resolver" for the v0.16 identity verbs (coordinator.h). */
__attribute__((weak)) const char *mps3_identity_op(int set, const mps3_ctrl_request_t *req,
                                                   const char **body, char *code,
                                                   size_t code_cap)
{
    (void)set;
    (void)req;
    (void)body;
    (void)snprintf(code, code_cap, "not_supported");
    return "identity not supported";
}

/* WEAK "no locate" for the v0.16 locate verb (coordinator.h). */
__attribute__((weak)) const char *mps3_locate_op(const mps3_ctrl_request_t *req,
                                                 const char **body, char *code,
                                                 size_t code_cap)
{
    (void)req;
    (void)body;
    (void)snprintf(code, code_cap, "not_supported");
    return "locate not supported";
}

/* WEAK "no presence" / "no panel" for the v0.17 verbs (coordinator.h): bare
 * metal (and a panel-less Linux build) declines both, as for `locate`. */
__attribute__((weak)) const char *mps3_hello_op(const mps3_ctrl_request_t *req,
                                                const char **body, char *code,
                                                size_t code_cap)
{
    (void)req;
    (void)body;
    (void)snprintf(code, code_cap, "not_supported");
    return "hello not supported";
}

__attribute__((weak)) const char *mps3_panel_op(const mps3_ctrl_request_t *req,
                                                const char **body, char *code,
                                                size_t code_cap)
{
    (void)req;
    (void)body;
    (void)snprintf(code, code_cap, "not_supported");
    return "panel not supported";
}

/* WEAK "no card job" for the `reboot` card-job seam (coordinator.h). Bare metal
 * keeps v0.11's reboot rules unchanged: only a swap refuses it. */
__attribute__((weak)) int mps3_card_job_active(void)
{
    return 0;
}

/* v0.11 per-boot state (defined with its verbs at the end of this file). */
static void coordinator_v011_reset(void);

/* THE HELD RESPONSE. A verb whose answer is only known later (an accepted
 * `swap`, a v0.13 `commit`, a `usd` format/clear) makes dispatch return 0 and
 * records itself here; coordinator_held_poll() builds its response once it has
 * settled. The resources behind the three are mutually exclusive (one swap FSM,
 * one store), so at most one is ever held. s_defer is the handler -> dispatch
 * "I deferred" signal. */
static mps3_ctrl_op_t s_held_op;
static int            s_defer;

void coordinator_init(void)
{
    /* TODO(A3): lwIP init.
     *   - netif_add() bound to the smsc911x driver's link-output fn
     *   - static IP (net_proto.h MPS3_DEFAULT_IP_*) or DHCP per D8
     *   - lwIP timer sources (sys_check_timeouts / TCP/ARP/DHCP timers)
     */

    /* TODO(A3): smsc911x_init() -- LAN9220 bring-up (see smsc911x/README.md
     * for the bare-metal port plan; do not vendor Zephyr code, just follow
     * its register sequence). Must complete before netif is marked up.
     */

    memset(&g_shell_state, 0, sizeof(g_shell_state));
    coordinator_v011_reset();
    s_held_op = MPS3_OP_UNKNOWN;
    s_defer = 0;
    /* static_id provenance: the mps3_shell_static_id() seam (coordinator.h)
     * -- a build-time-generated strong override on the real shell
     * (W-DFX-ART), a weak 0 fallback otherwise. Reading it back from a
     * hardware build-id register instead remains an option if A1/A6 ever
     * define one; the seam localizes that change to one function. */
    g_shell_state.static_id = mps3_shell_static_id();
    g_shell_state.current_rm_id = 0; /* greybox until a swap (or the power-on load) verifies another */

    clkrst_init();
    swap_fsm_init();
    config_agent_init();
    /* Thread the shell identity into config_agent (kept a setter so that
     * module stays coordinator.h-free — see config_agent.c's file header). */
    config_agent_set_running_static_id(g_shell_state.static_id);
    /* The overlay store on the USER microSD (D13): binds the engine's block
     * device, no I/O yet. The QSPI staging sinks it used to register are gone
     * with the SST26 backend -- clearings are RAM-resident (the target sizes
     * config_agent's clearing slot and swap_fsm's arena for the largest one) and
     * large partials stream ICAP-direct (below). */
    overlay_store_init();
#ifdef MPS3_CFG_AGENT_ICAP_DIRECT
    /* Path 3 (stream-direct) — the north-star unblock (docs/
     * OVER_THE_WIRE_RECONFIG_PLAN.md §5). Register the HWICAP stream-direct
     * PARTIAL sink so a large partial (886 KB–1.65 MB) streams straight to the
     * ICAP as it arrives over 6910, bypassing RAM/QSPI staging entirely — this
     * removes the partial size limit for EVERY RM without needing QSPI
     * first-light (the 886 KiB partial fits neither the 256 KiB LMB nor the
     * not-yet-operational onboard flash). config_agent PREFERS this over the
     * QSPI partial sink for large partials when it is registered; the small-
     * payload RAM path and the large-CLEARING QSPI path are unchanged. Gated
     * behind MPS3_CFG_AGENT_ICAP_DIRECT so the host test build and a
     * QSPI-staging build stay byte-for-byte unchanged. */
    config_agent_set_icap_direct_sink(swap_fsm_icap_direct_sink());
#endif
    jtag_server_init();  /* JTAG cutover: jtag_bb @0x44A7 (swd_server_init dormant) */
    xvc_server_init();
    uart_over_eth_init();

    /* W-NET-SEAM: the TCP 6900 control listener (line assembly ->
     * coordinator_dispatch_line() -> send-now vs park-for-swap) is real in
     * coordinator_net.c, written against common/net_if.h; config_agent's
     * 69/6910 listeners were opened by config_agent_init() above the same
     * way. The lwIP RAW-API backing of that seam is the one remaining
     * network TODO (net_if.h's file header names it). */
    coordinator_net_init();

    /* ARCHITECTURE_SPEC.md §6.1 step 4 (load the default overlay so the board
     * self-boots to a working DUT) is NO LONGER a blocking call here. It is the
     * overlay store's power-on hook, run from the "usd" service row once the
     * superloop -- and so the network -- is up, ONCE PER FPGA CONFIGURATION,
     * as an ordinary swap with the internal source "usd" (overlay_store.h THE
     * BOOT LATCH). */
}

/* coordinator_main_loop() USED TO LIVE HERE and was deleted (2026-09-09).
 *
 * It was declared noreturn, compiled into every image, and CALLED BY NOTHING on
 * the target: firmware/platform/src/main.c owns the real superloop (its header
 * says why -- the platform-only lwIP servicing has to interleave with the module
 * polls), and the only other caller was the retired harness_app spike, whose
 * directory no longer exists. Two superloops with one authoritative
 * sequence is a divergence waiting to happen: the copy here still polled
 * swd_server (TCP 6920), which the JTAG cutover retired in favour of
 * jtag_server (6921) -- so the dead loop was ALSO documenting a service the
 * live loop no longer runs. The sequence, and its rationale, live in main.c.
 */

/* -------------------------------------------------------------------------
 * Control-channel dispatch
 * ------------------------------------------------------------------------- */

/* One helper for every "write the response line" exit: if the real encode
 * fails (out too small / inconsistent resp -- both firmware bugs, since
 * callers size out >= MPS3_CTRL_RESP_MAX), fall back to a minimal error
 * line rather than sending nothing or a truncated line. */
static void encode_or_fallback(const mps3_ctrl_response_t *resp, char *out, int out_len)
{
    if (mps3_ctrl_encode_response(resp, out, out_len) < 0) {
        (void)snprintf(out, (size_t)out_len,
                       "{\"ok\":false,\"err\":\"encode overflow\"}\n");
    }
}

static void set_err(mps3_ctrl_response_t *resp, const char *msg)
{
    resp->ok = 0;
    strncpy(resp->err, msg, sizeof(resp->err) - 1);
    resp->err[sizeof(resp->err) - 1] = '\0';
}

/* The claim lock on a D13 store mutation (coordinator.h seam; weak 0 on bare
 * metal). 1 = refused, with the slot lock's shape and the stable code. */
static int claim_locked(mps3_ctrl_response_t *resp, const char *what)
{
    if (!mps3_claim_refuses_active_peer(what)) {
        return 0;
    }
    char msg[48];   /* < sizeof(resp->err): no strncpy truncation to warn about */
    (void)snprintf(msg, sizeof(msg), "%s locked: board claimed (use ssh)", what);
    set_err(resp, msg);
    strncpy(resp->code, "locked", sizeof(resp->code) - 1u);
    return 1;
}

/* `dutrx` (net-protocol.md v0.10) — the DUT's Ethernet RETURN path. Static, and
 * deliberately not in coordinator.h beside the other handlers: nothing outside
 * this file calls it. Every wire-level test drives it the way a client does,
 * through coordinator_dispatch_line(), and a handler reachable only through the
 * dispatcher cannot be called with a request the decode never produced. */
static void coordinator_handle_dutrx(const mps3_ctrl_request_t *req,
                                     mps3_ctrl_response_t *resp);
/* v0.11 -- same discipline as dutrx: reachable only through the dispatcher. */
static void coordinator_handle_stats(const mps3_ctrl_request_t *req,
                                     mps3_ctrl_response_t *resp);
static void coordinator_handle_log(const mps3_ctrl_request_t *req,
                                   mps3_ctrl_response_t *resp);
static void coordinator_handle_touch_cal(const mps3_ctrl_request_t *req,
                                         mps3_ctrl_response_t *resp);
static void coordinator_handle_reboot(const mps3_ctrl_request_t *req,
                                      mps3_ctrl_response_t *resp);
/* v0.14 -- same discipline again. */
static void coordinator_handle_slot(const mps3_ctrl_request_t *req,
                                    mps3_ctrl_response_t *resp);
/* v0.16 -- the identity verbs (identity / identity_set) and locate. */
static void coordinator_handle_identity(const mps3_ctrl_request_t *req,
                                        mps3_ctrl_response_t *resp, int set);
/* v0.17 -- presence (`hello`) and the front panel (`panel`). */
static void coordinator_handle_panel(const mps3_ctrl_request_t *req,
                                     mps3_ctrl_response_t *resp, int hello);

int coordinator_dispatch_line(const char *line, int len, char *out, int out_len)
{
    mps3_ctrl_request_t  req;
    mps3_ctrl_response_t resp;

    memset(&req, 0, sizeof(req));
    memset(&resp, 0, sizeof(resp));

    int rc = mps3_ctrl_decode_line(line, len, &req);
    if (rc != MPS3_CTRL_DECODE_OK) {
        resp.op = MPS3_OP_UNKNOWN;
        set_err(&resp,
                (rc == MPS3_CTRL_DECODE_EUNKNOWN_OP) ? "unknown op" :
                (rc == MPS3_CTRL_DECODE_EBADARGS)    ? "bad args"   :
                                                       "bad json");
        encode_or_fallback(&resp, out, out_len);
        return 1;
    }
    resp.op = req.op;

    switch (req.op) {
    case MPS3_OP_PING:      coordinator_handle_ping(&req, &resp);      break;
    case MPS3_OP_RESET:     coordinator_handle_reset(&req, &resp);     break;
    case MPS3_OP_SET_CLK:   coordinator_handle_set_clk(&req, &resp);   break;
    case MPS3_OP_SWAP:
        coordinator_handle_swap(&req, &resp);
        if (resp.ok) {
            /* Accepted: the real response (confirmed rm_id + verified) is
             * only known once swap_fsm settles -- hold it (see
             * coordinator.h's dispatch doc + coordinator_swap_final_response). */
            s_held_op = MPS3_OP_SWAP;
            return 0;
        }
        break;
    case MPS3_OP_LINK:      coordinator_handle_link(&req, &resp);      break;
    case MPS3_OP_COMMIT:
        s_defer = 0;
        coordinator_handle_commit(&req, &resp);
        if (s_defer) {
            /* v0.13: accepted -- the pair now comes over 6910; the answer is
             * held exactly like swap's. */
            s_held_op = MPS3_OP_COMMIT;
            return 0;
        }
        break;
    case MPS3_OP_USD:
        s_defer = 0;
        coordinator_handle_usd(&req, &resp);
        if (s_defer) {
            s_held_op = MPS3_OP_USD;   /* a format / clear job is running */
            return 0;
        }
        break;
    case MPS3_OP_TELEMETRY: coordinator_handle_telemetry(&req, &resp); break;
    case MPS3_OP_MACGEN:    coordinator_handle_macgen(&req, &resp);    break;
    case MPS3_OP_DIAG:      coordinator_handle_diag(&req, &resp);      break;
    case MPS3_OP_DISPLAY:   coordinator_handle_display(&req, &resp);   break;
    case MPS3_OP_VERSION:   coordinator_handle_version(&req, &resp);   break;
    case MPS3_OP_DUTRX:     coordinator_handle_dutrx(&req, &resp);     break;
    case MPS3_OP_STATS:     coordinator_handle_stats(&req, &resp);     break;
    case MPS3_OP_LOG:       coordinator_handle_log(&req, &resp);       break;
    case MPS3_OP_TOUCH_CAL: coordinator_handle_touch_cal(&req, &resp); break;
    case MPS3_OP_REBOOT:    coordinator_handle_reboot(&req, &resp);    break;
    case MPS3_OP_SLOT:      coordinator_handle_slot(&req, &resp);      break;
    case MPS3_OP_IDENTITY:     coordinator_handle_identity(&req, &resp, 0); break;
    case MPS3_OP_IDENTITY_SET: coordinator_handle_identity(&req, &resp, 1); break;
    case MPS3_OP_LOCATE:       coordinator_handle_identity(&req, &resp, 2); break;
    case MPS3_OP_HELLO:        coordinator_handle_panel(&req, &resp, 1);    break;
    case MPS3_OP_PANEL:        coordinator_handle_panel(&req, &resp, 0);    break;
    default:
        /* Unreachable: decode never returns OK with MPS3_OP_UNKNOWN. */
        resp.op = MPS3_OP_UNKNOWN;
        set_err(&resp, "unknown op");
        break;
    }

    encode_or_fallback(&resp, out, out_len);
    return 1;
}

/* Formats a u32 id the way the control channel reports identities
 * ("0x" + 8 lowercase hex digits). Width/case are a firmware-side choice,
 * not contract-fixed (net-protocol.md just shows "<static_id>"); pyverify
 * treats both as opaque strings. Flag for A6 if the fake shell picks a
 * different rendering -- the two servers should agree. */
static void format_id_hex(char *dst, size_t dst_sz, uint32_t id)
{
    (void)snprintf(dst, dst_sz, "0x%08" PRIx32, id);
}

void coordinator_handle_ping(const mps3_ctrl_request_t *req, mps3_ctrl_response_t *resp)
{
    (void)req;
    resp->ok = 1;
    /* shell_id = this build's static_id (mps3_shell_static_id() seam via
     * coordinator_init()); rm_id = the coordinator's view of what is
     * resident in the RP (updated by swap_fsm's verified DFXCTL.RM_ID
     * readback and by overlay_store's boot-load) -- deliberately NOT a
     * fresh DFXCTL.RM_ID register read here: mid-swap the register is
     * transient, while current_rm_id stays the last VERIFIED identity. */
    format_id_hex(resp->shell_id, sizeof(resp->shell_id), g_shell_state.static_id);
    format_id_hex(resp->rm_id, sizeof(resp->rm_id), g_shell_state.current_rm_id);
}

void coordinator_handle_reset(const mps3_ctrl_request_t *req, mps3_ctrl_response_t *resp)
{
    /* net-protocol.md: {"op":"reset","target":"dut"} -- only "dut" is shown
     * in the example; ARCHITECTURE_SPEC §5 also defines a debug reset
     * (srst, driven from swd_server via CLKRST.RESET_CTRL.dbg_resetn) and a
     * reconfig reset (owned internally by swap_fsm, not host-triggerable
     * directly). TODO(A3): decide + document whether target="dbg" is a
     * legal value here or is srst-only-via-SWD; not specified in the
     * contract, so treat "dut" as the only accepted value until A6 says
     * otherwise. */
    if (strcmp(req->target, "dut") == 0) {
        clkrst_pulse_reset(CLKRST_RESET_CTRL_DUT_RESETN);
        resp->ok = 1;
    } else {
        set_err(resp, "bad target");
    }
}

/* DUT clock the firmware last PROGRAMMED, in MHz (`stats.dut_mhz`). Seeded with
 * the block design's power-on output -- clk_wiz_dut CLKOUT1_REQUESTED_OUT_FREQ
 * 50.000 (fpga/shell/bd/shell_bd.tcl) -- because nothing reprograms the MMCM at
 * boot: until the first `set_clk`, 50 MHz is what the DUT is running at.
 * CLKRST.DUT_CLK_SEL cannot answer this: it resets to 0, which is the 25 MHz
 * preset's id, and it is an inert scratch register besides. */
#ifndef MPS3_DUT_CLK_BOOT_MHZ
#define MPS3_DUT_CLK_BOOT_MHZ 50u
#endif
static uint32_t s_dut_mhz = MPS3_DUT_CLK_BOOT_MHZ;

void coordinator_handle_set_clk(const mps3_ctrl_request_t *req, mps3_ctrl_response_t *resp)
{
    /* clkrst_set_preset() maps req->preset ("25mhz"/"50mhz"/"100mhz") to the
     * MMCM multiply/divide/output-divide and reprograms the DUT-clock MMCM over
     * the clk_wiz AXI4-Lite DRP (MMCM_DRP block), then bounded-polls
     * CLKRST.STATUS.mmcm_locked for the relock. Returns <0 on an unknown preset
     * (rejected below), 1 locked, 0 not-yet-locked. */
    int locked = clkrst_set_preset(req->preset);
    if (locked < 0) {
        set_err(resp, "unknown preset");
        return;
    }
    /* Record what was PROGRAMMED for `stats.dut_mhz`: 50 MHz (osc_clk_50m) *
     * M / (D * O), from the same table row the DRP was just written with. */
    for (int i = 0; i < clkrst_preset_table_len; i++) {
        const clkrst_preset_t *p = &clkrst_preset_table[i];
        if (strcmp(req->preset, p->name) == 0 && p->divclk != 0u && p->clkout0_div != 0u) {
            s_dut_mhz = (50u * (uint32_t)p->mult) /
                        ((uint32_t)p->divclk * (uint32_t)p->clkout0_div);
            break;
        }
    }
    resp->ok = 1;
    resp->locked = (locked > 0);
}

void coordinator_handle_swap(const mps3_ctrl_request_t *req, mps3_ctrl_response_t *resp)
{
    /* Non-blocking: just arm the FSM. The *actual* response (verified +
     * rm_id, net-protocol.md step 7) is only known once swap_fsm reaches
     * SWAP_DONE/SWAP_FAILED -- on this single-threaded server the single
     * request/response pair the contract shows is a HELD response:
     * coordinator_dispatch_line() returns 0 (deferred) when this handler
     * accepts, and the network layer replies later via
     * coordinator_swap_final_response() (which reads
     * swap_fsm_last_result()). The pcb bookkeeping for "later" is
     * W-NET-SEAM's; the codec + result plumbing on this side are real. */
    const char *why = mps3_swap_refusal();
    if (why != 0) {
        set_err(resp, why);   /* refused before the FSM (and any register) moves */
        return;
    }
    /* "usd" is the FSM's INTERNAL source (the power-on load); a host cannot
     * name it in v0.13. */
    if (strcmp(req->src, "usd") == 0) {
        set_err(resp, "bad args");
        return;
    }
    /* A commit or a `usd` format/clear holds the store: a swap now would change
     * what is running under a commit that promised to persist it. */
    if (overlay_store_commit_active() || overlay_store_action_active()) {
        set_err(resp, "store busy");
        return;
    }
    if (swap_fsm_start(req->rm, req->src) == 0) {
        resp->ok = 1; /* accepted -> dispatch defers the response */
    } else {
        set_err(resp, "swap already in progress");
    }
}

void coordinator_swap_final_response(mps3_ctrl_response_t *resp)
{
    const mps3_swap_result_t *r = swap_fsm_last_result();

    memset(resp, 0, sizeof(*resp));
    resp->op = MPS3_OP_SWAP;
    if (!r->valid) {
        /* Called before the FSM settled (or without any swap in flight) --
         * caller sequencing bug; fail closed rather than invent a result. */
        set_err(resp, "swap not complete");
        return;
    }
    if (s_held_op == MPS3_OP_SWAP) {
        s_held_op = MPS3_OP_UNKNOWN;   /* the held swap's answer is taken */
    }
    if (!r->ok) {
        /* Failure shape is the uniform {"ok":false,"err":...} -- rm_id/
         * verified are not reported for a failed swap (net-protocol.md
         * defines no failure variant; the RP is parked decoupled, see
         * swap_fsm.c's SWAP_FAILED note). */
        set_err(resp, "swap failed");
        return;
    }
    resp->ok = 1;
    resp->verified = r->verified ? 1 : 0;
    format_id_hex(resp->rm_id, sizeof(resp->rm_id), r->rm_id);
}

void coordinator_handle_link(const mps3_ctrl_request_t *req, mps3_ctrl_response_t *resp)
{
    /* {"op":"link","event":"down"} -- VPHY link injection (§8.2 virtual PHY,
     * host-injected link events for DUT testing). */
    uint32_t bits = 0;
    if (strcmp(req->event, "down") == 0) {
        bits = VPHY_LINK_EVENT_FORCE_DOWN;
    } else if (strcmp(req->event, "up") == 0) {
        bits = 0; /* release force_down */
    } else if (strcmp(req->event, "pulse") == 0) {
        bits = VPHY_LINK_EVENT_PULSE;
    } else {
        set_err(resp, "bad event");
        return;
    }
    mps3_reg_write32(MPS3_VPHY_BASE, VPHY_LINK_EVENT, bits);
    resp->ok = 1;
}

void coordinator_handle_commit(const mps3_ctrl_request_t *req, mps3_ctrl_response_t *resp)
{
    /* v0.13 (D13, D1 = re-push): persist what is RUNNING into the user
     * microSD's INACTIVE slot. The request names the pair (ids, lengths, CRCs);
     * the host then pushes it over 6910 exactly as for a swap while this
     * connection is PARKED; the store writes it, reads BOTH regions back and
     * CRCs them, and only then flips the header. Any failure leaves the previous
     * default intact. The answer is held (coordinator_held_poll). */
    ovlstore_sd_desc_t d;
    uint32_t rm_status, live_rm;
    int rc;
    const char *why;

    if (claim_locked(resp, "commit")) {
        return;                    /* the claim lock first: that peer learns nothing more */
    }
    why = mps3_swap_refusal();
    if (why != 0) {
        set_err(resp, why);        /* "identity lock: ..." (Linux) */
        return;
    }
    if (strcmp(req->src, "tcp") != 0) {
        set_err(resp, "bad args"); /* v0.13: the re-push is TCP 6910 only */
        return;
    }
    if (!swap_fsm_idle()) {
        set_err(resp, "store busy");
        return;
    }
    /* "Only what is running can be persisted": the LIVE DFXCTL.RM_ID, and only
     * while RM_STATUS says it is valid. ~rm_id can never match the request. */
    rm_status = mps3_reg_read32(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS);
    live_rm = (rm_status & DFXCTL_RM_STATUS_RM_ID_VALID)
                  ? mps3_reg_read32(MPS3_DFXCTL_BASE, DFXCTL_RM_ID) : ~req->rm_id;
    d.rm_id     = req->rm_id;
    d.static_id = req->static_id;
    d.clear_len = req->clear_len;
    d.clear_crc = req->clear_crc;
    d.part_len  = req->part_len;
    d.part_crc  = req->part_crc;
    rc = overlay_store_commit_begin(&d, g_shell_state.static_id, live_rm);
    if (rc != OVLSD_OK) {
        set_err(resp, overlay_store_err_name(rc));
        return;
    }
    resp->ok = 1;
    s_defer = 1;
}

/* `usd` (v0.13): the user-microSD store's status (never held -- it answers
 * during a swap, including the power-on load), or an action: `rescan` answers
 * at once; `format` / `clear` start a store job and hold the answer. */
void coordinator_handle_usd(const mps3_ctrl_request_t *req, mps3_ctrl_response_t *resp)
{
    int rc;

    if (req->action[0] == '\0') {
        overlay_store_usd_status(&resp->usd);
        resp->ok = 1;
        return;
    }
    if (claim_locked(resp, "usd")) {
        return;                    /* every action (format, clear, rescan); status is open */
    }
    rc = overlay_store_usd_action(req->action, req->confirm);
    if (rc == OVLSD_BUSY) {
        resp->ok = 1;
        s_defer = 1;
        return;
    }
    if (rc != OVLSD_OK) {
        set_err(resp, overlay_store_err_name(rc));
        return;
    }
    overlay_store_usd_status(&resp->usd);   /* for the resulting state */
    resp->usd.action_reply = 1;
    resp->ok = 1;
}

int coordinator_held_poll(mps3_ctrl_response_t *resp)
{
    int rc;
    char slot = 0;

    switch (s_held_op) {
    case MPS3_OP_SWAP:
        if (!swap_fsm_last_result()->valid) {
            return 0;
        }
        coordinator_swap_final_response(resp);   /* clears s_held_op */
        s_held_op = MPS3_OP_UNKNOWN;
        return 1;
    case MPS3_OP_COMMIT:
        rc = overlay_store_commit_poll(&slot);
        if (rc == OVLSD_BUSY) {
            return 0;
        }
        memset(resp, 0, sizeof(*resp));
        resp->op = MPS3_OP_COMMIT;
        if (rc == OVLSD_OK && (slot == 'A' || slot == 'B')) {
            resp->ok = 1;
            resp->slot = slot;
        } else {
            set_err(resp, overlay_store_err_name(rc == OVLSD_OK ? OVLSD_EIO : rc));
        }
        s_held_op = MPS3_OP_UNKNOWN;
        return 1;
    case MPS3_OP_USD:
        rc = overlay_store_usd_action_poll();
        if (rc == OVLSD_BUSY) {
            return 0;
        }
        memset(resp, 0, sizeof(*resp));
        resp->op = MPS3_OP_USD;
        if (rc == OVLSD_OK) {
            overlay_store_usd_status(&resp->usd);
            resp->usd.action_reply = 1;
            resp->ok = 1;
        } else {
            set_err(resp, overlay_store_err_name(rc));
        }
        s_held_op = MPS3_OP_UNKNOWN;
        return 1;
    default:
        return 0;
    }
}

int coordinator_held_pending(void)
{
    return s_held_op != MPS3_OP_UNKNOWN;
}

void coordinator_handle_telemetry(const mps3_ctrl_request_t *req, mps3_ctrl_response_t *resp)
{
    (void)req;
    /* ======================================================================
     * THERE IS NO POWER SENSOR. This verb FAILS, on purpose, always.
     * ======================================================================
     * This handler used to read TELEM.BUS_MV / TELEM.CURR_UA and report them
     * as {"mv":..,"ma":..}. Those registers read ZERO, always and forever --
     * not "zero right now", but zero by construction, at four independent
     * layers, any one of which alone is fatal:
     *
     *   1. The TELEM block's sample inputs are TIED TO GROUND in the block
     *      design: fpga/shell/bd/shell_bd.tcl wires gnd_ina228_32 -> the
     *      ina228_bus_mv_i / ina228_curr_ua_i / ina228_power_mw_i buses and
     *      gnd_ina228_1 -> ina228_sample_valid_i / i2c_err_i / alarm_i.
     *   2. The INA228 I2C engine that would drive them was never written --
     *      fpga/shell/ip/telem/README.md: "SEAMED (follow-up module, NOT yet
     *      written)". Its own README is explicit that with no engine attached
     *      "readings stay 0 ... visibly 'no data source', never plausible
     *      garbage". Emitting those zeroes as mv/ma defeated exactly that.
     *   3. The I2C pads are not even brought to the top level:
     *      fpga/shell/shell_top.sv records the INA228 bus as MCC-owned on
     *      MPS3, so no fabric path to the part exists to be written later.
     *   4. The MPS3 MCC console -- the one other conceivable path -- REFUSES
     *      voltage reads on this board: `CFG R V <dev>` answers "ERROR:
     *      Unable to perform requested function" for every device (confirmed
     *      on the real board, 2026-07-14).
     *
     * A plausible-looking zero is the worst possible telemetry: it is
     * indistinguishable from a genuine 0 mV / 0 mA measurement, and a host
     * charting it sees a flat line, not a missing sensor. So: fail loudly,
     * using the protocol's uniform error convention. Callers get ok:false and
     * a reason; the mv/ma keys are ABSENT from the wire, not zeroed.
     *
     * If an INA228 engine is ever written and un-grounded, this is the one
     * function to revisit -- and the codec will force the issue, because
     * mps3_ctrl_encode_response() has no ok=true telemetry shape to encode.
     *
     * `lockup` is a DIFFERENT story and is NOT suppressed. It is a real
     * partition pin (DFXCTL.RM_STATUS.dut_lockup) with a real value, so it
     * still rides on the wire, raw. The shell does not editorialise: it
     * reports the pin. Several RM wrappers hard-tie dut_lockup to 0 and so
     * can never assert it -- that is a fact about the RM, and deciding which
     * RMs the pin is meaningful for belongs to the host's per-RM catalogue
     * (docs/contracts/net-protocol.md "telemetry"), not to this handler.
     * ====================================================================== */
    set_err(resp, "no power sensor");   /* sets ok = 0 */
    resp->lockup = (mps3_reg_read32(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS) & DFXCTL_RM_STATUS_DUT_LOCKUP) != 0;
}

/* Maps a macgen `inject` fault name to its GENCHK.INJECT one-hot bit. "none"
 * arms nothing (bits=0, clearing any previously-armed fault). Any other name
 * is rejected (return -1) — the fault set is closed, fail closed like
 * link.event. Kept a table so the accepted set is one obvious list that
 * mirrors shell-regmap.md's INJECT field order exactly. */
static int macgen_inject_bits(const char *inject, uint32_t *bits)
{
    static const struct { const char *name; uint32_t bit; } tbl[] = {
        { "none",    0u                    },
        { "bad_fcs", GENCHK_INJECT_BAD_FCS },
        { "runt",    GENCHK_INJECT_RUNT    },
        { "giant",   GENCHK_INJECT_GIANT   },
        { "ifg",     GENCHK_INJECT_IFG     },
        { "dribble", GENCHK_INJECT_DRIBBLE },
    };
    for (size_t i = 0; i < sizeof(tbl) / sizeof(tbl[0]); i++) {
        if (strcmp(inject, tbl[i].name) == 0) {
            *bits = tbl[i].bit;
            return 0;
        }
    }
    return -1;
}

void coordinator_handle_macgen(const mps3_ctrl_request_t *req, mps3_ctrl_response_t *resp)
{
    /* {"op":"macgen","gen":true,"chk":true,"inject":"bad_fcs"} — the
     * MAC-in-operation control verb (net-protocol.md "MAC gen/checker
     * control", shell-regmap.md GENCHK @ 0x44A6). Drive CTRL.gen_en/chk_en +
     * the INJECT one-hot, then read back the three RO counters. Register
     * pokes are immediate, so unlike `swap` the response is NOT held. */
    uint32_t inject_bits;
    if (macgen_inject_bits(req->inject, &inject_bits) != 0) {
        set_err(resp, "bad inject");
        return;
    }

    uint32_t ctrl = 0;
    if (req->gen) {
        ctrl |= GENCHK_CTRL_GEN_EN;
    }
    if (req->chk) {
        ctrl |= GENCHK_CTRL_CHK_EN;
    }
    mps3_reg_write32(MPS3_GENCHK_BASE, GENCHK_CTRL, ctrl);
    mps3_reg_write32(MPS3_GENCHK_BASE, GENCHK_INJECT, inject_bits);

    /* Counters are RTL-owned and CLEAR-ON-ENABLE-RISE (shell-regmap.md v0.4:
     * gen_checker zeroes TX/RX/ERR_CNT on a gen_en/chk_en 0->1 edge) — monotonic
     * only *within* an enabled session, not free-running across enable toggles.
     * The coordinator only reads them (no counter-clear register); the host
     * enables once and reasons about deltas within that session
     * (net-protocol.md "Counter semantics"). */
    resp->ok = 1;
    resp->tx_cnt  = mps3_reg_read32(MPS3_GENCHK_BASE, GENCHK_TX_CNT);
    resp->rx_cnt  = mps3_reg_read32(MPS3_GENCHK_BASE, GENCHK_RX_CNT);
    resp->err_cnt = mps3_reg_read32(MPS3_GENCHK_BASE, GENCHK_ERR_CNT);
}

void coordinator_handle_diag(const mps3_ctrl_request_t *req, mps3_ctrl_response_t *resp)
{
    (void)req;
    /* {"op":"diag"} — read back the always-on diagnostic mailbox (diag.h): the
     * RX-recovery / ICAP / receive-progress + lwIP-window/pbuf counters the
     * superloop republishes every pass. This is the LIVE (idle-time) view; the
     * SAME struct is pinned at a fixed DMEM address for JTAG-MDM readout during a
     * swap, when the 6900 control channel is parked (diag.h). Pure readback, so
     * the response is not held (unlike swap). */
    /* WHOLE-STRUCT snapshot, straight into the response. This was 14 field
     * assignments that mapped a 25-counter mailbox onto 14 wire members — the
     * same field-list-by-hand scheme that already cost us tx_last_status
     * (published, JTAG-visible, and silently absent from the 6900 verb). The
     * wire key spellings, including the frozen "grants_sent" =
     * win_windows_drained and "grant_fails" = win_grant_send_fails, now live
     * once, in MPS3_DIAG_FIELDS (diag.h), and mps3_ctrl_encode_response()
     * walks them. Adding a counter needs no edit here. */
    mps3_diag_snapshot(&resp->diag);
    resp->ok = 1;
}

/* ==========================================================================
 * `dutrx` — read one chunk of a frame the DUT transmitted (net-protocol v0.10)
 * ==========================================================================
 *
 * IS THE BLOCK IN THIS FABRIC? DUTEGR (0x44B2_0000) is real RTL and is in the
 * shell BD, so it lands at the NEXT mint -- it is not in the fielded bitstream,
 * and a firmware built from this tree can be baked into that bitstream by
 * updatemem with no re-mint at all. So the presence of the slave is a
 * COMPILE-TIME fact about which bitstream this image is destined for, exactly
 * like CLCD_KVM_PRESENT, and it is gated the same way.
 *
 * Answering ok:true with zeroes on a shell that has no capture block would make
 * "this fabric cannot capture" indistinguishable from "the DUT sent nothing" --
 * the reading-shaped lie v0.6 took out of `telemetry`. Decline instead. */
#if defined(MPS3_HAS_DUT_EGRESS)
#define DUTEGR_PRESENT 1
#else
#define DUTEGR_PRESENT 0
#endif

#if DUTEGR_PRESENT
/* DUTEGR bit fields. platform_regs.h generates this block's BASE and its eight
 * register OFFSETS from the RTL's own IDX_* decode, and deliberately generates
 * NO bit-field defines -- those live beside the prose that explains them. These
 * are spelled from fpga/shell/ip/dut_egress/dut_egress.sv's read mux
 * (rd_status / rd_frame_len / rd_data) and docs/contracts/shell-regmap.md's
 * DUTEGR section, which agree because the doc is generated from the RTL.
 *
 * They belong in platform_regs.h with every other block's bit fields; they are
 * here while this handler is their only consumer, so one file owns the
 * DUT-egress read path end to end. Moving them is a pure relocation. */
#define DUTEGR_STATUS_FRAME_RDY  (1u << 0)  /* a WHOLE frame is at the head   */
#define DUTEGR_STATUS_OVF        (1u << 4)  /* sticky: something was dropped  */
#define DUTEGR_STATUS_DESYNC     (1u << 6)  /* sticky: the two end-of-frame
                                             * records disagreed              */
#define DUTEGR_DATA_BYTE_MASK    0x000000FFu
#define DUTEGR_DATA_VALID        (1u << 8)  /* 0 => the pop returned nothing  */
#define DUTEGR_DATA_LAST         (1u << 9)  /* this byte ends the frame       */
#endif /* DUTEGR_PRESENT */

static void coordinator_handle_dutrx(const mps3_ctrl_request_t *req,
                                     mps3_ctrl_response_t *resp)
{
    (void)req;
#if DUTEGR_PRESENT
    /* {"op":"dutrx"} — pop up to MPS3_DUTRX_CHUNK_MAX bytes of the frame at the
     * head of the capture FIFO. No arguments: the FIFO is its own cursor (DATA
     * is a DESTRUCTIVE read), so there is nothing for the host to track and no
     * per-connection state on the shell. Register reads are immediate, so the
     * response is SEND-NOW, never held like `swap`.
     *
     * Store-and-forward means FRAME_RDY only rises once every byte of the head
     * frame is committed, so the length cannot change under this loop and a
     * torn frame can never be read. That is what lets the chunking be
     * stateless: the frame the next request continues is the same frame. */
    uint32_t status = mps3_reg_read32(MPS3_DUTEGR_BASE, DUTEGR_STATUS);

    resp->ok = 1;

    if (status & DUTEGR_STATUS_FRAME_RDY) {
        uint32_t flen   = mps3_reg_read32(MPS3_DUTEGR_BASE, DUTEGR_FRAME_LEN);
        uint32_t total  = (flen >> 16) & 0xFFFFu;   /* the head frame's length */
        uint32_t remain = flen & 0xFFFFu;           /* bytes of it still here  */
        uint32_t want   = (remain > MPS3_DUTRX_CHUNK_MAX)
                              ? (uint32_t)MPS3_DUTRX_CHUNK_MAX : remain;
        uint32_t got    = 0;
        int      last   = 0;

        /* TWO bounds, deliberately. `want` is already clamped, but `remain`
         * came out of a HARDWARE register and dutrx_data is a fixed 256-byte
         * buffer -- so the buffer's own bound is restated here rather than
         * inferred from the clamp three lines up. Removing the clamp then
         * produces a short chunk (a caught test failure), not a stack smash. */
        while (got < want && got < (uint32_t)MPS3_DUTRX_CHUNK_MAX) {
            uint32_t d = mps3_reg_read32(MPS3_DUTEGR_BASE, DUTEGR_DATA);
            if ((d & DUTEGR_DATA_VALID) == 0u) {
                /* The pop returned nothing although FRAME_LEN said bytes were
                 * left. Stop AT ONCE rather than padding the chunk out with the
                 * zeroes an empty DATA read returns: a short chunk is visible
                 * (n < the length implies), invented bytes are not. */
                break;
            }
            resp->dutrx_data[got++] = (uint8_t)(d & DUTEGR_DATA_BYTE_MASK);
            last = (d & DUTEGR_DATA_LAST) != 0;
        }

        resp->dutrx_len  = (uint16_t)total;
        resp->dutrx_off  = (uint16_t)(total - remain); /* delivered before this */
        resp->dutrx_n    = (uint16_t)got;
        resp->dutrx_more = (remain - got) > 0u;
        resp->dutrx_last = last;
    }

    /* STATUS again, AFTER the pops: DESYNC latches on the pop whose stored
     * end-of-frame bit disagrees with the descriptor's length, so reading it
     * beforehand would report this chunk's disagreement one request late. LEVEL
     * likewise: the head frame's descriptor is retired by the pop of its LAST
     * byte, so `frames` read here is what is still waiting AFTER this chunk. */
    status = mps3_reg_read32(MPS3_DUTEGR_BASE, DUTEGR_STATUS);
    resp->dutrx_ovf    = (status & DUTEGR_STATUS_OVF) != 0;
    resp->dutrx_desync = (status & DUTEGR_STATUS_DESYNC) != 0;
    resp->dutrx_frames =
        (uint16_t)((mps3_reg_read32(MPS3_DUTEGR_BASE, DUTEGR_LEVEL) >> 16) & 0xFFFFu);

    /* The drop counters ride on EVERY reply, empty ones included. The block
     * cannot backpressure the bridge (that parks the whole bridge, killing the
     * DUT's ingress scoring and the LAN9220 uplink with it), so it drops -- and
     * RX_FRAMES + DROP_FULL + DROP_GIANT == frames presented is the invariant
     * that keeps those drops from being silent. A host that reads frames
     * without reading these is counting only what survived. */
    resp->dutrx_rx_frames  = mps3_reg_read32(MPS3_DUTEGR_BASE, DUTEGR_RX_FRAMES);
    resp->dutrx_drop_full  = mps3_reg_read32(MPS3_DUTEGR_BASE, DUTEGR_DROP_FULL);
    resp->dutrx_drop_giant = mps3_reg_read32(MPS3_DUTEGR_BASE, DUTEGR_DROP_GIANT);
#else
    /* No 0x44B2 slave in this bitstream (see the presence note above): the verb
     * decodes, and the handler declines rather than reading a DECERR'ing void
     * and reporting its zeroes as an empty FIFO. Same shape, same reasoning as
     * `display` on a KVM-less shell. */
    set_err(resp, "dut_egress not present");
#endif
}

#if CLCD_KVM_PRESENT
/* Copy a "harness"/"dut" owner string from a STATUS word into resp->owner. */
static void set_owner_from_status(mps3_ctrl_response_t *resp, uint32_t status)
{
    const char *name = clcd_kvm_owner_is_dut(status) ? "dut" : "harness";
    strncpy(resp->owner, name, sizeof(resp->owner) - 1);
    resp->owner[sizeof(resp->owner) - 1] = '\0';
}
#endif

void coordinator_handle_display(const mps3_ctrl_request_t *req, mps3_ctrl_response_t *resp)
{
    /* {"op":"display","owner":"dut|harness|toggle|query"} — the REMOTE arm of
     * the CLCD KVM's three converging request sources (button / local CSR /
     * network). It drives the same frozen CLCDKVM.CTRL.src_sel (+ src_sel_we)
     * via the button-safe firmware/clcd_kvm helpers — never an open-coded RMW —
     * so the KVM's forced-revert safety (a DFX swap overrides ANY source)
     * covers the remote path for free. Register pokes are immediate, so unlike
     * `swap` the response is SEND-NOW, not held.
     *
     * "query" is read-only: it reports STATUS.owner without touching CTRL, so a
     * `GET /display` / display_owner() never moves ownership. */
#if CLCD_KVM_PRESENT
    unsigned want;
    if (strcmp(req->owner, "query") == 0) {
        /* Read-only: report the committed owner, drive nothing. */
        resp->ok = 1;
        set_owner_from_status(resp, clcd_kvm_status());
        return;
    } else if (strcmp(req->owner, "harness") == 0) {
        want = CLCDKVM_OWNER_HARNESS;
    } else if (strcmp(req->owner, "dut") == 0) {
        want = CLCDKVM_OWNER_DUT;
    } else if (strcmp(req->owner, "toggle") == 0) {
        /* Flip the *requested* target, mirroring the hardware button (README
         * §7): toggle against tgt_owner, not the committed owner, so a second
         * toggle mid-handover cancels the first and lands where it started. */
        uint32_t st = clcd_kvm_status();
        unsigned cur_tgt = (st & CLCDKVM_STATUS_TGT_OWNER) ? CLCDKVM_OWNER_DUT
                                                           : CLCDKVM_OWNER_HARNESS;
        want = (cur_tgt == CLCDKVM_OWNER_DUT) ? CLCDKVM_OWNER_HARNESS
                                              : CLCDKVM_OWNER_DUT;
    } else {
        set_err(resp, "bad owner");
        return;
    }

    clcd_kvm_request_owner(want);

    /* Report the COMMITTED owner (STATUS.owner) — what is actually reaching the
     * pads. A flip to the other side takes a hardware handover (drain → reset →
     * settle → grant, ~7–9 ms) to commit, so immediately after a request this
     * can still read the OUTGOING owner. That is the honest state; a client
     * confirms the landing with a follow-up display_owner() ("query"). */
    resp->ok = 1;
    set_owner_from_status(resp, clcd_kvm_status());
#else
    (void)req;
    /* This bitstream has no AXI slave at 0x44AD (MPS3_HAS_CLCD_KVM off): the
     * KVM lands in the Wave-4 static rebuild. The verb still DECODES cleanly
     * (net_proto.c) — we fail loudly here rather than issue a bus access into a
     * DECERR'ing void. Decode-works / handler-declines is the documented
     * OFF-build behaviour (net-protocol.md "display"). */
    set_err(resp, "clcd_kvm not present");
#endif
}


/* LMB size this image was LINKED for, in KiB. -DMPS3_LMB_KB comes from the ONE
 * knob in firmware/platform/Makefile (LMB_KB), so the number reported here can
 * never drift from the number the linker script was generated with. The
 * fallback exists for the host-gcc test binaries and for any build that does
 * not pass it, and MUST track that Makefile's default -- it is the value a
 * plain `make elf` links, so a mismatch here would make the fallback itself a
 * lie. The stake is real: the LMB address decode ALIASES, so a 1 MiB image on
 * a 512 KiB shell reads the wrong diag mailbox and says nothing about it. */
#ifndef MPS3_LMB_KB
#define MPS3_LMB_KB 1024u
#endif

/* Compile-time feature set -> MPS3_FEATURE_* bitmask. The ONLY place in the
 * firmware where these five -D flags are read as a SET, which is the point: the
 * fielded combination used to exist solely as five literals in a mint script,
 * so a running board could not be asked what it was built with. Each line
 * mirrors one knob in firmware/platform/Makefile (PRODUCT=1 sets all five). */
static uint32_t version_features(void)
{
    uint32_t f = 0u;
#ifdef MPS3_HAS_CLCD
    f |= MPS3_FEATURE_CLCD;
#endif
#ifdef MPS3_HAS_CLCD_KVM
    f |= MPS3_FEATURE_CLCD_KVM;
#endif
#ifdef MPS3_HAS_TOUCH
    f |= MPS3_FEATURE_TOUCH;
#endif
    /* platform_regs.h DEFINES MPS3_HWICAP_FIFO as 0 when the knob is unset, so
     * this one must test the VALUE, not merely definedness -- #ifdef would
     * report "hwicap_fifo" on every LITE build, i.e. exactly backwards for the
     * mismatch this verb exists to expose. */
#if defined(MPS3_HWICAP_FIFO) && (MPS3_HWICAP_FIFO)
    f |= MPS3_FEATURE_HWICAP_FIFO;
#endif
#ifdef MPS3_CFG_AGENT_WINDOWED
    f |= MPS3_FEATURE_WINDOWED;
#endif
    /* ---- v0.11, appended (net_proto.h). ---------------------------------- */
#if DUTEGR_PRESENT
    f |= MPS3_FEATURE_DUT_EGRESS;
#endif
    /* jtag_server (6921) is initialised unconditionally by coordinator_init()
     * and polled by every superloop -- compiled in by construction, which is
     * exactly what the bit asserts. */
    f |= MPS3_FEATURE_JTAG_SERVER;
    /* XVC 2542's target, from the same -D the xvc_server build reads
     * (firmware/platform/Makefile XVC_TARGET). swdbb is jtag_bb's page under its
     * retired name, so it reports as jtagbb: what matters to a client is WHICH
     * HARDWARE port 2542 reaches, and those two reach the same one. */
#if defined(MPS3_XVC_TARGET_JTAGBB) || defined(MPS3_XVC_TARGET_SWDBB)
    f |= MPS3_FEATURE_XVC_JTAGBB;
#else
    f |= MPS3_FEATURE_XVC_DBGBR;
#endif
    f |= MPS3_FEATURE_STATS;
    f |= MPS3_FEATURE_LOG;
    f |= MPS3_FEATURE_REBOOT;
#ifdef MPS3_HAS_TOUCH
    f |= MPS3_FEATURE_TOUCH_CAL;
#endif
    /* v0.13 (D13): the `usd` verb and the re-push `commit`. Compiled into every
     * image -- a fabric without usd_spi answers state "no_hw", which is the
     * verb working, not the verb missing. */
    f |= MPS3_FEATURE_USD;
    /* v0.14 amendments (2026-09-26): services that follow the LINKED PROVIDER
     * (weak seams, 0 on bare metal), not a build flag. */
    if (mps3_slot_supported()) {
        f |= MPS3_FEATURE_SLOT;
    }
    if (mps3_debug_lock_supported()) {
        f |= MPS3_FEATURE_XVC_LOCK;
    }
    return f;
}

void coordinator_handle_version(const mps3_ctrl_request_t *req, mps3_ctrl_response_t *resp)
{
    (void)req;
    /* {"op":"version"} -- WHAT IMAGE IS THIS? (net-protocol.md v0.8)
     *
     * `ping` answers "which FABRIC is this" (static_id) and "what is in the RP"
     * (rm_id). Neither says which FIRMWARE is running, and the two axes are
     * orthogonal on purpose: one static_id serves many harness releases,
     * because a firmware-only bump re-bakes the .bit via updatemem without
     * touching the static routing (mps3_version.h, "This is NOT static_id").
     * So a board could report the right static_id, pass every gate, and be
     * carrying an image built with the wrong flags -- the exact failure the
     * HWICAP LITE/FIFO mismatch produced. This verb closes that.
     *
     * Every value but one is a pure read of a build-time constant (the
     * weak/strong mps3_harness_*() seam + the -D flags). The exception is
     * usr_access: a single read of the USRACC CSR (0x44B3_0000), which answers
     * on any board whose AXI-Lite window is alive and reports "no sensor"
     * (usr_access null) where it is not. The response is SEND-NOW (never held
     * like swap). */
    resp->ok = 1;
    strncpy(resp->harness, mps3_harness_version_str(), sizeof(resp->harness) - 1);
    resp->harness[sizeof(resp->harness) - 1] = '\0';
    format_id_hex(resp->ver32, sizeof(resp->ver32), mps3_harness_version());
    strncpy(resp->sha, mps3_harness_git_sha(), sizeof(resp->sha) - 1);
    resp->sha[sizeof(resp->sha) - 1] = '\0';
    resp->dirty    = mps3_harness_dirty() ? 1 : 0;
    resp->lmb_kb   = (uint32_t)MPS3_LMB_KB;
    resp->features = version_features();
    mps3_fabric_usr_access_str(resp->usr_access, sizeof(resp->usr_access));
}


/* ==========================================================================
 * v0.11 verbs: stats / log / touch_cal / reboot
 * ========================================================================== */

/* WEAK `stats` link/MAC seam (coordinator.h). What a build with no LAN9220
 * driver linked honestly knows: nothing -- link down, speed unknown, MAC 0.
 * firmware/platform/src/main.c overrides it on the target. */
__attribute__((weak)) void mps3_stats_net(mps3_stats_net_t *out)
{
    memset(out, 0, sizeof(*out));
}

/* WEAK `stats.dut_mhz` seam (coordinator.h): the bare-metal answer, the preset
 * this boot last programmed. mps3-harnessd reads the fabric instead. */
__attribute__((weak)) uint32_t mps3_dut_clk_mhz(uint32_t programmed_mhz)
{
    return programmed_mhz;
}

static void copy_str(char *dst, size_t dst_sz, const char *src)
{
    strncpy(dst, src ? src : "", dst_sz - 1u);
    dst[dst_sz - 1u] = '\0';
}

static void coordinator_handle_stats(const mps3_ctrl_request_t *req,
                                     mps3_ctrl_response_t *resp)
{
    (void)req;
    /* {"op":"stats"} -- the board's state in ONE line, in the exact key shape
     * fpgahub's shell_6900 provider already probes for (_from_stats_verb), so a
     * single round-trip replaces its ping + diag (+ telemetry) sweep. SEND-NOW:
     * every value is a register read or a firmware variable.
     *
     * Where each key comes from (the host test pins every one of these):
     *   up_ms      mps3_sys_now_ms()            -- resets on any shell restart,
     *                                              which is what makes it the
     *                                              reboot witness
     *   sid / rm   g_shell_state.static_id / current_rm_id (the same VERIFIED
     *              identities `ping` reports, not a transient register)
     *   rm_ok      DFXCTL.RM_STATUS[0] rm_id_valid
     *   lock       DFXCTL.RM_STATUS[1] dut_lockup (RAW; per-RM meaning is the
     *              host catalogue's call, as for `telemetry`)
     *   clk_sel    CLKRST.DUT_CLK_SEL[7:0]      -- the last preset id written
     *   mmcm       CLKRST.STATUS[0] mmcm_locked
     *   clk_alive  CLKRST.STATUS[1] dut_clk_alive
     *   dut_rst    CLKRST.RESET_CTRL[0] dut_resetn -- 1 = RELEASED
     *   rp_rst     NOT DFXCTL.STATUS[1] rp_in_reset -- 1 = RELEASED (observed,
     *              not the control bit: a failed swap parks the RP in reset and
     *              this is how a host sees it without JTAG)
     *   decpl      DFXCTL.STATUS[0] decoupled
     *   link/spd/fdx/mac  the mps3_stats_net() seam (LAN9220 PHY + our MAC)
     *   swap       swap_fsm_state_name(swap_fsm_state())
     *   swap_ok    swap_fsm_last_result()->ok (false until a swap completes)
     *   swap_n     swap_fsm_completed()
     *   icap       swap_fsm_icap_bytes()
     *   rxdrop / txerr  the diag mailbox's rx_drops / tx_errors
     *   swap_err   swap_fsm_fail_tag()          (extra)
     *   clr_ok     g_current_rm_clearing.valid  (extra) -- whether the NEXT swap
     *              can clear the resident RM
     *   dut_mhz    mps3_dut_clk_mhz(): the preset last programmed on bare
     *              metal; clk_wiz_dut's register file under harnessd (extra)
     *   svc_max_us / svc_skipped  mps3_service_window_take() (extra) -- the
     *              worst superloop pass and the sick-service edges SINCE THE
     *              PREVIOUS `stats`, so a poller sees "starving now", not a
     *              since-boot high-water mark that only ever rises.
     *   touch_ok / touch_bus_lost / touch_recoveries  touch_health() (extra,
     *              TOUCH=1 builds only; omitted otherwise) -- is touch sampling,
     *              how often the bus-loss latch fired, how often the periodic
     *              re-init brought it back. On silicon 2026-09-24 the latch
     *              killed touch for hours and nothing on the wire said so. */
    mps3_stats_t *st = &resp->stats;
    mps3_stats_net_t net;
    mps3_diag_t d;
    const mps3_swap_result_t *r = swap_fsm_last_result();
    uint32_t rm_status = mps3_reg_read32(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS);
    uint32_t dfx_st    = mps3_reg_read32(MPS3_DFXCTL_BASE, DFXCTL_STATUS);
    uint32_t clk_st    = mps3_reg_read32(MPS3_CLKRST_BASE, CLKRST_STATUS);
    uint32_t rst_ctrl  = mps3_reg_read32(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL);

    memset(&net, 0, sizeof(net));
    mps3_stats_net(&net);
    mps3_diag_snapshot(&d);

    st->up_ms     = mps3_sys_now_ms();
    st->sid       = g_shell_state.static_id;
    st->rm        = g_shell_state.current_rm_id;
    st->rm_ok     = (rm_status & DFXCTL_RM_STATUS_RM_ID_VALID) != 0u;
    st->lock      = (rm_status & DFXCTL_RM_STATUS_DUT_LOCKUP) != 0u;
    st->clk_sel   = mps3_reg_read32(MPS3_CLKRST_BASE, CLKRST_DUT_CLK_SEL)
                    & CLKRST_DUT_CLK_SEL_MASK;
    st->mmcm      = (clk_st & CLKRST_STATUS_MMCM_LOCKED) != 0u;
    st->clk_alive = (clk_st & CLKRST_STATUS_DUT_CLK_ALIVE) != 0u;
    st->dut_rst   = (rst_ctrl & CLKRST_RESET_CTRL_DUT_RESETN) != 0u;
    st->rp_rst    = (dfx_st & DFXCTL_STATUS_RP_IN_RESET) == 0u;
    st->decpl     = (dfx_st & DFXCTL_STATUS_DECOUPLED) != 0u;
    st->link      = net.link ? 1 : 0;
    st->spd       = net.link ? net.spd : 0u;
    st->fdx       = (net.link && net.fdx) ? 1 : 0;
    memcpy(st->mac, net.mac, sizeof(st->mac));
    copy_str(st->swap, sizeof(st->swap), swap_fsm_state_name(swap_fsm_state()));
    st->swap_ok   = (r->valid && r->ok) ? 1 : 0;
    st->swap_n    = swap_fsm_completed();
    st->icap      = swap_fsm_icap_bytes();
    st->rxdrop    = d.rx_drop_frames;
    st->txerr     = d.tx_errors;
    copy_str(st->swap_err, sizeof(st->swap_err), swap_fsm_fail_tag());
    st->clr_ok    = g_current_rm_clearing.valid ? 1 : 0;
    st->dut_mhz   = mps3_dut_clk_mhz(s_dut_mhz);   /* weak: s_dut_mhz itself */
    mps3_service_window_take(&st->svc_max_us, &st->svc_skipped);
#ifdef MPS3_HAS_TOUCH
    {
        touch_health_t th;
        touch_health(&th);
        st->touch_present    = 1;
        st->touch_ok         = th.ok ? 1 : 0;
        st->touch_bus_lost   = th.bus_lost;
        st->touch_recoveries = th.recoveries;
    }
#endif
    resp->ok = 1;
}

static void coordinator_handle_log(const mps3_ctrl_request_t *req,
                                   mps3_ctrl_response_t *resp)
{
    /* {"op":"log","off":N} -- one chunk of the console ring (log_ring.h),
     * starting at stream offset `off` (absent = 0 = the oldest retained byte).
     * Non-destructive: the shell keeps no reader state, so two clients never
     * steal each other's bytes. `off` in the reply is where the chunk ACTUALLY
     * starts -- later than asked when the asked-for bytes were overwritten --
     * and `dropped` is the since-boot overrun count, riding every reply like
     * dutrx's drop counters: a reader that ignores it is reading a log it
     * believes is complete. SEND-NOW. */
    uint32_t got_off = 0u, n = 0u;
    int more = 0;
    (void)mps3_log_read((uint32_t)req->off, resp->dutrx_data,
                        (uint32_t)MPS3_LOG_CHUNK_MAX, &got_off, &n, &more);
    resp->log_off     = got_off;
    resp->log_n       = (uint16_t)n;
    resp->log_more    = more;
    resp->log_dropped = mps3_log_dropped();
    resp->ok = 1;
}

static void coordinator_handle_touch_cal(const mps3_ctrl_request_t *req,
                                         mps3_ctrl_response_t *resp)
{
#ifdef MPS3_HAS_TOUCH
    /* {"op":"touch_cal","act":"get|set|raw|default", ...} -- read or replace the
     * STMPE811 raw->pixel calibration (touch.h touch_calib_t), and read the last
     * raw sample for a three-point capture. RAM only: a reboot returns to
     * TOUCH_CALIB_DEFAULT, which is the header constant a capture is pasted
     * into. SEND-NOW. */
    touch_calib_t c;

    if (strcmp(req->act, "raw") == 0) {
        uint16_t rx = 0u, ry = 0u, rz = 0u, x = 0u, y = 0u;
        uint32_t seen = 0u;
        touch_raw_sample(&rx, &ry, &rz, &seen);
        /* The same sample through the CURRENT calibration, so a host can check
         * a fit without re-implementing touch_map_raw() and its 180 flip. */
        touch_map_raw(rx, ry, &x, &y);
        resp->tc_kind  = MPS3_TC_RAW;
        resp->tc_raw_x = rx;
        resp->tc_raw_y = ry;
        resp->tc_raw_z = rz;
        resp->tc_seen  = seen;
        resp->tc_x     = x;
        resp->tc_y     = y;
        resp->ok = 1;
        return;
    }
    if (strcmp(req->act, "set") == 0) {
        const char *why;
        /* Range-check in int32 space BEFORE narrowing shift to uint8_t: a shift
         * of 256 must be rejected, not wrapped to 0. */
        if (req->cal[MPS3_CAL_SHIFT] < 0 ||
            req->cal[MPS3_CAL_SHIFT] > TOUCH_CAL_SHIFT_MAX) {
            set_err(resp, "bad shift");
            return;
        }
        c.ax = req->cal[MPS3_CAL_AX];
        c.bx = req->cal[MPS3_CAL_BX];
        c.cx = req->cal[MPS3_CAL_CX];
        c.ay = req->cal[MPS3_CAL_AY];
        c.by = req->cal[MPS3_CAL_BY];
        c.cy = req->cal[MPS3_CAL_CY];
        c.shift = (uint8_t)req->cal[MPS3_CAL_SHIFT];
        why = touch_calib_invalid_reason(&c);
        if (why != 0 || touch_set_calibration(&c) != 0) {
            set_err(resp, why ? why : "rejected");
            return;
        }
    } else if (strcmp(req->act, "default") == 0) {
        const touch_calib_t def = TOUCH_CALIB_DEFAULT;
        if (touch_set_calibration(&def) != 0) {
            set_err(resp, "default rejected");   /* a bad header constant */
            return;
        }
    } else if (strcmp(req->act, "get") != 0) {
        set_err(resp, "bad act");
        return;
    }
    /* get, and the echo after set/default: what is IN FORCE now. */
    touch_get_calibration(&c);
    resp->tc_kind = MPS3_TC_COEFFS;
    resp->tc_cal[MPS3_CAL_AX]    = c.ax;
    resp->tc_cal[MPS3_CAL_BX]    = c.bx;
    resp->tc_cal[MPS3_CAL_CX]    = c.cx;
    resp->tc_cal[MPS3_CAL_AY]    = c.ay;
    resp->tc_cal[MPS3_CAL_BY]    = c.by;
    resp->tc_cal[MPS3_CAL_CY]    = c.cy;
    resp->tc_cal[MPS3_CAL_SHIFT] = (int32_t)c.shift;
    resp->ok = 1;
#else
    (void)req;
    /* No touch driver in this image (TOUCH=1 off): decline, like `display` on a
     * KVM-less build. Answering with a calibration no driver uses would be a
     * setting that does nothing. */
    set_err(resp, "touch not present");
#endif
}

/* ---- reboot ------------------------------------------------------------- */

enum { REBOOT_IDLE = 0, REBOOT_REQUESTED = 1, REBOOT_ARMED = 2 };
static int      s_reboot_state;
static uint32_t s_reboot_t0_ms;

int coordinator_reboot_state(void) { return s_reboot_state; }

/* Is there a watchdog behind 0x44B4_0000? Its TBR is a free-running timebase,
 * so two reads differ on a live block; an unmapped page reads the same 0 twice.
 * Bounded: at most a handful of AXI reads. */
static int wdog_present(void)
{
    uint32_t a = mps3_reg_read32(MPS3_WDOG_BASE, WDOG_TBR);
    for (int i = 0; i < 8; i++) {
        if (mps3_reg_read32(MPS3_WDOG_BASE, WDOG_TBR) != a) {
            return 1;
        }
    }
    return 0;
}

static void coordinator_handle_reboot(const mps3_ctrl_request_t *req,
                                      mps3_ctrl_response_t *resp)
{
    (void)req;
    /* {"op":"reboot"} -- warm-restart the shell firmware through the shell
     * watchdog (coordinator.h's reboot block has the mechanism). The reply is
     * SENT FIRST: this handler only records the request; the watchdog is armed
     * by coordinator_reboot_poll() MPS3_REBOOT_ARM_DELAY_MS later.
     *
     * REFUSED MID-SWAP ("EBUSY"): a watchdog reset during an ICAP write is safe
     * for the fabric (dfx_ctl clamps the boundary set-dominant on wdt_reset) but
     * it abandons a half-written RP that only the boot-time default load would
     * then recover. A host that wants that should wait for the swap's reply.
     *
     * REFUSED DURING A CARD JOB ("EBUSY", the mps3_card_job_active() seam; weak 0
     * on bare metal): a watchdog reset mid-write or mid-read-back on the user
     * microSD left the card wedged on B2 silicon. A host waits for `job.state`
     * (slot) or the held reply (commit / usd) and asks again.
     *
     * REFUSED WITHOUT A WATCHDOG: on a shell minted before WDOG existed the verb
     * would otherwise answer ok and then never reboot -- a promise-shaped lie. */
    uint32_t in_ms;
    if (swap_fsm_state() != SWAP_IDLE || mps3_card_job_active()) {
        set_err(resp, "EBUSY");
        return;
    }
    if (s_reboot_state == REBOOT_IDLE) {
        if (!wdog_present()) {
            set_err(resp, "no watchdog");
            return;
        }
        s_reboot_state = REBOOT_REQUESTED;
        s_reboot_t0_ms = mps3_sys_now_ms();
    }
    /* Upper bound from NOW: the remaining arm delay + two watchdog stages. A
     * repeated request while one is already pending answers the same way
     * (idempotent, never a second timer). */
    {
        uint32_t since = mps3_sys_now_ms() - s_reboot_t0_ms;
        uint32_t arm_left = (since < MPS3_REBOOT_ARM_DELAY_MS)
                                ? (MPS3_REBOOT_ARM_DELAY_MS - since) : 0u;
        in_ms = arm_left + 2u * MPS3_WDT_STAGE_MS;
    }
    resp->in_ms = in_ms;
    resp->ok = 1;
}

void coordinator_reboot_poll(void)
{
    if (s_reboot_state != REBOOT_REQUESTED) {
        return;
    }
    if ((uint32_t)(mps3_sys_now_ms() - s_reboot_t0_ms) < MPS3_REBOOT_ARM_DELAY_MS) {
        return;   /* let the reply leave the board first */
    }
    /* ARM. Kicks stop FIRST, so no pass can land between enabling the watchdog
     * and inhibiting the kick. Then enable both halves (PG128: EWDT1 in TWCSR0
     * AND EWDT2 in TWCSR1 must be set; writing 0 to TWCSR0.WDS is a no-op, it is
     * W1C). From here the first expiry sets WDS, nobody clears it, and the
     * second asserts wdt_reset -> proc_sys_reset aux_reset_in. */
    mps3_service_inhibit_kick();
    mps3_reg_write32(MPS3_WDOG_BASE, WDOG_TWCSR1, WDOG_TWCSR1_EWDT2);
    mps3_reg_write32(MPS3_WDOG_BASE, WDOG_TWCSR0, WDOG_TWCSR0_EWDT1);
    s_reboot_state = REBOOT_ARMED;
}

/* ---- slot (v0.14) --------------------------------------------------------- */

/* The slot verb's STABLE CODES (2026-09-26, HM_ANSWERS S3/C2): one table from the
 * refusal texts -- the handler's own, the weak decline's and every provider's
 * (slot_linux.c; net-protocol.md "The acts") -- to the `code` beside `err`, so no
 * provider changes and a client matches a code instead of prose. `?` matches the
 * one slot letter; a pattern matches as a PREFIX of the text. An unlisted text
 * gets no code (the key is then absent, never a guess). */
static const struct { const char *pat; const char *code; } k_slot_codes[] = {
    { "slot not supported",           "not_supported"  },
    { "bad act",                      "bad_act"        },
    { "bad slot",                     "bad_slot"       },
    { "slot locked:",                 "locked"         },
    { "no card",                      "no_card"        },
    { "card io",                      "card_io"        },
    { "no stage0 block",              "no_stage0"      },
    { "fabric static_id unknown",     "fabric_unknown" },
    { "EBUSY",                        "busy"           },
    { "nothing staged",               "nothing_staged" },
    { "slot mismatch:",               "slot_mismatch"  },
    { "slot ? is not a valid image",  "not_valid"      },
    { "slot ? not verified",          "not_verified"   },
    { "slot ? changed since",         "changed"        },
    { "slot ? is for ",               "wrong_static"   },
    { "slot ? runs, but identity lock", "identity_lock" },
    { "boot-select",                  "bootsel"        },
};

static int slot_code_match(const char *pat, const char *s)
{
    for (; *pat; pat++, s++) {
        if (*s == '\0' || (*pat != '?' && *pat != *s)) {
            return 0;
        }
    }
    return 1;
}

static void set_slot_err(mps3_ctrl_response_t *resp, const char *msg)
{
    set_err(resp, msg);
    for (unsigned i = 0; i < sizeof(k_slot_codes) / sizeof(k_slot_codes[0]); i++) {
        if (slot_code_match(k_slot_codes[i].pat, msg)) {
            strncpy(resp->code, k_slot_codes[i].code, sizeof(resp->code) - 1u);
            resp->code[sizeof(resp->code) - 1u] = '\0';
            return;
        }
    }
}

static void coordinator_handle_slot(const mps3_ctrl_request_t *req,
                                    mps3_ctrl_response_t *resp)
{
    /* {"op":"slot","act":...[,"slot":"A"|"B"]}. The VALUES are checked here, on
     * every engine, so "bad act" / "bad slot" mean the same thing everywhere and
     * a provider only ever sees a request it can act on. Everything else --
     * which slot is running, what the card holds, whether a flip is safe -- is
     * the provider's (coordinator.h). No register is touched here. */
    static const struct { const char *name; int act; } k_acts[] = {
        { "status",   MPS3_SLOT_ACT_STATUS   },
        { "commit",   MPS3_SLOT_ACT_COMMIT   },
        { "rollback", MPS3_SLOT_ACT_ROLLBACK },
        { "verify",   MPS3_SLOT_ACT_VERIFY   },
    };
    int act = -1;
    for (unsigned i = 0; i < sizeof(k_acts) / sizeof(k_acts[0]); i++) {
        if (strcmp(req->act, k_acts[i].name) == 0) {
            act = k_acts[i].act;
            break;
        }
    }
    if (act < 0) {
        set_slot_err(resp, "bad act");
        return;
    }
    int sel = MPS3_SLOT_NONE;
    if (req->slot_sel[0] != '\0') {
        if (strcmp(req->slot_sel, "A") == 0) {
            sel = MPS3_SLOT_A;
        } else if (strcmp(req->slot_sel, "B") == 0) {
            sel = MPS3_SLOT_B;
        } else {
            set_slot_err(resp, "bad slot");
            return;
        }
    }
    const char *why = mps3_slot_op(act, sel, &resp->slot_st);
    if (why != 0) {
        set_slot_err(resp, why);
        return;
    }
    resp->ok = 1;
}

/* Per-boot reset of the v0.11 state. On the target .bss/.data already hold
 * these values at the one coordinator_init() per boot -- except that a WATCHDOG
 * warm restart does NOT re-initialise .data (MicroBlaze BRAM keeps its last
 * contents and crt0 only zeroes .bss), so s_dut_mhz would otherwise survive a
 * `reboot` holding the pre-reboot preset. Resetting here makes every boot,
 * cold or warm, start from the same state. */
static void coordinator_v011_reset(void)
{
    s_reboot_state = REBOOT_IDLE;
    s_reboot_t0_ms = 0u;
    s_dut_mhz      = MPS3_DUT_CLK_BOOT_MHZ;
}

/* ---- identity / identity_set (v0.16) ---------------------------------------- */

/* The board identity (net-protocol.md "Identity"): an ENGINE verb pair. The
 * claim lock is checked HERE, first, for the mutation -- the same seam and the
 * same shape as the D13 store's -- so no engine can serve identity_set to a
 * remote peer of a claimed board by forgetting it. Everything else is the
 * provider's: it parses the (optional) arguments from req->line, and on success
 * hands back the reply body. */
static void coordinator_handle_identity(const mps3_ctrl_request_t *req,
                                        mps3_ctrl_response_t *resp, int set)
{
    /* set: 0 identity, 1 identity_set (claim-locked), 2 locate (not locked: it is
     * benign, and the Harness Manager sends it over Ethernet) */
    if (set == 1 && claim_locked(resp, "identity")) {
        return;
    }
    const char *body = 0;
    char code[sizeof(resp->code)];
    code[0] = '\0';
    const char *why = (set == 2) ? mps3_locate_op(req, &body, code, sizeof(code))
                                 : mps3_identity_op(set, req, &body, code, sizeof(code));
    if (why) {
        set_err(resp, why);
        (void)snprintf(resp->code, sizeof(resp->code), "%s", code);
        return;
    }
    resp->ok = 1;
    resp->body = body;
}

/* ---- hello / panel (v0.17) -------------------------------------------------- */

/* Presence and the front panel (net-protocol.md "Presence and the panel"): ENGINE
 * verbs with the identity verbs' shape. The claim lock is checked HERE, first, for
 * the one mutation -- `panel` with a `page` (it moves what the glass shows) -- so no
 * engine can serve a page change to a remote peer of a claimed board by forgetting
 * it. `hello` and the `panel` reads (state, frame) stay open: presence is display
 * only, and a read changes nothing. */
static void coordinator_handle_panel(const mps3_ctrl_request_t *req,
                                     mps3_ctrl_response_t *resp, int hello)
{
    if (!hello && req->line) {
        mps3_json_obj_t obj;
        char page[16];
        if (mps3_json_parse(req->line, req->line_len, &obj) == MPS3_JSON_OK &&
            mps3_json_get_string(&obj, "page", page, sizeof(page)) != MPS3_JSON_EMISSING &&
            claim_locked(resp, "panel")) {
            return;
        }
    }
    const char *body = 0;
    char code[sizeof(resp->code)];
    code[0] = '\0';
    const char *why = hello ? mps3_hello_op(req, &body, code, sizeof(code))
                            : mps3_panel_op(req, &body, code, sizeof(code));
    if (why) {
        set_err(resp, why);
        (void)snprintf(resp->code, sizeof(resp->code), "%s", code);
        return;
    }
    resp->ok = 1;
    resp->body = body;
}
