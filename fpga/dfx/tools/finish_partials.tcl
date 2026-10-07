# finish_partials.tcl -- RECOVERY: generate the partial+clearing pairs (and the
# reference-RM FULL config bit = updatemem base) for RMs that are ALREADY routed
# into a prod/ dir, WITHOUT re-routing and WITHOUT re-locking the static.
# Mirrors build_dfx.tcl's write_bitstream section verbatim; reuses the same
# rm_list.tcl rm_field + rp_pblock naming. Then seeds overlay_inputs.txt with
# those base rows so a subsequent incremental add PRESERVES them.
#
# TRACKED COPY of fpga/dfx/build_clcd/finish_partials.tcl (gitignored scratch),
# moved here 2026-09-09. The original is left in place untouched.
#
# WHEN YOU NEED THIS: a `make prod` that died part-way (SIGTERM, OOM, a lost
# build window) leaves N routed dcps and no bitstreams. Re-running prod re-mints
# static_id and strands every fielded overlay. This finishes the run instead.
#
# The RM list was hardcoded to the six CLCD-era base RMs; it is now the optional
# 4th tclarg, defaulting to that same six so the recorded recovery reproduces.
# Pass the RMs your interrupted run actually routed.
#
# usage: vivado -mode batch -source finish_partials.tcl \
#          -tclargs <repo_root> <out_dir> <rp_inst> [rm_key ...]
set repo_root [lindex $argv 0]
set out_dir   [lindex $argv 1]
set rp_inst   [lindex $argv 2]
set base_rms  [lrange $argv 3 end]
if { [llength $base_rms] == 0 } {
    set base_rms {rm_greybox rm_regdemo_a rm_regdemo_b rm_led rm_uart_echo rm_nanosoc}
    puts "INFO: finish_partials using the DEFAULT base RM set: $base_rms"
}
set rp_pblock_name "pblock_rp_dut"
set reference_rm  rm_greybox

source $repo_root/fpga/dfx/rm_list.tcl
# Shared row helper: overlay_inputs.txt rows are RELATIVE to out_dir (see that
# file's header). Same rule as build_dfx.tcl, from the same source.
source $repo_root/fpga/dfx/tools/overlay_inputs.tcl
# The per-RM partial .ltx + its declared-vs-produced gate, exactly as build_dfx.tcl
# writes it: a recovered partial must carry the .ltx from the same routed config.
source $repo_root/fpga/dfx/tools/debug_probes.tcl

set idfh [open $out_dir/static_id.txt r]; set sid [string trim [read $idfh]]; close $idfh
puts "INFO: finish_partials reusing locked static_id=$sid (NOT recomputed)"

set rows {}
foreach rm_key $base_rms {
    set routed_dcp $out_dir/config_${rm_key}_routed.dcp
    if { ![file exists $routed_dcp] } { error "finish_partials: missing routed dcp $routed_dcp" }
    set pair_root $out_dir/config_${rm_key}_${rp_pblock_name}_partial
    puts "INFO: ---- $rm_key : writing bitstream(s) from $routed_dcp ----"
    open_checkpoint $routed_dcp
    if { $rm_key eq $reference_rm } {
        # reference RM: FULL device write -> config_rm_greybox.bit/.bin (boot
        # image / updatemem base) + its pblock-suffixed partial + clearing.
        write_bitstream -force -bin_file $out_dir/config_${rm_key}
    } else {
        write_bitstream -force -bin_file -cell [get_cells $rp_inst] $pair_root
    }
    write_rm_debug_probes $out_dir $rp_inst $rm_key
    close_project
    foreach f [list ${pair_root}.bin ${pair_root}_clear.bin ${pair_root}.bit ${pair_root}_clear.bit] {
        if { ![file exists $f] } { error "finish_partials: missing artefact $f for $rm_key (dir: [glob -nocomplain -tails -directory $out_dir config_${rm_key}*])" }
    }
    lappend rows [overlay_row $out_dir $rm_key [rm_field $rm_key rm_name] [rm_field $rm_key rm_id] ${pair_root}.bin ${pair_root}_clear.bin]
    puts [format "PARTIAL_OK %s : partial %d B, clearing %d B" \
        $rm_key [file size ${pair_root}.bin] [file size ${pair_root}_clear.bin]]
}

set ofh [open $out_dir/overlay_inputs.txt w]
puts $ofh "# rm_key rm_name rm_id partial_bin clearing_bin (recovered base partials, static_id=$sid)"
foreach row $rows { puts $ofh [join $row " "] }
close $ofh
puts "FINISH_PARTIALS_OK static_id=$sid rows=[llength $rows]"
