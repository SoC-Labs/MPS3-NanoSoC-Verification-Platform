##############################################################################
# mps3_realphy_timing.xdc -- IMPLEMENTATION-ONLY source-synchronous timing for
# the REAL external LAN8720 RMII PHY. SHELL_REALPHY variant ONLY (NON-DEFAULT).
#
# Added EXPLICITLY (not globbed) and marked USED_IN_SYNTHESIS=false by
# build_shell.tcl, exactly like mps3_harness_timing.xdc -- the generated clock
# below only exists once clk_wiz_shell has elaborated.
#
#   *** TIMING MODEL -- BOARD SIGNOFF PENDING, AND THE MODEL IS TIGHT. ***
#   Read section "THE BUDGET" below before touching a number. Two of the four
#   terms are ENGINEERING ESTIMATES, one is an UNVERIFIED datasheet-class
#   figure, and together they consume ~92% of the 20 ns RMII period. This file
#   is parameterised so replacing an estimate with a measured or datasheet
#   value is a one-line edit; tests/realphy_timing/ re-does the arithmetic on
#   every CI run and goes red if an edit spends the remaining margin.
#
# These pads are SOURCE-SYNCHRONOUS and are DELIBERATELY NOT false-pathed
# (partition-pins.md / mps3_harness_timing.xdc header: "If a future interface is
# genuinely source-synchronous (e.g. a real RMII PHY pad), give it real I/O
# delays -- do not extend these false paths to it.").
#
# CLOCKING DECISION Option A: the FPGA sources REF_CLK (clk_wiz_shell/clk_out2),
# forwards it out ETH_RMII_REF_CLK via an ODDRE1, and the LAN8720 in REFCLK-in
# mode times all its RMII I/O to that forwarded clock. So the natural I/O
# reference is a generated clock ON the ETH_RMII_REF_CLK pad.
#
# Target: Vivado 2024.1, part xcku115-flvb1760-1-c
##############################################################################

##############################################################################
# LINK RATE (REALPHY_RATE, default 100) -- AND WHY IT CHANGES NOTHING HERE.
#
# RMII runs REF_CLK at 50 MHz at BOTH 10 and 100 Mb/s. The line rate is carried
# by DI-BIT REPETITION -- at 10 Mb/s the PHY holds each RXD di-bit and CRS_DV
# for TEN REF_CLK cycles and the MAC-side bridge ticks once every ten -- not by
# slowing the clock. So every set_input_delay / set_output_delay below is
# IDENTICAL at both rates and this file does not branch on the rate.
#
# What the rate DOES change is the analog edge rate of the inbound pads:
# RXD/CRS_DV have a 25 MHz fundamental at 100 Mb/s and a 2.5 MHz fundamental at
# 10 Mb/s. That is a SIGNAL-INTEGRITY lever on the shield path, not an STA one.
# Anyone reaching for "drop to 10 Mb/s to fix timing" is reaching for the wrong
# tool: it buys analog margin and zero setup margin.
#
# A multicycle waiver IS arithmetically available at 10 Mb/s (the di-bit is
# stable for 10 cycles, so set_multicycle_path -setup 10 -hold 9 on the RX
# capture would be sound IF the capture flop only mattered on the bridge's tick
# cycle). It is DELIBERATELY NOT TAKEN: the shell-side capture flop below
# samples EVERY cycle, so the waiver would need a tick-alignment argument that
# nobody has made, and the per-cycle constraint closes anyway once the RX pads
# are IOB-packed. Do not add the MCP without writing that argument down first.
#
# REALPHY_RATE therefore never reaches this file: nothing below depends on it.
# build_shell.tcl normalises it and records it in shell_variant.txt. (Until
# 2026-09-24 this file read it from the environment inside a Tcl `if` and
# printed it with `puts` -- neither is XDC, [Designutils 20-1307], so both were
# dropped anyway.)
##############################################################################

##############################################################################
# THE BUDGET -- the RX direction is the whole story.
#
# Option A makes the RX path a ROUND TRIP, and the round trip is what the
# previous revision of this file got wrong. The clock the PHY launches data
# against is the one THIS FPGA sent out, so the flight time is paid TWICE:
#
#     clk_out2 --(ODDRE1+OBUF, tool-modelled)--> ETH_RMII_REF_CLK pad
#              --(board, brd_clk_out)---------> LAN8720 REF_CLK input
#              --(lan_tco)--------------------> LAN8720 RXD/CRS_DV valid
#              --(board, brd_data_in)---------> FPGA pad
#              --(ILOGIC setup)---------------> captured by clk_out2
#
# The old file charged ONE `brd_trace_max` for the whole loop. It now charges
# brd_clk_out_max AND brd_data_in_max, which is what the topology actually does.
# (The ODDRE1->pad leg is NOT in these numbers: Vivado computes it itself as the
# generated clock's source latency. Do not add it by hand or it is double-counted.)
#
# Setup budget, with the numbers declared below:
#     period                          20.00 ns   (50 MHz RMII REF_CLK)
#   - brd_clk_out_max                  2.00      ESTIMATE
#   - lan_tco_max                     14.00      UNVERIFIED datasheet-class
#   - brd_data_in_max                  2.00      ESTIMATE
#   - brd_skew                         0.50      ESTIMATE
#   ------------------------------------------
#   = left for FPGA capture            1.50 ns
#
# 1.50 ns must cover the ILOGIC flop's setup plus clock uncertainty. It does --
# but ONLY because shell_top.sv now re-registers the RX pads IOB=TRUE. If those
# pads fed the reconfigurable partition directly (as they did before
# 2026-09-11) the pad-to-first-flop route would have to fit in 1.50 ns as well,
# across a KU115 and into a DFX pblock, which it will not.
#
#   *** THE SINGLE HIGHEST-VALUE ACTION ON THIS FILE IS FREE AND AT A DESK: ***
#   read lan_tco_max off the LAN8720A datasheet. It is 70% of the budget and
#   nobody in this repo has checked it. Every other term is small by comparison.
#   See docs/planning/REALPHY_RATE_DECISION.md.
#
# If lan_tco_max turns out to be too large to close, the fix is NOT a lower link
# rate (see above) -- it is CLOCKING OPTION B: strap the LAN8720 for REF_CLK-OUT
# so clock and data leave the PHY together and the flight times cancel to first
# order. That is a shell_top + XDC change, not a board change.
##############################################################################

# --- parameters (edit these; provenance is stated per line) ------------------
# LAN8720A RMII AC (REFCLK-in mode). UNVERIFIED against the datasheet -- these
# are the conservative RMII-class figures the first pass assumed. CHECK THEM.
set lan_tco_max   14.0   ;# REF_CLK -> RXD/CRS_DV valid, max (PHY clk-to-out)
set lan_tco_min    2.0   ;# REF_CLK -> RXD/CRS_DV valid, min
set lan_tsu        4.0   ;# PHY setup req on TXD/TX_EN vs REF_CLK
set lan_th         2.0   ;# PHY hold  req on TXD/TX_EN vs REF_CLK
# Board flight, SPLIT BY LEG -- ENGINEERING ESTIMATES, no MPS3 shield length
# data in this repo. brd_clk_out is the FORWARDED REF_CLK reaching the PHY;
# brd_data_in is the PHY's RX group coming back; brd_data_out is the TX group
# going to the PHY. RE-CHECK ALL THREE against the real shield routing.
set brd_clk_out_max   2.0
set brd_clk_out_min   0.2
set brd_data_in_max   2.0
set brd_data_in_min   0.2
set brd_data_out_max  2.0
set brd_data_out_min  0.2
set brd_skew          0.5
# What the FPGA side must still find inside the period after the terms above:
# ILOGIC flop setup + clock uncertainty. ENGINEERING RESERVE, not a datasheet
# number -- it exists so tests/realphy_timing/ has something to fail against
# when an estimate is raised. Held by the IOB=TRUE RX re-register in shell_top.
set fpga_capture_reserve 1.0
# RMII reference period. 50 MHz at BOTH link rates -- see the rate block above.
set rmii_period      20.0

set clk_shell [get_clocks -of_objects [get_pins -hierarchical -filter {NAME =~ "*/clk_wiz_shell/clk_out1"}]]
set clk_out2_pin [get_pins -hierarchical -filter {NAME =~ "*/clk_wiz_shell/clk_out2"}]
set clk_dut   [get_clocks -of_objects [get_pins -hierarchical -filter {NAME =~ "*/clk_wiz_dut/clk_out1"}]]

# The forwarded REF_CLK the PHY actually sees. Unconditional: the ODDRE1 ->
# pad path exists in every SHELL_REALPHY=1 build (it is shell_top logic, not
# RM logic). It used to sit in `if {[catch ...]}`, which XDC drops WITH its
# body ([Designutils 20-1307]) -- so no real-PHY build ever had RMII timing.
create_generated_clock -name eth_rmii_ref_out -source $clk_out2_pin \
    -divide_by 1 [get_ports ETH_RMII_REF_CLK]

##########################################################################
# INPUT: PHY -> FPGA (RXD, CRS_DV), referenced to the forwarded REF_CLK.
# ROUND TRIP -- the forwarded clock's board flight is charged here because
# it delays when the PHY launches; the ODDRE1->pad leg is NOT (the tool
# computes it as the generated clock's source latency).
#   -max = brd_clk_out_max + PHY tco_max + brd_data_in_max + skew
#   -min = brd_clk_out_min + PHY tco_min + brd_data_in_min - skew
##########################################################################
set rmii_in_max [expr {$brd_clk_out_max + $lan_tco_max + $brd_data_in_max + $brd_skew}]
set rmii_in_min [expr {$brd_clk_out_min + $lan_tco_min + $brd_data_in_min - $brd_skew}]

set_input_delay -clock eth_rmii_ref_out -max $rmii_in_max \
    [get_ports {ETH_RMII_RXD[*] ETH_RMII_CRS_DV}]
set_input_delay -clock eth_rmii_ref_out -min $rmii_in_min \
    [get_ports {ETH_RMII_RXD[*] ETH_RMII_CRS_DV}]

##########################################################################
# OUTPUT: FPGA -> PHY (TXD, TX_EN), referenced to the forwarded REF_CLK.
# DELIBERATELY CONSERVATIVE: the forwarded clock reaches the PHY
# brd_clk_out LATE, which RELAXES the PHY's setup requirement, and that
# relief is NOT claimed here. If TX ever fails to close, subtracting
# brd_clk_out_min from the -max term is the sound first move.
#   -max = PHY setup + brd_data_out_max + skew
#   -min = -(PHY hold) - brd_data_out_min - skew
##########################################################################
set_output_delay -clock eth_rmii_ref_out -max \
    [expr {$lan_tsu + $brd_data_out_max + $brd_skew}] \
    [get_ports {ETH_RMII_TXD[*] ETH_RMII_TX_EN}]
set_output_delay -clock eth_rmii_ref_out -min \
    [expr {-$lan_th - $brd_data_out_min - $brd_skew}] \
    [get_ports {ETH_RMII_TXD[*] ETH_RMII_TX_EN}]

##########################################################################
# MDIO management -- MDC <= 2.5 MHz, huge budget. Constrain (do not
# false-path) with generous management-rate numbers vs the same reference.
# mdc/mdio_o/mdio_oe are launched by the shell-side IOB FFs on clk_out2;
# mdio_i is sampled by the RP. Loose numbers: any real path closes easily.
# MDIO is rate-INDEPENDENT and is the one inbound signal that is readable
# at any link speed -- which is why the bench experiment in
# docs/planning/REALPHY_RATE_DECISION.md reads BMSR first.
##########################################################################
set_output_delay -clock eth_rmii_ref_out -max 10.0 [get_ports {ETH_MDC ETH_MDIO}]
set_output_delay -clock eth_rmii_ref_out -min -2.0 [get_ports {ETH_MDC ETH_MDIO}]
set_input_delay  -clock eth_rmii_ref_out -max 20.0 [get_ports ETH_MDIO]
set_input_delay  -clock eth_rmii_ref_out -min  0.0 [get_ports ETH_MDIO]

##########################################################################
# Clock relationships. eth_rmii_ref_out is derived from clk_out2, which is
# the SAME MMCM as clk_out1 (clk_shell) -> synchronous, leave related. The
# ONE async crossing in this variant is the MDIO group: eth_miim runs on the
# DUT MMCM (dut_clk) and the shell re-registers mdc/mdio_o/oe onto clk_out2
# (a benign dut_clk -> clk_out2 CDC at <= 2.5 MHz). Declare the forwarded
# ref async to the DUT clock so that crossing is not falsely timed (mirrors
# async_shell_dutclk / async_shell_qspisclk).
##########################################################################
set_clock_groups -name async_dutclk_rmiiref -asynchronous \
    -group $clk_dut \
    -group [get_clocks eth_rmii_ref_out]
