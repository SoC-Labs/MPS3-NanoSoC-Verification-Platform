/*
 * test_swap_icap_direct.c — Path 3 (stream-direct config_agent -> HWICAP)
 * acceptance binary. A LARGE partial (> config_agent's RAM staging buffer) is
 * pushed over the net_if seam and streamed STRAIGHT into HWICAP.WF as it
 * arrives — never buffered in RAM or QSPI — through the REAL config_agent +
 * REAL swap_fsm to SWAP_DONE/verified. This is the north-star unblock
 * (docs/OVER_THE_WIRE_RECONFIG_PLAN.md §5): it removes the 886 KB–1.65 MB
 * partial size limit for every RM without needing QSPI first-light.
 *
 * What this pins that the QSPI-staging test cannot:
 *   - the I18 byte-lane order: the captured HWICAP words are MSB-first
 *     (word = b0<<24|b1<<16|b2<<8|b3), NOT a native little-endian memcpy of
 *     the .bin bytes (which the QSPI/RAM path — and its tests — bake in);
 *   - the swap-armed-first ordering: the stream-direct sink refuses to write
 *     to the ICAP unless the FSM has already asserted DECOUPLE + rp_reset
 *     (begin() fails closed → the partial is rejected, nothing hits ICAP);
 *   - the FSM fuses receipt and ICAP write: the partial is fully in the ICAP
 *     by the time SWAP_STREAM_PARTIAL runs, so it just advances to VERIFY.
 *
 * Built with -DMPS3_CFG_AGENT_STAGING_BYTES=4096 so "large" means ">4 KiB"
 * while the frames stay small — the exact code path a MiB partial takes.
 *
 * Links: config_agent.c, swap_fsm.c, swap_fsm_transitions.c,
 * common/{crc32,net_proto,net_if}.c, mock_regs.c, fake_net_if.c,
 * fake_overlay_store.c. Defines g_shell_state itself (no coordinator.c).
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

/* ---- HWICAP capture: WF writes recorded; SR reads model idle (DONE|EOS) ---- */
#define ICAP_CAP 8192
static uint32_t s_icap_words[ICAP_CAP];
static int      s_icap_count;
/* I18(3): when 0, the modelled HWICAP_SR NEVER raises EOS (only DONE), so the
 * stream-direct finish's bounded post-DESYNC EOS wait times out — the fault the
 * new diag field is meant to make measurable. boot() resets it to 1 (healthy). */
static int      s_icap_sr_eos = 1;

static int hwicap_hook(void *ctx, int is_write, uint32_t base, uint32_t off, uint32_t *val)
{
    (void)ctx; (void)base;
    if (is_write && off == HWICAP_WF) {
        if (s_icap_count < ICAP_CAP) {
            s_icap_words[s_icap_count] = *val;
        }
        s_icap_count++;
        return 1;
    }
    if (!is_write && off == HWICAP_CR) {
        /* Lite mode: the per-word StartConfig's WRITE bit self-clears the instant
         * the word reaches ICAP — model an instant drain so the CR poll exits at once. */
        *val = 0;
        return 1;
    }
    if (!is_write && off == HWICAP_SR) {
        /* Healthy (s_icap_sr_eos): the core is always "done" and end-of-sequence,
         * so the sink's bounded post-DESYNC EOS wait completes immediately. Fault
         * (s_icap_sr_eos==0): DONE but never EOS, so the wait times out — this is
         * the axi_hwicap "never raises EOS post-DESYNC" case I18(3) must diagnose. */
        *val = s_icap_sr_eos ? (HWICAP_SR_DONE | HWICAP_SR_EOS) : HWICAP_SR_DONE;
        return 1;
    }
    return 0; /* WFV reads, SZ/CR writes -> plain mock slots */
}

/* MSB-first packing of the payload byte pattern (seed + byte index), i.e. the
 * CORRECT I18 order the stream-direct sink must produce. */
static uint32_t msb_first_word(uint8_t seed, uint32_t word_idx)
{
    uint32_t b0 = (uint8_t)(seed + 4u * word_idx + 0u);
    uint32_t b1 = (uint8_t)(seed + 4u * word_idx + 1u);
    uint32_t b2 = (uint8_t)(seed + 4u * word_idx + 2u);
    uint32_t b3 = (uint8_t)(seed + 4u * word_idx + 3u);
    return (b0 << 24) | (b1 << 16) | (b2 << 8) | b3;
}

static void boot(void)
{
    mock_regs_reset();
    fake_net_reset();
    mock_regs_set_hook(MPS3_HWICAP_BASE, hwicap_hook, 0);
    mock_regs_poke(MPS3_HWICAP_BASE, HWICAP_WFV, 1024u);
    s_icap_count = 0;
    s_icap_sr_eos = 1; /* healthy EOS by default; the timeout test clears it */
    memset(&g_shell_state, 0, sizeof(g_shell_state));

    /* Greybox seed of the current-clearing cache: NULL bytes so the outgoing
     * clearing stream advances counters only (no WF writes) — keeps the ICAP
     * capture purely the incoming partial. */
    overlay_manifest_info_t greybox = {
        .static_id = TEST_STATIC_ID, .rm_id = 0,
        .clear_len_words = 4, .clear_crc32 = 0, .clear_data = 0,
    };
    fake_overlay_store_set_greybox(&greybox, 0);

    swap_fsm_init();            /* seeds g_current_rm_clearing from the greybox */
    config_agent_init();
    config_agent_set_running_static_id(TEST_STATIC_ID);
    /* Wire Path 3 exactly as coordinator_init() does under MPS3_CFG_AGENT_ICAP_DIRECT. */
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

/* Queue one frame at the firmware (connect + send + close), no polling yet —
 * the caller's pump() drives config_agent AND swap_fsm together so the
 * receive-and-ICAP-write happens while the FSM sits in SWAP_AWAIT_PARTIAL. */
static void send_frame(const uint8_t *frame, int len)
{
    int cli = fake_net_connect(MPS3_PORT_RAW_PUSH);
    assert(cli >= 0);
    CHECK(fake_net_send(cli, frame, len) == len);
    fake_net_close(cli);
}

static uint32_t s_verify_rm_id;

/* Inject the vendor-IP confirmations the mocks can't self-drive, keyed on the
 * FSM state, then step both polls. */
static void pump(int n)
{
    for (int i = 0; i < n; i++) {
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
        swap_fsm_poll();
        config_agent_poll();
    }
}

static void pump_until(mps3_swap_state_t target, int max)
{
    for (int i = 0; i < max && swap_fsm_state() != target; i++) {
        pump(1);
    }
    CHECK(swap_fsm_state() == target);
}

#define CLEAR_WORDS 16u    /* 64 B   <= 4096 -> RAM fast path */
#define BIG_WORDS   1200u  /* 4800 B >  4096 -> stream-direct to HWICAP */
#define CLEAR_SEED  0x50
#define BIG_SEED    0x40
#define BIG_RM_ID   0xB2u

static void test_large_partial_streams_direct_to_hwicap(void)
{
    boot();
    CHECK(MPS3_CFG_AGENT_STAGING_BYTES == 4096u);
    s_verify_rm_id = BIG_RM_ID;

    /* 1) Arm the swap FIRST so DECOUPLE + rp_reset are asserted before any
     *    ICAP write, and drive it to where it waits for the incoming pair. */
    CHECK(swap_fsm_start("regdemo_b", "tcp") == 0);
    pump_until(SWAP_AWAIT_INCOMING_CLEARING, 50);
    CHECK(s_icap_count == 0); /* greybox clearing has NULL bytes: no WF writes */

    /* 2) Push the incoming pair's small clearing (RAM-staged), advance to the
     *    partial wait. */
    int n = build_frame(s_frame, MPS3_BIN_KIND_CLEARING, BIG_RM_ID, CLEAR_WORDS, CLEAR_SEED);
    send_frame(s_frame, n);
    pump_until(SWAP_AWAIT_PARTIAL, 200);
    CHECK(s_icap_count == 0); /* clearing is RAM/cache path, not this swap's ICAP */

    /* 3) Push the LARGE partial. It streams straight to HWICAP.WF as it lands;
     *    the FSM fuses receipt + ICAP write and settles to DONE/verified. */
    n = build_frame(s_frame, MPS3_BIN_KIND_PARTIAL, BIG_RM_ID, BIG_WORDS, BIG_SEED);
    send_frame(s_frame, n);
    for (int i = 0; i < 5000 && !swap_fsm_idle(); i++) {
        pump(1);
    }
    CHECK(swap_fsm_idle());
    CHECK(swap_fsm_last_result()->valid && swap_fsm_last_result()->ok &&
          swap_fsm_last_result()->verified);
    CHECK(swap_fsm_last_result()->rm_id == BIG_RM_ID);

    /* Every partial word reached HWICAP.WF, packed MSB-first (I18) — NOT the
     * native-endian memcpy the RAM/QSPI writers use. */
    CHECK(s_icap_count == (int)BIG_WORDS);
    for (uint32_t i = 0; i < BIG_WORDS; i++) {
        assert(s_icap_words[i] == msb_first_word(BIG_SEED, i));
    }
    s_checks++;
    /* Sanity: MSB-first genuinely differs from a native LE memcpy here, so the
     * assertion above is not vacuous. */
    {
        uint8_t b[4] = { BIG_SEED, (uint8_t)(BIG_SEED + 1),
                         (uint8_t)(BIG_SEED + 2), (uint8_t)(BIG_SEED + 3) };
        uint32_t native;
        memcpy(&native, b, 4);
        CHECK(s_icap_words[0] != native || native == msb_first_word(BIG_SEED, 0));
    }

    /* Incoming clearing promoted to the current-clearing cache (I2). */
    CHECK(g_current_rm_clearing.valid && g_current_rm_clearing.rm_id == BIG_RM_ID);
    CHECK(g_current_rm_clearing.len_words == CLEAR_WORDS);

    /* I18(3): the stream-direct finish's post-DESYNC EOS wait was SATISFIED, and
     * the diag recorded it — the raw HWICAP_SR (mock: DONE|EOS) and the EOS-gate
     * flag. This is the field the next REAL swap reads over JTAG (icap_sr_last @
     * +0x5C, icap_eos_status @ +0x60) to prove EOS actually asserted on-silicon.
     * "A swap that sees EOS records it." */
    CHECK(swap_fsm_icap_eos_status() == MPS3_ICAP_EOS_SEEN);
    CHECK(swap_fsm_icap_sr_last() == (HWICAP_SR_DONE | HWICAP_SR_EOS));
}

/* I18(3): the DISTINCT-timeout half. A stream-direct swap whose ICAP is DONE but
 * NEVER raises EOS post-DESYNC: icap_direct_finish()'s bounded EOS wait expires,
 * so it returns -1 → config_agent rejects the partial (fail-closed, RP left
 * parked) → the swap does NOT complete. The diag records the outcome DISTINCTLY —
 * icap_eos_status == TIMEOUT and the raw NON-EOS SR — so a real swap that hit this
 * would be diagnosable by one JTAG read instead of code archaeology. This is the
 * exact case four "successful" swaps could never have proved either way. */
static void test_icap_finish_eos_timeout_recorded_distinctly(void)
{
    boot();
    s_icap_sr_eos = 0;          /* SR reads DONE forever, EOS never asserts */
    s_verify_rm_id = BIG_RM_ID;

    CHECK(swap_fsm_start("regdemo_b", "tcp") == 0);
    pump_until(SWAP_AWAIT_INCOMING_CLEARING, 50);

    int n = build_frame(s_frame, MPS3_BIN_KIND_CLEARING, BIG_RM_ID, CLEAR_WORDS, CLEAR_SEED);
    send_frame(s_frame, n);
    pump_until(SWAP_AWAIT_PARTIAL, 200);

    /* Push the large partial. Its words stream into HWICAP.WF fine (CR self-clears),
     * but the finish's EOS wait times out on the last DESYNC. config_agent rejects
     * the partial: it is NEVER handed to the FSM, which stays parked in
     * AWAIT_PARTIAL (DECOUPLE held) — a host retry recovers. */
    n = build_frame(s_frame, MPS3_BIN_KIND_PARTIAL, BIG_RM_ID, BIG_WORDS, BIG_SEED);
    send_frame(s_frame, n);
    for (int i = 0; i < 5000; i++) { pump(1); }

    /* The finish ran and recorded the timeout DISTINCTLY from the EOS-seen case. */
    CHECK(swap_fsm_icap_eos_status() == MPS3_ICAP_EOS_TIMEOUT);
    CHECK(swap_fsm_icap_sr_last() == HWICAP_SR_DONE); /* last NON-ZERO SR, no EOS bit */

    /* Fail-closed: the partial was rejected (nothing staged) and the swap never
     * reached a clean DONE/verified — the FSM stays parked, RP decoupled. */
    config_agent_bitstream_info_t info;
    CHECK(config_agent_take_validated_partial(&info) != 0);
    CHECK(swap_fsm_state() != SWAP_DONE);
    CHECK(!(swap_fsm_last_result()->valid && swap_fsm_last_result()->ok));
}

static void test_partial_rejected_when_swap_not_armed(void)
{
    boot();

    /* No swap armed -> RP is NOT decoupled. The stream-direct sink's begin()
     * must fail closed (never write config frames to a live RP): the partial
     * is rejected and NOTHING reaches HWICAP. */
    int n = build_frame(s_frame, MPS3_BIN_KIND_CLEARING, BIG_RM_ID, CLEAR_WORDS, CLEAR_SEED);
    send_frame(s_frame, n);
    for (int i = 0; i < 128; i++) { config_agent_poll(); }

    n = build_frame(s_frame, MPS3_BIN_KIND_PARTIAL, BIG_RM_ID, BIG_WORDS, BIG_SEED);
    send_frame(s_frame, n);
    for (int i = 0; i < 128; i++) { config_agent_poll(); }

    config_agent_bitstream_info_t info;
    CHECK(config_agent_take_validated_partial(&info) != 0); /* nothing staged */
    CHECK(s_icap_count == 0);                               /* no ICAP write */
}

/* CRC fail-closed: an ICAP-direct partial whose payload no longer matches its
 * header crc32. Path 3 streams the bytes to HWICAP as they arrive (no buffer to
 * pre-verify), so this pins the documented detect-AFTER-ICAP + park-safe
 * contract: config_agent's transport-CRC gate (finish_payload, s_crc_run vs the
 * header) REJECTS the partial, it is never handed to the FSM, and the RP stays
 * parked (decoupled + held in reset) for a host retry. Nothing exercised this. */
static void test_partial_crc_mismatch_fails_closed(void)
{
    boot();
    s_verify_rm_id = BIG_RM_ID;

    CHECK(swap_fsm_start("regdemo_b", "tcp") == 0);
    pump_until(SWAP_AWAIT_INCOMING_CLEARING, 50);
    int n = build_frame(s_frame, MPS3_BIN_KIND_CLEARING, BIG_RM_ID, CLEAR_WORDS, CLEAR_SEED);
    send_frame(s_frame, n);
    pump_until(SWAP_AWAIT_PARTIAL, 200);

    /* Valid partial frame, then corrupt ONE payload byte so the received bytes'
     * running CRC diverges from the header crc32 the pusher computed. */
    n = build_frame(s_frame, MPS3_BIN_KIND_PARTIAL, BIG_RM_ID, BIG_WORDS, BIG_SEED);
    s_frame[MPS3_BITSTREAM_HDR_WIRE_SIZE] ^= 0xFFu; /* flip first payload byte */
    send_frame(s_frame, n);
    for (int i = 0; i < 5000; i++) { pump(1); }

    /* Fail-closed: partial REJECTED (never staged), swap did NOT reach DONE. */
    config_agent_bitstream_info_t info;
    CHECK(config_agent_take_validated_partial(&info) != 0);
    CHECK(swap_fsm_state() != SWAP_DONE);
    CHECK(!(swap_fsm_last_result()->valid && swap_fsm_last_result()->ok));
    /* Bytes DID reach the ICAP (streamed before the finish-CRC gate) — this is
     * the detect-after-ICAP path, not a pre-reject no-op. */
    CHECK(s_icap_count > 0);
    /* RP left parked: still decoupled + held in reset (never released). */
    CHECK((mps3_reg_read32(MPS3_DFXCTL_BASE, DFXCTL_STATUS) &
           (DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET)) ==
          (DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET));
}

int main(void)
{
    test_large_partial_streams_direct_to_hwicap();
    test_partial_rejected_when_swap_not_armed();
    test_icap_finish_eos_timeout_recorded_distinctly();
    test_partial_crc_mismatch_fails_closed();

    printf("test_swap_icap_direct: %d checks passed\n", s_checks);
    return 0;
}
