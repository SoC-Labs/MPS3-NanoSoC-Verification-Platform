"""CSR / JTAG register backend over ``xsdb`` + ``hw_server`` — the
board-free Python port of ``scripts/mps3_diag.tcl``.

This is the :class:`~socket_harness.session.RegisterBackend` the whole CSR
facade (:mod:`socket_harness.registers`) rides on: it turns a register
read/write into an ``xsdb -eval <tcl>`` invocation, runs it through the
same injectable *runner* seam as :mod:`pyverify.debug`
(:data:`pyverify.debug.SubprocessRunner`), and parses the value back with
PURE, mutation-verifiable functions (:func:`parse_mrd_value`,
:func:`parse_mrd_block` — the same shape as :func:`pyverify.swd.parse_mdw`).

It is the concrete DFXCTL / diag JTAG twin of ``ShellClient.diag()``, and
the ONLY path readable DURING a partial reconfiguration (when the 6900
control channel is parked): ``mrd`` works on a *running* MicroBlaze via the
MDM. Three traps from ``scripts/mps3_diag.tcl`` are honoured here:

* **Never ``stop`` / ``con``.** Halting the core mid-swap resets the TCP
  stream and corrupts the transfer (we have destroyed a 1.31 MB stream that
  way). :meth:`XsdbRegisterEndpoint.write_word` emits a bare ``mwr`` and the
  scan uses ``targets``/``mrd`` only — nothing that halts the core.
* **FQDN hub URL.** ``tcp:<hub-fqdn>:3121`` — the bare
  host fails to resolve for ``hw_server`` (``mps3_diag.tcl:21``).
* **ASCENDING candidate scan.** The LMB address decode ALIASES, so a
  "newest-first" scan reports the wrong base on every old shell. The
  candidate bases are scanned low-to-high (:attr:`XsdbConfig.candidates`),
  across BOTH struct sizes: the mailbox is anchored at (LMB end - sizeof),
  so v8's 256-byte struct sits at ``…F00`` while the v5..v7 128-byte struct
  a fielded board is still running sits at ``…F80``. How many words to read
  follows from WHICH anchor hit, never from an assumed layout — see
  :meth:`DiagMailbox.read`.

No I/O happens at import time; every parse/encode helper is pure.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Optional

from pyverify.debug import SubprocessRunner, default_runner

from .endpoints import Address, DEFAULT_HUB_URL
from .session import RegisterBackend

__all__ = [
    "XsdbError",
    "TargetNotFound",
    "XsdbConfig",
    "XsdbRegisterEndpoint",
    "DiagMailbox",
    "parse_mrd_value",
    "parse_mrd_block",
]


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class XsdbError(RuntimeError):
    """An ``xsdb`` op failed or produced unparseable output."""


class TargetNotFound(XsdbError):
    """No MicroBlaze target carried the diag magic at any candidate base.

    Raised instead of silently returning a plausible zero — a wedged or
    unloaded shell must be reported, never faked (the honest-probe
    discipline shared with ``pyverify.edge``).
    """


# --------------------------------------------------------------------------- #
# Diag mailbox layout (firmware/common/diag.h v6, via scripts/mps3_diag.tcl).
# Word offset -> field name. Offsets are STABLE / append-only so a JTAG reader
# can hard-code them; the BASE is not (top-anchored to the LMB end).
# --------------------------------------------------------------------------- #
DIAG_MAGIC: int = 0xD1A6C0DE

# BEGIN GENERATED[diag-fields] — gen_diag.py — DO NOT EDIT BY HAND
#: ``(word_offset, field_name)`` for EVERY word the mailbox defines except the
#: ``reserved[]`` pad — the same list ``scripts/mps3_diag.tcl``'s ``FIELDS`` gets,
#: from the same source (``MPS3_DIAG_FIELDS`` in ``firmware/common/diag.h``).
#: Word 0 (``magic``) is ALSO decoded separately by :meth:`DiagMailbox.read`.
DIAG_FIELDS: tuple[tuple[int, str], ...] = (
    (0, "magic"),
    (1, "version"),
    (2, "rx_recover_events"),
    (3, "rx_recover_dumps"),
    (4, "rx_drop_frames"),
    (5, "icap_bytes"),
    (6, "rx_payload_got"),
    (7, "rx_payload_expect"),
    (8, "tcp_rcv_wnd"),
    (9, "tcp_rcv_ann_wnd"),
    (10, "rx_queued"),
    (11, "pbuf_free"),
    (12, "win_windows_drained"),
    (13, "win_grant_send_fails"),
    (14, "tcp_sndbuf"),
    (15, "tcp_snd_wnd"),
    (16, "tx_frames_sent"),
    (17, "tx_status_drained"),
    (18, "tx_fifo_full_drops"),
    (19, "tx_errors"),
    (20, "tx_space_stalls"),
    (21, "tx_iface_errors"),
    (22, "tx_last_status"),
    (23, "icap_sr_last"),
    (24, "icap_eos_status"),
    (25, "ovlstore_phase"),
    (26, "ovlstore_detail"),
    (27, "touch_probe_regs"),
    (28, "touch_probe_adc_x"),
    (29, "touch_probe_adc_y"),
    (30, "touch_probe_verdict"),
    (31, "svc_count"),
    (32, "svc_pass_max_us"),
    (33, "svc_worst_us"),
    (34, "svc_worst_ix"),
    (35, "svc_overrun_events"),
    (36, "svc_skip_events"),
    (37, "svc_skipped_mask"),
    (38, "svc_max_us_0"),
    (39, "svc_max_us_1"),
    (40, "svc_max_us_2"),
    (41, "svc_max_us_3"),
    (42, "svc_max_us_4"),
    (43, "svc_max_us_5"),
    (44, "svc_max_us_6"),
    (45, "usd_boot"),
)
# END GENERATED[diag-fields]

#: OVL_PHASE_* name indexed by phase value (overlay_store.h enum order) — used
#: to decode word 25 (ovlstore_phase) into the human-readable QSPI step so an
#: operator sees WHERE a QSPI wedge stuck, not a bare integer.
OVL_PHASES: tuple[str, ...] = (
    "IDLE",
    "INIT",
    "READ_HDR",
    "BP_UNLOCK",
    "ERASE_SECTOR",
    "PROGRAM_PAGE",
    "WAIT_READY",
    "CRC",
    "PROMOTE",
    "STREAM",
    "REJECT",
)


# --------------------------------------------------------------------------- #
# Pure parse helpers (board-free, mutation-verifiable — the same role as
# pyverify.swd.parse_mdw for OpenOCD output).
# --------------------------------------------------------------------------- #
def _data_words(text: str) -> "list[int]":
    """Flatten every hex *data* word in ``text`` into address order.

    Handles both xsdb output shapes:

    * ``mrd -value -force <addr> N`` prints bare values
      (``00000001`` / ``0x00000001``, possibly several per line).
    * plain ``mrd <addr> N`` prints ``AABBCCDD:   00000001 00000002 ...``.

    An ``ADDR:`` label (leading hex token immediately followed by ``:``) is
    dropped so only genuine data words survive.
    """
    words: "list[int]" = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Strip a leading "ADDR:" (or "0xADDR:") memory-address label.
        m = re.match(r"^(?:0[xX])?[0-9A-Fa-f]+:\s*(.*)$", line)
        rest = m.group(1) if m else line
        for tok in rest.replace(",", " ").split():
            t = tok[2:] if tok[:2].lower() == "0x" else tok
            if re.fullmatch(r"[0-9A-Fa-f]{1,8}", t):
                words.append(int(t, 16))
    return words


def parse_mrd_value(text: str) -> int:
    """Return the single 32-bit value from an ``mrd ... 1`` read.

    Takes the LAST hex data token (the value trails any ``ADDR:`` label and
    any connect banner), accepting both the bare-hex ``-value`` form and the
    ``AABBCCDD:   00000001`` plain form. Raises :class:`XsdbError` if the
    output carries no hex value (read failed / target unreachable).
    """
    words = _data_words(text)
    if not words:
        raise XsdbError(
            "no hex value in xsdb 'mrd' output (read failed / no target):\n"
            + text.strip()
        )
    return words[-1]


def parse_mrd_block(text: str, nwords: int) -> "list[int]":
    """Return ``nwords`` 32-bit values from an ``mrd ... N`` block read.

    Flattens all hex data tokens in address order (dropping ``ADDR:``
    labels) and returns the last ``nwords`` of them — trailing the read past
    any connect banner. Raises :class:`XsdbError` if fewer than ``nwords``
    data words are present.
    """
    words = _data_words(text)
    if len(words) < nwords:
        raise XsdbError(
            f"expected >= {nwords} hex words in xsdb 'mrd' output, got "
            f"{len(words)}:\n" + text.strip()
        )
    return words[-nwords:]


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class XsdbConfig:
    """Where/how to reach ``hw_server`` and locate the diag mailbox.

    ``hub_url`` MUST be the FQDN form (``mps3_diag.tcl:21``): the bare host
    fails to resolve. ``candidates`` are scanned ASCENDING — the LMB decode
    aliases, so newest-first reports the wrong base on an old shell
    (``mps3_diag.tcl:23-30``).
    """

    hub_url: str = DEFAULT_HUB_URL
    xsdb: str = "xsdb"
    magic: str = "D1A6C0DE"
    #: mailbox bases, ASCENDING (256 KB / 512 KB / 1 MB LMB, each in its v8
    #: 256-byte and its v5..v7 128-byte anchor). Order matters twice over: the
    #: LMB decode aliases, AND on a 256 KB v8 image ``0x0003FF80`` — where a v7
    #: image kept its MAGIC — is an ordinary counter. Ascending is the only
    #: order that is right in every combination.
    candidates: tuple[int, ...] = (
        0x0003FF00, 0x0003FF80,
        0x0007FF00, 0x0007FF80,
        0x000FFF00, 0x000FFF80,
    )
    timeout_s: float = 30.0

    @classmethod
    def from_address(cls, addr: Address, **overrides: object) -> "XsdbConfig":
        """Build a config from a resolved :class:`~socket_harness.endpoints.Address`
        (its ``hub_url``), falling back to the FQDN default when unset."""
        hub = addr.hub_url or DEFAULT_HUB_URL
        return cls(hub_url=hub, **overrides)  # type: ignore[arg-type]

    @property
    def magic_int(self) -> int:
        """The diag magic as an integer (``0xD1A6C0DE``)."""
        return int(self.magic, 16)


# --------------------------------------------------------------------------- #
# The register backend
# --------------------------------------------------------------------------- #
def _fmt(value: int) -> str:
    """32-bit hex literal in xsdb form, e.g. ``0x44A10010``."""
    return f"0x{value & 0xFFFFFFFF:08X}"


class XsdbRegisterEndpoint(RegisterBackend):
    """A :class:`~socket_harness.session.RegisterBackend` implemented over
    ``xsdb`` batch invocations.

    Each op builds a tiny Tcl script, wraps it as ``xsdb -eval <tcl>`` (see
    :meth:`_argv`), and hands it to ``runner`` (default
    :func:`pyverify.debug.default_runner` — a real ``subprocess.run``).
    ``dry_run=True`` makes every op return the argv instead of running, for a
    board-free preview (mirrors ``pyverify.swd.SwdDebugger``).
    """

    def __init__(
        self,
        cfg: XsdbConfig = XsdbConfig(),
        *,
        runner: Optional[SubprocessRunner] = None,
        dry_run: bool = False,
    ) -> None:
        self.cfg = cfg
        self.runner = runner
        self.dry_run = dry_run

    # -- tcl / argv construction (pure) ------------------------------------- #
    def _connect_tcl(self) -> str:
        """The ``connect -url <hub>`` preamble (FQDN hub URL required)."""
        return f"connect -url {self.cfg.hub_url}"

    def _read_tcl(self, addr: int, nwords: int) -> str:
        """``connect ...; puts [mrd -value -force <addr> <n>]`` — the read
        script. ``-value`` prints bare values, ``-force`` reads a RUNNING
        core via the MDM (no ``stop``)."""
        return (
            f"{self._connect_tcl()}\n"
            f"puts [mrd -value -force {_fmt(addr)} {nwords}]"
        )

    def _write_tcl(self, addr: int, val: int) -> str:
        """The write script: a bare ``mwr <addr> <val>``.

        Deliberately carries ONLY ``mwr`` — no ``stop``/``con`` (the trap
        from ``mps3_diag.tcl:15``: halting the core mid-swap resets the TCP
        stream and corrupts a partial reconfiguration). Keeping the emitted
        command surface minimal preserves that non-halting guarantee.
        """
        return f"mwr {_fmt(addr)} {_fmt(val)}"

    def _select_target_tcl(self) -> str:
        """Reproduce ``mps3_diag.tcl:58-70``: enumerate ``MicroBlaze*``
        targets, select each in turn, and scan the ASCENDING candidate bases
        for the magic word. Targets RENUMBER between sessions, so we identify
        ours by magic, not by index. On no match the script ``exit 1`` — a
        non-zero return maps to :class:`TargetNotFound`.
        """
        cands = " ".join(_fmt(c) for c in self.cfg.candidates)
        magic = self.cfg.magic
        return (
            'foreach t [ta -filter {name =~ "MicroBlaze*"} -target-properties] {\n'
            "    set id [dict get $t target_id]\n"
            "    if { [catch {targets $id}] } { continue }\n"
            f"    foreach base {{{cands}}} {{\n"
            "        if { [catch {set w [mrd -force $base 1]}] } { continue }\n"
            f'        if {{ [string equal -nocase [lindex $w 1] {magic}] }} {{\n'
            f'            puts "target=$id mailbox=$base magic={magic}"\n'
            "            exit 0\n"
            "        }\n"
            "    }\n"
            "}\n"
            f'puts "ERROR: no MicroBlaze target has magic {magic} at any of: {cands}"\n'
            "exit 1\n"
        )

    def _argv(self, tcl: str) -> "list[str]":
        """``[xsdb, '-eval', <tcl>]`` — the invocation for a Tcl script."""
        return [self.cfg.xsdb, "-eval", tcl]

    def command_str(self, tcl: str) -> str:
        """Shell-quoted preview of :meth:`_argv` output, for logging /
        ``--dry-run`` display."""
        return shlex.join(self._argv(tcl))

    # -- run seam ----------------------------------------------------------- #
    def _run(self, tcl: str) -> object:
        """Run ``xsdb -eval <tcl>`` (or, if :attr:`dry_run`, return the
        argv). Returns the ``CompletedProcess`` on a real run."""
        argv = self._argv(tcl)
        if self.dry_run:
            return argv
        run = self.runner if self.runner is not None else default_runner
        return run(argv, timeout_s=self.cfg.timeout_s)

    @staticmethod
    def _text(result: object) -> str:
        """stdout + stderr of a CompletedProcess-like, for parsing."""
        out = getattr(result, "stdout", "") or ""
        err = getattr(result, "stderr", "") or ""
        return out + "\n" + err

    # -- RegisterBackend protocol ------------------------------------------- #
    def read_word(self, addr: int) -> int:
        """Read one 32-bit word at ``addr`` via ``mrd -value -force``."""
        result = self._run(self._read_tcl(addr, 1))
        if self.dry_run:
            return result  # type: ignore[return-value]  # argv preview
        return parse_mrd_value(self._text(result))

    def read_block(self, addr: int, nwords: int) -> "list[int]":
        """Read ``nwords`` consecutive words starting at ``addr``."""
        result = self._run(self._read_tcl(addr, nwords))
        if self.dry_run:
            return result  # type: ignore[return-value]  # argv preview
        return parse_mrd_block(self._text(result), nwords)

    def write_word(self, addr: int, val: int) -> None:
        """Write ``val`` to ``addr`` via a bare ``mwr`` (never halts)."""
        result = self._run(self._write_tcl(addr, val))
        if self.dry_run:
            return result  # type: ignore[return-value]  # argv preview
        return None

    # -- target selection (tcl-side magic scan) ----------------------------- #
    def select_target(self) -> int:
        """Run the magic-scan Tcl (:meth:`_select_target_tcl`) and return the
        mailbox base it reports. Raises :class:`TargetNotFound` when the
        script exits non-zero or prints no ``mailbox=`` line.
        """
        tcl = f"{self._connect_tcl()}\n{self._select_target_tcl()}"
        result = self._run(tcl)
        if self.dry_run:
            return result  # type: ignore[return-value]  # argv preview
        text = self._text(result)
        rc = getattr(result, "returncode", 0)
        m = re.search(r"mailbox=(0[xX][0-9A-Fa-f]+)", text)
        if rc != 0 or m is None:
            raise TargetNotFound(
                f"no MicroBlaze target carried magic {self.cfg.magic} at any "
                f"of {[_fmt(c) for c in self.cfg.candidates]} "
                f"(is the shell loaded? has the mailbox moved? see "
                f"firmware/common/diag.h).\n" + text.strip()
            )
        return int(m.group(1), 16)


# --------------------------------------------------------------------------- #
# The diagnostic mailbox decoder
# --------------------------------------------------------------------------- #
#: Words in the mailbox at each anchor. The struct is TOP-anchored to the LMB
#: end, so its size shows in the low byte of its base: v8 is 256 B (64 words) at
#: ``…F00``, v5..v7 128 B (32 words) at ``…F80``. Reading 64 words at a 0x80
#: anchor runs off the LMB end, which ALIASES rather than faulting and so comes
#: back as plausible garbage — the length has to be derived, not assumed.
_WORDS_AT_ANCHOR: dict[int, int] = {0x00: 64, 0x80: 32}

#: The LAST word that carries a FIELD at each anchor; everything above it is
#: that layout's ``reserved[]`` pad. Reading a pad word as whatever field this
#: module's (newer) list puts at that index would fabricate a counter out of
#: zeroed padding -- ``svc_count`` decoding as "0 services" off a v7 image's pad
#: reads as a fact and is not one.
#:
#: This constant cannot rot: the 128-byte layout is FROZEN HISTORY. No new
#: 0x80-anchored image will ever be built, and in every version that shipped at
#: that size (v5, v6, v7) word 30 was the last field and word 31 was pad. The
#: ``None`` for the 0x100 anchor means "this module's own list decides", which
#: is correct because the list and the images are regenerated together.
_LAST_FIELD_WORD_AT_ANCHOR: "dict[int, int | None]" = {0x00: None, 0x80: 30}


class DiagMailbox:
    """Decode the always-on JTAG diagnostic mailbox
    (``firmware/common/diag.h`` v8) over any
    :class:`~socket_harness.session.RegisterBackend`.

    This is the JTAG post-mortem twin of ``ShellClient.diag()``: usable
    DURING a swap when the 6900 control channel is parked, because ``mrd``
    reads a running MicroBlaze via the MDM.
    """

    def __init__(self, cfg: XsdbConfig = XsdbConfig()) -> None:
        self.cfg = cfg

    def _resolve_base(self, backend: RegisterBackend) -> int:
        """Find the mailbox base by scanning the ASCENDING candidate bases
        for the magic word — backend-agnostic (works over the real xsdb
        endpoint or an in-proc fake). The ascending order is load-bearing:
        the LMB decode aliases, so a 256 KB shell hits 0x0003FF80 first (its
        true base) while a 512 KB shell reads ordinary .bss there and falls
        through to 0x0007FF80. Raises :class:`TargetNotFound` on no match —
        never a silent zero.
        """
        magic = self.cfg.magic_int
        for cand in self.cfg.candidates:
            try:
                word = backend.read_word(cand)
            except XsdbError:
                continue
            if (word & 0xFFFFFFFF) == magic:
                return cand
        raise TargetNotFound(
            f"no candidate base in {[_fmt(c) for c in self.cfg.candidates]} "
            f"holds magic {_fmt(magic)}"
        )

    def read(
        self,
        backend: "RegisterBackend | None" = None,
        *,
        base: "int | None" = None,
    ) -> dict:
        """Read + decode the mailbox into a field dict.

        ``base`` defaults to the magic-scanned hit (:meth:`_resolve_base`).
        The READ LENGTH follows from the anchor that hit
        (:data:`_WORDS_AT_ANCHOR`), so an older FIELDED image — a board still
        running the v5..v7 128-byte mailbox — decodes correctly instead of
        being read 64 words deep off the end of its LMB.

        Fields this module knows about that the image in front of it does not
        carry are OMITTED from the result and named in ``missing``. They are
        never defaulted to 0: a fabricated counter is exactly the defect this
        mailbox exists to catch, and a caller that gets ``svc_skipped == 0``
        from a v7 board would read it as "no service is sick" rather than "this
        image has no service table".

        Decodes word 25 (``ovlstore_phase``) into its :data:`OVL_PHASES` name,
        and adds the one derived check worth making automatically: a
        ``warning`` when ``tx_frames_sent`` != ``tx_status_drained`` (an
        un-drained TX-STATUS FIFO wedges the MAC).
        """
        be = backend if backend is not None else XsdbRegisterEndpoint(self.cfg)
        if base is None:
            base = self._resolve_base(be)

        nwords = _WORDS_AT_ANCHOR.get(base & 0xFF, 32)
        last_field = _LAST_FIELD_WORD_AT_ANCHOR.get(base & 0xFF)
        words = be.read_block(base, nwords)
        if len(words) < 27:
            raise XsdbError(
                f"diag mailbox read short: expected {nwords} words, "
                f"got {len(words)}"
            )

        out: dict = {
            "base": base,
            "magic": words[0],
            "layout_words": len(words),
        }
        missing: list[str] = []
        for off, name in DIAG_FIELDS:
            if off < len(words) and (last_field is None or off <= last_field):
                out[name] = words[off]
            else:
                missing.append(name)
        if missing:
            out["missing"] = tuple(missing)

        phase = out["ovlstore_phase"]
        out["ovlstore_phase_name"] = (
            OVL_PHASES[phase]
            if 0 <= phase < len(OVL_PHASES)
            else f"UNKNOWN({phase})"
        )

        skipped = out.get("svc_skipped_mask", 0)
        if skipped:
            # A superloop service has been taken OUT of the rotation for
            # blowing its budget on MPS3_SVC_SICK_K consecutive passes
            # (firmware/common/service.h). That is a wedge in progress and it
            # is invisible in every other counter here.
            out["service_warning"] = (
                f"svc_skipped = 0x{skipped:08X}: the superloop is skipping "
                "service(s) that overran their budget -- see "
                "firmware/common/service.h"
            )

        sent = out["tx_frames_sent"]
        drained = out["tx_status_drained"]
        if sent != drained:
            out["warning"] = (
                f"tx_frames_sent ({sent}) != tx_status_drained ({drained}); "
                "the TX status FIFO is not being drained -- the MAC will wedge"
            )
        return out
