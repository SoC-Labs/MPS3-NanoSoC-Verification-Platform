#!/usr/bin/env python3
"""mps3_image.py — the image-level steps of the post-build hook, in one place
the host tests can drive (br2_external/tests/run.sh). Called by
mps3_provision.sh with the Buildroot TARGET_DIR. IMAGE_CONTRACT §4.1/§6/§7.

    mps3_image.py hostkey-gate  TARGET KIND     no host key in a release image
    mps3_image.py inittab       TARGET VARIANT  merge /usr/share/mps3/inittab.d
    mps3_image.py manifest      TARGET          write /etc/mps3/version
    mps3_image.py kconfig-gate  CONFIG|auto     no flash stack, no AXI Quad SPI master,
                                                no riscv-pmu-sbi, uncompressed initramfs
    mps3_image.py greybox-gate  TARGET          a provisioned image carries its static's
                                                greybox clearing (after `manifest`)

Every subcommand exits non-zero with a one-line reason on failure, which makes
Buildroot's post-build step (and so the build) fail.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import time
from pathlib import Path

DROPIN_DIR = "usr/share/mps3/inittab.d"
PLACEHOLDER = "usr/share/mps3/placeholder/05-placeholder.inittab"
BEGIN = "# BEGIN mps3 inittab.d — merged by mps3_image.py; edit the drop-ins"
END = "# END mps3 inittab.d"
HARNESSD = "usr/sbin/mps3-harnessd"
VERSION = "etc/mps3/version"
GREYBOX = "etc/mps3/greybox_clear.bin"
GREYBOX_MAX = 1024 * 1024          # harnessd ovlstore_linux.c GREYBOX_MAX_BYTES

#: A file whose name says it is an SSH host PRIVATE key (dropbear or OpenSSH).
HOSTKEY_RE = re.compile(r"(^|/)(dropbear_\w+_host_key|ssh_host_\w+_key)$")


def die(msg: str) -> None:
    print(f"mps3_image: {msg}", file=sys.stderr)
    sys.exit(1)


# --------------------------------------------------------------------------- #
def hostkey_gate(target: Path, kind: str) -> None:
    """DL5 as amended 2026-09-23: host keys are generated per board on first
    boot. A RELEASE image carrying one would give every board the same SSH
    identity, so it is a build failure; only a LAB image (L0's B0 seam) may."""
    if kind not in ("release", "lab"):
        die(f"MPS3_IMAGE_KIND must be release or lab (got {kind!r})")
    found = []
    for root, dirs, files in os.walk(target, followlinks=False):
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), target)
            if HOSTKEY_RE.search(rel):
                found.append(rel)
    if found and kind == "release":
        die("RELEASE image contains SSH host key(s): " + ", ".join(sorted(found)) +
            " — host keys are generated per board on first boot; a baked key is "
            "allowed only with MPS3_IMAGE_KIND=lab (IMAGE_CONTRACT §4.1)")
    print(f"mps3_image: host keys in target: {len(found)} ({kind} image)"
          + (" — LAB image, baked key allowed" if found else " — first-boot generation"))


# --------------------------------------------------------------------------- #
def _dropins(target: Path) -> list[Path]:
    d = target / DROPIN_DIR
    return sorted(d.glob("*.inittab")) if d.is_dir() else []


def _lines(p: Path) -> list[str]:
    return [ln.rstrip("\n") for ln in p.read_text().splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def inittab(target: Path, variant: str) -> None:
    """Merge every drop-in's lines into /etc/inittab between the markers.

    Idempotent (a reused target dir re-merges from scratch). A line that a
    package hook already appended elsewhere in inittab is removed from there,
    so the managed block is the one place a respawn line lives. In the
    default variant with no harnessd drop-in, the placeholder is merged so
    supervision still has a process to prove (the manifest says so)."""
    it = target / "etc/inittab"
    if not it.is_file():
        die("etc/inittab missing (BusyBox init not in the image?)")
    drop = _dropins(target)
    if variant == "default":
        ph = target / PLACEHOLDER
        has_harnessd = any("mps3-harnessd" in " ".join(_lines(p)) for p in drop)
        if not has_harnessd and ph.is_file():
            drop.append(ph)
    managed: list[str] = []
    for p in drop:
        for ln in _lines(p):
            fields = ln.split(":", 3)
            if len(fields) != 4:
                die(f"{p.relative_to(target)}: not an inittab line: {ln!r}")
            action, proc = fields[2], fields[3]
            if action not in ("respawn", "once", "askfirst", "wait", "sysinit", "shutdown"):
                die(f"{p.relative_to(target)}: unknown action {action!r}")
            exe = proc.split()[0] if proc.split() else ""
            if exe.startswith("/") and not (target / exe.lstrip("/")).exists():
                die(f"{p.relative_to(target)}: {exe} is not in the image")
            if ln not in managed:
                managed.append(ln)
    text = it.read_text().splitlines()
    out, skip = [], False
    for ln in text:
        if ln == BEGIN:
            skip = True
            continue
        if ln == END:
            skip = False
            continue
        if skip or ln in managed:
            continue
        out.append(ln)
    # after the getty line (or at the end): respawn entries start after rcS
    at = len(out)
    for i, ln in enumerate(out):
        if "getty" in ln and not ln.lstrip().startswith("#"):
            at = i + 1
    block = [BEGIN] + [f"# from {p.relative_to(target)}" for p in drop] + managed + [END]
    out[at:at] = block
    it.write_text("\n".join(out) + "\n")
    print(f"mps3_image: inittab: {len(managed)} managed line(s) from {len(drop)} drop-in(s)")


# --------------------------------------------------------------------------- #
def _sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_sha(target: Path, exclude: set[str]) -> str:
    """sha256 over every path (sorted) + its content / link target / type.
    Modes and owners are not in it: Buildroot applies them after this hook."""
    h = hashlib.sha256()
    entries = []
    for root, dirs, files in os.walk(target, followlinks=False):
        for name in dirs + files:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, target)
            if rel in exclude:
                continue
            entries.append((rel, full))
    for rel, full in sorted(entries):
        h.update(rel.encode() + b"\0")
        if os.path.islink(full):
            h.update(b"L" + os.readlink(full).encode() + b"\0")
        elif os.path.isdir(full):
            h.update(b"D\0")
        elif os.path.isfile(full):
            h.update(b"F" + bytes.fromhex(_sha_file(Path(full))) + b"\0")
        else:
            h.update(b"S\0")
    return h.hexdigest()


def _kernel_config() -> Path | None:
    bd = os.environ.get("BUILD_DIR")
    if not bd:
        return None
    for d in sorted(Path(bd).glob("linux-*")):
        if "headers" not in d.name and (d / ".config").is_file():
            return d / ".config"
    return None


#: ILA finding #24 (2026-09-24; docs/planning/linux_lanes/FINDINGS_TRIAGE.md):
#: something programmed 13 bytes of the DUT's SST26 flash at 0x20000 to zero and
#: nobody knows what. Nothing in the Linux harness may be able to program or erase
#: that flash, so no harness kernel carries the MTD / SPI-NOR stack or the AXI Quad
#: SPI master (src/linux_harness/DRIVER_MATRIX.md §2.6, SUPERSEDED). rv32's
#: defconfig turns MTD and MTD_SPI_NOR ON: leaving them out of a fragment is not
#: enough, the fragment must say "is not set".
KCONFIG_FORBIDDEN = ("CONFIG_MTD", "CONFIG_MTD_SPI_NOR", "CONFIG_SPI_XILINX",
                     "CONFIG_SPI_XILINX_QSPI")

#: B1 silicon 2026-09-24 (BOOT lane; configs/kernel_fragment_harness.config has the
#: evidence): on MicroBlaze V the `time` CSR IS mcycle, and riscv-pmu-sbi's SBI PMU
#: COUNTER_STOP makes OpenSBI set mcountinhibit.CY -- rdtime, sched_clock,
#: get_cycles() and random_get_entropy() freeze for good, the crng never
#: initialises, the first blocking getrandom() hangs and stage0's WDOG resets the
#: board. rv32's defconfig turns it ON (default y): the fragment must unset it.
#: QEMU cannot show this (its `time` is not mcycle), so this gate is the guard.
KCONFIG_FORBIDDEN_CLOCK = ("CONFIG_RISCV_PMU_SBI",)


def _set_syms(text: str, syms: tuple[str, ...]) -> list[str]:
    """Every `SYM=<value>` line (=y, =m or a value, not =n) for SYM in syms; a
    longer symbol sharing the prefix never matches ("=" must follow the name)."""
    return [m.group(0) for sym in syms
            for m in re.finditer(rf"^{sym}=(\S+)", text, re.M) if m.group(1) != "n"]


def kconfig_gate(config: Path) -> None:
    """Refuse a kernel .config that sets any KCONFIG_FORBIDDEN or
    KCONFIG_FORBIDDEN_CLOCK symbol (=y, =m or a value; "# ... is not set" and
    absence both pass), or that embeds an initramfs (CONFIG_INITRAMFS_SOURCE
    non-empty) without CONFIG_INITRAMFS_COMPRESSION_NONE=y. A file with no
    CONFIG_ line at all is refused too, so a wrong path cannot pass vacuously."""
    if not config.is_file():
        die(f"kconfig-gate: {config} is not a file")
    text = config.read_text(errors="replace")
    if not re.search(r"^CONFIG_\w+=", text, re.M):
        die(f"kconfig-gate: {config} has no CONFIG_ assignments -- not a kernel .config")
    bad = _set_syms(text, KCONFIG_FORBIDDEN)
    if bad:
        die(f"kconfig-gate: {config} sets {', '.join(bad)} -- the harness kernel must not be "
            "able to program the DUT's SST26 (ILA #24; unset them in "
            "configs/kernel_fragment_harness.config)")
    bad = _set_syms(text, KCONFIG_FORBIDDEN_CLOCK)
    if bad:
        die(f"kconfig-gate: {config} sets {', '.join(bad)} -- its SBI PMU COUNTER_STOP "
            "inhibits mcycle, which IS `time` on MicroBlaze V: sched_clock/get_cycles/"
            "the crng freeze and stage0's WDOG resets the board (B1 2026-09-24; unset it "
            "in configs/kernel_fragment_harness.config)")
    src = re.search(r'^CONFIG_INITRAMFS_SOURCE="([^"]*)"', text, re.M)
    if src and src.group(1).strip() and \
            not re.search(r"^CONFIG_INITRAMFS_COMPRESSION_NONE=y$", text, re.M):
        die(f"kconfig-gate: {config} embeds an initramfs ({src.group(1)}) COMPRESSED -- "
            "inflating it costs ~20 s of the MBV's 42.9 s stage0 WDOG budget and stalls "
            "every request_module() until it ends (set CONFIG_INITRAMFS_COMPRESSION_NONE=y "
            "in configs/kernel_fragment_harness.config)")
    print(f"mps3_image: kernel .config: {', '.join(s[7:] for s in KCONFIG_FORBIDDEN)} "
          "all unset (ILA #24); RISCV_PMU_SBI unset (MBV time == mcycle); "
          f"initramfs {'uncompressed' if src and src.group(1).strip() else 'not embedded'}")


def _hex(v: str) -> int | None:
    try:
        return int(v.strip(), 16)
    except (ValueError, AttributeError):
        return None


def _version_keys(target: Path) -> dict[str, str]:
    vp = target / VERSION
    if not vp.is_file():
        die(f"greybox-gate: {vp} missing -- run `manifest` first")
    return dict(ln.split("=", 1) for ln in vp.read_text().splitlines()
                if "=" in ln and not ln.startswith("#"))


def greybox_gate(target: Path) -> None:
    """harnessd's swap_fsm seeds the greybox clearing from /etc/mps3/greybox_clear.bin;
    without it the first swap away from the greybox fails closed (B1 step f), and a
    clearing for ANOTHER static must never reach this fabric's ICAP. So:
      - an image provisioned for a static (etc/mps3/static_id != 0) carries a
        clearing: 4..1 MiB whole words, mode 0644;
      - version's greybox_clear_static_id equals that static_id, and its
        greybox_clear_sha256 is the file's;
      - an unprovisioned image carries none (it could only be another static's)."""
    sp = target / "etc/mps3/static_id"
    first = sp.read_text().splitlines()[:1] if sp.is_file() else []
    sid = _hex(first[0]) if first and first[0].strip() else 0
    if sid is None:
        die(f"greybox-gate: etc/mps3/static_id {first[0]!r} is not hex")
    keys = _version_keys(target)
    gb = target / GREYBOX
    if not sid:
        if gb.exists():
            die("greybox-gate: an UNPROVISIONED image (static_id 0) carries a greybox clearing "
                "-- it can only belong to some other static; provision MPS3_STATIC_ID too")
        print("mps3_image: greybox-gate: unprovisioned image, no clearing (none expected)")
        return
    if not gb.is_file():
        die(f"greybox-gate: the image is provisioned for 0x{sid:08X} but carries no "
            "/etc/mps3/greybox_clear.bin -- the first swap away from the greybox would fail "
            "closed; build with MPS3_GREYBOX_CLEAR=<that mint's prod/..._partial_clear.bin>")
    n = gb.stat().st_size
    if n == 0 or n % 4 or n > GREYBOX_MAX:
        die(f"greybox-gate: greybox_clear.bin is {n} B -- harnessd takes 4..{GREYBOX_MAX} B, "
            "whole words")
    if gb.stat().st_mode & 0o777 != 0o644:
        die(f"greybox-gate: greybox_clear.bin mode {gb.stat().st_mode & 0o777:o}, want 644")
    gsid = _hex(keys.get("greybox_clear_static_id", ""))
    if gsid != sid:
        die(f"greybox-gate: the clearing is recorded for {keys.get('greybox_clear_static_id')} "
            f"but the image is provisioned for 0x{sid:08X} -- a clearing for another static "
            "must never reach this fabric's ICAP")
    if keys.get("greybox_clear_sha256") != _sha_file(gb):
        die("greybox-gate: version's greybox_clear_sha256 is not the file's (changed after "
            "the manifest?)")
    print(f"mps3_image: greybox-gate: clearing for 0x{sid:08X}, {n} B, "
          f"sha256 {keys['greybox_clear_sha256'][:16]}...")


def _patches_sha() -> str:
    pd = os.environ.get("MPS3_KERNEL_PATCH_DIR")
    if not pd or not Path(pd).is_dir():
        return "unknown"
    h = hashlib.sha256()
    for p in sorted(Path(pd).glob("*.patch")):
        h.update(p.name.encode() + b"\0" + p.read_bytes())
    return h.hexdigest()


def manifest(target: Path) -> None:
    env = os.environ
    epoch = env.get("SOURCE_DATE_EPOCH")
    now = time.gmtime(int(epoch)) if epoch else time.gmtime()
    sid = "0x00000000"
    sp = target / "etc/mps3/static_id"
    if sp.is_file():
        first = sp.read_text().splitlines()[:1]
        if first and first[0].strip():
            sid = first[0].strip()
    hd = target / HARNESSD
    ph = (target / "etc/inittab").is_file() and \
        "harnessd-placeholder" in (target / "etc/inittab").read_text()
    if hd.is_file():
        harnessd = _sha_file(hd)
    elif ph:
        harnessd = "placeholder"
    else:
        harnessd = "absent"
    kc = _kernel_config()
    stage0 = env.get("MPS3_STAGE0_SHA256") or "unknown"
    gb = target / GREYBOX
    gb_sha = _sha_file(gb) if gb.is_file() else "none"
    gb_sid = env.get("MPS3_GREYBOX_CLEAR_STATIC_ID", "").strip() if gb.is_file() else ""
    try:
        gb_sid = f"0x{int(gb_sid, 16):08X}" if gb_sid else "none"
    except ValueError:
        die(f"manifest: MPS3_GREYBOX_CLEAR_STATIC_ID={gb_sid!r} is not hex")
    rows = [
        ("format", "1"),
        ("impl", "linux"),
        ("harness", env.get("MPS3_HARNESS_VERSION", "0.0.0")),
        ("ver32", env.get("MPS3_HARNESS_VER32", "0x00000000")),
        ("sha", env.get("MPS3_HARNESS_SHA", "unknown")),
        ("dirty", env.get("MPS3_HARNESS_DIRTY", "1")),
        ("date", env.get("MPS3_HARNESS_DATE", time.strftime("%Y-%m-%d", now))),
        ("build_date", time.strftime("%Y-%m-%dT%H:%M:%SZ", now)),
        ("image_kind", env.get("MPS3_IMAGE_KIND", "release")),
        ("variant", env.get("MPS3_VARIANT", "default")),
        ("idle", env.get("MPS3_IDLE", "nowfi")),
        ("kernel", env.get("MPS3_KERNEL_VERSION", "6.18.7")),
        ("kernel_config_sha256", _sha_file(kc) if kc else "unknown"),
        ("kernel_patches_sha256", _patches_sha()),
        ("rootfs_tree_sha256", tree_sha(target, {VERSION})),
        ("harnessd_sha256", harnessd),
        ("stage0_sha256", stage0),
        ("static_id", sid),
        ("greybox_clear_sha256", gb_sha),
        ("greybox_clear_static_id", gb_sid),
        ("buildroot", env.get("MPS3_BUILDROOT_VERSION", "unknown")),
        ("opensbi", env.get("MPS3_OPENSBI_VERSION", "1.6")),
    ]
    for k, v in rows:
        if not re.fullmatch(r"[a-z0-9_]+", k) or not re.fullmatch(r"[!-~]+", v):
            die(f"manifest {k}={v!r}: keys [a-z0-9_]+, values printable, no spaces")
    body = ["# /etc/mps3/version — MPS3 Linux harness image manifest (IMAGE_CONTRACT §6).",
            "# Generated at build; image-owned (never persisted). key=value, # comments."]
    body += [f"{k}={v}" for k, v in rows]
    vp = target / VERSION
    vp.parent.mkdir(parents=True, exist_ok=True)
    vp.write_text("\n".join(body) + "\n")
    print(f"mps3_image: /etc/mps3/version written (harnessd={harnessd[:16]}, "
          f"static_id={sid}, rootfs_tree={rows[14][1][:12]})")


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    cmd, target = argv[1], Path(argv[2])
    if cmd == "kconfig-gate":
        if argv[2] == "auto":            # the post-build hook: Buildroot's BUILD_DIR
            kc = _kernel_config()
            if kc is None:
                print("mps3_image: kconfig-gate: no kernel .config under $BUILD_DIR yet "
                      "(build.sh step 6 gates the built one)")
                return 0
            target = kc
        kconfig_gate(target)
        return 0
    if not target.is_dir():
        die(f"TARGET {target} is not a directory")
    if cmd == "hostkey-gate":
        hostkey_gate(target, argv[3] if len(argv) > 3 else "release")
    elif cmd == "inittab":
        inittab(target, argv[3] if len(argv) > 3 else "default")
    elif cmd == "manifest":
        manifest(target)
    elif cmd == "greybox-gate":
        greybox_gate(target)
    else:
        die(f"unknown subcommand {cmd}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
