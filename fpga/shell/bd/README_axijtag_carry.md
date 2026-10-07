# `axijtag_uart_carry.tcl` — MPS3 carry-across of the proven KR260/HAPS `axi_jtag` + `axi_uart16550` + magic-GPIO

> **STATUS: STAGED / UNVALIDATED.** This is "test-tomorrow" scaffolding.
> It has **not** been run through Vivado, elaborated, validated, synthesised, or
> put on a board. Do **not** represent it as verified. Vivado was busy when it
> was authored; no build/board command was run. Every vendor-IP CONFIG name,
> pin name, and address-segment name is reproduced from the proven KR260
> reference and the MPS3 `shell_bd.tcl` conventions — minor-version drift
> between the KR260 Vivado and MPS3's 2024.1 is possible and must be confirmed
> against the live Customize-IP dialog.

## What this is

A single self-contained, sourceable Tcl proc — `add_axijtag_uart_carry` — that
adds the proven three-IP debug/console triplet to the MPS3 KU115 DFX shell block
design, **without editing `shell_bd.tcl`** (a re-mint is running against that
file). It faithfully copies the KR260 CONFIG dicts and changes only the three
MPS3-specific axes: base addresses, TCK ratio, and the 16550 baud `clk_wiz`
solve.

| Block | MPS3 base | Size | Role |
|---|---|---|---|
| `axi_jtag_carry` (`axi_jtag:1.0`) | `0x44AF_0000` | 64K | AXI→JTAG shift engine → DUT SWJ-DP (JTAG mode). TCK = `shell_clk/32` = 3.125 MHz. |
| `axi_gpio_magic_carry` (`axi_gpio:2.0`) | `0x44B0_0000` | 64K | Reads `0x4A544147` ("JTAG") — pre-flight "aperture alive" probe. |
| `axi_uart16550_carry` (`axi_uart16550:2.0`) | `0x44B1_0000` | 64K | `ns16550a` console. `xin` = dedicated 9.216 MHz `clk_wiz` output → DL=5, exact 115200. |

Relative layout (`+0x0 / +0x1_0000 / +0x2_0000`) is preserved from the
reference; only the absolute base moves into the MPS3 `0x44A` CSR aperture (next
free 64K pages after TOUCH `0x44AE`).

## Reference (PROVEN, silicon-built, READ-ONLY — being actively built tonight)

- BD copied from: `nanosoc-multicore-system/pynq/targets/kr260-axijtag-uart/nanosoc_multicore_jtag_uart_design.tcl`
- Addresses / baud math from: `nanosoc-multicore-system/imp/fpga/output/kr260-axijtag-uart/address_map.txt`
- Carry-across plan aligned to: `docs/planning/AXIJTAG_UART_CARRY_ACROSS.md` (§3.2 address map, §3.3 partition-pin delta, §3.4 re-mint batching)

## How to integrate

`shell_bd.tcl` is `proc create_root_design {parentCell}` and leaves
`validate_bd_design` / `save_bd_design` to the caller (`build_shell.tcl`). This
carry file follows the same contract.

1. In the build wrapper, after `source .../shell_bd.tcl`, also:
   ```tcl
   source [file join $bd_dir axijtag_uart_carry.tcl]
   ```
2. Inside `create_root_design`, at the **end of SECTION 6** (after the existing
   `M00..M17` `mi_map` foreach and its `assign_bd_address` block — i.e. after
   `shell_bd.tcl:1329`, before the caller's validate), add **one** call:
   ```tcl
   add_axijtag_uart_carry        ;# staged: DUT-facing pins left stubbed
   ```
   After the SWD→JTAG boundary re-mint + uart byte-shim actually drive the
   DUT-facing pins, call instead:
   ```tcl
   add_axijtag_uart_carry 0      ;# no placeholder tie-offs (avoid multi-driver)
   ```
3. The proc re-fetches the shell handles (`clk_wiz_shell`,
   `proc_sys_reset_shell`, `axi_interconnect_0`, `osc_clk_50m`, `sys_rst_n`) by
   **name** from the current BD instance — it does not depend on `shell_bd.tcl`'s
   local Tcl variables. Call it with `current_bd_instance` at the BD root (which
   is the case inside `create_root_design`).
4. It does **not** call `validate_bd_design` / `save_bd_design`.

Because `axi_jtag` / `axi_gpio` / `axi_uart16550` / `clk_wiz` are all Xilinx
**catalog** IP (not the packaged `soclabs.org:user:*` CSR blocks), no extra
`ip_repo_paths` / packaging step is needed beyond what `shell_bd.tcl` already
requires.

## `docs/contracts/shell-regmap.md` additions (exact)

Add three rows and bump the header notes (`NUM_MI = 18 → 21`;
`assign_bd_address` now extends past TOUCH `0x44AE` to `0x44AF/0x44B0/0x44B1`):

| Block | Base | Size | Notes |
|---|---|---|---|
| **AXIJTAG** | `0x44AF_0000` | 64K | `axi_jtag:1.0` AXI→JTAG to DUT SWJ-DP; TCK = `shell_clk/32` = 3.125 MHz; 64K-aligned ⇒ mmap-able page. |
| **MAGICID** | `0x44B0_0000` | 64K | `axi_gpio`, all-inputs = `32'h4A54_4147` ("JTAG"); pre-flight liveness (equivalent to `DFXCTL.RM_ID`). |
| **UART16550** | `0x44B1_0000` | 64K | `axi_uart16550:2.0`, `ns16550a`; regfile @ `+0x1000` (`reg-offset=<0x1000>`); `xin` = 9.216 MHz ⇒ DL=5 exact 115200; 64K ≥ 0x2000 min aperture. |

Device-tree hint for **UART16550** (carried verbatim from the KR260
`address_map.txt`, only the base changes):

```
compatible     = "xlnx,xps-uart16550-2.00.a", "ns16550a";
reg            = <0x0 0x44B10000 0x0 0x00010000>;
reg-offset     = <0x1000>;      // UART_REG_BASEADDR in the IP RTL
reg-shift      = <2>;           // 32-bit register spacing
reg-io-width   = <1>;           // byte-wide regs, little-endian (do NOT set BE)
clock-frequency= <9216000>;     // the xin BAUD REFERENCE, not aclk (the trap)
current-speed  = <115200>;
// interrupts = <...>;          // OMITTED here — ip2intc_irpt is unwired
//                              // (poll mode). The 8250 driver polls happily.
```

## Every value CHANGED from the KR260 reference vs COPIED

**Changed (the 3 intended axes + the master/fabric-plumbing deltas MPS3 forces):**

- **`axi_jtag` `C_TCK_CLOCK_RATIO`: `8` → `32`** (KR260 line 268). MPS3 shell AXI
  runs at 100 MHz (KR260 was 25 MHz); `100/32 = 3.125 MHz` reproduces the proven
  KR260 TCK (`25/8`). **⚠ Confirm `axi_jtag:1.0` accepts ratio 32.** Alternative:
  keep ratio 8 and feed `axi_jtag/s_axi_aclk` from a dedicated 25 MHz shell clock.
- **Base addresses:** `0x8000_0000 / 0x8001_0000 / 0x8002_0000` (KR260 125–131) →
  `0x44AF_0000 / 0x44B0_0000 / 0x44B1_0000`. Relative layout preserved.
- **`clk_wiz_uart` input + solve:** KR260 solved 9.216 MHz from ~100 MHz PL0_REF,
  `M=76.125 D=8 O=103.250` (KR260 236–246, `address_map.txt` 23). MPS3 re-solves
  9.216 MHz from the **50 MHz** board osc (`osc_clk_50m`), `PRIM_SOURCE No_buffer`
  to match `clk_wiz_shell`/`clk_wiz_dut`. M/D/O intent in the create comment; the
  2024.1 solver picks the exact grid point (illustrative: `M=17.375 D=1 O=94.25`
  → 9.2175 MHz, +0.016 %, analogous to KR260's +1 ppm). **DL=5 → 115200 unchanged.**
- **Fabric plumbing:** KR260's fresh 3-slave SmartConnect (KR260 257) → 3 new
  master ports (`M18/M19/M20`) on the **existing** MPS3 `axi_interconnect:2.1`
  (`NUM_MI 18 → 21`). The AXI master is the shell **MicroBlaze v11**, so none of
  KR260's `zynq_ultra_ps_e` / `apply_board_preset` / `M_AXI_HPM0_LPD` / `pl_clk0`
  / `pl_resetn0` block is carried (KR260 190–201, 375–425). Clocks/resets come
  from `clk_wiz_shell` (100 MHz) / `proc_sys_reset_shell`.
- **SoC cell + its dap/uart nets NOT instantiated** (KR260 368–370, 443–493): on
  MPS3 the DUT is in the reconfigurable partition, reached only across the RP
  boundary. Left as TODO stubs (below).

**Copied faithfully (do not "improve"):**

- `axi_jtag` VLNV `xilinx.com:ip:axi_jtag:1.0` (KR260 267).
- Magic `axi_gpio`: `C_ALL_INPUTS 1` / `C_GPIO_WIDTH 32` / `C_IS_DUAL 0` /
  `C_INTERRUPT_PRESENT 0` + `xlconstant 0x4A544147` (KR260 275–287).
- `axi_uart16550:2.0`: `C_IS_A_16550 16550`, `C_HAS_EXTERNAL_XIN 1`,
  `C_EXTERNAL_XIN_CLK_HZ_d 9.216`, `C_HAS_EXTERNAL_RCLK 0` (KR260 311–317).
- 16550 modem/debug tie-offs: `ctsn/dcdn/dsrn/freeze = 0`, `rin = 1` — `dcdn=0`
  is load-bearing (else `open()` blocks on carrier); `ctsn=0` else TX stalls on
  `MCR.AFE` (KR260 336–362, 457–464).
- The "dedicated second MMCM, never clock the 16550 from aclk" doctrine and the
  DL=5 / exact-115200 recipe (KR260 47–96, 224–246).
- Address-segment names: `axi_jtag s_axi/reg0`, `axi_gpio S_AXI/Reg`,
  `axi_uart16550 S_AXI/Reg` (KR260 515–522).

## DUT-facing pins — TODO stubs only (deliberately not wired)

This is the one genuinely new integration cost on MPS3; neither reference board
crosses an RP partition boundary. The carry file **stubs and comments** these;
it does **not** invent the boundary wiring.

- **(A) `axi_jtag` ↔ DUT SWJ-DP — a SEPARATE partition-pins re-mint.** KR260
  wired `jtag/tck|tms|tdi → soc/dap_*` and `soc/dap_tdo → jtag/tdo` directly
  (KR260 443–446). On MPS3 these four wires must cross the RP boundary, which
  today carries 4-wire **SWD** (`rp_swd_clk / rp_swd_dio_o / rp_swd_dio_oe /
  rp_swd_dio_i`, driven by `swd_bb_0` through `dfx_decoupler_0`,
  `shell_bd.tcl:755–760`). Swapping that group to a 4-wire **JTAG** group is a
  **full partition re-mint**: it re-keys `static_id` **and** rebuilds every RM
  partial from RTL + re-runs `pin_check` (`partition-pins.md`). That is a
  separate, larger decision than this static-slave add — sequence it
  deliberately. The RP-facing port names are a `partition-pins.md` deliverable,
  **not invented here**.
- **(B) `axi_uart16550` ↔ `uart_bridge_0` byte-shim — NO boundary change.**
  KR260 crossed a raw serial pair (KR260 486–493). MPS3 crosses the console as
  an **AXI-Stream byte** interface into `uart_bridge_0`
  (`shell_bd.tcl:771–776`), which does the `dut_clk ⇄ shell_clk` async-FIFO CDC.
  The 16550's `sin`/`sout` must come from a **shell-side byte-shim** that
  (de)serialises against `uart_bridge_0`'s FIFOs — a shim that **does not exist
  yet** and is not authored here. Zero partition-boundary change. This asymmetry
  — **JTAG adoption re-mints the boundary, UART adoption does not** — is the key
  MPS3 finding (`AXIJTAG_UART_CARRY_ACROSS.md` §3.3).
- **(C) `ip2intc_irpt` left unconnected (poll mode).** KR260 wired it to
  `pl_ps_irq0` (KR260 437). MPS3 has no PS; the equivalent is a spare
  `axi_intc_0` input. Optional later step; the driver polls fine without it.

**Placeholder tie-offs.** `validate_bd_design` auto-ties dangling **inputs** to 0
(CRITICAL WARNING, not a hard error). With `stub_dut_pins=1` (default) the proc
makes that explicit: it drives `axi_jtag/tdo` and `axi_uart16550/sin` from a
0-constant so the staging intent is loud and there is one obvious place to
delete. Pass `stub_dut_pins=0` once (A)/(B) drive those pins for real, to avoid a
multi-driver conflict. Outputs (`tck/tms/tdi`, `sout`) are legal dangling and are
left open for (A)/(B).

## Honest proven-vs-unproven ledger (MPS3-specific)

**Proven-portable (carries unchanged):** the IP set + params, the 9.216 MHz-xin
/ DL=5 baud recipe, the modem-tie-off doctrine, the relative address layout.

**Unproven / net-new on MPS3:**
1. The whole thing is unbuilt on KU115.
2. **No host driver exists even in the reference** — "OpenOCD `mmap()`s
   `axi_jtag`" is aspirational (encrypted IP, no committed cfg, no XVC server).
   On MPS3 it must additionally be fronted by a MicroBlaze firmware JTAG/XVC
   server (analogue of today's `swd_server`) until MB-V Linux can `mmap`
   `0x44AF_0000`.
3. The DFX partition re-mint (SWD→JTAG) is exercised on no reference board.
4. **TCK ratio changed `/8 → /32`** — confirm the IP's ratio ceiling.
5. IDCODE constant flips SWD DPIDR `0x0bb1_1477` → JTAG TAP `0x6ba0_0477`.
6. MPS3 v0 DUT is **single-core** = one AHB-AP (vs the reference's two).

## Re-mint batching (from `AXIJTAG_UART_CARRY_ACROSS.md` §3.4)

- **Static-slave add** (this file: the three IP + `clk_wiz` output at
  `0x44AF/B0/B1`) = a "CLCD-class" static-only re-mint (RM RTL untouched,
  re-keys `static_id`, regenerates the 9 overlay partials). **Batch with the
  staged CLCDKVM Wave-4 static change** so overlays are re-keyed once.
- **SWD→JTAG boundary swap** (item A above) = a heavier partition-boundary
  re-mint. Separate, larger decision — sequence deliberately, not as a side
  effect.

## Files

- `fpga/shell/bd/axijtag_uart_carry.tcl` — the sourceable proc.
- `fpga/shell/bd/README_axijtag_carry.md` — this file.
