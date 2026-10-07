# Makefile — TRUE product boot-from-flash co-sim (ASIC-honest, no RAM_PRELOAD
# cheat for the application).
#
# Full `nanosoc` SoC with an EMPTY (all-zero preloaded) IMEM + the real
# SST26VF064B flash VIP backdoored with the FULL packed flash image (boot table
# + HOT image + COLD segment). The reworked stage-0 bootrom copies the HOT set
# out of flash into IMEM, CRC-verifies, REMAPs and jumps; the COLD half executes
# in place from flash.
#
# = tests/micropython_xip_boot (full-SoC filelist + UART decode + SST26 VIP)
#   BUT: IMEM empty, full packed flash, and a REGENERATED bootrom carrying the
#   reworked flash-boot stage-0 (built here into build/, swapped into the
#   generated filelist so the shared SoC tree is left untouched).
#
# Run:  source ../../set_env.sh; make              # GATE 1 (boot from flash)
#       source ../../set_env.sh; make UPY_GATE2=1  # + GATE 2 (print(1+1)->2)
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

PYTHON        ?= python3
# arm-none-eabi-* bin dir; empty => taken from PATH (tools.env may set it).
TOOLCHAIN_BIN ?= $(ARM_GCC_BIN)

UPY_DIR   := $(REPO_ROOT)/firmware/micropython
FLASH_HEX := $(UPY_DIR)/build/xip/flash_image_byte.hex

# Geometry: IMEM sized to hold the 40 KB hot set once copied (64 KB); DMEM 64 KB.
IMEM_RAM_ADDR_W ?= 16
DMEM_RAM_ADDR_W ?= 16

# --- Reworked stage-0 bootrom, built into THIS bench's build/ (shared SoC tree
# left untouched). The sim's BOOTROM is a case-ROM generated from stage0.hex, so
# to run the flash-boot stage-0 in sim we recompile + regenerate bootrom.sv here
# and swap it into the filelist below.
STAGE0_SRC := $(SOCLABS_NANOSOC_SOC_DIR)/firmware/bootloader/stage0/stage0_bootloader.c
SHARED_BOOTROM := $(FPGA_BOOTROM_DIR)/bootrom.sv
LOCAL_BOOTROM  := $(CURDIR)/build/bootrom.sv

$(LOCAL_BOOTROM): $(CURDIR)/tools/build_bootrom.sh $(STAGE0_SRC)
	PYTHON=$(PYTHON) TOOLCHAIN_BIN=$(TOOLCHAIN_BIN) \
	SOCLABS_NANOSOC_SOC_DIR=$(SOCLABS_NANOSOC_SOC_DIR) \
	bash $(CURDIR)/tools/build_bootrom.sh $(CURDIR)/build

# --- The packed flash image (built by firmware/micropython `make xip`). Needed
# at sim RUNTIME ($readmemh at time 0), so make the filelist depend on it.
$(FLASH_HEX):
	$(MAKE) -C $(UPY_DIR) xip

# Reuse micropython_boot's Vivado-TCL -> VCS filelist collector (symlinked), then
# swap the shared bootrom.sv for our locally-regenerated flash-boot bootrom.
NANOSOC_F := $(CURDIR)/nanosoc.f
$(NANOSOC_F): $(CURDIR)/collect_filelist.tcl $(SOCLABS_NANOSOC_SOC_DIR)/pynq/filelist.tcl $(LOCAL_BOOTROM) $(FLASH_HEX)
	tclsh $(CURDIR)/collect_filelist.tcl > $@.tmp
	sed -i 's|$(SHARED_BOOTROM)|$(LOCAL_BOOTROM)|' $@.tmp
	@grep -q '$(LOCAL_BOOTROM)' $@.tmp || { echo "ERROR: bootrom.sv swap failed (shared path not found in filelist)"; exit 1; }
	mv $@.tmp $@

# Make the generated+swapped filelist a compile prerequisite so cocotb builds
# it (and the local bootrom + packed flash) before elaboration.
CUSTOM_COMPILE_DEPS += $(NANOSOC_F)

VERILOG_SOURCES = $(CURDIR)/micropython_flash_boot_tb.sv
VERILOG_SOURCES += $(SOCLABS_AHB_QSPI_DIR)/verif/VIP/SST26VF064B.v

ifeq ($(SIM),vcs)
COMPILE_ARGS += -sverilog
COMPILE_ARGS += -timescale=1ns/1ps
COMPILE_ARGS += -f $(NANOSOC_F)
# IMEM = writable preload BRAM (sl_fpga_rom_word), preloaded ALL-ZERO -> empty.
COMPILE_ARGS += +define+RAM_PRELOAD
# NOTE: string params (IMEM_ZERO_IMG, FLASH_IMG) are built in the tb from the
# MPFB_REPO_ROOT macro -- VCS -pvalue SILENTLY IGNORES string params. Only
# integer geometry via -pvalue.
COMPILE_ARGS += +define+MPFB_REPO_ROOT=\"$(REPO_ROOT)\"
COMPILE_ARGS += -pvalue+micropython_flash_boot_tb/IMEM_RAM_ADDR_W=$(IMEM_RAM_ADDR_W)
COMPILE_ARGS += -pvalue+micropython_flash_boot_tb/DMEM_RAM_ADDR_W=$(DMEM_RAM_ADDR_W)
endif

# Test knobs.
UPY_MAX_MS      ?= 1500
UPY_PROGRESS_MS ?= 2
UPY_GATE2       ?= 0
export UPY_MAX_MS
export UPY_PROGRESS_MS
export UPY_GATE2

TOPLEVEL = micropython_flash_boot_tb
MODULE   = test_micropython_flash_boot

# The generated-filelist rule is the first real target; keep cocotb's `all` the
# default goal so `make` runs the sim, not just the filelist.
.DEFAULT_GOAL := all

include $(abspath $(CURDIR)/../common)/bench_common.mk
include $(shell cocotb-config --makefiles)/Makefile.sim
