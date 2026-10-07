# Cocotb Makefile for swd_bb verification (mps3-nanosoc-platform).
# See dut_notes.md for the DUT's port list, and tests/README.md for the
# overall verification strategy.
#
# Usage:
#   source <repo-root>/set_env.sh   # pins miniconda python3.10 + cocotb
#                                   # 2.0.1 + VCS 2022.06-SP2 + license env
#   make                            # run this block's bench directly
#
# Prefer `make -C tests BLOCK=swd_bb run-one` (the top-level Makefile) for
# routine use: it checks RTL readiness first (tests/common/list_benches.py).

TOPLEVEL_LANG = verilog
SIM ?= vcs

RTL_DIR := $(abspath $(CURDIR)/../../fpga/shell/ip/swd_bb)

VERILOG_SOURCES = $(RTL_DIR)/swd_bb.sv

# RTL is SystemVerilog (`logic`, `always_ff`) -- VCS needs -sverilog.
# VCS-only flag: Questa's vlog rejects it (vlog-1902) and already parses
# *.sv as SystemVerilog by extension (see tests/README.md "Sim target").
ifeq ($(SIM),vcs)
COMPILE_ARGS += -sverilog
endif

# Leaf module, plain I/O -- cocotb attaches directly (no wrapper).
TOPLEVEL = swd_bb
MODULE   = test_swd_bb

# SVA + coverage (A5): bind the reusable AXI4-Lite protocol checker onto this
# slave, and honour COVERAGE=1. See tests/common/bench_common.mk.
SVA_MODULES    := axi4lite_protocol_checker.sv
SVA_BIND_FILES := bind_swd_bb.sv
include $(abspath $(CURDIR)/../common)/bench_common.mk

include $(shell cocotb-config --makefiles)/Makefile.sim
