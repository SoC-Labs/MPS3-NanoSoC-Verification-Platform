# Cocotb Makefile for link_partner_mac verification (mps3-nanosoc-platform).
# See dut_notes.md for the pairing rationale; tests/README.md for strategy.
#
# Unlike the other ethernet benches this one elaborates TWO RTL blocks plus a
# bench-local wrapper (tb_link_partner_pair.sv): link_partner_mac is only
# meaningful against rmii_phy_if's MII pacing contract, so the bench tests
# the pair end-to-end (RMII pins <-> AXI-Stream).
#
# Usage: source <repo-root>/set_env.sh; make
# Prefer `make -C tests` for the RTL-readiness-gated summary.

TOPLEVEL_LANG = verilog
SIM ?= vcs

RTL_DIR := $(abspath $(CURDIR)/../../fpga/ethernet)

VERILOG_SOURCES = \
  $(RTL_DIR)/rmii_phy_if/rmii_phy_if.sv \
  $(RTL_DIR)/link_partner_mac/link_partner_mac.sv \
  $(CURDIR)/tb_link_partner_pair.sv
COMPILE_ARGS += -sverilog

TOPLEVEL = tb_link_partner_pair
MODULE   = test_link_partner_mac

include $(shell cocotb-config --makefiles)/Makefile.sim
