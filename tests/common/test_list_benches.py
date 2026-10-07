"""test_list_benches.py — the bench-readiness predicate, and the tautology it
replaced.

WHAT THIS IS THE REGRESSION FOR. `tests/jtag_dap_bringup` could not elaborate
from 2026-07-30 to 2026-09-11 — the multicore tree moved its SoC-400 wrappers
into a shared tech block and six paths in the bench's flist stopped resolving —
and `list_benches.py` reported it READY on every one of those days (`8b9a2fe`).
It had to: that bench's entry gated on `tb_top.sv` EXISTING inside the bench's
own directory, a file committed next to the entry itself. The predicate could
not return False, so it never did.

`test_old_predicate_could_not_have_failed` states that in one line. It is the
control: it asserts the OLD answer is True even against an external tree that is
demonstrably absent, so "READY" carried no information about that bench at all.

The rest assert the new predicate BOTH WAYS. A gate that cannot fail and a gate
that cannot pass are the same defect wearing different clothes, so there is a
green case (a complete external tree) and a red case, and the red case is the
exact shape of the real regression: the tech block's filelist missing.
"""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import list_benches as lb  # noqa: E402
from dut_presence import rtl_ready  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_TESTS_ROOT = os.path.dirname(_HERE)
_REPO_ROOT = os.path.dirname(_TESTS_ROOT)

#: Everything the two SoC-400 benches resolve out of the multicore checkout.
#: Written out relative to NANOSOC_MULTICORE_HOME so the fixture below builds a
#: tree of the same SHAPE as the real one rather than of the same paths.
_MULTICORE_FILES = (
    "nanosoc_arch_tech/rtl/coresight_soc400_tech/flist/coresight_soc400_swjdap.flist",
    "nanosoc_arch_tech/rtl/slcorem0_tech/flist/slcorem0_qs.flist",
    "coresight_soc400/flist/coresight_soc400_swjdp.flist",
)

#: The variables the bench Makefiles derive from NANOSOC_MULTICORE_HOME with
#: `?=`. An outer `make -C tests` exports them, and a derivation test that
#: silently used the outer value would prove nothing, so they are cleared.
_DERIVED = (
    "SOCLABS_CORESIGHT_SOC400_TECH_DIR",
    "SOCLABS_SLCOREM0_TECH_DIR",
)


#: The vendor IP library both SoC-400 benches also require, through the bench
#: Makefiles' `ARM_IP_LIBRARY_PATH`. Its old default was a
#: lab mount: present on the build hosts, absent on a GitHub runner. A test
#: that leaves it to the default therefore passes on one and fails on the other
#: -- which is exactly what happened from dc1cc48 (2026-09-14) until this line.
#: The green cases set it to a directory they create; `bare_env` clears it so
#: the red cases cannot lean on the mount either.
_ARM_IP = "ARM_IP_LIBRARY_PATH"


@pytest.fixture
def bare_env(monkeypatch):
    """No external trees at all — a fresh clone, or a shell with no tools.env."""
    for name in ("NANOSOC_MULTICORE_HOME", _ARM_IP) + _DERIVED:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _arm_ip_library(env, root):
    """Stand in for the lab's vendor IP mount: an existing directory, nothing
    more, because `external_gap` requires only that the path exists."""
    path = os.path.join(root, "arm_ip_library")
    os.makedirs(path, exist_ok=True)
    env.setenv(_ARM_IP, path)
    return path


def _multicore_tree(root, omit=()):
    """Build a checkout-shaped tree; `omit` leaves those relative files out."""
    for rel in _MULTICORE_FILES:
        if rel in omit:
            continue
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("// stand-in filelist for test_list_benches.py\n")
    return root


# ---------------------------------------------------------------------------
# The control: what the old predicate was worth
# ---------------------------------------------------------------------------

def test_old_predicate_could_not_have_failed(bare_env):
    """`tb_top.sv exists` is True even with the external tree gone."""
    bench = os.path.join(_TESTS_ROOT, "jtag_dap_bringup")
    # The old entry, verbatim: (bench's own dir, ["tb_top.sv"]).
    assert rtl_ready(bench, ["tb_top.sv"]) is True
    # ... and it stays True when the thing the bench actually compiles is not
    # there, which is the whole point. The new predicate disagrees.
    assert lb.skip_reason("jtag_dap_bringup") is not None


# ---------------------------------------------------------------------------
# The new predicate, red and green
# ---------------------------------------------------------------------------

def test_unset_external_tree_skips_and_names_the_variable(bare_env):
    why = lb.skip_reason("jtag_dap_bringup")
    assert why is not None
    assert "NANOSOC_MULTICORE_HOME" in why
    # The reason has to be actionable: an operator must be told where to set it.
    assert "tools.env" in why
    assert lb.is_ready("jtag_dap_bringup") is False


def test_complete_external_tree_is_ready(bare_env, tmp_path):
    root = _multicore_tree(str(tmp_path / "multicore"))
    bare_env.setenv("NANOSOC_MULTICORE_HOME", root)
    _arm_ip_library(bare_env, str(tmp_path))
    assert lb.skip_reason("jtag_dap_bringup") is None
    assert lb.is_ready("jtag_dap_bringup") is True
    # Derived through the bench Makefile's own `?=` lines, not from a copy of
    # those paths kept here: clearing the derived variables above is what makes
    # this a test of the derivation.
    assert all(os.environ.get(v) is None for v in _DERIVED)


def test_the_real_2026_07_regression_reads_skip(bare_env, tmp_path):
    """The tech block absent — the exact fault that read READY for six weeks."""
    missing = "nanosoc_arch_tech/rtl/coresight_soc400_tech/flist/" \
              "coresight_soc400_swjdap.flist"
    root = _multicore_tree(str(tmp_path / "moved"), omit=(missing,))
    bare_env.setenv("NANOSOC_MULTICORE_HOME", root)

    why = lb.skip_reason("jtag_dap_bringup")
    assert why is not None
    assert "coresight_soc400_swjdap.flist" in why
    assert "does not exist" in why
    # The Arm vendor flists next to it still resolve, so this really is "six
    # stale paths", not a missing dependency — as 8b9a2fe found.
    assert os.path.exists(os.path.join(root, _MULTICORE_FILES[2]))


def test_jtag_chain_is_gated_the_same_way(bare_env, tmp_path):
    """It shares the external collateral, so it must share the verdict."""
    assert lb.skip_reason("jtag_chain") is not None
    root = _multicore_tree(str(tmp_path / "multicore"))
    bare_env.setenv("NANOSOC_MULTICORE_HOME", root)
    _arm_ip_library(bare_env, str(tmp_path))
    assert lb.is_ready("jtag_chain") is True


def test_vendor_ip_library_absent_reads_skip(bare_env, tmp_path):
    """The multicore tree complete but the Arm IP mount missing -- a GitHub
    runner, or a workstation without the lab NFS. Must SKIP and say which
    variable, never READY: the bench would die in elaboration otherwise."""
    root = _multicore_tree(str(tmp_path / "multicore"))
    bare_env.setenv("NANOSOC_MULTICORE_HOME", root)
    bare_env.setenv(_ARM_IP, str(tmp_path / "no-such-mount"))
    why = lb.skip_reason("jtag_dap_bringup")
    assert why is not None
    assert "does not exist" in why
    assert _ARM_IP in why


def test_in_repo_benches_are_unaffected_by_the_environment(bare_env):
    """A bench whose DUT is in this repo must not acquire an external gate."""
    for name in ("dfx_ctl", "clkrst", "gen_checker", "dut_egress"):
        assert lb.skip_reason(name) is None, name


# ---------------------------------------------------------------------------
# The consumers: the output format, and the two lists that must agree
# ---------------------------------------------------------------------------

def test_output_format_is_what_the_makefile_parses(bare_env):
    """tests/Makefile reads `status name rtldir` and awk-matches on $2."""
    out = subprocess.run(
        [sys.executable, os.path.join(_HERE, "list_benches.py"), "--summary"],
        capture_output=True, text=True, check=True,
        env={k: v for k, v in os.environ.items()
             if k not in ("NANOSOC_MULTICORE_HOME",) + _DERIVED},
    ).stdout.splitlines()

    seen = set()
    for line in out:
        if line.startswith("SUMMARY "):
            continue
        fields = line.split()
        assert fields[0] in ("READY", "SKIP"), line
        assert fields[1] in lb.BENCHES, line
        assert os.path.isabs(fields[2]), line          # the dir, still third
        seen.add(fields[1])
    assert seen == set(lb.BENCHES)
    # A skip line carries its reason AFTER the directory, so the shell's
    # `read -r status name _rtldir` and the awk `$2` match both still work.
    skips = [l for l in out if l.startswith("SKIP ")]
    assert skips, "the bare environment must skip the external benches"
    for line in skips:
        assert " -- " in line, line


def test_bench_list_matches_the_makefile():
    """Every bench is in BOTH lists, or `make clean` silently misses one."""
    text = open(os.path.join(_TESTS_ROOT, "Makefile"), encoding="utf-8").read()
    start = text.index("BENCH_DIRS :=") + len("BENCH_DIRS :=")
    chunk = ""
    for line in text[start:].splitlines():
        chunk += " " + line.rstrip("\\")
        if not line.rstrip().endswith("\\"):
            break
    assert set(chunk.split()) == set(lb.BENCHES)


def test_every_bench_has_a_makefile_to_read_its_variables_from():
    for name in lb.BENCHES:
        mk = os.path.join(lb.BENCH_MAKEFILE_DIR[name], "Makefile")
        assert os.path.isfile(mk), name
