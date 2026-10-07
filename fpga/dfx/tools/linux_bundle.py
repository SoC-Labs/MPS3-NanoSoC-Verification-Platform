#!/usr/bin/env python3
"""linux_bundle.py -- package a MicroBlaze V (Linux) mint's release bundle.

    pack    prod/ + the IMAGE lane's slot image (+ its sidecar) + legal-info +
            the overlay set -> prod/linux_bundle.json (+ copies next to it)
    check   run the one gate over a bundle dir
            (scripts/harness_gates/check_image_overlay_match.py --linux-record)

Driven by `make -C fpga/dfx mint-linux-image SHELL_CPU=mbv ...`; see
docs/planning/linux_lanes/FLOW_CONTRACT.md §0 for the bundle's two targets.

TWO TARGETS, ONE STATIC
-----------------------
A Linux harness is updated through two doors, and the bundle says which bytes
go through which:

  mcc_sd    the MCC config SD: config_rm_greybox_stage0.bit -- the static with
            stage0 baked into the MBV LMB (mint-stage0). Changes only with a
            mint or a stage0 re-bake; written by an SD install + MCC REBOOT.
  ethernet  everything a running harness can take over the network without
            touching the MCC SD: the user-uSD slot image stage0 boots (S0LB
            boot table: OpenSBI + kernel + rootfs), the overlay set, this
            manifest, and the Buildroot legal-info that must travel with any
            distributed image (GPL).

Both halves must name ONE static: static_id (CRC of the locked static), the
full .bit's UserID (the implementation), and HARNESS_VER32. A slot image
provisioned for another static (the July 0x2B082E1B blob is the standing
example) is refused here, and again by the gate wherever the bundle travels.

WHAT IS CHECKED HERE (every one refuses; nothing is written on a refusal)
  * the IMAGE sidecar's provisioned static_id == prod/static_id.txt;
  * stage0_bake.json: present, its baked .bit is the flashable base on disk
    (sha256), and the stage0 inside was compiled FOR this static
    (stage0_identity_checked) -- a bring-up bake without that check may be
    packaged only as a prototype;
  * static_canon records SHELL_CPU=mbv (an mb static cannot carry this image);
  * the slot image is a well-formed S0LB boot table with every region CRC good
    (what stage0 would check at boot, checked before it reaches a card). IMAGE
    ships the raw 1-region FW_PAYLOAD (IMAGE_CONTRACT.md §1.1); it is wrapped with
    STAGE0's own stage0_pack.py (pc 0x80000000, a1 0), the one writer of S0LB;
  * the image is a `release` build (a `lab` image may carry a baked host key),
    its record's stage0_sha256 is the stage0 baked into this static, and the
    blob + record match their build's SHA256SUMS;
  * every overlay manifest in --overlay-root carries this static_id and is
    bound to this implementation (static_usercode == the .bit's UserID);
  * legal-info is present (a tarball is written, deterministic) -- required
    for a fieldable bundle, optional for a prototype (recorded null + reason).

Stdlib only, Python 3.6+. No Vivado, no board.
"""

import argparse
import datetime
import glob
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
GATE = os.path.join(REPO, "scripts", "harness_gates", "check_image_overlay_match.py")
#: STAGE0's packer: the ONE writer of the S0LB format (STAGE0_CONTRACT.md §5)
STAGE0_PACK = os.path.join(REPO, "src", "linux_soc", "hw", "fw_stage0", "stage0_pack.py")
#: IMAGE_CONTRACT.md §1.2: the 1-region hand-off
ONE_REGION = ("0x80000000", "0")      # pc, a1 (FDT embedded in FW_PAYLOAD)
#: IMAGE_CONTRACT.md §6 keys worth carrying into the bundle
VERSION_KEYS = ("format", "impl", "harness", "ver32", "sha", "dirty", "date", "build_date",
                "image_kind", "variant", "idle", "kernel", "kernel_config_sha256",
                "kernel_patches_sha256", "rootfs_tree_sha256", "harnessd_sha256",
                "stage0_sha256", "static_id", "buildroot", "opensbi",
                "greybox_clear_sha256", "greybox_clear_static_id")

BUNDLE = "linux_bundle.json"
SLOT_IMAGE = "linux_slot.img"
LEGAL_TAR = "linux_legal_info.tar"
FLASHABLE = "config_rm_greybox_stage0.bit"
#: the static's greybox clearing (stage 4), what the image carries at
#: /etc/mps3/greybox_clear.bin and bare metal bakes via gen_greybox_blob.py
GREYBOX_CLEAR = "config_rm_greybox_pblock_rp_dut_partial_clear.bin"


def load_gate():
    """The gate is the ONE implementation of 'do these name one static'; the
    packer uses its readers and runs it on what it wrote."""
    spec = importlib.util.spec_from_file_location("check_image_overlay_match", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def crc32_of(path):
    crc = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            crc = zlib.crc32(chunk, crc)
    return "0x%08X" % (crc & 0xFFFFFFFF)


def file_rec(path, name=None):
    return {"name": name or os.path.basename(path), "sha256": sha256_of(path),
            "bytes": os.path.getsize(path)}


def read_sidecar(path):
    """IMAGE's provisioning record: artifacts/version (key=value, the image's own
    /etc/mps3/version -- IMAGE_CONTRACT.md §6) or a JSON object."""
    with open(path) as fh:
        text = fh.read()
    if text.lstrip().startswith("{"):
        return json.loads(text)
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def sha256sums(path):
    """IMAGE's artifacts/SHA256SUMS -> {name: sha256}, or {} when absent."""
    out = {}
    if path and os.path.isfile(path):
        with open(path) as fh:
            for line in fh:
                parts = line.split()
                if len(parts) == 2:
                    out[parts[1].lstrip("*")] = parts[0]
    return out


def read_canon(path):
    if not path or not os.path.isfile(path):
        return None
    with open(path) as fh:
        return json.load(fh)


def legal_tar(src, out):
    """A tarball of Buildroot's legal-info dir that is byte-identical for the
    same input (sorted names, fixed mtime/owner) -- its sha256 then says
    'same licences and sources', not 'packaged at a different second'."""
    if os.path.isfile(src) and src.endswith(".tar"):
        shutil.copyfile(src, out)
        return
    subprocess.check_call(
        ["tar", "--sort=name", "--mtime=@0", "--owner=0", "--group=0",
         "--numeric-owner", "--format=gnu", "-cf", out,
         "-C", os.path.dirname(os.path.abspath(src)), os.path.basename(os.path.abspath(src))])


def cmd_pack(args):
    gate = load_gate()
    prod = os.path.abspath(args.prod)
    problems, notes = [], []
    prototype = args.mint_kind == "prototype"

    sid = gate.norm32(open(os.path.join(prod, "static_id.txt")).read().strip())
    base = os.path.join(prod, "config_rm_greybox.bit")
    flash = os.path.join(prod, FLASHABLE)
    for p in (base, flash):
        if not os.path.isfile(p):
            problems.append("%s missing -- run mint-stage0 first" % os.path.basename(p))
    uc = gate.usercode_from_bitstream(base) if os.path.isfile(base) else None

    # -- stage0 bake record
    bake_p = os.path.join(prod, "stage0_bake.json")
    bake = None
    if not os.path.isfile(bake_p):
        problems.append("no stage0_bake.json -- mint-stage0 has not run (or was refused)")
    else:
        with open(bake_p) as fh:
            bake = json.load(fh)
        if gate.norm32(bake.get("static_id")) != sid:
            problems.append("stage0_bake.json static_id %s != %s" % (bake.get("static_id"), sid))
        if os.path.isfile(flash) and (bake.get("baked_bit") or {}).get("sha256") != sha256_of(flash):
            problems.append("%s is not the bitstream stage0_bake.json recorded -- re-baked "
                            "or replaced since" % FLASHABLE)
        sl = bake.get("static_ltx") or {}
        slp = os.path.join(prod, sl.get("name") or "config_rm_greybox_static.ltx")
        if not sl:
            problems.append("stage0_bake.json binds no static .ltx: the MicroBlaze V static's "
                            "debug cores (SEAM-8 MIG hub + DDR4 slave) have no probes file "
                            "(FLOW_CONTRACT §6.3) -- re-run mint-stage0")
        elif not os.path.isfile(slp) or sha256_of(slp) != sl.get("sha256"):
            problems.append("%s is missing or differs from the one bound to the flashable "
                            "base in stage0_bake.json" % os.path.basename(slp))
        if not bake.get("stage0_identity_checked") and not prototype:
            problems.append("stage0_bake.json says the stage0's compiled-in static_id/ver32 "
                            "were NOT checked (--no-identity bring-up bake): not fieldable")
    ver32 = ((bake or {}).get("usr_access") or [None])[0]

    # -- static_canon: this is an mbv static, and it is the canon the MINT recorded
    canon = read_canon(args.static_canon)
    if canon is not None and args.mint_record:
        try:
            with open(args.mint_record) as fh:
                recorded = ((json.load(fh).get("static_canon") or {}).get("value") or {})
        except (OSError, ValueError) as exc:
            problems.append("%s unreadable: %s" % (args.mint_record, exc))
            recorded = {}
        if recorded.get("digest") and recorded["digest"] != canon.get("digest"):
            problems.append("%s (digest %s) is not the static_canon this mint recorded in %s "
                            "(%s) -- a canon recomputed from the current tree or other flags "
                            "describes some other build" % (args.static_canon,
                                                            str(canon.get("digest"))[:12],
                                                            os.path.basename(args.mint_record),
                                                            recorded["digest"][:12]))
        elif not recorded.get("digest"):
            notes.append("%s records no static_canon digest -- the canon was not cross-checked"
                         % os.path.basename(args.mint_record))
    if canon is None:
        problems.append("no static_canon.json (%s) -- cannot show this is an mbv static"
                        % args.static_canon)
    elif (canon.get("flags") or {}).get("SHELL_CPU") != "mbv":
        problems.append("static_canon records flags %s, no SHELL_CPU=mbv: this static is not "
                        "a MicroBlaze V build and cannot run a Linux slot image"
                        % canon.get("flags"))

    # -- the slot image + the IMAGE lane's provisioning record
    info = {}
    if not os.path.isfile(args.blob):
        problems.append("slot image not found: %s" % args.blob)
    if not args.blob_info or not os.path.isfile(args.blob_info):
        problems.append("no provisioning sidecar for the slot image (%s): it cannot say "
                        "which static it was provisioned for (IMAGE_CONTRACT.md)"
                        % args.blob_info)
    else:
        info = read_sidecar(args.blob_info)
        try:
            prov = gate.norm32(info.get("static_id"))
        except (TypeError, ValueError):
            prov = None
        if prov == "0x00000000":
            problems.append(
                "the slot image is UNPROVISIONED (static_id 0x00000000): it was built "
                "without MPS3_STATIC_ID. An image must be provisioned for its static; 0 "
                "is never accepted, prototype or not -- rebuild with the values "
                "`make -s -C fpga/dfx linux-image-env` prints")
        elif prov != sid:
            problems.append(
                "the slot image was provisioned for static_id %s, this static is %s -- "
                "rebuild it with MPS3_STATIC_ID=%s (make -s -C fpga/dfx linux-image-env)"
                % (info.get("static_id"), sid, sid))
        # the greybox CLEARING the image carries at /etc/mps3/greybox_clear.bin
        # (harnessd fails closed on the first swap away from greybox without it):
        # IMAGE records its sha256 + static_id (MPS3_GREYBOX_CLEAR seam); it must be
        # THIS static's prod clearing, byte for byte.
        gc_file = os.path.join(prod, GREYBOX_CLEAR)
        gc_sha = info.get("greybox_clear_sha256")
        gc_sid = info.get("greybox_clear_static_id")
        if not os.path.isfile(gc_file):
            problems.append("%s missing -- the static's greybox clearing is a stage-4 output"
                            % GREYBOX_CLEAR)
        elif not gc_sha or len(str(gc_sha)) != 64:
            problems.append("the image records no greybox clearing (greybox_clear_sha256=%s): "
                            "harnessd would fail closed on the first swap away from greybox. "
                            "Rebuild with MPS3_GREYBOX_CLEAR from `make linux-image-env`"
                            % (gc_sha or "absent"))
        elif gc_sha != sha256_of(gc_file):
            problems.append("the image's /etc/mps3/greybox_clear.bin (sha256 %s..) is not this "
                            "static's greybox clearing %s (%s..) -- a clearing from another "
                            "static would be pushed through ICAP on a swap"
                            % (gc_sha[:12], GREYBOX_CLEAR, sha256_of(gc_file)[:12]))
        if gc_sid is not None:
            try:
                if gate.norm32(gc_sid) != sid:
                    problems.append("the image's greybox clearing is recorded for static_id %s, "
                                    "not %s" % (gc_sid, sid))
            except (TypeError, ValueError):
                problems.append("greybox_clear_static_id=%r is not a 32-bit id" % gc_sid)
        kind = info.get("image_kind")
        if kind and kind != "release" and not prototype:
            problems.append("image_kind=%s: only a `release` image may be fielded (a `lab` "
                            "image may carry a baked SSH host key -- IMAGE_CONTRACT.md §4.1)"
                            % kind)
        s0sha = info.get("stage0_sha256")
        if bake and s0sha and len(s0sha) == 64 and s0sha != bake["stage0_elf"]["sha256"]:
            problems.append("the image records stage0_sha256 %s.. but the stage0 baked into "
                            "this static is %s.. -- rebuild it with the values "
                            "`make linux-image-env` prints" % (s0sha[:12],
                                                              bake["stage0_elf"]["sha256"][:12]))
        elif not s0sha or len(s0sha) != 64:
            notes.append("the image records stage0_sha256=%s (IMAGE_CONTRACT.md §6), so its "
                         "stage0 was not cross-checked" % (s0sha or "absent"))
    sums = sha256sums(args.sha256sums)
    if os.path.isfile(args.blob) and os.path.basename(args.blob) in sums \
            and sums[os.path.basename(args.blob)] != sha256_of(args.blob):
        problems.append("%s differs from its own build's SHA256SUMS -- the blob and the "
                        "version record beside it are not from one build"
                        % os.path.basename(args.blob))
    if args.blob_info and os.path.basename(args.blob_info) in sums and \
            os.path.isfile(args.blob_info) and \
            sums[os.path.basename(args.blob_info)] != sha256_of(args.blob_info):
        problems.append("%s differs from its build's SHA256SUMS" % os.path.basename(args.blob_info))

    # The slot image is an S0LB boot table (STAGE0_CONTRACT.md §5). IMAGE ships the
    # raw 1-region FW_PAYLOAD; it is wrapped here with STAGE0's own packer, so the
    # format has ONE writer. An S0LB image passed in is used as-is.
    s0 = None
    slot_src = args.blob
    wrapped = False
    if os.path.isfile(args.blob) and gate.s0lb_info(args.blob)[0] is None:
        slot_src = os.path.join(prod, SLOT_IMAGE + ".new")
        cmd = [sys.executable, STAGE0_PACK, "--out", slot_src, "--pc", ONE_REGION[0],
               "--a1", ONE_REGION[1], "%s@%s" % (args.blob, ONE_REGION[0])]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.isfile(slot_src):
            problems.append("stage0_pack.py could not wrap %s: %s"
                            % (os.path.basename(args.blob), (r.stderr or r.stdout).strip()))
            slot_src = args.blob
        else:
            wrapped = True
    if os.path.isfile(slot_src):
        s0, s0p = gate.s0lb_info(slot_src)
        if s0 is None:
            problems.append("%s is not an S0LB boot table and could not be made one"
                            % os.path.basename(args.blob))
        problems.extend("%s: %s" % (os.path.basename(args.blob), p) for p in s0p)

    # -- overlays, snapshotted
    overlays = []
    manifests = sorted(glob.glob(os.path.join(args.overlay_root, "*", "manifest.json")))
    if not manifests:
        problems.append("no overlay manifests under %s" % args.overlay_root)
    for mp in manifests:
        with open(mp) as fh:
            d = json.load(fh)
        rel = os.path.relpath(mp, args.overlay_root)
        o = {"rm_name": d.get("rm_name"), "rm_id": d.get("rm_id"),
             "static_id": d.get("static_id"), "static_usercode": d.get("static_usercode"),
             "partial_crc32": (d.get("partial") or {}).get("crc32"),
             "clearing_crc32": (d.get("clearing") or {}).get("crc32")}
        overlays.append(o)
        if not d.get("static_id") or gate.norm32(d["static_id"]) != sid:
            problems.append("%s: static_id %s != %s" % (rel, d.get("static_id"), sid))
        if not d.get("static_usercode"):
            problems.append("%s: no static_usercode (run make overlay-usercode)" % rel)
        elif uc and gate.norm32(d["static_usercode"]) != uc:
            problems.append("%s: static_usercode %s != UserID %s" % (rel, d["static_usercode"], uc))

    # -- legal-info
    legal_reason = None
    if args.legal_info and os.path.exists(args.legal_info):
        pass
    elif prototype:
        legal_reason = ("no Buildroot legal-info: acceptable for a prototype, which is never "
                        "distributed; recorded as null with this reason")
        notes.append(legal_reason)
    else:
        problems.append("no Buildroot legal-info (%s): a Linux image that leaves this lab "
                        "must carry its licences and sources (GPL). IMAGE_CONTRACT.md names "
                        "the path; pass LINUX_LEGAL_INFO=<dir>" % (args.legal_info or "unset"))

    if problems:
        for p in problems:
            print("LINUX BUNDLE REFUSED: %s" % p, file=sys.stderr)
        if wrapped:
            os.remove(slot_src)
        return 1

    # -- write: copies first, then the manifest that names them
    slot = os.path.join(prod, SLOT_IMAGE)
    if wrapped:
        os.replace(slot_src, slot)
    elif os.path.abspath(args.blob) != slot:
        shutil.copyfile(args.blob, slot)
    legal = None
    if args.legal_info and os.path.exists(args.legal_info):
        legal_tar(args.legal_info, os.path.join(prod, LEGAL_TAR))
        legal = file_rec(os.path.join(prod, LEGAL_TAR))

    bundle = {
        "schema": "mps3-linux-bundle",
        "schema_version": "1",
        "generated_by": "fpga/dfx/tools/linux_bundle.py",
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mint_kind": args.mint_kind,
        "fieldable": not prototype,
        "shell_cpu": "mbv",
        "static_id": sid,
        "static_usercode": uc,
        "static_ver32": ver32,
        "static_canon": canon.get("digest"),
        "targets": {
            "mcc_sd": {
                "what": "the FPGA configuration the MCC loads from its config SD at "
                        "power-on: the static with stage0 baked into the MBV LMB. "
                        "Changes only with a mint or a stage0 re-bake.",
                "flashable_bit": file_rec(flash),
                "stage0": {"name": bake["stage0_elf"]["name"],
                           "sha256": bake["stage0_elf"]["sha256"],
                           "baked_constants": (bake.get("stage0_elf_check") or {})
                           .get("baked_constants"),
                           "identity_checked": bool(bake.get("stage0_identity_checked"))},
                "static_ltx": bake.get("static_ltx"),
            },
            "ethernet": {
                "what": "everything a running harness takes over the network without "
                        "touching the MCC SD: the user-uSD slot image, the overlays, "
                        "this manifest, and the legal-info that travels with the image.",
                "slot_image": dict(file_rec(slot), crc32=crc32_of(slot), format="s0lb",
                                   source=dict(file_rec(args.blob),
                                               wrapped_by="stage0_pack.py 1-region pc=%s a1=%s"
                                               % ONE_REGION if wrapped else None),
                                   s0lb=s0),
                "provisioned": {"static_id": gate.norm32(info["static_id"]),
                                "sidecar": os.path.basename(args.blob_info)},
                "components": info.get("components") or
                              {k: info[k] for k in VERSION_KEYS if k in info},
                "overlays": overlays,
                "legal_info": legal,
                "legal_info_reason": None if legal else legal_reason,
                "greybox_clear": {"name": GREYBOX_CLEAR,
                                  "sha256": info.get("greybox_clear_sha256"),
                                  "static_id": gate.norm32(info.get("greybox_clear_static_id") or sid),
                                  "installed_at": "/etc/mps3/greybox_clear.bin"},
            },
        },
    }
    out = os.path.join(prod, BUNDLE)
    with open(out, "w") as fh:
        json.dump(bundle, fh, indent=2, sort_keys=True)
        fh.write("\n")
    for n in notes:
        print("NOTE: %s" % n)
    print("wrote %s: static_id %s UserID %s ver32 %s slot %s, %d overlay(s)%s"
          % (out, sid, uc, ver32, bundle["targets"]["ethernet"]["slot_image"]["sha256"][:12],
             len(overlays), ", legal-info %s" % legal["sha256"][:12] if legal else ""))
    # the gate, on what was just written -- one implementation of the check
    return gate.check_linux_records([prod], REPO)


def cmd_check(args):
    gate = load_gate()
    return gate.check_linux_records([os.path.abspath(d) for d in args.dir], REPO,
                                    args.overlays)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd")
    pk = sub.add_parser("pack", help="write prod/linux_bundle.json (+ copies)")
    pk.add_argument("--prod", required=True)
    pk.add_argument("--static-canon", required=True)
    pk.add_argument("--mint-record", default=None,
                    help="prod/mint.json: the canon must be the digest the mint recorded")
    pk.add_argument("--blob", required=True,
                    help="IMAGE's fw_payload_1region.bin (wrapped here) or an S0LB image")
    pk.add_argument("--blob-info", required=True,
                    help="IMAGE's provisioning record: artifacts/version (key=value) or JSON")
    pk.add_argument("--sha256sums", default="",
                    help="IMAGE's artifacts/SHA256SUMS: the blob and its record must match it")
    pk.add_argument("--legal-info", default="",
                    help="Buildroot's legal-info dir (or a .tar of it)")
    pk.add_argument("--overlay-root", required=True)
    pk.add_argument("--mint-kind", default="mint", choices=("mint", "prototype"))
    pk.set_defaults(func=cmd_pack)
    ck = sub.add_parser("check", help="the gate over one or more bundle dirs")
    ck.add_argument("dir", nargs="+")
    ck.add_argument("--overlays", default=None, help="also check these live manifests (glob)")
    ck.set_defaults(func=cmd_check)
    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
