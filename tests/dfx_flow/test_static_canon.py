"""tests/dfx_flow/test_static_canon.py

Board-free gates for the THREE identities a mint now produces, and for the
cross-check that makes one of them readable on hardware.

    static_id      CRC-32 of static_routed_locked.dcp -- "will this partial FIT
                   this fabric?" Untouched here: it is baked into every fielded
                   overlay and changing it is a re-mint.
    static_canon   SHA-256 of the SOURCES that decide the static -- "are these
                   the same INPUTS?" The question static_id structurally cannot
                   answer, because a .dcp is a zip with embedded timestamps and
                   a no-op rebuild therefore mints a different id.
    static_usercode BITSTREAM.CONFIG.USERID, which the device reports back as
                   REGISTER.USERCODE -- "which IMPLEMENTATION RUN is this?"

WHAT IS GATED HERE, AND THE CONTROL FOR EACH
--------------------------------------------
1. THE DIGEST IS OVER CONTENT, NOT OVER TIME. Two mint records built from the
   same inputs at different moments carry the IDENTICAL digest (the brief's
   control), and a ONE-BIT change to any declared source moves it. The linked
   .dcp -- a timestamped artefact -- is recorded beside the digest and provably
   not inside it.

2. THE INPUT LIST CANNOT ROT. A hash over inputs is only as good as the list of
   inputs, and a list of files is the classic place for a gate to die quietly.
   static_inputs.txt's three rules each have a mutation control here: a stale
   include glob, an undeclared design source, an unexplained exclusion. Plus a
   COMPLETENESS cross-check: every source file build_shell.tcl and
   package_csr_ip.tcl literally name must be in the hashed set. (That check is
   what found fpga/ethernet/ -- a SIBLING of fpga/shell/ that a hand-written
   list would have missed.)

3. THE OVERLAYS CARRY THE IMPLEMENTATION IDENTITY. gen_manifest.py has had a
   `static_usercode` field since c0784ed, pyverify reads it and
   check_image_overlay_match.py gates it -- and `make overlays` never passed it,
   so NOT ONE manifest in fpga/dfx/overlay/ had it. A guard with no data in it
   is not a guard. Controls: an unstamped bitstream is refused, a disagreeing
   build record is fatal, and --check fails on an unbound overlay.

4. THE ver32-vs-USR_ACCESS CROSS-CHECK HAS THREE STATES. agree / SKEW / NOT
   CHECKED -- and the third must never read as the first. Gated on the firmware
   codec's own format strings and on the host client's parse of them.

Everything here is stdlib + tclsh + make + host python. No simulator, no board,
no Vivado. The one test that reads a real 11 MB bitstream skips when it is
absent (it is gitignored).
"""

import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
DFX = REPO / "fpga" / "dfx"
TOOLS = DFX / "tools"
CANON = TOOLS / "static_canon.py"
DECL = TOOLS / "static_inputs.txt"
STAMP_TOOL = TOOLS / "stamp_usercode.py"
STAMP_TCL = TOOLS / "static_stamp.tcl"
MINT_RECORD = TOOLS / "mint_record.py"
BUILD_DFX_TCL = DFX / "build_dfx.tcl"
NET_PROTO_C = REPO / "firmware" / "common" / "net_proto.c"
NET_PROTO_H = REPO / "firmware" / "common" / "net_proto.h"
CLIENT_PY = REPO / "host" / "pyverify" / "pyverify" / "client.py"
#: (archive dir, expected USERID) for every PRESERVED boot image in the tree.
#: Each is a real Vivado bitstream, read from its header, never re-typed -- and
#: two images with two different USERIDs is a stronger check on the reader than
#: one. The FIRST row is the shell on the board (docs/FIELDED_SHELL.md); the
#: rest are superseded archives, kept because their bytes are still real.
#: Re-pointed 2026-09-22 when 0x3F1A560F was fielded: the old single constant
#: was named FIELDED_* and still held 0xA8C1C535's usercode, which would have
#: gone on asserting a dead shell's id as "what its overlays must carry".
PRESERVED_BITS = [
    ("0x3F1A560F", "0xD46FCDCB"),
    ("0xA8C1C535", "0x875DB8BB"),
]
#: A SAMPLE usercode, for the FAKE bitstreams the fixtures below build. It is
#: separate from PRESERVED_BITS on purpose. It was not: a single constant named
#: FIELDED_USERCODE held 0xA8C1C535's real usercode AND served as the value two
#: fixtures asserted, so a BOARD FACT was doing duty as a FIXTURE VALUE and
#: re-pointing it at the newly fielded shell broke two unrelated tests. The
#: sample deliberately stays 0x875DB8BB -- what a fixture asserts is that the
#: tool round-trips the bytes it was handed, and re-typing it would prove
#: nothing extra.
SAMPLE_USERCODE = "0x875DB8BB"


def run(*args, cwd=None, check=False):
    return subprocess.run([sys.executable, *[str(a) for a in args]],
                          cwd=str(cwd or REPO), capture_output=True, text=True,
                          check=check)


def canon(*args, cwd=None):
    return run(CANON, *args, cwd=cwd)


# ---------------------------------------------------------------------------
# a throwaway git repo, so the mutation controls act on a tree we own
# ---------------------------------------------------------------------------
def tiny_repo(tmp_path, files, decl_text):
    """A git checkout with `files` ({relpath: text}) and its own declaration.

    The real declaration is checked against the real tree elsewhere; these
    fixtures exist so a control can BREAK a declaration without touching the
    repo two other lanes are editing.
    """
    root = tmp_path / "repo"
    root.mkdir()
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True)
    decl = root / "decl.txt"
    decl.write_text(decl_text)
    return root, decl


BASE_FILES = {
    "src/top.sv": "module top; endmodule\n",
    "src/leaf.sv": "module leaf; endmodule\n",
    "src/pins.xdc": "# pins\n",
    "notes/README.md": "not a design source\n",
    "src/scratch.tcl": "# a helper nothing sources\n",
}
BASE_DECL = (
    "root src\n"
    "include src/top.sv\n"
    "include src/leaf.sv\n"
    "include src/pins.xdc\n"
    "exclude src/scratch.tcl # a helper nothing sources\n"
)


def digest_of(root, decl, *extra):
    proc = canon("compute", "--repo", root, "--decl", decl,
                 "--part", "xcku115-flvb1760-1-c", "--vivado", "2024.1", *extra)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


# ===========================================================================
# 1. the digest is over CONTENT, not over time
# ===========================================================================
def test_the_same_sources_hash_the_same_twice(tmp_path):
    """The whole point. static_id cannot do this: it is a CRC of a .dcp, which
    is a zip with embedded timestamps, so the same sources built twice mint two
    different ids and "did anything change?" has no answer anywhere."""
    root, decl = tiny_repo(tmp_path, BASE_FILES, BASE_DECL)
    first = digest_of(root, decl)
    second = digest_of(root, decl)
    assert first["digest"] == second["digest"]
    assert len(first["digest"]) == 64
    assert first["input_count"] == 3


def test_one_bit_of_one_source_moves_the_digest(tmp_path):
    """The control that makes the stability above meaningful. Without it, a
    constant would pass the test above."""
    root, decl = tiny_repo(tmp_path, BASE_FILES, BASE_DECL)
    before = digest_of(root, decl)["digest"]

    leaf = root / "src" / "leaf.sv"
    # one bit: 'leaf' -> 'lebf' (0x61 -> 0x62)
    leaf.write_bytes(leaf.read_bytes().replace(b"leaf;", b"lebf;", 1))
    after = digest_of(root, decl)["digest"]
    assert after != before, "a one-bit source change did not move the digest"


def test_an_excluded_file_does_not_move_the_digest(tmp_path):
    """Excluded means excluded: the digest is over the declared inputs, so a
    change to something declared irrelevant must NOT look like a change."""
    root, decl = tiny_repo(tmp_path, BASE_FILES, BASE_DECL)
    before = digest_of(root, decl)["digest"]
    (root / "src" / "scratch.tcl").write_text("# edited\n")
    assert digest_of(root, decl)["digest"] == before


def test_a_build_flag_is_part_of_the_identity(tmp_path):
    """SHELL_TOUCH=1 puts an AXI IIC master in the fabric. Two shells that
    differ by that are not the same shell, and the hash must say so."""
    root, decl = tiny_repo(tmp_path, BASE_FILES, BASE_DECL)
    off = digest_of(root, decl, "--flag", "SHELL_TOUCH=0")["digest"]
    on = digest_of(root, decl, "--flag", "SHELL_TOUCH=1")["digest"]
    assert off != on
    # and an ABSENT flag is a third state, not a synonym for =0
    assert digest_of(root, decl)["digest"] not in (off, on)


def test_the_part_and_the_tool_version_are_part_of_the_identity(tmp_path):
    root, decl = tiny_repo(tmp_path, BASE_FILES, BASE_DECL)
    base = digest_of(root, decl)["digest"]
    other_part = canon("compute", "--repo", root, "--decl", decl,
                       "--part", "xcku040-flvb1760-1-c", "--vivado", "2024.1")
    assert json.loads(other_part.stdout)["digest"] != base
    other_tool = canon("compute", "--repo", root, "--decl", decl,
                       "--part", "xcku115-flvb1760-1-c", "--vivado", "2025.1")
    assert json.loads(other_tool.stdout)["digest"] != base


def test_the_linked_dcp_is_recorded_but_never_hashed(tmp_path):
    """A timestamped artefact beside a content hash must be on the RIGHT side of
    the line. Hashing the .dcp in would put the whole defect straight back."""
    root, decl = tiny_repo(tmp_path, BASE_FILES, BASE_DECL)
    dcp_a = tmp_path / "a.dcp"
    dcp_a.write_bytes(b"PK\x03\x04 pretend checkpoint A")
    dcp_b = tmp_path / "b.dcp"
    dcp_b.write_bytes(b"PK\x03\x04 pretend checkpoint B -- different bytes")

    rec_a = digest_of(root, decl, "--linked-dcp", dcp_a)
    rec_b = digest_of(root, decl, "--linked-dcp", dcp_b)
    assert rec_a["digest"] == rec_b["digest"], \
        "the linked .dcp leaked into the digest"
    assert rec_a["linked_static_dcp"]["sha256"] != rec_b["linked_static_dcp"]["sha256"]
    assert "NOT HASHED" in rec_a["linked_static_dcp"]["note"]


def test_the_manifest_is_the_documented_shape(tmp_path):
    """`explain` prints the exact bytes hashed, and the digest is sha256 of
    them -- so the definition in the docstring is checkable, not a promise."""
    root, decl = tiny_repo(tmp_path, BASE_FILES, BASE_DECL)
    proc = canon("explain", "--repo", root, "--decl", decl,
                 "--part", "xcku115-flvb1760-1-c", "--vivado", "2024.1")
    assert proc.returncode == 0, proc.stderr
    text = proc.stdout
    lines = text.splitlines()
    assert lines[0] == "mps3-static-canon/1"
    assert lines[1] == "part\txcku115-flvb1760-1-c"
    assert lines[2] == "tool\tvivado\t2024.1"
    src_paths = [ln.split("\t")[2] for ln in lines if ln.startswith("src\t")]
    assert src_paths == sorted(src_paths), "src rows are not sorted bytewise"
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == \
        digest_of(root, decl)["digest"]


def test_verify_catches_a_source_that_moved(tmp_path):
    root, decl = tiny_repo(tmp_path, BASE_FILES, BASE_DECL)
    out = tmp_path / "canon.json"
    assert canon("compute", "--repo", root, "--decl", decl,
                 "--part", "p", "--vivado", "2024.1", "-o", out).returncode == 0
    assert canon("verify", "--repo", root, "--decl", decl, out).returncode == 0
    (root / "src" / "top.sv").write_text("module top; wire x; endmodule\n")
    bad = canon("verify", "--repo", root, "--decl", decl, out)
    assert bad.returncode == 1
    assert "DIGEST MISMATCH" in bad.stderr


# ===========================================================================
# 2. the input list cannot rot -- one mutation control per rule
# ===========================================================================
def test_the_repos_own_declaration_passes(tmp_path):
    """The baseline. Every control below must FAIL where this one passes."""
    proc = canon("check", "--repo", REPO)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_rule1_a_glob_that_matches_nothing_is_a_failure(tmp_path):
    """A stale include looks EXACTLY like a healthy one from outside: the tool
    exits 0 and the file it was written to cover is covered by nothing."""
    root, decl = tiny_repo(tmp_path, BASE_FILES,
                           BASE_DECL + "include src/renamed_away.sv\n")
    proc = canon("check", "--repo", root, "--decl", decl)
    assert proc.returncode == 1
    assert "src/renamed_away.sv" in proc.stderr
    assert "matches no tracked file" in proc.stderr
    # and compute refuses too -- a broken declaration must not yield a digest
    assert canon("compute", "--repo", root, "--decl", decl,
                 "--part", "p", "--vivado", "2024.1").returncode != 0


def test_rule2_an_undeclared_design_source_is_a_failure(tmp_path):
    """THE anti-rot rule. A new .sv under a declared root that nobody declared
    would otherwise be silently outside the hash forever."""
    files = dict(BASE_FILES)
    files["src/newly_added.sv"] = "module newly_added; endmodule\n"
    root, decl = tiny_repo(tmp_path, files, BASE_DECL)
    proc = canon("check", "--repo", root, "--decl", decl)
    assert proc.returncode == 1
    assert "src/newly_added.sv" in proc.stderr
    assert "neither `include`d nor `exclude`d" in proc.stderr


def test_rule2_ignores_files_that_are_not_design_sources(tmp_path):
    """The control for the control: a README under a root is not a failure, or
    the rule would be unusable and would get switched off."""
    files = dict(BASE_FILES)
    files["src/NOTES.md"] = "# prose\n"
    root, decl = tiny_repo(tmp_path, files, BASE_DECL)
    assert canon("check", "--repo", root, "--decl", decl).returncode == 0


def test_rule2_ignores_files_outside_every_root(tmp_path):
    files = dict(BASE_FILES)
    files["elsewhere/other.sv"] = "module other; endmodule\n"
    root, decl = tiny_repo(tmp_path, files, BASE_DECL)
    assert canon("check", "--repo", root, "--decl", decl).returncode == 0


def test_rule3_an_exclusion_without_a_reason_is_a_failure(tmp_path):
    """"Not hashed" is a claim about the build. Unexplained, it is
    indistinguishable from someone silencing a failing gate."""
    root, decl = tiny_repo(
        tmp_path, BASE_FILES,
        BASE_DECL.replace("exclude src/scratch.tcl # a helper nothing sources",
                          "exclude src/scratch.tcl"))
    proc = canon("check", "--repo", root, "--decl", decl)
    assert proc.returncode != 0
    combined = proc.stdout + proc.stderr
    assert "carries no '# reason'" in combined


def test_include_beats_exclude(tmp_path):
    """The precedence that lets one broad exclude carry the rationale for a
    subtree while a single file inside it is still hashed -- which is exactly
    how fpga/dfx/rms/* and rm_greybox are declared."""
    files = dict(BASE_FILES)
    files["src/sub/keep.sv"] = "module keep; endmodule\n"
    files["src/sub/drop.sv"] = "module drop; endmodule\n"
    decl_text = (BASE_DECL
                 + "exclude src/sub/* # the subtree does not reach the netlist\n"
                 + "include src/sub/keep.sv\n")
    root, decl = tiny_repo(tmp_path, files, decl_text)
    proc = canon("list", "--repo", root, "--decl", decl)
    assert proc.returncode == 0, proc.stderr
    listed = proc.stdout.split()
    assert "src/sub/keep.sv" in listed
    assert "src/sub/drop.sv" not in listed


def test_untracked_files_are_not_hashed(tmp_path):
    """The set is `git ls-files`. An untracked source is unreproducible by
    definition, and a digest that moved with scratch files in the tree would be
    useless."""
    root, decl = tiny_repo(tmp_path, BASE_FILES, BASE_DECL)
    before = digest_of(root, decl)["digest"]
    (root / "src" / "untracked.sv").write_text("module untracked; endmodule\n")
    assert digest_of(root, decl)["digest"] == before
    assert canon("check", "--repo", root, "--decl", decl).returncode == 0


def test_the_routed_artefacts_are_not_among_the_inputs():
    """A CONTENT hash that quietly hashed a build product would be static_id
    with extra steps."""
    proc = canon("list", "--repo", REPO)
    assert proc.returncode == 0, proc.stderr
    listed = proc.stdout.split()
    for artefact in ("static_routed_locked.dcp", "shell_static_synth.dcp",
                     "config_rm_greybox.bit", "static_id.txt"):
        assert not any(artefact in p for p in listed), \
            "%s reached the canonical input set" % artefact


def test_completeness_every_source_the_shell_build_names_is_hashed():
    """THE COMPLETENESS CROSS-CHECK.

    static_inputs.txt is a hand-written declaration, so "is it complete?" cannot
    be answered by reading it. This answers it mechanically: every file
    build_shell.tcl and package_csr_ip.tcl literally name -- through
    `[file join $shell_dir ...]` / `[file join $eth_dir ...]` -- must be in the
    hashed set. That is what put fpga/ethernet/ in the declaration: it is a
    SIBLING of fpga/shell/, and a list written by reading fpga/shell/ alone
    would have missed the whole virtual-PHY subsystem.
    """
    proc = canon("list", "--repo", REPO)
    assert proc.returncode == 0, proc.stderr
    hashed = set(proc.stdout.split())

    shell_dir = REPO / "fpga" / "shell"
    eth_dir = REPO / "fpga" / "ethernet"
    named = set()
    for tcl in (shell_dir / "build_shell.tcl",
                shell_dir / "ip_packaged" / "package_csr_ip.tcl"):
        text = tcl.read_text()
        for var, base in (("shell_dir", shell_dir), ("eth_dir", eth_dir)):
            for match in re.finditer(
                    r"\[file join \$%s ((?:\"[^\"]+\"\s*)+)\]" % var, text):
                parts = re.findall(r"\"([^\"]+)\"", match.group(1))
                candidate = base.joinpath(*parts)
                if candidate.is_file():
                    named.add(str(candidate.relative_to(REPO)))

    assert named, "the extractor found no file references -- it has gone blind"
    missing = sorted(named - hashed)
    assert not missing, (
        "fpga/shell's build names these files but static_inputs.txt does not "
        "hash them:\n  " + "\n  ".join(missing))


# ===========================================================================
# 3. static_usercode -- the overlays carry the implementation identity
# ===========================================================================
def fake_bit(path, usercode_hex):
    """A .bit whose ASCII header carries a UserID token, like Vivado's."""
    header = ("\x00\x09\x0f\xf0\x00\x00\x01a\x00\x1fshell_top;UserID=%s;"
              "COMPRESS=TRUE;Version=2024.1" % usercode_hex)
    path.write_bytes(header.encode("latin-1") + b"\x00" * 64)


def overlay_tree(tmp_path, names=("greybox", "led")):
    root = tmp_path / "overlay"
    for name in names:
        d = root / name
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps({
            "schema": 1, "static_id": "0xA8C1C535", "rm_id": "0x00000000",
            "rm_name": name,
            "clearing": {"file": "%s_clear.bin" % name, "len": 4, "crc32": "0x0"},
            "partial": {"file": "%s.bin" % name, "len": 8, "crc32": "0x0"},
            "built": "2026-09-11", "vivado": "2024.1",
        }, indent=2) + "\n")
    return root


def test_stamp_binds_every_overlay_to_the_bitstream_build(tmp_path):
    root = overlay_tree(tmp_path)
    bit = tmp_path / "config_rm_greybox.bit"
    fake_bit(bit, "875DB8BB")

    # CONTROL: before the stamp, --check FAILS. Without this the test below
    # could pass against a tool that writes nothing.
    pre = run(STAMP_TOOL, "--check", "--overlay-root", root, "--static-bit", bit)
    assert pre.returncode == 1
    assert "NOT bound" in pre.stderr

    assert run(STAMP_TOOL, "--overlay-root", root,
               "--static-bit", bit).returncode == 0
    for name in ("greybox", "led"):
        data = json.loads((root / name / "manifest.json").read_text())
        assert data["static_usercode"] == SAMPLE_USERCODE
        assert data["static_id"] == "0xA8C1C535", "the re-key was disturbed"
    assert run(STAMP_TOOL, "--check", "--overlay-root", root,
               "--static-bit", bit).returncode == 0


def test_a_wrong_usercode_is_caught_not_overwritten_silently(tmp_path):
    root = overlay_tree(tmp_path, names=("greybox",))
    bit = tmp_path / "config_rm_greybox.bit"
    fake_bit(bit, "875DB8BB")
    m = root / "greybox" / "manifest.json"
    data = json.loads(m.read_text())
    data["static_usercode"] = "0x5263642C"   # a DIFFERENT implementation run
    m.write_text(json.dumps(data, indent=2) + "\n")

    bad = run(STAMP_TOOL, "--check", "--overlay-root", root, "--static-bit", bit)
    assert bad.returncode == 1
    assert "0x5263642C" in bad.stderr


def test_an_unstamped_bitstream_is_refused(tmp_path):
    """0xFFFFFFFF is what EVERY unstamped design reads. Recording it would make
    check_image_overlay_match.py pass on exactly the builds it exists to catch."""
    root = overlay_tree(tmp_path, names=("greybox",))
    bit = tmp_path / "config_rm_greybox.bit"
    fake_bit(bit, "FFFFFFFF")
    proc = run(STAMP_TOOL, "--overlay-root", root, "--static-bit", bit)
    assert proc.returncode != 0
    assert "no USERID stamp" in proc.stdout + proc.stderr
    assert "static_usercode" not in (root / "greybox" / "manifest.json").read_text()
    # ...and it is still possible on purpose, loudly
    assert run(STAMP_TOOL, "--overlay-root", root, "--static-bit", bit,
               "--allow-unstamped").returncode == 0


def test_the_build_record_and_the_bitstream_must_agree(tmp_path):
    """Two independent derivations of one stamp. A disagreement means the .bit
    beside the record is not the one the record describes -- fatal, never
    resolved in favour of one of them."""
    root = overlay_tree(tmp_path, names=("greybox",))
    bit = tmp_path / "config_rm_greybox.bit"
    fake_bit(bit, "875DB8BB")
    stamp = tmp_path / "static_stamp.json"
    stamp.write_text(json.dumps({"usercode": "0x5263642C",
                                 "usr_access": "0x01000001",
                                 "harness_version": "1.0.0"}))
    proc = run(STAMP_TOOL, "--overlay-root", root, "--static-bit", bit,
               "--stamp-json", stamp)
    assert proc.returncode != 0
    assert "DISAGREE" in proc.stdout + proc.stderr

    stamp.write_text(json.dumps({"usercode": "0x875DB8BB",
                                 "usr_access": "0x01000001",
                                 "harness_version": "1.0.0"}))
    good = run(STAMP_TOOL, "--overlay-root", root, "--static-bit", bit,
               "--stamp-json", stamp)
    assert good.returncode == 0, good.stdout + good.stderr
    assert "agrees with static_stamp.json" in good.stdout


def test_stamping_refuses_an_empty_overlay_root(tmp_path):
    """It stamps what `make overlays` wrote; it never creates a manifest."""
    empty = tmp_path / "overlay"
    empty.mkdir()
    bit = tmp_path / "b.bit"
    fake_bit(bit, "875DB8BB")
    proc = run(STAMP_TOOL, "--overlay-root", empty, "--static-bit", bit)
    assert proc.returncode != 0
    assert "no overlay/*/manifest.json" in proc.stdout + proc.stderr


@pytest.mark.parametrize("static_id,usercode", PRESERVED_BITS)
def test_a_preserved_boot_image_reports_its_own_usercode(static_id, usercode):
    """REAL evidence, not a fixture: a preserved bitstream's header carries the
    USERID its overlays are bound to.

    The first row is the fielded shell, so this is the gate on the value
    `fpga/dfx/overlay/*/manifest.json` must carry. The rest are superseded
    archives and prove only that the reader works -- which is worth keeping,
    because it is the half that can be checked without a board.

    Skipped per row: the bitstreams are gitignored, so a fresh clone (and CI)
    has none of them until `fielded/<id>/fetch_fielded.sh` has run.
    """
    bit = REPO / "fielded" / static_id / "config_rm_greybox.bit"
    if not bit.is_file():
        pytest.skip("fielded/%s/config_rm_greybox.bit is gitignored "
                    "and has not been fetched" % static_id)
    sys.path.insert(0, str(DFX))
    from gen_manifest import usercode_from_bitstream
    assert usercode_from_bitstream(bit) == usercode


def test_the_mint_plan_stamps_the_overlays(tmp_path):
    """Stage 5 must run the stamper, or the field stays absent on every real
    mint and the guard stays data-free."""
    proc = subprocess.run(
        ["make", "-n", "-C", str(DFX), "mint",
         "BUILD=%s" % (tmp_path / "b"), "SHELL_PROJ=%s" % (tmp_path / "s"),
         "OVERLAY_ROOT=%s" % (tmp_path / "o"), "FW_WS=%s" % (tmp_path / "w"),
         "RM_SET=greybox", "TOUCH=0", "SHELL_TOUCH=0", "MINT_HUB="],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # NOT a substring search: the DRYRUN branch ECHOES the same command, and
    # `make -n` prints both arms. A first cut of this test passed against a
    # stage 5 whose real invocation had been deleted, because the echo survived.
    # So: find an EXECUTED line -- one that is not itself an echo.
    executed = [ln.strip().lstrip("@") for ln in proc.stdout.splitlines()
                if "overlay-usercode" in ln
                and not ln.strip().lstrip("@").startswith("echo ")]
    assert executed, (
        "stage 5 does not RUN the usercode stamper (only echoes it):\n%s"
        % proc.stdout)
    assert any("static_canon" in ln for ln in proc.stdout.splitlines())


# ===========================================================================
# static_stamp.tcl -- testable under plain tclsh, which is why it is not inline
# ===========================================================================
@pytest.mark.skipif(shutil.which("tclsh") is None, reason="tclsh not installed")
def test_static_stamp_tcl_writes_valid_json(tmp_path):
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    out_a.mkdir()
    out_b.mkdir()
    script = tmp_path / "drive.tcl"
    script.write_text(
        'source "%s"\n' % STAMP_TCL
        + 'write_static_stamp "%s" "config_rm_greybox.bit" "0xA8C1C535" '
          '"1.0.0" "875DB8BB" "0x01000001"\n' % out_a
        + 'write_static_stamp "%s" "x.bit" "0xDEADBEEF" "" "" ""\n' % out_b)
    proc = subprocess.run(["tclsh", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    stamped = json.loads((out_a / "static_stamp.json").read_text())
    assert stamped["usercode"] == "0x875DB8BB"     # normalised to 0x + upper
    assert stamped["usr_access"] == "0x01000001"
    assert stamped["stamped"] is True

    # DFX_NO_VERSION_STAMP: nulls, never a plausible-looking zero. A 0x00000000
    # here would be indistinguishable from a real stamp of zero.
    bare = json.loads((out_b / "static_stamp.json").read_text())
    assert bare["usercode"] is None
    assert bare["usr_access"] is None
    assert bare["stamped"] is False


def test_build_dfx_writes_the_stamp_beside_the_boot_image():
    """The writer is only useful if the flow calls it -- and only for the
    REFERENCE RM, since a partial can carry neither config register."""
    text = BUILD_DFX_TCL.read_text()
    assert "source $repo_root/fpga/dfx/tools/static_stamp.tcl" in text
    assert "write_static_stamp" in text
    ref_branch = text.split("if { $rm_key eq $reference_rm }", 1)[1]
    # the non-reference arm opens with its own comment; slice there rather than
    # on the first "} else {", which belongs to the USERID guard inside
    ref_branch, _, other_branch = ref_branch.partition("# Non-reference RM:")
    assert other_branch, "build_dfx.tcl's non-reference arm is no longer labelled"
    assert "write_static_stamp" in ref_branch, \
        "the stamp is written outside the reference-RM branch"
    assert "write_static_stamp" not in other_branch, \
        "a PARTIAL cannot carry USERID or USR_ACCESS -- it must not claim to"


# ===========================================================================
# the mint record carries both new identities
# ===========================================================================
def mint_build_dir(tmp_path, name="prod"):
    prod = tmp_path / name
    prod.mkdir(parents=True)
    (prod / "static_id.txt").write_text("0x5F3A19C2\n")
    (prod / "overlay_inputs.txt").write_text(
        "# rm_key rm_name rm_id partial clearing\n"
        "rm_greybox greybox 0x00000000 config_greybox_partial.bin "
        "config_greybox_partial_clear.bin\n")
    return prod


def test_record_carries_the_canonical_hash_and_the_usercode(tmp_path):
    prod = mint_build_dir(tmp_path)
    root, decl = tiny_repo(tmp_path, BASE_FILES, BASE_DECL)
    canon_json = tmp_path / "canon.json"
    assert canon("compute", "--repo", root, "--decl", decl, "--part", "p",
                 "--vivado", "2024.1", "-o", canon_json).returncode == 0

    bit = tmp_path / "config_rm_greybox.bit"
    fake_bit(bit, "875DB8BB")
    stamp = tmp_path / "static_stamp.json"
    stamp.write_text(json.dumps({"usercode": "0x875DB8BB",
                                 "usr_access": "0x01000001",
                                 "harness_version": "1.0.0"}))

    out = tmp_path / "mint.json"
    proc = run(MINT_RECORD, "record", "--build-dir", prod, "--repo", REPO,
               "--static-canon", canon_json, "--static-stamp", stamp,
               "--static-bit", bit, "-o", out)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    rec = json.loads(out.read_text())
    assert rec["schema_version"] == "1.1"
    assert rec["static_canon"]["value"]["digest"] == \
        json.loads(canon_json.read_text())["digest"]
    assert rec["static_usercode"]["value"]["usercode"] == SAMPLE_USERCODE
    assert rec["static_usercode"]["value"]["usr_access"]["value"] == "0x01000001"
    assert run(MINT_RECORD, "verify", out).returncode == 0


def test_two_records_of_the_same_build_carry_the_same_hash(tmp_path):
    """THE CONTROL THE BRIEF ASKS FOR: two mint records made at different
    moments, from the same inputs, agree on static_canon -- while their
    generated_at (and, on a real mint, their static_id) differ."""
    prod = mint_build_dir(tmp_path)
    root, decl = tiny_repo(tmp_path, BASE_FILES, BASE_DECL)
    canon_json = tmp_path / "canon.json"
    assert canon("compute", "--repo", root, "--decl", decl, "--part", "p",
                 "--vivado", "2024.1", "-o", canon_json).returncode == 0

    first = tmp_path / "mint_a.json"
    assert run(MINT_RECORD, "record", "--build-dir", prod, "--repo", REPO,
               "--static-canon", canon_json, "-o", first).returncode == 0
    rec_a = json.loads(first.read_text())

    # a SECOND record of the same build, with a different wall clock and a
    # different (simulated) static_id -- exactly what a no-op rebuild produces
    (prod / "static_id.txt").write_text("0xDEADBEEF\n")
    rec_a["generated_at"] = "2000-01-01T00:00:00Z"
    second = tmp_path / "mint_b.json"
    assert run(MINT_RECORD, "record", "--build-dir", prod, "--repo", REPO,
               "--static-canon", canon_json, "-o", second).returncode == 0
    rec_b = json.loads(second.read_text())

    assert rec_a["static_id"] != rec_b["static_id"], "the control did not vary"
    assert rec_a["static_canon"]["value"]["digest"] == \
        rec_b["static_canon"]["value"]["digest"], \
        "two records of the same sources disagree on the content hash"


def test_record_says_why_when_no_canon_was_taken(tmp_path):
    """The one rule: null WITH the reason, never silently absent."""
    prod = mint_build_dir(tmp_path)
    out = tmp_path / "mint.json"
    assert run(MINT_RECORD, "record", "--build-dir", prod, "--repo", REPO,
               "-o", out).returncode == 0
    rec = json.loads(out.read_text())
    assert rec["static_canon"]["value"] is None
    assert "static_id" in rec["static_canon"]["reason"]
    assert rec["static_usercode"]["value"] is None
    assert rec["static_usercode"]["reason"]
    assert run(MINT_RECORD, "verify", out).returncode == 0


def test_verify_rejects_a_digest_that_is_not_a_content_hash(tmp_path):
    """Mutation control on the new verify rules."""
    prod = mint_build_dir(tmp_path)
    root, decl = tiny_repo(tmp_path, BASE_FILES, BASE_DECL)
    canon_json = tmp_path / "canon.json"
    canon("compute", "--repo", root, "--decl", decl, "--part", "p",
          "--vivado", "2024.1", "-o", canon_json)
    out = tmp_path / "mint.json"
    run(MINT_RECORD, "record", "--build-dir", prod, "--repo", REPO,
        "--static-canon", canon_json, "-o", out)

    rec = json.loads(out.read_text())
    rec["static_canon"]["value"]["digest"] = "0xA8C1C535"   # a CRC, not a hash
    out.write_text(json.dumps(rec, indent=2))
    bad = run(MINT_RECORD, "verify", out)
    assert bad.returncode == 1
    assert "not a 64-hex SHA-256" in bad.stderr


def test_verify_rejects_a_linked_dcp_that_does_not_declare_itself_unhashed(tmp_path):
    prod = mint_build_dir(tmp_path)
    root, decl = tiny_repo(tmp_path, BASE_FILES, BASE_DECL)
    dcp = tmp_path / "s.dcp"
    dcp.write_bytes(b"PK\x03\x04")
    canon_json = tmp_path / "canon.json"
    canon("compute", "--repo", root, "--decl", decl, "--part", "p",
          "--vivado", "2024.1", "--linked-dcp", dcp, "-o", canon_json)
    out = tmp_path / "mint.json"
    run(MINT_RECORD, "record", "--build-dir", prod, "--repo", REPO,
        "--static-canon", canon_json, "-o", out)
    assert run(MINT_RECORD, "verify", out).returncode == 0

    rec = json.loads(out.read_text())
    rec["static_canon"]["value"]["linked_static_dcp"]["note"] = "just a file"
    out.write_text(json.dumps(rec, indent=2))
    bad = run(MINT_RECORD, "verify", out)
    assert bad.returncode == 1
    assert "NOT HASHED" in bad.stderr


def test_a_reconstruction_stays_at_schema_1_0(tmp_path):
    """Neither 1.1 field is reconstructible, and emitting permanent `unknown`s
    would churn every preserved fielded record for two fields that can never be
    anything else. `verify` holds a 1.0 record to the 1.0 key set."""
    fielded = REPO / "fielded" / "0xA8C1C535"
    if not (fielded / "MANIFEST.md5").is_file():
        pytest.skip("fielded/0xA8C1C535 is not present")
    if not (fielded / "overlay_inputs.txt").is_file():
        pytest.skip("overlay_inputs.txt is not in this tree (the public export omits it: "
                    "it records lab paths); fetch it with fetch_fielded.sh")
    out = tmp_path / "mint.json"
    assert run(MINT_RECORD, "reconstruct", "--dir", fielded, "--repo", REPO,
               "-o", out).returncode == 0
    rec = json.loads(out.read_text())
    assert rec["schema_version"] == "1.0"
    assert "static_canon" not in rec
    assert run(MINT_RECORD, "verify", out).returncode == 0


# ===========================================================================
# 4. the ver32-vs-USR_ACCESS cross-check, end to end on the parts that exist
# ===========================================================================
def test_the_firmware_codec_emits_all_three_states():
    """The codec owns the verdict, so it is the codec's source that must carry
    all three renderings. (The behavioural proof runs in firmware/test --
    test_net_proto_json.c's test_encode_version_usr_access_skew.)"""
    text = NET_PROTO_C.read_text()
    assert '\\"usr_access\\":null,\\"skew\\":null' in text, \
        "the NOT-CHECKED rendering is missing: an unreadable fabric value would " \
        "fall back to a comparison that never happened"
    assert '\\"usr_access\\":\\"%s\\",\\"skew\\":%s' in text
    assert 'strcmp(resp->ver32, resp->usr_access)' in text, \
        "the verdict is not derived from the two values it claims to compare"


def test_the_wire_contract_is_documented_where_callers_read_it():
    text = NET_PROTO_H.read_text()
    assert '"usr_access":"0x01000001","skew":false' in text
    assert '"usr_access":null,"skew":null' in text
    assert "This is not a pass." in text


def test_the_host_client_keeps_not_checked_distinct_from_agree():
    """The one mistake this field exists to prevent, gated on the host side:
    `bool(resp.get("skew", False))` would turn "no comparison" into "fine"."""
    sys.path.insert(0, str(REPO / "host" / "pyverify"))
    from pyverify.client import ShellClient, ShellProtocolError, VersionResponse

    class Stub(ShellClient):
        def __init__(self, payload):
            self._payload = payload

        def _request(self, obj):        # type: ignore[override]
            return self._payload

    base = {"ok": True, "harness": "1.0.0", "ver32": "0x01000001",
            "sha": "5c09de10", "dirty": 0, "lmb_kb": 1024, "features": []}

    agree = Stub({**base, "usr_access": "0x01000001", "skew": False}).version()
    assert agree.skew is False and agree.skew_verdict == "ok"

    skewed = Stub({**base, "usr_access": "0x01000000", "skew": True}).version()
    assert skewed.skew is True and skewed.skew_verdict == "SKEW"
    assert skewed.ok, "a skew must not be reported by failing the verb"

    unchecked = Stub({**base, "usr_access": None, "skew": None}).version()
    assert unchecked.skew is None and unchecked.skew_verdict == "unchecked"
    assert unchecked.usr_access == ""

    # a shell too old to know the keys at all reads the same way
    old = Stub(dict(base)).version()
    assert old.skew is None and old.skew_verdict == "unchecked"

    # and a verdict with nothing behind it is refused outright
    with pytest.raises(ShellProtocolError):
        Stub({**base, "skew": False}).version()

    assert VersionResponse(ok=True).skew_verdict == "unchecked"
