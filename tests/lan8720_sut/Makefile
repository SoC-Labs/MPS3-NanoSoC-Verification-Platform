# =============================================================================
# tests/lan8720_sut/Makefile
#
# SUT: exercises the REAL ethernet-subsystem DUT (ethmac_ahb_rmii) at its MDIO
# boundary, through a model of the MPS3 J28/PMOD0 shield path, against this
# repo's Clause-22 virtual PHY. Same J28 wiring as the arm_mps3 ethernet target.
#
# Pure-VCS SV bench (no cocotb), standalone like tests/rmii_conformance,
# tests/rmii_speed, tests/shield_path. Not wired into repo-root `make check`.
#
#   source <repo-root>/set_env.sh
#   make -C tests/lan8720_sut
#
# The DUT + its OpenCores ethmac core + the ARM CMSDK SRAM model are consumed
# READ-ONLY from the sibling repos and the ARM IP library (never modified). Paths
# resolve exactly like ethernet-subsystem-ahb/set_env.sh; override on the CLI if
# your checkout differs.
# =============================================================================

SIM ?= vcs
ifneq ($(SIM),vcs)
$(error This bench is VCS-only (pure-SV, no cocotb). Got SIM=$(SIM); use SIM=vcs)
endif

THIS_DIR  := $(abspath $(CURDIR))
MPS3_ROOT := $(abspath $(CURDIR)/../..)

# ---- read-only sources (sibling repos + ARM IP library) ---------------------
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

# ---- this repo's models (the LAN8720 stand-in + the shield path) -------------
PHY_DIR   := $(MPS3_ROOT)/fpga/ethernet/mdio_phy_model
SHIELD    := $(MPS3_ROOT)/tests/shield_path/shield_path.sv
TB        := $(THIS_DIR)/tb_lan8720_sut.sv
TB_DP     := $(THIS_DIR)/tb_lan8720_dp.sv

VCS      := vcs
VCS_ARGS := -full64 -sverilog -timescale=1ns/1ps -notice -licqueue \
            -top tb_lan8720_sut +incdir+$(PHY_DIR)

.PHONY: all guard dut.f run result dp dp-result clean

all: run

guard:
	@test -f "$(DUT_FLIST)" || { echo "ERROR: DUT flist not found: $(DUT_FLIST)"; \
	  echo "  override ETH_SS_HOME=/path/to/ethernet-subsystem-ahb"; exit 1; }
	@test -f "$(ETHMAC_IP_DIR)/rtl/verilog/eth_defines.v" || { \
	  echo "ERROR: OpenCores ethmac not found under ETHMAC_IP_DIR=$(ETHMAC_IP_DIR)"; exit 1; }
	@test -f "$(CMSDK_DIR)/logical/models/memories/cmsdk_fpga_sram.v" || { \
	  echo "ERROR: CMSDK SRAM model not found under CMSDK_DIR=$(CMSDK_DIR)"; exit 1; }

# Expand the flist's env vars into a concrete VCS -f file, and add ethmac_ahb.v
# (the rmii flist omits it -- the ASIC flow pulls it from ethmac_ahb.flist).
dut.f: guard
	@sed -e 's#$${ETHMAC_AHB_HOME}#$(ETHMAC_AHB_HOME)#g' \
	     -e 's#$${ETHMAC_IP_DIR}#$(ETHMAC_IP_DIR)#g' \
	     -e 's#$${CMSDK_DIR}#$(CMSDK_DIR)#g' \
	     "$(DUT_FLIST)" > dut.f
	@sed -i 's#\(.*ethmac_ahb_rmii.v\)#$(ETHMAC_AHB_HOME)/src/rtl/ethmac_ahb.v\n\1#' dut.f
	@echo "-- dut.f generated ($(shell grep -c '\.v' dut.f 2>/dev/null || echo '?') source files)"

run: dut.f
	@echo "== build (real DUT ethmac_ahb_rmii + shield_path + mdio_phy_model) =="
	$(VCS) $(VCS_ARGS) -f dut.f $(SHIELD) $(PHY_DIR)/mdio_phy_model.sv $(TB) \
	    -o simv_sut -l comp.log
	@echo "== run =="
	./simv_sut -l run.log
	@grep -E 'LAN8720_SUT:' run.log | grep -vE '^\s*$$' || true
	@grep -q 'VERDICT=PASS' run.log || { \
	  echo "FAIL: the SUT did not reach VERDICT=PASS (board fault not reproduced)."; exit 1; }
	@echo "OK: MPS3 dead-inbound signature reproduced through the real DUT MDIO master."

# ---- DATAPATH bench: real DUT MAC+DMA+RMII egress through the shield ---------
# Separate top (tb_lan8720_dp). A pure-SV AHB-lite slave RAM answers the DMA
# master; the RMII TX egress is decoded and checked frame-for-frame. No PHY model
# needed here (the shield b-side RMII/MDIO are tied off for the TX phase).
dp: dut.f
	@echo "== build DATAPATH (real DUT ethmac_ahb_rmii + shield_path + RMII egress decode) =="
	$(VCS) -full64 -sverilog -timescale=1ns/1ps -notice -licqueue -top tb_lan8720_dp \
	    +incdir+$(PHY_DIR) -f dut.f $(SHIELD) $(TB_DP) -o simv_dp -l comp_dp.log
	@echo "== run =="
	./simv_dp -l run_dp.log
	@grep -E 'LAN8720_DP:' run_dp.log | grep -vE '^\s*$$' || true
	@grep -q 'VERDICT=PASS' run_dp.log || { \
	  echo "FAIL: the datapath SUT did not reach VERDICT=PASS (TX egress not proven)."; exit 1; }
	@echo "OK: TX frame egressed on RMII through the real MAC + DMA + shield path."

dp-result: clean dp
	@{ echo "LAN8720 SUT -- DATAPATH (TX frame egress) through the real ethernet-subsystem DUT"; \
	   echo "DUT   : ethmac_ahb_rmii (OpenCores ethmac MAC + DMA master + rmii_to_mii)"; \
	   echo "RAM   : pure-SV AHB-lite slave (this TB) answering the DMA master"; \
	   echo "shield: tests/shield_path/shield_path.sv (MPS3 J28/PMOD0 path, ref_clk routed through)"; \
	   echo "VCS   : $$(vcs -ID 2>/dev/null | head -1)"; echo; \
	   cat run_dp.log; } > RESULT_DP.txt
	@echo "wrote RESULT_DP.txt"

result: clean run
	@{ echo "LAN8720 SUT -- real ethernet-subsystem DUT at the MDIO boundary"; \
	   echo "DUT   : ethmac_ahb_rmii (OpenCores ethmac + eth_miim MDIO master)"; \
	   echo "PHY   : fpga/ethernet/mdio_phy_model (this repo's Clause-22 virtual PHY)"; \
	   echo "shield: tests/shield_path/shield_path.sv (MPS3 J28/PMOD0 path, fault injectable)"; \
	   echo "VCS   : $$(vcs -ID 2>/dev/null | head -1)"; echo; \
	   cat run.log; } > RESULT.txt
	@echo "wrote RESULT.txt"

clean:
	rm -rf csrc simv_sut simv_sut.daidir *.log ucli.key vc_hdrs.h .vlogansetup.* \
	       AN.DB novas.* verdi* DVEfiles inter.vpd *.vpd dut.f .elab_probe.tcl \
	       simv_dut simv_dut.daidir elab_dut.log comp_sut.log run_sut.log \
	       simv_dp simv_dp.daidir comp_dp.log run_dp.log
