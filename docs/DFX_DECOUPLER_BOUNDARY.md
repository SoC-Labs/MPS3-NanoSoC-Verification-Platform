# DFX Decoupler — per-signal RP boundary (PG294)

> **Status: HISTORICAL** — a record of the per-signal DFX Decoupler boundary design as of 2026-07-09.
> Superseded by / current state in [docs/contracts/partition-pins.md](contracts/partition-pins.md) and [docs/STATUS.md](STATUS.md).
> Kept for provenance; do not update.
> Cites `PLATFORM_LIVE_STATUS.md` — internal notes, not in the public tree.

> **Status:** DESIGN (agent-authored, no Vivado run). Fills gap #2 of
> `PLATFORM_LIVE_STATUS.md` §5. This is the production-grade clean-swap
> boundary; it is **not** a blocker for the basic JTAG partial-swap mechanism,
> which was proven with *no* decoupler at all (`fpga/dfx/proof/rp_shell_top.sv`
> has none) and with the production static's decoupler present-but-minimal.
>
> **Ground truth:** `docs/contracts/partition-pins.md` v0.1 (the authoritative
> RP boundary list), `fpga/dfx/proof/rp_dut.sv` (the RP port list, 1:1 with the
> contract), `fpga/shell/bd/shell_bd.tcl` (the shell BD that instantiates the
> decoupler).

---

## 1. What the decoupler is for, and what it protects

During a partial reconfiguration the RP fabric is **transient garbage**: LUTs,
FFs and routing in the RP frames are being rewritten, so every net the RP
*drives* can glitch, float, or oscillate for the whole ICAP stream. The static
shell must survive that untouched (`shell_bd.tcl:596` "static shell must survive
RP teardown"; spec §16).

The rule that follows is directional:

- **RP→static signals (the RP is the driver) MUST be decoupled** — clamped to a
  known-safe constant on the static side for the duration of the swap. These are
  the dangerous ones: a stuck `phy_rmii_tx_en`, a stuck `uart_tx_tvalid`, a
  spurious `irq_out`, or a driven `dut_gpio_oe` can inject a garbage frame,
  wedge an async FIFO, fire a false interrupt, or fight a board pad while the RP
  is mid-rewrite.
- **static→RP signals (the shell is the driver) generally do NOT need
  decoupling.** The shell keeps driving them; the RP simply ignores whatever it
  receives because it is held in reset (`rp_resetn` low, gated by
  `dfx_ctl_0/rp_resetn_gate_o`, `shell_bd.tcl:486`) throughout. Clocks and
  resets in particular are **never** routed through the decoupler.

So the decoupler's job on this boundary is: **hold the ~19 RP→static output bits
at a safe idle while `DECOUPLE=1`, and pass everything else through.**

## 2. Current state in `shell_bd.tcl` (why this is gap #2)

`dfx_decoupler_0` is instantiated (`shell_bd.tcl:608`) but with **no boundary
interfaces configured** — in that default state the IP exposes only two scalar
pins, `decouple` (I) and `decouple_status` (O) (`shell_bd.tcl:613-615`). Only the
control handshake is wired:

- `dfx_ctl_0/decouple_en_o → dfx_decoupler_0/decouple` (`shell_bd.tcl:622`)
- `dfx_decoupler_0/decouple_status → dfx_ctl_0/decoupled_i` (`shell_bd.tcl:623`)

**Not one partition-pin signal passes through the decoupler today.** Every RP
signal is wired *directly* between a shell CSR block and the `rp_*` BD port:

| Group | Current direct wiring | Line |
|---|---|---|
| SWD in | `rp_swd_dio_i → swd_bb_0/swd_dio_i_i` | `shell_bd.tcl:516` |
| Console TX | `rp_uart_tx_tdata/tvalid → uart_bridge_0/...` | `shell_bd.tcl:523-524` |
| Console RX handshake | `rp_uart_rx_tready → uart_bridge_0/uart_rx_tready_i` | `shell_bd.tcl:528` |
| SWO | `rp_swo → uart_bridge_0/swo_i` | `shell_bd.tcl:529` |
| RM-verify | `rp_rm_id → dfx_ctl_0/rm_id_i`, `rp_dut_lockup → dfx_ctl_0/dut_lockup_i` | `shell_bd.tcl:497-498` |
| GPIO out | `rp_dut_gpio_o/oe → board_gpio_0/...` | `shell_bd.tcl:545-546` |
| RMII/MDIO TX | tied off / unconnected (SECTION 5 deferred) | `shell_bd.tcl:756-762` |
| **`rp_irq_out`** | **declared but wired to nothing** | `shell_bd.tcl:168` |

The existing `TODO(integrator, HIGH-RISK first-run item)` at `shell_bd.tcl:628-641`
and README uncertainty #2 (`fpga/shell/README.md`, and `shell_bd.tcl:896-899`)
already flag that the per-signal boundary must be authored in the PG294
customization GUI. **This document is that authoring, specified.**

> Side finding (independent of decoupling): `rp_irq_out` is created at
> `shell_bd.tcl:168` and never connected. Whatever the decoupling decision, it
> needs a real sink (AXI INTC spare line, or `dfx_ctl` telemetry). Fold it into
> the STATUS group below so it is both wired *and* clamped.

---

## 3. Full enumerated boundary — grouped by direction

Directions are **from the shell's point of view**, matching
`partition-pins.md` and `rp_dut.sv` exactly. "Decouple?" is the recommendation;
"Safe value" is what the static side sees while `DECOUPLE=1`.

### 3a. RP → static (RP drives; shell samples) — **MUST DECOUPLE**

These are the 19 output bits that make the RP dangerous during a swap.

| Signal | Width | `rp_dut.sv` | Safe value | Why it must be held |
|---|---|---|---|---|
| `swd_dio_i` | 1 | `:22` | `0` | SWDIO read-back into `swd_bb`; idle line during swap. |
| `phy_rmii_txd` | 2 | `:27` | `0` | TX nibble into the link-partner MAC. |
| `phy_rmii_tx_en` | 1 | `:28` | `0` | **Critical** — a stuck `tx_en` injects a runaway garbage frame into `link_partner_mac`. |
| `mdc` | 1 | `:29` | `0` | DUT-driven MDIO clock; a free-running `mdc` would clock junk into the virtual-PHY `mdio_slave`. |
| `mdio_o` | 1 | `:30` | `0` | MDIO write data from DUT. |
| `mdio_oe` | 1 | `:31` | `0` | MDIO output-enable; hold `0` so the virtual PHY sees an undriven bus. |
| `uart_tx_tdata` | 8 | `:34` | `0` | Console byte from DUT. |
| `uart_tx_tvalid` | 1 | `:35` | `0` | **Critical** — a stuck `tvalid` pushes garbage into `uart_bridge` async FIFO / floods the console-over-Ethernet relay. |
| `uart_rx_tready` | 1 | `:39` | `0` | **Critical** — a stuck `tready` would drain the RX async FIFO while the RP that should consume it is gone. |
| `swo` | 1 | `:40` | `0` | Cortex-M SWO/ITM trace bit; idle. |
| `rm_id` | 32 | `:42` | `0x0000_0000` | Held `0` ⇒ `dfx_ctl` RM-verify reads "no valid RM" during the swap, which is exactly correct (`rm_id_valid` false). |
| `dut_lockup` | 1 | `:43` | `0` | Held `0` ⇒ the shell watchdog/telemetry does not trip a false lockup while the RP is being rewritten. |
| `irq_out` | 1 | `:44` | `0` | Held `0` ⇒ no spurious IRQ into the shell INTC (also fixes the dangling-port finding above). |
| `dut_gpio_o` | 16 | `:46` | `0` | DUT drive value toward the board pad (via `board_gpio`). |
| `dut_gpio_oe` | 16 | `:47` | `0` | **Critical** — `oe=0` forces board pads to high-Z, so a mid-swap RP can't fight an external driver. |

Total: **19 bits across 15 nets.**

### 3b. static → RP (shell drives; RP samples) — **DO NOT DECOUPLE**

The shell keeps driving these; the RP is in reset and ignores them. Route
straight through (no decoupler LUTs spent).

| Signal | Width | `rp_dut.sv` | Class | Note |
|---|---|---|---|---|
| `dut_clk` | 1 | `:14` | **clock** | Never through a decoupler. `clk_wiz_dut` → port (`shell_bd.tcl:262`). |
| `phy_rmii_ref_clk` | 1 | `:24` | **clock** | 50 MHz RMII ref; `clk_wiz_shell/clk_out2` → port (`shell_bd.tcl:261`). Never decoupled. |
| `dut_resetn` | 1 | `:15` | reset | `dut_clkrst_0` (`shell_bd.tcl:491`). |
| `rp_resetn` | 1 | `:16` | reset | Held low *through* the swap by the reset gate — that is the real isolation on the input side. `shell_bd.tcl:492`. |
| `dbg_resetn` | 1 | `:17` | reset | `shell_bd.tcl:493`. |
| `swd_clk` | 1 | `:19` | scalar | `swd_bb_0` (`shell_bd.tcl:513`). |
| `swd_dio_o` | 1 | `:20` | scalar | `swd_bb_0` (`shell_bd.tcl:514`). |
| `swd_dio_oe` | 1 | `:21` | scalar | `swd_bb_0` (`shell_bd.tcl:515`). |
| `phy_rmii_crs_dv` | 1 | `:25` | scalar | virtual-PHY RX-valid to DUT. |
| `phy_rmii_rxd` | 2 | `:26` | scalar | virtual-PHY RX data to DUT. |
| `mdio_i` | 1 | `:32` | scalar | virtual-PHY MDIO reply to DUT. |
| `uart_rx_tdata` | 8 | `:37` | scalar | host→DUT console byte. |
| `uart_rx_tvalid` | 1 | `:38` | scalar | host→DUT console valid. |
| `uart_tx_tready` | 1 | `:36` | scalar | shell-side ready for DUT TX. |
| `dut_gpio_i` | 16 | `:48` | scalar | board pad → DUT sample. |

> **AXIS subtlety.** The console is two AXI-Stream links that each straddle both
> directions, so a *clean* PG294 model puts the whole stream through the
> decoupler even though only its RP-driven bits (3a) truly need clamping — the
> IP then manages the paired handshake automatically (§4). If you instead
> hand-roll signal-level clamps, decouple only the 3a bits and leave the
> static→RP partners passing through; both are legal, the AXIS-interface form is
> just less error-prone.

---

## 4. Recommended DFX Decoupler IP configuration (PG294 style)

**Principle (PG294): one decouple *interface* per logical interface, RP role set
so the tool knows which signals the RP drives.** Clocks/resets excluded. For a
scalar+stream boundary like this one, use a mix of the built-in **AXI4-Stream**
interface type (for the two console streams — gets handshake management for
free) and **Custom / signal-level** interfaces (for everything else).

Proposed interface set (six groups, matching the task's "one decouple group per
interface"):

| # | Interface (name) | Type | RP role | Signals it carries | Decoupled behaviour |
|---|---|---|---|---|---|
| 1 | `swd` | Custom | mixed | `swd_dio_i` (RP→S, **decouple→0**); `swd_clk`,`swd_dio_o`,`swd_dio_oe` (S→RP, pass) | clamp `swd_dio_i`=0 |
| 2 | `rmii_tx` | Custom | RP master | `phy_rmii_txd[1:0]`, `phy_rmii_tx_en` (RP→S) | clamp all =0 |
| 3 | `mdio` | Custom | mixed | `mdc`,`mdio_o`,`mdio_oe` (RP→S, **decouple→0**); `mdio_i` (S→RP, pass) | clamp `mdc/mdio_o/mdio_oe`=0 |
| 4 | `console_tx` | AXI4-Stream (TDATA8) | **RP master** | `uart_tx_tdata/tvalid` (RP→S), `uart_tx_tready` (S→RP) | on decouple: `s_tvalid=0`, `rp_tready=0` |
| 5 | `console_rx` | AXI4-Stream (TDATA8) | **RP slave** | `uart_rx_tdata/tvalid` (S→RP), `uart_rx_tready` (RP→S) | on decouple: `s_tready=0`, `rp_tvalid=0` |
| 6 | `swo` | Custom | RP master | `swo` (RP→S) | clamp `swo`=0 |
| 7 | `status` | Custom | RP master | `rm_id[31:0]`, `dut_lockup`, `irq_out` (RP→S) | clamp `rm_id`=0, `dut_lockup`=0, `irq_out`=0 |
| 8 | `gpio_out` | Custom | RP master | `dut_gpio_o[15:0]`, `dut_gpio_oe[15:0]` (RP→S) | clamp both =0 |

(GPIO input `dut_gpio_i` and all clocks/resets/RX scalars are **not** members of
any decoupler interface — they route straight through.)

Each configured interface makes the IP expose a paired port set:
`rp_<intf>_<sig>` (wire to the RP cell / `rp_*` BD port) and `s_<intf>_<sig>`
(wire to the shell CSR block). `decouple` drives all groups together; a
per-interface `decouple_<n>` split is available if staged isolation is ever
wanted (not needed here — one `DECOUPLE` bit from `dfx_ctl` is correct).

### On authoring the CONFIG string

`dfx_decoupler:1.0`'s customization is a nested `CONFIG.ALL_PARAMS` dictionary
(interface list × per-signal role/width/decouple-value). It is realistically
**GUI-authored then exported** — hand-writing it in Tcl is exactly the
"HIGH-RISK first-run item" the existing comment warns about (`shell_bd.tcl:628`).
The recommended flow:

1. Open the BD in Vivado 2024.1, customize `dfx_decoupler_0` with the eight
   interfaces above, set each signal's width/role/decoupled value per §3–§4.
2. `write_bd_tcl` (or copy the generated `set_property -dict [list CONFIG...]`)
   and paste the resulting `CONFIG.ALL_PARAMS` block back over the placeholder
   at `shell_bd.tcl:608`.
3. Re-run `validate_bd_design`; the `-quiet` guards on the handshake connects
   (`shell_bd.tcl:623,625`) can then be dropped.

---

## 5. Exact `shell_bd.tcl` changes

The edit is **re-routing, not re-inventing**: each RP→static net that today goes
`rp_<port> → CSR_block` is split into `rp_<port> → decoupler(rp side)` and
`decoupler(s side) → CSR_block`. Concretely, replace/insert as follows (line
numbers are current):

**Keep as-is (control handshake):** `shell_bd.tcl:622-627`.

**Add the interface CONFIG** to the `dfx_decoupler_0` create at `shell_bd.tcl:608`
(the `CONFIG.ALL_PARAMS` block per §4).

**Re-wire the RP→static (3a) nets — change each of these:**

| Was (current line) | Becomes |
|---|---|
| `rp_swd_dio_i → swd_bb_0/swd_dio_i_i` (`:516`) | `rp_swd_dio_i → dfx_decoupler_0/rp_swd_swd_dio_i`; `dfx_decoupler_0/s_swd_swd_dio_i → swd_bb_0/swd_dio_i_i` |
| `rp_uart_tx_tdata → uart_bridge_0/uart_tx_tdata_i` (`:523`) | via `console_tx` AXIS: `rp_uart_tx_* → dfx_decoupler_0/rp_console_tx_*`; `dfx_decoupler_0/s_console_tx_* → uart_bridge_0/uart_tx_*` |
| `rp_uart_tx_tvalid → uart_bridge_0/...` (`:524`) | (same `console_tx` interface) |
| `uart_bridge_0/uart_tx_tready_o → rp_uart_tx_tready` (`:525`) | (same `console_tx` interface — S→RP leg) |
| `uart_bridge_0/uart_rx_tdata_o → rp_uart_rx_tdata` (`:526`) | via `console_rx` AXIS (S→RP) |
| `uart_bridge_0/uart_rx_tvalid_o → rp_uart_rx_tvalid` (`:527`) | (same `console_rx` interface) |
| `rp_uart_rx_tready → uart_bridge_0/uart_rx_tready_i` (`:528`) | (same `console_rx` interface — RP→S leg, clamped) |
| `rp_swo → uart_bridge_0/swo_i` (`:529`) | `rp_swo → dfx_decoupler_0/rp_swo_swo`; `dfx_decoupler_0/s_swo_swo → uart_bridge_0/swo_i` |
| `rp_rm_id → dfx_ctl_0/rm_id_i` (`:497`) | `rp_rm_id → dfx_decoupler_0/rp_status_rm_id`; `dfx_decoupler_0/s_status_rm_id → dfx_ctl_0/rm_id_i` |
| `rp_dut_lockup → dfx_ctl_0/dut_lockup_i` (`:498`) | via `status`: `... /rp_status_dut_lockup`; `s_status_dut_lockup → dfx_ctl_0/dut_lockup_i` |
| `rp_irq_out` (dangling, `:168`) | via `status`: `rp_irq_out → dfx_decoupler_0/rp_status_irq_out`; `s_status_irq_out →` **new sink** (AXI INTC spare / `dfx_ctl` telemetry) |
| `rp_dut_gpio_o → board_gpio_0/dut_gpio_o_i` (`:545`) | via `gpio_out`: `rp_dut_gpio_o → .../rp_gpio_out_dut_gpio_o`; `s_gpio_out_dut_gpio_o → board_gpio_0/dut_gpio_o_i` |
| `rp_dut_gpio_oe → board_gpio_0/dut_gpio_oe_i` (`:546`) | via `gpio_out` (same interface) |

**RMII/MDIO TX (currently unconnected, SECTION 5 `:756-762`):** when the ethernet
subsystem lands (SECTION 5 TODO, `:743-749`), route `rp_phy_rmii_txd`,
`rp_phy_rmii_tx_en` through the `rmii_tx` interface and `rp_mdc`, `rp_mdio_o`,
`rp_mdio_oe` through the `mdio` interface, then on to `rmii_phy_if_0` /
`mdio_phy_model_0`. Until then they stay tied off — but **add them to the
decoupler config now** so the boundary is authored once.

**Leave untouched (3b static→RP + clocks/resets):** `shell_bd.tcl:261-262`
(clocks), `:491-493` (resets), `:513-515` (SWD out), `:547` (gpio in), and the
RX-side console legs that are the S→RP members already folded into the AXIS
interfaces above. Do **not** insert the decoupler into any clock or reset net.

### Sanity checks after the edit

- `validate_bd_design` clean (no unconnected `rp_*`/`s_*` decoupler pins; the
  `rp_irq_out` dangling-input warning gone).
- `decouple_status` no longer needs `-quiet` (`shell_bd.tcl:623`).
- Post-synth: confirm no clock/reset net was pulled into the decoupler (check the
  decoupler's pin list carries only the §3a/§4 members).
- The decoupled values in §3a match what `dfx_ctl` expects for a "no valid RM"
  readback (`rm_id=0 ⇒ rm_id_valid=0`), so a swap in progress reads as
  "RP absent," consistent with `swap_fsm` step 2/5 sequencing.

---

## 6. Summary

- The mechanism (partial swap) works **without** this — proven with no decoupler
  in the proof static. This is the **production clean-swap** boundary.
- Decouple the **19 RP→static output bits** (§3a) to safe idle; **pass through**
  all clocks, resets, and static→RP inputs (§3b).
- Author it as **eight PG294 interfaces** (§4): two AXI4-Stream (console), six
  Custom — one decouple group per logical interface, driven by the single
  `dfx_ctl DECOUPLE` bit already wired.
- The `shell_bd.tcl` work is pure re-routing of ~13 existing `connect_bd_net`
  lines through the decoupler's `rp_*`/`s_*` pins (§5), plus pasting the
  GUI-exported `CONFIG.ALL_PARAMS`, plus giving the currently-dangling
  `rp_irq_out` (`shell_bd.tcl:168`) a sink inside the `status` group.
