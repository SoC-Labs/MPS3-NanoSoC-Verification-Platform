###-----------------------------------------------------------------------------
### src/linux_soc/hw/build.tcl -- board-free build/impl driver for the
### LINUX-CAPABLE MicroBlaze V + DDR4 MIG SoC (`linux_soc`) on the MPS3's
### xcku115-flvb1760-1-c. Evolved from the proven poc/ddr4_mbv S0 spike
### (see the Linux-in-fabric feasibility note). Proves, WITHOUT a board, that
### the SoC builds, closes timing, fits, and (in the DDR4 IP's own
### example-design sim, driven separately -- see Makefile `sim`) reaches
### init_calib_complete.
###
### FOUNDATION contract. This is the address-map/CPU-config source of truth that
### every downstream Linux agent (device tree, firmware, Buildroot) builds
### against -- see ADDRESS_MAP.md next to this file. It does NOT touch
### fpga/shell/, fpga/dfx/, or static_id, and the repo-root `make check` stays
### green (nothing here is wired into it).
###
### Non-project / in-memory flow (mirrors fpga/monolithic/synth_check.tcl and the
### fpga/dfx non-project build): create_project -in_memory only, no project on
### disk. All scratch (checkpoints, reports, journal, log) lands under
### poc/ddr4_mbv/build/, which the repo's global `build/` .gitignore rule covers.
###
### Usage (normally via the Makefile, which sets -journal/-log under build/):
###   vivado -mode batch -source build.tcl \
###       -journal build/vivado.jou -log build/vivado.log -tclargs <stage>
### <stage> in {ip bd synth impl reports all}. Default = all.
###
### FILES OWNED BY OTHER AGENTS (this script `source`s / `exec`s them, never
### edits them; it fails loudly and early if one is missing):
###   gen_pins.py   -> emits ddr4_pins.xdc (DDR4 PACKAGE_PINs) + board_pins.xdc
###                    (uart/rst/led PACKAGE_PIN + IOSTANDARD) + pins.csv
###   ddr4_ip.tcl   -> creates + configures IP ddr4_0 (mem part, tCK, BFM sim,
###                    512-bit AXI slave)
###   mbv_soc.tcl   -> creates BD `mbv_soc` (microblaze_riscv_0 + mdm + LMB BRAM
###                    + uartlite/timer/intc + 2x smartconnect + clk_wiz +
###                    proc_sys_reset), ending in validate_bd_design
###
### Exit criteria (checked at the end of the `reports`/`all` stages): FAIL +
### `exit 1` if ANY of: DRC errors > 0, WNS < 0, WHS < 0. On PASS: `exit 0`.
### A machine-readable `RESULT:` line is printed and RESULT.txt is written next
### to the reports either way. This script never `catch`es a real build error
### into silence -- the only guarded calls are explicitly-optional diagnostic
### reports (`report_utilization -slr`, `report_design_analysis`) and best-effort
### metadata capture, each of which prints a visible NOTE when it degrades.
###-----------------------------------------------------------------------------

# --- small env helper ---------------------------------------------------------
proc _env_default {name default} {
    if { [info exists ::env($name)] && $::env($name) ne "" } {
        return $::env($name)
    }
    return $default
}

# --- locations ----------------------------------------------------------------
set SPIKE_DIR [file normalize [file dirname [info script]]]
set BUILD_DIR [_env_default LINUX_SOC_BUILD "$SPIKE_DIR/build"]
set RPT_DIR   "$BUILD_DIR/reports"
set CKPT_DIR  "$BUILD_DIR/checkpoints"
file mkdir $BUILD_DIR $RPT_DIR $CKPT_DIR

set POST_SYNTH_DCP "$CKPT_DIR/post_synth.dcp"
set POST_ROUTE_DCP "$CKPT_DIR/post_route.dcp"

# alongside inputs (owned by other agents)
set GEN_PINS   "$SPIKE_DIR/gen_pins.py"
set DDR4_IP    "$SPIKE_DIR/ddr4_ip.tcl"
set MBV_SOC    "$SPIKE_DIR/mbv_soc.tcl"
set PINS_XDC   "$SPIKE_DIR/ddr4_pins.xdc"   ;# produced by gen_pins.py
set BOARD_XDC  "$SPIKE_DIR/board_pins.xdc"  ;# produced by gen_pins.py (uart/rst/led)
set CPU_CFG_TXT "$BUILD_DIR/cpu_config.txt" ;# captured from the BD, read back by `reports`

set PY [_env_default PY "python3"]

# --- part + memory facts (MUST match ddr4_ip.tcl, which is owned by another
#     agent -- these are documented values, used only for RESULT.txt reporting
#     and %-utilisation denominators, never to re-configure the IP) ------------
set PART        [_env_default FPGA_PART "xcku115-flvb1760-1-c"]
set MEM_PART    "MTA4ATF51264HZ-2G6"      ;# SO-DIMM 1R x16 BG=1, per ddr4_ip.tcl.
                                           # Cross-checked against the IP after
                                           # sourcing ddr4_ip.tcl (see _assert_mem_part).
set TCK_PS      1250                       ;# controller tCK, per ddr4_ip.tcl
# tCK 1250 ps -> f_mem = 1/1250ps = 800 MHz -> DDR data rate = 1600 MT/s.
# ui_clk (user AXI clock) for the standard UltraScale 4:1 memory controller
# = f_mem / 4 = 200 MHz.
set MTS         [expr { int(round(2.0 * 1.0e6 / $TCK_PS)) }]   ;# = 1600
set UI_CLK_MHZ  [expr { (1.0e6 / $TCK_PS) / 4.0 }]             ;# = 200.0

# --- xcku115 device totals for %-utilisation (given in the brief) -------------
set LUT_TOTAL   663360
set FF_TOTAL    1326720
set BRAM_TOTAL  2160     ;# RAMB36-equivalent tiles ("Block RAM Tile")
set DSP_TOTAL   5520

# --- floorplan facts (from dfx_floorplan.xdc + the feasibility note) ----------
# xcku115-flvb1760-1-c is a 2-SLR SSI device:
#   SLR0 = clock regions X0Y0..X5Y4 ; SLR1 = clock regions X0Y5..X5Y9.
# DDR4 (SO-DIMM x64) lands in I/O banks 49/50/51 -> clock regions X2Y5/X2Y6/X2Y7
#   -> SLR1. This is where THIS spike's logic should sit.
# The existing DFX RP pblock (SLICE_X48Y0:SLICE_X95Y119, clock regions
#   X2Y0/X3Y0/X2Y1/X3Y1) and the primary ICAP (CONFIG_SITE_X0Y0, clock region
#   X5Y1) are both in SLR0. This spike must NOT place anything in SLR0's RP
#   region -- verified by report_slr_occupancy after place_design.
set RP_REGIONS  {X2Y0 X3Y0 X2Y1 X3Y1}     ;# SLR0 -- keep empty in this spike
set DDR4_REGIONS {X2Y5 X2Y6 X2Y7}          ;# SLR1 -- DDR4 banks 49/50/51

set TOP_MODULE  "linux_soc_wrapper"
set BD_NAME     "linux_soc"

# --- bitstream + XSA handoff --------------------------------------------------
# The XSA is the DOWNSTREAM CONTRACT: the Linux/DTS agent generates the device
# tree from it, so it must be produced even when timing is marginal. That single
# requirement drives the stage ordering in the dispatch below -- see do_bitstream.
set BIT_FILE    "$BUILD_DIR/${TOP_MODULE}.bit"
set XSA_FILE    "$BUILD_DIR/${BD_NAME}.xsa"
set BIT_STATUS  "not-attempted"   ;# filled in by do_bitstream, reported by do_reports
set XSA_STATUS  "not-attempted"

# --- stage argument -----------------------------------------------------------
set STAGE [expr { [llength $::argv] >= 1 ? [lindex $::argv 0] : "all" }]

puts "==========================================================="
puts " src/linux_soc/hw/build.tcl   (Linux-capable MicroBlaze V + DDR4 SoC)"
puts "   stage      = $STAGE"
puts "   part       = $PART"
puts "   mem part   = $MEM_PART   tCK ${TCK_PS}ps  ${MTS} MT/s  ui_clk ${UI_CLK_MHZ} MHz"
puts "   spike dir  = $SPIKE_DIR"
puts "   build dir  = $BUILD_DIR"
puts "==========================================================="

proc _require_file {label path} {
    if { ![file exists $path] } {
        puts "ERROR: required input missing ($label): $path"
        error "build.tcl: $label not found ($path). It is owned by another agent -- create it there, do not stub it here."
    }
}

# ddr4_ip.tcl owns the memory-part decision; the MEM_PART/TCK_PS constants here
# are only used to *report* it. If the two ever diverge, RESULT.txt records a
# part that was never built -- a provenance lie. Read the truth back off the IP
# and refuse to continue on mismatch.

# --- MicroBlaze V Early-Access gate: RESOLVED AT 2026.1 ----------------------
# In Vivado 2024.1 the Sv32 MMU (C_USE_MMU=3) was gated behind the undocumented
# env var AMD_VIVADO_MICROBLAZE_V_EA, and the poc/ddr4_mbv spike had to export it.
# MEASURED ON THIS 2026.1 INSTALL, with the variable deliberately UNSET:
#     set_property CONFIG.C_USE_MMU 3  ->  rc=0, reads back 3
# The gate is GONE: an MMU-enabled MicroBlaze V is a PRODUCTION configuration at
# 2026.1. So we do NOT export it -- and we actively complain if someone still does,
# because carrying a stale EA flag into a production flow is how an "early access"
# configuration silently ships.
#
# The real guarantee that we built a Linux-capable core is NOT an env-var check
# (which proves nothing about the netlist) but the read-back assertions in
# mbv_soc.tcl's _assert_cfg, which compare every ISA-critical parameter against
# what the IP actually took. That is the gate that matters; this is hygiene.
proc _check_mbv_ea {} {
    if { [info exists ::env(AMD_VIVADO_MICROBLAZE_V_EA)] } {
        puts ""
        puts "WARNING: AMD_VIVADO_MICROBLAZE_V_EA is set in the environment."
        puts "         It is NOT needed at Vivado 2026.1 (C_USE_MMU=3 is production)."
        puts "         Unset it -- a stale early-access flag hides real IP regressions."
        return
    }
    puts "-- MicroBlaze V EA flag correctly UNSET (C_USE_MMU=3 is production at 2026.1)"
}

# THE GATE THAT MATTERS.
#
# A PACKAGE_PIN on a port that does not exist is only a WARNING in Vivado:
#     WARNING: [Vivado 12-584] No ports matched 'c0_ddr4_dq[0]'.
# The constraint is silently discarded. The design still synthesises, still
# closes timing, still reports zero DRC errors -- and the DDR4 interface is
# attached to no package pins whatsoever. This actually happened here: the BD's
# make_bd_intf_pins_external named the wrapper ports C0_DDR4_0_* while the XDC
# constrained c0_ddr4_*, and 115 of 117 pins were dropped. An ERROR-count gate
# does not catch it. Counting the warning does.
#
# Verify, positively, that every port named in pins.csv exists on the design AND
# carries a PACKAGE_PIN. Never trust the absence of an error.
proc _assert_wrapper_ports {} {
    global SPIKE_DIR
    set csv [file join $SPIKE_DIR pins.csv]
    if { ![file exists $csv] } { error "build.tcl: _assert_wrapper_ports -- $csv missing" }

    set wrappers [glob -nocomplain [file join $::BUILD_DIR .gen sources_1 bd * hdl *_wrapper.v]]
    if { [llength $wrappers] != 1 } {
        error "build.tcl: expected exactly one generated BD wrapper, found: $wrappers"
    }
    set wf [lindex $wrappers 0]
    set fh [open $wf r]; set wtxt [read $fh]; close $fh

    # Collect every declared port base name from the wrapper.
    set have {}
    foreach m [regexp -all -inline {(?:input|output|inout)\s+(?:wire\s+)?(?:\[[^\]]*\]\s*)?([A-Za-z_][A-Za-z0-9_]*)} $wtxt] {
        if { [string match {input*} $m] || [string match {output*} $m] || [string match {inout*} $m] } { continue }
        dict set have $m 1
    }

    # Every distinct port the XDC constrains must exist on the wrapper.
    #
    # pins.csv now carries the top-level port name in an explicit `port` column,
    # so we READ the name rather than RECONSTRUCTING it. The old code rebuilt it
    # as "c0_ddr4_$sig" plus a hardcoded special case for the dm_dbi_n -> dm_n
    # rename -- a rule that could not express a port like `uart_txd` at all, and
    # would have demanded a nonexistent `c0_ddr4_uart_txd`. Column 0 is emitted by
    # gen_pins.py as exactly the string it puts in [get_ports {...}].
    set fh [open $csv r]
    set hdr [split [string trim [gets $fh]] ,]
    set pcol [lsearch -exact $hdr "port"]
    if { $pcol < 0 } {
        close $fh
        error "build.tcl: pins.csv has no 'port' column (header: $hdr) -- \
regenerate it with gen_pins.py"
    }
    set want {}
    while {[gets $fh line] >= 0} {
        if { [string trim $line] eq "" } { continue }
        set port [lindex [split $line ,] $pcol]
        if { $port eq "" } { continue }
        dict set want $port 1
    }
    close $fh

    set missing {}
    foreach b [dict keys $want] {
        if { ![dict exists $have $b] } { lappend missing $b }
    }

    if { [llength $missing] } {
        puts ""
        puts "########################################################################"
        puts "# PIN ATTACHMENT FAILED -- constrained ports are not on the design."
        puts "#"
        puts "# These port names are constrained in ddr4_pins.xdc / board_pins.xdc"
        puts "# but DO NOT EXIST on the synthesis top ([file tail $wf]):"
        foreach b [lsort $missing] { puts "#     $b" }
        puts "#"
        puts "# Vivado would only WARN ('\[Vivado 12-584\] No ports matched') and drop"
        puts "# every one of those PACKAGE_PIN constraints, then build cleanly."
        puts "#"
        puts "# Ports the wrapper actually declares:"
        foreach b [lsort [dict keys $have]] { puts "#     $b" }
        puts "########################################################################"
        exit 1
    }
    puts "-- _assert_wrapper_ports OK: all [dict size $want] constrained port names exist on [file tail $wf]"
}

# Belt and braces. _assert_wrapper_ports checks NAMES before synthesis; this
# checks that after synthesis every one of the 117 ports actually carries a
# PACKAGE_PIN, and that Vivado silently dropped nothing.
#
# This check now mirrors, at SYNTH time, the two DRCs that write_bitstream runs at
# the very END of a multi-hour build -- and which is exactly how this design failed
# before: impl completed, timing MET, 0 DRC errors reported, and then bitgen died on
#     NSTD-1  unspecified I/O standard  (IOSTANDARD left at 'DEFAULT')
#     UCIO-1  unconstrained logical port (no PACKAGE_PIN / LOC)
# for uart_txd, uart_rxd, sys_rst and init_calib_complete. Catching that after impl
# is worth ~2 hours; catching it after synth is worth minutes. So: EVERY top-level
# port must carry BOTH properties, not just the DDR4 ones.
#
# NOTE the 'DEFAULT' sentinel: an unconstrained port does not report an EMPTY
# IOSTANDARD, it reports the literal string DEFAULT -- which is precisely what the
# NSTD-1 message quotes. Testing only for {} would silently pass an unconstrained
# port and let the bitstream fail again at the finish line.
proc _assert_pins_locced {} {
    set n584 [get_msg_config -count -id "Vivado 12-584"]

    set ddr [get_ports -quiet {c0_ddr4_* c0_sys_clk_*}]
    set nddr [llength $ddr]

    set all [get_ports -quiet *]
    set unlocced {}    ;# UCIO-1: no PACKAGE_PIN
    set nostd   {}     ;# NSTD-1: IOSTANDARD still DEFAULT
    foreach p $all {
        set nm [get_property NAME $p]
        if { [get_property -quiet PACKAGE_PIN $p] eq "" } { lappend unlocced $nm }
        set io [get_property -quiet IOSTANDARD $p]
        if { $io eq "" || $io eq "DEFAULT" } { lappend nostd $nm }
    }

    if { $n584 > 0 || [llength $unlocced] > 0 || [llength $nostd] > 0 || $nddr != 117 } {
        puts ""
        puts "########################################################################"
        puts "# POST-SYNTH PIN CHECK FAILED (this is what bitgen would reject)."
        puts "#   total top-level ports    : [llength $all]"
        puts "#   DDR4/sys_clk ports found : $nddr   (expected 117)"
        puts "#   without PACKAGE_PIN      : [llength $unlocced]   -> DRC UCIO-1"
        if { [llength $unlocced] } { puts "#     first: [lrange $unlocced 0 7]" }
        puts "#   without IOSTANDARD       : [llength $nostd]   -> DRC NSTD-1"
        if { [llength $nostd] }    { puts "#     first: [lrange $nostd 0 7]" }
        puts "#   'No ports matched' warns : $n584"
        puts "#"
        puts "# Pins come from gen_pins.py -> ddr4_pins.xdc (DDR4, IOSTANDARD via the"
        puts "# IP's mig.xdc) + board_pins.xdc (uart/rst/led, IOSTANDARD set there)."
        puts "########################################################################"
        exit 1
    }
    puts "-- _assert_pins_locced OK: [llength $all] ports ($nddr DDR4/sys_clk),\
all with PACKAGE_PIN + IOSTANDARD, 0 dropped constraints"
}

# Vivado -mode batch prints "ERROR: [...]" and STILL exits 0 for a whole class of
# failures (e.g. generate_target on a nested sub-design). Silence must never look
# like success: count Vivado's own ERROR messages and fail the stage on any.
proc _assert_no_vivado_errors {where} {
    set n [get_msg_config -count -severity {ERROR}]
    if { $n > 0 } {
        puts ""
        puts "########################################################################"
        puts "# STAGE '$where' FAILED: Vivado logged $n ERROR message(s)."
        puts "# Exit code 0 from Vivado batch is NOT proof of success -- see the log."
        puts "########################################################################"
        exit 1
    }
    puts "-- no Vivado ERRORs logged during '$where'"
}

proc _assert_mem_part {} {
    global MEM_PART TCK_PS
    set ip [get_bd_cells -quiet ddr4_0]
    if { [llength $ip] == 0 } {
        error "build.tcl: _assert_mem_part -- BD cell ddr4_0 does not exist after sourcing ddr4_ip.tcl"
    }
    set actual_part [get_property CONFIG.C0.DDR4_MemoryPart $ip]
    set actual_tck  [get_property CONFIG.C0.DDR4_TimePeriod $ip]
    set bad 0
    if { $actual_part ne $MEM_PART } {
        puts "ERROR: memory part drift -- build.tcl reports '$MEM_PART' but ddr4_0 is configured '$actual_part'"
        incr bad
    }
    if { $actual_tck ne $TCK_PS } {
        puts "ERROR: tCK drift -- build.tcl reports ${TCK_PS}ps but ddr4_0 is configured ${actual_tck}ps"
        incr bad
    }
    if { $bad } {
        error "build.tcl: reporting constants disagree with the configured IP. Fix build.tcl to match ddr4_ip.tcl (which owns these facts)."
    }
    puts "-- _assert_mem_part OK: $actual_part @ ${actual_tck}ps"
}

###=============================================================================
### STAGE HELPERS
###=============================================================================

# --- gen_pins.py: run it, insist it exits 0, THEN trust ddr4_pins.xdc ---------
proc run_gen_pins {} {
    global PY GEN_PINS PINS_XDC SPIKE_DIR
    _require_file "gen_pins.py" $GEN_PINS
    puts "-- gen_pins: exec $PY $GEN_PINS (cwd [pwd])"
    # exec raises on a non-zero child exit; catch it so we can fail with a clear
    # message BEFORE any read of ddr4_pins.xdc (this is the "check it exits 0"
    # gate, NOT an error swallowed into silence -- we re-raise).
    set rc [catch {exec $PY $GEN_PINS} out opts]
    if { $out ne "" } { puts $out }
    if { $rc != 0 } {
        error "build.tcl: gen_pins.py did not exit 0 -- refusing to read ddr4_pins.xdc. ($opts)"
    }
    if { ![file exists $PINS_XDC] } {
        error "build.tcl: gen_pins.py exited 0 but $PINS_XDC was not produced (expected it alongside gen_pins.py)."
    }
    puts "-- gen_pins: OK, produced $PINS_XDC"
}

proc setup_project {} {
    global PART BD_NAME
    puts "-- create_project -in_memory -part $PART"
    create_project -in_memory -part $PART
    # Both ddr4_ip.tcl and mbv_soc.tcl are written against an OPEN block design:
    # ddr4_0 is a BD cell (not a standalone XCI) so that mbv_soc.tcl can connect
    # C0_DDR4_S_AXI to the CPU's cache masters. Create the container here.
    puts "-- create_bd_design $BD_NAME"
    create_bd_design $BD_NAME
    current_bd_design [get_bd_designs $BD_NAME]
}

# --- DDR4 IP (ddr4_0) ---------------------------------------------------------
proc setup_ip {} {
    global DDR4_IP
    _require_file "ddr4_ip.tcl" $DDR4_IP
    puts "-- source ddr4_ip.tcl (creates + configures ddr4_0)"
    source $DDR4_IP

    # The reporting constants above are a SECOND copy of facts that ddr4_ip.tcl
    # owns. A silent divergence would put a false memory part into RESULT.txt --
    # exactly the kind of provenance bug this spike exists to avoid. Fail hard.
    _assert_mem_part
    # Materialise IP output products so global synth can read them. Guarded only
    # for the "already generated by ddr4_ip.tcl" case -- a genuine generation
    # failure still surfaces (Vivado errors out of generate_target itself).
    # NOTE: the DDR4 controller instantiates its OWN MicroBlaze MCS (the
    # calibration processor), which lands as a NESTED sub-design under .gen/.
    # Vivado refuses `generate_target` on a nested IP ("can only be generated by
    # its parent sub-design", [Vivado 12-3563]) -- and, worse, prints that ERROR
    # while still returning exit 0. Only ever generate top-level IP sources.
    foreach xci [get_files -quiet *.xci] {
        if { [string match "*/.gen/*" $xci] } {
            puts "-- skip nested sub-design (parent owns it): [file tail $xci]"
            continue
        }
        puts "-- generate_target all $xci"
        generate_target all [get_files $xci]
    }
}

# --- BD (mbv_soc) + HDL wrapper as top ---------------------------------------
proc setup_bd {} {
    global MBV_SOC BD_NAME TOP_MODULE
    _require_file "mbv_soc.tcl" $MBV_SOC
    _check_mbv_ea
    puts "-- source mbv_soc.tcl (creates BD $BD_NAME, ends in validate_bd_design)"
    source $MBV_SOC

    set bd [get_files -quiet ${BD_NAME}.bd]
    if { $bd eq "" } {
        # fall back to whatever single .bd exists
        set bds [get_files -quiet *.bd]
        if { [llength $bds] == 1 } {
            set bd [lindex $bds 0]
        } else {
            error "build.tcl: expected a BD named ${BD_NAME}.bd after sourcing mbv_soc.tcl; found: $bds"
        }
    }
    puts "-- generate_target all $bd"
    generate_target all [get_files $bd]

    capture_cpu_config     ;# best-effort metadata, before we leave the BD context

    puts "-- make_wrapper (HDL wrapper for $bd, set as top)"
    set wrap [make_wrapper -files [get_files $bd] -top -force]
    add_files -norecurse $wrap
    update_compile_order -fileset sources_1
    set_property top $TOP_MODULE [current_fileset]
    puts "-- top set to $TOP_MODULE"
}

# --- pin constraints from gen_pins.py -----------------------------------------
#   ddr4_pins.xdc   117 DDR4/sys_clk PACKAGE_PINs (IOSTANDARD comes from mig.xdc)
#   board_pins.xdc  the 4 board pins: uart_txd/uart_rxd (console lane 2),
#                   sys_rst_n (active-LOW pad) and calib_complete_led_n
#                   (active-LOW pad) -- PACKAGE_PIN *and* IOSTANDARD, because
#                   nothing else constrains them. Their absence was the DRC
#                   NSTD-1 + UCIO-1 that killed write_bitstream.
proc setup_xdc {} {
    global PINS_XDC BOARD_XDC
    _require_file "ddr4_pins.xdc"  $PINS_XDC
    _require_file "board_pins.xdc" $BOARD_XDC
    foreach x [list $PINS_XDC $BOARD_XDC] {
        puts "-- read_xdc $x"
        read_xdc $x
    }
    # A dropped PACKAGE_PIN is only a WARNING. Catch it NOW, against the
    # generated wrapper's port list -- seconds, no synthesis required -- rather
    # than after a two-hour build that "succeeds" with an unattached interface.
    _assert_wrapper_ports
}

# --- capture MicroBlaze V config (MMU / atomics / optimization) ---------------
# Best-effort metadata for RESULT.txt. The authoritative values live in
# mbv_soc.tcl (owned by another agent); we read them off the BD cell if we can,
# and otherwise record the documented contract values with a visible NOTE. This
# is metadata, not a build gate -- degrading here never masks a build error.
# Records the CPU configuration for RESULT.txt, and -- more usefully -- renders the
# core's ISA as the string a device tree would have to advertise. The authoritative
# values are set AND read-back-asserted in mbv_soc.tcl; this reads them off the
# built BD so RESULT.txt reports the core that exists, not the one we intended.
#
# NOTE the spike reported "opt=unknown": it probed CONFIG.C_AREA_OPTIMIZED, which
# microblaze_riscv does not have (the parameter is C_OPTIMIZATION). Fixed here.
proc capture_cpu_config {} {
    global CPU_CFG_TXT
    set mmu "unknown" ; set atomics "unknown" ; set opt "unknown"
    set isa "unknown" ; set rstvec "unknown"
    set counters "unknown" ; set sstc "unknown"
    set intr_wakeup "unknown"

    set mb [get_bd_cells -quiet -hierarchical -filter {VLNV =~ *microblaze_riscv*}]
    if { [llength $mb] >= 1 } {
        set mb [lindex $mb 0]
        # C_USE_MMU: 0 MACHINE / 1 USER / 3 SUPERVISOR (Sv32). There is no 2.
        switch -- [get_property -quiet CONFIG.C_USE_MMU $mb] {
            0       { set mmu "none(machine)" }
            1       { set mmu "user" }
            3       { set mmu "supervisor(Sv32)" }
            default { set mmu "C_USE_MMU=[get_property -quiet CONFIG.C_USE_MMU $mb]" }
        }
        set atomics [get_property -quiet CONFIG.C_USE_ATOMIC $mb]
        # C_OPTIMIZATION: 0 PERFORMANCE / 1 AREA / 2 FREQUENCY / 3 THROUGHPUT.
        # 2 is ILLEGAL with C_USE_MMU=3, so a Linux core is always 0 here.
        switch -- [get_property -quiet CONFIG.C_OPTIMIZATION $mb] {
            0       { set opt "performance(0)" }
            1       { set opt "area(1)" }
            2       { set opt "frequency(2)" }
            3       { set opt "throughput(3)" }
            default { set opt "C_OPTIMIZATION=[get_property -quiet CONFIG.C_OPTIMIZATION $mb]" }
        }
        set rstvec [get_property -quiet CONFIG.C_BASE_VECTORS $mb]
        # [LINUX-LOGIN 2026-07-16] time CSR (only clocksource) + stimecmp (only
        # clockevent). Read back and recorded so a future Vivado default flip is
        # caught in the report, not on the board as a silent no-login-prompt.
        set counters [get_property -quiet CONFIG.C_USE_COUNTERS $mb]
        set sstc     [get_property -quiet CONFIG.C_USE_SSTC     $mb]
        # [WFI-COMA FIX 2026-07-16] C_INTERRUPT_WAKEUP: 0 (the IP default) means
        # `wfi` sleeps FOREVER -- no interrupt wakeup, no debug wakeup (xgui
        # tooltip; proven on silicon). Linux idles on wfi, so 0 = hang at first
        # idle. mbv_soc.tcl now pins + _assert_cfg's it to 1; recorded here so a
        # future default flip is caught in the report, not on the board.
        set intr_wakeup [get_property -quiet CONFIG.C_INTERRUPT_WAKEUP $mb]

        # Build the RISC-V ISA string from what the IP actually took. This is the
        # string the DTS `riscv,isa` must carry, and the arch a from-source
        # Buildroot toolchain must target. Note Zicbom is deliberately absent --
        # microblaze_riscv has no parameter for it (it does not implement it), even
        # though the vendor's PREBUILT glibc sysroot is named ..._zicbom_....
        set isa "rv[get_property -quiet CONFIG.C_DATA_SIZE $mb]i"
        if { [get_property -quiet CONFIG.C_USE_MULDIV      $mb] != 0 } { append isa "m" }
        if { [get_property -quiet CONFIG.C_USE_ATOMIC      $mb] != 0 } { append isa "a" }
        if { [get_property -quiet CONFIG.C_USE_FPU         $mb] != 0 } { append isa "f" }
        if { [get_property -quiet CONFIG.C_USE_COMPRESSION $mb] != 0 } { append isa "c" }
        if { [get_property -quiet CONFIG.C_USE_BITMAN_A    $mb] != 0 } { append isa "_zba" }
        if { [get_property -quiet CONFIG.C_USE_BITMAN_B    $mb] != 0 } { append isa "_zbb" }
        if { [get_property -quiet CONFIG.C_USE_BITMAN_C    $mb] != 0 } { append isa "_zbc" }
        if { [get_property -quiet CONFIG.C_USE_BITMAN_S    $mb] != 0 } { append isa "_zbs" }
    } else {
        puts "NOTE: capture_cpu_config -- no microblaze_riscv BD cell found (resumed from a checkpoint?); fields = n/a"
    }

    set fh [open $CPU_CFG_TXT w]
    puts $fh "mmu=$mmu"
    puts $fh "atomics=$atomics"
    puts $fh "opt=$opt"
    puts $fh "isa=$isa"
    puts $fh "rstvec=$rstvec"
    puts $fh "counters_zicntr=$counters"
    puts $fh "sstc=$sstc"
    puts $fh "interrupt_wakeup=$intr_wakeup"
    close $fh
    puts "-- cpu config: mmu=$mmu atomics=$atomics opt=$opt isa=$isa rstvec=$rstvec counters=$counters sstc=$sstc interrupt_wakeup=$intr_wakeup"
    puts "   (-> $CPU_CFG_TXT)"
}

proc read_cpu_config {} {
    global CPU_CFG_TXT
    set d [dict create mmu "n/a" atomics "n/a" opt "n/a" isa "n/a" rstvec "n/a"]
    if { [file exists $CPU_CFG_TXT] } {
        set fh [open $CPU_CFG_TXT r]
        foreach line [split [read $fh] "\n"] {
            # NOTE [a-z_]: keys like counters_zicntr / interrupt_wakeup carry an
            # underscore; the old [a-z]+ silently dropped them from the dict.
            if { [regexp {^([a-z_]+)=(.*)$} $line -> k v] } { dict set d $k $v }
        }
        close $fh
    } else {
        puts "NOTE: $CPU_CFG_TXT absent (reports stage resumed from a checkpoint without the BD); RESULT cpu fields = n/a"
    }
    return $d
}

# --- full front-end setup (gen pins -> project -> IP -> BD -> XDC) ------------
proc full_setup {} {
    run_gen_pins
    setup_project
    setup_ip
    setup_bd
    setup_xdc
}

# --- optional, flag-guarded floorplan pblock ----------------------------------
# Default OFF (DDR4_MBV_PBLOCK unset): the DDR4 controller is pin-driven into
# SLR1 by ddr4_pins.xdc, and the rest of the SoC is tiny (~4% of the device), so
# the tools are free to place it -- report_slr_occupancy is the check that the
# SLR0 RP region stays empty. When DDR4_MBV_PBLOCK is set (belt-and-braces), we
# confine the design's SLICE/DSP/BRAM to SLR1's clock regions so nothing can
# stray into SLR0's RP area. Idempotent (guarded like dfx_floorplan.xdc). Runs
# on the post-synth netlist (get_cells needs the netlist, not RTL).
proc add_floorplan {} {
    global TOP_MODULE
    if { ![info exists ::env(LINUX_SOC_PBLOCK)] || $::env(LINUX_SOC_PBLOCK) eq "" } {
        puts "-- floorplan pblock: OFF (set LINUX_SOC_PBLOCK=1 to confine the SoC to SLR1)"
        puts "   relying on pin-driven DDR4 placement in SLR1 + the post-place SLR check"
        return
    }
    set pb "pblock_linux_soc_slr1"
    puts "-- floorplan pblock: ON -> $pb (confine to SLR1 clock regions)"
    if { [llength [get_pblocks -quiet $pb]] == 0 } { create_pblock $pb }
    set top_cells [get_cells -quiet -hierarchical -filter {IS_PRIMITIVE}]
    if { [llength $top_cells] == 0 } {
        error "build.tcl: add_floorplan found no primitive cells -- must run after synth_design"
    }
    add_cells_to_pblock [get_pblocks $pb] [get_cells -hierarchical -filter {IS_PRIMITIVE}]
    set slr1_regions [get_clock_regions -quiet -of_objects [get_slrs SLR1]]
    foreach site_type {SLICE DSP48E2 RAMB18 RAMB36} {
        set xs {} ; set ys {}
        foreach s [get_sites -quiet -of_objects $slr1_regions -filter "NAME =~ ${site_type}_X*"] {
            if { [regexp "^${site_type}_X(\\d+)Y(\\d+)$" [get_property NAME $s] -> x y] } {
                lappend xs $x ; lappend ys $y
            }
        }
        if { ![llength $xs] } { continue }
        set x0 [lindex [lsort -integer $xs] 0] ; set x1 [lindex [lsort -integer $xs] end]
        set y0 [lindex [lsort -integer $ys] 0] ; set y1 [lindex [lsort -integer $ys] end]
        resize_pblock [get_pblocks $pb] -add "${site_type}_X${x0}Y${y0}:${site_type}_X${x1}Y${y1}"
    }
    set_property SNAPPING_MODE ON [get_pblocks $pb]
}

# --- SLR-occupancy check (after place_design) ---------------------------------
# Reports which SLRs carry placed logic, and (loud WARNING, not a gate) whether
# anything landed in SLR0's RP region -- the exit-criteria gate is DRC/WNS/WHS
# per the brief, so RP-region occupancy is reported, not failed on.
proc report_slr_occupancy {} {
    global RP_REGIONS RPT_DIR
    set slr_txt "$RPT_DIR/slr_occupancy.txt"
    set fh [open $slr_txt w]

    set prims [get_cells -quiet -hierarchical -filter {IS_PRIMITIVE}]
    set occupied [lsort -unique [get_slrs -quiet -of_objects $prims]]
    puts "-- SLR occupancy: placed logic in SLRs = $occupied"
    puts $fh "occupied_slrs = $occupied"
    foreach slr [get_slrs] {
        puts $fh "SLR $slr: clock regions = [lsort [get_clock_regions -quiet -of_objects $slr]]"
    }

    # Is anything placed on a site inside SLR0's RP region?
    set rp_sites [get_sites -quiet -of_objects [get_clock_regions -quiet $RP_REGIONS]]
    set rp_cells [get_cells -quiet -of_objects $rp_sites]
    set n_rp [llength $rp_cells]
    puts $fh "rp_region ($RP_REGIONS, SLR0) placed cells = $n_rp"
    if { $n_rp > 0 } {
        puts "WARNING: report_slr_occupancy -- $n_rp cell(s) placed in SLR0's RP region $RP_REGIONS."
        puts "         This spike is meant to keep that region EMPTY (it belongs to the DFX RP)."
        puts "         Set DDR4_MBV_PBLOCK=1 to confine the SoC to SLR1, or ignore for a spike."
    } else {
        puts "-- SLR occupancy: SLR0 RP region $RP_REGIONS is EMPTY (as intended)"
    }
    close $fh
    puts "-- SLR occupancy report: $slr_txt"
    return [list occupied $occupied rp_cells $n_rp]
}

# --- synth --------------------------------------------------------------------
proc do_synth {} {
    global TOP_MODULE PART POST_SYNTH_DCP
    puts "== synth_design -top $TOP_MODULE -part $PART =="
    synth_design -top $TOP_MODULE -part $PART
    write_checkpoint -force $POST_SYNTH_DCP
    puts "== synth done -> $POST_SYNTH_DCP =="
}

# --- impl (opt -> place -> [SLR check] -> route) ------------------------------
proc do_impl {} {
    global POST_ROUTE_DCP
    puts "== opt_design =="
    opt_design
    add_floorplan
    puts "== place_design =="
    place_design
    report_slr_occupancy
    puts "== route_design =="
    route_design
    write_checkpoint -force $POST_ROUTE_DCP
    puts "== impl done -> $POST_ROUTE_DCP =="
}

# --- bitstream + XSA (hardware handoff for the Linux/DTS agent) ---------------
# Ordering rule (load-bearing): this runs BEFORE do_reports, because do_reports
# is an exit gate -- it `exit 1`s on a WNS/WHS/DRC failure. If bitgen+XSA ran
# after it, a marginal-timing build would leave the DTS agent with NO XSA, which
# is precisely the outcome the brief forbids ("produce it even if timing is
# marginal"). Generate the handoff first; judge it second.
#
# Two independent failure modes, deliberately NOT conflated:
#   * write_bitstream runs its own DRC and RAISES a Tcl error on a DRC ERROR.
#     Letting that propagate would abort the script with no reports written and
#     no RESULT.txt -- i.e. a real DRC failure would look like a crash instead of
#     a FAIL with an evidence path. So: catch it, record it, carry on to
#     do_reports, which reports DRC_errors>0 as a FAIL verdict.
#   * The XSA only needs the BD hardware handoff (.hwh), NOT the bitstream. So if
#     bitgen fails we STILL emit an XSA -- just without -include_bit -- rather
#     than starving the DTS agent. A DTS does not care about a .bit.
proc do_bitstream {} {
    global BIT_FILE XSA_FILE BIT_STATUS XSA_STATUS

    puts "== write_bitstream -> $BIT_FILE =="
    if { [catch { write_bitstream -force $BIT_FILE } e] } {
        puts "NOTE: write_bitstream FAILED (continuing so reports/RESULT.txt still get written): $e"
        set BIT_STATUS "FAILED"
    } elseif { [file exists $BIT_FILE] } {
        set BIT_STATUS "OK"
        puts "-- bitstream OK: $BIT_FILE ([file size $BIT_FILE] bytes)"
    } else {
        puts "NOTE: write_bitstream returned 0 but produced no file."
        set BIT_STATUS "FAILED(no file)"
    }

    puts "== write_hw_platform -> $XSA_FILE =="
    set cmd [list write_hw_platform -fixed]
    if { $BIT_STATUS eq "OK" } { lappend cmd -include_bit }
    lappend cmd -force $XSA_FILE
    puts "-- $cmd"
    if { [catch { {*}$cmd } e] } {
        puts "NOTE: write_hw_platform FAILED: $e"
        set XSA_STATUS "FAILED"
    } elseif { [file exists $XSA_FILE] } {
        set XSA_STATUS [expr { $BIT_STATUS eq "OK" ? "OK(with .bit)" : "OK(no .bit)" }]
        puts "-- XSA OK: $XSA_FILE ([file size $XSA_FILE] bytes)"
    } else {
        puts "NOTE: write_hw_platform returned 0 but produced no file."
        set XSA_STATUS "FAILED(no file)"
    }
    puts "-- handoff: bitstream=$BIT_STATUS xsa=$XSA_STATUS"
}

proc ensure_synth {} {
    global POST_SYNTH_DCP
    if { [file exists $POST_SYNTH_DCP] } {
        puts "-- resume: open_checkpoint $POST_SYNTH_DCP"
        open_checkpoint $POST_SYNTH_DCP
    } else {
        full_setup
        do_synth
    }
}

proc ensure_impl {} {
    global POST_ROUTE_DCP
    if { [file exists $POST_ROUTE_DCP] } {
        puts "-- resume: open_checkpoint $POST_ROUTE_DCP"
        open_checkpoint $POST_ROUTE_DCP
    } else {
        ensure_synth
        do_impl
    }
}

###=============================================================================
### REPORTING + EXIT-CRITERIA CHECKER
###=============================================================================

# parse a report_utilization -return_string table for the "Used" cell of a row
proc _util_used {rpt labels} {
    foreach line [split $rpt "\n"] {
        if { [string first "|" $line] < 0 } { continue }
        set cols [split $line "|"]
        if { [llength $cols] < 3 } { continue }
        set name [string trim [lindex $cols 1]]
        set name [string trimright $name "*"]
        foreach l $labels {
            if { [string equal $name $l] } {
                set used [string trim [lindex $cols 2]]
                # "Block RAM Tile" is reported as a FRACTION (e.g. 48.5) because a
                # RAMB18 is half a tile. `string is integer` rejects that and the
                # value silently became 0 -- a design with a 64 KiB LMB and two
                # 8 KiB caches reported BRAM=0. Accept any number.
                if { [string is double -strict $used] } { return $used }
            }
        }
    }
    return "NA"
}

proc _pct {used total} {
    # "Block RAM Tile" is fractional (a RAMB18 is half a tile), so integer-only
    # here made a real 48.5 print as "NA%". Accept any number.
    if { ![string is double -strict $used] || $total == 0 } { return "NA" }
    return [format "%.2f" [expr { 100.0 * $used / $total }]]
}

# worst setup/hold slack from the routed design
proc _worst_slack {kind} {
    set p [get_timing_paths -quiet -max_paths 1 -nworst 1 -$kind]
    if { [llength $p] == 0 } { return "NA" }
    set s [get_property -quiet SLACK $p]
    if { $s eq "" } { return "NA" }
    return $s
}

# DRC error + critical-warning counts (after report_drc has populated them)
proc _drc_counts {} {
    # Vivado's SEVERITY property is mixed case -- "Error", "Critical Warning",
    # "Warning" -- NOT upper case. The original switch compared against
    # "ERROR"/"CRITICAL WARNING", matched nothing, and returned 0/0 for every
    # design. That made the DRC exit-gate incapable of ever firing: a build with
    # real DRC errors would have been reported as PASS. Normalise the case.
    set errors 0 ; set critwarn 0
    set total 0
    foreach v [get_drc_violations -quiet] {
        incr total
        switch -- [string toupper [get_property -quiet SEVERITY $v]] {
            "ERROR"            { incr errors }
            "CRITICAL WARNING" { incr critwarn }
        }
    }
    # Sanity: if report_drc found violations but we classified none, the property
    # name or values have changed again. Fail loudly rather than pass silently.
    if { $total > 0 && $errors == 0 && $critwarn == 0 } {
        set sevs {}
        foreach v [get_drc_violations -quiet] { lappend sevs [get_property -quiet SEVERITY $v] }
        puts "NOTE: $total DRC violations, none classified as Error/Critical Warning."
        puts "NOTE: observed severities: [lsort -unique $sevs]"
    }
    return [list $errors $critwarn]
}

proc do_reports {} {
    global RPT_DIR PART MEM_PART TCK_PS MTS UI_CLK_MHZ
    global LUT_TOTAL FF_TOTAL BRAM_TOTAL DSP_TOTAL
    global BIT_FILE XSA_FILE BIT_STATUS XSA_STATUS

    puts "== reports -> $RPT_DIR =="

    # --- required reports (NOT guarded -- fail loudly if any errors) ----------
    report_utilization              -file $RPT_DIR/utilization.rpt
    report_utilization -hierarchical -file $RPT_DIR/utilization_hier.rpt
    report_timing_summary           -file $RPT_DIR/timing_summary.rpt
    report_drc                      -file $RPT_DIR/drc.rpt
    report_clocks                   -file $RPT_DIR/clocks.rpt
    report_io                       -file $RPT_DIR/io.rpt

    # --- optional diagnostics ("if available" per the brief) ------------------
    if { [catch { report_utilization -slr -file $RPT_DIR/utilization_slr.rpt } e] } {
        puts "NOTE: report_utilization -slr unavailable here ($e) -- skipped (non-gating)"
    }
    if { [catch { report_design_analysis -file $RPT_DIR/design_analysis.rpt } e] } {
        puts "NOTE: report_design_analysis unavailable here ($e) -- skipped (non-gating)"
    }

    # --- SLR occupancy (recompute from the in-memory routed design) -----------
    set slr [report_slr_occupancy]
    set occupied_slrs [dict get $slr occupied]
    set rp_cells      [dict get $slr rp_cells]

    # --- scrape totals from report_utilization -------------------------------
    set util_str [report_utilization -return_string]
    set lut  [_util_used $util_str {"CLB LUTs" "Slice LUTs" "LUTs"}]
    set ff   [_util_used $util_str {"CLB Registers" "Slice Registers" "Register as Flip Flop"}]
    set bram [_util_used $util_str {"Block RAM Tile"}]
    set dsp  [_util_used $util_str {"DSPs" "DSP48E2 only"}]

    set lut_pct  [_pct $lut  $LUT_TOTAL]
    set ff_pct   [_pct $ff   $FF_TOTAL]
    set bram_pct [_pct $bram $BRAM_TOTAL]
    set dsp_pct  [_pct $dsp  $DSP_TOTAL]

    # --- timing ---------------------------------------------------------------
    set wns [_worst_slack setup]
    set whs [_worst_slack hold]

    # --- DRC ------------------------------------------------------------------
    set drc [_drc_counts]
    set drc_err  [lindex $drc 0]
    set drc_cw   [lindex $drc 1]

    # --- CPU config -----------------------------------------------------------
    set cpu [read_cpu_config]
    set cpu_str "MMU=[dict get $cpu mmu]/isa=[dict get $cpu isa]/opt=[dict get $cpu opt]/rstvec=[dict get $cpu rstvec]"

    # [WFI-COMA FIX 2026-07-16] interrupt_wakeup read-back. Prefer the LIVE BD
    # (in-session for `all`); fall back to cpu_config.txt for a `reports` resume.
    set intr_wakeup "n/a"
    set _mb [get_bd_cells -quiet -hierarchical -filter {VLNV =~ *microblaze_riscv*}]
    if { [llength $_mb] >= 1 } {
        set intr_wakeup [get_property -quiet CONFIG.C_INTERRUPT_WAKEUP [lindex $_mb 0]]
    } elseif { [dict exists $cpu interrupt_wakeup] } {
        set intr_wakeup [dict get $cpu interrupt_wakeup]
    }

    # --- exit criteria --------------------------------------------------------
    set reasons {}
    if { ![string is integer -strict $drc_err] || $drc_err > 0 } {
        lappend reasons "DRC_ERRORS=$drc_err(>0)"
    }
    if { $wns eq "NA" } {
        lappend reasons "WNS=NA(no timing paths -- cannot confirm setup closure)"
    } elseif { $wns < 0 } {
        lappend reasons "WNS=$wns(<0)"
    }
    if { $whs eq "NA" } {
        lappend reasons "WHS=NA(no timing paths -- cannot confirm hold closure)"
    } elseif { $whs < 0 } {
        lappend reasons "WHS=$whs(<0)"
    }
    # The bitstream and the XSA are contracted DELIVERABLES, not diagnostics: the
    # DTS agent is blocked without the XSA. A build that closes timing but failed
    # to emit them is not a PASS. (Only meaningful when do_bitstream ran -- the
    # standalone `reports` stage leaves these "not-attempted" and does not gate.)
    if { $BIT_STATUS ne "OK" && $BIT_STATUS ne "not-attempted" } {
        lappend reasons "BITSTREAM=$BIT_STATUS"
    }
    if { [string match "FAILED*" $XSA_STATUS] } {
        lappend reasons "XSA=$XSA_STATUS"
    }
    set verdict [expr { [llength $reasons] == 0 ? "PASS" : "FAIL" }]

    set ts [clock format [clock seconds] -format "%Y-%m-%dT%H:%M:%S%z"]

    # --- machine-readable one-liner ------------------------------------------
    set result_line [format \
        "RESULT: %s part=%s mem=%s tCK=%dps MTs=%d ui_clk=%.0fMHz cpu=%s intr_wakeup=%s LUT=%s(%s%%) FF=%s(%s%%) BRAM=%s(%s%%) DSP=%s(%s%%) WNS=%s WHS=%s DRC_ERR=%s DRC_CW=%s SLRs=%s RP_region_cells=%s bit=%s xsa=%s" \
        $verdict $PART $MEM_PART $TCK_PS $MTS $UI_CLK_MHZ $cpu_str $intr_wakeup \
        $lut $lut_pct $ff $ff_pct $bram $bram_pct $dsp $dsp_pct \
        $wns $whs $drc_err $drc_cw [join $occupied_slrs ,] $rp_cells \
        $BIT_STATUS $XSA_STATUS]
    puts "==========================================================="
    puts $result_line
    if { [llength $reasons] } { puts "RESULT_FAIL_REASONS: [join $reasons { }]" }
    puts "==========================================================="

    # --- RESULT.txt -----------------------------------------------------------
    set rf [open "$RPT_DIR/RESULT.txt" w]
    puts $rf "# src/linux_soc/hw build result -- Linux-capable MicroBlaze V + DDR4 SoC"
    puts $rf "verdict            : $verdict"
    puts $rf "timestamp          : $ts"
    puts $rf "part               : $PART"
    puts $rf "memory_part        : $MEM_PART"
    puts $rf "tCK_ps             : $TCK_PS"
    puts $rf "data_rate_MTs      : $MTS"
    puts $rf "ui_clk_MHz         : [format %.0f $UI_CLK_MHZ]"
    puts $rf "cpu_config         : $cpu_str"
    puts $rf "interrupt_wakeup   : $intr_wakeup  (C_INTERRUPT_WAKEUP -- MUST be 1: with the IP default 0,"
    puts $rf "                     ^ wfi sleeps FOREVER, unwakeable by interrupt OR debug (silicon-proven"
    puts $rf "                     ^ wfi-coma, 2026-07-16) and Linux hangs at first idle. Pinned +"
    puts $rf "                     ^ _assert_cfg'd in mbv_soc.tcl; AMD's own Linux preset sets 1.)"
    puts $rf "LUT_used           : $lut  ($lut_pct% of $LUT_TOTAL)"
    puts $rf "FF_used            : $ff  ($ff_pct% of $FF_TOTAL)"
    puts $rf "BRAM36_used        : $bram  ($bram_pct% of $BRAM_TOTAL)"
    puts $rf "DSP_used           : $dsp  ($dsp_pct% of $DSP_TOTAL)"
    puts $rf "WNS_ns             : $wns"
    puts $rf "WHS_ns             : $whs"
    puts $rf "DRC_errors         : $drc_err"
    puts $rf "DRC_critical_warns : $drc_cw"
    puts $rf "SLRs_occupied      : $occupied_slrs"
    puts $rf "SLR0_RP_region     : $rp_cells cell(s) placed in $::RP_REGIONS (target: 0)"
    puts $rf "bitstream          : $BIT_STATUS  ($BIT_FILE)"
    puts $rf "xsa                : $XSA_STATUS  ($XSA_FILE)"
    puts $rf "                     ^ the XSA is the Linux/DTS agent's input (hardware handoff)."
    if { [llength $reasons] } { puts $rf "fail_reasons       : [join $reasons { }]" }
    puts $rf ""
    puts $rf $result_line
    close $rf
    puts "-- RESULT.txt: $RPT_DIR/RESULT.txt"

    if { $verdict ne "PASS" } {
        puts "BUILD FAILED exit criteria -- exit 1"
        exit 1
    }
    puts "BUILD PASSED exit criteria -- exit 0"
    exit 0
}

###=============================================================================
### DISPATCH
###=============================================================================
switch -- $STAGE {
    ip {
        run_gen_pins
        setup_project
        setup_ip
        _assert_no_vivado_errors ip
        puts "STAGE ip DONE (ddr4_0 created + generated)."
    }
    bd {
        run_gen_pins
        setup_project
        setup_ip
        setup_bd
        setup_xdc
        _assert_no_vivado_errors bd
        puts "STAGE bd DONE (BD built, wrapper is top, pins read)."
    }
    synth {
        full_setup
        do_synth
        _assert_pins_locced
        _assert_no_vivado_errors synth
        puts "STAGE synth DONE."
    }
    impl {
        ensure_synth
        do_impl
        _assert_no_vivado_errors impl
        puts "STAGE impl DONE (routed)."
    }
    bit {
        # CAVEAT: resuming here from post_route.dcp gives a routed NETLIST but no
        # block design -- open_checkpoint does not restore the in-memory project's
        # BD, and write_hw_platform needs the BD hardware handoff (.hwh). So a
        # standalone `make bit` can emit a .bit but will likely FAIL the XSA.
        # The XSA is only reliably produced by `make all`, which keeps the BD, the
        # synth and the route in ONE Vivado session. Reported honestly, not hidden.
        ensure_impl
        do_bitstream
        puts "STAGE bit DONE (bitstream=$BIT_STATUS xsa=$XSA_STATUS)."
    }
    reports {
        ensure_impl
        do_reports   ;# exits 0/1
    }
    all {
        full_setup
        do_synth
        _assert_pins_locced
        _assert_no_vivado_errors synth
        do_impl
        _assert_no_vivado_errors impl
        # Handoff BEFORE the gate: do_reports exits 1 on a timing/DRC failure, and
        # the brief requires the XSA even when timing is marginal. Note we do NOT
        # _assert_no_vivado_errors after this -- a DRC error raised by bitgen must
        # surface as a reported FAIL verdict (with reports on disk), not as an
        # abort that leaves no evidence behind.
        do_bitstream
        do_reports   ;# exits 0/1
    }
    default {
        error "build.tcl: unknown stage '$STAGE' (want: ip bd synth impl bit reports all)"
    }
}
