/*
 * test_swap_icap_defer.c — the clearing->partial HANDOFF-RACE regression.
 *
 * THE BUG (silicon, 2026-08, "swap-AWAY from a DAP RM fails"): the ICAP-direct
 * partial sink's begin() rejects unless the swap FSM is already in
 * SWAP_AWAIT_PARTIAL. The FSM only reaches AWAIT_PARTIAL after the OUTGOING RM's
 * clearing has fully streamed to the ICAP. nanosoc/multicore have the two largest
 * clearings (215,864 / 267,732 B), so the FSM is still in SWAP_STREAM_CLEARING
 * when the host pusher — which opens the partial connection right after the
 * clearing connection closes — delivers the partial header. The OLD firmware then
 * HARD-REJECTED the partial (config_agent tcp_abort), the partial was dropped, and
 * the swap timed out in AWAIT_PARTIAL. Small-clearing RMs (led/greybox: ~78-80 KB)
 * win the race and never hit it — which is exactly why every non-DAP swap worked.
 *
 * WHY IT WAS NEVER CAUGHT: test_swap_icap_direct.c does pump_until(AWAIT_PARTIAL)
 * BEFORE pushing the partial, so it only ever exercises the partial arriving with
 * the FSM already parked and ready — the happy ordering, never the race.
 *
 * THE FIX (config_agent.c + the sink ready() hook): when the ICAP-direct sink is
 * not ready, config_agent DEFERS the partial (RECV_PENDING_ICAP_BEGIN) — it holds
 * the header, pulls no payload, leaves the TCP window closed so the host
 * backpressures, and re-arms once ready() flips true — instead of tearing the
 * session. This test drives the partial in EARLY (FSM in AWAIT_INCOMING_CLEARING,
 * a proxy for any pre-AWAIT_PARTIAL state) and proves: the session is HELD, not
 * torn (fw did not close it, nothing streamed to ICAP, the defer counter climbs),
 * and once the FSM advances the held partial arms, streams, and the swap reaches
 * SWAP_DONE/verified. Against the OLD firmware step 3 fails (fw closes the torn
 * session) and step 4 never completes.
 *
 * Link set: the ICAP_DIRECT_SRCS from test_swap_icap_direct (real config_agent +
 * real swap_fsm), -DMPS3_CFG_AGENT_STAGING_BYTES=4096 so "large" means ">4 KiB".
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../coordinator/swap_fsm.h"
#include "../coordinator/coordinator.h"
#include "../config_agent/config_agent.h"
#include "../common/platform_regs.h"
#include "../common/crc32.h"
#include "../common/net_if.h"
#include "mock_regs.h"
#include "fake_net_if.h"
#include "fake_overlay_store.h"

mps3_shell_state_t g_shell_state; /* swap_fsm.c's extern -- defined here */

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

#define TEST_STATIC_ID 0xA1B2C3D4u

/* ---- HWICAP capture: WF writes counted; CR/SR read idle (instant drain) ---- */
static int s_icap_count;

static int hwicap_hook(void *ctx, int is_write, uint32_t base, uint32_t off, uint32_t *val)
{
    (void)ctx; (void)base;
    if (is_write && off == HWICAP_WF) {
        s_icap_count++;
        return 1;
    }
    if (!is_write && off == HWICAP_CR) {
        *val = 0;                                   /* WRITE bit self-clears at once */
        return 1;
    }
    if (!is_write && off == HWICAP_SR) {
        *val = HWICAP_SR_DONE | HWICAP_SR_EOS;      /* healthy: finish's EOS wait passes */
        return 1;
    }
    return 0;
}

static void boot(void)
{
    mock_regs_reset();
    fake_net_reset();
    mock_regs_set_hook(MPS3_HWICAP_BASE, hwicap_hook, 0);
    mock_regs_poke(MPS3_HWICAP_BASE, HWICAP_WFV, 1024u);
    s_icap_count = 0;
    memset(&g_shell_state, 0, sizeof(g_shell_state));

    /* Greybox seed with NULL clearing bytes: the outgoing clearing stream
     * advances counters only (no WF writes), so s_icap_count stays PURELY the
     * incoming partial. */
    overlay_manifest_info_t greybox = {
        .static_id = TEST_STATIC_ID, .rm_id = 0,
        .clear_len_words = 4, .clear_crc32 = 0, .clear_data = 0,
    };
    fake_overlay_store_set_greybox(&greybox, 0);

    swap_fsm_init();
    config_agent_init();
    config_agent_set_running_static_id(TEST_STATIC_ID);
    config_agent_set_icap_direct_sink(swap_fsm_icap_direct_sink());
}

/* ---- wire framing (same bytes the host pusher emits) ----------------------- */
static uint8_t s_frame[16384];
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
    hdr.static_id = TEST_STATIC_ID;
    hdr.rm_id = rm_id;
    hdr.len_words = len_words;
    hdr.crc32 = mps3_crc32(payload, nbytes);
    mps3_bitstream_hdr_pack(&hdr, out);
    return (int)(MPS3_BITSTREAM_HDR_WIRE_SIZE + nbytes);
}

/* Queue one frame (connect + send + client half-close) and return the client
 * handle so the caller can assert whether the FIRMWARE tore the session
 * (fake_net_fw_closed) — the discriminator between DEFER (held) and the old
 * hard-reject (torn). The whole frame lands in the fake ring at once; the client
 * FIN is only observed by the firmware AFTER it drains the ring, so a held-then-
 * resumed transfer still finishes cleanly. */
static int send_frame_keep(const uint8_t *frame, int len)
{
    int cli = fake_net_connect(MPS3_PORT_RAW_PUSH);
    assert(cli >= 0);
    CHECK(fake_net_send(cli, frame, len) == len);
    fake_net_close(cli);
    return cli;
}

static uint32_t s_verify_rm_id;

/* Inject the vendor-IP confirmations the mocks can't self-drive, keyed on FSM
 * state. Split from the poll calls so the caller can drive swap_fsm and
 * config_agent INDEPENDENTLY — that separation is what lets this test place the
 * partial receipt strictly before the FSM reaches AWAIT_PARTIAL. */
static void inject_confirms(void)
{
    switch (swap_fsm_state()) {
    case SWAP_DECOUPLE_ASSERT:
        mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,
                       DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET);
        break;
    case SWAP_VERIFY:
        mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_ID, s_verify_rm_id);
        mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS, DFXCTL_RM_STATUS_RM_ID_VALID);
        break;
    case SWAP_RELEASE:
        mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS, 0u);
        break;
    default:
        break;
    }
}

static void pump_fsm(int n)  { for (int i = 0; i < n; i++) { inject_confirms(); swap_fsm_poll(); } }
static void pump_cfg(int n)  { for (int i = 0; i < n; i++) { config_agent_poll(); } }
static void pump_both(int n) { for (int i = 0; i < n; i++) { inject_confirms(); swap_fsm_poll(); config_agent_poll(); } }

static void pump_fsm_until(mps3_swap_state_t target, int max)
{
    for (int i = 0; i < max && swap_fsm_state() != target; i++) {
        pump_fsm(1);
    }
    CHECK(swap_fsm_state() == target);
}

#define CLEAR_WORDS 16u    /* 64 B   <= 4096 -> RAM fast path (no ICAP, no gate) */
#define BIG_WORDS   1200u  /* 4800 B >  4096 -> stream-direct to HWICAP (gated) */
#define CLEAR_SEED  0x50
#define BIG_SEED    0x40
#define BIG_RM_ID   0xB2u

/* The regression: the partial header arrives while the FSM is still BEFORE
 * SWAP_AWAIT_PARTIAL. The old firmware tore the session; the fix holds it. */
static void test_partial_deferred_then_completes(void)
{
    boot();
    CHECK(MPS3_CFG_AGENT_STAGING_BYTES == 4096u);
    s_verify_rm_id = BIG_RM_ID;

    /* 1) Arm the swap and drive the FSM ALONE to AWAIT_INCOMING_CLEARING — past
     *    DECOUPLE (so DFXCTL reads parked, which icap_direct_begin() also needs)
     *    but strictly BEFORE AWAIT_PARTIAL. config_agent is NOT polled here, so
     *    nothing has been received yet. */
    CHECK(swap_fsm_start("regdemo_b", "tcp") == 0);
    pump_fsm_until(SWAP_AWAIT_INCOMING_CLEARING, 50);
    CHECK(config_agent_icap_defer_polls() == 0);

    /* 2) Deliver the incoming clearing (RAM fast path) and let config_agent
     *    receive+stage it — but DO NOT pump the FSM, so it stays parked in
     *    AWAIT_INCOMING_CLEARING and has NOT yet taken the clearing / advanced. */
    int n = build_frame(s_frame, MPS3_BIN_KIND_CLEARING, BIG_RM_ID, CLEAR_WORDS, CLEAR_SEED);
    (void)send_frame_keep(s_frame, n);
    pump_cfg(50);
    CHECK(swap_fsm_state() == SWAP_AWAIT_INCOMING_CLEARING); /* FSM untouched */

    /* 3) THE RACE: deliver the partial while the FSM is NOT in AWAIT_PARTIAL.
     *    config_agent must DEFER it (hold the session), NOT tear it. */
    n = build_frame(s_frame, MPS3_BIN_KIND_PARTIAL, BIG_RM_ID, BIG_WORDS, BIG_SEED);
    int pcli = send_frame_keep(s_frame, n);
    pump_cfg(20);

    CHECK(config_agent_icap_defer_polls() > 0);  /* the fix engaged: we held */
    CHECK(s_icap_count == 0);                     /* nothing streamed to a not-yet-clear ICAP */
    CHECK(fake_net_fw_closed(pcli) == 0);         /* session HELD, not torn (old bug: == 1) */
    config_agent_bitstream_info_t info;
    CHECK(config_agent_take_validated_partial(&info) != 0); /* not staged yet — still held */

    /* 4) Advance the FSM: it takes the staged clearing -> AWAIT_PARTIAL, the held
     *    partial arms, streams to HWICAP, and the swap reaches DONE/verified. */
    for (int i = 0; i < 5000 && !swap_fsm_idle(); i++) {
        pump_both(1);
    }
    CHECK(swap_fsm_idle());
    CHECK(swap_fsm_last_result()->valid && swap_fsm_last_result()->ok &&
          swap_fsm_last_result()->verified);
    CHECK(swap_fsm_last_result()->rm_id == BIG_RM_ID);
    CHECK(s_icap_count == (int)BIG_WORDS);        /* every partial word reached HWICAP.WF */
    CHECK(g_current_rm_clearing.valid && g_current_rm_clearing.rm_id == BIG_RM_ID);
}

/* Guard the happy path is UNCHANGED: when the partial arrives with the FSM
 * already in AWAIT_PARTIAL, the defer path must NOT engage (defer counter stays
 * 0) and the swap completes exactly as test_swap_icap_direct.c pins. */
static void test_no_defer_when_already_await_partial(void)
{
    boot();
    s_verify_rm_id = BIG_RM_ID;

    CHECK(swap_fsm_start("regdemo_b", "tcp") == 0);
    pump_fsm_until(SWAP_AWAIT_INCOMING_CLEARING, 50);

    int n = build_frame(s_frame, MPS3_BIN_KIND_CLEARING, BIG_RM_ID, CLEAR_WORDS, CLEAR_SEED);
    (void)send_frame_keep(s_frame, n);
    /* Pump BOTH so the FSM takes the clearing and reaches AWAIT_PARTIAL first. */
    for (int i = 0; i < 200 && swap_fsm_state() != SWAP_AWAIT_PARTIAL; i++) {
        pump_both(1);
    }
    CHECK(swap_fsm_state() == SWAP_AWAIT_PARTIAL);

    n = build_frame(s_frame, MPS3_BIN_KIND_PARTIAL, BIG_RM_ID, BIG_WORDS, BIG_SEED);
    (void)send_frame_keep(s_frame, n);
    for (int i = 0; i < 5000 && !swap_fsm_idle(); i++) {
        pump_both(1);
    }
    CHECK(swap_fsm_idle());
    CHECK(swap_fsm_last_result()->ok && swap_fsm_last_result()->verified);
    CHECK(s_icap_count == (int)BIG_WORDS);
    CHECK(config_agent_icap_defer_polls() == 0);  /* no race -> defer never engaged */
}

int main(void)
{
    test_partial_deferred_then_completes();
    test_no_defer_when_already_await_partial();

    printf("test_swap_icap_defer: %d checks passed\n", s_checks);
    return 0;
}
