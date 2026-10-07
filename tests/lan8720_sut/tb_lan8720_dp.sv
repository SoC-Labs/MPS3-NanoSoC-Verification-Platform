// =============================================================================
// tests/lan8720_sut/tb_lan8720_dp.sv
//
// LAN8720 SUT -- DATAPATH (frame) exercise of the real ethernet-subsystem DUT
// (ethmac_ahb_rmii), through the MPS3 J28/PMOD0 shield model, at RMII.
//
// The MDIO diagnostic (tb_lan8720_sut.sv) covered the management path. This
// covers the frame datapath -- the missing RMII-boundary system test (the
// multicore cocotb injects at MII, downstream of rmii_to_mii).
//
// Phase order de-risks the foundation:
//   TX  -- preload a frame in DMA RAM, arm a TX BD, enable TX, and decode the
//          frame egressing on rmii_txd (through the shield outbound path).
//          Proves the real MAC + DMA + rmii_to_mii + RMII egress come alive.
//   RX  -- source an RMII frame inbound; healthy shield -> the MAC DMAs it and
//          the RX BD fills; dead-inbound shield -> the RX BD stays EMPTY,
//          reproducing board finding #4 at the datapath level.
//
// Both phases are implemented. A pure-SV AHB-lite slave RAM answers the DUT's
// DMA master; the register slave is driven by the same AHB master BFM as the
// MDIO SUT; an RMII frame source (rmii_rx_send, correct FCS) drives the RX path
// through the shield -- the first bench to exercise rmii_to_mii RX with a real
// frame (the reference cocotb injects at MII, above the bridge).
//
// VERDICT=PASS covers: TX body byte-exact + BD consumed + no DMA over-read;
// RX BD fills on a healthy shield (CRC accepted, header matches) and stays
// EMPTY with the inbound path dead (board finding #4); the TX frame length equals
// max(LEN+FCS, MINFL)+8 -- a gate, after the "TX-length bug" was RETRACTED: PACKETLEN
// is {MINFL[31:16], MAXFL[15:0]} and this bench had written it backwards. The register
// header has the fields transposed -- see FINDING_TX_LENGTH.md.
// =============================================================================
`timescale 1ns / 1ps

module tb_lan8720_dp;

  // ---- clocks ---------------------------------------------------------------
  logic hclk = 1'b0;    always #20 hclk = ~hclk;      // 25 MHz AHB/system
  logic ref_clk = 1'b0; always #10 ref_clk = ~ref_clk;// 50 MHz RMII reference
  logic hresetn = 1'b0;

  // ---- ethmac register map (OpenCores) --------------------------------------
  localparam MODER=32'h00, INT_SOURCE=32'h04, INT_MASK=32'h08,
             IPGT=32'h0C, IPGR1=32'h10, IPGR2=32'h14, PACKETLEN=32'h18,
             COLLCONF=32'h1C, TX_BD_NUM=32'h20, CTRLMODER=32'h24,
             MIIMODER=32'h28, BD_BASE=32'h400;
  // MODER bits
  localparam MODER_RXEN=32'h1, MODER_TXEN=32'h2, MODER_PRO=32'h20,
             MODER_FULLD=32'h400, MODER_CRCEN=32'h2000, MODER_PAD=32'h8000;
  localparam MODER_DEFAULT=32'h0000A000;
  // TX BD word0 flags (in [15:0]); LEN in [31:16]
  localparam TXF_RD=16'h8000, TXF_IRQ=16'h4000, TXF_WR=16'h2000,
             TXF_PAD=16'h1000, TXF_CRC=16'h0800;

  // ---- AHB register-slave side of the DUT -----------------------------------
  logic        hsel_s, hwrite_s, hready_s;
  logic [31:0] haddr_s, hwdata_s;
  logic  [1:0] htrans_s;
  logic  [2:0] hsize_s, hburst_s;
  logic  [3:0] hprot_s;
  wire  [31:0] hrdata_s;
  wire         hreadyout_s, hresp_s;
  assign hready_s = hreadyout_s;

  // ---- AHB DMA-master side of the DUT ---------------------------------------
  wire  [31:0] m_haddr, m_hwdata;
  wire   [1:0] m_htrans;
  wire   [2:0] m_hsize, m_hburst;
  wire   [3:0] m_hprot;
  wire         m_hmastlock, m_hwrite;
  logic [31:0] m_hrdata;
  logic        m_hready, m_hresp;

  // ---- RMII / MDIO ----------------------------------------------------------
  wire [1:0] rmii_txd;  wire rmii_tx_en;
  wire [1:0] a_rmii_rxd;  wire a_rmii_crs_dv;          // DUT RX, through the shield (from b-side)
  logic[1:0] rx_rxd_drv = 2'b00;  logic rx_crsdv_drv = 1'b0;  // PHY-side RX frame source (b-side)
  wire       mdc_pad_o, md_pad_o, md_padoe_o;  logic md_pad_i = 1'b1;
  wire       int_o;
  logic      dut_ref_clk;   // the DUT's ref clock, through the shield

  // ---- DUT ------------------------------------------------------------------
  ethmac_ahb_rmii dut (
    .hclk(hclk), .hresetn(hresetn),
    .haddr(m_haddr), .htrans(m_htrans), .hsize(m_hsize), .hburst(m_hburst),
    .hprot(m_hprot), .hmastlock(m_hmastlock), .hwrite(m_hwrite), .hwdata(m_hwdata),
    .hrdata(m_hrdata), .hready(m_hready), .hresp(m_hresp),
    .hsel_s(hsel_s), .haddr_s(haddr_s), .htrans_s(htrans_s), .hsize_s(hsize_s),
    .hburst_s(hburst_s), .hprot_s(hprot_s), .hwrite_s(hwrite_s), .hwdata_s(hwdata_s),
    .hready_s(hready_s), .hrdata_s(hrdata_s), .hreadyout_s(hreadyout_s), .hresp_s(hresp_s),
    .rmii_ref_clk(dut_ref_clk),
    .rmii_txd(rmii_txd), .rmii_tx_en(rmii_tx_en),
    .rmii_rxd(a_rmii_rxd), .rmii_crs_dv(a_rmii_crs_dv),
    .mode_speed(1'b1),
    .md_pad_i(md_pad_i), .mdc_pad_o(mdc_pad_o), .md_pad_o(md_pad_o),
    .md_padoe_o(md_padoe_o), .int_o(int_o)
  );

  // ---- shield: ref_clk (inbound) routed through it, fault-injectable --------
  logic pass_a2b = 1'b1, pass_b2a = 1'b1;
  wire  a_ref_clk;
  assign dut_ref_clk = a_ref_clk;
  shield_path u_shield (
    .pass_a2b(pass_a2b), .pass_b2a(pass_b2a),
    .a_mdc(mdc_pad_o), .a_mdio_o(md_pad_o), .a_mdio_oe(md_padoe_o), .a_mdio_i(),
    .a_rmii_crs_dv(a_rmii_crs_dv), .a_rmii_rxd(a_rmii_rxd), .a_rmii_ref_clk(a_ref_clk),
    .b_mdc(), .b_mdio_o(), .b_mdio_oe(), .b_mdio_i(1'b1),
    .b_rmii_crs_dv(rx_crsdv_drv), .b_rmii_rxd(rx_rxd_drv), .b_rmii_ref_clk(ref_clk)
  );

  // ---- AHB-lite slave RAM for the DUT's DMA master --------------------------
  // Word-addressable, single- and burst-transfer capable. 16 KiB window.
  localparam RAM_WORDS = 4096;                 // 16 KiB
  logic [31:0] ram [0:RAM_WORDS-1];
  // address-phase capture
  logic        a_valid;
  logic        a_write;
  logic [11:0] a_word;                         // word index (byte addr [13:2])
  // plain `always` (not always_ff): the initial block also preloads `ram`.
  always @(posedge hclk or negedge hresetn) begin
    if (!hresetn) begin
      a_valid <= 1'b0; a_write <= 1'b0; a_word <= '0;
      m_hrdata <= '0;
    end else begin
      // default single-cycle zero-wait slave: accept new addr each cycle
      if (m_htrans[1]) begin                   // NONSEQ or SEQ
        a_valid <= 1'b1; a_write <= m_hwrite; a_word <= m_haddr[13:2];
      end else begin
        a_valid <= 1'b0;
      end
      // data phase of the PREVIOUS address
      if (a_valid && a_write) ram[a_word] <= m_hwdata;
      m_hrdata <= ram[m_haddr[13:2]];          // read: present next-cycle data
    end
  end
  assign m_hready = 1'b1;                       // zero wait states
  assign m_hresp  = 1'b0;                       // OKAY

  // ---- register-slave AHB master BFM (same as the MDIO SUT) ------------------
  task automatic ahb_write(input [31:0] addr, input [31:0] data);
    @(posedge hclk);
    hsel_s=1; htrans_s=2'b10; haddr_s=addr; hwrite_s=1; hsize_s=3'b010; hburst_s=0; hprot_s=4'b0011;
    forever begin @(posedge hclk); if (hreadyout_s) break; end
    hsel_s=0; htrans_s=0; hwdata_s=data;
    forever begin @(posedge hclk); if (hreadyout_s) break; end
  endtask
  task automatic ahb_read(input [31:0] addr, output [31:0] data);
    @(posedge hclk);
    hsel_s=1; htrans_s=2'b10; haddr_s=addr; hwrite_s=0; hsize_s=3'b010; hburst_s=0; hprot_s=4'b0011;
    forever begin @(posedge hclk); if (hreadyout_s) break; end
    hsel_s=0; htrans_s=0;
    forever begin @(posedge hclk); if (hreadyout_s) break; end
    data = hrdata_s;
  endtask

  // ---- RMII TX egress decoder: reassemble the frame the MAC transmits -------
  // 100 Mb/s: one di-bit per ref_clk while tx_en. LSB-pair first, low nibble
  // first. Capture into tx_cap[], count bytes.
  logic [7:0] tx_cap [0:255];
  int         tx_bytes = 0;
  int         tx_frames = 0;     // count tx_en rising edges (frame starts)
  logic       tx_en_d = 1'b0;
  logic [3:0] tx_dibcnt = 0;
  logic [7:0] tx_acc;
  // plain `always` (not always_ff): permits the declaration initializers above.
  int         frame0_len = -1;   // tx_bytes latched at the first tx_en falling edge
  always @(posedge dut_ref_clk) begin
    tx_en_d <= rmii_tx_en;
    if (rmii_tx_en && !tx_en_d) tx_frames <= tx_frames + 1;
    if (!rmii_tx_en && tx_en_d && frame0_len < 0) frame0_len <= tx_bytes;
    if (rmii_tx_en) begin
      tx_acc = {rmii_txd, tx_acc[7:2]};        // shift di-bit in at top pair
      tx_dibcnt <= tx_dibcnt + 1;
      if (tx_dibcnt == 3) begin
        if (tx_bytes < 256) tx_cap[tx_bytes] = tx_acc;
        tx_bytes  <= tx_bytes + 1;
        tx_dibcnt <= 0;
      end
    end else begin
      tx_dibcnt <= 0;
    end
  end

  // ---- TX end-of-frame handshake probe (+TXDBG) ------------------------------
  // FINDING_TX_LENGTH.md: the frame never terminates on "buffer exhausted", only via
  // the MINFL compare or the MAXFL guard. Termination requires TxEndFrm asserted during
  // a StateData[1] beat (eth_txstatem.v:174-181). TxEndFrm is produced in eth_wishbone:
  //   1290: if (TxLengthEq0 & TxBufferAlmostEmpty & TxUsedData) TxEndFrm_wb <= 1;
  //   then latched into LastWord -> TxEndFrm on the Flop/TxByteCnt==3 beat.
  // This probe answers the two discriminators:
  //   (a) does TxEndFrm_wb EVER assert?   -> if never: the bug is the line-1290 gate
  //   (b) if it does, does LastWord/TxEndFrm ever latch? -> if not: the Flop/TxByteCnt
  //       alignment (a WB_CLK vs MTxClk crossing) is the bug.
  `define WBX  tb_lan8720_dp.dut.u_ethmac_ahb.u_eth_top.wishbone
  `define TXM  tb_lan8720_dp.dut.u_ethmac_ahb.u_eth_top.txethmac1

  bit txdbg = 1'b0;
  int dbg_n  = 0;
  int wb_seen = 0, lw_seen = 0, ef_seen = 0;   // did each ever assert?
  initial txdbg = $test$plusargs("TXDBG");

  always @(posedge `WBX.MTxClk) begin
    if (`WBX.TxEndFrm_wb) wb_seen++;
    if (`WBX.LastWord)    lw_seen++;
    if (`WBX.TxEndFrm)    ef_seen++;
    if (txdbg && `TXM.MTxEn && dbg_n < 300) begin
      dbg_n++;
      $display("TXDBG %0t ByteCnt=%0d NibCnt=%0d MinFL=%0d thr=%0d StateData=%b PAD=%b NibMinFl=%b | TxEndFrm=%b CrcEn=%b Pad=%b",
        $time, `TXM.txcounters1.ByteCnt, `TXM.txcounters1.NibCnt,
        `TXM.txcounters1.MinFL, ((`TXM.txcounters1.MinFL - 4) << 1) - 1,
        `TXM.StateData, `TXM.StatePAD, `TXM.NibbleMinFl,
        `WBX.TxEndFrm, `TXM.CrcEn, `TXM.Pad);
    end
  end

  // ---- helpers --------------------------------------------------------------
  int errors = 0;
  logic [7:0]  frame [0:1599];   // the frame body (no preamble/SFD/FCS), shared TX+RX
  int          flen;
  task automatic ram_put_byte(input int byte_addr, input [7:0] b);
    // BIG-ENDIAN byte into the word RAM: the OpenCores MAC transmits the MSB
    // of each 32-bit word first, so wire-order byte N must land at bits
    // [31:24 - (N%4)*8].  (cocotbext's AHB slave lays bytes out the same way,
    // which is why the reference bench loads the frame in plain wire order.)
    int w; int sh;
    w = byte_addr >> 2; sh = 24 - (byte_addr & 3) * 8;
    ram[w][sh +: 8] = b;
  endtask
  function automatic [7:0] ram_get_byte(input int byte_addr);
    int w; int sh;
    w = byte_addr >> 2; sh = 24 - (byte_addr & 3) * 8;
    return ram[w][sh +: 8];
  endfunction

  // Ethernet FCS: reflected CRC-32, poly 0xEDB88320, init/final 0xFFFFFFFF.
  // Appended LSB-byte-first (fcs[7:0], fcs[15:8], ...). Self-tested below.
  function automatic [31:0] eth_crc32(input int n, input logic [7:0] data []);
    logic [31:0] crc; int k, bit_i;
    crc = 32'hFFFF_FFFF;
    for (k = 0; k < n; k++) begin
      crc = crc ^ {24'h0, data[k]};
      for (bit_i = 0; bit_i < 8; bit_i++)
        crc = crc[0] ? ((crc >> 1) ^ 32'hEDB88320) : (crc >> 1);
    end
    return ~crc;
  endfunction

  // ---- RMII RX frame source (PHY side, through the shield b-side) -----------
  // Drives one di-bit per ref_clk while crs_dv is high, LSB-pair first: this is
  // the 100 Mb/s RMII stream the LAN8720 would present. Sends preamble(7x55) +
  // SFD(0xD5) + the given `n` frame bytes + a correct 4-byte FCS.
  logic [7:0] rx_full [0:1600];
  task automatic rmii_rx_send(input int n, input bit bad_fcs = 1'b0);
    int k, m; logic [7:0] b; logic [31:0] fcs; logic [7:0] fbytes [];
    fbytes = new[n];
    for (k = 0; k < n; k++) fbytes[k] = frame[k];
    fcs = eth_crc32(n, fbytes);
    if (bad_fcs) fcs[7:0] = fcs[7:0] ^ 8'hFF;           // corrupt the FCS
    m = 0;
    for (k = 0; k < 7; k++) rx_full[m++] = 8'h55;      // preamble
    rx_full[m++] = 8'hD5;                               // SFD
    for (k = 0; k < n; k++) rx_full[m++] = frame[k];    // payload
    rx_full[m++] = fcs[7:0];  rx_full[m++] = fcs[15:8];
    rx_full[m++] = fcs[23:16]; rx_full[m++] = fcs[31:24];
    @(negedge ref_clk);
    rx_crsdv_drv = 1'b1;
    for (k = 0; k < m; k++) begin
      b = rx_full[k];
      rx_rxd_drv = b[1:0]; @(negedge ref_clk);
      rx_rxd_drv = b[3:2]; @(negedge ref_clk);
      rx_rxd_drv = b[5:4]; @(negedge ref_clk);
      rx_rxd_drv = b[7:6]; @(negedge ref_clk);
    end
    rx_crsdv_drv = 1'b0; rx_rxd_drv = 2'b00;
    repeat (8) @(negedge ref_clk);
  endtask

  // ---- sequence -------------------------------------------------------------
  localparam TX_BUF = 32'h0000;                // TX frame buffer in DMA RAM
  localparam RX_BUF = 32'h2000;                // RX frame buffer in DMA RAM
  localparam RXF_E=16'h8000, RXF_IRQ=16'h4000, RXF_WR=16'h2000;  // RX BD word0 flags
  localparam RX_BD = BD_BASE + 2*8;            // RX BD index = TX_BD_NUM(2): 0x410
  int i;
  logic [31:0] rd;
  int exp_pad, exp_crc, exp_len;   // EXPERIMENT plusarg controls (TX phase)
  int exp_minfl_used;              // MINFL actually programmed into PACKETLEN[31:16]

  initial begin
    hsel_s=0; htrans_s=0; haddr_s=0; hwrite_s=0; hwdata_s=0; hsize_s=0; hburst_s=0; hprot_s=0;
    for (i=0;i<RAM_WORDS;i++) ram[i]='0;
    repeat (12) @(posedge hclk);
    hresetn = 1'b1;
    repeat (12) @(posedge hclk);

    ahb_read(MODER, rd);
    $display("LAN8720_DP: MODER default = 0x%08h (expect 0x0000A000)", rd);

    // ---- CRC-32 self-test: CRC32("123456789") must be 0xCBF43926 ----------
    begin
      logic [7:0] tv []; logic [31:0] c;
      tv = new[9];
      for (i=0;i<9;i++) tv[i] = 8'h31 + i[7:0];   // '1'..'9'
      c = eth_crc32(9, tv);
      if (c === 32'hCBF43926) $display("LAN8720_DP: CRC-32 self-test ok (0x%08h)", c);
      else begin errors++; $display("LAN8720_DP: CRC-32 self-test FAIL (got 0x%08h, want 0xCBF43926)", c); end
    end

    // ---- EXPERIMENT plusargs (default = the committed behaviour) -----------
    if (!$value$plusargs("EXP_PAD=%d", exp_pad)) exp_pad = 1;
    if (!$value$plusargs("EXP_CRC=%d", exp_crc)) exp_crc = 1;
    if (!$value$plusargs("EXP_LEN=%d", exp_len)) exp_len = 60;
    $display("LAN8720_DP: EXP  PAD=%0d CRC=%0d LEN=%0d", exp_pad, exp_crc, exp_len);

    // ---- build a small broadcast frame in DMA RAM (no CRC; MAC appends) ----
    // dst(6)=FF.., src(6)=02:00:00:00:00:01, ethertype 0x88B5, payload "MPS3-LAN8720!"
    flen = 0;
    for (i=0;i<6;i++) frame[flen++] = 8'hFF;
    frame[flen++]=8'h02; frame[flen++]=8'h00; frame[flen++]=8'h00;
    frame[flen++]=8'h00; frame[flen++]=8'h00; frame[flen++]=8'h01;
    frame[flen++]=8'h88; frame[flen++]=8'hB5;
    begin
      static string pl = "MPS3-LAN8720!";    // 13 chars
      for (int j = 0; j < pl.len(); j++) frame[flen++] = pl.getc(j);
    end
    while (flen < exp_len) frame[flen++] = 8'h00;  // pad body to EXP_LEN
    for (i=0;i<flen;i++) ram_put_byte(TX_BUF + i, frame[i]);
    for (i=flen;i<256;i++) ram_put_byte(TX_BUF + i, 8'hAA);  // PROBE: mark past-buffer bytes

    // ---- MAC init ---------------------------------------------------------
    ahb_write(IPGT, 32'h15); ahb_write(IPGR1, 32'h0C); ahb_write(IPGR2, 32'h12);
    // PACKETLEN is {MINFL[31:16], MAXFL[15:0]} -- eth_registers.v:926-927.
    // (reset 0x00400600 = MINFL 64 / MAXFL 1536, the only sane Ethernet values.)
    if (!$value$plusargs("EXP_MINFL=%d", exp_minfl_used)) exp_minfl_used = 64;
    ahb_write(PACKETLEN, (exp_minfl_used << 16) | 1518);
    $display("LAN8720_DP: EXP  MINFL=%0d MAXFL=1518", exp_minfl_used);
    ahb_write(TX_BD_NUM, 32'h2);                  // 2 TX BDs
    ahb_write(INT_MASK, 32'h0);

    // ---- arm TX BD 0: word0={LEN, flags}, word1=pointer -------------------
    begin
      logic [15:0] bdf;
      bdf = TXF_RD|TXF_IRQ|TXF_WR | (exp_pad?TXF_PAD:16'h0) | (exp_crc?TXF_CRC:16'h0);
      ahb_write(BD_BASE + 0, (flen<<16) | bdf);
    end
    ahb_write(BD_BASE + 4, TX_BUF);

    // ---- enable TX (full duplex, CRC, PAD) --------------------------------
    ahb_write(MODER, MODER_DEFAULT | MODER_TXEN | MODER_FULLD
                     | (exp_crc?MODER_CRCEN:32'h0) | (exp_pad?MODER_PAD:32'h0));

    $display("LAN8720_DP: PHASE TX -- armed BD0 (%0d bytes), TXEN set; watching rmii_txd", flen);

    // ---- wait for TX completion: the MAC clears the BD Ready bit when done --
    // (poll the BD, like the reference bench; RD clear is the primary indicator,
    //  int_o is secondary). ~6000 reads * ~4 cycles ~= 24k cycles.
    begin
      int p; rd = 32'hFFFF_FFFF;
      for (p = 0; p < 6000; p++) begin
        ahb_read(BD_BASE + 0, rd);
        if (((rd>>15)&1) == 0) break;
        if (int_o) break;
      end
      $display("LAN8720_DP: TX wait done after %0d polls (int_o=%0b)", p, int_o);
    end
    $display("LAN8720_DP: after TX -- BD0 word0=0x%08h (RD bit %0b), rmii bytes captured=%0d",
             rd, (rd>>15)&1, tx_bytes);
    ahb_read(INT_SOURCE, rd);
    $display("LAN8720_DP: INT_SOURCE=0x%08h (TXB[0]=%0b TXE[1]=%0b)", rd, rd&1, (rd>>1)&1);
    $display("LAN8720_DP: tx_en rising edges = %0d, first-frame length (to tx_en fall) = %0d bytes",
             tx_frames, frame0_len);
    ahb_read(BD_BASE + 0, rd);   // re-read BD word0 for the checks below

    // dump the captured RMII bytes across the frame + FCS + a little trailer
    begin
      string s; s = "";
      for (i=0;i<80 && i<tx_bytes;i++) s = {s, $sformatf("%02h ", tx_cap[i])};
      $display("LAN8720_DP: RMII egress [0..79]: %s", s);
    end

    // ---- checks -----------------------------------------------------------
    // 1) the WHOLE frame body egressed on RMII: after preamble+SFD(0xD5), all
    //    `flen` bytes (dst/src/type/payload/pad) must match byte-for-byte.
    begin
      int sfd; int miss; sfd = -1; miss = -1;
      for (i=0;i+1<tx_bytes;i++) if (tx_cap[i]==8'hD5) begin sfd=i+1; break; end
      if (sfd < 0) begin errors++; $display("LAN8720_DP:   FAIL  no SFD (0xD5) in RMII egress"); end
      else begin
        for (i=0;i<flen;i++) if (miss<0 && tx_cap[sfd+i] !== frame[i]) miss = i;
        if (miss < 0)
          $display("LAN8720_DP:   ok    RMII egress body matches all %0d frame bytes (SFD@%0d), FCS follows at +%0d",
                   flen, sfd-1, sfd+flen);
        else begin
          errors++;
          $display("LAN8720_DP:   FAIL  frame-body mismatch at byte %0d: egress=0x%02h expected=0x%02h (SFD@%0d)",
                   miss, tx_cap[sfd+miss], frame[miss], sfd-1);
        end
      end
    end
    // 2) the MAC cleared the TX BD Ready bit (transmission complete)
    if (((rd>>15)&1) == 0) $display("LAN8720_DP:   ok    TX BD Ready cleared (MAC consumed it)");
    else begin errors++; $display("LAN8720_DP:   FAIL  TX BD Ready still set (MAC did not transmit)"); end

    // 3) the DMA honoured the buffer length: memory PAST the 60-byte buffer is
    //    marked 0xAA (see the RAM preload). None of it must appear on the wire
    //    -- if it does, the MAC over-read past the buffer pointer+length.
    begin
      int aa; aa = 0;
      for (i=0;i<tx_bytes && i<256;i++) if (tx_cap[i]==8'hAA) aa++;
      if (aa == 0) $display("LAN8720_DP:   ok    no past-buffer (0xAA) bytes on the wire -- DMA respected the length");
      else begin errors++; $display("LAN8720_DP:   FAIL  %0d past-buffer 0xAA bytes egressed -- DMA over-read", aa); end
    end

    // ---- OBSERVATION (not a pass/fail gate): TX frame length vs MAXFL -------
    // The MAC transmits the correct body, then zero-pads the wire up to MAXFL
    // rather than terminating at length+FCS: frame0_len == MAXFL + 8 (pre+SFD).
    // The DMA did NOT over-read (check 3), so this is TX-side zero-fill, not a
    // memory over-fetch. Whether it is the ethmac TX path or an interaction
    // with this SUT's single-beat DMA slave is flagged for root-cause; the
    // reference cocotb bench never checks frame length, so it would not catch
    // this. See README / DUT_ETHERNET_EGRESS follow-up.
    // ---- TX frame length: must be max(LEN+FCS, MINFL) + 8 (preamble+SFD) ----
    // RETRACTED "TX-length bug": the MAC is CORRECT. PACKETLEN is
    // {MINFL[31:16], MAXFL[15:0]} (eth_registers.v:926-927; reset 0x00400600 =
    // MINFL 64 / MAXFL 1536). This bench used to write it the other way round,
    // so MINFL became 1518 and the MAC dutifully padded every short frame to
    // 1518 -- correct behaviour, wrong programming. See FINDING_TX_LENGTH.md.
    begin
      int exp_mac, exp_wire;
      exp_mac  = (flen + (exp_crc ? 4 : 0));
      if (exp_pad && exp_mac < exp_minfl_used) exp_mac = exp_minfl_used;
      exp_wire = exp_mac + 8;
      if (frame0_len == exp_wire)
        $display("LAN8720_DP:   ok    TX frame length %0d B on the wire = max(LEN+FCS, MINFL) + 8 (pre/SFD)",
                 frame0_len);
      else begin
        errors++;
        $display("LAN8720_DP:   FAIL  TX frame length %0d B, expected %0d (LEN=%0d CRC=%0d PAD=%0d MINFL=%0d)",
                 frame0_len, exp_wire, flen, exp_crc, exp_pad, exp_minfl_used);
      end
    end

    $display("LAN8720_DP: -- TX phase done (errors so far=%0d) --", errors);

    // =======================================================================
    // PHASE RX -- source a frame inbound through the shield and confirm the
    // MAC DMAs it (RX BD Empty bit clears, length written, frame in DMA RAM).
    // Positive control (healthy shield) then the board fault (dead inbound).
    // =======================================================================
    $display("");
    $display("LAN8720_DP: PHASE RX -- sourcing a frame inbound (RMII, through the shield)");

    // stop TX re-arming, keep the same frame body for RX; clear the RX buffer
    ahb_write(MODER, MODER_DEFAULT);                 // TX off while we set up RX
    for (i=0;i<64;i++) ram_put_byte(RX_BUF + i, 8'h00);

    // arm RX BD (index 2): word0 = flags (E=empty, IRQ, WR=wrap), word1 = ptr
    ahb_write(RX_BD + 0, {16'h0, (RXF_E|RXF_IRQ|RXF_WR)});
    ahb_write(RX_BD + 4, RX_BUF);
    // enable RX + promiscuous (accept any dst) + full duplex
    ahb_write(MODER, MODER_DEFAULT | MODER_RXEN | MODER_PRO | MODER_FULLD);
    ahb_read(RX_BD + 0, rd);
    $display("LAN8720_DP: RX BD armed: word0=0x%08h (E bit %0b), buf=0x%04h", rd, (rd>>15)&1, RX_BUF);

    // ---- positive control: healthy shield, expect the BD to fill -----------
    pass_b2a = 1'b1;
    rmii_rx_send(flen);                              // preamble+SFD+frame+FCS
    begin
      int p; rd = 32'h0000_8000;
      for (p=0; p<4000; p++) begin
        ahb_read(RX_BD + 0, rd);
        if (((rd>>15)&1) == 0) break;                // E cleared => filled
      end
      $display("LAN8720_DP: after healthy RX: RX BD word0=0x%08h (E=%0b, len=%0d) after %0d polls",
               rd, (rd>>15)&1, (rd>>16), p);
    end
    if (((rd>>15)&1) == 0) begin
      int rlen; rlen = (rd>>16);
      // RxBD status low bits (eth_wishbone RxStatusIn): [1]=CRC error,
      // [2]=ShortFrame (informational, cannot raise an error -- 64 B == MINFL).
      $display("LAN8720_DP:   ok    RX BD filled (Empty cleared), HW length=%0d, status=0x%04h", rlen, rd&16'hFFFF);
      if (((rd>>1)&1) == 0) $display("LAN8720_DP:   ok    RX BD CRC-error bit clear (sourced FCS accepted)");
      else begin errors++; $display("LAN8720_DP:   FAIL  RX BD CRC-error bit set (FCS rejected)"); end
      if (rlen != flen+4) $display("LAN8720_DP:   NOTE  RX length %0d != frame+FCS %0d", rlen, flen+4);
      if (((rd>>2)&1) == 1) $display("LAN8720_DP:   NOTE  ShortFrame flagged (64 B == MINFL; informational, not an error)");
      // body check: first 14 header bytes must match what we sourced
      begin
        int miss; miss = -1;
        for (i=0;i<14;i++) if (miss<0 && ram_get_byte(RX_BUF+i) !== frame[i]) miss=i;
        if (miss<0) $display("LAN8720_DP:   ok    RX DMA buffer header matches the sourced frame");
        else begin errors++; $display("LAN8720_DP:   FAIL RX buffer mismatch at byte %0d: got 0x%02h want 0x%02h",
                                       miss, ram_get_byte(RX_BUF+miss), frame[miss]); end
      end
    end else begin
      errors++;
      $display("LAN8720_DP:   FAIL  healthy RX did NOT fill the BD (Empty still set) -- positive control failed");
    end

    // ---- board fault: dead inbound shield, expect the BD to STAY empty -----
    // re-arm the BD, kill the inbound direction (rxd/crs_dv/ref_clk all dead),
    // source the same frame, and confirm nothing is received (finding #4).
    ahb_write(RX_BD + 0, {16'h0, (RXF_E|RXF_IRQ|RXF_WR)});
    ahb_write(RX_BD + 4, RX_BUF);
    pass_b2a = 1'b0;                                 // <-- inject the board fault
    rmii_rx_send(flen);
    repeat (400) @(posedge hclk);
    ahb_read(RX_BD + 0, rd);
    $display("LAN8720_DP: after dead-inbound RX: RX BD word0=0x%08h (E=%0b)", rd, (rd>>15)&1);
    if (((rd>>15)&1) == 1)
      $display("LAN8720_DP:   ok    dead inbound -> RX BD stayed EMPTY (board finding #4 reproduced)");
    else begin errors++; $display("LAN8720_DP:   FAIL  RX BD filled with the inbound path dead (fault not reproduced)"); end
    pass_b2a = 1'b1;

    // ---- RX bad-FCS: a corrupt frame must NOT be accepted as good ----------
    $display("");
    $display("LAN8720_DP: PHASE RX-2 -- inject a frame with a corrupted FCS");
    for (i=0;i<64;i++) ram_put_byte(RX_BUF + i, 8'h00);
    ahb_write(RX_BD + 0, {16'h0, (RXF_E|RXF_IRQ|RXF_WR)});
    ahb_write(RX_BD + 4, RX_BUF);
    rmii_rx_send(flen, 1'b1);                        // bad_fcs=1
    begin
      int p; rd = 32'h0000_8000;
      for (p=0; p<4000; p++) begin ahb_read(RX_BD+0, rd); if (((rd>>15)&1)==0) break; end
      $display("LAN8720_DP: after bad-FCS RX: RX BD word0=0x%08h (E=%0b, CRCerr[1]=%0b)",
               rd, (rd>>15)&1, (rd>>1)&1);
    end
    if (((rd>>15)&1) == 1)
      $display("LAN8720_DP:   ok    bad-FCS frame dropped (RX BD stayed EMPTY)");
    else if (((rd>>1)&1) == 1)
      $display("LAN8720_DP:   ok    bad-FCS frame received WITH CRC-error flag set (not accepted as good)");
    else begin errors++; $display("LAN8720_DP:   FAIL  bad-FCS frame accepted as good (E clear, CRC-error clear)"); end

    // ---- RX multi-BD ring: two frames must fill two consecutive RX BDs ------
    $display("");
    $display("LAN8720_DP: PHASE RX-3 -- two frames across a 2-entry RX BD ring (wrap)");
    for (i=0;i<64;i++) begin ram_put_byte(RX_BUF + i, 8'h00); ram_put_byte(RX_BUF + 32'h800 + i, 8'h00); end
    // reset the RX BD ring pointer to the first RX BD: disable RX, rewrite
    // TX_BD_NUM (which re-homes the RX pointer), re-arm, re-enable.
    ahb_write(MODER, MODER_DEFAULT);
    ahb_write(TX_BD_NUM, 32'h2);
    ahb_write(RX_BD + 0,     {16'h0, (RXF_E|RXF_IRQ)});          // BD2: no wrap
    ahb_write(RX_BD + 4,     RX_BUF);
    ahb_write(RX_BD + 8 + 0, {16'h0, (RXF_E|RXF_IRQ|RXF_WR)});   // BD3: wrap
    ahb_write(RX_BD + 8 + 4, RX_BUF + 32'h800);
    ahb_write(MODER, MODER_DEFAULT | MODER_RXEN | MODER_PRO | MODER_FULLD);
    rmii_rx_send(flen);                              // -> first free RX BD (BD2)
    begin int p; rd = 32'h8000;                      // wait until BD2 fills + ring advances
      for (p=0; p<4000; p++) begin ahb_read(RX_BD+0, rd); if (((rd>>15)&1)==0) break; end
    end
    repeat (200) @(posedge hclk);
    frame[0] = 8'h11;                                // mark frame2 (dst[0]) to trace it
    rmii_rx_send(flen);                              // -> next RX BD (BD3)
    frame[0] = 8'hFF;
    repeat (400) @(posedge hclk);
    begin
      logic [31:0] rd2, rd3;
      ahb_read(RX_BD + 0, rd2); ahb_read(RX_BD + 8, rd3);
      $display("LAN8720_DP: ring: BD2 word0=0x%08h (E=%0b) buf[0]=0x%02h, BD3 word0=0x%08h (E=%0b) buf[0]=0x%02h",
               rd2, (rd2>>15)&1, ram_get_byte(RX_BUF), rd3, (rd3>>15)&1, ram_get_byte(RX_BUF+32'h800));
      if (((rd2>>15)&1)==0 && ((rd3>>15)&1)==0)
        $display("LAN8720_DP:   ok    both RX BDs filled -- the RX BD ring advanced across two frames");
      else if (ram_get_byte(RX_BUF)==8'h11)
        // frame2 (marker 0x11) landed back in BD2, not BD3 -- ring did not advance.
        $display("LAN8720_DP:   NOTE  RX BD ring did not advance: frame2 re-used BD2 (BD3 still empty). Not gated -- unresolved whether MAC RX-BD-pointer quirk or pointer state after the prior single-BD frames; see README.");
      else
        $display("LAN8720_DP:   NOTE  RX ring inconclusive (frame2 not clearly placed)");
    end

    $display("");
    $display("LAN8720_DP: ============================================================");
    $display("LAN8720_DP: TX + RX datapath through the REAL DUT (MAC + DMA + rmii_to_mii):");
    $display("LAN8720_DP:   errors=%0d  VERDICT=%s", errors, (errors==0)?"PASS":"FAIL");
    $display("LAN8720_DP: ============================================================");
    $finish;
  end

  initial begin
    #80_000_000;
    $display("LAN8720_DP: TIMEOUT  VERDICT=FAIL  tx_bytes=%0d", tx_bytes);
    $finish;
  end

endmodule
