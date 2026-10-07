// -----------------------------------------------------------------------------
// rp_shell_top.sv — MINIMAL REAL static shell for the KU115 DFX proof.
//
// Purpose: prove the KU115 DFX machinery (greybox<->LED pr_verify + partial +
// clearing bitstreams) end to end on the real part, the analog of the proven
// Z2 DFX result — WITHOUT needing the full networked shell (MicroBlaze/lwIP/
// LAN9220/CSRs) or the nanosoc DUT. This is deliberately the smallest static
// that is (a) a legal, routable design and (b) actually exercises the ENTIRE
// RP boundary (partition-pins.md v0.1), so pr_verify and partial generation
// are meaningful.
//
// What it does: buffers a board oscillator into dut_clk, synchronizes the
// push-button into the 3 partition resets, instantiates the RP cell (rp_dut,
// filled by rm_greybox or rm_led at DFX link time), drives every shell->RP
// input to a safe constant, and folds every RP->shell output into an LED
// register so nothing is optimised away. LED[3:0] mirror the RM's blink GPIO
// (static with greybox, blinking with rm_led — the human-visible proof);
// LED[7:4] carry a parity fold of all other RP outputs (keeps them live).
//
// NOTE: the production shell generates dut_clk via a DRP MMCM (spec §5); here a
// plain BUFG off OSCCLK[1] is used — sufficient for the DFX proof, which is
// about the reconfiguration mechanics, not clock management.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module rp_shell_top (
  input  logic        OSCCLK1,        // board oscillator (AK16, 50 MHz)
  input  logic        USER_nPB0,      // push-button, active-low (AT30) — reset
  output logic [7:0]  USER_nLED       // active-low LEDs (AU32..AR30)
);
  // --- clock ----------------------------------------------------------------
  logic dut_clk;
  BUFG u_clk_bufg (.I(OSCCLK1), .O(dut_clk));  // IBUF auto-inserted at the port

  // --- reset synchronizer (async assert, sync deassert) ---------------------
  logic       nrst_meta, resetn;
  wire        arst_n = USER_nPB0;      // active-low button
  always_ff @(posedge dut_clk or negedge arst_n) begin
    if (!arst_n) {resetn, nrst_meta} <= 2'b00;
    else         {resetn, nrst_meta} <= {nrst_meta, 1'b1};
  end

  // --- RP boundary nets (RP -> shell outputs observed below) ----------------
  logic        w_jtag_tdo;
  logic [1:0]  w_txd;
  logic        w_txen, w_mdc, w_mdio_o, w_mdio_oe;
  logic [7:0]  w_utx_d;
  logic        w_utx_v, w_urx_r, w_swo;
  logic [31:0] w_rm_id;
  logic        w_lockup, w_irq;
  logic [15:0] w_gpio_o, w_gpio_oe;

  // --- the reconfigurable partition -----------------------------------------
  rp_dut u_rp_dut (
    .dut_clk          (dut_clk),
    .dut_resetn       (resetn),
    .rp_resetn        (resetn),
    .dbg_resetn       (resetn),

    .jtag_tck         (1'b0),
    .jtag_tms         (1'b0),
    .jtag_tdi         (1'b0),
    .jtag_tdo         (w_jtag_tdo),

    .phy_rmii_ref_clk (1'b0),
    .phy_rmii_crs_dv  (1'b0),
    .phy_rmii_rxd     (2'b00),
    .phy_rmii_txd     (w_txd),
    .phy_rmii_tx_en   (w_txen),
    .mdc              (w_mdc),
    .mdio_o           (w_mdio_o),
    .mdio_oe          (w_mdio_oe),
    .mdio_i           (1'b0),

    .uart_tx_tdata    (w_utx_d),
    .uart_tx_tvalid   (w_utx_v),
    .uart_tx_tready   (1'b1),
    .uart_rx_tdata    (8'h00),
    .uart_rx_tvalid   (1'b0),
    .uart_rx_tready   (w_urx_r),
    .swo              (w_swo),

    .rm_id            (w_rm_id),
    .dut_lockup       (w_lockup),
    .irq_out          (w_irq),

    .dut_gpio_o       (w_gpio_o),
    .dut_gpio_oe      (w_gpio_oe),
    .dut_gpio_i       (16'h0000)
  );

  // --- observation: fold every RP output so none is trimmed -----------------
  wire fold = (^w_rm_id) ^ (|w_txd) ^ w_txen ^ w_mdc ^ w_mdio_o ^ w_mdio_oe
            ^ (^w_utx_d) ^ w_utx_v ^ w_urx_r ^ w_swo ^ w_lockup ^ w_irq
            ^ w_jtag_tdo ^ (^w_gpio_oe);

  logic [7:0] led_r;
  always_ff @(posedge dut_clk or negedge resetn) begin
    if (!resetn) led_r <= 8'h00;
    else         led_r <= { {4{fold}}, w_gpio_o[3:0] };  // [3:0]=RM blink, [7:4]=fold
  end

  assign USER_nLED = ~led_r;  // LEDs are active-low
endmodule
