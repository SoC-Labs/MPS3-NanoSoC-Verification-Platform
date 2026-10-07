#!/usr/bin/env python3
"""fake_openocd.py -- a stand-in for OpenOCD 0.12 that mps3-debug can supervise on a
host: it parses the argv mps3-debug builds, dials remote_bitbang like the real adapter,
reads the IDCODE through it (the peer answers 'R' samples), and logs the lines the
launcher parses, in OpenOCD's own words:

  Info : Connecting to 127.0.0.1:<port>                       (remote_bitbang.c)
  Error: Error on socket 'remote_bitbang_fill_buf': ...       (a refused/reset 6921)
  Info : JTAG tap: nanosoc.cpu tap/device found: 0x6ba00477 ...
  Info : Listening on port N for gdb connections              (server.c, per core)
  Info : accepting 'gdb' connection on tcp/N / Info : dropped 'gdb' connection

It writes its argv (one per line) to $FAKE_OCD_ARGV when set, and exits 1 on its own
when the file $FAKE_OCD_CRASH appears (a crash after "up").
"""
import os
import select
import signal
import socket
import sys
import time


def log(line):
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


def main():
    argv = sys.argv[1:]
    if os.environ.get("FAKE_OCD_ARGV"):
        with open(os.environ["FAKE_OCD_ARGV"], "w") as f:
            f.write("\n".join(argv) + "\n")
    cmds = [argv[i + 1] for i, a in enumerate(argv) if a == "-c" and i + 1 < len(argv)]
    rbb_port, gdb, telnet, tcl, bind = 6921, 3333, 4444, 6666, None
    cores = 0
    for c in cmds:
        if c.startswith("set RBB_PORT "):
            rbb_port = int(c.split()[2])
        if "configure -event gdb-attach" in c:
            cores += 1
        if c.startswith("gdb_port "):
            for part in c.split(";"):
                k, v = part.split()
                if k == "gdb_port":
                    gdb = int(v)
                elif k == "telnet_port":
                    telnet = int(v)
                elif k == "tcl_port":
                    tcl = int(v)
                elif k == "bindto":
                    bind = v
    cores = max(cores, 1)
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    log("Open On-Chip Debugger 0.12.0+fake")
    log("Info : Connecting to 127.0.0.1:%d" % rbb_port)
    try:
        rbb = socket.create_connection(("127.0.0.1", rbb_port), timeout=5)
    except OSError as e:
        log("Error: Error on socket 'Failed to connect': %s." % e.strerror)
        return 1
    # 5 x TMS=1 (TLR selects IDCODE), then 32 samples -- the shape of a scan
    try:
        rbb.sendall(b"R" * 32)
        got = b""
        while len(got) < 32:
            chunk = rbb.recv(64)
            if not chunk:
                raise ConnectionResetError(104, "Connection reset by peer")
            got += chunk
    except OSError as e:
        log("Error: Error on socket 'remote_bitbang_fill_buf': %s." % (e.strerror or e))
        return 1
    if any(ch not in b"01" for ch in got[:32]):
        log("Error: remote_bitbang: invalid read response: %c(%d)" % (got[0], got[0]))
        return 1
    idcode = int(got[:32][::-1].decode(), 2)
    log("Info : JTAG tap: nanosoc.cpu tap/device found: 0x%08x (mfg: 0x23b (ARM Ltd), "
        "part: 0xba00, ver: 0x6)" % idcode)
    lsocks = []
    for port in [telnet, tcl] + [gdb + i for i in range(cores)]:
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((bind or "0.0.0.0", port))
        except OSError as e:
            what = "gdb" if port >= gdb and port < gdb + cores else "telnet"
            log("Error: couldn't bind %s to socket on port %d: %s" % (what, port, e.strerror))
            return 1
        s.listen(4)
        lsocks.append((s, port))
    for i in range(cores):
        log("Info : Listening on port %d for gdb connections" % (gdb + i))
    clients = {}
    crash = os.environ.get("FAKE_OCD_CRASH")
    while True:
        if crash and os.path.exists(crash):
            log("Error: fake crash requested")
            return 1
        rl = [s for s, _ in lsocks] + list(clients) + [rbb]
        r, _, _ = select.select(rl, [], [], 0.1)
        for s in r:
            if s is rbb:
                if not rbb.recv(64):
                    log("Error: Error on socket 'remote_bitbang_fill_buf': Connection reset by peer.")
                    return 1
                continue
            match = [p for ls, p in lsocks if ls is s]
            if match:
                c, _ = s.accept()
                svc = "gdb" if gdb <= match[0] < gdb + cores else (
                    "telnet" if match[0] == telnet else "tcl")
                clients[c] = svc
                log("Info : accepting '%s' connection on tcp/%d" % (svc, match[0]))
            else:
                if not s.recv(64):
                    log("Info : dropped '%s' connection" % clients.pop(s))
                    s.close()


if __name__ == "__main__":
    sys.exit(main())
