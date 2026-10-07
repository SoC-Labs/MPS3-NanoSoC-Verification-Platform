# Makefile — hybrid-XiP MicroPython boot co-sim (product-config proof).
#
# Full `nanosoc` SoC (hot set RAM_PRELOADed into a SMALL IMEM) + the real
# SST26VF064B flash VIP holding the cold segment. Boots to a REPL with cold code
# (parser/compiler/REPL) executing IN PLACE from flash through the CG092 cache.
#
# = tests/micropython_boot (full-SoC filelist + UART decode) + the SST26 VIP
#   from tests/qspi_xip. Reuses micropython_boot's collect_filelist.tcl.
#
# Run:  source ../../set_env.sh; make              # GATE 1 (banner)
#       source ../../set_env.sh; make UPY_GATE2=1  # + GATE 2 (print(1+1)->2, the
#                                                  #   real cold-execute proof)
TOPLEVEL_LANG = verilog
SIM ?= vcs

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

UPY_DIR := $(REPO_ROOT)/firmware/micropython/build

# The hybrid build's outputs + geometry. These MUST match the firmware manifest
# (firmware/micropython/build/xip_manifest.txt); overridable on the command line
# for characterisation sweeps (e.g. `make IMEM_RAM_ADDR_W=14` to test 16 KB).
HOT_IMEM_IMG   ?= $(UPY_DIR)/xip/micropython_hot_word.hex
COLD_FLASH_IMG ?= $(UPY_DIR)/xip/firmware_cold_byte.hex
COLD_FLASH_OFF ?= 0                 # cold linked at aperture 0x70000000 exactly
IMEM_RAM_ADDR_W ?= 16               # 64 KB — hot .text is 40 KB (32 KB would overflow)
DMEM_RAM_ADDR_W ?= 16               # 64 KB

# Reuse micropython_boot's Vivado-TCL -> VCS filelist collector (symlinked).
NANOSOC_F := $(CURDIR)/nanosoc.f
$(NANOSOC_F): $(CURDIR)/collect_filelist.tcl $(SOCLABS_NANOSOC_SOC_DIR)/pynq/filelist.tcl
	tclsh $(CURDIR)/collect_filelist.tcl > $@.tmp && mv $@.tmp $@

VERILOG_SOURCES = $(CURDIR)/micropython_xip_boot_tb.sv
# The real Microchip/SST SST26VF064B behavioural model (same VIP as qspi_xip).
VERILOG_SOURCES += $(SOCLABS_AHB_QSPI_DIR)/verif/VIP/SST26VF064B.v

ifeq ($(SIM),vcs)
COMPILE_ARGS += -sverilog
COMPILE_ARGS += -timescale=1ns/1ps
COMPILE_ARGS += -f $(NANOSOC_F)
# RAM_PRELOAD: IMEM is the $readmemh ROM variant (hot set baked at elaboration).
COMPILE_ARGS += +define+RAM_PRELOAD
COMPILE_ARGS += -pvalue+micropython_xip_boot_tb/HOT_IMEM_IMG=\"$(HOT_IMEM_IMG)\"
COMPILE_ARGS += -pvalue+micropython_xip_boot_tb/COLD_FLASH_IMG=\"$(COLD_FLASH_IMG)\"
COMPILE_ARGS += -pvalue+micropython_xip_boot_tb/COLD_FLASH_OFF=$(COLD_FLASH_OFF)
COMPILE_ARGS += -pvalue+micropython_xip_boot_tb/IMEM_RAM_ADDR_W=$(IMEM_RAM_ADDR_W)
COMPILE_ARGS += -pvalue+micropython_xip_boot_tb/DMEM_RAM_ADDR_W=$(DMEM_RAM_ADDR_W)
endif

# Test knobs (see micropython_boot for the empty-export hazard these avoid).
UPY_MAX_MS      ?= 800
UPY_PROGRESS_MS ?= 2
UPY_GATE2       ?= 0
export UPY_MAX_MS
export UPY_PROGRESS_MS
export UPY_GATE2

TOPLEVEL = micropython_xip_boot_tb
MODULE   = test_micropython_xip_boot

# The generated-filelist rule ($(NANOSOC_F)) above is the first target in this
# file, which would otherwise hijack the default goal (so `make` builds only
# nanosoc.f and stops). cocotb's Makefile.sim provides `all`.
.DEFAULT_GOAL := all

include $(abspath $(CURDIR)/../common)/bench_common.mk
include $(shell cocotb-config --makefiles)/Makefile.sim
