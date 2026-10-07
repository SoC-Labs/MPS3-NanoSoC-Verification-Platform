# board_image.tcl — THE canonical bootable harness image. Single source of truth.
#
# Sets:  BIT             path to the full bitstream every boot script programs
#        BIT_USERCODE    its BITSTREAM.CONFIG.USERID, as the hardware reports it
#                        back in REGISTER.USERCODE (dfx_preflight.sh checks this)
#
# ── WHY THIS FILE EXISTS ────────────────────────────────────────────────────
# There were two implementations of the SAME synthesized design, and booting the
# wrong one destroyed the FPGA configuration on 2026-07-24 (twice):
#
#   ONE synthesis   src/linux_harness/build/transplant_impl/shell_static_synth.dcp
#      ├─ phaseB's own P&R  -> transplant_impl/shell_linux_top.bit   (11,883,949 B)
#      └─ build_dfx.tcl P&R -> build_linux/prod/config_rm_greybox.bit(12,821,235 B)
#
# build_dfx.tcl re-implements that synthesis with the reconfigurable partition
# and lock_design, then emits BOTH the full static+greybox bitstream AND every
# partial. A partial is bound to the exact static P&R it was built against, so
# only build_dfx's static can accept them. phaseB's bitstream is the same design
# but a different placement/routing — loading a partial onto it corrupts the
# whole configuration, deterministically, by any write path.
#
# => The DFX static is THE image. It is not a build by-product: it is the only
#    implementation the shipped partials can be loaded onto, and everything the
#    harness does was proven on it (Linux boot, eth0, control plane, aux
#    backends, 30/30 wire conformance, 6/6 swaps — docs/ICAP_SWAP_PROVEN.md).
#    phaseB's shell_linux_top.bit is NOT a product image. Do not boot it.
#
# Override for a one-off experiment with MPS3_BOARD_BIT, but understand that a
# swap onto a non-DFX static destroys the configuration. dfx_preflight.sh will
# refuse it — let it.
#
# Keep BIT_USERCODE in step with BIT. It is asserted against the overlays at
# build time by scripts/harness_gates/check_image_overlay_match.py, and against
# the live device before every swap by board_scripts/dfx_preflight.sh.

if { [info exists ::env(MPS3_BOARD_BIT)] } {
    set BIT $::env(MPS3_BOARD_BIT)
    puts "board_image.tcl: WARNING — BIT overridden from MPS3_BOARD_BIT: $BIT"
    puts "board_image.tcl: if this is not build_dfx's static, DFX swaps WILL destroy the configuration"
} else {
    set BIT [file normalize [file join [file dirname [info script]] .. .. .. .. fpga dfx build_linux prod config_rm_greybox.bit]]
}

set BIT_USERCODE 0x5263642C
