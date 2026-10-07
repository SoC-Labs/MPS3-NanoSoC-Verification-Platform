# Cocotb Makefile — full-SoC MicroPython boot bench (tests/micropython_boot).
#
# Boots the REAL `nanosoc` core with its IMEM preloaded from the built
# MicroPython word-hex, and watches UART2 TXD for the "MicroPython" banner.
# See test_micropython_boot.py and README.md.
#
# Usage:
#   source <repo-root>/set_env.sh        # miniconda py3.10 + cocotb 2.0.1 + VCS
#   make                                  # GATE 1 (banner); slow (see README)
#   make UPY_MAX_MS=1500                   # longer sim-time budget
#   make UPY_GATE2=1                       # also attempt the RX round-trip
#
# The nanosoc RTL fileset is NOT hand-listed here: collect_filelist.tcl sources
# the authoritative $SOCLABS_NANOSOC_SOC_DIR/pynq/filelist.tcl and emits an
# equivalent VCS -f file (nanosoc.f), so this bench compiles exactly the SoC the
# FPGA build packages.

TOPLEVEL_LANG = verilog
SIM ?= vcs

# --- Environment the nanosoc filelist.tcl needs (same as pynq/Makefile) ------
# Overridable, but these are the checkouts on this machine.
# --- site-local tree/tool locations, NOT tracked: see tools.env.example -------
# Same seam and precedence as fpga/dfx/Makefile: tools.env first, then these
# `?=` defaults (none names a workstation), an explicit `make VAR=...` wins.
REPO_ROOT ?= $(abspath $(CURDIR)/../..)
TOOLS_ENV ?= $(REPO_ROOT)/tools.env
-include $(TOOLS_ENV)
export SOCLABS_NANOSOC_SOC_DIR       ?=
export SOCLABS_NANOSOC_ARCH_TECH_DIR ?= $(SOCLABS_NANOSOC_SOC_DIR)/nanosoc_arch_tech
export SOCLABS_NANOSOC_GEN_DIR       ?= $(SOCLABS_NANOSOC_ARCH_TECH_DIR)/nanosoc_gen
export ARM_IP_LIBRARY_PATH           ?=
export FPGA_BOOTROM_DIR              ?= $(SOCLABS_NANOSOC_SOC_DIR)/imp/fpga/firmware/stage0
export SOCLABS_AHB_QSPI_DIR          ?=

# --- MicroPython image + memory sizing (must match the built image) ----------
UPY_IMAGE       ?= $(REPO_ROOT)/firmware/micropython/build/micropython_word.hex
IMEM_RAM_ADDR_W ?= 17
DMEM_RAM_ADDR_W ?= 16

# --- Generated VCS command file from the authoritative filelist.tcl ----------
NANOSOC_F := $(CURDIR)/nanosoc.f
$(NANOSOC_F): collect_filelist.tcl $(SOCLABS_NANOSOC_SOC_DIR)/pynq/filelist.tcl
	tclsh $(CURDIR)/collect_filelist.tcl > $@.tmp && mv $@.tmp $@
CUSTOM_COMPILE_DEPS += $(NANOSOC_F)

# --- Bench sources -----------------------------------------------------------
VERILOG_SOURCES = $(CURDIR)/micropython_boot_tb.sv

ifeq ($(SIM),vcs)
COMPILE_ARGS += -sverilog
COMPILE_ARGS += -timescale=1ns/1ps
COMPILE_ARGS += -f $(NANOSOC_F)
# Preload the built MicroPython image into IMEM, sized for it, at elaboration.
COMPILE_ARGS += -pvalue+micropython_boot_tb/IMEM_MEM_FPGA_IMG=\"$(UPY_IMAGE)\"
COMPILE_ARGS += -pvalue+micropython_boot_tb/IMEM_RAM_ADDR_W=$(IMEM_RAM_ADDR_W)
COMPILE_ARGS += -pvalue+micropython_boot_tb/DMEM_RAM_ADDR_W=$(DMEM_RAM_ADDR_W)
endif

# Pass the test knobs through to cocotb.
#
# These MUST be `?=` assigned before `export`. A bare `export UPY_MAX_MS`
# exports the variable as an EMPTY STRING when it is not set on the command
# line -- and the Python side does `os.environ.get("UPY_MAX_MS", "800")`, which
# returns "" (present-but-empty), not the default, so `float("")` throws at
# import before the sim runs. Give them real defaults here.
UPY_MAX_MS      ?= 800
UPY_PROGRESS_MS ?= 2
UPY_GATE2       ?= 0
export UPY_MAX_MS
export UPY_PROGRESS_MS
export UPY_GATE2

TOPLEVEL = micropython_boot_tb
MODULE   = test_micropython_boot

# The generated-filelist rule above is the first target in this file, which
# would otherwise hijack the default goal; cocotb's Makefile.sim provides `all`.
.DEFAULT_GOAL := all

include $(abspath $(CURDIR)/../common)/bench_common.mk

include $(shell cocotb-config --makefiles)/Makefile.sim
