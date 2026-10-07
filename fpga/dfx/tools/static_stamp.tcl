# -----------------------------------------------------------------------------
# static_stamp.tcl -- write static_stamp.json: what a mint PUT in the boot image.
#
# Sourced by fpga/dfx/build_dfx.tcl. Kept out of that file, and free of every
# Vivado command, for one reason: it is then unit-testable under plain tclsh
# (tests/dfx_flow/test_static_canon.py), exactly like tools/overlay_inputs.tcl.
# A JSON writer that only ever runs inside a four-hour Vivado job is a JSON
# writer whose first bad comma is found by the stage that consumes it.
#
# WHAT IT RECORDS, AND WHY TWO FIELDS
#   usercode   BITSTREAM.CONFIG.USERID  -- the 8-hex build commit. Discriminates
#              IMPLEMENTATION runs, so it is what binds an overlay to the exact
#              bitstream build it was keyed to (tools/stamp_usercode.py).
#   usr_access BITSTREAM.CONFIG.USR_ACCESS -- HARNESS_VER32. Discriminates
#              firmware RELEASES, so it is what the running image compares
#              itself against to catch firmware/bitstream skew
#              (docs/VERSIONING_PLAN.md §3.4).
# They answer different questions and neither substitutes for the other.
#
# NOT THE AUTHORITY. tools/stamp_usercode.py reads USERID out of the .bit's own
# ASCII header and uses this file as a CROSS-CHECK. Two independent derivations
# of one stamp that must agree, or the .bit beside the record is not the one the
# record describes.
# -----------------------------------------------------------------------------

# Normalise a config-register value to "0x" + hex, or "" if it is not one.
# Vivado reports an unset property as "" and some as bare hex; neither may be
# written out as if it were a stamp.
proc static_stamp_hex32 { value } {
    # Vivado's `get_property BITSTREAM.CONFIG.USERID` returns a Verilog SIZED
    # LITERAL, e.g. "32'hD46FCDCB" -- accept that as well as a 0x-prefixed or
    # bare hex value. Without this branch USERID was serialised as null, and
    # stamp_usercode.py then refused to bind the overlays ("records no usercode").
    if { [regexp {^[0-9]+'[hH]([0-9a-fA-F]{1,8})$} $value -> vdigits] } {
        return [format "0x%s" [string toupper $vdigits]]
    }
    if { [regexp {^(0[xX])?([0-9a-fA-F]{1,8})$} $value -> _prefix digits] } {
        return [format "0x%s" [string toupper $digits]]
    }
    return ""
}

# out_dir      where static_stamp.json goes (the prod dir)
# bitstream    the full .bit these stamps are IN, by bare name
# static_id    the id minted by this run, for cross-reference
# harness_ver  "" when DFX_NO_VERSION_STAMP suppressed the stamp, else the
#              semantic version string ("1.0.0")
# usercode     raw BITSTREAM.CONFIG.USERID as the design reports it
# usr_access   raw BITSTREAM.CONFIG.USR_ACCESS as the design reports it
proc write_static_stamp { out_dir bitstream static_id harness_ver usercode usr_access } {
    set uc [static_stamp_hex32 $usercode]
    set ua [static_stamp_hex32 $usr_access]

    set path [file join $out_dir static_stamp.json]
    set fh [open $path w]
    puts $fh "\{"
    puts $fh "  \"schema\": \"mps3-static-stamp\","
    puts $fh "  \"schema_version\": \"1\","
    puts $fh "  \"generated_by\": \"fpga/dfx/tools/static_stamp.tcl\","
    puts $fh "  \"bitstream\": \"$bitstream\","
    puts $fh "  \"static_id\": \"$static_id\","
    puts $fh "  \"stamped\": [expr { $harness_ver ne "" ? "true" : "false" }],"
    if { $harness_ver ne "" } {
        puts $fh "  \"harness_version\": \"$harness_ver\","
    } else {
        puts $fh "  \"harness_version\": null,"
    }
    puts $fh "  \"usercode\": [expr { $uc ne "" ? "\"$uc\"" : "null" }],"
    puts $fh "  \"usr_access\": [expr { $ua ne "" ? "\"$ua\"" : "null" }],"
    puts $fh "  \"note\": \"USERID discriminates IMPLEMENTATION runs (overlay binding); USR_ACCESS carries HARNESS_VER32 and discriminates firmware RELEASES (skew check). Read fpga/dfx/tools/static_stamp.tcl before trusting either.\""
    puts $fh "\}"
    close $fh
    return $path
}
