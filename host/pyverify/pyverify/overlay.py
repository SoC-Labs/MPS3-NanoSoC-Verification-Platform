"""Overlay manifest loading + validation — ``docs/contracts/overlay-manifest.md``.

An "overlay" is the UltraScale-mandatory artefact **triple**
``{clearing.bin, partial.bin, manifest.json}`` for one Reconfigurable
Module (RM). This module is the host-side reader/validator; it is pure
logic (JSON + filesystem + CRC32), so unlike the rest of ``host/`` it is
made **fully real** — no stubs, no hardware needed to exercise it.

Directory layout it expects (overlay-manifest.md "Directory layout"):

    overlay/
      <rm_name>/
        manifest.json
        <rm>.bin           # partial
        <rm>_clear.bin      # clearing
        <rm>.ltx            # optional
        <rm>_app.bin         # optional DUT firmware

Validation performed (overlay-manifest.md "Rules" + IMPLEMENTATION_PLAN.md
correction C2):

1. schema/shape — both ``clearing`` and ``partial`` keys are present (a
   manifest missing either is invalid per the contract).
2. ``static_id`` match against the currently-running shell (else reject —
   "partials are only valid against their exact static").
3. CRC32 (and size) of the actual files on disk against the manifest's
   recorded values, so a corrupted/truncated artefact is caught host-side
   *before* anything gets pushed over the wire.
"""
from __future__ import annotations

import json
import zlib
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "OverlayManifestError",
    "OverlayValidationError",
    "OverlayFile",
    "OverlayManifest",
    "Overlay",
    "compute_crc32",
]


class OverlayManifestError(Exception):
    """``manifest.json`` is missing, unparsable, or missing required keys."""


class OverlayValidationError(Exception):
    """A structurally-valid manifest failed validation against the files on
    disk (or against an expected ``static_id``)."""


def _parse_int(value: object, *, field: str) -> int:
    """Parse a manifest integer field.

    The contract's example manifest mixes plain hex (``"0xA1B2C3D4"``) and
    underscore-grouped hex (``"0x0000_0001"``); Python's ``int(x, 0)``
    accepts both (PEP 515 underscore grouping applies inside ``int()``,
    not just source literals). Plain JSON ints are accepted too, since the
    contract doesn't actually mandate the string form.
    """
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError as exc:
            raise OverlayManifestError(
                f"field {field!r}: cannot parse {value!r} as an integer"
            ) from exc
    raise OverlayManifestError(
        f"field {field!r}: expected int or hex string, got {type(value).__name__}"
    )


def compute_crc32(path: Path, *, chunk_size: int = 1 << 20) -> int:
    """CRC32 of a file's contents, streamed (no full-file read into memory).

    Matches the CRC the net-protocol.md bitstream header carries
    (``u32 crc32(payload)``) and what ``pusher.push`` recomputes before
    framing — both must agree with what's recorded in the manifest.
    """
    crc = 0
    with path.open("rb") as fh:
        while chunk := fh.read(chunk_size):
            crc = zlib.crc32(chunk, crc)
    return crc & 0xFFFFFFFF


@dataclass(frozen=True)
class OverlayFile:
    """One entry of the manifest's ``clearing``/``partial`` object."""

    file: str
    len: int
    crc32: int

    @classmethod
    def from_dict(cls, d: dict, *, label: str) -> "OverlayFile":
        try:
            file_name = d["file"]
            length = d["len"]
            crc = d["crc32"]
        except KeyError as exc:
            raise OverlayManifestError(
                f"{label}: missing required key {exc.args[0]!r}"
            ) from exc
        if not isinstance(file_name, str) or not file_name:
            raise OverlayManifestError(f"{label}.file must be a non-empty string")
        if not isinstance(length, int) or length < 0:
            raise OverlayManifestError(f"{label}.len must be a non-negative int")
        return cls(file=file_name, len=length, crc32=_parse_int(crc, field=f"{label}.crc32"))


@dataclass(frozen=True)
class OverlayManifest:
    """Parsed ``overlay/<rm>/manifest.json`` (overlay-manifest.md "Manifest
    (per RM)")."""

    schema: int
    static_id: int
    rm_id: int
    rm_name: str
    clearing: OverlayFile
    partial: OverlayFile
    ltx: str | None = None
    fw: str | None = None
    built: str | None = None
    vivado: str | None = None
    #: Identity of the static implementation these partials are BOUND to —
    #: ``BITSTREAM.CONFIG.USERID`` of the full static bitstream, reported by the
    #: hardware as ``REGISTER.USERCODE``. Optional (older manifests omit it).
    #: Unlike ``static_id`` — which is checked against a *provisioned file* on the
    #: target and so reads correct whatever is actually flown — this can be
    #: verified against the running device; see ``board_scripts/dfx_preflight.sh``.
    static_usercode: int | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "OverlayManifest":
        if not isinstance(d, dict):
            raise OverlayManifestError(
                f"manifest root must be a JSON object, got {type(d).__name__}"
            )
        missing = [k for k in ("schema", "static_id", "rm_id", "rm_name",
                                "clearing", "partial") if k not in d]
        if missing:
            raise OverlayManifestError(
                f"manifest missing required key(s): {missing} "
                "(clearing+partial must always ship together — "
                "overlay-manifest.md 'Rules')"
            )
        clearing_raw = d["clearing"]
        partial_raw = d["partial"]
        if not isinstance(clearing_raw, dict) or not isinstance(partial_raw, dict):
            raise OverlayManifestError(
                "manifest 'clearing' and 'partial' must both be objects"
            )
        return cls(
            schema=int(d["schema"]),
            static_id=_parse_int(d["static_id"], field="static_id"),
            rm_id=_parse_int(d["rm_id"], field="rm_id"),
            rm_name=str(d["rm_name"]),
            clearing=OverlayFile.from_dict(clearing_raw, label="clearing"),
            partial=OverlayFile.from_dict(partial_raw, label="partial"),
            ltx=d.get("ltx"),
            fw=d.get("fw"),
            built=d.get("built"),
            vivado=d.get("vivado"),
            static_usercode=(
                _parse_int(d["static_usercode"], field="static_usercode")
                if d.get("static_usercode") is not None else None
            ),
        )


@dataclass
class Overlay:
    """An overlay directory + its parsed manifest, with path resolution and
    on-disk validation.

    ``directory`` is the RM's overlay dir, e.g. ``overlay/nanosoc/``
    (overlay-manifest.md "Directory layout").
    """

    directory: Path
    manifest: OverlayManifest

    @classmethod
    def load(cls, directory: Path) -> "Overlay":
        directory = Path(directory)
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            raise OverlayManifestError(f"no manifest at {manifest_path}")
        try:
            raw = json.loads(manifest_path.read_text())
        except json.JSONDecodeError as exc:
            raise OverlayManifestError(
                f"invalid JSON in {manifest_path}: {exc}"
            ) from exc
        manifest = OverlayManifest.from_dict(raw)
        return cls(directory=directory, manifest=manifest)

    def clearing_path(self) -> Path:
        return self.directory / self.manifest.clearing.file

    def partial_path(self) -> Path:
        return self.directory / self.manifest.partial.file

    def ltx_path(self) -> Path | None:
        return self.directory / self.manifest.ltx if self.manifest.ltx else None

    def fw_path(self) -> Path | None:
        return self.directory / self.manifest.fw if self.manifest.fw else None

    def validate(self, *, expected_static_id: int | None = None) -> None:
        """Full validation per overlay-manifest.md + C2.

        Raises :class:`OverlayValidationError` (with every problem found,
        not just the first) if anything disagrees. Intended to be called
        before any push (see :mod:`pyverify.swap`).
        """
        errors: list[str] = []

        if expected_static_id is not None and self.manifest.static_id != expected_static_id:
            errors.append(
                "static_id mismatch: manifest="
                f"0x{self.manifest.static_id:08x} running shell="
                f"0x{expected_static_id:08x} (a shell rebuild invalidates "
                "every stored partial — overlay-manifest.md)"
            )

        for label, spec, path in (
            ("clearing", self.manifest.clearing, self.clearing_path()),
            ("partial", self.manifest.partial, self.partial_path()),
        ):
            if not path.is_file():
                errors.append(f"{label} file missing: {path}")
                continue
            actual_len = path.stat().st_size
            if actual_len != spec.len:
                errors.append(
                    f"{label} length mismatch: manifest={spec.len} "
                    f"actual={actual_len} ({path})"
                )
            actual_crc = compute_crc32(path)
            if actual_crc != spec.crc32:
                errors.append(
                    f"{label} crc32 mismatch: manifest=0x{spec.crc32:08x} "
                    f"actual=0x{actual_crc:08x} ({path})"
                )

        if self.manifest.ltx is not None:
            ltx = self.ltx_path()
            if ltx is not None and not ltx.is_file():
                errors.append(f"ltx referenced but missing: {ltx}")
        if self.manifest.fw is not None:
            fw = self.fw_path()
            if fw is not None and not fw.is_file():
                errors.append(f"fw referenced but missing: {fw}")

        if errors:
            raise OverlayValidationError("; ".join(errors))
