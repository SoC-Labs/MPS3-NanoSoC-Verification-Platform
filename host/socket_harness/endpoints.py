from __future__ import annotations

"""Frozen endpoint registry — the single source of truth for the harness.

Every host-facing endpoint of the MPS3 nanoSoC shell (control, push, console,
debug tools, JTAG/xsdb register access, the MCC serial console) is described
once here as an :class:`EndpointSpec`.  Ports and CSR bases are transcribed
from the two normative contracts:

  * ``docs/contracts/net-protocol.md``  — the TCP/UDP port map (§"TCP/UDP port map").
  * ``docs/contracts/shell-regmap.md``  — the AXI4-Lite CSR block bases.

This module is deliberately dependency-light: it MUST NOT import ``pyverify``
(the drift-guard equalities against ``pyverify`` constants live only in
``tests/test_endpoints.py``).  It also does NO I/O at import time — building
the tuple + the name index is pure.

Notes worth carrying at the registry level (both traps proven on the bench):

  * ``DEFAULT_HUB_URL`` is the **FQDN** form (``scripts/mps3_diag.tcl:21``): a
    bare host fails for xsdb.  It is intentionally spelled differently from
    ``pyverify.swd.DEFAULT_HUB`` (bare ``"<hub-host>"``); nothing asserts the
    two are equal.
  * ``vphy`` / ``genchk`` / ``clcdkvm`` are address-map RESERVATIONS — the
    shipped static (``NUM_MI = 15``, ``assign_bd_address`` stops at CLCD
    ``0x44AC``) has no slave there, so probes must not assume they respond.
"""

import enum
import os
from dataclasses import dataclass


__all__ = [
    "Family",
    "Framing",
    "Address",
    "EndpointSpec",
    "REGISTRY",
    "by_name",
    "by_port",
    "by_family",
    "console_specs",
    "csr_specs",
    "DEFAULT_SHELL_HOST",
    "DEFAULT_HUB_URL",
    "DEFAULT_MCC_DEVICE",
    "fielded_static_id",
]


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

# The shell's static management IP on the lab LAN (matches
# pyverify.swd.DEFAULT_SHELL_HOST == "192.168.10.101").
DEFAULT_SHELL_HOST = "192.168.10.101"

# xsdb / hw_server hub URL.  FQDN is REQUIRED (scripts/mps3_diag.tcl:21 — a bare
# host name fails).  Supplied via the ``MPS3_HW_URL`` env var (operators export
# it in a local, git-ignored set_env.local.sh); when unset it falls back to a
# localhost placeholder that fails loudly, rather than baking a site hostname
# into the public tree.  Deliberately distinct from pyverify.swd.DEFAULT_HUB
# (the bare host); no equality between the two is assumed anywhere.
DEFAULT_HUB_URL = os.environ.get("MPS3_HW_URL") or "tcp:localhost:3121"

# MPS3 MCC / FT4232 board-management serial console (scripts/mps3_console.py).
DEFAULT_MCC_DEVICE = "/dev/ttyUSB6"


def fielded_static_id() -> int:
    """The static_id the board actually boots, from THE resolver.

    Deliberately a function, not a module constant: this module does **no I/O at
    import time** (see the module docstring) and the answer lives in a file —
    ``docs/FIELDED_SHELL.md``, read by :mod:`pyverify.fielded`. The registry's
    ``notes`` below name past statics as HISTORY ("DORMANT on the 0xCD74B6AE
    JTAG shell", "since 0xCD74B6AE"); those are permanently true and are left
    alone. Anything that needs the CURRENT one calls this.

    The import is local for the same reason the drift-guard equalities against
    ``pyverify`` constants live only in ``tests/test_endpoints.py``: importing
    it at module scope would make this registry depend on pyverify to be read
    at all.
    """
    from pyverify.fielded import load

    return load().static_id


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #


class Family(enum.Enum):
    """The kind of endpoint — dispatched on by the harness probe/session layer."""

    CONTROL = enum.auto()
    PUSH = enum.auto()
    CONSOLE = enum.auto()
    DEBUG_TOOL = enum.auto()
    JTAG = enum.auto()
    JTAG_REG = enum.auto()
    CSR = enum.auto()
    SERIAL = enum.auto()


class Framing(enum.Enum):
    """The on-the-wire framing/protocol an endpoint speaks."""

    JSON_LINE = enum.auto()
    RAW = enum.auto()
    TFTP = enum.auto()
    OPENOCD_RBB = enum.auto()
    XVC = enum.auto()
    XSDB_REG = enum.auto()
    SERIAL = enum.auto()


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Address:
    """A resolved, connectable address for one endpoint."""

    host: str
    port: int | None = None
    csr_base: int | None = None
    hub_url: str | None = None
    device: str | None = None


@dataclass(frozen=True)
class EndpointSpec:
    """A single frozen registry row describing one harness endpoint.

    ``gated_by`` uses the SAME string values as ``pyverify.edge.GatedBy``
    (``'shell'`` / ``'rp'`` / ``'none'``): a ``shell``-gated channel survives a
    ``dfx-swap``, an ``rp``-gated one is invalidated by it.
    """

    name: str
    family: Family
    framing: Framing
    proto: str  # 'tcp' | 'udp' | 'jtag' | 'serial'
    default_port: int | None
    csr_base: int | None
    gated_by: str  # 'shell' | 'rp' | 'none'
    survives_swap: bool
    owner: str
    notes: str
    device: str | None = None

    def resolve(
        self,
        host: str,
        *,
        port: int | None = None,
        hub_url: str | None = None,
        device: str | None = None,
    ) -> Address:
        """Bind this spec to a concrete host, filling defaults from the row."""
        return Address(
            host=host,
            port=port or self.default_port,
            csr_base=self.csr_base,
            hub_url=hub_url,
            device=device or self.device,
        )


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #

# The 14 CSR blocks share every column except name / base / owner / notes.
# Build them from a compact table so the block bases are trivially auditable
# against docs/contracts/shell-regmap.md.  RESERVED blocks are flagged in notes
# so RegisterAccess / probe never assume they respond on the shipped static.
_CSR_ROWS: tuple[tuple[str, int, str, str], ...] = (
    (
        "clkrst",
        0x44A00000,
        "fpga/shell/ip/clkrst",
        "DUT clock (DRP) + 3 resets: RESET_CTRL/DUT_CLK_SEL/DUT_CLK_DRP/STATUS.",
    ),
    (
        "dfxctl",
        0x44A10000,
        "fpga/shell/ip/dfx_ctl",
        "decouple/shutdown/RM_ID/RM_STATUS; RM_STATUS[2]=dut_eth_irq (2026-07-24, "
        "2-FF synced). NB a read of 0x20 pops a console byte (dfx_ctl.sv decode alias).",
    ),
    (
        "hwicap",
        0x44A20000,
        "Xilinx AXI HWICAP",
        "partial-bitstream config port (PG134); clearing then partial per swap.",
    ),
    (
        "vphy",
        0x44A30000,
        "fpga/ethernet/mdio_phy_model",
        "virtual-PHY register model + host-injected link events. "
        "LIVE: shell_bd.tcl:1399 assigns 0x44A3_0000 through eth_mac_test_subsystem_0.",
    ),
    (
        "usd",
        0x44A40000,
        "fpga/shell/ip/usd_spi",
        "USER microSD, SPI mode (D13; was the pad-less OVLSTORE AXI Quad SPI): "
        "ID/CTRL/CLKDIV/DATA/STATUS. A DATA write clocks the card -- never poke it blind.",
    ),
    (
        "telem",
        0x44A50000,
        "fpga/shell/ip/telem",
        "telemetry: INA228 I2C bus voltage / current / power + status.",
    ),
    (
        "genchk",
        0x44A60000,
        "fpga/ethernet/gen_checker",
        "error-inject gen/checker control (macgen verb): CTRL/INJECT/TX_CNT/RX_CNT/ERR_CNT. "
        "LIVE: shell_bd.tcl:1400 assigns 0x44A6_0000 through eth_mac_test_subsystem_0.",
    ),
    (
        "swdbb",
        0x44A70000,
        "fpga/shell/ip/swd_bb",
        "SWD pin-wiggler backing the OpenOCD remote_bitbang server (port 6920).",
    ),
    (
        "dbgbr",
        0x44A80000,
        "Xilinx Debug Bridge",
        "XVC/BSCAN debug bridge (ILA/VIO) behind port 2542; must be in static.",
    ),
    (
        "uartbr",
        0x44A90000,
        "fpga/shell/ip/uart_bridge",
        "UART0/UART1/SWO AXIS <-> MicroBlaze FIFOs (ports 6930/6931/6932); RX reads destructive.",
    ),
    (
        "gpio",
        0x44AA0000,
        "fpga/shell/ip/board_gpio",
        "board GPIO/PMOD passthrough + per-bit host mux override (OWN).",
    ),
    (
        "mmcm_drp",
        0x44AB0000,
        "Xilinx Clocking Wizard (DUT clk)",
        "DUT-clock MMCM DRP over AXI4-Lite (arbitrary-freq reconfig); must be in static.",
    ),
    (
        "clcd",
        0x44AC0000,
        "fpga/shell/ip/clcd",
        "QVGA HX8347-D 8080 parallel bus master; LIVE (instantiated, panel lit 2026-07-14).",
    ),
    (
        "clcdkvm",
        0x44AD0000,
        "fpga/shell/ip/clcd_kvm",
        "CLCD KVM panel arbiter (harness vs DUT, USER_nPB[1] toggle). "
        "LIVE: shell_bd.tcl:578 instantiates clcd_kvm_0, :1407 assigns 0x44AD_0000.",
    ),
)


def _csr_spec(name: str, base: int, owner: str, notes: str) -> EndpointSpec:
    return EndpointSpec(
        name=name,
        family=Family.CSR,
        framing=Framing.XSDB_REG,
        proto="jtag",
        default_port=None,
        csr_base=base,
        gated_by="shell",
        survives_swap=True,
        owner=owner,
        notes=notes,
    )


# The full registry, frozen at import.  Ordering is deliberate: network
# endpoints first (net-protocol.md port-map order), then the two JTAG helpers
# and the MCC serial console, then the 14 CSR blocks in base order.
REGISTRY: tuple[EndpointSpec, ...] = (
    EndpointSpec(
        name="control",
        family=Family.CONTROL,
        framing=Framing.JSON_LINE,
        proto="tcp",
        default_port=6900,
        csr_base=None,
        gated_by="shell",
        survives_swap=True,
        owner="A3 coordinator",
        notes="control/status JSON-line request/response (swap, reset, clock, "
        "RM-id, telemetry); net-protocol.md TCP 6900. A swap parks this connection.",
    ),
    EndpointSpec(
        name="push_tftp",
        family=Family.PUSH,
        framing=Framing.TFTP,
        proto="udp",
        default_port=69,
        csr_base=None,
        gated_by="shell",
        survives_swap=True,
        owner="A3 config_agent",
        notes="TFTP partial/clearing bitstream push (D3 default); net-protocol.md UDP 69.",
    ),
    EndpointSpec(
        name="push_raw",
        family=Family.PUSH,
        framing=Framing.RAW,
        proto="tcp",
        default_port=6910,
        csr_base=None,
        gated_by="shell",
        survives_swap=True,
        owner="A3 config_agent",
        notes="raw partial push (alt to TFTP); optional windowed flow control "
        "(net-protocol.md v0.5); net-protocol.md TCP 6910.",
    ),
    EndpointSpec(
        name="xvc",
        family=Family.DEBUG_TOOL,
        framing=Framing.XVC,
        proto="tcp",
        default_port=2542,
        csr_base=None,
        gated_by="shell",
        survives_swap=True,
        owner="A3 xvc_server",
        notes="Xilinx Virtual Cable -> Debug Bridge (DBGBR/ILA); net-protocol.md "
        "TCP 2542; must be in static (survives swaps).",
    ),
    EndpointSpec(
        name="swd",
        family=Family.DEBUG_TOOL,
        framing=Framing.OPENOCD_RBB,
        proto="tcp",
        default_port=6920,
        csr_base=None,
        gated_by="rp",
        survives_swap=False,
        owner="A3 swd_server",
        notes="OpenOCD remote_bitbang to the SWDBB pin-wiggler; net-protocol.md "
        "TCP 6920; RP-gated (invalidated by a dfx-swap). DORMANT on the "
        "0xCD74B6AE JTAG shell -- see 'jtag'.",
    ),
    EndpointSpec(
        name="jtag",
        family=Family.DEBUG_TOOL,
        framing=Framing.OPENOCD_RBB,
        proto="tcp",
        default_port=6921,
        csr_base=None,
        gated_by="rp",
        survives_swap=False,
        owner="A3 jtag_server",
        notes="OpenOCD remote_bitbang to the JTAGBB pin-wiggler -> SoC-400 "
        "SWJ-DP (TAP 0x6ba00477); net-protocol.md TCP 6921; RP-gated "
        "(invalidated by a dfx-swap). This is the LIVE DUT-debug path on the "
        "JTAG-bridge shell (since 0xCD74B6AE); host/openocd/nanosoc_mps3_jtag.cfg.",
    ),
    EndpointSpec(
        name="uart0",
        family=Family.CONSOLE,
        framing=Framing.RAW,
        proto="tcp",
        default_port=6930,
        csr_base=None,
        gated_by="shell",
        survives_swap=True,
        owner="A3 uart_over_eth",
        notes="UART0 boot-monitor raw byte stream; net-protocol.md TCP 6930; "
        "shell-gated boot console.",
    ),
    EndpointSpec(
        name="uart1",
        family=Family.CONSOLE,
        framing=Framing.RAW,
        proto="tcp",
        default_port=6931,
        csr_base=None,
        gated_by="rp",
        survives_swap=False,
        owner="A3 uart_over_eth",
        notes="UART1 application console raw byte stream; net-protocol.md TCP 6931; "
        "modelled as its own RP-gated channel.",
    ),
    EndpointSpec(
        name="swo",
        family=Family.CONSOLE,
        framing=Framing.RAW,
        proto="tcp",
        default_port=6932,
        csr_base=None,
        gated_by="rp",
        survives_swap=False,
        owner="A3 uart_over_eth",
        notes="SWO/ITM trace raw byte stream; net-protocol.md TCP 6932; RP-gated.",
    ),
    EndpointSpec(
        name="hw_server",
        family=Family.JTAG,
        framing=Framing.XSDB_REG,
        proto="jtag",
        default_port=3121,
        csr_base=None,
        gated_by="none",
        survives_swap=True,
        owner="Xilinx hw_server",
        notes="Vivado hw_server / xsdb JTAG hub; reach via DEFAULT_HUB_URL (FQDN "
        "REQUIRED — a bare host name fails, mps3_diag.tcl:21).",
    ),
    EndpointSpec(
        name="mcc_console",
        family=Family.SERIAL,
        framing=Framing.SERIAL,
        proto="serial",
        default_port=None,
        csr_base=None,
        gated_by="none",
        survives_swap=False,
        owner="MPS3 MCC",
        notes="MPS3 MCC FT4232 board-management serial console; raw 8N1, char-paced "
        "(shallow-FIFO guard). Device default /dev/ttyUSB6 (scripts/mps3_console.py).",
        device=DEFAULT_MCC_DEVICE,
    ),
    EndpointSpec(
        name="diag_mbox",
        family=Family.JTAG_REG,
        framing=Framing.XSDB_REG,
        proto="jtag",
        default_port=None,
        csr_base=None,
        gated_by="shell",
        survives_swap=True,
        owner="A3 firmware (diag.h)",
        notes="JTAG diagnostic mailbox anchored to the END of the MicroBlaze LMB "
        "(magic-scan ASCENDING candidate bases — LMB decode aliases); read-only "
        "via MDM, NEVER stop/con on a running core (mps3_diag.tcl).",
    ),
    _csr_spec(*_CSR_ROWS[0]),
    _csr_spec(*_CSR_ROWS[1]),
    _csr_spec(*_CSR_ROWS[2]),
    _csr_spec(*_CSR_ROWS[3]),
    _csr_spec(*_CSR_ROWS[4]),
    _csr_spec(*_CSR_ROWS[5]),
    _csr_spec(*_CSR_ROWS[6]),
    _csr_spec(*_CSR_ROWS[7]),
    _csr_spec(*_CSR_ROWS[8]),
    _csr_spec(*_CSR_ROWS[9]),
    _csr_spec(*_CSR_ROWS[10]),
    _csr_spec(*_CSR_ROWS[11]),
    _csr_spec(*_CSR_ROWS[12]),
    _csr_spec(*_CSR_ROWS[13]),
)


# Name index, built once at import (pure — a dict comprehension, no I/O).
_BY_NAME: dict[str, EndpointSpec] = {spec.name: spec for spec in REGISTRY}


# --------------------------------------------------------------------------- #
# Lookups
# --------------------------------------------------------------------------- #


def by_name(name: str) -> EndpointSpec:
    """Return the spec named ``name``; ValueError (listing known names) if absent."""
    try:
        return _BY_NAME[name]
    except KeyError:
        known = ", ".join(sorted(_BY_NAME))
        raise ValueError(f"unknown endpoint {name!r}; known: {known}") from None


def by_port(port: int) -> EndpointSpec:
    """Return the first tcp/udp spec whose default_port matches; ValueError if none."""
    for spec in REGISTRY:
        if spec.proto in ("tcp", "udp") and spec.default_port == port:
            return spec
    raise ValueError(f"no tcp/udp endpoint on port {port}")


def by_family(fam: Family) -> tuple[EndpointSpec, ...]:
    """Return every spec in ``fam``, in registry order."""
    return tuple(spec for spec in REGISTRY if spec.family is fam)


def console_specs() -> tuple[EndpointSpec, ...]:
    """Return the three console endpoints in fixed order: (uart0, uart1, swo)."""
    return (by_name("uart0"), by_name("uart1"), by_name("swo"))


def csr_specs() -> tuple[EndpointSpec, ...]:
    """Return the 14 CSR-block endpoints (family == Family.CSR), in base order."""
    return by_family(Family.CSR)
