// -----------------------------------------------------------------------------
// micropython_boot_tb.sv — full-SoC boot bench for the NanoSoC MicroPython image.
//
// Instantiates the REAL `nanosoc` top (build_soc/rtl/nanosoc.sv, the exact core
// the FPGA build packages — see collect_filelist.tcl) with its IMEM preloaded
// from the already-built MicroPython word-hex image, and surfaces the UART2 TXD
// pad so a cocotb 8N1 receiver can watch the console.
//
// Boot model (all real RTL, nothing stubbed on the path):
//   * IMEM is the RAM_PRELOAD ROM variant (sl_fpga_rom_word $readmemh) loaded
//     with micropython_word.hex at elaboration.
//   * On reset the Cortex-M0 boots from the stage-0 BOOTROM at 0x0. Stage-0
//     probes QSPI (no chip here -> NO-CHIP), falls back to ADP boot, sets
//     SYSCON.REMAP so IMEM aliases to 0x0, and jumps there.
//   * Execution lands on the MicroPython vector table; Reset_Handler brings up
//     UART2 (BAUDDIV=651) and prints the "MicroPython ..." banner, then the REPL.
//
// Pin disposition mirrors the HW-proven rp_nanosoc_wrapper.sv exactly:
//   * FT1248MODE strap p1_in[7]=1 -> UART2 TXD appears on top-level p1_out[5]
//     (hostio4 routes SYS_P1_OUT_MUX[5] i.e. uart2_txd out in FT1248 mode).
//   * The FT1248/USRT self-drain loopbacks on p1_in[0..3] keep stage-0's boot
//     banner from backing up on an absent host (stage-0 also bounds its waits).
//   * UART2 RXD is p1_in[4]; driven here from the dut_rxd bench port.
//
// UART2 TXD tap: idle-high until firmware sets ALTFUNC and drives P1[5]
//   uart_txd = p1_outen[5] ? p1_out[5] : 1'b1   (same idiom as the wrapper).
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module micropython_boot_tb #(
    // The Makefile passes the ABSOLUTE path (-pvalue, from UPY_IMAGE) so
    // $readmemh resolves regardless of the sim launch cwd; placeholder default.
    parameter IMEM_MEM_FPGA_IMG =
        "firmware/micropython/build/micropython_word.hex",
    parameter IMEM_RAM_ADDR_W = 17,   // 128 KB — the MicroPython image needs it
    parameter DMEM_RAM_ADDR_W = 16    // 64 KB  — SP = 0x18010000 = DMEM top
) (
    input  wire dut_clk,      // 50 MHz system clock (20 ns) driven by cocotb
    input  wire dut_resetn,   // active-low system reset driven by cocotb
    input  wire dut_rxd,      // host -> DUT serial into UART2 RXD (p1_in[4])
    output wire uart_txd      // DUT -> host UART2 TXD (muxed p1_out[5], idle-high)
);

    // -------------------------------------------------------------------------
    // GPIO port 1 — UART2 / FT1248 plumbing, bit-for-bit the rp wrapper pattern.
    // -------------------------------------------------------------------------
    wire [15:0] p1_out;
    wire [15:0] p1_outen;
    wire [15:0] p1_in;

    wire uart_txd_int = p1_outen[5] ? p1_out[5] : 1'b1;
    assign uart_txd   = uart_txd_int;

    assign p1_in[0]     = p1_out[3];    // FT_MISO <= FT_SSN loopback
    assign p1_in[1]     = p1_out[1];    // FT_CLK readback
    assign p1_in[2]     = 1'b0;         // FT_MIOSIO TXE# = 0 ("can accept")
    assign p1_in[3]     = p1_out[3];    // FT_SSN readback
    assign p1_in[4]     = dut_rxd;      // UART2 RXD <= host serial in
    assign p1_in[5]     = uart_txd_int; // UART2 TXD readback loopback
    assign p1_in[6]     = 1'b1;         // reserved
    assign p1_in[7]     = 1'b1;         // FT1248MODE strap (UART2 path active)
    assign p1_in[15:8]  = 8'h00;

    // GPIO port 0 — no board I/O in this bench; pads read as 0, drives open.
    wire [15:0] p0_out;
    wire [15:0] p0_outen;
    wire [15:0] p0_in = 16'h0000;

    // -------------------------------------------------------------------------
    // The real single-core nanosoc. Non-console ports tied off per the proven
    // rp_nanosoc_wrapper.sv disposition (expansion slave inert, DMA streams
    // sunk, SPI/QSPI inputs inert with drives open, scan/test off).
    // -------------------------------------------------------------------------
    nanosoc #(
        .IMEM_MEM_FPGA_IMG (IMEM_MEM_FPGA_IMG),
        .IMEM_RAM_ADDR_W   (IMEM_RAM_ADDR_W),
        .DMEM_RAM_ADDR_W   (DMEM_RAM_ADDR_W)
    ) u_nanosoc (
        .sys_clk         (dut_clk),
        .sys_sysresetn   (dut_resetn),
        .sys_xtalclk_out (),

        .sys_scanenable  (1'b0),
        .sys_testmode    (1'b0),
        .sys_scaninhclk  (1'b0),
        .sys_scanouthclk (),

        .cpu_0_swdi   (1'b0),
        .cpu_0_swclk  (1'b0),
        .cpu_0_swdo   (),
        .cpu_0_swdoen (),

        .p0_in     (p0_in),
        .p0_out    (p0_out),
        .p0_outen  (p0_outen),
        .p1_in     (p1_in),
        .p1_out    (p1_out),
        .p1_outen  (p1_outen),

        .sys_hclk    (),
        .sys_hresetn (),

        // Expansion AHB slave — inert (no transaction ever presented).
        .exp_hsel      (1'b0),
        .exp_haddr     (32'h0000_0000),
        .exp_htrans    (2'b00),
        .exp_hsize     (3'b000),
        .exp_hprot     (4'b0000),
        .exp_hwrite    (1'b0),
        .exp_hready    (1'b1),
        .exp_hwdata    (32'h0000_0000),
        .exp_hburst    (3'b000),
        .exp_hmastlock (1'b0),
        .exp_hreadyout (),
        .exp_hresp     (),
        .exp_hrdata    (),

        // DMA streams DMAC -> Expansion: sink
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

        // DMA streams Expansion -> DMAC: nothing to offer
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

        .exp_irq   (4'b0000),
        .exp_drq   (2'b00),
        .exp_dlast (),

        // PL022 SSP — no external slave
        .spi_sclk  (),
        .spi_ss    (),
        .spi_mosi  (),
        .spi_miso  (1'b0),

        // QSPI flash pads — no device: data-in inert, drives open. Stage-0's
        // JEDEC probe reads all-zeros => NO-CHIP => ADP/IMEM fallback boot.
        .qspi_sclk (),
        .qspi_csn  (),
        .qspi_io_o (),
        .qspi_io_e (),
        .qspi_io_i (4'b0000)
    );

endmodule
