/*
 * test_engine_host.c — full engine-vs-mock swap scenarios on the host.
 *
 * This is the fast, always-runnable half of the two-tier harness (the QEMU
 * run exercises the SAME engine + mock inside the real rv32 kernel; this
 * proves the sequencing/protocol logic in seconds on any box). Synthetic
 * config streams carry the real landmarks (dummy pad, bus-width words, the
 * AA995566 sync, type-1 NOOPs, the DESYNC command) so the mock's capture
 * checks mean something.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "../../mps3_icap_engine.h"
#include "../../mps3_icap_mock.h"
#include "../../mps3_crc32.h"

static int fails;
#define CHECK(cond) do { \
	if (!(cond)) { \
		fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
		fails++; \
	} \
} while (0)

/* ---- ops glue ------------------------------------------------------------ */
static int relax_calls, hostio_calls;

static u32 t_rd(void *ctx, int blk, u32 off) { return mps3_mock_rd(ctx, blk, off); }
static void t_wr(void *ctx, int blk, u32 off, u32 v) { mps3_mock_wr(ctx, blk, off, v); }
static void t_relax(void *ctx) { (void)ctx; relax_calls++; }
static int t_hostio(void *ctx) { (void)ctx; hostio_calls++; return 0; }

static const struct mps3_icap_ops OPS = {
	.rd = t_rd, .wr = t_wr, .relax = t_relax, .hostio4_reset = t_hostio,
};

/* ---- synthetic bitstreams ------------------------------------------------ */
/* words, serialised big-endian into `out` (cap in words). Returns bytes. */
static u32 mk_stream(u8 *out, u32 nwords_body)
{
	u32 n = 0, i;
	u32 w[4096];

	w[n++] = 0xFFFFFFFFu;             /* dummy pad                */
	w[n++] = 0x000000BBu;             /* bus width sync           */
	w[n++] = 0x11220044u;             /* bus width detect         */
	w[n++] = 0xFFFFFFFFu;
	w[n++] = MPS3_ICAP_SYNC_WORD;     /* AA995566                 */
	w[n++] = 0x20000000u;             /* type-1 NOOP              */
	for (i = 0; i < nwords_body; i++)
		w[n++] = 0x20000000u ^ (i * 0x01010101u); /* "frames"  */
	w[n++] = 0x30008001u;             /* type-1 write CMD         */
	w[n++] = MPS3_ICAP_DESYNC_CMD;    /* DESYNC                   */
	w[n++] = 0x20000000u;
	w[n++] = 0x20000000u;

	for (i = 0; i < n; i++) {
		out[4 * i + 0] = (u8)(w[i] >> 24);
		out[4 * i + 1] = (u8)(w[i] >> 16);
		out[4 * i + 2] = (u8)(w[i] >> 8);
		out[4 * i + 3] = (u8)(w[i]);
	}
	return 4 * n;
}

/* Shared fixtures */
static u8 clearing[64 * 1024], partial[64 * 1024];
static u32 clearing_len, partial_len;

#define RM_ID_OK 0x010000A1u /* rm_regdemo_a, rm_list.tcl v2 encoding */

static void fresh(struct mps3_icap_engine *e, struct mps3_mock *m,
		  u32 faults, u32 next_rm_id, const struct mps3_icap_cfg *cfgin)
{
	struct mps3_mock_cfg mc;
	struct mps3_icap_cfg cfg;

	memset(&mc, 0, sizeof(mc));
	mc.next_rm_id = next_rm_id;
	mc.faults = faults;
	mc.status_lat = 2;
	mc.valid_lat = 3;
	mc.cr_lat = 1;
	mc.fifo_depth = 1024;
	mps3_mock_init(m, &mc);

	if (cfgin) {
		cfg = *cfgin;
	} else {
		cfg.fifo_mode = 1;
		cfg.done_poll_max = 10000;
		cfg.confirm_poll_max = 1000;
		cfg.chunk_words = 128;
		cfg.eos_strict = 0;
	}
	mps3_engine_init(e, &OPS, m, &cfg);
}

static int is_parked(struct mps3_mock *m)
{
	/* drain the settle latency the same way a host would: poll STATUS */
	int i;
	u32 st = 0;
	for (i = 0; i < 16; i++)
		st = mps3_mock_rd(m, MPS3_BLK_DFXCTL, DFXCTL_STATUS);
	return (st & (DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET))
	       == (DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET);
}

/* ---- scenarios ------------------------------------------------------------ */

static void t_happy_staged_and_sd(int stream_direct, int fifo_mode)
{
	struct mps3_icap_engine e;
	struct mps3_mock m;
	struct mps3_icap_cfg cfg = { .fifo_mode = fifo_mode,
		.done_poll_max = 10000, .confirm_poll_max = 1000,
		.chunk_words = 128, .eos_strict = 0 };
	u32 expect_crc;
	int rc;

	fresh(&e, &m, 0, RM_ID_OK, &cfg);
	mps3_mock_arm_capture(&m);

	rc = mps3_engine_swap_begin(&e);
	CHECK(rc == MPS3_EOK);
	CHECK(e.st == SWAP_STREAM_CLEARING);
	CHECK(is_parked(&m));

	rc = mps3_engine_stream_clearing(&e, clearing, clearing_len);
	CHECK(rc == MPS3_EOK);
	CHECK(e.st == SWAP_AWAIT_PARTIAL);

	/* pair ordering: a second clearing push is not a legal engine call
	 * now; a partial without ordering violation proceeds */
	if (stream_direct) {
		u32 crc = mps3_crc32(partial, partial_len);
		u32 off = 0;
		/* deliberately awkward split sizes: 1,2,3,509... proves the
		 * <=3-byte residue straddle (a word across TCP segments) */
		static const u32 cuts[] = { 1, 2, 3, 509, 4096 };
		u32 ci = 0;
		rc = mps3_engine_sd_begin(&e, partial_len, crc, RM_ID_OK);
		CHECK(rc == MPS3_EOK);
		while (off < partial_len && rc == MPS3_EOK) {
			u32 n = cuts[ci % 5]; ci++;
			if (n > partial_len - off)
				n = partial_len - off;
			rc = mps3_engine_sd_write(&e, partial + off, n);
			off += n;
		}
		CHECK(rc == MPS3_EOK);
		rc = mps3_engine_sd_finish(&e);
		CHECK(rc == MPS3_EOK);
	} else {
		rc = mps3_engine_stream_partial(&e, partial, partial_len, RM_ID_OK);
		CHECK(rc == MPS3_EOK);
	}
	CHECK(e.st == SWAP_RELEASE);

	hostio_calls = 0;
	rc = mps3_engine_finish(&e);
	CHECK(rc == MPS3_EOK);
	CHECK(e.st == SWAP_IDLE);
	CHECK(e.last.valid && e.last.ok && e.last.verified);
	CHECK(e.last.rm_id == RM_ID_OK);
	CHECK(e.current_rm_id == RM_ID_OK);
	CHECK(hostio_calls == 1); /* hook ran, exactly once, pre-release */

	/* byte-exactness: mock re-serialised stream CRC == CRC of the two
	 * source files concatenated (catches ANY packing/byte-order drift) */
	expect_crc = mps3_crc32_final(mps3_crc32_update(
			mps3_crc32_update(MPS3_CRC32_INIT, clearing, clearing_len),
			partial, partial_len));
	CHECK(mps3_mock_stream_crc(&m) == expect_crc);
	CHECK(m.words_total == (clearing_len + partial_len) / 4);
	CHECK(m.saw_sync == 1);
	CHECK(m.saw_desync == 1);
	CHECK(m.fifo_overflow == 0);
	CHECK(e.icap_bytes == clearing_len + partial_len);
	CHECK(e.eos_status == MPS3_ICAP_EOS_SEEN);
	CHECK(e.sr_last != 0);

	/* released: not parked */
	CHECK(!is_parked(&m));
	/* shutdown deasserted at commit */
	CHECK((m.shutdown_reg & DFXCTL_SHUTDOWN_AXI) == 0);
	/* dut+dbg+rp all released */
	CHECK((m.clkrst_reg & 0x7u) == 0x7u);
}

static void t_wrong_rm_id(void)
{
	struct mps3_icap_engine e;
	struct mps3_mock m;
	int rc;

	fresh(&e, &m, 0, 0xDEADBEEFu, NULL); /* RP presents the WRONG id */
	mps3_mock_arm_capture(&m);
	CHECK(mps3_engine_swap_begin(&e) == MPS3_EOK);
	CHECK(mps3_engine_stream_clearing(&e, clearing, clearing_len) == MPS3_EOK);
	CHECK(mps3_engine_stream_partial(&e, partial, partial_len, RM_ID_OK) == MPS3_EOK);
	rc = mps3_engine_finish(&e);
	CHECK(rc == MPS3_EVERIFY);
	CHECK(e.st == SWAP_IDLE);
	CHECK(e.last.valid && !e.last.ok && !e.last.verified);
	/* re-isolated: parked again after the mismatch */
	CHECK(is_parked(&m));
	/* current rm_id NOT updated by a failed swap */
	CHECK(e.current_rm_id != 0xDEADBEEFu);
}

static void t_rm_id_never_valid(void)
{
	struct mps3_icap_engine e;
	struct mps3_mock m;
	int rc;

	fresh(&e, &m, MPS3_MOCK_FAULT_RMID_NEVER, RM_ID_OK, NULL);
	CHECK(mps3_engine_swap_begin(&e) == MPS3_EOK);
	CHECK(mps3_engine_stream_clearing(&e, clearing, clearing_len) == MPS3_EOK);
	CHECK(mps3_engine_stream_partial(&e, partial, partial_len, RM_ID_OK) == MPS3_EOK);
	rc = mps3_engine_finish(&e);
	CHECK(rc == MPS3_EVERIFYTMO);
	CHECK(is_parked(&m));
}

static void t_cr_stuck(void)
{
	struct mps3_icap_engine e;
	struct mps3_mock m;
	struct mps3_icap_cfg cfg = { .fifo_mode = 1, .done_poll_max = 200,
		.confirm_poll_max = 200, .chunk_words = 64, .eos_strict = 0 };
	int rc;

	fresh(&e, &m, MPS3_MOCK_FAULT_CR_STUCK, RM_ID_OK, &cfg);
	CHECK(mps3_engine_swap_begin(&e) == MPS3_EOK);
	rc = mps3_engine_stream_clearing(&e, clearing, clearing_len);
	CHECK(rc == MPS3_ESTREAM);
	CHECK(e.st == SWAP_IDLE);
	CHECK(e.last.valid && !e.last.ok);
	CHECK(is_parked(&m)); /* fail closed to parked-decoupled */
}

static void t_wfv_stuck(void)
{
	struct mps3_icap_engine e;
	struct mps3_mock m;
	struct mps3_icap_cfg cfg = { .fifo_mode = 1, .done_poll_max = 200,
		.confirm_poll_max = 200, .chunk_words = 64, .eos_strict = 0 };

	fresh(&e, &m, MPS3_MOCK_FAULT_WFV_STUCK0, RM_ID_OK, &cfg);
	CHECK(mps3_engine_swap_begin(&e) == MPS3_EOK);
	CHECK(mps3_engine_stream_clearing(&e, clearing, clearing_len) == MPS3_ESTREAM);
	CHECK(is_parked(&m));
}

static void t_decouple_never(void)
{
	struct mps3_icap_engine e;
	struct mps3_mock m;
	struct mps3_icap_cfg cfg = { .fifo_mode = 1, .done_poll_max = 200,
		.confirm_poll_max = 50, .chunk_words = 64, .eos_strict = 0 };

	fresh(&e, &m, MPS3_MOCK_FAULT_DECOUPLE_NEVER, RM_ID_OK, &cfg);
	CHECK(mps3_engine_swap_begin(&e) == MPS3_ECONFIRM);
	CHECK(e.st == SWAP_IDLE);
	CHECK(e.last.valid && !e.last.ok);
}

static void t_release_never(void)
{
	struct mps3_icap_engine e;
	struct mps3_mock m;
	struct mps3_icap_cfg cfg = { .fifo_mode = 1, .done_poll_max = 10000,
		.confirm_poll_max = 50, .chunk_words = 64, .eos_strict = 0 };

	fresh(&e, &m, MPS3_MOCK_FAULT_RELEASE_NEVER, RM_ID_OK, &cfg);
	CHECK(mps3_engine_swap_begin(&e) == MPS3_EOK);
	CHECK(mps3_engine_stream_clearing(&e, clearing, clearing_len) == MPS3_EOK);
	CHECK(mps3_engine_stream_partial(&e, partial, partial_len, RM_ID_OK) == MPS3_EOK);
	CHECK(mps3_engine_finish(&e) == MPS3_ECONFIRM);
	CHECK(is_parked(&m)); /* belt-and-suspenders repark took effect */
}

static void t_ordering(void)
{
	struct mps3_icap_engine e;
	struct mps3_mock m;

	/* partial without a clearing this session -> EORDER, and NOTHING
	 * reaches the ICAP */
	fresh(&e, &m, 0, RM_ID_OK, NULL);
	mps3_mock_arm_capture(&m);
	CHECK(mps3_engine_swap_begin(&e) == MPS3_EOK);
	CHECK(e.st == SWAP_STREAM_CLEARING);
	CHECK(mps3_engine_stream_partial(&e, partial, partial_len, RM_ID_OK) == MPS3_ESTATE);
	CHECK(mps3_engine_sd_begin(&e, partial_len, 0, RM_ID_OK) == MPS3_ESTATE);
	CHECK(m.words_total == 0);

	/* stream calls before arming -> ESTATE */
	fresh(&e, &m, 0, RM_ID_OK, NULL);
	CHECK(mps3_engine_stream_clearing(&e, clearing, clearing_len) == MPS3_ESTATE);
	CHECK(mps3_engine_stream_partial(&e, partial, partial_len, RM_ID_OK) == MPS3_ESTATE);
	CHECK(mps3_engine_finish(&e) == MPS3_ESTATE);
	CHECK(m.words_total == 0);
}

static void t_sd_crc_mismatch(void)
{
	struct mps3_icap_engine e;
	struct mps3_mock m;
	int rc;

	fresh(&e, &m, 0, RM_ID_OK, NULL);
	CHECK(mps3_engine_swap_begin(&e) == MPS3_EOK);
	CHECK(mps3_engine_stream_clearing(&e, clearing, clearing_len) == MPS3_EOK);
	rc = mps3_engine_sd_begin(&e, partial_len, 0x12345678u /* wrong */, RM_ID_OK);
	CHECK(rc == MPS3_EOK);
	CHECK(mps3_engine_sd_write(&e, partial, partial_len) == MPS3_EOK);
	rc = mps3_engine_sd_finish(&e);
	CHECK(rc == MPS3_ECRC);
	CHECK(e.st == SWAP_IDLE);
	CHECK(is_parked(&m)); /* containment: bytes hit the fabric, RP parked */
}

static void t_abort_park(void)
{
	struct mps3_icap_engine e;
	struct mps3_mock m;

	fresh(&e, &m, 0, RM_ID_OK, NULL);
	CHECK(mps3_engine_swap_begin(&e) == MPS3_EOK);
	CHECK(mps3_engine_stream_clearing(&e, clearing, clearing_len) == MPS3_EOK);
	mps3_engine_abort_park(&e); /* the 30s RX-idle reap lands here */
	CHECK(e.st == SWAP_IDLE);
	CHECK(e.last.valid && !e.last.ok && e.last.err == MPS3_EABORT);
	CHECK(is_parked(&m));
	/* a fresh swap is startable after the reap */
	CHECK(mps3_engine_swap_begin(&e) == MPS3_EOK);
	CHECK(mps3_engine_swap_begin(&e) == MPS3_ESTATE); /* single-swap-at-a-time */
}

static void t_eos_strict_and_never(void)
{
	struct mps3_icap_engine e;
	struct mps3_mock m;
	struct mps3_icap_cfg cfg = { .fifo_mode = 1, .done_poll_max = 200,
		.confirm_poll_max = 1000, .chunk_words = 128, .eos_strict = 0 };
	int rc;

	/* capture-only default: EOS never asserts -> swap still succeeds,
	 * eos_status records TIMEOUT (SERVICE_DISPOSITION §4 open question) */
	fresh(&e, &m, MPS3_MOCK_FAULT_EOS_NEVER, RM_ID_OK, &cfg);
	CHECK(mps3_engine_swap_begin(&e) == MPS3_EOK);
	CHECK(mps3_engine_stream_clearing(&e, clearing, clearing_len) == MPS3_EOK);
	CHECK(mps3_engine_stream_partial(&e, partial, partial_len, RM_ID_OK) == MPS3_EOK);
	CHECK(e.eos_status == MPS3_ICAP_EOS_TIMEOUT);
	CHECK(mps3_engine_finish(&e) == MPS3_EOK);
	CHECK(e.last.ok);

	/* strict mode: same fault now fails the swap parked (the firmware
	 * icap_direct_finish behaviour) */
	cfg.eos_strict = 1;
	fresh(&e, &m, MPS3_MOCK_FAULT_EOS_NEVER, RM_ID_OK, &cfg);
	CHECK(mps3_engine_swap_begin(&e) == MPS3_EOK);
	CHECK(mps3_engine_stream_clearing(&e, clearing, clearing_len) == MPS3_EOK);
	rc = mps3_engine_stream_partial(&e, partial, partial_len, RM_ID_OK);
	CHECK(rc == MPS3_EEOS);
	CHECK(is_parked(&m));
}

static void t_sd_unframed(void)
{
	/* fpga-mgr glue mode: expect_bytes==0 -> no length/CRC frame gate,
	 * everything else (arm gate, ordering, packing, EOS) identical */
	struct mps3_icap_engine e;
	struct mps3_mock m;

	fresh(&e, &m, 0, RM_ID_OK, NULL);
	mps3_mock_arm_capture(&m);
	CHECK(mps3_engine_swap_begin(&e) == MPS3_EOK);
	CHECK(mps3_engine_stream_clearing(&e, clearing, clearing_len) == MPS3_EOK);
	CHECK(mps3_engine_sd_begin(&e, 0, 0, RM_ID_OK) == MPS3_EOK);
	CHECK(mps3_engine_sd_write(&e, partial, partial_len) == MPS3_EOK);
	CHECK(mps3_engine_sd_finish(&e) == MPS3_EOK);
	CHECK(mps3_engine_finish(&e) == MPS3_EOK);
	CHECK(e.last.ok && e.last.rm_id == RM_ID_OK);
	CHECK(m.words_total == (clearing_len + partial_len) / 4);
}

static void t_lite_mode(void)
{
	/* full happy path again under the LITE protocol (one StartConfig per
	 * word, the on-silicon bare-metal shell's mode) with FIFO depth 1 */
	t_happy_staged_and_sd(0, 0);
	t_happy_staged_and_sd(1, 0);
}

int main(void)
{
	clearing_len = mk_stream(clearing, 200);
	partial_len = mk_stream(partial, 2000);

	t_happy_staged_and_sd(0, 1);   /* staged partial, FIFO mode  */
	t_happy_staged_and_sd(1, 1);   /* stream-direct, FIFO mode   */
	t_lite_mode();                  /* both, LITE mode            */
	t_wrong_rm_id();
	t_rm_id_never_valid();
	t_cr_stuck();
	t_wfv_stuck();
	t_decouple_never();
	t_release_never();
	t_ordering();
	t_sd_crc_mismatch();
	t_sd_unframed();
	t_abort_park();
	t_eos_strict_and_never();

	if (fails) {
		printf("test_engine_host: %d FAILURES\n", fails);
		return 1;
	}
	printf("test_engine_host: PASS (relax points hit: %d)\n", relax_calls);
	return 0;
}
