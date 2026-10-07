# Cocotb Makefile for rm_socscope -- the SoCScope trace plane as a DFX RM.
#
# WHY ICARUS AND NOT VCS, when every other bench here is `SIM ?= vcs`: this RM's
# RTL is not in this repo. It is read from $SOCSCOPE_HOME, whose own benches and
# mutation gate run under Icarus (SoCScope `make hw-bench`), and whose oracle
# (hw/bench/check_selftest.py) is the SAME function used for a board capture.
# Running the RM against that oracle under a different simulator would be a
# second apparatus to keep in step.
#
# `make SIM=vcs` is WIRED (the -pvalue+ spellings below) but does NOT elaborate
# today, and the reason is in SoCScope rather than here: socscope_framer.sv:98
# declares `integer i` at module scope and writes it from two processes, one of
# them an always_comb. Icarus accepts that; VCS enforces the LRM rule and refuses
# with Error-[ICPD]. One line in SoCScope fixes it; it is that repo's to make.
#
# FIVE CONFIGURATIONS, because the two features this RM gained are parameters and
# a parameter that is only ever elaborated one way is a parameter nobody proved:
#
#   CFG=split        TRACE_CLK_FREE=1  the shipped build: egress on the
#                                      free-running trace clock
#   CFG=burst_split  TRACE_CLK_FREE=1  C2: freeze the DUT clock mid-capture and
#                                      require the egress to keep draining
#   CFG=burst_tied   TRACE_CLK_FREE=0  C2's NEGATIVE CONTROL: the same run on the
#                                      B1 single-clock tie, which must FAIL to
#                                      deliver anything after the clock stops
#   CFG=freeze       FREEZE_IN_RM=1    the HW-013 in-RM state plane: fail-safe
#                    STEP_N=1          hold polarity, freeze, step exactly 1,
#                                      and the watchdog that bounds a freeze
#   CFG=freeze_n     FREEZE_IN_RM=1    C1 exactness at N=1000 -- and the
#                    STEP_N=1000       condition under which it stops being exact
#
# Usage:
#   source <repo-root>/set_env.sh     # miniconda python3.10 + cocotb 2.0.1
#   make                              # every configuration, in order
#   make CFG=freeze sim               # one configuration
#   SOCSCOPE_HOME=/path/to/SoCScope make
TOPLEVEL_LANG = verilog
SIM ?= icarus

# The SoCScope checkout. Same default and same variable as
# fpga/dfx/rms/rm_socscope/filelist.tcl and fpga/dfx/Makefile -- one name for one
# thing, so a bench and a synthesis cannot read different trees.
SOCSCOPE_HOME ?= $(abspath $(CURDIR)/../../../SoCScope)
SS_RTL  := $(SOCSCOPE_HOME)/hw/rtl
RTL_DIR := $(abspath $(CURDIR)/../../fpga/dfx/rms/rm_socscope)

# The module list is READ from SoCScope's own filelists, exactly as filelist.tcl
# reads them. Restating it here would make this the fifth copy; the fourth cost a
# day when socscope_cfg was extracted (SoCScope 9a734d5).
SS_SOURCES := $(addprefix $(SS_RTL)/,$(shell sed -e 's/\#.*//' $(SS_RTL)/socscope_trace_top.f $(SS_RTL)/socscope_selftest.f 2>/dev/null))
VERILOG_SOURCES = $(SS_SOURCES) $(RTL_DIR)/rm_socscope.sv

TOPLEVEL = rm_socscope
MODULE   = test_rm_socscope

# --- the configuration matrix ------------------------------------------------
# EGRESS_DIVISOR and SELFTEST_GAP_CYCLES are moved OFF their shipped values here
# and only here: at the shipped pacing (HW-007: one frame per 20 ms, because the
# shell's FIFO is 16 bytes) a single record costs 20 ms of simulated time. The
# bench proves the mechanism, not the pacing; the pacing is a board measurement.
CFG ?= split
ifeq ($(CFG),split)
  P_TRACE := 1
  P_FREEZE := 0
  P_STEP := 1
  P_WDOG := 64000000
  P_DIV := 3
  P_GAP := 800
else ifeq ($(CFG),burst_split)
  P_TRACE := 1
  P_FREEZE := 0
  P_STEP := 1
  P_WDOG := 64000000
  P_DIV := 3
  P_GAP := 300
else ifeq ($(CFG),burst_tied)
  P_TRACE := 0
  P_FREEZE := 0
  P_STEP := 1
  P_WDOG := 64000000
  P_DIV := 3
  P_GAP := 300
else ifeq ($(CFG),freeze)
  P_TRACE := 1
  P_FREEZE := 1
  P_STEP := 1
  P_WDOG := 600
  P_DIV := 3
  P_GAP := 800
else ifeq ($(CFG),freeze_n)
  P_TRACE := 1
  P_FREEZE := 1
  P_STEP := 1000
  P_WDOG := 100000
  P_DIV := 3
  P_GAP := 800
else
  $(error unknown CFG=$(CFG) -- one of: split burst_split burst_tied freeze freeze_n)
endif

PARAMS := TRACE_CLK_FREE=$(P_TRACE) FREEZE_IN_RM=$(P_FREEZE) STEP_N=$(P_STEP) \
          FREEZE_WDOG_CYCLES=$(P_WDOG) EGRESS_DIVISOR=$(P_DIV) \
          SELFTEST_GAP_CYCLES=$(P_GAP)

# socscope_records.vh is `include`d by the SoCScope sources -- the record format
# lives in one header for the same reason the module list lives in one filelist.
ifeq ($(SIM),icarus)
COMPILE_ARGS += -I$(SS_RTL) $(foreach p,$(PARAMS),-P$(TOPLEVEL).$(p))
else ifeq ($(SIM),vcs)
COMPILE_ARGS += -sverilog +incdir+$(SS_RTL) $(foreach p,$(PARAMS),-pvalue+$(TOPLEVEL).$(p))
endif

# One build tree and one result file per configuration, so a later config cannot
# silently re-run an earlier config's elaboration.
SIM_BUILD := sim_build_$(CFG)
COCOTB_RESULTS_FILE := results_$(CFG).xml

# The test reads the configuration it is running under rather than inferring it
# from the RTL: a bench that guesses its own parameters cannot fail when the
# Makefile and the elaboration disagree.
export CFG
export RM_SOCSCOPE_TRACE_CLK_FREE := $(P_TRACE)
export RM_SOCSCOPE_FREEZE_IN_RM   := $(P_FREEZE)
export RM_SOCSCOPE_STEP_N         := $(P_STEP)
export RM_SOCSCOPE_WDOG           := $(P_WDOG)
export RM_SOCSCOPE_DIV            := $(P_DIV)
export RM_SOCSCOPE_GAP            := $(P_GAP)
export SOCSCOPE_HOME

include $(abspath $(CURDIR)/../common)/bench_common.mk
include $(shell cocotb-config --makefiles)/Makefile.sim

.PHONY: all-cfgs check-socscope
check-socscope:
	@test -d "$(SS_RTL)" || { \
	  echo "error: rm_socscope bench needs the SoCScope checkout -- SOCSCOPE_HOME='$(SOCSCOPE_HOME)' has no hw/rtl."; \
	  echo "       Set SOCSCOPE_HOME (see tools.env.example); the RTL is NOT vendored into this repo."; \
	  exit 1; }

all-cfgs: check-socscope
	@rc=0; for c in split burst_split burst_tied freeze freeze_n; do \
	  echo "==== rm_socscope CFG=$$c ===="; \
	  $(MAKE) --no-print-directory CFG=$$c sim || rc=1; \
	done; \
	exit $$rc

.DEFAULT_GOAL := all-cfgs
