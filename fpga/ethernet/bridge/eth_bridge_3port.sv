// -----------------------------------------------------------------------------
// eth_bridge_3port.sv — fixed 3-port L2 bridge (management / DUT-MAC /
// LAN9220-uplink), dest-MAC parse + fixed two-entry forwarding table,
// flood-on-miss (spec §9, Decision D9).
//
// Real forwarding RTL (W-RTL-ETH, A1/A5). Hand-rolled — NOT the Forencich
// axis_switch/eth_axis_rx composition the Phase-0 notes suggested: no copy of
// verilog-ethernet/verilog-axis exists on this machine, this repo consumes
// external IP strictly read-only via env vars, and vendoring a third-party
// tree for a 3-port fixed bridge (spec §12's own sizing: "3-port forwarding
// glue (small)") is more surface than the ~200 lines below. Vendoring stays
// on the table for A6 if the bridge outgrows itself (spec §9 fallback order:
// Forencich -> Xilinx AXIS Switch -> LiteEth).
//
// Register-less per OPEN_ISSUES I11 / shell-regmap.md v0.2 ("BRIDGE (I11)
// stays register-less in v0.1: fixed 3-port forwarding, no MicroBlaze
// visibility — confirmed intentional"). The forwarding table is the two
// compile-time MAC parameters below.
//
// ---------------------------------------------------------------------------
// v1 FORWARDING POLICY (documented — what this bridge does and does NOT do)
//
//   * Store-and-forward: each ingress port buffers ONE complete frame
//     (per-port 2^BUF_AW-byte RAM), then a single round-robin sequencer
//     replays it to the egress port(s). Head-of-line blocking by design —
//     this is a 10/100 functional-test path, not a switch fabric.
//   * Static routing: dst == MGMT_MAC -> mgmt port; dst == DUT_MAC ->
//     dut_mac port; anything else (incl. broadcast/multicast and frames too
//     short to carry a full dst) FLOODS to every port except its ingress
//     (D9 flood-on-miss). No MAC learning, no aging, no VLAN/STP.
//   * Never reflects: a frame whose only resolved egress is its own ingress
//     port is silently dropped (standard bridge rule).
//   * Flood replay is SEQUENTIAL (one egress at a time from the same
//     buffer), not simultaneous — ordering across ports is not guaranteed.
//   * Oversize frames (> buffer) are received-and-dropped whole; no
//     truncated forward is ever emitted.
//   * dut_mac_s_tuser (link_partner_mac's frame-error flag) is accepted and
//     IGNORED for routing (v1): errored frames still forward; independent
//     scoring is gen_checker's job (spec §8.4), and no contract text asks
//     the bridge to filter on it. Flagged in the README for A6.
//   * The DUT's MAC address can change between RMs; with a compile-time
//     table the new MAC simply floods (functionally correct, noisier).
//     Runtime table override needs a regmap block — A6 ambiguity, README.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module eth_bridge_3port #(
  // Placeholder locally-administered MACs — same values as tests/bridge
  // (see dut_notes.md ambiguity note; A6 owns making these real).
  parameter logic [47:0] MGMT_MAC = 48'h02_00_00_00_00_01,
  parameter logic [47:0] DUT_MAC  = 48'h02_00_00_00_00_02,
  parameter int unsigned BUF_AW   = 11   // per-port frame buffer, 2^11 = 2048 B
) (
  input  logic       clk_i,
  input  logic       rst_i,

  // ---------------------------------------------------------------------
  // Port 0: management (MicroBlaze/lwIP) — interface shape TBD by A3's lwIP
  // integration; generic byte AXI-Stream for now.
  // ---------------------------------------------------------------------
  input  logic [7:0] mgmt_s_tdata,
  input  logic       mgmt_s_tvalid,
  output logic       mgmt_s_tready,
  input  logic       mgmt_s_tlast,
  output logic [7:0] mgmt_m_tdata,
  output logic       mgmt_m_tvalid,
  input  logic       mgmt_m_tready,
  output logic       mgmt_m_tlast,

  // ---------------------------------------------------------------------
  // Port 1: DUT-MAC — mirrors fpga/ethernet/link_partner_mac/'s AXI-Stream.
  // ---------------------------------------------------------------------
  input  logic [7:0] dut_mac_s_tdata,
  input  logic       dut_mac_s_tvalid,
  output logic       dut_mac_s_tready,
  input  logic       dut_mac_s_tlast,
  // verilator lint_off UNUSED
  input  logic       dut_mac_s_tuser,   // frame-error passthrough — accepted,
                                        // ignored for routing in v1 (header)
  // verilator lint_on UNUSED
  output logic [7:0] dut_mac_m_tdata,
  output logic       dut_mac_m_tvalid,
  input  logic       dut_mac_m_tready,
  output logic       dut_mac_m_tlast,

  // ---------------------------------------------------------------------
  // Port 2: LAN9220 uplink — toward fpga/ethernet/lan9220_if/.
  // ---------------------------------------------------------------------
  input  logic [7:0] uplink_s_tdata,
  input  logic       uplink_s_tvalid,
  output logic       uplink_s_tready,
  input  logic       uplink_s_tlast,
  output logic [7:0] uplink_m_tdata,
  output logic       uplink_m_tvalid,
  input  logic       uplink_m_tready,
  output logic       uplink_m_tlast
);

  localparam int unsigned NPORT = 3;
  localparam int unsigned P_MGMT   = 0;
  localparam int unsigned P_DUTMAC = 1;
  localparam int unsigned P_UPLINK = 2;

  // ---------------------------------------------------------------------
  // Port-array views of the named AXI-Stream ports.
  // ---------------------------------------------------------------------
  logic [7:0] in_tdata  [NPORT];
  logic       in_tvalid [NPORT];
  logic       in_tlast  [NPORT];

  assign in_tdata[P_MGMT]    = mgmt_s_tdata;
  assign in_tvalid[P_MGMT]   = mgmt_s_tvalid;
  assign in_tlast[P_MGMT]    = mgmt_s_tlast;
  assign in_tdata[P_DUTMAC]  = dut_mac_s_tdata;
  assign in_tvalid[P_DUTMAC] = dut_mac_s_tvalid;
  assign in_tlast[P_DUTMAC]  = dut_mac_s_tlast;
  assign in_tdata[P_UPLINK]  = uplink_s_tdata;
  assign in_tvalid[P_UPLINK] = uplink_s_tvalid;
  assign in_tlast[P_UPLINK]  = uplink_s_tlast;

  // ---------------------------------------------------------------------
  // Ingress side: per-port single-frame buffer + dest-MAC capture.
  // recv_q[i]=1 -> port accepts (tready); a completed frame parks with
  // pend_q[i]=1 until the sequencer releases it via rel[i].
  // ---------------------------------------------------------------------
  logic              recv_q  [NPORT];
  logic              pend_q  [NPORT];
  logic              drop_q  [NPORT];   // oversize: swallow to tlast, no fwd
  logic [BUF_AW-1:0] wr_ptr_q[NPORT];
  logic [BUF_AW:0]   len_q   [NPORT];   // completed-frame length in bytes
  logic [47:0]       dst_q   [NPORT];   // first 6 bytes, network order
  logic              rel     [NPORT];   // sequencer release pulse

  assign mgmt_s_tready    = recv_q[P_MGMT];
  assign dut_mac_s_tready = recv_q[P_DUTMAC];
  assign uplink_s_tready  = recv_q[P_UPLINK];

  // Sequencer read side (one shared reader; only one replay active at once).
  logic [BUF_AW-1:0] rd_ptr_q;
  logic [7:0]        rd_byte_q[NPORT];  // per-port registered RAM read

  for (genvar gi = 0; gi < NPORT; gi++) begin : g_ingress
    logic [7:0] buf_mem [0:(1 << BUF_AW)-1];

    always_ff @(posedge clk_i) begin
      // frame-buffer write (accepted beat, not in oversize-drop mode)
      if (recv_q[gi] && in_tvalid[gi] && !drop_q[gi])
        buf_mem[wr_ptr_q[gi]] <= in_tdata[gi];
      // registered read for the forwarding sequencer
      rd_byte_q[gi] <= buf_mem[rd_ptr_q];
    end

    always_ff @(posedge clk_i or posedge rst_i) begin
      if (rst_i) begin
        recv_q[gi]   <= 1'b1;
        pend_q[gi]   <= 1'b0;
        drop_q[gi]   <= 1'b0;
        wr_ptr_q[gi] <= '0;
        len_q[gi]    <= '0;
        dst_q[gi]    <= '0;
      end else if (recv_q[gi]) begin
        if (in_tvalid[gi]) begin      // tready == recv_q, so this is a beat
          if (32'(wr_ptr_q[gi]) < 6)
            dst_q[gi] <= {dst_q[gi][39:0], in_tdata[gi]};
          if (in_tlast[gi]) begin
            if (drop_q[gi]) begin     // oversize frame ends: reset, no fwd
              recv_q[gi]   <= 1'b1;
              drop_q[gi]   <= 1'b0;
              wr_ptr_q[gi] <= '0;
              dst_q[gi]    <= '0;
            end else begin
              recv_q[gi] <= 1'b0;
              pend_q[gi] <= 1'b1;
              len_q[gi]  <= {1'b0, wr_ptr_q[gi]} + 1'b1;
            end
          end else begin
            if (wr_ptr_q[gi] == {BUF_AW{1'b1}})
              drop_q[gi] <= 1'b1;     // buffer exhausted: swallow to tlast
            else
              wr_ptr_q[gi] <= wr_ptr_q[gi] + 1'b1;
          end
        end
      end else if (rel[gi]) begin     // sequencer done with this frame
        recv_q[gi]   <= 1'b1;
        pend_q[gi]   <= 1'b0;
        wr_ptr_q[gi] <= '0;
        dst_q[gi]    <= '0;
      end
    end
  end

  // ---------------------------------------------------------------------
  // Routing rule (v1 policy — see header).
  // ---------------------------------------------------------------------
  function automatic logic [NPORT-1:0] route(input logic [47:0] dst,
                                             input logic [1:0]  src_port);
    logic [NPORT-1:0] m;
    if      (dst == MGMT_MAC) m = 3'b001;
    else if (dst == DUT_MAC)  m = 3'b010;
    else                      m = 3'b111;            // flood-on-miss (D9)
    m &= ~(NPORT'(1) << src_port);                   // never reflect
    return m;
  endfunction

  // ---------------------------------------------------------------------
  // Forwarding sequencer: round-robin over parked ingress frames; replays
  // the granted frame to each egress in its mask, one port at a time.
  // Two-phase read (SEQ_READ latches buf_mem, SEQ_DRIVE handshakes it out):
  // 2 cycles/byte when the sink is always-ready — fine for a 10/100 path.
  // ---------------------------------------------------------------------
  typedef enum logic [1:0] {SEQ_IDLE, SEQ_READ, SEQ_DRIVE} seq_state_e;

  seq_state_e        seq_q;
  logic [1:0]        rr_q;          // round-robin scan pointer
  logic [1:0]        cur_in_q;      // granted ingress port
  logic [1:0]        cur_eg_q;      // egress currently being driven
  logic [NPORT-1:0]  mask_q;        // egress ports still to serve
  logic [BUF_AW:0]   cur_len_q;

  function automatic logic [1:0] rr_next(input logic [1:0] p);
    return (p == 2'd2) ? 2'd0 : (p + 2'd1);
  endfunction

  // lowest set bit -> port index (NPORT == 3; m[2] implied by elimination,
  // hence never read — that's the point of the final else)
  // verilator lint_off UNUSED
  function automatic logic [1:0] first_port(input logic [NPORT-1:0] m);
  // verilator lint_on UNUSED
    if      (m[0]) return 2'd0;
    else if (m[1]) return 2'd1;
    else           return 2'd2;
  endfunction

  logic       last_byte;
  logic       eg_tready;

  assign last_byte = ({1'b0, rd_ptr_q} == (cur_len_q - 1'b1));
  assign eg_tready = (cur_eg_q == 2'(P_MGMT))   ? mgmt_m_tready
                   : (cur_eg_q == 2'(P_DUTMAC)) ? dut_mac_m_tready
                                                : uplink_m_tready;

  always_ff @(posedge clk_i or posedge rst_i) begin
    if (rst_i) begin
      seq_q     <= SEQ_IDLE;
      rr_q      <= 2'd0;
      cur_in_q  <= 2'd0;
      cur_eg_q  <= 2'd0;
      mask_q    <= '0;
      cur_len_q <= '0;
      rd_ptr_q  <= '0;
      for (int i = 0; i < NPORT; i++) rel[i] <= 1'b0;
    end else begin
      for (int i = 0; i < NPORT; i++) rel[i] <= 1'b0;

      unique case (seq_q)
        SEQ_IDLE: begin
          if (pend_q[rr_q]) begin
            automatic logic [NPORT-1:0] m = route(dst_q[rr_q], rr_q);
            if (m == '0) begin
              rel[rr_q] <= 1'b1;      // own-port dest only: drop frame
            end else begin
              cur_in_q  <= rr_q;
              mask_q    <= m;
              cur_eg_q  <= first_port(m);
              cur_len_q <= len_q[rr_q];
              rd_ptr_q  <= '0;
              seq_q     <= SEQ_READ;
            end
          end
          rr_q <= rr_next(rr_q);
        end

        SEQ_READ: begin
          // rd_byte_q[cur_in_q] latches buf_mem[rd_ptr_q] at this edge
          seq_q <= SEQ_DRIVE;
        end

        SEQ_DRIVE: begin
          if (eg_tready) begin
            if (last_byte) begin
              automatic logic [NPORT-1:0] m_left
                  = mask_q & ~(NPORT'(1) << cur_eg_q);
              if (m_left == '0) begin
                rel[cur_in_q] <= 1'b1;
                seq_q         <= SEQ_IDLE;
              end else begin          // replay same frame to the next egress
                mask_q   <= m_left;
                cur_eg_q <= first_port(m_left);
                rd_ptr_q <= '0;
                seq_q    <= SEQ_READ;
              end
            end else begin
              rd_ptr_q <= rd_ptr_q + 1'b1;
              seq_q    <= SEQ_READ;
            end
          end
        end

        default: seq_q <= SEQ_IDLE;
      endcase
    end
  end

  // ---------------------------------------------------------------------
  // Egress drive: one shared data register (only one egress active at a
  // time); valid/last are decoded off registered state — glitch-free.
  // ---------------------------------------------------------------------
  logic [7:0] eg_byte;
  logic       eg_valid;

  assign eg_byte  = rd_byte_q[cur_in_q];
  assign eg_valid = (seq_q == SEQ_DRIVE);

  assign mgmt_m_tdata     = eg_byte;
  assign mgmt_m_tvalid    = eg_valid && (cur_eg_q == 2'(P_MGMT));
  assign mgmt_m_tlast     = last_byte;
  assign dut_mac_m_tdata  = eg_byte;
  assign dut_mac_m_tvalid = eg_valid && (cur_eg_q == 2'(P_DUTMAC));
  assign dut_mac_m_tlast  = last_byte;
  assign uplink_m_tdata   = eg_byte;
  assign uplink_m_tvalid  = eg_valid && (cur_eg_q == 2'(P_UPLINK));
  assign uplink_m_tlast   = last_byte;

endmodule
