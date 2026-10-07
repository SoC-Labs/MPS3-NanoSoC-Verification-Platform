/*
 * mps3_icap_mock.h — behavioural mock of the three swap-path blocks
 * (HWICAP FIFO/lite, DFXCTL decoupler/shutdown/RM_ID, CLKRST resets).
 *
 * MECHANISM CHOICE (the "your choice, justify" from the tasking): the mock
 * is a pure-C register model plugged in at the SAME seam the real MMIO
 * uses — the engine's ops vtable — not a QEMU device model. Justification:
 *  1. qemu-system-riscv32 -M virt is the proven boot target here
 *     (linux_soc/linux/boot_qemu.sh); adding a custom HWICAP device means
 *     patching + rebuilding the Buildroot QEMU and re-proving the boot —
 *     heavy, and every mock-semantics iteration costs a QEMU rebuild.
 *  2. The register-model-behind-the-accessor-seam pattern is exactly the
 *     firmware's proven MPS3_HAL_MOCK harness (firmware/test/mock_regs.c),
 *     which caught real sequencing bugs before silicon. Same contract
 *     boundary, same class of coverage.
 *  3. In-kernel, the mock exercises the REAL driver stack under the REAL
 *     rv32 kernel in QEMU: ioctl paths, copy_from_user, vmalloc staging,
 *     mutex serialisation, the idle-timeout delayed work, module init/exit.
 *     The only thing it cannot exercise is ioremap'd MMIO ordering and a
 *     real ICAPE3 — which nothing short of the board can (see COVERAGE.md).
 *
 * The model:
 *  - DFXCTL.STATUS follows DECOUPLE/CLKRST writes after a configurable
 *    latency (counted in STATUS reads), so confirm-polls are real polls.
 *  - RM_ID presents cfg.next_rm_id once a partial-era word has been pushed;
 *    rm_id_valid asserts only while released (decoupler clamp modelled) and
 *    after valid_lat reads.
 *  - HWICAP write FIFO with real vacancy accounting; CR.WRITE self-clears
 *    after cr_lat CR reads and only then drains the FIFO into the stream
 *    capture. Lite mode is the same model with depth 1.
 *  - Stream capture: word count, zlib CRC over the words re-serialised
 *    big-endian (== the original file bytes iff packing is correct — the
 *    byte-order proof), sync/desync detection, first-words ring.
 *  - Faults: WFV stuck 0, CR stuck, decouple/release never confirm,
 *    rm_id never valid, EOS never asserts, wrong rm_id via next_rm_id.
 */
#ifndef MPS3_ICAP_MOCK_H
#define MPS3_ICAP_MOCK_H

#include "mps3_compat.h"
#include "mps3_icap_regs.h"

#define MPS3_MOCK_FAULT_WFV_STUCK0      (1u << 0)
#define MPS3_MOCK_FAULT_CR_STUCK        (1u << 1)
#define MPS3_MOCK_FAULT_DECOUPLE_NEVER  (1u << 2)
#define MPS3_MOCK_FAULT_RELEASE_NEVER   (1u << 3)
#define MPS3_MOCK_FAULT_RMID_NEVER      (1u << 4)
#define MPS3_MOCK_FAULT_EOS_NEVER       (1u << 5)

#define MPS3_MOCK_FIRST_WORDS 8u

struct mps3_mock_cfg {
	u32 next_rm_id;    /* rm_id the RP presents after this swap's partial */
	u32 faults;        /* MPS3_MOCK_FAULT_* bitmask                       */
	u32 status_lat;    /* STATUS reads before decouple/reset edges settle */
	u32 valid_lat;     /* RM_STATUS reads before rm_id_valid asserts      */
	u32 cr_lat;        /* CR reads before WRITE self-clears               */
	u32 fifo_depth;    /* write FIFO depth (1024 = this BD; 1 ~= lite)    */
};

struct mps3_mock {
	struct mps3_mock_cfg cfg;

	/* DFXCTL / CLKRST architectural state */
	u32 decouple_reg, shutdown_reg, clkrst_reg;
	u32 status_settle;      /* countdown on STATUS reads   */
	u32 status_target;      /* value STATUS settles to     */
	u32 status_now;
	u32 valid_settle;
	u32 rm_id_now;          /* id currently presented by the "RP" */

	/* HWICAP */
	u32 fifo_pending;       /* words in the write FIFO     */
	u32 cr_busy;            /* CR.WRITE latched            */
	u32 cr_reads_left;
	u32 sr_eos;             /* EOS latched (post-DESYNC)   */
	u32 fifo_overflow;      /* words written past a full FIFO (a bug) */

	/* stream capture (reset on mock_arm_capture) */
	u32 words_total;
	u32 stream_crc;         /* running zlib CRC, big-endian re-serialised */
	u32 saw_sync;
	u32 saw_desync;
	u32 partial_era;        /* set once capture sees the 2nd sync (i.e. the
				 * partial after the clearing) — used to flip
				 * rm_id_now to next_rm_id */
	u32 sync_count;
	u32 first_words[MPS3_MOCK_FIRST_WORDS];
};

void mps3_mock_init(struct mps3_mock *m, const struct mps3_mock_cfg *cfg);
void mps3_mock_arm_capture(struct mps3_mock *m); /* zero the stream capture */

/* ops-vtable-shaped accessors (ctx = struct mps3_mock *) */
u32  mps3_mock_rd(void *ctx, int blk, u32 off);
void mps3_mock_wr(void *ctx, int blk, u32 off, u32 val);

/* finalised capture CRC (zlib final xor applied) */
u32  mps3_mock_stream_crc(const struct mps3_mock *m);

#endif /* MPS3_ICAP_MOCK_H */
