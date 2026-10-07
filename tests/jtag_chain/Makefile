# -----------------------------------------------------------------------------
# jtag_chain — TWO TAPs on ONE 4-wire partition wire-set.
#
# Drives the four JTAG partition pins of the REAL fpga/rp/nanosoc_iice/
# rp_nanosoc_iice_shim.sv and proves the IEEE 1149.1 daisy chain it builds:
# both IDCODEs read back, the DUT's Cortex-M0 still halts through the extra
# BYPASS/IR padding, and an Identify-TAP shift gets through with the DAP in
# BYPASS.
#
# Usage:
#   source <repo-root>/set_env.sh    # miniconda python + cocotb + VCS + licence
#   make                             # SIM=vcs (default)
#   make WAVES=1                     # + waves.vcd
#   make control-order               # THE RTL-SIDE CONTROL: rebuild with the
#                                    # chain order flipped in the FABRIC while
#                                    # the host keeps the shipped order, and
#                                    # require the IDCODE test to FAIL.
#
# The DUT hierarchy (SoC-400 wrappers + Arm M0 IP) lives OUTSIDE this repo and
# is CONFIDENTIAL vendor collateral; the env vars below locate it and nothing is
# copied. Same discipline as tests/jtag_dap_bringup.
# -----------------------------------------------------------------------------
TOPLEVEL_LANG = verilog
SIM ?= vcs

TOPLEVEL = tb_top
MODULE   = test_jtag_chain

REPO_ROOT ?= $(abspath $(CURDIR)/../..)

# --- site-local overrides, NOT tracked ---------------------------------------
# Same seam and same precedence as fpga/dfx/Makefile:64-69. See tools.env.example.
TOOLS_ENV ?= $(REPO_ROOT)/tools.env
-include $(TOOLS_ENV)

# --- external RTL locations --------------------------------------------------
# NANOSOC_MULTICORE_HOME has NO DEFAULT, deliberately: a default naming one
# workstation's home directory is invisible on that machine and silently reads
# the wrong tree on every other one. tests/jtag_dap_bringup still bakes such a
# default and it has already gone stale -- see this bench's README.
NANOSOC_MULTICORE_HOME            ?=
SOCLABS_CORESIGHT_SOC400_TECH_DIR ?= $(NANOSOC_MULTICORE_HOME)/nanosoc_arch_tech/rtl/coresight_soc400_tech
SOCLABS_SLCOREM0_TECH_DIR         ?= $(NANOSOC_MULTICORE_HOME)/nanosoc_arch_tech/rtl/slcorem0_tech
ARM_IP_LIBRARY_PATH               ?=
export NANOSOC_MULTICORE_HOME SOCLABS_CORESIGHT_SOC400_TECH_DIR
export SOCLABS_SLCOREM0_TECH_DIR ARM_IP_LIBRARY_PATH REPO_ROOT

PYTHON ?= python3

# --- flist expansion (flatten -f includes, $(VAR) -> ${VAR} for VCS) ----------
SIM_BUILD_DIRNAME ?= sim_build
FLIST          := $(CURDIR)/jtag_chain.flist
EXPANDED_FLIST := $(CURDIR)/$(SIM_BUILD_DIRNAME)/jtag_chain.expanded.flist

VERILOG_SOURCES = $(CURDIR)/tb_top.sv

ifeq ($(SIM),vcs)
COMPILE_ARGS += -full64 -sverilog -timescale=1ns/1ps -debug_access+all -kdb
COMPILE_ARGS += -f $(EXPANDED_FLIST)
endif

# CHAIN_ORDER=0 flips the chain order IN THE FABRIC (the shim's
# IICE_TAP_NEAREST_TDI generate) without touching the host side. Used only by
# `make control-order`.
CHAIN_ORDER ?= 1
ifeq ($(SIM),vcs)
COMPILE_ARGS += -pvalue+tb_top.CHAIN_IICE_NEAREST_TDI=$(CHAIN_ORDER)
endif

ifdef WAVES
SIM_ARGS += +WAVES
endif

.PHONY: boundary-check control-order

# cocotb's Makefile.sim supplies the default goal. This include MUST come before
# any target of ours, or the first target below silently becomes `make`'s
# default and a bare `make` runs the guard and nothing else -- which looks like
# a passing build and compiles no RTL at all.
include $(shell cocotb-config --makefiles)/Makefile.sim

# The boundary guard runs BEFORE anything is compiled: a stale
# iice_core_stub.sv would still elaborate and still pass, against a topology the
# RM does not have. See check_core_stub_ports.py's header.
boundary-check: needvars
	@$(PYTHON) $(CURDIR)/check_core_stub_ports.py $(REPO_ROOT)

# Fail BY NAME, in seconds, rather than deep inside VCS with "Source file
# cannot be opened".
.PHONY: needvars
needvars:
	@test -n "$(NANOSOC_MULTICORE_HOME)" || { \
	   echo "error: tests/jtag_chain needs NANOSOC_MULTICORE_HOME=<path to the" >&2; \
	   echo "       multicore DUT checkout>. It has NO default -- see" >&2; \
	   echo "       tools.env.example. Set it there or on the command line." >&2; \
	   exit 1; }
	@test -d "$(SOCLABS_CORESIGHT_SOC400_TECH_DIR)/flist" || { \
	   echo "error: SOCLABS_CORESIGHT_SOC400_TECH_DIR=$(SOCLABS_CORESIGHT_SOC400_TECH_DIR)" >&2; \
	   echo "       has no flist/ -- the shared SoC-400 tech block is a nested" >&2; \
	   echo "       submodule and may not be initialised in that checkout." >&2; \
	   exit 1; }

# expand_flist.sh is a local copy of the multicore utility (a SoC Labs script,
# not vendor IP) so this bench is self-contained inside mps3-nanosoc-platform.
$(EXPANDED_FLIST): $(FLIST)
	@mkdir -p $(dir $@)
	$(CURDIR)/expand_flist.sh $< $@

ifeq ($(SIM),vcs)
$(SIM_BUILD)/simv: $(EXPANDED_FLIST) boundary-check
endif

# -----------------------------------------------------------------------------
# control-order — THE RTL-SIDE NEGATIVE CONTROL.
#
# Rebuild the FABRIC with the chain order reversed (SWJ-DP nearest TDI) while
# the host-side scan arithmetic in test_jtag_chain.py keeps assuming the shipped
# order. The two IDCODEs then come back swapped and test_chain_idcodes MUST
# fail. If it passes, the test is not actually reading position-dependent data
# and every positive result in this bench is worthless.
#
# Built into its own SIM_BUILD so it cannot reuse the shipped-order simv --
# which is the exact way a "control" quietly stops being one.
# -----------------------------------------------------------------------------
control-order:
	@echo "== CONTROL: rebuilding with the chain order FLIPPED in the fabric =="
	@rm -rf $(CURDIR)/sim_build_control
	@if $(MAKE) --no-print-directory \
	      SIM_BUILD=$(CURDIR)/sim_build_control \
	      SIM_BUILD_DIRNAME=sim_build_control \
	      CHAIN_ORDER=0 \
	      TESTCASE=test_chain_idcodes \
	      COCOTB_RESULTS_FILE=$(CURDIR)/sim_build_control/results.xml \
	      >$(CURDIR)/control_order.log 2>&1; then \
	   echo "CONTROL FAILED TO FAIL: test_chain_idcodes PASSED with the chain"; \
	   echo "  order reversed in the fabric. The test is not position-sensitive,"; \
	   echo "  so its positive result proves nothing. See $(CURDIR)/control_order.log"; \
	   exit 1; \
	 else \
	   echo "CONTROL OK: test_chain_idcodes FAILED with the order reversed"; \
	   grep -E "IDCODE|FAIL" $(CURDIR)/control_order.log | head -20 || true; \
	 fi
