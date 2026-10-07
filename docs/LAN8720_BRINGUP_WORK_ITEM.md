# Work item — LAN8720 on MPS3: what is actually broken, and what this harness can add

> **Status: HISTORICAL** — a record of the LAN8720 bring-up work item as first proposed as of 2026-07-10.
> Superseded by / current state in [docs/STATUS.md](STATUS.md).
> Kept for provenance; do not update.
>
> **⚠ ITS DIAGNOSIS WAS WRONG. Do not act on §2, §3 or §4-T1.** The
> "SH0 level shifter passes FPGA→PHY but is dead on the PHY→FPGA return"
> conclusion, the auto-direction-translator hypothesis, and the SH1/bank-94
> experiment it motivates were all **refuted in July 2026**: the only part on
> the RMII path is a passive `SN74TVC16222` pass-FET array with no enable and
> no Mbps rating, inbound MHz-class edges were *measured* traversing it, and
> the actual fault was that the module sat on **J24**, not J28. The live
> record, with the arithmetic and the retraction table, is
> [docs/planning/REALPHY_RATE_DECISION.md](planning/REALPHY_RATE_DECISION.md) §0.
> §5 and §5b (the `mode_speed` defect and the MDIO bit-misalignment) still
> stand — those were proved in simulation and are unaffected.

**Status:** proposed · **Created:** 2026-07-10 · **Owner:** unassigned
**Supersedes:** the first draft of this file, which was wrong on two counts (see §0).

---

## 0. Two corrections to the record, up front

**(a) The MPS3 *does* have PMOD connectors.** `docs/CONNECTOR_SURVEY.md` §1.4 states flatly
*"There is no PMOD connector on HBI0309C."* **That is wrong.** The board carries **J28 (Pmod 0/1)**,
**J34 (Pmod 2/3)** and **J38**, which expose the SH0/SH1 shield channels through a **5 V level-shifter
bank** (`SH0_5V_IO[..]`, TRM Table A-13) and `SN74TVC16222` bus switches. Evidence: a *working, committed*
MPS3 constraint file —
`nanoSoC-refactor/ethernet-subsystem-ahb/fpga/targets/arm_mps3/ethernet_subsystem.xdc`
— headed *"ARM MPS3 with LAN8720 PHY on Pmod 0/1 (J28)"*.
`CONNECTOR_SURVEY.md` should be corrected.

**(b) This is not an unstarted bring-up.** A real LAN8720 has been run on both a PYNQ-Z2 (working) and
an MPS3 (failing), and the MPS3 fault has been **localised**. The authoritative record is
`nanoSoC-refactor/ethernet-subsystem-ahb/docs/mps3_ethernet_debug_plan.md`.

---

## 1. What is proven, on which board

From `mps3_ethernet_debug_plan.md` "What we know works":

| Test | Pynq-Z2 | MPS3 |
|---|:---:|:---:|
| MDIO BMSR / PHYID reads | yes | **yes**¹ |
| Firmware boot, UART banner, no HardFault | yes | yes |
| MAC internal loopback (MODER bit 7) | yes | yes |
| PHY near-end loopback (BMCR bit 14) | yes | yes |
| `arping` host↔board | yes | **no** |
| `tcpdump` sees any frame from the board | yes | **no** |

¹ superseded by the 2026-04-22 session below, which found MDIO **reads** return `0xFFFF`.

**So the digital path — MAC, RMII bridge, PHY digital core — is proven.** The Z2 works end to end.

---

## 2. The MPS3 root cause (already diagnosed, 2026-04-22)

Proved live with SWD + OpenOCD against the running MPS3:

1. Firmware healthy; MAC `MODER = 0x0000a403` (TX+RX enabled, full duplex).
2. **MDIO *write* works.** `BMCR.RESET=1` makes the LAN8720's LEDs blink. FPGA→PHY signalling is
   electrically fine.
3. **MDIO *read* is dead.** `BMSR` returns `0xFFFF` at **every** PHY address 0–31. During a read the PHY
   must drive the bus back during turn-around; those PHY→FPGA drives never arrive.
4. **MAC never receives a frame.** The host `arping`s; `tcpdump` confirms the ARP broadcasts go down the
   cable. All 124 RX buffer descriptors stay `EMPTY=1`; `INT_SOURCE` never flips RXB.
5. **The PHY's line side is fine.** Its LEDs show link + activity; it auto-negotiates with the host
   dongle across the cable.

> **Conclusion, quoted:** *"a single physical fault: the SH0 level shifter channel(s) between PMOD J28
> and the FPGA are passing FPGA→PHY bits cleanly but are dead on the PHY→FPGA return."*

That asymmetry breaks **every input** — `RXD[1:0]`, `CRS_DV`, **`REF_CLK`**, and the MDIO read phase —
while leaving **every output** — `TXD[1:0]`, `TX_EN`, `MDC`, MDIO write — intact.

### Already eliminated
4 cables · 2 LAN8720 breakouts · 3 host USB-Ethernet dongles · J38→J28 reroute · firmware BMCR/ADVERTISE
matched to the Z2's post-strap state · XDC fixes (GC-capable clock pin, removed stray
`CLOCK_DEDICATED_ROUTE FALSE`, added `SLEW FAST DRIVE 16 IOB TRUE` on the TX pins).

### Already eliminated on the MCC side
`FPGA_SCC: FALSE` (matches the known-good AN522 config) · `UARTMODE: 0` · wrapper holds `SH_nRST = 1'b1`
so the `SN74TVC16222` bus switches stay enabled · `ASSERTNPOR: TRUE`. Per TRM §A.6 the shifter direction
on the C-variant is **auto-sensing** and is *not* configurable from the SD-card config files.

---

## 3. The invariant nobody has broken yet

Every failing configuration shares **the SH0 shifter bank**. The reroute from J38 to J28 moved pins
*within SH0*. **SH1 (bank 94) has never been tried.**

That yields the cheapest discriminating experiment available, and it is the top task below.

There is also a strong architectural hypothesis worth stating: an **auto-direction** translator
(TXB-class) cannot reliably pass a **50 MHz inbound clock**. `REF_CLK` is an FPGA *input* in this design
(the LAN8720 is strapped REF_CLK-**out**). If the shield path simply cannot carry 50 MHz inbound, then
**no re-pinning fixes it** and the shield is not a viable RMII carrier at all — which would retroactively
vindicate `docs/DUT_ETHERNET_EGRESS.md`'s Option C (virtual PHY in fabric, share the LAN9220).

Note the direction the board *does* pass — FPGA→PHY — is exactly the direction this harness's contract
already assumes: `partition-pins.md:57` has **the shell sourcing `phy_rmii_ref_clk`** (DUT = ref-in).
Strapping the LAN8720 for REF_CLK-**in** removes the hardest inbound signal. It does not remove inbound
`RXD`/`CRS_DV`, so it is a partial mitigation, not a fix.

---

## 4. Tasks

### T1 — Move RMII to SH1 / bank 94 (the untried experiment) — **highest value**
Re-pin the RMII bus onto `SH1_IO[*]` (bank 94), with `REF_CLK` on a bank-94 **GC** pin.
GC pins per `fpga/shell/build_results_2026-07-10/post_impl_io.rpt`: bank 84 = `SH0_IO[0..3]`
(AW14/AW13/AW15/AY15); bank 94 = `SH1_IO[13..16]` (AW16/AY16/AY18/AY17).
- **Exit:** MDIO `BMSR` returns something other than `0xFFFF`. If it does, the SH0 shifter bank is the
  fault and the problem is a board repair. If it does not, the shield path is architecturally unable to
  carry inbound RMII and we stop pursuing a shield-mounted PHY.
- Note the current MPS3 XDC already puts `phy_rmii_ref_clk` on **AW14 = SH0_IO[0]**, a bank-84 GC pin — so
  the GC requirement is already satisfied and is *not* the bug.

### T2 — Strap the PHY for REF_CLK-in and retest
FPGA drives 50 MHz out (the direction that works); PHY's crystal disabled. Preserves this repo's
partition-pin contract unchanged.
- **Exit:** does `CRS_DV`/`RXD` inbound come alive once the clock is no longer inbound? Isolates
  "50 MHz inbound clock" from "inbound data" as the failing case.

### T3 — Pin-level shifter characterisation, using this harness
This is what the new harness uniquely adds. Once `board_gpio` reaches the shield header
(`CONNECTOR_SURVEY.md` §6 + the pin plan in this repo), firmware **owns** those pads through the OWN mux
and can drive/sample each one individually.
- Drive a slow pattern out on a shield pin, loop it back externally, read it in. Sweep frequency until
  the inbound path dies. **That measures the shifter's inbound bandwidth directly, with no PHY, no MAC,
  no MDIO.**
- Report the corner frequency. If it is below 50 MHz, §3's hypothesis is proved and the shield is closed
  as an RMII carrier.
- *Blocked on:* the shield-GPIO static rebuild (batches with decoupler / CSR-decode / `WDOG_RREQ` / CLCD).

### T4 — Networked visibility
Put an ILA on the RMII inputs and reach it through the shell's **Debug Bridge / XVC on TCP 2542**, so the
capture happens over Ethernet with no JTAG cable at the bench. Surface PHY MDIO status
(`BMSR`, `ANLPAR`, LAN8720 reg 31 speed/duplex) on TCP 6900, and on the CLCD once that lands.

### T5 — Only if T1/T2 succeed: MPS3 carrier integration
~10 pads, bank 94, static rebuild, re-mints `static_id`. Reopens `DUT_ETHERNET_EGRESS.md` Option A.

---

## 5. A real defect in the *multicore* wrapper — now PROVEN IN SIMULATION

> **Reproduced** by `tests/rmii_speed/` (VCS, bridge md5 `5e38c43c`). Falsification test with a
> positive control:
>
> | `mode_speed` | `mrx_clk` | nibbles seen | SFD found | payload recovered | verdict |
> |---|---|---|---|---|---|
> | `1` (correct) | 40.0 ns (25 MHz) | 48 | yes | **16/16** | PASS |
> | `0` (what ships) | 400.0 ns (2.5 MHz) | **5** | never | **0/16** | FAIL |
>
> At the 10 Mb/s divider the bridge samples a tenth of a 100 Mb/s stream and never
> synchronises. See `tests/rmii_speed/RESULT.txt`.
>
> Note `tests/rmii_conformance/` **cannot** show this: it compiles a different copy of the
> bridge (md5 `5dd9b16f`) whose header reads *"Removed the mode_speed input -- hard-wired to
> 100 Mbps operation"*. Its bit-order conformance result stands; it never touched the divider.



`nanosoc_multicore_vivado_wrapper.v:117-134` instantiates `rmii_to_mii` **directly** and does **not**
connect `.mode_speed`. Verified in the shipped build:

- `WARNING: [Synth 8-7071] port 'mode_speed' of module 'rmii_to_mii' is unconnected for instance
  'u_rmii_bridge'` — `nanosoc_multicore_project.runs/..._ip_0_0_synth_1/runme.log:130`
- `speed_div_reg[3:0]` **survives into the routed netlist**, which it could not if `mode_speed` had
  resolved to `1` (line 145 would hold the counter at zero). So Vivado tied the undriven input to **0**.
- `rmii_to_mii.v:152` — `tick = mode_speed | (speed_div == 4'd0)`; header: *"1 = 100 Mbps, 0 = 10 Mbps"*.
  ⇒ the bridge runs its **10 Mbps** divider.
- Firmware writes `ANAR = 0x0181` (`ptp_slave/main.c:178`) = 100BASE-TX + 100BASE-TX-FD, **10BASE-T not
  advertised** — the code comment claiming "+ 10BASE-T" is wrong. So the PHY negotiates **100 Mbps**
  against a bridge clocked for 10.

The **standalone** subsystem does not have this bug: `ethmac_ahb_rmii.v:54,87` declares and forwards
`mode_speed`, and `eth_speed_ctrl.v` exists to drive it. The Z2 result the team relies on comes through
that path.

**Consequence:** the multicore integration's Ethernet cannot be trusted even once the MPS3 shifter
question is settled. Fix by instantiating `ethmac_ahb_rmii` (or wiring `mode_speed` from the MAC's
negotiated speed) — and add an **RMII-boundary test**, because no test in either repo sits there: cocotb
injects at the **MII** boundary (`soc_ethernet/test_soc_ethernet.py`), downstream of the bridge, and the
one UVM env that does exercise it hard-drives `.mode_speed(1'b1)` — the very value the integration fails
to supply.

> This harness owns the missing model: `fpga/ethernet/rmii_phy_if/` (real RMII⇄MII) and
> `fpga/ethernet/mdio_phy_model/` (Clause-22 MDIO slave + PHY register model). Driving the multicore's
> `rmii_to_mii` against them in cocotb exposes `mode_speed = 0` on the first frame, with no board.

---

## 5b. NEW finding — the virtual PHY is not MDIO-interoperable with the real MAC

`tests/lan8720_sut/` (built 2026-07-10) drives the **real** ethernet-subsystem DUT
(`ethmac_ahb_rmii`, OpenCores `eth_miim` MDIO master) through `shield_path` to this repo's
`mdio_phy_model`. It **reproduces the board's dead-inbound signature** — MDIO reads `0xFFFF`
at all 32 PHY addresses — through the real MDIO master (VERDICT=PASS).

It also surfaced a bug the model's self-bench (`tests/mdio_phy_model/`) could not:

> **`mdio_phy_model`'s MDIO frame is bit-misaligned vs the real `eth_miim`.** Healthy reads come
> back **right-shifted by one bit** (PHYID1 `0x0007`→`0x0003`, PHYID2 `0xC0F1`→`0x6078`, ANAR
> `0x01E1`→`0x00F0`, each `>>1`), and **writes do not commit**. The model's self-bench used a
> matched hand-rolled master, so the off-by-one cancelled and was invisible.

**This is a live risk for `DUT_ETHERNET_EGRESS.md` Option C**, which faces `mdio_phy_model` at
the real nanoSoC MAC. As-is, the DUT would read shifted garbage and its PHY writes would no-op.
Caught in sim before any board. Fixing the shared model (its own bench + other users) is out of
scope for this SUT; flagged for the model's owner. Evidence: `tests/lan8720_sut/RESULT.txt`.

## 6. Open questions

1. Does SH1 / bank 94 behave differently from SH0? (T1 — nobody has tried.)
2. What is the inbound bandwidth of the MPS3 shield translator? (T3 — measurable here.)
3. Is `SN74TVC16222` the only device in the path, or is there an auto-direction translator in front of
   it? TRM §A.6 says "auto-sensing", which a passive FET bus switch is not. **Read the schematic.**
4. Is the fault a *failed* shifter IC on this particular board (repairable) or the *architecture*
   (not)? T1 discriminates.
5. `CONNECTOR_SURVEY.md` §1.4 needs correcting, and `DUT_ETHERNET_EGRESS.md:59` calls the shield
   LVCMOS18 while every constraint file says LVCMOS33.
