###-----------------------------------------------------------------------------
### fpga/shell/tools/bd_dump.tcl -- a canonical, diffable dump of an open BD.
###
### WHY. The CPU seam (fpga/shell/bd/cpu_{mb,mbv}.tcl) moved ~200 lines of the
### classic MicroBlaze subsystem out of shell_bd.tcl. The bare-metal static is
### the fielded fallback for the whole Linux programme, so "the refactor changed
### nothing" has to be a measured fact, not a reading of a diff. validate_bd
### passing proves the BD is legal; it does not prove it is the SAME BD. This
### dump is what makes "the same" checkable: every cell, its VLNV and every
### CONFIG.* value, every pin and the net on it, every net and all its members,
### every port, and every address segment, in one sorted text file.
### tests/shell_cpu_seam/compare_bd_dumps.py diffs two of them.
###
### WHAT IS DELIBERATELY LEFT OUT. Layout (LOCATION/SCREENSIZE: GUI placement,
### not hardware), and nothing else. In particular NET NAMES ARE KEPT: Vivado
### names a net after its first connection, the name survives into the wrapper
### and the synthesised netlist, and a reordering that renamed nets would change
### the static's netlist even though no wire moved. The dump would rather fail
### on that than let it through.
###
### Deterministic by construction: every collection is lsort-ed, every property
### list is lsort-ed, and the only line that varies with the tool (the version)
### is a `#` comment the comparator skips.
###
### Usage (inside a Vivado session with the BD open):
###   source fpga/shell/tools/bd_dump.tcl
###   soclabs_bd_dump <out_file> [<label>]
###-----------------------------------------------------------------------------

proc _bdd_props {obj pattern} {
    # CONFIG.* (or any pattern) as sorted "key=value" strings. -quiet on the
    # read: a few read-only derived properties refuse get_property on some IP.
    set out {}
    foreach k [lsort [list_property -quiet $obj $pattern]] {
        lappend out "$k=[get_property -quiet $k $obj]"
    }
    return $out
}

proc _bdd_net_of {pin} {
    set n [get_bd_nets -quiet -of_objects $pin]
    if { $n eq "" } { return "-" }
    return [lsort $n]
}

proc _bdd_intf_net_of {ipin} {
    set n [get_bd_intf_nets -quiet -of_objects $ipin]
    if { $n eq "" } { return "-" }
    return [lsort $n]
}

proc soclabs_bd_dump {out_file {label ""}} {
    set bd [current_bd_design -quiet]
    if { $bd eq "" } {
        error "soclabs_bd_dump: no block design is open"
    }
    set L {}
    lappend L "# soclabs BD dump v1 -- fpga/shell/tools/bd_dump.tcl"
    lappend L "# vivado [version -short]  label=$label"
    lappend L "DESIGN [get_property NAME $bd]"

    # ---- external ports ----------------------------------------------------
    foreach p [lsort [get_bd_ports -quiet]] {
        lappend L [format "PORT %s dir=%s type=%s left=%s right=%s net=%s" $p \
            [get_property -quiet DIR $p] [get_property -quiet TYPE $p] \
            [get_property -quiet LEFT $p] [get_property -quiet RIGHT $p] \
            [_bdd_net_of $p]]
        foreach kv [_bdd_props $p CONFIG.*] { lappend L "  $kv" }
    }
    foreach p [lsort [get_bd_intf_ports -quiet]] {
        lappend L [format "IPORT %s vlnv=%s mode=%s net=%s" $p \
            [get_property -quiet VLNV $p] [get_property -quiet MODE $p] \
            [_bdd_intf_net_of $p]]
        foreach kv [_bdd_props $p CONFIG.*] { lappend L "  $kv" }
    }

    # ---- cells, their CONFIG, their pins -----------------------------------
    set ncell 0
    set ncfg  0
    foreach c [lsort [get_bd_cells -quiet -hierarchical]] {
        incr ncell
        lappend L [format "CELL %s vlnv=%s type=%s" $c \
            [get_property -quiet VLNV $c] [get_property -quiet TYPE $c]]
        foreach kv [_bdd_props $c CONFIG.*] { lappend L "  $kv"; incr ncfg }
        foreach p [lsort [get_bd_pins -quiet -of_objects $c]] {
            lappend L [format "  PIN %s dir=%s type=%s left=%s right=%s net=%s" \
                [file tail $p] [get_property -quiet DIR $p] \
                [get_property -quiet TYPE $p] [get_property -quiet LEFT $p] \
                [get_property -quiet RIGHT $p] [_bdd_net_of $p]]
            foreach kv [_bdd_props $p CONFIG.*] { lappend L "    $kv" }
        }
        foreach p [lsort [get_bd_intf_pins -quiet -of_objects $c]] {
            lappend L [format "  IPIN %s vlnv=%s mode=%s net=%s" [file tail $p] \
                [get_property -quiet VLNV $p] [get_property -quiet MODE $p] \
                [_bdd_intf_net_of $p]]
            foreach kv [_bdd_props $p CONFIG.*] { lappend L "    $kv" }
        }
    }

    # ---- nets: every member, so a moved wire shows on BOTH of its ends -----
    foreach n [lsort [get_bd_nets -quiet -hierarchical]] {
        set m [lsort [concat [get_bd_pins -quiet -of_objects $n] \
                             [get_bd_ports -quiet -of_objects $n]]]
        lappend L "NET $n : [join $m { }]"
    }
    foreach n [lsort [get_bd_intf_nets -quiet -hierarchical]] {
        set m [lsort [concat [get_bd_intf_pins -quiet -of_objects $n] \
                             [get_bd_intf_ports -quiet -of_objects $n]]]
        lappend L "INET $n : [join $m { }]"
    }

    # ---- address map: per master address space, assigned + excluded -------
    foreach sp [lsort [get_bd_addr_spaces -quiet]] {
        foreach s [lsort [get_bd_addr_segs -quiet -of_objects $sp]] {
            lappend L [format "ASEG %s offset=%s range=%s excluded=%s slave=%s" $s \
                [get_property -quiet OFFSET $s] [get_property -quiet RANGE $s] \
                [get_property -quiet EXCLUDED $s] \
                [get_bd_addr_segs -quiet -of_objects $s]]
        }
    }
    foreach s [lsort [get_bd_addr_segs -quiet -excluded]] {
        lappend L "XSEG $s"
    }

    lappend L "# summary cells=$ncell config_values=$ncfg"
    set fh [open $out_file w]
    puts $fh [join $L "\n"]
    close $fh
    puts "BD_DUMP_WRITTEN $out_file cells=$ncell config_values=$ncfg lines=[llength $L]"
    return $out_file
}
