#!/usr/bin/env python3
"""check_counter_capture.py -- judge a B2/B3/B4/B5 capture from rm_dbg_demo.

The RM (fpga/rp/dbg_demo/rp_dbg_demo_wrapper.sv) has one ILA on dut_clk:
probe0 = ``counter[15:0]`` (free-running, +1 every dut_clk) and probe1 =
``tick`` = ``counter[7:0] == 0`` (combinational, same cycle). A capture is only
a proof if it matches that KNOWN behaviour (handover §6 B2), so:

  * every sample's counter is the previous one + 1, mod 2**width;
  * tick is 1 on exactly the samples where counter[7:0] == 0;
  * optionally (``--expect-first``) the first sample equals the trigger value.

Prints ``COUNTER_CHECK PASS: ...`` or ``COUNTER_CHECK FAIL: ...`` naming the
first violating CSV row, and exits 0 / 1 (2 = unreadable input).

    check_counter_capture.py capture.csv [--counter counter] [--tick tick]
                             [--width 16] [--tick-bits 8] [--expect-first 0x00FF]
                             [--min-samples 16]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ila_csv import IlaCsvError, read_ila_csv  # noqa: E402


def check(path, counter="counter", tick="tick", width=None, tick_bits=8,
          expect_first=None, min_samples=16):
    """Return (ok, message)."""
    cap = read_ila_csv(path)
    cnt = cap.values(counter)
    w = width or cap.width(counter) or 16
    mod = 1 << w
    tick_mask = (1 << tick_bits) - 1
    tk = cap.values(tick) if tick else None
    if len(cnt) < min_samples:
        return False, f"only {len(cnt)} sample(s) (< {min_samples}); not enough to judge"
    if expect_first is not None and cnt[0] != expect_first % mod:
        return False, (f"line {cap.lines[0]}: first sample counter=0x{cnt[0]:0{(w+3)//4}x}, "
                       f"expected the trigger value 0x{expect_first % mod:0{(w+3)//4}x}")
    for i in range(1, len(cnt)):
        want = (cnt[i - 1] + 1) % mod
        if cnt[i] != want:
            return False, (f"line {cap.lines[i]} (sample {i}): counter=0x{cnt[i]:0{(w+3)//4}x}, "
                           f"expected 0x{want:0{(w+3)//4}x} (previous +1 mod 2^{w})")
    ticks = 0
    if tk is not None:
        for i, (c, t) in enumerate(zip(cnt, tk)):
            want = 1 if (c & tick_mask) == 0 else 0
            if t != want:
                return False, (f"line {cap.lines[i]} (sample {i}): tick={t} with "
                               f"counter=0x{c:0{(w+3)//4}x}; tick must be {want} "
                               f"(1 iff counter[{tick_bits-1}:0]==0)")
            ticks += t
    return True, (f"{len(cnt)} samples, counter 0x{cnt[0]:0{(w+3)//4}x}..0x{cnt[-1]:0{(w+3)//4}x} "
                  f"steps +1 mod 2^{w} throughout"
                  + (f", tick high on exactly the {ticks} sample(s) with counter[{tick_bits-1}:0]==0"
                     if tk is not None else ""))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("csv")
    ap.add_argument("--counter", default="counter")
    ap.add_argument("--tick", default="tick", help="'' to skip the tick check")
    ap.add_argument("--width", type=int, default=None,
                    help="counter width (default: from the header's [hi:lo], else 16)")
    ap.add_argument("--tick-bits", type=int, default=8)
    ap.add_argument("--expect-first", type=lambda s: int(s, 0), default=None)
    ap.add_argument("--min-samples", type=int, default=16)
    a = ap.parse_args(argv)
    try:
        ok, msg = check(a.csv, a.counter, a.tick or None, a.width, a.tick_bits,
                        a.expect_first, a.min_samples)
    except (OSError, IlaCsvError) as exc:
        print(f"COUNTER_CHECK FAIL: cannot read {a.csv}: {exc}")
        return 2
    print(f"COUNTER_CHECK {'PASS' if ok else 'FAIL'}: {msg}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
