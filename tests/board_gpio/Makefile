# Cocotb Makefile for board_gpio verification (mps3-nanosoc-platform).
# fpga/shell/ip/board_gpio/board_gpio.sv is REAL since b3e8e99 (G3); the
# bench went green under VCS in the W-SIM pass (4/4). NOTE: list_benches.py
# mis-reported this block as SKIP until W-SIM — the RTL header quoted the
# "stub marker" literal in a comment saying the marker was absent, tripping
# dut_presence.rtl_ready()'s substring check (fixed comment-side).
#
# Usage: source <repo-root>/set_env.sh; make  (set_env.sh exists since W-SIM/I24)

TOPLEVEL_LANG = verilog
SIM ?= vcs

RTL_DIR := $(abspath $(CURDIR)/../../fpga/shell/ip/board_gpio)

VERILOG_SOURCES = $(RTL_DIR)/board_gpio.sv
COMPILE_ARGS += -sverilog

TOPLEVEL = board_gpio
MODULE   = test_board_gpio

# SVA + coverage (A5): bind the reusable AXI4-Lite protocol checker onto this
# slave, and honour COVERAGE=1. See tests/common/bench_common.mk.
SVA_MODULES    := axi4lite_protocol_checker.sv
SVA_BIND_FILES := bind_board_gpio.sv
include $(abspath $(CURDIR)/../common)/bench_common.mk

include $(shell cocotb-config --makefiles)/Makefile.sim
