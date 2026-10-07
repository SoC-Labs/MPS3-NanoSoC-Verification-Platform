# Cocotb Makefile for the M1 console-RX gate (mps3-nanosoc-platform).
#
# Proves whether a host byte can reach the DUT's console receiver — the one
# hardware unknown between this platform and a MicroPython REPL. See
# test_uart2_rx_path.py's docstring for the full path under test.
#
# DUT = nanosoc_ss_systemctrl (the real subsystem), which contains BOTH
#   * nanosoc_pin_mux      — the suspect (drives uart2_rxd), and
#   * the soc_peripheral region with the real Arm cmsdk_apb_uart as UART2.
# This is the smallest scope that contains the whole bug and still exercises
# the genuine CMSDK receiver, and its P1_IN[15:0] pad bus is a real input port.
#
# Usage:
#   source <repo-root>/set_env.sh   # miniconda py3.10 + cocotb 2.0.1 + VCS
#   make
#
# RTL sources come from the nanosoc_m0_soc tree (the tree the MPS3 build
# actually uses — SOCLABS_NANOSOC_SOC_DIR in fpga/dfx/Makefile), NOT the
# nanoSoC-refactor checkout.

TOPLEVEL_LANG = verilog
SIM ?= vcs

# --- site-local tree locations, NOT tracked: see tools.env.example -----------
REPO_ROOT ?= $(abspath $(CURDIR)/../..)
TOOLS_ENV ?= $(REPO_ROOT)/tools.env
-include $(TOOLS_ENV)
NANOSOC_SOC_DIR ?= $(SOCLABS_NANOSOC_SOC_DIR)
ARCH_TECH       := $(NANOSOC_SOC_DIR)/nanosoc_arch_tech
ARM_IP          ?= $(ARM_IP_LIBRARY_PATH)
CORSTONE101     := $(ARM_IP)/Corstone-101/BP210-r1p1-00rel0/BP210-BU-00000-r1p1-00rel0/logical

# --- DUT hierarchy (real RTL, read-only from the shared submodule) -----------
#
# The two files carrying the UART2-RX fix are overridable, so the bench can be
# re-run against the PRE-FIX RTL to prove it actually fails there. That
# falsification is what makes a green run mean something — see the README.
#   make falsify        (runs the pristine files straight out of git)
PINMUX  ?= $(ARCH_TECH)/rtl/src/control/verilog/nanosoc_pin_mux.v
SYSCTRL ?= $(ARCH_TECH)/rtl/src/subsystems/systemctrl/nanosoc_ss_systemctrl.v

VERILOG_SOURCES  = $(SYSCTRL)
VERILOG_SOURCES += $(PINMUX)
VERILOG_SOURCES += $(ARCH_TECH)/rtl/src/control/verilog/nanosoc_clkctrl.v
VERILOG_SOURCES += $(ARCH_TECH)/rtl/src/regions/soc_peripheral/nanosoc_region_soc_peripheral.v
VERILOG_SOURCES += $(ARCH_TECH)/rtl/src/regions/soc_peripheral/nanosoc_soc_peripheral_apb_ss.v
VERILOG_SOURCES += $(ARCH_TECH)/rtl/src/regions/soc_peripheral/nanosoc_soc_peripheral_decode.v
VERILOG_SOURCES += $(ARCH_TECH)/rtl/src/regions/soc_peripheral/nanosoc_sysctrl.v

# --- Arm CMSDK / Corstone-101 leaf IP (read-only lab IP library) -------------
VERILOG_SOURCES += $(CORSTONE101)/cmsdk_apb_uart/verilog/cmsdk_apb_uart.v
VERILOG_SOURCES += $(CORSTONE101)/cmsdk_apb_timer/verilog/cmsdk_apb_timer.v
VERILOG_SOURCES += $(CORSTONE101)/cmsdk_apb_dualtimers/verilog/cmsdk_apb_dualtimers.v
VERILOG_SOURCES += $(CORSTONE101)/cmsdk_apb_dualtimers/verilog/cmsdk_apb_dualtimers_frc.v
VERILOG_SOURCES += $(CORSTONE101)/cmsdk_apb_watchdog/verilog/cmsdk_apb_watchdog.v
VERILOG_SOURCES += $(CORSTONE101)/cmsdk_apb_watchdog/verilog/cmsdk_apb_watchdog_frc.v
VERILOG_SOURCES += $(CORSTONE101)/cmsdk_apb_slave_mux/verilog/cmsdk_apb_slave_mux.v
VERILOG_SOURCES += $(CORSTONE101)/cmsdk_apb_subsystem/verilog/cmsdk_apb_test_slave.v
VERILOG_SOURCES += $(CORSTONE101)/cmsdk_apb_subsystem/verilog/cmsdk_irq_sync.v
VERILOG_SOURCES += $(CORSTONE101)/cmsdk_ahb_to_apb/verilog/cmsdk_ahb_to_apb.v
VERILOG_SOURCES += $(CORSTONE101)/cmsdk_ahb_gpio/verilog/cmsdk_ahb_gpio.v
VERILOG_SOURCES += $(CORSTONE101)/cmsdk_ahb_gpio/verilog/cmsdk_ahb_to_iop.v
VERILOG_SOURCES += $(CORSTONE101)/cmsdk_iop_gpio/verilog/cmsdk_iop_gpio.v
VERILOG_SOURCES += $(CORSTONE101)/cmsdk_ahb_slave_mux/verilog/cmsdk_ahb_slave_mux.v
VERILOG_SOURCES += $(CORSTONE101)/cmsdk_ahb_default_slave/verilog/cmsdk_ahb_default_slave.v

# Generated discovery/ID block (from the SoC build — same tree the FPGA uses)
DISCOVERY := $(NANOSOC_SOC_DIR)/build_soc/rtl/nanosoc_ahb_interconnect_discovery
VERILOG_SOURCES += $(DISCOVERY)/nanosoc_ahb_interconnect_discovery_pkg.sv
VERILOG_SOURCES += $(DISCOVERY)/nanosoc_ahb_interconnect_discovery.sv
VERILOG_SOURCES += $(DISCOVERY)/nanosoc_ahb_interconnect_discovery_apb_wrapper.sv

# --- Bench-only stub (see its header for why this cannot affect uart2_rxd) ---
VERILOG_SOURCES += $(CURDIR)/socdebug_usrt_control_stub.sv

# --- Bench harness: ties HREADY<-HREADYOUT and pins the GPIO port directions --
VERILOG_SOURCES += $(CURDIR)/uart2_rx_harness.sv

# CMSDK headers (dualtimers/watchdog `include` their defines)
COMPILE_ARGS += +incdir+$(CORSTONE101)/cmsdk_apb_dualtimers/verilog
COMPILE_ARGS += +incdir+$(CORSTONE101)/cmsdk_apb_watchdog/verilog

ifeq ($(SIM),vcs)
COMPILE_ARGS += -sverilog
# The pin mux and sysctrl are plain Verilog-2001; the stub is SV. -sverilog
# covers both. +lint=TFIPC-L would flag the pin mux's intentionally
# unconnected uart0/uart1 ports, which are not under test here.
endif

TOPLEVEL = uart2_rx_harness
MODULE   = test_uart2_rx_path

include $(abspath $(CURDIR)/../common)/bench_common.mk

include $(shell cocotb-config --makefiles)/Makefile.sim

# -----------------------------------------------------------------------------
# falsify — run this exact bench against the PRE-FIX RTL, straight out of git.
#
# A passing bench proves nothing unless it can fail. This target extracts the
# pristine nanosoc_pin_mux.v / nanosoc_ss_systemctrl.v from the arch_tech
# submodule's HEAD (i.e. before the UART2-RX fix) into a scratch dir and runs
# the identical tests against them. Both tests MUST fail. If they pass, the
# bench is not measuring what it claims to.
# -----------------------------------------------------------------------------
FALSIFY_DIR := $(CURDIR)/sim_build_prefix

.PHONY: falsify
falsify:
	@mkdir -p $(FALSIFY_DIR)
	@cd $(ARCH_TECH) && git show HEAD:rtl/src/control/verilog/nanosoc_pin_mux.v \
	    > $(FALSIFY_DIR)/nanosoc_pin_mux.v
	@cd $(ARCH_TECH) && git show HEAD:rtl/src/subsystems/systemctrl/nanosoc_ss_systemctrl.v \
	    > $(FALSIFY_DIR)/nanosoc_ss_systemctrl.v
	@echo "=== FALSIFICATION: running the bench against PRE-FIX RTL (both tests must FAIL) ==="
	@$(MAKE) SIM_BUILD=sim_build_falsify \
	         PINMUX=$(FALSIFY_DIR)/nanosoc_pin_mux.v \
	         SYSCTRL=$(FALSIFY_DIR)/nanosoc_ss_systemctrl.v \
	  && (echo "!!! FALSIFICATION FAILED: the bench PASSED on buggy RTL — it cannot see the bug"; exit 1) \
	  || echo "=== OK: the bench fails on pre-fix RTL, as it must ==="
