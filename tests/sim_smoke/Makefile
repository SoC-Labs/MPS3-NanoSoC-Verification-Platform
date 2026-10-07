# Cocotb Makefile for the sim_smoke harness sanity bench (W-SIM, I24).
# The DUT (counter.sv) lives in THIS directory — no fpga/ dependency at all;
# a red run here means the simulator/cocotb/license environment is broken,
# not any RTL. Usage: source <repo-root>/set_env.sh; make
# (or `make -C tests BLOCK=sim_smoke run-one` from the repo root).

TOPLEVEL_LANG = verilog
SIM ?= vcs

VERILOG_SOURCES = $(abspath $(CURDIR))/counter.sv
# -sverilog is a VCS-only flag (Questa's vlog rejects it with vlog-1902 and
# parses *.sv as SystemVerilog by extension anyway) — found 2026-07-06 while
# proving the SIM=questa fallback, see tests/README.md "Sim target".
ifeq ($(SIM),vcs)
COMPILE_ARGS += -sverilog
endif

TOPLEVEL = smoke_counter
MODULE   = test_sim_smoke

include $(shell cocotb-config --makefiles)/Makefile.sim
