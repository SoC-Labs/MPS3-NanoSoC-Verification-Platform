// -----------------------------------------------------------------------------
// micropython_flash_boot_tb.sv — TRUE product boot-from-flash co-sim for the
// hybrid-XiP MicroPython build. This is the ASIC-HONEST boot: NO RAM_PRELOAD
// cheat for the application.
//
// Difference from micropython_xip_boot_tb (the RAM_PRELOAD proof):
//   * IMEM STARTS EMPTY. It is a writable preload-BRAM (sl_fpga_rom_word)
//     preloaded with an ALL-ZERO word-hex, so at elaboration IMEM is a
//     deterministic 0 — the hot image is NOT baked in. The stage-0 bootrom
//     must CPU-copy the HOT set out of flash into IMEM before anything runs.
//   * The SST26VF064B flash VIP is backdoored with the FULL PACKED flash image
//     (boot table @0x0 + HOT image @0x1000 + COLD segment @0x20000) from
//     flash_pack.py, at byte offset 0.
//
// Boot sequence proven here (all real RTL):
//   reset -> BOOTROM (reworked stage-0) probes QSPI JEDEC, brings up XiP,
//   bursts the boot table into SRAM (cache-safe), CPU-copies the HOT image from
//   flash 0x1000 into IMEM 0x10000000, CRC32-verifies it, then REMAPs IMEM->0x0
//   and jumps. The hot MicroPython runs; its startup re-brings-up XiP; the COLD
//   half (lexer/parser/compiler/REPL) executes IN PLACE from flash 0x20000.
//
// GATE 1 (boot proof): UART2 emits "NanoSoC MicroPython". Because IMEM started
// empty, reaching the banner PROVES stage-0 copied the hot image out of flash.
// GATE 2 (cold-execute proof, UPY_GATE2=1): print(1+1)->2 round-trips through
// cold-from-flash code.
//
// QSPI wiring + UART2/FT1248 plumbing: identical to micropython_xip_boot_tb.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

// Repo root for the two $readmemh images below; the Makefile passes
// +define+MPFB_REPO_ROOT="<abs repo root>" (string params cannot go via -pvalue).
`ifndef MPFB_REPO_ROOT
  `define MPFB_REPO_ROOT "../.."
`endif

module micropython_flash_boot_tb #(
    // IMEM preload = ALL-ZERO word-hex: IMEM comes up deterministic 0 (empty).
    // The hot image is copied in from flash by stage-0, NOT preloaded here.
    // NOTE: VCS -pvalue SILENTLY IGNORES string params — this DEFAULT is the
    // real path used by the sim (rooted at `MPFB_REPO_ROOT, set by the Makefile).
    parameter IMEM_ZERO_IMG =
        {`MPFB_REPO_ROOT, "/tests/micropython_flash_boot/imem_zero_word.hex"},
    // FULL packed flash image (boot table + HOT + COLD) as a byte-hex $readmemh
    // file, backdoored into the SST26 VIP at byte offset 0. Also a DEFAULT
    // string path (see the -pvalue note above).
    parameter FLASH_IMG =
        {`MPFB_REPO_ROOT, "/firmware/micropython/build/xip/flash_image_byte.hex"},

    // IMEM sized to hold the 40 KB hot set once copied (64 KB). DMEM 64 KB.
    parameter IMEM_RAM_ADDR_W = 16,
    parameter DMEM_RAM_ADDR_W = 16
) (
    input  wire dut_clk,
    input  wire dut_resetn,
    input  wire dut_rxd,
    output wire uart_txd
);

    // --- GPIO P1: UART2/FT1248 plumbing (identical to micropython_xip_boot_tb) -
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

    // --- The real single-core nanosoc, IMEM preloaded EMPTY (all-zero) --------
    // QSPI_FLASH_PRESENT defaults to 1 in build_soc/rtl/nanosoc.sv, so BOOT_CFG
    // .QSPI_PRESENT reads 1 and stage-0 takes the flash path; CORE_ID = 0.
    nanosoc #(
        .IMEM_MEM_FPGA_IMG (IMEM_ZERO_IMG),
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

        // QSPI pads -> the VIP
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

    // --- Backdoor: load the FULL PACKED flash image into the VIP at offset 0.
    // The byte-hex has an @00000000 header, so boot table (@0x0), HOT image
    // (@0x1000) and COLD segment (@0x20000) all land at their true flash byte
    // offsets. The image is contiguous 0xFF-filled, so no address gaps read X.
    initial begin
        #1;
        $readmemh(FLASH_IMG, u_flash.I0.memory);
        $display("[flash-boot] packed flash image loaded into SST26VF064B @ 0x0 (boot table + HOT@0x1000 + COLD@0x20000)");
        $display("[flash-boot] IMEM preloaded EMPTY (all-zero); stage-0 must copy the hot image out of flash");
    end

endmodule
