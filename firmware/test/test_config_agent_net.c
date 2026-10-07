/*
 * test_config_agent_net.c — host-gcc tests for config_agent.c's REAL
 * receive sessions (W-NET-SEAM): raw-TCP 6910 and TFTP :69, driven through
 * fake_net_if.c exactly as a host pusher would drive real sockets, plus
 * the two-slot {clearing, partial} pair staging (config_agent.h STAGING
 * DECISION — the C mirror of fakeshell.py's ConfigAgentModel semantics).
 *
 * Links: config_agent.c, common/net_proto.c, common/crc32.c,
 * common/net_if.c, fake_net_if.c. No mock_regs (config_agent has zero
 * register access — its file-header claim, still true with the seam).
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../config_agent/config_agent.h"
#include "../common/crc32.h"
#include "../common/net_if.h"
#include "fake_net_if.h"

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

#define STATIC_ID 0xA1B2C3D4u
#define CLIENT_TID 5001

/* Build one wire frame (24-byte header + payload) into out; returns total
 * length. Payload is a deterministic pattern of len_words words. */
static int build_frame(uint8_t *out, mps3_bin_kind_t kind, uint32_t static_id,
                       uint32_t rm_id, uint32_t len_words, uint8_t seed,
                       int corrupt_crc)
{
    uint32_t nbytes = len_words * 4u;
    uint8_t *payload = out + MPS3_BITSTREAM_HDR_WIRE_SIZE;
    for (uint32_t i = 0; i < nbytes; i++) {
        payload[i] = (uint8_t)(seed + i);
    }
    mps3_bitstream_hdr_t hdr;
    memset(&hdr, 0, sizeof(hdr));
    memcpy(hdr.magic, MPS3_BITSTREAM_MAGIC, 4);
    hdr.ver = MPS3_BITSTREAM_VER;
    hdr.kind = (uint8_t)kind;
    hdr.static_id = static_id;
    hdr.rm_id = rm_id;
    hdr.len_words = len_words;
    hdr.crc32 = mps3_crc32(payload, nbytes) ^ (corrupt_crc ? 0xFFFFFFFFu : 0u);
    mps3_bitstream_hdr_pack(&hdr, out);
    return (int)(MPS3_BITSTREAM_HDR_WIRE_SIZE + nbytes);
}

static void polls(int n)
{
    for (int i = 0; i < n; i++) {
        config_agent_poll();
    }
}

static void fresh(void)
{
    fake_net_reset();
    config_agent_init();
    config_agent_set_running_static_id(STATIC_ID);
}

/* ---- raw TCP (6910) -------------------------------------------------------- */

/* Push one frame the way fakeshell's raw_tcp_put does: send all, half-close,
 * poll until the firmware closes. Returns 1 if the firmware closed (it
 * always does — accept or reject both end in close). */
static void raw_push(const uint8_t *frame, int len)
{
    int cli = fake_net_connect(MPS3_PORT_RAW_PUSH);
    assert(cli >= 0);
    CHECK(fake_net_send(cli, frame, len) == len);
    fake_net_close(cli); /* client half-close: EOF is the sync point */
    polls(64);
    CHECK(fake_net_fw_closed(cli));
}

static void test_tcp_pair_staged_and_taken(void)
{
    fresh();
    static uint8_t frame[4096];

    int n = build_frame(frame, MPS3_BIN_KIND_CLEARING, STATIC_ID, 7, 16, 0x10, 0);
    raw_push(frame, n);
    CHECK(!config_agent_pair_ready()); /* clearing alone is half a pair */

    n = build_frame(frame, MPS3_BIN_KIND_PARTIAL, STATIC_ID, 7, 12, 0x40, 0);
    raw_push(frame, n);
    CHECK(config_agent_pair_ready()); /* BOTH staged before any swap — the two-slot fix */

    config_agent_bitstream_info_t info;
    CHECK(config_agent_take_validated_clearing(&info) == 0);
    CHECK(info.rm_id == 7 && info.len_words == 16 && info.static_id == STATIC_ID);
    CHECK(info.data != NULL);
    CHECK(info.data[0] == 0x10 && info.data[63] == (uint8_t)(0x10 + 63));

    CHECK(config_agent_take_validated_partial(&info) == 0);
    CHECK(info.rm_id == 7 && info.len_words == 12);
    CHECK(info.data != NULL && info.data[0] == 0x40);

    /* consumed: */
    CHECK(config_agent_take_validated_clearing(&info) != 0);
    CHECK(config_agent_take_validated_partial(&info) != 0);
}

static void test_tcp_partial_before_clearing_rejected(void)
{
    fresh();
    static uint8_t frame[4096];
    int n = build_frame(frame, MPS3_BIN_KIND_PARTIAL, STATIC_ID, 7, 8, 0x40, 0);
    raw_push(frame, n); /* I2 ordering: header-time ERR_ORDER, fw closes */
    config_agent_bitstream_info_t info;
    CHECK(config_agent_take_validated_partial(&info) != 0);

    /* And a clearing AFTER unblocks a partial (fresh pair). */
    n = build_frame(frame, MPS3_BIN_KIND_CLEARING, STATIC_ID, 7, 8, 0x10, 0);
    raw_push(frame, n);
    n = build_frame(frame, MPS3_BIN_KIND_PARTIAL, STATIC_ID, 7, 8, 0x40, 0);
    raw_push(frame, n);
    CHECK(config_agent_pair_ready());
}

static void test_tcp_new_clearing_drops_stale_partial(void)
{
    fresh();
    static uint8_t frame[4096];
    int n = build_frame(frame, MPS3_BIN_KIND_CLEARING, STATIC_ID, 7, 8, 0x10, 0);
    raw_push(frame, n);
    n = build_frame(frame, MPS3_BIN_KIND_PARTIAL, STATIC_ID, 7, 8, 0x40, 0);
    raw_push(frame, n);
    CHECK(config_agent_pair_ready());

    /* A new clearing starts a NEW pair: the staged partial must drop
     * (fakeshell ConfigAgentModel.finish_payload semantics). */
    n = build_frame(frame, MPS3_BIN_KIND_CLEARING, STATIC_ID, 9, 8, 0x20, 0);
    raw_push(frame, n);
    CHECK(!config_agent_pair_ready());
    config_agent_bitstream_info_t info;
    CHECK(config_agent_take_validated_clearing(&info) == 0);
    CHECK(info.rm_id == 9); /* the NEW clearing */
    CHECK(config_agent_take_validated_partial(&info) != 0); /* stale partial gone */
}

static void test_tcp_rejections_never_stage(void)
{
    fresh();
    static uint8_t frame[4096];
    config_agent_bitstream_info_t info;
    int n;

    /* wrong static_id */
    n = build_frame(frame, MPS3_BIN_KIND_CLEARING, 0xDEADBEEFu, 7, 8, 0x10, 0);
    raw_push(frame, n);
    CHECK(config_agent_take_validated_clearing(&info) != 0);

    /* corrupt CRC */
    n = build_frame(frame, MPS3_BIN_KIND_CLEARING, STATIC_ID, 7, 8, 0x10, /*corrupt=*/1);
    raw_push(frame, n);
    CHECK(config_agent_take_validated_clearing(&info) != 0);

    /* torn transfer: half the payload then EOF */
    n = build_frame(frame, MPS3_BIN_KIND_CLEARING, STATIC_ID, 7, 8, 0x10, 0);
    {
        int cli = fake_net_connect(MPS3_PORT_RAW_PUSH);
        CHECK(fake_net_send(cli, frame, n - 16) == n - 16);
        fake_net_close(cli);
        polls(64);
        CHECK(fake_net_fw_closed(cli));
    }
    CHECK(config_agent_take_validated_clearing(&info) != 0);

    /* extra bytes beyond the declared frame */
    n = build_frame(frame, MPS3_BIN_KIND_CLEARING, STATIC_ID, 7, 8, 0x10, 0);
    frame[n] = 0xEE;
    raw_push(frame, n + 1);
    CHECK(config_agent_take_validated_clearing(&info) != 0);

    /* bad magic */
    n = build_frame(frame, MPS3_BIN_KIND_CLEARING, STATIC_ID, 7, 8, 0x10, 0);
    frame[0] = 'X';
    raw_push(frame, n);
    CHECK(config_agent_take_validated_clearing(&info) != 0);
}

static void test_tcp_torn_repush_invalidates_then_recovers(void)
{
    /* A good clearing is staged; a RE-push of a clearing that tears must
     * leave NO stale "validated" clearing behind (fail closed), and a
     * subsequent good push stages again. */
    fresh();
    static uint8_t frame[4096];
    int n = build_frame(frame, MPS3_BIN_KIND_CLEARING, STATIC_ID, 7, 8, 0x10, 0);
    raw_push(frame, n);

    int cli = fake_net_connect(MPS3_PORT_RAW_PUSH);
    CHECK(fake_net_send(cli, frame, n - 4) == n - 4); /* tears mid-payload */
    fake_net_close(cli);
    polls(64);

    config_agent_bitstream_info_t info;
    CHECK(config_agent_take_validated_clearing(&info) != 0); /* invalidated */

    n = build_frame(frame, MPS3_BIN_KIND_CLEARING, STATIC_ID, 8, 8, 0x30, 0);
    raw_push(frame, n);
    CHECK(config_agent_take_validated_clearing(&info) == 0);
    CHECK(info.rm_id == 8);
}

static void test_tcp_second_connection_refused_while_busy(void)
{
    fresh();
    static uint8_t frame[4096];
    int n = build_frame(frame, MPS3_BIN_KIND_CLEARING, STATIC_ID, 7, 8, 0x10, 0);

    int first = fake_net_connect(MPS3_PORT_RAW_PUSH);
    CHECK(fake_net_send(first, frame, 10) == 10); /* mid-header, session busy */
    polls(4);
    CHECK(!fake_net_fw_closed(first));

    int second = fake_net_connect(MPS3_PORT_RAW_PUSH);
    polls(4);
    CHECK(fake_net_fw_closed(second)); /* refused immediately */
    fake_net_close(second);

    /* First transfer still completes fine. */
    CHECK(fake_net_send(first, frame + 10, n - 10) == n - 10);
    fake_net_close(first);
    polls(64);
    config_agent_bitstream_info_t info;
    CHECK(config_agent_take_validated_clearing(&info) == 0);
}

/* ---- TFTP (:69, RFC1350 WRQ/octet, SINGLE-PORT) ------------------------------ */

/* SINGLE-PORT: every datagram the server sends comes FROM :69, never from a fresh
 * transfer ID, and the client sends every DATA to :69. A host behind a stateful
 * firewall accepts only replies from the (ip, port) it sent to; the hub drops
 * fresh-TID replies (B1, 2026-09-24 -- stage0's copy of the bug). */

/* Drive one whole TFTP PUT like pyverify's tftp_put: WRQ, then 512-byte
 * DATA blocks, checking ACKs. Returns 0 on full success (final ACK), or
 * -1 if the server sent ERROR at any point. Every reply must come FROM :69. */
static int tftp_put(const uint8_t *data, int len, const char *mode)
{
    uint8_t pkt[600], rsp[600];
    uint16_t fw_port = 0, to_port = 0;

    /* WRQ "bitstream.bin" */
    int wl = 0;
    pkt[wl++] = 0; pkt[wl++] = 2; /* WRQ */
    const char *fn = "bitstream.bin";
    memcpy(&pkt[wl], fn, strlen(fn) + 1); wl += (int)strlen(fn) + 1;
    memcpy(&pkt[wl], mode, strlen(mode) + 1); wl += (int)strlen(mode) + 1;
    if (fake_net_udp_inject(MPS3_PORT_TFTP, CLIENT_TID, pkt, wl) != 0) {
        return -1;
    }
    polls(4);
    int rn = fake_net_udp_take_sent(&fw_port, &to_port, rsp, sizeof(rsp));
    if (rn >= 0) {
        assert(fw_port == MPS3_PORT_TFTP); /* single-port: even an ERROR */
    }
    if (rn < 4 || rsp[1] == 5 /* ERROR */) {
        return -1;
    }
    assert(rsp[1] == 4 && rsp[2] == 0 && rsp[3] == 0); /* ACK 0 */
    assert(to_port == CLIENT_TID);
    assert(fw_port == MPS3_PORT_TFTP); /* single-port: ACK 0 FROM :69 */
    uint16_t tid = fw_port;

    int off = 0;
    uint16_t block = 1;
    for (;;) {
        int chunk = len - off;
        if (chunk > 512) chunk = 512;
        pkt[0] = 0; pkt[1] = 3; /* DATA */
        pkt[2] = (uint8_t)(block >> 8); pkt[3] = (uint8_t)block;
        memcpy(&pkt[4], data + off, (size_t)chunk);
        if (fake_net_udp_inject(tid, CLIENT_TID, pkt, 4 + chunk) != 0) {
            return -1;
        }
        polls(4);
        rn = fake_net_udp_take_sent(&fw_port, &to_port, rsp, sizeof(rsp));
        if (rn < 4) {
            return -1;
        }
        assert(fw_port == MPS3_PORT_TFTP && to_port == CLIENT_TID); /* ACK/ERROR FROM :69 */
        if (rsp[1] == 5) {
            return -1; /* server ERROR: rejected */
        }
        assert(rsp[1] == 4);
        assert(((rsp[2] << 8) | rsp[3]) == block);
        off += chunk;
        if (chunk < 512) {
            return 0; /* final block ACKed */
        }
        block++;
    }
}

static void test_tftp_pair_happy_path(void)
{
    fresh();
    static uint8_t frame[4096];
    /* Sized so the transfer spans multiple DATA blocks (24 + 600*4 bytes). */
    int n = build_frame(frame, MPS3_BIN_KIND_CLEARING, STATIC_ID, 3, 600, 0x11, 0);
    CHECK(tftp_put(frame, n, "octet") == 0);
    n = build_frame(frame, MPS3_BIN_KIND_PARTIAL, STATIC_ID, 3, 200, 0x22, 0);
    CHECK(tftp_put(frame, n, "octet") == 0);
    CHECK(config_agent_pair_ready());

    config_agent_bitstream_info_t info;
    CHECK(config_agent_take_validated_clearing(&info) == 0);
    CHECK(info.rm_id == 3 && info.len_words == 600);
    CHECK(info.data[0] == 0x11);
    CHECK(config_agent_take_validated_partial(&info) == 0);
    CHECK(info.len_words == 200 && info.data[0] == 0x22);
}

static void test_tftp_rejects(void)
{
    fresh();
    static uint8_t frame[4096];
    config_agent_bitstream_info_t info;
    int n;

    /* non-octet mode refused up front */
    n = build_frame(frame, MPS3_BIN_KIND_CLEARING, STATIC_ID, 3, 8, 0x11, 0);
    CHECK(tftp_put(frame, n, "netascii") == -1);

    /* partial before clearing: ERROR once the header (block 1) lands */
    n = build_frame(frame, MPS3_BIN_KIND_PARTIAL, STATIC_ID, 3, 8, 0x22, 0);
    CHECK(tftp_put(frame, n, "octet") == -1);
    CHECK(config_agent_take_validated_partial(&info) != 0);

    /* corrupt CRC: ERROR instead of the final ACK */
    n = build_frame(frame, MPS3_BIN_KIND_CLEARING, STATIC_ID, 3, 8, 0x11, /*corrupt=*/1);
    CHECK(tftp_put(frame, n, "octet") == -1);
    CHECK(config_agent_take_validated_clearing(&info) != 0);

    /* RRQ: push-only server */
    uint8_t rrq[32] = { 0, 1, 'f', 0, 'o', 'c', 't', 'e', 't', 0 };
    CHECK(fake_net_udp_inject(MPS3_PORT_TFTP, CLIENT_TID, rrq, 10) == 0);
    polls(4);
    uint8_t rsp[600];
    uint16_t from = 0;
    CHECK(fake_net_udp_take_sent(&from, NULL, rsp, sizeof(rsp)) >= 4);
    CHECK(rsp[1] == 5); /* ERROR */
    CHECK(from == MPS3_PORT_TFTP);
}

static void test_tftp_duplicate_block_reacked(void)
{
    fresh();
    static uint8_t frame[4096];
    int n = build_frame(frame, MPS3_BIN_KIND_CLEARING, STATIC_ID, 3, 200, 0x33, 0);

    /* Hand-drive: WRQ, block 1, DUPLICATE block 1 (lost-ACK replay), final. */
    uint8_t pkt[600], rsp[600];
    uint16_t tid = 0, to = 0;
    pkt[0] = 0; pkt[1] = 2;
    memcpy(&pkt[2], "f\0octet\0", 8);
    CHECK(fake_net_udp_inject(MPS3_PORT_TFTP, CLIENT_TID, pkt, 10) == 0);
    polls(2);
    CHECK(fake_net_udp_take_sent(&tid, &to, rsp, sizeof(rsp)) == 4); /* ACK0 */
    CHECK(tid == MPS3_PORT_TFTP); /* single-port */

    pkt[0] = 0; pkt[1] = 3; pkt[2] = 0; pkt[3] = 1;
    memcpy(&pkt[4], frame, 512);
    CHECK(fake_net_udp_inject(tid, CLIENT_TID, pkt, 516) == 0);
    polls(2);
    CHECK(fake_net_udp_take_sent(NULL, NULL, rsp, sizeof(rsp)) == 4);
    CHECK(rsp[1] == 4 && rsp[3] == 1); /* ACK 1 */

    /* duplicate block 1: must re-ACK and NOT double-append */
    CHECK(fake_net_udp_inject(tid, CLIENT_TID, pkt, 516) == 0);
    polls(2);
    CHECK(fake_net_udp_take_sent(NULL, NULL, rsp, sizeof(rsp)) == 4);
    CHECK(rsp[1] == 4 && rsp[3] == 1); /* re-ACK 1 */

    /* final block 2 with the remainder */
    int rem = n - 512;
    pkt[3] = 2;
    memcpy(&pkt[4], frame + 512, (size_t)rem);
    CHECK(fake_net_udp_inject(tid, CLIENT_TID, pkt, 4 + rem) == 0);
    polls(2);
    CHECK(fake_net_udp_take_sent(NULL, NULL, rsp, sizeof(rsp)) == 4);
    CHECK(rsp[1] == 4 && rsp[3] == 2); /* final ACK — accepted */

    config_agent_bitstream_info_t info;
    CHECK(config_agent_take_validated_clearing(&info) == 0);
    CHECK(info.len_words == 200 && info.data[0] == 0x33);
}

/* Take the ONE reply the server owes; it must come FROM :69 and go to `to`.
 * Returns its opcode (4 ACK / 5 ERROR) with the block/code in *arg, or -1 if the
 * server sent nothing. */
static int sp_reply(uint16_t to, int *arg)
{
    uint8_t rsp[600];
    uint16_t from = 0, dst = 0;
    polls(2);
    int rn = fake_net_udp_take_sent(&from, &dst, rsp, sizeof(rsp));
    if (rn < 0) {
        return -1;
    }
    CHECK(rn >= 4);
    CHECK(from == MPS3_PORT_TFTP); /* never a fresh transfer ID */
    CHECK(dst == to);
    *arg = (rsp[2] << 8) | rsp[3];
    return rsp[1];
}

static void sp_send(uint16_t src, const uint8_t *pkt, int len)
{
    CHECK(fake_net_udp_inject(MPS3_PORT_TFTP, src, pkt, len) == 0);
}

/* The single-port session, peer-identified: a repeated WRQ (our ACK 0 was lost)
 * is re-ACKed, not refused; a second client is refused FROM :69 without
 * disturbing the first; a foreign peer's DATA is ignored; and the whole push
 * rides port 69 end to end. */
static void test_tftp_single_port_session(void)
{
    fresh();
    static uint8_t frame[4096];
    int n = build_frame(frame, MPS3_BIN_KIND_CLEARING, STATIC_ID, 5, 200, 0x44, 0);
    enum { OTHER = 5002 };
    uint8_t wrq[16] = { 0, 2, 'f', 0, 'o', 'c', 't', 'e', 't', 0 };
    uint8_t pkt[600];
    int arg = -1;

    sp_send(CLIENT_TID, wrq, 10);
    CHECK(sp_reply(CLIENT_TID, &arg) == 4 && arg == 0);        /* ACK 0 FROM :69 */

    /* ACK 0 lost: the client repeats its WRQ from the same port -> ACK 0 again */
    sp_send(CLIENT_TID, wrq, 10);
    CHECK(sp_reply(CLIENT_TID, &arg) == 4 && arg == 0);

    /* another client's WRQ mid-session: ERROR FROM :69, the session survives */
    sp_send(OTHER, wrq, 10);
    CHECK(sp_reply(OTHER, &arg) == 5);

    /* a foreign peer's DATA 1 is not the session's: no reply, nothing staged */
    pkt[0] = 0; pkt[1] = 3; pkt[2] = 0; pkt[3] = 1;
    memset(&pkt[4], 0xEE, 512);
    sp_send(OTHER, pkt, 516);
    CHECK(sp_reply(OTHER, &arg) == -1);

    /* the session's own DATA, all to :69 */
    memcpy(&pkt[4], frame, 512);
    sp_send(CLIENT_TID, pkt, 516);
    CHECK(sp_reply(CLIENT_TID, &arg) == 4 && arg == 1);
    /* a stray ACK on :69 (a confused peer) is ignored */
    uint8_t ack[4] = { 0, 4, 0, 1 };
    sp_send(OTHER, ack, 4);
    CHECK(sp_reply(OTHER, &arg) == -1);
    int rem = n - 512;
    pkt[3] = 2;
    memcpy(&pkt[4], frame + 512, (size_t)rem);
    sp_send(CLIENT_TID, pkt, 4 + rem);
    CHECK(sp_reply(CLIENT_TID, &arg) == 4 && arg == 2);        /* final ACK */

    config_agent_bitstream_info_t info;
    CHECK(config_agent_take_validated_clearing(&info) == 0);
    CHECK(info.rm_id == 5 && info.len_words == 200 && info.data[0] == 0x44);

    /* idle again: DATA on :69 from the finished peer is ignored, a new WRQ works */
    sp_send(CLIENT_TID, pkt, 4 + rem);
    CHECK(sp_reply(CLIENT_TID, &arg) == -1);
    sp_send(OTHER, wrq, 10);
    CHECK(sp_reply(OTHER, &arg) == 4 && arg == 0);
    uint8_t err[5] = { 0, 5, 0, 0, 0 };                        /* client gives up */
    sp_send(OTHER, err, 5);
    CHECK(sp_reply(OTHER, &arg) == -1);                         /* never answer an ERROR */
    sp_send(CLIENT_TID, wrq, 10);                               /* session torn: free */
    CHECK(sp_reply(CLIENT_TID, &arg) == 4 && arg == 0);
}

int main(void)
{
    test_tcp_pair_staged_and_taken();
    test_tcp_partial_before_clearing_rejected();
    test_tcp_new_clearing_drops_stale_partial();
    test_tcp_rejections_never_stage();
    test_tcp_torn_repush_invalidates_then_recovers();
    test_tcp_second_connection_refused_while_busy();
    test_tftp_pair_happy_path();
    test_tftp_rejects();
    test_tftp_duplicate_block_reacked();
    test_tftp_single_port_session();

    printf("test_config_agent_net: %d checks passed\n", s_checks);
    return 0;
}
