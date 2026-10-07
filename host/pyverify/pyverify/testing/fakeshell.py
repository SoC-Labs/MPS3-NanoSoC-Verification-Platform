"""``FakeShell`` — stdlib-only reference implementation of the shell's
network surface (``docs/contracts/net-protocol.md``).

Lets the entire host stack (:class:`pyverify.client.ShellClient`,
:class:`pyverify.swap.SwapOrchestrator`, the pusher,
:class:`pyverify.console.ConsoleReader`, :class:`pyverify.edge.EdgeDeviceApi`,
``pyverify.cli``) run end-to-end with **no hardware** — and doubles as the
**executable spec for A3's firmware**: every behaviour here cites the
contract clause or firmware file it mirrors, and the swap behaviour is
driven by the same :mod:`pyverify.testing.swap_model` Python port of
``firmware/coordinator/swap_fsm.c`` that the integration suite checks
against the C.

Services implemented (net-protocol.md port map; every port configurable,
``0`` = ephemeral, bound port exposed as an attribute after :meth:`start`):

===========  =====  ======================================================
default      proto  service
===========  =====  ======================================================
6900         TCP    control/status — JSON-lines request/response, all 8
                    verbs with the per-op response fields of the contract
                    (the 7 core verbs + macgen, net-protocol.md v0.4)
69           UDP    TFTP (RFC1350 WRQ/octet receive) bitstream push
6910         TCP    raw bitstream push (alt to TFTP)
6930/31/32   TCP    UART0/UART1/SWO consoles — configurable banner on
                    connect, then echo (or absorb)
===========  =====  ======================================================

Not implemented (out of W-FAKESHELL scope, per the port map's remaining
rows): XVC (2542) and SWD remote_bitbang (6920) — both are raw byte
protocols to *other* tools (Vivado/OpenOCD), not exercised by pyverify's
own code paths.

Bitstream receive validation mirrors ``firmware/config_agent/config_agent.c``
exactly (see :class:`ConfigAgentModel`): 24-byte ``"MPS3"`` header (reusing
the pusher's real framing via :mod:`pyverify.testing._pusher` — never
re-implemented), version, ``static_id`` match, kind, size bound, **I2
clearing-then-partial ordering**, then full-payload CRC32 — a torn or
mismatched transfer is rejected *before* any (modelled) ICAP write.

Synchronous, threaded design (``socketserver``/``threading``) to match
pyverify's synchronous style; no asyncio, no third-party deps.

ENGINE PROFILES (Linux harness plan §10; all opt-in, the DEFAULT is unchanged):

- ``profile="bare-metal"`` (default) -- exactly the behaviour this double has
  always had: no ``version.impl``, ``lmb_kb`` 1024, the 12 pre-v0.11 verbs.
- ``profile="linux"`` -- ``mps3-harnessd`` on the MicroBlaze V:
  ``version.impl == "linux"`` (the LAST key), ``lmb_kb`` 128, the PRODUCT=1
  feature set, the v0.11 verbs (``stats`` with the additive ``os_up_ms``,
  ``log``, ``touch_cal``, ``reboot``), ``diag`` OMITTING the keys harnessd has
  no source for, the ``identify`` responder on UDP 6899, and the TOFU
  ``authorized_keys`` claim over TFTP (accepted once, then TFTP error 2).
- ``hung=True`` -- 6900 ACCEPTS (the kernel's listen backlog) but never
  replies, and identify is silent: a client reads that as "wedged". A DEAD
  harnessd is :meth:`FakeShell.stop` -- the connect is refused, "offline" --
  and :meth:`FakeShell.start` again revives it on the same ports.
- ``mode="rescue"`` -- stage0's TFTP rescue server: TFTP (any WRQ is a boot
  blob; RRQ ``stage0.status`` returns the status block) and
  ``identify`` with ``mode:"rescue"``; nothing listens on 6900/6910/6930-2.

THE USER microSD (net-protocol.md v0.13, D13; both profiles). The ``usd`` verb
and the re-push ``commit`` are modelled on a card the scenario puts in the slot
(``usd_card=`` a layout, ``usd_default=`` a committed overlay, ``usd_fault=``
a card that is not ready, :meth:`FakeShell.usd_insert` / :meth:`usd_remove`
for card detect). Every rule of the contract section is enforced here:

- ``usd`` status: every ``state``; ``present`` false with no card is ``ok``;
  ``card_mb`` only when a card is ready; ``default`` only when ``valid`` or
  ``stale``; ``text`` is the CLCD string (``skipped`` while PB1 was held);
  ``boot`` is the power-on decision, made ONCE, at construction (PB1 held:
  ``skipped``; a VALID store over the greybox: ``loaded``; still probing:
  ``pending``; anything else: ``none`` -- the state says why).
- ``format``: ``confirm`` must be ``erase`` or ``erase-all``; plain format
  writes only under rule (a) (a ``0xDA`` partition) or (b) (a truly blank
  card), otherwise ``filesystem present`` / ``exists`` and nothing changes;
  ``erase-all`` wipes any card, and is ``wipe disabled`` on the linux profile.
- ``commit``: the v0.11 form (``rm`` only) is ``bad args``; refused with
  ``no card`` / ``no hw`` / ``foreign`` / ``unavailable`` / ``store busy`` /
  ``stale key`` (a ``static_id`` that is not the shell's) / ``rm mismatch``
  (not the live ``rm_id``) BEFORE it parks; then it parks like ``swap`` and
  admits the clearing, then the partial, over 6910 ONLY; a length or CRC-32
  that is not the request's is ``crc``; nothing pushed within the swap idle
  timeout is ``timeout``; the pair lands in the inactive slot and only then
  does the header flip, so any failure leaves the previous default intact.
- Error strings are the contract's NAMES (:data:`USD_ERRORS`). Where the
  contract is silent on the ORDER of two refusals, this double follows the
  landed store (``firmware/overlay_store/ovlstore_sd.c``): the card-state gate,
  then busy, then ``static_id``, then ``rm_id``.

THE BOOT SLOTS (net-protocol.md v0.14 "Slot images"; OPT-IN, linux profile only).
``FakeShell(profile="linux", slots={...})`` models ``mps3-harnessd``'s ``slot``
verb (``status`` / ``commit`` / ``rollback`` / ``verify``, every refusal in the
contract's words and the C's order) and the kind-2 slot-image push on 6910 and
TFTP (stage0's table rules, the region CRCs, the background read-back as a job),
plus THE LOCK: on a claimed board (``ssh_claimed``, or a TOFU claim) a
non-loopback peer's ``commit`` / ``rollback`` is ``slot locked: board claimed
(use ssh)``, its 6910 push is closed unread and its TFTP push gets ERROR 2.
Without ``slots=`` nothing changes: ``slot`` is an unknown op and a kind-2 push
is refused as an unknown kind, exactly as before (the keys: :class:`_SlotModel`).

Usage::

    with FakeShell.ephemeral() as fake:
        with ShellClient(fake.host, fake.control_port) as shell:
            shell.ping()
            ...

Known contract ambiguities surfaced by implementing this (flagged for A6,
contracts NOT edited here):

- **Raw-TCP push has no acknowledgement.** net-protocol.md defines no
  server->client response on 6910, so a client that does ``sendall`` +
  ``close`` cannot know the push was accepted (TFTP's final ACK gives that
  for free). This server closes the connection when it has finished
  validating; :func:`raw_tcp_put` waits for that EOF as a synchronisation
  point. A real pusher should either do the same or the contract should
  add an explicit status byte.
- **Push-vs-swap interleaving — RESOLVED ON SILICON (2026-07-14), see below.**
  This note used to read "net-protocol.md's swap sequence has the host push
  *both* files of the pair before ``swap`` ... A3's firmware will need
  two-slot staging or a stalling transport". The firmware went the other way,
  and the ordering is now enforced here (:meth:`FakeShell._op_swap`).
- **The receiver does not require clearing.rm_id == partial.rm_id** within
  a pair (config_agent.c has no such check; ordering is by *kind* only).
  If the pair files disagree, the swap still verifies against the
  *partial*'s rm_id per I14/I25. Worth an explicit contract statement.
- **link.event values**: the contract shows only ``"down"``; spec §8.1
  mentions up/down/**speed** events. This server accepts ``up``/``down``/
  ``pulse`` (matching the firmware, I31(b)) and rejects speed events
  (fail closed).

I31 RESOLVED (2026-07-07): (a) the failure-diagnostic key is now ``"err"``
here too, matching the firmware's ``mps3_ctrl_encode_response()`` and pinned
in net-protocol.md v0.2 — the test double follows the real server; (b)
``link.event`` accepts ``pulse`` as above. Clients still key only on ``ok``.

**SWAP ORDERING — THE FIDELITY FIX (2026-07-14).** This server used to accept a
bitstream push in ANY state and answer ``swap`` immediately. That made it **more
permissive than the firmware**, and it is exactly why a real host-side ordering
bug (``push_pair()`` then ``swap()``) stayed green in the whole E2E suite while
being dead on silicon. The real, hardware-verified sequence is::

    swap_begin()  ->  push clearing  ->  push partial  ->  swap_await()

so this server now models it:

1. ``{"op":"swap",...}`` does **not** reply. It runs the FSM to
   ``SWAP_AWAIT_INCOMING_CLEARING`` and then **parks the control connection**
   for the whole reconfiguration — which is *why* the real shell parks 6900.
   Only at that point does it start admitting bitstreams (:attr:`FakeShell.awaiting`).
2. The clearing is admitted, the FSM advances to ``SWAP_AWAIT_PARTIAL``, the
   partial is admitted, VERIFY runs, and only THEN does the parked reply go out.
3. **A push that arrives when the shell is not awaiting one is REJECTED**:
   raw-TCP gets an RST (the ECONNRESET the real shell gives; ``SO_LINGER`` 0),
   TFTP gets an ERROR packet. Recorded as a ``PushEvent`` with status
   ``ERR_NOT_AWAITING``.
4. Nothing arriving within ``swap_await_timeout`` fails the swap through the
   FSM's own ``await_timeout`` transition (``swap_fsm_transitions.c``: the
   ``SWAP_AWAIT_*`` states are the only ones with an idle timeout, and it is
   re-armed on RX progress — so an in-flight transfer keeps the swap alive).

Firmware provenance of the gate: ``swap_fsm.c``'s ``icap_direct_begin()`` fails
closed unless ``s_state == SWAP_AWAIT_PARTIAL`` (its own fail-closed fix, silicon
2026-07-10), and ``config_agent.c`` then closes the socket on that rejection.
The check sits where the C puts it — **after** header validation
(``begin_payload()``), so a bad magic/version/static_id/kind or an I2 ordering
violation is still reported as *that* error, not as ``ERR_NOT_AWAITING``.

Where this model is deliberately STRICTER than the C: config_agent.c only hits
the arming gate on the sink path (a payload larger than its RAM staging buffer);
a *small* clearing or partial lands in RAM and would be staged even while idle.
That leniency is an artefact of the staging fast path, not a protocol guarantee —
on real hardware every partial is MiB-scale and takes the ICAP-direct sink — and
it is precisely the slack the wrong ordering hid in. The reference server closes
it: a push is admitted only while a swap is awaiting exactly that kind.

**Uniform failure shape (2026-07-08):** every verb's failure reply is now
exactly ``{"ok":false,"err":"<diagnostic>"}`` — the fakeshell no longer echoes
per-verb steady-state fields on failure (``set_clk``'s ``locked``, ``swap``'s
``verified``/``rm_id``), because the firmware's ``mps3_ctrl_encode_response()``
emits none. That fakeshell-only divergence (the audit's I31 tail) is now caught
automatically by ``tests/firmware_logic/test_fakeshell_conformance.py``, which
runs one request list through BOTH the real ``ctrl_echo`` firmware binary and
this double and asserts the response *key sets*/types agree per verb (values
that legitimately depend on scenario/config are the only permitted delta). The
client response dataclasses default those dropped fields (``locked``/
``verified``/``rm_id``) to False/"" when absent, so nothing downstream regresses.
"""
from __future__ import annotations

import json
import re
import socket
import socketserver
import struct
import threading
import time
import zlib
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..client import CONTROL_PORT, MACGEN_INJECTS, usd_boot_word
from ..console import SWO_PORT, UART0_PORT, UART1_PORT
from ._pusher import (
    HEADER_SIZE,
    MAGIC,
    RAW_TCP_PORT,
    TFTP_PORT,
    BitstreamFramingError,
    BitstreamHeader,
    BitstreamKind,
)
from .swap_model import (
    DFXCTL_RM_STATUS_RM_ID_VALID,
    STATIC_ID,
    ClearingRef,
    SwapFsm,
    make_regs_confirmed,
    next_state,
)

__all__ = [
    "MPS3_BITSTREAM_VER",
    "MAX_PAYLOAD_WORDS",
    "NOT_AWAITING",
    "ConfigAgentStatus",
    "BitstreamInfo",
    "PushEvent",
    "ConfigAgentModel",
    "FakeShell",
    "TftpError",
    "tftp_put",
    "raw_tcp_put",
]
__all__ += [
    "USD_STATES",
    "USD_ERRORS",
    "USD_CARD_LAYOUTS",
    "USD_FAULTS",
    "USD_SLOT_BYTES",
    "UsdSlot",
    "PROFILES",
    "LINUX_PROFILE_FEATURES",
    "IDENTITY_DEFAULTS",
    "IDENTITY_LOCKED_ERR",
    "IDENTITY_NO_PERSIST_ERR",
    "IDENTITY_NOT_SUPPORTED_ERR",
    "identity_check",
    "PANEL_LOCKED_ERR",
    "PANEL_STATUS_ROWS",
    "PANEL_STATUS_ROWS_ALIGNED",
    "presence_clip",
    "ENGINE_FEATURES",
    "LCD_MIRROR_PORT",
    "IDENTIFY_PORT",
    "TOFU_FILENAME",
    "LINUX_OMITTED_DIAG_KEYS",
    "STATS_KEYS",
    "TOUCH_CAL_DEFAULT",
    "SLOT_IMAGE_KIND",
    "SLOT_LOCKED_ERR",
    "SLOT_BYTES",
    "SLOT_VERB_CODES",
    "SLOT_JOB_CODES",
    "slot_code",
]

#: :attr:`PushEvent.status` for a push refused because no swap was awaiting one.
#:
#: Deliberately NOT a :class:`ConfigAgentStatus` member: that enum is a faithful
#: mirror of config_agent.h's ``config_agent_status_t``, and this rejection does
#: not come from the config agent at all — in the firmware it is
#: ``swap_fsm.c``'s ``icap_direct_begin()`` returning -1 because the FSM is not
#: in ``SWAP_AWAIT_PARTIAL``, which config_agent.c then turns into a closed
#: socket. Keeping it out of the enum keeps the mirror honest.
NOT_AWAITING = "ERR_NOT_AWAITING"

#: net_proto.h MPS3_BITSTREAM_VER — the one header version the config agent
#: accepts (frame_bitstream's default).
MPS3_BITSTREAM_VER = 1

#: config_agent.h MPS3_CFG_AGENT_MAX_PAYLOAD_WORDS (4 MiB of payload).
MAX_PAYLOAD_WORDS = 4 * 1024 * 1024 // 4

#: ``version`` feature names, in the firmware's FIXED bit order
#: (``net_proto.h``'s ``MPS3_FEATURE_*`` / ``net_proto.c``'s
#: ``s_feature_names``). The wire array is emitted in THIS order with absent
#: features omitted, so the two servers' lines compare byte-for-byte.
VERSION_FEATURES: Tuple[str, ...] = (
    "clcd", "clcd_kvm", "touch", "hwicap_fifo", "windowed",
    # net-protocol v0.11 -- APPENDED in the firmware's bit order (5..12).
    "dut_egress", "jtag_server", "xvc_dbgbr", "xvc_jtagbb",
    "stats", "log", "reboot", "touch_cal",
    # net-protocol v0.13 -- APPENDED (bit 13 on this branch; clients test the
    # NAME, so a renumber at merge moves nothing here but the position).
    "usd",
    # v0.14 amendments (2026-09-26, HM_ANSWERS) -- APPENDED in landing order.
    # These follow a LINKED PROVIDER in the firmware (weak seams), not a build
    # flag: harnessd reports them, bare metal never does.
    "slot", "xvc_lock",
)

#: ENGINE feature names (net-protocol v0.15, ``net_proto.h``
#: ``mps3_proto_features_extra()``): emitted AFTER every bit name above, in this
#: order. They spend no ``MPS3_FEATURE_*`` bit -- only the Linux harness has them.
#: ``lcd_mirror``: the pixel-exact LCD mirror on TCP 127.0.0.1:6940, which also
#: adds ``version.lcd_mirror`` and ``stats.lcd_mirror``. ``identity`` (v0.16): the
#: board-identity verbs ``identity`` / ``identity_set`` (and identify's ``label``).
#: ``locate`` (v0.16, a build with the panel): the Harness Manager's identify-board
#: blink (``locate``). ``presence`` and ``panel`` (v0.17, a build with the panel):
#: the Harness Manager's ``hello`` (the session table) and ``panel`` (the state, a
#: frame half, a page change).
ENGINE_FEATURES: Tuple[str, ...] = ("lcd_mirror", "identity", "locate", "presence", "panel")

#: The LCD mirror's port (loopback only; net-protocol.md "LCD mirror (TCP 6940)").
LCD_MIRROR_PORT = 6940

#: ``diag`` counter keys, in the firmware's emitted order
#: (``net_proto.c``'s ``MPS3_OP_DIAG`` snprintf). Order matters: the
#: conformance suite compares the two servers' diag lines byte-for-byte, and a
#: Python dict preserves insertion order.
DIAG_COUNTERS: Tuple[str, ...] = (
    "rx_recover", "rx_dumps", "rx_drops", "icap_bytes", "got", "expect",
    "rcv_wnd", "rcv_ann_wnd", "rx_queued", "pbuf_free", "grants_sent",
    "grant_fails", "sndbuf", "snd_wnd",
    # v0.9: the verb now carries the WHOLE mailbox (diag.h MPS3_DIAG_FIELDS),
    # the 14 frozen keys above first, these 11 appended in X-macro order.
    "tx_frames_sent", "tx_status_drained", "tx_fifo_full_drops", "tx_errors",
    "tx_space_stalls", "tx_iface_errors", "tx_last_status", "icap_sr_last",
    "icap_eos_status", "ovlstore_phase", "ovlstore_detail",
    # v0.9.1: the STMPE811 panel-continuity probe (diag.h v7), appended in the
    # same X-macro order -- three packed evidence words and the verdict.
    "touch_regs", "touch_adc_x", "touch_adc_y", "touch_verdict",
    # v0.9.2: the superloop service telemetry (diag.h v8), appended in X-macro
    # order -- the per-pass/per-service worst-case microseconds, the
    # budget-overrun counters and the sick-service skip mask
    # (firmware/common/service.h).
    "svc_count", "pass_max_us", "svc_max_us", "svc_max_ix", "svc_overruns",
    "svc_skips", "svc_skipped",
    "svc_us_0", "svc_us_1", "svc_us_2", "svc_us_3", "svc_us_4", "svc_us_5",
    # diag.h v9 (D13): two formerly-reserved words, appended in X-macro order --
    # service 12 ("usd")'s worst case and the power-on load latch.
    "svc_us_6", "usd_boot",
)

#: ``dutrx`` chunk size -- ``net_proto.h``'s ``MPS3_DUTRX_CHUNK_MAX``. One
#: reply carries at most this many bytes of the head frame, hex-encoded. It is
#: a MATCHED PAIR with the firmware constant: a fake that chunked differently
#: would let a client that only ever ran against this double reassemble frames
#: by an offset arithmetic the real shell does not use.
DUTRX_CHUNK = 256

# --------------------------------------------------------------------------- #
# Profiles: which ENGINE this double models (Linux harness plan §10)
# --------------------------------------------------------------------------- #
#: UDP port of the ``identify`` probe (plan §10; pyverify.identify is the client).
IDENTIFY_PORT = 6899

#: The TFTP WRQ filename that claims a first SSH key (TOFU, plan §10): no MPS3
#: header, accepted while unclaimed, TFTP error 2 once claimed.
TOFU_FILENAME = "authorized_keys"

#: The feature set ``mps3-harnessd`` reports: the PRODUCT=1 set MINUS
#: ``windowed``, which harnessd builds without on purpose (its kernel sockets are
#: paced by their consumer: harnessd/Makefile "THE FLAG SET", HARNESSD_CONTRACT
#: §9.5). Listing ``windowed`` here made a client deploy to the fake with the
#: windowed TCP push where the real board takes plain pushes (BOARD-RUNNER's
#: find, 2026-09-24). Pinned against harnessd's own build twice: its Makefile
#: flags (tests/test_linux_profile.py) and a host-built harnessd's ``version``
#: (src/linux_harness/sw/harnessd/tests/test_fakeshell_parity.py). A scenario
#: that knows better passes ``features=`` explicitly.
LINUX_PROFILE_FEATURES: Tuple[str, ...] = (
    "clcd", "clcd_kvm", "touch", "hwicap_fifo", "dut_egress",
    "jtag_server", "xvc_dbgbr", "stats", "log", "reboot", "touch_cal", "usd",
    # v0.14 amendments: harnessd links the providers in every build (slot_linux.c).
    "slot", "xvc_lock",
    # v0.15 engine name (after every bit name): the LCD mirror
    "lcd_mirror",
    # v0.16 engine names: the board identity (identity / identity_set), locate
    "identity", "locate",
    # v0.17 engine names: presence (hello) and the front panel (panel)
    "presence", "panel",
)

#: Opt-in engine profiles. ``bare-metal`` is today's behaviour, byte for byte,
#: and is the default -- every existing caller (socharness pins it) is
#: unchanged. ``linux`` models ``mps3-harnessd`` on the MicroBlaze V:
#: ``version.impl == "linux"``, the 128 KiB LMB, the harnessd feature set, the
#: identify responder on UDP 6899 and the TOFU ``authorized_keys`` claim.
#: ``diag`` keys ``mps3-harnessd`` has NO source for -- the bare-metal LAN9220
#: driver's counters, lwIP's pcb/pbuf view, and the QSPI overlay store -- which
#: the Linux engine OMITS from the reply (the codec's validity mask,
#: ``mps3_proto_diag_omit()`` in src/linux_harness/sw/harnessd/version_linux.c)
#: rather than sending a zero that reads like a measurement. pyverify's
#: ``DiagResponse.present`` tells the two apart. Kept in step with that mask by
#: tests/firmware_logic/test_fakeshell_conformance.py's harnessd run (the
#: key-set comparison fails on any difference); ``omit_diag_keys=`` overrides.
LINUX_OMITTED_DIAG_KEYS: Tuple[str, ...] = (
    "rx_recover", "rx_dumps", "rcv_wnd", "rcv_ann_wnd", "rx_queued", "pbuf_free",
    "sndbuf", "snd_wnd", "tx_status_drained", "tx_fifo_full_drops",
    "tx_space_stalls", "tx_iface_errors", "tx_last_status",
    "ovlstore_phase", "ovlstore_detail",
)

#: ``stats`` keys, in wire order (net-protocol.md "Stats"): fpgahub's 22, then
#: the five additive extras.
STATS_KEYS: Tuple[str, ...] = (
    "up_ms", "sid", "rm", "rm_ok", "lock", "clk_sel", "mmcm", "clk_alive",
    "dut_rst", "rp_rst", "decpl", "link", "spd", "fdx", "mac", "swap",
    "swap_ok", "swap_n", "icap", "rxdrop", "txerr",
    "swap_err", "clr_ok", "dut_mhz", "svc_max_us", "svc_skipped",
)

#: ``touch_cal`` default map == firmware/touch/touch.h TOUCH_CALIB_DEFAULT: the
#: 2026-09-24 silicon fit (ax bx cx ay by cy shift; the panel's axes are swapped
#: and mirrored -- docs/evidence/2026-09-w3/touch_cal_20260924.txt).
TOUCH_CAL_DEFAULT: Tuple[int, ...] = (21, 377, -158863, -281, -2, 1091113, 12)
_TOUCH_CAL_KEYS: Tuple[str, ...] = ("ax", "bx", "cx", "ay", "by", "cy", "shift")

PROFILES: Dict[str, Dict[str, Any]] = {
    "bare-metal": {"impl": None, "lmb_kb": 1024, "features": (),
                   "identify": False, "tofu": False, "v011_verbs": False,
                   "omit_diag_keys": (), "has_overlay_store": True,
                   "usd_allow_wipe": True},
    # has_overlay_store True (v0.13): the linux profile models harnessd WITH the
    # D13 store over the POSIX block-device provider (handover §12.6/§13.4) --
    # the v0.13 target. A scenario modelling a harnessd that links no store
    # passes has_overlay_store=False; `usd` and a well-formed `commit` then
    # answer "unavailable". usd_allow_wipe False: under Linux the card also
    # holds the running system (p1-p3), so `erase-all` is "wipe disabled".
    "linux": {"impl": "linux", "lmb_kb": 128, "features": LINUX_PROFILE_FEATURES,
              "identify": True, "tofu": True, "v011_verbs": True,
              "omit_diag_keys": LINUX_OMITTED_DIAG_KEYS, "has_overlay_store": True,
              "usd_allow_wipe": False},
}

# --------------------------------------------------------------------------- #
# The user microSD (net-protocol.md v0.13 "User microSD (usd) and commit")
# --------------------------------------------------------------------------- #
#: ``usd.state`` values, in the contract's order.
USD_STATES: Tuple[str, ...] = (
    "no_hw", "none", "init", "unsupported", "error",
    "foreign", "empty", "valid", "stale", "bad",
)
#: States in which the card is READY (initialised): ``card_mb`` is reported.
USD_READY_STATES = frozenset({"foreign", "empty", "valid", "stale", "bad"})
#: States a ``commit`` / ``clear`` may write over.
USD_WRITABLE_STATES = frozenset({"empty", "valid", "stale", "bad"})
#: The contract's error NAMES for ``usd`` and ``commit``. Names, never errno
#: numbers (``ETIMEDOUT`` is 110 on the host and 116 in newlib).
USD_ERRORS = frozenset({
    "no card", "no hw", "foreign", "stale key", "unavailable",
    "filesystem present", "exists", "partition too small", "confirm required",
    "wipe disabled", "rm mismatch", "crc", "store busy", "io", "timeout",
    "bad args",
})
#: What a card in the slot can hold at LBA 0 and behind it (the store's MBR
#: classifier, ``ovlsd_mbr_kind_t``): ``blank`` (LBA 0 all zero), ``empty_mbr``
#: (a signed MBR with four empty entries) -- both TRULY blank, format rule (b);
#: ``da`` (a ``0xDA`` partition: the store, formatted or not -- rule (a));
#: ``da_small`` (a ``0xDA`` entry smaller than the layout); ``other`` (other
#: partitions, no ``0xDA`` -- rule (c)); ``fs`` (a FAT/exFAT/NTFS/ext/...
#: filesystem), ``gpt`` (a GPT header) and ``unknown`` (not zero, not a clean
#: MBR) -- all "not blank".
USD_CARD_LAYOUTS: Tuple[str, ...] = (
    "blank", "empty_mbr", "da", "da_small", "other", "fs", "gpt", "unknown",
)
#: Card-not-ready faults a scenario can put in the slot.
USD_FAULTS: Tuple[str, ...] = ("init", "unsupported", "error")
#: One store slot: 8 MiB (handover §4.5b), in 512-byte blocks.
USD_SLOT_BYTES = 8 * 1024 * 1024
_USD_BLOCK = 512
#: Rule (b) makes a 0xDA partition over the last 32 MiB (1 MiB-aligned), so a
#: card needs at least 33 MiB.
USD_MIN_CARD_MB = 33
#: The CLCD row-4 ``ERR <n>`` text a card in ``error`` shows (the store's
#: error code; the value is the scenario's, the SHAPE is the contract's).
USD_ERROR_TEXT = "ERR 13"


@dataclass
class UsdSlot:
    """One slot of the store on the user microSD (the codec's slot
    descriptor + the payload bytes a commit wrote)."""

    rm: str
    rm_id: int
    static_id: int
    clear_len: int = 4
    clear_crc: int = 0
    part_len: int = 4
    part_crc: int = 0
    #: fault injection: this slot's CRCs fail when the store verifies it
    corrupt: bool = False
    clearing: bytes = b""
    partial: bytes = b""


_TFTP_BLOCK = 512  # RFC1350 fixed block size

# --------------------------------------------------------------------------- #
# The Linux harness's boot slots (net-protocol.md v0.14 "Slot images")
# --------------------------------------------------------------------------- #
#: The 6910/TFTP header kind of a stage0 S0LB boot image (net_proto.h
#: MPS3_BIN_KIND_SLOT_IMAGE). Only the slot model answers it; without the model
#: a kind-2 push is refused as an unknown kind, exactly as before.
SLOT_IMAGE_KIND = 2
#: The lock's refusal (net-protocol.md "The lock"), byte for byte.
SLOT_LOCKED_ERR = "slot locked: board claimed (use ssh)"
#: A slot partition on the standard card (STAGE0_CONTRACT §6): 64 MiB; the
#: last 512 B hold the slot record, so an image may be at most 64 MiB - 512 B.
SLOT_BYTES = 64 * 1024 * 1024
_SLOT_RECORD = 512
_S0LB_MAGIC, _S0LB_VERSION, _S0LB_MAX_ENTRIES = 0x424C3053, 2, 8
_S0_DDR_BASE, _S0_DDR_SIZE = 0x80000000, 0x30000000
_SLOT_RUNNING = ("A", "B", "rescue", "none", "unknown")
_SLOT_STATES = ("absent", "empty", "bad", "valid", "io")
_SLOT_ACT_MAX, _SLOT_SEL_MAX = 31, 7      # net_proto.h act[32] / slot_sel[8]

#: The ``slot`` verb's stable ``code`` beside ``err`` (2026-09-26, HM_ANSWERS S3/C2):
#: coordinator.c's k_slot_codes, the same order. ``?`` matches the slot letter; a
#: pattern matches as a PREFIX; an unlisted text has no code (the key is absent).
SLOT_VERB_CODES: Tuple[Tuple[str, str], ...] = (
    ("slot not supported", "not_supported"), ("bad act", "bad_act"), ("bad slot", "bad_slot"),
    ("slot locked:", "locked"), ("no card", "no_card"), ("card io", "card_io"),
    ("no stage0 block", "no_stage0"), ("fabric static_id unknown", "fabric_unknown"),
    ("EBUSY", "busy"), ("nothing staged", "nothing_staged"), ("slot mismatch:", "slot_mismatch"),
    ("slot ? is not a valid image", "not_valid"), ("slot ? not verified", "not_verified"),
    ("slot ? changed since", "changed"), ("slot ? is for ", "wrong_static"),
    ("slot ? runs, but identity lock", "identity_lock"), ("boot-select", "bootsel"),
)
#: ``job.code`` for a failed job's ``job.err`` (slot_linux.c job_code_of, same order).
SLOT_JOB_CODES: Tuple[Tuple[str, str], ...] = (
    ("image for 0x", "wrong_static"), ("no stage0 block", "no_stage0"),
    ("fabric static_id unknown", "fabric_unknown"), ("no card", "no_card"),
    ("card io", "card_io"), ("no free slot", "no_free_slot"), ("slot mismatch:", "slot_mismatch"),
    ("image too large", "too_large"), ("slot ? absent", "slot_absent"), ("slot ? bad:", "slot_bad"),
    ("shorter than a header", "bad_image"), ("no S0LB magic", "bad_image"),
    ("bad version", "bad_image"), ("bad num_entries", "bad_image"),
    ("table truncated", "bad_image"), ("table CRC", "bad_image"), ("region ", "bad_image"),
    ("torn", "torn"), ("aborted", "aborted"), ("card write:", "card_write"),
    ("flush:", "readback"), ("header write:", "readback"), ("read-back:", "readback"),
    ("verifier died", "readback"), ("record ", "record"), ("no slot record", "no_record"),
    ("harnessd restarted", "restarted"), ("result lost", "restarted"),
)


def slot_code(err: str, table: Tuple[Tuple[str, str], ...] = SLOT_VERB_CODES) -> Optional[str]:
    """The stable code for a slot refusal / job error text, or None."""
    for pat, code in table:
        if len(err) >= len(pat) and all(p == "?" or p == c for p, c in zip(pat, err)):
            return code
    return None


def _slot_refusal(err: str) -> Dict[str, Any]:
    """``{"ok":false,"err":..}`` plus ``code`` when the text has one (net_proto.c)."""
    code = slot_code(err)
    return {"ok": False, "err": err, "code": code} if code else {"ok": False, "err": err}


# --------------------------------------------------------------------------- #
# The Linux harness's BOARD identity (net-protocol.md v0.16 "Identity")
# --------------------------------------------------------------------------- #
#: The image defaults (identity_core.h MPS3_ID_DEFAULT_*).
IDENTITY_DEFAULTS: Dict[str, str] = {"label": "MPS3", "ip": "192.168.10.101/24",
                                     "mac": "0200004d5053"}
IDENTITY_FIELDS: Tuple[str, ...] = ("label", "hostname", "ip", "mac")
#: The refusals, byte for byte (identity_linux.c / coordinator.c).
IDENTITY_LOCKED_ERR = "identity locked: board claimed (use ssh)"
IDENTITY_NO_PERSIST_ERR = "identity: no persistent /persist (use the card)"
IDENTITY_NOT_SUPPORTED_ERR = "identity not supported"

_ID_LABEL_RE = re.compile(r"[A-Z0-9-]{1,19}")
_ID_HOST_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?")


def identity_check(field: str, value: str) -> Tuple[Optional[str], Optional[str]]:
    """identity_core.c's rules, written independently: ``(why, None)`` or
    ``(None, canonical)`` -- canonical ip is ``a.b.c.d/nn`` (a bare quad takes
    /24), canonical mac is 12 lowercase hex digits."""
    if field == "label":
        if not value:
            return "empty", None
        if len(value) > 19:
            return "longer than 19 characters", None
        return (None, value) if _ID_LABEL_RE.fullmatch(value) else ("not [A-Z0-9-]", None)
    if field == "hostname":
        if not value:
            return "empty", None
        if len(value) > 63:
            return "longer than 63 characters", None
        ok = all(_ID_HOST_LABEL_RE.fullmatch(p) for p in value.split("."))
        return (None, value) if ok else ("not an RFC 1123 host name", None)
    if field == "ip":
        m = re.fullmatch(r"(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?:/(\d{1,2}))?", value or "")
        if not m:
            return ("empty" if not value else "not a dotted quad"), None
        o = [int(x) for x in m.groups()[:4]]
        pfx = int(m.group(5)) if m.group(5) is not None else 24
        if any(x > 255 for x in o):
            return "not a dotted quad", None
        if not 8 <= pfx <= 30:
            return "prefix not in 8..30", None
        if o[0] in (0, 127) or o[0] >= 224:
            return "not a usable host address", None
        ip = (o[0] << 24) | (o[1] << 16) | (o[2] << 8) | o[3]
        host = 0xFFFFFFFF >> pfx
        if ip & host == 0:
            return "the network address of its prefix", None
        if ip & host == host:
            return "the broadcast address of its prefix", None
        return None, "%d.%d.%d.%d/%d" % (*o, pfx)
    if field == "mac":
        v = value or ""
        if re.fullmatch(r"[0-9A-Fa-f]{12}", v):
            h = v
        elif re.fullmatch(r"[0-9A-Fa-f]{2}([:-])[0-9A-Fa-f]{2}(\1[0-9A-Fa-f]{2}){4}", v):
            h = re.sub(r"[:-]", "", v)
        else:
            return "not 12 hex digits", None
        b = bytes.fromhex(h)
        if b[0] & 1:
            return "multicast, not unicast", None
        if not any(b):
            return "all zero", None
        return None, h.lower()
    return "unknown key", None


class _IdentityModel:
    """``identity`` / ``identity_set`` as mps3-harnessd answers them
    (identity_linux.c over identity_core.c). Config keys (all optional):
    ``running`` {label, hostname, ip, mac, source{...}} -- this boot's (default: the
    image defaults, hostname from the label); ``stage0`` {label, ip, mac} with None
    for an absent field, or None (no valid block); ``override`` {subset} or None;
    ``persist`` (default True); ``trusted_peer`` (the ONE local peer, like harnessd's
    --mock-trusted-peer; default: 127/8)."""

    def __init__(self, shell: "FakeShell", cfg: Optional[Dict[str, Any]]) -> None:
        cfg = dict(cfg or {})
        unknown = set(cfg) - {"running", "stage0", "override", "persist", "trusted_peer"}
        if unknown:
            raise ValueError(f"unknown identity= keys {sorted(unknown)}")
        self.shell = shell
        self.stage0: Optional[Dict[str, Optional[str]]] = cfg.get("stage0")
        self.override: Optional[Dict[str, str]] = cfg.get("override")
        self.persist = bool(cfg.get("persist", True))
        self.trusted_peer: Optional[str] = cfg.get("trusted_peer")
        self.running = cfg.get("running") or self.resolve(None, None)
        self.sets: List[Dict[str, Any]] = []

    @staticmethod
    def resolve(ovr: Optional[Dict[str, str]],
                s0: Optional[Dict[str, Optional[str]]]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        src: Dict[str, str] = {}
        for f in ("label", "ip", "mac"):
            if ovr and ovr.get(f):
                out[f], src[f] = ovr[f], "override"
            elif s0 and s0.get(f):
                out[f], src[f] = s0[f], "stage0"
            else:
                out[f], src[f] = IDENTITY_DEFAULTS[f], "default"
        if ovr and ovr.get("hostname"):
            host, hsrc = ovr["hostname"], "override"
        elif identity_check("hostname", out["label"].lower())[0] is None:
            host, hsrc = out["label"].lower(), "label"
        else:
            host, hsrc = "mps3", "default"
        return {"label": out["label"], "hostname": host, "ip": out["ip"], "mac": out["mac"],
                "source": {"label": src["label"], "hostname": hsrc, "ip": src["ip"],
                           "mac": src["mac"]}}

    def _pending(self) -> Optional[Dict[str, str]]:
        nxt = self.resolve(self.override if self.persist else None, self.stage0)
        diff = {f: nxt[f] for f in IDENTITY_FIELDS if nxt[f] != self.running[f]}
        return diff or None

    def _peer_local(self, peer: Optional[str]) -> bool:
        if peer is None:
            return False
        if self.trusted_peer is not None:
            return peer == self.trusted_peer
        return peer.startswith("127.")

    def status(self) -> Dict[str, Any]:
        r = self.running
        ovr = None
        if self.persist and self.override is not None:
            ovr = {f: self.override[f] for f in IDENTITY_FIELDS if f in self.override}
        return {"ok": True, "op": "identity", "label": r["label"], "hostname": r["hostname"],
                "ip": r["ip"], "mac": r["mac"], "source": dict(r["source"]),
                "stage0": (None if self.stage0 is None else
                           {f: self.stage0.get(f) for f in ("label", "ip", "mac")}),
                "override": ovr, "pending": self._pending(), "persist": self.persist}

    def set(self, request: Dict[str, Any], peer: Optional[str]) -> Dict[str, Any]:
        def refuse(err: str, code: str) -> Dict[str, Any]:
            return {"ok": False, "err": err, "code": code}
        if self.shell.ssh_claimed and not self._peer_local(peer):
            return refuse(IDENTITY_LOCKED_ERR, "locked")
        if not self.persist:
            return refuse(IDENTITY_NO_PERSIST_ERR, "no_persist")
        clear = request.get("clear", False)
        if not isinstance(clear, bool):
            return refuse("invalid clear: not a bool", "invalid")
        edits: Dict[str, str] = {}
        for f in IDENTITY_FIELDS:
            if f in request:
                v = request[f]
                if not isinstance(v, str):
                    return refuse(f"invalid {f}: not a string", "invalid")
                if len(v) > 79:
                    return refuse(f"invalid {f}: too long", "invalid")
                edits[f] = v
        if clear and edits:
            return refuse("invalid request: clear takes no other field", "invalid")
        if not clear and not edits:
            return refuse("invalid request: nothing to set (label/hostname/ip/mac)", "invalid")
        new = dict(self.override or {})
        for f, v in edits.items():
            if v == "":
                new.pop(f, None)
                continue
            why, canon = identity_check(f, v)
            if why:
                return refuse(f"invalid {f}: {why}", "invalid")
            new[f] = canon
        self.override = None if (clear or not new) else new
        self.sets.append({"clear": clear, **edits})
        return {"ok": True, "op": "identity_set", "persisted": True,
                "pending": self._pending(), "applies": "reboot"}


# --------------------------------------------------------------------------- #
# The Linux harness's presence + front panel (net-protocol.md v0.17 "Presence and
# the panel"; the Harness Manager's R1/R2). Written independently of
# presence_core.c / panel_linux.c: the rules, not the code.
# --------------------------------------------------------------------------- #
PANEL_LOCKED_ERR = "panel locked: board claimed (use ssh)"
_PRES_ROLES: Tuple[str, ...] = ("holder", "owner", "watch")
_PRES_CAPS = {"sid": 8, "who": 20, "name": 16, "app": 12, "user": 12, "job": 8}
#: The glyph bytes of the aligned theme (clcd.h CLCD_GLYPH_*), as the reply's
#: \u0080-\u0086 decode them.
_GLYPH_HELD, _GLYPH_WARN, _GLYPH_USER = "\x83", "\x82", "\x84"
#: clcd_palette.h's role order; a frame's role CODE is chr(ord('a') + index).
_ROLE_INDEX = {n: i for i, n in enumerate((
    "text", "label", "value", "rule", "chrome", "title", "title-held", "title-warn", "ok",
    "warn", "err", "busy", "unk", "held", "bar", "track", "banner-err", "banner-warn",
    "banner-ok", "banner-busy", "banner-held"))}
#: A healthy Linux status page in today's words (the base the fake's frame starts
#: from; row 0's right side, row 11 and rows 10-12 are drawn from the table).
PANEL_STATUS_ROWS: Tuple[str, ...] = (
    "MPS3                nanoSoC harness     ",
    "-" * 40,
    "DUT : nanosoc              v1.0         ",
    "SWAP: LOADED VERIFIED     #001  last OK ",
    "SID : 0x14E1A2D8  USD : none            ",
    "NET : 192.168.10.101  UP 100/FD         ",
    "UP  : 000:00:01:48                      ",
    "DUT : RST-REL  CLK-ALIVE   MMCM-LOCK    ",
    "ICAP: 1835072 B  rxdrop 0 txerr 0       ",
    "CFG : 1x Cortex-M0  no ETH  1x UART     ",
    " " * 40,
    " " * 40,
    "SYS : linux  ssh unclaimed              ",
    "-" * 40,
    "MAC 02:00:00:4D:50:53             hb \\  ",
)
#: The same page in the ALIGNED theme's words (harnessd's default since lane
#: PANEL-FINISH; clcd.c's status page with clcd_theme_aligned, as clcd_preview's
#: aligned-status draws it): the base when no ``rows`` are given and the theme is
#: "aligned". Its role codes stay "a" (the fake models the table-driven rows'
#: roles, not every field's).
PANEL_STATUS_ROWS_ALIGNED: Tuple[str, ...] = (
    " MPS3".ljust(40),
    "-" * 40,
    "design nanosoc v1.0           \x80verified ",
    "prog   #001 loaded              last ok ",
    "shell  0x14E1A2D8 card none".ljust(40),
    "net    192.168.10.101         up 100/FD ",
    "up     000:00:01:48".ljust(40),
    "dut    rst-rel  clk-alive  mmcm-lock    ",
    "icap   1835072 B  rxdrop 0  txerr 0     ",
    "cfg    1x Cortex-M0  no ETH  1x UART    ",
    " " * 40,
    " " * 40,
    "sys    linux ssh unclaimed".ljust(40),
    "-" * 40,
    " mac 02:00:00:4D:50:53             hb \\ ",
)


def presence_clip(value: Any, limit: int, cut_at: str = "") -> str:
    """presence_core.c's field rule: every character outside 0x20-0x7E becomes
    '?', the value ends at ``cut_at`` (a principal's user part), then clipped."""
    s = "".join(ch if " " <= ch <= "~" else "?" for ch in str(value))
    if cut_at and cut_at in s:
        s = s.split(cut_at, 1)[0]
    return s[:limit]


def _pres_dur(s: int) -> str:
    s = max(0, int(s))
    return (f"{s // 3600}h{(s % 3600) // 60:02d}m" if s >= 3600 else
            f"{s // 60}m" if s >= 60 else f"{s}s")


class _PanelModel:
    """``hello`` / ``panel`` as mps3-harnessd answers them. Config keys (all
    optional): ``clock`` (a monotonic seconds callable, for TTL tests), ``theme``
    ("aligned", harnessd's default: HM's words, glyphs in the badge and hm row, |
    "today": today's words, ASCII), ``touch_present`` / ``touch_cal`` (default True),
    ``rows`` (the 15 base rows; default the theme's status page), ``label`` (row 0's
    name)."""

    COMMIT_S = 2.0

    def __init__(self, shell: "FakeShell", cfg: Optional[Dict[str, Any]]) -> None:
        cfg = dict(cfg or {})
        unknown = set(cfg) - {"clock", "theme", "touch_present", "touch_cal", "rows", "label"}
        if unknown:
            raise ValueError(f"unknown panel= keys {sorted(unknown)}")
        self.shell = shell
        self.clock = cfg.get("clock") or time.monotonic
        self.theme = cfg.get("theme", "aligned")        # harnessd's default
        if self.theme not in ("aligned", "today"):
            raise ValueError("panel theme must be 'aligned' or 'today'")
        self.touch_present = bool(cfg.get("touch_present", True))
        self.touch_cal = bool(cfg.get("touch_cal", True))
        self.base_rows = list(cfg.get("rows") or (PANEL_STATUS_ROWS_ALIGNED if self.theme == "aligned"
                                                  else PANEL_STATUS_ROWS))
        if len(self.base_rows) != 15 or any(len(r) != 40 for r in self.base_rows):
            raise ValueError("panel rows: 15 strings of 40 characters")
        self.label = str(cfg.get("label", "MPS3"))
        self.page = "status"
        #: sid -> {"facts", "seen", "facts_at", "shown", "shown_at", "commit"}
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.events: List[Tuple[int, str, float]] = []      # (seq, on, at)
        self.seq = 0
        self.hellos: List[Dict[str, Any]] = []
        self.pages: List[str] = []

    # -- the table ------------------------------------------------------------ #

    @staticmethod
    def parse(request: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, Any]]:
        def bad(msg: str) -> Tuple[Optional[str], Dict[str, Any]]:
            return msg, {}

        def is_int(v: Any) -> bool:
            return isinstance(v, int) and not isinstance(v, bool)

        def clamp(v: int, lo: int, hi: int) -> int:
            return max(lo, min(hi, v))

        v = request.get("v", 1)
        if not is_int(v) or v < 1:
            return bad("invalid v: an integer >= 1")
        sid = request.get("sid")
        if not isinstance(sid, str) or not presence_clip(sid, 8):
            return bad("invalid sid: 1-8 printable characters")
        who = request.get("who")
        if not isinstance(who, str):
            return bad("invalid who: a string (user@host)")
        for key in ("app", "name"):
            if key in request and not isinstance(request[key], str):
                return bad(f"invalid {key}: a string")
        role = request.get("role", "watch")
        if role not in _PRES_ROLES:
            return bad("invalid role: holder, owner or watch")
        ttl = request.get("ttl", 90)
        if not is_int(ttl):
            return bad("invalid ttl: an integer (30-300 s)")
        f: Dict[str, Any] = {"sid": presence_clip(sid, 8), "who": presence_clip(who, 20),
                             "app": presence_clip(request.get("app", ""), 12),
                             "name": presence_clip(request.get("name", ""), 16),
                             "role": role, "ttl": clamp(ttl, 30, 300), "lease": None, "job": None}
        lease = request.get("lease")
        if lease is not None or "lease" in request:
            if not isinstance(lease, dict) or any(isinstance(x, (dict, list))
                                                  for x in lease.values()):
                return bad("invalid lease: a flat object")
            for key in ("by", "req"):
                if key in lease and not isinstance(lease[key], str):
                    return bad(f"invalid lease.{key}: a string")
            if any(k in lease and not is_int(lease[k]) for k in ("left", "q", "rl")):
                return bad("invalid lease: left, q and rl are integers")
            f["lease"] = {"by": presence_clip(lease.get("by", ""), 12, "@"),
                          "req": presence_clip(lease.get("req", ""), 12, "@"),
                          "left": clamp(lease["left"], 0, 86400) if "left" in lease else None,
                          "q": clamp(lease.get("q", 0), 0, 99),
                          "rl": clamp(lease["rl"], 0, 600) if "rl" in lease else None}
        job = request.get("job")
        if job is not None or "job" in request:
            if not isinstance(job, dict) or any(isinstance(x, (dict, list)) for x in job.values()):
                return bad("invalid job: a flat object")
            if ("k" in job and not isinstance(job["k"], str)) or ("p" in job and not is_int(job["p"])):
                return bad("invalid job: {k: string, p: integer}")
            f["job"] = {"k": presence_clip(job.get("k", ""), 8), "p": clamp(job.get("p", 0), 0, 100)}
        return None, f

    def live(self, now: float, *, shown: bool = False) -> List[Dict[str, Any]]:
        alive = [s for s in self.sessions.values() if now - s["seen"] <= s["facts"]["ttl"]]
        key = "shown" if shown else "facts"
        return sorted(alive, key=lambda s: (_PRES_ROLES.index(s[key]["role"]), -s["seen"]))

    def commit(self, now: float) -> None:
        for s in self.sessions.values():
            if s["shown"] is not s["facts"] and now - s["commit"] >= self.COMMIT_S:
                s["shown"], s["shown_at"], s["commit"] = s["facts"], s["facts_at"], now

    def hello(self, f: Dict[str, Any]) -> int:
        now = self.clock()
        s = self.sessions.get(f["sid"])
        if s is None:
            if len(self.sessions) >= 4:
                del self.sessions[min(self.sessions.values(), key=lambda x: x["seen"])["sid"]]
            s = self.sessions[f["sid"]] = {"sid": f["sid"], "commit": now, "shown": f,
                                           "shown_at": now}
        elif now - s["commit"] >= self.COMMIT_S:
            s["shown"], s["shown_at"], s["commit"] = f, now, now
        s["facts"], s["facts_at"], s["seen"] = f, now, now
        return len(self.live(now))

    # -- what the panel draws ------------------------------------------------- #

    def _lease(self, now: float) -> Optional[Tuple[Dict[str, Any], float]]:
        best = None
        for s in self.live(now, shown=True):
            if s["shown"]["lease"] is None:
                continue
            k = (s["shown"]["role"] != "holder", -s["shown_at"])
            if best is None or k < best[0]:
                best = (k, s)
        return None if best is None else (best[1]["shown"]["lease"], best[1]["shown_at"])

    def badge(self, now: float) -> Optional[Tuple[str, str]]:
        found = self._lease(now)
        if found is None:
            return None
        lease, at = found
        g = self.theme == "aligned"
        if not lease["by"]:
            return ((_GLYPH_WARN + " " if g else "") + "not leased")[:24], "title-warn"
        text = (_GLYPH_HELD + " " if g else "") + lease["by"][:10]
        left = None
        if lease["left"] is not None:
            left = max(0, lease["left"] - int(now - at))
            text += " " + _pres_dur(left)
        if lease["q"]:
            for form in (", {} waiting", ", {} wait", " +{}"):
                tail = form.format(lease["q"])
                if len(text + tail) <= 24 or form == " +{}":
                    text += tail
                    break
        return text[:24], ("title-warn" if left is not None and left < 300 else "title-held")

    def hm_row(self, now: float) -> Tuple[str, str]:
        """Row 11 and its role codes."""
        row, roles = [" "] * 40, ["text"] * 40          # clcd_line_clear
        def put(col: int, text: str, role: str) -> None:
            for i, ch in enumerate(text[:max(0, 40 - col)]):
                row[col + i], roles[col + i] = ch, role
        put(0, "hm", "label")
        live = self.live(now, shown=True)
        if not live:
            if self.sessions:
                last = max(self.sessions.values(), key=lambda x: x["seen"])
                who = (last["shown"]["who"] or "?")[:16]
                put(7, f"{who}  left {_pres_dur(int(now - last['seen']))} ago", "label")
            else:
                put(7, "none connected", "label")
        else:
            who = (_GLYPH_USER if self.theme == "aligned" else "") + \
                (live[0]["shown"]["who"] or "?")[:16]
            put(7, who, "value")
            if len(live) > 1:
                put(7 + len(who) + 2, f"+{len(live) - 1} watching", "label")
        return "".join(row), "".join(chr(97 + _ROLE_INDEX[r]) for r in roles)

    def request(self, now: float) -> Optional[List[str]]:
        found = self._lease(now)
        if found is None or not found[0]["req"]:
            return None
        lease, at = found
        tm = ""
        if lease["rl"] is not None:
            rl = max(0, lease["rl"] - int(now - at))
            if rl == 0:
                return None
            tm = f"{rl // 60}:{rl % 60:02d}"
        lines = [f"{lease['req']} wants this board"]
        if lease["by"]:
            lines += [f"held by {lease['by']}" + (f" {tm} to answer" if tm else ""),
                      f"tap: tell {lease['by']} you are here"]
        else:
            lines += [f"{tm} to answer" if tm else "", "tap: say you are here"]
        return [ln[:40] for ln in lines]

    def frame(self, now: float) -> Tuple[List[str], str]:
        self.commit(now)
        rows = [r for r in self.base_rows]
        codes = ["a" * 40 for _ in rows]
        b = self.badge(now)
        if b is not None:
            text, role = b
            start = 40 - 1 - len(text)
            rows[0] = rows[0][:start] + text + rows[0][start + len(text):]
            codes[0] = codes[0][:start] + chr(97 + _ROLE_INDEX[role]) * len(text) + codes[0][start + len(text):]
        if self.page == "status":
            rows[11], codes[11] = self.hm_row(now)
        banner = self.banner(now)
        if banner is not None:
            lines, role = banner
            for i, ln in enumerate(lines):
                pad = (40 - len(ln)) // 2
                rows[10 + i] = (" " * pad + ln).ljust(40)
                codes[10 + i] = chr(97 + _ROLE_INDEX[role]) * 40
        return rows, "".join(codes)

    def banner(self, now: float) -> Optional[Tuple[List[str], str]]:
        """Rows 10-12 below the fault banners: IDENTIFY (a locate, while the
        harness owns the panel), then the lease request."""
        sh = self.shell
        if sh.display_owner != "harness":
            return None
        if sh.locate_until is not None and time.monotonic() < sh.locate_until:
            who = sh.locates[-1][1] if sh.locates else ""
            return ["", f"IDENTIFY: {who}"[:40], ""], "banner-busy"
        req = self.request(now)
        return (req, "banner-held") if req is not None else None

    def tap(self, on: str) -> int:
        """A tap on the glass the harness's hit test turned into an event."""
        self.seq += 1
        self.events.append((self.seq, on, time.monotonic()))
        del self.events[:-8]
        return self.seq

    # -- the replies ------------------------------------------------------------ #

    def _facts(self, now: float) -> Dict[str, Any]:
        sh = self.shell
        banner = self.banner(now)
        text = ""
        if sh.display_owner == "dut":
            text = "DUT HAS THE DISPLAY"
        elif banner is not None:
            text = next((ln.strip(" " + _GLYPH_HELD + _GLYPH_WARN + _GLYPH_USER)
                         for ln in banner[0] if ln.strip()), "")
        return {"page": self.page, "owner": sh.display_owner,
                "pending": sh.display_owner != sh.display_target, "banner": text,
                "card": sh.usd_status()["text"]}

    def _events(self) -> List[Dict[str, Any]]:
        t = time.monotonic()
        return [{"seq": seq, "k": "tap", "on": on, "ms_ago": int(max(0.0, t - at) * 1000)}
                for seq, on, at in self.events]

    def op_hello(self, request: Dict[str, Any]) -> Dict[str, Any]:
        why, f = self.parse(request)
        if why:
            return {"ok": False, "err": why, "code": "invalid"}
        self.hellos.append(dict(request))
        live = self.hello(f)
        now = self.clock()
        return {"ok": True, "op": "hello", "sessions": live,
                "panel": {**self._facts(now), "seq": self.seq}, "events": self._events()}

    def op_panel(self, request: Dict[str, Any], peer: Optional[str]) -> Dict[str, Any]:
        def refuse(err: str, code: str) -> Dict[str, Any]:
            return {"ok": False, "err": err, "code": code}
        sh = self.shell
        now = self.clock()
        frame = request.get("frame", False)
        has_frame = "frame" in request and frame is not False
        if "page" in request:
            if sh.ssh_claimed and not sh._panel_peer_local(peer):
                return refuse(PANEL_LOCKED_ERR, "locked")
            if has_frame:
                return refuse("invalid request: page takes no frame", "invalid")
            page = request["page"]
            if page not in ("status", "apps"):
                return refuse("invalid page: status or apps", "invalid")
            if sh.display_owner != "harness" or sh.display_target != "harness":
                return refuse("dut owns the panel", "held")
            self.page = page
            self.pages.append(page)
            return {"ok": True, "op": "panel", "page": page}
        if has_frame:
            if frame not in ("a", "b"):
                return refuse('invalid frame: "a" (rows 0-7) then "b" (rows 8-14)', "invalid")
            rows, codes = self.frame(now)
            r0, r1 = (0, 8) if frame == "a" else (8, 15)
            return {"ok": True, "op": "panel", "frame": frame, "theme": self.theme,
                    "rows": rows[r0:r1], "roles": codes[r0 * 40:r1 * 40]}
        return {"ok": True, "op": "panel", **self._facts(now),
                "touch": {"present": self.touch_present, "cal": self.touch_cal},
                "sessions": [{"sid": s["facts"]["sid"], "who": s["facts"]["who"],
                              "role": s["facts"]["role"], "age_s": int(now - s["seen"])}
                             for s in self.live(now)],
                "seq": self.seq, "events": self._events()}


def _s0lb_table(first: bytes, limit: int) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """stage0's table rules (s0_load / slot_linux.c table_check), written
    independently: magic, version, entry count, the TABLE CRC (header with its
    CRC field zeroed + every entry), every region inside the DDR window and
    inside ``limit`` bytes. ``(None, table)`` or ``(reason, None)`` in the
    contract's words ("job.err")."""
    if len(first) < 32:
        return "shorter than a header", None
    magic, ver, n, _pc, _a0, _a1, _fl, hcrc = struct.unpack_from("<8I", first, 0)
    if magic != _S0LB_MAGIC:
        return "no S0LB magic", None
    if ver != _S0LB_VERSION:
        return "bad version", None
    if n == 0 or n > _S0LB_MAX_ENTRIES:
        return "bad num_entries", None
    if len(first) < 32 + 16 * n:
        return "table truncated", None
    if zlib.crc32(first[:28] + b"\0\0\0\0" + first[32:32 + 16 * n]) & 0xFFFFFFFF != hcrc:
        return "table CRC", None
    extent, entries = 32 + 16 * n, []
    for i in range(n):
        src, dst, ln, rcrc = struct.unpack_from("<4I", first, 32 + 16 * i)
        if not (ln <= _S0_DDR_SIZE and dst >= _S0_DDR_BASE
                and dst - _S0_DDR_BASE <= _S0_DDR_SIZE - ln):
            return "region outside the DDR window", None
        if src > limit or ln > limit - src:
            return "region past the end", None
        extent = max(extent, src + ln)
        entries.append((src, ln, rcrc))
    return None, {"hdr_crc": hcrc, "extent": extent, "entries": entries}


class _SlotModel:
    """``mps3-harnessd``'s v0.14 slot provider (slot_linux.c over slot_card.c),
    as a scenario: the card's two stage0 slots, the boot-select pick, the ONE
    card job, and THE LOCK. Every refusal is the contract's text, checked in the
    C's order (mps3_slot_op / mps3_cfg_slot_sink). The card is not simulated
    byte by byte: a slot is its state + table CRC + extent (+ the slot record's
    static_id); a push checks the image with stage0's table rules and its
    region CRCs, and a failure past the first sector leaves the slot ``empty``
    (the C zeroes the old first sector before the body goes down).

    Scenario keys (``FakeShell(slots={...})``): ``card`` (True / False = no card /
    ``"io"``), ``running`` (``A B rescue none unknown``), ``default``, ``seq``,
    ``a`` / ``b`` (``{"state", "hdr_crc", "len", "sid"?, "err"?, "corrupt"?,
    "overlap"?}`` -- ``overlap`` marks slot_card's overlap-guard BAD, which a push
    is refused for), ``boot_crc`` (stage0's image_hdr_crc: the running slot is
    ``verified: boot`` when it matches), ``fabric_sid``, ``slot_bytes``,
    ``trusted_peer`` (the ONE peer treated as local, like harnessd's
    --mock-trusted-peer; default 127.0.0.0/8), ``verify_polls`` (requests a
    read-back stays ``verifying``), ``identity_lock`` (a reason text),
    ``confirmed`` (stage0's att_confirm holds the magic: ``slot status``'s
    ``confirmed``; default True, a boot harnessd has confirmed). ``slot status``
    also reports ``claimed`` = the shell's ``ssh_claimed`` (HM_ANSWERS S2/S5).

    A ``reboot`` BOOTS THE DEFAULT (HM_ANSWERS S9, :meth:`reboot`): stage0 picks
    the default if it is valid, else the other slot, else the rescue path; the
    per-boot state (``staged``, the read-back proofs, ``job``) is gone. The
    optional ``unhealthy`` key (a set of slot names, e.g. ``{"B"}``) models a
    FALLBACK: such a slot never confirms, so stage0 falls back to the other one and
    the default does not change. Each confirmed healthy boot stamps its slot's
    record (harnessd_slot_stamp_booted, HM_ANSWERS S1), so after a reboot the
    previously running slot can be ``verify``'d."""

    def __init__(self, shell: "FakeShell", cfg: Dict[str, Any]) -> None:
        unknown = set(cfg) - {"card", "running", "default", "seq", "a", "b", "boot_crc",
                              "fabric_sid", "slot_bytes", "trusted_peer", "verify_polls",
                              "identity_lock", "confirmed", "unhealthy"}
        if unknown:
            raise ValueError(f"unknown slots= keys {sorted(unknown)}")
        self.shell = shell
        self.card = cfg.get("card", True)                 # True / False / "io"
        self.running = cfg.get("running", "A")
        if self.running not in _SLOT_RUNNING:
            raise ValueError(f"slots running must be one of {_SLOT_RUNNING}")
        self.deflt = cfg.get("default", "A")
        if self.deflt not in ("A", "B"):
            raise ValueError("slots default must be 'A' or 'B'")
        self.seq = int(cfg.get("seq", 1))
        self.fabric_sid = int(cfg.get("fabric_sid", shell.static_id)) & 0xFFFFFFFF
        self.slot_bytes = int(cfg.get("slot_bytes", SLOT_BYTES))
        self.trusted_peer: Optional[str] = cfg.get("trusted_peer")
        self.verify_polls = int(cfg.get("verify_polls", 0))
        self.identity_lock: Optional[str] = cfg.get("identity_lock")
        self.confirmed = bool(cfg.get("confirmed", True))
        self.unhealthy = set(cfg.get("unhealthy", ()))
        if not self.unhealthy <= {"A", "B"}:
            raise ValueError("slots unhealthy must name slots 'A' / 'B'")
        self.slot: Dict[str, Dict[str, Any]] = {}
        for key, default in (("a", {"state": "valid", "hdr_crc": 0x3E5E9C2C, "len": 24354312}),
                             ("b", {"state": "empty"})):
            sl = dict(cfg.get(key, default))
            if sl.get("state") not in _SLOT_STATES:
                raise ValueError(f"slots {key} state must be one of {_SLOT_STATES}")
            self.slot[key.upper()] = sl
        run = self.slot.get(self.running)
        self.boot_crc = int(cfg.get("boot_crc", run.get("hdr_crc", 0) if run else 0))
        self.staged: Optional[str] = None
        self.vcrc = {"A": 0, "B": 0}
        self.vsid = {"A": 0, "B": 0}
        self.job = {"act": "none", "slot": None, "state": "idle", "got": 0, "len": 0, "err": ""}
        self._polls_left = 0
        self._pending: Optional[Dict[str, Any]] = None

    # -- the lock ------------------------------------------------------------ #
    def peer_local(self, peer: Optional[str]) -> bool:
        if peer is None:
            return False                                  # an unknown peer is never local
        if self.trusted_peer is not None:
            return peer == self.trusted_peer              # harnessd --mock-trusted-peer
        return peer.startswith("127.")

    def locked(self, peer: Optional[str]) -> bool:
        return bool(self.shell.ssh_claimed) and not self.peer_local(peer)

    # -- a reboot (HM_ANSWERS S9) ----------------------------------------------- #
    def _stamp_running(self) -> None:
        """harnessd_slot_stamp_booted() for THIS boot: once confirmed, the running
        slot's record binds its image to the fabric -- under the C's gates."""
        sl = self.slot.get(self.running)
        if (sl is None or not self.confirmed or self.identity_lock or self.fabric_sid == 0
                or sl["state"] != "valid" or sl.get("hdr_crc") != self.boot_crc
                or sl["len"] > self.slot_bytes - _SLOT_RECORD):
            return
        sl["sid"] = self.fabric_sid

    def reboot(self) -> None:
        """What the WDOG reset + stage0 do to the slots: the boot that ends has
        stamped its slot (it was confirmed), stage0 boots the default when it is
        valid and healthy, else the other valid slot (a FALLBACK: the default is
        not rewritten -- STAGE0 §1), else the rescue path; the per-boot state is
        gone (``/run`` is tmpfs) and the new boot confirms and stamps."""
        self._stamp_running()
        if self.card is True:
            order = (self.deflt, _usd_flip(self.deflt))
            ok = [n for n in order if self.slot[n]["state"] == "valid" and n not in self.unhealthy]
            self.running = ok[0] if ok else "rescue"
        else:
            self.running = "rescue"                   # no card: stage0's rescue path
        run = self.slot.get(self.running)
        self.boot_crc = int(run.get("hdr_crc", 0)) if run else 0
        self.staged = None
        self.vcrc = {"A": 0, "B": 0}
        self.vsid = {"A": 0, "B": 0}
        self.job = {"act": "none", "slot": None, "state": "idle", "got": 0, "len": 0, "err": ""}
        self._pending = None
        self._polls_left = 0
        self.confirmed = True
        self._stamp_running()

    # -- the card as stage0 sees it ------------------------------------------ #
    def busy(self) -> bool:
        return self.job["state"] in ("writing", "verifying")

    def target(self) -> Optional[str]:
        if self.running == "unknown":
            return None
        if self.running in ("A", "B"):
            return _usd_flip(self.running) if self.deflt == self.running else None
        return _usd_flip(self.deflt)                      # rescue / a JTAG boot

    def verified(self, name: str) -> str:
        sl = self.slot[name]
        if sl["state"] != "valid":
            return "no"
        if self.vcrc[name] and self.vcrc[name] == sl["hdr_crc"]:
            return "readback"
        if self.running == name and self.boot_crc == sl["hdr_crc"]:
            return "boot"
        return "no"

    def status(self) -> Dict[str, Any]:
        rep: Dict[str, Any] = {"ok": True, "card": self.card is True,
                               "fabric_sid": _hex32(self.fabric_sid), "running": self.running}
        if self.card is True:
            rep.update({"default": self.deflt, "seq": self.seq, "target": self.target()})
        rep["staged"] = self.staged
        if self.card is True:
            for name in ("A", "B"):
                sl = self.slot[name]
                one: Dict[str, Any] = {"state": sl["state"]}
                if sl["state"] in ("bad", "io"):
                    one["err"] = sl.get("err", "")
                if sl["state"] == "valid":
                    one["hdr_crc"] = _hex32(sl["hdr_crc"])
                    one["len"] = sl["len"]
                    if sl.get("sid") is not None:
                        one["sid"] = _hex32(sl["sid"])
                one["verified"] = self.verified(name)
                rep[name.lower()] = one
        rep["job"] = dict(self.job)
        if self.job["state"] == "failed":                 # job.code (HM_ANSWERS S3)
            code = slot_code(self.job["err"], SLOT_JOB_CODES)
            if code:
                rep["job"]["code"] = code
        rep["claimed"] = bool(self.shell.ssh_claimed)     # APPENDED (HM_ANSWERS S2)
        rep["confirmed"] = self.confirmed                  # APPENDED (HM_ANSWERS S5)
        return rep

    # -- the one card job ------------------------------------------------------ #
    def _fail(self, err: str) -> None:
        self.job.update(state="failed", err=err)

    def poll(self) -> None:
        """harnessd_slot_poll(): a finished read-back lands on the NEXT request."""
        if self.job["state"] != "verifying" or self._pending is None:
            return
        if self._polls_left > 0:
            self._polls_left -= 1
            return
        done, self._pending = self._pending, None
        name = done["slot"]
        if done.get("err"):
            self._fail(done["err"])
            return
        if done["act"] == "push":
            self.slot[name] = {"state": "valid", "hdr_crc": done["hdr_crc"], "len": done["extent"],
                               "sid": done["sid"]}
            self.staged = name
        self.vcrc[name], self.vsid[name] = done["hdr_crc"], done["sid"]
        self.job.update(state="ok", err="")

    def _verifying(self, **result: Any) -> None:
        self.job["state"] = "verifying"
        self._pending = result
        self._polls_left = self.verify_polls

    # -- the verb (mps3_slot_op) ------------------------------------------------- #
    def op(self, act: str, sel: Optional[str], peer: Optional[str]) -> Tuple[Optional[str], Dict[str, Any]]:
        self.poll()
        if act in ("commit", "rollback") and self.locked(peer):
            return SLOT_LOCKED_ERR, {}
        if act == "status":
            return ("card io", {}) if self.card == "io" else (None, self.status())
        if self.card is not True:
            return ("card io" if self.card == "io" else "no card"), {}
        if self.running == "unknown":
            return "no stage0 block", {}
        if self.fabric_sid == 0:
            return "fabric static_id unknown", {}
        if self.busy():
            return "EBUSY", {}
        if act == "verify":
            dest = sel or _usd_flip(self.deflt)
            sl = self.slot[dest]
            if sl["state"] != "valid":
                return f"slot {dest} is not a valid image", {}
            self.vcrc[dest] = self.vsid[dest] = 0
            self.job = {"act": "verify", "slot": dest, "state": "verifying", "got": 0,
                        "len": sl["len"], "err": ""}
            if sl.get("corrupt"):
                err = "read-back: region 0 CRC"
            elif sl.get("sid") is None:
                err = "no slot record: static_id unknown"
            elif sl["sid"] != self.fabric_sid:
                err = f"image for {_hex32(sl['sid'])} != fabric {_hex32(self.fabric_sid)}"
            else:
                err = ""
            self._verifying(act="verify", slot=dest, err=err, hdr_crc=sl["hdr_crc"],
                            sid=sl.get("sid") or 0)
            return None, self.status()
        if act == "commit":
            if self.staged is None:
                return "nothing staged: push an image first", {}
            dest = self.staged
        else:
            dest = _usd_flip(self.deflt)
        if sel is not None and sel != dest:
            return f"slot mismatch: {act} would pick {dest}", {}
        why = self._flip_ok(dest)
        if why:
            return why, {}
        if self.deflt != dest:
            self.deflt, self.seq = dest, self.seq + 1     # the boot-select writer
        return None, self.status()

    def _flip_ok(self, dest: str) -> Optional[str]:
        sl, how = self.slot[dest], self.verified(dest)
        if sl["state"] != "valid":
            return f"slot {dest} is not a valid image"
        if how == "readback":
            if self.vsid[dest] != self.fabric_sid:
                return f"slot {dest} is for {_hex32(self.vsid[dest])} != fabric {_hex32(self.fabric_sid)}"
            return None
        if how == "boot":
            if self.identity_lock:
                return f"slot {dest} runs, but identity lock: {self.identity_lock[:30]}"
            return None
        if self.vcrc[dest]:
            return f"slot {dest} changed since it was verified"
        return f"slot {dest} not verified"

    # -- the kind-2 push (mps3_cfg_slot_sink + the sink) --------------------------- #
    def push_begin(self, header: bytes, peer: Optional[str]) -> Tuple[str, Optional[str], int]:
        """``(verdict, slot, total)``: ``"unread"`` = closed before the provider
        (a bad header, a running job, THE LOCK) -- no state changes at all;
        ``"failed"`` = the provider refused and ``job`` says why; ``"go"``."""
        _m, ver, _k, rm_slot, sid, _rm, words, _crc = struct.unpack(">4sHBBIIII", header[:24])
        total = words * 4
        if ver != 1 or words == 0 or total > 64 * 1024 * 1024:
            return "unread", None, total
        self.poll()                                       # the service loop reaps a done job
        if self.busy() or self.locked(peer):
            return "unread", None, total
        tgt = self.target()
        why = None
        if self.running == "unknown":
            why = "no stage0 block"
        elif self.fabric_sid == 0:
            why = "fabric static_id unknown"
        elif sid != self.fabric_sid:
            why = f"image for {_hex32(sid)} != fabric {_hex32(self.fabric_sid)}"
        elif self.card is not True:
            why = "card io" if self.card == "io" else "no card"
        elif tgt is None:
            why = (f"no free slot: {'B' if self.running == 'B' else 'A'} runs, "
                   f"{self.deflt} is the default -- rollback first")
        elif rm_slot != 0 and rm_slot != {"A": 1, "B": 2}[tgt]:
            why = f"slot mismatch: the target is {tgt}"
        elif self.slot[tgt]["state"] == "bad" and self.slot[tgt].get("overlap"):
            why = f"slot {tgt} bad: {self.slot[tgt].get('err', '')}"
        elif self.slot[tgt]["state"] == "absent":
            why = f"slot {tgt} absent"
        elif total > self.slot_bytes - _SLOT_RECORD:
            why = f"image too large for slot {tgt}"
        if why:
            self.job = {"act": "push", "slot": tgt if why.startswith(("slot mismatch", "slot ",
                                                                      "image too")) else None,
                        "state": "failed", "got": 0, "len": total, "err": why}
            return "failed", tgt, total
        # push_begin(): what this boot knew about the slot is gone
        self.vcrc[tgt] = self.vsid[tgt] = 0
        if self.staged == tgt:
            self.staged = None
        self.job = {"act": "push", "slot": tgt, "state": "writing", "got": 0, "len": total, "err": ""}
        return "go", tgt, total

    def push_end(self, slot: str, header: bytes, payload: bytes) -> Optional[str]:
        """The bytes that arrived (``payload``: at most the declared length).
        ``None`` = accepted (the read-back is running), else the job's err."""
        total, crc = self.job["len"], struct.unpack(">I", header[20:24])[0]
        self.job["got"] = len(payload)
        first = payload[:512]
        table = None
        if len(first) >= 32:
            why, table = _s0lb_table(first, total)
            need_more = why == "table truncated" and len(first) < total and len(first) < 512
            if why and not need_more:
                self._fail(why)
                return why
        body_started = table is not None and len(payload) > 512
        why = None
        if len(payload) < total:
            why = "torn (the push stopped early)"
        elif zlib.crc32(payload) & 0xFFFFFFFF != crc:
            why = "aborted"                               # config_agent's transport CRC
        elif table is None:
            why = "table truncated"
        else:
            for i, (src, ln, rcrc) in enumerate(table["entries"]):
                if zlib.crc32(payload[src:src + ln]) & 0xFFFFFFFF != rcrc:
                    why = f"region {i} CRC"
                    break
        if why:
            if body_started:
                self.slot[slot] = {"state": "empty"}      # the old first sector is gone
            self._fail(why)
            return why
        if body_started:
            self.slot[slot] = {"state": "empty"}          # header written last, by the child
        sid = struct.unpack(">I", header[8:12])[0]
        self._verifying(act="push", slot=slot, hdr_crc=table["hdr_crc"], extent=table["extent"],
                        sid=sid, err="")
        return None

_OP_RRQ, _OP_WRQ, _OP_DATA, _OP_ACK, _OP_ERROR = 1, 2, 3, 4, 5


class ConfigAgentStatus(Enum):
    """Python mirror of config_agent.h's ``config_agent_status_t``."""

    OK = "ok"
    ERR_MAGIC = "header magic != 'MPS3' (or short header)"
    ERR_VERSION = "unsupported header version"
    ERR_STATIC_ID = "static_id mismatch (overlay-manifest.md: partials are only valid against their exact static)"
    ERR_KIND = "kind not clearing(0)/partial(1)"
    ERR_CRC = "payload crc32 mismatch"
    ERR_ORDER = "partial pushed before its pair's clearing (I2 ordering)"
    ERR_SIZE = "payload length disagrees with header len_words"
    ERR_TRUNCATED = "transport ended before len_words*4 payload bytes arrived"


@dataclass(frozen=True)
class BitstreamInfo:
    """Python mirror of config_agent.h's ``config_agent_bitstream_info_t``
    (+ kind/rm_slot, which the C keeps in the staged header)."""

    kind: BitstreamKind
    rm_slot: int
    rm_id: int
    static_id: int
    len_words: int
    crc32: int

    @classmethod
    def from_header(cls, header: BitstreamHeader) -> "BitstreamInfo":
        return cls(
            kind=header.kind, rm_slot=header.rm_slot, rm_id=header.rm_id,
            static_id=header.static_id, len_words=header.len_words,
            crc32=header.crc32,
        )


@dataclass(frozen=True)
class PushEvent:
    """One accepted or rejected bitstream transfer, for test assertions."""

    transport: str            # "tftp" | "tcp"
    ok: bool
    status: str               # ConfigAgentStatus.name, or a transport-level reason
    info: Optional[BitstreamInfo] = None   # header info, if the header parsed


class ConfigAgentModel:
    """Stateful port of ``firmware/config_agent/config_agent.c``: header
    validation order (magic -> ver -> static_id -> kind -> size -> I2
    ordering), payload length + CRC32 check, and the
    ``s_ordering_seen_clearing`` flag semantics (set when a clearing
    completes; reset when the paired partial is accepted) — widened to
    stage one full {clearing, partial} pair, per the "push-vs-swap
    interleaving" ambiguity in the module docstring.

    Not thread-safe by itself; :class:`FakeShell` serialises access.
    """

    def __init__(self, running_static_id: int, *, max_payload_words: int = MAX_PAYLOAD_WORDS):
        self.running_static_id = running_static_id
        self.max_payload_words = max_payload_words
        self._ordering_seen_clearing = False
        self._staged_clearing: Optional[BitstreamInfo] = None
        self._staged_partial: Optional[BitstreamInfo] = None

    # -- header time (BEFORE any payload/ICAP handling) --------------------- #

    def validate_header(self, header_bytes: bytes) -> Tuple[ConfigAgentStatus, Optional[BitstreamHeader]]:
        """config_agent_validate_header_ex(), same check order as the C.

        The version field is peeked manually (2 big-endian bytes) only so
        the *order* of checks matches the C exactly (ver before kind);
        everything else defers to the pusher's real unpack.
        """
        if len(header_bytes) < HEADER_SIZE or header_bytes[:4] != MAGIC:
            return ConfigAgentStatus.ERR_MAGIC, None
        ver = int.from_bytes(header_bytes[4:6], "big")
        if ver != MPS3_BITSTREAM_VER:
            return ConfigAgentStatus.ERR_VERSION, None
        try:
            header = BitstreamHeader.unpack(header_bytes)
        except BitstreamFramingError:
            # Magic/length/version already vetted above; the only remaining
            # unpack failure is an unknown kind byte.
            return ConfigAgentStatus.ERR_KIND, None
        if header.static_id != self.running_static_id:
            return ConfigAgentStatus.ERR_STATIC_ID, header
        if header.len_words > self.max_payload_words:
            return ConfigAgentStatus.ERR_SIZE, header
        if header.kind == BitstreamKind.PARTIAL and not self._ordering_seen_clearing:
            return ConfigAgentStatus.ERR_ORDER, header
        return ConfigAgentStatus.OK, header

    # -- payload complete ---------------------------------------------------- #

    def finish_payload(self, header: BitstreamHeader, payload: bytes) -> ConfigAgentStatus:
        """config_agent_check_payload_crc() + the RECV_PAYLOAD->RECV_DONE
        bookkeeping. On OK the bitstream is staged for :meth:`take_pair`."""
        expected = header.len_words * 4
        if len(payload) < expected:
            return ConfigAgentStatus.ERR_TRUNCATED
        if len(payload) > expected:
            return ConfigAgentStatus.ERR_SIZE
        if zlib.crc32(payload) & 0xFFFFFFFF != header.crc32:
            return ConfigAgentStatus.ERR_CRC
        info = BitstreamInfo.from_header(header)
        if header.kind == BitstreamKind.CLEARING:
            # A clearing starts a (new) pair; any stale partial is dropped.
            self._staged_clearing = info
            self._staged_partial = None
            self._ordering_seen_clearing = True
        else:
            self._staged_partial = info
            # Mirrors config_agent_take_validated_partial(): the ordering
            # flag resets once the pair is complete, so the NEXT pair must
            # again start with a clearing.
            self._ordering_seen_clearing = False
        return ConfigAgentStatus.OK

    # -- hand-off to the swap FSM ------------------------------------------- #

    @property
    def pair_ready(self) -> bool:
        return self._staged_clearing is not None and self._staged_partial is not None

    def take_clearing(self) -> Optional[BitstreamInfo]:
        """Mirrors config_agent_take_validated_clearing(): the swap FSM's
        ``step_await_incoming_clearing()`` consumes the staged clearing the
        moment it lands — it does NOT wait for the whole pair (the FSM has to
        reach ``SWAP_AWAIT_PARTIAL`` before the partial is even admitted)."""
        info, self._staged_clearing = self._staged_clearing, None
        return info

    def take_partial(self) -> Optional[BitstreamInfo]:
        """Mirrors config_agent_take_validated_partial(): consumed by
        ``step_await_partial()``."""
        info, self._staged_partial = self._staged_partial, None
        return info

    def take_pair(self) -> Optional[Tuple[BitstreamInfo, BitstreamInfo]]:
        """Both halves at once (all-or-nothing). Retained for callers that
        pre-stage a pair out-of-band and just want it back; the swap itself
        takes them one at a time, in the order the FSM awaits them."""
        if not self.pair_ready:
            return None
        return (self.take_clearing(), self.take_partial())


# --------------------------------------------------------------------------- #
# socketserver plumbing
# --------------------------------------------------------------------------- #


class _ShellTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, handler_cls, shell: "FakeShell", **extra):
        self.shell = shell
        for key, value in extra.items():
            setattr(self, key, value)
        super().__init__(addr, handler_cls)


class _ControlHandler(socketserver.StreamRequestHandler):
    """TCP 6900: one JSON object per ``\\n``-terminated line, strict
    request->response (net-protocol.md "Control channel")."""

    def handle(self):
        shell = self.server.shell
        # HUNG: the connection is up (the kernel accepted it) and nothing
        # reads it. Hold it until the client gives up, the shell stops, or the
        # hang clears -- in which case the buffered lines are served, exactly
        # like a process that resumes and finds its socket full.
        while shell.hung and not shell._stop.is_set():
            try:
                self.connection.settimeout(0.05)
                peek = self.connection.recv(1, socket.MSG_PEEK)
                if peek == b"":
                    return                       # client closed on us
            except socket.timeout:
                pass
            except OSError:
                return
        self.connection.settimeout(None)
        if shell._stop.is_set():
            return
        for raw in self.rfile:
            line = raw.strip()
            if not line:
                continue
            try:
                request = json.loads(line.decode("utf-8"))
                if not isinstance(request, dict):
                    raise ValueError("request must be a JSON object")
            except (ValueError, UnicodeDecodeError) as exc:
                response = {"ok": False, "err": f"malformed request line: {exc}"}
            else:
                response = shell.handle_control(request, peer=self.client_address[0])
            self.wfile.write(json.dumps(response, separators=(",", ":")).encode("ascii") + b"\n")
            self.wfile.flush()


class _RawPushHandler(socketserver.BaseRequestHandler):
    """TCP 6910: 24-byte header then len_words*4 payload bytes; the server
    closes the connection once validation finishes (accept or reject) —
    see the "no acknowledgement" ambiguity note in the module docstring."""

    def handle(self):
        shell = self.server.shell
        shell._transfer_begin()
        try:
            self._receive(shell)
        finally:
            shell._transfer_end()

    def _receive(self, shell: "FakeShell"):
        sock = self.request
        sock.settimeout(shell.transfer_timeout)
        header_bytes = _recv_exactly(sock, HEADER_SIZE)
        if (shell.slots is not None and len(header_bytes) == HEADER_SIZE
                and header_bytes[:4] == MAGIC and header_bytes[6] == SLOT_IMAGE_KIND):
            shell._slot_push_tcp(sock, header_bytes, self.client_address[0])
            return
        status, header = shell._ca_validate_header("tcp", header_bytes)
        if status is not ConfigAgentStatus.OK:
            # Reject BEFORE reading the payload — the config agent must
            # never touch ICAP (here: never even accumulate) on a bad
            # header (net-protocol.md "Bitstream framing").
            return
        # The busy-ICAP window: a partial that arrives while the outgoing
        # clearing still streams is PARKED here, payload unread (never refused
        # on 6910 -- config_agent.c RECV_PENDING_ICAP_BEGIN).
        shell._icap_gate("tcp", header)
        if not shell._admit_push("tcp", header):
            # No swap is awaiting this bitstream. The real shell RESETS the
            # connection here (swap_fsm.c's icap_direct_begin() fails closed,
            # config_agent.c closes the socket, the host sees ECONNRESET and
            # the shell's `diag.got` never leaves 0). Force a genuine RST —
            # SO_LINGER with a zero timeout — instead of relying on the
            # incidental "close with unread bytes queued" RST, so the client
            # sees the same failure regardless of how much of the payload it
            # had managed to send.
            _force_rst_on_close(sock)
            return
        expected = header.len_words * 4
        # Read one byte beyond the declared length: a well-formed client
        # then just delivers EOF; anything extra is a framing violation
        # (ERR_SIZE), and anything less is a torn payload (ERR_TRUNCATED).
        payload = _recv_exactly(sock, expected + 1)
        shell._ca_finish("tcp", header, payload)


class _ConsoleHandler(socketserver.BaseRequestHandler):
    """TCP 6930/6931/6932: raw byte stream — banner on connect, then echo
    or absorb (net-protocol.md port map; pyverify.console is the client)."""

    def handle(self):
        shell = self.server.shell
        banner = shell.banners.get(self.server.console_name, b"")
        if banner:
            self.request.sendall(banner)
        while True:
            try:
                data = self.request.recv(4096)
            except OSError:
                break
            if not data:
                break
            if shell.console_echo:
                self.request.sendall(data)


def _force_rst_on_close(sock: socket.socket) -> None:
    """Make this socket's ``close()`` emit a TCP RST rather than a FIN, so the
    peer sees ``ECONNRESET`` — the failure the real shell gives a push it is not
    expecting. ``SO_LINGER`` with ``l_onoff=1, l_linger=0`` is the portable way
    to ask for that (struct linger = two native ints)."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    except OSError:  # pragma: no cover — platform without SO_LINGER
        pass


def _recv_exactly(sock: socket.socket, count: int) -> bytes:
    """Read up to ``count`` bytes; stops early on EOF or timeout (the
    caller distinguishes a short result as torn/truncated)."""
    buf = bytearray()
    while len(buf) < count:
        try:
            chunk = sock.recv(count - len(buf))
        except socket.timeout:
            break
        except OSError:
            break
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def _hex32(value: int) -> str:
    return "0x%08x" % (value & 0xFFFFFFFF)


_HEX32_RE = re.compile(r"^0[xX][0-9a-fA-F]{1,8}$")


def _usd_flip(slot: Optional[str]) -> str:
    return "B" if slot == "A" else "A"


def _parse_commit(request: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, int]]]:
    """The v0.13 ``commit`` request, or ``None`` (= ``bad args``). Strict: every
    field is required; ids and CRCs are ``"0x"`` hex strings, lengths positive
    ints that are multiples of 4, ``src`` exactly ``"tcp"``. The v0.11 form
    (``rm`` only) therefore fails here."""
    rm = request.get("rm")
    if not isinstance(rm, str) or not rm:
        return None
    if request.get("src") != "tcp":
        return None
    out: Dict[str, int] = {}
    for key in ("rm_id", "static_id", "clear_crc", "part_crc"):
        value = request.get(key)
        if not isinstance(value, str) or not _HEX32_RE.match(value):
            return None
        out[key] = int(value, 16)
    for key in ("clear_len", "part_len"):
        value = request.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0 or value % 4:
            return None
        out[key] = value
    return rm, out


# --------------------------------------------------------------------------- #
# Driving the swap FSM in stages (the parked-swap model)
# --------------------------------------------------------------------------- #


def _step_until(fsm: SwapFsm, targets: Tuple[str, ...], max_steps: int = 30) -> None:
    """Step the FSM until it settles in one of ``targets``.

    Stops *on* the target without stepping out of it — the AWAIT_* states are
    self-loops until their input arrives, and DONE/FAILED would fall back to
    IDLE (so ``fsm.history[-1]`` stays the state that actually failed).
    """
    for _ in range(max_steps):
        if fsm.state in targets:
            return
        fsm.step()
    raise TimeoutError(  # pragma: no cover — a model bug, not a scenario
        f"FSM did not reach any of {targets} within {max_steps} steps "
        f"(stuck in {fsm.state})"
    )


def _fail_await_timeout(fsm: SwapFsm) -> None:
    """The swap's IDLE timeout fired while it sat in an AWAIT_* state: nothing
    was pushed into the parked swap. Taken through the model's own pure
    transition table (``swap_fsm_transitions.c``: ``await_timeout`` is the one
    input that fails a SWAP_AWAIT_* state) rather than by assigning FAILED, so
    the fake cannot drift from the C on what a timeout does.
    """
    fsm.history.append(fsm.state)
    fsm.state = next_state(fsm.state, await_timeout=True)


# --------------------------------------------------------------------------- #
# The fake shell
# --------------------------------------------------------------------------- #


class FakeShell:
    """Threaded fake of the MicroBlaze shell firmware's network surface.

    Parameters (all keyword-only except ``host``) — defaults are the
    contract's real port numbers; pass ``0`` (or use
    :meth:`FakeShell.ephemeral`) for OS-assigned ports in tests. After
    :meth:`start` the *bound* ports are exposed as ``control_port``,
    ``tftp_port``, ``raw_tcp_port``, ``uart0_port``, ``uart1_port``,
    ``swo_port`` (and the ``console_ports`` mapping).

    Configurable identity/behaviour:

    - ``static_id`` — the running shell's static id (ping.shell_id; pushes
      with any other ``static_id`` are rejected).
    - ``boot_rm_id`` — rm_id reported at boot (default 0 = greybox, which
      :mod:`pyverify.edge` reads as "no RM loaded").
    - ``known_rm_ids`` — optional ``{rm_name: rm_id}``; when given, the
      ``swap`` verb rejects unknown RM names. (A v0.13 ``commit`` is keyed by
      ``rm_id``, like the firmware: the name is only the CLCD label.)
    - ``clk_presets`` — optional iterable of accepted ``set_clk`` presets
      (``None`` accepts anything, since the preset table is still open —
      I16); ``clk_locked`` is what a successful set_clk reports.
    - ``banners`` — per-console connect banner overrides
      (``{"uart0"/"uart1"/"swo": bytes}``); ``console_echo`` selects echo
      vs absorb after the banner.
    - ``telemetry_lockup`` — the raw ``dut_lockup`` pin the ``telemetry``
      verb reports on its (always-failing) reply. There are deliberately no
      ``telemetry_mv``/``telemetry_ma`` seeds: net-protocol.md v0.6 removed
      ``mv``/``ma`` from the protocol (there is no power sensor on this
      platform), so a fake shell that could still be seeded with a rail
      voltage would be modelling hardware that does not exist.
    - ``boot_active_slot`` — the user-microSD store header's active slot at
      boot; a ``commit`` writes the inactive slot and flips (net-protocol.md
      v0.13).
    - ``usd_*`` — the user microSD (net-protocol.md v0.13; see the module
      docstring): ``usd_hw`` (the fabric has ``usd_spi``), ``usd_card`` (a
      layout from :data:`USD_CARD_LAYOUTS`, ``None`` = no card),
      ``usd_card_mb``, ``usd_formatted`` (a ``da`` card's store header is
      written), ``usd_default`` (``{"rm", "rm_id", "static_id"?, "slot"?}`` --
      a committed overlay; implies a formatted ``da`` card), ``usd_fault``
      (:data:`USD_FAULTS`), ``usd_pb1_held`` (PB1 held at power-up),
      ``usd_boot`` (override the power-on decision; ``"pending"`` holds the
      power-on load open until :meth:`complete_usd_boot`), ``usd_allow_wipe``
      (profile default), ``usd_commit_fail`` (a commit's write fails with
      this error name).
    - ``rescue_sink`` / ``rescue_status`` — ``mode="rescue"`` hooks: a callable
      ``(data) -> (ok, err)`` for a pushed boot blob (default: record it in
      ``rescue_pushes`` and ACK; an ``err`` containing "access violation" is TFTP
      ERROR 2, any other ERROR 0), and extra stage0 status-block fields by name
      for RRQ ``stage0.status`` (unknown names are a ValueError). A subclass can
      override ``_rescue_sink(self, data)`` instead.
    - ``slots`` — the v0.14 boot slots (``profile="linux"`` only; see the module
      docstring and :class:`_SlotModel` for the keys). ``None`` (the default)
      leaves the double exactly as it was: no ``slot`` verb, no kind 2.
    - ``rm_id_readback_override`` / ``rm_id_valid`` — fault injection for
      the swap's VERIFY step (what DFXCTL.RM_ID/RM_STATUS "read back").
    - ``greybox_clearing_available`` — set False to model a shell image
      missing its greybox clearing seed (first swap then fails closed at
      STREAM_CLEARING, exactly like the firmware).
    - ``clearing_stream_bytes_per_s`` — THE BUSY-ICAP WINDOW (opt-in; ``None``,
      the default, streams instantly = every existing scenario). After ``swap``
      the firmware first streams the OUTGOING RM's cached clearing into the ICAP
      (SWAP_STREAM_CLEARING) at this rate, i.e. for ``resident clearing bytes /
      rate`` seconds. Meanwhile the incoming clearing is admitted (it lands in
      RAM, as config_agent.c's staging path does), but a PARTIAL whose header
      arrives inside the window cannot arm the ICAP-direct sink (swap_fsm.c
      ``icap_direct_begin`` needs SWAP_AWAIT_PARTIAL): over **TFTP** it is
      REJECTED (``TFTP ERROR 0 "rejected"``, ``icap_busy_rejects``) and the swap
      carries on; over **6910** it is PARKED -- the header is read, the payload
      is left unread (the host's send blocks on the closed window) until the
      stream ends (config_agent.c RECV_PENDING_ICAP_BEGIN, ``icap_defer_parks``).
      Silicon B1 v4 2026-09-25: harnessd, nanosoc (167,308 B clearing) ->
      dbg_demo over TFTP failed exactly so.

    Observability for tests: ``push_events`` (accepted + rejected
    transfers), ``swaps``, ``resets``, ``commits`` (``(rm, slot)`` per
    successful v0.13 commit), ``usd_slots`` (what is on the card),
    ``link_events``, ``pair_ready``, plus :meth:`wait_push_events` and
    :meth:`wait_awaiting`.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        *,
        control_port: int = CONTROL_PORT,
        tftp_port: int = TFTP_PORT,
        raw_tcp_port: int = RAW_TCP_PORT,
        uart0_port: int = UART0_PORT,
        uart1_port: int = UART1_PORT,
        swo_port: int = SWO_PORT,
        static_id: int = STATIC_ID,
        boot_rm_id: int = 0,
        known_rm_ids: Optional[Dict[str, int]] = None,
        clk_presets: Optional[Iterable[str]] = None,
        clk_locked: bool = True,
        reset_targets: Tuple[str, ...] = ("dut", "rp", "dbg"),
        boot_active_slot: str = "A",
        banners: Optional[Dict[str, bytes]] = None,
        console_echo: bool = True,
        telemetry_lockup: bool = False,
        harness_version: str = "0.0.0",
        harness_ver32: int = 0,
        harness_sha: str = "unknown",
        harness_dirty: bool = False,
        harness_usr_access: Optional[int] = None,
        lmb_kb: int = 1024,
        features: Iterable[str] = (),
        diag_counters: Optional[Dict[str, int]] = None,
        has_clcd_kvm: bool = True,
        has_dut_egress: bool = True,
        dut_frames: Optional[Iterable[bytes]] = None,
        dutegr_drop_full: int = 0,
        dutegr_drop_giant: int = 0,
        dutegr_ovf: bool = False,
        dutegr_desync: bool = False,
        boot_display_owner: str = "harness",
        display_commit_polls: int = 0,
        macgen_frames_per_call: int = 8,
        greybox_clearing_available: bool = True,
        greybox_clearing_len_words: int = 4,
        transfer_timeout: float = 5.0,
        swap_await_timeout: float = 10.0,
        swap_arm_grace: float = 1.0,
        clearing_stream_bytes_per_s: Optional[float] = None,
        profile: str = "bare-metal",
        impl: Optional[str] = None,
        hung: bool = False,
        identify: Optional[bool] = None,
        identify_port: int = IDENTIFY_PORT,
        identify_rate_per_s: float = 10.0,
        tofu: Optional[bool] = None,
        ssh_claimed: bool = False,
        ssh_host_key_sha256: str = "SHA256:fakeshell0000000000000000000000000000000000",
        unit: Optional[str] = None,
        v011_verbs: Optional[bool] = None,
        omit_diag_keys: Optional[Iterable[str]] = None,
        mode: str = "run",
        has_watchdog: bool = True,
        reboot_in_ms: int = 2784,
        os_boot_ms: int = 18000,
        mac: str = "0002f7ef441c",
        board: str = "mps3",
        board_ip: str = "192.168.10.101",
        dhcp: bool = False,
        has_overlay_store: Optional[bool] = None,
        rescue_reason: str = "no card",
        rescue_sink: Optional[Any] = None,
        rescue_status: Optional[Dict[str, int]] = None,
        usd_hw: bool = True,
        usd_card: Optional[str] = None,
        usd_card_mb: int = 15193,
        usd_formatted: bool = True,
        usd_default: Optional[Dict[str, Any]] = None,
        usd_fault: Optional[str] = None,
        usd_pb1_held: bool = False,
        usd_boot: Optional[str] = None,
        usd_allow_wipe: Optional[bool] = None,
        usd_commit_fail: Optional[str] = None,
        slots: Optional[Dict[str, Any]] = None,
        identity: Optional[Dict[str, Any]] = None,
        panel: Optional[Dict[str, Any]] = None,
    ):
        if boot_active_slot not in ("A", "B"):
            raise ValueError("boot_active_slot must be 'A' or 'B'")
        # --- engine profile (opt-in; the default is today's bare metal) ----- #
        # A profile supplies defaults for lmb_kb/features ONLY where the caller
        # left the bare-metal default in place, so an explicit value always
        # wins. (A linux scenario that genuinely wants lmb_kb=1024 or no
        # features -- no real image does -- sets the attribute after init.)
        if profile not in PROFILES:
            raise ValueError(f"unknown profile {profile!r} (known: {sorted(PROFILES)})")
        prof = PROFILES[profile]
        self.profile = profile
        if lmb_kb == 1024:
            lmb_kb = prof["lmb_kb"]
        if not tuple(features):
            features = prof["features"]
        #: ``version.impl`` -- None = the key is ABSENT (bare metal, the wire
        #: default); set, it is appended to the version line.
        self.impl: Optional[str] = impl if impl is not None else prof["impl"]
        #: HUNG mode: 6900 still ACCEPTS (a hung harnessd's kernel completes
        #: the handshake from its listen backlog) but never replies, so a
        #: client times out on the read -> "wedged". A DEAD harnessd is
        #: stop(): nothing listens, the connect is REFUSED -> "offline". start()
        #: again on the same ports revives it with state intact.
        self.hung = bool(hung)
        #: the identify responder (UDP 6899) and the TOFU authorized_keys claim
        self.identify_enabled = prof["identify"] if identify is None else bool(identify)
        self.identify_port = identify_port
        self.identify_rate_per_s = float(identify_rate_per_s)
        self.tofu_enabled = prof["tofu"] if tofu is None else bool(tofu)
        self.ssh_claimed = bool(ssh_claimed)
        self.ssh_host_key_sha256 = ssh_host_key_sha256
        #: the claimed key file's bytes, once a TOFU claim was accepted
        self.authorized_keys: Optional[bytes] = None
        #: every identify request seen: (addr, request-or-None, replied?)
        self.identify_requests: List[Tuple[Any, Optional[Dict[str, Any]], bool]] = []
        self.unit = unit
        self._identify_sock: Optional[socket.socket] = None
        self._identify_times: List[float] = []
        #: net-protocol v0.11 verbs (stats/log/touch_cal/reboot). The default
        #: (bare-metal) double predates them and still answers "unknown op";
        #: the linux profile has them, since harnessd runs the v0.11 coordinator.
        self.v011_verbs = prof["v011_verbs"] if v011_verbs is None else bool(v011_verbs)
        self.omit_diag_keys = frozenset(
            prof["omit_diag_keys"] if omit_diag_keys is None else omit_diag_keys)
        unknown_omit = [k for k in self.omit_diag_keys if k not in DIAG_COUNTERS]
        if unknown_omit:
            raise ValueError(f"unknown diag keys to omit {unknown_omit!r}")
        #: "run" (the harness is up) or "rescue" (stage0's TFTP rescue server:
        #: TFTP + identify(mode=rescue) only, NO 6900/6910/consoles).
        if mode not in ("run", "rescue"):
            raise ValueError("mode must be 'run' or 'rescue'")
        self.mode = mode
        #: blobs pushed to the rescue server (mode="rescue"), in order
        self.rescue_pushes: List[bytes] = []
        self.has_watchdog = bool(has_watchdog)
        self.reboot_in_ms = int(reboot_in_ms)
        #: how long the OS had been up when the harness process started (the
        #: linux profile's stats/identify os_up_ms = this + up_ms)
        self.os_boot_ms = int(os_boot_ms)
        self.mac = mac
        self.board = board
        self.board_ip = board_ip
        self.dhcp = bool(dhcp)
        self.has_overlay_store = (prof["has_overlay_store"] if has_overlay_store is None
                                  else bool(has_overlay_store))
        #: `erase-all` permitted (bare metal) or "wipe disabled" (Linux)
        self.usd_allow_wipe = (prof["usd_allow_wipe"] if usd_allow_wipe is None
                               else bool(usd_allow_wipe))
        #: identify ``reason`` in rescue mode (STAGE0's S0_RR_* text)
        self.rescue_reason = rescue_reason
        #: mode="rescue" test hooks (additive): ``rescue_sink(data) -> (ok, err)``
        #: replaces what a pushed boot blob does (default: append to
        #: rescue_pushes and ACK); ``rescue_status`` = extra/overriding stage0
        #: status-block fields BY NAME (pyverify.mailbox's STAGE0 layout), for the
        #: RRQ ``stage0.status`` answer. A mutable dict: change it mid-test.
        if rescue_sink is not None:
            self._rescue_sink = rescue_sink
        self.rescue_status: Dict[str, int] = dict(rescue_status or {})
        _rescue_fields_check(self.rescue_status)
        self.reboots: List[float] = []
        self.log_ring = bytearray(
            b"--- MPS3 nanoSoC shell firmware (A3) starting ---\r\n"
            + (b"mps3-harnessd: impl linux\r\n" if self.impl else b""))
        self.log_dropped = 0
        self.touch_cal = list(TOUCH_CAL_DEFAULT)
        self.touch_raw = {"raw_x": 0, "raw_y": 0, "raw_z": 0, "seen": 0}
        self._t0 = time.monotonic()
        self.host = host
        self.control_port = control_port
        self.tftp_port = tftp_port
        self.raw_tcp_port = raw_tcp_port
        self.uart0_port = uart0_port
        self.uart1_port = uart1_port
        self.swo_port = swo_port

        # Identity / behaviour config.
        self.static_id = static_id
        # v0.14 boot slots: opt-in, and only where they exist (the Linux harness).
        if slots is not None and profile != "linux":
            raise ValueError("slots= models mps3-harnessd's v0.14 `slot` verb: it needs "
                             "profile='linux' (bare metal has no boot slots)")
        self.slots: Optional[_SlotModel] = _SlotModel(self, slots) if slots is not None else None
        self.known_rm_ids = dict(known_rm_ids) if known_rm_ids is not None else None
        self.clk_presets = frozenset(clk_presets) if clk_presets is not None else None
        self.clk_locked = clk_locked
        self.reset_targets = tuple(reset_targets)
        self.banners = {"uart0": b"nanosoc boot\n", "uart1": b"", "swo": b""}
        if banners:
            self.banners.update(banners)
        self.console_echo = console_echo
        self.telemetry_lockup = telemetry_lockup
        # Build identity the `version` verb reports (net-protocol.md v0.8 /
        # firmware coordinator_handle_version). DEFAULTS are the firmware's own
        # honest "not provisioned" answer -- the weak mps3_harness_*() seam's
        # 0.0.0 / ver32 0 / sha "unknown" / not dirty -- which is exactly what
        # ctrl_echo reports, because it links only the weak TU. A test modelling
        # a PROVISIONED board passes real values.
        #
        # `features` is the FIRMWARE's compile-time flag set; `has_clcd_kvm`
        # above is the FABRIC's 0x44AD slave. They are different facts and the
        # firmware keeps them separate too (a CLCD_KVM=1 image on a KVM-less
        # bitstream still lists "clcd_kvm" and still answers "clcd_kvm not
        # present"), so a scenario modelling a real board sets BOTH.
        self.harness_version = harness_version
        self.harness_ver32 = harness_ver32
        self.harness_sha = harness_sha
        self.harness_dirty = harness_dirty
        # Wave-B cross-check (net_proto.h "usr_access"/"skew"): what the FABRIC
        # says it is (the bitstream's USR_ACCESS/AXSS register), beside
        # harness_ver32's "what the IMAGE was built as". None (the default) is
        # ctrl_echo's own answer -- coordinator_handle_version() sets no
        # usr_access, so the real firmware's memset(&resp, 0, ...) leaves it
        # empty and the wire carries "usr_access":null,"skew":null: a THIRD
        # state (could not be read), not a pass. A scenario modelling a real
        # board's readback passes the raw u32; `skew` is then DERIVED (never
        # stored) by _op_version, exactly like the firmware derives it in
        # mps3_ctrl_encode_response() -- comparing the two _hex32() strings, not
        # the two ints, so there is one rendering and one comparison.
        self.harness_usr_access: Optional[int] = harness_usr_access
        self.lmb_kb = lmb_kb
        known = VERSION_FEATURES + ENGINE_FEATURES
        unknown = [f for f in features if f not in known]
        if unknown:
            raise ValueError(
                f"unknown version features {unknown!r} "
                f"(known: {list(known)})"
            )
        # Canonicalised to the firmware's bit order regardless of the order the
        # caller listed them -- the wire array's order is contract. Engine names
        # (v0.15) follow every bit name, in ENGINE_FEATURES order.
        self.features: Tuple[str, ...] = tuple(
            f for f in known if f in set(features)
        )
        # v0.16 board identity: served where the engine advertises it (the linux
        # profile's "identity" engine name); every other image declines the verbs,
        # as the firmware's weak provider does. `identity=` configures the model.
        if identity is not None and "identity" not in self.features:
            raise ValueError("identity= models mps3-harnessd's v0.16 identity verbs: the "
                             "features need 'identity' (profile='linux' has it)")
        self.identity: Optional[_IdentityModel] = (
            _IdentityModel(self, identity) if "identity" in self.features else None)
        self.locates: List[Tuple[int, str]] = []
        self.locate_until: Optional[float] = None
        # v0.17 presence + the front panel: served where the engine advertises
        # them (the linux profile's "presence"/"panel"); `panel=` configures it.
        if panel is not None and not {"presence", "panel"} & set(self.features):
            raise ValueError("panel= models mps3-harnessd's v0.17 hello/panel verbs: the "
                             "features need 'presence' or 'panel' (profile='linux' has both)")
        self.panel: Optional[_PanelModel] = (
            _PanelModel(self, panel) if {"presence", "panel"} & set(self.features) else None)
        # `diag` counter mailbox (firmware/common/diag.h). All zero by default:
        # that is a healthy idle shell AND what ctrl_echo reports (its mocks
        # publish nothing), so a scenario-matched fake is byte-identical. A test
        # wanting a wedged-RX scenario overrides the counters it cares about.
        self.diag_counters: Dict[str, int] = {k: 0 for k in DIAG_COUNTERS}
        if diag_counters:
            unknown_diag = [k for k in diag_counters if k not in self.diag_counters]
            if unknown_diag:
                raise ValueError(f"unknown diag counters {unknown_diag!r}")
            if "usd_boot" in diag_counters:
                # Not a scenario counter: it IS the power-on latch (diag.h v9),
                # derived from the usd_* scenario so the two can never disagree.
                raise ValueError("diag 'usd_boot' is the power-on latch word, derived "
                                 "from the usd_* scenario -- it cannot be seeded")
            self.diag_counters.update(diag_counters)
        # CLCD KVM (net-protocol.md "display"). has_clcd_kvm models whether the
        # running bitstream has the 0x44AD slave: on today's board it does NOT,
        # and the verb answers {"ok":false,"err":"clcd_kvm not present"} exactly
        # like coordinator_handle_display()'s OFF-build path. Default True: this
        # double is the executable spec of the FUTURE (Wave-4) board pyverify is
        # written against. `display_owner` is the committed owner.
        #
        # `display_commit_polls` models THE HANDOVER LAG, which is a documented
        # part of the contract and not a simulation nicety: net-protocol.md
        # "Display" says a flip takes a drain -> panel reset -> settle -> grant
        # (~7-9 ms) to commit, so "immediately after a flip the reply can still
        # report the OUTGOING owner". 0 (the default) keeps the previous
        # idealized instant handover, so every existing test and every existing
        # caller is unchanged. N > 0 means the next N `display` requests after a
        # flip still report the OUTGOING owner, and the (N+1)th reports the new
        # one -- which is what makes `ShellClient.display_settled`'s
        # confirm-then-poll loop testable, and what makes its ABSENCE visible:
        # with N > 0 a bare `display` reports the owner that is going away.
        self.has_clcd_kvm = has_clcd_kvm
        if boot_display_owner not in ("harness", "dut"):
            raise ValueError("boot_display_owner must be 'harness' or 'dut'")
        if display_commit_polls < 0:
            raise ValueError("display_commit_polls must be >= 0")
        self.display_commit_polls = display_commit_polls
        self.display_owner = boot_display_owner
        #: The owner the KVM is heading towards; equals display_owner when no
        #: handover is in flight.
        self.display_target = boot_display_owner
        self._display_pending = 0
        self.macgen_frames_per_call = macgen_frames_per_call
        self.transfer_timeout = transfer_timeout
        #: How long an arriving push waits for a swap's gate to open before it is
        #: rejected — absorbs the thread-scheduling skew between this server's
        #: control and push listeners (see :meth:`_admit_push`). Small enough
        #: that a genuine push-before-swap still fails promptly; comfortably
        #: larger than localhost thread-wakeup latency even under load.
        self.swap_arm_grace = swap_arm_grace
        #: How long a parked swap waits, IDLE, for the bitstream it is awaiting
        #: before failing through the FSM's ``await_timeout`` transition. Idle,
        #: not total: an in-flight transfer re-arms it (swap_fsm.c re-arms the
        #: deadline on every byte of RX progress), so a multi-MB push cannot
        #: trip it. Tests that deliberately never push want this SMALL.
        self.swap_await_timeout = swap_await_timeout
        if clearing_stream_bytes_per_s is not None and clearing_stream_bytes_per_s <= 0:
            raise ValueError("clearing_stream_bytes_per_s must be > 0 (or None: instant)")
        #: The busy-ICAP window's stream rate (see the class docstring).
        self.clearing_stream_bytes_per_s = clearing_stream_bytes_per_s
        #: monotonic() until which the outgoing clearing is streaming (0: idle).
        self._icap_busy_until = 0.0
        #: PARTIALs refused over TFTP inside the busy window / parked on 6910.
        self.icap_busy_rejects = 0
        self.icap_defer_parks = 0

        # VERIFY-step fault injection (what the "hardware" reads back).
        self.rm_id_readback_override: Optional[int] = None
        self.rm_id_valid = True

        # Mutable shell state (mirrors g_shell_state + the A/B store header).
        self.current_rm_id = boot_rm_id
        if greybox_clearing_available:
            # swap_fsm_init()'s boot seed: the greybox's clearing ships
            # inside the shell image (overlay-manifest.md).
            self._current_clearing = ClearingRef(
                valid=True, rm_id=boot_rm_id, static_id=static_id,
                len_words=greybox_clearing_len_words, crc32=0,
            )
        else:
            self._current_clearing = ClearingRef()
        self.active_slot = boot_active_slot
        self.link_up = True
        self.config_agent = ConfigAgentModel(static_id)

        # GENCHK gen/checker model (net-protocol.md "MAC gen/checker control").
        # Counters the `macgen` verb reads back; the register enables + armed
        # inject are the last-written CTRL/INJECT. Counters are monotonic
        # *within* an enabled session but the RTL CLEARS all three on a
        # gen_en/chk_en 0→1 rising edge (shell-regmap.md v0.4 "Counter
        # semantics") — _op_macgen models that clear-on-enable-rise so the
        # divergence is pinned, not silent.
        self.genchk_gen_en = False
        self.genchk_chk_en = False
        self.genchk_inject = "none"
        self.genchk_tx = 0
        self.genchk_rx = 0
        self.genchk_err = 0

        # DUT-egress capture model (net-protocol.md v0.10 "DUT egress";
        # shell-regmap.md DUTEGR; fpga/shell/ip/dut_egress/).
        #
        # `has_dut_egress` is the FABRIC's 0x44B2 slave, exactly like
        # `has_clcd_kvm` above -- and like it, the default models the board this
        # client is WRITTEN for (the block is in the BD, so it arrives at the
        # next mint), while today's fielded bitstream is has_dut_egress=False
        # and answers "dut_egress not present".
        #
        # `dut_frames` are whole frames waiting in the capture FIFO. WHOLE is
        # the word: the block is store-and-forward, so a frame becomes readable
        # only once every byte of it is committed and a torn frame can never be
        # observed -- which is what lets a chunked read be stateless, and why
        # this list holds complete frames rather than a byte stream.
        self.has_dut_egress = has_dut_egress
        self.dut_frames: List[bytes] = [bytes(f) for f in (dut_frames or ())]
        #: bytes of the head frame already handed out (the FIFO IS the cursor)
        self._dutrx_off = 0
        #: DUTEGR.RX_FRAMES -- frames captured WHOLE. Seeded from the queue so a
        #: scenario does not have to keep two numbers in step; push_dut_frame()
        #: advances it, exactly as the block does on a commit.
        self.dutegr_rx_frames = len(self.dut_frames)
        #: DROP_FULL / DROP_GIANT. Settable because the interesting scenario is
        #: a capture that LOST frames: the block cannot backpressure the bridge,
        #: so it drops, and rx + drop_full + drop_giant == frames presented is
        #: the invariant a host checks. A fake that could not model a drop could
        #: not test the client half of that.
        self.dutegr_drop_full = int(dutegr_drop_full)
        self.dutegr_drop_giant = int(dutegr_drop_giant)
        #: STATUS.OVF / STATUS.DESYNC -- sticky, reported raw, never interpreted
        self.dutegr_ovf = bool(dutegr_ovf)
        self.dutegr_desync = bool(dutegr_desync)
        #: every dutrx reply, for tests to assert the chunk sequence on
        self.dutrx_reads: List[Dict[str, Any]] = []

        # The user microSD (net-protocol.md v0.13). Set up LAST among the state
        # (below), because the power-on load reads current_rm_id / the clearing
        # cache and may replace them.
        self._usd_init(
            hw=usd_hw, card=usd_card, card_mb=usd_card_mb, formatted=usd_formatted,
            default=usd_default, fault=usd_fault, pb1_held=usd_pb1_held,
            commit_fail=usd_commit_fail,
        )

        # Observability.
        self.push_events: List[PushEvent] = []
        self.swaps: List[Dict[str, Any]] = []
        self.resets: List[str] = []
        self.commits: List[Tuple[str, str]] = []
        self.link_events: List[str] = []
        self.clk_requests: List[str] = []
        self.macgen_calls: List[Dict[str, Any]] = []
        self.display_requests: List[str] = []

        # Concurrency plumbing.
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._active_transfers = 0
        #: THE ARMING GATE. ``None`` = the shell is not expecting a bitstream and
        #: every push is reset; otherwise it is the ONE :class:`BitstreamKind` a
        #: parked swap is currently awaiting (the FSM's ``SWAP_AWAIT_INCOMING_
        #: CLEARING`` / ``SWAP_AWAIT_PARTIAL``).
        #:
        #: It is advanced CLEARING -> PARTIAL -> None **atomically with each
        #: acceptance**, inside :meth:`_ca_finish` under the lock — NOT by the
        #: parked swap's thread when it gets around to stepping the FSM. That
        #: matters: the real client is single-threaded and opens the partial's
        #: connection the instant the clearing's push returns, so a gate that
        #: only opened once the FSM thread had been rescheduled would race and
        #: reject a perfectly correct push. The FSM (the behaviour model) then
        #: follows the gate; the gate is what admission is decided on.
        self._await_kind: Optional[BitstreamKind] = None
        #: The ONE transport the gate admits (``"tcp"`` while a ``commit`` is
        #: parked: v0.13 commits take 6910 only), or ``None`` = either.
        self._await_transport: Optional[str] = None
        self._swap_in_flight = False
        #: A v0.13 ``commit`` is parked (the store is mid-job).
        self._commit_in_flight = False
        #: Payload bytes the parked commit received, by kind.
        self._commit_payloads: Dict[BitstreamKind, bytes] = {}
        #: When the parked swap last saw RX progress — the idle-timeout datum.
        self._await_since = 0.0
        self._stop = threading.Event()
        self._servers: List[socketserver.BaseServer] = []
        self._threads: List[threading.Thread] = []
        self._tftp_sock: Optional[socket.socket] = None
        self._started = False

        # The power-on decision, made ONCE per "FPGA configuration" -- i.e. per
        # FakeShell. A reboot (harnessd respawn / WDOG) never redoes it.
        self._usd_power_on(usd_boot)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    @classmethod
    def ephemeral(cls, host: str = "127.0.0.1", **kwargs) -> "FakeShell":
        """A FakeShell with every port OS-assigned (for tests)."""
        for key in ("control_port", "tftp_port", "raw_tcp_port",
                    "uart0_port", "uart1_port", "swo_port", "identify_port"):
            kwargs.setdefault(key, 0)
        return cls(host, **kwargs)

    def start(self) -> "FakeShell":
        if self._started:
            return self
        self._stop.clear()

        if self.mode == "rescue":
            # stage0's rescue server: TFTP + identify only. NOTHING listens on
            # 6900/6910/6930-2, so every TCP connect there is refused.
            tcp_servers: List[socketserver.BaseServer] = []
            self.console_ports = {}
        else:
            control = _ShellTCPServer((self.host, self.control_port), _ControlHandler, self)
            self.control_port = control.server_address[1]
            raw = _ShellTCPServer((self.host, self.raw_tcp_port), _RawPushHandler, self)
            self.raw_tcp_port = raw.server_address[1]
            consoles = []
            for name, port_attr in (("uart0", "uart0_port"), ("uart1", "uart1_port"), ("swo", "swo_port")):
                server = _ShellTCPServer(
                    (self.host, getattr(self, port_attr)), _ConsoleHandler, self,
                    console_name=name,
                )
                setattr(self, port_attr, server.server_address[1])
                consoles.append(server)
            self.console_ports = {
                "uart0": self.uart0_port, "uart1": self.uart1_port, "swo": self.swo_port,
            }
            tcp_servers = [control, raw] + consoles

        self._tftp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._tftp_sock.bind((self.host, self.tftp_port))
        self.tftp_port = self._tftp_sock.getsockname()[1]
        self._tftp_sock.settimeout(0.2)

        if self.identify_enabled or self.mode == "rescue":
            self._identify_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._identify_sock.bind((self.host, self.identify_port))
            self.identify_port = self._identify_sock.getsockname()[1]
            self._identify_sock.settimeout(0.2)

        self._servers = tcp_servers
        self._threads = [
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
            for server in self._servers
        ]
        self._threads.append(threading.Thread(target=self._tftp_serve, daemon=True))
        if self._identify_sock is not None:
            self._threads.append(threading.Thread(target=self._identify_serve, daemon=True))
        for thread in self._threads:
            thread.start()
        self._started = True
        return self

    def stop(self) -> None:
        if not self._started:
            return
        self._stop.set()
        for server in self._servers:
            server.shutdown()
            server.server_close()
        if self._tftp_sock is not None:
            self._tftp_sock.close()
            self._tftp_sock = None
        if self._identify_sock is not None:
            self._identify_sock.close()
            self._identify_sock = None
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._servers = []
        self._threads = []
        self._started = False

    def __enter__(self) -> "FakeShell":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()

    # ------------------------------------------------------------------ #
    # Control channel (TCP 6900) — the 12 verbs of net-protocol.md v0.10
    # ------------------------------------------------------------------ #
    #
    # This tuple is the double's whole verb set and must equal the firmware's
    # s_op_table (net_proto.c). `diag` was missing from it until v0.8 -- the
    # firmware had shipped the verb for weeks and the reference server answered
    # "unknown op 'diag'", which is precisely the class of drift
    # tests/firmware_logic/test_fakeshell_conformance.py exists to catch and
    # could not, because no conformance case sent `diag`. Both are fixed
    # together: the verb is here, and a case sends it.

    def handle_control(self, request: Dict[str, Any],
                       peer: Optional[str] = None) -> Dict[str, Any]:
        """One request. ``peer`` is the client's IP (the socket passes it); only
        the v0.14 slot lock reads it, and an unknown peer is never local."""
        op = request.get("op")
        verbs: Tuple[str, ...] = (
            "ping", "reset", "set_clk", "swap", "link", "commit", "telemetry",
            "macgen", "display", "diag", "version", "dutrx",
            # v0.13 (D13): the user microSD. On BOTH profiles -- the firmware's
            # op table has it unconditionally; a fabric without usd_spi answers
            # state "no_hw", an engine without the store "unavailable".
            "usd",
            # v0.16: the board identity + locate. On BOTH profiles, like `usd`: the
            # firmware's op table has them; an engine without them declines.
            "identity", "identity_set", "locate",
            # v0.17: presence + the front panel, likewise.
            "hello", "panel",
        )
        if self.v011_verbs:
            verbs += ("stats", "log", "touch_cal", "reboot")
        if self.slots is not None:
            verbs += ("slot",)                            # v0.14, opt-in (slots=)
        if not isinstance(op, str) or op not in verbs:
            return {"ok": False, "err": f"unknown op {op!r}"}
        if op == "slot":
            return self._op_slot(request, peer)
        if op in ("identity", "identity_set"):
            return self._op_identity(op, request, peer)
        if op == "locate":
            return self._op_locate(request)
        if op in ("hello", "panel"):
            return self._op_panel_verbs(op, request, peer)
        return getattr(self, f"_op_{op}")(request)

    # ------------------------------------------------------------------ #
    # net-protocol v0.17 `hello` / `panel` (the linux profile)
    # ------------------------------------------------------------------ #

    def _panel_peer_local(self, peer: Optional[str]) -> bool:
        if self.identity is not None:
            return self.identity._peer_local(peer)
        return peer is not None and peer.startswith("127.")

    def _op_hello(self, request: Dict[str, Any]) -> Dict[str, Any]:
        return self._op_panel_verbs("hello", request, None)

    def _op_panel(self, request: Dict[str, Any], peer: Optional[str] = None) -> Dict[str, Any]:
        return self._op_panel_verbs("panel", request, peer)

    def _op_panel_verbs(self, op: str, request: Dict[str, Any],
                        peer: Optional[str]) -> Dict[str, Any]:
        """coordinator_handle_panel(): the claim lock first (a page change), then
        panel_linux.c -- or the weak default's decline (bare metal, no panel).
        A request line over MPS3_NET_LINE_MAX (256 characters) never reaches the
        verb on the board: the line layer answers ``bad json``."""
        feature = "presence" if op == "hello" else "panel"
        if self.panel is None or feature not in self.features:
            return {"ok": False, "err": f"{op} not supported", "code": "not_supported"}
        if len(json.dumps(request, separators=(",", ":")).encode()) > 256:
            return {"ok": False, "err": "bad json"}
        with self._lock:
            if op == "hello":
                return self.panel.op_hello(request)
        return self.panel.op_panel(request, peer)

    # ------------------------------------------------------------------ #
    # net-protocol v0.16 `identity` / `identity_set` (the linux profile)
    # ------------------------------------------------------------------ #

    def _op_locate(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """locate_linux.c: ``s`` 0..30 (0 stops), optional printable ``who`` <= 32;
        a new one replaces the old; NOT claim-locked. ``until_ms`` is relative.
        ``locates`` records every accepted request (s, who) for tests; ``locate_until``
        is the monotonic end (None = not running)."""
        if "locate" not in self.features:
            return {"ok": False, "err": "locate not supported", "code": "not_supported"}
        s, who = request.get("s"), request.get("who", "")
        if not isinstance(s, int) or isinstance(s, bool) or not 0 <= s <= 30:
            return {"ok": False, "err": "invalid s: 0..30 (seconds; 0 stops)", "code": "invalid"}
        if not isinstance(who, str) or len(who) > 32:
            return {"ok": False, "err": "invalid who: a string of <= 32 characters",
                    "code": "invalid"}
        if any(not " " <= ch <= "~" for ch in who):
            return {"ok": False, "err": "invalid who: printable ASCII only", "code": "invalid"}
        with self._lock:
            self.locates.append((s, who))
            self.locate_until = (time.monotonic() + s) if s else None
        return {"ok": True, "op": "locate", "until_ms": s * 1000}

    def _op_identity(self, op: str, request: Dict[str, Any],
                     peer: Optional[str]) -> Dict[str, Any]:
        """coordinator_handle_identity(): the claim lock first (identity_set), then
        the engine's provider -- or the weak default's decline (bare metal)."""
        if self.identity is None:
            return {"ok": False, "err": IDENTITY_NOT_SUPPORTED_ERR, "code": "not_supported"}
        with self._lock:
            if op == "identity":
                return self.identity.status()
            return self.identity.set(request, peer)

    # ------------------------------------------------------------------ #
    # net-protocol v0.14 `slot` -- opt-in (slots=, linux profile)
    # ------------------------------------------------------------------ #

    def _op_slot(self, request: Dict[str, Any], peer: Optional[str]) -> Dict[str, Any]:
        """coordinator_handle_slot(): decode (``act`` required, ``slot`` optional,
        both strings within net_proto.h's buffers -- else ``bad args``), then the
        VALUES (``bad act`` / ``bad slot``), then the provider (the model)."""
        act, sel = request.get("act"), request.get("slot")
        if not isinstance(act, str) or len(act) > _SLOT_ACT_MAX:
            return {"ok": False, "err": "bad args"}
        if "slot" in request and (not isinstance(sel, str) or len(sel) > _SLOT_SEL_MAX):
            return {"ok": False, "err": "bad args"}
        if act not in ("status", "commit", "rollback", "verify"):
            return _slot_refusal("bad act")
        sel = sel or None                                 # "" == no selector (slot_sel[0] == 0)
        if sel is not None and sel not in ("A", "B"):
            return _slot_refusal("bad slot")
        assert self.slots is not None
        with self._lock:
            err, reply = self.slots.op(act, sel, peer)
        return _slot_refusal(err) if err else reply

    # ------------------------------------------------------------------ #
    # net-protocol v0.11 verbs -- opt-in (the linux profile turns them on)
    # ------------------------------------------------------------------ #

    def up_ms(self) -> int:
        """Milliseconds since this harness (process) started -- ``stats.up_ms``,
        the reboot witness."""
        return int((time.monotonic() - self._t0) * 1000) & 0xFFFFFFFF

    def _op_stats(self, request: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            last = self.swaps[-1] if self.swaps else None
            failed = [sw for sw in self.swaps if sw["final"] != "DONE"]
            preset = self.clk_requests[-1] if self.clk_requests else None
            mhz = 50
            if preset and preset.endswith("mhz") and preset[:-3].isdigit():
                mhz = int(preset[:-3])
            up = self.up_ms()
            reply: Dict[str, Any] = {
                "ok": True, "up_ms": up, "sid": _hex32(self.static_id),
                "rm": _hex32(self.current_rm_id), "rm_ok": bool(self.rm_id_valid),
                "lock": bool(self.telemetry_lockup),
                "clk_sel": (sorted(self.clk_presets).index(preset)
                            if preset and self.clk_presets and preset in self.clk_presets else 0),
                "mmcm": bool(self.clk_locked), "clk_alive": True, "dut_rst": True,
                "rp_rst": True, "decpl": False, "link": bool(self.link_up),
                "spd": 100 if self.link_up else 0, "fdx": bool(self.link_up),
                "mac": self.mac,
                "swap": "await_partial" if self._swap_in_flight else "idle",
                "swap_ok": bool(last and last["final"] == "DONE"),
                "swap_n": len(self.swaps),
                "icap": int(self.diag_counters.get("icap_bytes", 0)),
                "rxdrop": int(self.diag_counters.get("rx_drops", 0)),
                "txerr": int(self.diag_counters.get("tx_errors", 0)),
                "swap_err": (failed[-1]["history"][-1].lower() if failed and failed[-1]["history"] else ""),
                "clr_ok": bool(self._current_clearing.valid),
                "dut_mhz": mhz, "svc_max_us": 0, "svc_skipped": 0,
            }
            if "touch" in self.features:
                # ADDITIVE (2026-09-24, D1): a TOUCH=1 build of either engine
                # reports touch liveness; before os_up_ms, which stays LAST.
                reply["touch_ok"] = True
                reply["touch_bus_lost"] = 0
                reply["touch_recoveries"] = 0
            if "lcd_mirror" in self.features:
                # ADDITIVE (v0.15, Linux): the LCD mirror's oldest client; this
                # double serves no 6940, so there is never one. Before os_up_ms.
                reply["lcd_mirror"] = {"peer": None, "since": 0, "fps": 0.0, "bytes": 0}
            if self.impl:
                # ADDITIVE (Linux): the kernel's uptime. up_ms is the harness
                # PROCESS's (a respawn resets it without a reboot).
                reply["os_up_ms"] = (self.os_boot_ms + up) & 0xFFFFFFFF
            return reply

    def _op_log(self, request: Dict[str, Any]) -> Dict[str, Any]:
        off = request.get("off", 0)
        if not isinstance(off, int) or isinstance(off, bool) or off < 0:
            return {"ok": False, "err": "bad args"}
        with self._lock:
            head = self.log_dropped + len(self.log_ring)
            start = max(off, self.log_dropped)
            if start > head:
                start = head
            chunk = bytes(self.log_ring[start - self.log_dropped:][:256])
            n = len(chunk)
            return {"ok": True, "off": start, "n": n, "more": start + n < head,
                    "dropped": self.log_dropped, "data": chunk.hex()}

    def log_print(self, text: bytes) -> None:
        """Append console text to the 4 KiB ring (the oldest bytes fall off and
        are counted in ``dropped``, exactly like the firmware's log_ring)."""
        with self._lock:
            self.log_ring.extend(text)
            over = len(self.log_ring) - 4096
            if over > 0:
                del self.log_ring[:over]
                self.log_dropped += over

    def _op_touch_cal(self, request: Dict[str, Any]) -> Dict[str, Any]:
        if "touch" not in self.features:
            return {"ok": False, "err": "touch not present"}
        act = request.get("act", "get")
        with self._lock:
            if act == "get":
                pass
            elif act == "default":
                self.touch_cal = list(TOUCH_CAL_DEFAULT)
            elif act == "raw":
                return {"ok": True, **self.touch_raw, "x": 0, "y": 0}
            elif act == "set":
                vals = [request.get(k) for k in _TOUCH_CAL_KEYS]
                if any(not isinstance(v, int) or isinstance(v, bool) for v in vals):
                    return {"ok": False, "err": "bad args"}
                ax, bx, cx, ay, by, cy, shift = vals
                if not 0 <= shift <= 24:
                    return {"ok": False, "err": "bad shift"}
                if max(abs(ax), abs(bx), abs(ay), abs(by)) > (1 << 17):
                    return {"ok": False, "err": "coef range"}
                if max(abs(cx), abs(cy)) > (1 << 29):
                    return {"ok": False, "err": "offset range"}
                if ax * by - bx * ay == 0:
                    return {"ok": False, "err": "singular"}
                self.touch_cal = list(vals)
            else:
                return {"ok": False, "err": "bad args"}
            return {"ok": True, **dict(zip(_TOUCH_CAL_KEYS, self.touch_cal))}

    def _op_reboot(self, request: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            if self._swap_in_flight:
                return {"ok": False, "err": "EBUSY"}
            if self.slots is not None:                    # a card job holds the card
                self.slots.poll()                         # (harnessd, HM_ANSWERS change 6)
                if self.slots.busy():
                    return {"ok": False, "err": "EBUSY"}
            if not self.has_watchdog:
                return {"ok": False, "err": "no watchdog"}
            in_ms = self.reboot_in_ms
            self.reboots.append(time.monotonic())
        # The reply goes out FIRST; the restart lands in_ms later: up_ms (and,
        # on Linux, os_up_ms -- a WDOG reset restarts the whole MBV) restart.
        t = threading.Timer(in_ms / 1000.0, self._simulate_restart)
        t.daemon = True
        t.start()
        return {"ok": True, "in_ms": in_ms}

    def _simulate_restart(self) -> None:
        with self._lock:
            self._t0 = time.monotonic()
            self.os_boot_ms = 18000 if self.impl else 0
            if self.slots is not None:
                self.slots.reboot()                       # stage0 boots the default (S9)

    def _op_ping(self, request: Dict[str, Any]) -> Dict[str, Any]:
        # net-protocol.md: {"ok":true,"shell_id":"<static_id>","rm_id":"<id>"}
        with self._lock:
            return {
                "ok": True,
                "shell_id": _hex32(self.static_id),
                "rm_id": _hex32(self.current_rm_id),
            }

    def _op_reset(self, request: Dict[str, Any]) -> Dict[str, Any]:
        target = request.get("target", "dut")
        if target not in self.reset_targets:
            return {"ok": False, "err": f"unknown reset target {target!r}"}
        with self._lock:
            self.resets.append(target)
        return {"ok": True}

    def _op_set_clk(self, request: Dict[str, Any]) -> Dict[str, Any]:
        preset = request.get("preset")
        # Failure shape is the firmware's uniform {"ok":false,"err":...} — the
        # coordinator's mps3_ctrl_encode_response() emits NO per-op fields on
        # failure (net_proto.c), so the reference server must not either. The
        # earlier "locked":false echo here was a fakeshell-only divergence the
        # firmware↔fakeshell conformance suite (tests/firmware_logic/
        # test_fakeshell_conformance.py) now pins closed; the client defaults
        # SetClkResponse.locked to False when the key is absent, so nothing
        # downstream needs it.
        if not isinstance(preset, str) or not preset:
            return {"ok": False, "err": "set_clk requires a 'preset' string"}
        if self.clk_presets is not None and preset not in self.clk_presets:
            return {"ok": False, "err": f"unknown clk preset {preset!r} (I16 preset table)"}
        with self._lock:
            self.clk_requests.append(preset)
        return {"ok": True, "locked": self.clk_locked}

    def _op_link(self, request: Dict[str, Any]) -> Dict[str, Any]:
        event = request.get("event")
        # net-protocol.md v0.2 + firmware coordinator_handle_link accept
        # down/up/pulse (I31(b) — align with the real server). Speed events
        # remain undefined in the contract → fail closed.
        if event not in ("up", "down", "pulse"):
            return {"ok": False, "err": f"unknown link event {event!r}"}
        with self._lock:
            if event in ("up", "down"):
                self.link_up = event == "up"
            # "pulse" is a momentary event: recorded, steady state unchanged.
            self.link_events.append(event)
        return {"ok": True}

    def _op_commit(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """``commit`` -- net-protocol.md v0.13: a RE-PUSH of the running pair
        into the user microSD's inactive slot. PARKS like ``swap``.

        The v0.11 form (``rm`` only) is ``bad args``, as is any missing or
        mistyped field and any ``src`` but ``"tcp"``. The refusals come back AT
        ONCE, before the park: nothing is armed, so a push the host sends
        anyway is reset (it is not awaited)."""
        parsed = _parse_commit(request)
        if parsed is None:
            return {"ok": False, "err": "bad args"}
        rm, desc = parsed
        with self._cond:
            if not self.has_overlay_store:
                return {"ok": False, "err": "unavailable"}
            # The firmware's order (coordinator_handle_commit, then the store's
            # commit_begin): the swap FSM must be idle (the power-on load IS a
            # swap), then the card-state gate, then a store job, then the ids.
            if self._swap_in_flight or self._usd_boot_pending:
                return {"ok": False, "err": "store busy"}
            gate = self._usd_write_gate()
            if gate is not None:
                return {"ok": False, "err": gate}
            if self._commit_in_flight:
                return {"ok": False, "err": "store busy"}
            if desc["static_id"] != self.static_id:
                # Contract: refused unless static_id equals the shell's own. The
                # error-name table has no dedicated name; "stale key" is the
                # static_id-mismatch name (the CLCD's word for a slot minted for
                # another shell). Flagged as a contract ambiguity.
                return {"ok": False, "err": "stale key"}
            if desc["rm_id"] != self.current_rm_id:
                return {"ok": False, "err": "rm mismatch"}
            blocks = (-(-desc["clear_len"] // _USD_BLOCK)) + (-(-desc["part_len"] // _USD_BLOCK))
            if blocks * _USD_BLOCK > USD_SLOT_BYTES:
                # The store's "too big for slot" has no contract name either;
                # it is a request the store can never take: "bad args".
                return {"ok": False, "err": "bad args"}
            self._commit_in_flight = True
            self._commit_payloads = {}
        try:
            return self._run_commit(rm, desc)
        finally:
            with self._cond:
                self._commit_in_flight = False
                self._await_kind = None           # the gate closes with the commit
                self._await_transport = None
                self._commit_payloads = {}
                self._cond.notify_all()

    def _run_commit(self, rm: str, desc: Dict[str, int]) -> Dict[str, Any]:
        """The parked half of ``commit``: admit the clearing, then the partial,
        over 6910 only; check each against the request's length and CRC-32;
        then write the INACTIVE slot and flip the header. Every failure leaves
        the previous default exactly as it was."""
        with self._cond:
            self._await_kind = BitstreamKind.CLEARING
            self._await_transport = "tcp"
            self._await_since = time.monotonic()
            self._cond.notify_all()
        got: Dict[BitstreamKind, BitstreamInfo] = {}
        for kind, len_key, crc_key in (
            (BitstreamKind.CLEARING, "clear_len", "clear_crc"),
            (BitstreamKind.PARTIAL, "part_len", "part_crc"),
        ):
            info = self._await_bitstream(kind)
            if info is None:
                return {"ok": False, "err": "timeout"}
            if info.len_words * 4 != desc[len_key] or info.crc32 != desc[crc_key]:
                # The config agent already proved the payload matches ITS header;
                # this is the request's own claim about the bytes failing.
                return {"ok": False, "err": "crc"}
            got[kind] = info
        with self._cond:
            if not (self.usd_hw and self.usd_card is not None):
                return {"ok": False, "err": "no card"}        # pulled mid-commit
            if self.usd_commit_fail is not None:
                return {"ok": False, "err": self.usd_commit_fail}
            state, chosen = self._usd_eval()
            if state == "valid":
                target = _usd_flip(chosen)
            elif state == "stale":
                target = _usd_flip(self.active_slot)
            elif state == "bad":
                target = self.active_slot        # overwrite the corrupt one
            else:
                target = "A"                     # empty: slot A first
            self.usd_slots[target] = UsdSlot(
                rm=rm, rm_id=desc["rm_id"], static_id=desc["static_id"],
                clear_len=desc["clear_len"], clear_crc=desc["clear_crc"],
                part_len=desc["part_len"], part_crc=desc["part_crc"],
                clearing=self._commit_payloads.get(BitstreamKind.CLEARING, b""),
                partial=self._commit_payloads.get(BitstreamKind.PARTIAL, b""),
            )
            # Read back + CRCs checked (modelled as always passing unless a
            # fault was injected above) -- ONLY THEN does the header flip.
            self.active_slot = target
            self.usd_default_valid = True
            self.usd_skip = False
            self.commits.append((rm, target))
            return {"ok": True, "slot": target}

    # ------------------------------------------------------------------ #
    # net-protocol v0.13: the user microSD (`usd`)
    # ------------------------------------------------------------------ #

    def _usd_init(self, *, hw: bool, card: Optional[str], card_mb: int, formatted: bool,
                  default: Optional[Dict[str, Any]], fault: Optional[str],
                  pb1_held: bool, commit_fail: Optional[str]) -> None:
        if card is not None and card not in USD_CARD_LAYOUTS:
            raise ValueError(f"unknown usd_card layout {card!r} (known: {list(USD_CARD_LAYOUTS)})")
        if fault is not None and fault not in USD_FAULTS:
            raise ValueError(f"unknown usd_fault {fault!r} (known: {list(USD_FAULTS)})")
        if commit_fail is not None and commit_fail not in USD_ERRORS:
            raise ValueError(f"usd_commit_fail must be a contract error name, got {commit_fail!r}")
        #: the fabric has the usd_spi block (False = state "no_hw")
        self.usd_hw = bool(hw)
        self.usd_card_mb = int(card_mb)
        self.usd_fault = fault
        self.usd_pb1_held = bool(pb1_held)
        self.usd_commit_fail = commit_fail
        #: the store's two slots (None = never written)
        self.usd_slots: Dict[str, Optional[UsdSlot]] = {"A": None, "B": None}
        #: the header's active descriptor is valid (False = no default: "empty")
        self.usd_default_valid = False
        #: PB1 was held at power-up with a card in: text "skipped"
        self.usd_skip = False
        #: the power-on decision (`usd.boot`), set by _usd_power_on
        self.usd_boot = "none"
        self._usd_boot_pending = False
        if default is not None:
            card = "da" if card is None else card
            if card != "da":
                raise ValueError("usd_default needs a 'da' card")
            formatted = True
            slot = default.get("slot", self.active_slot)
            if slot not in ("A", "B"):
                raise ValueError("usd_default slot must be 'A' or 'B'")
            self.active_slot = slot
            self.usd_slots[slot] = UsdSlot(
                rm=str(default.get("rm", "")),
                rm_id=int(default["rm_id"]),
                static_id=int(default.get("static_id", self.static_id)),
                clear_len=int(default.get("clear_len", 4)),
                clear_crc=int(default.get("clear_crc", 0)),
                part_len=int(default.get("part_len", 4)),
                part_crc=int(default.get("part_crc", 0)),
                corrupt=bool(default.get("corrupt", False)),
            )
            self.usd_default_valid = True
        #: the card in the slot (a layout), or None
        self.usd_card: Optional[str] = card
        #: a 'da' card's store header has been written (else it is "foreign")
        self.usd_formatted = bool(formatted)

    def _usd_eval(self) -> Tuple[str, Optional[str]]:
        """``(state, chosen_slot)`` -- the store's reading of the card.
        ``chosen_slot`` is the slot a ``valid`` state would load."""
        if not self.usd_hw:
            return "no_hw", None
        if self.usd_card is None:
            return "none", None
        if self.usd_fault is not None:
            return self.usd_fault, None
        if self.usd_card != "da" or not self.usd_formatted:
            return "foreign", None             # no 0xDA store: read-only forever
        act = self.active_slot
        desc = self.usd_slots.get(act)
        if not self.usd_default_valid or desc is None:
            return "empty", None
        if desc.static_id != self.static_id:
            return "stale", None               # THE GATE: its bytes are never read
        if not desc.corrupt:
            return "valid", act
        other = self.usd_slots.get(_usd_flip(act))
        if other is not None and not other.corrupt and other.static_id == self.static_id:
            return "valid", _usd_flip(act)     # the fallback slot verifies
        return "bad", None                     # both slots fail CRC

    def _usd_text(self, state: str, chosen: Optional[str]) -> str:
        """The CLCD row-4 text after ``USD : `` (at most 16 chars)."""
        if self.usd_skip and self.usd_card is not None:
            return "skipped"
        if state == "valid" and chosen is not None:
            d = self.usd_slots[chosen]
            name = (d.rm or _hex32(d.rm_id))[:11]
            return f"{name} [{chosen}]"
        return {"no_hw": "no hw", "none": "none", "init": "init",
                "unsupported": "unsupported", "error": USD_ERROR_TEXT,
                "foreign": "foreign", "empty": "empty", "stale": "stale key",
                "bad": "bad"}[state]

    def usd_status(self) -> Dict[str, Any]:
        """The ``{"op":"usd"}`` reply, in wire key order."""
        with self._lock:
            state, chosen = self._usd_eval()
            reply: Dict[str, Any] = {
                "ok": True,
                "present": bool(self.usd_hw and self.usd_card is not None),
                "state": state,
                "text": self._usd_text(state, chosen),
            }
            if state in USD_READY_STATES:
                reply["card_mb"] = int(self.usd_card_mb)
            if state in ("valid", "stale"):
                slot = chosen if state == "valid" else self.active_slot
                d = self.usd_slots[slot]
                reply["default"] = {"rm_id": _hex32(d.rm_id),
                                    "static_id": _hex32(d.static_id), "slot": slot}
            reply["boot"] = self.usd_boot
            return reply

    def _usd_busy(self) -> bool:
        """The store is mid-job: a parked commit, or the power-on load. (A
        running swap keeps the store idle, but a commit cannot start while the
        swap FSM owns the RP either.) Caller holds the lock."""
        return self._commit_in_flight or self._usd_boot_pending or self._swap_in_flight

    def _usd_card_gate(self) -> Optional[str]:
        """The error for a card that cannot be written at all (the store's
        state gate minus FOREIGN), or None. Caller holds the lock."""
        state, _ = self._usd_eval()
        if state == "no_hw":
            return "no hw"
        if state == "none":
            return "no card"
        if state == "init":
            return "store busy"                # being probed / verified
        if state in ("unsupported", "error"):
            return "unavailable"
        return None

    def _usd_write_gate(self) -> Optional[str]:
        """The state gate for commit / clear: a card-not-ready error, or
        ``foreign`` (never written, except by the explicit wipe)."""
        gate = self._usd_card_gate()
        if gate is not None:
            return gate
        state, _ = self._usd_eval()
        return "foreign" if state == "foreign" else None

    def _op_usd(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """``usd`` -- status (never held; answers during a swap) or one of the
        actions ``format`` / ``clear`` / ``rescan``."""
        # `action` and `confirm` are OPTIONAL strings (absent == ""); present
        # but not a string is a malformed request (the firmware's decode).
        action = request.get("action", "")
        confirm = request.get("confirm", "")
        if not isinstance(action, str) or not isinstance(confirm, str):
            return {"ok": False, "err": "bad args"}
        if not self.has_overlay_store:
            return {"ok": False, "err": "unavailable"}
        if action == "":
            return self.usd_status()
        if action == "format":
            return self._usd_format(confirm)
        if action == "clear":
            return self._usd_clear()
        if action == "rescan":
            return self._usd_rescan()
        return {"ok": False, "err": "bad args"}

    def _usd_format(self, confirm: Any) -> Dict[str, Any]:
        if confirm not in ("erase", "erase-all"):
            return {"ok": False, "err": "confirm required"}   # judged FIRST
        with self._lock:
            if self._usd_busy():
                return {"ok": False, "err": "store busy"}
            gate = self._usd_card_gate()       # FOREIGN passes: format is its way out
            if gate is not None:
                return {"ok": False, "err": gate}
            if confirm == "erase-all":
                if not self.usd_allow_wipe:
                    return {"ok": False, "err": "wipe disabled"}
                if self.usd_card_mb < USD_MIN_CARD_MB:
                    return {"ok": False, "err": "partition too small"}
            else:
                layout = self.usd_card
                if layout == "da":
                    pass                                             # rule (a)
                elif layout in ("blank", "empty_mbr"):               # rule (b)
                    if self.usd_card_mb < USD_MIN_CARD_MB:
                        return {"ok": False, "err": "partition too small"}
                elif layout == "da_small":
                    return {"ok": False, "err": "partition too small"}
                elif layout == "other":
                    return {"ok": False, "err": "exists"}           # rule (c)
                else:                            # fs / gpt / unknown: not blank
                    return {"ok": False, "err": "filesystem present"}
            # Write: a 0xDA store with two empty slots (the wipe first zeroes
            # LBA 1..33 and writes a 0xDA-only MBR -- same end state here).
            self.usd_card = "da"
            self.usd_formatted = True
            self.usd_slots = {"A": None, "B": None}
            self.active_slot = "A"
            self.usd_default_valid = False
            self.usd_skip = False
            return {"ok": True, "state": self._usd_eval()[0]}

    def _usd_clear(self) -> Dict[str, Any]:
        with self._lock:
            if self._usd_busy():
                return {"ok": False, "err": "store busy"}
            gate = self._usd_write_gate()
            if gate is not None:
                return {"ok": False, "err": gate}
            self.usd_default_valid = False     # greybox at the next power-on
            self.usd_skip = False
            return {"ok": True, "state": self._usd_eval()[0]}

    def _usd_rescan(self) -> Dict[str, Any]:
        with self._lock:
            if self._commit_in_flight:
                return {"ok": False, "err": "store busy"}
            gate = self._usd_card_gate()
            if gate is not None:
                return {"ok": False, "err": gate}
            # The firmware answers at once, with the probe just RESTARTED: the
            # reply's state is "init". (The probe is instant here, so the next
            # `usd` status already shows what it found.)
            return {"ok": True, "state": "init"}

    def _usd_power_on(self, override: Optional[str]) -> None:
        """The power-on load: ONCE, at construction (one FPGA configuration).
        Gates: a ready card, a VALID store (static_id matches, CRCs pass --
        both before any ICAP byte), PB1 not held. A load goes through the
        ordinary swap FSM, so it lands in ``swaps`` like any swap."""
        state, chosen = self._usd_eval()
        card_in = self.usd_hw and self.usd_card is not None
        if override is not None:
            try:
                usd_boot_word(override)          # only a text a latch word renders as
            except ValueError:
                raise ValueError(f"usd_boot must be loaded/skipped/none/pending/failed:<name> "
                                 f"(a reason the firmware latch can carry), got {override!r}"
                                 ) from None
            self.usd_boot = override
            self._usd_boot_pending = override == "pending"
            if override == "skipped":
                self.usd_skip = card_in
            if override == "loaded" and state == "valid":
                self._usd_load(chosen)
            return
        # Mirrors the firmware's boot hook (overlay_store.c boot_step()): PB1 is
        # sampled FIRST, card or not; only a VALID store is loaded, and only
        # into the greybox; every other state is "none" -- the state says why.
        if self.usd_pb1_held:
            self.usd_skip = card_in            # the CLCD says "skipped" while a card is in
            self.usd_boot = "skipped"
        elif state == "init":
            self.usd_boot = "pending"          # still deciding (card init / verify)
            self._usd_boot_pending = True
        elif state == "valid" and self.current_rm_id == 0:
            self._usd_load(chosen)
            self.usd_boot = "loaded"
        else:
            self.usd_boot = "none"

    def _usd_load(self, slot: Optional[str]) -> None:
        d = self.usd_slots[slot] if slot else None
        if d is None:
            return
        self.current_rm_id = d.rm_id
        self._current_clearing = ClearingRef(
            valid=True, rm_id=d.rm_id, static_id=d.static_id,
            len_words=max(1, d.clear_len // 4), crc32=d.clear_crc,
        )
        self.swaps.append({"rm": d.rm, "src": "usd", "final": "DONE",
                           "history": [], "rm_id": d.rm_id})

    def complete_usd_boot(self, result: str = "loaded") -> None:
        """Finish a ``usd_boot="pending"`` power-on load: ``boot`` becomes
        ``result`` (``"loaded"`` also loads the default, if one verifies) and
        swaps / commits are admitted again."""
        with self._cond:
            self._usd_boot_pending = False
            if self.usd_fault == "init":
                self.usd_fault = None
            if result == "loaded":
                state, chosen = self._usd_eval()
                if state == "valid":
                    self._usd_load(chosen)
            usd_boot_word(result)                # a text the latch can carry
            self.usd_boot = result
            self._cond.notify_all()

    def usd_insert(self, layout: str = "da", *, formatted: bool = True,
                   card_mb: Optional[int] = None) -> None:
        """Card detect: a card goes into the slot (settle + probe are
        instant here). The power-on decision is NOT redone."""
        if layout not in USD_CARD_LAYOUTS:
            raise ValueError(f"unknown usd card layout {layout!r}")
        with self._lock:
            self.usd_card = layout
            self.usd_formatted = bool(formatted)
            self.usd_fault = None
            if card_mb is not None:
                self.usd_card_mb = int(card_mb)
            if layout != "da" or not formatted:
                self.usd_slots = {"A": None, "B": None}
                self.usd_default_valid = False

    def usd_remove(self) -> None:
        """Card detect: the card leaves the slot (clears ``skipped``). A
        commit parked right now fails with ``no card``."""
        with self._lock:
            self.usd_card = None
            self.usd_skip = False
            # (the slots stay with the card: usd_insert("da") puts it back)

    def _op_telemetry(self, request: Dict[str, Any]) -> Dict[str, Any]:
        # net-protocol.md v0.6 / firmware coordinator_handle_telemetry():
        # telemetry has NO SUCCESS SHAPE. There is no power sensor reachable
        # from this design (TELEM's sample inputs are tied to ground in the
        # block design, its INA228 I2C engine was never written, its pads are
        # not on the top level, and the MPS3 MCC refuses voltage reads), so
        # the old {"ok":true,"mv":950,"ma":120,...} was a reading-shaped lie.
        # The verb now fails loudly and the mv/ma keys are ABSENT, not zeroed.
        #
        # This reply is the ONE declared carve-out to the uniform failure
        # shape: it carries "lockup" (a real partition pin) on the failure
        # line, because that failure line is the only line telemetry has.
        #
        # The err string is byte-pinned to the firmware's
        # (coordinator.c: set_err(resp, "no power sensor")) and the key ORDER
        # is pinned to net_proto.c's snprintf, because
        # tests/firmware_logic/test_fakeshell_conformance.py compares the two
        # servers' telemetry lines BYTE-FOR-BYTE. Change one, change both.
        with self._lock:
            return {
                "ok": False,
                "err": "no power sensor",
                "lockup": self.telemetry_lockup,
            }

    def _op_macgen(self, request: Dict[str, Any]) -> Dict[str, Any]:
        # net-protocol.md "MAC gen/checker control" / firmware
        # coordinator_handle_macgen: gen/chk are required JSON bools driving
        # GENCHK.CTRL; inject is a required fault name (INJECT one-hot) from
        # the closed MACGEN_INJECTS set. Fail closed on a missing/mistyped arg
        # ("bad args" — mirrors the firmware decode's EBADARGS) or an unknown
        # fault ("bad inject" — mirrors coordinator_handle_macgen). On success
        # the reply's "err" is the ERR_CNT *counter* (int), NOT a diagnostic.
        gen = request.get("gen")
        chk = request.get("chk")
        inject = request.get("inject")
        # Decode-level: a missing/wrong-typed gen/chk/inject is EBADARGS in the
        # firmware ("bad args"); the handler then rejects an unknown fault
        # *string* as "bad inject". Mirror that two-stage split exactly.
        if (not isinstance(gen, bool) or not isinstance(chk, bool)
                or not isinstance(inject, str)):
            return {"ok": False, "err": "bad args"}
        if inject not in MACGEN_INJECTS:
            return {"ok": False, "err": "bad inject"}

        with self._lock:
            # Model the gen/checker: with gen enabled a batch of frames is
            # generated (TX advances); with chk enabled they are checked
            # (RX advances). An armed inject (non-"none") makes exactly the
            # next generated frame fail the checker, so ERR advances by one —
            # only when there is actually a checked frame (gen+chk on). A
            # clean run ("none") never touches ERR.
            #
            # Clear-on-enable-rise (shell-regmap.md v0.4 "Counter semantics"):
            # the RTL zeroes all three counters on a gen_en/chk_en 0→1 rising
            # edge, so a MAC test that toggles an enable off then on sees the
            # counters restart — monotonic within a session, NOT across a
            # toggle. Detect the rise against the last-written enables BEFORE
            # updating them.
            gen_rise = gen and not self.genchk_gen_en
            chk_rise = chk and not self.genchk_chk_en
            if gen_rise or chk_rise:
                self.genchk_tx = 0
                self.genchk_rx = 0
                self.genchk_err = 0
            self.genchk_gen_en = gen
            self.genchk_chk_en = chk
            self.genchk_inject = inject
            frames = self.macgen_frames_per_call if gen else 0
            self.genchk_tx += frames
            if chk:
                self.genchk_rx += frames
                if frames > 0 and inject != "none":
                    self.genchk_err += 1
            reply = {
                "ok": True,
                "tx": self.genchk_tx,
                "rx": self.genchk_rx,
                "err": self.genchk_err,
            }
            self.macgen_calls.append(
                {"gen": gen, "chk": chk, "inject": inject,
                 "tx": self.genchk_tx, "rx": self.genchk_rx, "err": self.genchk_err}
            )
            return reply

    def _op_display(self, request: Dict[str, Any]) -> Dict[str, Any]:
        # net-protocol.md "display" / firmware coordinator_handle_display: the
        # CLCD KVM remote ownership flip. Drives the same frozen
        # CLCDKVM.CTRL.src_sel the button + a local CSR write drive, then reports
        # STATUS.owner. A plain send-now response (never held like swap).
        #
        # OFF-build parity: if this modelled bitstream has no KVM slave, the
        # verb declines exactly like the firmware's !CLCD_KVM_PRESENT path.
        if not self.has_clcd_kvm:
            return {"ok": False, "err": "clcd_kvm not present"}
        owner = request.get("owner")
        if not isinstance(owner, str):
            return {"ok": False, "err": "bad args"}
        with self._lock:
            if owner == "query":
                pass  # read-only: report the committed owner, move nothing
            elif owner == "toggle":
                # A toggle flips the requested TARGET, exactly like a button
                # press -- so a second toggle mid-handover cancels the first
                # (net-protocol.md "Display").
                self.display_target = (
                    "dut" if self.display_target == "harness" else "harness")
                self._display_pending = self.display_commit_polls
                self.display_requests.append(owner)
            elif owner in ("harness", "dut"):
                self.display_target = owner
                self._display_pending = self.display_commit_polls
                self.display_requests.append(owner)
            else:
                return {"ok": False, "err": "bad owner"}

            # Commit the handover after display_commit_polls further requests.
            # With the default 0 this is the previous idealized instant
            # handover: the committed owner equals the target in the same reply.
            if self._display_pending > 0:
                self._display_pending -= 1
            else:
                self.display_owner = self.display_target
            # The reply carries STATUS.owner -- the COMMITTED owner, which is
            # the outgoing one while a handover is still in flight.
            return {"ok": True, "owner": self.display_owner}

    def _op_diag(self, request: Dict[str, Any]) -> Dict[str, Any]:
        # net-protocol.md "diag" / firmware coordinator_handle_diag: a pure
        # readback of the always-on diagnostic mailbox (firmware/common/diag.h).
        # No arguments, never held, and no failure mode -- the counters are RAM,
        # so the only reply is ok:true.
        #
        # The KEY ORDER below is the firmware's snprintf order (DIAG_COUNTERS),
        # not alphabetical: the conformance suite compares these lines
        # byte-for-byte. Values are ints, matching the firmware's %lu.
        with self._lock:
            reply: Dict[str, Any] = {"ok": True}
            # Keys with no source on this engine are OMITTED, not zeroed
            # (the linux profile; see LINUX_OMITTED_DIAG_KEYS).
            reply.update({k: int(self.diag_counters[k]) for k in DIAG_COUNTERS
                          if k not in self.omit_diag_keys})
            if "usd_boot" in reply:
                reply["usd_boot"] = self.usd_boot_latch()
            return reply

    def usd_boot_latch(self) -> int:
        """The diag mailbox's ``usd_boot`` word (diag.h v9): the power-on
        load LATCH (overlay_store.h "THE BOOT LATCH") -- ``[31:16]`` 0xB007,
        ``[15:8]`` the failure reason, ``[3:0]`` the decision -- rendered from
        the same decision ``usd.boot`` reports, so a JTAG read of the mailbox and
        the network verb always agree. 0 on an engine with no store (nothing
        ever latches)."""
        with self._lock:
            if not self.has_overlay_store:
                return 0
            return usd_boot_word(self.usd_boot)

    def push_dut_frame(self, frame: bytes) -> None:
        """Commit one WHOLE frame into the capture FIFO, as the block does when
        the DUT transmits one. Advances ``RX_FRAMES`` with it: the counter
        counts frames captured whole, so the two cannot drift apart here the way
        they could if a scenario set both by hand."""
        with self._lock:
            self.dut_frames.append(bytes(frame))
            self.dutegr_rx_frames += 1

    def _op_dutrx(self, request: Dict[str, Any]) -> Dict[str, Any]:
        # net-protocol.md v0.10 "DUT egress" / firmware coordinator_handle_dutrx:
        # ONE CHUNK (<= DUTRX_CHUNK bytes) of the frame at the head of the
        # DUT-egress capture FIFO, hex-encoded. No arguments -- the destructive
        # DATA port is its own cursor, so there is no offset to send and no
        # per-connection state to keep. Send-now, never held like swap.
        #
        # OFF-build parity, first: a bitstream with no 0x44B2 slave declines,
        # exactly like the firmware's !DUTEGR_PRESENT path. It must NOT answer
        # ok:true with zeroes -- "this fabric cannot capture" and "the DUT sent
        # nothing" would be the same line, which is the reading-shaped lie v0.6
        # took out of `telemetry`.
        if not self.has_dut_egress:
            return {"ok": False, "err": "dut_egress not present"}

        with self._lock:
            head = self.dut_frames[0] if self.dut_frames else b""
            total = len(head)
            remain = total - self._dutrx_off
            n = min(remain, DUTRX_CHUNK)
            chunk = head[self._dutrx_off:self._dutrx_off + n]
            off = self._dutrx_off

            if self.dut_frames:
                self._dutrx_off += n
                if self._dutrx_off >= total:
                    # The descriptor retires with the frame's LAST byte: the
                    # frame leaves the queue here, which is why `frames` below
                    # (read AFTER the pops, like the firmware reads LEVEL) does
                    # not count a frame this reply just finished.
                    self.dut_frames.pop(0)
                    self._dutrx_off = 0

            reply: Dict[str, Any] = {
                "ok": True,
                "len": total,
                "off": off,
                "n": n,
                "more": (remain - n) > 0,
                # `last` is the raw DATA[9] bit as the firmware saw it on this
                # chunk's final byte -- the block's SECOND, independent record
                # of where the frame ends (the first is the descriptor length).
                # Reported, not reconciled: when the two disagree the block
                # latches DESYNC and this pair is the evidence.
                "last": n > 0 and (off + n) == total,
                "frames": len(self.dut_frames),
                "rx": int(self.dutegr_rx_frames),
                "drop_full": int(self.dutegr_drop_full),
                "drop_giant": int(self.dutegr_drop_giant),
                "ovf": bool(self.dutegr_ovf),
                "desync": bool(self.dutegr_desync),
                # Key ORDER is the firmware's snprintf order (net_proto.c's
                # MPS3_OP_DUTRX arm) -- the conformance suite compares these
                # lines byte-for-byte. `data` is LOWERCASE hex, last, so the one
                # variable-length field sits at the end of the line.
                "data": chunk.hex(),
            }
            self.dutrx_reads.append(dict(reply))
            return reply

    def _op_version(self, request: Dict[str, Any]) -> Dict[str, Any]:
        # net-protocol.md v0.8 "version" / firmware coordinator_handle_version:
        # what IMAGE is this? `ping` answers which FABRIC (static_id) and what
        # is in the RP (rm_id); neither says which firmware release is running
        # or which compile-time features are in it. One static_id serves many
        # harness releases, so those questions are genuinely independent.
        #
        # Every field is a build-time constant on the real server (no register
        # access), which is why this verb answers even on a board whose fabric
        # is unhappy -- and why the double can model it with plain attributes.
        # `dirty` is an INT (0/1), not a bool, matching the firmware's %d; the
        # conformance suite type-checks that. `features` is emitted in the
        # firmware's fixed bit order with absent features omitted.
        #
        # `usr_access`/`skew`: the cross-check, mirroring
        # mps3_ctrl_encode_response()'s MPS3_OP_VERSION arm exactly.
        # harness_usr_access is None (fabric value not read) on the
        # ctrl_echo-matched default, which emits `null` for BOTH keys -- a
        # third state, not a pass. Set, it is compared as a STRING against
        # ver32's own _hex32() rendering (not the two raw ints), so there is
        # one formatter and one comparison, same as the firmware.
        with self._lock:
            reply: Dict[str, Any] = {
                "ok": True,
                "harness": self.harness_version,
                "ver32": _hex32(self.harness_ver32),
                "sha": self.harness_sha,
                "dirty": 1 if self.harness_dirty else 0,
                "lmb_kb": int(self.lmb_kb),
                "features": list(self.features),
            }
            if self.harness_usr_access is None:
                reply["usr_access"] = None
                reply["skew"] = None
            else:
                usr_access_hex = _hex32(self.harness_usr_access)
                reply["usr_access"] = usr_access_hex
                reply["skew"] = usr_access_hex != _hex32(self.harness_ver32)
            # `impl` is ADDITIVE and emitted only when set (net-protocol.md;
            # HARNESSD's net_proto change), so the bare-metal line is
            # byte-identical to before. Appended LAST: HOST_CONTRACT.md records
            # that position as the assumption the conformance suite checks.
            if "lcd_mirror" in self.features:
                # ADDITIVE (v0.15, Linux): where the mirror listens; before impl.
                reply["lcd_mirror"] = {"port": LCD_MIRROR_PORT, "mode": "sw", "proto": 1}
            if self.impl:
                reply["impl"] = self.impl
            return reply

    def _op_swap(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """``swap`` — THE PARKED VERB (see the module docstring's ordering note).

        Does NOT reply until the incoming RM's clearing+partial have been pushed
        *into* it. The caller (a control-channel handler thread, or a test
        driving :meth:`handle_control` directly) blocks here for the whole
        reconfiguration, which is exactly what the real shell does to its 6900
        connection — and exactly what makes the bitstream push possible, since
        the push transports only admit data while this is parked.
        """
        rm = request.get("rm")
        src = request.get("src", "tftp")
        # Uniform firmware failure shape {"ok":false,"err":...} — see the note
        # in _op_set_clk. The client defaults SwapResponse.verified to False and
        # .rm_id to "" when those keys are absent, so dropping the failure-only
        # "verified"/"rm_id" echoes here matches net_proto.c byte-for-byte
        # without losing anything the client reads (pinned by the conformance
        # suite).
        #
        # All of these reject BEFORE arming: a swap the shell never accepted
        # must not leave the push transports open (and the host must not be
        # left pushing into a shell that already said no).
        if not isinstance(rm, str) or not rm:
            return {"ok": False, "err": "swap requires an 'rm' string"}
        if src not in ("tftp", "tcp"):
            return {"ok": False, "err": f"unknown swap src {src!r}"}
        if self.known_rm_ids is not None and rm not in self.known_rm_ids:
            return {"ok": False, "err": f"unknown rm {rm!r}"}
        with self._cond:
            if self._swap_in_flight or self._usd_boot_pending:
                # swap_fsm_start() returns -1 unless the FSM is SWAP_IDLE. The
                # v0.13 power-on load IS a swap (internal source), so a swap
                # issued while it runs gets this same refusal (net-protocol.md
                # "Power-on load").
                return {"ok": False, "err": "a swap is already in flight"}
            if self._commit_in_flight:
                # A parked v0.13 commit holds the store (and the push
                # transports): coordinator_handle_swap answers "store busy".
                return {"ok": False, "err": "store busy"}
            self._swap_in_flight = True
        try:
            return self._run_swap(rm, src)
        finally:
            with self._cond:
                self._swap_in_flight = False
                self._await_kind = None   # the gate closes with the swap
                self._icap_busy_until = 0.0
                self._cond.notify_all()

    def _run_swap(self, rm: str, src: str) -> Dict[str, Any]:
        # The real behaviour model (the same Python port of swap_fsm.c the
        # integration suite cross-checks against the C), against an idealized
        # instantly-confirming decoupler and a "hardware" RM_ID readback we
        # control (fault-injectable).
        regs = make_regs_confirmed()
        fsm = SwapFsm(regs, current_clearing=self._current_clearing,
                      running_static_id=self.static_id)
        fsm.current_rm_id = self.current_rm_id
        fsm.start(rm)

        # THE BUSY-ICAP WINDOW (opt-in, see the class docstring): the outgoing
        # clearing streams into the ICAP for a while BEFORE the FSM can await the
        # partial. The incoming clearing may land meanwhile (RAM staging), so the
        # gate opens for it now; a partial arriving before the stream ends is
        # refused (TFTP) or parked (6910) by _icap_gate().
        stream_s = self._outgoing_stream_s()
        if stream_s > 0:
            with self._cond:
                self._icap_busy_until = time.monotonic() + stream_s
                self._await_kind = BitstreamKind.CLEARING
                self._await_since = time.monotonic()
                self._cond.notify_all()
                while not self._stop.is_set():
                    remaining = self._icap_busy_until - time.monotonic()
                    if remaining <= 0:
                        break
                    self._cond.wait(remaining)
                self._icap_busy_until = 0.0
                self._await_since = time.monotonic()   # the AWAIT idle bound starts now
                self._cond.notify_all()

        # GATE -> DECOUPLE_ASSERT -> STREAM_CLEARING -> AWAIT_INCOMING_CLEARING.
        # This can already FAIL without a single byte being pushed: a shell image
        # with no greybox clearing seed fails closed at STREAM_CLEARING (I2). The
        # host is never invited to push in that case — which is the point of
        # running the FSM this far BEFORE arming the transports.
        _step_until(fsm, ("AWAIT_INCOMING_CLEARING", "FAILED"))
        if fsm.state != "FAILED" and stream_s <= 0:
            # ARM. From here until the pair is complete (or the idle timeout),
            # the push transports admit exactly what the FSM is awaiting.
            with self._cond:
                self._await_kind = BitstreamKind.CLEARING
                self._await_since = time.monotonic()
                self._cond.notify_all()
        if fsm.state != "FAILED":
            clearing = self._await_bitstream(BitstreamKind.CLEARING)
            if clearing is None:
                _fail_await_timeout(fsm)
            else:
                fsm.deliver_incoming_clearing(
                    rm_id=clearing.rm_id, len_words=clearing.len_words,
                    static_id=clearing.static_id, crc32=clearing.crc32,
                )
                _step_until(fsm, ("AWAIT_PARTIAL", "FAILED"))

        if fsm.state == "AWAIT_PARTIAL":
            partial = self._await_bitstream(BitstreamKind.PARTIAL)
            if partial is None:
                _fail_await_timeout(fsm)
            else:
                fsm.deliver_partial(
                    rm_id=partial.rm_id, len_words=partial.len_words,
                    static_id=partial.static_id, crc32=partial.crc32,
                )
                # What the "hardware" reads back at DFXCTL.RM_ID/RM_STATUS when
                # VERIFY looks (I25). Known only now — the target rm_id comes
                # from the PARTIAL's own header, not from the swap's rm name.
                readback = (
                    self.rm_id_readback_override
                    if self.rm_id_readback_override is not None
                    else partial.rm_id
                )
                regs.dfxctl_rm_id = readback
                regs.dfxctl_rm_status = (
                    DFXCTL_RM_STATUS_RM_ID_VALID if self.rm_id_valid else 0
                )
                fsm.run_to_completion()

        final = fsm.state
        with self._cond:
            # On DONE the FSM promoted the incoming clearing into the
            # current-clearing cache (net-protocol.md step 5); on FAILED it
            # left the old cache object untouched — either way this is the
            # coordinator's new resident reference.
            self._current_clearing = fsm.current_clearing
            self.swaps.append({
                "rm": rm, "src": src, "final": final,
                "history": list(fsm.history), "rm_id": fsm.current_rm_id,
            })
            if final == "DONE":
                self.current_rm_id = fsm.current_rm_id
                return {"ok": True, "rm_id": _hex32(self.current_rm_id), "verified": True}
        failed_at = fsm.history[-1] if fsm.history else "?"
        # Uniform failure shape (no rm_id/verified echo) — matches the
        # firmware even on the VERIFY-failure path, where net_proto.c still
        # emits only {"ok":false,"err":...}.
        return {
            "ok": False,
            "err": f"swap failed (at {failed_at}); RP left decoupled and held in reset",
        }

    def _outgoing_stream_s(self) -> float:
        """How long SWAP_STREAM_CLEARING streams the resident clearing into the
        ICAP (the busy-ICAP window); 0 when the model is off or nothing is
        resident (an invalid cache fails closed at once, as in the firmware)."""
        rate = self.clearing_stream_bytes_per_s
        ref = self._current_clearing
        if rate is None or not ref.valid or ref.len_words <= 0:
            return 0.0
        return ref.len_words * 4 / rate

    def _icap_gate(self, transport: str, header: BitstreamHeader) -> bool:
        """The busy-ICAP window at the point config_agent.c's ``begin_payload()``
        routes a PARTIAL to the ICAP-direct sink (after header validation, before
        the arming gate). ``True`` = go on to :meth:`_admit_push`.

        Inside the window a TFTP partial is REFUSED (recorded, ``False``: the
        caller sends ``ERROR 0 "rejected"``) -- TFTP has no window to hold it on.
        A 6910 partial is PARKED: this blocks, with the payload left unread in
        the socket, until the outgoing clearing has streamed, then returns
        ``True`` (config_agent.c RECV_PENDING_ICAP_BEGIN)."""
        if header.kind is not BitstreamKind.PARTIAL:
            return True
        with self._cond:
            if time.monotonic() >= self._icap_busy_until:
                return True
            if transport != "tcp":
                self.icap_busy_rejects += 1
                self._record_event(transport, NOT_AWAITING, header,
                                   detail="icap busy (SWAP_STREAM_CLEARING)")
                return False
            self.icap_defer_parks += 1
            while not self._stop.is_set():
                remaining = self._icap_busy_until - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            return True

    def _await_bitstream(self, kind: BitstreamKind) -> Optional[BitstreamInfo]:
        """Block until the config agent has staged a validated bitstream of
        ``kind``, or the swap's IDLE timeout expires (``None``).

        Idle, not total: while a transfer is actually in flight the deadline is
        pushed out, mirroring swap_fsm.c's "re-armed on every byte of RX
        progress" — so a multi-MB partial cannot time its own swap out.

        Does not touch :attr:`_await_kind`: the gate is advanced by whoever
        ACCEPTS a push (:meth:`_ca_finish`), atomically with the acceptance —
        see the attribute's own comment for why that must not be done here.
        """
        take = (
            self.config_agent.take_clearing
            if kind is BitstreamKind.CLEARING
            else self.config_agent.take_partial
        )
        with self._cond:
            while True:
                staged = take()
                if staged is not None:
                    return staged
                if self._active_transfers > 0:
                    # RX in progress: not idle. Re-arm and re-check shortly.
                    self._await_since = time.monotonic()
                    self._cond.wait(0.05)
                    continue
                idle = time.monotonic() - self._await_since
                remaining = self.swap_await_timeout - idle
                if remaining <= 0:
                    return None
                self._cond.wait(min(remaining, 0.05))

    @property
    def awaiting(self) -> Optional[BitstreamKind]:
        """The bitstream kind a parked swap is currently awaiting, or ``None``
        when the shell would reset any push that arrived right now."""
        with self._lock:
            return self._await_kind

    # ------------------------------------------------------------------ #
    # Bitstream receive bookkeeping (shared by TFTP + raw TCP)
    # ------------------------------------------------------------------ #

    def _transfer_begin(self) -> None:
        with self._cond:
            self._active_transfers += 1
            self._await_since = time.monotonic()   # RX progress re-arms the idle timeout
            self._cond.notify_all()

    def _transfer_end(self) -> None:
        with self._cond:
            self._active_transfers -= 1
            self._cond.notify_all()

    def _ca_validate_header(self, transport: str, header_bytes: bytes):
        with self._cond:
            status, header = self.config_agent.validate_header(header_bytes)
            if status is not ConfigAgentStatus.OK:
                self._record_event(transport, status, header)
            return status, header

    def _admit_push(self, transport: str, header: BitstreamHeader) -> bool:
        """THE ARMING GATE (see :attr:`_await_kind`). ``True`` iff a parked swap
        is awaiting a bitstream of exactly this kind.

        Called from the transports at the point config_agent.c's
        ``begin_payload()`` calls ``sink->begin()`` — i.e. AFTER the header has
        validated, so a bad magic/version/static_id/kind or an I2 ordering
        violation is still reported as *that* error. A refusal is recorded as a
        ``PushEvent`` (``ERR_NOT_AWAITING``) and the transport then resets the
        connection.

        A swap whose gate has not opened yet makes this **wait** a bounded grace
        (:attr:`swap_arm_grace`) rather than rejecting. That is not politeness,
        it is fidelity: the real shell is a single-threaded poll loop, so the
        ``swap`` line — which the correct client sends on 6900 *before* it opens
        the push transport — is already in the shell's receive buffer when the
        push arrives, and the shell processes it (decoupling, streaming the
        outgoing clearing to ICAP, reaching ``SWAP_AWAIT_INCOMING_CLEARING``)
        before it ever looks at the push. This server's listeners are separate
        THREADS with no such ordering, so a push can reach this gate before the
        control thread has even dispatched a swap that was sent first. Waiting a
        short grace for the gate to open absorbs that scheduling skew — a fake
        that rejects a correct client for a race the firmware cannot have is as
        wrong as one that accepts a broken one.

        It does NOT accept the wrong order. A genuine push-before-swap (the bug
        this whole fix exists to catch) sends no swap first, so nothing arms the
        gate within the grace and the push is rejected — exactly as on silicon.
        The tests that drive the wrong order push with the swap sent strictly
        *after* the push has completed, so no swap can sneak the gate open inside
        a push's grace window.

        Rejection is therefore reserved for what it means on the board: no swap
        arrived to open the gate (or one did, and this is not the bitstream it
        is awaiting).
        """
        def armed() -> bool:
            # A parked v0.13 commit admits 6910 ONLY (`src` is "tcp").
            return self._await_kind is header.kind and (
                self._await_transport is None or self._await_transport == transport)

        with self._cond:
            deadline = time.monotonic() + self.swap_arm_grace
            while not armed():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            if armed():
                return True
            self._record_event(transport, NOT_AWAITING, header)
            return False

    def wait_awaiting(self, kind: Optional[BitstreamKind], timeout: float = 5.0) -> bool:
        """Block until the arming gate reads ``kind`` (``None`` = shut). For
        tests that want to observe the gate rather than race it — ``swap_begin``
        only *sends*, so the shell has not necessarily armed when it returns."""
        with self._cond:
            deadline = time.monotonic() + timeout
            while self._await_kind is not kind:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True

    def _ca_finish(self, transport: str, header: BitstreamHeader, payload: bytes) -> ConfigAgentStatus:
        with self._cond:
            status = self.config_agent.finish_payload(header, payload)
            if status is ConfigAgentStatus.OK and self._commit_in_flight:
                # The commit writes these exact bytes to the card.
                self._commit_payloads[header.kind] = bytes(payload)
            if status is ConfigAgentStatus.OK and self._await_kind is header.kind:
                # Advance the gate ATOMICALLY with the acceptance, so the next
                # push in a single-threaded client's back-to-back sequence can
                # never lose a race against the parked swap's FSM stepping.
                self._await_kind = (
                    BitstreamKind.PARTIAL
                    if header.kind is BitstreamKind.CLEARING
                    else None   # pair complete: the swap takes it from here
                )
            self._record_event(transport, status, header)
            return status

    def _record_event(self, transport: str, status, header: Optional[BitstreamHeader], detail: str = "") -> None:
        # Caller holds the lock.
        name = status.name if isinstance(status, ConfigAgentStatus) else str(status)
        if detail:
            name = f"{name}:{detail}"
        self.push_events.append(PushEvent(
            transport=transport,
            ok=(status is ConfigAgentStatus.OK),
            status=name,
            info=BitstreamInfo.from_header(header) if header is not None else None,
        ))
        self._cond.notify_all()

    @property
    def accepted_pushes(self) -> List[PushEvent]:
        with self._lock:
            return [e for e in self.push_events if e.ok]

    @property
    def rejected_pushes(self) -> List[PushEvent]:
        with self._lock:
            return [e for e in self.push_events if not e.ok]

    @property
    def pair_ready(self) -> bool:
        with self._lock:
            return self.config_agent.pair_ready

    def wait_push_events(self, count: int, timeout: float = 5.0) -> bool:
        """Block until at least ``count`` push events (accepted or
        rejected) have been recorded. For tests using clients that don't
        synchronise on the transport themselves."""
        with self._cond:
            deadline = time.monotonic() + timeout
            while len(self.push_events) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True

    # ------------------------------------------------------------------ #
    # TFTP server (UDP; RFC1350 WRQ + octet receive only)
    # ------------------------------------------------------------------ #

    def _tftp_serve(self) -> None:
        sock = self._tftp_sock
        while not self._stop.is_set():
            try:
                packet, client = sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(packet) < 2:
                continue
            opcode = int.from_bytes(packet[:2], "big")
            fname = packet[2:].split(b"\0", 1)[0]
            if opcode == _OP_WRQ and self.mode == "rescue":
                # the BOUND handler, so a subclass / an instance (rescue_sink=) can
                # stand in for stage0's verifier
                self._tftp_receive_file(client, packet, lambda _s, data: self._rescue_sink(data))
            elif opcode == _OP_WRQ and fname == TOFU_FILENAME.encode() and self.tofu_enabled:
                # The TOFU first-key claim (plan §10; HARNESSD_CONTRACT §9.3):
                # routed away from the bitstream path, no MPS3 header, taken
                # only while unclaimed -- afterwards TFTP ERROR 2.
                if self.ssh_claimed:
                    self._tftp_send_error_from(None, client, 2, "access violation: already claimed")
                else:
                    self._tftp_receive_file(client, packet, _tofu_sink)
            elif opcode == _OP_WRQ:
                # One transfer at a time, handled inline — mirrors the
                # firmware's single receive session (config_agent.c).
                self._transfer_begin()
                try:
                    self._tftp_receive(client, packet)
                finally:
                    self._transfer_end()
            elif opcode == _OP_RRQ and self.mode == "rescue" and fname == b"stage0.status":
                self._tftp_send_small(client, self.rescue_status_block())
            elif opcode == _OP_RRQ:
                self._tftp_send_error_from(None, client, 4, "reads not supported (push-only server)")
            # anything else on the main socket: ignore (stale/foreign TID)

    def _tftp_send_error_from(self, sock: Optional[socket.socket], addr, code: int, message: str) -> None:
        # RFC1350's ErrMsg is netascii. "replace" (not strict) so a stray
        # non-ASCII character in a diagnostic can never take the TFTP server
        # thread down with a UnicodeEncodeError instead of answering the client.
        packet = struct.pack(">HH", _OP_ERROR, code) + message.encode("ascii", "replace") + b"\0"
        if sock is None:
            temp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                temp.sendto(packet, addr)
            finally:
                temp.close()
        else:
            sock.sendto(packet, addr)

    def _tftp_receive(self, client, wrq_packet: bytes) -> None:
        parts = wrq_packet[2:].split(b"\0")
        if len(parts) < 2:
            self._tftp_send_error_from(None, client, 4, "malformed WRQ")
            with self._cond:
                self._record_event("tftp", "MALFORMED_WRQ", None)
            return
        mode = parts[1]
        if mode.lower() != b"octet":
            # net-protocol.md's payload is binary; netascii would corrupt it.
            self._tftp_send_error_from(None, client, 0, "only octet mode is supported")
            with self._cond:
                self._record_event("tftp", "NON_OCTET_MODE", None)
            return

        # RFC1350: the server answers from a NEW TID (ephemeral port); the
        # client must address all further packets to that TID.
        tsock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            tsock.bind((self.host, 0))
            tsock.settimeout(self.transfer_timeout)
            tsock.sendto(struct.pack(">HH", _OP_ACK, 0), client)

            buf = bytearray()
            header: Optional[BitstreamHeader] = None
            expected_block = 1
            while True:
                try:
                    packet, addr = tsock.recvfrom(65536)
                except socket.timeout:
                    with self._cond:
                        self._record_event("tftp", ConfigAgentStatus.ERR_TRUNCATED, header,
                                           detail="timed out mid-transfer")
                    return
                if addr != client or len(packet) < 4:
                    continue
                opcode = int.from_bytes(packet[:2], "big")
                if opcode == _OP_ERROR:
                    with self._cond:
                        self._record_event("tftp", ConfigAgentStatus.ERR_TRUNCATED, header,
                                           detail="client aborted with TFTP ERROR")
                    return
                if opcode != _OP_DATA:
                    self._tftp_send_error_from(tsock, client, 4, "expected DATA")
                    return
                block = int.from_bytes(packet[2:4], "big")
                if block == (expected_block - 1) & 0xFFFF:
                    # Retransmitted block (our ACK was lost) — re-ACK, drop.
                    tsock.sendto(struct.pack(">HH", _OP_ACK, block), client)
                    continue
                if block != expected_block & 0xFFFF:
                    self._tftp_send_error_from(tsock, client, 4, f"unexpected block {block}")
                    return
                data = packet[4:]
                buf.extend(data)

                if (header is None and self.slots is not None and len(buf) >= HEADER_SIZE
                        and bytes(buf[:4]) == MAGIC and buf[6] == SLOT_IMAGE_KIND):
                    self._slot_push_tftp(tsock, client, buf, block, len(data) < _TFTP_BLOCK)
                    return
                if header is None and len(buf) >= HEADER_SIZE:
                    status, header = self._ca_validate_header("tftp", bytes(buf[:HEADER_SIZE]))
                    if status is not ConfigAgentStatus.OK:
                        # Reject BEFORE accepting more payload ("before any
                        # ICAP write") — TFTP ERROR aborts the transfer.
                        self._tftp_send_error_from(tsock, client, 0, f"rejected: {status.name}")
                        return
                    if not self._icap_gate("tftp", header):
                        # The busy-ICAP window: config_agent.c's TFTP path has no
                        # window to park on, so begin() fails and the transfer is
                        # refused -- the firmware's exact text is "rejected".
                        self._tftp_send_error_from(
                            tsock, client, 0,
                            "rejected: ICAP busy streaming the outgoing clearing "
                            "(SWAP_STREAM_CLEARING); TFTP cannot park a partial",
                        )
                        return
                    if not self._admit_push("tftp", header):
                        # No swap is awaiting this bitstream (the arming gate —
                        # see the module docstring). TFTP's ERROR packet is this
                        # transport's equivalent of the raw-TCP RST, and unlike
                        # the RST it is unambiguous on the wire, so the pusher
                        # raises rather than silently "succeeding".
                        self._tftp_send_error_from(
                            tsock, client, 0,
                            f"rejected: {NOT_AWAITING} (the shell only accepts a "
                            "bitstream while a swap is awaiting one: issue the "
                            "'swap' RPC FIRST, then push into it)",
                        )
                        return

                final = len(data) < _TFTP_BLOCK
                if final:
                    if header is None:
                        # Transfer too short to even carry a header.
                        with self._cond:
                            status, header = self.config_agent.validate_header(bytes(buf))
                            self._record_event("tftp", status, header)
                        self._tftp_send_error_from(tsock, client, 0, f"rejected: {status.name}")
                        return
                    status = self._ca_finish("tftp", header, bytes(buf[HEADER_SIZE:]))
                    if status is not ConfigAgentStatus.OK:
                        # ERROR instead of the final ACK: the client learns
                        # the push was refused (torn/CRC/size).
                        self._tftp_send_error_from(tsock, client, 0, f"rejected: {status.name}")
                        return
                    tsock.sendto(struct.pack(">HH", _OP_ACK, block), client)
                    return
                tsock.sendto(struct.pack(">HH", _OP_ACK, block), client)
                expected_block = (expected_block + 1) & 0xFFFF
        finally:
            tsock.close()


    # ------------------------------------------------------------------ #
    # v0.14 kind-2 slot-image pushes (slots=): 6910 and TFTP
    # ------------------------------------------------------------------ #

    def _slot_push_tcp(self, sock: socket.socket, header: bytes, peer: str) -> None:
        """config_agent -> mps3_cfg_slot_sink() on 6910. A push refused before the
        provider (bad header, a running job, THE LOCK) or by it is closed unread;
        its bytes are drained first only so the client's send completes."""
        assert self.slots is not None
        with self._lock:
            verdict, slot, total = self.slots.push_begin(header, peer)
        if verdict != "go":
            _recv_exactly(sock, total + 1)
            return
        payload = _recv_exactly(sock, total)
        with self._lock:
            self.slots.push_end(slot, header, payload)

    def _slot_push_tftp(self, tsock: socket.socket, client, buf: bytearray, block: int,
                        final: bool) -> None:
        """The same push over TFTP 69: refused at the first DATA block (THE LOCK:
        ERROR 2 ``access violation``); the final ACK only when the image passed
        stage0's table rules and its region CRCs (the read-back then runs as the
        job), else an ERROR carrying the job's reason."""
        assert self.slots is not None
        header = bytes(buf[:HEADER_SIZE])
        with self._lock:
            verdict, slot, total = self.slots.push_begin(header, client[0])
            locked = verdict == "unread" and self.slots.locked(client[0])
        if verdict != "go":
            if locked:
                self._tftp_send_error_from(tsock, client, 2, "access violation: " + SLOT_LOCKED_ERR)
            else:
                self._tftp_send_error_from(tsock, client, 0, "rejected")
            return
        expected = (block + 1) & 0xFFFF
        while not final:
            tsock.sendto(struct.pack(">HH", _OP_ACK, (expected - 1) & 0xFFFF), client)
            try:
                packet, addr = tsock.recvfrom(65536)
            except socket.timeout:
                break                                     # torn: the job says so below
            if addr != client or len(packet) < 4:
                continue
            opcode, blk = struct.unpack(">HH", packet[:4])
            if opcode == _OP_ERROR:
                break
            if opcode != _OP_DATA or blk == (expected - 1) & 0xFFFF:
                continue                                  # a retransmit: re-ACKed above
            if blk != expected:
                self._tftp_send_error_from(tsock, client, 4, f"unexpected block {blk}")
                break
            data = packet[4:]
            buf.extend(data)
            final = len(data) < _TFTP_BLOCK
            expected = (expected + 1) & 0xFFFF
        with self._lock:
            why = self.slots.push_end(slot, header, bytes(buf[HEADER_SIZE:HEADER_SIZE + total]))
        if why:
            self._tftp_send_error_from(tsock, client, 0, f"rejected: {why}")
        elif final:
            tsock.sendto(struct.pack(">HH", _OP_ACK, (expected - 1) & 0xFFFF), client)


# --------------------------------------------------------------------------- #
# Linux-profile / rescue-mode services (bound onto FakeShell below)
# --------------------------------------------------------------------------- #

#: TOFU cap: the claim is an authorized_keys file, not a payload channel.
_TOFU_MAX_BYTES = 16 * 1024


def _authorized_key_fingerprint(data: Optional[bytes]) -> str:
    """harnessd's sshfp.c harnessd_authorized_key_fingerprint(), independently: the
    FIRST line holding ``<type> <base64>`` whose blob starts with that type ->
    OpenSSH's ``SHA256:<base64 of sha256(blob), no padding>``; "" when none."""
    import base64
    import binascii
    import hashlib
    for line in (data or b"").decode("latin-1").splitlines():
        tok = line.split()
        if not tok or tok[0].startswith("#"):
            continue
        for kind, b64 in zip(tok, tok[1:]):
            try:
                blob = base64.b64decode(b64, validate=True)
            except (binascii.Error, ValueError):
                continue
            if len(blob) >= 4:
                n = struct.unpack(">I", blob[:4])[0]
                if blob[4:4 + n] == kind.encode("latin-1") and n == len(kind):
                    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
                    return "SHA256:" + digest
    return ""


def _tofu_sink(self: "FakeShell", data: bytes) -> Tuple[bool, str]:
    if len(data) > _TOFU_MAX_BYTES:
        return False, "authorized_keys larger than 16 KiB"
    if not data.strip():
        return False, "empty authorized_keys"
    with self._lock:
        if self.ssh_claimed:               # a claim raced in while we received
            return False, "access violation: already claimed"
        self.authorized_keys = bytes(data)
        self.ssh_claimed = True
    return True, ""


def _rescue_sink(self: "FakeShell", data: bytes) -> Tuple[bool, str]:
    with self._lock:
        self.rescue_pushes.append(bytes(data))
    return True, ""


def _tftp_receive_file(self: "FakeShell", client, wrq_packet: bytes, sink) -> None:
    """Receive one whole file (octet, RFC1350 512-byte blocks) and hand it to
    ``sink(self, data) -> (ok, err)``. The FINAL ACK is sent only when the sink
    accepts -- the verdict is in-band, as both config_agent's named-file hook
    and stage0's rescue server do it."""
    parts = wrq_packet[2:].split(b"\0")
    if len(parts) < 2 or parts[1].lower() != b"octet":
        self._tftp_send_error_from(None, client, 4, "octet WRQ required")
        return
    tsock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        tsock.bind((self.host, 0))
        tsock.settimeout(self.transfer_timeout)
        tsock.sendto(struct.pack(">HH", _OP_ACK, 0), client)
        buf = bytearray()
        expected = 1
        while True:
            try:
                packet, addr = tsock.recvfrom(65536)
            except socket.timeout:
                return
            if addr != client or len(packet) < 4:
                continue
            opcode = int.from_bytes(packet[:2], "big")
            if opcode != _OP_DATA:
                return
            block = int.from_bytes(packet[2:4], "big")
            if block == (expected - 1) & 0xFFFF:
                tsock.sendto(struct.pack(">HH", _OP_ACK, block), client)
                continue
            if block != expected & 0xFFFF:
                self._tftp_send_error_from(tsock, client, 4, f"unexpected block {block}")
                return
            data = packet[4:]
            buf.extend(data)
            if len(buf) > 64 * 1024 * 1024:
                self._tftp_send_error_from(tsock, client, 3, "too large")
                return
            if len(data) < _TFTP_BLOCK:
                ok, err = sink(self, bytes(buf))
                if not ok:
                    code = 2 if "access violation" in err else 0
                    self._tftp_send_error_from(tsock, client, code, err)
                    return
                tsock.sendto(struct.pack(">HH", _OP_ACK, block), client)
                return
            tsock.sendto(struct.pack(">HH", _OP_ACK, block), client)
            expected = (expected + 1) & 0xFFFF
    finally:
        tsock.close()


def _tftp_send_small(self: "FakeShell", client, payload: bytes) -> None:
    """Serve a small RRQ (< 512 B, one DATA block) -- ``stage0.status``."""
    tsock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        tsock.bind((self.host, 0))
        tsock.settimeout(self.transfer_timeout)
        tsock.sendto(struct.pack(">HH", _OP_DATA, 1) + payload[:_TFTP_BLOCK - 1], client)
        try:
            tsock.recvfrom(64)           # the client's ACK(1); nothing to do with it
        except socket.timeout:
            pass
    finally:
        tsock.close()


def _rescue_fields_check(fields: Dict[str, int]) -> None:
    """Every rescue_status name must be a stage0 status field (a typo would
    otherwise be silently dropped from the block)."""
    from ..mailbox import STAGE0_STATUS_FIELDS
    known = dict(STAGE0_STATUS_FIELDS)
    bad = [k for k in fields if k not in known]
    if bad:
        raise ValueError(f"unknown stage0 status fields {bad!r} (known: {sorted(known)})")


def _rescue_status_block(self: "FakeShell", **fields: int) -> bytes:
    """A 256-byte stage0 status block in rescue, placed by NAME through the
    layout pyverify.mailbox decodes with (STAGE0's stage0_status.h), so it
    follows STAGE0's layout instead of restating its offsets. ``self.rescue_status``
    and then ``fields`` (both by field name) override the defaults."""
    from ..mailbox import (STAGE0_STATUS_BYTES, STAGE0_STATUS_FIELDS,
                           STAGE0_STATUS_MAGIC, STAGE0_STATUS_VERSION)
    offs = dict(STAGE0_STATUS_FIELDS)
    vals = {"magic": STAGE0_STATUS_MAGIC, "version": STAGE0_STATUS_VERSION,
            "size": STAGE0_STATUS_BYTES, "boot_count": 1, "phase": 5,   # RESCUE
            "rescue_state": 1,                                          # LISTEN
            "rescue_sessions": len(self.rescue_pushes),
            "magic_end": STAGE0_STATUS_MAGIC}
    _rescue_fields_check(self.rescue_status)
    _rescue_fields_check(fields)
    vals.update(self.rescue_status)
    vals.update(fields)
    words = [0] * (STAGE0_STATUS_BYTES // 4)
    for name, v in vals.items():
        if name in offs:
            words[offs[name] // 4] = v & 0xFFFFFFFF
    return struct.pack("<%dI" % len(words), *words)


def _identify_reply(self: "FakeShell", nonce: str) -> Dict[str, Any]:
    """The identify reply.

    RUN mode (the harness engine): HARNESSD_CONTRACT §9.2's key order -- ok, op,
    v, nonce, board, mac, ip, dhcp, shell_id, rm_id, harness, proto, [impl],
    [unit], up_ms, [ssh], ports -- plus the two socharness additions, ``mode``
    (after ``board``, where STAGE0 already puts it) and ``os_up_ms`` (after
    ``up_ms``). Those two positions are PROVISIONAL until net-protocol.md's
    identify section fixes them.

    RESCUE mode (stage0): STAGE0_CONTRACT §7's shape exactly -- ok, op, v,
    nonce, board, mode, shell_id, ip, mac, reason, ports{tftp}. No impl, no ssh:
    stage0 is not the harness engine.
    """
    up = self.up_ms()
    if self.mode == "rescue":
        return {"ok": True, "op": "identify", "v": 1, "nonce": nonce,
                "board": self.board, "mode": "rescue",
                "shell_id": _hex32(self.static_id), "ip": self.board_ip,
                "mac": self.mac, "reason": self.rescue_reason,
                "ports": {"tftp": self.tftp_port}}
    r: Dict[str, Any] = {
        "ok": True, "op": "identify", "v": 1, "nonce": nonce,
        "board": self.board, "mode": "run", "mac": self.mac, "ip": self.board_ip,
        "dhcp": self.dhcp, "shell_id": _hex32(self.static_id),
        "rm_id": _hex32(self.current_rm_id), "harness": self.harness_version,
        "proto": "0.11",
    }
    if self.impl:
        r["impl"] = self.impl
    if self.unit:
        r["unit"] = self.unit
    r["up_ms"] = up
    if self.impl:
        r["os_up_ms"] = (self.os_boot_ms + up) & 0xFFFFFFFF
        r["ssh"] = {"claimed": bool(self.ssh_claimed),
                    "host_key_sha256": self.ssh_host_key_sha256,
                    # HM_ANSWERS C1: the claim's first key ("" unclaimed / unparsable)
                    "key_sha256": (_authorized_key_fingerprint(self.authorized_keys)
                                   if self.ssh_claimed else "")}
    if self.identity is not None:
        # v0.16: the board's resolved label, after ssh, before ports (still last)
        r["label"] = self.identity.running["label"]
    r["ports"] = {"ctrl": self.control_port, "push": self.raw_tcp_port,
                  "tftp": self.tftp_port, "jtag": 6921, "xvc": 2542,
                  "uart0": self.uart0_port, "uart1": self.uart1_port,
                  "swo": self.swo_port}
    return r


_HEX = frozenset("0123456789abcdefABCDEF")


def _identify_serve(self: "FakeShell") -> None:
    """UDP 6899: one datagram in, one out, to the SENDER's addr:port.
    Malformed requests are SILENT (HARNESSD_CONTRACT §9.2), and replies are
    rate-limited to ``identify_rate_per_s`` (token bucket, burst = rate)."""
    sock = self._identify_sock
    tokens = self.identify_rate_per_s
    last = time.monotonic()
    while not self._stop.is_set():
        try:
            data, addr = sock.recvfrom(2048)
        except socket.timeout:
            continue
        except OSError:
            break
        req: Optional[Dict[str, Any]] = None
        try:
            obj = json.loads(data.decode("utf-8"))
            if (isinstance(obj, dict) and obj.get("op") == "identify" and obj.get("v") == 1
                    and isinstance(obj.get("nonce"), str) and 8 <= len(obj["nonce"]) <= 32
                    and set(obj["nonce"]) <= _HEX):
                req = obj
        except (ValueError, UnicodeDecodeError):
            req = None
        now = time.monotonic()
        tokens = min(self.identify_rate_per_s, tokens + (now - last) * self.identify_rate_per_s)
        last = now
        replied = False
        # identify is a harnessd SERVICE MODULE: a hung harnessd answers it no
        # more than it answers 6900 (hung mode is silent here too).
        if req is not None and tokens >= 1.0 and not self.hung:
            tokens -= 1.0
            payload = json.dumps(_identify_reply(self, req["nonce"]),
                                 separators=(",", ":")).encode("ascii")
            try:
                sock.sendto(payload[:1200], addr)
                replied = True
            except OSError:
                pass
        with self._lock:
            self.identify_requests.append((addr, req, replied))


FakeShell._tofu_sink = _tofu_sink                      # type: ignore[attr-defined]
FakeShell._rescue_sink = _rescue_sink                  # type: ignore[attr-defined]
FakeShell._tftp_receive_file = _tftp_receive_file      # type: ignore[attr-defined]
FakeShell._tftp_send_small = _tftp_send_small          # type: ignore[attr-defined]
FakeShell.rescue_status_block = _rescue_status_block   # type: ignore[attr-defined]
FakeShell.identify_reply = _identify_reply             # type: ignore[attr-defined]
FakeShell._identify_serve = _identify_serve            # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Reference push clients (test-side counterparts of the two transports).
# The production pusher (W-PUSH) is the real client; these exist so the fake
# shell's own test suite has zero dependency on that concurrent workstream.
# --------------------------------------------------------------------------- #


class TftpError(Exception):
    """The server answered a TFTP transfer with an ERROR packet."""


def tftp_put(host: str, port: int, data: bytes, *, filename: str = "bitstream.bin",
             timeout: float = 5.0) -> None:
    """Minimal RFC1350 TFTP PUT (octet mode). Raises :class:`TftpError` if
    the server rejects the transfer (including the fake shell's
    header/CRC/ordering rejections). Returns only once the server has
    ACKed the final block — i.e. the push is fully validated server-side.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        wrq = struct.pack(">H", _OP_WRQ) + filename.encode("ascii") + b"\0octet\0"
        sock.sendto(wrq, (host, port))

        def expect_ack(block: int, server_addr=None):
            while True:
                packet, addr = sock.recvfrom(65536)
                if server_addr is not None and addr != server_addr:
                    continue
                opcode = int.from_bytes(packet[:2], "big")
                if opcode == _OP_ERROR:
                    code = int.from_bytes(packet[2:4], "big")
                    message = packet[4:].split(b"\0", 1)[0].decode("ascii", "replace")
                    raise TftpError(f"server error {code}: {message}")
                if opcode == _OP_ACK and int.from_bytes(packet[2:4], "big") == block & 0xFFFF:
                    return addr
                # stale/duplicate ACK — keep waiting

        # RFC1350: the first response fixes the server's transfer TID.
        server_addr = expect_ack(0)
        block = 1
        offset = 0
        while True:
            chunk = data[offset:offset + _TFTP_BLOCK]
            sock.sendto(struct.pack(">HH", _OP_DATA, block & 0xFFFF) + chunk, server_addr)
            expect_ack(block, server_addr)
            offset += len(chunk)
            if len(chunk) < _TFTP_BLOCK:
                return
            block += 1
    finally:
        sock.close()


def raw_tcp_put(host: str, port: int, frame: bytes, *, timeout: float = 5.0) -> None:
    """Push one framed bitstream over the raw-TCP transport (6910): send,
    half-close, then wait for the server to close — with the fake shell,
    EOF means validation has finished and the push state is observable
    (see the "no acknowledgement" ambiguity note in the module docstring;
    unlike :func:`tftp_put` there is no accept/reject signal on the wire).

    A server-side *rejection* races with the send: when the fake shell
    refuses the header it returns without draining the payload, so its
    ``close()`` of a socket with unread bytes emits a TCP RST. Depending
    on exactly when that RST lands relative to this client's syscalls,
    ``sendall``/``shutdown``/``recv`` raise ``ECONNRESET``/``EPIPE``/
    ``ENOTCONN`` instead of delivering a clean EOF (this was a real,
    test-order-dependent flake). Any :class:`OSError` after the
    connection is established therefore means the same thing as EOF: the
    server has finished with this transfer — and since the fake shell
    always records the :class:`PushEvent` *before* its handler returns
    (and the RST can only be generated by the close that follows), the
    push outcome is already observable when we swallow the error.
    """
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        sock.settimeout(timeout)
        try:
            sock.sendall(frame)
            sock.shutdown(socket.SHUT_WR)
            while sock.recv(4096):
                pass
        except OSError:
            pass  # reset by a rejecting server: as synchronised as EOF (above)
    finally:
        sock.close()
