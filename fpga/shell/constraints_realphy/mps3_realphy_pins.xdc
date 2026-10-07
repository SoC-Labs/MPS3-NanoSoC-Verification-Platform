##############################################################################
# mps3_realphy_pins.xdc -- pin + IOSTANDARD constraints for the REAL external
# LAN8720 RMII PHY on the MPS3 shield. SHELL_REALPHY variant ONLY (NON-DEFAULT).
#
# *** DELIBERATELY NOT in fpga/shell/constraints/ (which build_shell.tcl GLOBS
#     into every build). This file is added EXPLICITLY, only when
#     SHELL_REALPHY=1, so the shipped/default shell never sees it. This avoids
#     the touch-XDC glob hazard (a constraints/ file applying unconditionally). ***
#
# Pin map: the MEASURED arm_mps3 map from
#   nanoSoC-refactor/ethernet-subsystem-ahb/fpga/targets/arm_mps3/
#   ethernet_subsystem.xdc  (the "J24 FULL RE-PIN 2026-07-19" block, incl. the
#   2026-08-06 RXD0<->RXD1 transpose proven on silicon). All RMII+MDIO signals
#   ride the single SH0 level-shifter bank (passive SN74TVC16222, GHz-class --
#   the "TXS0104E 40 Mbps" fear was retracted), except phy_rmii_txd[1] which
#   overflows to the SH1 bank (AW19 / J36.4).
#
# Board prerequisites (BOARD-GATED, cannot be set here). There are TWO, and
# they are both real:
#
#   B1. The module stays where these pins were MEASURED -- the J24 / Arduino
#       positions of the 2026-07-18 all-inputs probe (REF_CLK on SH0_IO7,
#       RX on SH0_IO6 / SH0_IO11 / SH0_IO12). Move the jumpers to the J28
#       pinout and every pin in this file is wrong. Re-pin OR re-jumper, not
#       both, and not neither.
#
#   B2. *** THE LAN8720 MUST BE STRAPPED REF_CLK-IN. *** This file drives
#       ETH_RMII_REF_CLK OUT on BB14 (Option A), and BB14 is the very net the
#       module was measured SOURCING a free-running clock on (2026-07-17).
#       A module still strapped REF_CLK-OUT puts two drivers on that net. This
#       is a contention risk, not a timing risk, and no amount of XDC fixes it.
#

#   *** THE "STRAP J29 IOREF TO 3V3" PREREQUISITE THAT USED TO BE WRITTEN HERE
#       IS WRONG AND HAS BEEN DELETED. Do not put it back. ***
#   J29 is the A0-A5 ANALOG header, not IOREF; Shield-0's IOREF link is J31.
#   And SH0REF does not set the pass-FET clamp: U35's A-side is clamped at
#   ~3V3 by its own self-biased GATE (R284 150k to 5VF, no enable pin), so
#   SH0REF only feeds the 10k pull-ups R285-R301 and the shield's advisory
#   IOREF pin. A wrong or unfitted J31 therefore CANNOT kill a driven signal,
#   and the pull-ups were measured reaching the FPGA through U35, which proves
#   SH0REF is present anyway. Provenance: rendered vector geometry of
#   HPI0309C_EXT.pdf p.13 (NOT PDF text order, which is what produced the
#   original error) + the 2026-07-16/17 on-silicon witness runs.
#   docs/planning/REALPHY_RATE_DECISION.md carries the full retraction.
#
# CLOCKING DECISION Option A: the FPGA SOURCES the 50 MHz REF_CLK and drives it
# OUT to the LAN8720 REFCLK-in strap (ETH_RMII_REF_CLK is an OUTPUT here). This
# differs from the arm_mps3 target, where the PHY sourced REF_CLK as an input.
# Same physical BB14 net, reversed direction.
#
# Target: Vivado 2024.1, part xcku115-flvb1760-1-c
##############################################################################

## 50 MHz RMII reference -- FPGA-SOURCED (Option A), driven OUT to the PHY.
##   BB14 = SH0_IO7 (measured). In the arm_mps3 target this was an input with
##   CLOCK_DEDICATED_ROUTE FALSE + create_clock; here it is an OUTPUT (the
##   ODDRE1 clock-forward in shell_top drives it), so no input create_clock.
set_property -dict {PACKAGE_PIN BB14 IOSTANDARD LVCMOS33 SLEW FAST DRIVE 12} [get_ports ETH_RMII_REF_CLK]

## RMII RX inputs (PHY -> FPGA).
##
## *** RXD0<->RXD1 TRANSPOSE LIVES HERE, AND ONLY HERE. ***
## The board's shield wiring transposes the RX di-bit lanes (on-silicon-proven,
## ethernet_subsystem.xdc 2026-08-06: the MII preamble arrived as 0xA/SFD 0xE
## instead of 0x5/0xD -- exactly a rxd0<->rxd1 swap; tb_rx_mac_dbg reproduces
## it). The correction is this pin mapping: the module's RXD0 line lands on
## SH0_IO6/BA15 and is bound to ETH_RMII_RXD[0]; RXD1 on SH0_IO11/BA13 -> [1].
## DO NOT re-apply this swap anywhere else -- NOT in rmii_to_mii (read-only,
## unchanged), NOT in the sim LAN8720 model/bench. A second swap re-creates the
## 0xA/no-lock failure. Sim has no shield/XDC, so its model drives rxd in the
## natural {rxd1,rxd0} order (no swap) and this single XDC layer is the only
## place HW differs -- sim and HW then agree with exactly one correction.
##
## IOB TRUE on the RX group too (added 2026-09-11), the input-side twin of the
## TX group's packing below. shell_top's r_phy_rxd/r_phy_crs_dv capture stage
## must land in the ILOGIC pad flop or the pad -> reconfigurable-partition route
## ends up inside the 50 MHz source-synchronous window, which has ~1.5 ns left
## in it (mps3_realphy_timing.xdc "THE BUDGET"). ILOGIC sites are static-only in
## a DFX design for the same reason OLOGIC sites are -- partition-pins.md's IOB
## packing note, HDPR-29, which only ever wrote down the output half.
set_property -dict {PACKAGE_PIN BA15 IOSTANDARD LVCMOS33 IOB TRUE} [get_ports {ETH_RMII_RXD[0]}]  ;# SH0_IO6  (module RXD0)
set_property -dict {PACKAGE_PIN BA13 IOSTANDARD LVCMOS33 IOB TRUE} [get_ports {ETH_RMII_RXD[1]}]  ;# SH0_IO11 (module RXD1)
set_property -dict {PACKAGE_PIN BB15 IOSTANDARD LVCMOS33 IOB TRUE} [get_ports ETH_RMII_CRS_DV]    ;# SH0_IO12 (measured)

## RMII TX outputs (FPGA -> PHY). SLEW FAST + DRIVE 16 for clean 50 MHz edges;
## IOB TRUE forces the shell-side re-register FFs (shell_top HDPR-29 stage) into
## the pad OLOGIC. txd[1] overflows to the SH1 bank (AW19 / J36.4).
set_property -dict {PACKAGE_PIN BA14 IOSTANDARD LVCMOS33 SLEW FAST DRIVE 16 IOB TRUE} [get_ports {ETH_RMII_TXD[0]}]  ;# SH0_IO10
set_property -dict {PACKAGE_PIN AW19 IOSTANDARD LVCMOS33 SLEW FAST DRIVE 16 IOB TRUE} [get_ports {ETH_RMII_TXD[1]}]  ;# SH1_IO3 / J36.4 (overflow)
set_property -dict {PACKAGE_PIN AY12 IOSTANDARD LVCMOS33 SLEW FAST DRIVE 16 IOB TRUE} [get_ports ETH_RMII_TX_EN]      ;# SH0_IO5

## MDIO management (FPGA drives MDC + bidir MDIO). Slow (<= 2.5 MHz).
set_property -dict {PACKAGE_PIN AU12 IOSTANDARD LVCMOS33 IOB TRUE} [get_ports ETH_MDC]   ;# SH0_IO13
set_property -dict {PACKAGE_PIN BA12 IOSTANDARD LVCMOS33}          [get_ports ETH_MDIO]  ;# SH0_IO8 (bidir, IOBUF)
