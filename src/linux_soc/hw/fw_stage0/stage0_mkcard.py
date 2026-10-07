#!/usr/bin/env python3
"""stage0_mkcard.py -- the user-uSD layout stage0 boots from
(docs/planning/linux_lanes/STAGE0_CONTRACT.md §6), as files you can dd, a
whole-card image, or a check of an existing card.

    MBR entry  type  start LBA        size               use
    p4         0xDA  2048   (1 MiB)   65536  (32 MiB)    D13 overlay store
    p1         0x7F  67584  (33 MiB)  131072 (64 MiB)    stage0 slot A
    p2         0x7F  198656 (97 MiB)  131072 (64 MiB)    stage0 slot B
    p3         0x83  329728 (161 MiB) rest / --persist   /persist (ext4)
    LBA 1, 2   boot-select sector, two copies (default slot + seq)

stage0 READS the card and never writes it. A blank card is handled by rescue-
booting a blob into RAM (stage0_push.py); Linux then writes this layout.

    stage0_mkcard.py card --slot-a boot.img [--slot-b same|none|IMG]
                          [--default A|B] [--seq N] --out-dir DIR
                          [--card-img card.img --card-mib 512]
        -> DIR/mbr.bin, DIR/bootsel.bin, DIR/slotA.img, DIR/slotB.img + a dd recipe
    stage0_mkcard.py bootsel --default B --seq 7 --out bootsel.bin [--copy 0|1]
        -> the boot-select sector(s) Linux writes to LBA 1 / 2
    stage0_mkcard.py check CARD_OR_IMAGE
        -> the MBR, the default slot and both slots, verified the way stage0 will

Stdlib only; shares stage0_pack.py's image checker.
"""
import argparse
import binascii
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from stage0_pack import check_image, IMAGE_MAX  # noqa: E402

BLOCK = 512
LBA_STORE, N_STORE = 2048, 65536
LBA_A, LBA_B, N_SLOT = 67584, 198656, 131072
LBA_PERSIST = 329728
DEFAULT_PERSIST_MIB = 256
TYPE_SLOT, TYPE_PERSIST, TYPE_STORE = 0x7F, 0x83, 0xDA
BOOTSEL_MAGIC, BOOTSEL_VERSION = 0x43423053, 1
BOOTSEL_LBAS = (1, 2)
DISK_SIG = 0x53304C42


def mbr(persist_blocks, disk_sig=DISK_SIG):
    """Entry order is the contract's (p1 A, p2 B, p3 persist, p4 store); disk
    order is not (p4 sits at LBA 2048 where D13 decided)."""
    sec = bytearray(BLOCK)
    struct.pack_into("<I", sec, 440, disk_sig)
    parts = [(TYPE_SLOT, LBA_A, N_SLOT), (TYPE_SLOT, LBA_B, N_SLOT),
             (TYPE_PERSIST, LBA_PERSIST, persist_blocks), (TYPE_STORE, LBA_STORE, N_STORE)]
    for i, (typ, lba, n) in enumerate(parts):
        e = 446 + 16 * i
        # CHS fields: the LBA-only convention (0xFE 0xFF 0xFF), as sfdisk writes
        sec[e:e + 16] = struct.pack("<B3sB3sII", 0, b"\xfe\xff\xff", typ, b"\xfe\xff\xff", lba, n)
    sec[510], sec[511] = 0x55, 0xAA
    return bytes(sec)


def bootsel(default, seq):
    d = {"A": 1, "B": 2}[default.upper()]
    sec = bytearray(BLOCK)
    struct.pack_into("<IIII", sec, 0, BOOTSEL_MAGIC, BOOTSEL_VERSION, seq, d)
    struct.pack_into("<I", sec, 0x1FC, binascii.crc32(bytes(sec[:0x1FC])) & 0xFFFFFFFF)
    return bytes(sec)


def parse_bootsel(sec):
    """-> (default 'A'/'B', seq) or None -- stage0's rule for one copy."""
    if len(sec) < BLOCK:
        return None
    magic, ver, seq, d = struct.unpack_from("<IIII", sec, 0)
    crc = struct.unpack_from("<I", sec, 0x1FC)[0]
    if magic != BOOTSEL_MAGIC or ver != BOOTSEL_VERSION or d not in (1, 2):
        return None
    if binascii.crc32(sec[:0x1FC]) & 0xFFFFFFFF != crc:
        return None
    return ("A" if d == 1 else "B", seq)


def pick_bootsel(c0, c1):
    a, b = parse_bootsel(c0), parse_bootsel(c1)
    if a and (not b or a[1] >= b[1]):
        return a
    return b if b else ("A", 0)


def load_image(path):
    img = open(path, "rb").read()
    problems, info = check_image(img)
    if problems:
        sys.exit("stage0_mkcard: %s is not a bootable stage0 image: %s" % (path, "; ".join(problems)))
    if len(img) > N_SLOT * BLOCK:
        sys.exit("stage0_mkcard: %s is %d B > the 64 MiB slot" % (path, len(img)))
    return img, info


def cmd_card(a):
    img_a, info_a = load_image(a.slot_a)
    if a.slot_b == "same":
        img_b, info_b = img_a, info_a
    elif a.slot_b == "none":
        img_b, info_b = None, None
    else:
        img_b, info_b = load_image(a.slot_b)
    if a.card_mib:
        total = a.card_mib * 2048
        persist = total - LBA_PERSIST
        if persist < 2048:
            sys.exit("stage0_mkcard: a %d MiB card leaves no room for /persist" % a.card_mib)
    else:
        persist = (a.persist_mib or DEFAULT_PERSIST_MIB) * 2048
    if a.persist_mib and a.card_mib:
        persist = min(persist, a.persist_mib * 2048)
    os.makedirs(a.out_dir, exist_ok=True)
    m = mbr(persist)
    bs = bootsel(a.default, a.seq)
    files = {"mbr.bin": m, "bootsel.bin": bs + bs, "slotA.img": img_a}
    if img_b is not None:
        files["slotB.img"] = img_b
    for name, data in files.items():
        with open(os.path.join(a.out_dir, name), "wb") as fh:
            fh.write(data)
    p4 = open(a.p4_init, "rb").read() if a.p4_init else None
    if p4 is not None and len(p4) > N_STORE * BLOCK:
        sys.exit("stage0_mkcard: --p4-init is larger than the 32 MiB store partition")

    print("stage0_mkcard: slot A %s (%d B, hdr_crc 0x%08X); slot B %s; default %s seq %d"
          % (a.slot_a, len(img_a), info_a["header_crc32"],
             "none" if img_b is None else "%d B hdr_crc 0x%08X" % (len(img_b), info_b["header_crc32"]),
             a.default.upper(), a.seq))
    print("write it (sdX = the USER microSD, never the MCC config card):")
    print("  dd if=%s/mbr.bin     of=/dev/sdX bs=512 count=1 conv=fsync" % a.out_dir)
    print("  dd if=%s/bootsel.bin of=/dev/sdX bs=512 seek=1 count=2 conv=fsync" % a.out_dir)
    print("  dd if=%s/slotA.img   of=/dev/sdX bs=512 seek=%d conv=fsync" % (a.out_dir, LBA_A))
    if img_b is not None:
        print("  dd if=%s/slotB.img   of=/dev/sdX bs=512 seek=%d conv=fsync" % (a.out_dir, LBA_B))
    print("  (p3 /persist at LBA %d is left for Linux to format; p4 0xDA at LBA %d is D13's)"
          % (LBA_PERSIST, LBA_STORE))

    if a.card_img:
        total = (a.card_mib or 512) * 2048
        if total < LBA_PERSIST + persist:
            sys.exit("stage0_mkcard: --card-mib too small for the layout")
        with open(a.card_img, "wb") as fh:
            fh.truncate(total * BLOCK)                 # sparse
            fh.seek(0)
            fh.write(m)
            fh.write(bs)
            fh.write(bs)
            if p4 is not None:
                fh.seek(LBA_STORE * BLOCK)
                fh.write(p4)
            fh.seek(LBA_A * BLOCK)
            fh.write(img_a)
            if img_b is not None:
                fh.seek(LBA_B * BLOCK)
                fh.write(img_b)
        print("card image: %s (%d MiB, sparse)" % (a.card_img, total // 2048))
    return 0


def cmd_bootsel(a):
    s = bootsel(a.default, a.seq)
    data = s if a.copy is not None else s + s
    with open(a.out, "wb") as fh:
        fh.write(data)
    where = ("LBA %d" % BOOTSEL_LBAS[a.copy]) if a.copy is not None else "LBA 1 and 2"
    print("stage0_mkcard: boot-select default %s seq %d -> %s (%s)"
          % (a.default.upper(), a.seq, a.out, where))
    return 0


def read_at(fh, lba, n):
    fh.seek(lba * BLOCK)
    return fh.read(n)


def slot_image(fh, lba, nblocks):
    """Read a slot's image by its own header, bounded by the partition."""
    limit = min(nblocks * BLOCK, IMAGE_MAX)
    hdr = read_at(fh, lba, BLOCK)
    if len(hdr) < 32:
        return hdr
    n = struct.unpack_from("<I", hdr, 8)[0]
    end = 32 + 16 * min(n, 8)
    for i in range(min(n, 8)):
        if 32 + 16 * i + 16 <= len(hdr):
            so, _d, ln, _c = struct.unpack_from("<IIII", hdr, 32 + 16 * i)
            end = max(end, so + ln)
    return read_at(fh, lba, min(end, limit))


def cmd_check(a):
    with open(a.card, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        card_blocks = size // BLOCK if size else 0
        sec = read_at(fh, 0, BLOCK)
        if len(sec) < BLOCK or sec[510] != 0x55 or sec[511] != 0xAA:
            print("%s: no MBR -> stage0 goes to rescue (\"card has no stage0 slots\")" % a.card)
            return 2
        slots = {}
        for i, name in ((0, "A"), (1, "B")):
            typ = sec[446 + 16 * i + 4]
            lba, n = struct.unpack_from("<II", sec, 446 + 16 * i + 8)
            ok = typ == TYPE_SLOT and lba and n and (not card_blocks or lba + n <= card_blocks)
            slots[name] = (lba, n) if ok else None
        default, seq = pick_bootsel(read_at(fh, BOOTSEL_LBAS[0], BLOCK),
                                    read_at(fh, BOOTSEL_LBAS[1], BLOCK))
        print("%s: default slot %s (boot-select seq %d)" % (a.card, default, seq))
        good = []
        for name in (default, "B" if default == "A" else "A"):
            if not slots[name]:
                print("  slot %s: no slot (MBR entry %d is not a type-0x7F partition)"
                      % (name, 1 if name == "A" else 2))
                continue
            img = slot_image(fh, *slots[name])
            problems, info = check_image(img)
            if problems:
                print("  slot %s @LBA %d: NOT bootable: %s" % (name, slots[name][0], "; ".join(problems)))
            else:
                print("  slot %s @LBA %d: OK, %d B, pc=0x%08X a1=0x%08X hdr_crc=0x%08X"
                      % (name, slots[name][0], info["length"], info["pc"], info["a1"],
                         info["header_crc32"]))
                good.append(name)
        if not good:
            print("  -> stage0 would go to rescue")
            return 1
        print("  -> stage0 would boot slot %s" % good[0])
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("card", help="MBR + boot-select + slot images (+ a whole-card image)")
    c.add_argument("--slot-a", required=True)
    c.add_argument("--slot-b", default="same", help="same (default), none, or an image")
    c.add_argument("--default", default="A", choices=["A", "B", "a", "b"])
    c.add_argument("--seq", type=int, default=1)
    c.add_argument("--persist-mib", type=int, default=0)
    c.add_argument("--card-mib", type=int, default=0)
    c.add_argument("--p4-init", default=None, help="raw bytes to place at the start of p4 (0xDA)")
    c.add_argument("--out-dir", required=True)
    c.add_argument("--card-img", default=None)
    b = sub.add_parser("bootsel", help="the boot-select sector(s)")
    b.add_argument("--default", required=True, choices=["A", "B", "a", "b"])
    b.add_argument("--seq", type=int, required=True)
    b.add_argument("--copy", type=int, choices=[0, 1], default=None,
                   help="emit one copy only (0 -> LBA 1, 1 -> LBA 2)")
    b.add_argument("--out", required=True)
    k = sub.add_parser("check", help="verify a card / card image read-only")
    k.add_argument("card")
    a = ap.parse_args()
    return {"card": cmd_card, "bootsel": cmd_bootsel, "check": cmd_check}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
