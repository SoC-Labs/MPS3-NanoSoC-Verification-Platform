"""tests/silicon/test_silicon_sweep_plan.py

Board-free gate on the SILICON SWEEP harness (``scripts/mps3_silicon_sweep.sh``).

Runs the sweep's ``--dry-run`` planner against SYNTHETIC overlay + prod trees
under ``tmp_path`` and asserts on the plan, the exit code and the JSON summary.
No board, no lease, no xsdb, no Vivado, no fpgahub, and no real
``fpga/dfx/**`` file is read or written -- so this rides in ``make check``
stage [3/10] (``pytest tests``) and therefore in the always-on hosted CI gate.

WHY A TEST AND NOT JUST "the dry run printed something"
------------------------------------------------------
This repo has shipped a gate that structurally could not fail and treats that as
worse than no gate. A silicon sweep is the most tempting place in the tree for
that mistake: its verdict is a single register readback, its inputs are
gitignored build artefacts that are absent in a fresh clone, and its expensive
half cannot run in CI. So every safety property the sweep claims is
NEGATIVE-CONTROLLED here -- for each rule there is a synthetic tree that MUST
make the planner fail, and the test asserts it does. If a future edit softens a
rule, the corresponding control goes green and this file goes red.

The controls, and the trap each one guards:

  * ``test_refuses_on_static_id_mismatch``  -- base and partials from DIFFERENT
    locked statics. This is the RP-corruption case; the sweep must never start.
  * ``test_refuses_on_locked_dcp_md5_mismatch`` -- static_id.txt AGREES but the
    locked checkpoints differ. static_id.txt is a CRC of the dcp, so two
    different dcps normally differ in id -- but a hand-copied or re-run dir can
    carry a stale/matching id file. The md5 is the authority.
  * ``test_zero_rm_id_is_scheduled_last_and_verified_as_a_transition`` -- greybox
    drives rm_id 0x00000000, which is ALSO the decoupler's DECOUPLED_VALUE, ALSO
    what the wrong board on the shared hw_server returns, and ALSO what a
    reverted shell shows. "Assert RM_ID == 0" therefore cannot fail for the right
    reason. The sweep must mark it verify=transition and schedule it after a
    non-zero RM.
  * ``test_duplicate_rm_id_is_flagged_incoherent`` -- two RMs sharing an rm_id
    make the ONLY check this sweep performs unable to tell them apart.
  * ``test_bad_manifest_is_flagged_not_skipped`` -- an unparseable manifest must
    fail, not vanish.
  * ``test_missing_partial_is_named_not_silently_dropped`` -- gitignored build
    output is absent in a fresh clone, so it must SKIP LOUDLY (named, exit 0)
    and escalate to a failure under ``--require-artefacts``.
  * ``test_max_cap_names_what_it_dropped`` -- no hidden truncation.
  * ``test_bad_rms_selection_refuses`` -- a typo in ``--rms`` must not silently
    shrink the sweep to nothing (a sweep of zero RMs passes trivially).
  * ``test_dry_run_never_names_keep_base`` -- ``mps3_swap_design.sh``'s clearing
    bitstream is hard-wired to GREYBOX's, which is only correct straight after a
    base load. ``--keep-base`` on step N>1 would clear the PREVIOUS RM with
    greybox's clearing: an UltraScale DFX violation. The sweep must never emit it.
  * ``test_dry_run_touches_nothing`` -- no lease, no hub, no xsdb, no vivado in
    the emitted plan's actually-executed path (asserted by running with a PATH
    that has none of them).

Plus two ANTI-DRIFT gates, which are the ones most likely to save a board window:

  * ``test_partial_filename_template_matches_the_swap_script`` -- the planner
    predicts ``config_rm_<rm>_pblock_rp_dut_partial.bit`` and friends. If
    ``mps3_swap_design.sh`` ever renames them, the dry run would happily report
    READY and the real sweep would fail on every RM. Asserted by regex against
    the swap script rather than by duplicating the strings.
  * ``test_ku115_microblaze_walk_is_identical_everywhere`` -- three scripts walk
    ``targets`` for the MicroBlaze that is a DESCENDANT of the xcku115, because
    the lowest-numbered "MicroBlaze #0" belongs to another board on the shared
    hw_server and returns all-zeros (indistinguishable from "greybox resident").
    A fix to one copy that misses the others is a silent wrong-board read, so the
    copies must stay byte-identical.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SWEEP = _REPO / "scripts" / "mps3_silicon_sweep.sh"
_SWAP = _REPO / "scripts" / "mps3_swap_design.sh"
_STATE = _REPO / "scripts" / "mps3_state.sh"
_RESET = _REPO / "scripts" / "mps3_rp_reset_release.sh"

# Two arbitrary but DISTINCT static ids. Neither is a real shipped id, so a
# fixture can never be confused with a real build dir.
SID_A = "0x5A5A0001"
SID_B = "0x5A5A0002"


# ---------------------------------------------------------------------------
# synthetic tree builders
# ---------------------------------------------------------------------------
def _overlay(root: Path, rms: dict, static_id: str = SID_A) -> Path:
    """fpga/dfx/overlay-shaped tree: {rm_name: rm_id_str}."""
    d = root / "overlay"
    for rm, rm_id in rms.items():
        (d / rm).mkdir(parents=True, exist_ok=True)
        (d / rm / "manifest.json").write_text(json.dumps({
            "schema": 1, "static_id": static_id, "rm_id": rm_id, "rm_name": rm,
            "clearing": {"file": "%s_clear.bin" % rm, "len": 1, "crc32": "0x0"},
            "partial": {"file": "%s.bin" % rm, "len": 1, "crc32": "0x0"},
        }) + "\n")
    return d


def _prod(root: Path, rms, static_id: str = SID_A, dcp_body: str = "locked-A",
          name: str = "prod", with_base: bool = True) -> Path:
    """A prod/ dir carrying static_id.txt, the locked dcp, the base bitstream,
    the greybox clearing, and a partial per RM."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "static_id.txt").write_text(static_id + "\n")
    (d / "static_routed_locked.dcp").write_text(dcp_body)
    if with_base:
        (d / "config_rm_greybox_fw.bit").write_text("x" * 32)
        (d / "config_rm_greybox_pblock_rp_dut_partial_clear.bit").write_text("x" * 8)
    for rm in rms:
        (d / ("config_rm_%s_pblock_rp_dut_partial.bit" % rm)).write_text("x" * 16)
    return d


# The source-inspecting anti-drift tests must distinguish what the script DOES
# from what it SAYS. The sweep deliberately prints the very strings those tests
# forbid (it explains "no --keep-base, ever" and "mps3_board.sh acquire does
# NOT"), so a naive "is this substring in the file" check fires on the
# explanation rather than on a real use. Drop comments AND output statements.
_PROSE = re.compile(r"^\s*(say|warn|echo|printf)\b|&&\s*(say|warn|echo)\b")


def _exec_lines(path: Path) -> list:
    """Lines of a shell script that actually run something -- comments and
    say/warn/echo/printf output lines removed."""
    keep = []
    for ln in path.read_text().splitlines():
        s = ln.strip()
        if not s or s.startswith("#") or _PROSE.search(ln):
            continue
        keep.append(ln)
    return keep


def _exec_src(path: Path) -> str:
    return "\n".join(_exec_lines(path))


def _run(overlay: Path, prod: Path, base: Path, *extra, jsonfile: Path = None,
         env_extra: dict = None):
    """Run the sweep in dry-run. Returns (returncode, stdout+stderr, json|None)."""
    cmd = [str(_SWEEP), "--overlay-dir", str(overlay), "--prod", str(prod),
           "--base", str(base)]
    if jsonfile is not None:
        cmd += ["--json", str(jsonfile)]
    cmd += list(extra)
    env = dict(os.environ)
    # Make absolutely sure a test can never reach the board even if the script
    # were edited to try: no hub host, no board, and an unusable xsdb/vivado.
    env.update({
        "MPS3_HUB": "test-invalid-host.invalid",
        "MPS3_BOARD_HOST": "127.0.0.1",
        "XSDB": "/nonexistent/xsdb",
        "VIVADO": "/nonexistent/vivado",
        # The script honours PY (its `PY="${PY:-python3}"`), and make(1) EXPORTS
        # command-line variables, so under `make check-ci PY=python` (ci.yml) the
        # script inherited a name that test_dry_run_touches_nothing's stripped
        # PATH did not carry: "line 207: python: command not found", norm_id
        # printed nothing, and the plan REFUSED (run 34474922254, hosted runner;
        # green on the box where PY was unset). Pin it to THIS interpreter, by
        # absolute path, so no PATH game can change what the script runs.
        "PY": sys.executable,
    })
    env.update(env_extra or {})
    p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=180)
    out = p.stdout + p.stderr
    doc = None
    if jsonfile is not None and jsonfile.exists():
        doc = json.loads(jsonfile.read_text())
    return p.returncode, out, doc


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------
def test_dry_run_plans_a_coherent_catalogue(tmp_path):
    rms = {"led": "0x0100001E", "nanosoc": "0x01000001", "greybox": "0x00000000"}
    ov = _overlay(tmp_path, rms)
    pd = _prod(tmp_path, rms)
    rc, out, doc = _run(ov, pd, pd, jsonfile=tmp_path / "s.json")

    assert rc == 0, out
    assert "SWEEP OK (dry-run)" in out
    assert doc["mode"] == "dry-run"
    assert doc["static_id"] == SID_A
    assert doc["failed"] == 0
    assert {r["rm_name"] for r in doc["rms"]} == set(rms)
    assert all(r["plan_status"] == "READY" for r in doc["rms"]), doc["rms"]
    # every path resolved, not merely a name
    for r in doc["rms"]:
        assert Path(r["partial"]).is_file()
        assert r["partial_bytes"] > 0
    # the md5 comparison actually ran and said so
    assert "locked dcp  md5 MATCH" in out
    # and the plan printed the real command it would run, with the real dirs
    assert "scripts/mps3_swap_design.sh led" in out
    assert str(pd) in out
    # ...and states what a PASS would and would not mean
    assert "does_not_prove" in doc and doc["does_not_prove"]
    assert "NOT PROVEN by this sweep" in out


def test_dry_run_touches_nothing(tmp_path):
    """The dry run must not invoke the lease, the hub, xsdb or vivado. Proven by
    running with a PATH stripped of ssh (the only route to fpgahub) and with
    XSDB/VIVADO pointed at nonexistent files: if anything tried, it would fail."""
    rms = {"led": "0x0100001E", "greybox": "0x00000000"}
    ov = _overlay(tmp_path, rms)
    pd = _prod(tmp_path, rms)
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    for tool in ("python3", "bash", "sh", "grep", "sed", "cut", "tr", "find",
                 "sort", "md5sum", "stat", "date", "mktemp", "basename",
                 "dirname", "tail", "printf", "cat", "mkdir", "wc", "env",
                 "whoami"):   # the default lease-holder name (line ~142)
        src = shutil.which(tool)
        if src:
            (fakebin / tool).symlink_to(src)
    assert shutil.which("ssh", path=str(fakebin)) is None
    rc, out, _ = _run(ov, pd, pd, env_extra={"PATH": str(fakebin)})
    assert rc == 0, out
    assert "no lease taken, no board touched, nothing programmed" in out


# ---------------------------------------------------------------------------
# negative controls -- each MUST fail
# ---------------------------------------------------------------------------
def test_refuses_on_static_id_mismatch(tmp_path):
    rms = {"led": "0x0100001E"}
    ov = _overlay(tmp_path, rms)
    pd = _prod(tmp_path, rms, static_id=SID_A, name="prodA")
    bd = _prod(tmp_path, rms, static_id=SID_B, dcp_body="locked-B", name="prodB")
    rc, out, _ = _run(ov, pd, bd)
    assert rc == 2, out
    assert "REFUSE" in out
    assert "DIFFERENT locked statics" in out
    assert "SWEEP REFUSED" in out


def test_refuses_on_locked_dcp_md5_mismatch(tmp_path):
    """static_id.txt agrees, the locked checkpoints do not. The md5 is the
    authority (mps3_swap_design.sh's header says so explicitly)."""
    rms = {"led": "0x0100001E"}
    ov = _overlay(tmp_path, rms)
    pd = _prod(tmp_path, rms, static_id=SID_A, dcp_body="locked-A", name="prodA")
    bd = _prod(tmp_path, rms, static_id=SID_A, dcp_body="locked-DIFFERENT",
               name="prodB")
    rc, out, _ = _run(ov, pd, bd)
    assert rc == 2, out
    assert "md5 DIFFERS" in out


def test_refuses_when_a_directory_is_missing_and_lists_candidates(tmp_path):
    rms = {"led": "0x0100001E"}
    ov = _overlay(tmp_path, rms)
    pd = _prod(tmp_path, rms)
    rc, out, _ = _run(ov, tmp_path / "does-not-exist", pd)
    assert rc == 2, out
    assert "no such directory" in out
    # ...and it must help, not just complain
    assert "candidate prod dirs" in out


def test_refuses_on_an_empty_catalogue(tmp_path):
    """No manifests => nothing to sweep. A sweep of zero RMs that exits 0 is the
    canonical gate-that-cannot-fail; refuse instead."""
    ov = tmp_path / "overlay"
    ov.mkdir()
    pd = _prod(tmp_path, [])
    rc, out, _ = _run(ov, pd, pd)
    assert rc == 2, out
    assert "no RM catalogue to sweep" in out


def test_zero_rm_id_is_scheduled_last_and_verified_as_a_transition(tmp_path):
    """Trap 5. greybox's rm_id 0x00000000 is also DECOUPLED_VALUE, also the
    wrong-board read, also a reverted shell. It must be marked
    verify=transition and moved LAST so a non-zero positive control precedes it."""
    # 'greybox' sorts FIRST alphabetically, so if the scheduler did nothing it
    # would be step 1 -- i.e. this control is meaningful.
    rms = {"greybox": "0x00000000", "led": "0x0100001E", "nanosoc": "0x01000001"}
    ov = _overlay(tmp_path, rms)
    pd = _prod(tmp_path, rms)
    rc, out, doc = _run(ov, pd, pd, jsonfile=tmp_path / "s.json")
    assert rc == 0, out

    order = [r["rm_name"] for r in doc["rms"]]
    assert order[-1] == "greybox", order
    assert order.index("greybox") > 0, order
    modes = {r["rm_name"]: r["verify_mode"] for r in doc["rms"]}
    assert modes["greybox"] == "transition"
    assert modes["led"] == "readback"
    assert "unfalsifiable" in out
    assert "TRANSITION" in out


def test_transition_only_catalogue_still_schedules_but_names_the_problem(tmp_path):
    """A catalogue of nothing but zero-id RMs has no positive control available,
    so on the board every one of them would be UNPROVABLE. The planner must not
    pretend otherwise."""
    rms = {"greybox": "0x00000000"}
    ov = _overlay(tmp_path, rms)
    pd = _prod(tmp_path, rms)
    rc, out, doc = _run(ov, pd, pd, jsonfile=tmp_path / "s.json")
    assert rc == 0, out
    assert doc["rms"][0]["verify_mode"] == "transition"
    assert "moved LAST" in out


def test_duplicate_rm_id_is_flagged_incoherent(tmp_path):
    """Two RMs sharing an rm_id: the RM_ID readback -- the ONLY check this sweep
    performs -- cannot distinguish them, so the check cannot fail for the right
    reason."""
    rms = {"led": "0x0100001E", "led_clone": "0x0100001E", "nanosoc": "0x01000001"}
    ov = _overlay(tmp_path, rms)
    pd = _prod(tmp_path, rms)
    rc, out, doc = _run(ov, pd, pd, jsonfile=tmp_path / "s.json")
    assert rc == 1, out
    dup = {r["rm_name"]: r for r in doc["rms"]}
    assert dup["led"]["plan_status"] == "DUP_RM_ID"
    assert dup["led_clone"]["plan_status"] == "DUP_RM_ID"
    assert dup["nanosoc"]["plan_status"] == "READY"
    assert "INCOHERENT" in out
    assert "cannot distinguish" in dup["led"]["plan_reason"]


def test_bad_manifest_is_flagged_not_skipped(tmp_path):
    rms = {"led": "0x0100001E"}
    ov = _overlay(tmp_path, rms)
    (ov / "broken").mkdir()
    (ov / "broken" / "manifest.json").write_text("{ this is not json")
    pd = _prod(tmp_path, list(rms) + ["broken"])
    rc, out, doc = _run(ov, pd, pd, jsonfile=tmp_path / "s.json")
    assert rc == 1, out
    bad = [r for r in doc["rms"] if r["rm_name"] == "broken"]
    assert bad and bad[0]["plan_status"] == "BAD_MANIFEST"
    assert "unparseable" in bad[0]["plan_reason"]


def test_manifest_without_rm_id_is_flagged(tmp_path):
    ov = _overlay(tmp_path, {"led": "0x0100001E"})
    (ov / "noid").mkdir()
    (ov / "noid" / "manifest.json").write_text(json.dumps(
        {"schema": 1, "static_id": SID_A, "rm_name": "noid"}) + "\n")
    pd = _prod(tmp_path, ["led", "noid"])
    rc, out, doc = _run(ov, pd, pd, jsonfile=tmp_path / "s.json")
    assert rc == 1, out
    noid = [r for r in doc["rms"] if r["rm_name"] == "noid"][0]
    assert noid["plan_status"] == "BAD_MANIFEST"
    assert "rm_id" in noid["plan_reason"]


def test_missing_partial_is_named_not_silently_dropped(tmp_path):
    """Overlay/prod artefacts are gitignored build output, absent in a fresh
    clone. Missing ones must SKIP LOUDLY (named, exit 0) -- and escalate to a
    FAILURE under --require-artefacts, which is what a lab box passes."""
    rms = {"led": "0x0100001E", "nanosoc": "0x01000001"}
    ov = _overlay(tmp_path, rms)
    pd = _prod(tmp_path, ["led"])          # nanosoc's partial deliberately absent
    rc, out, doc = _run(ov, pd, pd, jsonfile=tmp_path / "s.json")
    assert rc == 0, out
    ns = [r for r in doc["rms"] if r["rm_name"] == "nanosoc"][0]
    assert ns["plan_status"] == "NOT_BUILT"
    assert "config_rm_nanosoc_pblock_rp_dut_partial.bit" in ns["plan_reason"]
    assert "nanosoc" in out and "NOT_BUILT" in out
    assert "--require-artefacts" in out

    rc2, out2, doc2 = _run(ov, pd, pd, "--require-artefacts",
                           jsonfile=tmp_path / "s2.json")
    assert rc2 == 1, out2
    assert doc2["require_artefacts"] is True


def test_missing_base_bitstream_makes_every_rm_not_built(tmp_path):
    rms = {"led": "0x0100001E"}
    ov = _overlay(tmp_path, rms)
    pd = _prod(tmp_path, rms, with_base=False)
    rc, out, doc = _run(ov, pd, pd, "--require-artefacts",
                        jsonfile=tmp_path / "s.json")
    assert rc == 1, out
    assert doc["rms"][0]["plan_status"] == "NOT_BUILT"
    assert "base bitstream" in doc["rms"][0]["plan_reason"]


def test_max_cap_names_what_it_dropped(tmp_path):
    """No hidden truncation: a cap must print, and record, exactly what it left
    out -- otherwise a capped sweep reads as a full one."""
    rms = {"a_rm": "0x01000011", "b_rm": "0x01000012", "c_rm": "0x01000013"}
    ov = _overlay(tmp_path, rms)
    pd = _prod(tmp_path, rms)
    rc, out, doc = _run(ov, pd, pd, "--max", "1", jsonfile=tmp_path / "s.json")
    assert rc == 0, out
    assert "CAPPED by --max 1" in out
    assert len(doc["rms"]) == 1
    assert sorted(doc["capped_not_swept"]) == ["b_rm", "c_rm"]
    assert doc["max"] == 1


def test_bad_rms_selection_refuses(tmp_path):
    """A typo in --rms must refuse, not quietly sweep nothing."""
    rms = {"led": "0x0100001E"}
    ov = _overlay(tmp_path, rms)
    pd = _prod(tmp_path, rms)
    rc, out, _ = _run(ov, pd, pd, "--rms", "lled")
    assert rc == 2, out
    assert "not in the catalogue" in out


def test_selecting_away_every_rm_refuses(tmp_path):
    """A sweep of ZERO RMs exits 0 having verified nothing -- the canonical
    gate-that-cannot-fail. --skip that empties the catalogue must refuse."""
    rms = {"led": "0x0100001E", "nanosoc": "0x01000001"}
    ov = _overlay(tmp_path, rms)
    pd = _prod(tmp_path, rms)
    rc, out, _ = _run(ov, pd, pd, "--skip", "led,nanosoc")
    assert rc == 2, out
    assert "selected ZERO RMs" in out
    assert "verified nothing" in out


def test_negative_max_refuses(tmp_path):
    rms = {"led": "0x0100001E"}
    ov = _overlay(tmp_path, rms)
    pd = _prod(tmp_path, rms)
    rc, out, _ = _run(ov, pd, pd, "--max", "-1")
    assert rc == 2, out
    assert "schedules nothing" in out


def test_skip_and_rms_selection_are_recorded(tmp_path):
    rms = {"led": "0x0100001E", "nanosoc": "0x01000001", "greybox": "0x00000000"}
    ov = _overlay(tmp_path, rms)
    pd = _prod(tmp_path, rms)
    rc, out, doc = _run(ov, pd, pd, "--skip", "nanosoc",
                        jsonfile=tmp_path / "s.json")
    assert rc == 0, out
    assert {r["rm_name"] for r in doc["rms"]} == {"led", "greybox"}
    assert any("nanosoc" in e for e in doc["excluded_by_selection"])


def test_stale_manifest_static_id_warns_by_default_and_blocks_when_strict(tmp_path):
    """A manifest keyed to another shell is a WARNING on this JTAG path (the
    loaded artefact is the prod .bit; the checked value is rm_id, which
    rm_list.tcl assigns at design time and make check stage 2 already gates
    three ways) -- but --strict-manifest-static must be able to refuse."""
    rms = {"led": "0x0100001E"}
    ov = _overlay(tmp_path, rms, static_id=SID_B)   # manifest keyed to B
    pd = _prod(tmp_path, rms, static_id=SID_A)      # prod is A
    rc, out, doc = _run(ov, pd, pd, jsonfile=tmp_path / "s.json")
    assert rc == 0, out
    assert "keyed to a DIFFERENT static" in out
    assert doc["stale_manifest_rms"] == ["led:%s" % SID_B]
    assert doc["rms"][0]["plan_status"] == "READY"

    rc2, out2, doc2 = _run(ov, pd, pd, "--strict-manifest-static",
                           jsonfile=tmp_path / "s2.json")
    assert rc2 == 1, out2
    assert doc2["rms"][0]["plan_status"] == "STALE_MANIFEST"


def test_dry_run_never_names_keep_base(tmp_path):
    """Trap 2. mps3_swap_design.sh's clearing bitstream is hard-wired to
    GREYBOX's, correct only straight after a base load. With --keep-base on step
    N>1 the resident RM is the PREVIOUS design, so greybox's clearing would be
    used to clear something else -- an UltraScale DFX violation that corrupts the
    RP. The sweep must reload the base every step and never emit --keep-base."""
    rms = {"led": "0x0100001E", "nanosoc": "0x01000001"}
    ov = _overlay(tmp_path, rms)
    pd = _prod(tmp_path, rms)
    rc, out, _ = _run(ov, pd, pd)
    assert rc == 0, out
    # The banner explains it; no emitted command line may carry the flag.
    emitted = [ln for ln in out.splitlines() if "mps3_swap_design.sh" in ln]
    assert emitted
    assert not any("--keep-base" in ln for ln in emitted), emitted
    assert "no --keep-base, ever" in out
    # ...and no EXECUTED line may pass it (the script prints the flag name when
    # it explains the trap, which is not a use -- see _exec_lines).
    assert "--keep-base" not in _exec_src(_SWEEP)


def test_clearing_is_loaded_before_the_partial(tmp_path):
    """UltraScale DFX rule: clearing bitstream FIRST, always. The printed load
    order is the human-checkable record of that."""
    rms = {"led": "0x0100001E"}
    ov = _overlay(tmp_path, rms)
    pd = _prod(tmp_path, rms)
    rc, out, _ = _run(ov, pd, pd)
    assert rc == 0, out
    lines = out.splitlines()
    for i, ln in enumerate(lines):
        if "[2] clearing" in ln:
            assert "[3] partial" in lines[i + 1], lines[i:i + 3]
            break
    else:
        pytest.fail("dry run never printed the [1]base [2]clearing [3]partial order")


# ---------------------------------------------------------------------------
# anti-drift
# ---------------------------------------------------------------------------
def test_partial_filename_template_matches_the_swap_script():
    """The planner predicts artefact names; mps3_swap_design.sh loads them. If
    the two ever disagree the dry run reports READY for every RM and the real
    sweep fails on every RM -- a whole board window burnt on a naming change.
    Asserted against the swap script's own strings, not a duplicated constant."""
    swap = _SWAP.read_text()
    sweep = _SWEEP.read_text()
    for tmpl in (
        "config_rm_${DESIGN}_pblock_rp_dut_partial.bit",
        "config_rm_greybox_fw.bit",
        "config_rm_greybox_pblock_rp_dut_partial_clear.bit",
    ):
        assert tmpl in swap, "mps3_swap_design.sh no longer builds %r" % tmpl
        # the sweep uses ${rm} where the swap script uses ${DESIGN}
        assert tmpl.replace("${DESIGN}", "${rm}") in sweep, \
            "mps3_silicon_sweep.sh does not predict %r" % tmpl


def test_sweep_reuses_the_lease_via_the_documented_env_seam():
    """mps3_swap_design.sh only leaves a caller's lease alone when the token
    arrives via MPS3_LEASE_TOKEN. If the sweep stopped passing it, every step
    would take and RELEASE its own lease and another agent could slip in
    mid-sweep."""
    swap = _SWAP.read_text()
    sweep = _SWEEP.read_text()
    assert 'MPS3_LEASE_TOKEN' in swap and "will NOT be released here" in swap
    assert re.search(r'MPS3_LEASE_TOKEN="\$TOKEN"\s+MPS3_LEASE_HOLDER="\$HOLDER"', sweep)


def test_sweep_acquires_through_the_poller_not_mps3_board_acquire():
    """scripts/mps3_board.sh acquire does not survive a contended board: it
    passes --json and this fpgahub's QUEUED response carries no token, so a
    queued acquire errors out AND strands a queue entry. The sweep must use the
    poller."""
    code = _exec_src(_SWEEP)
    assert "mps3_lease_acquire.sh" in code
    offenders = [ln for ln in code.splitlines()
                 if "acquire" in ln and "mps3_lease_acquire.sh" not in ln]
    assert not offenders, (
        "the sweep must not call any acquire path other than the poller:\n"
        + "\n".join(offenders))
    # ...but release and heartbeat DO legitimately go through mps3_board.sh.
    assert 'mps3_board.sh" release' in code
    assert 'mps3_board.sh" heartbeat' in code


def test_sweep_releases_and_heartbeats_the_lease():
    """Never leak a lease: acquire, trap EXIT to release, heartbeat during long
    operations."""
    code = _exec_src(_SWEEP)
    assert re.search(r"trap\s+'release_lease'\s+EXIT", code)
    assert "heartbeat" in code
    # and it must NOT release a lease it does not own
    assert "OWN_LEASE" in code
    # A SIGNAL HANDLER MUST EXIT. Trapping INT/TERM to release and then falling
    # through would resume the sweep with NO LEASE HELD on a shared board -- the
    # exact failure mps3_board.sh exists to prevent.
    assert re.search(r"trap\s+'on_signal'\s+INT\s+TERM", code)
    handler = re.search(r"on_signal\(\)\s*\{(.*?)\n\}", _SWEEP.read_text(), re.S)
    assert handler, "no on_signal handler"
    assert re.search(r"^\s*exit\s+\d+", handler.group(1), re.M), \
        "the INT/TERM handler does not exit -- the sweep would carry on unleased"


_WALK = re.compile(
    r"set mbid \"\"\s*\n\s*set seen_ku 0\s*\n\s*foreach line \[split \[targets\] .*?"
    r"MicroBlaze #0.*?\}\s*\n\}",
    re.S,
)


def test_ku115_microblaze_walk_is_identical_everywhere():
    """THE SHARED-hw_server TRAP. Four other boards live on this hw_server and
    several expose a "MicroBlaze #0"; the enumeration order changes between
    sessions. Reading the lowest-numbered one silently drives the WRONG board and
    every register comes back 0x00000000 -- indistinguishable from "greybox
    resident". Three scripts carry the descendant-of-xcku115 walk. A fix applied
    to one and not the others is exactly the silent wrong-board read this guards,
    so they must stay byte-identical."""
    walks = {}
    for p in (_STATE, _SWAP, _RESET):
        m = _WALK.search(p.read_text())
        assert m, "%s no longer carries the xcku115-descendant MicroBlaze walk" % p.name
        walks[p.name] = m.group(0)
    uniq = set(walks.values())
    assert len(uniq) == 1, (
        "the xcku115 MicroBlaze walk has DRIFTED between:\n"
        + "\n".join("  %s:\n%s" % (k, v) for k, v in walks.items()))


def test_reset_release_asserts_consequences_not_just_the_write():
    """A blind `mwr 0x44A00000 0x7` proves nothing: DECOUPLE.en force-holds
    rp_resetn low regardless of RESET_CTRL, so the write can be accepted and have
    no effect. The helper must assert STATUS[1] clears AND rm_id_valid sets."""
    body = _RESET.read_text()
    assert "mwr 0x44A00000 0x7" in body
    assert "0x44A10008" in body and "0x44A10014" in body
    assert "rp_in_reset CLEARED" in body
    assert "rm_id_valid SET" in body
    assert "DECOUPLE.en is SET" in body


def test_scripts_are_executable_and_parse():
    for p in (_SWEEP, _RESET):
        assert os.access(p, os.X_OK), "%s is not executable" % p
        subprocess.run(["bash", "-n", str(p)], check=True,
                       capture_output=True, text=True)


def test_help_does_not_touch_anything():
    p = subprocess.run([str(_SWEEP), "--help"], capture_output=True, text=True,
                       timeout=60)
    assert p.returncode == 0
    assert "SILICON regression" in p.stdout
    # the traps must be in the --help output, not buried in the source
    assert "JTAG-VOLATILE" in p.stdout
    assert "clearing" in p.stdout


def test_board_mode_is_never_the_default():
    """The single most important safety property: running the script with no
    arguments must not touch the board."""
    code = _exec_src(_SWEEP)
    assert re.search(r"^ALLOW_BOARD=0$", code, re.M)
    assert "--allow-board" in code
