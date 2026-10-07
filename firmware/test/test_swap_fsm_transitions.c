/*
 * test_swap_fsm_transitions.c — host-gcc unit tests for
 * coordinator/swap_fsm_transitions.c's swap_fsm_next_state(), the pure
 * swap-FSM transition table (I2/I25-resolved model). Links
 * swap_fsm_transitions.c ONLY -- no mock_regs, no config_agent/
 * overlay_store stand-ins, no coordinator.h g_shell_state needed at all.
 * See Makefile.
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../coordinator/swap_fsm.h"

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

static mps3_swap_transition_inputs_t no_inputs(void)
{
    mps3_swap_transition_inputs_t in;
    memset(&in, 0, sizeof(in));
    return in;
}

static void test_idle_self_loops(void)
{
    mps3_swap_transition_inputs_t in = no_inputs();
    CHECK(swap_fsm_next_state(SWAP_IDLE, &in) == SWAP_IDLE);
    in.verify_ok = true; in.partial_ready = true; /* irrelevant to IDLE */
    CHECK(swap_fsm_next_state(SWAP_IDLE, &in) == SWAP_IDLE);
}

static void test_gate_is_unconditional(void)
{
    mps3_swap_transition_inputs_t in = no_inputs();
    CHECK(swap_fsm_next_state(SWAP_GATE, &in) == SWAP_DECOUPLE_ASSERT);
}

static void test_decouple_assert_waits_for_confirmation(void)
{
    mps3_swap_transition_inputs_t in = no_inputs();
    CHECK(swap_fsm_next_state(SWAP_DECOUPLE_ASSERT, &in) == SWAP_DECOUPLE_ASSERT);
    in.decouple_confirmed = true;
    CHECK(swap_fsm_next_state(SWAP_DECOUPLE_ASSERT, &in) == SWAP_STREAM_CLEARING);
}

static void test_decouple_assert_times_out_fails_closed(void)
{
    /* fail-STUCK fix: an unconfirmed decoupler that hits the confirm-poll
     * ceiling fails closed rather than parking here forever. */
    mps3_swap_transition_inputs_t in = no_inputs();
    in.confirm_timeout = true;
    CHECK(swap_fsm_next_state(SWAP_DECOUPLE_ASSERT, &in) == SWAP_FAILED);
    /* a confirmation in the SAME poll wins over the timeout (no spurious fail) */
    in.decouple_confirmed = true;
    CHECK(swap_fsm_next_state(SWAP_DECOUPLE_ASSERT, &in) == SWAP_STREAM_CLEARING);
}

static void test_stream_clearing_fails_closed_on_invalid_cache(void)
{
    mps3_swap_transition_inputs_t in = no_inputs();
    in.clearing_cache_valid = false;
    in.clearing_stream_done = true; /* must NOT matter -- cache validity gates first */
    CHECK(swap_fsm_next_state(SWAP_STREAM_CLEARING, &in) == SWAP_FAILED);
}

static void test_stream_clearing_waits_then_advances(void)
{
    mps3_swap_transition_inputs_t in = no_inputs();
    in.clearing_cache_valid = true;
    in.clearing_stream_done = false;
    CHECK(swap_fsm_next_state(SWAP_STREAM_CLEARING, &in) == SWAP_STREAM_CLEARING);
    in.clearing_stream_done = true;
    CHECK(swap_fsm_next_state(SWAP_STREAM_CLEARING, &in) == SWAP_AWAIT_INCOMING_CLEARING);
}

static void test_stream_clearing_fails_closed_on_stream_error(void)
{
    /* fail-OPEN fix: a stuck HWICAP write (stream_error) fails the swap even
     * when the cache is valid and the chunk claims done — never "streamed OK". */
    mps3_swap_transition_inputs_t in = no_inputs();
    in.clearing_cache_valid = true;
    in.stream_error = true;
    in.clearing_stream_done = true;
    CHECK(swap_fsm_next_state(SWAP_STREAM_CLEARING, &in) == SWAP_FAILED);
}

static void test_await_incoming_clearing(void)
{
    mps3_swap_transition_inputs_t in = no_inputs();
    CHECK(swap_fsm_next_state(SWAP_AWAIT_INCOMING_CLEARING, &in) == SWAP_AWAIT_INCOMING_CLEARING);
    in.incoming_clearing_ready = true;
    CHECK(swap_fsm_next_state(SWAP_AWAIT_INCOMING_CLEARING, &in) == SWAP_AWAIT_PARTIAL);
}

static void test_await_partial(void)
{
    mps3_swap_transition_inputs_t in = no_inputs();
    CHECK(swap_fsm_next_state(SWAP_AWAIT_PARTIAL, &in) == SWAP_AWAIT_PARTIAL);
    in.partial_ready = true;
    CHECK(swap_fsm_next_state(SWAP_AWAIT_PARTIAL, &in) == SWAP_STREAM_PARTIAL);
}

static void test_stream_partial(void)
{
    mps3_swap_transition_inputs_t in = no_inputs();
    CHECK(swap_fsm_next_state(SWAP_STREAM_PARTIAL, &in) == SWAP_STREAM_PARTIAL);
    in.partial_stream_done = true;
    /* R1 reorder: -> RELEASE, not VERIFY. RM_ID reads back CLAMPED (0) while
     * the decoupler is asserted, so the RP must be connected before we can
     * verify it. */
    CHECK(swap_fsm_next_state(SWAP_STREAM_PARTIAL, &in) == SWAP_RELEASE);
}

static void test_stream_partial_fails_closed_on_stream_error(void)
{
    mps3_swap_transition_inputs_t in = no_inputs();
    in.stream_error = true;
    in.partial_stream_done = true; /* must NOT matter -- a stuck write fails first */
    CHECK(swap_fsm_next_state(SWAP_STREAM_PARTIAL, &in) == SWAP_FAILED);
}

static void test_verify_i25_real_compare(void)
{
    /* I25: VERIFY must actually branch on verify_ok -- both directions
     * exercised, unlike the old always-true stub this replaces. */
    mps3_swap_transition_inputs_t in = no_inputs();
    in.verify_ok = true;
    CHECK(swap_fsm_next_state(SWAP_VERIFY, &in) == SWAP_CACHE_CLEARING);

    /* R1: a WRONG id no longer goes straight to FAILED -- verification now
     * happens with the RP connected, so it must be re-isolated first. */
    in.verify_ok = false;
    in.verify_mismatch = true;
    CHECK(swap_fsm_next_state(SWAP_VERIFY, &in) == SWAP_REISOLATE);
}

static void test_verify_polls_while_rm_id_unsettled(void)
{
    /* THE distinction that makes the R1 reorder work. dfx_ctl's rm_id_valid
     * needs the synchronized word to settle after the RP is connected. "Not yet
     * valid" is NOT a mismatch: conflating them would fail every swap on the
     * first poll. Nor is it success: that would read a clamped 0 as an id. */
    mps3_swap_transition_inputs_t in = no_inputs();   /* neither ok nor mismatch */
    CHECK(swap_fsm_next_state(SWAP_VERIFY, &in) == SWAP_VERIFY);

    /* ... but an RM that NEVER presents an id must not spin forever. */
    in.confirm_timeout = true;
    CHECK(swap_fsm_next_state(SWAP_VERIFY, &in) == SWAP_REISOLATE);
}

static void test_verify_ok_beats_a_stale_timeout(void)
{
    /* A settled, correct id in the same poll as the timeout still commits. */
    mps3_swap_transition_inputs_t in = no_inputs();
    in.verify_ok = true;
    in.confirm_timeout = true;
    CHECK(swap_fsm_next_state(SWAP_VERIFY, &in) == SWAP_CACHE_CLEARING);
}

static void test_reisolate_puts_a_bad_rm_back_in_the_box(void)
{
    /* R1: FAILED must still mean "RP inert". Since verification now runs with
     * the RP connected, REISOLATE re-asserts DECOUPLE + rp_resetn first. */
    mps3_swap_transition_inputs_t in = no_inputs();
    CHECK(swap_fsm_next_state(SWAP_REISOLATE, &in) == SWAP_REISOLATE);  /* wait */

    in.decouple_confirmed = true;
    CHECK(swap_fsm_next_state(SWAP_REISOLATE, &in) == SWAP_FAILED);
}

static void test_reisolate_that_never_confirms_still_fails(void)
{
    /* Nothing more can be done from here; parking the client forever is worse
     * than reporting the failure with the isolation unconfirmed. */
    mps3_swap_transition_inputs_t in = no_inputs();
    in.confirm_timeout = true;
    CHECK(swap_fsm_next_state(SWAP_REISOLATE, &in) == SWAP_FAILED);
}

static void test_cache_clearing_is_unconditional(void)
{
    /* R1 reorder: CACHE_CLEARING is now the LAST step before DONE. It stays
     * strictly after VERIFY, which is what keeps a rejected RM from clobbering
     * the previous, still-valid g_current_rm_clearing. */
    mps3_swap_transition_inputs_t in = no_inputs();
    CHECK(swap_fsm_next_state(SWAP_CACHE_CLEARING, &in) == SWAP_DONE);
}

static void test_release_waits_for_confirmation(void)
{
    mps3_swap_transition_inputs_t in = no_inputs();
    CHECK(swap_fsm_next_state(SWAP_RELEASE, &in) == SWAP_RELEASE);
    in.release_confirmed = true;
    CHECK(swap_fsm_next_state(SWAP_RELEASE, &in) == SWAP_VERIFY);   /* R1: was DONE */
}

static void test_release_times_out_fails_closed(void)
{
    /* fail-STUCK fix: a release that never confirms fails closed once its
     * bounded poll expires (was an infinite RELEASE self-loop).
     * -> FAILED, not REISOLATE: the release never took effect, so the RP is
     * still decoupled and held in reset. It is already inert. */
    mps3_swap_transition_inputs_t in = no_inputs();
    in.confirm_timeout = true;
    CHECK(swap_fsm_next_state(SWAP_RELEASE, &in) == SWAP_FAILED);
    in.release_confirmed = true; /* confirmation in the same poll still wins */
    CHECK(swap_fsm_next_state(SWAP_RELEASE, &in) == SWAP_VERIFY);
}

static void test_terminal_states_return_to_idle(void)
{
    mps3_swap_transition_inputs_t in = no_inputs();
    CHECK(swap_fsm_next_state(SWAP_DONE, &in) == SWAP_IDLE);
    CHECK(swap_fsm_next_state(SWAP_FAILED, &in) == SWAP_IDLE);
}

/* Full happy-path walk, exactly matching net-protocol.md's 7 numbered
 * swap-sequence steps end to end (mirrors
 * tests/integration/test_swap_sequence.py's
 * test_happy_path_transitions_through_every_state(), re-expressed against
 * the real C transition table instead of that file's Python port). */
static void test_full_happy_path_walk(void)
{
    mps3_swap_state_t s = SWAP_IDLE;
    /* swap_fsm_start() would set this directly in the real code; the pure
     * table itself never leaves IDLE on its own. */
    s = SWAP_GATE;

    mps3_swap_transition_inputs_t in;

    in = no_inputs();
    s = swap_fsm_next_state(s, &in);
    CHECK(s == SWAP_DECOUPLE_ASSERT);

    in = no_inputs(); in.decouple_confirmed = true;
    s = swap_fsm_next_state(s, &in);
    CHECK(s == SWAP_STREAM_CLEARING);

    in = no_inputs(); in.clearing_cache_valid = true; in.clearing_stream_done = true;
    s = swap_fsm_next_state(s, &in);
    CHECK(s == SWAP_AWAIT_INCOMING_CLEARING);

    in = no_inputs(); in.incoming_clearing_ready = true;
    s = swap_fsm_next_state(s, &in);
    CHECK(s == SWAP_AWAIT_PARTIAL);

    in = no_inputs(); in.partial_ready = true;
    s = swap_fsm_next_state(s, &in);
    CHECK(s == SWAP_STREAM_PARTIAL);

    /* R1 reorder: connect the RP (RELEASE) BEFORE reading its id (VERIFY),
     * because the decoupler clamps rm_id to 0 while asserted. */
    in = no_inputs(); in.partial_stream_done = true;
    s = swap_fsm_next_state(s, &in);
    CHECK(s == SWAP_RELEASE);

    in = no_inputs(); in.release_confirmed = true;
    s = swap_fsm_next_state(s, &in);
    CHECK(s == SWAP_VERIFY);

    /* rm_id_valid has not settled yet -> keep polling, do not decide. */
    in = no_inputs();
    s = swap_fsm_next_state(s, &in);
    CHECK(s == SWAP_VERIFY);

    in = no_inputs(); in.verify_ok = true;
    s = swap_fsm_next_state(s, &in);
    CHECK(s == SWAP_CACHE_CLEARING);

    in = no_inputs();
    s = swap_fsm_next_state(s, &in);
    CHECK(s == SWAP_DONE);

    in = no_inputs();
    s = swap_fsm_next_state(s, &in);
    CHECK(s == SWAP_IDLE);
}

static void test_await_states_are_bounded(void)
{
    /* These two were the ONLY states in the FSM with no bound. A client that
     * died mid-swap parked the shell forever and recovery needed a JTAG
     * bitstream reload (observed on silicon 2026-07-09). */
    mps3_swap_transition_inputs_t in = no_inputs();
    CHECK(swap_fsm_next_state(SWAP_AWAIT_INCOMING_CLEARING, &in) == SWAP_AWAIT_INCOMING_CLEARING);
    CHECK(swap_fsm_next_state(SWAP_AWAIT_PARTIAL, &in) == SWAP_AWAIT_PARTIAL);

    in.await_timeout = true;
    /* -> FAILED, not REISOLATE: still pre-RELEASE, so DECOUPLE is asserted and
     * rp_resetn held. The RP is already inert. */
    CHECK(swap_fsm_next_state(SWAP_AWAIT_INCOMING_CLEARING, &in) == SWAP_FAILED);
    CHECK(swap_fsm_next_state(SWAP_AWAIT_PARTIAL, &in) == SWAP_FAILED);
}

static void test_arriving_payload_beats_the_idle_timeout(void)
{
    /* A payload that lands in the same poll as the timeout must still be taken:
     * losing a fully-received, validated bitstream to a stopwatch would be a
     * gratuitous failure. */
    mps3_swap_transition_inputs_t in = no_inputs();
    in.await_timeout = true;
    in.incoming_clearing_ready = true;
    CHECK(swap_fsm_next_state(SWAP_AWAIT_INCOMING_CLEARING, &in) == SWAP_AWAIT_PARTIAL);

    in = no_inputs();
    in.await_timeout = true;
    in.partial_ready = true;
    CHECK(swap_fsm_next_state(SWAP_AWAIT_PARTIAL, &in) == SWAP_STREAM_PARTIAL);
}

int main(void)
{
    test_idle_self_loops();
    test_gate_is_unconditional();
    test_decouple_assert_waits_for_confirmation();
    test_decouple_assert_times_out_fails_closed();
    test_stream_clearing_fails_closed_on_invalid_cache();
    test_stream_clearing_fails_closed_on_stream_error();
    test_stream_clearing_waits_then_advances();
    test_await_incoming_clearing();
    test_await_partial();
    test_stream_partial();
    test_stream_partial_fails_closed_on_stream_error();
    test_verify_i25_real_compare();
    test_await_states_are_bounded();
    test_arriving_payload_beats_the_idle_timeout();
    test_reisolate_that_never_confirms_still_fails();
    test_reisolate_puts_a_bad_rm_back_in_the_box();
    test_verify_ok_beats_a_stale_timeout();
    test_verify_polls_while_rm_id_unsettled();
    test_cache_clearing_is_unconditional();
    test_release_waits_for_confirmation();
    test_release_times_out_fails_closed();
    test_terminal_states_return_to_idle();
    test_full_happy_path_walk();

    printf("test_swap_fsm_transitions: %d checks passed\n", s_checks);
    return 0;
}
