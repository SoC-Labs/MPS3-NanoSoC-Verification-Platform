###-----------------------------------------------------------------------------
### mps3-nanosoc-platform — static shell Vivado block-design (W-BD wave)
###
### Assembles the harness BD: MicroBlaze bare-metal coordinator + local memory
### + MDM + AXI INTC/Timer/UARTLite, AXI HWICAP (ICAP), DFX Decoupler + AXI
### Shutdown Manager, AXI EMC (LAN9220 host I/F), the six REAL custom shell
### CSR blocks (clkrst/dfx_ctl/board_gpio/jtag_bb/telem/uart_bridge), DRP clock
### wizard + proc_sys_reset, and the RP partition-pin boundary as BD ports.
###
### Every connect_bd_net / connect_bd_intf_net below is a REAL Tcl call (not a
### TODO comment) — this supersedes the earlier commented skeleton. It has NOT
### been run against a live Vivado 2024.1 install (no Vivado in this
### environment); vlnv strings, CONFIG property names and connection APIs are
### real, current (2024.1-era) Vivado Tcl, but expect first-run fixups — see
### the "UNCERTAINTIES FOR THE FIRST REAL VIVADO RUN" block at the end of this
### file and fpga/shell/README.md.
###
### Ground truth:
###   docs/contracts/partition-pins.md v0.1  — RP boundary signal list
###   docs/contracts/shell-regmap.md   v0.1  — AXI-Lite regmap base addresses
###   docs/ARCHITECTURE_SPEC.md §4.1/§5/§7/§8/§12
###   docs/NEXT_WAVE_PLAN.md W-BD entry (this workstream)
###
### *** TOPOLOGY DECISION (resolves the previous skeleton's AMBIGUITY #1) ***
### partition-pins.md v0.1 §"I4" resolved: "the RP is a hierarchical cell
### *alongside* the shell in one top design (`u_top` = `u_shell` + `u_rp_dut`)
### ... partition pins are BD pins between the two, not top-level [module]
### ports ... fixes A2's RP instance path (`u_top/u_rp_dut`)." NEXT_WAVE_PLAN's
### own W-BD line says the same thing explicitly: "rename the RP cell to the
### I4-resolved u_top/u_rp_dut." A two-level hierarchy path means u_rp_dut is
### a DIRECT CHILD of u_top, a SIBLING of the shell BD instance — not nested
### three-deep inside this BD's own hierarchy (which is what the old skeleton's
### SECTION 3 hier-cell-with-create_bd_pin approach would have produced).
###
### Consequently: this BD (call it `u_shell` once instantiated one level up)
### exposes the entire partition-pins.md v0.1 signal list as ordinary
### `make_bd_port` TOP-LEVEL PORTS OF THIS BD. From THIS BD's own internal
### viewpoint those are ports; once `fpga/shell/shell_top.sv` (== `u_top`)
### instantiates this BD's Vivado-generated wrapper module as a cell, they
### become PINS on that cell instance — i.e. exactly the "BD pins between the
### two" the contract describes, with zero contradiction. `shell_top.sv`
### separately instantiates `rp_dut` (fpga/dfx/proof/rp_dut.sv — the proven
### RP black-box port list, port-for-port identical to partition-pins.md) as
### a SIBLING cell `u_rp_dut`, and wires it 1:1 by name to this BD wrapper's
### pins. `u_rp_dut` is NOT marked `HD.RECONFIGURABLE` anywhere in this
### repo's fpga/shell/ tree — that property (plus the dfx_floorplan.xdc
### pblock) is applied by the separate DFX build script (fpga/dfx/, not this
### deliverable) when it re-opens this same static design to build per-RM
### partials. This file only ever produces a plain, non-DFX static netlist —
### sufficient for the XSA / Vitis firmware bring-up build (build_shell.tcl).
###
### *** CPU SEAM (2026-09-23, LINUX_HARNESS_PLAN_2026-09-23.md DL2) ***
### ONE shell BD, two CPUs. Everything CPU-specific lives in cpu_mb.tcl (the
### classic MicroBlaze, SHELL_CPU unset/mb -- the fielded bare-metal shell) or
### cpu_mbv.tcl (MicroBlaze V + DDR4 for Linux, SHELL_CPU=mbv, Vivado 2026.1).
### This file sources the selected one at six fixed seam points (`cpu_stage`
### core/intc/timer/axi/addr/finish) inside create_root_design, so the CPU file
### sees and sets this proc's variables. Every other block -- the RP boundary,
### the decoupler, dfx_ctl's POR/WDOG isolation, VPHY/GENCHK, DUTEGR, USRACC,
### WDOG, TOUCH, dbgbscan, REALPHY -- is THIS file's Tcl in both variants, which
### is the point: the July Linux fork (src/linux_harness/shell_linux_bd.tcl)
### was a copy of this file, and in two months it drifted so far that its
### decoupler clamp could never assert (dfx_ctl ext_por_n_i unconnected). A seam
### cannot drift that way; a copy always does.
###
### The seam's own gate: tests/shell_cpu_seam. SHELL_CPU=mb must produce a BD
### identical (fpga/shell/tools/bd_dump.tcl, every cell/CONFIG/net/net
### name/address) to the pre-seam file -- the moved lines execute at the same
### points in the same order, which is why the hooks sit where they do and why
### none of them may be "tidied" into one place.
###-----------------------------------------------------------------------------

# Capture this file's directory at SOURCE time so create_root_design can source
# sibling add-on scripts (e.g. touch_iic_add.tcl) by absolute path regardless of
# the caller's cwd. `info script` inside the proc would not name this file.
set ::SHELL_BD_DIR [file dirname [file normalize [info script]]]

# SHELL_CPU -- which CPU subsystem the shell carries (the CPU SEAM, see the
# header). STRICT, the SHELL_REALPHY lesson (build_shell.tcl's
# soclabs_realphy_env): a permissive `ne "0"` parse once built the NON-DEFAULT
# real-PHY variant for SHELL_REALPHY=no. Unset or empty -> mb; mb/mbv as given;
# anything else is a hard error rather than a guess. build_shell.tcl,
# validate_bd.tcl and tests/shell_cpu_seam call this same proc.
proc soclabs_shell_cpu {} {
    if { ![info exists ::env(SHELL_CPU)] } { return mb }
    set v [string trim $::env(SHELL_CPU)]
    if { $v eq "" } { return mb }
    if { [lsearch -exact {mb mbv} $v] < 0 } {
        error "shell_bd.tcl: SHELL_CPU=\"$::env(SHELL_CPU)\" is not one of: mb mbv. Refusing to guess which CPU the static carries."
    }
    return $v
}

proc create_root_design { parentCell } {

    variable script_folder

    if { $parentCell eq "" } {
        set parentCell [get_bd_cells /]
    }
    set parentObj [get_bd_cells $parentCell]
    if { $parentObj == "" } {
        catch {common::send_gid_msg -ssname BD::TCL -id 2090 -severity "ERROR" "Unable to find parent cell <$parentCell>!"}
        return
    }
    set oldCurInst [current_bd_instance .]
    current_bd_instance $parentObj

    # ===================================================================
    # REAL-PHY VARIANT GATE (SHELL_REALPHY=1) — NON-DEFAULT.
    # When unset/0 (the default) this BD is byte-identical to the shipped
    # virtual-PHY shell: SECTION 5 terminates the RP's RMII/MDIO group on the
    # in-fabric virtual PHY (eth_mac_test_subsystem_0). When SHELL_REALPHY=1,
    # SECTION 5 additionally routes that group out to real MPS3 shield pads for
    # a physical LAN8720 (the RP RX group is driven FROM input pads instead of
    # the virtual PHY). No new PARTITION pin, no static_id re-key — only new
    # SHELL board pads. Paired with shell_top's `MPS3_SHELL_REALPHY` ifdef and
    # the gated fpga/shell/constraints_realphy/*.xdc (both added by
    # build_shell.tcl when SHELL_REALPHY=1).
    # ===================================================================
    set shell_realphy [expr {[info exists ::env(SHELL_REALPHY)] \
                             && $::env(SHELL_REALPHY) ne "0" \
                             && $::env(SHELL_REALPHY) ne ""}]
    if {$shell_realphy} {
        puts "shell_bd.tcl: SHELL_REALPHY=1 -> real external LAN8720 RMII pads (NON-DEFAULT variant)"
    }

    # ===================================================================
    # CPU SEAM GATE (SHELL_CPU=mb|mbv). Unset/mb = the shipped classic
    # MicroBlaze shell, byte-identical to the pre-seam BD. mbv = MicroBlaze V
    # + DDR4 (Vivado 2026.1). Strictly parsed (soclabs_shell_cpu above).
    # ===================================================================
    set shell_cpu [soclabs_shell_cpu]
    set cpu_tcl [file join $::SHELL_BD_DIR "cpu_${shell_cpu}.tcl"]
    if { ![file exists $cpu_tcl] } {
        error "shell_bd.tcl: SHELL_CPU=$shell_cpu but $cpu_tcl does not exist"
    }
    puts "shell_bd.tcl: SHELL_CPU=$shell_cpu -> [file tail $cpu_tcl]"

    ###################################################################
    # SECTION 0 — TOP-LEVEL BD PORTS: clocks/reset in, board-facing
    # externals, and the FULL partition-pin boundary (partition-pins.md
    # v0.1). Declared first so every connection below has something to
    # land on.
    ###################################################################

    # -- Board clock / reset (fed from shell_top.sv's OSCCLK1 buffer + the
    #    board reset button/POR net; both already IBUF/BUFG'd or
    #    debounced-and-synchronized by the top level before reaching the BD) --
    # CRITICAL: declare 50 MHz on the BD clock port via -freq_hz AT CREATION. A BD
    # clock port defaults to FREQ_HZ=100000000, and clk_wiz HONORS the connected
    # port's FREQ_HZ over its own CONFIG.PRIM_IN_FREQ {50.000} — so at 100 MHz it
    # keeps the 50 MHz-derived MULT=20 but applies it to a 100 MHz assumption,
    # giving FVCO=2000 MHz (illegal; valid range 600-1200 for -1 speed grade) →
    # "IO Clock Placer failed" in impl. -freq_hz at creation is the tool-sanctioned
    # form ([BD 5-670] warns if it's omitted and a later set_property doesn't fully
    # propagate). 50 MHz → CLKIN1_PERIOD=20 → FVCO=1000 MHz; CLKOUT0=100, CLKOUT1=50.
    create_bd_port -dir I -type clk -freq_hz 50000000 osc_clk_50m
    create_bd_port -dir I -type rst sys_rst_n
    set_property CONFIG.POLARITY {ACTIVE_LOW} [get_bd_ports sys_rst_n]

    # -- LAN9220 host I/F (AXI EMC external interface + its interrupt) —
    #    "expose its external interface + an eth_irq input" per task scope.
    #    The EMC_INTF bus type below is the real PG105 external interface;
    #    confirm the exact intf VLNV against the 2024.1 axi_emc datasheet on
    #    first open (see uncertainties list). --
    create_bd_intf_port -mode Master -vlnv xilinx.com:interface:emc_rtl:1.0 EMC_INTF
    create_bd_port -dir I eth_irq

    # -- MicroBlaze console (physical UART fallback, spec §11 optional raw
    #    fallback; primary DUT console path is UARTBR below) --
    create_bd_port -dir O uart_tx_f
    create_bd_port -dir I uart_rx_f

    # -- USD: the USER microSD slot (usd_spi_0 -> shell_top OBUFT/IOBUFs).
    #    D13 (docs/planning/HANDOVER_USD_OVERLAY_STORE.md): the overlay store
    #    moves onto the user card, and usd_spi_0 replaces the pad-less
    #    axi_quad_spi_0 on the 0x44A4_0000 page (the SST26 has belonged to the RP
    #    since D16; the shell drives no SPI master onto it). Split-tristate
    #    o/oe pairs like board_gpio's pad group; _oe = 1 means drive. Every oe is
    #    usd_spi's registered pad gate (EN && card present), so with no card the
    #    socket stays high-Z whatever firmware writes. USD_DAT[1:2] are not
    #    block ports: shell_top holds their IOBUFs at T=1. NOT the MCC config
    #    card (V2M_MPS3): that one is not FPGA-wired. --
    create_bd_port -dir O usd_clk_o
    create_bd_port -dir O usd_clk_oe
    create_bd_port -dir O usd_cmd_o
    create_bd_port -dir O usd_cmd_oe
    create_bd_port -dir I usd_dat0_i
    create_bd_port -dir O usd_dat3_o
    create_bd_port -dir O usd_dat3_oe
    create_bd_port -dir I usd_ncd_i

    # -- Board GPIO/PMOD passthrough pads (board_gpio.sv's board_pad_* group;
    #    physical LED/button/PMOD pin mapping lives in fpga/shell/constraints/,
    #    not here — partition-pins.md "Board-port/GPIO" note) --
    create_bd_port -dir O -from 15 -to 0 board_gpio_pad_o
    create_bd_port -dir O -from 15 -to 0 board_gpio_pad_oe
    create_bd_port -dir I -from 15 -to 0 board_gpio_pad_i

    # -- CLCD pads (clcd_0 -> shell_top IOBUFs). o/i/oe triplet on the data bus
    #    like board_gpio's pad group; single-ended strobes/controls out. --
    create_bd_port -dir O -from 7 -to 0 clcd_pd_o
    create_bd_port -dir I -from 7 -to 0 clcd_pd_i
    create_bd_port -dir O clcd_pd_oe
    create_bd_port -dir O clcd_cs_n
    create_bd_port -dir O clcd_wr_n
    create_bd_port -dir O clcd_rd_n
    create_bd_port -dir O clcd_rs
    create_bd_port -dir O clcd_bl
    create_bd_port -dir O clcd_rst_n

    # -- TELEM I2C (INA228) — exposed for the future ina228_i2c_master engine
    #    (telem/README.md's documented seam); tied off inside telem_0 for now,
    #    still brought out to a BD port so shell_top.sv can wire real pads
    #    once that engine lands without another BD edit. --
    create_bd_port -dir O i2c_scl_o
    create_bd_port -dir O i2c_scl_t
    create_bd_port -dir O i2c_sda_o
    create_bd_port -dir O i2c_sda_t
    create_bd_port -dir I i2c_sda_i

    # ===================================================================
    # RP PARTITION-PIN BOUNDARY (partition-pins.md v0.1, EXACT signal list).
    # Directions are from the shell's point of view, matching the contract
    # table 1:1. These become PINS on this BD's wrapper cell once shell_top.sv
    # instantiates it — see the topology note at the top of this file.
    # ===================================================================

    # Clocks & resets (shell -> RP)
    create_bd_port -dir O rp_dut_clk
    create_bd_port -dir O rp_dut_resetn
    create_bd_port -dir O rp_rp_resetn
    create_bd_port -dir O rp_dbg_resetn

    # Processor debug — internal JTAG (shell drives the TAP)
    create_bd_port -dir O rp_jtag_tck
    create_bd_port -dir O rp_jtag_tms
    create_bd_port -dir O rp_jtag_tdi
    create_bd_port -dir I rp_jtag_tdo

    # ILA-over-XVC BSCAN group (boundary.yaml group `dbgbscan`). debug_bridge_0's
    # m0_bscan master port, carried into the RP DISCRETELY (not as a bscan_rtl
    # interface port: the decoupler model and the boundary test are per-signal).
    # 11 shell-driven legs pass straight through; the one RP-driven leg (tdo)
    # comes back through dfx_decoupler_0 INTF 19, clamped 0. An RM with ILAs
    # carries a mode-1 debug_bridge hub on these wires; every other RM ties
    # dbg_bscan_tdo to 0. Wired after debug_bridge_0 is created (SECTION 4).
    create_bd_port -dir O rp_dbg_bscan_bscanid_en
    create_bd_port -dir O rp_dbg_bscan_capture
    create_bd_port -dir O rp_dbg_bscan_drck
    create_bd_port -dir O rp_dbg_bscan_reset
    create_bd_port -dir O rp_dbg_bscan_runtest
    create_bd_port -dir O rp_dbg_bscan_sel
    create_bd_port -dir O rp_dbg_bscan_shift
    create_bd_port -dir O rp_dbg_bscan_tck
    create_bd_port -dir O rp_dbg_bscan_tdi
    create_bd_port -dir O rp_dbg_bscan_tms
    create_bd_port -dir O rp_dbg_bscan_update
    create_bd_port -dir I rp_dbg_bscan_tdo

    # Ethernet — RMII + MDIO (shell = virtual PHY; NOT wired to a real
    # virtual-PHY/bridge datapath in this wave — see SECTION 5 "Ethernet
    # MAC-verification subsystem (deferred)" below. Tied to safe idle here so
    # the BD is complete/legal even before that subsystem lands.)
    create_bd_port -dir O rp_phy_rmii_ref_clk
    create_bd_port -dir O rp_phy_rmii_crs_dv
    create_bd_port -dir O -from 1 -to 0 rp_phy_rmii_rxd
    create_bd_port -dir I -from 1 -to 0 rp_phy_rmii_txd
    create_bd_port -dir I rp_phy_rmii_tx_en
    create_bd_port -dir I rp_mdc
    create_bd_port -dir I rp_mdio_o
    create_bd_port -dir I rp_mdio_oe
    create_bd_port -dir O rp_mdio_i

    # -- Real-PHY shield pads (SHELL_REALPHY only) -----------------------------
    #    Carry the RP's RMII + MDIO group out to / in from a PHYSICAL LAN8720 on
    #    the MPS3 shield (measured arm_mps3 map), replacing the in-fabric virtual
    #    PHY for this variant. Board-facing single-ended pads (LVCMOS33) are
    #    formed at shell_top by OBUF/IBUF (unidirectional RMII) + an IOBUF for
    #    the bidirectional MDIO (gated by mdio_oe). The HDPR-29 IOB=TRUE TX
    #    re-register stage lives shell-side (static) — partition-pins.md "IOB
    #    packing note". phy_pad_rmii_ref_clk is clk_wiz_shell/clk_out2 (the fixed
    #    50 MHz RMII reference) driven OUT to the LAN8720 REFCLK-in strap
    #    (CLOCKING DECISION Option A: FPGA SOURCES REF_CLK).
    if {$shell_realphy} {
        create_bd_port -dir O phy_pad_rmii_ref_clk
        create_bd_port -dir O -from 1 -to 0 phy_pad_rmii_txd
        create_bd_port -dir O phy_pad_rmii_tx_en
        create_bd_port -dir O phy_pad_mdc
        create_bd_port -dir O phy_pad_mdio_o
        create_bd_port -dir O phy_pad_mdio_oe
        create_bd_port -dir I phy_pad_rmii_crs_dv
        create_bd_port -dir I -from 1 -to 0 phy_pad_rmii_rxd
        create_bd_port -dir I phy_pad_mdio_i
    }

    # Console / trace AXI-Stream (nanosoc cmsdk_apb_usrt) -> uart_bridge_0
    create_bd_port -dir I -from 7 -to 0 rp_uart_tx_tdata
    create_bd_port -dir I rp_uart_tx_tvalid
    create_bd_port -dir O rp_uart_tx_tready
    create_bd_port -dir O -from 7 -to 0 rp_uart_rx_tdata
    create_bd_port -dir O rp_uart_rx_tvalid
    create_bd_port -dir I rp_uart_rx_tready
    create_bd_port -dir I rp_swo

    # Status / misc (RP -> shell) -> dfx_ctl_0 (RM-load verify)
    create_bd_port -dir I -from 31 -to 0 rp_rm_id
    create_bd_port -dir I rp_dut_lockup
    create_bd_port -dir I rp_irq_out

    # Board-port / GPIO passthrough (shell <-> RP) -> board_gpio_0
    create_bd_port -dir I -from 15 -to 0 rp_dut_gpio_o
    create_bd_port -dir I -from 15 -to 0 rp_dut_gpio_oe
    create_bd_port -dir O -from 15 -to 0 rp_dut_gpio_i

    # -- Flash / QSPI XiP partition pins (partition-pins.md v0.2). RP-drive legs
    #    (rp_qspi_*) come IN from the RP and route through the decoupler; the
    #    clamped result leaves on qspi_pad_* to shell_top's SST26 IOBUFs. The
    #    io_i pad sample (shell->RP) is NOT a BD port — shell_top drives the
    #    RP's qspi_io_i directly from the pad (non-CDC pass-through, never
    #    clamped). See SECTION 4 wiring + the decoupler config above.
    create_bd_port -dir I rp_qspi_sclk
    create_bd_port -dir I rp_qspi_csn
    create_bd_port -dir I -from 3 -to 0 rp_qspi_io_o
    create_bd_port -dir I -from 3 -to 0 rp_qspi_io_oe
    create_bd_port -dir O qspi_pad_sclk
    create_bd_port -dir O qspi_pad_csn
    create_bd_port -dir O -from 3 -to 0 qspi_pad_io_o
    create_bd_port -dir O -from 3 -to 0 qspi_pad_io_oe

    # -- CLCD-KVM button (Wave 4). Raw async, active-low MPS3 push-button
    #    USER_nPB1 (AT32). Synchronised + debounced INSIDE clcd_kvm (hardware,
    #    so the button still works when the harness firmware is wedged). --
    create_bd_port -dir I user_npb1

    ###################################################################
    # SECTION 1 — CLOCKING (spec §5)
    ###################################################################

    # Fixed shell clock: ~100 MHz for MicroBlaze/AXI/ICAP, + the FIXED
    # 50 MHz RMII reference (shell sources it — spec §8.3; NOT DRP, this one
    # never moves). ONE clk_wiz, two static outputs.
    set clk_wiz_shell [create_bd_cell -type ip -vlnv xilinx.com:ip:clk_wiz:6.0 clk_wiz_shell]
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
    # TODO(integrator): PRIM_SOURCE {No_buffer} assumes shell_top.sv already
    # IBUF/BUFG's OSCCLK1 before this port; if instead the board oscillator
    # should drive clk_wiz's dedicated MMCM clock input pin directly, change
    # to a board-interface CONFIG (Global_buffer / board part pin) instead.

    # DUT-clock MMCM, DRP-reconfigurable (spec §5 "make the DUT-clock MMCM
    # DRP-reconfigurable"). dut_clkrst_0 drives its DRP port; see SECTION 3.
    set clk_wiz_dut [create_bd_cell -type ip -vlnv xilinx.com:ip:clk_wiz:6.0 clk_wiz_dut]
    set_property -dict [list \
        CONFIG.PRIM_IN_FREQ {50.000} \
        CONFIG.PRIM_SOURCE {No_buffer} \
        CONFIG.CLKOUT1_REQUESTED_OUT_FREQ {50.000} \
        CONFIG.USE_LOCKED {true} \
        CONFIG.USE_DYN_RECONFIG {true} \
        CONFIG.CLKIN1_JITTER_PS {160.0} \
    ] $clk_wiz_dut
    # D7 (RP Pblock sizing) + D12 (DUT-clock range/default freq) are open per
    # docs §15 — 50 MHz default is a placeholder matching the proof shell's
    # OSCCLK1-derived clock; retune CLKOUT1_REQUESTED_OUT_FREQ once nanosoc's
    # own timing closure sets the real target.
    # NOTE (2024.1 clk_wiz:6.0 real behaviour, confirmed live): enabling
    # USE_DYN_RECONFIG forces INTERFACE_SELECTION to Enable_AXI and this is
    # NOT user-overridable (attempting CONFIG.USE_RESET/RESET_TYPE or any
    # "native DRP pin" (den/dwe/daddr/din/dout/drdy) config on this cell now
    # throws "disabled parameter ... ignored" / "No pins matched") — 2024.1's
    # clk_wiz only exposes DRP-reconfigurable MMCMs through a single AXI4-Lite
    # slave (`s_axi_lite`, VLNV aximm_rtl) covering control+status+DRP
    # together; there is no separate scalar reset pin either (reset rides on
    # s_axi_aresetn). See the s_axi_lite wiring below (SECTION 1 end) and its
    # AXI-Lite slave hookup at the interconnect (SECTION 6, MMCM_DUT_DRP).

    # Shell-domain reset generator (MicroBlaze/AXI/CSR peripheral_aresetn).
    # dut_clkrst_0 generates its OWN three DUT-domain resets internally
    # (async-assert/sync-deassert per partition-pins.md) — no separate
    # proc_sys_reset_dut is needed; see dut_clkrst.sv's header.
    set proc_sys_reset_shell [create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 proc_sys_reset_shell]
    # C_EXT_RESET_HIGH is read-only on proc_sys_reset:5.0 (polarity is fixed by
    # the IP; ext_reset_in is active-low by default) — do not set it; sys_rst_n
    # (already active-low) connects straight to ext_reset_in below.

    connect_bd_net [get_bd_ports osc_clk_50m] [get_bd_pins $clk_wiz_shell/clk_in1]
    connect_bd_net [get_bd_ports osc_clk_50m] [get_bd_pins $clk_wiz_dut/clk_in1]
    # clk_wiz 6.0's reset pin is named per RESET_TYPE: ACTIVE_LOW -> "resetn"
    # (NOT "reset", which is the ACTIVE_HIGH pin name) — clk_wiz_shell sets
    # RESET_TYPE {ACTIVE_LOW}. clk_wiz_dut has NO scalar reset pin at all
    # (dynamic-reconfig/AXI mode — see the NOTE above); its reset instead
    # rides on s_axi_aresetn, wired below once shell_aresetn exists.
    connect_bd_net [get_bd_ports sys_rst_n]   [get_bd_pins $clk_wiz_shell/resetn]
    connect_bd_net [get_bd_ports sys_rst_n]   [get_bd_pins $proc_sys_reset_shell/ext_reset_in]
    connect_bd_net [get_bd_pins $clk_wiz_shell/clk_out1] [get_bd_pins $proc_sys_reset_shell/slowest_sync_clk]
    connect_bd_net [get_bd_pins $clk_wiz_shell/locked]   [get_bd_pins $proc_sys_reset_shell/dcm_locked]

    set shell_clk       [get_bd_pins $clk_wiz_shell/clk_out1]
    set rmii_ref_clk_sig [get_bd_pins $clk_wiz_shell/clk_out2]
    set dut_clk_sig     [get_bd_pins $clk_wiz_dut/clk_out1]
    set shell_aresetn   [get_bd_pins $proc_sys_reset_shell/peripheral_aresetn]
    set shell_mb_reset  [get_bd_pins $proc_sys_reset_shell/mb_reset]
    set shell_bus_arstn [get_bd_pins $proc_sys_reset_shell/interconnect_aresetn]

    # clk_wiz_dut's AXI4-Lite control/status/DRP slave — clock+reset like
    # every other shell-domain AXI-Lite peripheral; the interface itself
    # (`s_axi_lite`) is hung off axi_interconnect_0 in SECTION 6
    # (MMCM_DUT_DRP), not here (shell_clk/shell_aresetn only just came into
    # existence above).
    connect_bd_net $shell_clk     [get_bd_pins $clk_wiz_dut/s_axi_aclk]
    connect_bd_net $shell_aresetn [get_bd_pins $clk_wiz_dut/s_axi_aresetn]

    connect_bd_net $rmii_ref_clk_sig [get_bd_ports rp_phy_rmii_ref_clk]
    connect_bd_net $dut_clk_sig      [get_bd_ports rp_dut_clk]

    ###################################################################
    # SECTION 2 — THE CPU (the seam). SHELL_CPU selects the file:
    #   mb  (default) cpu_mb.tcl  -- classic MicroBlaze bare-metal coordinator,
    #                                1 MiB LMB (the fielded shell, byte-identical
    #                                to the pre-seam BD; its local_ram sizing
    #                                note and diag-mailbox lockstep live there)
    #   mbv           cpu_mbv.tcl -- MicroBlaze V (Linux, Sv32) + mdm_riscv +
    #                                128 KiB LMB + DDR4 MIG + SmartConnect +
    #                                its own CPU/DDR reset domain (Vivado 2026.1)
    # Only the CPU file differs between the two; every block below this
    # section is shared Tcl. See the CPU SEAM note at the top of this file.
    ###################################################################

    set cpu_stage core
    source $cpu_tcl

    # -- Interrupts: AXI INTC + a small concat of shell interrupt sources --
    set axi_intc_0 [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_intc:4.1 axi_intc_0]
    set xlconcat_intr [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconcat:2.1 xlconcat_intr]
    set_property CONFIG.NUM_PORTS {4} $xlconcat_intr
    connect_bd_net $shell_clk [get_bd_pins axi_intc_0/s_axi_aclk]
    connect_bd_net $shell_aresetn [get_bd_pins axi_intc_0/s_axi_aresetn]
    # axi_intc:4.1 has no separate `processor_clk` pin (confirmed live) —
    # s_axi_aclk is its only clock; the old `processor_clk` connect_bd_net
    # was a stale/invented pin name, removed.
    connect_bd_net [get_bd_pins $xlconcat_intr/dout] [get_bd_pins axi_intc_0/intr]
    # INTC -> CPU interrupt input: a scalar net on the classic MicroBlaze, an
    # interface net on the MBV, which also pins the Linux INTC contract
    # (cpu_$SHELL_CPU.tcl `intc`).
    set cpu_stage intc
    source $cpu_tcl
    # xlconcat_intr/In0..3 wired in SECTION 4 once axi_timer/axi_uartlite/
    # axi_hwicap/eth_irq all exist.

    # -- AXI Timer + AXI UARTLite (MicroBlaze console fallback) --
    set axi_timer_0 [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_timer:2.0 axi_timer_0]
    set axi_uartlite_0 [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_uartlite:2.0 axi_uartlite_0]
    set_property CONFIG.C_BAUDRATE {115200} $axi_uartlite_0
    connect_bd_net [get_bd_ports uart_tx_f] [get_bd_pins $axi_uartlite_0/tx]
    connect_bd_net [get_bd_ports uart_rx_f] [get_bd_pins $axi_uartlite_0/rx]
    # CONFIRMED LIVE: unlike every other AXI-Lite slave in this file, these
    # two never had their own s_axi_aclk/s_axi_aresetn wired directly (only
    # the axi_interconnect's per-master M0x_ACLK/M0x_ARESETN pins were
    # connected in SECTION 6 — a separate net from the slave IP's own aclk
    # pin; the interconnect does not auto-propagate one to the other).
    # validate_bd_design correctly flagged both as having no valid clock
    # source; fixed here.
    connect_bd_net $shell_clk     [get_bd_pins $axi_timer_0/s_axi_aclk]
    connect_bd_net $shell_aresetn [get_bd_pins $axi_timer_0/s_axi_aresetn]
    connect_bd_net $shell_clk     [get_bd_pins $axi_uartlite_0/s_axi_aclk]
    connect_bd_net $shell_aresetn [get_bd_pins $axi_uartlite_0/s_axi_aresetn]
    # CPU-specific timer/console configuration (cpu_$SHELL_CPU.tcl `timer`:
    # nothing for mb; the MBV pins + read-back-asserts both timer counters).
    set cpu_stage timer
    source $cpu_tcl

    ###################################################################
    # SECTION 3 — CUSTOM SHELL CSR IP (fpga/shell/ip/, REAL RTL).
    #
    # CONFIRMED LIVE, 2024.1 (supersedes this section's original plan):
    # `create_bd_cell -type module -reference <name>` — the package-free
    # "Add Module" instantiation the W-BD task brief called for — hard-fails
    # for ALL SIX of these cells with
    #   ERROR: [filemgmt 56-195] Reference '<name>' contains top file
    #   '.../<name>.sv' of type SystemVerilog. This type is not allowed as
    #   the top file in the reference.
    # This is not the anticipated "will Vivado auto-infer the AXI4-Lite bus
    # interface for a raw module" risk — `-type module` refuses a
    # SystemVerilog top file categorically (plain-Verilog re-tagging of the
    # same source then fails to parse: these blocks genuinely use
    # logic/always_ff/etc.). The only supported route for SV RTL in a 2024.1
    # BD is a real IP-XACT component (`-type ip`).
    #
    # Fix: each block is packaged via `ipx::package_project -import_files`
    # (fpga/shell/ip_packaged/package_csr_ip.tcl, run by the caller —
    # validate_bd.tcl / build_shell.tcl — BEFORE this file is sourced, which
    # also does the `set_property ip_repo_paths` + `update_ip_catalog`).
    # Confirmed live: `-import_files` ALONE auto-infers the AXI4-Lite bus
    # interface from the `s_axi_*` naming convention (no extra
    # ipx::add_bus_interface/ipx::infer_bus_interface calls needed) — but
    # the inferred names are LOWERCASE: bus interface `s_axi` (not `S_AXI`),
    # address block `reg0` (not `Reg`). Every reference to these six cells'
    # AXI-Lite interface below (this section + SECTION 6) uses those real
    # names, not the originally-guessed uppercase ones.
    #
    # Every module's C_S_AXI_ADDR_WIDTH is still widened to 32 so the
    # AXI-Lite interconnect's full address bus connects cleanly (the
    # module's own internal decode only ever looks at the low few bits
    # regardless of port width — see each .sv's ADDR_LSB/case-statement —
    # so widening is purely a bus-width fix-up, not a functional change;
    # confirmed live that the packaged IP's `reg0` address-block RANGE
    # scales correctly off this parameter).
    ###################################################################

    set dut_clkrst_0 [create_bd_cell -type ip -vlnv soclabs.org:user:dut_clkrst:1.0 dut_clkrst_0]
    set_property CONFIG.C_S_AXI_ADDR_WIDTH {32} $dut_clkrst_0

    set dfx_ctl_0 [create_bd_cell -type ip -vlnv soclabs.org:user:dfx_ctl:1.0 dfx_ctl_0]
    set_property CONFIG.C_S_AXI_ADDR_WIDTH {32} $dfx_ctl_0

    set board_gpio_0 [create_bd_cell -type ip -vlnv soclabs.org:user:board_gpio:1.0 board_gpio_0]
    set_property CONFIG.C_S_AXI_ADDR_WIDTH {32} $board_gpio_0

    set jtag_bb_0 [create_bd_cell -type ip -vlnv soclabs.org:user:jtag_bb:1.0 jtag_bb_0]
    set_property CONFIG.C_S_AXI_ADDR_WIDTH {32} $jtag_bb_0

    set telem_0 [create_bd_cell -type ip -vlnv soclabs.org:user:telem:1.0 telem_0]
    set_property -dict [list CONFIG.C_S_AXI_ADDR_WIDTH {32} CONFIG.SIM_FAKE_DATA {0}] $telem_0

    set uart_bridge_0 [create_bd_cell -type ip -vlnv soclabs.org:user:uart_bridge:1.0 uart_bridge_0]
    set_property CONFIG.C_S_AXI_ADDR_WIDTH {32} $uart_bridge_0

    set clcd_0 [create_bd_cell -type ip -vlnv soclabs.org:user:clcd:1.0 clcd_0]
    set_property CONFIG.C_S_AXI_ADDR_WIDTH {32} $clcd_0

    # clcd_kvm_0 — CLCD-KVM ownership arbiter (Wave 4). Sits BETWEEN clcd_0 and
    # the panel pads: arbitrates the on-board QVGA panel between the harness
    # (clcd_0) and the DUT's tunnelled display (post-decoupler dut_gpio), driven
    # by the USER_nPB1 button + a CSR override @0x44AD_0000, with a forced revert
    # to the harness whenever the RP is decoupled/held-in-reset. Static
    # peripheral — takes NO dfx_decoupler entry of its own.
    set clcd_kvm_0 [create_bd_cell -type ip -vlnv soclabs.org:user:clcd_kvm:1.0 clcd_kvm_0]
    set_property CONFIG.C_S_AXI_ADDR_WIDTH {32} $clcd_kvm_0

    # dut_egress_0 — the DUT's Ethernet RETURN path (DUTEGR @0x44B2_0000).
    # Captures the frames the DUT transmits, which SECTION 5 has until now
    # drained into a constant (`mgmt_m_tready = 1` with nothing behind it), into
    # a dual-clock FIFO the MicroBlaze reads, and (mint 3) INJECTS host frames
    # into the bridge from a second FIFO the MicroBlaze writes. Created HERE with
    # the other custom CSR blocks because its AXI-Lite surface is one of theirs;
    # its capture port AND its inject port are wired in SECTION 5, where the
    # bridge lives.
    #
    # Its second clock (rmii_clk_i, the 50 MHz refclk the bridge runs in) is NOT
    # part of the common fan-out below and is connected in SECTION 5.
    set dut_egress_0 [create_bd_cell -type ip -vlnv soclabs.org:user:dut_egress:1.0 dut_egress_0]
    # ONE set_property PER LINE, not a multi-line `-dict [list ... \`:
    # scripts/harness_gates/check_bench_param_parity.py (the bug-#1 gate) matches
    # CONFIG.* and the target cell on the SAME line, so a continued dict hides
    # every parameter but the last from it -- and C_S_AXI_ADDR_WIDTH is precisely
    # the one that must not be hidden.
    set_property CONFIG.C_S_AXI_ADDR_WIDTH {32} $dut_egress_0
    set_property CONFIG.DATA_DEPTH {2048} $dut_egress_0
    set_property CONFIG.FRAME_DEPTH {16} $dut_egress_0
    set_property CONFIG.MAX_FRAME {1536} $dut_egress_0

    # usr_access_rd_0 — USRACC @0x44B3_0000, the fabric's own build identity
    # (docs/VERSIONING_PLAN.md §3.4, deferred there, built here). Wraps the ONE
    # USR_ACCESSE2 BEL on this device (CONFIG_SITE_X0Y0/USR_ACCESS, clock region
    # X5Y1 — outside the RP, which is why only the static shell can carry it)
    # and presents the AXSS word `set_property BITSTREAM.CONFIG.USR_ACCESS` put
    # in this .bit. That is what finally lets `coordinator_handle_version()`
    # populate `usr_access` beside `ver32` and make the firmware/bitstream skew
    # check a number instead of a `null` (ba2f4be shipped everything BUT the
    # sensor). Purely observational: no other block reads it, nothing depends
    # on it, and a shell built without it behaves identically except that the
    # cross-check goes back to reporting "no comparison was made".
    set usr_access_rd_0 [create_bd_cell -type ip -vlnv soclabs.org:user:usr_access_rd:1.0 usr_access_rd_0]
    set_property CONFIG.C_S_AXI_ADDR_WIDTH {32} $usr_access_rd_0

    # usd_spi_0 — USD @0x44A4_0000, the SPI-mode master for the USER microSD
    # slot (fpga/shell/ip/usd_spi, D13). Replaces axi_quad_spi_0 on the same
    # page and the same interconnect port (M03). DEBOUNCE_CYCLES is left at its
    # RTL default (10 ms at the 100 MHz shell_clk) -- which is also what
    # tests/usd_spi's CFG=bd elaborates, so do not override it here without
    # adding that value to the bench.
    set usd_spi_0 [create_bd_cell -type ip -vlnv soclabs.org:user:usd_spi:1.0 usd_spi_0]
    set_property CONFIG.C_S_AXI_ADDR_WIDTH {32} $usd_spi_0

    # ===================================================================
    # THE SHELL WATCHDOG — WDOG @0x44B4_0000
    # (docs/planning/SERVICES_PARTITION.md §5, option (d): "take the watchdog
    # and the dfx_ctl reset fix now".)
    #
    # Until now, if the superloop STOPPED -- not "ran slowly", stopped -- nothing
    # on the board noticed, and the only ingress is the network that superloop
    # serves. Recovery was a physical power-cycle of the chassis. This is the
    # 107-LUT fix for that.
    #
    # Two-stage by design (PG128): the first expiry sets WDS, a status bit
    # firmware can read and report; the second asserts Timebase_WDT_Reset.
    # Firmware kicks it by writing WDS in TCSR0, at the END of
    # mps3_service_run_pass() and only if no vital service was skipped
    # (firmware/common/service.c) -- proving the loop SERVED, not merely spun.
    #
    # PARAMETERS, MEASURED NOT GUESSED. The study recorded
    # "C_WDT_ENABLE_ONCE/C_WDT_INTERVAL were rejected by set_property ... the
    # exact parameter names need confirming". Probed against
    # axi_timebase_wdt:3.0 on xcku115 in this Vivado 2024.1:
    #   * the real names are C_WDT_INTERVAL (integer) and WDT_ENABLE_ONCE
    #     (an ENUM, valid values `Enable_repeatedly` / `Enable_only_once`);
    #   * a MISSPELLED CONFIG.* is NOT an error -- `set_property
    #     CONFIG.C_WDT_ENABLE_ONCE 0` emits only "CRITICAL WARNING [BD 41-1276]
    #     Parameter does not exist" and returns success, which is why the study
    #     read it as "rejected". Anything set that way is silently a no-op;
    #   * the DEFAULT is WDT_ENABLE_ONCE = Enable_only_once, i.e. exactly the
    #     latch-and-never-adjust behaviour §5.5 says must not ship. Leaving this
    #     parameter alone is the wrong answer, so it is set explicitly.
    #
    # C_WDT_INTERVAL is the bit of the free-running timebase the stage taps, so
    # the first expiry is 2^N / 100 MHz: N=27 -> 1.34 s, reset at ~2.7 s. The
    # twelve service budgets sum to 268 ms (platform/src/main.c), so a
    # pathological pass in which EVERY service ran to its full budget still has
    # 5x margin, and 1.34 s is >10x the 100 ms sick-service cooldown. It fires
    # only when the loop has genuinely stopped, or when a vital service has been
    # sick long enough that the board is unreachable anyway.
    set axi_timebase_wdt_0 [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_timebase_wdt:3.0 axi_timebase_wdt_0]
    # ONE set_property PER LINE (see dut_egress_0's note on the bug-#1 gate).
    set_property CONFIG.C_WDT_INTERVAL {27} $axi_timebase_wdt_0
    set_property CONFIG.WDT_ENABLE_ONCE {Enable_repeatedly} $axi_timebase_wdt_0
    connect_bd_net $shell_clk     [get_bd_pins $axi_timebase_wdt_0/s_axi_aclk]
    # The watchdog is reset by its OWN reset, like every other AXI-Lite slave on
    # peripheral_aresetn -- and that is deliberate, not an oversight. It comes
    # out of reset DISABLED (PG128), so firmware must re-arm it in
    # coordinator_init(). That is what stops a RESET LOOP: one watchdog reset,
    # then either the shell comes back and re-arms (protected again) or it does
    # not, and is left alone for JTAG/a human to reach instead of being reset
    # every 2.7 s forever. A shell that cannot re-arm is already dead; a shell
    # being reset every 2.7 s is dead AND undebuggable.
    connect_bd_net $shell_aresetn [get_bd_pins $axi_timebase_wdt_0/s_axi_aresetn]
    # freeze: hold the timebase (an external debug-halt hook). Tied off -- this
    # shell has no such signal, and an unconnected INPUT fails
    # validate_bd_design.
    set gnd_wdt_freeze [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 gnd_wdt_freeze]
    set_property -dict [list CONFIG.CONST_WIDTH {1} CONFIG.CONST_VAL {0}] $gnd_wdt_freeze
    connect_bd_net [get_bd_pins $gnd_wdt_freeze/dout] [get_bd_pins $axi_timebase_wdt_0/freeze]

    # THE RESET FAN-OUT, and the hazard it makes live.
    #
    # `aux_reset_in` is UNCONNECTED in every shell up to 0xA8C1C535
    # (only ext_reset_in, the POR button, was wired). Driving it is the standard
    # hook and gives the standard fan-out: mb_reset -> MicroBlaze + both LMBs,
    # peripheral_aresetn -> the AXI slaves, interconnect_aresetn -> the
    # interconnect. A warm restart of the shell firmware, re-running
    # coordinator_init(), which reloads the default overlay from the QSPI store.
    # The diagnostic mailbox survives (it is local_ram BRAM and a processor
    # reset does not clear it), which is what makes a watchdog reset
    # diagnosable rather than merely mysterious.
    #
    # MEASURED: proc_sys_reset:5.0's C_AUX_RESET_HIGH defaults to 1 on this
    # part/tool, and the WDT's `wdt_reset` is active high, so they connect
    # directly with no inverter. (C_AUX_RST_WIDTH is 4: peripheral_aresetn is
    # held low for a TAIL after aux_reset_in deasserts -- see dfx_ctl.sv, where
    # that tail is the reason a "clamp while wdt_reset is high" fix would not
    # have worked.)
    connect_bd_net [get_bd_pins $axi_timebase_wdt_0/wdt_reset] \
                   [get_bd_pins $proc_sys_reset_shell/aux_reset_in]

    # ...and the same pulse goes to dfx_ctl_0, SET-DOMINANT, because the moment
    # aux_reset_in is driven the DFX boundary un-clamp hazard goes live:
    # dfx_ctl_0/s_axi_aresetn IS peripheral_aresetn, so before this change a
    # watchdog fire during an ICAP write cleared decouple_en and un-clamped the
    # partition boundary while the RP was transient garbage. dfx_ctl.sv now
    # takes the watchdog reset as an input that ASSERTS isolation, and takes the
    # board POR as the only reset that releases it. The fix and the wire that
    # makes it necessary land together, on purpose
    # (docs/planning/SERVICES_PARTITION.md §5.4; tests/dfx_ctl's
    # `make control-wdt-unclamp` shows the pre-fix RTL failing).
    connect_bd_net [get_bd_pins $axi_timebase_wdt_0/wdt_reset] \
                   [get_bd_pins $dfx_ctl_0/wdt_reset_i]
    connect_bd_net [get_bd_ports sys_rst_n] [get_bd_pins $dfx_ctl_0/ext_por_n_i]
    # NOTE the POR source: the raw `sys_rst_n` PORT, not any proc_sys_reset
    # output. Every proc_sys_reset output is pulsed by aux_reset_in, i.e. by the
    # watchdog, so none of them can serve as "the reset the watchdog cannot
    # assert". sys_rst_n is asynchronous to shell_clk and dfx_ctl.sv
    # synchronises it internally (2-FF ASYNC_REG).
    #
    # wdt_interrupt / timebase_interrupt are left unconnected. The INTC concat
    # is full at 4 ports and the first-stage expiry is already visible to
    # firmware as TCSR0.WDS, which is a poll, not an interrupt -- and a
    # superloop that has stopped running cannot service an interrupt anyway.

    # Common AXI-Lite clock/reset fan-out (every custom CSR block uses the
    # identical Xilinx-template write/read FSM on s_axi_aclk/s_axi_aresetn).
    foreach csr [list $dut_clkrst_0 $dfx_ctl_0 $board_gpio_0 $jtag_bb_0 $telem_0 $uart_bridge_0 $clcd_0 $clcd_kvm_0 $dut_egress_0 $usr_access_rd_0 $usd_spi_0] {
        connect_bd_net $shell_clk     [get_bd_pins $csr/s_axi_aclk]
        connect_bd_net $shell_aresetn [get_bd_pins $csr/s_axi_aresetn]
    }

    # ===================================================================
    # DFX DECOUPLER — PER-SIGNAL RP BOUNDARY (R1). Created HERE (ahead of
    # the RP<->CSR wiring below) so every RP->static OUTPUT net can be
    # routed THROUGH the decoupler's clamp instead of bypassing it. The
    # DECOUPLE/DECOUPLE_STATUS handshake and the AXI Shutdown Manager are
    # wired later, in SECTION 4.
    #
    # WHY THIS EXISTS: with no boundary configured the IP exposes ONLY
    # `decouple`/`decouple_status` and clamps NOTHING — asserting DECOUPLE
    # gates no partition-pin, and the post-swap RM_ID read feeds the RP's
    # own bits straight back (a tautological "verified"). During an ICAP
    # swap the RP fabric is transient garbage; without this clamp a stuck
    # tx_en injects a runaway frame into link_partner_mac, a stuck
    # uart tvalid/tready floods/drains the console FIFOs, a free-running
    # mdc clocks junk into the virtual-PHY, a driven gpio_oe fights a board
    # pad, and a spurious irq_out/dut_lockup storms the MicroBlaze.
    #
    # =====================================================================
    # dfx_decoupler:1.0 CONFIG.ALL_PARAMS SCHEMA — discovered live in a
    # scratch Vivado 2024.1 project (xcku115-flvb1760-1-c) by setting a
    # minimal config and reading back the normalized ALL_PARAMS + the
    # expanded GUI_* params + the generated pins. RECORD, so nobody
    # re-derives it (every previously-guessed pin/key name was wrong):
    #
    #   CONFIG.ALL_PARAMS is a nested dict:
    #     INTF { <intf_name> { ID <n>  VLNV <vlnv>  MODE <master|slave>
    #                          SIGNALS { <SIG> { PRESENT 1  WIDTH <w>
    #                                            MANAGEMENT <auto|manual>
    #                                            DECOUPLED_VALUE <v> } } } ... }
    #   NOTE: <v> MUST be a hex STRING starting with "0x" ("0x0", not "0"). A bare
    #   integer passes set_property AND passes validate_bd_design, then fails at IP
    #   GENERATION with "isn't a valid hexidecimal value" -- i.e. an hour into the
    #   build. validate_bd_design is NOT a sufficient gate for this IP; the cheap
    #   check is `generate_target synthesis` on the decoupler alone.
    #   * MODE is the RP-side role. MODE=master  => the RP DRIVES the
    #     interface: the tool makes `rp_<intf>_<SIG>` an INPUT (from the RP)
    #     and `s_<intf>_<SIG>` an OUTPUT (to the static shell) that is held
    #     at DECOUPLED_VALUE while decouple=1. This is exactly the RP->static
    #     clamp we need for all 20 RP outputs. (MODE=slave flips the two.)
    #   * VLNV xilinx.com:signal:data_rtl:1.0 is a SINGLE-signal interface
    #     whose one signal is named DATA (any width) — used for every group
    #     here (buses and scalars alike). One INTF == one net.
    #   * MANAGEMENT: 'auto' or 'manual' ONLY ('rp' is REJECTED). manual +
    #     DECOUPLED_VALUE 0x0 forces the clamp value explicitly (GUI_SIGNAL_
    #     DECOUPLED_<n> becomes true) rather than trusting auto-inference.
    #   * Generated pins per present interface: rp_<intf>_DATA (I, to RP
    #     port) and s_<intf>_DATA (O, to the static CSR sink). The always-
    #     present `decouple`(I)/`decouple_status`(O) handshake is unchanged.
    #   * AXIS (xilinx.com:interface:axis_rtl:1.0) IS supported and would
    #     auto-manage the tvalid/tready pair, BUT it also emits a bus
    #     INTF pin; connecting its member pins to our DISCRETE uart_bridge
    #     pins (uart_bridge exposes plain signals, not an AXIS bus) throws
    #     6x [BD 41-1306] "connection ... overridden ... not connected as
    #     part of interface" warnings (confirmed live). Since BOTH sides of
    #     this whole boundary are discrete (rp_* BD ports + discrete CSR
    #     pins), AXIS grouping buys nothing and only adds warnings, so the
    #     two console pairs use plain data_rtl signal groups like everything
    #     else. Discrete data_rtl boundary validates with ZERO warnings.
    # =====================================================================
    set dfx_decoupler_0 [create_bd_cell -type ip -vlnv xilinx.com:ip:dfx_decoupler:1.0 dfx_decoupler_0]
    # 20 interfaces (IDs 0-19) = the 20 RP->static OUTPUT nets (partition-pins.md
    # / rp_dut.sv outputs): 15 from v0.1, the 4 QSPI legs (IDs 15-18, v0.2) and
    # dbg_bscan_tdo (ID 19, the ILA-over-XVC group). ALL are MODE=master (RP drives), MANAGEMENT
    # manual, DECOUPLED_VALUE 0. Per-signal safe-idle justification:
    #   jtag_tdo    =0 : TDO read-back idle line during swap.
    #   phy_rmii_txd=0 : TX nibble into link_partner_mac -> idle.
    #   phy_rmii_tx_en=0: CRITICAL — a stuck tx_en injects a runaway garbage
    #                     frame into link_partner_mac. Force no-transmit.
    #   mdc         =0 : a free-running DUT mdc would clock junk into the
    #                    virtual-PHY mdio_slave. Park the MDIO clock.
    #   mdio_o      =0 : MDIO write data from DUT -> idle.
    #   mdio_oe     =0 : CRITICAL — 0 => DUT MDIO tri-stated, virtual PHY
    #                    sees an undriven bus (no bus fight).
    #   uart_tx_tdata =0 : console byte from DUT -> idle (don't-care w/ tvalid=0).
    #   uart_tx_tvalid=0 : CRITICAL — a stuck tvalid pushes garbage into the
    #                     uart_bridge async FIFO / floods console-over-eth.
    #   uart_rx_tready=0 : CRITICAL — clamp to NOT-READY (0), NOT ready(1):
    #                     0 PAUSES the RX FIFO (backpressured, data retained)
    #                     while the consuming RP is gone; 1 would DRAIN and
    #                     LOSE the RX FIFO into the void. Pause, don't drain.
    #   swo         =0 : Cortex-M SWO/ITM trace bit -> idle.
    #   rm_id       =0 : THE POINT — held 0 => dfx_ctl RM-verify reads
    #                    rm_id_valid=false during a swap, so the post-swap
    #                    RM_ID read is a GENUINE confirmation, not the RP's
    #                    own bits fed back. Consistent w/ swap_fsm step 2/5.
    #   dut_lockup  =0 : held 0 => shell watchdog/telemetry does not trip a
    #                    false lockup while the RP is being rewritten.
    #   irq_out     =0 : held 0 => no spurious IRQ storm into the shell INTC
    #                    from a partially-configured RP (see R7 sink below).
    #   dut_gpio_o  =0 : DUT drive value toward the board pad -> 0.
    #   dut_gpio_oe =0 : CRITICAL — oe=0 forces board pads to high-Z so a
    #                    mid-swap RP cannot fight an external driver.
    #   dbg_bscan_tdo=0: BSCAN TDO back to debug_bridge_0 -> idle. A cleared
    #                    RP (no RM hub) reads as an empty BSCAN chain, not X.
    # RP INPUTS (static->RP) are deliberately NOT members (they route
    # straight through): the shell keeps driving them and the RP ignores
    # them because rp_resetn holds it in reset for the whole swap
    #  - dut_clk, phy_rmii_ref_clk        : CLOCKS — never through a decoupler.
    #  - dut_resetn, rp_resetn, dbg_resetn: RESETS — never decoupled; rp_resetn
    #                                       held-low IS the input-side isolation.
    #  - jtag_tck/jtag_tms/jtag_tdi       : JTAG drive to a dead RP is harmless.
    #  - dbg_bscan_* (11 legs, not tdo)   : BSCAN drive to a dead RP is harmless;
    #                                       TCK only toggles during an XVC shift:,
    #                                       and the swap FSM gates those.
    #  - phy_rmii_crs_dv/phy_rmii_rxd     : virtual-PHY RX to a dead RP: harmless.
    #  - mdio_i                           : virtual-PHY MDIO reply: harmless.
    #  - uart_rx_tdata/uart_rx_tvalid     : host->DUT console to a dead RP: harmless.
    #  - uart_tx_tready                   : shell-ready toward the RP: harmless.
    #  - dut_gpio_i                       : board pad -> dead RP sample: harmless.
    # NONE of these can HANG the static: the only static blocks that WAIT on
    # an RP signal are uart_bridge (on rp_uart_tx_tvalid, clamped 0 => idle)
    # and its RX drain (on rp_uart_rx_tready, clamped 0 => paused, not lost);
    # everything else the static SAMPLES, it never blocks on.
    # -- Flash / QSPI XiP interfaces (IDs 15-18, partition-pins.md v0.2,
    #    boundary freeze 2026-07-15). The RP's QSPI controller drives the
    #    board's external SST26VF064B across the boundary. These RP-drive legs
    #    route through the decoupler clamp, but this is the ONE group with a
    #    MIXED / NON-ZERO safe-idle: qspi_csn clamps to 1 (chip DESELECTED) so a
    #    swap or a decoupled RP can never leave the flash asserted; sclk/io_o/
    #    io_oe clamp to 0. The clamp is a combinational mux (NOT a synchronizer),
    #    so it does not violate the "non-CDC source-synchronous" rule. qspi_io_i
    #    (shell->RP pad sample) is NOT decoupled — a straight pass-through in
    #    shell_top.sv, never clamped.
    #    NB: this is a BRACED literal dict — do NOT put `#` comments *inside* the
    #    braces below; they are not stripped and would corrupt ALL_PARAMS.
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
        dbg_bscan_tdo  { ID 19 VLNV xilinx.com:signal:data_rtl:1.0 MODE master SIGNALS { DATA { PRESENT 1 WIDTH 1 MANAGEMENT manual DECOUPLED_VALUE 0x0 } } }
      }
    }
    set_property CONFIG.ALL_PARAMS $dfx_decoupler_boundary $dfx_decoupler_0

    # -- CLKRST wiring: DRP <-> clk_wiz_dut, dut_clk_i heartbeat tap, ext POR,
    #    rp_resetn_gate from DFXCTL, dbg_reset_req tied 0 (srst is a pure
    #    software path via RESET_CTRL[2] per jtag_bb/README, no HW pulse net) --
    # clk_wiz_dut has NO native DRP pins in 2024.1 (den/dwe/daddr/din/dout/
    # drdy/dclk do not exist once USE_DYN_RECONFIG forces AXI4-Lite-only mode
    # — confirmed live, see the NOTE at clk_wiz_dut's creation in SECTION 1).
    # dut_clkrst_0's own drp_den_o/drp_dwe_o/drp_daddr_o/drp_di_o are already
    # hardwired to constant 0 inside dut_clkrst.sv (documented placeholder —
    # "wiring an actual preset ROM + DRP FSM is future work explicitly out of
    # this deliverable's scope", per that file's header) so they have nothing
    # real to drive today; leave them unconnected (legal — internal-cell
    # output pins, not BD ports) rather than wire them to a DRP interface
    # that no longer exists on this cell. Its *inputs* still need a driver:
    set gnd_drp_do [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 gnd_drp_do]
    set_property -dict [list CONFIG.CONST_WIDTH {16} CONFIG.CONST_VAL {0}] $gnd_drp_do
    set gnd_drp_drdy [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 gnd_drp_drdy]
    set_property CONFIG.CONST_VAL {0} $gnd_drp_drdy
    connect_bd_net [get_bd_pins $gnd_drp_do/dout]   [get_bd_pins $dut_clkrst_0/drp_do_i]
    connect_bd_net [get_bd_pins $gnd_drp_drdy/dout] [get_bd_pins $dut_clkrst_0/drp_drdy_i]
    # The real DRP path is clk_wiz_dut's own s_axi_lite slave (MicroBlaze
    # writes DRP registers directly over AXI-Lite — SECTION 6, MMCM_DUT_DRP);
    # dut_clkrst_0's DUT_CLK_DRP regmap field (shell-regmap.md 0x08) is a
    # separate, currently-inert 16-bit scratch window per its own header, not
    # a path to this cell.
    connect_bd_net [get_bd_pins $clk_wiz_dut/locked]       [get_bd_pins $dut_clkrst_0/mmcm_locked_i]
    connect_bd_net $dut_clk_sig                             [get_bd_pins $dut_clkrst_0/dut_clk_i]
    connect_bd_net [get_bd_ports sys_rst_n]                [get_bd_pins $dut_clkrst_0/ext_por_n_i]
    connect_bd_net [get_bd_pins $dfx_ctl_0/rp_resetn_gate_o] [get_bd_pins $dut_clkrst_0/rp_resetn_gate_i]
    set gnd0 [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 gnd_dbg_req]
    set_property CONFIG.CONST_VAL {0} $gnd0
    connect_bd_net [get_bd_pins $gnd0/dout] [get_bd_pins $dut_clkrst_0/dbg_reset_req_i]

    connect_bd_net [get_bd_pins $dut_clkrst_0/dut_resetn_o] [get_bd_ports rp_dut_resetn]
    connect_bd_net [get_bd_pins $dut_clkrst_0/rp_resetn_o]  [get_bd_ports rp_rp_resetn]
    connect_bd_net [get_bd_pins $dut_clkrst_0/dbg_resetn_o] [get_bd_ports rp_dbg_resetn]

    # -- DFXCTL wiring: DFX Decoupler + AXI Shutdown Manager control/status
    #    (SECTION 4), RM-load-verify taps direct from the RP boundary ports --
    # R1: rp_rm_id / rp_dut_lockup routed THROUGH the decoupler clamp (was a
    # direct RP->dfx_ctl bypass). s_*_DATA = 0 while decoupled => dfx_ctl sees
    # rm_id=0 (rm_id_valid=false) and no false lockup during a swap.
    connect_bd_net [get_bd_ports rp_rm_id]                 [get_bd_pins $dfx_decoupler_0/rp_rm_id_DATA]
    connect_bd_net [get_bd_pins $dfx_decoupler_0/s_rm_id_DATA] [get_bd_pins $dfx_ctl_0/rm_id_i]
    connect_bd_net [get_bd_ports rp_dut_lockup]            [get_bd_pins $dfx_decoupler_0/rp_dut_lockup_DATA]
    connect_bd_net [get_bd_pins $dfx_decoupler_0/s_dut_lockup_DATA] [get_bd_pins $dfx_ctl_0/dut_lockup_i]
    # DUT eth irq (RP irq_out) -> dfx_ctl (2026-07-24): the decoupler's clamped
    # s_irq_out_DATA output, previously an explicit dangling tie-off, now feeds
    # dfx_ctl_0/dut_eth_irq_i. dfx_ctl 2-FF ASYNC_REG-syncs it (same as
    # dut_lockup) and surfaces it at RM_STATUS[2] -- so the DUT's ethernet RX/TX
    # activity is readable from the shell (JTAG mrd 0x44A10014 / firmware poll)
    # with NO new unsynchronised CDC. Same data_rtl-DATA-pin -> scalar-input
    # connect_bd_net as dut_lockup above (xlconcat's type check does not apply).
    connect_bd_net [get_bd_pins $dfx_decoupler_0/s_irq_out_DATA] [get_bd_pins $dfx_ctl_0/dut_eth_irq_i]
    # dfx_ctl_0/rp_in_reset_i (STATUS[1] readback of "is the RP currently
    # held in reset") was left unconnected — a real gap (validate_bd_design
    # flagged it CRITICAL WARNING [BD 41-759], auto-tied to 0, meaning
    # STATUS[1] would always read "not in reset" regardless of reality).
    # The shell already knows this: dut_clkrst_0/rp_resetn_o (active-low,
    # fanned out to the rp_rp_resetn BD port above) IS the RP reset the
    # shell itself is driving — loop an inverted copy back into dfx_ctl_0
    # rather than leaving it tied off.
    set inv_rp_in_reset [create_bd_cell -type ip -vlnv xilinx.com:ip:util_vector_logic:2.0 inv_rp_in_reset]
    set_property -dict [list CONFIG.C_SIZE {1} CONFIG.C_OPERATION {not}] $inv_rp_in_reset
    connect_bd_net [get_bd_pins $dut_clkrst_0/rp_resetn_o] [get_bd_pins $inv_rp_in_reset/Op1]
    connect_bd_net [get_bd_pins $inv_rp_in_reset/Res] [get_bd_pins $dfx_ctl_0/rp_in_reset_i]

    # -- JTAGBB wiring: straight to the RP's JTAG partition pins --
    # JTAG out (tck/tms/tdi) is static->RP: pass straight through
    # (driving a reset-held RP is harmless — NOT decoupled).
    connect_bd_net [get_bd_pins $jtag_bb_0/jtag_tck_o] [get_bd_ports rp_jtag_tck]
    connect_bd_net [get_bd_pins $jtag_bb_0/jtag_tms_o] [get_bd_ports rp_jtag_tms]
    connect_bd_net [get_bd_pins $jtag_bb_0/jtag_tdi_o] [get_bd_ports rp_jtag_tdi]
    # JTAG in (tdo) is RP->static: route THROUGH the decoupler (clamp 0).
    connect_bd_net [get_bd_ports rp_jtag_tdo]                     [get_bd_pins $dfx_decoupler_0/rp_jtag_tdo_DATA]
    connect_bd_net [get_bd_pins $dfx_decoupler_0/s_jtag_tdo_DATA] [get_bd_pins $jtag_bb_0/jtag_tdo_i]

    # -- UARTBR wiring: console/trace AXI-Stream <-> RP partition pins.
    #    dut_clk_i is the CDC tap for the async FIFOs (uart_bridge.sv owns
    #    the actual gray-pointer synchronizers). UART1 is present-but-unused
    #    (single-core v0) -> tied idle, not exposed as a partition pin. --
    connect_bd_net $dut_clk_sig [get_bd_pins $uart_bridge_0/dut_clk_i]
    # RP->static console legs (tx_tdata, tx_tvalid, rx_tready, swo) routed
    # THROUGH the decoupler clamp. (See the per-signal justification at the
    # decoupler config; discrete data_rtl groups, NOT AXIS — AXIS members vs
    # these discrete uart_bridge pins throw [BD 41-1306], confirmed live.)
    connect_bd_net [get_bd_ports rp_uart_tx_tdata]                     [get_bd_pins $dfx_decoupler_0/rp_uart_tx_tdata_DATA]
    connect_bd_net [get_bd_pins $dfx_decoupler_0/s_uart_tx_tdata_DATA] [get_bd_pins $uart_bridge_0/uart_tx_tdata_i]
    connect_bd_net [get_bd_ports rp_uart_tx_tvalid]                     [get_bd_pins $dfx_decoupler_0/rp_uart_tx_tvalid_DATA]
    connect_bd_net [get_bd_pins $dfx_decoupler_0/s_uart_tx_tvalid_DATA] [get_bd_pins $uart_bridge_0/uart_tx_tvalid_i]
    connect_bd_net [get_bd_ports rp_uart_rx_tready]                     [get_bd_pins $dfx_decoupler_0/rp_uart_rx_tready_DATA]
    connect_bd_net [get_bd_pins $dfx_decoupler_0/s_uart_rx_tready_DATA] [get_bd_pins $uart_bridge_0/uart_rx_tready_i]
    connect_bd_net [get_bd_ports rp_swo]                          [get_bd_pins $dfx_decoupler_0/rp_swo_DATA]
    connect_bd_net [get_bd_pins $dfx_decoupler_0/s_swo_DATA]      [get_bd_pins $uart_bridge_0/swo_i]
    # static->RP console legs (tx_tready, rx_tdata, rx_tvalid) pass straight
    # through — the shell drives them; the reset-held RP ignores them.
    connect_bd_net [get_bd_pins $uart_bridge_0/uart_tx_tready_o] [get_bd_ports rp_uart_tx_tready]
    connect_bd_net [get_bd_pins $uart_bridge_0/uart_rx_tdata_o]  [get_bd_ports rp_uart_rx_tdata]
    connect_bd_net [get_bd_pins $uart_bridge_0/uart_rx_tvalid_o] [get_bd_ports rp_uart_rx_tvalid]

    set gnd8 [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 gnd_uart1_tx]
    set_property -dict [list CONFIG.CONST_WIDTH {8} CONFIG.CONST_VAL {0}] $gnd8
    set gnd1 [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 gnd_uart1_v]
    set_property CONFIG.CONST_VAL {0} $gnd1
    set vcc1 [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 vcc_uart1_rdy]
    set_property CONFIG.CONST_VAL {1} $vcc1
    connect_bd_net [get_bd_pins $gnd8/dout] [get_bd_pins $uart_bridge_0/uart1_tx_tdata_i]
    connect_bd_net [get_bd_pins $gnd1/dout] [get_bd_pins $uart_bridge_0/uart1_tx_tvalid_i]
    connect_bd_net [get_bd_pins $vcc1/dout] [get_bd_pins $uart_bridge_0/uart1_rx_tready_i]
    # uart1_tx_tready_o / uart1_rx_tdata_o / uart1_rx_tvalid_o left unconnected
    # (outputs; legal to leave dangling — UART1 is a present-but-unused
    # second console until the multicore RM lands, per uart_bridge/README.md).

    # -- Board GPIO wiring: RP <-> board_gpio_0 <-> physical pads --
    # dut_gpio_o / dut_gpio_oe are RP->static: route THROUGH the decoupler
    # (clamp 0). oe=0 forces the board pads to high-Z during a swap so a
    # mid-rewrite RP cannot fight an external driver. dut_gpio_i is
    # static->RP (board pad sample to the RP): pass straight through.
    connect_bd_net [get_bd_ports rp_dut_gpio_o]                        [get_bd_pins $dfx_decoupler_0/rp_dut_gpio_o_DATA]
    connect_bd_net [get_bd_pins $dfx_decoupler_0/s_dut_gpio_o_DATA]    [get_bd_pins $board_gpio_0/dut_gpio_o_i]
    connect_bd_net [get_bd_ports rp_dut_gpio_oe]                       [get_bd_pins $dfx_decoupler_0/rp_dut_gpio_oe_DATA]
    connect_bd_net [get_bd_pins $dfx_decoupler_0/s_dut_gpio_oe_DATA]   [get_bd_pins $board_gpio_0/dut_gpio_oe_i]
    connect_bd_net [get_bd_pins $board_gpio_0/dut_gpio_i_o] [get_bd_ports rp_dut_gpio_i]
    connect_bd_net [get_bd_pins $board_gpio_0/board_pad_o]  [get_bd_ports board_gpio_pad_o]
    connect_bd_net [get_bd_pins $board_gpio_0/board_pad_oe] [get_bd_ports board_gpio_pad_oe]
    connect_bd_net [get_bd_ports board_gpio_pad_i]          [get_bd_pins $board_gpio_0/board_pad_i]

    # -- USD wiring: usd_spi_0 <-> the user-microSD pads (static only; nothing
    #    here crosses the RP boundary, so no decoupler entry) --
    connect_bd_net [get_bd_pins $usd_spi_0/usd_clk_o]   [get_bd_ports usd_clk_o]
    connect_bd_net [get_bd_pins $usd_spi_0/usd_clk_oe]  [get_bd_ports usd_clk_oe]
    connect_bd_net [get_bd_pins $usd_spi_0/usd_cmd_o]   [get_bd_ports usd_cmd_o]
    connect_bd_net [get_bd_pins $usd_spi_0/usd_cmd_oe]  [get_bd_ports usd_cmd_oe]
    connect_bd_net [get_bd_ports usd_dat0_i]            [get_bd_pins $usd_spi_0/usd_dat0_i]
    connect_bd_net [get_bd_pins $usd_spi_0/usd_dat3_o]  [get_bd_ports usd_dat3_o]
    connect_bd_net [get_bd_pins $usd_spi_0/usd_dat3_oe] [get_bd_ports usd_dat3_oe]
    connect_bd_net [get_bd_ports usd_ncd_i]             [get_bd_pins $usd_spi_0/usd_ncd_i]

    # -- Flash / QSPI XiP wiring: RP-drive legs THROUGH the decoupler clamp
    #    (qspi_csn clamps to 1 = deselected; sclk/io_o/io_oe to 0), out to the
    #    SST26 pad IOBUFs formed in shell_top.sv. This is a matched, non-CDC
    #    source-synchronous crossing — the clamp is combinational, not a
    #    synchronizer. qspi_io_i (pad -> RP) is a straight shell_top pass-through
    #    (NOT decoupled), so it has no wiring here. D16 (post-config pad owner):
    #    the RP owns the SST26; the shell has no SPI master on it at all
    #    (axi_quad_spi_0 was removed with D13 — usd_spi_0 drives the USER
    #    microSD pads, which are a different set).
    connect_bd_net [get_bd_ports rp_qspi_sclk]                     [get_bd_pins $dfx_decoupler_0/rp_qspi_sclk_DATA]
    connect_bd_net [get_bd_pins $dfx_decoupler_0/s_qspi_sclk_DATA] [get_bd_ports qspi_pad_sclk]
    connect_bd_net [get_bd_ports rp_qspi_csn]                      [get_bd_pins $dfx_decoupler_0/rp_qspi_csn_DATA]
    connect_bd_net [get_bd_pins $dfx_decoupler_0/s_qspi_csn_DATA]  [get_bd_ports qspi_pad_csn]
    connect_bd_net [get_bd_ports rp_qspi_io_o]                     [get_bd_pins $dfx_decoupler_0/rp_qspi_io_o_DATA]
    connect_bd_net [get_bd_pins $dfx_decoupler_0/s_qspi_io_o_DATA] [get_bd_ports qspi_pad_io_o]
    connect_bd_net [get_bd_ports rp_qspi_io_oe]                    [get_bd_pins $dfx_decoupler_0/rp_qspi_io_oe_DATA]
    connect_bd_net [get_bd_pins $dfx_decoupler_0/s_qspi_io_oe_DATA] [get_bd_ports qspi_pad_io_oe]

    # -- CLCD-KVM wiring (Wave 4). clcd_0 NO LONGER reaches the pads directly:
    #    it feeds clcd_kvm_0 (SOURCE A); the DUT tunnel (post-decoupler
    #    dut_gpio, SOURCE B) is tapped in; clcd_kvm_0 drives the pads. Static
    #    peripheral, never crosses the RP boundary => NO dfx_decoupler entry. --
    #
    # SOURCE A: clcd_0's re-routed pad outputs -> clcd_kvm_0 harness inputs.
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

    # SOURCE B: the DUT display tunnel — TAP the SAME post-decoupler dut_gpio
    # nets board_gpio_0 already reads (fan-out, do NOT steal: bits [7:0] still
    # drive the LEDs). Encoding frozen in docs/contracts/dut-display-tunnel.md.
    connect_bd_net [get_bd_pins $dfx_decoupler_0/s_dut_gpio_o_DATA]  [get_bd_pins $clcd_kvm_0/dut_gpio_o_i]
    connect_bd_net [get_bd_pins $dfx_decoupler_0/s_dut_gpio_oe_DATA] [get_bd_pins $clcd_kvm_0/dut_gpio_oe_i]

    # DFX interlock: force revert to the harness whenever the RP is clamped
    # (decouple_status) or held in reset (rp_resetn). Both are fan-outs of nets
    # already sourced elsewhere (decouple_status -> dfx_ctl in SECTION 4;
    # rp_resetn_o -> the rp_rp_resetn port + rp_in_reset inverter above).
    connect_bd_net [get_bd_pins $dfx_decoupler_0/decouple_status] [get_bd_pins $clcd_kvm_0/decouple_status]
    connect_bd_net [get_bd_pins $dut_clkrst_0/rp_resetn_o]        [get_bd_pins $clcd_kvm_0/rp_resetn]

    # Button: raw async active-low USER_nPB1 pad (AT32) — sync+debounce inside.
    connect_bd_net [get_bd_ports user_npb1] [get_bd_pins $clcd_kvm_0/user_npb1]

    # KVM -> the panel pads (the BD ports clcd_0 used to drive; shell_top IOBUFs).
    connect_bd_net [get_bd_pins $clcd_kvm_0/clcd_pd_o]    [get_bd_ports clcd_pd_o]
    connect_bd_net [get_bd_ports clcd_pd_i]              [get_bd_pins $clcd_kvm_0/clcd_pd_i]
    connect_bd_net [get_bd_pins $clcd_kvm_0/clcd_pd_oe]   [get_bd_ports clcd_pd_oe]
    connect_bd_net [get_bd_pins $clcd_kvm_0/clcd_cs_n_o]  [get_bd_ports clcd_cs_n]
    connect_bd_net [get_bd_pins $clcd_kvm_0/clcd_wr_n_o]  [get_bd_ports clcd_wr_n]
    connect_bd_net [get_bd_pins $clcd_kvm_0/clcd_rd_n_o]  [get_bd_ports clcd_rd_n]
    connect_bd_net [get_bd_pins $clcd_kvm_0/clcd_rs_o]    [get_bd_ports clcd_rs]
    connect_bd_net [get_bd_pins $clcd_kvm_0/clcd_bl_o]    [get_bd_ports clcd_bl]
    connect_bd_net [get_bd_pins $clcd_kvm_0/clcd_rst_n_o] [get_bd_ports clcd_rst_n]
    # clcd_kvm_0/owner_o: status tap, no pad — left unconnected (legal output).

    # -- TELEM wiring: INA228 engine seam tied off (no I2C master engine in
    #    this wave — telem/README.md's documented follow-up); alarm_i tied 0.
    #    i2c pads still brought all the way to top-level BD ports so wiring
    #    the real engine later is a firmware/one-file change, not a re-spin. --
    set gnd32 [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 gnd_ina228_32]
    set_property -dict [list CONFIG.CONST_WIDTH {32} CONFIG.CONST_VAL {0}] $gnd32
    set gnd1b [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 gnd_ina228_1]
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
    # i2c_sda_i (BD input) intentionally left unconnected downstream — no
    # consumer until ina228_i2c_master exists; validate_bd_design will flag
    # it "not driving anything" only in the reverse direction (it's a BD
    # *input*, so this is a documented, expected dangling net).

    ###################################################################
    # SECTION 4 — DFX ISOLATION + VENDOR CONFIG/DEBUG/STORAGE IP
    ###################################################################

    # -- AXI HWICAP -> ICAPE3 (spec §7, D5=HWICAP for v1) --
    set axi_hwicap_0 [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_hwicap:3.0 axi_hwicap_0]
    set_property -dict [list \
        CONFIG.C_ICAP_DWIDTH {32} \
        CONFIG.C_MODE {0} \
        CONFIG.C_WRITE_FIFO_DEPTH {1024} \
    ] $axi_hwicap_0
    # LITE -> FIFO MODE (was C_MODE {1} = lite). Correcting the older comment:
    # C_MODE is NOT "0=OPB / 1=AXI-native" — it is the write-FIFO enable, a
    # checkbox param (choice_pairs 1=True(Lite) / 0=False(FIFO)). DISCOVERED
    # LIVE via report_property / list_property_value on a scratch axi_hwicap:3.0
    # cell (xcku115-flvb1760-1-c, Vivado 2024.1; the IP's own fresh default is
    # C_MODE=0 — the BD had explicitly FORCED lite):
    #   * CONFIG.C_MODE {1} instantiates NO write FIFO (WFV maxes at 1, one
    #     StartConfig per config word) and makes xparameters.h emit
    #     XPAR_AXI_HWICAP_0_MODE 1 -> the slow one-word-at-a-time path in the BSP
    #     driver (hwicap_v11_6/src/xhwicap.c `#if (XPAR_HWICAP_0_MODE == 1)`).
    #     That path is too fragile for a ~908 KB partial over the wire (stalls
    #     mid-stream on real silicon). CONFIG.C_MODE {0} instantiates a real
    #     write FIFO and makes XPAR_..._MODE 0 -> the driver's `#else` WFV-batch/
    #     StartConfig path (the standard robust HWICAP partial-reconfig config).
    #   * CONFIG.C_WRITE_FIFO_DEPTH — comboBox, valid {64 128 256 512 1024}
    #     (component.xml min 64 / max 1024, default 64); ENABLED only when
    #     C_MODE==0 (xgui update_PARAM_VALUE.C_WRITE_FIFO_DEPTH greys it out under
    #     lite). Set to the largest (1024 words = 4 KiB) so firmware can batch up
    #     to a full FIFO of words per StartConfig. (C_READ_FIFO_DEPTH left default
    #     128 — the read path is unused here.)
    #   * CONFIG.C_ICAP_DWIDTH {32} — unchanged (UltraScale+ ICAPE3 is 32-bit).
    # icap_clk stays tied to s_axi_aclk (same clock, see below): with one common
    # clock the write FIFO is legal (validate_PARAM_VALUE.C_ENABLE_ASYNC only
    # constrains DISABLING the FIFO, not enabling it), so no clocking change is
    # needed. Firmware side: build the coordinator ELF with HWICAP_FIFO=1 so its
    # writers use the matching WFV-paced FIFO protocol (firmware/platform/Makefile).
    connect_bd_net $shell_clk [get_bd_pins axi_hwicap_0/s_axi_aclk]
    connect_bd_net $shell_aresetn [get_bd_pins axi_hwicap_0/s_axi_aresetn]
    connect_bd_net $shell_clk [get_bd_pins axi_hwicap_0/icap_clk] -quiet

    # -- DFX Decoupler handshake + AXI Shutdown Manager (spec §7 "decoupling
    #    is mandatory", §16 "static shell must survive RP teardown").
    #
    #    The decoupler CELL + its full 15-interface per-signal RP boundary
    #    (R1) is created earlier, in SECTION 3, so the RP<->CSR nets can be
    #    routed through its clamp at their natural wiring sites. Here we wire
    #    only the DECOUPLE/DECOUPLE_STATUS control handshake + the AXI
    #    Shutdown Manager.
    #
    #    v0 has NO shell<->DUT AXI (partition-pins.md line 8), so there is no
    #    wide AXI bus for the Shutdown Manager to quiesce yet — it is
    #    instantiated per the task brief and wired to dfx_ctl_0's
    #    control/status pins so the sequencing (spec §6.2 steps 2/5) is real,
    #    but its AXI-side ports are left unconnected/reserved: this is the
    #    pre-wired slot for the v1+ optional MMIO bridge (spec §4.1). --
    set dfx_axi_shutdown_manager_0 [create_bd_cell -type ip -vlnv xilinx.com:ip:dfx_axi_shutdown_manager:1.0 dfx_axi_shutdown_manager_0]
    # CONFIRMED LIVE (2024.1): dfx_axi_shutdown_manager:1.0's real pins are
    # `clk`/`resetn` (not `aclk`/`aresetn`) and `request_shutdown` (I) /
    # `shutdown_requested` (O) (not `shutdown_req`/`shutdown_ack`); it also
    # has real `S_AXI`/`M_AXI` aximm_rtl bus interfaces (left intentionally
    # unconnected here — the v1+ MMIO-bridge slot).
    connect_bd_net [get_bd_pins $dfx_ctl_0/decouple_en_o] [get_bd_pins $dfx_decoupler_0/decouple]
    # decouple_status now feeds a REAL clamp fabric (boundary configured in
    # SECTION 3), so the -quiet guard is dropped — the pin is guaranteed present.
    connect_bd_net [get_bd_pins $dfx_decoupler_0/decouple_status] [get_bd_pins $dfx_ctl_0/decoupled_i]
    connect_bd_net [get_bd_pins $dfx_ctl_0/axi_shutdown_req_o] [get_bd_pins $dfx_axi_shutdown_manager_0/request_shutdown]
    # NOTHING CAN WAIT ON THIS ACK, and that is deliberate rather than an
    # oversight -- write it down so the next person does not go looking for the
    # bounded wait that would pair with it. dfx_ctl accepts axi_shutdown_ack_i
    # electrically but does not publish it in STATUS (dfx_ctl.sv's own note, and
    # the open question there: "add a STATUS[2]=shutdown_idle bit if firmware
    # ever needs to poll it"). So the shell ASSERTS shutdown and proceeds; there
    # is no register bit for a firmware mps3_spin_until() to read, and adding
    # one is an RTL change, not a firmware change. Note also the `-quiet`: if
    # the vendor ever renames this pin the connection silently does not happen,
    # and because nothing reads the ack, nothing downstream would notice.
    connect_bd_net [get_bd_pins $dfx_axi_shutdown_manager_0/shutdown_requested] [get_bd_pins $dfx_ctl_0/axi_shutdown_ack_i] -quiet
    connect_bd_net $shell_clk [get_bd_pins $dfx_axi_shutdown_manager_0/clk]
    connect_bd_net $shell_aresetn [get_bd_pins $dfx_axi_shutdown_manager_0/resetn]

    # -- (axi_quad_spi_0 / OVLSTORE removed with D13: the pad-less AXI Quad SPI
    #    that held 0x44A4_0000 is replaced by usd_spi_0, SECTION 3. The overlay
    #    store now lives on the user microSD card.) --

    # -- Debug Bridge (DBGBR — AXI->BSCAN mode, XVC-over-Ethernet path).
    #    C_DEBUG_MODE=2 = From_AXI_to_BSCAN: axi_jtag + a soft BSCAN inside the
    #    IP; its TCK (80 ns, IP-derived clock) sits on a STATIC BUFGCE
    #    (USE_SOFTBSCAN.U_TAP_TCKBUFG). C_NUM_BS_MASTER=1 adds ONE BSCAN master
    #    port, m0_bscan_*, which leaves the BD as the rp_dbg_bscan_* partition
    #    pins so an RM can carry its own mode-1 debug_bridge hub + ILAs, reached
    #    over XVC (handover RM_ILA_OVER_XVC §4.2). No other external ports.
    #    Do NOT add a clock buffer for m0_bscan_tck: it already leaves the
    #    static BUFGCE, and the RP pblock has no BUFG site (HDPR-18).
    #    NO DEBUG SLAVE MAY LIVE IN THE STATIC (no ila/vio/system_ila IP, no
    #    mark_debug): with C_NUM_BS_MASTER=1 Vivado refuses to insert a static
    #    debug hub ("Insertion of debug hub is not supported when there are
    #    instantiated debug bridge cores in either master mode or switch
    #    enabled") and instead wires any static ILA to the RM's hub by punching
    #    new ports into the RP cell -- a silent boundary change (ILA spike 3,
    #    2026-09-23, handover U3 = negative). ILAs go in RMs only. --
    set debug_bridge_0 [create_bd_cell -type ip -vlnv xilinx.com:ip:debug_bridge:3.0 debug_bridge_0]
    set_property -dict [list CONFIG.C_DEBUG_MODE {2} CONFIG.C_NUM_BS_MASTER {1}] $debug_bridge_0
    connect_bd_net $shell_clk [get_bd_pins debug_bridge_0/s_axi_aclk]
    connect_bd_net $shell_aresetn [get_bd_pins debug_bridge_0/s_axi_aresetn]
    # BSCAN out (11 legs) is static->RP: pass straight through, like the JTAG
    # legs (NOT decoupled; boundary rules forbid a clamp on a shell-driven leg).
    # XVC gating in the swap FSM keeps TCK still during a swap (TCK toggles only
    # during a shift:), so a reset-held RP sees a quiet group.
    connect_bd_net [get_bd_pins $debug_bridge_0/m0_bscan_bscanid_en] [get_bd_ports rp_dbg_bscan_bscanid_en]
    connect_bd_net [get_bd_pins $debug_bridge_0/m0_bscan_capture] [get_bd_ports rp_dbg_bscan_capture]
    connect_bd_net [get_bd_pins $debug_bridge_0/m0_bscan_drck] [get_bd_ports rp_dbg_bscan_drck]
    connect_bd_net [get_bd_pins $debug_bridge_0/m0_bscan_reset] [get_bd_ports rp_dbg_bscan_reset]
    connect_bd_net [get_bd_pins $debug_bridge_0/m0_bscan_runtest] [get_bd_ports rp_dbg_bscan_runtest]
    connect_bd_net [get_bd_pins $debug_bridge_0/m0_bscan_sel] [get_bd_ports rp_dbg_bscan_sel]
    connect_bd_net [get_bd_pins $debug_bridge_0/m0_bscan_shift] [get_bd_ports rp_dbg_bscan_shift]
    connect_bd_net [get_bd_pins $debug_bridge_0/m0_bscan_tck] [get_bd_ports rp_dbg_bscan_tck]
    connect_bd_net [get_bd_pins $debug_bridge_0/m0_bscan_tdi] [get_bd_ports rp_dbg_bscan_tdi]
    connect_bd_net [get_bd_pins $debug_bridge_0/m0_bscan_tms] [get_bd_ports rp_dbg_bscan_tms]
    connect_bd_net [get_bd_pins $debug_bridge_0/m0_bscan_update] [get_bd_ports rp_dbg_bscan_update]
    # BSCAN in (tdo) is RP->static: route THROUGH the decoupler (INTF 19, clamp 0).
    connect_bd_net [get_bd_ports rp_dbg_bscan_tdo]                     [get_bd_pins $dfx_decoupler_0/rp_dbg_bscan_tdo_DATA]
    connect_bd_net [get_bd_pins $dfx_decoupler_0/s_dbg_bscan_tdo_DATA] [get_bd_pins $debug_bridge_0/m0_bscan_tdo]

    # -- AXI EMC -> LAN9220 host I/F (spec §4.1/§12) --
    set axi_emc_0 [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_emc:3.0 axi_emc_0]
    set_property -dict [list \
        CONFIG.C_INCLUDE_NEGEDGE_IOREGS {0} \
        CONFIG.C_MEM0_TYPE {1} \
        CONFIG.C_MEM0_WIDTH {16} \
        CONFIG.C_TCEDV_PS_MEM_0 {50000} \
        CONFIG.C_TAVDV_PS_MEM_0 {50000} \
        CONFIG.C_THZCE_PS_MEM_0 {25000} \
        CONFIG.C_THZOE_PS_MEM_0 {25000} \
        CONFIG.C_TWC_PS_MEM_0 {100000} \
        CONFIG.C_TWP_PS_MEM_0 {50000} \
    ] $axi_emc_0
    # LAN9220 bank-0 AC timing (ps) — derived from the datasheet-based budget
    # in fpga/ethernet/lan9220_if/README.md "AC timing basis": the axi_emc
    # DEFAULTS (TCEDV/TAVDV = 15 ns) are FASTER than the LAN9220's 30 ns
    # read-data-valid spec, i.e. the default core samples reads too early —
    # a real, silent hardware bug (found in the first full W-BD build pass,
    # 2026-07-06, by inspecting the generated core's generics; the shell's
    # netlist timing is unaffected, only the pacing of the external bus).
    #   TCEDV/TAVDV 50 ns  (>= 30 ns data-valid + shared-SMBF-bus margin)
    #   THZCE/THZOE 25 ns  (bus-turnaround stretch so back-to-back reads
    #                       respect the 80-100 ns cycle floor)
    #   TWC 100 ns / TWP 50 ns (write cycle/pulse per the README table)
    # Values are the deliberately-conservative first-bring-up set: too-slow
    # costs 10/100 bandwidth, too-fast risks silent data corruption. Primary
    # LAN9220 datasheet AC table remains UNVERIFIED (lan9220_if README's
    # "Not yet verified" flag, A6) — revisit before tightening.
    # CONFIRMED LIVE (2024.1) — three property-name/existence corrections
    # from the original guess:
    #  - `C_INCLUDE_NEGEDGE_IO` -> `C_INCLUDE_NEGEDGE_IOREGS` (extra "REGS").
    #  - `C_MEM0_ADDR_BITS` does not exist at all on axi_emc:3.0 — the
    #    memory-bank aperture size is NOT a per-cell CONFIG property; the
    #    16 MiB LAN9220 window is set purely via `assign_bd_address -range
    #    16M` in SECTION 6 (confirmed live: the address-assignment range is
    #    independent of the S_AXI_MEM/MEM0 segment's own nominal RANGE
    #    metadata — assigning a 16M window onto a segment that reports a
    #    smaller default RANGE works fine).
    #  - `C_TAXI_CLK_FREQ_HZ` does not exist; the real property
    #    (`C_AXI_CLK_PERIOD_PS`) is READ-ONLY (auto-derived from the
    #    connected clock once wired) — not settable at all, so dropped
    #    rather than replaced.
    # C_MEM0_TYPE {1} = SRAM-style async memory, matching spec §4.1 "AXI EMC
    # (or tiny SMC master)" for the LAN9220's non-multiplexed 16-bit register/
    # FIFO host interface.
    connect_bd_net $shell_clk [get_bd_pins axi_emc_0/s_axi_aclk]
    connect_bd_net $shell_aresetn [get_bd_pins axi_emc_0/s_axi_aresetn]
    # axi_emc:3.0 also has its own `rdclk` input (the memory-side read-data
    # capture clock, separate from `s_axi_aclk`) — confirmed live,
    # validate_bd_design flags it if left unconnected. Same synchronous
    # shell clock domain as everything else here (no separate memory clock
    # in this design), so it's just shell_clk again.
    connect_bd_net $shell_clk [get_bd_pins axi_emc_0/rdclk]
    connect_bd_intf_net [get_bd_intf_pins axi_emc_0/EMC_INTF] [get_bd_intf_ports EMC_INTF]

    # -- Interrupt concat: HWICAP, AXI Timer, AXI UARTLite, eth_irq --
    connect_bd_net [get_bd_pins axi_hwicap_0/ip2intc_irpt] [get_bd_pins $xlconcat_intr/In0] -quiet
    connect_bd_net [get_bd_pins $axi_timer_0/interrupt]    [get_bd_pins $xlconcat_intr/In1]
    connect_bd_net [get_bd_pins $axi_uartlite_0/interrupt] [get_bd_pins $xlconcat_intr/In2]
    connect_bd_net [get_bd_ports eth_irq]                   [get_bd_pins $xlconcat_intr/In3]
    # R7 (dangling rp_irq_out): rp_irq_out was created (SECTION 0) and wired to
    # NOTHING (validate_bd_design would auto-tie it CRITICAL WARNING [BD 41-759]).
    # Terminate it by routing it THROUGH the decoupler (rp_irq_out_DATA), which
    # (a) drives the formerly-dangling BD input so it is no longer dangling, and
    # (b) clamps it to 0 during a swap exactly like every other RP output. The
    # decoupler's s_irq_out_DATA OUTPUT is now consumed by dfx_ctl_0 (a 2-FF
    # ASYNC_REG sync -> RM_STATUS[2], wired in SECTION 3 above), NOT the INTC.
    # It is deliberately NOT wired into the INTC:
    #   1. CDC HAZARD: irq_out is dut_clk-domain; feeding it raw into the
    #      shell-clk axi_intc would CREATE a new unsynchronized crossing —
    #      precisely the R4-class defect this hardening pass exists to remove.
    #      (rm_id/dut_lockup/irq_out reach the shell only via dfx_ctl's 2-FF
    #      ASYNC_REG syncs -> readable RM_ID/RM_STATUS, never a raw INTC edge.)
    #   2. TYPE: the decoupler's s_*_DATA pin carries a data_rtl interface type;
    #      xlconcat rejects it alongside the plain-scalar In0..3 with
    #      [xlconcat-10] "input pins ... different type" (confirmed live).
    #   3. No firmware consumes a DUT IRQ and shell-regmap.md allocates no INTC
    #      bit for it, so an INTC line now would be speculative.
    # (2026-07-24: the platform now DOES observe the DUT IRQ -- via the
    # dfx_ctl RM_STATUS[2] sync above, the R4-safe route. A future INTC line
    # for firmware-driven servicing remains a drop-in on top of that.)
    connect_bd_net [get_bd_ports rp_irq_out] [get_bd_pins $dfx_decoupler_0/rp_irq_out_DATA]

    ###################################################################
    # SECTION 5 — ETHERNET MAC-VERIFICATION SUBSYSTEM (LANDED 2026-07-24).
    # Was DEFERRED: the RP-facing RMII/MDIO group was tied to xlconstant zeros
    # and the decoupler's static-side outputs dangled, so on shipped silicon the
    # DUT MAC saw a permanent ZERO on RX by design. This section now wires the
    # real virtual PHY.
    #
    # eth_mac_test_subsystem (fpga/ethernet/) integrates rmii_phy_if +
    # link_partner_mac + eth_bridge_3port + mdio_phy_model + gen_checker in the
    # ARCHITECTURE_SPEC §8 topology. The shell plays the PHY role toward the DUT
    # MAC: it SOURCES the 50 MHz RMII reference, drives crs_dv/rxd at the RP
    # boundary, samples txd/tx_en back, and answers the DUT's MDIO polls. All of
    # it is inside fabric — this path touches no package pin and no physical PHY
    # (that is the separate ethernet-subsystem-ahb arm_mps3 target).
    #
    # Proven in simulation by tests/eth_mac_subsystem (the §8 integration bench)
    # and tests/nanosoc_multicore_eth (the real RM across a modelled DFX
    # boundary, 5/5). This is the first time it exists in a bitstream.
    #
    # Bridge port A (uplink->LAN9220) is SAFE-TIED here. Port B (mgmt) is
    # dut_egress_0 (DUTEGR) in both directions: its egress is the DUT's return
    # path, its ingress the host's inject path. Read the tready comment below
    # before touching the uplink ties.
    ###################################################################

    # -- 50 MHz datapath-domain reset ------------------------------------
    # The subsystem's rst_i is ACTIVE-HIGH and lives in the refclk (50 MHz)
    # domain; the shell only has an active-LOW 100 MHz-synchronous
    # shell_aresetn. A dedicated proc_sys_reset in the 50 MHz domain gives BOTH
    # polarities from ONE state machine (so the GENCHK clock-converter's
    # m_axi_aresetn and gen_checker's internal ~rst_i can never leave reset
    # inconsistently), synchronises deassertion on the clock that CONSUMES it,
    # and picks up MMCM-lock gating. Inverting shell_aresetn would work today
    # only because clk_out1/clk_out2 are the same MMCM at 2:1 -- an undocumented
    # dependency that breaks silently if CLKOUT2 is ever retuned.
    set proc_sys_reset_rmii [create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 proc_sys_reset_rmii]
    connect_bd_net [get_bd_ports sys_rst_n]            [get_bd_pins $proc_sys_reset_rmii/ext_reset_in]
    connect_bd_net $rmii_ref_clk_sig                   [get_bd_pins $proc_sys_reset_rmii/slowest_sync_clk]
    connect_bd_net [get_bd_pins $clk_wiz_shell/locked] [get_bd_pins $proc_sys_reset_rmii/dcm_locked]
    set rmii_reset   [get_bd_pins $proc_sys_reset_rmii/peripheral_reset]    ;# ACTIVE-HIGH, 50 MHz
    set rmii_aresetn [get_bd_pins $proc_sys_reset_rmii/peripheral_aresetn]  ;# ACTIVE-LOW,  50 MHz

    # -- the virtual-PHY / MAC-verification subsystem ---------------------
    # Packaged by ip_packaged/package_csr_ip.tcl (a -type module cell cannot
    # take a SystemVerilog top in 2024.1). Integrates rmii_phy_if +
    # link_partner_mac + eth_bridge_3port + mdio_phy_model + gen_checker in the
    # ARCHITECTURE_SPEC §8 topology, proven in sim by tests/eth_mac_subsystem.
    set eth_ss_0 [create_bd_cell -type ip -vlnv soclabs.org:user:eth_mac_test_subsystem:1.0 eth_mac_test_subsystem_0]
    connect_bd_net $rmii_ref_clk_sig [get_bd_pins $eth_ss_0/refclk_i]
    connect_bd_net $rmii_reset       [get_bd_pins $eth_ss_0/rst_i]
    # VPHY's AXI-Lite has its OWN clock port and a genuine internal 2-FF CDC
    # into the DUT's mdc domain (R4), so it runs at the shell's 100 MHz like
    # every other AXI-Lite slave. NO clock converter needed for VPHY.
    connect_bd_net $shell_clk     [get_bd_pins $eth_ss_0/s_axi_vphy_aclk]
    connect_bd_net $shell_aresetn [get_bd_pins $eth_ss_0/s_axi_vphy_aresetn]

    # static->RP RMII/MDIO RX group: driven by the virtual PHY (was tied to
    # xlconstant zeros while SECTION 5 was deferred). NOT decoupled (static->RP;
    # a reset-held RP ignores them).
    #
    # SHELL_REALPHY: the RP RX group is driven from the real LAN8720's RMII/MDIO
    # INPUT pads instead of the virtual PHY. eth_ss_0's phy_rmii_crs_dv_o /
    # phy_rmii_rxd_o / mdio_i_o are then left dangling (legal — dangling cell
    # OUTPUTS validate; only dangling INPUTS auto-tie). This is a pad-input ->
    # RP-output feed-through: the shield pad the physical PHY drives becomes the
    # RP's RX.
    if {$shell_realphy} {
        connect_bd_net [get_bd_ports phy_pad_rmii_crs_dv] [get_bd_ports rp_phy_rmii_crs_dv]
        connect_bd_net [get_bd_ports phy_pad_rmii_rxd]    [get_bd_ports rp_phy_rmii_rxd]
        connect_bd_net [get_bd_ports phy_pad_mdio_i]      [get_bd_ports rp_mdio_i]
    } else {
        connect_bd_net [get_bd_pins $eth_ss_0/phy_rmii_crs_dv_o] [get_bd_ports rp_phy_rmii_crs_dv]
        connect_bd_net [get_bd_pins $eth_ss_0/phy_rmii_rxd_o]    [get_bd_ports rp_phy_rmii_rxd]
        connect_bd_net [get_bd_pins $eth_ss_0/mdio_i_o]          [get_bd_ports rp_mdio_i]
    }
    # phy_rmii_ref_clk_o is DELIBERATELY LEFT DANGLING. rmii_phy_if.sv assigns
    # it straight from refclk_i (a pure feed-through, no ODDR/BUFG), and
    # rp_phy_rmii_ref_clk is ALREADY driven from clk_wiz_shell/clk_out2 above --
    # the two are the same net, so connecting it would put a second driver on a
    # BD output port. Do not "fix" this dangling pin.

    # RP->static RMII/MDIO TX group (phy_rmii_txd, phy_rmii_tx_en, mdc,
    # mdio_o, mdio_oe): these ARE RP outputs, so — unlike the pre-decoupler
    # code that left them dangling — route the RP side THROUGH the decoupler
    # NOW (R1 "author the boundary once", DFX_DECOUPLER_BOUNDARY.md §5). The
    # clamp is live immediately (phy_rmii_tx_en=0 => no runaway frame; mdc=0
    # => no junk MDIO clock; mdio_oe=0 => DUT MDIO tri-stated). The s_*_DATA
    # OUTPUTS are intentionally left dangling until SECTION 5's virtual-PHY
    # subsystem lands — then connect e.g. s_phy_rmii_txd_DATA -> rmii_phy_if_0
    # / s_mdc_DATA,s_mdio_o_DATA,s_mdio_oe_DATA -> mdio_phy_model_0. Dangling
    # cell OUTPUTS are legal (validate_bd_design only auto-ties dangling INPUTS).
    connect_bd_net [get_bd_ports rp_phy_rmii_txd]   [get_bd_pins $dfx_decoupler_0/rp_phy_rmii_txd_DATA]
    connect_bd_net [get_bd_ports rp_phy_rmii_tx_en] [get_bd_pins $dfx_decoupler_0/rp_phy_rmii_tx_en_DATA]
    connect_bd_net [get_bd_ports rp_mdc]            [get_bd_pins $dfx_decoupler_0/rp_mdc_DATA]
    connect_bd_net [get_bd_ports rp_mdio_o]         [get_bd_pins $dfx_decoupler_0/rp_mdio_o_DATA]
    connect_bd_net [get_bd_ports rp_mdio_oe]        [get_bd_pins $dfx_decoupler_0/rp_mdio_oe_DATA]
    # ...and the decoupler's static-side outputs finally have a consumer. The
    # clamp is now LOAD-BEARING: tx_en=0 => no runaway frame into
    # link_partner_mac; mdc=0 => no junk MDIO clock; mdio_oe=0 => DUT MDIO
    # tri-stated. Combinational mux, not a synchroniser -- the DUT's txd/tx_en
    # are source-synchronous to the SAME 50 MHz refclk the shell sourced, so
    # this stays a matched non-CDC path; the single static-side sampling flop
    # lives inside rmii_phy_if. Do NOT re-register here.
    connect_bd_net [get_bd_pins $dfx_decoupler_0/s_phy_rmii_txd_DATA]   [get_bd_pins $eth_ss_0/phy_rmii_txd_i]
    connect_bd_net [get_bd_pins $dfx_decoupler_0/s_phy_rmii_tx_en_DATA] [get_bd_pins $eth_ss_0/phy_rmii_tx_en_i]
    connect_bd_net [get_bd_pins $dfx_decoupler_0/s_mdc_DATA]            [get_bd_pins $eth_ss_0/mdc_i]
    connect_bd_net [get_bd_pins $dfx_decoupler_0/s_mdio_o_DATA]         [get_bd_pins $eth_ss_0/mdio_o_i]
    connect_bd_net [get_bd_pins $dfx_decoupler_0/s_mdio_oe_DATA]        [get_bd_pins $eth_ss_0/mdio_oe_i]

    # SHELL_REALPHY: fan the RP's (decoupler-clamped) TX group out to real shield
    # pads for the physical LAN8720, IN ADDITION to eth_ss_0 above. eth_ss_0 then
    # stays a passive TX MONITOR (its RX outputs dangle, but GENCHK still counts
    # the DUT's TX frames and the VPHY CSR window stays mapped — no interconnect
    # churn, no static_id delta). These are the same decoupler s_*_DATA nets
    # already feeding eth_ss_0, so each becomes a legal 1-source/2-sink fan-out;
    # the clamp still forces safe idle during a swap (tx_en=0 / mdc=0 / mdio_oe=0
    # -> the real PHY sees no runaway frame, no junk MDIO). The 50 MHz REF_CLK is
    # SHELL-SOURCED (Option A) and driven OUT the ref pad to the LAN8720 REFCLK
    # strap; it is the SAME clk_out2 net that already feeds the RP + proc_sys.
    if {$shell_realphy} {
        connect_bd_net $rmii_ref_clk_sig                                   [get_bd_ports phy_pad_rmii_ref_clk]
        connect_bd_net [get_bd_pins $dfx_decoupler_0/s_phy_rmii_txd_DATA]   [get_bd_ports phy_pad_rmii_txd]
        connect_bd_net [get_bd_pins $dfx_decoupler_0/s_phy_rmii_tx_en_DATA] [get_bd_ports phy_pad_rmii_tx_en]
        connect_bd_net [get_bd_pins $dfx_decoupler_0/s_mdc_DATA]            [get_bd_ports phy_pad_mdc]
        connect_bd_net [get_bd_pins $dfx_decoupler_0/s_mdio_o_DATA]         [get_bd_ports phy_pad_mdio_o]
        connect_bd_net [get_bd_pins $dfx_decoupler_0/s_mdio_oe_DATA]        [get_bd_ports phy_pad_mdio_oe]
    }

    # -- bridge port A (uplink): SAFE-TIED. Port B (mgmt) NO LONGER IS. -----
    # The uplink endpoint still does not exist (lan9220_if is a stub by
    # decision). Port B is now dut_egress_0's in BOTH directions, wired below:
    # its management EGRESS feeds the capture FIFO (DUTEGR RX), and its
    # management INGRESS (mgmt_s_*) is driven by dut_egress_0/inj_m_* -- the
    # host -> DUT inject path (DUTEGR TX, docs/planning/HANDOVER_DUT_INJECT.md,
    # mint 3). So `mgmt` is gone from this tie loop and from the m_tready
    # constant; only the uplink's *_s_* are tied off.
    #
    # *_m_tready MUST BE 1, NOT 0. This is the OPPOSITE of the decoupler's
    # "clamp to NOT-READY / pause, don't drain" doctrine, and the difference is
    # load-bearing: eth_bridge_3port is store-and-forward with ONE shared
    # round-robin sequencer and head-of-line blocking BY DESIGN, and it FLOODS
    # broadcast/unknown-destination frames to every non-ingress port. With
    # tready=0 the first ARP the DUT emits would park the sequencer FOREVER,
    # killing the DUT<->mgmt path too. tready=1 drains flooded copies harmlessly.
    # The decoupler case differs because there the consumer is coming back and
    # the data must be retained; here the consumer does not exist, so pausing is
    # a permanent deadlock rather than a pause.
    # *_s_tvalid=0 guarantees no frame is ever injected from the tied side.
    # (uplink only: port B's ingress is the inject path below.)
    set gnd_axis8 [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 gnd_axis_tdata8]
    set_property -dict [list CONFIG.CONST_WIDTH {8} CONFIG.CONST_VAL {0}] $gnd_axis8
    set gnd_axis1 [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 gnd_axis_ctrl]
    set_property CONFIG.CONST_VAL {0} $gnd_axis1
    set vcc_axis1 [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 vcc_axis_tready]
    set_property CONFIG.CONST_VAL {1} $vcc_axis1
    foreach port {uplink} {
        connect_bd_net [get_bd_pins $gnd_axis8/dout] [get_bd_pins $eth_ss_0/${port}_s_tdata]
        connect_bd_net [get_bd_pins $gnd_axis1/dout] [get_bd_pins $eth_ss_0/${port}_s_tvalid]
        connect_bd_net [get_bd_pins $gnd_axis1/dout] [get_bd_pins $eth_ss_0/${port}_s_tlast]
    }
    connect_bd_net [get_bd_pins $vcc_axis1/dout] [get_bd_pins $eth_ss_0/uplink_m_tready]

    # -- the DUT's RETURN PATH: bridge mgmt EGRESS -> dut_egress_0 ----------
    # This is the half of Option C that was designed (docs/DUT_ETHERNET_EGRESS.md)
    # and never built. DUT *reception* has been silicon-proven since 2026-07-30
    # (DFXCTL.RM_STATUS[2] 0 -> 1 under gen_checker traffic); until this cell
    # existed the frames the DUT transmitted reached this exact net and were
    # thrown away, so a DUT could be talked to and could not answer.
    #
    # dut_egress_0 drives mgmt_m_tready CONSTANT 1 from inside the block -- it
    # never backpressures. That is not laziness, it is the same constraint the
    # tie-off above was written for: eth_bridge_3port has ONE store-and-forward
    # round-robin sequencer with head-of-line blocking, so a sink that stalls
    # this port parks the WHOLE bridge (the DUT's ingress scoring and the
    # LAN9220 uplink with it). The block therefore drops frames it cannot hold
    # and COUNTS them (DUTEGR.DROP_FULL / DROP_GIANT / STATUS.OVF); a silently
    # dropping FIFO would be worse than none.
    #
    # No decoupler entry: this is a static-to-static net INSIDE the shell. The
    # RP-facing clamp that protects it is the RMII TX group above, which already
    # forces tx_en = 0 during a swap, so a mid-swap RP cannot inject a frame
    # into link_partner_mac at all.
    connect_bd_net $rmii_ref_clk_sig                      [get_bd_pins $dut_egress_0/rmii_clk_i]
    connect_bd_net [get_bd_pins $eth_ss_0/mgmt_m_tdata]   [get_bd_pins $dut_egress_0/frm_tdata_i]
    connect_bd_net [get_bd_pins $eth_ss_0/mgmt_m_tvalid]  [get_bd_pins $dut_egress_0/frm_tvalid_i]
    connect_bd_net [get_bd_pins $eth_ss_0/mgmt_m_tlast]   [get_bd_pins $dut_egress_0/frm_tlast_i]
    connect_bd_net [get_bd_pins $dut_egress_0/frm_tready_o] [get_bd_pins $eth_ss_0/mgmt_m_tready]

    # -- the host -> DUT INJECT PATH: dut_egress_0 -> bridge mgmt INGRESS ----
    # docs/planning/HANDOVER_DUT_INJECT.md §5. The MicroBlaze stages a frame
    # into DUTEGR.TX_DATA and COMMITs it; dut_egress_0's framer (rmii_clk_i
    # domain, the same 50 MHz net as the bridge) streams it -- padded and with
    # its FCS appended unless TX_CTRL.RAW -- into port B's ingress buffer.
    #
    # Unlike the egress above, this port MAY wait on mgmt_s_tready: it is a
    # SOURCE, and a source waiting holds up only its own frame, never the
    # bridge's shared sequencer (which serves only COMPLETED ingress frames).
    # dut_egress never starts a frame before every byte of it is committed, so
    # the ingress buffer never holds half a frame on its account.
    #
    # No decoupler entry, for the same reason as the egress: static-to-static.
    # While the RP is decoupled it is held in reset, and a frame flooded at it
    # is harmless; software stops injecting at the swap FSM's GATE and writes
    # DUTEGR.TX_CTRL.FLUSH after DONE (handover §4 rule 5).
    connect_bd_net [get_bd_pins $dut_egress_0/inj_m_tdata]  [get_bd_pins $eth_ss_0/mgmt_s_tdata]
    connect_bd_net [get_bd_pins $dut_egress_0/inj_m_tvalid] [get_bd_pins $eth_ss_0/mgmt_s_tvalid]
    connect_bd_net [get_bd_pins $dut_egress_0/inj_m_tlast]  [get_bd_pins $eth_ss_0/mgmt_s_tlast]
    connect_bd_net [get_bd_pins $eth_ss_0/mgmt_s_tready]    [get_bd_pins $dut_egress_0/inj_m_tready]
    # uplink_s_tready and *_m_{tdata,tvalid,tlast} of the uplink are cell
    # OUTPUTS -> dangling is legal (validate_bd_design only auto-ties dangling
    # INPUTS).

    # -- GENCHK AXI-Lite clock crossing -----------------------------------
    # gen_checker FUSES its control and datapath clocks: inside the subsystem it
    # is instantiated .s_axi_aclk(refclk_i), which is why there is no
    # s_axi_genchk_aclk port at all. Its AXI-Lite surface is therefore in the
    # 50 MHz domain while axi_interconnect_0 runs at 100 MHz. Cross it with an
    # EXPLICIT converter rather than relying on the interconnect's implicit
    # per-MI conversion -- this repo has been bitten repeatedly by implicit
    # vendor-IP behaviour, and an explicit cell is reviewable and deterministic.
    set axi_cc_genchk [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_clock_converter:2.1 axi_cc_genchk]
    connect_bd_net $shell_clk        [get_bd_pins $axi_cc_genchk/s_axi_aclk]
    connect_bd_net $shell_aresetn    [get_bd_pins $axi_cc_genchk/s_axi_aresetn]
    connect_bd_net $rmii_ref_clk_sig [get_bd_pins $axi_cc_genchk/m_axi_aclk]
    connect_bd_net $rmii_aresetn     [get_bd_pins $axi_cc_genchk/m_axi_aresetn]
    connect_bd_intf_net [get_bd_intf_pins $axi_cc_genchk/M_AXI] [get_bd_intf_pins $eth_ss_0/s_axi_genchk]
    # rp_jtag_tdo is a real input already wired above (through the decoupler
    # to jtag_bb_0), not part of this deferred group.

    ###################################################################
    # SECTION 6 — AXI-LITE INTERCONNECT + ADDRESS MAP (shell-regmap.md v0.1)
    ###################################################################

    set axi_interconnect_0 [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_interconnect:2.1 axi_interconnect_0]
    set_property CONFIG.NUM_MI {21} $axi_interconnect_0 ;# was 19; +2 = M19 USRACC, M20 WDOG (M16 VPHY, M17 GENCHK via axi_cc_genchk, M18 DUTEGR)
    connect_bd_net $shell_clk [get_bd_pins $axi_interconnect_0/ACLK]
    connect_bd_net $shell_bus_arstn [get_bd_pins $axi_interconnect_0/ARESETN]
    connect_bd_net $shell_clk [get_bd_pins $axi_interconnect_0/S00_ACLK]
    connect_bd_net $shell_aresetn [get_bd_pins $axi_interconnect_0/S00_ARESETN]
    # The CPU's peripheral master onto S00 (cpu_$SHELL_CPU.tcl `axi`; the MBV
    # variant also builds its cache -> DDR4 SmartConnect here).
    set cpu_stage axi
    source $cpu_tcl

    # M00..M13 <-> {CLKRST, DFXCTL, HWICAP, USD, TELEM, JTAGBB, DBGBR,
    # UARTBR, GPIO, TIMER, UARTLITE, INTC, EMC, MMCM_DUT_DRP}. Each master
    # port also needs its own ACLK/ARESETN pair per axi_interconnect
    # convention. M13/MMCM_DUT_DRP added post-first-run: clk_wiz_dut's
    # dynamic-reconfig interface is AXI4-Lite-only in 2024.1 (see SECTION 1
    # NOTE) so firmware drives the DUT-clock MMCM's DRP registers directly
    # over this slave rather than through dut_clkrst_0's placeholder DRP pins.
    set mi_map [list \
        0  $dut_clkrst_0       s_axi \
        1  $dfx_ctl_0          s_axi \
        2  $axi_hwicap_0       S_AXI_LITE \
        3  $usd_spi_0          s_axi \
        4  $telem_0            s_axi \
        5  $jtag_bb_0          s_axi \
        6  $debug_bridge_0     S_AXI \
        7  $uart_bridge_0      s_axi \
        8  $board_gpio_0       s_axi \
        9  $axi_timer_0        S_AXI \
        10 $axi_uartlite_0     S_AXI \
        11 $axi_intc_0         S_AXI \
        12 $axi_emc_0          S_AXI_MEM \
        13 $clk_wiz_dut        s_axi_lite \
        14 $clcd_0             s_axi \
        15 $clcd_kvm_0         s_axi \
        16 $eth_ss_0           s_axi_vphy \
        17 $axi_cc_genchk      S_AXI \
        18 $dut_egress_0       s_axi \
        19 $usr_access_rd_0    s_axi \
        20 $axi_timebase_wdt_0 S_AXI \
    ]
    foreach {idx cell intf} $mi_map {
        # CONFIRMED LIVE: axi_interconnect:2.1's per-master clock/reset pins
        # are `M<NN>_ACLK`/`M<NN>_ARESETN` — NOT `M<NN>_AXI_ACLK`/
        # `M<NN>_AXI_ARESETN` (the bus-interface pin itself IS `M<NN>_AXI`;
        # only the scalar clk/rst pins drop the `_AXI` infix).
        set mNN [format "M%02d" $idx]
        set mi  [format "M%02d_AXI" $idx]
        connect_bd_net $shell_clk [get_bd_pins $axi_interconnect_0/${mNN}_ACLK]
        connect_bd_net $shell_aresetn [get_bd_pins $axi_interconnect_0/${mNN}_ARESETN]
        connect_bd_intf_net [get_bd_intf_pins $axi_interconnect_0/$mi] [get_bd_intf_pins $cell/$intf]
    }
    # CONFIRMED LIVE (2024.1): axi_hwicap:3.0's AXI-Lite pin is `S_AXI_LITE`
    # and axi_emc:3.0's memory aperture is `S_AXI_MEM` — as originally
    # guessed, no drift found. (axi_quad_spi:3.2's `AXI_LITE` was too, before
    # D13 removed that cell.) debug_bridge:3.0's AXI-Lite pin is actually `S_AXI` with
    # address-block segment `Reg0` (NOT `S_AXI_REG`/`Reg` as originally
    # guessed). For the six packaged custom
    # modules ($dut_clkrst_0 etc.) the real inferred bus-interface name is
    # lowercase `s_axi` (NOT `S_AXI` — see SECTION 3's header for the full
    # story: `-type module` doesn't even accept these as raw SV cells, they
    # are packaged IP-XACT components now, and packaging's auto-inference
    # names the interface `s_axi`).

    # -- Address map (64 KiB pages). THESE LINES ARE THE MAP: tools/gen_regmap.py
    #    reads each -offset, -range and `;# NAME` off them and emits
    #    firmware/common/platform_regs.h, tests/common/regmap.py, the pyverify
    #    client and docs/contracts/shell-regmap.md. Nothing downstream restates a
    #    base; move a line and all four follow.
    #
    #    The page ALLOCATION -- who owns each 64 KiB page of
    #    0x44A0_0000..0x44B2_0000, including pages held by blocks that are
    #    designed but in no BD yet -- lives in that generator's RESERVATIONS, and
    #    it refuses to render two owners on one page. That check is why
    #    0x44AE_0000 can no longer be claimed by both the gated TOUCH block below
    #    and the staged AXI-JTAG carry, as it was until 2026-09-11.
    assign_bd_address -offset 0x44A00000 -range 64K [get_bd_addr_segs {dut_clkrst_0/s_axi/reg0}]     ;# CLKRST
    assign_bd_address -offset 0x44A10000 -range 64K [get_bd_addr_segs {dfx_ctl_0/s_axi/reg0}]        ;# DFXCTL
    assign_bd_address -offset 0x44A20000 -range 64K [get_bd_addr_segs {axi_hwicap_0/S_AXI_LITE/Reg}] ;# HWICAP
    assign_bd_address -offset 0x44A40000 -range 64K [get_bd_addr_segs {usd_spi_0/s_axi/reg0}]        ;# USD
    assign_bd_address -offset 0x44A50000 -range 64K [get_bd_addr_segs {telem_0/s_axi/reg0}]           ;# TELEM
    # VPHY / GENCHK — the virtual-PHY subsystem's two AXI-Lite surfaces.
    # -range 4K, NOT 64K: MEASURED via ip_packaged/pkg_smoke_eth.tcl, both
    # reg0 address blocks come out range=0x1000. The subsystem's AXI ports are a
    # hardcoded [11:0] with no width parameter, so the block range cannot scale
    # the way the eight shell CSR blocks' does (they are widened to 32 in
    # SECTION 3). -range 64K would exceed the declared block. 4 KiB is
    # functionally sufficient: both regmaps live below offset 0x100, and the
    # upper 60 KiB of each reserved page simply DECERRs instead of aliasing.
    assign_bd_address -offset 0x44A30000 -range 4K  [get_bd_addr_segs {eth_mac_test_subsystem_0/s_axi_vphy/reg0}]   ;# VPHY
    assign_bd_address -offset 0x44A60000 -range 4K  [get_bd_addr_segs {eth_mac_test_subsystem_0/s_axi_genchk/reg0}] ;# GENCHK (through axi_cc_genchk)
    assign_bd_address -offset 0x44A70000 -range 64K [get_bd_addr_segs {jtag_bb_0/s_axi/reg0}]         ;# JTAGBB
    assign_bd_address -offset 0x44A80000 -range 64K [get_bd_addr_segs {debug_bridge_0/S_AXI/Reg0}] ;# DBGBR
    assign_bd_address -offset 0x44A90000 -range 64K [get_bd_addr_segs {uart_bridge_0/s_axi/reg0}]     ;# UARTBR
    assign_bd_address -offset 0x44AA0000 -range 64K [get_bd_addr_segs {board_gpio_0/s_axi/reg0}]      ;# GPIO
    assign_bd_address -offset 0x44AB0000 -range 64K [get_bd_addr_segs {clk_wiz_dut/s_axi_lite/Reg}] ;# MMCM_DUT_DRP (new post-first-run slave, SECTION 1/6 NOTE; segment name confirmed live)
    assign_bd_address -offset 0x44AC0000 -range 64K [get_bd_addr_segs {clcd_0/s_axi/reg0}] ;# CLCD
    assign_bd_address -offset 0x44AD0000 -range 64K [get_bd_addr_segs {clcd_kvm_0/s_axi/reg0}] ;# CLCD-KVM (Wave 4)
    # DUTEGR — the DUT's Ethernet return path. 0x44B2_0000, NOT the next free
    # page after CLCD-KVM: 0x44AE is the gated TOUCH block's and 0x44AF/0x44B0/
    # 0x44B1 are the staged AXI-JTAG carry's reservations, so the contract
    # region 0x44A0_0000..0x44B2_0000 was FULL. tools/gen_regmap.py's own
    # message for that state is "a new block must extend REGION_HI, and say
    # so" -- this block is the first to do it, and the generator's WINDOW_HI/
    # REGION_HI move to 0x44B3_0000 with a note that names this line.
    assign_bd_address -offset 0x44B20000 -range 64K [get_bd_addr_segs {dut_egress_0/s_axi/reg0}] ;# DUTEGR
    # USRACC / WDOG — the two blocks this wave adds. 0x44B3 and 0x44B4 are the
    # next two free pages: everything below 0x44B2 is a BD block or one of the
    # staged AXI-JTAG carry's reservations, so tools/gen_regmap.py's REGION_HI
    # moves 0x44B3_0000 -> 0x44B5_0000 and its comment names these two lines.
    # Extending the region SILENTLY is how a page stops having exactly one
    # owner, which is why that generator refuses to render without it.
    #
    # USRACC is @0x44B3_0000 and NOT at VERSIONING_PLAN §3.4's proposed
    # DFXCTL.SHELL_USR_ACCESS @0x44A1_001C -- see
    # fpga/shell/ip/usr_access_rd/usr_access_rd.sv's header for why the
    # mechanism was taken and the address was not.
    assign_bd_address -offset 0x44B30000 -range 64K [get_bd_addr_segs {usr_access_rd_0/s_axi/reg0}] ;# USRACC
    assign_bd_address -offset 0x44B40000 -range 64K [get_bd_addr_segs {axi_timebase_wdt_0/S_AXI/Reg}] ;# WDOG

    # -- Phase-2 TOUCH (gated): add the AXI IIC touch master @0x44AE0000 (M18)
    #    + route CLCD_TINT to the INTC concat (In4) ONLY when built with env
    #    SHELL_TOUCH=1. A default regen never sources it, so the shipped
    #    0xCD74B6AE BD stays byte-identical. Pairs with shell_top.sv's
    #    `ifdef MPS3_SHELL_TOUCH glue (verilog_define set in build_shell.tcl) and
    #    the touch XDC (added conditionally there, NOT via the constraints glob).
    if {[info exists ::env(SHELL_TOUCH)] && $::env(SHELL_TOUCH)} {
        source [file join $::SHELL_BD_DIR touch_iic_add.tcl]
    }

    # -- CPU local memory (LMB) address windows. The CPU's own memory map --
    #    cpu_mb.tcl: both LMBs at 0x0, 1 MiB (the bare-metal firmware RAM);
    #    cpu_mbv.tcl: both LMBs at 0x0, 128 KiB (stage0 + status + mailbox) and
    #    the 1 GiB DDR4 window at 0x8000_0000. Outside the 0x44Ax contract window
    #    on purpose: tools/gen_regmap.py renders the MBV view from cpu_mbv.tcl. --
    set cpu_stage addr
    source $cpu_tcl

    # -- Shell-internal (non-contract) housekeeping peripherals: standard
    #    MicroBlaze BSP default addresses, chosen clear of the 0x44A0_0000+
    #    regmap window and of axi_emc's memory aperture below. --
    assign_bd_address -offset 0x41C00000 -range 64K [get_bd_addr_segs {axi_timer_0/S_AXI/Reg}]
    assign_bd_address -offset 0x40600000 -range 64K [get_bd_addr_segs {axi_uartlite_0/S_AXI/Reg}]
    assign_bd_address -offset 0x41200000 -range 64K [get_bd_addr_segs {axi_intc_0/S_AXI/Reg}]
    assign_bd_address -offset 0xC0000000 -range 16M [get_bd_addr_segs {axi_emc_0/S_AXI_MEM/*}] -quiet
    # TODO(integrator): axi_emc's memory-mapped address segment name varies
    # by CONFIG (single vs multi-bank EMC); the `-quiet` above avoids a hard
    # script failure if the segment name differs — confirm the real segment
    # name via the BD Address Editor and drop `-quiet` once fixed for real.

    # -- CPU seam, last stage: the cross-block wiring a CPU needs once every
    #    shared block exists (cpu_$SHELL_CPU.tcl `finish`; empty for mb). --
    set cpu_stage finish
    source $cpu_tcl

    ###################################################################
    # VALIDATE — left for the caller (build_shell.tcl) to invoke after
    # sourcing this file, once the custom RTL sources are on the project
    # (see that script). Not called here so this proc can also be sourced
    # for inspection without a live Vivado project.
    ###################################################################
    # validate_bd_design
    # save_bd_design

    current_bd_instance $oldCurInst
}

###-----------------------------------------------------------------------------
### UNCERTAINTIES FOR THE FIRST REAL VIVADO 2024.1 RUN (ranked, highest-risk
### first — see also inline TODO(integrator) comments above and
### fpga/shell/README.md's matching section):
###
### 1. RESOLVED (2024.1, confirmed live) — was framed as an AXI4-Lite
###    bus-interface auto-inference risk for the six `-type module` custom
###    CSR cells; the real failure was more basic: `-type module -reference`
###    hard-refuses a SystemVerilog top file at all ([filemgmt 56-195]).
###    Fix landed: each block is now packaged as a real IP-XACT component
###    (`ipx::package_project -import_files`, see
###    fpga/shell/ip_packaged/package_csr_ip.tcl) and instantiated `-type ip
###    -vlnv soclabs.org:user:<name>:1.0`. `-import_files` alone DOES
###    auto-infer the AXI4-Lite interface from the `s_axi_*` convention (no
###    extra ipx:: calls needed) — but the inferred names are lowercase
###    `s_axi` (bus interface) / `reg0` (address block), not the originally
###    guessed `S_AXI`/`Reg`; every reference to these six cells' AXI-Lite
###    interface in SECTION 3/6 above uses the real lowercase names.
###    Caller contract: validate_bd.tcl (and, not yet updated, build_shell.tcl
###    — out of this wave's scope) must package + `set_property
###    ip_repo_paths` + `update_ip_catalog` BEFORE sourcing this file.
### 2. RESOLVED (2024.1, confirmed live) — dfx_decoupler_0's real per-signal
###    boundary (R1) is now authored DIRECTLY in Tcl via CONFIG.ALL_PARAMS
###    (SECTION 3), NOT the PG294 GUI. The exact schema (INTF/SIGNALS/MODE/
###    MANAGEMENT/DECOUPLED_VALUE, the rp_<intf>_DATA/s_<intf>_DATA pin
###    naming, and the data_rtl-over-AXIS decision) was discovered in a
###    scratch project and is recorded in full at the decoupler create in
###    SECTION 3. All 20 RP->static output nets route THROUGH the clamp; the
###    5 deferred RMII/MDIO TX signals are pre-authored with dangling s_*
###    outputs (wire to the virtual-PHY when SECTION 5 lands). Discrete
###    data_rtl groups validate with ZERO warnings (AXIS member-vs-discrete
###    connection throws [BD 41-1306], so AXIS was rejected for the console).
### 3. Vendor-IP CONFIG property names/enum values for axi_hwicap (ICAP
###    target mode), axi_emc (SRAM vs NOR memory-type enum + address-segment
###    naming), and axi_quad_spi (SPI mode / memory-type enum) are
###    reproduced from PG134/PG105/PG153-era documentation; minor-version
###    drift across 2024.1 vs older Vivado releases is likely — confirm each
###    against the live Customize IP dialog.
### 4. lmb_v10 / lmb_bram_if_cntlr / mdm pin names in the local-memory
###    foreach loop use `-quiet` specifically because exact pin names
###    (LMB_Clk vs Clk, SYS_Rst vs LMB_Rst) drift by IP minor version —
###    confirm every connection actually landed via the BD canvas.
### 5. EMC_INTF / SPI_0 external interface VLNVs (xilinx.com:interface:
###    emc_rtl:1.0 / spi_rtl:1.0) — confirm the exact bus definition name
###    Vivado 2024.1 generates for axi_emc/axi_quad_spi's external ports;
###    interface VLNVs for "_rtl" wrapper types have shifted across
###    releases.
### 6. RM Pblock sizing (D7) and DUT-clock default frequency (D12) remain
###    open per docs/ARCHITECTURE_SPEC.md §15 — this file's clk_wiz_dut
###    default (50 MHz) is a placeholder, not a measured value.
###-----------------------------------------------------------------------------
