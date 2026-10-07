/*
 * test_tftp_large_swap.c — the TFTP transport (UDP 69, RFC1350 WRQ/octet)
 * carrying a >STAGING_BYTES payload into the SAME large-payload sinks the raw
 * 6910 tests exercise, closing the R5 coverage gap (all prior large-payload
 * staging tests pushed over raw TCP only):
 *
 *   - a large PARTIAL over TFTP -> the ICAP-direct sink (streamed straight to
 *     HWICAP.WF as it arrives), driven all the way through a real swap to
 *     DONE/verified — proving the swap-armed-first guard holds for TFTP too;
 *   - a mid-transfer TFTP ERROR into a streaming sink: the sink is aborted
 *     exactly once and NOTHING is staged (the torn transfer leaves no
 *     "validated" payload), and the receiver recovers.
 * RETARGETED (D13): the two QSPI-sink cases (clearing STAGE region, A/B-slot
 * scratch) went with the SST26 backend; the ERROR case now aborts a recording
 * test sink in the clearing seam instead of re-arming SST26 block protection.
 *
 * Built with -DMPS3_CFG_AGENT_STAGING_BYTES=4096 so "large" means ">4 KiB"
 * while the frames stay modest.
 *
 * Links: config_agent.c, swap_fsm.c, swap_fsm_transitions.c, hwicap_writer.c,
 * common/{crc32,net_proto,net_if}.c, mock_regs.c, fake_net_if.c,
 * fake_overlay_store.c (the greybox seed). g_shell_state defined here.
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../coordinator/swap_fsm.h"
#include "../coordinator/coordinator.h"
#include "../config_agent/config_agent.h"
#include "../overlay_store/overlay_store.h"
#include "../common/platform_regs.h"
#include "../common/crc32.h"
#include "../common/net_if.h"
#include "mock_regs.h"
#include "fake_net_if.h"
#include "fake_overlay_store.h"

mps3_shell_state_t g_shell_state;

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

#define TEST_STATIC_ID 0xA1B2C3D4u
#define CLIENT_TID 5001

static const uint8_t k_greybox_bin[16] = {
    0xE0, 0xE1, 0xE2, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7,
    0xE8, 0xE9, 0xEA, 0xEB, 0xEC, 0xED, 0xEE, 0xEF,
};

/* ---- a RECORDING streaming sink (the clearing seam) -------------------------- */
static int      s_rs_begin, s_rs_finish, s_rs_abort;
static uint32_t s_rs_bytes;
static int  rs_begin(uint32_t n)                { (void)n; s_rs_begin++; s_rs_bytes = 0; return 0; }
static int  rs_write(const void *b, uint32_t n) { (void)b; s_rs_bytes += n; return 0; }
static int  rs_finish(uint32_t crc)             { (void)crc; s_rs_finish++; return 0; }
static void rs_abort(void)                      { s_rs_abort++; }
static const mps3_cfg_agent_qspi_sink_t k_rec_sink = { rs_begin, rs_write, rs_finish, rs_abort, NULL };

/* ---- HWICAP capture -------------------------------------------------------- */
#define ICAP_CAP 8192
static uint32_t s_icap_words[ICAP_CAP];
static int      s_icap_count;

static int hwicap_hook(void *ctx, int is_write, uint32_t base, uint32_t off, uint32_t *val)
{
    (void)ctx; (void)base;
    if (is_write && off == HWICAP_WF) {
        if (s_icap_count < ICAP_CAP) s_icap_words[s_icap_count] = *val;
        s_icap_count++;
        return 1;
    }
    if (!is_write && off == HWICAP_CR) { *val = 0; return 1; }
    if (!is_write && off == HWICAP_SR) { *val = HWICAP_SR_DONE | HWICAP_SR_EOS; return 1; }
    return 0;
}

/* ---- step: config_agent poll, optionally pumping the swap FSM too ---------- */
static int      s_pump_swap;
static uint32_t s_verify_rm_id;

static void step(int n)
{
    for (int i = 0; i < n; i++) {
        if (s_pump_swap) {
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
            default: break;
            }
            swap_fsm_poll();
        }
        config_agent_poll();
    }
}

/* ---- wire frame ------------------------------------------------------------ */
static uint8_t s_frame[64 * 1024];

static int build_frame(uint8_t *out, mps3_bin_kind_t kind, uint32_t rm_id,
                       uint32_t len_words, uint8_t seed)
{
    uint32_t nbytes = len_words * 4u;
    uint8_t *payload = out + MPS3_BITSTREAM_HDR_WIRE_SIZE;
    for (uint32_t i = 0; i < nbytes; i++) payload[i] = (uint8_t)(seed + i);
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

/* ---- one whole TFTP PUT (WRQ + 512-byte DATA blocks), stepping between
 * datagrams. Returns 0 on the final ACK, -1 if the server ERRORed. ---------- */
static int tftp_put(const uint8_t *data, int len)
{
    uint8_t pkt[600], rsp[600];
    uint16_t fw_port = 0, to_port = 0;

    int wl = 0;
    pkt[wl++] = 0; pkt[wl++] = 2; /* WRQ */
    const char *fn = "bitstream.bin";
    memcpy(&pkt[wl], fn, strlen(fn) + 1); wl += (int)strlen(fn) + 1;
    const char *mode = "octet";
    memcpy(&pkt[wl], mode, strlen(mode) + 1); wl += (int)strlen(mode) + 1;
    if (fake_net_udp_inject(MPS3_PORT_TFTP, CLIENT_TID, pkt, wl) != 0) return -1;
    step(4);
    int rn = fake_net_udp_take_sent(&fw_port, &to_port, rsp, sizeof(rsp));
    if (rn < 4 || rsp[1] == 5) return -1;
    assert(rsp[1] == 4 && rsp[2] == 0 && rsp[3] == 0); /* ACK 0 */
    uint16_t tid = fw_port;

    int off = 0;
    uint16_t block = 1;
    for (;;) {
        int chunk = len - off;
        if (chunk > 512) chunk = 512;
        pkt[0] = 0; pkt[1] = 3; /* DATA */
        pkt[2] = (uint8_t)(block >> 8); pkt[3] = (uint8_t)block;
        memcpy(&pkt[4], data + off, (size_t)chunk);
        if (fake_net_udp_inject(tid, CLIENT_TID, pkt, 4 + chunk) != 0) return -1;
        step(4);
        rn = fake_net_udp_take_sent(&fw_port, &to_port, rsp, sizeof(rsp));
        if (rn < 4) return -1;
        if (rsp[1] == 5) return -1; /* server ERROR */
        assert(rsp[1] == 4 && ((rsp[2] << 8) | rsp[3]) == block);
        off += chunk;
        if (chunk < 512) return 0; /* final block ACKed */
        block++;
    }
}

/* Push a small frame over raw TCP 6910 (used for the incoming clearing of the
 * ICAP-direct swap; the large payload is what goes over TFTP). */
static void raw_push(const uint8_t *frame, int len)
{
    int cli = fake_net_connect(MPS3_PORT_RAW_PUSH);
    assert(cli >= 0);
    CHECK(fake_net_send(cli, frame, len) == len);
    fake_net_close(cli);
    step(64);
    CHECK(fake_net_fw_closed(cli));
}

static void boot(void)
{
    mock_regs_reset();
    fake_net_reset();
    mock_regs_set_hook(MPS3_HWICAP_BASE, hwicap_hook, 0);
    mock_regs_poke(MPS3_HWICAP_BASE, HWICAP_WFV, 1024u);
    s_icap_count = 0;
    s_pump_swap = 0;
    memset(&g_shell_state, 0, sizeof(g_shell_state));
    fake_overlay_store_reset();
    overlay_manifest_info_t grey = { .static_id = TEST_STATIC_ID, .rm_id = 0,
                                     .clear_len_words = 4, .clear_crc32 = 0,
                                     .clear_data = k_greybox_bin };
    fake_overlay_store_set_greybox(&grey, 0);
    swap_fsm_init();
    config_agent_init();
    config_agent_set_running_static_id(TEST_STATIC_ID);
    config_agent_set_qspi_sink(NULL);
    config_agent_set_qspi_clearing_sink(NULL);
    config_agent_set_icap_direct_sink(NULL);
    s_rs_begin = s_rs_finish = s_rs_abort = 0;
    s_rs_bytes = 0;
}

#define CLEAR_WORDS 16u    /* 64 B <= 4096 -> RAM fast path */
#define BIG_CLEAR_WORDS 6000u  /* 24000 B > 4096 -> the streaming clearing sink */
#define BIG_PART_WORDS  2000u  /* 8000 B  > 4096 -> ICAP-direct */
#define CLEAR_SEED  0x50
#define BIG_CLEAR_SEED 0xC0
#define BIG_PART_SEED  0x90

/* ============================================================================
 * TEST 3: a large PARTIAL over TFTP streamed straight to the ICAP-direct sink,
 * driven through a full swap to DONE/verified (swap-armed-first guard holds).
 * ========================================================================== */
static void test_tftp_large_partial_to_icap_direct_full_swap(void)
{
    boot();
    /* Wire Path 3 (ICAP-direct), exactly as coordinator_init() does. */
    config_agent_set_icap_direct_sink(swap_fsm_icap_direct_sink());

    s_verify_rm_id = 7;
    s_pump_swap = 1; /* step() now advances the swap FSM too */

    /* Arm the swap and let it stream the outgoing greybox clearing + reach the
     * point where it waits for the incoming pair. */
    CHECK(swap_fsm_start("regdemo", "tftp") == 0);
    for (int i = 0; i < 50 && swap_fsm_state() != SWAP_AWAIT_INCOMING_CLEARING; i++) step(1);
    CHECK(swap_fsm_state() == SWAP_AWAIT_INCOMING_CLEARING);
    CHECK(s_icap_count == 4); /* greybox clearing streamed (its 16 bytes = 4 words) */

    /* Incoming clearing over raw TCP (small, RAM), advancing to AWAIT_PARTIAL. */
    int n = build_frame(s_frame, MPS3_BIN_KIND_CLEARING, 7, CLEAR_WORDS, CLEAR_SEED);
    raw_push(s_frame, n);
    for (int i = 0; i < 200 && swap_fsm_state() != SWAP_AWAIT_PARTIAL; i++) step(1);
    CHECK(swap_fsm_state() == SWAP_AWAIT_PARTIAL);
    CHECK(s_icap_count == 4); /* clearing is RAM/cache path, not this swap's ICAP */

    /* The LARGE partial over TFTP: as each DATA block lands, config_agent's
     * ICAP-direct sink pushes its words straight to HWICAP.WF (begin() passes
     * because the swap already asserted DECOUPLE — the armed-first guard). */
    n = build_frame(s_frame, MPS3_BIN_KIND_PARTIAL, 7, BIG_PART_WORDS, BIG_PART_SEED);
    CHECK(tftp_put(s_frame, n) == 0);

    for (int i = 0; i < 5000 && !swap_fsm_idle(); i++) step(1);
    CHECK(swap_fsm_idle());
    CHECK(swap_fsm_last_result()->ok && swap_fsm_last_result()->verified);
    CHECK(swap_fsm_last_result()->rm_id == 7);

    /* Every partial word reached HWICAP over TFTP -> ICAP-direct. */
    CHECK(s_icap_count == (int)(4u + BIG_PART_WORDS));
    CHECK(g_current_rm_clearing.valid && g_current_rm_clearing.rm_id == 7);
}

/* ============================================================================
 * TEST 4: a mid-transfer TFTP ERROR aborts the streaming sink (once) and
 * stages nothing (the torn transfer leaves no validated payload behind).
 * ========================================================================== */
static void test_tftp_midtransfer_error_rearms_and_stages_nothing(void)
{
    boot();
    config_agent_set_qspi_clearing_sink(&k_rec_sink);

    int n = build_frame(s_frame, MPS3_BIN_KIND_CLEARING, 2, BIG_CLEAR_WORDS, BIG_CLEAR_SEED);

    uint8_t pkt[600], rsp[600];
    uint16_t fw_port = 0, to_port = 0;

    /* WRQ + ACK0 */
    int wl = 0; pkt[wl++] = 0; pkt[wl++] = 2;
    memcpy(&pkt[wl], "f\0octet\0", 8); wl += 8;
    CHECK(fake_net_udp_inject(MPS3_PORT_TFTP, CLIENT_TID, pkt, wl) == 0);
    step(4);
    CHECK(fake_net_udp_take_sent(&fw_port, &to_port, rsp, sizeof(rsp)) == 4);
    uint16_t tid = fw_port;

    /* A few DATA blocks: the header completes and the clearing sink's begin()
     * runs — the sink is now OPEN (it is owed a finish or an abort). */
    int off = 0;
    for (uint16_t block = 1; block <= 3; block++) {
        pkt[0] = 0; pkt[1] = 3; pkt[2] = 0; pkt[3] = (uint8_t)block;
        memcpy(&pkt[4], s_frame + off, 512);
        CHECK(fake_net_udp_inject(tid, CLIENT_TID, pkt, 4 + 512) == 0);
        step(4);
        CHECK(fake_net_udp_take_sent(NULL, NULL, rsp, sizeof(rsp)) == 4); /* ACK */
        off += 512;
    }
    CHECK(s_rs_begin == 1 && s_rs_abort == 0 && s_rs_bytes > 0); /* mid-transfer: open */

    /* The client gives up: a TFTP ERROR datagram on the TID. The server must
     * abort the sink, and stage nothing. */
    pkt[0] = 0; pkt[1] = 5; pkt[2] = 0; pkt[3] = 0; pkt[4] = 0;
    CHECK(fake_net_udp_inject(tid, CLIENT_TID, pkt, 5) == 0);
    step(4);

    CHECK(s_rs_abort == 1 && s_rs_finish == 0); /* aborted exactly once */
    config_agent_bitstream_info_t info;
    CHECK(config_agent_take_validated_clearing(&info) != 0); /* nothing staged */
    CHECK(!config_agent_pair_ready());
    (void)n;

    /* And the receiver recovers: a fresh, complete TFTP push stages fine. */
    n = build_frame(s_frame, MPS3_BIN_KIND_CLEARING, 3, BIG_CLEAR_WORDS, BIG_CLEAR_SEED);
    CHECK(tftp_put(s_frame, n) == 0);
    CHECK(config_agent_take_validated_clearing(&info) == 0);
    CHECK(info.rm_id == 3 && info.in_qspi == 1);   /* staged through the sink */
    CHECK(s_rs_begin == 2 && s_rs_finish == 1 && s_rs_abort == 1);
    CHECK(s_rs_bytes == BIG_CLEAR_WORDS * 4u);
}

int main(void)
{
    test_tftp_large_partial_to_icap_direct_full_swap();
    test_tftp_midtransfer_error_rearms_and_stages_nothing();

    printf("test_tftp_large_swap: %d checks passed\n", s_checks);
    return 0;
}
