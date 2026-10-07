// -----------------------------------------------------------------------------
// uart_axis_shim.sv — bit-serial UART <-> AXI-Stream byte shim.
//
// nanosoc's console is CMSDK UART2, a raw serial peripheral muxed onto GPIO
// port P1 (p1_out[5] = TXD, p1_in[4] = RXD) — see
// docs/nanosoc_m0_soc/PLATFORM_MAPPING.md "Console / trace". The DFX
// partition-pins.md v0.1 contract's console group is AXI-Stream byte
// (`uart_tx_tdata[7:0]`/`tvalid`/`tready`, `uart_rx_tdata[7:0]`/`tvalid`/
// `tready`). This module bridges the two: a standard fixed-divisor 8N1 UART
// receiver deserializes the DUT's TXD line into AXIS bytes on the
// `uart_tx_*` (DUT->host) side, and a matching transmitter serializes AXIS
// bytes from `uart_rx_*` (host->DUT) onto the DUT's RXD line.
//
// This is a from-scratch, bring-up-grade core, not a datasheet UART: no
// parity, fixed 8N1 framing, no hardware flow control, no framing-error
// reporting (a bad stop bit is accepted anyway — see RX_STOP below). Its
// job is a synthesizable, contract-correct AXIS handshake, not baud-accurate
// silicon-matched timing; see the CLK_HZ/BAUD note below.
//
// CLK_HZ / BAUD assumption (READ BEFORE INSTANTIATING):
//   partition-pins.md "Clock/reset domain rule" — dut_clk is generated
//   *in the static shell* (DRP MMCM); this module has no way to know that
//   frequency on its own, so CLK_HZ must be overridden at instantiation to
//   match whatever the shell's MMCM actually produces on dut_clk. The
//   default below (25 MHz) is picked because it is the one FPGA-proven
//   nanosoc operating point in this codebase (pynq_z2_build.md,
//   nanosoc_vivado_wrapper.v `sys_fclk`) — NOT because any dut_clk
//   frequency has actually been chosen for the MPS3/DFX shell yet
//   (docs/nanosoc_m0_soc/PLATFORM_MAPPING.md §"Gaps/decisions for A6" item
//   8 flags this as still open: 100 MHz nanosoc default vs. 25 MHz proven
//   vs. 50 MHz MPS3 OSCCLK). BAUD defaults to 115200, a conventional console
//   rate — it is not derived from any nanosoc firmware's actual UART2
//   divider setting, which is a firmware-side concern this shim does not
//   need to match exactly for the AXIS handshake to be correct.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module uart_axis_shim #(
  parameter int CLK_HZ = 25_000_000,  // MUST match the shell's actual dut_clk
                                       // frequency — see header note above.
  parameter int BAUD   = 115200       // Console baud rate (8N1). Bring-up
                                       // default, not firmware-derived.
) (
  input  logic clk,     // = dut_clk (same domain as the nanosoc instance)
  input  logic resetn,  // = the RM's combined reset (already synchronized
                         // for deassert by the static shell — no re-sync
                         // performed here, matching the contract's
                         // "Clock/reset domain rule").

  // ---------------------------------------------------------------------
  // Serial side — wired directly to nanosoc's raw CMSDK UART2 GPIO pins
  // (P1[5]=TXD out of the DUT, P1[4]=RXD into the DUT) inside
  // rp_nanosoc_wrapper.sv. Async pad input is 2-FF synchronized internally
  // below before use.
  // ---------------------------------------------------------------------
  input  logic dut_txd_i,   // <= nanosoc p1_out[5]  (DUT is transmitting)
  output logic dut_rxd_o,   // => nanosoc p1_in[4]   (this shim is transmitting)

  // ---------------------------------------------------------------------
  // AXI-Stream side — partition-pins.md v0.1 "Console / trace" group.
  // uart_tx_* : DUT -> host console bytes (this shim's UART *receiver*).
  // uart_rx_* : host -> DUT console bytes (this shim's UART *transmitter*).
  // ---------------------------------------------------------------------
  output logic [7:0] uart_tx_tdata,
  output logic       uart_tx_tvalid,
  input  logic       uart_tx_tready,

  input  logic [7:0] uart_rx_tdata,
  input  logic       uart_rx_tvalid,
  output logic       uart_rx_tready
);

  // ---------------------------------------------------------------------
  // Baud generator constants
  // ---------------------------------------------------------------------
  localparam int DIV     = (CLK_HZ / BAUD);              // clk cycles / bit
  localparam int HALFDIV = (DIV / 2);                    // clk cycles / half-bit
  localparam int CNT_W   = (DIV <= 1) ? 1 : $clog2(DIV);

  localparam logic [CNT_W-1:0] DIV_M1 = (CNT_W)'(DIV - 1);
  localparam logic [CNT_W-1:0] HALF_C = (CNT_W)'(HALFDIV);

  // =======================================================================
  // Receiver: dut_txd_i (serial, 8N1) -> uart_tx_* (AXIS)
  // =======================================================================
  typedef enum logic [1:0] {RX_IDLE, RX_START, RX_DATA, RX_STOP} rx_state_e;
  rx_state_e        rx_state;
  logic [CNT_W-1:0] rx_cnt;
  logic [2:0]       rx_bit_idx;
  logic [7:0]       rx_shift;
  logic [7:0]       rx_byte;
  logic             rx_valid;

  // 2-FF synchronizer for the async serial pad input.
  logic dut_txd_q, dut_txd_q2;
  always_ff @(posedge clk or negedge resetn) begin
    if (!resetn) begin
      dut_txd_q  <= 1'b1;
      dut_txd_q2 <= 1'b1;
    end else begin
      dut_txd_q  <= dut_txd_i;
      dut_txd_q2 <= dut_txd_q;
    end
  end

  always_ff @(posedge clk or negedge resetn) begin
    if (!resetn) begin
      rx_state   <= RX_IDLE;
      rx_cnt     <= '0;
      rx_bit_idx <= '0;
      rx_shift   <= '0;
      rx_byte    <= '0;
      rx_valid   <= 1'b0;
    end else begin
      // Clear rx_valid once the AXIS consumer accepts the byte. Written
      // before the state-machine block below so a same-cycle new byte
      // (RX_STOP -> RX_IDLE) can set rx_valid again without being clobbered.
      if (rx_valid && uart_tx_tready) begin
        rx_valid <= 1'b0;
      end

      case (rx_state)
        RX_IDLE: begin
          rx_cnt <= '0;
          if (!dut_txd_q2) begin                  // falling edge = start bit
            rx_state <= RX_START;
          end
        end

        RX_START: begin
          // Sample mid-start-bit so a runt glitch doesn't start a phantom
          // byte.
          if (rx_cnt == HALF_C) begin
            rx_cnt <= '0;
            if (!dut_txd_q2) begin
              rx_bit_idx <= '0;
              rx_state   <= RX_DATA;
            end else begin
              rx_state <= RX_IDLE;                // false start, line rose back
            end
          end else begin
            rx_cnt <= rx_cnt + 1'b1;
          end
        end

        RX_DATA: begin
          if (rx_cnt == DIV_M1) begin
            rx_cnt                <= '0;
            rx_shift[rx_bit_idx]  <= dut_txd_q2;   // sample at bit center
            if (rx_bit_idx == 3'd7) begin
              rx_state <= RX_STOP;
            end else begin
              rx_bit_idx <= rx_bit_idx + 1'b1;
            end
          end else begin
            rx_cnt <= rx_cnt + 1'b1;
          end
        end

        RX_STOP: begin
          if (rx_cnt == DIV_M1) begin
            rx_cnt   <= '0;
            rx_state <= RX_IDLE;
            // Best-effort: byte is accepted regardless of stop-bit value
            // (no framing-error signalling to the AXIS side — bring-up
            // scope, see module header).
            rx_byte  <= rx_shift;
            rx_valid <= 1'b1;
          end else begin
            rx_cnt <= rx_cnt + 1'b1;
          end
        end

        default: rx_state <= RX_IDLE;
      endcase
    end
  end

  assign uart_tx_tdata  = rx_byte;
  assign uart_tx_tvalid = rx_valid;

  // =======================================================================
  // Transmitter: uart_rx_* (AXIS) -> dut_rxd_o (serial, 8N1)
  // =======================================================================
  typedef enum logic [1:0] {TX_IDLE, TX_START, TX_DATA, TX_STOP} tx_state_e;
  tx_state_e        tx_state;
  logic [CNT_W-1:0] tx_cnt;
  logic [2:0]       tx_bit_idx;
  logic [7:0]       tx_shift;
  logic             tx_line;

  assign uart_rx_tready = (tx_state == TX_IDLE);
  assign dut_rxd_o      = tx_line;

  always_ff @(posedge clk or negedge resetn) begin
    if (!resetn) begin
      tx_state   <= TX_IDLE;
      tx_cnt     <= '0;
      tx_bit_idx <= '0;
      tx_shift   <= '0;
      tx_line    <= 1'b1;                          // idle-high (mark)
    end else begin
      case (tx_state)
        TX_IDLE: begin
          tx_line <= 1'b1;
          tx_cnt  <= '0;
          if (uart_rx_tvalid && uart_rx_tready) begin
            tx_shift <= uart_rx_tdata;
            tx_state <= TX_START;
          end
        end

        TX_START: begin
          tx_line <= 1'b0;                          // start bit
          if (tx_cnt == DIV_M1) begin
            tx_cnt     <= '0;
            tx_bit_idx <= '0;
            tx_state   <= TX_DATA;
          end else begin
            tx_cnt <= tx_cnt + 1'b1;
          end
        end

        TX_DATA: begin
          tx_line <= tx_shift[tx_bit_idx];          // LSB first
          if (tx_cnt == DIV_M1) begin
            tx_cnt <= '0;
            if (tx_bit_idx == 3'd7) begin
              tx_state <= TX_STOP;
            end else begin
              tx_bit_idx <= tx_bit_idx + 1'b1;
            end
          end else begin
            tx_cnt <= tx_cnt + 1'b1;
          end
        end

        TX_STOP: begin
          tx_line <= 1'b1;                          // stop bit
          if (tx_cnt == DIV_M1) begin
            tx_cnt   <= '0;
            tx_state <= TX_IDLE;
          end else begin
            tx_cnt <= tx_cnt + 1'b1;
          end
        end

        default: tx_state <= TX_IDLE;
      endcase
    end
  end

endmodule
