"""tests/firmware_logic/test_firmware_host_gcc_harness.py

Shells out to W3's real `firmware/test/` host-gcc harness (build + run) and
asserts every binary passes. This directory did **not** exist at the start
of this task (confirmed via `find firmware -iname "*test*"`, 2026-07-04)
but landed mid-session, complete with its own `Makefile`:
`firmware/test/{test_crc32.c, test_bitstream_header.c,
test_ovlstore_header.c, test_swap_fsm_transitions.c, test_config_agent.c,
test_swap_fsm_hw.c, mock_regs.{c,h}, fake_config_agent.{c,h},
fake_overlay_store.{c,h}}` -- pure-logic units (crc32, bitstream/ovlstore
header pack-unpack, the swap-FSM pure transition table, config_agent
validation) plus one HAL-mocked integration binary
(`test_swap_fsm_hw`, swap_fsm.c's real impure `step_*()` functions against
`mock_regs.c`'s in-memory register file instead of real MMIO --
`-DMPS3_HAL_MOCK`, see `firmware/common/platform_regs.h`'s HAL-split
comment).

This file is a thin wrapper: it does NOT reimplement the build (unlike an
earlier draft of this file, written before the Makefile existed, which
tried to resolve `firmware/common/*.c` link dependencies generically via
`nm`) -- `firmware/test/Makefile`'s own `test` target is the single source
of truth for "how to build and run this," so the right thing here is to
`make clean && make test` and check the result, not maintain a second,
parallel build recipe that could drift from the real one.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FW_TEST_DIR = _REPO_ROOT / "firmware" / "test"

_MAKE = shutil.which("make")
_CC = shutil.which("gcc") or shutil.which("cc")

# The binaries firmware/test/Makefile's own $(TESTS) variable names, as of
# this writing -- re-asserted below (not just trusted) so a future Makefile
# edit that silently drops a binary from $(TESTS) without anyone noticing
# is caught here, not just "make test" happening to still exit 0 for fewer
# binaries than expected.
_EXPECTED_TEST_BINARIES = (
    "test_crc32",
    "test_bitstream_header",
    "test_ovlstore_header",
    "test_net_proto_json",         # W-JSON: control-line tokenizer + per-op encoders
    "test_swap_fsm_transitions",
    "test_config_agent",
    "test_swap_fsm_hw",
    "test_swap_fsm_faults",        # fail-OPEN/fail-STUCK closures: stuck HWICAP -> SWAP_FAILED, DECOUPLE/RELEASE confirm timeouts
    "test_hwicap_byte_lane",       # R3/I18: ICAP words packed byte-exact big-endian (native memcpy would byte-swap)
    "test_coordinator_dispatch",   # W-JSON: line-in -> response-out for every verb
    "test_macgen",                 # I10 tail: macgen verb drives GENCHK CTRL/INJECT + counters
    "test_net_linebuf",            # W-NET-SEAM: seam helpers + fake_net_if backend spec
    "test_config_agent_net",       # W-NET-SEAM: real TFTP/raw-6910 receive sessions + pair staging
    "test_config_agent_windowed",  # OTW pivot: app-level windowed 6910 flow control (grant on sink-drain, fail-closed)
    "test_uart_over_eth",          # W-NET-SEAM: UARTBR relay (destructive reads, tx_full, SWO_CFG, hard-send-error drop)
    "test_swd_server",             # W-NET-SEAM: remote_bitbang drain -> SWDBB DRIVE/SAMPLE (+ hangup/send-error drop)
    "test_swap_e2e_net",           # W-NET-SEAM acceptance: full swap through the seam e2e
    "test_ctrl_reap",              # reap-before-refuse: 6900 close-then-reconnect x200, parked client reaped, live client still refuses
    "test_ctrl_reap_noprobe",      # negative control for the above: no probe -> the reconnect IS refused (proves the assertions have teeth)
    "test_swap_qspi_free",         # QSPI-free config: CLEARING_RAM+arena >= nanosoc's 117,684 B clearing -> full a->b->a swap-away, 0 flash writes, protection never unlocked
    "test_swap_qspi_free_tinyarena", # negative control for the above: arena too small -> swap-away fails CLOSED at STREAM_CLEARING, still 0 flash writes (proves the zero-write assertion has teeth)
    "test_swap_icap_direct",       # Path 3: large partial streamed straight to HWICAP.WF (MSB-first, swap-armed-first guard)
    "test_tftp_large_swap",        # R5: >STAGING_BYTES over TFTP -> QSPI clearing/partial + ICAP-direct sinks; mid-transfer ERROR re-arms protection
    "test_smsc911x",               # W-SMSC: LAN9220 driver vs chip model (Apache-2.0-derived) + reset/CSR timeout + TX-space
    "test_usd",                    # D13-L2: user-microSD driver vs fake_usd (no card -> zero SPI traffic)
    "test_usd_tinybudget",         # D13-L2: same, tiny per-poll budgets (every phase resumes across polls)
    "test_ovlstore_sd",            # D13-L3: the store on a block device (0xDA discovery, ping-pong header, static_id gate)
    "test_ovlstore_sd_tiny",       # D13-L3: same, 1-block ops / small CRC budget
    "test_config_agent_commit",    # D13: commit re-push sink (write_some back-pressure)
    "test_config_agent_commit_windowed", # D13: same over windowed 6910
    "test_usd_dispatch",           # D13: usd verb + v0.13 commit through the coordinator
    "test_usd_boot",               # D13: power-on load once per configuration (PB1 skip, stale, respawn)
    "test_clcd_usd",               # D13: CLCD row 4 USD field, every state string
    "test_touch_kvm",              # touch setup when the DUT owns the panel at the first poll
    "test_xvc_server",             # W-VITIS: xvc_server XVC engine vs behavioral DBGBR fake
)

pytestmark = [
    pytest.mark.skipif(not _FW_TEST_DIR.is_dir(), reason="firmware/test/ not present (W3 host-gcc harness not landed yet)"),
    pytest.mark.skipif(_MAKE is None, reason="no `make` on PATH -- cannot drive firmware/test/Makefile"),
    pytest.mark.skipif(_CC is None, reason="no gcc/cc on PATH -- firmware/test/Makefile needs one"),
]


#: A HANG GUARD, not a performance budget -- which is why it is generous.
#: `make clean && make test` rebuilds ~60 host binaries serially. On an idle
#: build host that is ~25 s of CPU (18 s user + 7 s sys, measured 2026-09-16)
#: but TWO MINUTES of wall clock as soon as anything else is running, because
#: the work is dominated by waiting, not computing. The previous flat 120 s
#: therefore had no margin at all: this test failed twice on 2026-09-16 on a
#: box that was also building firmware, each time as a bare TimeoutExpired that
#: reads exactly like a broken build, and it is in `check-ci`, so a noisy
#: shared runner could do the same to CI. Raise it and let a real hang be
#: caught by something that cannot be confused with load.
_MAKE_TIMEOUT_S = 900


def _run_make(target: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["make", target],
            cwd=str(_FW_TEST_DIR),
            capture_output=True, text=True, timeout=_MAKE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            "`make %s` in %s did not finish within %d s. That is %dx the "
            "measured idle wall time, so treat it as a HANG (or a machine "
            "under extreme load), not as a slow build -- check for a test "
            "binary waiting on input or a socket before assuming the harness "
            "is broken." % (target, _FW_TEST_DIR, _MAKE_TIMEOUT_S,
                            _MAKE_TIMEOUT_S // 120)
        ) from exc


def test_makefile_declares_all_expected_binaries():
    """Sanity on the Makefile itself (not a build) -- every binary this
    file expects to see pass below must actually be one of the Makefile's
    own targets, so a future rename/removal shows up as a clear failure
    here rather than this suite silently checking fewer binaries than it
    thinks it is."""
    makefile_text = (_FW_TEST_DIR / "Makefile").read_text(encoding="utf-8")
    for name in _EXPECTED_TEST_BINARIES:
        assert name in makefile_text, f"expected binary {name!r} not mentioned in firmware/test/Makefile"


def test_firmware_host_gcc_harness_builds_and_passes():
    """`make clean && make test` -- clean first per this lab's own
    hard-learned lesson (MEMORY.md: "Clean-rebuild firmware before ANY
    cocotb regression... stale tree fakes ~10 fails") applied here to a
    stale `bin/` directory instead of a stale cmake build tree, same
    failure shape. Asserts a clean exit AND that every expected binary's
    name appears in the "--- <name> ---" banner `firmware/test/Makefile`'s
    `test` target prints per binary, so a binary silently skipped by a
    Makefile edit doesn't hide behind an overall zero exit code.
    """
    cleaned = _run_make("clean")
    assert cleaned.returncode == 0, f"make clean failed:\n{cleaned.stdout}\n{cleaned.stderr}"

    result = _run_make("test")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"firmware/test/Makefile's `test` target failed (rc={result.returncode}):\n{combined}"
    )
    assert "ALL PASS" in combined, f"expected an 'ALL PASS' summary line:\n{combined}"
    for name in _EXPECTED_TEST_BINARIES:
        assert f"--- {name} ---" in combined, (
            f"expected {name!r} to have run (its '--- {name} ---' banner) -- "
            f"missing from output:\n{combined}"
        )
        assert f"{name}:" in combined and "checks passed" in combined, (
            f"expected {name!r} to report '<name>: N checks passed'"
        )
