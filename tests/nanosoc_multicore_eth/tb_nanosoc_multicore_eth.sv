//-----------------------------------------------------------------------------
// tb_nanosoc_multicore_eth.sv — LAYER-B integrated ethernet bench top.
//
// A joint work commissioned on behalf of SoC Labs, under Arm Academic Access
// license.  Copyright (C) 2026, SoC Labs (www.soclabs.org)
//-----------------------------------------------------------------------------
// WHAT THIS CLOSES
//   Every piece of the ethernet path is verified separately:
//     * tests/rmii_conformance   — rmii_phy_if wire conformance (RMII di-bits)
//     * tests/eth_mac_subsystem  — shell rmii_phy_if + link_partner_mac + bridge,
//                                  but with the bench PLAYING the DUT MAC
//     * cocotb/soc_ethernet      — the REAL SoC MAC, but against a stand-alone
//                                  tb_rmii_phy_model, NOT the shell's virtual PHY
//   The one un-stitched gap: the REAL multicore-ethernet nanoSoC RM (the DFX
//   Reconfigurable Module `rp_nanosoc_multicore_wrapper`) driving its ethernet
//   + console pins ACROSS the DFX partition boundary into the SHELL's virtual
//   PHY (rmii_phy_if + mdio_phy_model) and link_partner_mac.  This bench wires
//   exactly that and proves RX / TX / MDIO / console cross the boundary.
//
// THE "MODELLED DFX BOUNDARY"
//   The 30 partition pins (docs/contracts/partition-pins.md) are modelled here
//   as the explicit `b_*` net set below: plain fabric wires, exactly as the DFX
//   partition pins are (the contract carries no logic across the boundary — the
//   RMII pad re-register stage lives SHELL-SIDE inside rmii_phy_if.sv, per the
//   wrapper's HDPR-29 note).  So a byte only reaches the DUT MAC by physically
//   traversing rmii_phy_if -> b_phy_rmii_* -> the RM's RMII port, and vice-versa.
//
// HOW THE DUT MAC IS DRIVEN (no full firmware needed)
//   The RM wrapper ties its eth_ss_0 external AHB test-slave port off (it is an
//   internal debug port, not a partition pin).  cocotb reaches it hierarchically
//   (dut.u_rm.u_soc.eth_ss_0_*) with a cocotbext-ahb master — the SAME MAC
//   bring-up soc_ethernet uses — which deposits over the wrapper's constant ties
//   (VCS keeps the deposit; the constant port-tie re-evaluates only at t0).  A
//   real hello_uart image is preloaded into CPU0 IMEM so the console (uart_tx
//   AXIS across the boundary via the RM's uart_axis_shim) carries real bytes,
//   exactly as soc_ethernet co-runs hello_uart alongside the eth_ss_0 AHB drive.
//-----------------------------------------------------------------------------

`timescale 1ns/1ps

module tb_nanosoc_multicore_eth;

  // ==========================================================================
  // Clocks
  //   dut_clk        : the DUT system clock (-> RM sys_fclk / sys_hclk / rtc).
  //                    50 MHz to match the RM wrapper's UART_CLK_HZ default so
  //                    the uart_axis_shim baud divider tracks the delivered
  //                    image (see the wrapper header's baud note).
  //   phy_ref_clk    : the shell virtual-PHY's 50 MHz RMII reference — a REAL
  //                    second clock domain (the eth MAC's MII side derives from
  //                    it), started anti-phase to dut_clk so the MAC's WB<->MII
  //                    CDC is not a same-edge shortcut.
  //   s_axi_vphy_aclk: mdio_phy_model's AXI-Lite control clock (100 MHz).  We
  //                    issue no AXI writes, so its reset defaults hold (link up,
  //                    PHY_ID = 0x0007_C0F1), but it must run for the model's
  //                    AXI<->mdc synchronisers.
  // ==========================================================================
  localparam realtime DUT_CLK_HALF = 10.0;   // 50 MHz
  localparam realtime REF_CLK_HALF = 10.0;   // 50 MHz (RMII mandated)
  localparam realtime VPHY_CLK_HALF = 5.0;   // 100 MHz

  reg dut_clk = 1'b0;
  always #(DUT_CLK_HALF) dut_clk = ~dut_clk;

  reg phy_ref_clk = 1'b1;                    // anti-phase to dut_clk at t0
  always #(REF_CLK_HALF) phy_ref_clk = ~phy_ref_clk;

  reg s_axi_vphy_aclk = 1'b0;
  always #(VPHY_CLK_HALF) s_axi_vphy_aclk = ~s_axi_vphy_aclk;

  // ==========================================================================
  // Resets (all active-low at the RM boundary; active-high for the shell models)
  // ==========================================================================
  reg dut_resetn = 1'b0;
  reg rp_resetn  = 1'b0;
  reg dbg_resetn = 1'b0;
  reg phy_rst    = 1'b1;   // shell models: active-high reset
  reg vphy_aresetn = 1'b0; // mdio_phy_model AXI reset (active-low)

  initial begin
    // Hold everything in reset, then release together.
    repeat (20) @(posedge dut_clk);
    dut_resetn   = 1'b1;
    rp_resetn    = 1'b1;
    dbg_resetn   = 1'b1;
    phy_rst      = 1'b0;
    vphy_aresetn = 1'b1;
  end

  // ==========================================================================
  // The modelled DFX partition boundary — RMII + MDIO group (plain fabric nets)
  // ==========================================================================
  wire        b_phy_rmii_ref_clk;   // shell -> RM  (virtual PHY sources REF_CLK)
  wire        b_phy_rmii_crs_dv;    // shell -> RM  (PHY -> DUT RX carrier/data-valid)
  wire [1:0]  b_phy_rmii_rxd;       // shell -> RM  (PHY -> DUT RX di-bits)
  wire [1:0]  b_phy_rmii_txd;       // RM -> shell  (DUT MAC TX di-bits)
  wire        b_phy_rmii_tx_en;     // RM -> shell  (DUT MAC TX enable)
  wire        b_mdc;                // RM -> shell  (DUT is MDIO master)
  wire        b_mdio_o;             // RM -> shell  (DUT MDIO drive value)
  wire        b_mdio_oe;            // RM -> shell  (DUT MDIO output-enable)
  wire        b_mdio_i;             // shell -> RM  (virtual-PHY reg-model reply)

  // ==========================================================================
  // Console / trace AXI-Stream boundary (from the RM's internal uart_axis_shim)
  //   uart_tx_* : DUT CPU0 -> host console bytes (cocotb monitors these).
  //   uart_rx_* : host -> DUT (idle here; no host->DUT console traffic).
  // ==========================================================================
  wire [7:0]  uart_tx_tdata;
  wire        uart_tx_tvalid;
  wire        uart_tx_tready;   // driven by cocotb AxisByteMonitor (=1)
  wire        uart_rx_tready;   // RM output, unused

  // ==========================================================================
  // Status boundary
  // ==========================================================================
  wire [31:0] rm_id;
  wire        dut_lockup;
  wire        irq_out;          // = the SoC eth_irq (MAC interrupt) across boundary
  wire [15:0] dut_gpio_o;
  wire [15:0] dut_gpio_oe;

  // ==========================================================================
  // 1. The Reconfigurable Module — the REAL multicore-ethernet nanoSoC RM.
  //    ETH_IMEM preloaded with the converted hello_uart word image so CPU0
  //    boots and drives the console; UART_CLK_HZ/BAUD left at the wrapper
  //    defaults (50 MHz / 76800) which the 50 MHz dut_clk honours.
  // ==========================================================================
  rp_nanosoc_multicore_wrapper #(
    .ETH_IMEM_MEM_FPGA_IMG ("sim_build/image.hex")
  ) u_rm (
    .dut_clk          (dut_clk),
    .dut_resetn       (dut_resetn),
    .rp_resetn        (rp_resetn),
    .dbg_resetn       (dbg_resetn),

    // Internal SWD — idle (no debugger in this bench)
    .swd_clk          (1'b0),
    .swd_dio_o        (1'b1),
    .swd_dio_oe       (1'b0),
    .swd_dio_i        (),

    // Ethernet RMII + MDIO — across the modelled boundary to the shell PHY
    .phy_rmii_ref_clk (b_phy_rmii_ref_clk),
    .phy_rmii_crs_dv  (b_phy_rmii_crs_dv),
    .phy_rmii_rxd     (b_phy_rmii_rxd),
    .phy_rmii_txd     (b_phy_rmii_txd),
    .phy_rmii_tx_en   (b_phy_rmii_tx_en),
    .mdc              (b_mdc),
    .mdio_o           (b_mdio_o),
    .mdio_oe          (b_mdio_oe),
    .mdio_i           (b_mdio_i),

    // Console AXIS
    .uart_tx_tdata    (uart_tx_tdata),
    .uart_tx_tvalid   (uart_tx_tvalid),
    .uart_tx_tready   (uart_tx_tready),
    .uart_rx_tdata    (8'h00),
    .uart_rx_tvalid   (1'b0),
    .uart_rx_tready   (uart_rx_tready),
    .swo              (),

    // Status / misc
    .rm_id            (rm_id),
    .dut_lockup       (dut_lockup),
    .irq_out          (irq_out),
    .dut_gpio_o       (dut_gpio_o),
    .dut_gpio_oe      (dut_gpio_oe),
    .dut_gpio_i       (16'h0000)
  );

  // ==========================================================================
  // 2. Shell virtual PHY — rmii_phy_if.  RMII side faces the RM across the
  //    boundary; MII side faces link_partner_mac.  It also SOURCES the RMII
  //    reference clock the RM consumes (phy_rmii_ref_clk_o -> b_phy_rmii_ref_clk).
  // ==========================================================================
  wire [3:0] mii_rxd;      // rmii_phy_if -> link_partner (DUT-TX decoded)
  wire       mii_rx_dv;
  wire       mii_rx_er;
  wire       mii_rx_clk;
  wire [3:0] mii_txd;      // link_partner -> rmii_phy_if (toward DUT RX)
  wire       mii_tx_en;
  wire       mii_tx_clk;

  rmii_phy_if u_rmii_phy_if (
    .refclk_i            (phy_ref_clk),
    .rst_i               (phy_rst),

    // RMII side — to/from the RM across the boundary
    .phy_rmii_ref_clk_o  (b_phy_rmii_ref_clk),
    .phy_rmii_crs_dv_o   (b_phy_rmii_crs_dv),
    .phy_rmii_rxd_o      (b_phy_rmii_rxd),
    .phy_rmii_txd_i      (b_phy_rmii_txd),
    .phy_rmii_tx_en_i    (b_phy_rmii_tx_en),

    // MII side — to/from link_partner_mac
    .mii_rxd_o           (mii_rxd),
    .mii_rx_dv_o         (mii_rx_dv),
    .mii_rx_er_o         (mii_rx_er),
    .mii_rx_clk_o        (mii_rx_clk),
    .mii_txd_i           (mii_txd),
    .mii_tx_en_i         (mii_tx_en),
    .mii_tx_clk_o        (mii_tx_clk)
  );

  // ==========================================================================
  // 3. Link partner MAC — MII <-> AXI-Stream byte frames.
  //    m_axis_rx : frames the DUT MAC transmitted (recovered).  cocotb monitors.
  //    s_axis_tx : frames to inject toward the DUT MAC.        cocotb drives.
  // ==========================================================================
  wire [7:0] lpm_rx_tdata;   // DUT-TX recovered  (monitor)
  wire       lpm_rx_tvalid;
  wire       lpm_rx_tready;   // driven by cocotb (=1)
  wire       lpm_rx_tlast;
  wire       lpm_rx_tuser;    // frame-error flag

  wire [7:0] lpm_tx_tdata;    // inject toward DUT (driver)
  wire       lpm_tx_tvalid;
  wire       lpm_tx_tready;
  wire       lpm_tx_tlast;

  link_partner_mac u_link_partner_mac (
    .clk_i            (phy_ref_clk),
    .rst_i            (phy_rst),

    // MII side — to/from rmii_phy_if
    .mii_rxd_i        (mii_rxd),
    .mii_rx_dv_i      (mii_rx_dv),
    .mii_rx_er_i      (mii_rx_er),
    .mii_rx_clk_i     (mii_rx_clk),
    .mii_txd_o        (mii_txd),
    .mii_tx_en_o      (mii_tx_en),
    .mii_tx_clk_i     (mii_tx_clk),

    // AXIS RX (DUT TX -> here)
    .m_axis_rx_tdata  (lpm_rx_tdata),
    .m_axis_rx_tvalid (lpm_rx_tvalid),
    .m_axis_rx_tready (lpm_rx_tready),
    .m_axis_rx_tlast  (lpm_rx_tlast),
    .m_axis_rx_tuser  (lpm_rx_tuser),

    // AXIS TX (here -> DUT RX)
    .s_axis_tx_tdata  (lpm_tx_tdata),
    .s_axis_tx_tvalid (lpm_tx_tvalid),
    .s_axis_tx_tready (lpm_tx_tready),
    .s_axis_tx_tlast  (lpm_tx_tlast)
  );

  // ==========================================================================
  // 4. Shell MDIO virtual PHY register model.  mdc/mdio cross the boundary from
  //    the RM (the DUT MAC is the MDIO master); the AXI-Lite host surface is
  //    held idle so the reset defaults (link up, PHY_ID 0x0007_C0F1 at PHY
  //    address 1) answer the DUT's reads.
  // ==========================================================================
  mdio_phy_model u_mdio_phy_model (
    .s_axi_aclk     (s_axi_vphy_aclk),
    .s_axi_aresetn  (vphy_aresetn),
    .s_axi_awaddr   (12'h0),
    .s_axi_awprot   (3'h0),
    .s_axi_awvalid  (1'b0),
    .s_axi_awready  (),
    .s_axi_wdata    (32'h0),
    .s_axi_wstrb    (4'h0),
    .s_axi_wvalid   (1'b0),
    .s_axi_wready   (),
    .s_axi_bresp    (),
    .s_axi_bvalid   (),
    .s_axi_bready   (1'b0),
    .s_axi_araddr   (12'h0),
    .s_axi_arprot   (3'h0),
    .s_axi_arvalid  (1'b0),
    .s_axi_arready  (),
    .s_axi_rdata    (),
    .s_axi_rresp    (),
    .s_axi_rvalid   (),
    .s_axi_rready   (1'b0),

    // MDIO — across the boundary from the RM
    .mdc_i          (b_mdc),
    .mdio_o_i       (b_mdio_o),
    .mdio_oe_i      (b_mdio_oe),
    .mdio_i_o       (b_mdio_i)
  );

  // ==========================================================================
  // Safety backstop — a hard $finish so a wedged CPU boot or a zero-delay comb
  // loop cannot hang the run past any sane budget (the cocotb tests apply their
  // own, far tighter, per-test timeouts).  Also a coarse heartbeat so a hung
  // run is visibly distinguishable from a slow one in the log.
  // ==========================================================================
  initial begin : watchdog
    #20_000_000;   // 20 ms sim time
    $display("[%0t] TB WATCHDOG: hard $finish backstop reached", $time);
    $finish;
  end

  integer hb;
  initial begin : heartbeat
    hb = 0;
    forever begin
      #500_000;    // every 500 us
      hb = hb + 1;
      $display("[%0t] TB heartbeat %0d (irq_out=%0b rm_id=0x%08x)",
               $time, hb, irq_out, rm_id);
    end
  end

`ifdef WAVES
  initial begin
    $dumpfile("waves.vcd");
    $dumpvars(0, tb_nanosoc_multicore_eth);
  end
`endif

endmodule
