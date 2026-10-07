# -----------------------------------------------------------------------------
# dbg_ip.tcl -- the debug IP an RM needs to carry its own ILAs, created BY
# SCRIPT (never a committed .xci), the same way fpga/shell/bd/shell_bd.tcl
# creates the shell's IP. Handover: docs/planning/HANDOVER_RM_ILA_OVER_XVC.md §4.6.
#
# WHAT IT MAKES
#   * one `debug_bridge` in mode 1 (From_BSCAN_to_DebugHub), C_DESIGN_TYPE 1
#     (Reconfigurable_region_of_DFX_designs), hub clock declared at 50 MHz
#     (= phy_rmii_ref_clk, the only always-on shell clock in the RP, F12),
#     C_USE_BUFR 0 -- a BUFR in the RP is HDPR-18 (trap 1: NO clock buffer may
#     live in the RP). The bridge carries the RM's debug hub (xsdbm); every ILA
#     in the same RM is auto-joined to it at opt_design (F4).
#   * N `ila`s, one per capture set.
#
# Then `generate_target all` + `synth_ip` on each, so each IP is synthesised
# OUT OF CONTEXT with its OWN XDC applied (handover §7 U6), in a THROWAWAY
# in-memory project. The caller opens its own project afterwards and
# `read_ip`s the returned .xci list; synth_design then links the IP netlists.
#
# USAGE (from an RM's ooc_synth.tcl, BEFORE its own create_project):
#
#   source [file join $repo fpga/rp/common/dbg_ip.tcl]
#   set xcis [dbg_ip_build $ip_dir $part dbg_hub_bridge \
#               [list [list ila_foo {16 1} 1024 0]]]
#   create_project -in_memory -part $part
#   read_ip $xcis
#
# ILA spec = {module_name probe_widths depth ?strg_qual?}
#   probe_widths  list, one entry per probe (C_NUM_OF_PROBES = its length)
#   depth         C_DATA_DEPTH (1024, 2048, ... 131072)
#   strg_qual     optional, 1 = enable capture (storage) qualification
#                 (C_EN_STRG_QUAL) -- every probe then gets 2 match units, as
#                 the IP requires. Lets a capture keep only qualified samples
#                 (e.g. only UART bytes), so a deep buffer spans seconds.
#
# The module names are the caller's: the RM wrapper instantiates them by name.
# Never put a BSCANE2 or a clock buffer in an RM (F7, trap 1, trap 2: an ILA
# with no RM bridge makes Vivado look for a BSCANE2 hub -- HDPR-16).
# -----------------------------------------------------------------------------

proc dbg_ip_build { ip_dir part bridge_name ila_specs {hub_clk_hz 50000000} } {
    file mkdir $ip_dir
    create_project -in_memory -part $part
    set_property target_language Verilog [current_project]

    # --- the RM's debug hub: BSCAN (from the partition pins) -> xsdbm --------
    create_ip -name debug_bridge -vendor xilinx.com -library ip -version 3.0 \
        -module_name $bridge_name -dir $ip_dir -force
    set_property -dict [list \
        CONFIG.C_DEBUG_MODE        {1} \
        CONFIG.C_DESIGN_TYPE       {1} \
        CONFIG.C_CLK_INPUT_FREQ_HZ $hub_clk_hz \
        CONFIG.C_USE_BUFR          {0} \
    ] [get_ips $bridge_name]

    # --- one ILA per capture set ---------------------------------------------
    foreach spec $ila_specs {
        lassign $spec name widths depth strg_qual
        if { $strg_qual eq "" } { set strg_qual 0 }
        create_ip -name ila -vendor xilinx.com -library ip \
            -module_name $name -dir $ip_dir -force
        set cfg [list CONFIG.C_NUM_OF_PROBES [llength $widths] \
                      CONFIG.C_DATA_DEPTH    $depth \
                      CONFIG.C_EN_STRG_QUAL  $strg_qual]
        set n 0
        foreach w $widths {
            lappend cfg CONFIG.C_PROBE${n}_WIDTH $w
            if { $strg_qual } { lappend cfg CONFIG.C_PROBE${n}_MU_CNT 2 }
            incr n
        }
        set_property -dict $cfg [get_ips $name]
    }

    # --- report what the IP really took (the log is the evidence) ------------
    set xcis {}
    foreach ip [get_ips] {
        foreach p {C_DEBUG_MODE C_DESIGN_TYPE C_CLK_INPUT_FREQ_HZ C_USE_BUFR
                   C_NUM_OF_PROBES C_DATA_DEPTH C_EN_STRG_QUAL} {
            set v [get_property -quiet CONFIG.$p $ip]
            if { $v ne "" } { puts "DBG_IP: $ip $p = $v" }
        }
        lappend xcis [get_property IP_FILE $ip]
    }

    # --- generate + synthesise each IP OUT OF CONTEXT (its own XDC applies) ---
    foreach ip [get_ips] {
        generate_target all $ip
        synth_ip $ip
        puts "DBG_IP: synthesised $ip -> [get_property IP_FILE $ip]"
    }
    close_project
    return $xcis
}

# -----------------------------------------------------------------------------
# dbg_rm_netlist_checks -- the four netlist properties an ILA-carrying RM must
# have after OOC synth_design (ILA mint plan §3 DEBUG-RM "Done when"), printed
# as DBG_CHECK lines the log keeps as evidence. Returns the number of FAILED
# checks; the caller must refuse to print its completion marker on non-zero.
#
#   BSCANE2 cells              == 0   (F7: no BSCAN site in the RP; trap 2)
#   PRIMITIVE_GROUP==CLOCK     == 0   (F7/trap 1: no clock buffer in the RP)
#   xsdbm debug hubs           == 1   (exactly one hub: the RM bridge's)
#   ILA cores                  >= 1   (each named module in `ila_names`)
# -----------------------------------------------------------------------------
proc dbg_rm_netlist_checks { ila_names } {
    set fails 0

    set bscan [get_cells -quiet -hier -filter {REF_NAME =~ BSCAN*}]
    set n [llength $bscan]
    puts "DBG_CHECK: BSCANE2 cells = $n (want 0) [expr {$n == 0 ? {PASS} : {FAIL}}] $bscan"
    if { $n != 0 } { incr fails }

    set clk [get_cells -quiet -hier -filter {PRIMITIVE_GROUP == CLOCK}]
    set n [llength $clk]
    puts "DBG_CHECK: PRIMITIVE_GROUP==CLOCK cells = $n (want 0) [expr {$n == 0 ? {PASS} : {FAIL}}] $clk"
    if { $n != 0 } { incr fails }

    # The hub is the xsdbm core inside the bridge. Count only the OUTERMOST
    # hierarchical cell whose REF_NAME names an xsdbm (its own sub-modules can
    # carry xsdbm-prefixed names too).
    set hubs {}
    foreach c [get_cells -quiet -hier -filter {REF_NAME =~ *xsdbm*}] {
        set p [get_property PARENT $c]
        set nested 0
        while { $p ne "" } {
            set pc [get_cells -quiet $p]
            if { $pc ne "" && [string match *xsdbm* [get_property REF_NAME $pc]] } { set nested 1; break }
            set p [get_property -quiet PARENT $pc]
            if { $pc eq "" } { break }
        }
        if { !$nested } { lappend hubs $c }
    }
    set n [llength $hubs]
    puts "DBG_CHECK: xsdbm debug hubs = $n (want 1) [expr {$n == 1 ? {PASS} : {FAIL}}] $hubs"
    if { $n != 1 } { incr fails }

    set ilas {}
    foreach name $ila_names {
        foreach c [get_cells -quiet -hier -filter "REF_NAME == $name || ORIG_REF_NAME == $name"] {
            lappend ilas $c
        }
    }
    set n [llength $ilas]
    puts "DBG_CHECK: ILA cores = $n (want >= 1) [expr {$n >= 1 ? {PASS} : {FAIL}}] $ilas"
    if { $n < 1 } { incr fails }

    # Utilisation summary (the headroom table is in RAMB36 tiles + CLB %).
    set u [report_utilization -return_string]
    foreach {label re} {
        LUT  {\|\s*CLB LUTs\*?\s*\|\s*(\d+)}
        FF   {\|\s*CLB Registers\s*\|\s*(\d+)}
        BRAM {\|\s*Block RAM Tile\s*\|\s*([0-9.]+)}
    } {
        if { [regexp $re $u -> v] } { set util($label) $v } else { set util($label) "?" }
    }
    puts "DBG_CHECK: utilisation LUT=$util(LUT) FF=$util(FF) BRAM36=$util(BRAM)"

    puts "DBG_CHECK: [expr {$fails == 0 ? {ALL PASS} : "$fails FAILED"}]"
    return $fails
}
