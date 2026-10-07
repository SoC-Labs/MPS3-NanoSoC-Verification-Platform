"""dut_presence.py — decide whether a per-block cocotb bench can actually
run yet, for tests/<block>/test_*.py's `@cocotb.test(skip=...)` gating and
for tests/common/list_benches.py (used by the top-level tests/Makefile).

IMPORTANT NUANCE (found while writing these benches, 2026-07-04): by the
time this scaffolding was written, A1 had already landed Phase-0 RTL
*stubs* for all six blocks these benches target (fpga/ethernet/{mdio_phy_
model,gen_checker,rmii_phy_if,bridge}.sv, fpga/shell/ip/{dfx_ctl,clkrst}/
*.sv) — so a bare "does the file exist" check is no longer the right
readiness gate. Every one of those files:
  - has real, synthesizable-shaped port lists (safe to bind a cocotb DUT
    handle to today), but
  - has every register-write FSM / conversion / generator body left as
    `// TODO(A1)` with a tied-off placeholder output (e.g.
    `assign s_axi_rdata = '0;`, `assign mdio_i_o = 1'b1;`) and AXI-Lite
    aw/ar-ready that never asserts.

Running a bench against that today wouldn't fail cleanly — it would HANG
(cocotb `await RisingEdge(...)` loops waiting on a ready signal that is
permanently 0), which is worse than a clean skip. So readiness here checks
for the *absence* of the literal marker string every Phase-0 stub file
carries in its own header comment ("Phase 0 stub (A1)... Do NOT treat this
as working RTL") rather than mere file existence. Once A1 replaces a TODO
body with real logic (and, presumably, drops that header marker as part of
graduating the file), the corresponding bench's `@cocotb.test(skip=...)`
flips to run automatically — no manual edit needed in the common case.

TODO(A1/A6): if RTL graduation ends up signaled some other way (a separate
file, a version tag, a Makefile variable), update `rtl_ready()` instead of
relying on the marker string.
"""
from __future__ import annotations

import os

STUB_MARKER = "Phase 0 stub"


def rtl_present(rtl_dir: str, patterns=("*.v", "*.sv")) -> bool:
    """Bare existence check (any RTL file at all under `rtl_dir`). Kept
    separate from `rtl_ready()` below because "a file exists" and "the
    file is real, runnable logic" are different questions here — see the
    module docstring."""
    import glob

    if not os.path.isdir(rtl_dir):
        return False
    for pat in patterns:
        if glob.glob(os.path.join(rtl_dir, pat)):
            return True
    return False


def rtl_ready(rtl_dir: str, filenames, stub_marker: str = STUB_MARKER) -> bool:
    """True only if every file in `filenames` exists under `rtl_dir` AND
    none of them still contain `stub_marker`. See module docstring for why
    this is stricter than `rtl_present()`.
    """
    if not filenames:
        return False
    for fname in filenames:
        path = os.path.join(rtl_dir, fname)
        if not os.path.isfile(path):
            return False
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                if stub_marker in fh.read():
                    return False
        except OSError:
            return False
    return True
