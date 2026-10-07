# bank_gate.tcl -- falsifier for the CLCD bank-voltage gate: list every PORT in
# the bank that carries the CLCD pins, with its IOSTANDARD, so the caller can
# assert none is non-1.8V. (iobank has no VCCO property in 2024.1; IOSTANDARD
# per placed port is the durable proof.)
#
# TRACKED COPY of fpga/dfx/build_clcd/bank_gate.tcl (gitignored scratch), moved
# here 2026-09-09. Referenced by tools/finish_rekey.sh gate G2. The original is
# left in place untouched.
#
# The bank is DERIVED from a probe pin rather than hardcoded, because the bank
# NUMBER is a device+package fact and the pin is a board fact. AP14 is a CLCD
# pin on the MPS3 KU115 (bank 66); override for another board/pinout.
#
# usage: vivado -mode batch -source bank_gate.tcl -tclargs <routed.dcp> [probe_pin]
set dcp   [lindex $argv 0]
set probe [lindex $argv 1]
if { $probe eq "" } { set probe AP14 }
open_checkpoint $dcp
set b [get_property BANK [get_package_pins $probe]]
puts "CLCD_BANK $b (probe pin $probe)"
foreach port [get_ports -quiet -of_objects [get_iobanks $b]] {
  puts "BANKPORT $port IOSTD=[get_property IOSTANDARD $port] BANK=$b"
}
close_project
puts "BANK_GATE_OK"
