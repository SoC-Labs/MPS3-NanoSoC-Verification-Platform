"""``webharness`` — a small web dashboard for the MPS3 nanoSoC harness.

System stats, DUT identity, DUT clock/reset control, and the
"what can I attach to and how" service directory — served over HTTP from one
pure routing function, against a swappable backend.

Layering (each layer knows nothing about the one below it):

``catalog`` / ``control``
    Pure data + pure logic. Which services a given RM exposes, what the clock
    presets really do, which resets are actually drivable. Transcribed from the
    firmware and the frozen contracts, with drift-guard tests.
``backend``
    The deployment seam. ``ShellBackend`` (hub host -> board :6900, today),
    ``FakeBackend`` (no board), ``HarnessBackend`` (on-board Linux, future).
``api``
    One pure ``handle(method, path, body, backend) -> Response``.
``pages`` / ``server``
    The HTML, and a ``BaseHTTPRequestHandler`` that only moves bytes.

See ``README.md`` for how to run it and what is deliberately not here.
"""
from __future__ import annotations

from .api import API_VERSION, Response, handle
from .backend import Backend, BackendInfo, FakeBackend, ShellBackend

__all__ = [
    "API_VERSION", "Response", "handle",
    "Backend", "BackendInfo", "FakeBackend", "ShellBackend",
]
