// -----------------------------------------------------------------------------
// eth_ss_bringup.sv — AHB-Lite constant-programmer FSM for rm_eth_ss.
//
// A joint work commissioned on behalf of SoC Labs, under Arm Academic Access
// license.
//
// PURPOSE (rm_eth_ss "AHB decision", option (b) — see fpga/rp/eth_ss/README.md
// "Who drives the AHB slave port?"): the partition-pin boundary
// (docs/contracts/partition-pins.md v0.1) carries NO shell<->DUT AHB, so
// nothing outside this RM can ever program the ethmac_subsystem_ahb register
// file. Rather than leaving the MAC structurally present but permanently
// unconfigured (option (a), functionally dead), this little single-master
// AHB-Lite write engine walks a fixed bring-up sequence once after reset:
//
//   #  addr    value           register       why
//   -- ------  --------------  -------------  -----------------------------------
//   0  0x0020  0x0000_0001     TX_BD_NUM      1 TX BD (BD0 @0x400); RX BD ring
//                                             starts at BD1 @0x408. Satisfies
//                                             both MODER gates (RXEN needs
//                                             TX_BD_NUM<0x80, TXEN needs >0).
//   1  0x000C  0x0000_0015     IPGT           recommended back-to-back IPG for
//                                             full-duplex (OpenCores ethmac doc).
//   2  0x0040  MAC_ADDR0_VAL   MAC_ADDR0      station address octets 1..4
//                                             (RDL convention, LSB = 1st octet).
//   3  0x0044  MAC_ADDR1_VAL   MAC_ADDR1      station address octets 5..6.
//   4  0x0008  0x0000_001F     INT_MASK       unmask TXB|TXE|RXB|RXE|BUSY so
//                                             int_o (-> irq_out partition pin)
//                                             fires on real frame activity.
//   5  0x040C  RX_BUF_PTR      RX BD1 word1   frame-buffer pointer -> the RM's
//                                             internal DMA SRAM (offset 0).
//                                             Pointer FIRST: once E=1 the MAC
//                                             owns the BD.
//   6  0x0408  0x0000_E000     RX BD1 word0   EMPTY(15)|IRQ(14)|WRAP(13) — one
//                                             self-recycling RX descriptor
//                                             (bit positions confirmed against
//                                             eth_wishbone.v: ram_do[15] ready,
//                                             RxStatus[14] IRQEn, [13] Wrap).
//   7  0x1020  RTC_PERIOD_NS   PTP RTC period (integer ns, 8 LSBs used)
//   8  0x1024  RTC_PERIOD_FRAC PTP RTC period (fractional ns, 2^-32 ns units)
//   9  0x1000  0x0000_0004     PTP RTC ctrl   period_ld = bit2. Load strobes in
//                                             ha1588 reg.v are rising-edge
//                                             detected after a 3-FF CDC into
//                                             rtc_clk, hence set...
//   10 0x1000  0x0000_0000     PTP RTC ctrl   ...then clear (arms future loads;
//                                             the >= GAP_CYCLES spacing between
//                                             writes guarantees the level is
//                                             held long enough to cross).
//   11 0x0000  MODER_VAL       MODER          RECSMALL|PAD|CRCEN|FULLD|PRO|
//                                             TXEN|RXEN — enable LAST, so the
//                                             MAC wakes up fully configured.
//   12 0x0024  0x0000_0004     CTRLMODER      TXFLOW (bit 2) only. Gates the
//                                             MAC's PAUSE-frame transmitter
//                                             (eth_top.v: TxPauseRq_sync1 <=
//                                             r_TxPauseRq & r_TxFlow). RXFLOW
//                                             (bit 1) and PASSALL (bit 0) stay
//                                             0: received PAUSE frames are
//                                             still ignored, as before.
//   -- `done` rises here: bring-up is complete. --
//
// TRANSMIT BEACON (added 2026-09-23, lane EGRESS-TX). After the bring-up the
// FSM no longer parks: it writes
//
//      0x0050  0x0001_0000 | seq   TXCTRL   TXPAUSERQ (bit 16) | TXPAUSETV[15:0]
//
// immediately, then again every REARM_CYCLES clocks (default 50,000,000 =
// 1.0 s at the shell's 50 MHz dut_clk), seq = 1, 2, 3, ... (16-bit, wraps).
// Each write makes the MAC send ONE 802.3x PAUSE frame on its own — the
// MAC's control-frame generator (eth_transmitcontrol.v) builds the frame
// from registers, so no TX buffer descriptor and no DMA memory is involved.
// That is why this works with no CPU: the frame buffer SRAM (sl_ahb_sram in
// rp_eth_ss_wrapper.sv) sits on the MAC's DMA master bus, which this FSM
// cannot reach, so a BD-driven frame with our own payload is impossible
// without a wrapper change. The PAUSE frame is the only frame this MAC can
// emit from register writes alone.
//
// The frame on the wire (proven byte for byte in tests/eth_ss_bringup,
// BLOCK=tx, through the shell's virtual PHY, bridge and DUTEGR FIFO):
//   dst 01:80:C2:00:00:01 (802.3x MAC-control multicast) | src = station
//   address {MAC_ADDR1[15:0], MAC_ADDR0} sent MSB first | type 0x8808 |
//   opcode 0x0001 | pause time = seq (big-endian) | 42 zero pad bytes | FCS.
//   64 bytes with FCS. The MAC clears TXPAUSERQ itself once the frame is
//   queued (RstTxPauseRq), so each write is exactly one frame.
// The shell bridge FLOODS it (multicast dst, eth_bridge_3port.sv
// flood-on-miss) to its mgmt port, i.e. into DUTEGR, and to the uplink,
// which is safe-tied in the shell (drained, reaches no wire).
//
// Address map inside the subsystem's 64 KB AHB window (HADDR[15:0], decoded
// by cmsdk_apb_slave_mux on paddr[15:12] inside ethmac_subsystem_apb.v):
//   0x0000-0x0FFF  OpenCores ethmac (regs 0x000-0x3FF, BD SRAM 0x400-0x7FF)
//   0x1000-0x1FFF  HA1588 PTP (rtc ctrl 0x00, period 0x20/0x24, ...)
//   0x2000-0x2FFF  eth_rx_cksum (untouched — all IRQ_EN_* reset to 0, so
//                                cksum_int_o stays benign without config)
// Register offsets/fields per ethernet-mac-ahb/src/rdl/ethmac_regs.rdl and
// src/rtl/ha1588_patches/reg.v (reg_00[4:0] = {rtc_rst, time_ld, period_ld,
// adj_ld, time_rd}).
//
// Bus behaviour: single-master, single-slave AHB-Lite. Issues one NONSEQ
// 32-bit write at a time (never back-to-back — GAP_CYCLES idle cycles between
// transfers), so the cmsdk_ahb_to_apb slave sees the simplest legal traffic.
// HREADY in is expected to be the slave's own HREADYOUT (single-slave loop,
// wired in rp_eth_ss_wrapper.sv). HRESP is sampled but only latched into the
// `errored` telemetry flag — v1 makes no recovery attempt (see README).
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module eth_ss_bringup #(
  // Cycles to idle after resetn deasserts before the first write. Gives the
  // subsystem's internal bridges/synchronizers (and the rmii_to_mii reset
  // shift register) time to settle. Cheap insurance; 64 @ any plausible
  // dut_clk is microseconds.
  parameter int unsigned START_DELAY_CYCLES = 64,
  // Idle cycles between successive writes. MUST be >= 4 so the ha1588 ctrl
  // strobe levels (writes 9/10) are each held across that block's 3-FF
  // rtc_clk synchronizer before the opposite edge arrives.
  parameter int unsigned GAP_CYCLES         = 8,
  // Transmit beacon: 1 = after bring-up, send one PAUSE frame now and one
  // every REARM_CYCLES clocks thereafter; 0 = park after bring-up (the
  // pre-2026-09-23 behaviour, never transmits).
  parameter bit          TX_BEACON          = 1'b1,
  parameter int unsigned REARM_CYCLES       = 50_000_000,  // 1 s @ 50 MHz

  // Programmed values (defaults chosen in rp_eth_ss_wrapper.sv / README.md).
  parameter logic [31:0] MODER_VAL       = 32'h0001_A423,
  parameter logic [31:0] INT_MASK_VAL    = 32'h0000_001F,
  parameter logic [31:0] MAC_ADDR0_VAL   = 32'h454C_5302,
  parameter logic [31:0] MAC_ADDR1_VAL   = 32'h0000_3253,
  parameter logic [31:0] RX_BUF_PTR      = 32'h0000_0000,
  parameter logic [31:0] RTC_PERIOD_NS   = 32'd40,       // 40 ns = 25 MHz rtc_clk
  parameter logic [31:0] RTC_PERIOD_FRAC = 32'd0
) (
  input  logic        clk,     // dut_clk (= subsystem HCLK)
  input  logic        resetn,  // dut_resetn & rp_resetn (see wrapper)

  // AHB-Lite master, to ethmac_subsystem_ahb's slave port (HSEL is tied
  // high at the wrapper — single-slave bus).
  output logic [31:0] haddr,
  output logic  [1:0] htrans,
  output logic        hwrite,
  output logic  [2:0] hsize,
  output logic  [2:0] hburst,
  output logic  [3:0] hprot,
  output logic        hmastlock,
  output logic [31:0] hwdata,
  input  logic [31:0] hrdata,   // unused (write-only sequence)
  input  logic        hready,   // = slave HREADYOUT (single-slave loop)
  input  logic        hresp,

  // Telemetry (RM-internal; not partition pins in v0.1)
  output logic        done,     // bring-up complete (beacon may follow)
  output logic        errored   // any HRESP=ERROR seen during the sequence
);

  // ---------------------------------------------------------------------------
  // Write sequence ROM
  // ---------------------------------------------------------------------------
  localparam int unsigned N_WRITES = 13;
  localparam int unsigned RA_W = (REARM_CYCLES < 2) ? 1 : $clog2(REARM_CYCLES);

  typedef struct packed {
    logic [15:0] addr;
    logic [31:0] data;
  } wr_entry_t;

  function automatic wr_entry_t seq_entry(input logic [3:0] idx);
    unique case (idx)
      4'd0 :   seq_entry = '{addr: 16'h0020, data: 32'h0000_0001};  // TX_BD_NUM
      4'd1 :   seq_entry = '{addr: 16'h000C, data: 32'h0000_0015};  // IPGT (full duplex)
      4'd2 :   seq_entry = '{addr: 16'h0040, data: MAC_ADDR0_VAL};  // MAC_ADDR0
      4'd3 :   seq_entry = '{addr: 16'h0044, data: MAC_ADDR1_VAL};  // MAC_ADDR1
      4'd4 :   seq_entry = '{addr: 16'h0008, data: INT_MASK_VAL};   // INT_MASK
      4'd5 :   seq_entry = '{addr: 16'h040C, data: RX_BUF_PTR};     // RX BD1 pointer
      4'd6 :   seq_entry = '{addr: 16'h0408, data: 32'h0000_E000};  // RX BD1 E|IRQ|WRAP
      4'd7 :   seq_entry = '{addr: 16'h1020, data: RTC_PERIOD_NS};  // PTP period ns
      4'd8 :   seq_entry = '{addr: 16'h1024, data: RTC_PERIOD_FRAC};// PTP period frac
      4'd9 :   seq_entry = '{addr: 16'h1000, data: 32'h0000_0004};  // PTP ctrl: period_ld
      4'd10:   seq_entry = '{addr: 16'h1000, data: 32'h0000_0000};  // PTP ctrl: clear
      4'd11:   seq_entry = '{addr: 16'h0000, data: MODER_VAL};      // MODER (enable)
      4'd12:   seq_entry = '{addr: 16'h0024, data: 32'h0000_0004};  // CTRLMODER: TXFLOW
      default: seq_entry = '{addr: 16'h0000, data: 32'h0000_0000};
    endcase
  endfunction

  // ---------------------------------------------------------------------------
  // FSM
  // ---------------------------------------------------------------------------
  typedef enum logic [2:0] {
    S_WAIT,   // post-reset settle delay
    S_ADDR,   // drive AHB address phase (NONSEQ write)
    S_DATA,   // drive AHB data phase, wait for hready
    S_GAP,    // inter-write idle gap
    S_DONE    // bring-up done: parked (TX_BEACON=0) or waiting to re-arm
  } state_e;

  state_e             state;
  logic [3:0]         idx;       // current sequence entry
  logic [15:0]        wait_cnt;  // shared delay/gap counter
  logic [31:0]        hwdata_q;  // data-phase payload, latched at address phase
  logic               tx_phase;  // 1 = the current write is the TXCTRL beacon
  logic [15:0]        tx_seq;    // pause-time field of the next beacon frame
  logic [RA_W-1:0]    rearm_cnt; // beacon period counter

  wr_entry_t cur;
  assign cur = tx_phase ? '{addr: 16'h0050, data: {15'd0, 1'b1, tx_seq}}  // TXCTRL
                        : seq_entry(idx);

  // Constant AHB-Lite sideband: 32-bit, single, non-locked, data/privileged.
  assign hsize     = 3'b010;
  assign hburst    = 3'b000;
  assign hprot     = 4'b0011;
  assign hmastlock = 1'b0;
  assign hwdata    = hwdata_q;

  always_ff @(posedge clk or negedge resetn) begin
    if (!resetn) begin
      state    <= S_WAIT;
      idx      <= 4'd0;
      wait_cnt <= 16'd0;
      hwdata_q <= 32'd0;
      haddr    <= 32'd0;
      htrans   <= 2'b00;   // IDLE
      hwrite   <= 1'b0;
      done     <= 1'b0;
      errored  <= 1'b0;
      tx_phase <= 1'b0;
      tx_seq   <= 16'd1;
      rearm_cnt <= '0;
    end else begin
      unique case (state)
        S_WAIT: begin
          if (wait_cnt >= 16'(START_DELAY_CYCLES)) begin
            state    <= S_ADDR;
            wait_cnt <= 16'd0;
          end else begin
            wait_cnt <= wait_cnt + 16'd1;
          end
        end

        S_ADDR: begin
          // Drive the address phase; it is accepted on the first cycle
          // hready is high (bus was idle, so normally immediately).
          haddr  <= {16'h0000, cur.addr};
          htrans <= 2'b10;   // NONSEQ
          hwrite <= 1'b1;
          if (htrans == 2'b10 && hready) begin
            // Address phase accepted -> move to data phase.
            hwdata_q <= cur.data;
            htrans   <= 2'b00;  // single transfer: next phase is IDLE
            hwrite   <= 1'b0;
            state    <= S_DATA;
          end
        end

        S_DATA: begin
          // Hold hwdata until the slave completes the data phase.
          if (hready) begin
            if (hresp) errored <= 1'b1;
            if (tx_phase) begin
              // Beacon queued: count off one period, then send the next.
              tx_seq    <= tx_seq + 16'd1;
              rearm_cnt <= '0;
              state     <= S_DONE;
            end else if (idx == 4'(N_WRITES - 1)) begin
              done <= 1'b1;
              if (TX_BEACON) begin
                // First beacon straight after bring-up (one normal gap).
                tx_phase <= 1'b1;
                wait_cnt <= 16'd0;
                state    <= S_GAP;
              end else begin
                state <= S_DONE;
              end
            end else begin
              idx      <= idx + 4'd1;
              wait_cnt <= 16'd0;
              state    <= S_GAP;
            end
          end
        end

        S_GAP: begin
          if (wait_cnt >= 16'(GAP_CYCLES)) begin
            state <= S_ADDR;
          end else begin
            wait_cnt <= wait_cnt + 16'd1;
          end
        end

        S_DONE: begin
          // htrans stays IDLE, done stays high. With TX_BEACON=1 this is the
          // inter-beacon wait; with TX_BEACON=0 it is parked forever.
          done <= 1'b1;
          if (TX_BEACON) begin
            if (rearm_cnt >= RA_W'(REARM_CYCLES - 1)) begin
              rearm_cnt <= '0;
              state     <= S_ADDR;
            end else begin
              rearm_cnt <= rearm_cnt + 1'b1;
            end
          end
        end

        default: state <= S_DONE;
      endcase
    end
  end

  // hrdata intentionally unused — write-only programmer.
  logic unused_hrdata;
  assign unused_hrdata = ^hrdata;

endmodule
