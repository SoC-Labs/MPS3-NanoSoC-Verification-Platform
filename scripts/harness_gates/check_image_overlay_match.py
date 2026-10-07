#!/usr/bin/env python3
"""Tier-0 gate: the bootable image and the shipped overlays must come from ONE
implementation run.

CATCHES: the failure that destroyed the FPGA configuration twice on 2026-07-24.
A partial bitstream is bound to the exact static place-and-route it was built
against. The Linux harness had ONE synthesis implemented TWICE --

    src/linux_harness/build/transplant_impl/shell_static_synth.dcp
      |- phaseB's own P&R  -> transplant_impl/shell_linux_top.bit
      '- build_dfx.tcl P&R -> build_linux/prod/config_rm_greybox.bit  (+ partials)

-- and booting phaseB's while deploying build_dfx's partials corrupts the whole
configuration, deterministically, by any write path.

WHY THE EXISTING GATES MISS IT: check_overlay_static_id.py compares each
manifest's static_id against the shell firmware's compiled-in constant. Both
implementations carry the SAME static_id (0x2B082E1B) because static_id is a
CRC of static_routed_locked.dcp, which is minted once and then shared -- it
identifies the DESIGN, not the IMPLEMENTATION. So the mismatch sails through.
pr_verify does not catch it either: it only compares an RM against the static of
its own build run.

WHAT THIS CHECKS: BITSTREAM.CONFIG.USERID -- the 8-hex build commit that
build_dfx.tcl stamps into the FULL bitstream, and which the device reports back
as REGISTER.USERCODE. It differs between implementation runs, so it is the
identity that actually discriminates. (USR_ACCESS carries HARNESS_VER32, the
harness *version*, identical across impl runs of one version -- useless here.)

Three things must agree:
  1. the canonical image's UserID   (board_scripts/board_image.tcl -> BIT)
  2. board_image.tcl's BIT_USERCODE (the declared constant, so it cannot drift)
  3. every overlay manifest's static_usercode (gen_manifest.py --static-bit)

This is the BUILD-TIME half of the guard. The RUN-TIME half reads the same
register off the live device before a swap: board_scripts/dfx_preflight.sh.

SKIPs cleanly when the bitstream/overlays are absent -- they are untracked build
artefacts, so a fresh checkout has nothing to compare (matching the Makefile's
check-overlays behaviour). Pure stdlib, no tools, no board.

    python3 check_image_overlay_match.py [--repo ROOT] [--image BIT]

LINUX RECORDS (2026-09-23, the MicroBlaze V harness, fpga/dfx `mint-linux-image`).
A Linux static adds a FOURTH party that must agree: the boot blob stage0 loads
(OpenSBI + kernel + rootfs), because the rootfs carries the static_id the
harness answers `ping.shell_id` with -- baked in by provisioning (MPS3_STATIC_ID),
not compiled from overlay/mps3_shell_static_id.c. A blob provisioned for one
static and booted on another reports the wrong id and refuses every overlay at
best. So a Linux record directory -- a mint's prod/ or a fielded/<id>/ -- carries
`linux_bundle.json` (written by fpga/dfx/tools/linux_bundle.py; its two targets,
mcc_sd and ethernet, are FLOW_CONTRACT.md §0), and

    image  (the slot image's sha256 + the static_id it was provisioned for)
      <-> static (static_id.txt, the flashable .bit's UserID, stage0_bake.json,
                  static_canon's SHELL_CPU=mbv)
      <-> overlays (the manifests' static_id + static_usercode)

must all name ONE static. `--linux-record DIR` checks one record (and only
that); with no --linux-record, every committed fielded/*/linux_bundle.json is
checked AFTER the board_image check above, and a tree with none prints nothing
extra -- so the bare-metal output is byte-identical to before.

    python3 check_image_overlay_match.py --linux-record <prod or fielded dir>
          [--linux-overlays 'GLOB']   # also check live manifests, not just the snapshot
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import importlib.util
import json
import os
import re
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_REPO = os.path.dirname(os.path.dirname(HERE))  # scripts/harness_gates/ -> repo root
BOARD_IMAGE_TCL = os.path.join("src", "linux_soc", "hw", "board_scripts", "board_image.tcl")
# Only the overlays belonging to THIS image. board_image.tcl declares the MBV-Linux
# harness image, whose partials live in overlay_linux/. fpga/dfx/overlay/ holds the
# classic MicroBlaze shell's overlays -- a different static, legitimately a different
# identity. Globbing both would fail the gate on a correct tree, and a gate that
# cries wolf gets switched off. Point --overlays at another set to check it.
DEFAULT_OVERLAY_GLOB = "fpga/dfx/overlay_linux/*/manifest.json"


def norm32(value) -> str:
    """Normalise a 32-bit identity to canonical 0xXXXXXXXX, matching the manifest
    consumers (int(x, 0), as in pyverify/overlay.py:_parse_int)."""
    n = value if isinstance(value, int) else int(str(value).strip(), 0)
    return f"0x{n & 0xFFFFFFFF:08X}"


def usercode_from_bitstream(path: str) -> str:
    """Extract UserID from a .bit's short ASCII header (these files are ~12 MB;
    read only the header). An unstamped bitstream reports 0xFFFFFFFF."""
    with open(path, "rb") as fh:
        head = fh.read(200)
    m = re.search(rb"UserID=(0[xX])?([0-9A-Fa-f]{1,8})", head)
    if not m:
        raise ValueError(f"no UserID token in the header of {path}")
    return norm32("0x" + m.group(2).decode())


def parse_board_image_tcl(path: str) -> tuple[str | None, str | None]:
    """Pull the default BIT path and the declared BIT_USERCODE out of the Tcl.
    Deliberately reads the DEFAULT branch only -- the MPS3_BOARD_BIT override is
    an explicit one-off escape hatch, not something to gate on."""
    text = open(path).read()
    # There are two `set BIT` lines -- the MPS3_BOARD_BIT override branch and the
    # default. Take the literal path: anything containing '$' is a Tcl expansion
    # (the override), which is an explicit one-off escape hatch, not the shipped
    # default, and is not a resolvable path here.
    bits = [b for b in re.findall(r"^\s*set BIT\s+(\S+)\s*(?:;.*)?$", text, re.M) if "$" not in b]
    uc = re.search(r"^\s*set BIT_USERCODE\s+(\S+)\s*(?:;.*)?$", text, re.M)
    return (bits[-1] if bits else None, uc.group(1) if uc else None)


def check_board_image(args) -> int:
    """The bare-metal/July-fork check: board_image.tcl's BIT <-> its overlays."""
    tcl_path = os.path.join(args.repo, BOARD_IMAGE_TCL)
    if not os.path.isfile(tcl_path):
        print(f"  SKIP image/overlay match (no {BOARD_IMAGE_TCL})")
        return 0

    declared_bit, declared_uc = parse_board_image_tcl(tcl_path)
    image = args.image or declared_bit
    if not image:
        print(f"FAIL {BOARD_IMAGE_TCL} defines no default BIT", file=sys.stderr)
        return 1

    manifests = sorted(glob.glob(os.path.join(args.repo, args.overlays)))
    if not os.path.isfile(image) or not manifests:
        missing = "bitstream" if not os.path.isfile(image) else "overlays"
        print(f"  SKIP image/overlay match (no {missing} — untracked build artefacts)")
        return 0

    # An image we cannot identify cannot be certified against the overlays -- fail
    # closed (a malformed/truncated .bit with no UserID token would otherwise crash
    # with a traceback here). A legitimately unstamped bitstream still carries
    # UserID=0xFFFFFFFF, so a MISSING token means the file is not a usable .bit.
    try:
        actual = usercode_from_bitstream(image)
    except (ValueError, OSError) as exc:
        print(f"FAIL image/overlay match: cannot read UserID from {image}: {exc}",
              file=sys.stderr)
        return 1
    failures = []

    # 1. the declared constant must track the actual image
    if declared_uc and norm32(declared_uc) != actual:
        failures.append(
            f"{BOARD_IMAGE_TCL}: BIT_USERCODE={norm32(declared_uc)} but "
            f"{os.path.basename(image)} carries UserID={actual}"
        )

    # 2. every overlay must be built against that same implementation
    unstamped = []
    for mpath in manifests:
        try:
            d = json.loads(open(mpath).read())
        except json.JSONDecodeError as exc:
            failures.append(f"{os.path.relpath(mpath, args.repo)}: invalid JSON: {exc}")
            continue
        raw = d.get("static_usercode")
        rel = os.path.relpath(mpath, args.repo)
        if raw is None:
            unstamped.append(rel)
            continue
        if norm32(raw) != actual:
            failures.append(
                f"{rel}: static_usercode={norm32(raw)} but the bootable image "
                f"carries UserID={actual} — these partials were built against a "
                f"DIFFERENT implementation and will destroy the configuration"
            )

    if failures:
        print("FAIL image/overlay implementation mismatch:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        print(f"\n  bootable image : {image}", file=sys.stderr)
        print("  Fix: ship the static that build_dfx.tcl emitted alongside these "
              "partials, or rebuild the partials against the static you ship.\n"
              "  See docs/ICAP_SWAP_PROVEN.md and board_scripts/board_image.tcl.",
              file=sys.stderr)
        return 1

    checked = len(manifests) - len(unstamped)
    print(f"  OK image/overlay match: {os.path.basename(image)} UserID={actual}, "
          f"{checked} overlay(s) agree")
    if unstamped:
        # Not a failure: manifests predating the guard simply carry no identity.
        # Never claim to have verified what was skipped.
        print(f"  NOTE {len(unstamped)} overlay(s) carry no static_usercode and were "
              f"NOT verified: {', '.join(unstamped)}")
        print("       Rebuild with gen_manifest.py --static-bit <full_static.bit> "
              "to bring them under this gate.")
    return 0


# =============================================================================
# LINUX RECORDS -- image <-> static <-> overlays (see the module docstring)
# =============================================================================
LINUX_MANIFEST = "linux_bundle.json"
LINUX_SCHEMA = "mps3-linux-bundle"
S0_MAGIC = 0x424C3053      # "S0LB" -- src/linux_soc/hw/fw_stage0/stage0_boot.h


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _stage0_pack():
    """STAGE0's own stage0_pack.py -- the ONE parser of the S0LB boot table
    (STAGE0_CONTRACT.md §5; `check_image` verifies an image exactly as stage0's
    s0_load() does). Loaded from THIS checkout, whatever --repo says, because the
    format is the code's, not the record's. Returns None when absent."""
    path = os.path.join(DEFAULT_REPO, "src", "linux_soc", "hw", "fw_stage0", "stage0_pack.py")
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location("stage0_pack", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def s0lb_info(path: str):
    """-> (info | None when the file is not an S0LB boot table at all, [problems]).

    Delegates to stage0_pack.check_image: header magic/version (v2 today: the
    header CRC covers the header AND the entry table), num_entries, every region
    inside the image and the DDR window, every region CRC, the 64 MiB limit. A
    second parser here drifted once already (v1 -> v2) and flagged every good
    image; there is now one."""
    with open(path, "rb") as fh:
        head = fh.read(4)
        if len(head) < 4 or struct.unpack("<I", head)[0] != S0_MAGIC:
            return None, []
    pack = _stage0_pack()
    if pack is None:
        return {}, ["src/linux_soc/hw/fw_stage0/stage0_pack.py not found -- the S0LB "
                    "format has no parser in this checkout, so the image is NOT verified"]
    with open(path, "rb") as fh:
        img = fh.read()
    problems, info = pack.check_image(img)
    out = {"version": struct.unpack_from("<I", img, 4)[0] if len(img) >= 8 else None,
           "num_entries": info.get("entries")}
    if "pc" in info:
        out.update(entry_pc=f"0x{info['pc']:08X}", entry_a0=f"0x{info['a0']:08X}",
                   entry_a1=f"0x{info['a1']:08X}",
                   header_crc32=f"0x{info['header_crc32']:08X}")
    if "regions" in info:
        out["regions"] = [{"dst": f"0x{r['dst']:08X}", "len": r["len"],
                           "crc32": f"0x{r['crc32']:08X}"} for r in info["regions"]]
    return out, [f"S0LB: {p} (stage0 would refuse it)" for p in problems]


def _load_json(path: str):
    with open(path) as fh:
        return json.load(fh)


def _static_canon(rdir: str):
    """The static's content identity: static_canon.json beside the record, else
    mint.json's static_canon slot (what a fielded record carries)."""
    p = os.path.join(rdir, "static_canon.json")
    if os.path.isfile(p):
        return _load_json(p)
    p = os.path.join(rdir, "mint.json")
    if os.path.isfile(p):
        return ((_load_json(p).get("static_canon") or {}).get("value")) or None
    return None


def check_linux_record(rdir: str, repo: str, live_overlays: str | None = None):
    """One Linux record dir (linux_bundle.json + the static's own files).
    -> (failures, notes). Every comparison is against the STATIC's own files,
    never the bundle's word for itself."""
    failures, notes = [], []
    mpath = os.path.join(rdir, LINUX_MANIFEST)
    try:
        m = _load_json(mpath)
    except (OSError, json.JSONDecodeError) as exc:
        return [f"{mpath}: unreadable ({exc})"], notes
    if m.get("schema") != LINUX_SCHEMA:
        return [f"{mpath}: schema {m.get('schema')!r}, want {LINUX_SCHEMA!r}"], notes
    tg = m.get("targets") or {}
    sd, eth = tg.get("mcc_sd") or {}, tg.get("ethernet") or {}
    if m.get("shell_cpu") != "mbv":
        failures.append(f"shell_cpu={m.get('shell_cpu')!r}: a Linux bundle belongs to an "
                        "mbv static (SHELL_CPU=mbv)")

    # -- the static: static_id.txt is the authority, the .bit's UserID the other half
    sid_path = os.path.join(rdir, "static_id.txt")
    if not os.path.isfile(sid_path):
        return failures + [f"{rdir}: no static_id.txt -- nothing to hold the bundle to"], notes
    sid = norm32(open(sid_path).read().strip())
    try:
        want_sid = norm32(m.get("static_id"))
        want_uc = norm32(m.get("static_usercode"))
    except (TypeError, ValueError, AttributeError):
        return failures + [f"{mpath}: static_id/static_usercode missing or not 32-bit hex"], notes
    if want_sid != sid:
        failures.append(
            f"the bundle was packaged for static_id {want_sid} but this static is {sid}")
    fb = (sd.get("flashable_bit") or {})
    seen_bit = False
    for name in (fb.get("name") or "config_rm_greybox_stage0.bit", "config_rm_greybox.bit"):
        p = os.path.join(rdir, name)
        if not os.path.isfile(p):
            continue
        seen_bit = True
        try:
            got = usercode_from_bitstream(p)
        except (ValueError, OSError) as exc:
            failures.append(f"{name}: cannot read UserID ({exc})")
            continue
        if got != want_uc:
            failures.append(f"{name} carries UserID={got} but the bundle was packaged "
                            f"for {want_uc} -- a different implementation of the static")
        if name == fb.get("name") and fb.get("sha256") and sha256_file(p) != fb["sha256"]:
            failures.append(f"{name}: sha256 differs from the bundle's mcc_sd target -- the "
                            "flashable base was re-baked or replaced since")
    if not seen_bit:
        notes.append("no .bit here (gitignored binary) -- UserID checked against the "
                     "records only")

    for side, key_uc in (("stage0_bake.json", "static_usercode"), ("static_stamp.json", "usercode")):
        p = os.path.join(rdir, side)
        if not os.path.isfile(p):
            continue
        d = _load_json(p)
        if d.get("static_id") and norm32(d["static_id"]) != sid:
            failures.append(f"{side} static_id {norm32(d['static_id'])} != static_id.txt {sid}")
        if d.get(key_uc) and norm32(d[key_uc]) != want_uc:
            failures.append(f"{side} {key_uc} {norm32(d[key_uc])} != the bundle's {want_uc}")
        if side == "stage0_bake.json":
            if (d.get("stage0_elf") or {}).get("sha256") != (sd.get("stage0") or {}).get("sha256"):
                failures.append("the stage0 baked into this static (stage0_bake.json) is not "
                                "the stage0 the bundle names")
            if not d.get("stage0_identity_checked") and m.get("fieldable") is not False:
                failures.append("stage0_bake.json: the stage0's compiled-in static_id/ver32 "
                                "were never checked, yet the bundle claims to be fieldable")
    if not os.path.isfile(os.path.join(rdir, "stage0_bake.json")):
        failures.append("no stage0_bake.json -- nothing shows which stage0 is in the base")

    # the STATIC's probes file (SEAM-8 MIG hub + DDR4 slave), bound to the base
    sl = sd.get("static_ltx") or {}
    if not sl:
        failures.append("the mcc_sd target carries no static .ltx: the MicroBlaze V "
                        "static's debug cores have no probes file (FLOW_CONTRACT §6.3)")
    else:
        lp = os.path.join(rdir, sl.get("name") or "config_rm_greybox_static.ltx")
        if os.path.isfile(lp) and sha256_file(lp) != sl.get("sha256"):
            failures.append(f"{os.path.basename(lp)}: sha256 differs from the bundle's mcc_sd "
                            "target -- not the probes file of this static")
        spath = os.path.join(rdir, sl.get("sidecar") or "")
        if os.path.isfile(spath):
            side = _load_json(spath)
            if side.get("static_usercode") and norm32(side["static_usercode"]) != want_uc:
                failures.append(f"{sl.get('sidecar')}: static .ltx is for UserID "
                                f"{side['static_usercode']}, the bundle's static is {want_uc}")
            if side.get("static_id") and norm32(side["static_id"]) != sid:
                failures.append(f"{sl.get('sidecar')}: static .ltx is for static_id "
                                f"{side['static_id']}, not {sid}")
        elif not os.path.isfile(lp):
            notes.append("static .ltx + sidecar not present (gitignored) -- not re-checked")

    canon = _static_canon(rdir)
    if canon is None:
        notes.append("no static_canon.json / mint.json here -- SHELL_CPU not re-derived")
    else:
        if (canon.get("flags") or {}).get("SHELL_CPU") != "mbv":
            failures.append("the static's static_canon records no SHELL_CPU=mbv -- this is "
                            "not a MicroBlaze V static, whatever the bundle says")
        if m.get("static_canon") and canon.get("digest") != m["static_canon"]:
            failures.append("static_canon digest differs from the bundle's record")

    # -- the ethernet target: slot image + provisioning + legal-info
    prov = (eth.get("provisioned") or {}).get("static_id")
    try:
        prov_ok = prov is not None and norm32(prov) == want_sid
    except (TypeError, ValueError):
        prov_ok = False
    if prov_ok and want_sid == "0x00000000":
        prov_ok = False
    if not prov_ok:
        failures.append(f"the slot image was provisioned for static_id {prov!r}, not "
                        f"{want_sid} -- its rootfs belongs to another static")
    slot = eth.get("slot_image") or {}
    spath = os.path.join(rdir, slot.get("name") or "linux_slot.img")
    if os.path.isfile(spath):
        if sha256_file(spath) != slot.get("sha256"):
            failures.append(f"{os.path.basename(spath)}: sha256 differs from the bundle -- "
                            "this is not the slot image that was provisioned for this static")
        else:
            info, probs = s0lb_info(spath)
            if info is None:
                failures.append(f"{os.path.basename(spath)}: not an S0LB boot table")
            failures.extend(f"{os.path.basename(spath)}: {p}" for p in probs)
    else:
        notes.append(f"slot image {os.path.basename(spath)} not present (gitignored binary) "
                     "-- its sha256 was NOT re-checked")
    # the greybox clearing the image installs at /etc/mps3/greybox_clear.bin
    gc = eth.get("greybox_clear") or {}
    if not gc.get("sha256"):
        failures.append("the bundle records no greybox clearing for the image -- harnessd "
                        "would fail closed on the first swap away from greybox")
    else:
        gp = os.path.join(rdir, gc.get("name") or "config_rm_greybox_pblock_rp_dut_partial_clear.bin")
        if os.path.isfile(gp) and sha256_file(gp) != gc["sha256"]:
            failures.append(f"the image's greybox_clear.bin (sha256 {gc['sha256'][:12]}..) is not "
                            f"this static's {os.path.basename(gp)}")
        if gc.get("static_id") and norm32(gc["static_id"]) != sid:
            failures.append(f"the image's greybox clearing is for static_id {gc['static_id']}, not {sid}")
    legal = eth.get("legal_info")
    if legal:
        lp = os.path.join(rdir, legal.get("name") or "linux_legal_info.tar")
        if os.path.isfile(lp) and sha256_file(lp) != legal.get("sha256"):
            failures.append(f"{os.path.basename(lp)}: sha256 differs from the bundle")
    elif m.get("fieldable") is not False:
        failures.append("no Buildroot legal-info in a fieldable bundle (GPL: the image "
                        "must travel with its licences and sources)")

    # -- the overlays: the snapshot taken at packaging, and the live set if asked
    snap = eth.get("overlays") or []
    if not snap:
        failures.append("the bundle records no overlays -- nothing ties it to a swappable set")
    live = []
    if live_overlays:
        for mp in sorted(glob.glob(os.path.join(repo, live_overlays))):
            d = _load_json(mp)
            live.append({"rm_name": d.get("rm_name"), "static_id": d.get("static_id"),
                         "static_usercode": d.get("static_usercode"), "_src": mp})
        if not live:
            failures.append(f"--linux-overlays {live_overlays!r} matched no manifest")
    for o in snap + live:
        who = o.get("_src") or f"snapshot:{o.get('rm_name')}"
        if o.get("static_id") is None or norm32(o["static_id"]) != sid:
            failures.append(f"{who}: static_id {o.get('static_id')} != {sid}")
        if o.get("static_usercode") is None:
            failures.append(f"{who}: no static_usercode -- a Linux overlay must be bound "
                            "to the implementation (make -C fpga/dfx overlay-usercode)")
        elif norm32(o["static_usercode"]) != want_uc:
            failures.append(f"{who}: static_usercode {norm32(o['static_usercode'])} != {want_uc}")

    # -- a prototype is never fielded
    fielded = os.path.realpath(os.path.join(repo, "fielded")) + os.sep
    if os.path.realpath(rdir).startswith(fielded) and m.get("fieldable") is not True:
        failures.append(f"mint_kind={m.get('mint_kind')!r} is not fieldable, but it is "
                        "under fielded/ -- a prototype (P-mint) static is never fielded")
    return failures, notes


def check_linux_records(dirs, repo: str, live_overlays: str | None = None) -> int:
    rc = 0
    for rdir in dirs:
        failures, notes = check_linux_record(rdir, repo, live_overlays)
        rel = os.path.relpath(rdir, repo)
        if failures:
            rc = 1
            print(f"FAIL linux record {rel}: image/static/overlays do not name ONE static:",
                  file=sys.stderr)
            for f in failures:
                print(f"  - {f}", file=sys.stderr)
        else:
            m = _load_json(os.path.join(rdir, LINUX_MANIFEST))
            eth = (m.get("targets") or {}).get("ethernet") or {}
            print(f"  OK linux record {rel}: static_id {norm32(m['static_id'])}, "
                  f"UserID {norm32(m['static_usercode'])}, slot image "
                  f"{(eth.get('slot_image') or {}).get('sha256', '?')[:12]}, "
                  f"{len(eth.get('overlays') or [])} overlay(s) agree")
        for n in notes:
            print(f"  NOTE {rel}: {n}")
    return rc


def fielded_linux_records(repo: str):
    return sorted(os.path.dirname(p) for p in
                  glob.glob(os.path.join(repo, "fielded", "*", LINUX_MANIFEST)))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--image", help="override the bitstream to check")
    ap.add_argument("--overlays", default=DEFAULT_OVERLAY_GLOB,
                    help=f"glob of manifests belonging to this image (default: {DEFAULT_OVERLAY_GLOB})")
    ap.add_argument("--linux-record", action="append", default=[],
                    help="a Linux record dir (a mint's prod/ or fielded/<id>/) holding "
                         f"{LINUX_MANIFEST}; checks ONLY the given record(s)")
    ap.add_argument("--linux-overlays", default=None,
                    help="with --linux-record: also check these live manifests (glob, "
                         "relative to --repo) against the record's static")
    args = ap.parse_args(argv)

    if args.linux_record:
        return check_linux_records(args.linux_record, args.repo, args.linux_overlays)
    rc = check_board_image(args)
    linux = fielded_linux_records(args.repo)
    return (check_linux_records(linux, args.repo) if linux else 0) or rc


if __name__ == "__main__":
    sys.exit(main())
