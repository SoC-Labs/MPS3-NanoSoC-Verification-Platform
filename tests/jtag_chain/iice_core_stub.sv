//-----------------------------------------------------------------------------
// iice_core_stub.sv — a bench-side stand-in for the INSTRUMENTED
// `rp_nanosoc_iice_core` EDIF cell, so the REAL rp_nanosoc_iice_shim.sv can be
// simulated.
//
// A joint work commissioned on behalf of SoC Labs, under Arm Academic Access license.
// Copyright (C) 2026, SoC Labs (www.soclabs.org)
//-----------------------------------------------------------------------------
// WHY THIS FILE, AND WHY IT IS NOT A MOCK OF THE THING UNDER TEST
// ===============================================================
// The thing under test is the CHAIN SPLICE in fpga/rp/nanosoc_iice/
// rp_nanosoc_iice_shim.sv — six assign statements that decide which TAP sits
// where on the one jtag_* wire-set. This bench instantiates that REAL file
// (tests/jtag_chain/tb_top.sv), unmodified, straight out of the RM directory.
//
// What the shim instantiates is `rp_nanosoc_iice_core`, which in a real build
// is the 39-port Synplify/Identify EDIF cell: 35 contract ports plus the four
// identify_jtag_* the instrumentor adds. That cell contains 240 files of nanoSoC
// and a vendor-obfuscated soft TAP; it cannot be elaborated in a bench, and the
// EDIF only exists after a licensed Synplify run.
//
// So THIS module presents exactly that 39-port boundary and puts behind it the
// two things the chain is made of:
//
//   jtag_*            -> the REAL Arm SoC-400 SWJ-DP (cxdapswjdp via
//                        nanosoc_swj_dap_ss) -> AHB-AP -> dbg bridge -> a REAL
//                        Cortex-M0, wired exactly as tests/jtag_dap_bringup's
//                        tb_top wires it. Not a model.
//   identify_jtag_*   -> iice_soft_tap_model.sv, a behavioural 1149.1 TAP whose
//                        IR length and IDCODE are read off Identify's own device
//                        table. A model, and its header says so.
//
// The 39-port boundary is not retyped from memory: tests/jtag_chain/
// check_core_stub_ports.py derives the expected list from the REAL
// fpga/rp/nanosoc_iice/rp_nanosoc_iice_core.sv (via fpga/dfx/pin_check.py's own
// parser) plus lint/gen_lint_stubs.py's IDENTIFY_SOFT_TAP list, and the bench
// Makefile runs it BEFORE compiling. If the RM's boundary moves and this file
// does not, the bench refuses to build rather than proving a stale topology.
//
// STRAPS mirrored from the RM, because they change what the bench can prove:
//   ntrst    = 1'b1                 rp_nanosoc_wrapper.sv:526 — the TAP has NO
//                                   asynchronous reset reaching the boundary,
//                                   and neither does Identify's soft TAP, so
//                                   Test-Logic-Reset is 5x TMS and nothing else.
//   npotrst  = dut_sys_sysresetn    rp_nanosoc_wrapper.sv:527 — the DUT's system
//                                   reset RESETS THE SWJ-DP TAP. That is the
//                                   chain's real operational hazard and
//                                   test_dut_reset_reloads_dap_idcode exercises
//                                   it deliberately.
//   swj_enable = 1'b1               rp_nanosoc_wrapper.sv:528
// and dut_sys_sysresetn is the AND of the three contract resets, as the wrapper
// computes it.
//
// DPRESETn = the core's power-on reset, NOT the pulsed HRESETn — the
// SOC400_BASELINE_INTEGRATION.md "don't get this wrong" rule, carried verbatim
// from tests/jtag_dap_bringup/tb_top.sv so a live host link survives a
// SYSRESETREQ.
//-----------------------------------------------------------------------------
`timescale 1ns / 1ps

module rp_nanosoc_iice_core #(
    parameter int NGPIO = 16
) (
    // ---- Clocks & resets (shell -> RP) --------------------------------------
    input  logic dut_clk,
    input  logic dut_resetn,
    input  logic rp_resetn,
    input  logic dbg_resetn,

    // ---- Processor debug — this cell's position in the chain -----------------
    input  logic jtag_tck,
    input  logic jtag_tms,
    input  logic jtag_tdi,
    output logic jtag_tdo,

    // ---- RM debug — BSCAN legs (boundary.yaml `dbgbscan`, 2026-10 ILA mint) ----
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

    // ---- Ethernet — RMII + MDIO ---------------------------------------------
    input  logic       phy_rmii_ref_clk,
    input  logic       phy_rmii_crs_dv,
    input  logic [1:0] phy_rmii_rxd,
    output logic [1:0] phy_rmii_txd,
    output logic       phy_rmii_tx_en,
    output logic       mdc,
    output logic       mdio_o,
    output logic       mdio_oe,
    input  logic       mdio_i,

    // ---- Console / trace — AXI-Stream byte ----------------------------------
    output logic [7:0] uart_tx_tdata,
    output logic       uart_tx_tvalid,
    input  logic       uart_tx_tready,
    input  logic [7:0] uart_rx_tdata,
    input  logic       uart_rx_tvalid,
    output logic       uart_rx_tready,
    output logic       swo,

    // ---- Status / misc (RP -> shell) ----------------------------------------
    output logic [31:0] rm_id,
    output logic        dut_lockup,
    output logic        irq_out,

    // ---- Board-port / GPIO passthrough --------------------------------------
    output logic [NGPIO-1:0] dut_gpio_o,
    output logic [NGPIO-1:0] dut_gpio_oe,
    input  logic [NGPIO-1:0] dut_gpio_i,

    // ---- Flash / QSPI XiP ---------------------------------------------------
    output logic       qspi_sclk,
    output logic       qspi_csn,
    output logic [3:0] qspi_io_o,
    output logic [3:0] qspi_io_oe,
    input  logic [3:0] qspi_io_i,

    // ---- the four ports the Identify instrumentor ADDS -----------------------
    input  logic identify_jtag_tck,
    input  logic identify_jtag_tms,
    input  logic identify_jtag_tdi,
    output logic identify_jtag_tdo
);

    // =========================================================================
    // Reset combination — rp_nanosoc_wrapper.sv's "Reset combination" note:
    // nanosoc has exactly ONE reset input, so all three contract resets are
    // ANDed (active-low; ANY of the three resets the whole core).
    // =========================================================================
    wire dut_sys_sysresetn = dut_resetn & rp_resetn & dbg_resetn;

    // =========================================================================
    // GSR — the FPGA's global set/reset, modelled. NOT a partition pin, and NOT
    // a licence to invent a TRST wire the design does not have.
    //
    // MEASURED, and it cost a debug cycle: the Arm SWJ-DP's TAP controller state
    // register is reset by `ntrst` and by nothing else (cxdapswjdp's JTAG
    // protocol block has 3 `negedge ntrst` blocks alongside 17 on `npotrst`).
    // The RM straps dap_ntrst HIGH -- rp_nanosoc_wrapper.sv:526, deliberately,
    // because no TRST wire crosses the fielded boundary -- so in RTL simulation
    // that FSM starts at X and NEVER resolves: five TCK with TMS high walk an X
    // state to an X state, TDO stays X/0, and every IDCODE read returns zero.
    //
    // On the real KU115 it works, and the reason is device-level: at the end of
    // configuration Xilinx's GSR clears every flip-flop in the fabric, so the
    // TAP FSM powers up at a DEFINED state and the 5x-TMS reset then works. This
    // pulse models exactly that one device behaviour and nothing else -- it is
    // gone 100 ns into the run, long before any test drives TCK, and no test
    // uses it. Remove it and this bench reports a dead DAP that the silicon does
    // not have; drive it from a test and you would be testing a TRST pin that
    // does not exist.
    reg bench_gsr_n = 1'b0;
    initial begin
        bench_gsr_n = 1'b0;
        #100;
        bench_gsr_n = 1'b1;   // released for the rest of the simulation
    end

    // =========================================================================
    // The Identify soft TAP — a MODEL. See iice_soft_tap_model.sv's header.
    // =========================================================================
    wire [4:0]  iice_obs_ir;
    wire [31:0] iice_obs_idhw;
    wire [15:0] iice_obs_hcr;

    iice_soft_tap_model u_iice_tap (
        .tck       (identify_jtag_tck),
        .tms       (identify_jtag_tms),
        .tdi       (identify_jtag_tdi),
        .tdo       (identify_jtag_tdo),
        .obs_ir    (iice_obs_ir),
        .obs_idhw  (iice_obs_idhw),
        .obs_hcr   (iice_obs_hcr)
    );

    // =========================================================================
    // The DUT side: REAL SoC-400 SWJ-DP -> AHB-AP -> dbg bridge -> Cortex-M0.
    // Structure carried from tests/jtag_dap_bringup/tb_top.sv, which is the
    // proven P2 sim gate for this exact path (8/8 under VCS).
    // =========================================================================
    wire        cpu0_sys_poresetn;
    wire        cpu0_sys_hclk;
    wire        cpu0_sys_hresetn;

    wire [31:0] cpu0_haddr;
    wire  [1:0] cpu0_htrans;
    wire        cpu0_hwrite;
    wire  [2:0] cpu0_hsize;
    wire  [2:0] cpu0_hburst;
    wire  [3:0] cpu0_hprot;
    wire [31:0] cpu0_hwdata;
    wire        cpu0_hmastlock;
    wire [31:0] cpu0_hrdata;
    wire        cpu0_hready;
    wire        cpu0_hresp;

    wire [31:0] cpu0_dbg_slvaddr;
    wire [31:0] cpu0_dbg_slvwdata;
    wire  [1:0] cpu0_dbg_slvtrans;
    wire        cpu0_dbg_slvwrite;
    wire  [1:0] cpu0_dbg_slvsize;
    wire [31:0] cpu0_dbg_slvrdata;
    wire        cpu0_dbg_slvready;
    wire        cpu0_dbg_slvresp;

    wire        cpu0_core_sysresetreq;

    slcorem0 #(
        .EXTERNAL_DAP    (1),
        .DBG             (1),
        .NUMIRQ          (32),
        .CLKGATE_PRESENT (0),
        .ROMTABLE_BASE   (32'hE00FF000)
    ) u_cpu0 (
        .SYS_FCLK            (dut_clk),
        .SYS_SYSRESETn       (dut_sys_sysresetn),
        .SYS_SCANENABLE      (1'b0),
        .SYS_TESTMODE        (1'b0),
        .SYS_SYSRESETREQ     (1'b0),
        .CORE_PRMURESETREQ   (),
        .SYS_PORESETn        (cpu0_sys_poresetn),
        .SYS_HCLK            (cpu0_sys_hclk),
        .SYS_HRESETn         (cpu0_sys_hresetn),
        .CORE_PMUENABLE      (1'b0),
        .CORE_PMUDBGRESETREQ (),
        .HADDR               (cpu0_haddr),
        .HTRANS              (cpu0_htrans),
        .HWRITE              (cpu0_hwrite),
        .HSIZE               (cpu0_hsize),
        .HBURST              (cpu0_hburst),
        .HPROT               (cpu0_hprot),
        .HWDATA              (cpu0_hwdata),
        .HMASTLOCK           (cpu0_hmastlock),
        .HRDATA              (cpu0_hrdata),
        .HREADY              (cpu0_hready),
        .HRESP               (cpu0_hresp),
        .CORE_NMI            (1'b0),
        .CORE_IRQ            (32'h0),
        .CORE_TXEV           (),
        .CORE_RXEV           (1'b0),
        .CORE_LOCKUP         (dut_lockup),
        .CORE_SYSRESETREQ    (cpu0_core_sysresetreq),
        .CORE_SLEEPING       (),
        .CORE_SLEEPDEEP      (),
        .CORE_SWDI           (1'b1),
        .CORE_SWCLK          (1'b0),
        .CORE_SWDO           (),
        .CORE_SWDOEN         (),
        .DBGAHB_SLVADDR      (cpu0_dbg_slvaddr),
        .DBGAHB_SLVWDATA     (cpu0_dbg_slvwdata),
        .DBGAHB_SLVTRANS     (cpu0_dbg_slvtrans),
        .DBGAHB_SLVWRITE     (cpu0_dbg_slvwrite),
        .DBGAHB_SLVSIZE      (cpu0_dbg_slvsize),
        .DBGAHB_SLVRDATA     (cpu0_dbg_slvrdata),
        .DBGAHB_SLVREADY     (cpu0_dbg_slvready),
        .DBGAHB_SLVRESP      (cpu0_dbg_slvresp)
    );

    // Spin-loop ROM on the core's own AHB master:
    //   0x0 -> initial SP, 0x4 -> reset vector (Thumb, target 0x8),
    //   0x8+ -> 0xE7FEE7FE (branch-to-self).
    function automatic [31:0] rom_word(input [31:0] addr);
        case (addr & 32'hFFFFFFFC)
            32'h00000000: rom_word = 32'h20002000;
            32'h00000004: rom_word = 32'h00000009;
            default:      rom_word = 32'hE7FEE7FE;
        endcase
    endfunction

    reg [31:0] cpu0_addr_q;
    reg        cpu0_read_q;
    always @(posedge cpu0_sys_hclk or negedge cpu0_sys_hresetn) begin
        if (!cpu0_sys_hresetn) begin
            cpu0_addr_q <= 32'h0;
            cpu0_read_q <= 1'b0;
        end else if (cpu0_htrans[1]) begin
            cpu0_addr_q <= cpu0_haddr;
            cpu0_read_q <= ~cpu0_hwrite;
        end else begin
            cpu0_read_q <= 1'b0;
        end
    end
    assign cpu0_hrdata = cpu0_read_q ? rom_word(cpu0_addr_q) : 32'h0;
    assign cpu0_hready = 1'b1;
    assign cpu0_hresp  = 1'b0;

    // ---- the SWJ-DP itself ---------------------------------------------------
    wire [31:0] dap_haddr;
    wire  [1:0] dap_htrans;
    wire        dap_hwrite;
    wire  [2:0] dap_hsize;
    wire  [2:0] dap_hburst;
    wire  [3:0] dap_hprot;
    wire        dap_hmastlock;
    wire [31:0] dap_hwdata;
    wire [31:0] dap_hrdata;
    wire        dap_hready;
    wire        dap_hresp;

    wire dap_swdo, dap_swdoen, dap_ntdoen;
    wire dap_cdbgpwrupreq, dap_csyspwrupreq, dap_cdbgrstreq;
    wire dap_jtagnsw, dap_jtagtop;

    // NUM_AP=1: the fielded single-core nanoSoC has ONE AHB-AP at APSEL 0.
    // The shared tech block replaced the old _single/_multi pair (and the old
    // CPU0_/CPU1_/AP0_/AP1_ parameter names) with one parameterised block.
    nanosoc_swj_dap_ss #(
        .NUM_AP          (1),
        .CPU_DBG_BASE    (32'hA000_0000),
        .AP_ROMBASEADDR  (32'hA000_0003),
        .DAP_TARGETID    (32'h0BB1_1477),
        .DAP_INSTANCEID  (4'h0)
    ) u_dap (
        .HCLK         (cpu0_sys_hclk),
        .HRESETn      (cpu0_sys_hresetn),
        .DPRESETn     (cpu0_sys_poresetn),
        // The SWJ-DP's four serial legs ARE this cell's jtag_* contract ports.
        // Which end of the chain they land on is the SHIM's decision.
        .swclktck     (jtag_tck),
        .swditms      (jtag_tms),
        .tdi          (jtag_tdi),
        .swdo         (dap_swdo),
        .swdoen       (dap_swdoen),
        .tdo          (jtag_tdo),
        .ntdoen       (dap_ntdoen),
        // Straps, mirrored from rp_nanosoc_wrapper.sv:526-528. `ntrst` is the
        // strapped-high one; bench_gsr_n only models the FPGA's power-up clear
        // of that flop bank (see the GSR note above) and is high thereafter, so
        // from the first TCK onwards this IS the RM's `1'b1`.
        .ntrst        (bench_gsr_n),
        .npotrst      (dut_sys_sysresetn & bench_gsr_n),
        .swj_enable   (1'b1),
        .dbgen        (1'b1),
        .spiden       (1'b1),
        .cdbgpwrupreq (dap_cdbgpwrupreq),
        .cdbgpwrupack (1'b1),
        .csyspwrupreq (dap_csyspwrupreq),
        .csyspwrupack (1'b1),
        .cdbgrstreq   (dap_cdbgrstreq),
        .cdbgrstack   (1'b1),
        .jtagnsw      (dap_jtagnsw),
        .jtagtop      (dap_jtagtop),
        .HADDR        (dap_haddr),
        .HTRANS       (dap_htrans),
        .HWRITE       (dap_hwrite),
        .HSIZE        (dap_hsize),
        .HBURST       (dap_hburst),
        .HPROT        (dap_hprot),
        .HMASTLOCK    (dap_hmastlock),
        .HWDATA       (dap_hwdata),
        .HRDATA       (dap_hrdata),
        .HREADY       (dap_hready),
        .HRESP        (dap_hresp)
    );

    // Mock 1-region matrix decoder + debug bridge (0xA0 -> CPU0 DBGAHB).
    wire bridge0_hsel = (dap_haddr[31:24] == 8'hA0);
    wire [31:0] bridge0_hrdata;
    wire        bridge0_hreadyout;
    wire        bridge0_hresp;

    nanosoc_dbg_ahb_bridge u_cpu0_dbg_bridge (
        .HCLK              (cpu0_sys_hclk),
        .HRESETn           (cpu0_sys_hresetn),
        .HSEL              (bridge0_hsel),
        .HADDR             (dap_haddr),
        .HTRANS            (dap_htrans),
        .HWRITE            (dap_hwrite),
        .HSIZE             (dap_hsize),
        .HBURST            (dap_hburst),
        .HPROT             (dap_hprot[3:0]),
        .HMASTLOCK         (dap_hmastlock),
        .HWDATA            (dap_hwdata),
        .HREADY            (dap_hready),
        .HRDATA            (bridge0_hrdata),
        .HREADYOUT         (bridge0_hreadyout),
        .HRESP             (bridge0_hresp),
        .DBGAHB_SLVADDR    (cpu0_dbg_slvaddr),
        .DBGAHB_SLVWDATA   (cpu0_dbg_slvwdata),
        .DBGAHB_SLVTRANS   (cpu0_dbg_slvtrans),
        .DBGAHB_SLVWRITE   (cpu0_dbg_slvwrite),
        .DBGAHB_SLVSIZE    (cpu0_dbg_slvsize),
        .DBGAHB_SLVRDATA   (cpu0_dbg_slvrdata),
        .DBGAHB_SLVREADY   (cpu0_dbg_slvready),
        .DBGAHB_SLVRESP    (cpu0_dbg_slvresp)
    );

    reg active_b0_q;
    always @(posedge cpu0_sys_hclk or negedge cpu0_sys_hresetn) begin
        if (!cpu0_sys_hresetn)
            active_b0_q <= 1'b0;
        else if (dap_hready)
            active_b0_q <= bridge0_hsel & dap_htrans[1];
    end

    assign dap_hrdata = active_b0_q ? bridge0_hrdata    : 32'h0;
    assign dap_hready = active_b0_q ? bridge0_hreadyout : 1'b1;
    assign dap_hresp  = active_b0_q ? bridge0_hresp     : 1'b0;

    // =========================================================================
    // Everything the chain does not need: driven to the DFX decoupler's own
    // safe-idle values (fpga/shell/boundary.yaml `clamp` column), so an
    // undriven-output warning cannot hide a real one.
    // =========================================================================
    assign rm_id          = 32'h4949_4345;   // "IICE"
    assign dbg_bscan_tdo  = 1'b0;            // no RM debug hub
    assign irq_out        = 1'b0;
    assign phy_rmii_txd   = 2'b00;
    assign phy_rmii_tx_en = 1'b0;
    assign mdc            = 1'b0;
    assign mdio_o         = 1'b0;
    assign mdio_oe        = 1'b0;
    assign uart_tx_tdata  = 8'h00;
    assign uart_tx_tvalid = 1'b0;
    assign uart_rx_tready = 1'b0;
    assign swo            = 1'b0;
    assign dut_gpio_o     = {NGPIO{1'b0}};
    assign dut_gpio_oe    = {NGPIO{1'b0}};
    assign qspi_sclk      = 1'b0;
    assign qspi_csn       = 1'b1;            // flash DESELECTED — not 0
    assign qspi_io_o      = 4'b0000;
    assign qspi_io_oe     = 4'b0000;

    // Deliberately-unread inputs, named so a reader can see they were considered.
    wire _unused_ok = &{1'b0, phy_rmii_ref_clk, phy_rmii_crs_dv, phy_rmii_rxd,
                        mdio_i, uart_tx_tready, uart_rx_tdata, uart_rx_tvalid,
                        dut_gpio_i, qspi_io_i, cpu0_core_sysresetreq,
                        dap_swdo, dap_swdoen, dap_ntdoen, dap_cdbgpwrupreq,
                        dap_csyspwrupreq, dap_cdbgrstreq, dap_jtagnsw,
                        dap_jtagtop, cpu0_hsize, cpu0_hburst, cpu0_hprot,
                        cpu0_hwdata, cpu0_hmastlock, dap_hsize,
                        iice_obs_ir, iice_obs_idhw, iice_obs_hcr, 1'b0};

endmodule
