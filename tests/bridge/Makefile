# Cocotb Makefile for eth_bridge_3port verification (mps3-nanosoc-platform).
# See dut_notes.md for port list/stub status; tests/README.md for strategy.
#
# Usage: source <repo-root>/set_env.sh; make  (set_env.sh exists since W-SIM/I24)
# Prefer `make -C tests` for the RTL-readiness-gated summary.

TOPLEVEL_LANG = verilog
SIM ?= vcs

RTL_DIR := $(abspath $(CURDIR)/../../fpga/ethernet/bridge)

VERILOG_SOURCES = $(RTL_DIR)/eth_bridge_3port.sv
COMPILE_ARGS += -sverilog

TOPLEVEL = eth_bridge_3port
MODULE   = test_bridge

include $(shell cocotb-config --makefiles)/Makefile.sim
