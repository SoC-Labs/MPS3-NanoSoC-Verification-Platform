#!/usr/bin/env python3
"""check_uart_capture.py -- decode console bytes out of an ILA capture.

Two modes, because the two ILA RMs see the console differently:

``--mode axis`` (default; rm_nanosoc_ila). The serial TXD pin is NOT a port of
rp_nanosoc_wrapper (uart_axis_shim deserialises inside it), so the ILA sees the
AXI-stream byte interface: ``uart_tx_tdata[7:0]``, ``uart_tx_tvalid``,
``uart_tx_tready``. Every sample with tvalid && tready is one console byte.
With the RM's storage qualifier set to exactly that (ila_capture.tcl
``CAPTURE='uart_tx_tvalid==1,uart_tx_tready==1'``) every stored row is a byte;
without it the handshake filter below still picks them out.

``--mode serial``: a TXD line sampled at the DUT clock, decoded as 8N1 at
``--cycles-per-bit`` samples per bit (default 434 = 50 MHz / 115200). A frame is
a 1->0 edge, a start bit still 0 at its middle, eight data bits sampled at
their middles LSB first, and a stop bit that is 1; a frame whose start or stop
bit is wrong is reported and skipped, never guessed. (For any other baud pass
``--cycles-per-bit``; the nanosoc console is 76800, i.e. 651 at 50 MHz.)

PASS if at least ``--min-printable`` (default 1) printable character decodes;
the characters are printed either way. ``--expect TEXT`` additionally requires
TEXT to appear in the decoded string. Exit 0 PASS, 1 FAIL, 2 unreadable input.

    check_uart_capture.py cap.csv [--mode axis] [--data uart_tx_tdata]
                          [--valid uart_tx_tvalid] [--ready uart_tx_tready]
    check_uart_capture.py cap.csv --mode serial --txd uart_txd [--cycles-per-bit 434]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ila_csv import IlaCsvError, read_ila_csv  # noqa: E402


def decode_axis(data: Sequence[int], valid: Sequence[int],
                ready: Optional[Sequence[int]]) -> bytes:
    out = bytearray()
    for i, d in enumerate(data):
        if valid[i] and (ready is None or ready[i]):
            out.append(d & 0xFF)
    return bytes(out)


def decode_serial(bits: Sequence[int], cpb: float) -> Tuple[bytes, List[str]]:
    """8N1 decode of a TXD sample stream. Returns (bytes, framing notes)."""
    out = bytearray()
    notes: List[str] = []
    n = len(bits)
    i = 1
    while i < n:
        if not (bits[i - 1] == 1 and bits[i] == 0):
            i += 1
            continue
        start = i
        mid_start = int(round(start + cpb / 2))
        stop_mid = int(round(start + cpb * 9.5))
        if stop_mid >= n:
            notes.append(f"sample {start}: frame runs past the end of the capture (ignored)")
            break
        if bits[mid_start] != 0:
            notes.append(f"sample {start}: glitch, start bit is 1 at its middle (skipped)")
            i = start + 1
            continue
        byte = 0
        for b in range(8):
            if bits[int(round(start + cpb * (1.5 + b)))]:
                byte |= 1 << b
        if bits[stop_mid] != 1:
            notes.append(f"sample {start}: framing error, stop bit is 0 (0x{byte:02x} discarded)")
            i = stop_mid
            continue
        out.append(byte)
        i = stop_mid  # the next 1->0 edge is searched for from the stop bit
    return bytes(out), notes


def printable(bs: bytes) -> str:
    return "".join(chr(b) if 32 <= b < 127 else ("\\n" if b == 10 else "\\r" if b == 13
                   else f"\\x{b:02x}") for b in bs)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("csv")
    ap.add_argument("--mode", choices=("axis", "serial"), default="axis")
    ap.add_argument("--data", default="uart_tx_tdata")
    ap.add_argument("--valid", default="uart_tx_tvalid")
    ap.add_argument("--ready", default="uart_tx_tready", help="'' to ignore tready")
    ap.add_argument("--txd", default="uart_txd", help="serial mode: the TXD probe")
    ap.add_argument("--cycles-per-bit", type=float, default=434.0)
    ap.add_argument("--min-printable", type=int, default=1)
    ap.add_argument("--expect", default=None)
    a = ap.parse_args(argv)
    try:
        cap = read_ila_csv(a.csv)
        notes: List[str] = []
        if a.mode == "axis":
            got = decode_axis(cap.values(a.data), cap.values(a.valid),
                              cap.values(a.ready) if a.ready else None)
            how = f"{a.data} on {a.valid}{' && ' + a.ready if a.ready else ''}"
        else:
            got, notes = decode_serial(cap.values(a.txd), a.cycles_per_bit)
            how = f"8N1 on {a.txd} at {a.cycles_per_bit:g} samples/bit"
    except (OSError, IlaCsvError) as exc:
        print(f"UART_CHECK FAIL: cannot read {a.csv}: {exc}")
        return 2
    for note in notes:
        print(f"  note: {note}")
    npr = sum(1 for b in got if 32 <= b < 127)
    text = printable(got)
    print(f"decoded {len(got)} byte(s) ({how}): \"{text}\"")
    ok = npr >= a.min_printable
    why = f"{npr} printable character(s)"
    if ok and a.expect is not None and a.expect not in got.decode("latin-1"):
        ok = False
        why += f", but {a.expect!r} is not in the decoded text"
    print(f"UART_CHECK {'PASS' if ok else 'FAIL'}: {why}: \"{text}\"")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
