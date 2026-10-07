"""tests/clcd_kvm_e2e/test_clcd_kvm_e2e.py — the CLCD-KVM END-TO-END feature
proof (W3-B, docs/CLCD_KVM_WAVE_PLAN.md Wave 3).

This bench assembles the REAL delivered blocks in the silicon topology and
proves the whole feature in simulation, before any rebuild:

    AHB-Lite BFM --> nanosoc_exp_socket --> ahb_clcd --> clcd_core   (REAL RTL)
                             |  lcd_* (active-high)
                             v
                    [ the W3-A tunnel mux ]  (tb_clcd_kvm_e2e.sv, byte-identical
                    dut_gpio_o/oe[15:8]       mirror of rp_nanosoc_wrapper's mux)
                             |
    HarnessSource (model) -> h_* -> clcd_kvm -> panel pads -> PanelBusModel
    AXI4-Lite CSR BFM --------------->
    USER_nPB1 / decouple_status / rp_resetn ->

The DUT side is REAL RTL: the bytes the panel decodes on the DUT side are
ahb_clcd's genuine 8080 output, crossing the genuine tunnel CDC. The harness
side is modelled with kvm_models.HarnessSource, exactly as W2-C did.

THE HEADLINE (test 1): with both sources live, toggle USER_nPB1 and assert —
THROUGH THE PANEL MODEL — that the bytes reaching the panel switch from the
HARNESS source to the DUT's REAL ahb_clcd output, with NO malformed / truncated
8080 cycle at the seam and CS never left asserted across the handover; then the
return switch. `make falsify` breaks the KVM's outgoing (S_DRAIN) drain gate in
a SCRATCH copy and proves this test turns RED — a seam test that cannot fail
proves nothing.

Test 2 drives the REMOTE path (a CSR src_sel write, standing in for the 6900
`display` verb) and proves it flips ownership identically. Test 3 proves the DFX
forced-revert overrides a remote-flip-to-DUT (the W2-G safety note, verified
end-to-end as the wave plan requires).
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_TESTS = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_TESTS, "common"))
sys.path.insert(0, os.path.join(_TESTS, "clcd"))       # panel model — read-only reuse
sys.path.insert(0, os.path.join(_TESTS, "clcd_kvm"))   # HarnessSource — read-only reuse
sys.path.insert(0, _HERE)

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from dut_presence import rtl_ready
from regmap import AxiLiteMaster
from clcd_panel_model import PanelBusModel, RS_CMD, RS_DATA   # tests/clcd/ — AS-IS
from kvm_models import HarnessSource, H_STROBE_LO             # tests/clcd_kvm/ — AS-IS

# ahb_lite is the DUT-side BFM (owned by W2-B). Absent => skip loudly.
try:
    from ahb_lite import AhbLiteMaster
    _HAVE_AHB = True
except Exception:                                              # noqa: BLE001
    _HAVE_AHB = False

# --------------------------------------------------------------------------- #
# Readiness gating — every real dependency can be absent; SKIP cleanly rather
# than hang or fail cryptically (tests/common/dut_presence.py convention).
# --------------------------------------------------------------------------- #
_KVM_RTL = os.path.join(_TESTS, "..", "fpga", "shell", "ip", "clcd_kvm")
_EXP_RTL = os.path.join(_TESTS, "..", "fpga", "rp", "nanosoc_exp")
_CORE_RTL = os.path.join(_TESTS, "..", "fpga", "shell", "ip", "clcd")
NO_RTL = not (
    rtl_ready(_KVM_RTL, ["clcd_kvm.sv"])
    and rtl_ready(_EXP_RTL, ["nanosoc_exp_socket.sv", "ahb_clcd.sv"])
    and rtl_ready(_CORE_RTL, ["clcd_core.sv"])
    and _HAVE_AHB
)

# ---- clcd_kvm CSR map (README §5 @ 0x44AD_0000; here the block's own page) -- #
CTRL, STATUS, EVENT, PANEL_TMR, TIMEOUT, DEBOUNCE, TUNNEL = (
    0x00, 0x04, 0x08, 0x0C, 0x10, 0x14, 0x18)
C_SRC_SEL = 1 << 0
C_TIMEOUT_EN = 1 << 2
C_PB_EN = 1 << 3
C_SRC_SEL_WE = 1 << 16
CTRL_BASE = C_TIMEOUT_EN | C_PB_EN            # reset defaults we must preserve
E_FORCED_REVERT = 1 << 5
E_PB_TOGGLE = 1 << 6
OWNER_HARNESS, OWNER_DUT = 0, 1

# ---- ahb_clcd register map (README §7, base 0x6000_0000) ------------------- #
DUT_BASE = 0x6000_0000
DUT_CTRL, DUT_CMD, DUT_DATA, DUT_STATUS, DUT_TIMING = (
    DUT_BASE + 0x00, DUT_BASE + 0x04, DUT_BASE + 0x08,
    DUT_BASE + 0x0C, DUT_BASE + 0x10)
DUT_ENABLE = 1 << 0

# Disjoint byte ranges so every byte at the panel is attributable to a source
# with no ambiguity (the KVM never rewrites data — this is a labelling trick).
def _h_bytes(n, first=0x10):
    return [(RS_CMD if i % 3 == 0 else RS_DATA, (first + i) & 0x7F) for i in range(n)]


def _d_bytes(n, first=0x80):
    return [(RS_CMD if i % 4 == 0 else RS_DATA, 0x80 | ((first + i) & 0x7F))
            for i in range(n)]


def _is_harness(b):
    return b is not None and b < 0x80


def _is_dut(b):
    return b is not None and b >= 0x80


# --------------------------------------------------------------------------- #
# Bring-up: two clock domains, both real BFMs, both real DUT-side blocks.
# --------------------------------------------------------------------------- #
def _init_inputs(dut):
    # AXI4-Lite channel inputs — drive 0 so the KVM's slave FSM never samples x.
    for nm in ("awvalid", "wvalid", "bready", "arvalid", "rready"):
        getattr(dut, f"s_axi_{nm}").value = 0
    for nm in ("awaddr", "awprot", "wdata", "wstrb", "araddr", "arprot"):
        getattr(dut, f"s_axi_{nm}").value = 0
    dut.user_npb1.value = 1        # ACTIVE-LOW pad: idle HIGH (unpressed)
    dut.decouple_status.value = 0


async def bringup(dut):
    cocotb.start_soon(Clock(dut.s_axi_aclk, 10, units="ns").start())   # 100 MHz
    cocotb.start_soon(Clock(dut.hclk, 20, units="ns").start())         # 50 MHz

    dut.s_axi_aresetn.value = 0
    dut.hresetn.value = 0
    dut.rp_resetn.value = 0        # RP held in reset -> KVM interlock (fail-safe)
    _init_inputs(dut)

    harness = HarnessSource(dut, cs_setup=2, wr_lo=4, wr_hi=4)
    harness.idle_now()

    # AHB BFM parks the socket bus idle even while hresetn is low (it reads
    # hresetn and issues nothing until released).
    ahb = AhbLiteMaster.from_dut(dut)

    await ClockCycles(dut.s_axi_aclk, 5)
    dut.hresetn.value = 1
    await ClockCycles(dut.hclk, 4)
    dut.s_axi_aresetn.value = 1
    await ClockCycles(dut.s_axi_aclk, 4)
    dut.rp_resetn.value = 1        # RP out of reset -> interlock clears
    await ClockCycles(dut.s_axi_aclk, 4)

    axi = AxiLiteMaster.from_dut(dut)
    panel = PanelBusModel(dut)
    cocotb.start_soon(panel.run())
    cocotb.start_soon(harness.run())

    # Small µs timers so handovers are quick (TICK_DIV=100 at the shipped 100 MHz
    # -> 1 µs = 100 cycles; README §14 says program small values, don't lower the
    # clock). rst/settle = 2 µs, hung-owner timeout = 500 µs, debounce = 2 µs.
    await axi.write(PANEL_TMR, (2 << 16) | 2)
    await axi.write(TIMEOUT, 500)
    await axi.write(DEBOUNCE, 2)

    # Enable the reference accelerator: lcd_en=1, ready to stream when granted.
    await ahb.write(DUT_CTRL, DUT_ENABLE)
    await ClockCycles(dut.s_axi_aclk, 5)
    # clcd_kvm's pb_level resets to PRESSED (README §11 step 5: a button held
    # through reset is not a press); the released button is accepted on the
    # first 1 µs tick after s_axi_aresetn. A press before that tick is folded
    # into "held through reset", so let it land before any test presses.
    await ClockCycles(dut.s_axi_aclk, 110)
    return axi, ahb, harness, panel


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _owner(dut):
    try:
        return int(dut.owner_o.value)
    except ValueError:
        return None


async def wait_owner(dut, want, timeout=20000):
    for _ in range(timeout):
        await RisingEdge(dut.s_axi_aclk)
        if _owner(dut) == want:
            return
    raise AssertionError(
        f"owner never became {'DUT' if want else 'HARNESS'} "
        f"(stuck at {_owner(dut)}) within {timeout} cycles")


async def press_button(dut, hold=400, gap=60):
    """One clean press of USER_nPB1 (active-low), held past the debounce."""
    dut.user_npb1.value = 0
    await ClockCycles(dut.s_axi_aclk, hold)
    dut.user_npb1.value = 1
    await ClockCycles(dut.s_axi_aclk, gap)


async def flip_via_csr(dut, axi, want):
    """The REMOTE path: write CLCDKVM.CTRL.src_sel(+src_sel_we) — the exact
    register the 6900 `display` verb writes (W2-G)."""
    await axi.write(CTRL, CTRL_BASE | (want & 1) | C_SRC_SEL_WE)


async def wait_dut_strobes(panel, n, dut, timeout=40000):
    for _ in range(timeout):
        await RisingEdge(dut.s_axi_aclk)
        if sum(1 for s in panel.strobes if _is_dut(s.byte)) >= n:
            return
    raise AssertionError(
        f"only {sum(1 for s in panel.strobes if _is_dut(s.byte))}/{n} DUT bytes "
        f"reached the panel")


async def push_dut(ahb, pairs):
    for rs, b in pairs:
        await ahb.write(DUT_DATA if rs == RS_DATA else DUT_CMD, b)


def assert_all_wellformed(panel, ctx):
    """THE SEAM PROPERTY (source-agnostic, pads-only). Every decoded 8080 write
    cycle must be whole: PD stable across the WR-low pulse, and CS asserted
    throughout it. A cutover mid-cycle necessarily breaks one of these. This is
    exactly what the drain-gate mutation (`make falsify`) violates."""
    bad = [s for s in panel.strobes if not (s.pd_stable and s.cs_low_throughout)]
    assert not bad, (
        f"{ctx}: {len(bad)} truncated/malformed 8080 cycle(s) at the panel "
        f"(pd_stable/cs_low_throughout False) — a handover cut a cycle in half:"
        f"\n  " + "\n  ".join(repr(s) for s in bad[:8]))


# --------------------------------------------------------------------------- #
# TEST 1 — THE HEADLINE. Button handover harness -> DUT -> harness, clean seam.
# --------------------------------------------------------------------------- #
@cocotb.test(skip=NO_RTL)
async def test_kvm_e2e_button_handover_and_back(dut):
    axi, ahb, harness, panel = await bringup(dut)

    # -- Phase 1: the harness owns the panel (reset default) and streams. ----
    # A LONG batch (0x10..0x37, all < 0x60) so the harness is provably STILL
    # streaming when the debounced button press lands ~200 cycles later — that
    # is what puts a real 8080 cycle across the seam for the drain gate to
    # protect (and for `make falsify` to truncate when the gate is removed).
    assert _owner(dut) == OWNER_HARNESS, "should power up owned by the harness"
    h1 = _h_bytes(40, first=0x10)
    harness.enqueue(h1)
    await harness.wait_phase(H_STROBE_LO)     # press while WR is asserted
    assert not harness.quiet, "must press while the harness is mid-cycle"

    # -- Handover to the DUT via the BUTTON. The two-sided gate must hold the
    #    harness's in-flight + queued bytes until it drains, THEN switch. ----
    await press_button(dut)
    await wait_owner(dut, OWNER_DUT)

    # Every harness byte must have reached the panel WHOLE before the switch.
    h_seen = [(s.rs, s.byte) for s in panel.strobes if _is_harness(s.byte)]
    assert h_seen == h1, (
        f"harness bytes truncated/lost across the seam: saw {len(h_seen)}, "
        f"expected {len(h1)} ({h_seen[-3:]} vs {h1[-3:]})")
    assert_all_wellformed(panel, "phase 1 (harness owns, then hands to DUT)")

    # -- Phase 2: the DUT (REAL ahb_clcd) now drives the panel. --------------
    d = _d_bytes(6, first=0x80)
    await push_dut(ahb, d)
    await wait_dut_strobes(panel, len(d), dut)
    d_seen = [(s.rs, s.byte) for s in panel.strobes if _is_dut(s.byte)]
    assert d_seen == d, (
        f"the DUT's real ahb_clcd output did not reach the panel intact: "
        f"saw {d_seen}, expected {d}")
    assert _owner(dut) == OWNER_DUT

    # Let the DUT drain so it is quiescent for the return handover.
    await ClockCycles(dut.hclk, 60)

    # -- Return switch DUT -> harness, again via the button. ----------------
    await press_button(dut)
    await wait_owner(dut, OWNER_HARNESS)
    h3 = _h_bytes(4, first=0x60)              # 0x60..0x63, distinct from batch 1
    harness.enqueue(h3)
    await harness.wait_idle()
    await ClockCycles(dut.s_axi_aclk, 400)

    # The seam is clean for the WHOLE run — no malformed cycle from either side.
    assert_all_wellformed(panel, "full run (harness -> DUT -> harness)")
    # The second harness batch must have arrived after the DUT batch, whole.
    h3_seen = [(s.rs, s.byte) for s in panel.strobes
               if _is_harness(s.byte) and s.byte >= 0x60]
    assert h3_seen == h3, (
        f"harness did not cleanly regain the panel: saw {h3_seen}, expected {h3}")

    # No DUT byte ever appeared while the harness owned the panel (ordering):
    # the DUT run is a single contiguous block in the decoded sequence.
    seq_owners = [_is_dut(s.byte) for s in panel.strobes]
    first_d, last_d = seq_owners.index(True), len(seq_owners) - 1 - \
        seq_owners[::-1].index(True)
    assert all(seq_owners[first_d:last_d + 1]), (
        "a DUT byte and a harness byte interleaved at the panel — a source was "
        "connected while it did not own the bus")


# --------------------------------------------------------------------------- #
# TEST 2 — the REMOTE path: a CSR src_sel write flips ownership identically.
# --------------------------------------------------------------------------- #
@cocotb.test(skip=NO_RTL)
async def test_kvm_e2e_csr_src_sel_flips_identically(dut):
    axi, ahb, harness, panel = await bringup(dut)

    harness.enqueue(_h_bytes(4, first=0x10))
    await harness.wait_idle()
    await ClockCycles(dut.s_axi_aclk, 200)
    assert _owner(dut) == OWNER_HARNESS

    # Flip to the DUT the way the 6900 `display` verb does — a CSR write.
    await flip_via_csr(dut, axi, OWNER_DUT)
    await wait_owner(dut, OWNER_DUT)

    d = _d_bytes(5, first=0x90)
    await push_dut(ahb, d)
    await wait_dut_strobes(panel, len(d), dut)
    d_seen = [(s.rs, s.byte) for s in panel.strobes if _is_dut(s.byte)]
    assert d_seen == d, f"CSR-flip DUT output wrong: {d_seen} != {d}"

    await ClockCycles(dut.hclk, 60)
    # Flip back via the CSR.
    await flip_via_csr(dut, axi, OWNER_HARNESS)
    await wait_owner(dut, OWNER_HARNESS)

    assert_all_wellformed(panel, "CSR remote-flip round trip")


# --------------------------------------------------------------------------- #
# TEST 3 — the DFX forced revert overrides a remote-flip-to-DUT (W2-G safety).
# --------------------------------------------------------------------------- #
@cocotb.test(skip=NO_RTL)
async def test_kvm_e2e_forced_revert_overrides_remote_dut(dut):
    axi, ahb, harness, panel = await bringup(dut)

    # Remote-flip to the DUT and let it take the panel.
    await flip_via_csr(dut, axi, OWNER_DUT)
    await wait_owner(dut, OWNER_DUT)
    await push_dut(ahb, _d_bytes(3, first=0xA0))
    await wait_dut_strobes(panel, 3, dut)

    # Now the decoupler engages (a partial reconfiguration begins). The KVM must
    # force-revert to the harness, immediately, WITHOUT waiting for the (now
    # clamped) RP to declare itself quiescent — dut-display-tunnel.md §3 / the
    # KVM's interlock.
    dut.decouple_status.value = 1
    await wait_owner(dut, OWNER_HARNESS)

    ev, _ = await axi.read(EVENT)
    assert ev & E_FORCED_REVERT, (
        f"EVENT.forced_revert not set after decouple_status (EVENT=0x{ev:02X})")

    # A remote-flip-to-DUT WHILE decoupled must be ignored — the interlock masks
    # every DUT-requesting source, wherever it came from.
    await flip_via_csr(dut, axi, OWNER_DUT)
    await ClockCycles(dut.s_axi_aclk, 800)      # > rst+settle, had it not masked
    assert _owner(dut) == OWNER_HARNESS, (
        "a remote flip to DUT was honoured while the RP was decoupled — the "
        "forced-revert interlock did not override the network path")
    st, _ = await axi.read(STATUS)
    assert st & (1 << 13), "STATUS.decoupled should be set while decouple_status=1"

    # Release the decoupler; the panel must be recoverable to the DUT again.
    # Wait for the KVM's 2-FF decouple synchroniser to clear before flipping —
    # a src_sel_we that lands while `decoupled` is still asserted is masked by
    # the interlock and, being a one-shot pulse, is never re-applied. (This is a
    # real host-side rule too: re-issue a remote flip AFTER the decoupler
    # releases; the KVM does not queue a request made mid-swap.)
    dut.decouple_status.value = 0
    await ClockCycles(dut.s_axi_aclk, 5)
    await flip_via_csr(dut, axi, OWNER_DUT)
    await wait_owner(dut, OWNER_DUT)
