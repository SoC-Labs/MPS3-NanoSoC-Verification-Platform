/*
 * log_ring.h — the shell console's RAM tee (net-protocol.md v0.11 `log`).
 *
 * WHY THIS EXISTS. The shell prints its boot banner (and every later console
 * line) through xil_printf -> outbyte -> the UART-lite. At power-on the MCC
 * opens the serial path to the host roughly a second AFTER the firmware has
 * printed, so the banner is lost to any capture that was not already attached
 * (docs/evidence/2026-09-w2/p1_console_20260923.txt). This ring keeps the most
 * recent MPS3_LOG_RING_BYTES of console output in RAM so the 6900 `log` verb can
 * hand it back at any time, and the platform's outbyte() override tees every
 * byte into it.
 *
 * THE MODEL: a STREAM with absolute byte offsets, not a queue.
 *   - Every byte ever written has a stream offset: the first byte after boot is
 *     offset 0, the next 1, and so on (u32; the shell prints a few KiB per boot,
 *     so wrap is not a practical concern -- see mps3_log_read()).
 *   - The ring RETAINS the newest MPS3_LOG_RING_BYTES bytes. Writing into a full
 *     ring overwrites the OLDEST byte and counts it in `dropped` (a monotonic
 *     count of bytes lost to overrun since boot). A log reader wants the most
 *     recent state of the shell; the boot banner is protected separately, by the
 *     +5 s re-print in main.c, rather than by refusing new output.
 *   - Reading is NON-destructive and stateless on the shell: the client says
 *     which offset it wants, the shell answers with up to one chunk from there.
 *     Two readers (a GUI Log tab and a `pyverify log`) therefore never steal
 *     bytes from one another -- the failure a destructive queue would have.
 *
 * Portable: no register, no platform header. Host-tested by
 * firmware/test/test_log_ring.c.
 */
#ifndef MPS3_LOG_RING_H
#define MPS3_LOG_RING_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Ring capacity. MUST be a power of two (the index is a mask). 4 KiB holds the
 * whole boot banner plus the touch bring-up lines several times over. */
#ifndef MPS3_LOG_RING_BYTES
#define MPS3_LOG_RING_BYTES 4096u
#endif

#if (MPS3_LOG_RING_BYTES & (MPS3_LOG_RING_BYTES - 1u)) != 0u
#error "MPS3_LOG_RING_BYTES must be a power of two"
#endif

/* Largest chunk one `log` reply carries. SAME as MPS3_DUTRX_CHUNK_MAX (256), for
 * the same reason: 512 hex chars keep the reply far inside MPS3_CTRL_RESP_MAX and
 * leave `diag` the binding case. */
#define MPS3_LOG_CHUNK_MAX 256u

/* Forget everything (tests; the target relies on .bss zeroing). */
void mps3_log_reset(void);

/* Append one byte / n bytes. Never blocks, never fails: a full ring overwrites
 * its oldest byte and counts it in mps3_log_dropped(). */
void mps3_log_putc(char c);
void mps3_log_write(const char *s, uint32_t n);

/* Stream offset one past the newest byte (= bytes written since boot). */
uint32_t mps3_log_head(void);
/* Stream offset of the OLDEST byte still retained (= head - retained). */
uint32_t mps3_log_tail(void);
/* Bytes overwritten before anyone could have read them (== tail, since the
 * stream starts at 0; kept as its own accessor because that is what a reader
 * means by it and the wire key is named for it). */
uint32_t mps3_log_dropped(void);

/* Read up to `max` bytes starting at stream offset `want_off`.
 *   - want_off older than the tail: the read starts AT THE TAIL. The caller sees
 *     *got_off > want_off and knows (got_off - want_off) bytes were lost.
 *   - want_off at or past the head: nothing to read; *n = 0 and *got_off = head.
 * Returns the number of bytes copied (== *n). *more is 1 when bytes remain after
 * this chunk. Never reads past the ring; never allocates. */
uint32_t mps3_log_read(uint32_t want_off, uint8_t *dst, uint32_t max,
                       uint32_t *got_off, uint32_t *n, int *more);

#ifdef __cplusplus
}
#endif

#endif /* MPS3_LOG_RING_H */
