"""``pyverify.mailbox`` — the two blocks at the top of the harness CPU's LMB.

Both harness engines keep a small, fixed-layout block at the END of the CPU's
local memory (LMB) so it can be read from OUTSIDE the running software:

* the **diag mailbox** (``firmware/common/diag.h``, ``g_mps3_diag``): the
  always-on counters the 6900 ``diag`` verb also serves. It is TOP-anchored:
  ``base = lmb_kb * 1024 - sizeof(mps3_diag_t)``, and ``sizeof`` is 0x100 at
  diag v8 (0x80 at v5..v7). Bare-metal images have been linked at 256, 512 and
  1024 KiB; the MicroBlaze V Linux harness has a **128 KiB** LMB, so its
  mailbox sits at ``0x1FF00`` (``docs/planning/LINUX_HARNESS_PLAN_2026-09-23.md``
  §3, "LMB map").
* the **stage0 status block** (MicroBlaze V only), 256 B at ``0x1FE00``: what
  the first-stage loader decided at power-on -- boot slot, boot counter, DDR
  calibration, rescue state. Owned by the STAGE0 lane; its one layout
  definition is ``S0_STATUS_FIELDS(X)`` in
  ``src/linux_soc/hw/fw_stage0/stage0_status.h``.

HOW THEY ARE READ. Anything that can read 32-bit words at a physical address
is a :class:`WordReader`:

* over JTAG, ``scripts/mps3_diag.tcl`` (xsdb ``mrd``). On the MicroBlaze V the
  LMB is decoded INSIDE the CPU (its DLMB port) and is on no AXI bus
  (SHELL_CONTRACT.md §3), so a debug-module system-bus read that goes out over
  AXI cannot reach it; whether ``mdm_riscv`` can read it without a halt is
  SHELL's open item L-2 -- **unproven, B1 item 8**;
* over SSH on the Linux harness, :class:`SshDevmemReader` (BusyBox ``devmem``
  against ``/dev/mem`` -- the LMB tail is also a UIO map that ``mps3-harnessd``
  owns, but a read-only peek does not disturb it);
* in tests, :class:`FakeMemory`.

THE ADDRESS IS NEVER TRUSTED -- IT IS SCANNED. The LMB decode ALIASES: on a
128 KiB LMB, ``0x3FF00`` wraps onto ``0x1FF00`` and returns the same magic. So
:func:`find_diag` scans :func:`diag_candidates` ASCENDING and takes the FIRST
hit, exactly as ``scripts/mps3_diag.tcl`` does (read its header for the second
reason the order matters: a v7 image's magic address is a v8 image's counter).
The magic alone is also not enough to accept a hit -- the version word beside
it must be a real diag version, so an ordinary RAM word that happens to hold
the magic is rejected.

HALTING IS NEVER DONE HERE. Nothing in this module stops a processor: on the
MicroBlaze V an xsdb ``stop`` halts the whole of Linux. See
:func:`pyverify.lease.assert_xsdb_script_safe`.
"""
from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

try:  # Protocol is 3.8+ in typing
    from typing import Protocol
except ImportError:  # pragma: no cover
    Protocol = object  # type: ignore

__all__ = [
    "DIAG_MAGIC",
    "DIAG_VERSION_MAX",
    "DIAG_STRUCT_V8",
    "DIAG_STRUCT_V5",
    "MBV_LMB_KB",
    "BARE_METAL_LMB_KBS",
    "DIAG_FIELDS",
    "diag_anchor",
    "diag_candidates",
    "DiagMailbox",
    "decode_diag",
    "find_diag",
    "STAGE0_STATUS_ADDR",
    "STAGE0_STATUS_BYTES",
    "STAGE0_STATUS_FIELDS",
    "STAGE0_LAYOUT_SOURCE",
    "STAGE0_STATUS_MAGIC",
    "Stage0Status",
    "decode_stage0_status",
    "read_stage0_status",
    "WordReader",
    "FakeMemory",
    "SshDevmemReader",
    "SSH_DEVMEM_TIMEOUT_S",
    "mailbox_read_plan",
    "prefetch_mailboxes",
    "MailboxError",
]

#: ``MPS3_DIAG_MAGIC`` (diag.h).
DIAG_MAGIC = 0xD1A6C0DE
#: The newest ``MPS3_DIAG_VERSION`` this reader knows the layout of. A HIGHER
#: version is still accepted (the layout is append-only, so every field this
#: reader names keeps its offset); a version of 0 or one below the first
#: top-anchored layout (v5) is not a mailbox.
DIAG_VERSION_MAX = 9
_DIAG_VERSION_MIN = 5
#: ``sizeof(mps3_diag_t)`` at v8 (64 words) and at v5..v7 (32 words).
DIAG_STRUCT_V8 = 0x100
DIAG_STRUCT_V5 = 0x80
#: The last word that carries a FIELD in the 128-byte (v5..v7) layout. Above it
#: is that layout's reserved[] pad; decoding a pad word as a newer field would
#: fabricate a counter out of zeroed padding (mps3_diag.tcl's LAST_FIELD).
_V5_LAST_FIELD = 30
#: The last FIELD word of a v8 image (``svc_max_us_5``). v9 (D13) moved two
#: formerly-reserved words into fields (``svc_max_us_6``, ``usd_boot``) without
#: moving anything; on a v8 image those two words are still zeroed PAD, so they
#: are decoded only for version >= 9.
_V8_LAST_FIELD = 43

#: The MicroBlaze V Linux harness's LMB (plan §3, SHELL's ``cpu_mbv.tcl``).
MBV_LMB_KB = 128
#: Every LMB size a bare-metal image has been linked for.
BARE_METAL_LMB_KBS = (256, 512, 1024)

#: The v9 field list, ``(struct field, wire key)`` in WORD ORDER -- row index ==
#: word offset from the base. Mirrors ``MPS3_DIAG_FIELDS(X)`` in
#: ``firmware/common/diag.h`` minus the PAD; ``tests/test_mailbox.py`` parses
#: diag.h through ``tools/gen_diag.py`` and fails if the two ever disagree.
DIAG_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("magic", "magic"),
    ("version", "version"),
    ("rx_recover_events", "rx_recover"),
    ("rx_recover_dumps", "rx_dumps"),
    ("rx_drop_frames", "rx_drops"),
    ("icap_bytes", "icap_bytes"),
    ("rx_payload_got", "got"),
    ("rx_payload_expect", "expect"),
    ("tcp_rcv_wnd", "rcv_wnd"),
    ("tcp_rcv_ann_wnd", "rcv_ann_wnd"),
    ("rx_queued", "rx_queued"),
    ("pbuf_free", "pbuf_free"),
    ("win_windows_drained", "grants_sent"),
    ("win_grant_send_fails", "grant_fails"),
    ("tcp_sndbuf", "sndbuf"),
    ("tcp_snd_wnd", "snd_wnd"),
    ("tx_frames_sent", "tx_frames_sent"),
    ("tx_status_drained", "tx_status_drained"),
    ("tx_fifo_full_drops", "tx_fifo_full_drops"),
    ("tx_errors", "tx_errors"),
    ("tx_space_stalls", "tx_space_stalls"),
    ("tx_iface_errors", "tx_iface_errors"),
    ("tx_last_status", "tx_last_status"),
    ("icap_sr_last", "icap_sr_last"),
    ("icap_eos_status", "icap_eos_status"),
    ("ovlstore_phase", "ovlstore_phase"),
    ("ovlstore_detail", "ovlstore_detail"),
    ("touch_probe_regs", "touch_regs"),
    ("touch_probe_adc_x", "touch_adc_x"),
    ("touch_probe_adc_y", "touch_adc_y"),
    ("touch_probe_verdict", "touch_verdict"),
    ("svc_count", "svc_count"),
    ("svc_pass_max_us", "pass_max_us"),
    ("svc_worst_us", "svc_max_us"),
    ("svc_worst_ix", "svc_max_ix"),
    ("svc_overrun_events", "svc_overruns"),
    ("svc_skip_events", "svc_skips"),
    ("svc_skipped_mask", "svc_skipped"),
    ("svc_max_us_0", "svc_us_0"),
    ("svc_max_us_1", "svc_us_1"),
    ("svc_max_us_2", "svc_us_2"),
    ("svc_max_us_3", "svc_us_3"),
    ("svc_max_us_4", "svc_us_4"),
    ("svc_max_us_5", "svc_us_5"),
    # v9 (D13): APPEND-INTO-RESERVED -- no prior offset moves.
    ("svc_max_us_6", "svc_us_6"),
    ("usd_boot", "usd_boot"),
)


class MailboxError(Exception):
    """No mailbox where one was expected, or a reader could not read."""


def diag_anchor(lmb_kb: int, struct_bytes: int = DIAG_STRUCT_V8) -> int:
    """The mailbox base an image linked for ``lmb_kb`` puts it at.

    ``lmb_kb * 1024 - sizeof(mps3_diag_t)`` -- the linker's top anchor
    (``firmware/platform/lscript.ld.in`` on bare metal; the LMB map in the
    Linux plan §3 on the MicroBlaze V). 128 KiB at v8 -> ``0x1FF00``.
    """
    if lmb_kb <= 0:
        raise ValueError("lmb_kb must be positive, got %r" % (lmb_kb,))
    if struct_bytes not in (DIAG_STRUCT_V8, DIAG_STRUCT_V5):
        raise ValueError("struct_bytes must be 0x100 (v8) or 0x80 (v5..v7)")
    return lmb_kb * 1024 - struct_bytes


def diag_candidates(lmb_kbs: Iterable[int] = (MBV_LMB_KB,) + BARE_METAL_LMB_KBS) -> List[int]:
    """Every base the mailbox can be at, over BOTH struct sizes, ASCENDING.

    Ascending is not cosmetic: the LMB decode aliases, so on a small LMB every
    larger candidate wraps back onto the true one, and only the first (lowest)
    hit is the real address. See the module docstring.
    """
    out = set()
    for kb in lmb_kbs:
        out.add(diag_anchor(kb, DIAG_STRUCT_V8))
        out.add(diag_anchor(kb, DIAG_STRUCT_V5))
    return sorted(out)


@dataclass(frozen=True)
class DiagMailbox:
    """One decoded diag mailbox.

    ``fields`` holds only the words that exist in the layout found; a field
    this reader knows but the image does not have is ABSENT (``get`` returns
    ``None``), never a fabricated zero.
    """

    base: int
    layout: str                      #: "v8 (256 B)" or "v5-v7 (128 B)"
    words: Tuple[int, ...]
    fields: Dict[str, int] = field(default_factory=dict, compare=False, hash=False)

    @property
    def version(self) -> int:
        return self.words[1] if len(self.words) > 1 else 0

    @property
    def lmb_kb(self) -> int:
        """The LMB size this base implies (the inverse of :func:`diag_anchor`)."""
        size = DIAG_STRUCT_V8 if (self.base & 0xFF) == 0 else DIAG_STRUCT_V5
        return (self.base + size) // 1024

    def get(self, name: str) -> Optional[int]:
        return self.fields.get(name)

    def as_wire(self) -> Dict[str, int]:
        """The same values under the 6900 ``diag`` verb's JSON keys, so a
        JTAG/ssh read and a network read compare key-for-key."""
        by_name = dict(DIAG_FIELDS)
        return {by_name[n]: v for n, v in self.fields.items()
                if n in by_name and n not in ("magic", "version")}


def decode_diag(base: int, words: Sequence[int]) -> DiagMailbox:
    """Decode the words read at ``base``. HOW MANY words a base carries is
    decided by the ANCHOR (``base & 0xFF``: 0x00 -> 64, 0x80 -> 32), never by
    this module's newest version -- reading 64 at a 0x80 anchor runs off the
    LMB end and aliases into plausible garbage."""
    words = tuple(int(w) & 0xFFFFFFFF for w in words)
    if (base & 0xFF) == 0:
        layout, want, last = "v8 (256 B)", DIAG_STRUCT_V8 // 4, len(DIAG_FIELDS) - 1
        if len(words) > 1 and words[1] < 9:
            last = _V8_LAST_FIELD         # v8: the v9 words are still pad
    elif (base & 0xFF) == 0x80:
        layout, want, last = "v5-v7 (128 B)", DIAG_STRUCT_V5 // 4, _V5_LAST_FIELD
    else:
        raise MailboxError("0x%X is not a diag anchor (must end in 0x00 or 0x80)" % base)
    if len(words) < 2:
        raise MailboxError("need at least magic+version, got %d word(s)" % len(words))
    if words[0] != DIAG_MAGIC:
        raise MailboxError("no diag magic at 0x%X (read 0x%08X)" % (base, words[0]))
    words = words[:want]
    fields = {}
    for idx, (name, _key) in enumerate(DIAG_FIELDS):
        if idx > last or idx >= len(words):
            continue
        fields[name] = words[idx]
    return DiagMailbox(base=base, layout=layout, words=words, fields=fields)


def _plausible_version(v: int) -> bool:
    return _DIAG_VERSION_MIN <= v <= 0xFF


def _diag_words(base: int) -> int:
    """How many words the mailbox at ``base`` spans: decided by the ANCHOR
    (0x00 -> v8, 64 words; 0x80 -> v5..v7, 32 words), see :func:`decode_diag`."""
    return (DIAG_STRUCT_V8 if (base & 0xFF) == 0 else DIAG_STRUCT_V5) // 4


def find_diag(reader: "WordReader",
              lmb_kbs: Iterable[int] = (MBV_LMB_KB,) + BARE_METAL_LMB_KBS) -> DiagMailbox:
    """Scan the candidates ascending; decode the first real mailbox.

    A candidate is real when word 0 is the magic AND word 1 is a plausible diag
    version (>= 5, the first top-anchored layout). Raises
    :class:`MailboxError` naming every address tried.

    The scan is data-dependent (probe, then the body), so on a reader that can
    batch (:meth:`SshDevmemReader.prefetch`) every word it could ask for is
    fetched FIRST, in one session; the probes below are then served from that
    snapshot. A reader that cannot batch (JTAG, :class:`FakeMemory`) is read
    exactly as before.
    """
    lmb_kbs = tuple(lmb_kbs)
    _prefetch(reader, *mailbox_read_plan("diag", lmb_kbs))
    tried = []
    for base in diag_candidates(lmb_kbs):
        try:
            head = reader.read_words(base, 2)
        except MailboxError:
            tried.append("0x%05X (unreadable)" % base)
            continue
        if len(head) >= 2 and head[0] == DIAG_MAGIC and _plausible_version(head[1]):
            return decode_diag(base, reader.read_words(base, _diag_words(base)))
        tried.append("0x%05X" % base)
    raise MailboxError(
        "no diag mailbox (magic 0x%08X + a version >= %d) at any of: %s"
        % (DIAG_MAGIC, _DIAG_VERSION_MIN, ", ".join(tried)))


# --------------------------------------------------------------------------- #
# The stage0 status block (MicroBlaze V only)
# --------------------------------------------------------------------------- #

#: Plan §3 LMB map: ``0x1FE00–0x1FEFF`` = stage0 status block (STAGE0 owns it).
STAGE0_STATUS_ADDR = 0x1FE00
STAGE0_STATUS_BYTES = 0x100

#: WHERE THE FIELD TABLE CAME FROM. STAGE0 owns the layout: ONE definition,
#: ``S0_STATUS_FIELDS(X)`` in ``src/linux_soc/hw/fw_stage0/stage0_status.h``.
#: pyverify carries a SNAPSHOT of it (below) so a copy staged on the hub, with
#: no repo beside it, still decodes; when the header IS reachable (an in-tree
#: checkout) the table is read from the header at import time instead, so an
#: in-tree pyverify can never decode with a stale layout. tests/test_mailbox.py
#: fails whenever the snapshot and the header disagree -- refresh the snapshot
#: from the table its failure message prints.
_STAGE0_HEADER_REL = ("src", "linux_soc", "hw", "fw_stage0", "stage0_status.h")

_STAGE0_SNAPSHOT: Tuple[Tuple[str, int], ...] = (
    ('magic', 0x00),
    ('version', 0x04),
    ('size', 0x08),
    ('build_id', 0x0C),
    ('fabric_static_id', 0x10),
    ('fabric_ver32', 0x14),
    ('boot_count', 0x18),
    ('phase', 0x1C),
    ('booted_from', 0x20),
    ('last_error', 0x24),
    ('ddr_calib', 0x28),
    ('sd_result', 0x2C),
    ('sd_detail', 0x30),
    ('slot_a_rc', 0x34),
    ('slot_b_rc', 0x38),
    ('default_slot', 0x3C),
    ('cfg_seq', 0x40),
    ('att_from', 0x44),
    ('att_confirm', 0x48),
    ('fails_a', 0x4C),
    ('fails_b', 0x50),
    ('boot_limit', 0x54),
    ('last_verdict', 0x58),
    ('rescue_reason', 0x5C),
    ('rescue_state', 0x60),
    ('rescue_bytes', 0x64),
    ('rescue_sessions', 0x68),
    ('rescue_rejects', 0x6C),
    ('rescue_last_rc', 0x70),
    ('n_boot_a', 0x74),
    ('n_boot_b', 0x78),
    ('n_boot_rescue', 0x7C),
    ('n_fallback', 0x80),
    ('entry_pc', 0x84),
    ('entry_a0', 0x88),
    ('entry_a1', 0x8C),
    ('image_hdr_crc', 0x90),
    ('handoff_ms', 0x94),
    ('heartbeat', 0x98),
    ('uptime_ms', 0x9C),
    ('reset_cause', 0xA0),
    ('trap_mcause', 0xA4),
    ('trap_mepc', 0xA8),
    ('trap_mtval', 0xAC),
    ('ip_addr', 0xB0),
    ('mac_lo', 0xB4),
    ('mac_hi', 0xB8),
    ('net_rc', 0xBC),
    ('tftp_errors', 0xC0),
    ('rx_frames', 0xC4),
    ('tx_frames', 0xC8),
    ('pings', 0xCC),
    ('identifies', 0xD0),
    ('verdict_from', 0xD4),
    ('sd_rd_fails', 0xD8),
    ('sd_rd_last', 0xDC),
    ('subphase', 0xE0),
    ('entry', 0xE4),
    ('prev_phase', 0xE8),
    ('label_lo', 0xEC),
    ('label_hi', 0xF0),
    ('prev_uptime_ms', 0xF4),
    ('ddr_ok_ms', 0xF8),
)


def _find_stage0_file(name: str) -> "Optional[Path]":
    here = Path(__file__).resolve()
    rel = _STAGE0_HEADER_REL[:-1] + (name,)
    for parent in here.parents:
        cand = parent.joinpath(*rel)
        if cand.is_file():
            return cand
    return None


def _find_stage0_header() -> "Optional[Path]":
    return _find_stage0_file(_STAGE0_HEADER_REL[-1])


def _load_stage0_layout():
    """``(fields, source, magics)``, from the most authoritative source
    reachable: STAGE0's own layout-gated decoder ``stage0_status.py`` (STAGE0
    asked HOST to import it rather than keep a copy), else the C header it is
    gated against, else pyverify's snapshot (a copy staged alone on the hub)."""
    magics = {"S0_STATUS_MAGIC": 0x54533053, "S0_CONFIRM_MAGIC": 0x4B4F3053,
              "S0_STATUS_VERSION": 1}
    py = _find_stage0_file("stage0_status.py")
    if py is not None:
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("_mps3_stage0_status", str(py))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            fields = tuple((str(n), int(o)) for n, o in mod.FIELDS)
            if fields and fields[-1][0] == "magic_end":
                magics = {"S0_STATUS_MAGIC": int(mod.MAGIC),
                          "S0_CONFIRM_MAGIC": int(mod.CONFIRM_MAGIC),
                          "S0_STATUS_VERSION": int(mod.VERSION)}
                return fields, "stage0_status.py (in-tree)", magics
        except Exception:        # a broken sibling must not break pyverify
            pass
    hdr = _find_stage0_header()
    if hdr is not None:
        try:
            text = hdr.read_text()
        except OSError:
            text = ""
        rows = [(n, int(o, 16)) for n, o in
                re.findall(r'X\((\w+),\s*(0x[0-9A-Fa-f]+),\s*"', text)]
        if rows:
            for name in magics:
                m = re.search(r"#define\s+%s\s+(0x[0-9A-Fa-f]+|\d+)" % name, text)
                if m:
                    magics[name] = int(m.group(1), 0)
            return tuple(rows) + (("magic_end", 0xFC),), "stage0_status.h (in-tree)", magics
    return _STAGE0_SNAPSHOT + (("magic_end", 0xFC),), "stage0_status.h snapshot", magics


STAGE0_STATUS_FIELDS, STAGE0_LAYOUT_SOURCE, _S0_MAGICS = _load_stage0_layout()
#: ``S0_STATUS_MAGIC`` -- bytes "S0ST". Can never be the diag magic, so a
#: mailbox scanner cannot mistake one block for the other.
STAGE0_STATUS_MAGIC = _S0_MAGICS["S0_STATUS_MAGIC"]
STAGE0_STATUS_VERSION = _S0_MAGICS["S0_STATUS_VERSION"]
#: ``S0_CONFIRM_MAGIC``: what Linux writes into ``att_confirm`` once healthy.
STAGE0_CONFIRM_MAGIC = _S0_MAGICS["S0_CONFIRM_MAGIC"]

#: Enum decodes (stage0_status.h). Unknown values render as ``"?(<n>)"``.
STAGE0_PHASES = {0: "RESET", 1: "DDR", 2: "SD", 3: "SLOT_A", 4: "SLOT_B",
                 5: "RESCUE", 6: "HANDOFF", 7: "TRAP"}
STAGE0_FROM = {0: "NONE", 1: "A", 2: "B", 3: "RESCUE"}
STAGE0_DDR = {0: "UNKNOWN", 1: "OK", 2: "FAIL", 3: "IMPLIED", 4: "LOST"}
STAGE0_SD = {0: "NOTRIED", 1: "READY", 2: "NOCARD", 3: "NOHW", 4: "UNSUP",
             5: "ERROR", 6: "TIMEOUT", 7: "NOMBR", 8: "SKIPPED"}
STAGE0_RESCUE = {0: "OFF", 1: "LISTEN", 2: "RECEIVING", 3: "VERIFYING",
                 4: "REJECTED", 5: "ACCEPTED", 6: "NONET", 7: "NODDR"}
STAGE0_RESCUE_REASON = {0: "NONE", 1: "DDR", 2: "NOCARD", 3: "NOHW", 4: "UNSUP",
                        5: "SDERR", 6: "NOLAYOUT", 7: "BADSLOTS", 8: "EXHAUSTED",
                        9: "WDOG_LOOP"}
STAGE0_VERDICT = {0: "NONE", 1: "CONFIRMED", 2: "UNCONFIRMED"}
STAGE0_ERR_SRC = {0: "NONE", 1: "DDR", 2: "SD", 3: "SLOT_A", 4: "SLOT_B",
                  5: "RESCUE", 6: "NET", 7: "TRAP", 8: "WDOG"}
#: ``subphase`` [31:24] / ``entry`` [7:0] (stage0_status.h S0_SP_* / S0_EK_*;
#: lane S0-COLDFIX 2026-09-28).
STAGE0_SUBPHASE = {0: "NONE", 1: "SETTLE", 2: "DDR_WAIT", 3: "CARD_INIT", 4: "CARD_META",
                   5: "SLOT_LOAD", 6: "CRC", 7: "HANDOFF", 8: "RESCUE"}
STAGE0_ENTRY_KIND = {0: "NONE", 1: "COLD", 2: "WARM", 3: "WDOG", 4: "WDOG_LINUX",
                     5: "RESETTLE"}

#: field -> enum table, applied by :meth:`Stage0Status.summary` to whichever of
#: these the layout in force actually has.
_STAGE0_ENUMS = {
    "phase": STAGE0_PHASES, "booted_from": STAGE0_FROM, "att_from": STAGE0_FROM,
    "default_slot": STAGE0_FROM, "verdict_from": STAGE0_FROM,
    "last_handoff_from": STAGE0_FROM, "ddr_calib": STAGE0_DDR,
    "sd_result": STAGE0_SD, "rescue_state": STAGE0_RESCUE,
    "rescue_reason": STAGE0_RESCUE_REASON, "last_verdict": STAGE0_VERDICT,
}


@dataclass(frozen=True)
class Stage0Status:
    """The stage0 status block: raw words plus the named fields."""

    base: int
    words: Tuple[int, ...]
    layout_source: str
    fields: Dict[str, int] = field(default_factory=dict, compare=False, hash=False)

    @property
    def magic(self) -> int:
        return self.words[0] if self.words else 0

    @property
    def blank(self) -> bool:
        """All zero: stage0 never stamped the block (a bare-metal image, or a
        board that has not run stage0 since configuration)."""
        return not any(self.words)

    @property
    def valid(self) -> bool:
        """STAGE0_CONTRACT §3's acceptance rule: magic, version, size and the
        torn-read guard ``magic_end`` all agree."""
        f = self.fields
        return (f.get("magic") == STAGE0_STATUS_MAGIC
                and f.get("magic_end") == STAGE0_STATUS_MAGIC
                and f.get("size") == STAGE0_STATUS_BYTES
                and f.get("version") == STAGE0_STATUS_VERSION)

    @property
    def linux_confirmed(self) -> Optional[bool]:
        """Linux confirmed the current attempt (``att_confirm``); ``None`` when
        the layout in force has no confirmation field."""
        if "att_confirm" in self.fields:
            return self.fields["att_confirm"] == STAGE0_CONFIRM_MAGIC
        return None

    def summary(self) -> Dict[str, object]:
        """The fields a human triages a boot with, enums decoded. Only fields
        the layout in force has are reported."""
        f = self.fields
        out: Dict[str, object] = {"valid": self.valid}
        for name, table in _STAGE0_ENUMS.items():
            if name in f:
                out[name] = table.get(f[name], "?(%d)" % f[name])
        for name in ("boot_count", "n_boot_a", "n_boot_b", "n_boot_rescue", "n_fallback",
                     "fails_a", "fails_b", "boot_limit", "rescue_sessions",
                     "rescue_rejects", "handoff_ms", "uptime_ms"):
            if name in f:
                out[name] = f[name]
        if "last_error" in f:
            le = f["last_error"]
            out["last_error"] = "%s:%d" % (STAGE0_ERR_SRC.get(le >> 16, "?(%d)" % (le >> 16)),
                                           le & 0xFFFF)
        if "reset_cause" in f:
            out["watchdog_reset"] = bool(f["reset_cause"] & 0x8)
        if "subphase" in f:
            w = f["subphase"]
            out["subphase"] = "%s:%d" % (STAGE0_SUBPHASE.get(w >> 24, "?(%d)" % (w >> 24)),
                                         w & 0xFFFFFF)
        if "entry" in f:
            e = f["entry"]
            out["entry_kind"] = STAGE0_ENTRY_KIND.get(e & 0xFF, "?(%d)" % (e & 0xFF))
            out["wdog_run"] = (e >> 8) & 0xFF
            out["calib_drops"] = (e >> 16) & 0xFF
            out["ddr_recoveries"] = (e >> 24) & 0xF
            out["cal_at_settle"] = bool((e >> 28) & 1)
            out["cal_rose_in_settle"] = bool((e >> 29) & 1)
            out["settle_pending"] = bool(e >> 31)
        if "prev_phase" in f:
            # [7:0] phase, [15:8] subphase code, [31:16] its detail (sat 0xFFFF)
            w = f["prev_phase"]
            out["prev_phase"] = STAGE0_PHASES.get(w & 0xFF, "?(%d)" % (w & 0xFF))
            sp = (w >> 8) & 0xFF
            out["prev_subphase"] = "%s:%d" % (STAGE0_SUBPHASE.get(sp, "?(%d)" % sp), w >> 16)
        for name in ("prev_uptime_ms", "ddr_ok_ms", "sd_rd_fails"):
            if name in f:
                out[name] = f[name]
        if "label_lo" in f and "label_hi" in f:
            # the board's baked identity (lane IDENT): 8 ASCII bytes, NUL-padded; 0 = absent
            raw = b"".join((f[k] & 0xFFFFFFFF).to_bytes(4, "little")
                           for k in ("label_lo", "label_hi")).split(b"\0", 1)[0]
            out["label"] = raw.decode("ascii", "replace") if raw else None
        out["linux_confirmed"] = self.linux_confirmed
        for name in ("build_id", "fabric_static_id", "fabric_ver32"):
            if name in f:
                out[name] = "0x%08x" % f[name]
        return out


def decode_stage0_status(words: Sequence[int], base: int = STAGE0_STATUS_ADDR) -> Stage0Status:
    words = tuple(int(w) & 0xFFFFFFFF for w in words)
    fields = {name: words[off // 4] for name, off in STAGE0_STATUS_FIELDS if off // 4 < len(words)}
    return Stage0Status(base=base, words=words, layout_source=STAGE0_LAYOUT_SOURCE,
                        fields=fields)


def read_stage0_status(reader: "WordReader") -> Stage0Status:
    return decode_stage0_status(
        reader.read_words(STAGE0_STATUS_ADDR, STAGE0_STATUS_BYTES // 4))


# --------------------------------------------------------------------------- #
# One batched read for both blocks
# --------------------------------------------------------------------------- #


def mailbox_read_plan(what: str = "both",
                      lmb_kbs: Iterable[int] = (MBV_LMB_KB,) + BARE_METAL_LMB_KBS
                      ) -> Tuple[List[Tuple[int, int]], List[Tuple[int, int]]]:
    """``(ranges, stop_if)``: every word :func:`find_diag` (over ``lmb_kbs``)
    and/or :func:`read_stage0_status` can ask for, as merged ascending
    ``(addr, word_count)`` ranges, plus the stop guard for
    :meth:`SshDevmemReader.prefetch`.

    find_diag's reads depend on the data (probe a candidate, read the body only
    at a hit), so the plan holds EVERY candidate's full extent -- the 0x00
    anchor's 64 words, which contain the 0x80 anchor's 32. Default sizes:
    stage0 64 + four LMB sizes x 64 = 320 words.

    ``stop_if`` is ``(anchor, DIAG_MAGIC)`` per candidate: once an anchor reads
    the magic, the extents ABOVE it are not read. That is the ascending scan's
    early exit, taken on the remote side: on the MicroBlaze V (magic at
    0x1FF00) the batch reads 128 words and never touches a bare-metal anchor
    -- which the old one-probe-at-a-time scan never did either. It relies on
    the stage0 block sitting below every anchor (0x1FE00 < 0x1FF00); if a
    skipped word is ever asked for anyway, it is read fresh, never guessed.
    """
    if what not in ("diag", "stage0", "both"):
        raise ValueError("what must be diag, stage0 or both, got %r" % (what,))
    ranges: List[Tuple[int, int]] = []
    stop_if: List[Tuple[int, int]] = []
    if what in ("stage0", "both"):
        ranges.append((STAGE0_STATUS_ADDR, STAGE0_STATUS_BYTES // 4))
    if what in ("diag", "both"):
        for base in diag_candidates(lmb_kbs):
            ranges.append((base, _diag_words(base)))
            stop_if.append((base, DIAG_MAGIC))
    return _merge_ranges(ranges), stop_if


def prefetch_mailboxes(reader: "WordReader", what: str = "both",
                       lmb_kbs: Iterable[int] = (MBV_LMB_KB,) + BARE_METAL_LMB_KBS) -> None:
    """Fetch, in ONE session, everything a following ``find_diag(reader,
    lmb_kbs)`` and/or ``read_stage0_status(reader)`` will read. A no-op on a
    reader that cannot batch."""
    _prefetch(reader, *mailbox_read_plan(what, tuple(lmb_kbs)))


def _prefetch(reader: "WordReader", ranges: Sequence[Tuple[int, int]],
              stop_if: Sequence[Tuple[int, int]] = ()) -> None:
    fn = getattr(reader, "prefetch", None)
    if callable(fn):
        fn(ranges, stop_if=stop_if)


def _merge_ranges(ranges: Iterable[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Sort ``(addr, word_count)`` ranges ascending and merge the ones that
    overlap or touch (on the same word grid)."""
    spans = sorted((int(a), int(a) + 4 * int(n)) for a, n in ranges if int(n) > 0)
    out: List[List[int]] = []
    for lo, hi in spans:
        if out and lo <= out[-1][1] and (lo - out[-1][0]) % 4 == 0:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return [(lo, (hi - lo) // 4) for lo, hi in out]


# --------------------------------------------------------------------------- #
# Readers
# --------------------------------------------------------------------------- #


class WordReader(Protocol):
    """Anything that reads ``count`` 32-bit words starting at physical ``addr``.
    Raises :class:`MailboxError` when it cannot."""

    def read_words(self, addr: int, count: int) -> List[int]:  # pragma: no cover
        ...


class FakeMemory:
    """A sparse word memory for tests, with an optional LMB ALIAS: when
    ``alias_kb`` is set, every address is taken modulo that size, which is what
    the real LMB decode does and what makes the ascending scan necessary."""

    def __init__(self, words: Optional[Dict[int, int]] = None, *, alias_kb: Optional[int] = None):
        self.mem: Dict[int, int] = dict(words or {})
        self.alias_kb = alias_kb
        self.reads: List[Tuple[int, int]] = []

    def poke(self, addr: int, values: Sequence[int]) -> None:
        for i, v in enumerate(values):
            self.mem[addr + 4 * i] = int(v) & 0xFFFFFFFF

    def read_words(self, addr: int, count: int) -> List[int]:
        self.reads.append((addr, count))
        out = []
        for i in range(count):
            a = addr + 4 * i
            if self.alias_kb:
                a %= self.alias_kb * 1024
            out.append(self.mem.get(a, 0))
        return out


_DEVMEM_LINE = re.compile(r"^\s*(0[xX][0-9A-Fa-f]+)\s*$")

#: The bound on ONE ssh devmem session. B1 v4 (2026-09-25,
#: docs/evidence/2026-09-linux-b1-v4/b1_i_mailbox.txt): an ssh login ALONE took
#: ~14 s on the loaded MicroBlaze V (dropbear's key exchange on a soft core),
#: and the old 30 s bound -- per session, three sessions -- failed step (i).
#: 90 s = a login under that load + one mailbox batch of devmem forks, with
#: margin. ``pyverify mailbox --timeout`` overrides it.
SSH_DEVMEM_TIMEOUT_S = 90.0

#: The reply protocol of a batched read: exactly one line per requested word
#: -- a devmem value, ``_ERR_TOKEN`` (devmem failed for that word: bus error,
#: EPERM; the batch goes on) or ``_SKIP_TOKEN`` (the stop guard fired; not
#: read) -- then ``_END_TOKEN <word count>`` once the remote shell finished.
_ERR_TOKEN = "@MBX-ERR"
_SKIP_TOKEN = "@MBX-SKIP"
_END_TOKEN = "@MBX-END"
_STOP_VAR = "_mbx_stop"
#: dropbear refuses an exec request longer than its MAX_CMD_LEN (9000); the
#: default mailbox batch is ~3.5 KB.
_MAX_CMD_CHARS = 8000


def _hex_glob(value: int) -> str:
    """A shell ``case`` pattern matching devmem's print of ``value`` in either
    letter case (BusyBox prints ``0x%08X``)."""
    return "0[xX]" + "".join("[%s%s]" % (c.lower(), c) if c.isalpha() else c
                             for c in "%08X" % (value & 0xFFFFFFFF))


class SshDevmemReader:
    """Read words on the Linux harness over SSH with BusyBox ``devmem``.

    EVERY READ IS ONE SSH SESSION, and a session is expensive: on B1 v4 a
    login alone took ~14 s on the loaded MicroBlaze V. So a caller that needs
    several spans (the mailbox scan + the stage0 block) calls :meth:`prefetch`
    with all of them first -- one session, one devmem batch -- and the
    :meth:`read_words` calls that follow are served from that snapshot.
    :func:`find_diag` does this itself; :func:`prefetch_mailboxes` does it for
    both blocks. A :meth:`read_words` of anything NOT prefetched is a fresh
    session and is never cached, so a caller polling one reader (boot-rate's
    stage0 read after every boot) always sees new values.

    The addresses are expanded here, so the remote side needs no arithmetic.
    ``target`` is the ssh destination -- by default the ``mps3-linux`` alias
    from the user's ssh config (docs/LINUX_HARNESS.md), which carries the
    ProxyJump through the hub. Key-only: BatchMode, no password prompt.

    ``run`` is the test seam: ``(argv) -> (returncode, stdout, stderr)``.
    """

    def __init__(self, target: str = "mps3-linux", *, ssh_argv: Optional[Sequence[str]] = None,
                 devmem: str = "devmem", timeout: float = SSH_DEVMEM_TIMEOUT_S,
                 run: Optional[Callable[[List[str]], Tuple[int, str, str]]] = None):
        from .linux import ssh_argv as _ssh_argv  # local: avoid an import cycle
        self.target = target
        self._ssh = list(ssh_argv) if ssh_argv is not None else _ssh_argv(target, batch=True)
        self.devmem = devmem
        self.timeout = timeout
        self._run = run or self._subprocess_run
        self._cache: Dict[int, Optional[int]] = {}   # addr -> value; None = devmem failed
        self._skipped: set = set()                    # addrs the stop guard did not read
        self._cache_err = ""                          # the prefetch session's stderr

    def _subprocess_run(self, argv: List[str]) -> Tuple[int, str, str]:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            raise MailboxError("ssh read timed out after %ss" % self.timeout) from exc
        except OSError as exc:
            raise MailboxError("cannot run ssh: %s" % exc) from exc
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    # -- the command --------------------------------------------------------- #

    def batch_command(self, ranges: Sequence[Tuple[int, int]],
                      stop_if: Sequence[Tuple[int, int]] = ()) -> str:
        """ONE remote shell line reading every word of ``ranges`` (merged,
        ascending): a ``for a in <addrs>`` loop per range, one output line per
        word (see ``_ERR_TOKEN``), then ``@MBX-END <n>``.

        ``stop_if``: ``(addr, value)`` pairs. Right after the range holding
        ``addr`` is read, ``addr`` is probed once more; if it reads ``value``,
        every LATER range answers ``@MBX-SKIP`` per word instead of reading.
        """
        ranges = _merge_ranges(ranges)
        d = shlex.quote(self.devmem)
        read = "%s $a 32 || echo %s" % (d, _ERR_TOKEN)
        guarded = 'if [ -n "$%s" ]; then echo %s; else %s; fi' % (_STOP_VAR, _SKIP_TOKEN, read)
        parts: List[str] = []
        probed = False
        for i, (addr, count) in enumerate(ranges):
            addrs = " ".join("0x%X" % (addr + 4 * k) for k in range(count))
            parts.append("for a in %s; do %s; done" % (addrs, guarded if probed else read))
            if i == len(ranges) - 1:
                break                     # nothing after the last range to guard
            if i == 0 and stop_if:
                parts.append("%s=" % _STOP_VAR)
            for p, v in stop_if:
                if addr <= p < addr + 4 * count:
                    parts.append('[ -n "$%s" ] || case $(%s 0x%X 32 2>/dev/null) in %s) %s=1;; esac'
                                 % (_STOP_VAR, d, p, _hex_glob(v), _STOP_VAR))
                    probed = True
        parts.append("echo %s %d" % (_END_TOKEN, sum(c for _, c in ranges)))
        return "; ".join(parts)

    def remote_command(self, addr: int, count: int) -> str:
        return self.batch_command([(addr, count)])

    def argv(self, addr: int, count: int) -> List[str]:
        return self._ssh + [self.remote_command(addr, count)]

    # -- one session --------------------------------------------------------- #

    def _run_batch(self, ranges: Sequence[Tuple[int, int]],
                   stop_if: Sequence[Tuple[int, int]] = ()):
        """Run one batch; ``(addrs, tokens, stderr)`` with one token per addr:
        an int, ``_ERR_TOKEN`` or ``_SKIP_TOKEN``. Raises :class:`MailboxError`
        -- saying how many words came back -- unless EVERY word answered and
        the end marker arrived."""
        ranges = _merge_ranges(ranges)
        addrs = [a + 4 * k for a, c in ranges for k in range(c)]
        cmd = self.batch_command(ranges, stop_if)
        where = ("at 0x%X" % addrs[0] if len(ranges) == 1
                 else "in %d spans from 0x%X" % (len(ranges), addrs[0]))
        if len(cmd) > _MAX_CMD_CHARS:
            raise MailboxError("a %d-word devmem batch %s is a %d-char command, over "
                               "dropbear's limit -- read fewer words per call"
                               % (len(addrs), where, len(cmd)))
        rc, out, err = self._run(self._ssh + [cmd])
        toks: List[object] = []
        end = None
        for line in out.splitlines():
            s = line.strip()
            m = _DEVMEM_LINE.match(line)
            if m:
                toks.append(int(m.group(1), 16))
            elif s in (_ERR_TOKEN, _SKIP_TOKEN):
                toks.append(s)
            elif s.split()[:1] == [_END_TOKEN]:
                end = s.split()[1:]
                break
        got = "%d of %d word(s) came back %s" % (len(toks), len(addrs), where)
        if rc != 0:
            raise MailboxError("devmem read over ssh failed (rc=%d) -- %s: %s"
                               % (rc, got, (err or out).strip()[-300:] or "(no output)"))
        if end is None:
            raise MailboxError("devmem reply cut short: no end marker (%s), the remote "
                               "shell did not finish -- %s: %r" % (_END_TOKEN, got, out[-200:]))
        if len(toks) != len(addrs) or end != [str(len(addrs))]:
            raise MailboxError("devmem reply garbled -- %s (end marker %r): %r"
                               % (got, " ".join(end), out[:200]))
        return addrs, toks, err

    def _values(self, addrs: Sequence[int], vals: Sequence[object], err: str) -> List[int]:
        for a, v in zip(addrs, vals):
            if not isinstance(v, int):
                raise MailboxError("devmem could not read 0x%X over ssh: %s"
                                   % (a, err.strip()[-300:] or "(no stderr)"))
        return [int(v) for v in vals]  # type: ignore[arg-type]

    def prefetch(self, ranges: Sequence[Tuple[int, int]], *,
                 stop_if: Sequence[Tuple[int, int]] = ()) -> None:
        """Read every word of ``ranges`` in ONE ssh session and keep them, so
        :meth:`read_words` over them needs no further session. A snapshot:
        :meth:`drop_cache` discards it. A no-op when every word is already
        held (or was skipped by the guard). A word devmem could not read is
        held as a failure -- reading it raises, as a fresh read would.
        Raises :class:`MailboxError` when the session fails or not one word
        could be read (``/dev/mem`` refused: devmem's stderr says why)."""
        ranges = _merge_ranges(ranges)
        want = [a + 4 * k for a, c in ranges for k in range(c)]
        if all(a in self._cache or a in self._skipped for a in want):
            return
        addrs, toks, err = self._run_batch(ranges, stop_if)
        read = [t for t in toks if t != _SKIP_TOKEN]
        if read and all(t == _ERR_TOKEN for t in read):
            raise MailboxError("devmem could not read any of %d word(s) over ssh: %s"
                               % (len(read), err.strip()[-300:] or "(no stderr)"))
        for a, t in zip(addrs, toks):
            if t == _SKIP_TOKEN:
                self._skipped.add(a)
                self._cache.pop(a, None)
            else:
                self._cache[a] = t if isinstance(t, int) else None
                self._skipped.discard(a)
        self._cache_err = err

    def drop_cache(self) -> None:
        self._cache.clear()
        self._skipped.clear()
        self._cache_err = ""

    def _read_skipped(self) -> None:
        """A caller wants a word the stop guard skipped (find_diag rejected the
        hit the guard stopped on -- magic but a junk version): read EVERY
        skipped word in one more session, not one session per probe."""
        addrs, toks, err = self._run_batch(_merge_ranges((a, 1) for a in sorted(self._skipped)))
        for a, t in zip(addrs, toks):
            self._cache[a] = t if isinstance(t, int) else None
        self._skipped.clear()
        self._cache_err = "\n".join(e for e in (self._cache_err, err) if e)

    def read_words(self, addr: int, count: int) -> List[int]:
        if count <= 0:
            return []
        want = [addr + 4 * i for i in range(count)]
        if any(a in self._skipped for a in want):
            self._read_skipped()
        if all(a in self._cache for a in want):
            return self._values(want, [self._cache[a] for a in want], self._cache_err)
        addrs, toks, err = self._run_batch([(addr, count)])
        return self._values(addrs, toks, err)

    def write_readback_restore(self, addr: int, pattern: int) -> int:
        """Save ``addr``, write ``pattern``, read it back, restore the saved
        value -- all in ONE ssh session, so the register is never left holding
        the pattern if the connection drops between steps. Returns the
        read-back. The tier-3 CSR liveness probe (bug #1) over ssh."""
        d = shlex.quote(self.devmem)
        a = "0x%X" % addr
        cmd = ("o=$(%s %s 32) || exit 1; %s %s 32 0x%X || exit 1; r=$(%s %s 32); "
               "%s %s 32 $o; echo $r" % (d, a, d, a, pattern & 0xFFFFFFFF, d, a, d, a))
        rc, out, err = self._run(self._ssh + [cmd])
        if rc != 0:
            raise MailboxError("devmem write-readback at 0x%X over ssh failed (rc=%d): %s"
                               % (addr, rc, (err or out).strip() or "(no output)"))
        vals = [int(m.group(1), 16) for m in map(_DEVMEM_LINE.match, out.splitlines()) if m]
        if len(vals) != 1:
            raise MailboxError("devmem write-readback at 0x%X: expected one value, got %r"
                               % (addr, out[:200]))
        return vals[0]
