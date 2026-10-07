"""``webharness.backend`` — the deployment seam.

Everything above this module (:mod:`webharness.api`, :mod:`webharness.pages`)
is pure and knows nothing about where the harness actually is. A *backend*
answers a small set of verbs as plain dicts and **never raises for a board
that is down** — an unreachable shell is a reportable state, not an exception.

Three backends are foreseen; two exist:

``ShellBackend``  (**host mode — today**)
    Runs on the hub (<hub-host>) and speaks the frozen ``:6900`` JSON-line
    control channel to the board, exactly like ``pyverify.display_http``:
    a short-lived :class:`pyverify.client.ShellClient` per request via an
    injected ``client_factory``. Works against the currently shipped bare-metal
    shell (whichever that is -- ``docs/FIELDED_SHELL.md``) with no firmware
    change and no re-mint.

``FakeBackend``  (tests + ``--fake`` demo)
    Deterministic, in-process, no sockets. Lets the whole UI be exercised —
    and screenshotted — with no board and no network.

``HarnessBackend``  (**on-harness mode — not yet**)
    When the Linux harness shell ships (``src/linux_harness/``), the same API
    and pages run *on the board*: ``/run/mps3/status.json`` for stats, the UIO
    estate for CSRs, loopback ``:6900`` for the verbs. Nothing above this
    module changes. See ``README.md`` §5.

.. rubric:: Why every read is cached

``:6900`` is a **single-client** channel (net-protocol.md; extras are refused
accept-then-EOF). A browser tab on a refresh timer would otherwise hold the
one slot against ``pyverify``, ``fpgahub`` and the console tools. So reads go
through a small TTL cache (:attr:`ShellBackend.ttl`, default 1 s): an idle
page costs at most one short connection per second, and a burst of tabs costs
the same. Writes (reset / set_clk) are never cached and always invalidate.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

__all__ = ["Backend", "ShellBackend", "FakeBackend", "BackendInfo"]


@dataclass(frozen=True)
class BackendInfo:
    """What this server is talking to — surfaced verbatim in ``/api/status``
    so a screenshot is self-describing (which board? or is this a demo?)."""

    mode: str          # 'shell-tcp' | 'fake' | 'on-harness'
    target: str        # host:port, or a description for non-network backends
    live: bool         # False => nothing here reflects real hardware

    def to_dict(self) -> Dict[str, Any]:
        return {"mode": self.mode, "target": self.target, "live": self.live}


class Backend:
    """The verb surface :mod:`webharness.api` depends on. Subclass or duck-type.

    Every method returns a JSON-ready dict carrying at least ``ok`` (bool) and,
    on failure, ``err`` (str) — the same uniform shape the wire contract uses,
    so the HTTP layer never has to special-case a transport failure.
    """

    def info(self) -> BackendInfo:                      # pragma: no cover - abstract
        raise NotImplementedError

    def ping(self) -> Dict[str, Any]:                   # pragma: no cover - abstract
        raise NotImplementedError

    def diag(self) -> Dict[str, Any]:                   # pragma: no cover - abstract
        raise NotImplementedError

    def telemetry(self) -> Dict[str, Any]:              # pragma: no cover - abstract
        raise NotImplementedError

    def reset(self, target: str) -> Dict[str, Any]:     # pragma: no cover - abstract
        raise NotImplementedError

    def set_clk(self, preset: str) -> Dict[str, Any]:   # pragma: no cover - abstract
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Host mode — short-lived ShellClient per request, TTL-cached reads
# --------------------------------------------------------------------------- #

@dataclass
class _CacheEntry:
    at: float
    value: Dict[str, Any]


@dataclass
class ShellBackend(Backend):
    """Live backend over the board's ``:6900`` control channel.

    ``client_factory`` is a zero-arg callable returning something that behaves
    like :class:`pyverify.client.ShellClient` under a ``with`` block (connect
    on enter, close on exit) — the same injection point
    ``pyverify.display_http`` uses, which is what makes this class unit-testable
    with a fake client and no sockets.

    ``clock`` is injected for the same reason: the TTL cache is tested by
    advancing a fake clock, not by sleeping.
    """

    client_factory: Callable[[], Any]
    target: str = ""
    ttl: float = 1.0
    clock: Callable[[], float] = time.monotonic
    _cache: Dict[str, _CacheEntry] = field(default_factory=dict, repr=False)

    def info(self) -> BackendInfo:
        return BackendInfo(mode="shell-tcp", target=self.target or "shell", live=True)

    # -- plumbing ---------------------------------------------------------- #

    def _call(self, fn: Callable[[Any], Any]) -> Dict[str, Any]:
        """Open a short-lived client, run ``fn(client)``, normalise the result.

        Catches the two honest failure classes and reports them as data:
        ``OSError`` (refused / timeout / reset — the board or the network is
        down) and ``pyverify``'s protocol error (a reply we could not parse).
        Anything else propagates: a bug here should be loud, not rendered as
        "board down".
        """
        try:
            from pyverify.client import ShellProtocolError
        except Exception:      # pragma: no cover - pyverify always present in check
            ShellProtocolError = ()  # type: ignore[assignment]
        try:
            with self.client_factory() as client:
                resp = fn(client)
        except OSError as exc:
            return {"ok": False, "err": "shell unreachable: %s" % (exc,)}
        except ShellProtocolError as exc:  # type: ignore[misc]
            return {"ok": False, "err": "shell protocol error: %s" % (exc,)}
        return _as_dict(resp)

    def _cached(self, key: str, fn: Callable[[Any], Any]) -> Dict[str, Any]:
        now = self.clock()
        hit = self._cache.get(key)
        if hit is not None and (now - hit.at) < self.ttl:
            out = dict(hit.value)
            out["cached"] = True
            return out
        value = self._call(fn)
        self._cache[key] = _CacheEntry(at=now, value=value)
        out = dict(value)
        out["cached"] = False
        return out

    def invalidate(self) -> None:
        """Drop every cached read. Called after any write so the page reflects
        the new state on its next poll instead of up to ``ttl`` later."""
        self._cache.clear()

    # -- verbs ------------------------------------------------------------- #

    def ping(self) -> Dict[str, Any]:
        return self._cached("ping", lambda c: c.ping())

    def diag(self) -> Dict[str, Any]:
        return self._cached("diag", lambda c: c.diag())

    def telemetry(self) -> Dict[str, Any]:
        return self._cached("telemetry", lambda c: c.telemetry())

    def reset(self, target: str) -> Dict[str, Any]:
        out = self._call(lambda c: c.reset(target))
        self.invalidate()
        return out

    def set_clk(self, preset: str) -> Dict[str, Any]:
        # NB: no client-side preset validation here — the API layer already
        # rejects unknown presets against the catalog, and the shell's own
        # fail-closed strcmp table is the authority (clkrst.c:126-150). Passing
        # an unknown preset through on purpose is how the conformance suites
        # prove the server rejects it.
        out = self._call(lambda c: c.set_clk(preset))
        self.invalidate()
        return out


def _as_dict(resp: Any) -> Dict[str, Any]:
    """Normalise a pyverify response dataclass (or a plain dict) to a dict.

    ``TelemetryResponse`` deliberately has no ``mv``/``ma`` attributes — they
    are *absent*, not zero (client.py's v0.6 note) — so a generic
    ``__dict__``-style copy is exactly right: it carries what the reply had and
    invents nothing.
    """
    if isinstance(resp, dict):
        out = dict(resp)
    elif hasattr(resp, "__dict__"):
        out = dict(vars(resp))
    else:
        out = {"ok": bool(resp)}
    out["ok"] = bool(out.get("ok", False))
    return out


# --------------------------------------------------------------------------- #
# Fake mode — deterministic, no sockets
# --------------------------------------------------------------------------- #

@dataclass
class FakeBackend(Backend):
    """In-process fake: drives the whole UI with no board and no network.

    Defaults are SYNTHETIC FIXTURE VALUES modelling a JTAG shell with a
    single-core nanoSoC resident. They are deliberately NOT kept in step with the
    fielded shell (``docs/FIELDED_SHELL.md``): a fixture that tracks the board
    needs editing at every mint, and the id below is asserted by tests, so the
    churn would land in three files for no gain. Nothing here is a claim about
    what is on a board. ``reachable=False`` models a board that is down, so the
    "everything degrades honestly" path is exercisable in tests and demos.
    """

    shell_id: str = "0xcd74b6ae"
    rm_id: str = "0x01000001"
    reachable: bool = True
    locked: bool = True
    preset: str = "50mhz"
    #: every reset/set_clk that was accepted, in order — tests assert on this
    calls: list = field(default_factory=list)

    def info(self) -> BackendInfo:
        return BackendInfo(mode="fake", target="in-process fake (no board)", live=False)

    def _down(self) -> Dict[str, Any]:
        return {"ok": False, "err": "shell unreachable: fake backend is down"}

    def ping(self) -> Dict[str, Any]:
        if not self.reachable:
            return self._down()
        return {"ok": True, "shell_id": self.shell_id, "rm_id": self.rm_id}

    def diag(self) -> Dict[str, Any]:
        if not self.reachable:
            return self._down()
        return {
            "ok": True, "rx_recover": 0, "rx_dumps": 0, "rx_drops": 0,
            "icap_bytes": 0, "got": 0, "expect": 0, "rcv_wnd": 5840,
            "rcv_ann_wnd": 5840, "rx_queued": 0, "pbuf_free": 16,
            "grants_sent": 0, "grant_fails": 0, "sndbuf": 2048, "snd_wnd": 5840,
            # v0.9: the 11 formerly JTAG-only counters, same order as the firmware.
            "tx_frames_sent": 0,
            "tx_status_drained": 0,
            "tx_fifo_full_drops": 0,
            "tx_errors": 0,
            "tx_space_stalls": 0,
            "tx_iface_errors": 0,
            "tx_last_status": 0,
            "icap_sr_last": 0,
            "icap_eos_status": 0,
            "ovlstore_phase": 0,
            "ovlstore_detail": 0,
            # v0.9.1: the STMPE811 panel-continuity probe (diag.h v7). 0 across
            # the board is what a shell built without TOUCH=1 reports, and it
            # decodes as verdict "unknown" -- no probe ran.
            "touch_regs": 0,
            "touch_adc_x": 0,
            "touch_adc_y": 0,
            "touch_verdict": 0,
            # ... and DiagResponse's derived rendering of them, so the fake and
            # the real backend hand the page the same key set.
            "touch_verdict_word": "unknown",
            "touch_regs_hex": "0x00000000",
            "touch_adc_x_hex": "0x00000000",
            "touch_adc_y_hex": "0x00000000",
            # v0.9.2: the superloop service telemetry (diag.h v8). Plausible
            # HEALTHY values, not zeroes: this fake exists so the page can be
            # developed without a board, and a page that only ever renders zero
            # never shows what a sick service looks like. svc_skipped == 0 is
            # the healthy case and is the one field worth watching.
            "svc_count": 12,
            "pass_max_us": 1840,
            "svc_max_us": 900,
            "svc_max_ix": 4,
            "svc_overruns": 0,
            "svc_skips": 0,
            "svc_skipped": 0,
            "svc_us_0": 0x012C0384,   # net_rx 900 us, net_tmr 300 us
            "svc_us_1": 0x0258002A,   # tx_drain 42 us, swap 600 us
            "svc_us_2": 0x00320190,   # cfgagent 400 us, ctrl 50 us
            "svc_us_3": 0x00C80014,   # jtag 20 us, xvc 200 us
            "svc_us_4": 0x0096001E,   # uart 30 us, clcd 150 us
            "svc_us_5": 0x0008006E,   # diag 110 us, hbeat 8 us
        }

    def telemetry(self) -> Dict[str, Any]:
        # ALWAYS a failure, with lockup carried — net-protocol.md v0.6's single
        # documented carve-out. The fake must not invent a success shape.
        if not self.reachable:
            return self._down()
        return {"ok": False, "err": "no power sensor", "lockup": False}

    def reset(self, target: str) -> Dict[str, Any]:
        if not self.reachable:
            return self._down()
        if target != "dut":
            return {"ok": False, "err": "bad target"}
        self.calls.append(("reset", target))
        return {"ok": True}

    def set_clk(self, preset: str) -> Dict[str, Any]:
        if not self.reachable:
            return self._down()
        if preset not in ("25mhz", "50mhz", "100mhz"):
            return {"ok": False, "err": "unknown preset"}
        self.calls.append(("set_clk", preset))
        self.preset = preset
        return {"ok": True, "locked": self.locked}
