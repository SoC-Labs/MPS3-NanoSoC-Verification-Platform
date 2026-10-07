// -----------------------------------------------------------------------------
// mdio_slave.sv — Clause-22 MDIO (MDC/MDIO) slave frame engine.
//
// This is the *protocol* half of the virtual-PHY MDIO model (spec §8.1,
// §12; docs/contracts/partition-pins.md MDIO group). It decodes the
// preamble/ST/OP/PHYAD/REGAD/TA/DATA frame the DUT (the MDIO master) drives
// on `mdc_i`/`mdio_o_i`/`mdio_oe_i`, and drives `mdio_i_o` with register
// data during a read. It knows nothing about what BMCR/BMSR/etc. actually
// *mean* — that content lives in phy_reg_model.sv, reached only through the
// narrow reg_addr/reg_wr_en/reg_wr_data/reg_rd_data port group below.
//
// Clock domain: this module is **externally clocked by mdc_i** — the DUT's
// own MDIO clock (Clause-22 caps it at 2.5 MHz, but nothing here assumes a
// particular rate, and mdc_i may also simply stop between transactions).
// There is no independent clock domain of its own; `rst_n` must already be
// synchronized into the mdc_i domain by the caller (mdio_phy_model.sv does
// this once, in one place, rather than duplicating a synchronizer here and
// in phy_reg_model.sv).
//
// Bit timing (IEEE 802.3 Clause 22.2.4.5, "MDC shall be sourced by the STA
// ... data shall be valid ... around the rising edge of MDC"): this model
// *samples* mdio_o_i/mdio_oe_i on the rising edge of mdc_i (same edge the
// DUT and any real PHY would sample by), and *drives* mdio_i_o on the
// falling edge, so the driven value has a full half-period of setup before
// the DUT's next sampling edge.
//
// Preamble tolerance: real STAs send >=32 one-bits of preamlbe, but this
// slave does not require an exact count — it starts capturing the header
// the moment it sees a driven '0' (the first ST bit), preceded by any
// number of driven '1's (including zero). A malformed frame (bad ST, or an
// OP that is neither read nor write) is simply not answered/committed; the
// state machine still counts out the frame's bit-time budget and resyncs
// on the next preamble, so a bad frame cannot wedge the model.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module mdio_slave #(
  parameter logic [4:0] C_PHY_ADDR = 5'd1  // MDIO PHY address this model answers to
) (
  input  logic mdc,        // DUT's MDIO clock (externally clocked slave)
  input  logic rst_n,      // active-low reset, already synchronized to mdc

  // MDIO partition pins (partition-pins.md, shell's view) — DUT is the
  // MDIO master.
  input  logic mdio_o_i,   // MDIO data from DUT
  input  logic mdio_oe_i,  // DUT's output-enable (1 = DUT driving mdio_o_i)
  output logic mdio_i_o,   // this model's reply, sampled by the DUT

  // Register-file interface -> phy_reg_model.sv. reg_addr/reg_rd_data are
  // valid throughout a whole frame's payload phase (latched once the
  // header decodes); reg_wr_en pulses for exactly one mdc cycle when a
  // write frame addressed to us completes.
  output logic [4:0]  reg_addr_o,
  output logic        reg_wr_en_o,
  output logic [15:0] reg_wr_data_o,
  input  logic [15:0] reg_rd_data_i
);

  // ===========================================================================
  // Frame shape: ST(2) OP(2) PHYAD(5) REGAD(5) = 14-bit header,
  // then TA(2) DATA(16) = 18-bit payload. 32 bits total after preamble.
  // ===========================================================================
  localparam logic [4:0] HEADER_BITS  = 5'd14;
  localparam logic [4:0] PAYLOAD_BITS = 5'd18;

  typedef enum logic [1:0] {
    ST_PREAMBLE = 2'd0,  // hunting for preamble '1's then the first ST bit ('0')
    ST_HEADER   = 2'd1,  // capturing ST/OP/PHYAD/REGAD
    ST_PAYLOAD  = 2'd2   // TA + DATA (captured on write, driven on read)
  } state_e;

  state_e      state_q;
  logic [4:0]  bit_cnt_q;          // reused across HEADER and PAYLOAD phases
  logic [12:0] header_shreg_q;     // holds the last 13 captured bits; the
                                    // 14th (newest) is folded in below via
                                    // header_shreg_d, never stored as-is
  logic [14:0] data_shreg_q;       // write-side capture shift register (same
                                    // one-bit-shorter-than-the-lookahead shape)

  logic        is_read_q, is_write_q, phy_match_q;

  // ---------------------------------------------------------------------
  // Combinational "next value" helpers — computed from *current* (pre-edge)
  // state, so the header-complete / payload-complete decisions can be made
  // on the very same mdc edge that the last bit of that field arrives,
  // without resorting to procedural (automatic) locals inside the
  // sequential block.
  // ---------------------------------------------------------------------
  logic [13:0] header_shreg_d;
  assign header_shreg_d = {header_shreg_q, mdio_o_i};

  logic [1:0] hdr_st_next, hdr_op_next;
  logic [4:0] hdr_phyad_next, hdr_regad_next;
  assign hdr_st_next    = header_shreg_d[13:12];
  assign hdr_op_next    = header_shreg_d[11:10];
  assign hdr_phyad_next = header_shreg_d[9:5];
  assign hdr_regad_next = header_shreg_d[4:0];

  logic [15:0] data_shreg_d;
  assign data_shreg_d = {data_shreg_q, mdio_o_i};

  // ===========================================================================
  // Sampling process (posedge mdc): decode incoming bits while the DUT
  // drives them; commit register writes; select the register to read.
  // ===========================================================================
  always_ff @(posedge mdc or negedge rst_n) begin
    if (!rst_n) begin
      state_q        <= ST_PREAMBLE;
      bit_cnt_q      <= '0;
      header_shreg_q <= '0;
      data_shreg_q   <= '0;
      is_read_q      <= 1'b0;
      is_write_q     <= 1'b0;
      phy_match_q    <= 1'b0;
      reg_addr_o     <= '0;
      reg_wr_en_o    <= 1'b0;
      reg_wr_data_o  <= '0;
    end else begin
      reg_wr_en_o <= 1'b0; // default: one-cycle commit pulse, may be set below

      case (state_q)
        // -----------------------------------------------------------
        // Hunt for the first driven '0' (ST bit 1) after any run of
        // driven '1's (preamble, tolerant of any length including 0).
        // -----------------------------------------------------------
        ST_PREAMBLE: begin
          if (mdio_oe_i && !mdio_o_i) begin
            header_shreg_q <= '0;
            bit_cnt_q      <= 5'd1;
            state_q        <= ST_HEADER;
          end
        end

        // -----------------------------------------------------------
        // Capture ST/OP/PHYAD/REGAD, MSB-first. Losing drive mid-header
        // is a framing error -- resync on the next preamble.
        // -----------------------------------------------------------
        ST_HEADER: begin
          if (!mdio_oe_i) begin
            state_q   <= ST_PREAMBLE;
            bit_cnt_q <= '0;
          end else begin
            header_shreg_q <= header_shreg_d[12:0];
            if (bit_cnt_q == HEADER_BITS - 5'd1) begin
              is_read_q     <= (hdr_st_next == 2'b01) && (hdr_op_next == 2'b10);
              is_write_q    <= (hdr_st_next == 2'b01) && (hdr_op_next == 2'b01);
              phy_match_q   <= (hdr_phyad_next == C_PHY_ADDR);
              reg_addr_o    <= hdr_regad_next;
              bit_cnt_q     <= '0;
              state_q       <= ST_PAYLOAD;
            end else begin
              bit_cnt_q <= bit_cnt_q + 5'd1;
            end
          end
        end

        // -----------------------------------------------------------
        // TA(2) + DATA(16). Write: capture DATA (bit indices 2..17) and
        // commit on the last bit if addressed to us. Read: nothing to
        // sample here (the DUT has released the bus) -- driving
        // mdio_i_o happens in the negedge process below.
        // -----------------------------------------------------------
        ST_PAYLOAD: begin
          if (is_write_q && (bit_cnt_q >= 5'd2)) begin
            data_shreg_q <= data_shreg_d[14:0];
          end

          if (bit_cnt_q == PAYLOAD_BITS - 5'd1) begin
            if (is_write_q && phy_match_q) begin
              reg_wr_en_o   <= 1'b1;
              reg_wr_data_o <= data_shreg_d; // includes this final (LSB) bit
            end
            bit_cnt_q <= '0;
            state_q   <= ST_PREAMBLE;
          end else begin
            bit_cnt_q <= bit_cnt_q + 5'd1;
          end
        end

        default: begin
          state_q   <= ST_PREAMBLE;
          bit_cnt_q <= '0;
        end
      endcase
    end
  end

  // ===========================================================================
  // Drive process (negedge mdc): only meaningful during a read addressed
  // to us. reg_addr_o is already stable (set at header-complete, held
  // through the whole payload phase), so reg_rd_data_i is simply read live
  // -- no separate snapshot register is needed.
  //
  // Read-data turnaround timing (matches a real Clause-22 PHY, which the real
  // OpenCores-MAC MIIM master reads correctly on z2_03 silicon): the PHY takes
  // the bus at the FIRST payload bit-time (turnaround) and presents DATA[15] on
  // the very NEXT one -- i.e. a *single* effective turnaround bit-time from the
  // master's sampling point of view. An earlier revision drove DATA one MDC
  // period later (an explicit TA '0' at bit_cnt==1, DATA[15] at bit_cnt==2);
  // against a real MAC -- which captures the turnaround slot as the read's MSB
  // -- that read every register back as (value >> 1) (tests/nanosoc_multicore_eth
  // c2: PHY_ID 0x0007C0F1 -> 0x00036078). Driving DATA[15] at bit_cnt==1 removes
  // that one-MDC-period skew. tests/common/mdio_master.py::decode_read_reply
  // samples the matching window so the co-designed Python master still agrees.
  //   bit_cnt_q == 0        -> turnaround bit-time (idle-high: the master has
  //                            just released; mdio_i is a dedicated point-to-
  //                            point reply wire, not a shared bus)
  //   bit_cnt_q in [1..16]  -> DATA[15:0], MSB-first
  //   bit_cnt_q == 17       -> past DATA[0]; bus idle-high
  // ===========================================================================
  always_ff @(negedge mdc or negedge rst_n) begin
    if (!rst_n) begin
      mdio_i_o <= 1'b1;
    end else if (state_q == ST_PAYLOAD && is_read_q && phy_match_q) begin
      case (bit_cnt_q)
        5'd0:    mdio_i_o <= 1'b1;
        5'd17:   mdio_i_o <= 1'b1;
        default: mdio_i_o <= reg_rd_data_i[4'(5'd16 - bit_cnt_q)];
      endcase
    end else begin
      mdio_i_o <= 1'b1;
    end
  end

endmodule
