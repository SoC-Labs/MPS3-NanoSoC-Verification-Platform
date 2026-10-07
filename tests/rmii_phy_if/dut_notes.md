# DUT notes — `rmii_phy_if`

RTL: `fpga/ethernet/rmii_phy_if/rmii_phy_if.sv`, module `rmii_phy_if` (no
parameters). **Real conversion RTL since W-RTL-ETH (2026-07-06)** — the
Phase-0 stub marker is gone, so this bench unskips. Proven green under VCS
2022.06-SP2 + cocotb 2.0.1, 2/2, 2026-07-06.

Port list (unchanged from the Phase-0 skeleton):

- `refclk_i`, `rst_i` — **`refclk_i` must be 50 MHz** (decision recorded in
  the RTL header: the BD clock wizard sources it; no divider/MMCM inside
  the module; `phy_rmii_ref_clk_o` is the same net forwarded). The bench
  drives it at 20 ns accordingly.
- RP-facing (partition pins, shell's view): `phy_rmii_ref_clk_o`,
  `phy_rmii_crs_dv_o`, `phy_rmii_rxd_o[1:0]` (shell -> DUT MAC) /
  `phy_rmii_txd_i[1:0]`, `phy_rmii_tx_en_i` (DUT MAC -> shell — the
  HDPR-29 re-register stage samples these first, static-side, clocked on
  `refclk_i` = the same net as the forwarded ref clock).
- Shell-internal MII (toward `link_partner_mac`): `mii_rxd_o[3:0]`,
  `mii_rx_dv_o`, `mii_rx_er_o`, `mii_rx_clk_o` / `mii_txd_i[3:0]`,
  `mii_tx_en_i`, `mii_tx_clk_o`. The MII clocks are divide-by-2 pacing
  clocks in the refclk domain (data changes only on their low phase; TX
  inputs sampled mid-period) — consumers may use them as enables.

## Bit order — now RTL-confirmed (closes this bench's old TODO(A5))

LSB-first within each byte on both sides, per IEEE 802.3 Annex 22B:
RMII dibit0 = bits[1:0]; MII nibble0 = bits[3:0]; nibble = {dibit1,
dibit0}. This matches `tests/common/rmii.py`'s driver/monitor convention
exactly, so the bench's round-trip now checks the real contract, not
merely driver/monitor self-consistency.

## v1 simplifications pinned by the RTL header (not bugs)

- CRS_DV = plain frame-envelope valid (no 25 MHz CRS/DV toggling phase).
- No preamble insertion/stripping (transparent converter; MACs own it).
- 100 Mb/s full-duplex only; no 10 Mb/s repeat, COL, or RX_ER generation.
- Odd trailing dibit (non-whole-nibble frame) is truncated.
- Dibit/nibble phase locks to the tx_en/crs_dv assertion edge (= RMII
  preamble alignment).
