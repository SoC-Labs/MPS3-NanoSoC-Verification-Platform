#!/usr/bin/env python3
"""
verify_shell_image.py -- STATIC acceptance gate for an `updatemem`'d shell
bitstream, run BEFORE anyone loads it onto a board.

WHY: a bad shell image costs a board window, and the failure modes this repo has
actually shipped are all SILENT -- the board pings, reports a plausible identity,
passes the acceptance gate, and then cannot do the one thing it was rebuilt for:

  * LITE-vs-FIFO HWICAP mismatch (fixed d28c292): the firmware's HWICAP write
    protocol did not match the BD's `axi_hwicap C_MODE {0}` FIFO, so every RM
    swap failed silently. Nothing at build time noticed.
  * updatemem NO-OP: updatemem exits 0 after printing its usage text when an
    input path is wrong. A rekey script once wrote a sentinel naming a bitstream
    that was never created.
  * WRONG-IDENTITY re-bake: fpga/dfx/overlay/ is one directory that every re-key
    overwrites, so firmware built for an OLDER bitstream bakes the LATEST
    static_id. The shell then reports an id that does not match its own fabric
    and config_agent rejects every overlay pushed to it.
  * WRONG MMI: the DFX config is a different implementation from shell_harness,
    so their BRAM placements differ; the wrong .mmi corrupts the embed silently.

Every check here is static: no board, no Vivado, no JTAG. Pure stdlib, plus the
Vitis mb-nm/mb-objdump if they are on the system (skipped-with-a-shout if not).

USAGE
  python3 verify_shell_image.py \
      --bit      OUT.bit           # the updatemem OUTPUT (what will be flashed)
      --base-bit BASE.bit          # the bitstream updatemem consumed
      --elf      shell_fw.elf      # the ELF that was baked in
      --meminfo  config_rm_greybox.mmi
      --expect-static-id 0x0EE58A4D
      [--expect-xvc-target jtagbb|swdbb|dbgbr]
      [--expect-hwicap fifo|lite]
      [--expect-proc u_shell/shell_bd_i/microblaze_0]
      [--vitis /apps/Xilinx/Vitis/2024.1]

Exit 0 = every check passed. Exit 1 = at least one FAILED. Exit 2 = usage error.
A check that cannot run reports SKIP and does NOT pass silently.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# Xilinx bitstream sync word, big-endian in the file, after the ASCII header.
SYNC_WORD = bytes.fromhex("AA995566")
# Bus-width auto-detect pattern that precedes the sync word in a .bit
BUS_WIDTH = bytes.fromhex("000000BB")

# ---------------------------------------------------------------------------
# THE CONTRACT WITH THE FIRMWARE SOURCES.
#
# Everything below is a name or a number this script looks for inside a built
# ELF. None of it is checkable from here -- an `mb-nm` grep for a symbol that no
# longer exists reports SKIP, and a `--expect-xvc-target` value the firmware
# build no longer accepts is rejected by argparse three commits after the cause,
# at mint time, with a board window already booked.
#
# That is not hypothetical: `--expect-xvc-target` offered only {swdbb, dbgbr}
# for six weeks after the A6 SWD->JTAG cutover made `jtagbb` the live target
# (firmware/platform/Makefile:286-292 says so in as many words -- "NEW WORK USES
# jtagbb" -- while still naming this script as the reason `swdbb` is kept).
#
# They are module constants, not literals buried in the checks, so
# tests/firmware_logic/test_verify_shell_image_contract.py can import them and
# hold each one against the firmware tree. Add a name here, not inline.
# ---------------------------------------------------------------------------

#: XVC_TARGET values firmware/platform/Makefile:301-308 accepts. An UNSET
#: XVC_TARGET builds the Debug-Bridge path, which is spelled "dbgbr" here.
XVC_BITBANG_TARGETS = ("jtagbb", "swdbb")
XVC_TARGETS = XVC_BITBANG_TARGETS + ("dbgbr",)

#: MicroBlaze materialises a 32-bit address with `imm <high16>`; these are the
#: high halves of the two candidate MMIO pages, matched as decimal in the
#: disassembly. JTAGBB and SWDBB SHARE 0x44A7 -- see check_elf().
XVC_BB_IMM    = 0x44A7   # MPS3_JTAGBB_BASE == MPS3_SWDBB_BASE, >> 16
XVC_DBGBR_IMM = 0x44A8   # MPS3_DBGBR_BASE, >> 16

#: The baked shell identity, read out of .rodata/.data by symbol.
STATIC_ID_SYM = "mps3_greybox_clearing_static_id"

#: hwicap_writer.c compiles ONE of these two sets, never both -- the d28c292
#: trap. They sit in opposite arms of one `#if MPS3_HWICAP_FIFO`
#: (hwicap_writer.c:63/145), so exactly one reaches the ELF.
#:
#: `hwicap_fifo_write_words` used to be listed here too and is GONE: it was one
#: of swap_fsm.c's pre-consolidation duplicate writers, and after
#: firmware/common/hwicap_writer.c absorbed both copies the name survived only in
#: two comments (swap_fsm.c:19, hwicap_writer.h:10) and in this script. Being an
#: `any()` it never broke the check -- it was simply a name in a contract that
#: nothing on the other side answered to, which is how the whole file drifted.
HWICAP_LITE_SYMS = ("hwicap_lite_write",)
HWICAP_FIFO_SYMS = ("hwicap_fifo_drain",)

#: Symbols whose ABSENCE means a build flag did not take.
LOADBEARING_SYMS = ("clcd_init", "clcd_poll", "xvc_server_init",
                    "xvc_server_poll", "jtag_server_init",
                    "coordinator_handle_ping")

#: Every firmware symbol name this script greps for, in one place.
GREPPED_SYMS = ((STATIC_ID_SYM,) + HWICAP_LITE_SYMS + HWICAP_FIFO_SYMS
                + LOADBEARING_SYMS)

results: list[tuple[str, str, str]] = []   # (state, name, detail)


def ok(name: str, detail: str = "") -> None:
    results.append(("PASS", name, detail))


def bad(name: str, detail: str = "") -> None:
    results.append(("FAIL", name, detail))


def skip(name: str, detail: str = "") -> None:
    results.append(("SKIP", name, detail))


def note(name: str, detail: str = "") -> None:
    results.append(("INFO", name, detail))


# ---------------------------------------------------------------------------
# .bit ASCII header
# ---------------------------------------------------------------------------
def parse_bit_header(path: Path) -> dict:
    """Vivado writes a short plaintext header before the configuration data.

    Fields are TLV-ish; we do not need the framing, only the tokens. Read a
    generous 512 bytes -- these files are ~12 MB and the header is well under 200.
    """
    head = path.open("rb").read(512)
    txt = head.decode("latin-1")
    out: dict[str, str] = {}
    m = re.search(r"([A-Za-z0-9_]+);UserID=(0[xX])?([0-9A-Fa-f]{1,8});", txt)
    if m:
        out["design"] = m.group(1)
        out["userid"] = "0x" + m.group(3).rjust(8, "0").upper()
    m = re.search(r"COMPRESS=(\w+)", txt)
    if m:
        out["compress"] = m.group(1)
    m = re.search(r"Version=([0-9.]+)", txt)
    if m:
        out["version"] = m.group(1)
    m = re.search(r"(xc[a-z0-9]+-[a-z0-9]+-[0-9]+-[a-z0-9]+)", txt)
    if m:
        out["part"] = m.group(1)
    m = re.search(r"(\d{4}/\d{2}/\d{2})", txt)
    if m:
        out["date"] = m.group(1)
    m = re.search(r"(\d{2}:\d{2}:\d{2})", txt)
    if m:
        out["time"] = m.group(1)
    return out


def check_bit(bit: Path, base: Path | None, label: str) -> dict:
    h = parse_bit_header(bit)
    size = bit.stat().st_size

    # 1. non-trivial size. An updatemem no-op leaves nothing, and a truncated
    #    write leaves something far too small to be a KU115 configuration.
    if size < 1_000_000:
        bad(f"{label}: size", f"{size} bytes -- far too small for a KU115 "
                              "configuration (updatemem no-op or truncated write?)")
    else:
        ok(f"{label}: size", f"{size:,} bytes")

    # 2. the header must actually parse. A .bin (headerless) or a corrupt file
    #    silently fails to yield a UserID, and every identity check downstream
    #    would then be vacuous.
    for field in ("design", "userid", "part"):
        if field not in h:
            bad(f"{label}: header/{field}", "not found in the first 512 bytes -- "
                                            "is this a .bit (not a .bin)?")
    if "userid" in h:
        ok(f"{label}: UserID", h["userid"] + "  (= BITSTREAM.CONFIG.USERID, the "
                                             "identity the device reports as USERCODE)")
    if "design" in h:
        note(f"{label}: design", h["design"])
    if "part" in h:
        note(f"{label}: part", h["part"])
    if "compress" in h:
        note(f"{label}: compress", h["compress"])
    if h.get("date") or h.get("time"):
        note(f"{label}: built", f"{h.get('date','?')} {h.get('time','?')}")

    # 3. the configuration stream must begin properly: bus-width pattern then the
    #    0xAA995566 sync word. A header-only or ASCII-truncated file fails here.
    blob = bit.open("rb").read(4096)
    si = blob.find(SYNC_WORD)
    if si < 0:
        bad(f"{label}: sync word", "0xAA995566 not found in the first 4 KiB -- "
                                   "the configuration stream is missing or corrupt")
    else:
        bw = "yes" if BUS_WIDTH in blob[:si] else "no"
        ok(f"{label}: sync word", f"0xAA995566 at offset {si} "
                                  f"(bus-width pattern before it: {bw})")

    # 4. against the base updatemem consumed.
    if base is not None:
        hb = parse_bit_header(base)
        if h.get("userid") and hb.get("userid"):
            if h["userid"] == hb["userid"]:
                ok(f"{label}: UserID preserved",
                   f"{h['userid']} == base -- updatemem did not disturb the "
                   "implementation identity, so overlays keyed to this static "
                   "are still valid")
            else:
                bad(f"{label}: UserID preserved",
                    f"{h['userid']} != base {hb['userid']} -- the output is NOT "
                    "the same implementation; every overlay partial is invalid "
                    "against it")
        if h.get("part") and hb.get("part") and h["part"] != hb["part"]:
            bad(f"{label}: part", f"{h['part']} != base {hb['part']}")
        # updatemem MUST have changed something: identical bytes means it wrote
        # the base straight through and the firmware never landed.
        if bit.stat().st_size == base.stat().st_size and \
           bit.read_bytes() == base.read_bytes():
            bad(f"{label}: content changed vs base",
                "byte-identical to the base bitstream -- updatemem embedded "
                "NOTHING (a no-op run still exits 0)")
        else:
            ok(f"{label}: content changed vs base",
               f"differs from base ({base.stat().st_size:,} -> {size:,} bytes); "
               "the ELF was embedded")
    return h


# ---------------------------------------------------------------------------
# .mmi
# ---------------------------------------------------------------------------
def check_meminfo(mmi: Path, expect_proc: str | None, lay: dict | None) -> None:
    try:
        root = ET.parse(mmi).getroot()
    except Exception as e:                                   # noqa: BLE001
        bad("mmi: parses", f"{e}")
        return
    procs = root.findall(".//Processor")
    if not procs:
        bad("mmi: Processor", "no <Processor> element")
        return
    paths = [p.get("InstPath", "") for p in procs]
    note("mmi: InstPath", ", ".join(paths))
    if expect_proc:
        if expect_proc in paths:
            ok("mmi: -proc matches",
               f"{expect_proc} is present, so `updatemem -proc {expect_proc}` "
               "resolves (a bare 'microblaze_0' does NOT resolve in the DFX "
               "config's hierarchy)")
        else:
            bad("mmi: -proc matches",
                f"{expect_proc} not in {paths} -- updatemem would embed nothing")

    # The address space END is the LMB size. It must cover the ELF, and it is the
    # number firmware/platform/Makefile's LMB_KB has to agree with: the LMB decode
    # ALIASES, so a mismatch is silent.
    ends = [int(a.get("End", "0")) for a in root.findall(".//AddressSpace")]
    if not ends:
        skip("mmi: AddressSpace", "no <AddressSpace> element")
        return
    end = max(ends)
    kb = (end + 1) // 1024
    note("mmi: LMB size", f"0..{end} = {kb} KiB  (build with LMB_KB={kb})")
    if lay is None:
        skip("mmi: ELF fits the LMB", "no ELF layout")
        return

    size = end + 1
    if lay["mem_top"] <= size:
        ok("mmi: ELF fits the LMB",
           f"runtime top 0x{lay['mem_top']:08X} ({lay['mem_top']:,}) <= {size:,}")
    else:
        bad("mmi: ELF fits the LMB",
            f"runtime top 0x{lay['mem_top']:08X} ({lay['mem_top']:,}) EXCEEDS "
            f"{size:,} -- .bss/.heap/.stack do not fit; rebuild with a matching "
            f"LMB_KB={kb}")

    # THE ALIASING TRAP (firmware/platform/Makefile): the diag mailbox is anchored
    # to the END of the LMB, so LMB_KB MOVES it -- 256 KB -> 0x0003FF00,
    # 512 KB -> 0x0007FF00, 1 MB -> 0x000FFF00 at diag v8 -- and the LMB address
    # decode ALIASES, so a firmware built for the wrong size puts its mailbox at
    # an address that still reads back plausible data. Assert the ELF's NOLOAD
    # mailbox segment sits exactly where THIS bitstream's LMB ends.
    #
    # TWO SIZES are accepted because the STRUCT SIZE moves the anchor too: diag
    # v8 reserves 0x100, v5..v7 reserved 0x80, and an already-fielded image is
    # still the second kind. Accepting both does NOT weaken the check -- an
    # LMB_KB mismatch moves the mailbox by at least a whole LMB, never by 0x80 --
    # and the message names which layout the ELF actually carries.
    sizes = {0x100: "diag v8 (256 B)", 0x80: "diag v5-v7 (128 B)"}
    hit = next(((size - r, lbl) for r, lbl in sizes.items()
                if any(v == size - r for v, _ in lay["noload"])), None)
    if hit is not None:
        mailbox, layout = hit
        ok("mmi: diag mailbox anchored",
           f".mps3_diag NOLOAD segment at 0x{mailbox:08X} == LMB end - "
           f"0x{size - mailbox:X} [{layout}], so LMB_KB={kb} agrees with the "
           f"bitstream (a mismatch ALIASES silently)")
    elif lay["noload"]:
        mailbox = size - 0x100
        bad("mmi: diag mailbox anchored",
            f"ELF has NOLOAD segment(s) at "
            f"{[hex(v) for v, _ in lay['noload']]} but this bitstream's LMB ends "
            f"at 0x{size:08X}, so the mailbox belongs at 0x{mailbox:08X} (or "
            f"0x{size - 0x80:08X} for a pre-v8 image). The firmware was built "
            f"for a different LMB_KB -- reads will ALIAS to a plausible-looking "
            f"wrong address. Rebuild with LMB_KB={kb}.")
    else:
        mailbox = size - 0x100
        skip("mmi: diag mailbox anchored", "no NOLOAD segment in the ELF")

    # Only INITIALIZED bytes can clobber the mailbox; a NOLOAD segment there is
    # the mailbox itself.
    if lay["load_top"] <= mailbox:
        ok("mmi: mailbox not overwritten",
           f"last initialized byte 0x{lay['load_top']:08X}, mailbox at "
           f"0x{mailbox:08X}, {mailbox - lay['load_top']:,} bytes clear")
    else:
        bad("mmi: mailbox not overwritten",
            f"initialized data reaches 0x{lay['load_top']:08X}, past the mailbox "
            f"at 0x{mailbox:08X}")


# ---------------------------------------------------------------------------
# ELF
# ---------------------------------------------------------------------------
def elf_tool(vitis: str, name: str) -> str | None:
    cand = Path(vitis) / "gnu/microblaze/lin/bin" / name
    if cand.is_file():
        return str(cand)
    found = shutil.which(name)
    return found


def run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=False).stdout


def elf_layout(elf: Path) -> dict | None:
    """Read the ELF program headers directly -- no toolchain needed.

    Returns three numbers that mean different things and must not be confused:

      load_top : max(p_vaddr + p_filesz) over PT_LOADs that CARRY BYTES. This is
                 what updatemem actually embeds into BRAM INIT strings.
      mem_top  : max(p_vaddr + p_memsz) over all PT_LOADs. This is what the image
                 occupies at run time (.bss/.heap/.stack included) and is the
                 number that must fit the LMB.
      noload   : the vaddr/size of each zero-filesize PT_LOAD. lscript.ld.in pins
                 `.mps3_diag` (the diagnostic mailbox) at the top 0x80 bytes of
                 the LMB as (NOLOAD), so a segment there is EXPECTED and is NOT an
                 overrun -- conflating the two is how an earlier version of this
                 script failed a perfectly good image.
    """
    d = elf.open("rb").read()
    if d[:4] != b"\x7fELF" or d[4] != 1:
        return None
    little = d[5] == 1
    e = "<" if little else ">"
    e_phoff, = struct.unpack_from(e + "I", d, 0x1C)
    e_phentsize, = struct.unpack_from(e + "H", d, 0x2A)
    e_phnum, = struct.unpack_from(e + "H", d, 0x2C)
    load_top = mem_top = 0
    noload: list[tuple[int, int]] = []
    for i in range(e_phnum):
        o = e_phoff + i * e_phentsize
        p_type, = struct.unpack_from(e + "I", d, o)
        if p_type != 1:          # PT_LOAD
            continue
        p_vaddr, = struct.unpack_from(e + "I", d, o + 0x08)
        p_filesz, = struct.unpack_from(e + "I", d, o + 0x10)
        p_memsz, = struct.unpack_from(e + "I", d, o + 0x14)
        mem_top = max(mem_top, p_vaddr + p_memsz)
        if p_filesz:
            load_top = max(load_top, p_vaddr + p_filesz)
        else:
            noload.append((p_vaddr, p_memsz))
    if not mem_top:
        return None
    return {"load_top": load_top, "mem_top": mem_top, "noload": noload}


def check_elf(elf: Path, vitis: str, expect_static_id: str | None,
              expect_xvc: str | None, expect_hwicap: str | None) -> None:
    nm = elf_tool(vitis, "mb-nm")
    od = elf_tool(vitis, "mb-objdump")

    lay = elf_layout(elf)
    if lay:
        ok("elf: image extent",
           f"initialized to 0x{lay['load_top']:08X} ({lay['load_top']:,} B "
           f"embedded by updatemem), runtime top 0x{lay['mem_top']:08X}")
    else:
        bad("elf: image extent", "could not read the ELF program headers")

    # --- baked shell identity. This is the one that is silent on silicon. ------
    # greybox_blob.c defines it as a const uint32_t, so read it out of the ELF's
    # .rodata/.data by symbol rather than trusting the build log.
    if not nm or not od:
        skip("elf: baked static_id",
             "mb-nm/mb-objdump not found -- load Vitis 2024.1 (or pass --vitis) "
             "to run the identity, XVC-target and HWICAP checks. NOT verified.")
        return

    syms = run([nm, str(elf)])
    sym = {}
    for line in syms.splitlines():
        parts = line.split()
        if len(parts) == 3:
            sym[parts[2]] = int(parts[0], 16)

    baked = None
    if STATIC_ID_SYM in sym:
        addr = sym[STATIC_ID_SYM]
        # dump the 4 bytes at that address out of the loaded image
        dis = run([od, "-s", "-j", ".rodata", "--start-address", hex(addr),
                   "--stop-address", hex(addr + 4), str(elf)])
        m = re.search(r"^\s*[0-9a-f]+\s+([0-9a-f]{8})", dis, re.M)
        if not m:
            dis = run([od, "-s", "-j", ".data", "--start-address", hex(addr),
                       "--stop-address", hex(addr + 4), str(elf)])
            m = re.search(r"^\s*[0-9a-f]+\s+([0-9a-f]{8})", dis, re.M)
        if m:
            # objdump -s prints bytes in address order; the target is little-endian
            raw = bytes.fromhex(m.group(1))
            baked = int.from_bytes(raw, "little")
    if baked is None:
        skip("elf: baked static_id",
             f"{STATIC_ID_SYM} not readable from the ELF")
    else:
        got = f"0x{baked:08X}"
        note("elf: baked static_id", got)
        if expect_static_id:
            want = "0x%08X" % int(expect_static_id, 16)
            if got == want:
                ok("elf: static_id matches the bitstream",
                   f"{got} -- `ping.shell_id` will match the fabric, so "
                   "config_agent will accept overlays keyed to it")
            else:
                bad("elf: static_id matches the bitstream",
                    f"baked {got} != expected {want}. THIS IS THE SILENT ONE: "
                    "the shell will ping, answer, report a plausible id, and "
                    "reject every overlay. Rebuild with BLOB_MANIFEST pointing "
                    "at the manifest for THIS bitstream.")

    # --- XVC shift target, read from machine code, not from the build log ------
    # xvc_server.c compiles to accesses at the BIT-BANG page (0x44A7_0000) for a
    # bit-bang target and at MPS3_DBGBR_BASE (0x44A8_0000) for the default.
    # MicroBlaze materialises a 32-bit address with `imm <high16>`;
    # 0x44A7 = 17575, 0x44A8 = 17576. jtag_server.c and swd_server.c also drive
    # 0x44A7, so 0x44A7 alone proves nothing -- the DISCRIMINATOR is that
    # nothing else in the image touches 0x44A8, so DBGBR sites must be 0.
    #
    # WHAT THIS CANNOT SEE, stated rather than glossed. `jtagbb` and `swdbb` are
    # the SAME PAGE: platform_regs.h:109 and :111 both give 0x44A70000, because
    # jtag_bb.sv took over the slot swd_bb.sv vacated at the A6 cutover and
    # xvc_server.h asserts the bit positions agree at compile time. So the two
    # bit-bang targets are INDISTINGUISHABLE in the emitted code and this check
    # verifies the bit-bang/Debug-Bridge choice only. It used to claim a `swdbb`
    # pass proved "-DMPS3_XVC_TARGET_SWDBB is in the emitted code"; it did not,
    # and could not. Saying so is the point -- an acceptance gate that overstates
    # what it proved is worse than one that admits a gap.
    dis = run([od, "-d", str(elf)])
    n_bb    = len(re.findall(r"\bimm\s+%d\b" % XVC_BB_IMM, dis))
    n_dbgbr = len(re.findall(r"\bimm\s+%d\b" % XVC_DBGBR_IMM, dis))
    note("elf: MMIO target sites",
         f"0x44A7 bit-bang (JTAGBB/SWDBB, one page) = {n_bb}, "
         f"0x44A8 DBGBR = {n_dbgbr}")
    if expect_xvc in XVC_BITBANG_TARGETS:
        other = "swdbb" if expect_xvc == "jtagbb" else "jtagbb"
        if n_dbgbr == 0 and n_bb >= 3:
            ok(f"elf: XVC target = {expect_xvc.upper()}",
               f"{n_bb} bit-bang sites and ZERO Debug-Bridge sites -- a "
               f"bit-bang target is in the emitted code. NOT DISTINGUISHED from "
               f"'{other}': both name 0x44A7_0000 and xvc_server.h pins their "
               f"bit positions equal, so the two are the same machine code.")
        else:
            bad(f"elf: XVC target = {expect_xvc.upper()}",
                f"expected 0 DBGBR sites, found {n_dbgbr} (bit-bang {n_bb}). "
                f"XVC_TARGET={expect_xvc} did NOT take -- stale objects? "
                "`make clean` first: make keys off timestamps, not flags.")
    elif expect_xvc == "dbgbr":
        if n_dbgbr >= 4:
            ok("elf: XVC target = DBGBR", f"{n_dbgbr} Debug-Bridge sites")
        else:
            bad("elf: XVC target = DBGBR",
                f"only {n_dbgbr} Debug-Bridge sites found")

    # --- HWICAP write protocol. THE d28c292 TRAP. -----------------------------
    # swap_fsm.c compiles hwicap_lite_write() OR hwicap_fifo_drain()+
    # hwicap_fifo_write_words(), never both. The BD decides which is correct:
    # fpga/shell/bd/shell_bd.tcl's axi_hwicap C_MODE {0} == FIFO.
    has_lite = any(s in sym for s in HWICAP_LITE_SYMS)
    has_fifo = any(s in sym for s in HWICAP_FIFO_SYMS)
    mode = "fifo" if has_fifo and not has_lite else \
           "lite" if has_lite and not has_fifo else "AMBIGUOUS"
    note("elf: HWICAP writer", mode)
    if expect_hwicap:
        if mode == expect_hwicap:
            ok(f"elf: HWICAP = {expect_hwicap}",
               "matches the BD's axi_hwicap configuration; over-the-wire "
               "reconfiguration can work")
        else:
            bad(f"elf: HWICAP = {expect_hwicap}",
                f"ELF carries '{mode}'. A LITE firmware on a C_MODE{{0}} FIFO "
                "bitstream pings, reports the right static_id, passes the "
                "acceptance gate, and CANNOT LOAD ANY RM (commit d28c292). "
                "Rebuild with HWICAP_FIFO=1.")

    # Load-bearing symbols. NOT coordinator_main_loop: main.c runs its own
    # superloop, so --gc-sections legitimately strips that entry point and
    # probing for it produces a permanent, meaningless SKIP.
    #
    # jtag_server_init, NOT swd_server_init: coordinator.c:98 calls
    # jtag_server_init() and its own comment marks swd_server_init dormant, so
    # --gc-sections is entitled to strip the SWD one -- which would have turned
    # that probe into exactly the permanent meaningless SKIP the paragraph above
    # forbids.
    for want in LOADBEARING_SYMS:
        if want in sym:
            ok(f"elf: {want} linked", f"0x{sym[want]:08X}")
        else:
            # coordinator_poll may legitimately be named differently; report, do
            # not fail, on anything not load-bearing for the flags above.
            if want.startswith("clcd"):
                bad(f"elf: {want} linked",
                    "absent -- CLCD=1 did not take (--gc-sections strips an "
                    "unreferenced driver back out; `make clean` first)")
            else:
                skip(f"elf: {want} linked", "symbol not found")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bit", required=True, type=Path, help="the updatemem OUTPUT")
    ap.add_argument("--base-bit", type=Path, help="the bitstream updatemem consumed")
    ap.add_argument("--elf", type=Path, help="the ELF that was baked in")
    ap.add_argument("--meminfo", type=Path, help="the .mmi updatemem consumed")
    ap.add_argument("--expect-static-id", help="e.g. 0x0EE58A4D")
    ap.add_argument("--expect-userid", help="e.g. 0x86A8274E")
    ap.add_argument("--expect-xvc-target", choices=XVC_TARGETS,
                    help="the XVC_TARGET the firmware was built with; must be "
                         "one of firmware/platform/Makefile:301-308's values")
    ap.add_argument("--expect-hwicap", choices=("fifo", "lite"))
    ap.add_argument("--expect-proc", default="u_shell/shell_bd_i/microblaze_0")
    ap.add_argument("--vitis", default=os.environ.get("VITIS",
                                                      "/apps/Xilinx/Vitis/2024.1"))
    args = ap.parse_args()

    for p in (args.bit, args.base_bit, args.elf, args.meminfo):
        if p is not None and not p.is_file():
            print(f"verify_shell_image: not a file: {p}", file=sys.stderr)
            return 2

    print("=" * 76)
    print("verify_shell_image.py -- static acceptance gate (no board, no Vivado)")
    print(f"  image : {args.bit}")
    print("=" * 76)

    h = check_bit(args.bit, args.base_bit, "bit")
    if args.expect_userid and h.get("userid"):
        want = "0x%08X" % int(args.expect_userid, 16)
        if h["userid"] == want:
            ok("bit: UserID as expected", want)
        else:
            bad("bit: UserID as expected", f"{h['userid']} != {want}")

    lay = elf_layout(args.elf) if args.elf else None
    if args.meminfo:
        check_meminfo(args.meminfo, args.expect_proc, lay)
    else:
        skip("mmi checks", "--meminfo not given -- the wrong-MMI trap is NOT checked")

    if args.elf:
        check_elf(args.elf, args.vitis, args.expect_static_id,
                  args.expect_xvc_target, args.expect_hwicap)
    else:
        skip("elf checks", "--elf not given -- identity/XVC/HWICAP NOT checked")

    print()
    width = max(len(n) for _, n, _ in results)
    for state, name, detail in results:
        print(f"  [{state}] {name.ljust(width)}  {detail}")

    nfail = sum(1 for s, _, _ in results if s == "FAIL")
    nskip = sum(1 for s, _, _ in results if s == "SKIP")
    npass = sum(1 for s, _, _ in results if s == "PASS")
    print()
    print(f"  {npass} passed, {nfail} FAILED, {nskip} skipped")
    if nfail:
        print("  VERDICT: DO NOT LOAD THIS IMAGE.")
        return 1
    if nskip:
        print("  VERDICT: no failures, but SKIPPED checks are not passes -- read "
              "them before loading.")
        return 0
    print("  VERDICT: every static check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
