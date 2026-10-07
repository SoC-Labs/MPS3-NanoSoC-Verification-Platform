#!/usr/bin/env python3
"""Thin wrappers over the Verdi FSDB utilities, plus a pure-Python VCD reader/writer.

Stream C (compare pipeline).  See INTERFACES.md §4 and §6.

Everything vendor-specific is confined to this module: tool discovery, argv
construction, log-directory hygiene and the LD_LIBRARY_PATH fix for Identify's
``raw2fsdb``.  The VCD reader/writer below is deliberately pure Python with no
tool dependency at all, so the comparison logic downstream is unit-testable on a
box with no Verdi licence.

Design notes that were *established by experiment*, not assumed (2026-07-29,
``verdi/X-2025.06-SP2``):

* ``nCompare`` DOES have a documented batch interface -- ``-rule <f>.ncr
  -report <f>.nce`` -- and its rule file is Tcl using ``cmp*`` commands.  The
  empty ``[Batch Mode Options]`` sections come from the *older* Verdi on the
  default PATH (``VERDI_2022.06-SP2``); under the X-2025.06-SP2 module ``-h``
  prints the full list.  See ncompare/README.md.
* ``nCompare`` **always exits 0**, even when it finds mismatches.  The verdict
  must be parsed out of the ``.nce`` report.  Never trust its exit status.
* ``fsdbmangle`` is **not** a rename tool.  It is an obfuscator that invents
  *random* signal names (its only interesting flag is ``-seed``).  It cannot
  apply a rename map, so the hardware-side rename is done by pairing names
  explicitly (``cmpSetSignalPair golden secondary``) and, for our own
  comparator, in Python.  INTERFACES.md §3.3 anticipated this by keeping
  ``signal_map.tsv`` tool-neutral.
* ``fsdbqry`` fails on an ordinary FSDB (``*WARN* Only support streamlined
  header file in fsdbqry``); it is for "streamlined header" files.  Signal
  listing therefore goes through ``fsdb2vcd -et 0``, which emits the full
  ``$var`` header and essentially no value data, so it is cheap.
* Every Verdi utility drops a ``<tool>Log/`` directory in the CWD.  Every
  wrapper here forces it under a caller-chosen log directory.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

# --------------------------------------------------------------------------
# FROZEN exit codes -- INTERFACES.md §4.  Nothing in the compare pipeline may
# invent another code, and a harness error must never be reported as a match.
# --------------------------------------------------------------------------
EXIT_MATCH = 0
EXIT_MISMATCH = 1
EXIT_HARNESS = 2


class HarnessError(Exception):
    """Anything that means "we could not perform a trustworthy comparison".

    Always maps to exit code 2.  Raised rather than returned so that no code
    path can accidentally fall through to a "match" verdict.
    """


class ToolMissing(HarnessError):
    """A required Verdi/Identify executable is not on PATH."""


class ToolFailed(HarnessError):
    """A Verdi/Identify executable ran but reported failure."""


# --------------------------------------------------------------------------
# Tool discovery
# --------------------------------------------------------------------------

#: Where Identify keeps the shared library its own ``raw2fsdb`` forgets about.
#: READ-ONLY vendor tree -- we prepend it to LD_LIBRARY_PATH, we never touch it.
IDENTIFY_LIB_DIR = "/eda/synopsys/2022-23/RHELx86/SFPGA_2022.09-SP2/identify/linux_a_64"

_VERDI_TOOLS = {
    "nCompare",
    "ncmp",
    "fsdbmangle",
    "fsdbjoin",
    "fsdbqry",
    "fsdbreport",
    "fsdbdir",
    "vcd2fsdb",
    "fsdb2vcd",
}

_HOWTO = {
    "verdi": "module load verdi/X-2025.06-SP2   (or verdi/T-2022.06-SP2)",
    "identify": "module load identify/2022.09-SP2",
}


def _candidate_dirs(tool: str) -> List[str]:
    dirs: List[str] = []
    home = os.environ.get("VERDI_HOME")
    if home:
        dirs.append(os.path.join(home, "bin"))
    verdi = shutil.which("verdi")
    if verdi:
        dirs.append(os.path.dirname(os.path.abspath(verdi)))
        dirs.append(os.path.dirname(os.path.realpath(verdi)))
    if tool == "raw2fsdb":
        for sub in ("identify/bin", "fpga/bin"):
            dirs.append(
                os.path.join("/eda/synopsys/2022-23/RHELx86/SFPGA_2022.09-SP2", sub)
            )
    return dirs


def find_tool(tool: str, required: bool = True) -> Optional[str]:
    """Absolute path to a Verdi/Identify executable.

    Looks on PATH first, then at ``$VERDI_HOME/bin`` and the directory holding
    ``verdi``.  Raises :class:`ToolMissing` (-> exit 2) with an actionable
    message rather than degrading to some weaker comparison: a comparator that
    quietly stops comparing is the failure mode this whole harness exists to
    avoid.

    **Never resolves symlinks.**  Every Verdi utility in ``$VERDI_HOME/bin`` is
    a symlink to a single ``.wrapper`` script that dispatches on ``argv[0]``.
    Calling ``os.path.realpath`` collapses them all to ``.wrapper``, which then
    cannot tell which tool it is and dies with exit 127 (``platform/LINUXAMD64/
    bin/.wrapper: No such file or directory``).  The link itself must be exec'd.
    """
    found = shutil.which(tool)
    if found:
        return os.path.abspath(found)
    for d in _candidate_dirs(tool):
        cand = os.path.join(d, tool)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return os.path.abspath(cand)
    if not required:
        return None
    which_module = "identify" if tool == "raw2fsdb" else "verdi"
    raise ToolMissing(
        "required tool %r not found on PATH, in $VERDI_HOME/bin, or beside "
        "`verdi`.\n  Fix: %s\n  (VERDI_HOME=%r)"
        % (tool, _HOWTO[which_module], os.environ.get("VERDI_HOME", ""))
    )


def have_tool(tool: str) -> bool:
    """True if *tool* can be located.  Never raises."""
    try:
        return find_tool(tool, required=False) is not None
    except HarnessError:  # pragma: no cover - defensive
        return False


def verdi_available() -> bool:
    """True if the FSDB utilities this pipeline actually needs are present."""
    return all(have_tool(t) for t in ("fsdb2vcd", "vcd2fsdb"))


# --------------------------------------------------------------------------
# Running tools
# --------------------------------------------------------------------------

def _run(
    tool: str,
    args: Sequence[str],
    logdir: str,
    env: Optional[Dict[str, str]] = None,
    timeout: int = 1800,
) -> str:
    """Run *tool* with its log droppings confined to *logdir*.

    Verdi utilities create ``<tool>Log/`` relative to the CWD, so we run them
    *inside* ``logdir``.  All file arguments must therefore already be absolute
    -- callers are responsible for that and this is asserted.
    """
    exe = find_tool(tool)
    os.makedirs(logdir, exist_ok=True)
    run_env = dict(os.environ)
    # Stop the "old FSDB version" banner from polluting parsed output, exactly
    # as the vendor's own demo RUN scripts do.
    run_env.setdefault("FSDB_ENV_DISABLE_OLD_FSDB_VER_MSG", "on")
    if env:
        run_env.update(env)
    try:
        proc = subprocess.run(
            [exe] + list(args),
            cwd=logdir,
            env=run_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise ToolFailed("%s timed out after %ds (args: %s)" % (tool, timeout, " ".join(args)))
    out = proc.stdout.decode("utf-8", "replace")
    if proc.returncode != 0:
        raise ToolFailed(
            "%s exited %d\n  args: %s\n  output:\n%s"
            % (tool, proc.returncode, " ".join(args), _indent(out))
        )
    return out


def canon_path(path: str) -> str:
    """Canonical '/'-joined signal path: no leading, trailing or doubled slashes.

    ``signal_map.tsv`` writes hardware paths in Identify's convention with a
    leading slash (``/rp_nanosoc_wrapper/.../HADDR``) while a VCD/FSDB scope
    chain has none.  Comparing the two forms directly makes the §4 set-equality
    precheck fail on every hardware signal, so every path crossing that boundary
    goes through here.
    """
    return "/".join(p for p in str(path).split("/") if p)


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + l for l in text.rstrip().splitlines())


def _abs(path: str, what: str) -> str:
    if not os.path.isabs(path):
        path = os.path.abspath(path)
    return path


# --------------------------------------------------------------------------
# fsdb2vcd / vcd2fsdb
# --------------------------------------------------------------------------

_XTAG_RE = re.compile(r"^\s*(min|max)\s+xtag\s*:\s*\(\s*\d+\s+(\d+)\s*\)\s*$", re.M)


def fsdb_time_range(fsdb: str, logdir: str) -> Tuple[int, int]:
    """``(min_time, max_time)`` of *fsdb*, from ``fsdb2vcd -summary``.

    An IICE trace is **trigger-relative**, so its time axis does not start at 0
    -- a real Identify capture measured here runs ``#10240 .. #20480``.  Anything
    that needs a valid time bound must ask rather than assume.
    """
    fsdb = _abs(fsdb, "fsdb")
    if not os.path.isfile(fsdb):
        raise HarnessError("FSDB not found: %s" % fsdb)
    out = _run("fsdb2vcd", [fsdb, "-summary"], logdir)
    found: Dict[str, int] = {}
    for which, value in _XTAG_RE.findall(out):
        found[which] = int(value)
    if "min" not in found or "max" not in found:
        raise HarnessError(
            "could not read the time range of %s from `fsdb2vcd -summary`\n%s"
            % (fsdb, _indent(out))
        )
    if found["max"] < found["min"]:
        raise HarnessError(
            "FSDB %s reports max time %d < min time %d"
            % (fsdb, found["max"], found["min"])
        )
    return found["min"], found["max"]


def fsdb2vcd(fsdb: str, vcd: str, logdir: str, header_only: bool = False) -> str:
    """Convert *fsdb* to *vcd*.

    ``header_only`` emits the ``$var`` declarations plus a single timestamp -- the
    cheap way to enumerate signals, since ``fsdbqry`` does not work on ordinary
    FSDBs.

    It does this by asking for the file's own **minimum** time
    (``-bt <min> -et <min>``), NOT ``-et 0``.  ``-et 0`` is wrong for exactly the
    class of file this harness exists to read: an IICE trace is trigger-relative,
    so its axis starts at an arbitrary non-zero time, and ``fsdb2vcd`` then dies
    with ``*WARN* End time specified is smaller than FSDB file's minimum time /
    fsdb2vcd failed`` and exit 255 -- turning a perfectly good capture into a
    harness error.  (It does write a usable header before failing, but a tool
    that reported failure must never be trusted for its output.)

    ``-keep_last_time`` is deliberately NOT passed, here or on the data path.
    ``fsdb2vcd`` warns that "the VCD time stops at the last transition by
    default", and that is what we want: the measured capture's last *sample* is
    at 20470 while the FSDB's max time is 20480, one sample period beyond it.
    ``-keep_last_time`` would append a trailing timestamp that is not a sample,
    making a depth-1024 trace read as 1025 samples.  (Re-measured 2026-07-30: it
    does exactly that -- 1025 timestamps, the last carrying no events.)

    That "stops at the last transition" behaviour is also the reason a hardware
    trace with a QUIET TAIL loses its trailing timestamps, which is why
    :func:`fsdb_time_range` exists as a separate call: it reads the same end time
    from ``-summary`` as **metadata**, never as a sample, so nothing on the data
    path can mistake it for one.  See ``crop_trace.sample_grid``.
    """
    fsdb = _abs(fsdb, "fsdb")
    vcd = _abs(vcd, "vcd")
    if not os.path.isfile(fsdb):
        raise HarnessError("FSDB not found: %s" % fsdb)
    args = [fsdb, "-o", vcd]
    if header_only:
        t_min, _ = fsdb_time_range(fsdb, logdir)
        args += ["-bt", str(t_min), "-et", str(t_min)]
    out = _run("fsdb2vcd", args, logdir)
    if not os.path.isfile(vcd):
        raise ToolFailed("fsdb2vcd produced no output for %s\n%s" % (fsdb, _indent(out)))
    return vcd


def vcd2fsdb(vcd: str, fsdb: str, logdir: str) -> str:
    """Convert *vcd* to *fsdb*.  Used to build synthetic/normalised FSDBs."""
    vcd = _abs(vcd, "vcd")
    fsdb = _abs(fsdb, "fsdb")
    if not os.path.isfile(vcd):
        raise HarnessError("VCD not found: %s" % vcd)
    out = _run("vcd2fsdb", [vcd, "-o", fsdb], logdir)
    if not os.path.isfile(fsdb):
        raise ToolFailed("vcd2fsdb produced no output for %s\n%s" % (vcd, _indent(out)))
    return fsdb


def fsdb_summary(fsdb: str, logdir: str) -> Dict[str, str]:
    """``fsdb2vcd -summary`` parsed into a dict (version, var count, ...)."""
    fsdb = _abs(fsdb, "fsdb")
    out = _run("fsdb2vcd", [fsdb, "-summary"], logdir)
    info: Dict[str, str] = {}
    for line in out.splitlines():
        if ":" in line and not line.startswith(" "):
            k, _, v = line.partition(":")
            k = k.strip()
            v = v.strip()
            if k and v and len(k) < 40:
                info[k] = v
    return info


def list_signals(fsdb: str, logdir: str) -> Set[str]:
    """Set of '/'-joined signal paths in *fsdb*, e.g. ``{'iice/haddr', ...}``.

    Used for the INTERFACES.md §4 set-equality precheck.  Bit ranges are
    stripped -- ``$var wire 32 ! haddr [31:0]`` yields ``iice/haddr``.
    """
    tmp = os.path.join(logdir, "_listsig_%d.vcd" % os.getpid())
    os.makedirs(logdir, exist_ok=True)
    try:
        fsdb2vcd(fsdb, tmp, logdir, header_only=True)
        with open(tmp) as fh:
            variables, _, _ = parse_vcd_header(fh)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return {canon_path(v.path) for v in variables}


# --------------------------------------------------------------------------
# nCompare
# --------------------------------------------------------------------------

def ncompare(rule: str, report: str, logdir: str, silence: bool = True) -> str:
    """Run ``nCompare`` in batch mode on a ``.ncr`` rule file.

    ``-logdir`` is ALWAYS passed: without it nCompare drops ``nCompareLog/``
    into the CWD (it did exactly that in the repo root during investigation).

    Returns the path to the ``.nce`` report.  **nCompare exits 0 even on
    mismatch** -- the caller must parse the report for the verdict.
    """
    rule = _abs(rule, "rule")
    report = _abs(report, "report")
    if not os.path.isfile(rule):
        raise HarnessError("nCompare rule file not found: %s" % rule)
    nclog = os.path.join(logdir, "nCompareLog")
    args = ["-logdir", nclog, "-rule", rule, "-report", report]
    if silence:
        args.append("-silence")
    out = _run("nCompare", args, logdir)
    if not os.path.isfile(report):
        raise ToolFailed(
            "nCompare wrote no report %s\n%s" % (report, _indent(out))
        )
    return report


# --------------------------------------------------------------------------
# Identify raw2fsdb  (ships broken: needs LD_LIBRARY_PATH)
# --------------------------------------------------------------------------

def raw2fsdb(args: Sequence[str], logdir: str) -> str:
    """Run Identify's ``raw2fsdb`` with the library path it forgets to set.

    Out of the box it dies with ``error while loading shared libraries:
    libumr3.so``.  The library is present in the install; the modulefile just
    never exports LD_LIBRARY_PATH.  We prepend it **in the child environment
    only** -- the vendor tree is never modified (see the read-only rule in
    CLAUDE.md and INTERFACES.md §6).
    """
    if not os.path.isdir(IDENTIFY_LIB_DIR):
        raise HarnessError(
            "Identify library directory missing: %s\n"
            "  raw2fsdb cannot run without it (libumr3.so)." % IDENTIFY_LIB_DIR
        )
    ld = os.environ.get("LD_LIBRARY_PATH", "")
    new_ld = IDENTIFY_LIB_DIR + (os.pathsep + ld if ld else "")
    return _run("raw2fsdb", args, logdir, env={"LD_LIBRARY_PATH": new_ld})


# --------------------------------------------------------------------------
# Pure-Python VCD reader
# --------------------------------------------------------------------------

class VcdVar(object):
    """One ``$var`` declaration.

    ``path`` is the '/'-joined scope chain plus the leaf name, matching how
    nCompare's ``.nce`` reports names and how INTERFACES.md §1/§3.3 write
    hardware paths.
    """

    __slots__ = ("ident", "name", "width", "scope")

    def __init__(self, ident: str, name: str, width: int, scope: Tuple[str, ...]):
        self.ident = ident
        self.name = name
        self.width = width
        self.scope = scope

    @property
    def path(self) -> str:
        return "/".join(tuple(self.scope) + (self.name,))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "VcdVar(%r, w=%d)" % (self.path, self.width)


_VAR_RE = re.compile(
    r"\$var\s+(?P<type>\S+)\s+(?P<width>\d+)\s+(?P<ident>\S+)\s+(?P<rest>.*?)\$end",
    re.S,
)


def _tokens(fh: Iterable[str]) -> Iterable[str]:
    for line in fh:
        for tok in line.split():
            yield tok


def _parse_decls(tok_iter) -> Tuple[List[VcdVar], str]:
    """Consume the declaration section from *tok_iter*.

    Stops as soon as the ``$end`` closing ``$enddefinitions`` has been consumed,
    leaving *tok_iter* positioned on the first value-change token.  This
    single-shared-iterator arrangement matters: creating a second tokeniser over
    the same file object would silently drop whatever the first one had already
    buffered.
    """
    variables: List[VcdVar] = []
    scope: List[str] = []
    timescale = ""
    buf: List[str] = []
    keyword = None
    for tok in tok_iter:
        if keyword is None:
            if tok.startswith("$"):
                keyword = tok
                buf = []
            continue
        if tok == "$end":
            body = " ".join(buf)
            if keyword == "$var":
                m = _VAR_RE.match("$var " + body + " $end")
                if m:
                    rest = m.group("rest").split()
                    name = rest[0] if rest else ""
                    variables.append(
                        VcdVar(m.group("ident"), name, int(m.group("width")), tuple(scope))
                    )
            elif keyword == "$scope":
                parts = body.split()
                if len(parts) >= 2:
                    scope.append(parts[1])
            elif keyword == "$upscope":
                if scope:
                    scope.pop()
            elif keyword == "$timescale":
                timescale = body.strip()
            elif keyword == "$enddefinitions":
                return variables, timescale
            keyword = None
            buf = []
            continue
        buf.append(tok)
    return variables, timescale


def parse_vcd_header(fh) -> Tuple[List[VcdVar], str, int]:
    """Parse just the declaration section of an already-open VCD.

    Returns ``(variables, timescale_text, 0)``.  The third element is retained
    for call-site compatibility and is always 0.
    """
    variables, timescale = _parse_decls(_tokens(fh))
    return variables, timescale, 0


def normalise_vcd_value(raw: str, width: int) -> str:
    """Expand a VCD value to exactly *width* lowercase bits, MSB first.

    VCD writers drop leading bits.  IEEE 1364 says to left-extend with '0'
    when the given MSB is 0 or 1, and with the MSB itself when it is x or z.
    Getting this wrong silently corrupts every wide signal, so it is unit
    tested.
    """
    raw = raw.strip().lower()
    if not raw:
        raise HarnessError("empty VCD value")
    if len(raw) > width:
        # Some writers emit extra leading zeros; tolerate only redundant ones.
        head, raw2 = raw[: len(raw) - width], raw[len(raw) - width :]
        if set(head) - {"0"}:
            raise HarnessError(
                "VCD value %r wider than declared width %d" % (raw, width)
            )
        raw = raw2
    if len(raw) < width:
        fill = raw[0] if raw[0] in "xz" else "0"
        raw = fill * (width - len(raw)) + raw
    bad = set(raw) - set("01xz")
    if bad:
        raise HarnessError("VCD value %r has non-4-state chars %s" % (raw, sorted(bad)))
    return raw


def parse_vcd(path: str) -> Tuple[List[VcdVar], List[Tuple[int, List[Tuple[str, str]]]]]:
    """Read a VCD into ``(variables, changes)``.

    ``changes`` is a time-ordered list of ``(time, [(ident, raw_value), ...])``
    with one entry per timestamp; values are raw (not width-normalised) so the
    caller can normalise against the declared width.

    Real-valued (``r``) changes are rejected: an IICE trace is 4-state logic,
    and silently dropping signals is exactly the failure mode to avoid.
    """
    variables: List[VcdVar] = []
    changes: List[Tuple[int, List[Tuple[str, str]]]] = []
    with open(path) as fh:
        tok_iter = _tokens(fh)
        variables, _ = _parse_decls(tok_iter)
        cur_time = 0
        cur: List[Tuple[str, str]] = []
        pending_vector: Optional[str] = None
        started = False
        for tok in tok_iter:
            if pending_vector is not None:
                cur.append((tok, pending_vector))
                pending_vector = None
                continue
            if tok.startswith("#"):
                if started:
                    changes.append((cur_time, cur))
                cur_time = int(tok[1:])
                cur = []
                started = True
                continue
            if tok.startswith("$"):
                # $dumpvars / $end / $dumpall / $dumpoff ... : structural only.
                if tok == "$dumpvars" or tok == "$dumpall" or tok == "$dumpon":
                    started = True
                continue
            low = tok[0].lower()
            if low in ("b", "b".upper()):
                pending_vector = tok[1:]
                continue
            if low == "r":
                raise HarnessError(
                    "VCD contains a real-valued signal (%r); an IICE trace must "
                    "be 4-state logic only" % tok
                )
            if low in "01xz":
                cur.append((tok[1:], tok[0]))
                continue
            raise HarnessError("unrecognised VCD token %r" % tok)
        if pending_vector is not None:
            raise HarnessError("VCD ends mid vector-change")
        if started:
            changes.append((cur_time, cur))
    return variables, changes


# --------------------------------------------------------------------------
# Pure-Python VCD writer
# --------------------------------------------------------------------------

_ID_CHARS = "".join(chr(c) for c in range(33, 127))


def vcd_ident(index: int) -> str:
    """Deterministic printable VCD identifier for the *index*-th signal."""
    if index < 0:
        raise HarnessError("negative VCD identifier index")
    out = ""
    n = index
    base = len(_ID_CHARS)
    while True:
        out = _ID_CHARS[n % base] + out
        n = n // base - 1
        if n < 0:
            break
    return out


# Synthetic scope for signals whose manifest path has no scope of its own.
# Identify writes a port of the synthesis top as bare `/<leaf>`, and a VCD needs
# every $var inside some $scope, so those land here. Only the SCOPE is invented;
# the leaf name is untouched, and the hardware side matches on leaf.
TOP_SCOPE = "iice_top"


def write_vcd(
    path: str,
    signals: Sequence[Tuple[str, int]],
    frames: Sequence[Dict[str, str]],
    timescale: str = "1ns",
    version: str = "identify_iice harness",
) -> str:
    """Write a VCD with one timestamp per frame (time == frame index, in ns).

    *signals* is ``[(full_slash_path, width), ...]``; the leading component(s)
    become ``$scope module`` levels and the last is the ``$var`` leaf.  *frames*
    is a list of ``{full_slash_path: bitstring}``; each bitstring must already
    be exactly ``width`` characters from ``01xz`` (use
    :func:`normalise_vcd_value`).

    Sample index is used directly as the time stamp: the compare pipeline works
    in the sample domain (INTERFACES.md §0/§4), so an FSDB round-tripped through
    here is trigger-relative and sample-indexed by construction.
    """
    idents: Dict[str, str] = {}
    widths: Dict[str, int] = {}
    for i, (full, width) in enumerate(signals):
        idents[full] = vcd_ident(i)
        widths[full] = int(width)

    # Build a scope tree so nested $scope/$upscope come out well-formed.
    #
    # A SCOPELESS path is legal and must not be rejected. Identify's own path
    # convention makes a port of the synthesis top just `/<leaf>` -- no scope at
    # all (identify_debug_env_reference.pdf p.14: "the top-level design unit is
    # represented by the initial '/'"), and signals_nanosoc.yaml uses exactly
    # that for its sample clock, `/dut_clk`. Rejecting it made
    # `normalise_hw_capture` unable to re-emit any manifest whose clock is a top
    # port -- hit for real on 2026-07-30 normalising the first silicon capture:
    #   HARNESS ERROR (hw-fsdb): signal path '/dut_clk' has no scope
    # Such signals go in an explicit synthetic top scope instead, so the emitted
    # VCD stays well-formed and the leaf name (which is what the hardware side
    # matches on) is unchanged.
    tree: Dict[Tuple[str, ...], List[str]] = {}
    for full in idents:
        parts = tuple(p for p in full.split("/") if p)
        if len(parts) < 1:
            raise HarnessError(
                "signal path %r is empty; need at least a leaf name" % full
            )
        scope = parts[:-1] or (TOP_SCOPE,)
        tree.setdefault(scope, []).append(full)

    lines: List[str] = []
    lines.append("$date\n\tgenerated\n$end")
    lines.append("$version\n\t%s\n$end" % version)
    lines.append("$timescale\n\t%s\n$end" % timescale)

    def emit_scope(prefix: Tuple[str, ...]) -> None:
        children = sorted({s[: len(prefix) + 1] for s in tree if s[: len(prefix)] == prefix and len(s) > len(prefix)})
        for full in sorted(tree.get(prefix, [])):
            leaf = full.split("/")[-1]
            w = widths[full]
            rng = " [%d:0]" % (w - 1) if w > 1 else ""
            lines.append("$var wire %d %s %s%s $end" % (w, idents[full], leaf, rng))
        for child in children:
            lines.append("$scope module %s $end" % child[-1])
            emit_scope(child)
            lines.append("$upscope $end")

    roots = sorted({s[:1] for s in tree})
    for root in roots:
        lines.append("$scope module %s $end" % root[0])
        emit_scope(root)
        lines.append("$upscope $end")
    lines.append("$enddefinitions $end")

    def value_line(full: str, bits: str) -> str:
        w = widths[full]
        if len(bits) != w:
            raise HarnessError(
                "value %r for %s is %d bits, declared %d" % (bits, full, len(bits), w)
            )
        if w == 1:
            return "%s%s" % (bits, idents[full])
        return "b%s %s" % (bits, idents[full])

    if not frames:
        raise HarnessError("write_vcd: refusing to write an empty trace")

    prev: Dict[str, str] = {}
    for t, frame in enumerate(frames):
        missing = set(idents) - set(frame)
        if missing:
            raise HarnessError(
                "frame %d is missing signals: %s" % (t, sorted(missing)[:5])
            )
        lines.append("#%d" % t)
        if t == 0:
            lines.append("$dumpvars")
            for full in sorted(idents):
                lines.append(value_line(full, frame[full]))
            lines.append("$end")
        else:
            for full in sorted(idents):
                if frame[full] != prev.get(full):
                    lines.append(value_line(full, frame[full]))
        prev = dict(frame)
    # NOTE: deliberately no trailing time anchor.  An extra '#N' would show up
    # as one more timestamp on re-read, and a mismatch on the final sample is
    # detected fine without it (verified against nCompare on the last sample).
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Tiny CLI so the tool discovery can be checked by hand / in CI."""
    import argparse

    ap = argparse.ArgumentParser(description="Verdi FSDB tool probe")
    ap.add_argument("--which", action="store_true", help="report tool locations")
    ap.add_argument("--list-signals", metavar="FSDB")
    ap.add_argument("--summary", metavar="FSDB")
    ap.add_argument("--logdir", default=os.path.join(os.getcwd(), "build", "fsdb_logs"))
    args = ap.parse_args(argv)
    try:
        if args.which:
            for t in sorted(_VERDI_TOOLS | {"raw2fsdb"}):
                p = find_tool(t, required=False)
                print("%-12s %s" % (t, p or "NOT FOUND"))
        if args.list_signals:
            for s in sorted(list_signals(args.list_signals, args.logdir)):
                print(s)
        if args.summary:
            for k, v in sorted(fsdb_summary(args.summary, args.logdir).items()):
                print("%-24s %s" % (k, v))
    except HarnessError as exc:
        print("HARNESS ERROR: %s" % exc, file=sys.stderr)
        return EXIT_HARNESS
    return EXIT_MATCH


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
