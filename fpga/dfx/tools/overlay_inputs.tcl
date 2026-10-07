# -----------------------------------------------------------------------------
# overlay_inputs.tcl -- the one rule for how a row of overlay_inputs.txt names a
# bitstream. Sourced by build_dfx.tcl (both the full build and the incremental
# add) and by tools/finish_partials.tcl (the recovery path); unit-tested by
# tests/dfx_flow/test_mint_record.py under plain tclsh, with no Vivado.
#
# WHY THIS EXISTS. build_dfx.tcl wrote its hand-off rows with ABSOLUTE paths:
#
#   rm_greybox greybox 0x00000000 \
#     /home/<user>/<repo>/fpga/dfx/build_mint/prod/config_rm_greybox_..._partial.bin ...
#
# Ten such rows are what `fielded/0xA8C1C535/overlay_inputs.txt` still holds.
# The file is the record of which artefacts a mint produced, and it is exactly
# the file you want to copy to the hub, hand to another checkout, commit beside
# the checksums, or read back a year later -- and it could do none of those,
# because every row named one workstation's home directory. The artefacts sit
# NEXT TO the file that lists them; the only honest way to name them is
# relative to it.
#
# Vivado's own `file relative` does not exist, and `fileutil::relative` is a
# tcllib package Vivado does not ship, so the walk is written out here.
# -----------------------------------------------------------------------------

# overlay_row_rel out_dir path -> path relative to out_dir.
#
#   /a/b/prod   /a/b/prod/config_rm_led_partial.bin -> config_rm_led_partial.bin
#   /a/b/prod   config_rm_led_partial.bin           -> config_rm_led_partial.bin
#   /a/b/prod   /a/b/other/x.bin                    -> ../other/x.bin
#
# An already-relative path is returned unchanged: it is already relative to the
# out_dir by construction (every caller builds its pair paths from $out_dir).
# A path outside out_dir still comes back relative -- with `..` segments -- so
# the invariant "no row in overlay_inputs.txt is absolute" holds unconditionally
# and can be gated. On Linux every path shares "/", so this always terminates.
proc overlay_row_rel { out_dir path } {
    if { [file pathtype $path] ne "absolute" } {
        return $path
    }
    set from [file split [file normalize $out_dir]]
    set to   [file split [file normalize $path]]

    # drop the common prefix
    while { [llength $from] > 0 && [llength $to] > 0 &&
            [lindex $from 0] eq [lindex $to 0] } {
        set from [lrange $from 1 end]
        set to   [lrange $to   1 end]
    }
    # one ".." per remaining out_dir segment, then the rest of the target
    set parts {}
    foreach _ $from { lappend parts ".." }
    foreach seg $to { lappend parts $seg }
    if { [llength $parts] == 0 } { return "." }
    return [join $parts "/"]
}

# overlay_row out_dir rm_key rm_name rm_id partial clearing -> the row list,
# with both bitstream paths relativized. Callers `lappend` the result and join
# it with spaces; keeping the assembly here means a new caller cannot forget.
proc overlay_row { out_dir rm_key rm_name rm_id partial clearing } {
    return [list $rm_key $rm_name $rm_id \
                [overlay_row_rel $out_dir $partial] \
                [overlay_row_rel $out_dir $clearing]]
}
