// bind_nanosoc_lcd.sv — attach the AHB-Lite protocol checker onto the DUT-side
// accelerator's slave port, with NO RTL edit (same pattern as
// tests/clcd/bind_clcd.sv).
//
// Bound onto `nanosoc_exp_socket` — the module whose port list is FROZEN by
// `fpga/rp/nanosoc_exp/README.md` §2 ("Do not add, remove or rename ports"), and
// therefore the one module whose signal names this bind can rely on. It is also
// the module the RM wrapper instantiates, so it is the boundary that actually
// faces nanosoc's bus matrix on silicon: exactly where the "never stall the AHB"
// rule (§4) has to hold.
//
// The checker is parameterised at the socket's contract widths (ADDR_W=DATA_W=32
// = nanosoc's SYS_ADDR_W/SYS_DATA_W). ALLOW_ERR=0 pins §3's "drive hresp = 0".
`timescale 1ns / 1ps

bind nanosoc_exp_socket ahb_lite_protocol_checker #(
  .ADDR_W(32),
  .DATA_W(32),
  .MAX_WAIT(16),
  .ALLOW_ERR(1'b0),
  .NAME("nanosoc_exp_socket")
) u_ahb_lite_sva (
  .hclk      (hclk),
  .hresetn   (hresetn),
  .hsel      (hsel),
  .haddr     (haddr),
  .htrans    (htrans),
  .hwrite    (hwrite),
  .hwdata    (hwdata),
  .hready    (hready),
  .hrdata    (hrdata),
  .hreadyout (hreadyout),
  .hresp     (hresp)
);
