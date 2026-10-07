from __future__ import annotations
"""Carry-across of the PROVEN KR260/haps ``axi_jtag`` + ``axi_uart16550``
debug/console architecture onto the MPS3 DFX shell.

Reference (silicon-proven): ``nanosoc-multicore-system`` kr260-axijtag-uart /
haps-sx-axijtag-uart. The invariant core is an ``axi_jtag`` AXI->JTAG master into
the DUT CoreSight SWJ-DP, a magic-ID GPIO, and an ``axi_uart16550`` ns16550
console, all off one AXI master. See ``docs/planning/AXIJTAG_UART_CARRY_ACROSS.md``
and ``host/openocd/nanosoc_mps3_jtag.cfg``.

These endpoints are **STAGED, not live**: they require the batched re-mint that
instantiates ``axi_jtag@0x44AF_0000``, the magic GPIO ``@0x44B0_0000`` and
``axi_uart16550@0x44B1_0000`` in the shell, plus a firmware XVC/JTAG server
fronting ``axi_jtag`` (the reference has no host driver either — the IP is
encrypted, only Vivado ``hw_axi`` has driven it). They are deliberately kept OUT
of ``endpoints.REGISTRY`` (the shipped-static source of truth) so the harness
never claims a slave that isn't on silicon; ``test_carry_across`` guards that.
"""
import os

from .endpoints import EndpointSpec, Family, Framing

# --- MPS3 static-aperture bases -----------------------------------------------
# QUOTED, not decided, from tools/gen_regmap.py's RESERVATIONS -- the single
# page-allocation authority for 0x44A0_0000..0x44B2_0000. They MOVED UP ONE PAGE
# on 2026-09-11: this file and fpga/shell/bd/touch_iic_add.tcl both claimed
# 0x44AE_0000, neither able to see the other, and the contract
# (docs/contracts/shell-regmap.md) had already awarded that page to TOUCH. The
# trio keeps its relative order, so the KR260 reference layout still reads
# across. tests/firmware_logic/test_regmap_conformance.py reads these three
# literals back and fails if they drift from the map.
AXIJTAG_BASE = 0x44AF0000
MAGIC_BASE = 0x44B00000
UART16550_BASE = 0x44B10000
MAGIC_VALUE = 0x4A544147  # ASCII 'JTAG' — the aperture-alive magic word

STAGED_NOTE = (
    "STAGED axijtag-uart carry-across; NOT on the shipped static (re-mint-gated). "
    "See docs/planning/AXIJTAG_UART_CARRY_ACROSS.md"
)

# The JTAG-mode TAP IDCODE of the DUT SWJ-DP (Cortex-M0 r0p0) — NOT the SWD DPIDR
# 0x0bb11477 that the software-SWD path (endpoints 'swd') expects.
DAP_JTAG_TAPID = 0x6BA00477

# axi_jtag DAP path: modelled as the direct analogue of the software-SWD endpoint
# (OPENOCD_RBB @6920) — a firmware jtag_server on a fresh TCP port fronts the
# encrypted axi_jtag AXI window (the XVC alternative via Vivado hw_server @2542
# is documented in nanosoc_mps3_jtag.cfg).
axijtag = EndpointSpec(
    name="axijtag",
    family=Family.DEBUG_TOOL,
    framing=Framing.OPENOCD_RBB,
    proto="tcp",
    default_port=6921,
    csr_base=AXIJTAG_BASE,
    gated_by="rp",
    survives_swap=False,
    owner="axi_jtag DAP + fw jtag_server (carry-across)",
    notes=(
        "OpenOCD JTAG to the DUT SWJ-DP via axi_jtag@0x44AF + a fw jtag_server "
        "(analogue of swd 6920). TAP IDCODE 0x6ba00477. " + STAGED_NOTE
    ),
    device=None,
)

magic = EndpointSpec(
    name="magic",
    family=Family.CSR,
    framing=Framing.XSDB_REG,
    proto="jtag",
    default_port=None,
    csr_base=MAGIC_BASE,
    gated_by="shell",
    survives_swap=True,
    owner="axi_gpio magic-ID (carry-across)",
    notes="reads 0x4A544147 ('JTAG') aperture-alive preflight. " + STAGED_NOTE,
    device=None,
)

uart16550 = EndpointSpec(
    name="uart16550",
    family=Family.CONSOLE,
    framing=Framing.SERIAL,
    proto="serial",
    default_port=None,
    csr_base=UART16550_BASE,
    gated_by="shell",
    survives_swap=True,
    owner="axi_uart16550 ns16550 console (carry-across)",
    notes=(
        "ns16550a @0x44B1, xin 9.216 MHz DL=5 -> 115200 8N1; reached as an "
        "MB-V Linux tty / ser2net (mainline 8250 driver). " + STAGED_NOTE
    ),
    device="/dev/ttyS0",
)

STAGED_ENDPOINTS = (axijtag, magic, uart16550)

# --- OpenOCD launcher for host/openocd/nanosoc_mps3_jtag.cfg ------------------
_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
NANOSOC_MPS3_JTAG_CFG = os.path.join(_REPO, "host", "openocd", "nanosoc_mps3_jtag.cfg")


def openocd_jtag_argv(
    *,
    transport: str = "xvc",
    host: str = "192.168.10.101",
    xvc_port: int = 2542,
    rbb_port: int = 6921,
    cfg: str = NANOSOC_MPS3_JTAG_CFG,
    openocd: str = "openocd",
) -> list[str]:
    """Assemble the argv to launch OpenOCD against the MPS3 DUT DAP over axi_jtag.

    Pure argv assembly (no subprocess), in the spirit of
    ``pyverify.debug.OpenOcdRemoteBitbangConfig``. ``transport`` in {'xvc','rbb'}.
    """
    if transport not in ("xvc", "rbb"):
        raise ValueError("transport must be 'xvc' or 'rbb', got %r" % (transport,))
    return [
        openocd,
        "-c", "set TRANSPORT_MODE %s" % transport,
        "-c", "set XVC_HOST %s" % host,
        "-c", "set XVC_PORT %d" % xvc_port,
        "-c", "set RBB_HOST %s" % host,
        "-c", "set RBB_PORT %d" % rbb_port,
        "-f", cfg,
    ]


# --- axi_uart16550 baud arithmetic (proven on KR260: xin 9.216 MHz, DL=5 -> 115200) --
# The trap (address_map.txt): NEVER clock the 16550 from s_axi_aclk. A dedicated
# clk_wiz gives the 9.216 MHz xin baud reference; clocking it from a 25 MHz aclk
# gives DL=14 -> -3.1% -> corrupt long frames.
NS16550_XIN_HZ = 9_216_000


def ns16550_divisor_latch(baud: int, xin_hz: int = NS16550_XIN_HZ) -> int:
    """16550 divisor latch DL = round(xin / (16*baud)). DL=5 at 9.216 MHz / 115200."""
    if baud <= 0:
        raise ValueError("baud must be positive")
    return round(xin_hz / (16 * baud))


def ns16550_actual_baud(dl: int, xin_hz: int = NS16550_XIN_HZ) -> float:
    """Realised baud for a given divisor latch."""
    if dl <= 0:
        raise ValueError("DL must be positive")
    return xin_hz / (16 * dl)


__all__ = [
    "AXIJTAG_BASE", "MAGIC_BASE", "UART16550_BASE", "MAGIC_VALUE", "DAP_JTAG_TAPID",
    "STAGED_NOTE", "STAGED_ENDPOINTS", "axijtag", "magic", "uart16550",
    "NANOSOC_MPS3_JTAG_CFG", "openocd_jtag_argv",
    "NS16550_XIN_HZ", "ns16550_divisor_latch", "ns16550_actual_baud",
]
