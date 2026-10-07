"""hm_fixtures.py -- Harness Manager's recorded clcd.c streams, as bench fixtures.

ORIGIN (copied verbatim, request H2 of HM docs/design/LCD_MIRROR.md §11):
  repo    github SoC-Labs/HarnessManager (local checkout: set LCDM_HM_ROOT)
  commit  aae155c "docs: LCD mirror design -- ..."
  path    tests/spikes/lcd_mirror_data/{boot,link_down,banner,regain}.stream.z,
          *.grid, fwstream.c  ->  tests/lcd_mirror/hm_fixtures/
  made by fwstream.c: the REAL firmware/clcd/clcd.c + hx8347_init.c of platform
          feat/linux-harness 4956d88 through the mock HAL. Cumulative on ONE
          panel: boot (reset + init + first paint) -> link_down (incremental,
          red rows) -> banner (DUT OSD) -> regain (clcd_regain: re-init + full
          repaint; the KVM's reset pulse is NOT in the stream, add it).
  format  .stream.z = zlib of 2-byte records (rs, byte): rs 0 index, 1 data,
          2 = a clcd_0 CTRL write (bit 2 RESET_N, bit 1 BL).
          .grid = clcd_preview-style "   rr |<40 chars>|" / "INVrr |...|".

This is a SECOND, independently captured recording of the same renderer (the
platform bench's own is tools/clcd_stream_dump.c): different capture code,
different seeded state, different scenarios, so the two recordings vouch for
each other. HM's golden model (tests/spikes/lcd_mirror_decoder.py,
Hx8347dShadow) is imported read-only from the HM checkout when present
(LCDM_HM_ROOT); it is never copied or modified here.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import zlib
from typing import List, Optional, Tuple

_HERE = pathlib.Path(__file__).resolve().parent
DATA = _HERE / "hm_fixtures"
HM_ROOT = pathlib.Path(os.environ.get("LCDM_HM_ROOT", "harness-manager-not-configured"))
SCENARIOS = ("boot", "link_down", "banner", "regain")
RS_CTRL = 2
#: HM's ctrl_reset_pulse(): the KVM's S_RST as the pads show it.
KVM_RESET = [(RS_CTRL, 0x01), (RS_CTRL, 0x07)]


def load_stream(name: str) -> List[Tuple[int, int]]:
    raw = zlib.decompress((DATA / f"{name}.stream.z").read_bytes())
    return list(zip(raw[0::2], raw[1::2]))


def load_grid(name: str) -> Tuple[List[int], List[int]]:
    grid: List[int] = []
    inv: List[int] = []
    for line in (DATA / f"{name}.grid").read_text().splitlines():
        if "|" not in line:
            continue
        head, body = line.split("|", 1)
        body = body[:40]
        assert len(body) == 40, (name, line)
        inv.append(1 if head.startswith("INV") else 0)
        grid += [ord(c) for c in body]
    assert len(inv) == 15 and len(grid) == 600, name
    return grid, inv


def feed_model(model, records):
    """Apply HM records to a hx8347_gram_model.GramModel (CTRL -> reset/BL)."""
    rst_n = True
    for rs, b in records:
        if rs == RS_CTRL:
            now = bool(b & 0x04)
            if rst_n and not now:
                model.panel_reset()
            rst_n = now
        elif rst_n:
            model.byte(rs, b)


def to_stim(records, stim, ctrl_state: dict, rst_cycles: int = 16):
    """HM records -> pad stimulus (pad_decoder.Stim), like lcdmir_bench.harness_stim."""
    for rs, b in records:
        if rs == RS_CTRL:
            rst_n, bl = (b >> 2) & 1, (b >> 1) & 1
            if ctrl_state.get("rst_n", 1) and not rst_n:
                stim.rst(rst_cycles)
            if bl != ctrl_state.get("bl", 0):
                stim.level(bl, ctrl_state.get("owner", 0))
            ctrl_state["rst_n"], ctrl_state["bl"] = rst_n, bl
        else:
            stim.byte(rs, b)
    return stim


_HM = None


def hm_decoder():
    """HM's golden model module, or None if the HM checkout is absent."""
    global _HM
    if _HM is None:
        path = HM_ROOT / "tests" / "spikes" / "lcd_mirror_decoder.py"
        if not path.exists():
            return None
        os.environ.setdefault("LCDM_PLATFORM", str(_HERE.parents[1]))
        sys.dont_write_bytecode = True            # never write into HM's tree
        spec = importlib.util.spec_from_file_location("hm_lcd_mirror_decoder", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["hm_lcd_mirror_decoder"] = mod
        spec.loader.exec_module(mod)
        _HM = mod
    return _HM
