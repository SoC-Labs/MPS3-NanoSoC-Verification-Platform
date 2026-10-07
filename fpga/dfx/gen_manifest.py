#!/usr/bin/env python3
"""gen_manifest.py -- emit overlay/<rm_name>/manifest.json per
docs/contracts/overlay-manifest.md, given a built {clearing, partial} pair.

This is the one deliverable in fpga/dfx/ that is real, working Python (it
needs no FPGA / Vivado) -- everything else in this directory is a build-time
Tcl skeleton with TODO(A2) placeholders.

Schema (overlay-manifest.md):
    {
      "schema": 1,
      "static_id": "0xA1B2C3D4",
      "rm_id":     "0x0000_0001",
      "rm_name":   "nanosoc",
      "ip_class":  "arm-aaa",          # open | arm-aaa, from RM_LIB(<rm>,ip_class)
                                       # in rm_list.tcl (overlay-manifest.md v0.5)
      "clearing":  { "file": "nanosoc_clear.bin", "len": 393216, "crc32": "0x..." },
      "partial":   { "file": "nanosoc.bin",       "len": 2097152, "crc32": "0x..." },
      "ltx":       "nanosoc.ltx",      # optional
      "ltx_crc32": "0x...",            # with ltx, when --ltx-sidecar is given
      "ltx_uuids": ["8214FADE..."],    # with ltx, when --ltx-sidecar is given
      "fw":        "nanosoc_app.bin",  # optional
      "built":     "2026-07-04",
      "vivado":    "2024.1",
      "socscope_rev": "50364142d9c1"  # optional: SoCScope checkout the RM was
                                      # built from (<sha> | <sha>-dirty | unknown)
    }

Directory layout produced with --copy (overlay-manifest.md "Directory layout
(host side)"):
    overlay/<rm_name>/manifest.json
    overlay/<rm_name>/<rm>.bin           (partial)
    overlay/<rm_name>/<rm>_clear.bin     (clearing)
    overlay/<rm_name>/<rm>.ltx           (optional)
    overlay/<rm_name>/<rm>_app.bin       (optional)

Rules enforced here (overlay-manifest.md):
    - clearing + partial always ship together; refuse to emit a manifest
      missing either (this is the UltraScale-mandatory "why a set, not a
      file" rule -- a lone partial is not a valid, loadable overlay).
    - static_id must be supplied (directly or via --static-id-file, e.g. the
      sentinel build_dfx.tcl writes) -- a manifest with no static_id can't be
      validated by the host pusher against the running shell.
    - ip_class comes from rm_list.tcl, looked up by --rm-name; for a
      registered RM --ip-class may only repeat it (an unregistered one gets
      --ip-class, else arm-aaa with a warning). `verify` refuses a manifest
      whose ip_class is missing, not open|arm-aaa, or different from
      rm_list.tcl's.

Usage:
    # single RM, explicit static_id
    python3 gen_manifest.py build \\
        --rm-name nanosoc --rm-id 0x01000001 \\
        --static-id 0xA1B2C3D4 \\
        --partial build/config_rm_nanosoc_pblock_rp_dut_partial.bin \\
        --clearing build/config_rm_nanosoc_pblock_rp_dut_partial_clear.bin \\
        --vivado 2024.1 --copy --out-root overlay

    # static_id read from build_dfx.tcl's sentinel file
    python3 gen_manifest.py build --rm-name nanosoc --rm-id 0x01000001 \\
        --static-id-file build/static_id.txt \\
        --partial build/nanosoc_partial.bin --clearing build/nanosoc_clear.bin

    # round-trip check an existing manifest against its referenced files
    python3 gen_manifest.py verify overlay/nanosoc/manifest.json
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import shutil
import sys
import zlib
from pathlib import Path

SCHEMA_VERSION = 1

# --- ip_class: who may redistribute the partial (overlay-manifest.md v0.5) ----
#: "arm-aaa" = the RM's sources pull in Arm Academic Access IP (Cortex-M0/M0+,
#: CMSDK, SoC-400, anything reached through CMSDK_DIR / ARM_IP_LIBRARY_PATH /
#: the Arm IP library), so the overlay ships only in the private bundle. "open" =
#: nothing Arm-licensed in the partial. Harness Manager reads the key and shows
#: "unknown" for anything else. Decided per RM in rm_list.tcl, never here.
IP_CLASSES = ("open", "arm-aaa")

#: What an RM that rm_list.tcl does not register records (with a warning): the
#: RESTRICTIVE class. Calling Arm IP "open" would publish it; the reverse only
#: costs a private repo. check_overlay_ip_class.py refuses an unregistered RM in
#: the tree, so this default can never ship from the repo.
IP_CLASS_UNREGISTERED = "arm-aaa"

#: The one registry (the same file build_dfx.tcl sources).
RM_LIST = Path(__file__).resolve().parent / "rm_list.tcl"

# Matches:  set RM_LIB(rm_led,ip_class)  "open"   (same shape as the rm_id gates' parser)
_RM_LIB_SET_RX = re.compile(
    r'^\s*set\s+RM_LIB\(\s*(\w+)\s*,\s*(\w+)\s*\)\s+"?([^";\n]*?)"?\s*(?:;#.*)?$',
    re.MULTILINE,
)


def rm_ip_classes(rm_list: Path = RM_LIST) -> dict:
    """rm_name -> ip_class as rm_list.tcl declares it ("" when the RM declares
    none). Values are returned as written; callers validate against IP_CLASSES."""
    fields: dict = {}
    for rm_key, field, val in _RM_LIB_SET_RX.findall(Path(rm_list).read_text()):
        fields.setdefault(rm_key, {})[field] = val.strip()
    return {f["rm_name"]: f.get("ip_class", "") for f in fields.values() if "rm_name" in f}


def resolve_ip_class(rm_name: str, explicit: str | None, rm_list: Path = RM_LIST) -> str:
    """The ip_class to record for `rm_name`: rm_list.tcl's value. `explicit`
    (--ip-class) may repeat it, or name the class of an RM rm_list.tcl does not
    register; it may never contradict the registry."""
    declared = rm_ip_classes(rm_list).get(rm_name)
    if declared is not None:
        if declared not in IP_CLASSES:
            raise ValueError(
                f"{rm_list}: RM '{rm_name}' declares ip_class {declared!r}; set "
                f"RM_LIB(<rm>,ip_class) to one of {'|'.join(IP_CLASSES)}")
        if explicit and explicit != declared:
            raise ValueError(
                f"--ip-class {explicit} disagrees with {rm_list}, which says "
                f"{declared!r} for '{rm_name}' -- rm_list.tcl is the one place it is decided")
        return declared
    if explicit:
        return explicit
    print(f"warning: rm_name '{rm_name}' is not registered in {rm_list}; recording the "
          f"restrictive ip_class '{IP_CLASS_UNREGISTERED}' (register the RM, or pass "
          f"--ip-class)", file=sys.stderr)
    return IP_CLASS_UNREGISTERED


def crc32_and_len(path: Path) -> tuple[str, int]:
    """Return (crc32 as '0x'-prefixed lowercase hex, length in bytes)."""
    data = path.read_bytes()
    crc = zlib.crc32(data) & 0xFFFFFFFF
    return f"0x{crc:08x}", len(data)


def _validate_hexish(value: str, label: str) -> str:
    """Loosely validate a hex-literal-looking id string.

    overlay-manifest.md's own worked example uses two different groupings
    ("0xA1B2C3D4" for static_id, "0x0000_0001" for rm_id) -- pass whatever
    the caller supplies through unchanged rather than guessing a single
    canonical format; just check it looks like a hex literal.
    """
    v = value.strip()
    stripped = v[2:] if v.lower().startswith("0x") else v
    stripped = stripped.replace("_", "")
    if not v.lower().startswith("0x") or not stripped or any(
        c not in "0123456789abcdefABCDEF" for c in stripped
    ):
        raise ValueError(f"{label} does not look like a hex literal (e.g. 0xA1B2C3D4): {value!r}")
    return v


def _norm_hex32(value: str) -> str:
    """Normalise a 32-bit identity to canonical 0xXXXXXXXX (upper-case) form."""
    v = str(value).strip().lower()
    if v.startswith("0x"):
        v = v[2:]
    if not re.fullmatch(r"[0-9a-f]{1,8}", v):
        raise ValueError(f"not a 32-bit hex identity: {value!r}")
    return "0x" + v.rjust(8, "0").upper()


#: What --socscope-rev may record: an abbreviated/full sha, that sha with `-dirty`
#: (the tree had uncommitted changes when the RM was synthesised), or `unknown`.
#: build records the TRUTH, dirty or not -- refusing to record is how provenance
#: goes missing; the policy that a dirty or unknown rev may not SHIP lives in
#: scripts/harness_gates/check_socscope_overlay_rev.py, not here.
_SOCSCOPE_REV_RX = re.compile(r"^(?:unknown|[0-9a-f]{7,40}(?:-dirty)?)$")


def _validate_socscope_rev(value: object, label: str) -> str:
    v = str(value).strip() if isinstance(value, str) else value
    if not isinstance(v, str) or not _SOCSCOPE_REV_RX.match(v):
        raise ValueError(f"{label} must be <sha>, <sha>-dirty or unknown "
                         f"(lower-case hex, 7-40 digits): {value!r}")
    return v


def usercode_from_bitstream(path: Path) -> str:
    """Extract BITSTREAM.CONFIG.USERID from a full .bit's ASCII header.

    Vivado writes a short plaintext header before the configuration data; the
    'UserID=' token lives there. Read only the header -- these files are ~12 MB.
    An unstamped bitstream reports 0xFFFFFFFF, which is a legitimate value to
    record (it is what the hardware will report back) but a poor identity: it is
    shared by EVERY unstamped design, so callers should prefer a real stamp.
    """
    if not path.is_file():
        raise FileNotFoundError(f"--static-bit not found: {path}")
    head = path.open("rb").read(200)
    m = re.search(rb"UserID=(0[xX])?([0-9A-Fa-f]{1,8})", head)
    if not m:
        raise ValueError(f"no UserID token in the header of {path}")
    return _norm_hex32(m.group(2).decode())


def build_manifest(args: argparse.Namespace) -> dict:
    partial_path = Path(args.partial)
    clearing_path = Path(args.clearing)

    # The load-bearing rule: refuse to emit a manifest for a lone partial or
    # a lone clearing bitstream -- overlay-manifest.md is explicit that a
    # manifest missing either is invalid, because UltraScale can't load a
    # bare partial safely without its clearing bitstream having run first.
    missing = [str(p) for p in (partial_path, clearing_path) if not p.is_file()]
    if missing:
        raise FileNotFoundError(
            "clearing + partial must both exist to emit a valid overlay "
            f"(overlay-manifest.md): missing {missing}"
        )

    if args.static_id:
        static_id = _validate_hexish(args.static_id, "--static-id")
    elif args.static_id_file:
        sid_text = Path(args.static_id_file).read_text().strip()
        static_id = _validate_hexish(sid_text, f"--static-id-file ({args.static_id_file})")
    else:
        raise ValueError("one of --static-id / --static-id-file is required")

    rm_id = _validate_hexish(args.rm_id, "--rm-id")
    ip_class = resolve_ip_class(args.rm_name, getattr(args, "ip_class", None),
                                Path(getattr(args, "rm_list", None) or RM_LIST))

    clear_crc, clear_len = crc32_and_len(clearing_path)
    part_crc, part_len = crc32_and_len(partial_path)

    # Filenames recorded in the manifest are the CONTRACT names (bare,
    # relative to the manifest's own directory), not the build-tree source
    # paths -- matches the worked example in overlay-manifest.md.
    partial_name = f"{args.rm_name}.bin"
    clearing_name = f"{args.rm_name}_clear.bin"

    manifest = {
        "schema": SCHEMA_VERSION,
        "static_id": static_id,
        "rm_id": rm_id,
        "rm_name": args.rm_name,
        "ip_class": ip_class,
        "clearing": {"file": clearing_name, "len": clear_len, "crc32": clear_crc},
        "partial": {"file": partial_name, "len": part_len, "crc32": part_crc},
        "built": args.built or datetime.date.today().isoformat(),
        "vivado": args.vivado,
    }

    # static_usercode -- the identity of the static implementation these partials
    # are BOUND to, so a swap can be refused when the wrong static is flown.
    #
    # A partial is only loadable onto the exact static P&R it was built against;
    # loading it onto a different impl DESTROYS the whole FPGA configuration (this
    # happened twice on 2026-07-24). static_id CANNOT catch that: it is a CRC of
    # static_routed_locked.dcp compared against a PROVISIONED FILE on the target,
    # so it reads correct no matter what is actually flown.
    #
    # This field records BITSTREAM.CONFIG.USERID (= the 8-hex build commit) of the
    # full static bitstream, which the hardware reports back as REGISTER.USERCODE.
    # board_scripts/dfx_preflight.sh reads that register over JTAG and compares.
    # USR_ACCESS is deliberately NOT used: it carries HARNESS_VER32, which is the
    # harness *version* and is identical across different impl runs of the same
    # version -- it does not discriminate. USERID does.
    usercode = args.static_usercode
    if not usercode and args.static_bit:
        usercode = usercode_from_bitstream(Path(args.static_bit))
    if usercode:
        manifest["static_usercode"] = _norm_hex32(usercode)

    # socscope_rev -- which SoCScope checkout the RM's non-vendored RTL came from.
    # rm_socscope reads its sources from $SOCSCOPE_HOME at build time
    # (fpga/dfx/rms/rm_socscope/filelist.tcl), so without this the overlay is
    # silent about the one input that decides whether a board capture and the
    # host decoder agree. Optional (schema stays 1); `make overlays` passes it
    # for RMs it knows consume SoCScope.
    if getattr(args, "socscope_rev", None):
        manifest["socscope_rev"] = _validate_socscope_rev(args.socscope_rev, "--socscope-rev")

    if getattr(args, "ltx_sidecar", None) and not args.ltx:
        raise ValueError("--ltx-sidecar given without --ltx")
    if args.ltx:
        ltx_path = Path(args.ltx)
        if not ltx_path.is_file():
            raise FileNotFoundError(f"--ltx given but not found: {ltx_path}")
        manifest["ltx"] = f"{args.rm_name}.ltx"
        # The sidecar (fpga/dfx/tools/ltx_sidecar.py) is the record that this
        # .ltx and THIS partial came out of one routed config. An .ltx from any
        # other build mislabels every probe and nothing downstream can tell, so
        # a mismatch refuses the manifest rather than warning.
        if getattr(args, "ltx_sidecar", None):
            side = json.loads(Path(args.ltx_sidecar).read_text())
            ltx_crc, _ = crc32_and_len(ltx_path)
            if side.get("ltx_crc32") != ltx_crc:
                raise ValueError(f"--ltx {ltx_path} crc32 {ltx_crc} != "
                                 f"{side.get('ltx_crc32')} in its sidecar {args.ltx_sidecar}")
            if side.get("partial_crc32") != part_crc:
                raise ValueError(f"--partial {partial_path} crc32 {part_crc} != "
                                 f"{side.get('partial_crc32')} recorded beside the .ltx in "
                                 f"{args.ltx_sidecar}: the .ltx is not from this partial's "
                                 f"routed config")
            uuids = side.get("ila_uuids") or []
            if not uuids:
                raise ValueError(f"{args.ltx_sidecar} records no ILA uuid")
            manifest["ltx_crc32"] = ltx_crc
            manifest["ltx_uuids"] = list(uuids)

    if args.fw:
        fw_path = Path(args.fw)
        if not fw_path.is_file():
            raise FileNotFoundError(f"--fw given but not found: {fw_path}")
        manifest["fw"] = f"{args.rm_name}_app.bin"

    return manifest


def write_manifest(manifest: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest_path


def copy_artifacts(args: argparse.Namespace, out_dir: Path) -> None:
    """Copy source build artefacts into the overlay-manifest.md directory
    layout, renamed to the contract's bare filenames."""
    pairs = [
        (Path(args.partial), out_dir / f"{args.rm_name}.bin"),
        (Path(args.clearing), out_dir / f"{args.rm_name}_clear.bin"),
    ]
    if args.ltx:
        pairs.append((Path(args.ltx), out_dir / f"{args.rm_name}.ltx"))
    if args.fw:
        pairs.append((Path(args.fw), out_dir / f"{args.rm_name}_app.bin"))
    for src, dst in pairs:
        shutil.copy2(src, dst)
        print(f"  copied {src} -> {dst}")


def cmd_build(args: argparse.Namespace) -> int:
    try:
        manifest = build_manifest(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out_dir = Path(args.out_root) / args.rm_name
    if args.copy:
        out_dir.mkdir(parents=True, exist_ok=True)
        copy_artifacts(args, out_dir)

    manifest_path = write_manifest(manifest, out_dir)
    print(f"wrote {manifest_path}")
    print(json.dumps(manifest, indent=2))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text())
    base = manifest_path.parent
    ok = True

    for kind in ("clearing", "partial"):
        entry = manifest.get(kind)
        if not entry:
            print(f"error: manifest missing '{kind}' entry (invalid per overlay-manifest.md)", file=sys.stderr)
            ok = False
            continue
        f = base / entry["file"]
        if not f.is_file():
            print(f"error: {kind} file not found: {f}", file=sys.stderr)
            ok = False
            continue
        crc, length = crc32_and_len(f)
        if crc != entry["crc32"]:
            print(f"error: {kind} crc32 mismatch for {f}: manifest={entry['crc32']} actual={crc}", file=sys.stderr)
            ok = False
        if length != entry["len"]:
            print(f"error: {kind} len mismatch for {f}: manifest={entry['len']} actual={length}", file=sys.stderr)
            ok = False

    # Optional .ltx: present => the file must be beside the manifest, and when
    # the build recorded its crc32 (ltx_crc32, from the sidecar) it must match.
    if "ltx" in manifest:
        f = base / manifest["ltx"]
        if not f.is_file():
            print(f"error: ltx file not found: {f}", file=sys.stderr)
            ok = False
        elif "ltx_crc32" in manifest:
            crc, _ = crc32_and_len(f)
            if crc != manifest["ltx_crc32"]:
                print(f"error: ltx crc32 mismatch for {f}: manifest={manifest['ltx_crc32']} "
                      f"actual={crc}", file=sys.stderr)
                ok = False

    # Optional provenance field: absent is fine (every manifest before it was
    # added lacks it); present must be well-formed, or a consumer that trusts it
    # (check_socscope_overlay_rev.py) is reasoning from garbage.
    if "socscope_rev" in manifest:
        try:
            _validate_socscope_rev(manifest["socscope_rev"], "socscope_rev")
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            ok = False

    # ip_class is REQUIRED (overlay-manifest.md v0.5): an overlay that does not
    # say who may redistribute it cannot be placed in the public or the private
    # bundle, and Harness Manager shows it as "unknown". It must also still be
    # what rm_list.tcl says for this rm_name (an RM rm_list does not register is
    # checked for a legal value only).
    ip_class = manifest.get("ip_class")
    if not isinstance(ip_class, str) or ip_class not in IP_CLASSES:
        what = "missing" if ip_class is None else f"{ip_class!r} is not one of {'|'.join(IP_CLASSES)}"
        print(f"error: ip_class {what} (overlay-manifest.md; regenerate with `make -C fpga/dfx "
              f"overlays`, or stamp a published tree with fpga/dfx/tools/stamp_ip_class.py)",
              file=sys.stderr)
        ok = False
    else:
        rm_list = Path(getattr(args, "rm_list", None) or RM_LIST)
        declared = rm_ip_classes(rm_list).get(manifest.get("rm_name")) if rm_list.is_file() else None
        if declared is not None and declared != ip_class:
            print(f"error: ip_class {ip_class!r} but {rm_list} says {declared!r} for "
                  f"'{manifest.get('rm_name')}'", file=sys.stderr)
            ok = False

    if ok:
        print(f"OK: {manifest_path} -- clearing+partial present, sizes and CRCs match, "
              f"ip_class {ip_class}")
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build", help="compute crc32/len and emit manifest.json for one RM")
    p_build.add_argument("--rm-name", required=True, help="RM name, e.g. nanosoc (matches overlay/<rm_name>/)")
    p_build.add_argument("--rm-id", required=True, help="RM identity constant, e.g. 0x01000001 = {major,minor,design_id} (from rm_list.tcl)")
    id_group = p_build.add_mutually_exclusive_group(required=True)
    id_group.add_argument("--static-id", help="static shell id, e.g. 0xA1B2C3D4")
    id_group.add_argument("--static-id-file", help="path to a file containing the static_id (e.g. build_dfx.tcl's static_id.txt sentinel)")
    p_build.add_argument("--partial", required=True, help="path to the built partial .bin")
    p_build.add_argument("--clearing", required=True, help="path to the built clearing .bin")
    p_build.add_argument("--ltx", help="optional .ltx (RM-internal ILA probes)")
    p_build.add_argument("--ltx-sidecar",
                         help="config_<rm_key>.ltx.json from fpga/dfx/tools/ltx_sidecar.py: "
                              "binds --ltx to --partial by crc32 (refused on mismatch) and "
                              "records ltx_crc32 + ltx_uuids in the manifest")
    p_build.add_argument("--fw", help="optional baked/loaded DUT firmware image")
    uc_group = p_build.add_mutually_exclusive_group()
    uc_group.add_argument("--static-bit", help="full static .bit these partials were built against; its "
                                               "BITSTREAM.CONFIG.USERID is recorded as static_usercode so "
                                               "dfx_preflight.sh can refuse a swap onto the wrong static")
    uc_group.add_argument("--static-usercode", help="record static_usercode directly, e.g. 0x5263642C "
                                                    "(use --static-bit instead where the .bit is available)")
    p_build.add_argument("--socscope-rev", help="SoCScope checkout the RM was built from: <sha>, "
                                                "<sha>-dirty or unknown (recorded as socscope_rev; "
                                                "`make overlays` computes it for SoCScope-consuming RMs)")
    p_build.add_argument("--ip-class", choices=IP_CLASSES,
                         help="redistribution class; DEFAULT = RM_LIB(<rm>,ip_class) from "
                              "rm_list.tcl for --rm-name. Given for a registered RM it must "
                              "agree; for an unregistered one it names the class (else the "
                              "restrictive arm-aaa is recorded, with a warning)")
    p_build.add_argument("--rm-list", default=str(RM_LIST),
                         help="rm_list.tcl the ip_class is read from (default: %(default)s)")
    p_build.add_argument("--vivado", default="", help="Vivado version string, e.g. 2024.1")
    p_build.add_argument("--built", help="build date YYYY-MM-DD (default: today)")
    p_build.add_argument("--out-root", default="overlay", help="overlay directory root (default: overlay/)")
    p_build.add_argument("--copy", action="store_true", help="also copy partial/clearing/ltx/fw into overlay/<rm_name>/ under contract names")
    p_build.set_defaults(func=cmd_build)

    p_verify = sub.add_parser("verify", help="round-trip check an existing manifest.json against its referenced files")
    p_verify.add_argument("manifest", help="path to manifest.json")
    p_verify.add_argument("--rm-list", default=str(RM_LIST),
                          help="rm_list.tcl the manifest's ip_class must agree with "
                               "(default: %(default)s)")
    p_verify.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
