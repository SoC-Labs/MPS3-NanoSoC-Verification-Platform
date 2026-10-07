#!/usr/bin/env python3
"""mps3_console.py — char-paced serial console driver for the MPS3 uartlite.

WHY THIS EXISTS
---------------
The MPS3 harness console (`/dev/ttyUSB6` on <hub-host> @115200, `console=ttyUL0`,
uartlite @0x40600000) has **no RX flow control and a shallow RX FIFO**. Writing a
command at line rate overruns that FIFO and the board sees a mangled command —
which reads exactly like a wiring or baud fault, so it burns board time chasing a
phantom. Every board session therefore needs a CHAR-PACED writer.

Prior art this replaces:
  * The working paced writer only ever existed in a session scratchpad (ephemeral,
    lost) — `docs/HANDOVER_WIREGUARD.md` §6 records that as a known trap.
  * `src/linux_soc/hw/board_scripts/stable_login_proof.py` (linux worktree, UNTRACKED)
    embeds a one-off `slow()` at 90 ms/char inside a single-purpose login proof.
This module is the committed, reusable, dependency-free version of that pattern.

DESIGN NOTES
------------
* **stdlib only** (termios/tty/os/select) — deliberately no pyserial, so it runs on
  any lab host without an install step.
* Pacing default 4 ms/char (`MPS3_CONSOLE_PACE`). That is the handover's figure;
  it is a *ceiling on throughput*, not a tuned constant — raise it if a target
  still mangles input, since overrun is silent and asymmetric (too slow only
  wastes seconds, too fast corrupts the run).
* `expect()` matches against a rolling buffer of everything received since the
  call, so output that arrives *while* a command is still being paced out is not
  lost (a naive read-after-write races the DUT).
* Board-free self-test (`--self-test`) drives a pty loopback, asserting both the
  expect logic and that pacing actually took >= n*pace seconds. It carries a
  negative control (junk input must NOT produce the success marker), so a device
  that answered unconditionally could not fake a pass.
* **Mutation-verified 2026-07-24:** replacing the two `time.sleep(self.pace_s)`
  calls in `send()` with `pass` turns the self-test red on the pacing assertion
  only (`pacing too fast: 0.000s < 0.060s`) while the other three still pass —
  i.e. the pacing guard is load-bearing and specific, not a tautology.

USAGE
-----
    # assert a shell is alive
    scripts/mps3_console.py --dev /dev/ttyUSB6 \
        --send 'echo MPS3_MARK_$((6*7))' --expect 'MPS3_MARK_42'

    # multiple ordered steps (each --send may be followed by its own --expect)
    scripts/mps3_console.py --dev /dev/ttyUSB6 \
        --send 'root' --expect 'Password:' \
        --send 'soclabs' --expect '#' \
        --send 'wg show' --expect 'interface: wg0'

    # board-free proof that the driver itself works
    scripts/mps3_console.py --self-test

Exit status: 0 = every --expect matched in order; 1 = a timeout/mismatch; 2 = usage
or device error. Intended to be called from a session script whose RESULT.txt
records the exit code.
"""

from __future__ import annotations

import argparse
import os
import re
import select
import sys
import termios
import time

DEFAULT_PACE_S = float(os.environ.get("MPS3_CONSOLE_PACE", "0.004"))
DEFAULT_BAUD = int(os.environ.get("MPS3_CONSOLE_BAUD", "115200"))
DEFAULT_TIMEOUT_S = float(os.environ.get("MPS3_CONSOLE_TIMEOUT", "15"))

_BAUD_CONST = {
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
}


class ConsoleTimeout(RuntimeError):
    """An expect() pattern did not appear before its deadline."""


class Console:
    """A char-paced, expect-capable serial console.

    `fd` may be any file descriptor; `raw_115200()` is applied only when the fd is
    a real tty (the pty self-test path is already raw).
    """

    def __init__(self, fd: int, pace_s: float = DEFAULT_PACE_S, log=None):
        self.fd = fd
        self.pace_s = pace_s
        self.log = log
        self._buf = ""

    # -- setup ------------------------------------------------------------
    @classmethod
    def open(cls, dev: str, baud: int = DEFAULT_BAUD, pace_s: float = DEFAULT_PACE_S,
             log=None) -> "Console":
        # O_NOCTTY: never let the board console become this process's controlling
        # terminal (a stray SIGHUP would otherwise kill the session script).
        fd = os.open(dev, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        c = cls(fd, pace_s=pace_s, log=log)
        c.raw(baud)
        return c

    def raw(self, baud: int = DEFAULT_BAUD) -> None:
        """Put the tty in raw 8N1 at `baud`, no flow control."""
        if not os.isatty(self.fd):
            return
        if baud not in _BAUD_CONST:
            raise ValueError(f"unsupported baud {baud}; known: {sorted(_BAUD_CONST)}")
        iflag, oflag, cflag, lflag, ispeed, ospeed, cc = termios.tcgetattr(self.fd)
        # cfmakeraw equivalent: no canonical mode, no echo, no signal chars, no
        # CR/LF translation, no parity/xon-xoff mangling of the byte stream.
        iflag &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP
                   | termios.INLCR | termios.IGNCR | termios.ICRNL | termios.IXON)
        oflag &= ~termios.OPOST
        lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG
                   | termios.IEXTEN)
        cflag &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB)
        cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL  # CLOCAL: ignore modem lines
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0
        speed = _BAUD_CONST[baud]
        termios.tcsetattr(self.fd, termios.TCSANOW,
                          [iflag, oflag, cflag, lflag, speed, speed, cc])

    # -- io ---------------------------------------------------------------
    def _emit(self, text: str) -> None:
        if self.log:
            self.log.write(text)
            self.log.flush()

    def drain(self, seconds: float = 0.2) -> str:
        """Absorb whatever is already pending (banners, boot spew) for `seconds`."""
        return self._pump(deadline=time.monotonic() + seconds, pattern=None)

    def send(self, text: str, newline: str = "\r") -> None:
        """Write `text` one byte at a time, `pace_s` apart, then `newline`.

        Byte-at-a-time is the whole point: a single write() of the same string is
        what overruns the uartlite RX FIFO.
        """
        for ch in text:
            os.write(self.fd, ch.encode())
            time.sleep(self.pace_s)
        if newline:
            os.write(self.fd, newline.encode())
            time.sleep(self.pace_s)
        self._emit(f"\n[send] {text!r}\n")

    def expect(self, pattern: str, timeout: float = DEFAULT_TIMEOUT_S) -> str:
        """Read until `pattern` (regex) matches the rolling buffer, or raise."""
        rx = re.compile(pattern)
        if rx.search(self._buf):
            return self._consume(rx)
        deadline = time.monotonic() + timeout
        self._pump(deadline=deadline, pattern=rx)
        if rx.search(self._buf):
            return self._consume(rx)
        tail = self._buf[-400:]
        raise ConsoleTimeout(
            f"timed out after {timeout}s waiting for {pattern!r}; last 400 bytes:\n{tail}"
        )

    def _consume(self, rx: re.Pattern) -> str:
        m = rx.search(self._buf)
        matched = self._buf[: m.end()]
        self._buf = self._buf[m.end():]  # keep anything after the match
        return matched

    def _pump(self, deadline: float, pattern) -> str:
        """Read into the rolling buffer until `pattern` matches or `deadline`."""
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            r, _, _ = select.select([self.fd], [], [], min(0.2, remaining))
            if not r:
                continue
            try:
                chunk = os.read(self.fd, 4096)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                break  # device went away (board reset / lease expiry)
            if not chunk:
                continue
            text = chunk.decode("utf-8", errors="replace")
            self._buf += text
            self._emit(text)
            if pattern is not None and pattern.search(self._buf):
                break
        return self._buf

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


# -- board-free self-test -------------------------------------------------
def _self_test() -> int:
    """Drive a pty loopback: proves expect logic AND that pacing really paces."""
    import threading

    controller, peripheral = os.openpty()
    for fd in (controller, peripheral):
        attrs = termios.tcgetattr(fd)
        attrs[3] &= ~termios.ECHO  # lflag: no echo, or we match our own writes
        termios.tcsetattr(fd, termios.TCSANOW, attrs)

    stop = threading.Event()

    def fake_device():
        """Minimal DUT: on a full line, reply with a deterministic marker."""
        buf = ""
        while not stop.is_set():
            r, _, _ = select.select([peripheral], [], [], 0.1)
            if not r:
                continue
            try:
                data = os.read(peripheral, 1024).decode("utf-8", "replace")
            except OSError:
                return
            for ch in data:
                if ch in "\r\n":
                    if buf.strip() == "hello":
                        os.write(peripheral, b"WORLD_42\r\n")
                    elif buf.strip():
                        os.write(peripheral, b"unknown\r\n")
                    buf = ""
                else:
                    buf += ch

    t = threading.Thread(target=fake_device, daemon=True)
    t.start()

    failures = []
    pace = 0.01
    con = Console(controller, pace_s=pace)
    try:
        # 1) send/expect round trip
        t0 = time.monotonic()
        con.send("hello")
        elapsed = time.monotonic() - t0
        try:
            con.expect(r"WORLD_42", timeout=5)
            print("  [PASS] send/expect round trip")
        except ConsoleTimeout as e:
            failures.append(f"round trip: {e}")
            print("  [FAIL] send/expect round trip")

        # 2) pacing actually paced: 5 chars + newline => >= 6*pace
        want = 6 * pace
        if elapsed >= want * 0.9:
            print(f"  [PASS] pacing honoured ({elapsed:.3f}s >= ~{want:.3f}s)")
        else:
            failures.append(f"pacing too fast: {elapsed:.3f}s < {want:.3f}s")
            print(f"  [FAIL] pacing too fast ({elapsed:.3f}s < {want:.3f}s)")

        # 3) a pattern that never arrives must raise, not hang forever
        t0 = time.monotonic()
        try:
            con.expect(r"NEVER_APPEARS_XYZ", timeout=1.0)
            failures.append("missing pattern did not raise")
            print("  [FAIL] timeout path did not raise")
        except ConsoleTimeout:
            took = time.monotonic() - t0
            if took < 3.0:
                print(f"  [PASS] timeout path raises promptly ({took:.2f}s)")
            else:
                failures.append(f"timeout overshot: {took:.2f}s")
                print(f"  [FAIL] timeout overshot ({took:.2f}s)")

        # 4) negative control: the fake device must NOT answer WORLD_42 to junk.
        #    Without this, test 1 could pass against a device that says WORLD_42
        #    unconditionally — the assertion would prove nothing.
        con.send("goodbye")
        try:
            con.expect(r"unknown", timeout=5)
            print("  [PASS] negative control (junk -> 'unknown', not WORLD_42)")
        except ConsoleTimeout as e:
            failures.append(f"negative control: {e}")
            print("  [FAIL] negative control")
    finally:
        stop.set()
        con.close()
        try:
            os.close(peripheral)
        except OSError:
            pass

    print()
    if failures:
        print(f"SELF-TEST FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("SELF-TEST PASSED (4/4) — driver verified board-free")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Char-paced MPS3 serial console driver (send/expect).",
        epilog="--send and --expect are ORDERED and interleaved: each --expect "
               "applies to the output following the --send before it.",
    )
    p.add_argument("--dev", help="serial device, e.g. /dev/ttyUSB6")
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    p.add_argument("--pace", type=float, default=DEFAULT_PACE_S,
                   help=f"seconds per character (default {DEFAULT_PACE_S})")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                   help="per-expect timeout in seconds")
    p.add_argument("--send", action="append", default=[], dest="steps_send")
    p.add_argument("--expect", action="append", default=[], dest="steps_expect")
    p.add_argument("--drain", type=float, default=0.3,
                   help="seconds to absorb pending output before the first send")
    p.add_argument("--log", help="tee everything received to this file")
    p.add_argument("--self-test", action="store_true",
                   help="run the board-free pty loopback proof and exit")
    args = p.parse_args(argv)

    if args.self_test:
        print("mps3_console self-test (pty loopback, no board):")
        return _self_test()

    if not args.dev:
        p.error("--dev is required (or use --self-test)")

    # Rebuild the interleaved order from argv so --send/--expect pair up as typed.
    raw = sys.argv[1:] if argv is None else list(argv)
    order = [a for a in raw if a in ("--send", "--expect")]
    sends, expects = list(args.steps_send), list(args.steps_expect)
    steps = []
    for flag in order:
        if flag == "--send" and sends:
            steps.append(("send", sends.pop(0)))
        elif flag == "--expect" and expects:
            steps.append(("expect", expects.pop(0)))

    log = open(args.log, "a", encoding="utf-8") if args.log else None
    try:
        con = Console.open(args.dev, baud=args.baud, pace_s=args.pace, log=log)
    except OSError as e:
        print(f"mps3_console: cannot open {args.dev}: {e}", file=sys.stderr)
        return 2

    rc = 0
    try:
        con.drain(args.drain)
        for kind, val in steps:
            if kind == "send":
                con.send(val)
            else:
                try:
                    con.expect(val, timeout=args.timeout)
                    print(f"  [PASS] expect {val!r}")
                except ConsoleTimeout as e:
                    print(f"  [FAIL] expect {val!r}\n{e}", file=sys.stderr)
                    rc = 1
                    break
    finally:
        con.close()
        if log:
            log.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
