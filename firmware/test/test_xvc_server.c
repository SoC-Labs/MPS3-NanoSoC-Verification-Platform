/*
 * test_xvc_server.c — host-gcc tests for xvc_server.c's REAL XVC v1.0
 * protocol engine + DBGBR shift chunking (W-VITIS/A3). XVC bytes go in
 * through a fake_net_if client exactly as Vivado hw_server would send
 * them; Debug Bridge effects are observed through a behavioral mock_regs
 * hook standing in for the AXI-to-BSCAN register block (platform_regs.h
 * DBGBR_* — the PG245/XVC-reference LENGTH/TMS/TDI/TDO/CTRL layout,
 * flagged ASSUMED-PENDING-BRING-UP there; this test pins the firmware
 * side of that assumption down so bring-up only has to confirm the RTL
 * side).
 *
 * The fake bridge computes TDO = TMS ^ TDI masked to LENGTH bits — an
 * arbitrary-but-bit-exact function of BOTH input vectors, so a passing
 * round-trip proves every TMS/TDI bit reached the bridge in the right
 * position and every TDO bit came back in the right position.
 *
 * Links: xvc_server.c, common/net_if.c, mock_regs.c, fake_net_if.c.
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

mps3_shell_state_t g_shell_state;

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

/* ---- behavioral DBGBR fake -------------------------------------------------- */

#define FAKE_DBGBR_MAX_KICKS 128
#define FAKE_DBGBR_TRACE_MAX 512

/* One recorded register access, so a test can assert the TRANSACTION ORDER,
 * not just the final values. Order is the part a silent refactor breaks. */
typedef struct {
    int      is_write;
    uint32_t off;
} dbgbr_access_t;

typedef struct {
    uint32_t length;      /* last LENGTH write                   */
    uint32_t tms;         /* last TMS write                      */
    uint32_t tdi;         /* last TDI write                      */
    uint32_t tdo;         /* computed on CTRL=GO                 */
    int      kicks;       /* CTRL=GO count                       */
    uint32_t kick_lengths[FAKE_DBGBR_MAX_KICKS]; /* LENGTH at each kick */
    int      dead;        /* 1: CTRL never self-clears           */
    dbgbr_access_t trace[FAKE_DBGBR_TRACE_MAX];  /* ordered access log  */
    int      n_trace;
} fake_dbgbr_t;

static void dbgbr_trace(fake_dbgbr_t *d, int is_write, uint32_t off)
{
    if (d->n_trace < FAKE_DBGBR_TRACE_MAX) {
        d->trace[d->n_trace].is_write = is_write;
        d->trace[d->n_trace].off = off;
        d->n_trace++;
    }
}

static fake_dbgbr_t s_dbgbr;

static uint32_t bits_mask(uint32_t nbits)
{
    return (nbits >= 32u) ? 0xFFFFFFFFu : ((1u << nbits) - 1u);
}

static int dbgbr_hook(void *ctx, int is_write, uint32_t base, uint32_t off, uint32_t *val)
{
    fake_dbgbr_t *d = (fake_dbgbr_t *)ctx;
    (void)base;
    dbgbr_trace(d, is_write, off);
    if (is_write) {
        switch (off) {
        case DBGBR_LENGTH: d->length = *val; return 1;
        case DBGBR_TMS:    d->tms = *val;    return 1;
        case DBGBR_TDI:    d->tdi = *val;    return 1;
        case DBGBR_CTRL:
            if (*val & DBGBR_CTRL_GO) {
                d->tdo = (d->tms ^ d->tdi) & bits_mask(d->length);
                if (d->kicks < FAKE_DBGBR_MAX_KICKS) {
                    d->kick_lengths[d->kicks] = d->length;
                }
                d->kicks++;
            }
            return 1;
        default: return 0;
        }
    }
    switch (off) {
    case DBGBR_CTRL: *val = d->dead ? DBGBR_CTRL_GO : 0; return 1;
    case DBGBR_TDO:  *val = d->tdo;                      return 1;
    default:         return 0;
    }
}

/* ---- helpers ----------------------------------------------------------------- */

static void fresh(void)
{
    mock_regs_reset();
    fake_net_reset();
    memset(&g_shell_state, 0, sizeof(g_shell_state));
    memset(&s_dbgbr, 0, sizeof(s_dbgbr));
    mock_regs_set_hook(MPS3_DBGBR_BASE, dbgbr_hook, &s_dbgbr);
    /* THE FAKE CLOCK MUST MOVE, or the dead-bridge cases cannot finish.
     *
     * The per-chunk GO wait is mps3_spin_until(MPS3_XVC_DBGBR_SETTLE_TIMEOUT_US)
     * now, not an iteration count. An iteration count expires by itself; a
     * DEADLINE expires only when time passes, and on the host time passes only
     * when a register is read. At the default 0 us/read, `s_dbgbr.dead = 1`
     * stopped being a test that FAILS and became a test that HANGS. One
     * microsecond per read here makes every case in this file safe against that,
     * not just the two that pin GO today. */
    mock_regs_set_us_per_read(1u);
    xvc_server_init();
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

/* Reference model of the fake bridge across chunking: per whole vector,
 * TDO byte i = (TMS ^ TDI) byte i (the mask only trims bits past num_bits
 * in the final partial byte/chunk, which the caller checks separately). */
static void expect_tdo(uint32_t num_bits, const uint8_t *tms, const uint8_t *tdi, uint8_t *out)
{
    uint32_t nb = (num_bits + 7u) / 8u;
    for (uint32_t i = 0; i < nb; i++) {
        out[i] = (uint8_t)(tms[i] ^ tdi[i]);
    }
    uint32_t tail_bits = num_bits % 8u;
    if (tail_bits) {
        out[nb - 1] &= (uint8_t)((1u << tail_bits) - 1u);
    }
}

/* ---- tests --------------------------------------------------------------------- */

/* ---------------------------------------------------------------------------
 * Debug Bridge (DBGBR) CONFORMANCE — closes the ASSUMED-PENDING-BRING-UP flag
 * on the DBGBR register map and shift sequence.
 *
 * These offsets were "the PG245/XVC-reference pattern, not a vendor header",
 * flagged to "confirm against a live getinfo/shift round-trip at board bring-up
 * before trusting TDO data". They did NOT need a board: the layout belongs to
 * Xilinx's own AXI-to-BSCAN reference, XAPP1251
 * (Xilinx/XilinxVirtualCable, jtag/zynq7000/XAPP1251/src/xvcServer.c):
 *
 *     typedef struct {
 *       uint32_t  length_offset;   // 0x00
 *       uint32_t  tms_offset;      // 0x04
 *       uint32_t  tdi_offset;      // 0x08
 *       uint32_t  tdo_offset;      // 0x0C
 *       uint32_t  ctrl_offset;     // 0x10
 *     } jtag_t;
 *
 *     ptr->length_offset = 32;  ptr->tms_offset = tms;  ptr->tdi_offset = tdi;
 *     ptr->ctrl_offset = 0x01;
 *     while (ptr->ctrl_offset) { }        // GO self-clears on completion
 *     tdo = ptr->tdo_offset;              // read ONLY after it clears
 *
 * This test pins BOTH the offsets and the ORDER: LENGTH, TMS, TDI, CTRL=GO,
 * then >=1 CTRL read, then TDO. A refactor that hoists the TDO read above the
 * poll, or kicks CTRL before TDI is written, latches garbage on silicon but
 * would still pass a values-only test. (Our poll is bounded + fail-closed,
 * unlike XAPP1251's unbounded `while` — that is a deliberate improvement.)
 * ------------------------------------------------------------------------- */
static void test_dbgbr_xapp1251_conformance(void)
{
    /* (1) Offsets == the XAPP1251 struct field order. */
    CHECK(DBGBR_LENGTH == 0x00u);
    CHECK(DBGBR_TMS    == 0x04u);
    CHECK(DBGBR_TDI    == 0x08u);
    CHECK(DBGBR_TDO    == 0x0Cu);
    CHECK(DBGBR_CTRL   == 0x10u);
    CHECK(DBGBR_CTRL_GO == 0x1u);   /* ctrl_offset = 0x01 */

    /* (2) One 32-bit shift -> exactly one XAPP1251-shaped transaction. */
    fresh();
    int cli = fake_net_connect(MPS3_PORT_XVC);
    uint8_t tms[4] = {0xAA, 0xBB, 0xCC, 0xDD};
    uint8_t tdi[4] = {0x01, 0x02, 0x03, 0x04};
    CHECK(send_shift(cli, 32, tms, tdi) > 0);
    polls(4);

    CHECK(s_dbgbr.kicks == 1);
    CHECK(s_dbgbr.kick_lengths[0] == 32u);

    /* Walk the trace: find the single CTRL=GO write and check what surrounds it. */
    int i_len = -1, i_tms = -1, i_tdi = -1, i_go = -1, i_poll = -1, i_tdo = -1;
    for (int i = 0; i < s_dbgbr.n_trace; i++) {
        int w = s_dbgbr.trace[i].is_write;
        uint32_t o = s_dbgbr.trace[i].off;
        if (w && o == DBGBR_LENGTH && i_len < 0) i_len = i;
        if (w && o == DBGBR_TMS    && i_tms < 0) i_tms = i;
        if (w && o == DBGBR_TDI    && i_tdi < 0) i_tdi = i;
        if (w && o == DBGBR_CTRL   && i_go  < 0) i_go  = i;
        if (!w && o == DBGBR_CTRL  && i_poll < 0 && i_go >= 0) i_poll = i;
        if (!w && o == DBGBR_TDO   && i_tdo < 0) i_tdo = i;
    }
    CHECK(i_len >= 0 && i_tms >= 0 && i_tdi >= 0 && i_go >= 0);
    CHECK(i_poll >= 0 && i_tdo >= 0);

    /* THE ORDER (XAPP1251): length -> tms -> tdi -> ctrl=GO -> poll ctrl -> tdo */
    CHECK(i_len < i_tms);
    CHECK(i_tms < i_tdi);
    CHECK(i_tdi < i_go);      /* never kick before the vectors are staged */
    CHECK(i_go  < i_poll);    /* poll only after the kick                 */
    CHECK(i_poll < i_tdo);    /* TDO read ONLY after GO self-cleared      */

    /* No TDO read may precede the GO write at all. */
    for (int i = 0; i < i_go; i++) {
        CHECK(!(s_dbgbr.trace[i].is_write == 0 &&
                s_dbgbr.trace[i].off == DBGBR_TDO));
    }

    /* (3) A bridge whose GO never clears must fail closed, never read TDO. */
    fresh();
    s_dbgbr.dead = 1;
    cli = fake_net_connect(MPS3_PORT_XVC);
    CHECK(send_shift(cli, 32, tms, tdi) > 0);
    polls(4);
    CHECK(s_dbgbr.kicks == 1);          /* it did kick ... */
    int tdo_reads = 0;
    for (int i = 0; i < s_dbgbr.n_trace; i++) {
        if (!s_dbgbr.trace[i].is_write && s_dbgbr.trace[i].off == DBGBR_TDO) {
            tdo_reads++;
        }
    }
    CHECK(tdo_reads == 0);              /* ... but never trusted TDO */
}

static void test_getinfo(void)
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
    CHECK(s_dbgbr.kicks == 0); /* getinfo never touches hardware */
}

static void test_settck_echoes(void)
{
    fresh();
    int cli = fake_net_connect(MPS3_PORT_XVC);

    uint8_t cmd[11];
    memcpy(cmd, "settck:", 7);
    put_le32(cmd + 7, 10000u); /* 10 us period */
    CHECK(fake_net_send(cli, cmd, 11) == 11);
    polls(2);

    uint8_t rsp[8];
    CHECK(fake_net_recv(cli, rsp, sizeof(rsp)) == 4);
    CHECK(memcmp(rsp, cmd + 7, 4) == 0); /* byte-exact LE echo */
    CHECK(s_dbgbr.kicks == 0);           /* settck never touches hardware */
}

static void test_shift_single_chunk_roundtrip(void)
{
    fresh();
    int cli = fake_net_connect(MPS3_PORT_XVC);

    /* 8 bits: one chunk, LENGTH must be 8, TDO = TMS ^ TDI. */
    uint8_t tms[1] = { 0xA5 };
    uint8_t tdi[1] = { 0x3C };
    CHECK(send_shift(cli, 8, tms, tdi) == 12);
    polls(2);

    uint8_t rsp[4];
    CHECK(fake_net_recv(cli, rsp, sizeof(rsp)) == 1);
    CHECK(rsp[0] == (0xA5 ^ 0x3C));
    CHECK(s_dbgbr.kicks == 1);
    CHECK(s_dbgbr.kick_lengths[0] == 8);
}

static void test_shift_multi_chunk_roundtrip(void)
{
    fresh();
    int cli = fake_net_connect(MPS3_PORT_XVC);

    /* 77 bits = chunks of 32+32+13 across 10 vector bytes, exercising the
     * partial final chunk + partial final byte (77 % 8 = 5 bits). */
    uint8_t tms[10], tdi[10], want[10];
    for (int i = 0; i < 10; i++) {
        tms[i] = (uint8_t)(0x11 * i + 7);
        tdi[i] = (uint8_t)(0xC3 - 5 * i);
    }
    expect_tdo(77, tms, tdi, want);

    CHECK(send_shift(cli, 77, tms, tdi) == 30);
    polls(3);

    uint8_t rsp[16];
    CHECK(fake_net_recv(cli, rsp, sizeof(rsp)) == 10);
    CHECK(memcmp(rsp, want, 10) == 0);
    CHECK(s_dbgbr.kicks == 3);
    CHECK(s_dbgbr.kick_lengths[0] == 32);
    CHECK(s_dbgbr.kick_lengths[1] == 32);
    CHECK(s_dbgbr.kick_lengths[2] == 13);
}

static void test_shift_max_vector(void)
{
    fresh();
    int cli = fake_net_connect(MPS3_PORT_XVC);

    uint8_t tms[MPS3_XVC_MAX_VECTOR_BYTES], tdi[MPS3_XVC_MAX_VECTOR_BYTES],
            want[MPS3_XVC_MAX_VECTOR_BYTES];
    for (uint32_t i = 0; i < MPS3_XVC_MAX_VECTOR_BYTES; i++) {
        tms[i] = (uint8_t)(i * 13 + 1);
        tdi[i] = (uint8_t)(255 - i * 7);
    }
    expect_tdo(MPS3_XVC_MAX_VECTOR_BITS, tms, tdi, want);

    CHECK(send_shift(cli, MPS3_XVC_MAX_VECTOR_BITS, tms, tdi)
          == (int)(10 + 2 * MPS3_XVC_MAX_VECTOR_BYTES));
    polls(4);

    uint8_t rsp[MPS3_XVC_MAX_VECTOR_BYTES];
    /* Drain the reply across polls (fake backend may return it in one). */
    int got = 0;
    for (int i = 0; i < 8 && got < (int)sizeof(rsp); i++) {
        int n = fake_net_recv(cli, rsp + got, (int)sizeof(rsp) - got);
        CHECK(n >= 0);
        got += n;
        polls(1);
    }
    CHECK(got == (int)MPS3_XVC_MAX_VECTOR_BYTES);
    CHECK(memcmp(rsp, want, sizeof(want)) == 0);
    CHECK(s_dbgbr.kicks == (int)(MPS3_XVC_MAX_VECTOR_BITS / 32u));
}

static void test_fragmented_command_reassembly(void)
{
    fresh();
    int cli = fake_net_connect(MPS3_PORT_XVC);

    /* Deliver a 16-bit shift in hostile fragments: prefix split mid-word,
     * length split, vectors split. */
    uint8_t full[14];
    memcpy(full, "shift:", 6);
    put_le32(full + 6, 16);
    full[10] = 0x0F; full[11] = 0xF0; /* tms */
    full[12] = 0x55; full[13] = 0xAA; /* tdi */

    CHECK(fake_net_send(cli, full, 3) == 3);       /* "shi" */
    polls(2);
    CHECK(s_dbgbr.kicks == 0);
    CHECK(fake_net_send(cli, full + 3, 5) == 5);   /* "ft:" + 2 len bytes */
    polls(2);
    CHECK(s_dbgbr.kicks == 0);
    CHECK(fake_net_send(cli, full + 8, 5) == 5);   /* rest of len + tms + tdi[0] */
    polls(2);
    CHECK(s_dbgbr.kicks == 0);                     /* still one tdi byte short */
    CHECK(fake_net_send(cli, full + 13, 1) == 1);
    polls(2);

    uint8_t rsp[4];
    CHECK(fake_net_recv(cli, rsp, sizeof(rsp)) == 2);
    CHECK(rsp[0] == (0x0F ^ 0x55));
    CHECK(rsp[1] == (0xF0 ^ 0xAA));
    CHECK(s_dbgbr.kicks == 1);
    CHECK(s_dbgbr.kick_lengths[0] == 16);
}

static void test_back_to_back_commands_one_segment(void)
{
    fresh();
    int cli = fake_net_connect(MPS3_PORT_XVC);

    /* getinfo + 8-bit shift concatenated in one TCP segment: both are
     * serviced, in order. */
    uint8_t buf[32];
    memcpy(buf, "getinfo:", 8);
    memcpy(buf + 8, "shift:", 6);
    put_le32(buf + 14, 8);
    buf[18] = 0x01; /* tms */
    buf[19] = 0xFE; /* tdi */
    CHECK(fake_net_send(cli, buf, 20) == 20);
    polls(3);

    char rsp[64];
    int n = fake_net_recv(cli, rsp, sizeof(rsp));
    char expect[64];
    int e = snprintf(expect, sizeof(expect), "xvcServer_v1.0:%u\n",
                     (unsigned)MPS3_XVC_MAX_VECTOR_BITS);
    CHECK(n == e + 1); /* info string + 1 TDO byte, same drain */
    CHECK(memcmp(rsp, expect, (size_t)e) == 0);
    CHECK((uint8_t)rsp[e] == (0x01 ^ 0xFE));
}

static void test_gated_shift_stalls_then_services(void)
{
    fresh();
    int cli = fake_net_connect(MPS3_PORT_XVC);
    polls(1); /* accept */

    g_shell_state.xvc_gated = true;

    uint8_t tms[1] = { 0xFF };
    uint8_t tdi[1] = { 0x0F };
    CHECK(send_shift(cli, 8, tms, tdi) == 12);
    polls(5);
    CHECK(s_dbgbr.kicks == 0); /* no DBGBR access while gated */
    uint8_t rsp[4];
    CHECK(fake_net_recv(cli, rsp, sizeof(rsp)) == 0); /* no reply either */
    CHECK(!fake_net_fw_closed(cli)); /* stalled, NOT dropped */

    g_shell_state.xvc_gated = false;
    polls(2);
    CHECK(s_dbgbr.kicks == 1);
    CHECK(fake_net_recv(cli, rsp, sizeof(rsp)) == 1);
    CHECK(rsp[0] == (0xFF ^ 0x0F));
}

static void test_gated_getinfo_still_answered(void)
{
    fresh();
    int cli = fake_net_connect(MPS3_PORT_XVC);
    polls(1);

    g_shell_state.xvc_gated = true;
    CHECK(fake_net_send(cli, "getinfo:", 8) == 8);
    polls(2);

    char rsp[64];
    CHECK(fake_net_recv(cli, rsp, sizeof(rsp)) > 0); /* answered while gated */
    CHECK(s_dbgbr.kicks == 0);
}

static void test_junk_and_oversize_fail_closed(void)
{
    fresh();
    int cli = fake_net_connect(MPS3_PORT_XVC);
    CHECK(fake_net_send(cli, "quit:", 5) == 5); /* not an XVC verb */
    polls(2);
    CHECK(fake_net_fw_closed(cli));
    fake_net_close(cli);

    /* shift: with num_bits over the ACCEPT CEILING — fail closed before any
     * vector byte is even awaited.
     *
     * NOTE the ceiling, not the advertised size. Bounding this by
     * MPS3_XVC_MAX_VECTOR_BITS is the defect that killed Identify on its first
     * real scan: a client sizes its payload against the advertisement and then
     * adds TAP-navigation bits, so a legal num_bits exceeds it (xvc_server.h
     * has the measurements; test_xvc_identify_stream.c replays the real
     * traffic). Over-length is still a protocol violation — just at a bigger
     * number. */
    int cli2 = fake_net_connect(MPS3_PORT_XVC);
    polls(1);
    uint8_t cmd[10];
    memcpy(cmd, "shift:", 6);
    put_le32(cmd + 6, MPS3_XVC_ACCEPT_VECTOR_BITS + 1u);
    CHECK(fake_net_send(cli2, cmd, 10) == 10);
    polls(2);
    CHECK(fake_net_fw_closed(cli2));
    CHECK(s_dbgbr.kicks == 0);
    fake_net_close(cli2);

    /* num_bits == 0 — equally malformed. */
    int cli3 = fake_net_connect(MPS3_PORT_XVC);
    polls(1);
    put_le32(cmd + 6, 0);
    CHECK(fake_net_send(cli3, cmd, 10) == 10);
    polls(2);
    CHECK(fake_net_fw_closed(cli3));
    CHECK(s_dbgbr.kicks == 0);
}

static void test_dead_bridge_drops_client(void)
{
    fresh();
    s_dbgbr.dead = 1; /* CTRL.GO never self-clears */
    int cli = fake_net_connect(MPS3_PORT_XVC);

    uint8_t tms[1] = { 0x01 };
    uint8_t tdi[1] = { 0x02 };
    CHECK(send_shift(cli, 8, tms, tdi) == 12);
    polls(2);
    CHECK(fake_net_fw_closed(cli)); /* bounded poll expired -> fail closed */
}

/* The dead-bridge wait is a DURATION now, so assert the duration -- the `-1`
 * alone cannot tell a bound that is 200 us from one that is 200 ms, and the
 * iteration count it replaced could not be expressed in either unit. This is
 * the control for that conversion: it cannot be written against the old code
 * at all, because the old loop never read a clock. */
static void test_dead_bridge_gives_up_on_time(void)
{
    fresh();
    s_dbgbr.dead = 1;
    int cli = fake_net_connect(MPS3_PORT_XVC);

    uint8_t tms[1] = { 0x01 };
    uint8_t tdi[1] = { 0x02 };
    CHECK(send_shift(cli, 8, tms, tdi) == 12);

    uint32_t t0 = mock_time_now_us();
    polls(2);
    uint32_t elapsed = mock_time_now_us() - t0;

    CHECK(fake_net_fw_closed(cli));
    /* Floor: it really did wait out the budget. Ceiling: ONE budget, not one
     * per chunk -- shift_dbgbr() returns on the FIRST chunk that fails, which
     * is what keeps a dead bridge at 200 us instead of 65 x 200 us. (An 8-bit
     * shift is one chunk anyway; test_dbgbr_xapp1251_conformance's dead case
     * covers 32 bits. The ceiling is deliberately generous -- polls(2) also
     * runs the accept/recv path, which reads registers of its own.) */
    CHECK(elapsed >= MPS3_XVC_DBGBR_SETTLE_TIMEOUT_US);
    CHECK(elapsed < 4u * MPS3_XVC_DBGBR_SETTLE_TIMEOUT_US);

    /* The shipped value, pinned: xvc_server.c derives 200 us from a <=32-TCK
     * chunk and shows 65 chunks fit the xvc service's 50 ms budget. Change the
     * number and that derivation has to be changed with it. */
    CHECK(MPS3_XVC_DBGBR_SETTLE_TIMEOUT_US == 200u);
}

static void test_second_client_refused_and_reconnect(void)
{
    fresh();
    int first = fake_net_connect(MPS3_PORT_XVC);
    polls(1);
    int second = fake_net_connect(MPS3_PORT_XVC);
    polls(1);
    CHECK(fake_net_fw_closed(second));
    CHECK(!fake_net_fw_closed(first));

    /* First client hangs up; a new client is then adopted and served. */
    fake_net_close(first);
    polls(2);
    int third = fake_net_connect(MPS3_PORT_XVC);
    polls(2);
    CHECK(fake_net_send(third, "getinfo:", 8) == 8);
    polls(2);
    char rsp[64];
    CHECK(fake_net_recv(third, rsp, sizeof(rsp)) > 0);
}

/* REAP BEFORE REFUSE (2026-09-28, net_if.h mps3_net_peer_closed): hw_server /
 * Harness Manager close and reopen 2542 back to back, and accept runs before the
 * current client's EOF is read -- so the reopen used to be refused. Close,
 * reconnect with NO poll between, getinfo: answered every time. Also with a
 * shift left stalled by the gate: the dead client's leftovers go with it and
 * the newcomer starts from a clean command buffer. A second LIVE client is
 * still refused (test_second_client_refused_and_reconnect). */
static void test_close_then_reconnect_is_adopted(void)
{
    fresh();
    char rsp[64];
    int cli = fake_net_connect(MPS3_PORT_XVC);
    for (int i = 0; i < 50; i++) {
        CHECK(fake_net_send(cli, "getinfo:", 8) == 8);
        polls(2);
        CHECK(!fake_net_fw_closed(cli));
        int n = fake_net_recv(cli, rsp, sizeof(rsp) - 1);
        CHECK(n > 0);
        rsp[n] = '\0';
        CHECK(strncmp(rsp, "xvcServer_v1.0:", 15) == 0);
        fake_net_close(cli);
        cli = fake_net_connect(MPS3_PORT_XVC);   /* immediately: no poll between */
        CHECK(cli >= 0);
    }
    fake_net_close(cli);
    polls(2);

    int old = fake_net_connect(MPS3_PORT_XVC);
    polls(1);
    g_shell_state.xvc_gated = true;
    uint8_t tms[1] = { 0xFF };
    uint8_t tdi[1] = { 0x0F };
    CHECK(send_shift(old, 8, tms, tdi) == 12);
    polls(2);                                     /* consumed, stalled by the gate */
    fake_net_close(old);
    int next = fake_net_connect(MPS3_PORT_XVC);
    polls(1);
    CHECK(!fake_net_fw_closed(next));             /* adopted, not refused */
    g_shell_state.xvc_gated = false;
    CHECK(fake_net_send(next, "getinfo:", 8) == 8);
    polls(2);
    int n = fake_net_recv(next, rsp, sizeof(rsp) - 1);
    CHECK(n > 0);
    rsp[n] = '\0';
    CHECK(strncmp(rsp, "xvcServer_v1.0:", 15) == 0);  /* not a stale TDO byte */
    fake_net_close(next);
    polls(2);
}

static void test_do_shift_arg_validation(void)
{
    fresh();
    uint8_t v[MPS3_XVC_ACCEPT_VECTOR_BYTES] = { 0 };
    uint8_t out[MPS3_XVC_ACCEPT_VECTOR_BYTES];
    CHECK(xvc_server_do_shift(0, v, v, out) == -1);
    /* Rejected at the ACCEPT ceiling. Note the buffers above are ceiling-sized:
     * validating against the advertised size while passing 256-byte vectors
     * would have overrun them the moment the bound was correctly relaxed. */
    CHECK(xvc_server_do_shift(MPS3_XVC_ACCEPT_VECTOR_BITS + 1u, v, v, out) == -1);
    CHECK(s_dbgbr.kicks == 0);
    /* A shift LARGER than we advertise but inside the ceiling must succeed --
     * this is the exact case Identify needs (2048 + 5 navigation bits). */
    CHECK(xvc_server_do_shift(MPS3_XVC_MAX_VECTOR_BITS + 5u, v, v, out) == 0);
    s_dbgbr.kicks = 0;
    memset(&s_dbgbr.kick_lengths, 0, sizeof(s_dbgbr.kick_lengths));
    CHECK(xvc_server_do_shift(1, v, v, out) == 0);
    CHECK(s_dbgbr.kicks == 1);
    CHECK(s_dbgbr.kick_lengths[0] == 1);
}

/* THE CLAIM LOCK (2026-09-26, HM_ANSWERS C3; the strong seam is harnessd's): a
 * refused peer gets ONE line with code "locked" and the close -- even when it
 * spoke first -- and touches nothing; with the seam at 0 (the weak default, every
 * other test here) the same connect is adopted and served. */
static int s_refuse;
int mps3_xvc_refuse_peer(struct mps3_net_conn *conn)
{
    (void)conn;
    return s_refuse;
}

static void test_claim_lock_refuses_with_one_line(void)
{
    char rsp[160];
    fresh();
    s_refuse = 1;
    int cli = fake_net_connect(MPS3_PORT_XVC);
    CHECK(fake_net_send(cli, "getinfo:", 8) == 8);         /* it spoke before the accept */
    polls(2);
    CHECK(fake_net_fw_closed(cli));
    int n = fake_net_recv(cli, rsp, sizeof(rsp) - 1);
    CHECK(n == (int)strlen(MPS3_XVC_LOCKED_LINE));
    rsp[n > 0 ? n : 0] = '\0';
    CHECK(strcmp(rsp, MPS3_XVC_LOCKED_LINE) == 0);
    CHECK(s_dbgbr.kicks == 0);                            /* ...and it shifted nothing */
    s_refuse = 0;                                         /* the control: adopted */
    int ok = fake_net_connect(MPS3_PORT_XVC);
    polls(2);
    CHECK(!fake_net_fw_closed(ok));
}

int main(void)
{
    test_dbgbr_xapp1251_conformance();
    test_getinfo();
    test_settck_echoes();
    test_shift_single_chunk_roundtrip();
    test_shift_multi_chunk_roundtrip();
    test_shift_max_vector();
    test_fragmented_command_reassembly();
    test_back_to_back_commands_one_segment();
    test_gated_shift_stalls_then_services();
    test_gated_getinfo_still_answered();
    test_junk_and_oversize_fail_closed();
    test_dead_bridge_drops_client();
    test_dead_bridge_gives_up_on_time();
    test_second_client_refused_and_reconnect();
    test_close_then_reconnect_is_adopted();
    test_do_shift_arg_validation();

    test_claim_lock_refuses_with_one_line();

    printf("test_xvc_server: %d checks passed\n", s_checks);
    return 0;
}
