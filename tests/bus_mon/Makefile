# Cocotb bench for fpga/bus_mon/ahb_mon.sv -- the passive AHB-Lite observatory.
#
# Usage: source <repo-root>/set_env.sh; make
#
# WHAT THIS BENCH IS FOR. A protocol checker that never fires is indistinguishable
# from a protocol checker that is wired up wrong, and the second one is worse than
# having none -- it reports "clean" on a broken bus. So for every violation code
# in ahb_mon.sv there is a test that PROVOKES it and asserts the bit sets, plus a
# clean-traffic test asserting the bitmap stays zero. Same discipline as the
# negative controls in tests/identify_iice.

TOPLEVEL_LANG = verilog
SIM ?= vcs

RTL_DIR := $(abspath $(CURDIR)/../../fpga/bus_mon)

VERILOG_SOURCES = $(RTL_DIR)/ahb_mon.sv
COMPILE_ARGS += -sverilog

TOPLEVEL = ahb_mon
MODULE   = test_ahb_mon

include $(shell cocotb-config --makefiles)/Makefile.sim
