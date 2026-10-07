# ---------------------------------------------------------------------------
# xvc_smoke.tcl -- W0 / B1: does Vivado 2024.1 open the shell's XVC target, and
# EXACTLY what does it see? (handover HANDOVER_RM_ILA_OVER_XVC.md §6 B1, §7 U2)
#
# Run it through scripts/mps3_xvc_smoke.sh (tunnel, logging, verdict, exit
# status). By hand:
#     XVC_URL=localhost:2542 vivado -mode batch -nojournal -source host/xvc/xvc_smoke.tcl
#
# Prints, in order: the hw_server, every hw_target, every property of the
# current target, every hw_device and every property of each, the debug cores
# Vivado found after a refresh, then ONE verdict line:
#     XVC_SMOKE PASS: target=<name> device=<name> ilas=<n>
#     XVC_SMOKE PARTIAL: target=<name> opened but no hw_device ...   (exit 3)
#     XVC_SMOKE FAIL: <why>                                          (exit 1)
# On the bare-metal 0x72BB0A36 shell (the rollback image) debug_bridge_0 has ONE BSCAN master
# (C_NUM_BS_MASTER=1), so what Vivado finds depends on the resident RM: with
# rm_dbg_demo it found device debug_bridge_0 carrying 1 ILA
# (docs/evidence/2026-09-w3/ila_proofs_20260924.txt, B1). On the previous shell
# 0x3F1A560F (no BSCAN master) this smoke ended PARTIAL, an empty chain
# (docs/evidence/2026-09-w3/w0_xvc_smoke_20260923.txt).
# ---------------------------------------------------------------------------
source [file join [file dirname [file normalize [info script]]] xvc_common.tcl]

set XVC_URL [xvc_cfg XVC_URL localhost:2542]
set HW_URL  [xvc_cfg HW_SERVER_URL localhost:3121]

xvc_say "== XVC smoke ============================================"
xvc_say "   vivado    : [version -short]"
xvc_say "   xvc_url   : $XVC_URL"
xvc_say "   hw_server : $HW_URL (local)"
xvc_say "========================================================="

if {[catch {xvc_open $XVC_URL $HW_URL} err]} {
    xvc_close
    puts "XVC_SMOKE FAIL: open_hw_target -xvc_url $XVC_URL did not open: [string map {\n { }} $err]"
    exit 1
}

set tgt [current_hw_target]
xvc_say "--- get_hw_targets ---"
foreach t [get_hw_targets -quiet] { xvc_say "  target: $t" }
xvc_say "--- report_property current_hw_target ---"
catch {report_property $tgt} rp
puts $rp

set devs [get_hw_devices -quiet]
xvc_say "--- get_hw_devices ([llength $devs]) ---"
foreach d $devs { xvc_say "  device: $d" }
foreach d $devs {
    xvc_say "--- report_property $d ---"
    catch {report_property $d} rp
    puts $rp
}

if {[llength $devs] == 0} {
    xvc_close
    puts "XVC_SMOKE PARTIAL: target=$tgt opened but no hw_device was found (getinfo answered; the chain scan found nothing)"
    exit 3
}

set dev [current_hw_device]
# refresh_hw_device is what scans for debug cores. With no probes file Vivado
# still lists the hubs/cores it finds; a warning about "no debug cores" is the
# expected outcome on a shell whose bridge has no BSCAN master.
if {[catch {refresh_hw_device $dev} rerr]} {
    xvc_say "refresh_hw_device raised: [string map {\n { }} $rerr]"
}
xvc_say "--- report_property $dev (after refresh) ---"
catch {report_property $dev} rp
puts $rp

set cores [get_debug_cores -quiet]
set ilas  [get_hw_ilas -quiet -of_objects $dev]
set vios  [get_hw_vios -quiet -of_objects $dev]
xvc_say "--- get_debug_cores -quiet ([llength $cores]) : $cores"
xvc_say "--- get_hw_ilas -quiet     ([llength $ilas]) : $ilas"
xvc_say "--- get_hw_vios -quiet     ([llength $vios]) : $vios"
foreach i $ilas {
    xvc_say "--- report_property $i ---"
    catch {report_property $i} rp
    puts $rp
}

xvc_close
puts "XVC_SMOKE PASS: target=$tgt device=$dev ilas=[llength $ilas]"
exit 0
