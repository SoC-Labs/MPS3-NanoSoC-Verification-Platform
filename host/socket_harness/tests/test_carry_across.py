"""Carry-across endpoints: bases, honest staging separation, proven-flow families,
and the OpenOCD JTAG launcher argv. Board-free (pure data + argv assembly)."""
import os

import pytest

from socket_harness import endpoints as e
from socket_harness import carry_across as ca


def test_staged_bases():
    assert ca.axijtag.csr_base == 0x44AF0000
    assert ca.magic.csr_base == 0x44B00000
    assert ca.uart16550.csr_base == 0x44B10000
    assert ca.MAGIC_VALUE == 0x4A544147  # 'JTAG'
    assert ca.DAP_JTAG_TAPID == 0x6BA00477  # JTAG TAP, not the SWD DPIDR 0x0bb11477


def test_staged_endpoints_not_in_live_registry():
    # Honest: carry-across slaves are NOT on the shipped static — the harness must
    # not claim them as live. (They land in endpoints.REGISTRY only after the re-mint.)
    live = {s.name for s in e.REGISTRY}
    for s in ca.STAGED_ENDPOINTS:
        assert s.name not in live, s.name
        assert ca.STAGED_NOTE in s.notes


def test_families_match_proven_flow():
    # axijtag is the direct analogue of the software-SWD endpoint (OPENOCD_RBB, rp-gated).
    assert ca.axijtag.family is e.Family.DEBUG_TOOL
    assert ca.axijtag.framing is e.Framing.OPENOCD_RBB
    assert ca.axijtag.default_port == 6921 and ca.axijtag.gated_by == "rp"
    assert ca.axijtag.survives_swap is False  # the DAP reaches the RP DUT
    assert ca.magic.family is e.Family.CSR
    assert ca.uart16550.family is e.Family.CONSOLE
    # bases are 64K-aligned (mmap-able) and preserve the proven +0/+0x10000/+0x20000 layout
    assert ca.MAGIC_BASE - ca.AXIJTAG_BASE == 0x10000
    assert ca.UART16550_BASE - ca.AXIJTAG_BASE == 0x20000


def test_openocd_jtag_argv():
    argv = ca.openocd_jtag_argv(transport="xvc", host="10.0.0.9", xvc_port=2542)
    assert "-f" in argv and argv[argv.index("-f") + 1] == ca.NANOSOC_MPS3_JTAG_CFG
    assert "set TRANSPORT_MODE xvc" in argv
    assert "set XVC_HOST 10.0.0.9" in argv
    # the cfg it launches actually exists on disk
    assert os.path.isfile(ca.NANOSOC_MPS3_JTAG_CFG)
    # rbb mode is also supported (fw jtag_server path)
    assert "set TRANSPORT_MODE rbb" in ca.openocd_jtag_argv(transport="rbb")
    # negative control
    with pytest.raises(ValueError):
        ca.openocd_jtag_argv(transport="nope")


def test_ns16550_baud_math():
    # proven KR260: xin 9.216 MHz -> DL=5 -> 115200 8N1 (<0.1% error)
    assert ca.ns16550_divisor_latch(115200) == 5
    assert abs(ca.ns16550_actual_baud(5) - 115200) / 115200 < 0.001
    # the REJECTED path (clocking the 16550 from a 25 MHz aclk): DL=14 -> ~3% -> corrupt frames
    assert ca.ns16550_divisor_latch(115200, xin_hz=25_000_000) == 14
    assert abs(ca.ns16550_actual_baud(14, xin_hz=25_000_000) - 115200) / 115200 > 0.02
    with pytest.raises(ValueError):
        ca.ns16550_divisor_latch(0)
    with pytest.raises(ValueError):
        ca.ns16550_actual_baud(0)


def test_cfg_carries_the_proven_tapid():
    # the OpenOCD cfg's ACTIVE config must use the JTAG TAP IDCODE, not the SWD DPIDR
    # (the SWD DPIDR may appear in a clarifying comment, but must not drive the TAP).
    code = "\n".join(
        ln for ln in open(ca.NANOSOC_MPS3_JTAG_CFG).read().splitlines()
        if not ln.lstrip().startswith("#")
    )
    assert "0x6ba00477" in code
    assert "0x0bb11477" not in code  # SWD path's DPIDR — comment-only is fine, active config must not use it
