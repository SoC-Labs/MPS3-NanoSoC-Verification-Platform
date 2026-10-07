#!/usr/bin/env python3
"""ltx_sidecar.py -- the declared-vs-produced gate for RM-internal ILAs, and the
small record that binds each partial .ltx to the partial it describes.

    gate   --build-dir <prod> --rm-keys "<k> ..." --debug-keys "<k> ..."
           For every RM in --rm-keys:
             * declared debug (RM_LIB(<rm>,debug) 1)  -> config_<rm>.ltx MUST exist;
             * NOT declared                            -> config_<rm>.ltx MUST NOT;
           and for every .ltx that does exist: it parses, names ONLY cores under
           the RP cell, holds a debug hub and at least one ILA, and its ILA UUIDs
           agree with Vivado's report_debug_core. No two debug RMs may share an
           ILA uuid -- counting every config_<rm>.ltx.json already in the build
           dir, so add-rm is held to the RMs built before it (findings #6).
           Then it writes the sidecar config_<rm>.ltx.json (rm_key, ILA uuids,
           and the crc32 of BOTH the .ltx and the partial .bin it was written
           beside); an RM refused for a shared uuid gets no sidecar.
           With --check the sidecar is not written; it must already exist and
           still match the files on disk (what `make overlays` runs).

    static --build-dir <prod> --ref-key rm_greybox --shell-cpu mb|mbv [--check]
           The STATIC's probes file, config_<ref>_static.ltx (tools/
           debug_probes.tcl write_static_debug_probes; FLOW_CONTRACT §6.3):
             * it exists  <=>  debug_core_<ref>_static.rpt lists debug cores;
             * mb  -> the static holds NONE, so the .ltx must NOT exist;
               mbv -> exactly one static hub (XSDB_V*) + exactly one DDR4 MIG
                      slave (ipName DDR4_SDRAM);
             * every core in it is static (no reconfigTop, nothing under the RP);
             * its uuids == report_debug_core's.
           Then it writes config_<ref>_static.ltx.json binding the .ltx to the
           full static .bit (sha256 + UserID) and static_id.txt. With --check
           the sidecar must already exist and still match.

WHY A SIDECAR. An .ltx is only right for the routed config it was written from:
rebuild an RM without re-staging its .ltx and every probe is mislabelled, with
nothing to say so (handover trap 5). Both files come out of one Vivado session
in build_dfx.tcl; the sidecar writes down the pair's checksums straight after,
so a later stage can refuse a partial and an .ltx that did not come out of the
same routed config.

Python 3.8-compatible (the build host's python3 and Vivado's bundled one are 3.8).
Stdlib only. Exit 0 = pass, 1 = gate failure, 2 = usage.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zlib

RP_PBLOCK = "pblock_rp_dut"
SCHEMA = 1

#: report_debug_core prints each ILA's UUID alone in a one-cell table row.
_UUID_ROW = re.compile(r"^\|\s*([0-9A-Fa-f]{32})\s*\|\s*$")


def crc32_of(path):
    crc = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            crc = zlib.crc32(chunk, crc)
    return "0x%08x" % (crc & 0xFFFFFFFF)


def paths_for(build_dir, rm_key):
    return {
        "ltx": os.path.join(build_dir, "config_%s.ltx" % rm_key),
        "sidecar": os.path.join(build_dir, "config_%s.ltx.json" % rm_key),
        "rpt": os.path.join(build_dir, "debug_core_%s.rpt" % rm_key),
        "partial": os.path.join(build_dir, "config_%s_%s_partial.bin" % (rm_key, RP_PBLOCK)),
    }


def parse_ltx(path, rp_inst):
    """-> (cores, errors). cores = [{name, type, uuid}] from every probeset."""
    try:
        with open(path) as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        return [], ["%s does not parse as an .ltx (JSON): %s" % (path, exc)]
    data = (doc.get("ltx_root") or {}).get("ltx_data")
    if not isinstance(data, list) or not data:
        return [], ["%s has no ltx_root.ltx_data" % path]
    errors = []
    cores = []
    prefix = rp_inst + "/"
    for ps in data:
        cell = ps.get("cellName")
        if cell is not None and cell != rp_inst:
            errors.append("%s: probeset cellName %r is not the RP cell %r"
                          % (path, cell, rp_inst))
        for c in ps.get("debug_cores") or []:
            name = c.get("name", "")
            cores.append({"name": name, "type": c.get("type", ""),
                          "uuid": (c.get("uuid") or "").upper() or None})
            if not name.startswith(prefix):
                errors.append("%s names core %r OUTSIDE %s -- a partial .ltx may "
                              "describe only the RM's own cores" % (path, name, rp_inst))
            top = c.get("reconfigTop")
            if top is not None and top != rp_inst:
                errors.append("%s: core %r has reconfigTop %r, not %r"
                              % (path, name, top, rp_inst))
    if not cores:
        errors.append("%s lists no debug cores at all" % path)
        return cores, errors
    hubs = [c for c in cores if c["type"].startswith("XSDB")]
    ilas = [c for c in cores if c["type"].startswith("ILA")]
    if not hubs:
        errors.append("%s has no debug hub (XSDB) -- an ILA in an RP needs the RM's "
                      "own mode-1 bridge (handover trap 2)" % path)
    if not ilas:
        errors.append("%s has a hub but no ILA" % path)
    for c in ilas:
        if not c["uuid"]:
            errors.append("%s: ILA %r carries no uuid" % (path, c["name"]))
    return cores, errors


def uuids_from_report(path):
    out = []
    with open(path) as fh:
        for line in fh:
            m = _UUID_ROW.match(line.rstrip("\n"))
            if m:
                out.append(m.group(1).upper())
    return out


def build_sidecar(build_dir, rm_key, rp_inst):
    p = paths_for(build_dir, rm_key)
    cores, errors = parse_ltx(p["ltx"], rp_inst)
    if not os.path.isfile(p["partial"]):
        errors.append("no partial beside the .ltx: %s" % p["partial"])
    ltx_uuids = sorted(c["uuid"] for c in cores
                       if c["type"].startswith("ILA") and c["uuid"])
    if os.path.isfile(p["rpt"]):
        rpt_uuids = set(uuids_from_report(p["rpt"]))
        missing = [u for u in ltx_uuids if u not in rpt_uuids]
        if missing:
            errors.append("ILA uuid(s) %s are in %s but NOT in report_debug_core (%s) -- "
                          "the .ltx is not from this routed config"
                          % (missing, p["ltx"], p["rpt"]))
    else:
        errors.append("no report_debug_core output %s (build_dfx.tcl writes it beside "
                      "the .ltx)" % p["rpt"])
    if errors:
        return None, errors
    return {
        "schema": SCHEMA,
        "rm_key": rm_key,
        "rp_inst": rp_inst,
        "ltx": os.path.basename(p["ltx"]),
        "ltx_crc32": crc32_of(p["ltx"]),
        "ltx_len": os.path.getsize(p["ltx"]),
        "partial": os.path.basename(p["partial"]),
        "partial_crc32": crc32_of(p["partial"]),
        "ila_uuids": ltx_uuids,
        "cores": [{"name": c["name"], "type": c["type"]} for c in cores],
    }, []


def check_sidecar(build_dir, rm_key):
    p = paths_for(build_dir, rm_key)
    if not os.path.isfile(p["sidecar"]):
        return ["%s is missing: the .ltx was never gated (mint stage 4 / add-rm "
                "writes it)" % p["sidecar"]]
    try:
        with open(p["sidecar"]) as fh:
            side = json.load(fh)
    except ValueError as exc:
        return ["%s does not parse: %s" % (p["sidecar"], exc)]
    errors = []
    if side.get("rm_key") != rm_key:
        errors.append("%s says rm_key %r, not %r"
                      % (p["sidecar"], side.get("rm_key"), rm_key))
    for kind in ("ltx", "partial"):
        f = p[kind]
        if not os.path.isfile(f):
            errors.append("%s is missing" % f)
            continue
        got = crc32_of(f)
        if got != side.get(kind + "_crc32"):
            errors.append("%s crc32 %s != %s recorded in %s -- the .ltx and the partial "
                          "no longer come from the same routed config"
                          % (f, got, side.get(kind + "_crc32"), p["sidecar"]))
    if not side.get("ila_uuids"):
        errors.append("%s records no ILA uuid" % p["sidecar"])
    return errors


# ---------------------------------------------------------------------------
# the static's probes file
# ---------------------------------------------------------------------------
#: report_debug_core's table of contents: "1.2 ddr4_0: (xilinx_DDR4-SDRAM_v2, ...)"
_RPT_CORE = re.compile(r"^1\.\d+\s+(\S+):\s+\(")


def static_paths(build_dir, ref_key):
    base = os.path.join(build_dir, "config_%s_static.ltx" % ref_key)
    return {"ltx": base, "sidecar": base + ".json",
            "rpt": os.path.join(build_dir, "debug_core_%s_static.rpt" % ref_key),
            "bit": os.path.join(build_dir, "config_%s.bit" % ref_key),
            "dcp": os.path.join(build_dir, "config_%s_routed.dcp" % ref_key),
            "static_id": os.path.join(build_dir, "static_id.txt")}


def cores_from_report(path):
    """-> (core leaf names, uuids) listed by one report_debug_core file."""
    names, seen = [], set()
    with open(path) as fh:
        for line in fh:
            m = _RPT_CORE.match(line.strip())
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                names.append(m.group(1))
    return names, sorted(set(uuids_from_report(path)))


def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _userid(bit):
    with open(bit, "rb") as fh:
        m = re.search(rb"UserID=(0[xX])?([0-9A-Fa-f]{1,8})", fh.read(512))
    return ("0x%08X" % int(m.group(2), 16)) if m else None


def check_static(build_dir, ref_key, rp_inst, shell_cpu):
    """-> (sidecar dict | None when no .ltx is due, errors)."""
    p = static_paths(build_dir, ref_key)
    errors = []
    if not os.path.isfile(p["rpt"]):
        return None, ["no %s: build_dfx.tcl writes report_debug_core for the reference "
                      "config ALWAYS (tools/debug_probes.tcl write_static_debug_probes); "
                      "without it the static .ltx cannot be held to anything" % p["rpt"]]
    rpt_names, rpt_uuids = cores_from_report(p["rpt"])
    has = os.path.isfile(p["ltx"])
    if shell_cpu == "mb":
        if rpt_names:
            errors.append("the bare-metal (mb) static holds debug cores %s -- its BD forbids a "
                          "static debug slave (shell_bd.tcl, debug_bridge_0): a static ILA/VIO "
                          "punches ports into the RP" % rpt_names)
        if has:
            errors.append("%s exists for a bare-metal (mb) static -- it must NOT" % p["ltx"])
        return None, errors
    if not rpt_names:
        errors.append("the %s static holds NO debug core -- the MicroBlaze V static must carry "
                      "SEAM-8's mig_dbg_hub + the DDR4 calibration slave" % shell_cpu)
    if bool(rpt_names) != has:
        errors.append("%s %s but report_debug_core lists %s -- the static .ltx exists iff the "
                      "static holds debug cores" % (p["ltx"], "exists" if has else "is missing",
                                                    rpt_names or "no core"))
    if not has or errors:
        return None, errors
    try:
        with open(p["ltx"]) as fh:
            doc = json.load(fh)
        data = doc["ltx_root"]["ltx_data"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return None, ["%s does not parse as an .ltx: %s" % (p["ltx"], exc)]
    cores = []
    for ps in data:
        for c in ps.get("debug_cores") or []:
            cores.append({k: c[k] for k in ("name", "type", "ipName", "uuid") if k in c})
            if c.get("reconfigTop") or str(c.get("name", "")).startswith(rp_inst + "/"):
                errors.append("%s names RP core %r -- the static .ltx may describe only "
                              "static cores" % (p["ltx"], c.get("name")))
    hubs = [c for c in cores if str(c.get("type", "")).startswith("XSDB_V")]
    migs = [c for c in cores if c.get("ipName") == "DDR4_SDRAM"]
    if len(hubs) != 1:
        errors.append("%s has %d static debug hubs, want exactly 1 (SEAM-8's mig_dbg_hub)"
                      % (p["ltx"], len(hubs)))
    if len(migs) != 1:
        errors.append("%s has %d DDR4 MIG slaves, want exactly 1" % (p["ltx"], len(migs)))
    if len(cores) != len(rpt_names):
        errors.append("%s lists %d cores, report_debug_core %d (%s)"
                      % (p["ltx"], len(cores), len(rpt_names), rpt_names))
    ltx_uuids = sorted(str(c["uuid"]).upper() for c in cores if c.get("uuid"))
    if ltx_uuids != rpt_uuids:
        errors.append("uuids %s in %s != report_debug_core's %s -- the .ltx is not from this "
                      "routed static" % (ltx_uuids, p["ltx"], rpt_uuids))
    for key in ("bit", "static_id"):
        if not os.path.isfile(p[key]):
            errors.append("%s missing: the sidecar binds the .ltx to it" % p[key])
    if errors:
        return None, errors
    return {
        "schema": SCHEMA,
        "kind": "static",
        "ref_key": ref_key,
        "shell_cpu": shell_cpu,
        "ltx": os.path.basename(p["ltx"]),
        "ltx_crc32": crc32_of(p["ltx"]),
        "ltx_len": os.path.getsize(p["ltx"]),
        "ltx_sha256": _sha256(p["ltx"]),
        "report": os.path.basename(p["rpt"]),
        "static_id": open(p["static_id"]).read().strip(),
        "static_bit": os.path.basename(p["bit"]),
        "static_bit_sha256": _sha256(p["bit"]),
        "static_usercode": _userid(p["bit"]),
        "uuids": ltx_uuids,
        "cores": cores,
        "note": "the STATIC's debug cores; valid whatever RM is loaded (the static is "
                "locked). The flashable base carries the same UserID (stage0_bake.py / "
                "the MB bake verify it), which is what binds this file to it.",
    }, []


def cmd_static(args):
    p = static_paths(args.build_dir, args.ref_key)
    side, errors = check_static(args.build_dir, args.ref_key, args.rp_inst, args.shell_cpu)
    if not errors and side is not None and args.check:
        try:
            with open(p["sidecar"]) as fh:
                old = json.load(fh)
        except (OSError, ValueError) as exc:
            errors.append("%s missing or unreadable (%s): the static .ltx was never gated"
                          % (p["sidecar"], exc))
        else:
            for k in ("ltx_crc32", "static_bit_sha256", "static_id", "uuids"):
                if old.get(k) != side.get(k):
                    errors.append("%s: %s %r != %r on disk -- the .ltx and the static no "
                                  "longer match" % (p["sidecar"], k, old.get(k), side.get(k)))
    if errors:
        for e in errors:
            print("DFX_LTX_GATE_FAILED static(%s): %s" % (args.ref_key, e))
        return 1
    if side is None:
        if os.path.isfile(p["sidecar"]) and not args.check:
            os.remove(p["sidecar"])
        print("DFX_STATIC_LTX_GATE_OK ref=%s cpu=%s: no static debug core, no static .ltx "
              "(as required)" % (args.ref_key, args.shell_cpu))
        return 0
    if not args.check:
        with open(p["sidecar"], "w") as fh:
            json.dump(side, fh, indent=2)
            fh.write("\n")
    print("DFX_STATIC_LTX_GATE_OK ref=%s cpu=%s: %d static core(s) %s -> %s"
          % (args.ref_key, args.shell_cpu, len(side["cores"]),
             ",".join(side["uuids"]), p["sidecar"]))
    return 0


def keys_from_overlay_inputs(path):
    """rm_keys of every row of an overlay_inputs.txt (column 1; comments skipped)."""
    keys = []
    with open(path) as fh:
        for line in fh:
            cells = line.split()
            if cells and not cells[0].startswith("#"):
                keys.append(cells[0])
    return keys


def shared_uuid_failures(build_dir, fresh):
    """Two debug RMs must never carry the same ILA uuid. -> (messages, rm_keys).

    `fresh` = {rm_key: ila_uuids} derived in THIS run. Every other RM's
    config_<rm>.ltx.json already in build_dir is counted too, so `make add-rm`
    (one key per run) is held to the RMs built before it. The static's sidecar
    (config_*_static.ltx.json) is not an RM and is skipped.

    Why (findings #6): the 2026-09-23 ILA mint's dbg_demo and nanosoc_ila both
    named their ILA u_rp_dut/u_ila and both got uuid B18B3F67...2FC9. hw_server
    then cannot tell the two RMs' probe files apart. Name each ILA instance
    distinctly (u_ila_<rm>)."""
    owners = {}
    for k, uuids in fresh.items():
        for u in uuids:
            owners.setdefault(u, set()).add(k)
    try:
        names = sorted(os.listdir(build_dir))
    except OSError:
        names = []
    for n in names:
        if not (n.startswith("config_") and n.endswith(".ltx.json")) or n.endswith("_static.ltx.json"):
            continue
        try:
            with open(os.path.join(build_dir, n)) as fh:
                side = json.load(fh)
        except (OSError, ValueError):
            continue  # check_sidecar reports an unparseable sidecar of a gated RM
        k = side.get("rm_key")
        if not k or k in fresh:
            continue  # this run's derivation replaces the RM's old sidecar
        for u in side.get("ila_uuids") or []:
            owners.setdefault(str(u).upper(), set()).add(k)
    msgs, keys = [], set()
    for u, ks in sorted(owners.items()):
        if len(ks) > 1:
            keys |= ks
            msgs.append("ILA uuid %s is shared by debug RMs %s -- the UUID cannot tell their "
                        ".ltx files apart; give each RM's ILA a distinct instance name "
                        "(u_ila_<rm>) and rebuild" % (u, ", ".join(sorted(ks))))
    return msgs, keys


def cmd_gate(args):
    if args.overlay_inputs:
        rm_keys = keys_from_overlay_inputs(args.overlay_inputs)
    else:
        rm_keys = args.rm_keys.split()
    debug = set(args.debug_keys.split())
    failures = []
    fresh = {}      # rm_key -> ila_uuids derived this run
    pending = {}    # rm_key -> sidecar to write once the cross-RM check passes
    for rm_key in rm_keys:
        p = paths_for(args.build_dir, rm_key)
        has = os.path.isfile(p["ltx"])
        if rm_key in debug and not has:
            failures.append("%s is declared debug 1 (rm_list.tcl) but produced no %s"
                            % (rm_key, p["ltx"]))
            continue
        if rm_key not in debug and has:
            failures.append("%s is NOT declared debug 1 but %s exists -- an ILA the "
                            "registry does not know about" % (rm_key, p["ltx"]))
            continue
        if not has:
            print("   ltx: %-24s debug 0, no .ltx (as declared)" % rm_key)
            continue
        if args.check:
            errs = check_sidecar(args.build_dir, rm_key)
            side = None
            if not errs:
                side, errs = build_sidecar(args.build_dir, rm_key, args.rp_inst)
            if errs:
                failures.extend(errs)
            else:
                fresh[rm_key] = side["ila_uuids"]
                print("   ltx: %-24s OK (sidecar matches .ltx + partial)" % rm_key)
            continue
        side, errs = build_sidecar(args.build_dir, rm_key, args.rp_inst)
        if errs:
            failures.extend(errs)
            continue
        fresh[rm_key] = side["ila_uuids"]
        pending[rm_key] = side
    shared, blocked = shared_uuid_failures(args.build_dir, fresh)
    failures.extend(shared)
    # An RM whose uuid another debug RM already carries gets NO sidecar, so
    # `make overlays` refuses it for that reason as well.
    for rm_key, side in sorted(pending.items()):
        if rm_key in blocked:
            continue
        p = paths_for(args.build_dir, rm_key)
        with open(p["sidecar"], "w") as fh:
            json.dump(side, fh, indent=2)
            fh.write("\n")
        print("   ltx: %-24s %d ILA(s) %s -> %s"
              % (rm_key, len(side["ila_uuids"]), ",".join(side["ila_uuids"]), p["sidecar"]))
    if failures:
        for f in failures:
            print("DFX_LTX_GATE_FAILED %s" % f)
        return 1
    print("DFX_LTX_GATE_OK rms=%d debug=%d"
          % (len(rm_keys), len([k for k in rm_keys if k in debug])))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd")
    g = sub.add_parser("gate", help="declared-vs-produced .ltx gate (+ write sidecars)")
    g.add_argument("--build-dir", required=True)
    src = g.add_mutually_exclusive_group(required=True)
    src.add_argument("--rm-keys", help="space-separated rm_keys built into this dir")
    src.add_argument("--overlay-inputs",
                     help="take the rm_keys from this overlay_inputs.txt instead")
    g.add_argument("--debug-keys", default="",
                   help="space-separated rm_keys declared debug 1")
    g.add_argument("--rp-inst", default="u_rp_dut")
    g.add_argument("--check", action="store_true",
                   help="verify existing sidecars against the files; write nothing")
    st = sub.add_parser("static", help="the static's probes file: two-way gate + sidecar")
    st.add_argument("--build-dir", required=True)
    st.add_argument("--ref-key", default="rm_greybox")
    st.add_argument("--rp-inst", default="u_rp_dut")
    st.add_argument("--shell-cpu", required=True, choices=("mb", "mbv"))
    st.add_argument("--check", action="store_true",
                    help="verify the existing sidecar against the files; write nothing")
    args = ap.parse_args(argv)
    if args.cmd == "static":
        return cmd_static(args)
    if args.cmd != "gate":
        ap.print_help()
        return 2
    return cmd_gate(args)


if __name__ == "__main__":
    sys.exit(main())
