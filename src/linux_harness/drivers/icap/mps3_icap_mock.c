/*
 * mps3_icap_mock.c — behavioural mock (see mps3_icap_mock.h for the model
 * and the mechanism justification). Pure C: compiles into the kernel module
 * (mock=1) and into the host unit tests unchanged.
 */
#include "mps3_icap_mock.h"
#include "mps3_crc32.h"

void mps3_mock_init(struct mps3_mock *m, const struct mps3_mock_cfg *cfg)
{
	memset(m, 0, sizeof(*m));
	if (cfg)
		m->cfg = *cfg;
	if (m->cfg.fifo_depth == 0)
		m->cfg.fifo_depth = 1024u;
	/* Power-on architectural state: decoupled? No — the shell boots with
	 * the RP released and greybox resident; resets released (1=released,
	 * matching a booted shell). rm_id 0 = greybox. */
	m->clkrst_reg = 0x7u;
	m->status_now = 0u;
	m->rm_id_now = 0u;
	m->stream_crc = MPS3_CRC32_INIT;
}

void mps3_mock_arm_capture(struct mps3_mock *m)
{
	m->words_total = 0;
	m->stream_crc = MPS3_CRC32_INIT;
	m->saw_sync = 0;
	m->saw_desync = 0;
	m->partial_era = 0;
	m->sync_count = 0;
	m->fifo_overflow = 0;
	memset(m->first_words, 0, sizeof(m->first_words));
}

u32 mps3_mock_stream_crc(const struct mps3_mock *m)
{
	return mps3_crc32_final(m->stream_crc);
}

/* ---- internals ----------------------------------------------------------- */

static u32 mock_status_desired(struct mps3_mock *m)
{
	u32 want = 0;
	int decouple_en = (m->decouple_reg & DFXCTL_DECOUPLE_EN) != 0;
	int rp_held = (m->clkrst_reg & CLKRST_RESET_CTRL_RP_RESETN) == 0;

	if (decouple_en && !(m->cfg.faults & MPS3_MOCK_FAULT_DECOUPLE_NEVER))
		want |= DFXCTL_STATUS_DECOUPLED;
	if (!decouple_en && (m->cfg.faults & MPS3_MOCK_FAULT_RELEASE_NEVER))
		want |= DFXCTL_STATUS_DECOUPLED; /* release never takes */
	if (rp_held)
		want |= DFXCTL_STATUS_RP_IN_RESET;
	if (!rp_held && (m->cfg.faults & MPS3_MOCK_FAULT_RELEASE_NEVER))
		want |= DFXCTL_STATUS_RP_IN_RESET;
	return want;
}

static u32 mock_status_read(struct mps3_mock *m)
{
	u32 want = mock_status_desired(m);

	if (want != m->status_now) {
		if (m->status_settle == 0)
			m->status_settle = m->cfg.status_lat + 1u;
		if (--m->status_settle == 0)
			m->status_now = want;
	} else {
		m->status_settle = 0;
	}
	return m->status_now;
}

static void mock_capture_word(struct mps3_mock *m, u32 w)
{
	u8 be[4];

	be[0] = (u8)(w >> 24);
	be[1] = (u8)(w >> 16);
	be[2] = (u8)(w >> 8);
	be[3] = (u8)w;
	m->stream_crc = mps3_crc32_update(m->stream_crc, be, 4);

	if (m->words_total < MPS3_MOCK_FIRST_WORDS)
		m->first_words[m->words_total] = w;
	m->words_total++;

	if (w == MPS3_ICAP_SYNC_WORD) {
		m->saw_sync = 1;
		m->sync_count++;
		/* 2nd sync since arm == the incoming PARTIAL's stream: the
		 * "RP" now presents the new RM's id once released. */
		if (m->sync_count >= 2) {
			m->partial_era = 1;
			m->rm_id_now = m->cfg.next_rm_id;
		}
	}
	if (w == MPS3_ICAP_DESYNC_CMD && m->saw_sync)
		m->saw_desync = 1;
}

/* ---- ops-vtable accessors ------------------------------------------------ */

u32 mps3_mock_rd(void *ctx, int blk, u32 off)
{
	struct mps3_mock *m = (struct mps3_mock *)ctx;

	switch (blk) {
	case MPS3_BLK_HWICAP:
		switch (off) {
		case HWICAP_WFV:
			if (m->cfg.faults & MPS3_MOCK_FAULT_WFV_STUCK0)
				return 0;
			return m->cfg.fifo_depth - m->fifo_pending;
		case HWICAP_CR:
			if (m->cr_busy) {
				if (m->cfg.faults & MPS3_MOCK_FAULT_CR_STUCK)
					return HWICAP_CR_WRITE;
				if (m->cr_reads_left == 0) {
					m->cr_busy = 0;
					m->fifo_pending = 0; /* drained */
					return 0;
				}
				m->cr_reads_left--;
				return HWICAP_CR_WRITE;
			}
			return 0;
		case HWICAP_SR: {
			u32 sr = HWICAP_SR_DONE;
			if (m->saw_desync &&
			    !(m->cfg.faults & MPS3_MOCK_FAULT_EOS_NEVER))
				sr |= HWICAP_SR_EOS;
			return sr;
		}
		case HWICAP_RFO:
			return 0;
		default:
			return 0;
		}

	case MPS3_BLK_DFXCTL:
		switch (off) {
		case DFXCTL_DECOUPLE:
			return m->decouple_reg;
		case DFXCTL_SHUTDOWN:
			return m->shutdown_reg;
		case DFXCTL_STATUS:
			return mock_status_read(m);
		case DFXCTL_RM_ID:
			/* Decoupler CLAMPS rm_id to 0 while decoupled — the
			 * whole point of the R1 release-then-verify order. */
			if (m->status_now & DFXCTL_STATUS_DECOUPLED)
				return 0;
			return m->rm_id_now;
		case DFXCTL_RM_STATUS: {
			int connected =
				(m->status_now & (DFXCTL_STATUS_DECOUPLED |
						  DFXCTL_STATUS_RP_IN_RESET)) == 0;
			if (!connected ||
			    (m->cfg.faults & MPS3_MOCK_FAULT_RMID_NEVER)) {
				m->valid_settle = 0;
				return 0;
			}
			if (m->valid_settle <= m->cfg.valid_lat) {
				m->valid_settle++;
				if (m->valid_settle <= m->cfg.valid_lat)
					return 0;
			}
			return DFXCTL_RM_STATUS_RM_ID_VALID;
		}
		default:
			return 0;
		}

	case MPS3_BLK_CLKRST:
		if (off == CLKRST_RESET_CTRL)
			return m->clkrst_reg;
		return 0;

	default:
		return 0;
	}
}

void mps3_mock_wr(void *ctx, int blk, u32 off, u32 val)
{
	struct mps3_mock *m = (struct mps3_mock *)ctx;

	switch (blk) {
	case MPS3_BLK_HWICAP:
		switch (off) {
		case HWICAP_WF:
			if (m->fifo_pending >= m->cfg.fifo_depth) {
				m->fifo_overflow++;
				return; /* dropped — a pacing bug in the writer */
			}
			m->fifo_pending++;
			mock_capture_word(m, val);
			return;
		case HWICAP_CR:
			if (val & HWICAP_CR_WRITE) {
				m->cr_busy = 1;
				m->cr_reads_left = m->cfg.cr_lat;
			}
			return;
		default:
			return;
		}

	case MPS3_BLK_DFXCTL:
		if (off == DFXCTL_DECOUPLE)
			m->decouple_reg = val;
		else if (off == DFXCTL_SHUTDOWN)
			m->shutdown_reg = val;
		return;

	case MPS3_BLK_CLKRST:
		if (off == CLKRST_RESET_CTRL)
			m->clkrst_reg = val;
		return;

	default:
		return;
	}
}
