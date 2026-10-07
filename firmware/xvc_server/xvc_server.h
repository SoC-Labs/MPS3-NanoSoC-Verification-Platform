/*
 * xvc_server.h — TCP 2542 XVC (Xilinx Virtual Cable v1.0) server. ONE
 * protocol engine, TWO selectable shift targets. See xvc_server/README.md.
 *
 * REAL (W-VITIS/A3): the three-verb XVC wire protocol (getinfo:/settck:/
 * shift:) is parsed incrementally off the common/net_if.h seam. Where the
 * bits then GO is a compile-time choice:
 *
 *   target      define                     shifts into
 *   ----------  -------------------------  --------------------------------
 *   DBGBR       (none — DEFAULT)           the shell's Xilinx Debug Bridge,
 *                                          LENGTH/TMS/TDI/TDO/CTRL @0x44A8,
 *                                          layout CONFIRMED vs XAPP1251
 *   JTAGBB      -DMPS3_XVC_TARGET_JTAGBB   JTAGBB DRIVE/SAMPLE @0x44A7,
 *                                          bit-banged, 3 AXI accesses/bit.
 *                                          THE LIVE BIT-BANG TARGET.
 *   SWDBB       -DMPS3_XVC_TARGET_SWDBB    the SAME registers under their
 *                                          RETIRED names. Kept only for the
 *                                          consumers listed below; new work
 *                                          uses JTAGBB.
 *
 * DBGBR is the DEFAULT because it is what the shipped 2542 server does: the
 * BSCAN chain it reaches is the STATIC design's own debug chain (Vivado
 * hw_server / ILA / VIO, ARCHITECTURE_SPEC §10). Nothing about this change
 * touches that path, and an undefined-flag build is byte-identical to it.
 *
 * JTAGBB is the Identify/IICE path, and since the 2026-09-10 rebase it is also
 * the ONLY correct one. Read this before touching either bit-bang spelling.
 *
 * WHAT CHANGED. The SWDBB target was written when the RM gave Identify's soft
 * TAP the four `swd_*` partition pins outright. The A6 SWD->JTAG cutover
 * DELETED that group: fpga/shell/ip/swd_bb was replaced by
 * fpga/shell/ip/jtag_bb, which the fielded BD instantiates at the SAME
 * 0x44A7_0000 page (fpga/shell/bd/shell_bd.tcl:560,1401), and the boundary's
 * debug group is now jtag_tck/tms/tdi/tdo (fpga/shell/boundary.yaml). So the
 * soft TAP no longer has a wire-set of its own; it is DAISY-CHAINED with the
 * DUT's SoC-400 SWJ-DP on the one JTAG wire-set
 * (docs/planning/IICE_JTAG_CHAIN.md, fpga/rp/nanosoc_iice/).
 *
 * WHY BOTH SPELLINGS STILL EXIST — and why that is a hazard worth naming.
 * The two register layouts happen to COINCIDE, bit for bit:
 *
 *     SWDBB_DRIVE_SWCLK    (1<<0) == JTAGBB_DRIVE_TCK   (1<<0)
 *     SWDBB_DRIVE_SWDIO_O  (1<<1) == JTAGBB_DRIVE_TMS   (1<<1)
 *     SWDBB_DRIVE_SWDIO_OE (1<<2) == JTAGBB_DRIVE_TDI   (1<<2)
 *     SWDBB_SAMPLE_SWDIO_I (1<<0) == JTAGBB_SAMPLE_TDO  (1<<0)
 *     MPS3_SWDBB_BASE            == MPS3_JTAGBB_BASE    (0x44A7_0000)
 *
 * so an old SWDBB build still drives the right wires on the fielded shell --
 * by luck, through the names of a block that no longer exists. That is exactly
 * the kind of coincidence that stops being true silently, so it is now a
 * COMPILE-TIME ASSERTION (xvc_bb_layout_matches_the_retired_swdbb_names below)
 * rather than a comment. If jtag_bb.sv's bit assignments ever move, the SWDBB
 * build stops compiling instead of driving the wrong pins.
 *
 * The SWDBB spelling is retained because these still name it and are NOT this
 * module's to change:
 *     firmware/platform/verify_shell_image.py   --expect-xvc-target swdbb
 *     firmware/platform/board_xvc_throughput.sh passes that flag
 *     firmware/test/{test_xvc_server_swdbb,test_xvc_posix_loopback,
 *                    xvc_fw_daemon,fake_jtag_tap}.c
 *     host/socket_harness/xvc_server.py         (its SwdbbBackend + docs)
 * Deleting it would break all of those at once. Retiring it is a separate,
 * mechanical change across those files; see this module's README.
 *
 * CHAIN AWARENESS — there is none here, and that is correct. This engine
 * shifts the TMS/TDI vectors it is handed and returns TDO. Which TAP those
 * bits address, and how many BYPASS bits pad them, is decided ENTIRELY by the
 * client (OpenOCD's `jtag newtap` list, Identify's `chain add`). A bit shifter
 * that tried to know about TAPs would have to be told the chain, and would
 * then be a second place for the chain to be wrong.
 *
 * ===========================================================================
 * DEPLOYMENT PROPERTY — why the SWDBB target is cheap
 * ===========================================================================
 * Selecting SWDBB changes ONLY this firmware. The shell BD, the partition
 * boundary (untouched by this choice: 47 ports / 148 bits today, per
 * fpga/shell/boundary.yaml; 35 when this was written), and the routed static
 * design are all untouched, so shipping it is a **firmware re-bake +
 * `updatemem` into the existing `.bit`**:
 *
 *   - it does NOT re-mint `static_id` (whatever the shell currently is --
 *     docs/FIELDED_SHELL.md is the one authority; the value 0x0EE58A4D that
 *     used to be written here was the shell of the day and has been superseded
 *     twice), and
 *   - it does NOT invalidate the overlay partials keyed to it.
 *
 * Contrast docs/planning/JTAG_UART_REMINT_PLAN.md, which REPLACES swd_bb_0
 * with a jtag_bb_0 block: that is a static rebuild and a re-mint. Reusing
 * the pins instead of rewiring them is the whole point.
 *
 * Host-tested in firmware/test/test_xvc_server.c (DBGBR),
 * firmware/test/test_xvc_server_swdbb.c (the bit-bang engine, incl. a real
 * IEEE-1149.1 IDCODE read and the inverted-phase negative control) and
 * firmware/test/test_xvc_jtagbb_chain.c (the JTAGBB target over a real POSIX
 * socket, against a TWO-TAP chain -- the shape the IICE RM presents).
 */
#ifndef MPS3_XVC_SERVER_H
#define MPS3_XVC_SERVER_H

#include <stdint.h>

#include "../common/platform_regs.h" /* JTAGBB_* / SWDBB_* names + bases */

#ifdef __cplusplus
extern "C" {
#endif

/* Which target this TU was built for. Never test the MPS3_XVC_TARGET_* defines
 * directly — use these, so a test can assert which target it linked.
 *
 * MPS3_XVC_TARGET_IS_BITBANG is what the shift engine branches on: JTAGBB and
 * SWDBB are the same three-accesses-per-bit loop over the same registers under
 * two names, and duplicating the loop would let the two drift. */
#if defined(MPS3_XVC_TARGET_JTAGBB)
#define MPS3_XVC_TARGET_IS_JTAGBB 1
#else
#define MPS3_XVC_TARGET_IS_JTAGBB 0
#endif

#if defined(MPS3_XVC_TARGET_SWDBB)
#define MPS3_XVC_TARGET_IS_SWDBB 1
#else
#define MPS3_XVC_TARGET_IS_SWDBB 0
#endif

#if MPS3_XVC_TARGET_IS_JTAGBB && MPS3_XVC_TARGET_IS_SWDBB
#error "define at most ONE of MPS3_XVC_TARGET_JTAGBB / MPS3_XVC_TARGET_SWDBB"
#endif

#define MPS3_XVC_TARGET_IS_BITBANG \
    (MPS3_XVC_TARGET_IS_JTAGBB || MPS3_XVC_TARGET_IS_SWDBB)

/* ---------------------------------------------------------------------------
 * SWDBB pin reuse (plan §3's table). DERIVED from the SWD names in
 * platform_regs.h rather than restated as 1/2/4, deliberately: the I22
 * lesson (OPEN_ISSUES §I22) is that a bit mapping must be re-derived from
 * the authority, never retyped. swd_bb.sv keeps DRIVE[2:0] and nothing else,
 * so these three ARE the whole register.
 *
 *   DRIVE[0]  swd_clk     -> TCK   (shell -> RP)
 *   DRIVE[1]  swd_dio_o   -> TMS   (shell -> RP)
 *   DRIVE[2]  swd_dio_oe  -> TDI   (shell -> RP)
 *   SAMPLE[0] swd_dio_i   -> TDO   (RP -> shell, 2-FF synchronized)
 *
 * Same values as host/socket_harness/xvc_server.py's DRIVE_TCK/DRIVE_TMS/
 * DRIVE_TDI/SAMPLE_TDO — the two implementations must agree bit-for-bit or a
 * host-proven chain silently breaks under firmware. Pinned by
 * test_xvc_server_swdbb.c and by test_xvc_server.py.
 * ------------------------------------------------------------------------- */
#define XVC_SWDBB_TCK  SWDBB_DRIVE_SWCLK     /* DRIVE[0] */
#define XVC_SWDBB_TMS  SWDBB_DRIVE_SWDIO_O   /* DRIVE[1] */
#define XVC_SWDBB_TDI  SWDBB_DRIVE_SWDIO_OE  /* DRIVE[2] */
#define XVC_SWDBB_TDO  SWDBB_SAMPLE_SWDIO_I  /* SAMPLE[0], RO */

/* ---------------------------------------------------------------------------
 * JTAGBB — the LIVE block (fpga/shell/ip/jtag_bb/jtag_bb.sv @ 0x44A7_0000,
 * instantiated by the fielded BD at shell_bd.tcl:560/1401). Derived from the
 * JTAG names in platform_regs.h for the same I22 reason as above: a bit mapping
 * is re-derived from the authority, never retyped.
 *
 *   DRIVE[0]  jtag_tck   -> TCK   (shell -> RP, shared by BOTH chained TAPs)
 *   DRIVE[1]  jtag_tms   -> TMS   (shell -> RP, shared by BOTH chained TAPs)
 *   DRIVE[2]  jtag_tdi   -> TDI   (shell -> RP, head of the chain)
 *   SAMPLE[0] jtag_tdo   -> TDO   (RP -> shell, tail, 2-FF synchronized)
 * ------------------------------------------------------------------------- */
#define XVC_JTAGBB_TCK  JTAGBB_DRIVE_TCK      /* DRIVE[0] */
#define XVC_JTAGBB_TMS  JTAGBB_DRIVE_TMS      /* DRIVE[1] */
#define XVC_JTAGBB_TDI  JTAGBB_DRIVE_TDI      /* DRIVE[2] */
#define XVC_JTAGBB_TDO  JTAGBB_SAMPLE_TDO     /* SAMPLE[0], RO */

/* The names the shift engine actually uses, so there is ONE loop for the two
 * spellings. Selecting JTAGBB or SWDBB changes only which set of platform_regs
 * names is consulted — never the sequence of accesses. */
#if MPS3_XVC_TARGET_IS_SWDBB
#define XVC_BB_BASE    MPS3_SWDBB_BASE
#define XVC_BB_DRIVE   SWDBB_DRIVE
#define XVC_BB_SAMPLE  SWDBB_SAMPLE
#define XVC_BB_TCK     XVC_SWDBB_TCK
#define XVC_BB_TMS     XVC_SWDBB_TMS
#define XVC_BB_TDI     XVC_SWDBB_TDI
#define XVC_BB_TDO     XVC_SWDBB_TDO
#else
#define XVC_BB_BASE    MPS3_JTAGBB_BASE
#define XVC_BB_DRIVE   JTAGBB_DRIVE
#define XVC_BB_SAMPLE  JTAGBB_SAMPLE
#define XVC_BB_TCK     XVC_JTAGBB_TCK
#define XVC_BB_TMS     XVC_JTAGBB_TMS
#define XVC_BB_TDI     XVC_JTAGBB_TDI
#define XVC_BB_TDO     XVC_JTAGBB_TDO
#endif

/* THE COINCIDENCE, ASSERTED AT COMPILE TIME.
 *
 * A SWDBB-target build still reaches the right pins on the fielded shell only
 * because jtag_bb.sv happens to use the same page and the same bit positions
 * the retired swd_bb.sv did. Nothing enforces that, and if it stopped being
 * true the SWDBB build would silently bit-bang the wrong wires: TCK on the TMS
 * pin is not an error, it is a garbage scan.
 *
 * This typedef makes the whole tree stop compiling instead. It is deliberately
 * NOT #if'd on the selected target: every build of this header checks it, so
 * the day someone renumbers jtag_bb's DRIVE bits they are told immediately,
 * not on the next board day. If you are here because it fired: the fix is to
 * finish retiring the SWDBB spelling (see the consumer list in the file
 * header), not to widen the assertion. */
typedef char xvc_bb_layout_matches_the_retired_swdbb_names[
    ((MPS3_SWDBB_BASE      == MPS3_JTAGBB_BASE)   &&
     (SWDBB_DRIVE          == JTAGBB_DRIVE)       &&
     (SWDBB_SAMPLE         == JTAGBB_SAMPLE)      &&
     (SWDBB_DRIVE_SWCLK    == JTAGBB_DRIVE_TCK)   &&
     (SWDBB_DRIVE_SWDIO_O  == JTAGBB_DRIVE_TMS)   &&
     (SWDBB_DRIVE_SWDIO_OE == JTAGBB_DRIVE_TDI)   &&
     (SWDBB_SAMPLE_SWDIO_I == JTAGBB_SAMPLE_TDO)) ? 1 : -1];

/* Optional TCK half-period stretch, in extra idempotent DRIVE re-writes per
 * phase. Default 0 = the reference 3-accesses-per-bit sequence.
 *
 * Why it exists: Identify's soft TAP is auto-constrained to a 250 ns TCK
 * period (plan §2, .../share/synthesis/syn.sdc:4). The bit-bang period is
 * set by AXI-write pace, NOT by settck — if MicroBlaze AXI-Lite turns out
 * faster than ~125 ns per access on silicon, the free-running bit-bang would
 * overclock the TAP. Re-writing DRIVE with the SAME value is a pure
 * time-waster: swd_bb.sv's DRIVE is write-through with no side effects, so
 * an extra write cannot create a TCK edge (only 0->1 on bit 0 does). Raising
 * this is therefore the safe, RTL-free way to slow TCK down.
 * test_xvc_server_swdbb.c proves a stretched shift still produces exactly one
 * edge per bit and the identical TDO. */
#ifndef MPS3_XVC_SWDBB_TCK_STRETCH
#define MPS3_XVC_SWDBB_TCK_STRETCH 0u
#endif

/* TEST-ONLY NEGATIVE-CONTROL KNOB — never define in a shipping build.
 * 1 moves the SAMPLE read to AFTER the rising edge, which is the natural-
 * looking mistake and which yields IDCODE >> 1 instead of IDCODE. The
 * firmware/test harness builds one binary with it set purely to demonstrate
 * that the correct build's IDCODE proof has teeth (same dual-binary pattern
 * as test_swap_qspi_free / test_swap_qspi_free_tinyarena). */
#ifndef MPS3_XVC_SWDBB_SAMPLE_LATE
#define MPS3_XVC_SWDBB_SAMPLE_LATE 0
#endif

/* ===========================================================================
 * ADVERTISED size vs ACCEPTED size — they are NOT the same number
 * ===========================================================================
 * What goes in the getinfo: reply ("xvcServer_v1.0:<max_vector_bits>\n").
 * 2048 matches the Xilinx reference servers' usual advertisement and the host
 * server's XVC_MAX_VECTOR_BITS, which is what keeps the two interchangeable
 * behind one client.
 *
 * THIS IS A CHUNK HINT TO THE CLIENT, NOT A CEILING ON num_bits. The previous
 * comment here asserted "hw_server then never sends a larger shift" — that
 * belief is false, and it is the whole defect. A client sizes the PAYLOAD of a
 * shift: against this number and then adds its own TAP state-navigation TMS
 * bits, so the num_bits that arrives OVERSHOOTS it.
 *
 * MEASURED against the real Synopsys Identify debugger (T-2022.09-SP2) driving
 * the HOST server (host/socket_harness/xvc_server.py) over --fake-tap, varying
 * only the advertisement:
 *
 *     advertised    num_bits then requested
 *     ----------    ----------------------------------------------
 *           1024    1029   (= 1024 payload + 5 navigation bits)
 *           2048    2053   (= 2048 + 5)
 *           8192    3206   (whole scan fits; no chunking at all)
 *
 * Bounding num_bits by the advertised value drops the connection on the
 * client's first real scan. Identify reports only "Couldn't shift do data from
 * xvcServer", naming neither the shift nor the lengths.
 * ------------------------------------------------------------------------- */
#define MPS3_XVC_MAX_VECTOR_BITS 2048u
#define MPS3_XVC_MAX_VECTOR_BYTES ((MPS3_XVC_MAX_VECTOR_BITS + 7u) / 8u)

/* How much larger than the advertisement a shift: may legitimately be.
 *
 * 4x is the ratio the Xilinx XAPP1251 reference xvcServer.c itself runs at: it
 * advertises 2048 but bounds a request only by `nr_bytes * 2 > sizeof(buffer)`
 * against a 2048-BYTE buffer, i.e. 1024 bytes per vector = 8192 bits. So no
 * conformant client has ever been entitled to treat the advertised value as a
 * num_bits cap. Matching the ratio the host server uses (XVC_ACCEPT_RATIO)
 * keeps the two servers behaviourally identical, which is the point of having
 * two.
 *
 * COST — this is LMB BRAM, so it is not free. The three static buffers below
 * scale with the ACCEPT size, measured with mb-size on the real MicroBlaze
 * object (-mcpu=v11.0 -Os):
 *
 *     ratio  accept bits  s_cmd  s_out  s_tdo  total   delta
 *     -----  -----------  -----  -----  -----  ------  ------
 *       1x*         2048    522    256    256   1,034       0   *= the defect
 *       2x          4096  1,034    512    512   2,058  +1,024
 *       4x          8192  2,058  1,024  1,024   4,106  +3,072
 *
 * +3,072 B against the LMB budget in firmware/platform/Makefile: LMB_KB=1024
 * gives 1,048,368 usable and the documented allocation is
 * 226,652 + 2 x 262,144 = 750,940 B, so ~297,428 B is spare. The 4x ratio
 * spends 1.0% of that slack. 2x would do for the client measured above (which
 * needs 2,053 of the 8,192), but the headroom is cheap and the overshoot is a
 * property of the CLIENT's navigation, not a constant we control — a deeper
 * chain or a different tool navigates with more bits.
 *
 * Above the accept ceiling we still FAIL CLOSED (drop the connection). Never
 * truncate: a short TDO reply is indistinguishable to the client from real
 * captured data, which is the worst failure mode available to us on silicon. */
#ifndef MPS3_XVC_ACCEPT_RATIO
#define MPS3_XVC_ACCEPT_RATIO 4u
#endif
#define MPS3_XVC_ACCEPT_VECTOR_BITS \
    (MPS3_XVC_ACCEPT_RATIO * MPS3_XVC_MAX_VECTOR_BITS)
#define MPS3_XVC_ACCEPT_VECTOR_BYTES \
    ((MPS3_XVC_ACCEPT_VECTOR_BITS + 7u) / 8u)

/* The DBGBR per-chunk settle wait, in MICROSECONDS off the free-running AXI
 * timer (was an iteration count of 100000, which is not a duration -- see
 * common/service.h). xvc_server.c's definition carries the full derivation:
 * >= 20x the slowest plausible 32-TCK chunk, paid ONCE on a dead bridge because
 * shift_dbgbr() returns on the first failing chunk, and 65 x 200 us = 13 ms at
 * the maximal accepted shift, inside the xvc service's 50 ms budget.
 *
 * It lives in the HEADER so firmware/test/test_xvc_server.c asserts the same
 * symbol the firmware spins on rather than a copy of the number. Overridable
 * for a one-off experiment; the host test pins the shipped value. */
#ifndef MPS3_XVC_DBGBR_SETTLE_TIMEOUT_US
#define MPS3_XVC_DBGBR_SETTLE_TIMEOUT_US 200u
#endif

/* Binds the 2542 listener. On a SWDBB-target build it ALSO parks the reused
 * pins (DRIVE=0: TCK low, TMS/TDI low) so the first write of the first shift
 * cannot be a phantom rising edge. */
void xvc_server_init(void);

/* Non-blocking poll — accept/drain/dispatch getinfo/settck/shift commands
 * on the 2542 listener. While g_shell_state.xvc_gated a pending `shift:`
 * STALLS (bytes stay queued, no register access on either target, no reply —
 * serviced once ungated); getinfo:/settck: are still answered (harmless, no
 * hardware touched) so a mid-swap hw_server handshake doesn't wedge. See
 * README "Gating during a swap" for why stall-not-error was chosen. */
void xvc_server_poll(void);

/* THE CLAIM LOCK on 2542 (2026-09-26, HM_ANSWERS C3 / change 10; David: YES).
 * Asked for every accepted connection, BEFORE the single-client rule: nonzero =
 * refuse this peer with ONE line
 *   {"ok":false,"err":"xvc locked: board claimed (use ssh)","code":"locked"}
 * then close (hw_server fails either way; the line tells a tool why). A session
 * already open keeps running. WEAK default 0 (bare metal has no claim);
 * mps3-harnessd refuses a non-local peer once its SSH is claimed (slot_linux.c),
 * and reports `xvc_lock` in version.features. */
struct mps3_net_conn;
int mps3_xvc_refuse_peer(struct mps3_net_conn *conn);
#define MPS3_XVC_LOCKED_LINE \
    "{\"ok\":false,\"err\":\"xvc locked: board claimed (use ssh)\",\"code\":\"locked\"}\n"

/* Clocks `num_bits` TMS/TDI bits through the selected target and captures
 * TDO. `tms`/`tdi`/`tdo` are each ceil(num_bits/8) bytes, LSB-first per bit
 * within each byte (the XVC convention, identical on both targets: DBGBR
 * packs 4 of those bytes little-endian per 32-bit chunk so vector bit 32k+j
 * lands in chunk k bit j; SWDBB reads bit i directly out of vec[i/8] bit
 * i%8).
 *
 * Returns 0 on success and -1 on a bad argument (num_bits 0 or >
 * MPS3_XVC_ACCEPT_VECTOR_BITS — the ACCEPT ceiling, deliberately larger than
 * the advertised MPS3_XVC_MAX_VECTOR_BITS) on both targets. DBGBR additionally returns -1
 * on a bounded CTRL-poll timeout (dead/absent Debug Bridge — fail closed,
 * the poll loop drops the connection). The SWDBB bit-bang has no completion
 * handshake to time out, so it has no such failure: an absent/decoupled RM
 * simply scans as all-zero TDO, which only the host (Identify's own IDCODE
 * check) can distinguish. */
int xvc_server_do_shift(uint32_t num_bits, const uint8_t *tms, const uint8_t *tdi, uint8_t *tdo);

#ifdef __cplusplus
}
#endif

#endif /* MPS3_XVC_SERVER_H */
