/*
 * mps3_swap_transitions.c — pure transition table, ported from
 * firmware/coordinator/swap_fsm_transitions.c (main repo, read-only).
 *
 * PORT RULE: the decision logic below is a 1:1 copy of the silicon-proven
 * table (only the function name got an mps3_ prefix and the include
 * changed). If a divergence is ever needed, change the FIRMWARE first and
 * re-port — this file must stay diffable against the original.
 *
 * The table is total: every state has a defined next state for every
 * possible input. Terminals (DONE/FAILED) map to IDLE; IDLE self-loops
 * (only an external start command leaves it).
 */
#include "mps3_swap_transitions.h"

mps3_swap_state_t mps3_swap_next_state(mps3_swap_state_t cur,
				       const mps3_swap_transition_inputs_t *in)
{
	switch (cur) {
	case SWAP_IDLE:
		return SWAP_IDLE;

	case SWAP_GATE:
		/* Unconditional: gating consumers is a one-shot poke with no
		 * confirmation handshake in any contract doc. */
		return SWAP_DECOUPLE_ASSERT;

	case SWAP_DECOUPLE_ASSERT:
		if (in->decouple_confirmed)
			return SWAP_STREAM_CLEARING;
		/* fail-STUCK: a decoupler that never confirms fails closed
		 * (RP stays isolated) once the bounded poll expires. */
		return in->confirm_timeout ? SWAP_FAILED : SWAP_DECOUPLE_ASSERT;

	case SWAP_STREAM_CLEARING:
		/* I2: the shell always holds the current RM's clearing; an
		 * invalid cache is a bug — fail closed, never stall. */
		if (!in->clearing_cache_valid)
			return SWAP_FAILED;
		/* fail-OPEN fix: an HWICAP word whose write never completed
		 * must fail the swap, never count as "streamed". */
		if (in->stream_error)
			return SWAP_FAILED;
		return in->clearing_stream_done ? SWAP_AWAIT_INCOMING_CLEARING
						: SWAP_STREAM_CLEARING;

	case SWAP_AWAIT_INCOMING_CLEARING:
		if (in->incoming_clearing_ready)
			return SWAP_AWAIT_PARTIAL;
		/* fail-IDLE: -> FAILED (not REISOLATE): pre-RELEASE, DECOUPLE
		 * is asserted and rp_resetn held — the RP is already inert. */
		return in->await_timeout ? SWAP_FAILED : SWAP_AWAIT_INCOMING_CLEARING;

	case SWAP_AWAIT_PARTIAL:
		if (in->partial_ready)
			return SWAP_STREAM_PARTIAL;
		return in->await_timeout ? SWAP_FAILED : SWAP_AWAIT_PARTIAL;

	case SWAP_STREAM_PARTIAL:
		if (in->stream_error)
			return SWAP_FAILED;
		/* R1 ORDERING: -> RELEASE, not VERIFY. The decoupler CLAMPS
		 * rm_id while asserted; to observe the RP's id you must
		 * connect the RP. Release first, verify second. */
		return in->partial_stream_done ? SWAP_RELEASE : SWAP_STREAM_PARTIAL;

	case SWAP_RELEASE:
		if (in->release_confirmed)
			return SWAP_VERIFY;
		/* fail-STUCK (symmetric): the release never took effect, the
		 * RP is still decoupled and held in reset — already inert. */
		return in->confirm_timeout ? SWAP_FAILED : SWAP_RELEASE;

	case SWAP_VERIFY:
		/* Three outcomes:
		 *   settled and right -> commit
		 *   settled but wrong -> re-isolate, then fail
		 *   not settled yet   -> keep polling until the bounded timeout */
		if (in->verify_ok)
			return SWAP_CACHE_CLEARING;
		if (in->verify_mismatch || in->confirm_timeout)
			return SWAP_REISOLATE;
		return SWAP_VERIFY;

	case SWAP_CACHE_CLEARING:
		/* Bookkeeping promote, strictly AFTER verify so a rejected RM
		 * never clobbers the previous, still-valid clearing. */
		return SWAP_DONE;

	case SWAP_REISOLATE:
		/* Put a verification-failed RM back in the box before
		 * reporting failure. A re-isolate that never confirms still
		 * fails: parking the client forever is strictly worse. */
		if (in->decouple_confirmed || in->confirm_timeout)
			return SWAP_FAILED;
		return SWAP_REISOLATE;

	case SWAP_DONE:
		return SWAP_IDLE;

	case SWAP_FAILED:
		return SWAP_IDLE;

	default:
		return SWAP_IDLE;
	}
}
