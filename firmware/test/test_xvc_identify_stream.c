/*
 * test_xvc_identify_stream.c — replay the VERBATIM byte stream that the real
 * Synopsys Identify debugger sent, through the real xvc_server.c protocol
 * engine. TWO binaries from this one source, because the behaviour under test
 * is compile-time (same pattern as test_xvc_server_swdbb / _latesample):
 *
 *   test_xvc_identify_stream         positive: at the shipped
 *                                    MPS3_XVC_ACCEPT_RATIO the whole stream is
 *                                    served, including the 2053-bit shift.
 *   test_xvc_identify_stream_ratio1  NEGATIVE CONTROL: -DMPS3_XVC_ACCEPT_RATIO=1
 *                                    re-couples the accept ceiling to the
 *                                    advertised size, i.e. restores the defect,
 *                                    and asserts the exact damage — the client
 *                                    is dropped at the 2053-bit shift after
 *                                    exactly 4 commands. If a future tidy-up
 *                                    re-couples those two numbers, THIS binary
 *                                    starts passing and the positive one fails.
 *
 * ===========================================================================
 * Provenance of the stream — why it is a recording and not a construction
 * ===========================================================================
 * Captured off the HOST server (host/socket_harness/xvc_server.py, run with
 * `--fake-tap --trace-file ... --trace-raw`) while the real
 * identify_debugger_shell T-2022.09-SP2 ran `com check` against it. 567 bytes,
 * sha256 5a7905106e881e4c50384520926f4e7038c0fe0ad4dc6ec158b61985a5f8fd60.
 *
 * The same bytes are embedded in host/socket_harness/tests/test_xvc_server.py
 * as IDENTIFY_COM_CHECK_RX, and a test THERE
 * (test_the_firmware_test_pins_the_same_captured_bytes) parses this array back
 * out of this file and asserts the two agree — so the host and firmware suites
 * cannot silently drift onto different notions of what the client does.
 *
 * A hand-written frame would only pin somebody's READING of the XVC spec, and
 * the reading is exactly what was wrong: the server bounded shift:'s num_bits
 * by the value it advertised in getinfo:, but a client sizes the shift PAYLOAD
 * against that number and then adds TAP state-navigation bits on top. So
 * num_bits arrives 5 over. Identify's own report of this is only
 * "Couldn't shift do data from xvcServer".
 *
 * Stream contents: getinfo: / shift 5 / shift 6 / settck:100 / shift 2053.
 *
 * Links: xvc_server.c, common/net_if.c, mock_regs.c, fake_net_if.c.
 * Built at the DEFAULT (DBGBR) target: the num_bits bound and the command
 * accumulator are target-INDEPENDENT (they live in try_dispatch and
 * xvc_server_do_shift's shared validation), so this covers the shipped path and
 * the Identify/SWDBB path at once. test_xvc_server_swdbb.c separately proves
 * the SWDBB bit-banger itself handles an over-advertised shift.
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "../xvc_server/xvc_server.h"
#include "../coordinator/coordinator.h"
#include "../common/platform_regs.h"
#include "../common/net_proto.h"
#include "mock_regs.h"
#include "fake_net_if.h"

mps3_shell_state_t g_shell_state;

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

/* ---- the recording ---------------------------------------------------------- */
static const uint8_t s_identify_rx[567] = {
    0x67, 0x65, 0x74, 0x69, 0x6E, 0x66, 0x6F, 0x3A, 0x73, 0x68, 0x69, 0x66,
    0x74, 0x3A, 0x05, 0x00, 0x00, 0x00, 0x1F, 0x00, 0x73, 0x68, 0x69, 0x66,
    0x74, 0x3A, 0x06, 0x00, 0x00, 0x00, 0x1F, 0x00, 0x73, 0x65, 0x74, 0x74,
    0x63, 0x6B, 0x3A, 0x64, 0x00, 0x00, 0x00, 0x73, 0x68, 0x69, 0x66, 0x74,
    0x3A, 0x05, 0x08, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x08, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0xF0, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0xFF, 0x0F,
};
/* sha256 5a7905106e881e4c50384520926f4e7038c0fe0ad4dc6ec158b61985a5f8fd60 */

/* Byte offsets inside the recording, asserted below so a mangled fixture is
 * caught as a fixture bug and not mistaken for a server bug. */
#define IDENTIFY_RX_LEN        567u
#define BIG_SHIFT_OFFSET        43u   /* the "shift:" that broke us */
#define BIG_SHIFT_BITS        2053u
#define BIG_SHIFT_VEC_BYTES    257u   /* ceil(2053/8) */
#define BIG_SHIFT_CMD_BYTES    524u   /* 6 + 4 + 2*257 */

/* Replies the stream must draw out, in order:
 *   getinfo: -> "xvcServer_v1.0:2048\n"  (20)
 *   shift 5  -> 1 TDO byte
 *   shift 6  -> 1 TDO byte
 *   settck:  -> 4-byte echo
 *   shift 2053 -> 257 TDO bytes
 */
#define REPLY_BYTES_ALL  (20u + 1u + 1u + 4u + BIG_SHIFT_VEC_BYTES)
#define REPLY_BYTES_PRE  (20u + 1u + 1u + 4u)   /* before the big shift */

/* ---- behavioral DBGBR fake --------------------------------------------------
 * Minimal: TDO = TMS ^ TDI masked to LENGTH, and CTRL.GO self-clears on the
 * poll after it is written. Enough for a shift to COMPLETE, which is all this
 * test needs — bit-exact DBGBR conformance is test_xvc_server.c's job.
 * ------------------------------------------------------------------------- */
typedef struct {
    uint32_t length, tms, tdi, tdo;
    int      kicks;
} fake_dbgbr_t;

static fake_dbgbr_t s_dbgbr;

static uint32_t bits_mask(uint32_t nbits)
{
    return (nbits >= 32u) ? 0xFFFFFFFFu : ((1u << nbits) - 1u);
}

static int dbgbr_hook(void *ctx, int is_write, uint32_t base, uint32_t off, uint32_t *val)
{
    fake_dbgbr_t *d = (fake_dbgbr_t *)ctx;
    (void)base;
    if (is_write) {
        switch (off) {
        case DBGBR_LENGTH: d->length = *val; return 1;
        case DBGBR_TMS:    d->tms    = *val; return 1;
        case DBGBR_TDI:    d->tdi    = *val; return 1;
        case DBGBR_CTRL:
            if (*val & DBGBR_CTRL_GO) {
                d->tdo = (d->tms ^ d->tdi) & bits_mask(d->length);
                d->kicks++;
            }
            return 1;
        default: return 0;
        }
    }
    switch (off) {
    case DBGBR_CTRL: *val = 0u; return 1;   /* GO already self-cleared */
    case DBGBR_TDO:  *val = d->tdo; return 1;
    default: return 0;
    }
}

/* ---- helpers ----------------------------------------------------------------- */

static void fresh(void)
{
    mock_regs_reset();
    fake_net_reset();
    memset(&g_shell_state, 0, sizeof(g_shell_state));
    memset(&s_dbgbr, 0, sizeof(s_dbgbr));
    mock_regs_set_hook(MPS3_DBGBR_BASE, dbgbr_hook, &s_dbgbr);
    xvc_server_init();
}

static void polls(int n)
{
    for (int i = 0; i < n; i++) {
        xvc_server_poll();
    }
}

/* Drain whatever the firmware has queued for `cli`, appending into `out`. */
static uint32_t drain(int cli, uint8_t *out, uint32_t cap, uint32_t have)
{
    for (;;) {
        int n = fake_net_recv(cli, out + have, (int)(cap - have));
        if (n <= 0) {
            return have;
        }
        have += (uint32_t)n;
    }
}

/* ---- fixture integrity ------------------------------------------------------- */

static void test_the_fixture_is_the_stream_identify_actually_sent(void)
{
    CHECK(sizeof(s_identify_rx) == IDENTIFY_RX_LEN);
    /* getinfo: first ... */
    CHECK(memcmp(s_identify_rx, "getinfo:", 8) == 0);
    /* ... and the command that broke us at a known offset, well formed: a
     * 2053-bit shift carrying two 257-byte vectors. Only 5 bits over the 2048
     * we advertise. */
    CHECK(memcmp(&s_identify_rx[BIG_SHIFT_OFFSET], "shift:", 6) == 0);
    uint32_t nb = (uint32_t)s_identify_rx[BIG_SHIFT_OFFSET + 6]
                | ((uint32_t)s_identify_rx[BIG_SHIFT_OFFSET + 7] << 8)
                | ((uint32_t)s_identify_rx[BIG_SHIFT_OFFSET + 8] << 16)
                | ((uint32_t)s_identify_rx[BIG_SHIFT_OFFSET + 9] << 24);
    CHECK(nb == BIG_SHIFT_BITS);
    CHECK(nb == MPS3_XVC_MAX_VECTOR_BITS + 5u);
    CHECK(IDENTIFY_RX_LEN - BIG_SHIFT_OFFSET == BIG_SHIFT_CMD_BYTES);
    CHECK((nb + 7u) / 8u == BIG_SHIFT_VEC_BYTES);
}

/* ---- the two numbers must not be the same number ----------------------------- */

static void test_accept_ceiling_and_advertised_size_are_distinct(void)
{
    /* getinfo: still advertises the firmware's historic number, so the host
     * server stays drop-in interchangeable behind the same client. */
    CHECK(MPS3_XVC_MAX_VECTOR_BITS == 2048u);
#if MPS3_XVC_ACCEPT_RATIO == 1u
    CHECK(MPS3_XVC_ACCEPT_VECTOR_BITS == MPS3_XVC_MAX_VECTOR_BITS); /* the defect */
#else
    CHECK(MPS3_XVC_ACCEPT_VECTOR_BITS > MPS3_XVC_MAX_VECTOR_BITS);
    CHECK(MPS3_XVC_ACCEPT_VECTOR_BITS >= BIG_SHIFT_BITS);
    /* The accumulator must hold a whole maximal ACCEPTED command, or a legal
     * shift stalls un-dispatchable forever. This is the second, independent
     * half of the fix. */
    CHECK(10u + 2u * MPS3_XVC_ACCEPT_VECTOR_BYTES >= BIG_SHIFT_CMD_BYTES);
#endif
}

/* ---- THE regression: replay the real bytes ------------------------------------ */

static void test_the_real_identify_stream_is_served_end_to_end(void)
{
    fresh();
    int cli = fake_net_connect(MPS3_PORT_XVC);
    polls(1);
    CHECK(fake_net_send(cli, s_identify_rx, (int)sizeof(s_identify_rx))
          == (int)sizeof(s_identify_rx));

    uint8_t rsp[REPLY_BYTES_ALL + 64];
    uint32_t got = 0;
    /* Generous poll budget: the stream is 567 B and XVC_BYTES_PER_POLL now
     * covers a maximal command, but the reply pacing is one command per pass. */
    for (int i = 0; i < 64; i++) {
        polls(1);
        got = drain(cli, rsp, sizeof(rsp), got);
    }

#if MPS3_XVC_ACCEPT_RATIO == 1u
    /* NEGATIVE CONTROL — the pre-fix rule. The first four commands are served
     * (which is exactly why Identify's log said "debug IP state... ok." before
     * failing), then the 2053-bit shift is rejected and the client dropped. */
    CHECK(got == REPLY_BYTES_PRE);
    CHECK(fake_net_fw_closed(cli));
    /* Only the two small shifts reached the bridge (one 32-bit chunk each);
     * the 2053-bit one was refused before any register access. */
    CHECK(s_dbgbr.kicks == 2);
#else
    CHECK(got == REPLY_BYTES_ALL);
    CHECK(!fake_net_fw_closed(cli));
    /* getinfo reply verbatim, then the big shift's TDO is a full 257 bytes --
     * ceil(num_bits/8), the XVC 1.0 rule, NOT truncated to the 256 we advertise. */
    CHECK(memcmp(rsp, "xvcServer_v1.0:2048\n", 20) == 0);
    CHECK(got - REPLY_BYTES_PRE == BIG_SHIFT_VEC_BYTES);
#endif
    fake_net_close(cli);
}

/* ---- the same stream, fragmented ---------------------------------------------- */

static void test_the_same_stream_fragmented_gives_the_identical_replies(void)
{
    fresh();
    int cli = fake_net_connect(MPS3_PORT_XVC);
    polls(1);

    uint8_t rsp[REPLY_BYTES_ALL + 64];
    uint32_t got = 0;
    /* 7 bytes at a time, so fragment boundaries land inside the shift header
     * AND inside both vectors -- the accumulate-and-reevaluate path. */
    for (uint32_t off = 0; off < sizeof(s_identify_rx); off += 7u) {
        uint32_t n = sizeof(s_identify_rx) - off;
        if (n > 7u) {
            n = 7u;
        }
        CHECK(fake_net_send(cli, &s_identify_rx[off], (int)n) == (int)n);
        polls(2);
        got = drain(cli, rsp, sizeof(rsp), got);
    }
    for (int i = 0; i < 32; i++) {
        polls(1);
        got = drain(cli, rsp, sizeof(rsp), got);
    }

#if MPS3_XVC_ACCEPT_RATIO == 1u
    CHECK(got == REPLY_BYTES_PRE);
    CHECK(fake_net_fw_closed(cli));
#else
    CHECK(got == REPLY_BYTES_ALL);
    CHECK(!fake_net_fw_closed(cli));
    CHECK(memcmp(rsp, "xvcServer_v1.0:2048\n", 20) == 0);
#endif
    fake_net_close(cli);
}

/* ---- leniency has a limit ------------------------------------------------------ */

static void test_above_the_accept_ceiling_still_fails_closed(void)
{
    fresh();
    int cli = fake_net_connect(MPS3_PORT_XVC);
    polls(1);
    uint8_t cmd[10];
    memcpy(cmd, "shift:", 6);
    uint32_t over = MPS3_XVC_ACCEPT_VECTOR_BITS + 1u;
    cmd[6] = (uint8_t)over;
    cmd[7] = (uint8_t)(over >> 8);
    cmd[8] = (uint8_t)(over >> 16);
    cmd[9] = (uint8_t)(over >> 24);
    CHECK(fake_net_send(cli, cmd, 10) == 10);
    polls(3);
    /* Dropped, and NOT truncated to a short TDO: a short reply would read to
     * the client as real captured data. */
    CHECK(fake_net_fw_closed(cli));
    CHECK(s_dbgbr.kicks == 0);
    fake_net_close(cli);
}

int main(void)
{
    test_the_fixture_is_the_stream_identify_actually_sent();
    test_accept_ceiling_and_advertised_size_are_distinct();
    test_the_real_identify_stream_is_served_end_to_end();
    test_the_same_stream_fragmented_gives_the_identical_replies();
    test_above_the_accept_ceiling_still_fails_closed();

    printf("%s: %d checks passed\n", XVC_IDENTIFY_TEST_NAME, s_checks);
    return 0;
}
