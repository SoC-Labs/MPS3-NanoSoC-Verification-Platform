// -----------------------------------------------------------------------------
// rp_nanosoc_upy_wrapper.sv — SCAFFOLD RM: NanoSoC with enough BRAM to host a
// MicroPython interpreter, for a live REPL demo inside the MPS3 harness.
//
// =============================================================================
// THIS IS A SCAFFOLD. IT IS NOT THE PRODUCT. READ THIS BEFORE REUSING IT.
// =============================================================================
// It builds the DUT with 128 KB IMEM and 64 KB DMEM, which the FPGA can give
// away almost for free (the RP pblock has 144 BRAM36 tiles and the stock
// nanosoc RM uses 16.5). Silicon cannot. A 128 KB SRAM macro is a real, large
// piece of an ASIC, so a fat-BRAM build makes FPGA results non-representative
// of the chip and quietly forks the test suite — exactly what the one-config
// doctrine exists to prevent.
//
// The PRODUCT answer for code that does not fit in 16 KB is XiP from external
// QSPI flash (qspi_mem @ 0x7000_0000), which keeps IMEM at 16 KB on every
// target — simulation, FPGA and ASIC. That work is done at the SoC level and
// proven in tests/qspi_xip; it reaches the harness when the QSPI pins join the
// partition boundary, which requires a shell rebuild and a static_id re-key and
// is therefore BATCHED with the other pending boundary riders.
//
// This RM exists so there is a working demo in the harness BEFORE that batch
// lands. Everything above the memory sizing — the MicroPython port, the UART
// console, the GPIO/register demo — carries over to the product RM unchanged;
// only the linker script moves code from IMEM to the XiP aperture.
//
// Do not promote this RM to the product. Do not quote its resource numbers as
// the SoC's. Retire it once the QSPI RM boots.
//
// =============================================================================
// BOUNDARY: THIS RM RIDES THE QSPI XiP SHELL REBUILD (v0.2)
// =============================================================================
// CORRECTED 2026-07-15 — this block used to claim "costs nothing to add: the
// IDENTICAL v0.1 boundary (30 signals), static_id NOT re-minted". That is NO
// LONGER TRUE. The +14-pin Flash/QSPI XiP crossing (9a259f9, RTL + contract)
// added five signals to the partition boundary — qspi_sclk, qspi_csn,
// qspi_io_o[4], qspi_io_oe[4], qspi_io_i[4] (see the port list below, ~:118) —
// so this wrapper now carries the partition-pins.md v0.2 boundary of 35 signals
// (was 30), NGPIO=16. Consequently:
//   * the static shell IS rebuilt (541a513 routed the crossing through it),
//   * `static_id` WILL be RE-MINTED away from 0xE4B1C44A on that rebuild,
//   * every existing overlay MUST be re-keyed to the new static_id in the SAME
//     rebuild — this RM (rm_nanosoc_upy) included,
//   * `make check` stage 2/8 (pin_check) now checks the v0.2 boundary.
// This RM does not trigger the rebuild — the QSPI crossing does — but it can no
// longer be dropped in against the then-fielded 0xE4B1C44A static; it is one of the
// RMs the single QSPI/KVM boundary rebuild re-keys.
//
// The DUT itself is the real `rp_nanosoc_wrapper` — instantiated, not forked —
// with two memory-size generics overridden and a different baked IMEM image.
// One source of truth for the boundary and the tie-offs.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module rp_nanosoc_upy_wrapper #(
  // Partition-pin GPIO width — must match the contract (and rp_dut_stub).
  parameter int NGPIO = 16,

  // Shell-generated dut_clk, and the console baud the shell's uart_axis_shim
  // is built for. These MUST match the shell or the console is garbage.
  parameter        UART_CLK_HZ = 50_000_000,
  parameter        UART_BAUD   = 76800,

  // ---------------------------------------------------------------------------
  // SCAFFOLD MEMORY SIZING (byte-address widths; phys = 2**N)
  //
  //   IMEM 17 => 128 KB   — holds the MicroPython interpreter
  //   DMEM 16 =>  64 KB   — .bss/stack + a comfortable gc heap
  //
  // Product values are 14/14 (16 KB each) — see rp_nanosoc_wrapper's parameter
  // block. BRAM cost of the fat build is roughly 32 + 16 = ~48 RAMB36 of the
  // pblock's 144, on top of the stock RM's 16.5. Comfortable, and irrelevant to
  // silicon, which is the whole point of the warning above.
  // ---------------------------------------------------------------------------
  parameter        IMEM_RAM_ADDR_W = 17,
  parameter        DMEM_RAM_ADDR_W = 16,

  // The baked MicroPython image (word-format $readmemh, via the RAM_PRELOAD ROM
  // variant of nanosoc_region_imem). ooc_synth.tcl always passes the ABSOLUTE
  // path as a generic ($UPY_IMEM_IMG, else <repo>/firmware/micropython/build/
  // micropython_word.hex) so $readmemh resolves whatever the Vivado launch cwd
  // is; this plain-filename default is only a fallback.
  parameter        IMEM_MEM_FPGA_IMG = "micropython_word.hex"
) (
  // ===========================================================================
  // partition-pins.md v0.1 boundary — IDENTICAL to rp_nanosoc_wrapper and
  // rp_dut_stub. 30 signals. Adding or removing one here re-mints static_id and
  // invalidates every shipped overlay, so: don't.
  // ===========================================================================
  input  logic        dut_clk,
  input  logic        dut_resetn,
  input  logic        rp_resetn,
  input  logic        dbg_resetn,

  input  logic        jtag_tck,
  input  logic        jtag_tms,
  input  logic        jtag_tdi,
  output logic        jtag_tdo,

  input  logic        dbg_bscan_bscanid_en,
  input  logic        dbg_bscan_capture,
  input  logic        dbg_bscan_drck,
  input  logic        dbg_bscan_reset,
  input  logic        dbg_bscan_runtest,
  input  logic        dbg_bscan_sel,
  input  logic        dbg_bscan_shift,
  input  logic        dbg_bscan_tck,
  input  logic        dbg_bscan_tdi,
  input  logic        dbg_bscan_tms,
  input  logic        dbg_bscan_update,
  output logic        dbg_bscan_tdo,

  input  logic        phy_rmii_ref_clk,
  input  logic        phy_rmii_crs_dv,
  input  logic [1:0]  phy_rmii_rxd,
  output logic [1:0]  phy_rmii_txd,
  output logic        phy_rmii_tx_en,
  output logic        mdc,
  output logic        mdio_o,
  output logic        mdio_oe,
  input  logic        mdio_i,

  output logic [7:0]  uart_tx_tdata,
  output logic        uart_tx_tvalid,
  input  logic        uart_tx_tready,
  input  logic [7:0]  uart_rx_tdata,
  input  logic        uart_rx_tvalid,
  output logic        uart_rx_tready,
  output logic        swo,

  output logic [31:0] rm_id,
  output logic        dut_lockup,
  output logic        irq_out,

  output logic [NGPIO-1:0] dut_gpio_o,
  output logic [NGPIO-1:0] dut_gpio_oe,
  input  logic [NGPIO-1:0] dut_gpio_i,

  // Flash / QSPI XiP boundary (partition-pins.md v0.2) — passed straight
  // through to the inner rp_nanosoc_wrapper, which wires them to nanosoc's
  // QSPI controller. (This scaffold RM boots MicroPython from fat BRAM; the
  // product answer for >16 KB code is XiP over these very pins.)
  output logic       qspi_sclk,
  output logic       qspi_csn,
  output logic [3:0] qspi_io_o,
  output logic [3:0] qspi_io_oe,
  input  logic [3:0] qspi_io_i
);

  // ===========================================================================
  // The real nanosoc RM, instantiated (not forked) with the scaffold memory
  // sizes and the MicroPython image. Every boundary signal passes straight
  // through, so the two RMs cannot drift apart at the contract.
  //
  // NOTE `rm_id` is driven by rp_nanosoc_wrapper from its own localparam, which
  // encodes the STOCK nanosoc RM's design_id. It is overridden below so the
  // shell's telemetry reports the RM that is actually loaded — see rm_list.tcl
  // for the id allocation.
  // ===========================================================================
  logic [31:0] inner_rm_id;

  rp_nanosoc_wrapper #(
    .NGPIO             (NGPIO),
    .UART_CLK_HZ       (UART_CLK_HZ),
    .UART_BAUD         (UART_BAUD),
    .IMEM_MEM_FPGA_IMG (IMEM_MEM_FPGA_IMG),
    .IMEM_RAM_ADDR_W   (IMEM_RAM_ADDR_W),
    .DMEM_RAM_ADDR_W   (DMEM_RAM_ADDR_W)
  ) u_dut (
    .dut_clk          (dut_clk),
    .dut_resetn       (dut_resetn),
    .rp_resetn        (rp_resetn),
    .dbg_resetn       (dbg_resetn),

    // JTAG partition boundary — passed straight through to the inner
    // rp_nanosoc_wrapper, which binds them to nanosoc's SoC-400 SWJ-DP
    // (dap_swclktck/swditms/tdi/tdo). The upy DUT therefore DOES expose the
    // SWJ-DP transitively through the shared inner wrapper — no separate tie
    // needed. (rm_nanosoc_upy is excluded from this mint on a bootrom-OOC
    // block, but the boundary rename is a pure pass-through and safe now.)
    .jtag_tck         (jtag_tck),
    .jtag_tms         (jtag_tms),
    .jtag_tdi         (jtag_tdi),
    .jtag_tdo         (jtag_tdo),

    .dbg_bscan_bscanid_en (dbg_bscan_bscanid_en),
    .dbg_bscan_capture (dbg_bscan_capture),
    .dbg_bscan_drck   (dbg_bscan_drck),
    .dbg_bscan_reset  (dbg_bscan_reset),
    .dbg_bscan_runtest (dbg_bscan_runtest),
    .dbg_bscan_sel    (dbg_bscan_sel),
    .dbg_bscan_shift  (dbg_bscan_shift),
    .dbg_bscan_tck    (dbg_bscan_tck),
    .dbg_bscan_tdi    (dbg_bscan_tdi),
    .dbg_bscan_tms    (dbg_bscan_tms),
    .dbg_bscan_update (dbg_bscan_update),
    .dbg_bscan_tdo    (dbg_bscan_tdo),

    .phy_rmii_ref_clk (phy_rmii_ref_clk),
    .phy_rmii_crs_dv  (phy_rmii_crs_dv),
    .phy_rmii_rxd     (phy_rmii_rxd),
    .phy_rmii_txd     (phy_rmii_txd),
    .phy_rmii_tx_en   (phy_rmii_tx_en),
    .mdc              (mdc),
    .mdio_o           (mdio_o),
    .mdio_oe          (mdio_oe),
    .mdio_i           (mdio_i),

    .uart_tx_tdata    (uart_tx_tdata),
    .uart_tx_tvalid   (uart_tx_tvalid),
    .uart_tx_tready   (uart_tx_tready),
    .uart_rx_tdata    (uart_rx_tdata),
    .uart_rx_tvalid   (uart_rx_tvalid),
    .uart_rx_tready   (uart_rx_tready),
    .swo              (swo),

    .rm_id            (inner_rm_id),
    .dut_lockup       (dut_lockup),
    .irq_out          (irq_out),

    .dut_gpio_o       (dut_gpio_o),
    .dut_gpio_oe      (dut_gpio_oe),
    .dut_gpio_i       (dut_gpio_i),

    .qspi_sclk        (qspi_sclk),
    .qspi_csn         (qspi_csn),
    .qspi_io_o        (qspi_io_o),
    .qspi_io_oe       (qspi_io_oe),
    .qspi_io_i        (qspi_io_i)
  );

  // rm_id encoding v2: {version[15:0], design_id[15:0]}. design_id 0x0005 =
  // nanosoc_upy. NOTE 0x0004 is TAKEN by rm_uart_echo (the re-numbered "ECHO"),
  // so this is 0x0005; version 1.0.0 => high half 0x0100.
  // Named localparam + `assign rm_id = <NAME>;` is the form check_rm_id_encoding
  // follows and check_rm_id_literals cross-checks against rm_list.tcl -- a bare
  // literal here fails the `make check` rm_id-encoding stage. Keep it in sync
  // with RM_LIB(rm_nanosoc_upy,design_id/version).
  localparam logic [31:0] RM_ID_NANOSOC_UPY = 32'h0100_0005;
  assign rm_id = RM_ID_NANOSOC_UPY;

  // The inner wrapper drives its own (stock-nanosoc) rm_id, which we override
  // above. Sink it explicitly so the intent is clear and lint stays quiet.
  // verilator lint_off UNUSED
  wire [31:0] unused_inner_rm_id = inner_rm_id;
  // verilator lint_on UNUSED

endmodule
