// -----------------------------------------------------------------------------
// uart2_rx_harness.sv — thin SV harness around nanosoc_ss_systemctrl for the
// M1 console-RX gate (tests/uart2_rx_path).
//
// It exists for two reasons:
//
// 1. AHB-Lite requires the global HREADY to reflect the selected slave's
//    HREADYOUT. The soc_peripheral region's AHB-to-APB bridge inserts wait
//    states by driving HREADYOUT low; a bench that hardwires HREADY=1 tells
//    the slave the bus is always ready and register writes are silently
//    dropped. In a single-slave system the correct wiring is a continuous
//    assign, which a Python driver cannot express cleanly:
//
//        HREADY = HREADYOUT
//
//    (Same pattern as tests/uart_echo_integration/uart_echo_harness.sv.)
//
// 2. P0_OUT/P0_OUTEN/P0_ALTFUNC and P1_OUT/P1_OUTEN/P1_ALTFUNC are OUTPUTS of
//    nanosoc_ss_systemctrl — they are driven by the CMSDK GPIO blocks inside
//    it. They must be OBSERVED, never driven. The pad busses P0_IN/P1_IN are
//    the only GPIO-side inputs. Getting this wrong in a bench produces
//    plausible-looking but meaningless results (a forced value fighting the
//    GPIO block's driver), so the directions are pinned here in RTL where the
//    elaborator will enforce them.
//
// The DUT is the real nanosoc_ss_systemctrl: real nanosoc_pin_mux, real Arm
// cmsdk_apb_uart as UART2. `uart2_rxd` — the pin mux's UART2 receive line, and
// the signal this whole gate is about — is surfaced as a port.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module uart2_rx_harness (
  input  wire        SYS_CLK,
  input  wire        SYS_HCLK,
  input  wire        SYS_SYSRESETn,
  input  wire        SYS_PORESETn,
  input  wire        SYS_HRESETn,

  // AHB into the soc_peripheral region. HREADY is tied to HREADYOUT internally.
  input  wire        SOC_PERIPHERAL_HSEL,
  input  wire [31:0] SOC_PERIPHERAL_HADDR,
  input  wire [ 1:0] SOC_PERIPHERAL_HTRANS,
  input  wire [31:0] SOC_PERIPHERAL_HWDATA,
  input  wire        SOC_PERIPHERAL_HWRITE,
  output wire [31:0] SOC_PERIPHERAL_HRDATA,
  output wire        SOC_PERIPHERAL_HREADYOUT,
  output wire        SOC_PERIPHERAL_HRESP,

  // GPIO pad inputs — the only GPIO-side inputs. P1_IN[4] is UART2's RXD pad.
  input  wire [15:0] P0_IN,
  input  wire [15:0] P1_IN,

  // GPIO drive — OUTPUTS of the DUT. Observe only.
  output wire [15:0] P0_OUT,
  output wire [15:0] P0_OUTEN,
  output wire [15:0] P1_OUT,
  output wire [15:0] P1_OUTEN,
  output wire [15:0] P1_ALTFUNC,

  // The signal under test: the pin mux's UART2 receive line.
  output wire        uart2_rxd
);

  // AHB-Lite single-slave: the global HREADY IS this slave's HREADYOUT.
  wire hreadyout;
  assign SOC_PERIPHERAL_HREADYOUT = hreadyout;

  // Surface the pin mux's uart2_rxd (an internal wire of the subsystem).
  assign uart2_rxd = u_dut.uart2_rxd;

  nanosoc_ss_systemctrl u_dut (
    .SYS_CLK                  (SYS_CLK),
    .SYS_FCLK                 (),
    .SYS_XTALCLK_OUT          (),
    .SYS_SYSRESETn            (SYS_SYSRESETn),
    .SYS_PORESETn             (SYS_PORESETn),
    .SYS_TESTMODE             (1'b0),
    .SYS_HCLK                 (SYS_HCLK),
    .SYS_HRESETn              (SYS_HRESETn),

    // AHB slave
    .SOC_PERIPHERAL_HSEL      (SOC_PERIPHERAL_HSEL),
    .SOC_PERIPHERAL_HADDR     (SOC_PERIPHERAL_HADDR),
    .SOC_PERIPHERAL_HBURST    (3'b000),   // SINGLE
    .SOC_PERIPHERAL_HMASTLOCK (1'b0),
    .SOC_PERIPHERAL_HPROT     (4'b0011),  // data, privileged
    .SOC_PERIPHERAL_HSIZE     (3'b010),   // word
    .SOC_PERIPHERAL_HTRANS    (SOC_PERIPHERAL_HTRANS),
    .SOC_PERIPHERAL_HWDATA    (SOC_PERIPHERAL_HWDATA),
    .SOC_PERIPHERAL_HWRITE    (SOC_PERIPHERAL_HWRITE),
    .SOC_PERIPHERAL_HREADY    (hreadyout),   // <-- reason (1) this file exists
    .SOC_PERIPHERAL_HRDATA    (SOC_PERIPHERAL_HRDATA),
    .SOC_PERIPHERAL_HRESP     (SOC_PERIPHERAL_HRESP),
    .SOC_PERIPHERAL_HREADYOUT (hreadyout),

    // Peripheral clocks / external APB fan-out (unused here)
    .SYS_PCLK                 (),
    .SYS_PCLKG                (),
    .SYS_PRESETn              (),
    .SYS_PCLKEN               (),
    .SOC_PERIPHERAL_PENABLE   (),
    .SOC_PERIPHERAL_PWRITE    (),
    .SOC_PERIPHERAL_PADDR     (),
    .SOC_PERIPHERAL_PWDATA    (),
    .USRT_PSEL                (),
    .USRT_PRDATA              (32'h0000_0000),
    .USRT_PREADY              (1'b1),
    .USRT_PSLVERR             (1'b0),

    // Interrupts / system control (observed nowhere, tied benignly)
    .SYS_NMI                  (),
    .SYS_APB_IRQ              (),
    .SYS_GPIO0_IRQ            (),
    .SYS_GPIO1_IRQ            (),
    .SYS_REMAP_CTRL           (),
    .SYS_WDOGRESETREQ         (),
    .SYS_LOCKUPRESET          (),
    .CPU_SYSRESETREQ          (1'b0),
    .CPU_PRMURESETREQ         (1'b0),
    .SYS_PMUENABLE            (),
    .SYS_PMUDBGRESETREQ       (1'b0),
    .CPU_LOCKUP               (1'b0),
    .CPU_SLEEPING             (1'b0),
    .CPU_SLEEPDEEP            (1'b0),

    // USRT byte streams (stubbed block — see socdebug_usrt_control_stub.sv)
    .USRT0_TXD_TVALID         (),
    .USRT0_TXD_TDATA          (),
    .USRT0_TXD_TREADY         (1'b1),
    .USRT0_RXD_TVALID         (1'b0),
    .USRT0_RXD_TDATA          (8'h00),
    .USRT0_RXD_TREADY         (),
    .USRT1_TXD_TVALID         (),
    .USRT1_TXD_TDATA          (),
    .USRT1_TXD_TREADY         (1'b1),
    .USRT1_RXD_TVALID         (1'b0),
    .USRT1_RXD_TDATA          (8'h00),
    .USRT1_RXD_TREADY         (),

    // GPIO: pads in, drive out
    .P0_IN                    (P0_IN),
    .P0_OUT                   (P0_OUT),
    .P0_OUTEN                 (P0_OUTEN),
    .P0_ALTFUNC               (),
    .P1_IN                    (P1_IN),
    .P1_OUT                   (P1_OUT),
    .P1_OUTEN                 (P1_OUTEN),
    .P1_ALTFUNC               (P1_ALTFUNC),
    .P1_OUT_MUX               (),
    .P1_OUT_EN_MUX            ()
  );

endmodule
