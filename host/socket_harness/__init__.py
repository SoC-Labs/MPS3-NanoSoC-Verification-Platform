"""socket_harness — board-free unified host endpoint layer over pyverify.

Consolidates every host-facing endpoint of the MPS3 nanoSoC harness
(SWD/UART/SWO/XVC over TCP + xsdb/hw_server CSR access + the MCC serial
console) behind ONE frozen registry, ONE reconnecting Session abstraction,
a Python CSR/xsdb register endpoint, and a ser2net-ready pty/tcp console
bridge — plus a CLI (``python -m socket_harness ...``). stdlib-only beyond
the already-vendored :mod:`pyverify`.

Modules (mirrors the shape of ``pyverify/__init__.py``):

- :mod:`socket_harness.endpoints`      — the single frozen endpoint
  ``REGISTRY`` + lookups; the source of truth every other module consumes
  (ports from ``docs/contracts/net-protocol.md``, CSR bases from
  ``docs/contracts/shell-regmap.md``). Imports no pyverify.
- :mod:`socket_harness.retry`          — deterministic, injectable-clock
  connect/reconnect backoff (the reconnection spine).
- :mod:`socket_harness.session`        — base :class:`Session` lifecycle +
  the ``LineChannel``/``ByteChannel``/``RegisterBackend`` protocols +
  concrete socket/serial sessions. ``LineChannel`` is byte-identical to
  ``pyverify.client.Transport`` so a ``LineSession`` is a drop-in
  ``ShellClient`` transport (the interop keystone).
- :mod:`socket_harness.xsdb`           — the CSR/JTAG register backend over
  xsdb/hw_server; the ``scripts/mps3_diag.tcl`` -> Python port (pure parse
  functions + the ascending LMB-alias magic scan).
- :mod:`socket_harness.registers`      — CSR bitfield models transcribed
  from ``shell-regmap.md`` + a backend-agnostic ``RegisterAccess`` facade.
- :mod:`socket_harness.console_bridge`  — a pure-Python ser2net-compatible
  pty/tcp console bridge that ALSO emits the ser2net/socat configs from the
  one registry (kills the hand-maintained ``host/console/*``).
- :mod:`socket_harness.harness`        — the :class:`SocketHarness` library
  facade composing registry + sessions + registers + bridge and wiring the
  pyverify interop (``shell()`` rides a reconnecting ``LineSession``).
- :mod:`socket_harness.loopback`       — reusable board-free doubles shared
  by the tests and the CLI selftest (fakes + pty loopback).
- :mod:`socket_harness.cli`            — the ``python -m socket_harness``
  front-end (endpoints/emit/console/reg/probe/selftest).

Board-free discipline: no I/O happens at import time, every pure function
(decode/encode/parse/backoff) is unit-testable in isolation, and every
proof runs against in-process fakes or a pty loopback — zero real sockets
to a board.
"""
from __future__ import annotations

from .endpoints import (
    Address,
    DEFAULT_HUB_URL,
    DEFAULT_SHELL_HOST,
    EndpointSpec,
    Family,
    Framing,
    REGISTRY,
    by_family,
    by_name,
    by_port,
    console_specs,
    csr_specs,
)
from .retry import (
    Clock,
    ConnectFailed,
    RealClock,
    RetryPolicy,
    connect_with_retry,
)
from .session import (
    ByteChannel,
    EndpointClosed,
    LineChannel,
    LineSession,
    NotConnected,
    RawStreamSession,
    RegisterBackend,
    SerialSession,
    Session,
    SocketSession,
)
from .xsdb import (
    DiagMailbox,
    TargetNotFound,
    XsdbConfig,
    XsdbRegisterEndpoint,
    parse_mrd_block,
    parse_mrd_value,
)
from .registers import (
    BLOCKS,
    Reg,
    RegBlock,
    RegField,
    RegisterAccess,
)
from .console_bridge import (
    ConsoleBridge,
    LocalEndpoint,
    PtyEndpoint,
    PumpStat,
    TcpListenEndpoint,
    ser2net_yaml,
    socat_argv,
)
from .harness import ProbeResult, SocketHarness

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # endpoints
    "Family",
    "Framing",
    "EndpointSpec",
    "Address",
    "REGISTRY",
    "by_name",
    "by_port",
    "by_family",
    "console_specs",
    "csr_specs",
    "DEFAULT_SHELL_HOST",
    "DEFAULT_HUB_URL",
    # retry
    "Clock",
    "RetryPolicy",
    "RealClock",
    "connect_with_retry",
    "ConnectFailed",
    # session
    "LineChannel",
    "ByteChannel",
    "RegisterBackend",
    "Session",
    "SocketSession",
    "LineSession",
    "RawStreamSession",
    "SerialSession",
    "EndpointClosed",
    "NotConnected",
    # xsdb
    "XsdbConfig",
    "XsdbRegisterEndpoint",
    "DiagMailbox",
    "TargetNotFound",
    "parse_mrd_value",
    "parse_mrd_block",
    # registers
    "RegField",
    "Reg",
    "RegBlock",
    "RegisterAccess",
    "BLOCKS",
    # console_bridge
    "LocalEndpoint",
    "PtyEndpoint",
    "TcpListenEndpoint",
    "PumpStat",
    "ConsoleBridge",
    "ser2net_yaml",
    "socat_argv",
    # harness
    "ProbeResult",
    "SocketHarness",
]
