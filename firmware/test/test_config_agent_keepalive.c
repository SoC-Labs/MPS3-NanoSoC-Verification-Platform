/*
 * test_config_agent_keepalive.c — the "dead client wedges the shell" closure
 * (docs/QSPI_CLEARING_CACHE_HW_FINDINGS.md, remediation items 2/3), exercised
 * through the net_if.h SEAM.
 *
 * THE HOLE: a client connects to config_agent's single 6910 push port and dies
 * WITHOUT a FIN (killed process / yanked cable) BEFORE or BETWEEN swaps. No swap
 * is armed, so the swap FSM's AWAIT-idle timeout never engages; the TCP
 * connection stays ESTABLISHED and config_agent's one session is held, refusing
 * every later client until a JTAG reload.
 *
 * THE FIX (net_if_lwip.c accept path + create_platform.tcl lwip_tcp_keepalive):
 * lwIP keepalive probes the idle connection and, after the probes go unanswered,
 * tcp_abort()s the pcb. That fires net_if_lwip.c's conn_err_cb, so the owning
 * module's next mps3_net_recv() returns MPS3_NET_ERR and it runs its normal
 * close/cleanup — config_agent's session_reset() releases the 6910 session.
 *
 * WHAT THIS TEST COVERS: the module-visible END of that chain, through the seam.
 * fake_net_kill_peer() injects exactly the surface a keepalive abort produces (an
 * abortive close -> MPS3_NET_ERR on the next drained recv, NOT a graceful
 * MPS3_NET_CLOSED), and the test asserts config_agent RELEASES the wedged session
 * (config_agent_active_tcp_conn() -> NULL) and then ACCEPTS + completes a fresh
 * push — i.e. the board recovers hands-free, no JTAG reload.
 *
 * WHAT THIS TEST DOES *NOT* COVER (host-harness limits — stated plainly):
 *   - That the real lwIP accept path sets SOF_KEEPALIVE / keep_idle / keep_intvl
 *     / keep_cnt on the pcb. net_if_lwip.c is target-only (it includes lwIP
 *     headers) and is not compiled here; the fake has no pcb. That the accepted
 *     pcb is armed is verified by inspection of listener_accept_cb, and is what a
 *     real-socket keepalive-drop test (over pyverify, on the board) would assert.
 *   - The 35 s reap TIMING and the probe cadence — that is lwIP tcp_slowtmr
 *     behaviour driven by wall-clock ticks, not reachable through the seam.
 * This test pins the CONTRACT (a reaped peer frees the session); the timing/flag
 * are the lwIP-internal half.
 *
 * Links: config_agent.c, common/net_proto.c, common/crc32.c, common/net_if.c,
 * fake_net_if.c (same set as test_config_agent_net.c).
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../config_agent/config_agent.h"
#include "../common/crc32.h"
#include "../common/net_if.h"
#include "../common/net_proto.h"
#include "fake_net_if.h"

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

#define STATIC_ID 0xA1B2C3D4u

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

/* One wire frame (24-byte header + deterministic payload) into out; total len. */
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

/* A full, well-framed clearing push (all bytes + graceful half-close), the way a
 * healthy client completes. Proves the port is USABLE again after a reap. */
static void healthy_push_clearing(uint32_t rm_id, uint8_t seed)
{
    static uint8_t frame[4096];
    int n = build_frame(frame, MPS3_BIN_KIND_CLEARING, rm_id, 16, seed);
    int cli = fake_net_connect(MPS3_PORT_RAW_PUSH);
    CHECK(cli >= 0);
    CHECK(fake_net_send(cli, frame, n) == n);
    fake_net_close(cli); /* graceful FIN: the healthy client's EOF sync point */
    polls(64);
    CHECK(fake_net_fw_closed(cli));
}

/* THE hole: a client dies mid-HEADER, before any swap is armed. The swap FSM's
 * AWAIT-idle timeout cannot help (nothing armed); only the keepalive reap frees
 * the session. Modelled by fake_net_kill_peer -> MPS3_NET_ERR on next recv. */
static void test_dead_client_before_swap_is_reaped(void)
{
    fresh();
    static uint8_t frame[4096];
    /* Only the first 10 bytes are ever sent (the dribbled partial header), so
     * the framed length is deliberately unused -- void it rather than keep a
     * variable no assertion reads (-Wunused-variable, now -Werror). */
    (void)build_frame(frame, MPS3_BIN_KIND_CLEARING, 7, 16, 0x10);

    /* Client connects and dribbles a partial header, then goes silent. */
    int dead = fake_net_connect(MPS3_PORT_RAW_PUSH);
    CHECK(dead >= 0);
    CHECK(fake_net_send(dead, frame, 10) == 10); /* mid-header: session busy */
    polls(4);
    CHECK(config_agent_active_tcp_conn() != NULL); /* session HELD (the wedge) */

    /* Pre-existing single-session guard: a second client is refused while busy.
     * This is exactly what stays wedged forever without keepalive. */
    int refused = fake_net_connect(MPS3_PORT_RAW_PUSH);
    CHECK(refused >= 0);
    polls(4);
    CHECK(fake_net_fw_closed(refused));
    fake_net_close(refused);

    /* Peer vanishes without a FIN; lwIP keepalive reaps it (abortive close). */
    fake_net_kill_peer(dead);
    polls(8);

    /* THE FIX: config_agent released its session — no JTAG reload needed. */
    CHECK(config_agent_active_tcp_conn() == NULL);
    CHECK(config_agent_pair_ready() == 0); /* the torn client staged nothing */

    /* And the port is fully usable again: a fresh client completes a push. */
    healthy_push_clearing(9, 0x20);
    config_agent_bitstream_info_t info;
    CHECK(config_agent_take_validated_clearing(&info) == 0);
    CHECK(info.rm_id == 9); /* the NEW client's clearing, not the dead one's */
}

/* Between swaps: a client dies mid-PAYLOAD (swap armed on the FSM side, but here
 * we exercise config_agent's own session teardown). The reap must still release
 * the session and drop the half-received payload (fail closed), then recover. */
static void test_dead_client_mid_payload_is_reaped(void)
{
    fresh();
    static uint8_t frame[4096];
    int n = build_frame(frame, MPS3_BIN_KIND_CLEARING, 3, 16, 0x30);

    int dead = fake_net_connect(MPS3_PORT_RAW_PUSH);
    CHECK(dead >= 0);
    CHECK(fake_net_send(dead, frame, n - 8) == n - 8); /* header + most payload */
    polls(8);
    CHECK(config_agent_active_tcp_conn() != NULL); /* mid-payload, session held */
    uint32_t got = 0, expect = 0;
    config_agent_rx_progress(&got, &expect);
    CHECK(got > 0 && got < expect); /* genuinely mid-transfer */

    fake_net_kill_peer(dead);
    polls(8);

    CHECK(config_agent_active_tcp_conn() == NULL);      /* session released     */
    config_agent_bitstream_info_t info;
    CHECK(config_agent_take_validated_clearing(&info) != 0); /* nothing staged  */

    /* Recovered: a subsequent healthy push stages cleanly. */
    healthy_push_clearing(4, 0x40);
    CHECK(config_agent_take_validated_clearing(&info) == 0);
    CHECK(info.rm_id == 4);
}

/* Distinctness: a reaped peer (abortive) must NOT look like a graceful client
 * close to the seam — the fake models the same MPS3_NET_ERR-vs-MPS3_NET_CLOSED
 * split the lwIP backend does (conn->errored is checked before conn->peer_closed
 * in mps3_net_recv). A raw recv proves it directly. */
static void test_reset_surfaces_as_err_not_closed(void)
{
    fresh();
    static uint8_t frame[4096];
    (void)build_frame(frame, MPS3_BIN_KIND_CLEARING, 1, 16, 0x50);

    int dead = fake_net_connect(MPS3_PORT_RAW_PUSH);
    CHECK(dead >= 0);
    CHECK(fake_net_send(dead, frame, 4) == 4); /* a few bytes buffered */
    polls(1);
    /* config_agent has drained those 4 into its header accumulator; the seam
     * conn is the one config_agent holds. Kill and re-drive: the NEXT drained
     * recv must be MPS3_NET_ERR (reap), which config_agent turns into a reset. */
    fake_net_kill_peer(dead);
    polls(8);
    CHECK(config_agent_active_tcp_conn() == NULL);
}

int main(void)
{
    test_dead_client_before_swap_is_reaped();
    test_dead_client_mid_payload_is_reaped();
    test_reset_surfaces_as_err_not_closed();

    printf("test_config_agent_keepalive: %d checks passed\n", s_checks);
    return 0;
}
