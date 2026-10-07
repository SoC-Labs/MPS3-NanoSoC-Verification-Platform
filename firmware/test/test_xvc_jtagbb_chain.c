/*
 * test_xvc_jtagbb_chain.c — the REAL firmware XVC engine, built for the JTAGBB
 * target, driving a TWO-TAP JTAG CHAIN over a real loopback TCP socket.
 *
 * WHY THIS FILE EXISTS
 * ====================
 * Two things changed under the XVC server and neither had a host test:
 *
 *  1. THE TARGET. `-DMPS3_XVC_TARGET_SWDBB` names swd_bb.sv, a block the A6
 *     SWD->JTAG cutover DELETED. The live block is fpga/shell/ip/jtag_bb, which
 *     the fielded BD instantiates at the SAME 0x44A7_0000 page with the SAME
 *     bit positions — so the old build still works, by coincidence, through the
 *     names of something that no longer exists. `-DMPS3_XVC_TARGET_JTAGBB` is
 *     the honest spelling and this is its test. (The coincidence itself is now
 *     a compile-time assertion in xvc_server.h; see
 *     xvc_bb_layout_matches_the_retired_swdbb_names.)
 *
 *  2. THE CHAIN. The IICE reconfigurable module no longer gets a debug wire-set
 *     to itself: Identify's soft TAP is daisy-chained with the DUT's CoreSight
 *     SoC-400 SWJ-DP on the one jtag_* partition wire-set
 *     (docs/planning/IICE_JTAG_CHAIN.md, fpga/rp/nanosoc_iice/). So the bits
 *     this engine shifts now address TWO devices.
 *
 * WHAT THIS PROVES, AND THE POINT IT IS MAKING
 * ============================================
 * That the engine needs NO chain awareness whatsoever. It shifts the TMS/TDI
 * vectors it is handed and returns TDO; which TAP those bits address, and how
 * many BYPASS bits pad them, is the CLIENT's arithmetic (OpenOCD's
 * `jtag newtap` list, Identify's `chain add`). A bit shifter that knew about
 * TAPs would be a second place for the chain to be described and therefore a
 * second place for it to be wrong. This test drives a chain through the
 * unmodified engine and shows both devices answering.
 *
 * THE CHAIN MODEL, AND WHY IT IS A SECOND MODEL
 * =============================================
 * firmware/test/fake_jtag_tap.c's TAP is fixed at IR length 4 (FAKE_TAP_IR_LEN)
 * and deliberately not parameterised. The Identify soft TAP's IR is 5 — read off
 * Identify's own device table, .../identify/lib/share/contrib/syn_idcodes.tcl
 * :741-742, `idcode add ... SoftJTAG 5`, IDCODE 0x1063E4CD. A 4+4 chain would
 * not be the chain that ships.
 *
 * So there is a small parameterised model here. A second model is a liability,
 * so it is TIED to the proven one: test_chain_model_agrees_with_the_proven_tap()
 * configures this model as a ONE-device chain with the DAP's parameters and
 * requires it to produce a bit-identical TDO trace to fake_jtag_tap.c for the
 * same IDCODE-read vector. If the new model is wrong, that fails first, and it
 * fails before any chain claim is made.
 *
 * CHAIN ORDER (fpga/rp/nanosoc_iice/rp_nanosoc_iice_shim.sv, default):
 *     shell TDI -> [Identify soft TAP, IR 5] -> [SWJ-DP, IR 4] -> shell TDO
 * so the DAP is the device NEAREST TDO and its 32 IDCODE bits come back FIRST.
 * That is why host/openocd/nanosoc_iice_chain.cfg declares the DAP first, and
 * test_a_legacy_single_tap_read_still_sees_the_dap() is the concrete payoff.
 */
#define _GNU_SOURCE

#include <assert.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#include "../xvc_server/xvc_server.h"
#include "../coordinator/coordinator.h"
#include "../common/platform_regs.h"
#include "../common/net_proto.h"
#include "mock_regs.h"
#include "posix_net_if.h"
#include "fake_jtag_tap.h"

#ifndef XVC_CHAIN_TEST_NAME
#define XVC_CHAIN_TEST_NAME "test_xvc_jtagbb_chain"
#endif

mps3_shell_state_t g_shell_state;

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

/* The two devices on the wire-set, in the order the host declares them:
 * NEAREST TDO FIRST (openocd src/jtag/core.c:213 + doc/openocd.texi:13861). */
#define DAP_IDCODE   0x6BA00477u   /* cxdapswjdp JTAGDP_DEVICEID_IR4 */
#define DAP_IRLEN    4u
#define IICE_IDCODE  0x1063E4CDu   /* syn_idcodes.tcl:741-742, SoftJTAG */
#define IICE_IRLEN   5u

#define PUMP_MAX 200000

/* ===========================================================================
 * A minimal parameterised 1149.1 TAP, and a chain of them.
 * ===========================================================================
 * Same semantics as fake_jtag_tap.c's TAP (and as tests/jtag_chain's
 * iice_soft_tap_model.sv): the action of a state happens on the rising edge at
 * which the controller IS IN that state, and TDO combinationally presents the
 * shift register's LSB while in a Shift state, 0 otherwise. Only IDCODE and
 * BYPASS are implemented -- enough to prove chain arithmetic, and deliberately
 * not enough to pretend this is an Identify IICE.
 * ------------------------------------------------------------------------- */
typedef enum {
    T_TLR = 0, T_RTI, T_SELDR, T_CAPDR, T_SHDR, T_E1DR, T_PADR, T_E2DR, T_UPDR,
    T_SELIR, T_CAPIR, T_SHIR, T_E1IR, T_PAIR, T_E2IR, T_UPIR
} tstate_t;

typedef struct {
    uint32_t idcode;
    uint32_t irlen;
    tstate_t state;
    uint32_t ir;
    uint32_t ir_shift;
    uint32_t dr;
    uint32_t dr_len;
} ctap_t;

static tstate_t tap_next(tstate_t s, uint32_t tms)
{
    switch (s) {
    case T_TLR:   return tms ? T_TLR   : T_RTI;
    case T_RTI:   return tms ? T_SELDR : T_RTI;
    case T_SELDR: return tms ? T_SELIR : T_CAPDR;
    case T_CAPDR: return tms ? T_E1DR  : T_SHDR;
    case T_SHDR:  return tms ? T_E1DR  : T_SHDR;
    case T_E1DR:  return tms ? T_UPDR  : T_PADR;
    case T_PADR:  return tms ? T_E2DR  : T_PADR;
    case T_E2DR:  return tms ? T_UPDR  : T_SHDR;
    case T_UPDR:  return tms ? T_SELDR : T_RTI;
    case T_SELIR: return tms ? T_TLR   : T_CAPIR;
    case T_CAPIR: return tms ? T_E1IR  : T_SHIR;
    case T_SHIR:  return tms ? T_E1IR  : T_SHIR;
    case T_E1IR:  return tms ? T_UPIR  : T_PAIR;
    case T_PAIR:  return tms ? T_E2IR  : T_PAIR;
    case T_E2IR:  return tms ? T_UPIR  : T_SHIR;
    case T_UPIR:  return tms ? T_SELDR : T_RTI;
    default:      return T_TLR;
    }
}

static uint32_t ctap_ir_bypass(const ctap_t *t)
{
    return (t->irlen >= 32u) ? 0xFFFFFFFFu : ((1u << t->irlen) - 1u);
}

static void ctap_reset(ctap_t *t, uint32_t idcode, uint32_t irlen)
{
    memset(t, 0, sizeof(*t));
    t->idcode = idcode;
    t->irlen  = irlen;
    t->state  = T_TLR;
    t->ir     = 0u;         /* TLR selects IDCODE; opcode 0 in both devices
                             * here (JTAG-DP IDCODE is 0xE, but TLR loads the
                             * IDCODE *register* regardless of opcode value --
                             * modelled by dr_len below, not by the number) */
    t->dr_len = 32u;
    t->dr     = idcode;
}

static uint32_t ctap_tdo(const ctap_t *t)
{
    if (t->state == T_SHDR) { return t->dr & 1u; }
    if (t->state == T_SHIR) { return t->ir_shift & 1u; }
    return 0u;
}

/* IR value that means "this device is in BYPASS" -- all ones, per 1149.1. */
static int ctap_in_bypass(const ctap_t *t) { return t->ir == ctap_ir_bypass(t); }

static void ctap_tick(ctap_t *t, uint32_t tms, uint32_t tdi)
{
    switch (t->state) {
    case T_CAPDR:
        if (ctap_in_bypass(t)) { t->dr_len = 1u;  t->dr = 0u; }
        else                   { t->dr_len = 32u; t->dr = t->idcode; }
        break;
    case T_SHDR: {
        uint32_t top = t->dr_len - 1u;
        t->dr = (t->dr >> 1) & ~(1u << top);
        t->dr |= (tdi & 1u) << top;
        break;
    }
    case T_CAPIR: t->ir_shift = 1u; break;   /* 1149.1: IR capture LSBs = 01 */
    case T_SHIR:
        t->ir_shift = (t->ir_shift >> 1) |
                      ((tdi & 1u) << (t->irlen - 1u));
        break;
    case T_UPIR:  t->ir = t->ir_shift & ((1u << t->irlen) - 1u); break;
    default: break;
    }
    t->state = tap_next(t->state, tms);
    if (t->state == T_TLR) {          /* TLR reloads IDCODE in every device */
        t->ir     = 0u;
        t->dr_len = 32u;
    }
}

/* ---- the chain + the jtag_bb.sv register model ---------------------------- */
#define CHAIN_MAX 2

typedef struct {
    ctap_t   tap[CHAIN_MAX];   /* [0] = NEAREST TDO, matching the host's order */
    uint32_t n;
    uint32_t drive;            /* DRIVE[2:0], write-through, jtag_bb.sv */
    uint32_t edges;
    uint32_t drive_writes;
    uint32_t sample_reads;
    uint32_t stray;            /* accesses outside the two mapped offsets */
} chain_t;

static uint32_t chain_tdo(const chain_t *c)
{
    /* The chain's TDO is the TDO of the device nearest TDO -- tap[0]. */
    return ctap_tdo(&c->tap[0]);
}

static void chain_tick(chain_t *c, uint32_t tms, uint32_t tdi)
{
    /* Sample every device's TDO BEFORE any of them advances: in real hardware
     * all of them clock on the same edge, so the value device i receives on its
     * TDI is what device i+1 was PRESENTING, not what it becomes. Getting this
     * wrong is the classic chain-model bug and it looks like a one-bit skew. */
    uint32_t pre[CHAIN_MAX];
    for (uint32_t i = 0; i < c->n; i++) { pre[i] = ctap_tdo(&c->tap[i]); }

    for (uint32_t i = 0; i < c->n; i++) {
        /* tap[i]'s TDI comes from the device BEHIND it (further from TDO);
         * the last one is fed by the host. */
        uint32_t in = (i + 1u < c->n) ? pre[i + 1u] : (tdi & 1u);
        ctap_tick(&c->tap[i], tms, in);
    }
    c->edges++;
}

/* The behavioural model of fpga/shell/ip/jtag_bb/jtag_bb.sv, in C:
 *   DRIVE  @0x00 rw, keeps only [2:0], write-through to the three pins
 *   SAMPLE @0x04 ro, the live TDO
 * and every other offset in the page unmapped (reads 0, writes inert), which is
 * that file's full-address decode. A TCK edge exists if and only if DRIVE[0]
 * goes 0 -> 1, exactly as in the RTL -- so an idempotent re-write (the
 * MPS3_XVC_SWDBB_TCK_STRETCH knob) cannot manufacture one here either. */
static int chain_hook(void *ctx, int is_write, uint32_t base, uint32_t off,
                      uint32_t *val)
{
    chain_t *c = (chain_t *)ctx;
    (void)base;

    if (is_write) {
        if (off != JTAGBB_DRIVE) { c->stray++; return 1; }
        uint32_t prev = c->drive;
        c->drive = *val & (XVC_JTAGBB_TCK | XVC_JTAGBB_TMS | XVC_JTAGBB_TDI);
        c->drive_writes++;
        if ((c->drive & XVC_JTAGBB_TCK) && !(prev & XVC_JTAGBB_TCK)) {
            chain_tick(c, (c->drive & XVC_JTAGBB_TMS) ? 1u : 0u,
                          (c->drive & XVC_JTAGBB_TDI) ? 1u : 0u);
        }
        return 1;
    }

    switch (off) {
    case JTAGBB_DRIVE:  *val = c->drive; return 1;
    case JTAGBB_SAMPLE: *val = chain_tdo(c) & XVC_JTAGBB_TDO;
                        c->sample_reads++; return 1;
    default:            c->stray++; *val = 0u; return 1;
    }
}

/* ===========================================================================
 * Vector construction -- the CLIENT's job, done here explicitly
 * ===========================================================================
 * Everything chain-specific lives in these few lines, which is the whole point:
 * the firmware below is handed vectors and knows nothing.
 * ------------------------------------------------------------------------- */

/* An IDCODE read of a chain of `ndev` devices: 5x TMS=1 -> Test-Logic-Reset
 * (which selects IDCODE in EVERY device at once -- only possible because TMS is
 * shared), -> Run-Test/Idle -> Select-DR -> Capture-DR -> Shift-DR, then
 * 32*ndev shift bits with TMS=1 on the last.
 *
 * Deliberately the SAME navigation fake_tap_idcode_read_sequence() uses, just
 * with a longer payload -- so the 1-device case is byte-comparable with it. */
#define NAV_BITS   9u          /* bits 0..8; bit 9 is the first Shift-DR read */
#define FIRST_BIT  FAKE_TAP_IDCODE_FIRST_BIT   /* == 9 */

static uint32_t chain_idcode_seq_bits(uint32_t ndev) { return NAV_BITS + 32u * ndev; }

static void build_chain_idcode_vectors(uint32_t ndev, uint8_t *tms_vec,
                                       uint8_t *tdi_vec, uint32_t *nbits_out)
{
    uint32_t nbits = chain_idcode_seq_bits(ndev);
    uint32_t bits[NAV_BITS + 32u * CHAIN_MAX];
    uint32_t zeros[NAV_BITS + 32u * CHAIN_MAX];

    for (uint32_t i = 0; i < nbits; i++) { bits[i] = 0u; zeros[i] = 0u; }
    bits[0] = bits[1] = bits[2] = bits[3] = bits[4] = 1u;  /* -> TLR */
    /* bit 5 = 0 -> Run-Test/Idle */
    bits[6] = 1u;                                          /* -> Select-DR */
    /* bits 7,8 = 0 -> Capture-DR, Shift-DR */
    bits[nbits - 1u] = 1u;                                 /* last -> Exit1-DR */

    fake_xvc_bits_to_vector(bits, nbits, tms_vec);
    fake_xvc_bits_to_vector(zeros, nbits, tdi_vec);
    *nbits_out = nbits;
}

/* ===========================================================================
 * Harness -- real kernel sockets, real firmware superloop
 * ========================================================================= */
static chain_t s_chain;

static void chain_setup(uint32_t ndev)
{
    memset(&s_chain, 0, sizeof(s_chain));
    s_chain.n = ndev;
    /* [0] nearest TDO. With ndev==1 that is the DAP alone, which is what the
     * cross-check against fake_jtag_tap.c needs. */
    ctap_reset(&s_chain.tap[0], DAP_IDCODE, DAP_IRLEN);
    if (ndev > 1u) { ctap_reset(&s_chain.tap[1], IICE_IDCODE, IICE_IRLEN); }
}

static uint16_t fresh(uint32_t ndev)
{
    posix_net_reset();
    mock_regs_reset();
    memset(&g_shell_state, 0, sizeof(g_shell_state));
    chain_setup(ndev);
    mock_regs_set_hook(MPS3_JTAGBB_BASE, chain_hook, &s_chain);

    posix_net_map_port(MPS3_PORT_XVC, 0);   /* kernel-chosen port */
    xvc_server_init();                      /* REAL firmware init: binds + parks */
    /* xvc_server_init() parks DRIVE=0; that write is its own proof below, so
     * clear the counters rather than special-casing every later assertion. */
    s_chain.drive_writes = 0;
    s_chain.sample_reads = 0;
    s_chain.edges        = 0;

    uint16_t p = posix_net_actual_port(MPS3_PORT_XVC);
    assert(p != 0);
    return p;
}

static int cli_connect(uint16_t port)
{
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    assert(fd >= 0);
    struct sockaddr_in sa;
    memset(&sa, 0, sizeof(sa));
    sa.sin_family      = AF_INET;
    sa.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    sa.sin_port        = htons(port);
    if (connect(fd, (struct sockaddr *)&sa, sizeof(sa)) != 0) { close(fd); return -1; }
    return fd;
}

static size_t cli_send_all(int fd, const void *buf, size_t len)
{
    const char *p = (const char *)buf;
    size_t done = 0;
    for (int i = 0; i < PUMP_MAX && done < len; i++) {
        ssize_t n = send(fd, p + done, len - done, MSG_DONTWAIT | MSG_NOSIGNAL);
        if (n > 0) { done += (size_t)n; }
        xvc_server_poll();
    }
    return done;
}

static size_t cli_recv_exact(int fd, void *buf, size_t len)
{
    char *p = (char *)buf;
    size_t done = 0;
    for (int i = 0; i < PUMP_MAX && done < len; i++) {
        xvc_server_poll();
        ssize_t n = recv(fd, p + done, len - done, MSG_DONTWAIT);
        if (n > 0) { done += (size_t)n; }
        else if (n == 0) { break; }
        else if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) { break; }
    }
    return done;
}

/* One complete `shift:` command, sent and its TDO reply collected. */
static size_t do_shift(int fd, uint32_t nbits, const uint8_t *tms,
                       const uint8_t *tdi, uint8_t *tdo_out)
{
    uint32_t nb = (nbits + 7u) / 8u;
    uint8_t cmd[10u + 2u * ((NAV_BITS + 32u * CHAIN_MAX + 7u) / 8u)];
    memcpy(cmd, "shift:", 6);
    fake_xvc_put_le32(cmd + 6, nbits);
    memcpy(cmd + 10, tms, nb);
    memcpy(cmd + 10 + nb, tdi, nb);
    size_t sent = cli_send_all(fd, cmd, 10u + 2u * nb);
    assert(sent == 10u + 2u * nb);
    return cli_recv_exact(fd, tdo_out, nb);
}

/* THE per-binary expectation. */
static void check_recovered(uint32_t got, uint32_t want)
{
#if MPS3_XVC_SWDBB_SAMPLE_LATE
    /* NEGATIVE CONTROL: sampling AFTER the rising edge reads a bit the chain
     * has already shifted, so the whole trace is one position late. */
    CHECK(got != want);
#else
    CHECK(got == want);
#endif
}

/* ===========================================================================
 * Tests
 * ========================================================================= */

/* Without this, everything below would be quietly exercising the Debug Bridge. */
static void test_this_binary_is_the_jtagbb_target(void)
{
    CHECK(MPS3_XVC_TARGET_IS_JTAGBB == 1);
    CHECK(MPS3_XVC_TARGET_IS_SWDBB == 0);
    CHECK(MPS3_XVC_TARGET_IS_BITBANG == 1);
}

/* The engine must reach jtag_bb's registers under jtag_bb's names. The old
 * SWDBB build reaches the same addresses by coincidence (xvc_server.h asserts
 * that coincidence at compile time); this asserts that the JTAGBB build is not
 * relying on it. */
static void test_the_engine_uses_the_jtagbb_names(void)
{
    CHECK(XVC_BB_BASE   == MPS3_JTAGBB_BASE);
    CHECK(XVC_BB_DRIVE  == JTAGBB_DRIVE);
    CHECK(XVC_BB_SAMPLE == JTAGBB_SAMPLE);
    CHECK(XVC_BB_TCK    == JTAGBB_DRIVE_TCK);
    CHECK(XVC_BB_TMS    == JTAGBB_DRIVE_TMS);
    CHECK(XVC_BB_TDI    == JTAGBB_DRIVE_TDI);
    CHECK(XVC_BB_TDO    == JTAGBB_SAMPLE_TDO);
    /* And they really are DRIVE[0]/[1]/[2] + SAMPLE[0], i.e. jtag_bb.sv's
     * register map and not something that merely compiles. */
    CHECK(XVC_BB_TCK == (1u << 0));
    CHECK(XVC_BB_TMS == (1u << 1));
    CHECK(XVC_BB_TDI == (1u << 2));
    CHECK(XVC_BB_TDO == (1u << 0));
}

/* xvc_server_init() must park TCK LOW before anything else, or the first write
 * of the first shift is a rising edge and EVERY TAP on the wire-set eats a
 * phantom clock. On a chain that is worse than on one device: the whole chain
 * desynchronises together and every IDCODE looks like garbage. */
static void test_init_parks_tck_low_before_any_edge(void)
{
    posix_net_reset();
    mock_regs_reset();
    memset(&g_shell_state, 0, sizeof(g_shell_state));
    chain_setup(2u);
    mock_regs_set_hook(MPS3_JTAGBB_BASE, chain_hook, &s_chain);
    posix_net_map_port(MPS3_PORT_XVC, 0);
    xvc_server_init();

    CHECK(s_chain.drive_writes == 1u);
    CHECK(s_chain.drive == 0u);
    CHECK(s_chain.edges == 0u);
    CHECK(s_chain.stray == 0u);
}

/* THE GUARD ON THE SECOND MODEL. Configured as a ONE-device chain with the
 * DAP's parameters, the model here must produce a bit-identical TDO trace to
 * firmware/test/fake_jtag_tap.c -- the model the whole existing XVC suite is
 * proven against -- for the same IDCODE-read vector. If this fails, nothing
 * below means anything, so it runs first. */
static void test_chain_model_agrees_with_the_proven_tap(void)
{
    uint8_t tms[FAKE_TAP_IDCODE_SEQ_BYTES], tdi[FAKE_TAP_IDCODE_SEQ_BYTES];
    fake_tap_idcode_read_sequence(tms, tdi);

    /* (a) the proven model, driven directly */
    fake_tap_t ref;
    fake_tap_reset(&ref, DAP_IDCODE);
    uint8_t ref_tdo[FAKE_TAP_IDCODE_SEQ_BYTES] = { 0 };

    /* (b) this file's model, as a 1-device chain */
    chain_setup(1u);
    uint8_t new_tdo[FAKE_TAP_IDCODE_SEQ_BYTES] = { 0 };

    for (uint32_t i = 0; i < FAKE_TAP_IDCODE_SEQ_BITS; i++) {
        uint32_t m = (tms[i >> 3] >> (i & 7u)) & 1u;
        uint32_t d = (tdi[i >> 3] >> (i & 7u)) & 1u;

        if (fake_tap_tdo(&ref))    { ref_tdo[i >> 3] |= (uint8_t)(1u << (i & 7u)); }
        if (chain_tdo(&s_chain))   { new_tdo[i >> 3] |= (uint8_t)(1u << (i & 7u)); }

        fake_tap_tick(&ref, m, d);
        chain_tick(&s_chain, m, d);
    }

    CHECK(memcmp(ref_tdo, new_tdo, sizeof(ref_tdo)) == 0);
    CHECK(fake_xvc_recover32(new_tdo, FIRST_BIT) == DAP_IDCODE);

    /* And the VECTOR BUILDER above must agree with the proven one too: for a
     * 1-device chain it has to emit the identical navigation bytes, which are
     * themselves pinned to what host/socket_harness/xvc_server.py produces
     * (test_xvc_posix_loopback.c carries them as goldens: TMS = 5F 00 00 00 00
     * 01, TDI = all zero). Two builders that disagree would make every chain
     * result above unfalsifiable. */
    uint8_t mine_tms[16], mine_tdi[16];
    uint32_t nbits = 0;
    build_chain_idcode_vectors(1u, mine_tms, mine_tdi, &nbits);
    CHECK(nbits == FAKE_TAP_IDCODE_SEQ_BITS);
    CHECK(memcmp(mine_tms, tms, FAKE_TAP_IDCODE_SEQ_BYTES) == 0);
    CHECK(memcmp(mine_tdi, tdi, FAKE_TAP_IDCODE_SEQ_BYTES) == 0);
    CHECK(mine_tms[0] == 0x5Fu && mine_tms[5] == 0x01u);
}

/* THE HEADLINE: both devices answer, over a real TCP socket, through the real
 * firmware engine, with the DAP's IDCODE first because it is nearest TDO. */
static void test_chain_idcodes_over_a_real_socket(void)
{
    uint16_t port = fresh(2u);
    int fd = cli_connect(port);
    CHECK(fd >= 0);

    uint8_t tms[16], tdi[16], tdo[16];
    uint32_t nbits;
    build_chain_idcode_vectors(2u, tms, tdi, &nbits);
    CHECK(nbits == 73u);                       /* 9 navigation + 2 x 32 */

    size_t got = do_shift(fd, nbits, tms, tdi, tdo);
    CHECK(got == (nbits + 7u) / 8u);

    uint32_t dap  = fake_xvc_recover32(tdo, FIRST_BIT);
    uint32_t iice = fake_xvc_recover32(tdo, FIRST_BIT + 32u);
    printf("  chain over a socket: [0]=0x%08X [1]=0x%08X\n", dap, iice);
    check_recovered(dap,  DAP_IDCODE);
    check_recovered(iice, IICE_IDCODE);

    close(fd);
}

/* The engine is chain-AGNOSTIC: the register traffic is a function of num_bits
 * alone -- 3 accesses per bit plus one park -- and is identical whether the
 * wire-set carries one device or two. If a future edit taught this engine about
 * TAPs, this count would move. */
static void test_the_engine_does_not_know_about_taps(void)
{
    uint8_t tms[16], tdi[16], tdo[16];
    uint32_t nbits;

    uint16_t port = fresh(2u);
    int fd = cli_connect(port);
    CHECK(fd >= 0);
    build_chain_idcode_vectors(2u, tms, tdi, &nbits);
    (void)do_shift(fd, nbits, tms, tdi, tdo);

    /* 2 DRIVE writes + 1 SAMPLE read per bit, then one park write. */
    CHECK(s_chain.drive_writes == 2u * nbits + 1u);
    CHECK(s_chain.sample_reads == nbits);
    CHECK(s_chain.edges        == nbits);
    /* and NOTHING outside jtag_bb.sv's two mapped offsets. */
    CHECK(s_chain.stray == 0u);
    /* Parked low, so the next shift's first write is not an edge. */
    CHECK((s_chain.drive & XVC_JTAGBB_TCK) == 0u);
    close(fd);
}

/* THE PAYOFF OF THE CHAIN ORDER, made concrete.
 *
 * host/openocd/nanosoc_mps3_jtag.cfg declares ONE tap. Pointed at the chained
 * IICE RM it issues the plain 41-bit single-TAP IDCODE read. Because the shim
 * puts the SWJ-DP NEAREST TDO, the first 32 shifted-out bits are still the
 * DAP's -- so OpenOCD MATCHES 0x6BA00477 and then complains about a device
 * after the end of the chain, which names the real problem. Reverse the fabric
 * order and the same operator would instead be told "nanosoc.cpu UNEXPECTED:
 * 0x1063E4CD" and would go debugging a DAP that is fine.
 *
 * This test is the evidence for that design argument, not a restatement of it.
 */
static void test_a_legacy_single_tap_read_still_sees_the_dap(void)
{
    uint16_t port = fresh(2u);              /* TWO devices on the wire */
    int fd = cli_connect(port);
    CHECK(fd >= 0);

    /* The LEGACY vector: 41 bits, one device assumed. */
    uint8_t tms[FAKE_TAP_IDCODE_SEQ_BYTES], tdi[FAKE_TAP_IDCODE_SEQ_BYTES];
    uint8_t tdo[FAKE_TAP_IDCODE_SEQ_BYTES];
    fake_tap_idcode_read_sequence(tms, tdi);
    size_t got = do_shift(fd, FAKE_TAP_IDCODE_SEQ_BITS, tms, tdi, tdo);
    CHECK(got == FAKE_TAP_IDCODE_SEQ_BYTES);

    check_recovered(fake_xvc_recover32(tdo, FIRST_BIT), DAP_IDCODE);
    close(fd);
}

/* CONTROL: decode the chain scan with the declaration order SWAPPED. The wire
 * traffic is byte-identical; only the host's interpretation changes. If this
 * could not fail, test_chain_idcodes_over_a_real_socket would not be reading
 * position-dependent data. */
static void test_control_swapped_declaration_order_is_wrong(void)
{
    uint16_t port = fresh(2u);
    int fd = cli_connect(port);
    CHECK(fd >= 0);

    uint8_t tms[16], tdi[16], tdo[16];
    uint32_t nbits;
    build_chain_idcode_vectors(2u, tms, tdi, &nbits);
    (void)do_shift(fd, nbits, tms, tdi, tdo);

    uint32_t slot0 = fake_xvc_recover32(tdo, FIRST_BIT);
    uint32_t slot1 = fake_xvc_recover32(tdo, FIRST_BIT + 32u);
    /* Declared the other way round, slot 0 would be read as the Identify TAP. */
    CHECK(slot0 != IICE_IDCODE);
    CHECK(slot1 != DAP_IDCODE);
    close(fd);
}

int main(void)
{
    printf("%s: real firmware XVC engine, JTAGBB target, 2-TAP chain\n",
           XVC_CHAIN_TEST_NAME);

    test_this_binary_is_the_jtagbb_target();
    test_the_engine_uses_the_jtagbb_names();
    test_init_parks_tck_low_before_any_edge();
    test_chain_model_agrees_with_the_proven_tap();
    test_chain_idcodes_over_a_real_socket();
    test_the_engine_does_not_know_about_taps();
    test_a_legacy_single_tap_read_still_sees_the_dap();
    test_control_swapped_declaration_order_is_wrong();

    printf("%s: PASS (%d checks)\n", XVC_CHAIN_TEST_NAME, s_checks);
    return 0;
}
