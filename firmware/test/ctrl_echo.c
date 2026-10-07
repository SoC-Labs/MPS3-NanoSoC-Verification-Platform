/*
 * ctrl_echo.c — stdin/stdout echo harness for the cross-language golden
 * test (tests/firmware_logic/test_json_golden.py). NOT a self-checking
 * test binary (it is not in the Makefile's $(TESTS)); it is the C half of
 * E2E-2: pyverify's real ShellClient generates request lines, this tool
 * runs each through the REAL firmware path -- net_proto.c decode ->
 * coordinator.c dispatch/handlers -> clkrst.c / swap_fsm.c against
 * mock_regs + module fakes -> net_proto.c per-op encode -- and prints the
 * response line, which the host then parses back into its response
 * dataclasses. One line in, one line out, exactly the TCP-6900 framing
 * minus the socket.
 *
 * Deferred-swap handling: coordinator_dispatch_line() returns 0 for an
 * accepted swap (held response, see coordinator.h). A real shell parks
 * the pcb and finishes the FSM from the main loop; this tool stands in
 * for that by pumping swap_fsm_poll() with the same mock-register
 * choreography test_swap_fsm_hw.c uses, then emitting
 * coordinator_swap_final_response(). What appears on stdout is therefore
 * the single held response net-protocol.md's swap example shows.
 *
 * Fixed scenario (mirrors test_coordinator_dispatch.c so both suites
 * agree on expected bytes):
 *   static_id 0xa1b2c3d4; boot RM = greybox (rm_id 0); MMCM locked;
 *   no lockup; a pushed swap pair always carries rm_id 1 and verifies;
 *   the user microSD (v0.13) is EMPTY OF A CARD -- `usd` answers
 *   {"ok":true,"present":false,"state":"none","text":"none","boot":"none"} and
 *   every `commit` / `usd` action is refused {"ok":false,"err":"no card"}
 *   (fake_overlay_store.c's defaults: a deterministic line to compare);
 *   (TELEM is deliberately NOT seeded any more: `telemetry` no longer reads
 *   it. There is no power sensor on this platform, so the verb always answers
 *   {"ok":false,"err":"no power sensor","lockup":..} -- see
 *   coordinator_handle_telemetry(). It still reports the raw dut_lockup pin.)
 *   GENCHK counters seeded TX_CNT 1234 / RX_CNT 1230 / ERR_CNT 4 (distinct
 *   so a macgen tx/rx/err field mixup can't pass — the firmware only reads
 *   them; it does not model traffic).
 *   DUTEGR is PRESENT (-DMPS3_HAS_DUT_EGRESS in the Makefile) and its page is
 *   left all-zero, so `dutrx` answers the empty-FIFO line -- the one dutrx
 *   reply that is fully determined, and therefore the one the conformance
 *   suite can compare BYTE-FOR-BYTE (all thirteen keys, their order and their
 *   types). The opposite build -- no 0x44B2 slave, "dut_egress not present" --
 *   is pinned on the firmware side by test_coordinator_dispatch.c, which is
 *   built without the flag. Between them both arms of the verb are compared;
 *   with ctrl_echo on the OFF build only the error line would be.
 */
#include <stdio.h>
#include <string.h>
#include "../coordinator/coordinator.h"
#include "../coordinator/swap_fsm.h"
#include "../common/platform_regs.h"
#include "mock_regs.h"
#include "fake_config_agent.h"
#include "fake_overlay_store.h"
#include "../common/diag.h"

#define ECHO_STATIC_ID 0xA1B2C3D4u
#define ECHO_SWAP_RM_ID 1u

/* Strong override of coordinator.c's WEAK fallback (see coordinator.h). */
uint32_t mps3_shell_static_id(void)
{
    return ECHO_STATIC_ID;
}

/* Same choreography as test_coordinator_dispatch.c's pump_swap(): the
 * incoming pair "was already pushed" (net-protocol.md: the host pushes
 * the pair before issuing `swap`), DFXCTL/HWICAP react via pokes. */
static void pump_swap_to_completion(void)
{
    config_agent_bitstream_info_t incoming_clear = {
        .rm_id = ECHO_SWAP_RM_ID, .static_id = ECHO_STATIC_ID,
        .len_words = 8, .crc32 = 0x2222,
    };
    config_agent_bitstream_info_t incoming_partial = {
        .rm_id = ECHO_SWAP_RM_ID, .static_id = ECHO_STATIC_ID,
        .len_words = 4, .crc32 = 0x3333,
    };
    fake_config_agent_arm_clearing(&incoming_clear);
    fake_config_agent_arm_partial(&incoming_partial);
    mock_regs_poke(MPS3_HWICAP_BASE, HWICAP_WFV, 1024u);

    for (int i = 0; i < 64 && !swap_fsm_idle(); i++) {
        switch (swap_fsm_state()) {
        case SWAP_DECOUPLE_ASSERT:
            mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,
                           DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET);
            break;
        case SWAP_VERIFY:
            mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_ID, ECHO_SWAP_RM_ID);
            mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS,
                           DFXCTL_RM_STATUS_RM_ID_VALID);
            break;
        case SWAP_RELEASE:
            mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS, 0u);
            break;
        default:
            break;
        }
        swap_fsm_poll();
    }
}

int main(void)
{
    mock_regs_reset();
    fake_config_agent_reset();
    overlay_manifest_info_t greybox = {
        .static_id = ECHO_STATIC_ID, .rm_id = 0,
        .clear_len_words = 4, .clear_crc32 = 0x1111,
    };
    fake_overlay_store_reset();   /* no card: see the scenario above */
    fake_overlay_store_set_greybox(&greybox, 0);
    coordinator_init();

    /* Register readbacks for the fixed scenario. */
    mock_regs_poke(MPS3_CLKRST_BASE, CLKRST_STATUS, CLKRST_STATUS_MMCM_LOCKED);
    mock_regs_poke(MPS3_TELEM_BASE, TELEM_BUS_MV, 1200u);
    mock_regs_poke(MPS3_TELEM_BASE, TELEM_CURR_UA, 345678u);
    /* GENCHK counters the `macgen` verb reads back (distinct values). */
    mock_regs_poke(MPS3_GENCHK_BASE, GENCHK_TX_CNT, 1234u);
    mock_regs_poke(MPS3_GENCHK_BASE, GENCHK_RX_CNT, 1230u);
    mock_regs_poke(MPS3_GENCHK_BASE, GENCHK_ERR_CNT, 4u);

    /* The diag mailbox: every counter zero EXCEPT the v9 power-on latch, which
     * the superloop would publish from the store (overlay_store_diag_word()) --
     * so `diag`.usd_boot and `usd`.boot tell the same story (decided: none). */
    {
        mps3_diag_t v;
        memset(&v, 0, sizeof v);
        v.usd_boot = overlay_store_diag_word();
        mps3_diag_publish(&v);
    }

    /* Line buffer sized well past any legitimate request (the longest
     * contract verb line is < 64 bytes); an over-long line simply arrives
     * split and each piece fails decode -> {"ok":false,...}, which is the
     * right fail-closed answer for garbage input anyway. */
    char line[512];
    char out[MPS3_CTRL_RESP_MAX];
    while (fgets(line, sizeof(line), stdin) != NULL) {
        int ready = coordinator_dispatch_line(line, (int)strlen(line),
                                              out, (int)sizeof(out));
        if (ready == 0) {
            /* A held response (an accepted swap; nothing else can defer in
             * this scenario): finish it as the main loop would, then build it. */
            pump_swap_to_completion();
            mps3_ctrl_response_t resp;
            if (!coordinator_held_poll(&resp)) {
                coordinator_swap_final_response(&resp);
            }
            if (mps3_ctrl_encode_response(&resp, out, (int)sizeof(out)) < 0) {
                (void)snprintf(out, sizeof(out),
                               "{\"ok\":false,\"err\":\"encode overflow\"}\n");
            }
        }
        fputs(out, stdout);
        fflush(stdout);
    }
    return 0;
}
