"""Tests for :mod:`pyverify.fielded` — the ONE resolver for "what is on the board".

Before this module the answer lived in nine places (docs/FIELDED_SHELL.md's own
"Why this file exists" section lists them) and, in the *host* tree, in a set of
hardcoded build-directory defaults that had gone dead:
``scripts/harness_gates/swap_check.py`` defaulted ``--prod`` to
``fpga/dfx/build_v3/prod`` and ``scripts/mps3_swap_design.sh`` to
``fpga/dfx/build_v2enc/prod`` — neither of which is the fielded shell's prod
dir, and neither of which exists in a fresh clone.

``docs/FIELDED_SHELL.md`` is the authority (its table shape is FROZEN by
``scripts/harness_gates/check_fielded_shell_claims.py``); this module only ever
READS it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from pyverify import fielded

_REPO = Path(__file__).resolve().parents[3]


def _row(key: str) -> str:
    """One row of the real authority, parsed WITHOUT the resolver under test.

    This used to be a re-typed literal (`assert f.fielded == "0xA8C1C535"`), and
    on 2026-09-22 the board was re-fielded and these tests went red -- a 16th
    copy of the exact fact this module exists to have exactly ONE of. It
    survived the sweep because this file is deliberately EXEMPT from
    `check_fielded_shell_claims.py` (the synthetic trees below need literals),
    so the gate could never see it. The exemption is right; the literal was not.

    An independent parse still proves what the test is for -- that the resolver
    READS the file correctly -- and cannot go stale at the next mint.
    """
    text = (_REPO / "docs" / "FIELDED_SHELL.md").read_text()
    m = re.search(r"\|\s*`%s`\s*\|\s*`?([^|`]+?)`?\s*\|" % key, text)
    assert m, "no `%s` row in docs/FIELDED_SHELL.md" % key
    return m.group(1).strip()


# --------------------------------------------------------------------------- #
# Against the real repo (the file the gate freezes)
# --------------------------------------------------------------------------- #


def test_resolves_the_real_repo_table() -> None:
    f = fielded.load()
    assert f.fielded == _row("fielded")
    assert f.minted == _row("minted")
    # `static_id` follows `fielded` (the board), never `minted` (the overlay
    # set). Those two rows are EQUAL today, so this pair cannot discriminate
    # between them here -- test_static_id_follows_fielded_not_minted below is
    # what proves it, on a synthetic table that can make them differ.
    assert f.static_id == int(_row("fielded"), 16)
    assert f.lmb_kb == int(_row("lmb_kb"))
    # the fourth fact: the mailbox anchor derived, never re-typed (v8+: 0x100)
    assert f.diag_mailbox_base == f.lmb_kb * 1024 - 0x100


def test_fw_flags_parse_into_a_mapping() -> None:
    f = fielded.load()
    flags = f.fw_flags()
    assert flags["CLCD"] == "1"
    assert flags["TOUCH"] == "1"
    # the parenthetical fabric note must not become a flag
    assert "(fabric" not in flags


def test_prod_dir_is_the_fielded_artefact_dir_and_exists() -> None:
    f = fielded.load()
    assert f.prod_dir.name == _row("fielded")
    assert f.prod_dir.parent.name == "fielded"
    assert f.prod_dir.is_dir(), "fielded/<static_id>/ is tracked; it must exist"
    assert (f.prod_dir / "static_id.txt").is_file()


def test_prod_dir_static_id_txt_agrees_with_the_doc() -> None:
    """The two halves of the resolver must not be able to disagree silently:
    fielded/<id>/static_id.txt is generated, the doc row is written by hand."""
    f = fielded.load()
    on_disk = (f.prod_dir / "static_id.txt").read_text().strip()
    assert int(on_disk, 16) == f.static_id


# --------------------------------------------------------------------------- #
# Against synthetic trees — the failure modes
# --------------------------------------------------------------------------- #


def _tree(root: Path, rows: str, *, prod: str | None = "0xDEADBEEF") -> Path:
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "FIELDED_SHELL.md").write_text(
        "# heading\n\n"
        "<!-- FIELDED_SHELL_TABLE (parsed by scripts/harness_gates/"
        "check_fielded_shell_claims.py) -->\n\n"
        "| fact | value |\n|---|---|\n" + rows +
        "\n<!-- /FIELDED_SHELL_TABLE -->\n"
    )
    if prod:
        (root / "fielded" / prod).mkdir(parents=True, exist_ok=True)
        (root / "fielded" / prod / "static_id.txt").write_text(prod + "\n")
    return root


_GOOD_ROWS = (
    "| `minted` | `0xDEADBEEF` |\n"
    "| `fielded` | `0xDEADBEEF` |\n"
    "| `fielded_on` | 2026-01-01 |\n"
    "| `fielded_by` | abc1234 |\n"
    "| `fielded_fw_flags` | `A=1 B=0 C=1` (fabric `X=1`) |\n"
    "| `lmb_kb` | `256` |\n"
)


def test_synthetic_tree_round_trips(tmp_path: Path) -> None:
    f = fielded.load(_tree(tmp_path, _GOOD_ROWS))
    assert f.static_id == 0xDEADBEEF
    assert f.lmb_kb == 256
    assert f.diag_mailbox_base == 256 * 1024 - 0x100
    assert f.fw_flags() == {"A": "1", "B": "0", "C": "1"}


def test_static_id_follows_fielded_not_minted(tmp_path: Path) -> None:
    """The discriminating case, which the real table cannot supply today.

    `minted` is what the overlay set is keyed to; `fielded` is what the board
    boots. Between a mint and a deployment they differ -- which is exactly when
    a resolver that silently read the wrong row would send a push at a shell
    that is not there. The real rows agreed on 2026-09-22, so this proof has to
    be built rather than observed.
    """
    rows = _GOOD_ROWS.replace("| `minted` | `0xDEADBEEF` |",
                              "| `minted` | `0xFEEDFACE` |")
    f = fielded.load(_tree(tmp_path, rows))
    assert f.minted == "0xFEEDFACE"
    assert f.fielded == "0xDEADBEEF"
    assert f.static_id == 0xDEADBEEF, "static_id read `minted`, not `fielded`"
    assert f.prod_dir.name == "0xDEADBEEF"


def test_missing_doc_names_the_file(tmp_path: Path) -> None:
    with pytest.raises(fielded.FieldedError) as exc:
        fielded.load(tmp_path)
    assert "FIELDED_SHELL.md" in str(exc.value)


def test_missing_row_is_named_not_defaulted(tmp_path: Path) -> None:
    rows = _GOOD_ROWS.replace("| `lmb_kb` | `256` |\n", "")
    with pytest.raises(fielded.FieldedError) as exc:
        fielded.load(_tree(tmp_path, rows))
    assert "lmb_kb" in str(exc.value)


def test_missing_prod_dir_is_reported_not_invented(tmp_path: Path) -> None:
    """A resolver that returns a path to a directory that is not there would
    push the failure all the way down to a confusing 'no such file' inside a
    push. Ask for it explicitly and it must refuse here."""
    f = fielded.load(_tree(tmp_path, _GOOD_ROWS, prod=None))
    assert f.prod_dir.name == "0xDEADBEEF"          # the path is still computed
    with pytest.raises(fielded.FieldedError) as exc:
        f.require_prod_dir()
    assert "0xDEADBEEF" in str(exc.value)


def test_env_override_points_the_prod_dir_elsewhere(tmp_path: Path, monkeypatch) -> None:
    """A mint that has been built but not yet fielded lives in its own build
    dir; MPS3_PROD_DIR is the seam for that, and it must win over the doc."""
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setenv("MPS3_PROD_DIR", str(other))
    f = fielded.load(_tree(tmp_path, _GOOD_ROWS))
    assert f.prod_dir == other
    f.require_prod_dir()                            # exists -> no raise


# --------------------------------------------------------------------------- #
# prod-dir manifest reading (what mps3_push.py used to do inline)
# --------------------------------------------------------------------------- #


def test_overlay_from_prod_dir_builds_a_validatable_overlay(tmp_path: Path) -> None:
    import zlib
    prod = tmp_path / "prod"
    prod.mkdir()
    clearing = b"\xc1" * 64
    partial = b"\xb2" * 128
    (prod / "regdemo_b_clear.bin").write_bytes(clearing)
    (prod / "regdemo_b.bin").write_bytes(partial)
    (prod / "static_id.txt").write_text("0xA1B2C3D4\n")
    (prod / "overlay_inputs.txt").write_text(
        "# rm_key rm_name rm_id partial_bin clearing_bin\n"
        "rm_regdemo_b regdemo_b 0x010000B2 %s %s\n"
        % (prod / "regdemo_b.bin", prod / "regdemo_b_clear.bin")
    )
    ov = fielded.overlay_from_prod_dir(prod, "regdemo_b")
    assert ov.manifest.static_id == 0xA1B2C3D4
    assert ov.manifest.rm_id == 0x010000B2
    assert ov.manifest.rm_name == "regdemo_b"
    assert ov.manifest.clearing.crc32 == zlib.crc32(clearing) & 0xFFFFFFFF
    assert ov.manifest.partial.len == len(partial)
    ov.validate(expected_static_id=0xA1B2C3D4)      # must not raise


def test_overlay_from_prod_dir_names_an_unknown_rm(tmp_path: Path) -> None:
    prod = tmp_path / "prod"
    prod.mkdir()
    (prod / "static_id.txt").write_text("0xA1B2C3D4\n")
    (prod / "overlay_inputs.txt").write_text("rm_a a 0x1 /nope/a.bin /nope/a_clear.bin\n")
    with pytest.raises(fielded.FieldedError) as exc:
        fielded.overlay_from_prod_dir(prod, "regdemo_b")
    assert "regdemo_b" in str(exc.value)
    assert "a" in str(exc.value)                    # lists what IS there


def _diag_h_struct_bytes() -> "tuple[int, int]":
    """(MPS3_DIAG_VERSION, sizeof(mps3_diag_t)) from firmware/common/diag.h, the
    size summed over its X-macro rows by the generator that renders every other
    reader (tools/gen_diag.py) -- not restated here."""
    import importlib.util
    import sys
    root = Path(__file__).resolve().parents[3]
    gen = root / "tools" / "gen_diag.py"
    diag_h = root / "firmware" / "common" / "diag.h"
    if not gen.is_file() or not diag_h.is_file():
        pytest.skip("not in the platform tree (no tools/gen_diag.py / diag.h)")
    sys.path.insert(0, str(gen.parent))
    spec = importlib.util.spec_from_file_location("gen_diag_for_fielded", gen)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    text = diag_h.read_text()
    version = int(re.search(r"#define\s+MPS3_DIAG_VERSION\s+(\d+)u?", text).group(1))
    return version, 4 * sum(f.words for f in mod.parse_fields(text))


@pytest.mark.parametrize("lmb_kb", [128, 256, 1024])
def test_diag_mailbox_base_is_diag_h_s_anchor(tmp_path: Path, lmb_kb: int) -> None:
    """The anchor follows diag.h: LMB end - sizeof(mps3_diag_t). A diag.h that
    grows the struct again fails here instead of silently mis-pointing every
    script that asks fielded for the base."""
    version, size = _diag_h_struct_bytes()
    assert version >= 8 and size == 0x100, (version, size)
    f = fielded.load(_tree(tmp_path, _GOOD_ROWS.replace("`256`", f"`{lmb_kb}`")))
    assert f.diag_mailbox_base == lmb_kb * 1024 - size
    # negative control: the pre-2026-09-24 (v5..v7) anchor lands INSIDE a v8 mailbox
    old = lmb_kb * 1024 - 0x80
    assert f.diag_mailbox_base < old < f.diag_mailbox_base + size
