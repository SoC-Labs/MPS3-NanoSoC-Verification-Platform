"""test_verify_shell_image_contract.py — hold firmware/platform/verify_shell_image.py
to the firmware tree it reads.

WHY THIS EXISTS
---------------
``verify_shell_image.py`` is the LAST board-free gate before a shell bitstream is
flashed. Everything it knows about the firmware is a name or a number typed into
it: symbols it greps out of the ELF with ``mb-nm``, the MMIO page immediates it
counts in the disassembly, and the ``--expect-xvc-target`` values it will accept.

None of that is checkable from inside the script. When a name goes stale the
script does not fail — it reports ``SKIP`` (a symbol that is not there) or exits 2
from argparse (a target it no longer offers). Both surface at MINT TIME, on a
booked board window, commits after the cause.

That is not hypothetical. ``--expect-xvc-target`` offered only ``{swdbb, dbgbr}``
for six weeks after the A6 SWD->JTAG cutover deleted ``swd_bb.sv`` and made
``jtagbb`` the live target. ``firmware/platform/Makefile:286-292`` says so in as
many words — "NEW WORK USES jtagbb" — while naming this very script as the reason
the ``swdbb`` spelling is kept. The build system and its acceptance gate
disagreed, in writing, and nothing compared them.

So compare them. Every check below derives BOTH sides from a file that the
firmware build actually reads, and fails if they differ.

WHAT THIS DOES NOT DO
---------------------
It does not run the script, build an ELF, or need Vitis. It is a NAME-LEVEL
contract: that the things the script looks for exist and are spelled the same
way the firmware spells them. Whether the script's *logic* is right is a separate
question, answered by running it on a real image.
"""
import importlib.util
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "firmware/platform/verify_shell_image.py"
PLATFORM_MK = ROOT / "firmware/platform/Makefile"
PLATFORM_REGS = ROOT / "firmware/common/platform_regs.h"

#: Generators that EMIT a firmware definition rather than carrying one. The baked
#: shell identity is written out by gen_greybox_blob.py:125, so no .c in the tree
#: defines it and a source-only search would call it missing.
GENERATORS = (ROOT / "firmware/platform/gen_greybox_blob.py",)


def _load_script():
    """Import verify_shell_image.py by path (it is a CLI, not a package member)."""
    spec = importlib.util.spec_from_file_location("verify_shell_image", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


VSI = _load_script()


# --------------------------------------------------------------------------- #
# 1. The XVC target vocabulary — THE ONE THAT WENT STALE.
# --------------------------------------------------------------------------- #
def _makefile_xvc_targets():
    """The XVC_TARGET values firmware/platform/Makefile accepts.

    Read from TWO independent places in that file, which must agree:

      * the ``ifeq ($(XVC_TARGET),<v>)`` arms — what the build actually branches
        on, i.e. the values that produce a distinct ``-D``;
      * the ``$(error ... is not one of: ...)`` message — what the build TELLS an
        operator when they get it wrong.

    An unset XVC_TARGET builds the Debug-Bridge path; the error message spells
    that ``<empty>=dbgbr``, and ``dbgbr`` is the name the script uses for it.
    Deriving from both catches the message drifting away from the arms, which is
    its own quiet defect: an operator who reads it would be misinformed.
    """
    txt = PLATFORM_MK.read_text()
    arms = set(re.findall(r"^\s*(?:else\s+)?ifeq\s*\(\$\(XVC_TARGET\),\s*([A-Za-z0-9_]+)\s*\)",
                          txt, re.M))
    m = re.search(r"XVC_TARGET=.*?is not one of:\s*([^.]*)\.", txt, re.S)
    assert m, ("no \"XVC_TARGET ... is not one of: ...\" $(error) in "
               f"{PLATFORM_MK} — the Makefile no longer rejects a bad value by "
               "name, so this test cannot read the vocabulary from it")
    listed = set()
    for tok in m.group(1).replace("\\", " ").split(","):
        tok = tok.strip()
        if not tok:
            continue
        # "<empty>=dbgbr" -> "dbgbr"
        listed.add(tok.split("=")[-1].strip())
    assert arms | {"dbgbr"} == listed, (
        f"firmware/platform/Makefile disagrees with ITSELF: the ifeq arms accept "
        f"{sorted(arms)} (plus unset=dbgbr) but its $(error) message tells the "
        f"operator {sorted(listed)}")
    return listed


def test_expect_xvc_target_choices_match_the_firmware_build():
    """--expect-xvc-target must offer exactly what the firmware build accepts.

    THE REGRESSION THIS CATCHES, exactly: the script's choices were
    ``("swdbb", "dbgbr")`` while the Makefile had accepted ``jtagbb`` since the
    A6 cutover. ``verify_shell_image.py --expect-xvc-target jtagbb`` then died in
    argparse with exit 2 — on the one image the platform actually ships — and the
    operator's options were to skip the check or to pass the wrong value.

    Checked BOTH ways. A missing value is the stale case above; an EXTRA value is
    worse, because the script would accept a target the firmware cannot build and
    report a pass for a flag that never existed.
    """
    assert set(VSI.XVC_TARGETS) == _makefile_xvc_targets(), (
        f"verify_shell_image.XVC_TARGETS = {sorted(VSI.XVC_TARGETS)} but "
        f"firmware/platform/Makefile accepts {sorted(_makefile_xvc_targets())}. "
        "A target the script does not offer cannot be asserted at mint time; a "
        "target it offers that the build rejects is a check that can never have "
        "run.")


def test_bitbang_targets_are_a_subset_of_the_offered_targets():
    """The bit-bang subset must not name a target the script would reject."""
    assert set(VSI.XVC_BITBANG_TARGETS) <= set(VSI.XVC_TARGETS)


# --------------------------------------------------------------------------- #
# 2. The MMIO page immediates, derived from platform_regs.h.
# --------------------------------------------------------------------------- #
def _base(name):
    m = re.search(rf"#define\s+{re.escape(name)}\s+0x([0-9A-Fa-f]+)u?\b",
                  PLATFORM_REGS.read_text())
    assert m, f"{name} not defined in {PLATFORM_REGS}"
    return int(m.group(1), 16)


def test_xvc_page_immediates_are_the_real_register_bases():
    """The two `imm <high16>` values must be the pages the firmware writes.

    The script counts MicroBlaze ``imm 17575`` / ``imm 17576`` sites in the
    disassembly to tell a bit-bang build from a Debug-Bridge build. Those decimal
    literals are the top halves of two addresses in platform_regs.h; if a block
    is ever re-based, the counts silently go to zero and the target check passes
    or fails for a reason that has nothing to do with the target.
    """
    assert VSI.XVC_BB_IMM == _base("MPS3_JTAGBB_BASE") >> 16
    assert VSI.XVC_DBGBR_IMM == _base("MPS3_DBGBR_BASE") >> 16
    assert VSI.XVC_BB_IMM != VSI.XVC_DBGBR_IMM, (
        "the bit-bang and Debug-Bridge pages have become the same page, so "
        "counting sites can no longer discriminate between the two targets")


def test_jtagbb_and_swdbb_really_do_share_one_page():
    """The claim that licenses the script's honesty about what it cannot see.

    ``check_elf`` reports a bit-bang pass with the caveat that ``jtagbb`` and
    ``swdbb`` are INDISTINGUISHABLE in the emitted code. That is only true while
    the two bases are equal. If they ever diverge, the caveat becomes a false
    statement AND the check becomes able to discriminate — so the script would
    need rewriting, not just re-wording.
    """
    assert _base("MPS3_JTAGBB_BASE") == _base("MPS3_SWDBB_BASE"), (
        "MPS3_JTAGBB_BASE and MPS3_SWDBB_BASE have diverged. "
        "verify_shell_image.py's bit-bang check says the two targets are the "
        "same machine code; that is now false and the check must be split.")


# --------------------------------------------------------------------------- #
# 3. The symbol contract.
# --------------------------------------------------------------------------- #
def _c_sources():
    """Firmware .c the PLATFORM image is built from.

    firmware/test/ is excluded deliberately: it holds host-gcc fakes
    (``fake_services.c`` defines ``swd_server_init``, among others) which are
    never linked into the MicroBlaze ELF this script inspects. Counting them
    would let a symbol that exists only in a test fake pass as present.
    """
    return [p for p in (ROOT / "firmware").rglob("*.c")
            if "firmware/test/" not in p.as_posix()]


def _defines(sym):
    """Is `sym` DEFINED (not merely declared) somewhere the platform ELF links?

    A definition in this tree starts at column 0 and does not end in ``;``; a
    prototype is the same line with a semicolon (``hwicap_writer.c:65`` vs
    ``:80``). Conditional compilation is fine — ``hwicap_fifo_drain`` and
    ``hwicap_lite_write`` live in opposite arms of one ``#if MPS3_HWICAP_FIFO``
    and exactly one is ever in the image, but both must EXIST as names.
    """
    fn = re.compile(rf"^[A-Za-z_].*\b{re.escape(sym)}\s*\(", re.M)
    obj = re.compile(rf"^[A-Za-z_].*\b{re.escape(sym)}\s*(?:=|\[)", re.M)
    for p in _c_sources():
        txt = p.read_text(errors="replace")
        for rx in (fn, obj):
            for m in rx.finditer(txt):
                line = txt[m.start():txt.find("\n", m.start())]
                if not line.rstrip().endswith(";"):
                    return f"{p.relative_to(ROOT)}"
    for g in GENERATORS:                      # emitted, not carried
        if g.is_file() and re.search(rf"\b{re.escape(sym)}\b", g.read_text()):
            return f"{g.relative_to(ROOT)} (generated)"
    return None


@pytest.mark.parametrize("sym", VSI.GREPPED_SYMS)
def test_every_symbol_the_gate_greps_for_exists(sym):
    """A name the script looks for and the firmware no longer defines reports
    SKIP, and a SKIP is not a pass.

    The script's own docstring makes this rule explicit for
    ``coordinator_main_loop`` — "probing for it produces a permanent, meaningless
    SKIP" — and then had two names in that state anyway:
    ``hwicap_fifo_write_words``, which survives only in two comments and in the
    script, and ``swd_server_init``, which ``coordinator.c:98`` marks dormant in
    favour of ``jtag_server_init()`` and which ``--gc-sections`` is therefore
    entitled to strip. Both are fixed; this keeps them fixed.
    """
    where = _defines(sym)
    assert where, (
        f"verify_shell_image.py greps the ELF for '{sym}', but nothing under "
        f"firmware/ (excluding the host-gcc fakes in firmware/test/) defines it "
        f"and no generator emits it. On a real image that probe can only ever "
        f"report SKIP — which the script prints as 'not a pass' and an operator "
        f"under time pressure reads as 'fine'.")


def test_the_symbol_list_is_not_empty():
    """Guard against a vacuous parametrization (an empty GREPPED_SYMS would make
    every case above disappear silently rather than fail)."""
    assert len(VSI.GREPPED_SYMS) >= 6
    assert VSI.STATIC_ID_SYM in VSI.GREPPED_SYMS
