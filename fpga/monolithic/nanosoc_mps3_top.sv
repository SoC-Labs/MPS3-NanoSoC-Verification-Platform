//-----------------------------------------------------------------------------
// nanosoc_mps3_top.sv -- monolithic nanoSoC-on-MPS3 board-top wrapper
// A joint work commissioned on behalf of SoC Labs, under Arm Academic Access
// license. Companion to build_monolithic.tcl / filelist.tcl (WP0.4,
// docs/IMPLEMENTATION_PLAN.md; wiring per docs/nanosoc_m0_soc/PLATFORM_MAPPING.md
// section 2).
//-----------------------------------------------------------------------------
// Fills the TODO left by the earlier port of the legacy arm_mps3 target
// (README.md Sec 2.4): that port targeted `nanosoc_chip` from the *legacy*
// nanosoc-multicore-system arm_mps3 BD flow, whose RTL was confirmed to not
// exist anywhere in that source tree (orphaned pre-generator design). THIS
// wrapper instead instantiates the real, current, buildable single-core SoC
// -- module `nanosoc` -- from the sibling repo `nanosoc_m0_soc`
// (build_soc/rtl/nanosoc.sv, ~76 ports incl. 4 SPI pins -- see the CLOCK
// section below for a note on a port-list discrepancy found between this
// file and its arch_tech-submodule namesake), following the exact same "wrap
// `nanosoc` directly, not `nanosoc_system`/`nanosoc_chip`" pattern already
// HW-validated on a PYNQ-Z2 build (nanosoc_m0_soc/pynq/vivado_ip/
// nanosoc_vivado_wrapper.v, doc/reports/pynq_z2_build.md, 2026-07-03) --
// see docs/nanosoc_m0_soc/RTL_ANATOMY.md Sec 1.5 for why `nanosoc_system`/
// `nanosoc_chip` are avoided (confirmed exp-port direction bug, RTL_ANATOMY
// Sec 1.3).
//
// Port list: every port below, with its exact width, mirrors
// nanosoc_design_wrapper.v (the legacy MPS3 board wrapper this repo's
// nanosoc_mps3.xdc was ported from) so the existing, Vivado-2024.1-verified
// nanosoc_mps3.xdc (272/272 PACKAGE_PIN checked, README.md Sec 6) constrains
// this module without any XDC edits. Only OSCCLK[1], CB_nRST/CB_nPOR,
// UART_TX_F[2]/UART_RX_F[2] and USER_nLED[7:0]/USER_SW[7:0] are functionally
// driven by the SoC (matching the XDC's own top-comment, "nanosoc itself
// only drives/reads UART_TX_F[2]/UART_RX_F[2], OSCCLK[1] (ACLK), and
// CB_nRST/CS_nSRST" -- CS_nSRST is additionally read here but not consumed,
// see the RESET section below); every other board group declared on this
// module is a Vivado-mandated "every top port needs a legal site" tie-off,
// not an active function of this v0 baseline (matches nanosoc_mps3.xdc
// Sec header + README.md Sec 3).
//
// CLOCK: OSCCLK[1] is MPS3's "ACLK" 50 MHz reference (nanosoc_mps3.xdc line
// 234-238 comment; `create_clock -period 20.000` = 50 MHz, xdc line 688).
// Per docs/nanosoc_m0_soc/PLATFORM_MAPPING.md Sec 2, "50 MHz is a legal
// SYS_CLK_FREQ_HZ ... likely usable directly without an MMCM". NOTE ON A
// DISCREPANCY FOUND WHILE WIRING THIS UP: `nanosoc`'s two checked-in copies
// disagree on this point -- nanosoc_arch_tech/rtl/src/nanosoc/nanosoc.sv
// (the arch_tech submodule's own canonical copy) has NO SYS_CLK_FREQ_HZ
// parameter at all, while build_soc/rtl/nanosoc.sv (the project-regenerated
// file the real synth flow actually reads, per filelist.tcl) DOES declare
// one (default 100_000_000). The proven, HW-validated PYNQ-Z2 reference
// (pynq/vivado_ip/nanosoc_vivado_wrapper.v) instantiates the same
// build_soc/rtl/nanosoc.sv yet does NOT override this parameter, despite
// that board's actual sys_clk running at 25 MHz post-clk_wiz -- strong
// circumstantial evidence this parameter is discovery/build-info metadata
// (surfaced through nanosoc_build_info/nanosoc_ahb_interconnect_discovery),
// not something that changes clocking/timing behaviour; the value that
// actually matters for UART baud-rate math is the separate, firmware-side
// NANOSOC_SYS_CLK_FREQ_HZ #define (patched by the fw_config step, see
// README.md). This wrapper follows the same proven precedent and leaves
// SYS_CLK_FREQ_HZ at its RTL default rather than guessing at an override
// this agent could not verify against a real Vivado elaboration -- flagged
// here for the integrator to double check if it ever turns out to matter.
// Either way there is nothing to "close timing against" beyond routing
// OSCCLK[1] through a BUFG and constraining it, which nanosoc_mps3.xdc
// already does. NO MMCM IS NEEDED for this baseline. If a different sys_clk
// frequency is ever wanted (e.g. to match the 25 MHz PYNQ-Z2 firmware
// operating point), replace the BUFG below with an MMCME3_BASE/MMCME3_ADV
// instance and update nanosoc_mps3.xdc's `create_clock` on OSCCLK[1] plus
// the derived-clock constraint accordingly -- out of scope for this
// baseline.
//
// RESET: CB_nRST (board/MCC-driven reset, active low) ANDed with CB_nPOR
// (power-on-reset, active low) per PLATFORM_MAPPING.md Sec 2 ("Consider
// ANDing with CB_nPOR for a combined cold/warm reset"), then run through a
// 3-FF synchroniser (assert async, release sync) in the sys_clk domain to
// produce nanosoc's single `sys_sysresetn` input -- nanosoc has exactly one
// reset pin, so rp_resetn/dbg_resetn-style "separate reset domain" concepts
// (partition-pins.md gaps, not applicable to this whole-device baseline)
// don't arise here. CS_nSRST (CoreSight reset) is read but intentionally
// NOT ANDed in for this baseline -- see the DEBUG section below.
//
// UART: nanosoc has no dedicated UART pins -- the firmware console is CMSDK
// UART2, muxed onto GPIO port P1 by nanosoc_pin_mux when firmware sets the
// GPIO1 ALTFUNC bit: P1[5]=TXD (out), P1[4]=RXD (in), P1[7]=FT1248MODE strap.
// The P1[0]/[2]/[3]/[6]/[7] self-drain ties below are MANDATORY, not
// optional polish -- PLATFORM_MAPPING.md Sec 2 flags that without them the
// stage-0 BOOTROM's ADP/FT1248 debug-channel banner print backs up and
// HANGS BOOT before the jump to IMEM (same requirement independently
// confirmed by nanosoc_vivado_wrapper.v's own header, the HW-proven
// PYNQ-Z2 reference this section mirrors verbatim).
// NOTE (known, upstream, not fixed here): UART2 RX only reaches the pin mux
// via internal loopback in the currently generated RTL, not the physical
// pad -- so UART_RX_F[2] is wired for completeness/future-proofing but does
// not yet deliver host-to-DUT bytes. TX (the "UART prints" acceptance gate,
// spec Sec 14 step 1) is proven working.
//
// GPIO: p0_out[7:0] -> USER_nLED[7:0] (active-low LEDs, inverted);
// USER_SW[7:0] -> p0_in[7:0]. p1 is fully consumed by the UART2/FT1248
// muxing above and is not available for general board GPIO (matches
// PLATFORM_MAPPING.md Sec 1).
//
// DEBUG (SWD): tied off for this baseline. PLATFORM_MAPPING.md Sec 2 marks
// SWD "Optional for the 'boots, UART prints' acceptance gate" and
// recommends tying cpu_0_swdi=0/cpu_0_swclk=0, leaving swdo/swdoen open --
// done exactly that way here. The CoreSight debug header (CS_TDI/TDO/TMS/
// TCK/nSRST/nTRST/nDET) is a separate ARM DAP JTAG path with no built-in
// JTAG->SWD conversion for this SoC (same doc, same section) and is left
// tied off/unrouted, matching the "safe tie-off" treatment given to every
// other unused board group. Bringing up SWD access is future work (would
// route through SH0_IO/SH1_IO per the xdc's own provenance comment, the
// legacy design's precedent path -- not attempted here, out of scope for
// the build-half of this milestone).
//
// EXPANSION REGION / SPI: exp_* tied off exactly per the HW-validated
// nanosoc_vivado_wrapper.v pattern (benign, non-stalling tie: exp_hready=1,
// stream-in tready=1, everything else quiescent) -- see that file's own
// header for why (nanosoc.sv's exp_h* port directions don't back-propagate
// cleanly through a real default-slave instantiation under Vivado
// synthesis, confirmed independently in docs/nanosoc_m0_soc/RTL_ANATOMY.md
// Sec 1.3). SPI (PL022 SSP master pins) has no board-pin group anywhere in
// nanosoc_mps3.xdc (confirmed: no SPI_* constraint exists) -- left
// unconnected at this top level, spi_miso tied inactive; a future SPI
// bring-up would need new XDC pins allocated first (PLATFORM_MAPPING.md
// Sec 3 item 6, deferred, no SPI peripheral on the MPS3 board plan yet).
//-----------------------------------------------------------------------------

module nanosoc_mps3_top #(
    // Word-oriented $readmemh image preloaded into IMEM at synthesis time
    // (RAM_PRELOAD build -- see filelist.tcl). Override via the project's
    // GENERIC property in build_monolithic.tcl, or edit this default before
    // synthesis -- see README.md Sec "Firmware" for the exact commands that
    // produce this file (cmake + hex_byte_to_word.py) and why it must be
    // word- not byte-oriented.
    parameter IMEM_MEM_FPGA_IMG = "image.hex"
) (
    //--------------------------------------------------------------------
    // SMB / LAN9220 Ethernet SMC interface -- tied off (no LAN9220 driver
    // logic in this baseline; see README.md Sec 3).
    //--------------------------------------------------------------------
    output wire [6:0]   SMBF_ADDR,
    output wire         SMBF_FIFOSEL,
    inout  wire [15:0]  SMBF_DATA,
    output wire         SMBF_nOE,
    output wire         SMBF_nWE,
    output wire         SMBF_nRST,

    output wire         ETH_nCS,
    output wire         ETH_nOE,
    input  wire         ETH_INT,

    output wire         USB_nCS,
    output wire         USB_DACK,
    input  wire         USB_DREQ,
    input  wire         USB_INT,

    //--------------------------------------------------------------------
    // HDMI / MMB -- tied off (no display path in this baseline).
    //--------------------------------------------------------------------
    output wire [23:0]  MMB_DATA,
    output wire         MMB_DE,
    output wire         MMB_HS,
    output wire         MMB_VS,
    output wire         MMB_IDCLK,
    output wire         MMB_SCK,
    output wire         MMB_WS,
    output wire [3:0]   MMB_SD,

    output wire         HDMI_CSCL,
    inout  wire         HDMI_CSDA,
    input  wire         HDMI_INT,

    //--------------------------------------------------------------------
    // Audio -- tied off.
    //--------------------------------------------------------------------
    output wire         AUD_MCLK,
    output wire         AUD_SCLK,
    output wire         AUD_LRCK,
    output wire         AUD_SDIN,
    input  wire         AUD_SDOUT,

    output wire         AUD_nRST,
    output wire         AUD_SCL,
    inout  wire         AUD_SDA,

    //--------------------------------------------------------------------
    // eMMC -- tied off (held in reset).
    //--------------------------------------------------------------------
    inout  wire [7:0]   EMMC_DAT,
    inout  wire         EMMC_CMD,
    output wire         EMMC_CLK,
    output wire         EMMC_nRST,
    input  wire         EMMC_DS,

    //--------------------------------------------------------------------
    // CLCD -- tied off.
    //--------------------------------------------------------------------
    inout  wire [17:10] CLCD_PD,
    output wire         CLCD_RD,
    output wire         CLCD_RS,
    output wire         CLCD_CS,
    output wire         CLCD_WR_SCL,
    output wire         CLCD_BL,
    output wire         CLCD_RST,

    output wire         CLCD_TSCL,
    inout  wire         CLCD_TSDA,
    input  wire         CLCD_TINT,
    output wire         CLCD_TNC,

    //--------------------------------------------------------------------
    // UART -- UART_TX_F[2]/UART_RX_F[2] are nanosoc's real console (CMSDK
    // UART2 via the P1 GPIO pin mux); the other 3 lanes are unused.
    //--------------------------------------------------------------------
    output wire [3:0]   UART_TX_F,
    input  wire [3:0]   UART_RX_F,

    //--------------------------------------------------------------------
    // DEBUG (CoreSight JTAG/trace header) -- tied off, see header comment.
    //--------------------------------------------------------------------
    input  wire         CS_TDI,
    output wire         CS_TDO,
    inout  wire         CS_TMS,
    input  wire         CS_TCK,
    input  wire         CS_nSRST,
    input  wire         CS_nTRST,
    input  wire         CS_nDET,

    output wire [15:0]  CS_T_D,
    output wire         CS_T_CLK,
    output wire         CS_T_CTL,

    //--------------------------------------------------------------------
    // LED / switches / push-buttons -- p0 GPIO passthrough.
    //--------------------------------------------------------------------
    output wire [9:0]   USER_nLED,
    input  wire [7:0]   USER_SW,
    input  wire [1:0]   USER_nPB,

    //--------------------------------------------------------------------
    // Reference clocks -- OSCCLK[1] (ACLK, 50 MHz) is the only one used.
    //--------------------------------------------------------------------
    input  wire [5:0]   OSCCLK,

    //--------------------------------------------------------------------
    // Quad SPI -- tied off (no flash-boot path in this baseline).
    //--------------------------------------------------------------------
    inout  wire         QSPI_D0,
    inout  wire         QSPI_D1,
    inout  wire         QSPI_D2,
    inout  wire         QSPI_D3,
    output wire         QSPI_SCLK,
    output wire         QSPI_nCS,

    //--------------------------------------------------------------------
    // User SD -- tied off.
    //--------------------------------------------------------------------
    inout  wire [3:0]   USD_DAT,
    inout  wire         USD_CMD,
    output wire         USD_CLK,
    input  wire         USD_NCD,

    //--------------------------------------------------------------------
    // Reset / board control.
    //--------------------------------------------------------------------
    input  wire         CB_nPOR,
    input  wire         CB_nRST,
    input  wire         CB_RUN,

    input  wire         IOFPGA_NRST,
    input  wire         IOFPGA_NSPIR,

    output wire         IOFPGA_SYSWDT,
    input  wire         PB_IRQ,
    output wire         WDOG_RREQ,

    //--------------------------------------------------------------------
    // SCC -- tied off.
    //--------------------------------------------------------------------
    output wire         CFG_DATAOUT,
    input  wire         CFG_LOAD,
    input  wire         CFG_nRST,
    input  wire         CFG_CLK,
    input  wire         CFG_DATAIN,
    input  wire         CFG_WnR,

    //--------------------------------------------------------------------
    // MCC SMB -- tied off.
    //--------------------------------------------------------------------
    input  wire [25:16] SMBM_A,
    inout  wire [15:0]  SMBM_D,
    input  wire [4:1]   SMBM_nE,
    input  wire         SMBM_CLK,
    input  wire [1:0]   SMBM_nBL,
    input  wire         SMBM_nOE,
    input  wire         SMBM_nWE,
    output wire         SMBM_nWAIT,

    //--------------------------------------------------------------------
    // Arduino-style shield header + on-shield ADC SPI -- tied off (future
    // SWD bring-up precedent path per the xdc's provenance comment; not
    // used in this baseline).
    //--------------------------------------------------------------------
    inout  wire [17:0]  SH0_IO,
    inout  wire [17:0]  SH1_IO,
    output wire         SH_nRST,

    output wire         SH_ADC_CS,
    output wire         SH_ADC_CK,
    output wire         SH_ADC_DI,
    input  wire         SH_ADC_DO
);

    //=====================================================================
    // Clock: OSCCLK[1] (ACLK, 50 MHz) -> BUFG -> sys_clk. No MMCM needed
    // (see header comment).
    //=====================================================================
    wire sys_clk;

`ifndef VERILATOR
    BUFG u_bufg_sys_clk (
        .I (OSCCLK[1]),
        .O (sys_clk)
    );
`else
    // Lint-only fallback: BUFG is a Xilinx UNISIM primitive with no open
    // lint-tool model available; substitute a plain passthrough so this
    // wrapper's own syntax/connectivity can still be checked in isolation.
    // Real Vivado builds always take the BUFG branch above.
    assign sys_clk = OSCCLK[1];
`endif

    //=====================================================================
    // Reset: CB_nRST & CB_nPOR (active low) -> 3-FF synchroniser in the
    // sys_clk domain -> nanosoc's single sys_sysresetn input. Assert
    // asynchronously (as soon as either board reset drops), release
    // synchronously (glitch-free deassertion into the SoC's clock domain).
    //=====================================================================
    wire board_rst_n = CB_nPOR & CB_nRST;

    (* ASYNC_REG = "TRUE" *) reg [2:0] rst_sync_ff;

    always @(posedge sys_clk or negedge board_rst_n) begin
        if (!board_rst_n)
            rst_sync_ff <= 3'b000;
        else
            rst_sync_ff <= {rst_sync_ff[1:0], 1'b1};
    end

    wire sys_sysresetn = rst_sync_ff[2];

    //=====================================================================
    // GPIO port 0 -- USER_nLED / USER_SW passthrough.
    //=====================================================================
    wire [15:0] p0_in;
    wire [15:0] p0_out;
    wire [15:0] p0_outen;   // unused (MPS3 LEDs/switches are plain I/O, not tristate)

    assign p0_in[7:0]   = USER_SW;
    assign p0_in[15:8]  = 8'h00;

    assign USER_nLED[7:0] = ~p0_out[7:0];   // active-low LEDs, inverted
    assign USER_nLED[9:8] = 2'b11;          // unused LEDs -- off

    //=====================================================================
    // GPIO port 1 -- CMSDK UART2 pin-mux + mandatory FT1248/ADP self-drain
    // ties (see header comment). Pattern mirrors nanosoc_m0_soc's
    // HW-validated pynq/vivado_ip/nanosoc_vivado_wrapper.v verbatim.
    //=====================================================================
    wire [15:0] p1_in;
    wire [15:0] p1_out;
    wire [15:0] p1_outen;

    wire uart_txd_int = p1_outen[5] ? p1_out[5] : 1'b1;  // idle-high until
                                                          // UART2 altfunc set

    assign UART_TX_F[2] = uart_txd_int;
    assign UART_TX_F[3] = 1'b1;   // unused lanes -- idle-high (UART convention)
    assign UART_TX_F[1] = 1'b1;
    assign UART_TX_F[0] = 1'b1;

    assign p1_in[0]    = p1_out[3];       // FT_MISO <= FT_SSN (self-drain)
    assign p1_in[1]    = p1_out[1];       // FT_CLK readback
    assign p1_in[2]    = 1'b0;            // FT_MIOSIO TXE# = 0 ("can accept")
    assign p1_in[3]    = p1_out[3];       // FT_SSN readback
    assign p1_in[4]    = UART_RX_F[2];    // UART2 RXD (TX-only today, see hdr)
    assign p1_in[5]    = uart_txd_int;    // UART2 TXD readback
    assign p1_in[6]    = 1'b1;            // reserved
    assign p1_in[7]    = 1'b1;            // FT1248MODE strap: FT1248/UART2
    assign p1_in[15:8] = 8'h00;           // unused

    //=====================================================================
    // SWD -- tied off for this baseline (see header comment).
    //=====================================================================
    // cpu_0_swdo / cpu_0_swdoen intentionally left unconnected (open) --
    // no board pin drives SWD in this v0 baseline.

    //=====================================================================
    // Static tie-offs -- every remaining board I/O group. Outputs go to a
    // safe idle level (unused resets/chip-selects/enables held
    // inactive/asserted as noted per-signal); inouts are held high-Z; unused
    // inputs need no assignment.
    //=====================================================================

    // SMB / LAN9220 -- outputs
    assign SMBF_ADDR    = 7'b0;
    assign SMBF_FIFOSEL = 1'b0;
    assign SMBF_nOE     = 1'b1;   // disabled
    assign SMBF_nWE     = 1'b1;   // disabled
    assign SMBF_nRST    = 1'b0;   // hold shared LAN9220/USB-debug-FIFO bus in reset
    assign SMBF_DATA    = 16'bz;  // inout, unused -- high-Z

    assign ETH_nCS = 1'b1;        // deselected
    assign ETH_nOE = 1'b1;        // disabled

    assign USB_nCS   = 1'b1;      // deselected
    assign USB_DACK  = 1'b1;      // inactive

    // HDMI / MMB -- outputs
    assign MMB_DATA  = 24'b0;
    assign MMB_DE    = 1'b0;
    assign MMB_HS    = 1'b0;
    assign MMB_VS    = 1'b0;
    assign MMB_IDCLK = 1'b0;
    assign MMB_SCK   = 1'b0;
    assign MMB_WS    = 1'b0;
    assign MMB_SD    = 4'b0;

    assign HDMI_CSCL = 1'b1;      // I2C idle-high
    assign HDMI_CSDA = 1'bz;      // inout, unused -- high-Z

    // Audio -- outputs
    assign AUD_MCLK = 1'b0;
    assign AUD_SCLK = 1'b0;
    assign AUD_LRCK = 1'b0;
    assign AUD_SDIN = 1'b0;
    assign AUD_nRST = 1'b0;       // hold codec in reset
    assign AUD_SCL  = 1'b1;       // I2C idle-high
    assign AUD_SDA  = 1'bz;       // inout, unused -- high-Z

    // eMMC -- held in reset
    assign EMMC_CLK  = 1'b0;
    assign EMMC_nRST = 1'b0;
    assign EMMC_DAT  = 8'bz;      // inout, unused -- high-Z
    assign EMMC_CMD  = 1'bz;      // inout, unused -- high-Z

    // CLCD -- outputs
    assign CLCD_RD      = 1'b0;
    assign CLCD_RS      = 1'b0;
    assign CLCD_CS       = 1'b0;
    assign CLCD_WR_SCL  = 1'b0;
    assign CLCD_BL      = 1'b0;   // backlight off
    assign CLCD_RST     = 1'b0;   // hold in reset
    assign CLCD_TSCL    = 1'b1;   // I2C idle-high
    assign CLCD_TNC     = 1'b0;
    assign CLCD_PD      = 8'bz;   // inout [17:10], unused -- high-Z
    assign CLCD_TSDA    = 1'bz;   // inout, unused -- high-Z

    // DEBUG (CoreSight) -- outputs / inout tied off
    assign CS_TDO    = 1'b0;
    assign CS_T_D    = 16'b0;
    assign CS_T_CLK  = 1'b0;
    assign CS_T_CTL  = 1'b0;
    assign CS_TMS    = 1'bz;      // inout, unused -- high-Z

    // Quad SPI -- outputs / inouts
    assign QSPI_SCLK = 1'b0;
    assign QSPI_nCS  = 1'b1;      // deselected
    assign QSPI_D0   = 1'bz;
    assign QSPI_D1   = 1'bz;
    assign QSPI_D2   = 1'bz;
    assign QSPI_D3   = 1'bz;

    // User SD -- outputs / inouts
    assign USD_CLK = 1'b0;
    assign USD_DAT = 4'bz;
    assign USD_CMD = 1'bz;

    // Board control -- outputs
    assign IOFPGA_SYSWDT = 1'b0;
    assign WDOG_RREQ     = 1'b0;

    // SCC -- outputs
    assign CFG_DATAOUT = 1'b0;

    // MCC SMB -- outputs / inouts
    assign SMBM_nWAIT = 1'b1;     // ready/not-waiting
    assign SMBM_D     = 16'bz;    // inout, unused -- high-Z

    // Shield header + on-shield ADC SPI -- outputs / inouts
    assign SH_nRST   = 1'b0;      // hold shield in reset
    assign SH_ADC_CS = 1'b1;      // deselected
    assign SH_ADC_CK = 1'b0;
    assign SH_ADC_DI = 1'b0;
    assign SH0_IO    = 18'bz;     // inout, unused -- high-Z
    assign SH1_IO    = 18'bz;     // inout, unused -- high-Z

    //=====================================================================
    // SoC instance -- the real, current, single-core nanoSoC core top.
    //=====================================================================
    nanosoc #(
        .IMEM_MEM_FPGA_IMG (IMEM_MEM_FPGA_IMG)
    ) u_nanosoc (
        // Clock / reset
        .sys_clk         (sys_clk),
        .sys_sysresetn   (sys_sysresetn),
        .sys_xtalclk_out (),

        // Scan / test -- tied off for mission mode
        .sys_scanenable  (1'b0),
        .sys_testmode    (1'b0),
        .sys_scaninhclk  (1'b0),
        .sys_scanouthclk (),

        // DEBUG: CoreSight SoC-400 SWJ-DAP (replaces the per-core CM0 DAP).
        //
        // THE PORTS THIS BLOCK USED TO CONNECT NO LONGER EXIST. The July top
        // wired cpu_0_swdi/swclk/swdo/swdoen - the Cortex-M0's INTEGRATED DAP.
        // The SoC now builds with EXTERNAL_DAP=1 (nanosoc_ss_cpu.sv:188), which
        // elaborates slcorem0's g_external_dap branch and exposes a SoC-400
        // SWJ-DAP at the top instead. Elaboration failed on exactly these four.
        //
        // VALUES FOLLOW THE GENERATOR'S OWN GOLDEN FPGA WRAPPER, not a guess:
        //   nanosoc_gen/tests/golden/__snapshots__/fpga/
        //     nanosoc_multicore_vivado_wrapper.v:137-175
        // and the ASIC pad ring (nanosoc_chip_pads.v:154). Three of these are
        // NOT what "tie it all off" would give, and the difference matters:
        //   swj_enable = 1  the DAP is ENABLED - golden and silicon both tie 1,
        //                   and nanosoc.sv:83 says "tied 1 on this single-
        //                   chiplet SoC". 0 is for INACTIVE chiplets only.
        //   ntrst      = 1  JTAG TAP released, not held in reset.
        //   npotrst    = sys_sysresetn - golden's dap_npotrst_from_nrst feature:
        //                   the DP comes out of reset with the system.
        //
        // ONE DEPARTURE FROM GOLDEN, DELIBERATE: golden ROUTES swclktck/swditms
        // to physical SWD pins. Here they are held at idle, so the DAP is enabled
        // but unreachable - with swclktck low no bit is ever clocked in, so the
        // swditms level is never sampled. Routing SWD to the MPS3 CoreSight
        // header (CS_TCK/CS_TMS in nanosoc_mps3.xdc) needs a bidirectional IOBUF
        // on SWDIO and is the next step for on-board debug; it is not needed to
        // boot and print over UART.
        .dap_swclktck    (1'b0),
        .dap_swditms     (1'b1),
        .dap_tdi         (1'b0),
        .dap_ntrst       (1'b1),
        .dap_npotrst     (sys_sysresetn),
        .dap_swj_enable  (1'b1),
        .dap_swdo        (),
        .dap_swdoen      (),
        .dap_tdo         (),
        .dap_ntdoen      (),

        // QSPI flash controller (unconditional in the SoC since nanosoc_m0_soc
        // b065ed8, 2026-07-15). NOT routed to the MPS3's on-board QSPI flash yet.
        //
        // qspi_io_i = 4'b1111 IS CHOSEN FOR THE BOOT PATH. stage0 reads the flash
        // JEDEC ID first and treats both 0xFFFFFF and 0x000000 as "no flash"
        // (stage0_bootloader.c, JEDEC_ID_EMPTY_FF/_00), then prints 'q' and falls
        // back to the IMEM image preloaded via RAM_PRELOAD. All-ones is what an
        // unpopulated, pulled-up QSPI bus actually reads, so this is the same
        // path a board with no flash fitted would take.
        .qspi_io_i       (4'b1111),
        .qspi_io_o       (),
        .qspi_io_e       (),
        .qspi_sclk       (),
        .qspi_csn        (),


        // GPIO
        .p0_in    (p0_in),
        .p0_out   (p0_out),
        .p0_outen (p0_outen),
        .p1_in    (p1_in),
        .p1_out   (p1_out),
        .p1_outen (p1_outen),

        // Internal clock / reset taps -- not used at the board level here
        .sys_hclk    (),
        .sys_hresetn (),

        // Expansion region -- tied off benign, mirrors the HW-validated
        // nanosoc_vivado_wrapper.v pattern (see header comment).
        .exp_hsel      (1'b0),
        .exp_haddr     (32'h0000_0000),
        .exp_htrans    (2'b00),
        .exp_hwrite    (1'b0),
        .exp_hsize     (3'b000),
        .exp_hburst    (3'b000),
        .exp_hprot     (4'b0000),
        .exp_hwdata    (32'h0000_0000),
        .exp_hmastlock (1'b0),
        .exp_hready    (1'b1),
        .exp_hrdata    (),
        .exp_hresp     (),
        .exp_hreadyout (),

        // Expansion streams -- DMA->exp discarded, exp->DMA tied inactive
        .exp_str_in_0_tvalid (),
        .exp_str_in_0_tready (1'b1),
        .exp_str_in_0_tdata  (),
        .exp_str_in_0_tstrb  (),
        .exp_str_in_0_tlast  (),
        .exp_str_in_1_tvalid (),
        .exp_str_in_1_tready (1'b1),
        .exp_str_in_1_tdata  (),
        .exp_str_in_1_tstrb  (),
        .exp_str_in_1_tlast  (),
        .exp_str_in_2_tvalid (),
        .exp_str_in_2_tready (1'b1),
        .exp_str_in_2_tdata  (),
        .exp_str_in_2_tstrb  (),
        .exp_str_in_2_tlast  (),
        .exp_str_out_0_tvalid(1'b0),
        .exp_str_out_0_tready(),
        .exp_str_out_0_tdata (32'h0000_0000),
        .exp_str_out_0_tstrb (4'b0000),
        .exp_str_out_0_tlast (1'b0),
        .exp_str_out_0_flush (),
        .exp_str_out_1_tvalid(1'b0),
        .exp_str_out_1_tready(),
        .exp_str_out_1_tdata (32'h0000_0000),
        .exp_str_out_1_tstrb (4'b0000),
        .exp_str_out_1_tlast (1'b0),
        .exp_str_out_1_flush (),
        .exp_str_out_2_tvalid(1'b0),
        .exp_str_out_2_tready(),
        .exp_str_out_2_tdata (32'h0000_0000),
        .exp_str_out_2_tstrb (4'b0000),
        .exp_str_out_2_tlast (1'b0),
        .exp_str_out_2_flush (),
        .exp_irq   (4'b0000),
        .exp_drq   (2'b00),
        .exp_dlast (),

        // SPI master pads (PL022 SSP) -- no board pin group in
        // nanosoc_mps3.xdc yet (see header comment); left unconnected.
        .spi_sclk  (),
        .spi_ss    (),
        .spi_mosi  (),
        .spi_miso  (1'b0)
    );

endmodule
