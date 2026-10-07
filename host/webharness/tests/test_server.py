"""``webharness.server`` — the HTTP glue, over a real loopback socket.

This is the one file here that opens sockets, and they are all ``127.0.0.1``
against an in-process :class:`~webharness.backend.FakeBackend`: no board, no
network, no lease. It proves the bytes actually arrive — status lines,
``Content-Length``, the ``413`` body cap — which the pure ``handle()`` tests
cannot.
"""
from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

from webharness.backend import FakeBackend
from webharness.server import build_backend, serve


@pytest.fixture()
def live():
    backend = FakeBackend()
    server = serve(backend, listen_host="127.0.0.1", port=0, host_label="board")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % server.server_address[1]
    try:
        yield base, backend
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=5) as r:
        return r.status, r.read(), dict(r.headers)


def post_refused(base, path, payload):
    """POST ``payload`` on a bare socket; return every byte the server sent.

    A body the server REFUSES UNREAD cannot go through urllib: the 413 is
    written and the connection closed while the client is still inside
    ``sendall()``, so the send fails with EPIPE/ECONNRESET and urllib raises
    ``URLError`` before it has read the status line. That is a scheduling
    race, not a server defect -- the refused bytes are exactly what the 413
    path must never read -- and it failed 2/15 runs under CPU load on
    2026-09-10 (never on an idle box, which is how it survived two gates).
    So: send, tolerate the peer hanging up mid-body, then read what it wrote
    (Linux keeps already-received bytes readable after a RST) until EOF,
    which is also the proof that it did hang up.
    """
    host, port = base.rsplit("//", 1)[1].rsplit(":", 1)
    sock = socket.create_connection((host, int(port)), timeout=5)
    try:
        sock.sendall(
            b"POST %s HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
            b"Content-Length: %d\r\n\r\n" % (path.encode(), len(payload))
        )
        try:
            sock.sendall(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass  # refused mid-upload: the response is still in our queue
        seen = b""
        while True:
            try:
                chunk = sock.recv(65536)
            except ConnectionResetError:
                break
            if not chunk:
                break
            seen += chunk
    finally:
        sock.close()
    return seen


def post(base, path, obj):
    data = json.dumps(obj).encode()
    req = urllib.request.Request(base + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


# --------------------------------------------------------------------------- #

def test_serves_the_page(live):
    base, _ = live
    status, body, headers = get(base, "/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert body.startswith(b"<!doctype html>")
    assert int(headers["Content-Length"]) == len(body)


def test_healthz(live):
    base, _ = live
    status, body, _ = get(base, "/healthz")
    assert status == 200 and json.loads(body)["ok"] is True


def test_status_round_trip(live):
    base, _ = live
    status, body, headers = get(base, "/api/status")
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body)["rm"]["name"] == "nanosoc"


def test_hardware_state_is_never_cached_by_the_browser(live):
    base, _ = live
    _, _, headers = get(base, "/api/status")
    assert headers["Cache-Control"] == "no-store"


def test_post_reset_actually_drives_the_backend(live):
    base, backend = live
    status, body = post(base, "/api/reset", {"target": "dut"})
    assert status == 200 and json.loads(body)["ok"] is True
    assert backend.calls == [("reset", "dut")]


def test_post_clock_actually_drives_the_backend(live):
    base, backend = live
    status, body = post(base, "/api/clock", {"preset": "100mhz"})
    assert status == 200 and json.loads(body)["locked"] is True
    assert backend.calls == [("set_clk", "100mhz")]


def test_bad_preset_is_400_over_the_wire(live):
    base, backend = live
    status, body = post(base, "/api/clock", {"preset": "nope"})
    assert status == 400 and json.loads(body)["ok"] is False
    assert backend.calls == []


def test_unknown_path_is_404_over_the_wire(live):
    base, _ = live
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(base, "/does-not-exist")
    assert exc.value.code == 404


def test_oversized_body_is_refused_unread(live):
    base, backend = live
    huge = b'{"target":"' + b"d" * (128 * 1024) + b'"}'
    seen = post_refused(base, "/api/reset", huge)
    head, _, body = seen.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 413"), head[:40]
    assert json.loads(body)["ok"] is False
    assert backend.calls == []


def test_refusing_a_body_unread_closes_the_connection(live):
    """The 413 path never reads the body, so those bytes are still in the
    socket. Under HTTP/1.1 keep-alive they would be parsed as the next request
    line — the response must end the connection instead."""
    base, _ = live
    # post_refused reads to EOF, so it returns only once the server has hung
    # up (or after 5 s, which fails the test). Then assert it sent EXACTLY ONE
    # response: without the hang-up the handler loop parses the refused body's
    # leftover bytes as a request line and emits a spurious second response on
    # the same connection — the actual defect, which a plain "did it close
    # eventually" check would miss.
    seen = post_refused(base, "/api/reset", b"x" * (128 * 1024))
    assert seen.startswith(b"HTTP/1.1 413"), seen[:40]
    assert seen.count(b"HTTP/1.1 ") == 1, (
        "server emitted a second response after refusing the body: %r"
        % seen[:200]
    )
    assert b"Connection: close" in seen


def test_several_requests_share_one_connection(live):
    """HTTP/1.1 keep-alive: the page fires five API calls per refresh, and each
    must not cost a new TCP connection to the *web server* either."""
    base, _ = live
    for path in ("/healthz", "/api/status", "/api/services", "/api/clocks",
                 "/api/resets", "/api/diag"):
        status, _, _ = get(base, path)
        assert status == 200, path


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #

def test_build_backend_fake_needs_no_host():
    b = build_backend(fake=True)
    assert b.info().live is False


def test_build_backend_live_targets_the_shell_without_connecting():
    b = build_backend(fake=False, shell_host="192.168.10.101")
    info = b.info()
    assert info.mode == "shell-tcp" and info.target == "192.168.10.101"
    assert info.live is True


def test_main_requires_a_host_unless_fake():
    from webharness.server import main
    with pytest.raises(SystemExit):
        main([])
