#!/usr/bin/env python3
"""Tier-3 console gate: prove the DUT's UART reaches the host over TCP 6930.

Two modes, both raw byte streams on the shell's uart_over_eth port (net-protocol.md
6930 = UART0 boot monitor):

  * ECHO round-trip (default, for rm_uart_echo) -- send a known probe and assert
    it comes BACK on the same socket. This is the on-silicon twin of the
    ``uart_echo_integration`` cocotb bench: it exercises the DUT -> uart_bridge
    async-FIFO -> CSR -> uart_over_eth -> TCP join end to end.

  * BANNER (``--send`` omitted, i.e. ``--min-bytes N``) -- assert the DUT emits
    at least N bytes unprompted, e.g. a nanoSoC bootrom/hello banner. (nanoSoC
    UART RX into the core is a known upstream TX-only gap, so a *round-trip* is
    not available there -- assert the TX heartbeat instead.)

Standalone + dependency-free (raw socket, no pyverify) so harness_regression can
feed it to the fpgahub host over stdin exactly like ping_check.py:

    ssh <hub-host> python3 - 192.168.10.101 --send MPS3-ECHO < console_check.py

or run it locally when already on the hub:

    python3 console_check.py 192.168.10.101 --send MPS3-ECHO

Exit 0 on success (probe echoed / enough bytes seen), non-zero otherwise.
PRECONDITION: the matching RM is already resident (harness_regression swaps
rm_uart_echo in via swap_check.py immediately before this gate).
"""
import argparse
import socket
import sys
import time


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board", nargs="?", default="192.168.10.101")
    ap.add_argument("--port", type=int, default=6930)
    ap.add_argument("--send", default=None,
                    help="probe string to write; if set, it must echo back")
    ap.add_argument("--expect", default=None,
                    help="substring that must appear in the reply "
                         "(defaults to --send in echo mode)")
    ap.add_argument("--min-bytes", type=int, default=1,
                    help="banner mode (no --send): minimum bytes the DUT must emit")
    ap.add_argument("--timeout", type=float, default=8.0)
    a = ap.parse_args(argv)

    expect = a.expect if a.expect is not None else a.send
    mode = "echo(%r)" % a.send if a.send is not None else "banner(>=%dB)" % a.min_bytes
    print("== Tier-3 console gate: %s:%d %s ==" % (a.board, a.port, mode), flush=True)

    deadline = time.time() + a.timeout
    buf = b""
    try:
        s = socket.create_connection((a.board, a.port), timeout=a.timeout)
        s.settimeout(a.timeout)
        if a.send is not None:
            s.sendall(a.send.encode())
        while time.time() < deadline:
            try:
                chunk = s.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            if expect is not None and expect.encode() in buf:
                break
            if a.send is None and len(buf) >= a.min_bytes:
                break
        s.close()
    except OSError as e:  # connection refused (off-hub / no shell), etc.
        print("FAIL: console connect/read error: %r" % e)
        return 1

    shown = buf[:120].decode("latin1").replace("\n", "\\n").replace("\r", "\\r")
    print("   received %d byte(s): %r%s"
          % (len(buf), shown, " ..." if len(buf) > 120 else ""))

    if expect is not None:
        if expect.encode() in buf:
            print("\nOK: probe %r echoed back on 6930 (DUT->host UART join live)."
                  % a.send)
            return 0
        print("\nFAIL: probe %r was NOT echoed back within %.0fs -- the DUT<->harness "
              "UART join did not round-trip." % (a.send, a.timeout))
        return 1

    if len(buf) >= a.min_bytes:
        print("\nOK: DUT emitted %d byte(s) on 6930 (>= %d; UART TX heartbeat live)."
              % (len(buf), a.min_bytes))
        return 0
    print("\nFAIL: DUT emitted only %d byte(s) on 6930 (< %d) within %.0fs -- silent "
          "console." % (len(buf), a.min_bytes, a.timeout))
    return 1


if __name__ == "__main__":
    sys.exit(main())
