#!/usr/bin/env python3
"""stamp_ip_class.py -- add the top-level `ip_class` to an ALREADY-PUBLISHED overlay tree.

FOR THE CUTOVER COPY ONLY. Do NOT run this on <hub>/pv_soak/ovl/ while a soak is
running, or on any tree a live soak or Harness Manager is reading: HM asked that the
published pv_soak/ovl tree is left untouched until the cutover. Run it on the cutover
copy (e.g. a fresh copy of a <hub>/mints/<static_id>/overlay_mbv/ tree), then
publish that copy.

WHAT IT DOES. Overlays built before `ip_class` existed carry no such key, and Harness
Manager shows "unknown" for them. `make -C fpga/dfx overlays` now writes the key, but a
tree that is already published should not be regenerated just to gain one field. This
script adds the one key in place:

    <root>/<rm>/manifest.json   gains   "ip_class": "open" | "arm-aaa"

The value is NOT an argument: it comes from fpga/dfx/rm_list.tcl
(RM_LIB(<rm>,ip_class)), looked up by the manifest's own `rm_name`, through
gen_manifest.py -- the same lookup `make overlays` uses.

RULES
  * Dry run by default: prints the plan, writes nothing. `--apply` writes.
  * All or nothing. Every manifest is read and planned before the first write; any
    refusal below aborts the whole run with nothing written.
  * Refuses an rm_name that rm_list.tcl does not register (it has no class to give).
  * Refuses a manifest whose `ip_class` is already present with a DIFFERENT value:
    reclassifying an overlay is a decision, not a stamp. Fix rm_list.tcl or the tree.
  * Idempotent. A manifest that already carries the right value is left alone; a
    second run writes nothing and creates no backup directory.
  * Touches only that key. The member is inserted as text (after the "rm_name" line,
    else just after the opening brace), so every other byte of the file is unchanged;
    the result is re-parsed and must equal the original plus `ip_class`, or the run
    aborts before writing. crc32 / len / file names cannot move.
  * Backs up every file it changes, before changing it, to
    <backup-dir>/<rm>/manifest.json. The default backup dir is a SIBLING of the root
    (<root>.ip_class_bak-<UTC stamp>), so nothing new appears inside the tree that
    consumers glob. An existing backup file is never overwritten.
  * Writes atomically (temp file + rename in the same directory, mode preserved) and
    refuses a symlinked manifest.json rather than replace the link with a file.

USAGE (from a checkout of this repo: it imports gen_manifest.py and reads rm_list.tcl)
    python3 fpga/dfx/tools/stamp_ip_class.py <root>                  # plan only
    python3 fpga/dfx/tools/stamp_ip_class.py <root> --apply          # write + back up
            [--backup-dir DIR] [--rm-list fpga/dfx/rm_list.tcl]

Exit 0 = done (or nothing to do), 1 = refused (nothing written), 2 = usage.
Stdlib only (plus gen_manifest.py, its sibling). No board, no network.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DFX_DIR = HERE.parent
sys.path.insert(0, str(DFX_DIR))

# ONE rm_list.tcl lookup: the one `make overlays` uses.
from gen_manifest import IP_CLASSES, RM_LIST, rm_ip_classes  # noqa: E402

#: The top-level "rm_name" member on its own line, with its trailing comma.
_RM_NAME_LINE_RX = re.compile(
    r'^(?P<indent>[ \t]*)"rm_name"[ \t]*:[ \t]*"(?:[^"\\\n]|\\.)*"[ \t]*,[ \t]*$', re.M)


def insert_ip_class(text: str, ip_class: str) -> str:
    """Return `text` with one `"ip_class": "<v>"` member added, nothing else changed."""
    member = '"ip_class": %s' % json.dumps(ip_class)
    hits = list(_RM_NAME_LINE_RX.finditer(text))
    if len(hits) == 1:
        m = hits[0]
        return text[:m.end()] + "\n" + m.group("indent") + member + "," + text[m.end():]
    brace = text.index("{")                       # the top-level object (validated by caller)
    return text[:brace + 1] + member + ", " + text[brace + 1:]


def plan(root: Path, rm_list: Path):
    """Return (todo, ok, refusals, warnings). todo = [(path, rm_dir, new_text, cls)]."""
    classes = rm_ip_classes(rm_list)
    todo, ok, refusals, warnings = [], [], [], []
    manifests = sorted(root.glob("*/manifest.json"))
    if not manifests:
        refusals.append("no %s/*/manifest.json -- is this an overlay root?" % root)
    for path in manifests:
        rel = path.relative_to(root)
        if path.is_symlink():
            refusals.append("%s: is a symlink; stamp the file it points at instead" % rel)
            continue
        text = path.read_text(encoding="utf-8")
        try:
            d = json.loads(text)
        except ValueError as exc:
            refusals.append("%s: not valid JSON: %s" % (rel, exc))
            continue
        if not isinstance(d, dict):
            refusals.append("%s: manifest root is not a JSON object" % rel)
            continue
        name = d.get("rm_name")
        cls = classes.get(name) if isinstance(name, str) else None
        if cls is None:
            refusals.append("%s: rm_name %r is not registered in %s -- refusing an unknown RM"
                            % (rel, name, rm_list))
            continue
        if cls not in IP_CLASSES:
            refusals.append("%s: %s declares ip_class %r for %s, not one of %s"
                            % (rel, rm_list, cls, name, "|".join(IP_CLASSES)))
            continue
        if path.parent.name != name:
            warnings.append("%s: directory name %r differs from rm_name %r"
                            % (rel, path.parent.name, name))
        have = d.get("ip_class")
        if have == cls:
            ok.append((rel, cls))
            continue
        if have is not None:
            refusals.append("%s: already carries ip_class %r but rm_list.tcl says %r -- "
                            "not overwriting a classification" % (rel, have, cls))
            continue
        new = insert_ip_class(text, cls)
        after = json.loads(new)
        if after.pop("ip_class", None) != cls or after != d:
            refusals.append("%s: inserting the key would change more than the key "
                            "(unexpected layout) -- stamp it by hand" % rel)
            continue
        todo.append((path, path.parent.name, new, cls))
    return todo, ok, refusals, warnings


def write_atomic(path: Path, text: str) -> None:
    mode = path.stat().st_mode & 0o7777
    fd, tmp = tempfile.mkstemp(prefix=".manifest.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, str(path))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root", help="overlay root holding <rm>/manifest.json")
    ap.add_argument("--apply", action="store_true", help="write (default: plan only)")
    ap.add_argument("--backup-dir", default=None,
                    help="where originals go (default: <root>.ip_class_bak-<UTC stamp>)")
    ap.add_argument("--rm-list", default=str(RM_LIST), help="rm_list.tcl (default: %(default)s)")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print("stamp_ip_class: no such directory: %s" % root, file=sys.stderr)
        return 2
    todo, ok, refusals, warnings = plan(root, Path(args.rm_list))

    for rel, cls in ok:
        print("  ok       %-36s ip_class=%s (already)" % (rel, cls))
    label = "STAMP" if args.apply and not refusals else "would"
    for path, _, _, cls in todo:
        print("  %-8s %-36s ip_class=%s" % (label, path.relative_to(root), cls))
    for w in warnings:
        print("  WARNING  %s" % w)
    if refusals:
        print("stamp_ip_class: REFUSED -- nothing written:", file=sys.stderr)
        for r in refusals:
            print("  - %s" % r, file=sys.stderr)
        return 1
    if not todo:
        print("stamp_ip_class: nothing to do (%d manifest(s) already carry ip_class)" % len(ok))
        return 0
    if not args.apply:
        print("stamp_ip_class: dry run -- %d to stamp; re-run with --apply to write" % len(todo))
        return 0

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = Path(args.backup_dir) if args.backup_dir else \
        root.parent / ("%s.ip_class_bak-%s" % (root.name, stamp))
    for path, rm_dir, _, _ in todo:                  # every backup BEFORE any write
        dst = backup / rm_dir / "manifest.json"
        if dst.exists():
            print("stamp_ip_class: backup %s already exists -- refusing to overwrite it; "
                  "nothing written" % dst, file=sys.stderr)
            return 1
    for path, rm_dir, _, _ in todo:
        dst = backup / rm_dir / "manifest.json"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(path), str(dst))
    for path, _, new, _ in todo:
        write_atomic(path, new)
    print("stamp_ip_class: stamped %d, %d already correct; originals in %s"
          % (len(todo), len(ok), backup))
    return 0


if __name__ == "__main__":
    sys.exit(main())
