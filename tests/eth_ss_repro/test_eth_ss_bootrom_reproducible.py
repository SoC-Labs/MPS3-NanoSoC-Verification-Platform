"""tests/eth_ss_repro — the ethernet-subsystem bootloader/bootrom path must give
the same bytes from the same source, whichever entry point built it.

A joint work commissioned on behalf of SoC Labs, under Arm Academic Access
license.

WHAT THIS GUARDS
----------------
The project record said the eth_ss ("mainfix") image could not be rebuilt: a
clean rebuild produced a DEAD Cortex-M0 while the fielded image worked, and the
difference was localised to `bootloader.hex` "varying build to build" with the
addresses shifting by tens of bytes.

It does not vary build to build. It varies ENTRY POINT to entry point, and it is
not a race, a hash seed, or a timestamp -- it is one make variable:

    $ETH_SS_HOME/fpga/Makefile:230-232
        firmware: soc_model
            $(MAKE) -C $(ETH_SS_HOME) firmware TARGET=arm-none-eabi CPU=$(CPU) \
                GNU_CC_EXTRA_FLAGS='$(FW_BOARD_DEF)'

`GNU_CC_EXTRA_FLAGS` is passed on the make COMMAND LINE. A command-line variable
beats a `:=` assignment in every sub-makefile at every level, so the FPGA flow
does not ADD the board defines -- it REPLACES each firmware target's own flags
with them:

    firmware/bootloader/makefile:28   -Os -flto -mthumb-interwork      -> gone
    firmware/eth_app/udp_echo/makefile:15
        -flto -ffunction-sections -fdata-sections -Wl,-Map=...          -> gone

`make -C fpga build_design` therefore compiles a DIFFERENT bootloader from the
one `make bootloader` compiles, out of byte-identical sources, silently. That is
the whole of the "non-reproducible build".

The fix belongs upstream (see fpga/rp/local_overrides/eth_ss/patches/); until it
lands, fpga/rp/local_overrides/eth_ss/build_eth_ss_bootrom.sh appends instead of
replacing, and these tests hold that line.

CONTROL
-------
    ETHSS_REPRO_CONTROL=1 python -m pytest tests/eth_ss_repro -q
makes the driver reproduce the UNPATCHED upstream behaviour;
`test_board_defines_are_appended_not_substituted` must then go RED. A gate whose
control was never seen to fail is not a gate.

SKIPS (loudly, never fails) when the ARM toolchain or ETH_SS_HOME is absent --
which is the normal CI case, so `make check-ci` stays honest about what it did
and did not run.

Copyright (C) 2026, SoC Labs (www.soclabs.org)
"""
import hashlib
import os
import pathlib
import shutil
import subprocess

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[2]
_DRIVER = _REPO / "fpga" / "rp" / "local_overrides" / "eth_ss" / "build_eth_ss_bootrom.sh"

# The board defines the FPGA flow injects for TARGET=arm_mps3
# ($ETH_SS_HOME/fpga/Makefile FW_BOARD_DEF). Their exact values do not matter to
# this test -- what matters is that injecting them does not delete the leaf
# makefile's own flags.
_BOARD_DEFS = "-DBOARD_MPS3=1 -DETH_PHY_ADVERTISE=0x0041"

# The upstream line this override deviates from, recorded so the deviation
# cannot go stale unnoticed (see test_deviation_record_matches_upstream).
_UPSTREAM_CLOBBER_LINE = "GNU_CC_EXTRA_FLAGS='$(FW_BOARD_DEF)'"

_CONTROL = os.environ.get("ETHSS_REPRO_CONTROL") == "1"


def _skip_reason():
    """Why this bench cannot run here, or None if it can."""
    home = os.environ.get("ETH_SS_HOME", "")
    if not home:
        return ("ETH_SS_HOME is not set -- the ethernet-subsystem-ahb checkout "
                "this bench builds from is not available (see tools.env.example)")
    if not pathlib.Path(home).is_dir():
        return f"ETH_SS_HOME does not exist: {home}"
    if not (pathlib.Path(home) / "firmware" / "bootloader" / "makefile").is_file():
        return f"ETH_SS_HOME is not an ethernet-subsystem-ahb checkout: {home}"
    tc = os.environ.get("TOOLCHAIN_BIN", "")
    if not shutil.which("arm-none-eabi-gcc", path=(tc + os.pathsep + os.environ["PATH"]) if tc else None):
        return ("arm-none-eabi-gcc is not on PATH -- no ARM toolchain to compile "
                "the bootloader with (set TOOLCHAIN_BIN)")
    if not _DRIVER.is_file():
        return f"driver missing: {_DRIVER}"
    return None


_SKIP = _skip_reason()

# Applied per test, NOT as a module-level pytestmark: the make-semantics tests
# at the bottom need neither the ARM toolchain nor ETH_SS_HOME, so they run
# everywhere -- including CI, where everything else here skips. A file that
# skips whole in CI proves nothing about the fix it documents.
needs_eth_ss = pytest.mark.skipif(_SKIP is not None, reason=str(_SKIP))


def _build(out_dir, board_defs="", env_overrides=None, clobber=None):
    """Run the override driver. Returns its stdout.

    clobber=None means 'follow the control switch': in control mode the driver
    reproduces the unpatched upstream clobber, so the fix-guard test fails.
    """
    if clobber is None:
        clobber = _CONTROL
    cmd = [str(_DRIVER), "--out", str(out_dir)]
    if board_defs:
        cmd += ["--board-defs", board_defs]
    if clobber:
        cmd += ["--clobber-like-upstream"]
    env = dict(os.environ)
    env.update(env_overrides or {})
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=900)
    assert proc.returncode == 0, (
        f"driver failed ({proc.returncode})\n--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}")
    return proc.stdout


def _artefacts(out_dir):
    """{relative path: sha256} for every artefact the flow bakes into the RM."""
    out = {}
    for rel in ("firmware/bootloader/out/bootloader.bin",
                "firmware/bootloader/out/bootloader.hex",
                "bootrom/eth_ss_bootrom.sv",
                "bootrom/bootrom.bintxt",
                "bootrom/eth_ss_region_bootrom.v"):
        p = pathlib.Path(out_dir) / rel
        assert p.is_file(), f"driver did not produce {rel} under {out_dir}"
        out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _effective_flags(stdout):
    for line in stdout.splitlines():
        if "effective flags =" in line:
            return line.split("effective flags =", 1)[1].split("<--")[0].strip()
    raise AssertionError(f"driver printed no 'effective flags' line:\n{stdout}")


def _declared_flags(stdout):
    for line in stdout.splitlines():
        if "declared flags  =" in line:
            return line.split("declared flags  =", 1)[1].strip()
    raise AssertionError(f"driver printed no 'declared flags' line:\n{stdout}")


@needs_eth_ss
def test_two_clean_builds_are_byte_identical(tmp_path):
    """The literal reproducibility claim: same source, two clean builds in
    different directories, different wall clock, different TZ, different
    PYTHONHASHSEED -> the same bytes, all the way to the generated bootrom."""
    a = _build(tmp_path / "a", _BOARD_DEFS,
               {"PYTHONHASHSEED": "0", "TZ": "UTC", "SOURCE_DATE_EPOCH": "1700000000"})
    b = _build(tmp_path / "b", _BOARD_DEFS,
               {"PYTHONHASHSEED": "12345", "TZ": "Pacific/Auckland",
                "SOURCE_DATE_EPOCH": "1700000000"})
    del a, b
    ha, hb = _artefacts(tmp_path / "a"), _artefacts(tmp_path / "b")
    differing = sorted(k for k in ha if ha[k] != hb[k])
    assert not differing, (
        "two clean builds of identical source produced different bytes: "
        + ", ".join(differing))


@needs_eth_ss
def test_board_defines_are_appended_not_substituted(tmp_path):
    """THE FIX, and the control.

    Injecting the board defines must not delete the flags the leaf makefile
    declared for itself. Upstream's `GNU_CC_EXTRA_FLAGS='$(FW_BOARD_DEF)'` on the
    make command line does exactly that; ETHSS_REPRO_CONTROL=1 reproduces it and
    this test must go RED.
    """
    stdout = _build(tmp_path / "defs", _BOARD_DEFS)
    declared = _declared_flags(stdout)
    effective = _effective_flags(stdout)

    assert declared, (
        "the leaf makefile declares no GNU_CC_EXTRA_FLAGS -- the probe in "
        "eth_ss_probe.mk stopped resolving it, so this test proves nothing")

    missing = [tok for tok in declared.split() if tok not in effective.split()]
    assert not missing, (
        "the board defines REPLACED the bootloader's own compiler flags instead "
        f"of being appended: {' '.join(missing)} were dropped.\n"
        f"  declared : {declared}\n"
        f"  effective: {effective}\n"
        "This is $ETH_SS_HOME/fpga/Makefile:230-232 passing GNU_CC_EXTRA_FLAGS "
        "on the make command line, where it overrides every sub-makefile's ':='. "
        "See fpga/rp/local_overrides/eth_ss/README.md.")

    for tok in _BOARD_DEFS.split():
        assert tok in effective.split(), f"board define {tok} never reached the compile"


@needs_eth_ss
def test_the_clobber_actually_changes_the_binary(tmp_path):
    """Evidence that the flag loss is not cosmetic: the clobbered build is a
    different image, so a flow that silently clobbers ships different silicon
    content from the one a developer tested."""
    _build(tmp_path / "fixed", _BOARD_DEFS, clobber=False)
    _build(tmp_path / "clobbered", _BOARD_DEFS, clobber=True)
    fixed = _artefacts(tmp_path / "fixed")
    clob = _artefacts(tmp_path / "clobbered")
    key = "firmware/bootloader/out/bootloader.bin"
    assert fixed[key] != clob[key], (
        "the clobbered build is byte-identical to the correct one -- either "
        "upstream has been fixed (retire the override and this test) or the "
        "driver's --clobber-like-upstream stopped reproducing the bug")


@needs_eth_ss
def test_bootrom_gen_is_byte_stable_for_a_fixed_hex(tmp_path):
    """bootrom_gen.py already honours SOURCE_DATE_EPOCH (bootrom_gen.py:28).
    Confirm it, so a future regression there is caught here and not on a board."""
    _build(tmp_path / "r1", _BOARD_DEFS, {"SOURCE_DATE_EPOCH": "1700000000"})
    _build(tmp_path / "r2", _BOARD_DEFS, {"SOURCE_DATE_EPOCH": "1600000000"})
    h1, h2 = _artefacts(tmp_path / "r1"), _artefacts(tmp_path / "r2")
    # The ROM CONTENT must not depend on SOURCE_DATE_EPOCH; only a header
    # comment may, and bootrom_gen omits even that unless asked.
    assert h1["bootrom/bootrom.bintxt"] == h2["bootrom/bootrom.bintxt"], (
        "the generated ROM image changed with SOURCE_DATE_EPOCH -- a wall clock "
        "is reaching the ROM contents")


@needs_eth_ss
def test_deviation_record_matches_upstream():
    """A documented deviation that no longer matches upstream is a lie.

    Fails when $ETH_SS_HOME/fpga/Makefile no longer carries the clobbering line
    this override routes around -- which is exactly when the override should be
    retired and the local copy deleted.
    """
    mk = pathlib.Path(os.environ["ETH_SS_HOME"]) / "fpga" / "Makefile"
    if not mk.is_file():
        pytest.skip(f"no fpga/Makefile in ETH_SS_HOME ({mk})")
    text = mk.read_text(errors="ignore")
    assert _UPSTREAM_CLOBBER_LINE in text, (
        f"{mk} no longer contains the line this override deviates from:\n"
        f"    {_UPSTREAM_CLOBBER_LINE}\n"
        "If the upstream fix landed (see fpga/rp/local_overrides/eth_ss/patches/"
        "0001-*.patch), delete fpga/rp/local_overrides/eth_ss/ and this bench. "
        "If the line merely moved, update the deviation record in that "
        "directory's README.md.")


# ---------------------------------------------------------------------------
# The mechanism itself, in three lines of GNU make.
#
# No ARM toolchain, no ETH_SS_HOME, no simulator -- so these two DO run in CI
# and hold the reasoning behind patches/0001 and patches/0002 in place. They are
# the reason the fix is a two-line change and not a workaround: `+=` through a
# dedicated variable is the only way a parent flow can add a flag to a target
# whose makefile assigns with `:=`.
# ---------------------------------------------------------------------------

_TEMPLATE_MK = """\
GNU_CC_EXTRA_FLAGS ?=
NANOSOC_EXTRA_CFLAGS ?=
GNU_CC_EXTRA_FLAGS += $(NANOSOC_EXTRA_CFLAGS)
show:; @printf '%s\\n' '$(GNU_CC_EXTRA_FLAGS)'
"""

_LEAF_MK = """\
GNU_CC_EXTRA_FLAGS := -Os -flto
include template.mk
"""


def _make_show(tmp_path, *extra_args):
    (tmp_path / "template.mk").write_text(_TEMPLATE_MK)
    (tmp_path / "leaf.mk").write_text(_LEAF_MK)
    proc = subprocess.run(["make", "-s", "-f", "leaf.mk", "show", *extra_args],
                          cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.mark.skipif(shutil.which("make") is None, reason="GNU make is not on PATH")
def test_a_command_line_variable_deletes_a_leaf_makefiles_own_flags(tmp_path):
    """The bug, reduced. This is what $ETH_SS_HOME/fpga/Makefile:230-232 does to
    every firmware target it builds."""
    assert _make_show(tmp_path) == "-Os -flto"
    assert _make_show(tmp_path, "GNU_CC_EXTRA_FLAGS=-DBOARD_MPS3=1") == "-DBOARD_MPS3=1", (
        "a make command-line variable no longer overrides a sub-makefile ':=' -- "
        "the premise of this whole investigation has changed")


@pytest.mark.skipif(shutil.which("make") is None, reason="GNU make is not on PATH")
def test_the_append_seam_adds_without_deleting(tmp_path):
    """The fix, reduced: patches/0002 adds NANOSOC_EXTRA_CFLAGS to testcode.mk and
    patches/0001 makes fpga/Makefile pass THAT instead."""
    got = _make_show(tmp_path, "NANOSOC_EXTRA_CFLAGS=-DBOARD_MPS3=1")
    assert got == "-Os -flto -DBOARD_MPS3=1", (
        f"the append seam did not preserve the leaf's own flags: {got!r}")
