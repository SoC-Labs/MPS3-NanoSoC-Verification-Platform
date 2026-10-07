# MAC-in-operation verification subsystem (`fpga/ethernet/`)

Implements spec §8 ("Ethernet MAC-in-operation verification subsystem"):
the DUT's Ethernet MAC is verified as a black box, driven through its real
RMII interface, with this subsystem acting as the shell-side "virtual PHY"
+ link partner. All of it lives in the **static shell** (spec §8.3: "keep
the whole link partner + virtual PHY in the static shell, behind the
Shutdown Manager, so it persists across DUT swaps").

**Status (W-RTL-ETH, 2026-07-06): five of six blocks are real, benched RTL**
(`mdio_phy_model/`, `rmii_phy_if/`, `link_partner_mac/`, `bridge/`,
`gen_checker/` — all green under VCS + cocotb 2.0.1, verilator `-Wall`
clean); only `lan9220_if/` remains a stub by decision (AXI-EMC vendor cell
+ firmware own that path — see its README status note and the wiring
caution there). One planning note did NOT survive contact with
implementation: spec §12's "everything else is reuse/assembly of existing
open-source cores" — no verilog-ethernet/verilog-axis/LiteEth checkout
exists on this machine and the repo vendors no third-party RTL, so
`rmii_phy_if`/`bridge`/`link_partner_mac` were hand-rolled instead (small,
policy-clean; full vendoring rationale in `bridge/README.md`, which the
other two reference).

## Datapath (spec §8.2)

```
DUT MAC (RP, via partition pins)
  ⇄ RMII + MDIO ⇄ rmii_phy_if/ + mdio_phy_model/   (virtual PHY, this subsystem)
  ⇄ link_partner_mac/                              (recovers frames as AXI-Stream)
  ⇄ bridge/                                        (3-port: mgmt / DUT-MAC / LAN9220-uplink)
  ⇄ lan9220_if/                                    (AXI ⇄ LAN9220 static-memory-bus)
  ⇄ LAN9220 (promiscuous) ⇄ RJ45 ⇄ host/network
```

`gen_checker/` taps into this datapath (recommended, spec §8.1) to inject
malformed frames toward the DUT and independently score the DUT's TX
framing/CRC + RX handling — this is what makes it MAC *verification* rather
than "it pings" (spec §8.4).

## Subdirectory inventory

| Subdir | Status (W-RTL-ETH) | Function | Reuse — planned (spec §12) vs actual | Regmap |
|---|---|---|---|---|
| `rmii_phy_if/` | **real**, bench 2/2 | RMII (DUT-facing, 2-bit@50MHz) ⇄ MII (shell-facing) datapath conversion; forwards the 50 MHz REF_CLK (refclk_i must be 50 MHz — decision in its README); **the HDPR-29 RMII-TX re-register stage lives here** | planned LiteEth `rmii.py` (BSD-2); **actual: hand-rolled** (vendoring note, top of this file) | none (pure datapath) |
| `mdio_phy_model/` | **real**, bench green | MDIO (MDC/MDIO) slave + PHY register model (BMCR/BMSR/PHYID/ANAR); without it the DUT's PHY bring-up/auto-neg poll hangs (spec §8.1) | freecores/sgmii `mdio_slave.v` pattern; RTL is custom | `VPHY` @ `0x44A3_0000` |
| `link_partner_mac/` | **real**, bench 3/3 (paired with rmii_phy_if) | Recovers DUT frames as AXI-Stream (+FCS-check tuser tap); frames AXIS packets toward the DUT; the on-chip "link partner" | planned verilog-ethernet `eth_mac_mii` (MIT); **actual: hand-rolled** minimal MAC glue (AXIS frames carry FCS; see its README) | none |
| `bridge/` | **real**, bench 3/3 | 3-port store-and-forward bridge: management (MicroBlaze/lwIP) / DUT-MAC / LAN9220-uplink, dest-MAC parse + 2-entry compile-time table, flood-on-miss (spec §9, D9) | planned Forencich `axis_switch` (MIT); **actual: hand-rolled** (decision record in its README) | none — register-less confirmed (I11, v0.2) |
| `lan9220_if/` | stub **by decision** (see its README status note + wiring caution) | AXI ⇄ LAN9220 register/FIFO static-memory-bus interface | Xilinx AXI EMC vendor cell (W-BD owns in the BD); driver ported from Zephyr `eth_smsc911x.c` (Apache-2.0) — W-SMSC | none (paired with `axi_emc_0` vendor cell in `shell_bd.tcl`) |
| `gen_checker/` | **real**, bench 11/11 | Error-inject traffic gen/checker: bad FCS, runts, giants, IFG violations, dribble; independent TX/RX scoring + counters | custom RTL (sim behavioural spec: `tests/common/frames.py`) | `GENCHK` @ `0x44A6_0000` — **full v0.2 table implemented** |

## Clocking / gotchas (spec §8.3, §16)

- **Shell sources the 50 MHz RMII reference** (`phy_rmii_ref_clk`, a
  partition pin, shell → RP). Configure the DUT MAC for ref-**in** — never
  let both sides source it. Sourced in `rmii_phy_if/`.
- **LAN9220 must be promiscuous** to carry the DUT's MAC address as well as
  the management stack's — it's 10/100 and shared, so this is functional
  testing, not a throughput benchmark.
- **CDC (v1 clocking as implemented):** the whole subsystem datapath runs
  in ONE fixed 50 MHz shell domain — `rmii_phy_if.refclk_i` (must be
  50 MHz, forwarded as `phy_rmii_ref_clk`) is also `link_partner_mac.clk_i`,
  and the MII "clocks" between them are divide-by-2 pacing flops used as
  enables, so there is no crossing inside the pairing. The DUT MAC's RMII
  timing is synchronous to the shell-sourced REF_CLK by construction (RMII
  spec); the HDPR-29 re-register stage in `rmii_phy_if/` is the single
  static-side sampling stage at the boundary. If a future clocking plan
  runs the DUT's RMII logic on an unrelated clock, a real 2-FF/FIFO CDC
  must be added ahead of that stage (flagged in the RTL header, A6).
- **HDPR-29 (Z2 lesson):** pin-facing/boundary-facing output registers that
  want `IOB TRUE` packing must be static-side, never RM-side. The RMII TX
  re-register stage — the first flop stage on `phy_rmii_txd`/`phy_rmii_tx_en`
  immediately after they cross the partition boundary — lives in
  `rmii_phy_if/rmii_phy_if.sv`. See that subdirectory's README for detail.

## Ambiguities for A6 (updated W-RTL-ETH — the two Phase-0 ones narrowed)

1. **Bridge control surface — settled register-less for v0.1/v0.2**
   (`shell-regmap.md` v0.2: "confirmed intentional", OPEN_ISSUES I11). The
   landed RTL matches: compile-time 2-entry table (module parameters),
   flood-on-miss. Residual watch-item only: the DUT MAC changing between
   RMs makes post-swap DUT traffic flood until the parameter is rebuilt —
   if that noise or MicroBlaze visibility ever matters, a regmap block
   reopens I11.
2. **GENCHK register layout — now IN the contract** (v0.2 table:
   CTRL/INJECT/TX_CNT/RX_CNT/ERR_CNT) and implemented by the RTL. What
   remains of I10: (a) counter-CLEAR semantics are an RTL v1 choice
   (clear-on-enable-rise) the contract doesn't state — codify or amend;
   (b) no `net-protocol.md` verb drives GENCHK yet (`{"op":"macgen",…}`,
   Phase 5); (c) stale draft constants linger in `tests/common/regmap.py`
   and `firmware/common/platform_regs.h` — update to v0.2.
3. **link_partner_mac RX has no FIFO** (v1): a frame arriving while the
   bridge's dut_mac ingress is parked forwarding is dropped-and-flagged
   per-frame (documented in its README). Decide if v2 needs a small FIFO.
4. **lan9220_if stays a stub** pending W-BD's `axi_emc_0` + W-SMSC driver;
   do not wire the bridge uplink to it yet (wedge risk — see its README).
