"""mdio_master.py — Clause-22 MDIO (MDC/MDIO) frame logic + a cocotb bus
master, for exercising `fpga/ethernet/mdio_phy_model/mdio_phy_model.sv`
(the virtual-PHY register model; see that block's README for the exact
register table this file's constants mirror).

Two layers, deliberately separated:
  1. Pure functions (`build_frame_bits`, `decode_read_reply`) + register
     constants — no cocotb, no DUT, no simulator. Exercised directly by
     tests/common/test_mdio_master.py.
  2. `MDIOMaster` — a cocotb bit-bang driver using layer 1's framing. In
     the real system the DUT MAC is the MDIO *master* and
     `mdio_phy_model` is the *slave* it polls (partition-pins.md: "DUT is
     MDIO master; this model is the slave the DUT polls" —
     mdio_phy_model.sv's own comment). To unit-test that slave in
     isolation before any real DUT MAC exists, this class plays the
     master role instead, bit-banging `mdc_i`/`mdio_o_i`/`mdio_oe_i`
     (this module's inputs, i.e. what a real DUT would drive) and
     sampling `mdio_i_o` (this module's reply). Bind it directly to those
     four ports in tests/mdio_phy_model/test_mdio_phy_model.py.

Frame format (IEEE 802.3 Clause 22.2.4.5): PRE(32x'1') ST(2)=01
OP(2)=10(rd)/01(wr) PHYAD(5) REGAD(5) TA(2) DATA(16), MSB-first throughout.
"""
from __future__ import annotations

try:
    import cocotb
    from cocotb.triggers import Timer
    _HAVE_COCOTB = True
except ImportError:  # pragma: no cover - cocotb is a hard dependency of the
    _HAVE_COCOTB = False  # sim-side of this repo, but keep the pure-logic
    # half of this module importable (e.g. for a lint-only environment).

# --------------------------------------------------------------------------- #
# Clause-22 frame constants
# --------------------------------------------------------------------------- #
ST = 0b01
OP_WRITE = 0b01
OP_READ = 0b10

# Register table per fpga/ethernet/mdio_phy_model/README.md "Two register
# spaces" #2 — the handful of Clause-22 registers this model implements.
REG_BMCR = 0
REG_BMSR = 1
REG_PHYID1 = 2
REG_PHYID2 = 3
REG_ANAR = 4
REG_ANLPAR = 5

# BMCR (reg 0) bit positions — IEEE 802.3 standard, not vendor-specific.
BMCR_RESET = 1 << 15
BMCR_LOOPBACK = 1 << 14
BMCR_SPEED_LSB = 1 << 13       # 100 Mb/s when set (with MSB=0)
BMCR_AN_ENABLE = 1 << 12
BMCR_POWER_DOWN = 1 << 11
BMCR_ISOLATE = 1 << 10
BMCR_AN_RESTART = 1 << 9
BMCR_DUPLEX_FULL = 1 << 8
BMCR_COLLISION_TEST = 1 << 7
BMCR_SPEED_MSB = 1 << 6

# BMSR (reg 1) bit positions — IEEE 802.3 standard. mdio_phy_model's README
# only drives bits [5,4,3,2,1,0]; bits 15/14/13/12/11 (100BASE-T4 etc.
# capability advertisement) are listed here for completeness/decoding but
# the model isn't specified to drive them meaningfully.
BMSR_100BASE_T4 = 1 << 15
BMSR_100BASE_TX_FULL = 1 << 14
BMSR_100BASE_TX_HALF = 1 << 13
BMSR_10BASE_T_FULL = 1 << 12
BMSR_10BASE_T_HALF = 1 << 11
BMSR_AUTO_NEG_COMPLETE = 1 << 5     # README: "[5] auto-neg complete"
BMSR_REMOTE_FAULT = 1 << 4         # README: "[4] remote fault (tie 0)"
BMSR_AUTO_NEG_ABILITY = 1 << 3     # README: "[3] auto-neg capable (tie 1)"
BMSR_LINK_STATUS = 1 << 2          # README: "[2] link status"
BMSR_JABBER_DETECT = 1 << 1        # README: "[1] jabber (tie 0)"
BMSR_EXTENDED_CAP = 1 << 0         # README: "[0] extended capability (tie 1)"

# ANAR/ANLPAR (regs 4/5) — 100BASE-TX full-duplex advertise/link-partner bit,
# the one README pins down ("100BASE-TX full-duplex bit set when
# PHY_STATE.speed100 & full_duplex").
ANLPAR_100BASE_TX_FULL = 1 << 8
ANLPAR_100BASE_TX_HALF = 1 << 7
ANLPAR_10BASE_T_FULL = 1 << 6
ANLPAR_10BASE_T_HALF = 1 << 5
ANLPAR_SELECTOR_802_3 = 0b00001    # bits [4:0]


def _int_to_bits(value: int, width: int):
    return [(value >> (width - 1 - i)) & 1 for i in range(width)]


def build_frame_bits(op: int, phyad: int, regad: int, data: int = 0,
                      include_preamble: bool = True):
    """Returns a list of bits (0/1, or None where the station releases the
    bus — TA[0]/TA[1]/DATA on a read, since the PHY drives those).

    Layout: [PRE(32x1)?] ST(2) OP(2) PHYAD(5) REGAD(5) TA(2) DATA(16).
    """
    bits = []
    if include_preamble:
        bits += [1] * 32
    bits += _int_to_bits(ST, 2)
    bits += _int_to_bits(op, 2)
    bits += _int_to_bits(phyad, 5)
    bits += _int_to_bits(regad, 5)
    if op == OP_WRITE:
        bits += [1, 0]                       # TA driven "10" by the station
        bits += _int_to_bits(data, 16)
    else:
        bits += [None, None]                 # TA: station releases the bus
        bits += [None] * 16                   # DATA: PHY drives it, not us
    return bits


def decode_read_reply(ta_and_data_bits):
    """`ta_and_data_bits`: the 18 bits sampled off the reply wire during a read
    frame's second half (payload phase), one per MDC period. Returns
    (value, ta) where `value` is the 16-bit register contents and `ta` is the
    single sampled turnaround bit.

    Reply framing — co-designed with fpga/ethernet/mdio_phy_model/mdio_slave.sv,
    and matching a real Clause-22 PHY as read by a real MAC (OpenCores MIIM): the
    PHY takes the bus at the first payload bit-time (turnaround) and drives
    DATA[15] on the very next one, so DATA[15:0] land at sampled indices 1..16,
    MSB-first (index 0 is the turnaround, index 17 a trailing idle bit-time).

    An earlier revision of the model drove DATA one MDC period LATER (data at
    sampled indices 2..17, after a two-bit turnaround); that skew read every
    register back as (value >> 1) against a real MAC — the bug
    tests/nanosoc_multicore_eth (c2) caught. Sampling indices 1..16 here keeps
    this Python master aligned with the corrected model.
    """
    assert len(ta_and_data_bits) == 18, f"expected 18 bits, got {len(ta_and_data_bits)}"
    ta = ta_and_data_bits[0]
    value = 0
    for b in ta_and_data_bits[1:17]:
        value = (value << 1) | (int(b) & 1)
    return value, int(ta)


if _HAVE_COCOTB:

    class MDIOMaster:
        """Bit-bangs Clause-22 MDIO as the *station* (MDIO master) side —
        see module docstring for why this plays the DUT-MAC role when
        unit-testing mdio_phy_model in isolation.

        `mdc`, `mdio_o`, `mdio_oe`, `mdio_i` are cocotb signal handles:
        for mdio_phy_model.sv, bind as
        ``MDIOMaster(dut.mdc_i, dut.mdio_o_i, dut.mdio_oe_i, dut.mdio_i_o)``
        (mdc/mdio_o/mdio_oe are *our* drives -> the model's inputs;
        mdio_i is the model's reply -> our input).
        """

        def __init__(self, mdc, mdio_o, mdio_oe, mdio_i, period_ns: int = 400):
            # period_ns=400 -> 2.5 MHz MDC, the Clause-22 maximum rate.
            self.mdc = mdc
            self.mdio_o = mdio_o
            self.mdio_oe = mdio_oe
            self.mdio_i = mdio_i
            self._half = period_ns / 2
            self.mdc.value = 0
            self.mdio_oe.value = 0

        async def _clock_bit(self, drive_value):
            """One MDC period. `drive_value=None` releases the bus for
            this bit (used for TA/DATA on a read)."""
            if drive_value is None:
                self.mdio_oe.value = 0
            else:
                self.mdio_oe.value = 1
                self.mdio_o.value = int(drive_value)
            self.mdc.value = 0
            await Timer(self._half, units="ns")
            self.mdc.value = 1
            await Timer(self._half, units="ns")

        async def write(self, phyad: int, regad: int, data: int):
            bits = build_frame_bits(OP_WRITE, phyad, regad, data)
            for b in bits:
                await self._clock_bit(b)
            self.mdio_oe.value = 0  # release after the frame

        async def read(self, phyad: int, regad: int) -> int:
            bits = build_frame_bits(OP_READ, phyad, regad)
            header_len = len(bits) - 18  # PRE+ST+OP+PHYAD+REGAD, all driven
            for b in bits[:header_len]:
                await self._clock_bit(b)
            sampled = []
            for _ in range(18):
                self.mdio_oe.value = 0
                self.mdc.value = 0
                await Timer(self._half, units="ns")
                self.mdc.value = 1
                sampled.append(int(self.mdio_i.value))
                await Timer(self._half, units="ns")
            value, _ta0 = decode_read_reply(sampled)
            return value
