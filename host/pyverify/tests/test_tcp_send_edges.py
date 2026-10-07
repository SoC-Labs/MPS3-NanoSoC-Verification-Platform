"""``pusher.tcp_send`` edge cases found by the Linux harness lanes.

1. **A stall limit, not a transfer limit** (IMAGE's first-install rehearsal): a
   socket timeout bounds a WHOLE ``sendall`` since Python 3.5, so a 24 MB slot
   image over a slow ssh tunnel was cut at 30 s ("push B FAILED: torn"). Now
   ``timeout_s`` bounds each 64 KiB chunk. A slow server that never stalls
   longer than that succeeds however long it takes; a stalled one still fails.
   NEGATIVE CONTROL: one ``sendall`` with the same timeout against the same slow
   server times out -- the server really is slow enough to matter.
2. **The early-close flake** (``test_slot_e2e::test_no_card_is_a_clean_decline``):
   harnessd declines a no-card push by closing; when the RST lands after a
   COMPLETE send, ``shutdown()`` raises ENOTCONN. That is "the server closed" --
   the verdict comes from the protocol (the slot job), not the half-close.
   Made deterministic here with a real RST and a wait for it before the
   half-close. NEGATIVE CONTROLS: any other half-close errno, and a send that
   could NOT complete, are still a PushError.
"""
from __future__ import annotations

import errno
import select
import socket
import struct
import threading
import time

import pytest

from pyverify import pusher
from pyverify.pusher import PushError, tcp_send


def _small_sndbuf(monkeypatch, wrap=None):
    real = socket.create_connection

    def connect(*a, **kw):
        s = real(*a, **kw)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        return wrap(s) if wrap else s

    monkeypatch.setattr(socket, "create_connection", connect)


class _Server(threading.Thread):
    """One connection. ``mode``: 'slow' (read `step` bytes per `gap` s), 'stall'
    (never read), 'rst_after_all' (read everything, then close with an RST),
    'rst_after_header' (read 24 bytes, then RST)."""

    def __init__(self, mode, *, total=0, step=32 * 1024, gap=0.1):
        super().__init__(daemon=True)
        self.mode, self.total, self.step, self.gap = mode, total, step, gap
        self.ls = socket.socket()
        # an explicit SO_RCVBUF turns autotuning off, so the kernel cannot swallow
        # the payload; 'slow' needs room for one `step` per read
        self.ls.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, step if mode == "slow" else 4096)
        self.ls.bind(("127.0.0.1", 0))
        self.ls.listen(1)
        self.port = self.ls.getsockname()[1]
        self.got = 0
        self.closed = threading.Event()
        self.release = threading.Event()

    def run(self):
        c, _ = self.ls.accept()
        try:
            if self.mode == "stall":
                self.release.wait(30)
                return
            if self.mode == "slow":
                while True:
                    d = c.recv(self.step)
                    if not d:
                        return
                    self.got += len(d)
                    time.sleep(self.gap)
            want = self.total if self.mode == "rst_after_all" else 24
            while self.got < want:
                d = c.recv(65536)
                if not d:
                    break
                self.got += len(d)
            c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        finally:
            c.close()
            self.ls.close()
            self.closed.set()


# --------------------------------------------------------------------------- #
# 1. per-chunk (stall) timeout
# --------------------------------------------------------------------------- #

def test_a_slow_server_that_never_stalls_succeeds_past_the_timeout(monkeypatch):
    _small_sndbuf(monkeypatch)
    data = b"\x5a" * (1024 * 1024)                  # ~3.2 s at 320 KiB/s; timeout 1.5 s
    srv = _Server("slow")
    srv.start()
    t0 = time.monotonic()
    assert tcp_send(data, "127.0.0.1", srv.port, timeout_s=1.5) == len(data)
    took = time.monotonic() - t0
    srv.join(10)
    assert srv.got == len(data) and took > 1.5, took


def test_negative_control_one_whole_sendall_is_cut_by_the_same_timeout(monkeypatch):
    srv = _Server("slow")
    srv.start()
    s = socket.create_connection(("127.0.0.1", srv.port), timeout=1.5)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    with pytest.raises(socket.timeout):
        s.sendall(b"\x5a" * (1024 * 1024))           # the pre-fix shape
    s.close()


def test_a_truly_stalled_server_fails(monkeypatch):
    _small_sndbuf(monkeypatch)
    srv = _Server("stall")
    srv.start()
    t0 = time.monotonic()
    with pytest.raises(PushError, match="timed out"):
        tcp_send(b"\x5a" * (1024 * 1024), "127.0.0.1", srv.port, timeout_s=0.5)
    assert time.monotonic() - t0 < 5
    srv.release.set()


# --------------------------------------------------------------------------- #
# 2. the early close after a complete send
# --------------------------------------------------------------------------- #

class _WaitForRst:
    """A real socket whose shutdown() first waits until the server's RST has
    arrived (readable), so ENOTCONN is certain rather than a race."""

    def __init__(self, sock, srv):
        self._s, self._srv = sock, srv
        self.shutdown_errno = None

    def __getattr__(self, name):
        return getattr(self._s, name)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._s.close()

    def shutdown(self, how):
        assert self._srv.closed.wait(10)
        select.select([self._s], [], [], 5)
        try:
            return self._s.shutdown(how)
        except OSError as exc:
            self.shutdown_errno = exc.errno
            raise


def test_a_server_that_closes_after_the_whole_frame_is_not_a_failure(monkeypatch):
    data = b"\x33" * 2048
    srv = _Server("rst_after_all", total=len(data))
    srv.start()
    wrapped = []

    def wrap(s):
        w = _WaitForRst(s, srv)
        wrapped.append(w)
        return w

    _small_sndbuf(monkeypatch, wrap)
    assert tcp_send(data, "127.0.0.1", srv.port, timeout_s=5) == len(data)
    assert srv.got == len(data)
    # it really was the ENOTCONN/ECONNRESET case the flake hit
    assert wrapped[0].shutdown_errno in (errno.ENOTCONN, errno.ECONNRESET)


@pytest.mark.parametrize("err", [errno.EBADF, errno.EIO])
def test_negative_control_another_half_close_error_is_still_a_push_error(monkeypatch, err):
    class _Sock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def sendall(self, b):
            pass

        def shutdown(self, how):
            raise OSError(err, "boom")

    monkeypatch.setattr(pusher.socket, "create_connection", lambda *a, **kw: _Sock())
    with pytest.raises(PushError, match="boom"):
        tcp_send(b"x" * 100, "h", 1)


def test_negative_control_a_send_that_cannot_complete_is_a_push_error(monkeypatch):
    _small_sndbuf(monkeypatch)
    srv = _Server("rst_after_header")
    srv.start()
    with pytest.raises(PushError):
        tcp_send(b"\x44" * (2 * 1024 * 1024), "127.0.0.1", srv.port, timeout_s=5)


def test_slot_push_passes_its_stall_timeout_to_the_send(monkeypatch):
    from pyverify import cli as cli_mod
    from pyverify import slot as pslot
    seen = {}
    monkeypatch.setattr(pslot, "tcp_send", lambda frame, host, port, timeout_s: seen.update(
        timeout_s=timeout_s) or len(frame))
    monkeypatch.setattr(pslot, "image_info", lambda image: {"hdr_crc": 0})
    monkeypatch.setattr(pslot, "frame_slot_image", lambda image, **kw: image)
    pslot.push_slot_image(b"img", "h", static_id=1, timeout_s=7.5)
    assert seen == {"timeout_s": 7.5}
    a = cli_mod.build_parser().parse_args(["slot", "push", "x.img", "--push-timeout", "90"])
    assert a.push_timeout == 90.0 and a.timeout == 3600.0
    # the silicon defaults (29 Sep): a 30 s stall limit tore two pushes at ~27 MB
    assert cli_mod.build_parser().parse_args(["slot", "push", "x.img"]).push_timeout == 600.0
