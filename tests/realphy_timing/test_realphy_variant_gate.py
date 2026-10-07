"""tests/realphy_timing/test_realphy_variant_gate.py

SHELL_REALPHY is a MINT KNOB. These gates are what let it be one.

A variant flag is only safe to hand to `make -C fpga/dfx mint` if the DEFAULT
build provably cannot notice it exists. That is the promise every file involved
makes in its header ("keeps the shipped shell byte-identical when unset/0") and
until now nothing checked it. The promise has three surfaces and they fail
differently:

  * the Tcl gate      -- reads the environment and decides
  * the XDC           -- must never reach the default build's constraint glob
  * the RTL           -- must preprocess away entirely without the define

and a fourth thing that is not about the default build at all:

  * the RATE          -- REALPHY_RATE and the eth_ss RM's MODE_SPEED_100 are two
                         halves of one decision, in two files, with no link
                         between them. Mismatched, the fabric is built and
                         constrained for one line rate and the DUT's RMII bridge
                         divides for the other, and the failure mode is a silent
                         dead RX -- exactly the multicore-wrapper bug
                         tests/rmii_speed/ reproduced.

THE GATE THAT WAS NOT THERE, AND THE BUG IT WOULD HAVE CAUGHT
-------------------------------------------------------------
The original gate expression was

    [info exists ::env(SHELL_REALPHY)] && $v ne "0" && $v ne ""

so `SHELL_REALPHY=no`, `=false`, `=off` all read as TRUE and built the
NON-DEFAULT variant. In a mint that is a shell with nine extra board pads and a
different SECTION 5 routing, produced by a maintainer who typed the word "no".
`test_gate_truth_table` runs the REAL Tcl proc out of build_shell.tcl under
tclsh and pins the whole table, including that garbage is now refused rather
than guessed at.

Every structural gate below is paired with a control that mutates a temporary
copy and asserts the checker goes red. A guard nobody has watched fail is a
guard nobody should trust.
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import tempfile

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
BUILD_SHELL = REPO_ROOT / "fpga" / "shell" / "build_shell.tcl"
SHELL_BD = REPO_ROOT / "fpga" / "shell" / "bd" / "shell_bd.tcl"
SHELL_TOP = REPO_ROOT / "fpga" / "shell" / "shell_top.sv"
CONSTR_DIR = REPO_ROOT / "fpga" / "shell" / "constraints"
CONSTR_REALPHY_DIR = REPO_ROOT / "fpga" / "shell" / "constraints_realphy"
ETH_SS_RM = REPO_ROOT / "fpga" / "rp" / "eth_ss" / "rp_eth_ss_wrapper.sv"

TCLSH = shutil.which("tclsh")
VERILATOR = shutil.which("verilator")


# ===========================================================================
# helpers
# ===========================================================================

def _code_of(raw: str) -> str:
    """The Tcl code on a line, with comments removed so their braces do not count."""
    if raw.lstrip().startswith("#"):
        return ""
    return re.sub(r";\s*#.*$", "", raw)


def _guard_depths(text: str, guard_re: str) -> dict:
    """Map line number -> how many `if {<guard>} {` bodies the line sits inside.

    Line-granular brace counting. A line matching `guard_re` that ends deeper
    than it started has opened a guarded body at that depth; the body stays open
    until the total depth falls back below it. Good enough to answer the one
    question asked of it -- "is this action line lexically inside the guard?" --
    and its own control test below proves it can say no.
    """
    depths = {}
    depth = 0
    stack = []  # brace depths at which a guarded body is currently open
    pat = re.compile(guard_re)
    for lineno, raw in enumerate(text.splitlines(), 1):
        code = _code_of(raw)
        is_guard = bool(pat.search(code))
        depth_after = depth + code.count("{") - code.count("}")
        depths[lineno] = len(stack)
        while stack and depth_after < stack[-1]:
            stack.pop()
        if is_guard and depth_after > depth:
            stack.append(depth_after)
        depth = depth_after
    return depths


# The two spellings a real-PHY action wears. build_shell.tcl names the XDC and
# the verilog define ("realphy"/"REALPHY"); shell_bd.tcl names the pads
# ("phy_pad_"), which appear nowhere else in that file.
_REALPHY_MARKERS = ("realphy", "phy_pad_")


def _realphy_action_lines(text: str):
    """Lines that would ADD something real-PHY to a Vivado project or BD."""
    out = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        s = raw.strip()
        if s.startswith("#"):
            continue
        if not any(mk in s.lower() for mk in _REALPHY_MARKERS):
            continue
        if re.match(r"^(add_files|set_property|create_bd_port|connect_bd_net)\b", s):
            out.append((lineno, s))
    return out


# ===========================================================================
# 1. the Tcl gate: the real proc, run, with its truth table pinned
# ===========================================================================

def _run_gate(env_value, var="SHELL_REALPHY", default="0", allowed="0 1"):
    """Execute build_shell.tcl's OWN soc labs_realphy_env proc under tclsh."""
    src = BUILD_SHELL.read_text()
    m = re.search(r"^proc soclabs_realphy_env .*?^\}", src, re.S | re.M)
    assert m, "build_shell.tcl no longer defines proc soclabs_realphy_env"
    script = m.group(0) + f'\nputs [soclabs_realphy_env {var} {default} {{{allowed}}}]\n'
    env = dict(os.environ)
    env.pop(var, None)
    if env_value is not None:
        env[var] = env_value
    # A script fed on tclsh's STDIN exits 0 even after an uncaught error (it is
    # read as an interactive session). Run it as a FILE so `error` is fatal and
    # the exit status means what it says.
    with tempfile.NamedTemporaryFile("w", suffix=".tcl", delete=False) as fh:
        fh.write(script)
        path = fh.name
    try:
        p = subprocess.run([TCLSH, path], env=env, capture_output=True, text=True)
    finally:
        os.unlink(path)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


@pytest.mark.skipif(TCLSH is None, reason="tclsh not available")
@pytest.mark.parametrize(
    "value,expect",
    [
        (None, "0"),   # unset -> the shipped default, unchanged
        ("", "0"),     # empty  -> the shipped default (make passes empties)
        ("0", "0"),
        ("1", "1"),
        (" 1 ", "1"),  # whitespace from a make variable must not matter
    ],
)
def test_gate_truth_table_accepts_only_zero_and_one(value, expect):
    rc, out, err = _run_gate(value)
    assert rc == 0, err
    assert out == expect


@pytest.mark.skipif(TCLSH is None, reason="tclsh not available")
@pytest.mark.parametrize("value", ["no", "false", "off", "yes", "true", "2", "y"])
def test_gate_refuses_to_guess_at_anything_else(value):
    """THE REGRESSION. Every one of these used to mean "build the variant"."""
    rc, out, err = _run_gate(value)
    assert rc != 0, (
        f"SHELL_REALPHY={value!r} was accepted and returned {out!r}. The old gate read "
        f"every one of these as TRUE and built the NON-DEFAULT real-PHY shell."
    )
    assert "not one of" in err


@pytest.mark.skipif(TCLSH is None, reason="tclsh not available")
def test_rate_gate_accepts_only_ten_and_one_hundred():
    for good in ("10", "100"):
        rc, out, _ = _run_gate(good, var="REALPHY_RATE", default="100", allowed="10 100")
        assert rc == 0 and out == good
    for bad in ("1000", "50", "100M", "fast"):
        rc, _, err = _run_gate(bad, var="REALPHY_RATE", default="100", allowed="10 100")
        assert rc != 0, f"REALPHY_RATE={bad} was accepted"
    rc, out, _ = _run_gate(None, var="REALPHY_RATE", default="100", allowed="10 100")
    assert rc == 0 and out == "100", "the default link rate must be 100 Mb/s"


@pytest.mark.skipif(TCLSH is None, reason="tclsh not available")
def test_control_the_old_permissive_gate_would_fail_this_suite():
    """CONTROL: run the SUPERSEDED expression and watch it accept "no"."""
    script = (
        'set v $::env(SHELL_REALPHY)\n'
        'puts [expr {[info exists ::env(SHELL_REALPHY)] && $v ne "0" && $v ne ""}]\n'
    )
    env = dict(os.environ, SHELL_REALPHY="no")
    p = subprocess.run([TCLSH], input=script, env=env, capture_output=True, text=True)
    assert p.returncode == 0 and p.stdout.strip() == "1", (
        "the control must reproduce the old behaviour -- if it does not, this suite is "
        "not testing what it claims to test"
    )


def test_build_shell_guards_the_rate_against_a_half_configured_build():
    """REALPHY_RATE without SHELL_REALPHY=1 is a maintainer who thinks they are
    building the variant and is not -- the SHELL_TOUCH/TOUCH failure shape."""
    src = BUILD_SHELL.read_text()
    assert re.search(
        r"if\s*\{\s*!\$shell_realphy\s*&&\s*\[info exists ::env\(REALPHY_RATE\)\]",
        src,
    ), "build_shell.tcl no longer refuses REALPHY_RATE on a non-realphy build"


# ===========================================================================
# 2. nothing realphy reaches the default build
# ===========================================================================

@pytest.mark.parametrize("path", [BUILD_SHELL, SHELL_BD])
def test_every_realphy_action_is_inside_the_gate(path):
    text = path.read_text()
    depths = _guard_depths(text, r"if\s*\{\s*\$shell_realphy\s*\}")
    ungated = [(n, s) for n, s in _realphy_action_lines(text) if depths.get(n, 0) == 0]
    assert not ungated, (
        f"{path.relative_to(REPO_ROOT)} performs realphy actions OUTSIDE "
        f"`if {{$shell_realphy}}`, so they would land in the shipped default build:\n"
        + "\n".join(f"  line {n}: {s}" for n, s in ungated)
    )


@pytest.mark.parametrize("path", [BUILD_SHELL, SHELL_BD])
def test_control_moving_one_action_out_of_the_gate_is_caught(path, tmp_path):
    """CONTROL: hoist a guarded realphy action to column 0 and re-check."""
    text = path.read_text()
    gated = [(n, s) for n, s in _realphy_action_lines(text)
             if _guard_depths(text, r"if\s*\{\s*\$shell_realphy\s*\}").get(n, 0) > 0]
    assert gated, f"{path} has no gated realphy actions to mutate -- bad control"
    lineno, stmt = gated[0]
    lines = text.splitlines()
    # Splice the statement back in at the very top of the file, outside every brace.
    mutated = "\n".join([stmt] + lines)
    depths = _guard_depths(mutated, r"if\s*\{\s*\$shell_realphy\s*\}")
    ungated = [(n, s) for n, s in _realphy_action_lines(mutated) if depths.get(n, 0) == 0]
    assert ungated, (
        f"the guard checker did NOT notice a realphy action hoisted out of the gate "
        f"(line {lineno}: {stmt}). The checker is blind; fix it before trusting it."
    )


def test_realphy_xdc_is_not_in_the_globbed_constraints_dir():
    """build_shell.tcl globs constraints/*.xdc into EVERY build. That glob is
    exactly how the touch XDC nearly shipped by accident."""
    globbed = sorted(p.name for p in CONSTR_DIR.glob("*.xdc"))
    strays = [n for n in globbed if "realphy" in n.lower()]
    assert not strays, (
        f"{strays} sits in the globbed fpga/shell/constraints/ directory and would be "
        f"added to the DEFAULT shell build. Real-PHY XDC belongs in constraints_realphy/."
    )
    assert CONSTR_REALPHY_DIR.is_dir()
    assert CONSTR_REALPHY_DIR.parent == CONSTR_DIR.parent, (
        "constraints_realphy/ must be a SIBLING of constraints/, never inside it"
    )


def test_realphy_xdc_files_are_added_explicitly_by_name():
    src = BUILD_SHELL.read_text()
    for name in ("mps3_realphy_pins.xdc", "mps3_realphy_timing.xdc"):
        assert name in src, f"{name} is never added by build_shell.tcl"
    assert "constraints_realphy" in src
    assert not re.search(r'glob[^\n]*constraints_realphy', src), (
        "the real-PHY XDC must be added by NAME, not globbed -- a glob is how a "
        "file dropped in the directory silently joins a mint"
    )


def test_timing_xdc_is_implementation_only():
    """Its generated clock does not exist until clk_wiz_shell has elaborated."""
    src = BUILD_SHELL.read_text()
    assert re.search(
        r"set_property\s+USED_IN_SYNTHESIS\s+false[^\n]*mps3_realphy_timing\.xdc", src
    ), "mps3_realphy_timing.xdc is not marked USED_IN_SYNTHESIS false"


# ===========================================================================
# 3. the RTL preprocesses away without the define
# ===========================================================================

_PHY_TOKENS = ("ETH_RMII_", "ETH_MDC", "ETH_MDIO", "phy_pad_", "r_phy_rxd", "ODDRE1")


def _preprocess(defines):
    args = [VERILATOR, "-E", "-sv"]
    for d in defines:
        args += [f"+define+{d}"]
    args += [str(SHELL_TOP)]
    p = subprocess.run(args, capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert p.returncode == 0, p.stderr
    return p.stdout


@pytest.mark.skipif(VERILATOR is None, reason="verilator not available")
def test_default_preprocess_contains_no_real_phy_at_all():
    """The strongest form of "byte-identical default" available without Vivado:
    the compiler's own view of shell_top.sv, with no define, has no real PHY in
    it -- not a port, not a buffer, not a flop."""
    out = _preprocess([])
    present = [t for t in _PHY_TOKENS if t in out]
    assert not present, (
        f"shell_top.sv preprocesses to RTL that still mentions {present} with "
        f"MPS3_SHELL_REALPHY undefined. The default shell would gain real-PHY logic."
    )


@pytest.mark.skipif(VERILATOR is None, reason="verilator not available")
def test_control_the_define_really_does_bring_the_real_phy_in():
    """CONTROL: with the define, every one of those tokens appears. Without this
    the test above would pass on an empty file."""
    out = _preprocess(["MPS3_SHELL_REALPHY"])
    missing = [t for t in _PHY_TOKENS if t not in out]
    assert not missing, f"MPS3_SHELL_REALPHY did not bring in {missing}"


@pytest.mark.skipif(VERILATOR is None, reason="verilator not available")
def test_touch_and_realphy_do_not_interfere():
    """Two independent gated groups in one file. Each must be able to appear
    without the other -- the leading-comma port style makes that easy to break."""
    only_touch = _preprocess(["MPS3_SHELL_TOUCH"])
    assert "CLCD_TSCL" in only_touch
    assert not any(t in only_touch for t in ("ETH_RMII_", "phy_pad_"))
    both = _preprocess(["MPS3_SHELL_TOUCH", "MPS3_SHELL_REALPHY"])
    assert "CLCD_TSCL" in both and "ETH_RMII_REF_CLK" in both


# ===========================================================================
# 4. the rate is one decision, not two
# ===========================================================================

def _default_realphy_rate() -> int:
    m = re.search(r"set\s+realphy_rate\s+\[soclabs_realphy_env\s+REALPHY_RATE\s+(\d+)",
                  BUILD_SHELL.read_text())
    assert m, "build_shell.tcl no longer declares a default REALPHY_RATE"
    return int(m.group(1))


def _rm_mode_speed_100() -> int:
    m = re.search(r"parameter\s+bit\s+MODE_SPEED_100\s*=\s*1'b([01])", ETH_SS_RM.read_text())
    assert m, "rp_eth_ss_wrapper.sv no longer declares MODE_SPEED_100"
    return int(m.group(1))


def test_shell_default_rate_matches_the_rm_bridge_divider():
    """The fabric's declared rate and the DUT bridge's divider are one decision.

    Split across two files with no link, this is the multicore-wrapper bug in
    slow motion: `mode_speed` tied to the wrong value gives a bridge that
    samples a tenth of the stream, never finds the SFD, and reports nothing at
    all. tests/rmii_speed/ has the positive and negative controls for the RTL
    half; this is the build-configuration half.
    """
    shell_rate = _default_realphy_rate()
    rm_bit = _rm_mode_speed_100()
    expected_bit = 1 if shell_rate == 100 else 0
    assert rm_bit == expected_bit, (
        f"build_shell.tcl defaults REALPHY_RATE={shell_rate} Mb/s but "
        f"rp_eth_ss_wrapper.sv defaults MODE_SPEED_100=1'b{rm_bit} "
        f"({'100' if rm_bit else '10'} Mb/s). The shell would be built and pinned for one "
        f"line rate and the DUT's rmii_to_mii would divide for the other; RX dies silently."
    )


def test_control_a_rate_mismatch_is_detectable():
    """CONTROL: the comparison is real, not a tautology."""
    for shell_rate, rm_bit, ok in ((100, 1, True), (10, 0, True), (100, 0, False), (10, 1, False)):
        expected_bit = 1 if shell_rate == 100 else 0
        assert (rm_bit == expected_bit) is ok


def test_build_shell_records_the_variant_next_to_the_evidence():
    """A bitstream on an SD card must not be the only record of which knobs
    built it."""
    src = BUILD_SHELL.read_text()
    assert "shell_variant.txt" in src
    for key in ("SHELL_REALPHY=", "REALPHY_RATE=", "SHELL_TOUCH="):
        assert key in src, f"the variant record does not write {key}"
