/*
 * test_swap_fsm_hw.c — host-gcc integration test for
 * coordinator/swap_fsm.c's *impure* step_*() functions (register pokes +
 * module glue), run against firmware/test/mock_regs.c instead of real
 * MMIO. This is the "thin HAL shim" pay-off: swap_fsm.c is completely
 * unmodified between this test build and a real MicroBlaze build -- only
 * the linked mps3_reg_*32 backend (mock_regs.c here, real MMIO on target)
 * differs, selected purely by -DMPS3_HAL_MOCK.
 *
 * Links: swap_fsm.c, swap_fsm_transitions.c, mock_regs.c,
 * fake_config_agent.c (config_agent's real receive pipeline is
 * network-facing TODO(A3), so a live transfer can never be driven from a
 * unit test -- see fake_config_agent.h), fake_overlay_store.c (same
 * reasoning: overlay_store.c's greybox accessor needs a linker-provided
 * blob no host build has). NOT linked: config_agent.c, overlay_store.c,
 * ovlstore_codec.c, coordinator.c -- this test defines g_shell_state
 * itself (swap_fsm.c only touches its fields, never calls a coordinator.c
 * function) to avoid pulling in coordinator.c's own long TODO chain of
 * clkrst_init()/swd_server_init()/etc.
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
#include "../common/hwicap_writer.h"

mps3_shell_state_t g_shell_state; /* swap_fsm.c's extern -- defined here, not in coordinator.c */

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

static void reset_all(void)
{
    mock_regs_reset();
    fake_config_agent_reset();
    fake_overlay_store_reset();
    g_shell_state = (mps3_shell_state_t){0};
}

static void test_happy_path_full_swap(void)
{
    reset_all();

    overlay_manifest_info_t greybox = { .static_id = 0xA1B2C3D4u, .rm_id = 0, .clear_len_words = 4, .clear_crc32 = 0x1111 };
    fake_overlay_store_set_greybox(&greybox, 0);
    swap_fsm_init();

    CHECK(g_current_rm_clearing.valid);
    CHECK(g_current_rm_clearing.rm_id == 0);
    CHECK(g_current_rm_clearing.len_words == 4);

    CHECK(swap_fsm_start("nanosoc", "tftp") == 0);
    CHECK(swap_fsm_state() == SWAP_GATE);

    swap_fsm_poll(); /* GATE -> DECOUPLE_ASSERT (unconditional) */
    CHECK(swap_fsm_state() == SWAP_DECOUPLE_ASSERT);
    CHECK(g_shell_state.xvc_gated && g_shell_state.swd_gated &&
          g_shell_state.uart_gated && g_shell_state.link_gated);

    swap_fsm_poll(); /* DFXCTL.STATUS not confirmed yet in the mock -- stays parked */
    CHECK(swap_fsm_state() == SWAP_DECOUPLE_ASSERT);

    /* Simulate the vendor DFX Decoupler/Shutdown-Manager IP confirming. */
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,
                   DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET);
    /* Plenty of HWICAP write-FIFO vacancy for the whole test. */
    mock_regs_poke(MPS3_HWICAP_BASE, HWICAP_WFV, 1024u);

    swap_fsm_poll(); /* -> STREAM_CLEARING */
    CHECK(swap_fsm_state() == SWAP_STREAM_CLEARING);

    swap_fsm_poll(); /* g_current_rm_clearing.len_words==4, fits in one chunk -> done */
    CHECK(swap_fsm_state() == SWAP_AWAIT_INCOMING_CLEARING);

    swap_fsm_poll(); /* nothing armed yet -- stays */
    CHECK(swap_fsm_state() == SWAP_AWAIT_INCOMING_CLEARING);

    config_agent_bitstream_info_t incoming_clear = {
        .rm_id = 1, .static_id = 0xA1B2C3D4u, .len_words = 8, .crc32 = 0x2222,
    };
    fake_config_agent_arm_clearing(&incoming_clear);
    swap_fsm_poll(); /* -> AWAIT_PARTIAL */
    CHECK(swap_fsm_state() == SWAP_AWAIT_PARTIAL);

    swap_fsm_poll(); /* nothing armed yet -- stays */
    CHECK(swap_fsm_state() == SWAP_AWAIT_PARTIAL);

    config_agent_bitstream_info_t incoming_partial = {
        .rm_id = 1, .static_id = 0xA1B2C3D4u, .len_words = 4, .crc32 = 0x3333,
    };
    fake_config_agent_arm_partial(&incoming_partial);
    swap_fsm_poll(); /* -> STREAM_PARTIAL */
    CHECK(swap_fsm_state() == SWAP_STREAM_PARTIAL);

    swap_fsm_poll(); /* 4 words, fits in one chunk -> done */
    /* R1 reorder: STREAM_PARTIAL -> RELEASE. RM_ID reads back CLAMPED (0) while
     * the decoupler is asserted, so the RP is connected BEFORE it is verified. */
    CHECK(swap_fsm_state() == SWAP_RELEASE);

    swap_fsm_poll(); /* STATUS mock still shows the old confirmed bits -- stays */
    CHECK(swap_fsm_state() == SWAP_RELEASE);
    CHECK(g_shell_state.xvc_gated); /* must not ungate before STATUS confirms */

    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS, 0u); /* decoupled==0, rp_in_reset==0 */
    swap_fsm_poll(); /* -> VERIFY */
    CHECK(swap_fsm_state() == SWAP_VERIFY);
    /* AXI shutdown must STILL be asserted: an unverified RM may present its id
     * but must not master the bus. */
    CHECK(mock_regs_peek(MPS3_DFXCTL_BASE, DFXCTL_SHUTDOWN) & DFXCTL_SHUTDOWN_AXI);
    CHECK(g_shell_state.xvc_gated); /* and the debug channels stay shut */

    /* rm_id_valid has not settled yet -> VERIFY polls, it does not decide. */
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS, 0u);
    swap_fsm_poll();
    CHECK(swap_fsm_state() == SWAP_VERIFY);
    CHECK(g_shell_state.current_rm_id != 1u); /* nothing committed on an unsettled id */

    /* I25: verify does a REAL DFXCTL.RM_ID compare -- must match the target
     * rm_id (1, from the partial's own header) AND rm_id_valid. */
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_ID, 1u);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS, DFXCTL_RM_STATUS_RM_ID_VALID);
    swap_fsm_poll(); /* -> CACHE_CLEARING; THE commit point */
    CHECK(swap_fsm_state() == SWAP_CACHE_CLEARING);
    CHECK(g_shell_state.current_rm_id == 1u);
    /* committed: only now may the RP master the bus */
    CHECK(!(mock_regs_peek(MPS3_DFXCTL_BASE, DFXCTL_SHUTDOWN) & DFXCTL_SHUTDOWN_AXI));
    /* ...and only now may the DUT RUN. The swap path never released dut_resetn,
     * which went unnoticed because every DUT proven so far exposes a constant
     * rm_id tie-off and no logic -- it "verified" fine while held in reset.
     * rm_uart_echo, the first DUT with logic, emitted nothing on silicon. */
    CHECK(mock_regs_peek(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL) & CLKRST_RESET_CTRL_DUT_RESETN);
    /* dbg_resetn released too: nanoSoC ANDs all three resets into one core reset,
     * so without this a swapped SoC sits held in reset (dead DAP, no boot). */
    CHECK(mock_regs_peek(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL) & CLKRST_RESET_CTRL_DBG_RESETN);

    swap_fsm_poll(); /* -> DONE; clearing promoted, and only now do we ungate */
    CHECK(swap_fsm_state() == SWAP_DONE);
    CHECK(g_current_rm_clearing.valid);
    CHECK(g_current_rm_clearing.rm_id == 1u);
    CHECK(g_current_rm_clearing.len_words == 8u);
    CHECK(g_current_rm_clearing.crc32 == 0x2222u);
    CHECK(!g_shell_state.xvc_gated && !g_shell_state.swd_gated &&
          !g_shell_state.uart_gated && !g_shell_state.link_gated);

    swap_fsm_poll(); /* -> IDLE */
    CHECK(swap_fsm_state() == SWAP_IDLE);
    CHECK(swap_fsm_idle());
}

static void test_fails_closed_when_clearing_cache_invalid(void)
{
    reset_all();

    /* Greybox lookup fails -- g_current_rm_clearing stays invalid. */
    fake_overlay_store_set_greybox(NULL, -1);
    swap_fsm_init();
    CHECK(!g_current_rm_clearing.valid);

    swap_fsm_start("nanosoc", "tftp");
    swap_fsm_poll(); /* GATE -> DECOUPLE_ASSERT */
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,
                   DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET);
    swap_fsm_poll(); /* -> STREAM_CLEARING */
    CHECK(swap_fsm_state() == SWAP_STREAM_CLEARING);

    swap_fsm_poll(); /* I2 guarantee violated -- fails closed, not an infinite wait */
    CHECK(swap_fsm_state() == SWAP_FAILED);

    swap_fsm_poll(); /* -> IDLE */
    CHECK(swap_fsm_state() == SWAP_IDLE);
}

static void test_i25_verify_mismatch_fails_and_stays_decoupled(void)
{
    reset_all();
    overlay_manifest_info_t greybox = { .static_id = 0xA1B2C3D4u, .rm_id = 0, .clear_len_words = 1, .clear_crc32 = 0 };
    fake_overlay_store_set_greybox(&greybox, 0);
    swap_fsm_init();

    swap_fsm_start("nanosoc", "tftp");
    swap_fsm_poll(); /* -> DECOUPLE_ASSERT */
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,
                   DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET);
    mock_regs_poke(MPS3_HWICAP_BASE, HWICAP_WFV, 1024u);
    swap_fsm_poll(); /* -> STREAM_CLEARING */
    swap_fsm_poll(); /* -> AWAIT_INCOMING_CLEARING */

    config_agent_bitstream_info_t incoming_clear = { .rm_id = 1, .static_id = 0xA1B2C3D4u, .len_words = 1, .crc32 = 0 };
    fake_config_agent_arm_clearing(&incoming_clear);
    swap_fsm_poll(); /* -> AWAIT_PARTIAL */

    config_agent_bitstream_info_t incoming_partial = { .rm_id = 1, .static_id = 0xA1B2C3D4u, .len_words = 1, .crc32 = 0 };
    fake_config_agent_arm_partial(&incoming_partial);
    swap_fsm_poll(); /* -> STREAM_PARTIAL */
    swap_fsm_poll(); /* -> RELEASE (R1 reorder) */
    CHECK(swap_fsm_state() == SWAP_RELEASE);

    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS, 0u); /* release confirms */
    swap_fsm_poll(); /* -> VERIFY, with the RP connected */
    CHECK(swap_fsm_state() == SWAP_VERIFY);

    /* I25: DFXCTL.RM_ID reads back a DIFFERENT id than the target (1) -- the
     * wrong RM landed (or a torn/incomplete load). Old behaviour (the
     * documented I25 gap) hardcoded verified=true regardless. A settled-but-
     * wrong id is decided immediately: no point polling for it to change. */
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_ID, 99u);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS, DFXCTL_RM_STATUS_RM_ID_VALID);
    swap_fsm_poll();
    /* R1: verification ran with the RP CONNECTED, so failing straight to
     * SWAP_FAILED would leave an unknown RM driving the shell. Re-isolate. */
    CHECK(swap_fsm_state() == SWAP_REISOLATE);
    /* The DECIDING poll only sets the next state; step_reisolate() does the
     * register writes on the poll that RUNS it. */

    swap_fsm_poll();                       /* step_reisolate() runs here */
    CHECK(swap_fsm_state() == SWAP_REISOLATE);  /* STATUS has not confirmed yet */

    /* ... and it has put the RP back in the box. */
    CHECK((mock_regs_peek(MPS3_DFXCTL_BASE, DFXCTL_DECOUPLE) & DFXCTL_DECOUPLE_EN) != 0);
    CHECK((mock_regs_peek(MPS3_DFXCTL_BASE, DFXCTL_SHUTDOWN) & DFXCTL_SHUTDOWN_AXI) != 0);
    CHECK((mock_regs_peek(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL)
           & CLKRST_RESET_CTRL_RP_RESETN) == 0);  /* rp held in reset */
    CHECK((mock_regs_peek(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL)
           & CLKRST_RESET_CTRL_DUT_RESETN) == 0); /* and a rejected DUT must not run */
    CHECK((mock_regs_peek(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL)
           & CLKRST_RESET_CTRL_DBG_RESETN) == 0); /* nor its DAP */
    CHECK(g_shell_state.xvc_gated);          /* re-gated */
    /* THE invariant: a rejected id is never committed. (What `ping` should
     * report after a failed swap is a separate, pre-existing question -- see
     * the note in step_reisolate().) */
    CHECK(g_shell_state.current_rm_id != 99u);

    /* It waits for STATUS to confirm the isolation before reporting failure. */
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,
                   DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET);
    swap_fsm_poll();
    CHECK(swap_fsm_state() == SWAP_FAILED);

    /* Safe-failure-mode invariant: FAILED => RP inert (decoupled + reset).
     * The channels are still gated AT THIS POINT (re-gated by REISOLATE, and
     * step_done_or_failed has not run yet) -- but they are UNGATED once FAILED
     * is processed to IDLE below, so the operator is not locked out. */
    CHECK((mock_regs_peek(MPS3_DFXCTL_BASE, DFXCTL_DECOUPLE) & DFXCTL_DECOUPLE_EN) != 0);
    CHECK(g_shell_state.xvc_gated);

    /* g_current_rm_clearing must be untouched (still describes what's really
     * loaded -- SWAP_CACHE_CLEARING never ran; it sits AFTER verify). */
    CHECK(g_current_rm_clearing.rm_id == 0u);

    swap_fsm_poll(); /* FAILED -> IDLE; step_done_or_failed(false) runs here */
    CHECK(swap_fsm_state() == SWAP_IDLE);
    /* UNGATE-ON-FAILURE: a swap that passed SWAP_GATE and then failed must NOT
     * leave the debug/console channels gated forever -- the inert RP is the
     * safety, not the gating. Before this, only the DONE arc ungated, so a
     * failed swap dropped every later OpenOCD/console connect with no recovery
     * but a reflash (found on silicon via swd_server closing 6920). */
    CHECK(!g_shell_state.xvc_gated);
    CHECK(!g_shell_state.swd_gated);
    CHECK(!g_shell_state.uart_gated);
}

/* Drive the FSM to SWAP_AWAIT_INCOMING_CLEARING and stop there. */
static void arm_to_await_clearing(void)
{
    reset_all();
    overlay_manifest_info_t greybox = { .static_id = 0xA1B2C3D4u, .rm_id = 0,
                                        .clear_len_words = 4, .clear_crc32 = 0x1111 };
    fake_overlay_store_set_greybox(&greybox, 0);
    swap_fsm_init();
    swap_fsm_start("nanosoc", "tcp");
    swap_fsm_poll();                                  /* GATE -> DECOUPLE_ASSERT */
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,
                   DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET);
    mock_regs_poke(MPS3_HWICAP_BASE, HWICAP_WFV, 1024u);
    swap_fsm_poll();                                  /* -> STREAM_CLEARING */
    swap_fsm_poll();                                  /* -> AWAIT_INCOMING_CLEARING */
    CHECK(swap_fsm_state() == SWAP_AWAIT_INCOMING_CLEARING);
}

static void test_await_idle_timeout_unwedges_a_dead_client(void)
{
    /* THE bug this closes: a client that died mid-swap parked the shell forever.
     * ICMP fine, 6900/6910 still accept(), but every write reset -- lwIP has no
     * keepalive and config_agent is single-session. Recovery needed a JTAG
     * bitstream reload. Observed on silicon 2026-07-09. */
    arm_to_await_clearing();

    swap_fsm_poll();                                  /* arms the idle timer */
    CHECK(swap_fsm_state() == SWAP_AWAIT_INCOMING_CLEARING);

    mock_time_advance_ms(MPS3_SWAP_AWAIT_IDLE_MS - 1);
    swap_fsm_poll();                                  /* one ms short -- still waiting */
    CHECK(swap_fsm_state() == SWAP_AWAIT_INCOMING_CLEARING);

    int aborts_before = fake_config_agent_abort_calls();

    mock_time_advance_ms(2);
    swap_fsm_poll();
    CHECK(swap_fsm_state() == SWAP_FAILED);

    /* pre-RELEASE, so the RP was never connected: it is already inert. */
    CHECK((mock_regs_peek(MPS3_DFXCTL_BASE, DFXCTL_DECOUPLE) & DFXCTL_DECOUPLE_EN) != 0);
    CHECK(g_shell_state.xvc_gated);

    /* THE other half of the wedge. Aborting the swap is not enough: the dead
     * peer's TCP session must be released too, or config_agent's ONE session
     * stays held (lwIP has no keepalive, and a client killed without FIN never
     * closes it) and every later client is refused. */
    swap_fsm_poll();   /* FAILED -> IDLE, where step_done_or_failed() runs */
    CHECK(fake_config_agent_abort_calls() > aborts_before);
}

static void test_rx_progress_rearms_the_idle_timeout(void)
{
    /* A slow-but-progressing 1.31 MB upload must NEVER be killed. This is why
     * it is an idle timeout and not a deadline. */
    arm_to_await_clearing();
    swap_fsm_poll();

    for (int i = 0; i < 5; i++) {
        mock_time_advance_ms(MPS3_SWAP_AWAIT_IDLE_MS - 1);  /* almost expire ... */
        fake_config_agent_set_rx_progress((uint32_t)(i + 1) * 4096u, 1312536u);
        swap_fsm_poll();                                    /* ... but bytes arrived */
        CHECK(swap_fsm_state() == SWAP_AWAIT_INCOMING_CLEARING);
    }

    /* Total elapsed is now ~5x the idle bound, yet we are still waiting: the
     * timer tracks SILENCE, not wall-clock. Now go quiet and it must fire. */
    mock_time_advance_ms(MPS3_SWAP_AWAIT_IDLE_MS + 1);
    swap_fsm_poll();
    CHECK(swap_fsm_state() == SWAP_FAILED);
}

static void test_icap_direct_refuses_a_partial_when_not_awaiting_one(void)
{
    /* SILICON BUG, 2026-07-10. icap_direct_begin() checked only that the HARDWARE
     * was parked (DFXCTL.STATUS = decoupled + rp_in_reset). But a swap that fails
     * BEFORE release leaves DECOUPLE asserted and rp_resetn held -- so STATUS
     * still reads parked, and a partial pushed after the rejection was written
     * straight into the fabric: 1,083,360 bytes of an unverified RM over an
     * un-cleared RP, while the shell still reported the previous rm_id.
     *
     * "The hardware is parked" and "the FSM is expecting a partial" are not the
     * same statement. Only the second one licenses an ICAP write. */
    const struct mps3_cfg_agent_qspi_sink *sink = swap_fsm_icap_direct_sink();
    CHECK(sink != NULL);

    reset_all();
    overlay_manifest_info_t greybox = { .static_id = 0xA1B2C3D4u, .rm_id = 0,
                                        .clear_len_words = 4, .clear_crc32 = 0x1111 };
    fake_overlay_store_set_greybox(&greybox, 0);
    swap_fsm_init();

    /* Park the hardware exactly as a failed-pre-release swap leaves it. */
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,
                   DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET);

    /* IDLE: no swap at all. The old gate would have accepted this. */
    CHECK(swap_fsm_state() == SWAP_IDLE);
    CHECK(sink->begin(1024) != 0);

    /* And after a swap has FAILED, with DECOUPLE still asserted. */
    swap_fsm_start("nanosoc", "tcp");
    swap_fsm_poll();                       /* GATE -> DECOUPLE_ASSERT */
    swap_fsm_poll();                       /* -> STREAM_CLEARING */
    fake_overlay_store_set_greybox(NULL, -1);
    g_current_rm_clearing.valid = false;   /* the real trigger: nothing to clear with */
    swap_fsm_poll();                       /* clearing_cache_valid=0 -> FAILED */
    CHECK(swap_fsm_state() == SWAP_FAILED);

    /* Hardware STILL looks parked -- that is the whole trap. */
    uint32_t st = mock_regs_peek(MPS3_DFXCTL_BASE, DFXCTL_STATUS);
    CHECK((st & (DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET))
          == (DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET));
    CHECK(sink->begin(1024) != 0);         /* ...and the partial is refused anyway */
}

/* ==========================================================================
 * D13: the INTERNAL swap source "usd" (the power-on load from the user
 * microSD), against the scripted store (fake_overlay_store.c). This retargets
 * the two QSPI clearing-PROMOTE tests that went with the SST26 backend: the
 * multi-poll resident-clearing copy is now the card read into the RAM arena at
 * SWAP_CACHE_CLEARING, and its wedge guard is the same bound. The real store
 * end to end (fake_usd + usd.c + ovlstore_sd) is test_usd_boot.c.
 * ========================================================================== */

#define USD_RM       0x0100001Eu
#define USD_CLEAR_B  64u
#define USD_PART_B   96u
static uint8_t  s_usd_clear[USD_CLEAR_B], s_usd_part[USD_PART_B];
static uint32_t s_wf[256];
static int      s_wf_n;
static const uint8_t k_grey_bytes[16] = { 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16 };
static uint32_t s_usd_clear_len = USD_CLEAR_B;   /* the default's clear_len (a test may lie) */

static int usd_hwicap_hook(void *ctx, int is_write, uint32_t base, uint32_t off, uint32_t *val)
{
    (void)ctx; (void)base;
    if (is_write && off == HWICAP_WF) {
        if (s_wf_n < (int)(sizeof s_wf / sizeof s_wf[0])) {
            s_wf[s_wf_n] = *val;
        }
        s_wf_n++;
        return 1;
    }
    if (!is_write && off == HWICAP_CR) {
        *val = 0;   /* LITE: the WRITE bit self-clears at once */
        return 1;
    }
    return 0;
}

/* A usd swap armed with the scripted default, driven to STREAM_PARTIAL (the
 * outgoing greybox clearing has streamed; the card's pair has been taken). */
static void arm_usd_to_stream_partial(uint32_t chunk, uint32_t busy_every)
{
    reset_all();
    for (uint32_t i = 0; i < USD_CLEAR_B; i++) s_usd_clear[i] = (uint8_t)(0xC0u + i);
    for (uint32_t i = 0; i < USD_PART_B; i++)  s_usd_part[i]  = (uint8_t)(0x10u + i);
    s_wf_n = 0;
    overlay_manifest_info_t greybox = { .static_id = 0xA1B2C3D4u, .rm_id = 0,
                                        .clear_len_words = 4, .clear_crc32 = 0x1111,
                                        .clear_data = k_grey_bytes };
    fake_overlay_store_set_greybox(&greybox, 0);
    ovlstore_sd_desc_t d = { .rm_id = USD_RM, .static_id = 0xA1B2C3D4u,
                             .clear_len = s_usd_clear_len, .clear_crc = 0xAAAAu,
                             .part_len = USD_PART_B, .part_crc = 0xBBBBu };
    fake_overlay_store_set_src(&d, OVLSD_OK);
    fake_overlay_store_set_src_bytes(s_usd_clear, s_usd_part, chunk, busy_every);
    swap_fsm_init();
    mock_regs_set_hook(MPS3_HWICAP_BASE, usd_hwicap_hook, 0);
    mock_regs_poke(MPS3_HWICAP_BASE, HWICAP_WFV, 1024u);
    /* DECOYS: a pair staged in config_agent must be IGNORED by a usd swap. */
    config_agent_bitstream_info_t decoy = { .rm_id = 0x99u, .static_id = 0xA1B2C3D4u,
                                            .len_words = 4, .crc32 = 0x9999 };
    fake_config_agent_arm_clearing(&decoy);
    fake_config_agent_arm_partial(&decoy);
    CHECK(swap_fsm_start("led", "usd") == 0);

    swap_fsm_poll();                                  /* GATE -> DECOUPLE_ASSERT */
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,
                   DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET);
    swap_fsm_poll();                                  /* -> STREAM_CLEARING (the greybox) */
    swap_fsm_poll();                                  /* 4 words -> AWAIT_INCOMING_CLEARING */
    CHECK(s_wf_n == 4);                               /* the OUTGOING clearing, as for a net swap */
    swap_fsm_poll();                                  /* usd: the card's pair -> AWAIT_PARTIAL */
    CHECK(swap_fsm_state() == SWAP_AWAIT_PARTIAL);
    swap_fsm_poll();                                  /* -> STREAM_PARTIAL */
    CHECK(swap_fsm_state() == SWAP_STREAM_PARTIAL);
    {   /* config_agent was NOT consulted: both decoys are still staged. */
        config_agent_bitstream_info_t got;
        CHECK(config_agent_take_validated_clearing(&got) == 0 && got.rm_id == 0x99u);
        CHECK(config_agent_take_validated_partial(&got) == 0 && got.rm_id == 0x99u);
    }
    s_wf_n = 0;
}

static void finish_usd_swap_to_cache(uint32_t rm)
{
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS, 0u);   /* release confirms */
    swap_fsm_poll();                                       /* RELEASE -> VERIFY */
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_ID, rm);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS, DFXCTL_RM_STATUS_RM_ID_VALID);
    swap_fsm_poll();                                       /* -> CACHE_CLEARING */
    CHECK(swap_fsm_state() == SWAP_CACHE_CLEARING);
}

static void test_usd_source_full_swap(void)
{
    printf("- usd source: partial from the card to HWICAP, clearing into the arena\n");
    arm_usd_to_stream_partial(/*chunk=*/20u, /*busy_every=*/2u);

    /* The partial streams a buffer at a time; BUSY polls make no progress. */
    int polls = 0;
    while (swap_fsm_state() == SWAP_STREAM_PARTIAL && polls < 1000) {
        swap_fsm_poll();
        polls++;
    }
    CHECK(swap_fsm_state() == SWAP_RELEASE);
    CHECK(polls > (int)(USD_PART_B / 20u));           /* spanned many polls */
    CHECK(fake_overlay_store_src_begins(OVLSD_PARTIAL) == 1);
    CHECK(!fake_overlay_store_src_open());             /* the store closed it: DONE */
    CHECK(s_wf_n == (int)(USD_PART_B / 4u));           /* every word, exactly once */
    for (int i = 0; i < s_wf_n; i++) {                 /* in order, the card's bytes */
        CHECK(s_wf[i] == mps3_hwicap_pack_word(&s_usd_part[4 * i]));
    }

    finish_usd_swap_to_cache(USD_RM);
    CHECK(g_shell_state.current_rm_id == USD_RM);      /* the commit point */

    /* SWAP_CACHE_CLEARING reads the slot's CLEARING into the arena, across
     * polls; nothing points at the card afterwards. */
    polls = 0;
    while (swap_fsm_state() == SWAP_CACHE_CLEARING && polls < 1000) {
        swap_fsm_poll();
        polls++;
    }
    CHECK(swap_fsm_state() == SWAP_DONE);
    CHECK(polls > 2);
    CHECK(fake_overlay_store_src_begins(OVLSD_CLEARING) == 1);
    CHECK(g_current_rm_clearing.valid);
    CHECK(g_current_rm_clearing.rm_id == USD_RM);
    CHECK(g_current_rm_clearing.len_words == USD_CLEAR_B / 4u);
    CHECK(g_current_rm_clearing.data == swap_fsm_clearing_stage_buffer(NULL));
    CHECK(memcmp(g_current_rm_clearing.data, s_usd_clear, USD_CLEAR_B) == 0);
    CHECK(!g_shell_state.xvc_gated);
    swap_fsm_poll();                                   /* DONE -> IDLE */
    CHECK(swap_fsm_state() == SWAP_IDLE);
    CHECK(swap_fsm_last_result()->ok);
    CHECK(fake_overlay_store_src_aborts() == 0);
}

static void test_usd_source_bad_partial_fails_closed(void)
{
    printf("- usd source: a card that goes bad mid-partial fails the swap CLOSED\n");
    /* The store withholds the last buffer on a re-read CRC mismatch and ends the
     * stream with an error: the FSM must fail, parked, and never RELEASE. */
    arm_usd_to_stream_partial(16u, 0u);
    fake_overlay_store_set_src_fail(OVLSD_PARTIAL, (int32_t)(USD_PART_B - 16u), OVLSD_ECRC);
    int polls = 0;
    while (swap_fsm_state() == SWAP_STREAM_PARTIAL && polls < 1000) {
        swap_fsm_poll();
        polls++;
    }
    CHECK(swap_fsm_state() == SWAP_FAILED);
    CHECK(s_wf_n < (int)(USD_PART_B / 4u));             /* never the whole bitstream */
    uint32_t st = mock_regs_peek(MPS3_DFXCTL_BASE, DFXCTL_STATUS);
    CHECK((st & (DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET))
          == (DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET));
    swap_fsm_poll();                                    /* FAILED -> IDLE */
    CHECK(swap_fsm_state() == SWAP_IDLE);
    CHECK(!swap_fsm_last_result()->ok);
    CHECK(strcmp(swap_fsm_fail_tag(), "stream_partial") == 0);
    CHECK(swap_fsm_fail_state() == SWAP_STREAM_PARTIAL);
    CHECK(!fake_overlay_store_src_open());
    CHECK(g_current_rm_clearing.rm_id == 0u);           /* never promoted */
}

static void test_usd_source_no_default_fails_before_the_partial(void)
{
    printf("- usd source: no VALID default when the pair is taken -> FAILED, nothing streamed\n");
    arm_usd_to_stream_partial(16u, 0u);      /* gets us a clean baseline ... */
    reset_all();                             /* ... then re-arm with NO default */
    overlay_manifest_info_t greybox = { .static_id = 0xA1B2C3D4u, .rm_id = 0,
                                        .clear_len_words = 4, .clear_crc32 = 0x1111,
                                        .clear_data = k_grey_bytes };
    fake_overlay_store_set_greybox(&greybox, 0);
    swap_fsm_init();
    mock_regs_set_hook(MPS3_HWICAP_BASE, usd_hwicap_hook, 0);
    mock_regs_poke(MPS3_HWICAP_BASE, HWICAP_WFV, 1024u);
    s_wf_n = 0;
    CHECK(swap_fsm_start("led", "usd") == 0);
    swap_fsm_poll();
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,
                   DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET);
    swap_fsm_poll();
    swap_fsm_poll();                         /* the greybox clearing (4 words) */
    swap_fsm_poll();                         /* no default -> FAILED */
    CHECK(swap_fsm_state() == SWAP_FAILED);
    CHECK(s_wf_n == 4);                      /* not one word of an incoming pair */
    CHECK(fake_overlay_store_src_begins(OVLSD_PARTIAL) == 0);
    swap_fsm_poll();
    CHECK(strcmp(swap_fsm_fail_tag(), "await_clearing") == 0);
}

static void test_usd_cache_wedge_fails_closed(void)
{
    printf("- usd source: a clearing read that never ends is BOUNDED and fails the cache closed\n");
    /* The wedge guard the QSPI promote had, on the card read: a store that
     * answers BUSY forever must not park the FSM in SWAP_CACHE_CLEARING. The
     * swap itself verified, so it still reaches DONE -- with the resident
     * clearing INVALID, so a later swap-away fails closed. */
    arm_usd_to_stream_partial(1024u, 0u);
    while (swap_fsm_state() == SWAP_STREAM_PARTIAL) {
        swap_fsm_poll();
    }
    finish_usd_swap_to_cache(USD_RM);
    fake_overlay_store_set_src_bytes(s_usd_clear, s_usd_part, 1024u, 0xFFFFFFFFu); /* BUSY ~forever */
    int polls = 0;
    const int LIMIT = 1000000;
    while (swap_fsm_state() == SWAP_CACHE_CLEARING && polls < LIMIT) {
        swap_fsm_poll();
        polls++;
    }
    CHECK(swap_fsm_state() == SWAP_DONE);
    CHECK(polls > 1000 && polls < LIMIT);
    CHECK(!g_current_rm_clearing.valid);
    CHECK(fake_overlay_store_src_aborts() == 1);   /* the stream was closed */
    CHECK(!fake_overlay_store_src_open());
    swap_fsm_poll();
    CHECK(swap_fsm_state() == SWAP_IDLE);

    /* ...and a clearing too big for the arena is never read at all. */
    s_usd_clear_len = 0x7FFFFFFCu;
    arm_usd_to_stream_partial(1024u, 0u);
    s_usd_clear_len = USD_CLEAR_B;
    while (swap_fsm_state() == SWAP_STREAM_PARTIAL) {
        swap_fsm_poll();
    }
    finish_usd_swap_to_cache(USD_RM);
    swap_fsm_poll();
    CHECK(swap_fsm_state() == SWAP_DONE);
    CHECK(fake_overlay_store_src_begins(OVLSD_CLEARING) == 0);
    CHECK(!g_current_rm_clearing.valid);
}

/* The front panel's programming progress (swap_fsm_progress(), HM R5): inactive
 * when idle; from swap_fsm_start() the ICAP byte delta and a total that is 0
 * until the partial's length is known -- first the push's declared length while
 * it arrives, then the validated header's -- plus the outgoing clearing. */
static void test_progress_snapshot(void)
{
    reset_all();
    overlay_manifest_info_t greybox = { .static_id = 0xA1B2C3D4u, .rm_id = 0, .clear_len_words = 4, .clear_crc32 = 0x1111 };
    fake_overlay_store_set_greybox(&greybox, 0);
    swap_fsm_init();
    mps3_swap_progress_t pg;
    swap_fsm_progress(&pg);
    CHECK(!pg.active && pg.state == SWAP_IDLE && pg.rm && pg.rm[0] == '\0' && pg.total == 0u);

    uint32_t base = mps3_hwicap_bytes();
    CHECK(swap_fsm_start("nanosoc", "tftp") == 0);
    swap_fsm_progress(&pg);
    CHECK(pg.active && pg.state == SWAP_GATE && strcmp(pg.rm, "nanosoc") == 0);
    CHECK(pg.done == 0u && pg.total == 0u);          /* the partial's length: not known */

    swap_fsm_poll();
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS, DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET);
    mock_regs_poke(MPS3_HWICAP_BASE, HWICAP_WFV, 1024u);
    swap_fsm_poll();
    swap_fsm_poll();                                  /* the outgoing clearing streams */
    CHECK(swap_fsm_state() == SWAP_AWAIT_INCOMING_CLEARING);
    swap_fsm_progress(&pg);
    CHECK(pg.done == mps3_hwicap_bytes() - base && pg.total == 0u);

    config_agent_bitstream_info_t incoming_clear = {
        .rm_id = 1, .static_id = 0xA1B2C3D4u, .len_words = 8, .crc32 = 0x2222,
    };
    fake_config_agent_arm_clearing(&incoming_clear);
    swap_fsm_poll();
    CHECK(swap_fsm_state() == SWAP_AWAIT_PARTIAL);
    fake_config_agent_set_rx_progress(4096u, 1312536u);  /* the partial arriving */
    swap_fsm_progress(&pg);
    CHECK(pg.state == SWAP_AWAIT_PARTIAL && pg.total == 16u + 1312536u);

    config_agent_bitstream_info_t incoming_partial = {
        .rm_id = 1, .static_id = 0xA1B2C3D4u, .len_words = 4, .crc32 = 0x3333,
    };
    fake_config_agent_arm_partial(&incoming_partial);
    swap_fsm_poll();
    CHECK(swap_fsm_state() == SWAP_STREAM_PARTIAL);
    swap_fsm_progress(&pg);
    CHECK(pg.total == 16u + 16u);                     /* the validated header's length */
    swap_fsm_poll();
    swap_fsm_progress(&pg);
    CHECK(pg.active && pg.done == mps3_hwicap_bytes() - base);
    CHECK(pg.done <= pg.total);
}

int main(void)
{
    test_happy_path_full_swap();
    test_progress_snapshot();
    test_await_idle_timeout_unwedges_a_dead_client();
    test_icap_direct_refuses_a_partial_when_not_awaiting_one();
    test_rx_progress_rearms_the_idle_timeout();
    test_fails_closed_when_clearing_cache_invalid();
    test_i25_verify_mismatch_fails_and_stays_decoupled();
    test_usd_source_full_swap();
    test_usd_source_bad_partial_fails_closed();
    test_usd_source_no_default_fails_before_the_partial();
    test_usd_cache_wedge_fails_closed();

    printf("test_swap_fsm_hw: %d checks passed\n", s_checks);
    return 0;
}
