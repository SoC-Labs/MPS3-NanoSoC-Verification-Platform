/*
 * test_config_agent_slot.c -- the v0.14 slot-image push kind (header kind 2,
 * MPS3_BIN_KIND_SLOT_IMAGE) through config_agent.c's REAL receive sessions,
 * 6910 and TFTP, over fake_net_if.c (net-protocol.md "Slot images";
 * config_agent.h "SLOT-IMAGE pushes").
 *
 * ONE source, TWO binaries:
 *   test_config_agent_slot            no provider linked: the bare-metal image.
 *                                     A kind-2 push must be refused EXACTLY as an
 *                                     unknown kind always was (TCP close, TFTP
 *                                     ERROR), and must leave the bitstream path's
 *                                     staging and pair flag untouched.
 *   test_config_agent_slot_provider   -DTEST_SLOT_PROVIDER: a strong
 *                                     mps3_cfg_slot_sink() with a recording sink.
 *                                     The bytes reach it verbatim, finish() gets
 *                                     the header CRC, every tear reaches abort(),
 *                                     and the static_id belt holds even when the
 *                                     provider accepts a foreign image.
 *
 * Links: config_agent.c, common/net_proto.c, common/crc32.c, common/net_if.c,
 * fake_net_if.c -- the same link set as test_config_agent_net.
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

#define STATIC_ID  0xA1B2C3D4u
#define CLIENT_TID 5002

/* ---- the recording provider (strong build only) ---------------------------- */
#ifdef TEST_SLOT_PROVIDER
static struct {
    int      provider_calls;
    mps3_bitstream_hdr_t hdr;      /* what the provider was shown */
    int      refuse;               /* provider returns NULL        */
    int      begins, finishes, aborts;
    uint32_t begin_total;
    uint32_t finish_crc;
    int      fail_write_at;        /* write() fails once got >= this (0 = never) */
    int      fail_finish;
    uint8_t  got[16384];
    uint32_t got_len;
    uint32_t max_write;            /* the largest single write() */
} s_rec;

static int rec_begin(uint32_t total)
{
    s_rec.begins++;
    s_rec.begin_total = total;
    s_rec.got_len = 0;
    return 0;
}
static int rec_write(const void *buf, uint32_t len)
{
    if (s_rec.fail_write_at && s_rec.got_len >= (uint32_t)s_rec.fail_write_at) {
        return -1;
    }
    assert(s_rec.got_len + len <= sizeof(s_rec.got));
    memcpy(&s_rec.got[s_rec.got_len], buf, len);
    s_rec.got_len += len;
    if (len > s_rec.max_write) {
        s_rec.max_write = len;
    }
    return 0;
}
static int rec_finish(uint32_t crc)
{
    s_rec.finishes++;
    s_rec.finish_crc = crc;
    return s_rec.fail_finish ? -1 : 0;
}
static void rec_abort(void)
{
    s_rec.aborts++;
}
static const mps3_cfg_agent_qspi_sink_t s_rec_sink = {
    .begin = rec_begin, .write = rec_write, .finish = rec_finish, .abort = rec_abort,
};

const mps3_cfg_agent_qspi_sink_t *mps3_cfg_slot_sink(const mps3_bitstream_hdr_t *hdr)
{
    s_rec.provider_calls++;
    s_rec.hdr = *hdr;
    return s_rec.refuse ? 0 : &s_rec_sink;
}

static void rec_reset(void)
{
    memset(&s_rec, 0, sizeof(s_rec));
}

/* THE LOCK's hook (config_agent.h), as mps3-harnessd supplies it: here it
 * refuses on demand and records what config_agent told it about the peer. */
static struct { int refuse, calls, known; mps3_net_addr_t peer; } s_lock;
int mps3_cfg_slot_refuse_peer(const mps3_net_addr_t *peer, int known)
{
    s_lock.calls++;
    s_lock.known = known;
    s_lock.peer = *peer;
    return s_lock.refuse;
}
#endif

/* ---- helpers --------------------------------------------------------------- */

static int build_frame(uint8_t *out, uint8_t kind, uint8_t rm_slot, uint32_t static_id,
                       uint32_t len_words, uint8_t seed, int corrupt_crc)
{
    uint32_t nbytes = len_words * 4u;
    uint8_t *payload = out + MPS3_BITSTREAM_HDR_WIRE_SIZE;
    for (uint32_t i = 0; i < nbytes; i++) {
        payload[i] = (uint8_t)(seed + i * 7u);
    }
    mps3_bitstream_hdr_t hdr;
    memset(&hdr, 0, sizeof(hdr));
    memcpy(hdr.magic, MPS3_BITSTREAM_MAGIC, 4);
    hdr.ver = MPS3_BITSTREAM_VER;
    hdr.kind = kind;
    hdr.rm_slot = rm_slot;
    hdr.static_id = static_id;
    hdr.rm_id = (kind == MPS3_BIN_KIND_SLOT_IMAGE) ? 0u : 7u;
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
#ifdef TEST_SLOT_PROVIDER
    rec_reset();
#endif
}

static void raw_push(const uint8_t *frame, int len)
{
    int cli = fake_net_connect(MPS3_PORT_RAW_PUSH);
    assert(cli >= 0);
    CHECK(fake_net_send(cli, frame, len) == len);
    fake_net_close(cli);
    polls(64);
    CHECK(fake_net_fw_closed(cli));   /* accept or reject, 6910 always ends in close */
}

/* TFTP PUT (WRQ + DATA blocks). 0 = final ACK, -1 = an ERROR at any point
 * (its code in s_tftp_err, its text in s_tftp_msg). */
static int  s_tftp_err;
static char s_tftp_msg[64];
static int tftp_put(const uint8_t *data, int len)
{
    uint8_t pkt[600], rsp[600];
    uint16_t fw_port = 0;
    int wl = 0;
    pkt[wl++] = 0; pkt[wl++] = 2;
    memcpy(&pkt[wl], "linux_slot.img", 15); wl += 15;
    memcpy(&pkt[wl], "octet", 6); wl += 6;
    if (fake_net_udp_inject(MPS3_PORT_TFTP, CLIENT_TID, pkt, wl) != 0) {
        return -1;
    }
    polls(4);
    int rn = fake_net_udp_take_sent(&fw_port, NULL, rsp, sizeof(rsp));
    if (rn < 4 || rsp[1] == 5) {
        return -1;
    }
    uint16_t tid = fw_port;
    int off = 0;
    uint16_t block = 1;
    for (;;) {
        int chunk = len - off;
        if (chunk > 512) chunk = 512;
        pkt[0] = 0; pkt[1] = 3;
        pkt[2] = (uint8_t)(block >> 8); pkt[3] = (uint8_t)block;
        memcpy(&pkt[4], data + off, (size_t)chunk);
        if (fake_net_udp_inject(tid, CLIENT_TID, pkt, 4 + chunk) != 0) {
            return -1;
        }
        polls(4);
        rn = fake_net_udp_take_sent(NULL, NULL, rsp, sizeof(rsp));
        if (rn >= 5 && rsp[1] == 5) {
            s_tftp_err = (rsp[2] << 8) | rsp[3];
            snprintf(s_tftp_msg, sizeof(s_tftp_msg), "%.*s", rn - 4, (const char *)&rsp[4]);
            return -1;
        }
        if (rn < 4) {
            return -1;
        }
        off += chunk;
        if (chunk < 512) {
            return 0;
        }
        block++;
    }
}

/* The bitstream path must be exactly as it was around a slot push: a clearing
 * staged BEFORE it still pairs with a partial AFTER it. */
static void stage_clearing(void)
{
    static uint8_t f[512];
    int n = build_frame(f, MPS3_BIN_KIND_CLEARING, 0, STATIC_ID, 16, 0x10, 0);
    raw_push(f, n);
}
static void stage_partial_and_check_pair(void)
{
    static uint8_t f[512];
    int n = build_frame(f, MPS3_BIN_KIND_PARTIAL, 0, STATIC_ID, 12, 0x40, 0);
    raw_push(f, n);
    CHECK(config_agent_pair_ready());   /* the ordering flag survived the slot push */
}

/* ---- the bare-metal image: refused as an unknown kind ------------------------ */
#ifndef TEST_SLOT_PROVIDER
static void test_baremetal_refuses_a_slot_push(void)
{
    static uint8_t f[4096];
    fresh();
    stage_clearing();
    int n = build_frame(f, MPS3_BIN_KIND_SLOT_IMAGE, 0, STATIC_ID, 256, 0x33, 0);
    raw_push(f, n);                        /* closed, nothing staged */
    uint32_t got = 1, expect = 1;
    config_agent_rx_progress(&got, &expect);
    CHECK(got == 0);                       /* no payload byte was consumed as a payload */
    CHECK(!config_agent_pair_ready());
    stage_partial_and_check_pair();

    fresh();
    CHECK(tftp_put(f, n) == -1);           /* TFTP: an ERROR, as for any refused kind */

    /* The bitstream validator itself is UNCHANGED: kind 2 is still ERR_KIND there. */
    mps3_bitstream_hdr_t h;
    memset(&h, 0, sizeof(h));
    memcpy(h.magic, MPS3_BITSTREAM_MAGIC, 4);
    h.ver = MPS3_BITSTREAM_VER;
    h.kind = MPS3_BIN_KIND_SLOT_IMAGE;
    h.static_id = STATIC_ID;
    h.len_words = 4;
    CHECK(config_agent_validate_header_ex(&h, STATIC_ID, 1) == CFG_AGENT_ERR_KIND);
}
#endif

/* ---- the engine with a provider ----------------------------------------------- */
#ifdef TEST_SLOT_PROVIDER
static void test_tcp_slot_push_reaches_the_sink_verbatim(void)
{
    static uint8_t f[16384 + 64];
    fresh();
    stage_clearing();
    int n = build_frame(f, MPS3_BIN_KIND_SLOT_IMAGE, 2, STATIC_ID, 3000, 0x5A, 0);
    raw_push(f, n);
    CHECK(s_rec.provider_calls == 1);
    CHECK(s_rec.hdr.kind == MPS3_BIN_KIND_SLOT_IMAGE && s_rec.hdr.rm_slot == 2);
    CHECK(s_rec.begins == 1 && s_rec.begin_total == 12000u);
    CHECK(s_rec.got_len == 12000u);
    CHECK(memcmp(s_rec.got, f + MPS3_BITSTREAM_HDR_WIRE_SIZE, 12000u) == 0);
    CHECK(s_rec.finishes == 1 && s_rec.aborts == 0);
    CHECK(s_rec.finish_crc == mps3_crc32(f + MPS3_BITSTREAM_HDR_WIRE_SIZE, 12000u));
    /* The page-cache sink is exempt from the slow-sink 1 KiB/poll throttle. */
    CHECK(s_rec.max_write > 0u);
    /* nothing staged, the pair flag untouched */
    config_agent_bitstream_info_t info;
    CHECK(config_agent_take_validated_partial(&info) != 0);
    stage_partial_and_check_pair();
}

static void test_one_poll_moves_more_than_the_slow_sink_cap(void)
{
    static uint8_t f[16384 + 64];
    fresh();
    int n = build_frame(f, MPS3_BIN_KIND_SLOT_IMAGE, 0, STATIC_ID, 3000, 0x11, 0);
    int cli = fake_net_connect(MPS3_PORT_RAW_PUSH);
    CHECK(fake_net_send(cli, f, n) == n);
    polls(1);                      /* accept + header + the first payload burst */
    CHECK(s_rec.got_len > 1024u);  /* a QSPI/ICAP sink would have stopped at 1 KiB */
    fake_net_close(cli);
    polls(64);
    CHECK(s_rec.finishes == 1);
}

static void test_tftp_slot_push(void)
{
    static uint8_t f[4096 + 64];
    fresh();
    int n = build_frame(f, MPS3_BIN_KIND_SLOT_IMAGE, 0, STATIC_ID, 700, 0x21, 0);
    CHECK(tftp_put(f, n) == 0);    /* final ACK: bytes intact + the sink accepted */
    CHECK(s_rec.finishes == 1 && s_rec.got_len == 2800u);
    CHECK(memcmp(s_rec.got, f + MPS3_BITSTREAM_HDR_WIRE_SIZE, 2800u) == 0);

    fresh();
    s_rec.fail_finish = 1;         /* the engine refuses the image at finish */
    CHECK(tftp_put(f, n) == -1);
    CHECK(s_rec.finishes == 1);
}

static void test_static_id_belt_holds_even_if_the_provider_accepts(void)
{
    static uint8_t f[4096 + 64];
    fresh();
    int n = build_frame(f, MPS3_BIN_KIND_SLOT_IMAGE, 0, 0x0BADCAFEu, 256, 0x33, 0);
    raw_push(f, n);
    CHECK(s_rec.provider_calls == 1);    /* the provider saw it (and would record why) */
    CHECK(s_rec.begins == 0 && s_rec.got_len == 0);   /* ...but not one byte arrived */
    CHECK(s_rec.aborts == 0);            /* begin never ran: nothing is owed */
}

static void test_refusals_and_tears(void)
{
    static uint8_t f[4096 + 64];

    /* the provider refuses: closed, no begin */
    fresh();
    s_rec.refuse = 1;
    int n = build_frame(f, MPS3_BIN_KIND_SLOT_IMAGE, 1, STATIC_ID, 256, 0x33, 0);
    raw_push(f, n);
    CHECK(s_rec.provider_calls == 1 && s_rec.begins == 0);

    /* a transport CRC mismatch: finish never runs, abort does */
    fresh();
    n = build_frame(f, MPS3_BIN_KIND_SLOT_IMAGE, 0, STATIC_ID, 256, 0x33, 1);
    raw_push(f, n);
    CHECK(s_rec.begins == 1 && s_rec.finishes == 0 && s_rec.aborts == 1);

    /* torn mid-payload: abort */
    fresh();
    n = build_frame(f, MPS3_BIN_KIND_SLOT_IMAGE, 0, STATIC_ID, 256, 0x33, 0);
    raw_push(f, n - 100);
    CHECK(s_rec.begins == 1 && s_rec.finishes == 0 && s_rec.aborts == 1);

    /* a sink write failure (card fault): abort, and the next push is served */
    fresh();
    s_rec.fail_write_at = 512;
    raw_push(f, n);
    CHECK(s_rec.finishes == 0 && s_rec.aborts == 1);
    s_rec.fail_write_at = 0;
    raw_push(f, n);
    CHECK(s_rec.finishes == 1);

    /* framing refusals happen BEFORE the provider: empty, oversized, bad ver */
    fresh();
    n = build_frame(f, MPS3_BIN_KIND_SLOT_IMAGE, 0, STATIC_ID, 0, 0x33, 0);
    raw_push(f, n);
    CHECK(s_rec.provider_calls == 0);
    {
        mps3_bitstream_hdr_t h;
        memset(&h, 0, sizeof(h));
        memcpy(h.magic, MPS3_BITSTREAM_MAGIC, 4);
        h.ver = MPS3_BITSTREAM_VER;
        h.kind = MPS3_BIN_KIND_SLOT_IMAGE;
        h.static_id = STATIC_ID;
        h.len_words = MPS3_SLOT_IMAGE_MAX_BYTES / 4u + 1u;   /* 64 MiB + 4 */
        uint8_t hdr[MPS3_BITSTREAM_HDR_WIRE_SIZE];
        mps3_bitstream_hdr_pack(&h, hdr);
        raw_push(hdr, (int)sizeof(hdr));
        CHECK(s_rec.provider_calls == 0);
        h.len_words = MPS3_SLOT_IMAGE_MAX_BYTES / 4u;        /* exactly 64 MiB: offered */
        h.ver = 2;                                           /* ...but a bad version  */
        mps3_bitstream_hdr_pack(&h, hdr);
        raw_push(hdr, (int)sizeof(hdr));
        CHECK(s_rec.provider_calls == 0);
        h.ver = MPS3_BITSTREAM_VER;
        mps3_bitstream_hdr_pack(&h, hdr);
        raw_push(hdr, (int)sizeof(hdr));                     /* torn after the header */
        CHECK(s_rec.provider_calls == 1 && s_rec.aborts == 1);
    }
}
#endif

#ifdef TEST_SLOT_PROVIDER
/* THE LOCK (v0.14): asked before the provider, with the peer; a refusal closes
 * 6910 and answers TFTP with ERROR 2 "access violation" -- and the provider, the
 * sink and the bitstream staging never hear of the push. */
static void test_the_lock_refuses_before_the_provider(void)
{
    static uint8_t f[4096 + 64];
    int n = build_frame(f, MPS3_BIN_KIND_SLOT_IMAGE, 0, STATIC_ID, 256, 0x33, 0);

    fresh();
    memset(&s_lock, 0, sizeof(s_lock));
    s_lock.refuse = 1;
    raw_push(f, n);
    CHECK(s_lock.calls == 1 && s_rec.provider_calls == 0 && s_rec.begins == 0);
    CHECK(s_lock.known == 0);            /* the fake backend cannot name a TCP peer:
                                          * "unknown", which the engine treats as remote */
    fresh();
    memset(&s_lock, 0, sizeof(s_lock));
    s_lock.refuse = 1;
    s_tftp_err = -1;
    CHECK(tftp_put(f, n) == -1);
    CHECK(s_tftp_err == 2 && strcmp(s_tftp_msg, "access violation") == 0);
    CHECK(s_lock.known == 1 && s_lock.peer.port == CLIENT_TID);   /* the TFTP client */
    CHECK(s_rec.provider_calls == 0);

    /* negative control: the same pushes, not refused, reach the sink */
    fresh();
    memset(&s_lock, 0, sizeof(s_lock));
    CHECK(tftp_put(f, n) == 0 && s_rec.finishes == 1);
    raw_push(f, n);
    CHECK(s_rec.finishes == 2 && s_lock.calls == 2);

    /* a bitstream push never asks the lock */
    fresh();
    memset(&s_lock, 0, sizeof(s_lock));
    s_lock.refuse = 1;
    stage_clearing();
    stage_partial_and_check_pair();
    CHECK(s_lock.calls == 0);
}
#endif

int main(void)
{
#ifdef TEST_SLOT_PROVIDER
    test_tcp_slot_push_reaches_the_sink_verbatim();
    test_one_poll_moves_more_than_the_slow_sink_cap();
    test_tftp_slot_push();
    test_static_id_belt_holds_even_if_the_provider_accepts();
    test_refusals_and_tears();
    test_the_lock_refuses_before_the_provider();
    printf("test_config_agent_slot_provider: ALL PASS (%d checks)\n", s_checks);
#else
    test_baremetal_refuses_a_slot_push();
    printf("test_config_agent_slot: ALL PASS (%d checks)\n", s_checks);
#endif
    return 0;
}
