# -----------------------------------------------------------------------------
# jtag_dap_bringup — P2 SIM GATE for SOC400_BASELINE_INTEGRATION.md
#
# Drives the SERIAL JTAG pins (TCK/TMS/TDI/TDO) of the CoreSight SoC-400 SWJ-DP
# and halts a real Cortex-M0 to DHCSR S_HALT — the one segment nothing else
# proves. Reuses the committed multicore SoC-400 RTL + the Arm Cortex-M0-QS IP
# IN PLACE (see jtag_dap_bringup.flist); nothing is copied.
#
# Usage:
#   source <repo-root>/set_env.sh        # miniconda python + cocotb + VCS + license
#   make                                 # SIM=vcs (default)
#   make WAVES=1                         # + waves.vcd
#   make SIM=vcs NANOSOC_MULTICORE_HOME=/path/to/nanosoc-multicore-system
#
# The DUT hierarchy (SoC-400 wrappers + M0 IP) lives OUTSIDE this repo and is
# CONFIDENTIAL vendor collateral; the env vars below locate it and nothing is
# copied. Same seam and same precedence as tests/jtag_chain and fpga/dfx.
# -----------------------------------------------------------------------------
TOPLEVEL_LANG = verilog
SIM ?= vcs

TOPLEVEL = tb_top
MODULE   = test_jtag_dap_bringup

REPO_ROOT ?= $(abspath $(CURDIR)/../..)

# --- site-local overrides, NOT tracked ---------------------------------------
# Same seam and same precedence as fpga/dfx/Makefile:64-69. See tools.env.example.
TOOLS_ENV ?= $(REPO_ROOT)/tools.env
-include $(TOOLS_ENV)

# --- external RTL locations --------------------------------------------------
# NANOSOC_MULTICORE_HOME has NO DEFAULT, deliberately. This Makefile used to bake
# a workstation-home default: invisible on that one
# workstation, silently the wrong tree everywhere else, and it hid the fact that
# the flist had gone stale for six weeks. Fail by name instead -- see `needvars`.
NANOSOC_MULTICORE_HOME            ?=
SOCLABS_CORESIGHT_SOC400_TECH_DIR ?= $(NANOSOC_MULTICORE_HOME)/nanosoc_arch_tech/rtl/coresight_soc400_tech
SOCLABS_SLCOREM0_TECH_DIR         ?= $(NANOSOC_MULTICORE_HOME)/nanosoc_arch_tech/rtl/slcorem0_tech
ARM_IP_LIBRARY_PATH               ?=
export NANOSOC_MULTICORE_HOME SOCLABS_CORESIGHT_SOC400_TECH_DIR
export SOCLABS_SLCOREM0_TECH_DIR ARM_IP_LIBRARY_PATH REPO_ROOT

# --- flist expansion (flatten -f includes, $(VAR) -> ${VAR} for VCS) ----------
FLIST          := $(CURDIR)/jtag_dap_bringup.flist
EXPANDED_FLIST := $(CURDIR)/sim_build/jtag_dap_bringup.expanded.flist

VERILOG_SOURCES = $(CURDIR)/tb_top.sv

ifeq ($(SIM),vcs)
COMPILE_ARGS += -full64 -sverilog -timescale=1ns/1ps -debug_access+all -kdb
COMPILE_ARGS += -f $(EXPANDED_FLIST)
endif

ifdef WAVES
SIM_ARGS += +WAVES
endif

# cocotb's Makefile.sim supplies the default goal. This include MUST come before
# any target of ours, or the first target below silently becomes `make`'s default.
include $(shell cocotb-config --makefiles)/Makefile.sim

# Fail BY NAME, in seconds, rather than deep inside VCS with "Source file
# cannot be opened".
.PHONY: needvars
needvars:
	@test -n "$(NANOSOC_MULTICORE_HOME)" || { \
	   echo "error: tests/jtag_dap_bringup needs NANOSOC_MULTICORE_HOME=<path to" >&2; \
	   echo "       the multicore DUT checkout>. It has NO default -- see" >&2; \
	   echo "       tools.env.example. Set it there or on the command line." >&2; \
	   exit 1; }
	@test -d "$(SOCLABS_CORESIGHT_SOC400_TECH_DIR)/flist" || { \
	   echo "error: SOCLABS_CORESIGHT_SOC400_TECH_DIR=$(SOCLABS_CORESIGHT_SOC400_TECH_DIR)" >&2; \
	   echo "       has no flist/ -- the shared SoC-400 tech block is a nested" >&2; \
	   echo "       submodule and may not be initialised in that checkout." >&2; \
	   exit 1; }

# expand_flist.sh is a local copy of the multicore utility (SoC Labs script,
# not vendor IP) so this bench is self-contained inside mps3-nanosoc-platform.
$(EXPANDED_FLIST): $(FLIST)
	@mkdir -p $(dir $@)
	$(CURDIR)/expand_flist.sh $< $@

ifeq ($(SIM),vcs)
$(SIM_BUILD)/simv: $(EXPANDED_FLIST) needvars
endif
