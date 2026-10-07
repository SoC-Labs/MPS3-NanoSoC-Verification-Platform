#!/usr/bin/env python3
"""decode_readback.py -- recover named register / memory values from an UltraScale
configuration READBACK, using Vivado's logic-location (`.ll`) file.

WHAT THIS IS FOR
    XAPP1230 (v1.1) "Configuration Readback Capture in UltraScale FPGAs": the
    device can be made to sample every CLB register, LUTRAM, SRL and block RAM
    into configuration memory, which is then read back over JTAG/ICAP/SelectMAP.
    Vivado's `.ll` file maps each configuration bit to a design net. Together they
    give **complete register visibility with no instrumentation at all** -- no
    probe selection, no re-synthesis, no BRAM cost.

    That is the opposite trade-off from the IICE trace path in this repo:

        IICE trace   many cycles (923 measured) of FEW signals (69 bits)
        readback     ALL registers (6,554 in the RP) at ONE cycle

    They are complements. This tool is the readback half's decoder.

THE MAPPING (SOLVED 2026-07-31 -- see README.md for the evidence)
    A `.ll` "Bit" line is

        Bit <offset> <frameaddr> <frameoffset> <SLRname> <SLRnum> <key=value>...

    and the ASCII readback (`.rdbk` from `readback_hw_device`, `.rbd`/`.msd` from
    `write_bitstream -readback_file -mask_file`) is 32 characters + '\\n' per
    line, one 32-bit configuration word per line, after a short text header.

    MEASURED FACTS, in the order they matter:

      1. `<offset> == k*3936 + <frameoffset>` EXACTLY, for all 10,483,797 lines of
         the full-device `.ll`. 3936 = 123 words x 32 bits = one UltraScale frame.
         So `k = offset // 3936` is a *dense* frame ordinal -- it counts every
         frame of the SLR, not just the ones holding mapped content. (The 5,265
         frame *addresses* the `.ll` names are only the mapped subset; deriving an
         ordinal by sorting them is what refuted the second model below.)

      2. `<offset>` is **SLR-RELATIVE**. SLR0 and SLR1 offsets both start at 0 and
         `frameaddr -> k` is the same function in both. The `.ll` does NOT give
         the SLR's base in the stream; you must add it.

      3. The readback data is preceded by **133 pad words** (= one 123-word dummy
         frame + 10 pipeline words) before SLR0's frame 0.

      4. Inside a word, `offset % 32` is an **LSB-first** bit number, while the
         line is printed **MSB-first**. So the character column is `31 - off%32`.

    Hence, for SLR0:

        word_index    = 133 + offset // 32
        char_column   =  31 - offset  % 32          # 0 = leftmost character
        byte_in_file  = header_len + word_index*33 + char_column

    `ll_offset_to_stream_bit()` is that, and `verify_mapping()` re-proves it from
    a `.msd` mask every time rather than trusting this docstring.

TWO TRAPS, BOTH REAL
  1. **CLB register values read back INVERTED.** XAPP1230: "Value captured as 0
     when 1". BRAM/LUTRAM/SRL do NOT invert. `--invert-clb` (default on).
  2. **Vivado's `.msd` mask does NOT mark CLB flip-flop content.** Measured: it
     covers BRAM content and LUTRAM/SRL content at exactly 100%, and CLB FF
     content at exactly 0%. So "how many flip-flop bits land on mask=1" is *not*
     a mapping test -- it is a class test, and the correct answers are 1.0 for RAM
     and 0.0 for flops. `verify_mapping()` checks all three.

USAGE
    # re-prove the mapping against a Vivado mask (board-free, fail-closed)
    decode_readback.py --ll probe.ll --mask full.msd --verify

    # solve the pad from scratch if the device/geometry ever changes
    decode_readback.py --ll probe.ll --mask full.msd --solve-pad 0 600

    # decode named registers, with the design's own expected readback as the
    # INIT reference so live state is distinguishable from reset state
    decode_readback.py --ll probe.ll --rdbk snap.rdbk --ref full.rbd \\
        --net-filter 'u_gpr/reg_r' --json regs.json

    # pull a block RAM's contents back out as bytes
    decode_readback.py --ll probe.ll --rdbk snap.rdbk --bram RAMB18_X10Y30 \\
        --bram-out imem.bin
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
from typing import Dict, Iterator, List, Optional, Tuple

# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------

WORD_BITS = 32                    # configuration word width
LINE_BYTES = WORD_BITS + 1        # 32 ASCII characters + '\n'
FRAME_WORDS = 123                 # UltraScale configuration frame
FRAME_BITS = FRAME_WORDS * WORD_BITS          # 3936

#: Words of pad before SLR0 frame 0 in a full-device UltraScale readback.
#: 133 = one 123-word dummy frame + 10 pipeline words. Measured on XCKU115;
#: `solve_pad()` re-derives it and `verify_mapping()` re-checks it.
PAD_WORDS_SLR0 = 133

#: SLR1's base is NOT established -- see README. Decoding SLR1 entries with
#: SLR0's pad would be silently wrong, so the tool refuses instead.
PAD_WORDS_BY_SLR: Dict[int, Optional[int]] = {0: PAD_WORDS_SLR0, 1: None}


def ll_offset_to_stream_bit(offset: int, pad_words: int = PAD_WORDS_SLR0) -> int:
    """`.ll` `<offset>` -> flat bit index into the ASCII readback DATA region.

    Bit index `b` means: character `b % 32` of word `b // 32`, counting the
    left-most printed character as 0.
    """
    if offset < 0:
        raise ValueError("negative .ll offset")
    word = pad_words + offset // WORD_BITS
    col = (WORD_BITS - 1) - (offset % WORD_BITS)
    return word * WORD_BITS + col


def ll_offset_to_word_col(offset: int,
                          pad_words: int = PAD_WORDS_SLR0) -> Tuple[int, int]:
    """`.ll` `<offset>` -> `(word_index, char_column)`."""
    b = ll_offset_to_stream_bit(offset, pad_words)
    return b // WORD_BITS, b % WORD_BITS


def stream_bit_to_ll_offset(bit: int, pad_words: int = PAD_WORDS_SLR0) -> int:
    """Inverse of :func:`ll_offset_to_stream_bit`."""
    word, col = divmod(bit, WORD_BITS)
    return (word - pad_words) * WORD_BITS + ((WORD_BITS - 1) - col)


def frame_of_offset(offset: int) -> Tuple[int, int]:
    """`(frame_ordinal, frame_offset)`. Holds exactly for every `.ll` line."""
    return offset // FRAME_BITS, offset % FRAME_BITS


# ---------------------------------------------------------------------------
# .ll parsing
# ---------------------------------------------------------------------------

# `Bit  <offset> <frameaddr> <frameoffset> <SLRname> <SLRnum> <key=value>...`
_BIT_RE = re.compile(
    r"^Bit\s+(\d+)\s+(0x[0-9a-fA-F]+)\s+(\d+)\s+(\S+)\s+(\d+)\s*(.*)$")

# cell classes, in the order they must be tested
CLB_FF = "clb_ff"            # SLICE flip-flop: Latch=AQ..HQ2      -- INVERTS
BRAM_LATCH = "bram_latch"    # BRAM output register: Latch=DOAL0 .. -- see README
LUTRAM = "lutram"            # SLICE LUT as RAM/ROM/SRL: Ram=/Rom=
BRAM = "bram"                # block RAM content: RAM=B:BIT<n>
OTHER = "other"


class LlEntry(object):
    __slots__ = ("offset", "frame", "frame_off", "slr", "block", "latch",
                 "net", "ram", "cls")

    def __init__(self, offset, frame, frame_off, slr, block, latch, net, ram,
                 cls):
        self.offset = offset
        self.frame = frame
        self.frame_off = frame_off
        self.slr = slr
        self.block = block
        self.latch = latch
        self.net = net
        self.ram = ram
        self.cls = cls

    @property
    def inverts(self) -> bool:
        """XAPP1230: CLB registers read back inverted; RAM content does not."""
        return self.cls == CLB_FF

    def __repr__(self):
        return "LlEntry(off=%d slr=%d cls=%s net=%r)" % (
            self.offset, self.slr, self.cls, self.net)


def classify(latch: Optional[str], ram: Optional[str],
             block: Optional[str]) -> str:
    """Which physical memory class a `.ll` line describes.

    The distinction that matters and that the previous version of this tool got
    wrong: `Latch=DOAL0`/`DOAU3`/`DOBL7`/`DOPAU0` are **block RAM output
    registers**, not SLICE flip-flops. They live in BRAM tiles, they are 859 of
    the 15,592 SLR0 `Latch=` lines, and their bit-position convention inside the
    frame differs from a SLICE flop's (measured: SLICE flops sit at
    `frameoffset % 4 == 0`, BRAM output registers do not). Lumping them in with
    CLB flops is what made the flip-flop statistics look inconsistent.
    """
    if ram is not None:
        # `RAM=B:BIT<n>` / `B:PARBIT<n>` is block RAM content; `Ram=A:12` /
        # `Rom=A:12` is a SLICE LUT used as distributed RAM / ROM / SRL.
        if ram.startswith("B:"):
            return BRAM
        return LUTRAM
    if latch is not None:
        if latch.startswith("DO"):
            return BRAM_LATCH
        return CLB_FF
    return OTHER


def parse_ll(path: str,
             net_filter: Optional[str] = None,
             classes: Optional[Tuple[str, ...]] = None,
             require_net: bool = False,
             blocks: Optional[Tuple[str, ...]] = None) -> Iterator[LlEntry]:
    """Stream a `.ll`. These run 64 MB (RP-scoped) to 764 MB (full device), so
    this never builds a list -- callers filter as they go."""
    pat = re.compile(net_filter) if net_filter else None
    blockset = set(blocks) if blocks else None
    with open(path, "r", errors="replace") as fh:
        for line in fh:
            if not line.startswith("Bit"):
                continue
            m = _BIT_RE.match(line.rstrip("\n"))
            if not m:
                continue
            info = m.group(6)
            kv = {}
            for tok in info.split():
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    kv[k.lower()] = v
            # the file header documents `Ram=`, but block RAM is emitted as
            # uppercase `RAM=` (Vivado 2024.1). Lower-casing the keys above
            # handles both; the `B:` prefix is what separates them.
            latch, ram, block = kv.get("latch"), kv.get("ram"), kv.get("block")
            if ram is None:
                ram = kv.get("rom")
            cls = classify(latch, ram, block)
            if classes is not None and cls not in classes:
                continue
            net = kv.get("net")
            if require_net and net is None:
                continue
            if pat and (net is None or not pat.search(net)):
                continue
            if blockset is not None and block not in blockset:
                continue
            yield LlEntry(int(m.group(1)), int(m.group(2), 16), int(m.group(3)),
                          int(m.group(5)), block, latch, net, ram, cls)


# ---------------------------------------------------------------------------
# ASCII readback access
# ---------------------------------------------------------------------------

class AsciiReadback(object):
    """Random access into an ASCII readback / mask / expected-readback file.

    Handles both flavours seen in practice:
      * `readback_hw_device -readback_file` -> **no header** (measured: 0 bytes)
      * `write_bitstream -readback_file/-mask_file` -> a short text header
        ending in a `Bits:\\t<n>\\n` line (measured 314 / 310 bytes)

    `seek`+`read(1)` is fast enough for the tens of thousands of bits we care
    about and keeps the memory profile flat for a 398 MB file.
    """

    def __init__(self, path: str, pad_words: int = PAD_WORDS_SLR0):
        self.path = path
        self.pad_words = pad_words
        self._fh = open(path, "rb")
        self.size = os.path.getsize(path)
        self.header_len = self._detect_header(self._fh)
        data = self.size - self.header_len
        if data % LINE_BYTES:
            raise ValueError(
                "%s: %d data bytes is not a whole number of %d-byte lines "
                "(header detected as %d)"
                % (path, data, LINE_BYTES, self.header_len))
        self.n_words = data // LINE_BYTES
        self.declared_bits = self._declared_bits

    # -- header ------------------------------------------------------------
    @staticmethod
    def _detect_header(fh) -> int:
        pos = fh.tell()
        fh.seek(0)
        head = fh.read(4096)
        fh.seek(pos)
        if head[:1] in (b"0", b"1"):
            return 0                      # raw data, no header
        i = head.find(b"Bits:")
        if i < 0:
            raise ValueError("unrecognised readback header")
        j = head.find(b"\n", i)
        if j < 0:
            raise ValueError("truncated readback header")
        return j + 1

    @property
    def _declared_bits(self) -> Optional[int]:
        if self.header_len == 0:
            return None
        self._fh.seek(0)
        head = self._fh.read(self.header_len)
        m = re.search(rb"Bits:\s*(\d+)", head)
        return int(m.group(1)) if m else None

    def close(self):
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # -- access ------------------------------------------------------------
    def bit_at(self, stream_bit: int) -> Optional[int]:
        """Raw bit at a flat stream-bit index, or None if off the end / off grid."""
        if stream_bit < 0:
            return None
        word, col = divmod(stream_bit, WORD_BITS)
        pos = self.header_len + word * LINE_BYTES + col
        if word >= self.n_words:
            return None
        self._fh.seek(pos)
        ch = self._fh.read(1)
        if ch == b"0":
            return 0
        if ch == b"1":
            return 1
        return None            # landed on a newline => geometry is wrong

    def bit(self, ll_offset: int, pad_words: Optional[int] = None
            ) -> Optional[int]:
        """Raw bit for a `.ll` `<offset>`."""
        pad = self.pad_words if pad_words is None else pad_words
        return self.bit_at(ll_offset_to_stream_bit(ll_offset, pad))

    def word(self, word_index: int) -> Optional[str]:
        """The 32 printed characters of one configuration word."""
        if word_index < 0 or word_index >= self.n_words:
            return None
        self._fh.seek(self.header_len + word_index * LINE_BYTES)
        return self._fh.read(WORD_BITS).decode("ascii", "replace")


# ---------------------------------------------------------------------------
# verification -- the thing that keeps this honest
# ---------------------------------------------------------------------------

#: What a CORRECT mapping must produce when `.ll` classes are looked up in a
#: Vivado `-mask_file` (`.msd`). Measured on XCKU115 / config_rm_nanosoc_routed:
#:   BRAM       10,432,512 / 10,432,512 = 1.0000000
#:   LUTRAM/SRL      35,136 /     35,136 = 1.0000000
#:   CLB flop             0 /     15,592 = 0.0000000
#: The refuted alternatives miss these by a mile -- see README's table.
MASK_EXPECT = {
    BRAM:   (1.0, 1.0),        # (min, max) acceptable mask=1 fraction
    LUTRAM: (1.0, 1.0),
    CLB_FF: (0.0, 0.0),
}


def measure_mask(ll_path: str, mask_path: str, pad_words: int,
                 slr: int = 0, per_class_limit: Optional[int] = 200000
                 ) -> Dict[str, Dict[str, object]]:
    """mask=1 fraction per `.ll` class, at a given pad.

    `per_class_limit` caps each class so a 764 MB full-device `.ll` (10.4M block
    RAM lines) still verifies in seconds. The test is a per-bit conjunction, so a
    200k-bit sample that comes out exactly 1.0 or exactly 0.0 is already
    astronomically decisive; set it to None to check every bit.
    """
    out: Dict[str, Dict[str, object]] = {}
    counters = collections.defaultdict(lambda: [0, 0, 0])   # n, ones, offgrid
    with AsciiReadback(mask_path, pad_words) as rb:
        for e in parse_ll(ll_path):
            if e.slr != slr:
                continue
            c = counters[e.cls]
            if per_class_limit is not None and (c[0] + c[2]) >= per_class_limit:
                continue
            v = rb.bit(e.offset)
            if v is None:
                c[2] += 1
                continue
            c[0] += 1
            c[1] += v
    for cls, (n, ones, off) in counters.items():
        out[cls] = {"n": n, "ones": ones, "off_grid": off,
                    "frac": (ones / n) if n else None}
    return out


def verify_mapping(ll_path: str, mask_path: str, pad_words: int = PAD_WORDS_SLR0,
                   slr: int = 0) -> Tuple[bool, Dict[str, Dict[str, object]]]:
    """True only if every class hits its measured expectation exactly."""
    got = measure_mask(ll_path, mask_path, pad_words, slr=slr)
    ok = True
    for cls, (lo, hi) in MASK_EXPECT.items():
        st = got.get(cls)
        if not st or not st["n"]:
            ok = False
            continue
        if not (lo <= st["frac"] <= hi):
            ok = False
    return ok, got


def solve_pad(ll_path: str, mask_path: str, lo: int, hi: int,
              slr: int = 0, sample: int = 20000) -> List[Tuple[float, int]]:
    """Sweep the pad and score by block-RAM mask coverage.

    Block RAM content frames are *entirely* mask=1, so this pins the frame
    mapping (and therefore the pad) without depending on the intra-word column
    convention at all -- which is why it is the right thing to sweep first.
    Returns `[(frac, pad), ...]` best first.
    """
    offs = []
    for e in parse_ll(ll_path, classes=(BRAM,)):
        if e.slr != slr:
            continue
        offs.append(e.offset)
        if len(offs) >= sample:
            break
    if not offs:
        raise SystemExit("solve_pad: no block-RAM entries in %s" % ll_path)
    scores = []
    with AsciiReadback(mask_path) as rb:
        for pad in range(lo, hi):
            n = ones = 0
            for o in offs:
                v = rb.bit_at(ll_offset_to_stream_bit(o, pad))
                if v is None:
                    continue
                n += 1
                ones += v
            if n:
                scores.append((ones / n, pad))
    scores.sort(reverse=True)
    return scores


# ---------------------------------------------------------------------------
# decode
# ---------------------------------------------------------------------------

_IDX_RE = re.compile(r"^(.*?)\[(\d+)\]$")


def decode_nets(ll_path: str, rdbk_path: str,
                net_filter: Optional[str] = None,
                pad_words: int = PAD_WORDS_SLR0,
                invert_clb: bool = True,
                slr: int = 0,
                ref_path: Optional[str] = None,
                ) -> Tuple[Dict[str, Dict[int, int]],
                           Dict[str, Dict[int, int]],
                           Dict[str, int]]:
    """`({net: {bit: value}}, {net: {bit: ref_value}}, stats)`.

    `ref_path` should be the design's own `write_bitstream -readback_file`
    output. It is the *expected* readback of the same configuration, so it
    carries the design's INIT/reset state -- which is the only way to tell a
    live capture from an un-captured one without a second mechanism.
    """
    vals: Dict[str, Dict[int, int]] = {}
    refs: Dict[str, Dict[int, int]] = {}
    stats = collections.Counter()
    rb = AsciiReadback(rdbk_path, pad_words)
    ref = AsciiReadback(ref_path, pad_words) if ref_path else None
    try:
        for e in parse_ll(ll_path, net_filter, classes=(CLB_FF,),
                          require_net=True):
            if e.slr != slr:
                stats["skipped_other_slr"] += 1
                continue
            raw = rb.bit(e.offset)
            if raw is None:
                stats["off_grid"] += 1
                continue
            v = (1 - raw) if (invert_clb and e.inverts) else raw
            m = _IDX_RE.match(e.net)
            base, idx = (m.group(1), int(m.group(2))) if m else (e.net, 0)
            vals.setdefault(base, {})[idx] = v
            if ref is not None:
                rv = ref.bit(e.offset)
                if rv is not None:
                    rv = (1 - rv) if (invert_clb and e.inverts) else rv
                    refs.setdefault(base, {})[idx] = rv
                    stats["ref_differs"] += int(rv != v)
            stats["decoded"] += 1
    finally:
        rb.close()
        if ref is not None:
            ref.close()
    return vals, refs, dict(stats)


def as_word(bits: Dict[int, int]) -> Tuple[int, int, bool]:
    """`(value, width, contiguous)` for a bit-indexed net."""
    if not bits:
        return 0, 0, False
    hi = max(bits)
    contiguous = set(bits) == set(range(hi + 1))
    v = 0
    for i, b in bits.items():
        if b:
            v |= (1 << i)
    return v, hi + 1, contiguous


def decode_bram(ll_path: str, rdbk_path: str, site: str,
                pad_words: int = PAD_WORDS_SLR0, slr: int = 0,
                parity: bool = False) -> bytes:
    """Reconstruct one block RAM's contents.

    Bit `n` of the reconstruction is `RAM=B:BIT<n>` (or `B:PARBIT<n>` with
    `parity=True`), packed **LSB-first within each byte** -- i.e. `BIT<n>` is bit
    `n % 8` of byte `n // 8`. That convention plus the column rule above is what
    makes a firmware ROM come back as readable bytes; every other combination
    tried returns the same bytes in a scrambled order (see README).
    """
    want = re.compile(r"^B:PARBIT(\d+)$" if parity else r"^B:BIT(\d+)$")
    bits: Dict[int, int] = {}
    with AsciiReadback(rdbk_path, pad_words) as rb:
        for e in parse_ll(ll_path, classes=(BRAM,), blocks=(site,)):
            if e.slr != slr:
                continue
            m = want.match(e.ram or "")
            if not m:
                continue
            v = rb.bit(e.offset)
            if v is not None:
                bits[int(m.group(1))] = v
    if not bits:
        return b""
    n = max(bits) + 1
    out = bytearray((n + 7) // 8)
    for i, v in bits.items():
        if v:
            out[i // 8] |= (1 << (i % 8))
    return bytes(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cmd_verify(a) -> int:
    ok, got = verify_mapping(a.ll, a.mask, a.pad, slr=a.slr)
    print("mapping check: word = %d + offset//32 , column = 31 - offset%%32"
          % a.pad)
    print("  %-12s %-10s %-12s %s" % ("class", "n", "mask=1 frac", "expected"))
    for cls in (BRAM, LUTRAM, CLB_FF, BRAM_LATCH, OTHER):
        st = got.get(cls)
        if not st:
            continue
        exp = MASK_EXPECT.get(cls)
        print("  %-12s %-10d %-12s %s"
              % (cls, st["n"],
                 "n/a" if st["frac"] is None else "%.7f" % st["frac"],
                 ("%.1f" % exp[0]) if exp else "-"))
        if st["off_grid"]:
            print("      off_grid=%d" % st["off_grid"])
    if not ok:
        print("\nMAPPING CHECK FAILED. Refusing to decode.")
        print("A correct mapping puts block-RAM and LUTRAM/SRL content on")
        print("mask=1 at exactly 1.0 and CLB flip-flops on mask=1 at exactly")
        print("0.0 (Vivado does not mask flop content). Try --solve-pad.")
        return 2
    print("\nOK")
    return 0


def _cmd_solve(a) -> int:
    lo, hi = a.solve_pad
    scores = solve_pad(a.ll, a.mask, lo, hi, slr=a.slr)
    print("block-RAM mask coverage by pad (best first):")
    for frac, pad in scores[:10]:
        print("   pad=%-6d frac=%.7f%s" % (pad, frac,
                                           "   <== exact" if frac == 1.0 else ""))
    exact = [p for f, p in scores if f == 1.0]
    if len(exact) != 1:
        print("\nREFUSING to pick a pad: %d candidates reach exactly 1.0."
              % len(exact))
        print("Widen or narrow the sweep, or supply more block-RAM entries.")
        return 2
    print("\nsolved pad = %d" % exact[0])
    if not a.verify:
        return 0
    a.pad = exact[0]
    return _cmd_verify(a)


def _cmd_bram(a) -> int:
    data = decode_bram(a.ll, a.rdbk, a.bram, a.pad, slr=a.slr)
    if not data:
        print("no block-RAM content bits for site %s" % a.bram)
        return 2
    print("%s: %d bytes" % (a.bram, len(data)))
    runs = re.findall(rb"[ -~]{8,}", data)
    print("printable runs >= 8 chars: %d" % len(runs))
    for r in runs[:12]:
        print("   %r" % r[:70])
    if a.bram_out:
        with open(a.bram_out, "wb") as fh:
            fh.write(data)
        print("wrote %s" % a.bram_out)
    return 0


def _cmd_decode(a) -> int:
    vals, refs, stats = decode_nets(
        a.ll, a.rdbk, a.net_filter, a.pad,
        invert_clb=not a.no_invert_clb, slr=a.slr, ref_path=a.ref)
    print("decoded %d CLB flip-flop bits across %d nets  (%s)"
          % (stats.get("decoded", 0), len(vals), stats))
    if a.ref:
        n = stats.get("decoded", 0)
        d = stats.get("ref_differs", 0)
        print("vs --ref (design's own expected readback = INIT/reset state): "
              "%d of %d bits differ (%.4f)" % (d, n, (d / n) if n else 0.0))
        print("  ~0 means the captured state IS the reset state; a nonzero")
        print("  fraction is the positive control that capture is live.")
    hdr = "  %-64s %-13s" % ("net", "readback")
    if a.ref:
        hdr += " %-13s" % "ref(INIT)"
    print(hdr)
    shown = 0
    for base in sorted(vals):
        v, w, contig = as_word(vals[base])
        line = "  %-64s %-13s" % (base[-64:], "%d'h%X" % (w, v))
        if a.ref:
            rv, _, _ = as_word(refs.get(base, {}))
            line += " %-13s" % ("%d'h%X" % (w, rv))
        if not contig:
            line += "  SPARSE"
        print(line)
        shown += 1
        if shown >= a.top:
            print("  ... (%d more; use --json for all)" % (len(vals) - shown))
            break
    if a.json:
        out = {}
        for b in vals:
            v, w, c = as_word(vals[b])
            rec = {"bits": vals[b], "value": v, "width": w, "contiguous": c}
            if a.ref:
                rec["ref_value"] = as_word(refs.get(b, {}))[0]
            out[b] = rec
        with open(a.json, "w") as fh:
            json.dump({"pad_words": a.pad, "column_rule": "31 - offset%32",
                       "invert_clb": not a.no_invert_clb,
                       "stats": stats, "nets": out}, fh, indent=1)
        print("wrote %s" % a.json)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ll", required=True, help="Vivado logic-location file")
    p.add_argument("--rdbk", default=None,
                   help="ASCII readback (.rdbk capture or .rbd expectation)")
    p.add_argument("--mask", default=None,
                   help="Vivado -mask_file output (.msd), for --verify/--solve-pad")
    p.add_argument("--ref", default=None,
                   help=".rbd expected readback, used as the INIT reference")
    p.add_argument("--pad", type=int, default=PAD_WORDS_SLR0,
                   help="pad words before SLR0 frame 0 (default %d)"
                        % PAD_WORDS_SLR0)
    p.add_argument("--slr", type=int, default=0,
                   help="SLR to decode; only SLR0's pad is established")
    p.add_argument("--verify", action="store_true",
                   help="re-prove the mapping against --mask, then exit")
    p.add_argument("--solve-pad", nargs=2, type=int, metavar=("LO", "HI"),
                   help="sweep the pad against --mask and refuse to guess")
    p.add_argument("--net-filter", default=None,
                   help="regex; only nets matching are decoded")
    p.add_argument("--bram", default=None,
                   help="reconstruct this block-RAM site (e.g. RAMB18_X10Y30)")
    p.add_argument("--bram-out", default=None, help="write --bram bytes here")
    p.add_argument("--no-invert-clb", action="store_true",
                   help="do NOT invert CLB registers (XAPP1230 says invert)")
    p.add_argument("--json", default=None)
    p.add_argument("--top", type=int, default=40,
                   help="how many decoded nets to print")
    a = p.parse_args(argv)

    if a.slr != 0 and PAD_WORDS_BY_SLR.get(a.slr) is None:
        sys.stderr.write(
            "refusing to decode SLR%d: its base in the readback stream is not\n"
            "established (the .ll's <offset> is SLR-relative). See README.\n"
            % a.slr)
        return 2

    if a.solve_pad:
        if not a.mask:
            p.error("--solve-pad needs --mask")
        return _cmd_solve(a)
    if a.verify:
        if not a.mask:
            p.error("--verify needs --mask")
        return _cmd_verify(a)
    if not a.rdbk:
        p.error("need --rdbk (or --verify/--solve-pad with --mask)")

    # Fail closed: if a mask was supplied, the mapping must check out first.
    if a.mask:
        ok, _ = verify_mapping(a.ll, a.mask, a.pad, slr=a.slr)
        if not ok:
            sys.stderr.write("mapping check against --mask FAILED; refusing to "
                             "decode. Run with --verify for detail.\n")
            return 2

    if a.bram:
        return _cmd_bram(a)
    return _cmd_decode(a)


if __name__ == "__main__":
    raise SystemExit(main())
