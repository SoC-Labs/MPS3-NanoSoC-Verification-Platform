# tests/hostio4_hotswap — L2a: can a DFX decoupler park the hostio4 target?
#
#   source set_env.sh && make -C tests/hostio4_hotswap
#
# In the harness the hostio4 CONTROLLER is inside the reconfigurable partition
# (nanoSoC drives ioreq1/ioreq2, receives ioack). The TARGET is in the static
# shell. A partial reconfiguration deletes the controller mid-transaction while
# the target stays live and the decoupler holds the boundary at a constant safe
# value. This bench asks whether any such constant returns the target to idle.
#
# RTL: the arch_tech copy — see ../hostio4_golden/README.md "Provenance".

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
            -top tb_hostio4_park

.PHONY: check-rtl all park swap clean

all: park swap

simv_park: check-rtl tb_hostio4_park.sv
	@$(VCS) $(VCS_ARGS) $(HOSTIO4_RTL)/target_rtl/*.v tb_hostio4_park.sv \
	    -o simv_park -l comp_park.log > /dev/null
	@echo "  compile OK  (RTL: $(HOSTIO4_RTL)/target_rtl)"

# The bench is exhaustive over 9 FSM states x 4 static (ioreq1,ioreq2) values.
# It self-checks that the forced state encoding still matches the RTL, so a
# drifted encoding cannot masquerade as "everything parks".
#
# We assert the CURRENT, MEASURED answer: no static value parks the target, but
# toggling ioreq2 does. If the RTL ever changes so that a static value works,
# this target fails loudly and the harness requirement can be relaxed.
park: simv_park
	@./simv_park -l park.log > /dev/null
	@sed -n '/self-check/,/FLOAT /p' park.log
	@grep -q "PARK STATIC IMPOSSIBLE" park.log || { \
	   echo "  *** a static decoupler value now parks the target."; \
	   echo "      Good news, but the harness requirement below can change."; exit 1; }
	@grep -q "PARK ESCAPE OK" park.log || { \
	   echo "  *** toggling ioreq2 no longer parks the target either."; exit 1; }
	@grep -q "FLOAT UNSAFE" park.log || { \
	   echo "  *** a floating iodata4 no longer poisons fsm_state."; \
	   echo "      Good news, but requirement (2) below can be relaxed."; exit 1; }
	@echo ""
	@echo "  HARNESS REQUIREMENTS, all three measured above:"
	@echo "   1. The DFX decoupler CANNOT park hostio4_target with any constant."
	@echo "      (0,0) is the best and still wedges 5 of 9 states. So the shell must"
	@echo "      RESET the target on every swap -- or drive 3 ioreq2 toggles."
	@echo "   2. The decoupler must DRIVE iodata4, not leave it floating: with"
	@echo "      ioreq2 high, RXC1 X-merges the state register off a 'z'."
	@echo "   3. It must hold ioreq1 LOW. Holding it high drags even an idle target"
	@echo "      out of TXST into RXC1."
	@echo ""
	@echo "  Why it bites: ioack_o = fsm_state[0], so a wedged target holds ack"
	@echo "  asserted, and hostio4_link_cdc's neg_ack_stuck control proved a stuck"
	@echo "  ack hangs the controller forever."
	@echo ""
	@echo "  Confirmed on a LIVE link by L2b (make swap): the next DUT deadlocks"
	@echo "  from exactly these 5 states, under either decoupler ioack value."
	@echo "  So the hang is INTERMITTENT -- it depends where the swap lands."

# --- L2b: the same question, on a LIVE link ---------------------------------
# L2a forces FSM states; that is an analysis. This runs real traffic on all four
# channels, performs a swap in the middle, and asks whether the link comes back.
# The swap policy is the variable. See tb_hostio4_swap_live.sv for the sequence.
STIM  := ../hostio4_common/axis_stim.sv
RUN   := ../hostio4_common/run.sh
SWAPENV := SIMV=./simv_swap PASSMARK="LIVE SWAP PASS" FAILMARK="LIVE SWAP FAIL"

simv_swap: check-rtl tb_hostio4_swap_live.sv $(STIM)
	@$(VCS) -full64 -sverilog -timescale=1ns/1ps -q -notice +lint=none \
	    -top tb_hostio4_swap_live \
	    $(HOSTIO4_RTL)/soc_rtl/*.v $(HOSTIO4_RTL)/target_rtl/*.v \
	    $(STIM) tb_hostio4_swap_live.sv -o simv_swap -l comp_swap.log > /dev/null
	@echo "  compile OK  (live-swap bench)"

# The swap must land MID-TRANSACTION; +swap_state pins exactly where. Swapping
# once the stream has drained finds the target idle in TXST and everything
# "passes" -- see the header of tb_hostio4_swap_live.sv.
#
# MEASURED with pol_none (target left alone), swapping from each FSM state, on
# the RTL nanoSoC is actually built from:
#
#            decoupler drives RP ioack=0     ioack=1
#   TXST     recovers                        recovers
#   RXC1     DEADLOCK                        DEADLOCK
#   RXDH     DEADLOCK                        DEADLOCK
#   RXDL     DEADLOCK                        DEADLOCK
#   RXDZ     recovers                        recovers
#   TXSZ     recovers                        recovers
#   TXCZ     DEADLOCK                        DEADLOCK
#   TXDH     DEADLOCK                        DEADLOCK
#   TXDL     recovers                        recovers
#
# Exactly L2a's five wedging states, under BOTH ack values. The decoupler's
# ioack safe value does not rescue anything: the shipped controller boots into
# OFFZ ("off/deselected from reset") and does not spontaneously start a
# transaction, so it never emits the ioreq edges that could unwedge the target.
#
# (An earlier revision of this bench ran against a DIFFERENT hostio4 tree whose
# controller FSM has no OFFZ state. There, RXC1+ack0 "recovered by luck". That
# result was an artifact of the wrong RTL. See ../hostio4_golden/README.md.)
#
# The failure is always a clean deadlock, never silent corruption.
#
# pol_treset and pol_escape recover from ALL NINE states under BOTH ack values
# (54 runs; `make matrix`).
BASE := +n1=64 +n2=32 +swap_at=40

swap: simv_swap
	@echo "== L2b: a DFX swap landing MID-TRANSACTION on a live link =="
	@echo "   left alone, the target deadlocks the next DUT from 5 of 9 states:"
	@$(SWAPENV) $(RUN) fail swap_none_RXDH_ack0 $(BASE) +swap_state=RXDH +pol_none +dec_ioack=0
	@$(SWAPENV) $(RUN) fail swap_none_TXDH_ack0 $(BASE) +swap_state=TXDH +pol_none +dec_ioack=0
	@echo "   and the decoupler's ioack safe value rescues NOTHING -- the shipped"
	@echo "   controller boots into OFFZ and emits no ioreq edges to unwedge it:"
	@$(SWAPENV) $(RUN) fail swap_none_RXC1_ack1 $(BASE) +swap_state=RXC1 +pol_none +dec_ioack=1
	@$(SWAPENV) $(RUN) fail swap_none_RXC1_ack0 $(BASE) +swap_state=RXC1 +pol_none +dec_ioack=0
	@echo "   a state that is NOT a wedging state still recovers, as it must:"
	@$(SWAPENV) $(RUN) pass swap_none_TXSZ_ack0 $(BASE) +swap_state=TXSZ +pol_none +dec_ioack=0
	@echo "   resetting the target makes it immune everywhere:"
	@$(SWAPENV) $(RUN) pass swap_treset_RXDH_ack1 $(BASE) +swap_state=RXDH +pol_treset +dec_ioack=1
	@$(SWAPENV) $(RUN) pass swap_treset_RXC1_ack1 $(BASE) +swap_state=RXC1 +pol_treset +dec_ioack=1
	@echo "   so does the ioreq2 escape sequence:"
	@$(SWAPENV) $(RUN) pass swap_escape_RXDH_ack1 $(BASE) +swap_state=RXDH +pol_escape +dec_ioack=1
	@echo ""
	@echo "  REQUIREMENT, now demonstrated on a live link: the shell must RESET"
	@echo "  hostio4_target on every swap -- or drive the 3 ioreq2 escape toggles."
	@echo "  The decoupler's ioack safe value is irrelevant; it rescues nothing."

# The full 9-state x {ack0,ack1} x {none,treset,escape} sweep, for humans.
matrix: simv_swap
	@echo "== full swap matrix (54 runs; the Makefile gate gives a subset) =="
	@for pol in pol_none pol_treset pol_escape; do \
	   for ack in 0 1; do \
	     printf "  %-11s ack=%s : " $$pol $$ack; \
	     for st in TXST RXC1 RXDH RXDL RXDZ TXSZ TXCZ TXDH TXDL; do \
	       ./simv_swap -l mx.log $(BASE) +swap_state=$$st +$$pol +dec_ioack=$$ack >/dev/null 2>&1; \
	       if grep -q "LIVE SWAP PASS" mx.log; then printf "%-5s. " $$st; \
	       else printf "%-5sX " $$st; fi; done; echo ""; done; done
	@echo "  (. = link recovered,  X = did not)"

clean:
	@rm -rf csrc simv_park simv_park.daidir simv_swap simv_swap.daidir \
	        *.log ucli.key vc_hdrs.h .vlogansetup.*
	@echo "  cleaned"
