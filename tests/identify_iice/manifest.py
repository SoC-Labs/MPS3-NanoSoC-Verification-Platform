#!/usr/bin/env python3
"""Shared manifest loader + STRICT validator for the sim-vs-hardware IICE trace
harness.

Contract: `tests/identify_iice/INTERFACES.md` §1 (reserved names) and §2 (the
frozen `signals.yaml` schema). Every generator (`gen_idc.py`, `gen_shadow.py`,
`gen_mangle.py`) goes through `load()`, so a bad manifest fails ONCE, loudly,
with every problem listed — rather than three generators each emitting a
differently-broken artifact.

Why the validation is this fussy: the whole point of the manifest is that a
signal name is written in exactly one place, so the sim trace and the hardware
trace cannot silently drift apart. A manifest that is merely *parseable* but
semantically wrong (a trigger signal absent from `trigger.expr`, a `sim` path
accidentally written slash-separated, a signal called `sample_clk`) reproduces
exactly the failure mode the manifest exists to prevent: a clean-looking compare
report that compared the wrong thing. So: no warnings, only errors.

Dependencies: stdlib only. PyYAML is used when importable; when it is not, a
strict-subset YAML parser in this file takes over (see `_MiniYaml`). The repo's
`make check` must not gain a new pip requirement, so `yaml` is never a hard
dependency. `tests/test_generators.py` cross-checks the two parsers against each
other on both shipped manifests whenever PyYAML is present.

Public API (other streams may import this):

    load(path)                  -> Manifest          (raises ManifestError)
    loads(text, name)           -> Manifest
    Manifest.iice               -> IiceCfg
    Manifest.signals            -> list[Signal]
    Manifest.trigger_expr       -> str
    Manifest.signal_names()     -> list[str]
    trigger_identifiers(expr)   -> list[str]         (literal-stripping)
    RESERVED_NAMES, TRIGGER_MARKER, SAMPLE_CLK, SAMPLE_INDEX
"""

from __future__ import annotations

import os
import re
import sys

# --------------------------------------------------------------------------- #
# INTERFACES.md §1 — reserved FSDB leaf names. A manifest signal may not use
# any of these; the harness synthesises them.
# --------------------------------------------------------------------------- #
TRIGGER_MARKER = "trigger_marker"
SAMPLE_CLK = "sample_clk"
SAMPLE_INDEX = "sample_index"
RESERVED_NAMES = (TRIGGER_MARKER, SAMPLE_CLK, SAMPLE_INDEX)

# INTERFACES.md §2 enumerations.
TRIGGER_TIMES = ("early", "middle", "late")
CONTROLLERS = ("none", "counter", "statemachine")
EDGES = ("positive", "negative")
RADICES = ("hex", "bin", "dec", "oct")

# INTERFACES.md §2/§7: "trigger_states: 2..10 (the Identify binary says 2..10;
# its docs also say 2..16 in one place - trust 2..10)". Confirmed: the vendor doc
# genuinely disagrees with the binary, so the bound MUST carry its reason into
# the error message. Otherwise the next person reads p.51, sees 2..16, and files
# a bug against this validator instead of against Synopsys.
TRIGGER_STATES_MIN = 2
TRIGGER_STATES_MAX = 10
TRIGGER_STATES_REASON = (
    "the Identify BINARY enforces 2..10, which is what INTERFACES.md sections 2 "
    "and 7 freeze. Its own documentation disagrees: "
    "identify_debug_env_reference.pdf p.51 says \"The range is 2 to 16\". The "
    "binary wins -- this is NOT a bug in this validator. Also note that p.51 "
    "adds \"powers of 2 are preferable as other integers limit functionality and "
    "do not provide any cost savings\", so prefer 2, 4 or 8")

# INTERFACES.md §2: depth >= 8, no power-of-2 requirement.
DEPTH_MIN = 8

SIGNAL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
IICE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ManifestError(Exception):
    """Raised for any manifest that is unparseable or fails validation.

    `errors` holds one string per distinct problem; `str(exc)` is all of them,
    one per line, prefixed with the manifest's name.
    """

    def __init__(self, source, errors):
        self.source = source
        self.errors = list(errors)
        body = "\n".join("  - " + e for e in self.errors)
        super().__init__("%s: %d manifest error(s)\n%s" % (source, len(self.errors), body))


# --------------------------------------------------------------------------- #
# Fallback YAML — strict subset, used only when PyYAML is unavailable.
# --------------------------------------------------------------------------- #
try:  # pragma: no cover - environment dependent
    import yaml as _pyyaml
except ImportError:  # pragma: no cover - environment dependent
    _pyyaml = None

HAVE_PYYAML = _pyyaml is not None


class _MiniYaml(object):
    """A deliberately tiny, deliberately strict YAML subset parser.

    Supports exactly what the manifests need and rejects everything else:

      * block mappings (``key: value`` / ``key:`` + indented block)
      * block sequences of mappings or scalars (``- ...``)
      * scalars: int, float, ``true``/``false``, single/double-quoted strings,
        plain strings
      * ``#`` comments (quote-aware), blank lines, a leading ``---``

    Deliberately NOT supported (raises): tabs for indentation, flow collections
    ``{}``/``[]``, anchors/aliases/tags, multi-line block scalars ``|``/``>``,
    multiple documents. If a manifest ever needs one of those, install PyYAML
    rather than growing this class — a half-right YAML parser is worse than no
    YAML parser.
    """

    _INT_RE = re.compile(r"^[+-]?\d+$")
    _FLOAT_RE = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?$")

    def __init__(self, text):
        self._lines = self._lex(text)

    # -- lexing ------------------------------------------------------------ #
    @staticmethod
    def _strip_comment(raw):
        """Remove a trailing ``#`` comment, honouring quotes."""
        out = []
        quote = None
        i = 0
        while i < len(raw):
            c = raw[i]
            if quote:
                out.append(c)
                if c == "\\" and quote == '"' and i + 1 < len(raw):
                    out.append(raw[i + 1])
                    i += 2
                    continue
                if c == quote:
                    quote = None
                i += 1
                continue
            if c in ('"', "'"):
                quote = c
                out.append(c)
                i += 1
                continue
            if c == "#" and (not out or out[-1] in (" ", "\t")):
                break
            out.append(c)
            i += 1
        return "".join(out).rstrip()

    def _lex(self, text):
        lines = []
        for lineno, raw in enumerate(text.splitlines(), start=1):
            if "\t" in raw[: len(raw) - len(raw.lstrip())]:
                raise ValueError("line %d: tab used for indentation" % lineno)
            body = self._strip_comment(raw)
            if not body.strip():
                continue
            if body.strip() == "---":
                continue
            if body.strip() == "...":
                continue
            indent = len(body) - len(body.lstrip(" "))
            lines.append((lineno, indent, body.strip()))
        return lines

    # -- scalars ----------------------------------------------------------- #
    @classmethod
    def _scalar(cls, lineno, s):
        s = s.strip()
        if s == "":
            return None
        if s[0] in "[{":
            raise ValueError(
                "line %d: flow collections are not supported by the fallback "
                "YAML parser (use block style, or install PyYAML)" % lineno)
        if s[0] in "&*!|>":
            raise ValueError(
                "line %d: anchors/aliases/tags/block-scalars are not supported "
                "by the fallback YAML parser" % lineno)
        if len(s) >= 2 and s[0] == s[-1] == '"':
            return s[1:-1].replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
        if len(s) >= 2 and s[0] == s[-1] == "'":
            return s[1:-1].replace("''", "'")
        low = s.lower()
        if low in ("true", "yes", "on"):
            return True
        if low in ("false", "no", "off"):
            return False
        if low in ("null", "~"):
            return None
        if cls._INT_RE.match(s):
            return int(s, 10)
        if cls._FLOAT_RE.match(s):
            return float(s)
        return s

    # -- parsing ----------------------------------------------------------- #
    # Every line has already been reduced to (lineno, indent, stripped-body).
    # `_parse_map` / `_parse_seq` are mutually recursive and each consumes the
    # maximal run of lines at exactly `indent`; a deeper indent means "nested
    # block belonging to the previous key", a shallower one ends the block.
    _KEY_RE = re.compile(r"^(\"[^\"]*\"|'[^']*'|[^:\s][^:]*?)\s*:(?:\s+(.*))?$")
    _LOOKS_KEY_RE = re.compile(r"^(\"[^\"]*\"|'[^']*'|[^:\s][^:]*?)\s*:(\s|$)")

    def parse(self):
        if not self._lines:
            return None
        value, idx = self._parse_block(0, self._lines[0][1])
        if idx != len(self._lines):
            raise ValueError("line %d: unexpected indentation" % self._lines[idx][0])
        return value

    @staticmethod
    def _is_seq_entry(body):
        return body == "-" or body.startswith("- ")

    def _parse_block(self, idx, indent):
        if self._is_seq_entry(self._lines[idx][2]):
            return self._parse_seq(idx, indent)
        return self._parse_map(idx, indent)

    def _parse_seq(self, idx, indent):
        items = []
        while idx < len(self._lines):
            lineno, ind, body = self._lines[idx]
            if ind < indent:
                break
            if ind > indent:
                raise ValueError("line %d: unexpected indentation in sequence" % lineno)
            if not self._is_seq_entry(body):
                break

            tail = body[1:]                       # everything after the '-'
            rest = tail.strip()
            # Column at which the item's own content starts, so a `- key: v`
            # item's continuation lines (which are indented to that column) are
            # recognised as further keys of the SAME mapping.
            item_col = indent + 1 + (len(tail) - len(tail.lstrip(" ")))

            if rest == "":
                # `-` alone: the item is the indented block that follows.
                idx += 1
                if idx < len(self._lines) and self._lines[idx][1] > indent:
                    value, idx = self._parse_block(idx, self._lines[idx][1])
                    items.append(value)
                else:
                    items.append(None)
                continue

            if self._LOOKS_KEY_RE.match(rest):
                # `- key: value` — rewrite this line as a plain mapping entry
                # sitting at item_col, then let _parse_map absorb it plus any
                # sibling keys that follow at the same column.
                self._lines[idx] = (lineno, item_col, rest)
                value, idx = self._parse_map(idx, item_col)
                items.append(value)
                continue

            items.append(self._scalar(lineno, rest))
            idx += 1
        return items, idx

    def _parse_map(self, idx, indent):
        out = {}
        while idx < len(self._lines):
            lineno, ind, body = self._lines[idx]
            if ind < indent:
                break
            if ind > indent:
                raise ValueError("line %d: unexpected indentation in mapping" % lineno)
            if self._is_seq_entry(body):
                break

            m = self._KEY_RE.match(body)
            if not m:
                raise ValueError("line %d: not a `key: value` mapping entry: %r"
                                 % (lineno, body))
            key = m.group(1).strip()
            if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
                key = key[1:-1]
            if key in out:
                raise ValueError("line %d: duplicate mapping key %r" % (lineno, key))
            rest = (m.group(2) or "").strip()
            idx += 1

            if rest:
                out[key] = self._scalar(lineno, rest)
                continue
            # `key:` with nothing after it — the value is the following block.
            if idx < len(self._lines) and self._lines[idx][1] > indent:
                out[key], idx = self._parse_block(idx, self._lines[idx][1])
            elif (idx < len(self._lines) and self._lines[idx][1] == indent
                    and self._is_seq_entry(self._lines[idx][2])):
                # A sequence written at the same indent as its key, e.g.
                #   signals:
                #   - name: foo
                out[key], idx = self._parse_seq(idx, indent)
            else:
                out[key] = None
        return out, idx


def parse_yaml(text):
    """Parse `text` with PyYAML if available, else the strict-subset fallback."""
    if HAVE_PYYAML:
        return _pyyaml.safe_load(text)
    return _MiniYaml(text).parse()


def parse_yaml_fallback(text):
    """Parse `text` with the fallback parser regardless of PyYAML.

    Exists so `tests/test_generators.py` can prove the two parsers agree.
    """
    return _MiniYaml(text).parse()


# --------------------------------------------------------------------------- #
# trigger.expr identifier extraction
# --------------------------------------------------------------------------- #
# THE trap in this file. `trigger.expr` is a SystemVerilog expression over
# manifest signal NAMES, e.g.
#
#     htrans == 2'b10 && haddr == 32'h0000_0040
#
# A naive `[A-Za-z_]\w*` scan over that yields `htrans`, `b10`, `haddr` and
# `h0000_0040` — the base character and digits of each sized literal read as
# identifiers, so validation reports two bogus "unknown identifier" errors and
# the manifest is unusable. Every literal form must be deleted BEFORE any
# identifier is extracted. Unit-tested in tests/test_generators.py.
_RE_STR_LIT = re.compile(r'"(?:\\.|[^"\\])*"')
_RE_SYS_CALL = re.compile(r"\$[A-Za-z_][A-Za-z0-9_$]*")
#            size    '   [s]  base   digits/x/z/?/_
_RE_SIZED_LIT = re.compile(r"\b\d[\d_]*\s*'\s*[sS]?[bodhBODH][0-9a-fA-FxXzZ?_]+")
_RE_UNSIZED_LIT = re.compile(r"'\s*[sS]?[bodhBODH][0-9a-fA-FxXzZ?_]+")
_RE_FILL_LIT = re.compile(r"'\s*[01xXzZ]")          # '0 '1 'x 'z
_RE_REAL_LIT = re.compile(r"\b\d[\d_]*\.\d[\d_]*(?:[eE][+-]?\d+)?")
_RE_DEC_LIT = re.compile(r"\b\d[\d_]*\b")
_RE_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")

# Order matters: sized literals must go before bare decimals, or `2'b10`
# becomes `'b10` and then re-matches as an unsized literal by accident (and
# `32'h...` would lose its size and leave `'h...`). Strings and $-system calls
# go first so their contents never reach the literal passes.
_LITERAL_PASSES = (
    _RE_STR_LIT,
    _RE_SYS_CALL,
    _RE_SIZED_LIT,
    _RE_UNSIZED_LIT,
    _RE_FILL_LIT,
    _RE_REAL_LIT,
    _RE_DEC_LIT,
)


def strip_literals(expr):
    """Delete every SystemVerilog literal / system-call token from `expr`.

    Returns the expression with each literal replaced by a single space, so
    token boundaries survive.
    """
    out = expr
    for rx in _LITERAL_PASSES:
        out = rx.sub(" ", out)
    return out


def trigger_identifiers(expr):
    """Ordered, de-duplicated identifiers referenced by a `trigger.expr`.

    Literals are stripped first (see `strip_literals`). No SystemVerilog
    keyword filtering is applied on purpose: `trigger.expr` is defined by
    INTERFACES.md §2 as an expression "over the manifest `name`s ONLY", so any
    bare word that is not a manifest signal is an error, not a keyword.
    """
    seen = []
    for m in _RE_IDENT.finditer(strip_literals(expr)):
        tok = m.group(0)
        if tok not in seen:
            seen.append(tok)
    return seen


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
class Signal(object):
    """One manifest `signals[]` entry (INTERFACES.md §2)."""

    __slots__ = ("name", "width", "hw", "sim", "sample", "trigger", "radix")

    def __init__(self, name, width, hw, sim, sample, trigger, radix):
        self.name = name
        self.width = width
        self.hw = hw
        self.sim = sim
        self.sample = sample
        self.trigger = trigger
        self.radix = radix

    @property
    def is_scalar(self):
        return self.width == 1

    def sv_decl(self):
        """`logic` / `logic [W-1:0]` range text (empty string when scalar)."""
        return "" if self.is_scalar else "[%d:0]" % (self.width - 1)

    def __repr__(self):  # pragma: no cover - debugging aid
        return "Signal(%r, %d, sample=%r, trigger=%r)" % (
            self.name, self.width, self.sample, self.trigger)


class Clock(object):
    __slots__ = ("hw", "sim", "edge")

    def __init__(self, hw, sim, edge):
        self.hw = hw
        self.sim = sim
        self.edge = edge

    @property
    def sv_edge(self):
        return "posedge" if self.edge == "positive" else "negedge"


class IiceCfg(object):
    """IICE build-time configuration.

    `qualified_sampling` / `data_compression` / `always_armed` are OPTIONAL and
    default False, so every existing manifest keeps its exact meaning. They are
    build-time only: `iice sampler`'s `-buffertype`/`-depth`/`-qualified_sampling`
    are rejected by the DEBUGGER shell (measured 2026-07-30 -- "Invalid argument
    on command line: -depth"), so the .idc is the only place they can be set and
    changing one means re-instrument -> Synplify -> place/route -> new DFX
    partial.
    """

    __slots__ = ("name", "depth", "trigger_time", "controller",
                 "trigger_conditions", "trigger_states", "clock",
                 "qualified_sampling", "data_compression", "always_armed")

    def __init__(self, name, depth, trigger_time, controller,
                 trigger_conditions, trigger_states, clock,
                 qualified_sampling=False, data_compression=False,
                 always_armed=False):
        self.name = name
        self.depth = depth
        self.trigger_time = trigger_time
        self.controller = controller
        self.trigger_conditions = trigger_conditions
        self.trigger_states = trigger_states
        self.clock = clock
        self.qualified_sampling = qualified_sampling
        self.data_compression = data_compression
        self.always_armed = always_armed


class Manifest(object):
    """A loaded, VALIDATED manifest. Constructing one never yields a bad state:
    `load()`/`loads()` raise `ManifestError` instead of returning."""

    __slots__ = ("path", "source_name", "iice", "signals", "trigger_expr", "raw")

    def __init__(self, path, source_name, iice, signals, trigger_expr, raw):
        self.path = path
        self.source_name = source_name
        self.iice = iice
        self.signals = signals
        self.trigger_expr = trigger_expr
        self.raw = raw

    def signal_names(self):
        return [s.name for s in self.signals]

    def by_name(self, name):
        for s in self.signals:
            if s.name == name:
                return s
        raise KeyError(name)

    @property
    def shadow_module(self):
        """SystemVerilog module name of the generated shadow (INTERFACES.md §3.2)."""
        return "iice_shadow_%s" % self.iice.name

    @property
    def max_name_len(self):
        return max([len(s.name) for s in self.signals] + [0])


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def _req_int(errs, where, d, key, minimum=None, maximum=None, reason=None):
    """Validate a required integer.

    `reason` is appended to any out-of-range message. Use it whenever the bound
    is not self-evident — a bare "must be <= 10" invites the next reader to open
    the vendor PDF, find a different number, and file a bug against us.
    """
    tail = (" -- %s" % reason) if reason else ""
    if key not in d or d[key] is None:
        errs.append("%s: missing required key `%s`" % (where, key))
        return None
    v = d[key]
    if isinstance(v, bool) or not isinstance(v, int):
        errs.append("%s.%s: must be an integer, got %r" % (where, key, v))
        return None
    if minimum is not None and v < minimum:
        errs.append("%s.%s: must be >= %d, got %d%s" % (where, key, minimum, v, tail))
        return None
    if maximum is not None and v > maximum:
        errs.append("%s.%s: must be <= %d, got %d%s" % (where, key, maximum, v, tail))
        return None
    return v


def _req_enum(errs, where, d, key, allowed):
    if key not in d or d[key] is None:
        errs.append("%s: missing required key `%s`" % (where, key))
        return None
    v = d[key]
    if v not in allowed:
        errs.append("%s.%s: must be one of %s, got %r"
                    % (where, key, "|".join(allowed), v))
        return None
    return v


def _req_str(errs, where, d, key):
    if key not in d or d[key] is None:
        errs.append("%s: missing required key `%s`" % (where, key))
        return None
    v = d[key]
    if not isinstance(v, str) or not v.strip():
        errs.append("%s.%s: must be a non-empty string, got %r" % (where, key, v))
        return None
    return v.strip()


def _opt_bool(errs, where, d, key, default):
    if key not in d or d[key] is None:
        return default
    v = d[key]
    if not isinstance(v, bool):
        errs.append("%s.%s: must be a boolean (true|false), got %r" % (where, key, v))
        return default
    return v


def _check_hw_path(errs, where, path):
    """INTERFACES.md §2: `hw` paths are slash-separated (Identify convention)."""
    if not path.startswith("/"):
        errs.append("%s: hw path must be absolute and slash-separated "
                    "(start with '/'), got %r" % (where, path))
        return
    comps = path.split("/")[1:]
    if not comps or any(c == "" for c in comps):
        errs.append("%s: hw path has an empty component: %r" % (where, path))
        return
    for c in comps:
        if "." in c:
            errs.append("%s: hw path must be slash-separated, but component %r "
                        "contains '.' (looks dot-separated): %r" % (where, c, path))
            return
        if any(ch.isspace() for ch in c):
            errs.append("%s: hw path component %r contains whitespace: %r"
                        % (where, c, path))
            return


def _check_sim_path(errs, where, path):
    """INTERFACES.md §2: `sim` paths are dot-separated and absolute from the TB top."""
    if "/" in path:
        errs.append("%s: sim path must be dot-separated, but contains '/': %r"
                    % (where, path))
        return
    comps = path.split(".")
    if any(c == "" for c in comps):
        errs.append("%s: sim path has an empty component: %r" % (where, path))
        return
    if len(comps) < 2:
        errs.append("%s: sim path must be ABSOLUTE from the TB top (needs at "
                    "least one '.'), got %r" % (where, path))
        return
    for c in comps:
        if any(ch.isspace() for ch in c):
            errs.append("%s: sim path component %r contains whitespace: %r"
                        % (where, c, path))
            return


def _validate_iice(errs, doc):
    node = doc.get("iice")
    if not isinstance(node, dict):
        errs.append("top level: missing or malformed `iice:` mapping")
        return None

    name = _req_str(errs, "iice", node, "name")
    if name is not None and not IICE_NAME_RE.match(name):
        errs.append("iice.name: must match %s (it becomes the SystemVerilog "
                    "module name `iice_shadow_<name>` and the .idc filename), "
                    "got %r" % (IICE_NAME_RE.pattern, name))
        name = None

    depth = _req_int(errs, "iice", node, "depth", minimum=DEPTH_MIN)
    ttime = _req_enum(errs, "iice", node, "trigger_time", TRIGGER_TIMES)
    ctrl = _req_enum(errs, "iice", node, "controller", CONTROLLERS)
    tconds = _req_int(errs, "iice", node, "trigger_conditions", minimum=1)
    tstates = _req_int(errs, "iice", node, "trigger_states",
                       minimum=TRIGGER_STATES_MIN, maximum=TRIGGER_STATES_MAX,
                       reason=TRIGGER_STATES_REASON)

    clk_node = node.get("clock")
    clock = None
    if not isinstance(clk_node, dict):
        errs.append("iice: missing or malformed `clock:` mapping")
    else:
        chw = _req_str(errs, "iice.clock", clk_node, "hw")
        csim = _req_str(errs, "iice.clock", clk_node, "sim")
        cedge = _req_enum(errs, "iice.clock", clk_node, "edge", EDGES)
        if chw is not None:
            _check_hw_path(errs, "iice.clock.hw", chw)
        if csim is not None:
            _check_sim_path(errs, "iice.clock.sim", csim)
        if None not in (chw, csim, cedge):
            clock = Clock(chw, csim, cedge)

    # Optional sampler options, default False so every existing manifest is
    # unchanged. See IiceCfg's docstring: build-time only.
    qual = _opt_bool(errs, "iice", node, "qualified_sampling", False)
    comp = _opt_bool(errs, "iice", node, "data_compression", False)
    armed = _opt_bool(errs, "iice", node, "always_armed", False)

    # Qualified sampling stores a sample only while the TRIGGER condition holds,
    # so it is worthless without a trigger -- and `controller: none` means the
    # condition comes from per-signal watch values set at DEBUG time. Requiring
    # at least one `trigger: true` signal is the one check that can be made here;
    # the rest is a runtime concern the operator has to get right.
    raw_signals = doc.get("signals") if isinstance(doc, dict) else None
    any_trigger = any(
        isinstance(s, dict) and s.get("trigger") is True
        for s in (raw_signals or [])
    )
    if qual and not any_trigger:
        errs.append(
            "iice: qualified_sampling is on but no signal has `trigger: true`. "
            "Qualified sampling gates the sample buffer on the TRIGGER "
            "condition, so with nothing instrumented for trigger it would "
            "either store everything or store nothing -- neither is what you "
            "asked for.")

    for key in node:
        if key not in ("name", "depth", "trigger_time", "controller",
                       "trigger_conditions", "trigger_states", "clock",
                       "qualified_sampling", "data_compression",
                       "always_armed"):
            errs.append("iice: unknown key %r (the schema is frozen — see "
                        "INTERFACES.md §2)" % key)

    if None in (name, depth, ttime, ctrl, tconds, tstates) or clock is None:
        return None
    return IiceCfg(name, depth, ttime, ctrl, tconds, tstates, clock,
                   qualified_sampling=bool(qual), data_compression=bool(comp),
                   always_armed=bool(armed))


def _validate_signals(errs, doc):
    node = doc.get("signals")
    if not isinstance(node, list) or not node:
        errs.append("top level: `signals:` must be a non-empty list")
        return []

    out = []
    seen = {}
    for i, ent in enumerate(node):
        where = "signals[%d]" % i
        if not isinstance(ent, dict):
            errs.append("%s: must be a mapping, got %r" % (where, ent))
            continue

        name = _req_str(errs, where, ent, "name")
        if name is not None:
            where = "signals[%d] (%s)" % (i, name)
            if name in RESERVED_NAMES:
                errs.append("%s: %r is a RESERVED name (INTERFACES.md §1: the "
                            "harness synthesises %s) — rename the signal"
                            % (where, name, ", ".join(RESERVED_NAMES)))
                name = None
            elif not SIGNAL_NAME_RE.match(name):
                errs.append("%s: signal name must match %s (it is the FSDB leaf "
                            "name on both sides)" % (where, SIGNAL_NAME_RE.pattern))
                name = None
            elif name in seen:
                errs.append("%s: duplicate signal name %r (first seen at "
                            "signals[%d])" % (where, name, seen[name]))
                name = None
            else:
                seen[name] = i

        width = _req_int(errs, where, ent, "width", minimum=1)
        radix = _req_enum(errs, where, ent, "radix", RADICES)
        hw = _req_str(errs, where, ent, "hw")
        sim = _req_str(errs, where, ent, "sim")
        if hw is not None:
            _check_hw_path(errs, where + ".hw", hw)
        if sim is not None:
            _check_sim_path(errs, where + ".sim", sim)

        # Defaults: a probe you bothered to list is in the sample buffer;
        # trigger participation is opt-in because each trigger condition costs
        # trigger RAM (INTERFACES.md §2).
        sample = _opt_bool(errs, where, ent, "sample", True)
        trigger = _opt_bool(errs, where, ent, "trigger", False)
        if sample is False and trigger is False:
            # Identify semantics trap: `signals add` with NEITHER -sample nor
            # -trigger means BOTH (identify_debug_env_reference.pdf p.68). A
            # manifest entry that is neither is therefore not "probe nothing",
            # it is ambiguous — reject it rather than emit a bare `signals add`.
            errs.append("%s: `sample: false` with `trigger: false` is not a "
                        "valid probe. In Identify, `signals add` with neither "
                        "-sample nor -trigger means BOTH, so this cannot be "
                        "expressed — set at least one to true." % where)

        for key in ent:
            if key not in ("name", "width", "hw", "sim", "sample", "trigger", "radix"):
                errs.append("%s: unknown key %r (the schema is frozen — see "
                            "INTERFACES.md §2)" % (where, key))

        if None in (name, width, radix, hw, sim):
            continue
        out.append(Signal(name, width, hw, sim, sample, trigger, radix))
    return out


def _validate_trigger(errs, doc, signals):
    node = doc.get("trigger")
    if not isinstance(node, dict):
        errs.append("top level: missing or malformed `trigger:` mapping")
        return None
    for key in node:
        if key != "expr":
            errs.append("trigger: unknown key %r (the schema is frozen — see "
                        "INTERFACES.md §2)" % key)
    expr = _req_str(errs, "trigger", node, "expr")
    if expr is None:
        return None

    names = set(s.name for s in signals)
    idents = trigger_identifiers(expr)

    # Rule 1 (INTERFACES.md §2): every identifier in trigger.expr must be a
    # manifest `name`.
    for tok in idents:
        if tok not in names:
            errs.append("trigger.expr: identifier %r is not a manifest signal "
                        "name (known: %s). trigger.expr is an expression over "
                        "manifest names ONLY — never raw paths, never literals "
                        "spelled as words."
                        % (tok, ", ".join(sorted(names)) or "<none>"))

    # Rule 2 (INTERFACES.md §2): every signal with trigger: true must appear in
    # trigger.expr. Otherwise the .idc wires it into the IICE trigger logic
    # while the sim's trigger_marker ignores it — the two sides trigger on
    # different conditions and the windows silently misalign.
    used = set(idents)
    for s in signals:
        if s.trigger and s.name not in used:
            errs.append("signals (%s): has `trigger: true` but does not appear "
                        "in trigger.expr %r — the hardware IICE would trigger "
                        "on it while the sim shadow would not" % (s.name, expr))

    return expr


def _validate(source_name, doc):
    errs = []
    if not isinstance(doc, dict):
        raise ManifestError(source_name, ["manifest must be a YAML mapping at "
                                          "the top level, got %s" % type(doc).__name__])
    for key in doc:
        if key not in ("iice", "signals", "trigger"):
            errs.append("top level: unknown key %r (the schema is frozen — see "
                        "INTERFACES.md §2)" % key)

    iice = _validate_iice(errs, doc)
    signals = _validate_signals(errs, doc)
    expr = _validate_trigger(errs, doc, signals)

    if errs:
        raise ManifestError(source_name, errs)
    return iice, signals, expr


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #
def loads(text, source_name="<string>", path=None):
    """Parse + validate a manifest from YAML text. Raises `ManifestError`."""
    try:
        doc = parse_yaml(text)
    except ManifestError:
        raise
    except Exception as exc:  # PyYAML errors, or fallback-parser blowups
        raise ManifestError(source_name, ["YAML parse error: %s" % exc])
    iice, signals, expr = _validate(source_name, doc)
    return Manifest(path, source_name, iice, signals, expr, doc)


def load(path):
    """Read, parse and validate the manifest at `path`. Raises `ManifestError`."""
    path = os.path.abspath(path)
    try:
        with open(path, "r") as fh:
            text = fh.read()
    except IOError as exc:
        raise ManifestError(path, ["cannot read manifest: %s" % exc])
    return loads(text, source_name=os.path.basename(path), path=path)


def main(argv=None):
    """`python3 manifest.py <manifest.yaml> [...]` — validate and summarise."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.stderr.write("usage: manifest.py <signals.yaml> [...]\n")
        return 2
    rc = 0
    for p in argv:
        try:
            m = load(p)
        except ManifestError as exc:
            sys.stderr.write("%s\n" % exc)
            rc = 1
            continue
        print("%s: OK — iice %s, depth %d, controller %s, %d signal(s), "
              "trigger over {%s}" % (
                  m.source_name, m.iice.name, m.iice.depth, m.iice.controller,
                  len(m.signals), ", ".join(trigger_identifiers(m.trigger_expr))))
    return rc


if __name__ == "__main__":
    sys.exit(main())
