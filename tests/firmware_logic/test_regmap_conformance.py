"""tests/firmware_logic/test_regmap_conformance.py — the shell register map says
the same thing everywhere it is written down.

WHAT CHANGED, AND WHY THIS TEST IS DIFFERENT NOW
------------------------------------------------
This test used to compare three hand-maintained transcriptions of the map and it
covered **7 of the 15 blocks** — because every block had to be listed here, by
hand, in a ``BLOCKS`` dict that named each register three times. The eight blocks
nobody added were unpoliced, and they drifted: ``platform_regs.h`` said "CLCDKVM
— RESERVED, NOT INSTANTIATED" while ``fpga/shell/bd/shell_bd.tcl:578``
instantiated ``clcd_kvm_0`` and ``:1407`` gave it an address, and
``shell-regmap.md``'s map table announced a TOUCH block in its changelog that it
never gained a row for.

The map is now GENERATED (``tools/gen_regmap.py``: bases from the BD's
``assign_bd_address`` lines, offsets from each CSR block's own RTL decode). So
the four views cannot disagree by construction — and this test's job changes
accordingly. It now asserts three things a generator alone does not give you:

  1. **The generated views really do agree** — parsed back out of the tracked
     files, independently of the generator. A splice bug, a hand edit inside a
     fence, or a stale tracked file shows up here as well as in
     ``check_generated_fresh.py``.
  2. **The HAND-WRITTEN prose tables in the contract agree with the generated
     one.** ``shell-regmap.md``'s per-block sections carry the semantics (bit
     fields, side effects, hazards) and are written by a person. That is exactly
     where an offset can now go stale, so that is what is checked.
  3. **Coverage is total.** Every block the BD assigns inside the contract window
     appears in every view — no opt-in list to forget to extend.
  4. **One owner per page, and no third claimant.** ``0x44AE_0000`` was claimed
     twice — by the touch AXI IIC in ``fpga/shell/bd/touch_iic_add.tcl`` and by
     the staged ``axi_jtag`` in ``fpga/shell/bd/axijtag_uart_carry.tcl`` /
     ``host/socket_harness/carry_across.py`` — because a page taken by a block
     that is DESIGNED but unbuilt was written down in whichever file needed it,
     and nothing joined those files up. The map now carries the reservations too
     (``gen_regmap.RESERVATIONS``), and the last section here scans every tracked
     (and untracked-not-ignored) file for base DECLARATIONS in the region: a page
     claimed by an owner the map does not know, or at an address the map does not
     give it, fails. Both directions are mutation-controlled.

Pure parsing plus one import of the generated data module; no C toolchain, no
cocotb, no board.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REGMAP_MD = _REPO_ROOT / "docs" / "contracts" / "shell-regmap.md"
_PLATFORM_REGS_H = _REPO_ROOT / "firmware" / "common" / "platform_regs.h"
_REGMAP_PY = _REPO_ROOT / "tests" / "common" / "regmap.py"
_PYVERIFY_REGMAP = _REPO_ROOT / "host" / "pyverify" / "pyverify" / "regmap.py"

pytestmark = pytest.mark.skipif(
    not all(p.is_file() for p in (_REGMAP_MD, _PLATFORM_REGS_H, _REGMAP_PY,
                                  _PYVERIFY_REGMAP)),
    reason="one of the four generated regmap views is missing",
)

sys.path.insert(0, str(_REPO_ROOT / "host" / "pyverify"))
sys.path.insert(0, str(_REPO_ROOT / "tools"))
from pyverify import regmap as _ref  # noqa: E402  (the structured view)
import gen_regmap as _gen  # noqa: E402  (only for the alias cross-check)


#: The register spellings that legitimately differ between views, as this test
#: understands them: (block, python name) -> (c name, md name). It is asserted
#: EQUAL to the generator's own table below, so the two cannot drift apart —
#: adding an alias to the generator without declaring it here fails.
ALIASES = {
    ("UARTBR", "U0_DATA"): ("U0_TXRX", "U0_TX"),
    ("UARTBR", "U1_DATA"): ("U1_TXRX", "U1_TX"),
}

#: Blocks whose semantics live in vendor documentation, not in a hand-written
#: table in shell-regmap.md. Rule 2 (prose-vs-generated) cannot apply to them —
#: there is no prose table to compare. Anything else missing a prose table is a
#: failure, not an exemption.
NO_PROSE_TABLE = {"HWICAP", "DBGBR", "MMCM_DRP", "TOUCH", "SWDBB"}


# --------------------------------------------------------------------------- #
# Parsers — each reads a TRACKED file back, with no help from the generator.
# --------------------------------------------------------------------------- #
def _c_view() -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    """platform_regs.h -> (bases, {block: {reg: offset}})."""
    text = _PLATFORM_REGS_H.read_text()
    bases = {m.group(1): int(m.group(2), 16) for m in
             re.finditer(r"^#define\s+MPS3_(\w+)_BASE\s+(0x[0-9A-Fa-f]+)u",
                         text, re.M)}
    offs: dict[str, dict[str, int]] = {}
    block = None
    for line in text.splitlines():
        h = re.match(r"^/\* ---- ([A-Z][A-Z0-9_]*) @ 0x", line)
        if h:
            block = h.group(1)
            offs.setdefault(block, {})
            continue
        d = re.match(r"^#define\s+(\w+)\s+(0x[0-9A-Fa-f]+)u", line)
        if d and block and d.group(1).startswith(block + "_"):
            offs[block][d.group(1)[len(block) + 1:]] = int(d.group(2), 16)
    return bases, offs


def _py_view(blocks: list[str]) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    """tests/common/regmap.py -> (bases, {block: {reg: offset}}).

    Constant names are ``<BLOCK>_<REG>`` with no separator to key on, so the
    block set is taken from the reference view and matched longest-prefix-first
    (MMCM_DRP must win over nothing, and CLCD must not swallow CLCDKVM).
    """
    text = _REGMAP_PY.read_text()
    consts = {m.group(1): int(m.group(2), 16) for m in
              re.finditer(r"^([A-Z][A-Z0-9_]*)\s*=\s*(0x[0-9A-Fa-f]+)\b",
                          text, re.M)}
    ordered = sorted(blocks, key=len, reverse=True)
    bases, offs = {}, {b: {} for b in blocks}
    for name, val in consts.items():
        for b in ordered:
            if name == f"{b}_BASE":
                bases[b] = val
                break
            if name.startswith(b + "_"):
                offs[b][name[len(b) + 1:]] = val
                break
    return bases, offs


def _md_generated() -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    """The two GENERATED tables in shell-regmap.md."""
    text = _REGMAP_MD.read_text()
    bases = {}
    for m in re.finditer(r"^\|\s*`(0x[0-9A-Fa-f]{4})_([0-9A-Fa-f]{4})`\s*\|\s*"
                         r"([A-Z][A-Z0-9_]*)\s*\|", text, re.M):
        bases[m.group(3)] = int(m.group(1) + m.group(2), 16)
    offs: dict[str, dict[str, int]] = {}
    for m in re.finditer(r"^\|\s*([A-Z][A-Z0-9_]*)\s*\|\s*`(\w+)`\s*\|\s*"
                         r"0x([0-9A-Fa-f]+)\s*\|", text, re.M):
        offs.setdefault(m.group(1), {})[m.group(2)] = int(m.group(3), 16)
    return bases, offs


def _md_prose() -> dict[str, dict[str, int]]:
    """The HAND-WRITTEN per-block tables (``## BLOCK (0x...)`` sections).

    Rows look like ``| 0x14 | `FIFO_STATUS` | ... |``. Reserved rows carry no
    back-ticked name and are skipped. A row naming two registers at one offset
    (``| 0x00 | `U0_TX` / `U0_RX` | ...``) contributes the FIRST name, which is
    the spelling the alias table declares.
    """
    out: dict[str, dict[str, int]] = {}
    block = None
    for line in _REGMAP_MD.read_text().splitlines():
        h = re.match(r"^##\s+([A-Z][A-Z0-9_]*)\s*\(0x", line)
        if h:
            block = h.group(1)
            out.setdefault(block, {})
            continue
        if line.startswith("## "):
            block = None
            continue
        if block is None or not line.startswith("| 0x"):
            continue
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 4:
            continue
        off_m = re.fullmatch(r"0x([0-9A-Fa-f]+)", cells[1])
        reg_m = re.search(r"`([A-Za-z_]\w*)`", cells[2])
        if off_m and reg_m:
            out[block][reg_m.group(1)] = int(off_m.group(1), 16)
    return out


BLOCK_NAMES = [b.name for b in _ref.BLOCKS]
C_BASES, C_OFFS = _c_view()
PY_BASES, PY_OFFS = _py_view(BLOCK_NAMES)
MD_BASES, MD_OFFS = _md_generated()
MD_PROSE = _md_prose()


def _names(block: str, py_name: str) -> tuple[str, str]:
    """(c name, md name) for a register, per the declared alias table."""
    return ALIASES.get((block, py_name), (py_name, py_name))


# --------------------------------------------------------------------------- #
# Vacuity guards — a parser that finds nothing makes every check below pass.
# --------------------------------------------------------------------------- #
def test_parsers_are_not_vacuous():
    assert len(BLOCK_NAMES) >= 15, f"reference view has {len(BLOCK_NAMES)} blocks"
    assert len(C_BASES) >= 15, f"C header base parse looks empty: {C_BASES}"
    assert sum(len(v) for v in C_OFFS.values()) > 60, "C offset parse looks empty"
    assert sum(len(v) for v in PY_OFFS.values()) > 60, "py offset parse looks empty"
    assert len(MD_BASES) >= 15, f"md map-table parse looks empty: {MD_BASES}"
    assert sum(len(v) for v in MD_OFFS.values()) > 60, "md offset parse looks empty"
    assert sum(len(v) for v in MD_PROSE.values()) > 30, "md prose parse looks empty"
    # Spot values a broken parser could not fabricate.
    assert C_BASES["GENCHK"] == 0x44A60000
    assert PY_OFFS["GENCHK"]["TX_CNT"] == 0x08
    assert MD_OFFS["DFXCTL"]["RM_ID"] == 0x10
    assert MD_PROSE["UARTBR"]["FIFO_STATUS"] == 0x14


def test_alias_table_matches_the_generator():
    """This test's alias declarations and the generator's are one table."""
    theirs = {(blk, py): (c, md)
              for (blk, _rtl), (c, py, md) in _gen.NAME_ALIASES.items()
              if not (c == py == md)}
    assert ALIASES == theirs, (
        "the generator's NAME_ALIASES and this test's ALIASES disagree; a view "
        "is being emitted under a spelling nothing here checks"
    )


# --------------------------------------------------------------------------- #
# 1. The four generated views agree — bases and offsets, every block.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("block", BLOCK_NAMES)
def test_block_base_agrees_across_views(block):
    ref = _ref.BY_NAME[block].base
    assert block in C_BASES, f"MPS3_{block}_BASE missing from platform_regs.h"
    assert C_BASES[block] == ref, (
        f"{block}: platform_regs.h 0x{C_BASES[block]:08X} != BD 0x{ref:08X}")
    assert PY_BASES.get(block) == ref, (
        f"{block}: tests/common/regmap.py {PY_BASES.get(block)} != BD 0x{ref:08X}")
    assert MD_BASES.get(block) == ref, (
        f"{block}: shell-regmap.md map table {MD_BASES.get(block)} != "
        f"BD 0x{ref:08X}")


def _register_cases():
    for b in _ref.BLOCKS:
        for r in b.registers:
            yield pytest.param(b.name, r.name, r.offset, id=f"{b.name}.{r.name}")


@pytest.mark.parametrize("block,reg,offset", list(_register_cases()))
def test_register_offset_agrees_across_views(block, reg, offset):
    c_name, md_name = _names(block, reg)
    assert C_OFFS.get(block, {}).get(c_name) == offset, (
        f"{block}_{c_name} in platform_regs.h is "
        f"{C_OFFS.get(block, {}).get(c_name)}, RTL/PG says 0x{offset:02X}")
    assert PY_OFFS.get(block, {}).get(reg) == offset, (
        f"{block}_{reg} in tests/common/regmap.py is "
        f"{PY_OFFS.get(block, {}).get(reg)}, RTL/PG says 0x{offset:02X}")
    assert MD_OFFS.get(block, {}).get(md_name) == offset, (
        f"{block}.{md_name} in shell-regmap.md's generated table is "
        f"{MD_OFFS.get(block, {}).get(md_name)}, RTL/PG says 0x{offset:02X}")


# --------------------------------------------------------------------------- #
# 2. The hand-written contract prose agrees with the generated table.
#    This is the drift that generation does NOT close: a person maintains those
#    tables for their bit-field semantics, and an offset in them can rot.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("block", [b for b in BLOCK_NAMES if b not in NO_PROSE_TABLE])
def test_prose_table_agrees_with_generated(block):
    prose = MD_PROSE.get(block)
    assert prose, (
        f"shell-regmap.md has no hand-written `## {block} (0x...)` table. Either "
        f"write one (it carries the bit fields) or add {block} to "
        f"NO_PROSE_TABLE with a reason.")
    gen = {_names(block, r.name)[1]: r.offset for r in _ref.BY_NAME[block].registers}
    for name, off in prose.items():
        assert name in gen, (
            f"shell-regmap.md's {block} table documents `{name}` @0x{off:02X}, "
            f"which no longer exists in the hardware; the RTL decodes "
            f"{sorted(gen)}")
        assert gen[name] == off, (
            f"shell-regmap.md's {block} table puts `{name}` at 0x{off:02X}; the "
            f"RTL/PG says 0x{gen[name]:02X}")
    missing = sorted(set(gen) - set(prose))
    assert not missing, (
        f"{block} decodes {missing} in hardware but shell-regmap.md's table "
        f"does not document them — an undocumented register is an unusable one")


# --------------------------------------------------------------------------- #
# 3. Coverage is total, and derived — no opt-in list to forget.
# --------------------------------------------------------------------------- #
def test_every_bd_block_is_in_every_view():
    ref = set(BLOCK_NAMES)
    for view_name, view in (("platform_regs.h", set(C_BASES)),
                            ("tests/common/regmap.py", set(PY_BASES)),
                            ("shell-regmap.md", set(MD_BASES))):
        missing = ref - view
        assert not missing, f"{view_name} is missing blocks: {sorted(missing)}"


def test_the_fifteen_contract_blocks_are_all_covered():
    """The blocks the audit named. TOUCH (gated) is a superset, not a swap."""
    required = {"CLKRST", "DFXCTL", "HWICAP", "VPHY", "USD", "TELEM",
                "GENCHK", "SWDBB", "JTAGBB", "DBGBR", "UARTBR", "GPIO",
                "MMCM_DRP", "CLCD", "CLCDKVM"}
    assert required <= set(BLOCK_NAMES), (
        f"missing required blocks: {sorted(required - set(BLOCK_NAMES))}")


def test_clcdkvm_is_not_described_as_absent():
    """The specific lie this generation removed, pinned so it cannot come back.

    ``platform_regs.h`` and ``shell-regmap.md`` both said CLCDKVM was "RESERVED,
    NOT INSTANTIATED" while ``shell_bd.tcl`` instantiated ``clcd_kvm_0`` and
    assigned it 0x44AD_0000. Any tracked view that carries a base for it must
    therefore also stop calling it absent.
    """
    blk = _ref.BY_NAME["CLCDKVM"]
    assert blk.base == 0x44AD0000 and blk.gate is None, blk
    for path in (_PLATFORM_REGS_H, _REGMAP_MD):
        for m in re.finditer(r"[^\n]*CLCDKVM[^\n]*", path.read_text()):
            line = m.group(0)
            if "~~" in line or "was wrongly" in line:
                continue                      # struck-through history, left alone
            assert not re.search(r"NOT INSTANTIATED|not instantiated", line), (
                f"{path.name} still says CLCDKVM is not instantiated:\n  {line}")


# --------------------------------------------------------------------------- #
# 4. ONE OWNER PER PAGE — and no THIRD claimant can land.
#
# 0x44AE_0000 was claimed twice: fpga/shell/bd/touch_iic_add.tcl gave it to the
# touch AXI IIC, and the staged AXI-JTAG/UART carry pinned axi_jtag at the same
# base from fpga/shell/bd/axijtag_uart_carry.tcl and
# host/socket_harness/carry_across.py. Neither file could see the other; the
# touch script's own comment said so and left "move one of them" to whoever ran
# the mint. It cost nothing right up until both rode one mint.
#
# The map (tools/gen_regmap.py: the BD's assign_bd_address lines + RESERVATIONS)
# is now the single authority, and these three tests are what make that binding:
# the generator refuses to render a collision, and no tracked file may DECLARE a
# base in the region that the map does not know or does not agree with. A third
# claimant therefore cannot land quietly the way the second one did.
# --------------------------------------------------------------------------- #
#: How a base is DECLARED, per language. A reference to a base (firmware using
#: MPS3_GPIO_BASE, a doc quoting an address) is not a claim and is not scanned;
#: a file that writes `<OWNER>_BASE = <literal>` is claiming a page.
_DECL_PATTERNS = (
    # C:      #define MPS3_GPIO_BASE      0x44AA0000u
    re.compile(r"^\s*#define\s+MPS3_([A-Z0-9_]+?)_BASE\s+(0x[0-9A-Fa-f]{8})u?", re.M),
    # Python: GPIO_BASE = 0x44AA0000
    re.compile(r"^\s*([A-Z][A-Z0-9_]*?)_BASE\s*=\s*(0x[0-9A-Fa-f]{8})\b", re.M),
    # Tcl:    set ::MPS3_AXIJTAG_BASE 0x44AF0000   /   set MAP_GPIO_BASE 0x44AA0000
    re.compile(r"^\s*set\s+(?:::)?(?:MPS3_|MAP_)?([A-Z0-9_]+?)_BASE\s+"
               r"(0x[0-9A-Fa-f]{8})\b", re.M),
)

#: Files whose in-region literals are the MAP ITSELF, not a claim against it.
#: Scanning them would be circular, and they are covered by
#: check_generated_fresh.py + the agreement tests above instead.
_DECL_SCAN_SKIP = {
    "tools/gen_regmap.py",
    "host/pyverify/pyverify/regmap.py",
    "tests/firmware_logic/test_regmap_conformance.py",
}


def _resolve_owner(name: str) -> str:
    name = name.upper()
    name = _gen.BLOCK_ALIASES.get(name, name)
    return _gen.DECL_ALIASES.get(name, name)


def _tracked_files() -> list[str]:
    """Tracked files AND untracked-not-ignored ones.

    A third claimant arrives as a NEW file, which is untracked until it is
    committed. Scanning only `git ls-files` would let it pass every local run
    and first go red in CI, after the review that should have caught it —
    so `--others --exclude-standard` is in scope too (build output is ignored,
    and stays out).
    """
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=_REPO_ROOT, capture_output=True, text=True)
    if out.returncode != 0:                       # not a git checkout
        return []
    return [f for f in out.stdout.split("\0") if f]


def _scan_base_declarations(root: Path, files: list[str]):
    """[(relpath, owner, base)] for every base DECLARED inside the region."""
    found = []
    for rel in files:
        if rel in _DECL_SCAN_SKIP:
            continue
        p = root / rel
        if not p.is_file():
            continue
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        for pat in _DECL_PATTERNS:
            for m in pat.finditer(text):
                base = int(m.group(2), 16)
                if _ref.REGION_LO <= base < _ref.REGION_HI:
                    found.append((rel, _resolve_owner(m.group(1)), base))
        if rel.endswith(".tcl"):
            for m in _gen._ASSIGN_RE.finditer(text):
                base = int(m.group(1), 16)
                if _ref.REGION_LO <= base < _ref.REGION_HI:
                    found.append((rel, _resolve_owner(
                        re.split(r"[\s(]", m.group(4).strip())[0]
                        .replace("-", "_")), base))
    return found


DECLARATIONS = _scan_base_declarations(_REPO_ROOT, _tracked_files())


def test_page_scan_is_not_vacuous():
    """A scan that finds nothing would make the two tests below pass silently."""
    assert len(DECLARATIONS) >= 30, f"only {len(DECLARATIONS)} declarations found"
    files = {rel for rel, _o, _b in DECLARATIONS}
    assert len(files) >= 4, f"declarations found in only {sorted(files)}"
    for expect in ("fpga/shell/bd/shell_bd.tcl",
                   "fpga/shell/bd/touch_iic_add.tcl",
                   "firmware/common/platform_regs.h",
                   "host/socket_harness/carry_across.py"):
        assert expect in files, f"{expect} declares no base in the region"


def test_no_page_is_claimed_twice():
    """The BD blocks and the reservations occupy one page each, disjointly."""
    seen: dict[int, str] = {}
    for b in _ref.BLOCKS:
        if b.legacy_of:                 # SWDBB deliberately shares JTAGBB's page
            continue
        assert b.base not in seen, (
            f"0x{b.base:08X} claimed by both {seen[b.base]} and {b.name}")
        seen[b.base] = b.name
    for r in _ref.RESERVATIONS:
        assert r.base not in seen, (
            f"0x{r.base:08X} claimed by both {seen[r.base]} and the {r.name} "
            f"reservation — this is the TOUCH-vs-axi_jtag collision shape")
        seen[r.base] = r.name
    assert seen == _ref.PAGE_OWNER


def test_every_declared_base_in_the_tree_agrees_with_the_map():
    """No tracked file may claim a page the map does not give it.

    Both failure directions are real and both are the same defect: a NEW owner
    nobody added to the map (the third claimant), and a KNOWN owner written down
    at the wrong page (the copy that drifted).
    """
    bad = []
    for rel, owner, base in DECLARATIONS:
        want = _ref.BY_NAME[owner].base if owner in _ref.BY_NAME else next(
            (r.base for r in _ref.RESERVATIONS if r.name == owner), None)
        if want is None:
            bad.append(f"{rel}: `{owner}` claims 0x{base:08X}, but the map has "
                       f"no block or reservation called {owner}. Declare it in "
                       f"tools/gen_regmap.py (a BD assign_bd_address line, or a "
                       f"RESERVATIONS entry) — where the collision check can "
                       f"see it — before pinning a base for it here.")
        elif want != base:
            bad.append(f"{rel}: `{owner}` is declared at 0x{base:08X}; the map "
                       f"puts {owner} at 0x{want:08X}.")
    assert not bad, "\n".join(bad)


def test_reservation_pins_are_where_the_map_says():
    """Each reservation names every file that writes its literal; check them.

    This is the half a generator cannot do for itself: the staged carry's bases
    live in a Tcl BD script and a host module, neither of which is a generated
    view. Declaring them in RESERVATIONS and reading them back here is what
    makes those files QUOTE the map instead of restating it.
    """
    assert _ref.RESERVATIONS, "no reservations to check"
    for r in _ref.RESERVATIONS:
        assert r.pins, f"{r.name} reserves a page but names no file that pins it"
        for rel, sym in r.pins:
            p = _REPO_ROOT / rel
            assert p.is_file(), f"{r.name}: pins {rel}, which does not exist"
            text = p.read_text()
            m = re.search(r"(?:^|\s)" + re.escape(sym) +
                          r"\s*(?:=|\s)\s*(0x[0-9A-Fa-f]{8})\b", text, re.M)
            assert m, (f"{r.name}: {rel} no longer defines {sym} with a literal "
                       f"base — the map still says it does")
            assert int(m.group(1), 16) == r.base, (
                f"{r.name}: {rel}'s {sym} is {m.group(1)}, the map says "
                f"0x{r.base:08X}")


# --- mutation controls: a gate that cannot fail is not a gate ---------------- #
def _gen_in_copy(tmp_path, edit):
    """Copy the inputs the generator reads, mutate, and run it. -> CompletedProcess."""
    root = tmp_path / "repo"
    for d in ("tools", "fpga/shell/bd", "fpga/shell/ip", "fpga/ethernet",
              "firmware/common", "tests/common", "docs/contracts",
              "host/pyverify/pyverify", "host/socket_harness"):
        src = _REPO_ROOT / d
        if src.is_dir():
            shutil.copytree(src, root / d, dirs_exist_ok=True)
    edit(root)
    return subprocess.run([sys.executable, str(root / "tools/gen_regmap.py"),
                           "--repo", str(root), "--check"],
                          capture_output=True, text=True)


def test_mutation_two_bd_blocks_on_one_page_fails(tmp_path):
    def plant(root):
        p = root / "fpga/shell/bd/shell_bd.tcl"
        p.write_text(p.read_text().replace("-offset 0x44AC0000",
                                           "-offset 0x44AD0000"))
    r = _gen_in_copy(tmp_path, plant)
    assert r.returncode != 0, "two BD blocks on one page GENERATED cleanly"
    assert "0x44AD0000" in (r.stderr + r.stdout)


def test_mutation_a_reservation_on_a_bd_page_fails(tmp_path):
    """The collision the map exists to stop: exactly the TOUCH/axi_jtag shape."""
    def plant(root):
        p = root / "tools/gen_regmap.py"
        p.write_text(p.read_text().replace('("AXIJTAG", 0x44AF0000,',
                                           '("AXIJTAG", 0x44AE0000,'))
    r = _gen_in_copy(tmp_path, plant)
    assert r.returncode != 0, "a reservation on TOUCH's page GENERATED cleanly"
    assert "claimed TWICE" in (r.stderr + r.stdout)


def test_mutation_a_third_claimant_in_the_tree_is_caught(tmp_path):
    """A brand-new file taking a page: the scan must reject the owner name."""
    squat = tmp_path / "squatter.h"
    squat.write_text("#define MPS3_SQUATTER_BASE 0x44AE0000u\n")
    found = _scan_base_declarations(tmp_path, ["squatter.h"])
    assert found == [("squatter.h", "SQUATTER", 0x44AE0000)]
    assert "SQUATTER" not in _ref.BY_NAME
    assert not any(r.name == "SQUATTER" for r in _ref.RESERVATIONS), (
        "the scan found the planted claimant but the map knows it — the "
        "assertion in test_every_declared_base_in_the_tree_agrees_with_the_map "
        "would not have fired")


def test_mutation_a_known_owner_at_the_wrong_page_is_caught(tmp_path):
    drift = tmp_path / "drifted.py"
    drift.write_text("AXIJTAG_BASE = 0x44AE0000\n")
    found = _scan_base_declarations(tmp_path, ["drifted.py"])
    assert found == [("drifted.py", "AXIJTAG", 0x44AE0000)]
    want = next(r.base for r in _ref.RESERVATIONS if r.name == "AXIJTAG")
    assert want != 0x44AE0000
