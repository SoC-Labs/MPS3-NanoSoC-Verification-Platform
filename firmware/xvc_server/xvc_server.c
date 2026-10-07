/*
 * xvc_server.c — XVC v1.0 protocol server (TCP 2542). ONE protocol engine,
 * compile-time-selectable shift targets (see xvc_server.h's table): the
 * shell's Debug Bridge (DEFAULT), or the JTAGBB pin-wiggler bit-banged as JTAG
 * (-DMPS3_XVC_TARGET_JTAGBB; -DMPS3_XVC_TARGET_SWDBB is the same registers
 * under their retired names, kept for the consumers xvc_server.h lists).
 *
 * Wire protocol (Xilinx XVC 1.0 — see README.md):
 *   "getinfo:"                          -> "xvcServer_v1.0:<max_bits>\n"
 *   "settck:" <u32 period_ns, LE>       -> <u32 period_ns, LE> (echo)
 *   "shift:"  <u32 num_bits, LE> <tms bytes> <tdi bytes>
 *                                       -> <tdo bytes>  (ceil(num_bits/8))
 * Commands are NOT newline-framed — each is a fixed prefix plus
 * binary-length-determined payload, so the parser below is an incremental
 * accumulate-and-reevaluate loop, never a line assembler. NONE of the
 * framing, gating or transport code below is target-dependent: the two
 * targets differ only inside xvc_server_do_shift().
 *
 * DBGBR target (default): one <=32-bit chunk per kick (platform_regs.h
 * DBGBR_*, layout CONFIRMED vs Xilinx XAPP1251's xvcServer.c): LENGTH <-
 * bits, TMS <- word, TDI <- word, CTRL <- GO, poll GO clear, TDO -> word.
 *
 * JTAGBB target: three AXI accesses per JTAG bit against DRIVE/SAMPLE
 * @0x44A7_0000 (fpga/shell/ip/jtag_bb/jtag_bb.sv) — see the "Bit order and TDO
 * sampling phase" block below. This is the Identify/IICE path and it is a
 * FIRMWARE-ONLY change: firmware re-bake + `updatemem` into the existing
 * `.bit`, so no `static_id` re-mint and the shipped overlay partials stay
 * valid. The whole reason to prefer it over the host-side Python XVC
 * server (host/socket_harness/xvc_server.py) is locality: the same three
 * accesses per bit cost ~100 ns each over the local AXI bus instead of
 * 1-5 ms each through xsdb -> hw_server -> MDM, i.e. one TCP round trip per
 * 2048-bit shift instead of 6,145 remote ones.
 *
 * settck's period is advisory only on BOTH targets. The Debug Bridge's
 * BSCAN chain has no TCK divider register (AXI-side shifts run at the
 * bridge's own pace, matching the Xilinx reference server on this IP), and
 * the bit-bang path's TCK period is set by AXI-write pace, not by a divider —
 * MPS3_XVC_SWDBB_TCK_STRETCH is the knob there, not settck. So the period is
 * stored and echoed, nothing more.
 *
 * ON THE IICE RM THE WIRE-SET CARRIES TWO TAPs. The reconfigurable partition
 * daisy-chains Identify's soft TAP with the DUT's SoC-400 SWJ-DP on the one
 * jtag_* wire-set (docs/planning/IICE_JTAG_CHAIN.md). NOTHING in this file
 * changes for that, and nothing here should: a chain is addressed by padding
 * IR and DR scans with BYPASS bits, which is the CLIENT's arithmetic (OpenOCD's
 * `jtag newtap` list, Identify's `chain add`). This engine shifts the vectors
 * it is given. Teaching it about TAPs would create a second place for the chain
 * to be described, and therefore a second place for it to be wrong.
 *
 * Gating (README "Gating during a swap"): while g_shell_state.xvc_gated,
 * getinfo/settck still answer (no hardware touched) but a completed
 * `shift:` STALLS un-consumed in the command buffer — no register access, no
 * reply — and is serviced on the first ungated poll. Stall (not error) was
 * chosen because hw_server treats a short/failed reply as a dead cable and
 * tears the whole session down; a stalled TCP read just looks slow. Whether
 * the stall stays inside hw_server's timeouts is UNMEASURED (a swap can sit in
 * an AWAIT_* state for up to MPS3_SWAP_AWAIT_IDLE_MS = 30 s) -- do not build on
 * it: the host closes its XVC session before a swap and reopens it after
 * (HANDOVER_RM_ILA_OVER_XVC.md §4.9 item 4, §4.10).
 *
 * PIN CONTENTION (bit-bang targets only): firmware/jtag_server/jtag_server.c
 * drives the very same DRIVE register for OpenOCD remote_bitbang on port 6921,
 * and both servers are linked into the shell image. The operational rule is
 * ONE CLIENT AT A TIME — an Identify session on 2542 and an OpenOCD session on
 * 6921 would interleave writes to one 3-bit register and corrupt both scans.
 * Nothing here arbitrates it.
 *
 * NOTE what the chain did and did not change about that. It removed the old,
 * harsher cost — Identify no longer takes the pins away from the DUT's DAP, so
 * both TAPs are permanently reachable — but the two SERVERS still share one
 * register, so they still cannot run at once. The contention is now about
 * bus ownership, not about which TAP is wired up.
 * (The legacy swd_server on 6920 is no longer built into the image at all:
 * firmware/platform/Makefile:450-467, LEGACY_SWD.)
 */
#include <stdio.h>
#include <string.h>
#include "xvc_server.h"
#include "../common/net_if.h"
#include "../common/net_proto.h"     /* MPS3_PORT_XVC */
#include "../common/platform_regs.h" /* MPS3_DBGBR_BASE + DBGBR_* */
#include "../common/service.h"       /* mps3_spin_until — a bound in MICROSECONDS */
#include "../coordinator/coordinator.h" /* g_shell_state.xvc_gated */

/* Bounded wait on DBGBR_CTRL.GO self-clear, per 32-bit chunk, in MICROSECONDS.
 *
 * It was an ITERATION COUNT of 100000. Nothing could say what that was worth:
 * it is (loop body cycles / AXI clock) seconds, so the same constant meant a
 * different duration on every build (common/service.h, "THE BOUNDS WERE
 * ITERATION COUNTS"). The deviation from XAPP1251 is unchanged and is still the
 * point -- XAPP1251 spins in an unbounded `while (ptr->ctrl_offset) {}` and a
 * dead or absent bridge must not wedge the shell's superloop -- but the bound is
 * now a time, so it can be reasoned about against the service budget and
 * asserted in a host test.
 *
 * WHERE 200 us COMES FROM, and what it costs at the worst legitimate shift:
 *   - one chunk is <= 32 TCK. The bridge's shift clock is derived from the
 *     shell's 100 MHz AXI clock, so 32 shifts is sub-microsecond at /1 and
 *     ~10 us even at a /32 divide. This file's long-standing note put it at
 *     "well under a microsecond of AXI polls". 200 us is >= 20x the slowest of
 *     those, and three orders of magnitude short of a human-visible stall.
 *   - a maximal ACCEPTED shift is MPS3_XVC_ACCEPT_VECTOR_BITS, which is 65
 *     chunks. The timeout is NOT paid 65 times: shift_dbgbr() returns -1 on the
 *     FIRST chunk that fails, so a dead bridge costs ONE 200 us wait and the
 *     client is dropped. The only way to pay it repeatedly is a bridge that
 *     settles slowly rather than not at all, and 65 x 200 us = 13 ms is still
 *     inside the xvc service's 50 ms budget (firmware/platform/src/main.c).
 * Declared in the header, not here, so the host test asserts the same symbol the
 * firmware spins on. */

#define XVC_PREFIX_GETINFO "getinfo:"
#define XVC_PREFIX_SETTCK  "settck:"
#define XVC_PREFIX_SHIFT   "shift:"

/* Command accumulator sized for the largest ACCEPTABLE command: "shift:" +
 * u32 + 2 vectors at the ACCEPT ceiling, not at the advertised size.
 *
 * Sizing this from MPS3_XVC_MAX_VECTOR_BYTES was a SECOND defect, independent
 * of the num_bits bound: at 6+4+2*256 = 522 bytes the accumulator cannot hold
 * the 524-byte command that Identify's measured 2053-bit shift arrives in, so
 * even with the bound relaxed the recv loop would fill the buffer, find no
 * dispatchable command, and stall forever (xvc_server_poll's
 * `s_cmd_len >= XVC_CMD_BUF_MAX` early return). The buffer and the bound have
 * to move together or the fix is not a fix. */
#define XVC_CMD_BUF_MAX (6u + 4u + 2u * MPS3_XVC_ACCEPT_VECTOR_BYTES)

/* Per-poll byte budget out of the transport. Sized to one maximal ACCEPTED
 * command so that property still holds ("one command can arrive within a
 * single poll once buffered"); a smaller budget would only spread the same
 * command over more polls, which is correct but needlessly slow. This bounds
 * recv work per superloop pass, not memory — the shift itself (3 register
 * accesses per bit) dominates the poll's cost by orders of magnitude. */
#define XVC_BYTES_PER_POLL XVC_CMD_BUF_MAX

static mps3_net_listener_t *s_listener;
static mps3_net_conn_t     *s_conn;

static uint8_t  s_cmd[XVC_CMD_BUF_MAX];
static uint32_t s_cmd_len;

/* Outbound reply staging (backpressure retry, same pattern as
 * coordinator_net.c): largest reply is a full TDO vector at the ACCEPT ceiling;
 * the getinfo string ("xvcServer_v1.0:2048\n") and settck echo are smaller. */
static uint8_t  s_out[MPS3_XVC_ACCEPT_VECTOR_BYTES];
static uint32_t s_out_len;
static uint32_t s_out_sent;

static uint32_t s_tck_period_ns; /* advisory, stored + echoed only */

static uint8_t s_tdo[MPS3_XVC_ACCEPT_VECTOR_BYTES];

void xvc_server_init(void)
{
    s_listener = mps3_net_listen(MPS3_PORT_XVC);
    s_conn = 0;
    s_cmd_len = 0;
    s_out_len = 0;
    s_out_sent = 0;
    s_tck_period_ns = 0;

#if MPS3_XVC_TARGET_IS_BITBANG
    /* Park the pins explicitly, exactly as jtag_server_init() does: DRIVE=0 is
     * TCK low / TMS low / TDI low, which is also jtag_bb.sv's own 3'b000 reset
     * state. Parking TCK LOW matters — the first write of the first shift must
     * not be a rising edge, or the FIRST TAP IN THE CHAIN eats a phantom clock
     * before the vector starts, and every TAP behind it goes with it. */
    mps3_reg_write32(XVC_BB_BASE, XVC_BB_DRIVE, 0u);
#endif
}

/* ---- shift engine ---------------------------------------------------------
 * Vector convention shared by both targets and by the host-side server:
 * XVC vectors are LSB-first WITHIN each byte, so shift bit i lives at
 * vec[i/8] bit (i%8). The DBGBR path expresses that by packing 4 bytes
 * little-endian into a chunk word; the bit-bang path reads one bit at a time.
 * ------------------------------------------------------------------------- */

#if MPS3_XVC_TARGET_IS_BITBANG

/* Shift bit `index` out of an XVC vector. The single statement of the
 * LSB-first-within-byte rule for the bit-bang path; the mirror image of
 * host/socket_harness/xvc_server.py's vector_bit(). */
static uint32_t vector_bit(const uint8_t *vec, uint32_t index)
{
    return (uint32_t)((vec[index >> 3] >> (index & 7u)) & 1u);
}

/* ---------------------------------------------------------------------------
 * The bit-bang loop — BIT ORDER AND SAMPLING PHASE, the two things that are
 * invisible until board day
 * ---------------------------------------------------------------------------
 * IEEE 1149.1: the TAP samples TMS/TDI on the RISING edge of TCK and updates
 * TDO on the FALLING edge. So while TCK is low the TAP is already presenting
 * the bit that the UPCOMING rising edge will shift out, and the correct read
 * point is the low phase BEFORE that edge. Per JTAG bit this emits exactly:
 *
 *     write DRIVE = {TDI, TMS, TCK=0}   settle TMS/TDI, and fall TCK (the
 *                                       falling edge that updates the TAP's
 *                                       TDO for THIS bit)
 *     read  SAMPLE                      <-- TDO for THIS bit
 *     write DRIVE = {TDI, TMS, TCK=1}   rising edge: TAP consumes TMS/TDI
 *
 * then one final DRIVE write with TCK clear so TCK idles low between shifts
 * (the "park"). That is 3n+1 accesses for n bits.
 *
 * This ordering is byte-for-byte OpenOCD's bitbang.c scan loop (write TCK=0,
 * read, write TCK=1) and byte-for-byte the host-side reference,
 * host/socket_harness/xvc_server.py's SwdbbBackend.shift_drive_sample().
 * Reading AFTER the rising edge instead — the natural-looking mistake —
 * returns IDCODE >> 1: a whole trace off by one bit, with no error anywhere.
 * Both the correct and the inverted orderings are exercised against an
 * in-memory IEEE-1149.1 TAP in firmware/test/test_xvc_server_swdbb.c, the
 * inverted one as an explicit negative control that asserts the >>1 damage.
 *
 * swd_bb.sv's 2-FF synchronizer on SAMPLE (swd_bb.sv:255-263) is invisible
 * here: 2 shell clocks is ~20 ns at 100 MHz, against a whole AXI-Lite read
 * transaction between the falling edge and the sample.
 *
 * No LENGTH register and no completion poll exist on this path, so there is
 * no bounded-spin failure mode: a dead/absent RM shows up as TDO stuck at
 * whatever the decoupler drives (the DFX decoupler holds RP outputs at 0),
 * i.e. an all-zeros scan. That is indistinguishable from a real all-zeros
 * chain from in here — the honest place to catch it is the host, where
 * Identify's own IDCODE check runs. This function therefore cannot fail and
 * always returns 0 once its arguments validate.
 * ------------------------------------------------------------------------- */
static void bb_drive(uint32_t word)
{
    mps3_reg_write32(XVC_BB_BASE, XVC_BB_DRIVE, word);
}

/* Hold the current DRIVE value for MPS3_XVC_SWDBB_TCK_STRETCH extra AXI
 * writes. Idempotent by construction (write-through register, same value),
 * so it lengthens the half-period without adding a TCK edge. Compiles away
 * entirely at the default stretch of 0. */
static void bb_hold(uint32_t word)
{
#if MPS3_XVC_SWDBB_TCK_STRETCH > 0u
    for (uint32_t k = 0; k < (uint32_t)MPS3_XVC_SWDBB_TCK_STRETCH; k++) {
        bb_drive(word);
    }
#else
    (void)word; /* default: the reference 3-accesses-per-bit sequence, with no
                 * dead loop for the compiler to warn about */
#endif
}

static int shift_bitbang(uint32_t num_bits, const uint8_t *tms, const uint8_t *tdi, uint8_t *tdo)
{
    uint32_t nbytes = (num_bits + 7u) / 8u;
    for (uint32_t i = 0; i < nbytes; i++) {
        tdo[i] = 0u; /* accumulate set bits only, so start clean (this also
                      * zero-fills the pad bits of the final partial byte) */
    }

    uint32_t word = 0u;
    for (uint32_t i = 0; i < num_bits; i++) {
        word = 0u;
        if (vector_bit(tms, i)) {
            word |= XVC_BB_TMS;
        }
        if (vector_bit(tdi, i)) {
            word |= XVC_BB_TDI;
        }

        /* TCK low, TMS/TDI settled (XVC_BB_TCK deliberately absent). */
        bb_drive(word);
        bb_hold(word);

        /* Sample as late as possible in the low phase — maximum settle time
         * for the TAP's TDO update and for the 2-FF synchronizer. */
        if (!MPS3_XVC_SWDBB_SAMPLE_LATE) {
            if (mps3_reg_read32(XVC_BB_BASE, XVC_BB_SAMPLE) & XVC_BB_TDO) {
                tdo[i >> 3] |= (uint8_t)(1u << (i & 7u));
            }
        }

        /* Rising edge: every TAP on the wire-set consumes TMS/TDI here. */
        bb_drive(word | XVC_BB_TCK);
        bb_hold(word | XVC_BB_TCK);

        if (MPS3_XVC_SWDBB_SAMPLE_LATE) {
            /* NEGATIVE CONTROL ONLY (never in a shipping build): reading here
             * is one bit late — the TAP has already shifted. */
            if (mps3_reg_read32(XVC_BB_BASE, XVC_BB_SAMPLE) & XVC_BB_TDO) {
                tdo[i >> 3] |= (uint8_t)(1u << (i & 7u));
            }
        }
    }

    /* Park: TCK low with TMS/TDI left at the vector's final values, so the
     * next shift's first write creates no spurious edge. num_bits >= 1 is
     * guaranteed by the caller, so `word` is the last bit's word. */
    bb_drive(word & ~(uint32_t)XVC_BB_TCK);
    return 0;
}

#else /* DBGBR (default) */

/* GO self-clear, as an mps3_spin_until predicate. Stateless: the register IS
 * the state, and there is no error outcome to carry out — the bridge either
 * clears GO or it does not, and "does not" is exactly what the timeout means. */
static int dbgbr_go_cleared(void *ctx)
{
    (void)ctx;
    return (mps3_reg_read32(MPS3_DBGBR_BASE, DBGBR_CTRL) & DBGBR_CTRL_GO) == 0u;
}

static uint32_t vector_word(const uint8_t *vec, uint32_t byte_off, uint32_t nbytes)
{
    /* Pack up to 4 vector bytes into one DBGBR chunk word, little-endian:
     * vector bit (8*byte_off + j) -> word bit j. */
    uint32_t w = 0;
    for (uint32_t i = 0; i < nbytes; i++) {
        w |= (uint32_t)vec[byte_off + i] << (8u * i);
    }
    return w;
}

static int shift_dbgbr(uint32_t num_bits, const uint8_t *tms, const uint8_t *tdi, uint8_t *tdo)
{
    for (uint32_t done = 0; done < num_bits; done += 32u) {
        uint32_t chunk_bits  = num_bits - done;
        if (chunk_bits > 32u) {
            chunk_bits = 32u;
        }
        uint32_t byte_off    = done / 8u;                 /* 32-bit chunks are byte-aligned */
        uint32_t chunk_bytes = (chunk_bits + 7u) / 8u;

        mps3_reg_write32(MPS3_DBGBR_BASE, DBGBR_LENGTH, chunk_bits);
        mps3_reg_write32(MPS3_DBGBR_BASE, DBGBR_TMS,
                         vector_word(tms, byte_off, chunk_bytes));
        mps3_reg_write32(MPS3_DBGBR_BASE, DBGBR_TDI,
                         vector_word(tdi, byte_off, chunk_bytes));
        mps3_reg_write32(MPS3_DBGBR_BASE, DBGBR_CTRL, DBGBR_CTRL_GO);

        if (!mps3_spin_until(dbgbr_go_cleared, NULL,
                             MPS3_XVC_DBGBR_SETTLE_TIMEOUT_US)) {
            return -1; /* dead/absent bridge — caller fails closed */
        }

        uint32_t w = mps3_reg_read32(MPS3_DBGBR_BASE, DBGBR_TDO);
        for (uint32_t i = 0; i < chunk_bytes; i++) {
            tdo[byte_off + i] = (uint8_t)(w >> (8u * i));
        }
    }
    return 0;
}

#endif /* target select */

/* The one public shift entry. Argument validation is target-independent and
 * lives here, so both targets reject the same requests identically (and so a
 * bad num_bits can never reach a register). */
int xvc_server_do_shift(uint32_t num_bits, const uint8_t *tms, const uint8_t *tdi, uint8_t *tdo)
{
    /* ACCEPT ceiling, not the advertised size — see xvc_server.h. Above it we
     * fail closed; we never clamp num_bits, because a short TDO reply reads to
     * the client as real captured data. */
    if (num_bits == 0 || num_bits > MPS3_XVC_ACCEPT_VECTOR_BITS) {
        return -1;
    }
#if MPS3_XVC_TARGET_IS_BITBANG
    return shift_bitbang(num_bits, tms, tdi, tdo);
#else
    return shift_dbgbr(num_bits, tms, tdi, tdo);
#endif
}

/* ---- transport helpers ------------------------------------------------------ */

static void drop_client(void)
{
    mps3_net_close(s_conn);
    s_conn = 0;
    s_cmd_len = 0;
    s_out_len = 0;
    s_out_sent = 0;
}

static void queue_reply(const void *buf, uint32_t len)
{
    /* len is bounded by sizeof(s_out) by construction (largest reply =
     * MPS3_XVC_ACCEPT_VECTOR_BYTES TDO bytes, since a shift is bounded by the
     * ACCEPT ceiling and s_out is sized to match). */
    memcpy(s_out, buf, len);
    s_out_len = len;
    s_out_sent = 0;
}

/* Push queued reply bytes; returns 1 when the queue is empty. */
static int flush_out(void)
{
    while (s_out_sent < s_out_len) {
        int n = mps3_net_send(s_conn, &s_out[s_out_sent], s_out_len - s_out_sent);
        if (n == MPS3_NET_ERR) {
            drop_client();
            return 0;
        }
        if (n == 0) {
            return 0; /* backpressure — retry next poll */
        }
        s_out_sent += (uint32_t)n;
    }
    s_out_len = 0;
    s_out_sent = 0;
    return 1;
}

static uint32_t le32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

/* Consume `n` bytes off the front of the command buffer. */
static void cmd_consume(uint32_t n)
{
    memmove(s_cmd, s_cmd + n, s_cmd_len - n);
    s_cmd_len -= n;
}

/* 1 if the first s_cmd_len bytes are still a valid (possibly partial)
 * prefix of `pfx`. */
static int prefix_possible(const char *pfx)
{
    uint32_t plen = (uint32_t)strlen(pfx);
    uint32_t n = (s_cmd_len < plen) ? s_cmd_len : plen;
    return memcmp(s_cmd, pfx, n) == 0;
}

static int prefix_complete(const char *pfx)
{
    uint32_t plen = (uint32_t)strlen(pfx);
    return s_cmd_len >= plen && memcmp(s_cmd, pfx, plen) == 0;
}

/* Evaluate the accumulated bytes. Returns:
 *    1 — one command consumed (a reply may be queued in s_out)
 *    0 — need more bytes (or a shift is stalled while gated)
 *   -1 — protocol violation / dead bridge: caller drops the connection
 */
static int try_dispatch(void)
{
    if (s_cmd_len == 0) {
        return 0;
    }
    if (!prefix_possible(XVC_PREFIX_GETINFO) &&
        !prefix_possible(XVC_PREFIX_SETTCK) &&
        !prefix_possible(XVC_PREFIX_SHIFT)) {
        return -1; /* not an XVC command — fail closed, never resync-guess */
    }

    if (prefix_complete(XVC_PREFIX_GETINFO)) {
        char info[32];
        int n = snprintf(info, sizeof(info), "xvcServer_v1.0:%u\n",
                         (unsigned)MPS3_XVC_MAX_VECTOR_BITS);
        cmd_consume((uint32_t)strlen(XVC_PREFIX_GETINFO));
        queue_reply(info, (uint32_t)n);
        return 1;
    }

    if (prefix_complete(XVC_PREFIX_SETTCK)) {
        if (s_cmd_len < 7u + 4u) {
            return 0;
        }
        s_tck_period_ns = le32(&s_cmd[7]);
        uint8_t echo[4] = { s_cmd[7], s_cmd[8], s_cmd[9], s_cmd[10] };
        cmd_consume(7u + 4u);
        queue_reply(echo, 4u);
        return 1;
    }

    if (prefix_complete(XVC_PREFIX_SHIFT)) {
        if (s_cmd_len < 6u + 4u) {
            return 0;
        }
        uint32_t num_bits = le32(&s_cmd[6]);
        if (num_bits == 0 || num_bits > MPS3_XVC_ACCEPT_VECTOR_BITS) {
            /* NOTE the bound is the ACCEPT ceiling, NOT the advertised
             * MPS3_XVC_MAX_VECTOR_BITS. Bounding by the advertisement is the
             * defect that kills Identify on its first real scan: it chunks the
             * payload at the advertised size and adds navigation bits on top,
             * so num_bits legitimately exceeds it (xvc_server.h has the
             * measurements). Past the ACCEPT ceiling it really is a desynced or
             * hostile client, so fail closed — never truncate. */
            return -1;
        }
        uint32_t vec_bytes = (num_bits + 7u) / 8u;
        uint32_t total = 6u + 4u + 2u * vec_bytes;
        if (s_cmd_len < total) {
            return 0;
        }

        if (g_shell_state.xvc_gated) {
            return 0; /* STALL: leave the command queued, touch nothing
                       * (see file header / README) */
        }

        const uint8_t *tms = &s_cmd[10];
        const uint8_t *tdi = &s_cmd[10 + vec_bytes];
        if (xvc_server_do_shift(num_bits, tms, tdi, s_tdo) != 0) {
            return -1; /* bridge poll timeout — fail closed */
        }
        cmd_consume(total);
        queue_reply(s_tdo, vec_bytes);
        return 1;
    }

    return 0; /* prefix still ambiguous/incomplete */
}

/* WEAK "never refuse" for the claim lock (xvc_server.h): bare metal unchanged. */
__attribute__((weak)) int mps3_xvc_refuse_peer(struct mps3_net_conn *conn)
{
    (void)conn;
    return 0;
}

void xvc_server_poll(void)
{
    /* Accept even while gated so a connect isn't left dangling (getinfo/
     * settck are answered regardless). Single client v1: hw_server holds
     * one XVC connection per cable; extras refused (close-immediately).
     * A claimed Linux harness refuses a non-local peer first, with a line.
     * Reap before refuse (net_if.h mps3_net_peer_closed): a current client
     * whose peer is already gone -- its EOF not yet read, e.g. behind a shift
     * stalled while gated -- is dropped (with whatever it left in s_cmd/s_out:
     * nobody is there to answer) and the newcomer adopted; only a LIVE client
     * makes the newcomer a refused extra. */
    mps3_net_conn_t *incoming = mps3_net_accept(s_listener);
    if (incoming) {
        if (mps3_xvc_refuse_peer(incoming)) {
            mps3_net_refuse_with_line(incoming, MPS3_XVC_LOCKED_LINE);
        } else {
            if (s_conn != 0 && mps3_net_peer_closed(s_conn)) {
                drop_client();
            }
            if (s_conn == 0) {
                s_conn = incoming;
                s_cmd_len = 0;
                s_out_len = 0;
                s_out_sent = 0;
            } else {
                mps3_net_close(incoming);
            }
        }
    }
    if (!s_conn) {
        return;
    }

    /* Strict request->response pacing: finish any half-sent reply first. */
    if (s_out_len > 0 && !flush_out()) {
        return;
    }
    if (!s_conn) {
        return; /* flush_out may have dropped the client */
    }

    uint32_t budget = XVC_BYTES_PER_POLL;
    for (;;) {
        /* Dispatch everything already buffered before reading more. */
        for (;;) {
            int rc = try_dispatch();
            if (rc < 0) {
                drop_client();
                return;
            }
            if (rc == 0) {
                break;
            }
            if (!flush_out()) {
                return; /* backpressure or client gone — resume next poll */
            }
            if (!s_conn) {
                return;
            }
        }

        if (budget == 0) {
            return;
        }
        if (s_cmd_len >= XVC_CMD_BUF_MAX) {
            /* Buffer full without a dispatchable command. The accumulator is
             * sized to exactly one maximal ACCEPTED shift command, and the
             * num_bits bound rejects anything larger, so the only way to get
             * here is a complete maximal shift stalled while gated — keep
             * waiting. (When the buffer was sized to the ADVERTISED value this
             * was instead reachable by a legal 2053-bit shift, and it stalled
             * the client forever.) */
            return;
        }

        uint32_t want = XVC_CMD_BUF_MAX - s_cmd_len;
        if (want > budget) {
            want = budget;
        }
        int n = mps3_net_recv(s_conn, &s_cmd[s_cmd_len], want);
        if (n == 0) {
            return;
        }
        if (n < 0) {
            drop_client(); /* closed or errored */
            return;
        }
        s_cmd_len += (uint32_t)n;
        budget -= (uint32_t)n;
    }
}
