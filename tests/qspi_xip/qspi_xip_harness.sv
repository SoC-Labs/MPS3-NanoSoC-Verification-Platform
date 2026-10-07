// -----------------------------------------------------------------------------
// qspi_xip_harness.sv — SV harness for the M2 XiP gate (tests/qspi_xip).
//
// DUT = `qspi_flash_ahb`, the EXACT wrapper module the NanoSoC top instantiates
// as u_qspi_flash_0 (nanosoc_m0_soc/build_soc/rtl/nanosoc.sv:1237).  Not
// top_ahb_qspi directly — the whole point of the gate is that the module the
// SoC integrates is the module under test, so a wrapper-level port-mapping bug
// (o/i/e swap, PADDR slice, HSEL vs HSELx) shows up here.
//
// This file exists for four reasons:
//
// 1. TRISTATE RESOLUTION.  The SoC-facing wrapper presents split-direction pads
//    (QSPI_IO_o / QSPI_IO_i / QSPI_IO_e) so the SoC top can drop real IOBUFs on
//    them.  The SST26VF064B VIP has a bidirectional `inout [3:0] SIO`.  The
//    harness resolves them per lane, exactly as ahb_qspi's own pad wrapper does
//    (ahb_qspi/pad_level/generic/ahb_qspi_pads.v):
//        SIO[i]      = QSPI_IO_e[i] ? QSPI_IO_o[i] : 1'bz;
//        QSPI_IO_i[i]= SIO[i];
//    No pull-ups, matching ahb_qspi_pads.v (the controller drives IO[3:2] high
//    in SPI mode, which is what keeps the flash's HOLD#/WP# deasserted).
//
// 2. HREADY <- HREADYOUT.  AHB-Lite: the global HREADY seen by a slave must be
//    the selected slave's HREADYOUT.  Both the XiP data port and the config
//    AHB-to-APB bridge insert wait states; a bench that hardwires HREADY=1 has
//    its transfers silently dropped.  Tied in RTL on BOTH ports below.
//
// 3. THE APB BRIDGE.  In the SoC, qspi_ctrl @0x7400_0000 reaches the wrapper's
//    APB port through a `cmsdk_ahb_to_apb` (nanosoc.sv:1281, u_qspi_apb_bridge,
//    ADDRWIDTH=16).  The same bridge is instantiated here so the register
//    accesses the tests perform traverse the same logic as on silicon.  Inside
//    the wrapper, PADDR[15:12] picks the controller (0x0) or the CG092 cache
//    config (0x1) — hence CACHE_CONFIG at APB offset 0x1000.
//
// 4. FLASH BACKDOOR PRELOAD.  The gate must compare the AHB XiP read against an
//    INDEPENDENT golden reference.  If the bench programmed the flash through
//    the very controller it is testing, a symmetric bug (e.g. a byte-lane swap
//    present in both the program and the read path) would cancel out and the
//    test would pass on broken RTL.  So the flash array is loaded by hierarchical
//    deposit into the VIP's own non-volatile memory (`u_flash.I0.memory[]` — the
//    backdoor the model's own header documents, "load these memorys with customer
//    data ... after time 0"), with a byte pattern that is a function of the byte
//    ADDRESS.  Any byte-order or off-by-N error therefore produces a wrong value,
//    not a coincidentally-right one.
//
// Notes
// -----
//  * WATCHDOG_WIDTH is deliberately NOT overridden.  ahb_qspi's own cocotb TB
//    forces it to 12 to stay under its AHB master timeout; the SoC wrapper does
//    not expose the parameter, so the DUT here runs with top_ahb_qspi's default
//    of 16 — i.e. the configuration that is actually taped out / put on the FPGA.
//    The cocotb AHB masters are given a correspondingly generous timeout instead.
//  * The `defparam`s on the VIP shorten its erase/program/reset times.  They are
//    TIMING ONLY (Tbe/Tse/Tsce/Tpp/Tws) and change no data behaviour; same values
//    ahb_qspi's TB uses.  Without them a flash RST costs ~ms of sim time.
// -----------------------------------------------------------------------------
`timescale 1ns/10ps

module qspi_xip_harness #(
    // Byte window preloaded into the flash VIP.  Mirrored in test_qspi_xip.py —
    // keep the two in step.  Must lie inside the controller's 22-bit aperture.
    parameter integer PRELOAD_BASE = 32'h0003_0000,
    parameter integer PRELOAD_LEN  = 512
)(
    input  wire        HCLK,
    input  wire        HRESETn,

    // --- config AHB port -> cmsdk_ahb_to_apb -> DUT APB (SoC: qspi_ctrl) ------
    input  wire [31:0] config_HADDR,
    input  wire [ 1:0] config_HTRANS,
    input  wire        config_HWRITE,
    input  wire [ 2:0] config_HSIZE,
    input  wire [ 2:0] config_HBURST,
    input  wire [31:0] config_HWDATA,
    input  wire        config_HSEL,
    output wire [31:0] config_HRDATA,
    output wire        config_HREADY,     // = bridge HREADYOUT (see reason 2)
    output wire        config_HRESP,

    // --- data AHB port -> DUT AHB XiP slave (SoC: qspi_mem @0x7000_0000) -----
    input  wire [31:0] data_HADDR,
    input  wire [ 1:0] data_HTRANS,
    input  wire        data_HWRITE,
    input  wire [ 2:0] data_HSIZE,
    input  wire [ 2:0] data_HBURST,
    input  wire [31:0] data_HWDATA,
    input  wire        data_HSEL,
    output wire [31:0] data_HRDATA,
    output wire        data_HREADY,       // = DUT HREADYOUT (see reason 2)
    output wire        data_HRESP,

    // HPROT for the XiP port.  Deliberately NOT named `data_hprot`: cocotbext-ahb
    // would then claim it as part of the AHB bus and never drive it.  The CG092
    // cache treats a read as non-cacheable when HPROT[3]==0
    // (p_flash_cache_f0_core.v: `non_cache_aphase = HWRITE | ~HPROT[3]`), so the
    // tests need to set it explicitly.  4'b1000 = cacheable (what a cache-aware
    // master drives); 4'b0011 = privileged/data, non-cacheable (Cortex-M0 ties
    // HPROT[3:2] to 0).  See README.
    input  wire [ 3:0] XIP_HPROT,

    // --- observation only (never drive these from the bench) -----------------
    output wire        QSPI_SCLK,
    output wire        QSPI_nCS,
    output wire [ 3:0] QSPI_IO_o,
    output wire [ 3:0] QSPI_IO_e,
    output wire        IRQ_QSPI_FINISHED,

    // Free-running flash-activity counters.  The cache-hit test takes deltas
    // across a single AHB read: a chip-select assertion means the read went to
    // the flash (a miss / a bypass); zero new assertions means the CG092 served
    // it from its own RAM (a hit).  Counting in RTL rather than in a cocotb
    // background coroutine keeps the measurement free of scheduler races and of
    // cocotb-2.x task-lifetime issues.
    output wire [31:0] NCS_FALL_COUNT,
    output wire [31:0] SCLK_RISE_COUNT
);

    // =========================================================================
    // AHB-Lite single-slave wiring: HREADY IS HREADYOUT.
    // =========================================================================
    wire        cfg_hreadyout;
    wire        xip_hreadyout;
    assign config_HREADY = cfg_hreadyout;
    assign data_HREADY   = xip_hreadyout;

    // =========================================================================
    // APB fabric between the config AHB port and the DUT's APB slave.
    // Mirrors nanosoc.sv's u_qspi_apb_bridge (ADDRWIDTH = QSPI_APB_ADDR_W = 16).
    // =========================================================================
    wire [15:0] PADDR;
    wire [ 2:0] PPROT;
    wire        PSEL;
    wire        PENABLE;
    wire        PWRITE;
    wire [31:0] PWDATA;
    wire [ 3:0] PSTRB;
    wire [31:0] PRDATA;
    wire        PREADY;
    wire        PSLVERR;

    cmsdk_ahb_to_apb #(
        .ADDRWIDTH      (16),
        .REGISTER_RDATA (1),
        .REGISTER_WDATA (0)
    ) u_qspi_apb_bridge (
        .HCLK      (HCLK),
        .HRESETn   (HRESETn),
        .PCLKEN    (1'b1),

        .HSEL      (config_HSEL),
        .HADDR     (config_HADDR[15:0]),
        .HTRANS    (config_HTRANS),
        .HSIZE     (config_HSIZE),
        .HPROT     (4'b0011),        // privileged data — as the CPU drives it
        .HWRITE    (config_HWRITE),
        .HREADY    (cfg_hreadyout),  // <-- reason (2)
        .HWDATA    (config_HWDATA),

        .HREADYOUT (cfg_hreadyout),
        .HRDATA    (config_HRDATA),
        .HRESP     (config_HRESP),

        .PADDR     (PADDR),
        .PENABLE   (PENABLE),
        .PWRITE    (PWRITE),
        .PSTRB     (PSTRB),
        .PPROT     (PPROT),
        .PWDATA    (PWDATA),
        .PSEL      (PSEL),
        .APBACTIVE (),
        .PRDATA    (PRDATA),
        .PREADY    (PREADY),
        .PSLVERR   (PSLVERR)
    );

    // =========================================================================
    // THE DUT — the SoC's wrapper module, instantiated with the SoC's parameters.
    // (nanosoc.sv passes SYS_ADDR_W=32, SYS_DATA_W=32, APB_ADDR_W=16,
    //  APB_DATA_W=32, FLASH_ADDR_W=22, CACHE_SIZE=4096 — the wrapper's defaults.)
    // =========================================================================
    wire [3:0] QSPI_IO_i;

    qspi_flash_ahb #(
        .SYS_ADDR_W   (32),
        .SYS_DATA_W   (32),
        .APB_ADDR_W   (16),
        .APB_DATA_W   (32),
        .FLASH_ADDR_W (22),
        .CACHE_SIZE   (4096)
    ) u_dut (
        .HCLK              (HCLK),
        .HRESETn           (HRESETn),
        .PCLK              (HCLK),
        .PRESETn           (HRESETn),

        // AHB XiP read aperture
        .HSEL              (data_HSEL),
        .HADDR             (data_HADDR),
        .HTRANS            (data_HTRANS),
        .HWRITE            (data_HWRITE),
        .HSIZE             (data_HSIZE),
        .HBURST            (data_HBURST),
        .HPROT             (XIP_HPROT),
        .HWDATA            (data_HWDATA),
        .HMASTLOCK         (1'b0),
        .HREADY            (xip_hreadyout),   // <-- reason (2)
        .HRDATA            (data_HRDATA),
        .HRESP             (data_HRESP),
        .HREADYOUT         (xip_hreadyout),

        // APB config
        .PADDR             (PADDR),
        .PPROT             (PPROT),
        .PSEL              (PSEL),
        .PENABLE           (PENABLE),
        .PWRITE            (PWRITE),
        .PWDATA            (PWDATA),
        .PSTRB             (PSTRB),
        .PRDATA            (PRDATA),
        .PREADY            (PREADY),
        .PSLVERR           (PSLVERR),

        // Split-tristate pads
        .QSPI_SCLK         (QSPI_SCLK),
        .QSPI_nCS          (QSPI_nCS),
        .QSPI_IO_o         (QSPI_IO_o),
        .QSPI_IO_i         (QSPI_IO_i),
        .QSPI_IO_e         (QSPI_IO_e),

        .IRQ_QSPI_FINISHED (IRQ_QSPI_FINISHED)
    );

    // =========================================================================
    // Pad tristate resolution (reason 1).
    // =========================================================================
    wire [3:0] SIO;

    assign SIO[0] = QSPI_IO_e[0] ? QSPI_IO_o[0] : 1'bz;
    assign SIO[1] = QSPI_IO_e[1] ? QSPI_IO_o[1] : 1'bz;
    assign SIO[2] = QSPI_IO_e[2] ? QSPI_IO_o[2] : 1'bz;
    assign SIO[3] = QSPI_IO_e[3] ? QSPI_IO_o[3] : 1'bz;

    assign QSPI_IO_i[0] = SIO[0];
    assign QSPI_IO_i[1] = SIO[1];
    assign QSPI_IO_i[2] = SIO[2];
    assign QSPI_IO_i[3] = SIO[3];

    // =========================================================================
    // The real SST26VF064B VIP (Microchip/SST model, ahb_qspi/verif/VIP).
    // =========================================================================
    sst26vf064b u_flash (
        .SCK (QSPI_SCLK),
        .SIO (SIO),
        .CEb (QSPI_nCS)
    );

    // Timing-only shortenings, identical to ahb_qspi/verif/cocotb/ahb_qspi_cocotb.v.
    // Tws is the one that matters here (flash soft reset); the erase/program ones
    // are kept for parity so this bench can grow a program test without surprises.
    defparam u_flash.I0.Tbe  = 1_000;
    defparam u_flash.I0.Tse  = 1_000;
    defparam u_flash.I0.Tsce = 1_000;
    defparam u_flash.I0.Tpp  = 1_000;
    defparam u_flash.I0.Tws  = 1_000;

    // =========================================================================
    // Flash-activity counters (see port comment).  Deliberately free-running and
    // NOT reset by HRESETn: every measurement is a delta taken across one AHB
    // read, so an absolute origin is irrelevant, and a reset-domain dependency
    // could only introduce a way for the counter to miss traffic.
    // =========================================================================
    reg [31:0] ncs_fall_cnt  = 32'd0;
    reg [31:0] sclk_rise_cnt = 32'd0;

    always @(negedge QSPI_nCS)  ncs_fall_cnt  <= ncs_fall_cnt  + 32'd1;
    always @(posedge QSPI_SCLK) sclk_rise_cnt <= sclk_rise_cnt + 32'd1;

    assign NCS_FALL_COUNT  = ncs_fall_cnt;
    assign SCLK_RISE_COUNT = sclk_rise_cnt;

    // =========================================================================
    // Backdoor preload (reason 4).
    //
    // Pattern is a pure function of the BYTE address, so the golden reference in
    // Python is independent of anything the DUT does:
    //
    //     byte(a) = ((a[7:0] + 8'h11) & 8'hFF) ^ a[15:8]
    //
    // Consecutive bytes differ, so a byte-lane swap, an endianness flip or an
    // off-by-N in the address phase all produce a mismatch rather than a pass.
    //
    // `#2` (not `#0`): the VIP initialises its non-volatile arrays at time 0.
    // The model's own header documents exactly this pattern
    // ("initial #1 $readmemh(<path>.I0.memory, <file>)").
    // =========================================================================
    integer pi;
    reg [31:0] paddr_i;
    reg [ 7:0] pbyte;

    initial begin
        #2;
        for (pi = 0; pi < PRELOAD_LEN; pi = pi + 1) begin
            paddr_i = PRELOAD_BASE + pi;
            pbyte   = (paddr_i[7:0] + 8'h11) ^ paddr_i[15:8];
            u_flash.I0.memory[paddr_i] = pbyte;
        end
        $display("[harness] preloaded %0d bytes into SST26VF064B @ 0x%06x",
                 PRELOAD_LEN, PRELOAD_BASE);
    end

endmodule
