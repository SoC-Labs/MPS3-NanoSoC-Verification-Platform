# DUT notes — `link_partner_mac` (paired with `rmii_phy_if`)

RTL: `fpga/ethernet/link_partner_mac/link_partner_mac.sv`, module
`link_partner_mac` (no parameters) — real RTL since W-RTL-ETH (2026-07-06),
hand-rolled minimal MAC (see the block README for the vendoring decision).
Proven green under VCS 2022.06-SP2 + cocotb 2.0.1, 3/3, 2026-07-06.

**This bench elaborates a PAIR, not a leaf**: `tb_link_partner_pair.sv`
(bench-local wrapper, same precedent as `tests/sim_smoke/counter.sv`) wires
`link_partner_mac` to `rmii_phy_if` exactly as `fpga/ethernet/README.md`'s
datapath does, and the tests touch only the outer interfaces:

- RMII partition pins (`phy_rmii_*`, bench plays the DUT MAC), and
- the MAC's AXI-Stream ports (`m_axis_rx_*`/`s_axis_tx_*`, bench plays the
  bridge).

Rationale: the MAC's MII ports are only meaningful against `rmii_phy_if`'s
pacing-clock contract (the divided `mii_*_clk` phases, nibble order), so a
leaf-level MII bench would just re-mock that contract; pairing tests it.

## Gating

`tests/common/list_benches.py` gates this bench on `link_partner_mac.sv`
being de-stubbed (its schema is one dir per bench); the test file's own
`NO_RTL` additionally requires `rmii_phy_if.sv` de-stubbed, since the
wrapper elaborates both.

## Behaviour pinned by this bench

- AXIS frames **include the FCS** in both directions (convention shared
  with gen_checker + tests/common/frames.py).
- TX inserts 7×0x55 + 0xD5 preamble/SFD; RX strips them; byte content is
  otherwise untouched (no FCS insert/strip — check-only).
- `m_axis_rx_tuser` = the MAC's independent FCS verdict (0 good / 1 bad),
  delivered on the tlast beat; a bad-FCS frame is still delivered intact.

## Known v1 simplifications (documented in the RTL header — not bugs)

- Single-clock design: `clk_i` must be the same 50 MHz net as
  `rmii_phy_if.refclk_i`; the MII clocks are used as enables (no CDC).
- TX is single-frame store-and-forward (2 KB, oversize dropped whole).
- RX has NO FIFO: if `m_axis_rx_tready` is low when the next byte
  completes (every 4 clk), the unconsumed byte is dropped and the frame's
  tuser set. A6 flag: v2 wants a small RX FIFO. The bench's monitor is
  always-ready so this path is not exercised here.
- No length policing (gen_checker owns the 64..1518 envelope), no address
  filtering (deliberately promiscuous), no padding/half-duplex.
