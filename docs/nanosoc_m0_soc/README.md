# nanosoc_m0_soc — single-core DUT source (D2b RESOLVED)

> **Status: HISTORICAL** — a record of a read-only survey of the single-core `nanosoc_m0_soc` source tree as of 2026-08-07.
> Superseded by / current state in [docs/STATUS.md](../STATUS.md).
> Kept for provenance; do not update.

The single-core nanoSoC RTL lives at **`~/SoCLabs/nanosoc_m0_soc`**
(user-pointed, 2026-07-04). This resolves OPEN_ISSUES **D2b** — W1 correctly
found the *legacy* `arm_mps3` target's `nanosoc_chip` RTL was never checked in,
but this repo is the real, buildable single-core source. Three survey agents
characterized it; full detail in the sibling files:

- **`RTL_ANATOMY.md`** (N1) — top module, ports, peripherals, flists.
- **`FPGA_FLOW.md`** (N2) — build/generator flow, IP deps, firmware.
- **`PLATFORM_MAPPING.md`** (N3) — mapping to the RM wrapper + monolithic top.

## Headline decisions

- **Integration module = `nanosoc`** (module `nanosoc`, source
  `nanosoc_arch_tech/rtl/src/nanosoc/nanosoc.sv`, ~72 ports). Wrap **this**
  directly for both the MPS3 monolithic baseline and the DFX `rm_nanosoc` — the
  same pattern proven on PYNQ-Z2. Do **not** go through `nanosoc_system` /
  `nanosoc_chip`: N1 confirmed a real synthesis bug in the generated
  expansion-region wiring (`exp_hsel` etc. driven `input` both ends). (N3 read
  the generator-emitted `build_soc/rtl/nanosoc.sv` — same module, generated
  location; source it via the flist below, not by hand-picking a file.)
- **Single Cortex-M0** confirmed (SoC Labs `slcorem0` around Arm Cortex-M0 from
  `$ARM_IP_LIBRARY_PATH` (site Arm IP library), **read-only**).
- **No ethernet MAC** (all three agents). The platform's MAC-in-operation DUT
  needs the separate eth-ss subsystem; single-core nanosoc exercises
  console/SWD/GPIO only.
- **Console = CMSDK UART2 @ 0x40006000, raw serial** (muxed onto GPIO P1:
  `p1_out[5]`=TXD, `p1_in[4]`=RXD), **not** AXI-Stream. The partition contract's
  AXIS console needs a **UART↔AXIS shim** inside the RM. Known limitation: UART2
  RX is TX-only in the currently generated RTL (upstream fix).
- **FPGA flist = `nanosoc_arch_tech/rtl/flist/nanosoc_FPGA.flist`** (pulls the
  full hierarchy + CMSDK/Cortex-M0 IP). `build_soc/rtl/` is regenerable via
  `make soc_model` (nanosoc_gen + python3.10). Firmware "hello" (CMSDK UART2)
  preloads into IMEM at synth time; landmine: patch `NANOSOC_SYS_CLK_FREQ_HZ`
  to the FPGA clock.
- **Existing FPGA target = PYNQ-Z2 (xc7z020)** — the pattern to mirror. An
  MPS3/KU115 target is net-new (rewrite the PS-dependent BD/wrapper/XDC; the
  IP-packaging step + RTL flist carry over).

## Partition-pin mapping (30 signals, partition-pins.md v0.1)

| Bucket | Count | Signals |
|---|---|---|
| Map cleanly | 9 | dut_clk, dut_resetn, swd_clk, swd_dio_o, swd_dio_i, rm_id (constant), dut_gpio_o/oe/i |
| Via UART↔AXIS shim | 6 | uart_tx_* / uart_rx_* (RX side pending the upstream RTL fix) |
| Tie off inert | 9 | the entire RMII + MDIO group (no MAC) |
| Top-port gaps | 6 | rp_resetn, dbg_resetn, swd_dio_oe, swo, dut_lockup, irq_out (some exist in `nanosoc_ss_cpu` one level down — cheap generator-template fix) |

## Next steps this unblocks
1. Fill `fpga/rp/nanosoc/rp_nanosoc_wrapper.sv` — instantiate `nanosoc`, wire the
   9 clean pins, add the UART↔AXIS shim, tie off RMII/MDIO, assign `rm_id=0x1`.
2. Fill W1's `fpga/monolithic` top with `nanosoc` (N3's wiring table: OSCCLK[1]→
   sys_clk, CB_nRST→reset sync, UART2 P1 mux → MPS3 UART pins, GPIO→LEDs).
3. Decide the 6 top-port gaps: accept tie-offs for v0, or a small generator-
   template change to route dut_lockup/irq_out/swo out of `nanosoc`.
