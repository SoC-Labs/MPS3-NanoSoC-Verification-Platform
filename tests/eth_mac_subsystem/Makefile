# Cocotb Makefile for the eth_mac_test_subsystem integration bench
# (mps3-nanosoc-platform). See dut_notes.md for the datapath wiring + the
# interface reconciliations this bench exercises; tests/README.md for the
# overall verification strategy.
#
# This bench elaborates the WHOLE §8 subsystem: all five ethernet blocks plus
# the integration module (fpga/ethernet/eth_mac_test_subsystem.sv). Same
# multi-file pattern as tests/link_partner_mac, extended to the full set, with
# the +incdir mdio_phy_model needs for its two `include'd helper modules.
#
# Usage: source <repo-root>/set_env.sh; make
# Prefer `make -C tests BLOCK=eth_mac_subsystem run-one` for the readiness-gated
# summary.

TOPLEVEL_LANG = verilog
SIM ?= vcs

RTL_DIR := $(abspath $(CURDIR)/../../fpga/ethernet)

# mdio_phy_model.sv `include's mdio_slave.sv + phy_reg_model.sv (kept as
# separate lint-able files) — list ONLY the top and add its dir to +incdir,
# never the two helpers (double-definition otherwise). All other blocks are
# plain single-file sources.
VERILOG_SOURCES = \
  $(RTL_DIR)/rmii_phy_if/rmii_phy_if.sv \
  $(RTL_DIR)/link_partner_mac/link_partner_mac.sv \
  $(RTL_DIR)/bridge/eth_bridge_3port.sv \
  $(RTL_DIR)/gen_checker/gen_checker.sv \
  $(RTL_DIR)/mdio_phy_model/mdio_phy_model.sv \
  $(RTL_DIR)/eth_mac_test_subsystem.sv

COMPILE_ARGS += -sverilog
COMPILE_ARGS += +incdir+$(RTL_DIR)/mdio_phy_model

# mdio_phy_model.sv `include`s mdio_slave.sv + phy_reg_model.sv (listed only via
# +incdir, never as sources). cocotb tracks ONLY $(VERILOG_SOURCES) for the
# compile, so name the two helpers here or an incremental build (check-sim does
# not clean) would reuse a stale simv after a helper changes — the off-by-one
# MDIO read-data escape of 2026-07-11.
CUSTOM_COMPILE_DEPS += $(RTL_DIR)/mdio_phy_model/mdio_slave.sv \
                       $(RTL_DIR)/mdio_phy_model/phy_reg_model.sv

TOPLEVEL = eth_mac_test_subsystem
MODULE   = test_eth_mac_subsystem

include $(shell cocotb-config --makefiles)/Makefile.sim
