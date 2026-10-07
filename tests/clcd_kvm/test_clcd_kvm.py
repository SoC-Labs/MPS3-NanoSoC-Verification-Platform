"""tests/clcd_kvm/test_clcd_kvm.py — the CLCD **KVM**: the panel arbiter that
sits between the harness's `clcd_0` and a DUT-side accelerator and hands the one
physical HX8347-D 8080 bus back and forth between them.

Contract (FROZEN, and the ONLY thing this bench is written against — the RTL was
written in parallel by a different agent, from the same document):
    fpga/shell/ip/clcd_kvm/README.md        (ports, CSRs, FSM, gates)
    docs/contracts/dut-display-tunnel.md    (the DUT-side wire encoding)
    docs/contracts/shell-regmap.md  §CLCDKVM @ 0x44AD_0000 (v0.5)
    docs/CLCD_PANEL_FACTS.md                (the panel that is actually lit)

The panel side is watched by `tests/clcd/clcd_panel_model.py`, imported AS-IS:
it latches {RS, byte} on each WR_n RISING edge while CS_n is asserted, and it
watches ONLY the pads — so it is bus-agnostic and serves this bench unchanged.

------------------------------------------------------------------------------
THE HEADLINE PROPERTY — read this before changing anything
------------------------------------------------------------------------------
`clcd_kvm` exists to pass ONE test: **no truncated bus cycle across a switch.**
Everything else in the block is in service of it. A 2:1 mux would be free; what
is not free is cutting over *without* chopping a WR pulse in half (a garbage
byte into GRAM) or leaving CS asserted across the seam (which desyncs the
HX8347's command/parameter state machine — the panel then misinterprets every
subsequent byte, and only a hard reset recovers it).

The gate is **TWO-SIDED** (README §6), and this is the subtlety the whole wave
turns on:
  * S_DRAIN — the OUTGOING owner must be quiescent before we stop driving it.
  * S_GRANT — the INCOMING owner must be quiescent before we start driving it,
    because it has no idea it is about to be granted and may already be
    mid-cycle. **A one-sided gate is not enough**, and a bench that only tests
    the outgoing side would pass a KVM that jams the pads into the middle of the
    incoming source's WR pulse.
Both sides are tested here (`test_no_truncated_cycle_*` for the outgoing gate at
every phase of the 8080 cycle, `test_incoming_gate_*` for the incoming one).

**How the detector works, and why it cannot be fooled.** Both source models run
FREELY — they never learn that the KVM took the pads away — and each records the
bytes whose WR rising edge *it* completed. The panel model records the bytes that
actually reached the pads. A truncation shows up as one of:
  * a decoded byte that no source ever finished (or a missing one), or
  * `cs_low_throughout = False` / `pd_stable = False` on a strobe, or
  * a WR-low width or CS-setup interval that is not the source's programmed one
    (a mid-phase cutover necessarily shortens exactly one of them).
`test_force_switch_truncates_on_purpose` is the POSITIVE CONTROL: it uses the
block's own `CTRL.force_switch` escape hatch (which by contract skips the drain
gate) to produce a real truncation and asserts the detector FIRES. A detector
that has never fired is not a detector.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "clcd"))
sys.path.insert(0, os.path.dirname(__file__))

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge, Timer

from dut_presence import rtl_ready
from regmap import AxiLiteMaster
from clcd_panel_model import PanelBusModel, RS_CMD, RS_DATA  # noqa: F401  (reused AS-IS)
from kvm_models import (
    DutTunnelSource, HarnessSource,
    H_IDLE, H_SETUP, H_STROBE_LO, H_STROBE_HI, H_PHASE_NAMES,
    T_BUSY, T_CS, T_PD_OE, T_RD, T_REQ, T_RS, T_SPARE, T_WR, PD_SHIFT,
)

# The RTL is written in parallel by W2-A against the same frozen README. Until
# it lands, every test SKIPs cleanly rather than hanging (tests/clcd/ set this
# precedent; see tests/common/dut_presence.py for why "the file exists" is not
# the right readiness gate).
_RTL_DIR = os.path.join(os.path.dirname(__file__), "..", "..",
                        "fpga", "shell", "ip", "clcd_kvm")
NO_RTL = not rtl_ready(_RTL_DIR, ["clcd_kvm.sv"])

# ---- CSR map — clcd_kvm/README.md §5 @ 0x44AD_0000, 64 KiB page ------------ #
CTRL = 0x00
STATUS = 0x04
EVENT = 0x08
PANEL_TMR = 0x0C
TIMEOUT = 0x10
DEBOUNCE = 0x14
TUNNEL = 0x18
MAPPED = (CTRL, STATUS, EVENT, PANEL_TMR, TIMEOUT, DEBOUNCE, TUNNEL)

# CTRL
C_SRC_SEL = 1 << 0          # R/W-gated: the REQUESTED owner
C_FORCE_HARNESS = 1 << 1
C_TIMEOUT_EN = 1 << 2       # reset 1
C_PB_EN = 1 << 3            # reset 1
C_DUT_REQ_EN = 1 << 4       # reset 0 (an unprovisioned RM must not grab the panel)
C_BACKLIGHT = 1 << 5
C_PANEL_RST_N = 1 << 6
C_BL_RST_SRC = 1 << 7       # 0 = follow clcd_0's CTRL[1]/[2] (the drop-in default)
C_PANEL_RST_PULSE = 1 << 8  # W1P
C_FORCE_SWITCH = 1 << 9     # W1P
C_SRC_SEL_WE = 1 << 16      # W1P — write-enable for [0]
CTRL_RESET = C_TIMEOUT_EN | C_PB_EN                      # 0x0000_000C

# STATUS
S_OWNER = 1 << 0
S_SWITCH_PENDING = 1 << 1
S_TGT_OWNER = 1 << 2
S_PANEL_RST_ACTIVE = 1 << 3
S_PANEL_SETTLING = 1 << 4
S_GRANTING = 1 << 5
S_DRAINING = 1 << 6
S_KVM_DRIVES_PADS = 1 << 7
S_HARNESS_QUIET = 1 << 8
S_DUT_QUIET = 1 << 9
S_DUT_REQ = 1 << 10
S_PB_LEVEL = 1 << 11
S_PB_RAW = 1 << 12
S_DECOUPLED = 1 << 13
S_RP_IN_RESET = 1 << 14
S_INTERLOCK = 1 << 15
S_STATE_SHIFT = 16
S_STATE_MASK = 0x7 << S_STATE_SHIFT
ST_OWN, ST_DRAIN, ST_RST, ST_SETTLE, ST_GRANT = 0, 1, 2, 3, 4

# EVENT (RW1C)
E_HARNESS_GAINED = 1 << 0
E_HARNESS_LOST = 1 << 1
E_DUT_GAINED = 1 << 2
E_DUT_LOST = 1 << 3
E_TIMEOUT_FIRED = 1 << 4
E_FORCED_REVERT = 1 << 5
E_PB_TOGGLE = 1 << 6
E_PANEL_RESET_DONE = 1 << 7
E_ALL = 0xFF

OWNER_HARNESS = 0
OWNER_DUT = 1

# Reset values (README §2 parameters / §5 tables)
PANEL_TMR_RESET = (5000 << 16) | 2000
TIMEOUT_RESET = 1000
DEBOUNCE_RESET = 10000

# TICK_DIV = CLK_HZ / 1e6 = 100 at the shipped 100 MHz shell clock: every µs
# field costs 100 s_axi_aclk cycles. The bench keeps CLK_HZ at its SHIPPED value
# (it does not lower it) so the real divider is exercised, and instead programs
# SMALL µs values — which is exactly what those CSRs are for (README §14).
TICK_CYC = 100

# Disjoint byte ranges, so every byte at the panel is attributable to a source
# with no ambiguity. (The KVM never rewrites data, so this is a labelling trick,
# not an assumption about the RTL.)
def _h_bytes(n, first=0x10):
    """Harness stream: 0x10.. — mixed CMD/DATA, like a real init table."""
    return [(RS_CMD if i % 3 == 0 else RS_DATA, (first + i) & 0xFF)
            for i in range(n)]


def _d_bytes(n, first=0x80):
    """DUT stream the bench EXPECTS to see at the panel: 0x80.."""
    return [(RS_CMD if i % 4 == 0 else RS_DATA, (first + i) & 0xFF)
            for i in range(n)]


def _d_freewheel(n, first=0xC0):
    """DUT bytes emitted while the DUT does NOT own the panel. Every one of
    these MUST be discarded by the KVM — if one reaches the pads, the KVM
    connected a source it had not granted."""
    return [(RS_DATA, (first + i) & 0xFF) for i in range(n)]


# --------------------------------------------------------------------------- #
# Pads-only invariant monitor.
#
# Deliberately source-agnostic: it reads NOTHING but the panel pads, so it
# cannot be fooled by an RTL internal changing name, and it holds for every test
# regardless of who owns the bus. The CS-across-a-handover property is expressed
# through the panel reset, which is the trick that makes it pads-only: EVERY
# handover passes through S_RST (README §7), so
#
#       "CS_n was never left asserted across a handover"
#            <=>  "CLCD_RST was never driven low while CS_n was asserted"
#
# and the right-hand side is visible on four wires with no knowledge of the FSM.
# --------------------------------------------------------------------------- #
class PadInvariant:
    def __init__(self, dut):
        self.dut = dut
        self.violations = []
        self.enabled = True
        self.saw_panel_reset = False
        self._stop = False
        self.n = 0

    def stop(self):
        self._stop = True

    def check(self):
        assert not self.violations, (
            "panel-pad invariant violated:\n  " + "\n  ".join(self.violations[:12]))

    async def run(self):
        d = self.dut
        while not self._stop:
            await RisingEdge(d.s_axi_aclk)
            await ReadOnly()
            self.n += 1
            if not self.enabled:
                continue
            try:
                cs = int(d.clcd_cs_n_o.value)
                wr = int(d.clcd_wr_n_o.value)
                rd = int(d.clcd_rd_n_o.value)
                rst_n = int(d.clcd_rst_n_o.value)
                bl = int(d.clcd_bl_o.value)
                oe = int(d.clcd_pd_oe.value)
            except ValueError:
                continue                       # x/z mid-reset: don't fabricate
            if rst_n == 0:
                self.saw_panel_reset = True
            def v(msg):
                if len(self.violations) < 40:
                    self.violations.append(f"[cycle {self.n}] {msg}")
            # README §8: clcd_pd_oe is held 1 at ALL times in v1, in every state
            # and under either owner. Never float the bus toward the panel.
            if oe != 1:
                v(f"clcd_pd_oe={oe}: the pads must never be floated (README §8)")
            # A write strobe outside a chip select is a malformed 8080 cycle by
            # construction — the panel latches on WR's rising edge only while CS
            # is asserted, so this is either a truncated cycle or a spurious one.
            if wr == 0 and cs == 1:
                v("WR_n asserted while CS_n is DEasserted — malformed 8080 cycle")
            if rd == 0:
                v("clcd_rd_n_o asserted: READ_PATH=0 ships, RD must never assert")
            if rst_n == 0:
                # THE headline invariant, in pads-only form.
                if cs == 0:
                    v("CLCD_RST driven LOW while CS_n is ASSERTED — a bus cycle "
                      "was cut in half by the handover's panel reset")
                if wr == 0:
                    v("CLCD_RST driven LOW while WR_n is ASSERTED — a WR pulse "
                      "was truncated by the handover's panel reset")
                # README §8/§10: BL is forced off whenever the panel is in reset,
                # from ANY cause.
                if bl != 0:
                    v("CLCD_BL on while the panel is held in reset (README §10)")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
async def _bring_up(dut, dut_period_ns=20.0, skew_ns=0.0, seed=1,
                    cs_setup=2, wr_lo=4, wr_hi=4, npb1_at_reset=1):
    """Clock, reset, both source models, the panel model and the pad invariant.

    h_bl / h_rst_n start at 1/1: the shipped firmware has already lit the panel
    through CLCD.CTRL[1]/[2], and CLCDKVM.CTRL.bl_rst_src resets to 0 (= follow
    clcd_0), which is the drop-in property of README §10.

    npb1_at_reset: the USER_nPB1 pad level through reset (0 = HELD). pb_level
    resets to "pressed" (README §11 step 5), so with the button released it
    reads 1 until the first 1 µs tick accepts the release; bring-up waits that
    tick out, so every test starts from a settled button.
    """
    cocotb.start_soon(Clock(dut.s_axi_aclk, 10, units="ns").start())

    dut.s_axi_aresetn.value = 0
    dut.s_axi_awvalid.value = 0
    dut.s_axi_wvalid.value = 0
    dut.s_axi_bready.value = 0
    dut.s_axi_arvalid.value = 0
    dut.s_axi_rready.value = 0
    dut.s_axi_awaddr.value = 0
    dut.s_axi_araddr.value = 0
    dut.s_axi_wdata.value = 0
    dut.s_axi_wstrb.value = 0xF
    dut.s_axi_awprot.value = 0
    dut.s_axi_arprot.value = 0
    dut.clcd_pd_i.value = 0
    dut.decouple_status.value = 0
    dut.rp_resetn.value = 1
    dut.user_npb1.value = npb1_at_reset  # ACTIVE-LOW pad: 1 = NOT pressed

    h = HarnessSource(dut, cs_setup=cs_setup, wr_lo=wr_lo, wr_hi=wr_hi)
    h.idle_now()
    d = DutTunnelSource(dut, period_ns=dut_period_ns, skew_ns=skew_ns, seed=seed)
    d.idle_now()

    panel = PanelBusModel(dut)
    inv = PadInvariant(dut)

    await Timer(55, units="ns")
    dut.s_axi_aresetn.value = 1
    await RisingEdge(dut.s_axi_aclk)

    cocotb.start_soon(h.run())
    cocotb.start_soon(d.run())
    cocotb.start_soon(panel.run())
    cocotb.start_soon(inv.run())

    axi = AxiLiteMaster.from_dut(dut)
    await ClockCycles(dut.s_axi_aclk, 4)
    if npb1_at_reset:
        # the first tick (TICK_DIV cycles) accepts the released button
        await ClockCycles(dut.s_axi_aclk, TICK_CYC + 8)
    return axi, h, d, panel, inv


async def _timers(axi, rst_us=1, settle_us=1, timeout_us=40, debounce_us=2):
    """Program the four µs CSRs small, so a full handover is a few hundred
    cycles instead of a million (README §14). TICK_DIV stays at the shipped 100."""
    await axi.write(PANEL_TMR, ((settle_us & 0xFFFF) << 16) | (rst_us & 0xFFFF))
    await axi.write(TIMEOUT, timeout_us)
    await axi.write(DEBOUNCE, debounce_us)


async def _req_owner(axi, want, base=CTRL_RESET):
    """Request an owner through CTRL. src_sel is W-GATED: it only lands if
    src_sel_we (bit 16) is written 1 in the same transaction (README §5)."""
    await axi.write(CTRL, base | (want & 1) | C_SRC_SEL_WE)


async def _wait_owner(dut, want, timeout_cycles=20000, what=""):
    for i in range(timeout_cycles):
        await RisingEdge(dut.s_axi_aclk)
        if int(dut.owner_o.value) == want:
            return i
    raise AssertionError(
        f"ownership never reached {'DUT' if want else 'HARNESS'} within "
        f"{timeout_cycles} cycles {what}")


async def _owner(dut):
    await ReadOnly()
    return int(dut.owner_o.value)


async def _wait_tunnel_busy(dut, axi, timeout_cycles=20000):
    """Wait until the KVM SEES the DUT as non-quiescent (STATUS.dut_quiet == 0),
    i.e. the DUT's busy/cs have crossed the tunnel CDC. Used before requesting a
    switch AWAY from the DUT so the drain gate has a real non-quiescent tunnel to
    wait on (see the DUT->harness test for why the harness needs no equivalent)."""
    for _ in range(timeout_cycles):
        st, _ = await axi.read(STATUS)
        if not (st & S_DUT_QUIET):
            return
    raise AssertionError("the KVM never observed the DUT as busy across the tunnel")


def _state(status):
    return (status & S_STATE_MASK) >> S_STATE_SHIFT


def _wellformed(strobes, what, wr_lo=None, cs_setup=None, tol=0):
    """Every strobe that reached the panel must be a WHOLE, well-formed 8080
    write cycle. A cutover mid-phase necessarily shortens exactly one of the
    intervals below, so the width checks are the sharp end of the detector."""
    for i, s in enumerate(strobes):
        assert s.cs_low_throughout, (
            f"{what}: cycle {i} ({s!r}) — CS_n was not asserted across the whole "
            "WR pulse. The bus cycle was truncated by a handover.")
        assert s.pd_stable, (
            f"{what}: cycle {i} ({s!r}) — the data bus changed DURING the WR low "
            "pulse. The panel latched a byte nobody sent (a torn vector, or a "
            "cutover mid-strobe).")
        assert s.rd_high, f"{what}: cycle {i} — RD asserted on a write (READ_PATH=0)"
        assert s.oe_high is not False, f"{what}: cycle {i} — clcd_pd_oe was low"
        assert s.cs_setup_cycles is not None and s.cs_setup_cycles >= 1, (
            f"{what}: cycle {i} ({s!r}) — WR fell in the same cycle CS did (no "
            "setup). The pads were connected to a source that was ALREADY "
            "mid-cycle: the INCOMING quiescence gate (S_GRANT) is not working.")
        if wr_lo is not None:
            assert abs(s.wr_low_cycles - wr_lo) <= tol, (
                f"{what}: cycle {i} ({s!r}) — WR-low width {s.wr_low_cycles} "
                f"aclk cycles, source drives {wr_lo} (tol {tol}). A short pulse "
                "means the cycle was cut off mid-strobe.")
        if cs_setup is not None:
            assert abs(s.cs_setup_cycles - cs_setup) <= tol, (
                f"{what}: cycle {i} ({s!r}) — CS-setup {s.cs_setup_cycles} aclk "
                f"cycles, source drives {cs_setup} (tol {tol}).")


def _seq(strobes):
    return [(s.rs, s.byte) for s in strobes]


# DUT phase widths, converted to s_axi_aclk cycles for the panel model's
# measurements. 8 dut_clk cycles @ 50 MHz = 160 ns = 16 shell cycles. Tolerance
# 2 covers the CDC's sampling quantisation + the <=2-cycle stability filter; it
# is far tighter than any real truncation (which removes whole phases).
def _dut_wr_lo_cyc(d, aclk_ns=10.0):
    return round(d.wr_lo * d.period_ns / aclk_ns)


def _dut_cs_setup_cyc(d, aclk_ns=10.0):
    return round(d.cs_setup * d.period_ns / aclk_ns)


# A truncation removes a whole 8080 phase (>=8 dut_clk = ~16 aclk cycles); the
# CDC's sampling quantisation + the <=2-cycle stability filter perturb a measured
# width by at most a couple of aclk cycles. 3 sits comfortably between the two.
DUT_TOL = 3


# ============================================================================ #
# 1. CSR — reset values, RW, W1P readback, byte strobes, W1C
# ============================================================================ #
@cocotb.test(skip=NO_RTL)
async def test_csr_reset_values(dut):
    """Every reset value in README §5. The two that are `1` at reset are the two
    that MATTER: timeout_en (a KVM must not be able to hang on a dead input) and
    pb_en (press the button and it works, with no firmware at all)."""
    axi, h, d, panel, inv = await _bring_up(dut)

    ctrl, _ = await axi.read(CTRL)
    assert ctrl == CTRL_RESET, (
        f"CTRL reset {ctrl:#010x}, expected {CTRL_RESET:#010x} "
        "(timeout_en=1, pb_en=1, everything else 0)")
    assert ctrl & C_TIMEOUT_EN, "CTRL.timeout_en MUST reset to 1 (README §5)"
    assert ctrl & C_PB_EN, "CTRL.pb_en MUST reset to 1 (README §5)"
    assert not (ctrl & C_DUT_REQ_EN), (
        "CTRL.dut_req_en MUST reset to 0: an unprovisioned RM must not be able "
        "to grab the panel at power-on")
    assert not (ctrl & C_BL_RST_SRC), (
        "CTRL.bl_rst_src MUST reset to 0 (= follow clcd_0), or the shipped "
        "firmware leaves the panel dark and held in reset on the new bitstream")

    ev, _ = await axi.read(EVENT)
    assert ev == 0, f"EVENT reset {ev:#010x}, expected 0"

    pt, _ = await axi.read(PANEL_TMR)
    assert pt == PANEL_TMR_RESET, (
        f"PANEL_TMR reset {pt:#010x}, expected {PANEL_TMR_RESET:#010x} "
        "(rst_us=2000, settle_us=5000)")
    to, _ = await axi.read(TIMEOUT)
    assert to == TIMEOUT_RESET, f"TIMEOUT reset {to}, expected {TIMEOUT_RESET} µs"
    db, _ = await axi.read(DEBOUNCE)
    assert db == DEBOUNCE_RESET, f"DEBOUNCE reset {db}, expected {DEBOUNCE_RESET} µs"

    st, _ = await axi.read(STATUS)
    assert not (st & S_OWNER), "the HARNESS owns the panel at reset"
    assert not (st & S_SWITCH_PENDING)
    assert _state(st) == ST_OWN, f"FSM must reset into S_OWN, got state {_state(st)}"
    assert st & S_HARNESS_QUIET, "an idle clcd_0 must read as quiescent"
    assert st & S_DUT_QUIET, (
        "an all-zero tunnel (a decoupled or display-less RM) must read as "
        "QUIESCENT — that is the by-construction half of the active-high "
        "encoding (dut-display-tunnel.md §3)")
    assert not (st & S_INTERLOCK)
    assert int(dut.owner_o.value) == OWNER_HARNESS
    inv.check()


@cocotb.test(skip=NO_RTL)
async def test_csr_rw_w1p_and_byte_strobes(dut):
    """The RW fields hold; the three W1P bits (panel_rst_pulse, force_switch,
    src_sel_we) read back 0 and are never stored; byte strobes are honoured."""
    axi, h, d, panel, inv = await _bring_up(dut)

    await axi.write(PANEL_TMR, 0x0003_0002)
    v, _ = await axi.read(PANEL_TMR)
    assert v == 0x0003_0002, f"PANEL_TMR RW failed: {v:#010x}"
    await axi.write(TIMEOUT, 0x0000_1234)
    v, _ = await axi.read(TIMEOUT)
    assert v == 0x1234
    await axi.write(DEBOUNCE, 0x0000_0007)
    v, _ = await axi.read(DEBOUNCE)
    assert (v & 0xFFFF) == 7

    # W1P: written 1, must read back 0 (never stored).
    await axi.write(CTRL, CTRL_RESET | C_PANEL_RST_PULSE | C_FORCE_SWITCH | C_SRC_SEL_WE)
    await ClockCycles(dut.s_axi_aclk, 2)
    v, _ = await axi.read(CTRL)
    assert not (v & C_PANEL_RST_PULSE), "CTRL.panel_rst_pulse is W1P — must read 0"
    assert not (v & C_FORCE_SWITCH), "CTRL.force_switch is W1P — must read 0"
    assert not (v & C_SRC_SEL_WE), "CTRL.src_sel_we is W1P — must read 0"

    # Let that panel_rst_pulse sequence finish before poking CTRL again.
    await _timers(axi)
    await axi.write(CTRL, CTRL_RESET)
    await ClockCycles(dut.s_axi_aclk, 20)

    # Byte strobes (the clcd.sv:263-274 idiom): wstrb[0] gates [7:0]. A write
    # with wstrb[0]=0 cannot change backlight / panel_rst_n / src_sel.
    await axi.write(CTRL, CTRL_RESET | C_BACKLIGHT)
    v, _ = await axi.read(CTRL)
    assert v & C_BACKLIGHT
    await axi.write_bytes(CTRL, 0x0000_0000, strb=0b1110)   # lanes 1..3 only
    v, _ = await axi.read(CTRL)
    assert v & C_BACKLIGHT, (
        "a write with wstrb[0]=0 cleared CTRL[7:0] — byte strobes are not "
        "honoured; a byte-wise firmware write would silently rewrite the "
        "backlight and the ownership request")
    inv.check()


@cocotb.test(skip=NO_RTL)
async def test_event_is_w1c_and_never_read_to_clear(dut):
    """EVENT is RW1C: write 1 clears, write 0 leaves set, and a READ clears
    NOTHING. The platform dumps whole CSR pages over SWD/XVC — a read-to-clear
    EVENT here would silently destroy handover events, and the firmware would
    never learn it had regained the panel."""
    axi, h, d, panel, inv = await _bring_up(dut)
    await _timers(axi)

    # Provoke a real handover so several EVENT bits are genuinely set.
    await _req_owner(axi, OWNER_DUT)
    await _wait_owner(dut, OWNER_DUT)
    await ClockCycles(dut.s_axi_aclk, 10)

    ev, _ = await axi.read(EVENT)
    assert ev & E_DUT_GAINED, "EVENT.dut_gained must set on a commit into DUT"
    assert ev & E_HARNESS_LOST, "EVENT.harness_lost must set on a commit out of HARNESS"
    assert ev & E_PANEL_RESET_DONE, (
        "EVENT.panel_reset_done must set on EVERY completed auto panel-reset "
        "sequence — it is half of the firmware's repaint rule (README §5)")

    # A read does not clear.
    again, _ = await axi.read(EVENT)
    assert again == ev, (
        f"EVENT changed across a pure READ ({ev:#010x} -> {again:#010x}): it is "
        "read-to-clear. It must be W1C — a CSR page dump would eat the events.")

    # Writing 0 to a set bit leaves it set.
    await axi.write(EVENT, 0)
    v, _ = await axi.read(EVENT)
    assert v == ev, f"writing 0 to EVENT cleared bits ({ev:#010x} -> {v:#010x})"

    # Writing 1 clears exactly that bit and no other.
    await axi.write(EVENT, E_DUT_GAINED)
    v, _ = await axi.read(EVENT)
    assert not (v & E_DUT_GAINED), "W1C did not clear EVENT.dut_gained"
    assert v == (ev & ~E_DUT_GAINED), (
        f"W1C of one bit disturbed the others: {ev:#010x} -> {v:#010x}")

    await axi.write(EVENT, E_ALL)
    v, _ = await axi.read(EVENT)
    assert v == 0, f"W1C of all bits left {v:#010x}"
    inv.check()


@cocotb.test(skip=NO_RTL)
async def test_ctrl_rmw_does_not_clobber_a_concurrent_button_press(dut):
    """`CTRL.src_sel_we` — the whole reason it exists.

    Firmware does a read-modify-write of CTRL to turn the backlight on. If
    src_sel were a plain RW bit, that RMW would write back the STALE src_sel it
    read a microsecond ago and silently cancel an ownership change the user made
    with the button in between. With the write-enable, a plain RMW of CTRL can
    NEVER touch ownership. This test does exactly that race."""
    axi, h, d, panel, inv = await _bring_up(dut)
    await _timers(axi, debounce_us=2)

    # Firmware reads CTRL (src_sel = HARNESS at this instant).
    stale, _ = await axi.read(CTRL)
    assert not (stale & C_SRC_SEL)

    # ...the user presses the button. tgt_owner toggles to DUT in HARDWARE.
    dut.user_npb1.value = 0
    await ClockCycles(dut.s_axi_aclk, 4 * TICK_CYC)
    st, _ = await axi.read(STATUS)
    assert st & S_TGT_OWNER, "a debounced press must toggle tgt_owner to DUT"

    # ...and now firmware's RMW lands, writing back the STALE src_sel=HARNESS,
    # with NO src_sel_we. tgt_owner must NOT move.
    await axi.write(CTRL, stale | C_BACKLIGHT)
    await ClockCycles(dut.s_axi_aclk, 4)
    st, _ = await axi.read(STATUS)
    assert st & S_TGT_OWNER, (
        "a plain read-modify-write of CTRL (no src_sel_we) CLOBBERED the "
        "ownership request made by the button press. That is the exact bug "
        "CTRL[16] exists to prevent (README §5).")
    ctrl, _ = await axi.read(CTRL)
    assert ctrl & C_BACKLIGHT, "the RMW's real payload (backlight) must still land"
    assert ctrl & C_SRC_SEL, (
        "CTRL[0] must READ BACK the requested owner (tgt_owner), not the last "
        "value written")

    # And with src_sel_we, the write DOES land.
    await axi.write(CTRL, (ctrl & ~C_SRC_SEL) | C_SRC_SEL_WE)
    await ClockCycles(dut.s_axi_aclk, 4)
    st, _ = await axi.read(STATUS)
    assert not (st & S_TGT_OWNER), "a write WITH src_sel_we must update tgt_owner"
    inv.check()


# ============================================================================ #
# 2. NO READ SIDE EFFECTS — the platform sweeps every offset over SWD/XVC
# ============================================================================ #
@cocotb.test(skip=NO_RTL)
async def test_read_sweep_has_no_side_effect_anywhere_in_the_page(dut):
    """Read EVERY offset the block decodes and assert NOTHING moves.

    The shell has already shipped one destructive read (a read of DFXCTL 0x20
    popped a UART console byte through a decode alias). Here the blast radius is
    worse: a read that armed `panel_rst_pulse` would blank the screen every time
    the host dumped the CSR page, and a read-to-clear EVENT would eat the
    handover the firmware is waiting for.

    Swept with EVENT bits SET and the panel model armed, so a side effect is
    loud: no panel-bus activity, no EVENT change, no pad change, no owner change,
    no panel reset. (The 64 KiB page at the SHIPPED width 32 is swept by
    tests/csr_decode_width/test_decode_width_clcd_kvm.py; this bench elaborates
    the RTL default width 12, i.e. the 4 KiB the decode can see here.)"""
    axi, h, d, panel, inv = await _bring_up(dut)
    await _timers(axi)

    # Make the state INTERESTING first: a real handover leaves 3 EVENT bits set,
    # the DUT owning, and the panel out of reset. A read side effect now has
    # something to destroy.
    await _req_owner(axi, OWNER_DUT)
    await _wait_owner(dut, OWNER_DUT)
    await ClockCycles(dut.s_axi_aclk, 20)

    ev_before, _ = await axi.read(EVENT)
    assert ev_before != 0, "bench bug: EVENT should be non-zero before the sweep"
    ctrl_before, _ = await axi.read(CTRL)
    pads_before = (int(dut.clcd_bl_o.value), int(dut.clcd_rst_n_o.value))
    assert pads_before[1] == 1, "bench bug: the panel should be out of reset here"

    panel.reset_activity()
    panel.clear()
    saw_reset_before = inv.saw_panel_reset
    inv.saw_panel_reset = False

    # Sweep every word offset the width-12 decode covers (4 KiB), plus the
    # mapped ones twice for good measure.
    for off in list(range(0, 4096, 4)) + list(MAPPED):
        await axi.read(off)

    await ClockCycles(dut.s_axi_aclk, 10)

    assert panel.idle, (
        "a CSR READ drove the panel bus: any_cs_low=%s any_wr_low=%s "
        "any_rd_low=%s. A host CSR page-dump would corrupt the screen."
        % (panel.any_cs_low, panel.any_wr_low, panel.any_rd_low))
    assert len(panel.strobes) == 0, (
        f"{len(panel.strobes)} 8080 cycles were driven by a READ sweep: "
        f"{panel.sequence[:8]}")
    assert not inv.saw_panel_reset, (
        "a CSR READ pulsed CLCD_RST — an offset in this page is arming the "
        "panel-reset sequencer on a READ. Every action in this block must be "
        "armed by a WRITE (README §5).")

    ev_after, _ = await axi.read(EVENT)
    assert ev_after == ev_before, (
        f"the read sweep changed EVENT ({ev_before:#010x} -> {ev_after:#010x}). "
        "EVENT is W1C, NOT read-to-clear.")
    ctrl_after, _ = await axi.read(CTRL)
    assert ctrl_after == ctrl_before, (
        f"the read sweep changed CTRL ({ctrl_before:#010x} -> {ctrl_after:#010x})")
    assert (int(dut.clcd_bl_o.value), int(dut.clcd_rst_n_o.value)) == pads_before, (
        "the read sweep moved the BL/RST pads")
    assert int(dut.owner_o.value) == OWNER_DUT, "the read sweep changed the owner"
    inv.saw_panel_reset = saw_reset_before
    inv.check()


@cocotb.test(skip=NO_RTL)
async def test_unmapped_offsets_read_zero_and_writes_are_inert(dut):
    """Unmapped offsets in the page: reads return 0, writes are accepted
    (BRESP=OKAY) with no effect. A too-narrow decode would let BASE+0x20 alias
    CTRL — and a register sweep would then toggle the panel pads."""
    axi, h, d, panel, inv = await _bring_up(dut)

    await axi.write(CTRL, CTRL_RESET | C_BACKLIGHT)
    known, _ = await axi.read(CTRL)

    for off in (0x1C, 0x20, 0x40, 0x100, 0x800, 0xFFC):
        v, resp = await axi.read(off)
        assert v == 0, f"unmapped offset {off:#x} read {v:#010x}, must read 0"
        assert resp == 0, f"unmapped read {off:#x} returned BRESP {resp}"
        resp = await axi.write(off, 0xFFFF_FFFF)
        assert resp == 0, f"unmapped write {off:#x} returned BRESP {resp} (must be OKAY)"

    await ClockCycles(dut.s_axi_aclk, 4)
    v, _ = await axi.read(CTRL)
    assert v == known, (
        f"a write to an unmapped offset aliased into CTRL ({known:#010x} -> "
        f"{v:#010x}): a CSR sweep would move the panel pads")
    ev, _ = await axi.read(EVENT)
    assert ev == 0, f"a write to an unmapped offset set EVENT bits ({ev:#010x})"
    inv.check()


# ============================================================================ #
# 3. DEBOUNCE — a bouncing edge produces EXACTLY one toggle
# ============================================================================ #
@cocotb.test(skip=NO_RTL)
async def test_bouncing_button_produces_exactly_one_toggle(dut):
    """A real tactile switch bounces for 1-5 ms. `USER_nPB1` is a raw, async,
    human-driven pad with no upstream register: 3-FF sync, then INTEGRATE (the
    counter reloads on any bounce back to the old level), then toggle on the
    PRESS edge only (README §11).

    The bounce burst below contains an EVEN number of press-edges. That makes
    the test a sharp discriminator rather than a fuzzy one:
      * correct debounce  -> ONE toggle       -> tgt_owner = DUT
      * no debounce       -> SIX toggles      -> tgt_owner = HARNESS (back where
                                                  it started) -> RED
      * toggles on release too -> TWO toggles -> tgt_owner = HARNESS -> RED
    An odd/even argument beats an "approximately one" assertion."""
    axi, h, d, panel, inv = await _bring_up(dut)
    debounce_us = 3
    await _timers(axi, debounce_us=debounce_us)
    await axi.write(EVENT, E_ALL)                       # clear the slate

    st, _ = await axi.read(STATUS)
    assert not (st & S_TGT_OWNER) and not (st & S_PB_LEVEL)

    # A bounce burst: 6 falling (press) edges, each far shorter than the 3 µs
    # debounce window, then a settled press.
    for _ in range(6):
        dut.user_npb1.value = 0
        await ClockCycles(dut.s_axi_aclk, 11)           # 110 ns — pure bounce
        dut.user_npb1.value = 1
        await ClockCycles(dut.s_axi_aclk, 7)
    dut.user_npb1.value = 0                             # settled: PRESSED
    await ClockCycles(dut.s_axi_aclk, (debounce_us + 2) * TICK_CYC)

    st, _ = await axi.read(STATUS)
    assert st & S_PB_LEVEL, "pb_level must follow the settled press"
    assert st & S_TGT_OWNER, (
        "after a bouncing press, tgt_owner is back at HARNESS: the bounce "
        "produced an EVEN number of toggles. The debounce is not integrating "
        "(README §11) — every bounce edge is being accepted as a press.")
    ev, _ = await axi.read(EVENT)
    assert ev & E_PB_TOGGLE, "EVENT.pb_toggle must be set by an accepted press"

    # ...and the RELEASE does nothing (README §11 step 4: press only).
    await axi.write(EVENT, E_ALL)
    dut.user_npb1.value = 1
    await ClockCycles(dut.s_axi_aclk, (debounce_us + 2) * TICK_CYC)
    st, _ = await axi.read(STATUS)
    assert st & S_TGT_OWNER, (
        "releasing the button toggled ownership back. Release must do NOTHING "
        "— only a PRESS edge is a request (README §11).")
    ev, _ = await axi.read(EVENT)
    assert not (ev & E_PB_TOGGLE), "a RELEASE set EVENT.pb_toggle"

    # The handover the press asked for really did happen, in hardware, with no
    # firmware involvement beyond programming the timers.
    await _wait_owner(dut, OWNER_DUT, what="(after a debounced press)")
    inv.check()


@cocotb.test(skip=NO_RTL)
async def test_short_glitch_is_rejected_and_pb_en_gates_the_button(dut):
    """A glitch shorter than DEBOUNCE produces ZERO toggles; and CTRL.pb_en=0
    disables the button entirely (the one thing that can)."""
    axi, h, d, panel, inv = await _bring_up(dut)
    debounce_us = 5
    await _timers(axi, debounce_us=debounce_us)
    await axi.write(EVENT, E_ALL)

    dut.user_npb1.value = 0
    await ClockCycles(dut.s_axi_aclk, 2 * TICK_CYC)     # 2 µs < 5 µs debounce
    dut.user_npb1.value = 1
    await ClockCycles(dut.s_axi_aclk, (debounce_us + 2) * TICK_CYC)

    ev, _ = await axi.read(EVENT)
    assert not (ev & E_PB_TOGGLE), (
        "a 2 µs glitch on a 5 µs debounce was accepted as a press")
    st, _ = await axi.read(STATUS)
    assert not (st & S_TGT_OWNER), "a rejected glitch moved tgt_owner"
    assert not (st & S_PB_LEVEL), "a rejected glitch moved pb_level"

    # pb_en = 0: a real, settled press is ignored.
    await axi.write(CTRL, CTRL_RESET & ~C_PB_EN)
    dut.user_npb1.value = 0
    await ClockCycles(dut.s_axi_aclk, (debounce_us + 3) * TICK_CYC)
    st, _ = await axi.read(STATUS)
    assert st & S_PB_LEVEL, (
        "pb_level must STILL track the debounced pad with pb_en=0 — pb_en gates "
        "the REQUEST, not the debouncer (STATUS[11] is a diagnostic)")
    assert not (st & S_TGT_OWNER), "with CTRL.pb_en=0 a press must not request a switch"
    inv.check()


# ============================================================================ #
# 4. THE HEADLINE — no truncated bus cycle across a switch, either direction,
#    at every phase of the 8080 cycle.
# ============================================================================ #
@cocotb.test(skip=NO_RTL)
async def test_no_truncated_cycle_harness_to_dut_at_every_phase(dut):
    """HARNESS -> DUT, requested MID-BURST, with the request landing at every
    phase of the harness's 8080 cycle in turn.

    The outgoing gate (S_DRAIN) must let the harness finish EVERYTHING it has in
    flight AND in its FIFO — bytes stranded in the FIFO across a handover would
    be emitted on the way back, out of sequence, into a panel that has since been
    reset (README §6). So the property is exact and total:

        every byte the harness queued reaches the panel, whole and in order,
        BEFORE any DUT byte does, and the panel reset that separates the two
        owners never lands inside a bus cycle.
    """
    axi, h, d, panel, inv = await _bring_up(dut)
    await _timers(axi, rst_us=1, settle_us=1, timeout_us=60)

    period = h.period          # cs_setup + wr_lo + wr_hi + 1 = 11 aclk cycles
    for k in range(period):    # sweep the request across EVERY phase
        # --- back to a known state: HARNESS owns, both sides idle -----------
        if int(dut.owner_o.value) != OWNER_HARNESS:
            await _req_owner(axi, OWNER_HARNESS)
            await _wait_owner(dut, OWNER_HARNESS)
        await axi.write(EVENT, E_ALL)
        await h.wait_idle()
        await d.wait_idle()
        await ClockCycles(dut.s_axi_aclk, 5)
        panel.clear()
        panel.reset_activity()
        h.clear_emitted()
        d.clear_emitted()

        # --- the harness is MID-BURST: 5 bytes, one in flight, four queued ---
        stream = _h_bytes(5, first=0x10 + 8 * k)
        h.enqueue(stream)
        await h.wait_phase(H_SETUP)          # align to a known phase...
        await ClockCycles(dut.s_axi_aclk, k) # ...then slide the request by k
        await _req_owner(axi, OWNER_DUT)

        # --- the switch --------------------------------------------------- #
        await _wait_owner(dut, OWNER_DUT, what=f"(k={k})")
        await ClockCycles(dut.s_axi_aclk, 5)

        got = _seq(panel.strobes)
        assert got == stream, (
            f"k={k} (request landed {k} cycles into the harness's cycle): the "
            f"harness queued {len(stream)} bytes and the panel saw {len(got)}.\n"
            f"  queued : {stream}\n  panel  : {got}\n"
            "The outgoing gate (S_DRAIN) did not wait for h_fifo_empty && "
            "!h_busy — bytes were CUT OFF by the handover.")
        assert h.emitted == stream, "bench bug: the harness model did not emit its queue"
        _wellformed(panel.strobes, f"harness->DUT k={k}",
                    wr_lo=h.wr_lo, cs_setup=h.cs_setup, tol=0)

        # --- and the DUT really has it now --------------------------------- #
        panel.clear()
        dstream = _d_bytes(3, first=0x80)
        d.enqueue(dstream)
        await d.wait_idle()
        await ClockCycles(dut.s_axi_aclk, 10)
        got = _seq(panel.strobes)
        assert got == dstream, (
            f"k={k}: after the handover the DUT's bytes must reach the panel.\n"
            f"  sent : {dstream}\n  panel: {got}")
        _wellformed(panel.strobes, f"DUT after k={k}",
                    wr_lo=_dut_wr_lo_cyc(d), cs_setup=_dut_cs_setup_cyc(d),
                    tol=DUT_TOL)

        ev, _ = await axi.read(EVENT)
        assert ev & E_DUT_GAINED and ev & E_HARNESS_LOST, (
            f"k={k}: EVENT must record the handover (got {ev:#010x})")
        assert ev & E_PANEL_RESET_DONE, (
            f"k={k}: every handover hard-resets the panel and must say so")
        assert not (ev & E_TIMEOUT_FIRED), (
            f"k={k}: the drain TIMEOUT fired on a healthy source — the gate is "
            "waiting for something that never comes")

    assert inv.saw_panel_reset, "no handover ever reset the panel"
    inv.check()
    dut._log.info(f"[kvm] harness->DUT clean at all {period} cycle phases")


@cocotb.test(skip=NO_RTL)
async def test_no_truncated_cycle_dut_to_harness_at_every_phase(dut):
    """DUT -> HARNESS, requested MID-BURST, sliding the request across the DUT's
    (asynchronous, much slower) 8080 cycle. The mirror image of the test above —
    and the direction that matters most on the board, because it is the one the
    user takes when the DUT has wedged the screen."""
    axi, h, d, panel, inv = await _bring_up(dut)
    await _timers(axi, rst_us=1, settle_us=1, timeout_us=200)

    step = 3
    for k in range(0, d.period, step):    # 25 dut_clk cycles -> 9 offsets
        if int(dut.owner_o.value) != OWNER_DUT:
            await _req_owner(axi, OWNER_DUT)
            await _wait_owner(dut, OWNER_DUT)
        await axi.write(EVENT, E_ALL)
        await h.wait_idle()
        await d.wait_idle()
        await ClockCycles(dut.s_axi_aclk, 5)
        panel.clear()
        h.clear_emitted()
        d.clear_emitted()

        stream = _d_bytes(4, first=0x80 + 8 * (k // step))
        d.enqueue(stream)
        await d.wait_phase(H_SETUP)
        # The DUT's quiescence lags the harness's: its busy/cs cross the tunnel
        # CDC (2-FF sync + 2-cycle filter, ~4-6 shell cycles). Wait until the KVM
        # can actually SEE the DUT busy before asking for the switch — otherwise
        # the drain gate legitimately reads a stale-quiescent tunnel and cuts
        # over before the DUT ever appeared busy (a bench race, not an RTL bug;
        # on the board the DUT has been painting for milliseconds when you press
        # the button). This is the DUT-side analogue of the harness being
        # synchronously, immediately visible.
        await _wait_tunnel_busy(dut, axi)
        if k:                                # Timer(0) is illegal; k=0 = no slide
            await Timer(k * d.period_ns, units="ns")
        await _req_owner(axi, OWNER_HARNESS)

        await _wait_owner(dut, OWNER_HARNESS, what=f"(k={k})")
        await ClockCycles(dut.s_axi_aclk, 5)

        got = _seq(panel.strobes)
        assert got == stream, (
            f"k={k} dut_clk cycles into the DUT's cycle: the DUT queued "
            f"{len(stream)} bytes, the panel saw {len(got)}.\n"
            f"  queued : {stream}\n  panel  : {got}\n"
            "The outgoing gate did not wait for the tunnel's !cs && !busy.")
        _wellformed(panel.strobes, f"DUT->harness k={k}",
                    wr_lo=_dut_wr_lo_cyc(d), cs_setup=_dut_cs_setup_cyc(d),
                    tol=DUT_TOL)

        panel.clear()
        hstream = _h_bytes(3, first=0x30)
        h.enqueue(hstream)
        await h.wait_idle()
        await ClockCycles(dut.s_axi_aclk, 10)
        assert _seq(panel.strobes) == hstream, (
            f"k={k}: after the handover the harness's bytes must reach the panel")
        _wellformed(panel.strobes, f"harness after k={k}",
                    wr_lo=h.wr_lo, cs_setup=h.cs_setup, tol=0)

        ev, _ = await axi.read(EVENT)
        assert ev & E_HARNESS_GAINED and ev & E_DUT_LOST, (
            f"k={k}: EVENT must record the handover (got {ev:#010x})")
        assert not (ev & E_TIMEOUT_FIRED), f"k={k}: the drain timeout fired needlessly"

    inv.check()
    dut._log.info("[kvm] DUT->harness clean at every phase of the async DUT cycle")


@cocotb.test(skip=NO_RTL)
async def test_incoming_gate_waits_for_the_dut_to_go_quiescent(dut):
    """THE TWO-SIDED GATE — the half a naive KVM gets wrong.

    The DUT is *freewheeling*: it is driving 8080 cycles at a panel it does not
    own (perfectly legal — the KVM discards them; see dut-display-tunnel.md §6:
    the DUT is never even told that it owns the panel). We now grant it the
    panel WHILE IT IS MID-BURST.

    A KVM with only the outgoing (S_DRAIN) gate connects the pads the instant the
    harness has drained — landing in the middle of one of the DUT's cycles, with
    CS asserting mid-strobe and no setup. The panel latches a byte that was never
    driven at it.

    With the incoming (S_GRANT) gate, the KVM holds the pads itself until the
    DUT is quiescent, and the first byte the panel sees is a WHOLE cycle. The
    detector: not one freewheel byte reaches the panel, and every DUT strobe has
    the source's full CS-setup and WR-low widths."""
    axi, h, d, panel, inv = await _bring_up(dut)
    # The S_GRANT wait must be allowed to outlast the freewheel burst, or the
    # timeout would preempt the very gate under test. 300 µs >> 20 µs burst.
    await _timers(axi, rst_us=1, settle_us=1, timeout_us=300)

    # 40 bytes @ ~500 ns = ~20 µs of continuous DUT activity: far longer than
    # drain + rst + settle, so the DUT is GUARANTEED to still be mid-burst when
    # the KVM reaches S_GRANT.
    freewheel = _d_freewheel(40)
    d.enqueue(freewheel)
    await d.wait_phase(H_STROBE_LO)

    panel.clear()
    panel.reset_activity()
    await _req_owner(axi, OWNER_DUT)

    # Confirm the premise — the KVM really did enter S_GRANT with the DUT busy.
    saw_grant_while_busy = False
    for _ in range(4000):
        await ClockCycles(dut.s_axi_aclk, 5)
        st, _ = await axi.read(STATUS)
        if (st & S_GRANTING) and not (st & S_DUT_QUIET):
            saw_grant_while_busy = True
            break
        if int(dut.owner_o.value) == OWNER_DUT:
            break
    assert saw_grant_while_busy, (
        "bench premise failed: the KVM never sat in S_GRANT while the DUT was "
        "busy, so this test could not have exercised the incoming gate. Either "
        "the DUT model drained too fast, or S_GRANT does not exist.")

    await _wait_owner(dut, OWNER_DUT, timeout_cycles=60000)
    await ClockCycles(dut.s_axi_aclk, 10)

    # Not ONE freewheel byte may have reached the panel: the KVM held the pads
    # itself throughout, and by the time it let go the DUT was between bytes.
    got = _seq(panel.strobes)
    assert got == [], (
        f"{len(got)} bytes reached the panel from a DUT that did not own it "
        f"(and/or from mid-cycle at the grant): {got[:8]}.\n"
        "The INCOMING quiescence gate (S_GRANT) is not holding — the pads were "
        "connected to a source that was already mid-cycle.")

    # Now it owns the panel, and its first WHOLE cycle must be well-formed.
    stream = _d_bytes(4)
    d.enqueue(stream)
    await d.wait_idle()
    await ClockCycles(dut.s_axi_aclk, 10)
    assert _seq(panel.strobes) == stream, (
        f"after the grant the DUT's bytes must reach the panel: {panel.sequence}")
    _wellformed(panel.strobes, "DUT's first cycles after the grant",
                wr_lo=_dut_wr_lo_cyc(d), cs_setup=_dut_cs_setup_cyc(d), tol=DUT_TOL)
    inv.check()


@cocotb.test(skip=NO_RTL)
async def test_incoming_gate_waits_for_the_harness_to_go_quiescent(dut):
    """The same two-sided property, with the roles swapped: the DUT owns the
    panel, and the HARNESS is mid-burst (its FIFO full of bytes it is pushing at
    a panel it does not own) when we hand it back. Not one of those bytes may
    appear at the panel before a whole, well-formed cycle does."""
    axi, h, d, panel, inv = await _bring_up(dut)
    await _timers(axi, rst_us=1, settle_us=1, timeout_us=300)

    await _req_owner(axi, OWNER_DUT)
    await _wait_owner(dut, OWNER_DUT)
    await ClockCycles(dut.s_axi_aclk, 10)

    # The harness keeps streaming into a panel it does not own (the real
    # firmware's 250 ms refresh loop does exactly this). 60 bytes @ 11 cycles =
    # 660 cycles = 6.6 µs, comfortably past drain + rst(1 µs) + settle(1 µs).
    freewheel = [(RS_DATA, 0xC0 + (i & 0x1F)) for i in range(60)]
    h.enqueue(freewheel)
    await h.wait_phase(H_STROBE_LO)

    panel.clear()
    await _req_owner(axi, OWNER_HARNESS)

    saw_grant_while_busy = False
    for _ in range(4000):
        await ClockCycles(dut.s_axi_aclk, 3)
        st, _ = await axi.read(STATUS)
        if (st & S_GRANTING) and not (st & S_HARNESS_QUIET):
            saw_grant_while_busy = True
            break
        if int(dut.owner_o.value) == OWNER_HARNESS:
            break
    assert saw_grant_while_busy, (
        "bench premise failed: never observed S_GRANT with the harness busy")

    await _wait_owner(dut, OWNER_HARNESS, timeout_cycles=60000)
    await ClockCycles(dut.s_axi_aclk, 4)

    # The harness is still draining its freewheel queue — that is fine and
    # expected (it owns the panel now). What must NOT have happened is a partial
    # cycle at the seam. Every strobe the panel saw must be whole.
    _wellformed(panel.strobes, "harness's first cycles after the grant",
                wr_lo=h.wr_lo, cs_setup=h.cs_setup, tol=0)
    await h.wait_idle()
    inv.check()


@cocotb.test(skip=NO_RTL)
async def test_force_switch_truncates_on_purpose(dut):
    """POSITIVE CONTROL — prove the detector can actually FIRE.

    `CTRL.force_switch` (W1P) commits the pending switch immediately, skipping
    the outgoing drain gate. By contract (README §6) that is a DELIBERATE
    truncation: it is the escape hatch for a hung owner. So it is also the one
    lever that lets the bench provoke the exact failure it spends the rest of its
    life forbidding — WITHOUT touching a line of anyone's RTL.

    If this test cannot see a truncation, then `test_no_truncated_cycle_*` is
    green for the wrong reason and MEANS NOTHING."""
    axi, h, d, panel, inv = await _bring_up(dut)
    await _timers(axi, rst_us=1, settle_us=1, timeout_us=300)
    inv.enabled = False        # we are DELIBERATELY breaking the invariant here

    stream = _h_bytes(6, first=0x10)
    h.enqueue(stream)
    await h.wait_phase(H_STROBE_LO)          # mid-WR-pulse: the worst instant
    panel.clear()

    # Request + force, in one write. src_sel_we lands the request; force_switch
    # skips the S_DRAIN wait.
    await axi.write(CTRL, CTRL_RESET | OWNER_DUT | C_SRC_SEL_WE | C_FORCE_SWITCH)
    await _wait_owner(dut, OWNER_DUT, timeout_cycles=20000)
    await ClockCycles(dut.s_axi_aclk, 10)

    got = _seq(panel.strobes)
    truncated = (got != stream) or any(
        (not s.cs_low_throughout) or s.wr_low_cycles != h.wr_lo
        for s in panel.strobes)
    assert truncated, (
        "force_switch cut the harness off MID-STROBE and yet the panel saw a "
        f"complete, well-formed stream ({got}).\n"
        "THE DETECTOR IS BLIND. Every 'no truncated cycle' result in this file "
        "is therefore vacuous — fix the detector before trusting them.")
    dut._log.info(
        f"[kvm] positive control: force_switch truncated the stream as it must "
        f"(queued {len(stream)} bytes, panel saw {len(got)}) — the detector fires")

    ev, _ = await axi.read(EVENT)
    assert ev & E_DUT_GAINED, "force_switch must still commit the handover"


# ============================================================================ #
# 5. THE DFX INTERLOCK — forced revert (why this block lives in the STATIC shell)
# ============================================================================ #
async def _dut_owns(dut, axi, h, d, panel):
    await _req_owner(axi, OWNER_DUT)
    await _wait_owner(dut, OWNER_DUT)
    await ClockCycles(dut.s_axi_aclk, 10)
    await axi.write(EVENT, E_ALL)
    panel.clear()
    panel.reset_activity()


async def _assert_forced_revert(dut, axi, panel, inv, what):
    """Common tail: within 8 shell cycles the KVM must be driving the pads
    itself, and ZERO DUT-sourced bytes may reach the panel thereafter."""
    # README §9, NORMATIVE: within 8 s_axi_aclk cycles (80 ns) of the interlock
    # asserting, the KVM is driving the pads itself. Observe it on the pads:
    # CS and WR deasserted, and they stay that way.
    idle_at = None
    for i in range(64):
        await RisingEdge(dut.s_axi_aclk)
        await ReadOnly()
        if int(dut.clcd_cs_n_o.value) == 1 and int(dut.clcd_wr_n_o.value) == 1:
            idle_at = i
            break
    assert idle_at is not None and idle_at <= 8, (
        f"{what}: the pads were still being driven by the DUT {idle_at} cycles "
        "after the interlock asserted. README §9 requires the KVM to have taken "
        "the pads within 8 s_axi_aclk cycles (80 ns).")

    panel.clear()
    panel.reset_activity()
    await ClockCycles(dut.s_axi_aclk, 40)      # >> 8; the DUT is still hammering

    assert panel.idle, (
        f"{what}: the DUT reached the panel AFTER the forced revert "
        f"(any_cs_low={panel.any_cs_low} any_wr_low={panel.any_wr_low}). "
        "A partial reconfiguration would spray the screen.")
    assert len(panel.strobes) == 0, (
        f"{what}: {len(panel.strobes)} DUT bytes latched after the revert: "
        f"{panel.sequence[:8]}")

    await _wait_owner(dut, OWNER_HARNESS, timeout_cycles=40000, what=f"({what})")
    ev, _ = await axi.read(EVENT)
    assert ev & E_FORCED_REVERT, (
        f"{what}: EVENT.forced_revert must be set (got {ev:#010x})")
    st, _ = await axi.read(STATUS)
    assert st & S_INTERLOCK, f"{what}: STATUS.interlock must be asserted"
    assert not (st & S_TGT_OWNER), (
        f"{what}: the interlock must FORCE tgt_owner to HARNESS, not merely "
        "revert owner — otherwise the DUT is re-granted the moment it clears")


@cocotb.test(skip=NO_RTL)
async def test_forced_revert_on_decouple_status(dut):
    """`decouple_status` rises MID-BURST while the DUT owns the panel — i.e. a
    partial reconfiguration starts under it. The KVM must take the pads back
    within 8 cycles and never let the RP near them again.

    NOTE the bench is HARSHER than the board: it keeps the tunnel LIVE (strobes
    still asserting) while decouple_status is high. On real silicon the decoupler
    has already clamped those nets to 0x0, which is itself "all strobes idle" by
    construction (dut-display-tunnel.md §3). Driving them live proves the FSM's
    forced revert stands on its own, without leaning on the clamp — the two are
    independent lines of defence and BOTH are load-bearing (§9)."""
    axi, h, d, panel, inv = await _bring_up(dut)
    await _timers(axi, rst_us=1, settle_us=1, timeout_us=100)
    inv.enabled = False     # a forced revert MAY chop the in-flight cycle: that
                            # is correct (waiting for a clamped RP is exactly the
                            # wrong thing) and the invariant would flag it.

    await _dut_owns(dut, axi, h, d, panel)
    d.enqueue(_d_freewheel(60))
    await d.wait_phase(H_STROBE_LO)          # the worst instant: mid-WR-pulse

    dut.decouple_status.value = 1
    await _assert_forced_revert(dut, axi, panel, inv, "decouple_status")

    # The panel is reset under the harness, and the harness can drive again.
    d.hung = False
    d.queue.clear()
    await ClockCycles(dut.s_axi_aclk, 10)
    panel.clear()
    hs = _h_bytes(3, first=0x10)
    h.enqueue(hs)
    await h.wait_idle()
    await ClockCycles(dut.s_axi_aclk, 10)
    assert _seq(panel.strobes) == hs, (
        "after a forced revert the HARNESS must be able to drive the panel")

    # And while decoupled, the DUT cannot be granted the panel at all.
    await _req_owner(axi, OWNER_DUT)
    await ClockCycles(dut.s_axi_aclk, 2000)
    assert int(dut.owner_o.value) == OWNER_HARNESS, (
        "the DUT was granted the panel while the RP was DECOUPLED. Every "
        "DUT-requesting source must be MASKED under the interlock (README §9).")


@cocotb.test(skip=NO_RTL)
async def test_forced_revert_on_rp_resetn(dut):
    """The same, driven by the OTHER half of the interlock: `rp_resetn` drops.
    This covers the window where the RP is being reset but is not yet clamped —
    which is precisely why both terms are in the interlock and not just one."""
    axi, h, d, panel, inv = await _bring_up(dut)
    await _timers(axi, rst_us=1, settle_us=1, timeout_us=100)
    inv.enabled = False

    await _dut_owns(dut, axi, h, d, panel)
    d.enqueue(_d_freewheel(60))
    await d.wait_phase(H_STROBE_LO)

    dut.rp_resetn.value = 0
    await _assert_forced_revert(dut, axi, panel, inv, "rp_resetn low")

    st, _ = await axi.read(STATUS)
    assert st & S_RP_IN_RESET, "STATUS.rp_in_reset must report the synchronised !rp_resetn"


@cocotb.test(skip=NO_RTL)
async def test_decoupled_clamp_is_safe_by_construction(dut):
    """The OTHER half of §9 — the half that needs no logic at all.

    The dfx_decoupler clamps dut_gpio_o/oe to 0x0 (shell_bd.tcl:626, IDs 13/14,
    DECOUPLED_VALUE 0x0) for the whole duration of every partial reconfiguration.
    Because the tunnel carries strobes ACTIVE-HIGH, 0x0 decodes to "all strobes
    idle, nothing requested, nothing in flight" — with no special clamp value and
    no KVM logic. Under the "natural" active-low encoding the SAME clamp would
    hold CS, WR and RD asserted continuously while the RP's outputs are garbage.

    So: give the DUT the panel, then apply the REAL clamp (0x0) with
    decouple_status DEASSERTED — the FSM's forced revert is not helping here —
    and prove the panel sees nothing. Also prove the clamped tunnel reads as
    QUIESCENT (STATUS.dut_quiet), which is what stops the safe-switch gate from
    ever hanging on a dead RP."""
    axi, h, d, panel, inv = await _bring_up(dut)
    await _timers(axi, rst_us=1, settle_us=1, timeout_us=100)

    await _dut_owns(dut, axi, h, d, panel)
    d.enqueue(_d_bytes(4))
    await d.wait_idle()
    await ClockCycles(dut.s_axi_aclk, 5)
    assert len(panel.strobes) == 4, "bench premise: the DUT owns and can drive"

    # The clamp, with NO interlock signal. The KVM does not even know.
    d.stop()
    await Timer(d.period_ns * 2, units="ns")
    d.clamp()
    panel.clear()
    panel.reset_activity()
    await ClockCycles(dut.s_axi_aclk, 500)

    assert panel.idle and len(panel.strobes) == 0, (
        "a tunnel clamped to 0x0 reached the panel: the strobes are not being "
        "read as ACTIVE-HIGH. Inverting them re-introduces a panel-corrupting, "
        "silicon-only failure — see dut-display-tunnel.md §3, which begs you not "
        "to 'fix' this.")
    st, _ = await axi.read(STATUS)
    assert st & S_DUT_QUIET, (
        "a clamped (all-zero) tunnel must read as QUIESCENT, or the safe-switch "
        "gate can hang for ever on a dead RP")
    assert not (st & S_DUT_REQ), "a clamped tunnel must not read as requesting"
    tun, _ = await axi.read(TUNNEL)
    assert tun == 0, f"TUNNEL must snapshot the clamped tunnel as 0, got {tun:#010x}"
    inv.check()


@cocotb.test(skip=NO_RTL)
async def test_force_harness_is_the_software_twin_of_the_interlock(dut):
    """CTRL.force_harness: ownership pinned to HARNESS, all DUT-requesting
    sources ignored, any pending switch cancelled and reverted WITHOUT waiting
    for DUT quiescence."""
    axi, h, d, panel, inv = await _bring_up(dut)
    await _timers(axi, rst_us=1, settle_us=1, timeout_us=100)
    inv.enabled = False

    await _dut_owns(dut, axi, h, d, panel)
    d.enqueue(_d_freewheel(40))
    await d.wait_phase(H_STROBE_LO)

    await axi.write(CTRL, CTRL_RESET | C_FORCE_HARNESS)
    await _wait_owner(dut, OWNER_HARNESS, timeout_cycles=40000)
    ev, _ = await axi.read(EVENT)
    assert ev & E_FORCED_REVERT, "force_harness must set EVENT.forced_revert"
    st, _ = await axi.read(STATUS)
    assert st & S_INTERLOCK, "force_harness is part of the interlock term"

    # And it MASKS every DUT-requesting source while it is set.
    await _req_owner(axi, OWNER_DUT, base=CTRL_RESET | C_FORCE_HARNESS)
    dut.user_npb1.value = 0                      # a button press, too
    await ClockCycles(dut.s_axi_aclk, 3000)
    assert int(dut.owner_o.value) == OWNER_HARNESS, (
        "a switch to DUT was granted while CTRL.force_harness was set")
    st, _ = await axi.read(STATUS)
    assert not (st & S_TGT_OWNER), (
        "force_harness must FORCE tgt_owner to HARNESS and mask the sources, "
        "not merely block the commit")


# ============================================================================ #
# 6. THE HUNG-OWNER TIMEOUT — a KVM must not be able to hang on a dead input
# ============================================================================ #
@cocotb.test(skip=NO_RTL)
async def test_hung_outgoing_owner_is_preempted_after_timeout(dut):
    """S_DRAIN's timeout. The harness owns the panel and NEVER goes quiescent
    (its FIFO never empties — a wedged MicroBlaze, a stuck clcd_0 FSM). Without a
    timeout the KVM would sit in S_DRAIN for ever and the button would be dead.

    Non-vacuity matters here: the KVM must actually WAIT (this is a timeout, not
    an immediate switch), so the test asserts the handover took at least most of
    the programmed timeout, as well as completing."""
    axi, h, d, panel, inv = await _bring_up(dut)
    timeout_us = 20
    await _timers(axi, rst_us=1, settle_us=1, timeout_us=timeout_us)
    await axi.write(EVENT, E_ALL)

    h.hung = True                      # busy=1, fifo_empty=0, for ever
    await ClockCycles(dut.s_axi_aclk, 10)
    st, _ = await axi.read(STATUS)
    assert not (st & S_HARNESS_QUIET), "bench premise: the harness must look hung"

    await _req_owner(axi, OWNER_DUT)
    took = await _wait_owner(dut, OWNER_DUT, timeout_cycles=20 * timeout_us * TICK_CYC)

    expect = timeout_us * TICK_CYC
    assert took >= expect * 0.5, (
        f"the switch completed in {took} cycles but the outgoing owner was HUNG "
        f"and TIMEOUT is {timeout_us} µs ({expect} cycles). The KVM did not wait "
        "for quiescence at all — S_DRAIN's gate is missing.")
    ev, _ = await axi.read(EVENT)
    assert ev & E_TIMEOUT_FIRED, (
        "EVENT.timeout_fired must be set when a wait is preempted (README §6)")
    assert ev & E_DUT_GAINED, "on timeout the KVM must proceed ANYWAY"
    assert ev & E_PANEL_RESET_DONE, (
        "a preempted handover must STILL hard-reset the panel — the outgoing "
        "owner left the HX8347's command/parameter state machine mid-sequence")
    assert inv.saw_panel_reset, "the panel was never reset across the timeout handover"

    # timeout_en = 0 disarms it: the same hung owner now blocks the switch.
    h.hung = False
    await _req_owner(axi, OWNER_HARNESS)
    await _wait_owner(dut, OWNER_HARNESS)
    await axi.write(CTRL, (CTRL_RESET & ~C_TIMEOUT_EN))
    h.hung = True
    await axi.write(EVENT, E_ALL)
    await _req_owner(axi, OWNER_DUT, base=CTRL_RESET & ~C_TIMEOUT_EN)
    await ClockCycles(dut.s_axi_aclk, 4 * timeout_us * TICK_CYC)
    assert int(dut.owner_o.value) == OWNER_HARNESS, (
        "with CTRL.timeout_en=0 a hung owner must NOT be preempted (that is the "
        "debug-only choice the bit exists for) — the switch stayed pending")
    ev, _ = await axi.read(EVENT)
    assert not (ev & E_TIMEOUT_FIRED), "timeout fired with timeout_en=0"
    h.hung = False
    inv.check()


@cocotb.test(skip=NO_RTL)
async def test_hung_incoming_owner_is_preempted_after_timeout(dut):
    """S_GRANT's timeout — the one a one-sided implementation forgets. The
    INCOMING owner never declares itself quiescent (the DUT's tunnel `busy` bit
    is stuck high: a garbage RM, or an accelerator wedged mid-frame). The KVM
    must commit anyway rather than park in S_GRANT for ever with the panel dark
    and NEITHER source connected — which would be the worst outcome of all."""
    axi, h, d, panel, inv = await _bring_up(dut)
    timeout_us = 20
    await _timers(axi, rst_us=1, settle_us=1, timeout_us=timeout_us)
    await axi.write(EVENT, E_ALL)

    d.hung = True                      # tunnel busy stuck 1, strobes idle
    await ClockCycles(dut.s_axi_aclk, 20)
    st, _ = await axi.read(STATUS)
    assert not (st & S_DUT_QUIET), "bench premise: the DUT must look hung"

    await _req_owner(axi, OWNER_DUT)
    took = await _wait_owner(dut, OWNER_DUT, timeout_cycles=20 * timeout_us * TICK_CYC)
    expect = timeout_us * TICK_CYC
    assert took >= expect * 0.5, (
        f"the grant completed in {took} cycles with the INCOMING owner hung and "
        f"TIMEOUT = {expect} cycles. S_GRANT is not gating on tgt_owner_quiet — "
        "the incoming half of the two-sided gate is missing.")
    ev, _ = await axi.read(EVENT)
    assert ev & E_TIMEOUT_FIRED, "EVENT.timeout_fired must be set for a S_GRANT timeout"
    assert ev & E_DUT_GAINED
    d.hung = False
    inv.check()


# ============================================================================ #
# 7. THE PANEL RESET SEQUENCER — one sequencer, two callers
# ============================================================================ #
@cocotb.test(skip=NO_RTL)
async def test_panel_rst_pulse_resets_the_panel_without_changing_owner(dut):
    """CTRL.panel_rst_pulse (W1P): one full S_RST -> S_SETTLE -> S_GRANT
    sequence, no owner change, and EVENT.panel_reset_done still fires — which is
    exactly why the firmware rule is `harness_gained | panel_reset_done` and not
    `harness_gained` alone (README §5/§7). Firmware's panel-recovery lever."""
    axi, h, d, panel, inv = await _bring_up(dut)
    rst_us, settle_us = 2, 2
    await _timers(axi, rst_us=rst_us, settle_us=settle_us)
    await axi.write(EVENT, E_ALL)
    inv.saw_panel_reset = False

    assert int(dut.clcd_rst_n_o.value) == 1, (
        "bench premise: bl_rst_src=0 -> RST follows clcd_0's CTRL[2], driven 1")

    await axi.write(CTRL, CTRL_RESET | C_PANEL_RST_PULSE)

    # RST must go low, for about rst_us, then release; the KVM drives the idle
    # pattern throughout, and BL is forced off while RST is low.
    low_cycles = 0
    saw_low = False
    for _ in range(4 * (rst_us + settle_us) * TICK_CYC):
        await RisingEdge(dut.s_axi_aclk)
        await ReadOnly()
        if int(dut.clcd_rst_n_o.value) == 0:
            saw_low = True
            low_cycles += 1
            assert int(dut.clcd_bl_o.value) == 0, (
                "CLCD_BL must be forced OFF whenever the panel is in reset")
            assert int(dut.clcd_cs_n_o.value) == 1, "the KVM's idle pattern is CS=1"
            assert int(dut.clcd_wr_n_o.value) == 1, "the KVM's idle pattern is WR=1"
            assert int(dut.clcd_pd_oe.value) == 1, "never float the bus at the panel"
        elif saw_low:
            break
    assert saw_low, "CTRL.panel_rst_pulse did not pulse CLCD_RST low at all"
    # The pulse is timed in whole 1 µs TICKS against a FREE-RUNNING tick divider,
    # so where S_RST is entered relative to the next tick edge adds up to one full
    # tick period of jitter: a rst_us-µs pulse lasts anywhere in
    # ((rst_us-1)*TICK_CYC, rst_us*TICK_CYC]. That is a real, inherent property of
    # the timebase (README §5's 1 µs tick), not slop — assert to one tick.
    assert abs(low_cycles - rst_us * TICK_CYC) <= TICK_CYC + 3, (
        f"CLCD_RST was low for {low_cycles} cycles; PANEL_TMR.rst_us={rst_us} µs "
        f"at TICK_DIV={TICK_CYC} is ~{rst_us * TICK_CYC} (+/- one tick of jitter)")

    await ClockCycles(dut.s_axi_aclk, (settle_us + 2) * TICK_CYC)
    ev, _ = await axi.read(EVENT)
    assert ev & E_PANEL_RESET_DONE, (
        "EVENT.panel_reset_done must fire for ANY completed auto panel-reset — "
        "including one that did not change owner. The owner has to repaint.")
    assert not (ev & (E_HARNESS_GAINED | E_HARNESS_LOST | E_DUT_GAINED | E_DUT_LOST)), (
        f"panel_rst_pulse must not fabricate a gained/lost pair (EVENT={ev:#010x})")
    assert int(dut.owner_o.value) == OWNER_HARNESS, "panel_rst_pulse changed the owner"

    panel.clear()
    hs = _h_bytes(3)
    h.enqueue(hs)
    await h.wait_idle()
    await ClockCycles(dut.s_axi_aclk, 8)
    assert _seq(panel.strobes) == hs, "the owner must be able to drive after the pulse"
    _wellformed(panel.strobes, "after panel_rst_pulse", wr_lo=h.wr_lo,
                cs_setup=h.cs_setup)
    inv.check()


# ============================================================================ #
# 8. BL / RST — the DUT can NEVER touch them
# ============================================================================ #
@cocotb.test(skip=NO_RTL)
async def test_the_dut_can_never_drive_bl_or_rst(dut):
    """There are no BL/RST bits in the tunnel and there never will be — that is
    what makes a hung or garbage DUT ALWAYS recoverable (README §10).

    Drive EVERY tunnel bit high (including the three RESERVED ones) while the DUT
    owns the panel, and assert BL/RST do not move. Then blank the panel from the
    harness's CLCD.CTRL while the DUT still owns it — the harness's authority
    over the backlight survives handing the screen away."""
    axi, h, d, panel, inv = await _bring_up(dut)
    await _timers(axi, rst_us=1, settle_us=1)

    await _dut_owns(dut, axi, h, d, panel)
    bl0, rst0 = int(dut.clcd_bl_o.value), int(dut.clcd_rst_n_o.value)
    assert (bl0, rst0) == (1, 1), "bench premise: the panel is lit and out of reset"

    # Every tunnel bit high EXCEPT the two strobes (leaving those idle keeps the
    # panel bus quiet, so this test is about BL/RST and nothing else).
    d.stop()
    await Timer(d.period_ns * 2, units="ns")
    oe = ((1 << T_RS) | (1 << T_RD) | (1 << T_PD_OE) | (1 << T_BUSY)
          | (1 << T_REQ) | (1 << T_SPARE) | 0xFF)
    dut.dut_gpio_o_i.value = 0xFFFF
    dut.dut_gpio_oe_i.value = oe
    await ClockCycles(dut.s_axi_aclk, 20)

    assert int(dut.clcd_bl_o.value) == bl0, (
        "a tunnel bit pattern moved CLCD_BL. The DUT must not be able to reach "
        "the backlight — that is the recovery lever.")
    assert int(dut.clcd_rst_n_o.value) == rst0, (
        "a tunnel bit pattern moved CLCD_RST. A hung DUT would then be able to "
        "hold the panel in reset for ever.")

    # bl_rst_src = 0 (reset): BL/RST follow clcd_0's CTRL[1]/[2] even while the
    # DUT owns the panel — the drop-in property, and why the shipped firmware
    # keeps the panel lit on the new bitstream with no change.
    h.bl = 0
    await ClockCycles(dut.s_axi_aclk, 8)
    assert int(dut.clcd_bl_o.value) == 0, (
        "with bl_rst_src=0 the harness's CLCD.CTRL.backlight must still reach "
        "the pad, even while the DUT owns the panel bus (README §10)")
    h.bl = 1
    await ClockCycles(dut.s_axi_aclk, 8)
    assert int(dut.clcd_bl_o.value) == 1
    inv.check()


@cocotb.test(skip=NO_RTL)
async def test_bl_rst_src_selects_the_kvms_own_registers(dut):
    """bl_rst_src = 1: CTRL[5]/CTRL[6] drive the pads and clcd_0's CTRL is
    ignored. This is the cleaner steady state W2-F is asked to adopt — and it is
    REQUIRED if the harness ever wants to blank the panel while the DUT owns it.
    The auto-reset sequencer still overrides RST in this mode, which is the
    property that keeps recoverability intact."""
    axi, h, d, panel, inv = await _bring_up(dut)
    await _timers(axi, rst_us=2, settle_us=1)

    base = CTRL_RESET | C_BL_RST_SRC
    await axi.write(CTRL, base | C_BACKLIGHT | C_PANEL_RST_N)
    await ClockCycles(dut.s_axi_aclk, 4)
    assert int(dut.clcd_bl_o.value) == 1, "CTRL.backlight must drive CLCD_BL when bl_rst_src=1"
    assert int(dut.clcd_rst_n_o.value) == 1, "CTRL.panel_rst_n must drive CLCD_RST"

    # clcd_0's pads are now IGNORED.
    h.bl, h.rst_n = 0, 0
    await ClockCycles(dut.s_axi_aclk, 8)
    assert int(dut.clcd_bl_o.value) == 1, "with bl_rst_src=1, h_bl must not reach the pad"
    assert int(dut.clcd_rst_n_o.value) == 1, "with bl_rst_src=1, h_rst_n must not reach the pad"
    h.bl, h.rst_n = 1, 1

    # CTRL.panel_rst_n = 0 holds the panel in reset, and BL follows it off.
    await axi.write(CTRL, base | C_BACKLIGHT)
    await ClockCycles(dut.s_axi_aclk, 4)
    assert int(dut.clcd_rst_n_o.value) == 0
    assert int(dut.clcd_bl_o.value) == 0, (
        "BL must be forced off whenever the panel is in reset, from ANY cause")

    # ...and the hardware sequencer still overrides RST in this mode.
    await axi.write(CTRL, base | C_BACKLIGHT | C_PANEL_RST_N)
    await ClockCycles(dut.s_axi_aclk, 4)
    inv.saw_panel_reset = False
    await axi.write(CTRL, base | C_BACKLIGHT | C_PANEL_RST_N | C_PANEL_RST_PULSE)
    await ClockCycles(dut.s_axi_aclk, 6 * TICK_CYC)
    assert inv.saw_panel_reset, (
        "the auto panel-reset sequencer must override CLCD_RST in BOTH bl_rst_src "
        "modes — it is the pure-hardware recovery lever")
    inv.check()


# ============================================================================ #
# 9. THE TUNNEL — CDC, the `req` edge protocol, and the debug snapshot
# ============================================================================ #
@cocotb.test(skip=NO_RTL)
async def test_cdc_async_dut_clock_never_tears_the_8080_vector(dut):
    """The tunnel is a CDC and the KVM decodes a STROBED protocol across it.

    `board_gpio` gets away with a bare combinational mux because it only drives a
    pad; per-bit synchroniser skew here would tear the 8080 vector apart — `WR`
    asserting a cycle before `PD` settles latches a byte nobody sent. The KVM's
    2-FF sync + 2-cycle stability filter (README §12) exists to make that
    impossible.

    This test runs the DUT at a deliberately AWKWARD 37 MHz (a non-integer ratio
    to the 100 MHz shell clock, so the sampling phase walks through every
    relationship) AND injects per-bit skew on every tunnel transition — the bench
    model of exactly what the synchronisers do to an async vector, and the only
    way a cocotb bench can tear a vector at all (a whole-vector `.value =` is
    atomic and cannot).

    A KVM without the stability filter presents the half-updated vector to the
    pads for one cycle, and the panel model catches it as `pd_stable = False` or
    as a wrong byte."""
    # 27 ns = 37.037 MHz: async to the 100 MHz shell (ratio 2.7, non-integer, so
    # the sampling phase walks every relationship) AND representable at the 1 ps
    # simulator precision (1000/37 = 27.027... is not).
    period_ns = 27.0
    axi, h, d, panel, inv = await _bring_up(
        dut, dut_period_ns=period_ns, skew_ns=9.0, seed=7)
    await _timers(axi, rst_us=1, settle_us=1, timeout_us=300)

    await _req_owner(axi, OWNER_DUT)
    await _wait_owner(dut, OWNER_DUT)
    await ClockCycles(dut.s_axi_aclk, 10)
    panel.clear()

    stream = _d_bytes(24, first=0x80)
    d.enqueue(stream)
    await d.wait_idle()
    await ClockCycles(dut.s_axi_aclk, 20)

    got = _seq(panel.strobes)
    assert got == stream, (
        "the 8080 vector was TORN across the tunnel's clock-domain crossing.\n"
        f"  DUT sent : {stream}\n  panel saw: {got}\n"
        "The KVM's 2-FF sync + 2-cycle stability filter (README §12) is not "
        "holding the vector together: the pads saw a half-updated DUT vector.")
    _wellformed(panel.strobes, "async 37 MHz DUT with per-bit skew",
                wr_lo=_dut_wr_lo_cyc(d), cs_setup=_dut_cs_setup_cyc(d), tol=DUT_TOL)
    inv.check()
    dut._log.info(f"[kvm] {len(got)} bytes crossed a 37 MHz -> 100 MHz async "
                  "tunnel with per-bit skew, not one torn")


@cocotb.test(skip=NO_RTL)
async def test_dut_req_is_an_edge_and_is_gated_by_dut_req_en(dut):
    """The tunnel's `req` bit. Sampled as an EDGE, not a level (a level would let
    a stuck-high `req` veto every attempt to take the panel away with the
    button). Rising = 'I want the panel'; falling = 'you can have it back'. And
    it is only a request source at all when CTRL.dut_req_en is set — which resets
    to 0, so an unprovisioned RM cannot grab the panel at power-on."""
    axi, h, d, panel, inv = await _bring_up(dut)
    await _timers(axi, rst_us=1, settle_us=1, timeout_us=100)

    # dut_req_en = 0 (reset): req is IGNORED.
    d.req = 1
    await ClockCycles(dut.s_axi_aclk, 1000)
    st, _ = await axi.read(STATUS)
    assert st & S_DUT_REQ, "STATUS.dut_req must still REPORT the live req level"
    assert int(dut.owner_o.value) == OWNER_HARNESS, (
        "the DUT took the panel with CTRL.dut_req_en = 0 (its reset value). An "
        "unprovisioned or garbage RM must not be able to grab the panel before "
        "the harness has painted anything.")
    d.req = 0
    await ClockCycles(dut.s_axi_aclk, 20)

    # Opt the DUT in, then a RISING edge asks for the panel.
    await axi.write(CTRL, CTRL_RESET | C_DUT_REQ_EN)
    await ClockCycles(dut.s_axi_aclk, 10)
    d.req = 1
    await _wait_owner(dut, OWNER_DUT, timeout_cycles=40000,
                      what="(after a rising edge on the tunnel's req bit)")

    # A FALLING edge hands it back.
    d.req = 0
    await _wait_owner(dut, OWNER_HARNESS, timeout_cycles=40000,
                      what="(after a falling edge on req)")
    inv.check()


@cocotb.test(skip=NO_RTL)
async def test_tunnel_register_snapshots_the_tunnel_with_no_side_effect(dut):
    """TUNNEL @ 0x18 (RO): the synchronised, stability-filtered tunnel. It is how
    you tell 'the DUT is not driving anything' from 'the DUT is driving and the
    KVM is discarding it' without a scope. Pure capture."""
    axi, h, d, panel, inv = await _bring_up(dut)

    d.stop()
    await Timer(d.period_ns * 2, units="ns")
    o_val, oe_val = 0xAB37, 0x5C00 | 0x42
    dut.dut_gpio_o_i.value = o_val
    dut.dut_gpio_oe_i.value = oe_val
    await ClockCycles(dut.s_axi_aclk, 12)      # 2-FF sync + 2-cycle filter

    tun, _ = await axi.read(TUNNEL)
    assert (tun & 0xFFFF) == o_val, (
        f"TUNNEL[15:0] must be the synchronised dut_gpio_o: got {tun & 0xFFFF:#06x}, "
        f"expected {o_val:#06x}")
    assert ((tun >> 16) & 0xFFFF) == oe_val, (
        f"TUNNEL[31:16] must be the synchronised dut_gpio_oe: got "
        f"{(tun >> 16) & 0xFFFF:#06x}, expected {oe_val:#06x}")

    # Reading it twice cannot change it, and cannot drive the panel.
    panel.reset_activity()
    again, _ = await axi.read(TUNNEL)
    assert again == tun, "TUNNEL changed across a read — it must be pure capture"
    assert panel.idle, "reading TUNNEL drove the panel bus"
    inv.check()


# ============================================================================ #
# PB1 HELD THROUGH RESET — no press edge, no toggle (D13 boot-hook safety)
#
# D13's boot hook reads "PB1 held at power-up" as "skip the default overlay
# load". That same hold must NEVER also flip the panel to the DUT. Until
# 2026-09-23 the PB1 chain reset to "released" (pb_level_q = 0), so a button
# held through s_axi_aresetn -- a POR, or a WDOG reset -- was accepted as a
# PRESS on the first 1 µs tick after reset and toggled tgt_owner. The chain now
# resets to "pressed" (README §11). `make falsify-pb-reset` puts the old reset
# values back and these two tests MUST fail.
# ============================================================================ #
async def _pb_view(axi):
    st, _ = await axi.read(STATUS)
    ev, _ = await axi.read(EVENT)
    return st, ev


@cocotb.test(skip=NO_RTL)
async def test_pb1_held_through_reset_is_not_a_press(dut):
    """Held low through and after reset: no toggle, no pb_toggle event, the
    HARNESS keeps the panel. Then a real release + press still works."""
    axi, h, d, panel, inv = await _bring_up(dut, npb1_at_reset=0)

    st, ev = await _pb_view(axi)
    assert st & S_PB_LEVEL, "a button held through reset must read pressed (pb_level=1)"
    assert not (ev & E_PB_TOGGLE) and not (st & S_TGT_OWNER), (
        "a button HELD THROUGH RESET was taken as a press: the PB1 chain resets "
        "to 'released', so the first tick after reset saw a rising pb_level. "
        "The D13 boot hook's 'PB1 held at power-up' would flip the panel to the DUT.")

    debounce_us = 2
    await _timers(axi, debounce_us=debounce_us)
    await ClockCycles(dut.s_axi_aclk, 20 * TICK_CYC)   # keep holding: 10x the debounce
    st, ev = await _pb_view(axi)
    assert st & S_PB_LEVEL and st & S_PB_RAW
    assert not (ev & E_PB_TOGGLE), f"held button logged EVENT.pb_toggle (EVENT={ev:#x})"
    assert not (st & S_TGT_OWNER) and not (st & S_SWITCH_PENDING), (
        f"held button requested a switch (STATUS={st:#x})")
    assert int(dut.owner_o.value) == OWNER_HARNESS

    # release: a falling pb_level, which is never an event
    dut.user_npb1.value = 1
    await ClockCycles(dut.s_axi_aclk, (debounce_us + 3) * TICK_CYC)
    st, ev = await _pb_view(axi)
    assert not (st & S_PB_LEVEL), "pb_level did not follow the release"
    assert not (ev & E_PB_TOGGLE) and not (st & S_TGT_OWNER), "the RELEASE toggled"

    # ...and a real press after that release still works
    dut.user_npb1.value = 0
    await ClockCycles(dut.s_axi_aclk, (debounce_us + 3) * TICK_CYC)
    st, ev = await _pb_view(axi)
    assert st & S_PB_LEVEL and st & S_TGT_OWNER, (
        f"a real press after the release did not toggle (STATUS={st:#x})")
    assert ev & E_PB_TOGGLE, "a real press after the release did not log EVENT.pb_toggle"
    await _wait_owner(dut, OWNER_DUT, what="(the first real press after a held-through-reset)")
    inv.check()


@cocotb.test(skip=NO_RTL)
async def test_pb1_held_through_reset_real_timing(dut):
    """The same property at the SHIPPED timing: DEBOUNCE never written (10 ms),
    TICK_DIV 100. Held through a power-on reset for 11 ms, then through a second,
    WDOG-style reset (the old RTL pressed ~1 µs after it); only then is DEBOUNCE
    shortened, to show a real release + press still toggles. No source models
    run, so the 11 ms is nothing but the clock."""
    cocotb.start_soon(Clock(dut.s_axi_aclk, 10, units="ns").start())
    for sig in ("s_axi_awvalid", "s_axi_wvalid", "s_axi_bready", "s_axi_arvalid",
                "s_axi_rready", "s_axi_awaddr", "s_axi_araddr", "s_axi_wdata",
                "s_axi_awprot", "s_axi_arprot", "clcd_pd_i", "decouple_status"):
        getattr(dut, sig).value = 0
    dut.s_axi_wstrb.value = 0xF
    dut.rp_resetn.value = 1
    HarnessSource(dut).idle_now()
    DutTunnelSource(dut).idle_now()
    axi = AxiLiteMaster.from_dut(dut)

    async def reset_with_button_held():
        dut.user_npb1.value = 0                          # HELD
        dut.s_axi_aresetn.value = 0
        await ClockCycles(dut.s_axi_aclk, 8)
        dut.s_axi_aresetn.value = 1
        await RisingEdge(dut.s_axi_aclk)

    for which, hold_us in (("power-on", DEBOUNCE_RESET + 1000), ("WDOG-style second", 200)):
        await reset_with_button_held()
        await Timer(hold_us, unit="us")
        st, ev = await _pb_view(axi)
        assert st & S_PB_LEVEL, f"{which} reset: a held button must read pressed"
        assert not (ev & E_PB_TOGGLE) and not (st & S_TGT_OWNER) \
            and int(dut.owner_o.value) == OWNER_HARNESS, (
            f"{which} reset with PB1 held {hold_us} µs at the shipped 10 ms debounce: "
            f"the button was taken as a press (STATUS={st:#x} EVENT={ev:#x})")

    await axi.write(DEBOUNCE, 2)
    dut.user_npb1.value = 1                              # release
    await ClockCycles(dut.s_axi_aclk, 6 * TICK_CYC)
    st, ev = await _pb_view(axi)
    assert not (st & S_PB_LEVEL) and not (ev & E_PB_TOGGLE) and not (st & S_TGT_OWNER)
    dut.user_npb1.value = 0                              # a real press
    await ClockCycles(dut.s_axi_aclk, 6 * TICK_CYC)
    st, ev = await _pb_view(axi)
    assert st & S_TGT_OWNER and ev & E_PB_TOGGLE, (
        f"a real press after the release did not toggle (STATUS={st:#x} EVENT={ev:#x})")
