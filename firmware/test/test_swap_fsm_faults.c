/*
 * test_swap_fsm_faults.c — the swap FSM's FAIL-OPEN and FAIL-STUCK closures
 * (the ones a four-part audit flagged as untested), driven through
 * coordinator/swap_fsm.c's real impure step_*() functions against
 * firmware/test/mock_regs.c, exactly like test_swap_fsm_hw.c but exercising
 * the branches no happy-path test reaches:
 *
 *   1. FAIL-OPEN -> fail-closed (top safety fix): a HWICAP whose per-word
 *      StartConfig CR.WRITE never self-clears (stuck core). The writer
 *      (common/hwicap_writer.c hwicap_lite_write(); this binary is a LITE build)
 *      used to spin to its cap then return AS IF THE WORD WROTE, so a dead ICAP
 *      silently "streamed" a clearing/partial and the swap went on to "verify"
 *      a load that never happened. The FSM must now fail CLOSED (SWAP_FAILED,
 *      RP left decoupled) rather than advancing. This binary asserts the FSM's
 *      half of that (the writer's own half -- both modes, plus what `nwritten`
 *      reports on a stall -- is firmware/test/test_hwicap_writer.c).
 *
 *   2. FAIL-STUCK (DECOUPLE_ASSERT): a DFX decoupler that never confirms
 *      DECOUPLED+RP_IN_RESET used to park the client forever (inline TODO). The
 *      bounded confirm poll must now time out -> SWAP_FAILED.
 *
 *   3. FAIL-STUCK (SWAP_RELEASE): a release that never confirms decoupled==0 &&
 *      rp_in_reset==0 used to spin forever. Bounded -> SWAP_FAILED.
 *
 * Links (same set as test_swap_fsm_hw.c): swap_fsm.c, swap_fsm_transitions.c,
 * mock_regs.c, fake_config_agent.c, fake_overlay_store.c. -DMPS3_HAL_MOCK.
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

mps3_shell_state_t g_shell_state; /* swap_fsm.c's extern -- defined here */

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

/* The confirm-poll ceiling swap_fsm.c uses (MPS3_SWAP_CONFIRM_POLL_MAX). Kept
 * in step with that file; the tests loop a little past it and assert the FSM
 * has failed by then. If that ceiling ever changes, bump this to match. */
#define CONFIRM_BOUND 100000

/* ---- a HWICAP mock that HOLDS CR.WRITE set (a stuck lite core) ------------- */
static int s_hwicap_stuck;
static int s_wf_writes;

static int hwicap_stuck_hook(void *ctx, int is_write, uint32_t base, uint32_t off, uint32_t *val)
{
    (void)ctx; (void)base;
    if (is_write && off == HWICAP_WF) {
        s_wf_writes++;
        return 1; /* swallow the word */
    }
    if (!is_write && off == HWICAP_CR) {
        /* Stuck: the StartConfig WRITE bit never self-clears, so the writer's
         * bounded CR poll (common/hwicap_writer.c) must give up and report a
         * failed write. When not stuck, model the healthy instant drain. */
        *val = s_hwicap_stuck ? HWICAP_CR_WRITE : 0u;
        return 1;
    }
    return 0; /* WFV/SR etc -> plain mock slots */
}

static void reset_all(void)
{
    mock_regs_reset();
    fake_config_agent_reset();
    g_shell_state = (mps3_shell_state_t){0};
    s_hwicap_stuck = 0;
    s_wf_writes = 0;
}

/* ====================================================================== */

static void test_stuck_hwicap_fails_swap_not_silently_verifies(void)
{
    reset_all();
    mock_regs_set_hook(MPS3_HWICAP_BASE, hwicap_stuck_hook, 0);
    mock_regs_poke(MPS3_HWICAP_BASE, HWICAP_WFV, 1024u);

    /* A greybox clearing WITH real bytes so step_stream_clearing actually
     * pushes words (a NULL-data fake would only advance counters and never
     * reach the writer). */
    static uint8_t clr[16];
    for (int i = 0; i < 16; i++) clr[i] = (uint8_t)(0xA0 + i);
    overlay_manifest_info_t greybox = {
        .static_id = 0xA1B2C3D4u, .rm_id = 0,
        .clear_len_words = 4, .clear_crc32 = 0, .clear_data = clr,
    };
    fake_overlay_store_set_greybox(&greybox, 0);
    swap_fsm_init();
    CHECK(g_current_rm_clearing.valid && g_current_rm_clearing.data == clr);

    CHECK(swap_fsm_start("nanosoc", "tcp") == 0);
    swap_fsm_poll(); /* GATE -> DECOUPLE_ASSERT */
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,
                   DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET);
    swap_fsm_poll(); /* -> STREAM_CLEARING */
    CHECK(swap_fsm_state() == SWAP_STREAM_CLEARING);

    /* Now the ICAP is stuck: the first word's write never completes. */
    s_hwicap_stuck = 1;
    swap_fsm_poll(); /* push word0 -> CR poll never clears -> stream_error -> FAILED */
    CHECK(swap_fsm_state() == SWAP_FAILED);
    CHECK(s_wf_writes >= 1); /* it DID try to write (not a no-op path) */

    /* Fail-closed: DECOUPLE still asserted, servers never ungated, and the
     * last-result reports failure (NOT a bogus "verified"). */
    CHECK((mock_regs_peek(MPS3_DFXCTL_BASE, DFXCTL_DECOUPLE) & DFXCTL_DECOUPLE_EN) != 0);
    CHECK(g_shell_state.xvc_gated && g_shell_state.swd_gated);

    swap_fsm_poll(); /* FAILED -> IDLE (records the result) */
    CHECK(swap_fsm_state() == SWAP_IDLE);
    CHECK(swap_fsm_last_result()->valid && !swap_fsm_last_result()->ok &&
          !swap_fsm_last_result()->verified);
}

/* Drive an otherwise-healthy swap into a target state with NULL-data fakes (so
 * no HWICAP words are actually pushed — this test is about the DFXCTL confirm
 * waits, not streaming). */
static void seed_greybox_and_start(void)
{
    overlay_manifest_info_t greybox = {
        .static_id = 0xA1B2C3D4u, .rm_id = 0,
        .clear_len_words = 1, .clear_crc32 = 0, .clear_data = 0,
    };
    fake_overlay_store_set_greybox(&greybox, 0);
    swap_fsm_init();
    CHECK(swap_fsm_start("nanosoc", "tcp") == 0);
    swap_fsm_poll(); /* GATE -> DECOUPLE_ASSERT */
    CHECK(swap_fsm_state() == SWAP_DECOUPLE_ASSERT);
}

static void test_decouple_never_confirms_times_out(void)
{
    reset_all();
    mock_regs_poke(MPS3_HWICAP_BASE, HWICAP_WFV, 1024u);
    seed_greybox_and_start();

    /* DFXCTL.STATUS is left at 0 (decoupler never confirms). A handful of
     * polls must NOT trip the timeout (it genuinely waits), ... */
    for (int i = 0; i < 8; i++) swap_fsm_poll();
    CHECK(swap_fsm_state() == SWAP_DECOUPLE_ASSERT);

    /* ...but once the bounded confirm poll expires it fails closed. */
    int i;
    for (i = 0; i < CONFIRM_BOUND + 16 && swap_fsm_state() == SWAP_DECOUPLE_ASSERT; i++) {
        swap_fsm_poll();
    }
    CHECK(swap_fsm_state() == SWAP_FAILED);
    /* RP still isolated (DECOUPLE asserted every poll of this state). */
    CHECK((mock_regs_peek(MPS3_DFXCTL_BASE, DFXCTL_DECOUPLE) & DFXCTL_DECOUPLE_EN) != 0);
    CHECK(g_shell_state.xvc_gated);

    swap_fsm_poll(); /* FAILED -> IDLE */
    CHECK(swap_fsm_state() == SWAP_IDLE);
    CHECK(!swap_fsm_last_result()->ok);
}

static void drive_to_release(void)
{
    /* Healthy up to RELEASE using NULL-data fakes (no HWICAP writes needed). */
    mock_regs_poke(MPS3_HWICAP_BASE, HWICAP_WFV, 1024u);
    seed_greybox_and_start();
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,
                   DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET);
    swap_fsm_poll(); /* -> STREAM_CLEARING */
    swap_fsm_poll(); /* 1 word, NULL data -> done -> AWAIT_INCOMING_CLEARING */
    config_agent_bitstream_info_t clr = { .rm_id = 1, .static_id = 0xA1B2C3D4u, .len_words = 1 };
    fake_config_agent_arm_clearing(&clr);
    swap_fsm_poll(); /* -> AWAIT_PARTIAL */
    config_agent_bitstream_info_t part = { .rm_id = 1, .static_id = 0xA1B2C3D4u, .len_words = 1 };
    fake_config_agent_arm_partial(&part);
    swap_fsm_poll(); /* -> STREAM_PARTIAL */
    swap_fsm_poll(); /* -> VERIFY */
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_ID, 1u);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS, DFXCTL_RM_STATUS_RM_ID_VALID);
    swap_fsm_poll(); /* -> CACHE_CLEARING */
    swap_fsm_poll(); /* -> RELEASE */
    CHECK(swap_fsm_state() == SWAP_RELEASE);
}

static void test_release_never_confirms_times_out(void)
{
    reset_all();
    drive_to_release();

    /* DFXCTL.STATUS is deliberately LEFT at decoupled+rp_in_reset, so the
     * release never confirms (STATUS never drops to 0). A few polls wait... */
    for (int i = 0; i < 8; i++) swap_fsm_poll();
    CHECK(swap_fsm_state() == SWAP_RELEASE);

    /* ...then the bounded poll expires -> SWAP_FAILED (was an infinite spin). */
    int i;
    for (i = 0; i < CONFIRM_BOUND + 16 && swap_fsm_state() == SWAP_RELEASE; i++) {
        swap_fsm_poll();
    }
    CHECK(swap_fsm_state() == SWAP_FAILED);

    swap_fsm_poll(); /* FAILED -> IDLE */
    CHECK(swap_fsm_state() == SWAP_IDLE);
    CHECK(!swap_fsm_last_result()->ok);
}

/* Drive a healthy swap up to SWAP_VERIFY (target rm_id = 1), leaving the RM_ID
 * verify inputs UNSET so the caller controls the verify outcome. Mirrors
 * drive_to_release() but stops one step earlier. */
static void drive_to_verify(void)
{
    mock_regs_poke(MPS3_HWICAP_BASE, HWICAP_WFV, 1024u);
    seed_greybox_and_start();
    /* Poll to VERIFY, injecting the decouple confirm and arming the incoming
     * clearing/partial (rm_id=1, NULL data) as each state is reached — robust to
     * the exact per-state poll count. STATUS is left isolated so a later
     * REISOLATE confirms. */
    int armed_clr = 0, armed_part = 0;
    for (int i = 0; i < 64 && swap_fsm_state() != SWAP_VERIFY; i++) {
        switch (swap_fsm_state()) {
        case SWAP_DECOUPLE_ASSERT:
            mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,
                           DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET);
            break;
        case SWAP_AWAIT_INCOMING_CLEARING:
            if (!armed_clr) {
                config_agent_bitstream_info_t clr = { .rm_id = 1, .static_id = 0xA1B2C3D4u, .len_words = 1 };
                fake_config_agent_arm_clearing(&clr); armed_clr = 1;
            }
            break;
        case SWAP_AWAIT_PARTIAL:
            if (!armed_part) {
                config_agent_bitstream_info_t part = { .rm_id = 1, .static_id = 0xA1B2C3D4u, .len_words = 1 };
                fake_config_agent_arm_partial(&part); armed_part = 1;
            }
            break;
        case SWAP_RELEASE:
            /* R1 order: RELEASE precedes VERIFY. Confirm the release (STATUS
             * drops to 0 = reconnected) so the FSM advances to VERIFY with the
             * RP CONNECTED -- the precondition step_reisolate() has to undo. */
            mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS, 0u);
            break;
        default: break;
        }
        swap_fsm_poll();
    }
    CHECK(swap_fsm_state() == SWAP_VERIFY);
}

/* R1 reorder coverage: a verify MISMATCH (settled rm_id != the partial's target)
 * takes VERIFY -> REISOLATE -> FAILED. step_reisolate() was entirely uncovered:
 * it re-asserts DECOUPLE + holds all resets (an RM that failed verification must
 * not be left running, and the mismatch happens with the RP connected) before the
 * swap reports failure. */
static void test_verify_mismatch_reisolates_then_fails(void)
{
    reset_all();
    drive_to_verify();

    /* Settled (id_valid=1) but WRONG rm_id: 0x99 != target 1 -> verify_mismatch. */
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_ID, 0x99u);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS, DFXCTL_RM_STATUS_RM_ID_VALID);
    swap_fsm_poll(); /* VERIFY: mismatch -> REISOLATE */
    CHECK(swap_fsm_state() == SWAP_REISOLATE);

    /* step_reisolate() re-asserts DECOUPLE + holds resets; model the decoupler
     * confirming the re-isolation (STATUS reads isolated) so it fails closed. */
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,
                   DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET);
    swap_fsm_poll(); /* REISOLATE (decouple_confirmed) -> FAILED */
    CHECK(swap_fsm_state() == SWAP_FAILED);

    /* Re-isolation actually happened: DECOUPLE re-asserted and ALL three resets
     * held (rp/dut/dbg resetn cleared) -> the mis-verified RM is left inert. */
    CHECK((mock_regs_peek(MPS3_DFXCTL_BASE, DFXCTL_DECOUPLE) & DFXCTL_DECOUPLE_EN) != 0);
    CHECK((mock_regs_peek(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL) &
           (CLKRST_RESET_CTRL_RP_RESETN | CLKRST_RESET_CTRL_DUT_RESETN |
            CLKRST_RESET_CTRL_DBG_RESETN)) == 0);

    swap_fsm_poll(); /* FAILED -> IDLE */
    CHECK(swap_fsm_state() == SWAP_IDLE);
    CHECK(swap_fsm_last_result()->valid && !swap_fsm_last_result()->ok &&
          !swap_fsm_last_result()->verified);
}

/* FAIL-STUCK (SWAP_REISOLATE), symmetric to the DECOUPLE/RELEASE timeouts: if the
 * re-isolation never confirms (STATUS never reads isolated), the bounded confirm
 * poll must still fail closed rather than parking the FSM forever. */
static void test_reisolate_never_confirms_times_out(void)
{
    reset_all();
    drive_to_verify();

    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_ID, 0x99u);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS, DFXCTL_RM_STATUS_RM_ID_VALID);
    swap_fsm_poll(); /* VERIFY -> REISOLATE */
    CHECK(swap_fsm_state() == SWAP_REISOLATE);

    /* Re-isolation never confirms: STATUS reads NOT isolated. */
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS, 0u);
    for (int i = 0; i < 8; i++) swap_fsm_poll();
    CHECK(swap_fsm_state() == SWAP_REISOLATE); /* still waiting */

    int i;
    for (i = 0; i < CONFIRM_BOUND + 16 && swap_fsm_state() == SWAP_REISOLATE; i++) {
        swap_fsm_poll();
    }
    CHECK(swap_fsm_state() == SWAP_FAILED);

    swap_fsm_poll(); /* FAILED -> IDLE */
    CHECK(swap_fsm_state() == SWAP_IDLE);
    CHECK(!swap_fsm_last_result()->ok);
}

int main(void)
{
    test_stuck_hwicap_fails_swap_not_silently_verifies();
    test_decouple_never_confirms_times_out();
    test_release_never_confirms_times_out();
    test_verify_mismatch_reisolates_then_fails();
    test_reisolate_never_confirms_times_out();

    printf("test_swap_fsm_faults: %d checks passed\n", s_checks);
    return 0;
}
