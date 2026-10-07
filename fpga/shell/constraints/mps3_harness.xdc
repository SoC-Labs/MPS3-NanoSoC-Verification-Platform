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
# ETH_INT is open-drain-style / board-pulled on many SMSC LAN9220 designs;
# PULLUP here is a conservative default matching that expectation -- confirm
# against the MPS3 schematic (same "not yet verified" flag as the README's
# AC-timing section) before relying on the exact idle polarity in firmware.

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
# RE-PINNED 2026-09-14 FROM LANE 1 (AE30/AE31) TO LANE 2 (AD28/AE28).
# THE B7 DURABLE FIX. Full evidence: docs/planning/CONSOLE_AUDIT.md, commit
# 386fa27, and docs/TROUBLESHOOTING.md.
#
# The four FPGA UART lanes are NOT equivalent, and the reasoning that used to
# stand here ("[0] is the MCC's, [2] is historically the DUT's, that leaves
# [1]") never checked whether lane 1 reaches a host at all. It does not, by
# itself: lanes 0 and 1 reach the host ONLY through SN74CBTLV3253 source
# multiplexers whose selects the MCC drives from the `UARTMODE:` key in the SD
# card's config.txt, while lanes 2 and 3 are HARD-WIRED through a 74AVC4T245
# (Arm MPS3 TRM 100765_0000_04_en s2.18 Fig 2-25 p2-51; V2M-MPS3 schematics
# EOI-0309 rev C sheet 9 "USB UARTS"). This platform ships `UARTMODE: 0` =
# MCC:FPGA0, which does not name FPGA1 -- so the shell's console has been wired
# to a switched-off mux input since it was first pinned, and the symptom (a
# silent serial node) is indistinguishable from a dead processor. It cost this
# project a month of diagnosing firmware over JTAG telemetry. Firmware, BSP,
# baud and host tooling were all correct and are all unchanged by this edit.
#
# WHY LANE 2 AND NOT `UARTMODE: 1`. Setting UARTMODE: 1 also works and costs no
# rebuild, so it rides the next SD write as the interim -- but it leaves the
# console's reachability depending on a file that `sd_install` never rewrites.
# Lane 2 removes the multiplexer from the path entirely: it is reachable under
# EVERY UARTMODE. It is also the only lane with independent bench evidence in
# this repo (src/linux_soc/hw/gen_pins.py:161-184 pins the Linux transplant's
# console there and says in terms that no comparable evidence exists for lanes
# 0, 1 or 3).
#
# WHAT LANE 2 WAS RESERVED FOR, AND WHY THAT RELEASES. The reservation protected
# a physical DUT-UART fallback on shell_bd.tcl's `uart_tx_f`/`uart_rx_f` port
# ("OPTIONAL raw-serial fallback") against a future collision. The same comment
# recorded that the fallback was never wired, and it still is not: the DUT's
# console is UART-over-Ethernet across the partition-pin AXI-Stream, relayed on
# TCP 6930-6932. A reservation held for an unbuilt fallback loses to a console
# that does not work. Lane 3 (AD29/AD30) is also hard-wired and stays free, so
# a DUT-UART fallback still has a mux-free lane if one is ever built.
#
# THREE FILES MUST AGREE, and scripts/harness_gates/check_console_channel.py
# (make check / check-ci stage 2) fails until they do -- it derives the lane
# from this file, the mux mode from fpga/mps3_sd/templates/config.txt and the
# verdict from both, then requires docs/BOARD_BRINGUP.md's CONSOLE_CHANNEL
# marker to agree. With this re-pin the marker must read
#     <!-- CONSOLE_CHANNEL lane=2 uartmode=<whatever config.txt ships> reachable=yes -->
# and the prose must contain the words "FPGA UART lane 2" and the baud. Both of
# those files belong to the SD/bring-up lane, not this one.
#
# THIS IS A MINT-ONLY CHANGE: it moves pads, so it changes the static netlist,
# static_id and static_canon, and it re-keys every overlay. It cannot be
# deployed by rewriting an SD card. See docs/planning/MINT_2026_09.md.
#
# Channel map, for the reader: [0] AF27/AF28 = the MCC's OWN internal console,
# reserved, do not use (empirically verified 2026-04-22). [1] AE30/AE31 = muxed,
# what this console used to be on. [2] AD28/AE28 = hard-wired, THIS console.
# [3] AD29/AD30 = hard-wired, free.
#
# Provenance: fpga/monolithic/nanosoc_mps3.xdc "UART_TX_F/UART_RX_F" group
# (lane 2 = AD28 tx / AE28 rx at lines 116 and 126, IOSTANDARD LVCMOS18 at 121
# and 129) <- fpga_pinmap.xdc line 1 + ~114-121, ~738-744.
##############################################################################
set_property PACKAGE_PIN AD28    [get_ports MB_UART_TXD]
set_property IOSTANDARD LVCMOS18 [get_ports MB_UART_TXD]
set_property PACKAGE_PIN AE28    [get_ports MB_UART_RXD]
set_property IOSTANDARD LVCMOS18 [get_ports MB_UART_RXD]
# RESOLVED (was a flag): shell_top.sv now binds these two physical ports to
# the shell BD's uart_tx_f/uart_rx_f pair -- which IS the MicroBlaze's own
# axi_uartlite_0 console (shell_bd.tcl SECTION 2), not a DUT UART. The DUT's
# console remains UART-over-Ethernet via the partition-pin AXI-Stream +
# uart_bridge; no physical DUT-UART fallback is wired in v0 -- which is why
# channel [2]'s reservation could be released to this console (see the note
# above). Channel [3] (AD29/AD30) is also mux-free and remains unclaimed if a
# raw-serial DUT fallback is ever actually built.

##############################################################################
# External QSPI flash (SST26VF064B) -- RP-OWNED XiP path.
# D16 RESOLVED (2026-07-15, QSPI boundary freeze): these pads are now driven by
# the RP's own QSPI controller across the partition boundary (partition-pins.md
# v0.2), formed into pads by shell_top.sv's IOBUFs (u_qspi_*_iobuf). All four
# data lanes are INOUT for quad XiP. The shell has no SPI master on these pads
# (its axi_quad_spi_0 was removed with D13; usd_spi drives the USER microSD
# below, a different set of pins). PULLUP stays on D0-D3 as a benign idle bias.
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
# MCC-facing tie-offs (board-manager handover Lane D, D1). MPS3 TRM
# 100765_0000_04 Table 2-3 "Minimum RTL for correct operation of the MPS3
# board": SMBM_nWAIT Tie HIGH; IOFPGA_SYSWDT, WDOG_RREQ, CFG_DATAOUT Tie LOW.
# Until this block the shell left all four floating (UNUSEDPIN Pullnone below),
# and the MCC runs SMB cycles at every boot. Driven by constants in
# shell_top.sv. Pins + IOSTANDARD from Arm's own MPS3 pinmap
# (nanosoc_arch_tech/fpga/fpga/targets/arm_mps3/fpga_pinmap.xdc:207-208,
# 433-434, 734-735, 783-784), all LVCMOS33.
#   SMBM_nWAIT     AP28  -> 1  (FPGA never stretches an MCC SMB cycle)
#   WDOG_RREQ      AU19  -> 0  (no watchdog reset request to the MCC)
#   IOFPGA_SYSWDT  AU20  -> 0  (no RTCC/SYS_CFG request; D2 would drive it)
#   CFG_DATAOUT    AV18  -> 0  (SCC serial return idle; D2 would drive it)
# NOT tied here (also in Table 2-3, not in D1's scope): MMB_IDCLK, EMMC_CLK
# (tie LOW). QSPI_nCS/QSPI_SCLK are already driven (RP-owned, clamped).
##############################################################################
set_property PACKAGE_PIN AP28 [get_ports SMBM_nWAIT]
set_property IOSTANDARD LVCMOS33 [get_ports SMBM_nWAIT]
set_property PACKAGE_PIN AU19 [get_ports WDOG_RREQ]
set_property IOSTANDARD LVCMOS33 [get_ports WDOG_RREQ]
set_property PACKAGE_PIN AU20 [get_ports IOFPGA_SYSWDT]
set_property IOSTANDARD LVCMOS33 [get_ports IOFPGA_SYSWDT]
set_property PACKAGE_PIN AV18 [get_ports CFG_DATAOUT]
set_property IOSTANDARD LVCMOS33 [get_ports CFG_DATAOUT]

##############################################################################
# USER microSD slot (D13; usd_spi_0 @0x44A4_0000 in SPI mode) -- FPGA-wired per
# MPS3 TRM Sec 2.15. NOT the MCC config card (V2M_MPS3), which is not FPGA-wired.
# Pins + IOSTANDARD from Arm's MPS3 pinmap (nanosoc_arch_tech/fpga/fpga/targets/
# arm_mps3/fpga_pinmap.xdc:749-762; also fpga/monolithic/nanosoc_mps3.xdc:
# 275-286), all LVCMOS33.
#   USD_CLK AU15 (SCK, OBUFT)      USD_CMD AU16 (MOSI)     USD_NCD AT15 (CD)
#   USD_DAT[0] AV14 (MISO)  [1] AV13  [2] AT13 (unused, high-Z)  [3] AT12 (CS#)
# PULLUP on CMD, DAT[0..3] and NCD: usd_spi floats every pad when EN=0 or no
# card is present, so these pull-ups are what hold CS# deasserted, MOSI/MISO
# idle-high and card-detect at "no card" (HANDOVER_USD_OVERLAY_STORE.md §4.3,
# §7: whether HBI0309C also has external pull-ups is unverified -- these cover
# it either way). USD_CLK is left unpulled: a floating SCK with CS# high is
# ignored by the card.
##############################################################################
set_property PACKAGE_PIN AU15 [get_ports USD_CLK]
set_property IOSTANDARD LVCMOS33 [get_ports USD_CLK]
set_property PACKAGE_PIN AU16 [get_ports USD_CMD]
set_property IOSTANDARD LVCMOS33 [get_ports USD_CMD]
set_property PULLTYPE PULLUP [get_ports USD_CMD]
set_property PACKAGE_PIN AV14 [get_ports {USD_DAT[0]}]
set_property PACKAGE_PIN AV13 [get_ports {USD_DAT[1]}]
set_property PACKAGE_PIN AT13 [get_ports {USD_DAT[2]}]
set_property PACKAGE_PIN AT12 [get_ports {USD_DAT[3]}]
set_property IOSTANDARD LVCMOS33 [get_ports {USD_DAT[0]}]
set_property IOSTANDARD LVCMOS33 [get_ports {USD_DAT[1]}]
set_property IOSTANDARD LVCMOS33 [get_ports {USD_DAT[2]}]
set_property IOSTANDARD LVCMOS33 [get_ports {USD_DAT[3]}]
set_property PULLTYPE PULLUP [get_ports {USD_DAT[0]}]
set_property PULLTYPE PULLUP [get_ports {USD_DAT[1]}]
set_property PULLTYPE PULLUP [get_ports {USD_DAT[2]}]
set_property PULLTYPE PULLUP [get_ports {USD_DAT[3]}]
set_property PACKAGE_PIN AT15 [get_ports USD_NCD]
set_property IOSTANDARD LVCMOS33 [get_ports USD_NCD]
set_property PULLTYPE PULLUP [get_ports USD_NCD]

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
#  - RP/DUT partition pins (dut_clk, swd_*, phy_rmii_*, mdc/mdio_*,
#    uart_tx_tdata/uart_rx_tdata/swo, rm_id, dut_lockup, irq_out,
#    dut_gpio_*) -- these cross the static-shell <-> RP boundary as BD pins
#    inside one Vivado top design (docs/contracts/partition-pins.md "I4"),
#    NOT top-level board ports; they have no package pin of their own.
#  - The 20-pin CoreSight header (CS_TDI/TDO/TMS/TCK/nSRST/nTRST/nDET) --
#    no partition-pin route exists for it in the contract (documented A6
#    gap); shell_top.sv no longer declares those ports at all.
#  - TELEM I2C -- no FPGA-reachable power-monitor bus exists on MPS3. The
#    board has an AD7490 voltage ADC (voltages only) and NO INA-class
#    current/power monitor, so current/power cannot be measured at all. The
#    BD-side I2C seam is tied off inside shell_top.sv.
##############################################################################
