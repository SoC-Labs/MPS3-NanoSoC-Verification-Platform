// -----------------------------------------------------------------------------
// swo_uart_rx.sv — SWO (UART/NRZ mode) byte-capture deserialiser for the
// UARTBR bridge (fpga/shell/ip/uart_bridge/uart_bridge.sv).
//
// The `swo` partition pin (docs/contracts/partition-pins.md, console/trace
// group) is a RAW 1-bit Cortex-M SWO/ITM trace line — not a pre-framed byte
// stream. The Cortex-M TPIU/SWO can emit in either of two encodings:
//   - UART (NRZ) mode: standard async serial, 8 data bits, no parity,
//     1 stop bit, LSB first, idle-high — exactly an 8N1 UART at a baud the
//     DUT firmware programs into its own TPIU_ACPR divider;
//   - Manchester mode: self-clocking bi-phase encoding.
//
// THIS MODULE IMPLEMENTS UART (NRZ) MODE ONLY — v1 per the deliverable
// brief ("a simple 8N1-style deserialiser with a programmable divisor
// register"). Manchester capture is a documented follow-up (see the
// TODO(A1)/A6 list in uart_bridge.sv + this block's README): the DUT-side
// nanosoc TPIU must be configured for NRZ, and the shell's divisor here
// must be programmed to match the DUT's SWO baud (both are firmware/host
// coordination concerns, not RTL ones).
//
// Sampling scheme (classic async-serial RX):
//   - swo_i is 2-FF synchronized into clk_i (the dut_clk domain — the pin
//     is asynchronous by nature: its timing is set by the DUT's own TPIU
//     divider, not by any shell clock);
//   - a falling edge from idle-high arms the FSM, which waits HALF a bit
//     period, re-checks the line is still low (start-bit validation,
//     rejects glitches), then samples 8 data bits + the stop bit at
//     mid-bit intervals of one full bit period each;
//   - stop bit sampled 1 -> byte_valid_o strobe (byte on byte_o);
//     stop bit sampled 0 -> frame_err_o strobe, byte discarded. Either
//     way the FSM returns to hunting for the next start edge.
//
// Timing contract: bit period = (divisor_i + 1) clk_i cycles. divisor_i
// must be >= 3 (absolute minimum for the mid-bit math) and >= 7 is
// recommended (>= 8x oversampling keeps the 2-FF-sync + edge-detect
// quantization error well inside half a bit). divisor_i and enable_i are
// the parent's job to make safe: uart_bridge.sv captures the divisor into
// this domain on the (synchronized) enable rising edge, so divisor_i is
// guaranteed stable whenever enable_i is high — this module treats both as
// already-safe, same-domain levels.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module swo_uart_rx #(
  parameter int DIV_WIDTH = 16
) (
  input  logic                 clk_i,        // dut_clk domain
  input  logic                 rst_n_i,      // active-low, synchronous, dut domain
  input  logic                 enable_i,     // already-synchronized level (parent-owned CDC)
  input  logic [DIV_WIDTH-1:0] divisor_i,    // stable while enable_i=1 (parent-captured);
                                             // bit period = divisor_i+1 clk_i cycles
  input  logic                 swo_i,        // raw partition pin (async; 2-FF sync inside)

  output logic [7:0]           byte_o,       // captured byte (valid with byte_valid_o)
  output logic                 byte_valid_o, // 1-cycle strobe
  output logic                 frame_err_o   // 1-cycle strobe: stop bit sampled 0
);

  // ===========================================================================
  // Input synchronizer + edge detect. Reset to idle-high (a spurious
  // falling edge out of reset would otherwise fire a phantom start bit
  // while the DUT is still held in reset with the line undriven).
  // R4 CDC: swo_i is a raw async trace pin -> (* ASYNC_REG = "TRUE" *) on the
  // sync chain (3 deep: 2-FF sync + 1 for edge-detect history). Single-bit
  // serial line -> plain flop sync is correct.
  // ===========================================================================
  (* ASYNC_REG = "TRUE" *) logic [2:0] swo_sync_q;

  always_ff @(posedge clk_i) begin
    if (!rst_n_i) begin
      swo_sync_q <= 3'b111;
    end else begin
      swo_sync_q <= {swo_sync_q[1:0], swo_i};
    end
  end

  wire swo_s    = swo_sync_q[1];                    // safe, synchronized level
  wire swo_fall = swo_sync_q[2] & ~swo_sync_q[1];   // idle -> start transition

  // ===========================================================================
  // Deserialiser FSM (localparam states, house style — see dfx_ctl.sv's
  // register-index localparams).
  // ===========================================================================
  localparam logic [1:0] ST_IDLE  = 2'd0;  // hunt for a start edge
  localparam logic [1:0] ST_START = 2'd1;  // wait half a bit, validate start
  localparam logic [1:0] ST_DATA  = 2'd2;  // sample 8 data bits, LSB first
  localparam logic [1:0] ST_STOP  = 2'd3;  // sample + judge the stop bit

  logic [1:0]           state_q;
  logic [DIV_WIDTH-1:0] baud_cnt_q;   // down-counter: act when it reaches 0
  logic [2:0]           bit_idx_q;
  logic [7:0]           shift_q;

  wire cnt_done = (baud_cnt_q == '0);

  always_ff @(posedge clk_i) begin
    if (!rst_n_i) begin
      state_q      <= ST_IDLE;
      baud_cnt_q   <= '0;
      bit_idx_q    <= '0;
      shift_q      <= '0;
      byte_o       <= '0;
      byte_valid_o <= 1'b0;
      frame_err_o  <= 1'b0;
    end else begin
      // Strobes default low; set for exactly one cycle below.
      byte_valid_o <= 1'b0;
      frame_err_o  <= 1'b0;

      if (!enable_i) begin
        // Disabled: hold in IDLE. A byte in flight when the host clears
        // the enable is discarded (documented in the README — disable is
        // a host action taken when no trace is expected anyway).
        state_q <= ST_IDLE;
      end else begin
        unique case (state_q)
          ST_IDLE: begin
            if (swo_fall) begin
              state_q    <= ST_START;
              // Half a bit period lands the next sample mid-start-bit.
              baud_cnt_q <= {1'b0, divisor_i[DIV_WIDTH-1:1]};
            end
          end

          ST_START: begin
            if (cnt_done) begin
              if (!swo_s) begin
                // Genuine start bit — begin sampling data at full-bit steps.
                state_q    <= ST_DATA;
                baud_cnt_q <= divisor_i;
                bit_idx_q  <= '0;
              end else begin
                // Line bounced back high before mid-bit: glitch, re-arm.
                state_q <= ST_IDLE;
              end
            end else begin
              baud_cnt_q <= baud_cnt_q - 1'b1;
            end
          end

          ST_DATA: begin
            if (cnt_done) begin
              shift_q    <= {swo_s, shift_q[7:1]};   // LSB first
              baud_cnt_q <= divisor_i;
              if (bit_idx_q == 3'd7) begin
                state_q <= ST_STOP;
              end else begin
                bit_idx_q <= bit_idx_q + 1'b1;
              end
            end else begin
              baud_cnt_q <= baud_cnt_q - 1'b1;
            end
          end

          ST_STOP: begin
            if (cnt_done) begin
              if (swo_s) begin
                byte_o       <= shift_q;
                byte_valid_o <= 1'b1;
              end else begin
                frame_err_o  <= 1'b1;   // framing error: byte discarded
              end
              state_q <= ST_IDLE;       // hunt for the next start edge
            end else begin
              baud_cnt_q <= baud_cnt_q - 1'b1;
            end
          end

          default: state_q <= ST_IDLE;
        endcase
      end
    end
  end

endmodule
