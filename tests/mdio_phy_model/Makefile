# Cocotb Makefile for mdio_phy_model verification (mps3-nanosoc-platform).
# See dut_notes.md for the DUT's port list + current stub status, and
# tests/README.md for the overall verification strategy.
#
# Usage:
#   source <repo-root>/set_env.sh   # exists since the W-SIM pass (I24):
#                                    # pins miniconda python3.10 + cocotb
#                                    # 2.0.1 + VCS 2022.06-SP2 + license env.
#   make                             # run this block's bench directly
#   make TESTCASE=test_bmcr_bmsr_phyid_anar_anlpar_defaults
#
# Prefer `make -C tests` (the top-level Makefile) for routine use: it checks
# RTL readiness first (tests/common/list_benches.py) and prints a clean
# [SKIP] instead of a raw simulator/compile error when the block isn't ready.

TOPLEVEL_LANG = verilog
SIM ?= vcs

RTL_DIR := $(abspath $(CURDIR)/../../fpga/ethernet/mdio_phy_model)

VERILOG_SOURCES = $(RTL_DIR)/mdio_phy_model.sv

# RTL is SystemVerilog (`logic`, `always_ff`) -- VCS needs -sverilog for .sv
# sources using those constructs.
COMPILE_ARGS += -sverilog
# mdio_phy_model.sv `include`s mdio_slave.sv + phy_reg_model.sv (kept as
# separate lint-able files); VCS needs the RTL dir on its include search path.
COMPILE_ARGS += +incdir+$(RTL_DIR)

# cocotb's Makefile.sim tracks ONLY $(VERILOG_SOURCES) as compile prerequisites.
# The two `include`d helpers above are not (and must not be) listed as sources,
# so without naming them here an incremental build (`make check` / check-sim,
# which does NOT clean) would silently reuse a stale simv after a helper changes
# — the off-by-one MDIO read-data escape of 2026-07-11 (mdio_slave.sv fix vs a
# stale simv read every register back as value>>1). CUSTOM_COMPILE_DEPS re-arms
# the VCS/Questa/etc. recompile (Makefile.vcs: simv: $(VERILOG_SOURCES) $(CUSTOM_COMPILE_DEPS)).
CUSTOM_COMPILE_DEPS += $(RTL_DIR)/mdio_slave.sv $(RTL_DIR)/phy_reg_model.sv

# No separate testbench wrapper: mdio_phy_model is a leaf module with plain
# I/O (no internal clock generation to fake up), so cocotb attaches directly
# to it as TOPLEVEL.
TOPLEVEL = mdio_phy_model
MODULE   = test_mdio_phy_model

# SVA + coverage (A5): bind the reusable AXI4-Lite protocol checker onto the
# VPHY slave (accept-on-valid handshake style), and honour COVERAGE=1. See
# tests/common/bench_common.mk.
SVA_MODULES    := axi4lite_protocol_checker.sv
SVA_BIND_FILES := bind_mdio_phy_model.sv
include $(abspath $(CURDIR)/../common)/bench_common.mk

include $(shell cocotb-config --makefiles)/Makefile.sim
