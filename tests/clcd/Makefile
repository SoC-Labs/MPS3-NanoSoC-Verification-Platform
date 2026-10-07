# Cocotb Makefile for clcd verification (mps3-nanosoc-platform).
# See dut_notes.md for the DUT's port list, and tests/README.md for the overall
# verification strategy.
#
# Usage:
#   source <repo-root>/set_env.sh   # pins miniconda python3.10 + cocotb 2.0.1 +
#                                   # VCS 2022.06-SP2 + license env
#   make                            # DEFAULT: the shipping write-only build
#                                   #   (READ_PATH=0) -- test_clcd.py: init
#                                   #   sequence, bus protocol + TIMING, CTRL
#                                   #   pads, fifo_reset, FIFO backpressure.
#   make READP=1                    # SECONDARY: elaborate the read-back path
#                                   #   (READ_PATH=1) via a VCS -pvalue override
#                                   #   and run test_clcd_readpath.py -- the
#                                   #   no-read-side-effect sweep + the
#                                   #   CTRL.read_start-armed read cycle.
#
# Prefer `make -C tests BLOCK=clcd run-one` (top-level Makefile) for the default
# flow: it checks RTL readiness first (tests/common/list_benches.py). The
# READP=1 pass is a direct `make READP=1` here (one parameter value per simv).

TOPLEVEL_LANG = verilog
SIM ?= vcs

RTL_DIR := $(abspath $(CURDIR)/../../fpga/shell/ip/clcd)

VERILOG_SOURCES = $(RTL_DIR)/clcd.sv
# clcd_core.sv is a SEPARATE file that clcd.sv instantiates (clcd.sv:394,
# `clcd_core ... u_core`). It was split out of clcd.sv by the CLCD-KVM work
# (60d47f1) AFTER this bench was written (7d35c45), and this line was not added
# at the time -- so VCS failed elaboration with
#   Error-[CFCILFBI] Cannot find cell in liblist
#   Cell 'clcd_core' cannot be found in liblist for binding instance 'clcd.u_core'
# and `make check` stage [7/9] (cocotb benches, SIM=vcs) has been red ever since.
VERILOG_SOURCES += $(RTL_DIR)/clcd_core.sv

# RTL is SystemVerilog (`logic`, `always_ff`, `typedef enum`) -- VCS needs
# -sverilog. VCS-only: Questa's vlog rejects it (vlog-1902) and parses *.sv as
# SystemVerilog by extension anyway (tests/README.md "Sim target").
ifeq ($(SIM),vcs)
COMPILE_ARGS += -sverilog
endif

TOPLEVEL = clcd

# SVA + coverage (A5): bind the reusable AXI4-Lite protocol checker onto this
# slave, and honour COVERAGE=1. SVA_MODULES pulls the shared checker from
# tests/common/sva/ (read-only reuse); the bind file is THIS block's own
# (tests/clcd/bind_clcd.sv), so it is added to VERILOG_SOURCES directly rather
# than via SVA_BIND_FILES (which resolves under tests/common/sva/).
SVA_MODULES := axi4lite_protocol_checker.sv
include $(abspath $(CURDIR)/../common)/bench_common.mk
VERILOG_SOURCES += $(CURDIR)/bind_clcd.sv

READP ?= 0
ifeq ($(READP),1)
  # Elaborate the optional read-back datapath. shell_bd.tcl would set READ_PATH
  # per the board fact at Phase-D; the default build ships READ_PATH=0.
  ifeq ($(SIM),vcs)
    COMPILE_ARGS += -pvalue+clcd.READ_PATH=1
  endif
  MODULE    = test_clcd_readpath
  # Separate build/results so the two elaborations never collide (a stale simv
  # built with the other READ_PATH would silently run the wrong DUT).
  SIM_BUILD = sim_build_readpath
  COCOTB_RESULTS_FILE = $(CURDIR)/results_readpath.xml
else
  MODULE    = test_clcd
endif

include $(shell cocotb-config --makefiles)/Makefile.sim

# cocotb's clean:: only removes the CURRENT invocation's $(SIM_BUILD), so a bare
# `make clean` (READP=0) would strand the read-path artifacts.
clean::
	-rm -rf $(CURDIR)/sim_build_readpath $(CURDIR)/results_readpath.xml
