# Cocotb Makefile for the clcd_kvm (CLCD KVM) bench — mps3-nanosoc-platform.
# See dut_notes.md for the DUT's port list + register map, and the frozen
# contract fpga/shell/ip/clcd_kvm/README.md (the bench is written against THAT,
# not against the RTL — RTL and bench were written in parallel by W2-A/W2-C).
#
# Usage:
#   source <repo-root>/set_env.sh   # miniconda python3.10 + cocotb 2.0.1 + VCS
#   make                            # the whole bench (test_clcd_kvm.py)
#   make falsify                    # MUTATION PROOF: break the OUTGOING (S_DRAIN)
#                                   #   quiescence gate in a SCRATCH copy of the
#                                   #   RTL and prove the truncated-cycle tests
#                                   #   turn RED. A gate that cannot fail proves
#                                   #   nothing (the systemrdl equiv-gate lesson).
#   make falsify-grant              # same, for the INCOMING (S_GRANT) gate — the
#                                   #   half a one-sided KVM forgets.
#   make falsify-pb-reset           # CONTROL for the PB1-held-through-reset fix:
#                                   #   put the OLD reset values back (the PB1 chain
#                                   #   reset to "released") and prove the two
#                                   #   test_pb1_held_through_reset_* tests turn RED.
#
# Prefer `make -C tests BLOCK=clcd_kvm run-one` (top-level Makefile) for the
# gated flow: it checks RTL readiness first (tests/common/list_benches.py).

TOPLEVEL_LANG = verilog
SIM ?= vcs

RTL_DIR := $(abspath $(CURDIR)/../../fpga/shell/ip/clcd_kvm)

# RTL_SRC is overridable so `falsify` can point the identical bench at a MUTATED
# scratch copy without ever touching W2-A's file (the qspi_xip / uart2_rx_path
# falsify idiom).
RTL_SRC ?= $(RTL_DIR)/clcd_kvm.sv
VERILOG_SOURCES = $(RTL_SRC)

# RTL is SystemVerilog (`logic`, `always_ff`, `typedef enum`) -> VCS needs
# -sverilog (same as tests/clcd).
ifeq ($(SIM),vcs)
COMPILE_ARGS += -sverilog
endif

TOPLEVEL = clcd_kvm
MODULE  ?= test_clcd_kvm

# SVA: bind the shared AXI4-Lite protocol checker onto this slave (read-only
# reuse from tests/common/sva/ via SVA_MODULES); the bind file is THIS block's
# own (tests/clcd_kvm/bind_clcd_kvm.sv), added to VERILOG_SOURCES directly.
SVA_MODULES := axi4lite_protocol_checker.sv
include $(abspath $(CURDIR)/../common)/bench_common.mk
VERILOG_SOURCES += $(CURDIR)/bind_clcd_kvm.sv

# SIM_BUILD carries the source basename so an honest run never reuses a simv that
# was built from a MUTATED source (the stale-artifact trap that bit
# csr_decode_width — the .sv can be older than the binary, so nothing rebuilds).
SIM_BUILD ?= $(CURDIR)/sim_build_$(notdir $(basename $(RTL_SRC)))

include $(shell cocotb-config --makefiles)/Makefile.sim

# -----------------------------------------------------------------------------
# MUTATION PROOFS. The headline property of this block is "no truncated 8080
# cycle across a switch", and it rests on the TWO-SIDED quiescence gate (README
# §6). Each target removes ONE side of the gate in a scratch copy of the RTL and
# reruns the truncated-cycle tests, which MUST go red. The scratch copy is under
# $(MUT_DIR); W2-A's clcd_kvm.sv is never written.
#
# The mutated tests are run with TESTCASE so a sibling (CSR, debounce, ...) that
# legitimately does not depend on the drain gate is not dragged in. NB the
# hung-owner-timeout tests DO depend on the gate and would also (correctly) fail
# under the mutation — they are simply not in the TESTCASE list here.
# -----------------------------------------------------------------------------
MUT_DIR := $(CURDIR)/sim_build_mutant

.PHONY: falsify falsify-grant falsify-pb-reset _mutate_check

# Break the OUTGOING gate: S_DRAIN exits the instant a switch is pending, without
# waiting for the current owner to go quiescent -> the outgoing owner's in-flight
# and queued bytes are cut off at the seam.
falsify:
	@mkdir -p $(MUT_DIR)
	@sed -E 's/cur_owner_quiet \|\| force_switch_q/1'"'"'b1 \|\| force_switch_q/' \
	    $(RTL_DIR)/clcd_kvm.sv > $(MUT_DIR)/clcd_kvm_drainmut.sv
	@grep -q "1'b1 || force_switch_q" $(MUT_DIR)/clcd_kvm_drainmut.sv \
	  || { echo "!!! the S_DRAIN mutation did not apply — the gate expression in clcd_kvm.sv changed (see line ~592). Re-derive the mutation."; exit 1; }
	@echo "=== FALSIFICATION: S_DRAIN quiescence gate REMOVED. The truncated-cycle tests MUST fail. ==="
	@$(MAKE) SIM_BUILD=$(MUT_DIR)/run_drain RTL_SRC=$(MUT_DIR)/clcd_kvm_drainmut.sv \
	         TESTCASE=test_no_truncated_cycle_harness_to_dut_at_every_phase,test_no_truncated_cycle_dut_to_harness_at_every_phase \
	  && { echo "!!! FALSIFICATION FAILED: the truncated-cycle tests PASSED with the S_DRAIN gate removed."; \
	       echo "!!! The detector is blind — do not trust a green run."; exit 1; } \
	  || echo "=== OK: the truncated-cycle tests fail with the outgoing gate removed, as they must ==="

# Break the INCOMING gate: S_GRANT commits without waiting for the incoming owner
# to be quiescent -> the pads jump into the middle of a cycle the new source was
# already running. Caught by the incoming-gate tests.
falsify-grant:
	@mkdir -p $(MUT_DIR)
	@sed -E 's/if \(tgt_owner_quiet \|\| timeout_hit\) state_d = S_OWN;/if (1'"'"'b1 || timeout_hit) state_d = S_OWN;/' \
	    $(RTL_DIR)/clcd_kvm.sv > $(MUT_DIR)/clcd_kvm_grantmut.sv
	@grep -q "if (1'b1 || timeout_hit) state_d = S_OWN;" $(MUT_DIR)/clcd_kvm_grantmut.sv \
	  || { echo "!!! the S_GRANT mutation did not apply — the gate expression changed (see line ~628)."; exit 1; }
	@echo "=== FALSIFICATION: S_GRANT (incoming) quiescence gate REMOVED. The incoming-gate tests MUST fail. ==="
	@$(MAKE) SIM_BUILD=$(MUT_DIR)/run_grant RTL_SRC=$(MUT_DIR)/clcd_kvm_grantmut.sv \
	         TESTCASE=test_incoming_gate_waits_for_the_dut_to_go_quiescent,test_incoming_gate_waits_for_the_harness_to_go_quiescent \
	  && { echo "!!! FALSIFICATION FAILED: the incoming-gate tests PASSED with the S_GRANT gate removed."; exit 1; } \
	  || echo "=== OK: the incoming-gate tests fail with the incoming gate removed, as they must ==="

# The pre-2026-09-23 reset state of the PB1 chain: pb_level 0 and the edge
# detector's copy 0 ("released"). Each sed is checked, so a refactor of those
# lines fails loudly instead of running the unmutated RTL.
falsify-pb-reset:
	@mkdir -p $(MUT_DIR)
	@sed -E -e "s/pb_level_q <= 1'b1;        \/\/ PRESSED -- see the block comment/pb_level_q <= 1'b0;/" \
	        -e "s/if \(!s_axi_aresetn\) pb_level_d1_q <= 1'b1;/if (!s_axi_aresetn) pb_level_d1_q <= 1'b0;/" \
	    $(RTL_DIR)/clcd_kvm.sv > $(MUT_DIR)/clcd_kvm_pbresetmut.sv
	@grep -qE "pb_level_q <= 1'b0;$$" $(MUT_DIR)/clcd_kvm_pbresetmut.sv \
	  && grep -q "if (!s_axi_aresetn) pb_level_d1_q <= 1'b0;" $(MUT_DIR)/clcd_kvm_pbresetmut.sv \
	  || { echo "!!! the PB1 reset mutation did not apply -- the reset lines in clcd_kvm.sv changed (see ~line 393). Re-derive it."; exit 1; }
	@echo "=== FALSIFICATION: the PB1 chain resets to 'released' (the pre-fix RTL). The held-through-reset tests MUST fail. ==="
	@$(MAKE) SIM_BUILD=$(MUT_DIR)/run_pbreset RTL_SRC=$(MUT_DIR)/clcd_kvm_pbresetmut.sv \
	         TESTCASE=test_pb1_held_through_reset_is_not_a_press,test_pb1_held_through_reset_real_timing \
	  && { echo "!!! FALSIFICATION FAILED: the held-through-reset tests PASSED with the old reset values."; exit 1; } \
	  || echo "=== OK: the held-through-reset tests fail with the old PB1 reset values, as they must ==="

clean::
	-rm -rf $(CURDIR)/sim_build_clcd_kvm* $(MUT_DIR) __pycache__ results.xml \
	        ucli.key novas.* verdiLog *.vcd *.vpd
