# tests/identify_iice — sim-vs-hardware IICE trace comparison harness.
#
# OWNED BY THE INTEGRATOR. Do not edit if you own Makefile.sim or Makefile.compare
# — put your targets there. See INTERFACES.md §5.
#
# Design: docs/planning/IICE_SIM_VS_HW_TRACE_PLAN.md
# Contract: INTERFACES.md (frozen)

SHELL     := /bin/bash
HERE      := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
BUILD     := $(HERE)/build
GOLDEN    := $(HERE)/golden
PYTHON    ?= python3

# The selftest manifest is the CI gate: tiny DUT, runs in seconds, no nanosoc
# dependency. The nanosoc manifest must always GENERATE, but is only simulated
# on demand (the reference bench is minutes-to-tens-of-minutes).
MANIFEST  ?= $(HERE)/signals_selftest.yaml

export HERE BUILD GOLDEN PYTHON MANIFEST

.PHONY: all gen sim-fsdb fake-hw compare check negctl clean help

all: check

help:
	@echo "make gen        - regenerate .idc / shadow .sv / signal_map.tsv; fail if != golden/"
	@echo "make sim-fsdb   - run the selftest sim with the shadow IICE -> build/sim_iice.fsdb"
	@echo "make fake-hw    - derive a synthetic build/hw_iice.fsdb (no board, no Identify)"
	@echo "make compare    - crop + mangle + nCompare -> build/compare_report.txt"
	@echo "make negctl     - NEGATIVE CONTROL: perturb one bit, assert compare FAILS"
	@echo "make check      - gen + sim-fsdb + fake-hw + compare + negctl  (the gate)"
	@echo "make hw-fsdb    - pull a REAL trace off the board (needs lease + bitstream)"

$(BUILD):
	@mkdir -p $(BUILD)

# --- stream A: generators -----------------------------------------------------
gen: | $(BUILD)
	$(PYTHON) $(HERE)/gen_idc.py    --manifest $(MANIFEST) --out $(BUILD)
	$(PYTHON) $(HERE)/gen_shadow.py --manifest $(MANIFEST) --out $(BUILD)
	$(PYTHON) $(HERE)/gen_mangle.py --manifest $(MANIFEST) --out $(BUILD)
	@# The nanosoc manifest is never simulated in CI, but ALL THREE artifacts must
	@# still generate — otherwise a nanosoc-only generator bug hides until board day.
	@mkdir -p $(BUILD)/nanosoc
	$(PYTHON) $(HERE)/gen_idc.py    --manifest $(HERE)/signals_nanosoc.yaml --out $(BUILD)/nanosoc
	$(PYTHON) $(HERE)/gen_shadow.py --manifest $(HERE)/signals_nanosoc.yaml --out $(BUILD)/nanosoc
	$(PYTHON) $(HERE)/gen_mangle.py --manifest $(HERE)/signals_nanosoc.yaml --out $(BUILD)/nanosoc
	@echo "gen: nanosoc manifest also generates OK (all 3 artifacts)"
	@# anti-drift: generated output must match the committed goldens
	@bash -c 'fail=0; for f in $$(cd $(GOLDEN) && find . -type f); do \
	   if ! diff -q "$(GOLDEN)/$$f" "$(BUILD)/$$f" >/dev/null 2>&1; then \
	     echo "GOLDEN DRIFT: $$f"; diff -u "$(GOLDEN)/$$f" "$(BUILD)/$$f" | head -40; fail=1; fi; \
	 done; exit $$fail'
	@echo "GEN OK"

# --- stream B: simulation -----------------------------------------------------
-include $(HERE)/Makefile.sim

# --- stream C: compare --------------------------------------------------------
-include $(HERE)/Makefile.compare

# --- interactive evaluation (NOT part of `check`) ------------------------------
# `make check` answers "is the harness correct?". It cannot answer "is this
# tooling USEFUL?" -- for that see `make evaluate` (EVALUATING.md), which injects
# a realistic divergence, checks the report actually names it, and emits a joined
# FSDB to open in nWave.
-include $(HERE)/Makefile.evaluate

# --- the gate -----------------------------------------------------------------
check: gen sim-fsdb fake-hw compare negctl
	@echo "IDENTIFY_IICE_HARNESS_CHECK_OK"

clean:
	rm -rf $(BUILD) nCompareLog verdiLog novas.* verdi* *.fsdb *.vpd DVEfiles \
	       simv simv.daidir csrc ucli.key vc_hdrs.h AN.DB .vlogansetup.* \
	       __pycache__ .pytest_cache
