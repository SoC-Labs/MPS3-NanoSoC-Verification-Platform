// -----------------------------------------------------------------------------
// socdebug_usrt_control_stub.sv — BENCH-ONLY stub for socdebug_usrt_control.
//
// WHY A STUB (read before trusting this bench):
//   nanosoc_soc_peripheral_apb_ss.v instantiates TWO socdebug_usrt_control
//   blocks (u_apb_usrt_0 / u_apb_usrt_1 = the "UART0"/"UART1" APB slots at
//   0x4000_4000 / 0x4000_5000). Their RTL lives in the `socdebug_tech` repo,
//   which is NOT checked out standalone in this working set — the only copies
//   on disk are build artifacts under imp/ and inside unrelated
//   support-request trees. Pulling RTL from an orphan tree is exactly the
//   provenance trap that has already burned this project once (a hostio4
//   bench was run against an orphan copy and a result flipped), so this bench
//   stubs the module rather than guessing which copy is canonical.
//
// WHY THAT DOES NOT WEAKEN THE PROOF:
//   The signal under test is `uart2_rxd` — the receive line of CMSDK
//   *UART2* (u_apb_uart_2, a real cmsdk_apb_uart from the Arm IP library,
//   compiled unmodified). UART2's RXD is sourced from nanosoc_pin_mux, whose
//   uart0_rxd/uart1_rxd outputs are LEFT UNCONNECTED at its only
//   instantiation (nanosoc_ss_systemctrl.v:174,177). USRT0/USRT1 therefore
//   sit on a different APB slot, drive no pin-mux input, and cannot influence
//   uart2_rxd by any path. Stubbing them removes an unrelated compile
//   dependency, not part of the device under test.
//
//   The stub is APB-inert: PREADY=1 so the APB slave-mux never stalls,
//   PRDATA/PSLVERR=0, all interrupts and stream handshakes tied off benignly.
//   Nothing in this bench addresses 0x4000_4000/0x4000_5000.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module socdebug_usrt_control (
  input  wire        PCLK,
  input  wire        PCLKG,
  input  wire        PRESETn,

  input  wire        PSEL,
  input  wire [ 9:0] PADDR,
  input  wire        PENABLE,
  input  wire        PWRITE,
  input  wire [31:0] PWDATA,

  output wire [31:0] PRDATA,
  output wire        PREADY,
  output wire        PSLVERR,

  input  wire [ 3:0] ECOREVNUM,

  // USRT TXD (block -> stream)
  output wire        TX_VALID_o,
  output wire [ 7:0] TX_DATA8_o,
  input  wire        TX_READY_i,

  // USRT RXD (stream -> block)
  input  wire        RX_VALID_i,
  input  wire [ 7:0] RX_DATA8_i,
  output wire        RX_READY_o,

  output wire        TXINT,
  output wire        RXINT,
  output wire        TXOVRINT,
  output wire        RXOVRINT,
  output wire        UARTINT
);

  // APB-inert: never stall the slave mux, never assert an error.
  assign PRDATA     = 32'h0000_0000;
  assign PREADY     = 1'b1;
  assign PSLVERR    = 1'b0;

  // Stream: never source, always sink (so an upstream driver never hangs).
  assign TX_VALID_o = 1'b0;
  assign TX_DATA8_o = 8'h00;
  assign RX_READY_o = 1'b1;

  // No interrupts.
  assign TXINT      = 1'b0;
  assign RXINT      = 1'b0;
  assign TXOVRINT   = 1'b0;
  assign RXOVRINT   = 1'b0;
  assign UARTINT    = 1'b0;

endmodule
