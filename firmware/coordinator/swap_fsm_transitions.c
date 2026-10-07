/*
 * swap_fsm_transitions.c — the swap FSM's pure transition table.
 *
 * Deliberately the ONLY thing this file includes is swap_fsm.h (which
 * itself only pulls in stdint/stdbool/net_proto.h -- no platform_regs.h, no
 * config_agent.h, no overlay_store.h, no coordinator.h). That makes
 * `swap_fsm_next_state()` compilable and linkable completely standalone:
 * firmware/test/test_swap_fsm.c builds and links *only* this file to
 * exercise every transition in the table, with no mock register file and
 * no fake config_agent/overlay_store needed. swap_fsm.c (the impure poll
 * loop) is the only other place that calls this function.
 */
#include "swap_fsm.h"

mps3_swap_state_t swap_fsm_next_state(mps3_swap_state_t cur,
                                       const mps3_swap_transition_inputs_t *in)
{
    switch (cur) {
    case SWAP_IDLE:
        /* Only swap_fsm_start() leaves IDLE (an external command, not a
         * polled condition) -- the table itself is a self-loop here. */
        return SWAP_IDLE;

    case SWAP_GATE:
        /* Unconditional: gating XVC/SWD/UART/VPHY is a one-shot register
         * poke with no confirmation handshake in any contract doc. */
        return SWAP_DECOUPLE_ASSERT;

    case SWAP_DECOUPLE_ASSERT:
        if (in->decouple_confirmed) {
            return SWAP_STREAM_CLEARING;
        }
        /* fail-STUCK fix: a DFX decoupler/shutdown-manager that never confirms
         * DECOUPLED+RP_IN_RESET must not park the client forever -- once the
         * bounded confirm poll expires, fail closed (RP stays isolated). */
        return in->confirm_timeout ? SWAP_FAILED : SWAP_DECOUPLE_ASSERT;

    case SWAP_STREAM_CLEARING:
        /* I2 guarantees the shell always holds the currently-loaded RM's
         * clearing bitstream; an invalid cache here is a bug, not a normal
         * "wait for the network" path -- fail closed rather than stall
         * forever waiting on something that will never arrive. */
        if (!in->clearing_cache_valid) {
            return SWAP_FAILED;
        }
        /* fail-OPEN fix: an HWICAP word whose write never completed (SR_DONE /
         * CR self-clear timeout) must fail the swap, never be counted as
         * "streamed" -- otherwise a stuck ICAP silently "verifies" a load that
         * never happened. */
        if (in->stream_error) {
            return SWAP_FAILED;
        }
        return in->clearing_stream_done ? SWAP_AWAIT_INCOMING_CLEARING : SWAP_STREAM_CLEARING;

    case SWAP_AWAIT_INCOMING_CLEARING:
        if (in->incoming_clearing_ready) {
            return SWAP_AWAIT_PARTIAL;
        }
        /* fail-IDLE fix: a payload that never arrives must not park the client
         * forever. -> FAILED (not REISOLATE): we are still pre-RELEASE, so
         * DECOUPLE is asserted and rp_resetn held. The RP is already inert. */
        return in->await_timeout ? SWAP_FAILED : SWAP_AWAIT_INCOMING_CLEARING;

    case SWAP_AWAIT_PARTIAL:
        if (in->partial_ready) {
            return SWAP_STREAM_PARTIAL;
        }
        return in->await_timeout ? SWAP_FAILED : SWAP_AWAIT_PARTIAL;

    case SWAP_STREAM_PARTIAL:
        /* Same fail-OPEN fix as STREAM_CLEARING: a stuck HWICAP write fails the
         * swap rather than being silently treated as a completed stream. */
        if (in->stream_error) {
            return SWAP_FAILED;
        }
        /* R1 ORDERING: -> RELEASE, not VERIFY. RM_ID cannot be read while the
         * decoupler is asserted, because the decoupler CLAMPS rm_id to 0. That
         * clamp is the whole point of R1 -- it is what turns the post-swap
         * RM_ID read into a real confirmation instead of the RP's own bits fed
         * straight back through an inert decoupler. To observe the RP's id you
         * must connect the RP. So: release first, verify second. */
        return in->partial_stream_done ? SWAP_RELEASE : SWAP_STREAM_PARTIAL;

    case SWAP_RELEASE:
        if (in->release_confirmed) {
            return SWAP_VERIFY;
        }
        /* fail-STUCK fix (symmetric to DECOUPLE_ASSERT): a release that never
         * confirms decoupled==0 && rp_in_reset==0 fails closed once the bounded
         * poll expires, rather than leaving the client parked forever.
         * -> FAILED, not REISOLATE: the release never took effect, so the RP is
         * still decoupled and still held in reset. It is already inert. */
        return in->confirm_timeout ? SWAP_FAILED : SWAP_RELEASE;

    case SWAP_VERIFY:
        /* I25: a real compare feeds `verify_ok` (no more hardcoded
         * `verified = true`) -- see swap_fsm.c's step_verify().
         * Runs with the RP CONNECTED, so dfx_ctl's rm_id_valid can assert (it
         * is gated on ~decoupled && ~rp_in_reset). Three outcomes:
         *   settled and right -> commit
         *   settled but wrong -> re-isolate, then fail
         *   not settled yet   -> keep polling until the bounded timeout */
        if (in->verify_ok) {
            return SWAP_CACHE_CLEARING;
        }
        if (in->verify_mismatch || in->confirm_timeout) {
            return SWAP_REISOLATE;
        }
        return SWAP_VERIFY;

    case SWAP_CACHE_CLEARING:
        /* Unconditional: promoting the already-validated staged clearing
         * into g_current_rm_clearing is a same-cycle bookkeeping update,
         * not a hardware handshake. Still strictly AFTER verify, so a rejected
         * RM never clobbers the previous, still-valid clearing. */
        return SWAP_DONE;

    case SWAP_REISOLATE:
        /* Put a verification-failed RM back in the box before reporting the
         * failure, restoring the "FAILED => RP inert" invariant. A re-isolate
         * that never confirms still fails: there is nothing further we can do
         * from here, and parking the client forever is strictly worse. */
        if (in->decouple_confirmed || in->confirm_timeout) {
            return SWAP_FAILED;
        }
        return SWAP_REISOLATE;

    case SWAP_DONE:
        return SWAP_IDLE;

    case SWAP_FAILED:
        return SWAP_IDLE;

    default:
        return SWAP_IDLE;
    }
}
