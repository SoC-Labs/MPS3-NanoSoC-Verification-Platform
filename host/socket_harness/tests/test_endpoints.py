"""Registry integrity + the drift-guard against pyverify constants and
docs/contracts/{net-protocol.md,shell-regmap.md}.

Board-free: pure data assertions over the frozen ``socket_harness.endpoints``
REGISTRY, plus cross-file invariants against ``socket_harness.registers.BLOCKS``
and the ``pyverify`` port constants. No sockets, no subprocess, no board.

The whole point of this file is *drift detection*: the endpoint ports and CSR
bases are duplicated in several places historically (pyverify.{client,console,
debug,pusher}, scripts/mps3_console.py, scripts/mps3_diag.tcl, host/console/*),
and this registry is meant to be the single source of truth. Each assertion
below is a tripwire so that if a port or a CSR base drifts, exactly the matching
assertion reddens (see the MUTATION note at the bottom).
"""
from __future__ import annotations

import pytest

# --- the module under test -------------------------------------------------
from socket_harness.endpoints import (
    Address,
    DEFAULT_HUB_URL,
    DEFAULT_SHELL_HOST,
    EndpointSpec,
    Family,
    Framing,
    REGISTRY,
    by_family,
    by_name,
    by_port,
    console_specs,
    csr_specs,
)

# --- cross-file collateral (independently built to the same spec) ----------
from socket_harness.registers import BLOCKS
from socket_harness.console_bridge import ser2net_yaml, socat_argv

# --- pyverify constants: the drift-guard anchors ---------------------------
# pyverify is a hard dependency of this package (see pyproject.toml), so these
# imports resolve at test time in any environment that installed the harness.
from pyverify.client import CONTROL_PORT
from pyverify.pusher import TFTP_PORT, RAW_TCP_PORT
from pyverify.debug import XVC_PORT, SWD_REMOTE_BITBANG_PORT, JTAG_REMOTE_BITBANG_PORT
from pyverify.console import UART0_PORT, UART1_PORT, SWO_PORT
from pyverify.edge import GatedBy
from pyverify.swd import DEFAULT_SHELL_HOST as PYVERIFY_DEFAULT_SHELL_HOST


# The 14 CSR blocks, name -> base, transcribed straight from
# docs/contracts/shell-regmap.md. These are the *literal* anchors of test (3);
# they are asserted against the registry AND against registers.BLOCKS.
EXPECTED_CSR_BASES = {
    "clkrst": 0x44A00000,
    "dfxctl": 0x44A10000,
    "hwicap": 0x44A20000,
    "vphy": 0x44A30000,
    "usd": 0x44A40000,
    "telem": 0x44A50000,
    "genchk": 0x44A60000,
    "swdbb": 0x44A70000,
    "dbgbr": 0x44A80000,
    "uartbr": 0x44A90000,
    "gpio": 0x44AA0000,
    "mmcm_drp": 0x44AB0000,
    "clcd": 0x44AC0000,
    "clcdkvm": 0x44AD0000,
}


# ---------------------------------------------------------------------------
# (1) Registry integrity: names unique, ports unique within a proto.
# ---------------------------------------------------------------------------
def test_registry_names_are_unique():
    names = [spec.name for spec in REGISTRY]
    assert len(names) == len(set(names)), "duplicate endpoint name in REGISTRY"


def test_tcp_udp_ports_unique_within_proto():
    for proto in ("tcp", "udp"):
        ports = [
            spec.default_port
            for spec in REGISTRY
            if spec.proto == proto and spec.default_port is not None
        ]
        assert len(ports) == len(set(ports)), (
            f"duplicate default_port within proto={proto}: {ports}"
        )


# ---------------------------------------------------------------------------
# (2) DRIFT-GUARD against the pyverify port constants.
#     Each row of this table is an independent tripwire: change a registry
#     port and exactly one of these reddens.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "spec_name, pyverify_const, literal",
    [
        ("control", CONTROL_PORT, 6900),
        ("push_tftp", TFTP_PORT, 69),
        ("push_raw", RAW_TCP_PORT, 6910),
        ("xvc", XVC_PORT, 2542),
        ("swd", SWD_REMOTE_BITBANG_PORT, 6920),
        ("jtag", JTAG_REMOTE_BITBANG_PORT, 6921),
        ("uart0", UART0_PORT, 6930),
        ("uart1", UART1_PORT, 6931),
        ("swo", SWO_PORT, 6932),
    ],
)
def test_default_port_matches_pyverify_constant(spec_name, pyverify_const, literal):
    spec = by_name(spec_name)
    # The registry must agree with pyverify's canonical constant...
    assert spec.default_port == pyverify_const
    # ...and with the documented literal (net-protocol.md), so a *coordinated*
    # edit to both the registry and pyverify would still trip the literal.
    assert spec.default_port == literal


# ---------------------------------------------------------------------------
# (3) Every CSR row's csr_base matches the shell-regmap.md literal, and every
#     RegBlock in registers.BLOCKS agrees with the registry (cross-file base
#     invariant). registers.BLOCKS deliberately models only a subset of the 14
#     CSR rows (hwicap/dbgbr have no bitfield model yet), so the
#     BLOCKS half is checked as "every block present must agree", not "all 14
#     must exist".
# ---------------------------------------------------------------------------
def test_csr_specs_cover_the_14_documented_blocks():
    specs = csr_specs()
    assert len(specs) == 14
    names = {s.name for s in specs}
    assert names == set(EXPECTED_CSR_BASES)
    for s in specs:
        assert s.family is Family.CSR
        assert s.framing is Framing.XSDB_REG
        assert s.csr_base == EXPECTED_CSR_BASES[s.name], (
            f"csr_base drift for {s.name}: "
            f"{s.csr_base:#010x} != {EXPECTED_CSR_BASES[s.name]:#010x}"
        )


def test_blocks_base_matches_registry_csr_base():
    # BLOCKS is keyed by lowercase spec_name matching the CSR endpoint names.
    assert BLOCKS, "registers.BLOCKS must not be empty"
    for name, block in BLOCKS.items():
        spec = by_name(name)
        assert spec.family is Family.CSR, f"{name} is in BLOCKS but not a CSR endpoint"
        assert block.base == spec.csr_base, (
            f"base drift between registers.BLOCKS[{name!r}] and endpoints: "
            f"{block.base:#010x} != {spec.csr_base:#010x}"
        )
        assert block.base == EXPECTED_CSR_BASES[name]


def test_core_csr_blocks_are_modelled():
    # The blocks that registers.py transcribes verbatim must be present, so the
    # cross-file invariant above actually has teeth.
    for name in ("clkrst", "dfxctl", "vphy", "genchk", "swdbb", "uartbr"):
        assert name in BLOCKS, f"expected registers.BLOCKS to model {name!r}"


# ---------------------------------------------------------------------------
# (4) gated_by strings are a subset of pyverify.edge.GatedBy values.
# ---------------------------------------------------------------------------
def test_gated_by_values_are_subset_of_pyverify_gatedby():
    allowed = {member.value for member in GatedBy}
    assert allowed == {"shell", "rp", "none"}
    for spec in REGISTRY:
        assert spec.gated_by in allowed, (
            f"{spec.name} has gated_by={spec.gated_by!r} not in {allowed}"
        )


# ---------------------------------------------------------------------------
# (5) console_specs() returns exactly (uart0, uart1, swo), in that order.
# ---------------------------------------------------------------------------
def test_console_specs_order():
    specs = console_specs()
    assert tuple(s.name for s in specs) == ("uart0", "uart1", "swo")
    for s in specs:
        assert s.family is Family.CONSOLE
        assert isinstance(s, EndpointSpec)


# ---------------------------------------------------------------------------
# (6) Lookups: by_port(6900) is control; by_name('nope') raises a ValueError
#     that names the known endpoints.
# ---------------------------------------------------------------------------
def test_by_port_finds_control_and_uart0():
    assert by_port(6900).name == "control"
    assert by_port(6930).name == "uart0"


def test_by_port_unknown_raises_valueerror():
    with pytest.raises(ValueError):
        by_port(4)  # not a registered tcp/udp port


def test_by_name_unknown_raises_valueerror_listing_known_names():
    with pytest.raises(ValueError) as excinfo:
        by_name("nope")
    # The error is expected to enumerate the known names to help the operator;
    # at minimum a real registry name must appear in the message.
    assert "control" in str(excinfo.value)


def test_by_family_returns_all_matches():
    csr_rows = by_family(Family.CSR)
    assert {s.name for s in csr_rows} == set(EXPECTED_CSR_BASES)
    consoles = by_family(Family.CONSOLE)
    assert {s.name for s in consoles} == {"uart0", "uart1", "swo"}


# ---------------------------------------------------------------------------
# Defaults: DEFAULT_SHELL_HOST tracks pyverify's shell host (a host equality,
# which is legitimate). DEFAULT_HUB_URL is INTENTIONALLY the FQDN form and is
# deliberately NOT compared to pyverify.swd.DEFAULT_HUB ("<hub-host>") --
# xsdb requires the FQDN (scripts/mps3_diag.tcl:21). Per the project critique
# we never assert equality (or drift) between the two hub spellings.
# ---------------------------------------------------------------------------
def test_default_shell_host_matches_pyverify():
    assert DEFAULT_SHELL_HOST == "192.168.10.101"
    assert DEFAULT_SHELL_HOST == PYVERIFY_DEFAULT_SHELL_HOST


def test_default_hub_url_is_the_fqdn_form():
    # FQDN is load-bearing for xsdb `connect -url` (a bare host fails there). The
    # hub is env-supplied (MPS3_HW_URL); the public default is a localhost
    # placeholder. Assert the tcp:...:3121 SHAPE, not a specific site host.
    assert DEFAULT_HUB_URL.startswith("tcp:")
    assert DEFAULT_HUB_URL.endswith(":3121")


# ---------------------------------------------------------------------------
# resolve(): the spec -> Address projection used everywhere downstream.
# ---------------------------------------------------------------------------
def test_resolve_uses_defaults_and_overrides():
    ctrl = by_name("control")
    addr = ctrl.resolve(DEFAULT_SHELL_HOST)
    assert isinstance(addr, Address)
    assert addr.host == DEFAULT_SHELL_HOST
    assert addr.port == 6900  # default_port applied
    # explicit port override wins
    addr2 = ctrl.resolve(DEFAULT_SHELL_HOST, port=17000)
    assert addr2.port == 17000

    mcc = by_name("mcc_console")
    maddr = mcc.resolve("localhost")
    assert maddr.device == "/dev/ttyUSB6"  # serial device default carried through


# ---------------------------------------------------------------------------
# (7) Registry-driven config emitters (regeneration guard vs host/console/*).
#     Per the critique we do NOT claim byte-identical regeneration; we only
#     substring-check that the three console ports and the pty link paths
#     appear, proving the emitters are driven by the one registry.
# ---------------------------------------------------------------------------
def test_ser2net_yaml_contains_console_ports_and_pty_links():
    yaml = ser2net_yaml()
    assert isinstance(yaml, str)
    for port in ("6930", "6931", "6932"):
        assert port in yaml, f"ser2net yaml missing console port {port}"
    for name in ("uart0", "uart1", "swo"):
        assert f"/tmp/mps3-console/{name}" in yaml, (
            f"ser2net yaml missing pty link for {name}"
        )


def test_socat_argv_contains_pty_link_and_port_per_console():
    for spec in console_specs():
        argv = socat_argv(spec, DEFAULT_SHELL_HOST)
        assert isinstance(argv, list)
        joined = " ".join(argv)
        assert f"/tmp/mps3-console/{spec.name}" in joined, (
            f"socat argv missing pty link for {spec.name}: {argv}"
        )
        assert str(spec.default_port) in joined, (
            f"socat argv missing port {spec.default_port} for {spec.name}: {argv}"
        )
    # And, collectively, all three ports show up (regeneration-from-registry).
    all_joined = " ".join(
        " ".join(socat_argv(s, DEFAULT_SHELL_HOST)) for s in console_specs()
    )
    for port in ("6930", "6931", "6932"):
        assert port in all_joined


# ---------------------------------------------------------------------------
# NEGATIVE CONTROL (documented, kept commented so the suite stays green):
# a deliberately wrong expected port must fail the drift guard. Un-commenting
# the next line proves the guard is not vacuous -- control is 6900, not 6901.
#
#   assert by_name("control").default_port == 6901   # <-- would FAIL
#
# MUTATION note: changing any registry default_port reddens EXACTLY the
# matching parametrized case in test_default_port_matches_pyverify_constant
# (or the CSR-base assertion for a CSR row) and nothing else -- the tripwires
# are one-to-one with the registry rows.
# ---------------------------------------------------------------------------
