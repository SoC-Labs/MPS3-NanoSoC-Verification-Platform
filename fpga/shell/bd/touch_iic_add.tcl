# touch_iic_add.tcl -- Phase-2 CLCD TOUCH fabric addition.
#
# ============================ UNBUILT DRAFT ================================
# This has NOT been run through Vivado. It is a reviewable patch for the fabric
# re-mint wave (docs/planning/CLCD_APPS_PORTS_PAGE_PLAN.md §4 +
# docs/planning/CLCD_TOUCH_FABRIC_PATCH.md). Every step is validated only at the
# mint (validate_bd_design -> pr_verify -> timing -> re-key 9 overlays).
# ==========================================================================
#
# WHAT: adds a Xilinx AXI IIC master @ 0x44AE for the on-board resistive
# touch-screen controller (I2C TSC on CLCD_TSCL/TSDA), and routes the pen-down
# interrupt (CLCD_TINT) to the INTC concat as a POLLED pending bit (the platform
# has no ISRs; firmware reads INTC IPR bit 4). Pairs with shell_top.sv's
# `ifdef MPS3_SHELL_TOUCH ports/glue and fpga/shell/constraints/mps3_harness_touch.xdc.
#
# HOW IT IS INVOKED: shell_bd.tcl sources this ONLY when built with env
# SHELL_TOUCH=1, from the end of SECTION 6 (after the 0x44Ax address block,
# before VALIDATE). A default regen never sources it, so the then-shipped 0xCD74B6AE
# BD stays byte-identical. See CLCD_TOUCH_FABRIC_PATCH.md for the one-line hook.
#
# PREREQ vars in scope (all set by shell_bd.tcl before the source point, names
# confirmed against the current file): $shell_clk (clk_wiz_shell/clk_out1),
# $shell_aresetn (peripheral_aresetn), $axi_interconnect_0, $xlconcat_intr.
#
# ADDRESS: 0x44AE0000 IS TOUCH'S, AND THAT IS NOW DECIDED SOMEWHERE.
# This comment used to say the page CLASHED with the staged AXIJTAG/UART carry
# (axijtag_uart_carry.tcl + host/socket_harness/carry_across.py pinned axi_jtag
# at the same base) and left "move one of them" to whoever ran the mint. Two
# unbuilt drafts, one page, neither file able to see the other: free until both
# rode one mint, and then a mint.
#
# RESOLVED 2026-09-11 in TOUCH's favour, because the CONTRACT already said so --
# docs/contracts/shell-regmap.md v0.6 and docs/ARCHITECTURE.md's address map
# both carried a TOUCH row at 0x44AE_0000, a firmware driver and an XDC are
# written against it, and shell_bd.tcl already sources this file under
# SHELL_TOUCH=1, while the carry's claim lived only in scaffolding no build
# sources. The carry moved up one page, to 0x44AF/0x44B0/0x44B1.
#
# The page allocation is no longer restated here or anywhere else: it is
# tools/gen_regmap.py (this assign_bd_address line for the BD blocks,
# RESERVATIONS for the staged ones). That generator refuses to run if two owners
# land on one page, and tests/firmware_logic/test_regmap_conformance.py fails any
# tracked file that declares a base the map does not know -- so a THIRD claimant
# cannot land quietly the way the second one did.
#
# VALIDATE-AT-MINT checklist (do not skip):
#   * Confirm M19_ARESETN should be $shell_aresetn (matches M0..M18) vs the
#     interconnect bus reset $shell_bus_arstn.
#   * Confirm the AXI IIC 3-state pin names (scl_i/scl_o/scl_t, sda_*) for
#     axi_iic:2.1 (PG090) and the S_AXI/Reg segment name.

puts "touch_iic_add: UNBUILT DRAFT -- adding AXI IIC touch master (SHELL_TOUCH=1)"

# -- the AXI IIC master -----------------------------------------------------
set touch_iic_0 [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_iic:2.1 touch_iic_0]
set_property CONFIG.C_SCL_INERTIAL_DELAY {5} $touch_iic_0   ;# glitch filter (tune at bench)
connect_bd_net $shell_clk     [get_bd_pins touch_iic_0/s_axi_aclk]
connect_bd_net $shell_aresetn [get_bd_pins touch_iic_0/s_axi_aresetn]

# -- 3-state SCL/SDA + synchronized TINT brought out to shell_top ----------
#    (shell_top forms the open-drain I2C pads via IOBUFs and 2FF-syncs TINT).
foreach {p d} {iic_scl_i I iic_scl_o O iic_scl_t O \
               iic_sda_i I iic_sda_o O iic_sda_t O \
               clcd_tint_i I} {
    create_bd_port -dir $d $p
}
connect_bd_net [get_bd_ports iic_scl_i]        [get_bd_pins touch_iic_0/scl_i]
connect_bd_net [get_bd_pins  touch_iic_0/scl_o] [get_bd_ports iic_scl_o]
connect_bd_net [get_bd_pins  touch_iic_0/scl_t] [get_bd_ports iic_scl_t]
connect_bd_net [get_bd_ports iic_sda_i]        [get_bd_pins touch_iic_0/sda_i]
connect_bd_net [get_bd_pins  touch_iic_0/sda_o] [get_bd_ports iic_sda_o]
connect_bd_net [get_bd_pins  touch_iic_0/sda_t] [get_bd_ports iic_sda_t]

# -- one more interconnect master (M21 = touch_iic), mirroring shell_bd.tcl's
#    per-master ACLK/ARESETN wiring ---------------------------------------
#    M18 IS DUTEGR (the DUT Ethernet return path, 2026-09-11): this addition
#    used to take M18 and set NUM_MI 18 -> 19. Both numbers moved up one when
#    dut_egress_0 landed in the DEFAULT build; a gated addition has to follow
#    the default, not the other way round.
#
#    AND AGAIN, 2026-09-14: M19 = usr_access_rd_0 (USRACC @0x44B3_0000) and
#    M20 = axi_timebase_wdt_0 (WDOG @0x44B4_0000) landed in the DEFAULT build,
#    so this gated addition moves M19 -> M21 and NUM_MI 20 -> 22. This file is
#    sourced ONLY under SHELL_TOUCH=1, so a default regen never runs it and a
#    stale number here fails NOTHING until somebody builds with touch -- at
#    which point two blocks would be wired to M19 and one of them would simply
#    not be there. That is why the default build's lane owns this edit.
#    TOUCH's ADDRESS is untouched: 0x44AE_0000 is TOUCH's by contract
#    (tools/gen_regmap.py's RESERVATIONS note, cb45c18).
set_property CONFIG.NUM_MI {22} $axi_interconnect_0      ;# was 20 (M16 VPHY, M17 GENCHK, M18 DUTEGR, M19 USRACC, M20 WDOG)
connect_bd_net $shell_clk     [get_bd_pins $axi_interconnect_0/M21_ACLK]
connect_bd_net $shell_aresetn [get_bd_pins $axi_interconnect_0/M21_ARESETN]
connect_bd_intf_net [get_bd_intf_pins $axi_interconnect_0/M21_AXI] \
                    [get_bd_intf_pins touch_iic_0/S_AXI]
# This line IS the map: tools/gen_regmap.py reads the offset, the range and the
# `;# NAME` off it and emits MPS3_TOUCH_BASE + the TOUCH_IIC_* offsets into
# firmware/common/platform_regs.h, tests/common/regmap.py, the pyverify client
# and docs/contracts/shell-regmap.md. Move it and all five follow; nothing else
# in the tree gets to have an opinion about where TOUCH lives.
assign_bd_address -offset 0x44AE0000 -range 64K \
                  [get_bd_addr_segs {touch_iic_0/S_AXI/Reg}]     ;# TOUCH_IIC

# -- pen-down IRQ -> INTC concat as a POLLED pending bit (In4) --------------
#    firmware reads INTC IPR (0x41200000) bit 4; no CPU exception is enabled.
set_property CONFIG.NUM_PORTS {5} $xlconcat_intr         ;# was 4 (In0..In3 used)
connect_bd_net [get_bd_ports clcd_tint_i] [get_bd_pins $xlconcat_intr/In4]

puts "touch_iic_add: done -- AXI IIC @0x44AE0000 (M21), TINT -> concat In4. VALIDATE at mint."
