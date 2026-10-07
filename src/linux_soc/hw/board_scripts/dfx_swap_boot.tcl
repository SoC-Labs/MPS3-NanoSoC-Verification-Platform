# dfx_swap_boot.tcl — boot the MBV Linux harness on the DFX-SWAP-CAPABLE static.
#
# ⚠️ WHY THIS SCRIPT EXISTS (read before changing BIT):
# A partial bitstream is bound to the EXACT static implementation it was built
# against. The overlays in fpga/dfx/overlay_linux/ were built by
# fpga/dfx/build_dfx.tcl, which does its OWN place-and-route + lock_design of the
# static and emits its own full bitstream, config_rm_greybox.bit (static+greybox).
#
#   USE THIS  : fpga/dfx/build_linux/prod/config_rm_greybox.bit   (12,821,235 B)
#   NOT THIS  : build/transplant_impl/shell_linux_top.bit         (11,883,949 B)
#               ^ phaseB's SEPARATE impl run. Loading the overlay_linux partials
#                 onto it DESTROYS the whole FPGA configuration (recoverable only
#                 by reprogramming). This happened twice on 2026-07-24 before the
#                 mismatch was found — see docs/ICAP_SWAP_FAILURE_ANALYSIS.md and
#                 docs/ICAP_SWAP_PROVEN.md.
#
# The swap's static_id check does NOT catch this: it compares the overlay manifest
# against the provisioned FILE /etc/mps3/static_id, not the flown bitstream, so it
# reads 0x2B082E1B either way.
#
# GUARD (2026-07-24): board_scripts/dfx_preflight.sh now reads REGISTER.USERCODE from
# the RUNNING device over JTAG and refuses a swap unless it matches the UserID stamped
# in the static the partials came from. Proven in all three directions on silicon:
#   good static flown     + good expected -> MATCH    exit 0
#   good static flown     + phaseB bit    -> MISMATCH exit 1
#   unstamped static flown+ good expected -> MISMATCH exit 1  <- the destructive case
# USERCODE is not sticky: it tracked 0x5263642c -> 0xffffffff across reconfiguration.
# Run dfx_preflight.sh before any swap campaign, and again after programming — a
# "PROG_DONE" log line proves nothing if another lease holder reprograms behind you.
#
# Proven with this static (2026-07-24): first real ICAP swap, 6/6 repeat cycles,
# 30/30 live wire-conformance, aux backends + XVC up.
#
# Build the image with MPS3_STATIC_ID=0x2B082E1B (and MPS3_ROOT_PASSWD) so
# shell_id is correct at boot with no runtime provisioning step.

set HW [expr {[info exists ::env(MPS3_HARNESS_DIR)] ? $::env(MPS3_HARNESS_DIR) : [file normalize [file join [file dirname [info script]] .. .. .. linux_harness]]}]
source [file join [file dirname [info script]] board_image.tcl]  ;# canonical swap-capable image
# Stock wfi-idle kernel: the no-wfi workaround was RETIRED 2026-07-24 after the A/B
# (120 s fully idle, no wedge, swap still ok). Image_nowfi remains as a fallback only.
set KERNEL $HW/sw/artifacts/Image
set DTB    $HW/sw/artifacts/shell_linux.dtb
set ROOTFS $HW/sw/artifacts/rootfs.cpio.gz
set FWJUMP $HW/sw/artifacts/fw_jump.bin

connect -url tcp:<hub-fqdn>:3121
targets -set -filter {name =~ "xcku115"}
puts "PROGRAMMING (DFX-swap-capable static): $BIT"
fpga -file $BIT
puts "PROG_DONE"
after 8000

proc selhart {} { targets -set -filter {name =~ "*Hart*"} }
selhart
stop
after 500
mwr 0x84000000 0xa5a5a5a5
puts "DDR_84000000_RB=[mrd -value 0x84000000]"

proc load1 {f a} {
    selhart
    if {[catch {dow -data $f $a} e]} { puts "DOW_FAIL $a : $e"; return 0 }
    puts "DOW_OK $a"
    return 1
}
load1 $FWJUMP 0x80000000
load1 $KERNEL 0x80400000
load1 $DTB    0x82200000
load1 $ROOTFS 0x84000000
puts "PAYLOADS_LOADED"

selhart
rwr a0 0
rwr a1 0x82200000
rwr pc 0x80000000
puts "STARTING CPU (con)"
con
after 1500
disconnect
puts "DFX_SWAP_BOOT_DONE"
