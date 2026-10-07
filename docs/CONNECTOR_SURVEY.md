# MPS3 (HBI0309C) connector survey — where can `dut_gpio` actually go?

> **Status: HISTORICAL** — a record of the MPS3 (HBI0309C) connector survey for `dut_gpio` routing as of 2026-08-07.
> Superseded by / current state in [docs/STATUS.md](STATUS.md).
> Kept for provenance; do not update.

**Scope:** read-only survey. No RTL/XDC is changed by this document; the XDC in
§6 is a **proposal only**. Question asked: the RP boundary carries
`dut_gpio_o/oe/i[15:0]` and the shell instantiates `board_gpio` to broker them
onto board pads — so *which physical connector, if any, can the DUT's GPIO reach
on this carrier, and at what cost?*

**Evidence base (all first-hand, cited inline):**
- Board config `.txt` files under `~/MPS3/Corstone-700/Boardfiles/`
  (board-data only — no Arm IP/source was read).
- This repo's own already-on-silicon pin collateral:
  `fpga/monolithic/nanosoc_mps3.xdc` (the legacy Arm-MPS3 pinmap, "same
  PACKAGE_PIN letter-codes … copied verbatim", proven on `xcku115-flvb1760-1-c`)
  and `fpga/shell/constraints/mps3_harness.xdc`.
- `fpga/shell/shell_top.sv`, `fpga/shell/ip/board_gpio/`, `fpga/dfx/dfx_floorplan.xdc`.
- Vivado 2024.1 package-pin bank queries against `xcku115-flvb1760-1-c`.

---

## 0. Bottom line (blunt)

1. **CORRECTED (2026-07-10, per bench):** an earlier revision of this survey
   claimed "the MPS3 carrier has NO PMOD." **That is wrong.** The board carries
   **Pmod connectors — J28 (Pmod0/1), J34 (Pmod2/3), J38 — located under/behind
   the Arduino-style shield headers.** They and the shield headers
   (`SH0_IO[17:0]` + `SH1_IO[17:0]`, 36 single-ended IO, LVCMOS33) expose the
   **same SH0/SH1 shield-channel nets**, tapped through a 5 V level-shifter bank
   (`SH0_5V_IO[..]`, TRM Table A-13) and `SN74TVC16222` bus switches — so "Pmod"
   and "shield header" are two physical access points to one electrical net set.
   Everything below at the SH0/SH1 net/pin level therefore stands; only the
   "no Pmod" framing was wrong. (The board also has an **FMC** site, present but
   **not pinned** in any collateral — see §1.3.) The LAN8720 bring-up
   (`docs/LAN8720_BRINGUP_WORK_ITEM.md`) uses **Pmod0 = the SH0 channel**; note
   its wiring spills **one TX bit onto a separate Arduino-header pin** (bench
   detail, document with the wiring — see the allocation note below / §7).
2. **The DUT's GPIO does NOT reach any expansion connector today — but it is
   not un-wired either.** `shell_top.sv` hard-wires the 16 `board_gpio` pads to
   the **on-board user LEDs (bits [7:0], output-only) and DIP switches (bits
   [15:8], input-only)**. So `dut_gpio[7:0]` can blink 8 LEDs and
   `dut_gpio[15:8]` can read 8 switches — real physical I/O, but **fixed-function
   soldered parts, split output-only / input-only, not a pluggable or
   bidirectional header.** There is no top-level `board_pad_*` port at all.
3. **The RP-pblock IOB-exclusion reasoning checks out.** The shield/LED/switch
   pins live in *static* I/O banks (84/94/44); the RP pblock contains **zero
   IOB sites** by construction, so a connector pin can never be pinned inside
   the RP — it must be a static pad bridged to the RP over the `dut_gpio_*`
   partition pins. **That is exactly why `board_gpio` exists.** Verified in §5.
4. **Giving `dut_gpio` a real, wire-able connector is a *static* change** (add a
   `shell_top` port + IOBUFs + XDC, re-point `board_gpio`'s pad group), which
   **re-mints `static_id`** — so it is batched with the next static rebuild, not
   done here. Exact XDC in §6.

---

## 1. What connectors does HBI0309C actually have?

`~/MPS3/Corstone-700/Boardfiles/MB/HBI0309C/board.txt` →
`BOARD: HBI0309C … Motherboard configuration file`. HBI0309C is the **MPS3
motherboard** (Rev C). Its FPGA is the KU115 this platform targets. The
connector inventory below is taken from the board `.txt` files and from the
proven legacy pinmap `fpga/monolithic/nanosoc_mps3.xdc` (which enumerates every
board port the Arm MPS3 wrapper exposes).

### 1.1 Arduino-style shield headers — the real GPIO expansion  ✅

`fpga/monolithic/nanosoc_mps3.xdc` §"Arduino-style shield header SH0_IO/SH1_IO"
(lines 404-489) pins two 18-bit single-ended IO banks plus a shield reset and an
on-shield ADC SPI:

| Signal | Width | IOSTANDARD | I/O bank (queried) | Role |
|---|---|---|---|---|
| `SH0_IO[17:0]` | 18 | LVCMOS33 | **84** | Shield digital IO group 0 |
| `SH1_IO[17:0]` | 18 | LVCMOS33 | **94** | Shield digital IO group 1 |
| `SH_nRST` | 1 | LVCMOS33 | 84 | Shield reset (AU14) |
| `SH_ADC_CS/CK/DI/DO` | 4 | LVCMOS18 | 67 | On-shield ADC SPI |

This is the standard Arm MPS3 "shield" expansion (Arduino-form-factor headers).
It is the **only broadside general-purpose IO connector** on the board, and the
one `board_gpio.sv`'s own README already names ("PMOD/**Arduino**, spare FMC").
36 digital IO total; all 3.3 V.

### 1.2 On-board fixed-function I/O (NOT connectors, but physical)

- 8× user LEDs `USER_nLED[7:0]` (active-low), 8× DIP switches `USER_SW[7:0]`,
  2× push-buttons `USER_nPB[1:0]` — bank **44**, LVCMOS18
  (`mps3_harness.xdc` lines 65-89, `nanosoc_mps3.xdc` 192-231).
- 20-pin CoreSight debug header `CS_*` (JTAG-DP + 16-bit trace), bank ~ high-BB
  region (`nanosoc_mps3.xdc` 137-186). Not usable for DUT GPIO.
- Special-function interfaces (not GPIO): LAN9220 SMC Ethernet, USB debug FIFO,
  CLCD, HDMI CEC/DDC, Audio codec, eMMC, microSD (USD), MMB video, QSPI flash,
  MCC SMB/SCC. All pinned in `nanosoc_mps3.xdc`; none is a general IO header.

### 1.3 FMC — present on the board, NOT pinned in any collateral  ⚠️

`~/MPS3/Corstone-700/Boardfiles/config.txt` carries
`FMC_FORCE: FALSE  ;Force FMC power ON` — that option only exists because the
**board physically has an FMC (HPC/LPC) site**. However:
- **No FMC pins appear in any XDC** in this repo or the legacy Arm-MPS3 target
  (`grep` of `nanosoc_mps3.xdc` / `fpga_pinmap.xdc`: no `FMC*` ports).
- The board `.txt` files carry **no FMC pin map** (they are MCC/app-note config,
  not a pinout).
So FMC is a **latent** connector: usable only after (a) obtaining the board's
FMC↔KU115 pinout from the MPS3 TRM (not from the `.txt` files, and explicitly
out-of-scope to read Arm IP for) and (b) writing fresh pin constraints. It is
**not** a quick route for `dut_gpio`.

### 1.4 PMOD — CORRECTED: it *does* exist (J28/J34/J38, under the shields)  ✅

**This section previously said "PMOD does NOT exist on HBI0309C" — that was
wrong.** Pmod connectors **J28 (Pmod0/1), J34 (Pmod2/3), J38** are on the board,
physically **under/behind the Arduino shield headers**, and are wired to the
**same SH0/SH1 shield-channel nets** through the 5 V level-shifter bank and
`SN74TVC16222` bus switches (`docs/LAN8720_BRINGUP_WORK_ITEM.md`; confirmed at the
bench). So a signal constrained to an `SH0_IO[*]`/`SH1_IO[*]` pin appears on
**both** the shield header *and* the corresponding Pmod pin.

The repo's "PMOD" strings are therefore **real**, not aspirational:
- `board_gpio.sv` "PMOD/Arduino header", `shell_bd.tcl` "Board GPIO/PMOD
  passthrough" — the Pmod *is* the Arduino header's SH0/SH1 nets on a different
  connector body.
- The LAN8720 bring-up uses **Pmod0 (= SH0)**; its RMII wiring spills **one TX
  bit onto a separate Arduino-header pin** (bench-reported; capture the exact pin
  in the wiring doc — see §7).

The `docs/MPS3_ONBOARD_FLASH_MAPPING.md` §2.6 "QSPI-on-PMOD → PYNQ-Z2" note is a
*separate* recollection about a different target and does not bear on whether this
board has Pmod headers — it does.

---

## 2. Package pins the shield header maps to

From `fpga/monolithic/nanosoc_mps3.xdc` (proven on `xcku115-flvb1760-1-c`),
`SH0_IO[15:0]` — the 16 pins a 16-bit `dut_gpio` would map to:

```
SH0_IO[0]=AW14  SH0_IO[1]=AW13  SH0_IO[2]=AW15  SH0_IO[3]=AY15
SH0_IO[4]=AY13  SH0_IO[5]=AY12  SH0_IO[6]=BA15  SH0_IO[7]=BB14
SH0_IO[8]=BA12  SH0_IO[9]=BB12  SH0_IO[10]=BA14 SH0_IO[11]=BA13
SH0_IO[12]=BB15 SH0_IO[13]=AU12 SH0_IO[14]=AV12 SH0_IO[15]=AV17
                          ( SH0_IO[16]=AV16  SH0_IO[17]=AT14 )
```
All LVCMOS33, **I/O bank 84** (Vivado-queried on a sample: AW14/AW13/AW15/AY15/
BA12/BB12/AV16/AV17/AT14/AU14 all report `BANK=84`).

`SH1_IO[*]` (the second group, if 36-wide GPIO is ever wanted) is in **bank 94**
(AT17/AU17/BB16 queried `BANK=94`) — pins at `nanosoc_mps3.xdc` 444-461.

---

## 3. How `dut_gpio` is wired **today** (the real state)

Trace: `dut_gpio_o/oe/i[15:0]` (RP boundary)  →  `board_gpio.sv` per-bit
host/DUT mux  →  `board_pad_o/oe/i[15:0]` (module port)  →  BD ports
`board_gpio_pad_o/oe/i[15:0]` (`shell_bd.tcl` 109-111, 548-550)  →
**`shell_top.sv` lines 269-271**:

```systemverilog
wire [7:0] led_drive = board_gpio_pad_o[7:0] & board_gpio_pad_oe[7:0];
assign USER_nLED        = ~led_drive;                 // pads [7:0]  -> LEDs (out only)
assign board_gpio_pad_i = { USER_SW, led_drive };     // pads [15:8] <- switches (in only)
```

So the 16 `board_gpio` pads are **consumed inside `shell_top` as LEDs + DIP
switches** — there is **no top-level `board_pad_*` port**, and `mps3_harness.xdc`
correctly constrains `USER_nLED`/`USER_SW` (bank 44) rather than any
`board_pad_*`. Consequences for the DUT:
- `dut_gpio[7:0]` → 8 LEDs, **output-only** (readback loops the driven value;
  no independent input path — `shell_top.sv:264-265`).
- `dut_gpio[15:8]` ← 8 DIP switches, **input-only** (`pad_o/oe[15:8]` have no pad
  and are intentionally dangling — `shell_top.sv:266-267`).
- No bit is a true bidirectional, externally-wire-able GPIO. The shield header is
  **not** in this path.

This is genuinely useful for a demo DUT (blink a pattern, read a switch — which
is what `rm_led`/`rm_regdemo` do), but it is **not** a connector you can cable an
external device to.

---

## 4. Which bank, and is it "compatible with the RP pblock"?

The shield pins are in HR I/O banks **84 / 94** (3.3 V); the LED/switch pins in
bank **44** (1.8 V). The relevant compatibility fact is **not** "same bank as the
RP" — it is that **all of these are STATIC-shell I/O**, and the RP pblock is
deliberately built to contain **no IOB sites at all**:

`fpga/dfx/dfx_floorplan.xdc` (lines 60-95) builds the pblock from
`SLICE / DSP48E2 / RAMB18 / RAMB36` **site ranges** inside SLR0 clock regions
`{X2Y0 X3Y0 X2Y1 X3Y1}` — *never* whole clock regions — precisely so that no IOB
column is enclosed. Its own comment (verified against the real shell):

> "a whole-CLOCKREGION range includes that region's IOB/config sites, and any
> STATIC I/O buffer physically pinned into those regions then lands 'inside' the
> RP Pblock and **fails DRC HDPR-6** (static logic in the reconfig area) … the
> real shell's `USER_SW/USER_nLED/USER_nPB0` pads [are] legal alongside the
> Pblock since no IOB sites are ranged, **confirmed 0 HDPR violations + clean
> pr_verify** on the real shell."

So the reasoning in the brief is **correct and load-bearing**:
- A static IOB (LED, switch, or shield pin) can **never** be pinned inside the RP
  pblock — DRC HDPR-6 forbids it.
- Therefore every board-facing pin must be a **static pad**, brokered across the
  partition boundary to the RP over the `dut_gpio_*` partition pins.
- **That is the entire reason `board_gpio` lives in the static.** The bank of the
  target connector (84/94/44/…) is irrelevant to the RP — it only has to be a
  free static I/O bank, which banks 84/94 are.

Corollary: re-pointing `dut_gpio` at the shield header is a **static-side edit
only** (a `shell_top` port + IOBUFs + XDC + `board_gpio` rewire). The RP boundary
(`partition-pins.md`) and every RM are **untouched** — no RM re-synth, no change
to the `dut_gpio_*` contract.

---

## 5. What it would cost

A pin change forces a **static rebuild**, and `static_id` = CRC-32 of
`static_routed_locked.dcp` (`build_dfx.tcl`), so the rebuild **re-mints
`static_id` and invalidates every already-shipped partial** for the current
shell. This is why the change is **batched** with the next planned static
rebuild, not applied piecemeal. The RTL side is small:
1. Add a top-level bidirectional port to `shell_top.sv`, e.g.
   `inout wire [15:0] SH0_IO;`.
2. Replace the LED/switch mapping (§3) with per-bit `IOBUF`s so the DUT's
   `o/oe/i` become a real tristate pad (or keep LEDs/switches and *widen*
   `board_gpio`'s `NGPIO` to expose a second 16-bit group — a bigger change).
3. Add the XDC in §6.

---

## 6. PROPOSAL — XDC that WOULD map the DUT GPIO to the shield header

> ⚠️ **Do NOT add these to `fpga/shell/constraints/mps3_harness.xdc` yet.** They
> require the `shell_top.sv` `SH0_IO` port + IOBUFs from §5 to exist first, and
> committing them re-mints `static_id`. Presented here as the target end-state.

Map the DUT's 16 GPIO bits onto `SH0_IO[15:0]` (bank 84, LVCMOS33):

```tcl
##############################################################################
# Shield header SH0_IO[15:0] -> DUT GPIO (via board_gpio, bidirectional IOBUFs
# in shell_top). Bank 84, LVCMOS33. Pins verbatim from the proven Arm-MPS3
# pinmap (fpga/monolithic/nanosoc_mps3.xdc lines 408-441). PROPOSAL ONLY.
##############################################################################
set_property PACKAGE_PIN AW14 [get_ports {SH0_IO[0]}]
set_property PACKAGE_PIN AW13 [get_ports {SH0_IO[1]}]
set_property PACKAGE_PIN AW15 [get_ports {SH0_IO[2]}]
set_property PACKAGE_PIN AY15 [get_ports {SH0_IO[3]}]
set_property PACKAGE_PIN AY13 [get_ports {SH0_IO[4]}]
set_property PACKAGE_PIN AY12 [get_ports {SH0_IO[5]}]
set_property PACKAGE_PIN BA15 [get_ports {SH0_IO[6]}]
set_property PACKAGE_PIN BB14 [get_ports {SH0_IO[7]}]
set_property PACKAGE_PIN BA12 [get_ports {SH0_IO[8]}]
set_property PACKAGE_PIN BB12 [get_ports {SH0_IO[9]}]
set_property PACKAGE_PIN BA14 [get_ports {SH0_IO[10]}]
set_property PACKAGE_PIN BA13 [get_ports {SH0_IO[11]}]
set_property PACKAGE_PIN BB15 [get_ports {SH0_IO[12]}]
set_property PACKAGE_PIN AU12 [get_ports {SH0_IO[13]}]
set_property PACKAGE_PIN AV12 [get_ports {SH0_IO[14]}]
set_property PACKAGE_PIN AV17 [get_ports {SH0_IO[15]}]
set_property IOSTANDARD LVCMOS33 [get_ports {SH0_IO[*]}]
```

Matching `shell_top.sv` sketch (replaces the LED/switch mapping in §3):

```systemverilog
inout  wire [15:0] SH0_IO;                     // new top-level port
genvar g;
generate for (g = 0; g < 16; g = g + 1) begin : g_sh0_iobuf
  IOBUF u_sh0 (
    .I  (board_gpio_pad_o [g]),                // DUT drive value
    .T  (~board_gpio_pad_oe[g]),               // active-low tristate (oe=1 -> drive)
    .O  (board_gpio_pad_i [g]),                // pad value back to DUT/host mux
    .IO (SH0_IO[g])
  );
end endgenerate
```

This gives the DUT 16 **true bidirectional** GPIO on a physical, cable-able
header — the shield connector doing what board_gpio was designed to broker,
with the RP boundary and every RM completely unchanged.

---

## 7. Shield-channel allocation — GPIO vs ethernet vs debug (the real contention)

§6 proposes `dut_gpio → SH0_IO[15:0]`. But **SH0 is already contended**, so this
is not a free choice — it is an allocation decision across three claimants on the
two 18-bit channels (SH0 = bank 84, SH1 = bank 94):

| claimant | needs | current channel | notes |
|---|---|---|---|
| **DUT GPIO** (§6) | 16 IO, bidirectional | none (LEDs/switches today) | proposal wants SH0 |
| **LAN8720 ethernet** (RMII+MDIO) | ~9 IO incl. 1 clock-capable (REF_CLK) | **Pmod0 = SH0** | one TX bit on a separate Arduino-header pin (bench) |
| **Debug / ADP** (SWDIO/SWCLK, FT1248 bit-bang) | few IO | **SH0** in the *monolithic* target | `nanosoc_mps3.xdc:405` comment |

Two hard constraints make SH0 a poor home for new bidirectional GPIO:

1. **Pin budget.** GPIO(16) + ethernet(9) = 25 > 18 pins/channel — they cannot
   share one channel; they must split across SH0 **and** SH1.
2. **The SH0 inbound-shifter fault.** `LAN8720_BRINGUP_WORK_ITEM.md` localises the
   board failure to the **SH0 level-shifter passing FPGA→PHY but dead PHY→FPGA**.
   Any signal that needs a working *input* on SH0 — RMII RX, MDIO read, **or a
   bidirectional GPIO's read-back** — is broken until that shifter is repaired.
   The work item's highest-value experiment (T1) is precisely *"move RMII to SH1 /
   bank 94, the untried channel."*

**Verified (this analysis):** none of the 16 proposed `SH0_IO[0..15]` pins
(`AW14 AW13 AW15 AY15 AY13 AY12 BA15 BB14 BA12 BB12 BA14 BA13 BB15 AU12 AV12
AV17`) collide with any `PACKAGE_PIN` in the active shell/dfx/rp constraints — so
the §6 XDC is drop-in *as far as pin conflicts go*. SH1 is fully pinned and free
(`nanosoc_mps3.xdc:444-460`, `SH1_IO[0..16]` = `AT17 AU17 AV19 AW19 AW20 BA19
BA18 AY20 BA20 BA17 BB17 BB20 BB19 AW16 AY16 AY18 AY17`; GC/clock-capable pins are
`SH1_IO[13..16]`).

### Current LAN8720 pinout (from `ethernet-subsystem-ahb/fpga/targets/arm_mps3/ethernet_subsystem.xdc`)

The whole RMII bus is **deliberately kept on the SH0 shifter** (J28 + a J34-pin-3
flyoff), *not* SH1 — because on that unit **SH1's TX direction was also failing**
(the original `phy_rmii_txd[1]` on `SH1_IO[3]`/`AW19`/**J36 pin 4** was abandoned
for this reason). The one "separate Arduino-header" TX bit the bench referred to is:

| signal | package pin | shield net | connector |
|---|---|---|---|
| phy_rmii_ref_clk | AW14 | SH0_IO[0]  | J28 pin 3 |
| phy_rmii_rxd[0]  | AW13 | SH0_IO[1]  | J28 pin 2 |
| phy_rmii_rxd[1]  | AW15 | SH0_IO[2]  | J28 pin 8 |
| phy_rmii_crs_dv  | AV17 | SH0_IO[15] | J28 pin 9 |
| phy_rmii_txd[0]  | AY13 | SH0_IO[4]  | J28 pin 7 |
| **phy_rmii_txd[1]** | **BB15** | **SH0_IO[12]** | **J34 pin 3** (overflow; J34 pins 1-4 are SH0 channels) |
| phy_rmii_tx_en   | AY15 | SH0_IO[3]  | J28 pin 1 |
| mdio             | BB12 | SH0_IO[9]  | J28 pin 4 |
| phy_mdc          | AV12 | SH0_IO[14] | J28 pin 10 |

### Recommendation (updated with the above)

`txd[1]` lives on **SH0_IO[12]=BB15 (J34 pin 3)** — an SH0 pin the §6 GPIO proposal
does **not** claim (§6 uses SH0_IO[0..15] but that TX flyoff is SH0_IO[12], which
*is* in [0..15] → **collision**: GPIO §6 and RMII both want SH0_IO[12], plus
SH0_IO[0..4,9,14,15]). So GPIO(16)+RMII simply cannot coexist on SH0.

**Both shield channels have bench-reported faults on this unit:** SH0 **inbound**
is dead (the LAN8720 read/RX fault) and SH1 **TX / drive-out** was "failing
symmetric-asymmetric" (the reason `txd[1]` was pulled off `SH1_IO[3]`/J36 back onto
SH0). Since new **GPIO here is output-only** (bench decision, 2026-07-10), it needs
a working *drive-out* channel — which is exactly the direction SH1 was flagged bad
and SH0 is fine. So on the current signal-integrity picture:

- **SH0 drive-out works** (it's the proven-good direction — MDC/MDIO-write/TXD all
  land). Output-only GPIO is therefore *electrically* happiest on **SH0** — but SH0
  is fully occupied by RMII whenever the LAN8720 is present (§6's SH0_IO[0..15]
  collides with RMII on IO[0..4,9,12,14,15]).
- **SH1 drive-out is suspect** on this unit, so parking output-only GPIO there is
  *not* risk-free despite SH1 being pin-free — it wants the same SH1-shifter
  validation the RMII-on-SH1 (T1) experiment would provide.

Net: this is as much a **board signal-integrity / possibly board-repair** question
as a re-pin. Concretely:
- **If the LAN8720 stays on SH0:** don't add GPIO to SH0 (pin collision). Keep
  GPIO on today's LEDs, or take SH1-out *and validate the SH1 drive-out shifter*
  first.
- **If GPIO wants SH0 (§6, the electrically-good drive-out channel):** the PHY must
  vacate SH0 first — which reopens the SH1-drive-out question for the PHY.

**Bench decisions resolved:** (a) GPIO is **output-only** ✓. (b) TX overflow pin is
**BB15 / SH0_IO[12] / J34 pin 3** ✓ (not J36 — that was the abandoned SH1 route).
Remaining is a genuine board-level call (which channel, and whether SH1 drive-out
is healthy on the target unit) — carried on the next batched static rebuild.
