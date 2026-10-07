// -----------------------------------------------------------------------------
// rp_eth_ss_wrapper.sv — Reconfigurable Module (RM) top for the eth_ss DUT.
//
// A joint work commissioned on behalf of SoC Labs, under Arm Academic Access
// license.
//
// A1 fill (2026-07-06): instantiates the REAL standalone AHB-MAC + PTP
// subsystem (`ethmac_subsystem_ahb`, from the read-only
// $ETH_SS_HOME (ethernet-subsystem-ahb) checkout —
// see README.md "Which checkout and why") and wires it to the partition-pin
// boundary. Ports are EXACTLY the RP side of docs/contracts/partition-pins.md
// v0.1 (directions mirrored from the shell's view — identical port list to
// rp_nanosoc_wrapper.sv / rm_greybox.sv / rm_led.sv).
//
// Unlike rm_nanosoc, this DUT actually HAS a MAC: the contract's RMII + MDIO
// group is REAL here. The MAC core is MII, so the source repo's own proven
// rmii_to_mii bridge (ethernet-mac-ahb/amba_wb_bridges — the exact module the
// upstream eth_ss_vivado_wrapper.v uses on PYNQ/MPS3) adapts it to the
// contract's RMII pins inside this RM.
//
// There is deliberately NO CPU, NO console UART and NO JTAG TAP in this RM —
// the whole point of rm_eth_ss is a MAC-in-operation DUT variant. The AHB
// slave port that firmware would normally program is driven by
// eth_ss_bringup.sv, a one-shot constant-programmer FSM (AHB decision option
// (b) — full rationale + register list in README.md and in that file's
// header). The MAC's frame DMA lands in a small RM-internal AHB SRAM
// (sl_ahb_sram) so received frames genuinely complete, descriptors recycle
// and int_o (-> irq_out) fires — observable MAC life at the boundary without
// any software.
//
// No AXI(-Lite)/AHB ports at this boundary by design: partition-pins.md
// line 8 — "no shell<->DUT AXI in v0". Do not add any without an A6 contract
// change.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module rp_eth_ss_wrapper #(
  parameter int NGPIO = 16,   // partition-pins.md "Board-port / GPIO
                              // passthrough -- I4": build parameter, v0
                              // default 16. Exposed per fpga/dfx/rms/
                              // README.md RM authoring contract item 2.
                              // Tied off in this RM (no GPIO source).
  parameter bit MODE_SPEED_100 = 1'b1,  // rmii_to_mii link speed:
                              // 1 = 100 Mbps (shell virtual PHY default),
                              // 0 = 10 Mbps. Quasi-static.
  parameter int DMA_RAM_AW = 13,        // internal frame-DMA SRAM address
                              // width: 13 -> 8 KB (>= 2 max-size frames).
  parameter logic [31:0] RTC_PERIOD_NS   = 32'd40,  // PTP RTC nominal period,
                              // integer ns: 40 ns assumes dut_clk = 25 MHz
                              // (rtc_clk = dut_clk below). MUST track the
                              // shell's real dut_clk — A6 open point, same
                              // class as rm_nanosoc's UART_CLK_HZ.
  parameter logic [31:0] RTC_PERIOD_FRAC = 32'd0,   // fractional ns (2^-32 ns)
  parameter SWAP_DMA_BYTES = 1'b1       // passthrough to wb_to_ahb3lite on
                              // the DMA path; 1 = little-endian host layout
                              // (upstream default; moot in v1, nothing reads
                              // the DMA SRAM)
) (
  // =========================================================================
  // Clocks & resets — shell -> RP (partition-pins.md "Clocks & resets").
  // All DUT clocks are generated in the static shell; no clock generation
  // inside this RM at the boundary (the rmii_to_mii bridge's internal /2
  // MII clocks are an RM-internal matter — see README.md "Clocking").
  // Resets arrive pre-synchronized-for-deassert from the shell.
  //
  // WIRING DECISION (see README.md "Reset combination"): the subsystem has
  // exactly ONE reset input (HRESETn). dut_resetn & rp_resetn are ANDed
  // into it; dbg_resetn is deliberately NOT folded in — this DUT has no
  // debug logic, and an OpenOCD SRST pulse must not kill a MAC mid-frame.
  // =========================================================================
  input  logic dut_clk,       // DRP-reconfigurable DUT system clock
  input  logic dut_resetn,    // reset #1 — DUT system reset (host register)
  input  logic rp_resetn,     // reset #2 — reconfig reset, held through swap
  input  logic dbg_resetn,    // reset #3 — debug/SRST (unused: no debug logic)

  // =========================================================================
  // Processor debug — internal JTAG / SWJ-DP (partition-pins.md "Processor
  // debug"). TIE-OFF: no Cortex-M, no JTAG TAP in this RM. TCK/TMS/TDI are
  // ignored and TDO is driven to a benign idle 0 (a JTAG TDO is a pure
  // unidirectional output — no shared-wire reflection idiom applies).
  // =========================================================================
  input  logic jtag_tck,      // unused (no TAP)
  input  logic jtag_tms,      // unused (no TAP)
  input  logic jtag_tdi,      // unused (no TAP)
  output logic jtag_tdo,

  // =========================================================================
  // RM debug — BSCAN to an RM debug hub (partition-pins.md "RM debug",
  // 2026-10 ILA mint). This RM carries no hub: the 11 legs are ignored and
  // dbg_bscan_tdo is tied low.
  // =========================================================================
  input  logic dbg_bscan_bscanid_en,
  input  logic dbg_bscan_capture,
  input  logic dbg_bscan_drck,
  input  logic dbg_bscan_reset,
  input  logic dbg_bscan_runtest,
  input  logic dbg_bscan_sel,
  input  logic dbg_bscan_shift,
  input  logic dbg_bscan_tck,
  input  logic dbg_bscan_tdi,
  input  logic dbg_bscan_tms,
  input  logic dbg_bscan_update,
  output logic dbg_bscan_tdo,

  // =========================================================================
  // Ethernet — RMII + MDIO (partition-pins.md "Ethernet"). REAL signals:
  // shell is the virtual PHY, this RM's OpenCores MAC is the station.
  // MII<->RMII adaptation via the source repo's own rmii_to_mii bridge.
  //
  // HDPR-29 / IOB packing note: phy_rmii_txd/tx_en leave this RM as PLAIN
  // FABRIC SIGNALS (registered in the bridge, but with no IOB/OLOGIC
  // attributes) — the pad re-register stage lives shell-side.
  // =========================================================================
  input  logic       phy_rmii_ref_clk,  // 50 MHz REF_CLK from shell
  input  logic       phy_rmii_crs_dv,
  input  logic [1:0] phy_rmii_rxd,
  output logic [1:0] phy_rmii_txd,
  output logic       phy_rmii_tx_en,
  output logic       mdc,        // DUT is MDIO master (eth_miim inside MAC)
  output logic       mdio_o,
  output logic       mdio_oe,
  input  logic       mdio_i,     // virtual-PHY register model's reply

  // =========================================================================
  // Console / trace — AXI-Stream byte (partition-pins.md "Console / trace").
  // TIE-OFF: eth_ss has no console UART (no CPU to print). TX stream held
  // valid=0 (never offers a byte); RX stream swallowed with ready=1 (host
  // bytes are accepted-and-dropped so the shell FIFO can never back up).
  // =========================================================================
  output logic [7:0] uart_tx_tdata,
  output logic       uart_tx_tvalid,
  input  logic       uart_tx_tready,
  input  logic [7:0] uart_rx_tdata,
  input  logic       uart_rx_tvalid,
  output logic       uart_rx_tready,
  output logic       swo,        // no Cortex-M => no ITM/SWO: tied 0

  // =========================================================================
  // Status / misc — RP -> shell (partition-pins.md "Status / misc").
  // =========================================================================
  output logic [31:0] rm_id,       // 0x00000002 (rm_list.tcl rm_eth_ss)
  output logic        dut_lockup,  // no CPU to lock up: tied 0
  output logic        irq_out,     // REAL: MAC int_o | eth_rx_cksum cksum_int_o

  // =========================================================================
  // Board-port / GPIO passthrough — shell <-> RP (I4). TIE-OFF: no GPIO
  // source in this DUT; oe=0 so the shell never drives a pad on our behalf.
  // =========================================================================
  output logic [NGPIO-1:0] dut_gpio_o,
  output logic [NGPIO-1:0] dut_gpio_oe,
  input  logic [NGPIO-1:0] dut_gpio_i,  // unused

  // =========================================================================
  // Flash / QSPI XiP — RP <-> shell (partition-pins.md v0.2). TIE-OFF: the
  // eth_ss DUT has no QSPI controller, so drive the shared external flash to
  // safe idle (deselected, clock low, lanes tri-stated) — it must never
  // disturb the SST26 pads while an eth_ss RM is resident. io_i unused.
  // =========================================================================
  output logic       qspi_sclk,
  output logic       qspi_csn,
  output logic [3:0] qspi_io_o,
  output logic [3:0] qspi_io_oe,
  input  logic [3:0] qspi_io_i   // unused
);

  // ===========================================================================
  // RM identity — per rm_list.tcl RM_LIB(rm_eth_ss,rm_id) = 0x01000002.
  // Real, permanently-driven constant (RM authoring contract item 4).
  //
  // ENCODING v2 (docs/VERSIONING_PLAN.md §3.2):
  //     { ver_major[31:24], ver_minor[23:16], design_id[15:0] }
  //   design_id 0x0002 (UNCHANGED — the low half preserves the old id),
  //   version   1.0     => 0x01000002.
  // Keep in lockstep with rm_list.tcl; check_rm_id_encoding.py gates it.
  // ===========================================================================
  localparam logic [31:0] RM_ID_ETH_SS = 32'h0100_0002;

  assign rm_id = RM_ID_ETH_SS;

  // ===========================================================================
  // Reset combination — see header note / README.md. dbg_resetn is
  // intentionally unused (no debug domain in this RM).
  // ===========================================================================
  logic hresetn;
  assign hresetn = dut_resetn & rp_resetn;

  // ===========================================================================
  // Tie-offs: JTAG / console / trace / status / GPIO (contract item 5 —
  // everything not implemented is a safe, legally-driven, inert constant).
  // ===========================================================================
  assign jtag_tdo       = 1'b0;               // no TAP: TDO idle low
  assign dbg_bscan_tdo  = 1'b0;  // no RM debug hub (2026-10 ILA mint boundary)
  assign uart_tx_tdata  = 8'h00;
  assign uart_tx_tvalid = 1'b0;
  assign uart_rx_tready = 1'b1;               // swallow host bytes
  assign swo            = 1'b0;
  assign dut_lockup     = 1'b0;
  assign dut_gpio_o     = {NGPIO{1'b0}};
  assign dut_gpio_oe    = {NGPIO{1'b0}};
  // QSPI flash: safe idle (deselected, clock low, lanes tri-stated).
  assign qspi_sclk      = 1'b0;
  assign qspi_csn       = 1'b1;
  assign qspi_io_o      = 4'b0000;
  assign qspi_io_oe     = 4'b0000;

  // ===========================================================================
  // Internal AHB-Lite bus #1: bring-up programmer -> subsystem slave port.
  // Single-master single-slave: HSEL tied 1, HREADY = slave's own HREADYOUT.
  // ===========================================================================
  logic [31:0] cfg_haddr;
  logic  [1:0] cfg_htrans;
  logic        cfg_hwrite;
  logic  [2:0] cfg_hsize;
  logic  [2:0] cfg_hburst;
  logic  [3:0] cfg_hprot;
  logic        cfg_hmastlock;
  logic [31:0] cfg_hwdata;
  logic [31:0] cfg_hrdata;
  logic        cfg_hreadyout;
  logic        cfg_hresp;

  logic        bringup_done;     // RM-internal telemetry (no partition pin
  logic        bringup_errored;  // in v0.1 — candidates for a future gpio bit)

  eth_ss_bringup #(
    .RTC_PERIOD_NS   (RTC_PERIOD_NS),
    .RTC_PERIOD_FRAC (RTC_PERIOD_FRAC)
  ) u_bringup (
    .clk       (dut_clk),
    .resetn    (hresetn),
    .haddr     (cfg_haddr),
    .htrans    (cfg_htrans),
    .hwrite    (cfg_hwrite),
    .hsize     (cfg_hsize),
    .hburst    (cfg_hburst),
    .hprot     (cfg_hprot),
    .hmastlock (cfg_hmastlock),
    .hwdata    (cfg_hwdata),
    .hrdata    (cfg_hrdata),
    .hready    (cfg_hreadyout),   // single-slave loop
    .hresp     (cfg_hresp),
    .done      (bringup_done),
    .errored   (bringup_errored)
  );

  // ===========================================================================
  // Internal AHB-Lite bus #2: subsystem DMA master -> internal frame SRAM.
  // The OpenCores MAC fetches/stores frame data via its Wishbone master
  // (wb_to_ahb3lite inside the subsystem); with no shell<->DUT AHB in the
  // contract, the only legal DMA target is RM-internal memory. sl_ahb_sram
  // (source repo fpga_lib) = cmsdk_ahb_to_sram + cmsdk_fpga_sram, 8 KB.
  // Address decode: low DMA_RAM_AW bits, HSEL=1 (mirrors everywhere in the
  // 4 GB space — bring-up programs BD pointers at offset 0x0, so aliasing
  // is unreachable in practice and harmless if reached).
  // ===========================================================================
  logic [31:0] dma_haddr;
  logic  [1:0] dma_htrans;
  logic  [2:0] dma_hsize;
  logic  [2:0] dma_hburst;
  logic  [3:0] dma_hprot;
  logic        dma_hmastlock;
  logic        dma_hwrite;
  logic [31:0] dma_hwdata;
  logic [31:0] dma_hrdata;
  logic        dma_hreadyout;
  logic        dma_hresp;

  sl_ahb_sram #(
    .SYS_DATA_W (32),
    .RAM_ADDR_W (DMA_RAM_AW),
    .RAM_DATA_W (32)
  ) u_dma_ram (
    .HCLK      (dut_clk),
    .HRESETn   (hresetn),
    .HSEL      (1'b1),
    .HREADY    (dma_hreadyout),   // single-slave loop
    .HTRANS    (dma_htrans),
    .HSIZE     (dma_hsize),
    .HWRITE    (dma_hwrite),
    .HADDR     (dma_haddr[DMA_RAM_AW-1:0]),
    .HWDATA    (dma_hwdata),
    .HREADYOUT (dma_hreadyout),
    .HRESP     (dma_hresp),
    .HRDATA    (dma_hrdata)
  );

  // ===========================================================================
  // MII <-> RMII bridge — the source repo's own proven adapter (same module,
  // same wiring as upstream fpga/vivado_ip/eth_ss_vivado_wrapper.v). Its /2
  // mtx_clk/mrx_clk MII clocks are generated and consumed entirely inside
  // this RM (never cross the partition boundary); RESETn has an internal
  // 3-stage synchronizer onto phy_rmii_ref_clk.
  // ===========================================================================
  logic       mii_tx_clk;
  logic [3:0] mii_txd;
  logic       mii_tx_en;
  logic       mii_tx_err;
  logic       mii_rx_clk;
  logic [3:0] mii_rxd;
  logic       mii_rx_dv;
  logic       mii_rx_err;
  logic       mii_coll;
  logic       mii_crs;

  rmii_to_mii u_rmii_bridge (
    .RESETn       (hresetn),
    .mode_speed   (MODE_SPEED_100),

    // RMII side — the contract's partition pins, directly
    .rmii_ref_clk (phy_rmii_ref_clk),
    .rmii_txd     (phy_rmii_txd),
    .rmii_tx_en   (phy_rmii_tx_en),
    .rmii_rxd     (phy_rmii_rxd),
    .rmii_crs_dv  (phy_rmii_crs_dv),

    // MII side — to the subsystem MAC
    .mtx_clk      (mii_tx_clk),
    .mtxd         (mii_txd),
    .mtxen        (mii_tx_en),
    .mtxerr       (mii_tx_err),
    .mrx_clk      (mii_rx_clk),
    .mrxd         (mii_rxd),
    .mrxdv        (mii_rx_dv),
    .mrxerr       (mii_rx_err),
    .mcoll        (mii_coll),
    .mcrs         (mii_crs)
  );

  // ===========================================================================
  // The subsystem proper: OpenCores ethmac + HA1588 PTP + eth_rx_cksum
  // behind one AHB-Lite slave (register window) + one AHB-Lite DMA master.
  // ===========================================================================
  logic eth_int;
  logic cksum_int;

  ethmac_subsystem_ahb #(
    .SYS_ADDR_W     (32),
    .SYS_DATA_W     (32),
    .SWAP_DMA_BYTES (SWAP_DMA_BYTES)
  ) u_eth_ss (
    .HCLK      (dut_clk),
    .HRESETn   (hresetn),

    // AHB slave — driven by the bring-up programmer
    .HSEL      (1'b1),
    .HADDR     (cfg_haddr),
    .HTRANS    (cfg_htrans),
    .HWRITE    (cfg_hwrite),
    .HSIZE     (cfg_hsize),
    .HBURST    (cfg_hburst),
    .HPROT     (cfg_hprot),
    .HWDATA    (cfg_hwdata),
    .HMASTLOCK (cfg_hmastlock),
    .HREADY    (cfg_hreadyout),   // single-slave loop
    .HRDATA    (cfg_hrdata),
    .HREADYOUT (cfg_hreadyout),
    .HRESP     (cfg_hresp),

    // AHB master (frame DMA) — into the internal SRAM
    .ahb_master_haddr     (dma_haddr),
    .ahb_master_htrans    (dma_htrans),
    .ahb_master_hsize     (dma_hsize),
    .ahb_master_hburst    (dma_hburst),
    .ahb_master_hprot     (dma_hprot),
    .ahb_master_hmastlock (dma_hmastlock),
    .ahb_master_hwrite    (dma_hwrite),
    .ahb_master_hwdata    (dma_hwdata),
    .ahb_master_hrdata    (dma_hrdata),
    .ahb_master_hready    (dma_hreadyout),
    .ahb_master_hresp     (dma_hresp),

    // PTP RTC clock: dut_clk (same choice as upstream eth_ss_vivado_wrapper's
    // sys_fclk). RTC_PERIOD_NS parameter must match — see README "Clocking".
    .rtc_clk           (dut_clk),
    .rtc_time_ptp_ns   (),    // RM-internal PTP time bond-outs: no
    .rtc_time_ptp_sec  (),    // partition-pins.md home (v1+ candidate)
    .rtc_time_one_pps  (),

    // MII — via the RMII bridge above
    .mtx_clk_i (mii_tx_clk),
    .mtxd_o    (mii_txd),
    .mtxen_o   (mii_tx_en),
    .mtxerr_o  (mii_tx_err),
    .mrx_clk_i (mii_rx_clk),
    .mrxd_i    (mii_rxd),
    .mrxdv_i   (mii_rx_dv),
    .mrxerr_i  (mii_rx_err),
    .mcoll_i   (mii_coll),
    .mcrs_i    (mii_crs),

    // MDIO — REAL contract pins, direct (md_padoe_o is active-high drive
    // enable, matching the contract's mdio_oe sense)
    .md_pad_i   (mdio_i),
    .mdc_pad_o  (mdc),
    .md_pad_o   (mdio_o),
    .md_padoe_o (mdio_oe),

    // Interrupts
    .int_o       (eth_int),
    .cksum_int_o (cksum_int),

    // HA1588 hardware servo — needs an external PHC (tidelink/PHC block);
    // none in this RM: disabled inert. Servo outputs left open.
    .ha1588_servo_en            (1'b0),
    .ha1588_sync_interval       (30'd0),
    .phc_seconds                (48'd0),
    .phc_nanoseconds            (30'd0),
    .phc_hw_cap_seconds         (48'd0),
    .phc_hw_cap_nanoseconds     (30'd0),
    .phc_hw_cap_sub_nanoseconds (32'd0),
    .ha1588_hw_capture          (),
    .ha1588_hw_set_time         (),
    .ha1588_hw_set_seconds      (),
    .ha1588_hw_set_nanoseconds  (),
    .ha1588_hw_adj_valid        (),
    .ha1588_hw_adj_ns_incr_frac ()
  );

  // ===========================================================================
  // irq_out — REAL: the MAC's interrupt (frame RX/TX/error, unmasked by the
  // bring-up sequence's INT_MASK write) OR'd with the RX-checksum block's
  // line (inert until configured; all its IRQ_EN_* bits reset to 0).
  // ===========================================================================
  assign irq_out = eth_int | cksum_int;

  // ---------------------------------------------------------------------------
  // Deliberately-unused inputs (lint bookkeeping): no debug logic
  // (dbg_resetn, jtag_tck, jtag_tms, jtag_tdi), no console consumer
  // (uart_rx_tdata/tvalid, uart_tx_tready), no GPIO consumer (dut_gpio_i),
  // plus RM-internal bring-up telemetry with no v0.1 partition pin to ride on.
  // ---------------------------------------------------------------------------
  logic unused_ok;
  assign unused_ok = dbg_resetn | jtag_tck | jtag_tms | jtag_tdi
                   | (^uart_rx_tdata) | uart_rx_tvalid | uart_tx_tready
                   | (^dut_gpio_i)
                   | bringup_done | bringup_errored;

endmodule
