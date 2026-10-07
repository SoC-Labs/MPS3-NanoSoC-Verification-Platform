/*
 * test_transitions.c — host unit tests for the ported pure transition table.
 * Mirrors the intent of firmware/test/test_swap_fsm.c (which links the
 * firmware table alone): pins the decision logic so a port drift fails here,
 * not on silicon.
 */
#include <stdio.h>
#include <stdlib.h>
#include "../../mps3_swap_transitions.h"

static int fails;

#define CHECK(cond) do { \
	if (!(cond)) { \
		fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
		fails++; \
	} \
} while (0)

static mps3_swap_transition_inputs_t Z;

static mps3_swap_state_t step(mps3_swap_state_t s, mps3_swap_transition_inputs_t in)
{
	return mps3_swap_next_state(s, &in);
}

int main(void)
{
	mps3_swap_transition_inputs_t in;

	/* IDLE self-loops on anything */
	in = Z; in.decouple_confirmed = true; in.partial_ready = true;
	CHECK(step(SWAP_IDLE, in) == SWAP_IDLE);

	/* GATE is unconditional */
	CHECK(step(SWAP_GATE, Z) == SWAP_DECOUPLE_ASSERT);

	/* DECOUPLE_ASSERT: confirm wins; timeout fails closed; else waits */
	in = Z; in.decouple_confirmed = true;
	CHECK(step(SWAP_DECOUPLE_ASSERT, in) == SWAP_STREAM_CLEARING);
	in = Z; in.confirm_timeout = true;
	CHECK(step(SWAP_DECOUPLE_ASSERT, in) == SWAP_FAILED);
	CHECK(step(SWAP_DECOUPLE_ASSERT, Z) == SWAP_DECOUPLE_ASSERT);

	/* STREAM_CLEARING: invalid cache fails closed even with done set */
	in = Z; in.clearing_stream_done = true;
	CHECK(step(SWAP_STREAM_CLEARING, in) == SWAP_FAILED);
	in = Z; in.clearing_cache_valid = true; in.stream_error = true;
	in.clearing_stream_done = true;   /* error outranks done */
	CHECK(step(SWAP_STREAM_CLEARING, in) == SWAP_FAILED);
	in = Z; in.clearing_cache_valid = true; in.clearing_stream_done = true;
	CHECK(step(SWAP_STREAM_CLEARING, in) == SWAP_AWAIT_INCOMING_CLEARING);
	in = Z; in.clearing_cache_valid = true;
	CHECK(step(SWAP_STREAM_CLEARING, in) == SWAP_STREAM_CLEARING);

	/* AWAIT states: ready advances, idle-timeout fails, else waits */
	in = Z; in.incoming_clearing_ready = true;
	CHECK(step(SWAP_AWAIT_INCOMING_CLEARING, in) == SWAP_AWAIT_PARTIAL);
	in = Z; in.await_timeout = true;
	CHECK(step(SWAP_AWAIT_INCOMING_CLEARING, in) == SWAP_FAILED);
	CHECK(step(SWAP_AWAIT_INCOMING_CLEARING, Z) == SWAP_AWAIT_INCOMING_CLEARING);
	in = Z; in.partial_ready = true;
	CHECK(step(SWAP_AWAIT_PARTIAL, in) == SWAP_STREAM_PARTIAL);
	in = Z; in.await_timeout = true;
	CHECK(step(SWAP_AWAIT_PARTIAL, in) == SWAP_FAILED);

	/* STREAM_PARTIAL: R1 ordering — done goes to RELEASE, never VERIFY */
	in = Z; in.partial_stream_done = true;
	CHECK(step(SWAP_STREAM_PARTIAL, in) == SWAP_RELEASE);
	in = Z; in.stream_error = true; in.partial_stream_done = true;
	CHECK(step(SWAP_STREAM_PARTIAL, in) == SWAP_FAILED);

	/* RELEASE: confirmed -> VERIFY; timeout -> FAILED */
	in = Z; in.release_confirmed = true;
	CHECK(step(SWAP_RELEASE, in) == SWAP_VERIFY);
	in = Z; in.confirm_timeout = true;
	CHECK(step(SWAP_RELEASE, in) == SWAP_FAILED);
	CHECK(step(SWAP_RELEASE, Z) == SWAP_RELEASE);

	/* VERIFY: ok -> commit; mismatch -> REISOLATE (immediate); timeout ->
	 * REISOLATE; not-yet-valid keeps polling */
	in = Z; in.verify_ok = true;
	CHECK(step(SWAP_VERIFY, in) == SWAP_CACHE_CLEARING);
	in = Z; in.verify_mismatch = true;
	CHECK(step(SWAP_VERIFY, in) == SWAP_REISOLATE);
	in = Z; in.confirm_timeout = true;
	CHECK(step(SWAP_VERIFY, in) == SWAP_REISOLATE);
	CHECK(step(SWAP_VERIFY, Z) == SWAP_VERIFY);

	/* CACHE_CLEARING unconditional; REISOLATE confirms-or-times-out to
	 * FAILED; terminals -> IDLE */
	CHECK(step(SWAP_CACHE_CLEARING, Z) == SWAP_DONE);
	in = Z; in.decouple_confirmed = true;
	CHECK(step(SWAP_REISOLATE, in) == SWAP_FAILED);
	in = Z; in.confirm_timeout = true;
	CHECK(step(SWAP_REISOLATE, in) == SWAP_FAILED);
	CHECK(step(SWAP_REISOLATE, Z) == SWAP_REISOLATE);
	CHECK(step(SWAP_DONE, Z) == SWAP_IDLE);
	CHECK(step(SWAP_FAILED, Z) == SWAP_IDLE);

	/* Totality: every state x 4096 input combinations returns a defined
	 * state (never asserts/branches wild). */
	{
		int s, v;
		for (s = 0; s <= SWAP_REISOLATE; s++) {
			for (v = 0; v < 4096; v++) {
				mps3_swap_transition_inputs_t i2 = {
					.decouple_confirmed = !!(v & 1),
					.clearing_cache_valid = !!(v & 2),
					.clearing_stream_done = !!(v & 4),
					.incoming_clearing_ready = !!(v & 8),
					.partial_ready = !!(v & 16),
					.await_timeout = !!(v & 32),
					.partial_stream_done = !!(v & 64),
					.verify_ok = !!(v & 128),
					.verify_mismatch = !!(v & 256),
					.release_confirmed = !!(v & 512),
					.stream_error = !!(v & 1024),
					.confirm_timeout = !!(v & 2048),
				};
				mps3_swap_state_t n =
					mps3_swap_next_state((mps3_swap_state_t)s, &i2);
				CHECK(n >= SWAP_IDLE && n <= SWAP_REISOLATE);
			}
		}
	}

	/* Frozen enum values (diag mailbox / host tests read raw ints) */
	CHECK(SWAP_IDLE == 0 && SWAP_FAILED == 11 && SWAP_REISOLATE == 12);

	if (fails) {
		printf("test_transitions: %d FAILURES\n", fails);
		return 1;
	}
	printf("test_transitions: PASS\n");
	return 0;
}
