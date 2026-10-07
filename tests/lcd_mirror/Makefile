# Cocotb Makefile for LCDMIR, the pixel-exact LCD mirror snooper
# (fpga/shell/ip/lcd_mirror/, contract docs/planning/linux_lanes/LCD_MIRROR_FPGA.md).
#
# STANDALONE, like tests/clcd_kvm_e2e: not yet in tests/Makefile BENCH_DIRS or
# tests/common/list_benches.py -- registering it is a mint-4 integration step
# (fpga/shell/ip/lcd_mirror/README.md "Mint-4 integration").
#
#   source <repo-root>/set_env.sh   # miniconda py3.10 + cocotb 2.0.1 + VCS
#   make                            # MODE=direct: the snooper alone, every stream
#   make MODE=banked                # the 64 KiB banked-map fallback (BANKED=1)
#   make MODE=e2e                   # through the REAL clcd_kvm + ahb_clcd + clcd_core
#                                   #   (tests/clcd_kvm_e2e's harness, reused as-is)
#   make pytest                     # the golden model vs its independent oracles,
#                                   #   no simulator
#   make mutants                    # MUTATION CONTROLS: each broken RTL copy must
#                                   #   make its named tests FAIL
#   make all-modes                  # pytest + direct + banked + e2e + mutants
#
# Artefacts (the stimulus file, frame dumps, the recorded harness stream) go in
# $(SIM_BUILD); LCDMIR_WORK tells the Python where.

TOPLEVEL_LANG = verilog
SIM ?= vcs

REPO     := $(abspath $(CURDIR)/../..)
IP_DIR   := $(REPO)/fpga/shell/ip/lcd_mirror
KVM_DIR  := $(REPO)/fpga/shell/ip/clcd_kvm
CLCD_DIR := $(REPO)/fpga/shell/ip/clcd
EXP_DIR  := $(REPO)/fpga/rp/nanosoc_exp

# Overridable so `mutants` can point the identical bench at a MUTATED scratch
# copy without touching the real RTL (the tests/clcd_kvm_e2e falsify idiom).
LCDMIR_TOP  ?= $(IP_DIR)/lcd_mirror.sv
LCDMIR_CORE ?= $(IP_DIR)/lcd_mirror_core.sv
LCDMIR_RAM  ?= $(IP_DIR)/lcd_mirror_ram.sv
MUT_TAG     ?= clean

MODE ?= direct

VERILOG_SOURCES = $(LCDMIR_RAM) $(LCDMIR_CORE) $(LCDMIR_TOP)

ifeq ($(MODE),direct)
  VERILOG_SOURCES += $(CURDIR)/tb_axil_dump.sv $(CURDIR)/tb_lcd_mirror.sv
  TOPLEVEL = tb_lcd_mirror
  MODULE  ?= test_lcd_mirror
endif
ifeq ($(MODE),banked)
  VERILOG_SOURCES += $(CURDIR)/tb_axil_dump.sv $(CURDIR)/tb_lcd_mirror.sv
  TOPLEVEL = tb_lcd_mirror
  MODULE  ?= test_lcd_mirror_banked
  ifeq ($(SIM),vcs)
    COMPILE_ARGS += -pvalue+tb_lcd_mirror.BANKED=1
  endif
endif
ifeq ($(MODE),e2e)
  VERILOG_SOURCES += $(KVM_DIR)/clcd_kvm.sv $(CLCD_DIR)/clcd_core.sv \
                     $(EXP_DIR)/ahb_clcd.sv $(EXP_DIR)/nanosoc_exp_socket.sv \
                     $(REPO)/tests/clcd_kvm_e2e/tb_clcd_kvm_e2e.sv \
                     $(CURDIR)/tb_axil_dump.sv $(CURDIR)/tb_lcd_mirror_e2e.sv
  TOPLEVEL = tb_lcd_mirror_e2e
  MODULE  ?= test_lcd_mirror_e2e
endif

ifeq ($(SIM),vcs)
COMPILE_ARGS += -sverilog
endif

# SVA: the shared AXI4-Lite protocol checker, bound onto the slave.
SVA_MODULES := axi4lite_protocol_checker.sv
include $(abspath $(CURDIR)/../common)/bench_common.mk
VERILOG_SOURCES += $(CURDIR)/bind_lcd_mirror.sv

# One simv per mode and per mutant: a stale simv built from other sources must
# never run silently (the csr_decode_width trap). `:=`, NOT `?=`: cocotb's
# Makefile.inc EXPORTS SIM_BUILD and COCOTB_RESULTS_FILE, so under `?=` a
# recursive `$(MAKE) MODE=banked` would inherit the parent's direct build dir.
# A command-line value still wins.
SIM_BUILD := $(CURDIR)/sim_build_$(MODE)_$(MUT_TAG)
COCOTB_RESULTS_FILE := $(CURDIR)/results_$(MODE)_$(MUT_TAG).xml

export LCDMIR_WORK := $(SIM_BUILD)
COCOTB_PLUSARGS += +STIM_FILE=$(SIM_BUILD)/stim.hex +DUMP_FILE=$(SIM_BUILD)/fb_dump.hex

include $(shell cocotb-config --makefiles)/Makefile.sim

# -----------------------------------------------------------------------------
# The golden model against its independent oracles (no simulator, no licence).
# -----------------------------------------------------------------------------
.PHONY: pytest
pytest:
	LCDMIR_WORK=$(CURDIR)/sim_build_py python3 -m pytest -q -p no:cacheprovider $(CURDIR)/test_model.py

# -----------------------------------------------------------------------------
# MUTATION CONTROLS. Each rule seds ONE defect into a scratch copy, checks the
# sed actually applied (a silently-unapplied mutation proves nothing), rebuilds
# the bench against it and requires the named tests to FAIL.
# -----------------------------------------------------------------------------
MUT_DIR := $(CURDIR)/sim_build_mutants
comma := ,

# A mutant is KILLED only if its results file exists, the named tests RAN, and
# at least one FAILED. A mutant that does not compile is an ERROR, not a kill.
# Every per-mutant path is passed EXPLICITLY: cocotb's makefiles export
# SIM_BUILD/COCOTB_PLUSARGS, so a `?=` default here would silently inherit the
# parent's -- and compile the mutant INTO the clean simv (found the hard way).
# The filter is re-quoted because cocotb pastes it unquoted into a shell line.
define run_mutant
	@mkdir -p $(MUT_DIR)
	@sed -E $(3) $(IP_DIR)/$(2) > $(MUT_DIR)/$(1)_$(2)
	@if cmp -s $(IP_DIR)/$(2) $(MUT_DIR)/$(1)_$(2); then \
	   echo "!!! mutation $(1) did not apply to $(2) -- the source changed; re-derive it"; exit 1; fi
	@echo "=== MUTANT $(1): $(4) -> must FAIL: $(5) ==="
	@rm -f $(CURDIR)/results_direct_$(1).xml
	@rm -rf $(CURDIR)/sim_build_direct_$(1)
	-@$(MAKE) --no-print-directory MODE=direct MUT_TAG=$(1) \
	    SIM_BUILD=$(CURDIR)/sim_build_direct_$(1) \
	    COCOTB_RESULTS_FILE=$(CURDIR)/results_direct_$(1).xml \
	    LCDMIR_WORK=$(CURDIR)/sim_build_direct_$(1) \
	    COCOTB_PLUSARGS="+STIM_FILE=$(CURDIR)/sim_build_direct_$(1)/stim.hex +DUMP_FILE=$(CURDIR)/sim_build_direct_$(1)/fb_dump.hex" \
	    $(if $(filter lcd_mirror.sv,$(2)),LCDMIR_TOP=$(MUT_DIR)/$(1)_$(2)) \
	    $(if $(filter lcd_mirror_core.sv,$(2)),LCDMIR_CORE=$(MUT_DIR)/$(1)_$(2)) \
	    COCOTB_TEST_FILTER="'$(5)'" > $(MUT_DIR)/$(1).log 2>&1
	@python3 -c "import sys,xml.etree.ElementTree as E; \
	  r=E.parse('$(CURDIR)/results_direct_$(1).xml').getroot(); \
	  tc=[t for t in r.iter('testcase') if t.find('skipped') is None]; f=[t.get('name') for t in tc if t.find('failure') is not None]; \
	  print('    ran', [t.get('name') for t in tc], 'failed', f); \
	  sys.exit(0 if tc and f else 1)" \
	  && echo "=== OK: mutant $(1) KILLED ===" \
	  || { echo "!!! MUTANT $(1) SURVIVED (or did not build): see $(MUT_DIR)/$(1).log"; exit 1; }
endef

.PHONY: mutants mut_autoinc mut_mxswap mut_byteorder mut_snapclr mut_guard mut_cmp
mutants: mut_autoinc mut_mxswap mut_byteorder mut_snapclr mut_guard mut_cmp
	@echo "=== all 6 mutants killed ==="

mut_autoinc:
	$(call run_mutant,autoinc,lcd_mirror_core.sv,'s/if \(acx_q == ec_q\) begin/if (acx_q == ec_q + 9'"'"'d1) begin/',off-by-one x wrap in the auto-increment,test_harness_stream|test_clcd_demo_frames)

mut_mxswap:
	$(call run_mutant,mxswap,lcd_mirror_core.sv,'s/wire       flip_g = cfg_flip_conv \? \(mv \? mx : my\) : my;/wire       flip_g = cfg_flip_conv ? (mv ? mx : my) : mx;/; s/wire       flip_s = cfg_flip_conv \? \(mv \? my : mx\) : mx;/wire       flip_s = cfg_flip_conv ? (mv ? my : mx) : my;/',MX and MY swapped (physical convention),test_madctl_geometries)

mut_byteorder:
	$(call run_mutant,byteorder,lcd_mirror_core.sv,'s/: \{b0_q$(comma) d_b\};/: {d_b$(comma) b0_q};/',pixel byte order swapped,test_harness_stream|test_clcd_demo_frames)

mut_snapclr:
	$(call run_mutant,snapclr,lcd_mirror.sv,'s/dirty_live_q\[gk\] <= d2_mark \& hit;/dirty_live_q[gk] <= dirty_live_q[gk] | (d2_mark \& hit);/',SNAP does not clear the live dirty map,test_snap_semantics)

mut_cmp:
	$(call run_mutant,cmp,lcd_mirror.sv,'s/\(d2_old != d2_px\)/1'"'"'b1/',compare-on-write disabled (every write marks),test_clcd_demo_frames)

mut_guard:
	$(call run_mutant,guard,lcd_mirror_core.sv,'s/wire v_chg   = ~wr_n_1 \& ~wr_n_2 \& ~cs_n_1 \& ~same_1;/wire v_chg   = 1'"'"'b0;/',the PD-change-while-WR-low guard removed,test_viol_guards)

.PHONY: all-modes
all-modes: pytest
	$(MAKE) MODE=direct
	$(MAKE) MODE=banked
	$(MAKE) MODE=e2e
	$(MAKE) mutants

clean::
	-rm -rf $(CURDIR)/sim_build_* $(CURDIR)/results_*.xml __pycache__ ucli.key \
	        novas.* verdiLog *.vcd *.vpd csrc simv simv.daidir
