# Cocotb Makefile for rmii_phy_if verification (mps3-nanosoc-platform).
# See dut_notes.md for port list/stub status; tests/README.md for strategy.
#
# Usage: source <repo-root>/set_env.sh; make  (set_env.sh exists since W-SIM/I24)
# Prefer `make -C tests` for the RTL-readiness-gated summary.

TOPLEVEL_LANG = verilog
SIM ?= vcs

RTL_DIR := $(abspath $(CURDIR)/../../fpga/ethernet/rmii_phy_if)

VERILOG_SOURCES = $(RTL_DIR)/rmii_phy_if.sv
COMPILE_ARGS += -sverilog

TOPLEVEL = rmii_phy_if
MODULE   = test_rmii_phy_if

include $(shell cocotb-config --makefiles)/Makefile.sim
