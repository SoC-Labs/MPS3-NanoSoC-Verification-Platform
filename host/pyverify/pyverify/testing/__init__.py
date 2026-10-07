"""``pyverify.testing`` — executable behaviour models + a full fake shell.

Everything here is test/verification infrastructure (nothing in the
production ``pyverify`` modules imports it), but it is *packaged* — not
buried in a test file — because it is shared across suites and doubles as
the executable spec for the A3 firmware:

- :mod:`pyverify.testing.swap_model` — importable Python port of the
  firmware swap FSM (``firmware/coordinator/swap_fsm.c`` +
  ``swap_fsm_transitions.c``), extracted from
  ``tests/integration/test_swap_sequence.py`` (which still cross-checks it
  against the C's own fixture cases).
- :mod:`pyverify.testing.fakeshell` — stdlib-only threaded server
  implementing the shell's whole network surface per
  ``docs/contracts/net-protocol.md`` (control verbs, TFTP + raw-TCP
  bitstream receive with full config-agent validation, consoles), driven
  by the swap model. Lets the host stack run end-to-end with no hardware.
- :mod:`pyverify.testing._pusher` — tolerant single-source import of the
  pusher's real bitstream framing (works before and after the W-PKG move
  of ``host/pusher/push.py`` into ``pyverify.pusher``).
"""
from __future__ import annotations

from .fakeshell import (
    BitstreamInfo,
    ConfigAgentModel,
    ConfigAgentStatus,
    FakeShell,
    PushEvent,
    TftpError,
    raw_tcp_put,
    tftp_put,
)
from .swap_model import (
    STATES,
    STATIC_ID,
    ClearingRef,
    MockRegs,
    SwapFsm,
    greybox_clearing,
    make_regs_confirmed,
    next_state,
    validate_header_like_config_agent,
)

__all__ = [
    # fakeshell
    "FakeShell",
    "ConfigAgentModel",
    "ConfigAgentStatus",
    "BitstreamInfo",
    "PushEvent",
    "TftpError",
    "tftp_put",
    "raw_tcp_put",
    # swap model
    "STATES",
    "STATIC_ID",
    "ClearingRef",
    "MockRegs",
    "SwapFsm",
    "greybox_clearing",
    "make_regs_confirmed",
    "next_state",
    "validate_header_like_config_agent",
]
