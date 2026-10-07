# DUT notes — `eth_mac_test_subsystem` (the §8 integration bench)

RTL: `fpga/ethernet/eth_mac_test_subsystem.sv`, module
`eth_mac_test_subsystem` (GREENFIELD, W-ETH-SS, A1/A5). Integrates the five
real ethernet blocks into the ARCHITECTURE_SPEC.md §8 MAC-in-operation
datapath (roadmap step 9). Elaborated together with all five blocks; VCS
2022.06-SP2 + cocotb 2.0.1.

**This bench elaborates the WHOLE subsystem, not a leaf.** `TOPLEVEL =
eth_mac_test_subsystem`; the Makefile lists all five block sources plus the
integration file (+incdir for `mdio_phy_model`'s two `include'd helpers).
`list_benches.py` gates on the subsystem file being de-stubbed; the test's own
`NO_RTL` additionally requires every wired block de-stubbed (same pattern as
`tests/link_partner_mac`).

## What the bench drives (the DUT MAC, the host, the MicroBlaze)

- **DUT MAC**, on the RMII + MDIO partition pins: `RmiiFrameDriver` on
  `phy_rmii_txd/tx_en` (DUT TX), `RmiiFrameMonitor` on `phy_rmii_crs_dv/rxd`
  (DUT RX), `MDIOMaster` on `mdc/mdio_o/mdio_oe/mdio_i` (DUT is the MDIO
  master). All the hand-rolled `tests/common` helpers — cocotbext-eth is not
  installed.
- **Host / LAN9220 uplink**: `AxisByteDriver`/`AxisByteMonitor` on
  `uplink_s/uplink_m`.
- **MicroBlaze control**: `AxiLiteMaster` on the VPHY (`s_axi_vphy_*`,
  0x44A3) and GENCHK (`s_axi_genchk_*`, 0x44A6) surfaces.

## Clocking — genuinely different periods where CDC exists (spec §5)

| Clock | Period | Domain |
|---|---|---|
| `refclk_i` | 20 ns (50 MHz) | RMII ref + whole datapath (rmii_phy_if, link_partner_mac, bridge, gen_checker incl. its GENCHK AXI-Lite) |
| `s_axi_vphy_aclk` | 10 ns (100 MHz) | VPHY AXI-Lite; crosses into `mdc` via 2-flop syncs inside `mdio_phy_model` — the genuine CDC |
| `mdc` | 400 ns (2.5 MHz) | DUT-driven MDIO clock (bit-banged) |

Three unrelated periods, so the VPHY AXI⇄mdc synchronizers run at a real
clock ratio (not a same-clock shortcut). The datapath itself is one 50 MHz
domain by design (`fpga/ethernet/README.md` "Clocking / gotchas").

## Interface reconciliations this bench exercises (subsystem header R1–R5)

- **R1 checker tap gate** — `gen_checker.chk_s` is a constant-`tready` sink;
  the subsystem gates `chk_s_tvalid = mac_rx_tvalid & bridge_dut_mac_tready`
  so the checker counts only *committed* beats (a raw fan-out would double-
  count during bridge backpressure). Scenario (d)/(e) prove RX_CNT advances
  once per DUT frame.
- **R2 TX arbiter** — a packet-locked 2:1 AXIS arbiter merges the bridge's
  dut_mac egress (host→DUT) and `gen_checker.gen_m` (injection) into the one
  `link_partner_mac.s_axis_tx`. Scenario (b) host→DUT uses input A; scenario
  (d) generator uses input B.
- **R3 reset polarity** — datapath blocks take active-high `rst_i`;
  gen_checker/mdio take active-low `aresetn` (gen_checker's derived as
  `~rst_i`, VPHY keeps its own async AXI reset).
- **R4 clock fusion** — gen_checker's only clock is its `s_axi_aclk`, so
  GENCHK AXI-Lite runs on `refclk_i` here (the GENCHK BFM is bound to
  `refclk_i`, not a separate aclk). *A real shell needs an AXI clock-converter
  in front of GENCHK — flagged for A6/W-BD.*
- **R5 HDPR-29** — the RMII-TX re-register stage stays inside `rmii_phy_if`
  (static-side); the subsystem never re-registers the pins itself.

## Bench techniques worth knowing

- **Checker-scoring frames use `dst = DUT_MAC`** so the bridge routes them to
  their own ingress and *drops* them (no slow flood-forward), keeping the
  bridge's dut_mac ingress ready for the next frame. The checker still counts
  them (its tap is at ingress). Back-to-back DUT frames faster than the bridge
  drains would be lost (link_partner_mac has no RX FIFO, bridge is single-
  frame S&F) — the bench waits for RX_CNT to move between frames.
- **Pulse observation uses a no-preamble MDIO read** (`_mdio_read_short`):
  the `LINK_EVENT.pulse` hold is 32 mdc cycles, which a normal 64-cycle read
  (32-bit preamble) outlasts; the 32-cycle no-preamble read samples BMSR
  inside the hold. `mdio_slave.sv` documents tolerating zero preamble.

## lan9220_if

Not instantiated (stub by decision — see its README). The LAN9220 uplink is a
plain AXI-Stream port pair (`uplink_*`) for W-BD to attach the `axi_emc_0`
vendor cell + the ported smsc911x driver.
