"""``webharness.backend`` — the seam, with a fake client and a fake clock.

No sockets: ``ShellBackend`` takes a ``client_factory``, so a recording stub
stands in for :class:`pyverify.client.ShellClient`, and the TTL cache is tested
by advancing a fake clock rather than sleeping.

The connection-count assertions are the load-bearing ones. ``:6900`` is a
**single-client** channel; if this backend held a connection open, or opened
one per widget on a polling page, it would lock ``pyverify``/``fpgahub`` out of
the board. "One short connection per uncached read, none per cached read" is a
correctness property here, not an optimisation.
"""
from __future__ import annotations

import pytest

from webharness.backend import FakeBackend, ShellBackend, _as_dict


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class RecordingClient:
    """Behaves like a ShellClient under ``with``; records what was asked."""

    def __init__(self, log, fail=None):
        self.log = log
        self.fail = fail
        self.open = False

    def __enter__(self):
        if self.fail is not None:
            raise self.fail
        self.open = True
        self.log.append("connect")
        return self

    def __exit__(self, *exc):
        self.open = False
        self.log.append("close")
        return False

    def ping(self):
        assert self.open, "verb issued on a closed client"
        self.log.append("ping")
        return {"ok": True, "shell_id": "0xcd74b6ae", "rm_id": "0x01000001"}

    def diag(self):
        self.log.append("diag")
        return {"ok": True, "icap_bytes": 7}

    def telemetry(self):
        self.log.append("telemetry")
        return {"ok": False, "err": "no power sensor", "lockup": False}

    def reset(self, target):
        self.log.append(("reset", target))
        return {"ok": True}

    def set_clk(self, preset):
        self.log.append(("set_clk", preset))
        return {"ok": True, "locked": True}


def make(fail=None, ttl=1.0):
    log = []
    clock = FakeClock()
    backend = ShellBackend(
        client_factory=lambda: RecordingClient(log, fail=fail),
        target="192.168.10.101", ttl=ttl, clock=clock,
    )
    return backend, log, clock


# --------------------------------------------------------------------------- #
# Connection discipline
# --------------------------------------------------------------------------- #

def test_each_uncached_read_opens_and_closes_exactly_one_connection():
    backend, log, _ = make()
    backend.ping()
    assert log == ["connect", "ping", "close"]


def test_a_cached_read_opens_no_connection_at_all():
    backend, log, clock = make(ttl=1.0)
    backend.ping()
    log.clear()
    for _ in range(20):
        out = backend.ping()
        assert out["cached"] is True
    assert log == [], "a polling browser must not contend for the single client slot"


def test_the_cache_expires():
    backend, log, clock = make(ttl=1.0)
    backend.ping()
    clock.advance(1.5)
    log.clear()
    assert backend.ping()["cached"] is False
    assert log == ["connect", "ping", "close"]


def test_different_verbs_are_cached_independently():
    backend, log, _ = make()
    backend.ping()
    log.clear()
    backend.diag()
    assert "diag" in log, "diag must not be served from ping's cache entry"


def test_writes_are_never_cached_and_invalidate_the_reads():
    backend, log, _ = make(ttl=60.0)
    backend.ping()
    backend.reset("dut")
    log.clear()
    # the post-write ping must go back to the board, not answer from before it
    assert backend.ping()["cached"] is False
    assert log == ["connect", "ping", "close"]


def test_two_writes_both_reach_the_board():
    backend, log, _ = make(ttl=60.0)
    backend.set_clk("25mhz")
    backend.set_clk("50mhz")
    assert [e for e in log if isinstance(e, tuple)] == [
        ("set_clk", "25mhz"), ("set_clk", "50mhz")
    ]


# --------------------------------------------------------------------------- #
# Failure handling — a down board is data, not an exception
# --------------------------------------------------------------------------- #

def test_connection_refused_becomes_a_reportable_error():
    backend, _, _ = make(fail=ConnectionRefusedError(111, "Connection refused"))
    out = backend.ping()
    assert out["ok"] is False
    assert out["err"].startswith("shell unreachable")


def test_timeout_becomes_a_reportable_error():
    backend, _, _ = make(fail=OSError("timed out"))
    assert backend.ping()["err"].startswith("shell unreachable")


def test_protocol_error_is_distinguished_from_unreachable():
    from pyverify.client import ShellProtocolError
    backend, _, _ = make(fail=ShellProtocolError("garbage reply"))
    out = backend.ping()
    assert out["ok"] is False
    assert out["err"].startswith("shell protocol error")


def test_an_unexpected_exception_is_not_swallowed_as_board_down():
    """A bug in this package must be loud. Only OSError/ShellProtocolError are
    'the board did not answer'."""
    backend, _, _ = make(fail=ValueError("bug"))
    with pytest.raises(ValueError):
        backend.ping()


def test_failures_are_cached_too_so_a_down_board_is_not_hammered():
    backend, log, _ = make(fail=ConnectionRefusedError())
    backend.ping()
    n = len(log)
    for _ in range(10):
        backend.ping()
    assert len(log) == n


# --------------------------------------------------------------------------- #
# Response normalisation
# --------------------------------------------------------------------------- #

def test_as_dict_carries_pyverify_dataclasses_without_inventing_fields():
    from pyverify.client import PingResponse, TelemetryResponse
    d = _as_dict(PingResponse(ok=True, shell_id="0x1", rm_id="0x2"))
    assert d == {"ok": True, "shell_id": "0x1", "rm_id": "0x2"}

    t = _as_dict(TelemetryResponse(ok=False, err="no power sensor", lockup=True))
    assert t["lockup"] is True
    # mv/ma are ABSENT by design (client.py v0.6) — normalisation must not
    # resurrect them as zeros.
    assert "mv" not in t and "ma" not in t


def test_as_dict_forces_ok_to_a_bool():
    assert _as_dict({"ok": 1})["ok"] is True
    assert _as_dict({})["ok"] is False


def test_backend_info_is_pure_and_self_describing():
    backend, log, _ = make()
    info = backend.info()
    assert info.mode == "shell-tcp" and info.live is True
    assert info.target == "192.168.10.101"
    assert log == [], "info() must not open a connection"


# --------------------------------------------------------------------------- #
# FakeBackend
# --------------------------------------------------------------------------- #

def test_fake_backend_is_marked_not_live():
    info = FakeBackend().info()
    assert info.live is False and info.mode == "fake"


def test_fake_backend_telemetry_never_has_a_success_shape():
    out = FakeBackend().telemetry()
    assert out["ok"] is False and out["err"] == "no power sensor"
    assert "mv" not in out


def test_fake_backend_rejects_what_the_real_shell_rejects():
    b = FakeBackend()
    assert b.reset("rp")["err"] == "bad target"
    assert b.set_clk("33mhz")["err"] == "unknown preset"
    assert b.calls == []


def test_fake_backend_down_mode_reports_unreachable_everywhere():
    b = FakeBackend(reachable=False)
    for out in (b.ping(), b.diag(), b.telemetry(), b.reset("dut"), b.set_clk("25mhz")):
        assert out["ok"] is False and "unreachable" in out["err"]
