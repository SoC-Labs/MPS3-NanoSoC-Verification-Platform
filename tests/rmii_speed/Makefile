# =============================================================================
# tests/rmii_speed/Makefile -- does the shipped rmii_to_mii bridge run at the
# speed the PHY negotiates?
#
# Pure-VCS SV bench (no cocotb), same shape as tests/rmii_conformance/.
#
# Usage:
#   source <repo-root>/set_env.sh
#   make -C tests/rmii_speed
#
# Targets:
#   all      (default) positive control + the defect test
#   pos      mode_speed=1 -> MUST recover the frame       (positive control)
#   defect   mode_speed=0 -> MUST FAIL to recover it      (the claimed defect)
#   result   regenerate RESULT.txt
#   clean
#
# `defect` is a FALSIFICATION test. If mode_speed=0 recovers a 100 Mb/s stream,
# the defect claim is wrong and this Makefile says so and fails.
# =============================================================================

SIM ?= vcs
ifneq ($(SIM),vcs)
$(error This bench is VCS-only (pure-SV, no cocotb). Got SIM=$(SIM); use SIM=vcs)
endif

THIS_DIR  := $(abspath $(CURDIR))
MPS3_ROOT := $(abspath $(CURDIR)/../..)
TB        := $(THIS_DIR)/rmii_speed_tb.sv

# ---------------------------------------------------------------------------
# WHICH COPY OF THE BRIDGE?  There are at least three divergent copies of
# rmii_to_mii.v in this lab and they are NOT interchangeable:
#
#   md5 5e38c43c  has `input wire mode_speed`   <- what the multicore SHIPS
#   md5 5dd9b16f  "Removed the mode_speed input -- hard-wired to 100 Mbps"
#                                                <- what tests/rmii_conformance compiles
#   md5 91e33182  another variant
#
# This bench MUST compile the shipped one. Benching an orphan copy is how you
# get a green result for RTL nobody builds.
# ---------------------------------------------------------------------------
MULTICORE    ?= $(abspath $(MPS3_ROOT)/../nanosoc-multicore-system)
RMII_TO_MII  ?= $(MULTICORE)/ethernet-subsystem-ahb/ethernet-mac-ahb/amba_wb_bridges/src/rtl/rmii_to_mii.v

VCS      := vcs
VCS_ARGS := -full64 -sverilog -timescale=1ns/1ps -notice -licqueue -top rmii_speed_tb

.PHONY: all guard pos defect result clean

all: pos defect

# --- refuse to compile a bridge without the port under test ------------------
guard:
	@test -f "$(RMII_TO_MII)" || { \
	  echo "ERROR: bridge not found: $(RMII_TO_MII)"; \
	  echo "       override with  make RMII_TO_MII=/path/to/rmii_to_mii.v"; exit 1; }
	@grep -qE '^[[:space:]]*input[[:space:]]+wire[[:space:]]+mode_speed' "$(RMII_TO_MII)" || { \
	  echo "########################################################################"; \
	  echo "# WRONG COPY OF rmii_to_mii.v"; \
	  echo "#   $(RMII_TO_MII)"; \
	  echo "# has no 'mode_speed' input port. That variant is hard-wired to 100 Mb/s"; \
	  echo "# and cannot exercise the defect. Point RMII_TO_MII at the copy the"; \
	  echo "# multicore actually ships (expect md5 5e38c43c...)."; \
	  echo "########################################################################"; \
	  exit 1; }
	@echo "== bridge OK: $(RMII_TO_MII)"
	@echo "==   md5 $$(md5sum "$(RMII_TO_MII)" | cut -c1-8)  (expect 5e38c43c)"

# --- positive control: mode_speed=1 must recover a 100 Mb/s stream -----------
pos: guard
	@echo "== [pos] build =="
	$(VCS) $(VCS_ARGS) -o simv_pos -l comp_pos.log $(RMII_TO_MII) $(TB)
	@echo "== [pos] run (mode_speed=1)  EXPECT VERDICT=PASS =="
	./simv_pos +mode_speed=1 -l run_pos.log
	@grep -E 'RMII_SPEED:' run_pos.log || true
	@grep -q 'VERDICT=PASS' run_pos.log || { \
	  echo "FAIL: positive control did not recover the frame -- the bench itself is broken."; exit 1; }
	@echo "OK: positive control passed."

# --- the defect: mode_speed=0 must NOT recover a 100 Mb/s stream -------------
defect: guard
	@echo "== [defect] build =="
	$(VCS) $(VCS_ARGS) -o simv_def -l comp_def.log $(RMII_TO_MII) $(TB)
	@echo "== [defect] run (mode_speed=0)  EXPECT VERDICT=FAIL =="
	./simv_def +mode_speed=0 -l run_def.log
	@grep -E 'RMII_SPEED:' run_def.log || true
	@if grep -q 'VERDICT=PASS' run_def.log; then \
	  echo "########################################################################"; \
	  echo "# DEFECT CLAIM REFUTED."; \
	  echo "# mode_speed=0 recovered a 100 Mb/s RMII stream. The reasoning in"; \
	  echo "# docs/LAN8720_BRINGUP_WORK_ITEM.md section 5 is WRONG -- fix the doc."; \
	  echo "########################################################################"; \
	  exit 1; \
	fi
	@echo "OK: mode_speed=0 fails to recover 100 Mb/s, as predicted."

result: clean pos defect
	@{ echo "rmii_to_mii mode_speed bench -- VCS transcript"; \
	   echo "bridge : $(RMII_TO_MII)"; \
	   echo "md5    : $$(md5sum "$(RMII_TO_MII)" | cut -c1-8)"; \
	   echo "VCS    : $$(vcs -ID 2>/dev/null | head -1)"; echo; \
	   echo "#### POSITIVE CONTROL (mode_speed=1) -- expect PASS ####"; cat run_pos.log; echo; \
	   echo "#### DEFECT (mode_speed=0) -- expect FAIL ####"; cat run_def.log; } > RESULT.txt
	@echo "wrote RESULT.txt"

clean:
	rm -rf csrc simv_pos simv_def simv_pos.daidir simv_def.daidir \
	       *.log ucli.key vc_hdrs.h .vlogansetup.* AN.DB novas.* verdi* \
	       DVEfiles inter.vpd *.vpd
