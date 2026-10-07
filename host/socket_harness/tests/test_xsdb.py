"""xsdb parse + the ascending LMB-alias candidate scan + the wrong-magic
negative control for :mod:`socket_harness.xsdb`, all via a fake runner.

Everything here is board-free: the pure ``parse_mrd_*`` functions run on
canned text, and :class:`XsdbRegisterEndpoint` / :class:`DiagMailbox` are
driven through a :class:`socket_harness.loopback.FakeXsdbRunner` (a
SubprocessRunner-shaped double keyed by ``{addr: value}``), so NO ``xsdb``
process and no hw_server are ever launched.

Two traps from ``scripts/mps3_diag.tcl`` are pinned here:
  * the candidate bases are scanned ASCENDING — on a 256 KB shell the high
    base aliases down, so newest-first misreports the mailbox address;
  * ``write_word`` NEVER emits ``stop``/``con`` (halting a running MicroBlaze
    mid-swap resets the TCP stream and corrupts a partial transfer).
"""
from __future__ import annotations

import re

import pytest

from socket_harness.loopback import FakeXsdbRunner
from socket_harness.xsdb import (
    DiagMailbox,
    TargetNotFound,
    XsdbConfig,
    XsdbError,
    XsdbRegisterEndpoint,
    parse_mrd_block,
    parse_mrd_value,
)

MAGIC = 0xD1A6C0DE


def _joined(argv: object) -> str:
    if isinstance(argv, (list, tuple)):
        return " ".join(str(a) for a in argv)
    return str(argv)


# --------------------------------------------------------------------------- #
# PURE parse functions (the board-free, mutation-verifiable core)
# --------------------------------------------------------------------------- #


def test_parse_mrd_value_accepts_bare_and_labeled_forms() -> None:
    # `mrd -value -force <addr> 1` prints bare hex; plain `mrd` prints
    # 'AABBCCDD:   00000001' — accept both.
    assert parse_mrd_value("00000001") == 1
    assert parse_mrd_value("44A10010:   00000001") == 1
    assert parse_mrd_value("   DEADBEEF   ") == 0xDEADBEEF


def test_parse_mrd_value_raises_when_no_hex_token() -> None:
    with pytest.raises(XsdbError):
        parse_mrd_value("")
    with pytest.raises(XsdbError):
        parse_mrd_value("ghijk lmnop qrstuvwxyz")  # deliberately zero hex chars


def test_parse_mrd_block_flattens_data_in_address_order_dropping_labels() -> None:
    text = "44A10010:   00000001\n44A10014:   00000002\n44A10018:   00000003"
    assert parse_mrd_block(text, 3) == [1, 2, 3]


def test_parse_mrd_block_accepts_bare_value_stream() -> None:
    assert parse_mrd_block("00000001 00000002 00000003", 3) == [1, 2, 3]


def test_parse_mrd_block_raises_when_fewer_than_requested() -> None:
    with pytest.raises(XsdbError):
        parse_mrd_block("44A10010:   00000001", 2)


# --------------------------------------------------------------------------- #
# XsdbConfig — the LMB-alias trap is encoded in the candidate ORDER
# --------------------------------------------------------------------------- #


def test_xsdbconfig_candidates_are_ascending() -> None:
    cfg = XsdbConfig()
    # BOTH struct sizes, because both are in the field: the mailbox is anchored
    # at (LMB end - sizeof), so diag v8's 256-byte struct sits at ...F00 and the
    # v5..v7 128-byte struct a already-fielded board still runs sits at ...F80.
    assert cfg.candidates == (
        0x0003FF00, 0x0003FF80,
        0x0007FF00, 0x0007FF80,
        0x000FFF00, 0x000FFF80,
    )
    # Ascending is load-bearing TWICE OVER: on a 256 KB shell 0x0007FF80 aliases
    # down to 0x0003FF80, so a newest-first scan would report the wrong base --
    # and on a 256 KB v8 image 0x0003FF80 is an ordinary counter word where a v7
    # image kept its magic.
    assert list(cfg.candidates) == sorted(cfg.candidates)
    assert cfg.magic.upper() == "D1A6C0DE"


def test_xsdbconfig_hub_url_is_the_fqdn_xsdb_requires() -> None:
    # xsdb REQUIRES the FQDN form (mps3_diag.tcl:21 'bare host fails'). The hub is
    # env-supplied (MPS3_HW_URL); XsdbConfig defaults to DEFAULT_HUB_URL. Assert
    # the tcp:...:3121 SHAPE, host-agnostically (no site host baked in).
    assert XsdbConfig().hub_url.startswith("tcp:")
    assert XsdbConfig().hub_url.endswith(":3121")


# --------------------------------------------------------------------------- #
# XsdbRegisterEndpoint — read/write through the fake runner
# --------------------------------------------------------------------------- #


def test_read_word_round_trips_through_the_fake_runner() -> None:
    ep = XsdbRegisterEndpoint(runner=FakeXsdbRunner({0x44A10010: 0x1}))
    assert ep.read_word(0x44A10010) == 1


def test_write_word_emits_mwr_and_never_stop_or_con() -> None:
    # dry_run returns the argv/tcl WITHOUT running — inspect the emitted tcl.
    ep = XsdbRegisterEndpoint(dry_run=True)
    argv = ep.write_word(0x44A10014, 0xABCD)
    tcl = _joined(argv)
    assert "mwr" in tcl
    assert "abcd" in tcl.lower()
    assert "44a10014" in tcl.lower()
    # The mps3_diag.tcl:15 trap: never halt a running MicroBlaze.
    assert "stop" not in tcl
    assert re.search(r"\bcon\b", tcl) is None  # not tripped by 'connect'


def test_dry_run_does_not_invoke_the_subprocess_runner() -> None:
    # dry_run must return the argv/tcl and call the runner ZERO times. Wrap the
    # fake so any invocation is observable.
    fake = FakeXsdbRunner({0x44A10010: 0x1})
    calls: list[object] = []

    def runner(argv, *, timeout_s, input_text=None):  # type: ignore[no-untyped-def]
        calls.append(argv)
        return fake(argv, timeout_s=timeout_s, input_text=input_text)

    ep = XsdbRegisterEndpoint(runner=runner, dry_run=True)
    read_argv = ep.read_word(0x44A10010)
    write_argv = ep.write_word(0x44A10014, 0x2)

    assert "mrd" in _joined(read_argv)
    assert "44a10010" in _joined(read_argv).lower()
    assert "mwr" in _joined(write_argv)
    assert calls == []  # nothing was run


# --------------------------------------------------------------------------- #
# DiagMailbox — the ascending LMB-alias scan (the headline trap)
# --------------------------------------------------------------------------- #


def _endpoint(values: dict[int, int]) -> XsdbRegisterEndpoint:
    return XsdbRegisterEndpoint(runner=FakeXsdbRunner(values))


def test_ascending_scan_256kb_shell_picks_the_low_base() -> None:
    # 256 KB shell: the magic lives at 0x0003FF80 (the true base). The ascending
    # scan hits it FIRST. A distinct version word planted there proves the block
    # was decoded from 0x0003FF80.
    ep = _endpoint({0x0003FF80: MAGIC, 0x0003FF84: 6})
    result = DiagMailbox().read(ep)
    assert result["version"] == 6


def test_ascending_scan_512kb_shell_falls_through_to_the_high_base() -> None:
    # 512 KB shell: 0x0003FF80 is ordinary .bss (no magic); the scan must fall
    # THROUGH to the real mailbox at 0x0007FF80. Newest-first ordering would
    # have stopped at the alias and misreported — this proves the ascending
    # order in XsdbConfig.candidates.
    ep = _endpoint({0x0003FF80: 0x00000000, 0x0007FF80: MAGIC, 0x0007FF84: 7})
    result = DiagMailbox().read(ep)
    assert result["version"] == 7


def test_wrong_magic_everywhere_raises_target_not_found_never_a_silent_zero() -> None:
    # NEGATIVE CONTROL: no candidate carries the magic -> TargetNotFound, NOT a
    # plausible zeroed mailbox (the honest-failure discipline).
    ep = _endpoint({
        0x0003FF80: 0xDEADBEEF,
        0x0007FF80: 0xDEADBEEF,
        0x000FFF80: 0xDEADBEEF,
    })
    with pytest.raises(TargetNotFound):
        DiagMailbox().read(ep)


# --------------------------------------------------------------------------- #
# DiagMailbox — the v8 base move, and reading an OLDER FIELDED image
# --------------------------------------------------------------------------- #


def test_v8_anchor_is_read_sixty_four_words_deep() -> None:
    # diag v8 grew the struct 128 -> 256 B and moved the top-anchored base down
    # to ...F00. The reader must take the LENGTH from the anchor: 64 words.
    ep = _endpoint({0x0003FF00: MAGIC, 0x0003FF04: 8, 0x0003FF94: 0})
    result = DiagMailbox().read(ep)
    assert result["base"] == 0x0003FF00
    assert result["version"] == 8
    assert result["layout_words"] == 64
    assert "missing" not in result
    # The v8-only rows decode rather than falling off the end of the block.
    assert result["svc_skipped_mask"] == 0
    assert result["svc_max_us_5"] == 0


def test_a_fielded_v7_image_still_decodes_and_says_what_it_lacks() -> None:
    # THE REQUIREMENT A BASE MOVE HAS TO MEET. A board in the field is running
    # the 128-byte v7 mailbox at ...F80. New tooling must read it -- 32 words,
    # not 64 (reading 64 there runs off the LMB end, which ALIASES rather than
    # faulting and would come back as plausible garbage).
    ep = _endpoint({0x0003FF80: MAGIC, 0x0003FF84: 7, 0x0003FFF8: 3})
    result = DiagMailbox().read(ep)
    assert result["base"] == 0x0003FF80
    assert result["version"] == 7
    assert result["layout_words"] == 32
    assert result["touch_probe_verdict"] == 3      # word 30, the last v7 row

    # ... and the rows this tooling knows about but that image does not carry
    # are NAMED, never defaulted. A caller handed svc_skipped_mask == 0 from a
    # v7 board would read it as "no service is sick" rather than "this image has
    # no service table" -- a fabricated counter, which is the whole defect class
    # this mailbox exists to close.
    assert "svc_skipped_mask" in result["missing"]
    assert "svc_count" in result["missing"]
    assert "svc_skipped_mask" not in result
    assert "svc_count" not in result


def test_ascending_scan_prefers_the_v8_anchor_on_a_v8_image() -> None:
    # A 256 KB v8 image has its magic at 0x0003FF00 -- and 0x0003FF80, where a
    # v7 image kept ITS magic, is now svc_pass_max_us. Seed that word with the
    # magic bit pattern (a ~3.5e9 us pass; absurd, but this is the control) and
    # the ascending order must still land on the true base.
    ep = _endpoint({
        0x0003FF00: MAGIC, 0x0003FF04: 8,
        0x0003FF80: MAGIC,
    })
    result = DiagMailbox().read(ep)
    assert result["base"] == 0x0003FF00
    assert result["version"] == 8


def test_a_skipped_service_raises_a_named_warning() -> None:
    # A non-zero svc_skipped mask means the superloop has taken a service OUT of
    # the rotation for blowing its budget on MPS3_SVC_SICK_K consecutive passes.
    # That is a wedge in progress and it is invisible in every other counter.
    ep = _endpoint({
        0x0003FF00: MAGIC, 0x0003FF04: 8,
        0x0003FF94: 0b100,     # word 37 == svc_skipped_mask, service 2 is sick
    })
    result = DiagMailbox().read(ep)
    assert result["svc_skipped_mask"] == 0b100
    assert "svc_skipped" in result["service_warning"]
    assert "service.h" in result["service_warning"]

    # ... and a healthy mailbox must NOT carry the key at all (a warning that is
    # always present is a warning nobody reads).
    ok = DiagMailbox().read(_endpoint({0x0003FF00: MAGIC, 0x0003FF04: 8}))
    assert "service_warning" not in ok
