/*
 * swap_fsm.h — the DUT-swap state machine driving DFXCTL/HWICAP/CLKRST.
 * See coordinator/README.md "Swap sequence <-> code map" and
 * docs/ARCHITECTURE_SPEC.md §6.2 / §7.
 *
 * I2 RESOLVED (docs/contracts/OPEN_ISSUES.md / net-protocol.md "Swap
 * sequence"): the SHELL owns clearing-bitstream sequencing. There is
 * exactly ONE live "current clearing" reference at any time -- the
 * currently-loaded RM's clearing bitstream -- never a multi-RM keyed cache.
 * The old MPS3_RM_CACHE_SLOTS array (a hedge against three different,
 * mutually-inconsistent readings of the contracts, see coordinator/
 * README.md's now-resolved "Open design question") is retired; see
 * `mps3_clearing_ref_t g_current_rm_clearing` below.
 *
 * I25 RESOLVED (docs/contracts/OPEN_ISSUES.md): step_verify() now performs a
 * real DFXCTL.RM_ID compare instead of hardcoding `verified = true` -- see
 * swap_fsm.c's step_verify() and the `verify_ok` transition input below.
 */
#ifndef MPS3_SWAP_FSM_H
#define MPS3_SWAP_FSM_H

#include <stdint.h>
#include <stdbool.h>
#include "../common/net_proto.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Swap sequence per net-protocol.md's numbered steps, I2 model:
 *   1. SWAP_GATE                    gate XVC/SWD/UART/VPHY link
 *      SWAP_DECOUPLE_ASSERT         assert DECOUPLE + hold rp_resetn
 *   2. SWAP_STREAM_CLEARING         stream g_current_rm_clearing (already
 *                                   resident -- I2 guarantees the shell
 *                                   always holds it, no network wait) into
 *                                   HWICAP
 *      SWAP_AWAIT_INCOMING_CLEARING wait for config_agent to receive +
 *                                   validate the INCOMING pair's clearing
 *                                   bitstream (net-protocol.md: "the host
 *                                   supplies only the incoming RM's pair").
 *                                   Captured/staged only -- NOT streamed to
 *                                   HWICAP this swap; it becomes the *next*
 *                                   swap's outgoing clearing (see
 *                                   SWAP_CACHE_CLEARING below).
 *   3. SWAP_AWAIT_PARTIAL           wait for config_agent to validate the
 *                                   incoming partial
 *      SWAP_STREAM_PARTIAL          stream it into HWICAP
 *   4. SWAP_VERIFY                  read DFXCTL.RM_ID, compare against the
 *                                   target rm_id (I25 -- real compare, not a
 *                                   hardcoded true)
 *   5. SWAP_CACHE_CLEARING          promote the staged incoming clearing
 *                                   (from step 2's AWAIT_INCOMING_CLEARING)
 *                                   to g_current_rm_clearing
 *   6. SWAP_RELEASE                 release DECOUPLE, deassert rp_resetn
 *   7. SWAP_DONE / SWAP_FAILED      respond with confirmed rm_id + verified
 */
/* fail-IDLE bound for SWAP_AWAIT_INCOMING_CLEARING / SWAP_AWAIT_PARTIAL, the
 * only two states that used to have none. Re-armed on every byte of RX
 * progress, so this bounds SILENCE, not transfer duration -- a slow but
 * progressing 1.31 MB upload is never killed. Override with -D. */
#ifndef MPS3_SWAP_AWAIT_IDLE_MS
#define MPS3_SWAP_AWAIT_IDLE_MS 30000u
#endif

typedef enum {
    SWAP_IDLE = 0,
    SWAP_GATE,
    SWAP_DECOUPLE_ASSERT,
    SWAP_STREAM_CLEARING,
    SWAP_AWAIT_INCOMING_CLEARING,
    SWAP_AWAIT_PARTIAL,
    SWAP_STREAM_PARTIAL,
    SWAP_VERIFY,
    SWAP_CACHE_CLEARING,
    SWAP_RELEASE,
    SWAP_DONE,                /* terminal: success, respond to host, -> IDLE */
    SWAP_FAILED,              /* terminal: failure, respond to host, -> IDLE.
                              * Left DECOUPLEd + rp_reset held as the safe
                              * state (RP inert) -- see swap_fsm.c note on
                              * why this is a design choice, not a contract
                              * requirement (§13 robustness theme, no
                              * explicit swap-failure recovery spec'd). */

    /* R1: appended (NOT inserted) so every existing enumerator keeps its
     * numeric value -- the JTAG diag mailbox and the host tests both read
     * these as raw integers.
     *
     * Re-isolate: VERIFY now runs with the RP CONNECTED (it must -- see the
     * ordering note in swap_fsm.c), so a swap that fails verification has
     * already exposed an unknown RM to the shell. This state puts it back in
     * the box (DECOUPLE + rp_resetn + AXI shutdown) before reporting failure,
     * restoring the "FAILED => RP inert" invariant above. */
    SWAP_REISOLATE,
} mps3_swap_state_t;

/* I2 model: the ONE currently-loaded RM's clearing bitstream reference.
 * Boot seed = the greybox's clearing (ships inside the shell image,
 * overlay-manifest.md; see overlay_store_get_greybox_clearing()).
 * Thereafter replaced by SWAP_CACHE_CLEARING (with the just-validated incoming
 * RM's clearing) -- including the power-on load from the user microSD, which is
 * an ordinary swap with the internal source "usd" (D13).
 *
 * The clearing bytes are ALWAYS in RAM (D13 retired the QSPI clearing cache,
 * `in_qspi`, with the SST26 backend): `data` points at the shell image's
 * greybox blob (boot seed) or at this module's clearing ARENA (see
 * swap_fsm_clearing_stage_buffer(); sized to hold the largest clearing, 262,144
 * B >= the ~222 KB multicore one), or is NULL in harness builds whose fakes only
 * exercise the bookkeeping (streaming then advances counters, no data movement --
 * every consumer tolerates NULL). A clearing that does not fit the arena is NOT
 * cached: the ref is marked invalid and the next swap-away fails CLOSED. */
typedef struct {
    bool        valid;
    uint32_t    rm_id;
    uint32_t    static_id;
    uint32_t    len_words;
    uint32_t    crc32;
    const void *data;      /* RAM payload bytes; NULL tolerated (bookkeeping fakes) */
} mps3_clearing_ref_t;

extern mps3_clearing_ref_t g_current_rm_clearing;

/* The currently-RESIDENT RM's partial bitstream reference (same struct —
 * it is a generic bitstream ref). Promoted alongside the clearing at
 * SWAP_CACHE_CLEARING (a failed swap never updates it). `data` points into
 * config_agent's partial staging slot (valid until the next partial push
 * begins) or is NULL when the partial's bytes were never held in RAM
 * (ICAP-direct, or loaded from the user microSD). Informational since D13: the
 * v0.13 `commit` is a RE-PUSH, it never reads this. */
extern mps3_clearing_ref_t g_current_rm_partial;

void swap_fsm_set_current_partial(const mps3_clearing_ref_t *ref);

/* Path 3 (stream-direct) — the HWICAP stream-direct PARTIAL sink, handed to
 * config_agent_set_icap_direct_sink() by coordinator_init() (under
 * MPS3_CFG_AGENT_ICAP_DIRECT). Its begin/write/finish/abort pack each incoming
 * partial word MSB-first (I18) and push it straight into HWICAP.WF, WFV-paced
 * with a bounded inter-chunk SR_DONE poll, so an 886 KB–1.65 MB partial streams
 * to the ICAP as it arrives over 6910 without ever being buffered in RAM/QSPI.
 * begin() fails closed unless the FSM has already asserted DECOUPLE + rp_reset
 * (the swap must be armed first). The struct is config_agent.h's
 * mps3_cfg_agent_qspi_sink_t; forward-declared here to keep this header free of
 * a config_agent.h include (same discipline as overlay_store.h). */
struct mps3_cfg_agent_qspi_sink;
const struct mps3_cfg_agent_qspi_sink *swap_fsm_icap_direct_sink(void);

/* The clearing-arena slot NOT currently backing g_current_rm_clearing —
 * fill it (e.g. overlay_store reading the boot slot's clearing out of
 * QSPI, or swap_fsm itself copying config_agent's staged incoming
 * clearing), then pass a ref with .data == the returned pointer to
 * swap_fsm_set_current_clearing(): the FSM recognizes its own arena
 * pointers and flips which half is live. *cap_out (may be NULL) receives
 * the slot capacity in bytes. */
void *swap_fsm_clearing_stage_buffer(uint32_t *cap_out);

void swap_fsm_init(void);

/* Seeds/replaces g_current_rm_clearing -- see the struct doc above for the
 * three call sites (boot seed, post-boot-load, post-swap-cache). */
void swap_fsm_set_current_clearing(const mps3_clearing_ref_t *ref);

/* Arms a swap to `rm_name` sourced via `src` ("tftp" or "tcp", i.e. TFTP/69
 * vs raw push/6910 -- net-protocol.md swap.src -- or the INTERNAL source "usd":
 * the verified default on the user microSD (D13). Only the overlay store's
 * power-on hook starts a "usd" swap; coordinator_handle_swap() refuses it from
 * the host (there is no host `swap src:usd` in v0.13). With "usd" the FSM takes
 * the pair from overlay_store_src_*() instead of config_agent: the outgoing
 * clearing streams exactly as for a network swap, the partial streams from the
 * card (re-read and re-CRC'd; its last buffer is withheld on a mismatch), and at
 * SWAP_CACHE_CLEARING the slot's clearing is read into the RAM arena, so nothing
 * points at the card after the swap. Returns 0 if accepted, <0
 * if a swap is already in progress (single-swap-at-a-time v1; the contract
 * doesn't say whether concurrent swaps to different RMs should ever be
 * meaningful -- assume no). The target rm_id used for SWAP_VERIFY's I25
 * compare is NOT resolved from `rm_name` here -- it comes from the
 * partial's own bitstream header once config_agent validates it (I14: the
 * wire header already carries rm_id numerically for exactly this reason;
 * no separate name<->id table is needed in firmware). */
int swap_fsm_start(const char *rm_name, const char *src);

/* Call once per superloop iteration (firmware/platform/src/main.c). Does a bounded amount of
 * work (one HWICAP FIFO's worth of words, one register poll, etc.) per
 * call and returns -- never blocks. */
void swap_fsm_poll(void);

mps3_swap_state_t swap_fsm_state(void);
bool swap_fsm_idle(void);

/* Outcome of the most recently COMPLETED swap — recorded when the FSM's
 * terminal state (SWAP_DONE/SWAP_FAILED) runs its one poll before the
 * table advances to IDLE, cleared by swap_fsm_start(). This is the data
 * half of net-protocol.md step 7 ("respond with confirmed rm_id +
 * verified"): coordinator_swap_final_response() reads it to build the
 * held control-channel response once the network layer (W-NET-SEAM)
 * learns the FSM settled. `rm_id` on failure is the still-resident
 * previous RM's id (a failed swap parks decoupled and never updates
 * g_shell_state.current_rm_id — see swap_fsm.c's SWAP_FAILED note). */
typedef struct {
    bool     valid;    /* a swap has completed since the last swap_fsm_start() */
    bool     ok;       /* terminal state was SWAP_DONE */
    bool     verified; /* the I25 DFXCTL.RM_ID compare passed (implies ok in v1) */
    uint32_t rm_id;    /* g_shell_state.current_rm_id at completion */
} mps3_swap_result_t;

const mps3_swap_result_t *swap_fsm_last_result(void);

/* Free-running total of bytes written to HWICAP.WF across every ICAP writer
 * (clearing + partial, lite + FIFO, RAM/QSPI/stream-direct). A diagnostic
 * counter (never reset per swap) surfaced on the control channel's diag verb +
 * the JTAG-readable diag mailbox — lets a HW run compare bytes-into-ICAP against
 * config_agent's receive progress at a stall. */
uint32_t swap_fsm_icap_bytes(void);

/* THE FRONT PANEL'S PROGRAMMING PROGRESS (Harness Manager CLCD_ALIGNMENT R5): a
 * snapshot the panel turns into "prog  pushing <overlay>  42%" plus a bar. It is
 * the board's own count, so it works while a swap parks the control port.
 *   done   the swap_fsm_icap_bytes() delta since swap_fsm_start(): every byte this
 *          swap has put into HWICAP.WF (the outgoing clearing, then the partial,
 *          whichever writer -- RAM, stream-direct, the card).
 *   total  what this swap will put there: the outgoing clearing's length (known
 *          at start) + the incoming partial's -- the card slot's for "usd", the
 *          validated header's once config_agent hands it over, or, while that
 *          partial is still arriving in SWAP_AWAIT_PARTIAL, the push's declared
 *          length (config_agent_rx_progress). 0 while the partial's length is not
 *          known yet: the panel then shows no percentage rather than a guess.
 * O(1): counters only, no register access. Capture-only, like the diag getters
 * above: it changes no FSM timing or transition. */
typedef struct {
    bool        active;   /* a swap is in flight (state != SWAP_IDLE)            */
    uint8_t     state;    /* mps3_swap_state_t                                   */
    uint32_t    done;     /* bytes into HWICAP.WF since swap_fsm_start()          */
    uint32_t    total;    /* outgoing clearing + incoming partial; 0 = not known  */
    const char *rm;       /* the overlay name swap_fsm_start() was given; "" idle */
} mps3_swap_progress_t;
void swap_fsm_progress(mps3_swap_progress_t *out);

/* I18 residue (3) — post-DESYNC EOS/status measurability. The stream-direct
 * partial sink's icap_direct_finish() runs a BOUNDED post-DESYNC end-of-sequence
 * wait whose EOS assertion on this axi_hwicap build is UNPROVEN: the wait is
 * documented best-effort, so a successful swap alone does not demonstrate
 * HWICAP_SR_EOS ever set. These surface the RAW captured HWICAP_SR word and the
 * gate outcome onto the JTAG diag mailbox (via main.c -> mps3_diag_publish) so the
 * NEXT real stream-direct swap answers it by one JTAG read (diag.h icap_sr_last /
 * icap_eos_status). Capture-only — they change no FSM timing or transition. Both
 * persist across swaps (the most recent finish wins), like swap_fsm_icap_bytes(). */
#define MPS3_ICAP_EOS_NONE    0u  /* no stream-direct finish has run this boot */
#define MPS3_ICAP_EOS_SEEN    1u  /* last finish observed HWICAP_SR_EOS asserted */
#define MPS3_ICAP_EOS_TIMEOUT 2u  /* last finish's bounded EOS wait expired w/o EOS */

uint32_t swap_fsm_icap_sr_last(void);    /* raw last NON-ZERO HWICAP_SR seen at finish */
uint32_t swap_fsm_icap_eos_status(void); /* one of MPS3_ICAP_EOS_* */

/* `stats` (net-protocol v0.11) telemetry. Capture-only, like the two above: they
 * change no FSM timing or transition.
 *   swap_fsm_state_name()   the lowercase name of a state ("idle", "verify",
 *                           ...), stable wire strings; "?" for an out-of-range
 *                           value.
 *   swap_fsm_completed()    swaps that reached a TERMINAL state (DONE or FAILED)
 *                           since boot -- what fpgahub watches to notice a swap
 *                           it did not initiate.
 *   swap_fsm_fail_tag()     the state the most recent FAILED swap failed IN
 *                           (for a verify mismatch that is "verify", not the
 *                           "reisolate" that tidied up after it); "" until a
 *                           swap has failed this boot. It survives later
 *                           successful swaps on purpose: it is "the last
 *                           failure", and `swap_ok` says whether the last swap
 *                           was one. */
const char *swap_fsm_state_name(mps3_swap_state_t st);
uint32_t    swap_fsm_completed(void);
const char *swap_fsm_fail_tag(void);
/* The same as a state (SWAP_IDLE = no failure yet) -- the overlay store's boot
 * latch keeps it in 7 bits. */
mps3_swap_state_t swap_fsm_fail_state(void);

/* ==========================================================================
 * Pure transition table -- host-testable, ZERO register/hardware/network
 * dependency (see firmware/test/test_swap_fsm.c, which links
 * swap_fsm_transitions.c ALONE, no mock_regs, no config_agent/overlay_store
 * stand-ins needed at all).
 *
 * Every *decision* about which state comes next lives here, as one total
 * function over (current state, what's currently true). swap_fsm.c's
 * impure step_*() functions gather these booleans from real (or, in
 * firmware/test/, mocked) registers/modules, call this function, and then
 * perform whatever register pokes the OLD or NEW state implies -- they
 * never re-derive a transition decision themselves. This is the "thin HAL
 * shim" split applied to the FSM itself: swap_fsm_transitions.c has no
 * #include of platform_regs.h/config_agent.h/overlay_store.h/coordinator.h
 * at all, so it is trivially separable from every hardware poke.
 * ========================================================================== */
typedef struct {
    bool decouple_confirmed;      /* DECOUPLE_ASSERT -> STREAM_CLEARING: DFXCTL.STATUS confirms decoupled && rp_in_reset */
    bool clearing_cache_valid;    /* STREAM_CLEARING guard: g_current_rm_clearing.valid (I2 guarantees true post-boot-seed; false fails closed) */
    bool clearing_stream_done;    /* STREAM_CLEARING -> AWAIT_INCOMING_CLEARING: this call's HWICAP chunk finished the whole cached-clearing transfer */
    bool incoming_clearing_ready; /* AWAIT_INCOMING_CLEARING -> AWAIT_PARTIAL: config_agent_take_validated_clearing() handed one off */
    bool partial_ready;           /* AWAIT_PARTIAL -> STREAM_PARTIAL: config_agent_take_validated_partial() handed one off */
    bool await_timeout;           /* AWAIT_* -> FAILED: no RX progress for MPS3_SWAP_AWAIT_IDLE_MS. These were the only two
                                   * states in the FSM with no bound: a client that died mid-swap parked the shell FOREVER
                                   * (lwIP has no keepalive, config_agent is single-session), and recovery needed a JTAG
                                   * bitstream reload. It is an IDLE timeout, not a deadline -- a slow but progressing
                                   * 1.31 MB upload must never be killed. */
    bool partial_stream_done;     /* STREAM_PARTIAL -> RELEASE: this call's HWICAP chunk finished the whole partial transfer */
    bool verify_ok;               /* VERIFY -> CACHE_CLEARING: rm_id_valid && DFXCTL.RM_ID == target rm_id (I25) */
    bool verify_mismatch;         /* VERIFY -> REISOLATE: rm_id_valid asserted but the id is WRONG. Distinct from
                                   * "not yet valid": a settled-but-wrong id is decided immediately, whereas an
                                   * unsettled one keeps polling until confirm_timeout. Conflating them would either
                                   * fail every swap on the first poll, or spin for the full timeout on a real
                                   * mismatch. */
    bool release_confirmed;       /* RELEASE -> VERIFY: DFXCTL.STATUS confirms decoupled==0 && rp_in_reset==0 */
    bool stream_error;            /* STREAM_CLEARING/STREAM_PARTIAL -> FAILED: an HWICAP write / SR_DONE self-clear timed out
                                   * (fail CLOSED — an ICAP write that never completed must never be treated as "streamed OK") */
    bool confirm_timeout;         /* DECOUPLE_ASSERT/RELEASE -> FAILED: the bounded poll for DFXCTL.STATUS to confirm the
                                   * decouple (or release) expired — a decoupler/release that never confirms fails closed
                                   * rather than parking the client forever (fail-STUCK fix) */
} mps3_swap_transition_inputs_t;

/* Total function: every mps3_swap_state_t value has a defined next state
 * for every possible `in`. SWAP_IDLE always maps to itself (only an
 * explicit swap_fsm_start() call -- an external event, not a polled
 * condition -- leaves IDLE); SWAP_DONE/SWAP_FAILED always map to SWAP_IDLE
 * (the impure caller gets one last chance to run its terminal-state side
 * effect, e.g. sending the host response, against the OLD state before the
 * table advances to IDLE). */
mps3_swap_state_t swap_fsm_next_state(mps3_swap_state_t cur,
                                       const mps3_swap_transition_inputs_t *in);

#ifdef __cplusplus
}
#endif

#endif /* MPS3_SWAP_FSM_H */
