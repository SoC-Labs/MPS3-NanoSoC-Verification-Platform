# Cocotb Makefile for uart_bridge verification (mps3-nanosoc-platform).
# See dut_notes.md for the DUT's port list and the multi-file build note,
# and tests/README.md for the overall verification strategy.
#
# Usage:
#   source <repo-root>/set_env.sh   # pins miniconda python3.10 + cocotb
#                                   # 2.0.1 + VCS 2022.06-SP2 + license env
#   make                            # run this block's bench directly
#
# Prefer `make -C tests BLOCK=uart_bridge run-one` (top-level Makefile) for
# routine use: it checks RTL readiness first (tests/common/list_benches.py
# gates on ALL THREE RTL files below being present).

TOPLEVEL_LANG = verilog
SIM ?= vcs

RTL_DIR := $(abspath $(CURDIR)/../../fpga/shell/ip/uart_bridge)

# uart_bridge.sv `include`s uartbr_async_fifo.sv + swo_uart_rx.sv (kept as
# separate lint-able files), so ONLY the top goes in VERILOG_SOURCES and
# VCS gets the RTL dir on its include search path — same pattern as
# tests/mdio_phy_model/Makefile. (+incdir must be absolute: the cocotb VCS
# makefile compiles from inside $(SIM_BUILD).)
VERILOG_SOURCES = $(RTL_DIR)/uart_bridge.sv

# RTL is SystemVerilog (`logic`, `always_ff`) -- VCS needs -sverilog.
# VCS-only flag: Questa's vlog rejects it (vlog-1902) and already parses
# *.sv as SystemVerilog by extension (see tests/README.md "Sim target").
ifeq ($(SIM),vcs)
COMPILE_ARGS += -sverilog
endif
COMPILE_ARGS += +incdir+$(RTL_DIR)

# Leaf module (both clocks are inputs) -- cocotb attaches directly.
TOPLEVEL = uart_bridge
MODULE   = test_uart_bridge

# SVA + coverage (A5): bind the AXI4-Lite protocol checker onto the slave AND
# the CDC gray-pointer checker onto all five uartbr_async_fifo instances, and
# honour COVERAGE=1. See tests/common/bench_common.mk and bind_uart_bridge.sv.
SVA_MODULES    := axi4lite_protocol_checker.sv cdc_gray_checker.sv
SVA_BIND_FILES := bind_uart_bridge.sv
include $(abspath $(CURDIR)/../common)/bench_common.mk

include $(shell cocotb-config --makefiles)/Makefile.sim
