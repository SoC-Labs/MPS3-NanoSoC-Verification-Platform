// -----------------------------------------------------------------------------
// micropython_xip_boot_tb.sv — full-SoC boot bench for the HYBRID XiP MicroPython
// build: a small HOT set resident in IMEM, the COLD set executing IN PLACE from
// a real SST26VF064B flash VIP through the CG092 cache.
//
// This is the PRODUCT-config proof. The scaffold bench (tests/micropython_boot)
// bakes the whole 65 KB interpreter into a fat 128 KB BRAM IMEM — an FPGA-only
// cheat silicon cannot reproduce. Here IMEM holds ONLY the hot set (sized to the
// build's real hot number), and the cold half — parser, compiler, REPL, builtin
// modules, const tables — lives in flash and is fetched on demand. If the REPL
// evaluates Python, cold-code-execute-from-flash works, because the parser and
// compiler ARE cold.
//
// Boot model (all real RTL, nothing stubbed on the datapath):
//   * IMEM = RAM_PRELOAD ROM (sl_fpga_rom_word $readmemh) loaded with the HOT
//     word-hex at elaboration, sized IMEM_RAM_ADDR_W.
//   * The SST26 VIP holds the COLD segment at flash byte offset COLD_FLASH_OFF
//     (== aperture 0x70000000 + COLD_FLASH_OFF). The low boot-table region is
//     left ERASED (0xFF), so stage-0's QSPI probe finds a chip, reads a bad
//     boot-table magic, and cleanly falls back to the ADP/REMAP-to-IMEM path —
//     i.e. the hot set runs, exactly as the product's stage-0 hands off to a
//     resident hot image. (Stage-0 hybrid-boot rework is a separate item; this
//     bench does not depend on it.)
//   * MicroPython's own startup calls nanosoc_xip_bringup() BEFORE any cold
//     symbol is touched: program AHB_SPI_SETUP=0x800B, XIP_ACTIVE, enable+wait
//     the CG092 cache. From then on a fetch to 0x70xxxxxx is a cached flash read.
//
// QSPI wiring: the SoC presents split-tristate pads (qspi_io_o/i/e); the VIP has
// a bidirectional SIO[3:0]. Resolve per lane, same as tests/qspi_xip.
//
// UART2 TXD tap + FT1248 plumbing: bit-for-bit tests/micropython_boot.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module micropython_xip_boot_tb #(
    // HOT (IMEM-resident) image, word-hex, linked at 0x0. The Makefile passes
    // the absolute path (-pvalue); this repo-relative default is a placeholder.
    parameter HOT_IMEM_IMG =
        "firmware/micropython/build/xip/micropython_hot_word.hex",
    // COLD (flash XiP) image, byte-hex ($readmemh into the VIP array).
    parameter COLD_FLASH_IMG =
        "firmware/micropython/build/xip/firmware_cold_byte.hex",
    // Flash byte offset the cold segment is linked at (aperture 0x70000000+OFF).
    // Filled from the build's __xip_start__ (default = the multicore's 0x20000
    // boot-table reservation; the firmware manifest is authoritative).
    parameter int COLD_FLASH_OFF = 32'h0002_0000,

    // IMEM sized to the HOT set (byte-addr width; phys = 2**N). Filled from the
    // build's manifest. DMEM holds .data/.bss/stack + the gc heap.
    parameter IMEM_RAM_ADDR_W = 15,   // 32 KB default — the product-realistic size
    parameter DMEM_RAM_ADDR_W = 16    // 64 KB
) (
    input  wire dut_clk,
    input  wire dut_resetn,
    input  wire dut_rxd,
    output wire uart_txd
);

    // --- GPIO P1: UART2/FT1248 plumbing (identical to micropython_boot_tb) ----
    wire [15:0] p1_out, p1_outen, p1_in;
    wire uart_txd_int = p1_outen[5] ? p1_out[5] : 1'b1;
    assign uart_txd = uart_txd_int;

    assign p1_in[0]    = p1_out[3];
    assign p1_in[1]    = p1_out[1];
    assign p1_in[2]    = 1'b0;
    assign p1_in[3]    = p1_out[3];
    assign p1_in[4]    = dut_rxd;
    assign p1_in[5]    = uart_txd_int;
    assign p1_in[6]    = 1'b1;
    assign p1_in[7]    = 1'b1;         // FT1248MODE strap
    assign p1_in[15:8] = 8'h00;

    wire [15:0] p0_out, p0_outen;
    wire [15:0] p0_in = 16'h0000;

    // --- QSPI split-tristate <-> VIP bidirectional SIO (per tests/qspi_xip) ---
    wire        qspi_sclk;
    wire        qspi_csn;
    wire [3:0]  qspi_io_o, qspi_io_e;
    wire [3:0]  qspi_io_i;
    wire [3:0]  SIO;

    assign SIO[0] = qspi_io_e[0] ? qspi_io_o[0] : 1'bz;
    assign SIO[1] = qspi_io_e[1] ? qspi_io_o[1] : 1'bz;
    assign SIO[2] = qspi_io_e[2] ? qspi_io_o[2] : 1'bz;
    assign SIO[3] = qspi_io_e[3] ? qspi_io_o[3] : 1'bz;
    assign qspi_io_i[0] = SIO[0];
    assign qspi_io_i[1] = SIO[1];
    assign qspi_io_i[2] = SIO[2];
    assign qspi_io_i[3] = SIO[3];

    // --- The real single-core nanosoc (hot set in IMEM) ----------------------
    nanosoc #(
        .IMEM_MEM_FPGA_IMG (HOT_IMEM_IMG),
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
        .exp_str_in_0_tvalid (), .exp_str_in_0_tready (1'b1),
        .exp_str_in_0_tdata  (), .exp_str_in_0_tstrb  (), .exp_str_in_0_tlast (),
        .exp_str_in_1_tvalid (), .exp_str_in_1_tready (1'b1),
        .exp_str_in_1_tdata  (), .exp_str_in_1_tstrb  (), .exp_str_in_1_tlast (),
        .exp_str_in_2_tvalid (), .exp_str_in_2_tready (1'b1),
        .exp_str_in_2_tdata  (), .exp_str_in_2_tstrb  (), .exp_str_in_2_tlast (),
        .exp_str_out_0_tvalid (1'b0), .exp_str_out_0_tready (),
        .exp_str_out_0_tdata (32'h0), .exp_str_out_0_tstrb (4'b0),
        .exp_str_out_0_tlast (1'b0),  .exp_str_out_0_flush (),
        .exp_str_out_1_tvalid (1'b0), .exp_str_out_1_tready (),
        .exp_str_out_1_tdata (32'h0), .exp_str_out_1_tstrb (4'b0),
        .exp_str_out_1_tlast (1'b0),  .exp_str_out_1_flush (),
        .exp_str_out_2_tvalid (1'b0), .exp_str_out_2_tready (),
        .exp_str_out_2_tdata (32'h0), .exp_str_out_2_tstrb (4'b0),
        .exp_str_out_2_tlast (1'b0),  .exp_str_out_2_flush (),
        .exp_irq   (4'b0000),
        .exp_drq   (2'b00),
        .exp_dlast (),
        .spi_sclk  (),
        .spi_ss    (),
        .spi_mosi  (),
        .spi_miso  (1'b0),

        // QSPI pads -> the VIP (NOT tied off — this is the point of this bench)
        .qspi_sclk (qspi_sclk),
        .qspi_csn  (qspi_csn),
        .qspi_io_o (qspi_io_o),
        .qspi_io_e (qspi_io_e),
        .qspi_io_i (qspi_io_i)
    );

    // --- The real Microchip/SST SST26VF064B behavioural VIP -------------------
    sst26vf064b u_flash (
        .SCK (qspi_sclk),
        .SIO (SIO),
        .CEb (qspi_csn)
    );
    // Shorten erase/program timing (timing-only; same as tests/qspi_xip).
    defparam u_flash.I0.Tbe  = 1_000;
    defparam u_flash.I0.Tse  = 1_000;
    defparam u_flash.I0.Tsce = 1_000;
    defparam u_flash.I0.Tpp  = 1_000;
    defparam u_flash.I0.Tws  = 1_000;

    // --- Backdoor: load the COLD segment into the VIP array at its flash offset.
    // The boot-table region below COLD_FLASH_OFF is left at the VIP's erased
    // default (0xFF) so stage-0 reads a bad magic and falls back to IMEM boot.
    // $readmemh with a start address deposits the byte image at COLD_FLASH_OFF.
    initial begin
        #1;
        $readmemh(COLD_FLASH_IMG, u_flash.I0.memory, COLD_FLASH_OFF);
        $display("[xip-boot] cold segment loaded into SST26VF064B @ flash 0x%06x (aperture 0x%08x)",
                 COLD_FLASH_OFF, 32'h7000_0000 + COLD_FLASH_OFF);
    end

endmodule
