# Cocotb Makefile for RM `clcd_demo` -- the DUT-side display proof.
# See dut_notes.md for the DUT's port list and the elaboration choices, and
# tests/README.md for the overall verification strategy.
#
# Usage:
#   source <repo-root>/set_env.sh    # miniconda py3.10 + cocotb 2.0.1 + VCS
#   make                             # DEFAULT: MODE=card -- the whole bench at
#                                    #   a bench-sized 32x16 frame
#   make MODE=fullgeom               # the SHIPPING 320x240 geometry, run only
#                                    #   far enough to prove the address window
#                                    #   equals the firmware table's
#   make MODE=control                # THE CONTROL: rebuild with 8080 phases
#                                    #   BELOW the tunnel's floor; green only if
#                                    #   the bench REJECTS it
#   make all-modes                   # all three, in order
#
# WHY THERE ARE THREE ELABORATIONS. A 320x240 frame is 153,600 bytes -- about
# four million dut_clk cycles per repaint, and the counter test needs three
# repaints. The card tests therefore run at a shrunk geometry, which is safe
# because every layout boundary in clcd_demo_gen.sv is a FRACTION of W/H. What
# a shrunk frame cannot check is that the address window the RM computes lands
# exactly on the firmware table's own values, so MODE=fullgeom does that at the
# shipping geometry and stops as soon as it has.

TOPLEVEL_LANG = verilog
SIM ?= vcs

RM_DIR    := $(abspath $(CURDIR)/../../fpga/rp/clcd_demo)
SHELL_IP  := $(abspath $(CURDIR)/../../fpga/shell/ip/clcd)

# clcd_core.sv is the SHARED 8080 engine -- the same module the harness CLCD
# block instantiates and the same one lighting the panel on this board today.
# The RM does not vendor a copy (fpga/rp/clcd_demo/filelist.tcl says why), so
# the bench must read it from the shell.
VERILOG_SOURCES  = $(SHELL_IP)/clcd_core.sv
VERILOG_SOURCES += $(RM_DIR)/clcd_demo_gen.sv
VERILOG_SOURCES += $(RM_DIR)/rp_clcd_demo_wrapper.sv

TOPLEVEL = rp_clcd_demo_wrapper
MODULE   = test_clcd_demo

ifeq ($(SIM),vcs)
COMPILE_ARGS += -sverilog
endif

# --- the three elaborations -------------------------------------------------
# CLK_HZ is what the RM BELIEVES dut_clk is; it scales the firmware init table's
# MILLISECOND delays (215 ms of them) down to something a simulator can sit
# through. The bench's actual clock period is fixed at 20 ns.
MODE ?= card

ifeq ($(MODE),card)
  PIX_W ?= 32
  PIX_H ?= 16
  CLK_HZ ?= 1000
  SIM_BUILD = sim_build_card
  COCOTB_RESULTS_FILE = $(CURDIR)/results_card.xml
endif

ifeq ($(MODE),fullgeom)
  PIX_W ?= 0
  PIX_H ?= 0
  CLK_HZ ?= 1000
  MODULE = test_clcd_demo_fullgeom
  SIM_BUILD = sim_build_fullgeom
  COCOTB_RESULTS_FILE = $(CURDIR)/results_fullgeom.xml
endif

ifeq ($(MODE),control)
  # THE MUTATION: clcd_core's own SHELL defaults (CS_SETUP=2, WR_LO=WR_HI=4).
  # They are correct at the 100 MHz shell clock and BELOW the floor in
  # docs/contracts/dut-display-tunnel.md section 5 at 50 MHz -- i.e. this is the
  # mistake a maintainer actually makes, not an invented one.
  PIX_W ?= 32
  PIX_H ?= 16
  CLK_HZ ?= 1000
  CS_SETUP ?= 2
  WR_LO ?= 4
  WR_HI ?= 4
  SIM_BUILD = sim_build_control
  COCOTB_RESULTS_FILE = $(CURDIR)/results_control.xml
  export CLCD_DEMO_EXPECT_VIOLATION = 1
endif

CS_SETUP ?= 8
WR_LO ?= 8
WR_HI ?= 8

# The bench reads the elaboration back out of the environment, so the -pvalue+
# line below and the Python's expectations cannot disagree.
export CLCD_DEMO_PIX_W = $(PIX_W)
export CLCD_DEMO_PIX_H = $(PIX_H)
export CLCD_DEMO_CLK_HZ = $(CLK_HZ)

ifeq ($(SIM),vcs)
COMPILE_ARGS += -pvalue+$(TOPLEVEL).DUT_CLK_HZ=$(CLK_HZ)
COMPILE_ARGS += -pvalue+$(TOPLEVEL).PIX_W=$(PIX_W)
COMPILE_ARGS += -pvalue+$(TOPLEVEL).PIX_H=$(PIX_H)
COMPILE_ARGS += -pvalue+$(TOPLEVEL).CS_SETUP=$(CS_SETUP)
COMPILE_ARGS += -pvalue+$(TOPLEVEL).WR_LO=$(WR_LO)
COMPILE_ARGS += -pvalue+$(TOPLEVEL).WR_HI=$(WR_HI)
COMPILE_ARGS += -pvalue+$(TOPLEVEL).REQ_AFTER_MS=1
endif

include $(shell cocotb-config --makefiles)/Makefile.sim

.PHONY: all-modes
all-modes:
	$(MAKE) MODE=card
	$(MAKE) MODE=fullgeom
	$(MAKE) MODE=control

# cocotb's clean:: only sweeps the CURRENT invocation's $(SIM_BUILD).
clean::
	-rm -rf $(CURDIR)/sim_build_card $(CURDIR)/sim_build_fullgeom \
	        $(CURDIR)/sim_build_control $(CURDIR)/results_*.xml
