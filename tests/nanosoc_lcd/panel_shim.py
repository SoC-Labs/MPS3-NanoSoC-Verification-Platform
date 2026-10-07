"""panel_shim.py — the ACTIVE-HIGH -> ACTIVE-LOW adapter that lets the DUT-side
accelerator (`ahb_clcd` inside `nanosoc_exp_socket`) be verified by the SAME
cocotb panel monitor that verifies the shell's `clcd` block.

Why this file exists at all
--------------------------
`tests/clcd/clcd_panel_model.py` plays the real HX8347-D on the 8080 bus. It
watches ONLY the panel pads, so it is bus-agnostic (it does not care whether the
bytes arrived over AXI4-Lite or AHB-Lite) and is reused here VERBATIM — it is
`tests/clcd/`'s file and is read-only to this bench. But it expects the *panel's
native* pad names and the panel's native ACTIVE-LOW sense:

    clcd_cs_n_o, clcd_wr_n_o, clcd_rd_n_o   (asserted = 0)
    clcd_rs_o, clcd_pd_o                    (level, no polarity)
    s_axi_aclk                              (the sampling clock)

The socket's display pins are ACTIVE-HIGH and differently named
(`fpga/rp/nanosoc_exp/README.md` §2, §6):

    lcd_cs, lcd_wr   (asserted = 1)
    lcd_rs, lcd_pd
    hclk

That polarity is NOT a style choice and must NOT be "fixed" in the RTL: these
wires cross the RP boundary through the DFX decoupler, whose DECOUPLED_VALUE is
`0x0`. Active-high therefore means a decoupled (mid-partial-reconfiguration) DUT
clamps to "chip deselected, both strobes idle" **by construction**. Inverting
them in the RTL would turn that safe clamp into "chip selected, both strobes
asserted, for the whole reconfiguration" — a panel-corrupting bug that no
simulation of the block alone would catch. The shell (`clcd_kvm`) does the single
inversion onto the physical pads.

So the inversion belongs HERE, in the bench, in exactly one place: this shim
stands where the shell's KVM stands on the real board.

    DUT (active-high) --> [PanelPads] --> PanelBusModel (active-low) --> bytes

Read strobe
-----------
The socket has no `lcd_rd` and no `lcd_pd_oe` port: the panel is WRITE-ONLY in
this platform (`README.md` §2; `docs/CLCD_PANEL_FACTS.md` §5/§7.2 — READ_PATH=0
shipped, and whether the board's LCD buffers can be read at all is unknown). The
RM wrapper drives those tunnel bits to 0. The shim therefore presents a constant
DEASSERTED `clcd_rd_n_o` (= 1), which keeps the panel model's `any_rd_low` flag
false and lets its `.idle` property mean exactly "the DUT drove nothing at the
panel" — which is what the no-read-side-effects test (test 6) needs.
"""
from __future__ import annotations


class _Inverted:
    """One active-high DUT pin presented as its active-low panel pad.

    Propagates x/z faithfully: `int()` on an x-valued cocotb value raises
    ValueError, which is what the panel model's `_rd()` catches to mean "not
    driven yet / mid-reset — do not fabricate an edge". Inverting an unknown
    must stay unknown, so the ValueError is allowed to escape rather than being
    turned into a 0 or a 1.
    """

    __slots__ = ("_h",)

    def __init__(self, handle):
        self._h = handle

    @property
    def value(self):
        return 1 - int(self._h.value)


class _Const:
    """A pad the socket does not have, held at its inert level."""

    __slots__ = ("_v",)

    def __init__(self, v):
        self._v = v

    @property
    def value(self):
        return self._v


class PanelPads:
    """A duck-typed stand-in for a `clcd`-shaped DUT handle, built from a
    `nanosoc_exp_socket`-shaped one. Hand it to `PanelBusModel(...)`.

    Deliberately NOT gated by `lcd_en`. `lcd_en` selects, out in the RM wrapper,
    whether the socket's pins or the M0's raw GPIO bits reach the tunnel — it is
    not part of the 8080 cycle. Gating here would silently turn "the block forgot
    to assert lcd_en" into "the block emitted no bytes", which is a confusing way
    to fail. The bench asserts `lcd_en` explicitly instead (test 4), so the two
    faults report separately.
    """

    def __init__(self, dut):
        self.s_axi_aclk = dut.hclk          # the panel model samples on this
        self.clcd_cs_n_o = _Inverted(dut.lcd_cs)
        self.clcd_wr_n_o = _Inverted(dut.lcd_wr)
        self.clcd_rs_o = dut.lcd_rs         # 0 = CMD, 1 = DATA — same sense
        self.clcd_pd_o = dut.lcd_pd         # level — no polarity
        self.clcd_rd_n_o = _Const(1)        # no read path in the socket (see above)
        # No `clcd_pd_oe`: the panel model does `getattr(dut, "clcd_pd_oe", None)`
        # and tolerates its absence (Strobe.oe_high stays None).
