/*
 * test_coordinator_dispatch.c — host-gcc end-to-end test of the control
 * channel's line-in -> response-out path for EVERY net-protocol.md verb:
 * real net_proto.c codec, real coordinator.c dispatch + handlers, real
 * clkrst.c, real swap_fsm.c(+transitions) -- against mock_regs.c's
 * register file and the module fakes (fake_config_agent /
 * fake_overlay_store / fake_services). Only the network transport itself
 * is absent (W-NET-SEAM); everything from the received line to the
 * encoded response bytes is the shipping code.
 *
 * Links (see Makefile): coordinator.c, swap_fsm.c, swap_fsm_transitions.c,
 * clkrst.c, net_proto.c, mock_regs.c, fake_config_agent.c,
 * fake_overlay_store.c, fake_services.c. -DMPS3_HAL_MOCK.
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../coordinator/coordinator.h"
#include "../coordinator/swap_fsm.h"
#include "../common/platform_regs.h"
#include "../common/diag.h"
#include "mock_regs.h"
#include "fake_config_agent.h"
#include "fake_overlay_store.h"

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

#define TEST_STATIC_ID 0xA1B2C3D4u

/* Strong override of coordinator.c's WEAK fallback -- exactly how the real
 * shell build provisions its static_id (see coordinator.h's seam note). */
uint32_t mps3_shell_static_id(void)
{
    return TEST_STATIC_ID;
}

/* Fresh world per test case: mocks cleared, fakes seeded with a valid
 * greybox, then the REAL coordinator_init() (whose module _init() calls
 * land in the fakes/mocks). */
static void boot(void)
{
    mock_regs_reset();
    fake_config_agent_reset();
    overlay_manifest_info_t greybox = {
        .static_id = TEST_STATIC_ID, .rm_id = 0,
        .clear_len_words = 4, .clear_crc32 = 0x1111,
    };
    fake_overlay_store_reset();
    fake_overlay_store_set_greybox(&greybox, 0);
    coordinator_init();
}

/* Dispatch one C-literal line; returns coordinator_dispatch_line()'s
 * ready flag with the response (if any) in `out`. */
static int dispatch(const char *line, char *out, int out_len)
{
    return coordinator_dispatch_line(line, (int)strlen(line), out, out_len);
}

static void test_ping_reports_shell_and_rm_id(void)
{
    boot();
    char out[MPS3_CTRL_RESP_MAX];
    CHECK(dispatch("{\"op\":\"ping\"}", out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":true,\"shell_id\":\"0xa1b2c3d4\",\"rm_id\":\"0x00000000\"}\n") == 0);
}

static void test_reset_pulses_dut_resetn(void)
{
    boot();
    char out[MPS3_CTRL_RESP_MAX];
    CHECK(dispatch("{\"op\":\"reset\",\"target\":\"dut\"}", out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":true}\n") == 0);
    /* clkrst_pulse_reset() ends released: RESET_CTRL.dut_resetn == 1. */
    CHECK((mock_regs_peek(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL)
           & CLKRST_RESET_CTRL_DUT_RESETN) != 0);

    /* Only "dut" is contract-legal (see coordinator_handle_reset). */
    CHECK(dispatch("{\"op\":\"reset\",\"target\":\"rp\"}", out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"bad target\"}\n") == 0);
}

static void test_set_clk_selects_preset_and_reports_lock(void)
{
    boot();
    char out[MPS3_CTRL_RESP_MAX];

    /* MMCM already showing locked -> locked:true. */
    mock_regs_poke(MPS3_CLKRST_BASE, CLKRST_STATUS, CLKRST_STATUS_MMCM_LOCKED);
    CHECK(dispatch("{\"op\":\"set_clk\",\"preset\":\"50mhz\"}", out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":true,\"locked\":true}\n") == 0);
    /* The JSON preset arg reached the register: "50mhz" is table id 1. */
    CHECK(mock_regs_peek(MPS3_CLKRST_BASE, CLKRST_DUT_CLK_SEL) == 1u);

    /* Not (yet) locked is ok=true, locked=false -- distinct outcomes. */
    mock_regs_poke(MPS3_CLKRST_BASE, CLKRST_STATUS, 0);
    CHECK(dispatch("{\"op\":\"set_clk\",\"preset\":\"25mhz\"}", out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":true,\"locked\":false}\n") == 0);
    CHECK(mock_regs_peek(MPS3_CLKRST_BASE, CLKRST_DUT_CLK_SEL) == 0u);

    /* Unknown preset must be rejected (clkrst.c's real lookup, no longer
     * the match-anything placeholder). */
    CHECK(dispatch("{\"op\":\"set_clk\",\"preset\":\"13mhz\"}", out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"unknown preset\"}\n") == 0);
}

static void test_link_injects_vphy_events(void)
{
    boot();
    char out[MPS3_CTRL_RESP_MAX];

    CHECK(dispatch("{\"op\":\"link\",\"event\":\"down\"}", out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":true}\n") == 0);
    CHECK(mock_regs_peek(MPS3_VPHY_BASE, VPHY_LINK_EVENT) == VPHY_LINK_EVENT_FORCE_DOWN);

    CHECK(dispatch("{\"op\":\"link\",\"event\":\"up\"}", out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":true}\n") == 0);
    CHECK(mock_regs_peek(MPS3_VPHY_BASE, VPHY_LINK_EVENT) == 0u);

    CHECK(dispatch("{\"op\":\"link\",\"event\":\"pulse\"}", out, sizeof(out)) == 1);
    CHECK(mock_regs_peek(MPS3_VPHY_BASE, VPHY_LINK_EVENT) == VPHY_LINK_EVENT_PULSE);

    CHECK(dispatch("{\"op\":\"link\",\"event\":\"sideways\"}", out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"bad event\"}\n") == 0);
}

static void test_commit_v013(void)
{
    /* v0.13 (D13): commit is the RE-PUSH. The v0.11 form -- `rm` alone --
     * decodes as bad args (the store needs both lengths and both CRCs up front).
     * The full form is HELD like swap; its answer comes from
     * coordinator_held_poll(). Every store refusal reaches the wire by NAME.
     * (test_usd_dispatch.c has the full refusal table.) */
    boot();
    char out[MPS3_CTRL_RESP_MAX];
    static const char k_commit[] =
        "{\"op\":\"commit\",\"rm\":\"nanosoc\",\"src\":\"tcp\",\"rm_id\":\"0x00000001\","
        "\"static_id\":\"0xa1b2c3d4\",\"clear_len\":16,\"clear_crc\":\"0x1111\","
        "\"part_len\":32,\"part_crc\":\"0x2222\"}";

    CHECK(dispatch("{\"op\":\"commit\",\"rm\":\"nanosoc\"}", out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"bad args\"}\n") == 0);

    /* No card (the fake's default): refused at once, by name. */
    CHECK(dispatch(k_commit, out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"no card\"}\n") == 0);

    /* Accepted: HELD, then the answer. */
    fake_overlay_store_set_commit(OVLSD_OK, OVLSD_OK, 'A');
    out[0] = '\0';
    CHECK(dispatch(k_commit, out, sizeof(out)) == 0);
    CHECK(out[0] == '\0');
    CHECK(coordinator_held_pending());
    mps3_ctrl_response_t resp;
    CHECK(coordinator_held_poll(&resp) == 1);
    CHECK(mps3_ctrl_encode_response(&resp, out, sizeof(out)) > 0);
    CHECK(strcmp(out, "{\"ok\":true,\"slot\":\"A\"}\n") == 0);
    CHECK(!coordinator_held_pending());
    /* The request's fields reached the store intact. */
    CHECK(fake_overlay_store_last_commit_desc()->rm_id == 1u);
    CHECK(fake_overlay_store_last_commit_desc()->part_len == 32u);
    CHECK(fake_overlay_store_last_commit_live_static() == TEST_STATIC_ID);
}

/* telemetry FAILS LOUDLY: there is no power sensor reachable from this design
 * (TELEM's sample inputs are tied to ground in shell_bd.tcl, its INA228 I2C
 * engine was never written, the pads are not on the top level, and the MPS3 MCC
 * refuses voltage reads). The verb used to ship TELEM's permanent zeroes as
 * {"mv":0,"ma":0} -- a reading-shaped lie. It now returns the protocol's uniform
 * error shape, and the mv/ma keys are ABSENT rather than zeroed.
 *
 * `lockup` is NOT suppressed: it is a real pin, so it still rides the wire, raw,
 * inside the failure line. The shell reports the pin; the host's per-RM
 * catalogue decides whether the pin means anything for the loaded RM. */
static void test_telemetry_fails_loudly_but_still_reports_lockup(void)
{
    boot();
    char out[MPS3_CTRL_RESP_MAX];

    /* Poke NON-ZERO power registers. On real silicon these are hard zeroes, but
     * seeding them proves the handler does not read them AT ALL -- if it ever
     * starts reading TELEM again, these values would surface and fail the test. */
    mock_regs_poke(MPS3_TELEM_BASE, TELEM_BUS_MV, 1200u);
    mock_regs_poke(MPS3_TELEM_BASE, TELEM_CURR_UA, 345678u);

    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS, DFXCTL_RM_STATUS_DUT_LOCKUP);
    CHECK(dispatch("{\"op\":\"telemetry\"}", out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"no power sensor\",\"lockup\":true}\n") == 0);

    /* lockup tracks the pin, both ways -- raw, un-editorialised. */
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS, 0);
    CHECK(dispatch("{\"op\":\"telemetry\"}", out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"no power sensor\",\"lockup\":false}\n") == 0);

    /* No power reading escapes, in any form: not the seeded values, not zeroes,
     * not the keys themselves. This is the assertion that would have caught the
     * original bug, and it is the one that stops it coming back. */
    CHECK(strstr(out, "\"mv\"") == NULL);
    CHECK(strstr(out, "\"ma\"") == NULL);
    CHECK(strstr(out, "1200") == NULL);
    CHECK(strstr(out, "345") == NULL);
    CHECK(strstr(out, "\"ok\":true") == NULL);
}

static void test_diag_reports_counter_mailbox(void)
{
    boot();
    char out[MPS3_CTRL_RESP_MAX];

    /* The diag verb reads back the mailbox the superloop publishes (diag.h). Seed
     * it with representative values (one window paced -> win_windows_drained=1,
     * surfaced on the wire as the compatibility-kept "grants_sent" key) plus the
     * lwIP-window + TX-visibility fields, and assert the JSON. */
    mps3_diag_init();
    mps3_diag_t v = {
        .rx_recover_events = 0,     .rx_recover_dumps = 0,   .rx_drop_frames = 0,
        .icap_bytes = 256u,         .rx_payload_got = 4096u, .rx_payload_expect = 68288u,
        .tcp_rcv_wnd = 2048u,       .tcp_rcv_ann_wnd = 2048u,
        .rx_queued = 0u,            .pbuf_free = 8u,
        .win_windows_drained = 1u,  .win_grant_send_fails = 0u,
        .tcp_sndbuf = 4096u,        .tcp_snd_wnd = 64240u,
        /* v8 superloop service telemetry. Seeded NON-ZERO on purpose: a golden
         * line of thirteen zeroes would pass just as well against an encoder
         * that emitted the keys and dropped the values. svc_skipped = 0x200 is
         * service 9 (clcd) sick; svc_us_3 packs services 6 and 7 (jtag = 0x22
         * us, xvc = 0x11 us) two-per-word. */
        .svc_count = 12u,           .svc_pass_max_us = 1234u,
        .svc_skipped_mask = 0x200u, .svc_max_us_3 = 0x00110022u,
        /* v9 (D13): the usd row's word and the power-on latch. */
        .svc_max_us_6 = 0x00000033u, .usd_boot = 0xB0070002u,
    };
    mps3_diag_publish(&v);

    CHECK(dispatch("{\"op\":\"diag\"}", out, sizeof(out)) == 1);
    CHECK(strcmp(out,
        "{\"ok\":true,\"rx_recover\":0,\"rx_dumps\":0,\"rx_drops\":0,"
        "\"icap_bytes\":256,\"got\":4096,\"expect\":68288,"
        "\"rcv_wnd\":2048,\"rcv_ann_wnd\":2048,\"rx_queued\":0,"
        "\"pbuf_free\":8,\"grants_sent\":1,\"grant_fails\":0,"
        "\"sndbuf\":4096,\"snd_wnd\":64240,"
        "\"tx_frames_sent\":0,\"tx_status_drained\":0,"
        "\"tx_fifo_full_drops\":0,\"tx_errors\":0,"
        "\"tx_space_stalls\":0,\"tx_iface_errors\":0,"
        "\"tx_last_status\":0,\"icap_sr_last\":0,"
        "\"icap_eos_status\":0,\"ovlstore_phase\":0,"
        "\"ovlstore_detail\":0,"
        "\"touch_regs\":0,\"touch_adc_x\":0,"
        "\"touch_adc_y\":0,\"touch_verdict\":0,"
        "\"svc_count\":12,\"pass_max_us\":1234,\"svc_max_us\":0,"
        "\"svc_max_ix\":0,\"svc_overruns\":0,\"svc_skips\":0,"
        "\"svc_skipped\":512,"
        "\"svc_us_0\":0,\"svc_us_1\":0,\"svc_us_2\":0,"
        "\"svc_us_3\":1114146,\"svc_us_4\":0,\"svc_us_5\":0,"
        "\"svc_us_6\":51,\"usd_boot\":2953248770}\n") == 0);
}

/* Drives the armed FSM to completion with the same mock choreography as
 * test_swap_fsm_hw.c's happy path (the incoming pair "arrives" via the
 * fake config_agent; DFXCTL/HWICAP react via pokes). rm_id_readback is
 * what DFXCTL.RM_ID reports at SWAP_VERIFY -- pass the target id for a
 * verified swap, anything else for an I25 failure. */
static void pump_swap(uint32_t rm_id_readback)
{
    config_agent_bitstream_info_t incoming_clear = {
        .rm_id = 1, .static_id = TEST_STATIC_ID, .len_words = 8, .crc32 = 0x2222,
    };
    config_agent_bitstream_info_t incoming_partial = {
        .rm_id = 1, .static_id = TEST_STATIC_ID, .len_words = 4, .crc32 = 0x3333,
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
            mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_ID, rm_id_readback);
            mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS,
                           DFXCTL_RM_STATUS_RM_ID_VALID);
            break;
        case SWAP_RELEASE:
            mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS, 0u);
            break;
        case SWAP_REISOLATE:
            /* R1: a verify failure re-isolates the RP before reporting. Model
             * the vendor IP confirming decoupled + rp_in_reset again, else the
             * FSM correctly spins here until its bounded timeout. */
            mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,
                           DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET);
            break;
        default:
            break;
        }
        swap_fsm_poll();
    }
}

static void test_swap_defers_then_reports_verified_result(void)
{
    boot();
    char out[MPS3_CTRL_RESP_MAX];

    /* Accepted swap: dispatch returns 0 -- the response is HELD (see
     * coordinator.h). Nothing was written to out. */
    out[0] = '\0';
    CHECK(dispatch("{\"op\":\"swap\",\"rm\":\"nanosoc\",\"src\":\"tftp\"}",
                   out, sizeof(out)) == 0);
    CHECK(out[0] == '\0');
    CHECK(!swap_fsm_idle());

    /* A second swap while one is in flight is an IMMEDIATE error. */
    CHECK(dispatch("{\"op\":\"swap\",\"rm\":\"led\",\"src\":\"tftp\"}",
                   out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"swap already in progress\"}\n") == 0);

    /* Main loop finishes the swap; the held response is built + encoded. */
    pump_swap(1u);
    CHECK(swap_fsm_idle());
    mps3_ctrl_response_t resp;
    coordinator_swap_final_response(&resp);
    CHECK(mps3_ctrl_encode_response(&resp, out, sizeof(out)) > 0);
    CHECK(strcmp(out, "{\"ok\":true,\"rm_id\":\"0x00000001\",\"verified\":true}\n") == 0);

    /* The coordinator's world-view moved with it: ping now reports RM 1. */
    CHECK(dispatch("{\"op\":\"ping\"}", out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":true,\"shell_id\":\"0xa1b2c3d4\",\"rm_id\":\"0x00000001\"}\n") == 0);
}

static void test_swap_verify_failure_reports_failed(void)
{
    boot();
    char out[MPS3_CTRL_RESP_MAX];

    CHECK(dispatch("{\"op\":\"swap\",\"rm\":\"nanosoc\",\"src\":\"tftp\"}",
                   out, sizeof(out)) == 0);
    pump_swap(99u); /* wrong RM lands -> I25 verify fails -> SWAP_FAILED */
    CHECK(swap_fsm_idle());

    mps3_ctrl_response_t resp;
    coordinator_swap_final_response(&resp);
    CHECK(mps3_ctrl_encode_response(&resp, out, sizeof(out)) > 0);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"swap failed\"}\n") == 0);
}

static void test_swap_final_response_before_completion_fails_closed(void)
{
    boot();
    char out[MPS3_CTRL_RESP_MAX];
    CHECK(dispatch("{\"op\":\"swap\",\"rm\":\"nanosoc\",\"src\":\"tftp\"}",
                   out, sizeof(out)) == 0);
    /* FSM armed but not settled: no invented result. */
    mps3_ctrl_response_t resp;
    coordinator_swap_final_response(&resp);
    CHECK(!resp.ok);
    CHECK(strcmp(resp.err, "swap not complete") == 0);
    pump_swap(1u); /* leave the FSM idle for whoever tests next */
}

/* This binary is built WITHOUT -DMPS3_HAS_CLCD_KVM (there is no 0x44AD slave on
 * today's bitstream), so the display verb takes the OFF-build path: it DECODES
 * cleanly, then coordinator_handle_display() declines with the uniform failure
 * line rather than touching a DECERR'ing void. The ON-build path (real CSR
 * pokes + STATUS readback) is covered by test_display_dispatch.c. */
static void test_display_off_build_declines_cleanly(void)
{
    boot();
    char out[MPS3_CTRL_RESP_MAX];

    CHECK(dispatch("{\"op\":\"display\",\"owner\":\"dut\"}", out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"clcd_kvm not present\"}\n") == 0);
    CHECK(dispatch("{\"op\":\"display\",\"owner\":\"query\"}", out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"clcd_kvm not present\"}\n") == 0);

    /* Missing owner is still a decode-level bad-args rejection, ON or OFF. */
    CHECK(dispatch("{\"op\":\"display\"}", out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"bad args\"}\n") == 0);

    /* No CLCDKVM register was written on the OFF-build path. */
    CHECK(mock_regs_peek(MPS3_CLCDKVM_BASE, CLCDKVM_CTRL) == 0u);
}

/* `dutrx` on a bitstream with NO DUTEGR slave -- which is TODAY's fielded
 * bitstream: the capture block is real RTL and is in the BD, so it arrives at
 * the next mint, and a firmware built from this tree can be baked into the
 * CURRENT .bit by updatemem with no re-mint at all. This binary is built
 * without -DMPS3_HAS_DUT_EGRESS, so it is that board.
 *
 * The verb must DECODE and the handler must DECLINE. An ok:true line full of
 * zeroes would say "the DUT sent nothing" when the truth is "this fabric cannot
 * capture anything" -- the reading-shaped lie v0.6 took out of `telemetry`, and
 * the reason `display` answers the same way on a KVM-less shell. The ON-build
 * path (the real FIFO drain) is covered by test_dutrx_dispatch.c. */
static void test_dutrx_off_build_declines_cleanly(void)
{
    boot();
    char out[MPS3_CTRL_RESP_MAX];

    CHECK(dispatch("{\"op\":\"dutrx\"}", out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"dut_egress not present\"}\n") == 0);

    /* And it touched nothing at 0x44B2: no bus access into a DECERR'ing void.
     * (mock_regs_peek reads the slot array directly -- a read the handler had
     * made would have allocated the slot, but the value would still be 0, so
     * what this really pins is that CTRL was never WRITTEN.) */
    CHECK(mock_regs_peek(MPS3_DUTEGR_BASE, DUTEGR_CTRL) == 0u);
}

/* `version` (net-protocol.md v0.8) -- the verb that lets a RUNNING image say
 * what it is. Before it existed, `ping` reported static_id + rm_id and nothing
 * else, so a board could not be asked which firmware release it was carrying or
 * which compile-time features were in it: the fielded flag set lived only as
 * five literals in a mint script, and a mismatched image was indistinguishable
 * from a correct one over the wire.
 *
 * This binary links ONLY ../platform/mps3_version_weak.c (see the Makefile's
 * DISPATCH_SRCS note), so the seam answers the honest "not provisioned"
 * identity -- a FIXED expectation that does not move with the tree's commit.
 * It is also built with none of the feature -D flags, so "features" must be the
 * EMPTY array: the list is derived from the flags, never from a wish. */
static void test_version_reports_build_identity(void)
{
    boot();
    char out[MPS3_CTRL_RESP_MAX];
    CHECK(dispatch("{\"op\":\"version\"}", out, sizeof(out)) == 1);
    /* usr_access/skew are NULL here because boot() leaves the mock register
     * file empty, so USRACC_MAGIC reads 0 and mps3_fabric_usr_access_str()
     * answers "no sensor". That is the behaviour of EVERY shell fielded before
     * 2026-09-14 -- the page is unmapped and reads 0 -- and it is why the block
     * has a MAGIC register at all: without it, "no sensor" and "the fabric was
     * stamped 0x00000000" are the same read, and firmware would report a
     * comparison it never made.
     *
     * The two cases where the check DOES run are the next test. This one is the
     * honest negative, and it is also what keeps the golden/conformance suites
     * (tests/firmware_logic/test_json_golden.py, the FakeShell conformance)
     * byte-stable: nothing else pokes USRACC. */
    CHECK(strcmp(out,
                 "{\"ok\":true,\"harness\":\"0.0.0\",\"ver32\":\"0x00000000\","
                 "\"sha\":\"unknown\",\"dirty\":0,\"lmb_kb\":1024,"
                 "\"features\":[\"jtag_server\",\"xvc_dbgbr\",\"stats\",\"log\",\"reboot\",\"usd\"],\"usr_access\":null,\"skew\":null}\n") == 0);

    /* No arguments: extra keys are ignored like every other verb (tolerant
     * JSON), and the answer is unchanged. */
    char out2[MPS3_CTRL_RESP_MAX];
    CHECK(dispatch("{\"op\":\"version\",\"unexpected\":1}", out2, sizeof(out2)) == 1);
    CHECK(strcmp(out, out2) == 0);
}

/* THE CROSS-CHECK, now that there is something to check against.
 *
 * `ba2f4be` built every part of this except the sensor and said so: "it reads
 * null / null on every real board today, and will keep reading null until
 * somebody builds the fabric readback register that VERSIONING_PLAN.md section
 * 3.4 specifies ... When that register lands, THIS assertion is the one that
 * must change." It landed (fpga/shell/ip/usr_access_rd/, USRACC @0x44B3_0000),
 * so here is the assertion it asked for.
 *
 * Both outcomes are asserted, because only the pair is worth anything:
 *   - fabric == image  ->  "skew":false   the check ran and passed
 *   - fabric != image  ->  "skew":true    the check ran and FAILED
 * A test that only pinned the agreeing case would pass against a codec that
 * hard-coded `false`, which is exactly the failure mode the three-state design
 * exists to prevent.
 *
 * It also silently pins something else: `usr_access` and `ver32` must be
 * rendered by the SAME formatter, because net_proto.c compares the two STRINGS.
 * If mps3_usr_access.c's snprintf and coordinator.c's format_id_hex ever drift
 * -- different case, different width, a missing "0x" -- the AGREE case below
 * reports skew and goes red. That is the drift guard; there is no separate
 * test for it because a separate test could itself drift. */
static void test_version_cross_checks_the_fabric_identity(void)
{
    char out[MPS3_CTRL_RESP_MAX];

    /* --- AGREE. This image is the "not provisioned" weak seam, so its ver32
     * is 0x00000000; a fabric stamped the same is a matching pair. Note that
     * this is ALSO the case that would be indistinguishable from "no sensor"
     * without MAGIC -- it is stamped zero, and it must still read as a check
     * that RAN. */
    boot();
    mock_regs_poke(MPS3_USRACC_BASE, USRACC_MAGIC, USRACC_MAGIC_VALUE);
    mock_regs_poke(MPS3_USRACC_BASE, USRACC_STATUS, USRACC_STATUS_VALID);
    mock_regs_poke(MPS3_USRACC_BASE, USRACC_VALUE, 0x00000000u);
    CHECK(dispatch("{\"op\":\"version\"}", out, sizeof(out)) == 1);
    CHECK(strcmp(out,
                 "{\"ok\":true,\"harness\":\"0.0.0\",\"ver32\":\"0x00000000\","
                 "\"sha\":\"unknown\",\"dirty\":0,\"lmb_kb\":1024,"
                 "\"features\":[\"jtag_server\",\"xvc_dbgbr\",\"stats\",\"log\",\"reboot\",\"usd\"],\"usr_access\":\"0x00000000\","
                 "\"skew\":false}\n") == 0);

    /* --- SKEW. The .bit says it is harness v1.0.0 and the image inside it was
     * built as 0.0.0: a flashable base whose `updatemem` was never re-run. This
     * is the fault the whole mechanism exists to make visible, and before the
     * sensor existed it was invisible on hardware. */
    boot();
    mock_regs_poke(MPS3_USRACC_BASE, USRACC_MAGIC, USRACC_MAGIC_VALUE);
    mock_regs_poke(MPS3_USRACC_BASE, USRACC_STATUS, USRACC_STATUS_VALID);
    mock_regs_poke(MPS3_USRACC_BASE, USRACC_VALUE, 0x01000000u);
    CHECK(dispatch("{\"op\":\"version\"}", out, sizeof(out)) == 1);
    CHECK(strcmp(out,
                 "{\"ok\":true,\"harness\":\"0.0.0\",\"ver32\":\"0x00000000\","
                 "\"sha\":\"unknown\",\"dirty\":0,\"lmb_kb\":1024,"
                 "\"features\":[\"jtag_server\",\"xvc_dbgbr\",\"stats\",\"log\",\"reboot\",\"usd\"],\"usr_access\":\"0x01000000\","
                 "\"skew\":true}\n") == 0);

    /* --- THE TWO WAYS TO HAVE NO ANSWER, and neither may render as a pass.
     *
     * (a) wrong MAGIC: some other block, or nothing, behind that page. Note the
     *     VALUE register is seeded with a plausible identity here -- if firmware
     *     read it without checking MAGIC first it would report a comparison,
     *     and this assertion is what stops that. */
    boot();
    mock_regs_poke(MPS3_USRACC_BASE, USRACC_MAGIC, 0xDEADBEEFu);
    mock_regs_poke(MPS3_USRACC_BASE, USRACC_STATUS, USRACC_STATUS_VALID);
    mock_regs_poke(MPS3_USRACC_BASE, USRACC_VALUE, 0x01000000u);
    CHECK(dispatch("{\"op\":\"version\"}", out, sizeof(out)) == 1);
    CHECK(strstr(out, "\"usr_access\":null,\"skew\":null}") != NULL);

    /* (b) the block is there but the USR_ACCESSE2 primitive has not presented
     *     DATAVALID. An un-captured VALUE reads 0, which is a legal stamp --
     *     so "not valid yet" must also be no answer, not a zero. */
    boot();
    mock_regs_poke(MPS3_USRACC_BASE, USRACC_MAGIC, USRACC_MAGIC_VALUE);
    mock_regs_poke(MPS3_USRACC_BASE, USRACC_STATUS, 0x00000000u);
    mock_regs_poke(MPS3_USRACC_BASE, USRACC_VALUE, 0x01000000u);
    CHECK(dispatch("{\"op\":\"version\"}", out, sizeof(out)) == 1);
    CHECK(strstr(out, "\"usr_access\":null,\"skew\":null}") != NULL);
}

static void test_rejection_paths_produce_error_lines(void)
{
    boot();
    char out[MPS3_CTRL_RESP_MAX];

    CHECK(dispatch("{\"op\":\"ping\"", out, sizeof(out)) == 1); /* truncated */
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"bad json\"}\n") == 0);

    CHECK(dispatch("{\"op\":\"selfdestruct\"}", out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"unknown op\"}\n") == 0);

    CHECK(dispatch("{\"op\":\"reset\"}", out, sizeof(out)) == 1); /* missing target */
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"bad args\"}\n") == 0);

    CHECK(dispatch("{\"op\":\"swap\",\"rm\":\"nanosoc\"}", out, sizeof(out)) == 1); /* missing src */
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"bad args\"}\n") == 0);
    CHECK(swap_fsm_idle()); /* a rejected swap must not have armed the FSM */
}

int main(void)
{
    test_ping_reports_shell_and_rm_id();
    test_reset_pulses_dut_resetn();
    test_set_clk_selects_preset_and_reports_lock();
    test_link_injects_vphy_events();
    test_commit_v013();
    test_telemetry_fails_loudly_but_still_reports_lockup();
    test_diag_reports_counter_mailbox();
    test_swap_defers_then_reports_verified_result();
    test_swap_verify_failure_reports_failed();
    test_swap_final_response_before_completion_fails_closed();
    test_display_off_build_declines_cleanly();
    test_dutrx_off_build_declines_cleanly();
    test_version_reports_build_identity();
    test_version_cross_checks_the_fabric_identity();
    test_rejection_paths_produce_error_lines();

    printf("test_coordinator_dispatch: %d checks passed\n", s_checks);
    return 0;
}
