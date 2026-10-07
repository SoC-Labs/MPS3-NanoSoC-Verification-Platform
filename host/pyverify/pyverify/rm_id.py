"""The ``rm_id`` encoding (v2) — host-side.

``rm_id`` is the 32-bit constant an RM's wrapper drives out on the ``rm_id``
partition pin; the shell reads it back at ``DFXCTL.RM_ID`` after a load and
firmware's ``step_verify()`` compares it against the value carried in the
pushed partial's own header. Since 2026-07-14 (docs/VERSIONING_PLAN.md §3.2)
it carries the design's **version** as well as its identity:

     31           24 23           16 15                            0
    +---------------+---------------+-------------------------------+
    |   ver major   |   ver minor   |         design_id[15:0]        |
    +---------------+---------------+-------------------------------+

    rm_id = (major << 24) | (minor << 16) | design_id

**The consequence that this module exists to make un-missable:** an rm_id now
*legitimately changes on every version bump*. ``nanosoc`` v1.0 is
``0x01000001``; ``nanosoc`` v1.1 will be ``0x01010001``. So:

* Code that asks **"which design is this?"** must key on
  :func:`design_id` (``rm_id & 0xFFFF``), which is stable across version bumps.
  Anything that exact-matches a full 32-bit rm_id to answer an *identity*
  question is a latent bug: it silently stops recognising the design the moment
  it is re-versioned. This is the host-side twin of firmware's
  ``CLCD_RM_DESIGN()`` (``firmware/clcd/clcd.h``), and the two MUST agree.
* Code that asks **"did the board come up with exactly the artefact I pushed?"**
  must compare the full 32-bit value — but should derive the expectation from
  the overlay manifest / ``rm_list.tcl``, not hard-code it.

The greybox carve-out: ``rm_greybox`` is held at design 0x0000 / v0.0 so its
rm_id stays exactly ``0x00000000``. The DFX decoupler clamps rm_id to
``DECOUPLED_VALUE 0x0`` while decoupled, and both firmware and
:func:`pyverify.edge._rm_id_indicates_loaded` read an all-zero id as "no RM
loaded" — so 0 is reserved and must never be a real design.
"""
from __future__ import annotations

from typing import NamedTuple

__all__ = [
    "DESIGN_ID_MASK",
    "GREYBOX_RM_ID",
    "RmVersion",
    "design_id",
    "version",
    "make_rm_id",
    "format_rm_id",
    "parse_rm_id",
    "is_greybox",
    "same_design",
]

#: ``rm_id & DESIGN_ID_MASK`` is the version-stable identity of a design.
DESIGN_ID_MASK = 0xFFFF

#: The inert tie-off. Reserved: "nothing loaded" / decoupler clamp value.
GREYBOX_RM_ID = 0x00000000


class RmVersion(NamedTuple):
    """The (major, minor) design version packed into ``rm_id[31:16]``.

    ``patch`` deliberately does NOT fit in rm_id and lives host-side only, in
    the manifest (VERSIONING_PLAN.md §3.2) — so this is a 2-tuple, not a 3-.
    """

    major: int
    minor: int

    def __str__(self) -> str:  # "1.0"
        return f"{self.major}.{self.minor}"


def parse_rm_id(value: object) -> int:
    """Coerce an rm_id to ``int``.

    Over the wire (``{"op":"ping"}`` -> ``rm_id``) and in manifests an rm_id is
    a hex *string*; in registers and headers it is an int. Accepts both, plus
    the underscore-grouped form (``"0x0100_0001"``) that overlay-manifest.md's
    worked example uses — ``int(x, 0)`` handles PEP-515 grouping.
    """
    if isinstance(value, bool):  # bool is an int subclass; never a valid rm_id
        raise TypeError("rm_id must be an int or hex string, not bool")
    if isinstance(value, int):
        return value & 0xFFFFFFFF
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("rm_id is empty")
        return int(text, 0) & 0xFFFFFFFF
    raise TypeError(f"rm_id must be an int or hex string, got {type(value).__name__}")


def design_id(rm_id: object) -> int:
    """``rm_id & 0xFFFF`` — the design's identity, **stable across versions**.

    Use this for every "is this the nanosoc design?" question. The firmware
    twin is ``CLCD_RM_DESIGN()``.
    """
    return parse_rm_id(rm_id) & DESIGN_ID_MASK


def version(rm_id: object) -> RmVersion:
    """The (major, minor) design version from ``rm_id[31:16]``."""
    raw = parse_rm_id(rm_id)
    return RmVersion(major=(raw >> 24) & 0xFF, minor=(raw >> 16) & 0xFF)


def make_rm_id(design: int, major: int, minor: int) -> int:
    """Pack ``(design_id, major, minor)`` into an rm_id — the inverse of
    :func:`design_id` + :func:`version`, and the same arithmetic as
    ``rm_id_of`` in ``fpga/dfx/rm_list.tcl``."""
    if not 0 <= design <= 0xFFFF:
        raise ValueError(f"design_id 0x{design:X} does not fit 16 bits")
    for label, v in (("major", major), ("minor", minor)):
        if not 0 <= v <= 0xFF:
            raise ValueError(f"version {label}={v} does not fit its 8-bit field")
    return ((major & 0xFF) << 24) | ((minor & 0xFF) << 16) | (design & 0xFFFF)


def format_rm_id(rm_id: object) -> str:
    """The canonical wire/manifest rendering: ``"0x0100001e"`` (lowercase,
    zero-padded to 8) — matches the shell's ``"0x%08x"`` and gen_manifest.py."""
    return "0x%08x" % parse_rm_id(rm_id)


def is_greybox(rm_id: object) -> bool:
    """True for the all-zero id: greybox / decoupled / nothing loaded."""
    return parse_rm_id(rm_id) == GREYBOX_RM_ID


def same_design(a: object, b: object) -> bool:
    """Do two rm_ids name the same DESIGN, ignoring version?

    The comparison that identity checks want. ``same_design(0x01000001,
    0x01010001)`` is True — nanosoc v1.0 and nanosoc v1.1 are both nanosoc.
    """
    return design_id(a) == design_id(b)
