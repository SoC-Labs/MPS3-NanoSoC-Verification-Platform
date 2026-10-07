# tests/usr_access_rd/Makefile — USRACC, the fabric's own build identity.
#
# Elaborates fpga/shell/ip/usr_access_rd/usr_access_rd.sv at the parameterisation
# shell_bd.tcl ACTUALLY ships (C_S_AXI_ADDR_WIDTH=32, not the RTL default of 12)
# and drives base+offset, so this bench doubles as this block's bug-#1 proof and
# no scripts/harness_gates/param_parity_waivers.txt entry is needed.
#
# The Xilinx USR_ACCESSE2 primitive is supplied by usr_accesse2_model.sv in THIS
# directory rather than by a simulation branch inside the RTL -- see that file's
# header. The stamped value comes from a plusarg, so one elaboration serves the
# agreeing case, the skewed case and the stamped-zero case.
#
#   make                      the block, stamped 0x01000001
#
# CONTROLS -- each MUST BE SEEN TO FAIL, or the green run above proves nothing:
#
#   make control-value    the primitive is stamped one value and the bench
#                         asserts another. If the read really comes from the
#                         primitive, this FAILS.
#   make control-silicon  the PRE-FIX RTL (control/usr_access_rd_datavalid_gated.sv,
#                         capture gated on DATAVALID) against the UG570 Fig 7-9
#                         model (DATAVALID = one CFGCLK pulse during
#                         configuration, before reset release). Reproduces the
#                         2026-09-22 silicon read: MAGIC ok, VALUE 0, VALID 0.
#                         If the model really is what silicon does, this FAILS.
#
# USRACC_DV_MODE=level selects the PREVIOUS primitive model (DATAVALID a level
# after reset) -- the fixed RTL must pass under both; the old RTL passed ONLY
# under `level`, which is how the bug escaped to silicon.
#
#   make control-decode   rebuild against a copy of the RTL whose LOCAL_ADDR_W
#                         is reverted to C_S_AXI_ADDR_WIDTH -- bug #1 exactly.
#                         If the page decode is real, this FAILS.
#
# NOT a control, and recorded here because it was TRIED and came out green:
# driving the registers at offset-without-base. It cannot fail. LOCAL_ADDR_W
# caps the decode at the block's own 64 KiB page, so araddr[31:16] is ignored BY
# DESIGN -- page selection is the interconnect's job, not the slave's. Only a
# mutation of the decode itself reproduces bug #1, which is why the control
# below edits RTL rather than addresses.
#
# Usage: source <repo-root>/set_env.sh first (VCS + the pinned cocotb).
TOPLEVEL_LANG = verilog
SIM ?= vcs

MPS3_ROOT := $(abspath $(CURDIR)/../..)
IP        := $(MPS3_ROOT)/fpga/shell/ip/usr_access_rd

# The value the SIMULATED fabric is stamped with, and the value the bench
# asserts. Same by default; control-value drives them apart.
USRACC_STAMPED ?= 0x01000001
USRACC_EXPECT  ?= $(USRACC_STAMPED)
export USRACC_STAMPED
export USRACC_EXPECT

TOPLEVEL = usr_access_rd
MODULE   = test_usr_access_rd

VERILOG_SOURCES = $(IP)/usr_access_rd.sv $(CURDIR)/usr_accesse2_model.sv

# NB the literal -pvalue, not $(TOPLEVEL): check_bench_param_parity.py greps for
# the shipped parameterisation and a variable hides it.
PVALUE        := -pvalue+usr_access_rd.C_S_AXI_ADDR_WIDTH=32
COMPILE_ARGS  += -sverilog $(PVALUE)

# The primitive model reads the stamp from here. `$(shell ...)` strips the 0x so
# $value$plusargs("%h") parses it.
PLUSARGS += +USR_ACCESS=$(patsubst 0x%,%,$(USRACC_STAMPED))
# Primitive model: pulse (UG570 Fig 7-9, default) or level (the old model).
USRACC_DV_MODE ?= pulse
PLUSARGS += +USRACC_DV_MODE=$(USRACC_DV_MODE)

# SVA + coverage (A5): bind the reusable AXI4-Lite protocol checker onto this
# slave, and honour COVERAGE=1. See tests/common/bench_common.mk.
SVA_MODULES    := axi4lite_protocol_checker.sv
SVA_BIND_FILES := bind_usr_access_rd.sv
include $(abspath $(CURDIR)/../common)/bench_common.mk

include $(shell cocotb-config --makefiles)/Makefile.sim

# AFTER the cocotb include, and never before: the FIRST target in a makefile is
# its default goal, so declaring these above turns a bare `make` into an
# infinitely self-recursing control run. (Observed, not theorised.)
MUTANT_DIR := $(CURDIR)/mutant

.PHONY: control-value control-decode control-silicon
control-value:
	@echo "== CONTROL: the value really comes from the primitive. MUST FAIL. =="
	$(MAKE) sim USRACC_STAMPED=0xA5A50F0F USRACC_EXPECT=0x01000001

# The mutant is written to a directory of its own and built in a SIM_BUILD of
# its own: VCS will happily re-run a simv built from the unmutated RTL, and a
# control that silently tested the good build is worse than no control. The
# grep guard is there for the same reason -- if the sed ever stops matching (an
# RTL reword), this target must fail LOUDLY rather than pass green.
control-decode:
	@echo "== CONTROL: the page decode is real (bug #1). MUST FAIL. =="
	@mkdir -p $(MUTANT_DIR)
	@sed 's|localparam int LOCAL_ADDR_W = (C_S_AXI_ADDR_WIDTH < 16) ? C_S_AXI_ADDR_WIDTH : 16;|localparam int LOCAL_ADDR_W = C_S_AXI_ADDR_WIDTH;|' \
	     $(IP)/usr_access_rd.sv > $(MUTANT_DIR)/usr_access_rd.sv
	@grep -q 'LOCAL_ADDR_W = C_S_AXI_ADDR_WIDTH;$$' $(MUTANT_DIR)/usr_access_rd.sv \
	  || { echo "MUTATION DID NOT APPLY -- this control proves nothing; fix the sed"; exit 1; }
	$(MAKE) sim IP=$(MUTANT_DIR) SIM_BUILD=$(CURDIR)/sim_build_mutant

# The pre-fix RTL is a committed snapshot, not a sed mutation: the fix rewrote
# the capture, so there is no one-line edit that recovers the old behaviour.
# Own SIM_BUILD for the same reason as control-decode.
CONTROL_DIR := $(CURDIR)/control
control-silicon:
	@echo "== CONTROL: the DATAVALID-gated RTL reproduces the silicon read. MUST FAIL. =="
	@test -f $(CONTROL_DIR)/usr_access_rd_datavalid_gated.sv \
	  || { echo "control RTL missing -- this control proves nothing"; exit 1; }
	@mkdir -p $(MUTANT_DIR)/silicon
	@cp $(CONTROL_DIR)/usr_access_rd_datavalid_gated.sv $(MUTANT_DIR)/silicon/usr_access_rd.sv
	$(MAKE) sim IP=$(MUTANT_DIR)/silicon SIM_BUILD=$(CURDIR)/sim_build_silicon USRACC_DV_MODE=pulse
