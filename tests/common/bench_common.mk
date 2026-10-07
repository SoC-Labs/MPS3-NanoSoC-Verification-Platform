# -----------------------------------------------------------------------------
# bench_common.mk — shared per-block cocotb bench fragment (A5 verification).
#
# Two orthogonal add-ons the per-block Makefiles opt into:
#
#   1. SVA binds. Set SVA_MODULES (checker module .sv files) and SVA_BIND_FILES
#      (bind_*.sv files), both bare basenames under tests/common/sva/. When
#      SVA=1 (default) they are appended to VERILOG_SOURCES and VCS gets
#      `-assert svaext`. `make SVA=0` builds the bench with no bound checkers
#      (e.g. to A/B a suspected SVA false-fire, or for a non-VCS simulator that
#      chokes on a construct). A firing assertion $fatal()s the bench RED; run
#      with `SIM_ARGS=+SVA_NOFATAL` to keep going and collect every firing.
#
#   2. VCS code coverage. `make COVERAGE=1` adds `-cm line+cond+fsm+branch+tgl`
#      to BOTH the compile (COMPILE_ARGS) and run (SIM_ARGS) steps and writes an
#      isolated coverage DB per block under $(CURDIR)/cov_<block>.vdb, which the
#      top-level `make -C tests coverage` target merges + urg-reports. Coverage
#      is VCS-only (the flags are `-cm*`); it is a no-op under SIM=questa.
#
# Include this AFTER setting VERILOG_SOURCES / COMPILE_ARGS / SIM / TOPLEVEL and
# the SVA_* lists, but BEFORE `include $(shell cocotb-config --makefiles)/…`.
# -----------------------------------------------------------------------------

_BENCH_COMMON_SELF := $(lastword $(MAKEFILE_LIST))
_COMMON_DIR        := $(abspath $(dir $(_BENCH_COMMON_SELF)))
_SVA_DIR           := $(_COMMON_DIR)/sva

# --- 1. SVA bind wiring ------------------------------------------------------
SVA ?= 1
ifeq ($(SVA),1)
ifneq ($(strip $(SVA_MODULES)$(SVA_BIND_FILES)),)
VERILOG_SOURCES += $(addprefix $(_SVA_DIR)/,$(SVA_MODULES) $(SVA_BIND_FILES))
ifeq ($(SIM),vcs)
COMPILE_ARGS += -assert svaext
endif
endif
endif

# --- 2. VCS code-coverage toggle --------------------------------------------
COVERAGE ?= 0
ifeq ($(COVERAGE),1)
ifeq ($(SIM),vcs)
_CM_METRICS := line+cond+fsm+branch+tgl
_CM_DIR     := $(CURDIR)/cov_$(TOPLEVEL).vdb
COMPILE_ARGS += -cm $(_CM_METRICS) -cm_dir $(_CM_DIR) -cm_name $(TOPLEVEL)
SIM_ARGS     += -cm $(_CM_METRICS) -cm_dir $(_CM_DIR) -cm_name $(TOPLEVEL)
endif
endif
