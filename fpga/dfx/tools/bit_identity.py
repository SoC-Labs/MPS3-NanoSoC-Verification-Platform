#!/usr/bin/env python3
"""bit_identity.py -- read USR_ACCESS back OUT of a .bit file, offline.

    python3 bit_identity.py <file.bit> [--expect 0x01000000]

Prints the USR_ACCESS word the bitstream actually carries, found in the
configuration packet stream rather than taken from a build log. Exits 1 with
--expect if the file disagrees or carries no such word at all.

WHY THIS EXISTS (2026-09-22). `version` on the then-fielded 0x3F1A560F shell
reported `usr_access: null`, so the ver32-vs-USR_ACCESS skew check could not run. The mint's
`static_stamp.json` says it stamped 0x01000000 -- but it read that back from
`config_rm_greybox.bit`, while the file that reaches the board is
`config_rm_greybox_fw.bit`, written afterwards by `updatemem`. Nothing had ever
checked the FINAL artefact, which is exactly how the stale-ELF half-mint of
2026-09-16 happened. So this tool asks the artefact.

It answered, and it cleared updatemem: BOTH files carry the AXSS write with
0x01000000. See the note at the bottom of this docstring for what that leaves.

HOW IT READS IT (rewritten 2026-09-24, after RC1). USR_ACCESS reaches the device
as a Type-1 configuration WRITE of one word to the AXSS register (0x0D): header
0x3001A001, then the value. This reader WALKS the packet stream on the 32-bit
grid from each sync word, skips every payload by its declared word count, and
records an AXSS write only where a packet header really is.

The first version searched the whole file for those 4 header bytes and called
that exact. It was not: RC1 (static 0xAE3CF93E) failed at mint stage 6 because
the search also matched 30 01 A0 01 two bytes OFF the word grid, inside SLR0's
compressed frame data (the Type-2 FDRI payload, byte 1,721,899), and reported a
second "USR_ACCESS" 0x82404000 beside the real 0x01000000 in both SLRs. The
P-mint's frames simply did not contain those bytes. A byte search over
megabytes of frame data finds the signature by chance; only the packet
structure says what is a write.

The earlier walk that "printed confident nonsense" read Type-2 payloads as
headers. This one never looks inside a payload, with ONE exception: on an SSI
part (the KU115 is 2 SLRs) the next SLR's entire configuration stream is the
payload of a Type-1 WRITE to register 0x1E plus the Type-2 packet after it, with
its own sync word. That payload is walked as the next SLR's stream
(recursively, so a 3-SLR part works the same). A word that is not a packet
header, a sync word or a pad word (0xFFFFFFFF, bus-width 0x000000BB/0x11220044)
is an error, never skipped: a reader that guesses is how the first one went
wrong.

PER SLR. USR_ACCESS is a per-SLR register. Vivado has one knob
(BITSTREAM.CONFIG.USR_ACCESS) and writes it into EVERY SLR's stream, so a good
full bitstream writes AXSS exactly once per SLR stream with one value, or never
(an unstamped build). usr_access_check() is that rule and `main` enforces it:
exit 1 on a violation, a malformed stream, or --expect disagreeing.

WHAT THE ANSWER LEFT OPEN (2026-09-22), on the board rather than in the file: the
USRACC block at 0x44B3_0000 answered MAGIC = 0x55535241 "USRA" but read VALUE = 0 and
VALID = 0. MAGIC is a constant in the AXI decode, so it proves the BLOCK is in
the bitstream -- it says nothing about the USR_ACCESSE2 primitive behind it.
With the word proven present in the file, the remaining explanation is on the
fabric side of that block: the primitive is not presenting its word (not
inferred, optimised away, or never captured). That needs RTL work and a mint,
not a re-bake.

RESOLVED. It was the capture, not the primitive: DATAVALID pulses during
configuration, before the AXI block leaves reset (postscript of
docs/evidence/2026-09-w2/usracc_20260922.txt). The fixed usr_access_rd rode the
2026-10 ILA mint, and 0x72BB0A36 reads usr_access 0x01000001, skew false
(docs/evidence/2026-09-w3/w1_field_remote_20260924.txt).
"""
from __future__ import annotations

import argparse
import struct
import sys

#: Type-1 WRITE of one word to AXSS (register 0x0D): [31:29]=001 type,
#: [28:27]=10 write, [26:13]=0x000D register, [10:0]=1 word.
AXSS_WRITE_HEADER = (1 << 29) | (2 << 27) | (0x0D << 13) | 1
SYNC_WORD = 0xAA995566
REG_AXSS = 0x0D
#: UltraScale SSI: the next SLR's whole configuration stream is the payload of a
#: Type-1 WRITE to this register (word count 0) + the Type-2 packet after it.
REG_SLR_STREAM = 0x1E
#: Words that may sit between packets: dummy/pad and the bus-width pattern.
PAD_WORDS = (0xFFFFFFFF, 0x000000BB, 0x11220044)
_SYNC_BYTES = struct.pack(">I", SYNC_WORD)


class BitstreamError(ValueError):
    """The file is not a configuration packet stream this reader can walk."""


def _walk(data, start, end, streams):
    """Walk ONE SLR stream: the first sync word in data[start:end], then its
    packets on the 32-bit grid from that sync. Appends [(offset, value), ...]
    (its AXSS writes) to `streams`; the SLR streams nested in it follow it."""
    sync = data.find(_SYNC_BYTES, start, end)
    if sync < 0:
        raise BitstreamError("no sync word 0xAA995566 in bytes %d..%d" % (start, end))
    mine = []
    streams.append(mine)
    pos = sync + 4
    last_op = None
    while pos + 4 <= end:
        (w,) = struct.unpack_from(">I", data, pos)
        kind = w >> 29
        if w == SYNC_WORD or w in PAD_WORDS:
            pos += 4
        elif kind == 1:
            op, reg, n = (w >> 27) & 3, (w >> 13) & 0x3FFF, w & 0x7FF
            last_op = (op, reg)
            if op == 2 and reg == REG_AXSS and n == 1:
                if pos + 8 > end:
                    raise BitstreamError("AXSS write at byte %d is truncated" % pos)
                mine.append((pos, struct.unpack_from(">I", data, pos + 4)[0]))
            # only a WRITE carries data words in a bitstream
            pos += 4 + (4 * n if op == 2 else 0)
        elif kind == 2:
            n = w & 0x07FFFFFF
            body, pos = pos + 4, pos + 4 + 4 * n
            if pos > end:
                raise BitstreamError("Type-2 packet at byte %d claims %d words, past the "
                                     "end of its stream (byte %d)" % (body - 4, n, end))
            if last_op == (2, REG_SLR_STREAM):
                _walk(data, body, pos, streams)
        else:
            raise BitstreamError("byte %d: 0x%08X is not a configuration packet header"
                                 % (pos, w))
    return streams


def usr_access_by_slr(path: str) -> list:
    """[[(offset, value), ...] per SLR configuration stream, in file order] --
    every AXSS write the packet stream really makes."""
    with open(path, "rb") as fh:
        data = fh.read()
    return _walk(data, 0, len(data), [])


def usr_access_words(path: str) -> list:
    """Every USR_ACCESS value written by this bitstream, in file order, as
    (offset, value): the per-SLR lists of usr_access_by_slr(), flattened."""
    return [hit for slr in usr_access_by_slr(path) for hit in slr]


def usr_access_check(path: str):
    """-> (value or None, [problems]). A good full bitstream writes AXSS exactly
    once in EVERY SLR stream, with ONE value (-> that value), or never (an
    unstamped build -> None). Anything else is a problem, named per SLR."""
    try:
        slrs = usr_access_by_slr(path)
    except (OSError, BitstreamError) as exc:
        return None, ["%s: %s" % (path, exc)]
    values = sorted({v for s in slrs for _o, v in s})
    if not values:
        return None, []
    problems = []
    for i, s in enumerate(slrs):
        if len(s) != 1:
            problems.append("SLR stream %d writes USR_ACCESS %d time(s) %s -- want exactly "
                            "once per SLR" % (i, len(s), ["0x%08X" % v for _o, v in s]))
    if len(values) > 1:
        problems.append("the SLR streams write DIFFERENT USR_ACCESS values %s -- each SLR "
                        "would report its own" % ["0x%08X" % v for v in values])
    return (values[0] if len(values) == 1 and not problems else None), problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("bitstream")
    ap.add_argument("--expect", default=None,
                    help="0x... the USR_ACCESS word this bitstream must carry")
    args = ap.parse_args(argv)

    try:
        slrs = usr_access_by_slr(args.bitstream)
    except (OSError, BitstreamError) as exc:
        print("error: %s: %s" % (args.bitstream, exc), file=sys.stderr)
        return 1
    for i, slr in enumerate(slrs):
        for off, value in slr:
            print("%s: SLR stream %d: USR_ACCESS = 0x%08X (at byte %d)"
                  % (args.bitstream, i, value, off))
    value, problems = usr_access_check(args.bitstream)
    if problems:
        for p in problems:
            print("error: %s: %s" % (args.bitstream, p), file=sys.stderr)
        return 1
    if value is None:
        print("%s: NO USR_ACCESS (AXSS) write in any of %d SLR stream(s)"
              % (args.bitstream, len(slrs)))
        if args.expect is not None:
            print("error: the bitstream carries no USR_ACCESS word, so the fabric's "
                  "USR_ACCESSE2 primitive has nothing to present and any readback "
                  "block will report VALID=0. Expected %s." % args.expect,
                  file=sys.stderr)
            return 1
        return 0
    print("%s: USR_ACCESS 0x%08X in all %d SLR stream(s)" % (args.bitstream, value, len(slrs)))
    if args.expect is not None and value != int(args.expect, 16):
        print("error: USR_ACCESS is 0x%08X, expected 0x%08X" % (value, int(args.expect, 16)),
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
