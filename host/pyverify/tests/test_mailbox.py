"""pyverify.mailbox: the diag mailbox anchor at every LMB size (128 KiB for the
MicroBlaze V), the ascending alias-safe scan, the stage0 status block, and the
ssh/devmem reader -- plus the two drift gates that tie the embedded layouts to
their single C definitions (diag.h's X-macro, stage0_status.h's X-macro)."""
from __future__ import annotations

import fnmatch
import importlib.util
import json
import re
import shlex
import shutil
import struct
import subprocess

import pytest

from pyverify.fielded import repo_root
from pyverify.mailbox import (
    DIAG_FIELDS, DIAG_MAGIC, DIAG_STRUCT_V5, DIAG_STRUCT_V8, STAGE0_LAYOUT_SOURCE,
    STAGE0_STATUS_ADDR, STAGE0_STATUS_FIELDS, STAGE0_STATUS_MAGIC, FakeMemory, MailboxError,
    SshDevmemReader, decode_diag, decode_stage0_status, diag_anchor, diag_candidates,
    find_diag, read_stage0_status,
)

ROOT = repo_root()


# --------------------------------------------------------------------------- #
# Anchors
# --------------------------------------------------------------------------- #

def test_anchor_formula_at_every_lmb_size():
    assert diag_anchor(128) == 0x1FF00          # the MicroBlaze V (plan §3, SHELL §3)
    assert diag_anchor(256) == 0x3FF00
    assert diag_anchor(1024) == 0xFFF00
    assert diag_anchor(256, DIAG_STRUCT_V5) == 0x3FF80
    with pytest.raises(ValueError):
        diag_anchor(0)
    with pytest.raises(ValueError):
        diag_anchor(128, 0x40)


def test_candidates_are_ascending_and_start_at_the_mbv_anchor():
    c = diag_candidates()
    assert c == sorted(c)
    assert c[0] == 0x1FF00 and 0x1FF80 in c and 0xFFF80 in c


def _mailbox_words(version=8, n=64, fill=None):
    w = [0] * n
    w[0], w[1] = DIAG_MAGIC, version
    for i in range(2, min(n, len(DIAG_FIELDS))):
        w[i] = fill(i) if fill else i * 10
    return w


def test_find_diag_on_a_128k_lmb_that_aliases():
    """The MBV case: the LMB decode wraps at 128 KiB, so EVERY larger
    candidate reads back the same magic. Only the ascending scan reports the
    true base."""
    mem = FakeMemory(alias_kb=128)
    mem.poke(0x1FF00, _mailbox_words())
    mb = find_diag(mem)
    assert mb.base == 0x1FF00 and mb.lmb_kb == 128 and mb.layout.startswith("v8")
    assert mb.get("icap_bytes") == 5 * 10
    assert mb.as_wire()["icap_bytes"] == 50 and "magic" not in mb.as_wire()


def test_find_diag_falls_through_to_a_bare_metal_v7_image():
    mem = FakeMemory()
    mem.poke(0x3FF80, _mailbox_words(version=7, n=32))
    mb = find_diag(mem)
    assert mb.base == 0x3FF80 and mb.layout.startswith("v5-v7")
    # a v8-only field is ABSENT at a 0x80 anchor, never a fabricated 0
    assert mb.get("svc_count") is None and mb.get("touch_probe_verdict") is not None


def test_magic_alone_is_not_a_mailbox():
    """Ordinary RAM that happens to hold the magic, with a junk version beside
    it, must not be taken for the mailbox."""
    mem = FakeMemory()
    mem.poke(0x1FF00, [DIAG_MAGIC, 0x12345678])      # not a diag version
    mem.poke(0x3FF00, _mailbox_words())
    assert find_diag(mem).base == 0x3FF00


def test_no_mailbox_names_every_address_tried():
    with pytest.raises(MailboxError) as ei:
        find_diag(FakeMemory(), (128,))
    assert "0x1FF00" in str(ei.value) and "0x1FF80" in str(ei.value)


def test_decode_rejects_a_non_anchor_and_a_missing_magic():
    with pytest.raises(MailboxError):
        decode_diag(0x1FF40, _mailbox_words())
    with pytest.raises(MailboxError):
        decode_diag(0x1FF00, [0, 8] + [0] * 62)


# --------------------------------------------------------------------------- #
# Drift gates against the C definitions
# --------------------------------------------------------------------------- #

def _load_gen_diag():
    path = ROOT / "tools" / "gen_diag.py"
    assert path.is_file(), "tools/gen_diag.py is gone -- re-point this drift gate"
    import sys
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("gen_diag_for_pyverify", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # dataclasses resolve their module
    spec.loader.exec_module(mod)
    return mod


def test_diag_fields_match_diag_h():
    """pyverify's embedded (field, wire key) list == diag.h's MPS3_DIAG_FIELDS
    minus the PAD, parsed by the same generator that renders the other readers.
    A field appended to diag.h without this list following it fails here."""
    gd = _load_gen_diag()
    text = (ROOT / "firmware" / "common" / "diag.h").read_text()
    fields = gd.readout(gd.parse_fields(text))
    assert [(f.name, f.key) for f in fields] == list(DIAG_FIELDS)
    assert [f.word_offset for f in fields] == list(range(len(DIAG_FIELDS)))


_S0_ROW = re.compile(r'X\((\w+),\s*(0x[0-9A-Fa-f]+),\s*"')


def test_stage0_snapshot_matches_stage0_status_h():
    """The embedded snapshot (what a hub-staged pyverify decodes with) ==
    STAGE0's header. In-tree, the header is read directly; this gate is what
    keeps the vendored copy honest."""
    from pyverify import mailbox as mbx
    hdr = ROOT / "src" / "linux_soc" / "hw" / "fw_stage0" / "stage0_status.h"
    assert hdr.is_file(), "stage0_status.h (STAGE0's one layout definition) is missing"
    text = hdr.read_text()
    rows = [(n, int(o, 16)) for n, o in _S0_ROW.findall(text)]
    assert rows, "the S0_STATUS_FIELDS parser matched nothing -- fix the regex"
    assert list(mbx._STAGE0_SNAPSHOT) == rows, (
        "pyverify.mailbox._STAGE0_SNAPSHOT has drifted from stage0_status.h. "
        "Replace the snapshot with:\n" + "".join("    (%r, 0x%02X),\n" % r for r in rows))
    # in-tree, the live table is STAGE0's own decoder's, which its layout gate
    # holds to the same header
    assert STAGE0_LAYOUT_SOURCE == "stage0_status.py (in-tree)"
    assert list(STAGE0_STATUS_FIELDS[:-1]) == rows
    assert STAGE0_STATUS_FIELDS[-1] == ("magic_end", 0xFC)
    m = re.search(r"S0_STATUS_MAGIC\s+(0x[0-9A-Fa-f]+)", text)
    assert m and int(m.group(1), 16) == STAGE0_STATUS_MAGIC
    assert STAGE0_STATUS_ADDR == int(re.search(r"S0_STATUS_ADDR\s+(0x[0-9A-Fa-f]+)", text).group(1), 16)


# --------------------------------------------------------------------------- #
# stage0 status block
# --------------------------------------------------------------------------- #

def _stage0_block(**kw):
    w = [0] * 64
    offs = dict(STAGE0_STATUS_FIELDS)
    vals = {"magic": STAGE0_STATUS_MAGIC, "version": 1, "size": 0x100,
            "magic_end": STAGE0_STATUS_MAGIC}
    vals.update(kw)
    for k, v in vals.items():
        w[offs[k] // 4] = v
    return w


def test_stage0_decode_and_summary():
    from pyverify.mailbox import STAGE0_CONFIRM_MAGIC
    st = decode_stage0_status(_stage0_block(
        boot_count=3, phase=6, booted_from=2, ddr_calib=1, sd_result=1,
        n_fallback=1, last_error=(3 << 16) | 6, reset_cause=0x8,
        att_confirm=STAGE0_CONFIRM_MAGIC))
    s = st.summary()
    assert st.valid and s["phase"] == "HANDOFF" and s["booted_from"] == "B"
    assert s["last_error"] == "SLOT_A:6" and s["watchdog_reset"] and s["linux_confirmed"]
    assert s["boot_count"] == 3 and s["n_fallback"] == 1


def test_stage0_blank_and_torn():
    assert decode_stage0_status([0] * 64).blank
    torn = decode_stage0_status(_stage0_block(magic_end=0))
    assert not torn.valid and not torn.blank


def test_read_stage0_status_reads_the_right_window():
    mem = FakeMemory()
    mem.poke(STAGE0_STATUS_ADDR, _stage0_block(phase=5, rescue_state=1))
    st = read_stage0_status(mem)
    assert mem.reads == [(0x1FE00, 64)]
    assert st.summary()["rescue_state"] == "LISTEN"


# --------------------------------------------------------------------------- #
# SshDevmemReader
# --------------------------------------------------------------------------- #

#: One statement of a read command the reader builds: a ``for a in`` loop over
#: literal addresses, a stop-guard probe, or the end marker. Parsed in order, so
#: a guard only affects the loops after it -- the same as the remote shell.
_STMT = re.compile(
    r"for a in (?P<addrs>[^;]*); do (?P<body>.*?); done"
    r"|case \$\(devmem (?P<paddr>0[xX][0-9A-Fa-f]+) 32[^)]*\) in (?P<pat>[^)]+)\) (?P<svar>\w+)=1"
    r"|echo (?P<end>@MBX-END \d+)")


def _play(mem, cmd, fault=(), touched=None):
    """Play the remote shell + BusyBox devmem for a read command, against a
    FakeMemory. Understands BOTH the old one-loop ``|| exit 1`` form and the
    batched form (several loops, ``|| echo <err token>``, a ``_mbx_stop``-style
    guard, the end marker) by reading the tokens out of the command itself.
    ``fault`` addresses fail the way a bus error does (devmem killed, rc 135)."""
    out, err, var = [], [], {}
    for m in _STMT.finditer(cmd):
        if m.group("addrs") is not None:
            body = m.group("body")
            skip = re.search(r'if \[ -n "\$(\w+)" \]; then echo (\S+); else', body)
            onerr = re.search(r"\|\| echo ([^\s;]+)", body)
            for a in m.group("addrs").split():
                if skip and var.get(skip.group(1)):
                    out.append(skip.group(2))
                    continue
                addr = int(a, 16)
                if touched is not None:
                    touched.append(addr)
                if addr in fault:
                    err.append("Bus error")
                    if onerr:
                        out.append(onerr.group(1))
                        continue
                    return 1, "".join(o + "\n" for o in out), "\n".join(err)   # || exit 1
                out.append("0x%08X" % mem.read_words(addr, 1)[0])
        elif m.group("paddr") is not None:
            if var.get(m.group("svar")):
                continue                     # [ -n "$stop" ] || case ...
            addr = int(m.group("paddr"), 16)
            if touched is not None:
                touched.append(addr)
            val = "" if addr in fault else "0x%08X" % mem.read_words(addr, 1)[0]
            if fnmatch.fnmatchcase(val, m.group("pat")):
                var[m.group("svar")] = "1"
        else:
            out.append(m.group("end"))
    return 0, "".join(o + "\n" for o in out), "\n".join(err)


class _Remote:
    """Plays BusyBox devmem against a FakeMemory, parsing the command the
    reader built -- so the argv/parse pair is tested end to end."""

    def __init__(self, mem, rc=0):
        self.mem, self.rc, self.argvs = mem, rc, []

    def __call__(self, argv):
        self.argvs.append(argv)
        cmd = argv[-1]
        if self.rc:
            return self.rc, "", "devmem: /dev/mem: Operation not permitted"
        if cmd.startswith("for a in"):
            return _play(self.mem, cmd)
        m = re.search(r"devmem (0x[0-9A-F]+) 32 (0x[0-9A-F]+)", cmd)
        addr, pat = int(m.group(1), 16), int(m.group(2), 16)
        return 0, "0x%08X\n" % pat, ""


def test_ssh_reader_is_key_only_batch_and_one_round_trip():
    mem = FakeMemory()
    mem.poke(0x1FF00, _mailbox_words())
    rem = _Remote(mem)
    rd = SshDevmemReader("mps3-linux", run=rem)
    assert find_diag(rd, (128,)).base == 0x1FF00
    argv = rem.argvs[0]
    assert argv[0] == "ssh" and "BatchMode=yes" in argv and "PasswordAuthentication=no" in argv
    assert argv[-2] == "mps3-linux"
    # ONE session: find_diag batches its probe AND the body (it used to be two
    # -- the 2-word probe, then the 64-word read -- and B1 v4 timed out on that)
    assert len(rem.argvs) == 1


def test_ssh_reader_failure_is_loud():
    rd = SshDevmemReader("mps3-linux", run=_Remote(FakeMemory(), rc=1))
    with pytest.raises(MailboxError) as ei:
        rd.read_words(0x1FF00, 4)
    assert "Operation not permitted" in str(ei.value)


def test_ssh_reader_short_answer_is_loud():
    rd = SshDevmemReader("mps3-linux", run=lambda argv: (0, "0x00000001\n", ""))
    with pytest.raises(MailboxError):
        rd.read_words(0x1FF00, 2)


def test_write_readback_restore_is_one_session():
    rem = _Remote(FakeMemory())
    rd = SshDevmemReader("mps3-linux", run=rem)
    assert rd.write_readback_restore(0x44A90018, 0x37) == 0x37
    cmd = rem.argvs[-1][-1]
    assert cmd.count("devmem") == 4 and cmd.index("o=$(") < cmd.index("echo $r")
    # the restore writes back the SAVED value, never a constant
    assert "32 $o" in cmd


# --------------------------------------------------------------------------- #
# ONE ssh session per mailbox read (B1 v4, 2026-09-25)
# --------------------------------------------------------------------------- #
# Silicon: `pyverify.cli mailbox --what both --target mps3-b1` failed "ssh read
# timed out after 30.0s" (45 s wall). One ssh login alone took ~14 s on the
# loaded MicroBlaze V, and the reader opened a session per read_words() call:
# the 2-word diag probe, the 64-word body, the stage0 block -- three logins,
# each bounded by 30 s. The fake below COUNTS sessions.

class _Devmem:
    """Counting ssh+devmem fake: every call is one ssh session. ``cut`` keeps
    only the first N reply lines (a reply cut short in transit)."""

    def __init__(self, mem, fault=(), cut=None):
        self.mem, self.fault, self.cut = mem, set(fault), cut
        self.calls, self.touched, self.timeouts = [], [], []

    def __call__(self, argv):
        self.calls.append(list(argv))
        rc, out, err = _play(self.mem, argv[-1], self.fault, self.touched)
        if self.cut is not None:
            out = "".join(out.splitlines(True)[:self.cut])
        return rc, out, err


def _patch_ssh(monkeypatch, fake):
    """Route every SshDevmemReader session (the cli builds its own reader)
    through ``fake``, recording the timeout each session was given."""
    from pyverify import mailbox as mbx

    def run(self, argv):
        fake.timeouts.append(self.timeout)
        return fake(argv)

    monkeypatch.setattr(mbx.SshDevmemReader, "_subprocess_run", run)
    return fake


def _mbv_memory():
    """The MicroBlaze V: a 128 KiB LMB that ALIASES, a v9 mailbox at 0x1FF00
    (word i = 1000 + i) and a valid stage0 block at 0x1FE00."""
    mem = FakeMemory(alias_kb=128)
    mem.poke(0x1FF00, _mailbox_words(version=9, fill=lambda i: 1000 + i))
    mem.poke(STAGE0_STATUS_ADDR, _stage0_block(phase=6, boot_count=3))
    return mem


@pytest.mark.parametrize("what", ["both", "diag", "stage0"])
def test_cli_mailbox_is_one_ssh_session(monkeypatch, capsys, what):
    from pyverify import cli
    fake = _patch_ssh(monkeypatch, _Devmem(_mbv_memory()))
    assert cli.main(["mailbox", "--what", what, "--target", "mps3-b1"]) == 0
    assert len(fake.calls) == 1, (
        "%d ssh sessions for --what %s; B1 v4: one login is ~14 s on the MBV"
        % (len(fake.calls), what))
    assert fake.calls[0][-2] == "mps3-b1" and "BatchMode=yes" in fake.calls[0]
    out = json.loads(capsys.readouterr().out)
    if what != "stage0":
        d = out["diag"]
        assert d["base"] == "0x1FF00" and d["version"] == 9 and d["lmb_kb"] == 128
        assert d["counters"]["icap_bytes"] == 1005        # word 5
        assert d["counters"]["usd_boot"] == 1045          # word 45 (v9)
    if what != "diag":
        s = out["stage0"]
        assert s["valid"] and not s["blank"] and s["phase"] == "HANDOFF"
        assert s["boot_count"] == 3 and s["fields"]["magic"] == STAGE0_STATUS_MAGIC


def test_cli_mailbox_on_the_mbv_never_touches_a_bare_metal_anchor(monkeypatch, capsys):
    """The one batch must not read 0x3FF00+ on a healthy MBV: the old
    per-candidate scan never did (0x1FF00 hits first), and what answers there
    on the MBV's bus is unproven. The remote guard skips those extents once the
    128 KiB anchor shows the magic."""
    from pyverify import cli
    fake = _patch_ssh(monkeypatch, _Devmem(_mbv_memory()))
    assert cli.main(["mailbox"]) == 0
    assert fake.touched and max(fake.touched) < 0x20000, hex(max(fake.touched))


def test_cli_mailbox_falls_through_to_a_bare_metal_anchor_in_the_same_session(
        monkeypatch, capsys):
    """No mailbox at the 128 KiB anchors, a bus error on every 256 KiB word,
    the mailbox at 512 KiB: still ONE session, and the faulting words are the
    'unreadable' candidates they always were, not a dead batch."""
    from pyverify import cli
    mem = FakeMemory()                  # no alias: 0x1FF00 is ordinary RAM
    mem.poke(0x7FF00, _mailbox_words(version=8))
    fake = _patch_ssh(monkeypatch, _Devmem(mem, fault=range(0x3FF00, 0x40000, 4)))
    assert cli.main(["mailbox", "--what", "both"]) == 0
    assert len(fake.calls) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["diag"]["base"] == "0x7FF00" and out["diag"]["lmb_kb"] == 512
    assert out["stage0"]["blank"]
    assert 0xFFF00 not in fake.touched          # the guard stopped after 0x7FF00


def test_ssh_devmem_timeout_is_at_least_90s(monkeypatch, capsys):
    """B1 v4: a login alone was ~14 s under load; 30 s per session failed."""
    from pyverify import cli
    from pyverify import mailbox as mbx
    assert getattr(mbx, "SSH_DEVMEM_TIMEOUT_S", 0) >= 90.0
    assert SshDevmemReader("mps3-linux").timeout >= 90.0
    fake = _patch_ssh(monkeypatch, _Devmem(_mbv_memory()))
    assert cli.main(["mailbox"]) == 0
    assert fake.timeouts and min(fake.timeouts) >= 90.0


def test_cli_mailbox_timeout_flag(monkeypatch, capsys):
    from pyverify import cli
    fake = _patch_ssh(monkeypatch, _Devmem(_mbv_memory()))
    assert cli.main(["mailbox", "--timeout", "150"]) == 0
    assert fake.timeouts == [150.0]


def test_cli_mailbox_short_reply_says_how_many_words_came_back(monkeypatch, capsys):
    from pyverify import cli
    fake = _patch_ssh(monkeypatch, _Devmem(_mbv_memory(), cut=10))
    assert cli.main(["mailbox", "--what", "both"]) == 1
    err = capsys.readouterr().err
    assert re.search(r"\b10 of \d+ word", err), err
    assert len(fake.calls) == 1         # a broken reply is not retried per candidate


def test_reader_short_reply_is_a_clear_error():
    rd = SshDevmemReader("mps3-linux", run=lambda argv: (0, "0x00000001\n", ""))
    with pytest.raises(MailboxError, match=r"\b1 of 2 word"):
        rd.read_words(0x1FF00, 2)


def test_reader_garbled_reply_is_a_clear_error():
    reply = "0x00000001\n0xZZ\n@MBX-END 2\n"
    rd = SshDevmemReader("mps3-linux", run=lambda argv: (0, reply, ""))
    with pytest.raises(MailboxError, match=r"\b1 of 2 word"):
        rd.read_words(0x1FF00, 2)


def test_reader_reply_without_the_end_marker_is_an_error():
    """Every value present but no end marker: the remote shell never finished."""
    rd = SshDevmemReader("mps3-linux", run=lambda argv: (0, "0x00000001\n0x00000002\n", ""))
    with pytest.raises(MailboxError, match="end marker"):
        rd.read_words(0x1FF00, 2)


def test_reader_word_level_failure_names_the_address_and_stderr():
    mem = FakeMemory()
    fake = _Devmem(mem, fault={0x1FF04})
    rd = SshDevmemReader("mps3-linux", run=fake)
    with pytest.raises(MailboxError) as ei:
        rd.read_words(0x1FF00, 4)
    assert "0x1FF04" in str(ei.value) and "Bus error" in str(ei.value)


@pytest.mark.parametrize("lmbs", [(128,), (256,), (128, 256, 512, 1024)])
def test_the_read_plan_covers_every_read_find_diag_can_make(lmbs):
    """For a mailbox at EACH candidate, one prefetch serves find_diag and
    read_stage0_status with no further session: the plan holds every
    candidate's full extent plus the stage0 block."""
    from pyverify import mailbox as mbx
    for hit in diag_candidates(lmbs):
        v8 = (hit & 0xFF) == 0
        mem = FakeMemory()
        mem.poke(hit, _mailbox_words(version=8 if v8 else 7,
                                     n=(DIAG_STRUCT_V8 if v8 else DIAG_STRUCT_V5) // 4))
        mem.poke(STAGE0_STATUS_ADDR, _stage0_block())
        fake = _Devmem(mem)
        rd = SshDevmemReader("mps3-linux", run=fake)
        mbx.prefetch_mailboxes(rd, "both", lmbs)
        mb = find_diag(rd, lmbs)
        st = read_stage0_status(rd)
        assert (len(fake.calls), mb.base, st.valid) == (1, hit, True), hex(hit)


def test_a_skipped_extent_is_read_fresh_never_served_as_data():
    """The guard stops on the MAGIC; find_diag also wants a plausible version.
    Magic + junk version at 0x1FF00 sends find_diag on to 0x3FF00, which the
    batch skipped -- that read must go to a fresh session, not the cache."""
    from pyverify import mailbox as mbx
    mem = FakeMemory()
    mem.poke(0x1FF00, [DIAG_MAGIC, 0x12345678])
    mem.poke(0x3FF00, _mailbox_words())
    fake = _Devmem(mem)
    rd = SshDevmemReader("mps3-linux", run=fake)
    mbx.prefetch_mailboxes(rd, "diag", (128, 256))
    assert find_diag(rd, (128, 256)).base == 0x3FF00
    assert len(fake.calls) == 2


def test_prefetch_is_a_snapshot_plain_reads_stay_fresh():
    """read_words() outside a prefetch never caches: boot-rate polls stage0 on
    ONE reader across reboots and must see each new value."""
    mem = FakeMemory()
    fake = _Devmem(mem)
    rd = SshDevmemReader("mps3-linux", run=fake)
    mem.poke(STAGE0_STATUS_ADDR, [1])
    assert rd.read_words(STAGE0_STATUS_ADDR, 1) == [1]
    mem.poke(STAGE0_STATUS_ADDR, [2])
    assert rd.read_words(STAGE0_STATUS_ADDR, 1) == [2]
    assert len(fake.calls) == 2


def test_prefetch_all_words_failing_is_one_clear_error():
    """/dev/mem refused for every word: say so once, with devmem's stderr,
    instead of eight '(unreadable)' candidates."""
    from pyverify import mailbox as mbx
    mem = FakeMemory()
    everything = set(range(0x1FE00, 0x100000, 4))
    rd = SshDevmemReader("mps3-linux", run=_Devmem(mem, fault=everything))
    with pytest.raises(MailboxError, match="Bus error"):
        mbx.prefetch_mailboxes(rd, "both")


class _PosixSh:
    """Runs the reader's command in a REAL POSIX shell (``sh -c``), with
    ``devmem`` a shell function answering from a FakeMemory and logging each
    address it is asked for -- so the shell syntax of the batch (loops, the
    stop guard, the markers) is executed, not just pattern-matched."""

    def __init__(self, mem, log, fault=()):
        self.mem, self.log, self.fault, self.calls = mem, log, set(fault), 0

    def __call__(self, argv):
        self.calls += 1
        cmd = argv[-1]
        arms = []
        for a in sorted({int(x, 16) for x in re.findall(r"\b0x[0-9A-Fa-f]+\b", cmd)}):
            if a in self.fault:
                arms.append('0x%X) echo "devmem: Bus error" >&2; return 135;;' % a)
            else:
                arms.append("0x%X) echo 0x%08X;;" % (a, self.mem.read_words(a, 1)[0]))
        fn = ('devmem() { echo "$1" >> %s; case "$1" in %s '
              '*) echo "no fake for $1" >&2; return 1;; esac; }'
              % (shlex.quote(str(self.log)), " ".join(arms)))
        p = subprocess.run(["sh", "-c", fn + "\n" + cmd], capture_output=True,
                           text=True, timeout=60)
        return p.returncode, p.stdout, p.stderr


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
def test_the_batch_runs_in_a_real_posix_shell_mbv(tmp_path):
    from pyverify import mailbox as mbx
    log = tmp_path / "devmem.log"
    sh = _PosixSh(_mbv_memory(), log)
    rd = SshDevmemReader("mps3-linux", run=sh)
    mbx.prefetch_mailboxes(rd, "both")
    mb, st = find_diag(rd), read_stage0_status(rd)
    assert sh.calls == 1 and mb.base == 0x1FF00 and mb.get("usd_boot") == 1045
    assert st.valid and st.summary()["phase"] == "HANDOFF"
    touched = [int(x, 16) for x in log.read_text().split()]
    assert max(touched) < 0x20000               # the guard held in a real shell


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
def test_the_batch_runs_in_a_real_posix_shell_fallthrough(tmp_path):
    from pyverify import mailbox as mbx
    log = tmp_path / "devmem.log"
    mem = FakeMemory()
    mem.poke(0x7FF00, _mailbox_words(version=8))
    sh = _PosixSh(mem, log, fault=range(0x3FF00, 0x40000, 4))
    rd = SshDevmemReader("mps3-linux", run=sh)
    mbx.prefetch_mailboxes(rd, "both")
    assert find_diag(rd).base == 0x7FF00 and read_stage0_status(rd).blank
    assert sh.calls == 1
    touched = {int(x, 16) for x in log.read_text().split()}
    assert 0x3FF00 in touched and 0x7FF00 in touched and 0xFFF00 not in touched
