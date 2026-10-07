/*
 * coordinator.h — main-loop state + control-channel (TCP 6900) dispatch.
 * See coordinator/README.md and docs/contracts/net-protocol.md.
 */
#ifndef MPS3_COORDINATOR_H
#define MPS3_COORDINATOR_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include "../common/net_proto.h"
#include "../common/platform_regs.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------------------
 * Global shell state — the coordinator's view of "what's true right now."
 * Single-threaded bare metal: no lock needed as long as every mutation
 * happens from the main loop or a callback it invokes synchronously (never
 * from an ISR without disabling interrupts around the read-modify-write).
 * ------------------------------------------------------------------------ */
typedef struct {
    uint32_t static_id;        /* this shell build's static_id (build-time constant,
                                 * TODO(A3): source from a build-info register or
                                 * linker symbol once A1/A6 define where static_id
                                 * itself is readable in hardware) */
    uint32_t current_rm_id;    /* RM currently resident in the RP; 0 = greybox */
    bool     link_gated;       /* VPHY link forced down during a swap */
    bool     xvc_gated;        /* XVC/Debug Bridge access gated during a swap */
    bool     swd_gated;        /* SWD access gated during a swap */
    bool     uart_gated;       /* UART/SWO relay gated during a swap */
} mps3_shell_state_t;

extern mps3_shell_state_t g_shell_state;

/* This shell build's static_id — the identity `ping` reports as shell_id
 * and config_agent matches pushed bitstream headers against. Deliberate
 * SEAM: coordinator.c provides a WEAK fallback (returns 0 = "not
 * provisioned"); the real value is baked in at shell build time by a
 * strong override in a generated source file (A2's static_id scheme,
 * W-DFX-ART — e.g. hash of the routed static; A6 owns the final
 * provenance decision, see docs/NEXT_WAVE_PLAN.md). firmware/test/ and the
 * golden-test echo tool override it the same way. */
uint32_t mps3_shell_static_id(void);

/* The swap/commit REFUSAL seam. NULL = allowed. An engine that cannot prove
 * which fabric it is running on (the Linux harness: a card image whose claim
 * disagrees with the stage0-baked fabric static_id, or no valid stage0 block --
 * HARNESSD_CONTRACT.md §9.4) returns the reason, and `swap`/`commit` answer
 * {"ok":false,"err":<reason>} BEFORE any register is touched. coordinator.c
 * carries a WEAK default returning NULL, so a bare-metal image -- whose
 * static_id is baked into the same bitstream as the fabric -- never refuses. */
const char *mps3_swap_refusal(void);

/* The CLAIM-LOCK seam for the D13 store's mutations (HM_ANSWERS_2026-09-26 S6,
 * change 9; David: YES). 1 = refuse `what` ("usd" = a `usd` action -- format,
 * clear, rescan; "commit" = the v0.13 re-push) from the 6900 client being served,
 * because the board's SSH is claimed and that peer is not the board itself. The
 * verb then answers {"ok":false,"err":"<what> locked: board claimed (use
 * ssh)","code":"locked"} before anything else is checked -- the slot lock's rule
 * (net-protocol.md "The lock"). `usd` status stays open. coordinator.c carries a
 * WEAK default returning 0 (bare metal has no claim); mps3-harnessd's is in
 * slot_linux.c, the SAME check as the slot mutations. */
int mps3_claim_refuses_active_peer(const char *what);

/* Lifecycle. There is deliberately NO coordinator_main_loop() here: the
 * superloop lives in firmware/platform/src/main.c (which interleaves the
 * platform-only lwIP servicing with the module polls). The declaration that
 * used to sit on this line described a second, never-called copy of that
 * sequence -- see coordinator.c where it was removed. */
void coordinator_init(void);

/* One handler per net-protocol.md control-channel verb. Each is called
 * synchronously from the TCP 6900 recv callback with an already-decoded
 * request (see common/net_proto.h mps3_ctrl_request_t) and must return
 * promptly — a handler that needs real work done (e.g. `swap`) arms the
 * relevant module's non-blocking state machine and returns immediately;
 * the *response* for a long-running op is sent later, when that state
 * machine reaches its DONE/FAILED state (see swap_fsm.h).
 */
void coordinator_handle_ping(const mps3_ctrl_request_t *req, mps3_ctrl_response_t *resp);
void coordinator_handle_reset(const mps3_ctrl_request_t *req, mps3_ctrl_response_t *resp);
void coordinator_handle_set_clk(const mps3_ctrl_request_t *req, mps3_ctrl_response_t *resp);
void coordinator_handle_swap(const mps3_ctrl_request_t *req, mps3_ctrl_response_t *resp);
void coordinator_handle_link(const mps3_ctrl_request_t *req, mps3_ctrl_response_t *resp);
void coordinator_handle_commit(const mps3_ctrl_request_t *req, mps3_ctrl_response_t *resp);
void coordinator_handle_telemetry(const mps3_ctrl_request_t *req, mps3_ctrl_response_t *resp);
void coordinator_handle_macgen(const mps3_ctrl_request_t *req, mps3_ctrl_response_t *resp);
void coordinator_handle_diag(const mps3_ctrl_request_t *req, mps3_ctrl_response_t *resp);

/* v0.13 (D13) `usd`: status (send-now, never held) or an action (`rescan`
 * send-now; `format` / `clear` HELD until the store job ends). `commit` above is
 * the v0.13 re-push: accepted = HELD until the pair has been pushed over 6910,
 * written, read back and the header flipped (or it failed). */
void coordinator_handle_usd(const mps3_ctrl_request_t *req, mps3_ctrl_response_t *resp);

/* Remote CLCD-KVM ownership flip (net-protocol.md "display"). Drives the same
 * frozen CLCDKVM.CTRL.src_sel/src_sel_we the button and a local CSR write drive
 * (firmware/clcd_kvm/clcd_kvm_request_owner), then reads back STATUS.owner. This
 * is a plain SEND-NOW response (immediate CSR pokes — unlike swap it is never
 * held). On a build WITHOUT the KVM slave (MPS3_HAS_CLCD_KVM off) it decodes
 * fine but returns ok=0 "clcd_kvm not present" — it never touches 0x44AD. */
void coordinator_handle_display(const mps3_ctrl_request_t *req, mps3_ctrl_response_t *resp);

/* Build identity of the RUNNING image (net-protocol.md v0.8 "version").
 * Reports the harness release / HARNESS_VER32 / git sha+dirty from the
 * mps3_harness_*() seam (firmware/platform/mps3_version.h), the LMB size this
 * image was LINKED for (-DMPS3_LMB_KB), and the compile-time feature set as a
 * MPS3_FEATURE_* bitmask, plus (since 8ff559a) usr_access: ONE read of the
 * USRACC CSR at 0x44B3_0000, reported null where the fabric does not answer.
 * Everything else is a build-time constant, so the verb still answers on a
 * board whose fabric is unhappy; SEND-NOW, never held.
 *
 * It deliberately does NOT report static_id: that is `ping`'s job and a
 * different axis (compatibility vs provenance -- mps3_version.h says why). */
void coordinator_handle_version(const mps3_ctrl_request_t *req, mps3_ctrl_response_t *resp);

/* ------------------------------------------------------------------------
 * v0.11 verbs. `stats`, `log`, `touch_cal` and `reboot` are handled by static
 * functions in coordinator.c (reachable only through coordinator_dispatch_line,
 * like `dutrx`); what is declared here are the SEAMS and the reboot poll.
 * ------------------------------------------------------------------------ */

/* `stats` link/MAC seam. coordinator.c must stay free of platform/driver
 * headers, so the LAN9220 PHY view reaches it through this one function: a
 * WEAK default in coordinator.c reports "no link, unknown speed, MAC 0" (what a
 * host-gcc binary honestly knows), and firmware/platform/src/main.c provides
 * the strong definition from smsc911x_link_up()/ANLPAR and mps3_platform_mac().
 * spd is 10 or 100 when the link is up, 0 otherwise. */
typedef struct {
    int      link;
    uint32_t spd;
    int      fdx;
    uint8_t  mac[6];
} mps3_stats_net_t;
void mps3_stats_net(mps3_stats_net_t *out);

/* `stats.dut_mhz` seam. `programmed_mhz` is what THIS process last programmed
 * (seeded at the BD's 50 MHz, re-seeded on every boot). The WEAK default in
 * coordinator.c returns it unchanged -- the bare-metal image, whose reply bytes
 * are therefore exactly what they were. mps3-harnessd provides the strong one
 * (harnessd/clk_linux.c): the frequency clk_wiz_dut's register file describes
 * (clkrst_read_mhz), because a Linux process can be respawned while the MMCM
 * keeps running a preset the new process never programmed. */
uint32_t mps3_dut_clk_mhz(uint32_t programmed_mhz);

/* `reboot` (v0.11). The verb only REQUESTS: it answers {"ok":true,"in_ms":N}
 * and records the request. This poll -- run from coordinator_net_poll(), i.e.
 * from the control-channel service and every interleave -- arms the shell
 * watchdog once MPS3_REBOOT_ARM_DELAY_MS has passed, which is what lets the
 * reply leave the board first. From then on the superloop never kicks again
 * (mps3_service_inhibit_kick) and the watchdog's second expiry pulses
 * proc_sys_reset's aux_reset_in: a warm restart of the shell firmware
 * (fpga/shell/bd/shell_bd.tcl, "THE RESET FAN-OUT"). */
#ifndef MPS3_REBOOT_ARM_DELAY_MS
#define MPS3_REBOOT_ARM_DELAY_MS 100u
#endif
/* WDOG timeout, one stage: 2^C_WDT_INTERVAL shell clocks (shell_bd.tcl sets
 * C_WDT_INTERVAL 27 on the 100 MHz shell clock -> 1342 ms). The reset is the
 * SECOND expiry, so it lands at most two stages after arming. */
#ifndef MPS3_WDT_STAGE_MS
#define MPS3_WDT_STAGE_MS 1342u
#endif
void coordinator_reboot_poll(void);
/* 0 idle, 1 requested (reply sent, not yet armed), 2 armed (watchdog running,
 * no more kicks). For tests and the diag view; the target never branches on it. */
int  coordinator_reboot_state(void);

/* The CARD-JOB seam for `reboot` (Linux harness, HM_ANSWERS_2026-09-26 change 6).
 * 1 while the engine has a job on the user microSD that a watchdog reset must not
 * cut short: under mps3-harnessd, a slot-image push or `verify` (slot_linux.c) or
 * a D13 store job -- a `commit` or a `usd` format/clear (ovlstore_linux.c answers
 * for both). `reboot` then answers {"ok":false,"err":"EBUSY"}, exactly as it does
 * mid-swap. B2 silicon: a reboot mid-job wedged the card (stage0 then reported
 * `uSD init error`). coordinator.c carries a WEAK default returning 0, so a
 * bare-metal image's `reboot` is exactly what it was. */
int  mps3_card_job_active(void);

/* ------------------------------------------------------------------------
 * `slot` (v0.14, net-protocol.md "Slot images"; Linux harness plan §10a S10).
 * The verb is SHARED (decode + act/selector checks in coordinator.c, the reply
 * shape in net_proto.c); what it does is the ENGINE's, through ONE provider:
 *
 *   const char *mps3_slot_op(int act, int sel, mps3_slot_status_t *st);
 *     act  MPS3_SLOT_ACT_* (already validated)
 *     sel  MPS3_SLOT_NONE, or MPS3_SLOT_A/B when the request named a slot
 *     st   filled with the state AFTER the act, on success
 *   returns NULL on success, else the {"ok":false,"err":...} text (<= 63 chars).
 *
 * The WEAK default in coordinator.c answers "slot not supported" for every act:
 * the bare-metal MicroBlaze boots from the MCC's config SD and has no stage0
 * slots, so a tool learns that from the verb itself. mps3-harnessd supplies the
 * strong one (src/linux_harness/sw/harnessd/slot_linux.c), backed by the user
 * microSD. The 6910 half (a push of kind MPS3_BIN_KIND_SLOT_IMAGE) goes through
 * config_agent.h's mps3_cfg_slot_sink() provider, weak NULL in the same way. */
const char *mps3_slot_op(int act, int sel, mps3_slot_status_t *st);
/* 1 = this engine SERVES the `slot` verb (a strong mps3_slot_op() is linked):
 * `version.features` then reports "slot" (bit 14, HM_ANSWERS_2026-09-26 S3). The
 * WEAK default in coordinator.c returns 0 (bare metal: the verb declines), and
 * slot_linux.c returns 1 -- so the bit follows the linked provider, never a -D
 * flag, and cannot disagree with what the verb does. */
int mps3_slot_supported(void);
/* 1 = this engine locks the DUT debug ports (XVC 2542, jtag_server 6921) to the
 * board itself once its SSH is claimed (xvc_server.h / jtag_server.h "THE CLAIM
 * LOCK"): `version.features` then reports "xvc_lock" (bit 15, HM_ANSWERS C3). WEAK
 * 0 in coordinator.c (bare metal has no claim); slot_linux.c returns 1. */
int mps3_debug_lock_supported(void);

/* ---- identity / identity_set (v0.16, net-protocol.md "Identity") ----------
 * The board identity (label, hostname, IP, MAC) of a Linux harness: ENGINE verbs,
 * served by ONE provider that parses the request's own arguments from
 * req->line and renders the reply body (the keys after "op"):
 *
 *   const char *mps3_identity_op(int set, const mps3_ctrl_request_t *req,
 *                                const char **body, char *code, size_t code_cap);
 *     set  0 = `identity` (read), 1 = `identity_set`
 *   returns NULL on success (*body = the provider's static body text), else the
 *   {"ok":false,"err":...} text (<= 63 chars) with an optional stable `code`.
 *
 * `identity_set` is CLAIM-LOCKED before the provider is asked, exactly like the
 * D13 store's mutations (mps3_claim_refuses_active_peer("identity"):
 * "identity locked: board claimed (use ssh)", code "locked"). The WEAK default in
 * coordinator.c declines both verbs ("identity not supported", code
 * "not_supported"): bare metal has no resolver. mps3-harnessd's provider is
 * src/linux_harness/sw/harnessd/identity_linux.c. */
const char *mps3_identity_op(int set, const mps3_ctrl_request_t *req,
                             const char **body, char *code, size_t code_cap);

/* ---- locate (v0.16, net-protocol.md "Locate"; the Harness Manager's R3) ------
 * "Which of these boards is this one?": {"op":"locate","s":1..30[,"who":".."]}
 * blinks the panel backlight at 2 Hz and shows "IDENTIFY: <who>" for `s` seconds;
 * {"s":0} stops; a new one replaces the old; a tap stops it. An ENGINE verb with
 * the identity verbs' shape (the provider parses req->line, renders the body):
 * NOT claim-locked (it is benign, and a tool sends it over Ethernet). WEAK
 * default: "locate not supported", code "not_supported". mps3-harnessd's
 * provider is src/linux_harness/sw/harnessd/locate_linux.c. */
const char *mps3_locate_op(const mps3_ctrl_request_t *req, const char **body,
                           char *code, size_t code_cap);

/* ---- hello / panel (v0.17, net-protocol.md "Presence and the panel") -------
 * The Harness Manager's R1/R2: `hello` (presence: a session table of <= 4, each
 * listed for its TTL after its last hello; the reply carries the panel's state and
 * its tap ring) and `panel` (the state; a frame half "a"/"b" = rows 0-7 / 8-14 with
 * their per-cell roles; a page change). ENGINE verbs with the identity verbs'
 * shape: the provider parses req->line (a hello's lease/job nest one level:
 * mps3_json_parse_ex) and renders the body.
 *
 *   {"op":"panel","page":..} is CLAIM-LOCKED before the provider is asked
 *   ("panel locked: board claimed (use ssh)", code "locked"); `hello` and the
 *   panel reads are open.
 *
 * WEAK defaults: "hello not supported" / "panel not supported", code
 * "not_supported" (bare metal; a Linux build without the panel). mps3-harnessd's
 * provider is src/linux_harness/sw/harnessd/panel_linux.c over presence_core.c. */
const char *mps3_hello_op(const mps3_ctrl_request_t *req, const char **body,
                          char *code, size_t code_cap);
const char *mps3_panel_op(const mps3_ctrl_request_t *req, const char **body,
                          char *code, size_t code_cap);

/* Central dispatch — decodes one JSON line, calls the matching handler
 * above, and encodes the per-op response into `out` (size it
 * MPS3_CTRL_RESP_MAX; always NUL-terminated). Used by the TCP 6900 recv
 * callback.
 *
 * Returns 1 if `out` holds a complete response line to send NOW, or 0 if
 * the response is DEFERRED -- an accepted `swap`, an accepted v0.13 `commit`,
 * or a `usd` format/clear: the network layer must hold the connection open,
 * let the main loop run, and send what coordinator_held_poll() builds (for a
 * swap, coordinator_swap_final_response() is the same answer). net-protocol.md
 * shows each as one request/response pair; on this single-threaded server that
 * is naturally a held response, not a second ack+result exchange. */
int coordinator_dispatch_line(const char *line, int len, char *out, int out_len);

/* The DEFERRED response, once it has settled: returns 1 and fills *resp (then
 * nothing is held any more), or 0 while the held verb is still running (or
 * nothing is held). Poll it every pass whether or not the client that asked is
 * still connected: a held commit / format still has to finish. */
int coordinator_held_poll(mps3_ctrl_response_t *resp);
/* 1 while a deferred verb's answer is owed. */
int coordinator_held_pending(void);

/* Builds the final `swap` response (ok/rm_id/verified per net-protocol.md
 * step 7) from swap_fsm_last_result(). Call once the FSM has completed the
 * swap that coordinator_dispatch_line() deferred (any time at/after the
 * terminal state — the result stays latched until the next
 * swap_fsm_start()). If no swap has completed since the last start, the
 * response is ok=false "swap not complete" (fail closed). */
void coordinator_swap_final_response(mps3_ctrl_response_t *resp);

/* ------------------------------------------------------------------------
 * Control-channel transport (coordinator_net.c, W-NET-SEAM): the TCP 6900
 * listener written against common/net_if.h — JSON-lines assembly ->
 * coordinator_dispatch_line() -> send-now, or PARK the connection for an
 * accepted `swap` and send coordinator_swap_final_response() once the FSM
 * settles. Single-client v1 (a second connect is refused while one is
 * open — net-protocol.md is silent on concurrency; flag for A6, matches
 * the fake shell's one-session-at-a-time semantics loosely).
 * ------------------------------------------------------------------------ */
void coordinator_net_init(void);
void coordinator_net_poll(void);
/* The 6900 client whose line is being dispatched right now (NULL between
 * clients). For a handler that must know WHO asked -- the Linux harness's
 * slot lock (net-protocol.md "Slot images", v0.14) -- via mps3_net_conn_peer().
 * Read-only; nothing in the bare-metal image calls it. */
struct mps3_net_conn;   /* opaque (common/net_if.h) */
struct mps3_net_conn *coordinator_net_active_conn(void);

#ifdef __cplusplus
}
#endif

#endif /* MPS3_COORDINATOR_H */
