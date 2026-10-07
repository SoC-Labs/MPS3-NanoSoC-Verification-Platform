# Cocotb Makefile for the DUT-side display accelerator — `ahb_clcd` inside
# `nanosoc_exp_socket`, the reference block filling nanosoc's `exp_*` hole at
# 0x6000_0000. See dut_notes.md for the port list, the register map and the one
# behavioural assumption; fpga/rp/nanosoc_exp/README.md is the FROZEN contract
# this bench is written against.
#
# Usage:
#   source <repo-root>/set_env.sh    # miniconda python3.10 + cocotb 2.0.1 +
#                                    # VCS 2022.06-SP2 + license env
#   make                             # the seven checks (see test_nanosoc_lcd.py)
#   make SVA=0                       # without the bound AHB-Lite checker
#   make COVERAGE=1                  # VCS line+cond+fsm+branch+tgl
#
# Prefer `make -C tests BLOCK=nanosoc_lcd run-one` for the normal flow: it checks
# RTL readiness first (tests/common/list_benches.py).
#
# RTL_DIR override — how the mutation proof is run WITHOUT touching W2-D's files:
#
#   make RTL_DIR=/path/to/a/scratch/copy
#
# It re-points BOTH the Verilog sources and (via NANOSOC_LCD_RTL_DIR, exported
# below) the Python-side rtl_ready() skip gate, so a scratch/mutated copy of the
# RTL can be benched in place without editing anything under fpga/. Used to prove
# that test 5 ("never stalls the AHB") actually FAILS against a slave that
# back-pressures on a full FIFO — a guard that cannot fail is not a guard.

TOPLEVEL_LANG = verilog
SIM ?= vcs

# The socket lives in the RM's RTL tree, not under fpga/shell/ip: it is compiled
# INTO the nanosoc reconfigurable module, not into the static shell.
RTL_DIR ?= $(abspath $(CURDIR)/../../fpga/rp/nanosoc_exp)

# Glob, rather than list: the contract (README §11) fixes THREE files —
# nanosoc_exp_socket.sv (the frozen socket), ahb_clcd.sv (the reference
# accelerator) and clcd_core.sv (the FIFO + 8080 FSM lifted from the shell's
# clcd.sv) — but a student replacing the accelerator will add their own, and this
# bench should compile them without a Makefile edit.
VERILOG_SOURCES = $(sort $(wildcard $(RTL_DIR)/*.sv))
# clcd_core.sv is the shared 8080 engine, and it lives in the SHELL tree
# (fpga/shell/ip/clcd), not here -- ahb_clcd.sv instantiates it. Add it
# explicitly so the socket elaborates against the real, proven core rather
# than failing with "module clcd_core not found".
VERILOG_SOURCES += $(abspath $(CURDIR)/../../fpga/shell/ip/clcd/clcd_core.sv)

# TOPLEVEL is the SOCKET, not the accelerator inside it. The socket is the module
# whose port list README §2 FREEZES ("Do not add, remove or rename ports") and
# the module rp_nanosoc_wrapper.sv instantiates, so it is the boundary that
# actually faces nanosoc's bus matrix on silicon — and therefore the boundary
# where "never stall the AHB" (§4) has to hold. Benching it also covers ahb_clcd,
# which sits inside it.
TOPLEVEL ?= nanosoc_exp_socket
MODULE = test_nanosoc_lcd

# SystemVerilog (`logic`, `always_ff`, `typedef enum`) — VCS needs -sverilog.
ifeq ($(SIM),vcs)
COMPILE_ARGS += -sverilog
endif

# SVA + coverage. Unlike tests/clcd, the checker module is THIS bench's own
# (tests/common/sva/ holds only an AXI4-Lite checker, and tests/common/ is a
# shared dir this bench does not own), so both the checker and its bind file are
# added to VERILOG_SOURCES directly rather than via SVA_MODULES. bench_common.mk
# is still included for the SVA=0 escape hatch and the COVERAGE=1 toggle.
# ahb_lite_protocol_checker.sv is a candidate to promote to tests/common/sva/
# once a second AHB-Lite slave bench exists — nothing in it is ahb_clcd-specific.
include $(abspath $(CURDIR)/../common)/bench_common.mk

SVA ?= 1
ifeq ($(SVA),1)
VERILOG_SOURCES += $(CURDIR)/ahb_lite_protocol_checker.sv $(CURDIR)/bind_nanosoc_lcd.sv
ifeq ($(SIM),vcs)
COMPILE_ARGS += -assert svaext
endif
endif

# Tell the Python skip-gate (dut_presence.rtl_ready) which tree it is benching,
# so an RTL_DIR override does not leave the bench skipping the real files while
# elaborating the scratch ones.
export NANOSOC_LCD_RTL_DIR = $(RTL_DIR)

include $(shell cocotb-config --makefiles)/Makefile.sim
