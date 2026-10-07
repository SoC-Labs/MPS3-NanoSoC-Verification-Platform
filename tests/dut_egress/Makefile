# tests/dut_egress/Makefile — the DUT's Ethernet RETURN path (DUTEGR).
#
# TWO ARMS, and `make` with no BLOCK runs BOTH:
#
#   make BLOCK=csr   fpga/shell/ip/dut_egress/dut_egress.sv on its own, at the
#                    parameterisation shell_bd.tcl ACTUALLY ships
#                    (C_S_AXI_ADDR_WIDTH=32, not the RTL default of 12 — bug #1:
#                    the interconnect hands the slave the SYSTEM address, so a
#                    block benched at 12 is not the block that ships). Drives
#                    base+offset and the raw capture port directly, so the FIFO,
#                    the drop counters and the destructive DATA port are proven
#                    without a millisecond of RMII serialisation.
#
#   make BLOCK=e2e   the WHOLE loop, entirely over registers: tb_dut_egress.sv
#                    wires eth_mac_test_subsystem + dut_egress exactly as
#                    shell_bd.tcl SECTION 5 does WITH the inject-side BD delta
#                    (dut_egress_0/inj_m_* -> the bridge's mgmt_s_*), and the
#                    bench plays the DUT MAC on the RMII partition pins. A UDP
#                    datagram is written to DUTEGR.TX_DATA and COMMITted, crosses
#                    the bridge and the link-partner MAC onto RMII, the DUT
#                    echoes it, and the echo is read back BYTE FOR BYTE out of
#                    DUTEGR.DATA with both Internet checksums re-verified.
#
# CONTROLS — each must be SEEN TO FAIL, or the green run above proves nothing:
#
#   make control-byteorder   the bench compares against the frame with its bytes
#                            REVERSED. If the comparison is real, this FAILS.
#   make control-nodrop      the overflow scenario asserts DROP_FULL == 0 after
#                            deliberately overrunning the FIFO. If the drop
#                            counter is real, this FAILS.
#   make control-tornframe   (inject, handover §6 (a)) the csr arm against RTL
#                            whose READ SIDE STARTS BEFORE COMMIT (mutate.py:
#                            publish-on-push + start on any byte). FAILS in
#                            test_m: bytes leave before COMMIT.
#   make control-norollback  (inject, §6 (b)) the csr arm against RTL with the
#                            ROLLBACK REMOVED. FAILS in test_l / test_m: a
#                            discarded frame's bytes go out with the next one.
#   make control-raw         (inject, §6 (c)) the e2e arm with RAW FORCED TO 1:
#                            no FCS is appended, and the DUT MAC model FAILS the
#                            frame on its FCS check.
#   make control-noflushed   (inject, amendment A2) the csr arm against RTL where
#                            TX_FLUSHED @0x38 reads 0 (the register removed).
#                            FAILS in test_o: flushed frames are uncounted.
#   make control-flushasreject (A2) the pre-A2 accounting: flushed frames
#                            counted in TX_REJECT, 0x38 reads 0. FAILS in test_o.
#
# Usage: source <repo-root>/set_env.sh first (VCS + the pinned cocotb).
TOPLEVEL_LANG = verilog
SIM ?= vcs

BLOCK ?=

MPS3_ROOT  := $(abspath $(CURDIR)/../..)
IP_SHIPPED := $(MPS3_ROOT)/fpga/shell/ip/dut_egress
# The RTL dir is overridable ONLY so a control can point the identical bench at
# a MUTATED copy (mutate.py writes it under sim_build_mutant/, never over the
# shipped file). MUTANT names that copy, and goes into SIM_BUILD below.
DUTEGR_RTL ?= $(IP_SHIPPED)
MUTANT     ?=
IP         := $(DUTEGR_RTL)
ETH        := $(MPS3_ROOT)/fpga/ethernet
MUT_DIR    := $(CURDIR)/sim_build_mutant

ifeq ($(BLOCK),)
# No arm named: run every arm, each in its own sub-make (each needs a different
# TOPLEVEL and SIM_BUILD, which are fixed at parse time).
.PHONY: all clean
all:
	@$(MAKE) --no-print-directory BLOCK=csr
	@$(MAKE) --no-print-directory BLOCK=e2e

clean:
	@$(MAKE) --no-print-directory BLOCK=csr clean
	@$(MAKE) --no-print-directory BLOCK=e2e clean
	-rm -rf $(MUT_DIR) $(CURDIR)/sim_build_csr_*

.PHONY: control-byteorder control-nodrop control-tornframe control-norollback control-raw \
        control-noflushed control-flushasreject
control-byteorder:
	@echo "== CONTROL: byte order. This run MUST FAIL. =="
	$(MAKE) BLOCK=e2e PLUSARGS=+EGRESS_CONTROL=byteorder
control-nodrop:
	@echo "== CONTROL: drop counter. This run MUST FAIL. =="
	$(MAKE) BLOCK=csr PLUSARGS=+EGRESS_CONTROL=nodrop

# The inject side's controls (docs/planning/HANDOVER_DUT_INJECT.md §6). The two
# RTL mutants run ONLY the tests that police the removed property
# (COCOTB_TEST_FILTER — a regex with no '|' or quotes in it: cocotb's
# Makefile.vcs splices it into a shell line UNQUOTED, and a '|' there turns the
# run into a "command not found" that looks exactly like a failing control),
# so the red is attributable; each gets its own mutant dir
# and SIM_BUILD, because VCS will happily re-run a simv built from the shipped
# RTL otherwise and a control that quietly tested the good build is worse than
# none. mutate.py exits non-zero if a mutation does not apply.
control-tornframe:
	@echo "== CONTROL (a): the inject read side starts before COMMIT. This run MUST FAIL. =="
	python3 $(CURDIR)/mutate.py tornframe $(IP_SHIPPED) $(MUT_DIR)/tornframe
	$(MAKE) BLOCK=csr DUTEGR_RTL=$(MUT_DIR)/tornframe MUTANT=tornframe \
	  COCOTB_TEST_FILTER=test_m_
control-norollback:
	@echo "== CONTROL (b): the inject rollback is removed. This run MUST FAIL. =="
	python3 $(CURDIR)/mutate.py norollback $(IP_SHIPPED) $(MUT_DIR)/norollback
	$(MAKE) BLOCK=csr DUTEGR_RTL=$(MUT_DIR)/norollback MUTANT=norollback \
	  COCOTB_TEST_FILTER='test_[lm]_'
control-noflushed:
	@echo "== CONTROL (A2): TX_FLUSHED removed (0x38 reads 0). This run MUST FAIL. =="
	python3 $(CURDIR)/mutate.py noflushed $(IP_SHIPPED) $(MUT_DIR)/noflushed
	$(MAKE) BLOCK=csr DUTEGR_RTL=$(MUT_DIR)/noflushed MUTANT=noflushed \
	  COCOTB_TEST_FILTER=test_o_
control-flushasreject:
	@echo "== CONTROL (A2): flushed frames counted as rejects (pre-A2). This run MUST FAIL. =="
	python3 $(CURDIR)/mutate.py flushasreject $(IP_SHIPPED) $(MUT_DIR)/flushasreject
	$(MAKE) BLOCK=csr DUTEGR_RTL=$(MUT_DIR)/flushasreject MUTANT=flushasreject \
	  COCOTB_TEST_FILTER=test_o_
control-raw:
	@echo "== CONTROL (c): RAW forced to 1 end to end; the DUT model must flag a bad FCS. MUST FAIL. =="
	$(MAKE) BLOCK=e2e PLUSARGS=+INJECT_CONTROL=raw \
	  COCOTB_TEST_FILTER=test_a_

else ifeq ($(BLOCK),csr)

# NB the literal -pvalue below, not $(TOPLEVEL):
# scripts/harness_gates/check_bench_param_parity.py greps for the shipped
# parameterisation and a variable hides it.
TOPLEVEL        := dut_egress
MODULE          := test_dut_egress_csr
# dut_egress.sv `include's dutegr_cfifo.sv — list only the top, +incdir the dir.
VERILOG_SOURCES := $(IP)/dut_egress.sv
PVALUE          := -pvalue+dut_egress.C_S_AXI_ADDR_WIDTH=32
COMPILE_ARGS    += -sverilog $(PVALUE) +incdir+$(IP)
# DATA_DEPTH/FRAME_DEPTH/MAX_FRAME are left at their RTL defaults, which ARE the
# values shell_bd.tcl ships (2048/16/1536) — so this arm elaborates the shipped
# sizing too. If the BD ever ships a different size, the parity gate goes red.
CUSTOM_COMPILE_DEPS += $(IP)/dutegr_cfifo.sv
# Mutant name in the build-dir name: this bench is re-run against a MUTATED
# copy of the RTL (a green result means nothing until the mutation is seen to
# fail), and VCS will happily reuse a simv built from the mutant otherwise.
SIM_BUILD       := $(CURDIR)/sim_build_csr$(if $(MUTANT),_$(MUTANT))

include $(shell cocotb-config --makefiles)/Makefile.sim

else ifeq ($(BLOCK),e2e)

TOPLEVEL        := tb_dut_egress
MODULE          := test_dut_egress_e2e
# The composition, in the same order as shell_bd.tcl SECTION 5 builds it.
VERILOG_SOURCES := \
  $(ETH)/rmii_phy_if/rmii_phy_if.sv \
  $(ETH)/link_partner_mac/link_partner_mac.sv \
  $(ETH)/bridge/eth_bridge_3port.sv \
  $(ETH)/gen_checker/gen_checker.sv \
  $(ETH)/mdio_phy_model/mdio_phy_model.sv \
  $(ETH)/eth_mac_test_subsystem.sv \
  $(IP)/dut_egress.sv \
  $(CURDIR)/tb_dut_egress.sv
COMPILE_ARGS    += -sverilog
COMPILE_ARGS    += +incdir+$(ETH)/mdio_phy_model +incdir+$(IP)
# The `include'd helpers of mdio_phy_model and dut_egress: named so an
# incremental build cannot reuse a stale simv after one of them changes (the
# off-by-one MDIO read-data escape of 2026-07-11).
CUSTOM_COMPILE_DEPS += $(ETH)/mdio_phy_model/mdio_slave.sv \
                       $(ETH)/mdio_phy_model/phy_reg_model.sv \
                       $(IP)/dutegr_cfifo.sv
SIM_BUILD       := $(CURDIR)/sim_build_e2e

include $(shell cocotb-config --makefiles)/Makefile.sim

else
$(error unknown BLOCK=$(BLOCK); expected csr or e2e, or no BLOCK to run both)
endif
