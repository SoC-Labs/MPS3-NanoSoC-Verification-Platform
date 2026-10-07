###-----------------------------------------------------------------------------
### fpga/shell/tools/shell_bd_guards.tcl -- post-validate guards for the shell
### BD, both CPU variants. READ-ONLY: nothing here may create, connect or set a
### property (tests/shell_cpu_seam's identity gate dumps the BD after these run,
### and a guard that edited the design would be invisible to it).
###
### WHY THESE, WHEN validate_bd_design ALREADY PASSED
### validate_bd_design is silent about the two failure shapes this platform has
### actually shipped or nearly shipped:
###   1. A dangling INPUT is auto-tied, with at most a CRITICAL WARNING, and a
###      tie is a legal design. src/linux_harness/shell_linux_bd.tcl left
###      dfx_ctl_0/ext_por_n_i and wdt_reset_i unconnected; ext_por_n_i tied 0
###      reads as "POR held forever", which holds decouple_en at 0, so the
###      decoupler clamp could NEVER assert -- and its own undriven-reset guard
###      could not see it, because those two pins are not typed as resets.
###      So: an explicit list of load-bearing wires, checked by NET MEMBERSHIP
###      (the pin and its intended driver are on one net), for both CPUs.
###   2. A parameter the IP silently refuses or propagation silently overrides.
###      The MicroBlaze V's C_INTERRUPT_WAKEUP defaults to 0, and at 0 Linux
###      hangs at its first `wfi` (silicon, 2026-07-16). So: read back what the
###      kernel depends on AFTER propagation, and refuse on a mismatch.
### Plus the classic undriven-reset-input sweep (the c0_ddr4_aresetn lesson),
### with interface members understood rather than whitelisted.
###
### Called by build_shell.tcl, validate_bd.tcl and tools/shell_bd_dump_run.tcl
### right after validate_bd_design. Which CPU is present is read off the BD
### (microblaze_0 vs microblaze_riscv_0), not the environment: the guard checks
### the design it was handed.
###-----------------------------------------------------------------------------

proc _sbg_fail {msg} {
    puts "############################################################"
    puts "## shell_bd_guards: $msg"
    puts "############################################################"
    error "shell_bd_guards: $msg"
}

# Is `want` (a /cell/pin or /port path) on the same net as `pin`?
proc _sbg_on_net {pin want} {
    set p [get_bd_pins -quiet $pin]
    if { $p eq "" } { _sbg_fail "pin $pin does not exist" }
    set n [get_bd_nets -quiet -of_objects $p]
    if { $n eq "" } { return 0 }
    set members [concat [get_bd_pins -quiet -of_objects $n] [get_bd_ports -quiet -of_objects $n]]
    return [expr {[lsearch -exact $members $want] >= 0}]
}

proc _sbg_cfg {cell kvs what} {
    set c [get_bd_cells -quiet $cell]
    if { $c eq "" } { _sbg_fail "$what: cell $cell does not exist" }
    set bad {}
    foreach {k want} $kvs {
        set got [get_property -quiet CONFIG.$k $c]
        set ok [string equal $got $want]
        if { !$ok && ![catch {expr {($got) == ($want)}} r] && $r } { set ok 1 }
        if { !$ok } { lappend bad "$cell CONFIG.$k = '$got', want '$want'" }
    }
    if { [llength $bad] } { _sbg_fail "$what: [join $bad {; }]" }
    puts "-- shell_bd_guards: $what: [expr {[llength $kvs]/2}] value(s) read back as required"
}

proc soclabs_shell_bd_post_validate {} {
    set mbv [expr {[get_bd_cells -quiet /microblaze_riscv_0] ne ""}]
    set mb  [expr {[get_bd_cells -quiet /microblaze_0] ne ""}]
    if { $mbv == $mb } {
        _sbg_fail "expected exactly one of microblaze_0 / microblaze_riscv_0 (mb=$mb mbv=$mbv)"
    }
    set wdt /axi_timebase_wdt_0/wdt_reset

    # ---- 1. load-bearing wires, by net membership -------------------------
    set wires [list \
        /dfx_ctl_0/ext_por_n_i            /sys_rst_n \
        /dut_clkrst_0/ext_por_n_i         /sys_rst_n \
        /dfx_ctl_0/wdt_reset_i            $wdt \
        /proc_sys_reset_shell/aux_reset_in $wdt \
        /dfx_decoupler_0/decouple         /dfx_ctl_0/decouple_en_o \
        /dfx_ctl_0/decoupled_i            /dfx_decoupler_0/decouple_status \
    ]
    # The RM's BSCAN hub stays on debug_bridge_0 master 0 in BOTH variants.
    # [SEAM-8] raises C_NUM_BS_MASTER to 2 in mbv AFTER shell_bd.tcl has wired
    # m0 to the rp_dbg_bscan_* partition pins; a parameter change that re-mapped
    # or dropped those nets would silently cut every RM ILA off from XVC.
    foreach leg {bscanid_en capture drck reset runtest sel shift tck tdi tms update} {
        lappend wires /debug_bridge_0/m0_bscan_$leg /rp_dbg_bscan_$leg
    }
    lappend wires /debug_bridge_0/m0_bscan_tdo /dfx_decoupler_0/s_dbg_bscan_tdo_DATA
    if { $mb } {
        lappend wires /microblaze_0/Reset /proc_sys_reset_shell/mb_reset
    } else {
        lappend wires \
            /microblaze_riscv_0/Reset          /proc_sys_reset_cpu/mb_reset \
            /proc_sys_reset_cpu/aux_reset_in   $wdt \
            /proc_sys_reset_ddr/aux_reset_in   $wdt \
            /smartconnect_ddr/aresetn          /proc_sys_reset_cpu/interconnect_aresetn \
            /ddr4_0/c0_ddr4_aresetn            /proc_sys_reset_ddr/peripheral_aresetn \
            /telem_0/alarm_i                   /ddr4_0/c0_init_calib_complete \
            /mig_dbg_hub/clk                   /clk_wiz_shell/clk_out1
    }
    set bad {}
    foreach {pin want} $wires {
        if { ![_sbg_on_net $pin $want] } { lappend bad "$pin is not driven by $want" }
    }
    if { [llength $bad] } { _sbg_fail "load-bearing wire(s) missing: [join $bad {; }]" }
    puts "-- shell_bd_guards: [expr {[llength $wires]/2}] load-bearing wires present (POR/WDOG -> dfx_ctl clamp, CPU reset domain)"

    # WDOG -> aux_reset_in polarity. C_AUX_RESET_HIGH is PROPAGATED from the
    # driver's POLARITY (wdt_reset is ACTIVE_HIGH -> 1); an unconnected aux pin
    # propagates 0. Read it back so a WDOG that would hold a CPU in reset
    # forever -- or never reset it -- cannot validate quietly.
    # The watchdog's window, per CPU: 27 (1.34 s / ~2.7 s) for the bare-metal
    # superloop, 31 (21.5 s / 42.9 s, the IP maximum) for Linux -- stage0 arms
    # it at the kernel hand-off ([SEAM-7]). Repeated enable, never once-only.
    _sbg_cfg axi_timebase_wdt_0 [list C_WDT_INTERVAL [expr {$mbv ? 31 : 27}] \
                                      WDT_ENABLE_ONCE Enable_repeatedly] \
        "axi_timebase_wdt_0 window ([expr {$mbv ? {mbv: Linux boot} : {mb: superloop}}])"

    set psr [list proc_sys_reset_shell]
    if { $mbv } { lappend psr proc_sys_reset_cpu proc_sys_reset_ddr }
    foreach c $psr {
        _sbg_cfg $c {C_AUX_RESET_HIGH 1} "$c aux_reset_in is the active-high WDOG"
    }

    # ---- 2. undriven reset-typed INPUTS ------------------------------------
    # A member pin of a CONNECTED interface is driven through that interface
    # (its own pin-level net is empty by construction) -- understood here, not
    # whitelisted. The whitelist is only what is dangling in the fielded shell
    # by design: debug-system-reset inputs (the MDM carries debug reset on its
    # bus) and the RMII domain's aux input (nothing but POR resets it).
    set white {proc_sys_reset_rmii/aux_reset_in}
    set undriven {}
    foreach c [get_bd_cells -quiet] {
        set via_intf {}
        foreach ip [get_bd_intf_pins -quiet -of_objects $c] {
            if { [get_bd_intf_nets -quiet -of_objects $ip] ne "" } {
                foreach mp [get_bd_pins -quiet -of_objects $ip] { lappend via_intf $mp }
            }
        }
        foreach p [get_bd_pins -quiet -of_objects $c -filter {DIR == I && TYPE == rst}] {
            set name "[get_property NAME $c]/[get_property NAME $p]"
            if { [string match */mb_debug_sys_rst $name] } { continue }
            if { [lsearch -exact $white $name] >= 0 } { continue }
            if { [lsearch -exact $via_intf $p] >= 0 } { continue }
            if { [get_bd_nets -quiet -of_objects $p] eq "" } { lappend undriven $name }
        }
    }
    if { [llength $undriven] } {
        _sbg_fail "reset INPUT(s) left dangling (auto-tied; ACTIVE_LOW => asserted forever): $undriven"
    }
    puts "-- shell_bd_guards: no undriven reset inputs"

    # ---- 2b. the debug bridge's BSCAN masters, per CPU ---------------------
    # mb: one master (the RM hub). mbv: two -- [SEAM-8]'s m1 feeds the static
    # mig_dbg_hub (mode 1, BSCAN -> Debug Hub) that the DDR4 MIG's always-present
    # XSDB calibration slave needs; without it DFX opt_design fails
    # (Chipscope 16-335). The hub must be on shell_clk, which runs whether or not
    # DDR4 calibrates.
    _sbg_cfg debug_bridge_0 [list C_DEBUG_MODE 2 C_NUM_BS_MASTER [expr {$mbv ? 2 : 1}]] \
        "debug_bridge_0 BSCAN masters ([expr {$mbv ? {mbv: RM hub + MIG hub} : {mb: RM hub}}])"
    if { $mbv } {
        _sbg_cfg mig_dbg_hub {C_DEBUG_MODE 1} "mig_dbg_hub (static BSCAN -> Debug Hub for the MIG)"
        set hubnet [get_bd_intf_nets -quiet -of_objects [get_bd_intf_pins -quiet /mig_dbg_hub/S_BSCAN]]
        set m1 [get_bd_intf_pins -quiet /debug_bridge_0/m1_bscan]
        if { $hubnet eq "" || [lsearch -exact [get_bd_intf_pins -quiet -of_objects $hubnet] $m1] < 0 } {
            _sbg_fail "mig_dbg_hub/S_BSCAN is not on debug_bridge_0/m1_bscan"
        }
        puts "-- shell_bd_guards: mig_dbg_hub hangs off debug_bridge_0/m1_bscan"
    } elseif { [get_bd_cells -quiet /mig_dbg_hub] ne "" } {
        _sbg_fail "mig_dbg_hub exists in the bare-metal BD (it is \[SEAM-8\], mbv only)"
    }

    # ---- 3. the MicroBlaze V's Linux contract, AFTER propagation -----------
    if { $mbv } {
        # [FIX-A] of the July fork, kept: COUNTERS=1 is the clocksource (rdtime),
        # INTERRUPT_WAKEUP=1 is the silicon-proven wfi-coma fix (E3), SSTC=1 is a
        # latent capability the DTS does not advertise (errata E1/E2).
        _sbg_cfg microblaze_riscv_0 {
            C_USE_COUNTERS 1  C_USE_SSTC 1  C_INTERRUPT_WAKEUP 1
            C_USE_MMU 3  C_USE_ICACHE 1  C_USE_DCACHE 1  C_BASE_VECTORS 0x0000000000000000
        } "microblaze_riscv_0 Linux contract (post-propagation)"
        # [FIX-C]: kind_of_intr must survive propagation (uartlite's pin is
        # metadata'd EDGE; the manual all-level word is correct, LVL_P latches).
        _sbg_cfg axi_intc_0 {C_KIND_OF_INTR 0x00000000 C_IRQ_IS_LEVEL 1 C_IRQ_ACTIVE 0x1 C_HAS_FAST 0} \
            "axi_intc_0 Linux irq-xilinx-intc contract (post-propagation)"
        set nin [expr {[get_bd_cells -quiet /touch_iic_0] ne "" ? 5 : 4}]
        _sbg_cfg axi_intc_0 [list C_NUM_INTR_INPUTS $nin] "axi_intc_0 input count (read-only, from xlconcat)"
        _sbg_cfg axi_timer_0 {enable_timer2 1} "axi_timer_0 both counters (soclabs,mbv-timer)"
        # [SEAM-6] halt-free JTAG reads: the MDM's debug LMB + AXI masters.
        _sbg_cfg mdm_riscv_0 {C_DBG_MEM_ACCESS 1} "mdm_riscv_0 debug memory access"
        _sbg_cfg dlmb_bram_if_cntlr {C_NUM_LMB 2} "dlmb_bram_if_cntlr second port for the MDM"
        foreach {sp want} {/mdm_riscv_0/Data 0x0001FF00 /mdm_riscv_0/Data 0x80000000} {
            set hit 0
            foreach seg [get_bd_addr_segs -quiet -of_objects [get_bd_addr_spaces -quiet $sp]] {
                set o [get_property -quiet OFFSET $seg]; set r [get_property -quiet RANGE $seg]
                if { $o ne "" && $want >= $o && $want < $o + $r } { set hit 1 }
            }
            if { !$hit } { _sbg_fail "$sp cannot reach $want (the MDM's debug LMB/AXI path is not mapped)" }
        }
        puts "-- shell_bd_guards: the MDM reaches the diag mailbox (0x0001FF00) and DDR without the hart"

        # The kernel's memory map, read off the MBV Data space.
        set want [list 0x00000000 0x00020000 LMB 0x80000000 0x40000000 DDR4 \
                       0xC0000000 0x01000000 EMC 0x41200000 0x00010000 INTC \
                       0x41C00000 0x00010000 TIMER 0x40600000 0x00010000 UARTLITE]
        set have {}
        foreach s [get_bd_addr_segs -quiet -of_objects [get_bd_addr_spaces -quiet /microblaze_riscv_0/Data]] {
            set o [get_property -quiet OFFSET $s]
            set r [get_property -quiet RANGE $s]
            if { $o ne "" } { lappend have [format "0x%08X/0x%08X" $o $r] }
        }
        set miss {}
        foreach {o r n} $want {
            if { [lsearch -exact $have [format "0x%08X/0x%08X" $o $r]] < 0 } { lappend miss "$n@$o/$r" }
        }
        if { [llength $miss] } { _sbg_fail "microblaze_riscv_0/Data is missing: $miss (have: $have)" }
        puts "-- shell_bd_guards: MBV Data space carries LMB/DDR4/EMC/INTC/TIMER/UARTLITE at the contract addresses"
    }
    puts "SHELL_BD_GUARDS_OK cpu=[expr {$mbv ? {mbv} : {mb}}]"
}
