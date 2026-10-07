"""tests/integration/test_qspi_regmap_parity.py

RTL-parity gate: the register offsets hard-coded in the host driver
``host/pyverify/pyverify/qspi_flash.py`` (``REG_*``) must match the authoritative
RTL register map ``$SOCLABS_AHB_QSPI_DIR/sys_desc/register_maps/apb_qspi_regs.yaml``
(itself derived from ``src/rdl/apb_qspi_regs.rdl`` / ``logical/apb_qspi_regs.v``).

Why this gate exists: this project has been bitten TWICE by a host bench that
hard-coded a register layout and validated it against a reference that shared the
same wrong assumption (the ethmac ``PACKETLEN``/``MIICOMMAND`` field traps) — so a
"reference-confirmed" pass proved nothing. ``qspi_flash.py``'s offsets and its
test double both carry the SAME constants, so nothing catches drift from the RTL.
This gate closes that by DERIVING the truth from the RTL register map, not from a
copy of the driver's own numbers.

Scope: register OFFSETS (the controller interface, which lives in the RTL). NOT
the SPI flash opcodes (JEDEC 0x9F, WREN 0x06, ULBPR 0x98, ...) — those are SST26
datasheet constants, not RTL, and belong to the flash part, not this controller.

Cross-repo: apb_qspi is a separate repo (SOCLABS_AHB_QSPI_DIR, as the fpga/dfx +
micropython Makefiles use). Absent in a bare checkout of this repo alone, so the
gate SKIPs cleanly when the map is not found — like the overlay round-trip. Pure
stdlib text scan; imports nothing, opens no device.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_QSPI_PY = _REPO_ROOT / "host" / "pyverify" / "pyverify" / "qspi_flash.py"

# Explicit REG_* (driver) -> QSPI_* (RTL map) name correspondence. Explicit, not a
# strip/prepend transform, because the names are NOT uniformly related:
# REG_AHB_SPI_SETUP is QSPI_AHB_SETUP in the RTL (same offset, different name).
# The gate asserts this map covers EVERY REG_* in the driver (see the completeness
# test), so a newly-added driver constant cannot silently escape the offset check.
_NAME_MAP = {
    "REG_CTRL":          "QSPI_CTRL",
    "REG_STATUS":        "QSPI_STATUS",
    "REG_SPI_CMD":       "QSPI_SPI_CMD",
    "REG_ADDR":          "QSPI_ADDR",
    "REG_RDATA0":        "QSPI_RDATA0",
    "REG_WDATA0":        "QSPI_WDATA0",
    "REG_AHB_SPI_SETUP": "QSPI_AHB_SETUP",
    "REG_CLK_DIV":       "QSPI_CLK_DIV",
}


def _yaml_path() -> Path | None:
    base = os.environ.get("SOCLABS_AHB_QSPI_DIR")
    if not base:
        return None
    p = Path(base) / "sys_desc" / "register_maps" / "apb_qspi_regs.yaml"
    return p if p.is_file() else None


def _driver_regs() -> dict[str, int]:
    """REG_* = <int> from qspi_flash.py (regex, so no pyverify import needed)."""
    out: dict[str, int] = {}
    for name, val in re.findall(r"^(REG_[A-Z0-9_]+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)",
                               _QSPI_PY.read_text(), re.M):
        out[name] = int(val, 0)
    return out


def _rtl_regs(yaml_path: Path) -> dict[str, int]:
    """QSPI_* register name -> byte offset, from the RTL-derived YAML (regex-paired
    '- name:'/'offset:', so pyyaml is not required)."""
    text = yaml_path.read_text()
    return {n: int(o, 0) for n, o in
            re.findall(r"-\s*name:\s*(QSPI_\w+)\s*\n\s*offset:\s*(0x[0-9A-Fa-f]+)", text)}


@pytest.fixture(scope="module")
def driver_regs():
    assert _QSPI_PY.is_file(), f"missing {_QSPI_PY}"
    return _driver_regs()


@pytest.fixture(scope="module")
def rtl_regs():
    p = _yaml_path()
    if p is None:
        pytest.skip("apb_qspi_regs.yaml not found (set SOCLABS_AHB_QSPI_DIR); "
                    "cross-repo, absent in a bare checkout")
    return _rtl_regs(p)


def test_driver_has_reg_constants(driver_regs):
    """Empty-gate guard: the regex actually found REG_* constants."""
    assert driver_regs, "no REG_* constants parsed from qspi_flash.py"


def test_name_map_covers_every_driver_reg(driver_regs):
    """Completeness: every REG_* in the driver must have a map entry, so a newly
    added driver constant cannot slip past the offset check unnoticed. Runs even
    when the RTL repo is absent -- it is a property of the driver + map alone."""
    unmapped = sorted(set(driver_regs) - set(_NAME_MAP))
    assert not unmapped, (
        f"REG_* constant(s) with no RTL name mapping: {unmapped}. Add them to "
        "_NAME_MAP (and confirm the offset against apb_qspi_regs.yaml)."
    )


def test_offsets_match_rtl(driver_regs, rtl_regs):
    """THE gate: every driver REG_* offset equals its RTL register's offset."""
    mismatches = []
    for reg, off in sorted(driver_regs.items()):
        rtl_name = _NAME_MAP[reg]      # guaranteed present by the completeness test
        assert rtl_name in rtl_regs, (
            f"{reg} maps to {rtl_name}, absent from the RTL map "
            f"(RTL has: {sorted(rtl_regs)})")
        if rtl_regs[rtl_name] != off:
            mismatches.append(
                f"{reg}=0x{off:X} but RTL {rtl_name}=0x{rtl_regs[rtl_name]:X}")
    assert not mismatches, (
        "qspi_flash.py register offsets DRIFTED from the RTL register map "
        "(the ethmac 'reference shares the bug' failure class): "
        + "; ".join(mismatches))
