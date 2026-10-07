/*
 * hwicap_writer.h — THE HWICAP writer. One API, both write-path protocols.
 *
 * WHY THIS FILE EXISTS.
 * The fabric has exactly one axi_hwicap, and it has exactly one write-path
 * protocol, chosen by the BD's C_MODE parameter. Until this file, the firmware
 * had TWO writers for it:
 *
 *   - firmware/coordinator/swap_fsm.c   hwicap_lite_write / hwicap_fifo_drain /
 *                                       hwicap_fifo_write_words / icap_batch_flush
 *   - firmware/overlay_store/overlay_store.c
 *                                       hwicap_lite_write / hwicap_write_words
 *
 * Both carried their OWN copy of the LITE-vs-FIFO distinction, each behind its
 * own `#if MPS3_HWICAP_FIFO`, each with its own poll ceiling, each documented as
 * "deliberately duplicated ... rather than reaching into <the other file>'s
 * statics across module boundaries". That duplication is the shape that shipped a
 * shell which pinged, reported the right static_id, passed every acceptance gate
 * and could not load a single RM (LITE writers against a FIFO axi_hwicap; see the
 * HWICAP_FIFO block in firmware/platform/Makefile, fixed d28c292) — and it had
 * already produced a second, quieter defect: swap_fsm.c's copy counted the bytes
 * it pushed onto the diag mailbox, overlay_store.c's copy did not, so every
 * QSPI-sourced or boot-load stream reported `icap_bytes` = 0 against diag.h's
 * documented "total bytes written to HWICAP.WF" (pyverify client.py reads
 * icap_bytes == got as "the ICAP is keeping up"). Two writers cannot both be
 * the one that matches the fabric.
 *
 * MODE SELECTION IS UNCHANGED. MPS3_HWICAP_FIFO (platform_regs.h default 0 =
 * LITE; firmware/platform/Makefile HWICAP_FIFO=1, part of PRODUCT=1) still picks
 * the protocol, at exactly the same compile-time seam. What changed is that it is
 * now read in ONE translation unit, so the two halves of the firmware cannot
 * disagree about which one is built.
 *
 *   LITE (MPS3_HWICAP_FIFO=0, BD axi_hwicap C_MODE {1}): no write FIFO. One
 *     StartConfig per word — push to WF, kick CR=WRITE, poll CR until the WRITE
 *     bit self-clears. (BSP hwicap_v11_6/src/xhwicap.c MODE==1 path.)
 *   FIFO (MPS3_HWICAP_FIFO=1, BD axi_hwicap C_MODE {0} + C_WRITE_FIFO_DEPTH):
 *     poll WFV, push up to WFV words into WF, kick ONE StartConfig, poll CR,
 *     refill. (The BSP's `#else` branch.)
 *
 * Every entry point below is BOUNDED and FAIL-CLOSED: a poll that never
 * completes returns an error rather than spinning forever or — the older bug —
 * spinning to the ceiling and carrying on as if the word had been written.
 *
 * Word packing is NOT duplicated here either: it goes through the one shared
 * mps3_hwicap_pack_word() primitive in platform_regs.h (MSB-first on target per
 * I18), which firmware/test/test_hwicap_byte_lane.c pins.
 */
#ifndef MPS3_HWICAP_WRITER_H
#define MPS3_HWICAP_WRITER_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Return codes. finish() distinguishes an EOS timeout from every other failure
 * because the caller records the two differently on the diag mailbox (swap_fsm.h
 * MPS3_ICAP_EOS_*): a flush failure means "the bytes never got in", an EOS
 * timeout means "the bytes got in and the ICAP never reached end-of-sequence". */
#define MPS3_HWICAP_OK       0
#define MPS3_HWICAP_ERR      (-1)
#define MPS3_HWICAP_ERR_EOS  (-2)

/* Bounded poll ceiling for every HWICAP spin (CR self-clear, WFV vacancy, the
 * post-DESYNC EOS wait). Previously TWO constants with the same value and
 * different names — swap_fsm.c's MPS3_HWICAP_DONE_POLL_MAX and
 * overlay_store.c's OVL_HWICAP_DONE_POLL_MAX. A bring-up tuning knob, not a
 * contract value: a healthy write clears in ~0 iterations, so this only ever
 * converts "never" into "failed". */
#ifndef MPS3_HWICAP_DONE_POLL_MAX
#define MPS3_HWICAP_DONE_POLL_MAX 1000000u
#endif

/* Words buffered before an automatic drain in FIFO mode, and the size of the
 * on-stack packing window mps3_hwicap_write_words() uses. Sized to the write
 * FIFO so a full batch is one StartConfig. LITE mode compiles no accumulator at
 * all: each word self-drains through its own StartConfig. */
#ifndef MPS3_HWICAP_BATCH_WORDS
#define MPS3_HWICAP_BATCH_WORDS 256u
#endif

/* 1 if this image was built for the FIFO write path, 0 for LITE. Compile-time
 * constant; exposed as a function so a test binary (and coordinator.c's feature
 * word) can assert which protocol it actually linked rather than re-deriving the
 * macro and getting it wrong. */
int mps3_hwicap_is_fifo(void);

/* ---- transaction: begin -> write/write_words -> flush -> finish ------------ */

/* Reset the byte packer and (FIFO) the batch accumulator. Call before a stream;
 * anything left over from a torn previous stream is discarded, NOT written. */
void mps3_hwicap_begin(void);

/* Stream an arbitrary byte run. Bytes are assembled into 32-bit config words
 * MSB-first (the .bin's first byte is the word's MSB, I18) and a word may
 * STRADDLE calls — this is the path config_agent's <=512-byte TCP chunks take,
 * so the partial word is carried in writer state across calls. Returns
 * MPS3_HWICAP_OK, or MPS3_HWICAP_ERR if a write stalled (fail closed: the caller
 * drops the session and leaves the RP parked). */
int mps3_hwicap_write(const void *buf, uint32_t len);

/* Write `nwords` whole words packed from `bytes`. SYNCHRONOUS: nothing is left
 * buffered when it returns, so a caller that advances a "words streamed" counter
 * on success is telling the truth in both modes. `nwritten` (optional) receives
 * the number of words CONFIRMED into the ICAP, which on failure is how many made
 * it before the stall — swap_fsm's chunk stepper needs that to keep s_words_done
 * honest. Returns MPS3_HWICAP_OK or MPS3_HWICAP_ERR. */
int mps3_hwicap_write_words(const uint8_t *bytes, uint32_t nwords, uint32_t *nwritten);

/* Drain whatever mps3_hwicap_write() buffered. No-op in LITE mode (every word
 * already self-drained). MPS3_HWICAP_ERR if the drain stalled. */
int mps3_hwicap_flush(void);

/* End of stream: reject a non-word-aligned tail, flush, then wait (bounded) for
 * HWICAP_SR_EOS. Returns MPS3_HWICAP_OK, MPS3_HWICAP_ERR (unaligned tail or a
 * stalled flush — the bytes never got in), or MPS3_HWICAP_ERR_EOS (the bytes
 * went in, end-of-sequence never asserted within the bound). */
int mps3_hwicap_finish(void);

/* Torn/rejected mid-stream: drop the partial word and any buffered words. Words
 * already CONFIRMED into the ICAP are in the fabric and cannot be recalled —
 * the caller's job is to leave the RP parked. */
void mps3_hwicap_abort(void);

/* How many words the ICAP is ready to take in THIS superloop poll, given the
 * caller's own per-poll cap. This is the writer's "ready" query: both streaming
 * callers need a budget rather than a bool.
 *   FIFO: min(cap, HWICAP_WFV) — 0 means "no room this poll, come back".
 *   LITE: cap. There is no write FIFO to pace against; WFV on a LITE core is not
 *     a vacancy at all, and pacing a QSPI->ICAP stream by it (which swap_fsm.c's
 *     QSPI branches did, while its RAM branch did not) throttles a LITE build to
 *     WFV words per poll for no reason. The per-poll bound in LITE mode is
 *     purely the caller's cap, which is what the RAM path always used. */
uint32_t mps3_hwicap_pace_words(uint32_t cap);

/* ---- conveniences ---------------------------------------------------------- */

/* Stream a whole in-memory bitstream (len must be a multiple of 4). */
int mps3_hwicap_stream_mem(const void *data, uint32_t len);

/* Reader for mps3_hwicap_stream_region(): fill `buf` with `len` bytes from
 * absolute offset `off`; return 0 on success. Keeps the flash leaves private to
 * overlay_store.c — the writer never learns what a QSPI core is. */
typedef int (*mps3_hwicap_read_fn)(uint32_t off, uint8_t *buf, uint32_t len, void *ctx);

/* Stream `len` bytes read from `off` through the reader, one `scratch`-sized
 * chunk at a time (scratch is the caller's buffer so this file adds no static or
 * stack footprint of its own). len must be a multiple of 4; scratch_len must be
 * a non-zero multiple of 4. */
int mps3_hwicap_stream_region(mps3_hwicap_read_fn rd, void *ctx,
                              uint32_t off, uint32_t len,
                              uint8_t *scratch, uint32_t scratch_len);

/* ---- diagnostics ----------------------------------------------------------- */

/* Free-running total of bytes CONFIRMED into HWICAP.WF by every writer entry
 * point — this is diag.h's `icap_bytes` ("total bytes written to HWICAP.WF"),
 * and it is now actually total: RAM clearing, QSPI-resident clearing/partial,
 * stream-direct partial and the boot-time default load all land here. Monotonic;
 * never reset per swap (a post-mortem JTAG read wants the running figure).
 * mps3_hwicap_stats_reset() exists for test setup, not for the target. */
uint32_t mps3_hwicap_bytes(void);

/* Raw last NON-ZERO HWICAP_SR observed by the EOS wait (I18(3) capture; an
 * all-zero read tells you nothing, so it is not recorded). */
uint32_t mps3_hwicap_sr_last(void);

void mps3_hwicap_stats_reset(void);

#ifdef __cplusplus
}
#endif

#endif /* MPS3_HWICAP_WRITER_H */
