/*
 * test_log_ring.c -- the console log ring behind the v0.11 `log` verb
 * (firmware/common/log_ring.c) plus the codec's `log` reply at full width.
 *
 * What must hold, and why each matters on the board:
 *   - the ring keeps the NEWEST MPS3_LOG_RING_BYTES; overrun overwrites the
 *     oldest and COUNTS it (`dropped`), so a reader can tell a complete log from
 *     one with a hole in it;
 *   - reads are non-destructive and addressed by stream offset, so two readers
 *     never steal bytes from each other, and a stale offset skips forward to the
 *     oldest retained byte instead of reading garbage;
 *   - a chunk never exceeds MPS3_LOG_CHUNK_MAX and the encoded reply fits
 *     MPS3_CTRL_RESP_MAX with `diag` still the binding case.
 * Fails on the pre-v0.11 tree: none of it existed.
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../common/log_ring.h"
#include "../common/net_proto.h"

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

static void test_empty(void)
{
    uint8_t buf[16];
    uint32_t off = 99, n = 99;
    int more = 7;
    mps3_log_reset();
    CHECK(mps3_log_head() == 0u && mps3_log_tail() == 0u && mps3_log_dropped() == 0u);
    CHECK(mps3_log_read(0u, buf, sizeof buf, &off, &n, &more) == 0u);
    CHECK(off == 0u && n == 0u && more == 0);
}

static void test_basic_read_and_offsets(void)
{
    uint8_t buf[64];
    uint32_t off, n;
    int more;
    mps3_log_reset();
    mps3_log_write("abcdefghij", 10u);
    CHECK(mps3_log_head() == 10u);
    CHECK(mps3_log_read(0u, buf, 4u, &off, &n, &more) == 4u);
    CHECK(off == 0u && n == 4u && more == 1 && memcmp(buf, "abcd", 4) == 0);
    CHECK(mps3_log_read(4u, buf, 64u, &off, &n, &more) == 6u);
    CHECK(off == 4u && more == 0 && memcmp(buf, "efghij", 6) == 0);
    /* Non-destructive: the same read again gives the same bytes. */
    CHECK(mps3_log_read(4u, buf, 64u, &off, &n, &more) == 6u);
    CHECK(memcmp(buf, "efghij", 6) == 0);
    /* Past the head (e.g. an offset from a previous, longer boot): nothing,
     * and the head is reported so the client can resync in one step. */
    CHECK(mps3_log_read(500u, buf, 64u, &off, &n, &more) == 0u);
    CHECK(off == 10u && n == 0u && more == 0);
}

static void test_wraparound_and_dropped(void)
{
    uint8_t buf[MPS3_LOG_CHUNK_MAX];
    uint32_t off, n, i;
    int more;
    mps3_log_reset();
    /* Fill with a position-dependent pattern, 1.5 rings plus change. */
    const uint32_t total = MPS3_LOG_RING_BYTES + MPS3_LOG_RING_BYTES / 2u + 17u;
    for (i = 0u; i < total; i++) {
        mps3_log_putc((char)(i & 0xFFu));
    }
    CHECK(mps3_log_head() == total);
    CHECK(mps3_log_dropped() == total - MPS3_LOG_RING_BYTES);
    CHECK(mps3_log_tail() == total - MPS3_LOG_RING_BYTES);

    /* A reader that asks from 0 is moved to the tail; gap = dropped. */
    CHECK(mps3_log_read(0u, buf, sizeof buf, &off, &n, &more) == MPS3_LOG_CHUNK_MAX);
    CHECK(off == mps3_log_tail() && more == 1);
    for (i = 0u; i < n; i++) {
        CHECK(buf[i] == (uint8_t)((off + i) & 0xFFu));   /* right bytes, in order */
    }

    /* A read that straddles the physical end of the buffer (the wrap). */
    {
        uint32_t phys_end = (mps3_log_head() / MPS3_LOG_RING_BYTES) * MPS3_LOG_RING_BYTES;
        uint32_t start = phys_end - 10u;                 /* 10 before, 20 after */
        CHECK(start >= mps3_log_tail());
        CHECK(mps3_log_read(start, buf, 30u, &off, &n, &more) == 30u);
        CHECK(off == start);
        for (i = 0u; i < 30u; i++) {
            CHECK(buf[i] == (uint8_t)((start + i) & 0xFFu));
        }
    }

    /* Walking the whole retained window in chunks returns exactly RING bytes
     * and ends with more == 0 at the head. */
    {
        uint32_t cur = 0u, got = 0u;
        do {
            CHECK(mps3_log_read(cur, buf, sizeof buf, &off, &n, &more) == n);
            if (got == 0u) CHECK(off == mps3_log_tail());
            else           CHECK(off == cur);
            got += n;
            cur = off + n;
        } while (more);
        CHECK(got == MPS3_LOG_RING_BYTES);
        CHECK(cur == mps3_log_head());
    }

    /* Exactly one ring: nothing dropped yet; one more byte drops exactly one. */
    mps3_log_reset();
    for (i = 0u; i < MPS3_LOG_RING_BYTES; i++) mps3_log_putc('q');
    CHECK(mps3_log_dropped() == 0u && mps3_log_tail() == 0u);
    mps3_log_putc('r');
    CHECK(mps3_log_dropped() == 1u && mps3_log_tail() == 1u);
}

static void test_log_reply_fits(void)
{
    /* The widest log line through the REAL encoder: a full chunk, every
     * counter at max width. It must fit RESP_MAX and stay under diag's worst
     * (1084 with the NUL since diag v9). */
    mps3_ctrl_response_t r;
    char out[MPS3_CTRL_RESP_MAX];
    memset(&r, 0, sizeof r);
    r.op = MPS3_OP_LOG;
    r.ok = 1;
    r.log_off = 0xFFFFFFFFu;
    r.log_n = MPS3_LOG_CHUNK_MAX;
    r.log_more = 0;
    r.log_dropped = 0xFFFFFFFFu;
    memset(r.dutrx_data, 0xAB, sizeof r.dutrx_data);
    int n = mps3_ctrl_encode_response(&r, out, sizeof out);
    CHECK(n > 0 && n < 1084 && n < MPS3_CTRL_RESP_MAX);
    CHECK(strstr(out, "\"data\":\"abab") != NULL);
    printf("  log worst line = %d B (diag worst 1084, RESP_MAX %d)\n", n, MPS3_CTRL_RESP_MAX);
    /* A chunk longer than the contract is a caller bug: fail closed. */
    r.log_n = MPS3_LOG_CHUNK_MAX + 1u;
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof out) < 0);
    /* A short buffer fails closed, never truncated. */
    r.log_n = MPS3_LOG_CHUNK_MAX;
    CHECK(mps3_ctrl_encode_response(&r, out, n) < 0 && out[0] == '\0');
}

int main(void)
{
    test_empty();
    test_basic_read_and_offsets();
    test_wraparound_and_dropped();
    test_log_reply_fits();
    printf("test_log_ring: ALL PASS (%d checks)\n", s_checks);
    return 0;
}
