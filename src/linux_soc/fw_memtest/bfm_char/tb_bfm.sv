//-----------------------------------------------------------------------------
// bfm_char/tb_bfm.sv -- characterise the DDR4 BFM's AXI4 slave, by hand.
//
// No CPU. No cache. No SmartConnect. This TB IS the AXI master, so AWSIZE,
// WSTRB, AWLEN and ARSIZE are exactly what is written below and the BFM's reply
// is attributable to the BFM alone.
//
// The question being answered:
//   Does the DDR4 BFM honour a NARROW (32-bit, AWSIZE=2) write into its 512-bit
//   AXI slave -- i.e. does a 4-byte store come back on a 4-byte load?
//
// The SoC cannot issue anything else: the MicroBlaze-V is a 32-bit master and
// the MIG slave is 512-bit, so every CPU store is a narrow beat with 4 of 64
// WSTRB lanes set. If the BFM drops those, no correct design can ever pass a
// memtest against it.
//
// Sampling is on the NEGEDGE: the BFM is a SystemC/TLM model whose outputs do
// not obey Verilog NBA scheduling, so a posedge monitor races it.
//-----------------------------------------------------------------------------

`timescale 1ps/1ps

module tb_bfm;

    localparam int SYS_CLK_PS = 10000;   // 100 MHz differential reference

    bit  c0_sys_clk_p = 1'b0;
    wire c0_sys_clk_n = ~c0_sys_clk_p;
    always #(SYS_CLK_PS/2) c0_sys_clk_p = ~c0_sys_clk_p;

    // ddr4_0/sys_rst is ACTIVE HIGH (ddr4_ip.tcl header).
    logic sys_rst = 1'b1;

    // c0_ddr4_aresetn is an ACTIVE-LOW *INPUT* -- the AXI-slave reset. Leaving it
    // dangling is the exact bug that wedged the SoC; here the TB drives it.
    logic aresetn = 1'b0;

    wire        ui_clk;
    wire        ui_clk_sync_rst;
    wire        init_calib_complete;

    // ---- AXI4 slave (512b data / 64b strb / 32b addr / 4b id) ----
    logic [3:0]   awid   = '0;
    logic [31:0]  awaddr = '0;
    logic [7:0]   awlen  = '0;
    logic [2:0]   awsize = '0;
    logic [1:0]   awburst= 2'b01;   // INCR
    logic         awvalid= 1'b0;
    wire          awready;

    logic [511:0] wdata  = '0;
    logic [63:0]  wstrb  = '0;
    logic         wlast  = 1'b0;
    logic         wvalid = 1'b0;
    wire          wready;

    wire [3:0]    bid;
    wire [1:0]    bresp;
    wire          bvalid;
    logic         bready = 1'b1;

    logic [3:0]   arid   = '0;
    logic [31:0]  araddr = '0;
    logic [7:0]   arlen  = '0;
    logic [2:0]   arsize = '0;
    logic [1:0]   arburst= 2'b01;
    logic         arvalid= 1'b0;
    wire          arready;

    wire [3:0]    rid;
    wire [511:0]  rdata;
    wire [1:0]    rresp;
    wire          rlast;
    wire          rvalid;
    logic         rready = 1'b1;

    ddr4_char_wrapper dut (
        .C0_SYS_CLK_0_clk_p            (c0_sys_clk_p),
        .C0_SYS_CLK_0_clk_n            (c0_sys_clk_n),
        .sys_rst_0                     (sys_rst),
        .c0_ddr4_aresetn_0             (aresetn),
        .c0_ddr4_ui_clk_0              (ui_clk),
        .c0_ddr4_ui_clk_sync_rst_0     (ui_clk_sync_rst),
        .c0_init_calib_complete_0      (init_calib_complete),

        .C0_DDR4_S_AXI_0_awid          (awid),
        .C0_DDR4_S_AXI_0_awaddr        (awaddr),
        .C0_DDR4_S_AXI_0_awlen         (awlen),
        .C0_DDR4_S_AXI_0_awsize        (awsize),
        .C0_DDR4_S_AXI_0_awburst       (awburst),
        .C0_DDR4_S_AXI_0_awlock        (1'b0),
        .C0_DDR4_S_AXI_0_awcache       (4'b0011),
        .C0_DDR4_S_AXI_0_awprot        (3'b000),
        .C0_DDR4_S_AXI_0_awqos         (4'b0000),
        .C0_DDR4_S_AXI_0_awvalid       (awvalid),
        .C0_DDR4_S_AXI_0_awready       (awready),

        .C0_DDR4_S_AXI_0_wdata         (wdata),
        .C0_DDR4_S_AXI_0_wstrb         (wstrb),
        .C0_DDR4_S_AXI_0_wlast         (wlast),
        .C0_DDR4_S_AXI_0_wvalid        (wvalid),
        .C0_DDR4_S_AXI_0_wready        (wready),

        .C0_DDR4_S_AXI_0_bid           (bid),
        .C0_DDR4_S_AXI_0_bresp         (bresp),
        .C0_DDR4_S_AXI_0_bvalid        (bvalid),
        .C0_DDR4_S_AXI_0_bready        (bready),

        .C0_DDR4_S_AXI_0_arid          (arid),
        .C0_DDR4_S_AXI_0_araddr        (araddr),
        .C0_DDR4_S_AXI_0_arlen         (arlen),
        .C0_DDR4_S_AXI_0_arsize        (arsize),
        .C0_DDR4_S_AXI_0_arburst       (arburst),
        .C0_DDR4_S_AXI_0_arlock        (1'b0),
        .C0_DDR4_S_AXI_0_arcache       (4'b0011),
        .C0_DDR4_S_AXI_0_arprot        (3'b000),
        .C0_DDR4_S_AXI_0_arqos         (4'b0000),
        .C0_DDR4_S_AXI_0_arvalid       (arvalid),
        .C0_DDR4_S_AXI_0_arready       (arready),

        .C0_DDR4_S_AXI_0_rid           (rid),
        .C0_DDR4_S_AXI_0_rdata         (rdata),
        .C0_DDR4_S_AXI_0_rresp         (rresp),
        .C0_DDR4_S_AXI_0_rlast         (rlast),
        .C0_DDR4_S_AXI_0_rvalid        (rvalid),
        .C0_DDR4_S_AXI_0_rready        (rready)
    );

    int n_fail = 0;
    int n_pass = 0;

    //-------------------------------------------------------------------------
    // Single-beat write of `nbytes` at `addr`, data placed on the correct byte
    // lanes with the correct WSTRB -- i.e. a textbook AXI narrow transfer, the
    // same shape the CPU's stores arrive as.
    //-------------------------------------------------------------------------
    // HANDSHAKE DISCIPLINE (this is the whole ballgame against a TLM model):
    //   * DRIVE with blocking assignments immediately after a negedge, so the
    //     stimulus is stable for the entire cycle that the slave samples.
    //   * DETECT the transfer by seeing `ready` at a NEGEDGE while our `valid` is
    //     already high -- that negedge is inside the cycle whose POSEDGE performs
    //     the transfer. Sampling `ready` on the posedge races the SystemC model
    //     (its outputs do not obey NBA scheduling); sampling it at the negedge
    //     AFTER the posedge misses a ready that the slave has already dropped.
    task automatic axi_write_narrow(input [31:0] addr, input [31:0] data, input int size_log2);
        int lane;
        int nbytes;
        bit aw_done, w_done;
        begin
            nbytes = 1 << size_log2;
            lane   = addr[5:0];                    // byte lane inside the 64-byte bus

            @(negedge ui_clk);
            awid    = 4'd0;
            awaddr  = addr;
            awlen   = 8'd0;                        // single beat
            awsize  = size_log2[2:0];
            awburst = 2'b01;
            awvalid = 1'b1;

            wdata   = '0;
            wstrb   = '0;
            for (int b = 0; b < nbytes; b++) begin
                wdata[8*(lane+b) +: 8] = data[8*b +: 8];
                wstrb[lane+b]          = 1'b1;
            end
            wlast   = 1'b1;
            wvalid  = 1'b1;

            aw_done = 1'b0;
            w_done  = 1'b0;
            while (!aw_done || !w_done) begin
                // We are at a negedge with valid(s) asserted. Whatever `ready` reads
                // now is what the upcoming posedge will use.
                if (awvalid && awready) aw_done = 1'b1;
                if (wvalid  && wready ) w_done  = 1'b1;
                @(posedge ui_clk);                 // the transfer(s) happen here
                @(negedge ui_clk);
                if (aw_done) awvalid = 1'b0;
                if (w_done ) begin wvalid = 1'b0; wlast = 1'b0; end
            end

            // Wait for the write response.
            while (!bvalid) @(negedge ui_clk);
            $display("[BFM] WR addr=0x%08h awsize=%0d(%0dB) lane=%0d wstrb=0x%016h data=0x%08h -> BRESP=%0d",
                     addr, size_log2, nbytes, lane, wstrb_of(lane, nbytes), data, bresp);
            @(posedge ui_clk);
            @(negedge ui_clk);
        end
    endtask

    function automatic [63:0] wstrb_of(input int lane, input int nbytes);
        wstrb_of = '0;
        for (int b = 0; b < nbytes; b++) wstrb_of[lane+b] = 1'b1;
    endfunction

    //-------------------------------------------------------------------------
    // Single-beat read; returns the `nbytes` from the addressed byte lanes.
    //-------------------------------------------------------------------------
    task automatic axi_read_narrow(input [31:0] addr, input int size_log2, output [31:0] data);
        int lane;
        int nbytes;
        logic [511:0] cap;
        begin
            nbytes = 1 << size_log2;
            lane   = addr[5:0];

            @(negedge ui_clk);
            arid    = 4'd0;
            araddr  = addr;
            arlen   = 8'd0;
            arsize  = size_log2[2:0];
            arburst = 2'b01;
            arvalid = 1'b1;

            while (!arready) @(negedge ui_clk);   // ready seen at negedge -> xfer at next posedge
            @(posedge ui_clk);
            @(negedge ui_clk);
            arvalid = 1'b0;

            while (!rvalid) @(negedge ui_clk);
            cap = rdata;
            data = '0;
            for (int b = 0; b < nbytes; b++)
                data[8*b +: 8] = cap[8*(lane+b) +: 8];
            @(posedge ui_clk);
            @(negedge ui_clk);
        end
    endtask

    // T3 helper: a FULL-WIDTH 512-bit beat (AWSIZE=6, all 64 WSTRB lanes) -- the
    // only shape a SUPPORTS_NARROW_BURST=0 transactor models correctly.
    task automatic full_width_test();
        logic [511:0] cap;
        bit aw_done, w_done;
        int bad;
        begin
            @(negedge ui_clk);
            awid = 0; awaddr = 32'h200; awlen = 0; awsize = 3'd6; awburst = 2'b01; awvalid = 1'b1;
            for (int b = 0; b < 64; b++) wdata[8*b +: 8] = b[7:0] ^ 8'hA5;
            wstrb = {64{1'b1}}; wlast = 1'b1; wvalid = 1'b1;

            aw_done = 1'b0; w_done = 1'b0;
            while (!aw_done || !w_done) begin
                if (awvalid && awready) aw_done = 1'b1;
                if (wvalid  && wready ) w_done  = 1'b1;
                @(posedge ui_clk);
                @(negedge ui_clk);
                if (aw_done) awvalid = 1'b0;
                if (w_done ) begin wvalid = 1'b0; wlast = 1'b0; end
            end
            while (!bvalid) @(negedge ui_clk);
            $display("[BFM] WR512 addr=0x00000200 awsize=6 wstrb=all -> BRESP=%0d", bresp);
            @(posedge ui_clk); @(negedge ui_clk);

            arid = 0; araddr = 32'h200; arlen = 0; arsize = 3'd6; arburst = 2'b01; arvalid = 1'b1;
            while (!arready) @(negedge ui_clk);
            @(posedge ui_clk); @(negedge ui_clk);
            arvalid = 1'b0;
            while (!rvalid) @(negedge ui_clk);
            cap = rdata;
            @(posedge ui_clk); @(negedge ui_clk);

            bad = 0;
            for (int b = 0; b < 64; b++)
                if (cap[8*b +: 8] !== (b[7:0] ^ 8'hA5)) bad++;
            if (bad == 0) begin
                n_pass++;
                $display("[BFM] RD512 addr=0x00000200  all 64 bytes OK");
            end else begin
                n_fail++;
                $display("[BFM] RD512 addr=0x00000200  %0d/64 bytes WRONG  got=0x%0h", bad, cap);
            end
        end
    endtask

    task automatic check(input [31:0] addr, input [31:0] exp, input int size_log2);
        logic [31:0] got;
        begin
            axi_read_narrow(addr, size_log2, got);
            if (got === exp) begin
                n_pass++;
                $display("[BFM] RD addr=0x%08h exp=0x%08h got=0x%08h   OK", addr, exp, got);
            end else begin
                n_fail++;
                $display("[BFM] RD addr=0x%08h exp=0x%08h got=0x%08h   *** MISMATCH ***", addr, exp, got);
            end
        end
    endtask

    initial begin
        $display("=========================================================");
        $display(" tb_bfm : DDR4 BFM AXI-slave characterisation");
        $display("=========================================================");

        repeat (200) @(posedge c0_sys_clk_p);
        sys_rst = 1'b0;
        $display("[BFM] %0t ps : sys_rst released", $time);

        // Wait for calibration -- but do NOT hang on it. The DDR4 *TLM* model does
        // not model calibration at all (it never drives c0_init_calib_complete),
        // while the RTL model asserts it at ~6.9 us. Both are usable; only the RTL
        // one has a calibration to wait for. Report which world we are in.
        fork : calib_wait
            begin wait (init_calib_complete === 1'b1);
                  $display("[BFM] %0t ps : init_calib_complete ASSERTED", $time); end
            begin #20_000_000;
                  if (init_calib_complete !== 1'b1)
                      $display("[BFM] %0t ps : init_calib_complete NEVER asserted (TLM model does not model calibration) -- proceeding", $time); end
        join_any
        disable calib_wait;
        repeat (20) @(negedge ui_clk);
        aresetn = 1'b1;
        $display("[BFM] %0t ps : c0_ddr4_aresetn released", $time);
        repeat (20) @(negedge ui_clk);

        //---------------------------------------------------------------------
        // T1 -- NARROW 32-bit stores, the shape the CPU actually issues.
        //       Four consecutive words inside ONE 64-byte beat, then a word in
        //       the next beat. If the BFM ignores AWSIZE these will corrupt each
        //       other (each 4-byte store commits all 64 bytes of its beat).
        //---------------------------------------------------------------------
        $display("\n--- T1: narrow 32-bit (AWSIZE=2) writes, address-in-address ---");
        axi_write_narrow(32'h0000_0000, 32'h0000_0000, 2);
        axi_write_narrow(32'h0000_0004, 32'h0000_0004, 2);
        axi_write_narrow(32'h0000_0008, 32'h0000_0008, 2);
        axi_write_narrow(32'h0000_000C, 32'h0000_000C, 2);
        axi_write_narrow(32'h0000_0040, 32'h0000_0040, 2);

        $display("--- T1: read back ---");
        check(32'h0000_0000, 32'h0000_0000, 2);
        check(32'h0000_0004, 32'h0000_0004, 2);
        check(32'h0000_0008, 32'h0000_0008, 2);
        check(32'h0000_000C, 32'h0000_000C, 2);
        check(32'h0000_0040, 32'h0000_0040, 2);

        //---------------------------------------------------------------------
        // T2 -- walking-1s on the data bus, one address. Exercises every WSTRB
        //       lane combination a 32-bit master can produce.
        //---------------------------------------------------------------------
        $display("\n--- T2: walking-1s data pattern @0x100 (narrow) ---");
        for (int i = 0; i < 32; i++) begin
            logic [31:0] pat;
            pat = 32'h1 << i;
            axi_write_narrow(32'h0000_0100, pat, 2);
            check(32'h0000_0100, pat, 2);
        end

        //---------------------------------------------------------------------
        // T3 -- FULL-WIDTH 512-bit beat (AWSIZE=6, all 64 WSTRB lanes). This is
        //       the ONLY shape a SUPPORTS_NARROW_BURST=0 transactor models
        //       correctly. If T3 passes while T1 fails, the defect is proven to
        //       be the BFM's narrow-burst handling and nothing else.
        //---------------------------------------------------------------------
        $display("\n--- T3: FULL-WIDTH 512-bit (AWSIZE=6) write/read @0x200 ---");
        full_width_test();

        $display("\n=========================================================");
        $display(" BFMCHAR RESULT: pass=%0d fail=%0d", n_pass, n_fail);
        if (n_fail == 0) $display(" BFMCHAR: NARROW ACCESSES ARE MODELLED CORRECTLY");
        else             $display(" BFMCHAR: BFM MIS-MODELS NARROW ACCESSES (see above)");
        $display("=========================================================");
        $finish;
    end

    // Count ui_clk edges. If the DDR4 model never drives its own ui_clk, the AXI
    // transactor has no clock and NOTHING can ever happen -- that is a different
    // failure from "the memory returned the wrong data" and must be told apart.
    int ui_edges = 0;
    always @(posedge ui_clk) ui_edges++;

    // Handshake watchdog: if a transfer stalls, say WHY rather than just timing out.
    initial begin
        forever begin
            #5_000_000;   // every 5 us
            $display("[BFM] hb t=%0t aresetn=%0b calib=%0b ui_edges=%0d | awvalid=%0b awready=%0b wvalid=%0b wready=%0b bvalid=%0b arvalid=%0b arready=%0b rvalid=%0b",
                     $time, aresetn, init_calib_complete, ui_edges,
                     awvalid, awready, wvalid, wready, bvalid, arvalid, arready, rvalid);
        end
    end

    // Safety net -- never let the bench hang the overnight run.
    initial begin
        #190_000_000;  // 190 us
        $display("[BFM] TIMEOUT: pass=%0d fail=%0d (bench did not complete)", n_pass, n_fail);
        $display(" BFMCHAR RESULT: TIMEOUT");
        $finish;
    end

endmodule
