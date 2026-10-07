"""tests/integration/test_ci_gate_mirrors_check.py

Anti-drift gate for the hosted CI board-free gate.

`make check-ci` (what .github/workflows/ci.yml runs on every push/PR) duplicates
stages 1-5 of `make check` -- the board-free logic stages. Duplication was a
deliberate choice: `check` is the repo's most important target and cannot be
exercised end to end without a VCS licence, so it is not refactored to call
check-ci (see the Makefile banner above check-ci). The cost of duplication is
DRIFT, and one direction of drift is dangerous:

    a board-free gate is added to `make check` stages 1-5 but NOT to check-ci,
    so hosted CI is quietly WEAKER than a local `make check` -- a real
    regression could pass CI.

(The other direction -- check-ci stricter than check -- is caught by developers
immediately, because their local `make check` would then be weaker than CI.)

This test parses the Makefile, pulls every board-free ACTION out of check's
stages 1-5 (a harness-gate .py, a `pytest` invocation, a `$(MAKE) -C <dir>
<target>` sub-make, or a gate .sh), and asserts each one also appears in the
check-ci recipe. A new board-free stage added to `check` and forgotten in
check-ci fails HERE, board-free, instead of silently narrowing CI.

It also asserts the CI plumbing that makes the gate runnable exists and stays
consistent (the requirements file, the workflow that invokes check-ci).
"""
import re
import pathlib

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[2]
_MAKEFILE = _REPO / "Makefile"


def _recipe_lines(text: str, target: str):
    """Return the recipe lines (tab-indented) of `target:` in Makefile `text`.

    Collects consecutive lines beginning with a tab immediately after the target
    header, which is how GNU make delimits a recipe."""
    lines = text.splitlines()
    out = []
    hdr = re.compile(r"^" + re.escape(target) + r":(\s|$)")
    i = 0
    while i < len(lines):
        if hdr.match(lines[i]):
            i += 1
            while i < len(lines) and lines[i].startswith("\t"):
                out.append(lines[i])
                i += 1
            return out
        i += 1
    return None


def _board_free_actions(recipe_lines):
    """Extract the set of board-free 'actions' from recipe lines.

    An action is a token that denotes running a real gate: a harness_gates .py,
    a pytest invocation (normalised to its target path), a `-C <dir> <target>`
    sub-make, or a gate .sh path. These are the shapes every board-free stage
    takes, so a newly-added board-free stage is picked up automatically."""
    actions = set()
    # Key on real COMMANDS only -- drop `echo` lines so that rewording a stage's
    # progress label (e.g. "[3/5] ... pytest (tests/)") can never be mistaken for
    # a gate and cause a false drift failure.
    cmd_lines = [
        ln for ln in recipe_lines
        if not re.match(r"^\t\s*@?echo\b", ln)
    ]
    blob = "\n".join(cmd_lines)
    # harness-gate python scripts
    actions.update(re.findall(r"scripts/harness_gates/[\w./-]+\.py", blob))
    # `$(MAKE) ... -C <dir> [<target>]` sub-makes (pin-check, firmware, stage0)
    for d, t in re.findall(r"-C\s+([\w./-]+)(?:\s+([\w./-]+))?", blob):
        actions.add(f"-C {d} {t}".rstrip())
    # `$(MAKE) --no-print-directory <phony>` (e.g. contracts) with no -C
    for phony in re.findall(r"\$\(MAKE\)\s+--no-print-directory\s+([a-z][\w-]*)\b", blob):
        actions.add(f"MAKE:{phony}")
    # pytest invocations -- normalise to the path/dir being tested
    for m in re.findall(r"pytest\s+(\S+)", blob):
        actions.add("pytest ." if m.startswith("-") else f"pytest {m}")
    # gate shell scripts
    actions.update(re.findall(r"[\w./-]+\.sh", blob))
    return actions


def _stages_1_to_5(check_recipe):
    """The board-free slice of check's recipe: everything before the stage-6
    (verilator lint) marker."""
    cut = len(check_recipe)
    for i, ln in enumerate(check_recipe):
        if "[6/10]" in ln or "verilator lint" in ln:
            cut = i
            break
    return check_recipe[:cut]


def test_makefile_has_both_targets():
    text = _MAKEFILE.read_text()
    assert _recipe_lines(text, "check") is not None, "no `check:` target in Makefile"
    assert _recipe_lines(text, "check-ci") is not None, "no `check-ci:` target in Makefile"


def test_check_ci_covers_every_board_free_action_in_check():
    text = _MAKEFILE.read_text()
    check = _stages_1_to_5(_recipe_lines(text, "check"))
    check_ci = _recipe_lines(text, "check-ci")

    want = _board_free_actions(check)
    got = _board_free_actions(check_ci)

    # sanity: our extractor actually found the known stages (guards against a
    # regex that silently matches nothing, which would make this test vacuous).
    assert any("harness_gates" in a for a in want), (
        "extractor found no harness-gate scripts in check stages 1-5 -- "
        "the parser is broken, not the Makefile"
    )
    assert any(a.startswith("pytest") for a in want), "extractor found no pytest stage in check"

    missing = want - got
    assert not missing, (
        "board-free gate(s) present in `make check` stages 1-5 but MISSING from "
        "`make check-ci` -- hosted CI would be weaker than local `make check`. "
        "Add them to the check-ci recipe.\n  missing: " + ", ".join(sorted(missing))
    )


def test_ci_plumbing_present_and_consistent():
    """The workflow and its pinned requirements must exist and refer to the
    target/file they depend on -- so a rename can't silently orphan CI."""
    req = _REPO / ".github" / "ci-requirements.txt"
    wf = _REPO / ".github" / "workflows" / "ci.yml"
    assert req.exists(), "missing .github/ci-requirements.txt"
    assert wf.exists(), "missing .github/workflows/ci.yml"

    wf_text = wf.read_text()
    assert "make check-ci" in wf_text, "ci.yml no longer invokes `make check-ci`"
    assert "ci-requirements.txt" in wf_text, "ci.yml no longer installs ci-requirements.txt"

    # cocotb is load-bearing for collection (see the requirements-file comment);
    # its absence would silently red the logic gate on a hosted runner.
    assert re.search(r"^cocotb==", req.read_text(), re.M), (
        "cocotb must stay pinned in ci-requirements.txt -- tests/common/regmap.py "
        "imports it at top level, so collection fails without it"
    )


@pytest.mark.parametrize("phony", ["check", "check-ci", "check-linux"])
def test_targets_are_phony(phony):
    """Both gates must be .PHONY (they have no file products); otherwise a stray
    file named `check`/`check-ci` would make the gate a silent no-op."""
    text = _MAKEFILE.read_text()
    phony_targets = set()
    for m in re.findall(r"^\.PHONY:\s*(.+)$", text, re.M):
        phony_targets.update(m.split())
    assert phony in phony_targets, f"`{phony}` is not declared .PHONY"


# --------------------------------------------------------------------------- #
# The blind spot that let a stale overlay ship
# --------------------------------------------------------------------------- #
#
# `check` invokes check-overlays AFTER its [6/10] marker, so `_stages_1_to_5`
# does not see it and the parity test above cannot demand it. That is not an
# oversight in the slicing -- one of check-overlays' three gates (the manifest
# CRC/len round-trip) needs the gitignored *.bin build products and genuinely
# cannot run in hosted CI, so mirroring the target wholesale would break CI on
# a fresh clone.
#
# The other two are artifact-free, and R9 is the one with a history: it lived
# inside the payload-gated branch AND was absent from check-ci, so it ran in NO
# CI job and locally only when build products happened to be present. A fresh
# clone was green while nanosoc_upy sat keyed to a dead shell across the
# 0xCD74B6AE and 0xA8C1C535 mints -- un-loadable on the fielded board the whole
# time (fixed 3c9703d).
#
# These two tests are narrow on purpose. The general parity rule cannot express
# "mirror the artifact-free half of a target", so the specific gate that was
# lost is pinned by name instead.

def test_r9_static_id_lockstep_runs_in_ci():
    """R9 must be in check-ci. It reads manifests + one .c file: no payloads.

    If this fails, hosted CI can no longer tell that an overlay is keyed to a
    shell that is not on the board -- which is a silent, shippable defect: the
    pusher refuses the partial at load time, on the bench, with a lease running.
    """
    text = _MAKEFILE.read_text()
    ci = _recipe_lines(text, "check-ci")
    assert ci is not None, "no `check-ci:` target in Makefile"
    assert any("check_overlay_static_id.py" in ln for ln in ci), (
        "check_overlay_static_id.py (R9) is missing from `make check-ci`. It is "
        "board-free AND artifact-free, so there is no reason for it not to run in "
        "hosted CI -- and when it did not, a two-mint-stale overlay shipped."
    )


def test_r9_is_not_gated_on_build_products_in_check():
    """In `check`, R9 must sit OUTSIDE the `ls *.bin` guard.

    The payload CRC round-trip needs *.bin; R9 does not. Putting them in one
    guarded branch means R9 silently does not run on any tree without build
    products -- which is every fresh clone.
    """
    text = _MAKEFILE.read_text()
    recipe = _recipe_lines(text, "check-overlays")
    assert recipe is not None, "no `check-overlays:` target in Makefile"
    body = "\n".join(ln for ln in recipe if not re.match(r"^\t\s*@?#", ln))
    r9_at = body.find("check_overlay_static_id.py")
    guard_at = body.find("*.bin")
    assert r9_at != -1, "R9 no longer runs in check-overlays at all"
    assert guard_at != -1, "the *.bin payload guard is gone -- re-check this test's premise"
    assert r9_at < guard_at, (
        "R9 now runs after/inside the `ls *.bin` payload guard in check-overlays. "
        "It needs no payloads, and gating it on build products is exactly how it "
        "stopped running on fresh trees."
    )


# --------------------------------------------------------------------------- #
# Orphaned gates: a gate nobody invokes
# --------------------------------------------------------------------------- #
#
# The parity test above answers "is check-ci as strong as check?". It cannot
# answer the prior question -- "does anything run this gate at all?" -- and that
# is where the corpus actually rotted. Six board-free gates
# (check_bench_param_parity, check_bd_config_lint, check_diag_mailbox_parity,
# check_packaged_ip_fresh, check_diag_field_parity, check_impl_reports) lived
# under scripts/harness_gates/ and were invoked ONLY by
# scripts/harness_regression.sh -- a target nothing runs on change. The whole
# bug-#1 CSR-decode class therefore had no gate in any CI job, and nothing said
# so, because an uninvoked gate produces no output at all: it reads as a pass.
#
# This test closes that: every gate script on disk must be invoked by SOMETHING
# -- the check-ci recipe, the check recipe (including the targets check invokes),
# or the harness-regression driver -- or be named in the allowlist below with a
# reason. The allowlist is the point: an exemption you have to write a sentence
# for is an exemption someone can audit.

_GATES_DIR = _REPO / "scripts" / "harness_gates"

#: gate filename -> why nothing board-free invokes it. Every entry is a licence
#: to be un-run, so every entry states what it needs instead.
_ORPHAN_ALLOWLIST = {
    # --- board-only: need the MPS3 on a held lease, so they can only ever run
    #     under `harness_regression.sh --through 3 --allow-board`. -----------
    "swap_check.py": "board-only: streams a partial over 6910 and reads back DFXCTL.RM_ID",
    "ping_check.py": "board-only: TCP 6900 {\"op\":\"ping\"} against the live shell",
    "console_check.py": "board-only: TCP 6930 echo round-trip against a resident RM",
    "swd_check.py": "board-only: openocd over the shell's debug server (6921 rbb)",
    "tier3_csr_liveness.tcl": "board-only: xsdb mrd/mwr over JTAG-MDM on the running MicroBlaze",
    "tier3_csr_liveness_mbv.tcl": "board-only (MicroBlaze V): the same probe through mdm_riscv_0; "
                                  "SOURCED by tier3_csr_liveness.tcl when MPS3_HARNESS_CPU=mbv, "
                                  "exercised board-free by host/pyverify/tests/test_tier3_tcl_mbv.py",
    # --- Vivado-only ------------------------------------------------------- #
    "tier2_dcp_assert.tcl": "Vivado-only: open_checkpoint on the static synth DCP (--with-vivado)",
    # --- run BY the DFX flow, per config, inside the mint (2026-09-23) ----- #
    "check_hdpr_reports.py": "mint-time: fpga/dfx/build_dfx.tcl calls it on every config's "
                             "drc_hdpr_<rm>_prelink.rpt and drc_<rm>.rpt (tools/debug_probes.tcl "
                             "hdpr_report_gate); board-free logic tested by test_hdpr_report_gate.py",
    "rm_netlist_check.tcl": "Vivado-only: `make check-rm-netlist` opens every rm_*_synth.dcp "
                            "(check-iice-shaped, SKIPs loudly without checkpoints)",
}

#: Allowlisted gates whose invoker is NOT a Makefile recipe: name -> the file
#: that must reference it. An allowlist entry that claims an invoker which no
#: longer calls the gate is the orphan this file exists to catch.
_FLOW_INVOKED = {
    "check_hdpr_reports.py": _REPO / "fpga" / "dfx" / "tools" / "debug_probes.tcl",
    "rm_netlist_check.tcl": _MAKEFILE,
    "tier3_csr_liveness_mbv.tcl": _GATES_DIR / "tier3_csr_liveness.tcl",
}


def _invocation_blob():
    """Everything that could plausibly invoke a gate, as one searchable string.

    = the check-ci recipe + the check recipe + the recipes of every phony target
    `check`/`check-ci` delegate to (check-overlays holds two gates that `check`
    reaches only through a sub-make) + the harness-regression driver.
    """
    text = _MAKEFILE.read_text()
    parts = []
    seen = set()
    todo = ["check", "check-ci"]
    while todo:
        tgt = todo.pop()
        if tgt in seen:
            continue
        seen.add(tgt)
        recipe = _recipe_lines(text, tgt)
        if recipe is None:
            continue
        blob = "\n".join(recipe)
        parts.append(blob)
        # follow one hop of `$(MAKE) --no-print-directory <phony>` delegation
        todo += re.findall(r"\$\(MAKE\)\s+--no-print-directory\s+([a-z][\w-]*)\b", blob)
    parts.append((_REPO / "scripts" / "harness_regression.sh").read_text())
    return "\n".join(parts)


def _gate_files():
    return sorted(
        p for p in _GATES_DIR.iterdir()
        if p.is_file() and p.suffix in (".py", ".tcl")
    )


def test_every_harness_gate_is_invoked_by_something():
    """No gate may exist that nothing runs.

    CONTROL for this test: drop a `zz_orphan_probe.py` into scripts/harness_gates/
    and it must go red naming that file. It was verified that way before the six
    orphaned gates were folded into `check`/`check-ci`.
    """
    blob = _invocation_blob()
    assert "harness_gates" in blob, "invocation blob is empty -- the parser is broken"

    orphans = []
    for p in _gate_files():
        if p.name in _ORPHAN_ALLOWLIST:
            continue
        if p.name not in blob:
            orphans.append(p.name)

    assert not orphans, (
        "harness gate(s) that NOTHING invokes -- not `make check`, not `make "
        "check-ci`, not scripts/harness_regression.sh. An uninvoked gate emits no "
        "output, so it reads as a pass; that is how the bug-#1 parity gate sat "
        "un-run in every CI job.\n"
        "  orphaned: " + ", ".join(sorted(orphans)) + "\n"
        "  Fix: wire it into a check/check-ci stage or a harness_regression tier, "
        "or add it to _ORPHAN_ALLOWLIST in this file WITH a reason."
    )


def test_orphan_allowlist_entries_all_carry_a_reason():
    """An exemption without a stated reason is how an allowlist becomes a bin."""
    for name, why in _ORPHAN_ALLOWLIST.items():
        assert why and len(why) > 20, (
            "_ORPHAN_ALLOWLIST[%r] needs a real reason, not %r" % (name, why))


def test_the_six_folded_gates_are_not_allowlisted():
    """The six board-free gates that were orphaned must stay wired, not waived.

    If a future edit finds one of these inconvenient, the cheap escape is to add
    it to the allowlist. That would recreate exactly the state this work fixed --
    so name them, and refuse.
    """
    for name in ("check_bench_param_parity.py", "check_bd_config_lint.py",
                 "check_diag_mailbox_parity.py", "check_packaged_ip_fresh.py",
                 "check_diag_field_parity.py", "check_impl_reports.py"):
        assert name not in _ORPHAN_ALLOWLIST, (
            "%s is board-free and artifact-free (or self-skipping); it belongs in "
            "check/check-ci, not in the orphan allowlist." % name)
    # dut_rx_check.tcl IS board-only, so the allowlist would take it -- but it is
    # wired into harness_regression.sh tier 3 (MPS3_RUN_DUTRX), which is one of the
    # three invokers this test accepts. Allowlisting it would be a downgrade: from
    # "runs when a board is present" to "runs nowhere, by decision".
    assert "dut_rx_check.tcl" not in _ORPHAN_ALLOWLIST, (
        "dut_rx_check.tcl is invoked by harness_regression.sh tier 3; it does not "
        "need (and must not have) an orphan exemption.")


def test_flow_invoked_gates_are_really_invoked():
    """An allowlisted gate's claimed invoker must still name it."""
    for name, invoker in _FLOW_INVOKED.items():
        assert name in _ORPHAN_ALLOWLIST, name
        if not (_GATES_DIR / name).is_file():
            continue
        assert name in invoker.read_text(), (
            "%s is allowlisted as invoked by %s, which no longer references it"
            % (name, invoker))


# --------------------------------------------------------------------------- #
# The Linux harness gate (make check-linux, 2026-09-23)
# --------------------------------------------------------------------------- #
#
# check-linux is the gate the MicroBlaze V Linux harness lands behind: the
# harnessd host build, ONE conformance suite run against ctrl_echo AND
# mps3-harnessd, stage0's host tests, IMAGE's DTS gates and SHELL's CPU-seam
# pytest. Each is a named sub-target owned by the lane that wrote it
# (docs/planning/linux_lanes/HOST_CONTRACT.md §1). These tests make sure it
# cannot quietly stop running: not dropped from check-ci, not missing a stage,
# and no stage able to pass by skipping.

_LINUX_STAGES = ("check-linux-harnessd", "check-linux-conformance",
                 "check-linux-stage0", "check-linux-dts", "check-linux-seam")


def test_check_linux_runs_in_check_ci_and_check():
    text = _MAKEFILE.read_text()
    ci = _recipe_lines(text, "check-ci")
    assert ci is not None and any(
        re.search(r"\$\(MAKE\)\s+--no-print-directory\s+check-linux\b", ln) for ln in ci), (
        "`make check-ci` no longer runs check-linux -- hosted CI would stop gating the "
        "Linux harness while every run still reads green")
    check = _stages_1_to_5(_recipe_lines(text, "check"))
    assert "MAKE:check-linux" in _board_free_actions(check), (
        "check-linux must run inside `make check` stages 1-5 (before the lint marker)")


def test_check_linux_invokes_every_lane_stage():
    text = _MAKEFILE.read_text()
    recipe = _recipe_lines(text, "check-linux")
    assert recipe is not None, "no `check-linux:` target in Makefile"
    called = set(re.findall(r"\$\(MAKE\)\s+--no-print-directory\s+([a-z][\w-]*)",
                            "\n".join(recipe)))
    missing = set(_LINUX_STAGES) - called
    assert not missing, "check-linux no longer runs: " + ", ".join(sorted(missing))
    m = re.search(r"^LINUX_CHECKS\s*:?=\s*((?:.*\\\n)*.*)$", text, re.M)
    assert m, "LINUX_CHECKS variable is gone"
    listed = set(m.group(1).replace("\\", " ").split())
    assert set(_LINUX_STAGES) <= listed, "LINUX_CHECKS (the .PHONY list) lost a stage"
    assert re.search(r"^\.PHONY:.*\$\(LINUX_CHECKS\)", text, re.M), \
        "the check-linux stages are not .PHONY"


@pytest.mark.parametrize("stage", _LINUX_STAGES)
def test_every_check_linux_stage_fails_loudly_and_never_skips(stage):
    """A stage must name a missing input as a FAILURE (exit 1), and must never
    print SKIP: the v0.7 wire gate this replaced skipped whenever its script
    was absent, which is exactly how a gate goes dark on a fresh tree."""
    recipe = _recipe_lines(_MAKEFILE.read_text(), stage)
    assert recipe is not None, f"no `{stage}:` target in Makefile"
    body = "\n".join(recipe)
    assert "exit 1" in body and "FAIL" in body, f"{stage} has no loud failure path"
    assert "SKIP" not in body.upper().replace("SKIPS", ""), f"{stage} can skip"
    # ONE exception, by name: stage0's size gate needs a Vivado rv32 compiler a
    # hosted runner does not have, and says NOT RUN loudly when it is absent
    # (STAGE0's handoff). Anything else that learns to "not run" fails here.
    if "NOT RUN" in body:
        assert stage == "check-linux-stage0" and body.count("NOT RUN") == 1 \
            and "size-gate" in body, f"{stage} grew a NOT RUN path: {body}"


def test_conformance_stage_requires_harnessd():
    body = "\n".join(_recipe_lines(_MAKEFILE.read_text(), "check-linux-conformance"))
    assert "MPS3_CONFORMANCE_REQUIRE=harnessd" in body
    assert "MPS3_HARNESSD_BIN=" in body
    assert "tests/firmware_logic/test_fakeshell_conformance.py" in body


def test_retired_v07_wire_gate_is_run_by_nothing():
    """run_wire_compat_host.sh pinned the retired v0.7 daemons (stats = unknown
    op, 14 diag keys). It lives in src/linux_harness/sw/tests/legacy/ with a
    README; no gate may call it (or anything else under legacy/) again."""
    # commands only: the Makefile's comments explain the retirement by name
    blob = "\n".join(ln for ln in _invocation_blob().splitlines()
                     if not ln.strip().lstrip("@").startswith("#"))
    assert "run_wire_compat_host" not in blob
    assert "/legacy/" not in blob
    legacy = _REPO / "src" / "linux_harness" / "sw" / "tests" / "legacy"
    assert (legacy / "README.md").is_file(), "the retirement README is missing"
    assert not (_REPO / "tests" / "linux_fork_boundary").exists(), (
        "tests/linux_fork_boundary is back under tests/, where `pytest tests` collects it")


def test_flow_tools_tests_run_in_ci():
    """FLOW's fpga/dfx/tools/tests sit outside tests/, so `pytest tests` never
    collects them -- they must be named in check-ci (and check, by parity)."""
    ci = "\n".join(_recipe_lines(_MAKEFILE.read_text(), "check-ci"))
    assert re.search(r"pytest\s+fpga/dfx/tools/tests\b", ci), (
        "fpga/dfx/tools/tests (FLOW's 40 tests) is missing from check-ci")


def test_ci_installs_what_check_linux_needs():
    """check-linux needs tclsh (the tier-3 stub-xsdb tests, pin-check), dtc
    (IMAGE's DTS gate) and iverilog (SHELL's clamp bench); the workflow must
    install them on purpose."""
    wf = (_REPO / ".github" / "workflows" / "ci.yml").read_text()
    assert "device-tree-compiler" in wf, "ci.yml must apt-install device-tree-compiler (dtc)"
    assert "iverilog" in wf, (
        "ci.yml must apt-install iverilog: tests/shell_cpu_seam's decoupler-clamp bench "
        "needs it, and it is the check that catches the July fork's clamp bug")
    assert re.search(r"apt-get install[^\n]*\btcl\b", wf), "ci.yml must apt-install tcl"
