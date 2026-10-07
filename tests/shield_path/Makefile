# =============================================================================
# tests/shield_path/Makefile -- reproduce the MPS3 LAN8720 failure signature.
#
# Pure-VCS SV bench (no cocotb), same shape as tests/rmii_conformance/ and
# tests/rmii_speed/. Not wired into repo-root `make check`.
#
#   source <repo-root>/set_env.sh
#   make -C tests/shield_path
#
# Targets:
#   all      (default) run the three-phase signature test
#   run      compile + run                       -> run.log
#   result   regenerate RESULT.txt
#   clean
# =============================================================================

SIM ?= vcs
ifneq ($(SIM),vcs)
$(error This bench is VCS-only (pure-SV, no cocotb). Got SIM=$(SIM); use SIM=vcs)
endif

THIS_DIR  := $(abspath $(CURDIR))
MPS3_ROOT := $(abspath $(CURDIR)/../..)

# This repo's own Clause-22 virtual PHY -- READ ONLY, never modified here.
# NOTE: mdio_phy_model.sv `include`s mdio_slave.sv and phy_reg_model.sv itself
# (see its header, line 21). Passing those two on the command line as well would
# double-define them. VCS resolves `include against +incdir+, not against the
# including file's directory, so the incdir below is required.
PHY_DIR   := $(MPS3_ROOT)/fpga/ethernet/mdio_phy_model
PHY_SRCS  := $(PHY_DIR)/mdio_phy_model.sv

MODEL     := $(THIS_DIR)/shield_path.sv
TB        := $(THIS_DIR)/shield_path_tb.sv

VCS      := vcs
VCS_ARGS := -full64 -sverilog -timescale=1ns/1ps -notice -licqueue \
            -top shield_path_tb +incdir+$(PHY_DIR)

.PHONY: all run result clean

all: run

run:
	@echo "== build (mdio_phy_model behind shield_path) =="
	$(VCS) $(VCS_ARGS) -o simv -l comp.log $(PHY_SRCS) $(MODEL) $(TB)
	@echo "== run: healthy -> inbound dead -> restored =="
	./simv -l run.log
	@grep -E 'SHIELD_PATH:' run.log || true
	@grep -q 'VERDICT=PASS' run.log || { \
	  echo "FAIL: the bench did not reproduce the documented board signature."; exit 1; }
	@echo "OK: MPS3 signature reproduced (writes land, reads read 0xFFFF)."

result: clean run
	@{ echo "MPS3 shield-path failure signature -- VCS transcript"; \
	   echo "virtual PHY : fpga/ethernet/mdio_phy_model/ (C_PHY_ADDR=1, ID 0x0007C0F1)"; \
	   echo "VCS         : $$(vcs -ID 2>/dev/null | head -1)"; echo; \
	   cat run.log; } > RESULT.txt
	@echo "wrote RESULT.txt"

clean:
	rm -rf csrc simv simv.daidir *.log ucli.key vc_hdrs.h .vlogansetup.* \
	       AN.DB novas.* verdi* DVEfiles inter.vpd *.vpd
