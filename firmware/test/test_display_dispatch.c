/*
 * test_display_dispatch.c — host-gcc test of the `display` control verb on a
 * build that HAS the CLCD KVM (-DMPS3_HAS_CLCD_KVM), i.e. the Wave-4 bitstream.
 *
 * This is the ON-build counterpart of test_coordinator_dispatch.c's
 * test_display_off_build_declines_cleanly(): here the real coordinator.c
 * dispatch drives the REAL firmware/clcd_kvm/clcd_kvm.c helpers
 * (clcd_kvm_request_owner / clcd_kvm_status) against the mock CLCDKVM CSR page,
 * proving the network verb:
 *   - moves ownership through the frozen CTRL.src_sel (+ src_sel_we) — the
 *     button-safe path, NEVER an open-coded RMW;
 *   - reports the COMMITTED owner read back from STATUS.owner;
 *   - implements "toggle" against tgt_owner and "query" as a read-only report;
 *   - rejects an unknown owner value (fail closed).
 *
 * Links (see Makefile): the same DISPATCH_SRCS set as test_coordinator_dispatch
 * PLUS clcd_kvm.c, built with -DMPS3_HAL_MOCK -DMPS3_HAS_CLCD_KVM.
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

/* A fresh, empty CLCDKVM page for each case (no coordinator_init needed: the
 * display handler touches only the KVM CSRs, not g_shell_state). */
static void reset_regs(void)
{
    mock_regs_reset();
}

/* A display request drives CTRL through clcd_kvm_request_owner(): src_sel_we is
 * ALWAYS set (the write-enable), and src_sel carries the requested owner. */
static void test_dut_and_harness_drive_src_sel(void)
{
    char out[MPS3_CTRL_RESP_MAX];

    reset_regs();
    CHECK(dispatch("{\"op\":\"display\",\"owner\":\"dut\"}", out, sizeof(out)) == 1);
    /* src_sel = DUT, and the write-enable is set — nothing else. */
    CHECK(mock_regs_peek(MPS3_CLCDKVM_BASE, CLCDKVM_CTRL)
          == (CLCDKVM_CTRL_SRC_SEL_WE | CLCDKVM_CTRL_SRC_SEL));

    reset_regs();
    CHECK(dispatch("{\"op\":\"display\",\"owner\":\"harness\"}", out, sizeof(out)) == 1);
    /* src_sel = HARNESS (bit clear), write-enable still set. */
    CHECK(mock_regs_peek(MPS3_CLCDKVM_BASE, CLCDKVM_CTRL) == CLCDKVM_CTRL_SRC_SEL_WE);
}

/* The reply carries the COMMITTED owner (STATUS.owner), which can LAG the
 * request until the hardware handover completes. Seed STATUS.owner = DUT, ask
 * for HARNESS, and the reply still reports "dut" — the honest committed state. */
static void test_reply_reports_committed_owner_from_status(void)
{
    char out[MPS3_CTRL_RESP_MAX];

    reset_regs();
    mock_regs_poke(MPS3_CLCDKVM_BASE, CLCDKVM_STATUS, CLCDKVM_STATUS_OWNER); /* owner=DUT */
    CHECK(dispatch("{\"op\":\"display\",\"owner\":\"harness\"}", out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":true,\"owner\":\"dut\"}\n") == 0);
    /* ...but the request DID drive CTRL toward HARNESS (src_sel clear + we). */
    CHECK(mock_regs_peek(MPS3_CLCDKVM_BASE, CLCDKVM_CTRL) == CLCDKVM_CTRL_SRC_SEL_WE);

    reset_regs();
    mock_regs_poke(MPS3_CLCDKVM_BASE, CLCDKVM_STATUS, 0u); /* owner=HARNESS */
    CHECK(dispatch("{\"op\":\"display\",\"owner\":\"dut\"}", out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":true,\"owner\":\"harness\"}\n") == 0);
}

/* "toggle" flips the REQUESTED target (STATUS.tgt_owner), mirroring the button. */
static void test_toggle_flips_target(void)
{
    char out[MPS3_CTRL_RESP_MAX];

    reset_regs();
    mock_regs_poke(MPS3_CLCDKVM_BASE, CLCDKVM_STATUS, 0u); /* tgt_owner = HARNESS */
    CHECK(dispatch("{\"op\":\"display\",\"owner\":\"toggle\"}", out, sizeof(out)) == 1);
    /* HARNESS -> DUT: src_sel set. */
    CHECK((mock_regs_peek(MPS3_CLCDKVM_BASE, CLCDKVM_CTRL) & CLCDKVM_CTRL_SRC_SEL) != 0);
    CHECK((mock_regs_peek(MPS3_CLCDKVM_BASE, CLCDKVM_CTRL) & CLCDKVM_CTRL_SRC_SEL_WE) != 0);

    reset_regs();
    mock_regs_poke(MPS3_CLCDKVM_BASE, CLCDKVM_STATUS, CLCDKVM_STATUS_TGT_OWNER); /* tgt = DUT */
    CHECK(dispatch("{\"op\":\"display\",\"owner\":\"toggle\"}", out, sizeof(out)) == 1);
    /* DUT -> HARNESS: src_sel clear, write-enable set. */
    CHECK((mock_regs_peek(MPS3_CLCDKVM_BASE, CLCDKVM_CTRL) & CLCDKVM_CTRL_SRC_SEL) == 0);
    CHECK((mock_regs_peek(MPS3_CLCDKVM_BASE, CLCDKVM_CTRL) & CLCDKVM_CTRL_SRC_SEL_WE) != 0);
}

/* "query" is READ-ONLY: it reports STATUS.owner and writes NOTHING to CTRL —
 * so a GET /display / display_owner() can never move ownership. */
static void test_query_is_read_only(void)
{
    char out[MPS3_CTRL_RESP_MAX];

    reset_regs();
    mock_regs_poke(MPS3_CLCDKVM_BASE, CLCDKVM_STATUS, CLCDKVM_STATUS_OWNER); /* owner=DUT */
    CHECK(dispatch("{\"op\":\"display\",\"owner\":\"query\"}", out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":true,\"owner\":\"dut\"}\n") == 0);
    /* Nothing was written to CTRL — no src_sel_we, no ownership move. */
    CHECK(mock_regs_peek(MPS3_CLCDKVM_BASE, CLCDKVM_CTRL) == 0u);
}

/* An unknown owner value is rejected, and drives no CSR. */
static void test_bad_owner_rejected(void)
{
    char out[MPS3_CTRL_RESP_MAX];

    reset_regs();
    CHECK(dispatch("{\"op\":\"display\",\"owner\":\"banana\"}", out, sizeof(out)) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"bad owner\"}\n") == 0);
    CHECK(mock_regs_peek(MPS3_CLCDKVM_BASE, CLCDKVM_CTRL) == 0u);
}

int main(void)
{
    test_dut_and_harness_drive_src_sel();
    test_reply_reports_committed_owner_from_status();
    test_toggle_flips_target();
    test_query_is_read_only();
    test_bad_owner_rejected();

    printf("test_display_dispatch: %d checks passed\n", s_checks);
    return 0;
}
