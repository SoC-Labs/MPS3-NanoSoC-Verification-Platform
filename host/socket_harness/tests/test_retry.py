"""Deterministic backoff schedule + proof that the sleeps actually happen.

Board-free and socket-free: ``connect_with_retry`` is exercised with an
in-process ``open_once`` callable and a virtual ``FakeClock`` (from
``socket_harness.loopback``), so no time actually passes and no socket is
opened. The whole reconnection spine is verified as a pure state machine.

The pacing philosophy mirrors scripts/mps3_console.py: with ``jitter=False`` the
schedule is exact, so the timing assertions can be equalities rather than
tolerances.
"""
from __future__ import annotations

import pytest

from socket_harness.retry import (
    ConnectFailed,
    RealClock,
    RetryPolicy,
    connect_with_retry,
)
from socket_harness.loopback import FakeClock


# ---------------------------------------------------------------------------
# A thin recording wrapper over the loopback FakeClock. It satisfies the
# retry.Clock protocol (monotonic/sleep) and additionally records every slept
# delay so the test can assert the exact schedule AND count of sleeps,
# independent of FakeClock's internal counter semantics. `monotonic` is
# delegated so virtual time still advances by the slept amounts.
# ---------------------------------------------------------------------------
class RecordingClock:
    def __init__(self, base: FakeClock | None = None) -> None:
        self._base = base if base is not None else FakeClock()
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self._base.monotonic()

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._base.sleep(seconds)


def make_open_once(fail_times: int, value: str = "SOCK"):
    """Return an ``open_once`` that raises a connection error the first
    ``fail_times`` calls, then returns ``value``. Every raised exception is a
    distinct instance recorded on ``.exceptions`` so the test can identify the
    *last* one (for ConnectFailed chaining).
    """
    state = {"calls": 0}
    exceptions: list[BaseException] = []

    def open_once() -> str:
        state["calls"] += 1
        if state["calls"] <= fail_times:
            exc = ConnectionRefusedError(f"refused #{state['calls']}")
            exceptions.append(exc)
            raise exc
        return value

    open_once.state = state  # type: ignore[attr-defined]
    open_once.exceptions = exceptions  # type: ignore[attr-defined]
    return open_once


# ---------------------------------------------------------------------------
# The pure backoff schedule.
# ---------------------------------------------------------------------------
def test_backoff_schedule_exact_and_capped_length():
    policy = RetryPolicy(attempts=5, base=0.1, factor=2.0, cap=5.0)
    schedule = policy.backoffs()
    # len == attempts - 1
    assert len(schedule) == policy.attempts - 1
    # exact geometric growth (float-exact for these operands, verified)
    assert schedule == [0.1, 0.2, 0.4, 0.8]


def test_backoff_schedule_is_capped():
    policy = RetryPolicy(attempts=6, base=1.0, factor=3.0, cap=5.0)
    # 1, 3, 9, 27, 81 -> capped at 5 from the third delay on.
    assert policy.backoffs() == [1.0, 3.0, 5.0, 5.0, 5.0]


def test_backoff_default_is_no_jitter():
    # Default jitter=False keeps the schedule deterministic (self-test timing).
    assert RetryPolicy().jitter is False


def test_backoff_jitter_stays_within_half_to_full_band():
    base_schedule = RetryPolicy(attempts=5, base=0.1, factor=2.0, cap=5.0).backoffs()
    jittered = RetryPolicy(
        attempts=5, base=0.1, factor=2.0, cap=5.0, jitter=True
    ).backoffs()
    assert len(jittered) == len(base_schedule)
    eps = 1e-9
    for d, j in zip(base_schedule, jittered):
        # uniform(0.5, 1.0) * d  =>  0.5*d <= j <= d
        assert 0.5 * d - eps <= j <= d + eps


# ---------------------------------------------------------------------------
# connect_with_retry: first-try success sleeps zero times.
# ---------------------------------------------------------------------------
def test_first_try_success_returns_value_without_sleeping():
    clock = RecordingClock()
    open_once = make_open_once(fail_times=0)
    result = connect_with_retry(open_once, RetryPolicy(), clock)
    assert result == "SOCK"
    assert clock.sleeps == []
    assert open_once.state["calls"] == 1


# ---------------------------------------------------------------------------
# connect_with_retry: fail k-1 times then succeed -> exactly k-1 sleeps, in
# the scheduled order, and the returned value is the eventual success.
# ---------------------------------------------------------------------------
def test_eventual_success_sleeps_the_scheduled_prefix():
    policy = RetryPolicy(attempts=5, base=0.1, factor=2.0, cap=5.0)
    clock = RecordingClock()
    # succeed on the 3rd attempt => 2 failures => 2 sleeps
    open_once = make_open_once(fail_times=2)

    start = clock.monotonic()
    result = connect_with_retry(open_once, policy, clock)
    elapsed = clock.monotonic() - start

    # return-value proof (orthogonal to timing)
    assert result == "SOCK"
    assert open_once.state["calls"] == 3

    # exact schedule prefix and count
    expected = policy.backoffs()[:2]  # [0.1, 0.2]
    assert clock.sleeps == expected
    assert len(clock.sleeps) == 2

    # timing proof: virtual time advanced by the consumed backoffs.
    assert elapsed == pytest.approx(sum(expected))
    # MUTATION target: this is the 'elapsed >= sum(backoffs)' assertion --
    # replacing clock.sleep(delay) with pass reddens only this timing check
    # while the return-value assertions above still pass.
    assert elapsed >= sum(expected)


def test_on_retry_callback_fires_once_per_retry():
    policy = RetryPolicy(attempts=5, base=0.1, factor=2.0, cap=5.0)
    clock = RecordingClock()
    open_once = make_open_once(fail_times=2)

    calls: list[tuple[int, BaseException]] = []

    def on_retry(attempt: int, exc: Exception) -> None:
        calls.append((attempt, exc))

    result = connect_with_retry(open_once, policy, clock, on_retry=on_retry)
    assert result == "SOCK"
    # one callback per performed retry == one per sleep
    assert len(calls) == len(clock.sleeps) == 2
    # attempt indices are ints, strictly increasing; each carries the raised exc
    attempts = [a for a, _ in calls]
    assert attempts == sorted(attempts)
    assert all(isinstance(a, int) for a in attempts)
    for _, exc in calls:
        assert exc in open_once.exceptions


# ---------------------------------------------------------------------------
# connect_with_retry: all attempts fail -> ConnectFailed, chaining the last
# underlying exception, after sleeping the full schedule (attempts-1 sleeps).
# ---------------------------------------------------------------------------
def test_all_fail_raises_connectfailed_chaining_last_exception():
    policy = RetryPolicy(attempts=5, base=0.1, factor=2.0, cap=5.0)
    clock = RecordingClock()
    # always fail (fail_times >= attempts guarantees every attempt raises)
    open_once = make_open_once(fail_times=99)

    start = clock.monotonic()
    with pytest.raises(ConnectFailed) as excinfo:
        connect_with_retry(open_once, policy, clock)
    elapsed = clock.monotonic() - start

    # tried exactly `attempts` times
    assert open_once.state["calls"] == policy.attempts

    # slept the full schedule (one sleep between each pair of attempts)
    full_schedule = policy.backoffs()
    assert clock.sleeps == full_schedule
    assert len(clock.sleeps) == policy.attempts - 1

    # ConnectFailed chains the LAST underlying exception (raise ... from exc)
    assert excinfo.value.__cause__ is open_once.exceptions[-1]

    # timing proof (mutation target)
    assert elapsed >= sum(full_schedule)
    assert elapsed == pytest.approx(sum(full_schedule))


# ---------------------------------------------------------------------------
# NEGATIVE CONTROL: a non-connection error must propagate immediately -- it is
# NOT retried and NOT wrapped in ConnectFailed, and no sleep happens. This
# guards against connect_with_retry swallowing programming errors.
# ---------------------------------------------------------------------------
def test_non_connection_error_propagates_without_retry():
    clock = RecordingClock()

    def open_once() -> str:
        raise ValueError("bug, not a connection failure")

    with pytest.raises(ValueError):
        connect_with_retry(open_once, RetryPolicy(), clock)
    assert clock.sleeps == []  # never entered the backoff path


# ---------------------------------------------------------------------------
# RealClock smoke: it satisfies the Clock protocol shape and sleep(0) is a
# board-free no-op (no real delay incurred by the suite).
# ---------------------------------------------------------------------------
def test_realclock_shape():
    rc = RealClock()
    t = rc.monotonic()
    assert isinstance(t, float)
    rc.sleep(0)  # instant, no wall-clock cost
    assert rc.monotonic() >= t
