// -----------------------------------------------------------------------------
// clcd_core.sv — the bus-agnostic 8080 byte-streaming engine (FIFO + strobe FSM)
//
// Factored verbatim-in-behaviour out of clcd.sv (the AXI4-Lite shell block, live
// on silicon since b4afe3e and rendering the harness status screen — see
// docs/CLCD_PANEL_FACTS.md). It is the SAME engine, not a re-derivation: the
// FIFO, its drop-on-full policy, the three-phase FSM and the phase_load() floor
// are lifted unchanged. Two front ends instantiate it:
//
//   fpga/shell/ip/clcd/clcd.sv        AXI4-Lite, in the STATIC shell  (harness)
//   fpga/rp/nanosoc_exp/ahb_clcd.sv   AHB-Lite,  inside the nanosoc RM (DUT)
//
// so the DUT-side block inherits the shell block's proof and its cocotb panel
// model (tests/clcd/clcd_panel_model.py watches only the pads, so it is
// bus-agnostic). The port list is FROZEN in fpga/shell/ip/clcd_kvm/README.md §1
// of the wave brief — W2-A and W2-D bind to it independently.
//
// WHAT IS *NOT* IN HERE, deliberately:
//   - Backlight and panel reset. The KVM owns CLCD_BL / CLCD_RST
//     (fpga/shell/ip/clcd_kvm/README.md §10) — that is what makes a hung or
//     garbage DUT always recoverable. A core that could drive them would hand
//     that lever to whichever front end happened to instantiate it.
//   - The CLCD_RD read-back path. READ_PATH=0 ships and the panel has no proven
//     read-back (docs/CLCD_PANEL_FACTS.md §7.2: assume none). clcd.sv keeps its
//     READ_PATH=1 datapath AROUND this core (it gates the core's `enable` while a
//     read cycle is armed or in flight, so the two can never drive the pads at
//     once); the core stays write-only and rd_n is a constant.
//   - Any register file. Timing arrives as inputs, in clk cycles, so the front
//     end owns the CSR (or AHB register) map and this file owns no addresses.
//
// FIFO OVERFLOW POLICY — never back-pressure the front end (clcd.sv:31-36):
//   a push arriving while `fifo_full` is DROPPED. The push interface has no
//   ready. On the shell side that is load-bearing: clcd_poll() runs in the same
//   superloop as the lwIP TCP/ARP timers and a bus stall there drops the network,
//   the only way into the board. The front end polls fifo_full and does not push.
//
// Single clock domain. Every phase is counted in `clk` cycles (100 MHz in the
// shell, 50 MHz dut_clk in the RM), so the same timing numbers mean different
// nanoseconds in the two front ends — that is the front end's problem, not this
// module's (docs/contracts/dut-display-tunnel.md §5 sets the DUT's floor).
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module clcd_core #(
  // Bus-cycle FIFO depth, in {RS,byte} entries. Must be <= 255 so a full level
  // is representable in the 8-bit fifo_level (and hence in the shell block's
  // STATUS[15:8]). Distributed RAM / 1x RAMB18.
  parameter int FIFO_DEPTH    = 128,

  // Recommended reset values for the FRONT END's timing register, in clk cycles.
  // The core itself takes its timing from the wr_lo/wr_hi/cs_setup inputs every
  // cycle and holds no register: these exist so both front ends seed the same
  // defaults from one place (clcd.sv's TIMING and ahb_clcd's TIMING agree by
  // construction). They are the values proven on the board at 100 MHz —
  // 20 ns setup / 40 ns WR low / 40 ns WR high (docs/CLCD_PANEL_FACTS.md §6).
  parameter int WR_LO_INIT    = 4,
  parameter int WR_HI_INIT    = 4,
  parameter int CS_SETUP_INIT = 2
) (
  input  logic       clk,
  input  logic       rst_n,

  // -- push interface: 1-cycle push, no ready. A push while `fifo_full` is
  //    DROPPED (see the header); the front end must poll and not push.
  input  logic       push_valid,
  input  logic       push_rs,      // 0 = command, 1 = data
  input  logic [7:0] push_data,

  // -- control
  input  logic       enable,       // gates cycle LAUNCH only; see the FSM note
  input  logic       fifo_reset,   // synchronous flush
  input  logic [7:0] wr_lo,        // timing, in clk cycles
  input  logic [7:0] wr_hi,
  input  logic [7:0] cs_setup,

  // -- status
  output logic       busy,         // FSM != IDLE
  output logic       fifo_empty,
  output logic       fifo_full,
  output logic [7:0] fifo_level,

  // -- 8080 pads. ACTIVE-LOW strobes, exactly as clcd.sv drives them today.
  output logic [7:0] pd_o,
  output logic       pd_oe,        // 1 = drive the bus (constant: write-only core)
  output logic       cs_n,
  output logic       wr_n,
  output logic       rd_n,         // constant 1: no read path in the core
  output logic       rs
);

  // ===========================================================================
  // {RS, byte} FIFO. Entry = {rs, byte[7:0]} (9 bits). Pointers are exactly
  // clog2(FIFO_DEPTH) wide; the count is 8-bit because FIFO_DEPTH <= 255.
  // ===========================================================================
  localparam int              PTRW    = (FIFO_DEPTH <= 1) ? 1 : $clog2(FIFO_DEPTH);
  localparam logic [PTRW-1:0] PTR_MAX = PTRW'(FIFO_DEPTH - 1);
  localparam logic [7:0]      DEPTH_8 = 8'(FIFO_DEPTH);

  logic [8:0]      fifo_mem [0:FIFO_DEPTH-1];
  logic [PTRW-1:0] head_q, tail_q;
  logic [7:0]      count_q;

  assign fifo_full  = (count_q == DEPTH_8);
  assign fifo_empty = (count_q == 8'd0);
  assign fifo_level = count_q;

  wire       push_en  = push_valid && !fifo_full;   // drop-on-full, never stall
  wire [8:0] push_dat = {push_rs, push_data};

  wire [8:0] fifo_head      = fifo_mem[head_q];
  wire       fifo_head_rs   = fifo_head[8];
  wire [7:0] fifo_head_byte = fifo_head[7:0];

  // ===========================================================================
  // Bus-cycle FSM. Pops one {RS,byte} per cycle and drives the 8080 strobes:
  //   SETUP     : CS asserted (low), RS/PD stable; wait cs_setup cycles.
  //   STROBE_LO : WR low (wr_lo cycles).
  //   STROBE_HI : WR high again (wr_hi cycles). The WR RISING edge at the
  //               LO->HI boundary is where the panel latches the byte.
  // Each phase lasts max(N,1) clk cycles (phase_load below).
  //
  // CS is deasserted between EVERY byte -- the FSM returns through IDLE for >=1
  // cycle before relaunching -- so a quiescent point exists per byte, not per
  // burst. The KVM's safe-switch gate is built on exactly that
  // (docs/CLCD_PANEL_FACTS.md §6; fpga/shell/ip/clcd_kvm/README.md §6).
  //
  // `enable` gates cycle LAUNCH only: with enable=0 the FIFO still accepts
  // pushes (dropped only when full) but nothing drains. A cycle in flight always
  // runs to completion -- neither an enable deassert nor a fifo_reset aborts it
  // mid-strobe, which would glitch the pads. clcd.sv's read path relies on this
  // when it steals the bus.
  // ===========================================================================
  typedef enum logic [1:0] {
    ST_IDLE      = 2'd0,
    ST_SETUP     = 2'd1,
    ST_STROBE_LO = 2'd2,
    ST_STROBE_HI = 2'd3
  } state_e;

  state_e     state_q;
  logic [7:0] tmr_q;
  logic       cur_rs_q;     // RS driven this cycle
  logic [7:0] cur_byte_q;   // PD driven this cycle

  assign busy = (state_q != ST_IDLE);

  // phase_load(v): cycles-1, floored at 0, so a phase always lasts >= 1 cycle
  // (a timing input of 0 still yields a single-cycle strobe).
  function automatic logic [7:0] phase_load(input logic [7:0] v);
    phase_load = (v == 8'd0) ? 8'd0 : (v - 8'd1);
  endfunction

  wire do_launch = (state_q == ST_IDLE) && enable && !fifo_empty;
  wire pop_en    = do_launch;

  // -- FIFO pointers / count --------------------------------------------------
  always_ff @(posedge clk) begin
    if (!rst_n || fifo_reset) begin
      head_q  <= '0;
      tail_q  <= '0;
      count_q <= 8'd0;
    end else begin
      if (push_en) begin
        fifo_mem[tail_q] <= push_dat;
        tail_q <= (tail_q == PTR_MAX) ? '0 : (tail_q + 1'b1);
      end
      if (pop_en) begin
        head_q <= (head_q == PTR_MAX) ? '0 : (head_q + 1'b1);
      end
      case ({push_en, pop_en})
        2'b10:   count_q <= count_q + 8'd1;
        2'b01:   count_q <= count_q - 8'd1;
        default: count_q <= count_q;   // 00 (idle) or 11 (push+pop) -> no change
      endcase
    end
  end

  // -- bus-cycle FSM ----------------------------------------------------------
  always_ff @(posedge clk) begin
    if (!rst_n) begin
      state_q    <= ST_IDLE;
      tmr_q      <= 8'd0;
      cur_rs_q   <= 1'b0;
      cur_byte_q <= 8'd0;
    end else begin
      case (state_q)
        ST_IDLE: begin
          if (do_launch) begin
            cur_rs_q   <= fifo_head_rs;
            cur_byte_q <= fifo_head_byte;
            tmr_q      <= phase_load(cs_setup);
            state_q    <= ST_SETUP;
          end
        end
        ST_SETUP: begin
          if (tmr_q == 8'd0) begin
            tmr_q   <= phase_load(wr_lo);
            state_q <= ST_STROBE_LO;
          end else begin
            tmr_q <= tmr_q - 8'd1;
          end
        end
        ST_STROBE_LO: begin
          if (tmr_q == 8'd0) begin
            tmr_q   <= phase_load(wr_hi);
            state_q <= ST_STROBE_HI;
          end else begin
            tmr_q <= tmr_q - 8'd1;
          end
        end
        ST_STROBE_HI: begin
          if (tmr_q == 8'd0) begin
            state_q <= ST_IDLE;
          end else begin
            tmr_q <= tmr_q - 8'd1;
          end
        end
        default: state_q <= ST_IDLE;
      endcase
    end
  end

  // -- 8080 strobe generation (combinational from FSM state) ------------------
  // CS is asserted in the three non-IDLE states and nowhere else; RS/PD hold the
  // launched values across the whole cycle (and stay put while idle -- harmless
  // with CS deasserted, and bit-identical to what ships).
  always_comb begin
    cs_n = 1'b1;
    wr_n = 1'b1;
    case (state_q)
      ST_SETUP:     cs_n = 1'b0;
      ST_STROBE_LO: begin cs_n = 1'b0; wr_n = 1'b0; end
      ST_STROBE_HI: cs_n = 1'b0;
      default: ;   // ST_IDLE: both deasserted
    endcase
  end

  assign rs    = cur_rs_q;
  assign pd_o  = cur_byte_q;
  assign pd_oe = 1'b1;   // write-only engine: always drive the bus toward the panel
  assign rd_n  = 1'b1;   // no read path here -- see the header

  // ===========================================================================
  // Unused-bit sink for the -Wall self-check. WR_LO_INIT/WR_HI_INIT/
  // CS_SETUP_INIT are front-end seeds (see the parameter comment), not core
  // state, so nothing in here reads them; sinking them keeps -Wall clean without
  // a module-wide waiver that could hide a real unused signal.
  // ===========================================================================
  // verilator lint_off UNUSED
  wire _unused_ok = &{1'b0,
                      1'(WR_LO_INIT), 1'(WR_HI_INIT), 1'(CS_SETUP_INIT),
                      1'b0};
  // verilator lint_on UNUSED

endmodule
