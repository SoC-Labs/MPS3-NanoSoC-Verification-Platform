#!/usr/bin/env python3
"""
stage0_pack.py -- pack the Linux boot payload into a stage0 boot image the
first-stage loader (fw_stage0/) can read from storage. Fork of the proven
firmware/micropython/flash_pack.py idea, adapted to the stage0_boot.h format
(32-byte header + 16-byte region entries) and DDR destinations.

Format (stage0_boot.h):
    header  : <8 x u32>  magic, version (2), num_entries, entry_pc, entry_a0,
                          entry_a1, flags, header_crc32 = the TABLE CRC: CRC32
                          of the header (that field 0) + all the entries, so it
                          binds every region's dst/len/CRC and identifies the image
    entries : num_entries x <4 x u32>  src_offset, dst_addr, len, crc32
    payloads: each region's bytes at its src_offset (4-byte aligned, 0xFF pad)

CRC32 = binascii.crc32 (reflected 0xEDB88320) -- identical to the loader's
s0_crc32(), asserted by the host unit test.

Two ready-made layouts match docs/UNATTENDED_BOOT_PLAN.md:

  4-region (today's map, matches dfx_swap_boot.tcl):
    stage0_pack.py --out boot.img --pc 0x80000000 --a1 0x82200000 \
        fw_jump.bin@0x80000000 Image@0x80400000 \
        shell_linux.dtb@0x82200000 rootfs.cpio.gz@0x84000000

  single-blob (OpenSBI FW_PAYLOAD; a1 irrelevant):
    stage0_pack.py --out boot.img --pc 0x80000000 --a1 0 fw_payload.bin@0x80000000

The same bytes go into a uSD slot (stage0_mkcard.py) and into a rescue push
(stage0_push.py). Verify one exactly as stage0 will:
    stage0_pack.py --check boot.img
"""
import argparse, binascii, struct, sys

MAGIC   = 0x424C3053   # "S0LB"
VERSION = 2            # v2: header_crc32 covers the header AND the entry table
HDR_FMT = "<8I"        # 32 bytes
ENT_FMT = "<4I"        # 16 bytes
MAX_ENTRIES = 8


def parse_region(spec):
    """'path@0xADDR' -> (path, addr)."""
    if "@" not in spec:
        sys.exit(f"region '{spec}' must be path@0xADDR")
    path, addr = spec.rsplit("@", 1)
    return path, int(addr, 0)


IMAGE_MAX = 64 * 1024 * 1024      # S0_IMAGE_MAX: slot size = rescue staging window
DST_BASE, DST_END = 0x80000000, 0xB0000000   # stage0's destination window


def check_image(img):
    """Verify an image exactly as stage0's s0_load() will: magic, version,
    header CRC, num_entries, every region inside the image and inside the DDR
    destination window, every region CRC. Returns (problems, info)."""
    problems, info = [], {}
    if len(img) < 32:
        return ["shorter than a header (%d B)" % len(img)], info
    if len(img) > IMAGE_MAX:
        problems.append("%d B > the 64 MiB stage0 maximum" % len(img))
    f = list(struct.unpack_from(HDR_FMT, img, 0))
    magic, ver, n, pc, a0, a1, _flags, hcrc = f
    info.update(pc=pc, a0=a0, a1=a1, entries=n, header_crc32=hcrc, length=len(img))
    if magic != MAGIC:
        return problems + ["no boot table (magic 0x%08X)" % magic], info
    if ver != VERSION:
        return problems + ["bad version %d (stage0 takes %d)" % (ver, VERSION)], info
    if not 1 <= n <= MAX_ENTRIES:
        return problems + ["bad num_entries %d" % n], info
    if len(img) < 32 + 16 * n:
        return problems + ["entry table truncated"], info
    f[7] = 0
    if binascii.crc32(struct.pack(HDR_FMT, *f) + img[32:32 + 16 * n]) & 0xFFFFFFFF != hcrc:
        return problems + ["table CRC (header or an entry corrupt)"], info
    regions = []
    for i in range(n):
        off = 32 + 16 * i
        if off + 16 > len(img):
            return problems + ["entry %d truncated" % i], info
        so, dst, ln, crc = struct.unpack_from(ENT_FMT, img, off)
        regions.append(dict(src_offset=so, dst=dst, len=ln, crc32=crc))
        if so + ln > len(img):
            problems.append("region %d runs past the image (truncated)" % i)
        elif binascii.crc32(img[so:so + ln]) & 0xFFFFFFFF != crc:
            problems.append("region %d payload CRC" % i)
        if dst < DST_BASE or dst + ln > DST_END:
            problems.append("region %d dst 0x%08X+0x%X is outside 0x%08X..0x%08X"
                            % (i, dst, ln, DST_BASE, DST_END))
    info["regions"] = regions
    return problems, info


def main():
    if "--check" in sys.argv[1:]:
        ap = argparse.ArgumentParser(description="verify a stage0 boot image")
        ap.add_argument("--check", required=True, metavar="IMG")
        a = ap.parse_args()
        img = open(a.check, "rb").read()
        problems, info = check_image(img)
        for p in problems:
            print("stage0_pack --check: %s: %s" % (a.check, p))
        if problems:
            return 1
        print("stage0_pack --check: %s OK: %d B, %d region(s), pc=0x%08X a1=0x%08X hdr_crc=0x%08X"
              % (a.check, info["length"], info["entries"], info["pc"], info["a1"],
                 info["header_crc32"]))
        return 0
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--pc", required=True, help="entry_pc, e.g. 0x80000000")
    ap.add_argument("--a0", default="0", help="a0 at hand-off (hartid), default 0")
    ap.add_argument("--a1", required=True, help="a1 (dtb addr; 0 for FW_PAYLOAD)")
    ap.add_argument("--align", default="0x1000", help="payload alignment, default 4 KiB")
    ap.add_argument("regions", nargs="+", metavar="path@0xADDR")
    args = ap.parse_args()

    regions = [parse_region(s) for s in args.regions]
    if not (1 <= len(regions) <= MAX_ENTRIES):
        sys.exit(f"need 1..{MAX_ENTRIES} regions, got {len(regions)}")
    align = int(args.align, 0)

    # Layout: header + entries, then each payload aligned. Compute offsets.
    body_off = len(struct.pack(HDR_FMT, *([0] * 8))) + \
               len(regions) * len(struct.pack(ENT_FMT, *([0] * 4)))

    def align_up(x, a):
        return (x + a - 1) & ~(a - 1)

    entries, blobs, cur = [], [], align_up(body_off, align)
    for path, addr in regions:
        with open(path, "rb") as f:
            data = f.read()
        crc = binascii.crc32(data) & 0xFFFFFFFF
        entries.append((cur, addr, len(data), crc))
        blobs.append((cur, data))
        print(f"  {path}: {len(data):,} B -> DDR 0x{addr:08X}  src_off 0x{cur:X}  crc 0x{crc:08X}")
        cur = align_up(cur + len(data), align)

    # the table CRC: the header with header_crc32 zeroed, then every entry
    hdr0 = struct.pack(HDR_FMT, MAGIC, VERSION, len(regions),
                       int(args.pc, 0), int(args.a0, 0), int(args.a1, 0), 0, 0)
    table = b"".join(struct.pack(ENT_FMT, *e) for e in entries)
    hcrc = binascii.crc32(hdr0 + table) & 0xFFFFFFFF
    hdr = struct.pack(HDR_FMT, MAGIC, VERSION, len(regions),
                      int(args.pc, 0), int(args.a0, 0), int(args.a1, 0), 0, hcrc)

    img = bytearray(hdr)
    for e in entries:
        img += struct.pack(ENT_FMT, *e)
    for off, data in blobs:
        if len(img) < off:
            img += b"\xFF" * (off - len(img))
        img += data

    with open(args.out, "wb") as f:
        f.write(img)
    print(f"stage0_pack: wrote {args.out} ({len(img):,} B, {len(regions)} region(s), "
          f"pc=0x{int(args.pc,0):08X} a1=0x{int(args.a1,0):08X})")


if __name__ == "__main__":
    sys.exit(main())
