"""Tests for pyverify.edge — the MPS3 Edge Device API model
(docs/HARDWARE_HUB_INTEGRATION.md). Pure logic only: channel enumeration,
the jtag/xvc conflict marking, two-level FpgaStatus assembly from a mock
ShellClient-shaped object, and the reset-kind -> invalidation-set
computation. No sockets, no subprocess, no hardware.
"""
from __future__ import annotations

import pytest

from pyverify.client import ShellProtocolError
from pyverify.edge import (
    ALL_CHANNEL_IDS,
    MPS3_CHANNELS,
    ChannelHandle,
    ChannelType,
    EdgeApiError,
    EdgeDeviceApi,
    GatedBy,
    JtagDoneResult,
    ResetKind,
    channel_to_dict,
    find_conflicts,
    fpga_status_to_dict,
    is_valid_board_name,
    reset,
    reset_result_to_dict,
)


# --------------------------------------------------------------------------- #
# enumerate_channels() — integration-doc §2
# --------------------------------------------------------------------------- #


def test_enumerate_channels_returns_the_full_section_2_table() -> None:
    api = EdgeDeviceApi()
    channels = api.enumerate_channels()
    ids = {c.id for c in channels}
    assert ids == {
        "console", "dut-uart", "swo", "mgmt", "dut-net", "jtag", "xvc", "swd",
    }
    # returns a fresh list each call, not a reference to the frozen tuple
    assert channels is not MPS3_CHANNELS
    assert channels == list(MPS3_CHANNELS)


def test_enumerate_channels_is_pure_no_args_needed() -> None:
    # No shell/jtag_probe injected — enumeration never touches either seam.
    api = EdgeDeviceApi(shell=None)
    assert len(api.enumerate_channels()) == 8


@pytest.mark.parametrize(
    "channel_id,expected_handle,expected_type,expected_gated_by",
    [
        ("console", ChannelHandle.UART, ChannelType.UART, GatedBy.SHELL),
        ("dut-uart", ChannelHandle.UART, ChannelType.UART, GatedBy.RP),
        ("swo", ChannelHandle.UART, ChannelType.UART, GatedBy.RP),
        ("mgmt", ChannelHandle.MGMT, ChannelType.ETHERNET_MGMT, GatedBy.SHELL),
        ("dut-net", ChannelHandle.DUT_NET, ChannelType.ETHERNET_DUT, GatedBy.RP),
        ("jtag", ChannelHandle.JTAG, ChannelType.JTAG, GatedBy.NONE),
        ("xvc", ChannelHandle.XVC, ChannelType.XVC, GatedBy.SHELL),
        ("swd", ChannelHandle.SWD, ChannelType.SWD_OVER_ETH, GatedBy.RP),
    ],
)
def test_channel_enums_match_section_2(
    channel_id: str, expected_handle: ChannelHandle, expected_type: ChannelType,
    expected_gated_by: GatedBy,
) -> None:
    by_id = {c.id: c for c in MPS3_CHANNELS}
    channel = by_id[channel_id]
    assert channel.handle == expected_handle
    assert channel.type == expected_type
    assert channel.gated_by == expected_gated_by


def test_jtag_xvc_conflict_marked_both_ways() -> None:
    conflicts = find_conflicts()
    assert conflicts == {"jtag": ("xvc",), "xvc": ("jtag",)}


def test_no_other_channel_declares_a_conflict() -> None:
    conflicts = find_conflicts()
    assert set(conflicts) == {"jtag", "xvc"}


def test_channel_to_dict_shape() -> None:
    by_id = {c.id: c for c in MPS3_CHANNELS}
    d = channel_to_dict(by_id["mgmt"])
    assert d["id"] == "mgmt"
    assert d["handle"] == "mgmt"
    assert d["type"] == "ethernet-mgmt"
    assert d["gated_by"] == "shell"
    assert d["endpoint"] == {"transport": "tcp", "ports": [6900, 69, 6910], "netns": None}
    assert d["conflicts"] == []


def test_dut_net_endpoint_uses_vxlan_l2_and_netns() -> None:
    by_id = {c.id: c for c in MPS3_CHANNELS}
    dut_net = by_id["dut-net"]
    assert dut_net.endpoint.transport == "vxlan-l2"
    assert dut_net.endpoint.netns == "board-<id>"


# --------------------------------------------------------------------------- #
# status_fpga() — integration-doc §3 two-level FpgaStatus
# --------------------------------------------------------------------------- #


class _MockPing:
    def __init__(self, ok: bool, shell_id: str = "", rm_id: str = "") -> None:
        self.ok = ok
        self.shell_id = shell_id
        self.rm_id = rm_id


class _MockShellClient:
    """Bare object satisfying ShellStatusClient's structural ``.ping()``
    requirement — deliberately *not* a real pyverify.client.ShellClient, to
    prove the seam is structural, not a hard import dependency.
    """

    def __init__(self, ping_response: _MockPing | Exception) -> None:
        self._ping_response = ping_response

    def ping(self) -> _MockPing:
        if isinstance(self._ping_response, Exception):
            raise self._ping_response
        return self._ping_response


def _jtag_done(static_id: str = "0xA1B2C3D4") -> JtagDoneResult:
    return JtagDoneResult(done=True, static_id=static_id, usercode=None)


# rm_id encoding v2 (docs/VERSIONING_PLAN.md §3.2):
#   { ver_major[31:24], ver_minor[23:16], design_id[15:0] }
# The DESIGN half is the stable identity; the top half is a version that
# legitimately changes on every bump. These fixtures use real-shaped ids so the
# tests exercise what a board actually reports.
NANOSOC_V1_0 = "0x01000001"   # design 0x0001 @ v1.0 — what ships today
NANOSOC_V1_1 = "0x01010001"   # design 0x0001 @ v1.1 — the NEXT version bump
ETH_SS_V1_0 = "0x01000002"    # design 0x0002 @ v1.0 — a DIFFERENT design


def test_status_fpga_shell_loaded_and_rp_loaded_with_known_name() -> None:
    shell = _MockShellClient(_MockPing(ok=True, shell_id="0xA1B2C3D4", rm_id=NANOSOC_V1_0))
    api = EdgeDeviceApi(
        shell=shell,
        jtag_probe=_jtag_done,
        known_rm_names={NANOSOC_V1_0: "nanosoc"},
    )
    status = api.status_fpga()

    assert status.board == "mps3_01"
    assert status.shell.loaded is True
    assert status.shell.derived_by == "jtag-done"
    assert status.shell.static_id == "0xA1B2C3D4"

    assert status.rp.loaded is True
    assert status.rp.derived_by == "shell-control"
    assert status.rp.rm_id == NANOSOC_V1_0
    assert status.rp.rm_name == "nanosoc"


def test_status_fpga_rm_name_survives_a_design_version_bump() -> None:
    """THE regression test for the v2 encoding's whole hazard.

    The board is running a NEWER version of a design the registry already
    knows. Its full 32-bit rm_id has therefore changed (v1.0 0x01000001 ->
    v1.1 0x01010001) — but it is still nanosoc, and the hub must still say so.

    A registry lookup keyed on the full 32-bit id (which is what this did
    before 2026-07-14) returns ``rm_name=None`` here: it "forgets" a design the
    instant it is re-versioned, and status.fpga reports a nameless RM. The
    lookup keys on ``rm_id & 0xFFFF`` precisely so that this passes.
    """
    shell = _MockShellClient(_MockPing(ok=True, rm_id=NANOSOC_V1_1))
    api = EdgeDeviceApi(
        shell=shell,
        jtag_probe=_jtag_done,
        known_rm_names={NANOSOC_V1_0: "nanosoc"},   # registry knows only v1.0
    )
    status = api.status_fpga()
    assert status.rp.loaded is True
    assert status.rp.rm_id == NANOSOC_V1_1          # reported verbatim, version and all
    assert status.rp.rm_name == "nanosoc"           # ...but still RECOGNISED


def test_status_fpga_rm_name_registry_may_be_keyed_by_bare_design_id() -> None:
    """A registry keyed by the design half alone works too — the natural way to
    write one now that the version is not part of the identity."""
    shell = _MockShellClient(_MockPing(ok=True, rm_id=NANOSOC_V1_1))
    api = EdgeDeviceApi(shell=shell, jtag_probe=_jtag_done,
                        known_rm_names={"0x0001": "nanosoc"})
    assert api.status_fpga().rp.rm_name == "nanosoc"


def test_status_fpga_rm_name_does_not_confuse_two_designs() -> None:
    """Masking must not become a wildcard: a DIFFERENT design must not pick up
    nanosoc's name just because the version half matches."""
    shell = _MockShellClient(_MockPing(ok=True, rm_id=ETH_SS_V1_0))
    api = EdgeDeviceApi(shell=shell, jtag_probe=_jtag_done,
                        known_rm_names={NANOSOC_V1_0: "nanosoc"})
    status = api.status_fpga()
    assert status.rp.loaded is True
    assert status.rp.rm_id == ETH_SS_V1_0
    assert status.rp.rm_name is None


def test_status_fpga_rm_name_unknown_when_not_in_registry() -> None:
    shell = _MockShellClient(_MockPing(ok=True, rm_id=ETH_SS_V1_0))
    api = EdgeDeviceApi(shell=shell, jtag_probe=_jtag_done, known_rm_names={})
    status = api.status_fpga()
    assert status.rp.loaded is True
    assert status.rp.rm_id == ETH_SS_V1_0
    assert status.rp.rm_name is None


def test_status_fpga_rm_name_registry_with_unparsable_key_does_not_raise() -> None:
    """An unparsable registry key must degrade to "no name", never take down
    the whole status.fpga call."""
    shell = _MockShellClient(_MockPing(ok=True, rm_id=NANOSOC_V1_0))
    api = EdgeDeviceApi(shell=shell, jtag_probe=_jtag_done,
                        known_rm_names={"not-a-number": "junk"})
    status = api.status_fpga()
    assert status.rp.loaded is True
    assert status.rp.rm_name is None


def test_status_fpga_rp_not_loaded_when_rm_id_is_zero_greybox() -> None:
    # A zero rm_id means "no RM loaded" (idle/greybox state), not an error.
    shell = _MockShellClient(_MockPing(ok=True, shell_id="0xA1B2C3D4", rm_id="0x0"))
    api = EdgeDeviceApi(shell=shell, jtag_probe=_jtag_done)
    status = api.status_fpga()
    assert status.rp.loaded is False
    assert status.rp.rm_name is None


def test_status_fpga_rp_not_loaded_when_ping_not_ok() -> None:
    shell = _MockShellClient(_MockPing(ok=False))
    api = EdgeDeviceApi(shell=shell, jtag_probe=_jtag_done)
    status = api.status_fpga()
    assert status.rp.loaded is False


def test_status_fpga_rp_not_loaded_when_shell_control_channel_errors() -> None:
    shell = _MockShellClient(ShellProtocolError("shell control channel closed by peer"))
    api = EdgeDeviceApi(shell=shell, jtag_probe=_jtag_done)
    status = api.status_fpga()
    assert status.rp.loaded is False
    assert status.rp.derived_by == "shell-control"


def test_status_fpga_rp_not_loaded_when_no_shell_injected() -> None:
    api = EdgeDeviceApi(shell=None)
    status = api.status_fpga()
    assert status.rp.loaded is False
    assert status.shell.loaded is False  # default stub_jtag_done_probe: honestly "unknown"


def test_status_fpga_shell_not_loaded_by_default_stub() -> None:
    # No jtag_probe injected: the stub is conservative ("unknown" != "up").
    api = EdgeDeviceApi()
    status = api.status_fpga()
    assert status.shell.loaded is False
    assert status.shell.static_id is None


def test_fpga_status_to_dict_shape() -> None:
    shell = _MockShellClient(_MockPing(ok=True, rm_id="0x1"))
    api = EdgeDeviceApi(shell=shell, jtag_probe=_jtag_done, known_rm_names={"0x1": "nanosoc"})
    d = fpga_status_to_dict(api.status_fpga())
    assert d == {
        "board": "mps3_01",
        "shell": {"loaded": True, "derived_by": "jtag-done", "static_id": "0xA1B2C3D4"},
        "rp": {"loaded": True, "derived_by": "shell-control", "rm_id": "0x1", "rm_name": "nanosoc"},
    }


# --------------------------------------------------------------------------- #
# reset() — integration-doc §4 invalidation-set logic
# --------------------------------------------------------------------------- #


def test_dfx_swap_invalidates_rp_derived_channels_and_xvc() -> None:
    """Every rp-gated channel, plus xvc: its server is shell firmware, but the
    ILAs behind it are the RM's own, so the session dies with the RM."""
    result = reset(ResetKind.DFX_SWAP)
    assert set(result.invalidated_channels) == {"swd", "dut-net", "dut-uart", "swo", "xvc"}
    assert result.touches_shell is False
    assert result.touches_rp is True
    assert result.rp_scoped is True


def test_dfx_swap_survivors_are_mgmt_jtag_console() -> None:
    result = reset(ResetKind.DFX_SWAP)
    survivors = set(ALL_CHANNEL_IDS) - set(result.invalidated_channels)
    assert survivors == {"mgmt", "jtag", "console"}


def test_mcc_reconfig_invalidates_all_channels() -> None:
    result = reset(ResetKind.MCC_RECONFIG)
    assert set(result.invalidated_channels) == set(ALL_CHANNEL_IDS)
    assert result.touches_shell is True
    assert result.touches_rp is True
    assert result.rp_scoped is False


def test_usb_power_also_invalidates_all_channels() -> None:
    result = reset(ResetKind.USB_POWER)
    assert set(result.invalidated_channels) == set(ALL_CHANNEL_IDS)
    assert result.touches_shell is True
    assert result.touches_rp is True


@pytest.mark.parametrize("kind", [ResetKind.DUT_RESET, ResetKind.UART_SOFT, ResetKind.SYSTEM])
def test_soft_resets_invalidate_nothing(kind: ResetKind) -> None:
    result = reset(kind)
    assert result.invalidated_channels == []
    assert result.touches_shell is False
    assert result.touches_rp is False
    assert result.rp_scoped is False


def test_reset_accepts_plain_string_kind() -> None:
    result = reset("dfx-swap")
    assert result.kind == "dfx-swap"
    assert set(result.invalidated_channels) == {"swd", "dut-net", "dut-uart", "swo", "xvc"}


def test_reset_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        reset("not-a-real-kind")


def test_edge_device_api_reset_method_matches_module_function() -> None:
    api = EdgeDeviceApi()
    assert api.reset("mcc-reconfig") == reset("mcc-reconfig")


def test_reset_result_to_dict_shape() -> None:
    d = reset_result_to_dict(reset(ResetKind.DFX_SWAP))
    assert d["kind"] == "dfx-swap"
    assert set(d["invalidated_channels"]) == {"swd", "dut-net", "dut-uart", "swo", "xvc"}
    assert d["rp_scoped"] is True


# --------------------------------------------------------------------------- #
# dispatch() — hardwarehub.md §5.3 JSON-RPC method-name mapping
# --------------------------------------------------------------------------- #


def test_dispatch_enumerate_channels() -> None:
    api = EdgeDeviceApi()
    result = api.dispatch("enumerate.channels")
    assert len(result["channels"]) == 8
    assert {c["id"] for c in result["channels"]} == {c.id for c in MPS3_CHANNELS}


def test_dispatch_status_fpga() -> None:
    shell = _MockShellClient(_MockPing(ok=True, rm_id="0x1"))
    api = EdgeDeviceApi(shell=shell, jtag_probe=_jtag_done)
    result = api.dispatch("status.fpga")
    assert result["board"] == "mps3_01"
    assert result["rp"]["loaded"] is True


def test_dispatch_reset() -> None:
    api = EdgeDeviceApi()
    result = api.dispatch("reset", {"kind": "dfx-swap"})
    assert set(result["invalidated_channels"]) == {"swd", "dut-net", "dut-uart", "swo", "xvc"}


def test_dispatch_reset_missing_kind_raises() -> None:
    api = EdgeDeviceApi()
    with pytest.raises(EdgeApiError, match="kind"):
        api.dispatch("reset", {})


def test_dispatch_reset_bad_kind_raises_edge_api_error() -> None:
    api = EdgeDeviceApi()
    with pytest.raises(EdgeApiError):
        api.dispatch("reset", {"kind": "not-a-real-kind"})


def test_dispatch_unknown_method_raises() -> None:
    api = EdgeDeviceApi()
    with pytest.raises(EdgeApiError, match="unknown method"):
        api.dispatch("status.pi")


# --------------------------------------------------------------------------- #
# Board identity
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["mps3_01", "mps3_2", "mps3_123"])
def test_valid_board_names(name: str) -> None:
    assert is_valid_board_name(name) is True


@pytest.mark.parametrize("name", ["pynq_z2_03", "mps3", "mps3_", "MPS3_01"])
def test_invalid_board_names(name: str) -> None:
    assert is_valid_board_name(name) is False
