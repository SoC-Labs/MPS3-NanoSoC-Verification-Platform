/*
 * test_config_agent_commit.c — config_agent's v0.13 COMMIT sink
 * (config_agent.h "the COMMIT sink"), against the fake net backend and a
 * scripted sink that stands in for the overlay store:
 *
 *   1. while a commit sink is registered, BOTH pushes of a pair go to it (never
 *      to a staging slot, never to the ICAP-direct sink), header first;
 *   2. BACK-PRESSURE: write_some() may take less than offered, or nothing; the
 *      rest waits in the <= 512 B carry, nothing more is pulled off the
 *      connection until it drains, and every byte still arrives exactly once, in
 *      order (WINDOWED build: the window reopens only for bytes the sink TOOK);
 *   3. finish() only after every byte and a matching transport CRC; a CRC
 *      mismatch is reported through abort(CFG_AGENT_ERR_CRC), a torn push through
 *      abort(-1), a header config_agent rejects through abort(<the status>), each
 *      exactly once;
 *   4. a TFTP push while a commit is armed is refused, and reported;
 *   5. with the sink dropped, pushes go back to the ordinary staging path.
 *
 * Built twice (Makefile): the fire-hose receive, and -DMPS3_CFG_AGENT_WINDOWED.
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../config_agent/config_agent.h"
#include "../common/crc32.h"
#include "../common/net_if.h"
#include "fake_net_if.h"

#ifndef COMMIT_TEST_NAME
#define COMMIT_TEST_NAME "test_config_agent_commit"
#endif

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

#define STATIC_ID 0xA1B2C3D4u
#define HDR       ((int)MPS3_BITSTREAM_HDR_WIRE_SIZE)

/* ---- the scripted commit sink ------------------------------------------------ */
static uint8_t  s_got[2][64 * 1024];
static uint32_t s_got_n[2];
static int      s_begins[2], s_finishes[2], s_aborts, s_last_abort_why;
static int      s_cur = -1;          /* kind of the push in progress */
static int      s_refuse_begin;
static uint32_t s_take_max;          /* write_some takes at most this per call */
static uint32_t s_zero_every;        /* every Nth write_some takes NOTHING (0 = never) */
static uint32_t s_ws_calls;
static uint32_t s_max_offered;       /* the largest len ever offered */

static int sk_begin(const mps3_bitstream_hdr_t *h)
{
    if (s_refuse_begin) {
        return -1;
    }
    s_cur = h->kind;
    s_begins[s_cur]++;
    s_got_n[s_cur] = 0;
    return 0;
}

static int sk_write_some(const void *buf, uint32_t len, uint32_t *used)
{
    uint32_t take = len;
    s_ws_calls++;
    if (len > s_max_offered) {
        s_max_offered = len;
    }
    if (s_zero_every && (s_ws_calls % s_zero_every) == 0u) {
        take = 0;
    } else if (s_take_max && take > s_take_max) {
        take = s_take_max;
    }
    memcpy(&s_got[s_cur][s_got_n[s_cur]], buf, take);
    s_got_n[s_cur] += take;
    *used = take;
    return 0;
}

static int sk_finish(const mps3_bitstream_hdr_t *h)
{
    s_finishes[h->kind]++;
    s_cur = -1;
    return 0;
}

static void sk_abort(int why)
{
    s_aborts++;
    s_last_abort_why = why;
    s_cur = -1;
}

static const mps3_cfg_agent_commit_sink_t k_sink = {
    sk_begin, sk_write_some, sk_finish, sk_abort,
};

/* A sink that must NEVER be touched while a commit is armed. */
static int s_icap_calls;
static int  ic_begin(uint32_t n)                { (void)n; s_icap_calls++; return 0; }
static int  ic_write(const void *b, uint32_t n) { (void)b; (void)n; s_icap_calls++; return 0; }
static int  ic_finish(uint32_t c)               { (void)c; s_icap_calls++; return 0; }
static void ic_abort(void)                      { s_icap_calls++; }
static const mps3_cfg_agent_qspi_sink_t k_icap = { ic_begin, ic_write, ic_finish, ic_abort, NULL };

static void reset(void)
{
    fake_net_reset();
    config_agent_init();
    config_agent_set_running_static_id(STATIC_ID);
    config_agent_set_icap_direct_sink(&k_icap);
    memset(s_got_n, 0, sizeof s_got_n);
    memset(s_begins, 0, sizeof s_begins);
    memset(s_finishes, 0, sizeof s_finishes);
    s_aborts = 0;
    s_last_abort_why = 0;
    s_cur = -1;
    s_refuse_begin = 0;
    s_take_max = 0;
    s_zero_every = 0;
    s_ws_calls = 0;
    s_max_offered = 0;
    s_icap_calls = 0;
}

static uint8_t s_frame[80 * 1024];

static int build(uint8_t *out, uint8_t kind, uint32_t rm_id, uint32_t static_id,
                 uint32_t len_words, uint8_t seed, int corrupt_crc)
{
    uint32_t n = len_words * 4u;
    for (uint32_t i = 0; i < n; i++) {
        out[HDR + i] = (uint8_t)(seed + i * 7u);
    }
    mps3_bitstream_hdr_t h;
    memset(&h, 0, sizeof h);
    memcpy(h.magic, MPS3_BITSTREAM_MAGIC, 4);
    h.ver = MPS3_BITSTREAM_VER;
    h.kind = kind;
    h.static_id = static_id;
    h.rm_id = rm_id;
    h.len_words = len_words;
    h.crc32 = mps3_crc32(out + HDR, n) ^ (corrupt_crc ? 1u : 0u);
    mps3_bitstream_hdr_pack(&h, out);
    return HDR + (int)n;
}

/* Push one frame over 6910, polling between sends; returns the client. */
static int push(const uint8_t *f, int len, int close_at_end)
{
    int cli = fake_net_connect(MPS3_PORT_RAW_PUSH);
    assert(cli >= 0);
    int off = 0;
    while (off < len) {
        int want = len - off;
        if (want > 3000) want = 3000;
        int sent = fake_net_send(cli, f + off, want);
        assert(sent >= 0);
        off += sent;
        for (int i = 0; i < 8; i++) config_agent_poll();
    }
    if (close_at_end) {
        fake_net_close(cli);
    }
    for (int i = 0; i < 20000 && !fake_net_fw_closed(cli); i++) {
        config_agent_poll();
    }
    return cli;
}

static void check_bytes(int kind, const uint8_t *frame, int len)
{
    CHECK(s_got_n[kind] == (uint32_t)(len - HDR));
    CHECK(memcmp(s_got[kind], frame + HDR, (size_t)(len - HDR)) == 0);
}

/* 1 + 2 + 3(finish): the happy pair, under back-pressure. */
static void test_pair_goes_to_the_sink_under_back_pressure(uint32_t take_max, uint32_t zero_every)
{
    reset();
    config_agent_set_commit_sink(&k_sink);
    s_take_max = take_max;
    s_zero_every = zero_every;

    int n = build(s_frame, MPS3_BIN_KIND_CLEARING, 7, STATIC_ID, 3000, 0x11, 0);
    int cli = push(s_frame, n, 1);
    CHECK(fake_net_fw_closed(cli));
    CHECK(s_begins[0] == 1 && s_finishes[0] == 1 && s_aborts == 0);
    check_bytes(0, s_frame, n);
#ifdef MPS3_CFG_AGENT_WINDOWED
    /* The window reopened for EXACTLY the bytes the sink took (+ the header). */
    CHECK(fake_net_recved_total(cli) == (uint32_t)n);
#endif

    n = build(s_frame, MPS3_BIN_KIND_PARTIAL, 7, STATIC_ID, 9000, 0x55, 0);
    cli = push(s_frame, n, 1);
    CHECK(fake_net_fw_closed(cli));
    CHECK(s_begins[1] == 1 && s_finishes[1] == 1 && s_aborts == 0);
    check_bytes(1, s_frame, n);

    /* Nothing reached a staging slot or the ICAP-direct sink. */
    config_agent_bitstream_info_t info;
    CHECK(config_agent_take_validated_clearing(&info) != 0);
    CHECK(config_agent_take_validated_partial(&info) != 0);
    CHECK(s_icap_calls == 0);
    /* The carry never has to hold more than one recv chunk. */
    CHECK(s_max_offered <= 512u);
    if (take_max || zero_every) {
        CHECK(s_ws_calls > (uint32_t)(n - HDR) / 512u);   /* many short takes */
    }
}

/* 3: CRC mismatch, torn push, rejected header: abort(why), exactly once. */
static void test_failures_are_reported_once(void)
{
    reset();
    config_agent_set_commit_sink(&k_sink);
    int n = build(s_frame, MPS3_BIN_KIND_CLEARING, 7, STATIC_ID, 200, 0x22, /*corrupt=*/1);
    (void)push(s_frame, n, 1);
    CHECK(s_aborts == 1 && s_last_abort_why == CFG_AGENT_ERR_CRC);
    CHECK(s_finishes[0] == 0);

    /* torn: close half way through the payload */
    reset();
    config_agent_set_commit_sink(&k_sink);
    n = build(s_frame, MPS3_BIN_KIND_CLEARING, 7, STATIC_ID, 2000, 0x22, 0);
    (void)push(s_frame, n / 2, 1);
    CHECK(s_begins[0] == 1 && s_aborts == 1 && s_last_abort_why == -1);
    CHECK(s_finishes[0] == 0);

    /* a header config_agent rejects (another shell's static_id): the sink is
     * told, without begin() */
    reset();
    config_agent_set_commit_sink(&k_sink);
    n = build(s_frame, MPS3_BIN_KIND_CLEARING, 7, 0xDEADBEEFu, 16, 0x22, 0);
    (void)push(s_frame, n, 1);
    CHECK(s_begins[0] == 0 && s_aborts == 1 && s_last_abort_why == CFG_AGENT_ERR_STATIC_ID);

    /* a partial first (order): rejected, reported */
    reset();
    config_agent_set_commit_sink(&k_sink);
    n = build(s_frame, MPS3_BIN_KIND_PARTIAL, 7, STATIC_ID, 16, 0x22, 0);
    (void)push(s_frame, n, 1);
    CHECK(s_begins[1] == 0 && s_aborts == 1 && s_last_abort_why == CFG_AGENT_ERR_ORDER);

    /* the sink refusing the header tears the push; no abort is owed */
    reset();
    config_agent_set_commit_sink(&k_sink);
    s_refuse_begin = 1;
    n = build(s_frame, MPS3_BIN_KIND_CLEARING, 7, STATIC_ID, 16, 0x22, 0);
    int cli = push(s_frame, n, 0);
    CHECK(fake_net_fw_closed(cli));
    CHECK(s_aborts == 0);
    fake_net_close(cli);

    /* config_agent_abort_session() mid-push (the coordinator's idle timeout)
     * reports a torn push. */
    reset();
    config_agent_set_commit_sink(&k_sink);
    n = build(s_frame, MPS3_BIN_KIND_CLEARING, 7, STATIC_ID, 2000, 0x22, 0);
    cli = fake_net_connect(MPS3_PORT_RAW_PUSH);
    CHECK(fake_net_send(cli, s_frame, 1000) == 1000);
    for (int i = 0; i < 16; i++) config_agent_poll();
    CHECK(s_begins[0] == 1 && s_aborts == 0);
    config_agent_abort_session();
    CHECK(s_aborts == 1 && s_last_abort_why == -1);
    config_agent_abort_session();          /* idempotent: never a second abort */
    CHECK(s_aborts == 1);
}

/* 2 (hold): while the sink takes NOTHING, nothing more is pulled off the
 * connection -- the host is held by TCP, not by a buffer here. */
static void test_a_stalled_store_holds_the_connection(void)
{
    reset();
    config_agent_set_commit_sink(&k_sink);
    s_zero_every = 1;                       /* every write_some takes nothing */
    int n = build(s_frame, MPS3_BIN_KIND_CLEARING, 7, STATIC_ID, 4000, 0x33, 0);
    int cli = fake_net_connect(MPS3_PORT_RAW_PUSH);
    CHECK(fake_net_send(cli, s_frame, n) == n);
    for (int i = 0; i < 200; i++) config_agent_poll();
    CHECK(s_begins[0] == 1);
    CHECK(s_got_n[0] == 0);
    uint32_t got = 0, expect = 0;
    config_agent_rx_progress(&got, &expect);
    CHECK(got == 0 && expect == 4000u * 4u);
    CHECK(!fake_net_fw_closed(cli));
    /* The store frees up: every byte arrives, in order, once. */
    s_zero_every = 0;
    fake_net_close(cli);
    for (int i = 0; i < 20000 && !fake_net_fw_closed(cli); i++) config_agent_poll();
    CHECK(s_finishes[0] == 1 && s_aborts == 0);
    check_bytes(0, s_frame, n);
}

/* 4: TFTP while armed. */
static void test_tftp_is_refused_while_armed(void)
{
    reset();
    config_agent_set_commit_sink(&k_sink);
    int n = build(s_frame, MPS3_BIN_KIND_CLEARING, 7, STATIC_ID, 64, 0x44, 0);
    uint8_t pkt[600], rsp[600];
    uint16_t fw_port = 0;
    int wl = 0;
    pkt[wl++] = 0; pkt[wl++] = 2;
    memcpy(&pkt[wl], "c.bin\0octet\0", 12); wl += 12;
    CHECK(fake_net_udp_inject(MPS3_PORT_TFTP, 5001, pkt, wl) == 0);
    for (int i = 0; i < 4; i++) config_agent_poll();
    CHECK(fake_net_udp_take_sent(&fw_port, NULL, rsp, sizeof rsp) == 4);   /* ACK 0 */
    pkt[0] = 0; pkt[1] = 3; pkt[2] = 0; pkt[3] = 1;
    memcpy(&pkt[4], s_frame, 256);
    CHECK(fake_net_udp_inject(fw_port, 5001, pkt, 4 + 256) == 0);
    for (int i = 0; i < 4; i++) config_agent_poll();
    int rn = fake_net_udp_take_sent(NULL, NULL, rsp, sizeof rsp);
    CHECK(rn >= 4 && rsp[1] == 5);                                         /* ERROR */
    CHECK(s_begins[0] == 0 && s_aborts == 1 && s_last_abort_why == CFG_AGENT_ERR_ORDER);
    (void)n;
}

/* 4b (merge with v0.14): a slot-image push while a commit is armed is refused
 * with ERR_ORDER, the commit's sink is NOT told (no abort), and the commit's own
 * pair still goes through afterwards. */
static void test_slot_image_is_refused_while_armed(void)
{
    reset();
    config_agent_set_commit_sink(&k_sink);
    int n = build(s_frame, MPS3_BIN_KIND_SLOT_IMAGE, 0, STATIC_ID, 64, 0x55, 0);
    int cli = push(s_frame, n, 0);
    CHECK(fake_net_fw_closed(cli));                 /* refused: the fw hung up   */
    CHECK(s_begins[0] == 0 && s_begins[1] == 0);    /* nothing reached the sink  */
    CHECK(s_aborts == 0);                           /* the commit is still armed */
    n = build(s_frame, MPS3_BIN_KIND_CLEARING, 7, STATIC_ID, 64, 0x44, 0);
    (void)push(s_frame, n, 1);
    CHECK(s_begins[0] == 1);                        /* its pair still arrives    */
    check_bytes(0, s_frame, n);
}

/* 5: dropped sink -> the ordinary staging path again. */
static void test_dropping_the_sink_restores_staging(void)
{
    reset();
    config_agent_set_commit_sink(&k_sink);
    config_agent_set_commit_sink(NULL);
    int n = build(s_frame, MPS3_BIN_KIND_CLEARING, 9, STATIC_ID, 64, 0x66, 0);
    (void)push(s_frame, n, 1);
    CHECK(s_begins[0] == 0 && s_aborts == 0);
    config_agent_bitstream_info_t info;
    CHECK(config_agent_take_validated_clearing(&info) == 0 && info.rm_id == 9u);
    /* ...and config_agent_init() forgets a registration (a commit is a request). */
    config_agent_set_commit_sink(&k_sink);
    config_agent_init();
    n = build(s_frame, MPS3_BIN_KIND_CLEARING, 9, STATIC_ID, 64, 0x66, 0);
    (void)push(s_frame, n, 1);
    CHECK(s_begins[0] == 0);
}

int main(void)
{
    test_pair_goes_to_the_sink_under_back_pressure(0u, 0u);     /* takes all */
    test_pair_goes_to_the_sink_under_back_pressure(100u, 0u);   /* short takes */
    test_pair_goes_to_the_sink_under_back_pressure(37u, 3u);    /* odd + zero takes */
    test_failures_are_reported_once();
    test_a_stalled_store_holds_the_connection();
    test_tftp_is_refused_while_armed();
    test_slot_image_is_refused_while_armed();
    test_dropping_the_sink_restores_staging();
    printf("%s: %d checks passed\n", COMMIT_TEST_NAME, s_checks);
    return 0;
}
