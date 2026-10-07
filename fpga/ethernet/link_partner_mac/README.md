# `link_partner_mac` — on-chip link-partner MAC

**Real RTL since W-RTL-ETH (2026-07-06).** Recovers frames sent by the DUT
MAC (via `rmii_phy_if/`'s MII side) as AXI-Stream, and accepts frames to
transmit toward the DUT the same way — the shell-side "link partner" spec
§8 refers to (a real MAC-framing layer, not just a PHY, so the datapath can
independently CRC-check what the DUT sends and frame what the DUT
receives). Bench: `tests/link_partner_mac/` (3/3 green under VCS + cocotb
2.0.1) — it elaborates this module **paired with `rmii_phy_if`** via a
bench-local wrapper, since the MII pacing contract between the two is
exactly what needs proving. Verilator `-Wall` clean.

## Reuse outcome (supersedes the Phase-0 plan)

The Phase-0 plan named verilog-ethernet's `eth_mac_mii` +
`eth_axis_rx`/`eth_axis_tx` (MIT) as near-direct instantiation. **Decision:
hand-rolled minimal MAC glue instead** — same vendoring grounds as
`bridge/README.md` (no verilog-ethernet checkout exists on this machine;
repo consumes external IP read-only; no vendored third-party RTL), plus a
scope observation: with FCS kept **inside** the AXIS frames (the convention
gen_checker and `tests/common/frames.py` already use), the residual MAC is
just preamble/SFD framing + an independent FCS *check* — small enough that
importing a full MAC (with its FCS insert/strip, padding and FIFO stack,
plus the flipped-clocking adaptation) is more integration surface than the
written module.

## What v1 does / does not do (normative list in the RTL header)

- **AXIS frame convention: frames include the 4-byte FCS, both
  directions.** No FCS insertion/stripping here; RX *checks* it and
  reports per-frame via `m_axis_rx_tuser` (1 = bad FCS / non-whole-byte
  frame / overrun drop).
- TX: single-frame store-and-forward (2 KB); emits 7×0x55 + 0xD5
  preamble/SFD then the frame nibbles LSB-first; enforces 96-bit IFG.
  Oversize frames dropped whole. No padding, no half-duplex/deferral.
- RX: preamble hunt + SFD strip; low-nibble-first byte assembly; one-byte
  lookahead so `tlast` lands on the true final byte; promiscuous (no
  address filter); no length policing (gen_checker owns the envelope).
- **Single-clock**: `clk_i` must be the same 50 MHz net as
  `rmii_phy_if.refclk_i`; the MII clocks are pacing enables, no CDC inside
  the pairing (the partition-boundary CDC story lives in `rmii_phy_if`).
- **No RX FIFO (A6 flag):** the wire can't be backpressured; a byte the
  sink hasn't taken by the time the next completes (4 clk) is dropped and
  the frame's tuser set. The in-tree sink (bridge ingress) is always-ready
  while receiving but parks between frames — v2 wants a small FIFO here.

## Note for `gen_checker/`

Spec §8.1 wants the error generator/checker able to "inject known-good and
malformed frames... and independently check the DUT MAC's TX framing/CRC and
RX behaviour." The cleanest tap points for that are exactly this module's
AXI-Stream ports — `gen_checker/` should sit logically between
`link_partner_mac/` and `bridge/` (or splice in as a third AXI-Stream
producer/consumer arbitrated ahead of the bridge). Not wired in the BD yet;
see `fpga/shell/bd/shell_bd.tcl` Section 5 TODOs (W-BD owns that wiring).
