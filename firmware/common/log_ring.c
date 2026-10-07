/*
 * log_ring.c — see log_ring.h. The console tee behind the v0.11 `log` verb.
 */
#include <stddef.h>

#include "log_ring.h"

#define MASK (MPS3_LOG_RING_BYTES - 1u)

static uint8_t  s_buf[MPS3_LOG_RING_BYTES];
static uint32_t s_head;   /* stream offset one past the newest byte */

void mps3_log_reset(void)
{
    s_head = 0u;
}

void mps3_log_putc(char c)
{
    s_buf[s_head & MASK] = (uint8_t)c;
    s_head++;
}

void mps3_log_write(const char *s, uint32_t n)
{
    uint32_t i;
    if (s == NULL) {
        return;
    }
    for (i = 0u; i < n; i++) {
        mps3_log_putc(s[i]);
    }
}

uint32_t mps3_log_head(void)
{
    return s_head;
}

uint32_t mps3_log_tail(void)
{
    return (s_head > MPS3_LOG_RING_BYTES) ? (s_head - MPS3_LOG_RING_BYTES) : 0u;
}

uint32_t mps3_log_dropped(void)
{
    /* The stream starts at offset 0, so everything before the tail was written
     * and then overwritten -- lost to overrun. */
    return mps3_log_tail();
}

uint32_t mps3_log_read(uint32_t want_off, uint8_t *dst, uint32_t max,
                       uint32_t *got_off, uint32_t *n, int *more)
{
    const uint32_t head = s_head;
    const uint32_t tail = mps3_log_tail();
    uint32_t off = want_off;
    uint32_t avail, take, i;

    /* Older than what is retained: start at the oldest byte we still have. */
    if (off < tail) {
        off = tail;
    }
    /* At/after the head (or a client offset from a PREVIOUS boot that is now
     * ahead of this one): nothing to read. Report the head so the client can
     * resynchronise without a second round-trip. */
    if (off >= head) {
        if (got_off) *got_off = head;
        if (n)       *n = 0u;
        if (more)    *more = 0;
        return 0u;
    }

    avail = head - off;
    take  = (avail < max) ? avail : max;
    if (dst == NULL) {
        take = 0u;
    }
    for (i = 0u; i < take; i++) {
        dst[i] = s_buf[(off + i) & MASK];
    }
    if (got_off) *got_off = off;
    if (n)       *n = take;
    if (more)    *more = (avail > take) ? 1 : 0;
    return take;
}
