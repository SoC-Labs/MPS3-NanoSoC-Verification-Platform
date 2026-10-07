# mps3-nanosoc-platform — top-level orchestration
#
# Consumes SoC IP/firmware from source repos via env vars (defaults assume the
# sibling monorepo checkout). `make check` (W-CI) is the one-command repo
# health gate; the per-area build targets below it delegate to the owning
# makefiles where those exist (fpga/dfx, firmware/test, tests) and remain
# stubs where they don't yet (shell BD, tender).

# --- source-repo locations (override on the CLI) ---------------------------
NANOSOC_SRC     ?= $(HOME)/SoCLabs/temp/nanosoc-multicore-system
ARCH_TECH_SRC   ?= $(NANOSOC_SRC)/nanosoc_arch_tech
ETH_SS_SRC      ?= $(NANOSOC_SRC)/ethernet-subsystem-ahb
FPGAHUB_SRC     ?= $(HOME)/SoCLabs/fpgahub

# --- FPGA / toolchain ------------------------------------------------------
FPGA_PART       ?= xcku115-flvb1760-1-c
BOARD           ?= arm_mps3
VIVADO          ?= vivado
PY              ?= python3
VERILATOR       ?= verilator

.PHONY: help
help:
	@echo "mps3-nanosoc-platform targets:"
	@echo "  make check        - one-command repo health gate (W-CI):"
	@echo "                        contracts -> tests/ pytest -> host/pyverify pytest"
	@echo "                        -> firmware host-gcc harness -> verilator lint"
	@echo "                        -> [SIM=vcs] READY cocotb benches (source set_env.sh first)"
	@echo "                        -> [if overlays exist] fpga/dfx manifest verify"
	@echo "                        -> [module load verdi] IICE sim-vs-hw trace harness"
	@echo "                        -> [module load identify] real Identify vs the firmware XVC engine"
	@echo "  make check-linux  - the MicroBlaze V Linux harness gate (harnessd build + conformance"
	@echo "                        + stage0 + DTS + CPU seam); part of check / check-ci stage 5"
	@echo "  make contracts    - check the interface contracts exist        [A6]"
	@echo "  make lint         - verilator --lint-only over the real RTL set [A6]"
	@echo "  make sim          - run cocotb benches (delegates to tests/; source set_env.sh)"
	@echo "  make shell        - build the static shell bitstream           [A1/A2 TODO]"
	@echo "  make rp-nanosoc   - build the nanosoc reconfigurable module    (see fpga/dfx/Makefile)"
	@echo "  make dfx-probe    - KU115 DFX floorplan/pr_verify probe        (see fpga/dfx/Makefile)"
	@echo "  make firmware     - MicroBlaze bare-metal shell firmware       [A3 TODO — host harness: make -C firmware/test test]"
	@echo "  make tender       - provision the Pi-5 tender image            [A4   TODO]"

# ---------------------------------------------------------------------------
# make check — the W-CI one-command gate. Non-zero exit on any failure.
# Guarded stages: cocotb benches only when SIM is set (e.g.
# `source set_env.sh && make check SIM=vcs`); overlay round-trip only when
# fpga/dfx/overlay/*/manifest.json triples exist; the IICE trace harness only
# when SIM is set AND Verdi is on PATH; the real-Identify XVC gate only when
# identify_debugger_shell is on PATH AND an instrumented rev_1_identify/ build
# exists.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# make check-ci — the BOARD-FREE logic gate: stages 1-5 of `make check` (the
# stages that need NO simulator, NO Vivado, NO built overlays and NO board).
# This is exactly what the hosted CI runs (.github/workflows/ci.yml) and what a
# developer can reproduce locally with `make check-ci`. Fast (~3 min).
#
# KEEP IN SYNC WITH `check` (below): these five stages are duplicated verbatim
# from check's stages 1-5. `check` is deliberately NOT refactored to call this
# target: it is the repo's most important gate and cannot be exercised end to
# end here (stage 7 needs a VCS licence), so it is left untouched. Drift in the
# dangerous direction -- a board-free gate added to `check` but forgotten here,
# so CI is quietly weaker than a local `make check` -- is caught at gate time by
# tests/integration/test_ci_gate_mirrors_check.py.
#
# Requires: python3 with pytest + pydantic + cocotb (cocotb is IMPORT-ONLY here,
# NO simulator -- tests/common/regmap.py imports it at top level, so two
# pure-logic tests fail COLLECTION without it), a C compiler + make, and tclsh
# (pin-check). See .github/ci-requirements.txt for the exact pinned set.
# ---------------------------------------------------------------------------
.PHONY: check-ci
check-ci:
	@echo "== check-ci [1/5] contracts exist =="
	@$(MAKE) --no-print-directory contracts
	@echo "== check-ci [2/5] RM boundary conformance + rm_id gates =="
	@$(MAKE) --no-print-directory -C fpga/dfx pin-check
	@$(PY) scripts/harness_gates/check_rm_id_encoding.py
	@$(PY) scripts/harness_gates/check_rm_id_literals.py
	@# The MANIFEST-ONLY half of check-overlays. Both of these read
	@# overlay/*/manifest.json (+ mps3_shell_static_id.c) and nothing else, so
	@# they are board-free AND artifact-free and belong here; only the CRC/len
	@# round-trip in `check` needs the gitignored *.bin payloads, which is why
	@# check-overlays as a whole is not mirrored. Without this, R9 ran in NO CI
	@# job -- which is how nanosoc_upy stayed keyed to a dead shell across two
	@# mints while every CI run reported green (fixed 3c9703d).
	@if ls fpga/dfx/overlay/*/manifest.json >/dev/null 2>&1; then \
	  $(PY) scripts/harness_gates/check_overlay_static_id.py --repo .; \
	  $(PY) scripts/harness_gates/check_clearing_fits.py \
	    --waivers scripts/harness_gates/clearing_fit_waivers.txt; \
	else \
	  echo "   SKIP overlay manifest gates (no fpga/dfx/overlay/*/manifest.json)"; \
	fi
	@# No tracked file may claim a FIELDED static_id other than the one authority
	@# (docs/FIELDED_SHELL.md). Deliberately in stage 2 -- with the other mechanical
	@# agreement gates -- rather than late in the recipe, because a board-free gate
	@# placed after check's [6/10] marker is invisible to
	@# test_ci_gate_mirrors_check.py's parity scan. That is how R9 stopped running.
	@$(PY) scripts/harness_gates/check_fielded_shell_claims.py --repo .
	@# ...and the same class one level down: an overlay built from a NON-VENDORED
	@# source tree (rm_socscope reads $SOCSCOPE_HOME) must record which revision.
	@# Manifest-only, so board-free and artifact-free; a hosted runner has no
	@# sibling SoCScope checkout, and the gate says so and still checks the
	@# structural rules (present / not `-dirty` / `unknown` only with a waiver).
	@$(PY) scripts/harness_gates/check_socscope_overlay_rev.py --repo .
	@# ...and who may REDISTRIBUTE each overlay. Every manifest must carry a top-level
	@# ip_class (open | arm-aaa) equal to RM_LIB(<rm>,ip_class) in rm_list.tcl -- the
	@# release split (Arm Academic Access overlays private, the rest public) and Harness
	@# Manager's IP-class column both read it. Manifest + rm_list only: board-free and
	@# artifact-free, so it rides with its siblings in stage 2 of check AND check-ci.
	@$(PY) scripts/harness_gates/check_overlay_ip_class.py --repo .
	@# --- the six harness_regression TIER-0/1 gates (folded in 2026-09-09) -----
	@# Until today these six ran ONLY under `make harness-regression` -- a target
	@# nothing invokes on change: not `check`, not `check-ci`, not any workflow. So
	@# the whole family they gate had no gate that actually RUNS: bug #1 (CSR decode
	@# width -- the escape that made every shell CSR read 0 on silicon), #2 (stale
	@# packaged IP), #3 (a DECOUPLED_VALUE that passes validate_bd_design and dies at
	@# IP generation), #4 (a hardware change moved a firmware address) and #6 (a
	@# diagnostic that lied). Same shape as R9 two stanzas up: a gate that does not
	@# run reads as a pass.
	@#
	@# They are pure stdlib, seconds, and read only TRACKED sources, so they belong
	@# in stage 2 beside the other mechanical agreement gates -- and ahead of the
	@# stage-6 RTL-lint marker, because test_ci_gate_mirrors_check.py slices check's
	@# recipe AT that marker and cannot see anything after it. (Do not spell that
	@# marker out in a comment here: the first draft of this block did, which
	@# truncated the slice to nothing and turned the parity test red with
	@# "extractor found no pytest stage in check".)
	@# Each is guarded on its own input so a fresh clone SKIPs with a printed reason
	@# rather than failing. The guards are on TRACKED files, so in practice they
	@# never fire; they exist so a partial/sparse checkout degrades honestly.
	@if [ -f fpga/shell/bd/shell_bd.tcl ]; then \
	   $(PY) scripts/harness_gates/check_bench_param_parity.py \
	     --waivers scripts/harness_gates/param_parity_waivers.txt; \
	 else echo "   SKIP bench/BD param parity (no fpga/shell/bd/shell_bd.tcl)"; fi
	@if [ -f fpga/shell/bd/shell_bd.tcl ]; then \
	   $(PY) scripts/harness_gates/check_bd_config_lint.py; \
	 else echo "   SKIP shell_bd.tcl CONFIG lint (no fpga/shell/bd/shell_bd.tcl)"; fi
	@if [ -f firmware/platform/Makefile ] && [ -f scripts/mps3_diag.tcl ]; then \
	   $(PY) scripts/harness_gates/check_diag_mailbox_parity.py; \
	 else echo "   SKIP diag-mailbox parity (firmware/platform/Makefile or scripts/mps3_diag.tcl absent)"; fi
	@if [ -f fpga/shell/ip_packaged/package_csr_ip.tcl ]; then \
	   $(PY) scripts/harness_gates/check_packaged_ip_fresh.py; \
	 else echo "   SKIP packaged-IP freshness (no fpga/shell/ip_packaged/package_csr_ip.tcl)"; fi
	@if [ -f firmware/common/diag.c ]; then \
	   $(PY) scripts/harness_gates/check_diag_field_parity.py; \
	 else echo "   SKIP diag field parity (no firmware/common/diag.c)"; fi
	@# The STATIC side of the RP boundary: shell_top.sv's u_rp_dut port map and
	@# shell_bd.tcl's rp_* create_bd_port list, both hand-written, both held to
	@# fpga/shell/boundary.yaml (read through tools/gen_boundary.py's loader).
	@# Until 2026-09-23 only the RM side and the stub were gated (ILA mint, FLOW).
	@if [ -f fpga/shell/shell_top.sv ] && [ -f fpga/shell/bd/shell_bd.tcl ] && [ -f fpga/shell/boundary.yaml ]; then \
	   $(PY) scripts/harness_gates/check_shell_top_boundary.py; \
	 else echo "   SKIP shell_top/BD vs boundary.yaml (fpga/shell/{shell_top.sv,bd/shell_bd.tcl,boundary.yaml} absent)"; fi
	@# Every tools/gen_*.py view must be byte-identical to a fresh render. The
	@# register map, the diag mailbox layout and the RP<->shell partition boundary
	@# are each DERIVED once (from the shell BD, from diag.h's field list, from
	@# boundary.yaml) and rendered into the ~11 files that used to restate them by
	@# hand. Generation only closes that drift while the rendered files are
	@# current; a generator nobody re-runs is a comment. Stage 2, beside the other
	@# mechanical agreement gates, and ahead of the stage-6 RTL-lint marker so
	@# test_ci_gate_mirrors_check.py's parity slice can see it.
	@if [ -d tools ]; then \
	   $(PY) scripts/harness_gates/check_generated_fresh.py --repo .; \
	 else echo "   SKIP generated-view freshness (no tools/)"; fi
	@# ...and the same class of agreement one level OUT, at the board boundary:
	@# the console's FPGA UART lane (the XDC), the MCC's UART mux (the SD card's
	@# config.txt) and what BOARD_BRINGUP.md tells an operator to open. Nothing
	@# referenced anything else, and the shell console was pinned to a mux input
	@# the shipped UARTMODE does not select -- reachable by no host node at all,
	@# which looks exactly like a dead processor and sent every firmware
	@# diagnosis over JTAG instead (docs/planning/CONSOLE_AUDIT.md). The gate does
	@# NOT demand a reachable console: a known-unreachable one is allowed and is
	@# today's state. It demands that the state be WRITTEN DOWN and stay true, so
	@# a re-pin or a UARTMODE change goes red until the operator doc follows.
	@if [ -f fpga/shell/constraints/mps3_harness.xdc ]; then \
	   $(PY) scripts/harness_gates/check_console_channel.py --repo .; \
	 else echo "   SKIP console-channel agreement (no mps3_harness.xdc)"; fi
	@# ...and the same class of agreement at the OUTERMOST layer: docs/STATUS.md,
	@# the one file whose entire content is claims, and which nothing checked. Its
	@# own header has said for months that "a silicon claim with no citation is a
	@# row that has drifted"; running this gate for the first time found three rows
	@# citing files that do not exist. The gate demands that every cited path and
	@# path:line resolve, that every row carry a badge the legend defines and at
	@# least one citation, and that a Silicon row name evidence -- a commit, a test,
	@# an artifact -- rather than the PLAN for the hardware result it claims. It
	@# cannot tell whether a claim is true; it can tell whether it is checkable.
	@# Stage 2 with the other mechanical agreement gates, and ahead of the stage-6
	@# RTL-lint marker so test_ci_gate_mirrors_check.py's parity slice can see it.
	@if [ -f docs/STATUS.md ]; then \
	   $(PY) scripts/harness_gates/check_status_citations.py --repo .; \
	 else echo "   SKIP STATUS.md citations (no docs/STATUS.md)"; fi
	@# check_impl_reports.py reads fpga/shell/build_results_*/ -- untracked Vivado
	@# text reports, absent on a fresh clone and on every hosted runner. It SKIPs
	@# ITSELF there (prints the reason, exits 0), so it needs no Makefile guard;
	@# when the reports ARE present it hard-asserts WNS>0 and 0 DRC errors.
	@$(PY) scripts/harness_gates/check_impl_reports.py
	@echo "== check-ci [3/5] cross-workstream pytest (tests/ + the DFX flow tools) =="
	$(PY) -m pytest tests -q
	@# The DFX flow's own tools (FLOW, Linux harness 2026-09-23: the Linux bundle,
	@# stage0 bake, Vivado version guard, OOC early warning) -- 40 board-free
	@# tests with negative controls. NOT under tests/, so `pytest tests` above
	@# never collected them; named here so it cannot drift out of CI again.
	$(PY) -m pytest fpga/dfx/tools/tests -q
	@echo "== check-ci [4/5] host pytest (pyverify + socket_harness + webharness) =="
	cd host/pyverify && $(PY) -m pytest -q
	PYTHONPATH=host:host/pyverify $(PY) -m pytest host/socket_harness/tests -q
	PYTHONPATH=host:host/pyverify $(PY) -m pytest host/webharness/tests -q
	@# host/readback (IICE readback-frame decoder): pure-Python, board-free. Was
	@# invoked by NO stage on the branch that wrote it -- the same orphan shape
	@# as the stage0 tests below, caught on merge (2026-09-10).
	cd host/readback && $(PY) -m pytest tests -q
	@echo "== check-ci [5/5] firmware + stage0 host-gcc + the Linux harness gate =="
	@$(MAKE) --no-print-directory -C firmware/test test
	@# The Linux harness (MicroBlaze V + mps3-harnessd). NOT guarded: every
	@# sub-target fails loudly when its input is missing (see check-linux below).
	@# Its stage 3 runs the stage0 host tests, which used to run here behind an
	@# `if [ -d ]` guard that would have skipped them on a tree without them.
	@# It replaced the v0.7 daemon wire contract (run_wire_compat_host.sh),
	@# retired 2026-09-23 to src/linux_harness/sw/tests/legacy/.
	@$(MAKE) --no-print-directory check-linux
	@# ADVISORY swap-path coverage -- MUST mirror the same action in `check`
	@# (tests/integration/test_ci_gate_mirrors_check.py enforces board-free parity:
	@# a board-free gate in check must also be in check-ci). Non-failing; set
	@# COV_STRICT=1 COV_FLOOR=NN to gate. Skips cleanly if gcov is absent.
	@if [ -x firmware/test/coverage.sh ]; then \
	   echo "   -- advisory swap-path coverage (gcov) --"; \
	   $(MAKE) --no-print-directory -C firmware/test coverage \
	     || echo "   (coverage advisory stage errored -- non-fatal)"; \
	 else echo "   SKIP coverage (firmware/test/coverage.sh absent)"; fi
	@echo "CI GATE OK -- board-free logic stages (1-5) all passed"

.PHONY: check
check:
	@echo "== check [1/10] contracts exist =="
	@$(MAKE) --no-print-directory contracts
	@echo "== check [2/10] RM boundary conformance (partition-pins.md) =="
	@$(MAKE) --no-print-directory -C fpga/dfx pin-check
	@# rm_id encoding v2 (VERSIONING_PLAN.md §3.2): the SAME class of gate as
	@# pin-check -- a mechanical three-way agreement check on the RM library --
	@# so it rides in the same stage. Asserts wrapper localparam == rm_list.tcl
	@# == overlay manifest for every RM. A manifest that disagrees with the
	@# fabric makes EVERY swap of that RM fail step_verify() on the board.
	@$(PY) scripts/harness_gates/check_rm_id_encoding.py
	@# ...and the companion gate on every rm_id CONSUMER. check_rm_id_encoding.py
	@# above gates the three AUTHORITATIVE sources against each other; this one
	@# gates everyone who PINS an id against that authority. Since v2 an rm_id
	@# carries the design VERSION, so it changes on every version bump -- which
	@# made every hard-coded rm_id a landmine (six test_e2e_deploy tests died on
	@# the cutover). Now a surviving pin is self-checking: the next bump fails
	@# HERE, naming the line and the new value, instead of on hardware.
	@$(PY) scripts/harness_gates/check_rm_id_literals.py
	@# ...and the same class of gate one level up: no tracked file may claim a
	@# FIELDED static_id other than docs/FIELDED_SHELL.md's. `0xCD74B6AE` was named
	@# as "the currently shipped shell" in nine files; running this gate for the
	@# first time found ELEVEN stale claims naming THREE dead shells. Present-tense
	@# only -- history ("landed as 0x...", "the then-fielded 0x...") is true
	@# permanently and is left alone, which is why the gate cannot be a plain grep.
	@$(PY) scripts/harness_gates/check_fielded_shell_claims.py --repo .
	@# ...and the same class one level down. rm_socscope's RTL is read from the
	@# sibling SoCScope checkout at synth time, never vendored, so the only record
	@# of WHICH SoCScope a fielded overlay carries is the manifest's socscope_rev.
	@# Before it existed the answer was unknowable (fpga/dfx/overlay/socscope is
	@# waived for exactly that reason, until its next rebuild). Present -> must
	@# resolve in $SOCSCOPE_HOME; `-dirty` -> FAIL (unreproducible); older than
	@# HEAD -> a NOTE, because a pending rebuild is normal, not wrong.
	@$(PY) scripts/harness_gates/check_socscope_overlay_rev.py --repo .
	@# ...and who may REDISTRIBUTE each overlay. Every manifest must carry a top-level
	@# ip_class (open | arm-aaa) equal to RM_LIB(<rm>,ip_class) in rm_list.tcl -- the
	@# release split (Arm Academic Access overlays private, the rest public) and Harness
	@# Manager's IP-class column both read it. Manifest + rm_list only: board-free and
	@# artifact-free, so it rides with its siblings in stage 2 of check AND check-ci.
	@$(PY) scripts/harness_gates/check_overlay_ip_class.py --repo .
	@# --- the six harness_regression TIER-0/1 gates (folded in 2026-09-09) -----
	@# Until today these six ran ONLY under `make harness-regression` -- a target
	@# nothing invokes on change: not `check`, not `check-ci`, not any workflow. So
	@# the whole family they gate had no gate that actually RUNS: bug #1 (CSR decode
	@# width -- the escape that made every shell CSR read 0 on silicon), #2 (stale
	@# packaged IP), #3 (a DECOUPLED_VALUE that passes validate_bd_design and dies at
	@# IP generation), #4 (a hardware change moved a firmware address) and #6 (a
	@# diagnostic that lied). Same shape as R9 two stanzas up: a gate that does not
	@# run reads as a pass.
	@#
	@# They are pure stdlib, seconds, and read only TRACKED sources, so they belong
	@# in stage 2 beside the other mechanical agreement gates -- and ahead of the
	@# stage-6 RTL-lint marker, because test_ci_gate_mirrors_check.py slices check's
	@# recipe AT that marker and cannot see anything after it. (Do not spell that
	@# marker out in a comment here: the first draft of this block did, which
	@# truncated the slice to nothing and turned the parity test red with
	@# "extractor found no pytest stage in check".)
	@# Each is guarded on its own input so a fresh clone SKIPs with a printed reason
	@# rather than failing. The guards are on TRACKED files, so in practice they
	@# never fire; they exist so a partial/sparse checkout degrades honestly.
	@if [ -f fpga/shell/bd/shell_bd.tcl ]; then \
	   $(PY) scripts/harness_gates/check_bench_param_parity.py \
	     --waivers scripts/harness_gates/param_parity_waivers.txt; \
	 else echo "   SKIP bench/BD param parity (no fpga/shell/bd/shell_bd.tcl)"; fi
	@if [ -f fpga/shell/bd/shell_bd.tcl ]; then \
	   $(PY) scripts/harness_gates/check_bd_config_lint.py; \
	 else echo "   SKIP shell_bd.tcl CONFIG lint (no fpga/shell/bd/shell_bd.tcl)"; fi
	@if [ -f firmware/platform/Makefile ] && [ -f scripts/mps3_diag.tcl ]; then \
	   $(PY) scripts/harness_gates/check_diag_mailbox_parity.py; \
	 else echo "   SKIP diag-mailbox parity (firmware/platform/Makefile or scripts/mps3_diag.tcl absent)"; fi
	@if [ -f fpga/shell/ip_packaged/package_csr_ip.tcl ]; then \
	   $(PY) scripts/harness_gates/check_packaged_ip_fresh.py; \
	 else echo "   SKIP packaged-IP freshness (no fpga/shell/ip_packaged/package_csr_ip.tcl)"; fi
	@if [ -f firmware/common/diag.c ]; then \
	   $(PY) scripts/harness_gates/check_diag_field_parity.py; \
	 else echo "   SKIP diag field parity (no firmware/common/diag.c)"; fi
	@# The STATIC side of the RP boundary: shell_top.sv's u_rp_dut port map and
	@# shell_bd.tcl's rp_* create_bd_port list, both hand-written, both held to
	@# fpga/shell/boundary.yaml (read through tools/gen_boundary.py's loader).
	@# Until 2026-09-23 only the RM side and the stub were gated (ILA mint, FLOW).
	@if [ -f fpga/shell/shell_top.sv ] && [ -f fpga/shell/bd/shell_bd.tcl ] && [ -f fpga/shell/boundary.yaml ]; then \
	   $(PY) scripts/harness_gates/check_shell_top_boundary.py; \
	 else echo "   SKIP shell_top/BD vs boundary.yaml (fpga/shell/{shell_top.sv,bd/shell_bd.tcl,boundary.yaml} absent)"; fi
	@# Every tools/gen_*.py view must be byte-identical to a fresh render. The
	@# register map, the diag mailbox layout and the RP<->shell partition boundary
	@# are each DERIVED once (from the shell BD, from diag.h's field list, from
	@# boundary.yaml) and rendered into the ~11 files that used to restate them by
	@# hand. Generation only closes that drift while the rendered files are
	@# current; a generator nobody re-runs is a comment. Stage 2, beside the other
	@# mechanical agreement gates, and ahead of the stage-6 RTL-lint marker so
	@# test_ci_gate_mirrors_check.py's parity slice can see it.
	@if [ -d tools ]; then \
	   $(PY) scripts/harness_gates/check_generated_fresh.py --repo .; \
	 else echo "   SKIP generated-view freshness (no tools/)"; fi
	@# ...and the same class of agreement one level OUT, at the board boundary:
	@# the console's FPGA UART lane (the XDC), the MCC's UART mux (the SD card's
	@# config.txt) and what BOARD_BRINGUP.md tells an operator to open. Nothing
	@# referenced anything else, and the shell console was pinned to a mux input
	@# the shipped UARTMODE does not select -- reachable by no host node at all,
	@# which looks exactly like a dead processor and sent every firmware
	@# diagnosis over JTAG instead (docs/planning/CONSOLE_AUDIT.md). The gate does
	@# NOT demand a reachable console: a known-unreachable one is allowed and is
	@# today's state. It demands that the state be WRITTEN DOWN and stay true, so
	@# a re-pin or a UARTMODE change goes red until the operator doc follows.
	@if [ -f fpga/shell/constraints/mps3_harness.xdc ]; then \
	   $(PY) scripts/harness_gates/check_console_channel.py --repo .; \
	 else echo "   SKIP console-channel agreement (no mps3_harness.xdc)"; fi
	@# ...and the same class of agreement at the OUTERMOST layer: docs/STATUS.md,
	@# the one file whose entire content is claims, and which nothing checked. Its
	@# own header has said for months that "a silicon claim with no citation is a
	@# row that has drifted"; running this gate for the first time found three rows
	@# citing files that do not exist. The gate demands that every cited path and
	@# path:line resolve, that every row carry a badge the legend defines and at
	@# least one citation, and that a Silicon row name evidence -- a commit, a test,
	@# an artifact -- rather than the PLAN for the hardware result it claims. It
	@# cannot tell whether a claim is true; it can tell whether it is checkable.
	@# Stage 2 with the other mechanical agreement gates, and ahead of the stage-6
	@# RTL-lint marker so test_ci_gate_mirrors_check.py's parity slice can see it.
	@if [ -f docs/STATUS.md ]; then \
	   $(PY) scripts/harness_gates/check_status_citations.py --repo .; \
	 else echo "   SKIP STATUS.md citations (no docs/STATUS.md)"; fi
	@# check_impl_reports.py reads fpga/shell/build_results_*/ -- untracked Vivado
	@# text reports, absent on a fresh clone and on every hosted runner. It SKIPs
	@# ITSELF there (prints the reason, exits 0), so it needs no Makefile guard;
	@# when the reports ARE present it hard-asserts WNS>0 and 0 DRC errors.
	@$(PY) scripts/harness_gates/check_impl_reports.py
	@echo "== check [3/10] cross-workstream pytest (tests/ + the DFX flow tools) =="
	$(PY) -m pytest tests -q
	@# FLOW's tools tests (fpga/dfx/tools/tests): outside tests/, so named here.
	$(PY) -m pytest fpga/dfx/tools/tests -q
	@echo "== check [4/10] host pytest (pyverify + socket_harness + webharness) =="
	cd host/pyverify && $(PY) -m pytest -q
	PYTHONPATH=host:host/pyverify $(PY) -m pytest host/socket_harness/tests -q
	@# webharness (the web dashboard): pure-routing + backend-seam tests, plus
	@# the drift guards that parse firmware/clcd/clcd.{h,c} and assert the
	@# service/RM tables agree over all 65536 design ids. Board-free -- the only
	@# sockets are a loopback server against the in-process fake backend.
	PYTHONPATH=host:host/pyverify $(PY) -m pytest host/webharness/tests -q
	@# host/readback (IICE readback-frame decoder): pure-Python, board-free. Was
	@# invoked by NO stage on the branch that wrote it -- the same orphan shape
	@# as the stage0 tests below, caught on merge (2026-09-10).
	cd host/readback && $(PY) -m pytest tests -q
	@echo "== check [5/10] firmware host-gcc harness =="
	@$(MAKE) --no-print-directory -C firmware/test test
	@# (The stage0 loader host tests -- once orphaned, then run here behind an
	@# `if [ -d ]` guard -- now run UNGUARDED as check-linux's stage 3, below.)
	@# The Linux harness gate (MicroBlaze V + mps3-harnessd): one conformance
	@# suite against ctrl_echo AND mps3-harnessd, plus each L1 lane's own gate.
	@# It REPLACED the v0.7 daemon wire contract (run_wire_compat_host.sh), which
	@# pinned the retired hand-ported daemons (`stats` = unknown op, 14 diag
	@# keys) and now lives, un-run, in src/linux_harness/sw/tests/legacy/.
	@# Unguarded on purpose: a missing sub-target FAILS -- see check-linux.
	@$(MAKE) --no-print-directory check-linux
	@# ADVISORY swap-path line coverage (gcov text, no lcov). Prints per-file
	@# coverage for config_agent.c + swap_fsm*.c in their real build configs;
	@# folded here (host-gcc category) so the [N/10] labels stay stable. NON-FAILING
	@# by design -- turn it into a hard gate with COV_STRICT=1 COV_FLOOR=NN. Skips
	@# cleanly if gcov or the coverage script is absent.
	@if [ -x firmware/test/coverage.sh ]; then \
	   echo "   -- advisory swap-path coverage (gcov) --"; \
	   $(MAKE) --no-print-directory -C firmware/test coverage \
	     || echo "   (coverage advisory stage errored -- non-fatal)"; \
	 else echo "   SKIP coverage (firmware/test/coverage.sh absent)"; fi
	@echo "== check [6/10] verilator lint =="
	@$(MAKE) --no-print-directory lint
	@echo "== check [7/10] cocotb benches (guard: SIM) =="
	@$(MAKE) --no-print-directory check-sim
	@echo "== check [8/10] overlay round-trip (guard: triples present) =="
	@$(MAKE) --no-print-directory check-overlays
	@echo "== check [9/10] IICE sim-vs-hw trace harness (guard: SIM + Verdi) =="
	@$(MAKE) --no-print-directory check-iice
	@echo "== check [10/10] real Identify vs the firmware XVC engine (guard: identify) =="
	@$(MAKE) --no-print-directory check-identify-fw
	@# FOUR stages are guarded and skip silently. Saying "CHECK OK" without naming
	@# what did not run is how a green gate hides an empty one -- stage 7 skipped
	@# for months while tests/common/list_benches.py did not even parse (0d86c1c).
	@skipped=""; \
	 [ -n "$(SIM)" ] || skipped="$$skipped [7/10] cocotb benches (set SIM=vcs + 'source set_env.sh')"; \
	 ls fpga/dfx/overlay/*/*.bin >/dev/null 2>&1 || skipped="$$skipped [8/10] overlay round-trip (no overlay .bin built)"; \
	 { [ -n "$(SIM)" ] && command -v nCompare >/dev/null 2>&1; } || skipped="$$skipped [9/10] IICE trace harness (needs SIM=vcs + 'module load verdi')"; \
	 { command -v identify_debugger_shell >/dev/null 2>&1 && \
	   [ -d fpga/rp/nanosoc_iice/build/rev_1_identify ]; } || \
	   skipped="$$skipped [10/10] real-Identify XVC gate (needs 'module load identify' + an instrumented rev_1_identify/ build)"; \
	 if [ -n "$$skipped" ]; then \
	   echo "CHECK OK -- but these stages did NOT run:$$skipped"; \
	 else \
	   echo "CHECK OK (all 10 stages ran)"; \
	 fi

# ---------------------------------------------------------------------------
# make check-linux -- the MicroBlaze V LINUX HARNESS gate
# (docs/planning/LINUX_HARNESS_PLAN_2026-09-23.md §4-5). Board-free, no Vivado,
# no Buildroot. Five stages, each OWNED by the L1 lane that wrote what it runs;
# the recipe each one runs is taken from that lane's contract
# (docs/planning/linux_lanes/<LANE>_CONTRACT.md), and HOST_CONTRACT.md §1 is the
# map. Run by check / check-ci stage 5, and so by hosted CI.
#
# NO STAGE SKIPS. Every sub-target first checks that its input exists and FAILS
# with the owning lane's name when it does not -- and a sub-target that is not
# defined at all fails too, because make has no rule for it. The seam this gate
# guards is exactly the one where "the file is not there yet" would otherwise
# read as a pass (the v0.7 wire gate SKIPped whenever its script was absent).
#
#   make check-linux                       # all five
#   make check-linux-conformance           # one stage
# ---------------------------------------------------------------------------
HARNESSD_DIR    ?= src/linux_harness/sw/harnessd
HARNESSD_ECHO   ?= $(HARNESSD_DIR)/build/host-echo/mps3-harnessd
STAGE0_TEST_DIR ?= src/linux_soc/hw/fw_stage0/test
LINUX_CHECKS    := check-linux-harnessd check-linux-conformance check-linux-stage0 \
                   check-linux-dts check-linux-seam

.PHONY: check-linux $(LINUX_CHECKS)
check-linux:
	@echo "== check-linux [1/5] mps3-harnessd host build + its own tests   (owner: HARNESSD) =="
	@$(MAKE) --no-print-directory check-linux-harnessd
	@echo "== check-linux [2/5] one conformance suite: ctrl_echo + harnessd (owner: HOST) =="
	@$(MAKE) --no-print-directory check-linux-conformance
	@echo "== check-linux [3/5] stage0 host tests                           (owner: STAGE0) =="
	@$(MAKE) --no-print-directory check-linux-stage0
	@echo "== check-linux [4/5] DTS freshness + block ownership + image scripts (owner: IMAGE) =="
	@$(MAKE) --no-print-directory check-linux-dts
	@echo "== check-linux [5/5] shell CPU seam (board-free half)            (owner: SHELL) =="
	@$(MAKE) --no-print-directory check-linux-seam
	@echo "LINUX GATE OK -- all five check-linux stages ran"

# HARNESSD_CONTRACT.md §8: `check` builds `host` + `host-echo` and runs every
# harnessd host test (UIO HAL, impl byte-identity, identify, TOFU, e2e sockets).
check-linux-harnessd:
	@test -f $(HARNESSD_DIR)/Makefile || { \
	  echo "FAIL check-linux-harnessd: no $(HARNESSD_DIR)/Makefile -- HARNESSD_CONTRACT.md §8 (owner: HARNESSD)"; exit 1; }
	nice -n 19 $(MAKE) --no-print-directory -C $(HARNESSD_DIR) check

# HOST: tests/firmware_logic/test_fakeshell_conformance.py with harnessd REQUIRED
# -- the same case table ctrl_echo answers, against the host-echo twin over real
# sockets. REQUIRE turns a missing binary into a failure, never a skip.
check-linux-conformance:
	@test -f $(HARNESSD_DIR)/Makefile || { \
	  echo "FAIL check-linux-conformance: no $(HARNESSD_DIR)/Makefile to build host-echo (owner: HARNESSD)"; exit 1; }
	nice -n 19 $(MAKE) --no-print-directory -C $(HARNESSD_DIR) host-echo
	@test -x $(HARNESSD_ECHO) || { \
	  echo "FAIL check-linux-conformance: host-echo produced no $(HARNESSD_ECHO) (HARNESSD_CONTRACT.md §8)"; exit 1; }
	MPS3_HARNESSD_BIN=$(abspath $(HARNESSD_ECHO)) MPS3_CONFORMANCE_REQUIRE=harnessd \
	  nice -n 19 $(PY) -m pytest tests/firmware_logic/test_fakeshell_conformance.py -q

# STAGE0_CONTRACT.md §2: host tests (boot order, slot fallback, TFTP loss/retry,
# the status-block layout gate) -- always. The SIZE gate (the image fits below
# 0x1FE00 with its budget, plus its negative controls) needs the rv32 cross
# compiler from a Vivado install: it runs whenever that compiler is present, and
# when it is not, says so LOUDLY as "NOT RUN" -- the one stage-internal step that
# may not run, because a hosted runner has no Vivado; FLOW's mint-stage0 runs it
# before every bake regardless. (tests/integration/test_ci_gate_mirrors_check.py
# allows exactly this one NOT RUN.)
STAGE0_CROSS ?= $(XILINX_VIVADO)/gnu/riscv/lin/bin/riscv64-unknown-elf-
check-linux-stage0:
	@test -f $(STAGE0_TEST_DIR)/Makefile || { \
	  echo "FAIL check-linux-stage0: no $(STAGE0_TEST_DIR)/Makefile (owner: STAGE0)"; exit 1; }
	nice -n 19 $(MAKE) --no-print-directory -C $(STAGE0_TEST_DIR)
	@if command -v $(STAGE0_CROSS)gcc >/dev/null 2>&1; then \
	   nice -n 19 $(MAKE) --no-print-directory -C src/linux_soc/hw/fw_stage0 size-gate CROSS=$(STAGE0_CROSS) || exit 1; \
	 else \
	   echo "!!! NOT RUN: stage0 size-gate -- no rv32 toolchain at $(STAGE0_CROSS)gcc"; \
	   echo "!!!          (set XILINX_VIVADO or STAGE0_CROSS; FLOW's mint-stage0 runs it before a bake)"; \
	 fi

# IMAGE_CONTRACT.md §11: the generated DTS is fresh; ownership (a block is
# kernel-owned or harnessd-owned, never both), dtc compile and the negative
# controls; the image scripts' host tests. Needs dtc (CI installs it).
check-linux-dts:
	@for f in tools/gen_dts.py tools/dts_gates.py src/linux_harness/sw/br2_external/tests/run.sh; do \
	  test -f $$f || { echo "FAIL check-linux-dts: no $$f -- IMAGE_CONTRACT.md §11 (owner: IMAGE)"; exit 1; }; \
	done
	$(PY) tools/gen_dts.py --check
	$(PY) tools/dts_gates.py
	sh src/linux_harness/sw/br2_external/tests/run.sh

# SHELL_CONTRACT.md §1: the pytest half of the seam gate (the Vivado half,
# run_seam_gate.sh, is a pre-mint step, never CI). It replaced
# tests/linux_fork_boundary, retired with the fork BD.
check-linux-seam:
	@test -f tests/shell_cpu_seam/test_shell_cpu_seam.py || { \
	  echo "FAIL check-linux-seam: no tests/shell_cpu_seam/test_shell_cpu_seam.py (owner: SHELL)"; exit 1; }
	$(PY) -m pytest tests/shell_cpu_seam -q

# The sim-vs-hardware IICE trace comparison gate (tests/identify_iice). Needs BOTH
# VCS (to produce the sim FSDB) and Verdi (nCompare + the fsdb utilities), so it is
# double-guarded and SKIPs loudly rather than silently passing. Note the harness's
# pure-Python unit tests -- including the crop maths and the comparator core -- run
# unguarded in stage [3/10] via `pytest tests`, so a regression there is caught even
# with no EDA tools present. This stage adds the parts that genuinely need the tools:
# a real FSDB round-trip and the negative control on real traces.
# The per-RM NETLIST gate for the DFX partition (ILA mint, 2026-09-23): for every
# rm_*_synth.dcp in RM_NETLIST_DIR -- 0 BSCANE2, 0 PRIMITIVE_GROUP==CLOCK cells,
# at most one mode-1 debug hub (xsdbm), and an ILA only beside a hub (handover
# §4.8, traps 1-2). scripts/harness_gates/rm_netlist_check.tcl. Needs Vivado and
# staged checkpoints (gitignored build output), so it is check-iice-shaped: it
# SKIPs loudly without either, and is NOT in `check`/`check-ci`. Point it at a
# mint's prod dir: make check-rm-netlist RM_NETLIST_DIR=<BUILD>/prod
RM_NETLIST_DIR ?= fpga/dfx/build/prod
.PHONY: check-rm-netlist
check-rm-netlist:
	@if ! command -v $(VIVADO) >/dev/null 2>&1; then \
	  echo "  SKIP RM netlist gate ($(VIVADO) not on PATH -- set VIVADO=/apps/Xilinx/Vivado/2024.1/bin/vivado)"; \
	elif ! ls $(RM_NETLIST_DIR)/rm_*_synth.dcp >/dev/null 2>&1; then \
	  echo "  SKIP RM netlist gate (no rm_*_synth.dcp in RM_NETLIST_DIR=$(RM_NETLIST_DIR) -- stage them with make -C fpga/dfx rm-<name>-dcp, or point RM_NETLIST_DIR at a mint's prod dir)"; \
	else \
	  log=$$(mktemp -d)/rm_netlist_check.log; \
	  ( cd $$(dirname $$log) && $(VIVADO) -mode batch -nojournal -log $$log \
	      -source $(CURDIR)/scripts/harness_gates/rm_netlist_check.tcl \
	      -tclargs $(abspath $(RM_NETLIST_DIR)) >/dev/null ); \
	  grep -E "^RM_NETLIST_CHECK" $$log; \
	  grep -q "^RM_NETLIST_CHECK_OK" $$log || { echo "  FAIL RM netlist gate -- see $$log"; exit 1; }; \
	fi

.PHONY: check-iice
check-iice:
	@if [ -z "$(SIM)" ]; then \
	  echo "  SKIP IICE trace harness (set SIM=vcs and 'source set_env.sh' to enable)"; \
	elif ! command -v nCompare >/dev/null 2>&1; then \
	  echo "  SKIP IICE trace harness (nCompare not on PATH -- 'module load verdi/X-2025.06-SP2')"; \
	else \
	  $(MAKE) --no-print-directory -C tests/identify_iice check SIM=$(SIM) || exit 1; \
	fi

# The REAL Synopsys Identify debugger driving the REAL firmware XVC engine
# (firmware/xvc_server/xvc_server.c, -DMPS3_XVC_TARGET_SWDBB) over a real TCP
# socket, with no board, no MicroBlaze and no Vivado -- see
# host/identify/fw_com_check.sh. Runs BOTH the positive session and the
# system-level negative control (bin/xvc_fw_daemon_ratio1, the pre-fix accept
# ceiling, which the debugger must FAIL against -- otherwise "Identify was happy"
# could just mean "Identify is easy to please").
#
# Double-guarded and SKIPs loudly, the same shape as check-iice: it needs the
# licensed `identify_debugger_shell` on PATH (`module load identify`) AND an
# instrumented rev_1_identify/ build, which is gitignored build output and so
# absent in a fresh clone. It checks out an `identdebugger` licence seat per
# session (~20 s each).
#
# The board-free, TOOL-FREE half of this path runs UNGUARDED in stage [5/10]:
# firmware/test's test_xvc_posix_loopback{,_latesample} drive the identical stack
# (the same real xvc_server.c, the same POSIX socket backend, the same in-memory
# TAP) from an in-process socket client, so a regression is caught with no EDA
# tools present. This stage adds only the part that genuinely needs the vendor
# binary.
.PHONY: check-identify-fw
check-identify-fw:
	@if ! command -v identify_debugger_shell >/dev/null 2>&1; then \
	  echo "  SKIP real-Identify XVC gate (identify_debugger_shell not on PATH -- 'module load identify/2022.09-SP2')"; \
	elif [ ! -d fpga/rp/nanosoc_iice/build/rev_1_identify ]; then \
	  echo "  SKIP real-Identify XVC gate (no instrumented fpga/rp/nanosoc_iice/build/rev_1_identify/)"; \
	else \
	  host/identify/fw_com_check.sh || exit 1; \
	fi

.PHONY: contracts
contracts:
	@for f in partition-pins shell-regmap net-protocol overlay-manifest partition-timing; do \
	  test -f docs/contracts/$$f.md && echo "  OK  docs/contracts/$$f.md" \
	    || { echo "  MISSING docs/contracts/$$f.md"; exit 1; }; \
	done

# --- verilator lint (the loop the wave agents ran by hand, wired as a target).
# Set = every real (non-stub) RTL block documented lint-clean in its README;
# mdio_phy_model and uart_bridge pull helpers in via `include and need the
# incdir. verilator 4.028 on this machine is lint-only (too old for cocotb).
LINT_SV := \
  fpga/shell/ip/clkrst/dut_clkrst.sv \
  fpga/shell/ip/dfx_ctl/dfx_ctl.sv \
  fpga/shell/ip/board_gpio/board_gpio.sv \
  fpga/shell/ip/swd_bb/swd_bb.sv \
  fpga/shell/ip/telem/telem.sv \
  fpga/shell/ip/clcd_kvm/clcd_kvm.sv \
  fpga/shell/ip/usd_spi/usd_spi.sv \
  fpga/ethernet/rmii_phy_if/rmii_phy_if.sv \
  fpga/ethernet/bridge/eth_bridge_3port.sv \
  fpga/ethernet/gen_checker/gen_checker.sv \
  fpga/ethernet/link_partner_mac/link_partner_mac.sv \
  fpga/bus_mon/ahb_mon.sv

.PHONY: lint
lint:
	@for f in $(LINT_SV); do \
	  $(VERILATOR) --lint-only -sv $$f || exit 1; echo "  OK  lint $$f"; \
	done
	@$(VERILATOR) --lint-only -sv -Ifpga/ethernet/mdio_phy_model \
	  fpga/ethernet/mdio_phy_model/mdio_phy_model.sv || exit 1
	@echo "  OK  lint fpga/ethernet/mdio_phy_model/mdio_phy_model.sv (+incdir)"
	@$(VERILATOR) --lint-only -sv -Ifpga/shell/ip/uart_bridge \
	  fpga/shell/ip/uart_bridge/uart_bridge.sv || exit 1
	@echo "  OK  lint fpga/shell/ip/uart_bridge/uart_bridge.sv (+incdir)"
	@# dut_egress.sv `include's dutegr_cfifo.sv (the commit/rollback dual-clock
	@# FIFO), same single-file-build idiom as uart_bridge above -- so it needs the
	@# same -I and cannot be a plain LINT_SV entry.
	@$(VERILATOR) --lint-only -sv -Ifpga/shell/ip/dut_egress \
	  fpga/shell/ip/dut_egress/dut_egress.sv || exit 1
	@echo "  OK  lint fpga/shell/ip/dut_egress/dut_egress.sv (+incdir)"
	@# ...and the helper on its own: it is a genuinely separate, instantiable
	@# module and must stay lintable as one.
	@$(VERILATOR) --lint-only -sv \
	  fpga/shell/ip/dut_egress/dutegr_cfifo.sv || exit 1
	@echo "  OK  lint fpga/shell/ip/dut_egress/dutegr_cfifo.sv"
	@# clcd.sv now instantiates clcd_core (the shared 8080 engine, W2-A split),
	@# so it can no longer be linted standalone; -I lets verilator resolve the
	@# submodule by filename (same idiom as uart_bridge above).
	@$(VERILATOR) --lint-only -sv -Ifpga/shell/ip/clcd \
	  fpga/shell/ip/clcd/clcd.sv || exit 1
	@echo "  OK  lint fpga/shell/ip/clcd/clcd.sv (+clcd_core)"
	@# The DUT-side display socket: nanosoc_exp_socket -> ahb_clcd -> clcd_core.
	@# -I on both dirs resolves ahb_clcd (nanosoc_exp) and clcd_core (shell/clcd).
	@$(VERILATOR) --lint-only -sv \
	  -Ifpga/rp/nanosoc_exp -Ifpga/shell/ip/clcd \
	  fpga/rp/nanosoc_exp/nanosoc_exp_socket.sv \
	  --top-module nanosoc_exp_socket || exit 1
	@echo "  OK  lint fpga/rp/nanosoc_exp/nanosoc_exp_socket.sv (+ahb_clcd +clcd_core)"
	@# eth_mac_test_subsystem (§8 integration): an assembly module, so it must
	@# be linted WITH its five instantiated blocks (a standalone LINT_SV entry
	@# would error on undefined submodules). +incdir for mdio_phy_model's two
	@# `include'd helpers; those two files are NOT passed separately (the top
	@# `include's them — double-definition otherwise).
	@$(VERILATOR) --lint-only -sv -Ifpga/ethernet/mdio_phy_model \
	  fpga/ethernet/rmii_phy_if/rmii_phy_if.sv \
	  fpga/ethernet/link_partner_mac/link_partner_mac.sv \
	  fpga/ethernet/bridge/eth_bridge_3port.sv \
	  fpga/ethernet/gen_checker/gen_checker.sv \
	  fpga/ethernet/mdio_phy_model/mdio_phy_model.sv \
	  fpga/ethernet/eth_mac_test_subsystem.sv \
	  --top-module eth_mac_test_subsystem || exit 1
	@echo "  OK  lint fpga/ethernet/eth_mac_test_subsystem.sv (+deps, --top-module)"

# Runs every READY cocotb bench when SIM is set (SKIPs otherwise so `make
# check` stays green on boxes without a simulator env). Fails on the first
# red bench. Requires `source set_env.sh` first (I24).
.PHONY: check-sim
check-sim:
	@if [ -z "$(SIM)" ]; then \
	  echo "  SKIP cocotb benches (set SIM=vcs and 'source set_env.sh' to enable)"; \
	else \
	  for name in $$($(PY) tests/common/list_benches.py | awk '$$1=="READY"{print $$2}'); do \
	    echo "---- bench $$name (SIM=$(SIM)) ----"; \
	    $(MAKE) -C tests/$$name SIM=$(SIM) || exit 1; \
	  done; \
	fi

# Round-trips every existing overlay triple when any exist (W-DFX-ART emits
# them via `make -C fpga/dfx overlays`); SKIPs cleanly otherwise.
.PHONY: check-overlays
check-overlays:
	@# `&&`, NOT `;` -- a `;` here DISCARDS the exit status of the overlay verify:
	@# only the last command in the recipe line decides the result, so a failing
	@# `make -C fpga/dfx verify` printed "Error 1" and `make check` still reported
	@# CHECK OK / exit 0. That is exactly how a stale overlay partial went
	@# unnoticed (2026-07-24: overlay/nanosoc_upy/nanosoc_upy.bin was still the
	@# pre-XiP 07-17 build while its manifest described the 07-19 XiP one --
	@# crc32 and len both mismatched, verify said so, and the gate stayed green).
	@# A gate that prints a failure and exits 0 is worse than no gate.
	@# Guard on the PAYLOADS, not just the manifests. `verify` round-trips each
	@# manifest's crc32/len against its *.bin payload, so it needs the payloads
	@# present. On a fresh worktree the *.bin are untracked build products and are
	@# legitimately absent (.gitignore: "a fresh clone then fails gen_manifest.py
	@# verify until the payloads are regenerated") -- hard-failing there makes
	@# `make check` un-greenable on a clean checkout, which is how a gate gets
	@# ignored. Skipping when NO payload exists does NOT reopen the 2026-07-24
	@# stale-partial hole: whenever a *.bin IS present, verify still runs under the
	@# `&&` and still catches a crc/len mismatch (that incident had the .bin
	@# present-but-stale). This only stops the false-fail when there is nothing to
	@# check. check_clearing_fits reads manifest metadata, so it runs whenever the
	@# manifests exist.
	@# R9 (static_id lockstep) runs on the MANIFESTS, outside the payload guard.
	@# It reads overlay/*/manifest.json and overlay/mps3_shell_static_id.c and
	@# nothing else -- no *.bin, no simulator, no board -- so gating it on build
	@# products only hid it. It was inside the `ls *.bin` branch above AND absent
	@# from check-ci, which means it never ran in CI at all and ran locally only
	@# when build products happened to be lying about. A fresh clone was green
	@# with a two-mint-stale overlay in it: nanosoc_upy sat on 0xD84A2E7A across
	@# the 0xCD74B6AE and 0xA8C1C535 mints (fixed 3c9703d), un-loadable on the
	@# fielded shell the whole time, and no gate said so. Same lesson as the
	@# 2026-07-24 stale-partial incident recorded above -- a gate that does not
	@# run is worse than one that fails, because it reads as a pass.
	@if ls fpga/dfx/overlay/*/manifest.json >/dev/null 2>&1; then \
	  $(PY) scripts/harness_gates/check_overlay_static_id.py --repo . && \
	  if ls fpga/dfx/overlay/*/*.bin >/dev/null 2>&1; then \
	    $(MAKE) -C fpga/dfx verify; \
	  else \
	    echo "  SKIP overlay CRC round-trip (no *.bin payloads — run 'make -C fpga/dfx overlays')"; \
	  fi && \
	  $(PY) scripts/harness_gates/check_clearing_fits.py \
	    --waivers scripts/harness_gates/clearing_fit_waivers.txt; \
	elif ls fpga/dfx/overlay/*/manifest.json >/dev/null 2>&1; then \
	  echo "  SKIP overlay verify (manifests present but NO .bin built — run 'make -C fpga/dfx overlays')"; \
	else \
	  echo "  SKIP overlay verify (no fpga/dfx/overlay/*/manifest.json — run 'make -C fpga/dfx overlays')"; \
	fi
	@# The bootable image and the shipped partials must come from ONE impl run.
	@# Runs unconditionally (it SKIPs itself when the artefacts are absent) and
	@# covers overlay_linux/ too, which the guarded branch above does not.
	@$(PY) scripts/harness_gates/check_image_overlay_match.py

# --- delegations / remaining stubs -----------------------------------------
.PHONY: sim
sim:
	@$(MAKE) -C tests

.PHONY: rp-nanosoc dfx-probe
rp-nanosoc:
	@$(MAKE) -C fpga/dfx rm-nanosoc-dcp

dfx-probe:
	@$(MAKE) -C fpga/dfx proof

.PHONY: shell firmware tender
shell firmware tender:
	@echo "[stub] '$@' not implemented yet — see the owning workstream README."

# ---------------------------------------------------------------------------
# harness-regression — tiered go/no-go for a newly generated HARNESS IMAGE
# (static shell bitstream + re-keyed firmware). Each gate names the historical
# escape it catches; the run STOPS at the first failure. See
# docs/HARNESS_REGRESSION.md. Default runs tiers 0..2 (no board, no Vivado).
#   make harness-regression                       # tiers 0..2
#   make harness-regression HR_ARGS="--only 0"    # just the seconds-long tier
#   source set_env.sh && make harness-regression HR_ARGS="--through 1" SIM=vcs
#   make harness-regression HR_ARGS="--through 3 --allow-board"   # + on-board
# ---------------------------------------------------------------------------
.PHONY: harness-regression
harness-regression:
	@SIM=$(SIM) scripts/harness_regression.sh $(HR_ARGS)
