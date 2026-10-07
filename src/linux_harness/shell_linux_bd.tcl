### DEPRECATED 2026-09-23 (LINUX_HARNESS_PLAN_2026-09-23.md DL2): the July Linux
### fork is superseded by the ONE shell BD's CPU seam -- fpga/shell/bd/shell_bd.tcl +
### cpu_mbv.tcl (SHELL_CPU=mbv), fpga/shell/build_shell.tcl and validate_bd.tcl,
### fpga/shell/shell_top.sv (`ifdef MPS3_SHELL_CPU_MBV), fpga/shell/constraints/mbv/.
### Do not edit or build this file; it is deleted at landing (lead).
###-----------------------------------------------------------------------------
### src/linux_harness/shell_linux_bd.tcl — THE TRANSPLANT BD.
###
### The mps3-nanosoc-platform static shell with the classic bare-metal
### MicroBlaze coordinator REPLACED by a Linux-capable MicroBlaze V
### (rv32imac + Zba/Zbb/Zbs, Sv32 SUPERVISOR MMU) + DDR4 MIG (1 GiB @
### 0x8000_0000) + a 128 KiB LMB boot BRAM, with EVERY existing shell
### peripheral kept at its frozen address and reachable from the MBV, the
### full 4-line IRQ set into the (asserted) axi_intc, and an explicit
### assign_bd_address map exported after validate.
###
### AUTHORED AGAINST (ground truth, in precedence order):
###   1. TRANSPLANT_CONTRACT.md (same dir) — authoritative audit of
###      fpga/shell/bd/shell_bd.tcl (working tree, 41 cells, 19-intf decoupler)
###   2. <repo>/fpga/shell/bd/shell_bd.tcl
###      — every RP-boundary/CSR/vendor-IP wire below is carried VERBATIM
###   3. ../linux_soc/hw/mbv_soc.tcl — the PROVEN MBV+DDR4 recipe; its lessons
###      (c0_ddr4_aresetn driver, smartconnect dual-clock crossing, calib-gated
###      CPU release, _assert_cfg read-back discipline, undriven-reset guard,
###      explicit assign_bd_address, DDR4 external-port rename) are carried
###      VERBATIM and marked [MBV-LESSON].
###
### BASELINE DECLARATION (contract "baseline warning"): this file forks the
### WORKING-TREE shell_bd.tcl of branch feat/clcd-kvm-display — i.e. the
### 41-cell BD WITH clcd_kvm_0 (Wave 4) and WITH the 2026-07-15 QSPI v0.2
### boundary (decoupler IDs 15-18, qspi_pad_* ports, OVLSTORE SPI_0 removed).
### That baseline is AHEAD of the board-proven shell (2026-07-11,
### static_id 0xE4B1C44A, 40 cells, 15-intf decoupler): neither Wave 4 nor
### QSPI v0.2 has been through build_shell.tcl or a board. Forking the
### working tree is deliberate — it is the declared platform direction and
### the decoupler/partition-pin boundary this successor must preserve —
### but nothing here inherits "board-proven" status from the old shell.
### A CPU transplant is a maximal static change regardless: static_id
### re-mints and ALL 8 RM overlays must be re-keyed/re-implemented (contract
### §7 law, §9.6).
###
### CONTRACT WITH THE PARENT SCRIPT (mirrors mbv_soc.tcl):
###   1. create_project ... -part xcku115-flvb1760-1-c
###   2. set_property ip_repo_paths <snapshot of fpga/shell/ip_packaged> +
###      update_ip_catalog  (the eight soclabs.org:user:*:1.0 CSR components)
###   3. create_bd_design <name>
###   4. source ddr4_ip.tcl        ;# creates cell `ddr4_0` (proven config)
###   5. source shell_linux_bd.tcl ;# THIS FILE — ends in validate_bd_design
###                                 + post-propagation asserts + reset guard
###                                 + address-map export + save_bd_design
###
### DELIBERATE DEVIATIONS from the audit contract / donor sources are tagged
### [DEV-n] inline and enumerated in ADDRESS_MAP.md §Deviations. Summary:
###   [DEV-1] baseline = working tree (above).
###   [DEV-2] classic-MB subsystem removed: microblaze_0/mdm_1 replaced by
###           microblaze_riscv_0/mdm_riscv_0; the 1 MiB local_ram replaced by
###           a 128 KiB lmb_bram BOOT BRAM (linux_soc sizing). The LMB JTAG
###           diag mailbox (0xFFF80, magic 0xD1A6C0DE) loses its substrate
###           and is RETIRED here — successor diagnostics must be re-homed
###           (ramoops/driver mailbox), see ADDRESS_MAP.md.
###   [DEV-3] CPU/AXI clock = shell_clk (100 MHz from OSCCLK1 via
###           clk_wiz_shell), NOT the ui_clk-derived clk_wiz of mbv_soc.tcl.
###           This is contract §9.3 option (a) — recommended: the 16-slave
###           shell fabric keeps its clock; smartconnect_ddr (NUM_CLKS 2)
###           does the 100<->200 MHz crossing. Made consciously, not blended.
###   [DEV-4] a SECOND proc_sys_reset (proc_sys_reset_cpu) gates ONLY the
###           CPU+LMB+DDR-path on c0_init_calib_complete. mbv_soc.tcl gated
###           its whole SoC; here the shell peripheral/DFX fabric must stay
###           alive even if DDR4 never calibrates (harness liveness), so
###           proc_sys_reset_shell keeps its original, calib-independent role.
###   [DEV-5] axi_intc_0 pinned to the Linux irq-xilinx-intc contract
###           (C_KIND_OF_INTR 0x0 all-level, C_IRQ_IS_LEVEL 1, C_IRQ_ACTIVE
###           0x1, C_HAS_FAST 0) — the donor shell left these at defaults
###           (poll-model firmware never cared). Same 4 sources, same order.
###   [DEV-6] axi_timer enable_timer2 pinned {1} + asserted (donor inherited
###           the default; Linux clockevent+clocksource needs both counters).
###   [DEV-7] new BD ports for DDR4: c0_sys_clk_p/n (OSC6 100 MHz diff),
###           external interface c0_ddr4 (renamed — the wrapper-port/XDC
###           match is load-bearing), calib_complete_led_n. The successor
###           shell_top must add these pads; the board-proven shell_top.sv
###           is NOT touched.
###   [DEV-8] axi_intc AXI-Lite interface referenced as lowercase `s_axi`
###           (2026.1 catalogue fact, probed by mbv_soc.tcl) — the donor BD
###           said S_AXI at 2024.1. Resolved via _find_intf either way.
###   [DEV-9] authored/validated at Vivado 2026.1 (donor: 2024.1). Exact
###           donor VLNVs are requested; any catalogue version substitution
###           is LOUD (::vlnv_substitutions) and recorded in the build log.
###   [DEV-10] DUT DEBUG IS JTAG, NOT SWD (2026-09-11). swd_bb_0 -> jtag_bb_0
###           and the rp_swd_* partition-pin group -> rp_jtag_*, catching this
###           fork up with the A6 SWD->JTAG cutover that moved
###           fpga/shell/boundary.yaml months ago. This is NOT a rename and
###           NOT a deviation from the donor -- it is the donor
###           (fpga/shell/bd/shell_bd.tcl:560,709,798-806,1348,1412), carried
###           verbatim like every other boundary wire. It is listed as a
###           numbered deviation only because it is a deviation from what THIS
###           FILE said yesterday. See docs/planning/LINUX_FORK_JTAG_MIGRATION.md.
### Frozen half honoured: decoupler ALL_PARAMS verbatim (19 intfs, hex-string
### DECOUPLED_VALUEs, qspi_csn=0x1 the sole non-zero); dfx_ctl/dut_clkrst/
### shutdown-mgr/inv_rp_in_reset nets verbatim; clk_wiz_dut AXI-Lite DRP @
### 0x44AB0000; all partition-pin ports (names+widths) verbatim; axi_emc slow
### LAN9220 AC timing verbatim; debug_bridge C_DEBUG_MODE 2; VPHY/GENCHK pages
### still reserved; rp_irq_out still a documented decoupler tie-off.
###
### CHANGELOG
###   2026-09-11 (SWD->JTAG catch-up, [DEV-10]): this fork was still building the
###     RETIRED swd_* debug boundary a whole cutover after fpga/shell/boundary.yaml
###     moved to jtag_*. Until cb45c18 the fork carried its OWN copy of the RP stub,
###     so the two wrong halves agreed and the build reported success -- while
###     producing a static into which NO overlay in fpga/dfx/prod/ could link.
###     cb45c18 pointed the build at the generated stub, which made the mismatch a
###     loud elaboration failure. This change fixes it at the source: swd_bb_0 ->
###     jtag_bb_0, rp_swd_{clk,dio_o,dio_oe,dio_i} -> rp_jtag_{tck,tms,tdi,tdo},
###     decoupler INTF ID 0 swd_dio_i -> jtag_tdo, MAP_SWDBB_BASE -> MAP_JTAGBB_BASE
###     (SAME 0x44A7_0000 page). The four ports keep their 3xO+1xI shape, which is
###     precisely why nothing noticed: a gate that counts ports or checks directions
###     would have passed. tests/linux_fork_boundary/ now diffs the NAMES, widths,
###     directions and decoupler clamps against fpga/shell/boundary.yaml.
###   2026-07-17 (M7b transplant-fold): SILICON ERRATA folded in from the
###     linux_soc board windows (2026-07-16/17). E1 "Sstc dead" + E2 "STIP
###     stuck" mean the architected S-mode timer is unusable: the kernel tick
###     moves to the AXI timer on SEIP via the "soclabs,mbv-timer" errata driver
###     (linux_soc patches/linux/0003), and the DEPLOYED DTS drops "sstc". NO
###     HARDWARE CONFIG CHANGE here — C_USE_SSTC stays 1 as a latent capability
###     (the fixed-IP re-enable path), C_INTERRUPT_WAKEUP=1 (E3 wfi-coma fix)
###     was already asserted and is re-VERIFIED this fold, and axi_timer already
###     drives INTC In1 as a level source. Comments at C_USE_SSTC, [DEV-6], the
###     [FIX-A] post-validate assert and the address_map export were corrected
###     to state the errata truth (sstc is NOT the clockevent). Re-validated.
###   2026-07-16 (judge-fix wave): [FIX-A] mbv_cfg pins + read-back-asserts
###     C_USE_COUNTERS=1, C_USE_SSTC=1 (the kernel's ONLY clocksource/
###     clockevent — previously inherited as IP defaults, one Vivado upgrade
###     from silently flipping) and C_INTERRUPT_WAKEUP=1 (the WFI-COMA FIX,
###     PROVEN ON SILICON 2026-07-16: default 0 = wfi never wakes, Linux
###     hangs at first idle; repro src/linux_soc/fw_mspin). [FIX-B] the
###     address_map.txt riscv_isa line is now COMPUTED from post-validate
###     read-backs, not a hardcoded string; sstc/counters/interrupt_wakeup
###     read-backs exported alongside. [FIX-C] axi_intc C_KIND_OF_INTR=0x0
###     re-asserted POST-validate + the manual-vs-computed Vivado warning
###     documented as expected/benign (linux_soc IRQ_WIRING_AUDIT.md: the
###     LVL_P latch makes level-forced hardware-safe). [FIX-D] boot-BRAM
###     provisioning ownership documented at the lmb_bram cell
###     (ADDRESS_MAP.md §6 is normative). Re-validated after these edits.
###   2026-07-15: initial authoring + validate (see ADDRESS_MAP.md §5).
###-----------------------------------------------------------------------------

set REQUIRED_PART "xcku115-flvb1760-1-c"

###############################################################################
# SECTION 0 — THE ADDRESS MAP (THE CONTRACT)
#
# Every base identical to the donor shell (shell-regmap.md v0.5 / contract §2)
# — firmware/common/platform_regs.h and all host tooling bake these. The MBV
# recipe adds 0x8000_0000..0xBFFF_FFFF (DDR4, no collision) and shrinks the
# 0x0 LMB window from 1 MiB (firmware RAM) to 128 KiB (boot BRAM) [DEV-2].
###############################################################################

set MAP_LMB_BASE      0x00000000
set MAP_LMB_RANGE     128K            ;# [DEV-2] boot BRAM (linux_soc sizing), was 1 MiB firmware RAM
set MAP_UART_BASE     0x40600000      ;# UARTLITE (console fallback)
set MAP_INTC_BASE     0x41200000      ;# INTC
set MAP_TIMER_BASE    0x41C00000      ;# TIMER
set MAP_CLKRST_BASE   0x44A00000      ;# CLKRST      dut_clkrst_0
set MAP_DFXCTL_BASE   0x44A10000      ;# DFXCTL      dfx_ctl_0
set MAP_HWICAP_BASE   0x44A20000      ;# HWICAP
# 0x44A3_0000 VPHY — RESERVED, not instantiated (kept reserved)
set MAP_OVL_BASE      0x44A40000      ;# OVLSTORE (vestigial register window)
set MAP_TELEM_BASE    0x44A50000      ;# TELEM
# 0x44A6_0000 GENCHK — RESERVED, not instantiated (kept reserved)
set MAP_JTAGBB_BASE   0x44A70000      ;# JTAGBB [DEV-10] — the page is the
                                      ;# SAME 64 KiB the retired SWDBB held.
                                      ;# The A6 cutover re-used it rather than
                                      ;# moving it, which is why the old
                                      ;# firmware XVC/SWD target answered for a
                                      ;# month by coincidence: same page, same
                                      ;# bit positions, different protocol.
set MAP_DBGBR_BASE    0x44A80000      ;# DBGBR (XVC)
set MAP_UARTBR_BASE   0x44A90000      ;# UARTBR
set MAP_GPIO_BASE     0x44AA0000      ;# GPIO
set MAP_DRP_BASE      0x44AB0000      ;# MMCM_DUT_DRP (clk_wiz_dut s_axi_lite)
set MAP_CLCD_BASE     0x44AC0000      ;# CLCD
set MAP_CLCDKVM_BASE  0x44AD0000      ;# CLCDKVM (Wave 4)
set MAP_PERIPH_RANGE  64K
set MAP_DDR_BASE      0x80000000      ;# [MBV-LESSON] 1 GiB — rv32 Linux lowmem limit
set MAP_DDR_RANGE     1G
set MAP_DDR_HIGH      0xBFFFFFFF      ;# cache aperture == DDR aperture, DERIVED
set MAP_EMC_BASE      0xC0000000      ;# LAN9220 via AXI EMC
set MAP_EMC_RANGE     16M

set MAP_RESET_VECTOR  0x00000000      ;# boot from BRAM, never uncalibrated DDR

# IRQ map — xlconcat In<n> == INTC input n == DTS interrupt number. SAME four
# sources, SAME order as the donor shell (contract §4). rp_irq_out remains a
# deliberate NON-interrupt (decoupler tie-off; CDC + type + no-consumer
# reasons recorded at the wire below).
set IRQ_HWICAP 0
set IRQ_TIMER  1
set IRQ_UART   2
set IRQ_ETH    3

set CPU_CLK_HZ 100000000              ;# [DEV-3] shell_clk — DTS timebase must match

###############################################################################
# GUARDS — fail loud and early if the caller's contract was not met.
###############################################################################

if { [catch { current_project } _proj] || $_proj eq "" } {
    error "shell_linux_bd.tcl: no project is open (parent must create_project -part $REQUIRED_PART)."
}
set _cur_part [get_property PART [current_project]]
if { $_cur_part ne $REQUIRED_PART } {
    error "shell_linux_bd.tcl: project part is '$_cur_part', this design targets '$REQUIRED_PART' only."
}
if { [catch { current_bd_design } _bd] || $_bd eq "" } {
    error "shell_linux_bd.tcl: no block design is open (parent must create_bd_design first)."
}
if { [llength [get_bd_cells -quiet ddr4_0]] == 0 } {
    error "shell_linux_bd.tcl: cell 'ddr4_0' not found — source ddr4_ip.tcl BEFORE this file."
}
# The eight packaged soclabs CSR components must be on the IP catalogue.
foreach _csr {dut_clkrst dfx_ctl board_gpio jtag_bb telem uart_bridge clcd clcd_kvm} {
    if { [llength [get_ipdefs -quiet soclabs.org:user:${_csr}:1.0]] == 0 } {
        error "shell_linux_bd.tcl: soclabs.org:user:${_csr}:1.0 not in the IP catalogue — parent must set ip_repo_paths to the ip_packaged snapshot + update_ip_catalog first."
    }
}
current_bd_instance [get_bd_cells /]

puts "==========================================================="
puts " shell_linux_bd.tcl : building THE TRANSPLANT BD into '$_bd'"
puts "   part            = $_cur_part"
puts "   ddr4_0          = present"
puts "   reset vector    = $MAP_RESET_VECTOR (LMB boot BRAM)"
puts "   DDR aperture    = $MAP_DDR_BASE + $MAP_DDR_RANGE"
puts "   CPU/shell clock = $CPU_CLK_HZ Hz (clk_wiz_shell) \[DEV-3\]"
puts "==========================================================="

# ---------------------------------------------------------------------------
# [MBV-LESSON] READ-BACK ASSERTION — verbatim from mbv_soc.tcl. IP xguis
# SILENTLY refuse illegal parameter values; every Linux-critical parameter is
# read back and compared, and a mismatch is FATAL.
# ---------------------------------------------------------------------------
proc _assert_cfg {cell cfg_list what} {
    set bad {}
    foreach {k want} $cfg_list {
        set got [get_property $k $cell]
        set eq [string equal $got $want]
        if { !$eq } {
            if { ![catch { expr { ($got) == ($want) } } r] && $r } { set eq 1 }
        }
        if { !$eq } {
            lappend bad [format "%-26s requested=%-14s read-back=%s" $k $want $got]
        }
    }
    if { [llength $bad] } {
        puts ""
        puts "########################################################################"
        puts "# $what — PARAMETER READ-BACK MISMATCH"
        foreach b $bad { puts "#   $b" }
        puts "########################################################################"
        error "shell_linux_bd.tcl: $what read-back mismatch (see above)."
    }
    puts "-- $what: all [expr {[llength $cfg_list]/2}] parameters read back as requested"
}

# [DEV-9] 2024.1 -> 2026.1 port helper: request the donor's exact VLNV; if the
# catalogue no longer carries that version, substitute the newest same-name
# definition LOUDLY (collected in ::vlnv_substitutions for the build report —
# each substitution must be reviewed, a new major version may change CONFIGs).
set ::vlnv_substitutions {}
proc _create_ip {vlnv name} {
    if { [llength [get_ipdefs -quiet -all $vlnv]] > 0 } {
        return [create_bd_cell -type ip -vlnv $vlnv $name]
    }
    set base [join [lrange [split $vlnv :] 0 2] :]
    set cands [lsort [get_ipdefs -quiet ${base}:*]]
    if { [llength $cands] == 0 } {
        error "shell_linux_bd.tcl: IP '$vlnv' not in the 2026.1 catalogue and no ${base}:* candidate exists."
    }
    set sub [lindex $cands end]
    puts "WARNING: VLNV_SUBSTITUTION $vlnv -> $sub (2026.1 catalogue drift — REVIEW CONFIG COMPATIBILITY)"
    lappend ::vlnv_substitutions "$vlnv -> $sub"
    return [create_bd_cell -type ip -vlnv $sub $name]
}

# [DEV-8] interface-name case drift helper (axi_intc S_AXI@2024.1 vs
# s_axi@2026.1 etc.): return the first candidate interface pin that exists.
proc _find_intf {cell candidates} {
    foreach c $candidates {
        set p [get_bd_intf_pins -quiet $cell/$c]
        if { $p ne "" } { return $p }
    }
    error "shell_linux_bd.tcl: none of the interface pins '$candidates' exist on $cell."
}
# Same idea for address segments (paths tried in order).
proc _find_seg {candidates} {
    foreach c $candidates {
        set s [get_bd_addr_segs -quiet $c]
        if { [llength $s] == 1 } { return $s }
    }
    error "shell_linux_bd.tcl: no unique address segment among '$candidates'."
}

# Handles on the pre-existing DDR4 controller (ddr4_ip.tcl header):
set ddr_ui_clk [get_bd_pins ddr4_0/c0_ddr4_ui_clk]          ;# 200 MHz, already BUFG'd
set ddr_ui_rst [get_bd_pins ddr4_0/c0_ddr4_ui_clk_sync_rst] ;# active-HIGH, ui_clk domain
set ddr_calib  [get_bd_pins ddr4_0/c0_init_calib_complete]  ;# active-HIGH "DDR ready"

###############################################################################
# SECTION 1 — TOP-LEVEL BD PORTS
# Donor shell SECTION 0 carried VERBATIM (names, widths, directions,
# -freq_hz), plus the new DDR4 ports [DEV-7].
###############################################################################

# CRITICAL (donor lesson, load-bearing): declare 50 MHz AT CREATION. A BD clock
# port defaults to FREQ_HZ=100000000 and clk_wiz honours the PORT's FREQ_HZ
# over its own PRIM_IN_FREQ -> FVCO 2000 MHz (illegal) -> "IO Clock Placer
# failed" in impl.
create_bd_port -dir I -type clk -freq_hz 50000000 osc_clk_50m
create_bd_port -dir I -type rst sys_rst_n
set_property CONFIG.POLARITY {ACTIVE_LOW} [get_bd_ports sys_rst_n]

# LAN9220 host I/F (AXI EMC external interface + interrupt input)
create_bd_intf_port -mode Master -vlnv xilinx.com:interface:emc_rtl:1.0 EMC_INTF
create_bd_port -dir I eth_irq

# Physical console fallback UART
create_bd_port -dir O uart_tx_f
create_bd_port -dir I uart_rx_f

# Board GPIO passthrough pads
create_bd_port -dir O -from 15 -to 0 board_gpio_pad_o
create_bd_port -dir O -from 15 -to 0 board_gpio_pad_oe
create_bd_port -dir I -from 15 -to 0 board_gpio_pad_i

# CLCD pads (via clcd_kvm_0 since Wave 4)
create_bd_port -dir O -from 7 -to 0 clcd_pd_o
create_bd_port -dir I -from 7 -to 0 clcd_pd_i
create_bd_port -dir O clcd_pd_oe
create_bd_port -dir O clcd_cs_n
create_bd_port -dir O clcd_wr_n
create_bd_port -dir O clcd_rd_n
create_bd_port -dir O clcd_rs
create_bd_port -dir O clcd_bl
create_bd_port -dir O clcd_rst_n

# TELEM I2C seam (tied off inside telem_0; pads reserved for ina228 engine)
create_bd_port -dir O i2c_scl_o
create_bd_port -dir O i2c_scl_t
create_bd_port -dir O i2c_sda_o
create_bd_port -dir O i2c_sda_t
create_bd_port -dir I i2c_sda_i

# ==== RP PARTITION-PIN BOUNDARY (partition-pins.md v0.2, VERBATIM — frozen) ==
create_bd_port -dir O rp_dut_clk
create_bd_port -dir O rp_dut_resetn
create_bd_port -dir O rp_rp_resetn
create_bd_port -dir O rp_dbg_resetn

# [DEV-10] DUT debug group: the A6 SWD->JTAG cutover. FOUR UNIDIRECTIONAL
# wires (TCK/TMS/TDI shell-driven, TDO shell-received) replace the two-wire SWD
# group's clk + tristate pair (dio_o/dio_oe) + readback (dio_i). Port COUNT and
# DIRECTIONS happen to coincide 3xO + 1xI, which is exactly why this drift
# survived a cutover unnoticed: only the NAMES differ at this level, and
# nothing checked the names. fpga/shell/boundary.yaml group `jtag` is the
# declaration; tests/linux_fork_boundary now diffs these four lines against it.
create_bd_port -dir O rp_jtag_tck
create_bd_port -dir O rp_jtag_tms
create_bd_port -dir O rp_jtag_tdi
create_bd_port -dir I rp_jtag_tdo

create_bd_port -dir O rp_phy_rmii_ref_clk
create_bd_port -dir O rp_phy_rmii_crs_dv
create_bd_port -dir O -from 1 -to 0 rp_phy_rmii_rxd
create_bd_port -dir I -from 1 -to 0 rp_phy_rmii_txd
create_bd_port -dir I rp_phy_rmii_tx_en
create_bd_port -dir I rp_mdc
create_bd_port -dir I rp_mdio_o
create_bd_port -dir I rp_mdio_oe
create_bd_port -dir O rp_mdio_i

create_bd_port -dir I -from 7 -to 0 rp_uart_tx_tdata
create_bd_port -dir I rp_uart_tx_tvalid
create_bd_port -dir O rp_uart_tx_tready
create_bd_port -dir O -from 7 -to 0 rp_uart_rx_tdata
create_bd_port -dir O rp_uart_rx_tvalid
create_bd_port -dir I rp_uart_rx_tready
create_bd_port -dir I rp_swo

create_bd_port -dir I -from 31 -to 0 rp_rm_id
create_bd_port -dir I rp_dut_lockup
create_bd_port -dir I rp_irq_out

create_bd_port -dir I -from 15 -to 0 rp_dut_gpio_o
create_bd_port -dir I -from 15 -to 0 rp_dut_gpio_oe
create_bd_port -dir O -from 15 -to 0 rp_dut_gpio_i

# QSPI XiP partition pins (v0.2). qspi_io_i is NOT a BD port (shell_top
# pad->RP passthrough, never clamped — contract §7).
create_bd_port -dir I rp_qspi_sclk
create_bd_port -dir I rp_qspi_csn
create_bd_port -dir I -from 3 -to 0 rp_qspi_io_o
create_bd_port -dir I -from 3 -to 0 rp_qspi_io_oe
create_bd_port -dir O qspi_pad_sclk
create_bd_port -dir O qspi_pad_csn
create_bd_port -dir O -from 3 -to 0 qspi_pad_io_o
create_bd_port -dir O -from 3 -to 0 qspi_pad_io_oe

# CLCD-KVM button (raw async active-low USER_nPB1; sync+debounce inside IP)
create_bd_port -dir I user_npb1

# ==== NEW DDR4 ports [DEV-7] =================================================
# OSC6 100 MHz differential reference for the MIG (linux_soc recipe; pads per
# ddr4_pins.xdc — SLR1 banks 49-51, no RP-pblock collision, keep separable).
create_bd_port -dir I c0_sys_clk_p
create_bd_port -dir I c0_sys_clk_n
connect_bd_net [get_bd_ports c0_sys_clk_p] [get_bd_pins ddr4_0/c0_sys_clk_p]
connect_bd_net [get_bd_ports c0_sys_clk_n] [get_bd_pins ddr4_0/c0_sys_clk_n]
# DDR4 calibration status LED (active-LOW pad; LIT == calibrated) — the
# bring-up diagnostic that distinguishes "DDR dead" from "CPU dead".
create_bd_port -dir O calib_complete_led_n

###############################################################################
# SECTION 2 — CLOCKING + RESETS
# clk_wiz_shell / clk_wiz_dut / proc_sys_reset_shell VERBATIM from the donor.
# proc_sys_reset_cpu is NEW [DEV-4]: CPU-domain reset gated on DDR calibration
# (the [MBV-LESSON] slave-live-first ordering) WITHOUT making the shell
# peripheral/DFX fabric depend on DDR4.
###############################################################################

set clk_wiz_shell [_create_ip xilinx.com:ip:clk_wiz:6.0 clk_wiz_shell]
set_property -dict [list \
    CONFIG.PRIM_IN_FREQ {50.000} \
    CONFIG.PRIM_SOURCE {No_buffer} \
    CONFIG.CLKOUT2_USED {true} \
    CONFIG.CLKOUT1_REQUESTED_OUT_FREQ {100.000} \
    CONFIG.CLKOUT2_REQUESTED_OUT_FREQ {50.000} \
    CONFIG.USE_LOCKED {true} \
    CONFIG.USE_RESET {true} \
    CONFIG.RESET_TYPE {ACTIVE_LOW} \
    CONFIG.CLKIN1_JITTER_PS {160.0} \
] $clk_wiz_shell

# DUT-clock MMCM — AXI4-Lite-only DRP (the ONLY arbitrary DUT-clock reconfig
# path, frozen @0x44AB0000). 50 MHz default (D12 still open, inherited).
set clk_wiz_dut [_create_ip xilinx.com:ip:clk_wiz:6.0 clk_wiz_dut]
set_property -dict [list \
    CONFIG.PRIM_IN_FREQ {50.000} \
    CONFIG.PRIM_SOURCE {No_buffer} \
    CONFIG.CLKOUT1_REQUESTED_OUT_FREQ {50.000} \
    CONFIG.USE_LOCKED {true} \
    CONFIG.USE_DYN_RECONFIG {true} \
    CONFIG.CLKIN1_JITTER_PS {160.0} \
] $clk_wiz_dut

set proc_sys_reset_shell [_create_ip xilinx.com:ip:proc_sys_reset:5.0 proc_sys_reset_shell]

connect_bd_net [get_bd_ports osc_clk_50m] [get_bd_pins $clk_wiz_shell/clk_in1]
connect_bd_net [get_bd_ports osc_clk_50m] [get_bd_pins $clk_wiz_dut/clk_in1]
connect_bd_net [get_bd_ports sys_rst_n]   [get_bd_pins $clk_wiz_shell/resetn]
connect_bd_net [get_bd_ports sys_rst_n]   [get_bd_pins $proc_sys_reset_shell/ext_reset_in]
connect_bd_net [get_bd_pins $clk_wiz_shell/clk_out1] [get_bd_pins $proc_sys_reset_shell/slowest_sync_clk]
connect_bd_net [get_bd_pins $clk_wiz_shell/locked]   [get_bd_pins $proc_sys_reset_shell/dcm_locked]

set shell_clk        [get_bd_pins $clk_wiz_shell/clk_out1]
set rmii_ref_clk_sig [get_bd_pins $clk_wiz_shell/clk_out2]
set dut_clk_sig      [get_bd_pins $clk_wiz_dut/clk_out1]
set shell_aresetn    [get_bd_pins $proc_sys_reset_shell/peripheral_aresetn]
set shell_bus_arstn  [get_bd_pins $proc_sys_reset_shell/interconnect_aresetn]
# proc_sys_reset_shell/mb_reset now has NO consumer (the classic MB+LMB are
# gone [DEV-2]; the MBV+LMB use proc_sys_reset_cpu below). Dangling OUTPUT —
# legal. proc_sys_reset_shell/aux_reset_in stays unconnected exactly as in the
# board-proven donor (whitelisted in the reset guard).

# clk_wiz_dut's AXI-Lite slave clock/reset (its ONLY reset — no scalar pin).
connect_bd_net $shell_clk     [get_bd_pins $clk_wiz_dut/s_axi_aclk]
connect_bd_net $shell_aresetn [get_bd_pins $clk_wiz_dut/s_axi_aresetn]

connect_bd_net $rmii_ref_clk_sig [get_bd_ports rp_phy_rmii_ref_clk]
connect_bd_net $dut_clk_sig      [get_bd_ports rp_dut_clk]

# --- [DEV-4] CPU-domain reset: released only when the shell MMCM is locked
#     AND the button is idle AND DDR4 calibration is complete ---------------
# [MBV-LESSON] aux_reset_in is active-LOW (C_AUX_RESET_HIGH defaults 0):
# calib=0 holds reset, calib=1 releases — polarities line up with ZERO glue.
# Do NOT set C_AUX_RESET_HIGH. The reset vector lives in BRAM, but the CPU
# must still not run before the DRAM it will jump into has calibrated.
set proc_sys_reset_cpu [_create_ip xilinx.com:ip:proc_sys_reset:5.0 proc_sys_reset_cpu]
connect_bd_net $shell_clk                          [get_bd_pins $proc_sys_reset_cpu/slowest_sync_clk]
connect_bd_net [get_bd_pins $clk_wiz_shell/locked] [get_bd_pins $proc_sys_reset_cpu/dcm_locked]
connect_bd_net [get_bd_ports sys_rst_n]            [get_bd_pins $proc_sys_reset_cpu/ext_reset_in]
connect_bd_net $ddr_calib                          [get_bd_pins $proc_sys_reset_cpu/aux_reset_in]
# proc_sys_reset_cpu/mb_debug_sys_rst deliberately unconnected: mdm_riscv in
# plain JTAG-BSCAN mode carries debug reset over the MBDEBUG_0 bus
# ([MBV-LESSON]; harmless [BD 41-759] auto-tie; whitelisted in the guard).

set mb_reset   [get_bd_pins $proc_sys_reset_cpu/mb_reset]              ;# active-HIGH -> CPU + LMB
set cpu_arstn  [get_bd_pins $proc_sys_reset_cpu/peripheral_aresetn]    ;# (unused; kept for symmetry)
set ddr_icon_arstn [get_bd_pins $proc_sys_reset_cpu/interconnect_aresetn] ;# -> smartconnect_ddr

# --- [MBV-LESSON] DRIVE THE MIG'S AXI-SLAVE RESET — VERBATIM ----------------
# ddr4_0/c0_ddr4_aresetn is an active-LOW *INPUT* (ui_clk domain). Left
# dangling, IPI silently ties it 1'b0 = RESET ASSERTED FOREVER: calibration
# says OK while the AXI shim is stone dead and every CPU store is swallowed
# with no B response. Drive it with ~c0_ddr4_ui_clk_sync_rst (the IP's own
# example-design idiom) — clean active-LOW reset in the shim's own domain.
set ddr_axi_rst_inv [_create_ip xilinx.com:ip:util_vector_logic:2.0 ddr_axi_rst_inv]
set ddr_axi_rst_inv_cfg [list CONFIG.C_SIZE {1} CONFIG.C_OPERATION {not}]
set_property -dict $ddr_axi_rst_inv_cfg $ddr_axi_rst_inv
_assert_cfg $ddr_axi_rst_inv $ddr_axi_rst_inv_cfg \
    "ddr_axi_rst_inv (active-HIGH ui_clk_sync_rst -> active-LOW MIG AXI-slave reset)"
connect_bd_net $ddr_ui_rst                        [get_bd_pins $ddr_axi_rst_inv/Op1]
connect_bd_net [get_bd_pins $ddr_axi_rst_inv/Res] [get_bd_pins ddr4_0/c0_ddr4_aresetn]

# --- MIG system reset: active-LOW pad -> [NOT] -> active-HIGH sys_rst -------
# [MBV-LESSON] THE POLARITY TRAP, verbatim: USER_nPB0 idles HIGH; ddr4 sys_rst
# is ACTIVE-HIGH with NO polarity knob. Straight-through wiring would hold the
# MIG in reset forever (dark calib LED, dead board). Explicit inverter.
set ddr_sys_rst_inv [_create_ip xilinx.com:ip:util_vector_logic:2.0 ddr_sys_rst_inv]
set ddr_sys_rst_inv_cfg [list CONFIG.C_SIZE {1} CONFIG.C_OPERATION {not}]
set_property -dict $ddr_sys_rst_inv_cfg $ddr_sys_rst_inv
_assert_cfg $ddr_sys_rst_inv $ddr_sys_rst_inv_cfg "ddr_sys_rst_inv (active-LOW pad -> active-HIGH MIG sys_rst)"
connect_bd_net [get_bd_ports sys_rst_n]            [get_bd_pins $ddr_sys_rst_inv/Op1]
connect_bd_net [get_bd_pins $ddr_sys_rst_inv/Res]  [get_bd_pins ddr4_0/sys_rst]

# --- calib LED: active-HIGH calib -> active-LOW LED pad (LIT == calibrated) -
set calib_led_inv [_create_ip xilinx.com:ip:util_vector_logic:2.0 calib_led_inv]
set calib_led_inv_cfg [list CONFIG.C_SIZE {1} CONFIG.C_OPERATION {not}]
set_property -dict $calib_led_inv_cfg $calib_led_inv
_assert_cfg $calib_led_inv $calib_led_inv_cfg "calib_led_inv (active-HIGH calib -> active-LOW LED pad)"
connect_bd_net $ddr_calib                      [get_bd_pins $calib_led_inv/Op1]
connect_bd_net [get_bd_pins $calib_led_inv/Res] [get_bd_ports calib_complete_led_n]

###############################################################################
# SECTION 3 — MicroBlaze V CPU + MDM + LMB BOOT BRAM  [DEV-2]
# Core config VERBATIM from mbv_soc.tcl (the Linux-capable ISA: rv32imac +
# Zba/Zbb/Zbs, Sv32 SUPERVISOR, caches over the DDR aperture, soft-float).
# C_OPTIMIZATION=0 is a FINDING, not a choice: 2 (FREQUENCY) is illegal with
# C_USE_MMU=3. No Zicbom parameter exists on this IP — DTS riscv,isa must be
# rv32imac_zba_zbb_zbs (NO zicbom).
###############################################################################

set mbv [_create_ip xilinx.com:ip:microblaze_riscv:1.0 microblaze_riscv_0]
set mbv_cfg [list \
    CONFIG.C_USE_MMU          {3} \
    CONFIG.C_ADDR_SIZE        {32} \
    CONFIG.C_DATA_SIZE        {32} \
    CONFIG.C_OPTIMIZATION     {0} \
    CONFIG.C_BASE_VECTORS     $MAP_RESET_VECTOR \
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
    CONFIG.C_ICACHE_BASEADDR  $MAP_DDR_BASE \
    CONFIG.C_ICACHE_HIGHADDR  $MAP_DDR_HIGH \
    CONFIG.C_DCACHE_BASEADDR  $MAP_DDR_BASE \
    CONFIG.C_DCACHE_HIGHADDR  $MAP_DDR_HIGH \
    CONFIG.C_USE_INTERRUPT    {1} \
    CONFIG.C_INTERRUPT_WAKEUP {1} \
    CONFIG.C_DEBUG_ENABLED    {1} \
    CONFIG.C_INTERCONNECT     {2} \
    CONFIG.C_D_AXI            {1} \
    CONFIG.C_I_LMB            {1} \
    CONFIG.C_D_LMB            {1} \
]
set_property -dict $mbv_cfg $mbv
_assert_cfg $mbv $mbv_cfg "microblaze_riscv_0 (Linux-capable RV32 core)"
# [FIX-A] Three Linux-load-bearing parameters PINNED + read-back-asserted (they
# ride the _assert_cfg above), verbatim from the flown mbv_soc.tcl:
#   C_USE_COUNTERS 1     Zicntr cycle/time/instret. `time` is the kernel's ONLY
#                        clocksource (rdtime / sched_clock / printk stamps).
#                        Previously an inherited IP default — one Vivado upgrade
#                        away from silently flipping. Pinned, per the judge flag.
#   C_USE_SSTC 1         Sstc stimecmp/stimecmph. KEPT as a LATENT hardware
#                        capability (matches the flown mbv_soc.tcl), but per
#                        SILICON ERRATA E1/E2 it is NOT the deployed clockevent:
#                        on this MBV V silicon (board 2026-07-16/17) writing
#                        stimecmp never raises a deliverable S-mode interrupt
#                        (E1) and mip.STIP is stuck-once-set (E2). The kernel
#                        tick therefore moves to the AXI timer delivered on SEIP
#                        via the "soclabs,mbv-timer" errata driver, and the
#                        DEPLOYED DTS drops "sstc" from riscv,isa-extensions
#                        (shell_linux.dts; linux_soc mk_dtb_pland.sh +
#                        patches/linux/0003). This SoC has NO CLINT/ACLINT (no
#                        such parameter exists on the 2026.1 IP — verified
#                        against the complete CONFIG.* space). Leaving C_USE_SSTC
#                        =1 is harmless (inert unless the DT re-advertises it —
#                        that IS the fixed-IP re-enable path). rdtime/Zicntr
#                        (C_USE_COUNTERS) stays the clocksource regardless.
#   C_INTERRUPT_WAKEUP 1 [WFI-COMA FIX, PROVEN ON SILICON 2026-07-16] IP
#                        DEFAULT IS 0 = a core sleeping after `wfi` NEVER wakes
#                        — not for an interrupt, not even for a debug halt
#                        (repro: src/linux_soc/fw_mspin — interrupts deliver
#                        perfectly while executing, wfi is a coma). Linux idles
#                        in wfi, so default 0 = hang at first idle. AMD's own
#                        "Linux (RV64IMAFDC)" preset is the only preset setting
#                        this, and it sets 1. Known benign side effect (the
#                        IP's bd.tcl, by design): C_USE_SLEEP auto-derives to 1
#                        and, with the 2-bit Wakeup input left unconnected,
#                        C_ASYNC_WAKEUP to 3 — same values AMD's flow produces;
#                        Sleep/Wakeup status pins stay deliberately unwired.

# MDM (RISC-V), plain JTAG-BSCAN — replaces mdm_1 (contract §9.2.4). Coexists
# with debug_bridge_0's internally-bonded BSCAN exactly as mdm_1 did.
set mdm [_create_ip xilinx.com:ip:mdm_riscv:1.0 mdm_riscv_0]
connect_bd_intf_net [get_bd_intf_pins $mbv/DEBUG] [get_bd_intf_pins $mdm/MBDEBUG_0]

# LMB boot memory: 128 KiB shared ILMB/DLMB BRAM at the reset vector
# ([MBV-LESSON] sizing — a jump stub up to a small SREC first-stage; the
# donor's 1 MiB firmware-RAM rationale (core + 2x clearing) moves to the
# Linux service design, contract §9.6).
#
# [FIX-D] BOOT-BRAM PROVISIONING — OWNERSHIP DECIDED (normative statement in
# ADDRESS_MAP.md §6; summary here so nobody re-litigates it at the cell):
#   * The HW track OWNS the BRAM's baked-in image: a diagnostic PARK STUB
#     (linux_soc fw_dbg/bootstub.* is the proven donor — banner + calib bit on
#     the UARTLITE, then spin; it must never jump into DDR on its own). Baked
#     via blk_mem_gen COE at BD build / updatemem+MMI post-route. An all-zero
#     BRAM is tolerable (0x00000000 = illegal insn -> trap loop at the 0x0
#     trap base, harmless) but NOT the deliverable: the stub is what makes a
#     dead bench distinguishable from a dead DDR remotely.
#   * The Linux payload chain (fw_jump.bin / Image / DTB / initrd) is NEVER in
#     this BRAM (fw_jump.elf alone is 276 KiB > 128 KiB, and it is linked at
#     vaddr 0x0 = THIS BRAM — the "never `elf load`" trap): the boot track
#     OWNS the XSDB load-to-DDR recipe (mk_dtb.sh: dow -data to 0x8000_0000+,
#     rwr a0/a1/pc, con) — proven on silicon 2026-07-16.
#   * A self-booting first stage (SREC/ethernet/QSPI loader in BRAM) is a
#     future, separately-owned work item — NOT implied by this BD.
set ilmb   [_create_ip xilinx.com:ip:lmb_v10:3.0 ilmb_v10]
set dlmb   [_create_ip xilinx.com:ip:lmb_v10:3.0 dlmb_v10]
set ilmb_c [_create_ip xilinx.com:ip:lmb_bram_if_cntlr:4.0 ilmb_bram_if_cntlr]
set dlmb_c [_create_ip xilinx.com:ip:lmb_bram_if_cntlr:4.0 dlmb_bram_if_cntlr]
set_property CONFIG.C_ECC {0} $ilmb_c
set_property CONFIG.C_ECC {0} $dlmb_c

set lmb_bram [_create_ip xilinx.com:ip:blk_mem_gen:8.4 lmb_bram]
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
] $lmb_bram

connect_bd_intf_net [get_bd_intf_pins $mbv/ILMB]         [get_bd_intf_pins $ilmb/LMB_M]
connect_bd_intf_net [get_bd_intf_pins $mbv/DLMB]         [get_bd_intf_pins $dlmb/LMB_M]
connect_bd_intf_net [get_bd_intf_pins $ilmb/LMB_Sl_0]    [get_bd_intf_pins $ilmb_c/SLMB]
connect_bd_intf_net [get_bd_intf_pins $dlmb/LMB_Sl_0]    [get_bd_intf_pins $dlmb_c/SLMB]
connect_bd_intf_net [get_bd_intf_pins $ilmb_c/BRAM_PORT] [get_bd_intf_pins $lmb_bram/BRAM_PORTA]
connect_bd_intf_net [get_bd_intf_pins $dlmb_c/BRAM_PORT] [get_bd_intf_pins $lmb_bram/BRAM_PORTB]

# CPU + LMB clock/reset: shell_clk [DEV-3] + the calib-gated mb_reset [DEV-4].
# LMB uses the active-HIGH reset convention -> feed mb_reset, NOT a
# peripheral_aresetn (donor-confirmed [BD 41-238] polarity bug otherwise).
connect_bd_net $shell_clk [get_bd_pins $mbv/Clk]
connect_bd_net $mb_reset  [get_bd_pins $mbv/Reset]
foreach p [list $ilmb $dlmb $ilmb_c $dlmb_c] {
    connect_bd_net $shell_clk [get_bd_pins $p/LMB_Clk] -quiet
    connect_bd_net $mb_reset  [get_bd_pins $p/SYS_Rst] -quiet
    connect_bd_net $mb_reset  [get_bd_pins $p/LMB_Rst] -quiet
}

###############################################################################
# SECTION 4 — INTC / TIMER / UARTLITE (shell housekeeping trio, Linux-pinned)
###############################################################################

set axi_intc_0 [_create_ip xilinx.com:ip:axi_intc:4.1 axi_intc_0]
# [DEV-5] Linux irq-xilinx-intc contract (donor left defaults). All four
# sources are level: hwicap ip2intc_irpt (IER/ISR level block), timer (held
# until T0INT W1C), uartlite (FIFO state), eth_irq (LAN9220 IRQ_CFG must be
# programmed push-pull ACTIVE-HIGH by software — driver obligation, flagged in
# ADDRESS_MAP.md; the shell firmware polled and never enabled it).
#
# [FIX-C] kind-of-intr manual-vs-computed — RESOLVED, expected warning:
# Vivado propagation computes C_KIND_OF_INTR from each source pin's
# SENSITIVITY metadata; the uartlite's `interrupt` is metadata'd EDGE_RISING
# (a 1-cycle pulse), so propagation may warn "manual value (0x00000000) does
# not match computed value" (seen live on linux_soc: build_dbg_bd.log:1379).
# That warning is EXPECTED AND BENIGN here — do NOT "fix" it by adopting the
# computed word. Settled by the linux_soc IRQ audit
# (../linux_soc/hw/build_dbg/IRQ_WIRING_AUDIT.md §2, source-verified against
# axi_intc_v4_1_rfs.vhd ~line 1964): in LEVEL mode axi_intc's LVL_P detector
# LATCHES — hw_intr(i) sets when the (synchronised) input is active and HOLDS
# until IAR ack — and same-clock inputs get no synchroniser that could swallow
# a pulse, so a 1-cycle edge pulse is captured and held even in LEVEL mode,
# and the Linux level flow (mask -> handle -> ack) cannot lose it. Forcing
# all-level keeps ONE irq_chip flow for all four lines and matches the DTS
# (`xlnx,kind-of-intr = <0x0>`), which MUST track this value. Guarded twice:
# the _assert_cfg below (pre-validate) and a POST-validate re-assert in
# SECTION 9 (so a propagation override could never ship silently).
set intc_cfg [list \
    CONFIG.C_KIND_OF_INTR {0x00000000} \
    CONFIG.C_IRQ_IS_LEVEL {1} \
    CONFIG.C_IRQ_ACTIVE   {0x1} \
    CONFIG.C_HAS_FAST     {0} \
]
set_property -dict $intc_cfg $axi_intc_0
_assert_cfg $axi_intc_0 $intc_cfg "axi_intc_0 (level-sensitive, no fast irq) \[DEV-5\]"
# C_NUM_INTR_INPUTS is READ-ONLY (set_property is a silent no-op) — derived
# from the xlconcat width during validate's propagation; asserted ==4 in
# SECTION 9 ([MBV-LESSON]).
connect_bd_net $shell_clk     [get_bd_pins $axi_intc_0/s_axi_aclk]
connect_bd_net $shell_aresetn [get_bd_pins $axi_intc_0/s_axi_aresetn]

set xlconcat_intr [_create_ip xilinx.com:ip:xlconcat:2.1 xlconcat_intr]
set_property CONFIG.NUM_PORTS {4} $xlconcat_intr
connect_bd_net [get_bd_pins $xlconcat_intr/dout] [get_bd_pins $axi_intc_0/intr]
# INTERRUPT is a BUS INTERFACE on microblaze_riscv (mbinterrupt_rtl), and
# axi_intc's `interrupt` output carries the same VLNV — interface-to-interface
# ([MBV-LESSON]; the donor's scalar irq->INTERRUPT net was the classic-MB form).
connect_bd_intf_net [get_bd_intf_pins $axi_intc_0/interrupt] [get_bd_intf_pins $mbv/INTERRUPT]

set axi_timer_0 [_create_ip xilinx.com:ip:axi_timer:2.0 axi_timer_0]
# [DEV-6] both counters pinned. Channel 0 is THE KERNEL CLOCKEVENT via the
# "soclabs,mbv-timer" errata driver (silicon errata E1/E2: architected Sstc
# stimecmp is dead / STIP stuck — the tick is delivered on SEIP through
# axi_intc In1 instead; DTS shell_linux.dts + linux_soc patches/linux/0003).
# The clocksource is rdtime/Zicntr (C_USE_COUNTERS), NOT counter 1. enable_
# timer2=1 (both counters) is the same value the donor inherited — kept loud;
# the driver only uses channel 0, channel 1 is spare. The INTC In1 input MUST
# stay LEVEL (C_KIND_OF_INTR=0) — the driver's ISR ack sequence depends on it.
set timer_cfg [list CONFIG.enable_timer2 {1} CONFIG.COUNT_WIDTH {32} CONFIG.mode_64bit {0}]
set_property -dict $timer_cfg $axi_timer_0
_assert_cfg $axi_timer_0 $timer_cfg "axi_timer_0 (kernel clockevent on SEIP via soclabs,mbv-timer) \[DEV-6\]"

set axi_uartlite_0 [_create_ip xilinx.com:ip:axi_uartlite:2.0 axi_uartlite_0]
set uart_cfg [list CONFIG.C_BAUDRATE {115200} CONFIG.C_DATA_BITS {8} CONFIG.C_USE_PARITY {0}]
set_property -dict $uart_cfg $axi_uartlite_0
_assert_cfg $axi_uartlite_0 $uart_cfg "axi_uartlite_0 (console fallback, 115200 8N1)"
connect_bd_net [get_bd_ports uart_tx_f] [get_bd_pins $axi_uartlite_0/tx]
connect_bd_net [get_bd_ports uart_rx_f] [get_bd_pins $axi_uartlite_0/rx]

# Donor lesson: the slave IP's own s_axi_aclk/aresetn are separate nets from
# the interconnect's per-master M<NN>_ACLK pins — wire BOTH.
connect_bd_net $shell_clk     [get_bd_pins $axi_timer_0/s_axi_aclk]
connect_bd_net $shell_aresetn [get_bd_pins $axi_timer_0/s_axi_aresetn]
connect_bd_net $shell_clk     [get_bd_pins $axi_uartlite_0/s_axi_aclk]
connect_bd_net $shell_aresetn [get_bd_pins $axi_uartlite_0/s_axi_aresetn]

###############################################################################
# SECTION 5 — CUSTOM SHELL CSR IP (eight packaged soclabs components) +
# THE DFX DECOUPLER BOUNDARY. Donor SECTION 3 carried VERBATIM.
# Decode-width law: C_S_AXI_ADDR_WIDTH {32} on ALL EIGHT — the RTL default of
# 12 never ships (contract §2 normative lesson).
###############################################################################

set dut_clkrst_0 [create_bd_cell -type ip -vlnv soclabs.org:user:dut_clkrst:1.0 dut_clkrst_0]
set_property CONFIG.C_S_AXI_ADDR_WIDTH {32} $dut_clkrst_0
set dfx_ctl_0 [create_bd_cell -type ip -vlnv soclabs.org:user:dfx_ctl:1.0 dfx_ctl_0]
set_property CONFIG.C_S_AXI_ADDR_WIDTH {32} $dfx_ctl_0
set board_gpio_0 [create_bd_cell -type ip -vlnv soclabs.org:user:board_gpio:1.0 board_gpio_0]
set_property CONFIG.C_S_AXI_ADDR_WIDTH {32} $board_gpio_0
# [DEV-10] jtag_bb replaces swd_bb. NOT a rename: the CSR contract changes.
# SWDBB DRIVE was {swclk, swdio_o, swdio_oe} with SAMPLE {swdio_i} reading back
# the one bidirectional wire; JTAGBB DRIVE is {tck, tms, tdi} with SAMPLE {tdo}
# reading a SEPARATE wire, so there is no turnaround and no output-enable. Same
# 64 KiB page, same bit positions 0/1/2 — see fpga/shell/ip/jtag_bb/README.md.
set jtag_bb_0 [create_bd_cell -type ip -vlnv soclabs.org:user:jtag_bb:1.0 jtag_bb_0]
set_property CONFIG.C_S_AXI_ADDR_WIDTH {32} $jtag_bb_0
set telem_0 [create_bd_cell -type ip -vlnv soclabs.org:user:telem:1.0 telem_0]
set_property -dict [list CONFIG.C_S_AXI_ADDR_WIDTH {32} CONFIG.SIM_FAKE_DATA {0}] $telem_0
set uart_bridge_0 [create_bd_cell -type ip -vlnv soclabs.org:user:uart_bridge:1.0 uart_bridge_0]
set_property CONFIG.C_S_AXI_ADDR_WIDTH {32} $uart_bridge_0
set clcd_0 [create_bd_cell -type ip -vlnv soclabs.org:user:clcd:1.0 clcd_0]
set_property CONFIG.C_S_AXI_ADDR_WIDTH {32} $clcd_0
set clcd_kvm_0 [create_bd_cell -type ip -vlnv soclabs.org:user:clcd_kvm:1.0 clcd_kvm_0]
set_property CONFIG.C_S_AXI_ADDR_WIDTH {32} $clcd_kvm_0

# Width-32 read-back is load-bearing (silicon-killing regression class).
foreach {c n} [list $dut_clkrst_0 dut_clkrst_0 $dfx_ctl_0 dfx_ctl_0 $board_gpio_0 board_gpio_0 \
               $jtag_bb_0 jtag_bb_0 $telem_0 telem_0 $uart_bridge_0 uart_bridge_0 \
               $clcd_0 clcd_0 $clcd_kvm_0 clcd_kvm_0] {
    _assert_cfg $c [list CONFIG.C_S_AXI_ADDR_WIDTH 32] "$n C_S_AXI_ADDR_WIDTH=32 (decode-width law)"
}

foreach csr [list $dut_clkrst_0 $dfx_ctl_0 $board_gpio_0 $jtag_bb_0 $telem_0 $uart_bridge_0 $clcd_0 $clcd_kvm_0] {
    connect_bd_net $shell_clk     [get_bd_pins $csr/s_axi_aclk]
    connect_bd_net $shell_aresetn [get_bd_pins $csr/s_axi_aresetn]
}

# ==== DFX DECOUPLER — the frozen 19-interface boundary, VERBATIM ============
# ALL_PARAMS authored directly in Tcl. Every DECOUPLED_VALUE is a "0x" HEX
# STRING — a bare integer passes validate_bd_design and fails at IP generation
# an hour into a build (donor-confirmed). qspi_csn=0x1 is the SOLE non-zero
# clamp (flash DESELECTED during swap). validate is NOT a sufficient gate for
# this cell: the parent driver runs `generate_target synthesis` on the
# decoupler alone as the cheap check (contract §9.6).
# NB: braced literal dict — NO `#` comments inside the braces.
set dfx_decoupler_0 [_create_ip xilinx.com:ip:dfx_decoupler:1.0 dfx_decoupler_0]
set dfx_decoupler_boundary {
  INTF {
    jtag_tdo       { ID 0  VLNV xilinx.com:signal:data_rtl:1.0 MODE master SIGNALS { DATA { PRESENT 1 WIDTH 1  MANAGEMENT manual DECOUPLED_VALUE 0x0 } } }
    phy_rmii_txd   { ID 1  VLNV xilinx.com:signal:data_rtl:1.0 MODE master SIGNALS { DATA { PRESENT 1 WIDTH 2  MANAGEMENT manual DECOUPLED_VALUE 0x0 } } }
    phy_rmii_tx_en { ID 2  VLNV xilinx.com:signal:data_rtl:1.0 MODE master SIGNALS { DATA { PRESENT 1 WIDTH 1  MANAGEMENT manual DECOUPLED_VALUE 0x0 } } }
    mdc            { ID 3  VLNV xilinx.com:signal:data_rtl:1.0 MODE master SIGNALS { DATA { PRESENT 1 WIDTH 1  MANAGEMENT manual DECOUPLED_VALUE 0x0 } } }
    mdio_o         { ID 4  VLNV xilinx.com:signal:data_rtl:1.0 MODE master SIGNALS { DATA { PRESENT 1 WIDTH 1  MANAGEMENT manual DECOUPLED_VALUE 0x0 } } }
    mdio_oe        { ID 5  VLNV xilinx.com:signal:data_rtl:1.0 MODE master SIGNALS { DATA { PRESENT 1 WIDTH 1  MANAGEMENT manual DECOUPLED_VALUE 0x0 } } }
    uart_tx_tdata  { ID 6  VLNV xilinx.com:signal:data_rtl:1.0 MODE master SIGNALS { DATA { PRESENT 1 WIDTH 8  MANAGEMENT manual DECOUPLED_VALUE 0x0 } } }
    uart_tx_tvalid { ID 7  VLNV xilinx.com:signal:data_rtl:1.0 MODE master SIGNALS { DATA { PRESENT 1 WIDTH 1  MANAGEMENT manual DECOUPLED_VALUE 0x0 } } }
    uart_rx_tready { ID 8  VLNV xilinx.com:signal:data_rtl:1.0 MODE master SIGNALS { DATA { PRESENT 1 WIDTH 1  MANAGEMENT manual DECOUPLED_VALUE 0x0 } } }
    swo            { ID 9  VLNV xilinx.com:signal:data_rtl:1.0 MODE master SIGNALS { DATA { PRESENT 1 WIDTH 1  MANAGEMENT manual DECOUPLED_VALUE 0x0 } } }
    rm_id          { ID 10 VLNV xilinx.com:signal:data_rtl:1.0 MODE master SIGNALS { DATA { PRESENT 1 WIDTH 32 MANAGEMENT manual DECOUPLED_VALUE 0x0 } } }
    dut_lockup     { ID 11 VLNV xilinx.com:signal:data_rtl:1.0 MODE master SIGNALS { DATA { PRESENT 1 WIDTH 1  MANAGEMENT manual DECOUPLED_VALUE 0x0 } } }
    irq_out        { ID 12 VLNV xilinx.com:signal:data_rtl:1.0 MODE master SIGNALS { DATA { PRESENT 1 WIDTH 1  MANAGEMENT manual DECOUPLED_VALUE 0x0 } } }
    dut_gpio_o     { ID 13 VLNV xilinx.com:signal:data_rtl:1.0 MODE master SIGNALS { DATA { PRESENT 1 WIDTH 16 MANAGEMENT manual DECOUPLED_VALUE 0x0 } } }
    dut_gpio_oe    { ID 14 VLNV xilinx.com:signal:data_rtl:1.0 MODE master SIGNALS { DATA { PRESENT 1 WIDTH 16 MANAGEMENT manual DECOUPLED_VALUE 0x0 } } }
    qspi_sclk      { ID 15 VLNV xilinx.com:signal:data_rtl:1.0 MODE master SIGNALS { DATA { PRESENT 1 WIDTH 1 MANAGEMENT manual DECOUPLED_VALUE 0x0 } } }
    qspi_csn       { ID 16 VLNV xilinx.com:signal:data_rtl:1.0 MODE master SIGNALS { DATA { PRESENT 1 WIDTH 1 MANAGEMENT manual DECOUPLED_VALUE 0x1 } } }
    qspi_io_o      { ID 17 VLNV xilinx.com:signal:data_rtl:1.0 MODE master SIGNALS { DATA { PRESENT 1 WIDTH 4 MANAGEMENT manual DECOUPLED_VALUE 0x0 } } }
    qspi_io_oe     { ID 18 VLNV xilinx.com:signal:data_rtl:1.0 MODE master SIGNALS { DATA { PRESENT 1 WIDTH 4 MANAGEMENT manual DECOUPLED_VALUE 0x0 } } }
  }
}
set_property CONFIG.ALL_PARAMS $dfx_decoupler_boundary $dfx_decoupler_0

# ==== CLKRST wiring (verbatim) ==============================================
set gnd_drp_do [_create_ip xilinx.com:ip:xlconstant:1.1 gnd_drp_do]
set_property -dict [list CONFIG.CONST_WIDTH {16} CONFIG.CONST_VAL {0}] $gnd_drp_do
set gnd_drp_drdy [_create_ip xilinx.com:ip:xlconstant:1.1 gnd_drp_drdy]
set_property CONFIG.CONST_VAL {0} $gnd_drp_drdy
connect_bd_net [get_bd_pins $gnd_drp_do/dout]   [get_bd_pins $dut_clkrst_0/drp_do_i]
connect_bd_net [get_bd_pins $gnd_drp_drdy/dout] [get_bd_pins $dut_clkrst_0/drp_drdy_i]
connect_bd_net [get_bd_pins $clk_wiz_dut/locked]         [get_bd_pins $dut_clkrst_0/mmcm_locked_i]
connect_bd_net $dut_clk_sig                              [get_bd_pins $dut_clkrst_0/dut_clk_i]
connect_bd_net [get_bd_ports sys_rst_n]                  [get_bd_pins $dut_clkrst_0/ext_por_n_i]
connect_bd_net [get_bd_pins $dfx_ctl_0/rp_resetn_gate_o] [get_bd_pins $dut_clkrst_0/rp_resetn_gate_i]
set gnd0 [_create_ip xilinx.com:ip:xlconstant:1.1 gnd_dbg_req]
set_property CONFIG.CONST_VAL {0} $gnd0
connect_bd_net [get_bd_pins $gnd0/dout] [get_bd_pins $dut_clkrst_0/dbg_reset_req_i]
connect_bd_net [get_bd_pins $dut_clkrst_0/dut_resetn_o] [get_bd_ports rp_dut_resetn]
connect_bd_net [get_bd_pins $dut_clkrst_0/rp_resetn_o]  [get_bd_ports rp_rp_resetn]
connect_bd_net [get_bd_pins $dut_clkrst_0/dbg_resetn_o] [get_bd_ports rp_dbg_resetn]

# ==== DFXCTL wiring: RM-verify taps THROUGH the decoupler (verbatim) ========
connect_bd_net [get_bd_ports rp_rm_id]                          [get_bd_pins $dfx_decoupler_0/rp_rm_id_DATA]
connect_bd_net [get_bd_pins $dfx_decoupler_0/s_rm_id_DATA]      [get_bd_pins $dfx_ctl_0/rm_id_i]
connect_bd_net [get_bd_ports rp_dut_lockup]                     [get_bd_pins $dfx_decoupler_0/rp_dut_lockup_DATA]
connect_bd_net [get_bd_pins $dfx_decoupler_0/s_dut_lockup_DATA] [get_bd_pins $dfx_ctl_0/dut_lockup_i]
set inv_rp_in_reset [_create_ip xilinx.com:ip:util_vector_logic:2.0 inv_rp_in_reset]
set_property -dict [list CONFIG.C_SIZE {1} CONFIG.C_OPERATION {not}] $inv_rp_in_reset
connect_bd_net [get_bd_pins $dut_clkrst_0/rp_resetn_o] [get_bd_pins $inv_rp_in_reset/Op1]
connect_bd_net [get_bd_pins $inv_rp_in_reset/Res]      [get_bd_pins $dfx_ctl_0/rp_in_reset_i]

# ==== JTAGBB wiring (verbatim vs the donor, shell_bd.tcl SECTION 3) =========
# [DEV-10]. Three shell->RP drives straight out (a drive to a dead RP is
# harmless, so they are NOT decoupler members); the ONE RP->shell leg, jtag_tdo,
# goes through the decoupler exactly as swd_dio_i did — same ID 0, same width 1,
# same 0x0 clamp (TDO idle low during a swap).
connect_bd_net [get_bd_pins $jtag_bb_0/jtag_tck_o] [get_bd_ports rp_jtag_tck]
connect_bd_net [get_bd_pins $jtag_bb_0/jtag_tms_o] [get_bd_ports rp_jtag_tms]
connect_bd_net [get_bd_pins $jtag_bb_0/jtag_tdi_o] [get_bd_ports rp_jtag_tdi]
connect_bd_net [get_bd_ports rp_jtag_tdo]                     [get_bd_pins $dfx_decoupler_0/rp_jtag_tdo_DATA]
connect_bd_net [get_bd_pins $dfx_decoupler_0/s_jtag_tdo_DATA] [get_bd_pins $jtag_bb_0/jtag_tdo_i]

# ==== UARTBR wiring (verbatim; uart_bridge owns the dut_clk<->shell CDC) ====
connect_bd_net $dut_clk_sig [get_bd_pins $uart_bridge_0/dut_clk_i]
connect_bd_net [get_bd_ports rp_uart_tx_tdata]                      [get_bd_pins $dfx_decoupler_0/rp_uart_tx_tdata_DATA]
connect_bd_net [get_bd_pins $dfx_decoupler_0/s_uart_tx_tdata_DATA]  [get_bd_pins $uart_bridge_0/uart_tx_tdata_i]
connect_bd_net [get_bd_ports rp_uart_tx_tvalid]                     [get_bd_pins $dfx_decoupler_0/rp_uart_tx_tvalid_DATA]
connect_bd_net [get_bd_pins $dfx_decoupler_0/s_uart_tx_tvalid_DATA] [get_bd_pins $uart_bridge_0/uart_tx_tvalid_i]
connect_bd_net [get_bd_ports rp_uart_rx_tready]                     [get_bd_pins $dfx_decoupler_0/rp_uart_rx_tready_DATA]
connect_bd_net [get_bd_pins $dfx_decoupler_0/s_uart_rx_tready_DATA] [get_bd_pins $uart_bridge_0/uart_rx_tready_i]
connect_bd_net [get_bd_ports rp_swo]                                [get_bd_pins $dfx_decoupler_0/rp_swo_DATA]
connect_bd_net [get_bd_pins $dfx_decoupler_0/s_swo_DATA]            [get_bd_pins $uart_bridge_0/swo_i]
connect_bd_net [get_bd_pins $uart_bridge_0/uart_tx_tready_o] [get_bd_ports rp_uart_tx_tready]
connect_bd_net [get_bd_pins $uart_bridge_0/uart_rx_tdata_o]  [get_bd_ports rp_uart_rx_tdata]
connect_bd_net [get_bd_pins $uart_bridge_0/uart_rx_tvalid_o] [get_bd_ports rp_uart_rx_tvalid]
set gnd8 [_create_ip xilinx.com:ip:xlconstant:1.1 gnd_uart1_tx]
set_property -dict [list CONFIG.CONST_WIDTH {8} CONFIG.CONST_VAL {0}] $gnd8
set gnd1 [_create_ip xilinx.com:ip:xlconstant:1.1 gnd_uart1_v]
set_property CONFIG.CONST_VAL {0} $gnd1
set vcc1 [_create_ip xilinx.com:ip:xlconstant:1.1 vcc_uart1_rdy]
set_property CONFIG.CONST_VAL {1} $vcc1
connect_bd_net [get_bd_pins $gnd8/dout] [get_bd_pins $uart_bridge_0/uart1_tx_tdata_i]
connect_bd_net [get_bd_pins $gnd1/dout] [get_bd_pins $uart_bridge_0/uart1_tx_tvalid_i]
connect_bd_net [get_bd_pins $vcc1/dout] [get_bd_pins $uart_bridge_0/uart1_rx_tready_i]

# ==== Board GPIO wiring (verbatim) ==========================================
connect_bd_net [get_bd_ports rp_dut_gpio_o]                      [get_bd_pins $dfx_decoupler_0/rp_dut_gpio_o_DATA]
connect_bd_net [get_bd_pins $dfx_decoupler_0/s_dut_gpio_o_DATA]  [get_bd_pins $board_gpio_0/dut_gpio_o_i]
connect_bd_net [get_bd_ports rp_dut_gpio_oe]                     [get_bd_pins $dfx_decoupler_0/rp_dut_gpio_oe_DATA]
connect_bd_net [get_bd_pins $dfx_decoupler_0/s_dut_gpio_oe_DATA] [get_bd_pins $board_gpio_0/dut_gpio_oe_i]
connect_bd_net [get_bd_pins $board_gpio_0/dut_gpio_i_o] [get_bd_ports rp_dut_gpio_i]
connect_bd_net [get_bd_pins $board_gpio_0/board_pad_o]  [get_bd_ports board_gpio_pad_o]
connect_bd_net [get_bd_pins $board_gpio_0/board_pad_oe] [get_bd_ports board_gpio_pad_oe]
connect_bd_net [get_bd_ports board_gpio_pad_i]          [get_bd_pins $board_gpio_0/board_pad_i]

# ==== QSPI XiP wiring (verbatim, v0.2 boundary) =============================
connect_bd_net [get_bd_ports rp_qspi_sclk]                      [get_bd_pins $dfx_decoupler_0/rp_qspi_sclk_DATA]
connect_bd_net [get_bd_pins $dfx_decoupler_0/s_qspi_sclk_DATA]  [get_bd_ports qspi_pad_sclk]
connect_bd_net [get_bd_ports rp_qspi_csn]                       [get_bd_pins $dfx_decoupler_0/rp_qspi_csn_DATA]
connect_bd_net [get_bd_pins $dfx_decoupler_0/s_qspi_csn_DATA]   [get_bd_ports qspi_pad_csn]
connect_bd_net [get_bd_ports rp_qspi_io_o]                      [get_bd_pins $dfx_decoupler_0/rp_qspi_io_o_DATA]
connect_bd_net [get_bd_pins $dfx_decoupler_0/s_qspi_io_o_DATA]  [get_bd_ports qspi_pad_io_o]
connect_bd_net [get_bd_ports rp_qspi_io_oe]                     [get_bd_pins $dfx_decoupler_0/rp_qspi_io_oe_DATA]
connect_bd_net [get_bd_pins $dfx_decoupler_0/s_qspi_io_oe_DATA] [get_bd_ports qspi_pad_io_oe]

# ==== CLCD-KVM wiring (verbatim, Wave 4) ====================================
connect_bd_net [get_bd_pins $clcd_0/clcd_pd_o]    [get_bd_pins $clcd_kvm_0/h_pd_o]
connect_bd_net [get_bd_pins $clcd_0/clcd_pd_oe]   [get_bd_pins $clcd_kvm_0/h_pd_oe]
connect_bd_net [get_bd_pins $clcd_0/clcd_cs_n_o]  [get_bd_pins $clcd_kvm_0/h_cs_n]
connect_bd_net [get_bd_pins $clcd_0/clcd_wr_n_o]  [get_bd_pins $clcd_kvm_0/h_wr_n]
connect_bd_net [get_bd_pins $clcd_0/clcd_rd_n_o]  [get_bd_pins $clcd_kvm_0/h_rd_n]
connect_bd_net [get_bd_pins $clcd_0/clcd_rs_o]    [get_bd_pins $clcd_kvm_0/h_rs]
connect_bd_net [get_bd_pins $clcd_0/clcd_bl_o]    [get_bd_pins $clcd_kvm_0/h_bl]
connect_bd_net [get_bd_pins $clcd_0/clcd_rst_n_o] [get_bd_pins $clcd_kvm_0/h_rst_n]
connect_bd_net [get_bd_pins $clcd_0/busy_o]       [get_bd_pins $clcd_kvm_0/h_busy]
connect_bd_net [get_bd_pins $clcd_0/fifo_empty_o] [get_bd_pins $clcd_kvm_0/h_fifo_empty]
connect_bd_net [get_bd_pins $clcd_kvm_0/h_pd_i]   [get_bd_pins $clcd_0/clcd_pd_i]
# SOURCE B: display tunnel — fan-out TAP of the post-decoupler dut_gpio nets.
connect_bd_net [get_bd_pins $dfx_decoupler_0/s_dut_gpio_o_DATA]  [get_bd_pins $clcd_kvm_0/dut_gpio_o_i]
connect_bd_net [get_bd_pins $dfx_decoupler_0/s_dut_gpio_oe_DATA] [get_bd_pins $clcd_kvm_0/dut_gpio_oe_i]
# DFX interlock: forced revert while decoupled / RP in reset.
connect_bd_net [get_bd_pins $dfx_decoupler_0/decouple_status] [get_bd_pins $clcd_kvm_0/decouple_status]
connect_bd_net [get_bd_pins $dut_clkrst_0/rp_resetn_o]        [get_bd_pins $clcd_kvm_0/rp_resetn]
connect_bd_net [get_bd_ports user_npb1] [get_bd_pins $clcd_kvm_0/user_npb1]
connect_bd_net [get_bd_pins $clcd_kvm_0/clcd_pd_o]    [get_bd_ports clcd_pd_o]
connect_bd_net [get_bd_ports clcd_pd_i]               [get_bd_pins $clcd_kvm_0/clcd_pd_i]
connect_bd_net [get_bd_pins $clcd_kvm_0/clcd_pd_oe]   [get_bd_ports clcd_pd_oe]
connect_bd_net [get_bd_pins $clcd_kvm_0/clcd_cs_n_o]  [get_bd_ports clcd_cs_n]
connect_bd_net [get_bd_pins $clcd_kvm_0/clcd_wr_n_o]  [get_bd_ports clcd_wr_n]
connect_bd_net [get_bd_pins $clcd_kvm_0/clcd_rd_n_o]  [get_bd_ports clcd_rd_n]
connect_bd_net [get_bd_pins $clcd_kvm_0/clcd_rs_o]    [get_bd_ports clcd_rs]
connect_bd_net [get_bd_pins $clcd_kvm_0/clcd_bl_o]    [get_bd_ports clcd_bl]
connect_bd_net [get_bd_pins $clcd_kvm_0/clcd_rst_n_o] [get_bd_ports clcd_rst_n]

# ==== TELEM wiring (verbatim; INA228 seam tied off) =========================
set gnd32 [_create_ip xilinx.com:ip:xlconstant:1.1 gnd_ina228_32]
set_property -dict [list CONFIG.CONST_WIDTH {32} CONFIG.CONST_VAL {0}] $gnd32
set gnd1b [_create_ip xilinx.com:ip:xlconstant:1.1 gnd_ina228_1]
set_property CONFIG.CONST_VAL {0} $gnd1b
foreach sig {ina228_bus_mv_i ina228_curr_ua_i ina228_power_mw_i} {
    connect_bd_net [get_bd_pins $gnd32/dout] [get_bd_pins $telem_0/$sig]
}
foreach sig {ina228_sample_valid_i ina228_i2c_err_i alarm_i} {
    connect_bd_net [get_bd_pins $gnd1b/dout] [get_bd_pins $telem_0/$sig]
}
connect_bd_net [get_bd_pins $gnd1b/dout] [get_bd_ports i2c_scl_o] -quiet
connect_bd_net [get_bd_pins $gnd1b/dout] [get_bd_ports i2c_scl_t] -quiet
connect_bd_net [get_bd_pins $gnd1b/dout] [get_bd_ports i2c_sda_o] -quiet
connect_bd_net [get_bd_pins $gnd1b/dout] [get_bd_ports i2c_sda_t] -quiet
# i2c_sda_i: documented, expected dangling BD input (no consumer yet).

###############################################################################
# SECTION 6 — DFX ISOLATION + VENDOR CONFIG/DEBUG/STORAGE IP (donor SECTION 4
# verbatim: HWICAP FIFO-mode, shutdown manager, vestigial OVLSTORE, DBGBR,
# slow-timed AXI EMC)
###############################################################################

set axi_hwicap_0 [_create_ip xilinx.com:ip:axi_hwicap:3.0 axi_hwicap_0]
set hwicap_cfg [list CONFIG.C_ICAP_DWIDTH {32} CONFIG.C_MODE {0} CONFIG.C_WRITE_FIFO_DEPTH {1024}]
set_property -dict $hwicap_cfg $axi_hwicap_0
_assert_cfg $axi_hwicap_0 $hwicap_cfg "axi_hwicap_0 (FIFO mode, 1024-word write FIFO — the swap-path contract)"
connect_bd_net $shell_clk     [get_bd_pins $axi_hwicap_0/s_axi_aclk]
connect_bd_net $shell_aresetn [get_bd_pins $axi_hwicap_0/s_axi_aresetn]
connect_bd_net $shell_clk     [get_bd_pins $axi_hwicap_0/icap_clk] -quiet

set dfx_axi_shutdown_manager_0 [_create_ip xilinx.com:ip:dfx_axi_shutdown_manager:1.0 dfx_axi_shutdown_manager_0]
# S_AXI/M_AXI intentionally unconnected — the reserved v1+ MMIO-bridge slot.
connect_bd_net [get_bd_pins $dfx_ctl_0/decouple_en_o]        [get_bd_pins $dfx_decoupler_0/decouple]
connect_bd_net [get_bd_pins $dfx_decoupler_0/decouple_status] [get_bd_pins $dfx_ctl_0/decoupled_i]
connect_bd_net [get_bd_pins $dfx_ctl_0/axi_shutdown_req_o]   [get_bd_pins $dfx_axi_shutdown_manager_0/request_shutdown]
connect_bd_net [get_bd_pins $dfx_axi_shutdown_manager_0/shutdown_requested] [get_bd_pins $dfx_ctl_0/axi_shutdown_ack_i] -quiet
connect_bd_net $shell_clk     [get_bd_pins $dfx_axi_shutdown_manager_0/clk]
connect_bd_net $shell_aresetn [get_bd_pins $dfx_axi_shutdown_manager_0/resetn]

# OVLSTORE (vestigial): register window only; SPI_0 external port REMOVED
# (QSPI v0.2, D16 — the RP owns the SST26 pads). Kept for regmap compat.
set axi_quad_spi_0 [_create_ip xilinx.com:ip:axi_quad_spi:3.2 axi_quad_spi_0]
set_property -dict [list \
    CONFIG.C_USE_STARTUP {0} \
    CONFIG.C_SPI_MODE {0} \
    CONFIG.C_SPI_MEMORY {0} \
    CONFIG.C_NUM_SS_BITS {1} \
] $axi_quad_spi_0
connect_bd_net $shell_clk     [get_bd_pins $axi_quad_spi_0/ext_spi_clk]
connect_bd_net $shell_clk     [get_bd_pins $axi_quad_spi_0/s_axi_aclk]
connect_bd_net $shell_aresetn [get_bd_pins $axi_quad_spi_0/s_axi_aresetn]

# Debug Bridge — AXI->BSCAN (XVC path; must survive swaps; stays in static).
set debug_bridge_0 [_create_ip xilinx.com:ip:debug_bridge:3.0 debug_bridge_0]
set_property CONFIG.C_DEBUG_MODE {2} $debug_bridge_0
_assert_cfg $debug_bridge_0 [list CONFIG.C_DEBUG_MODE 2] "debug_bridge_0 (AXI->BSCAN, XVC)"
connect_bd_net $shell_clk     [get_bd_pins $debug_bridge_0/s_axi_aclk]
connect_bd_net $shell_aresetn [get_bd_pins $debug_bridge_0/s_axi_aresetn]

# AXI EMC -> LAN9220. AC timing DELIBERATELY SLOW (donor lesson: the IP
# defaults under-sample the LAN9220's 30 ns read spec — do not "optimize";
# LAN9220 datasheet AC table still unverified, A6).
set axi_emc_0 [_create_ip xilinx.com:ip:axi_emc:3.0 axi_emc_0]
set emc_cfg [list \
    CONFIG.C_INCLUDE_NEGEDGE_IOREGS {0} \
    CONFIG.C_MEM0_TYPE {1} \
    CONFIG.C_MEM0_WIDTH {16} \
    CONFIG.C_TCEDV_PS_MEM_0 {50000} \
    CONFIG.C_TAVDV_PS_MEM_0 {50000} \
    CONFIG.C_THZCE_PS_MEM_0 {25000} \
    CONFIG.C_THZOE_PS_MEM_0 {25000} \
    CONFIG.C_TWC_PS_MEM_0 {100000} \
    CONFIG.C_TWP_PS_MEM_0 {50000} \
]
set_property -dict $emc_cfg $axi_emc_0
_assert_cfg $axi_emc_0 $emc_cfg "axi_emc_0 (LAN9220, 16-bit async SRAM, conservative AC timing)"
connect_bd_net $shell_clk     [get_bd_pins $axi_emc_0/s_axi_aclk]
connect_bd_net $shell_aresetn [get_bd_pins $axi_emc_0/s_axi_aresetn]
connect_bd_net $shell_clk     [get_bd_pins $axi_emc_0/rdclk]
connect_bd_intf_net [get_bd_intf_pins $axi_emc_0/EMC_INTF] [get_bd_intf_ports EMC_INTF]

# ==== Interrupt concat — SAME four sources, SAME order (contract §4) ========
connect_bd_net [get_bd_pins $axi_hwicap_0/ip2intc_irpt] [get_bd_pins $xlconcat_intr/In${IRQ_HWICAP}] -quiet
connect_bd_net [get_bd_pins $axi_timer_0/interrupt]     [get_bd_pins $xlconcat_intr/In${IRQ_TIMER}]
connect_bd_net [get_bd_pins $axi_uartlite_0/interrupt]  [get_bd_pins $xlconcat_intr/In${IRQ_UART}]
connect_bd_net [get_bd_ports eth_irq]                   [get_bd_pins $xlconcat_intr/In${IRQ_ETH}]
# rp_irq_out: STILL deliberately NOT an interrupt (contract §4 — dut_clk-domain
# CDC hazard raw into a shell-clk INTC; decoupler s_*_DATA is data_rtl which
# xlconcat rejects [xlconcat-10]; no consumer contract yet). Terminated at the
# decoupler clamp; s_irq_out_DATA is the documented dangling tie-off. Future
# recipe: 2-FF ASYNC_REG sync + regmap entry + spare INTC line.
connect_bd_net [get_bd_ports rp_irq_out] [get_bd_pins $dfx_decoupler_0/rp_irq_out_DATA]

# ==== Deferred RMII/MDIO group (verbatim: safe idle + pre-authored clamps) ==
set gnd2 [_create_ip xilinx.com:ip:xlconstant:1.1 gnd_rmii2]
set_property -dict [list CONFIG.CONST_WIDTH {2} CONFIG.CONST_VAL {0}] $gnd2
set gnd1c [_create_ip xilinx.com:ip:xlconstant:1.1 gnd_rmii1]
set_property CONFIG.CONST_VAL {0} $gnd1c
connect_bd_net [get_bd_pins $gnd1c/dout] [get_bd_ports rp_phy_rmii_crs_dv]
connect_bd_net [get_bd_pins $gnd2/dout]  [get_bd_ports rp_phy_rmii_rxd]
connect_bd_net [get_bd_pins $gnd1c/dout] [get_bd_ports rp_mdio_i]
connect_bd_net [get_bd_ports rp_phy_rmii_txd]   [get_bd_pins $dfx_decoupler_0/rp_phy_rmii_txd_DATA]
connect_bd_net [get_bd_ports rp_phy_rmii_tx_en] [get_bd_pins $dfx_decoupler_0/rp_phy_rmii_tx_en_DATA]
connect_bd_net [get_bd_ports rp_mdc]            [get_bd_pins $dfx_decoupler_0/rp_mdc_DATA]
connect_bd_net [get_bd_ports rp_mdio_o]         [get_bd_pins $dfx_decoupler_0/rp_mdio_o_DATA]
connect_bd_net [get_bd_ports rp_mdio_oe]        [get_bd_pins $dfx_decoupler_0/rp_mdio_oe_DATA]
# s_phy_rmii_*/s_mdc/s_mdio_* outputs stay dangling by design (ethernet wave).

###############################################################################
# SECTION 7 — AXI FABRICS
#   7a. axi_interconnect_0 (16 MI, all frozen bases) — S00 re-sourced from the
#       MBV's peripheral master M_AXI_DP (contract §9.2.1). Single shell_clk
#       domain, no CDC inside — unchanged.
#   7b. smartconnect_ddr — M_AXI_DC + M_AXI_IC -> DDR4, DUAL-CLOCK
#       ([MBV-LESSON] verbatim: 100 MHz SI side, 200 MHz ui_clk MI side,
#       async crossing + 32b->512b upsizing inside smartconnect).
###############################################################################

set axi_interconnect_0 [_create_ip xilinx.com:ip:axi_interconnect:2.1 axi_interconnect_0]
set_property CONFIG.NUM_MI {16} $axi_interconnect_0
connect_bd_net $shell_clk       [get_bd_pins $axi_interconnect_0/ACLK]
connect_bd_net $shell_bus_arstn [get_bd_pins $axi_interconnect_0/ARESETN]
connect_bd_net $shell_clk       [get_bd_pins $axi_interconnect_0/S00_ACLK]
connect_bd_net $shell_aresetn   [get_bd_pins $axi_interconnect_0/S00_ARESETN]
connect_bd_intf_net [get_bd_intf_pins $mbv/M_AXI_DP] [get_bd_intf_pins $axi_interconnect_0/S00_AXI]

# Master index map — IDENTICAL to the donor (mi_map). Interface names resolved
# case-tolerantly [DEV-8]; the resolved names are what the address map uses.
set mi_map [list \
    0  $dut_clkrst_0   {s_axi} \
    1  $dfx_ctl_0      {s_axi} \
    2  $axi_hwicap_0   {S_AXI_LITE s_axi_lite} \
    3  $axi_quad_spi_0 {AXI_LITE axi_lite} \
    4  $telem_0        {s_axi} \
    5  $jtag_bb_0      {s_axi} \
    6  $debug_bridge_0 {S_AXI s_axi} \
    7  $uart_bridge_0  {s_axi} \
    8  $board_gpio_0   {s_axi} \
    9  $axi_timer_0    {S_AXI s_axi} \
    10 $axi_uartlite_0 {S_AXI s_axi} \
    11 $axi_intc_0     {s_axi S_AXI} \
    12 $axi_emc_0      {S_AXI_MEM s_axi_mem} \
    13 $clk_wiz_dut    {s_axi_lite} \
    14 $clcd_0         {s_axi} \
    15 $clcd_kvm_0     {s_axi} \
]
foreach {idx cell intf_cands} $mi_map {
    set mNN [format "M%02d" $idx]
    set mi  [format "M%02d_AXI" $idx]
    connect_bd_net $shell_clk     [get_bd_pins $axi_interconnect_0/${mNN}_ACLK]
    connect_bd_net $shell_aresetn [get_bd_pins $axi_interconnect_0/${mNN}_ARESETN]
    connect_bd_intf_net [get_bd_intf_pins $axi_interconnect_0/$mi] [_find_intf $cell $intf_cands]
}

# smartconnect_ddr — [MBV-LESSON] verbatim (NUM_CLKS 2 dual-clock).
set sc_ddr [_create_ip xilinx.com:ip:smartconnect:1.0 smartconnect_ddr]
set sc_ddr_cfg [list CONFIG.NUM_SI {2} CONFIG.NUM_MI {1} CONFIG.NUM_CLKS {2}]
set_property -dict $sc_ddr_cfg $sc_ddr
_assert_cfg $sc_ddr $sc_ddr_cfg "smartconnect_ddr (2 SI, dual-clock 100<->200 crossing)"
connect_bd_net $shell_clk      [get_bd_pins $sc_ddr/aclk]    ;# 100 MHz cache-master side
connect_bd_net $ddr_ui_clk     [get_bd_pins $sc_ddr/aclk1]   ;# 200 MHz ui_clk DDR side
connect_bd_net $ddr_icon_arstn [get_bd_pins $sc_ddr/aresetn] ;# calib-gated domain [DEV-4]
connect_bd_intf_net [get_bd_intf_pins $mbv/M_AXI_DC]   [get_bd_intf_pins $sc_ddr/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins $mbv/M_AXI_IC]   [get_bd_intf_pins $sc_ddr/S01_AXI]
connect_bd_intf_net [get_bd_intf_pins $sc_ddr/M00_AXI] [get_bd_intf_pins ddr4_0/C0_DDR4_S_AXI]
# RELEASE ORDER SAFE ([MBV-LESSON]): the MIG AXI shim leaves reset on
# ui_clk_sync_rst deassert, strictly before any master can issue (mb_reset +
# ddr_icon_arstn additionally wait on clk_wiz lock AND calib). The shim also
# self-gates on mc_init_complete_r internally. Slave live first.

# DDR4 physical memory interface out to the board [DEV-7].
set _ddr_phy [get_bd_intf_pins -quiet ddr4_0/C0_DDR4]
if { $_ddr_phy eq "" } {
    error "shell_linux_bd.tcl: ddr4_0/C0_DDR4 physical memory interface not found."
}
make_bd_intf_pins_external $_ddr_phy
# [MBV-LESSON] CRITICAL rename: the auto port name C0_DDR4_0 makes the wrapper
# emit C0_DDR4_0_* scalar ports which SILENTLY miss every lowercase c0_ddr4_*
# PACKAGE_PIN in ddr4_pins.xdc ("No ports matched" -> unconstrained memory
# pins, clean-looking dead build). Rename so wrapper ports come out c0_ddr4_*.
set _ddr_ext [get_bd_intf_ports -quiet C0_DDR4_0]
if { $_ddr_ext eq "" } {
    error "shell_linux_bd.tcl: expected external interface port 'C0_DDR4_0' after make_bd_intf_pins_external; got: [get_bd_intf_ports]"
}
set_property name c0_ddr4 $_ddr_ext
puts " shell_linux_bd.tcl : renamed external DDR4 interface port C0_DDR4_0 -> c0_ddr4"

###############################################################################
# SECTION 8 — ADDRESS MAP (explicit assign_bd_address at the SECTION 0
# constants; NEVER auto-assign). Donor bases verbatim + LMB 128K + DDR 1G.
###############################################################################

assign_bd_address -offset $MAP_LMB_BASE -range $MAP_LMB_RANGE [get_bd_addr_segs {ilmb_bram_if_cntlr/SLMB/Mem}]
assign_bd_address -offset $MAP_LMB_BASE -range $MAP_LMB_RANGE [get_bd_addr_segs {dlmb_bram_if_cntlr/SLMB/Mem}]

assign_bd_address -offset $MAP_CLKRST_BASE  -range $MAP_PERIPH_RANGE [get_bd_addr_segs {dut_clkrst_0/s_axi/reg0}]
assign_bd_address -offset $MAP_DFXCTL_BASE  -range $MAP_PERIPH_RANGE [get_bd_addr_segs {dfx_ctl_0/s_axi/reg0}]
assign_bd_address -offset $MAP_HWICAP_BASE  -range $MAP_PERIPH_RANGE [_find_seg {axi_hwicap_0/S_AXI_LITE/Reg axi_hwicap_0/s_axi_lite/Reg}]
assign_bd_address -offset $MAP_OVL_BASE     -range $MAP_PERIPH_RANGE [_find_seg {axi_quad_spi_0/AXI_LITE/Reg axi_quad_spi_0/axi_lite/Reg}]
assign_bd_address -offset $MAP_TELEM_BASE   -range $MAP_PERIPH_RANGE [get_bd_addr_segs {telem_0/s_axi/reg0}]
assign_bd_address -offset $MAP_JTAGBB_BASE  -range $MAP_PERIPH_RANGE [get_bd_addr_segs {jtag_bb_0/s_axi/reg0}]
assign_bd_address -offset $MAP_DBGBR_BASE   -range $MAP_PERIPH_RANGE [_find_seg {debug_bridge_0/S_AXI/Reg0 debug_bridge_0/s_axi/Reg0 debug_bridge_0/S_AXI/Reg}]
assign_bd_address -offset $MAP_UARTBR_BASE  -range $MAP_PERIPH_RANGE [get_bd_addr_segs {uart_bridge_0/s_axi/reg0}]
assign_bd_address -offset $MAP_GPIO_BASE    -range $MAP_PERIPH_RANGE [get_bd_addr_segs {board_gpio_0/s_axi/reg0}]
assign_bd_address -offset $MAP_DRP_BASE     -range $MAP_PERIPH_RANGE [_find_seg {clk_wiz_dut/s_axi_lite/Reg}]
assign_bd_address -offset $MAP_CLCD_BASE    -range $MAP_PERIPH_RANGE [get_bd_addr_segs {clcd_0/s_axi/reg0}]
assign_bd_address -offset $MAP_CLCDKVM_BASE -range $MAP_PERIPH_RANGE [get_bd_addr_segs {clcd_kvm_0/s_axi/reg0}]
# VPHY 0x44A3_0000 / GENCHK 0x44A6_0000: RESERVED pages, still not instantiated.

assign_bd_address -offset $MAP_TIMER_BASE -range $MAP_PERIPH_RANGE [_find_seg {axi_timer_0/S_AXI/Reg axi_timer_0/s_axi/Reg}]
assign_bd_address -offset $MAP_UART_BASE  -range $MAP_PERIPH_RANGE [_find_seg {axi_uartlite_0/S_AXI/Reg axi_uartlite_0/s_axi/Reg}]
assign_bd_address -offset $MAP_INTC_BASE  -range $MAP_PERIPH_RANGE [_find_seg {axi_intc_0/s_axi/Reg axi_intc_0/S_AXI/Reg}]

# LAN9220 16 MiB window. The donor used -quiet because the EMC segment name is
# CONFIG-dependent; keep the wildcard but ASSERT the result in SECTION 9 so a
# silent miss cannot ship.
assign_bd_address -offset $MAP_EMC_BASE -range $MAP_EMC_RANGE [get_bd_addr_segs {axi_emc_0/S_AXI_MEM/*}] -quiet

# DDR4: the segment lives on the IP's own memory map, not the AXI pin.
set _ddr_seg [get_bd_addr_segs -quiet {ddr4_0/C0_DDR4_MEMORY_MAP/C0_DDR4_ADDRESS_BLOCK}]
if { $_ddr_seg eq "" } {
    set _ddr_seg [get_bd_addr_segs -quiet -of_objects [get_bd_cells ddr4_0]]
}
if { [llength $_ddr_seg] != 1 } {
    error "shell_linux_bd.tcl: expected exactly one DDR4 address segment on ddr4_0, found: '$_ddr_seg'"
}
assign_bd_address -offset $MAP_DDR_BASE -range $MAP_DDR_RANGE $_ddr_seg

###############################################################################
# SECTION 9 — VALIDATE + POST-PROPAGATION ASSERTS + GUARDS + EXPORT
###############################################################################

puts "-----------------------------------------------------------"
puts " shell_linux_bd.tcl : running validate_bd_design ..."
puts "-----------------------------------------------------------"
if { [catch { validate_bd_design } _verr] } {
    puts "==========================================================="
    puts " shell_linux_bd.tcl : validate_bd_design FAILED"
    puts "-----------------------------------------------------------"
    puts $_verr
    puts "==========================================================="
    error "shell_linux_bd.tcl: validate_bd_design failed — see above."
}
puts "==========================================================="
puts " shell_linux_bd.tcl : validate_bd_design PASSED"
puts "==========================================================="

# [MBV-LESSON] C_NUM_INTR_INPUTS is READ-ONLY, derived from the xlconcat net
# during propagation. If it is not 4, one or more of {hwicap, timer, uartlite,
# eth} is physically wired into nothing and the DTS would name ghost inputs.
_assert_cfg $axi_intc_0 [list CONFIG.C_NUM_INTR_INPUTS 4] \
    "axi_intc_0 intr width (READ-ONLY — derived from xlconcat during propagation)"

# [FIX-C] POST-validate re-assert of the Linux INTC contract: propagation may
# WARN that the manual C_KIND_OF_INTR (0x0, all level) differs from the
# computed sensitivity word (uartlite's pin is metadata'd EDGE_RISING) — that
# warning is expected and benign (LVL_P latches; see SECTION 4) — but the
# MANUAL value must have survived propagation unchanged, because the DTS
# `xlnx,kind-of-intr = <0x0>` and the kernel's edge-vs-level irq_chip choice
# key off exactly this word. A silent override here = lost/stuck IRQs.
_assert_cfg $axi_intc_0 $intc_cfg \
    "axi_intc_0 Linux irq contract POST-validate (kind_of_intr=0x0 must survive propagation) \[FIX-C\]"

# [FIX-A] POST-validate re-assert of the three Linux-critical MBV parameters.
# COUNTERS=1 (rdtime/Zicntr clocksource); INTERRUPT_WAKEUP=1 (the silicon-proven
# E3 WFI-coma fix). SSTC=1 is kept as a LATENT capability only — per silicon
# errata E1/E2 the deployed DTS drops "sstc" and the clockevent runs on the AXI
# timer over SEIP (soclabs,mbv-timer); the assert just freezes the HW default so
# a Vivado upgrade can't silently flip it and break the fixed-IP re-enable path.
# Pre-validate they ride mbv_cfg's assert; this re-read guards a propagation-
# time override.
_assert_cfg $mbv [list \
    CONFIG.C_USE_COUNTERS     {1} \
    CONFIG.C_USE_SSTC         {1} \
    CONFIG.C_INTERRUPT_WAKEUP {1} \
] "microblaze_riscv_0 SSTC/COUNTERS/INTERRUPT_WAKEUP POST-validate \[FIX-A\]"

# EMC window assert — the -quiet assign above must actually have landed.
set _emc_ok 0
foreach _s [get_bd_addr_segs -quiet -of_objects [get_bd_addr_spaces $mbv/Data]] {
    set _off [get_property -quiet OFFSET $_s]
    set _rng [get_property -quiet RANGE  $_s]
    if { $_off ne "" && [expr {$_off == $MAP_EMC_BASE}] } {
        if { [expr {$_rng == 0x1000000}] } { set _emc_ok 1 }
    }
}
if { !$_emc_ok } {
    error "shell_linux_bd.tcl: LAN9220 EMC window NOT assigned at $MAP_EMC_BASE/16M — the -quiet assign silently missed (segment name drift). Fix the S_AXI_MEM segment path."
}
puts "-- LAN9220 EMC window confirmed at $MAP_EMC_BASE / 16M"

# Full frozen-base assert: every donor CSR base must exist in the MBV Data
# space at exactly its contract offset (host tooling bakes these).
set _expected_bases [list \
    $MAP_UART_BASE UARTLITE  $MAP_INTC_BASE INTC       $MAP_TIMER_BASE TIMER \
    $MAP_CLKRST_BASE CLKRST  $MAP_DFXCTL_BASE DFXCTL   $MAP_HWICAP_BASE HWICAP \
    $MAP_OVL_BASE OVLSTORE   $MAP_TELEM_BASE TELEM     $MAP_JTAGBB_BASE JTAGBB \
    $MAP_DBGBR_BASE DBGBR    $MAP_UARTBR_BASE UARTBR   $MAP_GPIO_BASE GPIO \
    $MAP_DRP_BASE MMCM_DUT_DRP $MAP_CLCD_BASE CLCD     $MAP_CLCDKVM_BASE CLCDKVM \
    $MAP_DDR_BASE DDR4 ]
set _found_offsets {}
foreach _s [get_bd_addr_segs -quiet -of_objects [get_bd_addr_spaces $mbv/Data]] {
    set _off [get_property -quiet OFFSET $_s]
    if { $_off ne "" } { lappend _found_offsets [format 0x%08X $_off] }
}
set _missing {}
foreach {_b _n} $_expected_bases {
    if { [lsearch -exact $_found_offsets [format 0x%08X $_b]] < 0 } {
        lappend _missing "$_n@[format 0x%08X $_b]"
    }
}
if { [llength $_missing] } {
    error "shell_linux_bd.tcl: frozen bases MISSING from the MBV Data space: $_missing"
}
puts "-- all [expr {[llength $_expected_bases]/2}] frozen bases present in microblaze_riscv_0/Data"

# --- [MBV-LESSON] UNDRIVEN-RESET GUARD — verbatim rule ----------------------
# A dangling reset INPUT is silently auto-tied to 1'b0: for an ACTIVE_LOW
# reset that is ASSERTED FOREVER, with no warning, no DRC, and a clean-looking
# validate (this exact class held the MIG AXI shim in reset and wedged the CPU
# on its first DDR store). Whitelist ONLY pins that are dangling in the
# board-proven donor or covered by the MDM bus semantics.
set _rst_whitelist {
    proc_sys_reset_cpu/mb_debug_sys_rst
    proc_sys_reset_shell/mb_debug_sys_rst
    proc_sys_reset_shell/aux_reset_in
}
# (proc_sys_reset_shell/aux_reset_in is unconnected in the board-proven shell;
#  the IP's component default deasserts it. proc_sys_reset_cpu/aux_reset_in IS
#  driven — by c0_init_calib_complete.)
set _undriven {}
foreach _c [get_bd_cells] {
    foreach _p [get_bd_pins -quiet -of_objects $_c -filter {DIR == I && TYPE == rst}] {
        set _name "[get_property NAME $_c]/[get_property NAME $_p]"
        if { [lsearch -exact $_rst_whitelist $_name] >= 0 } { continue }
        if { [llength [get_bd_nets -quiet -of_objects $_p]] == 0 } {
            lappend _undriven $_name
        }
    }
}
if { [llength $_undriven] } {
    puts "==========================================================="
    puts " shell_linux_bd.tcl : UNDRIVEN RESET INPUT(S)"
    foreach _u $_undriven { puts "   $_u" }
    puts "==========================================================="
    error "shell_linux_bd.tcl: reset INPUT(s) left dangling: $_undriven — IPI ties these to 1'b0 (ACTIVE_LOW => ASSERTED FOREVER, silently)."
}
puts " shell_linux_bd.tcl : reset-connectivity guard PASSED (no undriven reset inputs)"

if { [llength $::vlnv_substitutions] } {
    puts "==========================================================="
    puts " shell_linux_bd.tcl : VLNV SUBSTITUTIONS (2024.1 -> 2026.1 drift) \[DEV-9\]"
    foreach _s $::vlnv_substitutions { puts "   $_s" }
    puts " Each must be reviewed for CONFIG compatibility."
    puts "==========================================================="
} else {
    puts "-- no VLNV substitutions: every donor IP version exists in the 2026.1 catalogue"
}

save_bd_design

###############################################################################
# address_map.txt — the map READ BACK off the built BD (not a restatement of
# SECTION 0). ADDRESS_MAP.md is checked against this dump, not against intent.
###############################################################################

if { [info exists ::env(LINUX_HARNESS_BUILD)] && $::env(LINUX_HARNESS_BUILD) ne "" } {
    set _bdir $::env(LINUX_HARNESS_BUILD)
} else {
    set _bdir "[file normalize [file dirname [info script]]]/build"
}
file mkdir $_bdir

set fh [open "$_bdir/address_map.txt" w]
puts $fh "# shell_linux_bd address map — READ BACK from the built block design."
puts $fh "# Generated by shell_linux_bd.tcl. Do not hand-edit."
puts $fh "#"
puts $fh "# master_address_space -> segment : offset  range"
foreach space [lsort [get_bd_addr_spaces -quiet]] {
    foreach seg [get_bd_addr_segs -quiet -of_objects $space] {
        set off [get_property -quiet OFFSET $seg]
        set rng [get_property -quiet RANGE  $seg]
        if { $off eq "" } { continue }
        puts $fh [format "%-38s %-56s %-12s %s" $space $seg $off $rng]
    }
}
puts $fh ""
puts $fh "reset_vector      = $MAP_RESET_VECTOR   (microblaze_riscv C_BASE_VECTORS -> LMB boot BRAM)"
puts $fh "cpu_clk_hz        = $CPU_CLK_HZ  (shell_clk, clk_wiz_shell/clk_out1) \[DEV-3\]"
puts $fh "ddr_base          = $MAP_DDR_BASE"
puts $fh "ddr_range         = $MAP_DDR_RANGE"
puts $fh "lmb_base          = $MAP_LMB_BASE"
puts $fh "lmb_range         = $MAP_LMB_RANGE  \[DEV-2\]"
puts $fh "irq_hwicap        = $IRQ_HWICAP"
puts $fh "irq_timer         = $IRQ_TIMER"
puts $fh "irq_uartlite      = $IRQ_UART"
puts $fh "irq_eth           = $IRQ_ETH   (LAN9220 — IRQ_CFG must be set push-pull active-HIGH by sw)"
puts $fh "intc_kind_of_intr = [get_property CONFIG.C_KIND_OF_INTR $axi_intc_0]  (READ BACK post-validate; all level — DTS xlnx,kind-of-intr must match; the manual-vs-computed Vivado warning is expected+benign, LVL_P latches) \[DEV-5\]\[FIX-C\]"
# [FIX-B] riscv_isa is COMPUTED from the post-validate read-backs — never a
# hardcoded restatement of intent (the judge defect). Base rv32i is the only
# constant: C_DATA_SIZE=32 is asserted in mbv_cfg. NO zicbom (the IP has no
# such parameter — cache ops are custom CSRs, not Zicbom).
set _isa "rv32i"
if { [get_property CONFIG.C_USE_MULDIV      $mbv] } { append _isa "m" }
if { [get_property CONFIG.C_USE_ATOMIC      $mbv] } { append _isa "a" }
if { [get_property CONFIG.C_USE_COMPRESSION $mbv] } { append _isa "c" }
if { [get_property CONFIG.C_USE_BITMAN_A    $mbv] } { append _isa "_zba" }
if { [get_property CONFIG.C_USE_BITMAN_B    $mbv] } { append _isa "_zbb" }
if { [get_property CONFIG.C_USE_BITMAN_S    $mbv] } { append _isa "_zbs" }
puts $fh "riscv_isa         = $_isa  (READ BACK from microblaze_riscv_0 CONFIG; NO zicbom — no such parameter exists) \[FIX-B\]"
puts $fh "mmu               = [get_property CONFIG.C_USE_MMU $mbv]  (3 = SUPERVISOR / Sv32)"
puts $fh "atomics           = [get_property CONFIG.C_USE_ATOMIC $mbv]"
puts $fh "compression       = [get_property CONFIG.C_USE_COMPRESSION $mbv]"
puts $fh "counters_zicntr   = [get_property CONFIG.C_USE_COUNTERS $mbv]  (rdtime/time CSR = the kernel's clocksource) \[FIX-A\]"
puts $fh "sstc              = [get_property CONFIG.C_USE_SSTC $mbv]  (LATENT capability; DEPLOYED DTS DROPS sstc per errata E1/E2 — clockevent is the AXI timer on SEIP via soclabs,mbv-timer) \[FIX-A\]"
puts $fh "interrupt_wakeup  = [get_property CONFIG.C_INTERRUPT_WAKEUP $mbv]  (MUST be 1 — wfi-coma fix, silicon-proven 2026-07-16) \[FIX-A\]"
puts $fh "decoupler_intfs   = 19 (IDs 0-18; qspi_csn clamp 0x1 sole non-zero)"
puts $fh "vlnv_substitutions= [expr {[llength $::vlnv_substitutions] ? $::vlnv_substitutions : {none}}]"
close $fh
puts "-- address map exported: $_bdir/address_map.txt"

puts "==========================================================="
puts " shell_linux_bd.tcl : DONE — BD assembled, validated, guarded, exported."
puts "==========================================================="
