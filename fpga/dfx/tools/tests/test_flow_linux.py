"""fpga/dfx/tools/tests/test_flow_linux.py -- the FLOW lane's gates, each with its
negative control (LINUX_HARNESS_PLAN_2026-09-23.md §4, lane FLOW).

    1. ONE VIVADO PER BUILD DIR, and SHELL_CPU=mbv => 2026.1
       (tools/vivado_guard.py, tools/vivado_version.tcl, the Makefile's parse-time
       guards).
    2. THE DEFAULT PATH IS UNMOVED: unset SHELL_CPU/VIVADO plans exactly what
       SHELL_CPU=mb plans, and neither mentions anything mbv.
    3. THE MBV MINT PLAN: stage 6 is the stage0 bake, under 2026.1, with the CPU
       knob reaching the shell build, build_dfx.tcl and static_canon.
    4. mint-stage0's guards (tools/stage0_bake.py): the stage0 ELF fits the LMB
       map and was compiled FOR this static; the -proc is the MBV, never the DDR
       MIG's calibration MCS; the baked base is the same implementation.
    5. mint-linux-image + the gate (tools/linux_bundle.py,
       scripts/harness_gates/check_image_overlay_match.py --linux-record): image
       <-> static <-> overlays name ONE static. A July 0x2B082E1B slot image
       against a new static is refused.

Everything is synthetic and board-free: fake checkpoints (zip + dcp.xml), fake
.bit headers, ELF32 images written byte by byte, S0LB boot tables. `make -n`
only; no Vivado. Stdlib + pytest (+ tclsh / make when present).

Lives beside the tools it tests (fpga/dfx/tools/ is FLOW's write scope); the
lead may `git mv` it into tests/dfx_flow/ -- it finds the repo either way.
"""

import importlib.util
import json
import os
import pathlib
import shutil
import struct
import subprocess
import sys
import zipfile
import zlib

import pytest


def _find_repo(start):
    p = pathlib.Path(start).resolve()
    for cand in [p] + list(p.parents):
        if (cand / "fpga" / "dfx" / "Makefile").is_file():
            return cand
    raise RuntimeError("repo root not found above %s" % start)


REPO = _find_repo(__file__)
DFX = REPO / "fpga" / "dfx"
TOOLS = DFX / "tools"
GATE_PATH = REPO / "scripts" / "harness_gates" / "check_image_overlay_match.py"
MAKE = shutil.which("make")
TCLSH = shutil.which("tclsh")

JULY_STATIC = "0x2B082E1B"     # the July 2026 MBV Linux static
JULY_USERCODE = "0x5263642C"
USERCODE = "0xABCD1234"
VER32 = 0x01000000


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_tool(tool, *args):
    return subprocess.run([sys.executable, str(TOOLS / tool)] + [str(a) for a in args],
                          capture_output=True, text=True)


# ---------------------------------------------------------------------------
# fixtures: checkpoints, bitstreams, ELFs, S0LB images
# ---------------------------------------------------------------------------
def fake_dcp(path, version):
    path.parent.mkdir(parents=True, exist_ok=True)
    xml = ('<?xml version="1.0"?>\n<Checkpoint Version="26" Minor="0">\n'
           '\t<PRODUCT Name="Vivado v%s (64-bit)"/>\n</Checkpoint>\n' % version)
    with zipfile.ZipFile(str(path), "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("dcp.xml", xml)
        z.writestr("top.edf", "netlist")


T1_WR = (1 << 29) | (2 << 27)
SYNC = b"\xff" * 32 + b"\x00\x00\x00\xbb\x11\x22\x00\x44" + b"\xff" * 8 + b"\xaa\x99\x55\x66"


def slr_stream(usr_access, fill, frames=b""):
    """One SLR's configuration stream, the shape Vivado writes: sync, the AXSS
    write (usr_access None = unstamped), an FDRI Type-1 + Type-2 with the frame
    data, DESYNC. `frames` is appended to the frame data verbatim."""
    body = SYNC + struct.pack(">I", 0x20000000)
    if usr_access is not None:
        body += struct.pack(">II", T1_WR | (0x0D << 13) | 1, usr_access)
    data = fill * 4096 + frames
    data += b"\x00" * (-len(data) % 4)
    body += struct.pack(">II", T1_WR | (0x02 << 13), (2 << 29) | (len(data) // 4)) + data
    return body + struct.pack(">II", T1_WR | (0x04 << 13) | 1, 0x0D) + struct.pack(">I", 0x20000000) * 4


def fake_bit(path, userid, usr_access=VER32, fill=b"\x00", slr1="same", frames=b""):
    """A 2-SLR full .bit like the KU115's: SLR0's stream, whose last packet is a
    Type-1 WRITE to 0x1E + a Type-2 carrying SLR1's whole stream. slr1 = SLR1's
    USR_ACCESS: "same" (what Vivado writes), a different word, or None (none)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    head = (b"\x00\x09\x0f\xf0\x0f\xf0\x0f\xf0\x0f\xf0\x00\x00\x01a\x00\x30"
            b"config_rm_greybox;UserID=0X" + userid[2:].encode() + b";Version=2026.1\x00")
    inner = slr_stream(usr_access if slr1 == "same" else slr1, fill)
    inner += b"\x00" * (-len(inner) % 4)
    outer = slr_stream(usr_access, fill, frames)
    outer += SYNC + struct.pack(">II", T1_WR | (0x1E << 13), (2 << 29) | (len(inner) // 4)) + inner
    path.write_bytes(head + outer + struct.pack(">I", 0x20000000) * 2)


def fake_elf(path, machine=243, entry=0, image_top=0x1000, syms=None, values=None):
    """A minimal ELF32 LE: one PT_LOAD at paddr 0 of `image_top` bytes, a symtab.
    values = {addr: u32} poked into the image; syms = {name: addr}."""
    image = bytearray(image_top)
    for addr, val in (values or {}).items():
        struct.pack_into("<I", image, addr, val)
    syms = syms or {}
    strtab = b"\0"
    names = {}
    for n in syms:
        names[n] = len(strtab)
        strtab += n.encode() + b"\0"
    symtab = b"\0" * 16
    for n, v in syms.items():
        symtab += struct.pack("<IIIBBH", names[n], v, 4, 0x11, 0, 1)
    ph_off, img_off = 52, 0x100
    sym_off = img_off + len(image)
    str_off = sym_off + len(symtab)
    sh_off = str_off + len(strtab)
    ehdr = (b"\x7fELF\x01\x01\x01" + b"\0" * 9
            + struct.pack("<HHIIIIIHHHHHH", 2, machine, 1, entry, ph_off, sh_off, 0,
                          52, 32, 1, 40, 3, 0))
    phdr = struct.pack("<IIIIIIII", 1, img_off, 0, 0, len(image), len(image), 7, 4)
    shdrs = (b"\0" * 40
             + struct.pack("<IIIIIIIIII", 0, 2, 0, 0, sym_off, len(symtab), 2, 1, 4, 16)
             + struct.pack("<IIIIIIIIII", 0, 3, 0, 0, str_off, len(strtab), 0, 0, 1, 0))
    blob = bytearray(ehdr + phdr)
    blob += b"\0" * (img_off - len(blob))
    blob += image + symtab + strtab + shdrs
    path.write_bytes(bytes(blob))


def stage0_elf(path, static_id, ver32=VER32, stack=0x1FE00):
    fake_elf(path, syms={"mps3_stage0_static_id": 0x100, "mps3_stage0_ver32": 0x104,
                         "_estack": stack},
             values={0x100: int(static_id, 16), 0x104: ver32})


STAGE0_PACK = REPO / "src" / "linux_soc" / "hw" / "fw_stage0" / "stage0_pack.py"


def s0lb(path, regions, pc=0x80000000, corrupt=False):
    """A REAL boot image, packed by STAGE0's own stage0_pack.py (the one writer
    of the format -- v2 today). corrupt=True flips the last payload byte."""
    args = []
    for i, (dst, data) in enumerate(regions):
        part = path.parent / ("%s.r%d" % (path.name, i))
        part.write_bytes(data)
        args.append("%s@0x%08X" % (part, dst))
    r = subprocess.run([sys.executable, str(STAGE0_PACK), "--out", str(path),
                        "--pc", "0x%08X" % pc, "--a1", "0"] + args,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    if corrupt:
        img = bytearray(path.read_bytes())
        img[-1] ^= 0xFF
        path.write_bytes(bytes(img))


def s0lb_v1(path, regions, pc=0x80000000):
    """The July/v1 layout (header CRC over the header only) -- refused since
    STAGE0_CONTRACT v1.2 §5. Written by hand: nothing in the tree writes it any more."""
    hdr0 = struct.pack("<8I", 0x424C3053, 1, len(regions), pc, 0, 0, 0, 0)
    hcrc = zlib.crc32(hdr0) & 0xFFFFFFFF
    cur = 0x1000
    entries, blobs = [], []
    for dst, data in regions:
        entries.append(struct.pack("<4I", cur, dst, len(data), zlib.crc32(data) & 0xFFFFFFFF))
        blobs.append((cur, data))
        cur = (cur + len(data) + 0xFFF) & ~0xFFF
    img = bytearray(struct.pack("<8I", 0x424C3053, 1, len(regions), pc, 0, 0, 0, hcrc))
    for e in entries:
        img += e
    for off, data in blobs:
        img += b"\xff" * (off - len(img))
        img += data
    path.write_bytes(bytes(img))


MMI_TWO_PROCS = """<MemInfo Version="1" Minor="9">
  <Processor Endianness="Little" InstPath="u_shell/shell_bd_i/microblaze_riscv_0">
    <AddressSpace Name="a" Begin="0" End="131071">
    </AddressSpace>
  </Processor>
  <Processor Endianness="Little" InstPath="u_shell/shell_bd_i/ddr4_0/inst/u_ddr4_mem_intfc/u_ddr_cal_riu/mcs0/inst/microblaze_I">
    <AddressSpace Name="b" Begin="0" End="65535">
    </AddressSpace>
  </Processor>
</MemInfo>
"""
MMI_MB_ONLY = """<MemInfo Version="1" Minor="9">
  <Processor Endianness="Little" InstPath="u_shell/shell_bd_i/microblaze_0">
    <AddressSpace Name="a" Begin="0" End="1048575">
    </AddressSpace>
  </Processor>
</MemInfo>
"""


# ---------------------------------------------------------------------------
# 1. one Vivado per build dir; mbv => 2026.1
# ---------------------------------------------------------------------------
def test_dcp_version_reads_the_writer(tmp_path):
    g = _load("vivado_guard", TOOLS / "vivado_guard.py")
    fake_dcp(tmp_path / "a.dcp", "2026.1")
    fake_dcp(tmp_path / "b.dcp", "2024.1")
    (tmp_path / "c.dcp").write_text("not a checkpoint")
    assert g.dcp_version(str(tmp_path / "a.dcp")) == "2026.1"
    assert g.dcp_version(str(tmp_path / "b.dcp")) == "2024.1"
    assert g.dcp_version(str(tmp_path / "c.dcp")) is None


def test_mix_guard_passes_one_version_and_refuses_two(tmp_path):
    b = tmp_path / "build"
    fake_dcp(b / "prod" / "rm_led_synth.dcp", "2024.1")
    fake_dcp(b / "rm_nanosoc_synth" / "rm_nanosoc_synth.dcp", "2024.1")
    (b / "prod" / "junk.dcp").write_text("x")          # unreadable: skipped
    ok = run_tool("vivado_guard.py", "mix", "--want", "2024.1", "--build", b)
    assert ok.returncode == 0 and ok.stdout == "", ok.stdout
    # NEGATIVE CONTROL: the July trap -- a 2024.1 checkpoint in a 2026.1 run
    bad = run_tool("vivado_guard.py", "mix", "--want", "2026.1", "--build", b)
    assert bad.returncode == 1 and "rm_led_synth.dcp was written by Vivado 2024.1" in bad.stdout
    # and a single reused checkpoint named on its own
    fake_dcp(tmp_path / "reuse.dcp", "2026.1")
    bad2 = run_tool("vivado_guard.py", "mix", "--want", "2024.1", "--dcp", tmp_path / "reuse.dcp")
    assert bad2.returncode == 1


@pytest.mark.parametrize("cpu,ver,rc", [("mbv", "2024.1", 1), ("mbv", "2025.2", 1),
                                        ("mbv", "2026.1", 0), ("mb", "2024.1", 0),
                                        ("", "2024.1", 0), ("riscv", "2026.1", 1)])
def test_cpu_guard(cpu, ver, rc):
    assert run_tool("vivado_guard.py", "cpu", "--shell-cpu", cpu, "--vivado-ver", ver).returncode == rc


@pytest.mark.skipif(not TCLSH, reason="tclsh not installed")
def test_tcl_twin_refuses_a_foreign_checkpoint(tmp_path):
    fake_dcp(tmp_path / "old.dcp", "2024.1")
    fake_dcp(tmp_path / "new.dcp", "2026.1")
    script = """
source {tools}/vivado_version.tcl
set ::DFX_VIVADO_SHORT_OVERRIDE 2026.1
puts [dcp_product_version {old}]
dfx_version_guard {new} "x"
if {{[catch {{dfx_version_guard {old} "x"}} e]}} {{ puts REFUSED }} else {{ puts ACCEPTED }}
set ::env(DFX_SHELL_CPU) mbv
set ::DFX_VIVADO_SHORT_OVERRIDE 2024.1
if {{[catch {{dfx_cpu_version_guard}} e]}} {{ puts CPU_REFUSED }} else {{ puts CPU_ACCEPTED }}
""".format(tools=TOOLS, old=tmp_path / "old.dcp", new=tmp_path / "new.dcp")
    out = subprocess.run([TCLSH], input=script, capture_output=True, text=True).stdout
    assert "2024.1" in out and "REFUSED" in out and "CPU_REFUSED" in out, out
    assert "ACCEPTED" not in out.replace("CPU_", ""), out


# ---------------------------------------------------------------------------
# 2 + 3. the Makefile: default unmoved, mbv plan, refusals
# ---------------------------------------------------------------------------
def make_n(*goals, **kw):
    env = dict(os.environ)
    for v in ("MINT_HUB", "SHELL_CPU", "VIVADO", "VIVADO_VER", "MINT_RMS"):
        env.pop(v, None)
    args = ["%s=%s" % (k, v) for k, v in sorted(kw.items())]
    return subprocess.run([MAKE, "-n", "--no-print-directory", "-C", str(DFX)] + list(goals) + args,
                          capture_output=True, text=True, env=env)


def _plan(tmp_path, **kw):
    base = dict(BUILD=tmp_path / "b", SHELL_PROJ=tmp_path / "s", OVERLAY_ROOT=tmp_path / "o",
                FW_WS=tmp_path / "w", MINT_HUB="", SHELL_TOUCH=1, TOUCH=1)
    base.update(kw)
    return make_n("mint", **base)


needs_make = pytest.mark.skipif(not MAKE, reason="make not installed")


@needs_make
def test_default_equals_explicit_mb_and_mentions_no_mbv(tmp_path):
    unset = _plan(tmp_path)
    mb = _plan(tmp_path, SHELL_CPU="mb")
    assert unset.returncode == 0 and mb.returncode == 0, unset.stderr + mb.stderr
    assert unset.stdout == mb.stdout, "SHELL_CPU=mb must plan exactly what unset plans"
    for needle in ("SHELL_CPU=mbv", "DFX_SHELL_CPU", "stage0_bake", "config_rm_greybox_stage0",
                   "2026.1", "--flag SHELL_CPU"):
        assert needle not in unset.stdout, "default plan mentions %r" % needle
    assert "/apps/Xilinx/Vivado/2024.1/bin/vivado" in unset.stdout
    assert "config_rm_greybox_fw.bit" in unset.stdout


@needs_make
def test_mbv_plan(tmp_path):
    p = _plan(tmp_path, SHELL_CPU="mbv", VIVADO="2026.1",
              MINT_RMS="greybox led dbg_demo nanosoc")
    assert p.returncode == 0, p.stderr
    out = p.stdout
    stages = ["[1/8] preflight", "[2/8] static shell", "[4/8] DFX prod", "[5/8] overlays",
              "[6/8] stage0", "[7/8] mint record", "[8/8] hub copy"]
    pos = -1
    for st in stages:
        i = out.find(st)
        assert i > pos, "stage %r missing or out of order" % st
        pos = i
    assert "SHELL_CPU=mbv /research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/vivado" in out
    assert "DFX_SHELL_CPU=mbv /research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/vivado" in out
    assert "--flag SHELL_CPU=mbv" in out and "--vivado 2026.1" in out
    assert "rm_greybox rm_led rm_dbg_demo rm_nanosoc" in out
    assert "config_rm_greybox_stage0.bit" in out
    assert "/research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/updatemem" in out
    for absent in ("shell_fw.elf", "verify_shell_image.py", "firmware/platform"):
        assert absent not in out, "mbv plan still runs the MicroBlaze firmware bake (%s)" % absent


@needs_make
@pytest.mark.parametrize("kw,needle", [
    (dict(SHELL_CPU="mbv", VIVADO="2024.1"), "needs Vivado >= 2026.1"),
    (dict(SHELL_CPU="mbv", VIVADO="/apps/Xilinx/Vivado/2024.1/bin/vivado"), "needs Vivado >= 2026.1"),
    (dict(VIVADO="/research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/vivado", VIVADO_VER="2024.1"),
     "is a 2026.1 install but VIVADO_VER=2024.1"),
    (dict(SHELL_CPU="riscv"), "want mb"),
    (dict(MINT_RMS="greybox led", RM_SET="greybox"), "disagree"),
])
def test_makefile_refuses(tmp_path, kw, needle):
    p = _plan(tmp_path, **kw)
    assert p.returncode != 0 and needle in (p.stdout + p.stderr), p.stdout + p.stderr


@needs_make
def test_mbv_refuses_the_tracked_bare_metal_overlay_set(tmp_path):
    p = _plan(tmp_path, SHELL_CPU="mbv", OVERLAY_ROOT=DFX / "overlay")
    assert p.returncode != 0 and "tracked bare-metal" in p.stderr


@needs_make
def test_makefile_refuses_a_version_mix_before_any_stage(tmp_path):
    fake_dcp(tmp_path / "b" / "prod" / "rm_led_synth.dcp", "2024.1")
    ok = _plan(tmp_path)                                         # 2024.1 on 2024.1
    assert ok.returncode == 0, ok.stderr
    bad = _plan(tmp_path, SHELL_CPU="mbv")                       # 2026.1 on 2024.1
    assert bad.returncode != 0 and "VIVADO VERSION MIX" in bad.stderr
    fake_dcp(tmp_path / "s2" / "shell_static_synth.dcp", "2026.1")
    bad2 = _plan(tmp_path, SHELL_PROJ=tmp_path / "s2")          # a 2026.1 shell in 2024.1
    assert bad2.returncode != 0 and "shell_static_synth.dcp was written by Vivado 2026.1" in bad2.stderr


@needs_make
def test_mbv_dryrun_hides_every_tool_behind_the_guard(tmp_path):
    p = _plan(tmp_path, SHELL_CPU="mbv", DRYRUN=1)
    assert p.returncode == 0, p.stderr
    lines, pend = [], ""
    for line in p.stdout.splitlines():
        if line.endswith("\\"):
            pend += line[:-1] + " "
            continue
        lines.append(pend + line)
        pend = ""
    for ln in lines:
        s = ln.strip().lstrip("@")
        if s.startswith(("#", "echo ", "mkdir ", "test ")):
            continue
        if "vivado" in s.lower() or "updatemem" in s.lower() or "stage0_bake.py" in s:
            assert "DRYRUN" in s, "tool invocation outside the DRYRUN guard:\n%s" % ln


# ---------------------------------------------------------------------------
# 4. mint-stage0 guards
# ---------------------------------------------------------------------------
def test_stage0_elf_identity_is_checked(tmp_path):
    stage0_elf(tmp_path / "good.elf", "0x3F1A560F")
    stage0_elf(tmp_path / "other.elf", JULY_STATIC)
    fake_elf(tmp_path / "noid.elf")
    exp = ["--expect-static-id", "0x3F1A560F", "--expect-ver32", "0x01000000"]
    assert run_tool("stage0_bake.py", "elf", "--elf", tmp_path / "good.elf", *exp).returncode == 0
    # NEGATIVE CONTROLS: built for another static; built without the constants
    r = run_tool("stage0_bake.py", "elf", "--elf", tmp_path / "other.elf", *exp)
    assert r.returncode == 1 and "built for ANOTHER static" in r.stderr
    r = run_tool("stage0_bake.py", "elf", "--elf", tmp_path / "noid.elf", *exp)
    assert r.returncode == 1 and "no symbol mps3_stage0_static_id" in r.stderr
    r = run_tool("stage0_bake.py", "elf", "--elf", tmp_path / "good.elf",
                 "--expect-static-id", "0x3F1A560F", "--expect-ver32", "0x02000000")
    assert r.returncode == 1


def test_stage0_elf_lmb_map(tmp_path):
    stage0_elf(tmp_path / "stack.elf", "0x1", stack=0x20000)       # July's _estack
    assert run_tool("stage0_bake.py", "elf", "--elf", tmp_path / "stack.elf").returncode == 1
    fake_elf(tmp_path / "big.elf", image_top=0x1FF00)              # over the status block
    assert run_tool("stage0_bake.py", "elf", "--elf", tmp_path / "big.elf").returncode == 1
    fake_elf(tmp_path / "mb.elf", machine=189)                     # a classic MicroBlaze ELF
    assert run_tool("stage0_bake.py", "elf", "--elf", tmp_path / "mb.elf").returncode == 1
    fake_elf(tmp_path / "entry.elf", entry=0x24)
    assert run_tool("stage0_bake.py", "elf", "--elf", tmp_path / "entry.elf").returncode == 1


def test_proc_is_the_mbv_never_the_mig_mcs(tmp_path):
    (tmp_path / "mbv.mmi").write_text(MMI_TWO_PROCS)
    (tmp_path / "mb.mmi").write_text(MMI_MB_ONLY)
    r = run_tool("stage0_bake.py", "proc", "--mmi", tmp_path / "mbv.mmi")
    assert r.returncode == 0 and r.stdout.strip() == "u_shell/shell_bd_i/microblaze_riscv_0"
    assert run_tool("stage0_bake.py", "proc", "--mmi", tmp_path / "mb.mmi").returncode != 0


def _static(prod, userid=USERCODE):
    prod.mkdir(parents=True, exist_ok=True)
    (prod / "static_routed_locked.dcp").write_bytes(b"locked static bytes")
    sid = "0x%08X" % (zlib.crc32(b"locked static bytes") & 0xFFFFFFFF)
    (prod / "static_id.txt").write_text(sid + "\n")
    fake_bit(prod / "config_rm_greybox.bit", userid)
    (prod / "static_stamp.json").write_text(json.dumps(
        {"static_id": sid, "usercode": userid, "usr_access": "0x%08X" % VER32}))
    (prod / GREYBOX_CLEAR).write_bytes(b"greybox clearing of " + sid.encode())
    return sid


GREYBOX_CLEAR = "config_rm_greybox_pblock_rp_dut_partial_clear.bin"


def _clear_sha(prod):
    import hashlib
    return hashlib.sha256((prod / GREYBOX_CLEAR).read_bytes()).hexdigest()


def _bake(prod, sid, baked_fill=b"\x01", elf_sid=None, extra=()):
    stage0_elf(prod / "stage0.elf", elf_sid or sid)
    return run_tool("stage0_bake.py", "bit", "--base", prod / "config_rm_greybox.bit",
                    "--baked", prod / "config_rm_greybox_stage0.bit", "--elf", prod / "stage0.elf",
                    "--proc", "u_shell/shell_bd_i/microblaze_riscv_0",
                    "--static-id-file", prod / "static_id.txt",
                    "--locked-dcp", prod / "static_routed_locked.dcp",
                    "--stamp-json", prod / "static_stamp.json",
                    "--record", prod / "stage0_bake.json", *extra)


def test_bake_identity_guard(tmp_path):
    prod = tmp_path / "prod"
    sid = _static(prod)
    ident = run_tool("stage0_bake.py", "identity", "--bit", prod / "config_rm_greybox.bit",
                     "--static-id-file", prod / "static_id.txt")
    assert ident.stdout.split() == [sid, "0x01000000"]
    fake_bit(prod / "config_rm_greybox_stage0.bit", USERCODE, fill=b"\x01")
    assert _bake(prod, sid).returncode == 0
    rec = json.loads((prod / "stage0_bake.json").read_text())
    assert rec["static_id"] == sid and rec["stage0_identity_checked"] is True
    # NEGATIVE CONTROLS
    fake_bit(prod / "config_rm_greybox_stage0.bit", USERCODE, fill=b"\x01",    # compressed:
             frames=b"\x02" * 1000)                                             # length moves
    assert _bake(prod, sid).returncode == 0, "a compressed bake changes length; that is fine"
    fake_bit(prod / "config_rm_greybox_stage0.bit", USERCODE, fill=b"\x00")      # a no-op
    assert _bake(prod, sid).returncode == 1
    fake_bit(prod / "config_rm_greybox_stage0.bit", "0xDEADBEEF", fill=b"\x01")  # other impl
    assert _bake(prod, sid).returncode == 1
    fake_bit(prod / "config_rm_greybox_stage0.bit", USERCODE, 0x02000000, b"\x01")
    assert _bake(prod, sid).returncode == 1                                      # USR_ACCESS
    fake_bit(prod / "config_rm_greybox_stage0.bit", USERCODE, fill=b"\x01")
    r = _bake(prod, sid, elf_sid=JULY_STATIC)                                    # stage0 for July
    assert r.returncode == 1 and "ANOTHER static" in r.stderr
    (prod / "static_id.txt").write_text("0x12345678\n")                         # id != locked dcp
    assert _bake(prod, "0x12345678").returncode == 1


def test_fieldable_refuses_a_prototype(tmp_path):
    (tmp_path / "p.json").write_text(json.dumps({"mint_kind": "prototype"}))
    (tmp_path / "m.json").write_text(json.dumps({"mint_kind": "mint"}))
    assert run_tool("stage0_bake.py", "fieldable", "--record", tmp_path / "p.json").returncode == 1
    assert run_tool("stage0_bake.py", "fieldable", "--record", tmp_path / "m.json").returncode == 0


# ---------------------------------------------------------------------------
# 5. the Linux bundle + the gate
# ---------------------------------------------------------------------------
MIG_UUID = "35988F4F3335580E8082F08D4B627CD0"
HUB = {"type": "XSDB_V3", "name": "u_shell/shell_bd_i/mig_dbg_hub/inst/xsdbm",
       "spec": "labtools_xsdbm_v3", "clk_input_freq_hz": "100000000"}
MIG = {"type": "XSDBS_V2", "name": "u_shell/shell_bd_i/ddr4_0",
       "spec": "labtools_xsdbslavelib_v2", "ipName": "DDR4_SDRAM", "uuid": MIG_UUID}


def fake_static_debug(prod, cores=None, rpt_cores=None, write_ltx=True):
    """The shape Vivado 2026.1 wrote for the P-mint (0x61BC6789): the full-design
    .ltx of the reference config + its report_debug_core. cores=[] -> a static
    with no debug core (the bare-metal case)."""
    cores = [HUB, MIG] if cores is None else cores
    rpt_cores = cores if rpt_cores is None else rpt_cores
    lines = ["Debug Core Information", "Table of Contents", "-----------------",
             "1. Debug Cores"]
    for i, c in enumerate(rpt_cores, 1):
        lines.append("1.%d %s: (%s, implemented, inserted)" % (i, c["name"].split("/")[-1], c["spec"]))
    lines.append("1. Debug Cores")
    if not rpt_cores:
        lines.append("INFO: [Chipscope 16-244] No debug cores were found in this design.")
    for c in rpt_cores:
        if c.get("uuid"):
            lines += ["UUID of Debug Core", "| UUID                             |", "| %s |" % c["uuid"]]
    (prod / "debug_core_rm_greybox_static.rpt").write_text("\n".join(lines) + "\n")
    if write_ltx and cores:
        doc = {"ltx_root": {"version": 4, "minor": 0, "ltx_data": [
            {"name": "EDA_PROBESET", "active": True, "debug_cores": cores}]}}
        (prod / "config_rm_greybox_static.ltx").write_text(json.dumps(doc))


def static_gate(prod, cpu="mbv", *extra):
    return run_tool("ltx_sidecar.py", "static", "--build-dir", prod, "--shell-cpu", cpu, *extra)


def _linux_tree(tmp_path, provisioned=None, overlay_uc=USERCODE, cpu_flag=True, legal=True,
                corrupt=False, kind="mint"):
    prod = tmp_path / "build" / "prod"
    sid = _static(prod)
    fake_bit(prod / "config_rm_greybox_stage0.bit", USERCODE, fill=b"\x01")
    fake_static_debug(prod)
    assert static_gate(prod).returncode == 0
    assert _bake(prod, sid, extra=("--mint-kind", kind, "--static-ltx-sidecar",
                                   prod / "config_rm_greybox_static.ltx.json")).returncode == 0
    canon = {"schema": "mps3-static-canon", "digest": "ab" * 32,
             "flags": dict({"SHELL_TOUCH": "1", "SHELL_REALPHY": "0"},
                           **({"SHELL_CPU": "mbv"} if cpu_flag else {}))}
    (tmp_path / "static_canon.json").write_text(json.dumps(canon))
    ovl = tmp_path / "overlay"
    for rm in ("greybox", "led"):
        (ovl / rm).mkdir(parents=True)
        (ovl / rm / "manifest.json").write_text(json.dumps(
            {"schema": 1, "static_id": sid, "rm_id": "0x0100001E", "rm_name": rm,
             "partial": {"crc32": "0x1"}, "clearing": {"crc32": "0x2"},
             "static_usercode": overlay_uc}))
    img = tmp_path / "img" / "boot.img"
    img.parent.mkdir()
    s0lb(img, [(0x80000000, b"opensbi+kernel+rootfs" * 100)], corrupt=corrupt)
    (tmp_path / "img" / "boot.img.json").write_text(json.dumps(
        {"static_id": provisioned or sid, "components": {"kernel": "11" * 32},
         "greybox_clear_sha256": _clear_sha(prod), "greybox_clear_static_id": sid}))
    li = tmp_path / "legal-info"
    if legal:
        (li / "licenses").mkdir(parents=True)
        (li / "manifest.csv").write_text("PACKAGE,VERSION,LICENSE\nbusybox,1.36,GPL-2.0\n")
    return prod, sid, img, li


def _pack(tmp_path, prod, img, li, kind="mint"):
    return run_tool("linux_bundle.py", "pack", "--prod", prod,
                    "--static-canon", tmp_path / "static_canon.json",
                    "--blob", img, "--blob-info", str(img) + ".json",
                    "--legal-info", li if li.exists() else "",
                    "--overlay-root", tmp_path / "overlay", "--mint-kind", kind)


def test_bundle_packs_and_the_gate_agrees(tmp_path):
    prod, sid, img, li = _linux_tree(tmp_path)
    r = _pack(tmp_path, prod, img, li)
    assert r.returncode == 0, r.stdout + r.stderr
    b = json.loads((prod / "linux_bundle.json").read_text())
    assert b["static_id"] == sid and b["fieldable"] is True
    assert set(b["targets"]) == {"mcc_sd", "ethernet"}
    assert b["targets"]["mcc_sd"]["flashable_bit"]["name"] == "config_rm_greybox_stage0.bit"
    assert b["targets"]["ethernet"]["legal_info"]["name"] == "linux_legal_info.tar"
    gate = _load("gate", GATE_PATH)
    assert gate.main(["--repo", str(REPO), "--linux-record", str(prod)]) == 0


def test_july_slot_image_against_a_new_static_is_refused(tmp_path):
    """THE named negative control: a slot image provisioned for the July static."""
    prod, sid, img, li = _linux_tree(tmp_path, provisioned=JULY_STATIC)
    r = _pack(tmp_path, prod, img, li)
    assert r.returncode == 1 and "provisioned for static_id 0x2B082E1B" in r.stderr, r.stderr
    assert not (prod / "linux_bundle.json").exists()


def test_gate_catches_what_happens_after_packaging(tmp_path, capsys):
    prod, sid, img, li = _linux_tree(tmp_path)
    assert _pack(tmp_path, prod, img, li).returncode == 0
    gate = _load("gate", GATE_PATH)
    # a July bundle's claims beside the new static
    b = json.loads((prod / "linux_bundle.json").read_text())
    july = dict(b, static_id=JULY_STATIC, static_usercode=JULY_USERCODE)
    july["targets"] = json.loads(json.dumps(b["targets"]))
    july["targets"]["ethernet"]["provisioned"]["static_id"] = JULY_STATIC
    (prod / "linux_bundle.json").write_text(json.dumps(july))
    assert gate.main(["--repo", str(REPO), "--linux-record", str(prod)]) == 1
    (prod / "linux_bundle.json").write_text(json.dumps(b))
    assert gate.main(["--repo", str(REPO), "--linux-record", str(prod)]) == 0
    # the slot image swapped under a valid bundle
    slot = prod / "linux_slot.img"
    data = bytearray(slot.read_bytes()); data[-1] ^= 1
    slot.write_bytes(bytes(data))
    assert gate.main(["--repo", str(REPO), "--linux-record", str(prod)]) == 1
    assert "not the slot image" in capsys.readouterr().err


@pytest.mark.parametrize("kw,needle", [
    (dict(overlay_uc="0x0EE58A4D"), "static_usercode 0x0EE58A4D != UserID"),
    (dict(cpu_flag=False), "no SHELL_CPU=mbv"),
    (dict(legal=False), "no Buildroot legal-info"),
    (dict(corrupt=True), "region 0 payload CRC"),
])
def test_bundle_refusals(tmp_path, kw, needle):
    prod, sid, img, li = _linux_tree(tmp_path, **kw)
    r = _pack(tmp_path, prod, img, li)
    assert r.returncode == 1 and needle in r.stderr, r.stderr


def test_prototype_packs_without_legal_info_but_is_never_fielded(tmp_path):
    prod, sid, img, li = _linux_tree(tmp_path, legal=False, kind="prototype")
    r = _pack(tmp_path, prod, img, li, kind="prototype")
    assert r.returncode == 0, r.stderr
    b = json.loads((prod / "linux_bundle.json").read_text())
    assert b["fieldable"] is False and b["targets"]["ethernet"]["legal_info"] is None
    fake_repo = tmp_path / "repo"
    rec = fake_repo / "fielded" / sid
    shutil.copytree(str(prod), str(rec))
    gate = _load("gate", GATE_PATH)
    assert gate.main(["--repo", str(fake_repo), "--linux-record", str(rec)]) == 1


def test_bring_up_bake_without_identity_is_not_fieldable(tmp_path):
    prod, sid, img, li = _linux_tree(tmp_path)
    assert _bake(prod, sid, extra=("--no-identity", "--static-ltx-sidecar",
                                   prod / "config_rm_greybox_static.ltx.json")).returncode == 0
    r = _pack(tmp_path, prod, img, li)
    assert r.returncode == 1 and "not fieldable" in r.stderr


def test_default_gate_run_is_unchanged_without_linux_records(tmp_path, capsys):
    """No fielded/*/linux_bundle.json -> the bare-metal check alone, as before."""
    gate = _load("gate", GATE_PATH)
    assert gate.fielded_linux_records(str(tmp_path)) == []
    rc = gate.main(["--repo", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0 and "linux record" not in out and "SKIP image/overlay match" in out


# ---------------------------------------------------------------------------
# 5b. IMAGE_CONTRACT.md's real shape: raw FW_PAYLOAD + artifacts/version + SUMS
# ---------------------------------------------------------------------------
def _image_artifacts(tmp_path, sid, kind="release", stage0_sha="unknown", clear_sha=None):
    clear_sha = clear_sha or _clear_sha(tmp_path / "build" / "prod")
    art = tmp_path / "artifacts"
    art.mkdir()
    (art / "fw_payload_1region.bin").write_bytes(b"\x97\x02\x00\x00" + b"opensbi+Image" * 500)
    (art / "version").write_text(
        "format=1\nimpl=linux\nimage_kind=%s\nkernel=6.18.7\nstatic_id=%s\n"
        "stage0_sha256=%s\nharnessd_sha256=placeholder\ngreybox_clear_sha256=%s\n"
        "greybox_clear_static_id=%s\n" % (kind, sid, stage0_sha, clear_sha, sid))
    sums = ""
    for f in ("fw_payload_1region.bin", "version"):
        import hashlib
        sums += "%s  %s\n" % (hashlib.sha256((art / f).read_bytes()).hexdigest(), f)
    (art / "SHA256SUMS").write_text(sums)
    return art


def _pack_art(tmp_path, prod, art, li, kind="mint"):
    return run_tool("linux_bundle.py", "pack", "--prod", prod,
                    "--static-canon", tmp_path / "static_canon.json",
                    "--blob", art / "fw_payload_1region.bin", "--blob-info", art / "version",
                    "--sha256sums", art / "SHA256SUMS", "--legal-info", li,
                    "--overlay-root", tmp_path / "overlay", "--mint-kind", kind)


def test_raw_fw_payload_is_wrapped_by_stage0_pack(tmp_path):
    prod, sid, _img, li = _linux_tree(tmp_path)
    s0sha = json.loads((prod / "stage0_bake.json").read_text())["stage0_elf"]["sha256"]
    art = _image_artifacts(tmp_path, sid, stage0_sha=s0sha)
    r = _pack_art(tmp_path, prod, art, li)
    assert r.returncode == 0, r.stdout + r.stderr
    b = json.loads((prod / "linux_bundle.json").read_text())
    slot = b["targets"]["ethernet"]["slot_image"]
    assert slot["source"]["name"] == "fw_payload_1region.bin"
    assert "stage0_pack.py" in slot["source"]["wrapped_by"]
    assert slot["s0lb"]["num_entries"] == 1 and slot["s0lb"]["entry_pc"] == "0x80000000"
    assert b["targets"]["ethernet"]["components"]["image_kind"] == "release"


def test_unprovisioned_image_is_never_accepted(tmp_path):
    """static_id 0x00000000 = built without MPS3_STATIC_ID. Refused for a mint AND a
    prototype: an image must be provisioned for its own static (lead, 2026-09-24)."""
    for kind in ("mint", "prototype"):
        sub = tmp_path / kind
        sub.mkdir()
        prod, sid, _img, li = _linux_tree(sub, kind=kind)
        art = _image_artifacts(sub, "0x00000000")
        r = _pack_art(sub, prod, art, li, kind=kind)
        assert r.returncode == 1 and "UNPROVISIONED" in r.stderr, (kind, r.stderr)


@pytest.mark.parametrize("mutate,needle", [
    ("lab", "only a `release` image"),
    ("stage0", "stage0_sha256"),
    ("sums", "SHA256SUMS"),
    ("july", "provisioned for static_id 0x2B082E1B"),
])
def test_image_record_refusals(tmp_path, mutate, needle):
    prod, sid, _img, li = _linux_tree(tmp_path)
    art = _image_artifacts(tmp_path, JULY_STATIC if mutate == "july" else sid,
                           kind="lab" if mutate == "lab" else "release",
                           stage0_sha="ab" * 32 if mutate == "stage0" else "unknown")
    if mutate == "sums":
        with open(str(art / "fw_payload_1region.bin"), "ab") as fh:
            fh.write(b"stale")
    r = _pack_art(tmp_path, prod, art, li)
    assert r.returncode == 1 and needle in r.stderr, r.stderr
    assert not (prod / "linux_slot.img.new").exists()


def test_gate_parses_s0lb_with_stage0s_own_parser(tmp_path):
    """v2 (STAGE0_CONTRACT v1.2 §5): the header CRC covers the header AND the
    entry table. A real packed image passes; v1, a flipped entry-table byte, and
    a region outside the DDR window are refused -- by stage0_pack.check_image."""
    gate = _load("gate", GATE_PATH)
    s0lb(tmp_path / "ok.img", [(0x80000000, b"x" * 64), (0x84000000, b"rootfs" * 9)])
    info, probs = gate.s0lb_info(str(tmp_path / "ok.img"))
    assert probs == [] and info["version"] == 2 and info["num_entries"] == 2, probs
    assert info["regions"][1]["dst"] == "0x84000000"
    # NEGATIVE CONTROLS
    s0lb_v1(tmp_path / "v1.img", [(0x80000000, b"x" * 64)])
    info, probs = gate.s0lb_info(str(tmp_path / "v1.img"))
    assert info is not None and any("bad version 1" in p for p in probs), probs
    img = bytearray((tmp_path / "ok.img").read_bytes())
    img[32 + 16 + 8] ^= 0x01                        # entry 1's len: the table CRC must catch it
    (tmp_path / "entry.img").write_bytes(bytes(img))
    assert any("table CRC" in p for p in gate.s0lb_info(str(tmp_path / "entry.img"))[1])
    s0lb(tmp_path / "hi.img", [(0xB0000000, b"x" * 64)])
    assert any("outside" in p for p in gate.s0lb_info(str(tmp_path / "hi.img"))[1])
    (tmp_path / "raw.bin").write_bytes(b"\x97\x02\x00\x00 not a boot table")
    assert gate.s0lb_info(str(tmp_path / "raw.bin")) == (None, [])


# ---------------------------------------------------------------------------
# 6. the early-warning run's build-host gate (tools/ooc_early_warning.sh)
# ---------------------------------------------------------------------------
OOC = TOOLS / "ooc_early_warning.sh"
FAKE_VIVADO = """#!/bin/bash
# stands in for vivado: honours -log and ooc_inline.tcl's -tclargs <repo> <rm_key> <out>
log=""; while [ $# -gt 0 ]; do case "$1" in -log) log="$2"; shift;; -tclargs) shift; key="$2"; out="$3"; break;; esac; shift; done
mkdir -p "$out"; : > "$out/${key}_synth.dcp"; echo "OOC_INLINE_COMPLETE $key vivado=fake" > "$log"
"""


def _ooc(tmp_path, log, *extra):
    fake = tmp_path / "vivado"
    fake.write_text(FAKE_VIVADO)
    fake.chmod(0o755)
    return subprocess.run(["bash", str(OOC), "--run", "--out", str(tmp_path / "out"),
                           "--rms", "greybox", "--vivado", str(fake), "--mint-log", str(log)]
                          + list(extra), capture_output=True, text=True, timeout=120)


def _age(path, minutes):
    t = path.stat().st_mtime - minutes * 60
    os.utime(str(path), (t, t))


@needs_make
def test_ooc_gate_refuses_until_the_mint_is_provably_done(tmp_path):
    mint = tmp_path / "mintwt"
    mint.mkdir()
    log = mint / "mint.log"
    log.write_text("routing...\n")
    r = _ooc(tmp_path, log)                                      # no MINT COMPLETE, no override
    assert r.returncode == 3 and "MINT COMPLETE" in r.stderr
    r = _ooc(tmp_path, log, "--mint-finished-no-complete-line")   # 1: no MINT_EXIT line
    assert r.returncode == 3 and "FAIL 1" in r.stdout
    log.write_text("make: *** Error 2\nMINT_EXIT=2\n")
    r = _ooc(tmp_path, log, "--mint-finished-no-complete-line")   # 2: written just now
    assert r.returncode == 3 and "FAIL 2" in r.stdout
    _age(log, 30)
    (mint / "w.mk").write_text("w:\n\tsleep 30\n")
    busy = subprocess.Popen([MAKE, "-f", "w.mk", "w"], cwd=str(mint),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import time
        time.sleep(0.5)
        r = _ooc(tmp_path, log, "--mint-finished-no-complete-line")   # 3: a make in the mint dir
        assert r.returncode == 3 and "FAIL 3" in r.stdout, r.stdout + r.stderr
    finally:
        busy.kill()
        busy.wait()
    assert not (tmp_path / "out" / "ooc.mk").exists(), "a refused gate must run nothing"
    r = _ooc(tmp_path, log, "--mint-finished-no-complete-line")       # all three verified
    assert r.returncode == 0, r.stdout + r.stderr
    rep = (tmp_path / "out" / "REPORT.md").read_text()
    assert "| greybox | PASS |" in rep and "evidence 3" in rep and "MINT_EXIT=2" in rep


@needs_make
def test_ooc_gate_accepts_mint_complete_without_the_override(tmp_path):
    log = tmp_path / "mint.log"
    log.write_text("...\nMINT COMPLETE\n")
    r = _ooc(tmp_path, log)
    assert r.returncode == 0 and "| greybox | PASS |" in (tmp_path / "out" / "REPORT.md").read_text()


# ---------------------------------------------------------------------------
# 7. the STATIC's probes file (FLOW_CONTRACT §6.3, the mint-3 gate)
# ---------------------------------------------------------------------------
def _gate_tree(tmp_path):
    prod = tmp_path / "prod"
    _static(prod)
    return prod


def test_static_ltx_gate_mbv_pass_and_sidecar(tmp_path):
    prod = _gate_tree(tmp_path)
    fake_static_debug(prod)
    r = static_gate(prod)
    assert r.returncode == 0, r.stdout
    side = json.loads((prod / "config_rm_greybox_static.ltx.json").read_text())
    assert side["kind"] == "static" and side["uuids"] == [MIG_UUID]
    assert side["static_usercode"] == USERCODE and side["static_id"] == (prod / "static_id.txt").read_text().strip()
    assert static_gate(prod, "mbv", "--check").returncode == 0
    # --check: the .ltx changed after its sidecar was written
    doc = json.loads((prod / "config_rm_greybox_static.ltx").read_text())
    doc["ltx_root"]["minor"] = 1
    (prod / "config_rm_greybox_static.ltx").write_text(json.dumps(doc))
    r = static_gate(prod, "mbv", "--check")
    assert r.returncode == 1 and "ltx_crc32" in r.stdout


RP_ILA = {"type": "ILA_V3", "name": "u_rp_dut/u_ila_dbg_demo", "reconfigTop": "u_rp_dut",
          "spec": "labtools_ila_v6", "uuid": "B18B3F67433359B4A1D657D53DA82FC9"}


@pytest.mark.parametrize("case,kw,needle", [
    ("ltx missing", dict(write_ltx=False), "is missing"),
    ("no static core", dict(cores=[]), "holds NO debug core"),
    ("RP core in it", dict(cores=[HUB, MIG, RP_ILA]), "names RP core"),
    ("two hubs", dict(cores=[HUB, dict(HUB, name="u_shell/x/xsdbm"), MIG]), "2 static debug hubs"),
    ("no DDR4 slave", dict(cores=[HUB]), "0 DDR4 MIG slaves"),
    ("uuid mismatch", dict(rpt_cores=[HUB, dict(MIG, uuid="0" * 32)]), "uuids"),
])
def test_static_ltx_gate_mbv_refusals(tmp_path, case, kw, needle):
    prod = _gate_tree(tmp_path)
    fake_static_debug(prod, **kw)
    r = static_gate(prod)
    assert r.returncode == 1 and needle in r.stdout, (case, r.stdout)
    assert not (prod / "config_rm_greybox_static.ltx.json").exists()


def test_static_ltx_gate_bare_metal_must_have_none(tmp_path):
    prod = _gate_tree(tmp_path)
    fake_static_debug(prod, cores=[])                            # the fielded 2024.1 shape
    r = static_gate(prod, "mb")
    assert r.returncode == 0 and "as required" in r.stdout
    assert not (prod / "config_rm_greybox_static.ltx").exists()
    fake_static_debug(prod)                                      # a static debug core appears
    r = static_gate(prod, "mb")
    assert r.returncode == 1 and "must NOT" in r.stdout and "forbids" in r.stdout
    (prod / "debug_core_rm_greybox_static.rpt").unlink()          # a tree without the report
    assert static_gate(prod, "mb").returncode == 1


def test_bake_binds_the_static_ltx_to_the_flashable_base(tmp_path):
    prod = _gate_tree(tmp_path)
    sid = (prod / "static_id.txt").read_text().strip()
    fake_bit(prod / "config_rm_greybox_stage0.bit", USERCODE, fill=b"\x01")
    fake_static_debug(prod)
    assert static_gate(prod).returncode == 0
    side = prod / "config_rm_greybox_static.ltx.json"
    assert _bake(prod, sid, extra=("--static-ltx-sidecar", side)).returncode == 0
    rec = json.loads((prod / "stage0_bake.json").read_text())
    assert rec["static_ltx"]["name"] == "config_rm_greybox_static.ltx"
    # NEGATIVE CONTROLS: a sidecar for another implementation; no sidecar at all
    d = json.loads(side.read_text())
    d["static_usercode"] = "0xDEADBEEF"
    side.write_text(json.dumps(d))
    r = _bake(prod, sid, extra=("--static-ltx-sidecar", side))
    assert r.returncode == 1 and "UserID" in r.stderr
    side.unlink()
    r = _bake(prod, sid, extra=("--static-ltx-sidecar", side))
    assert r.returncode == 1 and "static .ltx" in r.stderr


def test_bundle_carries_and_the_gate_checks_the_static_ltx(tmp_path):
    prod, sid, img, li = _linux_tree(tmp_path)
    assert _pack(tmp_path, prod, img, li).returncode == 0
    b = json.loads((prod / "linux_bundle.json").read_text())
    assert b["targets"]["mcc_sd"]["static_ltx"]["uuids"] == [MIG_UUID]
    gate = _load("gate", GATE_PATH)
    assert gate.main(["--repo", str(REPO), "--linux-record", str(prod)]) == 0
    (prod / "config_rm_greybox_static.ltx").write_text("{}")         # swapped afterwards
    assert gate.main(["--repo", str(REPO), "--linux-record", str(prod)]) == 1


def test_bundle_refuses_a_bake_that_bound_no_static_ltx(tmp_path):
    prod, sid, img, li = _linux_tree(tmp_path)
    assert _bake(prod, sid).returncode == 0                          # re-baked without it
    r = _pack(tmp_path, prod, img, li)
    assert r.returncode == 1 and "binds no static .ltx" in r.stderr


@pytest.mark.skipif(not TCLSH, reason="tclsh not installed")
@pytest.mark.parametrize("cores,expect", [
    ("u_shell/shell_bd_i/mig_dbg_hub/inst/xsdbm u_shell/shell_bd_i/ddr4_0", "WRITTEN"),
    ("", "none"),
    ("u_shell/shell_bd_i/ddr4_0 u_rp_dut/u_ila_dbg_demo", "FAILED"),
])
def test_write_static_debug_probes_tcl(tmp_path, cores, expect):
    """The Vivado half, under tclsh with the four Vivado commands stubbed."""
    out = tmp_path / "prod"
    out.mkdir()
    (out / "config_rm_greybox_static_pblock_rp_dut_partial.ltx").write_text("stale extra")
    (out / "config_rm_greybox_static.ltx.json").write_text("stale sidecar")
    tcl = """
proc get_debug_cores {args} { return {%s} }
proc report_debug_core {args} { set f [lindex $args end]; set fh [open $f w]; puts $fh rpt; close $fh }
proc write_debug_probes {args} {
    set f [lindex $args end]; set fh [open $f w]
    puts $fh {"type": "XSDB_V3", "name": "u_shell/shell_bd_i/mig_dbg_hub/inst/xsdbm"}
    close $fh
    set root [file rootname $f]
    foreach x {_pblock_rp_dut_partial.ltx _pblock_rp_dut_partial_clear.ltx} {
        set fh [open ${root}$x w]; puts $fh x; close $fh }
}
source %s
if {[catch {write_static_debug_probes %s u_rp_dut rm_greybox} e]} { puts "CAUGHT $e" }
""" % (cores, TOOLS / "debug_probes.tcl", out)
    r = subprocess.run([TCLSH], input=tcl, capture_output=True, text=True)
    names = sorted(p.name for p in out.iterdir())
    assert "config_rm_greybox_static.ltx.json" not in names, "a stale sidecar survived"
    if expect != "FAILED":
        assert "debug_core_rm_greybox_static.rpt" in names, r.stdout + r.stderr
    assert not [n for n in names if "_partial" in n], "Vivado's extras must be deleted: %s" % names
    if expect == "WRITTEN":
        assert "DFX_STATIC_LTX_WRITTEN" in r.stdout and "config_rm_greybox_static.ltx" in names
    elif expect == "none":
        assert "DFX_STATIC_LTX none" in r.stdout and "config_rm_greybox_static.ltx" not in names
    else:
        assert "DFX_LTX_GATE_FAILED" in r.stdout and "CAUGHT" in r.stdout


@needs_make
def test_static_ltx_gate_reaches_both_cpu_plans(tmp_path):
    mb = _plan(tmp_path)
    mbv = _plan(tmp_path, SHELL_CPU="mbv")
    assert "ltx_sidecar.py static --ref-key rm_greybox --rp-inst u_rp_dut --shell-cpu mb " in mb.stdout
    assert "ltx_sidecar.py static --ref-key rm_greybox --rp-inst u_rp_dut --shell-cpu mbv " in mbv.stdout
    assert "--static-ltx-sidecar" in mbv.stdout and "--static-ltx-sidecar" not in mb.stdout


@needs_make
def test_fielded_files_carries_the_static_ltx_and_skips_its_partial(tmp_path):
    prod = tmp_path / "b" / "prod"
    prod.mkdir(parents=True)
    (prod / "stage0_bake.json").write_text(json.dumps({"mint_kind": "mint"}))
    (prod / "config_rm_dbg_demo.ltx.json").write_text("{}")
    (prod / "config_rm_greybox_static.ltx.json").write_text("{}")
    env = dict(os.environ)
    env.pop("MINT_HUB", None)
    r = subprocess.run([MAKE, "-s", "-C", str(DFX), "fielded-files", "SHELL_CPU=mbv",
                        "BUILD=%s" % (tmp_path / "b"), "SHELL_PROJ=%s" % (tmp_path / "s")],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    out = r.stdout.split()
    assert str(prod / "config_rm_greybox_static.ltx") in out
    assert str(prod / "config_rm_dbg_demo_pblock_rp_dut_partial.bin") in out
    assert not [p for p in out if "static_pblock" in p], "the static sidecar was treated as an RM's"


# ---------------------------------------------------------------------------
# 8. post-mint packaging builds nothing (IMAGE's finding, 2026-09-24)
# ---------------------------------------------------------------------------
TOOL_RUNS = ("vivado", "updatemem", "build_shell.tcl", "ooc_synth", "static_canon.py compute",
             "write_mem_info", "make -C")


def _tool_runs(plan):
    """Tools a `make -n` plan would RUN: quoted text (echo'd hints such as
    "run the mint (make -C ...)") is not an invocation."""
    import re as _re
    hits = set()
    for line in plan.splitlines():
        bare = _re.sub(r'"[^"]*"', '""', line)
        hits.update(t for t in TOOL_RUNS if t in bare)
    return sorted(hits)


def _finished_old_mbv_tree(tmp_path):
    """A finished mbv mint whose every file is OLDER than today's sources -- the
    P-mint copy after SHELL/FLOW commits moved on."""
    b = tmp_path / "build"
    prod = b / "prod"
    prod.mkdir(parents=True)
    for rel in ("preflight.stamp", "static_canon.json", "shell_proj/shell_static_synth.dcp",
                "prod/static_canon.json", "prod/rm_nanosoc_synth.dcp", "prod/static_id.txt",
                "prod/overlay_inputs.txt", "prod/static_routed_locked.dcp",
                "prod/config_rm_greybox_routed.dcp", "prod/config_rm_greybox.bit",
                "prod/config_rm_greybox.mmi", "prod/stage0.elf", "prod/config_rm_greybox_stage0.bit",
                "prod/stage0_bake.json", "prod/mint.json", "prod/config_rm_greybox_static.ltx",
                "prod/config_rm_greybox_static.ltx.json", "overlay_mbv/mps3_shell_static_id.c"):
        p = b / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
        os.utime(str(p), (946684800, 946684800))          # 2000-01-01: older than every source
    return b


@needs_make
def test_mint_linux_image_plans_zero_tool_runs_on_a_finished_tree(tmp_path):
    b = _finished_old_mbv_tree(tmp_path)
    args = dict(SHELL_CPU="mbv", BUILD=b, SHELL_PROJ=b / "shell_proj", MINT_HUB="",
                LINUX_ARTIFACTS=tmp_path / "artifacts", RM_SET="greybox led dbg_demo nanosoc",
                SHELL_TOUCH=1, TOUCH=1)
    plan = make_n("mint-linux-image", **args)
    assert plan.returncode == 0, plan.stderr
    runs = _tool_runs(plan.stdout)
    assert not runs, "post-mint packaging plans %s:\n%s" % (runs, plan.stdout)
    assert "linux_bundle.py pack" in plan.stdout and "--mint-record" in plan.stdout
    # NEGATIVE CONTROL: the same tree IS stale for the build targets -- so the
    # zero above is the rule, not an up-to-date accident.
    stage0 = make_n("mint-stage0", **args)
    assert {"vivado", "updatemem"} & set(_tool_runs(stage0.stdout)), stage0.stdout
    chk = make_n("linux-bundle-check", **args)
    assert not _tool_runs(chk.stdout), chk.stdout


def test_bundle_refuses_a_canon_that_is_not_the_mints(tmp_path):
    prod, sid, img, li = _linux_tree(tmp_path)
    shutil.copy(str(tmp_path / "static_canon.json"), str(prod / "static_canon.json"))
    flags = {"SHELL_CPU": "mbv", "SHELL_REALPHY": "0", "SHELL_TOUCH": "1"}
    (prod / "mint.json").write_text(json.dumps(       # the mint recorded ANOTHER canon
        {"static_canon": {"value": {"digest": "cd" * 32, "flags": flags}, "reason": None}}))
    r = run_tool("linux_bundle.py", "pack", "--prod", prod,
                 "--static-canon", prod / "static_canon.json", "--mint-record", prod / "mint.json",
                 "--blob", img, "--blob-info", str(img) + ".json", "--legal-info", li,
                 "--overlay-root", tmp_path / "overlay")
    assert r.returncode == 1 and "is not the static_canon this mint recorded" in r.stderr, r.stderr
    (prod / "mint.json").write_text(json.dumps(       # the fixture's own canon
        {"static_canon": {"value": {"digest": "ab" * 32, "flags": flags}, "reason": None}}))
    r = run_tool("linux_bundle.py", "pack", "--prod", prod,
                 "--static-canon", prod / "static_canon.json", "--mint-record", prod / "mint.json",
                 "--blob", img, "--blob-info", str(img) + ".json", "--legal-info", li,
                 "--overlay-root", tmp_path / "overlay")
    assert r.returncode == 0, r.stderr


# ---------------------------------------------------------------------------
# 9. the greybox clearing the image carries (/etc/mps3/greybox_clear.bin)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mutate,needle", [
    ("absent", "records no greybox clearing"),
    ("other static", "is not this static's greybox clearing"),
    ("other id", "recorded for static_id"),
])
def test_bundle_refuses_a_foreign_or_missing_greybox_clearing(tmp_path, mutate, needle):
    prod, sid, img, li = _linux_tree(tmp_path)
    side = json.loads((img.parent / "boot.img.json").read_text())
    if mutate == "absent":
        del side["greybox_clear_sha256"]
    elif mutate == "other static":
        import hashlib
        side["greybox_clear_sha256"] = hashlib.sha256(b"greybox clearing of 0x2B082E1B").hexdigest()
    else:
        side["greybox_clear_static_id"] = JULY_STATIC
    (img.parent / "boot.img.json").write_text(json.dumps(side))
    r = _pack(tmp_path, prod, img, li)
    assert r.returncode == 1 and needle in r.stderr, r.stderr


def test_gate_catches_a_swapped_greybox_clearing(tmp_path):
    prod, sid, img, li = _linux_tree(tmp_path)
    assert _pack(tmp_path, prod, img, li).returncode == 0
    b = json.loads((prod / "linux_bundle.json").read_text())
    assert b["targets"]["ethernet"]["greybox_clear"]["sha256"] == _clear_sha(prod)
    gate = _load("gate", GATE_PATH)
    assert gate.main(["--repo", str(REPO), "--linux-record", str(prod)]) == 0
    (prod / GREYBOX_CLEAR).write_bytes(b"a re-routed static's clearing")
    assert gate.main(["--repo", str(REPO), "--linux-record", str(prod)]) == 1


@needs_make
def test_linux_image_env_exports_the_greybox_clearing(tmp_path):
    b = tmp_path / "b"
    prod = b / "prod"
    sid = _static(prod)
    stage0_elf(prod / "stage0.elf", sid)
    env = dict(os.environ)
    env.pop("MINT_HUB", None)
    run = lambda: subprocess.run([MAKE, "-s", "-C", str(DFX), "linux-image-env", "SHELL_CPU=mbv",
                                  "BUILD=%s" % b, "SHELL_PROJ=%s" % (b / "s")],
                                 capture_output=True, text=True, env=env)
    r = run()
    assert r.returncode == 0 and "MPS3_GREYBOX_CLEAR=%s" % (prod / GREYBOX_CLEAR) in r.stdout, r.stdout + r.stderr
    ov = b / "overlay_mbv" / "greybox"                       # the re-keyed overlay copy
    ov.mkdir(parents=True)
    (ov / "greybox_clear.bin").write_bytes(b"another static's clearing")
    r = run()
    assert r.returncode != 0 and "differs" in r.stderr


# ---------------------------------------------------------------------------
# 9. stage 6 (mb): the BSP is named up front and created from THIS mint's XSA
#    (findings #4: a fresh worktree failed at stage 6, hours in, on xil_printf.h)
# ---------------------------------------------------------------------------
BSP_REL = "shell_platform/microblaze_0/standalone_domain/bsp/microblaze_0"


def _bsp(ws):
    b = ws / BSP_REL
    for rel in ("include/xparameters.h", "include/xil_printf.h", "lib/libxil.a"):
        (b / rel).parent.mkdir(parents=True, exist_ok=True)
        (b / rel).write_text("x")


@needs_make
def test_mb_mint_plans_the_bsp_only_when_it_is_missing(tmp_path):
    missing = _plan(tmp_path)
    assert missing.returncode == 0, missing.stderr
    out = missing.stdout
    assert "include/xparameters.h is MISSING" in out
    create = "create_platform.tcl %s %s" % (tmp_path / "s" / "shell_harness.xsa", tmp_path / "w")
    assert create in out, out
    assert -1 < out.find("[1/8] preflight") < out.find("[6/8] BSP") < out.find("[6/8] firmware")
    _bsp(tmp_path / "w")                                  # the main checkout's case
    present = _plan(tmp_path)
    assert present.returncode == 0, present.stderr
    assert "create_platform" not in present.stdout and "MISSING" not in present.stdout
    mbv = _plan(tmp_path, SHELL_CPU="mbv", VIVADO="2026.1", FW_WS=tmp_path / "none")
    assert mbv.returncode == 0, mbv.stderr
    assert "create_platform" not in mbv.stdout, "mbv's stage 6 is stage0: no BSP"


@needs_make
def test_control_preflight_refuses_in_seconds_with_no_bsp_and_no_xsct(tmp_path):
    env = dict(os.environ)
    for v in ("MINT_HUB", "SHELL_CPU", "VIVADO", "VIVADO_VER"):
        env.pop(v, None)
    args = ["BUILD=%s" % (tmp_path / "b"), "SHELL_PROJ=%s" % (tmp_path / "s"),
            "OVERLAY_ROOT=%s" % (tmp_path / "o"), "FW_WS=%s" % (tmp_path / "w"),
            "XSCT=%s" % (tmp_path / "no_xsct"), "MINT_HUB=",
            "USD_PREMINT=skip"]   # if the check ever stops refusing, no VCS bench starts
    r = subprocess.run([MAKE, "--no-print-directory", "-C", str(DFX), "mint-preflight"] + args,
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode != 0
    assert "FAIL preflight: no xsct at %s" % (tmp_path / "no_xsct") in r.stdout, r.stdout + r.stderr
    assert not (tmp_path / "b" / "preflight.stamp").exists()
    assert "check_bd_config_lint" not in r.stdout      # refused before any other gate ran



# ---------------------------------------------------------------------------
# 10. The XDC gate in build_dfx.tcl (SHELL's fpga/shell/tools/xdc_gate.tcl):
#     Vivado DROPS `if`/`catch`/`puts` in an XDC (Designutils 20-1307)
# ---------------------------------------------------------------------------
XDC_GATE = REPO / "fpga" / "shell" / "tools" / "xdc_gate.tcl"


def _tcl_procs(path, names):
    """The text of each named top-level proc in a Tcl file (brace-balanced)."""
    import re as _re
    out, cur, depth = [], None, 0
    for line in pathlib.Path(path).read_text().splitlines(True):
        if cur is None:
            m = _re.match(r"proc (\S+) ", line)
            if not (m and m.group(1) in names):
                continue
            cur = ""
        cur += line
        depth += line.count("{") - line.count("}")
        if depth == 0:
            out.append(cur)
            cur = None
    assert len(out) == len(names), (names, len(out))
    return "".join(out)


def _xdc_procs_run(tmp_path, body):
    tcl = ("source %s\nproc read_xdc {args} { puts \"READ_XDC $args\" }\nset ::drops 0\n"
           "proc get_msg_config {args} { return $::drops }\n%s\n%s\n"
           % (XDC_GATE, _tcl_procs(DFX / "build_dfx.tcl", ["dfx_read_xdc", "dfx_xdc_dropped_gate"]),
              body))
    f = tmp_path / ("run%d.tcl" % len(list(tmp_path.glob("run*.tcl"))))
    f.write_text(tcl)                     # a script FILE: tclsh exits 1 on an uncaught error
    return subprocess.run([TCLSH, str(f)], capture_output=True, text=True)


@pytest.mark.skipif(TCLSH is None or not XDC_GATE.is_file(), reason="tclsh / SHELL's xdc_gate.tcl")
def test_xdc_gate_procs_and_their_controls(tmp_path):
    good = tmp_path / "good.xdc"
    good.write_text("set_false_path -from [get_ports a]\n")
    bad = tmp_path / "bad.xdc"
    bad.write_text("if {1} {\n  set_false_path -from [get_ports c]\n}\n")
    r = _xdc_procs_run(tmp_path, "dfx_read_xdc rm_x %s -cell u_rp_dut\ndfx_xdc_dropped_gate rm_x link\n"
                                 "puts DONE" % good)
    assert r.returncode == 0 and "READ_XDC -cell u_rp_dut %s" % good in r.stdout, r.stdout + r.stderr
    assert "DONE" in r.stdout and "DFX_XDC_GATE_FAILED" not in r.stdout
    # CONTROL 1: an `if` in the file -> refused BEFORE read_xdc, with the marker
    r = _xdc_procs_run(tmp_path, "dfx_read_xdc rm_x %s\nputs DONE" % bad)
    assert r.returncode != 0 and "READ_XDC" not in r.stdout and "DONE" not in r.stdout
    assert "DFX_XDC_GATE_FAILED rm=rm_x stage=subset file=%s" % bad in r.stdout, r.stdout
    # CONTROL 2: Vivado counted a 20-1307 -> the dropped gate fails, with the marker
    r = _xdc_procs_run(tmp_path, "set ::drops 2\ndfx_xdc_dropped_gate rm_x opt\nputs DONE")
    assert r.returncode != 0 and "DONE" not in r.stdout
    assert "DFX_XDC_GATE_FAILED rm=rm_x stage=opt" in r.stdout, r.stdout


def test_every_xdc_build_dfx_reads_goes_through_the_gate():
    import re as _re
    src = (DFX / "build_dfx.tcl").read_text()
    code = [l for l in src.splitlines() if not l.lstrip().startswith("#")]
    raw = [l for l in code if _re.search(r"\bread_xdc\b", l) and "dfx_read_xdc" not in l]
    assert raw == ["    read_xdc {*}$args $xdc"], raw           # only inside dfx_read_xdc
    calls = [l.strip() for l in code if l.strip().startswith("dfx_read_xdc ")]
    assert calls == ["dfx_read_xdc $rm_key $shell_timing_xdc", "dfx_read_xdc $rm_key $mbv_timing_xdc",
                     "dfx_read_xdc $rm_key $rm_xdc -cell [get_cells $rp_inst]"], calls
    assert "source -notrace $repo_root/fpga/shell/tools/xdc_gate.tcl" in src
    assert "source $repo_root/fpga/dfx/dfx_floorplan.xdc" in src      # Tcl, never an XDC check
    i_link = src.index("dfx_xdc_dropped_gate $rm_key link")
    i_opt = src.index("    opt_design\n")
    i_after = src.index("dfx_xdc_dropped_gate $rm_key opt")
    assert src.index("dfx_read_xdc $rm_key $rm_xdc") < i_link < i_opt < i_after
    mk = (DFX / "Makefile").read_text()
    greps = [l for l in mk.splitlines() if "_GATE_FAILED" in l and "grep" in l]
    assert greps and all("DFX_(LTX|HDPR|RP_PIN|XDC)_GATE_FAILED" in l for l in greps), greps
    jobs = (TOOLS / "dfx_jobs.py").read_text()
    assert 'r"^DFX_(LTX|HDPR|RP_PIN|XDC)_GATE_FAILED.*$"' in jobs
    assert '"fpga/shell/tools/xdc_gate.tcl"' in jobs      # a changed gate voids a DFX_JOBS resume


@pytest.mark.skipif(TCLSH is None or not XDC_GATE.is_file(), reason="tclsh / SHELL's xdc_gate.tcl")
def test_every_xdc_a_mint_reads_passes_the_subset_scan():
    """The files build_dfx.tcl read_xdc's today: the shell timing XDC, the mbv one,
    every RM's _rm.xdc. The DORMANT QSPI pad model is scanned too, so enabling it
    later cannot bring an `if` back."""
    files = [REPO / "fpga/shell/constraints/mps3_harness_timing.xdc",
             REPO / "fpga/shell/constraints/mbv/mbv_timing.xdc",
             REPO / "fpga/shell/constraints/qspi/mps3_qspi_pad_timing.xdc"]
    files += sorted((REPO / "fpga" / "rp").glob("*/*_rm.xdc"))
    tcl = "source %s\n" % XDC_GATE + "".join(
        'puts "HITS [llength [soclabs_xdc_scan {%s}]] [soclabs_xdc_scan {%s}]"\n' % (f, f) for f in files)
    r = subprocess.run([TCLSH], input=tcl, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    hits = r.stdout.splitlines()
    assert len(hits) == len(files) and all(h.startswith("HITS 0") for h in hits), r.stdout



# ---------------------------------------------------------------------------
# 11. RC1 (2026-09-24, static 0xAE3CF93E) failed at stage 6: the USR_ACCESS
#     reader byte-searched frame data, and the recipe built stage0 anyway
# ---------------------------------------------------------------------------
AXSS_SIG = struct.pack(">I", 0x3001A001)


def _byte_search(path):
    """The reader RC1 ran (bit_identity.py before 2026-09-24), kept as the control."""
    d = pathlib.Path(path).read_bytes()
    out, i = [], d.find(AXSS_SIG)
    while i >= 0:
        out.append((i, struct.unpack_from(">I", d, i + 4)[0]))
        i = d.find(AXSS_SIG, i + 1)
    return out


def _bi():
    return _load("bit_identity", TOOLS / "bit_identity.py")


def test_axss_reader_ignores_the_signature_inside_frame_data(tmp_path):
    # RC1's shape: the header bytes 2 bytes OFF the word grid in SLR0's frame data,
    # then once more ON the grid, still inside the FDRI payload.
    frames = (b"\x00\x02" + AXSS_SIG + struct.pack(">I", 0x82404000) + b"\x00\x00"
              + AXSS_SIG + struct.pack(">I", 0x0BADF00D))
    bit = tmp_path / "config_rm_greybox.bit"
    fake_bit(bit, USERCODE, frames=frames)
    bi = _bi()
    assert [[v for _o, v in s] for s in bi.usr_access_by_slr(str(bit))] == [[VER32], [VER32]]
    assert bi.usr_access_check(str(bit)) == (VER32, [])
    # CONTROL: the byte search RC1 ran sees the frame data too -- the fixture IS RC1's case
    assert sorted({v for _o, v in _byte_search(bit)}) == sorted({VER32, 0x82404000, 0x0BADF00D})
    (tmp_path / "static_id.txt").write_text("0xAE3CF93E\n")
    ident = run_tool("stage0_bake.py", "identity", "--bit", bit, "--static-id-file",
                     tmp_path / "static_id.txt")
    assert ident.returncode == 0 and ident.stdout.split() == ["0xAE3CF93E", "0x%08X" % VER32]
    assert run_tool("bit_identity.py", bit, "--expect", "0x%08X" % VER32).returncode == 0


@pytest.mark.parametrize("kw,needle", [
    (dict(slr1=0x82404000), "DIFFERENT USR_ACCESS values"),
    (dict(slr1=None), "SLR stream 1 writes USR_ACCESS 0 time(s)"),
])
def test_axss_gate_wants_one_word_in_every_slr(tmp_path, kw, needle):
    bit = tmp_path / "config_rm_greybox.bit"
    fake_bit(bit, USERCODE, **kw)
    value, problems = _bi().usr_access_check(str(bit))
    assert value is None and any(needle in p for p in problems), problems
    r = run_tool("bit_identity.py", bit)
    assert r.returncode == 1 and needle in r.stderr, r.stdout + r.stderr
    (tmp_path / "static_id.txt").write_text("0xAE3CF93E\n")
    ident = run_tool("stage0_bake.py", "identity", "--bit", bit, "--static-id-file",
                     tmp_path / "static_id.txt")
    assert ident.returncode != 0 and ident.stdout.strip() == "" and needle in ident.stderr


def test_axss_unstamped_and_malformed(tmp_path):
    bi = _bi()
    bit = tmp_path / "unstamped.bit"
    fake_bit(bit, USERCODE, usr_access=None, slr1=None)
    assert bi.usr_access_check(str(bit)) == (None, [])
    assert run_tool("bit_identity.py", bit).returncode == 0          # a legal unstamped build
    (tmp_path / "static_id.txt").write_text("0xAE3CF93E\n")
    ident = run_tool("stage0_bake.py", "identity", "--bit", bit, "--static-id-file",
                     tmp_path / "static_id.txt")
    assert ident.returncode != 0 and "no USR_ACCESS write" in ident.stderr   # but not for stage0
    for bad_sid in ("\n", "0x00000000\n", "garbage\n"):
        good = tmp_path / "good.bit"
        fake_bit(good, USERCODE)
        (tmp_path / "static_id.txt").write_text(bad_sid)
        r = run_tool("stage0_bake.py", "identity", "--bit", good, "--static-id-file",
                     tmp_path / "static_id.txt")
        assert r.returncode != 0 and r.stdout.strip() == "", bad_sid
    # a word that is no packet header is an error, never skipped
    data = bytearray(bit.read_bytes())
    i = data.find(b"\xaa\x99\x55\x66") + 4
    data[i:i + 4] = struct.pack(">I", 0x12345678)
    bad = tmp_path / "malformed.bit"
    bad.write_bytes(bytes(data))
    with pytest.raises(bi.BitstreamError):
        bi.usr_access_by_slr(str(bad))
    assert run_tool("bit_identity.py", bad).returncode == 1


REAL_BITS = sorted(p for p in (REPO / "fpga" / "dfx").glob("build*/prod/config_rm_greybox*.bit")
                   if "partial" not in p.name)


@pytest.mark.skipif(not REAL_BITS, reason="no full bitstream in fpga/dfx/build*/prod")
# ids as a LIST: pytest calls an ids function on the NOTSET placeholder of an empty
# parameter set during collection, before skipif applies (a clean checkout = CI)
@pytest.mark.parametrize("bit", REAL_BITS,
                         ids=["%s/%s" % (p.parent.parent.name, p.name) for p in REAL_BITS])
def test_axss_reader_on_real_full_bitstreams(bit):
    """Every real full .bit on disk walks cleanly: 2 SLR streams (KU115), one word each, equal."""
    bi = _bi()
    slrs = bi.usr_access_by_slr(str(bit))
    value, problems = bi.usr_access_check(str(bit))
    assert len(slrs) == 2 and not problems and value is not None, (slrs, problems)


OLD_STAGE0_IDS = (
    '\t set -- $$($(STAGE0_BAKE) identity --bit $(PROD)/config_rm_greybox.bit '
    '--static-id-file $(PROD)/static_id.txt); \\\n'
    '\t sid=$$1; v32=$$2; \\\n')


def _stage0_makefile_rc1(tmp_path):
    """The Makefile with RC1's stage-6 identity lines put back (the control)."""
    mk = (DFX / "Makefile").read_text()
    a = mk.index("\t ids=$$($(STAGE0_BAKE) identity")
    b = mk.index('\t echo "   building stage0 for static_id', a)
    out = tmp_path / "Makefile.rc1"
    out.write_text(mk[:a] + OLD_STAGE0_IDS + mk[b:])
    return out


@needs_make
def test_stage6_never_builds_stage0_with_empty_ids(tmp_path):
    b = tmp_path / "b"
    prod = b / "prod"
    prod.mkdir(parents=True)
    (prod / "static_id.txt").write_text("0xAE3CF93E\n")
    fake_bit(prod / "config_rm_greybox.bit", USERCODE, slr1=0x82404000)
    fs0 = tmp_path / "fs0"                       # a stage0 "build" that records its inputs
    fs0.mkdir()
    (fs0 / "Makefile").write_text(
        "clean:\n\t@true\nstage0.elf:\n"
        "\t@echo 'id=[$(MPS3_STATIC_ID)] ver=[$(MPS3_HARNESS_VER32)]' >> calls.txt; touch stage0.elf\n")
    env = dict(os.environ)
    for v in ("MINT_HUB", "SHELL_CPU", "VIVADO", "VIVADO_VER", "MAKEFLAGS", "MFLAGS"):
        env.pop(v, None)

    def run(makefile):
        return subprocess.run(
            [MAKE, "--no-print-directory", "-C", str(DFX), "-f", str(makefile), "DFX_DIR=%s" % DFX,
             str(prod / "stage0.elf"), "SHELL_CPU=mbv", "VIVADO=2026.1", "BUILD=%s" % b,
             "SHELL_PROJ=%s" % (b / "s"), "STAGE0_DIR=%s" % fs0,
             "-o", str(prod / "static_id.txt"), "-o", str(prod / "config_rm_greybox.bit")],
            capture_output=True, text=True, env=env, timeout=120)
    calls = fs0 / "calls.txt"
    r = run(DFX / "Makefile")
    assert r.returncode != 0 and "NOT building stage0 with empty values" in r.stdout, r.stdout + r.stderr
    assert not calls.exists(), "the stage0 build must not be reached"
    # CONTROL: RC1's recipe swallowed the refusal and built stage0 with nothing in it
    r = run(_stage0_makefile_rc1(tmp_path))
    assert calls.read_text().strip() == "id=[] ver=[]", r.stdout + r.stderr
    calls.unlink()
    # and a good base reaches the build with both identities
    fake_bit(prod / "config_rm_greybox.bit", USERCODE)
    run(DFX / "Makefile")
    assert calls.read_text().strip() == "id=[0xAE3CF93E] ver=[0x%08X]" % VER32


@needs_make
def test_linux_image_env_prints_nothing_without_one_identity(tmp_path):
    b = tmp_path / "b"
    prod = b / "prod"
    sid = _static(prod)
    stage0_elf(prod / "stage0.elf", sid)
    fake_bit(prod / "config_rm_greybox.bit", USERCODE, slr1=0x82404000)
    env = dict(os.environ)
    env.pop("MINT_HUB", None)
    r = subprocess.run([MAKE, "-s", "-C", str(DFX), "linux-image-env", "SHELL_CPU=mbv",
                        "BUILD=%s" % b, "SHELL_PROJ=%s" % (b / "s")],
                       capture_output=True, text=True, env=env)
    assert r.returncode != 0 and "export" not in r.stdout, r.stdout + r.stderr
    assert "DIFFERENT USR_ACCESS values" in r.stderr
