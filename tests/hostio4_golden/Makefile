# tests/hostio4_golden — L0: hostio4 vendor golden reference under VCS
#
#   source set_env.sh && make -C tests/hostio4_golden          # golden + neg control
#   make -C tests/hostio4_golden golden                        # golden only
#   make -C tests/hostio4_golden neg                           # negative control only
#   make -C tests/hostio4_golden clean
#
# WHY: before we build ANY hostio4 integration, establish that the vendor RTL
# passes the vendor's own reference stimulus on OUR simulator. Cheapest possible
# ground truth; needs no repo RTL and no contract change.
#
# WHICH RTL: the arch_tech copy — the version nanoSoC is actually built from
# (fpga/monolithic/filelist.tcl reads ${NANOSOC_ARCH_TECH_DIR}/rtl/hostio4/...).
# NOT the standalone hostio4 repo, whose soc_rtl is
# mid-refactor and cannot elaborate as packaged — see README.md "Provenance".

SHELL := /bin/bash

# check-rtl is defined before `all`, so pin the default goal explicitly --
# otherwise a bare `make` builds only the guard, runs nothing, and exits 0.
.DEFAULT_GOAL := all

# The RTL the DUT contains. Override if your arch_tech checkout differs.
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
	@test -f "$(HOSTIO4_TB)/tb_hostio4.v" || { \
	  echo "  hostio4 testbench not found at: $(HOSTIO4_TB)"; \
	  echo "  Set HOSTIO4_HOME to the standalone hostio4 checkout (tools.env)"; exit 1; }
# The testbench + stimulus + reference live only in the standalone repo.
# Set HOSTIO4_HOME (tools.env) to that checkout; HOSTIO4_TB derives from it.
HOSTIO4_HOME ?=
HOSTIO4_TB  ?= $(HOSTIO4_HOME)/tb

VCS       := vcs
VCS_ARGS  := -full64 -sverilog -timescale=1ns/1ps -q -notice +lint=none -top tb_hostio4

# tb_axi_stream_io8_rxd_check_file.v is deliberately NOT compiled: tb_hostio4
# never instantiates it, so VCS would elaborate it as a second top and its
# initial block would $fopen a nonexistent "rxd-ref.log". Harmless, but noisy.
TB_SRCS := $(HOSTIO4_TB)/tb_axi_stream_io8_buffer.v \
           $(HOSTIO4_TB)/tb_axi_stream_io8_txd_from_file.v \
           $(HOSTIO4_TB)/tb_axi_stream_io8_rxd_to_file.v \
           $(HOSTIO4_TB)/tb_hostio4.v

# The tb transmits 360 bytes; the reference file carries a 361st trailing
# newline that is never sent. Compare the first 360 bytes.
XMIT_BYTES := 360
CAPTURES   := C_rxd0_out.log C_rxd1_out.log T_rxd0_out.log T_rxd1_out.log

.PHONY: check-rtl all golden neg clean

all: golden neg
	@echo ""
	@echo "  L0 GOLDEN: PASS, and the comparison fails on a 1-byte corruption."

simv: check-rtl $(TB_SRCS)
	@cp $(HOSTIO4_TB)/asc-test.txt $(HOSTIO4_TB)/asc-test-ref.log .
	@$(VCS) $(VCS_ARGS) +define+ADP_FILE=\"asc-test.txt\" \
	    $(HOSTIO4_RTL)/soc_rtl/*.v $(HOSTIO4_RTL)/target_rtl/*.v \
	    $(TB_SRCS) -o simv -l comp.log > /dev/null
	@echo "  compile OK  (RTL: $(HOSTIO4_RTL))"

golden: simv
	@./simv -l run.log > /dev/null
	@grep -E 'count *=' run.log | sed 's/^/    /'
	@fail=0; for f in $(CAPTURES); do \
	   if head -c $(XMIT_BYTES) asc-test-ref.log | cmp -s - $$f; then \
	     printf "    %-16s byte-exact over all $(XMIT_BYTES) transmitted bytes\n" $$f; \
	   else printf "    %-16s MISMATCH\n" $$f; fail=1; fi; done; \
	 [ $$fail -eq 0 ] || { echo "  GOLDEN FAILED"; exit 1; }
	@echo "  GOLDEN: all 4 streams match the vendor reference"

# A check that cannot fail is worthless. Corrupt one stimulus byte; the
# captures MUST diverge from the reference.
neg: simv
	@cp asc-test.txt asc-test.txt.orig
	@sed -i '3s/^3/X/' asc-test.txt
	@./simv -l run_neg.log > /dev/null
	@bad=0; for f in C_rxd0_out.log T_rxd0_out.log; do \
	   if head -c $(XMIT_BYTES) asc-test-ref.log | cmp -s - $$f; then \
	     echo "    *** $$f STILL MATCHES — the check is blind ***"; bad=1; \
	   else echo "    caught: $$f diverges from reference"; fi; done; \
	 mv asc-test.txt.orig asc-test.txt; \
	 ./simv -l run.log > /dev/null; \
	 [ $$bad -eq 0 ] || { echo "  NEGATIVE CONTROL FAILED"; exit 1; }
	@echo "  NEG CONTROL: a 1-byte stimulus corruption is detected"

clean:
	@rm -rf csrc simv simv.daidir *.log *.txt ucli.key vc_hdrs.h .vlogansetup.*
	@echo "  cleaned"
