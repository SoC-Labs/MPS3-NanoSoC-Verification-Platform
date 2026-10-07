# nanosoc_m0_soc → mps3-nanosoc-platform: signal mapping report

> **Status: HISTORICAL** — a record of the `nanosoc_m0_soc` -> partition-pins signal mapping survey as of 2026-08-07.
> Superseded by / current state in [docs/contracts/partition-pins.md](../contracts/partition-pins.md) and [docs/STATUS.md](../STATUS.md).
> Kept for provenance; do not update.

**Scope:** read-only mapping of the single-core nanoSoC RTL at
`~/SoCLabs/nanosoc_m0_soc` onto the `mps3-nanosoc-platform`
partition-pins v0.1 contract (RM path) and the W1 monolithic MPS3 baseline
(whole-device path). No RTL, wrapper, or build file was modified to produce
this report.

**DUT top used for the mapping:** `nanosoc_m0_soc/build_soc/rtl/nanosoc.sv`
(module `nanosoc`) — the generator-emitted, FPGA-target structural top
(`soc_toplevel.sv.j2`). This is the right level: it's what
`nanosoc_m0_soc/pynq/vivado_ip/nanosoc_vivado_wrapper.v` instantiates as
`u_soc`, and that wrapper is the **proven, HW-validated reference pattern**
for wiring this exact top onto a board (PYNQ-Z2 today) — both mappings below
lean on it heavily. (`nanosoc_chip.v`/`nanosoc_chip_pads.v` under
`nanosoc_m0_soc/chip/` and `build_soc/rtl/` are the ASIC pad-ring level —
GPIO_TIO alt-mode muxed SWD/UART on shared pads for tapeout — not the FPGA
integration point.)

## Headline findings

- **No RMII MAC.** `nanosoc.sv`'s full port list (134 lines, read in full)
  contains zero `rmii`/`mdio`/`eth`/`mac` tokens. Ethernet is a CPU0/
  `network_core` feature added later in the multicore generator tree; it does
  not exist in this single-core SoC's ports, instance list, or any
  sub-hierarchy reachable from this top.
- **Console UART is raw-serial, not AXI-Stream**, and it isn't even a
  dedicated top-level pin: nanosoc's firmware console is CMSDK **UART2**,
  which firmware mux-selects onto **GPIO port P1** (`p1_out[5]`=TXD,
  `p1_in[4]`=RXD, `p1_in[7]`=1 strap for FT1248/UART2 mode) — see
  `nanosoc_vivado_wrapper.v` header comment, lines 41-49. Getting to the
  contract's `uart_tx_tdata[7:0]`/`uart_rx_tdata[7:0]` AXI-Stream framing
  needs a **UART-bit ⇄ AXI-Stream-byte shim** inside the RM, sitting between
  P1[5]/P1[4] and the partition pins. **Known limitation carried over from
  the proven wrapper (not introduced here): UART2's RX input path stops at
  the pin mux — `uart2_rxd` is fed from internal feedback, not the pads — so
  the SoC-side UART is TX-only in the currently generated RTL** (wrapper
  lines 46-49). `uart_rx_*` can be wired but won't reach the CPU until that
  RTL gap is closed upstream.
- **No SWO.** nanosoc's CPU is Cortex-M0/M0+ (`rtl/slcorem0_tech`,
  `rtl/slcorem0p_tech`); ITM/SWO trace is an M3/M4/M33 feature. Confirmed:
  `grep -rni "swo\|tpiu"` across `build_soc/rtl/` returns nothing.
- **Signal counts against the 30 named signals in partition-pins.md v0.1**
  (excluding the bus-width detail, counting each named wire once):
  **9 map cleanly, 6 map via a small in-RM shim (UART), 9 tie off inert (no
  RMII MAC), 6 are gaps with no nanosoc top-level port at all.** See §3 for
  the full breakdown.

---

## 1. `rm_nanosoc` RM wrapper — signal-by-signal

RM wrapper file: `mps3-nanosoc-platform/fpga/rp/nanosoc/rp_nanosoc_wrapper.sv`
(currently ties everything off; ports already match partition-pins.md v0.1).
"Status" legend: **Clean** = direct nanosoc port, straightforward glue only;
**Shim** = functionally realizable but needs a small adapter block instanced
inside the RM; **Tie-off** = no such subsystem exists in nanosoc at all;
**Gap** = no nanosoc top-level port for this function — an A6 decision
(§3) is needed, not just wiring.

### Clocks & resets (shell → RP)

| partition-pins.md signal | nanosoc.sv port | Status | Notes |
|---|---|---|---|
| `dut_clk` | `sys_clk` (in) | Clean | Direct connect. Confirm shell's DUT-clock frequency against `SYS_CLK_FREQ_HZ` (default 100 MHz param, `nanosoc` line 19) vs. the proven FPGA operating point of 25 MHz (PYNQ-Z2, `nanosoc_vivado_wrapper.v` `sys_fclk`) — pick one and pass it as the parameter override. |
| `dut_resetn` | `sys_sysresetn` (in) | Clean | This is nanosoc's **only** top-level reset input — everything (core, debug/SW-DP, AHB fabric) resets off this one net. |
| `rp_resetn` | *(none)* | Gap | nanosoc has no second reset port. If the RM needs to honor "held through swap" semantics distinct from the host-register reset, it must be ANDed with `dut_resetn` in the RM wrapper *before* the single `sys_sysresetn` pin — there is no way to route it separately into the DUT. |
| `dbg_resetn` | *(none)* | Gap | No discrete debug-domain reset port either; the SW-DP shares `sys_sysresetn` with the core. A host wanting to reset "just the debug logic" would do it at the SWD protocol level (SW-DP power-up/down request via `cpu_0_swdi`/`swclk` sequencing), not through a pin. Options for A6: (a) drop this signal for `rm_nanosoc` (no home), or (b) also fold it into the `sys_sysresetn` AND-tree alongside `rp_resetn`, accepting it resets the whole core, not just debug. |

### Processor debug — SWD (bidir, shell drives probe)

| partition-pins.md signal | nanosoc.sv port | Status | Notes |
|---|---|---|---|
| `swd_clk` | `cpu_0_swclk` (in) | Clean | Direct connect. |
| `swd_dio_o` | `cpu_0_swdi` (in) | Clean | Direct connect — this is the value the shell/host is driving onto DIO. |
| `swd_dio_oe` | *(none)* | Gap | nanosoc's SW-DP has no "host is driving" input pin; standard 2-pin SW-DP integrations don't need one (turnaround is timed by the protocol itself, watched by both ends via SWCLK). Not consumed by the DUT — informational only, for the shell's own physical-pad tristate arbitration. Safe to leave unconnected inside the RM. |
| `swd_dio_i` | `cpu_0_swdo` (out) + `cpu_0_swdoen` (out) | Clean | RM combines both: `swd_dio_i = cpu_0_swdoen ? cpu_0_swdo : swd_dio_o` (reflect the shell's own driven value back when the DUT isn't driving, so the shell's timing-based arbiter sees a consistent line). Current stub just ties `swd_dio_i = 0` (line 119) — needs this mux once nanosoc is instantiated. |

### Ethernet — RMII + MDIO

| partition-pins.md signal | nanosoc.sv port | Status | Notes |
|---|---|---|---|
| `phy_rmii_ref_clk`, `phy_rmii_crs_dv`, `phy_rmii_rxd[1:0]`, `phy_rmii_txd[1:0]`, `phy_rmii_tx_en`, `mdc`, `mdio_o`, `mdio_oe`, `mdio_i` (9 signals) | *(none)* | **Tie-off** | **This entire group is inert for `rm_nanosoc`.** Single-core nanosoc has no RMII MAC, no MDIO controller, no Ethernet pins anywhere in `nanosoc.sv`'s port list or sub-hierarchy. The RM wrapper stub's existing tie-offs (lines 120-124: `phy_rmii_txd=0`, `phy_rmii_tx_en=0`, `mdc=0`, `mdio_o=0`, `mdio_oe=0`) are already correct for this DUT and should stay. **A MAC-in-operation DUT needs the separate eth-ss subsystem** (the CPU0/`network_core` multicore variant, `nanosoc-multicore-system`) — out of scope for this repo's `nanosoc_m0_soc`. Single-core nanosoc alone exercises console/SWD/board-GPIO only, exactly as `partition-pins.md`'s own I1 decision anticipates for the deferred dual-core/eth variant. |

### Console / trace — AXI-Stream byte

| partition-pins.md signal | nanosoc.sv port | Status | Notes |
|---|---|---|---|
| `uart_tx_tdata[7:0]`, `uart_tx_tvalid`, `uart_tx_tready` | `p1_out[5]` / `p1_outen[5]` (raw serial TXD, via CMSDK UART2) | **Shim** | Needs a UART-bit-serial ⇄ AXI-Stream-byte transmitter (shift register + baud generator) inside the RM, driving `p1_out[5]`/reading `p1_in[5]` loopback, with `p1_outen[5]=1` once UART2 altfunc is enabled by firmware. TX direction is HW-proven working (PYNQ-Z2). |
| `uart_rx_tdata[7:0]`, `uart_rx_tvalid`, `uart_rx_tready` | `p1_in[4]` (raw serial RXD, via CMSDK UART2) | **Shim (RX known broken)** | Same shim, receive side, feeding `p1_in[4]`. **Flag clearly: the generated RTL's UART2 RX path does not reach the CPU today** (internal feedback stub, not the pad — `nanosoc_vivado_wrapper.v` lines 46-49). Wiring this is necessary for contract conformance but won't functionally deliver host→DUT bytes until that upstream RTL gap is fixed. |
| `swo` | *(none)* | **Gap** | No SWO/ITM on this Cortex-M0/M0+ core at all — not a missing wire, a missing feature class. Tie `swo=0` permanently for `rm_nanosoc` (stub already does this, line 128) and flag for A6 that this signal is structurally N/A for the single-core M0 DUT, not just unimplemented. |
| — | **Mandatory extra tie, not in the contract** | — | Regardless of the UART2/AXIS shim, `p1` also carries the FT1248/ADP self-drain wiring the proven design needs to avoid a boot hang: stage-0 BOOTROM prints its banner over the SoCDebug USRT2/FT1248 path before jumping to IMEM; with the port left floating, those writes back up and **hang the boot**. The RM must replicate `nanosoc_vivado_wrapper.v` lines 149-157: `p1_in[0]=p1_out[3]`, `p1_in[2]=0`, `p1_in[3]=`p1_out[3]` loopback, `p1_in[6]=1`, `p1_in[7]=1`, `p1_in[15:8]=0`. This has no partition-pins.md home (it's below the AXIS abstraction) — it's internal RM plumbing the shim needs to carry forward. |

### Status / misc (RP → shell)

| partition-pins.md signal | nanosoc.sv port | Status | Notes |
|---|---|---|---|
| `rm_id` | *(none — constant)* | Clean (by design) | Per the task brief's own expectation: this is an RM-assigned identity constant, not a DUT signal. Stub already does this correctly (`RM_ID_PLACEHOLDER = 32'hDEAD_0000`, line 97) — just needs a real per-variant ID scheme once one exists (A6, per the stub's own TODO). |
| `dut_lockup` | *(not exposed at nanosoc.sv top — exists one level down)* | **Gap (cheap fix available)** | `cpu_0_lockup` is a real output of `nanosoc_ss_cpu` (`build_soc/rtl/nanosoc_ss_cpu.sv` line 80) and is wired inside `nanosoc.sv` (as `u_ss_cpu_cpu_0_lockup`) into `u_ss_systemctrl`'s auto-reset-on-lockup logic (lines 529, 963) — but it is **never routed to a `nanosoc.sv` top-level port**. This is a template/generator wiring gap, not a missing feature: adding one output port to `nanosoc.sv` (regenerate from `soc_toplevel.sv.j2`) would close it cheaply. Until then, tie `dut_lockup=0` in the RM (stub already does, line 129) — this under-reports real lockup events to the shell's telemetry. |
| `irq_out` | *(not exposed at nanosoc.sv top — related signals exist one level down)* | Gap | `cpu_0_sleeping`/`cpu_0_sleepdeep`/`cpu_0_txev` all exist as `nanosoc_ss_cpu` outputs (same non-exposure pattern as `dut_lockup` above) but none is wired to a `nanosoc.sv` top port either. No natural single "spare IRQ/event" signal exists at the DUT boundary today. Tie `irq_out=0` (stub already does, line 130) until A6 picks which internal event (if any) should be exported and a top-level port is added upstream. |

### Board-port / GPIO passthrough (shell ⇄ RP) — I4

| partition-pins.md signal | nanosoc.sv port | Status | Notes |
|---|---|---|---|
| `dut_gpio_o[15:0]` | `p0_out[15:0]` | **Clean** | Exact 16-bit width match with `NGPIO=16` — the cleanest group in the whole contract. |
| `dut_gpio_oe[15:0]` | `p0_outen[15:0]` | Clean | Direct connect. |
| `dut_gpio_i[15:0]` | `p0_in[15:0]` | Clean | Direct connect. Note `p1` is fully consumed by the UART2/FT1248 muxing above and is **not** available for general board GPIO in this mapping — only `p0` is free. |

### nanosoc.sv ports with **no** partition-pins.md home

(the reverse gap — DUT ports the contract doesn't have a slot for)

| nanosoc.sv port(s) | Notes |
|---|---|
| `spi_sclk`, `spi_ss`, `spi_mosi`, `spi_miso` | Arm PL022 SSP master pins (`nanosoc.sv` lines 130-133). No SPI group in partition-pins.md at all (not even in the "deferred/optional" list). Candidate: fold into a future board-port SPI group, or leave permanently unconnected for v0 (no SPI peripheral on the MPS3/DFX plan yet) — A6 decision. |
| `exp_hsel`/`exp_haddr`/…/`exp_hreadyout` (AHB target, 18 signals), `exp_str_in_{0,1,2}_*`, `exp_str_out_{0,1,2}_*` (3 AXI-Stream pairs), `exp_irq[3:0]`, `exp_drq[1:0]`, `exp_dlast[1:0]` | The expansion-region AHB port + DMA streams. Contract line 8 states "no shell↔DUT AXI in v0" — consistent with tying this off exactly as `nanosoc_vivado_wrapper.v` already does (lines 217-267: `exp_hsel=0`, `exp_hready=1`, streams inert, IRQs/DRQs=0). No action needed; just carry that same tie-off pattern into `rm_nanosoc`. |
| `sys_xtalclk_out`, `sys_scanenable`, `sys_testmode`, `sys_scaninhclk`, `sys_scanouthclk` | ASIC scan/test/crystal-out ports, meaningless in an FPGA RM. Tie `sys_scanenable=sys_testmode=sys_scaninhclk=0` (mission mode), leave `sys_xtalclk_out`/`sys_scanouthclk` open — exactly as the proven wrapper does (lines 189-192). |
| `sys_hclk`, `sys_hresetn` (outputs) | Internal AHB clock/reset taps, used board-side only as LED-stretcher debug taps in the proven wrapper (`sys_hclk_dbg`, `sys_hresetn_o`, lines 100-103/174-175). No partition-pins.md slot; harmless to leave unconnected, or could feed a future shell debug-status CSR. |

---

## 2. Monolithic drop-in — wiring nanosoc into W1's `fpga/monolithic`

Target: `mps3-nanosoc-platform/fpga/monolithic/` (whole-XCKU115 MPS3
board-wrapper baseline, no DFX). **Important scoping note found while
reading `fpga/monolithic/README.md`:** W1's documented blocker (§2.4) is that
the *legacy* `nanosoc_chip` IP (from the old `arm_mps3` FPGA target in the
`nanosoc-multicore-system` repo) has **no RTL anywhere in that source tree** —
it's an orphaned pre-generator design. That is a different codebase than the
one this report maps. **`nanosoc_m0_soc`'s `nanosoc.sv` is real, current,
generator-emitted RTL and is a plausible candidate to fill that exact TODO**
— it matches the README's own suggested option (b): "write a *new*
`nanosoc_chip`-equivalent single-core top against the modern `nanosoc_gen`
generator." The wiring below assumes that path.

The `nanosoc_mps3.xdc` top comment (lines 21-25) already narrows the live
port set: **`UART_TX_F[2]`/`UART_RX_F[2]`, `OSCCLK[1]`, and
`CB_nRST`/`CS_nSRST`** are the only board pins the legacy design actually
drives/reads; everything else in that 547-constraint file is declared but
tied off by the wrapper. The same narrow set is what `nanosoc.sv` needs.

| MPS3 board pin(s) (`nanosoc_mps3.xdc`) | nanosoc.sv port | Notes |
|---|---|---|
| `OSCCLK[1]` (KU115 `AK16`, LVCMOS18, board osc, `create_clock -period 20.000` ⇒ **50 MHz**, xdc line 688) | `sys_clk` | 50 MHz is a legal `SYS_CLK_FREQ_HZ` override (pass as a parameter to the `nanosoc` instance) — likely usable directly without an MMCM, avoiding one build-time dependency. If the design needs the SoC's own proven 25 MHz PYNQ operating point instead, insert a `clk_wiz`/MMCM between `OSCCLK[1]` and `sys_clk` and re-derive `create_clock` accordingly. |
| `CB_nRST` (KU115 `AV23`, LVCMOS33, MCC-driven board/pushbutton reset, xdc line 296) | `sys_sysresetn` | `CB_nRST` is an async board-level signal (only constrained via `set_input_delay` relative to `OSCCLK[1]`, xdc lines 691-692) — needs a 2-3 FF reset synchronizer in the `sys_clk` domain before hitting `sys_sysresetn`, same discipline the RM-side contract mandates for `dut_resetn`. Consider ANDing with `CB_nPOR` (power-on-reset, `AU22`, xdc line 294) for a combined cold/warm reset. |
| `UART_TX_F[2]` (KU115 `AD28`, LVCMOS18, xdc line 116) | `p1_out[5]` (CMSDK UART2 TXD, via P1 GPIO mux) | Same `uart_txd_int = p1_outen[5] ? p1_out[5] : 1'b1` idle-high pattern as `nanosoc_vivado_wrapper.v` line 144. Confirmed by the xdc's own top comment as the one lane nanosoc's `nanosoc_design_wrapper.v` (legacy) wires to `cmsdk_apb_usrt` — same peripheral, same pin role. |
| `UART_RX_F[2]` (KU115 `AE28`, LVCMOS18, xdc line 126) | `p1_in[4]` (CMSDK UART2 RXD) | Same RX caveat as §1: not proven to reach the CPU in the currently generated RTL. Wire it anyway (future-proof once the upstream gap closes). |
| *(mandatory, not board-facing)* | `p1_in[0]=p1_out[3]`, `p1_in[2]=0`, `p1_in[3]=p1_out[3]`, `p1_in[6]=1`, `p1_in[7]=1`, `p1_in[15:8]=0` | Same FT1248/ADP self-drain + UART2-mode strap as §1 — **required to avoid the stage-0 BOOTROM boot hang**, not optional polish. |
| `USER_nLED[7:0]` (KU115 `AU32…AR30` etc., LVCMOS18, active-low, xdc lines 192-211) | `p0_out[7:0]` | `assign USER_nLED[7:0] = ~p0_out[7:0];` (active-low LEDs; invert). Tie `p0_outen[7:0]` high in firmware/board glue since MPS3 LEDs are plain outputs, not tristate. |
| `USER_SW[*]` (xdc lines 212+) | `p0_in[*]` | Reciprocal input path for the DIP switches, same `p0` port, remaining bits. |
| `CS_TDI`/`TDO`/`TMS`/`TCK`/`nSRST`/`nTRST` (CoreSight JTAG chain, xdc lines ~143-148) **or** `SH0_IO`/`SH1_IO` (Arduino shield header, xdc lines 404-483, commented in the xdc as "carries the PMOD0 debug/ADP-injection path wired in `nanosoc_design_wrapper.v` (SWDIO/SWCLK, FT1248 bit-bang)") | `cpu_0_swdi`/`swclk`/`swdo`/`swdoen` | **Optional for the "boots, UART prints" acceptance gate** (spec §14 step 1) — recommend tying `cpu_0_swdi=0`, `cpu_0_swclk=0` and leaving `swdo`/`swdoen` open for the minimal bring-up. If/when SWD access is wanted on the monolithic build, the xdc's own provenance comment names `SH0_IO`/`SH1_IO` as the legacy design's precedent path (not the `CS_*` CoreSight JTAG chain, which is a separate ARM DAP debug-header path with no built-in JTAG→SWD conversion for this specific SoC). |
| *(unused)* | `spi_*`, `exp_*`, `sys_scan*`, `sys_xtalclk_out` | Tie off exactly per §1's "no partition-pins.md home" table — same ties, same rationale, board-agnostic. |

**Net result:** filling W1's TODO with `nanosoc_m0_soc`'s `nanosoc.sv` needs,
at minimum: (1) a `nanosoc` instance parameterized for a 50 MHz (or MMCM'd)
`sys_clk`, (2) a reset synchronizer on `CB_nRST`, (3) the same P1
UART2-mux + FT1248-drain tie pattern already proven on PYNQ-Z2, (4) `p0`↔LED/
switch wiring, (5) everything else tied off inert. This is a materially
smaller lift than resurrecting the orphaned legacy `nanosoc_chip` IP
(README §2.4) since all the RTL already exists, builds, and has a
Vivado-proven wiring precedent to copy from.

---

## 3. Gaps / decisions for A6

1. **RMII/MDIO group (9 signals) is aspirational for `rm_nanosoc`.** Single-core
   nanosoc has no MAC. Confirm with A6 whether `rm_nanosoc` v0 simply ties this
   inert (as the stub already does) with the note "no networking claimed for
   this RM," or whether a MAC needs to be added/substituted — matching exactly
   the ambiguity the platform's own `fpga/monolithic/README.md` §2.2 already
   flagged independently for the legacy target.
2. **UART↔AXIS shim is required, not optional**, for the console/trace group
   (6 of the 30 signals) — nanosoc has no AXI-Stream console anywhere; it's a
   raw CMSDK UART2 muxed onto GPIO. Someone needs to write (or source) a small
   bit-serial⇄byte-stream UART core for the RM. Flag the known RX-path
   limitation (TX-only proven today) so it isn't mistaken for a new bug once
   wired.
3. **`swo` is structurally N/A** — this DUT's core has no ITM/SWO. Consider
   dropping it from the boundary for `rm_nanosoc` specifically, or documenting
   it as permanently tied for any Cortex-M0/M0+-based RM.
4. **Three-reset contract vs. nanosoc's one reset pin.** `rp_resetn` and
   `dbg_resetn` have no distinct nanosoc port — nanosoc only exposes
   `sys_sysresetn`. Decide whether the RM wrapper silently ANDs all three into
   that one pin (simplest, but changes reset semantics from what the contract
   implies) or whether `rp_resetn`/`dbg_resetn` are simply not meaningful for
   this particular DUT and should be documented as such.
5. **`dut_lockup`/`irq_out` are cheap generator fixes, not missing features.**
   `cpu_0_lockup` (and `cpu_0_sleeping`/`sleepdeep`/`txev`) already exist as
   real outputs of `nanosoc_ss_cpu` — they're just not routed to a
   `nanosoc.sv` top-level port by the current `soc_toplevel.sv.j2` template.
   If real telemetry is wanted, the fix is in the **generator**, not a
   from-scratch RTL feature — worth prioritizing over other gaps here since
   it's the cheapest to close.
6. **No SPI group in the contract at all** — nanosoc's PL022 SPI master pins
   have nowhere to go. Low priority (no SPI peripheral on the MPS3/DFX board
   plan yet) but worth a one-line acknowledgment in `partition-pins.md`'s
   deferred/optional list so it isn't rediscovered as a surprise later.
7. **Monolithic vs. RM path both need the same FT1248/ADP self-drain tie.**
   This is below the partition-pins.md abstraction (it's inside what becomes
   the UART shim) but is load-bearing — omitting it reproduces a real boot
   hang, not a cosmetic gap. Worth a shared note/helper so both the RM
   wrapper and the monolithic wrapper don't each rediscover it independently.
8. **Clock frequency choice.** Neither mapping is forced into a specific
   `sys_clk` frequency: `SYS_CLK_FREQ_HZ` defaults to 100 MHz, the HW-proven
   PYNQ-Z2 operating point is 25 MHz, and MPS3's `OSCCLK[1]` is a natural
   50 MHz. A6 should pick one canonical frequency (or explicitly allow both
   paths to differ) before timing constraints are written for either RM or
   monolithic builds.

---

## Sources read (all read-only, none modified)

- `nanosoc_m0_soc/build_soc/rtl/nanosoc.sv` (full 1349-line top, port list read in full)
- `nanosoc_m0_soc/build_soc/rtl/nanosoc_ss_cpu.sv` (CPU subsystem port list)
- `nanosoc_m0_soc/build_soc/rtl/nanosoc_chip.v`, `nanosoc_chip_pads.v` (ASIC pad-ring level, confirmed not the FPGA integration point)
- `nanosoc_m0_soc/pynq/vivado_ip/nanosoc_vivado_wrapper.v` (proven, HW-validated reference wiring — primary source for the UART/FT1248/SWD/GPIO patterns used above)
- `mps3-nanosoc-platform/docs/contracts/partition-pins.md` (v0.1)
- `mps3-nanosoc-platform/fpga/rp/nanosoc/rp_nanosoc_wrapper.sv` (RM stub)
- `mps3-nanosoc-platform/fpga/monolithic/README.md`, `nanosoc_mps3.xdc`
