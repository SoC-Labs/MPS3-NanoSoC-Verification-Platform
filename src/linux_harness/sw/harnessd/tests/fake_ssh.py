#!/usr/bin/env python3
"""fake_ssh.py — stands in for `ssh -N -L 127.0.0.1:L:127.0.0.1:R ... target` in
the slot-lock tests (test_slot_lock_e2e.py), via $MPS3_SSH.

A real tunnel's far end connects to the board's services FROM THE BOARD ITSELF
(127.0.0.1). On a host test every peer is 127.x, so the harness under test is
started with --mock-trusted-peer 127.0.0.3 (only that address counts as "the
board"), and this forwarder connects from $FAKE_SSH_SOURCE (default 127.0.0.3).
Everything else about the tunnel -- the -L parsing, the local listeners, the
bytes both ways -- is what pyverify.slot.SshTunnel really drives. Runs until
SIGTERM. Stdlib only.
"""
import os
import signal
import socket
import sys
import threading


def pipe(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except OSError:
        pass
    finally:
        try:
            b.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def serve(lhost, lport, rhost, rport, src):
    ls = socket.socket()
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.bind((lhost, lport))
    ls.listen(16)
    while True:
        c, _ = ls.accept()
        try:
            r = socket.create_connection((rhost, rport), source_address=(src, 0))
        except OSError:
            c.close()
            continue
        threading.Thread(target=pipe, args=(c, r), daemon=True).start()
        threading.Thread(target=pipe, args=(r, c), daemon=True).start()


def main():
    args = sys.argv[1:]
    src = os.environ.get("FAKE_SSH_SOURCE", "127.0.0.3")
    fwd = []
    i = 0
    while i < len(args):
        if args[i] == "-L":
            lh, lp, rh, rp = args[i + 1].split(":")
            fwd.append((lh, int(lp), rh, int(rp)))
            i += 2
        elif args[i] == "-o":
            i += 2
        else:
            i += 1
    if not fwd:
        sys.exit("fake_ssh: no -L")
    signal.signal(signal.SIGTERM, lambda *a: os._exit(0))
    for f in fwd:
        threading.Thread(target=serve, args=(*f, src), daemon=True).start()
    signal.pause()


if __name__ == "__main__":
    main()
