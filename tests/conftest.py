"""tests/conftest.py

Keeps bare `pytest` collection limited to the pure-Python logic under tests/
(tests/common/, tests/integration/, tests/edge/, tests/firmware_logic/, and the
pure-logic files that sit alongside benches).

The per-block cocotb bench files declare `@cocotb.test()` coroutines that take a
`dut` handle supplied by a running simulator. pytest tries to collect any
`test_*` name it finds (they are still importable async functions) and fails
collection with "fixture 'dut' not found" — or, if the bench imports a cocotb
symbol the installed cocotb predates, with an ImportError that aborts the whole
run. Those benches are meant to be run via `make` in their own directory, under
cocotb's regression manager with a real simulator — see each block's Makefile and
tests/README.md — not via bare `pytest`.

WHY THIS IS DETECTED, NOT LISTED (changed 2026-07-24)
-----------------------------------------------------
This was a hand-maintained `collect_ignore_glob` list of directories. Every new
cocotb bench had to remember to add itself, and eight had not:

    clcd_kvm, clcd_kvm_e2e, micropython_boot, micropython_flash_boot,
    micropython_xip_boot, nanosoc_lcd, qspi_xip, uart2_rx_path

`make check` stage 3 was red as a result — tests/nanosoc_lcd imports
`SimTimeoutError`, which the installed cocotb 1.7.2 (python3.8) does not have, so
collection aborted before any test ran. A gate nobody can run green is a gate
that gets ignored, and the list would have drifted again.

So a bench is now identified by what it IS, not by being remembered: a test file
that imports cocotb at top level AND declares `@cocotb.test()`. Both conditions
are required, which preserves the distinction the old list handled by hand —
`board_gpio/` holds a real bare-pytest-collectible pure-logic file
(`test_gpio_mux_logic.py`) next to a bench (`test_board_gpio.py`), and only the
bench is excluded. A directory-level glob would have silently swallowed both;
that trap is why the old list special-cased board_gpio by exact filename.

Detection is per-file and cheap (a regex over each test_*.py at collection).
"""
import pathlib
import re

_TESTS_DIR = pathlib.Path(__file__).parent
_COCOTB_IMPORT = re.compile(r"^\s*(?:from\s+cocotb|import\s+cocotb)", re.M)


def _is_cocotb_bench(path: pathlib.Path) -> bool:
    """True for a simulator-driven bench: imports cocotb AND declares a
    @cocotb.test() coroutine. Requiring both keeps pure-logic helpers that merely
    reference cocotb (or benches' non-bench siblings) collectible."""
    try:
        src = path.read_text(errors="ignore")
    except OSError:
        return False
    return bool(_COCOTB_IMPORT.search(src)) and "@cocotb.test" in src


# Paths are relative to this conftest, which is what pytest expects.
collect_ignore = [
    str(p.relative_to(_TESTS_DIR))
    for p in sorted(_TESTS_DIR.glob("*/test_*.py"))
    if _is_cocotb_bench(p)
]
