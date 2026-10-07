# Cocotb Makefile for the CLCD-KVM END-TO-END feature proof (W3-B,
# docs/CLCD_KVM_WAVE_PLAN.md Wave 3). This bench structurally assembles the REAL
# delivered blocks — clcd_kvm (shell) + the W3-A tunnel mux (in tb_clcd_kvm_e2e.sv)
# + nanosoc_exp_socket + ahb_clcd + clcd_core — and drives them so the panel
# model (tests/clcd/clcd_panel_model.py) watches ownership hand over.
#
# STANDALONE: it does NOT touch the shared tests/Makefile or
# tests/common/list_benches.py (they carry another track's edits). Run it here:
#
#   source <repo-root>/set_env.sh   # miniconda python3.10 + cocotb 2.0.1 + VCS
#   make                            # the three e2e tests
#   make falsify                    # MUTATION PROOF: break the KVM's outgoing
#                                   #   (S_DRAIN) drain gate in a SCRATCH copy and
#                                   #   prove the headline seam test turns RED. A
#                                   #   seam test that cannot fail proves nothing.
#   make clean

TOPLEVEL_LANG = verilog
SIM ?= vcs

REPO       := $(abspath $(CURDIR)/../..)
KVM_DIR    := $(REPO)/fpga/shell/ip/clcd_kvm
CLCD_DIR   := $(REPO)/fpga/shell/ip/clcd
EXP_DIR    := $(REPO)/fpga/rp/nanosoc_exp

# KVM_SRC is overridable so `falsify` points the identical bench at a MUTATED
# scratch copy of clcd_kvm.sv without ever touching W2-A's file (the
# tests/clcd_kvm falsify idiom).
KVM_SRC ?= $(KVM_DIR)/clcd_kvm.sv

VERILOG_SOURCES = \
  $(KVM_SRC) \
  $(CLCD_DIR)/clcd_core.sv \
  $(EXP_DIR)/ahb_clcd.sv \
  $(EXP_DIR)/nanosoc_exp_socket.sv \
  $(CURDIR)/tb_clcd_kvm_e2e.sv

# RTL is SystemVerilog (`logic`, `always_ff`, `typedef enum`) -> VCS -sverilog.
ifeq ($(SIM),vcs)
COMPILE_ARGS += -sverilog
endif

TOPLEVEL = tb_clcd_kvm_e2e
MODULE  ?= test_clcd_kvm_e2e

# SIM_BUILD carries the KVM source basename so an honest run never reuses a simv
# built from a MUTATED source (the stale-artifact trap that bit csr_decode_width).
SIM_BUILD ?= $(CURDIR)/sim_build_$(notdir $(basename $(KVM_SRC)))

include $(shell cocotb-config --makefiles)/Makefile.sim

# -----------------------------------------------------------------------------
# MUTATION PROOF. The headline property is "no truncated 8080 cycle across a
# switch", resting on the KVM's two-sided quiescence gate. This removes the
# OUTGOING (S_DRAIN) side in a scratch copy and reruns the headline test, which
# MUST go red. W2-A's clcd_kvm.sv is never written.
# -----------------------------------------------------------------------------
MUT_DIR := $(CURDIR)/sim_build_mutant

.PHONY: falsify
falsify:
	@mkdir -p $(MUT_DIR)
	@sed -E 's/cur_owner_quiet \|\| force_switch_q/1'"'"'b1 \|\| force_switch_q/' \
	    $(KVM_DIR)/clcd_kvm.sv > $(MUT_DIR)/clcd_kvm_drainmut.sv
	@grep -q "1'b1 || force_switch_q" $(MUT_DIR)/clcd_kvm_drainmut.sv \
	  || { echo "!!! the S_DRAIN mutation did not apply — the gate expression in clcd_kvm.sv changed (see ~line 592). Re-derive the mutation."; exit 1; }
	@echo "=== FALSIFICATION: KVM S_DRAIN (outgoing) drain gate REMOVED. The headline seam test MUST fail. ==="
	@$(MAKE) SIM_BUILD=$(MUT_DIR)/run_drain KVM_SRC=$(MUT_DIR)/clcd_kvm_drainmut.sv \
	         TESTCASE=test_kvm_e2e_button_handover_and_back \
	  && { echo "!!! FALSIFICATION FAILED: the headline seam test PASSED with the drain gate removed."; \
	       echo "!!! The detector is blind — do not trust a green run."; exit 1; } \
	  || echo "=== OK: the headline seam test fails with the drain gate removed, as it must ==="

clean::
	-rm -rf $(CURDIR)/sim_build_clcd_kvm* $(MUT_DIR) __pycache__ results.xml \
	        ucli.key novas.* verdiLog *.vcd *.vpd csrc simv simv.daidir
