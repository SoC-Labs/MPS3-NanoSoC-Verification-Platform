##############################################################################
# constraints/qspi/mps3_qspi_pad_timing.xdc -- the SST26VF064B QSPI pad model.
#
# *** DORMANT: NO BUILD READS THIS FILE. ***
#
# Until 2026-09-24 this block lived in constraints/mps3_harness_timing.xdc
# wrapped in a Tcl `if {...} elseif {[catch {...}]} else {...}`. XDC does not
# support `if` (CRITICAL WARNING [Designutils 20-1307] "Command 'if' is not
# supported in the xdc constraint file"): Vivado DROPS the whole command, body
# and all. So none of it -- the generated clock, the output/input delays, the
# async group -- has ever applied to any shell, including every fielded one.
# The QSPI pads have always been unconstrained; this file only makes that
# explicit instead of silent.
#
# It is plain XDC now (no control flow), and it is NOT switched on by moving
# it, because it has never been through place-and-route with a qspi-carrying RM
# (its own caveat 4 below), and the hold side looks tight on paper: the
# generated clock's source latency (MMCM -> RM divider -> pad) delays the
# capture edge while -min is -(TDH + skew). Turning it on in a mint without a
# run that proves it would trade a silent gap for a failed mint.
#
# TO ENABLE (FLOW): read_xdc it in fpga/dfx/build_dfx.tcl beside
# mps3_harness_timing.xdc, run ONE DFX config with a qspi RM (rm_nanosoc),
# report_timing on qspi_sclk, fix what fails, then make it default. It needs
# the RM linked: in the flat build_shell.tcl run the RP is a stub and there is
# no MMCM -> QSPI_SCLK path to time.
##############################################################################

set clk_shell [get_clocks -of_objects [get_pins -hierarchical -filter {NAME =~ "*/clk_wiz_shell/clk_out1"}]]

##############################################################################
# External QSPI flash (SST26VF064B) -- RP-owned XiP path. SOURCE-SYNCHRONOUS.
#
#   *** FIRST-PASS TIMING MODEL -- BOARD SIGNOFF PENDING. ***
#   Datasheet numbers below are real and cited; the BOARD TRACE numbers are
#   engineering estimates (no MPS3 layout/length data was available). A timing
#   engineer must re-check the trace section against the real MPS3 flash
#   routing before this is called signed off. Everything is parameterised at
#   the top of the block so that is a one-line-per-value edit.
#
# Supersedes the previous `set_false_path` placeholder on these six pads
# (which was zero I/O timing). Closes the deferred-timing TODO in
# docs/contracts/partition-pins.md (QSPI crossing, v0.2) and the matching TODO
# in mps3_harness.xdc / shell_top.sv. This group is the documented NON-CDC
# exception in that contract: a matched-depth, source-synchronous passthrough
# (RP drives -> combinational DFX decoupler clamp -> shell IOBUFs -> pads;
# shell_top.sv u_qspi_*_iobuf). Because it is a passthrough and NOT a CDC, it
# must be timed, not false-pathed.
#
# ---------------------------------------------------------------------------
# TOPOLOGY (what the constraints model)
# ---------------------------------------------------------------------------
#   RP `qspi_flash_ahb` -> `top_ahb_qspi` -> `qspi_controller` + `qspi_clock_div`
#     QSPI_SCLK_i = HCLK/(2*CLK_DIV)   (CLK_DIV = APB reg13[4:0], byte 0x34)
#     Pin SCLK is QSPI_SCLK_i gated by QSPI_SCLK_e (held at CPOL=0 when idle).
#     SPI MODE 0, hardwired (QSPI_SPI_MODE tied 2'b00 in top_ahb_qspi.v):
#        master DRIVES QSPI_IO_o on the NEGEDGE of SCLK;
#        master SAMPLES QSPI_IO_i on the POSEDGE of SCLK.
#     The flash mirrors it (Mode 0): it samples SI on SCK rising and launches
#     SO on SCK falling. So both directions are half-SCLK-period, centre
#     aligned. The RTL has NO rx-sample-phase compensation -- that half period
#     is the entire budget, which is why `-clock_fall` on the input delay
#     below is load-bearing, not cosmetic.
#   HCLK here is the DUT clock (clk_wiz_dut/clk_out1, 50 MHz default, but
#   DRP-reconfigurable -- see shell_bd.tcl SECTION 1).
#
# ---------------------------------------------------------------------------
# THE ->8 RATIO
# ---------------------------------------------------------------------------
# partition-pins.md v0.2 makes SCLK <= HCLK/8 a hard requirement for XiP read
# closure, i.e. CLK_DIV >= 4. The generated clock below is therefore declared
# -divide_by 8 (the fastest legal SCLK). At the 50 MHz default that is
# 6.25 MHz / 160 ns -- ~17x slower than the part's 104 MHz limit, so these
# constraints are expected to pass with very large margin. That is the point:
# a passing report against REAL numbers is evidence; a false_path was not.
#
# NOTE -- no set_multicycle_path is needed or wanted here. The ->8 is already
# expressed in the generated clock, so every pad path is analysed edge-to-edge
# against qspi_sclk itself (160 ns period, 80 ns launch-to-capture half cycle).
# An MCP on top would double-count the ratio and silently inflate the budget.
#
# ---------------------------------------------------------------------------
# DATASHEET VALUES -- Microchip SST26VF064B/BA, DS20005119G, Table 8-1
# "AC Operating Characteristics", page 48. 104 MHz column (VDD 2.7-3.6V;
# these pads are LVCMOS33, mps3_harness.xdc). Cross-checked against the
# vendor's own behavioural model in-tree at
# ahb_qspi/verif/VIP/SST26VF064B.v (specify block, lines 106-163) -- every
# value agrees.
#   TDS  Data In Setup Time ............ 3 ns min   (flash setup requirement)
#   TDH  Data In Hold Time ............. 4 ns min   (flash hold requirement)
#   TV   Output Valid from SCK ......... 8 ns max @ 30 pF  (5 ns @ 10 pF)
#   TOH  Output Hold from SCK Change ... 0 ns min
#   TCES CE# Active Setup Time ......... 5 ns min
#   TCEH CE# Active Hold Time .......... 5 ns min   (TCHS/TCHH also 5 ns)
# We take TV = 8 ns, the 30 pF column -- the conservative one. The in-tree VIP
# uses Tv=5 (the 10 pF number); do not "correct" the 8 down to match the VIP.
# Read path is Fast-Read 0Bh + 8 dummy cycles (firmware/micropython
# port/xip_bringup.c, AHB_SPI_SETUP=0x800B). Per DS20005119G page 14 note 4,
# 0Bh runs at the full 104 MHz -- the 40 MHz limit is the 03h Read only, so
# the 104 MHz column is the right one. Dummy cycles shift WHICH bit is
# sampled, never the sample PHASE, so they do not enter this model.
#
# ---------------------------------------------------------------------------
# BOARD TRACE -- *** ESTIMATE, NOT MEASURED. SIGNOFF ITEM. ***
# ---------------------------------------------------------------------------
# No MPS3 flash trace-length data was available. Assumed: FR4 microstrip at
# ~170 ps/inch, flash within ~4 inches of the FPGA => ~0.2..0.8 ns one way,
# and SCLK/data NOT length-matched (so clock-vs-data skew is allowed to take
# the full +/-0.6 ns range rather than a matched-pair number). If the real
# layout matches SCLK to the data lanes, the skew term shrinks and these get
# looser still. Replace with measured lengths at signoff.
##############################################################################

# -- Datasheet (DS20005119G Table 8-1, 104 MHz column) ------------------------
set qspi_tds       3.0    ;# TDS  data-in setup  (flash requires)
set qspi_tdh       4.0    ;# TDH  data-in hold   (flash requires)
set qspi_tv        8.0    ;# TV   output valid from SCK, 30 pF
set qspi_toh       0.0    ;# TOH  output hold from SCK change
set qspi_tces      5.0    ;# TCES CE# active setup (= TCHS)
set qspi_tceh      5.0    ;# TCEH CE# active hold  (= TCHH)
# -- Board trace, ESTIMATED (see header) --------------------------------------
set qspi_trc_max   0.80   ;# longest  pad->flash one-way PCB delay, ns
set qspi_trc_min   0.20   ;# shortest pad->flash one-way PCB delay, ns
# Worst-case clock-vs-data trace skew either way (unmatched-trace assumption).
set qspi_skew      [expr {$qspi_trc_max - $qspi_trc_min}]

set qspi_dut_clk_pin [get_pins -hierarchical -filter {NAME =~ "*/clk_wiz_dut/clk_out1"}]
set qspi_sclk_port   [get_ports QSPI_SCLK]

# The reference clock is declared AT THE PAD, sourced from the DUT-clock MMCM:
# RM-agnostic (no RM-internal hierarchy is named, so this survives any RM that
# carries the qspi group) and it is the clock the flash actually sees. Source
# latency to the pad is therefore modelled by Vivado, and the PCB clock-trace
# delay is carried in the delay numbers below (which are all referenced to the
# FPGA pad, not to the flash). With a black-box or stub RP there is no path from
# the MMCM to QSPI_SCLK and Vivado reports the generated clock as having no
# logical path from its master -- expected there, a finding with an RM linked.
create_generated_clock -name qspi_sclk -source $qspi_dut_clk_pin \
    -divide_by 8 $qspi_sclk_port

##########################################################################
# OUTPUT: FPGA -> flash (data lanes + nCS), referenced to qspi_sclk.
# Master launches on SCLK negedge, flash captures on SCK posedge.
#   -max = TDS + skew   (setup: data trace long, clock trace short)
#   -min = -TDH - skew  (hold : data trace short, clock trace long)
# Vivado picks the negedge launch from the RM netlist itself; do not add
# -clock_fall here (that would move the REFERENCE edge, not the launch).
##########################################################################
set_output_delay -clock qspi_sclk -max [expr {$qspi_tds + $qspi_skew}] \
    [get_ports {QSPI_D0 QSPI_D1 QSPI_D2 QSPI_D3}]
set_output_delay -clock qspi_sclk -min [expr {-$qspi_tdh - $qspi_skew}] \
    [get_ports {QSPI_D0 QSPI_D1 QSPI_D2 QSPI_D3}]

# nCS is not a per-cycle signal but it IS specified relative to SCK
# (TCES/TCEH/TCHS/TCHH), so it is timed to the same reference clock.
# The cycle-level rules the FSM owns (TCPH CE# high time = 12 ns min,
# TCHZ = 12 ns) are protocol/sequencing, not pad setup/hold, and are not
# expressible here -- they are the RM FSM's responsibility.
set_output_delay -clock qspi_sclk -max [expr {$qspi_tces + $qspi_skew}] \
    [get_ports QSPI_nCS]
set_output_delay -clock qspi_sclk -min [expr {-$qspi_tceh - $qspi_skew}] \
    [get_ports QSPI_nCS]

##########################################################################
# INPUT: flash -> FPGA (read data), referenced to qspi_sclk.
# -clock_fall is LOAD-BEARING: the flash launches SO on the SCK FALLING
# edge (Mode 0) and the RP samples on the RISING edge. Without it Vivado
# would reference the rising edge and model a full-period path that does
# not exist, hiding the real half-period budget.
#   -max = clk_trace_max + TV  + data_trace_max
#   -min = clk_trace_min + TOH + data_trace_min
# (Both trace terms are present because the round trip is pad -> flash on
# SCLK, then flash -> pad on the data lane.)
##########################################################################
set_input_delay -clock qspi_sclk -clock_fall \
    -max [expr {$qspi_trc_max + $qspi_tv + $qspi_trc_max}] \
    [get_ports {QSPI_D0 QSPI_D1 QSPI_D2 QSPI_D3}]
set_input_delay -clock qspi_sclk -clock_fall \
    -min [expr {$qspi_trc_min + $qspi_toh + $qspi_trc_min}] \
    [get_ports {QSPI_D0 QSPI_D1 QSPI_D2 QSPI_D3}]

##########################################################################
# qspi_sclk is derived from the DUT-clock MMCM, so it inherits the
# shell-vs-DUT asynchrony declared at the top of this file. It needs its
# own group because the async_shell_dutclk group above was evaluated
# before this clock existed. This is not academic: the DFX decoupler's
# clamp-enable is a SHELL-domain signal feeding the (combinational) clamp
# in the qspi pad path, so shell->qspi_sclk paths do exist and would
# otherwise be timed against unrelated clocks.
##########################################################################
set_clock_groups -name async_shell_qspisclk -asynchronous \
    -group $clk_shell \
    -group [get_clocks qspi_sclk]

##############################################################################
# QSPI SIGNOFF CAVEATS -- read before calling this closed.
#
# 1. TRACE NUMBERS ARE ESTIMATES (see the header). Datasheet values are real
#    and cited; the PCB numbers are not measured. This is the main open item.
#
# 2. CLK_DIV=0 IS NOT FORBIDDEN BY THE RTL -- partition-pins.md v0.2 says
#    "the RTL already forbids CLK_DIV=0"; that claim is WRONG. qspi_clock_div.v
#    line 10 reads:
#        assign QSPI_SCLK_i = (QSPI_CLK_DIV==5'h00) ? HCLK : QSPI_SCLK_reg;
#    i.e. CLK_DIV=0 is an explicit, deliberate HCLK bypass (a combinational
#    mux putting raw HCLK on the pad), and the RDL documents it as such. There
#    is no RTL guard. CLK_DIV also RESETS TO 1 (HCLK/2), not to a ->8 value.
#    So the -divide_by 8 above is a constraint on FIRMWARE as much as on
#    silicon: firmware MUST program CLK_DIV >= 4 before any XiP fetch, or this
#    model is simply not describing the hardware. (Independent corroboration
#    that fast dividers do not work: ahb_qspi/docs/cocotb-silicon-findings.md
#    reports CLK_DIV=1 garbles RDID on real silicon.) Recommend the RM or
#    firmware pin this down; a set_case_analysis on CLK_DIV[4:0] would let a
#    future revision prove the bypass mux is unreachable, but it needs
#    RM-internal hierarchy and so cannot live in this (shell-owned) file.
#
# 3. THE RM MUST DEFINE ITS OWN INTERNAL SCLK CLOCK. The whole qspi_controller
#    is clocked by the internal QSPI_SCLK_i, not by HCLK. This file constrains
#    the PADS; it cannot reach RM-internal objects. If the RM does not declare
#    a generated clock on QSPI_SCLK_i, the controller's internal paths get
#    timed via the CLK_DIV=0 bypass mux at full HCLK rate (pessimistic but
#    safe) rather than at the real SCLK rate. Verify with report_clock_networks
#    on a linked implementation run.
#
# 4. NOT YET RUN THROUGH PLACE-AND-ROUTE. Syntax and port/pin targets are
#    checked; the actual slack is not. Confirm with report_timing on the
#    qspi_sclk clock after implementation.
##############################################################################
