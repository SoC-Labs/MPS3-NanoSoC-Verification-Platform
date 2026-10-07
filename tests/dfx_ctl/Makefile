# Cocotb Makefile for dfx_ctl verification (mps3-nanosoc-platform).
# See dut_notes.md for port list/stub status; tests/README.md for strategy.
#
# Usage: source <repo-root>/set_env.sh; make  (set_env.sh exists since W-SIM/I24)
# Prefer `make -C tests` for the RTL-readiness-gated summary.

TOPLEVEL_LANG = verilog
SIM ?= vcs

RTL_DIR := $(abspath $(CURDIR)/../../fpga/shell/ip/dfx_ctl)

VERILOG_SOURCES = $(RTL_DIR)/dfx_ctl.sv
COMPILE_ARGS += -sverilog

TOPLEVEL = dfx_ctl
MODULE   = test_dfx_ctl

# SVA + coverage (A5): bind the reusable AXI4-Lite protocol checker onto this
# slave, and honour COVERAGE=1. See tests/common/bench_common.mk.
SVA_MODULES    := axi4lite_protocol_checker.sv
SVA_BIND_FILES := bind_dfx_ctl.sv
include $(abspath $(CURDIR)/../common)/bench_common.mk

include $(shell cocotb-config --makefiles)/Makefile.sim

# CONTROL -- AFTER the cocotb include, never before (the first target in a
# makefile is its default goal).
#
#   make control-wdt-unclamp     MUST FAIL.
#
# Rebuilds this same bench against a copy of dfx_ctl.sv with the watchdog reset
# fix reverted -- decouple_en_q back on s_axi_aresetn, the set-dominant
# wdt_reset_i branch disabled, rp_gate_q's reset back to a bare 1'b1. The ports
# are unchanged, so the ONLY variable is the reset behaviour under test, and
# test_watchdog_reset_* go red. docs/planning/SERVICES_PARTITION.md §5.4.
#
# Its own MUTANT_DIR and SIM_BUILD: VCS will re-run a simv built from the
# unmutated RTL otherwise, and a control that quietly tested the good build is
# worse than no control at all.
MUTANT_DIR := $(CURDIR)/mutant

.PHONY: control-wdt-unclamp
control-wdt-unclamp:
	@echo "== CONTROL: the pre-fix RTL un-clamps on a watchdog reset. MUST FAIL. =="
	@mkdir -p $(MUTANT_DIR)
	python3 $(CURDIR)/mutate_pre_wdt_fix.py $(RTL_DIR)/dfx_ctl.sv $(MUTANT_DIR)/dfx_ctl.sv
	$(MAKE) sim RTL_DIR=$(MUTANT_DIR) SIM_BUILD=$(CURDIR)/sim_build_mutant
