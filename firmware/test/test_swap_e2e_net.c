/*
 * test_swap_e2e_net.c — the W-NET-SEAM acceptance binary: a FULL swap
 * driven end-to-end through the seam with the shipping code at every layer
 * that exists:
 *
 *   fake 6910 client --> REAL config_agent receive session (header ->
 *   payload -> CRC -> two-slot pair staging) --> REAL swap_fsm (gate,
 *   decouple, stream cached clearing, take pair, stream partial words into
 *   HWICAP_WF, I25 verify, cache promotion, release) --> REAL coordinator
 *   dispatch + held-response bookkeeping (coordinator_net.c) --> response
 *   JSON line back out the fake 6900 client.
 *
 * Only mock_regs (register backend), fake_net_if (transport backend),
 * fake_overlay_store (greybox seed; no SPI here — that's W-SPI's binary)
 * and fake_services (unrelated port servers) are fakes.
 *
 * Links: coordinator.c, coordinator_net.c, swap_fsm.c,
 * swap_fsm_transitions.c, clkrst.c, config_agent.c, common/net_proto.c,
 * common/crc32.c, common/net_if.c, mock_regs.c, fake_net_if.c,
 * fake_overlay_store.c, fake_services.c. -DMPS3_HAL_MOCK.
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../coordinator/coordinator.h"
#include "../coordinator/swap_fsm.h"
#include "../config_agent/config_agent.h"
#include "../common/platform_regs.h"
#include "../common/crc32.h"
#include "../common/net_if.h"
#include "mock_regs.h"
#include "fake_net_if.h"
#include "fake_overlay_store.h"

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

#define TEST_STATIC_ID 0xA1B2C3D4u

/* Strong override of coordinator.c's WEAK static_id seam — exactly how the
 * real shell build provisions it. */
uint32_t mps3_shell_static_id(void)
{
    return TEST_STATIC_ID;
}

/* ---- HWICAP word capture (mock hook): every word the FSM streams -------- */
#define ICAP_CAP 4096
static uint32_t s_icap_words[ICAP_CAP];
static int      s_icap_count;

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
        /* Lite mode: the WRITE bit self-clears the instant the word reaches
         * ICAP, so model an instant drain and the per-word CR poll exits at once. */
        *val = 0;
        return 1;
    }
    return 0; /* everything else (WFV reads, SZ/CR writes) -> plain slots */
}

/* ---- frame building (same wire bytes the host pusher emits) -------------- */
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

/* ---- the superloop (coordinator_main_loop's body, minus for(;;)) ---------- */
static void superloop(int iterations, uint32_t rm_id_readback)
{
    for (int i = 0; i < iterations; i++) {
        /* Vendor-IP choreography the mocks can't self-drive: DFX decoupler
         * STATUS confirmations and the RM_ID readback (same pattern as
         * test_coordinator_dispatch's pump_swap). */
        switch (swap_fsm_state()) {
        case SWAP_DECOUPLE_ASSERT:
            mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,
                           DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET);
            break;
        case SWAP_VERIFY:
            mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_ID, rm_id_readback);
            mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS,
                           DFXCTL_RM_STATUS_RM_ID_VALID);
            break;
        case SWAP_RELEASE:
            mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS, 0u);
            break;
        case SWAP_REISOLATE:
            /* R1: verify failure re-isolates the RP before reporting failure.
             * Model the vendor IP re-confirming decoupled + rp_in_reset. */
            mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,
                           DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET);
            break;
        default:
            break;
        }
        swap_fsm_poll();
        config_agent_poll();
        coordinator_net_poll();
    }
}

static void boot(void)
{
    mock_regs_reset();
    fake_net_reset();
    overlay_manifest_info_t greybox = {
        .static_id = TEST_STATIC_ID, .rm_id = 0,
        .clear_len_words = 4, .clear_crc32 = 0x1111, .clear_data = NULL,
    };
    fake_overlay_store_set_greybox(&greybox, 0);
    coordinator_init();
    mock_regs_set_hook(MPS3_HWICAP_BASE, hwicap_hook, 0);
    mock_regs_poke(MPS3_HWICAP_BASE, HWICAP_WFV, 1024u);
    s_icap_count = 0;
}

/* Send one line on the control client; poll; read the response line. */
static int ctrl_exchange(int cli, const char *line, char *rsp, int cap)
{
    CHECK(fake_net_send(cli, line, (int)strlen(line)) == (int)strlen(line));
    superloop(8, 0);
    int n = fake_net_recv(cli, rsp, cap - 1);
    rsp[n < 0 ? 0 : n] = '\0';
    return n;
}

static void raw_push_ok(const uint8_t *frame, int len)
{
    int cli = fake_net_connect(MPS3_PORT_RAW_PUSH);
    CHECK(cli >= 0);
    CHECK(fake_net_send(cli, frame, len) == len);
    fake_net_close(cli);
    superloop(64, 0);
    CHECK(fake_net_fw_closed(cli));
}

static uint8_t s_frame[8192];

int main(void)
{
    boot();
    char rsp[512];

    /* -- ping over the real 6900 listener ---------------------------------- */
    int ctrl = fake_net_connect(MPS3_PORT_CONTROL);
    CHECK(ctrl >= 0);
    CHECK(ctrl_exchange(ctrl, "{\"op\":\"ping\"}\n", rsp, sizeof(rsp)) > 0);
    CHECK(strcmp(rsp, "{\"ok\":true,\"shell_id\":\"0xa1b2c3d4\",\"rm_id\":\"0x00000000\"}\n") == 0);

    /* second control client refused while the first is open */
    int ctrl2 = fake_net_connect(MPS3_PORT_CONTROL);
    superloop(2, 0);
    CHECK(fake_net_fw_closed(ctrl2));
    CHECK(!fake_net_fw_closed(ctrl));

    /* -- SWAP #1: push pair (raw 6910), then swap, held response ----------- */
    int n = build_frame(s_frame, MPS3_BIN_KIND_CLEARING, 1, 16, 0x10);
    raw_push_ok(s_frame, n);
    n = build_frame(s_frame, MPS3_BIN_KIND_PARTIAL, 1, 12, 0x40);
    raw_push_ok(s_frame, n);
    CHECK(config_agent_pair_ready());

    const char *swap1 = "{\"op\":\"swap\",\"rm\":\"nanosoc\",\"src\":\"tcp\"}\n";
    CHECK(fake_net_send(ctrl, swap1, (int)strlen(swap1)) == (int)strlen(swap1));
    superloop(4, 1);
    CHECK(fake_net_recv(ctrl, rsp, sizeof(rsp)) == 0); /* response is HELD */
    CHECK(!swap_fsm_idle());

    superloop(64, 1); /* FSM runs to DONE; held response released */
    CHECK(swap_fsm_idle());
    int rn = fake_net_recv(ctrl, rsp, sizeof(rsp) - 1);
    CHECK(rn > 0);
    rsp[rn] = '\0';
    CHECK(strcmp(rsp, "{\"ok\":true,\"rm_id\":\"0x00000001\",\"verified\":true}\n") == 0);

    /* Swap #1's HWICAP traffic: the greybox clearing has no in-RAM bytes
     * (fake seed, data=NULL -> counters only), so the captured words are
     * exactly the partial's 12. Byte-identical to what was pushed. */
    CHECK(s_icap_count == 12);
    for (int i = 0; i < 12; i++) {
        uint32_t w;
        uint8_t bytes[4] = { (uint8_t)(0x40 + 4 * i), (uint8_t)(0x41 + 4 * i),
                             (uint8_t)(0x42 + 4 * i), (uint8_t)(0x43 + 4 * i) };
        memcpy(&w, bytes, 4);
        assert(s_icap_words[i] == w);
    }
    s_checks++; /* the word-compare loop held */

    /* world-view moved */
    CHECK(ctrl_exchange(ctrl, "{\"op\":\"ping\"}\n", rsp, sizeof(rsp)) > 0);
    CHECK(strcmp(rsp, "{\"ok\":true,\"shell_id\":\"0xa1b2c3d4\",\"rm_id\":\"0x00000001\"}\n") == 0);

    /* current clearing is now pair #1's, bytes resident in the arena */
    CHECK(g_current_rm_clearing.valid && g_current_rm_clearing.rm_id == 1);
    CHECK(g_current_rm_clearing.len_words == 16 && g_current_rm_clearing.data != NULL);
    CHECK(((const uint8_t *)g_current_rm_clearing.data)[0] == 0x10);
    /* and the resident partial ref (commit's byte source) got promoted */
    CHECK(g_current_rm_partial.valid && g_current_rm_partial.rm_id == 1);
    CHECK(g_current_rm_partial.data != NULL);

    /* -- SWAP #2: proves I2 end-to-end — the OUTGOING RM's cached clearing
     * (pair #1's, promoted at SWAP_CACHE_CLEARING) streams FIRST, then the
     * new partial. -------------------------------------------------------- */
    n = build_frame(s_frame, MPS3_BIN_KIND_CLEARING, 2, 8, 0x60);
    raw_push_ok(s_frame, n);
    n = build_frame(s_frame, MPS3_BIN_KIND_PARTIAL, 2, 10, 0x80);
    raw_push_ok(s_frame, n);

    s_icap_count = 0;
    const char *swap2 = "{\"op\":\"swap\",\"rm\":\"led\",\"src\":\"tcp\"}\n";
    CHECK(fake_net_send(ctrl, swap2, (int)strlen(swap2)) == (int)strlen(swap2));
    superloop(64, 2);
    CHECK(swap_fsm_idle());
    rn = fake_net_recv(ctrl, rsp, sizeof(rsp) - 1);
    CHECK(rn > 0);
    rsp[rn] = '\0';
    CHECK(strcmp(rsp, "{\"ok\":true,\"rm_id\":\"0x00000002\",\"verified\":true}\n") == 0);

    /* 16 clearing words (pair #1's payload, out of the arena) + 10 partial */
    CHECK(s_icap_count == 26);
    for (int i = 0; i < 16; i++) {
        uint32_t w;
        uint8_t bytes[4] = { (uint8_t)(0x10 + 4 * i), (uint8_t)(0x11 + 4 * i),
                             (uint8_t)(0x12 + 4 * i), (uint8_t)(0x13 + 4 * i) };
        memcpy(&w, bytes, 4);
        assert(s_icap_words[i] == w);
    }
    s_checks++; /* clearing words = pair #1's clearing, byte-identical */
    CHECK(g_current_rm_clearing.rm_id == 2); /* pair #2's promoted in turn */

    /* -- SWAP #3: I25 verify failure — held response reports failure, RP
     * parked decoupled, resident refs untouched. --------------------------- */
    n = build_frame(s_frame, MPS3_BIN_KIND_CLEARING, 3, 4, 0xA0);
    raw_push_ok(s_frame, n);
    n = build_frame(s_frame, MPS3_BIN_KIND_PARTIAL, 3, 4, 0xB0);
    raw_push_ok(s_frame, n);

    const char *swap3 = "{\"op\":\"swap\",\"rm\":\"bad\",\"src\":\"tcp\"}\n";
    CHECK(fake_net_send(ctrl, swap3, (int)strlen(swap3)) == (int)strlen(swap3));
    superloop(64, 99); /* wrong RM_ID reads back -> SWAP_FAILED */
    CHECK(swap_fsm_idle());
    rn = fake_net_recv(ctrl, rsp, sizeof(rsp) - 1);
    CHECK(rn > 0);
    rsp[rn] = '\0';
    CHECK(strcmp(rsp, "{\"ok\":false,\"err\":\"swap failed\"}\n") == 0);
    CHECK((mock_regs_peek(MPS3_DFXCTL_BASE, DFXCTL_DECOUPLE) & DFXCTL_DECOUPLE_EN) != 0);
    CHECK(g_current_rm_clearing.rm_id == 2); /* failed swap never promotes */
    CHECK(g_shell_state.current_rm_id == 2);

    /* control channel still alive after the failure */
    CHECK(ctrl_exchange(ctrl, "{\"op\":\"ping\"}\n", rsp, sizeof(rsp)) > 0);
    CHECK(strcmp(rsp, "{\"ok\":true,\"shell_id\":\"0xa1b2c3d4\",\"rm_id\":\"0x00000002\"}\n") == 0);

    /* -- client hangup while parked: response dropped, server survives ----- */
    n = build_frame(s_frame, MPS3_BIN_KIND_CLEARING, 4, 4, 0xC0);
    raw_push_ok(s_frame, n);
    n = build_frame(s_frame, MPS3_BIN_KIND_PARTIAL, 4, 4, 0xD0);
    raw_push_ok(s_frame, n);
    const char *swap4 = "{\"op\":\"swap\",\"rm\":\"x\",\"src\":\"tcp\"}\n";
    CHECK(fake_net_send(ctrl, swap4, (int)strlen(swap4)) == (int)strlen(swap4));
    superloop(4, 4);
    fake_net_close(ctrl); /* hang up mid-swap */
    superloop(64, 4);
    CHECK(swap_fsm_idle()); /* the swap itself still completed */
    CHECK(g_shell_state.current_rm_id == 4);

    int ctrl3 = fake_net_connect(MPS3_PORT_CONTROL); /* slot freed for a new client */
    CHECK(ctrl3 >= 0);
    CHECK(ctrl_exchange(ctrl3, "{\"op\":\"ping\"}\n", rsp, sizeof(rsp)) > 0);
    CHECK(strcmp(rsp, "{\"ok\":true,\"shell_id\":\"0xa1b2c3d4\",\"rm_id\":\"0x00000004\"}\n") == 0);

    printf("test_swap_e2e_net: %d checks passed\n", s_checks);
    return 0;
}
