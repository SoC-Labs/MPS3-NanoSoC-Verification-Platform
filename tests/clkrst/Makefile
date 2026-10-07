# Cocotb Makefile for dut_clkrst verification (mps3-nanosoc-platform).
# See dut_notes.md for port list/stub status; tests/README.md for strategy.
#
# Usage: source <repo-root>/set_env.sh; make  (set_env.sh exists since W-SIM/I24)
# Prefer `make -C tests` for the RTL-readiness-gated summary.

TOPLEVEL_LANG = verilog
SIM ?= vcs

RTL_DIR := $(abspath $(CURDIR)/../../fpga/shell/ip/clkrst)

VERILOG_SOURCES = $(RTL_DIR)/dut_clkrst.sv
COMPILE_ARGS += -sverilog

TOPLEVEL = dut_clkrst
MODULE   = test_clkrst

# SVA + coverage (A5): bind the reusable AXI4-Lite protocol checker onto this
# slave, and honour COVERAGE=1. See tests/common/bench_common.mk.
SVA_MODULES    := axi4lite_protocol_checker.sv
SVA_BIND_FILES := bind_clkrst.sv
include $(abspath $(CURDIR)/../common)/bench_common.mk

include $(shell cocotb-config --makefiles)/Makefile.sim
