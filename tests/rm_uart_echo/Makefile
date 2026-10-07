# Cocotb Makefile for rm_uart_echo verification (mps3-nanosoc-platform).
#
# rm_uart_echo (fpga/dfx/rms/rm_uart_echo/rm_uart_echo.sv) is the first DFX
# Reconfigurable Module that moves DATA across the RP boundary: it drives the
# partition-pins.md "Console / trace" AXI-Stream byte pair (banner + echo +
# backpressure). Before this bench the fpga/dfx/rms/ RMs had lint-only coverage.
#
# Usage:
#   source <repo-root>/set_env.sh   # pins miniconda python3.10 + cocotb 2.0.1
#                                   # + VCS 2022.06-SP2 + license env
#   make                            # run this block's bench directly
#
# Prefer `make -C tests BLOCK=rm_uart_echo run-one` (top-level Makefile) for
# routine use: it checks RTL readiness first (tests/common/list_benches.py).

TOPLEVEL_LANG = verilog
SIM ?= vcs

RTL_DIR := $(abspath $(CURDIR)/../../fpga/dfx/rms/rm_uart_echo)

# Single, dependency-free RM source (matches the DFX flow's single-file OOC
# synth of this RM).
VERILOG_SOURCES = $(RTL_DIR)/rm_uart_echo.sv

# RTL is SystemVerilog (`logic`, `always_ff`) -- VCS needs -sverilog.
ifeq ($(SIM),vcs)
COMPILE_ARGS += -sverilog
endif

# Leaf module (dut_clk is an input) -- cocotb attaches directly.
TOPLEVEL = rm_uart_echo
MODULE   = test_rm_uart_echo

# No SVA binds for this RM (no reusable checker maps onto its ports); still pull
# in bench_common.mk for the COVERAGE=1 toggle (its SVA wiring is a no-op when
# SVA_MODULES/SVA_BIND_FILES are empty).
include $(abspath $(CURDIR)/../common)/bench_common.mk

include $(shell cocotb-config --makefiles)/Makefile.sim
