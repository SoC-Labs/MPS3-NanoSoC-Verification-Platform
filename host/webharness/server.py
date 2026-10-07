"""``webharness.server`` — the thin HTTP glue. All logic lives in
:mod:`webharness.api`; this module only moves bytes.

Run it on the hub (<hub-host>), pointed at a board::

    python -m webharness --shell-host 192.168.10.101

…or with no board at all, to see the whole UI::

    python -m webharness --fake

.. rubric:: Binding, and why the default is localhost

This server can **reset the DUT and retune its clock**. It has no
authentication — deliberately, because adding a half-measure would invite
trusting it. So it binds ``127.0.0.1`` by default and prints a loud warning if
you ask it to bind anything wider. To reach it from elsewhere, forward a port
over SSH (``ssh -L 8080:localhost:8080 <hub-host>``) or put it behind the
``wg0`` mgmt tunnel the tender already uses — do not put it on the lab LAN.
"""
from __future__ import annotations

import argparse
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from .api import handle
from .backend import FakeBackend, ShellBackend

__all__ = ["make_handler_class", "serve", "build_backend", "main"]

#: Request bodies are tiny JSON objects. Anything larger is refused unread —
#: fail closed rather than buffering whatever a client claims to be sending.
MAX_BODY = 64 * 1024

DEFAULT_PORT = 8080
DEFAULT_SHELL_PORT = 6900


def make_handler_class(backend: Any, host_label: str) -> type:
    """A :class:`BaseHTTPRequestHandler` subclass bound to ``backend``."""

    class _Handler(BaseHTTPRequestHandler):
        server_version = "mps3-webharness/1.0"
        protocol_version = "HTTP/1.1"      # keep-alive; Content-Length always set

        def _read_body(self) -> Optional[bytes]:
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                return None
            if length < 0 or length > MAX_BODY:
                return None
            if length == 0:
                return b""
            return self.rfile.read(length)

        def _dispatch(self) -> None:
            body = self._read_body()
            if body is None:
                # We are refusing the body UNREAD, so whatever the client
                # already sent is still in the socket. Under HTTP/1.1
                # keep-alive those leftover bytes would be parsed as the next
                # request line — so this response ends the connection.
                payload = b'{"ok":false,"err":"bad or oversized request body"}\n'
                self._write(413, "application/json", payload, close=True)
                return
            resp = handle(self.command, self.path, body, backend,
                          host_label=host_label)
            self._write(resp.status, resp.content_type, resp.body)

        def _write(self, status: int, ctype: str, payload: bytes,
                   close: bool = False) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            # This page drives hardware; a cached /api/status would show a
            # stale board.
            self.send_header("Cache-Control", "no-store")
            if close:
                # close_connection is what actually ends it: the handler loop
                # checks it after this request and stops, so the refused body's
                # leftover bytes are never parsed as the next request line.
                # (Without this the server emits a spurious second response —
                # a 414 on the payload — proven by the mutation in
                # tests/test_server.py.) The header tells the client too.
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:    # noqa: N802 (http.server naming)
            self._dispatch()

        def do_POST(self) -> None:   # noqa: N802
            self._dispatch()

        def log_message(self, fmt: str, *args: Any) -> None:
            # One tidy line per request on stderr, instead of http.server's
            # default noise. Quiet enough to leave running in a tmux pane.
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    return _Handler


def build_backend(
    *,
    fake: bool,
    shell_host: str = "",
    shell_port: int = DEFAULT_SHELL_PORT,
    timeout: float = 5.0,
    ttl: float = 1.0,
) -> Any:
    """Pick a backend from CLI-shaped arguments (separated out so tests can
    build the same objects the CLI does)."""
    if fake:
        return FakeBackend()

    from pyverify.client import ShellClient

    def factory() -> Any:
        return ShellClient(shell_host, port=shell_port, timeout=timeout)

    return ShellBackend(client_factory=factory, target=shell_host, ttl=ttl)


def serve(
    backend: Any,
    *,
    listen_host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    host_label: str = "",
) -> ThreadingHTTPServer:
    """Build (but do not block on) the server. The caller runs
    ``.serve_forever()``; tests drive it and call ``.shutdown()``. Pass
    ``port=0`` for an OS-assigned port (``server.server_address[1]``)."""
    if not host_label:
        host_label = backend.info().target or "<shell-host>"
    return ThreadingHTTPServer((listen_host, port),
                               make_handler_class(backend, host_label))


def main(argv: "Optional[list]" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="webharness",
        description="Small web dashboard for the MPS3 nanoSoC harness: system "
                    "stats, DUT identity, clock/reset control, and the "
                    "attachable-services directory.",
    )
    parser.add_argument("--shell-host", default="",
                        help="board shell host/IP (net-protocol.md :6900). "
                             "Required unless --fake.")
    parser.add_argument("--shell-port", type=int, default=DEFAULT_SHELL_PORT)
    parser.add_argument("--fake", action="store_true",
                        help="run against the in-process fake backend — the "
                             "whole UI, no board, no network")
    parser.add_argument("--listen-host", default="127.0.0.1",
                        help="interface to bind (default localhost; see the "
                             "module docstring before widening this)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=5.0,
                        help="per-request control-channel timeout, seconds")
    parser.add_argument("--ttl", type=float, default=1.0,
                        help="read cache TTL, seconds — :6900 is single-client, "
                             "so this bounds how often a browser can contend "
                             "with pyverify/fpgahub for it")
    parser.add_argument("--host-label", default="",
                        help="host as it should appear in the rendered attach "
                             "commands (defaults to --shell-host)")
    args = parser.parse_args(argv)

    if not args.fake and not args.shell_host:
        parser.error("--shell-host is required (or use --fake)")

    backend = build_backend(
        fake=args.fake, shell_host=args.shell_host, shell_port=args.shell_port,
        timeout=args.timeout, ttl=args.ttl,
    )
    host_label = args.host_label or args.shell_host or "<shell-host>"
    server = serve(backend, listen_host=args.listen_host, port=args.port,
                   host_label=host_label)
    bound_host, bound_port = server.server_address[:2]

    # flush=True throughout: stdout is block-buffered when redirected to a file
    # or a pipe, and a supervisor/tmux pane showing nothing at all reads as
    # "it failed to start".
    print("webharness: http://%s:%d  ->  %s" % (bound_host, bound_port,
                                                backend.info().target), flush=True)
    if args.listen_host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: bound to %s — this server has NO AUTH and can reset the "
              "DUT.\n         Prefer 127.0.0.1 + an SSH tunnel, or the wg0 mgmt "
              "tunnel." % args.listen_host, file=sys.stderr, flush=True)
    if args.fake:
        print("         FAKE backend: nothing shown reflects real hardware.",
              flush=True)
    print("         Ctrl-C to stop.", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
