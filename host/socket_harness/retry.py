from __future__ import annotations

"""Deterministic, injectable-clock connect/reconnect backoff.

This is the reconnection spine shared by every :class:`Session` and by the
console bridge.  It has three design commitments:

* **Pure, testable backoff.**  :meth:`RetryPolicy.backoffs` is a pure function
  of the policy fields, so the schedule can be asserted exactly in a unit test
  (mirroring ``scripts/mps3_console.py``'s determinism philosophy: with the
  default ``jitter=False`` the schedule is reproducible to the float).

* **Injectable clock.**  Time is reached only through the :class:`Clock`
  protocol, so tests drive :func:`connect_with_retry` with a virtual clock
  (``loopback.FakeClock``) and never actually sleep or touch a real socket.

* **No import cycle with session.**  ``session.EndpointClosed`` is a subclass of
  :class:`ConnectionError`, and :class:`ConnectionError` is itself a subclass of
  :class:`OSError`.  We therefore catch ``(OSError, ConnectionError)`` here and
  let ``session.py`` map peer-half-close to ``EndpointClosed`` — this keeps
  ``retry.py`` import-free of ``session`` (the spec's chosen resolution).

No I/O happens at import time.
"""

import random
import time
from dataclasses import dataclass
from typing import Callable, Optional, Protocol, TypeVar

__all__ = [
    "Clock",
    "RealClock",
    "RetryPolicy",
    "connect_with_retry",
    "ConnectFailed",
]

T = TypeVar("T")


class Clock(Protocol):
    """The only time seam.  Implemented by :class:`RealClock` in production and
    by ``loopback.FakeClock`` (virtual time) in tests."""

    def monotonic(self) -> float:
        ...

    def sleep(self, seconds: float) -> None:
        ...


class RealClock:
    """Production :class:`Clock` backed by the stdlib ``time`` module."""

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class ConnectFailed(Exception):
    """Raised by :func:`connect_with_retry` once every attempt is exhausted.

    Carries the last underlying exception both as the chained ``__cause__``
    (the caller sees it via ``raise ... from``) and as the ``last_error``
    attribute for programmatic access.
    """

    def __init__(self, message: str, last_error: Optional[BaseException] = None) -> None:
        super().__init__(message)
        self.last_error = last_error


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential-backoff policy.

    ``attempts`` total tries produce ``attempts - 1`` inter-attempt delays.
    Each delay ``k`` (0-based) is ``min(cap, base * factor ** k)``.  With the
    default ``jitter=False`` the schedule is fully deterministic.
    """

    attempts: int = 5
    base: float = 0.1
    factor: float = 2.0
    cap: float = 5.0
    jitter: bool = False

    def backoffs(self) -> list[float]:
        """Return the deterministic delay schedule of length ``attempts - 1``.

        PURE: a function of the policy fields only (aside from the optional
        jitter draw).  With ``jitter=False`` the result is exact and stable, so
        e.g. ``RetryPolicy().backoffs() == [0.1, 0.2, 0.4, 0.8]``.  When
        ``jitter`` is set each capped delay is scaled by a uniform draw in
        ``[0.5, 1.0)``.
        """
        delays: list[float] = []
        for k in range(self.attempts - 1):
            delay = min(self.cap, self.base * self.factor ** k)
            if self.jitter:
                delay *= random.uniform(0.5, 1.0)
            delays.append(delay)
        return delays


def connect_with_retry(
    open_once: Callable[[], T],
    policy: RetryPolicy,
    clock: Clock,
    on_retry: Optional[Callable[[int, Exception], None]] = None,
) -> T:
    """Call ``open_once`` up to ``policy.attempts`` times, backing off between
    failures via ``clock.sleep``.

    On a connection-shaped failure (``OSError``/``ConnectionError`` — the latter
    covers ``session.EndpointClosed`` without importing it) the k-th backoff is
    slept and, if given, ``on_retry(attempt, exc)`` is notified with the 1-based
    number of the attempt that just failed.  Any other exception propagates
    unchanged (it is not a transient connection problem, so it is neither
    retried nor wrapped).

    Guarantees:

    * ``clock.sleep`` is called exactly ``len(policy.backoffs())`` times in the
      all-fail case (one delay after every failed try except the last).
    * ``clock.sleep`` is called 0 times when the first try succeeds.
    * After the final failed attempt the last underlying exception is re-raised
      wrapped in :class:`ConnectFailed`, chained via ``from``.
    """
    schedule = policy.backoffs()
    last_exc: Optional[BaseException] = None
    for i in range(policy.attempts):
        try:
            return open_once()
        except (OSError, ConnectionError) as exc:
            # ConnectionError (and thus session.EndpointClosed) is an OSError
            # subclass; listed explicitly to document the intent.
            last_exc = exc
            if i >= len(schedule):
                # Final attempt — no backoff after the last try.
                break
            clock.sleep(schedule[i])
            if on_retry is not None:
                on_retry(i + 1, exc)
    raise ConnectFailed(
        f"failed to connect after {policy.attempts} attempt(s)",
        last_exc,
    ) from last_exc
