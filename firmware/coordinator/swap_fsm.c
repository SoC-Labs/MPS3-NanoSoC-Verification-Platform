/*
 * swap_fsm.c — DUT-swap state machine (impure half: register pokes +
 * module glue). The actual state-transition DECISIONS live in
 * swap_fsm_transitions.c's swap_fsm_next_state() (pure, host-testable with
 * zero dependency on anything this file includes) -- every step_*()
 * function below gathers inputs, performs whatever side effects belong to
 * the state it's IN, then hands the decision to that pure function and
 * acts on the result. See swap_fsm.h's big top-of-file comment for the
 * full I2/I25-resolved sequence this implements.
 *
 * The bitstream DATA paths are real now (W-NET-SEAM): payloads live in
 * config_agent's two staging slots (see config_agent.h STAGING DECISION);
 * the incoming clearing is copied at take-time into this module's own
 * two-buffer ARENA (so the *current* RM's clearing survives the next
 * pair's push overwriting config_agent's slot), and the HWICAP streaming
 * steps push actual payload words into HWICAP_WF.
 *
 * This file no longer OWNS a HWICAP writer. It used to carry one half of a
 * duplicated pair (hwicap_lite_write / hwicap_fifo_drain / hwicap_fifo_write_words
 * / icap_batch_flush here, a second copy of the same LITE-vs-FIFO split in
 * overlay_store.c) -- the shape that shipped a shell which pinged, passed every
 * gate and could not load an RM. All of it now lives behind ONE API,
 * common/hwicap_writer.h, and both files call it. The guarantees are unchanged
 * and are documented there: bounded + fail-closed polls (a stall becomes
 * in.stream_error -> SWAP_FAILED, and the post-DESYNC EOS wait rejects rather
 * than passing on timeout), and the ONE shared mps3_hwicap_pack_word() byte-lane
 * primitive (I18) for every path.
 */
#include <string.h>
#include <stdbool.h>
#include "swap_fsm.h"
#include "coordinator.h"
#include "../common/platform_regs.h"
#include "../common/hwicap_writer.h"  /* THE HWICAP writer -- see its header */
#include "../common/timebase.h"
#include "../config_agent/config_agent.h"
#include "../overlay_store/overlay_store.h"

/* Target-only diagnostic print. On the host test build (MPS3_HAL_MOCK — the same
 * discriminator platform_regs.h uses for host-vs-real-target) this is a no-op so
 * the harness stays free of the Xilinx BSP's xil_printf; on the real MicroBlaze
 * target it routes to the BSP's integer printf (the one main.c uses for its
 * bring-up banner). */
#ifdef MPS3_HAL_MOCK
#define MPS3_SWAP_LOG(...) ((void)0)
#else
#include "xil_printf.h"
#define MPS3_SWAP_LOG(...) xil_printf(__VA_ARGS__)
#endif

/* How many 32-bit words to push into HWICAP.WF per swap_fsm_poll() call
 * while streaming. Chosen to keep one poll iteration short relative to
 * lwIP's timer granularity -- exact value is a tuning TODO(A3), not a
 * contract value. */
#define MPS3_HWICAP_CHUNK_WORDS 256u

/* The running "bytes into HWICAP.WF" total and the I18(3) raw-SR capture now
 * live in the ONE writer (common/hwicap_writer.c) -- they are properties of the
 * ICAP write path, not of the swap FSM, and keeping them here is exactly how the
 * counter came to miss every byte overlay_store.c's copy of the writer pushed.
 * swap_fsm_icap_bytes()/_sr_last() below forward to it, so swap_fsm.h, the diag
 * mailbox and the `diag` verb are unchanged.
 *
 * The EOS *verdict* stays here: it is a per-finish outcome the FSM records
 * (MPS3_ICAP_EOS_*, swap_fsm.h), mapped from what the writer's finish returned.
 * Persists across swaps so a post-mortem JTAG read sees the most recent one. */
static uint32_t s_icap_eos_status = MPS3_ICAP_EOS_NONE;  /* MPS3_ICAP_EOS_* (swap_fsm.h) */

static mps3_swap_state_t s_state = SWAP_IDLE;
static char     s_target_rm[MPS3_CTRL_STR_MAX];
static char     s_src[MPS3_CTRL_STR_MAX];
static uint32_t s_words_done;
static uint32_t s_words_total;
static uint32_t s_target_rm_id; /* I25/I14: numeric, from the partial's own bitstream header -- see step_await_partial() */
/* The panel's progress snapshot (swap_fsm_progress()): the ICAP byte counter at
 * swap_fsm_start(), the outgoing clearing's length and the incoming partial's
 * length in bytes (0 until known). Written only where the FSM already learns
 * these numbers; read by nothing that decides a transition. */
static uint32_t s_prog_base;
static uint32_t s_prog_clear;
static uint32_t s_prog_part;

/* Per-swap confirm-poll counters for the two DFXCTL.STATUS waits
 * (DECOUPLE_ASSERT decouple + RELEASE release), reset by
 * swap_fsm_start()/swap_fsm_init(). Kept in swap_fsm.c (the impure half)
 * rather than the pure table: counting polls is a side effect; the table only
 * ever sees the resulting `confirm_timeout` boolean (see
 * MPS3_SWAP_CONFIRM_POLL_MAX). */
static uint32_t s_decouple_polls;
static uint32_t s_verify_polls;      /* bounded wait for dfx_ctl's rm_id_valid */

/* fail-IDLE: SWAP_AWAIT_INCOMING_CLEARING / SWAP_AWAIT_PARTIAL were the only
 * two states with no bound. A client that died mid-swap parked the shell
 * forever -- ICMP fine, 6900/6910 still accept(), but every write reset,
 * because lwIP has no keepalive and config_agent is single-session. Recovery
 * needed a JTAG bitstream reload, on a platform whose premise is that JTAG is a
 * crutch. Observed on silicon 2026-07-09.
 *
 * This is an IDLE timeout: it is re-armed on every byte of RX progress, so a
 * slow-but-progressing 1.31 MB upload is never killed. Only true silence trips
 * it. Generous by default -- a healthy client starts pushing within seconds. */
static uint32_t s_await_deadline_ms;  /* 0 = not armed */
static uint32_t s_await_last_got;
static uint32_t s_reisolate_polls;   /* bounded wait for re-isolation to confirm */
static uint32_t s_release_polls;

/* The INTERNAL source "usd" (D13): the verified default on the user microSD,
 * started only by the overlay store's power-on hook (swap_fsm.h). s_src_usd is
 * set by swap_fsm_start(); s_usd_stream is the store stream THIS swap has open
 * (0 none, else 1 + the ovlstore_sd_which_t), so every exit can abort it;
 * s_usd_desc is the descriptor taken at SWAP_AWAIT_INCOMING_CLEARING.
 *
 * SWAP_CACHE_CLEARING's card read of the slot's clearing into the RAM arena runs
 * ACROSS polls (the card delivers about one block a pass): s_cache_started marks
 * that its stream is open for this episode, s_cache_got counts the bytes copied,
 * and s_cache_polls bounds the wait exactly like the DFXCTL confirm waits
 * (MPS3_SWAP_CONFIRM_POLL_MAX), so a store that never delivers fails the cache
 * closed rather than parking the FSM. It replaces the QSPI STAGE->CACHE promote
 * that D13 deleted with the SST26 backend. */
static int                s_src_usd;
static int                s_usd_stream;
static ovlstore_sd_desc_t s_usd_desc;
static int                s_cache_started;
static uint32_t           s_cache_got;
static uint32_t           s_cache_polls;

static void usd_stream_close(void)
{
    if (s_usd_stream) {
        overlay_store_src_abort();
        s_usd_stream = 0;
    }
}

/* I2 model: the ONE currently-loaded RM's clearing reference (boot-seeded
 * from the greybox, updated at boot-load-default and at each successful
 * swap's SWAP_CACHE_CLEARING). */
mps3_clearing_ref_t g_current_rm_clearing;

/* The resident RM's partial ref (informational since the v0.13 re-push
 * commit) -- see swap_fsm.h. */
mps3_clearing_ref_t g_current_rm_partial;

/* Staged incoming clearing, captured during SWAP_AWAIT_INCOMING_CLEARING,
 * promoted to g_current_rm_clearing at SWAP_CACHE_CLEARING. Kept separate
 * from g_current_rm_clearing itself so a swap that fails part-way through
 * never corrupts the still-valid "current" entry (fail-closed: the
 * currently-loaded RM's clearing must remain known-good until a swap
 * actually completes). */
static mps3_clearing_ref_t s_staged_incoming_clearing;

/* Staged incoming partial ref (info from config_agent's hand-off) --
 * promoted to g_current_rm_partial at SWAP_CACHE_CLEARING, same
 * fail-closed reasoning as the clearing above. */
static mps3_clearing_ref_t s_staged_incoming_partial;

/* 1 when the staged incoming pair came through a streaming sink that holds NO
 * bytes for this module (config_agent's info.in_qspi -- no production build
 * registers such a sink since D13 deleted the QSPI staging). There is nothing to
 * stream or cache from, so the swap fails CLOSED at the step that would need the
 * bytes instead of streaming garbage. */
static int s_incoming_partial_unsourced;
static int s_incoming_clearing_unsourced;

/* 1 when the staged incoming partial was streamed STRAIGHT to HWICAP as it
 * arrived over 6910 (Path 3, config_agent's info.in_icap): the whole payload
 * is ALREADY in the ICAP by the time step_await_partial() takes the hand-off,
 * so step_stream_partial() has nothing left to push — it just marks the stream
 * done. s_staged_incoming_partial.data is NULL in that case. */
static int s_incoming_partial_in_icap;

/* Clearing ARENA: the SINGLE resident buffer holding the current RM's clearing
 * bytes (unless it is the greybox, streamed straight from its baked blob). Since
 * D13 every clearing is RAM-resident: the QSPI clearing CACHE went with the
 * SST26 backend, and the target sizes this arena to the largest clearing
 * (firmware/platform/Makefile SWAP_CLEARING_ARENA_BYTES = 262,144 B >= the
 * ~222 KB multicore clearing; README "Memory honesty").
 *
 * ONE buffer, not a ping-pong: the copy into it is DEFERRED to
 * SWAP_CACHE_CLEARING (after SWAP_VERIFY passes), so a FAILED swap never
 * overwrites the still-valid current clearing (fail-closed). The incoming
 * clearing stays valid in config_agent's slot from take-time to cache-time under
 * the single-client-parked model (coordinator_net.c holds the control connection
 * through the whole swap, so no new pair can be pushed mid-swap); a "usd" swap
 * reads it from the card at cache-time instead. A clearing that does NOT fit is
 * never copied (no overrun): the resident clearing is marked unavailable and the
 * next swap-AWAY fails closed rather than replaying garbage. */
/* The RESIDENT clearing must fit here, or swap-AWAY is impossible:
 * step_cache_clearing() refuses to cache an oversized clearing (correctly -- it
 * will not overrun the arena) and marks it invalid, so the NEXT swap fails closed
 * at SWAP_STREAM_CLEARING. On silicon that is exactly what happened: a 51,952 B
 * clearing against a 4,096 B arena, so regdemo_b -> regdemo_a could never work.
 * Decoupled from MPS3_CFG_AGENT_STAGING_BYTES (config_agent's own staging) so the
 * two can be sized independently; the 512 KiB LMB affords a real one. */
#ifndef MPS3_SWAP_CLEARING_ARENA_BYTES
#define MPS3_SWAP_CLEARING_ARENA_BYTES MPS3_CFG_AGENT_STAGING_BYTES
#endif
static uint8_t s_clearing_arena[MPS3_SWAP_CLEARING_ARENA_BYTES];

/* Last completed swap's outcome -- see swap_fsm.h. */
static mps3_swap_result_t s_last_result;

const mps3_swap_result_t *swap_fsm_last_result(void)
{
    return &s_last_result;
}

uint32_t swap_fsm_icap_bytes(void)
{
    return mps3_hwicap_bytes();
}

/* The panel's programming progress (swap_fsm.h). */
void swap_fsm_progress(mps3_swap_progress_t *out)
{
    if (!out) {
        return;
    }
    memset(out, 0, sizeof(*out));
    out->state = (uint8_t)s_state;
    out->rm    = "";
    if (s_state == SWAP_IDLE) {
        return;
    }
    out->active = true;
    out->rm     = s_target_rm;
    out->done   = mps3_hwicap_bytes() - s_prog_base;
    uint32_t part = s_prog_part;
    if (part == 0u && s_src_usd && s_usd_desc.part_len != 0u) {
        part = s_usd_desc.part_len;          /* the card slot's, known since AWAIT_INCOMING_CLEARING */
    }
    if (part == 0u && s_state == SWAP_AWAIT_PARTIAL) {
        uint32_t got = 0, expect = 0;        /* the partial is arriving: its declared length */
        config_agent_rx_progress(&got, &expect);
        part = expect;
    }
    out->total = part ? s_prog_clear + part : 0u;
}

/* I18(3): raw last NON-ZERO HWICAP_SR seen by the writer's post-DESYNC EOS wait,
 * and whether that wait saw EOS or timed out (MPS3_ICAP_EOS_*). Both are for the
 * JTAG diag mailbox; see the s_icap_eos_status comment. */
uint32_t swap_fsm_icap_sr_last(void)    { return mps3_hwicap_sr_last(); }
uint32_t swap_fsm_icap_eos_status(void) { return s_icap_eos_status; }

void swap_fsm_set_current_clearing(const mps3_clearing_ref_t *ref)
{
    g_current_rm_clearing = *ref;
}

void *swap_fsm_clearing_stage_buffer(uint32_t *cap_out)
{
    if (cap_out) {
        *cap_out = (uint32_t)sizeof(s_clearing_arena);
    }
    return s_clearing_arena;
}

void swap_fsm_set_current_partial(const mps3_clearing_ref_t *ref)
{
    g_current_rm_partial = *ref;
}

void swap_fsm_init(void)
{
    s_state = SWAP_IDLE;
    memset(s_target_rm, 0, sizeof(s_target_rm));
    memset(s_src, 0, sizeof(s_src));
    s_words_done = 0;
    s_words_total = 0;
    s_target_rm_id = 0;
    s_prog_base = 0;
    s_prog_clear = 0;
    s_prog_part = 0;
    memset(&g_current_rm_clearing, 0, sizeof(g_current_rm_clearing));
    memset(&g_current_rm_partial, 0, sizeof(g_current_rm_partial));
    memset(&s_staged_incoming_clearing, 0, sizeof(s_staged_incoming_clearing));
    memset(&s_staged_incoming_partial, 0, sizeof(s_staged_incoming_partial));
    s_incoming_partial_unsourced = 0;
    s_incoming_clearing_unsourced = 0;
    s_incoming_partial_in_icap = 0;
    s_decouple_polls = 0;
    s_verify_polls = 0;
    s_reisolate_polls = 0;
    s_await_deadline_ms = 0;
    s_await_last_got = 0;
    s_release_polls = 0;
    s_src_usd = 0;
    s_usd_stream = 0;
    memset(&s_usd_desc, 0, sizeof(s_usd_desc));
    s_cache_started = 0;
    s_cache_got = 0;
    s_cache_polls = 0;
    memset(&s_last_result, 0, sizeof(s_last_result));

    /* I2 boot seed: the greybox's clearing ships inside the shell image
     * (overlay-manifest.md), so the coordinator's "currently-loaded
     * clearing" at power-on is the greybox's -- see
     * overlay_store_get_greybox_clearing()'s doc comment for where that
     * blob actually comes from (a linker-provided symbol, not SPI). The
     * power-on load from the user microSD, when there is one, is an ordinary
     * swap and replaces it at its SWAP_CACHE_CLEARING like any other. */
    overlay_manifest_info_t greybox;
    if (overlay_store_get_greybox_clearing(&greybox) == 0) {
        mps3_clearing_ref_t seed = {
            .valid     = true,
            .rm_id     = greybox.rm_id,
            .static_id = greybox.static_id,
            .len_words = greybox.clear_len_words,
            .crc32     = greybox.clear_crc32,
            /* The greybox blob ships read-only inside the shell image --
             * its bytes are streamed straight from there, no arena copy
             * needed (NULL in harness builds whose fake doesn't carry
             * bytes; streaming then advances counters only). */
            .data      = greybox.clear_data,
        };
        swap_fsm_set_current_clearing(&seed);
    }
    /* else: TODO(A3) -- greybox clearing genuinely unavailable (blob not
     * linked in yet, this build). g_current_rm_clearing.valid stays false;
     * the first swap attempt will fail closed at SWAP_STREAM_CLEARING
     * rather than silently proceeding without a clearing bitstream (see
     * swap_fsm_next_state()'s clearing_cache_valid guard). */
}

mps3_swap_state_t swap_fsm_state(void) { return s_state; }
bool swap_fsm_idle(void) { return s_state == SWAP_IDLE; }

int swap_fsm_start(const char *rm_name, const char *src)
{
    if (s_state != SWAP_IDLE) {
        return -1; /* a swap is already in flight */
    }
    strncpy(s_target_rm, rm_name, sizeof(s_target_rm) - 1);
    strncpy(s_src, src, sizeof(s_src) - 1);
    s_words_done = 0;
    s_words_total = 0;
    s_target_rm_id = 0; /* resolved once config_agent hands off the partial's header, see step_await_partial() */
    s_prog_base  = mps3_hwicap_bytes();
    s_prog_clear = g_current_rm_clearing.valid ? g_current_rm_clearing.len_words * 4u : 0u;
    s_prog_part  = 0;
    memset(&s_staged_incoming_clearing, 0, sizeof(s_staged_incoming_clearing));
    memset(&s_staged_incoming_partial, 0, sizeof(s_staged_incoming_partial));
    s_incoming_partial_unsourced = 0;
    s_incoming_clearing_unsourced = 0;
    s_incoming_partial_in_icap = 0;
    s_decouple_polls = 0;
    s_verify_polls = 0;
    s_reisolate_polls = 0;
    s_await_deadline_ms = 0;
    s_await_last_got = 0;
    s_release_polls = 0;
    s_src_usd = (strcmp(src, "usd") == 0);
    s_usd_stream = 0;
    memset(&s_usd_desc, 0, sizeof(s_usd_desc));
    s_cache_started = 0;
    s_cache_got = 0;
    s_cache_polls = 0;
    memset(&s_last_result, 0, sizeof(s_last_result)); /* the new swap's outcome isn't known yet */
    s_state = SWAP_GATE;
    return 0;
}

/* ---- per-state step functions --------------------------------------
 * Each gathers/performs whatever belongs to being IN the named state, then
 * defers the actual "what state is next" call to swap_fsm_next_state()
 * (swap_fsm_transitions.c) and only applies side effects that belong to a
 * PARTICULAR transition (e.g. seeding s_words_total when *entering*
 * STREAM_CLEARING) after seeing what the pure function decided.
 */

/* Bounded poll ceiling for the DFXCTL.STATUS confirm waits (DECOUPLE_ASSERT
 * decouple + RELEASE release). Each tick is one main-loop swap_fsm_poll(), NOT
 * a tight inner spin, so this is a COUNT OF SUPERLOOP ITERATIONS: the vendor DFX
 * Decoupler/Shutdown-Manager confirms in a handful of AXI cycles, so any healthy
 * swap confirms in ~1 poll and this ceiling is astronomically generous. Its only
 * job is to convert a decoupler/release that NEVER confirms (dead IP / stuck bus)
 * from an infinite park into a fail-closed SWAP_FAILED. Not a contract value --
 * a bring-up tuning knob, same spirit as MPS3_HWICAP_DONE_POLL_MAX. */
#define MPS3_SWAP_CONFIRM_POLL_MAX 100000u

/* One bounded HWICAP chunk out of `src` (may be NULL — harness fakes carry
 * no bytes; the transfer then advances counters only, preserving the FSM
 * choreography the pure-table tests pin down). The per-call cap is purely
 * MPS3_HWICAP_CHUNK_WORDS so the FSM still yields cooperatively between polls;
 * how those words reach the ICAP (one StartConfig per word, or a WFV-paced FIFO
 * batch) is the writer's business, not this file's.
 *
 * Returns the number of words ACTUALLY CONFIRMED into the ICAP (so s_words_done
 * stays honest) and sets *err=1 if the write stalled — the caller turns that into
 * SWAP_FAILED rather than counting a stuck word as streamed. */
static uint32_t hwicap_push_chunk(const void *src, uint32_t words_done,
                                  uint32_t words_total, int *err)
{
    *err = 0;
    uint32_t remaining = words_total - words_done;
    uint32_t chunk = (remaining < MPS3_HWICAP_CHUNK_WORDS) ? remaining : MPS3_HWICAP_CHUNK_WORDS;
    if (src != NULL && chunk > 0) {
        const uint8_t *bytes = (const uint8_t *)src;
        uint32_t written = 0;
        if (mps3_hwicap_write_words(&bytes[4u * words_done], chunk, &written)
            != MPS3_HWICAP_OK) {
            *err = 1;
            return written; /* only `written` words made it in before the stall */
        }
        return written;
    }
    return chunk;
}

/* ====================================================================== *
 * Path 3 — stream-direct config_agent -> HWICAP PARTIAL sink.
 *
 * As each word of the 6910 partial arrives, it is packed and pushed straight
 * into HWICAP.WF (WFV-paced), computing NO local buffer — this is what removes
 * the 886 KB–1.65 MB partial size limit without RAM or QSPI (see
 * docs/OVER_THE_WIRE_RECONFIG_PLAN.md §5, Path 3). config_agent drives it via
 * the mps3_cfg_agent_qspi_sink_t begin/write/finish/abort seam
 * (config_agent_set_icap_direct_sink()); the running transport CRC that
 * config_agent computes as bytes land (its s_crc_run vs the header) is the
 * reject gate, so this sink needs no CRC of its own.
 *
 * The two known-risk fixes the plan flags (I18 byte-lane order and the
 * LITE-vs-FIFO write protocol) are NOT handled here any more: they are the
 * writer's, once, for every path — common/hwicap_writer.{c,h}. In particular the
 * MSB-first assembly of a config word that STRADDLES two TCP segments is
 * mps3_hwicap_write()'s carried state, so this sink holds no packing state of its
 * own. What remains here is the part that is genuinely the FSM's: the
 * swap-armed-first preconditions below.
 * ====================================================================== */

static int icap_direct_begin(uint32_t total_bytes)
{
    (void)total_bytes;
    /* Path-3 invariant (net-protocol.md swap steps 1-7): the swap must be
     * ARMED first so the FSM has already asserted DECOUPLE + held rp_resetn
     * BEFORE any ICAP write. This sink writes to the ICAP as bytes arrive, so
     * confirm the RP is parked here and fail closed otherwise — never stream
     * config frames into a live RP. (Under the intended host ordering the FSM
     * is in SWAP_AWAIT_PARTIAL, i.e. past SWAP_DECOUPLE_ASSERT, when the
     * partial push starts, so DFXCTL.STATUS reads decoupled+in-reset.) */
    /* FAIL-CLOSED FIX (silicon, 2026-07-10). The STATUS check below confirms the
     * HARDWARE is parked. It does NOT confirm the FSM is expecting a partial --
     * and those are not the same thing. After a swap fails BEFORE release (e.g.
     * SWAP_STREAM_CLEARING's clearing_cache_valid guard fires), DECOUPLE stays
     * asserted and rp_resetn stays held, so STATUS still reads decoupled+in-reset.
     * A partial pushed after that rejection sailed through this gate and was
     * written into the fabric: 1,083,360 bytes of an unverified RM over an
     * un-cleared RP, while the shell still reported the previous rm_id. The
     * fabric and the shell's model disagreed, silently.
     *
     * The swap is armed only when the FSM is actually waiting for the partial. */
    if (s_state != SWAP_AWAIT_PARTIAL) {
        return -1;
    }

    uint32_t status = mps3_reg_read32(MPS3_DFXCTL_BASE, DFXCTL_STATUS);
    if ((status & (DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET))
        != (DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET)) {
        return -1;
    }
    mps3_hwicap_begin();  /* fresh packer + (FIFO) empty batch accumulator */
    return 0;
}

static int icap_direct_write(const void *buf, uint32_t len)
{
    /* Straight to the writer: it assembles MSB-first words across calls (a
     * config word can straddle two of config_agent's <=512-byte TCP chunks) and
     * gets them into the ICAP by whichever protocol this image was built for,
     * never buffering the whole (MiB-scale) partial. A stall rejects here, so
     * config_agent drops the session and the RP stays parked (fail closed) —
     * a word that did not reach the ICAP never passes as written. */
    return (mps3_hwicap_write(buf, len) == MPS3_HWICAP_OK) ? 0 : -1;
}

static int icap_direct_finish(uint32_t expected_crc)
{
    (void)expected_crc; /* config_agent already CRC-checked the streamed bytes
                         * (its running s_crc_run vs the header) BEFORE calling
                         * us and dropped the session on mismatch — the RP is
                         * left parked. The bytes are already in the ICAP by
                         * now, so ICAP's own embedded CRC + the post-load
                         * DFXCTL.RM_ID verify are the belt-and-suspenders. */
    /* One call ends the stream: reject a non-word-aligned tail, drain whatever
     * the writer still holds (FIFO mode buffers; LITE mode never does), then wait
     * — bounded — for the post-DESYNC end-of-sequence. The wait is FAIL-CLOSED
     * (it was fail-OPEN once: the old body returned 0 whether or not EOS ever
     * asserted, so a stuck ICAP that never reached end-of-sequence was reported
     * as a clean finish and the swap "verified" a load that never completed).
     * A timeout returns -1 -> config_agent rejects the partial -> the RP is left
     * parked (DECOUPLE held), and a host retry recovers.
     *
     * The two failure kinds are recorded differently on the diag mailbox: only
     * an EOS timeout is an EOS verdict (I18(3)); an unaligned tail or a stalled
     * flush means the bytes never got in, which is not an EOS observation. */
    int rc = mps3_hwicap_finish();
    if (rc == MPS3_HWICAP_OK) {
        s_icap_eos_status = MPS3_ICAP_EOS_SEEN;      /* EOS asserted post-DESYNC */
        return 0;
    }
    if (rc == MPS3_HWICAP_ERR_EOS) {
        s_icap_eos_status = MPS3_ICAP_EOS_TIMEOUT;   /* bounded wait expired w/o EOS */
    }
    return -1;
}

/* config_agent's DEFER gate: it holds the incoming partial (window-backpressured,
 * session NOT torn) until this returns 1, then arms the ICAP-direct sink — see
 * the mps3_cfg_agent_qspi_sink_t ready() note. This is exactly icap_direct_begin()'s
 * first precondition (s_state == SWAP_AWAIT_PARTIAL); begin() still re-checks it
 * AND the DFXCTL parked-status as the hard hardware gate, so a ready()==1 that
 * somehow raced a state change still fails closed there. The FSM reaches
 * AWAIT_PARTIAL only after the OUTGOING clearing has fully streamed to the ICAP,
 * so this is the precise "ICAP is clear, send the partial now" edge — which is
 * what a large outgoing clearing (DAP RMs) used to arrive too early for. */
static int icap_direct_ready(void)
{
    return s_state == SWAP_AWAIT_PARTIAL;
}

static void icap_direct_abort(void)
{
    /* Torn/rejected mid-stream: whatever words were already pushed are in the
     * fabric, but config_agent never hands the FSM a validated partial, so the
     * FSM stays in SWAP_AWAIT_PARTIAL with DECOUPLE asserted + rp_resetn held —
     * the RP is parked inert (the safe state). Just reset the writer's packer
     * (and, in FIFO mode, discard words buffered but never drained — they never
     * reached the ICAP) so a later stream starts clean. */
    mps3_hwicap_abort();
}

static const mps3_cfg_agent_qspi_sink_t s_icap_direct_sink = {
    .begin  = icap_direct_begin,
    .write  = icap_direct_write,
    .finish = icap_direct_finish,
    .abort  = icap_direct_abort,
    .ready  = icap_direct_ready,
};

const struct mps3_cfg_agent_qspi_sink *swap_fsm_icap_direct_sink(void)
{
    return &s_icap_direct_sink;
}

static void step_gate(void)
{
    /* net-protocol.md step 1: "gate XVC/SWD/UART/VPHY link". These are
     * module-local flags each server checks in its own _poll() rather than a
     * single shared register -- no regmap block covers "gate everything."
     * NOT a no-op for XVC: while g_shell_state.xvc_gated, xvc_server_poll()
     * still ANSWERS `getinfo:` and `settck:` (no hardware is touched); only a
     * completed `shift:` STALLS, un-consumed and unanswered, until the first
     * ungated poll (xvc_server.c, "Gating"). That the stall stays inside
     * hw_server's timeouts is UNMEASURED (the swap's idle timeout is 30 s,
     * swap_fsm.h MPS3_SWAP_AWAIT_IDLE_MS); the host closes and reopens its XVC
     * session around a swap instead of relying on it.
     */
    g_shell_state.xvc_gated  = true;
    g_shell_state.swd_gated  = true;
    g_shell_state.uart_gated = true;
    g_shell_state.link_gated = true;
    /* VPHY link-down injection so the DUT's own MAC sees a clean link drop
     * across the swap, per §6.3 "MAC/VPHY: virtual PHY re-asserts link-up." */
    mps3_reg_write32(MPS3_VPHY_BASE, VPHY_LINK_EVENT, VPHY_LINK_EVENT_FORCE_DOWN);

    mps3_swap_transition_inputs_t in = {0};
    s_state = swap_fsm_next_state(s_state, &in); /* unconditional -> DECOUPLE_ASSERT */
}

static void step_decouple_assert(void)
{
    /* net-protocol.md step 1 (cont'd): "assert DECOUPLE + hold rp_resetn". */
    mps3_reg_set_bits32(MPS3_DFXCTL_BASE, DFXCTL_DECOUPLE, DFXCTL_DECOUPLE_EN);
    mps3_reg_set_bits32(MPS3_DFXCTL_BASE, DFXCTL_SHUTDOWN, DFXCTL_SHUTDOWN_AXI);
    /* rp_resetn is *released*-when-1 per shell-regmap.md CLKRST note, so
     * "hold in reset" = clear the bit.
     *
     * dut_resetn too: it is the DUT's FUNCTIONAL reset (the RP's dut_clk domain),
     * and a previous swap released it. Reconfiguring the fabric underneath a
     * running DUT is exactly what DECOUPLE exists to prevent -- do not leave its
     * logic clocked out of reset while its LUTs are rewritten. */
    mps3_reg_clr_bits32(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL,
                        CLKRST_RESET_CTRL_RP_RESETN | CLKRST_RESET_CTRL_DUT_RESETN
                        | CLKRST_RESET_CTRL_DBG_RESETN);

    mps3_swap_transition_inputs_t in = {0};
    in.decouple_confirmed =
        (mps3_reg_read32(MPS3_DFXCTL_BASE, DFXCTL_STATUS)
         & (DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET))
        == (DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET);
    /* fail-STUCK fix: if the DFX decoupler never confirms, don't park here
     * forever — once the bounded poll expires, hand the table a timeout so it
     * fails closed to SWAP_FAILED (RP left isolated). */
    if (!in.decouple_confirmed && ++s_decouple_polls >= MPS3_SWAP_CONFIRM_POLL_MAX) {
        in.confirm_timeout = true;
    }

    mps3_swap_state_t next = swap_fsm_next_state(s_state, &in);
    if (next == SWAP_STREAM_CLEARING) {
        /* Entering the clearing-stream state: seed the chunk counters from
         * the CACHED outgoing RM's clearing (I2 -- always resident, no
         * network wait needed to get here). */
        s_words_total = g_current_rm_clearing.len_words;
        s_words_done = 0;
    }
    s_state = next;
}

static void step_stream_clearing(void)
{
    /* I2: g_current_rm_clearing is the shell's own cached copy of the
     * currently-loaded RM's clearing bitstream -- always resident by
     * construction (boot-seeded from the greybox, kept current by every
     * successful swap/boot-load). HWICAP streaming per PG134 shape: check
     * write-FIFO vacancy, push up to MPS3_HWICAP_CHUNK_WORDS words, kick
     * CR_WRITE, poll SR_DONE. TODO(A3): actual payload source (DDR
     * pointer / re-read from QSPI) is the location descriptor left
     * abstract in mps3_clearing_ref_t. */
    mps3_swap_transition_inputs_t in = {0};
    in.clearing_cache_valid = g_current_rm_clearing.valid;

    if (in.clearing_cache_valid) {
        /* The resident clearing is always RAM (the greybox blob or the arena):
         * stream from the pointer. */
        int err = 0;
        s_words_done += hwicap_push_chunk(g_current_rm_clearing.data,
                                          s_words_done, s_words_total, &err);
        in.stream_error = (err != 0);
        in.clearing_stream_done = (s_words_done >= s_words_total);
    }
    /* else: clearing_cache_valid is false -- swap_fsm_next_state() fails
     * this closed to SWAP_FAILED (I2 guarantees this shouldn't happen; see
     * swap_fsm_transitions.c). */

    s_state = swap_fsm_next_state(s_state, &in);
}


/* Re-arm on RX progress; report expiry. Wrap-safe: the ms counter rolls over
 * every ~49.7 days, so compare the SIGNED difference, never `now > deadline`. */
static bool await_idle_expired(void)
{
    uint32_t got = 0, expect = 0;
    config_agent_rx_progress(&got, &expect);

    uint32_t now = mps3_sys_now_ms();
    if (s_await_deadline_ms == 0 || got != s_await_last_got) {
        s_await_last_got    = got;             /* progress (or first arm) */
        s_await_deadline_ms = now + MPS3_SWAP_AWAIT_IDLE_MS;
        if (s_await_deadline_ms == 0) {        /* never let 0 mean "armed" */
            s_await_deadline_ms = 1;
        }
        return false;
    }
    return (int32_t)(now - s_await_deadline_ms) >= 0;
}

static void step_await_incoming_clearing(void)
{
    /* I2: the host's swap payload is the INCOMING RM's {clearing, partial}
     * pair. This state captures the pair's clearing bitstream -- NOT
     * streamed to HWICAP this swap, just validated + staged so
     * SWAP_CACHE_CLEARING can promote it to g_current_rm_clearing once the
     * new partial is verified loaded. */
    config_agent_bitstream_info_t info;
    mps3_swap_transition_inputs_t in = {0};

    if (s_src_usd) {
        /* D13 "usd": the pair is the verified default on the card. Its clearing
         * is NOT read now -- like a network clearing it is only needed after the
         * swap verifies, at SWAP_CACHE_CLEARING, which reads it into the arena.
         * A store that no longer offers a VALID default fails the swap here,
         * before a single word of the incoming pair reaches the ICAP. */
        if (overlay_store_src_default(&s_usd_desc) != 0) {
            in.await_timeout = true;   /* -> SWAP_FAILED (the outgoing clearing is in) */
        } else {
            s_staged_incoming_clearing.valid     = true;
            s_staged_incoming_clearing.rm_id     = s_usd_desc.rm_id;
            s_staged_incoming_clearing.static_id = s_usd_desc.static_id;
            s_staged_incoming_clearing.len_words = s_usd_desc.clear_len / 4u;
            s_staged_incoming_clearing.crc32     = s_usd_desc.clear_crc;
            s_staged_incoming_clearing.data      = NULL;
            in.incoming_clearing_ready = true;
        }
        s_state = swap_fsm_next_state(s_state, &in);
        return;
    }

    if (config_agent_take_validated_clearing(&info) == 0) {
        s_staged_incoming_clearing.valid     = true;
        s_staged_incoming_clearing.rm_id     = info.rm_id;
        s_staged_incoming_clearing.static_id = info.static_id;
        s_staged_incoming_clearing.len_words = info.len_words;
        s_staged_incoming_clearing.crc32     = info.crc32;
        /* Keep the pointer INTO config_agent's clearing slot for now; the
         * resident copy into the single-buffer arena is DEFERRED to
         * SWAP_CACHE_CLEARING (after SWAP_VERIFY passes) so a failed swap
         * never overwrites the still-valid current clearing. The slot stays
         * readable from here to cache-time under the single-client-parked
         * invariant (see s_clearing_arena's comment). NULL (harness fake:
         * bookkeeping only) is carried through and skips the copy at cache. */
        s_staged_incoming_clearing.data = info.data;
        /* A clearing staged through a streaming sink has no RAM bytes here: it
         * cannot be cached, so it is carried as UNSOURCED and SWAP_CACHE_CLEARING
         * marks the resident clearing invalid (the next swap-away fails closed). */
        s_incoming_clearing_unsourced = info.in_qspi;
        in.incoming_clearing_ready = true;
    }
    /* else: stay here -- config_agent_poll() is still receiving the
     * clearing half of the incoming pair. */


    in.await_timeout = await_idle_expired();
    s_state = swap_fsm_next_state(s_state, &in);
}

static void step_await_partial(void)
{
    /* net-protocol.md step 3: "stream new RM partial (received via
     * TFTP/6910)". config_agent owns receipt + header validation
     * (magic/static_id/crc/ordering) and enforces clearing-then-partial
     * *protocol* ordering on its side; here we just wait for it to hand
     * off a validated, fully-received partial payload. The partial's own
     * header carries the numeric target rm_id (I14) -- capture it now so
     * SWAP_VERIFY has something concrete to compare DFXCTL.RM_ID against
     * (I25). */
    config_agent_bitstream_info_t info;
    mps3_swap_transition_inputs_t in = {0};

    if (s_src_usd) {
        /* D13 "usd": the partial comes from the card, from the descriptor taken
         * at SWAP_AWAIT_INCOMING_CLEARING. Its stream opens at the first
         * SWAP_STREAM_PARTIAL poll. */
        s_target_rm_id = s_usd_desc.rm_id;
        s_words_total  = s_usd_desc.part_len / 4u;
        s_words_done   = 0;
        s_prog_part    = s_words_total * 4u;
        s_staged_incoming_partial.valid     = true;
        s_staged_incoming_partial.rm_id     = s_usd_desc.rm_id;
        s_staged_incoming_partial.static_id = s_usd_desc.static_id;
        s_staged_incoming_partial.len_words = s_words_total;
        s_staged_incoming_partial.crc32     = s_usd_desc.part_crc;
        s_staged_incoming_partial.data      = NULL;
        in.partial_ready = true;
        s_state = swap_fsm_next_state(s_state, &in);
        return;
    }

    if (config_agent_take_validated_partial(&info) == 0) {
        s_target_rm_id = info.rm_id;
        s_words_total = info.len_words;
        s_words_done = 0;
        s_prog_part = info.len_words * 4u;
        s_staged_incoming_partial.valid     = true;
        s_staged_incoming_partial.rm_id     = info.rm_id;
        s_staged_incoming_partial.static_id = info.static_id;
        s_staged_incoming_partial.len_words = info.len_words;
        s_staged_incoming_partial.crc32     = info.crc32;
        /* No copy: the partial streams to HWICAP within THIS swap, before
         * any new push can plausibly land (single receive session) — see
         * config_agent.h's `data` lifetime note. A partial staged through a
         * streaming sink with no bytes for us (info.in_qspi) is UNSOURCED and
         * fails the swap closed at SWAP_STREAM_PARTIAL. */
        s_staged_incoming_partial.data      = info.data;
        s_incoming_partial_unsourced        = info.in_qspi;
        /* Path 3: a stream-direct partial is ALREADY fully in the ICAP by the
         * time we take this hand-off (config_agent's ICAP-direct sink pushed
         * every word as it arrived during this same SWAP_AWAIT_PARTIAL wait),
         * so SWAP_STREAM_PARTIAL will have nothing left to push. */
        s_incoming_partial_in_icap          = info.in_icap;
        in.partial_ready = true;
    }
    /* else: stay here -- config_agent_poll() (called every main-loop
     * iteration alongside swap_fsm_poll()) is still receiving bytes. */


    in.await_timeout = await_idle_expired();
    s_state = swap_fsm_next_state(s_state, &in);
}

static void step_stream_partial(void)
{
    /* Mirrors step_stream_clearing() against the staged partial payload
     * (captured by step_await_partial()). Three byte sources:
     *   - ICAP-direct (Path 3, in_icap): NOTHING to do — config_agent already
     *     streamed the whole partial straight into HWICAP.WF as it arrived over
     *     6910, so the transfer is complete; just mark it done and advance to
     *     SWAP_VERIFY (the DFXCTL.RM_ID compare is the correctness gate for a
     *     stream that ICAP's embedded CRC didn't already reject).
     *   - RAM fast path: hwicap_push_chunk() from the staging-slot pointer;
     *   - the user microSD (D13 "usd"): the store re-reads the slot's partial
     *     and CRCs it again as it hands it out, a buffer at a time, paced by the
     *     writer (mps3_hwicap_pace_words) so one poll stays short. It WITHHOLDS
     *     the region's last buffer unless the re-read CRC matches, so a card that
     *     went bad since the verify never completes the bitstream: the stream
     *     errors, and the swap fails closed with the RP parked. */
    if (s_incoming_partial_in_icap) {
        s_words_done = s_words_total;
        mps3_swap_transition_inputs_t in_icap = {0};
        in_icap.partial_stream_done = true;
        s_state = swap_fsm_next_state(s_state, &in_icap);
        return;
    }
    mps3_swap_transition_inputs_t in = {0};
    if (s_incoming_partial_unsourced) {
        in.stream_error = true;   /* no byte source: fail closed, nothing streamed */
    } else if (s_src_usd) {
        if (!s_usd_stream) {
            if (overlay_store_src_begin(OVLSD_PARTIAL) != 0) {
                in.stream_error = true;
            } else {
                s_usd_stream = 1 + (int)OVLSD_PARTIAL;
            }
        }
        if (s_usd_stream) {
            uint32_t pace = mps3_hwicap_pace_words(MPS3_HWICAP_CHUNK_WORDS);
            const uint8_t *p = NULL;
            uint32_t n = 0;
            int rc = (pace > 0u) ? overlay_store_src_next(pace * 4u, &p, &n) : OVLSD_BUSY;
            if (rc == OVLSD_OK && n > 0u) {
                uint32_t written = 0;
                if ((n & 3u) != 0u ||
                    s_words_done + n / 4u > s_words_total ||
                    mps3_hwicap_write_words(p, n / 4u, &written) != MPS3_HWICAP_OK) {
                    in.stream_error = true;
                }
                s_words_done += written;
            } else if (rc == OVLSD_DONE) {
                s_usd_stream = 0;   /* the store closed it: every byte handed out */
                if (s_words_done != s_words_total) {
                    in.stream_error = true;
                }
            } else if (rc < 0) {
                s_usd_stream = 0;   /* the store ended it (CRC, card gone, ...) */
                in.stream_error = true;
            }
            /* OVLSD_BUSY: the card has not delivered yet -- next poll. */
        }
        if (in.stream_error) {
            usd_stream_close();
        }
        in.partial_stream_done = !in.stream_error && !s_usd_stream &&
                                 (s_words_done >= s_words_total);
        s_state = swap_fsm_next_state(s_state, &in);
        return;
    } else {
        int err = 0;
        s_words_done += hwicap_push_chunk(s_staged_incoming_partial.data,
                                          s_words_done, s_words_total, &err);
        in.stream_error = (err != 0);
    }

    in.partial_stream_done = (s_words_done >= s_words_total);
    s_state = swap_fsm_next_state(s_state, &in);
}

static void step_verify(void)
{
    /* I25 FIX: shell-regmap.md v0.1 "RM-load verify" -- read DFXCTL.RM_ID
     * (0x44A1_0010, real offset, was a PLACEHOLDER) + RM_STATUS.rm_id_valid
     * and ACTUALLY compare against s_target_rm_id (from the partial's own
     * header, see step_await_partial()). This used to hardcode
     * `verified = true` regardless (the documented I25 gap /
     * test_verify_state_currently_always_reports_success_TODO canary in
     * tests/integration/test_swap_sequence.py) -- that gap is closed here. */
    uint32_t rm_id     = mps3_reg_read32(MPS3_DFXCTL_BASE, DFXCTL_RM_ID);
    uint32_t rm_status = mps3_reg_read32(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS);
    bool     id_valid  = (rm_status & DFXCTL_RM_STATUS_RM_ID_VALID) != 0;

    mps3_swap_transition_inputs_t in = {0};
    /* rm_id_valid is dfx_ctl's settle qualifier: it asserts only once the
     * synchronized word has been byte-for-byte stable, AND only while the RP is
     * connected and out of reset. Until it asserts, `rm_id` is meaningless --
     * treat "not yet valid" as "keep polling", NOT as a mismatch. */
    in.verify_ok       = id_valid && (rm_id == s_target_rm_id);
    in.verify_mismatch = id_valid && (rm_id != s_target_rm_id);
    if (!id_valid && ++s_verify_polls >= MPS3_SWAP_CONFIRM_POLL_MAX) {
        in.confirm_timeout = true;   /* an RM that never presents an id */
    }

    mps3_swap_state_t next = swap_fsm_next_state(s_state, &in);
    if (next == SWAP_CACHE_CLEARING) {
        /* THE commit point. Only now -- with a settled, correct rm_id read back
         * through the decoupler's clamp -- do we let the RP master the bus, and
         * only now do we let the DUT RUN.
         *
         * dut_resetn was never released by the swap path at all. It went
         * unnoticed because every DUT proven so far (greybox, regdemo_a/b, led)
         * exposes a constant rm_id tie-off and no logic: rm_id reads back fine
         * with the DUT held in reset, so the swap "passed". rm_uart_echo is the
         * first DUT with logic, and on first contact it emitted nothing and
         * echoed nothing -- CLKRST.RESET_CTRL read 0x2 (rp released, dut held).
         *
         * Released HERE, not at RELEASE: an unverified RM must never be clocked.
         * The host can still assert/pulse it via {"op":"reset","target":"dut"}. */

        /* dbg_resetn too: nanoSoC's wrapper ANDs all three resets into the SoC's
         * single sys_sysresetn (rp_nanosoc_wrapper.sv "sys_sysresetn = dut & rp &
         * dbg"), and dbg_resetn is released ONLY by swd_server's srst handler. So
         * with no debugger attached, a freshly-swapped nanoSoC would sit at
         * RESET_CTRL=0x3 -> sys_sysresetn=0 -> whole core held in reset: dead DAP,
         * no boot, no UART. rm_uart_echo hid this because its FIFO uses only
         * dut_resetn. dbg_resetn's rest state IS released (srst deasserted); this
         * makes that true after a swap. swd_server still owns it for real srst
         * pulses. (test_swd_server already ASSUMES it starts released -- the test
         * and the shipped init had diverged.) */
        mps3_reg_clr_bits32(MPS3_DFXCTL_BASE, DFXCTL_SHUTDOWN, DFXCTL_SHUTDOWN_AXI);
        mps3_reg_set_bits32(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL,
                            CLKRST_RESET_CTRL_DUT_RESETN | CLKRST_RESET_CTRL_DBG_RESETN);
        g_shell_state.current_rm_id = rm_id;
    }
    s_state = next;
}

/* R1: a verification failure now happens with the RP CONNECTED, so put it back
 * in the box before we report. Mirrors step_decouple_assert()'s writes. */
static void step_reisolate(void)
{
    mps3_reg_set_bits32(MPS3_DFXCTL_BASE, DFXCTL_SHUTDOWN, DFXCTL_SHUTDOWN_AXI);
    mps3_reg_set_bits32(MPS3_DFXCTL_BASE, DFXCTL_DECOUPLE, DFXCTL_DECOUPLE_EN);
    /* Hold BOTH: an RM that failed verification must not be left running. */
    mps3_reg_clr_bits32(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL,
                        CLKRST_RESET_CTRL_RP_RESETN | CLKRST_RESET_CTRL_DUT_RESETN
                        | CLKRST_RESET_CTRL_DBG_RESETN);

    /* PRE-EXISTING GAP, deliberately NOT changed here: g_shell_state.current_rm_id
     * still reports the PREVIOUS rm_id, but STREAM_PARTIAL already overwrote the
     * fabric, so that RM is gone. `ping` therefore names an RM that is not
     * resident. This predates the R1 reorder (the old order also reconfigured
     * before verifying) and 0 would be no better -- 0 is greybox's id, and the
     * fabric holds a broken image, not greybox. Reporting it honestly needs an
     * "unknown" encoding in the ping schema. Same applies to g_current_rm_clearing,
     * which still describes the previous RM's clearing rather than the one that
     * would actually clear what is now in the fabric. Tracked separately.
     *
     * Re-gate defensively. Under the R1 ordering the ungate lives on the DONE
     * arc, so these are already true on every failure path; keeping the writes
     * means a future reordering cannot silently leak an open channel. */
    g_shell_state.xvc_gated  = true;
    g_shell_state.swd_gated  = true;
    g_shell_state.uart_gated = true;
    g_shell_state.link_gated = true;

    mps3_swap_transition_inputs_t in = {0};
    const uint32_t isolated = DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET;
    in.decouple_confirmed =
        (mps3_reg_read32(MPS3_DFXCTL_BASE, DFXCTL_STATUS) & isolated) == isolated;
    if (!in.decouple_confirmed && ++s_reisolate_polls >= MPS3_SWAP_CONFIRM_POLL_MAX) {
        in.confirm_timeout = true;
    }
    s_state = swap_fsm_next_state(s_state, &in);
}

static void step_cache_clearing(void)
{
    /* net-protocol.md step 5: "cache the new RM's clearing bitstream as
     * the current one (from its pair)" -- promote what
     * SWAP_AWAIT_INCOMING_CLEARING staged into g_current_rm_clearing. This
     * only runs after SWAP_VERIFY succeeds, so a failed swap never
     * clobbers the still-valid previous g_current_rm_clearing entry.
     *
     * Two byte sources:
     *   - the user microSD (D13 "usd"): the slot's clearing is read from the
     *     card into the arena across polls (below);
     *   - RAM (a network swap): copy the bytes OUT of config_agent's slot into
     *     the resident single-buffer arena.
     * Doing either only on the success path is what keeps the arena
     * fail-closed: a swap that fails before here leaves the previous current
     * clearing untouched. */
    if (s_src_usd) {
        /* D13 "usd": read the slot's CLEARING from the card into the RAM arena,
         * across polls (the store re-reads and re-CRCs it and withholds the last
         * buffer on a mismatch). Only then does the arena hold it: nothing points
         * at the card after the swap. The RP is already loaded and verified, so
         * this is off the critical path. Any failure -- a store error, a clearing
         * that does not fit, a wait that never ends -- marks the resident
         * clearing INVALID: this swap still succeeded, and the next swap-AWAY
         * fails closed at SWAP_STREAM_CLEARING instead of replaying garbage. */
        uint32_t cap = 0;
        uint8_t *dst = (uint8_t *)swap_fsm_clearing_stage_buffer(&cap);
        uint32_t need = s_staged_incoming_clearing.len_words * 4u;
        int finished = 0, failed = 0;

        if (!s_cache_started) {
            s_cache_started = 1;
            s_cache_got = 0;
            s_cache_polls = 0;
            if (need > cap || overlay_store_src_begin(OVLSD_CLEARING) != 0) {
                failed = 1;
            } else {
                s_usd_stream = 1 + (int)OVLSD_CLEARING;
                return;   /* opened; the first buffer comes on a later poll */
            }
        } else {
            const uint8_t *p = NULL;
            uint32_t n = 0;
            int rc = overlay_store_src_next(cap - s_cache_got, &p, &n);
            if (rc == OVLSD_OK) {
                if (n > cap - s_cache_got) {
                    failed = 1;
                } else {
                    memcpy(dst + s_cache_got, p, n);
                    s_cache_got += n;
                }
            } else if (rc == OVLSD_DONE) {
                s_usd_stream = 0;
                if (s_cache_got == need) {
                    finished = 1;
                } else {
                    failed = 1;
                }
            } else if (rc < 0) {
                s_usd_stream = 0;
                failed = 1;
            }
            if (!finished && !failed && ++s_cache_polls >= MPS3_SWAP_CONFIRM_POLL_MAX) {
                failed = 1;   /* a store that never delivers: bounded, fail closed */
            }
        }
        if (!finished && !failed) {
            return;   /* more of the clearing to come -- stay in SWAP_CACHE_CLEARING */
        }
        usd_stream_close();
        s_cache_started = 0;
        if (finished) {
            s_staged_incoming_clearing.data = dst;
        } else {
            MPS3_SWAP_LOG("swap: usd clearing not cached; swap-away needs a re-push\r\n");
            s_staged_incoming_clearing.data  = NULL;
            s_staged_incoming_clearing.valid = false;
        }
    } else if (s_incoming_clearing_unsourced) {
        /* Staged through a streaming sink with no bytes for us: not cacheable. */
        s_staged_incoming_clearing.data  = NULL;
        s_staged_incoming_clearing.valid = false;
    } else if (s_staged_incoming_clearing.data != NULL) {
        uint32_t cap = 0;
        void *dst = swap_fsm_clearing_stage_buffer(&cap);
        uint32_t n = s_staged_incoming_clearing.len_words * 4u;
        if (n <= cap) {
            memcpy(dst, s_staged_incoming_clearing.data, (size_t)n);
            s_staged_incoming_clearing.data = dst;
        } else {
            /* The incoming clearing RAM-staged in config_agent's (possibly
             * larger) clearing slot — MPS3_CFG_AGENT_CLEARING_RAM_BYTES — but it
             * does NOT fit this module's resident arena
             * (MPS3_SWAP_CLEARING_ARENA_BYTES). Do NOT copy (never overrun the arena): mark the
             * current clearing UNAVAILABLE (valid=false) so a later swap-AWAY
             * from this RM fails closed at SWAP_STREAM_CLEARING
             * (clearing_cache_valid==false) instead of replaying a truncated /
             * empty clearing. Fail-closed is preserved: the swap that just
             * loaded this RM still succeeds — its OUTGOING clearing was already
             * streamed before CACHE, so skipping this copy corrupts nothing in
             * use; only caching-for-future-reuse is lost, and a re-push of this
             * RM's pair restores it. (Cannot occur when CLEARING_RAM_BYTES ==
             * STAGING_BYTES, i.e. every host test build.) */
            MPS3_SWAP_LOG("swap: clearing not cached (%u B > arena %u B); "
                          "swap-away needs re-push\r\n",
                          (unsigned)n, (unsigned)cap);
            s_staged_incoming_clearing.data  = NULL;
            s_staged_incoming_clearing.valid = false;
        }
    }
    swap_fsm_set_current_clearing(&s_staged_incoming_clearing);
    memset(&s_staged_incoming_clearing, 0, sizeof(s_staged_incoming_clearing));

    /* The partial that just verified-in is now the resident one — promote
     * its ref too (commit's byte source; a failed swap never gets here, so
     * g_current_rm_partial keeps describing what is really loaded). */
    swap_fsm_set_current_partial(&s_staged_incoming_partial);
    memset(&s_staged_incoming_partial, 0, sizeof(s_staged_incoming_partial));

    mps3_swap_transition_inputs_t in = {0};
    mps3_swap_state_t next = swap_fsm_next_state(s_state, &in); /* uncond. -> DONE */
    if (next == SWAP_DONE) {
        /* Ungate moved here from step_release() by the R1 reorder. Gating is
         * released only after the rm_id confirmed the right RM is resident, so
         * the debug/console channels are never opened onto an unverified RM.
         * (Previously RELEASE reached DONE directly and ungated there.) */
        g_shell_state.xvc_gated  = false;
        g_shell_state.swd_gated  = false;
        g_shell_state.uart_gated = false;
        g_shell_state.link_gated = false;
        /* Re-assert link-up so the DUT's PHY bring-up sees a fresh link
         * event rather than a stale force_down (§6.3). */
        mps3_reg_write32(MPS3_VPHY_BASE, VPHY_LINK_EVENT, 0);
    }
    s_state = next;
}

static void step_release(void)
{
    /* net-protocol.md step 6: "release DECOUPLE, deassert rp_resetn". */
    mps3_reg_clr_bits32(MPS3_DFXCTL_BASE, DFXCTL_DECOUPLE, DFXCTL_DECOUPLE_EN);
    mps3_reg_set_bits32(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL, CLKRST_RESET_CTRL_RP_RESETN);
    /* NOTE: AXI shutdown stays ASSERTED across RELEASE. It is cleared at the
     * commit point in step_verify(), once the rm_id proves the right RM loaded.
     * So an unverified RM can drive its id (which is how we identify it) but
     * cannot master the bus. Inert today -- the RP's S_AXI/M_AXI are left
     * unconnected in shell_bd.tcl -- and load-bearing the moment they are not. */

    mps3_swap_transition_inputs_t in = {0};
    in.release_confirmed =
        (mps3_reg_read32(MPS3_DFXCTL_BASE, DFXCTL_STATUS)
         & (DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET)) == 0;
    /* fail-STUCK fix (symmetric to DECOUPLE_ASSERT): a release that never
     * confirms must not park the client forever — time out to SWAP_FAILED. */
    if (!in.release_confirmed && ++s_release_polls >= MPS3_SWAP_CONFIRM_POLL_MAX) {
        in.confirm_timeout = true;
    }

    /* R1 ORDERING: RELEASE no longer reaches DONE -- it reaches VERIFY. The
     * ungate that used to live here has moved to step_cache_clearing()'s DONE
     * arc, which is also strictly safer: the debug/console channels are never
     * opened onto an RM whose identity has not been confirmed. */
    s_state = swap_fsm_next_state(s_state, &in);
}

static void step_done_or_failed(bool ok)
{
    /* net-protocol.md step 7: "respond with confirmed rm_id + verified".
     * The DATA half is real now: the outcome is recorded in s_last_result
     * for coordinator_swap_final_response() to encode (W-JSON). The
     * TRANSPORT half is still TODO(A3)/W-NET-SEAM: reaching back to
     * whichever TCP 6900 connection issued the `swap` op needs a
     * pending-response handle (pcb pointer or opaque token) threaded from
     * coordinator_handle_swap() through swap_fsm_start() into here --
     * left open, since it depends on how coordinator.c's TCP glue ends up
     * shaped once lwIP is actually wired in. `verified` deliberately
     * equals `ok` in v1: SWAP_DONE is only reachable through a passed I25
     * verify, and a failed verify is exactly SWAP_FAILED. */
    s_last_result.valid    = true;
    s_last_result.ok       = ok;
    s_last_result.verified = ok;
    /* A "usd" swap that ended with a store stream still open (a failure part-way
     * through the partial): close it, so the store is free again. */
    usd_stream_close();

    if (!ok) {
        /* A push session outlives its swap only as garbage: begin_payload()
         * fails closed once the FSM is no longer armed. Free it, or a peer that
         * died without FIN holds config_agent's ONE session forever and every
         * later client is refused (lwIP has no keepalive). The AWAIT_* idle
         * timeout gets us here; it cannot free the socket by itself.
         * Not done on success: the client has already shut down and drained. */
        config_agent_abort_session();
    }
    s_last_result.rm_id    = g_shell_state.current_rm_id;
    /* SWAP_FAILED design note: on failure we deliberately do NOT run
     * step_release() -- the RP is left decoupled + held in reset (the
     * "safe state," matching the §13 robustness theme of never leaving a
     * half-configured RP live on the shared AXI bus) rather than attempting
     * an automatic rollback to the previous RM. No contract doc specifies
     * swap-failure recovery behavior explicitly; a host-issued retry
     * (`swap` again) or `reset` is required to leave SWAP_FAILED's
     * decoupled state. Flag for A6/A4: should a failed swap auto-restore
     * the *previous* RM instead of parking decoupled? Note also that a
     * failed swap never reaches SWAP_CACHE_CLEARING, so
     * g_current_rm_clearing (still describing the RM that's actually
     * loaded, since it was never cleared out -- SWAP_FAILED can only be
     * reached AFTER SWAP_STREAM_CLEARING at the earliest) is left
     * untouched, which is the correct fail-safe: it still names whatever
     * is really resident in the RP. */

    if (!ok) {
        /* UNGATE on failure too. The RP is deliberately left inert here
         * (decoupled + held in reset) -- THAT is the safety. Leaving the
         * debug/console channels gated ON TOP of an inert RP is not extra
         * safety, it is a lockout: a swap that passed SWAP_GATE and then failed
         * would leave XVC/SWD/UART gated FOREVER, since only the DONE arc
         * ungates. There is no host op to clear it -- recovery needed a
         * reflash. (Found the hard way: a swap that fail-closed at
         * STREAM_CLEARING left SWD gated, so OpenOCD's 6920 session was dropped
         * on connect -- swd_server.c drops while g_shell_state.swd_gated.)
         *
         * Safe to ungate: the decoupler clamps every RP output to its idle
         * value while decoupled, so a host that connects now sees clamped/idle
         * signals, not a half-configured RP. The channels' poll loops
         * (swd_server/xvc_server/uart_over_eth) simply become reachable again.
         * link stays gated -- there is no verified RM to bring a PHY up for;
         * the next successful swap re-asserts VPHY_LINK_EVENT on its DONE arc. */
        g_shell_state.xvc_gated  = false;
        g_shell_state.swd_gated  = false;
        g_shell_state.uart_gated = false;
    }

    mps3_swap_transition_inputs_t in = {0};
    s_state = swap_fsm_next_state(s_state, &in); /* unconditional -> IDLE */
}

/* `stats` telemetry (swap_fsm.h). */
static uint32_t          s_completed;
static mps3_swap_state_t s_fail_tag = SWAP_IDLE;  /* IDLE == "no failure yet" */
static mps3_swap_state_t s_fail_origin = SWAP_IDLE; /* where a REISOLATE began */

const char *swap_fsm_state_name(mps3_swap_state_t st)
{
    /* Indexed by the enum's NUMERIC value (SWAP_REISOLATE is appended last in
     * the enum, so it is last here). Wire strings: never rename one. */
    static const char *const names[] = {
        "idle", "gate", "decouple", "stream_clearing", "await_clearing",
        "await_partial", "stream_partial", "verify", "cache_clearing",
        "release", "done", "failed", "reisolate",
    };
    if ((unsigned)st >= sizeof(names) / sizeof(names[0])) {
        return "?";
    }
    return names[(unsigned)st];
}

uint32_t swap_fsm_completed(void) { return s_completed; }

const char *swap_fsm_fail_tag(void)
{
    return (s_fail_tag == SWAP_IDLE) ? "" : swap_fsm_state_name(s_fail_tag);
}

mps3_swap_state_t swap_fsm_fail_state(void)
{
    return s_fail_tag;
}

static void swap_fsm_poll_step(void);

void swap_fsm_poll(void)
{
    const mps3_swap_state_t before = s_state;

    swap_fsm_poll_step();

    /* Capture-only bookkeeping on the transition just taken. */
    if (s_state == SWAP_REISOLATE && before != SWAP_REISOLATE) {
        s_fail_origin = before;          /* the state that FOUND the failure */
    }
    if (s_state == SWAP_FAILED && before != SWAP_FAILED) {
        s_fail_tag = (before == SWAP_REISOLATE) ? s_fail_origin : before;
    }
    if ((before == SWAP_DONE || before == SWAP_FAILED) && s_state == SWAP_IDLE) {
        s_completed++;
    }
}

static void swap_fsm_poll_step(void)
{
    switch (s_state) {
    case SWAP_IDLE:                     return; /* nothing to do */
    case SWAP_GATE:                     step_gate();                     break;
    case SWAP_DECOUPLE_ASSERT:          step_decouple_assert();          break;
    case SWAP_STREAM_CLEARING:          step_stream_clearing();          break;
    case SWAP_AWAIT_INCOMING_CLEARING:  step_await_incoming_clearing();  break;
    case SWAP_AWAIT_PARTIAL:            step_await_partial();            break;
    case SWAP_STREAM_PARTIAL:           step_stream_partial();           break;
    case SWAP_VERIFY:                   step_verify();                   break;
    case SWAP_CACHE_CLEARING:           step_cache_clearing();           break;
    case SWAP_RELEASE:                  step_release();                  break;
    case SWAP_REISOLATE:                step_reisolate();                break;
    case SWAP_DONE:                     step_done_or_failed(true);       break;
    case SWAP_FAILED:                   step_done_or_failed(false);      break;
    default:                            s_state = SWAP_IDLE;             break;
    }
}
