# tests/Makefile — top-level cocotb bench discovery/runner + pure-Python
# unit-test runner for mps3-nanosoc-platform. See tests/README.md for the
# full verification strategy and the phase-gate -> bench mapping table.
#
# Usage:
#   make            # RTL-readiness summary, then run every READY cocotb
#                    bench + the pure-Python suite; prints a final tally
#   make list       # just the readiness summary (no simulator invoked)
#   make pytest     # only the pure-Python helper/integration tests (no
#                    simulator, no RTL needed — safe to run right now)
#   make BLOCK=gen_checker   # run one cocotb bench directly (still gated
#                             on that block's RTL readiness)
#   make clean      # clean sim artifacts in every per-block dir
#
# VCS licensing/env: `source set_env.sh` (repo root — exists since the
# W-SIM pass, closes I24) before running any cocotb target here. It pins
# miniconda python3.10 + cocotb 2.0.1 + VCS 2022.06-SP2 + license env;
# see its header for the cocotb version decision and Questa fallback.

SHELL := /bin/bash
PY ?= python3

# --- site-local external source trees, for the benches whose DUT is NOT in this
#     repository (jtag_dap_bringup, jtag_chain, uart2_rx_path, qspi_xip) -------
#
# WHY THIS IS HERE. common/list_benches.py decides readiness partly on whether
# those external trees resolve, and it reads the ENVIRONMENT to do it -- it
# deliberately does not parse tools.env itself, because a Python
# re-implementation of make's include and override precedence would be a second,
# drifting copy of the same answer (see that file's docstring). So make does the
# make part, once, here: tools.env is included exactly as the bench Makefiles
# and fpga/dfx/Makefile include it, and the variables are exported so that
# `make list` and the sub-make that actually runs the bench cannot disagree.
#
# Exported only if non-empty: `export VAR` on an undefined variable exports an
# EMPTY one, which would look like "set to nothing" downstream and turn a
# perfectly runnable bench into a SKIP.
#
# KNOWN WART, not fixed here: tools.env spells the SoC checkout
# SOCLABS_NANOSOC_SOC_DIR while tests/uart2_rx_path and tests/qspi_xip read
# NANOSOC_SOC_DIR and bake a workstation default. Those two Makefiles are not
# this lane's to change; list_benches.py reads each bench's own variable, so it
# reports the truth either way.
REPO_ROOT ?= $(abspath $(CURDIR)/..)
TOOLS_ENV ?= $(REPO_ROOT)/tools.env
-include $(TOOLS_ENV)
EXTERNAL_TREE_VARS := NANOSOC_MULTICORE_HOME SOCLABS_CORESIGHT_SOC400_TECH_DIR \
                      SOCLABS_SLCOREM0_TECH_DIR ARM_IP_LIBRARY_PATH \
                      NANOSOC_SOC_DIR AHB_QSPI_DIR
$(foreach v,$(EXTERNAL_TREE_VARS),$(if $($(v)),$(eval export $(v))))
# Every per-block bench dir with a Makefile. `all:` runs the READY ones off
# list_benches.py; this list drives `clean` (and documents the full set). Keep
# it in sync with `python3 common/list_benches.py`: a READY bench missing here
# still runs under `all`, but its sim_build/ is not swept by `make clean` — the
# housekeeping gap that let eth_mac_subsystem / csr_decode_width /
# uart_echo_integration / rm_uart_echo drift out of the runner unnoticed.
BENCH_DIRS := sim_smoke mdio_phy_model gen_checker rmii_phy_if bridge \
	link_partner_mac eth_mac_subsystem dut_egress eth_ss_bringup uart_axis_shim \
	dfx_ctl clkrst board_gpio uart_bridge csr_decode_width swd_bb telem usr_access_rd \
	clcd clcd_kvm nanosoc_lcd uart_echo_integration rm_uart_echo uart2_rx_path qspi_xip \
	jtag_dap_bringup jtag_chain rm_socscope usd_spi
# jtag_chain (23814a9) was in NEITHER this list nor common/list_benches.py, so
# it never appeared in `make list`, was never run by `make all`, and its
# sim_build/ was never swept by `make clean` -- the same housekeeping gap this
# comment already records for four other benches. common/test_list_benches.py
# now asserts the two lists agree, so the next one fails instead of drifting.

# Benches wired for VCS code coverage (they `include ../common/bench_common.mk`,
# so `make COVERAGE=1` instruments them — the eight AXI4-Lite slaves the SVA
# checker also binds onto). See `make coverage` below.
COVERAGE_BENCHES := clkrst dfx_ctl board_gpio swd_bb telem uart_bridge gen_checker mdio_phy_model uart_axis_shim eth_ss_bringup clcd usd_spi
COV_REPORT := cov_report

.PHONY: all list pytest coverage $(BENCH_DIRS) clean

all: list
	@echo "== running READY cocotb benches (see 'list' above for SKIPs) =="
	@ready=0; fail=0; \
	while read -r status name _rtldir; do \
		if [ "$$status" = "READY" ]; then \
			echo "---- $$name ----"; \
			if $(MAKE) -C $$name; then ready=$$((ready+1)); else fail=$$((fail+1)); fi; \
		fi; \
	done < <($(PY) common/list_benches.py); \
	echo "=========================================="; \
	echo " cocotb benches: $$ready ran ok, $$fail failed"; \
	echo "=========================================="
	@$(MAKE) pytest

list:
	@$(PY) common/list_benches.py --summary

# Run a single block directly, e.g. `make BLOCK=gen_checker run-one`.
.PHONY: run-one
run-one:
	@if [ -z "$(BLOCK)" ]; then echo "usage: make BLOCK=<name> run-one"; exit 1; fi
	@status=$$($(PY) common/list_benches.py | awk -v b=$(BLOCK) '$$2==b {print $$1}'); \
	if [ "$$status" = "READY" ]; then \
		$(MAKE) -C $(BLOCK); \
	else \
		echo "[SKIP] $(BLOCK): RTL not landed/de-stubbed yet — see $(BLOCK)/dut_notes.md"; \
	fi

pytest:
	@echo "== pure-Python helper + integration tests (no simulator, no RTL needed) =="
	$(PY) -m pytest common integration board_gpio edge firmware_logic -q

# VCS code coverage (line+cond+fsm+branch+tgl): run each instrumented bench
# with COVERAGE=1 and urg-report it INDIVIDUALLY (the eight benches are eight
# different top-level designs — a single cross-design urg merge silently drops
# all but the first, so we report per-block and tabulate the DUT-module scores).
# VCS-only (the `-cm*` flags are Synopsys); a no-op under SIM=questa.
#   source set_env.sh && make -C tests coverage
coverage:
	@command -v urg >/dev/null 2>&1 || { echo "urg not on PATH — source set_env.sh first"; exit 1; }
	@echo "== VCS code coverage (line+cond+fsm+branch+tgl) over: $(COVERAGE_BENCHES) =="
	@fail=0; \
	printf "\n%-16s %7s %7s %7s %7s %7s %7s %7s\n" BLOCK SCORE LINE COND TOGGLE FSM BRANCH ASSERT; \
	printf -- "----------------------------------------------------------------------\n"; \
	for b in $(COVERAGE_BENCHES); do \
		$(MAKE) -C $$b clean >/dev/null 2>&1 || true; \
		if ! $(MAKE) -C $$b COVERAGE=1 >$$b/cov_build.log 2>&1; then \
			printf "%-16s  BENCH FAILED (see %s/cov_build.log)\n" $$b $$b; fail=$$((fail+1)); continue; fi; \
		d=$$(ls -d $$b/cov_*.vdb 2>/dev/null | head -1); \
		if [ -z "$$d" ]; then printf "%-16s  no coverage DB\n" $$b; fail=$$((fail+1)); continue; fi; \
		rm -rf $$b/$(COV_REPORT); \
		urg -dir $$d -format text -report $$b/$(COV_REPORT) >$$b/cov_urg.log 2>&1 || \
			{ printf "%-16s  urg failed (see %s/cov_urg.log)\n" $$b $$b; fail=$$((fail+1)); continue; }; \
		top=$$(sed -n 's/^TOPLEVEL[[:space:]]*=[[:space:]]*//p' $$b/Makefile | head -1); \
		awk -v b=$$b -v top=$$top 'NF>=7 && $$NF==top { \
			asrt = (NF>=8 ? $$7 : "--"); \
			printf "%-16s %7s %7s %7s %7s %7s %7s %7s\n", b, $$1,$$2,$$3,$$4,$$5,$$6,asrt; \
			found=1 } \
			END { if (!found) printf "%-16s  NO DUT ROW (TOPLEVEL=%s not in modlist) -- table would silently drop this block\n", b, top }' \
			$$b/$(COV_REPORT)/modlist.txt || fail=$$((fail+1)); \
	done; \
	printf -- "----------------------------------------------------------------------\n"; \
	echo "DUT-module scores above (the bound SVA checker's own coverage is excluded)."; \
	echo "Full per-block reports: tests/<block>/$(COV_REPORT)/{dashboard,modinfo,modlist}.txt"; \
	echo "benches with a coverage problem: $$fail"

clean:
	@for d in $(BENCH_DIRS); do $(MAKE) -C $$d clean 2>/dev/null || true; done
	@find . -name "__pycache__" -o -name ".pytest_cache" -o -name "sim_build" -o -name "*.vcd" | xargs rm -rf
	@rm -rf $(COV_REPORT) $(COV_REPORT)_urg.log
	@find . -maxdepth 2 \( -name "cov_*.vdb" -o -name "cov_report" -o -name "cov_build.log" -o -name "cov_urg.log" \) | xargs rm -rf 2>/dev/null || true
