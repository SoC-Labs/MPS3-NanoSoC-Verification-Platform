/*
 * fake_jtag_tap.h — the in-memory IEEE-1149.1 TAP plus the behavioral SWDBB
 * register model (fpga/shell/ip/swd_bb/swd_bb.sv, in C) that the firmware's
 * SWDBB XVC bit-bang is proven against.
 *
 * EXTRACTED from firmware/test/test_xvc_server_swdbb.c so that more than one
 * program can drive the SAME model instead of keeping divergent copies. Users:
 *
 *   test_xvc_server_swdbb.c       3 binaries — positive, the late-sample
 *                                 NEGATIVE CONTROL, the TCK-stretch variant.
 *                                 Calls xvc_server_do_shift()/xvc_server_poll()
 *                                 directly over fake_net_if.c's in-memory queues.
 *   test_xvc_posix_loopback.c     2 binaries — the same IDCODE read carried over
 *                                 a REAL loopback TCP socket via posix_net_if.c.
 *   xvc_fw_daemon.c               `make -C firmware/test tools` — the host
 *                                 executable that lets the REAL Synopsys
 *                                 Identify debugger drive the REAL firmware XVC
 *                                 engine with no board and no MicroBlaze.
 *
 * This is the C mirror of host/socket_harness/xvc_server.py's FakeJtagTap /
 * FakeSwdbbBackend, and the two are cross-pinned byte-for-byte by
 * test_xvc_server_swdbb.c's test_vectors_are_byte_identical_to_the_host_server().
 * Change one and that test fails — which is the point of having two.
 *
 * ===========================================================================
 * DELIBERATELY NOT parameterised by the two XVC compile-time knobs
 * ===========================================================================
 * MPS3_XVC_SWDBB_TCK_STRETCH and MPS3_XVC_SWDBB_SAMPLE_LATE change what the
 * PRODUCTION shift loop DOES (how many idempotent DRIVE re-writes it issues per
 * phase, and whether it reads SAMPLE before or after the rising edge). This
 * model only ever OBSERVES those accesses; it never predicts them:
 *
 *   - a TCK edge exists if and only if DRIVE[0] transitions 0 -> 1, exactly as
 *     in swd_bb.sv, so a stretch's repeated same-value writes cannot
 *     manufacture one — no stretch term appears anywhere below;
 *   - SAMPLE always returns the bit the TAP is presenting AT THAT MOMENT, so a
 *     read moved to the wrong phase returns the wrong bit, which is precisely
 *     the IDCODE >> 1 damage the late-sample negative control asserts.
 *
 * So this translation unit compiles identically for every binary that links it
 * and both knobs stay honest: no expectation of either is baked in here. The
 * per-binary EXPECTATIONS do depend on them, and those stay in the tests.
 */
#ifndef MPS3_FAKE_JTAG_TAP_H
#define MPS3_FAKE_JTAG_TAP_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ---- fake TAP: a minimal but faithful IEEE 1149.1 controller ---------------
 * Actions are taken on the edge at which the controller IS IN the state, then
 * the state advances — so Capture-DR loads the DR on the same edge that enters
 * Shift-DR, and the first TDO read taken in Shift-DR is DR bit 0. That is what
 * makes an IDCODE read come out aligned rather than off by one.
 * ------------------------------------------------------------------------- */
typedef enum {
    FAKE_TAP_TLR = 0, FAKE_TAP_RTI, FAKE_TAP_SEL_DR, FAKE_TAP_CAP_DR,
    FAKE_TAP_SHIFT_DR, FAKE_TAP_EXIT1_DR, FAKE_TAP_PAUSE_DR, FAKE_TAP_EXIT2_DR,
    FAKE_TAP_UPDATE_DR, FAKE_TAP_SEL_IR, FAKE_TAP_CAP_IR, FAKE_TAP_SHIFT_IR,
    FAKE_TAP_EXIT1_IR, FAKE_TAP_PAUSE_IR, FAKE_TAP_EXIT2_IR, FAKE_TAP_UPDATE_IR,
    FAKE_TAP_NSTATES
} fake_tap_state_t;

#define FAKE_TAP_IR_LEN    4u
#define FAKE_TAP_IR_IDCODE 0x1u
#define FAKE_TAP_IR_BYPASS 0xFu

typedef struct {
    uint32_t         idcode;
    fake_tap_state_t state;
    uint32_t         ir;
    uint32_t         ir_shift;
    uint32_t         dr;
    uint32_t         dr_len;
    uint32_t         rising_edges;
} fake_tap_t;

/* Park the model in Test-Logic-Reset with `idcode` loadable. 1149.1: TLR
 * selects the IDCODE instruction, so a bare 5-ones-then-navigate scan reads it
 * with no IR shift at all. */
void fake_tap_reset(fake_tap_t *t, uint32_t idcode);

/* The bit the TAP is presenting NOW — valid while TCK is low, i.e. what a host
 * must read BEFORE the next rising edge. Only driven in the two Shift states;
 * 0 elsewhere (a real TAP tri-states TDO outside Shift/Exit, and the shell's
 * swd_dio_i then reads whatever the RM ties it to — 0 is the honest stand-in,
 * and also what the DFX decoupler drives while the RP is isolated). */
uint32_t fake_tap_tdo(const fake_tap_t *t);

/* One TCK RISING edge: the TAP consumes tms/tdi, then advances. */
void fake_tap_tick(fake_tap_t *t, uint32_t tms, uint32_t tdi);

/* ---- behavioral SWDBB fake (swd_bb.sv, in C) -------------------------------
 * DRIVE @0x00 rw keeps only [2:0]; SAMPLE @0x04 is RO and returns the live
 * inbound pin. Every OTHER offset in the page is unmapped: reads 0, writes
 * ignored (swd_bb.sv's full-address decode, RESOLVED ambiguity #1 in that
 * file) — counted here as `stray` so a production access outside the two
 * mapped offsets fails a test.
 *
 * The pin masks come from xvc_server.h's XVC_SWDBB_* (which are themselves
 * derived from platform_regs.h's SWD names), NOT from retyped literals: the
 * model and the production code must move together, and
 * test_pin_reuse_matches_the_host_server() independently pins those macros to
 * the host server's values.
 * ------------------------------------------------------------------------- */
#define FAKE_SWDBB_TRACE_MAX 512

typedef struct {
    int      is_write;
    uint32_t off;
    uint32_t val;   /* value written, or value returned on a read */
} fake_swdbb_access_t;

typedef struct {
    uint32_t   drive;        /* DRIVE[2:0], write-through */
    fake_tap_t tap;
    int        ops;          /* every access, mapped or not */
    int        sample_reads;
    int        drive_writes;
    int        stray;        /* accesses outside 0x00 / 0x04 */
    fake_swdbb_access_t trace[FAKE_SWDBB_TRACE_MAX];
    int        n_trace;      /* saturates at FAKE_SWDBB_TRACE_MAX (house pattern) */
} fake_swdbb_t;

/* The mock_regs behavioral hook (mock_regs.h mock_regs_hook_fn). Install with
 *     mock_regs_set_hook(MPS3_SWDBB_BASE, fake_swdbb_hook, &my_swdbb);
 * after mock_regs_reset() (which drops hooks) and after fake_tap_reset()ing
 * `my_swdbb.tap`. `ctx` is the fake_swdbb_t*. */
int fake_swdbb_hook(void *ctx, int is_write, uint32_t base, uint32_t off, uint32_t *val);

/* Zero the counters + trace WITHOUT disturbing DRIVE or the TAP, so a caller
 * can ignore xvc_server_init()'s park write (which has its own test). */
void fake_swdbb_clear_counters(fake_swdbb_t *d);

/* ---- XVC vector helpers -----------------------------------------------------
 * The XVC vector convention (LSB-first WITHIN each byte: shift bit i lives at
 * vec[i/8] bit i%8) and the 1149.1 navigation for an IDCODE read, as reusable
 * builders. Callers that want to prove the TRANSPORT (test_xvc_posix_loopback.c)
 * use these; test_xvc_server_swdbb.c deliberately keeps its OWN private
 * restatement of the same convention, because its job is to prove the convention
 * itself and a shared helper cannot cross-check a shared helper.
 *
 * Both are pinned to the golden bytes host/socket_harness/xvc_server.py
 * produced, in test_xvc_server_swdbb.c (its own copies) and in
 * test_xvc_posix_loopback.c (these).
 * ------------------------------------------------------------------------- */

/* Pack per-bit values into an XVC vector, LSB-first within each byte. */
void fake_xvc_bits_to_vector(const uint32_t *bits, uint32_t n, uint8_t *out);

/* Reassemble 32 bits out of a vector starting at bit `first`, LSB first. */
uint32_t fake_xvc_recover32(const uint8_t *vec, uint32_t first);

/* Little-endian u32, the XVC framing's length/period encoding. */
void fake_xvc_put_le32(uint8_t *p, uint32_t v);

/* An IDCODE read expressed as an XVC shift, DERIVED from the 1149.1 controller
 * graph rather than copied from a capture:
 *
 *   rising edge   TMS   resulting state
 *   1..5            1   Test-Logic-Reset (5 ones from ANY state); TLR selects
 *                       the IDCODE instruction
 *   6               0   Run-Test/Idle
 *   7               1   Select-DR-Scan
 *   8               0   Capture-DR
 *   9               0   Shift-DR (the DR was loaded with IDCODE on this same
 *                       edge, in Capture-DR)
 *   10..41          0   shift, the last one with TMS=1 -> Exit1-DR
 *
 * TDO for shift bit i is read while TCK is low, i.e. BEFORE edge i+1. Bit 9 is
 * therefore the first read taken in Shift-DR, and bits 9..40 carry IDCODE bits
 * 0..31 — hence FAKE_TAP_IDCODE_FIRST_BIT.
 *
 * Both output vectors are FAKE_TAP_IDCODE_SEQ_BYTES long. */
#define FAKE_TAP_IDCODE_SEQ_BITS  41u
#define FAKE_TAP_IDCODE_SEQ_BYTES ((FAKE_TAP_IDCODE_SEQ_BITS + 7u) / 8u)
#define FAKE_TAP_IDCODE_FIRST_BIT 9u
void fake_tap_idcode_read_sequence(uint8_t *tms_vec, uint8_t *tdi_vec);

#ifdef __cplusplus
}
#endif

#endif /* MPS3_FAKE_JTAG_TAP_H */
