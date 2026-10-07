# Cocotb Makefile for eth_ss_bringup verification (mps3-nanosoc-platform).
# NEW leaf bench (A5 verification-confidence pass): fpga/rp/eth_ss/
# eth_ss_bringup.sv is the single-master AHB-Lite constant-programmer FSM that
# configures rm_eth_ss after reset — real sequential logic that had ZERO
# verification. See dut_notes.md and tests/README.md.
#
# TWO ARMS (selected with ARM=, not BLOCK=: tests/Makefile's run-one passes
# BLOCK=eth_ss_bringup down to this sub-make):
#
#   make            ARM=fsm (default) — the FSM alone against an always-ready
#                   AHB slave: the 13 bring-up writes, then the TXCTRL
#                   transmit-beacon schedule. Pure repo RTL, so it runs in CI.
#
#   make ARM=tx     the WHOLE RM (rp_eth_ss_wrapper: FSM + real OpenCores MAC
#                   + rmii_to_mii) driving the shell's virtual PHY, bridge and
#                   DUTEGR capture FIFO (tests/dut_egress/tb_dut_egress.sv,
#                   reused as-is). Proves the RM TRANSMITS: frames are read
#                   back byte for byte over DUTEGR's AXI-Lite surface. Needs
#                   $ETH_SS_HOME + the Arm IP library (read-only),
#                   so it is NOT in the default run.
#
# CONTROL (must be SEEN TO FAIL): run either arm against the pre-2026-09-23
# FSM, which parks after MODER and never transmits:
#   make ARM=tx BRINGUP_SV=/path/to/old/eth_ss_bringup.sv
#
# Usage: source <repo-root>/set_env.sh; make [ARM=tx]
#        (or `make -C tests BLOCK=eth_ss_bringup run-one` for ARM=fsm)

TOPLEVEL_LANG = verilog
SIM ?= vcs
ARM ?= fsm

MPS3_ROOT  := $(abspath $(CURDIR)/../..)
RTL_DIR    := $(MPS3_ROOT)/fpga/rp/eth_ss
BRINGUP_SV ?= $(RTL_DIR)/eth_ss_bringup.sv
# A control build must never reuse the real build's simv (or vice versa).
ifeq ($(BRINGUP_SV),$(RTL_DIR)/eth_ss_bringup.sv)
BUILD_SFX :=
else
BUILD_SFX := _control
endif

ifeq ($(ARM),fsm)

VERILOG_SOURCES = $(BRINGUP_SV)

# RTL is SystemVerilog (`logic`, `always_ff`, typedef enum/struct) -- VCS
# needs -sverilog. VCS-only flag (Questa parses *.sv by extension; see README).
ifeq ($(SIM),vcs)
COMPILE_ARGS += -sverilog
# Shorten the post-reset settle + inter-write gap for a fast sim (defaults are
# 64 / 8 — the bench's expectations are timing-independent). MUST keep
# GAP_CYCLES >= 4 (the RTL's ha1588-strobe CDC requirement). REARM_CYCLES is
# the beacon period (RTL default 50,000,000 = 1 s at 50 MHz).
COMPILE_ARGS += -pvalue+eth_ss_bringup/START_DELAY_CYCLES=8 -pvalue+eth_ss_bringup/GAP_CYCLES=4
# (Not passed to a control FSM that predates the parameter.)
ifeq ($(BRINGUP_SV),$(RTL_DIR)/eth_ss_bringup.sv)
COMPILE_ARGS += -pvalue+eth_ss_bringup/REARM_CYCLES=200
endif
endif
export REARM_CYCLES := 200

TOPLEVEL = eth_ss_bringup
MODULE   = test_eth_ss_bringup
SIM_BUILD := $(CURDIR)/sim_build_fsm$(BUILD_SFX)

# Coverage toggle (no SVA bind — this block is an AHB master, not an
# AXI4-Lite slave).
include $(abspath $(CURDIR)/../common)/bench_common.mk

include $(shell cocotb-config --makefiles)/Makefile.sim

else ifeq ($(ARM),tx)

# --- site-local tree locations, NOT tracked: see tools.env.example -----------
REPO_ROOT ?= $(abspath $(CURDIR)/../..)
TOOLS_ENV ?= $(REPO_ROOT)/tools.env
-include $(TOOLS_ENV)
# ETH_SS_HOME and ARM_IP_LIBRARY_PATH have NO default (tools.env).
ETH_SS_HOME     ?=
ETHMAC_AHB_HOME ?= $(ETH_SS_HOME)/ethernet-mac-ahb
ETHMAC_IP_DIR   ?= $(ARM_IP_LIBRARY_PATH)/OpenCores-EthMAC
HA1588_IP_DIR   ?= $(ARM_IP_LIBRARY_PATH)/OpenCores-HA1588
CMSDK_DIR       ?= $(ARM_IP_LIBRARY_PATH)/BP210/BP210-BU-00000-r1p1-00rel0
export ETH_SS_HOME ETHMAC_AHB_HOME ETHMAC_IP_DIR HA1588_IP_DIR CMSDK_DIR

ETH := $(MPS3_ROOT)/fpga/ethernet
IP  := $(MPS3_ROOT)/fpga/shell/ip/dut_egress

# The subsystem's own flist, then the extras filelist.tcl adds — the same set
# the OOC synth reads (fpga/rp/eth_ss/filelist.tcl).
FLIST := $(ETHMAC_AHB_HOME)/flist/ethmac_subsystem_apb.flist
FLIST_RAW   := $(shell grep -v -e '^[[:space:]]*[+-]' -e '^[[:space:]]*$$' $(FLIST))
FLIST_FILES := $(subst $${ETHMAC_AHB_HOME},$(ETHMAC_AHB_HOME),$(subst $${ETHMAC_IP_DIR},$(ETHMAC_IP_DIR),$(subst $${HA1588_IP_DIR},$(HA1588_IP_DIR),$(subst $${CMSDK_DIR},$(CMSDK_DIR),$(FLIST_RAW)))))
RM_SOURCES := \
  $(foreach f,$(FLIST_FILES),$(f)) \
  $(CMSDK_DIR)/logical/cmsdk_ahb_to_apb/verilog/cmsdk_ahb_to_apb.v \
  $(CMSDK_DIR)/logical/cmsdk_ahb_to_sram/verilog/cmsdk_ahb_to_sram.v \
  $(ETH_SS_HOME)/src/rtl/fpga_lib/sram/sl_ahb_sram.v \
  $(ETH_SS_HOME)/src/rtl/ethmac_subsystem_ahb.v \
  $(ETHMAC_AHB_HOME)/amba_wb_bridges/src/rtl/rmii_to_mii.v \
  $(BRINGUP_SV) \
  $(RTL_DIR)/rp_eth_ss_wrapper.sv

VERILOG_SOURCES := \
  $(RM_SOURCES) \
  $(ETH)/rmii_phy_if/rmii_phy_if.sv \
  $(ETH)/link_partner_mac/link_partner_mac.sv \
  $(ETH)/bridge/eth_bridge_3port.sv \
  $(ETH)/gen_checker/gen_checker.sv \
  $(ETH)/mdio_phy_model/mdio_phy_model.sv \
  $(ETH)/eth_mac_test_subsystem.sv \
  $(IP)/dut_egress.sv \
  $(MPS3_ROOT)/tests/dut_egress/tb_dut_egress.sv \
  $(CURDIR)/tb_eth_ss_tx.sv

COMPILE_ARGS += -sverilog +v2k -timescale=1ns/1ps
COMPILE_ARGS += +define+ETH_WISHBONE_B3
COMPILE_ARGS += +incdir+$(ETHMAC_AHB_HOME)/src/rtl +incdir+$(ETHMAC_IP_DIR)/rtl/verilog
COMPILE_ARGS += +incdir+$(HA1588_IP_DIR)/rtl/tsu +incdir+$(ETH_SS_HOME)/src/rtl
COMPILE_ARGS += +incdir+$(ETH)/mdio_phy_model +incdir+$(IP)
ifneq ($(BUILD_SFX),)
COMPILE_ARGS += +define+CONTROL_OLD_FSM
endif

TOPLEVEL  := tb_eth_ss_tx
MODULE    := test_eth_ss_tx
SIM_BUILD := $(CURDIR)/sim_build_tx$(BUILD_SFX)

include $(shell cocotb-config --makefiles)/Makefile.sim

else
$(error unknown ARM=$(ARM); expected fsm or tx)
endif
