/*
 * test_macgen.c — host-gcc test of the `macgen` control verb's line-in ->
 * response-out path (I10 tail): real net_proto.c decode/encode + real
 * coordinator.c dispatch/coordinator_handle_macgen against mock_regs.c's
 * in-memory GENCHK register file. Proves the verb drives GENCHK.CTRL +
 * INJECT correctly and reports TX/RX/ERR_CNT back, per net-protocol.md
 * "MAC gen/checker control" + shell-regmap.md GENCHK.
 *
 * Links (see Makefile): the DISPATCH_SRCS set (real coordinator.c + codec +
 * clkrst/swap_fsm, mock/fake backends). -DMPS3_HAL_MOCK. macgen touches only
 * the GENCHK block, so — unlike test_coordinator_dispatch.c — this test does
 * NOT call coordinator_init(): a bare mock_regs_reset() per case is enough
 * (the handler reads no g_shell_state).
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../coordinator/coordinator.h"
#include "../common/platform_regs.h"
#include "mock_regs.h"

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

static int dispatch(const char *line, char *out, int out_len)
{
    return coordinator_dispatch_line(line, (int)strlen(line), out, out_len);
}

/* Fresh, empty GENCHK register file per case. */
static void boot(void)
{
    mock_regs_reset();
}

static uint32_t genchk(uint32_t off)
{
    return mock_regs_peek(MPS3_GENCHK_BASE, off);
}

static void test_gen_chk_enable_and_counter_readback(void)
{
    boot();
    char out[MPS3_CTRL_RESP_MAX];

    /* Seed the three RO counters with DISTINCT values so a tx/rx/err mixup
     * in the encoder/handler cannot pass. */
    mock_regs_poke(MPS3_GENCHK_BASE, GENCHK_TX_CNT, 100u);
    mock_regs_poke(MPS3_GENCHK_BASE, GENCHK_RX_CNT, 97u);
    mock_regs_poke(MPS3_GENCHK_BASE, GENCHK_ERR_CNT, 0u);

    CHECK(dispatch("{\"op\":\"macgen\",\"gen\":true,\"chk\":true,\"inject\":\"none\"}",
                   out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":true,\"tx\":100,\"rx\":97,\"err\":0}\n") == 0);

    /* gen+chk both enabled; inject "none" armed nothing. */
    CHECK(genchk(GENCHK_CTRL) == (GENCHK_CTRL_GEN_EN | GENCHK_CTRL_CHK_EN));
    CHECK(genchk(GENCHK_INJECT) == 0u);
}

static void test_gen_chk_flags_map_independently(void)
{
    char out[MPS3_CTRL_RESP_MAX];

    boot();
    CHECK(dispatch("{\"op\":\"macgen\",\"gen\":true,\"chk\":false,\"inject\":\"none\"}",
                   out, sizeof(out)) == 1);
    CHECK(genchk(GENCHK_CTRL) == GENCHK_CTRL_GEN_EN);

    boot();
    CHECK(dispatch("{\"op\":\"macgen\",\"gen\":false,\"chk\":true,\"inject\":\"none\"}",
                   out, sizeof(out)) == 1);
    CHECK(genchk(GENCHK_CTRL) == GENCHK_CTRL_CHK_EN);

    boot();
    CHECK(dispatch("{\"op\":\"macgen\",\"gen\":false,\"chk\":false,\"inject\":\"none\"}",
                   out, sizeof(out)) == 1);
    CHECK(genchk(GENCHK_CTRL) == 0u);
}

static void test_every_inject_fault_maps_to_its_onehot_bit(void)
{
    char out[MPS3_CTRL_RESP_MAX];
    struct { const char *name; uint32_t bit; } cases[] = {
        { "bad_fcs", GENCHK_INJECT_BAD_FCS },
        { "runt",    GENCHK_INJECT_RUNT    },
        { "giant",   GENCHK_INJECT_GIANT   },
        { "ifg",     GENCHK_INJECT_IFG     },
        { "dribble", GENCHK_INJECT_DRIBBLE },
        { "none",    0u                    },
    };
    for (size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
        char line[96];
        boot();
        (void)snprintf(line, sizeof(line),
                       "{\"op\":\"macgen\",\"gen\":true,\"chk\":true,\"inject\":\"%s\"}",
                       cases[i].name);
        CHECK(dispatch(line, out, sizeof(out)) == 1);
        CHECK(genchk(GENCHK_INJECT) == cases[i].bit);
        /* every accepted inject is a success line */
        CHECK(strncmp(out, "{\"ok\":true,", 11) == 0);
    }
}

static void test_bad_inject_rejected_without_touching_registers(void)
{
    boot();
    char out[MPS3_CTRL_RESP_MAX];

    /* Sentinels: a rejected macgen must not have written CTRL/INJECT (the
     * handler validates inject BEFORE any register write). */
    mock_regs_poke(MPS3_GENCHK_BASE, GENCHK_CTRL, 0xDEADu);
    mock_regs_poke(MPS3_GENCHK_BASE, GENCHK_INJECT, 0xBEEFu);

    CHECK(dispatch("{\"op\":\"macgen\",\"gen\":true,\"chk\":true,\"inject\":\"smash\"}",
                   out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"bad inject\"}\n") == 0);
    CHECK(genchk(GENCHK_CTRL) == 0xDEADu);   /* untouched */
    CHECK(genchk(GENCHK_INJECT) == 0xBEEFu); /* untouched */
}

static void test_missing_or_mistyped_args_are_bad_args(void)
{
    boot();
    char out[MPS3_CTRL_RESP_MAX];

    /* missing gen */
    CHECK(dispatch("{\"op\":\"macgen\",\"chk\":true,\"inject\":\"none\"}",
                   out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"bad args\"}\n") == 0);

    /* missing chk */
    CHECK(dispatch("{\"op\":\"macgen\",\"gen\":true,\"inject\":\"none\"}",
                   out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"bad args\"}\n") == 0);

    /* missing inject */
    CHECK(dispatch("{\"op\":\"macgen\",\"gen\":true,\"chk\":true}",
                   out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"bad args\"}\n") == 0);

    /* gen is an int, not a bool -> wrong type -> bad args (fail closed) */
    CHECK(dispatch("{\"op\":\"macgen\",\"gen\":1,\"chk\":true,\"inject\":\"none\"}",
                   out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"bad args\"}\n") == 0);
}

static void test_inject_rewritten_every_call(void)
{
    /* Arm a fault, then a follow-up "none" must clear INJECT back to 0 — the
     * coordinator rewrites INJECT unconditionally each call (it does not rely
     * on any RTL self-clear, per net-protocol.md). */
    boot();
    char out[MPS3_CTRL_RESP_MAX];

    CHECK(dispatch("{\"op\":\"macgen\",\"gen\":true,\"chk\":true,\"inject\":\"giant\"}",
                   out, sizeof(out)) == 1);
    CHECK(genchk(GENCHK_INJECT) == GENCHK_INJECT_GIANT);

    CHECK(dispatch("{\"op\":\"macgen\",\"gen\":true,\"chk\":true,\"inject\":\"none\"}",
                   out, sizeof(out)) == 1);
    CHECK(genchk(GENCHK_INJECT) == 0u);
}

int main(void)
{
    test_gen_chk_enable_and_counter_readback();
    test_gen_chk_flags_map_independently();
    test_every_inject_fault_maps_to_its_onehot_bit();
    test_bad_inject_rejected_without_touching_registers();
    test_missing_or_mistyped_args_are_bad_args();
    test_inject_rewritten_every_call();

    printf("test_macgen: %d checks passed\n", s_checks);
    return 0;
}
