"""clcd_panel_model.py — a cocotb monitor that plays the role of the on-board
HX8347-D panel on the 8080 parallel bus the `clcd` block drives.

It watches ONLY the panel-side pads exported by the frozen port contract
(`fpga/shell/ip/clcd/README.md`):

    clcd_cs_n_o   chip select   (active low)
    clcd_rs_o     reg/data sel  (0 = command, 1 = data)
    clcd_wr_n_o   write strobe  (active low; the panel latches on its RISING edge)
    clcd_rd_n_o   read strobe   (active low; held deasserted when READ_PATH=0)
    clcd_pd_o     8-bit data bus (out)
    clcd_pd_oe    data-bus output-enable (1 = block drives the pads) [optional]

The one hardware fact this model encodes: an Intel-8080 write cycle latches the
data bus into the controller on the **rising edge of WR_n** while CS_n is
asserted. So the decoded {RS, byte} stream is exactly the sequence of
(clcd_rs_o, clcd_pd_o) values sampled at each WR_n 0->1 transition.

The model deliberately knows NOTHING about HX8347-D register values — it decodes
whatever byte stream the FSM emits. `tests/clcd/test_clcd.py` pushes a *synthetic*
vector and proves the block streams it faithfully; whether those bytes are the
right ones for a real panel is a board fact (see dut_notes.md, and the block
README's "init-table seam").

Timing note: every panel-side pad is synchronous to `s_axi_aclk` (the TIMING
register counts strobe widths in `s_axi_aclk` cycles), so the monitor samples
once per `s_axi_aclk` posedge in the ReadOnly phase and reconstructs each strobe
from consecutive samples. Widths below are therefore in whole `s_axi_aclk`
cycles.
"""
from __future__ import annotations

import cocotb
from cocotb.triggers import RisingEdge, ReadOnly

# RS encodings (clcd_rs_o), per the port contract.
RS_CMD = 0   # CMD  write (RS=0)
RS_DATA = 1  # DATA write (RS=1)


def _rd(sig):
    """Read a signal as an int, or None if it holds x/z (e.g. mid-reset)."""
    try:
        return int(sig.value)
    except ValueError:
        return None


class Strobe:
    """One decoded 8080 write cycle (a WR_n low pulse)."""

    __slots__ = (
        "rs", "byte", "wr_low_cycles", "pd_stable", "cs_low_throughout",
        "rd_high", "oe_high", "cs_setup_cycles", "n_fall", "n_rise",
    )

    def __init__(self):
        self.rs = None                # clcd_rs_o sampled across the low pulse
        self.byte = None              # stable clcd_pd_o value latched (the byte)
        self.wr_low_cycles = None     # s_axi_aclk cycles WR_n was low (== wr_lo)
        self.pd_stable = None         # clcd_pd_o constant across the whole low pulse
        self.cs_low_throughout = None # clcd_cs_n_o asserted (0) across the pulse
        self.rd_high = None           # clcd_rd_n_o stayed deasserted (1)
        self.oe_high = None           # clcd_pd_oe asserted (1) or None if no pad
        self.cs_setup_cycles = None   # cycles from the preceding CS_n fall to WR_n fall
        self.n_fall = None            # cycle index of the WR_n falling edge
        self.n_rise = None            # cycle index of the WR_n rising edge

    def __repr__(self):
        kind = "CMD" if self.rs == RS_CMD else "DATA" if self.rs == RS_DATA else "?"
        b = "??" if self.byte is None else f"0x{self.byte:02X}"
        return (f"<Strobe {kind} {b} wr_lo={self.wr_low_cycles} "
                f"cs_setup={self.cs_setup_cycles}>")


class PanelBusModel:
    """Concurrent monitor + decoder. Start `run()` as a background task after the
    clock exists; read `.strobes` / `.sequence` after a stream has drained."""

    def __init__(self, dut):
        self.dut = dut
        self.strobes: list[Strobe] = []
        self._stop = False
        self._oe = getattr(dut, "clcd_pd_oe", None)
        # "Any bus activity at all" flags — used by the no-read-side-effect
        # sweep, which must catch even a read (CLCD_RD) cycle that drives NO
        # WR strobe (so `strobes` alone would miss it).
        self.any_cs_low = False
        self.any_wr_low = False
        self.any_rd_low = False

    # ---- results ---------------------------------------------------------- #
    @property
    def sequence(self):
        """The decoded ordered [(rs, byte), ...] — the thing a stream test
        compares against its synthetic input vector."""
        return [(s.rs, s.byte) for s in self.strobes]

    def clear(self):
        self.strobes = []

    def reset_activity(self):
        """Clear the 'any pad ever asserted' flags before a window in which the
        block must stay completely idle on the panel side."""
        self.any_cs_low = False
        self.any_wr_low = False
        self.any_rd_low = False

    @property
    def idle(self):
        return not (self.any_cs_low or self.any_wr_low or self.any_rd_low)

    def stop(self):
        self._stop = True

    # ---- monitor ---------------------------------------------------------- #
    async def run(self):
        dut = self.dut
        n = 0
        prev_wr = None
        prev_cs = None
        cs_fall_n = None          # cycle index of the most recent CS_n falling edge

        in_low = False
        low_start_n = None
        pd_first = None
        pd_stable = True
        cs_low_all = True
        rd_high_all = True

        while not self._stop:
            await RisingEdge(dut.s_axi_aclk)
            await ReadOnly()

            wr = _rd(dut.clcd_wr_n_o)
            cs = _rd(dut.clcd_cs_n_o)
            rs = _rd(dut.clcd_rs_o)
            rd = _rd(dut.clcd_rd_n_o)
            pd = _rd(dut.clcd_pd_o)
            oe = _rd(self._oe) if self._oe is not None else None

            if wr is None or cs is None:
                # Undriven / mid-reset: don't fabricate edges.
                prev_wr, prev_cs = wr, cs
                n += 1
                continue

            # "Any activity" flags (any assertion of any strobe/select).
            if cs == 0:
                self.any_cs_low = True
            if wr == 0:
                self.any_wr_low = True
            if rd == 0:
                self.any_rd_low = True

            # CS_n falling edge -> remember when the chip select asserted.
            if prev_cs == 1 and cs == 0:
                cs_fall_n = n

            # WR_n falling edge -> a write strobe begins.
            if prev_wr == 1 and wr == 0:
                in_low = True
                low_start_n = n
                pd_first = pd
                pd_stable = True
                cs_low_all = (cs == 0)
                rd_high_all = (rd == 1)
            elif in_low and wr == 0:
                # inside the low pulse: accumulate stability / qualifier checks
                if pd != pd_first:
                    pd_stable = False
                if cs != 0:
                    cs_low_all = False
                if rd != 1:
                    rd_high_all = False

            # WR_n rising edge -> the panel latches {RS, byte} here.
            if in_low and prev_wr == 0 and wr == 1:
                st = Strobe()
                st.rs = rs
                st.byte = pd_first            # the value held stable across the low pulse
                st.wr_low_cycles = n - low_start_n
                st.pd_stable = pd_stable
                st.cs_low_throughout = cs_low_all and (cs == 0)
                st.rd_high = rd_high_all and (rd == 1)
                st.oe_high = None if oe is None else (oe == 1)
                st.n_fall = low_start_n
                st.n_rise = n
                st.cs_setup_cycles = (
                    None if cs_fall_n is None else (low_start_n - cs_fall_n)
                )
                self.strobes.append(st)
                in_low = False

            prev_wr, prev_cs = wr, cs
            n += 1
