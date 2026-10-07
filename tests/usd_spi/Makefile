# Cocotb bench for fpga/shell/ip/usd_spi/usd_spi.sv -- USD @ 0x44A4_0000, the
# SPI-mode master for the MPS3 USER microSD slot
# (docs/planning/HANDOVER_USD_OVERLAY_STORE.md §4.1, §4.7).
#
#   source <repo-root>/set_env.sh
#   make                              # regress: every CFG in REGRESS_CFGS (bdfast w12)
#   make premint                      # CFG=bd: the shipped 10 ms debounce (~5 min)
#   make sim CFG=bdfast               # one configuration (the quick dev loop)
#   make sim CFG=bd TESTCASE=test_sd_end_to_end
#   make control-cd-gate-ignored      # the CONTROL.  MUST FAIL.  See below.
#
# WHO RUNS WHAT. `make check` (stage 7, check-sim) and `make -C tests` run the
# default goal: bdfast + w12 -- the shipped width 32 and the narrow width, with
# a 64-cycle debounce, a few minutes. The full `bd` configuration (width 32 AND
# the shipped DEBOUNCE_CYCLES default, ~5 min of sim) is the PRE-MINT gate:
# fpga/dfx/Makefile `mint-preflight` (mint stage 1) runs `make premint` here
# before any static is built (handover §11, lead call 3).
#
# CFG picks the elaborated parameterisation. Each has its own SIM_BUILD, because
# VCS only rebuilds simv when a SOURCE changes: a -pvalue change alone would
# silently re-run the previous configuration's simv.
#
#   bd      C_S_AXI_ADDR_WIDTH=32, DEBOUNCE_CYCLES = RTL default (10 ms at 100 MHz).
#           EXACTLY what shell_bd.tcl instantiates (fpga/shell/bd/shell_bd.tcl usd_spi_0). The bench
#           drives BASE+offset (0x44A4_0000 + off), as the interconnect does.
#           This is the configuration that matters: a CSR block once shipped dead
#           because every bench elaborated the RTL default width instead
#           (tests/csr_decode_width, scripts/harness_gates/check_bench_param_parity.py).
#   w12     C_S_AXI_ADDR_WIDTH=12 (the RTL default), DEBOUNCE_CYCLES=64. Offsets
#           driven from 0. Keeps the narrow instantiation honest too.
#   bdfast  as bd, but DEBOUNCE_CYCLES=64: same tests, a fraction of the sim time.
#
# The tests read the elaborated width and debounce back from the DUT's own ports
# and parameters, so nothing here is restated in Python.

TOPLEVEL_LANG = verilog
SIM ?= vcs

RTL_DIR ?= $(abspath $(CURDIR)/../../fpga/shell/ip/usd_spi)

VERILOG_SOURCES = $(RTL_DIR)/usd_spi.sv
COMPILE_ARGS += -sverilog

TOPLEVEL = usd_spi
MODULE   = test_usd_spi

CFG          ?= bd
# make check / make -C tests: bdfast + w12. The `bd` config is `make premint`.
REGRESS_CFGS ?= bdfast w12

# Literal -pvalue strings, not built from $(TOPLEVEL):
# scripts/harness_gates/check_bench_param_parity.py greps this file for the
# shipped parameterisation, and a variable would hide it.
ifeq ($(CFG),bd)
  PVALUE := -pvalue+usd_spi.C_S_AXI_ADDR_WIDTH=32
else ifeq ($(CFG),bdfast)
  PVALUE := -pvalue+usd_spi.C_S_AXI_ADDR_WIDTH=32 -pvalue+usd_spi.DEBOUNCE_CYCLES=64
else ifeq ($(CFG),w12)
  PVALUE := -pvalue+usd_spi.DEBOUNCE_CYCLES=64
else
  $(error unknown CFG=$(CFG); expected bd, bdfast or w12)
endif

ifeq ($(SIM),vcs)
COMPILE_ARGS += $(PVALUE)
endif

# `:=`, not `?=`: cocotb's Makefile.inc EXPORTS both, so a `?=` here would let a
# regress sub-make inherit the PREVIOUS configuration's build directory.
# (A command-line override, as the control target below uses, still wins.)
SIM_BUILD           := $(CURDIR)/sim_build/$(CFG)
COCOTB_RESULTS_FILE := $(SIM_BUILD)/results.xml

# SVA + coverage (A5): bind the reusable AXI4-Lite protocol checker onto this
# slave (at the instance's own C_S_AXI_ADDR_WIDTH -- see bind_usd_spi.sv), and
# honour COVERAGE=1. See tests/common/bench_common.mk.
SVA_MODULES    := axi4lite_protocol_checker.sv
SVA_BIND_FILES := bind_usd_spi.sv
include $(abspath $(CURDIR)/../common)/bench_common.mk

# The default goal is the regression over REGRESS_CFGS, so the suite runner
# (`make -C tests/usd_spi`, tests/Makefile, `make check`) exercises the shipped
# width. The shipped debounce too is `premint`'s job (below).
.DEFAULT_GOAL := regress

include $(shell cocotb-config --makefiles)/Makefile.sim

.PHONY: regress premint
regress:
	@for c in $(REGRESS_CFGS); do \
	  echo "== usd_spi: CFG=$$c =="; \
	  $(MAKE) --no-print-directory sim CFG=$$c || exit 1; \
	done

# PRE-MINT: exactly what shell_bd.tcl instantiates -- C_S_AXI_ADDR_WIDTH=32 and
# DEBOUNCE_CYCLES at its RTL default (10 ms at 100 MHz). A fresh SIM_BUILD every
# time (VCS reuses a simv whose SOURCES are unchanged), and a results.xml that
# must exist and hold no failure: cocotb's own exit code is the first check, the
# XML the second, so a sim that dies before writing results cannot pass.
premint:
	@echo "== usd_spi PRE-MINT: CFG=bd (width 32 + the shipped 10 ms debounce) =="
	rm -rf $(CURDIR)/sim_build/bd
	$(MAKE) --no-print-directory sim CFG=bd
	@test -s $(CURDIR)/sim_build/bd/results.xml || { echo "FAIL usd_spi premint: no results.xml"; exit 1; }
	@! grep -q '<failure' $(CURDIR)/sim_build/bd/results.xml || { echo "FAIL usd_spi premint: failures in results.xml"; exit 1; }
	@echo "PASS usd_spi premint (CFG=bd)"

clean::
	rm -rf $(CURDIR)/sim_build $(CURDIR)/results.xml $(CURDIR)/__pycache__ $(CURDIR)/ucli.key

# CONTROL -- after the cocotb include (the first target in a makefile would
# otherwise become its default goal; .DEFAULT_GOAL pins it anyway).
#
#   make control-cd-gate-ignored     MUST FAIL.
#
# Rebuilds this same bench against a copy of usd_spi.sv whose pad gate ignores
# card detect (pads_en = EN, instead of EN && (CD_PRESENT || CD_IGNORE)) -- see
# mutate_cd_gate_ignored.py. The ports are unchanged, so the only variable is the
# gate, and test_pads_hiz_without_card must go red. Its own RTL copy and its own
# SIM_BUILD under sim_build/ (VCS would otherwise re-run a simv built from the
# unmutated RTL, and a control that quietly tested the good build is worse than
# no control at all).
CONTROL_CFG ?= bdfast
MUTANT_DIR  := $(CURDIR)/sim_build/mutant_rtl

.PHONY: control-cd-gate-ignored
control-cd-gate-ignored:
	@echo "== CONTROL: a pad gate that ignores card detect. MUST FAIL. =="
	@mkdir -p $(MUTANT_DIR)
	python3 $(CURDIR)/mutate_cd_gate_ignored.py $(RTL_DIR)/usd_spi.sv $(MUTANT_DIR)/usd_spi.sv
	$(MAKE) --no-print-directory sim CFG=$(CONTROL_CFG) RTL_DIR=$(MUTANT_DIR) \
	  SIM_BUILD=$(CURDIR)/sim_build/control_$(CONTROL_CFG) \
	  COCOTB_RESULTS_FILE=$(CURDIR)/sim_build/control_$(CONTROL_CFG)/results.xml
