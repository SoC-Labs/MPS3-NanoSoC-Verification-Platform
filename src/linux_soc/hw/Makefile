# -----------------------------------------------------------------------------
# src/linux_soc/hw/Makefile -- driver for `linux_soc`: the LINUX-CAPABLE
# "DDR4 MIG + MicroBlaze V" SoC (RV32IMAC + Zba/Zbb/Zbs, Sv32 MMU, atomics) on
# the MPS3's xcku115-flvb1760-1-c.
#
# Evolved from the PROVEN poc/ddr4_mbv S0 spike (which built, closed timing at
# WNS +0.174 ns and passed a DDR4 BFM calibration sim). poc/ddr4_mbv is left
# intact as the reference; this tree is the one downstream agents build against.
#
# This is the FOUNDATION: mbv_soc.tcl fixes the address map, the CPU ISA and the
# reset vector that the device tree, OpenSBI, the kernel and Buildroot all depend
# on. ADDRESS_MAP.md is the contract; build/address_map.txt is that contract read
# back off the built block design.
#
# It does NOT touch fpga/shell/, fpga/dfx/ or static_id, and nothing here is wired
# into the repo-root `make check` -- that gate stays green independent of this dir.
#
# All Vivado scratch (checkpoints, reports, journal, log) lands under
# src/linux_soc/hw/build/, which the repo's global `build/` .gitignore rule covers.
#
# Typical run:
#   make bd        <- the gate that matters for the foundation: elaborate the BD
#                     and validate_bd_design cleanly. Minutes, not hours.
#   make all       <- full pipeline (pins -> IP -> BD -> synth -> impl -> reports)
#   cat build/reports/RESULT.txt
#   cat build/address_map.txt      <- the map as actually assigned by Vivado
#
# -----------------------------------------------------------------------------
# VIVADO 2026.1 PORT NOTES (both of these are load-bearing -- do not "simplify"):
#
# 1. NO EARLY-ACCESS FLAG.
#    The spike had to `export AMD_VIVADO_MICROBLAZE_V_EA := 1` because in Vivado
#    2024.1 the MicroBlaze V Sv32 MMU (C_USE_MMU=3) -- exactly what a Linux core
#    needs -- was gated behind that undocumented env var, and without it
#    set_property SILENTLY refused the value.
#    MEASURED ON THIS 2026.1 INSTALL WITH THE VARIABLE UNSET:
#        set_property CONFIG.C_USE_MMU 3 [microblaze_riscv]  ->  rc=0, reads back 3
#    The gate is GONE; an MMU-enabled MicroBlaze V is a PRODUCTION configuration at
#    2026.1. We therefore do NOT export it, and build.tcl WARNS if it is set --
#    carrying a stale EA flag into a production flow is how an early-access
#    configuration silently ships.
#
# 2. ABSOLUTE VIVADO BINARY.
#    `module load vivado/2026.1` defines `vivado` as a shell ALIAS, and aliases do
#    NOT survive into make's non-interactive $(SHELL). Invoking the bare name here
#    would fail (or, worse, find some other vivado on PATH). Note the NESTED path:
#    it is $XILINX_VIVADO/Vivado/bin/vivado, not $XILINX_VIVADO/bin/vivado.
# -----------------------------------------------------------------------------

XILINX_VIVADO ?= /research/CAD/Xilinx/Vivado/2026.1
VIVADO        ?= $(XILINX_VIVADO)/Vivado/bin/vivado
PY            ?= python3

HW_DIR    := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
BUILD     := $(HW_DIR)/build
RPT       := $(BUILD)/reports
BUILD_TCL := $(HW_DIR)/build.tcl

# gen_pins.py reads the nanosoc_tech pinmap from $SOCLABS_NANOSOC_TECH_DIR: the
# environment, else that ONE key from the repo-root tools.env (only that key is
# read -- tools.env's VIVADO is the DFX flow's, not this one's). No default.
SOCLABS_NANOSOC_TECH_DIR ?= $(strip $(shell sed -n 's/^[[:space:]]*SOCLABS_NANOSOC_TECH_DIR[[:space:]]*[:?]\{0,1\}=[[:space:]]*//p' "$(HW_DIR)/../../../tools.env" 2>/dev/null | tail -n 1))
export SOCLABS_NANOSOC_TECH_DIR

# build.tcl honours LINUX_SOC_BUILD for its scratch dir; keep the two in step.
export LINUX_SOC_BUILD := $(BUILD)

# One in-memory Vivado invocation per stage. -journal/-log land under build/.
# The stage name is passed via -tclargs.
VIVADO_RUN = mkdir -p $(BUILD) && cd $(BUILD) && $(VIVADO) -mode batch \
	-source $(BUILD_TCL) \
	-journal $(BUILD)/vivado_$(1).jou -log $(BUILD)/vivado_$(1).log \
	-tclargs $(1)

.PHONY: help pins ip bd synth impl bit reports all sim fw clean

help:
	@echo "src/linux_soc/hw -- Linux-capable MicroBlaze V + DDR4 SoC (board-free)"
	@echo ""
	@echo "  make pins     - run gen_pins.py -> ddr4_pins.xdc + pins.csv (no Vivado)"
	@echo "  make ip       - create + configure DDR4 IP ddr4_0 (in-memory)"
	@echo "  make bd       - build the linux_soc BD, validate it, wrap it, read the pin XDC"
	@echo "                  ^ THE FOUNDATION GATE. Also emits build/address_map.txt."
	@echo "  make synth    - + synth_design                       (writes post_synth.dcp)"
	@echo "  make impl     - + opt/place/route + post-place SLR check (writes post_route.dcp)"
	@echo "  make bit      - + write_bitstream + write_hw_platform (.bit + .xsa)"
	@echo "                  ^ NB: the XSA needs the BD, which a resumed post_route.dcp does"
	@echo "                    NOT carry -- only 'make all' reliably emits it. See build.tcl."
	@echo "  make reports  - + utilization/timing/DRC/clocks/io reports + RESULT gate"
	@echo "  make all      - the whole pipeline in one Vivado run  (-> build/reports/RESULT.txt)"
	@echo "                  synth -> impl -> .bit + .xsa -> reports. The handoff is written"
	@echo "                  BEFORE the timing gate, so a marginal build still yields an XSA."
	@echo "  make sim      - DDR4 IP example-design/BFM calibration sim"
	@echo "  make fw       - build the bare-metal DDR memtest      (delegates to fw/Makefile)"
	@echo "  make clean    - remove build/ ONLY (never anything outside this dir)"
	@echo ""
	@echo "  Exit gate: 'make reports'/'make all' exit 1 if DRC errors>0 or WNS<0 or WHS<0."
	@echo "  Optional:  LINUX_SOC_PBLOCK=1 confines the SoC to SLR1 (keeps SLR0's RP region empty)."
	@echo "  Overrides: VIVADO=$(VIVADO)"
	@echo "             PY=$(PY)"

# gen_pins.py is standalone (no Vivado). build.tcl re-runs it itself before read_xdc.
pins:
	cd $(HW_DIR) && $(PY) gen_pins.py

ip:
	$(call VIVADO_RUN,ip)

bd:
	$(call VIVADO_RUN,bd)
	@echo "---- ADDRESS MAP (read back off the built BD) ----"
	@cat $(BUILD)/address_map.txt 2>/dev/null || echo "(address_map.txt not produced -- BD stage did not complete)"

synth:
	$(call VIVADO_RUN,synth)

impl:
	$(call VIVADO_RUN,impl)

bit:
	$(call VIVADO_RUN,bit)

reports:
	$(call VIVADO_RUN,reports)
	@echo "---- RESULT ----" && cat $(RPT)/RESULT.txt 2>/dev/null || true

all:
	$(call VIVADO_RUN,all)
	@echo "---- RESULT ----" && cat $(RPT)/RESULT.txt 2>/dev/null || true

# --- sim: the DDR4 IP's own example design + Micron model, run in xsim ---------
# sim.tcl stands up the IP's example design (traffic generator + memory model) and
# greps for the TB's PASS markers. Proven on the spike: TEST PASSED, 0 mismatches.
# (No AMD_VIVADO_MICROBLAZE_V_EA here either -- see the port notes above.)
sim:
	mkdir -p $(BUILD)/sim
	cd $(BUILD)/sim && $(VIVADO) -mode batch \
	    -source $(HW_DIR)/sim.tcl \
	    -journal $(BUILD)/sim/vivado_sim.jou -log $(BUILD)/sim/vivado_sim.log \
	    -tclargs $(BUILD)/sim
	@grep -E '^SIMRESULT:' $(BUILD)/sim/vivado_sim.log || { \
	  echo "sim: no SIMRESULT line -- run did not reach the verdict"; exit 1; }
	@grep -q 'SIMRESULT: PASS' $(BUILD)/sim/vivado_sim.log || { \
	  echo "sim: calibration NOT proven -- inspect $(BUILD)/sim"; exit 1; }
	@echo "sim: DDR4 calibration PASSED."

# --- fw: bare-metal DDR memtest (a smoke test for the map, not the Linux boot) --
fw:
	$(MAKE) -C $(HW_DIR)/fw

# --- clean: build/ ONLY -- never rm -rf anything outside this directory ---------
clean:
	rm -rf $(BUILD)
