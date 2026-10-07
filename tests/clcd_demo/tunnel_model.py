"""tunnel_model.py -- a model of everything BETWEEN the clcd_demo RM and the
glass: the tunnel encoding, the KVM's inversion, and the HX8347-D's 8080 latch.

It is deliberately split in two:

  * `TunnelChecker` is PURE PYTHON over a list of per-dut_clk-cycle samples. It
    knows the wire encoding (docs/contracts/dut-display-tunnel.md section 2),
    the KVM's active-high -> active-low inversion (section 3), the panel's
    "latch the bus on the WR_n RISING edge while CS_n is asserted" rule, and the
    section-5 TIMING FLOOR. It raises `TunnelViolation` on anything the real
    path would mangle.

  * `TunnelMonitor` is the thin cocotb half: sample `dut_gpio_o` / `dut_gpio_oe`
    once per `dut_clk` and hand the samples to the checker.

The split is not tidiness. A checker that only runs inside a simulator cannot be
shown to FAIL, and a check nobody has seen fail is not evidence. Because the
decode and the timing rules are pure, `tests/clcd_demo/test_tunnel_checker.py`
feeds this class deliberately-broken traces -- a setup/hold below the floor, a
swapped command/data line -- and asserts it rejects them, under plain pytest,
with no simulator and no licence.

The panel's byte-level model is the same idea as `tests/clcd/clcd_panel_model.py`
(which watches the SHELL block's pads); this one watches the DUT's tunnel
instead, because the tunnel bit map is exactly the thing that has never been
exercised from the RM side.
"""
from __future__ import annotations

from typing import List, NamedTuple, Optional, Sequence

# ---------------------------------------------------------------------------
# The tunnel bit map. NORMATIVE SOURCE: docs/contracts/dut-display-tunnel.md
# section 2 ("The bit map -- FROZEN"). Restated here because a bench must state
# what it believes independently of the RTL it is checking -- if this and
# fpga/rp/clcd_demo/rp_clcd_demo_wrapper.sv ever disagree, one of them is wrong
# and the bench is what says so.
# ---------------------------------------------------------------------------
PD_HI, PD_LO = 15, 8          # dut_gpio_o[15:8]   = PD[7:0], MSB-aligned

BIT_CS = 8                    # dut_gpio_oe[8]     ACTIVE-HIGH: 1 = selected
BIT_WR = 9                    # dut_gpio_oe[9]     ACTIVE-HIGH: 1 = strobe on
BIT_RS = 10                   # dut_gpio_oe[10]    0 = command, 1 = data
BIT_RD = 11                   # RESERVED -- must be 0
BIT_PD_OE = 12                # RESERVED -- must be 0
BIT_BUSY = 13                 # ACTIVE-HIGH: not quiescent
BIT_REQ = 14                  # ACTIVE-HIGH: asking for the panel
BIT_SPARE = 15                # RESERVED -- must be 0

RESERVED_BITS = (BIT_RD, BIT_PD_OE, BIT_SPARE)

RS_CMD, RS_DATA = 0, 1

#: docs/contracts/dut-display-tunnel.md section 5, in dut_clk cycles. NOT a
#: guideline: below this the KVM's 2-cycle stability filter can no longer
#: guarantee a settled vector at the pads, and the failure is silicon-only.
PHASE_FLOOR_CYCLES = 8
GUARD_FLOOR_CYCLES = 4


class TunnelViolation(AssertionError):
    """The DUT drove something the KVM/panel would mangle."""


class Sample(NamedTuple):
    """One dut_clk cycle of the tunnel, already split out of the two vectors."""
    cs: int
    wr: int
    rs: int
    pd: int
    busy: int
    req: int
    reserved: int          # OR of every RESERVED bit -- must always be 0


class Strobe(NamedTuple):
    """One decoded 8080 write cycle: what the panel latched, and when."""
    rs: int
    byte: int
    setup_cycles: int      # CS asserted -> WR asserted
    wr_cycles: int         # WR asserted (panel WR_n LOW)
    hold_cycles: int       # WR deasserted -> CS deasserted (PD/RS still held)
    at_cycle: int          # index of the latching edge, for error messages


def split(gpio_o: int, gpio_oe: int) -> Sample:
    """(dut_gpio_o, dut_gpio_oe) -> Sample, per the frozen bit map."""
    return Sample(
        cs=(gpio_oe >> BIT_CS) & 1,
        wr=(gpio_oe >> BIT_WR) & 1,
        rs=(gpio_oe >> BIT_RS) & 1,
        pd=(gpio_o >> PD_LO) & 0xFF,
        busy=(gpio_oe >> BIT_BUSY) & 1,
        req=(gpio_oe >> BIT_REQ) & 1,
        reserved=sum(((gpio_oe >> b) & 1) << i
                     for i, b in enumerate(RESERVED_BITS)),
    )


class TunnelChecker:
    """Decode a tunnel trace into 8080 write cycles, refusing anything unsafe.

    `phase_floor` / `guard_floor` default to the contract's numbers. A bench
    that deliberately builds the RM with sub-floor timing passes the SAME
    defaults -- the point of the control is that the CONTRACT rejects it, not
    that a relaxed checker accepts it.
    """

    def __init__(self, phase_floor: int = PHASE_FLOOR_CYCLES,
                 guard_floor: int = GUARD_FLOOR_CYCLES,
                 check_reserved: bool = True):
        self.phase_floor = phase_floor
        self.guard_floor = guard_floor
        self.check_reserved = check_reserved
        self.strobes: List[Strobe] = []
        self.req_edges: List[tuple] = []      # (cycle, 0->1 or 1->0)
        self.busy_low_between_bytes = 0
        self._n = 0

    # -- the whole check, over a finished trace ------------------------------
    def feed(self, samples: Sequence[Sample]) -> List[Strobe]:
        prev: Optional[Sample] = None
        cs_rise = None            # cycle CS went asserted
        wr_rise = None            # cycle WR went asserted
        wr_fall = None            # cycle WR went deasserted
        latched_rs = latched_pd = None
        held_pd = held_rs = None  # PD/RS as of the CS-asserted window

        for n, s in enumerate(samples):
            self._n = n
            if self.check_reserved and s.reserved:
                raise TunnelViolation(
                    f"cycle {n}: a RESERVED tunnel bit is set (rd/pd_oe/spare "
                    f"= 0b{s.reserved:03b}). docs/contracts/"
                    f"dut-display-tunnel.md section 2: every RESERVED bit must "
                    f"be driven 0 -- that is what makes a decoupled RP, a "
                    f"display-less RM and this one present identical bits.")

            if prev is not None and s.req != prev.req:
                self.req_edges.append((n, prev.req, s.req))

            # -- CS edges -----------------------------------------------------
            if prev is None or (s.cs and not prev.cs):
                if s.cs:
                    cs_rise = n
                    held_pd, held_rs = s.pd, s.rs
                    wr_rise = wr_fall = None
            if prev is not None and prev.cs and not s.cs:
                # CS released. If WR was still asserted the panel just had a
                # strobe truncated under it.
                if prev.wr:
                    raise TunnelViolation(
                        f"cycle {n}: CS deasserted while WR was still "
                        f"asserted -- the write strobe was truncated. The "
                        f"panel latches on the WR rising edge; a byte cut off "
                        f"here is a garbage write into GRAM.")
                if wr_fall is not None:
                    hold = n - wr_fall
                    if hold < self.guard_floor:
                        raise TunnelViolation(
                            f"cycle {n}: only {hold} cycle(s) of PD/RS hold "
                            f"after WR deasserted (floor {self.guard_floor}, "
                            f"docs/contracts/dut-display-tunnel.md section 5).")
                    self.strobes.append(Strobe(
                        rs=latched_rs, byte=latched_pd,
                        setup_cycles=wr_rise - cs_rise,
                        wr_cycles=wr_fall - wr_rise,
                        hold_cycles=hold, at_cycle=wr_fall))
                cs_rise = None

            # -- PD/RS must not move inside a selected window ------------------
            if s.cs and held_pd is not None:
                if s.pd != held_pd or s.rs != held_rs:
                    raise TunnelViolation(
                        f"cycle {n}: PD/RS changed while CS was asserted "
                        f"(PD 0x{held_pd:02X}->0x{s.pd:02X}, RS "
                        f"{held_rs}->{s.rs}). The panel samples the bus at the "
                        f"WR edge; a moving bus inside a selected window is a "
                        f"race, not a byte.")

            # -- WR edges ------------------------------------------------------
            if prev is not None and s.wr and not prev.wr:
                if not s.cs:
                    raise TunnelViolation(
                        f"cycle {n}: WR asserted with CS deasserted -- the "
                        f"panel is not selected, so this strobe goes nowhere "
                        f"(or somewhere worse).")
                wr_rise = n
                setup = n - cs_rise
                if setup < self.phase_floor:
                    raise TunnelViolation(
                        f"cycle {n}: CS->WR setup is {setup} cycle(s), floor "
                        f"{self.phase_floor} (docs/contracts/"
                        f"dut-display-tunnel.md section 5: every 8080 phase "
                        f"lasts >= 8 dut_clk cycles).")
                if setup < self.guard_floor:
                    raise TunnelViolation(
                        f"cycle {n}: PD/RS guard band before WR is {setup} "
                        f"cycle(s), floor {self.guard_floor}.")
            if prev is not None and prev.wr and not s.wr:
                wr_fall = n
                width = n - wr_rise
                if width < self.phase_floor:
                    raise TunnelViolation(
                        f"cycle {n}: WR was asserted for {width} cycle(s), "
                        f"floor {self.phase_floor} (docs/contracts/"
                        f"dut-display-tunnel.md section 5).")
                # The panel latches HERE (tunnel WR falling == panel WR_n
                # rising, because the KVM inverts -- section 3).
                latched_rs, latched_pd = prev.rs, prev.pd

            prev = s

        if prev is not None and prev.cs and prev.wr:
            raise TunnelViolation(
                "trace ended mid-strobe (CS and WR both asserted) -- capture "
                "more cycles, or the RM never released the bus.")
        return self.strobes

    # -- convenience views ---------------------------------------------------
    @property
    def sequence(self) -> List[tuple]:
        """[(rs, byte)] -- exactly what the panel's register state machine sees."""
        return [(s.rs, s.byte) for s in self.strobes]

    def commands(self) -> List[int]:
        return [s.byte for s in self.strobes if s.rs == RS_CMD]


def busy_ever_low_between(samples: Sequence[Sample], a: int, b: int) -> bool:
    """Did `busy` drop at any point between two cycle indices?

    The KVM's safe-switch gate is `!cs && !busy` (clcd_kvm README section 6). If
    that pair is never simultaneously true during a repaint, every handover has
    to wait out the 1 ms hung-owner timeout and gets FORCED -- which is exactly
    the case the RM's one-byte-outstanding design exists to avoid.
    """
    return any(not s.busy and not s.cs for s in samples[a:b])
