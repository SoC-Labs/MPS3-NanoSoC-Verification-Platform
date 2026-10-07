// DEPRECATED 2026-09-23 (LINUX_HARNESS_PLAN_2026-09-23.md DL2): the July Linux
// fork is superseded by the ONE shell BD's CPU seam -- fpga/shell/bd/shell_bd.tcl +
// cpu_mbv.tcl (SHELL_CPU=mbv), fpga/shell/build_shell.tcl and validate_bd.tcl,
// fpga/shell/shell_top.sv (`ifdef MPS3_SHELL_CPU_MBV), fpga/shell/constraints/mbv/.
// Do not edit or build this file; it is deleted at landing (lead).
// -----------------------------------------------------------------------------
// shell_linux_top.sv — TRANSPLANT harness top level (MPS3 / XCKU115).
//
// Successor of the donor fpga/shell/shell_top.sv (main repo, branch
// feat/clcd-kvm-display) for the shell_linux_bd TRANSPLANT BD: the classic
// MicroBlaze coordinator replaced by MicroBlaze V + DDR4 (TRANSPLANT_CONTRACT
// §9, shell_linux_bd.tcl). The donor's u_top = u_shell + u_rp_dut sibling
// topology (I4), every IOBUF/IBUF/BUFG, the EMC/CLCD/QSPI/board_gpio pad
// glue, and the DONT_TOUCH'd rp_dut boundary are carried VERBATIM. Deltas
// vs the donor top, each per contract §9.3 [DEV-7] / the pin-XDC deviations:
//
//   [LINUX-DEV-1] console: BD uart_tx_f/uart_rx_f now bind to ports
//       LINUX_UART_TXD/RXD, pinned to FT4232 lane 2 (AD28/AE28) — the
//       silicon-proven Linux console lane (host if02). Donor lane-1 pads
//       AE30/AE31 are unused here.
//   [LINUX-DEV-2] USER_nLED[0] pad is driven by the BD's DDR4 calibration
//       indicator calib_complete_led_n (active-low pad, LED LIT = DDR4
//       calibrated — same bench meaning as the proven linux_soc build).
//       board_gpio LED bit0 keeps its readback loop but no longer reaches a
//       pad (virtual). LEDs [7:1] stay board_gpio-owned.
//   [DEV-7] new DDR4 ports: c0_sys_clk_p/n (OSC6 100 MHz diff) + the
//       c0_ddr4_* SODIMM interface (117 pads incl. sys clk, ddr4_pins.xdc,
//       SLR1 banks 49-51 — no RP-pblock collision).
//   [DEV-10] DUT debug is JTAG (2026-09-11): the swd_* partition-pin group is
//       gone; u_rp_dut and u_shell now carry jtag_tck/tms/tdi/tdo, matching
//       fpga/shell/boundary.yaml and the generated fpga/shell/rp_dut_stub.sv
//       that build_transplant_phaseB.tcl compiles into this top. AT THIS LEVEL
//       it really is only a rename — the debug group never reaches a pad in
//       this design (the bit-bang CSR is shell-internal), so there is no
//       tristate/IOBUF glue to unwind, unlike QSPI or CLCD. The DESIGN change
//       lives in shell_linux_bd.tcl (swd_bb -> jtag_bb, a different CSR
//       contract) and in the Linux daemon estate. See
//       docs/planning/LINUX_FORK_JTAG_MIGRATION.md.
//
// The donor shell_top.sv / main repo are NOT touched by this file.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module shell_linux_top (
  // -- Clock / reset (donor pins, HW-proven) --
  input  wire        OSCCLK1,        // board oscillator, 50 MHz
  input  wire        USER_nPB0,      // push-button, active-low — system POR
  input  wire        USER_nPB1,      // push-button, active-low — CLCD-KVM toggle

  // -- LEDs / DIP switches --
  output wire [7:0]  USER_nLED,      // active-low LEDs ([0] = DDR4 calib, [LINUX-DEV-2])
  input  wire [7:0]  USER_SW,        // DIP switches

  // -- LAN9220 static-memory-bus (SMC) host interface (donor, verbatim) --
  output wire        ETH_nCS,
  output wire        ETH_nOE,
  input  wire        ETH_INT,
  output wire [6:0]  SMBF_ADDR,
  inout  wire [15:0] SMBF_DATA,
  output wire        SMBF_FIFOSEL,
  output wire        SMBF_nOE,
  output wire        SMBF_nWE,
  output wire        SMBF_nRST,

  // -- Linux console (FT4232 lane 2 = host if02, silicon-proven) [LINUX-DEV-1]
  output wire        LINUX_UART_TXD,
  input  wire        LINUX_UART_RXD,

  // -- External QSPI flash (SST26VF064B), RP-owned XiP path (donor, verbatim)
  inout  wire        QSPI_D0,
  inout  wire        QSPI_D1,
  inout  wire        QSPI_D2,
  inout  wire        QSPI_D3,
  inout  wire        QSPI_SCLK,
  inout  wire        QSPI_nCS,

  // -- On-board QVGA CLCD (HX8347-D, 8080) (donor, verbatim) --
  inout  wire [17:10] CLCD_PD,
  output wire         CLCD_RD,
  output wire         CLCD_RS,
  output wire         CLCD_CS,
  output wire         CLCD_WR_SCL,
  output wire         CLCD_BL,
  output wire         CLCD_RST,

  // -- DDR4 SODIMM (MIG) — [DEV-7], pins per ddr4_pins.xdc (proven set) --
  input  wire         c0_sys_clk_p,
  input  wire         c0_sys_clk_n,
  output wire         c0_ddr4_act_n,
  output wire [16:0]  c0_ddr4_adr,
  output wire [1:0]   c0_ddr4_ba,
  output wire [0:0]   c0_ddr4_bg,
  output wire [0:0]   c0_ddr4_ck_c,
  output wire [0:0]   c0_ddr4_ck_t,
  output wire [0:0]   c0_ddr4_cke,
  output wire [0:0]   c0_ddr4_cs_n,
  inout  wire [7:0]   c0_ddr4_dm_n,
  inout  wire [63:0]  c0_ddr4_dq,
  inout  wire [7:0]   c0_ddr4_dqs_c,
  inout  wire [7:0]   c0_ddr4_dqs_t,
  output wire [0:0]   c0_ddr4_odt,
  output wire         c0_ddr4_reset_n
);

  // =========================================================================
  // Board clock / reset conditioning (donor pattern: this top owns the input
  // buffer; both BD clk_wiz are PRIM_SOURCE No_buffer).
  // =========================================================================
  wire osc_clk_ibuf;
  IBUF  u_osc_ibuf (.I(OSCCLK1), .O(osc_clk_ibuf));
  wire osc_clk_50m;
  BUFG  u_osc_bufg (.I(osc_clk_ibuf), .O(osc_clk_50m));

  wire sys_rst_n = USER_nPB0;  // active-low; BD does async-assert/sync-deassert
                               // internally (proc_sys_reset_* + dut_clkrst_0 +
                               // the MIG sys_rst inversion carried in the BD).

  // =========================================================================
  // RP partition-pin nets (partition-pins.md v0.2, EXACT names — donor,
  // verbatim).
  // =========================================================================
  wire        w_dut_clk, w_dut_resetn, w_rp_resetn, w_dbg_resetn;
  wire        w_jtag_tck, w_jtag_tms, w_jtag_tdi, w_jtag_tdo;   // [DEV-10]
  wire        w_phy_rmii_ref_clk, w_phy_rmii_crs_dv;
  wire [1:0]  w_phy_rmii_rxd, w_phy_rmii_txd;
  wire        w_phy_rmii_tx_en, w_mdc, w_mdio_o, w_mdio_oe, w_mdio_i;
  wire [7:0]  w_uart_tx_tdata, w_uart_rx_tdata;
  wire        w_uart_tx_tvalid, w_uart_tx_tready;
  wire        w_uart_rx_tvalid, w_uart_rx_tready, w_swo;
  wire [31:0] w_rm_id;
  wire        w_dut_lockup, w_irq_out;
  wire [15:0] w_dut_gpio_o, w_dut_gpio_oe, w_dut_gpio_i;
  wire        w_qspi_sclk, w_qspi_csn;
  wire [3:0]  w_qspi_io_o, w_qspi_io_oe, w_qspi_io_i;
  wire        w_qspi_pad_sclk, w_qspi_pad_csn;
  wire [3:0]  w_qspi_pad_io_o, w_qspi_pad_io_oe;

  // =========================================================================
  // EMC (LAN9220) glue nets (donor, verbatim — see donor shell_top.sv for the
  // byte/word address mapping rationale + BYTE_TEST bring-up flag).
  // =========================================================================
  wire [31:0] emc_addr;
  wire        emc_ce_n, emc_oen, emc_wen;

  assign ETH_nCS      = emc_ce_n;
  assign ETH_nOE      = emc_oen;
  assign SMBF_nOE     = emc_oen;
  assign SMBF_nWE     = emc_wen;
  assign SMBF_ADDR    = emc_addr[7:1];
  assign SMBF_FIFOSEL = 1'b0;
  assign SMBF_nRST    = sys_rst_n;

  // =========================================================================
  // u_shell — the TRANSPLANT BD wrapper (shell_linux_bd via make_wrapper).
  // =========================================================================
  wire [15:0] board_gpio_pad_o, board_gpio_pad_oe, board_gpio_pad_i;

  wire [7:0] w_clcd_pd_o, w_clcd_pd_i;
  wire       w_clcd_pd_oe;
  wire       w_clcd_cs_n, w_clcd_wr_n, w_clcd_rd_n, w_clcd_rs, w_clcd_bl, w_clcd_rst_n;

  wire       w_calib_led_n;  // [LINUX-DEV-2] active-low: 0 == DDR4 calibrated

  shell_linux_bd_wrapper u_shell (
    .osc_clk_50m (osc_clk_50m),
    .sys_rst_n   (sys_rst_n),

    // -- LAN9220 host I/F (AXI EMC external interface) --
    .EMC_INTF_addr   (emc_addr),
    .EMC_INTF_ce_n   (emc_ce_n),
    .EMC_INTF_oen    (emc_oen),
    .EMC_INTF_wen    (emc_wen),
    .EMC_INTF_dq_io  (SMBF_DATA),   // wrapper-internal IOBUFs drive the pad
    .EMC_INTF_wait   (1'b0),        // LAN9220 SMC has no wait/ready pin
    // EMC_INTF_{adv_ldn,ben,ce,clken,cre,lbon,qwen,rnw,rpn}: SRAM-mode
    // unused outputs, intentionally unconnected.
    // INVERTED on purpose -- see the measurement below.  The LAN9220 keeps its
    // NATIVE open-drain, ACTIVE-LOW IRQ output (we no longer ask the driver for
    // IRQ_POL=1/push-pull); ETH_INT idles HIGH via the pad PULLUP and is pulled
    // LOW to assert.  The axi_intc input is ACTIVE-HIGH/LEVEL, so it needs the
    // inverse.  MEASURED ON SILICON 2026-07-19 (masked-IRQ devmem experiment):
    //   INT_CFG=0x111 (IRQ_EN|IRQ_POL=1|push-pull) => INT_STS=0 and INT_EN=0
    //   (NOTHING pending, no source even enabled) yet INTC ISR bit3 = ASSERTED.
    //   i.e. with IRQ_POL=1 this part drives the pin to the asserted level with
    //   nothing pending => request_irq() storms and silently livelocks the CPU
    //   (no printk can escape an IRQ storm on an interrupt-driven console).
    // This inversion MUST ship together with (a) PULLTYPE PULLUP on ETH_INT and
    // (b) dropping smsc,irq-active-high + smsc,irq-push-pull from shell_linux.dts.
    // Any two of the three alone reproduce the livelock.
    .eth_irq         (~ETH_INT),

    // -- Linux console (BD ports uart_*_f; pads = FT4232 lane 2) --
    .uart_tx_f   (LINUX_UART_TXD),
    .uart_rx_f   (LINUX_UART_RXD),

    // -- External QSPI flash boundary (QSPI freeze v0.2, donor verbatim) --
    .rp_qspi_sclk   (w_qspi_sclk),
    .rp_qspi_csn    (w_qspi_csn),
    .rp_qspi_io_o   (w_qspi_io_o),
    .rp_qspi_io_oe  (w_qspi_io_oe),
    .qspi_pad_sclk  (w_qspi_pad_sclk),
    .qspi_pad_csn   (w_qspi_pad_csn),
    .qspi_pad_io_o  (w_qspi_pad_io_o),
    .qspi_pad_io_oe (w_qspi_pad_io_oe),

    // -- Board GPIO pad group --
    .board_gpio_pad_o  (board_gpio_pad_o),
    .board_gpio_pad_oe (board_gpio_pad_oe),
    .board_gpio_pad_i  (board_gpio_pad_i),

    // -- CLCD 8080 bus master (donor verbatim) --
    .clcd_pd_o   (w_clcd_pd_o),
    .clcd_pd_i   (w_clcd_pd_i),
    .clcd_pd_oe  (w_clcd_pd_oe),
    .clcd_cs_n   (w_clcd_cs_n),
    .clcd_wr_n   (w_clcd_wr_n),
    .clcd_rd_n   (w_clcd_rd_n),
    .clcd_rs     (w_clcd_rs),
    .clcd_bl     (w_clcd_bl),
    .clcd_rst_n  (w_clcd_rst_n),

    // -- CLCD-KVM ownership button --
    .user_npb1   (USER_nPB1),

    // -- TELEM I2C seam (no MPS3 pin exists; donor tie-off carried) --
    .i2c_sda_i (1'b1),
    // i2c_scl_o / i2c_scl_t / i2c_sda_o / i2c_sda_t: intentionally unconnected.

    // -- DDR4 [DEV-7]: straight pass-through to the SODIMM pads --
    .c0_sys_clk_p    (c0_sys_clk_p),
    .c0_sys_clk_n    (c0_sys_clk_n),
    .c0_ddr4_act_n   (c0_ddr4_act_n),
    .c0_ddr4_adr     (c0_ddr4_adr),
    .c0_ddr4_ba      (c0_ddr4_ba),
    .c0_ddr4_bg      (c0_ddr4_bg),
    .c0_ddr4_ck_c    (c0_ddr4_ck_c),
    .c0_ddr4_ck_t    (c0_ddr4_ck_t),
    .c0_ddr4_cke     (c0_ddr4_cke),
    .c0_ddr4_cs_n    (c0_ddr4_cs_n),
    .c0_ddr4_dm_n    (c0_ddr4_dm_n),
    .c0_ddr4_dq      (c0_ddr4_dq),
    .c0_ddr4_dqs_c   (c0_ddr4_dqs_c),
    .c0_ddr4_dqs_t   (c0_ddr4_dqs_t),
    .c0_ddr4_odt     (c0_ddr4_odt),
    .c0_ddr4_reset_n (c0_ddr4_reset_n),
    .calib_complete_led_n (w_calib_led_n),

    // -- RP partition-pin boundary (donor verbatim) --
    .rp_dut_clk       (w_dut_clk),
    .rp_dut_resetn    (w_dut_resetn),
    .rp_rp_resetn     (w_rp_resetn),
    .rp_dbg_resetn    (w_dbg_resetn),

    .rp_jtag_tck      (w_jtag_tck),
    .rp_jtag_tms      (w_jtag_tms),
    .rp_jtag_tdi      (w_jtag_tdi),
    .rp_jtag_tdo      (w_jtag_tdo),

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
  );

  // =========================================================================
  // External QSPI flash pads (donor, verbatim — RP-owned XiP path).
  // =========================================================================
  IOBUF u_qspi_sclk_iobuf (.IO(QSPI_SCLK), .I(w_qspi_pad_sclk), .T(1'b0),                 .O());
  IOBUF u_qspi_ncs_iobuf  (.IO(QSPI_nCS),  .I(w_qspi_pad_csn),  .T(1'b0),                 .O());
  IOBUF u_qspi_d0_iobuf   (.IO(QSPI_D0),   .I(w_qspi_pad_io_o[0]), .T(~w_qspi_pad_io_oe[0]), .O(w_qspi_io_i[0]));
  IOBUF u_qspi_d1_iobuf   (.IO(QSPI_D1),   .I(w_qspi_pad_io_o[1]), .T(~w_qspi_pad_io_oe[1]), .O(w_qspi_io_i[1]));
  IOBUF u_qspi_d2_iobuf   (.IO(QSPI_D2),   .I(w_qspi_pad_io_o[2]), .T(~w_qspi_pad_io_oe[2]), .O(w_qspi_io_i[2]));
  IOBUF u_qspi_d3_iobuf   (.IO(QSPI_D3),   .I(w_qspi_pad_io_o[3]), .T(~w_qspi_pad_io_oe[3]), .O(w_qspi_io_i[3]));

  // =========================================================================
  // CLCD data bus + strobes (donor, verbatim).
  // =========================================================================
  genvar gi;
  generate
    for (gi = 0; gi < 8; gi = gi + 1) begin : g_clcd_pd
      IOBUF u_clcd_pd_iobuf (
        .IO (CLCD_PD[10 + gi]),
        .I  (w_clcd_pd_o[gi]),
        .O  (w_clcd_pd_i[gi]),
        .T  (~w_clcd_pd_oe)
      );
    end
  endgenerate

  assign CLCD_CS     = w_clcd_cs_n;
  assign CLCD_WR_SCL = w_clcd_wr_n;
  assign CLCD_RD     = w_clcd_rd_n;
  assign CLCD_RS     = w_clcd_rs;
  assign CLCD_BL     = w_clcd_bl;
  assign CLCD_RST    = w_clcd_rst_n;

  // =========================================================================
  // board_gpio pad group -> LEDs + DIP switches.
  // [LINUX-DEV-2]: USER_nLED[0] pad is the DDR4 calibration indicator
  // (w_calib_led_n is already active-low: LED LIT = calibrated — the
  // linux_soc bench convention). board_gpio's LED bit0 keeps its readback
  // loop (firmware sees what it drove) but no longer reaches the pad.
  // LEDs [7:1] and all switches stay exactly as the donor wired them.
  // =========================================================================
  wire [7:0] led_drive = board_gpio_pad_o[7:0] & board_gpio_pad_oe[7:0];
  assign USER_nLED[0]   = w_calib_led_n;          // [LINUX-DEV-2]
  assign USER_nLED[7:1] = ~led_drive[7:1];
  assign board_gpio_pad_i = { USER_SW, led_drive };

  // =========================================================================
  // u_rp_dut — the RP cell, SIBLING of u_shell (I4; donor verbatim).
  // DONT_TOUCH keeps the partition boundary intact through synth/opt so the
  // post-synth checkpoint stays black-box-able for the DFX flow.
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

    .qspi_sclk        (w_qspi_sclk),
    .qspi_csn         (w_qspi_csn),
    .qspi_io_o        (w_qspi_io_o),
    .qspi_io_oe       (w_qspi_io_oe),
    .qspi_io_i        (w_qspi_io_i)
  );

endmodule
