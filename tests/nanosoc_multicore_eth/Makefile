#-----------------------------------------------------------------------------
# tests/nanosoc_multicore_eth/Makefile — LAYER-B integrated ethernet bench.
#
# A joint work commissioned on behalf of SoC Labs, under Arm Academic Access
# license.  Copyright (C) 2026, SoC Labs (www.soclabs.org)
#-----------------------------------------------------------------------------
# Elaborates the REAL multicore-ethernet nanoSoC RM (fpga/rp/nanosoc_multicore/
# rp_nanosoc_multicore_wrapper.sv, which pulls in the whole DUT SoC via the
# DUT's own proven read_verilog recipe / VCS flist) and wires its RMII + MDIO +
# console partition pins ACROSS a modelled DFX boundary to the shell's virtual
# PHY (fpga/ethernet/{rmii_phy_if,mdio_phy_model}) + link_partner_mac.
#
# Mirrors cocotb/soc_ethernet's Makefile (the proven SoC-MAC bring-up flow):
#   * the DUT VCS flist ($NANOSOC_MULTICORE_HOME/flist/...), pre-expanded;
#   * +define+RAM_PRELOAD (CPU IMEM $readmemh preload) + ETH_WISHBONE_B3;
#   * hello_uart byte-hex -> word-hex conversion into sim_build/image.hex;
#   * the smoke_remap bootrom variant reinstalled into the shared stub;
#   * COCOTB_RESOLVE_X=ZEROS (the WB slave drives X on read-data during writes).
# ...and ADDS the mps3-shell ethernet models + the RM wrapper + uart_axis_shim.
#
# Usage:
#   source $NANOSOC_MULTICORE_HOME/set_env.sh   # DUT env (flist + read-only IP)
#   source <mps3-repo-root>/set_env.sh          # python3.10 + cocotb 2.0.1 + VCS
#   make SIM=vcs
#-----------------------------------------------------------------------------

SIM           ?= vcs
TOPLEVEL_LANG  = verilog

ifeq ($(strip $(NANOSOC_MULTICORE_HOME)),)
$(error NANOSOC_MULTICORE_HOME is unset -- source the DUT set_env.sh \
  ($$NANOSOC_MULTICORE_HOME/set_env.sh) before this bench: it provides the SoC \
  flist and the ~14 read-only IP env vars the DUT fileset resolves against)
endif

export NANOSOC_MULTICORE_HOME
export CMSDK_DIR ?= $(ARM_IP_LIBRARY_PATH)/Corstone-101/BP210-r1p1-00rel0/BP210-BU-00000-r1p1-00rel0

MPS3_ROOT := $(abspath $(CURDIR)/../..)
ETH_DIR   := $(MPS3_ROOT)/fpga/ethernet
RP_DIR    := $(MPS3_ROOT)/fpga/rp

# --- extra (mps3-repo) sources layered on top of the DUT SoC flist ----------
#   uart_axis_shim + the RM wrapper are NOT in the DUT flist (they are the DFX
#   RP side); the shell ethernet models are the shell side.  mdio_phy_model
#   `include's mdio_slave.sv + phy_reg_model.sv -> +incdir, list only the top.
VERILOG_SOURCES = \
  $(RP_DIR)/nanosoc/uart_axis_shim.sv \
  $(RP_DIR)/nanosoc_multicore/rp_nanosoc_multicore_wrapper.sv \
  $(ETH_DIR)/rmii_phy_if/rmii_phy_if.sv \
  $(ETH_DIR)/link_partner_mac/link_partner_mac.sv \
  $(ETH_DIR)/mdio_phy_model/mdio_phy_model.sv \
  $(CURDIR)/tb_nanosoc_multicore_eth.sv

TOPLEVEL = tb_nanosoc_multicore_eth
MODULE   = test_nanosoc_multicore_eth

# --- firmware image (CPU0 console) ------------------------------------------
CMAKE_PRESET ?= gcc-m0plus-le
FIRMWARE_HEX ?= $(NANOSOC_MULTICORE_HOME)/build/cmake/$(CMAKE_PRESET)/firmware/apps/hello_uart/hello_uart.hex
export FIRMWARE_HEX

# eth-scratch sentinel sizes -- exported so the Python test tracks the RTL SRAM
# wrap size (same derivation as cocotb/soc_ethernet).
ETH_SCRATCH_RX_ADDR_W ?= $(shell grep -m1 'ETH_SCRATCH_RX_ADDR_W:' $(NANOSOC_MULTICORE_HOME)/sys_desc/nanosoc_multicore_soc.yaml | sed -n 's/.*default:[[:space:]]*\([0-9][0-9]*\).*/\1/p')
ETH_SCRATCH_RX_ADDR_W := $(if $(ETH_SCRATCH_RX_ADDR_W),$(ETH_SCRATCH_RX_ADDR_W),14)
export ETH_SCRATCH_RX_ADDR_W

# --- pre-expanded flist (resolves $(VAR)->${VAR} + nested -f includes) -------
EXPANDED_FLIST = $(CURDIR)/sim_build/nanosoc_multicore.expanded.flist

COMPILE_ARGS += -full64 -sverilog -timescale=1ns/1ps -debug_access+all -kdb
COMPILE_ARGS += -f $(EXPANDED_FLIST)
COMPILE_ARGS += +incdir+$(ETH_DIR)/mdio_phy_model
# mdio_phy_model.sv `include`s these two helpers (never listed as sources);
# cocotb tracks only $(VERILOG_SOURCES), so name them here or an incremental
# build would reuse a stale simv after a helper changes (off-by-one MDIO escape,
# 2026-07-11).
CUSTOM_COMPILE_DEPS += $(ETH_DIR)/mdio_phy_model/mdio_slave.sv \
                       $(ETH_DIR)/mdio_phy_model/phy_reg_model.sv
COMPILE_ARGS += +define+RAM_PRELOAD
COMPILE_ARGS += +define+ETH_WISHBONE_B3

ifdef WAVES
  COMPILE_ARGS += +define+WAVES
  SIM_ARGS     += +vcs+dumpvars+waves.vcd
endif
SIM_ARGS     += +CODEFILENAME=$(FIRMWARE_HEX)

# The OpenCores MAC WB slave drives read-data with X during a write's data
# phase; the AHB<->WB bridge forwards it on sHRDATA.  cocotbext-ahb logs hrdata
# at every transaction end, so resolve X->0 for the BFM's Python view only.
export COCOTB_RESOLVE_X ?= ZEROS

include $(shell cocotb-config --makefiles)/Makefile.sim

# --- expanded flist rule (after the cocotb include so `sim` stays default) ---
$(EXPANDED_FLIST): $(NANOSOC_MULTICORE_HOME)/flist/nanosoc_multicore.flist
	@mkdir -p $(dir $@)
	$(NANOSOC_MULTICORE_HOME)/scripts/expand_flist.sh $< $@

# --- byte-hex -> word-hex firmware conversion (sl_fpga_rom_word $readmemh) ---
HEX_CONVERTER = $(NANOSOC_MULTICORE_HOME)/ethernet-subsystem-ahb/fpga/scripts/hex_byte_to_word.py
.PHONY: convert_firmware_hex
convert_firmware_hex: $(EXPANDED_FLIST)
	@mkdir -p $(SIM_BUILD)
	@test -f "$(FIRMWARE_HEX)" || { echo "ERROR: firmware hex not found: $(FIRMWARE_HEX)"; exit 1; }
	python3 $(HEX_CONVERTER) "$(FIRMWARE_HEX)" $(SIM_BUILD)/image.hex

# --- restore the smoke_remap bootrom variant into the shared stub -----------
.PHONY: install_bootrom
install_bootrom:
	$(NANOSOC_MULTICORE_HOME)/scripts/install_bootrom.sh smoke_remap $(CMAKE_PRESET)

# --- wire flist + firmware + bootrom into the cocotb compile dependency ------
$(SIM_BUILD)/simv: $(EXPANDED_FLIST) convert_firmware_hex install_bootrom
