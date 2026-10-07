# -----------------------------------------------------------------------------
# ooc_synth.tcl -- out-of-context synth + place + route + sign-off reports for
# LCDMIR (lcd_mirror) on xcku115-flvb1760-1-c at 100 MHz.
#
# The sizing / timing datapoint for LCD_MIRROR_FPGA.md §3 (estimated 38 RAMB36,
# ~1.2K LUT, ~1.5K FF). Shaped like fpga/rp/*/ooc_synth.tcl, plus place/route:
# a static block has no RP pblock to fit, the question is resources + timing.
#
# Usage (from anywhere):
#   nice -n 19 vivado -mode batch -source fpga/shell/ip/lcd_mirror/ooc_synth.tcl \
#        -journal <out>/ooc.jou -log <out>/ooc.log
# Env: OUT_DIR (default <this dir>/build), OOC_THREADS (default 2),
#      LCDMIR_BANKED (default 0), LCDMIR_ADDR_W (default 32 = the BD width),
#      LCDMIR_DIRTY_CMP (default 1 = compare-on-write dirty tracking built).
# -----------------------------------------------------------------------------
set part xcku115-flvb1760-1-c
set top  lcd_mirror

set _dir [file dirname [file normalize [info script]]]
set out_dir [expr {[info exists ::env(OUT_DIR)] && $::env(OUT_DIR) ne "" ? $::env(OUT_DIR) : "$_dir/build"}]
set threads [expr {[info exists ::env(OOC_THREADS)] ? $::env(OOC_THREADS) : 2}]
set banked  [expr {[info exists ::env(LCDMIR_BANKED)] ? $::env(LCDMIR_BANKED) : 0}]
set addr_w  [expr {[info exists ::env(LCDMIR_ADDR_W)] ? $::env(LCDMIR_ADDR_W) : 32}]
set dcmp    [expr {[info exists ::env(LCDMIR_DIRTY_CMP)] ? $::env(LCDMIR_DIRTY_CMP) : 1}]
file mkdir $out_dir
set_param general.maxThreads $threads

create_project -in_memory -part $part
read_verilog -sv [list $_dir/lcd_mirror_ram.sv $_dir/lcd_mirror_core.sv $_dir/lcd_mirror.sv]

synth_design -mode out_of_context -top $top -part $part \
    -generic C_S_AXI_ADDR_WIDTH=$addr_w -generic BANKED=$banked -generic DIRTY_CMP=$dcmp
read_xdc $_dir/lcd_mirror_ooc.xdc
report_utilization    -file $out_dir/util_synth_lcd_mirror.rpt
write_checkpoint -force $out_dir/lcd_mirror_synth.dcp

opt_design
place_design
phys_opt_design
route_design
report_utilization          -file $out_dir/util_routed_lcd_mirror.rpt
report_utilization -hierarchical -file $out_dir/util_routed_hier_lcd_mirror.rpt
report_timing_summary -max_paths 10 -file $out_dir/timing_routed_lcd_mirror.rpt
report_ram_utilization      -file $out_dir/ram_lcd_mirror.rpt
write_checkpoint -force $out_dir/lcd_mirror_routed.dcp

set wns [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -setup]]
set whs [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -hold]]
puts "LCDMIR_OOC_RESULT banked=$banked addr_w=$addr_w dirty_cmp=$dcmp WNS=$wns WHS=$whs"
puts "LCD_MIRROR_OOC_COMPLETE"
