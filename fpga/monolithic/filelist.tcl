###=============================================================================
### STALE AGAINST THE CURRENT SoC - READ THIS BEFORE USING THIS FILE (2026-09-23)
###
### This list was transcribed from nanosoc_m0_soc/pynq/filelist.tcl around
### 2026-07-04 and has not followed the SoC since. Against today's generated
### build_soc/rtl/nanosoc.sv it CANNOT elaborate: it never reads qspi_flash_ahb,
### nanosoc_swj_dap_ss or nanosoc_dbg_ahb_bridge, all of which the SoC now
### instantiates. (Its two stale local_overrides were retired below; that fix
### alone is not enough.)
###
### The toolkit flow no longer reads this file. fpga/monolithic/gen_flist.tcl
### derives the flist from pynq/filelist.tcl directly - see its header for the
### measurements. This file is kept only because build_monolithic.tcl still
### sources it, and that legacy direct flow is therefore also broken against
### the current SoC until it is pointed at pynq's list too.
###=============================================================================
###-----------------------------------------------------------------------------
### fpga/monolithic/filelist.tcl -- RTL source list for the monolithic
### nanoSoC-on-MPS3 baseline (Vivado 2024.1, WP0.4).
###
### A joint work commissioned on behalf of SoC Labs, under Arm Academic Access
### license. Companion to build_monolithic.tcl and nanosoc_mps3_top.sv.
###-----------------------------------------------------------------------------
### SUPERSEDES the earlier version of this file, which drove the *legacy*
### arm_mps3 Block-Design flow (ip_repo_paths + update_ip_catalog + a VLNV
### pre-check for `soclabs.org:user:nanosoc_chip:1.0` and friends). That flow
### targeted `nanosoc_chip` from nanosoc-multicore-system's orphaned
### pre-generator arm_mps3 target, whose RTL was confirmed absent from that
### source tree entirely (README.md Sec 2.4, git history included). This
### version instead builds the real, current, single-core nanoSoC from the
### sibling repo `nanosoc_m0_soc` -- module `nanosoc`
### (build_soc/rtl/nanosoc.sv) -- as a plain flat-RTL Vivado project source
### list. There is no Block Design and no packaged/catalog IP at all in this
### flow: nanosoc_mps3_top.sv instantiates `nanosoc` directly in RTL, matching
### the pattern already HW-validated on a PYNQ-Z2 build (nanosoc_m0_soc's
### pynq/vivado_ip/nanosoc_vivado_wrapper.v, doc/reports/pynq_z2_build.md,
### 2026-07-03).
###
### WHY THIS IS A HAND-TRANSCRIPTION, NOT A LITERAL `-f nanosoc_FPGA.flist`:
### nanosoc_m0_soc/nanosoc_arch_tech/rtl/flist/nanosoc_FPGA.flist (the
### upstream flist this file's RTL selection is cross-checked against, see
### docs/nanosoc_m0_soc/RTL_ANATOMY.md Sec 5) is written in a VCS/Synopsys-
### style filelist dialect: `$(MAKE_VAR)` shell/make-style substitution,
### `+libext+.v+.vlib`, and `-f` chains resolved by a simulator's own command-
### file parser. Vivado's `read_verilog -f` does not understand any of that
### syntax. nanosoc_m0_soc's OWN proven, already-synthesized reference
### (pynq/filelist.tcl) does not attempt it either -- it hand-transcribes the
### exact same RTL set (nanosoc_FPGA.flist -> nanosoc_ip.flist ->
### corstone101_ip.flist / slcorem0.flist / socdebug.flist, per
### RTL_ANATOMY.md Sec 5.2) into plain Tcl `read_verilog` calls with Tcl-side
### env-var resolution. This file mirrors that exact, already-Vivado-verified
### transcription (docs/nanosoc_m0_soc/FPGA_FLOW.md: "The IP-packaging step
### and the generated-RTL filelist are almost entirely board-agnostic and
### should carry over close to verbatim"), adapted only to: (a) this repo's
### own env-var names, (b) reading straight into a plain project's
### sources_1 fileset instead of an ipx::package_project fileset (both are
### legitimate `read_verilog` targets -- no different semantics), and (c) an
### MPS3-specific NANOSOC_BOOTROM_DIR (the PYNQ flow's firmware/bootrom was
### built for a 25 MHz sys_clk; MPS3 needs its own 50 MHz-patched rebuild,
### see README.md).
###
### Env vars (all optional except NANOSOC_BOOTROM_DIR; sane defaults shown):
###   NANOSOC_M0_SOC_SRC  - nanosoc_m0_soc repo root (READ-ONLY; this file
###                         only ever reads from it)
###                         default $SOCLABS_NANOSOC_SOC_DIR (tools.env
###                         spelling), else the pinned submodule
###                         <repo>/fpga/deps/nanosoc_m0_soc
###   ARM_IP_LIBRARY_PATH - Arm Academic Access IP library (READ-ONLY;
###                         Cortex-M0, Corstone-101 CMSDK, PL022 SSP)
###                         REQUIRED, no default (a site mount)
###   NANOSOC_BOOTROM_DIR - REQUIRED, no default. Directory containing the
###                         generated nanosoc_region_bootrom.v + bootrom.sv
###                         from an MPS3-clock-patched firmware/bootrom
###                         build (README.md "Firmware" section has the
###                         exact commands). This script fails loudly if
###                         either file is missing, rather than letting a
###                         confusing "module not found" surface deep inside
###                         elaboration.
###-----------------------------------------------------------------------------

proc _env_default {name default} {
    if { [info exists ::env($name)] && $::env($name) ne "" } {
        return $::env($name)
    }
    return $default
}

set NANOSOC_M0_SOC_SRC  [_env_default NANOSOC_M0_SOC_SRC \
    [_env_default SOCLABS_NANOSOC_SOC_DIR \
        [file normalize [file join [file dirname [file normalize [info script]]] .. deps nanosoc_m0_soc]]]]
set ARM_IP_LIBRARY_PATH [_env_default ARM_IP_LIBRARY_PATH ""]
if { $ARM_IP_LIBRARY_PATH eq "" } {
    puts "ERROR: ARM_IP_LIBRARY_PATH is not set (the read-only Arm IP library; no default)."
    error "filelist.tcl: set ARM_IP_LIBRARY_PATH in the environment (see tools.env.example)"
}

if { ![file isdirectory $NANOSOC_M0_SOC_SRC] } {
    puts "ERROR: NANOSOC_M0_SOC_SRC does not exist: $NANOSOC_M0_SOC_SRC"
    error "filelist.tcl: NANOSOC_M0_SOC_SRC not found"
}
if { ![file isdirectory $ARM_IP_LIBRARY_PATH] } {
    puts "ERROR: ARM_IP_LIBRARY_PATH does not exist: $ARM_IP_LIBRARY_PATH"
    error "filelist.tcl: ARM_IP_LIBRARY_PATH not found"
}

set NANOSOC_ARCH_TECH_DIR ${NANOSOC_M0_SOC_SRC}/nanosoc_arch_tech
set NANOSOC_GEN_DIR       ${NANOSOC_ARCH_TECH_DIR}/nanosoc_gen

if { ![info exists ::env(NANOSOC_BOOTROM_DIR)] || $::env(NANOSOC_BOOTROM_DIR) eq "" } {
    puts "ERROR: NANOSOC_BOOTROM_DIR is not set."
    puts "       This must point at a directory containing the generated"
    puts "       nanosoc_region_bootrom.v + bootrom.sv from an MPS3-clock-"
    puts "       patched firmware/bootrom build -- see README.md 'Firmware'"
    puts "       for the exact commands. There is no default: reusing the"
    puts "       PYNQ-Z2 build's bootrom would bake in the WRONG UART baud"
    puts "       divisor (that build's bootrom was compiled against a 25 MHz"
    puts "       firmware clock constant, not MPS3's 50 MHz OSCCLK\[1\])."
    error "filelist.tcl: NANOSOC_BOOTROM_DIR not set"
}
set NANOSOC_BOOTROM_DIR $::env(NANOSOC_BOOTROM_DIR)
foreach f {nanosoc_region_bootrom.v bootrom.sv} {
    if { ![file exists ${NANOSOC_BOOTROM_DIR}/$f] } {
        puts "ERROR: $f not found under NANOSOC_BOOTROM_DIR: $NANOSOC_BOOTROM_DIR"
        error "filelist.tcl: stage-0 bootrom not found -- build it first (README.md)"
    }
}

puts "==========================================================="
puts " fpga/monolithic filelist.tcl"
puts "   NANOSOC_M0_SOC_SRC     = $NANOSOC_M0_SOC_SRC"
puts "   NANOSOC_ARCH_TECH_DIR  = $NANOSOC_ARCH_TECH_DIR"
puts "   ARM_IP_LIBRARY_PATH    = $ARM_IP_LIBRARY_PATH"
puts "   NANOSOC_BOOTROM_DIR    = $NANOSOC_BOOTROM_DIR"
puts "==========================================================="

###-----------------------------------------------------------------------------
### Verilog define: RAM_PRELOAD -- selects the ROM ($readmemh-preloadable
### sl_ahb_rom/sl_fpga_rom_word) variant of nanosoc_region_imem instead of
### plain SRAM, so IMEM is initialised with the firmware hex file at
### synthesis time. Without this define IMEM is empty and the CM0 hard-
### faults immediately on reset. (Mirrors pynq/filelist.tcl verbatim.)
###-----------------------------------------------------------------------------
set_property verilog_define {RAM_PRELOAD} [current_fileset]

###-----------------------------------------------------------------------------
### Include directories (needed by a handful of Arm IP files that use
### `include for shared headers).
###-----------------------------------------------------------------------------
set_property include_dirs [list \
    ${NANOSOC_M0_SOC_SRC}/build_soc/rtl \
    ${ARM_IP_LIBRARY_PATH}/latest/Cortex-M0/logical/cortexm0_dap/verilog \
    ${ARM_IP_LIBRARY_PATH}/latest/Cortex-M0/logical/ualdis/verilog \
    ${ARM_IP_LIBRARY_PATH}/latest/Corstone-101/logical/cmsdk_apb_dualtimers/verilog \
    ${ARM_IP_LIBRARY_PATH}/latest/Corstone-101/logical/cmsdk_apb_watchdog/verilog \
    ${ARM_IP_LIBRARY_PATH}/latest/Corstone-101/logical/models/memories \
    ${ARM_IP_LIBRARY_PATH}/PL022/PL022-BU-00000-r1p4-00rel0/ssp_pl022/verilog/rtl_source \
] [current_fileset]

###=============================================================================
### ARM CMSDK IP (Corstone-101: peripherals, muxes, FPGA-friendly memories)
###=============================================================================
read_verilog [list \
    ${ARM_IP_LIBRARY_PATH}/latest/Corstone-101/logical/cmsdk_ahb_to_sram/verilog/cmsdk_ahb_to_sram.v \
    ${ARM_IP_LIBRARY_PATH}/latest/Corstone-101/logical/models/memories/cmsdk_fpga_sram.v \
    ${ARM_IP_LIBRARY_PATH}/latest/Corstone-101/logical/models/memories/cmsdk_fpga_rom.v \
    ${ARM_IP_LIBRARY_PATH}/latest/Corstone-101/logical/cmsdk_ahb_to_apb/verilog/cmsdk_ahb_to_apb.v \
    ${ARM_IP_LIBRARY_PATH}/latest/Corstone-101/logical/cmsdk_apb_timer/verilog/cmsdk_apb_timer.v \
    ${ARM_IP_LIBRARY_PATH}/latest/Corstone-101/logical/cmsdk_apb_dualtimers/verilog/cmsdk_apb_dualtimers_frc.v \
    ${ARM_IP_LIBRARY_PATH}/latest/Corstone-101/logical/cmsdk_apb_dualtimers/verilog/cmsdk_apb_dualtimers.v \
    ${ARM_IP_LIBRARY_PATH}/latest/Corstone-101/logical/cmsdk_apb_uart/verilog/cmsdk_apb_uart.v \
    ${ARM_IP_LIBRARY_PATH}/latest/Corstone-101/logical/cmsdk_apb_watchdog/verilog/cmsdk_apb_watchdog_frc.v \
    ${ARM_IP_LIBRARY_PATH}/latest/Corstone-101/logical/cmsdk_apb_watchdog/verilog/cmsdk_apb_watchdog.v \
    ${ARM_IP_LIBRARY_PATH}/latest/Corstone-101/logical/cmsdk_apb_slave_mux/verilog/cmsdk_apb_slave_mux.v \
    ${ARM_IP_LIBRARY_PATH}/latest/Corstone-101/logical/cmsdk_apb_subsystem/verilog/cmsdk_apb_test_slave.v \
    ${ARM_IP_LIBRARY_PATH}/latest/Corstone-101/logical/cmsdk_apb_subsystem/verilog/cmsdk_irq_sync.v \
    ${ARM_IP_LIBRARY_PATH}/latest/Corstone-101/logical/cmsdk_ahb_slave_mux/verilog/cmsdk_ahb_slave_mux.v \
    ${ARM_IP_LIBRARY_PATH}/latest/Corstone-101/logical/cmsdk_ahb_default_slave/verilog/cmsdk_ahb_default_slave.v \
    ${ARM_IP_LIBRARY_PATH}/latest/Corstone-101/logical/cmsdk_ahb_gpio/verilog/cmsdk_ahb_gpio.v \
    ${ARM_IP_LIBRARY_PATH}/latest/Corstone-101/logical/cmsdk_ahb_gpio/verilog/cmsdk_ahb_to_iop.v \
    ${ARM_IP_LIBRARY_PATH}/latest/Corstone-101/logical/cmsdk_iop_gpio/verilog/cmsdk_iop_gpio.v \
    ${ARM_IP_LIBRARY_PATH}/latest/Corstone-101/logical/models/clkgate/cmsdk_clock_gate.v \
]

###=============================================================================
### ARM Cortex-M0 core + DAP + integration (per-core SWD, EXTERNAL_DAP=0)
###=============================================================================
read_verilog [glob \
    ${ARM_IP_LIBRARY_PATH}/latest/Cortex-M0/logical/cortexm0/verilog/*.v \
    ${ARM_IP_LIBRARY_PATH}/latest/Cortex-M0/logical/cortexm0_dap/verilog/*.v \
    ${ARM_IP_LIBRARY_PATH}/latest/Cortex-M0/logical/cortexm0_integration/verilog/*.v \
    ${ARM_IP_LIBRARY_PATH}/latest/Cortex-M0/logical/models/cells/*.v \
    ${ARM_IP_LIBRARY_PATH}/latest/Cortex-M0/logical/models/wrappers/*.v \
]

###=============================================================================
### SoC Labs Cortex-M0 tech wrapper (slcorem0)
###=============================================================================
read_verilog [list \
    ${NANOSOC_ARCH_TECH_DIR}/rtl/slcorem0_tech/src/verilog/slcorem0.v \
    ${NANOSOC_ARCH_TECH_DIR}/rtl/slcorem0_tech/src/verilog/slcorem0_prmu.v \
    ${NANOSOC_ARCH_TECH_DIR}/rtl/slcorem0_tech/src/verilog/slcorem0_stclkctrl.v \
    ${NANOSOC_ARCH_TECH_DIR}/rtl/slcorem0_tech/src/verilog/slcorem0_rstctrl.v \
    ${NANOSOC_ARCH_TECH_DIR}/rtl/slcorem0_tech/src/verilog/slcorem0_integration.v \
]

###=============================================================================
### SoCDebug (ADP controller, FT1248 controller, USRT)
###=============================================================================
read_verilog [list \
    ${NANOSOC_ARCH_TECH_DIR}/rtl/socdebug_tech/controller/verilog/socdebug_adp_control.v \
    ${NANOSOC_ARCH_TECH_DIR}/rtl/socdebug_tech/controller/verilog/socdebug_ahb.v \
    ${NANOSOC_ARCH_TECH_DIR}/rtl/socdebug_tech/controller/verilog/socdebug_ft1248_control.v \
    ${NANOSOC_ARCH_TECH_DIR}/rtl/socdebug_tech/controller/verilog/socdebug_usrt_control.v \
]

###=============================================================================
### HOSTIO4 controller (EXTIO 8x4 external host interface)
###=============================================================================
read_verilog [list \
    ${NANOSOC_ARCH_TECH_DIR}/rtl/hostio4/soc_rtl/hostio4_controller.v \
    ${NANOSOC_ARCH_TECH_DIR}/rtl/hostio4/soc_rtl/hostio4_controller_fsm.v \
    ${NANOSOC_ARCH_TECH_DIR}/rtl/hostio4/soc_rtl/hostio4_controller_sync.v \
    ${NANOSOC_ARCH_TECH_DIR}/rtl/hostio4/soc_rtl/hostio4_controller_axis_rxport.v \
    ${NANOSOC_ARCH_TECH_DIR}/rtl/hostio4/soc_rtl/hostio4_controller_axis_txport.v \
]

###=============================================================================
### NanoSoC arch_tech subsystems + regions + control
###=============================================================================
# nanosoc_ss_debug / nanosoc_ss_systemctrl: LOCAL OVERRIDES from nanosoc_m0_soc
# itself (bare AHB port names -- see that repo's src/rtl/local_overrides/
# headers for the rationale). Read-only reference, never copied here.
# THE TWO local_overrides THAT USED TO BE READ HERE ARE RETIRED (2026-09-23).
#
# nanosoc_m0_soc/src/rtl/local_overrides/nanosoc_ss_{debug,systemctrl}.v exist
# to rename the arch_tech modules' DEBUG_H*/prefixed AHB ports to BARE H*
# names, because nanosoc_gen's toplevel backend used to emit bare names. Each
# override's own header names the upstream fix: "teach the generator a
# port-prefix override". THAT FIX LANDED. build_soc/rtl/nanosoc.sv was
# regenerated 2026-08-09 and now connects the prefixed names - so the
# workaround became the thing that breaks elaboration, the exact inverse of
# why it was written.
#
# MEASURED, not inferred. Ports nanosoc.sv connects vs ports each file declares:
#
#     module                 source     connected declared UNSATISFIED
#     nanosoc_ss_debug       override       38       38        11
#     nanosoc_ss_debug       arch_tech      38       38         0
#     nanosoc_ss_systemctrl  override       63       69        13
#     nanosoc_ss_systemctrl  arch_tech      63       69         0
#
# The 11 is exactly the eleven [Synth 8-11365] errors real Vivado reported on
# the first toolkit synth of this target, which is what makes the 13 trusted:
# systemctrl never got elaborated that run, and would have been the NEXT
# failure. Both now read the canonical arch_tech RTL.
#
# The overrides themselves are left alone - nanosoc_m0_soc is read-only by its
# own README's rule, and other flows (pynq/filelist.tcl) may still pin an older
# generated nanosoc.sv that needs them. Retiring them there is that repo's call.
read_verilog [list \
    ${NANOSOC_ARCH_TECH_DIR}/rtl/src/subsystems/debug/nanosoc_ss_debug.v \
    ${NANOSOC_ARCH_TECH_DIR}/rtl/src/subsystems/systemctrl/nanosoc_ss_systemctrl.v \
    ${NANOSOC_ARCH_TECH_DIR}/rtl/src/subsystems/hostio4/nanosoc_ss_hostio4.v \
    ${NANOSOC_ARCH_TECH_DIR}/rtl/src/regions/imem/nanosoc_region_imem.v \
    ${NANOSOC_ARCH_TECH_DIR}/rtl/src/regions/dmem/nanosoc_region_dmem.v \
    ${NANOSOC_ARCH_TECH_DIR}/rtl/src/regions/sram/nanosoc_region_sram.v \
    ${NANOSOC_ARCH_TECH_DIR}/rtl/src/regions/dmac_ctrl/nanosoc_region_dmac_ctrl.v \
    ${NANOSOC_ARCH_TECH_DIR}/rtl/src/regions/soc_peripheral/nanosoc_region_soc_peripheral.v \
    ${NANOSOC_ARCH_TECH_DIR}/rtl/src/regions/soc_peripheral/nanosoc_sysctrl.v \
    ${NANOSOC_ARCH_TECH_DIR}/rtl/src/regions/soc_peripheral/nanosoc_soc_peripheral_apb_ss.v \
    ${NANOSOC_ARCH_TECH_DIR}/rtl/src/regions/soc_peripheral/nanosoc_soc_peripheral_decode.v \
    ${NANOSOC_ARCH_TECH_DIR}/rtl/src/regions/systable/nanosoc_coresight_systable.v \
    ${NANOSOC_ARCH_TECH_DIR}/rtl/src/regions/systable/nanosoc_region_systable.v \
    ${NANOSOC_ARCH_TECH_DIR}/rtl/src/control/verilog/nanosoc_clkctrl.v \
    ${NANOSOC_ARCH_TECH_DIR}/rtl/src/control/verilog/nanosoc_pin_mux.v \
]
read_verilog -sv [list \
    ${NANOSOC_ARCH_TECH_DIR}/rtl/src/subsystems/dma/nanosoc_ss_dma.sv \
]

###=============================================================================
### Stage-0 boot ROM -- generated by bootrom_gen.py from an MPS3-clock-
### patched firmware/bootrom build (README.md "Firmware"). REPLACES
### arch_tech's rtl/src/regions/bootrom/nanosoc_region_bootrom.v (which
### expects a hand-provided nanosoc_bootrom_cpu_0 module) -- already checked
### to exist above (fail-fast block near the top of this file).
###=============================================================================
read_verilog     ${NANOSOC_BOOTROM_DIR}/nanosoc_region_bootrom.v
read_verilog -sv ${NANOSOC_BOOTROM_DIR}/bootrom.sv

###=============================================================================
### FPGA-friendly memory wrappers (preloadable ROM, plain SRAM) + FPGA-only
### helper RTL vendored directly into nanosoc_m0_soc (src/rtl/fpga_lib/) --
### see that repo's own provenance notes for these files.
###=============================================================================
read_verilog [list \
    ${NANOSOC_M0_SOC_SRC}/src/rtl/fpga_lib/sram/sl_ahb_sram.v \
    ${NANOSOC_M0_SOC_SRC}/src/rtl/fpga_lib/rom/sl_ahb_rom.v \
    ${NANOSOC_M0_SOC_SRC}/src/rtl/fpga_lib/rom/sl_fpga_rom_word.v \
    ${NANOSOC_M0_SOC_SRC}/src/rtl/fpga_lib/exp/nanosoc_region_exp_default.v \
]

###=============================================================================
### Arm PL022 SSP (SPI master) -- Arm Academic Access IP, referenced
### read-only from ARM_IP_LIBRARY_PATH (never copied into this repo).
###=============================================================================
set PL022_RTL ${ARM_IP_LIBRARY_PATH}/PL022/PL022-BU-00000-r1p4-00rel0/ssp_pl022/verilog/rtl_source
read_verilog [list \
    ${PL022_RTL}/Ssp.v \
    ${PL022_RTL}/SspApbif.v \
    ${PL022_RTL}/SspDMA.v \
    ${PL022_RTL}/SspDataStp.v \
    ${PL022_RTL}/SspIntGen.v \
    ${PL022_RTL}/SspMTxRxCntl.v \
    ${PL022_RTL}/SspRegCore.v \
    ${PL022_RTL}/SspRevAnd.v \
    ${PL022_RTL}/SspRxFCntl.v \
    ${PL022_RTL}/SspRxFIFO.v \
    ${PL022_RTL}/SspRxRegFile.v \
    ${PL022_RTL}/SspSTxRxCntl.v \
    ${PL022_RTL}/SspScaleCntr.v \
    ${PL022_RTL}/SspSynctoPCLK.v \
    ${PL022_RTL}/SspSynctoSSPCLK.v \
    ${PL022_RTL}/SspTest.v \
    ${PL022_RTL}/SspTxFCntl.v \
    ${PL022_RTL}/SspTxFIFO.v \
    ${PL022_RTL}/SspTxLJustify.v \
    ${PL022_RTL}/SspTxRegFile.v \
]

###=============================================================================
### Generated structural glue logic helpers (passthrough, or_reduce, …)
###=============================================================================
read_verilog -sv [list \
    ${NANOSOC_GEN_DIR}/rtl/soc_glue/soc_glue_passthrough.sv \
    ${NANOSOC_GEN_DIR}/rtl/soc_glue/soc_glue_or_reduce.sv \
    ${NANOSOC_GEN_DIR}/rtl/soc_glue/soc_glue_or_combine.sv \
    ${NANOSOC_GEN_DIR}/rtl/soc_glue/soc_glue_and_gate.sv \
    ${NANOSOC_GEN_DIR}/rtl/soc_glue/soc_glue_constant.sv \
    ${NANOSOC_GEN_DIR}/rtl/soc_glue/soc_glue_mux2.sv \
    ${NANOSOC_GEN_DIR}/rtl/soc_glue/soc_glue_reset_sync.sv \
]

###=============================================================================
### Generated AHB interconnects (top-level + CPU subsystem) + discovery
###=============================================================================
read_verilog [glob \
    ${NANOSOC_M0_SOC_SRC}/build_soc/rtl/nanosoc_ahb_interconnect/nanosoc_ahb_interconnect/*.v \
]
read_verilog -sv [list \
    ${NANOSOC_M0_SOC_SRC}/build_soc/rtl/nanosoc_ahb_interconnect/nanosoc_config_pkg.sv \
    ${NANOSOC_M0_SOC_SRC}/build_soc/rtl/nanosoc_ahb_interconnect/nanosoc_interconnect.sv \
    ${NANOSOC_M0_SOC_SRC}/build_soc/rtl/nanosoc_ahb_interconnect_discovery/nanosoc_ahb_interconnect_discovery_pkg.sv \
    ${NANOSOC_M0_SOC_SRC}/build_soc/rtl/nanosoc_ahb_interconnect_discovery/nanosoc_ahb_interconnect_discovery.sv \
    ${NANOSOC_M0_SOC_SRC}/build_soc/rtl/nanosoc_ahb_interconnect_discovery/nanosoc_ahb_interconnect_discovery_apb_wrapper.sv \
]

read_verilog [glob \
    ${NANOSOC_M0_SOC_SRC}/build_soc/rtl/nanosoc_cpu_ss_ahb_interconnect/nanosoc_cpu_ss_ahb_interconnect/*.v \
]
read_verilog -sv [list \
    ${NANOSOC_M0_SOC_SRC}/build_soc/rtl/nanosoc_cpu_ss_ahb_interconnect/nanosoc_cpu_ss_config_pkg.sv \
    ${NANOSOC_M0_SOC_SRC}/build_soc/rtl/nanosoc_cpu_ss_ahb_interconnect/nanosoc_cpu_ss_interconnect.sv \
    ${NANOSOC_M0_SOC_SRC}/build_soc/rtl/nanosoc_cpu_ss_ahb_interconnect_discovery/nanosoc_cpu_ss_ahb_interconnect_discovery_pkg.sv \
    ${NANOSOC_M0_SOC_SRC}/build_soc/rtl/nanosoc_cpu_ss_ahb_interconnect_discovery/nanosoc_cpu_ss_ahb_interconnect_discovery.sv \
    ${NANOSOC_M0_SOC_SRC}/build_soc/rtl/nanosoc_cpu_ss_ahb_interconnect_discovery/nanosoc_cpu_ss_ahb_interconnect_discovery_apb_wrapper.sv \
]

###=============================================================================
### Generated build-info registers (referenced by the discovery block)
###=============================================================================
read_verilog -sv [list \
    ${NANOSOC_M0_SOC_SRC}/build_soc/rtl/nanosoc_build_info/nanosoc_build_info_pkg.sv \
    ${NANOSOC_M0_SOC_SRC}/build_soc/rtl/nanosoc_build_info/nanosoc_build_info.sv \
    ${NANOSOC_M0_SOC_SRC}/build_soc/rtl/nanosoc_build_info/nanosoc_build_info_apb_wrapper.sv \
]

###=============================================================================
### Generated SoC config package + subsystem module + core top-level.
### IMPORTANT: this reads build_soc/rtl/nanosoc.sv -- the PROJECT-regenerated
### snapshot -- not nanosoc_arch_tech/rtl/src/nanosoc/nanosoc.sv (the
### arch_tech submodule's own separately-generated copy). The two are NOT
### interchangeable: this agent found the arch_tech copy has a reversed
### exp_h* AHB port-direction convention and is missing the spi_* ports
### entirely relative to build_soc/rtl/nanosoc.sv (confirmed by direct diff
### while lint-checking nanosoc_mps3_top.sv). nanosoc_mps3_top.sv's
### instantiation matches build_soc/rtl/nanosoc.sv (same file this section
### reads, same file the proven pynq/filelist.tcl reads) -- reading the
### wrong copy here would desync the wrapper from what it was written
### against.
###=============================================================================
read_verilog -sv [list \
    ${NANOSOC_M0_SOC_SRC}/build_soc/rtl/nanosoc_soc_config_pkg.sv \
    ${NANOSOC_M0_SOC_SRC}/build_soc/rtl/nanosoc_ss_cpu.sv \
    ${NANOSOC_M0_SOC_SRC}/build_soc/rtl/nanosoc.sv \
]

puts "INFO: filelist.tcl -- all nanosoc_m0_soc RTL + stage-0 bootrom read into [current_fileset]."
