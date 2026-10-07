// -----------------------------------------------------------------------------
// lan9220_if.sv — AXI-Stream <-> LAN9220 static-memory-bus glue, sitting
// between fpga/ethernet/bridge/'s uplink port and the axi_emc_0 vendor IP
// cell (shell_bd.tcl) that drives the physical LAN9220 pins.
//
// Phase 0 stub (A1). Port list is real; FIFO push/pop + descriptor sequencing
// is `// TODO` (largely lives in firmware/smsc911x/ once real, per README).
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module lan9220_if (
  input  logic       clk_i,
  input  logic       rst_i,

  // ---------------------------------------------------------------------
  // AXI-Stream side — toward fpga/ethernet/bridge/'s uplink port.
  // ---------------------------------------------------------------------
  input  logic [7:0] s_axis_tdata,   // frames toward LAN9220 (shell -> network)
  input  logic       s_axis_tvalid,
  output logic       s_axis_tready,
  input  logic       s_axis_tlast,

  output logic [7:0] m_axis_tdata,   // frames from LAN9220 (network -> shell)
  output logic       m_axis_tvalid,
  input  logic       m_axis_tready,
  output logic       m_axis_tlast,

  // ---------------------------------------------------------------------
  // Static-memory-bus control side — toward axi_emc_0 (Xilinx AXI EMC vendor
  // cell). This module speaks whatever narrow AXI4-Lite-ish control/status
  // handshake axi_emc_0 exposes above the raw LAN9220 pins; TODO(A1): pin
  // this down once axi_emc_0's exact configuration (16-bit async SRAM
  // timing matching the LAN9220 SMC interface) is set in shell_bd.tcl.
  // ---------------------------------------------------------------------
  input  logic       eth_int_i        // LAN9220 interrupt (partition-pins.md
                                       // has no analog for this — it's a
                                       // board-level signal, not a DUT one;
                                       // see fpga/shell/bd/shell_bd.tcl
                                       // Section 4).
);

  // TODO(A1): drive/respond to axi_emc_0's memory-mapped interface to
  // push/pop the LAN9220's TX/RX FIFOs and service eth_int_i (link/FIFO
  // status). The actual register sequencing (per the ported Zephyr
  // eth_smsc911x.c driver — see README) is expected to live mostly in
  // firmware/smsc911x/, with this RTL doing only the AXI-Stream <-> local
  // buffering/handshake, not the full LAN9220 register protocol.

  assign s_axis_tready = 1'b0;
  assign m_axis_tdata   = '0;
  assign m_axis_tvalid  = 1'b0;
  assign m_axis_tlast   = 1'b0;

endmodule
