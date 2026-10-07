//-----------------------------------------------------------------------------
// tb_top.sv — jtag_dap_bringup  (P2 SIM GATE, SOC400_BASELINE_INTEGRATION.md)
// A joint work commissioned on behalf of SoC Labs, under Arm Academic Access license.
//
// Copyright (C) 2026, SoC Labs (www.soclabs.org)
//-----------------------------------------------------------------------------
// The ONE segment nothing currently proves: the host-JTAG serial line ->
// SWJ-DP serial decode (cxdapswjdp + nanosoc_swj_dp_gate) -> DP-bus -> AHB-AP
// -> unconditional-capture dbg_ahb_bridge -> Cortex-M0 debug slave, halting a
// real Cortex-M0 to DHCSR S_HALT over the SERIAL TCK/TMS/TDI/TDO pins.
//
// vs the proven multicore bench cocotb/coresight_soc400_dap_to_core: that one
// drives the PARALLEL DP-bus (dp_dapsel/dp_dapenable/...) directly and thereby
// bypasses cxdapswjdp + nanosoc_swj_dp_gate. THIS bench exposes only the four
// JTAG wires + the reset/strap pins, and a cocotb remote_bitbang driver wiggles
// them, so the SWJ-DP serial front-end IS in the path under test.
//
//   external JTAG pins (tck/tms/tdi/tdo)
//        |
//   nanosoc_swj_dap_ss  (nanosoc_swj_dp_gate -> cxdapswjdp
//                        -> nanosoc_dap_ss = NUM_AP x cxdapahbap + xlate + arb
//                        + a dbg-AHB timeout guard; NUM_AP=1 here)
//        |
//   single shared AHB master  -- 1-region mock matrix decoder
//                                    |
//                                    +-- 0xA0xxxxxx  cpu_0_dbg_bridge
//                                                        (nanosoc_dbg_ahb_bridge,
//                                                         UNCONDITIONAL-CAPTURE)
//                                                        -> slcorem0.DBGAHB
//
// The M0 has EXTERNAL_DAP=1 (no internal CORTEXM0DAP); its AHB master fetches
// from a tiny inline ROM (SP@0x0, reset vector -> 0x8, Thumb branch-to-self).
// The single AP is wired to the core at 0xA0; every other address lands on a
// default-OKAY slave (the old two-AP build parked a dormant AP1 at 0xB0).
//-----------------------------------------------------------------------------

`timescale 1ns/1ps

module tb_top;

    parameter FCLK_PERIOD_NS = 10;   // 100 MHz HCLK / core FCLK

    // =========================================================================
    // System clock and reset
    // =========================================================================
    reg sys_fclk;
    reg sys_sysresetn;

    initial sys_fclk = 1'b0;
    always  #(FCLK_PERIOD_NS/2) sys_fclk = ~sys_fclk;

    initial begin
        sys_sysresetn = 1'b0;
        #200;
        sys_sysresetn = 1'b1;
    end

    // =========================================================================
    // External serial JTAG pins — driven by the cocotb remote_bitbang driver.
    // Named jtag_* to mirror the MPS3 partition-boundary pins the shell's
    // jtag_bb/jtag_server will wiggle (docs/contracts/partition-pins.md, the
    // Option-B re-mint target).
    // =========================================================================
    reg  jtag_tck;      // TCK  (SWJ swclktck)
    reg  jtag_tms;      // TMS  (SWJ swditms)
    reg  jtag_tdi;      // TDI
    wire jtag_tdo;      // TDO
    reg  jtag_ntrst;    // nTRST — async TAP reset pin
    reg  jtag_npotrst;  // nPOTRST — SWJ power-on reset pin
    reg  swj_enable;    // enable strap (1 = this chiplet drives the pins)

    initial begin
        jtag_tck     = 1'b0;
        jtag_tms     = 1'b1;   // idle high
        jtag_tdi     = 1'b0;
        jtag_ntrst   = 1'b1;   // released; driver pulses it low to reset the TAP
        jtag_npotrst = 1'b1;
        swj_enable   = 1'b1;
    end

    // =========================================================================
    // CPU0 instance (slcorem0 with EXTERNAL_DAP=1)
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

    // DBGAHB (driven by cpu_0_dbg_bridge)
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
        .SYS_FCLK            (sys_fclk),
        .SYS_SYSRESETn       (sys_sysresetn),
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
        .CORE_LOCKUP         (),
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

    // =========================================================================
    // Spin-loop ROM (responds to the core's AHB master)
    //   0x0 -> 0x20002000 (initial SP)
    //   0x4 -> 0x00000009 (initial PC, Thumb-bit, target 0x8)
    //   0x8+ -> 0xE7FEE7FE (Thumb branch-to-self, twice for word fetches)
    // =========================================================================
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

    // =========================================================================
    // SWJ-DP DAP subsystem — SERIAL pins in, single shared AHB master out.
    //
    // DPRESETn = cpu0_sys_poresetn (POWER-ON reset, NOT the pulsed HRESETn):
    // the SOC400_BASELINE_INTEGRATION.md "don't get this wrong" rule — tying
    // the SWJ-DP debug-power reset to the system reset would drop the live host
    // link on every SYSRESETREQ.  ntrst/npotrst are the EXTERNAL JTAG reset
    // pins, driven by the probe (cocotb).
    // =========================================================================
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

    wire        dap_swdo;
    wire        dap_swdoen;
    wire        dap_ntdoen;
    wire        dap_cdbgpwrupreq;
    wire        dap_csyspwrupreq;
    wire        dap_cdbgrstreq;
    wire        dap_jtagnsw;
    wire        dap_jtagtop;

    // NUM_AP=1 -- the FIELDED single-core nanoSoC configuration, and the same
    // parameter set tests/jtag_chain/iice_core_stub.sv:302 uses.
    //
    // These four names used to be CPU0_DBG_BASE / CPU1_DBG_BASE /
    // AP0_ROMBASEADDR / AP1_ROMBASEADDR, from the pre-NUM_AP two-AP block. When
    // the tech-block move replaced the _single/_multi pair with ONE
    // NUM_AP-parameterised module, those names ceased to exist -- and VCS
    // reports an override of a parameter a module does not have as
    // "Warning-[AOUP] ... will ignore it", not an error. The bench went on
    // passing 8/8 with the AP silently on its 0xF000_0003 ROM-base default.
    // test_ahb_ap_idr now ASSERTS the BASE readback, so that cannot recur
    // silently.
    nanosoc_swj_dap_ss #(
        .NUM_AP          (1),
        .CPU_DBG_BASE    (32'hA000_0000),
        .AP_ROMBASEADDR  (32'hA000_0003),
        .DAP_TARGETID    (32'h0BB1_1477),
        .DAP_INSTANCEID  (4'h0)
    ) u_dap (
        .HCLK         (cpu0_sys_hclk),
        .HRESETn      (cpu0_sys_hresetn),
        .DPRESETn     (cpu0_sys_poresetn),   // POR — see note above
        // External serial pins
        .swclktck     (jtag_tck),
        .swditms      (jtag_tms),
        .tdi          (jtag_tdi),
        .swdo         (dap_swdo),
        .swdoen       (dap_swdoen),
        .tdo          (jtag_tdo),
        .ntdoen       (dap_ntdoen),
        .ntrst        (jtag_ntrst),
        .npotrst      (jtag_npotrst),
        .swj_enable   (swj_enable),
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
        // Shared AHB master
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

    // =========================================================================
    // Mock 1-region matrix decoder + debug bridge
    //   0xA0 -> cpu_0_dbg_bridge -> slcorem0.DBGAHB
    //   else -> default-OKAY slave (rdata=0) — every non-0xA0 debug access
    // =========================================================================
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
        // Accepted-and-ignored by the bridge, but leaving it dangling raised a
        // Warning-[TFIPC] "too few instance port connections" on every build --
        // noise that would mask a REAL missing port the next time this boundary
        // moves.
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

    // Slave-side mux: pick the bridge whose HSEL was asserted last address
    // phase, default to all-zero/ready/OKAY.
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
    // Waveform dump (gated)
    // =========================================================================
    initial begin
        if ($test$plusargs("WAVES")) begin
            $dumpfile("waves.vcd");
            $dumpvars(0, tb_top);
        end
    end

endmodule
