# Elaborate each CSR block at the width shell_bd.tcl ACTUALLY instantiates it
# with (C_S_AXI_ADDR_WIDTH=32), not the RTL default of 12 that every other block
# bench uses, and drive base+offset as the interconnect does.
#
#   make                     # uart_bridge (default)
#   make BLOCK=dfx_ctl       # the block whose failure took the platform down
#   make BLOCK=dut_clkrst    # CLKRST  (RTL dir is clkrst/, top is dut_clkrst)
#   make BLOCK=board_gpio    # GPIO passthrough
#   make BLOCK=swd_bb        # SWD bit-bang probe (gates the SWD bring-up work)
#   make BLOCK=telem         # INA228 power telemetry
#   make BLOCK=clcd          # HX8347-D 8080 CLCD bus master (@ 0x44AC_0000)
#
# See test_csr_decode_width.py for why that parameter difference hid a bug which
# made every CSR register unreachable on silicon. ALL six shell CSR blocks ship
# at C_S_AXI_ADDR_WIDTH=32 (shell_bd.tcl) with the identical decode shape, so all
# six are covered here -- the four below were the last width-12-only blocks (they
# were WAIVED in scripts/harness_gates/param_parity_waivers.txt until this bench
# elaborated them at 32).
TOPLEVEL_LANG = verilog
SIM ?= vcs

BLOCK ?= uart_bridge
IP := $(abspath $(CURDIR)/../../fpga/shell/ip)
# The two ethernet CSR blocks (VPHY/GENCHK) live in a SIBLING tree, not under
# fpga/shell/ip -- which is exactly why check_bench_param_parity.py's
# fpga/shell/ip-rooted CSR_BLOCKS dict was structurally blind to them.
ETH := $(abspath $(CURDIR)/../../fpga/ethernet)

ifeq ($(BLOCK),uart_bridge)
  RTL_DIR         := $(IP)/uart_bridge
  # uart_bridge.sv `include`s uartbr_async_fifo.sv + swo_uart_rx.sv
  VERILOG_SOURCES := $(RTL_DIR)/uart_bridge.sv
  TOPLEVEL        := uart_bridge
  MODULE          := test_csr_decode_width
  # Literal, not $(TOPLEVEL): scripts/harness_gates/check_bench_param_parity.py
  # greps for the shipped parameterisation, and a variable hides it.
  PVALUE          := -pvalue+uart_bridge.C_S_AXI_ADDR_WIDTH=32
else ifeq ($(BLOCK),dfx_ctl)
  RTL_DIR         := $(IP)/dfx_ctl
  VERILOG_SOURCES := $(RTL_DIR)/dfx_ctl.sv
  TOPLEVEL        := dfx_ctl
  MODULE          := test_decode_width_dfx_ctl
  PVALUE          := -pvalue+dfx_ctl.C_S_AXI_ADDR_WIDTH=32
else ifeq ($(BLOCK),dut_clkrst)
  # NB: the RTL directory is clkrst/, but the Verilog top module (and the name
  # shell_bd.tcl / the parity gate use) is dut_clkrst.
  RTL_DIR         := $(IP)/clkrst
  VERILOG_SOURCES := $(RTL_DIR)/dut_clkrst.sv
  TOPLEVEL        := dut_clkrst
  MODULE          := test_decode_width_dut_clkrst
  PVALUE          := -pvalue+dut_clkrst.C_S_AXI_ADDR_WIDTH=32
else ifeq ($(BLOCK),board_gpio)
  RTL_DIR         := $(IP)/board_gpio
  VERILOG_SOURCES := $(RTL_DIR)/board_gpio.sv
  TOPLEVEL        := board_gpio
  MODULE          := test_decode_width_board_gpio
  PVALUE          := -pvalue+board_gpio.C_S_AXI_ADDR_WIDTH=32
else ifeq ($(BLOCK),swd_bb)
  RTL_DIR         := $(IP)/swd_bb
  VERILOG_SOURCES := $(RTL_DIR)/swd_bb.sv
  TOPLEVEL        := swd_bb
  MODULE          := test_decode_width_swd_bb
  PVALUE          := -pvalue+swd_bb.C_S_AXI_ADDR_WIDTH=32
else ifeq ($(BLOCK),telem)
  RTL_DIR         := $(IP)/telem
  VERILOG_SOURCES := $(RTL_DIR)/telem.sv
  TOPLEVEL        := telem
  MODULE          := test_decode_width_telem
  # Only C_S_AXI_ADDR_WIDTH needs forcing: shell_bd.tcl also sets
  # CONFIG.SIM_FAKE_DATA {0}, but that EQUALS telem.sv's RTL default (0), so
  # elaborating at the default already ships-matches it (the parity gate agrees).
  # The bench drives the ina228_* sample seam directly to make RO regs non-zero.
  PVALUE          := -pvalue+telem.C_S_AXI_ADDR_WIDTH=32
else ifeq ($(BLOCK),clcd)
  RTL_DIR         := $(IP)/clcd
  VERILOG_SOURCES := $(RTL_DIR)/clcd.sv
  TOPLEVEL        := clcd
  MODULE          := test_decode_width_clcd
  # Only C_S_AXI_ADDR_WIDTH needs forcing: the READ path (READ_PATH) defaults to
  # 0, which is the shipped write-only build, and the decode-width proof does not
  # exercise the read datapath (READ has no read side effect -- see the module).
  PVALUE          := -pvalue+clcd.C_S_AXI_ADDR_WIDTH=32
else ifeq ($(BLOCK),clcd_kvm)
  RTL_DIR         := $(IP)/clcd_kvm
  VERILOG_SOURCES := $(RTL_DIR)/clcd_kvm.sv
  TOPLEVEL        := clcd_kvm
  MODULE          := test_decode_width_clcd_kvm
  # Only C_S_AXI_ADDR_WIDTH needs forcing; every other clcd_kvm parameter (CLK_HZ,
  # the µs reset values, PB_SYNC_STAGES) equals its RTL default in shell_bd.tcl,
  # so elaborating at the default already ships-matches them. The sweep reads
  # every offset in the 64 KiB page and asserts no read side effect (README §5).
  PVALUE          := -pvalue+clcd_kvm.C_S_AXI_ADDR_WIDTH=32
else ifeq ($(BLOCK),mdio_phy_model)
  # VPHY @ 0x44A3_0000 -- the virtual-PHY register model inside
  # eth_mac_test_subsystem. RTL lives in fpga/ethernet/, NOT fpga/shell/ip/.
  #
  # READ THIS BEFORE "FIXING" THE WIDTH: unlike the eight shell CSR blocks,
  # this one is NOT currently instantiated at 32. eth_mac_test_subsystem
  # hardcodes .C_S_AXI_ADDR_WIDTH(12) and its own s_axi_vphy_* ports are a
  # fixed [11:0], so today's decode is safe by accident of that narrow port.
  # This arm is a FORWARD GUARD, not a ships-match check: the natural tidy-up
  # (add a C_S_AXI_ADDR_WIDTH parameter to the subsystem so assign_bd_address
  # can use clean 64K pages instead of 4K) would widen these ports to 32 and,
  # without the LOCAL_ADDR_W cap, silently make every VPHY register dead on
  # silicon -- bug #1, verbatim. Elaborating at 32 here means that tidy-up
  # cannot land un-guarded.
  RTL_DIR         := $(ETH)/mdio_phy_model
  VERILOG_SOURCES := $(RTL_DIR)/mdio_phy_model.sv
  TOPLEVEL        := mdio_phy_model
  MODULE          := test_decode_width_mdio_phy_model
  # mdio_phy_model.sv `include's mdio_slave.sv + phy_reg_model.sv; the
  # +incdir+$(RTL_DIR) below covers them (same shape as uart_bridge).
  PVALUE          := -pvalue+mdio_phy_model.C_S_AXI_ADDR_WIDTH=32
else ifeq ($(BLOCK),gen_checker)
  # GENCHK @ 0x44A6_0000 -- error-inject generator/checker inside
  # eth_mac_test_subsystem. Same FORWARD-GUARD rationale as mdio_phy_model
  # above (currently instantiated at 12; this arm stops a future widening from
  # shipping a dead decode).
  RTL_DIR         := $(ETH)/gen_checker
  VERILOG_SOURCES := $(RTL_DIR)/gen_checker.sv
  TOPLEVEL        := gen_checker
  MODULE          := test_decode_width_gen_checker
  PVALUE          := -pvalue+gen_checker.C_S_AXI_ADDR_WIDTH=32
else
  $(error unknown BLOCK=$(BLOCK); expected uart_bridge, dfx_ctl, dut_clkrst, board_gpio, swd_bb, telem, clcd, clcd_kvm, mdio_phy_model or gen_checker)
endif

# Separate build dirs so the two blocks do not clobber each other.
#
# RTL_DIR is included in the name because this bench is routinely re-run against a
# MUTATED copy of the RTL (see the commit for c89abce: the bench must be shown to
# FAIL on the un-fixed decode before its green result means anything). Without
# this, VCS keeps the simv built from the mutated sources -- the real .sv is older
# than the binary, so nothing rebuilds -- and the next honest run silently reports
# the mutant's result. That is the same stale-artifact class as the packaged-IP
# trap in 4e51895, and it bit this very bench.
SIM_BUILD := $(CURDIR)/sim_build_$(BLOCK)_$(notdir $(RTL_DIR))

ifeq ($(SIM),vcs)
COMPILE_ARGS += -sverilog
# THE point of this bench: match the BD's parameterisation.
COMPILE_ARGS += $(PVALUE)
endif
COMPILE_ARGS += +incdir+$(RTL_DIR)

include $(shell cocotb-config --makefiles)/Makefile.sim
