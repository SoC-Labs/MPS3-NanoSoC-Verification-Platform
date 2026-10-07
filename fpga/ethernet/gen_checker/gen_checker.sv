// -----------------------------------------------------------------------------
// gen_checker.sv — error-inject traffic generator / checker (GENCHK regmap,
// shell-regmap.md v0.2 @ 0x44A6_0000, OPEN_ISSUES I10).
//
// Real RTL (W-RTL-ETH, A1/A5) — one of the two blocks spec §12 flags as
// needing genuinely new RTL. Register layout implements the v0.2 CONTRACT
// table (CTRL/INJECT/TX_CNT/RX_CNT/ERR_CNT), which supersedes the old
// Phase-0 DRAFT (GEN_CTRL mode field / packed CHK_STATUS / CHK_CLEAR) that
// tests/common/regmap.py still carries — flagged for A6 (see README).
//
// ---------------------------------------------------------------------------
// REGISTERS (shell-regmap.md v0.2, GENCHK @ 0x44A6_0000)
//   0x00 CTRL    [0] gen_en, [1] chk_en                                (rw)
//   0x04 INJECT  [0] bad_fcs, [1] runt, [2] giant, [3] ifg, [4] dribble (rw)
//                 "fault to inject on next frame": write sets pending bits;
//                 the generator consumes (clears) ALL pending bits at the
//                 start of the next frame; readback = still-pending bits.
//                 A same-cycle host write wins over consumption.
//   0x08 TX_CNT  [31:0] frames generated                               (ro)
//   0x0C RX_CNT  [31:0] DUT frames checked                             (ro)
//   0x10 ERR_CNT [31:0] frames failing the checker                     (ro)
//   0x14 DUT_IP  [31:0] sniffed DUT IPv4, network order (byte0 in      (ro)
//                 [31:24]) — Phase-3 DUT-IP sniffer, always-on tap
//   0x18 DUT_STATUS [0] ip_seen (1 once an IP is latched),             (ro)
//                 [31:16] last-seen ethertype (debug aid)
//
// v1 semantics this RTL adds where the contract is silent (flagged for A6):
//   * Counter clear: TX_CNT clears on a 0->1 write of CTRL.gen_en;
//     RX_CNT/ERR_CNT clear on a 0->1 write of CTRL.chk_en. (v0.2 defines no
//     explicit clear register; benches use deltas and don't depend on this.)
//   * gen_en is free-run: frames stream (with inter-frame gap) until cleared.
//   * chk_en gates COUNTING only — the AXIS sink always accepts (tready=1);
//     enable/disable while a frame is mid-flight miscounts that one frame
//     (enable while the stream is idle).
//
// ---------------------------------------------------------------------------
// WHAT THE v1 MODEL DOES vs DOES NOT DO
//
// Generator (gen_m_*, byte AXI-Stream toward the DUT-RX-bound path):
//   * Frame = DST(6)+SRC(6)+ethertype(2)+counting payload+FCS(4), 64 bytes
//     total; FCS = IEEE CRC-32 (zlib variant, poly 0xEDB88320 reflected,
//     init/final all-ones) transmitted LSB-byte-first — bit-exact with
//     tests/common/frames.py build_frame(), the behavioural spec.
//   * INJECT faults (consumed per-frame): bad_fcs = FCS xor 0xFFFFFFFF
//     (frames.py's deterministic corruption); runt = 32-byte total frame;
//     giant = 1536-byte total frame; dribble = one extra byte after the
//     FCS (byte-granular approximation of dribble bits — an AXIS byte
//     stream cannot carry sub-byte dribble; wire-level dribble would need
//     rmii_phy_if cooperation, out of scope v1); ifg = the gap AFTER the
//     injected frame is compressed to 1 idle cycle (8 bit-times) instead of
//     the normal 12 (96 bit-times) — IFG here is modelled at the AXIS byte
//     layer (1 byte-cycle = 8 bit-times at 100 Mb/s), not at RMII dibit
//     timing.
//   * Frame content (addresses/ethertype/payload) is fixed, not
//     host-configurable, and runt/giant lengths are single fixed points —
//     not a length sweep.
//
// Checker (chk_s_*, byte AXI-Stream tapped from the DUT-TX-recovered path):
//   * Per tlast-delimited frame: streams the same CRC-32 and verifies the
//     residue (raw register == 0xDEBB20E3 <=> zlib.crc32(frame)==0x2144DF1C),
//     checks the 64..1518-byte untagged length envelope, and OR-accumulates
//     chk_s_tuser (link_partner_mac's own error flag) across the frame as a
//     cross-check, not the sole source of truth. Any of {bad FCS, runt,
//     giant, tuser} increments ERR_CNT; every frame increments RX_CNT.
//   * NOT scored in v1: IFG violations on the RX side (no arrival-time
//     model at this tap — the DUT's TX IFG would need scoring inside
//     rmii_phy_if's dibit domain), dribble bits (byte stream), VLAN-tagged
//     max-length (1522) — the envelope is the untagged 1518.
//
// DUT-IP sniffer (Phase-3, DUT_IP/DUT_STATUS at 0x14/0x18):
//   * Passively snoops the SAME chk_s_* tap (frames FROM the DUT) and latches
//     the DUT's own IPv4 address so the shell firmware can learn it without
//     arming the checker — the sniffer is ALWAYS-ON, independent of chk_en.
//     Non-perturbing: it only READS the tap (chk_s_tready stays 1).
//   * Untagged frame at chk_s (no preamble/SFD): ethertype = bytes 12..13;
//     for IPv4 (0x0800) the source address is bytes 26..29; for ARP (0x0806)
//     the sender-protocol-address is bytes 28..31. Reuses the checker's
//     frame-aligned byte index clen_q as the offset. Address stored in
//     network order (bits[31:24] = first/leftmost dotted-quad octet); ARP and
//     IPv4 are mutually exclusive per frame (ethertype gate), last frame wins.
//   * LIMITATION (v1): UNTAGGED frames only. An 802.1Q VLAN tag (0x8100 at
//     bytes 12..13) inserts 4 bytes and shifts every subsequent field, so the
//     sniffer would latch the wrong bytes — a tagged frame is simply ignored
//     (its ethertype is neither 0x0800 nor 0x0806). Stacked/QinQ likewise.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module gen_checker #(
  parameter int C_S_AXI_ADDR_WIDTH = 12,
  parameter int C_S_AXI_DATA_WIDTH = 32
) (
  // ---------------------------------------------------------------------
  // AXI4-Lite slave — MicroBlaze data bus, GENCHK regmap (v0.2 table above).
  // Accept-on-valid response style (same as mdio_phy_model — one of the two
  // contract-blessed styles, shell-regmap.md v0.2 "AXI4-Lite slave
  // conventions").
  // ---------------------------------------------------------------------
  input  logic                          s_axi_aclk,
  input  logic                          s_axi_aresetn,

  // verilator lint_off UNUSED
  input  logic [C_S_AXI_ADDR_WIDTH-1:0] s_axi_awaddr,  // full local word address
                                                         // [11:2] decoded (only the
                                                         // byte-offset [1:0] unused)
  input  logic [2:0]                    s_axi_awprot,  // not decoded
  // verilator lint_on UNUSED
  input  logic                          s_axi_awvalid,
  output logic                          s_axi_awready,

  // verilator lint_off UNUSED
  input  logic [C_S_AXI_DATA_WIDTH-1:0] s_axi_wdata,  // only [4:0] land in rw
                                                        // fields (CTRL[1:0],
                                                        // INJECT[4:0])
  input  logic [(C_S_AXI_DATA_WIDTH/8)-1:0] s_axi_wstrb, // byte lane 0 qualifies
                                                          // both rw registers
  // verilator lint_on UNUSED
  input  logic                          s_axi_wvalid,
  output logic                          s_axi_wready,

  output logic [1:0]                    s_axi_bresp,
  output logic                          s_axi_bvalid,
  input  logic                          s_axi_bready,

  // verilator lint_off UNUSED
  input  logic [C_S_AXI_ADDR_WIDTH-1:0] s_axi_araddr,  // full local word address
                                                         // [11:2] decoded, see
                                                         // s_axi_awaddr above
  input  logic [2:0]                    s_axi_arprot,  // not decoded
  // verilator lint_on UNUSED
  input  logic                          s_axi_arvalid,
  output logic                          s_axi_arready,

  output logic [C_S_AXI_DATA_WIDTH-1:0] s_axi_rdata,
  output logic [1:0]                    s_axi_rresp,
  output logic                          s_axi_rvalid,
  input  logic                          s_axi_rready,

  // ---------------------------------------------------------------------
  // Generator output — AXI-Stream spliced toward the DUT-RX-bound path (see
  // link_partner_mac/README.md "Note for gen_checker" for the intended tap
  // point; not wired in shell_bd.tcl yet).
  // ---------------------------------------------------------------------
  output logic [7:0] gen_m_tdata,
  output logic       gen_m_tvalid,
  input  logic       gen_m_tready,
  output logic       gen_m_tlast,

  // ---------------------------------------------------------------------
  // Checker input — AXI-Stream tapped from the DUT-TX-recovered path
  // (link_partner_mac's m_axis_rx_* including its tuser frame-error flag).
  // ---------------------------------------------------------------------
  input  logic [7:0] chk_s_tdata,
  input  logic       chk_s_tvalid,
  output logic       chk_s_tready,
  input  logic       chk_s_tlast,
  input  logic       chk_s_tuser
);

  // ===========================================================================
  // Shared CRC-32 (IEEE 802.3 / zlib variant): reflected poly 0xEDB88320,
  // init all-ones, byte streamed LSB-first. Raw (pre-final-XOR) register;
  // FCS = ~raw; valid-frame residue over data+FCS: raw == 32'hDEBB_20E3.
  // ===========================================================================
  function automatic logic [31:0] crc32_byte(input logic [31:0] c,
                                             input logic [7:0]  b);
    logic [31:0] x;
    x = c ^ {24'h0, b};
    for (int i = 0; i < 8; i++)
      x = x[0] ? ((x >> 1) ^ 32'hEDB8_8320) : (x >> 1);
    return x;
  endfunction

  localparam logic [31:0] CRC_INIT    = 32'hFFFF_FFFF;
  localparam logic [31:0] CRC_RESIDUE = 32'hDEBB_20E3;

  // ===========================================================================
  // Register file (v0.2 contract layout).
  // ===========================================================================
  logic        gen_en_q, chk_en_q;                    // CTRL
  logic [4:0]  inject_q;                              // INJECT (pending)
  logic [31:0] tx_cnt_q, rx_cnt_q, err_cnt_q;         // counters (ro)

  // DUT-IP sniffer state (RO; driven in the sniffer always_ff below, declared
  // here so the read-mux ahead of it can reference them — VCS is order-strict).
  logic [31:0] dut_ip_q;         // sniffed DUT IPv4, network order (byte0 = [31:24])
  logic        dut_ip_seen_q;    // 1 once a full address has been latched
  logic [15:0] dut_ethertype_q;  // last-seen ethertype (debug aid; frame gate)

  // Generator-side handshakes into the register file (single always block
  // owns inject_q/tx_cnt_q writes; these are request pulses computed below).
  logic        inj_consume;    // generator latches+clears pending INJECT
  logic        tx_done;        // generator completed a frame (tlast accepted)

  // Checker-side result pulses (computed in the checker section below).
  logic        chk_frame_done; // a tlast beat was accepted while chk_en
  logic        chk_frame_err;  // ... and the frame failed

  // ===========================================================================
  // AXI4-Lite slave — accept-on-valid (mdio_phy_model pattern; see that
  // file's comments for the BFM-interop rationale on both channels).
  // ===========================================================================
  logic axi_awready_q, axi_wready_q, axi_bvalid_q;
  logic axi_arready_q, axi_rvalid_q;

  // Full local word-address decode (addr[C_S_AXI_ADDR_WIDTH-1:ADDR_LSB]).
  // ONLY the five mapped offsets respond; every other offset in the page is
  // unmapped — reads return 0, writes are accepted (BRESP=OKAY) with no effect.
  // (Was: only addr[4:2] were decoded, so e.g. 0x20 aliased onto CTRL and a
  // stray write there silently toggled gen_en/chk_en and cleared the counters —
  // see README RESOLVED note.)  This block latches only the decoded word INDEX
  // (not the whole address) — the truncated-register style; widening the fix
  // means latching the full index, not just [4:2].
  //
  // DECODE-WIDTH CAP (added 2026-07-24 — the bug-#1 guard the eight shell CSR
  // blocks already carry; see fpga/shell/ip/dfx_ctl/dfx_ctl.sv "LOCAL_ADDR_W").
  // shell_bd.tcl instantiates every CSR slave with C_S_AXI_ADDR_WIDTH=32 and
  // drives the FULL interconnect address, not a page-local offset. Without this
  // cap, IDX_W would be 30 and waddr_idx for GENCHK's base 0x44A6_0000 would be
  // 0x1129_8000 -- never equal to IDX_CTRL ('h0). Every write ignored, every
  // read 0: the block is alive in simulation and DEAD ON SILICON. That is
  // exactly how bug #1 shipped once already.
  // Capping at 16 decodes the block's own 64 KiB page, so BASE+0x1_0000 cannot
  // alias offset 0. The ternary keeps a narrower instantiation legal, so this is
  // a NO-OP at the currently-shipped 12-bit width (12 < 16 => LOCAL_ADDR_W=12,
  // IDX_W=10, bit-identical to before) and only bites if the port is widened.
  // tests/csr_decode_width BLOCK=gen_checker elaborates at 32 and drives
  // base+offset; reverting LOCAL_ADDR_W to C_S_AXI_ADDR_WIDTH turns it red.
  localparam int ADDR_LSB = 2;
  localparam int LOCAL_ADDR_W = (C_S_AXI_ADDR_WIDTH < 16) ? C_S_AXI_ADDR_WIDTH : 16;
  localparam int IDX_W    = LOCAL_ADDR_W - ADDR_LSB;

  localparam logic [IDX_W-1:0] IDX_CTRL    = 'h0;  // 0x00
  localparam logic [IDX_W-1:0] IDX_INJECT  = 'h1;  // 0x04
  localparam logic [IDX_W-1:0] IDX_TX_CNT     = 'h2;  // 0x08 (ro)
  localparam logic [IDX_W-1:0] IDX_RX_CNT     = 'h3;  // 0x0C (ro)
  localparam logic [IDX_W-1:0] IDX_ERR_CNT    = 'h4;  // 0x10 (ro)
  localparam logic [IDX_W-1:0] IDX_DUT_IP     = 'h5;  // 0x14 (ro) DUT-IP sniffer
  localparam logic [IDX_W-1:0] IDX_DUT_STATUS = 'h6;  // 0x18 (ro) DUT-IP sniffer

  logic [IDX_W-1:0] axi_araddr_q;   // latched read word index (full-width)

  wire [IDX_W-1:0] waddr_idx = s_axi_awaddr[LOCAL_ADDR_W-1:ADDR_LSB];

  assign s_axi_awready = axi_awready_q;
  assign s_axi_wready  = axi_wready_q;
  assign s_axi_bresp   = 2'b00; // OKAY (unmapped/RO writes accepted, no effect)
  assign s_axi_bvalid  = axi_bvalid_q;
  assign s_axi_arready = axi_arready_q;
  assign s_axi_rresp   = 2'b00; // OKAY (unmapped reads return 0)
  assign s_axi_rvalid  = axi_rvalid_q;

  logic aw_accept;
  assign aw_accept = ~axi_awready_q && s_axi_awvalid && s_axi_wvalid;

  always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
    if (!s_axi_aresetn) begin
      axi_awready_q <= 1'b0;
      axi_wready_q  <= 1'b0;
    end else if (aw_accept) begin
      axi_awready_q <= 1'b1;
      axi_wready_q  <= 1'b1;
    end else begin
      axi_awready_q <= 1'b0;
      axi_wready_q  <= 1'b0;
    end
  end

  always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
    if (!s_axi_aresetn) begin
      gen_en_q     <= 1'b0;
      chk_en_q     <= 1'b0;
      inject_q     <= '0;
      tx_cnt_q     <= '0;
      rx_cnt_q     <= '0;
      err_cnt_q    <= '0;
      axi_bvalid_q <= 1'b0;
    end else begin
      // ---- generator/checker events (may be overridden by a host write
      //      in the same cycle — write wins, see INJECT note in header) ----
      if (inj_consume)   inject_q <= '0;
      if (tx_done)       tx_cnt_q <= tx_cnt_q + 1;
      if (chk_frame_done) begin
        rx_cnt_q <= rx_cnt_q + 1;
        if (chk_frame_err) err_cnt_q <= err_cnt_q + 1;
      end

      // ---- host writes ----
      if (aw_accept && s_axi_wstrb[0]) begin
        case (waddr_idx)
          IDX_CTRL: begin // CTRL
            gen_en_q <= s_axi_wdata[0];
            chk_en_q <= s_axi_wdata[1];
            // v1 clear-on-enable-rise semantics (header note, A6 flag)
            if (s_axi_wdata[0] && !gen_en_q) tx_cnt_q <= '0;
            if (s_axi_wdata[1] && !chk_en_q) begin
              rx_cnt_q  <= '0;
              err_cnt_q <= '0;
            end
          end
          IDX_INJECT: inject_q <= s_axi_wdata[4:0]; // INJECT
          default: ; // TX_CNT/RX_CNT/ERR_CNT ro; every unmapped offset — no
                     // effect (full-address decode; 0x20 no longer aliases CTRL)
        endcase
      end

      if (aw_accept && ~axi_bvalid_q) begin
        axi_bvalid_q <= 1'b1;
      end else if (axi_bvalid_q && s_axi_bready) begin
        axi_bvalid_q <= 1'b0;
      end
    end
  end

  logic ar_accept;
  assign ar_accept = ~axi_arready_q && s_axi_arvalid;

  always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
    if (!s_axi_aresetn) begin
      axi_arready_q <= 1'b0;
      axi_araddr_q  <= '0;
    end else if (ar_accept) begin
      axi_arready_q <= 1'b1;
      axi_araddr_q  <= s_axi_araddr[LOCAL_ADDR_W-1:ADDR_LSB];
    end else begin
      axi_arready_q <= 1'b0;
    end
  end

  always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
    if (!s_axi_aresetn) begin
      axi_rvalid_q <= 1'b0;
    end else if (ar_accept) begin
      axi_rvalid_q <= 1'b1;
    end else if (axi_rvalid_q && s_axi_rready) begin
      axi_rvalid_q <= 1'b0;
    end
  end

  always_comb begin
    case (axi_araddr_q)
      IDX_CTRL:    s_axi_rdata = {{(C_S_AXI_DATA_WIDTH-2){1'b0}}, chk_en_q, gen_en_q};
      IDX_INJECT:  s_axi_rdata = {{(C_S_AXI_DATA_WIDTH-5){1'b0}}, inject_q};
      IDX_TX_CNT:  s_axi_rdata = tx_cnt_q;
      IDX_RX_CNT:  s_axi_rdata = rx_cnt_q;
      IDX_ERR_CNT: s_axi_rdata = err_cnt_q;
      // DUT-IP sniffer: IP in network order; STATUS = {ethertype[31:16], 0, seen[0]}
      IDX_DUT_IP:  s_axi_rdata = dut_ip_q;
      IDX_DUT_STATUS: s_axi_rdata =
                     {dut_ethertype_q, {(C_S_AXI_DATA_WIDTH-17){1'b0}}, dut_ip_seen_q};
      default:     s_axi_rdata = '0;  // every unmapped offset reads 0
    endcase
  end

  // ===========================================================================
  // Generator.
  // ===========================================================================
  localparam logic [47:0] GEN_DST       = 48'h00_11_22_33_44_55;
  localparam logic [47:0] GEN_SRC       = 48'hAA_BB_CC_DD_EE_FF;
  localparam logic [15:0] GEN_ETHERTYPE = 16'h88B5;   // IEEE local experimental

  localparam int unsigned LEN_GOOD_DATA  = 60;    // + 4 FCS = 64-byte frame
  localparam int unsigned LEN_RUNT_DATA  = 28;    // + 4 FCS = 32  (< 64)
  localparam int unsigned LEN_GIANT_DATA = 1532;  // + 4 FCS = 1536 (> 1518)

  localparam int unsigned GAP_NORMAL = 12;  // 96 bit-times at 8 bits/cycle
  localparam int unsigned GAP_SHORT  = 1;   // 8 bit-times — IFG violation

  // INJECT bit indices (shell-regmap.md v0.2)
  localparam int unsigned INJ_BAD_FCS = 0;
  localparam int unsigned INJ_RUNT    = 1;
  localparam int unsigned INJ_GIANT   = 2;
  localparam int unsigned INJ_IFG     = 3;
  localparam int unsigned INJ_DRIBBLE = 4;

  function automatic logic [7:0] gen_byte(input logic [10:0] idx);
    if      (32'(idx) < 6)  return GEN_DST[8*(5 - 32'(idx)) +: 8];
    else if (32'(idx) < 12) return GEN_SRC[8*(11 - 32'(idx)) +: 8];
    else if (32'(idx) == 12) return GEN_ETHERTYPE[15:8];
    else if (32'(idx) == 13) return GEN_ETHERTYPE[7:0];
    else                     return idx[7:0];     // counting payload/pad
  endfunction

  typedef enum logic [2:0] {G_IDLE, G_GAP, G_DATA, G_FCS, G_EXTRA} gen_state_e;

  gen_state_e  gst_q;
  logic [4:0]  inj_lat_q;      // faults latched for the current frame
  logic [10:0] gidx_q;         // byte index within the data portion
  logic [10:0] dlen_q;         // data-portion length for this frame
  logic [1:0]  fidx_q;         // FCS byte index
  logic [31:0] gcrc_q;         // running CRC over the data portion
  logic [31:0] fcs_q;          // latched (possibly corrupted) FCS word
  logic [3:0]  gap_q;          // inter-frame gap countdown
  logic [3:0]  gap_next_q;     // gap to use before the NEXT frame

  logic        gen_beat;
  logic [31:0] gcrc_next;

  assign gen_beat  = gen_m_tvalid && gen_m_tready;
  assign gcrc_next = crc32_byte(gcrc_q, gen_byte(gidx_q));

  assign inj_consume = (gst_q == G_IDLE) && gen_en_q;

  assign gen_m_tdata = (gst_q == G_DATA) ? gen_byte(gidx_q)
                     : (gst_q == G_FCS)  ? fcs_q[8*fidx_q +: 8]
                                         : 8'hEE;              // dribble byte
  assign gen_m_tvalid = (gst_q == G_DATA) || (gst_q == G_FCS) || (gst_q == G_EXTRA);
  assign gen_m_tlast  = ((gst_q == G_FCS) && (fidx_q == 2'd3)
                          && !inj_lat_q[INJ_DRIBBLE])
                        || (gst_q == G_EXTRA);

  assign tx_done = gen_beat && gen_m_tlast;

  always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
    if (!s_axi_aresetn) begin
      gst_q      <= G_IDLE;
      inj_lat_q  <= '0;
      gidx_q     <= '0;
      dlen_q     <= '0;
      fidx_q     <= '0;
      gcrc_q     <= CRC_INIT;
      fcs_q      <= '0;
      gap_q      <= '0;
      gap_next_q <= 4'(GAP_NORMAL);
    end else begin
      unique case (gst_q)
        G_IDLE: begin
          gap_next_q <= 4'(GAP_NORMAL);    // fresh enable: normal lead-in gap
          if (gen_en_q) begin
            inj_lat_q <= inject_q;         // consume pending faults
            dlen_q    <= inject_q[INJ_RUNT]  ? 11'(LEN_RUNT_DATA)
                       : inject_q[INJ_GIANT] ? 11'(LEN_GIANT_DATA)
                                             : 11'(LEN_GOOD_DATA);
            gap_q     <= gap_next_q;
            gst_q     <= G_GAP;
          end
        end

        G_GAP: begin                       // idle cycles = inter-frame gap
          if (gap_q <= 4'd1) begin
            gidx_q <= '0;
            gcrc_q <= CRC_INIT;
            gst_q  <= G_DATA;
          end else begin
            gap_q <= gap_q - 4'd1;
          end
        end

        G_DATA: begin
          if (gen_beat) begin
            gcrc_q <= gcrc_next;
            if (gidx_q == dlen_q - 11'd1) begin
              // good FCS = ~raw; bad_fcs = that xor 0xFFFFFFFF (= raw)
              fcs_q  <= inj_lat_q[INJ_BAD_FCS] ? gcrc_next : ~gcrc_next;
              fidx_q <= '0;
              gst_q  <= G_FCS;
            end else begin
              gidx_q <= gidx_q + 11'd1;
            end
          end
        end

        G_FCS: begin
          if (gen_beat) begin
            if (fidx_q == 2'd3)
              gst_q <= inj_lat_q[INJ_DRIBBLE] ? G_EXTRA : G_IDLE;
            else
              fidx_q <= fidx_q + 2'd1;
          end
        end

        G_EXTRA: begin                     // single dribble byte (tlast)
          if (gen_beat) gst_q <= G_IDLE;
        end

        default: gst_q <= G_IDLE;
      endcase

      // frame completion: pick the gap that precedes the NEXT frame
      // (registered AFTER the case so it wins over G_IDLE's default)
      if (tx_done)
        gap_next_q <= inj_lat_q[INJ_IFG] ? 4'(GAP_SHORT) : 4'(GAP_NORMAL);
    end
  end

  // ===========================================================================
  // Checker.
  // ===========================================================================
  localparam int unsigned CHK_MIN_LEN = 64;    // untagged 802.3 envelope
  localparam int unsigned CHK_MAX_LEN = 1518;

  logic [31:0] ccrc_q;
  logic [15:0] clen_q;      // bytes seen so far (excl. current beat)
  logic        cuser_q;     // sticky OR of tuser across the frame

  logic        chk_beat;
  logic [31:0] ccrc_next;
  logic [15:0] clen_total;
  logic        frame_err;

  assign chk_s_tready = 1'b1;   // pure sink: never backpressures the tap
  assign chk_beat     = chk_s_tvalid;   // tready is constant 1

  assign ccrc_next  = crc32_byte(ccrc_q, chk_s_tdata);
  assign clen_total = clen_q + 16'd1;
  assign frame_err  = (ccrc_next != CRC_RESIDUE)
                    || (clen_total < 16'(CHK_MIN_LEN))
                    || (clen_total > 16'(CHK_MAX_LEN))
                    || cuser_q || chk_s_tuser;

  assign chk_frame_done = chk_beat && chk_s_tlast && chk_en_q;
  assign chk_frame_err  = frame_err;

  always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
    if (!s_axi_aresetn) begin
      ccrc_q  <= CRC_INIT;
      clen_q  <= '0;
      cuser_q <= 1'b0;
    end else if (chk_beat) begin
      if (chk_s_tlast) begin
        ccrc_q  <= CRC_INIT;
        clen_q  <= '0;
        cuser_q <= 1'b0;
      end else begin
        ccrc_q  <= ccrc_next;
        clen_q  <= clen_total;
        cuser_q <= cuser_q | chk_s_tuser;
      end
    end
  end

  // ===========================================================================
  // DUT-IP sniffer (Phase-3) — DUT_IP @ 0x14, DUT_STATUS @ 0x18.
  //
  // Always-on, non-perturbing snoop of the SAME chk_s_* tap the checker reads
  // (chk_s_tready stays 1; this block only READS the tap signals). Runs
  // entirely in the refclk domain like every other counter — the 50->100 MHz
  // AXI-Lite crossing is done by axi_cc_genchk in the shell, so no new CDC.
  //
  // clen_q is the checker's frame-aligned byte index: during a !tlast beat its
  // registered value IS the 0-based offset of the current chk_s_tdata byte
  // (it increments per non-last beat, resets on tlast). Untagged frame layout
  // (no preamble/SFD): [dstMAC 0..5][srcMAC 6..11][ethertype 12..13][payload..].
  //   ethertype = bytes 12..13         (MSB-first)
  //   IPv4 (0x0800) src address        = bytes 26..29
  //   ARP  (0x0806) sender-proto-addr  = bytes 28..31
  // dut_ip_q shifts MSB-first so bits[31:24] hold the first (network-order)
  // octet. The ethertype latched at bytes 12/13 is stable well before byte 26,
  // so it gates the address capture for the SAME frame; ARP/IPv4 are mutually
  // exclusive, last frame wins. See header for the UNTAGGED-only limitation.
  // ===========================================================================
  localparam logic [15:0] ETYPE_IPV4 = 16'h0800;
  localparam logic [15:0] ETYPE_ARP  = 16'h0806;
  // dut_ip_q / dut_ip_seen_q / dut_ethertype_q declared in the register-file
  // section above (the read-mux references them; VCS requires declare-before-use).

  always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
    if (!s_axi_aresetn) begin
      dut_ip_q        <= '0;
      dut_ip_seen_q   <= 1'b0;
      dut_ethertype_q <= '0;
    end else if (chk_beat && !chk_s_tlast) begin
      // ethertype capture (bytes 12,13 — MSB first)
      if (clen_q == 16'd12) dut_ethertype_q[15:8] <= chk_s_tdata;
      if (clen_q == 16'd13) dut_ethertype_q[7:0]  <= chk_s_tdata;

      // address capture — mutually exclusive by the (stable) ethertype
      if (dut_ethertype_q == ETYPE_ARP) begin
        // ARP sender-protocol-address = bytes 28..31
        if (clen_q >= 16'd28 && clen_q <= 16'd31) begin
          dut_ip_q <= {dut_ip_q[23:0], chk_s_tdata};
          if (clen_q == 16'd31) dut_ip_seen_q <= 1'b1;
        end
      end else if (dut_ethertype_q == ETYPE_IPV4) begin
        // IPv4 source address = bytes 26..29
        if (clen_q >= 16'd26 && clen_q <= 16'd29) begin
          dut_ip_q <= {dut_ip_q[23:0], chk_s_tdata};
          if (clen_q == 16'd29) dut_ip_seen_q <= 1'b1;
        end
      end
    end
  end

endmodule
