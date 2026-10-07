"""``pyverify.mactest`` — MAC-in-operation test driver (I10 tail; spec §8).

Scripts the shell's error-inject traffic generator/checker (GENCHK, driven by
the ``macgen`` control verb — net-protocol.md "MAC gen/checker control") into
one self-contained MAC test:

1. **baseline** — enable gen+chk with ``inject:"none"`` and snapshot the
   counters;
2. **clean traffic** — a few more clean rounds; assert ``TX_CNT``/``RX_CNT``
   advance and ``ERR_CNT`` does **not** (a clean run must not manufacture
   errors);
3. **fault inject** — arm one fault (default ``bad_fcs``) and assert
   ``ERR_CNT`` increments (the checker caught the injected bad frame);
4. **link event** — inject a VPHY link event via the existing ``link`` verb
   (default a ``down``/``up`` blip), asserting each is accepted.

It reports a single :class:`MacTestResult` (``passed`` + the counter snapshots
+ a human ``detail``). Because it only uses the ``macgen``/``link`` control
verbs, it runs unchanged against a live shell OR the reference
:class:`pyverify.testing.fakeshell.FakeShell` with no hardware — the driver's
own suite (``host/pyverify/tests/test_mactest.py``) drives the latter.

The driver reasons about counter **deltas** within a single enabled session
(net-protocol.md / shell-regmap.md v0.4 "Counter semantics"): the RTL clears
the counters on a ``gen_en``/``chk_en`` 0→1 rising edge and they advance
monotonically thereafter, so this driver enables gen+chk once at the baseline
and then compares deltas between calls (never across an enable toggle), never
assuming a particular absolute start value — only that clean traffic advances
tx/rx with no new errors and an armed inject advances err.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, Sequence, Tuple

from .client import MACGEN_INJECTS, LinkResponse, MacGenResponse, validate_macgen_inject

__all__ = ["MacTestResult", "MacGenDriver", "run_mac_test"]


class MacGenDriver(Protocol):
    """The slice of :class:`~pyverify.client.ShellClient` the driver needs —
    so it duck-types over the real client, :class:`~pyverify.board.Mps3Board`,
    or any test double."""

    def macgen(self, *, gen: bool = ..., chk: bool = ..., inject: str = ...) -> MacGenResponse: ...

    def link(self, event: str) -> LinkResponse: ...


@dataclass(frozen=True)
class MacTestResult:
    """Outcome of :func:`run_mac_test`.

    ``passed`` is the overall verdict; ``detail`` names the first failed
    expectation (``""`` on success). The three snapshots are the ``macgen``
    replies at each phase so a caller can log/inspect the actual counters.
    """

    passed: bool
    baseline: Optional[MacGenResponse] = None
    clean: Optional[MacGenResponse] = None
    faulted: Optional[MacGenResponse] = None
    link_events: Tuple[str, ...] = field(default_factory=tuple)
    detail: str = ""


def run_mac_test(
    driver: MacGenDriver,
    *,
    inject: str = "bad_fcs",
    clean_rounds: int = 2,
    link_events: Sequence[str] = ("down", "up"),
    injects: "Optional[Sequence[str]]" = MACGEN_INJECTS,
) -> MacTestResult:
    """Run the MAC-in-operation scenario against ``driver``.

    Parameters
    ----------
    inject:
        The fault to arm for the error round (one of
        :data:`~pyverify.client.MACGEN_INJECTS`; validated up front unless
        ``injects=None``).
    clean_rounds:
        Number of clean (``inject:"none"``) traffic rounds after the baseline
        (``>= 1`` so there is a delta to measure).
    link_events:
        VPHY link events to inject at the end (each must be accepted). Pass
        ``()`` to skip the link phase.
    injects:
        Client-side inject allow-list (``None`` disables the check).

    Returns a :class:`MacTestResult`; never raises for a *verb-level* failure
    (that becomes ``passed=False`` + ``detail``). A ``ValueError`` is still
    raised for a caller mistake (unknown ``inject``, ``clean_rounds < 1``).
    """
    validate_macgen_inject(inject, injects)
    if clean_rounds < 1:
        raise ValueError("clean_rounds must be >= 1 (need a delta to measure)")
    if inject == "none":
        raise ValueError("inject must be a real fault, not 'none' (the error "
                          "round would then match the clean round)")

    # 1) baseline — gen+chk on, no fault.
    baseline = driver.macgen(gen=True, chk=True, inject="none")
    if not baseline.ok:
        return MacTestResult(False, baseline=baseline, detail="baseline macgen rejected")

    # 2) clean traffic — counters must advance, errors must NOT.
    clean = baseline
    for _ in range(clean_rounds):
        clean = driver.macgen(gen=True, chk=True, inject="none")
        if not clean.ok:
            return MacTestResult(False, baseline=baseline, clean=clean,
                                 detail="clean-traffic macgen rejected")
    if not (clean.tx > baseline.tx and clean.rx > baseline.rx):
        return MacTestResult(
            False, baseline=baseline, clean=clean,
            detail=(f"clean traffic did not advance counters "
                    f"(tx {baseline.tx}->{clean.tx}, rx {baseline.rx}->{clean.rx})"),
        )
    if clean.err != baseline.err:
        return MacTestResult(
            False, baseline=baseline, clean=clean,
            detail=f"clean traffic produced errors (err {baseline.err}->{clean.err})",
        )

    # 3) fault inject — ERR_CNT must increment.
    faulted = driver.macgen(gen=True, chk=True, inject=inject)
    if not faulted.ok:
        return MacTestResult(False, baseline=baseline, clean=clean, faulted=faulted,
                             detail=f"fault-inject macgen ({inject}) rejected")
    if faulted.err <= clean.err:
        return MacTestResult(
            False, baseline=baseline, clean=clean, faulted=faulted,
            detail=(f"inject '{inject}' did not raise err count "
                    f"(err {clean.err}->{faulted.err})"),
        )

    # 4) link events — each must be accepted.
    seen: list[str] = []
    for event in link_events:
        resp = driver.link(event)
        seen.append(event)
        if not resp.ok:
            return MacTestResult(
                False, baseline=baseline, clean=clean, faulted=faulted,
                link_events=tuple(seen),
                detail=f"link event {event!r} rejected",
            )

    return MacTestResult(
        True, baseline=baseline, clean=clean, faulted=faulted,
        link_events=tuple(seen),
        detail="",
    )
