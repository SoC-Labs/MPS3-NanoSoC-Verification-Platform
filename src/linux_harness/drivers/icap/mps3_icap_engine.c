/*
 * mps3_icap_engine.c — DFX swap engine (see mps3_icap_engine.h for the
 * design contract). Sequencing, protocols and failure semantics are ported
 * from firmware/coordinator/swap_fsm.c + overlay_store.c's HWICAP writers
 * (main repo, read-only), with the cooperative-superloop chunking replaced
 * by ops->relax() scheduling points. Every "what happens next" decision
 * goes through the ported pure table (mps3_swap_next_state).
 *
 * SILICON-PROVEN RULES REPRODUCED (do not re-discover):
 *  - CR bit0=WRITE (was once swapped: swap stalled, RM_ID stuck at greybox).
 *  - MSB-first packing via the ONE shared primitive (a native memcpy on
 *    rv32 means ICAP never sees 0xAA995566).
 *  - Every wait is bounded and fail-closed: an ICAP write that never
 *    completes is a failed swap, never a silent success.
 *  - Failure invariant: RP parked DECOUPLEd + in reset.
 *  - Verify runs with the RP CONNECTED (decoupler clamps RM_ID to 0 while
 *    asserted); "not yet valid" keeps polling, "valid but wrong" fails
 *    immediately and RE-ISOLATES before reporting.
 *  - sd_begin fails closed unless the engine is actually awaiting a partial
 *    AND DFXCTL confirms parked (the 2026-07-10 silicon fix: hardware-parked
 *    alone is NOT "the FSM expects a partial").
 */
#include "mps3_icap_engine.h"
#include "mps3_crc32.h"

/* ---- small accessors ---------------------------------------------------- */

static inline u32 rd(struct mps3_icap_engine *e, int blk, u32 off)
{
	return e->ops->rd(e->ctx, blk, off);
}

static inline void wr(struct mps3_icap_engine *e, int blk, u32 off, u32 v)
{
	e->ops->wr(e->ctx, blk, off, v);
}

static inline void set_bits(struct mps3_icap_engine *e, int blk, u32 off, u32 m)
{
	wr(e, blk, off, rd(e, blk, off) | m);
}

static inline void clr_bits(struct mps3_icap_engine *e, int blk, u32 off, u32 m)
{
	wr(e, blk, off, rd(e, blk, off) & ~m);
}

static inline void relax(struct mps3_icap_engine *e)
{
	if (e->ops->relax)
		e->ops->relax(e->ctx);
}

#define RELAX_EVERY 64u

/* ---- bounded HWICAP waits (fail-closed) --------------------------------- */

static int wait_cr_write_clear(struct mps3_icap_engine *e)
{
	u32 spins;

	for (spins = 0; spins < e->cfg.done_poll_max; spins++) {
		if (!(rd(e, MPS3_BLK_HWICAP, HWICAP_CR) & HWICAP_CR_WRITE))
			return MPS3_EOK;
		if ((spins & (RELAX_EVERY - 1)) == RELAX_EVERY - 1)
			relax(e);
	}
	return MPS3_ESTREAM; /* CR.WRITE never self-cleared */
}

static int wait_wfv_nonzero(struct mps3_icap_engine *e, u32 *vacancy)
{
	u32 spins;

	for (spins = 0; spins < e->cfg.done_poll_max; spins++) {
		u32 v = rd(e, MPS3_BLK_HWICAP, HWICAP_WFV);
		if (v != 0) {
			*vacancy = v;
			return MPS3_EOK;
		}
		if ((spins & (RELAX_EVERY - 1)) == RELAX_EVERY - 1)
			relax(e);
	}
	return MPS3_ESTREAM; /* write FIFO never made room */
}

/* ---- write protocols ----------------------------------------------------
 * LITE (bare-metal shell, C_MODE 1): no write FIFO — push word, one
 * StartConfig per word, poll CR.WRITE self-clear.
 * FIFO (this BD, C_MODE 0 depth 1024): poll WFV, push <=vacancy words, one
 * StartConfig per batch, poll CR.WRITE self-clear, refill.
 * Both count e->icap_bytes only for CONFIRMED words. */

static int lite_write_word(struct mps3_icap_engine *e, u32 w)
{
	int rc;

	wr(e, MPS3_BLK_HWICAP, HWICAP_WF, w);
	wr(e, MPS3_BLK_HWICAP, HWICAP_CR, HWICAP_CR_WRITE); /* StartConfig */
	rc = wait_cr_write_clear(e);
	if (rc)
		return rc;
	e->icap_bytes += 4u;
	return MPS3_EOK;
}

static int fifo_drain(struct mps3_icap_engine *e, const u32 *words, u32 nwords)
{
	u32 done = 0;

	while (done < nwords) {
		u32 vacancy, batch, i;
		int rc = wait_wfv_nonzero(e, &vacancy);
		if (rc)
			return rc;
		batch = nwords - done;
		if (batch > vacancy)
			batch = vacancy;
		for (i = 0; i < batch; i++)
			wr(e, MPS3_BLK_HWICAP, HWICAP_WF, words[done + i]);
		wr(e, MPS3_BLK_HWICAP, HWICAP_CR, HWICAP_CR_WRITE);
		rc = wait_cr_write_clear(e);
		if (rc)
			return rc;
		done += batch;
		e->icap_bytes += (u64)batch * 4u;
	}
	return MPS3_EOK;
}

/* Pack + write `nwords` config words from big-endian file bytes. Bounded
 * stack window (same shape as firmware hwicap_fifo_write_words). */
#define PACK_WINDOW_WORDS 256u

static int write_words(struct mps3_icap_engine *e, const u8 *bytes, u32 nwords)
{
	if (e->cfg.fifo_mode) {
		u32 done = 0;
		while (done < nwords) {
			u32 words[PACK_WINDOW_WORDS];
			u32 win = nwords - done, i;
			int rc;
			if (win > PACK_WINDOW_WORDS)
				win = PACK_WINDOW_WORDS;
			for (i = 0; i < win; i++)
				words[i] = mps3_hwicap_pack_word(&bytes[4u * (done + i)]);
			rc = fifo_drain(e, words, win);
			if (rc)
				return rc;
			done += win;
		}
		return MPS3_EOK;
	} else {
		u32 i;
		for (i = 0; i < nwords; i++) {
			int rc = lite_write_word(e,
					mps3_hwicap_pack_word(&bytes[4u * i]));
			if (rc)
				return rc;
		}
		return MPS3_EOK;
	}
}

/* ---- park / failure handling -------------------------------------------- */

/* Re-isolate writes: mirrors firmware step_reisolate() (which itself mirrors
 * step_decouple_assert). Returns EOK once DFXCTL confirms, ECONFIRM if the
 * bounded poll expires (nothing further can be done; parking the caller
 * forever is strictly worse — ported comment). */
static int park_writes_and_confirm(struct mps3_icap_engine *e)
{
	u32 polls;

	for (polls = 0; polls < e->cfg.confirm_poll_max; polls++) {
		u32 status;
		const u32 isolated = DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET;

		set_bits(e, MPS3_BLK_DFXCTL, DFXCTL_SHUTDOWN, DFXCTL_SHUTDOWN_AXI);
		set_bits(e, MPS3_BLK_DFXCTL, DFXCTL_DECOUPLE, DFXCTL_DECOUPLE_EN);
		/* Hold ALL THREE: an RM that failed must not be left running
		 * (1 = released, so "hold" = clear). */
		clr_bits(e, MPS3_BLK_CLKRST, CLKRST_RESET_CTRL,
			 CLKRST_RESET_CTRL_RP_RESETN | CLKRST_RESET_CTRL_DUT_RESETN |
			 CLKRST_RESET_CTRL_DBG_RESETN);

		status = rd(e, MPS3_BLK_DFXCTL, DFXCTL_STATUS);
		if ((status & isolated) == isolated)
			return MPS3_EOK;
		if ((polls & (RELAX_EVERY - 1)) == RELAX_EVERY - 1)
			relax(e);
	}
	return MPS3_ECONFIRM;
}

/* Record a failed swap and return to IDLE. If `reisolate`, drive the park
 * writes first (used on every failure path where the RP could have been
 * touched — cheap and idempotent on paths where it is already parked). */
static void engine_fail(struct mps3_icap_engine *e, int err, bool reisolate)
{
	if (reisolate)
		(void)park_writes_and_confirm(e);
	e->sd_active = false;
	e->last.valid = true;
	e->last.ok = false;
	e->last.verified = false;
	e->last.rm_id = e->current_rm_id; /* the still-resident previous RM */
	e->last.err = err;
	e->st = SWAP_IDLE;
	e->clearing_streamed = false;
}

/* ---- public API ---------------------------------------------------------- */

void mps3_engine_init(struct mps3_icap_engine *e,
		      const struct mps3_icap_ops *ops, void *ctx,
		      const struct mps3_icap_cfg *cfg)
{
	memset(e, 0, sizeof(*e));
	e->ops = ops;
	e->ctx = ctx;
	if (cfg) {
		e->cfg = *cfg;
	} else {
		e->cfg.fifo_mode = 1;
		e->cfg.done_poll_max = 1000000u;
		e->cfg.confirm_poll_max = 100000u;
		e->cfg.eos_poll_max = 50000u;
		e->cfg.chunk_words = 1024u;
		e->cfg.eos_strict = 0;
	}
	if (e->cfg.chunk_words == 0)
		e->cfg.chunk_words = 1024u;
	e->st = SWAP_IDLE;
	e->eos_status = MPS3_ICAP_EOS_NONE;
}

int mps3_engine_swap_begin(struct mps3_icap_engine *e)
{
	mps3_swap_transition_inputs_t in;
	u32 polls = 0;

	if (e->st != SWAP_IDLE)
		return MPS3_ESTATE;

	e->clearing_streamed = false;
	e->target_rm_id = 0;
	e->sd_active = false;
	e->batch_n = 0;
	memset(&e->last, 0, sizeof(e->last));

	/* GATE: consumer gating (SWD/XVC/UART/link) is daemon-side under
	 * Linux (SERVICE_DISPOSITION §0 item 3); the driver exposes
	 * swap-active state instead. Table arc is unconditional. */
	e->st = SWAP_GATE;
	memset(&in, 0, sizeof(in));
	e->st = mps3_swap_next_state(e->st, &in); /* -> DECOUPLE_ASSERT */

	/* DECOUPLE_ASSERT: firmware re-issues the writes every poll —
	 * mirrored (idempotent set/clr). */
	while (e->st == SWAP_DECOUPLE_ASSERT) {
		u32 status;

		set_bits(e, MPS3_BLK_DFXCTL, DFXCTL_DECOUPLE, DFXCTL_DECOUPLE_EN);
		set_bits(e, MPS3_BLK_DFXCTL, DFXCTL_SHUTDOWN, DFXCTL_SHUTDOWN_AXI);
		/* rp_resetn is released-when-1: "hold in reset" = clear. dut
		 * and dbg too — never rewrite LUTs under a clocked DUT. */
		clr_bits(e, MPS3_BLK_CLKRST, CLKRST_RESET_CTRL,
			 CLKRST_RESET_CTRL_RP_RESETN | CLKRST_RESET_CTRL_DUT_RESETN |
			 CLKRST_RESET_CTRL_DBG_RESETN);

		memset(&in, 0, sizeof(in));
		status = rd(e, MPS3_BLK_DFXCTL, DFXCTL_STATUS);
		in.decouple_confirmed =
			(status & (DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET))
			== (DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET);
		if (!in.decouple_confirmed && ++polls >= e->cfg.confirm_poll_max)
			in.confirm_timeout = true;

		e->st = mps3_swap_next_state(e->st, &in);
		if ((polls & (RELAX_EVERY - 1)) == RELAX_EVERY - 1)
			relax(e);
	}

	if (e->st == SWAP_FAILED) {
		/* Decoupler never confirmed: fail closed, RP left isolated
		 * (the assert writes stay in force). */
		engine_fail(e, MPS3_ECONFIRM, false);
		return MPS3_ECONFIRM;
	}
	return MPS3_EOK; /* SWAP_STREAM_CLEARING */
}

int mps3_engine_stream_clearing(struct mps3_icap_engine *e,
				const u8 *buf, u32 len)
{
	mps3_swap_transition_inputs_t in;
	u32 words_total, words_done = 0;

	if (e->st != SWAP_STREAM_CLEARING)
		return MPS3_ESTATE;
	if (buf == NULL || (len & 3u) != 0) {
		engine_fail(e, MPS3_EALIGN, false); /* already parked */
		return MPS3_EALIGN;
	}
	words_total = len / 4u;

	while (e->st == SWAP_STREAM_CLEARING) {
		u32 chunk = words_total - words_done;
		if (chunk > e->cfg.chunk_words)
			chunk = e->cfg.chunk_words;

		memset(&in, 0, sizeof(in));
		in.clearing_cache_valid = true;
		if (chunk > 0) {
			int rc = write_words(e, buf + 4u * words_done, chunk);
			if (rc)
				in.stream_error = true;
			else
				words_done += chunk;
		}
		in.clearing_stream_done = (words_done >= words_total);
		e->st = mps3_swap_next_state(e->st, &in);
		relax(e);
	}

	if (e->st == SWAP_FAILED) {
		engine_fail(e, MPS3_ESTREAM, true);
		return MPS3_ESTREAM;
	}

	/* AWAIT_INCOMING_CLEARING: under Linux the incoming pair's clearing
	 * is staged by the userspace daemon (it becomes the NEXT swap's
	 * outgoing clearing) and never touches the driver — auto-ack the
	 * table arc. The ICAP-ordering invariant that matters (clearing
	 * before partial into the fabric) is enforced by clearing_streamed. */
	memset(&in, 0, sizeof(in));
	in.incoming_clearing_ready = true;
	e->st = mps3_swap_next_state(e->st, &in); /* -> AWAIT_PARTIAL */
	e->clearing_streamed = true;
	return MPS3_EOK;
}

/* Common tail: walk STREAM_PARTIAL with a staged buffer. */
static int stream_partial_walk(struct mps3_icap_engine *e,
			       const u8 *buf, u32 words_total)
{
	mps3_swap_transition_inputs_t in;
	u32 words_done = 0;

	while (e->st == SWAP_STREAM_PARTIAL) {
		u32 chunk = words_total - words_done;
		if (chunk > e->cfg.chunk_words)
			chunk = e->cfg.chunk_words;

		memset(&in, 0, sizeof(in));
		if (chunk > 0) {
			int rc = write_words(e, buf + 4u * words_done, chunk);
			if (rc)
				in.stream_error = true;
			else
				words_done += chunk;
		}
		in.partial_stream_done = (words_done >= words_total);
		e->st = mps3_swap_next_state(e->st, &in);
		relax(e);
	}
	if (e->st == SWAP_FAILED) {
		engine_fail(e, MPS3_ESTREAM, true);
		return MPS3_ESTREAM;
	}
	return MPS3_EOK; /* SWAP_RELEASE */
}

/* Post-partial end-of-sequence capture (I18(3)): bounded SR poll, keep the
 * last NON-ZERO SR word. Whether SR_EOS asserts on this axi_hwicap build is
 * STILL OPEN — default is capture-only (SERVICE_DISPOSITION §4: "capture
 * the same raw SR, not gate success on EOS alone"); eos_strict=1 restores
 * the firmware icap_direct_finish fail-closed gate. */
static int eos_capture(struct mps3_icap_engine *e)
{
	u32 spins;

	for (spins = 0; spins < e->cfg.eos_poll_max; spins++) {
		u32 sr = rd(e, MPS3_BLK_HWICAP, HWICAP_SR);
		if (sr != 0u)
			e->sr_last = sr;
		if (sr & HWICAP_SR_EOS) {
			e->eos_status = MPS3_ICAP_EOS_SEEN;
			return MPS3_EOK;
		}
		if ((spins & (RELAX_EVERY - 1)) == RELAX_EVERY - 1)
			relax(e);
	}
	/* Best-effort: a non-asserting SR_EOS post-DESYNC is an open question on
	 * this axi_hwicap build. eos_status=TIMEOUT is surfaced via sysfs (and the
	 * swap_worker "settled" log) — the discriminator for the "150s silent then
	 * config lost" failure — so no engine-side printk (this file is dual-target,
	 * host + kernel). Bounded by the SHORT eos_poll_max (not done_poll_max) so
	 * this fails fast + diagnostic instead of spinning ~150s. */
	e->eos_status = MPS3_ICAP_EOS_TIMEOUT;
	return e->cfg.eos_strict ? MPS3_EEOS : MPS3_EOK;
}

int mps3_engine_stream_partial(struct mps3_icap_engine *e,
			       const u8 *buf, u32 len, u32 target_rm_id)
{
	mps3_swap_transition_inputs_t in;
	int rc;

	if (e->st != SWAP_AWAIT_PARTIAL)
		return MPS3_ESTATE;
	if (!e->clearing_streamed)
		return MPS3_EORDER; /* pair ordering: clearing first, always */
	if (buf == NULL || (len & 3u) != 0 || len == 0) {
		engine_fail(e, MPS3_EALIGN, true);
		return MPS3_EALIGN;
	}

	e->target_rm_id = target_rm_id;
	memset(&in, 0, sizeof(in));
	in.partial_ready = true;
	e->st = mps3_swap_next_state(e->st, &in); /* -> STREAM_PARTIAL */

	rc = stream_partial_walk(e, buf, len / 4u);
	if (rc)
		return rc;

	rc = eos_capture(e);
	if (rc) {
		engine_fail(e, rc, true);
		return rc;
	}
	return MPS3_EOK; /* SWAP_RELEASE — call mps3_engine_finish() */
}

/* ---- stream-direct (Path 3) ---------------------------------------------- */

int mps3_engine_sd_begin(struct mps3_icap_engine *e, u32 expect_bytes,
			 u32 expect_crc, u32 target_rm_id)
{
	u32 status;

	/* Ported icap_direct_begin, BOTH gates:
	 * (1) the engine must actually be awaiting a partial (the 2026-07-10
	 *     silicon fix — a parked RP after an earlier rejection is NOT an
	 *     armed swap; 1,083,360 bytes once sailed through on that bug);
	 * (2) DFXCTL must confirm decoupled + rp_in_reset — never stream
	 *     config frames into a live RP. */
	if (e->st != SWAP_AWAIT_PARTIAL)
		return MPS3_ESTATE;
	if (!e->clearing_streamed)
		return MPS3_EORDER;
	/* expect_bytes==0: UNFRAMED (fpga-mgr image, no wire header). */
	if ((expect_bytes & 3u) != 0)
		return MPS3_EALIGN;

	status = rd(e, MPS3_BLK_DFXCTL, DFXCTL_STATUS);
	if ((status & (DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET))
	    != (DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET))
		return MPS3_ESTATE;

	e->sd_active = true;
	e->sd_framed = (expect_bytes != 0);
	e->sd_word = 0;
	e->sd_word_n = 0;
	e->sd_crc = MPS3_CRC32_INIT;
	e->sd_bytes = 0;
	e->sd_expect_bytes = expect_bytes;
	e->sd_expect_crc = expect_crc;
	e->target_rm_id = target_rm_id;
	e->batch_n = 0;
	return MPS3_EOK;
}

static int sd_flush_batch(struct mps3_icap_engine *e)
{
	int rc;

	if (e->batch_n == 0)
		return MPS3_EOK;
	if (e->cfg.fifo_mode)
		rc = fifo_drain(e, e->batch, e->batch_n);
	else {
		u32 i;
		rc = MPS3_EOK;
		for (i = 0; i < e->batch_n && rc == MPS3_EOK; i++)
			rc = lite_write_word(e, e->batch[i]);
	}
	if (rc)
		return rc;
	e->batch_n = 0;
	return MPS3_EOK;
}

int mps3_engine_sd_write(struct mps3_icap_engine *e, const u8 *buf, u32 len)
{
	if (!e->sd_active || e->st != SWAP_AWAIT_PARTIAL)
		return MPS3_ESTATE;
	if (e->sd_framed && e->sd_bytes + len > e->sd_expect_bytes) {
		/* bytes beyond the declared frame: reject, park */
		mps3_engine_sd_abort(e);
		engine_fail(e, MPS3_EALIGN, true);
		return MPS3_EALIGN;
	}

	e->sd_crc = mps3_crc32_update(e->sd_crc, buf, len);
	e->sd_bytes += len;

	/* Assemble MSB-first words; a config word can straddle two write()
	 * calls (<=3-byte residue carried in sd_word/sd_word_n — ported
	 * icap_direct_write, including the exact shift order). */
	while (len > 0) {
		e->sd_word = (e->sd_word << 8) | (u32)(*buf++); /* first byte -> MSB */
		e->sd_word_n++;
		len--;
		if (e->sd_word_n == 4) {
			e->batch[e->batch_n++] = e->sd_word;
			if (e->batch_n == MPS3_ICAP_BATCH_WORDS) {
				int rc = sd_flush_batch(e);
				if (rc) {
					mps3_engine_sd_abort(e);
					engine_fail(e, MPS3_ESTREAM, true);
					return MPS3_ESTREAM;
				}
				relax(e);
			}
			e->sd_word = 0;
			e->sd_word_n = 0;
		}
	}
	return MPS3_EOK;
}

int mps3_engine_sd_finish(struct mps3_icap_engine *e)
{
	mps3_swap_transition_inputs_t in;
	int rc;

	if (!e->sd_active || e->st != SWAP_AWAIT_PARTIAL)
		return MPS3_ESTATE;

	if (e->sd_word_n != 0 ||
	    (e->sd_framed && e->sd_bytes != e->sd_expect_bytes)) {
		mps3_engine_sd_abort(e);
		engine_fail(e, MPS3_EALIGN, true);
		return MPS3_EALIGN;
	}
	/* Containment gate: the bytes are already IN the fabric, but the RP
	 * is decoupled — a failed CRC fails the swap parked, the host
	 * retries. (Stream-direct's documented tradeoff, SERVICE_DISPOSITION
	 * §3.3.) Unframed mode has no wire CRC to check. */
	if (e->sd_framed && mps3_crc32_final(e->sd_crc) != e->sd_expect_crc) {
		mps3_engine_sd_abort(e);
		engine_fail(e, MPS3_ECRC, true);
		return MPS3_ECRC;
	}
	rc = sd_flush_batch(e);
	if (rc) {
		mps3_engine_sd_abort(e);
		engine_fail(e, MPS3_ESTREAM, true);
		return MPS3_ESTREAM;
	}
	rc = eos_capture(e);
	if (rc) {
		mps3_engine_sd_abort(e);
		engine_fail(e, rc, true);
		return rc;
	}
	e->sd_active = false;

	/* Hand-off + no-op STREAM_PARTIAL walk (the whole partial is already
	 * in the ICAP — mirrors firmware in_icap fast path). */
	memset(&in, 0, sizeof(in));
	in.partial_ready = true;
	e->st = mps3_swap_next_state(e->st, &in); /* -> STREAM_PARTIAL */
	memset(&in, 0, sizeof(in));
	in.partial_stream_done = true;
	e->st = mps3_swap_next_state(e->st, &in); /* -> RELEASE */
	return MPS3_EOK;
}

void mps3_engine_sd_abort(struct mps3_icap_engine *e)
{
	/* Ported icap_direct_abort: words already pushed are in the fabric,
	 * but the RP is parked inert; just reset the packer. */
	e->sd_word = 0;
	e->sd_word_n = 0;
	e->batch_n = 0;
	e->sd_active = false;
}

/* ---- release + verify + commit / re-isolate ------------------------------ */

int mps3_engine_finish(struct mps3_icap_engine *e)
{
	mps3_swap_transition_inputs_t in;
	u32 polls;
	int verify_err = 0;
	u32 rm_id = 0;

	if (e->st != SWAP_RELEASE)
		return MPS3_ESTATE;

	/* Swap-scoped invariant (TRANSPLANT_CONTRACT §9.4): reset any
	 * static-side hostio4_target BETWEEN partial-stream-done and release,
	 * while the RP is still decoupled + in reset — so the new RM can
	 * never see a stale-state target. Optional hook: no hostio4_target
	 * exists in the current static shell. */
	if (e->ops->hostio4_reset) {
		e->hostio4_calls++;
		(void)e->ops->hostio4_reset(e->ctx);
	}

	/* RELEASE: deassert decouple, release rp_resetn. AXI SHUTDOWN stays
	 * ASSERTED across release — it is cleared at the commit point only,
	 * once the rm_id proves the right RM loaded (an unverified RM can
	 * drive its id but cannot master the bus). */
	polls = 0;
	while (e->st == SWAP_RELEASE) {
		u32 status;

		clr_bits(e, MPS3_BLK_DFXCTL, DFXCTL_DECOUPLE, DFXCTL_DECOUPLE_EN);
		set_bits(e, MPS3_BLK_CLKRST, CLKRST_RESET_CTRL,
			 CLKRST_RESET_CTRL_RP_RESETN);

		memset(&in, 0, sizeof(in));
		status = rd(e, MPS3_BLK_DFXCTL, DFXCTL_STATUS);
		in.release_confirmed =
			(status & (DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET)) == 0;
		if (!in.release_confirmed && ++polls >= e->cfg.confirm_poll_max)
			in.confirm_timeout = true;
		e->st = mps3_swap_next_state(e->st, &in);
		if ((polls & (RELAX_EVERY - 1)) == RELAX_EVERY - 1)
			relax(e);
	}
	if (e->st == SWAP_FAILED) {
		/* Release never confirmed. Firmware reasons "the RP is still
		 * decoupled and still held in reset — already inert" and
		 * parks; we additionally re-drive the park writes so a
		 * HALF-taken release (decouple dropped, reset still held)
		 * cannot escape the parked-failure invariant. Deliberate
		 * belt-and-suspenders, documented in README (delta 3). */
		engine_fail(e, MPS3_ECONFIRM, true);
		return MPS3_ECONFIRM;
	}

	/* VERIFY: with the RP CONNECTED. rm_id_valid is dfx_ctl's settle
	 * qualifier — until it asserts, RM_ID is meaningless: keep polling.
	 * Valid+wrong is decided immediately (never spins the timeout). */
	polls = 0;
	while (e->st == SWAP_VERIFY) {
		u32 rm_status = rd(e, MPS3_BLK_DFXCTL, DFXCTL_RM_STATUS);
		bool id_valid = (rm_status & DFXCTL_RM_STATUS_RM_ID_VALID) != 0;

		rm_id = rd(e, MPS3_BLK_DFXCTL, DFXCTL_RM_ID);
		memset(&in, 0, sizeof(in));
		in.verify_ok = id_valid && (rm_id == e->target_rm_id);
		in.verify_mismatch = id_valid && (rm_id != e->target_rm_id);
		if (!id_valid && ++polls >= e->cfg.confirm_poll_max)
			in.confirm_timeout = true;

		if (in.verify_mismatch)
			verify_err = MPS3_EVERIFY;
		else if (in.confirm_timeout)
			verify_err = MPS3_EVERIFYTMO;

		e->st = mps3_swap_next_state(e->st, &in);

		if (e->st == SWAP_CACHE_CLEARING) {
			/* THE commit point (ported step_verify): only now —
			 * with a settled, correct rm_id read back through a
			 * released decoupler — let the RP master the bus and
			 * let the DUT run. dbg_resetn released too (nanoSoC
			 * ANDs all three into sys_sysresetn; a swapped SoC
			 * with dbg held would never boot — silicon lesson). */
			clr_bits(e, MPS3_BLK_DFXCTL, DFXCTL_SHUTDOWN,
				 DFXCTL_SHUTDOWN_AXI);
			set_bits(e, MPS3_BLK_CLKRST, CLKRST_RESET_CTRL,
				 CLKRST_RESET_CTRL_DUT_RESETN |
				 CLKRST_RESET_CTRL_DBG_RESETN);
			e->current_rm_id = rm_id;
		}
		if ((polls & (RELAX_EVERY - 1)) == RELAX_EVERY - 1)
			relax(e);
	}

	if (e->st == SWAP_CACHE_CLEARING) {
		/* Clearing-cache promote is daemon bookkeeping under Linux
		 * (the daemon holds the resident clearing); the table arc is
		 * unconditional. */
		memset(&in, 0, sizeof(in));
		e->st = mps3_swap_next_state(e->st, &in); /* -> DONE */
	}

	if (e->st == SWAP_REISOLATE) {
		/* Put the unknown RM back in the box BEFORE reporting. */
		polls = 0;
		while (e->st == SWAP_REISOLATE) {
			u32 status;
			const u32 isolated =
				DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET;

			set_bits(e, MPS3_BLK_DFXCTL, DFXCTL_SHUTDOWN,
				 DFXCTL_SHUTDOWN_AXI);
			set_bits(e, MPS3_BLK_DFXCTL, DFXCTL_DECOUPLE,
				 DFXCTL_DECOUPLE_EN);
			clr_bits(e, MPS3_BLK_CLKRST, CLKRST_RESET_CTRL,
				 CLKRST_RESET_CTRL_RP_RESETN |
				 CLKRST_RESET_CTRL_DUT_RESETN |
				 CLKRST_RESET_CTRL_DBG_RESETN);

			memset(&in, 0, sizeof(in));
			status = rd(e, MPS3_BLK_DFXCTL, DFXCTL_STATUS);
			in.decouple_confirmed = (status & isolated) == isolated;
			if (!in.decouple_confirmed &&
			    ++polls >= e->cfg.confirm_poll_max)
				in.confirm_timeout = true;
			e->st = mps3_swap_next_state(e->st, &in);
			if ((polls & (RELAX_EVERY - 1)) == RELAX_EVERY - 1)
				relax(e);
		}
		/* -> SWAP_FAILED */
		engine_fail(e, verify_err ? verify_err : MPS3_ECONFIRM, false);
		return e->last.err;
	}

	if (e->st == SWAP_DONE) {
		e->last.valid = true;
		e->last.ok = true;
		e->last.verified = true; /* DONE is only reachable through a
					  * passed verify (ported v1 rule) */
		e->last.rm_id = e->current_rm_id;
		e->last.err = 0;
		memset(&in, 0, sizeof(in));
		e->st = mps3_swap_next_state(e->st, &in); /* -> IDLE */
		e->clearing_streamed = false;
		return MPS3_EOK;
	}

	/* Unreachable if the table is total; fail closed anyway. */
	engine_fail(e, MPS3_ESTATE, true);
	return MPS3_ESTATE;
}

void mps3_engine_abort_park(struct mps3_icap_engine *e)
{
	if (e->st == SWAP_IDLE)
		return;
	mps3_engine_sd_abort(e);
	engine_fail(e, MPS3_EABORT, true);
}
