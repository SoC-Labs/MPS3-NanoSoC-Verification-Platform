#!/usr/bin/env python3
"""Gate: every overlay says who may redistribute it, and says what rm_list.tcl says.

THE FAILURE THIS EXISTS FOR. The release plan (docs/planning/BOARD_MANAGER_HARNESS_
HANDOVER.md, G1/G2/G5) ships overlays that contain Arm Academic Access IP in a PRIVATE
bundle and everything else in a public one. The split is only mechanical if every
overlay carries its class. Harness Manager reads a top-level `ip_class` from each
manifest.json and shows "unknown" for anything else, so before this gate every overlay
in the tree read "unknown".

THE ONE SOURCE. `ip_class` is decided per RM in fpga/dfx/rm_list.tcl
(`set RM_LIB(<rm>,ip_class) "open"|"arm-aaa"`), next to the RM's rm_id and synth
recipe. gen_manifest.py copies it into each manifest it builds. This gate holds the
copies to the source.

CHECKS (every failure is listed, not just the first):
  1. rm_list.tcl: every RM in RM_ORDER declares ip_class, and it is `open` or `arm-aaa`.
  2. every fpga/dfx/overlay/*/manifest.json and fpga/dfx/overlay_linux/*/manifest.json:
       - carries a top-level string `ip_class` that is `open` or `arm-aaa`;
       - names an rm_name that rm_list.tcl registers;
       - its ip_class equals rm_list.tcl's for that rm_name.
  3. A TRIPWIRE on the dangerous direction. Marking Arm IP `open` would publish it;
     marking open IP `arm-aaa` only costs a private repo. So an RM declared `open` fails
     if any NON-COMMENT line of its own sources (<wrapper_dir>/*.sv|*.v|*.tcl|*.f) names
     an Arm-IP root: CMSDK_DIR, ARM_IP_LIBRARY_PATH, ARM_CORTEXM0PLUS_IP_PATH,
     XHB500_IP_DIR, /research/AAA, SOCLABS_NANOSOC_SOC_DIR, NANOSOC_MULTICORE_HOME,
     a cmsdk_* / cortexm0* module, or rp_nanosoc_wrapper. It reads one directory and
     follows nothing, so it catches a slip, not a determined mistake: the class itself
     is a human decision, recorded with its evidence in rm_list.tcl.

No manifests (a sparse checkout) is not a failure: check 1 and 3 still run.

Exit 0 clean, 1 on any failure. Stdlib only.

    python3 check_overlay_ip_class.py [--repo ROOT]
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

#: The legal values. Keep in step with gen_manifest.py's IP_CLASSES and Harness
#: Manager's reader (harness_manager_mps3/overlays.py), which maps anything else to
#: "unknown".
IP_CLASSES = ("open", "arm-aaa")

#: Overlay roots gated, relative to the repo.
OVERLAY_ROOTS = (os.path.join("fpga", "dfx", "overlay"),
                 os.path.join("fpga", "dfx", "overlay_linux"))

#: Check 3's markers: the env vars and paths through which an RM's sources reach Arm
#: IP in this repo, plus the module names that are Arm IP or wrap it.
_ARM_MARKER_RX = re.compile(
    r"CMSDK_DIR|ARM_IP_LIBRARY_PATH|ARM_CORTEXM0PLUS_IP_PATH|XHB500_IP_DIR|/research/AAA"
    r"|SOCLABS_NANOSOC_SOC_DIR|NANOSOC_MULTICORE_HOME"
    r"|\bcmsdk_\w+|\bcortexm0\w*|\brp_nanosoc_wrapper\b",
    re.IGNORECASE)
_SCAN_SUFFIXES = (".sv", ".v", ".tcl", ".f")


def _load_rm_list_parser():
    """check_rm_id_encoding.py's rm_list.tcl parser, loaded by path: one regex for
    RM_LIB in the gates directory, not two."""
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(
        "check_rm_id_encoding", os.path.join(here, "check_rm_id_encoding.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.parse_rm_list


def _code_lines(path: str):
    """Yield (lineno, text) for the non-comment part of each line."""
    tcl = path.endswith((".tcl", ".f"))
    with open(path, errors="replace") as fh:
        for n, line in enumerate(fh, 1):
            s = line.strip()
            if tcl:
                if s.startswith("#"):
                    continue
                s = s.split(";#", 1)[0]
            else:
                if s.startswith("//") or s.startswith("*") or s.startswith("/*"):
                    continue
                s = s.split("//", 1)[0]
            if s:
                yield n, s


def arm_markers(repo: str, wrapper_dir: str) -> list[str]:
    """`file:line: marker` for every Arm-IP marker in the RM's own source files."""
    hits = []
    d = os.path.join(repo, wrapper_dir)
    if not os.path.isdir(d):
        return hits
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        if not (os.path.isfile(p) and name.endswith(_SCAN_SUFFIXES)):
            continue
        for n, s in _code_lines(p):
            m = _ARM_MARKER_RX.search(s)
            if m:
                hits.append("%s:%d: %s" % (os.path.relpath(p, repo), n, m.group(0)))
    return hits


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=None)
    args = ap.parse_args(argv)
    repo = args.repo or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    rm_list = os.path.join(repo, "fpga", "dfx", "rm_list.tcl")

    print("== overlay ip_class: manifest vs fpga/dfx/rm_list.tcl ==")
    if not os.path.isfile(rm_list):
        print("\nFAIL: %s not found" % rm_list)
        return 1

    order, lib = _load_rm_list_parser()(Path(rm_list))
    errors: list[str] = []

    # --- 1. the source ------------------------------------------------------------
    by_name: dict[str, str] = {}
    print("   %-22s %-18s %s" % ("RM", "rm_name", "ip_class"))
    for rm in order:
        ent = lib.get(rm, {})
        name = ent.get("rm_name", "")
        cls = ent.get("ip_class")
        print("   %-22s %-18s %s" % (rm, name or "?", cls if cls is not None else "<none>"))
        if cls is None:
            errors.append("%s: rm_list.tcl declares no ip_class "
                          "(set RM_LIB(%s,ip_class) \"open\"|\"arm-aaa\")" % (rm, rm))
            continue
        if cls not in IP_CLASSES:
            errors.append("%s: rm_list.tcl ip_class %r is not one of %s"
                          % (rm, cls, "|".join(IP_CLASSES)))
            continue
        if name:
            by_name[name] = cls
        # --- 3. the tripwire -----------------------------------------------------
        if cls == "open":
            for hit in arm_markers(repo, ent.get("wrapper_dir", "")):
                errors.append("%s: declared ip_class \"open\" but its own sources name "
                              "an Arm-IP root -- %s. An open overlay may be published; "
                              "if this RM pulls in Arm IP it is \"arm-aaa\"." % (rm, hit))

    # --- 2. the copies --------------------------------------------------------------
    manifests = []
    for root in OVERLAY_ROOTS:
        manifests += sorted(glob.glob(os.path.join(repo, root, "*", "manifest.json")))
    print()
    if not manifests:
        print("   SKIP manifest checks: no fpga/dfx/overlay{,_linux}/*/manifest.json")
    for m in manifests:
        rel = os.path.relpath(m, repo)
        try:
            d = json.loads(open(m).read())
        except (OSError, ValueError) as exc:
            errors.append("%s: unreadable: %s" % (rel, exc))
            continue
        if not isinstance(d, dict):
            errors.append("%s: manifest root is not a JSON object" % rel)
            continue
        name, got = d.get("rm_name"), d.get("ip_class")
        want = by_name.get(name) if isinstance(name, str) else None
        print("   %-48s %-12s rm_list: %s" % (rel, got if got is not None else "<none>",
                                              want or "?"))
        if got is None:
            errors.append("%s: no top-level ip_class (Harness Manager shows \"unknown\"). "
                          "Regenerate with `make -C fpga/dfx overlays`, or stamp a "
                          "published tree with fpga/dfx/tools/stamp_ip_class.py" % rel)
        elif not isinstance(got, str) or got not in IP_CLASSES:
            errors.append("%s: ip_class %r is not one of %s" % (rel, got, "|".join(IP_CLASSES)))
        if not isinstance(name, str) or name not in {lib[r].get("rm_name") for r in order
                                                     if r in lib}:
            errors.append("%s: rm_name %r is not registered in rm_list.tcl, so its ip_class "
                          "has no source" % (rel, name))
        elif want is not None and got in IP_CLASSES and got != want:
            errors.append("%s: ip_class %r but rm_list.tcl says %r for %s -- rm_list.tcl is "
                          "the source; regenerate the manifest" % (rel, got, want, name))

    if errors:
        print("\nFAIL: %d ip_class error(s):" % len(errors))
        for e in errors:
            print("  - %s" % e)
        return 1
    print("\nOK: %d RM(s) declare ip_class; %d manifest(s) carry it and agree with rm_list.tcl."
          % (len(order), len(manifests)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
