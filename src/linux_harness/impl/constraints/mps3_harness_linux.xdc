# DEPRECATED 2026-09-23 (LINUX_HARNESS_PLAN_2026-09-23.md DL2): the July Linux
# fork is superseded by the ONE shell BD's CPU seam -- fpga/shell/bd/shell_bd.tcl +
# cpu_mbv.tcl (SHELL_CPU=mbv), fpga/shell/build_shell.tcl and validate_bd.tcl,
# fpga/shell/shell_top.sv (`ifdef MPS3_SHELL_CPU_MBV), fpga/shell/constraints/mbv/.
# Do not edit or build this file; it is deleted at landing (lead).
##############################################################################
# mps3_harness_linux.xdc -- TRANSPLANT (shell_linux_bd) pin constraints.
# COPY of the donor fpga/shell/constraints/mps3_harness.xdc (main repo,
# branch feat/clcd-kvm-display) with EXACTLY THREE deviations, each marked
# [LINUX-DEV-n] inline:
#   [LINUX-DEV-1] console UART re-pinned from FT4232 lane 1 (AE30/AE31) to
#       lane 2 (AD28/AE28) and renamed LINUX_UART_TXD/RXD. Lane 2 is the ONLY
#       lane with on-bench proof of reaching the host FT4232 (linux_soc
#       gen_pins.py provenance), and the FT4232 if02 console is the
#       silicon-proven lane for this exact kernel/OpenSBI stack (2026-07-16
#       bring-up). Lane 2's historical reservation was for a physical DUT-UART
#       fallback that v0 never wired (DUT console is uart_bridge-over-network).
#   [LINUX-DEV-2] USER_nLED[0] pad (AU32) is driven by the BD's
#       calib_complete_led_n (LED LIT = DDR4 calibrated -- same bench meaning
#       as the proven linux_soc build). board_gpio LED bit0 becomes
#       readback-only (virtual). No constraint change here, wiring change in
#       shell_linux_top.sv.
#   [LINUX-DEV-3] PULLTYPE PULLUP added on USER_nPB0 (linux_soc board_pins.xdc
#       safety add: a floating POR pad must not glitch the DDR4 MIG into
#       reset).
# Plus the DDR4 additions, which live in ddr4_pins.xdc (verbatim proven copy)
# -- NOT in this file.
#
# Donor header follows.
##############################################################################
# mps3_harness.xdc -- static-shell harness pin constraints (KU115 / MPS3 HBI0309C)
#
# Scope: pins the static shell drives/reads directly (board oscillator,
# reset button, status LEDs + DIP switches via board_gpio, the complete
# LAN9220 SMC bus, the MicroBlaze's own bring-up/console UART, the overlay
# QSPI). This file constrains fpga/shell/shell_top.sv (the harness top; its
# port names match this file 1:1) -- NOT the DUT/RP (that boundary is
# entirely partition pins per docs/contracts/partition-pins.md, no board
# pads there).
#
# Provenance -- every group below is copied from, or cross-checked against,
# THREE independent sources that already agree (do not re-derive package
# pins from scratch):
#   1. nanosoc_arch_tech/fpga/fpga/targets/arm_mps3/fpga_pinmap.xdc
#      (raw legacy 2021.1 pin/IOSTANDARD list -- REFERENCE ONLY, do not
#      modify that file, per task scope)
#   2. fpga/monolithic/nanosoc_mps3.xdc (this project's own W1 port of (1),
#      reorganized by board subsystem with provenance comments per group --
#      "LAN9220 Ethernet SMC interface" + "SMBF_* static memory bus" groups)
#   3. fpga/dfx/proof/proof.xdc (this project's OWN already-HW-verified
#      subset: OSCCLK[1], USER_nPB[0], USER_nLED[7:0] -- "Pins verified
#      against the live xcku115-flvb1760-1-c package")
#
# This file supersedes/extends (3) with the full harness pin set; (3)'s
# clock/reset/LED constraints are copied here unchanged (same package pins,
# same port names) so mps3_harness.xdc can stand alone as the harness's
# constraints entry point -- fpga/dfx/proof/ keeps its own copy for the
# narrower DFX-proof static, no cross-file dependency introduced.
#
# Target: Vivado 2024.1, part xcku115-flvb1760-1-c (Arm MPS3, Kintex UltraScale)
# Config voltage / bitstream properties: copied at the END of this file from
# fpga/monolithic/nanosoc_mps3.xdc (already proven on this part) -- this file
# is now the harness build's standalone constraints entry point.
##############################################################################

##############################################################################
# Board oscillator -- OSCCLK[1] (50 MHz), the harness's DUT/system clock source
# Provenance: fpga_timing.xdc line 8 (create_clock, 20.000 ns / 50 MHz);
# fpga/dfx/proof/proof.xdc (package pin + IOSTANDARD, HW-verified).
##############################################################################
set_property PACKAGE_PIN AK16    [get_ports OSCCLK1]
set_property IOSTANDARD LVCMOS18 [get_ports OSCCLK1]
create_clock -period 20.000 -name dut_clk -waveform {0.000 10.000} [get_ports OSCCLK1]
# NOTE: this is the BOARD OSCILLATOR net feeding clk_wiz_dut / clk_wiz_shell
# (fpga/shell/bd/shell_bd.tcl SECTION 1) -- the clock objects those IP cells
# generate downstream (the actual DUT clock into the RP, and the shell's own
# ~100 MHz fixed AXI/MicroBlaze/ICAP clock) are NAMED DIFFERENTLY once
# clk_wiz elaborates; do not confuse `dut_clk` (this file's name for the raw
# 50 MHz board oscillator clock OBJECT) with the `dut_clk` PARTITION PIN
# (docs/contracts/partition-pins.md) which is the clk_wiz OUTPUT -- same
# English name, different clock domain, see mps3_harness_timing.xdc.

##############################################################################
# Push-button USER_nPB[0] (active-low) -- board-level system reset source
# Provenance: fpga/dfx/proof/proof.xdc (HW-verified); fpga/monolithic/nanosoc_mps3.xdc
##############################################################################
set_property PACKAGE_PIN AT30    [get_ports USER_nPB0]
set_property IOSTANDARD LVCMOS18 [get_ports USER_nPB0]
set_property PULLTYPE PULLUP     [get_ports USER_nPB0]   ;# [LINUX-DEV-3] linux_soc safety add (DDR4 MIG reset glitch guard)

##############################################################################
# Push-button USER_nPB[1] (active-low, AT32) -- CLCD-KVM ownership toggle
# (Wave 4). Was unconstrained in the shell until now. Same bank/IOSTANDARD as
# USER_nPB0. Provenance: fpga/monolithic/nanosoc_mps3.xdc "USER_nPB" group.
# Sampled + debounced in hardware inside clcd_kvm (async pad), NOT in firmware.
##############################################################################
set_property PACKAGE_PIN AT32    [get_ports USER_nPB1]
set_property IOSTANDARD LVCMOS18 [get_ports USER_nPB1]

##############################################################################
# USER_nLED[7:0] (active-low) -- status LEDs
# Provenance: fpga/dfx/proof/proof.xdc (HW-verified); fpga/monolithic/nanosoc_mps3.xdc
##############################################################################
set_property PACKAGE_PIN AU32    [get_ports {USER_nLED[0]}]
set_property PACKAGE_PIN AU30    [get_ports {USER_nLED[1]}]
set_property PACKAGE_PIN AU31    [get_ports {USER_nLED[2]}]
set_property PACKAGE_PIN AR32    [get_ports {USER_nLED[3]}]
set_property PACKAGE_PIN AT33    [get_ports {USER_nLED[4]}]
set_property PACKAGE_PIN AW30    [get_ports {USER_nLED[5]}]
set_property PACKAGE_PIN AW31    [get_ports {USER_nLED[6]}]
set_property PACKAGE_PIN AR30    [get_ports {USER_nLED[7]}]
set_property IOSTANDARD LVCMOS18 [get_ports {USER_nLED[*]}]

##############################################################################
# USER_SW[7:0] -- DIP switches (board_gpio pads [15:8]; LEDs are pads [7:0]
# -- see shell_top.sv's board_gpio mapping note + fpga/shell/README.md)
# Provenance: fpga/monolithic/nanosoc_mps3.xdc "USER_nLED / USER_SW /
# USER_nPB" group <- fpga_pinmap.xdc (same HW-proven set as the LEDs above).
##############################################################################
set_property PACKAGE_PIN BA29    [get_ports {USER_SW[0]}]
set_property PACKAGE_PIN BB29    [get_ports {USER_SW[1]}]
set_property PACKAGE_PIN BA32    [get_ports {USER_SW[2]}]
set_property PACKAGE_PIN BA33    [get_ports {USER_SW[3]}]
set_property PACKAGE_PIN BA30    [get_ports {USER_SW[4]}]
set_property PACKAGE_PIN BB30    [get_ports {USER_SW[5]}]
set_property PACKAGE_PIN AY33    [get_ports {USER_SW[6]}]
set_property PACKAGE_PIN AY31    [get_ports {USER_SW[7]}]
set_property IOSTANDARD LVCMOS18 [get_ports {USER_SW[*]}]

##############################################################################
# LAN9220 Ethernet SMC interface (10/100, promiscuous per spec -- the
# harness's sole path to the network: management/reconfig/debug/DUT-uplink)
#
# Provenance: fpga/monolithic/nanosoc_mps3.xdc "LAN9220 Ethernet SMC
# interface" group <- fpga_pinmap.xdc lines ~37-39 (IOSTANDARD), ~319-321
# (PACKAGE_PIN). Ports connect to fpga/ethernet/lan9220_if/lan9220_emc_wrap.sv
# top-level pins of the same name -- see that module + its README for the
# device-specific vs shared-SMBF-bus signal split and the AXI EMC bank
# timing these pins are configured against.
##############################################################################
set_property PACKAGE_PIN AL24    [get_ports ETH_nCS]
set_property IOSTANDARD LVCMOS18 [get_ports ETH_nCS]
set_property PACKAGE_PIN AJ23    [get_ports ETH_nOE]
set_property IOSTANDARD LVCMOS18 [get_ports ETH_nOE]
set_property PACKAGE_PIN AK23    [get_ports ETH_INT]
set_property IOSTANDARD LVCMOS18 [get_ports ETH_INT]
set_property PULLTYPE PULLUP     [get_ports ETH_INT]
# PULLUP is CORRECT and load-bearing: the LAN9220 keeps its NATIVE open-drain,
# ACTIVE-LOW IRQ output (shell_linux.dts no longer sets smsc,irq-active-high /
# smsc,irq-push-pull), so the pin is high-Z when idle and must be pulled HIGH =
# deasserted; the chip pulls it LOW to assert.  shell_linux_top.sv feeds the
# axi_intc (ACTIVE-HIGH/LEVEL) with the INVERSE, `.eth_irq(~ETH_INT)`.
# All three must ship together -- any two alone livelock the kernel.
#
# HISTORY (do not re-litigate; both were measured on silicon):
#  * 2026-07-18: with the OLD DT (irq-active-high/push-pull) the chip's reset
#    default (IRQ disabled, open-drain => high-Z) + this PULLUP made In3 look
#    asserted at idle: INTC ISR = 0x00000008 with the driver idle.
#  * 2026-07-19: I briefly tried PULLDOWN to cure that.  It DID (ISR 0x8 -> 0x0)
#    but `ip link set eth0 up` still froze the box, because the real defect is
#    the POLARITY PROGRAMMING, not the idle pull: with INT_CFG=0x111
#    (IRQ_EN|IRQ_POL=1|push-pull) the part asserts the pin while INT_STS=0 and
#    INT_EN=0 (nothing pending) -- proven by a masked-IRQ devmem experiment.
#    Hence: native active-low + PULLUP + RTL inversion + no smsc,irq-* flags.
# ORIGINAL (superseded) note follows for context.  PROVEN ON SILICON
# 2026-07-18 (read-only devmem, no lockup triggered):
#   LAN9220 INT_CFG (0xC000_0054) = 0x00000000 at reset = IRQ_EN 0 + open-drain
#     => the ETH_INT pin is HIGH-Z until the driver programs INT_CFG.
#   With PULLUP the high-Z pin idles HIGH, and this INTC input is configured
#     ACTIVE-HIGH / LEVEL (axi_intc C_IRQ_ACTIVE 0x1, C_IRQ_IS_LEVEL 1; DT
#     `interrupts = <3 4>` = LEVEL_HIGH) => In3 asserted from power-on.
#   Measured live with eth0 DOWN and the driver idle:
#     INTC ISR (0x4120_0000) = 0x00000008  (bit3 = In3 ASSERTED, read twice)
#   `request_irq()` runs inside smsc911x_open() (NOT probe), so `ip link set
#     eth0 up` unmasked an unclearable level IRQ => kernel livelock.  That is
#     why probe looked clean but bringing the interface up hung the box.
# PULLDOWN makes the idle high-Z state read as DEASSERTED for an active-high
# level input, and is harmless once the driver drives the pin push-pull
# (smsc,irq-push-pull + smsc,irq-active-high already in shell_linux.dts, which
# are consistent with the INTC -- so the DT and RTL need NO change; the pad
# pull was the sole inconsistency).  Fail-safe: if the chip ever returns to
# high-Z, the line still reads deasserted.
# NOTE: the bare-metal harness firmware is POLL-mode and never touches
# INT_CFG, so Linux was the first ever consumer of this IRQ line -- which is
# why the bad pull survived until now.

##############################################################################
# SMBF_* static memory bus (shared: LAN9220 register/FIFO port + the USB
# debug FIFO's own decode, per fpga/monolithic/nanosoc_mps3.xdc's own
# provenance comment: "carries LAN9220 + USB debug FIFO on the same 16-bit
# mux"). This project only drives the LAN9220 side of it (USB_* pins are
# NOT constrained here -- out of scope, no USB debug FIFO consumer in this
# harness design).
#
# Provenance: fpga/monolithic/nanosoc_mps3.xdc "SMBF_* static memory bus"
# group <- fpga_pinmap.xdc lines ~10-39 (IOSTANDARD), ~639-665 (PACKAGE_PIN).
##############################################################################
set_property PACKAGE_PIN AK20    [get_ports {SMBF_ADDR[0]}]
set_property PACKAGE_PIN AK21    [get_ports {SMBF_ADDR[1]}]
set_property PACKAGE_PIN AJ18    [get_ports {SMBF_ADDR[2]}]
set_property PACKAGE_PIN AJ19    [get_ports {SMBF_ADDR[3]}]
set_property PACKAGE_PIN AH21    [get_ports {SMBF_ADDR[4]}]
set_property PACKAGE_PIN AJ21    [get_ports {SMBF_ADDR[5]}]
set_property PACKAGE_PIN AH19    [get_ports {SMBF_ADDR[6]}]
set_property IOSTANDARD LVCMOS18 [get_ports {SMBF_ADDR[*]}]

set_property PACKAGE_PIN AK22    [get_ports {SMBF_DATA[0]}]
set_property PACKAGE_PIN AL22    [get_ports {SMBF_DATA[1]}]
set_property PACKAGE_PIN AL19    [get_ports {SMBF_DATA[2]}]
set_property PACKAGE_PIN AL20    [get_ports {SMBF_DATA[3]}]
set_property PACKAGE_PIN AH18    [get_ports {SMBF_DATA[4]}]
set_property PACKAGE_PIN AM19    [get_ports {SMBF_DATA[5]}]
set_property PACKAGE_PIN AN19    [get_ports {SMBF_DATA[6]}]
set_property PACKAGE_PIN AP19    [get_ports {SMBF_DATA[7]}]
set_property PACKAGE_PIN AP20    [get_ports {SMBF_DATA[8]}]
set_property PACKAGE_PIN AM20    [get_ports {SMBF_DATA[9]}]
set_property PACKAGE_PIN AN21    [get_ports {SMBF_DATA[10]}]
set_property PACKAGE_PIN AP21    [get_ports {SMBF_DATA[11]}]
set_property PACKAGE_PIN AR22    [get_ports {SMBF_DATA[12]}]
set_property PACKAGE_PIN AM21    [get_ports {SMBF_DATA[13]}]
set_property PACKAGE_PIN AM22    [get_ports {SMBF_DATA[14]}]
set_property PACKAGE_PIN AN22    [get_ports {SMBF_DATA[15]}]
set_property IOSTANDARD LVCMOS18 [get_ports {SMBF_DATA[*]}]
# SMBF_DATA is a true bidirectional pad (inout at lan9220_emc_wrap.sv's
# top-level port) -- no per-bit SLEW/DRIVE override: the LAN9220's async
# register/FIFO bus runs far below RMII-class edge rates (spec §8's 50 MHz
# RMII is the fast interface in this design; this SMC bus's back-to-back
# transfer floor is ~80-100 ns per the datasheet-derived AC timing in
# fpga/ethernet/lan9220_if/README.md), so default SLEW/DRIVE is adequate.

set_property PACKAGE_PIN AJ20    [get_ports SMBF_FIFOSEL]
set_property IOSTANDARD LVCMOS18 [get_ports SMBF_FIFOSEL]
set_property PACKAGE_PIN AN23    [get_ports SMBF_nOE]
set_property IOSTANDARD LVCMOS18 [get_ports SMBF_nOE]
set_property PACKAGE_PIN AP23    [get_ports SMBF_nWE]
set_property IOSTANDARD LVCMOS18 [get_ports SMBF_nWE]
set_property PACKAGE_PIN AL23    [get_ports SMBF_nRST]
set_property IOSTANDARD LVCMOS18 [get_ports SMBF_nRST]

##############################################################################
# MicroBlaze bring-up/console UART -- the shell coordinator's OWN console
# (boot log, lwIP status, coordinator/xvc_server/swd_server diagnostics),
# distinct from the DUT's console (which is UART-over-Ethernet via the
# partition-pin AXI-Stream + fpga/shell/ip/uart_bridge/, relayed by
# MicroBlaze/lwIP -- docs/contracts/partition-pins.md "Console / trace").
#
# Channel choice: MPS3's 4 FPGA-side UART lanes (UART_TX_F/UART_RX_F[3:0])
# route through the on-board FT4232H quad-UART. Channel [0] is documented
# (nanosoc-multicore-system/ethernet-subsystem-ahb/.../ethernet_subsystem.xdc
# header comment, empirically verified 2026-04-22) as the MCC's OWN internal
# console -- reserved, do not use. Channel [2] is the pin group the PROVEN
# reference targets wire to the DUT's own cmsdk_apb_usrt
# (nanosoc_design_wrapper.v; eth_ss_design_wrapper.v) -- reserved here too,
# even though this harness's v0 DUT console goes over the partition-pin
# AXI-Stream instead, to avoid any future collision if a physical DUT-UART
# fallback (shell_bd.tcl's `uart_tx_f`/`uart_rx_f` port, marked "OPTIONAL
# raw-serial fallback") is ever wired to that same historical pin group.
# That leaves channel [1] for the MicroBlaze's own physical console below.
#
# Provenance: fpga/monolithic/nanosoc_mps3.xdc "UART_TX_F/UART_RX_F" group
# <- fpga_pinmap.xdc line 1 + ~114-121 (IOSTANDARD), ~738-744 (PACKAGE_PIN).
##############################################################################
# [LINUX-DEV-1] Console re-pinned to FT4232 lane 2 (AD28/AE28) -- the
# silicon-proven Linux console lane (host FT4232 if02; linux_soc
# board_pins.xdc + 2026-07-16 bring-up). Donor lane-1 pins AE30/AE31 are left
# UNUSED (not constrained) by this transplant. See file-header deviation note.
set_property PACKAGE_PIN AD28    [get_ports LINUX_UART_TXD]
set_property IOSTANDARD LVCMOS18 [get_ports LINUX_UART_TXD]
set_property PACKAGE_PIN AE28    [get_ports LINUX_UART_RXD]
set_property IOSTANDARD LVCMOS18 [get_ports LINUX_UART_RXD]
# RESOLVED (was a flag): shell_top.sv now binds these two physical ports to
# the shell BD's uart_tx_f/uart_rx_f pair -- which IS the MicroBlaze's own
# axi_uartlite_0 console (shell_bd.tcl SECTION 2), not a DUT UART. The DUT's
# console remains UART-over-Ethernet via the partition-pin AXI-Stream +
# uart_bridge; no physical DUT-UART fallback is wired in v0 (channel [2]'s
# pin group stays reserved, see the note above).

##############################################################################
# External QSPI flash (SST26VF064B) -- RP-OWNED XiP path.
# D16 RESOLVED (2026-07-15, QSPI boundary freeze): these pads are now driven by
# the RP's own QSPI controller across the partition boundary (partition-pins.md
# v0.2), formed into pads by shell_top.sv's IOBUFs (u_qspi_*_iobuf). All four
# data lanes are INOUT for quad XiP. The shell's OVLSTORE axi_quad_spi
# RELINQUISHED these pads (its SPI_0 external interface was removed in
# shell_bd.tcl). PULLUP stays on D0-D3 as a benign idle bias.
# Provenance: fpga/monolithic/nanosoc_mps3.xdc "Quad SPI boot/overlay flash"
# group <- fpga_pinmap.xdc lines ~546-557 (IOSTANDARD+PACKAGE_PIN),
# ~970-973 (PULLUP).
# TODO (DEFERRED — QSPI boundary freeze): I/O timing signoff of this
# source-synchronous XiP path is still owed. mps3_harness_timing.xdc currently
# carries a set_false_path on these ports — a placeholder that lets the boundary
# build, NOT signoff. Replace it with real set_input/output_delay vs the
# SST26VF064B datasheet before trusting XiP capture on silicon.
##############################################################################
set_property PACKAGE_PIN AU24    [get_ports QSPI_D0]
set_property IOSTANDARD LVCMOS33 [get_ports QSPI_D0]
set_property PULLUP true         [get_ports QSPI_D0]
set_property PACKAGE_PIN AV24    [get_ports QSPI_D1]
set_property IOSTANDARD LVCMOS33 [get_ports QSPI_D1]
set_property PULLUP true         [get_ports QSPI_D1]
set_property PACKAGE_PIN AV21    [get_ports QSPI_D2]
set_property IOSTANDARD LVCMOS33 [get_ports QSPI_D2]
set_property PULLUP true         [get_ports QSPI_D2]
set_property PACKAGE_PIN AV22    [get_ports QSPI_D3]
set_property IOSTANDARD LVCMOS33 [get_ports QSPI_D3]
set_property PULLUP true         [get_ports QSPI_D3]
set_property PACKAGE_PIN AT25    [get_ports QSPI_SCLK]
set_property IOSTANDARD LVCMOS33 [get_ports QSPI_SCLK]
set_property PACKAGE_PIN AT24    [get_ports QSPI_nCS]
set_property IOSTANDARD LVCMOS33 [get_ports QSPI_nCS]

##############################################################################
# CLCD (QVGA HX8347-D, 8-bit 8080 parallel) -- 14 non-touch pads, all LVCMOS18
# Provenance: fpga/monolithic/nanosoc_mps3.xdc:495-522 (verbatim). Touch pads
# CLCD_TSCL/TSDA/TINT/TNC (nanosoc_mps3.xdc:523-530) are OUT of v1 and
# deliberately NOT constrained (fpga/shell/ip/clcd/README.md "Touch ...
# unconstrained and reserved"). Bank/Vcco confirmation: §6 (plan §13-Q4).
##############################################################################
set_property PACKAGE_PIN AN17 [get_ports {CLCD_PD[10]}]
set_property PACKAGE_PIN AP16 [get_ports {CLCD_PD[11]}]
set_property PACKAGE_PIN AP18 [get_ports {CLCD_PD[12]}]
set_property PACKAGE_PIN AR18 [get_ports {CLCD_PD[13]}]
set_property PACKAGE_PIN AM16 [get_ports {CLCD_PD[14]}]
set_property PACKAGE_PIN AN16 [get_ports {CLCD_PD[15]}]
set_property PACKAGE_PIN AR17 [get_ports {CLCD_PD[16]}]
set_property PACKAGE_PIN AR16 [get_ports {CLCD_PD[17]}]
set_property IOSTANDARD LVCMOS18 [get_ports {CLCD_PD[17]}]
set_property IOSTANDARD LVCMOS18 [get_ports {CLCD_PD[16]}]
set_property IOSTANDARD LVCMOS18 [get_ports {CLCD_PD[15]}]
set_property IOSTANDARD LVCMOS18 [get_ports {CLCD_PD[14]}]
set_property IOSTANDARD LVCMOS18 [get_ports {CLCD_PD[13]}]
set_property IOSTANDARD LVCMOS18 [get_ports {CLCD_PD[12]}]
set_property IOSTANDARD LVCMOS18 [get_ports {CLCD_PD[11]}]
set_property IOSTANDARD LVCMOS18 [get_ports {CLCD_PD[10]}]
set_property PACKAGE_PIN AM15 [get_ports CLCD_RD]
set_property IOSTANDARD LVCMOS18 [get_ports CLCD_RD]
set_property PACKAGE_PIN AN14 [get_ports CLCD_RS]
set_property IOSTANDARD LVCMOS18 [get_ports CLCD_RS]
set_property PACKAGE_PIN AP15 [get_ports CLCD_CS]
set_property IOSTANDARD LVCMOS18 [get_ports CLCD_CS]
set_property PACKAGE_PIN AP14 [get_ports CLCD_WR_SCL]
set_property IOSTANDARD LVCMOS18 [get_ports CLCD_WR_SCL]
set_property PACKAGE_PIN AJ16 [get_ports CLCD_BL]
set_property IOSTANDARD LVCMOS18 [get_ports CLCD_BL]
set_property PACKAGE_PIN AK18 [get_ports CLCD_RST]
set_property IOSTANDARD LVCMOS18 [get_ports CLCD_RST]

##############################################################################
# Configuration / bitstream properties -- from fpga/monolithic/nanosoc_mps3.xdc
# (proven on this part) EXCEPT PERSIST:
#
#   BITSTREAM.CONFIG.PERSIST **NO** -- deliberate deviation from the
#   monolithic XDC's `Yes` (platform invariant C3, proven in the DFX proof /
#   fbc9025 and enforced in fpga/dfx/build_dfx.tcl): PERSIST is mutually
#   exclusive with ICAP use, and this shell carries AXI HWICAP; it also
#   trips DRC PRST-1 ("PERSIST requires CONFIG_MODE") on this part when no
#   CONFIG_MODE is set (hit live at place_design, 2026-07-06). The
#   monolithic design has no ICAP, so its `Yes` was legal there; the
#   harness/DFX platform must keep it NO. UltraScale expects NO/YES, not
#   FALSE/TRUE (fpga/dfx/proof/README.md fixup #1).
##############################################################################
set_property CONFIG_VOLTAGE 3.3 [current_design]
set_property CFGBVS VCCO [current_design]
set_property BITSTREAM.CONFIG.UNUSEDPIN Pullnone [current_design]
set_property BITSTREAM.CONFIG.PERSIST NO [current_design]
set_property BITSTREAM.STARTUP.MATCH_CYCLE Auto [current_design]
set_property BITSTREAM.GENERAL.COMPRESS True [current_design]

##############################################################################
# NOTE on what is intentionally NOT in this file:
#  - RP/DUT partition pins (dut_clk, jtag_* [DEV-10], phy_rmii_*, mdc/mdio_*,
#    uart_tx_tdata/uart_rx_tdata/swo, rm_id, dut_lockup, irq_out,
#    dut_gpio_*) -- these cross the static-shell <-> RP boundary as BD pins
#    inside one Vivado top design (docs/contracts/partition-pins.md "I4"),
#    NOT top-level board ports; they have no package pin of their own.
#  - The 20-pin CoreSight header (CS_TDI/TDO/TMS/TCK/nSRST/nTRST/nDET) --
#    no partition-pin route exists for it in the contract (documented A6
#    gap); shell_top.sv no longer declares those ports at all.
#  - TELEM I2C (INA228) -- MPS3's power monitors are MCC-owned; no FPGA pin
#    exists, the BD-side seam is tied off inside shell_top.sv.
##############################################################################
