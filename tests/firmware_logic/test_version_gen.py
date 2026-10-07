"""tests/firmware_logic/test_version_gen.py — the cross-language gate on the
HARNESS VERSION scheme (docs/VERSIONING_PLAN.md §3.1).

HARNESS_VER32 is encoded in THREE places and they must never drift:

- **Python** — ``scripts/gen_version.py`` ``pack_ver32()``: the one generator.
  It feeds the firmware (``firmware/platform/generated/mps3_version.c``) *and*
  the bitstream (``fpga/dfx/build_dfx.tcl`` execs ``--print ver32`` to stamp
  ``BITSTREAM.CONFIG.USR_ACCESS``).
- **C macro** — ``MPS3_HARNESS_VER32()`` in ``firmware/platform/mps3_version.h``.
- **The generated C** — the strong ``mps3_harness_version()`` override.

A silent disagreement between them is the exact failure this platform keeps
being bitten by (the wrapper-vs-rm_list-vs-manifest ``rm_id`` three-way drift).
So this test does not re-implement the encoding — it RUNS the real generator,
COMPILES the real generated TU against the real committed header, and executes
it, asserting the number the firmware would report is the number the bitstream
would be stamped with.

It also pins the seam's link semantics: the generated strong definitions must
WIN over the committed weak fallbacks when both are linked (the
``mps3_shell_static_id()`` / ``greybox_blob.c`` pattern), and the weak
fallbacks alone must still link (proved separately, and faster, by
``firmware/test/test_version.c``).

Provenance is exercised on a **non-git** tree (``--repo`` pointed at a tmpdir)
so the result is deterministic: the real repo is routinely dirty with several
agents in it, which would make a "clean => flags 0" assertion flaky. The dirty
path is asserted by pointing ``--repo`` at a scratch git repo we make dirty
ourselves.
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GEN = REPO / "scripts" / "gen_version.py"
SEAM_H = REPO / "firmware" / "platform" / "mps3_version.h"
WEAK_C = REPO / "firmware" / "platform" / "mps3_version_weak.c"

#: The worked example the whole scheme is documented against.
PLAN_VERSION = "1.4.2"
PLAN_VER32_CLEAN = 0x01040200
PLAN_VER32_DIRTY = 0x01040201


def _run_gen(*args, version="1.0.0", repo=None, tmp_path=None):
    """Run gen_version.py against a synthetic VERSION file."""
    vf = tmp_path / "VERSION"
    vf.write_text(version + "\n")
    cmd = [sys.executable, str(GEN), "--version-file", str(vf)]
    if repo is not None:
        cmd += ["--repo", str(repo)]
    cmd += list(args)
    return subprocess.run(cmd, check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True)


def _print(field, version="1.0.0", repo=None, tmp_path=None):
    out = _run_gen("--print", field, version=version, repo=repo, tmp_path=tmp_path)
    return out.stdout.strip()


# --------------------------------------------------------------------------
# The committed artefacts exist and say what the flow assumes
# --------------------------------------------------------------------------

def test_repo_root_version_file_is_a_bare_semver():
    """VERSION is the single source of truth; the flow parses it strictly."""
    raw = (REPO / "VERSION").read_text().strip()
    assert re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}", raw), (
        f"VERSION holds {raw!r}; expected a bare 'major.minor.patch'"
    )
    # Each field must survive the one-byte-per-field packing.
    assert all(int(p) <= 255 for p in raw.split("."))


def test_generator_is_reachable_from_the_bitstream_flow():
    """build_dfx.tcl execs scripts/gen_version.py --print ver32. If that path
    ever moves, the shell build fails at minute 0 (by design) — but so should
    this test, and with a clearer message."""
    assert GEN.is_file(), f"{GEN} missing — fpga/dfx/build_dfx.tcl execs it"
    tcl = (REPO / "fpga" / "dfx" / "build_dfx.tcl").read_text()
    assert "gen_version.py" in tcl
    assert "BITSTREAM.CONFIG.USR_ACCESS" in tcl


# --------------------------------------------------------------------------
# The encoding
# --------------------------------------------------------------------------

def test_plan_worked_example_clean(tmp_path):
    """v1.4.2 from a tree with no git => flags 0 => 0x01040200."""
    non_git = tmp_path / "not_a_repo"
    non_git.mkdir()
    got = _print("ver32", version=PLAN_VERSION, repo=non_git, tmp_path=tmp_path)
    assert got == f"0x{PLAN_VER32_CLEAN:08X}"
    assert _print("flags", version=PLAN_VERSION, repo=non_git, tmp_path=tmp_path) == "0x00"
    assert _print("dirty", version=PLAN_VERSION, repo=non_git, tmp_path=tmp_path) == "0"
    assert _print("sha", version=PLAN_VERSION, repo=non_git, tmp_path=tmp_path) == "unknown"
    assert _print("version", version=PLAN_VERSION, repo=non_git, tmp_path=tmp_path) == PLAN_VERSION


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_dirty_tree_sets_flag_bit0(tmp_path):
    """A committed-then-modified tree => flags bit0 => 0x01040201.

    This is the flag that tells a fielded board "the image you are running was
    NOT built from a clean commit" — it must actually fire.
    """
    scratch = tmp_path / "scratch_repo"
    scratch.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def git(*a):
        subprocess.run(("git",) + a, cwd=scratch, check=True, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    git("init", "-q")
    (scratch / "f.txt").write_text("committed\n")
    git("add", "f.txt")
    git("commit", "-qm", "init")

    # Clean commit -> no dirty flag, and a real 8-hex sha.
    assert _print("dirty", version=PLAN_VERSION, repo=scratch, tmp_path=tmp_path) == "0"
    assert _print("ver32", version=PLAN_VERSION, repo=scratch,
                  tmp_path=tmp_path) == f"0x{PLAN_VER32_CLEAN:08X}"
    sha = _print("sha", version=PLAN_VERSION, repo=scratch, tmp_path=tmp_path)
    assert re.fullmatch(r"[0-9a-f]{8}", sha), f"sha={sha!r} is not 8 hex chars"

    # Now dirty it.
    (scratch / "f.txt").write_text("modified\n")
    assert _print("dirty", version=PLAN_VERSION, repo=scratch, tmp_path=tmp_path) == "1"
    assert _print("flags", version=PLAN_VERSION, repo=scratch, tmp_path=tmp_path) == "0x01"
    assert _print("ver32", version=PLAN_VERSION, repo=scratch,
                  tmp_path=tmp_path) == f"0x{PLAN_VER32_DIRTY:08X}"


def test_field_byte_placement(tmp_path):
    non_git = tmp_path / "not_a_repo"
    non_git.mkdir()
    for version, expect in (
        ("255.0.0", 0xFF000000),
        ("0.255.0", 0x00FF0000),
        ("0.0.255", 0x0000FF00),
        ("1.0.0", 0x01000000),
        ("0.0.0", 0x00000000),
    ):
        got = _print("ver32", version=version, repo=non_git, tmp_path=tmp_path)
        assert got == f"0x{expect:08X}", f"v{version} packed to {got}"


def test_malformed_version_fails_loudly(tmp_path):
    """A typo'd VERSION must NOT silently stamp a wrong number into a bitstream
    that then goes to a board. It must fail the build."""
    for bad in ("1.4", "1.4.2.3", "v1.4.2", "256.0.0", "", "1.4.x"):
        vf = tmp_path / "VERSION"
        vf.write_text(bad + "\n")
        r = subprocess.run(
            [sys.executable, str(GEN), "--version-file", str(vf), "--print", "ver32"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert r.returncode != 0, f"VERSION={bad!r} was accepted (stdout={r.stdout!r})"


# --------------------------------------------------------------------------
# The generated C: compile it and RUN it
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("cc") is None and shutil.which("gcc") is None,
                    reason="no host C compiler")
def test_generated_c_reports_the_same_ver32_as_the_generator(tmp_path):
    """The end-to-end pin: generator -> C -> executed value.

    Compiles the REAL generated TU + the REAL committed weak TU + the REAL
    committed seam header, links them together, runs the result, and asserts
    the executed mps3_harness_version() equals the ver32 the generator printed
    (== what build_dfx.tcl stamps into USR_ACCESS). Linking BOTH TUs also proves
    the strong override WINS over the weak fallback — the seam's core semantic.
    """
    cc = shutil.which("cc") or shutil.which("gcc")
    non_git = tmp_path / "not_a_repo"
    non_git.mkdir()

    outdir = tmp_path / "generated"
    _run_gen("--outdir", str(outdir), version=PLAN_VERSION, repo=non_git,
             tmp_path=tmp_path)

    gen_c = outdir / "mps3_version.c"
    gen_h = outdir / "mps3_version.h"
    assert gen_c.is_file() and gen_h.is_file()

    # The generated header includes "../mps3_version.h" (the committed seam), so
    # the generated dir must sit one level under a dir holding that seam header
    # — mirror the real firmware/platform/ layout exactly.
    platform = tmp_path / "platform"
    (platform / "generated").mkdir(parents=True)
    shutil.copy(SEAM_H, platform / "mps3_version.h")
    shutil.copy(WEAK_C, platform / "mps3_version_weak.c")
    shutil.copy(gen_c, platform / "generated" / "mps3_version.c")
    shutil.copy(gen_h, platform / "generated" / "mps3_version.h")

    main_c = tmp_path / "main.c"
    main_c.write_text(
        '#include <stdio.h>\n'
        '#include "platform/mps3_version.h"\n'
        'int main(void) {\n'
        '    printf("%08X|%s|%s|%d\\n", mps3_harness_version(),\n'
        '           mps3_harness_version_str(), mps3_harness_git_sha(),\n'
        '           mps3_harness_dirty() ? 1 : 0);\n'
        '    return 0;\n'
        '}\n')

    exe = tmp_path / "vprobe"
    subprocess.run(
        [cc, "-std=c11", "-Wall", "-Wextra", "-Werror", "-o", str(exe),
         str(main_c), str(platform / "mps3_version_weak.c"),
         str(platform / "generated" / "mps3_version.c")],
        cwd=tmp_path, check=True)

    out = subprocess.run([str(exe)], check=True, stdout=subprocess.PIPE,
                         text=True).stdout.strip()
    ver32_hex, ver_str, sha, dirty = out.split("|")

    expected = _print("ver32", version=PLAN_VERSION, repo=non_git, tmp_path=tmp_path)
    assert f"0x{ver32_hex}" == expected == f"0x{PLAN_VER32_CLEAN:08X}"
    assert ver_str == PLAN_VERSION           # strong override won over "0.0.0"
    assert sha == "unknown"                  # non-git tree
    assert dirty == "0"


@pytest.mark.skipif(shutil.which("cc") is None and shutil.which("gcc") is None,
                    reason="no host C compiler")
def test_weak_seam_links_with_no_generated_identity(tmp_path):
    """The seam's contract: firmware links with NO generated file present, and
    honestly reports 'not provisioned' (0 / 0.0.0) rather than a made-up
    version. A fresh clone is exactly this situation."""
    cc = shutil.which("cc") or shutil.which("gcc")
    platform = tmp_path / "platform"
    platform.mkdir()
    shutil.copy(SEAM_H, platform / "mps3_version.h")
    shutil.copy(WEAK_C, platform / "mps3_version_weak.c")

    main_c = tmp_path / "main.c"
    main_c.write_text(
        '#include <stdio.h>\n'
        '#include "platform/mps3_version.h"\n'
        'int main(void) {\n'
        '    printf("%08X|%s\\n", mps3_harness_version(), mps3_harness_version_str());\n'
        '    return 0;\n'
        '}\n')

    exe = tmp_path / "vweak"
    subprocess.run(
        [cc, "-std=c11", "-Wall", "-Wextra", "-Werror", "-o", str(exe),
         str(main_c), str(platform / "mps3_version_weak.c")],
        cwd=tmp_path, check=True)

    out = subprocess.run([str(exe)], check=True, stdout=subprocess.PIPE,
                         text=True).stdout.strip()
    assert out == "00000000|0.0.0"
