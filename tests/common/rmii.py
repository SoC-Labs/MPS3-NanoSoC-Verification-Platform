"""rmii.py — RMII bit-level driver/monitor for the rmii_phy_if bench
(partition-pins.md "Ethernet — RMII + MDIO"; ARCHITECTURE_SPEC.md §8.1/§8.3).

Self-contained (no extra pip package required) — every class here is
parametrized by explicit cocotb signal handles rather than hardcoded port
names, so the same driver/monitor pair binds to either side of
`fpga/ethernet/rmii_phy_if/rmii_phy_if.sv`:
  - RP-facing (partition pins, shell's view): `phy_rmii_ref_clk_o`,
    `phy_rmii_crs_dv_o`, `phy_rmii_rxd_o` (shell->DUT) and
    `phy_rmii_txd_i`/`phy_rmii_tx_en_i` (DUT->shell).
  - Shell-internal MII side (toward link_partner_mac): `mii_rxd_o`,
    `mii_rx_dv_o`, `mii_rx_er_o`, `mii_rx_clk_o` / `mii_txd_i`,
    `mii_tx_en_i`, `mii_tx_clk_o`.

cocotbext-eth ships an equivalent RmiiSource/RmiiSink/RmiiPhy pair (see
ARCHITECTURE_SPEC.md §12 reuse table) and is a reasonable accelerant if/when
it's installed in the sim environment (not installed as of this writing —
`import cocotbext.eth` fails in this dev environment); this hand-rolled
version keeps the bench runnable without that extra dependency.

NOTE on bit order: rmii_phy_if.sv's conversion bodies are 100% `// TODO(A1)`
today (every assign is a tied-off placeholder) — there is no RTL-confirmed
RMII-dibit-within-byte or MII-nibble-within-byte ordering to match yet. The
driver/monitor pair below is internally consistent (what one emits, the
other correctly decodes) but the specific bit order is a placeholder -
TODO(A5): confirm against IEEE 802.3 Annex 22B / the final RTL once
rmii_phy_if.sv's TODOs are filled in, and adjust `_BIT_ORDER` below if it
doesn't match.
"""
from __future__ import annotations

import cocotb
from cocotb.triggers import RisingEdge

try:
    import cocotbext.eth  # noqa: F401
    HAS_COCOTBEXT_ETH = True
except ImportError:
    HAS_COCOTBEXT_ETH = False


class RmiiFrameDriver:
    """Drives a 2-bit RMII (or 4-bit MII, via `bits_per_symbol=4`) bus:
    a `carrier`/`valid` signal + N-bit data, one symbol per `clk` edge.
    Used both for `phy_rmii_{crs_dv,rxd}` (RMII, 2 bits/symbol) and
    `mii_{rx_dv,rxd}` (MII, 4 bits/symbol) sides of the loopback bench.
    """

    def __init__(self, clk, valid, data, bits_per_symbol: int = 2):
        self.clk = clk
        self.valid = valid
        self.data = data
        self.bits_per_symbol = bits_per_symbol
        self.valid.value = 0
        self.data.value = 0

    async def send_frame(self, frame_bytes: bytes, ifg_bits: int = 96):
        """Sends `frame_bytes` (e.g. from frames.build_frame()) as
        successive `bits_per_symbol`-wide symbols, LSB-of-byte first
        within each byte. Idles `valid` low for `ifg_bits` bit-times after
        (models the inter-frame gap — see frames.ifg_violation())."""
        shifts = range(0, 8, self.bits_per_symbol)
        mask = (1 << self.bits_per_symbol) - 1
        for byte in frame_bytes:
            for shift in shifts:
                symbol = (byte >> shift) & mask
                await RisingEdge(self.clk)
                self.valid.value = 1
                self.data.value = symbol
        await RisingEdge(self.clk)
        self.valid.value = 0
        self.data.value = 0
        idle_symbols = max(0, ifg_bits // self.bits_per_symbol)
        for _ in range(idle_symbols):
            await RisingEdge(self.clk)


class RmiiFrameMonitor:
    """Samples a `carrier`/`valid` + N-bit data bus and reassembles bytes
    (mirror image of `RmiiFrameDriver`)."""

    def __init__(self, clk, valid, data, bits_per_symbol: int = 2):
        self.clk = clk
        self.valid = valid
        self.data = data
        self.bits_per_symbol = bits_per_symbol

    async def recv_frame(self, timeout_cycles: int = 10000):
        for _ in range(timeout_cycles):
            await RisingEdge(self.clk)
            if int(self.valid.value):
                break
        else:
            return None
        byte_val = 0
        nbits = 0
        out = bytearray()
        while int(self.valid.value):
            symbol = int(self.data.value)
            byte_val |= symbol << nbits
            nbits += self.bits_per_symbol
            if nbits >= 8:
                out.append(byte_val & 0xFF)
                byte_val = 0
                nbits = 0
            await RisingEdge(self.clk)
        return bytes(out)
