"""``webharness.catalog`` — the "what can I attach to, and how" directory.

This is the **pure data half** of the web harness: given a resident ``rm_id``
it answers *which services are up*, *on what port*, and *the exact command a
user runs from Linux to attach to each*. No sockets, no board, no I/O.

Source of truth, and why it is transcribed rather than imported:

* **Ports** come from ``socket_harness.endpoints.REGISTRY`` wherever a row
  exists — that package is the frozen registry and this module defers to it.
  :func:`registry_port` does the lookup; :data:`SERVICES` records the port it
  *expects*, and ``tests/test_catalog.py`` asserts the two agree for every
  service the registry knows. That is a tripwire, not a duplication: if a port
  moves in the registry, exactly one assertion reddens.
* **The JTAG bridge (TCP 6921)** was missing from the registry when this module
  was written — that package predated the SWD→JTAG migration (shell
  ``0xCD74B6AE``), whose ``jtag_server`` listens on 6921 while ``swd_server``
  is dormant (``firmware/platform/src/main.c:240``, ``firmware/jtag_server/``).
  **Closed 2026-08-01**: the contract itself grew the row
  (``net-protocol.md`` TCP 6921 / ``net_proto.h`` ``MPS3_PORT_JTAG``), so the
  chain ``contract -> pyverify.debug.JTAG_REMOTE_BITBANG_PORT ->
  socket_harness.REGISTRY['jtag'] -> here`` is now complete and every link is
  drift-guarded. :func:`registry_gap` stays as the *mechanism* — it returns
  empty today and a test pins that, so the next service to arrive ahead of the
  registry is visible instead of silently papered over.
* **Which services a given RM exposes** mirrors ``clcd_rm_services()``
  (``firmware/clcd/clcd.c``) bit-for-bit, keyed on the DESIGN half of the
  rm_id exactly like the firmware. ``tests/test_catalog.py`` parses the C
  source and asserts the two tables agree over all 65536 design ids — so the
  glass on the board and this page can never disagree about what is up.
* **RM names / makeup** mirror ``clcd_rm_name()`` / ``clcd_rm_caps()`` from
  the same file, under the same parsed-C drift guard.

The "shell" services (6900 / 69 / 6910 / 2542) belong to the *static shell*,
not the DUT, so they are present for every RM — including an unknown or
mid-swap id. The "dut" services need the resident RM to actually have the
CPU / UART behind them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

__all__ = [
    "SVC_CTRL", "SVC_TFTP", "SVC_PUSH", "SVC_XVC", "SVC_SWD", "SVC_JTAG",
    "SVC_UART0", "SVC_UART1", "SVC_SWO",
    "ServiceSpec", "SERVICES", "RM_NAMES", "RM_CAPS", "RM_SERVICES",
    "SHELL_SERVICES",
    "rm_design", "rm_name", "rm_caps", "services_for", "service_rows",
    "registry_port", "registry_gap",
]


# --------------------------------------------------------------------------- #
# Service bits — transcribed from firmware/clcd/clcd.h:161-169 (CLCD_SVC_*)
# --------------------------------------------------------------------------- #

SVC_CTRL = 1 << 0    # TCP 6900 control/status
SVC_TFTP = 1 << 1    # UDP 69   TFTP push
SVC_PUSH = 1 << 2    # TCP 6910 raw partial push
SVC_XVC = 1 << 3     # TCP 2542 XVC debug bridge
SVC_SWD = 1 << 4     # TCP 6920 OpenOCD SWD
SVC_JTAG = 1 << 5    # TCP 6921 OpenOCD JTAG
SVC_UART0 = 1 << 6   # TCP 6930 DUT UART0
SVC_UART1 = 1 << 7   # TCP 6931 DUT UART1
SVC_SWO = 1 << 8     # TCP 6932 SWO/ITM trace

#: Everything the STATIC shell serves, regardless of what RM is resident (and
#: regardless of whether the rm_id is even readable). Mirrors the unconditional
#: seed in ``clcd_rm_services()``.
SHELL_SERVICES = SVC_CTRL | SVC_TFTP | SVC_PUSH | SVC_XVC


@dataclass(frozen=True)
class ServiceSpec:
    """One attachable service.

    ``registry_name`` is the ``socket_harness.endpoints`` row this service
    corresponds to, or ``None`` when the registry has no row for it (today:
    only ``jtag`` — see the module docstring). ``attach`` is a format string
    taking ``{host}``; it is the literal command a user pastes.
    """

    key: str
    label: str
    bit: int
    proto: str            # 'tcp' | 'udp'
    port: int
    scope: str            # 'shell' (always up) | 'dut' (RM-dependent)
    registry_name: Optional[str]
    attach: str
    note: str = ""

    def command(self, host: str) -> str:
        return self.attach.format(host=host)


#: Ordered for display: shell services first, then the per-DUT ones.
SERVICES: Tuple[ServiceSpec, ...] = (
    ServiceSpec(
        key="ctrl", label="Control / status", bit=SVC_CTRL,
        proto="tcp", port=6900, scope="shell", registry_name="control",
        attach="python -m pyverify.cli edge status --host {host}",
        note="JSON-line control channel. Single client; PARKED during a swap.",
    ),
    ServiceSpec(
        key="tftp", label="Bitstream push (TFTP)", bit=SVC_TFTP,
        proto="udp", port=69, scope="shell", registry_name="push_tftp",
        attach="python -m pyverify.cli deploy --host {host} --overlay <rm>",
        note="Default partial/clearing transport.",
    ),
    ServiceSpec(
        key="push", label="Bitstream push (raw TCP)", bit=SVC_PUSH,
        proto="tcp", port=6910, scope="shell", registry_name="push_raw",
        attach="python -m pyverify.cli deploy --host {host} --overlay <rm> --windowed",
        note="Alternative to TFTP; supports the windowed flow control.",
    ),
    ServiceSpec(
        key="xvc", label="XVC debug bridge (ILA)", bit=SVC_XVC,
        proto="tcp", port=2542, scope="shell", registry_name="xvc",
        attach="xsdb -eval 'connect -xvc {host}:2542'",
        note="Xilinx Virtual Cable v1.0 -> Debug Bridge.",
    ),
    ServiceSpec(
        key="swd", label="DUT debug — SWD", bit=SVC_SWD,
        proto="tcp", port=6920, scope="dut", registry_name="swd",
        attach="openocd -f host/openocd/nanosoc_mps3.cfg",
        note="OpenOCD remote_bitbang. DORMANT on the 0xCD74B6AE JTAG shell — "
             "use the JTAG bridge instead.",
    ),
    ServiceSpec(
        key="jtag", label="DUT debug — JTAG", bit=SVC_JTAG,
        proto="tcp", port=6921, scope="dut", registry_name="jtag",
        attach="openocd -f host/openocd/nanosoc_mps3_jtag.cfg",
        note="OpenOCD remote_bitbang -> jtag_bb -> SoC-400 SWJ-DP "
             "(TAP 0x6ba00477). The LIVE debug path on the shipped shell.",
    ),
    ServiceSpec(
        key="uart0", label="DUT UART0 (boot monitor)", bit=SVC_UART0,
        proto="tcp", port=6930, scope="dut", registry_name="uart0",
        attach="nc {host} 6930",
        note="Raw byte stream.",
    ),
    ServiceSpec(
        key="uart1", label="DUT UART1 (application)", bit=SVC_UART1,
        proto="tcp", port=6931, scope="dut", registry_name="uart1",
        attach="nc {host} 6931",
        note="Raw byte stream; two-UART designs only.",
    ),
    ServiceSpec(
        key="swo", label="DUT SWO / ITM trace", bit=SVC_SWO,
        proto="tcp", port=6932, scope="dut", registry_name="swo",
        attach="nc {host} 6932",
        note="RX-only trace stream.",
    ),
)

_BY_KEY: Dict[str, ServiceSpec] = {s.key: s for s in SERVICES}


# --------------------------------------------------------------------------- #
# RM design tables — mirrors of firmware/clcd/clcd.c
# --------------------------------------------------------------------------- #

#: design half -> name. Mirror of ``clcd_rm_name()`` (clcd.c:220-244).
RM_NAMES: Dict[int, str] = {
    0x0000: "greybox",
    0x0001: "nanosoc",
    0x0002: "eth_ss",
    0x0003: "nanosoc_multicore",
    0x0004: "uart_echo",
    0x001E: "led",
    0x00A1: "regdemo_a",
    0x00B2: "regdemo_b",
}

#: design half -> one-line makeup. Mirror of ``clcd_rm_caps()`` (clcd.c:328-338).
RM_CAPS: Dict[int, str] = {
    0x0000: "greybox: empty tie-off",
    0x0001: "1x Cortex-M0  no ETH  1x UART",
    0x0002: "no CPU  ETH MAC+PTP  no UART",
    0x0003: "2x CPU  ETH MAC+PTP  2x UART",
    0x0004: "no CPU  1x UART (echo)",
    0x001E: "no CPU  LED demo",
    0x00A1: "no CPU  AHB register demo",
    0x00B2: "no CPU  AHB register demo",
}

#: design half -> EXTRA (per-DUT) service bits on top of :data:`SHELL_SERVICES`.
#: Mirror of ``clcd_rm_services()`` (clcd.c:348-366). A design absent from this
#: map gets shell services only — the firmware's ``default:`` arm.
RM_SERVICES: Dict[int, int] = {
    0x0001: SVC_SWD | SVC_JTAG | SVC_UART0 | SVC_SWO,
    0x0003: SVC_SWD | SVC_JTAG | SVC_UART0 | SVC_UART1 | SVC_SWO,
    0x0004: SVC_UART0,
}


def rm_design(rm_id: int) -> int:
    """The DESIGN half of an rm_id (``CLCD_RM_DESIGN``, clcd.h:233)."""
    return int(rm_id) & 0xFFFF


def rm_name(rm_id: int) -> str:
    """Human name for ``rm_id``, or the firmware's honest ``rm?XXXXXXXX``
    rendering of the WHOLE 32-bit word when the design half is unrecognised
    (clcd.c:231-242 — never lie about an unknown id)."""
    design = rm_design(rm_id)
    if design in RM_NAMES:
        return RM_NAMES[design]
    return "rm?%08X" % (int(rm_id) & 0xFFFFFFFF)


def rm_caps(rm_id: int) -> str:
    """One-line makeup of ``rm_id``'s design, or ``"design unknown"``."""
    return RM_CAPS.get(rm_design(rm_id), "design unknown")


def services_for(rm_id: Optional[int]) -> int:
    """Service bitmask for the resident RM.

    ``rm_id=None`` means "the rm_id is not readable" (RP decoupled / in reset /
    id not valid / control channel down). That is NOT an error and NOT zero:
    the static shell's own services are still up, so it returns
    :data:`SHELL_SERVICES` — the same thing the firmware shows for an
    unknown id.
    """
    if rm_id is None:
        return SHELL_SERVICES
    return SHELL_SERVICES | RM_SERVICES.get(rm_design(rm_id), 0)


def registry_port(name: Optional[str]) -> Optional[int]:
    """The ``socket_harness`` registry's port for ``name``, or ``None`` when
    there is no such row (or no name). Imported lazily so this module stays
    importable — and testable — without socket_harness on the path.
    """
    if name is None:
        return None
    try:
        from socket_harness.endpoints import by_name
    except Exception:          # pragma: no cover - environment without the pkg
        return None
    try:
        return by_name(name).default_port
    except Exception:
        return None


def registry_gap() -> Tuple[str, ...]:
    """Service keys this catalog carries that the frozen registry does not.
    Surfaced in the API so the gap is reportable, not folklore."""
    return tuple(s.key for s in SERVICES if s.registry_name is None)


def service_rows(rm_id: Optional[int], host: str) -> List[dict]:
    """The directory, as JSON-ready rows: every service, with ``present``
    resolved against the resident RM and ``command`` resolved against ``host``.

    Absent services are still listed (with ``present:false``) — "this port
    exists but this RM has no CPU behind it" is the useful answer, and hiding
    the row would make an eth-only RM look like a broken shell.
    """
    mask = services_for(rm_id)
    rows = []
    for spec in SERVICES:
        rows.append({
            "key": spec.key,
            "label": spec.label,
            "proto": spec.proto,
            "port": spec.port,
            "scope": spec.scope,
            "present": bool(mask & spec.bit),
            "command": spec.command(host),
            "note": spec.note,
            "registry_name": spec.registry_name,
        })
    return rows


def by_key(key: str) -> Optional[ServiceSpec]:
    return _BY_KEY.get(key)
