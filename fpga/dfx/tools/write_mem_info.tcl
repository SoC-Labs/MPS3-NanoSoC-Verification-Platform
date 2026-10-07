# write_mem_info.tcl -- emit the MMI (BRAM<->LMB map) for a MicroBlaze
# implementation; `updatemem` needs it to patch the program into a bitstream.
#
# TRACKED COPY. Was fpga/dfx/build_clcd/write_mem_info.tcl, a gitignored scratch
# file that the mint script of the day executed -- i.e. a load-bearing step of
# the mint lived somewhere `git clone` does not reach. Moved here 2026-09-09;
# the build_clcd/ original is left in place untouched.
#
# EXECUTED TODAY BY: `make -C fpga/dfx mint` stage 6 (the MMI for the flashable
# base) -- see fpga/dfx/Makefile and docs/BUILD_AND_MINT.md -- and by
# fpga/dfx/tools/finish_rekey.sh Phase 1C. The mint script that used to call it
# (qspi_kvm_rekey/rebuild_jtag_uart.sh) was deleted 2026-09-10 once every step
# it performed had a make target.
#
# THE TRAP THIS FILE EXISTS TO AVOID. For the updatemem that produces the
# FLASHABLE base, the MMI must come from the DFX greybox ROUTED checkpoint, NOT
# from the non-DFX shell_harness implementation. They are different
# implementations, their BRAMs land in different places, and the wrong MMI
# corrupts the embed *silently* -- updatemem still exits 0. Pass the right
# checkpoint; the script cannot tell which one you meant.
#
# TWO INPUT FORMS, because there were two copies of this recipe. The mint script
# used to carry a run-time heredoc for the project form (invisible to grep) and
# source the scratch file for the checkpoint form. One file, both forms:
#
#   <in> ending .dcp -> open_checkpoint (the DFX routed config)
#   <in> ending .xpr -> open_project + open_run $impl (a shell project run)
#
# usage: vivado -mode batch -source write_mem_info.tcl \
#          -tclargs <routed.dcp | project.xpr> <out.mmi> [out.bit] [impl_run]
#
# [out.bit] is optional and only meaningful for the .xpr form: it write_bitstream's
# the opened run. [impl_run] defaults to impl_1.

set in   [lindex $argv 0]
set out  [lindex $argv 1]
set obit [lindex $argv 2]
set impl [lindex $argv 3]
if { $impl eq "" } { set impl impl_1 }

switch -- [string tolower [file extension $in]] {
    .dcp {
        puts "INFO: write_mem_info from CHECKPOINT $in"
        open_checkpoint $in
    }
    .xpr {
        puts "INFO: write_mem_info from PROJECT $in (run $impl)"
        open_project $in
        open_run $impl
    }
    default {
        error "write_mem_info.tcl: unrecognised input '$in' -- need a .dcp checkpoint or a .xpr project"
    }
}

write_mem_info -force $out
if { $obit ne "" } {
    puts "INFO: write_bitstream -> $obit"
    write_bitstream -force $obit
}
close_project
puts "WRITE_MEM_INFO_OK $out"
