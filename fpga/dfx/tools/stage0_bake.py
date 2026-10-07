#!/usr/bin/env python3
"""stage0_bake.py -- the guards around `make -C fpga/dfx mint-stage0`.

    elf    the stage0 ELF is a RISC-V rv32 image that fits the MBV LMB below the
           stage0 status block (plan §3 LMB map), entry at the reset vector
    proc   the ONE processor in the MMI whose LMB is the MBV's (128 KiB at 0x0)
           -> the -proc path updatemem needs
    identity  print "<static_id> <ver32>" of a routed static: static_id.txt and the
           ONE USR_ACCESS word its full .bit carries -- the two constants the
           stage0 build compiles in (the Makefile passes them to STAGE0's make)
    fieldable  refuse a stage0_bake.json whose mint_kind is not `mint`
    bit    after updatemem: the baked bitstream is the SAME implementation as the
           base (UserID, USR_ACCESS), updatemem really embedded something,
           the static identity is untouched, and the stage0 inside was compiled
           FOR this static -- then write stage0_bake.json

WHY
---
`mint-stage0` is today's firmware bake (mint stage 6) with a different CPU in the
LMB: `updatemem` patches the stage0 ELF into the MicroBlaze V's 128 KiB LMB BRAM
of the routed greybox config, and the result is the flashable base the MCC loads
from its SD card. The precedent is src/linux_soc/hw/build_dbg.tcl (the July
bootstub bake). Four things went wrong, or nearly did, in the two bakes this
repo has already done, and each is a check here:

  1. The -proc path. The July recipe took the FIRST `InstPath=` in the MMI. The
     MBV static's MMI holds TWO processors: the MicroBlaze V and the DDR4 MIG's
     own calibration MicroBlaze MCS (`.../ddr4_0/.../mcs0/inst/microblaze_I`,
     64 KiB). It worked because the MBV happened to be listed first. Baking
     stage0 into the MIG's calibration BRAM would leave DDR uncalibratable and
     the harness dark. `proc` selects by the LMB the plan fixes (§3: 128 KiB at
     0x0) and refuses anything but exactly one match.
  2. Identity. updatemem rewrites BRAM init frames only, so the baked image must
     be the SAME implementation: header UserID (what the device reports as
     USERCODE, what every overlay manifest's static_usercode is bound to) and
     the USR_ACCESS word(s) byte-equal to the base. NOT the same length: the
     shell bitstream is compressed, so the length follows the BRAM contents. static_id is a
     CRC of static_routed_locked.dcp, which a bake cannot touch -- `bit`
     re-derives it from that file and compares with static_id.txt anyway, so the
     record says so from evidence, not from assumption.
  3. A no-op. updatemem exits 0 after printing its usage text when a path is
     wrong; the artefact is the only proof. Byte-identical to the base = refused.
  4. The LMB map. stage0 owns 0x00000-0x1FDFF; 0x1FE00 is the stage0 status
     block and 0x1FF00 the diag mailbox harnessd reads over UIO (plan §3). The
     July stage0.ld put the stack top at 0x20000, i.e. ON the mailbox. `elf`
     refuses any loadable byte, and a stack top (`_estack`/`__stack_top`/
     `_stack_top` when the symbol table has one), above --lmb-top.
  5. Which static the stage0 is FOR (2026-09-23 safety review). stage0 is built
     with the static's static_id and HARNESS_VER32 as compile-time constants
     (STAGE0_CONTRACT.md names the -D's), and harnessd reports THAT fabric
     value -- not the card's /etc/mps3/static_id -- so a card moved to another
     board, or a rootfs provisioned for another static, cannot make a board lie
     about what it is. The constants are bound to the bitstream by updatemem,
     exactly like the bare-metal ELF's greybox static_id. `--expect-static-id`
     / `--expect-ver32` read the two u32 symbols out of the ELF (the Python
     twin of tools/elf_static_id.py, which needs mb-nm and so cannot read an
     rv32 ELF) and refuse a stage0 built for any other static. A stage0 that
     does not carry the symbols at all is refused too: it cannot be shown to
     belong here.

Stdlib only, Python 3.6+. No Vivado, no board. Reuses gen_manifest.py's UserID
parser and bit_identity.py's USR_ACCESS reader -- one parser each, not a third.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
DFX_DIR = os.path.dirname(HERE)
sys.path.insert(0, DFX_DIR)
sys.path.insert(0, HERE)

from pathlib import Path  # noqa: E402

from gen_manifest import usercode_from_bitstream  # noqa: E402
from bit_identity import usr_access_check  # noqa: E402

EM_RISCV = 243
#: plan §3: stage0 image + stack live below the status block.
DEFAULT_LMB_TOP = 0x1FE00
#: plan §3: the MBV LMB is 128 KiB at 0x0 (the MB v11 one is not).
DEFAULT_LMB_BYTES = 128 * 1024
STACK_SYMBOLS = ("_estack", "__stack_top", "_stack_top", "__stack_end")


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
    return crc & 0xFFFFFFFF


# ---------------------------------------------------------------------------
# ELF (32-bit little-endian only -- that is all rv32 is)
# ---------------------------------------------------------------------------
def read_elf32(path):
    data = Path(path).read_bytes()
    if data[:4] != b"\x7fELF":
        raise ValueError("%s is not an ELF file" % path)
    if data[4] != 1:
        raise ValueError("%s is not ELFCLASS32 (EI_CLASS=%d) -- stage0 is rv32" % (path, data[4]))
    if data[5] != 1:
        raise ValueError("%s is not little-endian (EI_DATA=%d)" % (path, data[5]))
    (e_type, e_machine, _ver, e_entry, e_phoff, e_shoff, _flags, _ehsize,
     e_phentsize, e_phnum, e_shentsize, e_shnum, e_shstrndx) = struct.unpack_from(
        "<HHIIIIIHHHHHH", data, 16)
    segs = []
    for i in range(e_phnum):
        (p_type, p_offset, p_vaddr, p_paddr, p_filesz, p_memsz, p_flags,
         _align) = struct.unpack_from("<IIIIIIII", data, e_phoff + i * e_phentsize)
        segs.append({"type": p_type, "offset": p_offset, "vaddr": p_vaddr,
                     "paddr": p_paddr, "filesz": p_filesz, "memsz": p_memsz,
                     "flags": p_flags})
    syms = {}
    sections = []
    for i in range(e_shnum):
        sections.append(struct.unpack_from("<IIIIIIIIII", data, e_shoff + i * e_shentsize))
    for sh in sections:
        (_name, sh_type, _fl, _addr, sh_offset, sh_size, sh_link, _info, _al,
         sh_entsize) = sh
        if sh_type != 2 or not sh_entsize:          # SHT_SYMTAB
            continue
        strtab = sections[sh_link]
        str_off = strtab[4]
        for j in range(sh_size // sh_entsize):
            st_name, st_value, _sz, _info2, _other, _shndx = struct.unpack_from(
                "<IIIBBH", data, sh_offset + j * sh_entsize)
            if not st_name:
                continue
            end = data.index(b"\0", str_off + st_name)
            syms[data[str_off + st_name:end].decode("latin-1")] = st_value
    return {"type": e_type, "machine": e_machine, "entry": e_entry,
            "segments": segs, "symbols": syms, "data": data}


def elf_u32_at(elf, addr):
    """The little-endian u32 the LOADED image holds at `addr` (i.e. what lands
    in the LMB), or None if no PT_LOAD file image covers it."""
    for seg in elf["segments"]:
        if seg["type"] != 1:
            continue
        if seg["vaddr"] <= addr and addr + 4 <= seg["vaddr"] + seg["filesz"]:
            off = seg["offset"] + (addr - seg["vaddr"])
            return struct.unpack_from("<I", elf["data"], off)[0]
    return None


#: STAGE0_CONTRACT.md (assumed until published): the two u32 constants stage0
#: is compiled with, as symbols in its image. Overridable from the Makefile.
DEFAULT_SYM_STATIC_ID = "mps3_stage0_static_id"
DEFAULT_SYM_VER32 = "mps3_stage0_ver32"


def check_elf(path, lmb_top, entry, expect=None):
    """-> (problems, summary). `expect` = {symbol: wanted u32} -- each symbol
    must exist, lie in the loaded image, and hold exactly that value."""
    problems = []
    try:
        elf = read_elf32(path)
    except (OSError, ValueError, struct.error) as exc:
        return ["cannot read %s as an ELF32: %s" % (path, exc)], {}
    if elf["machine"] != EM_RISCV:
        problems.append("e_machine=%d, not RISC-V (%d): this is not a MicroBlaze V "
                        "image (a classic MicroBlaze ELF is e_machine 189)"
                        % (elf["machine"], EM_RISCV))
    if elf["entry"] != entry:
        problems.append("entry 0x%X != 0x%X -- the MBV comes out of reset at the LMB "
                        "base, and stage0 must start there" % (elf["entry"], entry))
    hi = 0
    for seg in elf["segments"]:
        if seg["type"] != 1 or seg["memsz"] == 0:   # PT_LOAD
            continue
        top = seg["paddr"] + seg["memsz"]
        hi = max(hi, top)
        if top > lmb_top:
            problems.append(
                "a loadable segment reaches 0x%X (paddr 0x%X + memsz 0x%X) -- above "
                "0x%X, where the stage0 status block and the diag mailbox live "
                "(plan §3 LMB map)" % (top, seg["paddr"], seg["memsz"], lmb_top))
    stack = None
    for name in STACK_SYMBOLS:
        if name in elf["symbols"]:
            stack = (name, elf["symbols"][name])
            break
    if stack and stack[1] > lmb_top:
        problems.append(
            "stack top %s = 0x%X is above 0x%X: the stack would grow down over the "
            "stage0 status block (0x1FE00) and the diag mailbox (0x1FF00) -- "
            "plan §3 LMB map. Move it to <= 0x%X in the linker script."
            % (stack[0], stack[1], lmb_top, lmb_top))
    baked = {}
    for sym, want in sorted((expect or {}).items()):
        if sym not in elf["symbols"]:
            problems.append(
                "no symbol %s: this stage0 was not built with the static's identity "
                "compiled in (STAGE0_CONTRACT.md), so nothing shows it belongs to "
                "this static" % sym)
            continue
        got = elf_u32_at(elf, elf["symbols"][sym])
        baked[sym] = None if got is None else "0x%08X" % got
        if got is None:
            problems.append("%s @0x%X is not in any loaded segment (a .bss symbol "
                            "is zero at reset, not a baked constant)"
                            % (sym, elf["symbols"][sym]))
        elif got != want:
            problems.append(
                "%s = 0x%08X but this static is 0x%08X -- the stage0 was built for "
                "ANOTHER static; baked in, the board would report the wrong "
                "identity to harnessd and every client" % (sym, got, want))
    summary = {"entry": "0x%X" % elf["entry"], "machine": elf["machine"],
               "baked_constants": baked,
               "load_top": "0x%X" % hi, "free_below_lmb_top": lmb_top - hi,
               "stack_symbol": stack[0] if stack else None,
               "stack_top": ("0x%X" % stack[1]) if stack else None}
    return problems, summary


def expectations(args):
    exp = {}
    if getattr(args, "expect_static_id", None):
        exp[args.sym_static_id] = int(args.expect_static_id, 16)
    if getattr(args, "expect_ver32", None):
        exp[args.sym_ver32] = int(args.expect_ver32, 16)
    return exp


def cmd_elf(args):
    problems, s = check_elf(args.elf, args.lmb_top, args.entry, expectations(args))
    if problems:
        for p in problems:
            print("STAGE0 ELF REFUSED: %s: %s" % (args.elf, p), file=sys.stderr)
        return 1
    print("stage0 ELF ok: %s rv32, entry %s, image top %s, %d bytes free below 0x%X%s"
          % (os.path.basename(args.elf), s["entry"], s["load_top"],
             s["free_below_lmb_top"], args.lmb_top,
             ", stack top %s=%s" % (s["stack_symbol"], s["stack_top"]) if s["stack_symbol"]
             else " (no stack-top symbol to check)"))
    for sym, val in sorted(s["baked_constants"].items()):
        print("  %s = %s (matches)" % (sym, val))
    return 0


# ---------------------------------------------------------------------------
# the static's identity, as the stage0 build needs it
# ---------------------------------------------------------------------------
def static_identity(bit, static_id_file):
    """-> (static_id, ver32) as 0x%08X strings. ver32 is the USR_ACCESS word the
    full .bit writes once in EVERY SLR stream (bit_identity.usr_access_check, a
    packet walk -- not a byte search: RC1). No word, a missing or extra write in
    any SLR, or two values is refused, and so is a static_id that is not 8 hex
    digits or is zero: stage0 compiles in exactly one of each."""
    text = Path(static_id_file).read_text().strip()
    if not re.fullmatch(r"0x[0-9A-Fa-f]{8}", text) or int(text, 16) == 0:
        raise SystemExit("stage0_bake: %s holds %r, not a static_id (0x + 8 hex, "
                         "non-zero)" % (static_id_file, text))
    sid = "0x%08X" % int(text, 16)
    value, problems = usr_access_check(str(bit))
    if problems or value is None:
        raise SystemExit(
            "stage0_bake: %s -- stage0 compiles in exactly ONE HARNESS_VER32, and this "
            "bitstream does not give one: %s"
            % (bit, "; ".join(problems) or "no USR_ACCESS write in any SLR (an unstamped "
                                             "build: DFX_NO_VERSION_STAMP)"))
    return sid, "0x%08X" % value


def cmd_identity(args):
    sid, v32 = static_identity(args.bit, args.static_id_file)
    print("%s %s" % (sid, v32))
    return 0


# ---------------------------------------------------------------------------
# MMI
# ---------------------------------------------------------------------------
PROC_RE = re.compile(
    r'<Processor\s+Endianness="(\w+)"\s+InstPath="([^"]+)">\s*'
    r'<AddressSpace\s+Name="[^"]*"\s+Begin="(\d+)"\s+End="(\d+)"')


def mmi_processors(mmi):
    text = Path(mmi).read_text(errors="replace")
    return [{"endianness": e, "inst": p, "begin": int(b), "end": int(en)}
            for e, p, b, en in PROC_RE.findall(text)]


def select_proc(mmi, lmb_bytes):
    procs = mmi_processors(mmi)
    hits = [p for p in procs if p["begin"] == 0 and p["end"] + 1 == lmb_bytes]
    if len(hits) != 1:
        listing = "; ".join("%s [0x%X..0x%X]" % (p["inst"], p["begin"], p["end"])
                            for p in procs) or "none"
        raise SystemExit(
            "stage0_bake: want exactly ONE processor in %s whose LMB is %d KiB at 0x0 "
            "(the MicroBlaze V, plan §3), found %d. Processors in the MMI: %s. "
            "(An MBV static's MMI also lists the DDR4 MIG's calibration MCS; baking "
            "stage0 into that would leave DDR uncalibratable.) Pass MBV_PROC=<InstPath> only if you "
            "have read the MMI and know which one it is."
            % (mmi, lmb_bytes // 1024, len(hits), listing))
    return hits[0]


def cmd_proc(args):
    p = select_proc(args.mmi, args.lmb_bytes)
    print(p["inst"])
    return 0


# ---------------------------------------------------------------------------
# the baked bitstream
# ---------------------------------------------------------------------------
def cmd_bit(args):
    problems = []
    base, baked = Path(args.base), Path(args.baked)
    uc_base = usercode_from_bitstream(base)
    uc_baked = usercode_from_bitstream(baked)
    if uc_base != uc_baked:
        problems.append("UserID %s != base %s -- not the same implementation; every "
                        "overlay keyed to the base would be refused (or worse)"
                        % (uc_baked, uc_base))
    ua_base, pb = usr_access_check(str(base))
    ua_baked, pk = usr_access_check(str(baked))
    problems.extend("base: %s" % p for p in pb)
    problems.extend("baked: %s" % p for p in pk)
    if not (pb or pk) and ua_base != ua_baked:
        problems.append("USR_ACCESS %s != base %s" % (
            None if ua_baked is None else "0x%08X" % ua_baked,
            None if ua_base is None else "0x%08X" % ua_base))
    # NOT a length check. The shell bitstream is COMPRESS=TRUE (mps3_harness.xdc),
    # so updatemem re-emits the compressed frame stream and the length moves with
    # the BRAM contents: the then-fielded 0x3F1A560F bake grew 11,123,518 -> 12,041,006 B
    # and the P-mint's 13,561,173 -> 13,606,289 B. An equal-length rule here
    # refused a correct bake (P-mint run 2, 2026-09-24). What must hold is the
    # identity (above) and that the bake did something (below).
    if baked.stat().st_size < base.stat().st_size // 2:
        problems.append("length %d is under half the base's %d -- not a full "
                        "bitstream (truncated write?)" % (baked.stat().st_size,
                                                          base.stat().st_size))
    elif baked.read_bytes() == base.read_bytes():
        problems.append("byte-identical to the base: updatemem embedded NOTHING (it "
                        "exits 0 after printing its usage text on a bad path)")
    static_id = Path(args.static_id_file).read_text().strip().upper().replace("0X", "0x")
    locked_crc = None
    if args.locked_dcp:
        locked_crc = "0x%08X" % crc32_of(args.locked_dcp)
        if locked_crc != static_id:
            problems.append("CRC-32 of %s is %s but static_id.txt says %s -- the locked "
                            "static this tree minted is not the one on disk"
                            % (os.path.basename(args.locked_dcp), locked_crc, static_id))
    if args.stamp_json and os.path.isfile(args.stamp_json):
        stamp = json.loads(Path(args.stamp_json).read_text())
        if stamp.get("static_id") and stamp["static_id"].upper() != static_id.upper():
            problems.append("static_stamp.json static_id %s != static_id.txt %s"
                            % (stamp["static_id"], static_id))
        if stamp.get("usercode") and stamp["usercode"].upper() != uc_baked.upper():
            problems.append("static_stamp.json usercode %s != the baked UserID %s"
                            % (stamp["usercode"], uc_baked))
    expect = {}
    if not args.no_identity:
        if ua_base is None:
            problems.append("the base carries no single USR_ACCESS word (%s); stage0's "
                            "ver32 has nothing to be checked against"
                            % ("; ".join(pb) or "none written"))
        else:
            expect[args.sym_ver32] = ua_base
        expect[args.sym_static_id] = int(static_id, 16)
    elf_problems, elf_summary = check_elf(args.elf, args.lmb_top, args.entry, expect)
    problems.extend(elf_problems)

    # The STATIC's probes file (ltx_sidecar.py `static`, mint stage 4) is bound to
    # this flashable base here: same UserID (the implementation) and static_id.
    static_ltx = None
    if args.static_ltx_sidecar:
        sp = args.static_ltx_sidecar
        if not os.path.isfile(sp):
            problems.append("no %s: the MicroBlaze V static carries debug cores (SEAM-8), "
                            "so mint stage 4 must have written its static .ltx + sidecar" % sp)
        else:
            side = json.loads(Path(sp).read_text())
            if (side.get("static_usercode") or "").upper() != uc_baked.upper():
                problems.append("%s is for UserID %s, this base is %s"
                                % (os.path.basename(sp), side.get("static_usercode"), uc_baked))
            if (side.get("static_id") or "").upper() != static_id.upper():
                problems.append("%s is for static_id %s, not %s"
                                % (os.path.basename(sp), side.get("static_id"), static_id))
            ltx_p = os.path.join(os.path.dirname(sp), side.get("ltx") or "")
            if not os.path.isfile(ltx_p) or sha256_of(ltx_p) != side.get("ltx_sha256"):
                problems.append("%s does not match the sidecar %s"
                                % (side.get("ltx"), os.path.basename(sp)))
            else:
                static_ltx = {"name": side["ltx"], "sha256": side["ltx_sha256"],
                              "sidecar": os.path.basename(sp),
                              "sidecar_sha256": sha256_of(sp), "uuids": side.get("uuids")}

    if problems:
        for p in problems:
            print("STAGE0 BAKE REFUSED: %s: %s" % (baked.name, p), file=sys.stderr)
        return 1

    record = {
        "schema": "mps3-stage0-bake",
        "schema_version": "1",
        "generated_by": "fpga/dfx/tools/stage0_bake.py",
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mint_kind": args.mint_kind,
        "static_id": static_id,
        "static_id_rederived": locked_crc is not None,
        "static_usercode": uc_baked,
        "usr_access": [] if ua_baked is None else ["0x%08X" % ua_baked],
        "proc": args.proc,
        "proc_endianness_in_mmi": args.proc_endianness or None,
        "stage0_elf": {"name": os.path.basename(args.elf),
                       "sha256": sha256_of(args.elf),
                       "bytes": os.path.getsize(args.elf),
                       "lmb_top": "0x%X" % args.lmb_top},
        "stage0_elf_check": elf_summary,
        "stage0_identity_checked": not args.no_identity,
        "static_ltx": static_ltx,
        "base_bit": {"name": base.name, "sha256": sha256_of(base)},
        "baked_bit": {"name": baked.name, "sha256": sha256_of(baked),
                      "bytes": baked.stat().st_size},
        "note": "updatemem changed BRAM init frames only: UserID and USR_ACCESS are the "
                "base's, so every overlay keyed to this static stays valid (the length "
                "differs: the bitstream is compressed).",
    }
    if args.record:
        Path(args.record).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print("stage0 bake ok: %s  static_id %s  UserID %s  stage0 sha256 %s  (%s)"
          % (baked.name, static_id, uc_baked, record["stage0_elf"]["sha256"][:12],
             args.record or "no record written"))
    return 0


def cmd_fieldable(args):
    """`make fielded-files` (mbv): a prototype (P-mint) static is never fielded."""
    try:
        kind = json.loads(Path(args.record).read_text()).get("mint_kind")
    except (OSError, ValueError) as exc:
        print("fielded-files: cannot read %s (%s) -- mint-stage0 has not run, so there is "
              "no flashable base to field" % (args.record, exc), file=sys.stderr)
        return 1
    if kind != "mint":
        print("fielded-files: %s says mint_kind=%s -- a prototype (P-mint) static is "
              "never fielded" % (args.record, kind), file=sys.stderr)
        return 1
    return 0


def _int(text):
    return int(text, 0)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd")

    el = sub.add_parser("elf", help="the stage0 ELF fits the MBV LMB map")
    el.add_argument("--elf", required=True)
    el.add_argument("--lmb-top", type=_int, default=DEFAULT_LMB_TOP)
    el.add_argument("--entry", type=_int, default=0)
    el.add_argument("--expect-static-id", default=None)
    el.add_argument("--expect-ver32", default=None)
    el.add_argument("--sym-static-id", default=DEFAULT_SYM_STATIC_ID)
    el.add_argument("--sym-ver32", default=DEFAULT_SYM_VER32)
    el.set_defaults(func=cmd_elf)

    idn = sub.add_parser("identity", help="print '<static_id> <ver32>' of a static")
    idn.add_argument("--bit", required=True, help="the full static .bit (config_rm_greybox.bit)")
    idn.add_argument("--static-id-file", required=True)
    idn.set_defaults(func=cmd_identity)

    pr = sub.add_parser("proc", help="the MBV processor path in the MMI")
    pr.add_argument("--mmi", required=True)
    pr.add_argument("--lmb-bytes", type=_int, default=DEFAULT_LMB_BYTES)
    pr.set_defaults(func=cmd_proc)

    bt = sub.add_parser("bit", help="verify the baked bitstream, write the record")
    bt.add_argument("--base", required=True)
    bt.add_argument("--baked", required=True)
    bt.add_argument("--elf", required=True)
    bt.add_argument("--proc", required=True)
    bt.add_argument("--proc-endianness", default="")
    bt.add_argument("--static-id-file", required=True)
    bt.add_argument("--locked-dcp", default=None)
    bt.add_argument("--stamp-json", default=None)
    bt.add_argument("--lmb-top", type=_int, default=DEFAULT_LMB_TOP)
    bt.add_argument("--entry", type=_int, default=0)
    bt.add_argument("--mint-kind", default="mint", choices=("mint", "prototype"))
    bt.add_argument("--static-ltx-sidecar", default=None,
                    help="config_<ref>_static.ltx.json: bind the static probes file to "
                         "this flashable base (required when given)")
    bt.add_argument("--sym-static-id", default=DEFAULT_SYM_STATIC_ID)
    bt.add_argument("--sym-ver32", default=DEFAULT_SYM_VER32)
    bt.add_argument("--no-identity", action="store_true",
                    help="skip the baked static_id/ver32 check (bring-up ONLY: the "
                         "record then says stage0_identity_checked=false and "
                         "linux_image.py refuses to package it for fielding)")
    bt.add_argument("--record", default=None)
    bt.set_defaults(func=cmd_bit)

    fd = sub.add_parser("fieldable", help="refuse a prototype stage0_bake.json")
    fd.add_argument("--record", required=True)
    fd.set_defaults(func=cmd_fieldable)

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
