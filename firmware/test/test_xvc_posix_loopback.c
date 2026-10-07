/*
 * test_xvc_posix_loopback.c — the REAL firmware XVC engine, on a REAL TCP
 * socket, driven by a REAL socket client. Board-free, tool-free, in $(TESTS).
 *
 * ===========================================================================
 * What this adds over test_xvc_server_swdbb.c
 * ===========================================================================
 * That suite links fake_net_if.c: bytes are handed to the engine through
 * in-memory rings, which proves the PROTOCOL but says nothing about the
 * transport. This one links posix_net_if.c, so the exact same firmware source
 * (firmware/xvc_server/xvc_server.c, -DMPS3_XVC_TARGET_SWDBB) is driven through
 * a kernel socket: real accept(), real segmentation, real EAGAIN, real FIN. It
 * is the self-checking counterpart of bin/xvc_fw_daemon, which is that same
 * stack with the socket client replaced by the Synopsys Identify debugger.
 *
 * Everything below runs SINGLE-THREADED: the client and xvc_server_poll() are
 * interleaved in one process (every seam call is non-blocking, so this is
 * exactly the superloop shape the firmware runs on target). No fork, no thread,
 * no sleep-and-hope — every wait is a bounded pump loop.
 *
 * The listener binds an EPHEMERAL port (posix_net_map_port(MPS3_PORT_XVC, 0)):
 * xvc_server_init() is not edited to be testable, and two concurrent runs of the
 * suite can never collide on 2542.
 *
 * ===========================================================================
 * TWO BINARIES — the same negative control as the fake-net suite
 * ===========================================================================
 *   test_xvc_posix_loopback             LATE=0  positive: IDCODE 0x6BA00477
 *                                       arrives back over the socket
 *   test_xvc_posix_loopback_latesample  LATE=1  NEGATIVE CONTROL: the SAME
 *                                       source built with
 *                                       -DMPS3_XVC_SWDBB_SAMPLE_LATE=1 moves the
 *                                       SAMPLE read after the rising edge and
 *                                       asserts the damage (IDCODE >> 1). If the
 *                                       shipped sampling phase were inverted,
 *                                       THIS binary would pass and the positive
 *                                       one would fail.
 *
 * Without the control, "the IDCODE came back over a socket" would be a gate that
 * mostly proves the socket works. With it, the socket path is proven to be
 * carrying a bit-exact trace.
 *
 * g_shell_state defined here (xvc_server.c reads xvc_gated only).
 */
#define _GNU_SOURCE

#include <assert.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#include "../xvc_server/xvc_server.h"
#include "../coordinator/coordinator.h"
#include "../common/platform_regs.h"
#include "../common/net_proto.h"
#include "mock_regs.h"
#include "posix_net_if.h"
#include "fake_jtag_tap.h"

#ifndef XVC_LOOPBACK_TEST_NAME
#define XVC_LOOPBACK_TEST_NAME "test_xvc_posix_loopback"
#endif

mps3_shell_state_t g_shell_state;

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

/* The DUT's real JTAG TAP id (host/openocd/nanosoc_mps3_jtag.cfg) — the same
 * constant the fake-net suite, the host-side suite and bin/xvc_fw_daemon use. */
#define IDCODE 0x6BA00477u

/* Bounded pump budget. One maximal accepted shift is ~24 k register accesses
 * against an in-memory model, so anything that has not completed in this many
 * poll passes is wedged, not slow — and the CHECK fails instead of the suite
 * hanging. */
#define PUMP_MAX 200000

static fake_swdbb_t s_swdbb;

/* ---- harness --------------------------------------------------------------- */

/* Bring up the whole stack on a fresh ephemeral port and return that port. */
static uint16_t fresh_with_idcode(uint32_t idcode)
{
    posix_net_reset();
    mock_regs_reset();
    memset(&g_shell_state, 0, sizeof(g_shell_state));
    memset(&s_swdbb, 0, sizeof(s_swdbb));
    fake_tap_reset(&s_swdbb.tap, idcode);
    mock_regs_set_hook(MPS3_SWDBB_BASE, fake_swdbb_hook, &s_swdbb);

    posix_net_map_port(MPS3_PORT_XVC, 0); /* kernel-chosen port */
    xvc_server_init();                    /* REAL firmware init: binds + parks */
    fake_swdbb_clear_counters(&s_swdbb);   /* ignore the park write */

    uint16_t p = posix_net_actual_port(MPS3_PORT_XVC);
    assert(p != 0);
    return p;
}

static uint16_t fresh(void) { return fresh_with_idcode(IDCODE); }

static int cli_connect(uint16_t port)
{
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    assert(fd >= 0);
    struct sockaddr_in sa;
    memset(&sa, 0, sizeof(sa));
    sa.sin_family      = AF_INET;
    sa.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    sa.sin_port        = htons(port);
    if (connect(fd, (struct sockaddr *)&sa, sizeof(sa)) != 0) {
        close(fd);
        return -1;
    }
    return fd;
}

/* Push every byte at the firmware, polling the superloop as we go so the server
 * drains the socket while we are still filling it (which is what makes a
 * >socket-buffer command work at all). Returns bytes sent. */
static size_t cli_send_all(int fd, const void *buf, size_t len)
{
    const char *p = (const char *)buf;
    size_t done = 0;
    for (int i = 0; i < PUMP_MAX && done < len; i++) {
        ssize_t n = send(fd, p + done, len - done, MSG_DONTWAIT | MSG_NOSIGNAL);
        if (n > 0) {
            done += (size_t)n;
        }
        xvc_server_poll();
    }
    return done;
}

/* Read exactly `len` reply bytes, polling the superloop between attempts.
 * Returns the count actually read (< len if the server closed first). */
static size_t cli_recv_exact(int fd, void *buf, size_t len)
{
    char *p = (char *)buf;
    size_t done = 0;
    for (int i = 0; i < PUMP_MAX && done < len; i++) {
        xvc_server_poll();
        ssize_t n = recv(fd, p + done, len - done, MSG_DONTWAIT);
        if (n > 0) {
            done += (size_t)n;
        } else if (n == 0) {
            break; /* server closed */
        } else if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
            break;
        }
    }
    return done;
}

/* 1 once the FIRMWARE has closed its end (recv on the client returns 0). */
static int cli_saw_fin(int fd)
{
    char c;
    for (int i = 0; i < 2000; i++) {
        xvc_server_poll();
        ssize_t n = recv(fd, &c, 1, MSG_DONTWAIT);
        if (n == 0) {
            return 1;
        }
        if (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
            return 1; /* ECONNRESET also means "gone" */
        }
    }
    return 0;
}

/* Build one complete `shift:` command. */
static size_t build_shift(uint8_t *out, uint32_t num_bits,
                          const uint8_t *tms, const uint8_t *tdi)
{
    uint32_t nb = (num_bits + 7u) / 8u;
    memcpy(out, "shift:", 6);
    fake_xvc_put_le32(out + 6, num_bits);
    memcpy(out + 10, tms, nb);
    memcpy(out + 10 + nb, tdi, nb);
    return 10u + 2u * nb;
}

/* THE per-binary expectation, identical in shape to the fake-net suite's. */
static void check_recovered_idcode(uint32_t got, uint32_t want)
{
#if MPS3_XVC_SWDBB_SAMPLE_LATE
    /* NEGATIVE CONTROL: sampling AFTER the rising edge reads the bit the TAP has
     * ALREADY shifted, so every bit is one position late. Only the top bit is
     * lost outright, so the low 31 bits are exactly IDCODE >> 1 — an
     * off-by-one-bit trace with no error reported anywhere. */
    CHECK(got != want);
    CHECK((got & 0x7FFFFFFFu) == ((want >> 1) & 0x7FFFFFFFu));
#else
    CHECK(got == want);
#endif
}

/* ---- tests ----------------------------------------------------------------- */

static void test_this_binary_is_the_swdbb_target(void)
{
    /* Otherwise everything below would be quietly exercising the Debug Bridge. */
    CHECK(MPS3_XVC_TARGET_IS_SWDBB == 1);
}

/* The shared vector builder must serialise to the SAME bytes the HOST server
 * produced. These constants came out of host/socket_harness/xvc_server.py (see
 * test_xvc_server_swdbb.c's cross-language golden for the exact command), so
 * this pins fake_jtag_tap.c's builder to the host convention independently of
 * the fake-net suite's own private restatement of it. */
static void test_the_shared_vectors_match_the_host_server(void)
{
    static const uint8_t host_tms[FAKE_TAP_IDCODE_SEQ_BYTES] =
        { 0x5F, 0x00, 0x00, 0x00, 0x00, 0x01 };
    static const uint8_t host_tdi[FAKE_TAP_IDCODE_SEQ_BYTES] =
        { 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 };

    uint8_t tms[FAKE_TAP_IDCODE_SEQ_BYTES], tdi[FAKE_TAP_IDCODE_SEQ_BYTES];
    fake_tap_idcode_read_sequence(tms, tdi);
    CHECK(memcmp(tms, host_tms, sizeof(tms)) == 0);
    CHECK(memcmp(tdi, host_tdi, sizeof(tdi)) == 0);
    CHECK(FAKE_TAP_IDCODE_SEQ_BITS == 41u);
    CHECK(FAKE_TAP_IDCODE_FIRST_BIT == 9u);
}

/* The listener really is a kernel socket on a real port, and it is NOT 2542 —
 * i.e. the firmware's hard-coded MPS3_PORT_XVC was redirected by the backend's
 * port map, not by editing the firmware. */
static void test_the_listener_is_a_real_kernel_socket(void)
{
    uint16_t port = fresh();
    CHECK(port != 0);
    CHECK(port != MPS3_PORT_XVC);

    int cli = cli_connect(port);
    CHECK(cli >= 0);
    xvc_server_poll();
    /* Connecting alone must not move a pin. */
    CHECK(s_swdbb.ops == 0);
    close(cli);
    posix_net_reset();
}

static void test_getinfo_over_a_real_socket(void)
{
    uint16_t port = fresh();
    int cli = cli_connect(port);
    CHECK(cli >= 0);

    CHECK(cli_send_all(cli, "getinfo:", 8) == 8);

    char expect[64];
    int elen = snprintf(expect, sizeof(expect), "xvcServer_v1.0:%u\n",
                        (unsigned)MPS3_XVC_MAX_VECTOR_BITS);
    char rsp[64];
    memset(rsp, 0, sizeof(rsp));
    CHECK(cli_recv_exact(cli, rsp, (size_t)elen) == (size_t)elen);
    CHECK(memcmp(rsp, expect, (size_t)elen) == 0);
    /* The advertised value is what the client will chunk against — it must be
     * the 2048 the host server advertises, not the accept ceiling. */
    CHECK(MPS3_XVC_MAX_VECTOR_BITS == 2048u);
    CHECK(s_swdbb.ops == 0); /* getinfo touches no hardware */

    close(cli);
    posix_net_reset();
}

static void test_settck_echo_over_a_real_socket(void)
{
    uint16_t port = fresh();
    int cli = cli_connect(port);
    CHECK(cli >= 0);

    /* 1 ms, the period host/identify/debug_session.tcl asks for. */
    uint8_t cmd[11];
    memcpy(cmd, "settck:", 7);
    fake_xvc_put_le32(cmd + 7, 1000000u);
    CHECK(cli_send_all(cli, cmd, sizeof(cmd)) == sizeof(cmd));

    uint8_t echo[4];
    CHECK(cli_recv_exact(cli, echo, sizeof(echo)) == 4);
    CHECK(memcmp(echo, cmd + 7, 4) == 0);
    CHECK(s_swdbb.ops == 0); /* advisory only: no divider, no pin */

    close(cli);
    posix_net_reset();
}

/* HEADLINE: a complete IEEE-1149.1 IDCODE read, carried by the real XVC wire
 * protocol over a real TCP socket, into the real bit-bang loop and back. */
static void test_idcode_read_over_a_real_socket(void)
{
    uint16_t port = fresh();
    int cli = cli_connect(port);
    CHECK(cli >= 0);

    uint8_t tms[FAKE_TAP_IDCODE_SEQ_BYTES], tdi[FAKE_TAP_IDCODE_SEQ_BYTES];
    fake_tap_idcode_read_sequence(tms, tdi);
    uint8_t cmd[10 + 2 * FAKE_TAP_IDCODE_SEQ_BYTES];
    size_t clen = build_shift(cmd, FAKE_TAP_IDCODE_SEQ_BITS, tms, tdi);
    CHECK(cli_send_all(cli, cmd, clen) == clen);

    uint8_t tdo[FAKE_TAP_IDCODE_SEQ_BYTES];
    memset(tdo, 0, sizeof(tdo));
    CHECK(cli_recv_exact(cli, tdo, sizeof(tdo)) == FAKE_TAP_IDCODE_SEQ_BYTES);
    check_recovered_idcode(fake_xvc_recover32(tdo, FAKE_TAP_IDCODE_FIRST_BIT), IDCODE);

    /* Exactly one TCK edge per shift bit, one SAMPLE read per bit, no access
     * outside the two offsets swd_bb.sv decodes, and the vector really did land
     * the TAP in Exit1-DR (its last bit carried TMS=1). */
    CHECK(s_swdbb.tap.rising_edges == FAKE_TAP_IDCODE_SEQ_BITS);
    CHECK(s_swdbb.sample_reads == (int)FAKE_TAP_IDCODE_SEQ_BITS);
    CHECK(s_swdbb.stray == 0);
    CHECK(s_swdbb.tap.state == FAKE_TAP_EXIT1_DR);

    /* The connection is still live and serves a SECOND scan — Identify does
     * hundreds per session, and each must re-enter TLR and re-read the DR. */
    CHECK(cli_send_all(cli, cmd, clen) == clen);
    memset(tdo, 0, sizeof(tdo));
    CHECK(cli_recv_exact(cli, tdo, sizeof(tdo)) == FAKE_TAP_IDCODE_SEQ_BYTES);
    check_recovered_idcode(fake_xvc_recover32(tdo, FAKE_TAP_IDCODE_FIRST_BIT), IDCODE);
    CHECK(s_swdbb.tap.rising_edges == 2u * FAKE_TAP_IDCODE_SEQ_BITS);

    close(cli);
    posix_net_reset();
}

static void test_a_different_idcode_comes_back_different(void)
{
    /* Guards against the model returning IDCODE because the test asked for it. */
    static const uint32_t values[3] = { 0x0BB11477u, 0xDEADBEEFu, 0x00000001u };
    for (int i = 0; i < 3; i++) {
        uint16_t port = fresh_with_idcode(values[i]);
        int cli = cli_connect(port);
        CHECK(cli >= 0);
        uint8_t tms[FAKE_TAP_IDCODE_SEQ_BYTES], tdi[FAKE_TAP_IDCODE_SEQ_BYTES];
        fake_tap_idcode_read_sequence(tms, tdi);
        uint8_t cmd[10 + 2 * FAKE_TAP_IDCODE_SEQ_BYTES];
        size_t clen = build_shift(cmd, FAKE_TAP_IDCODE_SEQ_BITS, tms, tdi);
        CHECK(cli_send_all(cli, cmd, clen) == clen);
        uint8_t tdo[FAKE_TAP_IDCODE_SEQ_BYTES];
        CHECK(cli_recv_exact(cli, tdo, sizeof(tdo)) == FAKE_TAP_IDCODE_SEQ_BYTES);
        check_recovered_idcode(fake_xvc_recover32(tdo, FAKE_TAP_IDCODE_FIRST_BIT),
                               values[i]);
        close(cli);
    }
    posix_net_reset();
}

/* THE size the real Synopsys Identify debugger actually asks for: 2053 bits,
 * five MORE than we advertise (xvc_server.h's three-point measurement). The
 * command is 524 bytes, which is larger than the pre-fix XVC_CMD_BUF_MAX of 522
 * — so this is also the regression on the command-accumulator sizing, now over a
 * real socket where the 524 bytes really do arrive in whatever segments the
 * kernel chooses rather than in one memcpy. */
static void test_identify_sized_2053_bit_shift_over_a_real_socket(void)
{
    uint16_t port = fresh();
    int cli = cli_connect(port);
    CHECK(cli >= 0);

    const uint32_t over = MPS3_XVC_MAX_VECTOR_BITS + 5u;
    CHECK(over == 2053u);
    const uint32_t vec_bytes = (over + 7u) / 8u;
    CHECK(vec_bytes == 257u);

    static uint8_t tms[MPS3_XVC_ACCEPT_VECTOR_BYTES];
    static uint8_t tdi[MPS3_XVC_ACCEPT_VECTOR_BYTES];
    memset(tms, 0, sizeof(tms));
    memset(tdi, 0, sizeof(tdi));
    /* The 9-edge navigation, then TMS=0 all the way so every remaining edge is a
     * real Shift-DR clock. */
    static const uint32_t nav[FAKE_TAP_IDCODE_FIRST_BIT] = { 1, 1, 1, 1, 1, 0, 1, 0, 0 };
    for (uint32_t i = 0; i < FAKE_TAP_IDCODE_FIRST_BIT; i++) {
        if (nav[i]) {
            tms[i >> 3] |= (uint8_t)(1u << (i & 7u));
        }
    }

    static uint8_t cmd[10 + 2 * MPS3_XVC_ACCEPT_VECTOR_BYTES];
    size_t clen = build_shift(cmd, over, tms, tdi);
    CHECK(clen == 524u); /* the exact byte count from the proving log line */
    CHECK(cli_send_all(cli, cmd, clen) == clen);

    static uint8_t tdo[MPS3_XVC_ACCEPT_VECTOR_BYTES];
    memset(tdo, 0, sizeof(tdo));
    CHECK(cli_recv_exact(cli, tdo, vec_bytes) == vec_bytes);

    /* Every one of the 2053 edges was clocked, the partial final byte included. */
    CHECK(s_swdbb.tap.rising_edges == over);
    CHECK(s_swdbb.sample_reads == (int)over);
    CHECK(s_swdbb.stray == 0);
    CHECK(s_swdbb.tap.state == FAKE_TAP_SHIFT_DR);
    check_recovered_idcode(fake_xvc_recover32(tdo, FAKE_TAP_IDCODE_FIRST_BIT), IDCODE);
    /* 2053 = 256*8 + 5: the 3 pad bits of the last byte must be zero, not stale
     * (a nonzero pad means the writer indexed past num_bits). */
    CHECK((tdo[256] & 0xE0u) == 0u);

    close(cli);
    posix_net_reset();
}

/* The engine must not shift until the LAST byte of a command has arrived, even
 * when the kernel delivers it in pieces the sender chose. */
static void test_fragmented_command_shifts_nothing_until_complete(void)
{
    uint16_t port = fresh();
    int cli = cli_connect(port);
    CHECK(cli >= 0);

    uint8_t tms[FAKE_TAP_IDCODE_SEQ_BYTES], tdi[FAKE_TAP_IDCODE_SEQ_BYTES];
    fake_tap_idcode_read_sequence(tms, tdi);
    uint8_t cmd[10 + 2 * FAKE_TAP_IDCODE_SEQ_BYTES];
    size_t clen = build_shift(cmd, FAKE_TAP_IDCODE_SEQ_BITS, tms, tdi);

    /* Split the prefix mid-word ("shi"), the length, and the vectors. */
    const size_t cuts[4] = { 3, 5, 6, 4 };
    size_t at = 0;
    for (int c = 0; c < 4; c++) {
        CHECK(cli_send_all(cli, cmd + at, cuts[c]) == cuts[c]);
        at += cuts[c];
        for (int i = 0; i < 64; i++) {
            xvc_server_poll();
        }
        CHECK(s_swdbb.ops == 0); /* incomplete: not one pin poked */
    }
    CHECK(at < clen);
    CHECK(cli_send_all(cli, cmd + at, clen - at) == clen - at);

    uint8_t tdo[FAKE_TAP_IDCODE_SEQ_BYTES];
    CHECK(cli_recv_exact(cli, tdo, sizeof(tdo)) == FAKE_TAP_IDCODE_SEQ_BYTES);
    check_recovered_idcode(fake_xvc_recover32(tdo, FAKE_TAP_IDCODE_FIRST_BIT), IDCODE);
    CHECK(s_swdbb.tap.rising_edges == FAKE_TAP_IDCODE_SEQ_BITS);

    close(cli);
    posix_net_reset();
}

/* Single client v1: hw_server/Identify hold one XVC connection per cable, and
 * the pins cannot be shared. A second connection is close-refused, and — the
 * part that matters — the FIRST client keeps working. */
static void test_second_client_is_close_refused(void)
{
    uint16_t port = fresh();
    int a = cli_connect(port);
    CHECK(a >= 0);
    xvc_server_poll();
    int b = cli_connect(port);
    CHECK(b >= 0);
    for (int i = 0; i < 64; i++) {
        xvc_server_poll();
    }
    CHECK(cli_saw_fin(b) == 1); /* the intruder was dropped */

    /* The incumbent is untouched. */
    CHECK(cli_send_all(a, "getinfo:", 8) == 8);
    char rsp[32];
    memset(rsp, 0, sizeof(rsp));
    CHECK(cli_recv_exact(a, rsp, 20) == 20);
    CHECK(memcmp(rsp, "xvcServer_v1.0:2048\n", 20) == 0);

    close(a);
    close(b);
    posix_net_reset();
}

/* Fail closed on a non-XVC verb: drop the connection, never resync-guess, and
 * never touch a pin. Over a real socket the drop is observable as a real FIN. */
static void test_junk_verb_fails_closed_over_a_real_socket(void)
{
    uint16_t port = fresh();
    int cli = cli_connect(port);
    CHECK(cli >= 0);
    CHECK(cli_send_all(cli, "quit:", 5) == 5);
    CHECK(cli_saw_fin(cli) == 1);
    CHECK(s_swdbb.ops == 0);
    close(cli);

    /* And num_bits over the ACCEPT ceiling: dropped before a vector byte is even
     * awaited. NOT the advertised size — a shift merely larger than we advertise
     * is legal and is what Identify sends (covered above). */
    port = fresh();
    cli = cli_connect(port);
    CHECK(cli >= 0);
    uint8_t cmd[10];
    memcpy(cmd, "shift:", 6);
    fake_xvc_put_le32(cmd + 6, MPS3_XVC_ACCEPT_VECTOR_BITS + 1u);
    CHECK(cli_send_all(cli, cmd, sizeof(cmd)) == sizeof(cmd));
    CHECK(cli_saw_fin(cli) == 1);
    CHECK(s_swdbb.ops == 0);
    close(cli);
    posix_net_reset();
}

/* A debugger session ends by closing the socket. The engine must release the
 * slot so the NEXT session is served — otherwise the daemon is single-use and a
 * second `com check` would hang, which is exactly the failure mode a bring-up
 * would blame on the fabric. */
static void test_a_new_client_is_served_after_the_first_disconnects(void)
{
    uint16_t port = fresh();

    int a = cli_connect(port);
    CHECK(a >= 0);
    CHECK(cli_send_all(a, "getinfo:", 8) == 8);
    char rsp[32];
    CHECK(cli_recv_exact(a, rsp, 20) == 20);
    close(a);
    for (int i = 0; i < 256; i++) {
        xvc_server_poll(); /* the FIN must be observed and the slot released */
    }

    int b = cli_connect(port);
    CHECK(b >= 0);
    uint8_t tms[FAKE_TAP_IDCODE_SEQ_BYTES], tdi[FAKE_TAP_IDCODE_SEQ_BYTES];
    fake_tap_idcode_read_sequence(tms, tdi);
    uint8_t cmd[10 + 2 * FAKE_TAP_IDCODE_SEQ_BYTES];
    size_t clen = build_shift(cmd, FAKE_TAP_IDCODE_SEQ_BITS, tms, tdi);
    CHECK(cli_send_all(b, cmd, clen) == clen);
    uint8_t tdo[FAKE_TAP_IDCODE_SEQ_BYTES];
    CHECK(cli_recv_exact(b, tdo, sizeof(tdo)) == FAKE_TAP_IDCODE_SEQ_BYTES);
    check_recovered_idcode(fake_xvc_recover32(tdo, FAKE_TAP_IDCODE_FIRST_BIT), IDCODE);

    close(b);
    posix_net_reset();
}

int main(void)
{
    test_this_binary_is_the_swdbb_target();
    test_the_shared_vectors_match_the_host_server();
    test_the_listener_is_a_real_kernel_socket();
    test_getinfo_over_a_real_socket();
    test_settck_echo_over_a_real_socket();
    test_idcode_read_over_a_real_socket();
    test_a_different_idcode_comes_back_different();
    test_identify_sized_2053_bit_shift_over_a_real_socket();
    test_fragmented_command_shifts_nothing_until_complete();
    test_second_client_is_close_refused();
    test_junk_verb_fails_closed_over_a_real_socket();
    test_a_new_client_is_served_after_the_first_disconnects();

    printf("%s: %d checks passed\n", XVC_LOOPBACK_TEST_NAME, s_checks);
    return 0;
}
