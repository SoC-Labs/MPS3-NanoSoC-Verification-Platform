// -----------------------------------------------------------------------------
// rp_nanosoc_multicore_wrapper.sv — Reconfigurable Module (RM) top for the
// MULTICORE-ETHERNET nanoSoC DUT (module `nanosoc_multicore_soc`).
//
// A joint work commissioned on behalf of SoC Labs, under Arm Academic Access
// license.
//
// Instantiates the REAL two-core ethernet SoC (network_core = CPU0 eth stack,
// chip_core = CPU1 chip control) from the read-only DUT checkout's regenerated
// top, build_soc/rtl/nanosoc_multicore_soc.sv (resolved, together with all of
// its IP, by $NANOSOC_MULTICORE_HOME/pynq/filelist.tcl — the exact read_verilog
// recipe the DUT's own PYNQ/MPS3 bitstream builds from), and wires it to the
// DFX partition-pin boundary.
//
// Ports are EXACTLY the RP side of docs/contracts/partition-pins.md v0.1 —
// the SAME 30-signal port list as rp_nanosoc_wrapper.sv / rp_eth_ss_wrapper.sv
// / rm_greybox.sv (directions mirrored from the shell's view), so
// fpga/dfx/pin_check.py passes. No AXI/AHB at this boundary (contract line 8,
// "no shell<->DUT AXI in v0").
//
// Unlike rm_nanosoc (single-core, no MAC) this DUT has BOTH a real Cortex-M0+
// pair AND a real ethernet MAC + PTP: the contract's Ethernet (RMII+MDIO) group
// and Console (CPU0 UART) group are REAL here; irq_out carries the live eth_irq
// and dut_lockup ORs both cores' lockup. CPU1's console (chip_core_uart_txd) is
// surfaced on dut_gpio_o[0] as an observation tap (partition-pins.md I4 board-
// port passthrough — the only DUT signal with a home in that group here).
//
// The tie-off disposition for everything the boundary does NOT carry (D2D,
// eth_ss_0 test slave, QSPI, PL022 SPI, JTAG, scan, PMU/NMI/RXEV, the two
// sysresetreq legs, hostio4 P1) is copied VERBATIM — same constants, same open
// legs, same nanosoc_d2d_idle_slave terminator — from the DUT's own proven,
// auto-generated FPGA wrapper build_soc/rtl/nanosoc_multicore_vivado_wrapper.v,
// which is known to elaborate + build a bitstream. Only the boundary-facing
// legs (clocks/resets, SWD, RMII/MDIO, CPU0 console via uart_axis_shim, irq/
// lockup/rm_id) are re-pointed at the partition pins; every tied input and open
// output keeps the vivado wrapper's exact direction so this instantiation
// inherits that wrapper's proven port-direction correctness.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module rp_nanosoc_multicore_wrapper #(
  parameter int NGPIO = 16,   // partition-pins.md I4 board-port/GPIO width.
                              // v0 default 16 (RM authoring contract item 2).
  // ---- CPU0 console (uart_axis_shim) baud, matched to the delivered image ---
  parameter        UART_CLK_HZ = 50_000_000,  // = the shell's ACTUAL dut_clk
                              // (shell clk_wiz_dut CLKOUT1 = 50 MHz; corroborated
                              // by nanosoc_multicore_ooc.xdc's 20.000 ns clock).
                              // The shim baud divider counts real dut_clk cycles.
  parameter        UART_BAUD   = 76800,        // 8N1. An image baked at
                              // NANOSOC_SYS_CLK_FREQ_HZ = 25 MHz (BAUDDIV = 651)
                              // run on the real 50 MHz dut_clk emits 50e6/651 ~=
                              // 76800 baud; shim DIV = 50e6/76800 = 651 matches
                              // the firmware divider cycle-for-cycle. Override to
                              // 38400 if firmware is rebuilt for a 50 MHz clock.
  // ---- inner-SoC preload/size params (mirror the DUT's own FPGA wrapper) -----
  parameter        ETH_IMEM_MEM_FPGA_IMG = "eth_imem_preload.hex",
                              // CPU0 (network_core) IMEM $readmemh preload — the
                              // RAM_PRELOAD ROM variant (verilog_define set by
                              // the DUT pynq flist). ooc_synth.tcl passes the
                              // ABSOLUTE path (<repo>/fpga/rp/nanosoc_multicore/
                              // eth_imem_preload.hex, or $ETH_IMEM_IMG) as a
                              // generic so $readmemh resolves regardless of the
                              // Vivado launch cwd. Integrator overrides with a
                              // real firmware image.
  parameter        CC_IMEM_MEM_FPGA_IMG  = "cc_imem_preload.hex",
                              // CPU1 (chip_core) IMEM $readmemh preload. Ditto.
  parameter        CC_IMEM_RAM_ADDR_W    = 14,  // CPU1 IMEM 4*2^14 = 16 KB (FPGA
                              // size; matches the DUT vivado wrapper override).
  parameter        CC_DMEM_RAM_ADDR_W    = 12,  // CPU1 DMEM 4*2^12 =  4 KB (FPGA).
  parameter        CC_BOOTROM_ADDR_W     = 11   // CPU1 bootrom 4*2^11 = 8 KB.
) (
  // =========================================================================
  // Clocks & resets — shell -> RP (partition-pins.md "Clocks & resets").
  // All DUT clocks are generated in the static shell; no clock generation in
  // this RM. dut_clk drives the SoC free-running clock and the PTP RTC clock;
  // phy_rmii_ref_clk is the shell virtual-PHY's 50 MHz RMII reference (a REAL
  // second clock domain — the eth MAC's MII side derives from it).
  //
  // Reset combination: nanosoc_multicore_soc has ONE system reset input
  // (sys_sysresetn) plus the DAP power-on reset (dap_npotrst). All three
  // contract resets are ANDed (active-low) into both — matching the proven
  // single-core rp_nanosoc_wrapper: ANY of the three resets the whole SoC.
  // (rp_resetn's "held through swap" and dbg_resetn's "debug-only" nuances
  // both collapse onto a whole-SoC reset for this DUT — A6 gap, as documented
  // for rm_nanosoc.)
  // =========================================================================
  input  logic dut_clk,       // DRP-reconfigurable DUT system clock (-> sys_fclk)
  input  logic dut_resetn,    // reset #1 — DUT system reset (host register)
  input  logic rp_resetn,     // reset #2 — reconfig reset, held through swap
  input  logic dbg_resetn,    // reset #3 — debug/SRST from OpenOCD path

  // =========================================================================
  // Processor debug — internal JTAG / SWJ-DP (partition-pins.md "Processor
  // debug"). Shell drives the TAP; this RM is the target (the SoC's SoC-400
  // SWJ-DP, driven in JTAG mode). 3-out / 1-in: TCK/TMS/TDI in, TDO out —
  // bound to the SoC's dap_swclktck/swditms/tdi/tdo. No shared-SWDIO mux (JTAG
  // TDO is a pure output). swj_enable/ntrst/npotrst are RM-internal straps.
  // =========================================================================
  input  logic jtag_tck,      // TCK -> dap_swclktck
  input  logic jtag_tms,      // TMS -> dap_swditms
  input  logic jtag_tdi,      // TDI -> dap_tdi
  output logic jtag_tdo,      // TDO <- dap_tdo, back to shell

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
  // Ethernet — RMII + MDIO (partition-pins.md "Ethernet"). REAL: shell is the
  // virtual PHY, the SoC's OpenCores MAC is the station and MDIO master. The
  // MAC's RMII pins are the SoC top's own rmii_* / md*_pad_* ports (the SoC
  // already contains the RMII<->MII adaptation internally).
  //
  // HDPR-29 / IOB packing: phy_rmii_txd/tx_en leave this RM as PLAIN FABRIC
  // signals — the pad re-register stage lives shell-side (rmii_phy_if.sv).
  // =========================================================================
  input  logic       phy_rmii_ref_clk,
  input  logic       phy_rmii_crs_dv,
  input  logic [1:0] phy_rmii_rxd,
  output logic [1:0] phy_rmii_txd,
  output logic       phy_rmii_tx_en,
  output logic       mdc,        // DUT is MDIO master
  output logic       mdio_o,
  output logic       mdio_oe,
  input  logic       mdio_i,     // virtual-PHY register model's reply

  // =========================================================================
  // Console / trace — AXI-Stream byte (partition-pins.md "Console / trace").
  // REAL: bridged to the SoC's dedicated CPU0 (network_core) console UART
  // (uart_txd/uart_rxd top-level ports) via uart_axis_shim.sv (shared with
  // rm_nanosoc, ../nanosoc/uart_axis_shim.sv). No SWO (Cortex-M0+ has no ITM).
  // =========================================================================
  output logic [7:0] uart_tx_tdata,
  output logic       uart_tx_tvalid,
  input  logic       uart_tx_tready,
  input  logic [7:0] uart_rx_tdata,
  input  logic       uart_rx_tvalid,
  output logic       uart_rx_tready,
  output logic       swo,        // no ITM/SWO on M0+: tied 0

  // =========================================================================
  // Status / misc — RP -> shell (partition-pins.md "Status / misc").
  // =========================================================================
  output logic [31:0] rm_id,      // 0x00000003 (rm_list.tcl rm_nanosoc_multicore)
  output logic        dut_lockup, // network_core_lockup | chip_core_lockup
  output logic        irq_out,    // REAL: the SoC eth_irq (MAC interrupt)

  // =========================================================================
  // Board-port / GPIO passthrough — shell <-> RP (partition-pins.md I4).
  // dut_gpio_o[0] surfaces CPU1's console TXD (chip_core_uart_txd) as an
  // observation tap (oe[0]=1). No other DUT signal has an I4 home here, so the
  // remaining bits are tied inert and dut_gpio_i is unused.
  // =========================================================================
  output logic [NGPIO-1:0] dut_gpio_o,
  output logic [NGPIO-1:0] dut_gpio_oe,
  input  logic [NGPIO-1:0] dut_gpio_i,

  // =========================================================================
  // Flash / QSPI XiP — RP <-> shell (partition-pins.md v0.2 "Flash / QSPI
  // XiP"). WIRED to the multicore SoC's own QSPI controller (qspi_flash_ahb):
  // the RM drives the board's external SST26VF064B across the boundary. The
  // SoC controller port `qspi_io_e` maps to the contract's `qspi_io_oe`.
  // NON-CDC source-synchronous crossing; the shell clamps it deselected during
  // a swap.
  //
  //   TODO (DEFERRED, per the boundary-freeze brief): XiP-EXECUTE unproven —
  //   cold-XiP livelock, I/O timing signoff and silicon bring-up are separate
  //   follow-ups. Register-accessible now; the cores still boot from the
  //   preloaded IMEM images, not flash.
  // =========================================================================
  output logic       qspi_sclk,
  output logic       qspi_csn,
  output logic [3:0] qspi_io_o,
  output logic [3:0] qspi_io_oe,
  input  logic [3:0] qspi_io_i
);

  // ===========================================================================
  // RM identity — per rm_list.tcl RM_LIB(rm_nanosoc_multicore,rm_id)
  // = 0x01000003. Real, permanently-driven constant read back by the
  // coordinator after a partial load (RM-load verification, spec §7).
  //
  // ENCODING v2 (docs/VERSIONING_PLAN.md §3.2):
  //     { ver_major[31:24], ver_minor[23:16], design_id[15:0] }
  //   design_id 0x0003 (UNCHANGED — the low half preserves the old id),
  //   version   1.0     => 0x01000003.
  // Keep in lockstep with rm_list.tcl; check_rm_id_encoding.py gates it.
  // ===========================================================================
  localparam logic [31:0] RM_ID_NANOSOC_MULTICORE = 32'h0100_0003;
  assign rm_id = RM_ID_NANOSOC_MULTICORE;

  // ===========================================================================
  // Reset combination (see header note) — one active-low AND for the whole SoC.
  // ===========================================================================
  logic sys_resetn_comb;
  assign sys_resetn_comb = dut_resetn & rp_resetn & dbg_resetn;

  // ===========================================================================
  // JTAG boundary — TDO is a pure unidirectional output from the SoC's dap_tdo
  // (no shared-SWDIO reflection mux). The SoC's SWD-out legs (dap_swdo/
  // dap_swdoen) and the TDO tristate-enable (dap_ntdoen) are unused on this
  // internal FPGA net and are left open at the instance below.
  // ===========================================================================
  logic dap_tdo_w;
  assign jtag_tdo = dap_tdo_w;
  assign dbg_bscan_tdo = 1'b0;  // no RM debug hub (2026-10 ILA mint boundary)

  // ===========================================================================
  // CPU0 console <-> AXIS shim. The SoC has a dedicated uart_txd/uart_rxd port
  // pair (no P1/FT1248 GPIO plumbing needed at this boundary, unlike the
  // single-core RM), so the shim connects to them directly.
  // ===========================================================================
  logic soc_uart_txd;   // SoC CPU0 TXD  (DUT -> host)
  logic soc_uart_rxd;   // shim serializer (host -> DUT CPU0 RXD)

  uart_axis_shim #(
    .CLK_HZ (UART_CLK_HZ),
    .BAUD   (UART_BAUD)
  ) u_uart_axis_shim (
    .clk            (dut_clk),
    .resetn         (sys_resetn_comb),
    .dut_txd_i      (soc_uart_txd),
    .dut_rxd_o      (soc_uart_rxd),
    .uart_tx_tdata  (uart_tx_tdata),
    .uart_tx_tvalid (uart_tx_tvalid),
    .uart_tx_tready (uart_tx_tready),
    .uart_rx_tdata  (uart_rx_tdata),
    .uart_rx_tvalid (uart_rx_tvalid),
    .uart_rx_tready (uart_rx_tready)
  );

  // ===========================================================================
  // Status ties + core-lockup OR + CPU1-console observation tap.
  // ===========================================================================
  assign swo = 1'b0;                         // Cortex-M0+: no ITM/SWO

  logic network_core_lockup_w;
  logic chip_core_lockup_w;
  assign dut_lockup = network_core_lockup_w | chip_core_lockup_w;

  logic chip_core_uart_txd_w;                // CPU1 console TXD -> gpio tap
  assign dut_gpio_o  = { {(NGPIO-1){1'b0}}, chip_core_uart_txd_w };
  assign dut_gpio_oe = { {(NGPIO-1){1'b0}}, 1'b1 };  // drive only bit 0 out

  // ===========================================================================
  // D2D outbound terminator — the SoC decodes 0x2E000000..0x2FFFFFFF off-die to
  // an AHB master with nothing attached on this FPGA target. A constant tie
  // cannot answer an AHB slave handshake and an unconnected input makes Vivado
  // tie hready=0 (bus wedge), so answer with the DUT's own idle-slave helper —
  // exactly as nanosoc_multicore_vivado_wrapper.v does.
  // ===========================================================================
  wire [1:0]  d2d_ahb_m_htrans;
  wire [31:0] d2d_ahb_m_hrdata;
  wire        d2d_ahb_m_hready;
  wire        d2d_ahb_m_hresp;

  nanosoc_d2d_idle_slave #(
    .DATA_W (32)
  ) u_d2d_idle_slave (
    .htrans  (d2d_ahb_m_htrans),
    .hrdata  (d2d_ahb_m_hrdata),
    .hready  (d2d_ahb_m_hready),
    .hresp   (d2d_ahb_m_hresp),
    .hclk    (dut_clk),
    .hresetn (sys_resetn_comb)
  );

  // sysresetreq legs: routed INSIDE the SoC to per-core reset_ctrl; the wrapper
  // must NOT loop them into the global sys_sysresetreq (that reboots both cores
  // + shared fabric). Collect the two outputs into a harmless sink so they do
  // not dangle (matches the DUT vivado wrapper).
  wire network_core_sysresetreq_w;
  wire chip_core_sysresetreq_w;
  wire _unused_sysresetreq =
      &{1'b0, network_core_sysresetreq_w, chip_core_sysresetreq_w};

  // ===========================================================================
  // The multicore SoC. Disposition of tied/open/terminated ports copied
  // verbatim from build_soc/rtl/nanosoc_multicore_vivado_wrapper.v (proven to
  // elaborate + build); only the boundary-facing legs are re-pointed at the
  // partition pins.
  // ===========================================================================
  nanosoc_multicore_soc #(
    .ETH_IMEM_MEM_FPGA_IMG  (ETH_IMEM_MEM_FPGA_IMG),
    .CC_IMEM_MEM_FPGA_IMG   (CC_IMEM_MEM_FPGA_IMG),
    .CC_IMEM_RAM_ADDR_W     (CC_IMEM_RAM_ADDR_W),
    .CC_DMEM_RAM_ADDR_W     (CC_DMEM_RAM_ADDR_W),
    .CC_BOOTROM_ADDR_W      (CC_BOOTROM_ADDR_W)
  ) u_soc (
    // --- clocks / resets (boundary) ---------------------------------------
    .sys_fclk             (dut_clk),
    .rmii_ref_clk         (phy_rmii_ref_clk),
    .sys_sysresetn        (sys_resetn_comb),
    .rtc_clk              (dut_clk),           // rtc_from_sys_fclk feature
    // --- SWJ-DP as JTAG TAP (boundary) -------------------------------------
    //     TCK/TMS/TDI in, TDO out. SWD-out legs (swdo/swdoen) unused on this
    //     internal net -> left open. (dap_tdi/tdo bound in the block below.)
    .dap_swclktck         (jtag_tck),
    .dap_swditms          (jtag_tms),
    .dap_swdo             (),
    .dap_swdoen           (),
    // --- CPU0 console (boundary, via shim) --------------------------------
    .uart_rxd             (soc_uart_rxd),
    .uart_txd             (soc_uart_txd),
    // --- CPU1 console: RXD idle-high, TXD to the gpio observation tap ------
    .chip_core_uart_rxd   (1'b1),
    .chip_core_uart_txd   (chip_core_uart_txd_w),
    // --- RMII + MDIO (boundary) -------------------------------------------
    .rmii_txd             (phy_rmii_txd),
    .rmii_tx_en           (phy_rmii_tx_en),
    .rmii_rxd             (phy_rmii_rxd),
    .rmii_crs_dv          (phy_rmii_crs_dv),
    .mdc_pad_o            (mdc),
    .md_pad_i             (mdio_i),
    .md_pad_o             (mdio_o),
    .md_padoe_o           (mdio_oe),
    // --- QSPI: NOW PROMOTED to partition pins (v0.2 boundary freeze). The
    //     controller reaches the board's external SST26VF064B across the
    //     RP<->shell boundary via the qspi_* partition pins above. `qspi_io_e`
    //     (SoC output-enable) maps to the contract's `qspi_io_oe`. XiP-EXECUTE
    //     DEFERRED — register-accessible, cores still boot from preloaded IMEM.
    .qspi_sclk            (qspi_sclk),
    .qspi_csn             (qspi_csn),
    .qspi_io_i            (qspi_io_i),
    .qspi_io_o            (qspi_io_o),
    .qspi_io_e            (qspi_io_oe),
    // --- PL022 SPI: no external slave -> miso tied 0, outputs open ---------
    .spi_sclk             (),
    .spi_mosi             (),
    .spi_miso             (1'b0),
    .spi_ss               (),
    // --- PPS out: no partition-pin home -> open ---------------------------
    .phc_pps_out          (),
    // --- scan / test: tied (excluded pads) --------------------------------
    .sys_scanenable       (1'b0),
    .sys_testmode         (1'b0),
    // --- JTAG TDI/TDO (boundary) — the other two legs of the JTAG TAP ------
    .dap_tdi              (jtag_tdi),
    .dap_ntrst            (1'b1),              // TAP TRSTn deasserted (kept live)
    .dap_tdo              (dap_tdo_w),
    .dap_ntdoen           (),                  // TDO tristate-enable: unused
    // --- hostio4 P1: tied in / open out -----------------------------------
    .hostio4_p1_in        (7'b000_0000),
    .hostio4_p1_out       (),
    .hostio4_p1_outen     (),
    // --- global reset request / DAP straps --------------------------------
    .sys_sysresetreq      (1'b0),
    .dap_npotrst          (sys_resetn_comb),   // dap_npotrst_from_nrst feature
    .dap_swj_enable       (1'b1),
    // --- per-core PMU / NMI / RXEV: tied ----------------------------------
    .network_core_pmuenable (1'b0),
    .chip_core_pmuenable  (1'b0),
    .network_core_nmi     (1'b0),
    .chip_core_nmi        (1'b0),
    .network_core_rxev    (1'b0),
    .chip_core_rxev       (1'b0),
    // --- eth_ss_0 external test AHB slave: no external master -> tied in,
    //     open out ------------------------------------------------------------
    .eth_ss_0_htrans      (2'b00),
    .eth_ss_0_haddr       (32'd0),
    .eth_ss_0_hwrite      (1'b0),
    .eth_ss_0_hsize       (3'b000),
    .eth_ss_0_hburst      (3'b000),
    .eth_ss_0_hprot       (4'b0000),
    .eth_ss_0_hwdata      (32'd0),
    .eth_ss_0_hmastlock   (1'b0),
    .eth_ss_0_hrdata      (),
    .eth_ss_0_hready      (),
    .eth_ss_0_hresp       (),
    // --- D2D inbound AHB slave: no remote die -> tied in, open out ---------
    .d2d_ahb_s_htrans     (2'b00),
    .d2d_ahb_s_haddr      (32'd0),
    .d2d_ahb_s_hwrite     (1'b0),
    .d2d_ahb_s_hsize      (3'b010),
    .d2d_ahb_s_hburst     (3'b000),
    .d2d_ahb_s_hprot      (4'b0000),
    .d2d_ahb_s_hwdata     (32'd0),
    .d2d_ahb_s_hmastlock  (1'b0),
    .d2d_ahb_s_hrdata     (),
    .d2d_ahb_s_hready     (),
    .d2d_ahb_s_hresp      (),
    // --- D2D interrupts / PHC servo bond-outs: tied / open ----------------
    .d2d_irq              (16'd0),
    .d2d_phc_hw_capture   (1'b0),
    .d2d_phc_hw_set_time  (1'b0),
    .d2d_phc_hw_set_seconds (48'd0),
    .d2d_phc_hw_set_nanoseconds (30'd0),
    .d2d_phc_hw_adj_valid (1'b0),
    .d2d_phc_hw_adj_ns_incr_frac (32'd0),
    .d2d_phc_seconds      (),
    .d2d_phc_nanoseconds  (),
    .d2d_phc_hw_cap_seconds (),
    .d2d_phc_hw_cap_nanoseconds (),
    .d2d_phc_hw_cap_sub_nanoseconds (),
    // --- D2D outbound AHB master: control legs open, handshake answered by
    //     the idle-slave terminator wires above ------------------------------
    .d2d_ahb_m_haddr      (),
    .d2d_ahb_m_hwrite     (),
    .d2d_ahb_m_hsize      (),
    .d2d_ahb_m_hburst     (),
    .d2d_ahb_m_hprot      (),
    .d2d_ahb_m_hwdata     (),
    .d2d_ahb_m_hmastlock  (),
    .d2d_ahb_m_htrans     (d2d_ahb_m_htrans),
    .d2d_ahb_m_hrdata     (d2d_ahb_m_hrdata),
    .d2d_ahb_m_hready     (d2d_ahb_m_hready),
    .d2d_ahb_m_hresp      (d2d_ahb_m_hresp),
    // --- status / debug outputs -------------------------------------------
    .sys_poresetn         (),
    .sys_hclk             (),
    .sys_hresetn          (),
    .network_core_txev    (),
    .network_core_lockup  (network_core_lockup_w),
    .network_core_sysresetreq (network_core_sysresetreq_w),
    .network_core_sleeping (),
    .network_core_sleepdeep (),
    .chip_core_txev       (),
    .chip_core_lockup     (chip_core_lockup_w),
    .chip_core_sysresetreq (chip_core_sysresetreq_w),
    .chip_core_sleeping   (),
    .chip_core_sleepdeep  (),
    .chip_core_wdog_reset (),
    // --- PTP / eth status + IRQs ------------------------------------------
    .rtc_time_ptp_ns      (),
    .rtc_time_ptp_sec     (),
    .rtc_time_one_pps     (),
    .ha1588_servo_locked  (),
    .eth_irq              (irq_out),
    .phc_pps_irq          (),
    .phc_alarm_irq        ()
  );

  // ---------------------------------------------------------------------------
  // Deliberately-unused boundary inputs (lint bookkeeping): the GPIO
  // passthrough sample has no DUT consumer here. (All three JTAG inputs —
  // jtag_tck/tms/tdi — are now consumed by the SWJ-DP instance above.)
  // ---------------------------------------------------------------------------
  logic unused_ok;
  assign unused_ok = (^dut_gpio_i);

endmodule
