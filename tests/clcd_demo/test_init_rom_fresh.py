"""test_init_rom_fresh.py -- the RM's panel init ROM must stay a fresh render of
the FIRMWARE's init table, and its address window must stay the firmware's.

Board-free and simulator-free: runs under `pytest tests` (root `make check-ci`
stage 3).

WHY IT MATTERS. `firmware/clcd/hx8347_init.c` is the one carefully-sourced
transcription of this panel's power-on sequence -- provenance in
`firmware/clcd/PANEL_PROVENANCE.md` -- and its own header flags MADCTL (0x16)
and PANEL_CTRL (0x36) as the values most likely to move at bring-up. The demo RM
must send the SAME sequence, because every CLCD-KVM handover hard-resets the
panel under it. Generating the ROM only closes that drift while the generated
file is current; a generator nobody re-runs is a comment. This is the gate that
re-runs it. Same shape as `scripts/harness_gates/check_generated_fresh.py`.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

import pytest

_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
_RM = _ROOT / "fpga" / "rp" / "clcd_demo"
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_RM))

import hx8347_table as fw  # noqa: E402
import card_model  # noqa: E402


def test_generated_rom_is_a_fresh_render():
    r = subprocess.run([sys.executable, str(_RM / "gen_init_rom.py"), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, (
        r.stdout + r.stderr +
        "\nfpga/rp/clcd_demo/clcd_demo_gen.sv's generated ROM no longer matches "
        "firmware/clcd/hx8347_init.c. Re-run:\n"
        "    python3 fpga/rp/clcd_demo/gen_init_rom.py")


def _rom_entries(src: str):
    """Pull the packed {op,val} words back out of the generated block."""
    m = re.search(r"localparam logic \[9:0\] INIT_ROM \[0:INIT_N-1\] = '\{(.*?)\};",
                  src, re.S)
    assert m, "INIT_ROM not found in clcd_demo_gen.sv"
    return [int(v, 16) for v in re.findall(r"10'h([0-9A-Fa-f]{3})", m.group(1))]


def test_rom_words_equal_the_firmware_table_entry_for_entry():
    """The `--check` above proves the FILE is a fresh render; this proves the
    render means what it should, by decoding the ROM back to (op, val)."""
    table, defines = fw.load(_ROOT)
    words = _rom_entries((_RM / "clcd_demo_gen.sv").read_text())
    assert len(words) == len(table)
    for i, (w, e) in enumerate(zip(words, table)):
        assert (w >> 8, w & 0xFF) == (e.op, e.val), (
            f"ROM[{i}] = op {w >> 8} val 0x{w & 0xFF:02X}, firmware table has "
            f"{e.op_name} 0x{e.val:02X}")


def test_madctl_carries_the_shipped_rotation():
    """The one deliberate deviation from the vendored ST table (hx8347_init.h:
    CLCD_ROTATE_180 default 1 -> 0xE0 ^ (MY|MX) = 0x20). If the panel is
    remounted and the firmware flips it, the RM must flip with it -- otherwise
    the test card is upside-down relative to the harness screen and a
    photograph of the proof argues with a photograph of the status page."""
    table, defines = fw.load(_ROOT)
    idx = [i for i, e in enumerate(table)
           if e.op == defines["HX_CMD"] and e.val == defines["HX_REG_MADCTL"]]
    assert idx, "the firmware table no longer writes MADCTL (0x16)"
    datum = table[idx[-1] + 1]
    assert datum.op == defines["HX_DAT"]
    assert datum.val == defines["HX_MADCTL_VALUE"] == 0x20


def test_window_registers_at_the_shipped_geometry_equal_the_firmware_values():
    """The RM recomputes the address-window END registers from its own PIX_W /
    PIX_H so a bench can shrink the frame. At the SHIPPED geometry that
    recomputation must land exactly on the firmware table's own values -- if it
    does not, the RM is painting into a window the panel was not set up for."""
    table, defines = fw.load(_ROOT)
    w, h = fw.frame_geometry(table, defines)
    assert (w, h) == (320, 240)
    for reg, fw_val in fw.window_writes(table, defines):
        assert card_model.window_value(reg, w, h) == fw_val, (
            f"window register 0x{reg:02X}: RM computes "
            f"0x{card_model.window_value(reg, w, h):02X}, firmware writes "
            f"0x{fw_val:02X}")


def test_the_parser_refuses_a_table_it_cannot_resolve(tmp_path):
    """A control for the generator's own input: if the firmware header stops
    defining a name the ROM depends on, generation must FAIL rather than fall
    back to a stale constant."""
    hdr = tmp_path / "firmware" / "clcd"
    hdr.mkdir(parents=True)
    (hdr / "hx8347_init.h").write_text(
        "#define CLCD_ROTATE_180 1\n#define HX_CMD 0u\n")
    (hdr / "hx8347_init.c").write_text(
        "const hx8347_entry_t hx8347_init[] = { { HX_CMD, 0x22 } };\n")
    with pytest.raises(ValueError, match="no longer defines"):
        fw.load(tmp_path)
