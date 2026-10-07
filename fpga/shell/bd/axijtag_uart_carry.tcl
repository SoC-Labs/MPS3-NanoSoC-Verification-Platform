###-----------------------------------------------------------------------------
### mps3-nanosoc-platform — MPS3 shell-BD carry-across of the PROVEN KR260/HAPS
### axi_jtag + axi_uart16550 + magic-GPIO debug/console architecture.
###
### A joint work commissioned on behalf of SoC Labs, under Arm Academic
### Access license.
###
### Contributors
###   SoC Labs (noreply@soclabs.org)
###
### Copyright (C) 2026, SoC Labs (www.soclabs.org)
###-----------------------------------------------------------------------------
###
### ============================ STATUS: STAGED / UNVALIDATED =================
### This file is "test-tomorrow" scaffolding. It has been authored by faithfully
### copying the PROVEN KR260 block-design CONFIG dicts and adapting ONLY the
### three MPS3-specific axes (base addresses, TCK ratio, and the 16550 baud
### clk_wiz solve). It has NOT been run through Vivado, elaborated, validated,
### synthesised, or put on a board. Do not represent it as verified. Every
### vendor-IP CONFIG property name, enum value, pin name, and address-segment
### name below is reproduced from the KR260 reference and the MPS3 shell_bd.tcl
### conventions; minor-version drift across the KR260 Vivado and MPS3's 2024.1
### is possible and must be confirmed against the live Customize-IP dialog.
### ===========================================================================
###
### WHAT THIS ADDS (mirrors the reference triplet, MPS3 addresses):
###   axi_jtag        @ 0x44AF_0000  (64K)  — AXI->JTAG shift engine, drives the
###                                           DUT SWJ-DP in JTAG mode. TCK =
###                                           s_axi_aclk / C_TCK_CLOCK_RATIO.
###   axi_gpio magic  @ 0x44B0_0000  (64K)  — reads 0x4A544147 ("JTAG"): a
###                                           pre-flight "aperture alive" probe.
###   axi_uart16550   @ 0x44B1_0000  (64K)  — ns16550a console endpoint, baud
###                                           reference = dedicated 9.216 MHz
###                                           clk_wiz output (DL=5 -> 115200).
###
### HOW TO SOURCE IT INTO shell_bd.tcl  (do NOT edit shell_bd.tcl to inline it;
### a re-mint is running against that file — source this alongside instead):
###
###   1. In the build wrapper (e.g. build_shell.tcl / validate_bd.tcl), AFTER
###      `source .../shell_bd.tcl` but BEFORE it calls create_root_design's
###      validate/save, also:
###          source [file join $bd_dir axijtag_uart_carry.tcl]
###   2. Inside create_root_design, at the END of SECTION 6 (after the existing
###      M00..M17 mi_map foreach and its assign_bd_address block, i.e. after
###      shell_bd.tcl:1329), add ONE call:
###          add_axijtag_uart_carry            ;# staged: DUT pins left stubbed
###      When (and only when) the SWD->JTAG partition-boundary re-mint has wired
###      the real DUT-facing pins, call instead:
###          add_axijtag_uart_carry 0          ;# no placeholder tie-offs
###   3. The proc re-fetches the shell handles (clk_wiz_shell, proc_sys_reset_
###      shell, axi_interconnect_0, osc_clk_50m, sys_rst_n) by NAME from the
###      current BD instance — it does NOT depend on shell_bd.tcl's local Tcl
###      variables (Tcl proc scope), exactly like the KR260 create_root_design
###      re-fetches everything through get_bd_pins.
###   4. It does NOT call validate_bd_design / save_bd_design — the caller
###      (build_shell.tcl) still owns that, same as shell_bd.tcl.
###
### EXACT docs/contracts/shell-regmap.md ADDITIONS (3 new rows; bump the header
### note "NUM_MI = 18 -> 21" and "assign_bd_address ... stops at CLCDKVM 0x44AD
### (0x44AE with SHELL_TOUCH=1) -> now extends to AXIJTAG/MAGICID/UART16550
### 0x44AF/0x44B0/0x44B1"):
###
###   | Block     | Base          | Size | Notes                                |
###   |-----------|---------------|------|--------------------------------------|
###   | AXIJTAG   | 0x44AF_0000   | 64K  | axi_jtag:1.0 AXI->JTAG to DUT SWJ-DP; |
###   |           |               |      | TCK = shell_clk/32 = 3.125 MHz;       |
###   |           |               |      | 64K-aligned => mmap-able page.        |
###   | MAGICID   | 0x44B0_0000   | 64K  | axi_gpio, all-inputs=32'h4A544147     |
###   |           |               |      | ("JTAG"); pre-flight liveness probe   |
###   |           |               |      | (equiv. to DFXCTL.RM_ID).             |
###   | UART16550 | 0x44B1_0000   | 64K  | axi_uart16550:2.0, ns16550a; regfile  |
###   |           |               |      | @ +0x1000 (reg-offset=<0x1000>); xin  |
###   |           |               |      | =9.216 MHz => DL=5 exact 115200;      |
###   |           |               |      | 64K >= 0x2000 min aperture.           |
###
###   Device-tree hint for UART16550 (carried from the KR260 address_map.txt):
###     compatible="xlnx,xps-uart16550-2.00.a","ns16550a"; reg-offset=<0x1000>;
###     reg-shift=<2>; reg-io-width=<1>; clock-frequency=<9216000> (the XIN, not
###     aclk!); current-speed=<115200>. (Interrupt row omitted — see the IRQ
###     TODO below; the 8250 driver polls happily without it.)
###
### EVERY VALUE CHANGED FROM THE KR260 REFERENCE (vs faithfully COPIED)
### Reference: nanosoc-multicore-system/pynq/targets/kr260-axijtag-uart/
###            nanosoc_multicore_jtag_uart_design.tcl  (line cites below)
###
###   CHANGED (3 intended axes + the master/fabric-plumbing deltas MPS3 forces):
###     - axi_jtag C_TCK_CLOCK_RATIO : 8  -> 32   (KR260 line 268). Reason: the
###       MPS3 shell AXI runs at 100 MHz (KR260 s_axi_aclk was 25 MHz). 100/32 =
###       3.125 MHz reproduces the proven KR260 TCK (25/8). ALT: keep ratio 8 and
###       feed axi_jtag/s_axi_aclk from a 25 MHz shell clock (÷8) — see the inline
###       note. *** CONFIRM axi_jtag:1.0's C_TCK_CLOCK_RATIO ceiling accepts 32. ***
###     - base addresses : 0x8000_0000 / 0x8001_0000 / 0x8002_0000 (KR260 lines
###       125-131, address_map.txt 7-9) -> 0x44AF_0000 / 0x44B0_0000 / 0x44B1_0000
###       (MPS3 0x44A CSR aperture; next free 64K pages after TOUCH 0x44AE,
###       shell_bd.tcl:1329). RELATIVE +0x0/+0x1_0000/+0x2_0000 layout PRESERVED.
###     - clk_wiz_uart PRIM_IN_FREQ / solve : KR260 solved 9.216 MHz from ~100 MHz
###       PL0_REF, M=76.125 D=8 O=103.250 (KR260 lines 236-246, address_map.txt
###       23). MPS3 re-solves 9.216 MHz from the 50 MHz board osc (osc_clk_50m),
###       PRIM_SOURCE No_buffer to match clk_wiz_shell/clk_wiz_dut. See the M/D/O
###       intent comment at the clk_wiz create. DL=5 -> 115200 UNCHANGED.
###     - fabric plumbing : KR260's SmartConnect NUM_MI 3 slave fan-out (KR260
###       line 257) is replaced by 3 NEW master ports (M18/M19/M20) on the EXISTING
###       MPS3 axi_interconnect:2.1 (NUM_MI 18 -> 21). The AXI master is the shell
###       MicroBlaze v11, NOT a Zynq PS — so NONE of KR260's zynq_ultra_ps_e /
###       apply_board_preset / M_AXI_HPM0_LPD / pl_clk0 / pl_resetn0 block is
###       carried (KR260 lines 190-201, 375-425). Clocks/resets come from the
###       shell's clk_wiz_shell(100MHz)/proc_sys_reset_shell instead.
###     - the SoC cell + its dap_tck/tms/tdi/tdo and uart_txd/uart_rxd nets
###       (KR260 lines 368-370, 443-493) are NOT instantiated here: on MPS3 the
###       DUT lives in the reconfigurable partition, reached only across the RP
###       boundary. Those connections are left as clearly-marked TODO stubs (see
###       the DUT-FACING section at the end). This is the ONE genuine new cost
###       that neither reference board faces.
###
###   COPIED FAITHFULLY (do not "improve"):
###     - axi_jtag VLNV xilinx.com:ip:axi_jtag:1.0                (KR260 267)
###     - axi_gpio magic: C_ALL_INPUTS 1 / C_GPIO_WIDTH 32 /
###       C_IS_DUAL 0 / C_INTERRUPT_PRESENT 0 + xlconstant 0x4A544147
###                                                               (KR260 275-287)
###     - axi_uart16550 VLNV :2.0, C_IS_A_16550 16550,
###       C_HAS_EXTERNAL_XIN 1, C_EXTERNAL_XIN_CLK_HZ_d 9.216,
###       C_HAS_EXTERNAL_RCLK 0                                   (KR260 311-317)
###     - 16550 modem/debug tie-offs: ctsn/dcdn/dsrn/freeze = 0, rin = 1
###       (dcdn=0 or open("/dev/ttyS*") BLOCKS on carrier; ctsn=0 or the TX
###       stalls the moment MCR.AFE is set)                       (KR260 336-362,
###                                                                       457-464)
###     - the 9.216 MHz "dedicated second MMCM, never clock the 16550 from aclk"
###       doctrine + DL=5/exact-115200 baud recipe            (KR260 47-96, 224-246)
###     - address-segment names: axi_jtag s_axi/reg0, axi_gpio S_AXI/Reg,
###       axi_uart16550 S_AXI/Reg                                 (KR260 515-522)
###
### OPEN ITEMS (see README_axijtag_carry.md for the full ledger):
###   - DUT-facing JTAG pins cross the RP partition boundary  => a SEPARATE
###     partition-pins re-mint (SWD group -> JTAG group). STUBBED below.
###   - 16550 sin/sout come from a shell-side byte-shim off uart_bridge_0's
###     FIFOs (NO boundary change). STUBBED below.
###   - No host driver exists even in the reference ("OpenOCD mmap()s axi_jtag"
###     is aspirational) — on MPS3 a MicroBlaze firmware JTAG/XVC server is
###     needed until MB-V Linux can mmap 0x44AF_0000.
###   - ip2intc_irpt is left unconnected (poll-mode). Wiring it to axi_intc_0
###     is a later, optional integration step.
###-----------------------------------------------------------------------------

# Address map + magic value + baud reference. Kept as ::-scope constants so a
# host stack / device-tree overlay generator can `source` this file purely to
# read them, exactly as the KR260 file exposes ::NANOSOC_* (KR260 125-135).
set ::MPS3_AXIJTAG_BASE     0x44AF0000   ;# CHANGED from KR260 0x80000000
set ::MPS3_AXIJTAG_RANGE    64K
set ::MPS3_MAGICID_BASE     0x44B00000   ;# CHANGED from KR260 0x80010000
set ::MPS3_MAGICID_RANGE    64K
set ::MPS3_MAGICID_VAL      0x4A544147   ;# COPIED  ("JTAG")
set ::MPS3_UART16550_BASE   0x44B10000   ;# CHANGED from KR260 0x80020000
set ::MPS3_UART16550_RANGE  64K
# 16550 baud reference in MHz (== device-tree clock-frequency / 1e6). COPIED
# from KR260 ::NANOSOC_UART_XIN_MHZ (KR260 135). DL = xin / (16 * 115200) = 5.
set ::MPS3_UART16550_XIN_MHZ 9.216
# TCK ratio: CHANGED 8 -> 32 so 100 MHz / 32 = 3.125 MHz (= KR260's 25 MHz / 8).
set ::MPS3_AXIJTAG_TCK_RATIO 32


###-----------------------------------------------------------------------------
### proc add_axijtag_uart_carry {stub_dut_pins}
###   stub_dut_pins (default 1): when 1, create explicit PLACEHOLDER tie-offs on
###     the DUT-facing INPUT pins (axi_jtag/tdo, axi_uart16550/sin) so intent is
###     loud and there is one obvious place to delete them. Pass 0 once the real
###     SWD->JTAG boundary re-mint + uart byte-shim drive those pins, to avoid a
###     multi-driver conflict.
###   Call from INSIDE create_root_design (current_bd_instance == root), AFTER
###   the interconnect and its M00..M17 map exist.
###-----------------------------------------------------------------------------
proc add_axijtag_uart_carry { {stub_dut_pins 1} } {

    ###################################################################
    # Re-fetch the shell's fixed-name handles from the live BD (proc scope
    # does not see shell_bd.tcl's locals — same discipline as the KR260 file).
    ###################################################################
    set ic         axi_interconnect_0
    set shell_clk    [get_bd_pins  clk_wiz_shell/clk_out1]            ;# 100 MHz
    set shell_aresetn [get_bd_pins proc_sys_reset_shell/peripheral_aresetn]
    set osc_clk    [get_bd_ports osc_clk_50m]                        ;# 50 MHz board osc
    set sys_rst_n  [get_bd_ports sys_rst_n]                          ;# board reset, active-low

    ###################################################################
    # Grow the EXISTING shell interconnect by 3 master ports.
    # shell_bd.tcl:1250 sets NUM_MI 18 and wires M00..M17; we append
    # M18=AXIJTAG, M19=MAGICID, M20=UART16550. (KR260 instead used a fresh
    # 3-slave SmartConnect, KR260 257 — not applicable; MPS3 already has the
    # MicroBlaze-mastered axi_interconnect.)
    ###################################################################
    set_property CONFIG.NUM_MI {21} [get_bd_cells $ic]   ;# CHANGED: 18 -> 21

    ###################################################################
    # axi_jtag — "AXI To JTAG Converter" (xilinx.com:ip:axi_jtag:1.0). COPIED
    # from KR260 267-268 EXCEPT C_TCK_CLOCK_RATIO (8 -> 32; see header).
    #
    # TCK = s_axi_aclk / C_TCK_CLOCK_RATIO. On MPS3 s_axi_aclk = shell_clk =
    # 100 MHz, so ratio 32 => 3.125 MHz, matching the proven KR260 TCK (25/8).
    # OpenOCD `adapter speed` is INERT — this ratio (with the clock) is the knob.
    #
    # ALTERNATIVE if axi_jtag:1.0 rejects ratio 32 (CONFIRM the IP ceiling):
    #   keep C_TCK_CLOCK_RATIO 8 and feed $jtag/s_axi_aclk from a dedicated
    #   25 MHz shell clock (e.g. a 3rd clk_wiz_shell output) instead of the
    #   100 MHz shell_clk. 25/8 = 3.125 MHz, identical result.
    ###################################################################
    set jtag [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_jtag:1.0 axi_jtag_carry]
    set_property -dict [list CONFIG.C_TCK_CLOCK_RATIO $::MPS3_AXIJTAG_TCK_RATIO] $jtag

    ###################################################################
    # MAGIC-ID REGISTER — pre-flight liveness probe. COPIED from KR260 275-287.
    #   mrd 0x44B00000  ->  0x4A544147  ("JTAG")
    # (DFXCTL.RM_ID already gives an equivalent check on MPS3, so this block is
    #  optional — kept for parity with the proven reference triplet.)
    ###################################################################
    set magic_gpio [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_gpio:2.0 axi_gpio_magic_carry]
    set_property -dict [list \
        CONFIG.C_ALL_INPUTS        {1}  \
        CONFIG.C_GPIO_WIDTH        {32} \
        CONFIG.C_IS_DUAL           {0}  \
        CONFIG.C_INTERRUPT_PRESENT {0}  \
    ] $magic_gpio

    set magic_const [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 magic_id_const_carry]
    set_property -dict [list \
        CONFIG.CONST_WIDTH {32} \
        CONFIG.CONST_VAL   [expr {$::MPS3_MAGICID_VAL}] \
    ] $magic_const

    ###################################################################
    # CLOCKING — a dedicated SECOND MMCM purely for the 16550 baud reference.
    # COPIED doctrine from KR260 224-246 ("never clock the 16550 from aclk;
    # 25 MHz aclk -> DL=14 -> -3.1% -> corrupt frames"). On MPS3 the DRC ceiling
    # is xin <= s_axi_aclk/2 = 100/2 = 50 MHz, so 9.216 MHz is comfortably legal
    # (even more margin than KR260's 25 MHz aclk).
    #
    # CHANGED: input is the 50 MHz board oscillator (osc_clk_50m), not KR260's
    # ~100 MHz PL0_REF. PRIM_SOURCE No_buffer matches clk_wiz_shell/clk_wiz_dut
    # (shell_bd.tcl:226,245), which already IBUF/BUFG osc_clk_50m upstream.
    #
    #   M/D/O INTENT (50 MHz in -> 9.216 MHz out):
    #     ideal  : VCO = 921.6 MHz, O = 100.000  -> 9.216000 MHz EXACT
    #              (M/D = 18.432; not on the 0.125 MMCM grid, so unrealisable)
    #     solver : lands on the nearest achievable grid point, e.g.
    #              M = 17.375, D = 1, O = 94.250 -> VCO 868.75 MHz ->
    #              9.21751 MHz (+0.016 %, ~+160 ppm) -- ANALOGOUS to KR260's
    #              +1 ppm (M=76.125/D=8/O=103.250 from 99.999 MHz). The
    #              CLKOUT1_REQUESTED_OUT_FREQ below lets 2024.1's clk_wiz solve
    #              it; the exact M/D/O it prints is what the device tree's
    #              clock-frequency must match (nominal 9216000, DL = 5 either way).
    #   DL = round(xin / (16 * 115200)) = round(9.216e6 / 1843200) = 5  -> 115200
    ###################################################################
    set clk_wiz_uart [create_bd_cell -type ip -vlnv xilinx.com:ip:clk_wiz:6.0 clk_wiz_uart_carry]
    set_property -dict [list \
        CONFIG.PRIM_IN_FREQ               {50.000}                    \
        CONFIG.PRIM_SOURCE                {No_buffer}                 \
        CONFIG.CLKOUT1_REQUESTED_OUT_FREQ $::MPS3_UART16550_XIN_MHZ   \
        CONFIG.CLKOUT1_USED               {true}                      \
        CONFIG.NUM_OUT_CLKS               {1}                         \
        CONFIG.USE_LOCKED                 {false}                     \
        CONFIG.USE_RESET                  {true}                      \
        CONFIG.RESET_TYPE                 {ACTIVE_LOW}                \
        CONFIG.CLKIN1_JITTER_PS           {160.0}                     \
    ] $clk_wiz_uart
    # locked unused (COPIED KR260 rationale, lines 232-234): nothing is gated on
    # this clock; the AXI side is held in reset by proc_sys_reset_shell (which
    # watches clk_wiz_shell/locked) far longer than an MMCM takes to lock.

    ###################################################################
    # axi_uart16550 — the ns16550a console endpoint. COPIED from KR260 311-317.
    #   C_IS_A_16550       16550 : 16-deep FIFOs (a 16450 would need a
    #                              per-character IRQ at 115200).
    #   C_HAS_EXTERNAL_XIN 1     : take the baud reference from the xin pin, not
    #                              s_axi_aclk -- THE WHOLE POINT (header CLOCK #2).
    #   C_EXTERNAL_XIN_CLK_HZ_d  : 9.216 (MHz); IP derives Hz = 9216000.
    #   C_HAS_EXTERNAL_RCLK 0    : RX clock = internal BAUDOUT (standard 16550).
    # C_S_AXI_ACLK_FREQ_HZ_d is propagate-only (IP fills it from the connected
    # s_axi_aclk); on MPS3 it lands on ~100 MHz, well inside the IP's 25..300 MHz
    # AXI-clock DRC (KR260 306-309).
    ###################################################################
    set uart16550 [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_uart16550:2.0 axi_uart16550_carry]
    set_property -dict [list \
        CONFIG.C_IS_A_16550            {16550}                      \
        CONFIG.C_HAS_EXTERNAL_XIN      {1}                          \
        CONFIG.C_EXTERNAL_XIN_CLK_HZ_d $::MPS3_UART16550_XIN_MHZ    \
        CONFIG.C_HAS_EXTERNAL_RCLK     {0}                          \
    ] $uart16550

    ###################################################################
    # 16550 modem / debug tie-offs. COPIED from KR260 336-362, 457-464.
    #   ctsn=0 dcdn=0 dsrn=0 freeze=0 (asserted / off), rin=1 (RI deasserted).
    # C_USE_MODEM_PORTS is force-enabled inside the IP, so these pins exist and
    # MUST be driven. dcdn=0 is load-bearing: without it open("/dev/ttyS*")
    # BLOCKS on carrier unless userspace passes CLOCAL/O_NONBLOCK.
    ###################################################################
    set const_uart_lo [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 const_uart_lo_carry]
    set_property -dict [list CONFIG.CONST_WIDTH {1} CONFIG.CONST_VAL {0}] $const_uart_lo
    set const_uart_hi [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 const_uart_hi_carry]
    set_property -dict [list CONFIG.CONST_WIDTH {1} CONFIG.CONST_VAL {1}] $const_uart_hi

    ###################################################################
    # CLOCKS
    ###################################################################
    # 100 MHz shell clock -> every AXI side + the axi_jtag TCK reference.
    connect_bd_net $shell_clk \
        [get_bd_pins $jtag/s_axi_aclk]       \
        [get_bd_pins $magic_gpio/s_axi_aclk] \
        [get_bd_pins $uart16550/s_axi_aclk]
    # clk_wiz_uart input = the 50 MHz board osc (same net clk_wiz_shell/dut use).
    connect_bd_net $osc_clk [get_bd_pins $clk_wiz_uart/clk_in1]
    # 9.216 MHz baud reference -> 16550 xin. This net is the single reason
    # 115200 is exact (DL=5).
    connect_bd_net [get_bd_pins $clk_wiz_uart/clk_out1] [get_bd_pins $uart16550/xin]

    ###################################################################
    # RESETS
    ###################################################################
    # clk_wiz_uart resetn <- board reset, matching clk_wiz_shell (shell_bd.tcl:282).
    connect_bd_net $sys_rst_n [get_bd_pins $clk_wiz_uart/resetn]
    # AXI-side resets <- shell peripheral_aresetn (same as every shell CSR block).
    connect_bd_net $shell_aresetn \
        [get_bd_pins $jtag/s_axi_aresetn]       \
        [get_bd_pins $magic_gpio/s_axi_aresetn] \
        [get_bd_pins $uart16550/s_axi_aresetn]

    ###################################################################
    # AXI — attach the three new slaves as interconnect master ports M18/M19/M20.
    # Mirrors shell_bd.tcl's mi_map foreach discipline (each master port needs
    # its own M<NN>_ACLK/M<NN>_ARESETN; the bus pin is M<NN>_AXI). shell_bd.tcl
    # 1284-1294.
    ###################################################################
    set carry_mi [list \
        18 $jtag       s_axi \
        19 $magic_gpio S_AXI \
        20 $uart16550  S_AXI \
    ]
    foreach {idx cell intf} $carry_mi {
        set mNN [format "M%02d" $idx]
        set mi  [format "M%02d_AXI" $idx]
        connect_bd_net $shell_clk     [get_bd_pins $ic/${mNN}_ACLK]
        connect_bd_net $shell_aresetn [get_bd_pins $ic/${mNN}_ARESETN]
        connect_bd_intf_net [get_bd_intf_pins $ic/$mi] [get_bd_intf_pins $cell/$intf]
    }

    # magic constant -> gpio inputs.
    connect_bd_net [get_bd_pins $magic_const/dout] [get_bd_pins $magic_gpio/gpio_io_i]

    # 16550 modem tie-offs.
    connect_bd_net [get_bd_pins $const_uart_lo/dout] \
        [get_bd_pins $uart16550/freeze] \
        [get_bd_pins $uart16550/ctsn]   \
        [get_bd_pins $uart16550/dcdn]   \
        [get_bd_pins $uart16550/dsrn]
    connect_bd_net [get_bd_pins $const_uart_hi/dout] [get_bd_pins $uart16550/rin]

    ###################################################################
    # ADDRESS MAP — explicit, deterministic, 64K-aligned (mmap-able).
    # Segment names COPIED from KR260 515-522. Bases CHANGED to the MPS3 0x44A
    # aperture. Follows shell_bd.tcl:1308-1329 assign_bd_address style.
    ###################################################################
    assign_bd_address -offset $::MPS3_AXIJTAG_BASE   -range $::MPS3_AXIJTAG_RANGE \
        [get_bd_addr_segs [list $jtag/s_axi/reg0]]      -quiet   ;# AXIJTAG
    assign_bd_address -offset $::MPS3_MAGICID_BASE   -range $::MPS3_MAGICID_RANGE \
        [get_bd_addr_segs [list $magic_gpio/S_AXI/Reg]] -quiet   ;# MAGICID
    assign_bd_address -offset $::MPS3_UART16550_BASE -range $::MPS3_UART16550_RANGE \
        [get_bd_addr_segs [list $uart16550/S_AXI/Reg]]  -quiet   ;# UART16550
    #   16550 regfile lives at base+0x1000 (reg-offset), so the >=0x2000 aperture
    #   rule (KR260 address_map.txt 63-67) is satisfied by the 64K page.

    ###################################################################
    # ================= DUT-FACING PINS: TODO STUBS ONLY =================
    # These are the ONE genuinely new integration cost on MPS3 (the DUT is in
    # the reconfigurable partition; neither reference board crosses an RP
    # boundary). The real wiring is DELIBERATELY NOT authored here.
    #
    # ---- (A) axi_jtag <-> DUT SWJ-DP : SEPARATE partition-pins re-mint ----
    # KR260 wired the JTAG master straight to the SoC (KR260 443-446):
    #     jtag/tck -> soc/dap_tck ; jtag/tms -> soc/dap_tms ;
    #     jtag/tdi -> soc/dap_tdi ; soc/dap_tdo -> jtag/tdo
    # On MPS3 tck/tms/tdi (OUTPUTS) and tdo (INPUT) must reach the DUT ACROSS
    # the RP partition boundary. Today that boundary carries 4-wire SWD
    # (partition-pins.md "Processor debug": rp_swd_clk / rp_swd_dio_o /
    # rp_swd_dio_oe / rp_swd_dio_i, driven by swd_bb_0 via the dfx_decoupler,
    # shell_bd.tcl:755-760). Adopting axi_jtag SWAPS that group for a 4-wire
    # JTAG group (e.g. rp_jtag_tck / rp_jtag_tms / rp_jtag_tdi / rp_jtag_tdo).
    # *** ANY change to the RP boundary is a FULL partition re-mint: it re-keys
    #     static_id AND rebuilds every RM partial from RTL + re-runs pin_check.
    #     That is a separate, larger decision than this static-slave add. ***
    # DO NOT wire these here. When the boundary re-mint lands, connect (names
    # illustrative — the real RP-facing port names are a partition-pins.md
    # deliverable, NOT invented here):
    #     connect_bd_net [get_bd_pins axi_jtag_carry/tck] [get_bd_ports rp_jtag_tck]  ;# TODO
    #     connect_bd_net [get_bd_pins axi_jtag_carry/tms] [get_bd_ports rp_jtag_tms]  ;# TODO
    #     connect_bd_net [get_bd_pins axi_jtag_carry/tdi] [get_bd_ports rp_jtag_tdi]  ;# TODO
    #     connect_bd_net [get_bd_ports rp_jtag_tdo] [get_bd_pins axi_jtag_carry/tdo]  ;# TODO (through decoupler clamp, RP->static)
    #
    # ---- (B) axi_uart16550 <-> uart_bridge byte-shim : NO boundary change ----
    # KR260 crossed a RAW serial pair (soc/uart_txd <-> 16550 sin/sout, KR260
    # 486-493). MPS3 instead crosses the console as an AXI-Stream BYTE interface
    # into uart_bridge_0 (uart_tx_tdata/tvalid, uart_rx_tready; shell_bd.tcl
    # 771-776) which does the dut_clk<->shell_clk async-FIFO CDC. The 16550's
    # sin (serial IN) / sout (serial OUT) must therefore come from a SHELL-SIDE
    # byte-shim that (de)serialises against uart_bridge_0's FIFOs -- a shim that
    # DOES NOT EXIST YET and is NOT invented here. Zero partition-boundary change.
    #     connect_bd_net [get_bd_pins <uart_byte_shim>/sout_to_16550] [get_bd_pins axi_uart16550_carry/sin]   ;# TODO
    #     connect_bd_net [get_bd_pins axi_uart16550_carry/sout] [get_bd_pins <uart_byte_shim>/sin_from_16550]  ;# TODO
    #
    # ---- (C) 16550 interrupt : left unconnected (poll mode) ----
    # KR260 wired uart16550/ip2intc_irpt -> ps/pl_ps_irq0 (KR260 437). MPS3 has
    # no PS; the equivalent is a spare axi_intc_0 input. The ns16550/8250 driver
    # polls happily without it (KR260 address_map.txt 59-61), so it is left
    # dangling. axi_jtag has no interrupt.
    #     connect_bd_net [get_bd_pins axi_uart16550_carry/ip2intc_irpt] [get_bd_pins <axi_intc_0 concat input>]  ;# TODO(optional)
    #
    # ---- Placeholder tie-offs on the DUT-facing INPUTS (staging only) ----
    # validate_bd_design AUTO-TIES dangling INPUTS to 0 (CRITICAL WARNING, not a
    # hard error -- shell_bd.tcl:1081,1179,1226). With stub_dut_pins=1 we make
    # that explicit so the intent is loud and there is ONE place to delete. Pass
    # stub_dut_pins=0 once (A)/(B) drive tdo/sin for real, to avoid a
    # multi-driver conflict.
    ###################################################################
    if { $stub_dut_pins } {
        set stub_lo [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 axijtag_carry_dut_stub_lo]
        set_property -dict [list CONFIG.CONST_WIDTH {1} CONFIG.CONST_VAL {0}] $stub_lo
        # PLACEHOLDER — DELETE when (A) wires the real DUT dap_tdo across the boundary.
        connect_bd_net [get_bd_pins $stub_lo/dout] [get_bd_pins $jtag/tdo]
        # PLACEHOLDER — DELETE when (B) wires the real byte-shim to the 16550 sin.
        connect_bd_net [get_bd_pins $stub_lo/dout] [get_bd_pins $uart16550/sin]
        # (axi_jtag tck/tms/tdi and uart16550 sout are OUTPUTS -> legal dangling;
        #  validate only auto-ties dangling INPUTS. Left open for (A)/(B).)
        puts "add_axijtag_uart_carry: STAGED -- DUT-facing tdo/sin are PLACEHOLDER-tied to 0; wire the real RP-boundary JTAG + uart byte-shim, then call with stub_dut_pins=0."
    }

    # NOTE: no validate_bd_design / save_bd_design here -- the caller
    # (build_shell.tcl, after create_root_design) owns that, same as shell_bd.tcl.
}
