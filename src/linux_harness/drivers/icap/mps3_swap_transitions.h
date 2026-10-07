/*
 * mps3_swap_transitions.h — the swap FSM's pure transition table, ported
 * from firmware/coordinator/swap_fsm.h + swap_fsm_transitions.c (main repo,
 * read-only). DRIVER_MATRIX §2.8: "The pure transition table ... ports
 * verbatim — reuse them." Only the includes and integer types changed; the
 * states, the inputs struct and every decision in the table are byte-level
 * the same logic. The numeric enum values are FROZEN (the bare-metal diag
 * mailbox and host tests read them as raw integers; keeping them identical
 * keeps the two implementations diffable).
 */
#ifndef MPS3_SWAP_TRANSITIONS_H
#define MPS3_SWAP_TRANSITIONS_H

#include "mps3_compat.h"

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
	SWAP_DONE,
	SWAP_FAILED,   /* terminal: RP left DECOUPLEd + in reset (parked) */
	SWAP_REISOLATE /* appended in firmware R1 — value 12, frozen */
} mps3_swap_state_t;

typedef struct {
	bool decouple_confirmed;
	bool clearing_cache_valid;
	bool clearing_stream_done;
	bool incoming_clearing_ready;
	bool partial_ready;
	bool await_timeout;
	bool partial_stream_done;
	bool verify_ok;
	bool verify_mismatch;
	bool release_confirmed;
	bool stream_error;
	bool confirm_timeout;
} mps3_swap_transition_inputs_t;

mps3_swap_state_t mps3_swap_next_state(mps3_swap_state_t cur,
				       const mps3_swap_transition_inputs_t *in);

#endif /* MPS3_SWAP_TRANSITIONS_H */
