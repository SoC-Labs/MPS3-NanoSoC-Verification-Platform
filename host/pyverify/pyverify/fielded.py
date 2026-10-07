"""``pyverify.fielded`` — the ONE resolver for "what is on the board".

Two facts drive every board-facing helper in this tree: the **static_id** of the
shell the MPS3 actually boots, and the **prod directory** holding the artefacts
keyed to it. Until this module they were re-typed at every call site, and the
copies had gone stale in the usual way:

* ``scripts/harness_gates/swap_check.py`` defaulted ``--prod`` to
  ``fpga/dfx/build_v3/prod``,
* ``scripts/mps3_swap_design.sh`` to ``fpga/dfx/build_v2enc/prod`` +
  ``fpga/dfx/build_clcd/prod``,

none of which is the fielded shell's prod dir and none of which exists in a
fresh clone. That is the same failure ``docs/FIELDED_SHELL.md`` was written to
end — "a fact asserted in nine places is a fact that will be wrong in eight of
them" — except in code, where the claims gate cannot see it (it scans
``.md``/``.txt`` only).

**This module only ever READS ``docs/FIELDED_SHELL.md``.** That file's table is
the authority and its shape is frozen by
``scripts/harness_gates/check_fielded_shell_claims.py``; nothing here writes it,
reformats it, or infers a row it does not carry. A missing row is an error, never
a default — a resolver that quietly invents ``lmb_kb`` would point every xsdb
read at an address the LMB decode *aliases* into a plausible wrong answer
(FIELDED_SHELL.md, "bug #4").

Seams, so nothing site-specific is baked in:

``MPS3_REPO_ROOT``
    repo root (default: derived from this file's location).
``MPS3_PROD_DIR``
    prod directory override — a mint that is BUILT but not yet FIELDED lives in
    its own build dir, and pointing a push at it is a legitimate, routine thing
    to do. The override wins over the doc; :meth:`Fielded.prod_dir_is_fielded`
    reports which one you got.
"""
from __future__ import annotations

import os
import re
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple, Union

from .overlay import Overlay, OverlayFile, OverlayManifest

__all__ = [
    "FieldedError",
    "Fielded",
    "load",
    "repo_root",
    "overlay_from_prod_dir",
    "overlay_from_files",
    "prod_dir_rms",
]


class FieldedError(Exception):
    """The fielded-shell facts could not be resolved (missing file, missing
    row, unparseable value, absent artefact directory)."""


#: Rows ``docs/FIELDED_SHELL.md`` must carry. Kept as a literal rather than
#: "whatever the table happens to have": a row silently disappearing is exactly
#: the drift this resolver exists to make impossible.
REQUIRED_ROWS: Tuple[str, ...] = (
    "minted", "fielded", "fielded_on", "fielded_by", "fielded_fw_flags", "lmb_kb",
)

_TABLE_OPEN = "<!-- FIELDED_SHELL_TABLE"
_TABLE_CLOSE = "<!-- /FIELDED_SHELL_TABLE -->"
_ROW = re.compile(r"^\|\s*(?P<key>[^|]+?)\s*\|\s*(?P<value>.*?)\s*\|\s*$")
#: ``NAME=VALUE`` tokens inside ``fielded_fw_flags``. Anchored on a word
#: boundary so the parenthetical fabric note (``(fabric `SHELL_TOUCH=1`)``) does
#: not smuggle its backtick into a flag name.
_FLAG = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)=([^\s`)]+)")


def repo_root() -> Path:
    """Repo root: ``$MPS3_REPO_ROOT`` if set, else derived from this file
    (``<root>/host/pyverify/pyverify/fielded.py``)."""
    env = os.environ.get("MPS3_REPO_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[3]


def _strip_md(value: str) -> str:
    """A table cell's value with markdown code fencing removed.

    The table spells some values in backticks (`` `0xA8C1C535` ``) and some
    bare (``2026-08-10``); both are the file's own established style and this
    resolver must not care which.
    """
    value = value.strip()
    if value.startswith("`") and "`" in value[1:]:
        return value[1:value.index("`", 1)]
    return value


@dataclass(frozen=True)
class Fielded:
    """The resolved contents of ``docs/FIELDED_SHELL.md``'s table, plus the
    artefact directory keyed to it."""

    minted: str
    fielded: str
    fielded_on: str
    fielded_by: str
    fielded_fw_flags: str
    lmb_kb: int
    root: Path
    doc_path: Path
    #: Set when ``MPS3_PROD_DIR`` overrode the doc-derived directory.
    prod_override: Optional[Path] = None

    # -- the facts ---------------------------------------------------------- #

    @property
    def static_id(self) -> int:
        """The FIELDED static_id as an int (what the board actually boots).

        Deliberately ``fielded``, never ``minted``: between a mint and a
        deployment the repo holds overlays keyed to a shell no board is
        running, which is a normal state (FIELDED_SHELL.md, "The two facts are
        not the same fact")."""
        return int(self.fielded, 16)

    @property
    def minted_id(self) -> int:
        return int(self.minted, 16)

    @property
    def in_sync(self) -> bool:
        """True when the minted overlays are keyed to the fielded shell."""
        return self.minted_id == self.static_id

    @property
    def diag_mailbox_base(self) -> int:
        """``lmb_kb*1024 - sizeof(mps3_diag_t)`` = ``- 0x100`` since diag v8 --
        derived here so no script re-types it, through
        :func:`pyverify.mailbox.diag_anchor`, the ONE place the anchor is
        computed (firmware/common/diag.h; tests/test_fielded.py holds it to
        diag.h's own struct size). Until 2026-09-24 this said ``- 0x80``, the
        v5..v7 size: at 128 KiB that is ``0x1FF80``, the MIDDLE of a v8 mailbox.

        The LMB address decode ALIASES, so a stale belief about the size does
        not error: it returns the magic word from the wrapped-around address
        (FIELDED_SHELL.md, "bug #4")."""
        from .mailbox import diag_anchor
        return diag_anchor(self.lmb_kb)

    def fw_flags(self) -> Dict[str, str]:
        """``fielded_fw_flags`` parsed into ``{NAME: VALUE}``.

        The row's parenthetical fabric note is prose, not a firmware flag, and
        is excluded."""
        text = self.fielded_fw_flags
        head = text.split("(", 1)[0]
        return {m.group(1): m.group(2) for m in _FLAG.finditer(head)}

    # -- the artefacts ------------------------------------------------------ #

    @property
    def prod_dir(self) -> Path:
        """Directory holding the artefacts keyed to :attr:`static_id`.

        ``$MPS3_PROD_DIR`` if set, else ``<root>/fielded/<fielded>/`` — the
        tracked copy (``fielded/0x…/MANIFEST.md5``) rather than a gitignored
        ``fpga/dfx/build*/prod`` that a fresh clone does not have."""
        if self.prod_override is not None:
            return self.prod_override
        return self.root / "fielded" / self.fielded

    @property
    def prod_dir_is_fielded(self) -> bool:
        """False when ``$MPS3_PROD_DIR`` pointed somewhere else — i.e. when the
        artefacts are NOT provably the fielded shell's."""
        return self.prod_override is None

    def require_prod_dir(self) -> Path:
        """:attr:`prod_dir`, or :class:`FieldedError` naming what is absent.

        Failing here beats failing three layers down inside a push with a bare
        "no such file"."""
        path = self.prod_dir
        if not path.is_dir():
            raise FieldedError(
                "no artefact directory for the fielded shell %s at %s "
                "(fielded/<static_id>/ is tracked; set MPS3_PROD_DIR to a "
                "built-but-not-yet-fielded prod dir if that is what you mean)"
                % (self.fielded, path)
            )
        return path

    def artefact(self, name: str) -> Path:
        """One file inside :meth:`require_prod_dir`, existence-checked."""
        path = self.require_prod_dir() / name
        if not path.is_file():
            raise FieldedError("no %s in %s" % (name, self.prod_dir))
        return path


def load(root: "Optional[Path]" = None) -> Fielded:
    """Parse ``<root>/docs/FIELDED_SHELL.md``'s frozen table."""
    root = Path(root) if root is not None else repo_root()
    doc = root / "docs" / "FIELDED_SHELL.md"
    if not doc.is_file():
        raise FieldedError(
            "no docs/FIELDED_SHELL.md under %s -- it is the single authority "
            "for what is on the board and cannot be substituted" % root
        )

    rows: Dict[str, str] = {}
    inside = False
    for line in doc.read_text().splitlines():
        if line.startswith(_TABLE_OPEN):
            inside = True
            continue
        if line.startswith(_TABLE_CLOSE):
            break
        if not inside:
            continue
        m = _ROW.match(line)
        if not m:
            continue
        key = _strip_md(m.group("key"))
        if key in ("fact", "---", ""):
            continue
        rows[key] = _strip_md(m.group("value"))

    missing = [k for k in REQUIRED_ROWS if not rows.get(k)]
    if missing:
        raise FieldedError(
            "docs/FIELDED_SHELL.md's table is missing row(s) %s -- a resolver "
            "that defaulted them would answer confidently and wrongly (see the "
            "file's own note on the aliasing LMB decode)" % ", ".join(missing)
        )

    try:
        lmb_kb = int(rows["lmb_kb"], 0)
    except ValueError as exc:
        raise FieldedError("lmb_kb %r is not an integer" % rows["lmb_kb"]) from exc
    for key in ("minted", "fielded"):
        try:
            int(rows[key], 16)
        except ValueError as exc:
            raise FieldedError("%s %r is not a hex static_id" % (key, rows[key])) from exc

    env_prod = os.environ.get("MPS3_PROD_DIR")
    return Fielded(
        minted=rows["minted"],
        fielded=rows["fielded"],
        fielded_on=rows["fielded_on"],
        fielded_by=rows["fielded_by"],
        fielded_fw_flags=rows["fielded_fw_flags"],
        lmb_kb=lmb_kb,
        root=root,
        doc_path=doc,
        prod_override=Path(env_prod) if env_prod else None,
    )


# --------------------------------------------------------------------------- #
# prod-dir manifests
#
# A DFX prod dir describes its RMs in two generated files rather than the
# per-RM ``manifest.json`` an overlay dir carries:
#
#   static_id.txt       one hex word
#   overlay_inputs.txt  rm_key rm_name rm_id partial_bin clearing_bin
#
# ``scripts/mps3_push.py`` used to parse those inline, alongside its own copy of
# the framing and the socket work. Reading them here means the ONE pusher
# (pyverify.pusher + pyverify.swap) drives both artefact shapes.
# --------------------------------------------------------------------------- #


def _read_prod_rows(prod_dir: Path) -> "Tuple[int, Dict[str, Tuple[int, str, str]]]":
    prod_dir = Path(prod_dir)
    sid_file = prod_dir / "static_id.txt"
    inputs = prod_dir / "overlay_inputs.txt"
    for path in (sid_file, inputs):
        if not path.is_file():
            raise FieldedError("%s is not a DFX prod dir: no %s" % (prod_dir, path.name))
    try:
        static_id = int(sid_file.read_text().strip(), 16)
    except ValueError as exc:
        raise FieldedError("%s does not hold a hex static_id" % sid_file) from exc

    rows: Dict[str, Tuple[int, str, str]] = {}
    for lineno, line in enumerate(inputs.read_text().splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 5:
            raise FieldedError(
                "%s:%d: expected 5 fields (rm_key rm_name rm_id partial clearing), "
                "got %d" % (inputs, lineno, len(parts))
            )
        key, name, rm_id, partial, clearing = parts
        try:
            rm_id_int = int(rm_id, 16)
        except ValueError as exc:
            raise FieldedError("%s:%d: rm_id %r is not hex" % (inputs, lineno, rm_id)) from exc
        rows[name] = (rm_id_int, partial, clearing)
        rows.setdefault(key, (rm_id_int, partial, clearing))
    return static_id, rows


def prod_dir_rms(prod_dir: Path) -> "Tuple[str, ...]":
    """The RM *names* a prod dir offers, in file order (for error messages and
    a ``--list``)."""
    _static_id, rows = _read_prod_rows(prod_dir)
    return tuple(n for n in rows if not n.startswith("rm_") or n in rows)


def _file_info(path: Path, *, label: str) -> "Tuple[int, int]":
    if not path.is_file():
        raise FieldedError("%s bitstream missing: %s" % (label, path))
    payload = path.read_bytes()
    if len(payload) % 4:
        raise FieldedError(
            "%s: %d bytes is not a whole number of config words"
            % (path, len(payload))
        )
    return len(payload), zlib.crc32(payload) & 0xFFFFFFFF


def overlay_from_prod_dir(prod_dir: Path, rm_name: str) -> Overlay:
    """Build an :class:`~pyverify.overlay.Overlay` for ``rm_name`` out of a DFX
    prod dir's ``static_id.txt`` + ``overlay_inputs.txt``.

    Lengths and CRCs are computed from the files on disk, never read from a
    note — the property ``scripts/mps3_push.py``'s docstring claimed and which
    must survive the move into the shared pusher.
    """
    prod_dir = Path(prod_dir)
    static_id, rows = _read_prod_rows(prod_dir)
    if rm_name not in rows:
        known = ", ".join(sorted(n for n in rows if not n.startswith("rm_")))
        raise FieldedError(
            "rm %r is not in %s/overlay_inputs.txt (has: %s)"
            % (rm_name, prod_dir, known or "<none>")
        )
    rm_id, partial_raw, clearing_raw = rows[rm_name]

    # overlay_inputs.txt records ABSOLUTE paths from the build host. When the
    # dir has been staged elsewhere (harness_regression.sh scps it to the hub)
    # the basenames still resolve inside it, so prefer a local hit and fall
    # back to the recorded path. A staged dir whose paths were NOT rewritten is
    # the exact case that made the hub-side push fail on every RM.
    def _resolve(raw: str) -> Path:
        local = prod_dir / Path(raw).name
        return local if local.is_file() else Path(raw)

    partial = _resolve(partial_raw)
    clearing = _resolve(clearing_raw)
    clearing_len, clearing_crc = _file_info(clearing, label="clearing")
    partial_len, partial_crc = _file_info(partial, label="partial")

    manifest = OverlayManifest(
        schema=1,
        static_id=static_id,
        rm_id=rm_id,
        rm_name=rm_name,
        clearing=OverlayFile(file=clearing.name, len=clearing_len, crc32=clearing_crc),
        partial=OverlayFile(file=partial.name, len=partial_len, crc32=partial_crc),
    )
    # Overlay resolves clearing/partial relative to `directory`; both files
    # resolved above live in (or were copied into) the prod dir.
    directory = partial.parent if partial.parent == clearing.parent else prod_dir
    return Overlay(directory=directory, manifest=manifest)


def overlay_from_files(
    *,
    static_id: int,
    rm_id: int,
    rm_name: str,
    clearing: "Union[str, Path]",
    partial: "Union[str, Path]",
) -> Overlay:
    """Build an :class:`~pyverify.overlay.Overlay` from four explicit files +
    ids — the "no manifest at all" escape hatch ``scripts/mps3_push.py``'s
    ``--static-id/--rm-id/--clearing/--partial`` form offers.

    Lengths and CRCs are computed here, from the files, so the escape hatch
    cannot be used to assert a length that is not true."""
    clearing = Path(clearing)
    partial = Path(partial)
    clearing_len, clearing_crc = _file_info(clearing, label="clearing")
    partial_len, partial_crc = _file_info(partial, label="partial")
    manifest = OverlayManifest(
        schema=1, static_id=static_id, rm_id=rm_id, rm_name=rm_name,
        clearing=OverlayFile(file=clearing.name, len=clearing_len, crc32=clearing_crc),
        partial=OverlayFile(file=partial.name, len=partial_len, crc32=partial_crc),
    )
    directory = partial.parent if partial.parent == clearing.parent else Path(".")
    return Overlay(directory=directory, manifest=manifest)
