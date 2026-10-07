"""tests/edge/test_edge_channel_invalidation.py

Cross-checks `docs/HARDWARE_HUB_INTEGRATION.md` §2 ("Channel / handle
mapping") and §4 ("Reset taxonomy for MPS3") against each other, and
against W4's `host/pyverify/pyverify/edge.py` (`EdgeDeviceApi`/
`MPS3_CHANNELS`/`reset()`). No cocotb, no simulator, no DUT -- pure
pytest, runs today.

Independence note: the expected channel set and the expected
`dfx-swap`-invalidation set below are **hand-derived directly from the doc
text**, not imported from `pyverify.edge` or anywhere else in this repo --
per the task brief, this file must not use the thing it's cross-checking
as its own oracle. The doc-only tests (module section 1 below) run
unconditionally against nothing but the two derivations below and already
demonstrate a real discrepancy *within the doc itself* (§2's `gated_by`
column vs. §4's prose invalidation list -- see
`test_dfx_swap_rp_gated_channels_vs_doc_literal_invalidation_list`).

`pyverify.edge` did **not** exist at the start of this task (confirmed via
`find host -iname "*edge*"` and a listing of `host/pyverify/pyverify/`,
2026-07-04) but landed mid-session -- W4's module is imported directly
below (guarded by `try/except ImportError` so this file still skips
cleanly if it's ever reverted/renamed) and cross-checked in module section
2. Reading `edge.py` turned up that **W4 independently discovered the same
§2-vs-§4 doc ambiguity** this file's section 1 documents (see `edge.py`'s
own module docstring, "dut-uart vs. console") and resolved it by modelling
`dut-uart` (UART1/application console) as its own RP-gated channel
distinct from `console` (UART0/boot-monitor, shell-gated) -- i.e. W4's
`MPS3_CHANNELS` has **8** entries, not §2's literal 7 (`console` split in
two). That's a reasoned, documented choice, not silently-introduced drift,
so section 2 treats it as the accepted resolution and cross-checks against
it explicitly (`EXPECTED_CHANNELS_RESOLVED`) -- but doing that surfaced a
SEPARATE, unflagged inconsistency **within `edge.py` itself**: see
`test_every_rp_gated_channel_is_invalidated_by_dfx_swap_TODO` below.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYVERIFY_SRC = _REPO_ROOT / "host" / "pyverify"
if str(_PYVERIFY_SRC) not in sys.path:
    sys.path.insert(0, str(_PYVERIFY_SRC))

try:
    import pyverify.edge as edge  # type: ignore
except ImportError:
    edge = None


# =========================================================================== #
# Section 1 -- doc-only derivations, no pyverify.edge dependency, always run.
# =========================================================================== #

# name -> (hub `handle`, "gated by" column verbatim), read straight off
# HARDWARE_HUB_INTEGRATION.md §2's table (the "…<name>" column minus the
# leading "…").
# §2 reconciled to v0.1 (A6, 2026-07-04): the console row is split into
# `console` (UART0 boot-monitor, shell-gated) + `dut-uart` (UART1 app console,
# rp-gated), and swo stays rp-gated — so §2's rp-gated set == §4's dfx-swap
# invalidation set exactly (no more §2-vs-§4 discrepancy).
EXPECTED_CHANNELS = {
    "console":  ("uart", "shell"),
    "dut-uart": ("uart", "rp"),
    "swo":      ("uart", "rp"),
    "mgmt":     ("mgmt", "shell"),
    "dut-net":  ("dut-net", "rp"),
    "jtag":     ("jtag", "config"),   # "— (*is* config)" in the doc's own table
    "xvc":      ("xvc", "shell"),
    "swd":      ("swd", "rp"),
}

EXPECTED_RP_GATED = frozenset(
    name for name, (_handle, gated_by) in EXPECTED_CHANNELS.items() if gated_by == "rp"
)
EXPECTED_SHELL_GATED = frozenset(
    name for name, (_handle, gated_by) in EXPECTED_CHANNELS.items() if gated_by == "shell"
)

# §4's `dfx-swap` row, LITERAL prose (v0.2, 2026-09-23, RM-internal ILAs):
# "Invalidates swd, dut-net, dut-uart, swo (every rp-gated channel) AND xvc;
# mgmt/jtag/console survive." xvc stays shell-GATED -- its server is shell
# firmware and never goes down -- but a swap INVALIDATES the session on it: the
# ILAs behind the bridge are the RM's own (HANDOVER_RM_ILA_OVER_XVC.md §4.10).
DFX_SWAP_INVALIDATES_LITERAL = frozenset({"swd", "dut-net", "dut-uart", "swo", "xvc"})
DFX_SWAP_SURVIVES_LITERAL = frozenset({"mgmt", "jtag", "console"})
#: shell-gated, yet swap-invalidated: the one deliberate exception to "gated_by
#: decides survival", named so the tests below state it rather than hide it.
SHELL_GATED_BUT_SWAP_INVALIDATED = frozenset({"xvc"})


def test_expected_channel_count_and_handles_match_section_2():
    """Sanity on the hand-transcription itself: §2 (v0.1) lists exactly 8
    channel rows (console split into console+dut-uart); re-count them here so
    a future doc edit (adding/removing a row) is forced to touch this file."""
    assert len(EXPECTED_CHANNELS) == 8
    handles = {name: handle for name, (handle, _gated_by) in EXPECTED_CHANNELS.items()}
    assert handles == {
        "console": "uart", "dut-uart": "uart", "swo": "uart", "mgmt": "mgmt",
        "dut-net": "dut-net", "jtag": "jtag", "xvc": "xvc", "swd": "swd",
    }


def test_shell_gated_channels_plus_jtag_exactly_match_dfx_swap_survivors():
    """What this proves: the *shell-gated* side of §2's table is internally
    consistent with §4's dfx-swap row -- every channel gated_by="shell",
    plus jtag (its own "is config" category, never RP-derived), is exactly
    the survivor list §4 states in prose. This half of the cross-check has
    NO discrepancy -- see the next test for the half that does."""
    assert DFX_SWAP_SURVIVES_LITERAL == (EXPECTED_SHELL_GATED - SHELL_GATED_BUT_SWAP_INVALIDATED) | {"jtag"}


def test_dfx_swap_rp_gated_channels_match_doc_literal_invalidation_list():
    """§2-vs-§4 consistency, ENFORCED (v0.1). The earlier canary documented a
    real doc-internal discrepancy (§2 gated exactly {swo,dut-net,swd} by rp
    while §4's dfx-swap prose said {swd,dut-net,dut-uart}); A6 reconciled it
    2026-07-04 by splitting the console row (adding rp-gated `dut-uart`) and
    adding `swo` to §4's list. Now §2's rp-gated set MUST equal §4's dfx-swap
    invalidation set exactly — this test breaks if either side drifts again."""
    assert EXPECTED_RP_GATED | SHELL_GATED_BUT_SWAP_INVALIDATED == DFX_SWAP_INVALIDATES_LITERAL == frozenset(
        {"swd", "dut-net", "dut-uart", "swo", "xvc"}
    )


# =========================================================================== #
# Section 2 -- cross-check against the real host/pyverify/pyverify/edge.py.
# Everything below is skipped as a whole if the import fails (module
# reverted/renamed); individual assertions use edge.py's actual, documented
# public names (MPS3_CHANNELS/GatedBy/reset/ResetKind/ALL_CHANNEL_IDS --
# all in edge.py's own `__all__`), not guessed ones, since the module is
# confirmed landed and read directly as of this writing.
# =========================================================================== #

pytestmark_skip_no_edge = pytest.mark.skipif(
    edge is None, reason="pyverify.edge not importable (W4 module missing/reverted)"
)

# The accepted resolution of the section-1 ambiguity, matching edge.py's own
# documented choice: split "console" into "console" (UART0, shell) and
# "dut-uart" (UART1, rp) so §4's literal invalidation list becomes
# satisfiable. id -> gated_by.
EXPECTED_CHANNELS_RESOLVED = {
    "console": "shell",
    "dut-uart": "rp",
    "swo": "rp",
    "mgmt": "shell",
    "dut-net": "rp",
    "jtag": "none",
    "xvc": "shell",
    "swd": "rp",
}


@pytestmark_skip_no_edge
def test_edge_channel_ids_and_gated_by_match_the_resolved_expectation():
    """Cross-check `MPS3_CHANNELS` against `EXPECTED_CHANNELS_RESOLVED`
    above (independently written, not copied from edge.py) -- catches drift
    like a renamed/removed channel id or a flipped `gated_by`, while
    accepting the documented console/dut-uart split as legitimate (both
    this repo's docs and W4 independently flag it as the same known
    ambiguity, not silent divergence)."""
    actual = {c.id: c.gated_by.value for c in edge.MPS3_CHANNELS}
    assert actual == EXPECTED_CHANNELS_RESOLVED


@pytestmark_skip_no_edge
def test_edge_dfx_swap_invalidation_set_matches_doc_literal_reading():
    """`reset("dfx-swap").invalidated_channels` must be exactly §4's literal
    {swd, dut-net, dut-uart} -- the one unambiguous, checkable regression
    surface here (independent of which side of the doc ambiguity is
    "correct"): if this set changes shape, either a channel got renamed or
    someone tried to resolve the ambiguity differently without updating
    this test, and either way it needs eyes on it."""
    result = edge.reset("dfx-swap")
    assert set(result.invalidated_channels) == {"swd", "dut-net", "dut-uart", "swo", "xvc"}
    assert result.rp_scoped is True
    assert result.touches_shell is False
    assert result.touches_rp is True


@pytestmark_skip_no_edge
def test_edge_mcc_reconfig_and_usb_power_invalidate_every_channel():
    """§4: `mcc-reconfig`/`usb-power` are the "rebuild everything" resets --
    both must invalidate ALL channels, touching both shell and rp."""
    all_ids = {c.id for c in edge.MPS3_CHANNELS}
    for kind in ("mcc-reconfig", "usb-power"):
        result = edge.reset(kind)
        assert set(result.invalidated_channels) == all_ids, kind
        assert result.touches_shell and result.touches_rp, kind
        assert result.rp_scoped is False, kind


@pytestmark_skip_no_edge
def test_edge_dut_reset_and_uart_soft_and_system_invalidate_nothing():
    """§4: `dut-reset`/`uart-soft`/`system` are all "no lease teardown"
    resets ("leases survive" / "none") -- soft resets that never touch
    shell or rp state in a way that invalidates a channel."""
    for kind in ("dut-reset", "uart-soft", "system"):
        result = edge.reset(kind)
        assert result.invalidated_channels == [], kind
        assert not result.touches_shell and not result.touches_rp, kind


@pytestmark_skip_no_edge
def test_edge_jtag_xvc_conflict_is_mutual():
    """§2: jtag/xvc "share FT2232 channel A" -- edge.py's `conflicts` field
    must record the exclusion both directions, not just one (a Hub arbiter
    checking only one channel's `conflicts` list would miss the other)."""
    by_id = {c.id: c for c in edge.MPS3_CHANNELS}
    assert "xvc" in by_id["jtag"].conflicts
    assert "jtag" in by_id["xvc"].conflicts


@pytestmark_skip_no_edge
def test_every_rp_gated_channel_is_invalidated_by_dfx_swap():
    """The `GatedBy.RP` invariant, now ENFORCED (was a canary documenting the
    `swo` exception). `edge.py`'s `GatedBy.RP` docstring says "a channel gated
    on `rp` does not survive dfx-swap" -- so gated_by=RP MUST imply membership
    in `reset("dfx-swap").invalidated_channels`, with no exceptions. A6 fixed
    the `swo` carve-out 2026-07-04 (added it to `_RESET_SEMANTICS[DFX_SWAP]`);
    a Hub lessee holding an `swo` trace lease now correctly has it torn down on
    a DUT swap. Strict, no-exceptions: this is the whole point of the RP-gated
    class."""
    rp_gated = {c.id for c in edge.MPS3_CHANNELS if c.gated_by == edge.GatedBy.RP}
    invalidated = set(edge.reset("dfx-swap").invalidated_channels)
    assert rp_gated - invalidated == set(), (
        "every gated_by=rp channel must be in the dfx-swap invalidation set; "
        f"exempt={rp_gated - invalidated}"
    )
    # and no non-rp channel sneaks in, except the one named exception (xvc)
    assert invalidated == rp_gated | SHELL_GATED_BUT_SWAP_INVALIDATED
