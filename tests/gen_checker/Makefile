# Cocotb Makefile for gen_checker verification (mps3-nanosoc-platform).
# See dut_notes.md for port list/stub status; tests/README.md for strategy.
#
# Usage: source <repo-root>/set_env.sh; make  (set_env.sh exists since W-SIM/I24)
# Prefer `make -C tests` for the RTL-readiness-gated summary.

TOPLEVEL_LANG = verilog
SIM ?= vcs

RTL_DIR := $(abspath $(CURDIR)/../../fpga/ethernet/gen_checker)

VERILOG_SOURCES = $(RTL_DIR)/gen_checker.sv
COMPILE_ARGS += -sverilog

TOPLEVEL = gen_checker
MODULE   = test_gen_checker

# SVA + coverage (A5): bind the reusable AXI4-Lite protocol checker onto this
# slave (accept-on-valid handshake style), and honour COVERAGE=1. See
# tests/common/bench_common.mk.
SVA_MODULES    := axi4lite_protocol_checker.sv
SVA_BIND_FILES := bind_gen_checker.sv
include $(abspath $(CURDIR)/../common)/bench_common.mk

include $(shell cocotb-config --makefiles)/Makefile.sim
