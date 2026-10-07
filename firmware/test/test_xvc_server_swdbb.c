/*
 * test_xvc_server_swdbb.c — host-gcc tests for xvc_server.c built for the
 * SWDBB bit-bang target (-DMPS3_XVC_TARGET_SWDBB), the Identify/IICE path of
 * docs/planning/IDENTIFY_IICE_DFX_PLAN.md §3b.
 *
 * The DBGBR (default) target keeps its own suite in test_xvc_server.c; that
 * binary is built WITHOUT the flag, so the two together also prove the
 * shipped Debug-Bridge behaviour is still reachable and unchanged.
 *
 * ===========================================================================
 * What is actually proven here
 * ===========================================================================
 * The headline is test_idcode_read_through_do_shift(): a complete IEEE-1149.1
 * IDCODE read is pushed through the REAL firmware shift loop into an
 * in-memory TAP, and the 32-bit value comes back out. That one result depends
 * on three conventions that are each invisible until board day, and each has
 * a negative control:
 *
 *   1. XVC vectors are LSB-first WITHIN each byte, both directions
 *      -> test_msb_first_packing_would_not_recover_the_idcode()
 *   2. TDO is sampled in the TCK-LOW phase, BEFORE the rising edge that
 *      consumes the matching TDI bit
 *      -> the whole `test_xvc_server_swdbb_latesample` BINARY: the identical
 *         source built with -DMPS3_XVC_SWDBB_SAMPLE_LATE=1, which asserts the
 *         damage (IDCODE >> 1) instead of the success. Unlike an in-test
 *         re-implementation, that control runs through the REAL production
 *         loop, so it fails if the shipped ordering is ever inverted.
 *   3. TMS/TDI must be settled with TCK LOW, and only a 0->1 transition of
 *      DRIVE[0] is an edge
 *      -> test_drive_word_sequence_per_bit() pins the exact ordered register
 *         trace, and test_back_to_back_shifts_make_no_spurious_edge() pins
 *         the park.
 *
 * Prior art for why this matters: OPEN_ISSUES.md §I22 — the SWD bit order was
 * closed by READING the host driver and then pinned by a test that
 * re-derives the mapping, so an inverted convention fails a test rather than
 * a bring-up. Same discipline here, and additionally
 * test_pin_reuse_matches_the_host_server() pins this firmware to the
 * host-side host/socket_harness/xvc_server.py convention, since a chain
 * proven with one and run with the other must agree bit-for-bit.
 *
 * ===========================================================================
 * Three binaries from this one source (see firmware/test/Makefile)
 * ===========================================================================
 *   test_xvc_server_swdbb             STRETCH=0 LATE=0  positive
 *   test_xvc_server_swdbb_latesample  STRETCH=0 LATE=1  NEGATIVE CONTROL
 *   test_xvc_server_swdbb_stretch     STRETCH=2 LATE=0  the TCK-stretch knob
 * Same dual/triple-binary pattern as test_swap_qspi_free /
 * test_swap_qspi_free_tinyarena: the differing behaviour is compile-time, so
 * it cannot be a runtime branch.
 *
 * SWDBB effects are observed through a behavioral mock_regs hook that
 * reproduces fpga/shell/ip/swd_bb/swd_bb.sv exactly — DRIVE @0x00 is a 3-bit
 * write-through register, SAMPLE @0x04 is read-only and returns the live
 * inbound pin, and the TAP's rising edge is derived from DRIVE[0] going
 * 0 -> 1, which is the only way the real RM can see a TCK edge either.
 *
 * That hook and the 1149.1 TAP behind it USED to live in this file; they now
 * live in fake_jtag_tap.c so that this suite, the real-loopback-socket suite
 * (test_xvc_posix_loopback.c) and the host XVC daemon (xvc_fw_daemon.c) all
 * drive ONE model. Nothing about the model is parameterised by this file's two
 * compile-time knobs — see that file's header for why — so all three binaries
 * below still differ ONLY in the expectations stated here.
 *
 * Links: xvc_server.c, common/net_if.c, mock_regs.c, fake_net_if.c,
 * fake_jtag_tap.c, and host/socket_harness/xvc_server.py (FakeJtagTap/
 * FakeSwdbbBackend, of which fake_jtag_tap.c is the C mirror).
 * g_shell_state defined here (xvc_server.c reads xvc_gated only).
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../xvc_server/xvc_server.h"
#include "../coordinator/coordinator.h"
#include "../common/platform_regs.h"
#include "../common/net_proto.h"
#include "mock_regs.h"
#include "fake_net_if.h"
#include "fake_jtag_tap.h"

/* Each binary announces its own name (Makefile-supplied), so a failing run in
 * the suite log is unambiguous about WHICH configuration broke. */
#ifndef XVC_SWDBB_TEST_NAME
#define XVC_SWDBB_TEST_NAME "test_xvc_server_swdbb"
#endif

mps3_shell_state_t g_shell_state;

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

/* The DUT's real JTAG TAP id (host/openocd/nanosoc_mps3_jtag.cfg), and the
 * same constant the host-side test uses — so the two suites are provably
 * talking about the same scan. */
#define IDCODE 0x6BA00477u

/* Extra DRIVE writes per TCK phase in THIS binary (0 unless the Makefile
 * overrode it). Kept local so the expectations below are derived from the
 * same number the production code compiled against. */
#define STRETCH ((int)MPS3_XVC_SWDBB_TCK_STRETCH)

/* ---- the shared TAP + SWDBB model ------------------------------------------
 * Both models now live in fake_jtag_tap.c, so this suite, the real-socket
 * loopback suite (test_xvc_posix_loopback.c) and the host XVC daemon
 * (xvc_fw_daemon.c, `make tools`) drive ONE model rather than divergent copies.
 * Read that file's header for why it is deliberately NOT parameterised by
 * MPS3_XVC_SWDBB_TCK_STRETCH / MPS3_XVC_SWDBB_SAMPLE_LATE. The per-binary
 * EXPECTATIONS that DO depend on those knobs stay in this file, below.
 * ------------------------------------------------------------------------- */
static fake_swdbb_t s_swdbb;

/* ---- helpers ----------------------------------------------------------------- */

static void fresh_with_idcode(uint32_t idcode)
{
    mock_regs_reset();
    fake_net_reset();
    memset(&g_shell_state, 0, sizeof(g_shell_state));
    memset(&s_swdbb, 0, sizeof(s_swdbb));
    fake_tap_reset(&s_swdbb.tap, idcode);
    mock_regs_set_hook(MPS3_SWDBB_BASE, fake_swdbb_hook, &s_swdbb);
    xvc_server_init();
    fake_swdbb_clear_counters(&s_swdbb);
}

static void fresh(void)
{
    fresh_with_idcode(IDCODE);
}

static void polls(int n)
{
    for (int i = 0; i < n; i++) {
        xvc_server_poll();
    }
}

static void put_le32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16);
    p[3] = (uint8_t)(v >> 24);
}

/* Pack per-bit values into an XVC vector: LSB-first within each byte. The
 * inverse of xvc_server.c's vector_bit(), stated independently here. */
static void bits_to_vector(const uint32_t *bits, uint32_t n, uint8_t *out)
{
    memset(out, 0, (n + 7u) / 8u);
    for (uint32_t i = 0; i < n; i++) {
        if (bits[i] & 1u) {
            out[i >> 3] |= (uint8_t)(1u << (i & 7u));
        }
    }
}

/* Reassemble 32 bits out of a TDO vector starting at bit `first`, LSB first. */
static uint32_t recover32(const uint8_t *tdo, uint32_t first)
{
    uint32_t v = 0;
    for (uint32_t k = 0; k < 32u; k++) {
        uint32_t i = first + k;
        v |= (uint32_t)((tdo[i >> 3] >> (i & 7u)) & 1u) << k;
    }
    return v;
}

/* An IDCODE read expressed as an XVC shift, DERIVED from the 1149.1
 * controller graph rather than copied from a capture:
 *
 *   rising edge   TMS   resulting state
 *   1..5            1   Test-Logic-Reset (5 ones from ANY state); TLR
 *                       selects the IDCODE instruction
 *   6               0   Run-Test/Idle
 *   7               1   Select-DR-Scan
 *   8               0   Capture-DR
 *   9               0   Shift-DR (the DR was loaded with IDCODE on this same
 *                       edge, in Capture-DR)
 *   10..41          0   shift, the last one with TMS=1 -> Exit1-DR
 *
 * TDO for shift bit i is read while TCK is low, i.e. BEFORE edge i+1. Bit 9
 * is therefore the first read taken in Shift-DR, and bits 9..40 carry IDCODE
 * bits 0..31.
 */
#define IDCODE_SEQ_BITS  41u
#define IDCODE_SEQ_BYTES ((IDCODE_SEQ_BITS + 7u) / 8u)
#define IDCODE_FIRST_BIT 9u

static void idcode_read_sequence(uint8_t *tms_vec, uint8_t *tdi_vec)
{
    uint32_t tms[IDCODE_SEQ_BITS];
    uint32_t tdi[IDCODE_SEQ_BITS];
    static const uint32_t nav[IDCODE_FIRST_BIT] = { 1, 1, 1, 1, 1, 0, 1, 0, 0 };

    for (uint32_t i = 0; i < IDCODE_FIRST_BIT; i++) {
        tms[i] = nav[i];
    }
    for (uint32_t i = IDCODE_FIRST_BIT; i < IDCODE_SEQ_BITS; i++) {
        tms[i] = 0u;
    }
    tms[IDCODE_SEQ_BITS - 1u] = 1u; /* exit on the last shift bit */
    for (uint32_t i = 0; i < IDCODE_SEQ_BITS; i++) {
        tdi[i] = 0u;                 /* an IDCODE read shifts in don't-cares */
    }

    bits_to_vector(tms, IDCODE_SEQ_BITS, tms_vec);
    bits_to_vector(tdi, IDCODE_SEQ_BITS, tdi_vec);
}

/* THE per-binary expectation. Every binary runs the identical shift; only the
 * expected outcome differs, which is what makes the late-sample build a real
 * negative control rather than a second copy of the same proof. */
static void check_recovered_idcode(uint32_t got, uint32_t want)
{
#if MPS3_XVC_SWDBB_SAMPLE_LATE
    /* NEGATIVE CONTROL: sampling AFTER the rising edge reads the bit the TAP
     * has ALREADY shifted, so every bit is one position late. Only the top
     * bit is lost outright (the TAP has left Shift-DR by then), so the low 31
     * bits are exactly IDCODE >> 1 — an off-by-one-bit trace with no error
     * reported anywhere. This is the silent failure the production ordering
     * exists to avoid; if the shipped ordering were ever inverted, THIS
     * binary would pass and the positive one would fail. */
    CHECK(got != want);
    CHECK((got & 0x7FFFFFFFu) == ((want >> 1) & 0x7FFFFFFFu));
#else
    CHECK(got == want);
#endif
}

/* ---- tests --------------------------------------------------------------------- */

/* This binary must actually be the SWDBB build; otherwise every proof below
 * would be silently exercising the Debug Bridge. */
static void test_this_binary_is_the_swdbb_target(void)
{
    CHECK(MPS3_XVC_TARGET_IS_SWDBB == 1);
}

/* ---------------------------------------------------------------------------
 * The pin reuse, re-derived (I22 discipline). These four masks must equal BOTH
 * the SWD names in platform_regs.h (they are literally the same register bits,
 * reinterpreted RM-side) AND the host-side server's DRIVE_TCK/DRIVE_TMS/
 * DRIVE_TDI/SAMPLE_TDO. Restating the numeric values here is deliberate: it is
 * the one place the two independent implementations are cross-checked, so a
 * unilateral change to either side fails a test instead of a bring-up.
 * ------------------------------------------------------------------------- */
static void test_pin_reuse_matches_the_host_server(void)
{
    /* Same bits as the SWD interpretation (swd_bb.sv:187,243). */
    CHECK(XVC_SWDBB_TCK == SWDBB_DRIVE_SWCLK);
    CHECK(XVC_SWDBB_TMS == SWDBB_DRIVE_SWDIO_O);
    CHECK(XVC_SWDBB_TDI == SWDBB_DRIVE_SWDIO_OE);
    CHECK(XVC_SWDBB_TDO == SWDBB_SAMPLE_SWDIO_I);

    /* Same VALUES as socket_harness.xvc_server's DRIVE_TCK / DRIVE_TMS /
     * DRIVE_TDI / SAMPLE_TDO. */
    CHECK(XVC_SWDBB_TCK == (1u << 0));   /* swd_clk    -> TCK */
    CHECK(XVC_SWDBB_TMS == (1u << 1));   /* swd_dio_o  -> TMS */
    CHECK(XVC_SWDBB_TDI == (1u << 2));   /* swd_dio_oe -> TDI */
    CHECK(XVC_SWDBB_TDO == (1u << 0));   /* swd_dio_i  -> TDO */

    /* And the same block + offsets (shell-regmap.md §SWDBB). */
    CHECK(MPS3_SWDBB_BASE == 0x44A70000u);
    CHECK(SWDBB_DRIVE  == 0x00u);
    CHECK(SWDBB_SAMPLE == 0x04u);

    /* The advertised ceiling must match the host's XVC_MAX_VECTOR_BITS too,
     * or hw_server/Identify would size shifts for the wrong server. */
    CHECK(MPS3_XVC_MAX_VECTOR_BITS == 2048u);
    CHECK(MPS3_XVC_MAX_VECTOR_BYTES == 256u);
}

/* ---------------------------------------------------------------------------
 * CROSS-LANGUAGE GOLDEN — the vector convention, byte-for-byte against the
 * host-side server.
 *
 * The constants below were produced by the HOST implementation, not by this
 * one:
 *
 *   $ PYTHONPATH=host:host/pyverify python3 -c "...
 *       from test_xvc_server import idcode_read_sequence
 *       from socket_harness.xvc_server import bits_to_vector, FakeJtagTap, \
 *            FakeSwdbbBackend, SwdbbJtagShifter
 *       n, tms, tdi, first = idcode_read_sequence()
 *       tv, dv = bits_to_vector(tms), bits_to_vector(tdi)
 *       tdo = SwdbbJtagShifter(FakeSwdbbBackend(FakeJtagTap(0x6BA00477))).shift(n, tv, dv)"
 *     num_bits = 41  first = 9
 *     TMS  0x5F 0x00 0x00 0x00 0x00 0x01
 *     TDI  0x00 0x00 0x00 0x00 0x00 0x00
 *     TDO  0x00 0xEE 0x08 0x40 0xD7 0x00     recovered = 0x6BA00477
 *
 * Two independent derivations of the 1149.1 navigation (one in Python, one in
 * this file) must serialise to the SAME bytes, and the firmware's bit-bang must
 * return the SAME TDO the host's does. That is what "the bit order is verified
 * against the host convention" means concretely: not two prose comments that
 * agree, but identical bytes on the wire. If either side's LSB-first packing is
 * ever changed unilaterally, this fails.
 * ------------------------------------------------------------------------- */
static void test_vectors_are_byte_identical_to_the_host_server(void)
{
    static const uint8_t host_tms[IDCODE_SEQ_BYTES] =
        { 0x5F, 0x00, 0x00, 0x00, 0x00, 0x01 };
    static const uint8_t host_tdi[IDCODE_SEQ_BYTES] =
        { 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 };
    static const uint8_t host_tdo[IDCODE_SEQ_BYTES] =
        { 0x00, 0xEE, 0x08, 0x40, 0xD7, 0x00 };

    uint8_t tms[IDCODE_SEQ_BYTES], tdi[IDCODE_SEQ_BYTES], tdo[IDCODE_SEQ_BYTES];
    idcode_read_sequence(tms, tdi);

    /* The REQUEST side: identical bytes, both directions of the convention. */
    CHECK(memcmp(tms, host_tms, sizeof(tms)) == 0);
    CHECK(memcmp(tdi, host_tdi, sizeof(tdi)) == 0);

    fresh();
    CHECK(xvc_server_do_shift(IDCODE_SEQ_BITS, tms, tdi, tdo) == 0);

#if MPS3_XVC_SWDBB_SAMPLE_LATE
    /* NEGATIVE CONTROL: the inverted phase cannot reproduce the host's TDO. */
    CHECK(memcmp(tdo, host_tdo, sizeof(tdo)) != 0);
#else
    /* The RESPONSE side: the firmware bit-bang returns exactly what the host
     * server returns for the same scan of the same TAP. */
    CHECK(memcmp(tdo, host_tdo, sizeof(tdo)) == 0);
#endif
}

/* ---------------------------------------------------------------------------
 * The TAP model itself. A broken model would make every proof below worthless,
 * so it is exercised directly first (mirrors the host suite's three
 * test_fake_tap_* cases).
 * ------------------------------------------------------------------------- */
static void test_fake_tap_is_a_faithful_1149_1_controller(void)
{
    fake_tap_t t;

    /* Five TMS=1 edges reach Test-Logic-Reset from ANYWHERE, and TLR selects
     * IDCODE. */
    fake_tap_reset(&t, IDCODE);
    for (int i = 0; i < 3; i++) { /* wander off into the DR column */
        fake_tap_tick(&t, 1u, 0u);
        fake_tap_tick(&t, 0u, 0u);
    }
    for (int i = 0; i < 5; i++) {
        fake_tap_tick(&t, 1u, 0u);
    }
    CHECK(t.state == FAKE_TAP_TLR);
    CHECK(t.ir == FAKE_TAP_IR_IDCODE);

    /* Capture-DR loads the DR on the same edge that enters Shift-DR, so TDO
     * presents IDCODE bit 0 BEFORE the next edge. */
    fake_tap_reset(&t, IDCODE);
    static const uint32_t nav[9] = { 1, 1, 1, 1, 1, 0, 1, 0, 0 };
    for (int i = 0; i < 9; i++) {
        fake_tap_tick(&t, nav[i], 0u);
    }
    CHECK(t.state == FAKE_TAP_SHIFT_DR);
    CHECK(fake_tap_tdo(&t) == (IDCODE & 1u));

    /* An IR shift really selects BYPASS, and BYPASS really captures a 1-bit
     * zero register (so the model can tell instructions apart). */
    fake_tap_reset(&t, IDCODE);
    static const uint32_t ir_nav[10] = { 1, 1, 1, 1, 1, 0, 1, 1, 0, 0 };
    for (int i = 0; i < 10; i++) {
        fake_tap_tick(&t, ir_nav[i], 0u);
    }
    CHECK(t.state == FAKE_TAP_SHIFT_IR);
    for (int k = 0; k < 4; k++) {
        fake_tap_tick(&t, (k == 3) ? 1u : 0u, 1u); /* shift 4 ones = BYPASS */
    }
    fake_tap_tick(&t, 1u, 0u); /* Exit1-IR -> Update-IR */
    fake_tap_tick(&t, 0u, 0u); /* Update-IR latches, -> RTI */
    CHECK(t.ir == FAKE_TAP_IR_BYPASS);
}

/* ---------------------------------------------------------------------------
 * HEADLINE: a real IDCODE read through the real firmware shift loop.
 * ------------------------------------------------------------------------- */
static void test_idcode_read_through_do_shift(void)
{
    fresh();

    uint8_t tms[IDCODE_SEQ_BYTES], tdi[IDCODE_SEQ_BYTES], tdo[IDCODE_SEQ_BYTES];
    idcode_read_sequence(tms, tdi);
    CHECK(xvc_server_do_shift(IDCODE_SEQ_BITS, tms, tdi, tdo) == 0);

    check_recovered_idcode(recover32(tdo, IDCODE_FIRST_BIT), IDCODE);

    /* Exactly one rising edge per shift bit, and the vector landed the TAP in
     * Exit1-DR because the last shift bit carried TMS=1. */
    CHECK(s_swdbb.tap.rising_edges == IDCODE_SEQ_BITS);
    CHECK(s_swdbb.tap.state == FAKE_TAP_EXIT1_DR);
    /* One SAMPLE read per bit, and nothing outside the two mapped offsets. */
    CHECK(s_swdbb.sample_reads == (int)IDCODE_SEQ_BITS);
    CHECK(s_swdbb.stray == 0);
}

static void test_a_different_idcode_comes_back_different(void)
{
    /* Guards against the model "returning IDCODE" because the test asked for
     * it — the same guard the host suite has. */
    static const uint32_t values[4] = { 0x0BB11477u, 0xFFFFFFFFu, 0x00000001u,
                                        0xDEADBEEFu };
    for (int i = 0; i < 4; i++) {
        fresh_with_idcode(values[i]);
        uint8_t tms[IDCODE_SEQ_BYTES], tdi[IDCODE_SEQ_BYTES], tdo[IDCODE_SEQ_BYTES];
        idcode_read_sequence(tms, tdi);
        CHECK(xvc_server_do_shift(IDCODE_SEQ_BITS, tms, tdi, tdo) == 0);
        check_recovered_idcode(recover32(tdo, IDCODE_FIRST_BIT), values[i]);
    }
}

/* ---------------------------------------------------------------------------
 * NEGATIVE CONTROL (bit order): the same TDO bits repacked MSB-first within
 * each byte do NOT recover the IDCODE. Proves the LSB-first packing is
 * load-bearing and not an accident of a symmetric test vector.
 * ------------------------------------------------------------------------- */
static void test_msb_first_packing_would_not_recover_the_idcode(void)
{
    fresh();
    uint8_t tms[IDCODE_SEQ_BYTES], tdi[IDCODE_SEQ_BYTES], tdo[IDCODE_SEQ_BYTES];
    idcode_read_sequence(tms, tdi);
    CHECK(xvc_server_do_shift(IDCODE_SEQ_BITS, tms, tdi, tdo) == 0);

    uint8_t msb_first[IDCODE_SEQ_BYTES];
    memset(msb_first, 0, sizeof(msb_first));
    for (uint32_t i = 0; i < IDCODE_SEQ_BITS; i++) {
        if ((tdo[i >> 3] >> (i & 7u)) & 1u) {
            msb_first[i >> 3] |= (uint8_t)(1u << (7u - (i & 7u)));
        }
    }
    CHECK(recover32(msb_first, IDCODE_FIRST_BIT) != IDCODE);
}

/* ---------------------------------------------------------------------------
 * The DRIVE-word control: the exact ordered register trace per JTAG bit.
 * Expectations are BUILT from the documented sequence (TMS/TDI settled with
 * TCK low -> sample -> one rising edge), not transcribed from a run, so a
 * refactor that reorders the accesses fails here rather than on silicon.
 * ------------------------------------------------------------------------- */
static void test_drive_word_sequence_per_bit(void)
{
    fresh();

    /* 4 bits: TMS = 1,0,1,1   TDI = 0,1,1,0 (all four TMS/TDI combinations). */
    static const uint32_t tms_bits[4] = { 1, 0, 1, 1 };
    static const uint32_t tdi_bits[4] = { 0, 1, 1, 0 };
    uint8_t tms[1], tdi[1], tdo[1];
    bits_to_vector(tms_bits, 4u, tms);
    bits_to_vector(tdi_bits, 4u, tdi);
    CHECK(xvc_server_do_shift(4u, tms, tdi, tdo) == 0);

    fake_swdbb_access_t want[FAKE_SWDBB_TRACE_MAX];
    int n = 0;
    for (int i = 0; i < 4; i++) {
        uint32_t w = (tms_bits[i] ? XVC_SWDBB_TMS : 0u) |
                     (tdi_bits[i] ? XVC_SWDBB_TDI : 0u);
        /* TCK low, data settled (+ any stretch holds at the same value). */
        for (int k = 0; k <= STRETCH; k++) {
            want[n].is_write = 1; want[n].off = SWDBB_DRIVE; want[n].val = w; n++;
        }
#if !MPS3_XVC_SWDBB_SAMPLE_LATE
        want[n].is_write = 0; want[n].off = SWDBB_SAMPLE; n++;
#endif
        /* Rising edge (+ stretch holds). */
        for (int k = 0; k <= STRETCH; k++) {
            want[n].is_write = 1; want[n].off = SWDBB_DRIVE;
            want[n].val = w | XVC_SWDBB_TCK; n++;
        }
#if MPS3_XVC_SWDBB_SAMPLE_LATE
        want[n].is_write = 0; want[n].off = SWDBB_SAMPLE; n++;
#endif
    }
    /* Park: the LAST bit's TMS/TDI with TCK clear (bit 3 was TMS=1, TDI=0). */
    want[n].is_write = 1; want[n].off = SWDBB_DRIVE; want[n].val = XVC_SWDBB_TMS; n++;

    CHECK(s_swdbb.n_trace == n);
    for (int i = 0; i < n; i++) {
        CHECK(s_swdbb.trace[i].is_write == want[i].is_write);
        CHECK(s_swdbb.trace[i].off == want[i].off);
        if (want[i].is_write) {
            CHECK(s_swdbb.trace[i].val == want[i].val);
        }
    }

    /* Whatever the stretch, exactly 4 edges — a hold is never an edge. */
    CHECK(s_swdbb.tap.rising_edges == 4u);
    CHECK(s_swdbb.stray == 0);
}

/* Cost model, asserted: (3 + 2*STRETCH) accesses per bit, plus one park.
 * At the default stretch of 0 that is the 3n+1 the plan's throughput estimate
 * is built on. */
static void test_access_count_is_three_per_bit_plus_park(void)
{
    static const uint32_t widths[3] = { 1u, 8u, 77u };
    for (int w = 0; w < 3; w++) {
        fresh();
        uint8_t tms[MPS3_XVC_MAX_VECTOR_BYTES], tdi[MPS3_XVC_MAX_VECTOR_BYTES],
                tdo[MPS3_XVC_MAX_VECTOR_BYTES];
        memset(tms, 0x5A, sizeof(tms));
        memset(tdi, 0xC3, sizeof(tdi));
        CHECK(xvc_server_do_shift(widths[w], tms, tdi, tdo) == 0);
        CHECK(s_swdbb.ops == (int)((3u + 2u * (uint32_t)STRETCH) * widths[w] + 1u));
        CHECK(s_swdbb.sample_reads == (int)widths[w]);
        CHECK(s_swdbb.drive_writes ==
              (int)((2u + 2u * (uint32_t)STRETCH) * widths[w] + 1u));
        CHECK(s_swdbb.stray == 0);
    }
}

/* The park exists so back-to-back shifts do not smear: TCK idles low, so the
 * next shift's first write cannot be a rising edge. */
static void test_back_to_back_shifts_make_no_spurious_edge(void)
{
    fresh();
    uint8_t tms[IDCODE_SEQ_BYTES], tdi[IDCODE_SEQ_BYTES], tdo[IDCODE_SEQ_BYTES];
    idcode_read_sequence(tms, tdi);

    for (int rep = 0; rep < 3; rep++) {
        CHECK(xvc_server_do_shift(IDCODE_SEQ_BITS, tms, tdi, tdo) == 0);
        /* Each repeat re-enters TLR from wherever it was and re-reads IDCODE,
         * so the value must come back every time, not just the first. */
        check_recovered_idcode(recover32(tdo, IDCODE_FIRST_BIT), IDCODE);
    }
    CHECK(s_swdbb.tap.rising_edges == 3u * IDCODE_SEQ_BITS);
}

/* xvc_server_init() must leave TCK low before any shift runs. */
static void test_init_parks_the_pins_with_tck_low(void)
{
    mock_regs_reset();
    fake_net_reset();
    memset(&g_shell_state, 0, sizeof(g_shell_state));
    memset(&s_swdbb, 0, sizeof(s_swdbb));
    fake_tap_reset(&s_swdbb.tap, IDCODE);
    s_swdbb.drive = XVC_SWDBB_TCK | XVC_SWDBB_TMS; /* leftover from an SWD session */
    mock_regs_set_hook(MPS3_SWDBB_BASE, fake_swdbb_hook, &s_swdbb);

    xvc_server_init();

    CHECK(s_swdbb.drive_writes == 1);
    CHECK(s_swdbb.drive == 0u);                       /* TCK low, TMS/TDI low */
    CHECK(!(s_swdbb.drive & XVC_SWDBB_TCK));
    CHECK(s_swdbb.tap.rising_edges == 0u);            /* parking is not an edge */
    CHECK(s_swdbb.stray == 0);
}

/* ---------------------------------------------------------------------------
 * The vector ceiling — and the distinction that matters most on this path.
 *
 * What getinfo: ADVERTISES (MPS3_XVC_MAX_VECTOR_BITS, 2048) is NOT the bound on
 * a legitimate request. A client sizes its shift PAYLOAD against the advertised
 * number and then adds TAP state-navigation TMS bits, so num_bits arrives OVER
 * it — measured at 2048 + 5 for Synopsys Identify, the tool that drives exactly
 * this SWDBB path (xvc_server.h has the three-point measurement;
 * test_xvc_identify_stream.c replays the real bytes). The real bound is
 * MPS3_XVC_ACCEPT_VECTOR_BITS.
 *
 * So this checks three things: rejection above the ACCEPT ceiling, a maximal
 * shift at the ADVERTISED size still working, and — the case that was broken —
 * an OVER-ADVERTISED shift really being clocked bit for bit.
 * ------------------------------------------------------------------------- */
static void test_max_vector_ceiling(void)
{
    uint8_t tms[MPS3_XVC_ACCEPT_VECTOR_BYTES], tdi[MPS3_XVC_ACCEPT_VECTOR_BYTES],
            tdo[MPS3_XVC_ACCEPT_VECTOR_BYTES];
    memset(tms, 0, sizeof(tms));
    memset(tdi, 0, sizeof(tdi));

    /* Over the ACCEPT ceiling and zero: rejected BEFORE any register is
     * touched. (Above the ceiling we fail closed rather than clamp: a short TDO
     * reply reads to the client as real captured data.) */
    fresh();
    CHECK(xvc_server_do_shift(0u, tms, tdi, tdo) == -1);
    CHECK(xvc_server_do_shift(MPS3_XVC_ACCEPT_VECTOR_BITS + 1u, tms, tdi, tdo) == -1);
    CHECK(s_swdbb.ops == 0);
    CHECK(s_swdbb.tap.rising_edges == 0u);
    /* ...but a shift merely LARGER THAN ADVERTISED is legal and must not be
     * refused. This single assertion is the one that fails on the pre-fix
     * firmware, and it is Identify's actual request size. */
    CHECK(MPS3_XVC_ACCEPT_VECTOR_BITS > MPS3_XVC_MAX_VECTOR_BITS);
    CHECK(xvc_server_do_shift(MPS3_XVC_MAX_VECTOR_BITS + 5u, tms, tdi, tdo) == 0);

    /* Exactly at the ceiling: a real maximal shift. TMS=0 throughout keeps the
     * TAP in Shift-DR after the 9-edge navigation, so all 2048 edges land and
     * the whole vector really is clocked. */
    fresh();
    static const uint32_t nav[IDCODE_FIRST_BIT] = { 1, 1, 1, 1, 1, 0, 1, 0, 0 };
    for (uint32_t i = 0; i < IDCODE_FIRST_BIT; i++) {
        if (nav[i]) {
            tms[i >> 3] |= (uint8_t)(1u << (i & 7u));
        }
    }
    CHECK(xvc_server_do_shift(MPS3_XVC_MAX_VECTOR_BITS, tms, tdi, tdo) == 0);
    CHECK(s_swdbb.tap.rising_edges == MPS3_XVC_MAX_VECTOR_BITS);
    CHECK(s_swdbb.tap.state == FAKE_TAP_SHIFT_DR);
    CHECK(s_swdbb.sample_reads == (int)MPS3_XVC_MAX_VECTOR_BITS);
    CHECK(s_swdbb.ops ==
          (int)((3u + 2u * (uint32_t)STRETCH) * MPS3_XVC_MAX_VECTOR_BITS + 1u));
    /* The IDCODE is still in there, at the same offset. */
    check_recovered_idcode(recover32(tdo, IDCODE_FIRST_BIT), IDCODE);
    /* ... and the bits past the DR are the zeros a 1-bit-per-edge shift-out of
     * an exhausted 32-bit DR gives (TDI=0 shifted in), i.e. no stale data. */
    for (uint32_t i = IDCODE_FIRST_BIT + 32u; i < MPS3_XVC_MAX_VECTOR_BITS; i++) {
        CHECK(((tdo[i >> 3] >> (i & 7u)) & 1u) == 0u);
    }

    /* THE case the pre-fix firmware refused: 2053 bits, 5 more than we
     * advertise, which is precisely what Identify asks for. Not merely
     * accepted — every one of the 2053 edges must actually be clocked, the
     * partial final byte included (2053 = 256*8 + 5, so the last byte carries
     * 5 significant bits and 3 pad bits). */
    fresh();
    const uint32_t over = MPS3_XVC_MAX_VECTOR_BITS + 5u;
    CHECK(over == 2053u);
    CHECK((over + 7u) / 8u == 257u);
    CHECK(xvc_server_do_shift(over, tms, tdi, tdo) == 0);
    CHECK(s_swdbb.tap.rising_edges == over);
    CHECK(s_swdbb.sample_reads == (int)over);
    CHECK(s_swdbb.ops == (int)((3u + 2u * (uint32_t)STRETCH) * over + 1u));
    CHECK(s_swdbb.tap.state == FAKE_TAP_SHIFT_DR);
    check_recovered_idcode(recover32(tdo, IDCODE_FIRST_BIT), IDCODE);
    /* The 3 pad bits of the final partial byte must be zero, not stale: the
     * client reads only the low 5, but a nonzero pad means the writer indexed
     * past num_bits. */
    CHECK((tdo[256] & 0xE0u) == 0u);
}

/* ---- framing (the shared protocol engine, over the wire) ------------------- */

/* Send one complete shift command; returns bytes sent. */
static int send_shift(int cli, uint32_t num_bits, const uint8_t *tms, const uint8_t *tdi)
{
    uint8_t buf[16 + 2 * MPS3_XVC_ACCEPT_VECTOR_BYTES];
    uint32_t nb = (num_bits + 7u) / 8u;
    memcpy(buf, "shift:", 6);
    put_le32(buf + 6, num_bits);
    memcpy(buf + 10, tms, nb);
    memcpy(buf + 10 + nb, tdi, nb);
    return fake_net_send(cli, buf, (int)(10 + 2 * nb));
}

static void test_getinfo_and_settck_touch_no_pins(void)
{
    fresh();
    int cli = fake_net_connect(MPS3_PORT_XVC);
    CHECK(cli >= 0);

    CHECK(fake_net_send(cli, "getinfo:", 8) == 8);
    polls(2);
    char rsp[64];
    int n = fake_net_recv(cli, rsp, sizeof(rsp) - 1);
    CHECK(n > 0);
    rsp[n] = '\0';
    char expect[64];
    snprintf(expect, sizeof(expect), "xvcServer_v1.0:%u\n",
             (unsigned)MPS3_XVC_MAX_VECTOR_BITS);
    CHECK(strcmp(rsp, expect) == 0);

    /* settck is stored + echoed only: the bit-bang period is set by AXI-write
     * pace (MPS3_XVC_SWDBB_TCK_STRETCH), not by a divider register. */
    uint8_t cmd[11];
    memcpy(cmd, "settck:", 7);
    put_le32(cmd + 7, 10000u);
    CHECK(fake_net_send(cli, cmd, 11) == 11);
    polls(2);
    uint8_t echo[8];
    CHECK(fake_net_recv(cli, echo, sizeof(echo)) == 4);
    CHECK(memcmp(echo, cmd + 7, 4) == 0);

    /* Neither verb may poke a pin. */
    CHECK(s_swdbb.ops == 0);
    CHECK(s_swdbb.tap.rising_edges == 0u);
}

/* The end-to-end proof: an IDCODE read carried by the real XVC wire protocol,
 * delivered in hostile fragments, into the real bit-bang loop. */
static void test_idcode_over_the_wire_in_fragments(void)
{
    fresh();
    int cli = fake_net_connect(MPS3_PORT_XVC);
    polls(1);

    uint8_t tms[IDCODE_SEQ_BYTES], tdi[IDCODE_SEQ_BYTES];
    idcode_read_sequence(tms, tdi);

    uint8_t full[10 + 2 * IDCODE_SEQ_BYTES];
    memcpy(full, "shift:", 6);
    put_le32(full + 6, IDCODE_SEQ_BITS);
    memcpy(full + 10, tms, IDCODE_SEQ_BYTES);
    memcpy(full + 10 + IDCODE_SEQ_BYTES, tdi, IDCODE_SEQ_BYTES);

    /* Prefix split mid-word ("shi"), length split, vectors split — nothing may
     * shift until the final byte arrives. 3+5+6+4 = 18 of 22 bytes. */
    int cuts[4] = { 3, 5, 6, 4 };
    int at = 0;
    for (int c = 0; c < 4; c++) {
        CHECK(fake_net_send(cli, full + at, cuts[c]) == cuts[c]);
        at += cuts[c];
        polls(2);
        CHECK(s_swdbb.ops == 0); /* still incomplete: not one pin poked */
    }
    CHECK(at < (int)sizeof(full));
    CHECK(fake_net_send(cli, full + at, (int)sizeof(full) - at) ==
          (int)sizeof(full) - at);
    polls(3);

    uint8_t rsp[IDCODE_SEQ_BYTES];
    CHECK(fake_net_recv(cli, rsp, sizeof(rsp)) == (int)IDCODE_SEQ_BYTES);
    check_recovered_idcode(recover32(rsp, IDCODE_FIRST_BIT), IDCODE);
    CHECK(s_swdbb.tap.rising_edges == IDCODE_SEQ_BITS);
}

static void test_gated_shift_stalls_then_services(void)
{
    fresh();
    int cli = fake_net_connect(MPS3_PORT_XVC);
    polls(1);

    g_shell_state.xvc_gated = true;

    uint8_t tms[IDCODE_SEQ_BYTES], tdi[IDCODE_SEQ_BYTES];
    idcode_read_sequence(tms, tdi);
    CHECK(send_shift(cli, IDCODE_SEQ_BITS, tms, tdi) > 0);
    polls(5);
    CHECK(s_swdbb.ops == 0); /* no SWDBB access at all while gated */
    uint8_t rsp[IDCODE_SEQ_BYTES];
    CHECK(fake_net_recv(cli, rsp, sizeof(rsp)) == 0); /* and no reply */
    CHECK(!fake_net_fw_closed(cli));                  /* stalled, NOT dropped */

    g_shell_state.xvc_gated = false;
    polls(2);
    CHECK(fake_net_recv(cli, rsp, sizeof(rsp)) == (int)IDCODE_SEQ_BYTES);
    check_recovered_idcode(recover32(rsp, IDCODE_FIRST_BIT), IDCODE);
}

static void test_junk_and_oversize_fail_closed(void)
{
    fresh();
    int cli = fake_net_connect(MPS3_PORT_XVC);
    CHECK(fake_net_send(cli, "quit:", 5) == 5); /* not an XVC verb */
    polls(2);
    CHECK(fake_net_fw_closed(cli));
    CHECK(s_swdbb.ops == 0);
    fake_net_close(cli);

    /* num_bits over the ACCEPT ceiling: dropped before a vector byte is even
     * awaited, and before any pin moves. NOT the advertised size — a shift
     * merely larger than we advertise is legal and is what Identify sends
     * (test_max_vector_ceiling covers that case). */
    int cli2 = fake_net_connect(MPS3_PORT_XVC);
    polls(1);
    uint8_t cmd[10];
    memcpy(cmd, "shift:", 6);
    put_le32(cmd + 6, MPS3_XVC_ACCEPT_VECTOR_BITS + 1u);
    CHECK(fake_net_send(cli2, cmd, 10) == 10);
    polls(2);
    CHECK(fake_net_fw_closed(cli2));
    CHECK(s_swdbb.ops == 0);
    fake_net_close(cli2);

    /* num_bits == 0 — equally malformed. */
    int cli3 = fake_net_connect(MPS3_PORT_XVC);
    polls(1);
    put_le32(cmd + 6, 0);
    CHECK(fake_net_send(cli3, cmd, 10) == 10);
    polls(2);
    CHECK(fake_net_fw_closed(cli3));
    CHECK(s_swdbb.ops == 0);
}

/* A maximal shift really does arrive and reply over the wire (522 B in,
 * 256 B out) — the size Identify will actually use. */
static void test_max_vector_over_the_wire(void)
{
    fresh();
    int cli = fake_net_connect(MPS3_PORT_XVC);
    polls(1);

    uint8_t tms[MPS3_XVC_MAX_VECTOR_BYTES], tdi[MPS3_XVC_MAX_VECTOR_BYTES];
    memset(tms, 0, sizeof(tms));
    memset(tdi, 0, sizeof(tdi));
    static const uint32_t nav[IDCODE_FIRST_BIT] = { 1, 1, 1, 1, 1, 0, 1, 0, 0 };
    for (uint32_t i = 0; i < IDCODE_FIRST_BIT; i++) {
        if (nav[i]) {
            tms[i >> 3] |= (uint8_t)(1u << (i & 7u));
        }
    }
    CHECK(send_shift(cli, MPS3_XVC_MAX_VECTOR_BITS, tms, tdi) ==
          (int)(10 + 2 * MPS3_XVC_MAX_VECTOR_BYTES));
    polls(4);

    uint8_t rsp[MPS3_XVC_MAX_VECTOR_BYTES];
    int got = 0;
    for (int i = 0; i < 8 && got < (int)sizeof(rsp); i++) {
        int n = fake_net_recv(cli, rsp + got, (int)sizeof(rsp) - got);
        CHECK(n >= 0);
        got += n;
        polls(1);
    }
    CHECK(got == (int)MPS3_XVC_MAX_VECTOR_BYTES);
    check_recovered_idcode(recover32(rsp, IDCODE_FIRST_BIT), IDCODE);
    CHECK(s_swdbb.tap.rising_edges == MPS3_XVC_MAX_VECTOR_BITS);
}

int main(void)
{
    test_this_binary_is_the_swdbb_target();
    test_pin_reuse_matches_the_host_server();
    test_vectors_are_byte_identical_to_the_host_server();
    test_fake_tap_is_a_faithful_1149_1_controller();
    test_idcode_read_through_do_shift();
    test_a_different_idcode_comes_back_different();
    test_msb_first_packing_would_not_recover_the_idcode();
    test_drive_word_sequence_per_bit();
    test_access_count_is_three_per_bit_plus_park();
    test_back_to_back_shifts_make_no_spurious_edge();
    test_init_parks_the_pins_with_tck_low();
    test_max_vector_ceiling();
    test_getinfo_and_settck_touch_no_pins();
    test_idcode_over_the_wire_in_fragments();
    test_gated_shift_stalls_then_services();
    test_junk_and_oversize_fail_closed();
    test_max_vector_over_the_wire();

    printf("%s: %d checks passed\n", XVC_SWDBB_TEST_NAME, s_checks);
    return 0;
}
