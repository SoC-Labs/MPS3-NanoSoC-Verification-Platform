/*
 * mps3_icap_engine.h — the DFX swap engine: HWICAP write protocols +
 * DFXCTL/CLKRST swap sequencing, driven by the ported pure transition table.
 *
 * DESIGN: the engine is kernel-independent C over an ops vtable (register
 * read/write, cooperative relax, optional hostio4-reset hook). The kernel
 * module binds it to real MMIO (or the in-kernel mock); the host unit tests
 * bind it to the same mock compiled for userspace. This is the firmware's
 * proven thin-HAL split (MPS3_HAL_MOCK) applied to the Linux driver, so the
 * sequencing logic that took multiple silicon-found bugs to settle is
 * testable everywhere it runs.
 *
 * The engine is a TABLE WALKER: unlike the bare-metal superloop (one bounded
 * chunk per poll), calls here run a whole phase to completion — the kernel
 * preempts, so cooperative chunking dissolves into ops->relax() scheduling
 * points between chunks (SERVICE_DISPOSITION §4 "chunk-with-scheduling-points
 * survives"). Every decision still goes through mps3_swap_next_state(), so
 * the silicon-proven decision logic stays authoritative and the table's
 * host tests stay meaningful.
 *
 * Concurrency: the engine is NOT internally locked. The caller (the kernel
 * driver's mutex, or the single-threaded host test) must serialise all calls.
 */
#ifndef MPS3_ICAP_ENGINE_H
#define MPS3_ICAP_ENGINE_H

#include "mps3_compat.h"
#include "mps3_icap_regs.h"
#include "mps3_swap_transitions.h"

/* Engine error codes (negative). The kernel driver maps these to errno. */
#define MPS3_EOK              0
#define MPS3_ESTATE          -1   /* call not legal in this engine state       */
#define MPS3_EORDER          -2   /* pair ordering violated (partial w/o clearing) */
#define MPS3_ECONFIRM        -3   /* decouple/release/reisolate confirm-poll expired */
#define MPS3_ESTREAM         -4   /* HWICAP write stalled (WFV/CR bounded poll) */
#define MPS3_ECRC            -5   /* stream-direct running CRC mismatch        */
#define MPS3_EVERIFY         -6   /* RM_ID settled but WRONG (re-isolated)     */
#define MPS3_EVERIFYTMO      -7   /* rm_id_valid never asserted (re-isolated)  */
#define MPS3_EALIGN          -8   /* payload not a whole number of words       */
#define MPS3_EEOS            -9   /* strict mode: EOS never asserted post-DESYNC */
#define MPS3_EABORT         -10   /* session aborted (explicit or idle-timeout reap) */

/* eos_status values (mirrors firmware swap_fsm.h MPS3_ICAP_EOS_*). */
#define MPS3_ICAP_EOS_NONE    0u
#define MPS3_ICAP_EOS_SEEN    1u
#define MPS3_ICAP_EOS_TIMEOUT 2u

struct mps3_icap_ops {
	u32  (*rd)(void *ctx, int blk, u32 off);
	void (*wr)(void *ctx, int blk, u32 off, u32 val);
	/* Cooperative scheduling point — cond_resched() in the kernel, no-op
	 * in host tests. Called between chunks and inside bounded polls. */
	void (*relax)(void *ctx);
	/* Swap-scoped invariant (TRANSPLANT_CONTRACT §9.4 / tests/
	 * hostio4_hotswap): reset any static-side hostio4_target on EVERY
	 * swap, placed BETWEEN partial-stream-done and release — while the RP
	 * is still decoupled + in reset. No hostio4_target exists in the
	 * current static shell, so this hook is optional (NULL); the engine
	 * carries the call site NOW so the invariant is not rediscovered as
	 * an intermittent silicon hang when the hostio wave lands one. */
	int  (*hostio4_reset)(void *ctx);
};

struct mps3_icap_cfg {
	int fifo_mode;        /* 1 = FIFO protocol (this BD: C_MODE 0, depth
			       * 1024); 0 = lite (one StartConfig per word,
			       * the on-silicon bare-metal shell). */
	u32 done_poll_max;    /* bounded spins: WFV vacancy / CR self-clear
			       * (the write-drain waits — must tolerate a real
			       * FIFO drain, so large). NOT the EOS wait. */
	u32 confirm_poll_max; /* bounded spins: DFXCTL confirm polls +
			       * rm_id_valid settle wait. */
	u32 chunk_words;      /* relax() granularity while streaming. */
	u32 eos_poll_max;     /* SHORT best-effort post-DESYNC EOS wait bound —
			       * deliberately << done_poll_max so a non-asserting
			       * SR_EOS (open question on this axi_hwicap) fails
			       * fast + diagnostic instead of spinning ~150s. */
	int eos_strict;       /* 0 (default, SERVICE_DISPOSITION §4: "capture
			       * the same raw SR, not gate success on EOS
			       * alone" — whether EOS asserts on this
			       * axi_hwicap build is still open): EOS timeout
			       * is captured, not fatal. 1: fail the swap on
			       * EOS timeout (the current firmware
			       * icap_direct_finish behaviour). */
};

#define MPS3_ICAP_BATCH_WORDS 256u

struct mps3_icap_result {
	bool valid;
	bool ok;
	bool verified;
	u32  rm_id;
	int  err;             /* MPS3_E* of the failure, 0 on success */
};

struct mps3_icap_engine {
	const struct mps3_icap_ops *ops;
	void *ctx;
	struct mps3_icap_cfg cfg;

	mps3_swap_state_t st;
	bool clearing_streamed;   /* pair-ordering gate                     */
	u32  target_rm_id;        /* from the partial's own wire header     */

	/* stream-direct packer: a config word can straddle two write()s */
	bool sd_active;
	bool sd_framed;           /* 0 = fpga-mgr image (no wire header): skip
				   * the length/CRC frame checks at finish;
				   * alignment + protocol checks still apply */
	u32  sd_word, sd_word_n;
	u32  sd_crc;              /* running zlib CRC over payload bytes    */
	u32  sd_bytes;            /* payload bytes accepted so far          */
	u32  sd_expect_bytes;
	u32  sd_expect_crc;
	u32  batch[MPS3_ICAP_BATCH_WORDS];
	u32  batch_n;

	/* diagnostics (free-running, mirror firmware counters) */
	u64  icap_bytes;          /* bytes CONFIRMED into HWICAP            */
	u32  sr_last;             /* last NON-ZERO HWICAP_SR seen at finish */
	u32  eos_status;          /* MPS3_ICAP_EOS_*                        */
	u32  hostio4_calls;

	u32  current_rm_id;       /* last VERIFIED rm_id                    */
	struct mps3_icap_result last;
};

void mps3_engine_init(struct mps3_icap_engine *e,
		      const struct mps3_icap_ops *ops, void *ctx,
		      const struct mps3_icap_cfg *cfg);

/* Sequencing step 1+2: gate hook (caller-side), assert DECOUPLE + AXI
 * shutdown + hold rp/dut/dbg resets, bounded confirm-poll. On success the
 * engine sits at SWAP_STREAM_CLEARING. On confirm timeout: fail-closed
 * (RP left isolated), result recorded, engine back at IDLE. */
int mps3_engine_swap_begin(struct mps3_icap_engine *e);

/* Step 3: stream the OUTGOING RM's clearing (caller has already validated
 * its CRC — validation-before-ICAP is the caller's contract for staged
 * payloads). Runs the whole transfer with relax() points. On success the
 * engine advances through AWAIT_INCOMING_CLEARING (the incoming clearing is
 * daemon-side staging, auto-acked here — see README "delta 2") to
 * AWAIT_PARTIAL. On stream error: swap failed, parked. */
int mps3_engine_stream_clearing(struct mps3_icap_engine *e,
				const u8 *buf, u32 len);

/* Step 5, staged flavour: stream a fully-buffered, CRC-verified partial.
 * target_rm_id comes from the partial's own wire header (I14/I25).
 * On success the engine sits at SWAP_RELEASE (call finish next). */
int mps3_engine_stream_partial(struct mps3_icap_engine *e,
			       const u8 *buf, u32 len, u32 target_rm_id);

/* Step 5, stream-direct flavour (Path 3): bytes hit the ICAP before the
 * trailing CRC completes — acceptable ONLY because the RP is decoupled and
 * a failed CRC fails the swap parked (containment, SERVICE_DISPOSITION
 * §3.3). begin fails closed unless the engine is armed at AWAIT_PARTIAL
 * AND DFXCTL confirms decoupled+rp_in_reset (ported icap_direct_begin,
 * including the 2026-07-10 silicon fail-closed fix).
 * expect_bytes==0 selects UNFRAMED mode (the fpga-manager glue: no wire
 * header, so no length/CRC gate — the caller vouches for the image). */
int mps3_engine_sd_begin(struct mps3_icap_engine *e, u32 expect_bytes,
			 u32 expect_crc, u32 target_rm_id);
int mps3_engine_sd_write(struct mps3_icap_engine *e, const u8 *buf, u32 len);
int mps3_engine_sd_finish(struct mps3_icap_engine *e);
void mps3_engine_sd_abort(struct mps3_icap_engine *e);

/* Steps 5.5–9: hostio4 hook (still parked) -> release + confirm -> RM_ID
 * verify with the RP connected (distinguishing not-yet-valid from
 * valid-but-wrong) -> commit (shutdown deassert, dut/dbg release, rm_id
 * update) or RE-ISOLATE + fail. Returns MPS3_EOK / MPS3_EVERIFY /
 * MPS3_EVERIFYTMO / MPS3_ECONFIRM; engine is back at IDLE either way and
 * e->last holds the outcome. */
int mps3_engine_finish(struct mps3_icap_engine *e);

/* Park now (idle-timeout reap, explicit abort, session teardown): re-isolate
 * writes + bounded confirm, record a failed result, return to IDLE. Safe to
 * call in any state; a no-op at IDLE. */
void mps3_engine_abort_park(struct mps3_icap_engine *e);

static inline bool mps3_engine_active(const struct mps3_icap_engine *e)
{
	return e->st != SWAP_IDLE;
}

#endif /* MPS3_ICAP_ENGINE_H */
