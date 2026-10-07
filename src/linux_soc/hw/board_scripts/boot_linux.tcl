# Turnkey MBV-Linux board boot via local xsdb -> remote hw_server on the hub.
# Run LOCALLY:  /apps/Xilinx/Vivado/2024.1/bin/xsdb board_scripts/boot_linux.tcl
# Defaults = the silicon-proven auth-login stack (Image_irqfix + irquart_login DTB +
# rootfs_login). Override any of BIT/KERNEL/DTB/ROOTFS below to change the stack.
#
# Notes learned 2026-07-17:
#  * artifacts live on the WORKSTATION (home not shared with hub) -> xsdb runs LOCALLY,
#    dow reads local files and streams over JTAG to the remote hw_server.
#  * 30MB kernel dow = ~5-8 min. Run this from a BACKGROUND shell (a 2-min foreground
#    cap kills it mid-stream). Launch the ttyUSB6 console listener AFTER "STARTING CPU".
#  * linux_soc_dbg.bit already has C_INTERRUPT_WAKEUP=1 and the DDR4 smartconnect fix.
set BD   [expr {[info exists ::env(MPS3_LINUX_SOC_DIR)] ? $::env(MPS3_LINUX_SOC_DIR) : [file normalize [file join [file dirname [info script]] .. ..]]}]
set BIT    $BD/hw/build_dbg/linux_soc_dbg.bit
set KERNEL $BD/linux/artifacts/Image_irqfix
set DTB    $BD/linux/artifacts/mbv_soc_pland_irquart_login.dtb
set ROOTFS $BD/linux/artifacts/rootfs_login.cpio.gz
set FWJUMP $BD/package/sw/fw_jump.bin

connect -url tcp:<hub-fqdn>:3121
targets -set -filter {name =~ "xcku115"}
puts "PROGRAMMING $BIT ..."
fpga -file $BIT
puts "PROG_DONE"
after 6000
proc selhart {} { targets -set -filter {name =~ "*Hart*"} }
selhart
stop
after 500
# DDR sanity at the top load region before committing ~40MB
mwr 0x84000000 0xa5a5a5a5
puts "DDR_84000000_RB=[mrd -value 0x84000000] (expect 2779096485 = 0xa5a5a5a5)"
proc load1 {f a} { selhart; if {[catch {dow -data $f $a} e]} {puts "DOW_FAIL $a : $e"; return 0}; puts "DOW_OK $a"; return 1 }
puts "LOADING PAYLOADS ..."
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
puts "BOOT_DONE"
