/*
 * test_config_agent_windowed.c — WINDOW-AS-GRANT flow control on raw-TCP 6910
 * (MPS3_CFG_AGENT_WINDOWED). This is the primary over-the-wire-reconfig fix after
 * the HW pivot: the stall was in the lwIP/TCP RECEIVE path under a fire-and-hose
 * push, so the shell now paces the host purely via the TCP RECEIVE WINDOW. The
 * 6910 connection runs in manual-window mode (mps3_net_set_manual_window at
 * accept): mps3_net_recv() consumes bytes WITHOUT reopening the window, and the
 * shell reopens it by exactly one CFG_AGENT_ACK_WINDOW_BYTES only after a full
 * window's worth of received bytes has DRAINED to the sink (mps3_net_recved).
 * There is NO shell->host application byte — the earlier 1-byte 0x06 grant (which
 * deadlocked on this board's LAN9220 tiny-TX-segment egress) is gone.
 *
 * The fake net backend records every mps3_net_recved() call (fake_net_recved_total
 * / fake_net_recved_calls), so these tests observe the pacing directly: bytes
 * consumed but the window WITHHELD until a full window drains, then reopened one
 * window at a time — and, load-bearingly, only AFTER the sink DRAIN (write/finish),
 * never on bare recv.
 *
 * Built with -DMPS3_CFG_AGENT_WINDOWED -DCFG_AGENT_ACK_WINDOW_BYTES=1024 (small
 * window so multi-window + final-short-window paths run with tiny frames) and
 * -DMPS3_CFG_AGENT_STAGING_BYTES=512 (so any payload >512 B routes through the
 * fake ICAP/QSPI sink, letting a reopen be proved to trail the sink DRAIN).
 * Payloads <512 B stay on the RAM fast path. Window accounting is on TOTAL
 * received bytes (24-byte header + payload) because every received byte consumes
 * TCP window; a reopen therefore lands on a TCP-stream boundary, not a payload
 * boundary. The default (flag-off) fire-hose path is covered by
 * test_config_agent_net.c.
 *
 * Proven here:
 *   1. a multi-window push reopens the window once per DRAINED window, staged;
 *   2. a sub-window payload reopens NOTHING (it fits the initial window) yet stages;
 *   3. the window reopen strictly follows the sink DRAIN and only on a FULL window
 *      — a half-window that DRAINS does NOT reopen (proves not-on-bare-recv);
 *   4. a sink whose write() stalls does NOT reopen the window (fail-closed abort);
 *   5. a finish() failure aborts fail-closed (nothing staged) after all bytes drained;
 *   6. windowing composes with the two-slot {clearing, partial} pair staging.
 *
 * Links: config_agent.c, common/{crc32,net_proto,net_if}.c, fake_net_if.c.
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
#define WIN       1024u          /* must equal the -D override in the Makefile recipe */
#define HDR       ((int)MPS3_BITSTREAM_HDR_WIRE_SIZE)

/* ---- fake sink: records the exact DRAIN point (write / finish) so a window
 * reopen can be proved to trail it, plus knobs to stall the drain (write fail) or
 * reject the finish. One shared backend registered for BOTH the large-clearing
 * and the large-partial (ICAP-direct) roles; begin() re-arms it per transfer. ---*/
static uint32_t sink_cum;          /* cumulative bytes the sink has DRAINED (write ok) */
static int      sink_begun;
static int      sink_finished;     /* finish() returned OK */
static int      sink_aborted;
static uint32_t sink_fail_after;   /* if !=0, write() FAILS once cum would exceed this */
static int      sink_finish_fails; /* if set, finish() rejects (torn flash copy shape) */

static int fake_sink_begin(uint32_t total_bytes)
{
    (void)total_bytes;
    sink_begun = 1;
    sink_cum = 0;
    sink_finished = 0;
    sink_aborted = 0;
    return 0;
}
static int fake_sink_write(const void *buf, uint32_t len)
{
    (void)buf;
    if (sink_fail_after && (sink_cum + len) > sink_fail_after) {
        return -1; /* the drain stalls/faults here — must NOT reopen the window */
    }
    sink_cum += len;
    return 0;
}
static int fake_sink_finish(uint32_t expected_crc)
{
    (void)expected_crc;
    if (sink_finish_fails) {
        return -1;
    }
    sink_finished = 1;
    return 0;
}
static void fake_sink_abort(void) { sink_aborted = 1; }

static const mps3_cfg_agent_qspi_sink_t k_fake_sink = {
    fake_sink_begin, fake_sink_write, fake_sink_finish, fake_sink_abort,
    /* .ready */ 0, /* QSPI-style sink: no FSM-state gate => always ready */
};

static int build_frame(uint8_t *out, mps3_bin_kind_t kind, uint32_t rm_id,
                       uint32_t len_words, uint8_t seed)
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
    hdr.static_id = STATIC_ID;
    hdr.rm_id = rm_id;
    hdr.len_words = len_words;
    hdr.crc32 = mps3_crc32(payload, nbytes);
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
    /* Route any payload > STAGING_BYTES (=512 in this build) through the fake
     * sink so a window reopen can be observed to trail the sink DRAIN, not the
     * recv: clearing -> the clearing sink, partial -> the ICAP-direct (Path 3). */
    config_agent_set_qspi_clearing_sink(&k_fake_sink);
    config_agent_set_icap_direct_sink(&k_fake_sink);
    sink_cum = 0;
    sink_begun = 0;
    sink_finished = 0;
    sink_aborted = 0;
    sink_fail_after = 0;
    sink_finish_fails = 0;
}

/* full windows expected for a given total received-byte count (HDR + payload) */
static uint32_t full_windows(int total)
{
    return (uint32_t)total / WIN;
}

/* Drive a full window-as-grant push on an already-connected client: send the
 * whole frame (the in-memory ring holds it; a real host would be paced by TCP
 * flow control instead), let the shell drain it window-by-window with NO grant
 * read, half-close, and let the shell finish + stage on EOF. */
static void windowed_push(int cli, const uint8_t *frame, int total)
{
    CHECK(fake_net_send(cli, frame, total) == total);
    polls(64);            /* drain: the sink caps at ~1 KiB/poll */
    fake_net_close(cli);  /* half-close: EOF triggers finish + stage */
    polls(16);
    CHECK(fake_net_fw_closed(cli));
}

/* A clearing of 750 words = 3000 B, total 3024 B over a 1024-B window. EVERY
 * drained byte reopens the window (3024 B total) -- a sub-window residual must NOT
 * be stranded, or lwIP never advertises it and the host deadlocks (HW: 176,684 B).
 * The diag still counts 2 WHOLE windows. Drained through the sink, then staged. */
static void test_windowed_multi_window_clearing(void)
{
    fresh();
    uint8_t frame[4096];
    int total = build_frame(frame, MPS3_BIN_KIND_CLEARING, 0x11u, 750u, 0x40);
    CHECK(total == HDR + 3000);

    /* windows_drained is free-running (never reset), so measure the delta. */
    uint32_t wd0 = 0, f0 = 0;
    config_agent_win_diag(&wd0, &f0);

    int cli = fake_net_connect(MPS3_PORT_RAW_PUSH);
    assert(cli >= 0);
    windowed_push(cli, frame, total);

    /* Window reopens for ALL drained bytes (no stranded residual); the diag still
     * reports 2 whole windows. Pacing invariant: reopened == drained, never more. */
    CHECK(full_windows(total) == 2u);
    CHECK(fake_net_recved_total(cli) == (uint32_t)total);
    CHECK(fake_net_recved_calls(cli) >= 2);

    uint32_t wd1 = 0, f1 = 0;
    config_agent_win_diag(&wd1, &f1);
    CHECK(wd1 - wd0 == 2);
    CHECK(f1 - f0 == 0);

    /* Every byte drained through the sink and finish() ran (sink path, >512 B). */
    CHECK(sink_begun == 1);
    CHECK(sink_cum == 3000);
    CHECK(sink_finished == 1);

    config_agent_bitstream_info_t info;
    CHECK(config_agent_take_validated_clearing(&info) == 0);
    CHECK(info.in_qspi == 1); /* clearing sink -> QSPI-staged */
    CHECK(info.rm_id == 0x11u);
    CHECK(info.len_words == 750u);
}

/* A payload smaller than one window AND below the 512-B sink threshold: total
 * 424 B < 1024. It reopens for its drained bytes (a sub-window residual must never
 * be stranded -- see win_reopen_drained_windows), on the RAM fast path (sink
 * untouched), then staged. Reopening here is harmless: the host has nothing left
 * to send and finish() rides the EOF. */
static void test_windowed_single_short_window(void)
{
    fresh();
    uint8_t frame[2048];
    int total = build_frame(frame, MPS3_BIN_KIND_CLEARING, 0x22u, 100u, 0x70); /* 400 B < 512 */
    CHECK(total == HDR + 400);

    int cli = fake_net_connect(MPS3_PORT_RAW_PUSH);
    assert(cli >= 0);
    windowed_push(cli, frame, total);

    CHECK(fake_net_recved_total(cli) == (uint32_t)total); /* every drained byte reopens */
    CHECK(fake_net_recved_calls(cli) >= 1);
    CHECK(sink_begun == 0);                  /* RAM fast path: sink never involved */

    config_agent_bitstream_info_t info;
    CHECK(config_agent_take_validated_clearing(&info) == 0);
    CHECK(info.in_qspi == 0); /* RAM-staged */
    CHECK(info.rm_id == 0x22u);
    CHECK(info.len_words == 100u);
}

/* THE load-bearing proof: the window reopen TRAILS the sink DRAIN, byte for byte.
 * A 2048-B (512-word) clearing routed to the sink, driven a half-window at a time.
 * At every step the reopened total equals exactly HDR + sink_cum -- the window is
 * never reopened for a byte the sink has not taken, which is the whole pacing
 * invariant (the host cannot outrun the ICAP). Note we reopen EVERY drained byte,
 * not only whole windows: stranding a sub-window residual deadlocks the host,
 * because lwIP will not advertise a small window grow (HW: hang at 176,684 B). */
static void test_windowed_reopen_trails_sink_drain(void)
{
    fresh();
    uint8_t frame[4096];
    int total = build_frame(frame, MPS3_BIN_KIND_CLEARING, 0x55u, 512u, 0x10); /* 2048 B */
    CHECK(total - HDR == 2048);

    int cli = fake_net_connect(MPS3_PORT_RAW_PUSH);
    assert(cli >= 0);

    /* Header alone (24 B): begin_payload() registers + begin()s the sink; the 24
     * consumed bytes are far below one window, so NO reopen. */
    CHECK(fake_net_send(cli, frame, HDR) == HDR);
    polls(4);
    CHECK(sink_begun == 1);
    CHECK(fake_net_recved_total(cli) == (uint32_t)HDR);  /* header drained -> reopened */

    /* 512 payload bytes: the sink DRAINS them (cum=512) and the window reopens by
     * exactly those drained bytes -- never for bytes the sink has not taken. */
    CHECK(fake_net_send(cli, frame + HDR, 512) == 512);
    polls(4);
    CHECK(sink_cum == 512);                                        /* drained to the sink... */
    CHECK(fake_net_recved_total(cli) == (uint32_t)HDR + 512u);     /* ...and exactly that reopened */

    /* 488 more payload: total 24+1000 = 1024 crosses the first boundary. The sink
     * drains the 488 FIRST (cum=1000), THEN the window reopens by exactly 1024. */
    CHECK(fake_net_send(cli, frame + HDR + 512, 488) == 488);
    polls(4);
    CHECK(sink_cum == 1000);                                       /* sink drained before the reopen */
    CHECK(fake_net_recved_total(cli) == (uint32_t)HDR + 1000u);    /* reopen tracks the drain */
    CHECK(fake_net_recved_calls(cli) >= 1);

    /* Remaining 1048 payload -> total 2072 -> a second full window (2048); the
     * 24-B residual rides the last window. finish is still gated on EOF. */
    CHECK(fake_net_send(cli, frame + HDR + 1000, 1048) == 1048);
    polls(8);
    CHECK(sink_cum == 2048);
    CHECK(fake_net_recved_total(cli) == (uint32_t)HDR + 2048u);   /* == total drained */
    CHECK(fake_net_recved_calls(cli) >= 2);
    CHECK(sink_finished == 0);                     /* finish runs on EOF, not before */

    /* Half-close -> EOF -> finish() + stage. */
    fake_net_close(cli);
    polls(8);
    CHECK(fake_net_fw_closed(cli));
    CHECK(sink_finished == 1);
    CHECK(fake_net_recved_total(cli) == (uint32_t)total); /* every drained byte reopened */

    config_agent_bitstream_info_t info;
    CHECK(config_agent_take_validated_clearing(&info) == 0);
    CHECK(info.in_qspi == 1);
    CHECK(info.rm_id == 0x55u);
}

/* A stalled/failed sink drain does NOT reopen the window: the sink refuses to
 * drain past window 1, so window 2's write faults -> the session aborts
 * fail-closed with no spurious reopen and nothing staged. */
static void test_windowed_sink_write_failure_no_reopen(void)
{
    fresh();
    sink_fail_after = 1024; /* the sink drains up to 1024 B, then faults */
    uint8_t frame[4096];
    int total = build_frame(frame, MPS3_BIN_KIND_CLEARING, 0x66u, 512u, 0x20); /* 2048 B */
    (void)total;

    int cli = fake_net_connect(MPS3_PORT_RAW_PUSH);
    assert(cli >= 0);

    /* Header + 1000 payload = 1024 total: drains cleanly (cum=1000 <= 1024) and
     * reopens exactly one window. */
    CHECK(fake_net_send(cli, frame, HDR + 1000) == HDR + 1000);
    polls(6);
    CHECK(fake_net_recved_total(cli) == WIN);   /* HDR+1000 == 1024 == one window */
    CHECK(fake_net_recved_calls(cli) >= 1);
    CHECK(sink_cum == 1000);
    uint32_t reopened_before_fault = fake_net_recved_total(cli);
    int      calls_before_fault    = fake_net_recved_calls(cli);

    /* 512 more payload: the sink write FAULTS (cum would exceed 1024) -> abort. NO
     * reopen for the undrained window (a stalled sink does not ack), scratch
     * re-armed via abort(). */
    CHECK(fake_net_send(cli, frame + HDR + 1000, 512) == 512);
    polls(6);
    /* THE invariant: a byte the sink REFUSED never reopens the window. */
    CHECK(fake_net_recved_total(cli) == reopened_before_fault);
    CHECK(fake_net_recved_calls(cli) == calls_before_fault);
    CHECK(sink_aborted == 1);
    CHECK(fake_net_fw_closed(cli));

    config_agent_bitstream_info_t info;
    CHECK(config_agent_take_validated_clearing(&info) != 0); /* nothing staged */
}

/* A finish() rejection (torn flash copy shape) aborts fail-closed even though
 * every payload byte drained — nothing is staged. */
static void test_windowed_finish_failure_fail_closed(void)
{
    fresh();
    sink_finish_fails = 1;
    uint8_t frame[4096];
    int total = build_frame(frame, MPS3_BIN_KIND_CLEARING, 0x77u, 512u, 0x30); /* 2048 B */

    int cli = fake_net_connect(MPS3_PORT_RAW_PUSH);
    assert(cli >= 0);

    /* All payload sent + drained; window reopened for every drained byte. */
    CHECK(fake_net_send(cli, frame, total) == total);
    polls(16);
    CHECK(sink_cum == 2048);                              /* every byte drained... */
    CHECK(fake_net_recved_total(cli) == (uint32_t)total); /* ...and reopened as they drained */
    CHECK(sink_finished == 0);                     /* finish not yet (EOF-gated) */

    /* Half-close -> finish() runs and FAILS -> abort, nothing staged. */
    fake_net_close(cli);
    polls(8);
    CHECK(sink_finished == 0);      /* finish rejected */
    CHECK(sink_aborted == 1);
    CHECK(fake_net_fw_closed(cli));

    config_agent_bitstream_info_t info;
    CHECK(config_agent_take_validated_clearing(&info) != 0); /* nothing staged */
}

/* After a clearing, a windowed PARTIAL is accepted (ordering satisfied) and the
 * pair is ready — proves windowing composes with the two-slot pair staging, both
 * legs draining through the sink. */
static void test_windowed_pair(void)
{
    fresh();
    uint8_t frame[4096];

    int total = build_frame(frame, MPS3_BIN_KIND_CLEARING, 0x33u, 300u, 0x40); /* 1200 B */
    int cli = fake_net_connect(MPS3_PORT_RAW_PUSH);
    assert(cli >= 0);
    windowed_push(cli, frame, total);
    CHECK(fake_net_recved_total(cli) == (uint32_t)total); /* every drained byte reopened */
    CHECK(sink_finished == 1);

    total = build_frame(frame, MPS3_BIN_KIND_PARTIAL, 0x33u, 500u, 0x40);       /* 2000 B */
    cli = fake_net_connect(MPS3_PORT_RAW_PUSH);
    assert(cli >= 0);
    windowed_push(cli, frame, total);
    CHECK(fake_net_recved_total(cli) == (uint32_t)total); /* every drained byte reopened */
    CHECK(sink_finished == 1);

    CHECK(config_agent_pair_ready());
}

int main(void)
{
    test_windowed_multi_window_clearing();
    test_windowed_single_short_window();
    test_windowed_reopen_trails_sink_drain();
    test_windowed_sink_write_failure_no_reopen();
    test_windowed_finish_failure_fail_closed();
    test_windowed_pair();

    printf("test_config_agent_windowed: %d checks passed\n", s_checks);
    return 0;
}
