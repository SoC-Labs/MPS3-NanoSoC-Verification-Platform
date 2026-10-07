# board_swap.tcl — program + partial-swap the DFX design on a real MPS3 over JTAG.
#
# The proof/DFX static shell has NO on-chip ICAP loader (that is the networked
# production shell, not yet built), so partials are delivered from a host over
# JTAG via the Vivado Hardware Manager (FT2232 -> KU115 config TAP). This is
# the first DFX-on-board test: swap the RP with the shell staying up.
#
# Prereqs on the host that can see the board's JTAG:
#   - Vivado 2024.1 hw_server running where the FT2232 attaches:
#       hw_server &            (or over the network: hw_server -s tcp::3121)
#   - The bitstreams from build_proof.tcl (+ build_rm_nanosoc.tcl):
#       config_greybox.bit and the partial/clearing pairs. Exact partial names
#       are config_<rm>_pblock_rp_dut_partial{,_clear}.bit — check your out dir.
#
# Usage:
#   vivado -mode batch -source board_swap.tcl -tclargs <bitdir> [<hw_url>]
# <hw_url> defaults to localhost:3121; <bitdir> holds the .bit files.
#
# NOTE ON UltraScale ORDERING (load-bearing): before loading a NEW partial you
# MUST load the CURRENTLY-loaded RM's CLEARING bitstream first (there is no
# shell coordinator here — you sequence it by hand). Post-power-on the loaded
# RM is the greybox (baked into config_greybox.bit).
if { [llength $argv] < 1 } { error "usage: -tclargs <bitdir> \[<hw_url>\]" }
set bitdir [file normalize [lindex $argv 0]]
set hw_url [expr { [llength $argv] >= 2 ? [lindex $argv 1] : "localhost:3121" }]

open_hw_manager
connect_hw_server -url $hw_url
# never "the first target": the MPS3's cable only (a SHARED hw_server has others)
source [file join [file dirname [file normalize [info script]]] .. .. .. scripts mps3_hw_target.tcl]
set cable [expr {[info exists ::env(MPS3_JTAG_CABLE)] ? $::env(MPS3_JTAG_CABLE) : "*210249B86C47*"}]
set dev [mps3_open_hw_target $cable]
puts "INFO: target device = $dev"

proc prog { dev bit } {
    puts "\n>>> programming [file tail $bit]"
    set_property PROGRAM.FILE $bit $dev
    program_hw_devices $dev
    refresh_hw_device $dev
}

# --- 1. full static + greybox (skip if the MCC already loaded it from SD) -----
prog $dev $bitdir/config_greybox.bit
puts "INFO: greybox loaded — LED\[3:0\] static (0)."

# --- 2. swap greybox -> led: clear greybox, then load led partial ------------
# (filenames verified against the actual build outputs: the reference RM
#  greybox emits pblock-suffixed names from its full write; the -cell writes
#  for led/nanosoc emit config_<rm>.bit + config_<rm>_clear.bit.)
prog $dev $bitdir/config_greybox_pblock_rp_dut_partial_clear.bit
prog $dev $bitdir/config_led.bit
puts "INFO: LED RM loaded — LED\[3:0\] should now BLINK. DFX swap proven."

# --- 3. swap led -> nanosoc: clear led, then load nanosoc partial ------------
prog $dev $bitdir/config_led_clear.bit
prog $dev $bitdir/config_nanosoc.bit
puts "INFO: nanosoc RM loaded — the real Cortex-M0 SoC now runs in the RP."
puts "INFO: check UART (CMSDK UART2 TX) for the hello banner if firmware baked."

puts "\nBOARD_SWAP_COMPLETE"
