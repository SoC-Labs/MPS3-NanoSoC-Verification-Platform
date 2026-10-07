SIM           ?= vcs
TOPLEVEL_LANG  = verilog
TOPLEVEL       = tb_lan8720_rmii_echo
MODULE         = test_lan8720_rmii_echo

# tests/lan8720_rmii_echo/Makefile -- cocotb bench: the DUT MAC RMII path
# against a REAL LAN8720 behavioral model (lan8720_model.sv).
# A joint work commissioned on behalf of SoC Labs, under Arm Academic Access
# license.  Copyright (C) 2026, SoC Labs (www.soclabs.org)
#
# DUT = ethmac_ahb_rmii (OpenCores ethmac + eth_miim + rmii_to_mii), read-only
# from the sibling ethernet-mac-ahb repo + ARM IP library. cocotb drives the AHB
# register slave (tests/common/ahb_lite.py); the DMA master is answered by the
# TB SV slave RAM; frames go in/out at the RMII pads through the LAN8720 model.
# VCS + cocotb 2.0.1 (repo proven combo).
#
#   source <repo-root>/set_env.sh
#   make -C tests/lan8720_rmii_echo           # default 100 Mbps run
#   make -C tests/lan8720_rmii_echo guard     # +MODE_SPEED=0 (RX-dies guard)
#
# Not in repo-root `make check` (needs the sibling checkout + VCS), like
# tests/lan8720_sut and tests/nanosoc_multicore_eth.

THIS_DIR  := $(abspath $(CURDIR))
MPS3_ROOT := $(abspath $(CURDIR)/../..)

# --- site-local tree locations, NOT tracked: see tools.env.example -----------
REPO_ROOT ?= $(abspath $(CURDIR)/../..)
TOOLS_ENV ?= $(REPO_ROOT)/tools.env
-include $(TOOLS_ENV)
ARM_IP_LIBRARY_PATH ?=
CMSDK_DIR           ?= $(ARM_IP_LIBRARY_PATH)/BP210/BP210-BU-00000-r1p1-00rel0
ETHMAC_IP_DIR       ?= $(ARM_IP_LIBRARY_PATH)/OpenCores-EthMAC
ETH_SS_HOME         ?= $(abspath $(MPS3_ROOT)/../nanoSoC-refactor/ethernet-subsystem-ahb)
ETHMAC_AHB_HOME     ?= $(ETH_SS_HOME)/ethernet-mac-ahb
DUT_FLIST           := $(ETHMAC_AHB_HOME)/flist/ethmac_ahb_rmii.flist

VERILOG_SOURCES = \
  $(THIS_DIR)/lan8720_model.sv \
  $(THIS_DIR)/tb_lan8720_rmii_echo.sv

EXPANDED_FLIST = $(THIS_DIR)/sim_build/dut.f

COMPILE_ARGS += -full64 -sverilog -timescale=1ns/1ps -debug_access+all -kdb
COMPILE_ARGS += -f $(EXPANDED_FLIST)
COMPILE_ARGS += +incdir+$(ETHMAC_AHB_HOME)/src/rtl
COMPILE_ARGS += +incdir+$(ETHMAC_IP_DIR)/rtl/verilog
COMPILE_ARGS += +define+ETH_WISHBONE_B3
CUSTOM_COMPILE_DEPS += $(EXPANDED_FLIST)

# OpenCores WB slave drives X on read-data during a write data phase; the
# AHB<->WB bridge forwards it on hrdata_s. Resolve X->0 for the BFM view.
export COCOTB_RESOLVE_X ?= ZEROS

ifdef WAVES
  COMPILE_ARGS += +define+WAVES
  SIM_ARGS     += +vcs+dumpvars+waves.vcd
endif

include $(shell cocotb-config --makefiles)/Makefile.sim

# DUT flist expansion (after the include so `sim` stays default). Expand the
# $${VAR} refs, then inject ethmac_ahb.v (the rmii flist omits it) -- exactly
# the tests/lan8720_sut recipe.
$(EXPANDED_FLIST): $(DUT_FLIST)
	@test -f "$(DUT_FLIST)" || { echo "ERROR: DUT flist not found: $(DUT_FLIST)"; exit 1; }
	@test -f "$(ETHMAC_IP_DIR)/rtl/verilog/eth_defines.v" || { echo "ERROR: OpenCores ethmac not under ETHMAC_IP_DIR=$(ETHMAC_IP_DIR)"; exit 1; }
	@mkdir -p $(dir $@)
	@sed -e 's#$${ETHMAC_AHB_HOME}#$(ETHMAC_AHB_HOME)#g' \
	     -e 's#$${ETHMAC_IP_DIR}#$(ETHMAC_IP_DIR)#g' \
	     -e 's#$${CMSDK_DIR}#$(CMSDK_DIR)#g' \
	     "$(DUT_FLIST)" > $@
	@sed -i 's#\(.*ethmac_ahb_rmii.v\)#$(ETHMAC_AHB_HOME)/src/rtl/ethmac_ahb.v\n\1#' $@
	@echo "-- dut.f generated"

.PHONY: guard
guard:
	$(MAKE) PLUSARGS=+MODE_SPEED=0
