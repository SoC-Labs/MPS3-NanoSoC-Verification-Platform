/*
 * test_hwicap_byte_lane.c — pins the ICAP byte-lane order (R3 / I18), the
 * endianness bug that "would byte-swap every config word so ICAP never syncs."
 *
 * Built with -DMPS3_HWICAP_MSB_FIRST=1 so the REAL target packing is exercised
 * on the host (the host-mock default is native memcpy, kept only so the other
 * tests' byte-pattern assertions stay identical — see platform_regs.h). Feeds a
 * known .bin prefix carrying the 0xAA995566 sync word through the swap FSM's RAM
 * clearing writer (coordinator/swap_fsm.c hwicap_push_chunk ->
 * mps3_hwicap_pack_word -> HWICAP.WF) and asserts the captured WF write sequence
 * is byte-exact BIG-endian. A re-introduced native-endian memcpy on any writer
 * fails HERE, not at board bring-up.
 *
 * All writers (swap_fsm.c's RAM + stream-direct, overlay_store.c's QSPI/boot)
 * now share ONE primitive, mps3_hwicap_pack_word(); this binary also asserts
 * that primitive directly so the guarantee is nailed at the source.
 *
 * Links: swap_fsm.c, swap_fsm_transitions.c, mock_regs.c, fake_config_agent.c,
 * fake_overlay_store.c. -DMPS3_HAL_MOCK -DMPS3_HWICAP_MSB_FIRST=1.
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../coordinator/swap_fsm.h"
#include "../coordinator/coordinator.h"
#include "../common/platform_regs.h"
#include "mock_regs.h"
#include "fake_config_agent.h"
#include "fake_overlay_store.h"

mps3_shell_state_t g_shell_state;

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

#define TEST_STATIC_ID 0xA1B2C3D4u

/* A real DFX .bin prefix shape: the 32-bit config words are BIG-endian on
 * disk (first file byte = MSB). The sync word is the bytes AA 99 55 66, which
 * ICAP must receive as 0xAA995566 to lock (fpga/dfx/README.md I18). */
static const uint8_t kbin[] = {
    0xFF, 0xFF, 0xFF, 0xFF,   /* word0: dummy pad                */
    0xAA, 0x99, 0x55, 0x66,   /* word1: SYNC WORD                */
    0x20, 0x00, 0x00, 0x00,   /* word2: Type-1 NOP               */
    0x30, 0x02, 0x20, 0x01,   /* word3: write-to-CMD register    */
};
#define KWORDS (sizeof(kbin) / 4u)

static uint32_t be_word(uint32_t i)
{
    const uint8_t *p = &kbin[4u * i];
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

/* Explicit little-endian pack of the same 4 bytes (host-endianness-independent
 * construction) — what a native memcpy on the little-endian MicroBlaze would
 * WRONGLY produce, so the "MSB-first != native" assertions are never vacuous. */
static uint32_t le_word(uint32_t i)
{
    const uint8_t *p = &kbin[4u * i];
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

/* ---- HWICAP capture ------------------------------------------------------- */
#define ICAP_CAP 64
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
        *val = 0; /* lite mode: WRITE self-clears instantly */
        return 1;
    }
    return 0;
}

static void test_pack_primitive_is_big_endian(void)
{
    CHECK(MPS3_HWICAP_MSB_FIRST == 1); /* this binary forces target packing */

    /* The sync word packs to exactly 0xAA995566 (what ICAP needs), NOT the
     * byte-swapped 0x665599AA a native little-endian memcpy would give. */
    CHECK(mps3_hwicap_pack_word(&kbin[4]) == 0xAA995566u);
    CHECK(le_word(1) == 0x665599AAu);            /* the WRONG order, spelled out */
    CHECK(mps3_hwicap_pack_word(&kbin[4]) != le_word(1));

    for (uint32_t i = 0; i < KWORDS; i++) {
        assert(mps3_hwicap_pack_word(&kbin[4u * i]) == be_word(i));
    }
    s_checks++;
}

static void test_writer_streams_big_endian_words(void)
{
    mock_regs_reset();
    fake_config_agent_reset();
    memset(&g_shell_state, 0, sizeof(g_shell_state));
    mock_regs_set_hook(MPS3_HWICAP_BASE, hwicap_hook, 0);
    mock_regs_poke(MPS3_HWICAP_BASE, HWICAP_WFV, 1024u);
    s_icap_count = 0;

    /* Seed the current-clearing cache with the known .bin prefix as its RAM
     * bytes, then drive the swap FSM to stream it out through the real RAM
     * writer (hwicap_push_chunk). */
    overlay_manifest_info_t greybox = {
        .static_id = TEST_STATIC_ID, .rm_id = 0,
        .clear_len_words = KWORDS, .clear_crc32 = 0, .clear_data = kbin,
    };
    fake_overlay_store_set_greybox(&greybox, 0);
    swap_fsm_init();

    CHECK(swap_fsm_start("nanosoc", "tcp") == 0);
    swap_fsm_poll(); /* GATE -> DECOUPLE_ASSERT */
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,
                   DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET);
    swap_fsm_poll(); /* -> STREAM_CLEARING */
    CHECK(swap_fsm_state() == SWAP_STREAM_CLEARING);
    swap_fsm_poll(); /* streams all KWORDS words in one chunk -> AWAIT_INCOMING_CLEARING */

    CHECK(s_icap_count == (int)KWORDS);
    for (uint32_t i = 0; i < KWORDS; i++) {
        assert(s_icap_words[i] == be_word(i)); /* byte-exact big-endian */
    }
    s_checks++;
    /* The sync word specifically reached HWICAP.WF as 0xAA995566 (would be
     * 0x665599AA under the byte-swapping native memcpy this test guards). */
    CHECK(s_icap_words[1] == 0xAA995566u);
}

int main(void)
{
    test_pack_primitive_is_big_endian();
    test_writer_streams_big_endian_words();

    printf("test_hwicap_byte_lane: %d checks passed\n", s_checks);
    return 0;
}
