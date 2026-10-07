# Cocotb Makefile for telem verification (mps3-nanosoc-platform).
# See dut_notes.md for the DUT's port list, and tests/README.md for the
# overall verification strategy.
#
# Usage:
#   source <repo-root>/set_env.sh   # pins miniconda python3.10 + cocotb
#                                   # 2.0.1 + VCS 2022.06-SP2 + license env
#   make                            # DEFAULT: seam mode (SIM_FAKE_DATA=0,
#                                   #   the synthesis configuration) — the
#                                   #   bench drives the ina228_* engine
#                                   #   seam ports directly
#   make FAKE=1                     # SECONDARY: fake mode (SIM_FAKE_DATA=1
#                                   #   via VCS -pvalue parameter override,
#                                   #   FAKE_PERIOD shortened to 16) — runs
#                                   #   test_telem_fake.py against the RTL's
#                                   #   internal generator formulas
#
# WHY SEAM MODE IS THE DEFAULT (the block README offers both routes):
#   - SIM_FAKE_DATA=0 is what synthesis builds; benching the shipping
#     configuration matters more than benching the bench-aid.
#   - The seam ports ARE the agreed contract for the follow-up
#     ina228_i2c_master engine (README "engine seam contract" table) —
#     driving them from the bench exercises exactly the interface that
#     engine must satisfy, so this bench doubles as the seam's spec test.
#   - Fake mode cannot exercise i2c_err stickiness, enable-gated sample
#     rejection, or atomicity with adversarial (mid-stream-changing)
#     values — the generator never errors and only strobes while enabled.
#   Fake mode is still covered (its value formulas are contract-documented
#   and A5-checkable) — just as the secondary `make FAKE=1` build, since
#   one compiled sim can only have one parameter value.
#
# Prefer `make -C tests BLOCK=telem run-one` (top-level Makefile) for the
# default flow; run `make FAKE=1` here directly for the fake-mode pass.

TOPLEVEL_LANG = verilog
SIM ?= vcs

RTL_DIR := $(abspath $(CURDIR)/../../fpga/shell/ip/telem)

VERILOG_SOURCES = $(RTL_DIR)/telem.sv

# RTL is SystemVerilog (`logic`, `always_ff`) -- VCS needs -sverilog.
# VCS-only flag: Questa's vlog rejects it (vlog-1902) and already parses
# *.sv as SystemVerilog by extension (see tests/README.md "Sim target").
# NOTE the FAKE=1 -pvalue+ overrides below are also VCS syntax; Questa
# would need vsim -g/-G instead — fake mode is VCS-only for now.
ifeq ($(SIM),vcs)
COMPILE_ARGS += -sverilog
endif

TOPLEVEL = telem

# SVA + coverage (A5): bind the reusable AXI4-Lite protocol checker onto this
# slave, and honour COVERAGE=1. See tests/common/bench_common.mk. (The FAKE=1
# path below still binds the checker — it is the same DUT with SIM_FAKE_DATA=1.)
SVA_MODULES    := axi4lite_protocol_checker.sv
SVA_BIND_FILES := bind_telem.sv
include $(abspath $(CURDIR)/../common)/bench_common.mk

FAKE ?= 0
ifeq ($(FAKE),1)
  # VCS parameter override at elaboration (telem.sv deliberately declares
  # SIM_FAKE_DATA/FAKE_PERIOD as `int` so bare -pvalue+ works). FAKE_PERIOD
  # is shortened 64 -> 16 to keep the sim quick; the bench freezes readings
  # via CTRL.enable=0 before checking, so the shorter period cannot tear.
  COMPILE_ARGS += -pvalue+telem/SIM_FAKE_DATA=1 -pvalue+telem/FAKE_PERIOD=16
  MODULE    = test_telem_fake
  # Separate build/results so seam- and fake-mode artifacts never collide
  # (a stale simv elaborated with the other parameter value would silently
  # run the wrong DUT).
  SIM_BUILD = sim_build_fake
  COCOTB_RESULTS_FILE = $(CURDIR)/results_fake.xml
else
  MODULE    = test_telem
endif

include $(shell cocotb-config --makefiles)/Makefile.sim

# cocotb's clean:: only removes the CURRENT invocation's $(SIM_BUILD), so a
# bare `make clean` (FAKE=0) would strand the fake-mode artifacts.
clean::
	-rm -rf $(CURDIR)/sim_build_fake $(CURDIR)/results_fake.xml
