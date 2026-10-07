"""``pyverify.localport`` -- is a LOCAL TCP port free, and did it get free again?

ILA-mint finding #20 (2026-09-24): an ssh ``ControlMaster`` keeps a ``-L``
forward bound after the ``ssh -N`` that asked for it has exited (on a dev box: local
2542 and 2547 stayed bound to a master long after their sessions ended). A
forward left bound to the Linux board's loopback 6900/6910 would pass the S12
claim lock for anyone on this host. :class:`pyverify.slot.SshTunnel` checks its
ports with these before binding and after closing.

"Free" means what the next binder will see: a bind to ``host:port`` with
``SO_REUSEADDR`` (as ssh and hw_server set it) succeeds. A TIME_WAIT left by an
old connection does not count as busy; a LISTENING socket on the port, on this
address or on the wildcard, does. Stdlib only.
"""
from __future__ import annotations

import socket
import time
from typing import Callable, Iterable, List

__all__ = ["port_free", "busy_ports", "wait_ports_free"]


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    """True when ``host:port`` can be bound for listening right now."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        s.close()


def busy_ports(ports: Iterable[int], host: str = "127.0.0.1") -> List[int]:
    """The subset of ``ports`` that something is already listening on."""
    return [p for p in ports if not port_free(p, host)]


def wait_ports_free(ports: Iterable[int], timeout_s: float = 3.0, *, host: str = "127.0.0.1",
                    poll_s: float = 0.1, sleep: Callable[[float], None] = time.sleep,
                    clock: Callable[[], float] = time.monotonic) -> List[int]:
    """Wait up to ``timeout_s`` for every port to be free. Returns the ports
    STILL bound at the deadline (``[]`` = all released)."""
    ports = list(ports)
    deadline = clock() + timeout_s
    while True:
        busy = busy_ports(ports, host)
        if not busy or clock() >= deadline:
            return busy
        sleep(poll_s)
