# Co-simulates rm_uart_echo with the REAL uart_bridge, joined exactly as
# shell_bd.tcl joins them. See test_uart_echo_integration.py for why the join --
# not the halves -- is what needed a bench.
TOPLEVEL_LANG = verilog
SIM ?= vcs

RM_DIR     := $(abspath $(CURDIR)/../../fpga/dfx/rms/rm_uart_echo)
BRIDGE_DIR := $(abspath $(CURDIR)/../../fpga/shell/ip/uart_bridge)

# uart_bridge.sv `include`s uartbr_async_fifo.sv + swo_uart_rx.sv.
VERILOG_SOURCES = \
  $(RM_DIR)/rm_uart_echo.sv \
  $(BRIDGE_DIR)/uart_bridge.sv \
  $(CURDIR)/uart_echo_harness.sv

ifeq ($(SIM),vcs)
COMPILE_ARGS += -sverilog
endif
COMPILE_ARGS += +incdir+$(BRIDGE_DIR) +incdir+$(RM_DIR)

TOPLEVEL = uart_echo_harness
MODULE   = test_uart_echo_integration

include $(shell cocotb-config --makefiles)/Makefile.sim
