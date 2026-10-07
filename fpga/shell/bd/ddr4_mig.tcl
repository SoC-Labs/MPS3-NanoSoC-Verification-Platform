###-----------------------------------------------------------------------------
### fpga/shell/bd/ddr4_mig.tcl -- the MPS3 DDR4 SODIMM controller for the
### MicroBlaze V shell (SHELL_CPU=mbv). Sourced by cpu_mbv.tcl's `core` stage.
###
### FOLDED from src/linux_soc/hw/ddr4_ip.tcl (the July PoC that calibrated and
### booted Linux on this board, 2026-07-16) into the CPU seam, as a proc so its
### locals cannot collide with create_root_design's. The CONFIG VALUES ARE THAT
### FILE'S, UNCHANGED; its header carries the full memory-part derivation and
### is not repeated here. The facts a maintainer must not lose:
###
###   * PART MTA4ATF51264HZ-2G6 (SODIMMs) stands in for the fitted -2G3B1: the
###     same organisation field for field (1 rank, x16, BG width 1, row 16).
###     NEVER the x8 MTA8ATF51264HZ-2G1: BG width 2 / row 15 -- a wrong bank
###     and row map that builds, simulates and calibrates clean and then
###     corrupts on the bench.
###   * CasLatency PINNED 12. At tCK 1250 ps the -2G6 model auto-derives CL=11
###     (13.75 ns < the fitted die's 14.16 ns tAA) -- found as
###     init_calib_complete stuck 0 on silicon, 2026-07-15.
###   * tCK 1250 ps = 1600 MT/s, ui_clk 200 MHz. Legal in any bank group on -1.
###   * AxiNarrowBurst true: a 32-bit master on a 512-bit slave narrow-writes on
###     every store.
###   * Simulation_Mode BFM affects simulation only; the synthesised controller
###     is the real one.
###   * c0_ddr4_aresetn is an INPUT (active-low AXI shim reset). Left dangling it
###     is tied 0 = reset forever: calibration reports OK while every CPU store
###     is swallowed. cpu_mbv.tcl drives it; tools/shell_bd_guards.tcl checks it.
###   * Pins: fpga/shell/constraints/mbv/ddr4_pins.xdc (PACKAGE_PIN only; the
###     IP's generated XDC owns the electrical set). The external interface is
###     renamed c0_ddr4 in cpu_mbv.tcl so the wrapper's ports match that file.
###
### Every requested value is read back and ANY snap is an error: the July
### driver refused on a non-zero warning count, and a shell build has no
### business being more lenient.
###-----------------------------------------------------------------------------

proc soclabs_ddr4_mig_create {{name ddr4_0}} {
    set vlnv "xilinx.com:ip:ddr4:2.2"
    set req_part "xcku115-flvb1760-1-c"

    set cur_part [get_property PART [current_project]]
    if { $cur_part ne $req_part } {
        error "ddr4_mig.tcl: project part is '$cur_part'; this DDR4 configuration is valid for '$req_part' only (part-specific timing/geometry)"
    }
    if { [llength [get_ipdefs -quiet -all $vlnv]] == 0 } {
        error "ddr4_mig.tcl: '$vlnv' is not in this Vivado's catalogue (the MBV shell is built with 2026.1)"
    }
    if { [get_bd_cells -quiet $name] ne "" } {
        error "ddr4_mig.tcl: a BD cell '$name' already exists"
    }
    set cell [create_bd_cell -type ip -vlnv $vlnv $name]

    set cfg [list \
        CONFIG.C0.DDR4_MemoryType       {SODIMMs} \
        CONFIG.C0.DDR4_MemoryPart       {MTA4ATF51264HZ-2G6} \
        CONFIG.System_Clock             {Differential} \
        CONFIG.C0.DDR4_InputClockPeriod {10000} \
        CONFIG.C0.DDR4_TimePeriod       {1250} \
        CONFIG.Simulation_Mode          {BFM} \
        CONFIG.C0.DDR4_AxiSelection     {true} \
        CONFIG.C0.DDR4_AxiDataWidth     {512} \
        CONFIG.C0.DDR4_AxiIDWidth       {4} \
        CONFIG.C0.DDR4_AxiAddressWidth  {32} \
        CONFIG.C0.DDR4_AxiNarrowBurst   {true} \
        CONFIG.C0.DDR4_DataWidth        {64} \
        CONFIG.C0.DDR4_Ecc              {false} \
        CONFIG.C0.DDR4_DataMask         {DM_NO_DBI} \
        CONFIG.C0.DDR4_Slot             {Single} \
        CONFIG.C0.DDR4_MemoryVoltage    {1.2V} \
        CONFIG.C0.DDR4_isCustom         {false} \
        CONFIG.C0.DDR4_Specify_MandD    {false} \
        CONFIG.C0.DDR4_CasLatency       {12} \
    ]
    set_property -dict $cfg $cell

    set snaps {}
    foreach {k want} $cfg {
        set got [get_property $k $cell]
        if { $got ne $want } { lappend snaps "$k requested=$want read-back=$got" }
    }
    if { [llength $snaps] } {
        error "ddr4_mig.tcl: Vivado SNAPPED [llength $snaps] DDR4 parameter(s): [join $snaps {; }] -- refusing an unintended controller"
    }

    # Narrow-burst support on the AXI slave: the BFM honours WSTRB only with
    # it. Read-only (and already 1) at 2026.1 [BD 41-737]; settable before.
    set saxi [get_bd_intf_pins -quiet $cell/C0_DDR4_S_AXI]
    if { $saxi eq "" } { error "ddr4_mig.tcl: $name/C0_DDR4_S_AXI not found" }
    if { [get_property -quiet CONFIG.SUPPORTS_NARROW_BURST $saxi] ne "1" } {
        set_property CONFIG.SUPPORTS_NARROW_BURST {1} $saxi
    }
    if { [get_property -quiet CONFIG.SUPPORTS_NARROW_BURST $saxi] ne "1" } {
        error "ddr4_mig.tcl: $name/C0_DDR4_S_AXI SUPPORTS_NARROW_BURST did not read back 1"
    }

    set tck [get_property CONFIG.C0.DDR4_TimePeriod $cell]
    puts [format "INFO: ddr4_mig.tcl -- %s: %s (%s) tCK %s ps = %.0f MT/s, ui_clk %.1f MHz, AXI %s b, CL %s, 0 snapped" \
        $name [get_property CONFIG.C0.DDR4_MemoryPart $cell] \
        [get_property CONFIG.C0.DDR4_MemoryType $cell] $tck \
        [expr {2.0e6 / $tck}] [expr {1.0e6 / $tck / 4.0}] \
        [get_property CONFIG.C0.DDR4_AxiDataWidth $cell] \
        [get_property CONFIG.C0.DDR4_CasLatency $cell]]
    return $cell
}
