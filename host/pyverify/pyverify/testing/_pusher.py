"""Bitstream-framing names for ``pyverify.testing``.

The 24-byte ``"MPS3"`` header + CRC32 framing (net-protocol.md "Bitstream
framing") has exactly ONE implementation in this repo: ``pyverify.pusher``.
This module used to be a tolerant loader that also accepted the pre-package
layout in ``host/pusher/push.py``; that file and its re-export shim were
retired on 2026-09-09, so the fallback -- and the two-module-objects hazard
it existed to manage -- went with them. Keep importing the framing names from
HERE inside ``pyverify.testing`` and its tests, so a future move of the
implementation has one line to follow.
"""
from __future__ import annotations

from pyverify.pusher import (
    HEADER_SIZE,
    MAGIC,
    RAW_TCP_PORT,
    TFTP_PORT,
    BitstreamFramingError,
    BitstreamHeader,
    BitstreamKind,
    frame_bitstream,
    unframe_bitstream,
)

__all__ = [
    "MAGIC",
    "HEADER_SIZE",
    "TFTP_PORT",
    "RAW_TCP_PORT",
    "BitstreamFramingError",
    "BitstreamKind",
    "BitstreamHeader",
    "frame_bitstream",
    "unframe_bitstream",
]
