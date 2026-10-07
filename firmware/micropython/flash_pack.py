#!/usr/bin/env python3
# -----------------------------------------------------------------------------
# NanoSoC hybrid-XiP MicroPython — QSPI flash image packer
#
# Assembles ONE packed QSPI flash image for the TRUE product boot-from-flash:
#
#   flash 0x00000000  boot table   : v1 header (magic "BOOT") + 1 entry.
#                                     Entry 0 names the HOT image (app_offset,
#                                     app_size, app_crc32). stage1_* unused (0).
#   flash 0x00001000  HOT image    : raw .bin of the loadable IMEM sections
#                                     (.isr_vector + .text + .data). stage-0
#                                     CPU-copies this into IMEM (0x10000000),
#                                     CRC32-checks it, then REMAPs + jumps.
#   flash 0x00020000  COLD segment : micropython_cold.bin (.text.xip +
#                                     .rodata.xip, linked at aperture
#                                     0x70020000). Executes IN PLACE from flash
#                                     through the CG092 cache.
#
# The boot-table layout and the CRC32 MUST match the stage-0 bootloader exactly:
#   * struct layout    -> nanosoc_m0_soc/firmware/bootloader/boot_table.h
#                         header  {magic, version, num_entries, reserved} (16 B)
#                         entry   {stage1_offset, stage1_size, app_offset,
#                                  app_size, flags, stage1_crc32, app_crc32,
#                                  reserved} (32 B)  — all <u32 little-endian.
#   * CRC32 polynomial -> boot_table.h boot_crc32(): reflected 0xEDB88320,
#                         init 0xFFFFFFFF, final XOR ~ — i.e. the standard
#                         zlib/PNG CRC32, which is exactly binascii.crc32().
#
# Emits:
#   --out-bin  raw flash .bin (0xFF-filled gaps, truncated past the last datum)
#   --out-hex  byte-format $readmemh file (@00000000 header, 2 hex digits/line)
#              for backdooring the SST26VF064B VIP array at byte offset 0.
#
# A joint work commissioned on behalf of SoC Labs, under Arm Academic Access
# license.  Copyright (C) 2026, SoC Labs (www.soclabs.org)
# -----------------------------------------------------------------------------

import argparse
import binascii
import struct
import sys

# Constants — MUST match nanosoc_m0_soc/firmware/bootloader/boot_table.h.
BOOT_TABLE_MAGIC       = 0x424F4F54   # "BOOT"
BOOT_TABLE_VERSION     = 1            # boot_table.h is v1
BOOT_ENTRY_FLAG_VALID  = (1 << 0)
BOOT_TABLE_HEADER_SIZE = 16           # {magic, version, num_entries, reserved}
BOOT_TABLE_ENTRY_SIZE  = 32


def crc32(data: bytes) -> int:
    """CRC32 identical to boot_table.h boot_crc32() (== zlib/binascii.crc32)."""
    return binascii.crc32(data) & 0xFFFFFFFF


def build_boot_table(app_offset, app_size, app_crc):
    """v1 header + a single entry 0 describing the HOT image."""
    header = struct.pack('<IIII',
                         BOOT_TABLE_MAGIC,      # 0x00 magic
                         BOOT_TABLE_VERSION,    # 0x04 version
                         1,                     # 0x08 num_entries
                         0)                     # 0x0C reserved
    entry = struct.pack('<IIIIIIII',
                        0,                      # stage1_offset (unused)
                        0,                      # stage1_size   (unused)
                        app_offset,             # app_offset -> HOT image
                        app_size,               # app_size
                        BOOT_ENTRY_FLAG_VALID,  # flags
                        0,                      # stage1_crc32  (unused)
                        app_crc,                # app_crc32
                        0)                      # reserved
    assert len(header) == BOOT_TABLE_HEADER_SIZE
    assert len(entry) == BOOT_TABLE_ENTRY_SIZE
    return header + entry


def main():
    p = argparse.ArgumentParser(description="NanoSoC hybrid-XiP flash packer")
    p.add_argument('--hot', required=True, help='HOT image raw .bin (IMEM blob)')
    p.add_argument('--cold', required=True, help='COLD .xip segment raw .bin')
    p.add_argument('--hot-offset', type=lambda x: int(x, 0), default=0x1000,
                   help='flash byte offset of the HOT image (default 0x1000)')
    p.add_argument('--cold-offset', type=lambda x: int(x, 0), default=0x20000,
                   help='flash byte offset of the COLD segment (default 0x20000)')
    p.add_argument('--boot-table-offset', type=lambda x: int(x, 0), default=0x0,
                   help='flash byte offset of the boot table (default 0x0)')
    p.add_argument('--out-bin', required=True, help='output raw flash .bin')
    p.add_argument('--out-hex', required=True,
                   help='output byte-hex $readmemh (@0 header, 2 digits/line)')
    p.add_argument('--pad-align', type=lambda x: int(x, 0), default=0x1000,
                   help='0xFF-pad the emitted image up to this alignment past '
                        'the last datum (default 0x1000) — covers cache-line '
                        'prefetch past the cold segment with deterministic FF')
    args = p.parse_args()

    with open(args.hot, 'rb') as f:
        hot = f.read()
    with open(args.cold, 'rb') as f:
        cold = f.read()

    if not hot:
        sys.exit("ERROR: HOT image is empty")
    if not cold:
        sys.exit("ERROR: COLD segment is empty")

    # Sanity: regions must not overlap.
    if args.hot_offset + len(hot) > args.cold_offset:
        sys.exit(f"ERROR: HOT image ({len(hot)} B @ 0x{args.hot_offset:x}) "
                 f"runs into COLD @ 0x{args.cold_offset:x}")
    if args.boot_table_offset + BOOT_TABLE_HEADER_SIZE + BOOT_TABLE_ENTRY_SIZE \
            > args.hot_offset:
        sys.exit("ERROR: boot table runs into the HOT image")

    app_crc = crc32(hot)
    boot_table = build_boot_table(args.hot_offset, len(hot), app_crc)

    end = args.cold_offset + len(cold)
    if args.pad_align:
        end = (end + args.pad_align - 1) & ~(args.pad_align - 1)

    # 0xFF is the erased-flash state.
    flash = bytearray(b'\xFF' * end)
    flash[args.boot_table_offset:args.boot_table_offset + len(boot_table)] = boot_table
    flash[args.hot_offset:args.hot_offset + len(hot)] = hot
    flash[args.cold_offset:args.cold_offset + len(cold)] = cold

    with open(args.out_bin, 'wb') as f:
        f.write(flash)

    # Byte-format $readmemh: single @00000000 header, 2 hex digits per line. The
    # SST26 VIP array is byte-wide, so this deposits one flash byte per element
    # starting at byte 0. Contiguous (gaps already 0xFF) — no @ jumps, so a
    # cache line straddling any boundary reads deterministic bytes, never X.
    with open(args.out_hex, 'w') as f:
        f.write("@00000000\n")
        f.write("\n".join(f"{b:02X}" for b in flash))
        f.write("\n")

    print(f"[flash_pack] boot table @0x{args.boot_table_offset:06X}  "
          f"magic BOOT v{BOOT_TABLE_VERSION}, 1 entry")
    print(f"[flash_pack] HOT   image @0x{args.hot_offset:06X}  "
          f"{len(hot)} bytes  CRC32=0x{app_crc:08X}")
    print(f"[flash_pack] COLD  seg   @0x{args.cold_offset:06X}  "
          f"{len(cold)} bytes")
    print(f"[flash_pack] wrote {args.out_bin} and {args.out_hex} "
          f"({end} bytes, 0xFF-padded to 0x{args.pad_align:X})")


if __name__ == '__main__':
    main()
