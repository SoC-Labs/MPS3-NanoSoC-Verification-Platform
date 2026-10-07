#!/usr/bin/env python3
"""mint_record.py -- write (or reconstruct, or check) `mint.json`: the
machine-readable record of ONE mint.

    record       from a finished DFX build dir            -> mint.json
    reconstruct  from a preserved fielded/<id>/ directory -> mint.json
    verify       a mint.json against the schema + invariants

WHY IT EXISTS
-------------
A mint produces two identities and neither one describes the run. `static_id`
is a CRC of the routed static: it says nothing about which source trees were
read, which firmware flags were baked beside it, or which RM checkpoints were
REUSED from an older tree rather than rebuilt. `docs/FIELDED_SHELL.md` carries
what is on the board, deliberately by hand. Everything in between -- the run --
was recorded nowhere.

The cost of that is on the record. `fielded/0xA8C1C535/README.md` had to
reconstruct the fielded mint from FILE TIMESTAMPS ("the shell project completed
22:28-22:42 on 08-09, the static was locked at 22:59"), and for `rm_socscope`
the chain simply runs out: no `synth.log` exists in any tree, so nothing on disk
records the SoCScope revision that RM was synthesised from. That is not a
housekeeping gap; a partial built from an unknown revision cannot be reproduced,
and cannot be trusted after the fact.

THE ONE RULE HERE: a field that cannot be established is `null` WITH THE REASON
it is null. Never a guess, never a plausible default, never silently absent.
Every provenance slot is `{"value": ..., "reason": ...}` with exactly one of the
two non-null, and `verify` fails a record that carries both -- because a value
that arrived beside an excuse is a value somebody invented.

SITE NEUTRALITY. No absolute path reaches the record: build dirs are stored
relative to the repo root, overlay rows relative to the build dir, external
source trees as a git SHA and nothing else. A record naming `/home/<someone>`
cannot be moved to the hub, committed, or read on another machine -- which is
exactly what happened to `overlay_inputs.txt`.

THREE IDENTITIES, AND THE RECORD CARRIES ALL THREE. `static_id` says "will this
partial FIT this fabric" and is a CRC of a TIMESTAMPED .dcp, so it cannot say
whether a rebuild changed anything. `static_canon` (tools/static_canon.py) is a
SHA-256 over the SOURCES that decide the static, so identical inputs give an
identical hash -- that is the question static_id structurally cannot answer.
`static_usercode` (BITSTREAM.CONFIG.USERID, which the device reports back as
REGISTER.USERCODE) is the identity of the IMPLEMENTATION RUN, so an overlay can
be traced to the exact bitstream build it was keyed to. None replaces another.

Stdlib, Python 3.7+ (the sibling tools it imports for the usercode cross-check
use `from __future__ import annotations`). No Vivado, no board, no network.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys

SCHEMA = "mps3-mint-record"
#: 1.0 -> 1.1: `static_canon` and `static_usercode` added, both as slots. A
#: record written before 1.1 has neither key; `verify` says which version it is
#: reading and holds it to that version's key set, so an older record on the hub
#: does not start failing because the tool moved on.
SCHEMA_VERSION = "1.1"
#: `reconstruct` stays at 1.0 -- see the comment where it builds its record.
RECONSTRUCTED_SCHEMA_VERSION = "1.0"
GENERATED_BY = "fpga/dfx/tools/mint_record.py"

STATIC_ID_RE = re.compile(r"^0x[0-9A-Fa-f]{8}$")
RM_ID_RE = re.compile(r"^0x[0-9A-Fa-f]{8}$")
MD5_RE = re.compile(r"^[0-9a-f]{32}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

TOP_LEVEL_KEYS_1_0 = (
    "schema", "schema_version", "record_kind", "generated_at", "generated_by",
    "static_id", "build_dir", "shell_proj", "rm_set", "sources", "firmware",
    "dcps", "artefacts", "overlay_inputs", "timestamps", "tools", "integrity",
)
TOP_LEVEL_KEYS = TOP_LEVEL_KEYS_1_0 + ("static_canon", "static_usercode")


# ---------------------------------------------------------------------------
# slots: the value/reason discipline
# ---------------------------------------------------------------------------
def known(value):
    return {"value": value, "reason": None}


def unknown(reason):
    assert reason, "an unknown field must carry the reason it is unknown"
    return {"value": None, "reason": reason}


def walk_slots(obj, prefix=""):
    if isinstance(obj, dict):
        if set(obj.keys()) == {"value", "reason"}:
            yield prefix, obj
            return
        for key, val in obj.items():
            for item in walk_slots(val, "%s.%s" % (prefix, key) if prefix else key):
                yield item
    elif isinstance(obj, list):
        for i, val in enumerate(obj):
            for item in walk_slots(val, "%s[%d]" % (prefix, i)):
                yield item


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def now_utc():
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def mtime_utc(path):
    ts = datetime.datetime.utcfromtimestamp(os.path.getmtime(path))
    return ts.replace(microsecond=0).isoformat() + "Z"


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def repo_relative(path, repo):
    """A path expressed relative to the repo root, or None if it is outside.

    Absolute paths never reach the record: a record that names one
    workstation's home directory is a record nobody else can read.
    """
    try:
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(repo))
    except ValueError:
        return None
    return None if rel.startswith("..") else rel


def git_describe(tree):
    """{'sha', 'dirty'} for a git checkout, or None if it is not one."""
    if not tree or not os.path.isdir(tree):
        return None
    try:
        sha = subprocess.check_output(
            ["git", "-C", tree, "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL).decode().strip()
        porcelain = subprocess.check_output(
            ["git", "-C", tree, "status", "--porcelain"],
            stderr=subprocess.DEVNULL).decode().strip()
    except (subprocess.CalledProcessError, OSError):
        return None
    return {"sha": sha, "dirty": bool(porcelain)}


def parse_product_block(makefile):
    """The PRODUCT=1 expansion, read from firmware/platform/Makefile itself.

    PRODUCT=1 exists because the fielded flag set used to be five literals
    inside a mint script -- "a definition that cannot be checked, reused by the
    firmware tests, or kept in step with the Makefile". Re-typing those five
    literals HERE would recreate exactly that. So the record reads the block:

        PRODUCT ?=
        ifeq ($(PRODUCT),1)
        CLCD        ?= 1
        ...
        endif

    Returns {} if the block is not found, so the caller can say so rather than
    invent a flag set.
    """
    try:
        with open(makefile) as fh:
            text = fh.read()
    except OSError:
        return {}
    match = re.search(r"^ifeq \(\$\(PRODUCT\),1\)\s*$(.*?)^endif\s*$",
                      text, re.M | re.S)
    if not match:
        return {}
    flags = {}
    for line in match.group(1).splitlines():
        found = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\?=\s*(\S+)\s*$", line)
        if found:
            flags[found.group(1)] = found.group(2)
    if flags:
        flags["PRODUCT"] = "1"
    return flags


def parse_flags(text):
    """'PRODUCT=1 CLCD=1 TOUCH=0' -> {'PRODUCT': '1', ...}"""
    flags = {}
    for token in (text or "").split():
        if "=" not in token:
            continue
        name, _, value = token.partition("=")
        flags[name] = value
    return flags


def read_overlay_inputs(path):
    """[(rm_key, rm_name, rm_id, partial, clearing)] -- comments dropped."""
    rows = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cells = line.split()
            if len(cells) != 5:
                raise SystemExit(
                    "mint_record: %s: expected 5 fields per row, got %d: %r"
                    % (path, len(cells), line))
            rows.append(cells)
    return rows


def read_static_canon(path):
    """tools/static_canon.py's static_canon.json -> the slot the record carries.

    The digest is the whole point: two mints from identical sources carry the
    IDENTICAL value here, whatever their static_ids say, and a one-bit change to
    any declared input moves it. `inputs` travels with it so the record can also
    answer WHICH file moved -- otherwise a differing digest is a fact with no
    next step.

    `linked_static_dcp` rides along but is NOT in the digest: it is a timestamped
    artefact, and hashing it would put the defect straight back.
    """
    if not path:
        return unknown(
            "no static_canon.json was passed to this record, so the CONTENT "
            "identity of this static is not written down: `static_id` alone "
            "cannot say whether this build's sources differ from the last "
            "one's (it is a CRC of a timestamped .dcp). "
            "`make -C fpga/dfx canon` produces it")
    if not os.path.isfile(path):
        return unknown(
            "static_canon.json was named for this run (%s) but does not exist, "
            "so no source-content hash was taken" % os.path.basename(path))
    with open(path) as fh:
        canon = json.load(fh)
    digest = canon.get("digest")
    if not (isinstance(digest, str) and SHA256_RE.match(digest)):
        raise SystemExit(
            "mint_record: %s carries no SHA-256 `digest` -- it is not a "
            "static_canon record and this file will not invent one" % path)
    keep = ("schema", "schema_version", "digest", "short", "part", "vivado",
            "flags", "input_count", "manifest_bytes", "inputs",
            "linked_static_dcp")
    return known({k: canon[k] for k in keep if k in canon})


def read_static_stamp(stamp_path, static_bit):
    """The config-register stamps this mint put in the boot image.

    `usercode` is BITSTREAM.CONFIG.USERID, which the device reports back as
    REGISTER.USERCODE. It is the identity of the IMPLEMENTATION RUN -- the thing
    static_id cannot discriminate, because static_id is compared against a
    PROVISIONED FILE on the target and therefore reads correct no matter which
    implementation of that design is actually flown (two of them destroyed the
    FPGA configuration on 2026-07-24).

    Two derivations. The .bit's own ASCII header is the authority; build_dfx.tcl's
    static_stamp.json is the cross-check, and a disagreement is fatal rather than
    quietly resolved in favour of one of them.
    """
    from_bit = None
    provenance = None
    if static_bit and os.path.isfile(static_bit):
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from stamp_usercode import resolve_usercode  # noqa: E402
            from_bit, provenance = resolve_usercode(static_bit, stamp_path,
                                                    allow_unstamped=True)
        except ImportError as exc:
            raise SystemExit(
                "mint_record: --static-bit was given but tools/stamp_usercode.py "
                "could not be imported (%s). The usercode is read through that "
                "ONE parser; this file will not grow a second copy of it." % exc)

    stamp = None
    if stamp_path and os.path.isfile(stamp_path):
        with open(stamp_path) as fh:
            stamp = json.load(fh)

    if from_bit is None and stamp is None:
        return unknown(
            "neither the full static bitstream nor build_dfx.tcl's "
            "static_stamp.json was passed to this record, so the USERID this "
            "build stamped -- the only identity that discriminates two "
            "IMPLEMENTATIONS of one design -- is not written down. The overlays "
            "carry it as `static_usercode`; docs/FIELDED_SHELL.md does not")

    usercode = from_bit or (stamp or {}).get("usercode")
    if usercode is None:
        return unknown(
            "static_stamp.json exists but records no usercode: this build "
            "stamped nothing (DFX_NO_VERSION_STAMP), so there is no "
            "implementation identity to record and the overlays cannot be "
            "bound to it")
    # usr_access and harness_version are the SIBLING stamp -- HARNESS_VER32, the
    # firmware-RELEASE axis -- set in the same breath as USERID and answering a
    # different question (skew, not implementation). They are nested slots so the
    # same value/reason discipline applies one level down: a build that stamped
    # no version says so, rather than carrying a null nobody can interpret.
    no_stamp_file = ("build_dfx.tcl's static_stamp.json was not passed to this "
                     "record, so only the bitstream header was read and that "
                     "carries USERID alone")
    return known({
        "usercode": usercode,
        "usr_access": (known(stamp["usr_access"])
                       if stamp and stamp.get("usr_access") else
                       unknown(no_stamp_file if stamp is None else
                               "this build set no BITSTREAM.CONFIG.USR_ACCESS "
                               "(DFX_NO_VERSION_STAMP), so the fabric carries "
                               "no harness stamp for the firmware to check "
                               "itself against")),
        "harness_version": (known(stamp["harness_version"])
                            if stamp and stamp.get("harness_version") else
                            unknown(no_stamp_file if stamp is None else
                                    "this build was made with "
                                    "DFX_NO_VERSION_STAMP, so no harness "
                                    "version was stamped into the bitstream")),
        "source": provenance or ("build_dfx.tcl static_stamp.json (the full "
                                 "bitstream was not passed, so the artefact "
                                 "itself was not cross-checked)"),
    })


def read_manifest_md5(path):
    """MANIFEST.md5 -> {filename: md5}. `md5sum -c` format, comments dropped."""
    entries = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            digest, name = parts[0], parts[1].strip().lstrip("*")
            if MD5_RE.match(digest):
                entries[name] = digest
    return entries


# ---------------------------------------------------------------------------
# record: from a live build dir
# ---------------------------------------------------------------------------
def cmd_record(args):
    build_dir = os.path.abspath(args.build_dir)
    repo = os.path.abspath(args.repo)

    static_id_txt = os.path.join(build_dir, "static_id.txt")
    inputs_txt = os.path.join(build_dir, "overlay_inputs.txt")
    for required in (static_id_txt, inputs_txt):
        if not os.path.isfile(required):
            raise SystemExit(
                "mint_record: %s not found -- point --build-dir at a finished "
                "DFX prod/ dir (`make -C fpga/dfx prod` writes both)" % required)

    with open(static_id_txt) as fh:
        static_id = fh.read().strip()
    if not STATIC_ID_RE.match(static_id):
        raise SystemExit("mint_record: %s does not hold a static_id: %r"
                         % (static_id_txt, static_id))

    # --- overlay rows, always relative to the build dir ---------------------
    rows = []
    for rm_key, rm_name, rm_id, partial, clearing in read_overlay_inputs(inputs_txt):
        rows.append({
            "rm_key": rm_key,
            "rm_name": rm_name,
            "rm_id": rm_id,
            "partial": relativize(partial, build_dir),
            "clearing": relativize(clearing, build_dir),
        })

    # --- checksums of everything the mint produced --------------------------
    dcps, artefacts = [], []
    search_dirs = [build_dir]
    if args.shell_proj and os.path.isdir(args.shell_proj):
        search_dirs.append(os.path.abspath(args.shell_proj))
    seen = set()
    for directory in search_dirs:
        for name in sorted(os.listdir(directory)):
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            if name in seen:
                continue
            entry = {"name": name, "md5": md5_of(path),
                     "bytes": os.path.getsize(path), "mtime": mtime_utc(path)}
            if name.endswith(".dcp"):
                seen.add(name)
                dcps.append(entry)
            elif name.endswith((".bit", ".mmi", ".xsa", ".bin")) and "partial" not in name:
                seen.add(name)
                artefacts.append(entry)

    # --- sources -------------------------------------------------------------
    repo_git = git_describe(repo)
    sources = {
        "repo": known(repo_git) if repo_git else
                unknown("--repo is not a git checkout, so the tree this mint was "
                        "built from cannot be named"),
    }
    for name, tree, what in (
        ("nanosoc_m0_soc", args.nanosoc_home, "the nanosoc RM source tree "
         "(SOCLABS_NANOSOC_SOC_DIR / NANOSOC_M0_SOC_HOME)"),
        ("socscope", args.socscope_home, "the SoCScope RM source tree (SOCSCOPE_HOME)"),
    ):
        described = git_describe(tree) if tree else None
        if described:
            sources[name] = known(described)
        elif tree:
            sources[name] = unknown(
                "%s was named for this run but is not a git checkout, so no "
                "revision can be recorded -- an RM built from it is not "
                "reproducible" % what)
        else:
            sources[name] = unknown(
                "%s was not passed to this record, so nothing here knows which "
                "revision was read" % what)

    # --- reused checkpoints ---------------------------------------------------
    if args.reuse_dcps_from:
        source_dir = os.path.abspath(args.reuse_dcps_from)
        reused = sorted(n for n in os.listdir(source_dir)
                        if n.endswith("_synth.dcp")) if os.path.isdir(source_dir) else []
        reuse = known({
            "from": repo_relative(source_dir, repo) or os.path.basename(source_dir),
            "checkpoints": reused,
            "note": "these RM checkpoints were COPIED IN, not synthesised by this "
                    "run; their provenance is that tree's synth logs, not this record",
        })
    else:
        reuse = unknown("no RM checkpoint was reused: every RM in rm_set was "
                        "synthesised by this run (REUSE_DCPS_FROM unset)")

    # --- firmware -------------------------------------------------------------
    # The PRODUCT=1 expansion comes from the firmware Makefile that DEFINES it;
    # --fw-flags then overrides individual entries (TOUCH, which the mint passes
    # explicitly because an explicit knob must beat PRODUCT's `?=`).
    flags = parse_product_block(args.product_makefile) if args.product_makefile else {}
    if args.product_makefile and not flags:
        raise SystemExit(
            "mint_record: no `ifeq ($(PRODUCT),1)` block found in %s -- the "
            "record will not invent a firmware flag set. Fix the path, or drop "
            "--product-makefile and pass --fw-flags explicitly."
            % args.product_makefile)
    flags.update(parse_flags(args.fw_flags))
    if flags:
        firmware_flags = known(flags)
        product = known(flags.get("PRODUCT") == "1")
    else:
        firmware_flags = unknown(
            "no firmware flag set was passed to this record; docs/FIELDED_SHELL.md's "
            "`fielded_fw_flags` row is the authority for what a fielded image "
            "was built with")
        product = unknown("the firmware flag set is not recorded, so PRODUCT "
                          "cannot be derived from it")
    if args.fw_elf and os.path.isfile(args.fw_elf):
        elf = known({"name": os.path.basename(args.fw_elf),
                     "md5": md5_of(args.fw_elf),
                     "bytes": os.path.getsize(args.fw_elf)})
    else:
        elf = unknown("no firmware ELF was passed to this record (or the path "
                      "named one that does not exist), so the image baked beside "
                      "this static is not identified")

    timestamps = {}
    for label, path in (("static_id_txt", static_id_txt),
                        ("overlay_inputs_txt", inputs_txt),
                        ("static_routed_locked_dcp",
                         os.path.join(build_dir, "static_routed_locked.dcp"))):
        if os.path.exists(path):
            timestamps[label] = mtime_utc(path)

    record = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "record_kind": "mint",
        "generated_at": now_utc(),
        "generated_by": GENERATED_BY,
        "static_id": static_id,
        "build_dir": repo_relative(build_dir, repo) or os.path.basename(build_dir),
        "shell_proj": (known(repo_relative(args.shell_proj, repo)
                             or os.path.basename(os.path.abspath(args.shell_proj)))
                       if args.shell_proj else
                       unknown("the shell project dir was not passed to this "
                               "record, so the static this DFX run linked "
                               "against is named only in the build log")),
        "rm_set": (args.rm_set or "").split(),
        # The CONTENT identity, beside (never instead of) static_id.
        "static_canon": read_static_canon(args.static_canon),
        # The IMPLEMENTATION identity: what the device reports as REGISTER.USERCODE.
        "static_usercode": read_static_stamp(args.static_stamp, args.static_bit),
        "sources": sources,
        "reused_dcps": reuse,
        "firmware": {"flags": firmware_flags, "product": product, "elf": elf},
        "dcps": dcps,
        "artefacts": artefacts,
        "overlay_inputs": rows,
        "timestamps": timestamps,
        "tools": {"vivado": (known(args.vivado_version) if args.vivado_version
                             else unknown("no Vivado version was passed to this "
                                          "record"))},
        "integrity": {},
    }
    emit(record, args.out or os.path.join(build_dir, "mint.json"))
    return 0


def relativize(path, build_dir):
    """A bitstream path as the record must carry it: relative to the build dir."""
    if not os.path.isabs(path):
        return path
    return os.path.relpath(path, build_dir)


# ---------------------------------------------------------------------------
# reconstruct: from a preserved fielded/<static_id>/ directory
# ---------------------------------------------------------------------------
def cmd_reconstruct(args):
    """Rebuild the record of a mint that predates this tool.

    HERMETIC BY DESIGN: reads exactly three files -- static_id.txt,
    overlay_inputs.txt, MANIFEST.md5 -- and nothing else. Not the ~36 MB of
    .dcp/.bit/.xsa (gitignored, absent on a fresh clone), and not
    docs/FIELDED_SHELL.md (a different authority, free to change without this
    record going stale). So it answers the same on every machine, which is what
    lets the committed result be gated by a round-trip test.
    """
    d = os.path.abspath(args.dir)
    repo = os.path.abspath(args.repo)
    static_id_txt = os.path.join(d, "static_id.txt")
    inputs_txt = os.path.join(d, "overlay_inputs.txt")
    manifest = os.path.join(d, "MANIFEST.md5")
    for required in (static_id_txt, inputs_txt, manifest):
        if not os.path.isfile(required):
            raise SystemExit("mint_record: %s not found -- reconstruct needs "
                             "static_id.txt, overlay_inputs.txt and MANIFEST.md5"
                             % required)

    with open(static_id_txt) as fh:
        static_id = fh.read().strip()

    raw_rows = read_overlay_inputs(inputs_txt)

    # Where was this built? The rows are absolute, and their common directory is
    # the only surviving statement of it. Express it from the repo root down
    # (".../fpga/dfx/build_mint/prod" -> "fpga/dfx/build_mint/prod") so the
    # answer does not depend on whose home directory the mint ran in.
    dirnames = {os.path.dirname(p) for _, _, _, p, c in raw_rows
                for p in (p, c) if os.path.isabs(p)}
    if len(dirnames) == 1:
        abs_build = dirnames.pop()
        marker = "/fpga/dfx/"
        if marker in abs_build:
            build_dir = abs_build[abs_build.index(marker) + 1:]
        else:
            build_dir = os.path.basename(abs_build)
        rows = [{"rm_key": k, "rm_name": n, "rm_id": i,
                 "partial": os.path.relpath(p, abs_build),
                 "clearing": os.path.relpath(c, abs_build)}
                for k, n, i, p, c in raw_rows]
    else:
        abs_build = None
        build_dir = None
        rows = [{"rm_key": k, "rm_name": n, "rm_id": i,
                 "partial": p, "clearing": c} for k, n, i, p, c in raw_rows]

    checksums = read_manifest_md5(manifest)
    dcps, artefacts = [], []
    for name in sorted(checksums):
        entry = {"name": name, "md5": checksums[name], "bytes": None,
                 "mtime": None}
        (dcps if name.endswith(".dcp") else artefacts).append(entry)

    def integrity_of(path, name):
        if name not in checksums:
            return "not-in-MANIFEST"
        return ("md5-matches-MANIFEST" if md5_of(path) == checksums[name]
                else "md5-DIFFERS-from-MANIFEST")

    record = {
        "schema": SCHEMA,
        # A reconstruction is a 1.0 record BY CONSTRUCTION. Neither 1.1 field is
        # reconstructible: `static_canon` is a hash of the SOURCES a mint read
        # and this mint's repo revision was never written down (sources.repo
        # says so), and `static_usercode` lives in the header of a gitignored
        # 11 MB .bit that a fresh checkout does not have -- while this command
        # reads exactly three tracked text files, on purpose, so its output is
        # the same on every machine. Emitting them as permanent `unknown`s would
        # churn every preserved fielded record for two fields that can never be
        # anything else. `verify` holds a 1.0 record to the 1.0 key set.
        "schema_version": RECONSTRUCTED_SCHEMA_VERSION,
        "record_kind": "reconstructed",
        "generated_at": now_utc(),
        "generated_by": GENERATED_BY,
        "static_id": static_id,
        "build_dir": build_dir,
        "shell_proj": unknown(
            "not recorded: the two shell-project artefacts in MANIFEST.md5 "
            "(shell_static_synth.dcp, shell_harness.xsa) name no project dir, "
            "and the mint predates this record"),
        "rm_set": [row["rm_key"] for row in rows],
        "sources": {
            "repo": unknown(
                "the mint predates mint.json: the repo revision it was built "
                "from was never written down. docs/FIELDED_SHELL.md's "
                "`fielded_by` records the commit that FIELDED this shell, which "
                "is a different fact"),
            "nanosoc_m0_soc": unknown(
                "the external-source RM checkpoints were REUSED, not rebuilt, in "
                "this mint; the only record of which trees produced them is the "
                "rm_*_synth/synth.log of the tree they were copied from, which "
                "is gitignored scratch (see this directory's README, "
                "\"Provenance\")"),
            "socscope": unknown(
                "no synth.log for rm_socscope exists in any tree -- its "
                "checkpoint was copied in from a build dir that kept none. "
                "Nothing on disk records the SoCScope revision it was "
                "synthesised from; the provenance chain runs out here"),
        },
        "reused_dcps": known({
            "from": None,
            "checkpoints": [],
            "note": "four external-source RM checkpoints (nanosoc, eth_ss, "
                    "nanosoc_multicore, socscope) were verified byte-identical to "
                    "an earlier build tree's and were copied in, not rebuilt. The "
                    "tree is named in this directory's README; it is gitignored, "
                    "so it is not named here as a path",
        }),
        "firmware": {
            "flags": unknown(
                "not recorded in the build dir: the mint's firmware flag set was "
                "never written beside its artefacts. docs/FIELDED_SHELL.md's "
                "`fielded_fw_flags` row is the authority for what the fielded "
                "image was built with"),
            "product": unknown(
                "the firmware flag set is not recorded here, so PRODUCT cannot "
                "be derived from it"),
            "elf": unknown(
                "the firmware ELF is not among the preserved artefacts; what was "
                "flashed is the greybox base bitstream with the ELF already "
                "baked in by updatemem"),
        },
        "dcps": dcps,
        "artefacts": artefacts,
        "overlay_inputs": rows,
        "timestamps": {},
        "tools": {"vivado": unknown(
            "the Vivado version was not recorded beside the artefacts; the "
            "overlay manifests generated from this mint carry it")},
        "integrity": {
            "static_id_txt": integrity_of(static_id_txt, "static_id.txt"),
            "overlay_inputs_txt": integrity_of(inputs_txt, "overlay_inputs.txt"),
            "note": "computed from the two tracked text files against this "
                    "directory's own MANIFEST.md5; the binaries are gitignored "
                    "and are checked by verify_fielded.sh",
        },
    }
    emit(record, args.out or os.path.join(d, "mint.json"))
    return 0


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------
def cmd_verify(args):
    with open(args.record) as fh:
        rec = json.load(fh)
    problems = []

    if rec.get("schema") != SCHEMA:
        problems.append("schema is %r, want %r" % (rec.get("schema"), SCHEMA))
    # A record written by an older tool is held to ITS key set, not this one's:
    # a mint.json sitting on the hub must not start failing because the tool
    # grew a field after that mint ran.
    version = str(rec.get("schema_version", ""))
    required = TOP_LEVEL_KEYS_1_0 if version == "1.0" else TOP_LEVEL_KEYS
    if version not in ("1.0", SCHEMA_VERSION):
        problems.append("schema_version %r is not 1.0 or %s"
                        % (version, SCHEMA_VERSION))
    for key in required:
        if key not in rec:
            problems.append("missing required key %r" % key)
    if not STATIC_ID_RE.match(str(rec.get("static_id", ""))):
        problems.append("static_id %r is not 0xXXXXXXXX" % rec.get("static_id"))
    if rec.get("record_kind") not in ("mint", "reconstructed"):
        problems.append("record_kind %r is not mint|reconstructed"
                        % rec.get("record_kind"))

    for path, slot in walk_slots(rec):
        has_value = slot["value"] is not None
        has_reason = slot["reason"] is not None
        if has_value and has_reason:
            problems.append(
                "%s carries BOTH a value and a reason -- a value that arrived "
                "beside an excuse is a value somebody invented" % path)
        elif not has_value and not has_reason:
            problems.append("%s is null with no reason -- say WHY it is unknown"
                            % path)

    for row in rec.get("overlay_inputs", []):
        for field in ("partial", "clearing"):
            value = row.get(field, "")
            if os.path.isabs(value):
                problems.append(
                    "%s.%s is an absolute path (%s) -- overlay rows must be "
                    "relative to the build dir or the record cannot travel with "
                    "the artefacts" % (row.get("rm_key"), field, value))
        if not RM_ID_RE.match(str(row.get("rm_id", ""))):
            problems.append("%s.rm_id %r is not 0xXXXXXXXX"
                            % (row.get("rm_key"), row.get("rm_id")))

    for entry in rec.get("dcps", []) + rec.get("artefacts", []):
        if not MD5_RE.match(str(entry.get("md5", ""))):
            problems.append("%s has no md5" % entry.get("name"))

    # static_canon: a digest that is not a SHA-256 is not a content hash, and a
    # digest computed over a timestamped artefact is the defect this field was
    # added to remove -- so the artefact that IS recorded beside it must say, in
    # the record itself, that it was not hashed in.
    canon = (rec.get("static_canon") or {}).get("value")
    if canon is not None:
        if not SHA256_RE.match(str(canon.get("digest", ""))):
            problems.append("static_canon.digest %r is not a 64-hex SHA-256"
                            % canon.get("digest"))
        if not canon.get("input_count"):
            problems.append("static_canon records no inputs -- a hash over "
                            "nothing is not a content identity")
        linked = canon.get("linked_static_dcp")
        if linked is not None and "NOT HASHED" not in str(linked.get("note", "")):
            problems.append(
                "static_canon.linked_static_dcp carries no note saying it is "
                "NOT HASHED into the digest -- a timestamped artefact beside a "
                "content hash must say which side of the line it is on")

    stamp = (rec.get("static_usercode") or {}).get("value")
    if stamp is not None and not STATIC_ID_RE.match(str(stamp.get("usercode", ""))):
        problems.append("static_usercode.usercode %r is not 0xXXXXXXXX"
                        % stamp.get("usercode"))

    blob = json.dumps(rec)
    for needle in ("/home/", "/Users/"):
        if needle in blob:
            problems.append("a home directory (%s) survived into the record"
                            % needle)

    if problems:
        sys.stderr.write("mint_record: %s FAILED %d check(s):\n"
                         % (args.record, len(problems)))
        for problem in problems:
            sys.stderr.write("  - %s\n" % problem)
        return 1
    print("mint_record: %s OK (static_id %s, %d overlay rows, %d dcps)"
          % (args.record, rec["static_id"], len(rec["overlay_inputs"]),
             len(rec["dcps"])))
    return 0


def emit(record, out):
    with open(out, "w") as fh:
        json.dump(record, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print("mint_record: wrote %s (static_id %s, %d overlay rows)"
          % (out, record["static_id"], len(record["overlay_inputs"])))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd")

    rec = sub.add_parser("record", help="write mint.json from a DFX build dir")
    rec.add_argument("--build-dir", required=True, help="the DFX prod/ dir")
    rec.add_argument("--repo", default=os.getcwd())
    rec.add_argument("--shell-proj", default=None)
    rec.add_argument("--fw-flags", default=None,
                     help='e.g. "PRODUCT=1 CLCD=1 TOUCH=1"; overrides '
                          '--product-makefile entry by entry')
    rec.add_argument("--product-makefile", default=None,
                     help="firmware/platform/Makefile -- the PRODUCT=1 flag set "
                          "is READ from it, never re-typed here")
    rec.add_argument("--fw-elf", default=None)
    rec.add_argument("--rm-set", default=None)
    rec.add_argument("--nanosoc-home", default=None)
    rec.add_argument("--socscope-home", default=None)
    rec.add_argument("--reuse-dcps-from", default=None)
    rec.add_argument("--static-canon", default=None,
                     help="tools/static_canon.py's static_canon.json -- the "
                          "SHA-256 over the sources that decide this static "
                          "(`make -C fpga/dfx canon`)")
    rec.add_argument("--static-stamp", default=None,
                     help="build_dfx.tcl's static_stamp.json (USERID + "
                          "USR_ACCESS as the design reported them)")
    rec.add_argument("--static-bit", default=None,
                     help="the FULL static bitstream; its header's UserID= is "
                          "the AUTHORITY for static_usercode and is "
                          "cross-checked against --static-stamp")
    rec.add_argument("--vivado-version", default=None)
    rec.add_argument("-o", "--out", default=None)
    rec.set_defaults(func=cmd_record)

    rc = sub.add_parser("reconstruct",
                        help="write mint.json from a preserved fielded/<id>/ dir")
    rc.add_argument("--dir", required=True)
    rc.add_argument("--repo", default=os.getcwd())
    rc.add_argument("-o", "--out", default=None)
    rc.set_defaults(func=cmd_reconstruct)

    vf = sub.add_parser("verify", help="check a mint.json")
    vf.add_argument("record")
    vf.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
