# -----------------------------------------------------------------------------
# Cocotb Makefile for the M2 XiP gate (mps3-nanosoc-platform).
#
# Proves that memory-mapped execute-in-place reads work through the
# `qspi_flash_ahb` wrapper — the exact module NanoSoC instantiates as
# u_qspi_flash_0 — against the real SST26VF064B flash VIP.
#
# Usage:
#   source <repo-root>/set_env.sh    # miniconda py3.10 + cocotb 2.0.1 + VCS
#   make                             # all four tests
#   make TESTCASE=test_xip_read_matches_flash    # just the gate
#
# The source list is the RTL side of nanosoc_m0_soc/flist/nanosoc_qspi.flist
# (the flist the QSPI-enabled SoC build uses), plus the VIP and the harness.
# Keep the two in step: if nanosoc_qspi.flist gains a file, add it here.
# -----------------------------------------------------------------------------

TOPLEVEL_LANG = verilog
SIM          ?= vcs

# cocotb's VCS shim defaults to 1ps precision; the SST26 VIP's timing checks are
# expressed in ns.  Match ahb_qspi's own bench exactly (1ns/1ns).
COCOTB_HDL_TIMEUNIT      = 1ns
COCOTB_HDL_TIMEPRECISION = 1ns

# --- site-local tree locations, NOT tracked: see tools.env.example -----------
REPO_ROOT ?= $(abspath $(CURDIR)/../..)
TOOLS_ENV ?= $(REPO_ROOT)/tools.env
-include $(TOOLS_ENV)
# --- Source trees: no workstation defaults. The tools.env names
# (SOCLABS_NANOSOC_SOC_DIR / SOCLABS_AHB_QSPI_DIR) feed this bench's own names.
NANOSOC_SOC_DIR ?= $(SOCLABS_NANOSOC_SOC_DIR)
AHB_QSPI_DIR    ?= $(SOCLABS_AHB_QSPI_DIR)
ARM_IP          ?= $(ARM_IP_LIBRARY_PATH)

CG092 := $(ARM_IP)/CG092/CG092-BU-50000-r2p0-00rel0/p_flash_cache_f0
CS101 := $(ARM_IP)/latest/Corstone-101/logical

# --- DUT: the SoC's wrapper (module qspi_flash_ahb) --------------------------
# This is the module under test. Overridable so the bench can be pointed at a
# candidate fix without touching the shared SoC tree.
QSPI_WRAPPER ?= $(NANOSOC_SOC_DIR)/src/rtl/wrappers/qspi_flash_ahb.v
VERILOG_SOURCES  = $(QSPI_WRAPPER)

# --- SoC Labs ahb_qspi IP (wrapped by the DUT) -------------------------------
VERILOG_SOURCES += $(AHB_QSPI_DIR)/logical/top_ahb_qspi/logical/top_ahb_qspi.v
VERILOG_SOURCES += $(AHB_QSPI_DIR)/logical/apb_qspi_regs/logical/apb_qspi_regs.v
VERILOG_SOURCES += $(AHB_QSPI_DIR)/logical/ahb_qspi_interface/logical/ahb_qspi_interface.sv
VERILOG_SOURCES += $(AHB_QSPI_DIR)/logical/qspi_controller/logical/qspi_controller.sv
VERILOG_SOURCES += $(AHB_QSPI_DIR)/logical/qspi_controller/logical/qspi_controller_mux.v
VERILOG_SOURCES += $(AHB_QSPI_DIR)/logical/qspi_controller/logical/qspi_clock_div.v
VERILOG_SOURCES += $(AHB_QSPI_DIR)/logical/cache_subsytem/logical/cache_subsystem.v

# --- Arm CG092 flash cache (read-only lab IP library) ------------------------
VERILOG_SOURCES += $(CG092)/logical/p_flash_cache_f0/verilog/p_flash_cache_f0_bus_logic.v
VERILOG_SOURCES += $(CG092)/logical/p_flash_cache_f0/verilog/p_flash_cache_f0_mrb.v
VERILOG_SOURCES += $(CG092)/logical/p_flash_cache_f0/verilog/p_flash_cache_f0_core.v
VERILOG_SOURCES += $(CG092)/logical/p_flash_cache_f0/verilog/p_flash_cache_f0.v
VERILOG_SOURCES += $(CG092)/logical/p_flash_cache_f0/verilog/p_flash_cache_f0_reg_block.v
VERILOG_SOURCES += $(CG092)/logical/models/cells/generic/p_flash_cache_f0_capt_sync.v
VERILOG_SOURCES += $(CG092)/logical/models/cells/generic/cdc_capt_sync.v
VERILOG_SOURCES += $(CG092)/logical/models/modules/generic/p_flash_cache_f0_static_reg.v
VERILOG_SOURCES += $(CG092)/logical/models/modules/generic/static_reg.v

# --- Cache RAM models --------------------------------------------------------
VERILOG_SOURCES += $(AHB_QSPI_DIR)/logical/cache_models/generic/cache_ram.v
VERILOG_SOURCES += $(CG092)/logical/testbench/execution_tb/verilog/p_flash_cache_f0_sp_ram.v

# --- Arm Corstone-101 (internal APB decode + the SoC's qspi_ctrl bridge) -----
VERILOG_SOURCES += $(CS101)/cmsdk_apb_slave_mux/verilog/cmsdk_apb_slave_mux.v
VERILOG_SOURCES += $(CS101)/cmsdk_ahb_to_apb/verilog/cmsdk_ahb_to_apb.v

# --- Flash VIP: the real Microchip/SST SST26VF064B behavioural model ---------
VERILOG_SOURCES += $(AHB_QSPI_DIR)/verif/VIP/SST26VF064B.v

# --- Bench harness: tristate resolution + HREADY<-HREADYOUT + backdoor -------
VERILOG_SOURCES += $(CURDIR)/qspi_xip_harness.sv

COMPILE_ARGS += +incdir+$(CG092)/logical/p_flash_cache_f0/verilog
COMPILE_ARGS += +libext+.v+.vlib+.sv

ifeq ($(SIM),vcs)
COMPILE_ARGS += -sverilog
# +neg_tchk: the SST26 VIP declares negative timing checks; without this VCS
# clamps them and floods the log. Same flag ahb_qspi's own bench uses.
COMPILE_ARGS += +neg_tchk
# The VIP corrupts its whole array to 'x on a timing violation (see its header
# note 4). We drive it in-spec (CLK_DIV>=2), but keep the checks ON so that an
# out-of-spec change is loud rather than silent.
endif

TOPLEVEL = qspi_xip_harness
MODULE   = test_qspi_xip

include $(abspath $(CURDIR)/../common)/bench_common.mk

include $(shell cocotb-config --makefiles)/Makefile.sim

clean::
	@rm -rf __pycache__ results.xml ucli.key novas.* verdiLog *.vcd *.vpd
	@rm -rf sim_build_falsify

# -----------------------------------------------------------------------------
# falsify — prove this bench can actually FAIL.
#
# A green run means nothing unless a broken DUT turns it red. This target mutates
# the wrapper with the single most likely real-world integration mistake for a
# split-tristate pad interface — a lane reversal on the input leg, QSPI_IO_i[3:0]
# wired back to front — and reruns the two tests that exercise the pads. Both
# MUST fail. (test_apb_regs_reachable is expected to keep passing under this
# mutation: it never leaves the APB. That asymmetry is the point — it shows the
# tests are sensitive to the thing each one claims to test, and not to noise.)
#
# The mutant is generated into a scratch dir; the shared SoC tree is never touched.
# -----------------------------------------------------------------------------
FALSIFY_DIR := $(CURDIR)/sim_build_falsify
MUTANT      := $(FALSIFY_DIR)/qspi_flash_ahb_mutant.v

.PHONY: falsify
falsify:
	@mkdir -p $(FALSIFY_DIR)
	@sed -E 's/\.QSPI_IO_i([[:space:]]*)\(QSPI_IO_i\)/.QSPI_IO_i\1({QSPI_IO_i[0],QSPI_IO_i[1],QSPI_IO_i[2],QSPI_IO_i[3]})/' \
	    $(QSPI_WRAPPER) > $(MUTANT)
	@grep -q 'QSPI_IO_i\[0\],QSPI_IO_i\[1\]' $(MUTANT) \
	  || { echo "!!! the mutation did not apply — the wrapper's port formatting changed"; exit 1; }
	@echo "=== FALSIFICATION: QSPI_IO_i lanes reversed in the wrapper. The pad-facing tests MUST fail. ==="
	@$(MAKE) SIM_BUILD=sim_build_falsify_run QSPI_WRAPPER=$(MUTANT) \
	         TESTCASE=test_flash_vip_jedec_id,test_xip_read_matches_flash \
	  && { echo "!!! FALSIFICATION FAILED: the bench PASSED on a wrapper with its IO lanes reversed."; \
	       echo "!!! It is not measuring the pad path. Do not trust a green run."; exit 1; } \
	  || echo "=== OK: the bench fails on the mutated wrapper, as it must ==="
