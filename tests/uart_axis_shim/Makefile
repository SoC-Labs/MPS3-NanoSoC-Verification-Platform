# Cocotb Makefile for uart_axis_shim verification (mps3-nanosoc-platform).
# NEW leaf bench (A5 verification-confidence pass): fpga/rp/nanosoc/
# uart_axis_shim.sv is real sequential UART<->AXIS logic that had ZERO
# verification. See dut_notes.md for the port list and tests/README.md for
# the overall strategy.
#
# Usage: source <repo-root>/set_env.sh; make
#        (or `make -C tests BLOCK=uart_axis_shim run-one`)

TOPLEVEL_LANG = verilog
SIM ?= vcs

RTL_DIR := $(abspath $(CURDIR)/../../fpga/rp/nanosoc)

VERILOG_SOURCES = $(RTL_DIR)/uart_axis_shim.sv

# RTL is SystemVerilog (`logic`, `always_ff`, typedef enum) -- VCS needs
# -sverilog. VCS-only flag (Questa parses *.sv by extension; see README).
ifeq ($(SIM),vcs)
COMPILE_ARGS += -sverilog
# Shrink the baud divisor for a fast sim: CLK_HZ/BAUD = 800000/100000 = 8
# clk cycles per bit (matches DIV in test_uart_axis_shim.py). The real
# 25 MHz/115200 default (DIV=217) would make every byte ~2170 cycles.
COMPILE_ARGS += -pvalue+uart_axis_shim/CLK_HZ=800000 -pvalue+uart_axis_shim/BAUD=100000
endif

TOPLEVEL = uart_axis_shim
MODULE   = test_uart_axis_shim

# Coverage toggle (no SVA bind — this block is not an AXI4-Lite slave).
include $(abspath $(CURDIR)/../common)/bench_common.mk

include $(shell cocotb-config --makefiles)/Makefile.sim
