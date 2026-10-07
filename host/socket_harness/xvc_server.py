from __future__ import annotations

"""xvc_server.py — a host-side **Xilinx Virtual Cable (XVC) 1.0** server that
bit-bangs a soft JTAG TAP inside the MPS3 DFX reconfigurable module through four
registers the shell already ships.

This is the host leg of ``docs/planning/IDENTIFY_IICE_DFX_PLAN.md`` §3(a). It
exists so the Synopsys Identify debugger can reach an IICE living *inside* the
RP, which cannot instantiate a ``BSCANE2`` (§2, ``HDPR-16``), **without adding a
single partition pin**: no contract edit, no ``pin_check.py`` change (still 35
ports), no shell rebuild, no ``static_id`` re-mint, no overlay re-key.

    identify_debugger  (com cabletype XilinxVirtualCable)
        |  TCP, XVC 1.0: getinfo: / settck: / shift:
        v
    THIS SERVER  (XvcServer -> XvcSession -> SwdbbJtagShifter)
        |  xsdb  mwr/mrd  over  hw_server tcp:<hub-host>:3121
        v
    SWDBB @ 0x44A7_0000   (fpga/shell/ip/swd_bb/swd_bb.sv)
        |  four DUT partition pins (docs/contracts/partition-pins.md:48-54)
        v
    RM soft TAP  (`device jtagport soft`)  ->  IICE

**Pin reuse.** ``swd_bb`` is deliberately dumb — "register state wired straight
to the SWD partition pins, plus one synchronized sample path back" — so the RM
is free to reinterpret the four wires. Reinterpreting is a partial-bitstream-only
change; *replacing* ``swd_bb_0`` with a ``jtag_bb_0`` (as
``docs/planning/JTAG_UART_REMINT_PLAN.md`` proposes) would be a static rebuild
and a re-mint. That difference is the whole point of this file.

===========================================================================
Register map (``docs/contracts/shell-regmap.md`` §SWDBB, ``swd_bb.sv:187,243``)
===========================================================================
| Reg      | Off  | Bit | Partition pin  | Reused here as |
|----------|------|-----|----------------|----------------|
| ``DRIVE``  | 0x00 rw | [0] | ``swd_clk``     | **TCK** |
| ``DRIVE``  | 0x00 rw | [1] | ``swd_dio_o``   | **TMS** |
| ``DRIVE``  | 0x00 rw | [2] | ``swd_dio_oe``  | **TDI** |
| ``SAMPLE`` | 0x04 ro | [0] | ``swd_dio_i``   | **TDO** |

Cost of the reuse: SWD access to the M0 is unavailable while Identify owns the
pins (plan §3 lists the two accepted outs). Nothing here mediates that — take
the pins for the experiment, then stop the server.

===========================================================================
Bit order and TDO sampling phase — the one thing that must be right
===========================================================================
Two independent conventions, both pinned by unit tests against
:class:`FakeJtagTap` (a bit-order or phase error is otherwise invisible until
board day; cf. the ``I22`` SWD bit-order episode, ``OPEN_ISSUES.md`` §I22, which
was closed by *reading the host driver* rather than by bring-up):

1. **XVC vector packing is LSB-first within each byte.** Shift bit ``i`` lives at
   ``vec[i // 8] >> (i % 8) & 1``. This matches the C reference's
   ``vector_word()`` (``firmware/xvc_server/xvc_server.c:85-94``: "vector bit
   (8*byte_off + j) -> word bit j") and Xilinx XAPP1251. TDO is packed back the
   same way (:func:`bits_to_vector`).

2. **TDO is sampled in the TCK-low phase, immediately BEFORE the rising edge
   that consumes the matching TDI bit.** IEEE 1149.1: the TAP samples TMS/TDI on
   the rising edge of TCK and updates TDO on the falling edge, so while TCK is
   low the TAP is presenting the bit that the *upcoming* rising edge will shift
   out. Per JTAG bit this server therefore emits exactly::

       write DRIVE = {TDI, TMS, TCK=0}      # settle TMS/TDI with TCK low
       read  SAMPLE                          # <-- TDO for THIS bit
       write DRIVE = {TDI, TMS, TCK=1}      # rising edge: TAP consumes TMS/TDI

   This is byte-for-byte OpenOCD's ``bitbang.c`` scan loop (write TCK=0, read,
   write TCK=1) and it is what makes an IDCODE read come back aligned. Sampling
   after the rising edge instead yields ``IDCODE >> 1`` — which
   ``test_xvc_server.py`` demonstrates as an explicit negative control.

The 2-FF synchronizer on ``SAMPLE`` (``swd_bb.sv:255-263``) is invisible here:
its 2 shell-clock latency (~20 ns) is five orders of magnitude below one AXI
round trip.

===========================================================================
Performance — why :class:`PersistentXsdbSession` is mandatory, not an optimisation
===========================================================================
Cost model (ESTIMATE — see the honesty note below):

* 3 register accesses per JTAG bit; a maximal 2048-bit ``shift:`` therefore costs
  ``3 * 2048 + 1 = 6145`` accesses (the ``+1`` parks TCK low).
* One remote AXI access through ``xsdb`` -> ``hw_server`` -> MicroBlaze MDM is
  ~1-5 ms (``IDENTIFY_IICE_DFX_PLAN.md`` §3a). With a **persistent** session all
  6145 land inside ONE ``eval`` round trip, so a full 2048-bit shift is roughly
  **6-31 s**, and an IICE download of 1024 samples x ~70 probe bits (~72 kbit,
  ~36 maximal shifts) is **~4-19 minutes**. That matches the plan's "minutes".
* :class:`~socket_harness.xsdb.XsdbRegisterEndpoint` instead spawns a fresh
  ``xsdb -eval`` per access (``xsdb.py:305-312``, ``timeout_s = 30.0``). At an
  optimistic 2 s of interpreter startup + ``connect`` per access that is
  ``6145 * 2 s ~= 3.4 hours`` for ONE shift and ~5 days for one trace. The
  persistent session is therefore a ~1000x reduction and the difference between
  "usable" and "impossible" — it comes entirely from deleting process spawn +
  ``connect``, not from any cleverness in the Tcl.
* The real fix is plan §3(b) and it is **now BUILT**: the same ``3n+1`` sequence
  runs *locally* inside the MicroBlaze, selected with
  ``-DMPS3_XVC_TARGET_SWDBB`` in ``firmware/xvc_server/xvc_server.c`` (see that
  module's README). One TCP round trip per 2048-bit shift instead of 6,145
  remote accesses ⇒ order 1 Mbit/s, a trace in tens of milliseconds. Its shift
  loop is the exact ordering of :meth:`SwdbbBackend.shift_drive_sample` below,
  and the two are cross-pinned: ``firmware/test/test_xvc_server_swdbb.c``
  asserts its masks/base/offsets/ceiling equal this module's, and both suites
  read the same ``IDCODE`` out of an equivalent in-memory TAP.
  This module remains the zero-firmware-change way to prove the chain first,
  and the ``--fake-tap`` mode remains the board-free way to check the host
  tooling either way.

**Honesty note: none of the above is measured.** Nothing in this file has ever
been run against a board, an ``xsdb``, or an ``hw_server``. The numbers are the
plan's per-access estimate multiplied out. Everything *proven* here is proven
against :class:`FakeJtagTap` / :class:`ScriptedXsdbProcess` in
``tests/test_xvc_server.py``.

===========================================================================
The advertised size is a CHUNK HINT, not a ceiling on ``num_bits``
===========================================================================
The one interoperability trap in this protocol, and it costs a whole debug
session if you get it wrong. ``getinfo:`` advertises a number; a client sizes the
**payload** of a ``shift:`` against it and then adds its own TAP
state-navigation TMS bits, so the ``num_bits`` that actually arrives OVERSHOOTS
the advertisement. Bounding ``num_bits`` by the advertised value drops the
connection on the client's first real scan, and the client-side message names
neither the shift nor the lengths — Identify says only::

    Error: Couldn't shift do data from xvcServer

Measured against the real Synopsys Identify debugger (T-2022.09-SP2) over
``--fake-tap``, which is how this was found:

===========  ==============================================================
advertised   what Identify then asks for
===========  ==============================================================
      1024   ``num_bits=1029``  (1024 payload + 5 navigation)
      2048   ``num_bits=2053``  (2048 payload + 5)
      8192   ``num_bits=3206``  (the whole scan fits; no chunking at all)
===========  ==============================================================

So :data:`XVC_MAX_VECTOR_BITS` is what we ADVERTISE and
:data:`XVC_ACCEPT_VECTOR_BITS` is what we ACCEPT, and they are deliberately
different numbers. Past the accept ceiling we still fail closed — a truncated
TDO reply is indistinguishable to the client from real captured data.

**The firmware server had the same trap; it is FIXED and now EXECUTED against
the real client.** ``firmware/xvc_server/xvc_server.c`` used to reject
``num_bits > MPS3_XVC_MAX_VECTOR_BITS`` exactly as this file did, and its command
accumulator ``XVC_CMD_BUF_MAX = 6 + 4 + 2 * 256 = 522`` bytes could not even hold
the 524-byte command a 2053-bit shift arrives in. Both halves were fixed in
f93d565 (``MPS3_XVC_ACCEPT_RATIO``), and since 2026-07-30 that fix is proven by
EXECUTION rather than by replay: ``firmware/test/bin/xvc_fw_daemon`` links that
same C engine against real POSIX sockets and an in-memory TAP, and the real
Identify debugger drives it through two 2053-bit shifts
(``host/identify/fw_com_check.sh``). The pre-fix build is kept as a checked-in
negative control (``bin/xvc_fw_daemon_ratio1``) and the debugger provably fails
against it — with a DIFFERENT message from this server's, worth knowing at triage
time: the firmware fails closed by dropping the TCP connection, so Identify says
``Error: Connection reset by peer`` rather than ``Couldn't shift do data from
xvcServer``.

===========================================================================
Diagnosing a shift failure: use ``--trace-file``, never stderr
===========================================================================
``--trace-file`` records every command — bit count, TMS/TDI/TDO byte lengths,
payload edges — straight to a file, flushed per record (:class:`XvcTrace`).
Reach for it FIRST. The reason the bug above took so long is that the per-shift
diagnostics were ``print(file=sys.stderr)`` and the server ran with stderr on a
pipe, so block buffering meant not one record survived the session teardown.

===========================================================================
Board-free discipline (this package is in the root ``make check`` gate)
===========================================================================
Import opens nothing, spawns nothing and needs no Vivado. Every seam is
injectable: the register transport (:class:`SwdbbBackend`), the ``xsdb`` process
(:class:`XsdbProcess`) and the TCP listener (a
:class:`~socket_harness.console_bridge.LocalEndpoint`). The protocol engine
(:class:`XvcSession`) is pure bytes-in/bytes-out, and script generation and
reply parsing are pure functions.
"""

import abc
import argparse
import os
import re
import select
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Protocol, Sequence, runtime_checkable

from .console_bridge import TcpListenEndpoint
from .endpoints import DEFAULT_HUB_URL
from .xsdb import XsdbConfig

__all__ = [
    # constants
    "XVC_MAX_VECTOR_BITS",
    "XVC_MAX_VECTOR_BYTES",
    "XVC_ACCEPT_RATIO",
    "XVC_ACCEPT_VECTOR_BITS",
    "DEFAULT_XVC_PORT",
    "SWDBB_BASE",
    "SWDBB_DRIVE",
    "SWDBB_SAMPLE",
    "DRIVE_TCK",
    "DRIVE_TMS",
    "DRIVE_TDI",
    "SAMPLE_TDO",
    # errors
    "XvcError",
    "XvcProtocolError",
    "XvcBackendError",
    "XsdbSessionError",
    "XsdbTimeout",
    # pure helpers
    "vector_bit",
    "bits_to_vector",
    "vector_to_bits",
    "build_batch_script",
    "build_shift_script",
    "parse_marked_words",
    # register transport
    "RegOp",
    "SwdbbBackend",
    "FakeJtagTap",
    "FakeSwdbbBackend",
    "XsdbProcess",
    "SubprocessXsdbProcess",
    "ScriptedXsdbProcess",
    "PersistentXsdbSession",
    "XsdbSwdbbBackend",
    # JTAG + protocol + server
    "SwdbbJtagShifter",
    "XvcTrace",
    "XvcSession",
    "XvcListenEndpoint",
    "XvcPumpStat",
    "XvcServer",
    "main",
]


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
#: Advertised ``getinfo:`` vector limit. MUST equal the firmware's
#: ``MPS3_XVC_MAX_VECTOR_BITS`` (``firmware/xvc_server/xvc_server.h:26``) so the
#: two servers are interchangeable behind the same client.
#:
#: THIS IS A CHUNK HINT TO THE CLIENT, NOT A CEILING ON ``num_bits``. See
#: :data:`XVC_ACCEPT_RATIO`.
XVC_MAX_VECTOR_BITS: int = 2048
XVC_MAX_VECTOR_BYTES: int = (XVC_MAX_VECTOR_BITS + 7) // 8

#: How much bigger than the advertised value a ``shift:`` may legitimately be.
#:
#: MEASURED, not assumed. Synopsys Identify (T-2022.09-SP2) sizes the *payload*
#: of a ``shift:`` against the advertised value and then adds its own TAP
#: state-navigation TMS bits on top, so ``num_bits`` OVERSHOOTS what we
#: advertise. Three runs of the real debugger against ``--fake-tap`` pin it:
#:
#:     advertised    num_bits requested
#:     ----------    ---------------------------------------------
#:           1024    1029   (= 1024 + 5 navigation bits)
#:           2048    2053   (= 2048 + 5)
#:           8192    3206   (whole operation fits; no chunking at all)
#:
#: Treating the advertised number as a hard ``num_bits`` bound therefore drops
#: the connection on the client's FIRST real scan, which Identify reports as
#: the maddeningly opaque ``Couldn't shift do data from xvcServer``.
#:
#: 4x is not a guess either: it is the ratio the Xilinx XAPP1251 reference
#: ``xvcServer.c`` itself runs at. That server advertises ``2048`` but bounds a
#: request only by ``nr_bytes * 2 > sizeof(buffer)`` with ``buffer[2048]``
#: BYTES — i.e. 1024 bytes per vector, 8192 bits, four times the number it
#: advertised. So a conformant XVC client has never been entitled to assume the
#: advertised value caps ``num_bits``, and every real server accepts past it.
#:
#: Above this hard ceiling we still fail CLOSED (never truncate): a request that
#: large is a desynced or hostile client, not a big scan.
XVC_ACCEPT_RATIO: int = 4
XVC_ACCEPT_VECTOR_BITS: int = XVC_ACCEPT_RATIO * XVC_MAX_VECTOR_BITS

#: ``docs/contracts/net-protocol.md``: TCP 2542 is the XVC port on this harness.
DEFAULT_XVC_PORT: int = 2542

#: ``docs/contracts/shell-regmap.md``: SWDBB block base.
SWDBB_BASE: int = 0x44A70000
SWDBB_DRIVE: int = 0x00
SWDBB_SAMPLE: int = 0x04

DRIVE_TCK: int = 1 << 0  # swd_clk    -> TCK
DRIVE_TMS: int = 1 << 1  # swd_dio_o  -> TMS
DRIVE_TDI: int = 1 << 2  # swd_dio_oe -> TDI
DRIVE_MASK: int = DRIVE_TCK | DRIVE_TMS | DRIVE_TDI
SAMPLE_TDO: int = 1 << 0  # swd_dio_i  -> TDO

# XVC 1.0 command prefixes. Commands are NOT newline framed — each is a fixed
# prefix plus a binary-length-determined payload, so the parser is an
# accumulate-and-reevaluate loop, never a line assembler (same as the C).
_CMD_GETINFO = b"getinfo:"
_CMD_SETTCK = b"settck:"
_CMD_SHIFT = b"shift:"

# Markers the generated Tcl prints so the reply can be located inside whatever
# banner/prompt noise an interactive xsdb interleaves. END is emitted by the
# session itself and terminates every eval.
_MARK_VALS = "MPS3XVC_VALS"
_MARK_TDO = "MPS3XVC_TDO"
_MARK_ERR = "MPS3XVC_ERR"
_MARK_END = "MPS3XVC_END"


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class XvcError(RuntimeError):
    """Base for every failure in this module."""


class XvcProtocolError(XvcError):
    """The client sent something that is not valid XVC 1.0.

    Fail closed: the caller drops the connection rather than resync-guessing
    (identical policy to ``xvc_server.c``'s ``try_dispatch() -> -1``).
    """


class XvcBackendError(XvcError):
    """A register access / shift failed in the transport.

    Never swallowed into a plausible all-zero TDO vector: a dead ``xsdb``, an
    unloaded shell or a decoded-to-zero CSR block must be reported (the
    honest-probe discipline shared with ``pyverify.edge``). An all-zeros TDO is
    exactly what a dead SWDBB looks like, and it would read as "the TAP is in
    BYPASS" — silently wrong.
    """


class XsdbSessionError(XvcError):
    """The persistent ``xsdb`` process is dead, unstartable, or spoke garbage."""


class XsdbTimeout(XsdbSessionError):
    """A batch did not complete inside its deadline."""


# --------------------------------------------------------------------------- #
# Pure bit helpers — the XVC LSB-first-within-byte convention, in one place
# --------------------------------------------------------------------------- #
def vector_bit(vec: bytes, index: int) -> int:
    """Return shift bit ``index`` out of an XVC vector.

    LSB-first within each byte: bit ``i`` is ``vec[i // 8] >> (i % 8) & 1``.
    Mirrors ``xvc_server.c:85-94``.
    """
    return (vec[index >> 3] >> (index & 7)) & 1


def bits_to_vector(bits: Sequence[int]) -> bytes:
    """Pack ``bits`` (one int per shift bit, LSB significant) into an XVC vector.

    The exact inverse of :func:`vector_bit`, so a TDO vector built here is
    unpacked correctly by any conformant XVC client.
    """
    out = bytearray((len(bits) + 7) // 8)
    for i, bit in enumerate(bits):
        if bit & 1:
            out[i >> 3] |= 1 << (i & 7)
    return bytes(out)


def vector_to_bits(vec: bytes, num_bits: int) -> "List[int]":
    """Unpack the first ``num_bits`` of an XVC vector into a list of 0/1."""
    return [vector_bit(vec, i) for i in range(num_bits)]


# --------------------------------------------------------------------------- #
# The register transport seam
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RegOp:
    """One element of a batched register program.

    A batch is an ordered sequence of these; the backend returns one value per
    read, in order. Deliberately not a
    :class:`~socket_harness.session.RegisterBackend` call pair, because the whole
    point is that thousands of accesses must cross the ``xsdb`` boundary in ONE
    round trip.
    """

    addr: int
    is_read: bool
    value: int = 0

    @classmethod
    def write(cls, addr: int, value: int) -> "RegOp":
        return cls(addr=addr, is_read=False, value=value & 0xFFFFFFFF)

    @classmethod
    def read(cls, addr: int) -> "RegOp":
        return cls(addr=addr, is_read=True, value=0)


class SwdbbBackend(abc.ABC):
    """Abstract batched register transport onto the SWDBB block.

    Concrete implementations: :class:`XsdbSwdbbBackend` (real, over a persistent
    ``xsdb``) and :class:`FakeSwdbbBackend` (in-memory, drives a
    :class:`FakeJtagTap`). A subclass only has to implement :meth:`run_batch`;
    everything else, including the reference JTAG shift expansion, is derived
    from it.
    """

    @abc.abstractmethod
    def run_batch(self, ops: "Sequence[RegOp]") -> "List[int]":
        """Execute ``ops`` in order; return one value per read op, in order."""

    # -- conveniences (never on the hot path) ------------------------------- #
    def write_word(self, addr: int, value: int) -> None:
        self.run_batch([RegOp.write(addr, value)])

    def read_word(self, addr: int) -> int:
        vals = self.run_batch([RegOp.read(addr)])
        if len(vals) != 1:
            raise XvcBackendError(f"read_word: expected 1 value, got {len(vals)}")
        return vals[0]

    # -- the hot path ------------------------------------------------------- #
    def shift_drive_sample(
        self,
        low_values: "Sequence[int]",
        park_value: int,
        *,
        drive_addr: int,
        sample_addr: int,
    ) -> "List[int]":
        """Clock ``len(low_values)`` JTAG bits and return the TDO bit per bit.

        ``low_values[i]`` is the DRIVE word for bit ``i`` **with TCK low**; the
        rising edge is generated by re-writing ``low_values[i] | DRIVE_TCK``.
        ``park_value`` is written once at the end so TCK idles low.

        THIS DEFAULT IS THE REFERENCE SEMANTICS: the three-access-per-bit
        sequence documented in the module header, expanded into one
        :meth:`run_batch`. :class:`FakeSwdbbBackend` inherits it unchanged, so
        the bit-order/phase tests exercise exactly the ordering a real backend
        must reproduce. :class:`XsdbSwdbbBackend` overrides it only to emit a
        much smaller equivalent Tcl program.
        """
        ops: "List[RegOp]" = []
        for value in low_values:
            ops.append(RegOp.write(drive_addr, value & ~DRIVE_TCK))
            ops.append(RegOp.read(sample_addr))
            ops.append(RegOp.write(drive_addr, value | DRIVE_TCK))
        ops.append(RegOp.write(drive_addr, park_value & ~DRIVE_TCK))

        samples = self.run_batch(ops)
        if len(samples) != len(low_values):
            raise XvcBackendError(
                f"shift returned {len(samples)} samples for "
                f"{len(low_values)} bits"
            )
        return [s & SAMPLE_TDO for s in samples]

    def close(self) -> None:  # pragma: no cover - trivial default
        """Release any transport resources. Default: nothing to do."""


# --------------------------------------------------------------------------- #
# The loopback TAP — what makes this module trustworthy without a board
# --------------------------------------------------------------------------- #
#: IEEE 1149.1 controller: ``state -> (next_on_tms0, next_on_tms1)``.
_TAP_NEXT = {
    "TLR": ("RTI", "TLR"),
    "RTI": ("RTI", "SEL_DR"),
    "SEL_DR": ("CAP_DR", "SEL_IR"),
    "CAP_DR": ("SHIFT_DR", "EXIT1_DR"),
    "SHIFT_DR": ("SHIFT_DR", "EXIT1_DR"),
    "EXIT1_DR": ("PAUSE_DR", "UPDATE_DR"),
    "PAUSE_DR": ("PAUSE_DR", "EXIT2_DR"),
    "EXIT2_DR": ("SHIFT_DR", "UPDATE_DR"),
    "UPDATE_DR": ("RTI", "SEL_DR"),
    "SEL_IR": ("CAP_IR", "TLR"),
    "CAP_IR": ("SHIFT_IR", "EXIT1_IR"),
    "SHIFT_IR": ("SHIFT_IR", "EXIT1_IR"),
    "EXIT1_IR": ("PAUSE_IR", "UPDATE_IR"),
    "PAUSE_IR": ("PAUSE_IR", "EXIT2_IR"),
    "EXIT2_IR": ("SHIFT_IR", "UPDATE_IR"),
    "UPDATE_IR": ("RTI", "SEL_DR"),
}


class FakeJtagTap:
    """A minimal but faithful IEEE 1149.1 TAP, in memory.

    Enough of a TAP to make bit order and sampling phase *falsifiable* without a
    board: the 16-state controller, an IR, an IDCODE DR, BYPASS, and optional
    user DRs. Everything happens on the **rising** TCK edge (:meth:`tick`); TDO
    (:attr:`tdo`) is the combinational value the TAP presents while TCK is low,
    i.e. what a host must read *before* the next rising edge.

    Actions are taken on the edge at which the controller *is in* the state, then
    the state advances — so ``Capture-DR`` loads the DR on the same edge that
    enters ``Shift-DR``, and the first TDO read in ``Shift-DR`` is DR bit 0.
    That is what makes an IDCODE read come out aligned.
    """

    #: 4-bit IR is the common minimum; ``Test-Logic-Reset`` must select IDCODE.
    IR_IDCODE = 0b0001
    IR_BYPASS = 0b1111

    def __init__(
        self,
        idcode: int = 0x6BA00477,
        *,
        ir_len: int = 4,
        user_drs: "Optional[dict]" = None,
    ) -> None:
        self.idcode = idcode & 0xFFFFFFFF
        self.ir_len = ir_len
        #: ``{ir_value: (length, capture_value)}`` — extra data registers.
        self.user_drs = dict(user_drs or {})
        self.state = "TLR"
        self.ir = self.IR_IDCODE
        self.rising_edges = 0
        #: ``(ir, value)`` pairs latched at every ``Update-DR``.
        self.dr_updates: "List[tuple]" = []
        self._ir_shift = 0
        self._dr = 0
        self._dr_len = 32

    # -- register selection ------------------------------------------------- #
    def _selected_dr(self) -> "tuple":
        """``(length, capture_value)`` for the current IR."""
        if self.ir == self.IR_IDCODE:
            return (32, self.idcode)
        if self.ir in self.user_drs:
            return self.user_drs[self.ir]
        # BYPASS, and any unimplemented instruction, is a 1-bit register of 0
        # (1149.1 requires unimplemented instructions to behave as BYPASS).
        return (1, 0)

    # -- the wires ---------------------------------------------------------- #
    @property
    def tdo(self) -> int:
        """The bit the TAP is presenting NOW (valid while TCK is low).

        Only driven in the two Shift states; 0 elsewhere (a real TAP tri-states
        TDO outside Shift/Exit, and the shell's ``swd_dio_i`` would read whatever
        the RM ties it to — 0 is the honest stand-in).
        """
        if self.state == "SHIFT_DR":
            return self._dr & 1
        if self.state == "SHIFT_IR":
            return self._ir_shift & 1
        return 0

    def tick(self, tms: bool, tdi: bool) -> None:
        """One rising TCK edge with the given TMS/TDI."""
        state = self.state
        tdi_bit = 1 if tdi else 0

        if state == "TLR":
            # 1149.1: Test-Logic-Reset loads the IDCODE instruction.
            self.ir = self.IR_IDCODE
        elif state == "CAP_DR":
            self._dr_len, self._dr = self._selected_dr()
        elif state == "SHIFT_DR":
            mask = (1 << self._dr_len) - 1
            self._dr = ((tdi_bit << (self._dr_len - 1)) | (self._dr >> 1)) & mask
        elif state == "CAP_IR":
            # 1149.1 mandates the two LSBs capture as 01.
            self._ir_shift = 0b0001
        elif state == "SHIFT_IR":
            mask = (1 << self.ir_len) - 1
            self._ir_shift = ((tdi_bit << (self.ir_len - 1)) | (self._ir_shift >> 1)) & mask
        elif state == "UPDATE_IR":
            self.ir = self._ir_shift
        elif state == "UPDATE_DR":
            self.dr_updates.append((self.ir, self._dr))

        self.state = _TAP_NEXT[state][1 if tms else 0]
        self.rising_edges += 1


class FakeSwdbbBackend(SwdbbBackend):
    """In-memory SWDBB: DRIVE/SAMPLE semantics wired to a :class:`FakeJtagTap`.

    Reproduces exactly what ``swd_bb.sv`` does — DRIVE is a 3-bit write-through
    register, SAMPLE returns the live inbound pin — and derives the TAP's rising
    edge from DRIVE[0] going 0 -> 1, which is the only way the real RM can see a
    TCK edge either. Counters (:attr:`batch_calls`, :attr:`op_count`) let tests
    assert the batching contract.
    """

    def __init__(
        self,
        tap: "Optional[FakeJtagTap]" = None,
        *,
        base: int = SWDBB_BASE,
    ) -> None:
        self.tap = FakeJtagTap() if tap is None else tap
        self.base = base
        self.drive = 0
        self.batch_calls = 0
        self.op_count = 0
        #: every DRIVE value written, in order (for ordering assertions).
        self.drive_writes: "List[int]" = []

    def run_batch(self, ops: "Sequence[RegOp]") -> "List[int]":
        self.batch_calls += 1
        out: "List[int]" = []
        drive_addr = self.base + SWDBB_DRIVE
        sample_addr = self.base + SWDBB_SAMPLE
        for op in ops:
            self.op_count += 1
            if op.is_read:
                if op.addr == sample_addr:
                    out.append(self.tap.tdo & SAMPLE_TDO)
                elif op.addr == drive_addr:
                    out.append(self.drive)
                else:
                    raise XvcBackendError(
                        f"read of unmapped SWDBB offset {op.addr:#010x} "
                        f"(base {self.base:#010x}); swd_bb decodes ONLY "
                        "0x00/0x04 and returns 0 elsewhere"
                    )
                continue

            if op.addr != drive_addr:
                raise XvcBackendError(
                    f"write to unmapped/read-only SWDBB offset {op.addr:#010x} "
                    f"(base {self.base:#010x})"
                )
            previous = self.drive
            self.drive = op.value & DRIVE_MASK  # swd_bb keeps only [2:0]
            self.drive_writes.append(self.drive)
            if (self.drive & DRIVE_TCK) and not (previous & DRIVE_TCK):
                self.tap.tick(
                    tms=bool(self.drive & DRIVE_TMS),
                    tdi=bool(self.drive & DRIVE_TDI),
                )
        return out


# --------------------------------------------------------------------------- #
# Persistent xsdb session
# --------------------------------------------------------------------------- #
@runtime_checkable
class XsdbProcess(Protocol):
    """The injectable ``xsdb`` process seam.

    Byte oriented rather than line oriented on purpose: an interactive ``xsdb``
    interleaves a prompt with no trailing newline, so line assembly has to be
    ours (:meth:`PersistentXsdbSession._readline`).
    """

    def send(self, data: bytes) -> None:
        ...

    def recv(self, timeout: float) -> bytes:
        """Up to one chunk. ``b''`` on timeout; raise on EOF/death."""
        ...

    def close(self) -> None:
        ...


class SubprocessXsdbProcess:
    """A real long-lived ``xsdb`` child process.

    ``stdout`` is drained with :func:`os.read` on the raw fd (never through the
    :class:`io.BufferedReader`) so :func:`select.select` readiness and our own
    buffer never disagree — the classic cause of a "hangs forever with a full
    pipe" stall.
    """

    def __init__(self, argv: "Sequence[str]") -> None:
        self.argv = list(argv)
        try:
            self._proc = subprocess.Popen(  # noqa: S603 - argv is ours
                self.argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            raise XsdbSessionError(
                f"cannot launch {self.argv!r}: {exc} "
                "(is the Vivado/Vitis environment sourced?)"
            ) from exc

    def send(self, data: bytes) -> None:
        stdin = self._proc.stdin
        if stdin is None:  # pragma: no cover - Popen always gives us one
            raise XsdbSessionError("xsdb stdin is closed")
        try:
            stdin.write(data)
            stdin.flush()
        except (BrokenPipeError, ValueError, OSError) as exc:
            raise XsdbSessionError(
                f"xsdb stdin write failed ({exc}); rc={self._proc.poll()!r}"
            ) from exc

    def recv(self, timeout: float) -> bytes:
        stdout = self._proc.stdout
        if stdout is None:  # pragma: no cover
            raise XsdbSessionError("xsdb stdout is closed")
        fd = stdout.fileno()
        ready, _, _ = select.select([fd], [], [], max(timeout, 0.0))
        if not ready:
            return b""
        chunk = os.read(fd, 65536)
        if chunk == b"":
            raise XsdbSessionError(
                f"xsdb exited (stdout EOF); rc={self._proc.poll()!r}"
            )
        return chunk

    def close(self) -> None:
        try:
            self.send(b"exit\n")
        except XsdbSessionError:
            pass
        try:
            self._proc.wait(timeout=5.0)
        except Exception:  # pragma: no cover - best-effort teardown
            try:
                self._proc.kill()
            except Exception:
                pass


class ScriptedXsdbProcess:
    """In-memory :class:`XsdbProcess` double.

    Records every script written and answers each with canned text, then the
    ``MPS3XVC_END`` line the real Tcl would print. ``responder`` gets the script
    and returns the body output, so a test can *interpret* the emitted Tcl (see
    ``test_xvc_server.py``, which drives a :class:`FakeJtagTap` from the value
    list inside a generated shift script — that is what pins the compact Tcl to
    the reference op expansion).
    """

    def __init__(
        self,
        replies: "Optional[Sequence[str]]" = None,
        *,
        responder: "Optional[Callable[[str], str]]" = None,
        banner: str = "",
    ) -> None:
        self.sent: "List[str]" = []
        self._replies = list(replies or [])
        self._responder = responder
        self._out = bytearray(banner.encode())
        self.closed = False

    def send(self, data: bytes) -> None:
        script = data.decode("utf-8", "replace")
        self.sent.append(script)
        if self._responder is not None:
            body = self._responder(script)
        elif self._replies:
            body = self._replies.pop(0)
        else:
            body = ""
        text = body
        if text and not text.endswith("\n"):
            text += "\n"
        # The session always appends `puts "MPS3XVC_END"`; simulate Tcl running
        # it so the reader's framing is genuinely exercised.
        text += _MARK_END + "\n"
        self._out += text.encode()

    def recv(self, timeout: float) -> bytes:
        if not self._out:
            # Yield rather than spin: a test that forgets to queue a reply must
            # hit the session's deadline, not burn a core doing it.
            time.sleep(min(max(timeout, 0.0), 0.01))
            return b""
        chunk = bytes(self._out)
        del self._out[:]
        return chunk

    def close(self) -> None:
        self.closed = True


class PersistentXsdbSession:
    """ONE long-lived ``xsdb``: connected once, fed scripts on stdin.

    This is the whole reason the host-side XVC route is viable at all — see the
    module header's performance section. It deliberately does NOT replace
    :class:`~socket_harness.xsdb.XsdbRegisterEndpoint` (whose per-op
    ``xsdb -eval`` spawn is fine for reading a diag mailbox once) and shares its
    :class:`~socket_harness.xsdb.XsdbConfig`, so the FQDN hub-URL trap
    (``mps3_diag.tcl:21``) is honoured in one place.

    Non-halting guarantee preserved: the emitted Tcl carries only
    ``connect`` / ``mwr`` / ``mrd -force``. No ``stop``, no ``con`` — halting a
    running MicroBlaze mid-swap corrupts the transfer
    (``xsdb.py`` docstring, trap 1).
    """

    def __init__(
        self,
        cfg: XsdbConfig = XsdbConfig(),
        *,
        spawn: "Optional[Callable[[], XsdbProcess]]" = None,
        timeout_s: float = 300.0,
        connect: bool = True,
        target_filter: str = "",
    ) -> None:
        self.cfg = cfg
        self.timeout_s = timeout_s
        self._spawn = spawn if spawn is not None else self._default_spawn
        self._connect_on_start = connect
        #: xsdb `targets -set -filter <expr>` run once after connect. REQUIRED
        #: against real hardware: `connect` alone leaves no current target, so
        #: the first mrd/mwr dies with "Invalid target. Use "targets" command to
        #: select a target". Found only by running against a board -- the unit
        #: tests use a fake backend that never needs a target selected.
        #: MUST include a jtag_cable_name filter: the hw_server is shared with
        #: four other boards and an unfiltered selection can hit the wrong one
        #: (every register then reads 0x00000000, which mimics a valid state).
        self._target_filter = target_filter
        self._proc: "Optional[XsdbProcess]" = None
        self._buf = bytearray()
        #: how many scripts have crossed the boundary (the batching metric).
        self.eval_calls = 0

    def _default_spawn(self) -> XsdbProcess:
        return SubprocessXsdbProcess([self.cfg.xsdb])

    # -- lifecycle ---------------------------------------------------------- #
    @property
    def started(self) -> bool:
        return self._proc is not None

    def start(self) -> "PersistentXsdbSession":
        """Spawn the interpreter and ``connect`` once. Idempotent."""
        if self._proc is not None:
            return self
        self._proc = self._spawn()
        del self._buf[:]
        if self._connect_on_start:
            # `catch` because a second connect to an already-connected session
            # is an error, and a re-`start()` after a soft failure is normal.
            self.eval(f"catch {{connect -url {self.cfg.hub_url}}}")
        if self._target_filter:
            # NOT wrapped in `catch`: if the target cannot be selected, every
            # subsequent mrd/mwr fails with "Invalid target", and failing here
            # with the filter in the message is far easier to diagnose than a
            # shift error surfacing later as "Couldn't shift do data".
            self.eval(f"targets -set -filter {{{self._target_filter}}}")
        return self

    def close(self) -> None:
        proc, self._proc = self._proc, None
        del self._buf[:]
        if proc is not None:
            proc.close()

    def __enter__(self) -> "PersistentXsdbSession":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- the one I/O primitive --------------------------------------------- #
    def eval(self, script: str, *, timeout_s: "Optional[float]" = None) -> str:
        """Run ``script``, return everything it printed before ``MPS3XVC_END``.

        The END sentinel (rather than a prompt match) is what makes this robust:
        an interactive ``xsdb`` emits a promptless ``xsdb%`` that glues itself to
        the front of the next line, and banner text arrives unpredictably.
        """
        proc = self._proc
        if proc is None:
            raise XsdbSessionError("PersistentXsdbSession.eval() before start()")
        self.eval_calls += 1
        payload = script.rstrip("\n") + "\n" + f'puts "{_MARK_END}"\n'
        proc.send(payload.encode())

        deadline = time.monotonic() + (
            self.timeout_s if timeout_s is None else timeout_s
        )
        lines: "List[str]" = []
        while True:
            line = self._readline(deadline)
            if line is None:
                raise XsdbTimeout(
                    "xsdb batch did not finish before its deadline "
                    f"({self.timeout_s if timeout_s is None else timeout_s}s); "
                    "partial output:\n" + "\n".join(lines[-20:])
                )
            if _MARK_END in line:
                # The sentinel may be prefixed by a prompt; anything before it on
                # the same line is real output.
                head = line.split(_MARK_END)[0].strip()
                if head:
                    lines.append(head)
                break
            lines.append(line)
        return "\n".join(lines)

    def _readline(self, deadline: float) -> "Optional[str]":
        """One line from the process, or ``None`` on deadline expiry."""
        proc = self._proc
        assert proc is not None
        while True:
            nl = self._buf.find(b"\n")
            if nl >= 0:
                line = bytes(self._buf[:nl]).decode("utf-8", "replace")
                del self._buf[: nl + 1]
                return line.rstrip("\r")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            chunk = proc.recv(min(remaining, 1.0))
            if chunk:
                self._buf += chunk


# --------------------------------------------------------------------------- #
# Pure Tcl generation + reply parsing
# --------------------------------------------------------------------------- #
def _hex(value: int) -> str:
    """32-bit hex literal in xsdb form (matches ``xsdb._fmt``)."""
    return f"0x{value & 0xFFFFFFFF:08X}"


def _mrd(addr_expr: str) -> str:
    """``mrd -value -force`` of one word.

    ``-value`` prints bare values and ``-force`` reads through the MDM on a
    RUNNING core without halting it — the same invocation
    :meth:`socket_harness.xsdb.XsdbRegisterEndpoint._read_tcl` uses, so both
    paths inherit the same non-halting guarantee.
    """
    return f"lindex [mrd -value -force {addr_expr} 1] 0"


def build_batch_script(ops: "Sequence[RegOp]") -> "tuple":
    """``(tcl, n_reads)`` for a generic :class:`RegOp` batch.

    One literal line per op inside a single ``catch``, so a failure anywhere
    aborts the whole batch and is reported as ``MPS3XVC_ERR`` rather than
    silently yielding a short value list. Intended for small batches; a JTAG
    shift uses :func:`build_shift_script`, which is ~20x smaller.
    """
    body: "List[str]" = []
    n_reads = 0
    for op in ops:
        if op.is_read:
            body.append(f"  lappend _A [{_mrd(_hex(op.addr))}]")
            n_reads += 1
        else:
            body.append(f"  mwr {_hex(op.addr)} {_hex(op.value)}")
    return (_wrap_catch(body, _MARK_VALS), n_reads)


def build_shift_script(
    drive_addr: int,
    sample_addr: int,
    low_values: "Sequence[int]",
    park_value: int,
) -> str:
    """The compact Tcl for one JTAG shift — the hot path.

    Exploits the regularity the generic form cannot see: per bit, the TCK-high
    word is just the TCK-low word with bit 0 set, so only the **low** words need
    transmitting, and each is a single decimal digit (DRIVE is 3 bits, 0..7 — one
    digit, and never Tcl-octal-ambiguous). Cost per JTAG bit falls from ~113
    bytes of literal Tcl to 2, i.e. **4,342 bytes** for a maximal 2048-bit shift
    against **231,513** for :func:`build_batch_script` on the same ops (both
    measured, not estimated — :func:`test_shift_script_is_far_smaller_than_the_generic_expansion`).
    The reply is ~18 KB of bare hex words either way.

    Semantically identical to :meth:`SwdbbBackend.shift_drive_sample`'s default
    op expansion; ``test_xvc_server.py`` asserts that equality by interpreting
    the emitted value list through :class:`FakeSwdbbBackend`.

    The SAMPLE words are returned raw (bare hex, as ``mrd -value`` prints them)
    and reduced to TDO bits in Python — deliberately no arithmetic in Tcl, whose
    ``scan``/``expr`` handling of a bare ``0x``-prefixed word is version
    dependent and cannot be tested here.
    """
    values = " ".join(str(v & DRIVE_MASK & ~DRIVE_TCK) for v in low_values)
    drive = _hex(drive_addr)
    body = [
        f"  foreach _v {{{values}}} {{",
        f"    mwr {drive} $_v",
        f"    lappend _A [{_mrd(_hex(sample_addr))}]",
        f"    mwr {drive} [expr {{$_v | {DRIVE_TCK}}}]",
        "  }",
        f"  mwr {drive} {park_value & DRIVE_MASK & ~DRIVE_TCK}",
    ]
    return _wrap_catch(body, _MARK_TDO)


def _wrap_catch(body: "Iterable[str]", ok_marker: str) -> str:
    """Wrap a Tcl body so it reports exactly one marker line, ever.

    ``if``'s taken branch is a ``puts`` (result: empty string), so an
    interactive interpreter echoes nothing extra of its own.
    """
    lines = ["set _A {}", "if {[catch {"]
    lines.extend(body)
    lines.append('} _e]} { puts "' + _MARK_ERR + ' $_e" } else { puts "'
                 + ok_marker + ' $_A" }')
    return "\n".join(lines)


def parse_marked_words(text: str, marker: str, expect: int) -> "List[int]":
    """Extract exactly ``expect`` hex words from the ``marker`` line of ``text``.

    Raises :class:`XvcBackendError` on an ``MPS3XVC_ERR`` line, a missing marker,
    or the wrong word count — never pads or truncates. A short reply here means
    the batch partially executed, and inventing zeros for the rest would fake a
    TDO stream.
    """
    err = re.search(re.escape(_MARK_ERR) + r"\s*(.*)", text)
    if err is not None:
        raise XvcBackendError("xsdb batch failed: " + err.group(1).strip())
    hit = re.search(re.escape(marker) + r"(.*)", text, re.S)
    if hit is None:
        raise XvcBackendError(
            f"no {marker} marker in xsdb reply (xsdb died? not connected? "
            "wrong hub url?):\n" + text.strip()[:2000]
        )
    tokens = hit.group(1).replace("{", " ").replace("}", " ").split()
    words: "List[int]" = []
    for tok in tokens:
        if len(words) == expect:
            break  # ignore any trailing prompt/banner the interpreter adds
        raw = tok[2:] if tok[:2].lower() == "0x" else tok
        if re.fullmatch(r"[0-9A-Fa-f]{1,8}", raw):
            words.append(int(raw, 16))
        else:
            break  # non-hex token ends the value list
    if len(words) != expect:
        raise XvcBackendError(
            f"expected {expect} words after {marker}, got {len(words)} "
            "(batch aborted part-way? a short reply is NEVER zero-padded)"
        )
    return words


class XsdbSwdbbBackend(SwdbbBackend):
    """The real backend: SWDBB accesses over a :class:`PersistentXsdbSession`."""

    def __init__(
        self,
        session: PersistentXsdbSession,
        *,
        base: int = SWDBB_BASE,
    ) -> None:
        self.session = session
        self.base = base
        self.batch_calls = 0

    def run_batch(self, ops: "Sequence[RegOp]") -> "List[int]":
        self.batch_calls += 1
        script, n_reads = build_batch_script(ops)
        text = self.session.eval(script)
        return parse_marked_words(text, _MARK_VALS, n_reads)

    def shift_drive_sample(
        self,
        low_values: "Sequence[int]",
        park_value: int,
        *,
        drive_addr: int,
        sample_addr: int,
    ) -> "List[int]":
        """One ``eval`` per shift — the entire performance argument."""
        self.batch_calls += 1
        script = build_shift_script(drive_addr, sample_addr, low_values, park_value)
        text = self.session.eval(script)
        words = parse_marked_words(text, _MARK_TDO, len(low_values))
        return [w & SAMPLE_TDO for w in words]

    def close(self) -> None:
        self.session.close()


# --------------------------------------------------------------------------- #
# The JTAG bit-banger
# --------------------------------------------------------------------------- #
class SwdbbJtagShifter:
    """Turn one XVC ``shift:`` into DRIVE/SAMPLE traffic.

    :meth:`drive_values` is pure and is where the XVC bit-order convention is
    applied exactly once; :meth:`shift` hands the whole vector to the backend in
    a SINGLE call (never per bit).

    ``max_bits`` here is a SANITY ceiling on how large a vector this shifter will
    expand — deliberately :data:`XVC_ACCEPT_VECTOR_BITS`, the hard accept
    ceiling, and NOT the smaller number ``getinfo:`` advertises. A shifter capped
    at the advertisement would re-introduce the over-length rejection one layer
    down from :class:`XvcSession`, where it is even harder to see.
    """

    def __init__(
        self,
        backend: SwdbbBackend,
        *,
        base: int = SWDBB_BASE,
        max_bits: int = XVC_ACCEPT_VECTOR_BITS,
    ) -> None:
        self.backend = backend
        self.base = base
        self.max_bits = max_bits
        #: cumulative statistics, handy in logs and in --fake-tap runs.
        self.shifts = 0
        self.bits = 0

    @property
    def drive_addr(self) -> int:
        return self.base + SWDBB_DRIVE

    @property
    def sample_addr(self) -> int:
        return self.base + SWDBB_SAMPLE

    def drive_values(self, num_bits: int, tms: bytes, tdi: bytes) -> "List[int]":
        """PURE: the TCK-low DRIVE word for each of ``num_bits`` JTAG bits.

        ``tms``/``tdi`` are XVC vectors (LSB-first within each byte). The
        returned words carry TMS in bit 1 and TDI in bit 2 with TCK clear, which
        is the SWDBB pin reuse from the module header.
        """
        if num_bits <= 0 or num_bits > self.max_bits:
            raise XvcProtocolError(
                f"shift of {num_bits} bits outside 1..{self.max_bits}"
            )
        need = (num_bits + 7) // 8
        if len(tms) < need or len(tdi) < need:
            raise XvcProtocolError(
                f"shift vectors too short for {num_bits} bits: "
                f"tms={len(tms)} tdi={len(tdi)}, need {need} each"
            )
        out: "List[int]" = []
        for i in range(num_bits):
            word = 0
            if vector_bit(tms, i):
                word |= DRIVE_TMS
            if vector_bit(tdi, i):
                word |= DRIVE_TDI
            out.append(word)
        return out

    def shift(self, num_bits: int, tms: bytes, tdi: bytes) -> bytes:
        """Clock ``num_bits`` and return the TDO vector (``ceil(num_bits/8)`` B)."""
        lows = self.drive_values(num_bits, tms, tdi)
        # Park with TCK low and TMS/TDI at their final values: TCK idles low,
        # matching swd_bb's 3'b000 reset intent, and the next shift's first write
        # therefore creates no spurious edge.
        park = lows[-1]
        bits = self.backend.shift_drive_sample(
            lows, park, drive_addr=self.drive_addr, sample_addr=self.sample_addr
        )
        if len(bits) != num_bits:
            raise XvcBackendError(
                f"backend returned {len(bits)} TDO bits for {num_bits} requested"
            )
        self.shifts += 1
        self.bits += num_bits
        return bits_to_vector(bits)

    def ops_per_shift(self, num_bits: int) -> int:
        """Register accesses a shift of ``num_bits`` costs (for logs/estimates)."""
        return 3 * num_bits + 1


# --------------------------------------------------------------------------- #
# Per-command diagnostics
# --------------------------------------------------------------------------- #
class XvcTrace:
    """A per-command XVC trace written STRAIGHT to a file.

    This exists because of a specific, expensive failure mode: the per-shift
    diagnostics were originally ``print(..., file=sys.stderr)``, and when the
    server was launched with its stderr on a pipe (which is how it gets launched
    next to a GUI/debugger client) Python's block buffering meant **not one shift
    record ever reached the log** before the client tore the session down. A
    client-side error message like Identify's ``Couldn't shift do data from
    xvcServer`` is then the only evidence, and it names neither the shift nor the
    lengths.

    So: line-buffered file, one ``flush()`` per record, no ``print``. The cost of
    a flush is irrelevant next to milliseconds-per-AXI-access.

    Records are ``key=value`` space separated with a leading monotonic timestamp
    and an event kind, e.g.::

        0.014 shift bits=32 tms_len=4 tdi_len=4 tdo_len=4 tms=ff.. tdo=7704a06b

    ``raw=True`` additionally captures the two byte streams verbatim into
    ``<path>.rx`` / ``<path>.tx``. That is what lets a *real* client's byte
    sequence be replayed into a regression test instead of a hand-rolled
    approximation of what the spec says the client ought to send.
    """

    #: bytes of a vector shown at each end of a record (hex, ``..`` in between).
    EDGE = 8

    def __init__(self, path: str, *, raw: bool = False) -> None:
        self.path = path
        self._fh = open(path, "a", buffering=1)  # line buffered
        self._rx = open(path + ".rx", "ab", buffering=0) if raw else None
        self._tx = open(path + ".tx", "ab", buffering=0) if raw else None
        self._t0 = time.monotonic()
        self.records = 0

    # -- formatting --------------------------------------------------------- #
    @classmethod
    def edges(cls, data: bytes) -> str:
        """``data`` as hex, elided in the middle, so a record stays one line."""
        if len(data) <= 2 * cls.EDGE:
            return data.hex() or "-"
        return f"{data[: cls.EDGE].hex()}..{data[-cls.EDGE :].hex()}"

    def event(self, kind: str, **fields: object) -> None:
        parts = [f"{time.monotonic() - self._t0:8.3f}", kind]
        parts += [f"{k}={v}" for k, v in fields.items()]
        self._fh.write(" ".join(parts) + "\n")
        self._fh.flush()
        self.records += 1

    # -- raw stream capture ------------------------------------------------- #
    def rx(self, data: bytes) -> None:
        if self._rx is not None:
            self._rx.write(data)

    def tx(self, data: bytes) -> None:
        if self._tx is not None:
            self._tx.write(data)

    def close(self) -> None:
        for fh in (self._fh, self._rx, self._tx):
            if fh is not None:
                try:
                    fh.close()
                except OSError:  # pragma: no cover
                    pass


# --------------------------------------------------------------------------- #
# XVC 1.0 protocol engine — pure bytes in, bytes out
# --------------------------------------------------------------------------- #
def _prefix_possible(buf: bytearray, prefix: bytes) -> bool:
    n = min(len(buf), len(prefix))
    return bytes(buf[:n]) == prefix[:n]


def _prefix_complete(buf: bytearray, prefix: bytes) -> bool:
    return len(buf) >= len(prefix) and bytes(buf[: len(prefix)]) == prefix


class XvcSession:
    """One XVC client's command stream. No sockets, no board.

    Structurally the C engine (``firmware/xvc_server/xvc_server.c``
    ``try_dispatch``): accumulate bytes, re-evaluate, consume whole commands.
    Framing and the 2048-bit ceiling are matched exactly so the firmware server
    can replace this one without the client noticing.
    """

    def __init__(
        self,
        shifter: "Optional[SwdbbJtagShifter]" = None,
        *,
        max_bits: int = XVC_MAX_VECTOR_BITS,
        accept_bits: "Optional[int]" = None,
        trace: "Optional[XvcTrace]" = None,
    ) -> None:
        self.shifter = shifter
        #: what ``getinfo:`` ADVERTISES — the client's payload chunk hint.
        self.max_bits = max_bits
        #: the hard ceiling on an ACCEPTED ``shift:``. Strictly greater than
        #: :attr:`max_bits`, because clients overshoot the advertisement with
        #: their TAP-navigation bits (:data:`XVC_ACCEPT_RATIO`).
        self.accept_bits = (
            XVC_ACCEPT_RATIO * max_bits if accept_bits is None else accept_bits
        )
        self.trace = trace
        #: last ``settck:`` period. ADVISORY ONLY — see :meth:`_settck`.
        self.tck_period_ns = 0
        self.commands = 0
        self.shifts = 0
        self.bits_shifted = 0
        self._buf = bytearray()

    @property
    def info_string(self) -> bytes:
        """The ``getinfo:`` reply (``xvcServer_v1.0:<max_bits>\\n``)."""
        return b"xvcServer_v1.0:%d\n" % self.max_bits

    def feed(self, data: bytes) -> bytes:
        """Absorb ``data``; return the concatenated replies for whole commands.

        Raises :class:`XvcProtocolError` on a protocol violation (caller drops
        the client) and lets :class:`XvcBackendError` propagate (hardware
        failure must be visible, never a zero-filled TDO).
        """
        self._buf += data
        out = bytearray()
        while True:
            reply = self._try_dispatch()
            if reply is None:
                break
            out += reply
        return bytes(out)

    @property
    def pending(self) -> int:
        """Bytes buffered for an incomplete command (0 between commands)."""
        return len(self._buf)

    def _try_dispatch(self) -> "Optional[bytes]":
        buf = self._buf
        if not buf:
            return None
        if not (
            _prefix_possible(buf, _CMD_GETINFO)
            or _prefix_possible(buf, _CMD_SETTCK)
            or _prefix_possible(buf, _CMD_SHIFT)
        ):
            if self.trace is not None:
                self.trace.event(
                    "bad-command", buffered=len(buf), head=self.trace.edges(bytes(buf[:16]))
                )
            raise XvcProtocolError(
                "not an XVC 1.0 command (expected getinfo:/settck:/shift:), got "
                f"{bytes(buf[:16])!r}"
            )
        if _prefix_complete(buf, _CMD_GETINFO):
            del buf[: len(_CMD_GETINFO)]
            self.commands += 1
            if self.trace is not None:
                self.trace.event("getinfo", reply=self.info_string.strip().decode())
            return self.info_string
        if _prefix_complete(buf, _CMD_SETTCK):
            return self._settck()
        if _prefix_complete(buf, _CMD_SHIFT):
            return self._shift()
        return None  # a still-ambiguous partial prefix

    def _settck(self) -> "Optional[bytes]":
        """``settck:<u32 le>`` -> echo.

        ADVISORY. There is no TCK divider anywhere on this path to program: TCK
        is literally a register bit (``swd_bb.sv:243``), so its period is set by
        how fast ``xsdb`` can complete an AXI write — order milliseconds, i.e.
        thousands of times slower than any period a client would request. The
        requested value is stored and echoed unchanged, exactly like the Xilinx
        reference server and ``xvc_server.c:228-237``. Echoing the request rather
        than a truthful ~1 ms is deliberate: clients treat a wildly larger
        returned period as a cable fault and tear the session down.
        """
        need = len(_CMD_SETTCK) + 4
        if len(self._buf) < need:
            return None
        raw = bytes(self._buf[len(_CMD_SETTCK) : need])
        self.tck_period_ns = int.from_bytes(raw, "little")
        del self._buf[:need]
        self.commands += 1
        if self.trace is not None:
            self.trace.event("settck", period_ns=self.tck_period_ns, reply_len=len(raw))
        return raw

    def _shift(self) -> "Optional[bytes]":
        """``shift:<u32 nbits le><tms bytes><tdi bytes>`` -> ``<tdo bytes>``."""
        header = len(_CMD_SHIFT) + 4
        if len(self._buf) < header:
            return None
        num_bits = int.from_bytes(bytes(self._buf[len(_CMD_SHIFT) : header]), "little")
        if num_bits == 0 or num_bits > self.accept_bits:
            # NOTE the bound is accept_bits, NOT the advertised max_bits. Bounding
            # by the advertisement is exactly the bug that made Identify die with
            # "Couldn't shift do data from xvcServer" on its first real scan: it
            # chunks the PAYLOAD at the advertised size and then adds navigation
            # bits, so num_bits legitimately exceeds it. See XVC_ACCEPT_RATIO.
            # Past accept_bits we still fail closed -- never truncate a scan,
            # because a short TDO reply reads to the client as real captured data.
            if self.trace is not None:
                self.trace.event(
                    "shift-reject",
                    bits=num_bits,
                    advertised=self.max_bits,
                    accept_bits=self.accept_bits,
                    buffered=len(self._buf),
                    head=self.trace.edges(bytes(self._buf[:24])),
                )
            raise XvcProtocolError(
                f"shift: num_bits={num_bits} outside 1..{self.accept_bits} "
                f"(hard accept ceiling; getinfo: advertises {self.max_bits})"
            )
        vec = (num_bits + 7) // 8
        total = header + 2 * vec
        if len(self._buf) < total:
            if self.trace is not None:
                self.trace.event(
                    "shift-partial",
                    bits=num_bits,
                    have=len(self._buf),
                    need=total,
                )
            return None
        tms = bytes(self._buf[header : header + vec])
        tdi = bytes(self._buf[header + vec : total])
        if self.shifter is None:
            raise XvcProtocolError(
                "shift: received but this session has no shifter "
                "(getinfo:/settck: only)"
            )
        tdo = self.shifter.shift(num_bits, tms, tdi)
        del self._buf[:total]
        self.commands += 1
        self.shifts += 1
        self.bits_shifted += num_bits
        if self.trace is not None:
            # bits / vector byte lengths / reply byte length + the payload edges:
            # everything needed to tell a length bug from a JTAG-navigation bug
            # without a second run.
            self.trace.event(
                "shift",
                n=self.shifts,
                bits=num_bits,
                tms_len=len(tms),
                tdi_len=len(tdi),
                tdo_len=len(tdo),
                tms=self.trace.edges(tms),
                tdi=self.trace.edges(tdi),
                tdo=self.trace.edges(tdo),
            )
        return tdo


# --------------------------------------------------------------------------- #
# TCP front end
# --------------------------------------------------------------------------- #
class XvcListenEndpoint(TcpListenEndpoint):
    """:class:`~socket_harness.console_bridge.TcpListenEndpoint` + client drop.

    XVC must fail CLOSED on a protocol violation, but tearing down the *listener*
    would drop the port Identify is configured against. This adds the one missing
    operation: close the accepted connection only, so the next
    ``read``/``write`` re-accepts.
    """

    def drop_client(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass

    @property
    def has_client(self) -> bool:
        return self._conn is not None

    def accept_if_pending(self, timeout: float = 0.0) -> bool:
        """Accept a waiting connection WITHOUT reading from it.

        The parent's lazy accept happens inside ``read()``, which then calls
        ``recv()`` on the fresh connection — and a client that connects but sends
        nothing yet (which is exactly what a cable client does: connect, then
        decide) would block that ``recv`` forever, wedging the server. Splitting
        accept from read is what keeps ``pump_once`` non-blocking.
        """
        if self._listen is None:
            raise RuntimeError("XvcListenEndpoint.accept_if_pending() before open()")
        if self._conn is not None:
            return False
        ready, _, _ = select.select([self._listen], [], [], max(timeout, 0.0))
        if not ready:
            return False
        conn, _addr = self._listen.accept()
        self._conn = conn
        return True


@dataclass
class XvcPumpStat:
    """Result of one :meth:`XvcServer.pump_once` step."""

    rx: int = 0
    tx: int = 0
    commands: int = 0
    shifts: int = 0
    bits: int = 0
    accepted: bool = False
    client_closed: bool = False
    dropped: bool = False
    error: str = ""


class XvcServer:
    """Serve XVC 1.0 on a TCP port, one client at a time.

    Single client on purpose: the four SWDBB pins are one physical resource, and
    a second cable client would interleave TCK edges into the same TAP. Extra
    connections simply wait in the listen backlog.

    ``pump_once`` is the unit-testable single step (the ``ConsoleBridge``
    pattern); ``serve_forever`` is the driver.
    """

    def __init__(
        self,
        shifter: "Optional[SwdbbJtagShifter]",
        *,
        host: str = "127.0.0.1",
        port: int = DEFAULT_XVC_PORT,
        local: object = None,
        max_bits: int = XVC_MAX_VECTOR_BITS,
        accept_bits: "Optional[int]" = None,
        trace: "Optional[XvcTrace]" = None,
    ) -> None:
        self.shifter = shifter
        self.max_bits = max_bits
        self.accept_bits = (
            XVC_ACCEPT_RATIO * max_bits if accept_bits is None else accept_bits
        )
        self.trace = trace
        self.local = XvcListenEndpoint(host, port) if local is None else local
        self.session = XvcSession(
            shifter, max_bits=max_bits, accept_bits=self.accept_bits, trace=trace
        )
        self._closed = False

    @property
    def port(self) -> "Optional[int]":
        return getattr(self.local, "port", None)

    def _reset_session(self) -> None:
        self.session = XvcSession(
            self.shifter,
            max_bits=self.max_bits,
            accept_bits=self.accept_bits,
            trace=self.trace,
        )

    def _drop_client(self) -> None:
        drop = getattr(self.local, "drop_client", None)
        if callable(drop):
            drop()
        else:  # pragma: no cover - only for a foreign endpoint
            self.local.close()
            self.local.open()
        self._reset_session()

    def pump_once(self, timeout: float = 0.5) -> XvcPumpStat:
        """At most one read + dispatch + reply.

        A protocol violation drops the client and is reported on the stat (a
        hostile/desynced client is not our emergency). A
        :class:`XvcBackendError` also drops the client but is **re-raised**: a
        dead ``xsdb`` or an unloaded shell must stop the tool, not quietly return
        zeros that read as a TAP in BYPASS.
        """
        stat = XvcPumpStat()
        self.local.open()

        # Accept as its OWN step. Reading straight through a lazy accept would
        # block in recv() on a client that has connected but not yet spoken --
        # which is precisely what an XVC client does on attach.
        has_client = getattr(self.local, "has_client", None)
        accept = getattr(self.local, "accept_if_pending", None)
        if callable(accept) and has_client is not None and not has_client:
            readable, _, _ = select.select([self.local.fileno()], [], [], timeout)
            if not readable:
                return stat
            if accept(0.0):
                stat.accepted = True
                self._reset_session()
                if self.trace is not None:
                    self.trace.event("accept", port=self.port)
            return stat

        readable, _, _ = select.select([self.local.fileno()], [], [], timeout)
        if not readable:
            return stat

        try:
            data = self.local.read(4096)
        except OSError as exc:
            stat.dropped = True
            stat.error = f"read failed: {exc}"
            if self.trace is not None:
                self.trace.event("read-failed", error=repr(str(exc)))
            self._drop_client()
            return stat

        if not data:
            # Peer hung up. TcpListenEndpoint already cleared its connection;
            # discard any half-parsed command so a new client starts clean.
            stat.client_closed = True
            if self.trace is not None:
                self.trace.event("client-closed", pending=self.session.pending)
            self._reset_session()
            return stat

        stat.rx = len(data)
        if self.trace is not None:
            self.trace.rx(data)
            self.trace.event("rx", nbytes=len(data), pending=self.session.pending)
        before = (self.session.commands, self.session.shifts, self.session.bits_shifted)
        try:
            reply = self.session.feed(data)
        except XvcProtocolError as exc:
            stat.dropped = True
            stat.error = str(exc)
            if self.trace is not None:
                self.trace.event("drop", why="protocol", error=repr(str(exc)))
            self._drop_client()
            return stat
        except XvcBackendError as exc:
            stat.dropped = True
            stat.error = str(exc)
            if self.trace is not None:
                self.trace.event("drop", why="backend", error=repr(str(exc)))
            self._drop_client()
            raise
        stat.commands = self.session.commands - before[0]
        stat.shifts = self.session.shifts - before[1]
        stat.bits = self.session.bits_shifted - before[2]

        if reply:
            try:
                self.local.write(reply)
            except OSError as exc:
                stat.dropped = True
                stat.error = f"write failed: {exc}"
                if self.trace is not None:
                    self.trace.event("write-failed", error=repr(str(exc)))
                self._drop_client()
                return stat
            stat.tx = len(reply)
            if self.trace is not None:
                self.trace.tx(reply)
                self.trace.event("tx", nbytes=len(reply))
        return stat

    def serve_forever(self, *, poll: float = 0.5, log=None) -> None:
        """Pump until :meth:`close` or ``KeyboardInterrupt``."""
        self._closed = False
        self.local.open()
        try:
            while not self._closed:
                stat = self.pump_once(poll)
                if log is not None and (stat.commands or stat.dropped or stat.client_closed):
                    log(stat)
        except KeyboardInterrupt:  # pragma: no cover - operator ^C
            pass
        finally:
            self.close()

    def close(self) -> None:
        self._closed = True
        try:
            self.local.close()
        except Exception:  # pragma: no cover
            pass
        if self.shifter is not None:
            try:
                self.shifter.backend.close()
            except Exception:  # pragma: no cover
                pass


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m socket_harness.xvc_server",
        description=(
            "XVC 1.0 server bit-banging a soft JTAG TAP in the MPS3 DFX RM via "
            "SWDBB (DRIVE/SAMPLE) over a persistent xsdb session. "
            "NEVER RUN AGAINST HARDWARE as of this writing."
        ),
    )
    p.add_argument("--host", default="0.0.0.0",
                   help="bind address (default: %(default)s)")
    p.add_argument("--port", type=int, default=DEFAULT_XVC_PORT,
                   help="XVC TCP port (default: %(default)s)")
    p.add_argument("--hw-server-url", default=DEFAULT_HUB_URL,
                   help="hw_server URL for `connect -url`; MUST be the FQDN "
                        "form (default: %(default)s)")
    p.add_argument("--swdbb-base", default=hex(SWDBB_BASE),
                   help="SWDBB block base address (default: %(default)s)")
    p.add_argument("--xsdb", default="xsdb", help="xsdb executable")
    p.add_argument("--max-bits", type=int, default=XVC_MAX_VECTOR_BITS,
                   help="advertised getinfo: vector limit -- a CHUNK HINT the "
                        "client sizes its payload against, not a ceiling on "
                        "num_bits (default: %(default)s)")
    p.add_argument("--accept-bits", type=int, default=None,
                   help="hard ceiling on an accepted shift:; above it we fail "
                        "closed. MUST exceed --max-bits, because clients add "
                        "TAP-navigation bits on top of the advertised payload "
                        f"size (default: {XVC_ACCEPT_RATIO} x --max-bits, the "
                        "ratio the XAPP1251 reference server itself runs at)")
    p.add_argument("--timeout", type=float, default=300.0,
                   help="per-batch xsdb deadline in seconds (default: %(default)s)")
    p.add_argument("--dry-run", action="store_true",
                   help="spawn nothing; print the config and the Tcl a shift "
                        "would emit, with the cost estimate, then exit")
    p.add_argument("--fake-tap", action="store_true",
                   help="serve against an in-process FakeJtagTap instead of a "
                        "board -- lets the Identify side (cable, chain add, "
                        "IDCODE) be proven with no hardware at all")
    p.add_argument("--idcode", default="0x6BA00477",
                   help="--fake-tap IDCODE (default: %(default)s)")
    p.add_argument("--ir-len", type=int, default=4, dest="ir_len",
                   help="--fake-tap IR length (default: %(default)s). Vivado's "
                        "chain scan only lists a device it recognises, so a "
                        "Vivado smoke against the fake uses a Xilinx IDCODE and "
                        "its real IR length (scripts/mps3_xvc_smoke.sh --fake)")
    p.add_argument("--trace-file",
                   help="append a per-COMMAND trace (bits, TMS/TDI/TDO byte "
                        "lengths, payload edges) to this file, flushed per "
                        "record. Use this and not stderr: stderr through a pipe "
                        "is block-buffered, so the records that diagnose a "
                        "client-side 'couldn't shift' never arrive.")
    p.add_argument("--trace-raw", action="store_true",
                   help="with --trace-file, also capture the verbatim byte "
                        "streams to <trace-file>.rx / .tx -- what a real "
                        "client's traffic must be replayed from")
    p.add_argument("--target-filter",
                   default='name =~ "MicroBlaze*#0" && '
                           'jtag_cable_name =~ "*210249B86C47*"',
                   help="xsdb `targets -set -filter` expression, run once after "
                        "connect. REQUIRED on hardware: connect alone leaves no "
                        "current target and the first mrd/mwr dies with "
                        "'Invalid target'. KEEP the jtag_cable_name clause -- "
                        "the hw_server is shared with four other boards. "
                        "(default: %(default)s)")
    return p


def main(argv: "Optional[Sequence[str]]" = None) -> int:
    args = _build_parser().parse_args(argv)
    base = int(args.swdbb_base, 0)
    cfg = XsdbConfig(hub_url=args.hw_server_url, xsdb=args.xsdb)
    accept_bits = (
        XVC_ACCEPT_RATIO * args.max_bits
        if args.accept_bits is None
        else args.accept_bits
    )
    if accept_bits < args.max_bits:
        _build_parser().error(
            f"--accept-bits ({accept_bits}) is below --max-bits "
            f"({args.max_bits}); the accept ceiling must be at least the "
            "advertised size, and should exceed it (see XVC_ACCEPT_RATIO)"
        )

    if args.dry_run:
        return _dry_run(args, base, cfg)

    if args.fake_tap:
        backend: SwdbbBackend = FakeSwdbbBackend(
            FakeJtagTap(int(args.idcode, 0), ir_len=args.ir_len), base=base
        )
        print(
            f"[xvc] FAKE TAP mode: in-process FakeJtagTap idcode="
            f"{int(args.idcode, 0):#010x}. No xsdb, no board.",
            file=sys.stderr,
        )
    else:
        session = PersistentXsdbSession(
            cfg, timeout_s=args.timeout,
            target_filter=args.target_filter,
        ).start()
        backend = XsdbSwdbbBackend(session, base=base)
        print(f"[xvc] persistent xsdb session up ({cfg.hub_url})", file=sys.stderr)

    trace: "Optional[XvcTrace]" = None
    if args.trace_file:
        trace = XvcTrace(args.trace_file, raw=args.trace_raw)
        trace.event("start", port=args.port, advertised=args.max_bits,
                    accept_bits=accept_bits, fake_tap=int(bool(args.fake_tap)))
        print(f"[xvc] tracing every command to {args.trace_file}"
              + (" (+ .rx/.tx raw capture)" if args.trace_raw else ""),
              file=sys.stderr)

    shifter = SwdbbJtagShifter(backend, base=base, max_bits=accept_bits)
    server = XvcServer(
        shifter, host=args.host, port=args.port, max_bits=args.max_bits,
        accept_bits=accept_bits, trace=trace,
    )
    server.local.open()
    print(
        f"[xvc] listening on {args.host}:{server.port} "
        f"(SWDBB {_hex(base)}, advertising {args.max_bits} bits/shift, "
        f"accepting up to {accept_bits})\n"
        f"[xvc] point Identify at it:  server set -addr <host> -port {server.port}",
        file=sys.stderr,
    )

    def log(stat: XvcPumpStat) -> None:
        if stat.dropped:
            print(f"[xvc] client dropped: {stat.error}", file=sys.stderr)
        elif stat.client_closed:
            print("[xvc] client closed", file=sys.stderr)
        elif stat.shifts:
            print(
                f"[xvc] {stat.shifts} shift(s), {stat.bits} bits, "
                f"~{shifter.ops_per_shift(stat.bits)} register accesses",
                file=sys.stderr,
            )

    try:
        server.serve_forever(log=log)
    finally:
        if trace is not None:
            trace.event("stop", records=trace.records)
            trace.close()
    return 0


def _dry_run(args: argparse.Namespace, base: int, cfg: XsdbConfig) -> int:
    """Print everything a real run would do, without doing any of it."""
    shifter = SwdbbJtagShifter(
        FakeSwdbbBackend(base=base), base=base, max_bits=args.max_bits
    )
    demo_bits = 8
    tms = bytes([0b0000_0001])
    tdi = bytes([0b1010_1010])
    lows = shifter.drive_values(demo_bits, tms, tdi)
    script = build_shift_script(
        shifter.drive_addr, shifter.sample_addr, lows, lows[-1]
    )
    full = build_shift_script(
        shifter.drive_addr,
        shifter.sample_addr,
        [0] * args.max_bits,
        0,
    )
    ops = shifter.ops_per_shift(args.max_bits)
    print(f"bind            : {args.host}:{args.port}")
    print(f"hw_server       : {cfg.hub_url}")
    print(f"xsdb            : {cfg.xsdb}  (ONE persistent process)")
    print(f"SWDBB base      : {_hex(base)}"
          f"  DRIVE {_hex(base + SWDBB_DRIVE)}"
          f"  SAMPLE {_hex(base + SWDBB_SAMPLE)}")
    print(f"max bits/shift  : {args.max_bits}")
    print(f"accesses/shift  : {ops} (3 per JTAG bit + 1 park)")
    print(f"script bytes    : {len(full)} for a maximal shift")
    print("cost estimate   : ESTIMATE ONLY, never measured on hardware -- at "
          "1-5 ms per")
    print(f"                  remote AXI access, {ops} accesses ~= "
          f"{ops * 0.001:.1f}-{ops * 0.005:.1f} s per maximal shift")
    print()
    print(f"--- Tcl for a {demo_bits}-bit shift "
          f"(tms={tms.hex()} tdi={tdi.hex()}) ---")
    print(script)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
