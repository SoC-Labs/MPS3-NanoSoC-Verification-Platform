// -----------------------------------------------------------------------------
// rp_nanosoc_wrapper.sv — Reconfigurable Module (RM) top for the nanosoc DUT.
//
// A1/W1 fill (2026-07-04): instantiates the REAL single-core nanoSoC
// (module `nanosoc`, <nanosoc_m0_soc>/nanosoc_arch_tech/
// rtl/src/nanosoc/nanosoc.sv — read-only upstream, never modified) and wires
// it to the partition-pin boundary. Ports are EXACTLY the RP side of
// docs/contracts/partition-pins.md (directions mirrored from the shell's
// view — see README.md "Directions — mirror image of the shell's view").
// v0 DUT = single-core nanosoc (I1 RESOLVED; multicore deferred to the
// ethernet effort).
//
// No AXI(-Lite)/AHB ports here by design: partition-pins.md line 8 — "no
// shell<->DUT AXI in v0". Do not add any without an A6 contract change.
//
// IMPORTANT — port list reconciled 2026-07-10 (G3) against the SoC top the
// OOC synth ACTUALLY reads: `$SOCLABS_NANOSOC_SOC_DIR/pynq/filelist.tcl`
// read_verilog's `build_soc/rtl/nanosoc.sv` (the REGENERATED top), NOT the
// canonical `nanosoc_arch_tech/rtl/src/nanosoc/nanosoc.sv` this wrapper was
// first drafted against (2026-07-04). The two diverge in exactly two ways,
// both handled below:
//   - The resolved top HAS `spi_*` PL022 SSP ports (`spi_sclk/ss/mosi` out,
//     `spi_miso` in); the canonical top has none. There is no partition-pins
//     SPI group and no external SPI slave, so `spi_miso` is tied inert and the
//     three `spi_*` outputs are left open (see the instance below).
//   - The resolved top's `exp_*` expansion port is an AHB *slave* on nanosoc
//     (hsel/haddr/htrans/hsize/hprot/hwrite/hready/hwdata/hburst/hmastlock are
//     INPUTS; hrdata/hresp/hreadyout are OUTPUTS) — the INVERSE of the
//     canonical top's AHB-master directions. The earlier wrapper drove
//     constants (1'b1/1'b0/32'h0) into hreadyout/hresp/hrdata, which are
//     OUTPUTS in the resolved top → a hard elaboration error. Fixed below:
//     the unused slave inputs are tied inert (no transaction ever presented)
//     and the slave outputs are left open.
// If a future drift re-points the synth at the canonical top (spi_* removed /
// exp_* flipped back to master), re-check this instantiation against it.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module rp_nanosoc_wrapper #(
  parameter int NGPIO = 16,  // partition-pins.md "Board-port / GPIO
                              // passthrough -- I4": build parameter, v0
                              // default 16 (matches nanosoc's p0 GPIO port
                              // width exactly -- see "Board-port / GPIO"
                              // section below). Exposed as a parameter per
                              // fpga/dfx/rms/README.md's RM authoring
                              // contract item 2, matching rm_greybox/rm_led.
  parameter        UART_CLK_HZ = 50_000_000,  // = the shell's ACTUAL dut_clk
                              // (G4). shell_bd.tcl clk_wiz_dut
                              // CLKOUT1_REQUESTED_OUT_FREQ = 50.000 MHz ->
                              // clk_out1 -> rp_dut_clk; corroborated by
                              // nanosoc_ooc.xdc create_clock -period 20.000.
                              // The shim's baud divider counts real dut_clk
                              // cycles, so this MUST be the true 50 MHz.
  parameter        UART_BAUD   = 76800,        // Console baud (8N1) MATCHED to
                              // the delivered firmware. The baked `hello`
                              // image was built with NANOSOC_SYS_CLK_FREQ_HZ
                              // = 25 MHz -> BAUDDIV = 25e6/38400 = 651
                              // (verified in hello.lst). On the real 50 MHz
                              // dut_clk that BAUDDIV=651 image emits
                              // 50e6/651 ~= 76800 baud, so shim DIV =
                              // 50e6/76800 = 651 matches the firmware divider
                              // cycle-for-cycle -> legible banner. NOTE:
                              // firmware hardcodes 38400 in the READ-ONLY
                              // uart_stdout.c, so 115200 is unreachable
                              // without a firmware edit; if the firmware is
                              // rebuilt at 50 MHz (BAUDDIV -> 1302) set this
                              // to 38400.
  // The build supplies the ABSOLUTE path: ooc_synth.tcl computes it from
  // [info script] (<repo>/fpga/rp/nanosoc/hello_image.hex, or $IMEM_IMG) and
  // passes `synth_design -generic IMEM_MEM_FPGA_IMG=<abs>`; the IICE flow's
  // gen_prj.tcl bakes the same absolute path into its generated copy of this
  // file. $readmemh needs an absolute path because the Vivado OOC launch cwd
  // is not this directory, so this plain-filename default is only a fallback
  // for tools launched from fpga/rp/nanosoc/.
  parameter        IMEM_MEM_FPGA_IMG = "hello_image.hex",
                              // the baked IMEM image (G2): the arch_tech
                              // `hello` UART2 banner, word-format $readmemh
                              // loaded by sl_fpga_rom_word via the
                              // RAM_PRELOAD ROM variant of
                              // nanosoc_region_imem. Integrator may override
                              // at instantiation (the build always does,
                              // via the generic above).

  // -------------------------------------------------------------------------
  // DUT memory sizing. Byte-address widths: phys = 2**N.
  //
  // THE DEFAULTS ARE THE PRODUCT VALUES and must stay that way: 14 => 16 KB
  // IMEM and 16 KB DMEM, exactly what the SoC declares and exactly what an
  // ASIC would carry. This RM is bit-identical to its pre-parameterisation
  // self at these defaults.
  //
  // They are exposed ONLY so a derived, clearly-labelled SCAFFOLD RM
  // (fpga/rp/nanosoc_upy) can build a fat-BRAM DUT to host a MicroPython
  // interpreter, which does not fit in 16 KB. That is an FPGA-only
  // convenience: silicon cannot reproduce a 128 KB SRAM for free, so a fat
  // build is NOT the product and must never be presented as one. The product
  // path for large code is XiP from external QSPI flash, which keeps IMEM at
  // 16 KB on every target. See docs (one-config) and fpga/rp/nanosoc_upy.
  // -------------------------------------------------------------------------
  // SOCSCOPE=0 by default, and that matters: rm_nanosoc is a SHIPPED, silicon-proven
  // RM. Instrumenting it unconditionally would change a proven artefact's resources,
  // timing and `swo` behaviour for every existing user. B2 is a BUILD VARIANT --
  //     synth_design -generic SOCSCOPE=1 -generic IMEM_MEM_FPGA_IMG=<b2 image>
  // -- so the default build stays bit-for-bit the RM that already works.
  parameter        SOCSCOPE        = 0,
  parameter        IMEM_RAM_ADDR_W = 14,  // 16 KB -- product value. Do not raise here.
  parameter        DMEM_RAM_ADDR_W = 14   // 16 KB -- product value. Do not raise here.
) (
  // =========================================================================
  // Clocks & resets — shell -> RP (partition-pins.md lines 25-31).
  // All DUT clocks are generated in the static shell; no clock generation
  // inside this RM (contract "Clock/reset domain rule"). All three resets
  // arrive pre-synchronized-for-deassert from the shell; this module does
  // NOT re-synchronize them itself (that would double up the shell's own
  // CDC discipline).
  //
  // WIRING DECISION (documented per task brief, also see README.md
  // "Reset combination"): nanosoc has exactly ONE reset input,
  // `sys_sysresetn` — there is no second/third reset port to route
  // `rp_resetn`/`dbg_resetn` to separately (confirmed by reading the full
  // nanosoc.sv port list: no `dbg_resetn`/`rp_resetn`-shaped port exists).
  // All three contract resets are therefore ANDed together (active-low, so
  // ANY of the three asserting resets the whole core):
  //     sys_sysresetn_i = dut_resetn & rp_resetn & dbg_resetn;
  // This accepts that `rp_resetn`'s "held through swap" semantics and
  // `dbg_resetn`'s "just the debug logic" semantics both collapse onto a
  // whole-core reset for this DUT — flagged as an A6 gap (see README.md /
  // task reply), not silently swept under the rug.
  // =========================================================================
  input  logic dut_clk,       // DRP-reconfigurable DUT system clock
  input  logic dut_resetn,    // reset #1 — DUT system reset (host register)
  input  logic rp_resetn,     // reset #2 — reconfig reset, held through swap
  input  logic dbg_resetn,    // reset #3 — debug/SRST from OpenOCD path

  // =========================================================================
  // Processor debug — internal JTAG / SWJ-DP (partition-pins.md "Processor
  // debug"). Shell drives the TAP; this RM is the target (nanosoc's SoC-400
  // SWJ-DP, driven in JTAG mode). 3-out / 1-in, same shape as the old SWD
  // group. The SWJ straps (swj_enable/ntrst/npotrst) are tied INSIDE this
  // wrapper (see the DUT instance), never carried across the boundary.
  // =========================================================================
  input  logic jtag_tck,      // TCK -> nanosoc dap_swclktck
  input  logic jtag_tms,      // TMS -> nanosoc dap_swditms
  input  logic jtag_tdi,      // TDI -> nanosoc dap_tdi
  output logic jtag_tdo,      // TDO <- nanosoc dap_tdo, back to shell

  // =========================================================================
  // RM debug — BSCAN to an RM debug hub (partition-pins.md "RM debug",
  // 2026-10 ILA mint). This RM carries no hub: the 11 legs are ignored and
  // dbg_bscan_tdo is tied low.
  // =========================================================================
  input  logic dbg_bscan_bscanid_en,
  input  logic dbg_bscan_capture,
  input  logic dbg_bscan_drck,
  input  logic dbg_bscan_reset,
  input  logic dbg_bscan_runtest,
  input  logic dbg_bscan_sel,
  input  logic dbg_bscan_shift,
  input  logic dbg_bscan_tck,
  input  logic dbg_bscan_tdi,
  input  logic dbg_bscan_tms,
  input  logic dbg_bscan_update,
  output logic dbg_bscan_tdo,

  // =========================================================================
  // Ethernet — RMII + MDIO (partition-pins.md lines 41-52). Shell is the
  // virtual PHY: it sources phy_rmii_ref_clk and phy_rmii_rxd/crs_dv; this
  // RM's MAC drives phy_rmii_txd/tx_en and is the MDIO master.
  //
  // TIE-OFF, NOT WIRED: single-core nanosoc has NO ethernet MAC anywhere in
  // its port list or sub-hierarchy (confirmed by full nanosoc.sv read —
  // zero rmii_/mdio_/eth_/mac tokens). This entire group is structurally
  // inert for this DUT, same as rm_greybox/rm_led's ties.
  //
  // HDPR-29 / IOB packing note (partition-pins.md "IOB packing note",
  // fpga/ethernet/rmii_phy_if/README.md): phy_rmii_txd/tx_en leave this RM as
  // PLAIN FABRIC SIGNALS — no IOB/OLOGIC attributes here, the re-register
  // stage that wants that packing lives shell-side in rmii_phy_if.sv, not
  // in this RM. Do not add output register constraints to these ports.
  // =========================================================================
  input  logic       phy_rmii_ref_clk,
  input  logic       phy_rmii_crs_dv,
  input  logic [1:0] phy_rmii_rxd,
  output logic [1:0] phy_rmii_txd,
  output logic       phy_rmii_tx_en,
  output logic       mdc,        // DUT is MDIO master
  output logic       mdio_o,
  output logic       mdio_oe,
  input  logic       mdio_i,     // virtual-PHY register model's reply

  // =========================================================================
  // Console / trace — AXI-Stream byte, nanosoc cmsdk_apb_usrt (UART2)
  // (partition-pins.md lines 54-63). Bridged to nanosoc's raw serial
  // CMSDK UART2 GPIO pins (P1[5]=TXD, P1[4]=RXD) via uart_axis_shim.sv
  // (deliverable 2) — see the instantiation below.
  // =========================================================================
  output logic [7:0] uart_tx_tdata,
  output logic       uart_tx_tvalid,
  input  logic       uart_tx_tready,
  input  logic [7:0] uart_rx_tdata,
  input  logic       uart_rx_tvalid,
  output logic       uart_rx_tready,
  output logic       swo,        // Cortex-M SWO/ITM single-wire trace

  // =========================================================================
  // Status / misc — RP -> shell (partition-pins.md lines 65-70).
  // =========================================================================
  output logic [31:0] rm_id,      // RM identity register, for RM-load verify (spec §7/§13)
  output logic         dut_lockup, // core lockup indicator
  output logic         irq_out,    // spare DUT IRQ/event (optional)

  // =========================================================================
  // Board-port / GPIO passthrough — shell <-> RP (partition-pins.md v0.1, I4).
  // The DUT reaches MPS3 board I/O (LEDs/PMOD/FMC) THROUGH the shell; physical
  // pins are fixed in the shell constraints, so this boundary is pin-agnostic.
  // Maps directly onto nanosoc's GPIO port 0 (p0_*, 16 bits, exact width
  // match with NGPIO=16) — see README.md "Board-port / GPIO" for why P1 is
  // NOT available here (fully consumed by the UART2/FT1248 wiring below).
  // =========================================================================
  output logic [NGPIO-1:0] dut_gpio_o,  // DUT drive value -> shell -> board pad
  output logic [NGPIO-1:0] dut_gpio_oe, // DUT per-bit output-enable
  input  logic [NGPIO-1:0] dut_gpio_i,  // board pad -> shell -> DUT sample

  // =========================================================================
  // Flash / QSPI XiP — RP <-> shell (partition-pins.md v0.2 "Flash / QSPI
  // XiP"). WIRED here to nanosoc's own QSPI controller pads (qspi_flash_ahb):
  // the RM drives the SST26VF064B across the boundary. This is the product
  // XiP path (replaces the fat-BRAM upy scaffold). NON-CDC source-synchronous
  // crossing — the shell passes it matched (no synchronizers) and clamps it
  // deselected during a swap.
  //
  //   TODO (DEFERRED, per the boundary-freeze brief): XiP-EXECUTE is NOT yet
  //   proven — the cold-XiP backward-branch livelock, I/O timing signoff and
  //   silicon bring-up are separate follow-ups. The controller is present and
  //   register-accessible; THIS RM still boots from IMEM (hello_image.hex).
  // =========================================================================
  output logic       qspi_sclk,
  output logic       qspi_csn,
  output logic [3:0] qspi_io_o,
  output logic [3:0] qspi_io_oe,   // SoC controller port `qspi_io_e`
  input  logic [3:0] qspi_io_i
);

  // ===========================================================================
  // RM identity — per rm_list.tcl RM_LIB(rm_nanosoc,rm_id) = 0x01000001.
  // This is what the MicroBlaze coordinator reads back after a partial load to
  // confirm the correct RM is present (spec §7 "RM-load verification").
  //
  // ENCODING v2 (docs/VERSIONING_PLAN.md §3.2):
  //     { ver_major[31:24], ver_minor[23:16], design_id[15:0] }
  //   design_id 0x0001 (UNCHANGED — the low half preserves the old id),
  //   version   1.0     => 0x01000001.
  // Keep in lockstep with rm_list.tcl; scripts/harness_gates/
  // check_rm_id_encoding.py fails the build if this drifts from rm_list.tcl or
  // the overlay manifest.
  // ===========================================================================
  localparam logic [31:0] RM_ID_NANOSOC = 32'h0100_0001;

  assign rm_id = RM_ID_NANOSOC;

  // ===========================================================================
  // Reset combination (see header note above) — nanosoc's single
  // sys_sysresetn input is the AND of all three contract resets.
  // ===========================================================================
  logic dut_sys_sysresetn;
  assign dut_sys_sysresetn = dut_resetn & rp_resetn & dbg_resetn;

  // ===========================================================================
  // JTAG boundary (SoC-400 SWJ-DP driven as a 4-wire JTAG TAP). Unlike the old
  // SWD idiom there is NO shared bidirectional SWDIO wire, so the shell-side
  // reflection mux is gone: TDO is a pure unidirectional output straight from
  // the DUT's dap_tdo. The DUT's SWD-out legs (dap_swdo/dap_swdoen) and the
  // TDO tristate-enable (dap_ntdoen) are unused on an internal FPGA net and are
  // left open at the instance below. jtag_tck/tms/tdi drive dap_swclktck/
  // swditms/tdi directly.
  // ===========================================================================
  logic dap_tdo_w;
  assign jtag_tdo = dap_tdo_w;
  assign dbg_bscan_tdo = 1'b0;  // no RM debug hub (2026-10 ILA mint boundary)

  // ===========================================================================
  // GPIO port 0 — direct 1:1 to the board-port passthrough group (I4).
  // ===========================================================================
  logic [15:0] p0_in_w;
  logic [15:0] p0_out_w;
  logic [15:0] p0_outen_w;

  assign p0_in_w     = dut_gpio_i[15:0];

  // ===========================================================================
  // CLCD-KVM display tunnel + exp_* master nets (W3-A, docs/CLCD_KVM_WAVE_PLAN.md
  // Wave 3). Declared HERE, before the dut_gpio mux that reads them, for
  // declaration-before-use (the task's suggested "after the mdio tie-off"
  // anchor sits AFTER this mux — a forward reference the tools reject). The
  // DUT-side display accelerator lives in nanosoc's exp_* expansion AHB port
  // (0x6000_0000, a real MASTER after the W3-0 ooc_synth override) inside
  // nanosoc_exp_socket (instanced after u_nanosoc), and drives the on-board
  // panel through the tunnel on the spare upper 8 bits of dut_gpio_o/oe.
  //   exp_*_w  : the exp_* AHB-Lite master<->socket nets.
  //   lcd_*    : the socket's display pins (ALL ACTIVE-HIGH; the shell KVM
  //              inverts to the panel's active-low pads — this is a SAFETY
  //              property, docs/contracts/dut-display-tunnel.md §3).
  logic        exp_hsel_w, exp_hwrite_w, exp_hready_w, exp_hmastlock_w;
  logic [31:0] exp_haddr_w, exp_hwdata_w, exp_hrdata_w;
  logic [1:0]  exp_htrans_w;
  logic [2:0]  exp_hsize_w, exp_hburst_w;
  logic [3:0]  exp_hprot_w;
  logic        exp_hreadyout_w, exp_hresp_w;

  logic        lcd_en;
  logic [7:0]  lcd_pd;
  logic        lcd_cs, lcd_wr, lcd_rs, lcd_busy, lcd_req;
  logic [3:0]  exp_irq_w;
  logic [1:0]  exp_drq_w;

  // dut_gpio mux. Low byte [7:0] = GPIO/LEDs, UNTOUCHED (board_gpio's LED path
  // keeps working while the DUT drives the display). High byte [15:8] = the
  // display tunnel when the socket drives it (lcd_en), else GPIO port 0's upper
  // byte (Tier-0 bit-bang, or a display-less RM). The lcd_en busy-gate lives
  // HERE (not in ahb_clcd): a disabled socket forwards GPIO, so its intrinsic
  // lcd_busy is never tunnelled. Bit map is FROZEN — do NOT restate it without
  // citing docs/contracts/dut-display-tunnel.md §2:
  //   dut_gpio_o[15:8]  = PD[7:0]
  //   dut_gpio_oe[15:8] = {spare, req, busy, pd_oe, rd, rs, wr, cs}  (MSB..LSB)
  // strobes ACTIVE-HIGH; reserved bits (spare/pd_oe/rd) driven 0.
  assign dut_gpio_o  = { lcd_en ? lcd_pd : p0_out_w[15:8],
                         p0_out_w[7:0] };
  assign dut_gpio_oe = { lcd_en ? {1'b0,      // [15] spare  RESERVED = 0
                                   lcd_req,   // [14] req
                                   lcd_busy,  // [13] busy
                                   1'b0,      // [12] pd_oe  RESERVED = 0
                                   1'b0,      // [11] rd     RESERVED = 0
                                   lcd_rs,    // [10] rs
                                   lcd_wr,    // [9]  wr
                                   lcd_cs}    // [8]  cs
                                : p0_outen_w[15:8],
                         p0_outen_w[7:0] };

  // ===========================================================================
  // GPIO port 1 — entirely consumed by the CMSDK UART2 / FT1248 hostio4
  // wiring below (see README.md "Board-port / GPIO": P1 is NOT available
  // for the dut_gpio_* group in this mapping, only P0 is free).
  //
  // The tie pattern on p1_in[6:0]/[15:8] mirrors, bit-for-bit, the
  // HW-proven nanosoc_vivado_wrapper.v (PYNQ-Z2) pattern — required to
  // avoid a real stage-0 BOOTROM boot hang (the SoCDebug USRT2/FT1248
  // self-drain: without it, the bootrom's boot-banner print over the
  // FT1248 controller backs up waiting for a host that isn't there). P1[5]/
  // P1[4] (UART2 TXD/RXD) are the only two bits that connect to something
  // "real" — the uart_axis_shim instance below — everything else on P1 is
  // static plumbing, not a DUT feature.
  // ===========================================================================
  logic [15:0] p1_in_w;
  logic [15:0] p1_out_w;
  logic [15:0] p1_outen_w;

  // UART2 TXD: idle-high until firmware sets ALTFUNC and actually drives
  // P1[5] (same idiom as nanosoc_vivado_wrapper.v's uart_txd_int).
  logic uart_txd_int;
  assign uart_txd_int = p1_outen_w[5] ? p1_out_w[5] : 1'b1;

  logic shim_dut_rxd;  // uart_axis_shim's serializer output -> nanosoc RXD

  assign p1_in_w[0]    = p1_out_w[3];  // FT_MISO <= FT_SSN loopback (idle
                                        // high = "RX empty", no spurious
                                        // reads; low during transfers = no
                                        // write NAK)
  assign p1_in_w[1]    = p1_out_w[1];  // FT_CLK readback (no external FTDI)
  assign p1_in_w[2]    = 1'b0;         // FT_MIOSIO TXE# = 0 ("can accept")
  assign p1_in_w[3]    = p1_out_w[3];  // FT_SSN readback
  assign p1_in_w[4]    = shim_dut_rxd; // UART2 RXD <= uart_axis_shim serializer
  assign p1_in_w[5]    = uart_txd_int; // UART2 TXD readback loopback
  assign p1_in_w[6]    = 1'b1;         // reserved
  assign p1_in_w[7]    = 1'b1;         // FT1248MODE strap (FT1248/UART2 mode)
  assign p1_in_w[15:8] = 8'h00;        // unused GPIO/altfunc bits

  // ===========================================================================
  // UART <-> AXIS shim (deliverable 2). See uart_axis_shim.sv header for the
  // CLK_HZ/BAUD assumptions — both are RM parameters here so an integrator
  // can override them at instantiation without editing this file.
  // ===========================================================================
  uart_axis_shim #(
    .CLK_HZ (UART_CLK_HZ),
    .BAUD   (UART_BAUD)
  ) u_uart_axis_shim (
    .clk            (dut_clk),
    .resetn         (dut_sys_sysresetn),

    .dut_txd_i      (uart_txd_int),
    .dut_rxd_o      (shim_dut_rxd),

    .uart_tx_tdata  (uart_tx_tdata),
    .uart_tx_tvalid (uart_tx_tvalid),
    .uart_tx_tready (uart_tx_tready),

    .uart_rx_tdata  (uart_rx_tdata),
    .uart_rx_tvalid (uart_rx_tvalid),
    .uart_rx_tready (uart_rx_tready)
  );

  // ===========================================================================
  // No SWO on this DUT: nanosoc's CPU is Cortex-M0 (no ITM/SWO — that's an
  // M3/M4/M33 feature). Structurally N/A, not just unimplemented (A6 gap #4).
  // ===========================================================================
  // ===========================================================================
  // SoCScope trace plane -- MPS3 bring-up B2.
  //
  // B1 proved the transport with traffic generated inside the RM. This is the
  // step that points the probe at a REAL nanoSoC bus: `exp_*`, nanosoc's
  // expansion AHB master at 0x6000_0000. The transfers are the CPU's own, over
  // the real interconnect, which is the whole difference.
  //
  // WHY exp_* AND NOT AN INTERNAL BUS. `nanosoc.sv` is REGENERATED from the SoC
  // description, so anything tapped inside it is wiped by the next regen. exp_*
  // is already at this wrapper's level and is a genuine AHB-Lite master.
  //
  // WHICH READY. `exp_hready_w` is nanosoc's GLOBAL bus ready (out to the socket);
  // `exp_hreadyout_w` is the socket's own. A transfer completes when the MASTER
  // sees ready, so the probe samples the global one -- taking the slave's would
  // record transfers as complete that the interconnect had not yet finished.
  //
  // THE CSR IS ON NO DUT BUS. socscope_cfg configures the block from inside the
  // RM. Hanging the CSR off a DUT bus would put it where the probe may be
  // watching, so reading a status register would itself be a transfer that fills
  // the ring the status register counts.
  //
  // PASSIVE BY CONSTRUCTION: every exp_* connection below is an INPUT to the
  // probe. B2's control is that the DUT behaves identically with the probe
  // enabled and disabled -- this wiring is what makes that claim checkable rather
  // than a code review.
  // ===========================================================================
  generate if (SOCSCOPE != 0) begin : g_socscope
  wire        ss_csr_hsel, ss_csr_hwrite, ss_csr_hreadyout, ss_csr_hresp;
  wire [31:0] ss_csr_haddr, ss_csr_hwdata, ss_csr_hrdata;
  wire [1:0]  ss_csr_htrans;
  wire [2:0]  ss_csr_hsize;

  socscope_cfg #(
      .DIVISOR   (24),               // must equal the shell's UART_OVER_ETH_SWO_DIVISOR
      .FILT_BASE (32'h6000_0000),    // the expansion window
      .FILT_MASK (32'hFFFF_0000)
  ) u_socscope_cfg (
      .clk(dut_clk), .resetn(dut_resetn),
      .csr_hsel(ss_csr_hsel), .csr_haddr(ss_csr_haddr), .csr_htrans(ss_csr_htrans),
      .csr_hwrite(ss_csr_hwrite), .csr_hsize(ss_csr_hsize), .csr_hwdata(ss_csr_hwdata),
      .csr_hreadyout(ss_csr_hreadyout),
      /* verilator lint_off PINCONNECTEMPTY */
      .configured()
      /* verilator lint_on PINCONNECTEMPTY */
  );

  socscope_trace_top #(
      .DEPTH    (64),
      .CSR_BASE (32'h0000_0000),
      .TICKDIV  (64),                // HW-010: 84 ms of span, so stamps do not saturate
      .FREEZE   (0)                  // B2 does not gate the DUT clock
  ) u_socscope (
      .clk(dut_clk), .resetn(dut_resetn),
      .trace_clk(dut_clk), .trace_resetn(dut_resetn),

      // The observed bus -- ALL INPUTS.
      .obs_haddr (exp_haddr_w),  .obs_htrans(exp_htrans_w), .obs_hwrite(exp_hwrite_w),
      .obs_hsize (exp_hsize_w),  .obs_hwdata(exp_hwdata_w), .obs_hrdata(exp_hrdata_w),
      .obs_hready(exp_hready_w), .obs_hresp (exp_hresp_w),

      .csr_hsel(ss_csr_hsel), .csr_haddr(ss_csr_haddr), .csr_htrans(ss_csr_htrans),
      .csr_hwrite(ss_csr_hwrite), .csr_hsize(ss_csr_hsize), .csr_hwdata(ss_csr_hwdata),
      .csr_hrdata(ss_csr_hrdata), .csr_hreadyout(ss_csr_hreadyout),
      .csr_hresp(ss_csr_hresp),

      .txd(swo),

      .freeze_cmd_stb(1'b0), .freeze_cmd(2'd0), .freeze_cmd_n(32'd0),
      .freeze_ext_run(1'b0),
      /* verilator lint_off PINCONNECTEMPTY */
      .dut_clk_en(), .frozen(), .stepping(), .wdog_fired(),
      .step_rem(), .delivered(), .wdog_rem()
      /* verilator lint_on PINCONNECTEMPTY */
  );
  end else begin : g_no_socscope
    // The RM as it shipped: swo driven inert, exactly as before.
    assign swo = 1'b0;
  end endgenerate

  // ===========================================================================
  // dut_lockup / irq_out: cpu_0_lockup (and cpu_0_sleeping/sleepdeep/txev)
  // exist as REAL outputs of nanosoc_ss_cpu one level down, but are not
  // routed to a nanosoc.sv top-level port by the current RTL — there is
  // nothing at this wrapper's boundary to wire them from without modifying
  // nanosoc.sv (read-only, out of scope). Tie inert; flagged as an A6 /
  // generator-template gap (A6 gaps #5, #6 — see README.md / task reply).
  // ===========================================================================
  assign dut_lockup = 1'b0;
  assign irq_out    = 1'b0;

  // ===========================================================================
  // Ethernet — RMII + MDIO: no MAC in this DUT, tie off exactly as
  // rm_greybox/rm_led do.
  // ===========================================================================
  assign phy_rmii_txd   = 2'b00;
  assign phy_rmii_tx_en = 1'b0;
  assign mdc            = 1'b0;
  assign mdio_o          = 1'b0;
  assign mdio_oe         = 1'b0;

  // ===========================================================================
  // nanosoc instance — the real single-core SoC. Expansion (`exp_*`), DMA
  // streams, IRQ/DRQ, and scan/test ports have no partition-pins.md home
  // (contract line 8, "no shell<->DUT AXI in v0") and are tied off benign
  // below, matching the disposition (if not the exact port directions —
  // see header note) of the proven nanosoc_vivado_wrapper.v.
  // ===========================================================================
  nanosoc #(
    .IMEM_MEM_FPGA_IMG (IMEM_MEM_FPGA_IMG),
    // Memory sizing is now a wrapper parameter so a derived RM can build a
    // larger DUT without forking this file. The DEFAULTS ARE THE SoC's OWN
    // (14 => 16 KB each), so this RM is bit-identical to before — see the
    // parameter declarations above for why the fat sizes are scaffold-only.
    .IMEM_RAM_ADDR_W   (IMEM_RAM_ADDR_W),
    .DMEM_RAM_ADDR_W   (DMEM_RAM_ADDR_W)
  ) u_nanosoc (
    // Clock / reset
    .sys_clk         (dut_clk),
    .sys_sysresetn   (dut_sys_sysresetn),
    .sys_xtalclk_out (),                 // ASIC crystal-out, meaningless on FPGA

    // Scan / test — tied off for mission mode
    .sys_scanenable  (1'b0),
    .sys_testmode    (1'b0),
    .sys_scaninhclk  (1'b0),
    .sys_scanouthclk (),

    // SoC-400 SWJ-DP — driven as a 4-wire JTAG TAP (partition boundary).
    //   TCK/TMS/TDI in from the shell; TDO out to the shell.
    //   swj_enable/ntrst/npotrst are RM-internal straps (NOT partition pins).
    //   SWD-out (swdo/swdoen) + TDO tristate (ntdoen) are unused on this
    //   internal net -> left open.
    .dap_swclktck   (jtag_tck),
    .dap_swditms    (jtag_tms),
    .dap_tdi        (jtag_tdi),
    .dap_tdo        (dap_tdo_w),
    .dap_swdo       (),
    .dap_swdoen     (),
    .dap_ntdoen     (),
    .dap_ntrst      (1'b1),                // TAP TRSTn deasserted (TAP kept live)
    .dap_npotrst    (dut_sys_sysresetn),   // SWJ-DP power-on reset = wrapper reset
    .dap_swj_enable (1'b1),                // SWJ-DP enabled (single-chiplet SoC)

    // GPIO port 0 — board-port passthrough (I4)
    .p0_in     (p0_in_w),
    .p0_out    (p0_out_w),
    .p0_outen  (p0_outen_w),
    // GPIO port 1 — UART2/FT1248 plumbing (see above)
    .p1_in     (p1_in_w),
    .p1_out    (p1_out_w),
    .p1_outen  (p1_outen_w),

    // Internal AHB clock/reset taps (board-level debug taps only upstream;
    // no partition-pins.md home) — left open.
    .sys_hclk    (),
    .sys_hresetn (),

    // Expansion region AHB port — a real AHB-Lite MASTER out of nanosoc after
    // the W3-0 ooc_synth.tcl exp_* direction override (docs/CLCD_KVM_WAVE_PLAN.md
    // Wave 3, W0-A). Post-flip nanosoc DRIVES hsel/haddr/htrans/hsize/hprot/
    // hwrite/hwdata/hburst/hmastlock and the muxed bus-ready (exp_hready is
    // nanosoc's hreadymux OUTPUT), and SAMPLES hrdata/hresp/hreadyout back from
    // the slave. Wired to nanosoc_exp_socket (instanced after this module);
    // exp_hready_w is nanosoc's global-ready OUT, feeding the socket's hready
    // input, and the socket's hreadyout drives nanosoc's exp_hreadyout IN.
    .exp_hsel      (exp_hsel_w),
    .exp_haddr     (exp_haddr_w),
    .exp_htrans    (exp_htrans_w),
    .exp_hsize     (exp_hsize_w),
    .exp_hprot     (exp_hprot_w),
    .exp_hwrite    (exp_hwrite_w),
    .exp_hready    (exp_hready_w),
    .exp_hwdata    (exp_hwdata_w),
    .exp_hburst    (exp_hburst_w),
    .exp_hmastlock (exp_hmastlock_w),
    .exp_hreadyout (exp_hreadyout_w),
    .exp_hresp     (exp_hresp_w),
    .exp_hrdata    (exp_hrdata_w),

    // DMA Stream 0..2: DMAC -> Expansion (nanosoc drives out; sink it)
    .exp_str_in_0_tvalid (),
    .exp_str_in_0_tready (1'b1),
    .exp_str_in_0_tdata  (),
    .exp_str_in_0_tstrb  (),
    .exp_str_in_0_tlast  (),
    .exp_str_in_1_tvalid (),
    .exp_str_in_1_tready (1'b1),
    .exp_str_in_1_tdata  (),
    .exp_str_in_1_tstrb  (),
    .exp_str_in_1_tlast  (),
    .exp_str_in_2_tvalid (),
    .exp_str_in_2_tready (1'b1),
    .exp_str_in_2_tdata  (),
    .exp_str_in_2_tstrb  (),
    .exp_str_in_2_tlast  (),

    // DMA Stream 0..2: Expansion -> DMAC (nothing to offer in)
    .exp_str_out_0_tvalid (1'b0),
    .exp_str_out_0_tready (),
    .exp_str_out_0_tdata  (32'h0000_0000),
    .exp_str_out_0_tstrb  (4'b0000),
    .exp_str_out_0_tlast  (1'b0),
    .exp_str_out_0_flush  (),
    .exp_str_out_1_tvalid (1'b0),
    .exp_str_out_1_tready (),
    .exp_str_out_1_tdata  (32'h0000_0000),
    .exp_str_out_1_tstrb  (4'b0000),
    .exp_str_out_1_tlast  (1'b0),
    .exp_str_out_1_flush  (),
    .exp_str_out_2_tvalid (1'b0),
    .exp_str_out_2_tready (),
    .exp_str_out_2_tdata  (32'h0000_0000),
    .exp_str_out_2_tstrb  (4'b0000),
    .exp_str_out_2_tlast  (1'b0),
    .exp_str_out_2_flush  (),

    // Expansion interrupt / DMA request lines — driven by nanosoc_exp_socket
    // (the reference ahb_clcd leaves both at 0; a student's accelerator raises
    // exp_irq[0] on "FIFO not full" and drives exp_drq[0] for DMAC 0 — Tier 3).
    .exp_irq   (exp_irq_w),
    .exp_drq   (exp_drq_w),
    .exp_dlast (),

    // SPI (PL022 SSP) — present in the RESOLVED build_soc/rtl/nanosoc.sv (G3),
    // absent from the canonical top this wrapper was first authored against.
    // No partition-pins.md SPI group and no external SPI slave: tie the lone
    // input (miso) inert and leave the three outputs (sclk/ss/mosi) open.
    .spi_sclk  (),
    .spi_ss    (),
    .spi_mosi  (),
    .spi_miso  (1'b0),

    // QSPI flash controller pads — NOW PROMOTED to partition pins (v0.2
    // boundary freeze, 2026-07-15). The controller reaches the board's external
    // SST26VF064B across the RP<->shell boundary via the qspi_* partition pins
    // above. The SoC's `qspi_io_e` output-enable maps to the contract's
    // `qspi_io_oe`. XiP-EXECUTE remains DEFERRED (see the port-list TODO) — the
    // controller is register-accessible but this RM still boots from IMEM.
    .qspi_sclk (qspi_sclk),
    .qspi_csn  (qspi_csn),
    .qspi_io_o (qspi_io_o),
    .qspi_io_e (qspi_io_oe),
    .qspi_io_i (qspi_io_i)
  );

  // ===========================================================================
  // nanosoc_exp_socket — THE HOLE (W3-A, docs/CLCD_KVM_WAVE_PLAN.md Wave 3).
  // The DUT-side display accelerator (reference: ahb_clcd) lives inside this
  // socket: an AHB-Lite SLAVE hung off nanosoc's exp_* master (0x6000_0000,
  // 256 MB), driving the panel out through the tunnel above. This is
  // RM-INTERNAL — no partition-pin change, so only rm_nanosoc rebuilds and
  // pin_check stays green. The port list is FROZEN (fpga/rp/nanosoc_exp/
  // README.md §2 / nanosoc_exp_socket.sv): there is deliberately NO lcd_rd and
  // NO lcd_pd_oe — the panel is WRITE-ONLY (docs/CLCD_PANEL_FACTS.md §5/§7.2),
  // and the tunnel's rd/pd_oe/spare bits are driven 0 by the mux above.
  //   hready  = the GLOBAL bus-ready nanosoc drives out (exp_hready_w).
  //   hreadyout = the socket's OWN ready, back into nanosoc's exp_hreadyout.
  // Clock/reset: the same dut_clk / dut_sys_sysresetn that clock nanosoc.
  // ===========================================================================
  nanosoc_exp_socket #(
    .ADDR_W (32),
    .DATA_W (32)
  ) u_exp_socket (
    .hclk      (dut_clk),
    .hresetn   (dut_sys_sysresetn),

    .hsel      (exp_hsel_w),
    .haddr     (exp_haddr_w),
    .htrans    (exp_htrans_w),
    .hwrite    (exp_hwrite_w),
    .hsize     (exp_hsize_w),
    .hburst    (exp_hburst_w),
    .hprot     (exp_hprot_w),
    .hmastlock (exp_hmastlock_w),
    .hwdata    (exp_hwdata_w),
    .hready    (exp_hready_w),
    .hrdata    (exp_hrdata_w),
    .hreadyout (exp_hreadyout_w),
    .hresp     (exp_hresp_w),

    .lcd_en    (lcd_en),
    .lcd_pd    (lcd_pd),
    .lcd_cs    (lcd_cs),
    .lcd_wr    (lcd_wr),
    .lcd_rs    (lcd_rs),
    .lcd_busy  (lcd_busy),
    .lcd_req   (lcd_req),

    .irq       (exp_irq_w),
    .drq       (exp_drq_w)
  );

endmodule
