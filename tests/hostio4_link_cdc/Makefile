# tests/hostio4_link_cdc — L1: hostio4 across two INDEPENDENT clocks
#
#   source set_env.sh && make -C tests/hostio4_link_cdc     # everything
#   make -C tests/hostio4_link_cdc ratios     # clock-ratio sweep
#   make -C tests/hostio4_link_cdc neg        # negative controls
#   make -C tests/hostio4_link_cdc skew       # iodata4-vs-strobe budget
#   make -C tests/hostio4_link_cdc sync       # what the 2FFs actually buy
#   make -C tests/hostio4_link_cdc masking    # harmonic ratios hide skew bugs
#   make -C tests/hostio4_link_cdc clean
#
# L0 (tests/hostio4_golden) runs the vendor bench, which drives BOTH ends from
# one oscillator (C_clk = clk, T_clk = !clk): same frequency, zero drift. It
# cannot say anything about the hostio4_*_sync 2FF synchronisers. This bench
# makes the clocks genuinely unrelated and skews the wires.
#
# Every number asserted below was MEASURED by bisection, not assumed. See
# README.md for the raw sweeps and for what this bench still cannot prove.
#
# RTL: the arch_tech copy, same as L0 — see ../hostio4_golden/README.md
# "Provenance" for why the standalone repo must not be used.

SHELL := /bin/bash

# check-rtl is defined before `all`, so pin the default goal explicitly --
# otherwise a bare `make` builds only the guard, runs nothing, and exits 0.
.DEFAULT_GOAL := all

# --- which hostio4? ---------------------------------------------------------
# Resolve EXACTLY as the project's own build does. fpga/monolithic/filelist.tcl:
#     NANOSOC_M0_SOC_SRC, else SOCLABS_NANOSOC_SOC_DIR (tools.env); no workstation default
#     set NANOSOC_ARCH_TECH_DIR ${NANOSOC_M0_SOC_SRC}/nanosoc_arch_tech
# and the top-level Makefile's ARCH_TECH_SRC resolves to a byte-identical copy.
#
# DO NOT hardcode a path here. There are FOUR hostio4 trees on this machine and
# only this one is the RTL nanoSoC is built from. An earlier version of these
# benches pointed at nanoSoC-refactor/ethernet-subsystem-ahb-m0plus/... which no
# repo file references at all -- and it is a DIFFERENT design: its controller FSM
# lacks the OFFZ reset state, and that single difference flipped a swap result.
# See ../hostio4_golden/README.md "Provenance".
# --- site-local tree locations, NOT tracked: see tools.env.example -----------
REPO_ROOT ?= $(abspath $(CURDIR)/../..)
TOOLS_ENV ?= $(REPO_ROOT)/tools.env
-include $(TOOLS_ENV)
NANOSOC_M0_SOC_SRC ?= $(SOCLABS_NANOSOC_SOC_DIR)
HOSTIO4_RTL        ?= $(NANOSOC_M0_SOC_SRC)/nanosoc_arch_tech/rtl/hostio4

# Fail loudly and usefully rather than with a wall of VCS "module not found".
check-rtl:
	@test -d "$(HOSTIO4_RTL)/soc_rtl" -a -d "$(HOSTIO4_RTL)/target_rtl" || { \
	  echo "  hostio4 RTL not found at: $(HOSTIO4_RTL)"; \
	  echo "  Set NANOSOC_M0_SOC_SRC (as fpga/monolithic/filelist.tcl does), e.g."; \
	  echo "    make NANOSOC_M0_SOC_SRC=/path/to/nanosoc_m0_soc"; exit 1; }

VCS      := vcs
VCS_ARGS := -full64 -sverilog -timescale=1ns/1ps -q -notice +lint=none \
            -top tb_hostio4_link_cdc

NBYTES ?= 64
R      := ../hostio4_common/run.sh

.PHONY: check-rtl all ratios neg skew sync masking clean

all: ratios neg skew sync masking
	@echo ""
	@echo "  L1 PASS: byte-exact across unrelated clocks; the bench detects a stuck"
	@echo "           ack, a reversed bus, over-budget skew, and a missing 2FF."

STIM := ../hostio4_common/axis_stim.sv

simv: check-rtl tb_hostio4_link_cdc.sv $(STIM)
	@$(VCS) $(VCS_ARGS) \
	    $(HOSTIO4_RTL)/soc_rtl/*.v $(HOSTIO4_RTL)/target_rtl/*.v \
	    $(STIM) tb_hostio4_link_cdc.sv -o simv -l comp.log > /dev/null
	@echo "  compile OK  (RTL: $(HOSTIO4_RTL))"

# --- clock-ratio sweep -----------------------------------------------------
# Every case must be byte-exact on all 4 channels, both directions, with random
# backpressure at both ends. Distinct LFSR seeds per channel, so a channel
# swap or a loopback would also be caught.
#   near_unity   — 10.0 vs 10.001 ns: the phase slides slowly through every
#                  possible sampling position. The classic 2FF killer.
#   dut_slow     — nanoSoC (controller) 25 MHz, shell (target) 100 MHz
#   dut_fast     — the inverse
#   coprime      — no repeating phase relationship at all
ratios: simv
	@echo "== clock ratios (nbytes=$(NBYTES)/channel) =="
	@$(R) pass ratio_coprime      +cper=10.0  +tper=7.3    +nbytes=$(NBYTES)
	@$(R) pass ratio_near_unity   +cper=10.0  +tper=10.001 +nbytes=$(NBYTES)
	@$(R) pass ratio_dut_slow     +cper=40.0  +tper=10.0   +nbytes=$(NBYTES)
	@$(R) pass ratio_dut_fast     +cper=10.0  +tper=40.0   +nbytes=$(NBYTES)
	@$(R) pass ratio_phase_offset +cper=10.0  +tper=7.3 +tskew=3.17 +nbytes=$(NBYTES)

# --- negative controls -----------------------------------------------------
# A bench that cannot fail proves nothing.
neg: simv
	@echo "== negative controls (each MUST fail) =="
	@$(R) fail neg_ack_stuck  +cper=10.0 +tper=7.3 +nbytes=32 +neg_ack_stuck
	@$(R) fail neg_data_rev   +cper=10.0 +tper=7.3 +nbytes=32 +neg_data_rev
	@$(R) fail neg_over_skew  +cper=10.0 +tper=7.3 +nbytes=32 +ddata_t=30.0

# --- iodata4-vs-strobe skew budget -----------------------------------------
# Both ends capture data from the RAW iodata4 wires and qualify the capture
# with a 2FF-synchronised strobe (target: ioreq2_s; controller: ioack_s). The
# 2FF latency IS the settling budget for the data wires. The controller FSM
# even says so: "async-data safe by protocol".
#
# MEASURED, by bisecting ddata with dreq/dack = 0:
#   target-receive   cliff at 2*T_per exactly.   T=7.3 -> 14.7 pass / 15.0 fail
#                                                T=20.3 -> 40.0 pass / 42.0 fail
#                    and independent of C_per (checked at C = 5, 10, 40).
#   controller-receive  >= 2*C_per, plus extra slack from the ioclken (C/2)
#                    capture grid and the target's data-hold window, so the
#                    observed cliff is ratio-dependent: 2.0*C at C=10 but
#                    2.2-2.4*C at C=20. TREAT 2*C AS THE SAFE BOUND.
#
# The brackets below straddle the budget from both sides, so they trip if the
# synchroniser depth changes in EITHER direction (1FF shrinks it, 3FF grows it).
skew: simv
	@echo "== iodata4 skew budget vs the 2FF strobe =="
	@echo "   target-receive, T=7.3ns => budget 2*T = 14.6 ns"
	@$(R) pass skew_t_under_14ns +cper=10.0 +tper=7.3 +nbytes=32 +ddata_t=14.0
	@$(R) fail skew_t_over_16ns  +cper=10.0 +tper=7.3 +nbytes=32 +ddata_t=16.0
	@echo "   ...and it scales with the RECEIVER's clock: T=20.3ns => 2*T = 40.6 ns"
	@$(R) pass skew_t_under_40ns +cper=10.0 +tper=20.3 +nbytes=32 +ddata_t=40.0
	@$(R) fail skew_t_over_42ns  +cper=10.0 +tper=20.3 +nbytes=32 +ddata_t=42.0
	@echo "   ...not with the TRANSMITTER's (same cliff at C=5 and C=40)"
	@$(R) pass skew_t_txclk_fast +cper=5.0  +tper=7.3 +nbytes=32 +ddata_t=14.0
	@$(R) fail skew_t_txclk_slow +cper=40.0 +tper=7.3 +nbytes=32 +ddata_t=15.0
	@echo "   controller-receive, C=10.0ns => safe bound 2*C = 20.0 ns"
	@$(R) pass skew_c_under_19ns +cper=10.0 +tper=7.3 +nbytes=32 +ddata_c=19.0
	@$(R) fail skew_c_over_26ns  +cper=10.0 +tper=7.3 +nbytes=32 +ddata_c=26.0

# --- what the synchronisers actually buy -----------------------------------
# testmode=1 bypasses every 2FF (sig_s = testmode ? sig_a : sig_r[2]).
#
# THE UNCOMFORTABLE RESULT, asserted here so nobody has to rediscover it:
# with zero wire skew, bypassing every synchroniser STILL PASSES. An RTL
# simulator has no metastability, so a missing 2FF is invisible unless you
# also inject skew. Two independent clocks are NOT sufficient to catch it.
#
# Inject 2 ns of skew and the bypassed design dies, while the real one shrugs
# off 12 ns. That 0.2 ns -> 14.6 ns collapse is the synchronisers' whole job,
# and it is the only way this bench can see them at all.
sync: simv
	@echo "== what the 2FF synchronisers buy (testmode=1 bypasses them) =="
	@$(R) pass sync_bypass_noskew  +cper=10.0 +tper=7.3 +nbytes=32 +testmode
	@echo "     ^ PASSES. Two clocks alone cannot detect a missing synchroniser."
	@$(R) fail sync_bypass_skew02  +cper=10.0 +tper=7.3 +nbytes=32 +testmode +ddata_t=0.2
	@$(R) pass sync_real_skew12    +cper=10.0 +tper=7.3 +nbytes=32           +ddata_t=12.0
	@echo "     ^ budget collapses from 14.6 ns to under 0.2 ns without the 2FFs."

# --- harmonic ratios mask skew violations ----------------------------------
# Found the hard way while bisecting: at cper=10.0/tper=20.0 (exactly 1:2) the
# sampling phase never slides, so a 42 ns skew violation -- 2 ns past the
# budget -- sails through. Detune the target clock by 1.5% and it fails at
# once. This is why `ratios` above must contain a non-harmonic pair, and why
# nobody should ever "simplify" these periods to round numbers.
masking: simv
	@echo "== a harmonic clock ratio HIDES an over-budget skew =="
	@$(R) pass mask_harmonic_1to2  +cper=10.0 +tper=20.0 +nbytes=32 +ddata_t=42.0
	@echo "     ^ 42 ns skew, budget 40 ns, yet it passes: phase is frozen at 1:2."
	@$(R) fail mask_detuned_1p5pct +cper=10.0 +tper=20.3 +nbytes=32 +ddata_t=42.0
	@echo "     ^ same skew, target clock detuned 1.5%: caught immediately."

clean:
	@rm -rf csrc simv simv.daidir *.log ucli.key vc_hdrs.h .vlogansetup.*
	@echo "  cleaned"
