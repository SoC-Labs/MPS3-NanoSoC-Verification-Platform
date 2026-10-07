"""The SoCScope provenance gate: does a socscope overlay say which SoCScope built it?

rm_socscope's RTL is read from a sibling checkout at build time and never vendored, so
the only record of WHICH SoCScope a fielded overlay carries is what the manifest says.
Before `socscope_rev` there was nothing to say: the overlay built on 2026-08-10 was
keyed to a shell, dated, CRC'd -- and silent about the one input that decides whether a
board capture and the host decoder agree.

Properties, each with a control that must FAIL:

    a socscope manifest WITHOUT the field fails         (the gate is not decoration)
    a `-dirty` rev fails                                (unreproducible; never waivable)
    an unwaived `unknown` fails; a waived one passes    (waivers need a reason)
    a sha that does not resolve in the checkout fails; one that does passes
    a resolvable sha that is not HEAD is a NOTE, not a failure
    a manifest for a non-consumer without the field passes
    gen_manifest.py build --socscope-rev round-trips through verify

and, since the record written at SYNTH time landed (ooc_synth.tcl ->
overlay/<rm>/provenance.json), the manifest is held TO it:

    a record that agrees with the manifest passes
    a record that says the tree was DIRTY fails         (and cannot be re-stamped away)
    a record naming a different commit fails            (the original defect, reproduced)
    an unreadable record fails rather than being skipped
    --require-provenance makes a missing record fatal   (the burn-down lever)

Controls run against scratch git repos under tmp_path -- a platform-shaped tree carrying
only the overlay manifests + waiver file, and a one-commit "SoCScope" checkout -- never
the real repo, exactly like test_fielded_shell_claims.py.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[2]
_GATE = _REPO / "scripts" / "harness_gates" / "check_socscope_overlay_rev.py"
_WAIVERS = _REPO / "scripts" / "harness_gates" / "socscope_rev_waivers.txt"
_GEN = _REPO / "fpga" / "dfx" / "gen_manifest.py"
_REAL_SOCSCOPE = _REPO.parent / "SoCScope"

SHA = "0123456789ab"   # a plausible abbreviated sha that exists in no checkout


def _run(repo, home, *extra):
    return subprocess.run(
        [sys.executable, str(_GATE), "--repo", str(repo), "--socscope-home", str(home), *extra],
        capture_output=True, text=True)


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _manifest(rm_name, rm_id="0x01000006", **extra):
    m = {
        "schema": 1, "static_id": "0xA8C1C535", "rm_id": rm_id, "rm_name": rm_name,
        "clearing": {"file": "%s_clear.bin" % rm_name, "len": 4, "crc32": "0x00000000"},
        "partial": {"file": "%s.bin" % rm_name, "len": 4, "crc32": "0x00000000"},
        "built": "2026-08-10", "vivado": "2024.1",
    }
    m.update(extra)
    return m


def _scratch_platform(tmp_path, manifests, waiver_lines=()):
    """A platform-shaped git repo: overlay manifests + the waiver file, nothing else."""
    repo = tmp_path / "platform"
    for m in manifests:
        d = repo / "fpga" / "dfx" / "overlay" / m["rm_name"]
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps(m, indent=2) + "\n")
    w = repo / "scripts" / "harness_gates" / "socscope_rev_waivers.txt"
    w.parent.mkdir(parents=True)
    w.write_text("# scratch waivers\n" + "".join(l + "\n" for l in waiver_lines))
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    return repo


def _scratch_socscope(tmp_path, commits=1):
    """A 'SoCScope' checkout with N commits; returns (path, [full shas, oldest first])."""
    home = tmp_path / "SoCScope"
    home.mkdir()
    _git(home, "init", "-q")
    _git(home, "config", "user.email", "t@example.invalid")
    _git(home, "config", "user.name", "t")
    shas = []
    for i in range(commits):
        (home / "f.txt").write_text("rev %d\n" % i)
        _git(home, "add", "f.txt")
        _git(home, "commit", "-q", "-m", "c%d" % i)
        shas.append(subprocess.run(["git", "-C", str(home), "rev-parse", "HEAD"],
                                   capture_output=True, text=True, check=True).stdout.strip())
    return home, shas


# --------------------------------------------------------------------------- #
# (a) the real repo
# --------------------------------------------------------------------------- #

def test_the_repo_passes_right_now():
    """Runs with whatever SoCScope checkout the machine has (the gate's own default),
    so it also proves the checkout-absent path does not fail on a fresh clone."""
    r = subprocess.run([sys.executable, str(_GATE), "--repo", str(_REPO)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_real_socscope_manifest_carries_the_field():
    m = json.loads((_REPO / "fpga" / "dfx" / "overlay" / "socscope" / "manifest.json").read_text())
    assert "socscope_rev" in m, "socscope/manifest.json records no socscope_rev"


def test_every_real_waiver_names_a_real_overlay_with_a_reason():
    """A waiver for an overlay that no longer exists is a licence nobody is using."""
    sys.path.insert(0, str(_GATE.parent))
    import check_socscope_overlay_rev as g
    waivers = g.load_waivers(str(_WAIVERS))
    for rm, reason in waivers.items():
        assert (_REPO / "fpga" / "dfx" / "overlay" / rm / "manifest.json").is_file(), rm
        assert len(reason) > 20, "waiver for %s has no real reason: %r" % (rm, reason)


# --------------------------------------------------------------------------- #
# the controls
# --------------------------------------------------------------------------- #

def test_CONTROL_socscope_manifest_without_the_field_fails(tmp_path):
    home, _ = _scratch_socscope(tmp_path)
    repo = _scratch_platform(tmp_path, [_manifest("socscope")])
    r = _run(repo, home)
    assert r.returncode == 1, r.stdout
    assert "records no socscope_rev" in r.stdout
    assert "socscope/manifest.json" in r.stdout


def test_CONTROL_dirty_rev_fails(tmp_path):
    home, shas = _scratch_socscope(tmp_path)
    repo = _scratch_platform(tmp_path, [_manifest("socscope", socscope_rev=shas[0][:12] + "-dirty")])
    r = _run(repo, home)
    assert r.returncode == 1, r.stdout
    assert "DIRTY" in r.stdout


def test_CONTROL_dirty_rev_cannot_be_waived(tmp_path):
    home, shas = _scratch_socscope(tmp_path)
    repo = _scratch_platform(tmp_path, [_manifest("socscope", socscope_rev=shas[0][:12] + "-dirty")],
                             waiver_lines=["socscope  # trying to waive a dirty build"])
    r = _run(repo, home)
    assert r.returncode == 1, r.stdout


def test_CONTROL_unwaived_unknown_fails(tmp_path):
    home, _ = _scratch_socscope(tmp_path)
    repo = _scratch_platform(tmp_path, [_manifest("socscope", socscope_rev="unknown")])
    r = _run(repo, home)
    assert r.returncode == 1, r.stdout
    assert "not waived" in r.stdout


def test_waived_unknown_passes_and_says_so(tmp_path):
    home, _ = _scratch_socscope(tmp_path)
    repo = _scratch_platform(tmp_path, [_manifest("socscope", socscope_rev="unknown")],
                             waiver_lines=["socscope  # built before provenance existed"])
    r = _run(repo, home)
    assert r.returncode == 0, r.stdout
    assert "WAIVED: built before provenance existed" in r.stdout


def test_a_waiver_without_a_reason_is_not_a_waiver(tmp_path):
    home, _ = _scratch_socscope(tmp_path)
    repo = _scratch_platform(tmp_path, [_manifest("socscope", socscope_rev="unknown")],
                             waiver_lines=["socscope"])
    r = _run(repo, home)
    assert r.returncode == 1, r.stdout


def test_CONTROL_unresolvable_sha_fails(tmp_path):
    home, _ = _scratch_socscope(tmp_path)
    repo = _scratch_platform(tmp_path, [_manifest("socscope", socscope_rev=SHA)])
    r = _run(repo, home)
    assert r.returncode == 1, r.stdout
    assert "does not resolve" in r.stdout


def test_resolvable_head_sha_passes_without_a_note(tmp_path):
    home, shas = _scratch_socscope(tmp_path)
    repo = _scratch_platform(tmp_path, [_manifest("socscope", socscope_rev=shas[-1][:12])])
    r = _run(repo, home)
    assert r.returncode == 0, r.stdout
    assert "PREDATES" not in r.stdout


def test_older_resolvable_sha_is_a_NOTE_not_a_failure(tmp_path):
    home, shas = _scratch_socscope(tmp_path, commits=2)
    repo = _scratch_platform(tmp_path, [_manifest("socscope", socscope_rev=shas[0][:12])])
    r = _run(repo, home)
    assert r.returncode == 0, r.stdout
    assert "PREDATES the checkout" in r.stdout


def test_non_consumer_without_the_field_passes(tmp_path):
    """rm_nanosoc may or may not be a B2 build; the flow cannot tell, so it is not required."""
    home, _ = _scratch_socscope(tmp_path)
    repo = _scratch_platform(tmp_path, [_manifest("nanosoc", rm_id="0x01000001")])
    r = _run(repo, home)
    assert r.returncode == 0, r.stdout


def test_a_field_on_a_non_consumer_is_still_validated(tmp_path):
    """A hand-recorded B2 rev is welcome, but a bogus one is a lie the gate must catch."""
    home, _ = _scratch_socscope(tmp_path)
    repo = _scratch_platform(tmp_path, [_manifest("nanosoc", rm_id="0x01000001", socscope_rev=SHA)])
    r = _run(repo, home)
    assert r.returncode == 1, r.stdout


def test_absent_checkout_still_runs_the_structural_checks(tmp_path):
    """A fresh CI clone has no sibling SoCScope. Presence/dirty/unknown must still gate;
    only sha resolution is skipped, loudly. --require-checkout turns absence into a fail."""
    nohome = tmp_path / "nowhere"
    dirty = _scratch_platform(tmp_path, [_manifest("socscope", socscope_rev=SHA + "-dirty")])
    r = _run(dirty, nohome)
    assert r.returncode == 1, r.stdout
    sha_only = tmp_path / "p2"
    sha_only.mkdir()
    d = sha_only / "fpga" / "dfx" / "overlay" / "socscope"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps(_manifest("socscope", socscope_rev=SHA)))
    r = _run(sha_only, nohome)
    assert r.returncode == 0, r.stdout
    assert "NOT checked" in r.stdout
    r = _run(sha_only, nohome, "--require-checkout")
    assert r.returncode == 1, r.stdout


# --------------------------------------------------------------------------- #
# (f) gen_manifest.py round-trip
# --------------------------------------------------------------------------- #

def _gen(*args):
    return subprocess.run([sys.executable, str(_GEN), *args], capture_output=True, text=True)


@pytest.mark.parametrize("rev", ["50364142d9c1", "50364142d9c1-dirty", "unknown"])
def test_gen_manifest_records_socscope_rev_and_verify_accepts_it(tmp_path, rev):
    """build records exactly what it was told (the truth, dirty or not -- refusing to
    record is how provenance goes missing); verify round-trips it."""
    (tmp_path / "p.bin").write_bytes(b"\x01\x02\x03\x04")
    (tmp_path / "c.bin").write_bytes(b"\x05\x06\x07\x08")
    r = _gen("build", "--rm-name", "socscope", "--rm-id", "0x01000006",
             "--static-id", "0xA8C1C535", "--partial", str(tmp_path / "p.bin"),
             "--clearing", str(tmp_path / "c.bin"), "--socscope-rev", rev,
             "--copy", "--out-root", str(tmp_path / "overlay"))
    assert r.returncode == 0, r.stdout + r.stderr
    mpath = tmp_path / "overlay" / "socscope" / "manifest.json"
    m = json.loads(mpath.read_text())
    assert m["socscope_rev"] == rev
    assert m["schema"] == 1
    r = _gen("verify", str(mpath))
    assert r.returncode == 0, r.stdout + r.stderr


def test_gen_manifest_build_rejects_a_malformed_rev(tmp_path):
    (tmp_path / "p.bin").write_bytes(b"\x01")
    (tmp_path / "c.bin").write_bytes(b"\x02")
    r = _gen("build", "--rm-name", "socscope", "--rm-id", "0x01000006",
             "--static-id", "0xA8C1C535", "--partial", str(tmp_path / "p.bin"),
             "--clearing", str(tmp_path / "c.bin"), "--socscope-rev", "v1.2",
             "--out-root", str(tmp_path / "overlay"))
    assert r.returncode != 0
    assert "socscope-rev" in (r.stdout + r.stderr)


def test_verify_accepts_absence_and_rejects_a_malformed_value(tmp_path):
    """Optional field: every pre-existing manifest lacks it and must still verify."""
    (tmp_path / "p.bin").write_bytes(b"\x01")
    (tmp_path / "c.bin").write_bytes(b"\x02")
    r = _gen("build", "--rm-name", "greybox", "--rm-id", "0x01000000",
             "--static-id", "0xA8C1C535", "--partial", str(tmp_path / "p.bin"),
             "--clearing", str(tmp_path / "c.bin"), "--copy",
             "--out-root", str(tmp_path / "overlay"))
    assert r.returncode == 0, r.stderr
    mpath = tmp_path / "overlay" / "greybox" / "manifest.json"
    m = json.loads(mpath.read_text())
    assert "socscope_rev" not in m
    assert _gen("verify", str(mpath)).returncode == 0
    m["socscope_rev"] = 12345
    mpath.write_text(json.dumps(m))
    r = _gen("verify", str(mpath))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "socscope_rev" in (r.stdout + r.stderr)


# --------------------------------------------------------------------------- #
# the synth-time provenance record (fpga/dfx/rms/rm_socscope/ooc_synth.tcl)
#
# The manifest's rev used to be stamped by `make overlays` from whatever
# $SOCSCOPE_HOME was on AT THAT MOMENT, with "run the two targets back to back" as
# the mitigation. That is a procedure, not a check, and it had already failed: the
# 2026-08-10 overlay's checkpoint (23:19:37) predates BOTH commits that describe how
# it was built -- SoCScope 9a734d5 at 23:28:53 and this repo's 93d6815 at 23:33:45 --
# so it was synthesised from two dirty trees and NO sha names it.
#
# ooc_synth.tcl now writes the revision from inside the process that read the files,
# and `make overlays` copies that record next to the manifest. These are the controls
# for the two ways that record can contradict the manifest. Each must FAIL: a
# cross-check that cannot go red is a comment.
# --------------------------------------------------------------------------- #

def _provenance(rev, dirty=False, sources=True):
    rec = {
        "schema": "rm-socscope-provenance", "schema_version": 1,
        "rm_key": "rm_socscope", "rm_name": "socscope", "rm_id": "0x01000006",
        "synthesised_at": "2026-09-14T14:51:16Z", "vivado": "2024.1",
        "part": "xcku115-flvb1760-1-c", "socscope_home": "/nowhere",
        "socscope_rev": rev, "socscope_dirty": dirty, "generics": {},
        "sources_digest": "0" * 64,
    }
    if sources:
        rec["sources"] = [{"path": "SOCSCOPE_HOME/hw/rtl/socscope_ring.sv",
                           "sha256": "1" * 64}]
    return rec


def _with_provenance(repo, rm_name, rec):
    p = repo / "fpga" / "dfx" / "overlay" / rm_name / "provenance.json"
    p.write_text(json.dumps(rec, indent=2) + "\n")
    return p


def test_provenance_agreeing_with_the_manifest_passes(tmp_path):
    home, shas = _scratch_socscope(tmp_path)
    rev = shas[0][:12]
    repo = _scratch_platform(tmp_path, [_manifest("socscope", socscope_rev=rev)])
    _with_provenance(repo, "socscope", _provenance(rev))
    r = _run(repo, home)
    assert r.returncode == 0, r.stdout


def test_CONTROL_provenance_from_a_dirty_tree_fails(tmp_path):
    """`socscope_dirty` cannot be hidden by re-stamping the manifest later, which is
    exactly what a `-dirty` suffix CAN be."""
    home, shas = _scratch_socscope(tmp_path)
    rev = shas[0][:12]
    repo = _scratch_platform(tmp_path, [_manifest("socscope", socscope_rev=rev)])
    _with_provenance(repo, "socscope", _provenance(rev, dirty=True))
    r = _run(repo, home)
    assert r.returncode == 1, r.stdout
    assert "DIRTY" in r.stdout


def test_CONTROL_a_manifest_stamped_from_a_moved_tree_fails(tmp_path):
    """The original defect, reproduced: the manifest names HEAD-at-overlays-time and
    the record names HEAD-at-synth-time, and they are different commits."""
    home, shas = _scratch_socscope(tmp_path, commits=2)
    synth_rev, stamped_rev = shas[0][:12], shas[1][:12]
    repo = _scratch_platform(tmp_path, [_manifest("socscope", socscope_rev=stamped_rev)])
    _with_provenance(repo, "socscope", _provenance(synth_rev))
    r = _run(repo, home)
    assert r.returncode == 1, r.stdout
    assert "does NOT match the synth-time record" in r.stdout


def test_CONTROL_a_broken_provenance_file_fails_rather_than_being_ignored(tmp_path):
    home, shas = _scratch_socscope(tmp_path)
    rev = shas[0][:12]
    repo = _scratch_platform(tmp_path, [_manifest("socscope", socscope_rev=rev)])
    (repo / "fpga" / "dfx" / "overlay" / "socscope" / "provenance.json").write_text("{ not json")
    r = _run(repo, home)
    assert r.returncode == 1, r.stdout
    assert "unreadable provenance.json" in r.stdout


def test_require_provenance_makes_a_missing_record_fatal(tmp_path):
    """The burn-down lever: a NOTE by default, a failure once every consumer overlay
    has been rebuilt through ooc_synth.tcl."""
    home, shas = _scratch_socscope(tmp_path)
    rev = shas[0][:12]
    repo = _scratch_platform(tmp_path, [_manifest("socscope", socscope_rev=rev)])
    assert _run(repo, home).returncode == 0
    r = _run(repo, home, "--require-provenance")
    assert r.returncode == 1, r.stdout
    assert "no provenance.json" in r.stdout


def test_the_real_overlay_carries_a_clean_synth_time_record():
    """Not a scratch repo: the shipped overlay itself. This is what the 2026-08-10
    overlay could not have and why it was rebuilt."""
    p = _REPO / "fpga" / "dfx" / "overlay" / "socscope" / "provenance.json"
    assert p.is_file(), ("the shipped socscope overlay carries no provenance.json -- "
                        "rebuild it: make -C fpga/dfx add-rm-socscope BUILD=<locked tree> "
                        "&& make -C fpga/dfx overlays")
    rec = json.loads(p.read_text())
    assert rec["schema"] == "rm-socscope-provenance"
    assert rec["socscope_dirty"] is False, "the shipped overlay was built from a dirty tree"
    m = json.loads((_REPO / "fpga" / "dfx" / "overlay" / "socscope" / "manifest.json").read_text())
    assert m["socscope_rev"].startswith(rec["socscope_rev"][:7])
    assert rec["sources"], "no per-source digests recorded"
