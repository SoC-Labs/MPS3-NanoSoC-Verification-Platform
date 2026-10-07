"""pyverify — host-side verification library for the MPS3 nanoSoC platform.

Speaks the client side of every contract in ``docs/contracts/``:

- :mod:`pyverify.client`  — control channel (net-protocol.md, TCP 6900)
- :mod:`pyverify.mactest` — MAC-in-operation test driver (macgen/GENCHK, spec §8)
- :mod:`pyverify.overlay` — overlay manifest load + validate (overlay-manifest.md)
- :mod:`pyverify.pusher`  — bitstream framing + TFTP/raw-TCP push (net-protocol.md, 69/6910)
- :mod:`pyverify.swap`    — deploy orchestration (spec §6.2/§6.3)
- :mod:`pyverify.console` — UART/SWO TCP consoles (net-protocol.md, 6930-6932)
- :mod:`pyverify.debug`   — OpenOCD/XVC launch wrappers (net-protocol.md, 6920/2542)
- :mod:`pyverify.swd`     — :class:`SwdDebugger`, the high-level SWD debug API
  (dpidr/halt/read/write/load_image over OpenOCD on the <hub-host> hub)
- :mod:`pyverify.edge`    — MPS3 Edge Device API model (HARDWARE_HUB_INTEGRATION.md)
- :mod:`pyverify.board`   — :class:`Mps3Board`, the "PYNQ experience" session facade

:mod:`pyverify.cli` (``python -m pyverify.cli deploy ...``) is the
command-line front-end; :class:`pyverify.board.Mps3Board` is the
interactive/notebook front-end — both wire the same
:class:`~pyverify.pusher.BitstreamPusher` into
:class:`~pyverify.swap.SwapOrchestrator`.

See ``host/README.md`` for how these compose into the "select DUT -> load
firmware -> run test -> check" loop, and ``host/notebooks/demo.md`` for a
worked Jupyter cell sequence.
"""
from __future__ import annotations

from .board import Mps3Board
from .client import (
    CONTROL_PORT,
    DEFAULT_CLK_PRESETS,
    MACGEN_INJECTS,
    CommitResponse,
    LinkResponse,
    MacGenResponse,
    PingResponse,
    VersionResponse,
    ResetResponse,
    SetClkResponse,
    ShellClient,
    ShellProtocolError,
    SwapResponse,
    TelemetryResponse,
    UsdResponse,
    validate_clk_preset,
    validate_macgen_inject,
)
from .mactest import MacTestResult, run_mac_test
from .console import SWO_PORT, UART0_PORT, UART1_PORT, ConsoleReader
from .debug import (
    SWD_REMOTE_BITBANG_PORT,
    XVC_PORT,
    OpenOcdRemoteBitbangConfig,
    XvcTarget,
    launch_openocd,
    launch_vivado_xvc_tcl,
)
from .swd import (
    BOOTROM_BASE,
    DEFAULT_HUB,
    DEFAULT_REPO_DIR,
    DEFAULT_SHELL_HOST,
    DMEM_BASE,
    DMEM_SIZE,
    EXPECTED_CPUID_CM0,
    EXPECTED_CPUID_CM0PLUS,
    EXPECTED_DPIDR_CM0,
    EXPECTED_DPIDR_CM0PLUS,
    IMEM_BASE,
    IMEM_REMAP_BASE,
    IMEM_SIZE,
    SCS_CPUID,
    SWD_PORT,
    SwdConfig,
    SwdDebugger,
    SwdError,
    parse_downloaded_bytes,
    parse_dpidr,
    parse_mdw,
    parse_reg,
)
from .edge import (
    ALL_CHANNEL_IDS,
    Channel,
    ChannelEndpoint,
    ChannelHandle,
    ChannelType,
    EdgeApiError,
    EdgeDeviceApi,
    FpgaStatus,
    GatedBy,
    JtagDoneResult,
    MPS3_CHANNELS,
    ResetKind,
    ResetResult,
    RpLevelStatus,
    ShellLevelStatus,
    channel_to_dict,
    find_conflicts,
    fpga_status_to_dict,
    is_valid_board_name,
    reset_result_to_dict,
)
from .overlay import (
    Overlay,
    OverlayFile,
    OverlayManifest,
    OverlayManifestError,
    OverlayValidationError,
)
# NB: rm_id.design_id()/version() are NOT re-exported bare -- `version` would
# read as the package version. Reach them as `pyverify.rm_id.design_id(...)`.
from .rm_id import (
    DESIGN_ID_MASK,
    GREYBOX_RM_ID,
    RmVersion,
    format_rm_id,
    is_greybox,
    make_rm_id,
    parse_rm_id,
    same_design,
)
from .pusher import (
    HEADER_SIZE,
    MAGIC,
    RAW_TCP_PORT,
    TFTP_PORT,
    BitstreamFramingError,
    BitstreamHeader,
    BitstreamKind,
    BitstreamPusher,
    PushError,
    PushResult,
    frame_bitstream,
    tcp_send,
    tftp_put,
    unframe_bitstream,
)
from .swap import (
    PersistResult,
    Pusher,
    ReattachPlan,
    SwapDeployResult,
    SwapError,
    SwapOrchestrator,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # client
    "CONTROL_PORT",
    "DEFAULT_CLK_PRESETS",
    "MACGEN_INJECTS",
    "validate_clk_preset",
    "validate_macgen_inject",
    "ShellClient",
    "ShellProtocolError",
    "PingResponse",
    "VersionResponse",
    "ResetResponse",
    "SetClkResponse",
    "SwapResponse",
    "LinkResponse",
    "CommitResponse",
    "TelemetryResponse",
    "MacGenResponse",
    "UsdResponse",          # v0.13: the user microSD
    # mactest (MAC-in-operation driver)
    "MacTestResult",
    "run_mac_test",
    # overlay
    "Overlay",
    "OverlayFile",
    "OverlayManifest",
    "OverlayManifestError",
    "OverlayValidationError",
    # rm_id encoding v2 (VERSIONING_PLAN.md §3.2)
    "DESIGN_ID_MASK",
    "GREYBOX_RM_ID",
    "RmVersion",
    "format_rm_id",
    "is_greybox",
    "make_rm_id",
    "parse_rm_id",
    "same_design",
    # pusher
    "MAGIC",
    "HEADER_SIZE",
    "TFTP_PORT",
    "RAW_TCP_PORT",
    "BitstreamFramingError",
    "BitstreamHeader",
    "BitstreamKind",
    "BitstreamPusher",
    "PushError",
    "PushResult",
    "frame_bitstream",
    "unframe_bitstream",
    "tftp_put",
    "tcp_send",
    # swap
    "Pusher",
    "SwapError",
    "ReattachPlan",
    "SwapDeployResult",
    "SwapOrchestrator",
    "PersistResult",        # v0.13: deploy(persist=True)
    # board (the "PYNQ experience" facade)
    "Mps3Board",
    # console
    "ConsoleReader",
    "UART0_PORT",
    "UART1_PORT",
    "SWO_PORT",
    # debug
    "OpenOcdRemoteBitbangConfig",
    "XvcTarget",
    "SWD_REMOTE_BITBANG_PORT",
    "XVC_PORT",
    "launch_openocd",
    "launch_vivado_xvc_tcl",
    # swd (high-level SWD debug API)
    "SwdDebugger",
    "SwdConfig",
    "SwdError",
    "SWD_PORT",
    "DEFAULT_HUB",
    "DEFAULT_SHELL_HOST",
    "DEFAULT_REPO_DIR",
    "IMEM_BASE",
    "IMEM_SIZE",
    "IMEM_REMAP_BASE",
    "DMEM_BASE",
    "DMEM_SIZE",
    "BOOTROM_BASE",
    "SCS_CPUID",
    "EXPECTED_DPIDR_CM0",
    "EXPECTED_DPIDR_CM0PLUS",
    "EXPECTED_CPUID_CM0",
    "EXPECTED_CPUID_CM0PLUS",
    "parse_dpidr",
    "parse_reg",
    "parse_mdw",
    "parse_downloaded_bytes",
    # edge
    "ALL_CHANNEL_IDS",
    "Channel",
    "ChannelEndpoint",
    "ChannelHandle",
    "ChannelType",
    "EdgeApiError",
    "EdgeDeviceApi",
    "FpgaStatus",
    "GatedBy",
    "JtagDoneResult",
    "MPS3_CHANNELS",
    "ResetKind",
    "ResetResult",
    "RpLevelStatus",
    "ShellLevelStatus",
    "channel_to_dict",
    "find_conflicts",
    "fpga_status_to_dict",
    "is_valid_board_name",
    "reset_result_to_dict",
]
