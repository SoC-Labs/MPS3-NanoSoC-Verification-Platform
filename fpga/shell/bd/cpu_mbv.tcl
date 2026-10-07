###-----------------------------------------------------------------------------
### fpga/shell/bd/cpu_mbv.tcl -- the MicroBlaze V half of the shell's CPU seam
### (SHELL_CPU=mbv): a Linux-capable RV32 core with an Sv32 MMU, 1 GiB of DDR4,
### and a 128 KiB LMB for stage0. Vivado 2026.1 ONLY.
###
### WHERE THIS CAME FROM. The MicroBlaze V subsystem of the July Linux fork
### (src/linux_harness/shell_linux_bd.tcl, SECTIONS 2-4/7/8, which booted Linux,
### SSH and six ICAP swaps on this board as static 0x2B082E1B) and the PoC it
### was built from (src/linux_soc/hw/mbv_soc.tcl + ddr4_ip.tcl). Their CPU,
### cache, INTC, timer, DDR4 and SmartConnect configuration is carried VERBATIM
### and their lessons keep their tags ([MBV-LESSON], [FIX-A]/[FIX-C], errata
### E1-E3). What is NOT carried is everything else in the fork: every non-CPU
### block now comes from shell_bd.tcl, so this variant has the POR/WDOG
### decoupler clamp, dut_eth_irq, VPHY/GENCHK, DUTEGR, USRACC, WDOG, TOUCH,
### dbgbscan, the MCC tie-offs and REALPHY by construction, and cannot drift
### from the bare-metal shell again.
###
### WHAT DELIBERATELY DIFFERS FROM THE JULY FORK (each is SHELL_CONTRACT.md):
###   [SEAM-1] The CPU is NOT held in reset on DDR4 calibration. The fork
###            ([DEV-4]) gated proc_sys_reset_cpu on c0_init_calib_complete,
###            which made a calibration failure indistinguishable from a dead
###            CPU -- no banner, no console, nothing. stage0 now runs first and
###            READS calibration (TELEM STATUS[0], [SEAM-3]); on failure it says
###            so on the console and falls to rescue without touching DDR.
###   [SEAM-2] The WDOG restarts the MBV. The fork's aux_reset_in carried calib
###            and nothing could reset the CPU short of POR. Here the watchdog's
###            wdt_reset resets the CPU + LMB (proc_sys_reset_cpu) AND the whole
###            DDR AXI path -- SmartConnect and the MIG's AXI shim -- together
###            (proc_sys_reset_ddr for the shim), so no stale AXI response can
###            cross a reset boundary into a freshly reset core. It never resets
###            the MIG PHY: calibration survives a watchdog reset, sys_rst stays
###            POR-only. stage0 re-runs from 0x0 (BRAM contents persist).
###   [SEAM-3] c0_init_calib_complete drives telem_0/alarm_i, an input that is
###            tied 0 in the bare-metal shell (MPS3 has no INA228) and that
###            telem.sv already 2-FF synchronises. No RTL change, no new cell,
###            no new page. Read: TELEM CTRL[1]=1, then STATUS[0].
###   [SEAM-4] No calib LED on USER_nLED[0] (the fork's [LINUX-DEV-2]): the LEDs
###            stay board_gpio's in both variants; the calib state is readable
###            from software and over JTAG instead.
###   [SEAM-6] Debug memory access ON (mdm_riscv C_DBG_MEM_ACCESS=1): the MDM
###            reads the LMB (the diag mailbox) through the dlmb controller's
###            second port and DDR through smartconnect_ddr's third port -- over
###            JTAG, with Linux running and the hart NOT halted (plan B1 item 8).
###   [SEAM-7] WDOG C_WDT_INTERVAL 31 (the IP maximum), not the bare-metal 27:
###            stage0 arms it at the Linux hand-off, and it must outlast a
###            kernel boot before harnessd starts kicking (reset 42.9 s after
###            arming; SHELL_CONTRACT.md §6).
###   [SEAM-5] Cell names match the classic variant wherever the role matches
###            (ilmb_v10, dlmb_v10, *_bram_if_cntlr, local_ram), so FLOW's MMI/
###            updatemem tooling differs only in the -proc path.
###
### STAGES: see cpu_mb.tcl's header; same contract, same scope rules. The
### 2026.1 catalogue matches the 2024.1 interface names case-INSENSITIVELY
### (probed 2026-09-23: axi_intc_0/S_AXI resolves to /axi_intc_0/s_axi), which is
### why shell_bd.tcl's shared lines need no per-version spelling.
###-----------------------------------------------------------------------------

# Read-back assertion (the [MBV-LESSON] rule, from mbv_soc.tcl): IP xguis
# SILENTLY refuse illegal values, so every Linux-critical parameter is read back
# and a mismatch is fatal. Numeric compare as a fallback (C_BASE_VECTORS reads
# back as 0x0000000000000000 at 2026.1).
if { [info procs ::soclabs_mbv_assert_cfg] eq "" } {
    proc ::soclabs_mbv_assert_cfg {cell cfg_list what} {
        set bad {}
        foreach {k want} $cfg_list {
            set got [get_property $k $cell]
            set eq [string equal $got $want]
            if { !$eq && ![catch {expr {($got) == ($want)}} r] && $r } { set eq 1 }
            if { !$eq } { lappend bad [format "%-26s requested=%-14s read-back=%s" $k $want $got] }
        }
        if { [llength $bad] } {
            foreach b $bad { puts "## $what -- READ-BACK MISMATCH: $b" }
            error "cpu_mbv.tcl: $what read-back mismatch: [join $bad {; }]"
        }
        puts "-- cpu_mbv.tcl: $what: [expr {[llength $cfg_list]/2}] parameters read back as requested"
    }
}

switch -exact -- $cpu_stage {

core {
    # -- Tool guard: the MicroBlaze V with an Sv32 MMU and the S-mode/SSTC IP
    #    revisions is a 2026.1 flow ("VERSION TRAP": a 2026.1 checkpoint cannot
    #    be opened by 2024.1, so the whole MBV static and its overlays are
    #    2026.1). build_shell.tcl refuses earlier; this is the backstop.
    if { [string match "2024.*" [version -short]] || [string match "2025.1*" [version -short]] } {
        error "cpu_mbv.tcl: SHELL_CPU=mbv needs Vivado 2026.1 (this is [version -short])"
    }

    # -- DDR4 MIG (the July-proven configuration, folded) --------------------
    source [file join $::SHELL_BD_DIR ddr4_mig.tcl]
    set ddr4_0 [soclabs_ddr4_mig_create ddr4_0]
    set ddr_ui_clk [get_bd_pins ddr4_0/c0_ddr4_ui_clk]          ;# 200 MHz, BUFG'd in the IP
    set ddr_ui_rst [get_bd_pins ddr4_0/c0_ddr4_ui_clk_sync_rst] ;# active-HIGH, ui_clk domain
    set ddr_calib  [get_bd_pins ddr4_0/c0_init_calib_complete]  ;# active-HIGH "DDR ready"

    # OSC6 100 MHz differential reference (pads in constraints/mbv/ddr4_pins.xdc:
    # SLR1 banks 49-51, clear of the RP pblock). Scalar ports + member-pin nets,
    # exactly as the fork built and routed them; the wrapper names they produce
    # (c0_sys_clk_p/n) are what shell_top and the XDC use.
    create_bd_port -dir I c0_sys_clk_p
    create_bd_port -dir I c0_sys_clk_n
    connect_bd_net [get_bd_ports c0_sys_clk_p] [get_bd_pins ddr4_0/c0_sys_clk_p]
    connect_bd_net [get_bd_ports c0_sys_clk_n] [get_bd_pins ddr4_0/c0_sys_clk_n]

    # MIG system reset: [MBV-LESSON] THE POLARITY TRAP. USER_nPB0 idles HIGH;
    # ddr4 sys_rst is ACTIVE-HIGH with no polarity knob. Straight-through wiring
    # holds the MIG in reset forever (dark board). POR only -- never the WDOG
    # ([SEAM-2]): recalibrating on every watchdog reset would buy nothing.
    set ddr_sys_rst_inv [create_bd_cell -type ip -vlnv xilinx.com:ip:util_vector_logic:2.0 ddr_sys_rst_inv]
    set inv_cfg [list CONFIG.C_SIZE {1} CONFIG.C_OPERATION {not}]
    set_property -dict $inv_cfg $ddr_sys_rst_inv
    soclabs_mbv_assert_cfg $ddr_sys_rst_inv $inv_cfg "ddr_sys_rst_inv (active-LOW pad -> active-HIGH MIG sys_rst)"
    connect_bd_net [get_bd_ports sys_rst_n]           [get_bd_pins $ddr_sys_rst_inv/Op1]
    connect_bd_net [get_bd_pins $ddr_sys_rst_inv/Res] [get_bd_pins ddr4_0/sys_rst]

    # -- CPU reset domain ([SEAM-1], [SEAM-2]) -------------------------------
    # POR + MMCM lock release it; the WDOG (aux_reset_in, wired in `finish` once
    # axi_timebase_wdt_0 exists) re-asserts it. NOT calib-gated.
    # C_AUX_RESET_HIGH is PROPAGATED from the driver's POLARITY (wdt_reset is
    # ACTIVE_HIGH -> 1); tools/shell_bd_guards.tcl reads it back post-validate.
    set proc_sys_reset_cpu [create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 proc_sys_reset_cpu]
    connect_bd_net $shell_clk                          [get_bd_pins $proc_sys_reset_cpu/slowest_sync_clk]
    connect_bd_net [get_bd_pins $clk_wiz_shell/locked] [get_bd_pins $proc_sys_reset_cpu/dcm_locked]
    connect_bd_net [get_bd_ports sys_rst_n]            [get_bd_pins $proc_sys_reset_cpu/ext_reset_in]
    set cpu_mb_reset [get_bd_pins $proc_sys_reset_cpu/mb_reset]              ;# active-HIGH -> MBV + LMB
    set cpu_ic_arstn [get_bd_pins $proc_sys_reset_cpu/interconnect_aresetn]  ;# -> smartconnect_ddr
    # mb_debug_sys_rst deliberately unconnected: mdm_riscv carries debug reset
    # on its MBDEBUG bus ([MBV-LESSON]; whitelisted by the reset guard).

    # -- DDR AXI-shim reset domain (ui_clk) -----------------------------------
    # [MBV-LESSON] c0_ddr4_aresetn is an active-LOW INPUT in the ui_clk domain;
    # dangling = tied 0 = the AXI shim in reset forever while calibration says
    # OK. July drove it with ~ui_clk_sync_rst. Here a ui_clk proc_sys_reset does
    # the same job (dcm_locked = ~ui_clk_sync_rst, so it releases no earlier
    # than July's inverter did) and ALSO takes the WDOG ([SEAM-2]), so the shim
    # and the SmartConnect in front of it reset together with the CPU.
    set ddr_ui_rst_inv [create_bd_cell -type ip -vlnv xilinx.com:ip:util_vector_logic:2.0 ddr_ui_rst_inv]
    set_property -dict $inv_cfg $ddr_ui_rst_inv
    soclabs_mbv_assert_cfg $ddr_ui_rst_inv $inv_cfg "ddr_ui_rst_inv (ui_clk_sync_rst -> 'ui domain up')"
    connect_bd_net $ddr_ui_rst [get_bd_pins $ddr_ui_rst_inv/Op1]
    set proc_sys_reset_ddr [create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 proc_sys_reset_ddr]
    connect_bd_net $ddr_ui_clk                         [get_bd_pins $proc_sys_reset_ddr/slowest_sync_clk]
    connect_bd_net [get_bd_pins $ddr_ui_rst_inv/Res]   [get_bd_pins $proc_sys_reset_ddr/dcm_locked]
    connect_bd_net [get_bd_ports sys_rst_n]            [get_bd_pins $proc_sys_reset_ddr/ext_reset_in]
    connect_bd_net [get_bd_pins $proc_sys_reset_ddr/peripheral_aresetn] [get_bd_pins ddr4_0/c0_ddr4_aresetn]

    # -- MicroBlaze V: core config VERBATIM from the flown fork/mbv_soc.tcl
    #    (rv32imac + Zba/Zbb/Zbs, Sv32, caches over the DDR aperture, soft-float).
    #    C_OPTIMIZATION=0 is a FINDING: 2 (FREQUENCY) is illegal with C_USE_MMU=3.
    #    No Zicbom parameter exists: the DTS isa is rv32imac_zba_zbb_zbs. --------
    set microblaze_riscv_0 [create_bd_cell -type ip -vlnv xilinx.com:ip:microblaze_riscv:1.0 microblaze_riscv_0]
    set mbv_cfg [list \
        CONFIG.C_USE_MMU          {3} \
        CONFIG.C_ADDR_SIZE        {32} \
        CONFIG.C_DATA_SIZE        {32} \
        CONFIG.C_OPTIMIZATION     {0} \
        CONFIG.C_BASE_VECTORS     {0x00000000} \
        CONFIG.C_USE_MULDIV       {1} \
        CONFIG.C_USE_ATOMIC       {1} \
        CONFIG.C_USE_COMPRESSION  {1} \
        CONFIG.C_USE_BITMAN_A     {1} \
        CONFIG.C_USE_BITMAN_B     {1} \
        CONFIG.C_USE_BITMAN_S     {1} \
        CONFIG.C_USE_COUNTERS     {1} \
        CONFIG.C_USE_SSTC         {1} \
        CONFIG.C_USE_BARREL       {1} \
        CONFIG.C_USE_FPU          {0} \
        CONFIG.C_USE_ICACHE       {1} \
        CONFIG.C_USE_DCACHE       {1} \
        CONFIG.C_ICACHE_BYTE_SIZE {8192} \
        CONFIG.C_DCACHE_BYTE_SIZE {8192} \
        CONFIG.C_ICACHE_BASEADDR  {0x80000000} \
        CONFIG.C_ICACHE_HIGHADDR  {0xBFFFFFFF} \
        CONFIG.C_DCACHE_BASEADDR  {0x80000000} \
        CONFIG.C_DCACHE_HIGHADDR  {0xBFFFFFFF} \
        CONFIG.C_USE_INTERRUPT    {1} \
        CONFIG.C_INTERRUPT_WAKEUP {1} \
        CONFIG.C_DEBUG_ENABLED    {1} \
        CONFIG.C_INTERCONNECT     {2} \
        CONFIG.C_D_AXI            {1} \
        CONFIG.C_I_LMB            {1} \
        CONFIG.C_D_LMB            {1} \
    ]
    set_property -dict $mbv_cfg $microblaze_riscv_0
    soclabs_mbv_assert_cfg $microblaze_riscv_0 $mbv_cfg "microblaze_riscv_0 (Linux-capable RV32 core)"
    # [FIX-A] the three Linux-load-bearing values ride the assert above AND are
    # re-read after propagation by tools/shell_bd_guards.tcl:
    #   C_USE_COUNTERS 1     Zicntr: `time` is the kernel's clocksource.
    #   C_USE_SSTC 1         LATENT only. Silicon errata E1 (stimecmp never
    #                        raises a deliverable S-mode interrupt) and E2
    #                        (mip.STIP stuck once set): the DEPLOYED DTS drops
    #                        "sstc" and the tick is axi_timer_0 on SEIP via the
    #                        "soclabs,mbv-timer" driver (linux patch 0003).
    #   C_INTERRUPT_WAKEUP 1 E3, the WFI-COMA FIX, PROVEN ON SILICON 2026-07-16:
    #                        the IP default 0 means a hart in `wfi` never wakes;
    #                        Linux idles in wfi and hangs at first idle. The
    #                        derived C_USE_SLEEP=1 / C_ASYNC_WAKEUP=3 are AMD's
    #                        own values; the Sleep/Wakeup pins stay unwired.

    # Debug: mdm_riscv in JTAG-BSCAN mode. Coexists with debug_bridge_0, whose
    # BSCAN is the soft one inside the IP (C_DEBUG_MODE 2), as mdm_1 does in the
    # classic variant.
    #
    # [SEAM-6] DEBUG MEMORY ACCESS ON (C_DBG_MEM_ACCESS=1; July had it off).
    # The diag mailbox lives in the LMB, and the plan's B1 item 8 reads it over
    # JTAG WHILE LINUX RUNS -- no halt, because halting the one hart under a
    # live kernel is the intrusion the mailbox exists to avoid. The LMB is on no
    # AXI bus, so the MDM needs its own path INTO it: with debug memory access
    # the MDM grows an LMB master (LMB_0), which takes the dlmb controller's
    # second port (C_NUM_LMB 2, SLMB1, below), and an AXI master (M_AXI), which
    # takes smartconnect_ddr's third port in the `axi` stage -- so xsdb can also
    # read DDR (kernel log, ramoops) without touching the hart. The CSR blocks
    # are NOT on the debug path (see the `axi` stage for why).
    set mdm_riscv_0 [create_bd_cell -type ip -vlnv xilinx.com:ip:mdm_riscv:1.0 mdm_riscv_0]
    set mdm_cfg [list CONFIG.C_DBG_MEM_ACCESS {1}]
    set_property -dict $mdm_cfg $mdm_riscv_0
    soclabs_mbv_assert_cfg $mdm_riscv_0 $mdm_cfg "mdm_riscv_0 (debug memory access: LMB_0 + M_AXI)"
    connect_bd_intf_net [get_bd_intf_pins $microblaze_riscv_0/DEBUG] [get_bd_intf_pins $mdm_riscv_0/MBDEBUG_0]
    connect_bd_net $shell_clk     [get_bd_pins $mdm_riscv_0/M_AXI_ACLK]
    connect_bd_net $shell_aresetn [get_bd_pins $mdm_riscv_0/M_AXI_ARESETN]

    # -- LMB: 128 KiB shared ILMB/DLMB BRAM at the reset vector. Layout (plan
    #    §3, SHELL_CONTRACT.md §3): stage0 0x00000-0x1FDFF, stage0 status block
    #    0x1FE00, diag mailbox v8 0x1FF00 (= 128 KiB - 0x100). Linux never runs
    #    from here; stage0 is baked by updatemem (FLOW mint-stage0). The DLMB
    #    also serves Linux's UIO view of the mailbox (the CPU decodes it). --
    set ilmb_v10 [create_bd_cell -type ip -vlnv xilinx.com:ip:lmb_v10:3.0 ilmb_v10]
    set dlmb_v10 [create_bd_cell -type ip -vlnv xilinx.com:ip:lmb_v10:3.0 dlmb_v10]
    set ilmb_bram_if_cntlr [create_bd_cell -type ip -vlnv xilinx.com:ip:lmb_bram_if_cntlr:4.0 ilmb_bram_if_cntlr]
    set dlmb_bram_if_cntlr [create_bd_cell -type ip -vlnv xilinx.com:ip:lmb_bram_if_cntlr:4.0 dlmb_bram_if_cntlr]
    set_property CONFIG.C_ECC {0} $ilmb_bram_if_cntlr
    set_property CONFIG.C_ECC {0} $dlmb_bram_if_cntlr
    # [SEAM-6] the data-side controller takes a second LMB port for the MDM.
    set dlmb_cfg [list CONFIG.C_NUM_LMB {2}]
    set_property -dict $dlmb_cfg $dlmb_bram_if_cntlr
    soclabs_mbv_assert_cfg $dlmb_bram_if_cntlr $dlmb_cfg "dlmb_bram_if_cntlr (CPU DLMB + MDM debug LMB)"
    set local_ram [create_bd_cell -type ip -vlnv xilinx.com:ip:blk_mem_gen:8.4 local_ram]
    set_property -dict [list \
        CONFIG.Memory_Type {True_Dual_Port_RAM} \
        CONFIG.Use_Byte_Write_Enable {true} \
        CONFIG.Byte_Size {9} \
        CONFIG.Write_Width_A {32} \
        CONFIG.Write_Depth_A {32768} \
        CONFIG.Write_Width_B {32} \
        CONFIG.Enable_B {Use_ENB_Pin} \
        CONFIG.Register_PortA_Output_of_Memory_Primitives {false} \
        CONFIG.Register_PortB_Output_of_Memory_Primitives {false} \
        CONFIG.Use_RSTA_Pin {true} \
        CONFIG.Use_RSTB_Pin {true} \
    ] $local_ram
    # 32768 x 32b = 128 KiB. Moves in lockstep with the `addr` stage's -range
    # 128K, STAGE0's linker script (image below 0x1FE00) and HOST's mailbox
    # anchor (lmb_kb=128 -> 0x1FF00). Nothing else may assume a size.

    connect_bd_intf_net [get_bd_intf_pins $microblaze_riscv_0/ILMB] [get_bd_intf_pins $ilmb_v10/LMB_M]
    connect_bd_intf_net [get_bd_intf_pins $microblaze_riscv_0/DLMB] [get_bd_intf_pins $dlmb_v10/LMB_M]
    connect_bd_intf_net [get_bd_intf_pins $ilmb_v10/LMB_Sl_0] [get_bd_intf_pins $ilmb_bram_if_cntlr/SLMB]
    connect_bd_intf_net [get_bd_intf_pins $dlmb_v10/LMB_Sl_0] [get_bd_intf_pins $dlmb_bram_if_cntlr/SLMB]
    connect_bd_intf_net [get_bd_intf_pins $mdm_riscv_0/LMB_0]  [get_bd_intf_pins $dlmb_bram_if_cntlr/SLMB1]
    connect_bd_intf_net [get_bd_intf_pins $ilmb_bram_if_cntlr/BRAM_PORT] [get_bd_intf_pins $local_ram/BRAM_PORTA]
    connect_bd_intf_net [get_bd_intf_pins $dlmb_bram_if_cntlr/BRAM_PORT] [get_bd_intf_pins $local_ram/BRAM_PORTB]

    # CPU + LMB on shell_clk (the fork's [DEV-3]: the 20-slave shell fabric
    # keeps its 100 MHz; smartconnect_ddr does the 100<->200 crossing) and the
    # CPU domain's ACTIVE-HIGH mb_reset (LMB resets are active-high -- feeding
    # them a peripheral_aresetn is the [BD 41-238] polarity bug).
    connect_bd_net $shell_clk    [get_bd_pins $microblaze_riscv_0/Clk]
    connect_bd_net $cpu_mb_reset [get_bd_pins $microblaze_riscv_0/Reset]
    foreach p [list $ilmb_v10 $dlmb_v10 $ilmb_bram_if_cntlr $dlmb_bram_if_cntlr] {
        connect_bd_net $shell_clk    [get_bd_pins $p/LMB_Clk] -quiet
        connect_bd_net $cpu_mb_reset [get_bd_pins $p/SYS_Rst] -quiet
        connect_bd_net $cpu_mb_reset [get_bd_pins $p/LMB_Rst] -quiet
    }
}

intc {
    # [DEV-5]/[FIX-C] the Linux irq-xilinx-intc contract: all inputs LEVEL,
    # active-high, no fast interrupts. The uartlite's pin is metadata'd
    # EDGE_RISING, so propagation may WARN that the manual C_KIND_OF_INTR
    # differs from the computed word: expected and benign (axi_intc's LVL_P
    # detector latches a one-cycle pulse until IAR ack; linux_soc
    # IRQ_WIRING_AUDIT.md §2). The DTS `xlnx,kind-of-intr = <0x0>` must match;
    # tools/shell_bd_guards.tcl re-reads this word after propagation.
    set intc_cfg [list \
        CONFIG.C_KIND_OF_INTR {0x00000000} \
        CONFIG.C_IRQ_IS_LEVEL {1} \
        CONFIG.C_IRQ_ACTIVE   {0x1} \
        CONFIG.C_HAS_FAST     {0} \
    ]
    set_property -dict $intc_cfg $axi_intc_0
    soclabs_mbv_assert_cfg $axi_intc_0 $intc_cfg "axi_intc_0 (Linux: level, active-high, no fast irq)"
    # INTERRUPT is a BUS INTERFACE on microblaze_riscv (mbinterrupt_rtl), and
    # axi_intc's `interrupt` carries the same VLNV ([MBV-LESSON]).
    connect_bd_intf_net [get_bd_intf_pins $axi_intc_0/interrupt] [get_bd_intf_pins $microblaze_riscv_0/INTERRUPT]
}

timer {
    # [DEV-6] both counters pinned: channel 0 is the kernel clockevent through
    # the "soclabs,mbv-timer" errata driver (E1/E2), delivered on SEIP via INTC
    # In1, which must stay LEVEL. Channel 1 is spare. Same values the bare-metal
    # shell takes by default -- pinned here because Linux depends on them.
    set timer_cfg [list CONFIG.enable_timer2 {1} CONFIG.COUNT_WIDTH {32} CONFIG.mode_64bit {0}]
    set_property -dict $timer_cfg $axi_timer_0
    soclabs_mbv_assert_cfg $axi_timer_0 $timer_cfg "axi_timer_0 (kernel clockevent, both counters)"
    # The console: read back only (shared code sets the baud; these are the
    # IP defaults and the DTS/getty assume them).
    soclabs_mbv_assert_cfg $axi_uartlite_0 [list CONFIG.C_BAUDRATE 115200 CONFIG.C_DATA_BITS 8 CONFIG.C_USE_PARITY 0] \
        "axi_uartlite_0 (kernel console ttyUL0, 115200 8N1)"
}

axi {
    # Peripheral master (uncached, everything outside the DDR aperture) onto
    # the shared shell fabric -- the same S00 the classic core uses.
    connect_bd_intf_net [get_bd_intf_pins $microblaze_riscv_0/M_AXI_DP] [get_bd_intf_pins $axi_interconnect_0/S00_AXI]

    # [MBV-LESSON] VERBATIM: both cache masters (M_AXI_DC AND M_AXI_IC) reach
    # DDR4 through ONE dual-clock SmartConnect -- 100 MHz SI side, 200 MHz
    # ui_clk MI side, async crossing + 32b->512b upsizing inside. NUM_SI=2 is
    # the fix: an I-cache that cannot reach DDR fetches nothing from Linux.
    set smartconnect_ddr [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:1.0 smartconnect_ddr]
    set sc_cfg [list CONFIG.NUM_SI {3} CONFIG.NUM_MI {1} CONFIG.NUM_CLKS {2}]
    set_property -dict $sc_cfg $smartconnect_ddr
    soclabs_mbv_assert_cfg $smartconnect_ddr $sc_cfg "smartconnect_ddr (3 SI: DC, IC, MDM debug; dual-clock 100<->200)"
    connect_bd_net $shell_clk    [get_bd_pins $smartconnect_ddr/aclk]    ;# cache-master side
    connect_bd_net $ddr_ui_clk   [get_bd_pins $smartconnect_ddr/aclk1]   ;# ui_clk DDR side
    connect_bd_net $cpu_ic_arstn [get_bd_pins $smartconnect_ddr/aresetn] ;# CPU domain: WDOG-reset with the core
    connect_bd_intf_net [get_bd_intf_pins $microblaze_riscv_0/M_AXI_DC] [get_bd_intf_pins $smartconnect_ddr/S00_AXI]
    connect_bd_intf_net [get_bd_intf_pins $microblaze_riscv_0/M_AXI_IC] [get_bd_intf_pins $smartconnect_ddr/S01_AXI]
    connect_bd_intf_net [get_bd_intf_pins $smartconnect_ddr/M00_AXI]    [get_bd_intf_pins ddr4_0/C0_DDR4_S_AXI]
    # [SEAM-6] the MDM's debug AXI master takes the THIRD SmartConnect port:
    # halt-free JTAG reads of DDR (the kernel log, ramoops at the top of RAM)
    # when Linux is wedged and SSH is gone -- the case a debugger is for. It is
    # NOT on the peripheral fabric: axi_interconnect_0 clips NUM_MI to 16 as
    # soon as it gets a second slave interface (found at validate, 2026-09-23),
    # and the shell needs 21-22. CSR reads with Linux running go through
    # harnessd/SSH; a debug read of a CSR address gets a clean DECERR, not a hang.
    connect_bd_intf_net [get_bd_intf_pins $mdm_riscv_0/M_AXI] [get_bd_intf_pins $smartconnect_ddr/S02_AXI]

    # DDR4 physical interface out to the board. [MBV-LESSON] CRITICAL RENAME:
    # the auto name C0_DDR4_0 makes the wrapper emit C0_DDR4_0_* ports that
    # silently miss every lowercase c0_ddr4_* PACKAGE_PIN in ddr4_pins.xdc
    # ("No ports matched" -> unconstrained memory pins, a clean-looking dead
    # build). Rename so the wrapper ports come out c0_ddr4_*.
    set ddr_phy [get_bd_intf_pins -quiet ddr4_0/C0_DDR4]
    if { $ddr_phy eq "" } { error "cpu_mbv.tcl: ddr4_0/C0_DDR4 physical interface not found" }
    make_bd_intf_pins_external $ddr_phy
    set ddr_ext [get_bd_intf_ports -quiet C0_DDR4_0]
    if { $ddr_ext eq "" } {
        error "cpu_mbv.tcl: expected external port C0_DDR4_0 after make_bd_intf_pins_external; got [get_bd_intf_ports]"
    }
    set_property name c0_ddr4 $ddr_ext
}

addr {
    # LMB: 128 KiB at the reset vector, the same BRAM on both LMBs (TDP:
    # ilmb->PORTA, dlmb->PORTB) -- must match local_ram's 32768 x 32b.
    assign_bd_address -offset 0x00000000 -range 128K [get_bd_addr_segs {ilmb_bram_if_cntlr/SLMB/Mem}]
    assign_bd_address -offset 0x00000000 -range 128K [get_bd_addr_segs {dlmb_bram_if_cntlr/SLMB/Mem}]
    # [SEAM-6] the same BRAM, seen by the MDM's debug LMB (xsdb mrd 0x1FF00).
    assign_bd_address -offset 0x00000000 -range 128K [get_bd_addr_segs {dlmb_bram_if_cntlr/SLMB1/Mem}]
    # DDR4: 1 GiB at 0x8000_0000 = rv32 Linux lowmem; == the cache aperture
    # above (C_*CACHE_BASEADDR/HIGHADDR). The segment lives on the IP's memory
    # map, not on the AXI pin.
    assign_bd_address -offset 0x80000000 -range 1G [get_bd_addr_segs {ddr4_0/C0_DDR4_MEMORY_MAP/C0_DDR4_ADDRESS_BLOCK}]
    # The shared housekeeping lines that follow (timer 0x41C0, uartlite 0x4060,
    # intc 0x4120, EMC 0xC000) apply to this Data space unchanged.
}

finish {
    # [SEAM-7] THE WATCHDOG WINDOW IS A LINUX BOOT, NOT A SUPERLOOP PASS.
    # The bare-metal shell sets C_WDT_INTERVAL 27 (first expiry 2^27 cycles =
    # 1.34 s, reset ~2.7 s) because its superloop kicks every pass from the
    # first instruction. Under Linux, stage0 arms the WDOG at the hand-off
    # (STAGE0_CONTRACT: a kernel that dies before harnessd starts must reset
    # and count as a failed boot), and nothing kicks it until mps3-harnessd is
    # up, ~20-40 s later. 31 is the IP's MAXIMUM (range 8..31; 32 is refused at
    # set_property, probed 2026-09-23). Per the vendor HDL (timebase_wdt_core):
    # arming a disabled WDT zeroes the timebase, so the first expiry is 2^31 /
    # 100 MHz = 21.47 s after arming and the reset 42.95 s after it unless WDS is
    # cleared in between; once kicking, a stall resets 21.5-42.9 s after the
    # last kick. MBV-only: the bare-metal BD keeps 27, byte-identical.
    set wdt_cfg [list CONFIG.C_WDT_INTERVAL {31}]
    set_property -dict $wdt_cfg $axi_timebase_wdt_0
    soclabs_mbv_assert_cfg $axi_timebase_wdt_0 $wdt_cfg "axi_timebase_wdt_0 (Linux boot window: 2^31 cycles, reset 42.9 s after arming)"

    # [SEAM-2] the watchdog restarts the MBV and the whole DDR AXI path. Same
    # wdt_reset net that already drives proc_sys_reset_shell/aux_reset_in and
    # dfx_ctl_0/wdt_reset_i (set-dominant clamp) in shell_bd.tcl.
    connect_bd_net [get_bd_pins $axi_timebase_wdt_0/wdt_reset] [get_bd_pins $proc_sys_reset_cpu/aux_reset_in]
    connect_bd_net [get_bd_pins $axi_timebase_wdt_0/wdt_reset] [get_bd_pins $proc_sys_reset_ddr/aux_reset_in]

    # [SEAM-3] DDR4 calibration -> TELEM STATUS[0] (via CTRL.alarm_en). Take
    # telem_0/alarm_i off the shared ground it sits on in the bare-metal shell
    # and put calib on it. telem.sv 2-FF synchronises alarm_i (async board
    # ALERT allowed), so the 200 MHz ui_clk-domain flag crosses cleanly.
    set alarm_pin [get_bd_pins $telem_0/alarm_i]
    set alarm_net [get_bd_nets -quiet -of_objects $alarm_pin]
    if { $alarm_net eq "" } {
        error "cpu_mbv.tcl: telem_0/alarm_i has no net -- shell_bd.tcl no longer ties it; revisit \[SEAM-3\]"
    }
    disconnect_bd_net $alarm_net $alarm_pin
    connect_bd_net $ddr_calib $alarm_pin

    # [SEAM-8] THE DDR4 MIG IS A DEBUG SLAVE: GIVE IT AN EXPLICIT STATIC HUB.
    # The MIG always carries an XSDB calibration-debug slave (u_ddr_cal/
    # U_XSDB_SLAVE; the cell reports IS_DEBUGGABLE=1, read-only; the IP has no
    # parameter to drop it). The shell's debug_bridge_0 is in master mode with a
    # BSCAN switch (C_DEBUG_MODE 2, C_NUM_BS_MASTER 1 -- the ILA-over-XVC path),
    # and with such a bridge present Vivado will not auto-insert a hub
    # (UG908 "Debug Hub Insertion Guidelines"). The non-DFX shell impl only
    # warns (Chipscope 16-336) and leaves the slave unconnected; the DFX flow
    # FAILS opt_design (Chipscope 16-335, an ERROR that cannot be downgraded) --
    # found by the P-mint, 2026-09-23. So: a second BSCAN master port on
    # debug_bridge_0 feeding a mode-1 (BSCAN -> Debug Hub) bridge in the STATIC,
    # which is the hub the MIG's slave connects to. Side effect, wanted: the
    # MIG's calibration view is reachable over XVC like the RM ILAs.
    # MBV-only (the bare-metal BD has no MIG and keeps C_NUM_BS_MASTER 1).
    # build_dfx.tcl's RP pin gate still proves no port was punched into the RP.
    set_property CONFIG.C_NUM_BS_MASTER {2} $debug_bridge_0
    soclabs_mbv_assert_cfg $debug_bridge_0 [list CONFIG.C_DEBUG_MODE {2} CONFIG.C_NUM_BS_MASTER {2}] \
        "debug_bridge_0 (m1_bscan feeds the static MIG debug hub)"
    set mig_dbg_hub [create_bd_cell -type ip -vlnv xilinx.com:ip:debug_bridge:3.0 mig_dbg_hub]
    set mig_hub_cfg [list CONFIG.C_DEBUG_MODE {1} CONFIG.C_DESIGN_TYPE {0} \
                          CONFIG.C_CLK_INPUT_FREQ_HZ {100000000}]
    set_property -dict $mig_hub_cfg $mig_dbg_hub
    soclabs_mbv_assert_cfg $mig_dbg_hub $mig_hub_cfg "mig_dbg_hub (static BSCAN->Debug Hub for the DDR4 MIG)"
    connect_bd_intf_net [get_bd_intf_pins $debug_bridge_0/m1_bscan] [get_bd_intf_pins $mig_dbg_hub/S_BSCAN]
    connect_bd_net $shell_clk [get_bd_pins $mig_dbg_hub/clk]
}

default {
    error "cpu_mbv.tcl: unknown cpu_stage '$cpu_stage' -- shell_bd.tcl added a seam point this CPU file does not handle"
}

}
