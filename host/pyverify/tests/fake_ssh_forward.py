#!/usr/bin/env python3
"""fake_ssh_forward.py -- stands in for ``ssh ... -N -L 127.0.0.1:L:127.0.0.1:R ... target``
in test_ssh_tunnel.py (via ``SshTunnel(ssh=...)``), INCLUDING the ControlMaster
behaviour behind ILA-mint finding #20.

* Every argv is appended as one JSON line to ``$FAKE_SSH_LOG`` (when set).
* ``$FAKE_SSH_MASTER_DIR`` set = "the user's ssh config has a ControlMaster for
  this host". Then, unless the command line turns it off (``-o ControlPath=none``
  or ``-o ControlMaster=no``), the forwards are handed to a MASTER: a detached
  process that binds and serves them and OUTLIVES this client (its pid goes to
  ``<dir>/master.pid``). That is what a real master does with a mux client's
  ``-L``: the forward stays bound after the ``ssh -N`` exits.
* Otherwise the forwards live in this process and die with it (SIGTERM).
  ``-o ExitOnForwardFailure=yes`` + a port that cannot be bound = exit 255.
* ``$FAKE_SSH_LATE`` = ``"<R>=<secs>[,...]"``: the forward to remote port R is
  bound that many seconds AFTER the others -- a forwarder that brings its
  listeners up independently (as the harnessd e2e fake does, one thread each),
  which is what made the slot-lock push flake under load.

Each accepted connection is piped to 127.0.0.1:R. Stdlib only.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time


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


def serve(ls, rport):
    while True:
        try:
            c, _ = ls.accept()
        except OSError:
            return
        try:
            r = socket.create_connection(("127.0.0.1", rport), 5)
        except OSError:
            c.close()
            continue
        threading.Thread(target=pipe, args=(c, r), daemon=True).start()
        threading.Thread(target=pipe, args=(r, c), daemon=True).start()


def parse(args):
    opts, fwd, i = {}, [], 0
    while i < len(args):
        a = args[i]
        if a == "-o":
            k, _, v = args[i + 1].partition("=")
            opts.setdefault(k, v)                     # ssh: the first value wins
            i += 2
        elif a == "-L":
            parts = args[i + 1].split(":")
            fwd.append((int(parts[-3]), int(parts[-1])))
            i += 2
        else:
            i += 1
    return opts, fwd


def listen_on(lport, exit_on_failure):
    ls = socket.socket()
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        ls.bind(("127.0.0.1", lport))
    except OSError as exc:
        sys.stderr.write("bind [127.0.0.1]:%d: %s\n" % (lport, exc))
        if exit_on_failure:
            sys.stderr.write("Could not request local forwarding.\n")
            sys.stderr.flush()
            os._exit(255)
        return None
    ls.listen(16)
    return ls


def late_forward(lport, rport, delay, exit_on_failure):
    time.sleep(delay)
    ls = listen_on(lport, exit_on_failure)
    if ls is not None:
        serve(ls, rport)


def run_forwards(fwd, *, exit_on_failure):
    late = {int(r): float(s) for r, _, s in
            (x.partition("=") for x in os.environ.get("FAKE_SSH_LATE", "").split(",") if x)}
    socks = []
    for lport, rport in fwd:
        if rport in late:
            threading.Thread(target=late_forward, args=(lport, rport, late[rport], exit_on_failure),
                             daemon=True).start()
            continue
        ls = listen_on(lport, exit_on_failure)
        if ls is not None:
            socks.append((ls, rport))
    for ls, rport in socks:
        threading.Thread(target=serve, args=(ls, rport), daemon=True).start()


def main():
    args = sys.argv[1:]
    if args and args[0] == "--master":          # the detached "ControlMaster"
        fwd = [tuple(x) for x in json.loads(args[1])]
        run_forwards(fwd, exit_on_failure=False)
        signal.signal(signal.SIGTERM, lambda *a: os._exit(0))
        tmp = args[2] + ".tmp"
        with open(tmp, "w") as f:
            f.write(str(os.getpid()))
        os.replace(tmp, args[2])                 # ready: the forwards are bound
        while True:
            time.sleep(3600)
    log = os.environ.get("FAKE_SSH_LOG")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps(args) + "\n")
    opts, fwd = parse(args)
    if not fwd:
        sys.exit("fake_ssh_forward: no -L")
    signal.signal(signal.SIGTERM, lambda *a: os._exit(0))
    master_dir = os.environ.get("FAKE_SSH_MASTER_DIR")
    if master_dir and opts.get("ControlPath") != "none" and opts.get("ControlMaster") != "no":
        pidfile = os.path.join(master_dir, "master.pid")
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--master", json.dumps(fwd),
                          pidfile], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        while not os.path.exists(pidfile):
            time.sleep(0.02)
    else:
        run_forwards(fwd, exit_on_failure=opts.get("ExitOnForwardFailure") == "yes")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
