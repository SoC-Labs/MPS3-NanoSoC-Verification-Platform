#!/usr/bin/env python3
"""Tier-1 (static) gate: the diag mailbox cannot silently drop a counter.

CATCHES: bug #6 (a diagnostic that lied) -- a field that exists in the mailbox
but reads back its stale/zero init value forever, because some hand-written
field list forgot it. "An unverified diagnostic is worse than none."

WHY THIS GATE WAS REWRITTEN (2026-09-10)
----------------------------------------
The original gate compared the field SETS assigned by mps3_diag_init(),
mps3_diag_publish() and mps3_diag_snapshot() in firmware/common/diag.c -- it
was written when all three copied the struct FIELD BY FIELD, which is the
scheme that produced bug #6 in the first place.

diag.c was then fixed properly: init() memsets the whole struct and
publish()/snapshot() do whole-struct assignments, so a field can no longer be
forgotten BY CONSTRUCTION. That is the better fix -- and it blinded this gate.
With no field-wise assignments left to find, the gate reported

    init() stamps 0 counters, publish() writes 0, snapshot() reads 0
    OK: mps3_diag_publish() covers every counter the mailbox defines

i.e. it printed "covers every counter" over an EMPTY set and returned 0. A gate
that cannot fail is worse than no gate: it occupies the slot where a real check
would have been, and it reports success while doing nothing.

So this gate now asserts the structure that actually holds, plus the hole that
whole-struct copies do NOT close:

  1. STRUCTURE (fails if anyone reverts to field-wise copies)
       init()     zeroes the WHOLE struct with one memset(sizeof)
       publish()  does `g_mps3_diag = *v;` and then re-assigns ONLY metadata
       snapshot() does `*out = g_mps3_diag;` and assigns no individual field
     Any per-field assignment outside the metadata set re-opens bug #6 and
     fails here.

  2. REACHABILITY (the hole the struct copy leaves open)
     A whole-struct copy guarantees TRANSPORT, not PRODUCTION. A field nobody
     ever writes into the caller's `mps3_diag_t v` is copied faithfully -- and
     reads back zero forever, which is bug #6 wearing a different hat. So every
     non-metadata field declared in diag.h must be written (or address-taken)
     by a real publisher call site: the firmware that calls mps3_diag_publish().

  3. READOUT COVERAGE (advisory, unchanged in spirit)
     Fields the 6900 `diag` verb (coordinator_handle_diag) does not read are
     REPORTED, not failed -- some are deliberately JTAG-only.

Pure stdlib, seconds, no build. Source-vs-source, so a green build cannot
defeat it.

    python3 check_diag_field_parity.py [--repo ROOT]
"""
from __future__ import annotations

import argparse
import os
import re
import sys

#: Header/pad fields. publish() deliberately PRESERVES the init()-stamped magic
#: and version across the struct copy, and `reserved` is pad. They are exempt
#: from the produce-every-field rule, and they are the ONLY per-field
#: assignments publish() is allowed to make.
META = {"magic", "version", "reserved"}

#: Files that publish for real. Test harnesses also call mps3_diag_publish(),
#: but a test writing a field proves nothing about the shipped image, so they
#: are excluded from the producer scan by path.
_TEST_DIR = os.path.join("firmware", "test")


def func_body(src: str, name: str) -> str | None:
    """Body of the DEFINITION of `name` (braces included), or None.

    Skips prototypes and call sites by requiring the next non-space token after
    the closing paren to be `{` rather than `;` -- `coordinator.c` names
    coordinator_handle_diag at its dispatch switch long before it defines it,
    and taking the first match there silently returned nothing.
    """
    for m in re.finditer(r"\b%s\s*\(" % re.escape(name), src):
        brace = src.find("{", m.end())
        semi = src.find(";", m.end())
        if brace < 0 or (0 <= semi < brace):
            continue                      # prototype or call, not a definition
        depth = 0
        for j in range(brace, len(src)):
            if src[j] == "{":
                depth += 1
            elif src[j] == "}":
                depth -= 1
                if depth == 0:
                    return src[brace:j + 1]
    return None


def strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    return re.sub(r"//[^\n]*", " ", src)


def declared_fields(diag_h: str) -> list[str]:
    """Field names of `mps3_diag_t`, in declaration order."""
    src = strip_comments(open(diag_h).read())
    m = re.search(r"typedef\s+struct\s*\{(.*?)\}\s*mps3_diag_t\s*;", src, flags=re.S)
    if not m:
        return []
    return re.findall(r"\b(?:u?int(?:8|16|32|64)_t|char|unsigned|int|long)\s+"
                      r"(\w+)\s*(?:\[[^\]]*\])?\s*;", m.group(1))


def field_assignments(body: str) -> set[str]:
    """`g_mps3_diag.<f> = ...` / `out-><f> = ...` inside a function body."""
    return set(re.findall(r"(?:g_mps3_diag\.|out->)(\w+)\s*=(?!=)", strip_comments(body)))


def producers(root: str) -> tuple[set[str], list[str]]:
    """Fields written (or address-taken) at real mps3_diag_publish() call sites.

    Returns (field set, list of producer files). Matches both
    `v.field = ...`/`&v.field` forms, keyed off the variable actually handed to
    mps3_diag_publish() so a rename cannot quietly empty this set.
    """
    found: set[str] = set()
    files: list[str] = []
    for dirpath, _dirs, names in os.walk(os.path.join(root, "firmware")):
        rel = os.path.relpath(dirpath, root)
        if rel.startswith(_TEST_DIR) or os.sep + "test" in os.sep + rel:
            continue
        for n in names:
            if not n.endswith(".c") or n == "diag.c":
                continue
            path = os.path.join(dirpath, n)
            src = strip_comments(open(path, errors="replace").read())
            varnames = set(re.findall(r"mps3_diag_publish\s*\(\s*&?\s*(\w+)\s*\)", src))
            if not varnames:
                continue
            files.append(os.path.relpath(path, root))
            for var in varnames:
                found |= set(re.findall(r"\b%s\.(\w+)" % re.escape(var), src))
    return found, sorted(files)


def readout_fields(root: str, counters: list[str]) -> set[str]:
    """Fields the 6900 `diag` verb puts on the wire.

    Two recognised shapes. Up to net-protocol v0.8 coordinator_handle_diag
    copied fields one by one (`resp->diag_x = d.x;`) and the emitter had a
    hand-written key list, so the readout was whatever that body named. From
    v0.9 the handler snapshots the WHOLE mailbox (`mps3_diag_snapshot(&resp->
    diag)`) and net_proto.c emits every row of diag.h's MPS3_DIAG_FIELDS
    X-macro, so the readout is total BY CONSTRUCTION -- but only when BOTH
    halves are present, so both are required before this says "everything".
    An unrecognised shape returns the empty set, and main() FAILS on that:
    the first version of this rule returned empty for the v0.9 shape and the
    advisory list quietly became empty, which reads as "all transported"."""
    path = os.path.join(root, "firmware", "coordinator", "coordinator.c")
    proto = os.path.join(root, "firmware", "common", "net_proto.c")
    if not (os.path.isfile(path) and os.path.isfile(proto)):
        return set()
    body = func_body(open(path, errors="replace").read(), "coordinator_handle_diag")
    if not body:
        return set()
    body = strip_comments(body)
    per_field = set(re.findall(r"\b\w+\.(\w+)\s*;", body)) | \
        set(re.findall(r"=\s*\w+\.(\w+)", body))
    if per_field:
        return per_field
    snapshots_whole = re.search(r"mps3_diag_snapshot\s*\(\s*&\s*resp->diag\s*\)", body)
    emitter = strip_comments(open(proto, errors="replace").read())
    emits_every_row = re.search(r"MPS3_DIAG_FIELDS\s*\(\s*MPS3_DIAG_JSON_ROW\s*\)", emitter)
    if snapshots_whole and emits_every_row:
        return set(counters)
    return set()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=None)
    args = ap.parse_args()
    root = args.repo or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    diag_c = os.path.join(root, "firmware", "common", "diag.c")
    diag_h = os.path.join(root, "firmware", "common", "diag.h")
    for p in (diag_c, diag_h):
        if not os.path.isfile(p):
            print("FAIL: cannot find %s" % p, file=sys.stderr)
            return 1
    src = open(diag_c).read()

    init_b = func_body(src, "mps3_diag_init")
    pub_b = func_body(src, "mps3_diag_publish")
    snap_b = func_body(src, "mps3_diag_snapshot")
    if not (init_b and pub_b and snap_b):
        print("FAIL: could not locate mps3_diag_init/publish/snapshot bodies",
              file=sys.stderr)
        return 1

    fields = declared_fields(diag_h)
    counters = [f for f in fields if f not in META]
    violations: list[str] = []

    print("== Tier-1 diag mailbox gate (bug #6) ==")
    if not counters:
        # The failure mode this gate itself suffered: an empty set silently
        # satisfying every "for f in ..." check below.
        print("   VIOLATION mps3_diag_t declares NO counter fields -- either the"
              " struct moved or this gate stopped parsing it.")
        violations.append("no fields parsed")
        counters = []
    print("   mps3_diag_t declares %d fields (%d counters + %d metadata)"
          % (len(fields), len(counters), len(fields) - len(counters)))

    # ---- 1. STRUCTURE: whole-struct copies, not field lists ----------------
    if not re.search(r"memset\s*\(\s*\(?\s*(?:void\s*\*)?\s*\)?\s*&g_mps3_diag\s*,"
                     r"\s*0\s*,\s*sizeof\s*\(\s*g_mps3_diag\s*\)\s*\)",
                     strip_comments(init_b)):
        violations.append("mps3_diag_init() does not memset the WHOLE struct "
                          "-- a new field would keep whatever was in RAM")
    if not re.search(r"g_mps3_diag\s*=\s*\*\s*v\s*;", strip_comments(pub_b)):
        violations.append("mps3_diag_publish() does not do the whole-struct copy "
                          "`g_mps3_diag = *v;` -- this is exactly the field-wise "
                          "scheme that produced bug #6")
    if not re.search(r"\*\s*out\s*=\s*g_mps3_diag\s*;", strip_comments(snap_b)):
        violations.append("mps3_diag_snapshot() does not do the whole-struct copy "
                          "`*out = g_mps3_diag;` -- the 6900 readout can drift "
                          "from the mailbox again (it already did once)")

    stray_pub = sorted(field_assignments(pub_b) - META)
    stray_snap = sorted(field_assignments(snap_b) - META)
    for f in stray_pub:
        violations.append("mps3_diag_publish() assigns '%s' field-wise after the "
                          "struct copy -- only metadata %s may be re-stamped"
                          % (f, sorted(META)))
    for f in stray_snap:
        violations.append("mps3_diag_snapshot() assigns '%s' field-wise -- the "
                          "whole-struct copy is the guarantee; a field list is not"
                          % f)
    if not violations:
        print("   STRUCTURE ok: init() memsets, publish()/snapshot() copy whole "
              "structs, only %s re-stamped" % sorted(META))

    # ---- 2. REACHABILITY: transport is not production ----------------------
    produced, prod_files = producers(root)
    if not prod_files:
        violations.append("no firmware file outside firmware/test/ calls "
                          "mps3_diag_publish() -- nothing produces the mailbox")
    never = [f for f in counters if f not in produced]
    print("   producers: %s -> %d field(s) written"
          % (", ".join(prod_files) or "(none)", len(produced & set(counters))))
    for f in never:
        violations.append("'%s' is declared in mps3_diag_t but NO publisher ever "
                          "writes it -- the struct copy transports it faithfully "
                          "and it reads back 0 forever (bug #6)" % f)

    # ---- 3. READOUT COVERAGE (advisory) -----------------------------------
    read = readout_fields(root, counters)
    if not read:
        violations.append("cannot tell what the 6900 `diag` verb reads out: "
                          "coordinator_handle_diag neither copies fields one by one "
                          "nor snapshots the whole mailbox with net_proto.c emitting "
                          "every MPS3_DIAG_FIELDS row -- an unrecognised shape must "
                          "FAIL here, not read as 'all transported'")
    unread = [f for f in counters if f not in read]
    for f in unread:
        print("   ADVISORY  '%s' is published but the 6900 `diag` verb does not "
              "report it -- JTAG-only." % f)

    for v in violations:
        print("   VIOLATION %s" % v)
    if violations:
        print("\nFAIL: %d diag mailbox violation(s)." % len(violations))
        return 1
    print("\nOK: %d mailbox counters, all produced by %s, transported by "
          "whole-struct copy%s"
          % (len(counters), " + ".join(prod_files),
             " (%d JTAG-only advisory)" % len(unread) if unread
             else ", all %d on the 6900 wire" % len(read)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
