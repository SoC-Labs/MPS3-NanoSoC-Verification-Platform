"""``pyverify.display_http`` — a THIN HTTP convenience shim over the CLCD-KVM
``display`` verb, for flipping the on-board panel from a browser or ``curl``.

This is **not** a server on the board. It runs on the **hub host**
(<hub-host>), exactly like ``pusher``/``console``/``pyverify`` itself — the
same model the rest of the host stack uses. It is a paper-thin wrapper: every
request opens a short-lived :class:`pyverify.client.ShellClient` to the shell's
existing TCP-6900 control channel, calls the real
:meth:`~pyverify.client.ShellClient.display` / ``display_owner`` method, and
returns its JSON. There is deliberately no state, no auth, no session — the
real API is pyverify; this is a keyboard-shortcut over it.

Routes::

    GET  /display              -> query the current committed owner (read-only)
    POST /display/harness      -> flip the panel to the harness
    POST /display/dut          -> flip the panel to the DUT
    POST /display/toggle       -> toggle the requested owner (like USER_nPB[1])

Every route answers ``{"ok":bool,"owner":str,"err":str}`` (HTTP 200 when the
shell accepted the verb, 400 for an unknown owner in the path, 502 when the
control channel is unreachable, 404 for any other path).

Run it (on <hub-host>, pointing at the board's shell)::

    python -m pyverify.display_http --shell-host 192.168.10.101 --port 8080

    # then, from anywhere that can reach the hub:
    curl -X POST http://<hub-host>:8080/display/dut
    curl http://<hub-host>:8080/display

Stdlib only (``http.server``) — no Flask, nothing vendored. The routing core
(:func:`handle_display`) is a pure function taking a ``client_factory`` seam, so
it is unit-tested with a fake client and no sockets (see
``tests/test_display.py``).
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional, Tuple

from .client import DISPLAY_OWNERS, ShellClient

__all__ = ["handle_display", "make_handler_class", "serve", "main"]

#: ``() -> ShellClient``-ish: builds a connected-on-``with`` control client.
ClientFactory = Callable[[], Any]


def handle_display(
    method: str,
    path: str,
    client_factory: ClientFactory,
) -> Tuple[int, dict]:
    """Route one HTTP request to the ``display`` verb. Pure: no sockets of its
    own — it calls ``client_factory()`` and drives the returned client under a
    ``with`` block (connect on enter, close on exit).

    Returns ``(http_status, body_dict)``. ``body_dict`` is always the
    ``{"ok","owner","err"}`` shape (even for shim-level 400/404/502 errors, so a
    caller can parse one shape).
    """
    if method == "GET" and path == "/display":
        return _run(client_factory, owner=None)

    if method == "POST" and path.startswith("/display/"):
        owner = path[len("/display/"):]
        if owner not in DISPLAY_OWNERS:
            return 400, {
                "ok": False, "owner": "",
                "err": f"unknown owner {owner!r} in path (want one of "
                       f"{', '.join(DISPLAY_OWNERS)})",
            }
        return _run(client_factory, owner=owner)

    return 404, {
        "ok": False, "owner": "",
        "err": f"no route for {method} {path} (try GET /display or "
               f"POST /display/{{{'|'.join(DISPLAY_OWNERS)}}})",
    }


def _run(client_factory: ClientFactory, *, owner: Optional[str]) -> Tuple[int, dict]:
    try:
        with client_factory() as client:
            resp = client.display_owner() if owner is None else client.display(owner)
    except OSError as exc:
        # Control channel unreachable (connection refused/timeout/reset).
        return 502, {"ok": False, "owner": "", "err": f"shell unreachable: {exc}"}
    return 200, {"ok": resp.ok, "owner": resp.owner, "err": resp.err}


def make_handler_class(client_factory: ClientFactory) -> type:
    """Build a :class:`BaseHTTPRequestHandler` subclass bound to
    ``client_factory``. Every GET/POST defers to :func:`handle_display`."""

    class _DisplayHandler(BaseHTTPRequestHandler):
        server_version = "pyverify-display-http/1.0"

        def _dispatch(self) -> None:
            status, body = handle_display(self.command, self.path, client_factory)
            payload = json.dumps(body).encode("ascii") + b"\n"
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:   # noqa: N802 (http.server naming)
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def log_message(self, *args: Any) -> None:  # keep the shim quiet
            pass

    return _DisplayHandler


def serve(
    shell_host: str,
    *,
    shell_port: int = 6900,
    listen_host: str = "127.0.0.1",
    port: int = 8080,
    timeout: float = 5.0,
) -> ThreadingHTTPServer:
    """Build (but do not block on) a threading HTTP server for the shim. The
    caller runs ``.serve_forever()`` (or, in tests, drives it and calls
    ``.shutdown()``). The bound port is ``server.server_address[1]`` — pass
    ``port=0`` for an OS-assigned one."""
    def factory() -> ShellClient:
        return ShellClient(shell_host, port=shell_port, timeout=timeout)

    return ThreadingHTTPServer((listen_host, port), make_handler_class(factory))


def main(argv: "Optional[list[str]]" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pyverify.display_http", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--shell-host", required=True,
                        help="board shell host/IP (net-protocol.md control channel)")
    parser.add_argument("--shell-port", type=int, default=6900)
    parser.add_argument("--listen-host", default="127.0.0.1",
                        help="interface for the shim to bind (default localhost; "
                             "use 0.0.0.0 to expose it on the hub)")
    parser.add_argument("--port", type=int, default=8080, help="shim HTTP port")
    args = parser.parse_args(argv)

    server = serve(
        args.shell_host, shell_port=args.shell_port,
        listen_host=args.listen_host, port=args.port,
    )
    host, port = server.server_address[:2]
    print(f"pyverify.display_http: serving on http://{host}:{port} -> shell "
          f"{args.shell_host}:{args.shell_port}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
