// -----------------------------------------------------------------------------
// shell_top.sv — MPS3/XCKU115 harness top level ("u_top" per
// docs/contracts/partition-pins.md v0.1's resolved I4 topology).
//
// u_top = u_shell (this BD's Vivado-generated wrapper, `shell_bd_wrapper`,
// instantiated below as `u_shell`) + u_rp_dut (the RP cell, instantiated
// directly here as `u_rp_dut`, SIBLING of u_shell — not nested inside the BD).
// This is the exact pattern the contract's I4 resolution calls for
// ("the RP is a hierarchical cell *alongside* the shell in one top design
// ... fixes A2's RP instance path (u_top/u_rp_dut)"), and mirrors
// fpga/dfx/proof/rp_shell_top.sv's own structure. NOTE for A2: because
// shell_top IS the netlist top, the Vivado cell path of the RP inside the
// checkpoints this build writes is plain `u_rp_dut` (a top-level cell) —
// pass that as build_dfx.tcl's <rp_inst> tclarg, exactly like the proof
// stand-in static (build_dfx.tcl's own header documents this case).
//
// `u_rp_dut` binds to fpga/shell/rp_dut_stub.sv in THIS (non-DFX, harness)
// build — a synthesizable, all-outputs-tied stub with the exact
// partition-pins.md port list (rm_id readback = ASCII "STUB"), because a
// plain empty black box trips DRC INBB-3 at opt_design in a non-DFX flow.
// The DONT_TOUCH attribute below keeps the RP cell boundary intact through
// synth/opt (no constant propagation across partition pins, no hierarchy
// dissolution) so the post-synth checkpoint this build writes remains
// black-box-able for the separate DFX flow (fpga/dfx/build_dfx.tcl does
// `update_design -cells <rp_inst> -black_box` conceptually via its
// clearing/linking steps; a dissolved or constant-swept boundary would
// break that).
//
// HD.RECONFIGURABLE is NOT set anywhere in this file or in shell_bd.tcl —
// per the task scope, that property (plus dfx_floorplan.xdc's Pblock) is
// applied by the separate DFX build (fpga/dfx/) when it re-opens this same
// static design to build real per-RM partials.
//
// BOARD PORT LIST: every port below is pin-constrained in
// fpga/shell/constraints/mps3_harness.xdc (names match 1:1). Ports that
// existed in earlier drafts but have NO known MPS3 package pin were removed
// from the top and safely stubbed internally instead (do-not-invent-pins
// rule): TELEM I2C pads (no FPGA-reachable power-monitor bus exists on MPS3:
// the board has an AD7490 voltage ADC and no INA-class part, so current/power
// cannot be measured at all — sda_i tied high, scl/sda outputs left internal),
// the 20-pin CoreSight header (no
// partition-pin route exists in the contract — documented A6 gap).
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module shell_top (
  // -- Clock / reset (proven pins: fpga/dfx/proof/proof.xdc) --
  input  wire        OSCCLK1,        // board oscillator, 50 MHz (spec §5)
  input  wire        USER_nPB0,      // push-button, active-low — system POR
  input  wire        USER_nPB1,      // push-button, active-low (AT32) — CLCD-KVM
                                      // ownership toggle; sync+debounced inside
                                      // clcd_kvm so it works when firmware wedges

  // -- LEDs / DIP switches (via board_gpio_0's pad group — see mapping note
  //    below; pins from fpga/monolithic/nanosoc_mps3.xdc, HW-proven set) --
  output wire [7:0]  USER_nLED,      // active-low LEDs
  input  wire [7:0]  USER_SW,        // DIP switches

  // -- LAN9220 static-memory-bus (SMC) host interface. Pin names/groups per
  //    fpga/monolithic/nanosoc_mps3.xdc ("LAN9220 Ethernet SMC interface" +
  //    "SMBF_* static memory bus"); wiring to the AXI EMC external interface
  //    is done below (see the EMC mapping note). --
  output wire        ETH_nCS,
  output wire        ETH_nOE,
  input  wire        ETH_INT,
  output wire [6:0]  SMBF_ADDR,
  inout  wire [15:0] SMBF_DATA,
  output wire        SMBF_FIFOSEL,
  output wire        SMBF_nOE,
  output wire        SMBF_nWE,
  output wire        SMBF_nRST,

  // -- MicroBlaze console (FT4232 channel [1] — see mps3_harness.xdc's
  //    channel-allocation note; [0]=MCC-reserved, [2]=legacy DUT console) --
  output wire        MB_UART_TXD,
  input  wire        MB_UART_RXD,

  // -- External QSPI flash (SST26VF064B). Pins from
  //    fpga/monolithic/nanosoc_mps3.xdc "Quad SPI boot/overlay flash" group.
  //    D16 RESOLVED (2026-07-15, QSPI boundary freeze): these pads are now
  //    owned by the RP's own QSPI controller for XiP (partition-pins.md v0.2).
  //    The RP drive crosses the boundary through the DFX decoupler (clamped
  //    deselected during a swap) and the SST26 pads are formed by the IOBUFs
  //    below. The shell has no SPI master on them (its axi_quad_spi_0 was
  //    removed with D13; usd_spi drives the USER microSD). All four data
  //    lanes are INOUT for quad XiP (io0-io3); SCLK/nCS are controller-driven.
  inout  wire        QSPI_D0,
  inout  wire        QSPI_D1,
  inout  wire        QSPI_D2,
  inout  wire        QSPI_D3,
  inout  wire        QSPI_SCLK,
  inout  wire        QSPI_nCS,

  // -- On-board QVGA CLCD (Himax HX8347-D, 8-bit 8080 parallel). 14 non-touch
  //    pads; the 4 touch pads (CLCD_TSCL/TSDA/TINT/TNC) are OUT of v1 and
  //    stay unconstrained/reserved (ip/clcd/README.md:99-100). Pin comments
  //    from nanosoc_mps3.xdc:495-522; CLCD_PD is inout[17:10] as in the
  //    monolithic top (nanosoc_mps3_top.sv:454). --
  inout  wire [17:10] CLCD_PD,      // 8-bit parallel data bus (bidirectional)
  output wire         CLCD_RD,      // 8080 read strobe   (AM15, active-low)
  output wire         CLCD_RS,      // reg/data select    (AN14, 0=cmd 1=data)
  output wire         CLCD_CS,      // chip select        (AP15, active-low)
  output wire         CLCD_WR_SCL,  // 8080 write strobe  (AP14, active-low)
  output wire         CLCD_BL,      // backlight           (AJ16)

  // -- MCC-facing tie-offs (board-manager handover D1; MPS3 TRM Table 2-3
  //    "Minimum RTL for correct operation"). Constant-driven below; pins in
  //    mps3_harness.xdc. Were floating (UNUSEDPIN Pullnone) until 2026-10. --
  output wire         SMBM_nWAIT,     // MCC SMB wait, tie HIGH  (AP28)
  output wire         WDOG_RREQ,      // watchdog reset req, tie LOW (AU19)
  output wire         IOFPGA_SYSWDT,  // RTCC/SYS_CFG req, tie LOW   (AU20)
  output wire         CFG_DATAOUT,    // SCC serial data out, tie LOW (AV18)

  // -- USER microSD slot (D13; usd_spi_0 @0x44A4_0000, SPI mode). FPGA-wired,
  //    and NOT the MCC config card. Pins from the Arm MPS3 pinmap
  //    (fpga_pinmap.xdc:749-762), all LVCMOS33, PULLUP on CMD/DAT/NCD in
  //    mps3_harness.xdc. CMD and DAT[3:0] are inout (IOBUF) so a later native
  //    4-bit SD controller needs no pin change; in SPI mode CMD=MOSI,
  //    DAT[0]=MISO, DAT[3]=CS#, DAT[1]/DAT[2] held high-Z. --
  output wire         USD_CLK,        // SCK                  (AU15)
  inout  wire         USD_CMD,        // MOSI                 (AU16)
  inout  wire [3:0]   USD_DAT,        // [0] MISO, [3] CS#    (AV14/AV13/AT13/AT12)
  input  wire         USD_NCD,        // card detect          (AT15)
`ifdef MPS3_SHELL_TOUCH
  // Phase-2 resistive-touch I2C (UNBUILT DRAFT, gated). Preprocessed OUT by
  // default so the shipped build is unchanged; the mint defines MPS3_SHELL_TOUCH
  // AND sets env SHELL_TOUCH=1 (BD) together. Pads constrained by
  // mps3_harness_touch.xdc; AXI IIC master + TINT concat in touch_iic_add.tcl.
  inout  wire         CLCD_TSCL,    // touch I2C SCL       (AL18)
  inout  wire         CLCD_TSDA,    // touch I2C SDA       (AJ15)
  input  wire         CLCD_TINT,    // touch pen-down IRQ  (AJ14, active-low)
`endif
  output wire         CLCD_RST      // panel reset         (AK18, active-low)
`ifdef MPS3_SHELL_REALPHY
  // -- Real external LAN8720 (Waveshare) RMII PHY on the MPS3 shield
  //    (SHELL_REALPHY, NON-DEFAULT). Measured arm_mps3 pin map, LVCMOS33, in
  //    fpga/shell/constraints_realphy/*.xdc (added explicitly under the flag,
  //    NOT globbed). Option A: the FPGA SOURCES the 50 MHz REF_CLK and drives
  //    it OUT to the PHY's REFCLK-in strap. Leading-comma style so the
  //    non-realphy build keeps CLCD_RST as the final comma-less port. --
  , output wire        ETH_RMII_REF_CLK  // 50 MHz REF_CLK out   (BB14, SH0_IO7)
  , output wire [1:0]  ETH_RMII_TXD      // TX di-bits           (BA14/AW19)
  , output wire        ETH_RMII_TX_EN    // TX enable            (AY12)
  , input  wire        ETH_RMII_CRS_DV   // carrier / RX-valid   (BB15)
  , input  wire [1:0]  ETH_RMII_RXD      // RX di-bits           (BA15/BA13)
  , output wire        ETH_MDC           // MDIO clock           (AU12)
  , inout  wire        ETH_MDIO          // MDIO data (bidir)    (BA12)
`endif
`ifdef MPS3_SHELL_CPU_MBV
  // -- MicroBlaze V shell only (SHELL_CPU=mbv; build_shell.tcl sets the define
  //    and adds fpga/shell/constraints/mbv/*.xdc together). The DDR4 SODIMM
  //    and its OSC6 100 MHz reference, straight through to the BD wrapper's
  //    c0_ddr4 interface (cpu_mbv.tcl renames it so these names match the
  //    XDC) -- the MIG instantiates its own I/O buffers. PACKAGE_PINs in
  //    constraints/mbv/ddr4_pins.xdc; SLR1 banks 49-51, clear of the RP.
  //    Leading-comma style, like the real-PHY block, so any combination of
  //    the gated groups leaves a legal port list.
  , input  wire        c0_sys_clk_p      // OSC6 100 MHz diff ref  (H19)
  , input  wire        c0_sys_clk_n      //                        (H18)
  , output wire        c0_ddr4_act_n
  , output wire [16:0] c0_ddr4_adr
  , output wire [1:0]  c0_ddr4_ba
  , output wire [0:0]  c0_ddr4_bg
  , output wire [0:0]  c0_ddr4_ck_c
  , output wire [0:0]  c0_ddr4_ck_t
  , output wire [0:0]  c0_ddr4_cke
  , output wire [0:0]  c0_ddr4_cs_n
  , inout  wire [7:0]  c0_ddr4_dm_n
  , inout  wire [63:0] c0_ddr4_dq
  , inout  wire [7:0]  c0_ddr4_dqs_c
  , inout  wire [7:0]  c0_ddr4_dqs_t
  , output wire [0:0]  c0_ddr4_odt
  , output wire        c0_ddr4_reset_n
`endif
);

  // =========================================================================
  // Board clock / reset conditioning — same pattern as the proven
  // fpga/dfx/proof/rp_shell_top.sv proof shell (IBUF/BUFG the oscillator).
  // The BD's own clk_wiz_shell/clk_wiz_dut + proc_sys_reset_shell take it
  // from here (PRIM_SOURCE=No_buffer, so this top owns the input buffer).
  // =========================================================================
  wire osc_clk_ibuf;
  IBUF  u_osc_ibuf (.I(OSCCLK1), .O(osc_clk_ibuf));
  wire osc_clk_50m;
  BUFG  u_osc_bufg (.I(osc_clk_ibuf), .O(osc_clk_50m));

  // MCC-facing tie-offs (D1). SMBM_nWAIT high = never stretch an MCC SMB
  // cycle; the three requests/data lines low = idle. D2 (SCC/SYS_CFG) or D3
  // (WDOG_RREQ from a register) replace these constants if they are built.
  assign SMBM_nWAIT    = 1'b1;
  assign WDOG_RREQ     = 1'b0;
  assign IOFPGA_SYSWDT = 1'b0;
  assign CFG_DATAOUT   = 1'b0;

  wire sys_rst_n = USER_nPB0;  // active-low; the BD's proc_sys_reset_shell
                                // + dut_clkrst_0 both do their own
                                // async-assert/sync-deassert internally —
                                // no extra synchronizer needed here.

  // =========================================================================
  // RP partition-pin nets (partition-pins.md v0.1, EXACT names — matches
  // rp_dut's port list 1:1). Prefix-free here; the BD's own ports use an
  // `rp_` prefix (shell_bd.tcl SECTION 0) purely to keep its own port list
  // visually grouped — this file un-prefixes them at the single point where
  // they cross from u_shell into u_rp_dut.
  // =========================================================================
  wire        w_dut_clk, w_dut_resetn, w_rp_resetn, w_dbg_resetn;
  wire        w_jtag_tck, w_jtag_tms, w_jtag_tdi, w_jtag_tdo;
  // RM debug BSCAN group (boundary.yaml `dbgbscan`, ILA-over-XVC): debug_bridge_0's
  // m0_bscan master port. 11 shell-driven legs straight through to the RP; the
  // RP's dbg_bscan_tdo returns through the BD's decoupler (INTF 19, clamp 0).
  // w_dbg_bscan_tck is the bridge's soft-BSCAN TCK, already on a static BUFGCE
  // inside debug_bridge_0 — no buffer is added here (the RP has no BUFG site).
  wire        w_dbg_bscan_bscanid_en, w_dbg_bscan_capture, w_dbg_bscan_drck;
  wire        w_dbg_bscan_reset, w_dbg_bscan_runtest, w_dbg_bscan_sel;
  wire        w_dbg_bscan_shift, w_dbg_bscan_tck, w_dbg_bscan_tdi;
  wire        w_dbg_bscan_tms, w_dbg_bscan_update, w_dbg_bscan_tdo;
  wire        w_phy_rmii_ref_clk, w_phy_rmii_crs_dv;
  wire [1:0]  w_phy_rmii_rxd, w_phy_rmii_txd;
  wire        w_phy_rmii_tx_en, w_mdc, w_mdio_o, w_mdio_oe, w_mdio_i;
  wire [7:0]  w_uart_tx_tdata, w_uart_rx_tdata;
  wire        w_uart_tx_tvalid, w_uart_tx_tready;
  wire        w_uart_rx_tvalid, w_uart_rx_tready, w_swo;
  wire [31:0] w_rm_id;
  wire        w_dut_lockup, w_irq_out;
  wire [15:0] w_dut_gpio_o, w_dut_gpio_oe, w_dut_gpio_i;
  // Flash / QSPI XiP boundary (partition-pins.md v0.2). RP-drive legs (w_qspi_*
  // from u_rp_dut) go INTO the BD; the decoupler-clamped result comes back on
  // w_qspi_pad_* and forms the SST26 pads via the IOBUFs below. w_qspi_io_i is
  // the pad sample driven straight back to the RP (non-CDC pass-through — it
  // never enters the BD/decoupler).
  wire        w_qspi_sclk, w_qspi_csn;
  wire [3:0]  w_qspi_io_o, w_qspi_io_oe, w_qspi_io_i;
  wire        w_qspi_pad_sclk, w_qspi_pad_csn;
  wire [3:0]  w_qspi_pad_io_o, w_qspi_pad_io_oe;

  // =========================================================================
  // EMC (LAN9220) glue nets. Confirmed against the real Vivado-generated
  // shell_bd_wrapper.v (2024.1, emc_rtl EMC_INTF flattening): addr[31:0],
  // ce_n[0:0], oen[0:0], wen (scalar), dq_io[15:0] (wrapper contains the
  // IOBUFs), wait[0:0] input, plus SRAM-unused outputs (adv_ldn, ben[1:0],
  // ce[0:0], clken, cre, lbon, qwen[1:0], rnw, rpn) left unconnected.
  //
  // Address mapping: axi_emc's mem_a is a BYTE address (C_MEM_A_LSB=0 in the
  // generated core); the LAN9220's SMBF_ADDR[6:0] is a 16-bit-word address
  // into its 256-byte CSR/FIFO window (fpga/ethernet/lan9220_if/README.md
  // pin table) -> SMBF_ADDR = emc_addr[7:1].
  // FLAG (board-unverifiable here): confirm on hardware with the LAN9220
  // BYTE_TEST register read (offset 0x64 must return 0x87654321) before
  // trusting the driver; if it reads shifted, the alternative is
  // emc_addr[6:0].
  // =========================================================================
  wire [31:0] emc_addr;
  wire        emc_ce_n, emc_oen, emc_wen;

  assign ETH_nCS      = emc_ce_n;
  assign ETH_nOE      = emc_oen;   // device-specific OE pad …
  assign SMBF_nOE     = emc_oen;   // … and the shared-bus OE strobe: both
                                   // driven from the EMC's oen (the legacy
                                   // pinmap keeps them as two distinct pads;
                                   // no phase relationship invented — see
                                   // lan9220_if/README.md "ETH_nOE vs
                                   // SMBF_nOE" open item).
  assign SMBF_nWE     = emc_wen;
  assign SMBF_ADDR    = emc_addr[7:1];
  assign SMBF_FIFOSEL = 1'b0;      // full CSR/FIFO address decode mode (the
                                   // FIFO port is reachable at its normal
                                   // addresses; fast-burst FIFO_SEL mode is
                                   // a firmware/driver optimization, not
                                   // wired in v0 — lan9220_if/README.md).
  assign SMBF_nRST    = sys_rst_n; // hold the LAN9220 (+shared USB debug
                                   // FIFO bus) in reset while the board POR
                                   // button is held; firmware does the
                                   // datasheet-timed soft-reset sequencing.

  // =========================================================================
  // u_shell — the static shell BD wrapper (fpga/shell/bd/shell_bd.tcl via
  // build_shell.tcl's `make_wrapper`). Every scalar/vector port name below
  // is confirmed against the real generated shell_bd_wrapper.v; the two
  // interface ports (EMC_INTF_*, SPI_0_*) use the real 2024.1 flattened
  // names (the earlier `ifdef MPS3_SHELL_*_WRAPPER_CONFIRMED` guesses are
  // resolved — guards removed).
  // =========================================================================
  wire [15:0] board_gpio_pad_o, board_gpio_pad_oe, board_gpio_pad_i;

  // CLCD block <-> pad glue (triplet on the data bus + single-ended controls)
  wire [7:0] w_clcd_pd_o, w_clcd_pd_i;
  wire       w_clcd_pd_oe;
  wire       w_clcd_cs_n, w_clcd_wr_n, w_clcd_rd_n, w_clcd_rs, w_clcd_bl, w_clcd_rst_n;

  // USD (user microSD) block <-> pad glue (usd_spi's split-tristate pairs)
  wire w_usd_clk_o,  w_usd_clk_oe;
  wire w_usd_cmd_o,  w_usd_cmd_oe;
  wire w_usd_dat0_i;
  wire w_usd_dat3_o, w_usd_dat3_oe;

`ifdef MPS3_SHELL_TOUCH
  // Phase-2 touch glue (UNBUILT DRAFT, gated). The BD's AXI IIC forms SCL/SDA as
  // 3-state (i/o/t); shell_top makes the open-drain pads (below) and 2FF-syncs
  // the async pen-down onto the board clock before the BD's INTC concat.
  wire w_tscl_i, w_tscl_o, w_tscl_t;
  wire w_tsda_i, w_tsda_o, w_tsda_t;
  (* ASYNC_REG = "TRUE" *) reg [1:0] r_tint_sync = 2'b00;
  always @(posedge osc_clk_50m) r_tint_sync <= {r_tint_sync[0], CLCD_TINT};
  wire w_tint_sync = r_tint_sync[1];
`endif

`ifdef MPS3_SHELL_REALPHY
  // Real-PHY BD<->pad glue nets (SHELL_REALPHY). TX group + REF_CLK are BD
  // OUTPUTS (RP-driven, decoupler-clamped inside the BD); RX group + MDIO-in
  // are BD INPUTS driven by the shield pads. w_phy_pad_ref_clk == the shell's
  // clk_wiz_shell/clk_out2 50 MHz reference, used here both as the TX
  // re-register/clock-forward source and driven OUT to the PHY.
  wire        w_phy_pad_ref_clk;
  wire [1:0]  w_phy_pad_txd;
  wire        w_phy_pad_tx_en;
  wire        w_phy_pad_mdc;
  wire        w_phy_pad_mdio_o, w_phy_pad_mdio_oe, w_phy_pad_mdio_i;
  wire        w_phy_pad_crs_dv;
  wire [1:0]  w_phy_pad_rxd;
`endif

  shell_bd_wrapper u_shell (
    .osc_clk_50m (osc_clk_50m),
    .sys_rst_n   (sys_rst_n),

    // -- LAN9220 host I/F (AXI EMC external interface; see glue above) --
    .EMC_INTF_addr   (emc_addr),
    .EMC_INTF_ce_n   (emc_ce_n),
    .EMC_INTF_oen    (emc_oen),
    .EMC_INTF_wen    (emc_wen),
    .EMC_INTF_dq_io  (SMBF_DATA),   // wrapper-internal IOBUFs drive the pad
    .EMC_INTF_wait   (1'b0),        // LAN9220 SMC has no wait/ready pin
    // EMC_INTF_{adv_ldn,ben,ce,clken,cre,lbon,qwen,rnw,rpn}: SRAM-mode
    // unused outputs, intentionally unconnected.
`ifdef MPS3_SHELL_CPU_MBV
    // MicroBlaze V / Linux: the LAN9220 keeps its NATIVE open-drain ACTIVE-LOW
    // IRQ (pad PULLUP in mps3_harness.xdc idles it high), and the INTC input is
    // ACTIVE-HIGH LEVEL, so invert here. Silicon-proven 2026-07-19 on the July
    // fork, where the other two halves were ALSO needed and must ship with
    // this: the pad PULLUP, and NO smsc,irq-active-high / smsc,irq-push-pull in
    // the DTS. (Programming INT_CFG to push-pull active-high instead asserts
    // the pin with nothing pending and livelocks `ip link set eth0 up`.)
    // SHELL_CONTRACT.md §4, INTC In3.
    .eth_irq         (~ETH_INT),
`else
    .eth_irq         (ETH_INT),     // FLAG: LAN9220 IRQ defaults to
                                    // active-low open-drain; firmware MUST
                                    // program IRQ_CFG (IRQ_POL=1, push-pull)
                                    // before enabling this line in the AXI
                                    // INTC (firmware/smsc911x driver step).
                                    // The bare-metal firmware polls and never
                                    // enables it; see the MBV branch above.
`endif

    // -- MicroBlaze console (BD port names uart_*_f; physical pins are the
    //    FT4232 channel-1 pair, named MB_UART_* in the XDC) --
    .uart_tx_f   (MB_UART_TXD),
    .uart_rx_f   (MB_UART_RXD),

    // -- External QSPI flash boundary (QSPI freeze 2026-07-15). The shell
    //    has no SPI master on the SST26 (axi_quad_spi_0 removed with D13).
    //    RP-drive legs go in; the decoupler-clamped drive comes back out to the
    //    SST26 IOBUFs below. --
    .rp_qspi_sclk   (w_qspi_sclk),
    .rp_qspi_csn    (w_qspi_csn),
    .rp_qspi_io_o   (w_qspi_io_o),
    .rp_qspi_io_oe  (w_qspi_io_oe),
    .qspi_pad_sclk  (w_qspi_pad_sclk),
    .qspi_pad_csn   (w_qspi_pad_csn),
    .qspi_pad_io_o  (w_qspi_pad_io_o),
    .qspi_pad_io_oe (w_qspi_pad_io_oe),

    // -- Board GPIO pad group (mapping to LEDs/switches below) --
    .board_gpio_pad_o  (board_gpio_pad_o),
    .board_gpio_pad_oe (board_gpio_pad_oe),
    .board_gpio_pad_i  (board_gpio_pad_i),

    // -- CLCD 8080 bus master (clcd_0). BD exposes the block's o/i/oe triplet
    //    like board_gpio's pad group; pads are formed at this top (§2d). --
    .clcd_pd_o   (w_clcd_pd_o),
    .clcd_pd_i   (w_clcd_pd_i),
    .clcd_pd_oe  (w_clcd_pd_oe),
    .clcd_cs_n   (w_clcd_cs_n),
    .clcd_wr_n   (w_clcd_wr_n),
    .clcd_rd_n   (w_clcd_rd_n),
    .clcd_rs     (w_clcd_rs),
    .clcd_bl     (w_clcd_bl),
    .clcd_rst_n  (w_clcd_rst_n),

    // -- USD (user microSD, usd_spi_0). o/oe pairs out, MISO and card-detect
    //    in; pads formed below. USD_NCD goes in raw: usd_spi synchronises and
    //    debounces it. --
    .usd_clk_o   (w_usd_clk_o),
    .usd_clk_oe  (w_usd_clk_oe),
    .usd_cmd_o   (w_usd_cmd_o),
    .usd_cmd_oe  (w_usd_cmd_oe),
    .usd_dat0_i  (w_usd_dat0_i),
    .usd_dat3_o  (w_usd_dat3_o),
    .usd_dat3_oe (w_usd_dat3_oe),
    .usd_ncd_i   (USD_NCD),

    // -- CLCD-KVM ownership button (Wave 4): raw async active-low AT32 pad
    //    straight into the BD; clcd_kvm does the sync + debounce. --
    .user_npb1   (USER_nPB1),

    // -- TELEM I2C: no MPS3 pin exists for an FPGA-driven power-monitor bus.
    //    The board carries an AD7490 voltage ADC and NO INA-class current/
    //    power monitor, so current/power cannot be measured on this board at
    //    all — outputs left unconnected, input tied to idle-high. The BD still
    //    exposes the seam so a future board/pin fact only touches this file. --
    .i2c_sda_i (1'b1),
    // i2c_scl_o / i2c_scl_t / i2c_sda_o / i2c_sda_t: intentionally
    // unconnected (no physical pad; engine itself is the documented telem
    // follow-up seam).

`ifdef MPS3_SHELL_TOUCH
    // -- Phase-2 touch AXI IIC (UNBUILT DRAFT, gated): BD ports added by
    //    touch_iic_add.tcl; pads formed by the IOBUFs below. --
    .iic_scl_i   (w_tscl_i),
    .iic_scl_o   (w_tscl_o),
    .iic_scl_t   (w_tscl_t),
    .iic_sda_i   (w_tsda_i),
    .iic_sda_o   (w_tsda_o),
    .iic_sda_t   (w_tsda_t),
    .clcd_tint_i (w_tint_sync),
`endif

    // -- RP partition-pin boundary (BD ports -> pins on this instance,
    //    per the contract's I4 resolution — see file header note) --
    .rp_dut_clk       (w_dut_clk),
    .rp_dut_resetn    (w_dut_resetn),
    .rp_rp_resetn     (w_rp_resetn),
    .rp_dbg_resetn    (w_dbg_resetn),

    .rp_jtag_tck      (w_jtag_tck),
    .rp_jtag_tms      (w_jtag_tms),
    .rp_jtag_tdi      (w_jtag_tdi),
    .rp_jtag_tdo      (w_jtag_tdo),

    // RM debug BSCAN group (ILA-over-XVC; tdo comes back through the decoupler)
    .rp_dbg_bscan_bscanid_en (w_dbg_bscan_bscanid_en),
    .rp_dbg_bscan_capture    (w_dbg_bscan_capture),
    .rp_dbg_bscan_drck       (w_dbg_bscan_drck),
    .rp_dbg_bscan_reset      (w_dbg_bscan_reset),
    .rp_dbg_bscan_runtest    (w_dbg_bscan_runtest),
    .rp_dbg_bscan_sel        (w_dbg_bscan_sel),
    .rp_dbg_bscan_shift      (w_dbg_bscan_shift),
    .rp_dbg_bscan_tck        (w_dbg_bscan_tck),
    .rp_dbg_bscan_tdi        (w_dbg_bscan_tdi),
    .rp_dbg_bscan_tms        (w_dbg_bscan_tms),
    .rp_dbg_bscan_update     (w_dbg_bscan_update),
    .rp_dbg_bscan_tdo        (w_dbg_bscan_tdo),

    .rp_phy_rmii_ref_clk (w_phy_rmii_ref_clk),
    .rp_phy_rmii_crs_dv  (w_phy_rmii_crs_dv),
    .rp_phy_rmii_rxd     (w_phy_rmii_rxd),
    .rp_phy_rmii_txd     (w_phy_rmii_txd),
    .rp_phy_rmii_tx_en   (w_phy_rmii_tx_en),
    .rp_mdc              (w_mdc),
    .rp_mdio_o           (w_mdio_o),
    .rp_mdio_oe          (w_mdio_oe),
    .rp_mdio_i           (w_mdio_i),

    .rp_uart_tx_tdata  (w_uart_tx_tdata),
    .rp_uart_tx_tvalid (w_uart_tx_tvalid),
    .rp_uart_tx_tready (w_uart_tx_tready),
    .rp_uart_rx_tdata  (w_uart_rx_tdata),
    .rp_uart_rx_tvalid (w_uart_rx_tvalid),
    .rp_uart_rx_tready (w_uart_rx_tready),
    .rp_swo            (w_swo),

    .rp_rm_id       (w_rm_id),
    .rp_dut_lockup  (w_dut_lockup),
    .rp_irq_out     (w_irq_out),

    .rp_dut_gpio_o  (w_dut_gpio_o),
    .rp_dut_gpio_oe (w_dut_gpio_oe),
    .rp_dut_gpio_i  (w_dut_gpio_i)
`ifdef MPS3_SHELL_REALPHY
    // Real-PHY pad group (present on the wrapper only when the BD was built with
    // SHELL_REALPHY=1 — build_shell.tcl sets the env and this define together).
    , .phy_pad_rmii_ref_clk (w_phy_pad_ref_clk)
    , .phy_pad_rmii_txd     (w_phy_pad_txd)
    , .phy_pad_rmii_tx_en   (w_phy_pad_tx_en)
    , .phy_pad_mdc          (w_phy_pad_mdc)
    , .phy_pad_mdio_o       (w_phy_pad_mdio_o)
    , .phy_pad_mdio_oe      (w_phy_pad_mdio_oe)
    , .phy_pad_rmii_crs_dv  (w_phy_pad_crs_dv)
    , .phy_pad_rmii_rxd     (w_phy_pad_rxd)
    , .phy_pad_mdio_i       (w_phy_pad_mdio_i)
`endif
`ifdef MPS3_SHELL_CPU_MBV
    // DDR4 (present on the wrapper only when the BD was built with
    // SHELL_CPU=mbv -- build_shell.tcl sets the env and this define together).
    , .c0_sys_clk_p    (c0_sys_clk_p)
    , .c0_sys_clk_n    (c0_sys_clk_n)
    , .c0_ddr4_act_n   (c0_ddr4_act_n)
    , .c0_ddr4_adr     (c0_ddr4_adr)
    , .c0_ddr4_ba      (c0_ddr4_ba)
    , .c0_ddr4_bg      (c0_ddr4_bg)
    , .c0_ddr4_ck_c    (c0_ddr4_ck_c)
    , .c0_ddr4_ck_t    (c0_ddr4_ck_t)
    , .c0_ddr4_cke     (c0_ddr4_cke)
    , .c0_ddr4_cs_n    (c0_ddr4_cs_n)
    , .c0_ddr4_dm_n    (c0_ddr4_dm_n)
    , .c0_ddr4_dq      (c0_ddr4_dq)
    , .c0_ddr4_dqs_c   (c0_ddr4_dqs_c)
    , .c0_ddr4_dqs_t   (c0_ddr4_dqs_t)
    , .c0_ddr4_odt     (c0_ddr4_odt)
    , .c0_ddr4_reset_n (c0_ddr4_reset_n)
`endif
  );

  // =========================================================================
  // External QSPI flash (SST26VF064B) pads — RP-owned XiP path (D16 resolved,
  // 2026-07-15). The RP's QSPI controller drives these across the partition
  // boundary; the drive is decoupler-clamped (deselected during a swap) and
  // arrives on w_qspi_pad_*. SCLK/nCS are always driven (T=0); io0-io3 are
  // per-lane tri-stated by w_qspi_pad_io_oe and their pad value is sampled back
  // to the RP on w_qspi_io_i (this is the shell->RP pass-through leg — NOT
  // decoupled, matched to the RP-drive legs' depth).
  //
  // Pad map (mps3_harness.xdc): QSPI_D0=AU24(io0) .. QSPI_D3=AV22(io3),
  // QSPI_SCLK=AT25, QSPI_nCS=AT24. In quad XiP all four data pads are IO.
  //
  // TODO (DEFERRED — QSPI boundary freeze): I/O timing signoff of this
  // source-synchronous path (set_input/output_delay vs the SST26VF064B
  // datasheet, matched register depth) and XiP-EXECUTE bring-up are follow-ups.
  // mps3_harness_timing.xdc still carries the old set_false_path on these ports
  // — that is a placeholder, NOT signoff; retighten when timing is done.
  IOBUF u_qspi_sclk_iobuf (.IO(QSPI_SCLK), .I(w_qspi_pad_sclk), .T(1'b0),                 .O());
  IOBUF u_qspi_ncs_iobuf  (.IO(QSPI_nCS),  .I(w_qspi_pad_csn),  .T(1'b0),                 .O());
  IOBUF u_qspi_d0_iobuf   (.IO(QSPI_D0),   .I(w_qspi_pad_io_o[0]), .T(~w_qspi_pad_io_oe[0]), .O(w_qspi_io_i[0]));
  IOBUF u_qspi_d1_iobuf   (.IO(QSPI_D1),   .I(w_qspi_pad_io_o[1]), .T(~w_qspi_pad_io_oe[1]), .O(w_qspi_io_i[1]));
  IOBUF u_qspi_d2_iobuf   (.IO(QSPI_D2),   .I(w_qspi_pad_io_o[2]), .T(~w_qspi_pad_io_oe[2]), .O(w_qspi_io_i[2]));
  IOBUF u_qspi_d3_iobuf   (.IO(QSPI_D3),   .I(w_qspi_pad_io_o[3]), .T(~w_qspi_pad_io_oe[3]), .O(w_qspi_io_i[3]));

  // =========================================================================
  // CLCD data bus: 8 bidirectional pads. The block drives w_clcd_pd_o when
  // w_clcd_pd_oe=1 (writes) and samples w_clcd_pd_i on a CLCD_RD cycle. In the
  // default READ_PATH=0 build clcd_pd_oe is held 1 (write-only) and clcd_pd_i
  // is ignored (ip/clcd/clcd.sv:59-60). IOBUF is the same class of Xilinx I/O
  // primitive this top already instantiates directly (IBUF/BUFG, :100/:102).
  // Bit map: clcd_pd_o[0] -> pad CLCD_PD[10] ... clcd_pd_o[7] -> CLCD_PD[17]
  // (ip/clcd/README.md:86; nanosoc_mps3.xdc:495-502).
  // =========================================================================
  genvar gi;
  generate
    for (gi = 0; gi < 8; gi = gi + 1) begin : g_clcd_pd
      IOBUF u_clcd_pd_iobuf (
        .IO (CLCD_PD[10 + gi]),
        .I  (w_clcd_pd_o[gi]),
        .O  (w_clcd_pd_i[gi]),
        .T  (~w_clcd_pd_oe)        // oe=1 => drive => T=0 (Xilinx T is active-high Hi-Z)
      );
    end
  endgenerate

  // CLCD strobes/controls: the block already emits the correct active levels
  // (CS/WR/RD/RST active-low, RS/BL active-high — ip/clcd/README.md:90-95,
  // clcd.sv:99-104), so drive the pads straight through.
  assign CLCD_CS     = w_clcd_cs_n;
  assign CLCD_WR_SCL = w_clcd_wr_n;
  assign CLCD_RD     = w_clcd_rd_n;
  assign CLCD_RS     = w_clcd_rs;
  assign CLCD_BL     = w_clcd_bl;
  assign CLCD_RST    = w_clcd_rst_n;

  // =========================================================================
  // USER microSD pads (D13). usd_spi_0 gates every oe with EN && card-present,
  // so with no card all of these are high-Z (T=1) and the XDC pull-ups hold
  // them. Xilinx T is active-high Hi-Z, hence T = ~oe (board_gpio convention).
  // SCK is an OBUFT, not a plain OBUF: it floats with the rest.
  // DAT[1]/DAT[2] are unused in SPI mode: T=1 permanently, input unused.
  // =========================================================================
  OBUFT u_usd_clk_obuft  (.O(USD_CLK),     .I(w_usd_clk_o),  .T(~w_usd_clk_oe));
  IOBUF u_usd_cmd_iobuf  (.IO(USD_CMD),    .I(w_usd_cmd_o),  .T(~w_usd_cmd_oe),  .O());
  IOBUF u_usd_dat0_iobuf (.IO(USD_DAT[0]), .I(1'b0),         .T(1'b1),           .O(w_usd_dat0_i));
  IOBUF u_usd_dat1_iobuf (.IO(USD_DAT[1]), .I(1'b0),         .T(1'b1),           .O());
  IOBUF u_usd_dat2_iobuf (.IO(USD_DAT[2]), .I(1'b0),         .T(1'b1),           .O());
  IOBUF u_usd_dat3_iobuf (.IO(USD_DAT[3]), .I(w_usd_dat3_o), .T(~w_usd_dat3_oe), .O());

`ifdef MPS3_SHELL_TOUCH
  // Phase-2 touch I2C open-drain pads (UNBUILT DRAFT, gated): drive low, release
  // Hi-Z -> external/PULLUP high. Xilinx IOBUF T is active-high Hi-Z, so T = the
  // AXI IIC's scl_t/sda_t directly. CLCD_TINT is a plain input (IBUF inferred),
  // synchronized at u_shell above.
  IOBUF u_tscl_iobuf (.IO(CLCD_TSCL), .I(w_tscl_o), .T(w_tscl_t), .O(w_tscl_i));
  IOBUF u_tsda_iobuf (.IO(CLCD_TSDA), .I(w_tsda_o), .T(w_tsda_t), .O(w_tsda_i));
`endif

  // =========================================================================
  // board_gpio pad group -> physical LEDs + DIP switches ("LEDs/switches via
  // board_gpio" per the W-BD brief). Bit map (documented in
  // fpga/shell/README.md and for A3's board_gpio firmware):
  //   pads [7:0]  = USER_nLED[7:0]  — output-only board nets. LED lights
  //                 when the pad is OWNED+DRIVEN high (pad_o & pad_oe);
  //                 nLED is active-low, hence the inversion. Readback
  //                 (pad_i) loops the driven value, as an output-only pad
  //                 group has no independent input path.
  //   pads [15:8] = USER_SW[7:0]    — input-only board nets; pad_o/oe[15:8]
  //                 have no pad to drive and are intentionally unconnected.
  // =========================================================================
  wire [7:0] led_drive = board_gpio_pad_o[7:0] & board_gpio_pad_oe[7:0];
  assign USER_nLED = ~led_drive;
  assign board_gpio_pad_i = { USER_SW, led_drive };

  // =========================================================================
  // u_rp_dut — the reconfigurable-partition cell, SIBLING of u_shell
  // (contract's I4 resolution). Binds to fpga/shell/rp_dut_stub.sv in this
  // harness build (see header). DONT_TOUCH: keep the partition-pin boundary
  // intact through synth/opt so the written checkpoint stays black-box-able
  // for the DFX flow (fpga/dfx/build_dfx.tcl) — do not remove.
  // =========================================================================
  (* DONT_TOUCH = "TRUE" *)
  rp_dut u_rp_dut (
    .dut_clk          (w_dut_clk),
    .dut_resetn       (w_dut_resetn),
    .rp_resetn        (w_rp_resetn),
    .dbg_resetn       (w_dbg_resetn),

    .jtag_tck         (w_jtag_tck),
    .jtag_tms         (w_jtag_tms),
    .jtag_tdi         (w_jtag_tdi),
    .jtag_tdo         (w_jtag_tdo),

    // RM debug BSCAN group: an RM with ILAs carries a mode-1 debug_bridge hub on
    // these; every other RM ties dbg_bscan_tdo to 0 and ignores the inputs.
    .dbg_bscan_bscanid_en    (w_dbg_bscan_bscanid_en),
    .dbg_bscan_capture       (w_dbg_bscan_capture),
    .dbg_bscan_drck          (w_dbg_bscan_drck),
    .dbg_bscan_reset         (w_dbg_bscan_reset),
    .dbg_bscan_runtest       (w_dbg_bscan_runtest),
    .dbg_bscan_sel           (w_dbg_bscan_sel),
    .dbg_bscan_shift         (w_dbg_bscan_shift),
    .dbg_bscan_tck           (w_dbg_bscan_tck),
    .dbg_bscan_tdi           (w_dbg_bscan_tdi),
    .dbg_bscan_tms           (w_dbg_bscan_tms),
    .dbg_bscan_update        (w_dbg_bscan_update),
    .dbg_bscan_tdo           (w_dbg_bscan_tdo),

    .phy_rmii_ref_clk (w_phy_rmii_ref_clk),
    .phy_rmii_crs_dv  (w_phy_rmii_crs_dv),
    .phy_rmii_rxd     (w_phy_rmii_rxd),
    .phy_rmii_txd     (w_phy_rmii_txd),
    .phy_rmii_tx_en   (w_phy_rmii_tx_en),
    .mdc              (w_mdc),
    .mdio_o           (w_mdio_o),
    .mdio_oe          (w_mdio_oe),
    .mdio_i           (w_mdio_i),

    .uart_tx_tdata    (w_uart_tx_tdata),
    .uart_tx_tvalid   (w_uart_tx_tvalid),
    .uart_tx_tready   (w_uart_tx_tready),
    .uart_rx_tdata    (w_uart_rx_tdata),
    .uart_rx_tvalid   (w_uart_rx_tvalid),
    .uart_rx_tready   (w_uart_rx_tready),
    .swo              (w_swo),

    .rm_id            (w_rm_id),
    .dut_lockup       (w_dut_lockup),
    .irq_out          (w_irq_out),

    .dut_gpio_o       (w_dut_gpio_o),
    .dut_gpio_oe      (w_dut_gpio_oe),
    .dut_gpio_i       (w_dut_gpio_i),

    // Flash / QSPI XiP boundary (partition-pins.md v0.2). RP drives sclk/csn/
    // io_o/io_oe out to the shell (decoupler-clamped to the pads); io_i is the
    // pad sample driven back from shell_top's IOBUFs (non-CDC pass-through).
    .qspi_sclk        (w_qspi_sclk),
    .qspi_csn         (w_qspi_csn),
    .qspi_io_o        (w_qspi_io_o),
    .qspi_io_oe       (w_qspi_io_oe),
    .qspi_io_i        (w_qspi_io_i)
  );

`ifdef MPS3_SHELL_REALPHY
  // =========================================================================
  // Real external LAN8720 shield pads (SHELL_REALPHY, NON-DEFAULT).
  //
  // Datapath: the RP's RMII/MDIO group crosses the BD as w_phy_pad_* (the TX
  // group is decoupler-clamped INSIDE the BD, so a swap forces tx_en=0/mdc=0/
  // mdio_oe=0 -> the physical PHY sees no runaway frame / no junk MDIO). Board
  // pads are LVCMOS33 (fpga/shell/constraints_realphy/mps3_realphy_pins.xdc).
  //
  // Option A (FPGA sources REF_CLK): w_phy_pad_ref_clk is clk_wiz_shell/
  // clk_out2 (50 MHz) — the SAME net the RP + in-fabric virtual PHY use — driven
  // OUT to the LAN8720's REFCLK-in strap via an ODDRE1 clock-forward (the
  // UltraScale idiom for putting a clock on a pad cleanly, vs routing the clock
  // net straight through an OBUF).
  //
  // HDPR-29 IOB re-register (partition-pins.md "IOB packing note"): OLOGIC pad
  // registers are STATIC-only in a DFX design, so the RMII TX pad FFs must live
  // in the shell, not the RM (the RM's rmii_to_mii TX FFs cannot IOB-pack). The
  // RP already launched the TX group synchronous to this same clk_out2, so this
  // re-register is a matched, non-CDC restage (NOT a synchronizer) that lets the
  // FFs pack into the pad OLOGIC (IOB=TRUE, also asserted in the pins XDC).
  // =========================================================================
  wire w_pad_ref_clk_fwd;
  ODDRE1 #(.SRVAL(1'b0)) u_realphy_refclk_oddr (
    .Q  (w_pad_ref_clk_fwd),
    .C  (w_phy_pad_ref_clk),
    .D1 (1'b1),
    .D2 (1'b0),
    .SR (1'b0)
  );
  OBUF u_realphy_refclk_obuf (.I(w_pad_ref_clk_fwd), .O(ETH_RMII_REF_CLK));

  (* IOB = "TRUE" *) reg [1:0] r_phy_txd;
  (* IOB = "TRUE" *) reg       r_phy_tx_en;
  (* IOB = "TRUE" *) reg       r_phy_mdc;
  (* IOB = "TRUE" *) reg       r_phy_mdio_o;
  (* IOB = "TRUE" *) reg       r_phy_mdio_oe;
  always @(posedge w_phy_pad_ref_clk) begin
    r_phy_txd     <= w_phy_pad_txd;
    r_phy_tx_en   <= w_phy_pad_tx_en;
    r_phy_mdc     <= w_phy_pad_mdc;
    r_phy_mdio_o  <= w_phy_pad_mdio_o;
    r_phy_mdio_oe <= w_phy_pad_mdio_oe;
  end

  OBUF u_realphy_txd0_obuf (.I(r_phy_txd[0]), .O(ETH_RMII_TXD[0]));
  OBUF u_realphy_txd1_obuf (.I(r_phy_txd[1]), .O(ETH_RMII_TXD[1]));
  OBUF u_realphy_txen_obuf (.I(r_phy_tx_en),  .O(ETH_RMII_TX_EN));
  OBUF u_realphy_mdc_obuf  (.I(r_phy_mdc),    .O(ETH_MDC));

  // MDIO: drive r_phy_mdio_o when r_phy_mdio_oe=1 (Xilinx IOBUF T is active-high
  // Hi-Z, so T = ~oe), sample the pad back to the RP on w_phy_pad_mdio_i. Same
  // IOBUF idiom as the QSPI (:359) / CLCD (:378) bidir pads above.
  IOBUF u_realphy_mdio_iobuf (
    .IO (ETH_MDIO),
    .I  (r_phy_mdio_o),
    .T  (~r_phy_mdio_oe),
    .O  (w_phy_pad_mdio_i)
  );

  // RMII RX inputs -- HDPR-29's INPUT TWIN (added 2026-09-11).
  //
  // partition-pins.md's "IOB packing note" only ever spelled out the OUTPUT
  // case: OLOGIC pad sites are static-only in a DFX design, so the TX
  // re-register must live shell-side. ILOGIC pad sites are static-only for
  // EXACTLY the same reason, and the RX group was crossing as plain fabric
  // straight into the RP -- which put the whole pad -> pblock route inside the
  // 50 MHz source-synchronous capture window. That window has ~1.5 ns left in
  // it after the PHY's clock-to-out and the board round trip
  // (constraints_realphy/mps3_realphy_timing.xdc "THE BUDGET"), and a
  // pad-to-partition route across a KU115 does not fit in 1.5 ns.
  //
  // So: capture in the pad flop here, IOB=TRUE, on the SAME clk_out2 that is
  // forwarded out to the PHY. The external window then has to cover only an
  // ILOGIC setup, and the shell -> RP leg gets a full clean 20 ns cycle with no
  // external I/O delay charged against it.
  //
  // All THREE RX signals take the SAME one cycle, so the CRS_DV/RXD alignment
  // that rmii_to_mii's SFD nibble-align depends on is preserved exactly. The
  // cost is one REF_CLK cycle (20 ns) of RX latency, which Ethernet does not
  // care about. The RP's rmii_to_mii still owns its own input synchroniser
  // downstream; this stage is a matched pad re-register on the same clock, NOT
  // a synchroniser and NOT a CDC -- the exact mirror of the TX stage above.
  wire w_pad_crs_dv_i, w_pad_rxd0_i, w_pad_rxd1_i;
  IBUF u_realphy_crsdv_ibuf (.I(ETH_RMII_CRS_DV), .O(w_pad_crs_dv_i));
  IBUF u_realphy_rxd0_ibuf  (.I(ETH_RMII_RXD[0]), .O(w_pad_rxd0_i));
  IBUF u_realphy_rxd1_ibuf  (.I(ETH_RMII_RXD[1]), .O(w_pad_rxd1_i));

  (* IOB = "TRUE" *) reg       r_phy_crs_dv;
  (* IOB = "TRUE" *) reg [1:0] r_phy_rxd;
  always @(posedge w_phy_pad_ref_clk) begin
    r_phy_crs_dv <= w_pad_crs_dv_i;
    r_phy_rxd    <= {w_pad_rxd1_i, w_pad_rxd0_i};
  end

  assign w_phy_pad_crs_dv = r_phy_crs_dv;
  assign w_phy_pad_rxd    = r_phy_rxd;
`endif

endmodule
