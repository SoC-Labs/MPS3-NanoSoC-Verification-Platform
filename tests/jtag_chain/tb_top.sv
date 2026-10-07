//-----------------------------------------------------------------------------
// tb_top.sv — jtag_chain: TWO TAPs on ONE 4-wire partition wire-set.
//
// A joint work commissioned on behalf of SoC Labs, under Arm Academic Access license.
// Copyright (C) 2026, SoC Labs (www.soclabs.org)
//-----------------------------------------------------------------------------
// WHAT IS UNDER TEST
// ==================
// fpga/rp/nanosoc_iice/rp_nanosoc_iice_shim.sv — the REAL file, compiled from
// the RM directory, not a copy. Its job is one thing: splice the Identify soft
// TAP into the SAME jtag_tck/tms/tdi/tdo the DUT's CoreSight SoC-400 SWJ-DP is
// on, as an IEEE 1149.1 daisy chain, and decide the order.
//
// Everything else here exists to give that splice something real to splice.
// tests/jtag_chain/iice_core_stub.sv presents the 39-port instrumented-EDIF
// boundary and holds the two TAPs: the REAL SWJ-DP + AHB-AP + Cortex-M0 on the
// jtag_* legs, and a behavioural model of Identify's soft TAP on the
// identify_jtag_* legs. Read that file's header for what is real and what is a
// model; the distinction is load-bearing.
//
//   cocotb remote_bitbang driver
//        |  jtag_tck / jtag_tms / jtag_tdi / jtag_tdo   (the 4 partition pins)
//        v
//   rp_nanosoc_iice_shim              <-- THE DESIGN UNDER TEST
//        |  chain splice (6 assigns, order set by IICE_TAP_NEAREST_TDI)
//        v
//   rp_nanosoc_iice_core  [bench stand-in for the instrumented EDIF cell]
//        +-- iice_soft_tap_model      IR 5, IDCODE 0x1063E4CD   (MODEL)
//        +-- nanosoc_swj_dap_ss       IR 4, IDCODE 0x6BA00477   (REAL Arm RTL)
//              -> cxdapahbap -> nanosoc_dbg_ahb_bridge -> slcorem0 -> Cortex-M0
//
// vs tests/jtag_dap_bringup: that bench proves the SWJ-DP serial front end with
// ONE TAP on the wires. This one adds a second TAP in front of it and proves
// the DAP is still reachable through the extra 1 bit of BYPASS and 5 bits of
// IR padding — and that Identify's TAP is reachable the other way round.
//
// NO TRST PIN. Neither TAP has one reaching the boundary: the RM straps
// dap_ntrst high (rp_nanosoc_wrapper.sv:526) and Identify's soft TAP has only
// four pins. So Test-Logic-Reset is five TCK with TMS high and nothing else —
// which is also why the DUT-reset hazard below matters.
//
// THE DUT-RESET HAZARD IS DELIBERATELY REACHABLE FROM HERE. The RM ties
// dap_npotrst to the DUT's system reset, so pulsing dut_resetn resets the
// SWJ-DP's TAP controller to Test-Logic-Reset, where 1149.1 reloads IDCODE and
// that TAP's DR width jumps from 1 (BYPASS) to 32 under the host's feet.
// test_dut_reset_reloads_dap_idcode exercises exactly that.
//-----------------------------------------------------------------------------
`timescale 1ns/1ps

module tb_top;

    parameter FCLK_PERIOD_NS = 10;   // 100 MHz dut_clk

    // =========================================================================
    // Chain order, as a TB parameter so the RTL-side control can be built
    // without editing a file:
    //   1 (default) shell TDI -> Identify -> SWJ-DP -> shell TDO
    //   0           shell TDI -> SWJ-DP  -> Identify -> shell TDO
    // Override with VCS `-pvalue+tb_top.CHAIN_IICE_NEAREST_TDI=0`
    // (the bench Makefile's `control-order` target does exactly that).
    // =========================================================================
    parameter bit CHAIN_IICE_NEAREST_TDI = 1'b1;

    // =========================================================================
    // Clock. dut_clk is a partition pin: the shell's DRP MMCM makes it and the
    // RM never generates a clock (fpga/shell/boundary.yaml, "No clock
    // generation inside an RM").
    // =========================================================================
    reg dut_clk;
    initial dut_clk = 1'b0;
    always  #(FCLK_PERIOD_NS/2) dut_clk = ~dut_clk;

    // =========================================================================
    // The three contract resets, driven by the cocotb harness. The RM ANDs
    // them; dut_resetn is the one the hazard test pulses.
    // =========================================================================
    reg dut_resetn;
    reg rp_resetn;
    reg dbg_resetn;

    initial begin
        dut_resetn = 1'b0;
        rp_resetn  = 1'b0;
        dbg_resetn = 1'b0;
        #200;
        dut_resetn = 1'b1;
        rp_resetn  = 1'b1;
        dbg_resetn = 1'b1;
    end

    // =========================================================================
    // The four JTAG partition pins — the whole host-visible interface. The
    // cocotb driver wiggles these with OpenOCD remote_bitbang byte semantics,
    // exactly as firmware/jtag_server does through jtag_bb @ 0x44A7_0000.
    // =========================================================================
    reg  jtag_tck;
    reg  jtag_tms;
    reg  jtag_tdi;
    wire jtag_tdo;

    initial begin
        jtag_tck = 1'b0;
        jtag_tms = 1'b1;   // idle high
        jtag_tdi = 1'b0;
    end

    // =========================================================================
    // Inert partition-pin stimulus. Nothing in this bench drives the RM's other
    // groups; they are held at the values the DFX decoupler clamps them to so
    // an accidental dependency shows up as a failure, not as a lucky X.
    // =========================================================================
    localparam int NGPIO = 16;

    wire [1:0]  phy_rmii_txd;
    wire        phy_rmii_tx_en, mdc, mdio_o, mdio_oe;
    wire [7:0]  uart_tx_tdata;
    wire        uart_tx_tvalid, uart_rx_tready, swo;
    wire [31:0] rm_id;
    wire        dut_lockup, irq_out;
    wire [NGPIO-1:0] dut_gpio_o, dut_gpio_oe;
    wire        qspi_sclk, qspi_csn;
    wire [3:0]  qspi_io_o, qspi_io_oe;

    // =========================================================================
    // THE DESIGN UNDER TEST — the real RM top, straight out of fpga/rp/.
    // =========================================================================
    rp_nanosoc_iice_shim #(
        .NGPIO                (NGPIO),
        .IICE_TAP_NEAREST_TDI (CHAIN_IICE_NEAREST_TDI)
    ) u_rm (
        .dut_clk          (dut_clk),
        .dut_resetn       (dut_resetn),
        .rp_resetn        (rp_resetn),
        .dbg_resetn       (dbg_resetn),

        .jtag_tck         (jtag_tck),
        .jtag_tms         (jtag_tms),
        .jtag_tdi         (jtag_tdi),
        .jtag_tdo         (jtag_tdo),

        .phy_rmii_ref_clk (1'b0),
        .phy_rmii_crs_dv  (1'b0),
        .phy_rmii_rxd     (2'b00),
        .phy_rmii_txd     (phy_rmii_txd),
        .phy_rmii_tx_en   (phy_rmii_tx_en),
        .mdc              (mdc),
        .mdio_o           (mdio_o),
        .mdio_oe          (mdio_oe),
        .mdio_i           (1'b0),

        .uart_tx_tdata    (uart_tx_tdata),
        .uart_tx_tvalid   (uart_tx_tvalid),
        .uart_tx_tready   (1'b1),
        .uart_rx_tdata    (8'h00),
        .uart_rx_tvalid   (1'b0),
        .uart_rx_tready   (uart_rx_tready),
        .swo              (swo),

        .rm_id            (rm_id),
        .dut_lockup       (dut_lockup),
        .irq_out          (irq_out),

        .dut_gpio_o       (dut_gpio_o),
        .dut_gpio_oe      (dut_gpio_oe),
        .dut_gpio_i       ({NGPIO{1'b0}}),

        .qspi_sclk        (qspi_sclk),
        .qspi_csn         (qspi_csn),
        .qspi_io_o        (qspi_io_o),
        .qspi_io_oe       (qspi_io_oe),
        .qspi_io_i        (4'b0000)
    );

    // =========================================================================
    // Hierarchical aliases the cocotb tests read. Declared here so a rename
    // inside the stub breaks ELABORATION with a named error, rather than
    // producing a test that silently reads X and passes its `!=` assertions.
    // =========================================================================
    wire        cpu0_sys_hclk    = u_rm.u_core.cpu0_sys_hclk;
    wire        cpu0_sys_hresetn = u_rm.u_core.cpu0_sys_hresetn;
    wire [4:0]  iice_ir          = u_rm.u_core.iice_obs_ir;
    wire [31:0] iice_idhw        = u_rm.u_core.iice_obs_idhw;

    // The order the shim actually built, exposed so a test can assert the
    // bench and the DUT agree about which control it is running.
    wire        chain_iice_nearest_tdi = CHAIN_IICE_NEAREST_TDI;

    // =========================================================================
    // Waveform dump (gated)
    // =========================================================================
    initial begin
        if ($test$plusargs("WAVES")) begin
            $dumpfile("waves.vcd");
            $dumpvars(0, tb_top);
        end
    end

endmodule
