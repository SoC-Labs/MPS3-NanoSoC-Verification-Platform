# `rmii_phy_if` — RMII ⇄ MII datapath (virtual-PHY side)

**Real conversion RTL since W-RTL-ETH (2026-07-06).** Converts between the
DUT-facing RMII interface (2-bit data @ 50 MHz, `phy_rmii_*` partition pins)
and a shell-internal MII-style bus consumed by
`fpga/ethernet/link_partner_mac/`. Sources the 50 MHz RMII reference clock
(shell is the "PHY" in this pairing — spec §8.3). Bench:
`tests/rmii_phy_if/` (2/2 green under VCS + cocotb 2.0.1); also elaborated
paired with the MAC by `tests/link_partner_mac/`. Verilator `-Wall` clean.

**Reuse outcome:** hand-rolled, not LiteEth. The Phase-0 plan named LiteEth
`liteeth/phy/rmii.py` (BSD-2) as the reuse target; at implementation time no
LiteEth checkout exists on this machine, the repo consumes external IP
strictly read-only via env vars (no vendored third-party RTL), and the
conversion is ~150 lines in one module — smaller than importing/flipping a
generated core. Same vendoring decision as `bridge/`; see that README.

## Clocking (decision — the old "divide/MMCM?" TODO is resolved)

`refclk_i` **must already be 50 MHz** (the shell clocking plan/BD clock
wizard sources it). No divider or MMCM inside the module;
`phy_rmii_ref_clk_o` is the same net forwarded across the partition
boundary (DUT MAC must be configured ref-**in**). Everything inside runs in
this one 50 MHz domain. The MII "clocks" (`mii_rx_clk_o`/`mii_tx_clk_o`)
are divide-by-2 pacing clocks generated as flops in that domain: MII RX
data/dv change only on the low phase (stable across every rising edge), and
MII TX inputs are sampled at the middle of each `mii_tx_clk` period —
consumers in the same domain (link_partner_mac) use them as enables, so no
CDC exists inside this pairing. See the RTL header's CLOCKING note for the
partition-boundary CDC argument (RMII is synchronous to the shell-sourced
REF_CLK by construction; a 2-FF/FIFO stage is only needed if a future plan
un-shares that reference — flagged for A6 there).

## What the v1 model does / does not do (full list in the RTL header)

Modelled: dibit⇄nibble conversion both directions, LSB-first within bytes
(IEEE 802.3 Annex 22B order: dibit0 = bits[1:0], nibble0 = bits[3:0]);
phase lock to the tx_en/crs_dv assertion edge (the RMII preamble-alignment
rule); CRS_DV/RX_DV held for the exact frame duration.

Simplified v1 (documented deviations): CRS_DV is a plain frame-envelope
valid (no 25 MHz CRS/DV toggle de-multiplexing); no preamble insertion or
stripping (transparent converter — the MACs own preamble); 100 Mb/s
full-duplex only (no 10 Mb/s dibit repeat, no COL/half-duplex CRS); no
RX_ER generation (`mii_rx_er_o` tied low); odd trailing dibits truncated.

## Port groups (see `rmii_phy_if.sv` — unchanged from the Phase-0 skeleton)

1. **RP-facing (RMII), names/directions per `docs/contracts/partition-pins.md`
   — shell's view:**
   - `phy_rmii_ref_clk` (O), `phy_rmii_crs_dv` (O), `phy_rmii_rxd[1:0]` (O) —
     shell drives these toward the DUT MAC.
   - `phy_rmii_txd[1:0]` (I), `phy_rmii_tx_en` (I) — shell samples these from
     the DUT MAC.
2. **Shell-internal (MII), toward `link_partner_mac/`:** standard 4-bit MII
   nibble bus (`mii_rxd[3:0]`, `mii_rx_dv`, `mii_rx_er`, `mii_rx_clk` /
   `mii_txd[3:0]`, `mii_tx_en`, `mii_tx_clk`).
3. **Reference clock source:** `refclk_i` — required 50 MHz, see Clocking.

## HDPR-29 IOB packing note (Z2 lesson) — THE RMII TX RE-REGISTER STAGE

`partition-pins.md`'s "IOB packing note" (from the Z2 DFX bring-up,
HDPR-29): pin-facing/boundary-facing output registers that want `IOB TRUE`
packing must live in the **static shell**, never in the RM — OLOGIC/pad
sites are static-only in a DFX design. Concretely: `phy_rmii_txd` and
`phy_rmii_tx_en` are **inputs to the shell** (driven by the RP/DUT MAC), so
the first register stage that samples them, immediately after they cross the
partition boundary, is instantiated **here**, in `rmii_phy_if.sv`
(`phy_rmii_txd_q`/`phy_rmii_tx_en_q`, clocked on `refclk_i` — the same net
as the forwarded reference) — not folded into RP-side logic, and not left
as an unregistered combinational tap.

This MPS3 build's RMII never reaches a physical pad (the "virtual PHY" is
fabric-only — spec §8: "we do not tap frames above it... we terminate at
RMII and provide a fabric-side virtual PHY"), so there is no OLOGIC/pad
timing argument today. The rule still applies for two reasons: (1) it keeps
the RP boundary clean of any static-side timing dependency, which matters
again the moment this shell is reused with a real physical PHY downstream
(a straightforward follow-on since the signal names/roles are identical to
the Z2 target this contract mirrors — see `partition-pins.md` line 10), and
(2) it's the same discipline that makes `swd_dio_*` / `mdio_*` safe to treat
as plain fabric signals across the partition rather than needing IOB
attributes pushed into the RM. Mark the stage `(* IOB = "TRUE" *)` if/when
a physical pad follows it (v1+, not this MPS3 build).
