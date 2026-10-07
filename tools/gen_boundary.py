#!/usr/bin/env python3
"""gen_boundary.py — write every view of the RP ⇄ static-shell partition boundary.

ONE declaration, ``fpga/shell/boundary.yaml``; four generated views:

  ``fpga/shell/rp_dut_stub.sv``                  port list + inert tie-offs
  ``fpga/shell/generated/rp_wrapper_skeleton.sv``   starting point for a NEW RM
  ``fpga/dfx/pin_check.py``                      the table the DFX gate diffs
  ``docs/contracts/partition-pins.md``           the per-group signal tables

A fifth view, the Linux fork's ``expected_rp_pins`` in
``src/linux_harness/impl/build_transplant_phaseB.tcl``, was generated here until
the 2026-10 ILA widening. The fork is a separate static (``0x2B082E1B``) with no
XVC and no debug bridge; following the widening would force a fork re-mint nobody
has planned, so it STOPPED TRACKING this boundary
(docs/planning/ILA_MINT_PLAN_2026-09-23.md decision 5) and pins its own fielded
35-port list, checked by ``tests/linux_fork_boundary``. This generator no longer
touches that file.

...and one check that owns no file: :func:`check_rp_dut_copies` refuses to
generate while any tracked ``module rp_dut`` in the tree disagrees with the
boundary.

WHY
---
The partition boundary (35 ports / 136 bits at mint 0x3F1A560F; 47 / 148 from
the 2026-10 ILA mint) was asserted independently in ~13 places. Only
the RM-wrapper side was gated: ``pin_check.py`` scraped the MARKDOWN with a
regex and diffed each wrapper against it. Nothing at all gated the SHELL side.
``src/linux_harness/impl/rp_dut_stub.sv`` proved the failure mode is real — a
14th copy, still carrying the pre-re-mint SWD group (``swd_clk``/``swd_dio_o``/
``swd_dio_oe``/``swd_dio_i``) that the JTAG re-mint replaced, read by one Tcl
line and looked at by nothing. It was DELETED on 2026-09-11 rather than
generated: two identical files with a gate between them is still two files, and
the build that read it can read ``fpga/shell/rp_dut_stub.sv`` directly. The
15th copy — that build's own ``expected_rp_pins`` conformance list, pre-cutover
too — was a generated view here until the fork stopped tracking (above). :func:`check_rp_dut_copies` is what makes a
16th impossible: every tracked ``module rp_dut`` is parsed and compared, so a new
copy must AGREE, and one that drifts fails this generator (and therefore
``check_generated_fresh.py``, in both ``make check`` and ``make check-ci``).

This boundary is minted: the 2026-10 ILA mint, 0x72BB0A36 (fielded 2026-09-24),
is routed against it, ``dbgbscan`` group included; the previous shell 0x3F1A560F
had the 35-port boundary without it. One changed direction, width or
name re-keys ``static_id`` and invalidates every overlay in ``fpga/dfx/prod/``.
So the generator is deliberately strict: the derived ``totals:`` in the YAML are
RECOMPUTED here and a mismatch is a hard :class:`genlib.GenError`, never a
warning and never a silent widening.

WHAT IS DERIVED, AND WHAT IS NOT
--------------------------------
Derived: port lists, port directions and widths, the stub's tie-off values (they
ARE the decoupler safe-idle clamps), the pin_check lookup table, the markdown
signal tables. Hand-written and merely SPLICED AROUND: every word of reasoning
in ``partition-pins.md`` (the CDC rule, the QSPI non-CDC exception, the 2026-07-16
CORRECTION, the IOB packing note, the deferred-validation TODO) and the prose
headers of the two SystemVerilog files. A generator that owned the prose would
be a generator nobody dares re-run.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import genlib  # noqa: E402

GENERATOR = "gen_boundary.py"
BOUNDARY_YAML = "fpga/shell/boundary.yaml"

STUB = "fpga/shell/rp_dut_stub.sv"
SKELETON = "fpga/shell/generated/rp_wrapper_skeleton.sv"
PIN_CHECK = "fpga/dfx/pin_check.py"
CONTRACT_MD = "docs/contracts/partition-pins.md"

#: Every tracked SystemVerilog file that declares ``module rp_dut`` must carry
#: THIS boundary, port for port. :func:`check_rp_dut_copies` enforces it; these
#: are the two the design legitimately has, and both are checked, not excused:
#:
#:   fpga/shell/rp_dut_stub.sv   the generated stub (this generator writes it)
#:   fpga/dfx/proof/rp_dut.sv    the port-only black box the DFX flow links RM
#:                               checkpoints into. Not generated -- build_dfx.tcl
#:                               owns it -- but it must not drift, so it is
#:                               compared rather than exempted.
#:
#: A THIRD one is how this started: src/linux_harness/impl/rp_dut_stub.sv was a
#: 14th hand copy still carrying the retired swd_* group, and nothing looked at
#: it. It is gone (deleted 2026-09-11; build_transplant_phaseB.tcl reads the
#: generated stub instead), and a new one cannot appear unless it agrees.

#: `assign <name>` is padded to at least this column in rp_dut_stub.sv, so a
#: block of short names still lines up with its neighbours.
_TIEOFF_MIN_PAD = 14
#: column at which a trailing `// ...` comment starts in the tie-off block
_TIEOFF_COMMENT_COL = 40


# ── load + validate ──────────────────────────────────────────────────────────
def load(repo: Path) -> dict:
    """Parse and validate ``boundary.yaml``. Every failure is a GenError."""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - PyYAML is present here
        raise genlib.GenError(
            f"{BOUNDARY_YAML}: PyYAML is required to read the boundary "
            f"declaration ({exc})") from None

    text = genlib.read(repo, BOUNDARY_YAML)
    try:
        bnd = yaml.safe_load(text)
    except Exception as exc:
        raise genlib.GenError(f"{BOUNDARY_YAML}: not parseable: {exc}") from None
    if not isinstance(bnd, dict):
        raise genlib.GenError(f"{BOUNDARY_YAML}: top level is not a mapping")

    for key in ("version", "ngpio", "stub_rm_id_ascii", "totals",
                "rm_direction_map", "groups"):
        if key not in bnd:
            raise genlib.GenError(f"{BOUNDARY_YAML}: missing top-level `{key}`")

    if bnd["rm_direction_map"] != {"O": "input", "I": "output"}:
        raise genlib.GenError(
            f"{BOUNDARY_YAML}: rm_direction_map must invert the shell view "
            f"(O->input, I->output); got {bnd['rm_direction_map']}")

    seen = set()
    for g in bnd["groups"]:
        if "id" not in g or "signals" not in g:
            raise genlib.GenError(f"{BOUNDARY_YAML}: group without id/signals")
        for s in g["signals"]:
            for key in ("name", "dir", "width", "clamp", "note"):
                if key not in s:
                    raise genlib.GenError(
                        f"{BOUNDARY_YAML}: {g['id']}/{s.get('name', '?')}: "
                        f"missing `{key}`")
            if s["dir"] not in ("O", "I"):
                raise genlib.GenError(
                    f"{BOUNDARY_YAML}: {s['name']}: dir must be O or I")
            if s["name"] in seen:
                raise genlib.GenError(
                    f"{BOUNDARY_YAML}: duplicate signal `{s['name']}`")
            seen.add(s["name"])
            # a shell-driven leg cannot have a decoupler clamp: only RP-drive
            # legs are decoupler members (rp_resetn is the input-side isolation)
            if s["dir"] == "O" and s["clamp"] != "none":
                raise genlib.GenError(
                    f"{BOUNDARY_YAML}: {s['name']} is shell-driven (O) but "
                    f"declares clamp {s['clamp']!r}; shell->RP legs route "
                    f"straight through the decoupler")
            if s["dir"] == "I" and s["clamp"] == "none":
                raise genlib.GenError(
                    f"{BOUNDARY_YAML}: {s['name']} is RP-driven (I) and must "
                    f"declare a decoupler safe-idle clamp")
            if g.get("domain_column") and "domain" not in s:
                raise genlib.GenError(
                    f"{BOUNDARY_YAML}: {s['name']}: group {g['id']} has a "
                    f"Domain column but this signal has no `domain`")

    validate_totals(bnd)
    return bnd


def signals(bnd: dict):
    """Every signal, in contract order."""
    for g in bnd["groups"]:
        for s in g["signals"]:
            yield s


def bits_of(sig: dict, ngpio: int) -> int:
    w = sig["width"]
    if isinstance(w, int):
        return w
    if w == "NGPIO":
        return ngpio
    raise genlib.GenError(f"{sig['name']}: unknown width {w!r}")


def validate_totals(bnd: dict) -> None:
    """Recompute ports/bits and refuse to generate on a mismatch.

    This is the derived assertion the YAML asks for: a signal added, removed or
    resized without updating ``totals:`` fails here rather than quietly re-keying
    ``static_id`` and bricking every fielded overlay.
    """
    sigs = list(signals(bnd))
    ports, bits = len(sigs), sum(bits_of(s, bnd["ngpio"]) for s in sigs)
    want_p, want_b = bnd["totals"]["ports"], bnd["totals"]["bits"]
    if (ports, bits) != (want_p, want_b):
        raise genlib.GenError(
            f"{BOUNDARY_YAML}: totals mismatch — declared {want_p} ports / "
            f"{want_b} bits, computed {ports} ports / {bits} bits. This "
            f"boundary is minted (the 2026-10 ILA mint, 0x72BB0A36, is routed "
            f"against it): if the change is intended it re-keys static_id and "
            f"needs a full re-mint.")


# ── shared rendering helpers ────────────────────────────────────────────────
def _range(sig: dict, ngpio_literal: bool, ngpio: int) -> str:
    """The `[msb:0]` field of a port declaration ('' for a scalar)."""
    w = sig["width"]
    if w == "NGPIO":
        return f"[{ngpio - 1}:0]" if ngpio_literal else "[NGPIO-1:0]"
    return "" if w == 1 else f"[{w - 1}:0]"


def _port_line(sig: dict, direction: str, ngpio_literal: bool, ngpio: int) -> str:
    """`  input  logic [1:0]  name` — the range occupies a fixed 7-column field,
    widening (with at least one space) for a symbolic range like [NGPIO-1:0]."""
    rng = _range(sig, ngpio_literal, ngpio)
    return (f"  {direction.ljust(6)} logic "
            f"{rng}{' ' * max(1, 7 - len(rng))}{sig['name']}")


def _comment_block(raw: str) -> list[str]:
    return [f"  // {ln}" for ln in raw.rstrip("\n").split("\n")]


def _literal(sig: dict, value, ngpio: int, ascii_tag: str | None) -> str:
    """A sized SystemVerilog literal for a tie-off value."""
    w = bits_of(sig, ngpio)
    if ascii_tag is not None:
        if len(ascii_tag) * 8 != w:
            raise genlib.GenError(
                f"{sig['name']}: stub ascii {ascii_tag!r} is "
                f"{len(ascii_tag) * 8} bits, port is {w}")
        return f"{w}'h" + "_".join(f"{ord(c):02x}" for c in ascii_tag)
    if not isinstance(value, int):
        raise genlib.GenError(f"{sig['name']}: clamp {value!r} is not an int")
    if value >= (1 << w):
        raise genlib.GenError(
            f"{sig['name']}: clamp {value} does not fit in {w} bits")
    if w <= 4:                      # binary reads better for a lane mask
        return f"{w}'b{value:0{w}b}"
    return f"{w}'h{value:0{(w + 3) // 4}x}"


# ── view 1: fpga/shell/rp_dut_stub.sv ───────────────────────────────────────
def _stub_ports(bnd: dict) -> str:
    """The stub's port list. NGPIO is substituted literally: the stub is a
    concrete v0 instance, not a parameterised wrapper."""
    blocks = []
    for g in bnd["groups"]:
        lines = []
        if g.get("stub_port_comment"):
            lines += _comment_block(g["stub_port_comment"])
        for s in g["signals"]:
            d = "input" if s["dir"] == "O" else "output"
            lines.append(_port_line(s, d, True, bnd["ngpio"]) + ",")
        blocks.append("\n".join(lines))
    body = "\n\n".join(blocks)
    return body[:-1] + "\n"        # drop the trailing comma of the last port


def _stub_tieoffs(bnd: dict) -> str:
    """Every stub OUTPUT driven to its DFX decoupler safe-idle value.

    The stub and the decoupler agree BY CONSTRUCTION: both read `clamp` from
    boundary.yaml. `rm_id` is the one deliberate divergence — the decoupler
    holds it at 0 during a swap (so dfx_ctl's rm_id_valid is a genuine
    post-swap confirmation) while the stub drives an ASCII tag so the host can
    see the harness is carrying the inert RP.
    """
    out = []
    header = ["  // --- inert tie-offs (all outputs driven to safe defaults) "
              "------------------"]
    for g in bnd["groups"]:
        driven = [s for s in g["signals"] if s["dir"] == "I"]
        if not driven:
            continue
        # one alignment column per block, so a block of short names still lines
        # up with its longest neighbour
        pad = max(_TIEOFF_MIN_PAD, max(len(s["name"]) for s in driven) + 1)
        lines = list(header)
        header = []
        if g.get("stub_comment"):
            lines += _comment_block(g["stub_comment"])
        for s in driven:
            # `stub_ascii_from` names a TOP-LEVEL key whose ASCII value the stub
            # drives instead of the clamp — the indirection keeps the tag itself
            # declared exactly once (rm_id is the only user).
            key = s.get("stub_ascii_from")
            if key is not None and key not in bnd:
                raise genlib.GenError(
                    f"{BOUNDARY_YAML}: {s['name']}: stub_ascii_from names "
                    f"`{key}`, which is not a top-level key")
            ascii_tag = bnd[key] if key is not None else None
            lit = _literal(s, s["clamp"], bnd["ngpio"], ascii_tag)
            ln = f"  assign {s['name'].ljust(pad)}= {lit};"
            if s.get("stub_note"):
                ln = ln.ljust(_TIEOFF_COMMENT_COL) + f"// {s['stub_note']}"
            lines.append(ln)
        out.append("\n".join(lines))
    return "\n\n".join(out) + "\n"


def _stub(repo: Path, bnd: dict) -> str:
    text = genlib.read(repo, STUB)
    text = genlib.splice(text, "boundary-ports", "sv", GENERATOR,
                         _stub_ports(bnd))
    return genlib.splice(text, "boundary-tieoffs", "sv", GENERATOR,
                         _stub_tieoffs(bnd))


# ── view 2: fpga/shell/generated/rp_wrapper_skeleton.sv ─────────────────────
_SKELETON_HEAD = """\
// -----------------------------------------------------------------------------
// rp_wrapper_skeleton.sv — GENERATED by tools/{gen} from {yml}.
//                          DO NOT EDIT. Copy it, then edit the copy.
//
// The starting point for a NEW reconfigurable module. This is the RP side of
// the {ports}-port / {bits}-bit partition boundary, with every direction inverted
// from the contract (which states directions from the SHELL's view):
//
//     contract  O  (shell drives)    ->  RM port `input`
//     contract  I  (shell receives)  ->  RM port `output`
//
// TO USE IT
//   1. Copy this file to fpga/rp/<your_rm>/<your_rm>_wrapper.sv and rename the
//      module. Do NOT add, remove, rename, re-direct or resize a single port:
//      this boundary is minted (the 2026-10 ILA mint) and every overlay in
//      fpga/dfx/prod/ is placed and routed against it. A drifted wrapper is a
//      hardware hazard, not a slow build.
//   2. Fill in the body: instantiate your design, drive every output, and give
//      `rm_id` a unique 32-bit tag (the host reads it back to verify which RM
//      is loaded — see docs/contracts/overlay-manifest.md).
//   3. Register the RM in fpga/dfx/rm_list.tcl (RM_ORDER + RM_LIB entries).
//      `make -C fpga/dfx pin-check` then checks it automatically.
//   4. Tie the SWJ straps (`swj_enable`, `ntrst`, `npotrst`) INSIDE the wrapper;
//      they do not cross the boundary.
//
// RULES THAT BITE
//   * No clock generation inside an RM. Every DUT clock is made in the static
//     shell (DRP MMCM) and arrives as a partition pin.
//   * Every crossing is CDC'd on the STATIC side; the RM sees safe signals.
//     The one exception is the qspi group, a matched NON-CDC source-synchronous
//     passthrough — do not add register stages to it asymmetrically.
//   * Pin-facing output registers that want IOB packing must live in the SHELL,
//     not here: OLOGIC pad sites are static-only in a DFX design.
//   * Every output must be driven. An undriven output floats the shell's input.
//
// See docs/contracts/partition-pins.md for the full contract.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module rm_skeleton #(
  // v0 freezes NGPIO at {ngpio}; pin_check.py rejects any other value.
  parameter int NGPIO = {ngpio}
) (
"""

_SKELETON_TAIL = """\
);

  // ---------------------------------------------------------------------------
  // WHAT YOU FILL IN
  // ---------------------------------------------------------------------------
  // 1. rm_id — a unique 32-bit identity, read back by the host after a swap to
  //    prove WHICH RM landed. Convention in this tree is a 4-char ASCII tag,
  //    e.g. the inert shell stub drives 32'h53_54_55_42 ("STUB").
  //
  //      assign rm_id = 32'h??_??_??_??;
  //
  // 2. Your design. Reset with `dut_resetn` (system) and hold anything that
  //    must survive a swap off `rp_resetn`; `dbg_resetn` is the debug/SRST leg
  //    from the OpenOCD path.
  //
  // 3. Every remaining output. An RM that leaves one undriven will synthesise
  //    and then behave unpredictably on silicon. If your RM does not use a
  //    group, tie it to the SAME safe-idle value the DFX decoupler clamps it to
  //    (see the `clamp` column in fpga/shell/boundary.yaml) — notably
  //    qspi_csn = 1'b1 (flash DESELECTED), everything else 0:
  //
  //      assign jtag_tdo       = 1'b0;
  //      assign phy_rmii_txd   = 2'b00;
  //      assign phy_rmii_tx_en = 1'b0;
  //      assign mdc            = 1'b0;
  //      assign mdio_o         = 1'b0;
  //      assign mdio_oe        = 1'b0;   // MDIO tri-stated
  //      assign uart_tx_tdata  = 8'h00;
  //      assign uart_tx_tvalid = 1'b0;
  //      assign uart_rx_tready = 1'b0;
  //      assign swo            = 1'b0;
  //      assign dut_lockup     = 1'b0;
  //      assign irq_out        = 1'b0;
  //      assign dut_gpio_o     = {NGPIO{1'b0}};
  //      assign dut_gpio_oe    = {NGPIO{1'b0}};   // board pads high-Z
  //      assign qspi_sclk      = 1'b0;
  //      assign qspi_csn       = 1'b1;   // flash DESELECTED — not 0
  //      assign qspi_io_o      = 4'b0000;
  //      assign qspi_io_oe     = 4'b0000;
//      assign dbg_bscan_tdo  = 1'b0;   // no RM debug hub
//
// 4. The dbgbscan group. An RM with no ILAs ignores the 11 dbg_bscan_* inputs
//    and ties dbg_bscan_tdo to 0. An RM WITH ILAs instantiates a debug_bridge in
//    mode 1 (C_DEBUG_MODE=1, "From BSCAN to Debug Hub"), clocks its hub from
//    phy_rmii_ref_clk (the only always-on shell clock; never dut_clk), wires
//    the 12 legs to its S_BSCAN_* pins, and drives dbg_bscan_tdo from it. An ILA
//    WITHOUT that bridge is illegal in an RP (Vivado looks for a BSCANE2).

endmodule
"""


def _skeleton(bnd: dict) -> str:
    blocks = []
    for g in bnd["groups"]:
        lines = []
        for s in g["signals"]:
            d = bnd["rm_direction_map"][s["dir"]]
            lines.append(_port_line(s, d, False, bnd["ngpio"]) + ",")
        blocks.append("\n".join(lines))
    body = "\n\n".join(blocks)
    body = body[:-1] + "\n"        # drop the trailing comma of the last port

    sigs = list(signals(bnd))
    head = _SKELETON_HEAD.format(
        gen=GENERATOR, yml=BOUNDARY_YAML, ngpio=bnd["ngpio"],
        ports=len(sigs), bits=sum(bits_of(s, bnd["ngpio"]) for s in sigs))
    return head + body + _SKELETON_TAIL


# ── view 3: fpga/dfx/pin_check.py ───────────────────────────────────────────
def _pin_check(repo: Path, bnd: dict) -> str:
    sigs = list(signals(bnd))
    keyw = max(len(s["name"]) for s in sigs) + 3      # `"name":`
    rows = [f'    {chr(34) + s["name"] + chr(34) + ":":<{keyw}} '
            f'("{s["dir"]}", "{s["width"]}"),' for s in sigs]

    body = "\n".join([
        f"#: the RP <-> shell partition boundary, from {BOUNDARY_YAML}.",
        f"#: {len(sigs)} ports / {sum(bits_of(s, bnd['ngpio']) for s in sigs)}"
        f" bits with NGPIO={bnd['ngpio']} — the minted boundary (the",
        "#: 2026-10 ILA mint). pin_check carries the table rather than scraping the",
        "#: contract markdown, so this gate has no runtime dependency on a",
        "#: document's formatting and none on a YAML library.",
        f'BOUNDARY_SOURCE = "{BOUNDARY_YAML}"',
        "",
        "#: v0 build parameter for the GPIO passthrough width.",
        f"NGPIO = {bnd['ngpio']}",
        "",
        "#: contract direction (shell's view) -> required RM port direction.",
        "RM_DIR = {"
        + ", ".join(f'"{k}": "{v}"' for k, v in bnd["rm_direction_map"].items())
        + "}",
        "",
        "#: signal -> (shell-view direction, width), in contract order.",
        "CONTRACT = {",
        *rows,
        "}",
    ])
    return genlib.splice(genlib.read(repo, PIN_CHECK), "boundary", "py",
                         GENERATOR, body)


# ── view 4: docs/contracts/partition-pins.md ────────────────────────────────
def _md_table(group: dict) -> str:
    dom = bool(group.get("domain_column"))
    cols = ["Signal", "Dir", "Width"] + (["Domain"] if dom else []) + ["Notes"]

    def row(cells):
        return "".join(f"| {c} " if c else "| " for c in cells) + "|"

    lines = [row(cols), "|" + "---|" * len(cols)]
    for s in group["signals"]:
        cells = [f"`{s['name']}`", s["dir"], str(s["width"])]
        if dom:
            cells.append(s["domain"])
        cells.append(s["note"])
        lines.append(row(cells))
    return "\n".join(lines) + "\n"


def _contract_md(repo: Path, bnd: dict) -> str:
    text = genlib.read(repo, CONTRACT_MD)
    for g in bnd["groups"]:
        text = genlib.splice(text, f"boundary-{g['id']}", "md", GENERATOR,
                             _md_table(g))
    return text


# ── the check that owns no file ─────────────────────────────────────────────
_MODULE_RE = re.compile(r"\bmodule\s+rp_dut\s*(?:#\s*\(.*?\))?\s*\(", re.S)
_PORT_RE = re.compile(
    r"^\s*(input|output)\s+logic\s*(\[[^\]]*\])?\s*(\w+)\s*,?\s*(?://.*)?$",
    re.M)


def _ports_of_rp_dut(text: str) -> list[tuple[str, str, str]] | None:
    """[(name, direction, range)] for a file's ``module rp_dut`` port list."""
    m = _MODULE_RE.search(text)
    if not m:
        return None
    depth, i = 1, m.end()
    while i < len(text) and depth:
        depth += (text[i] == "(") - (text[i] == ")")
        i += 1
    return [(p[2], p[0], (p[1] or "").replace(" ", ""))
            for p in _PORT_RE.findall(text[m.end():i - 1])]


def check_rp_dut_copies(repo: Path, bnd: dict) -> None:
    """Every tracked ``module rp_dut`` in the tree carries THIS boundary.

    Not "there is only one copy": the DFX flow legitimately needs a second, the
    port-only black box in ``fpga/dfx/proof/rp_dut.sv`` that RM checkpoints link
    into. What must be impossible is a copy that DISAGREES — which is what the
    deleted ``src/linux_harness/impl/rp_dut_stub.sv`` was for a whole cutover.
    So each is parsed and diffed, and the failure names the file and the signal.
    """
    want = [(s["name"], "input" if s["dir"] == "O" else "output",
             _range(s, True, bnd["ngpio"])) for s in signals(bnd)]
    found = 0
    for path in sorted(repo.rglob("*.sv")):
        rel = path.relative_to(repo).as_posix()
        if rel.startswith(".git/") or "/build" in f"/{rel}" or rel.startswith("build"):
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        got = _ports_of_rp_dut(text)
        if got is None:
            continue
        found += 1
        # The generated stub is THIS generator's own output: it is rewritten
        # from the YAML in the same run, and --check / check_generated_fresh.py
        # catch it stale. Comparing it here, BEFORE regeneration, made the
        # boundary impossible to widen at all -- the generator refused to write
        # the very file it was about to fix (found at the 2026-10 dbgbscan
        # widening).
        if rel == STUB:
            continue
        if got != want:
            wn = [p[0] for p in want]
            gn = [p[0] for p in got]
            extra, missing = [n for n in gn if n not in wn], [n for n in wn if n not in gn]
            detail = (f"extra={extra} missing={missing}" if (extra or missing)
                      else "same names, different direction/width: "
                           + str([(a, b) for a, b in zip(want, got) if a != b]))
            raise genlib.GenError(
                f"{rel}: its `module rp_dut` port list is NOT the boundary in "
                f"{BOUNDARY_YAML} ({detail}). This boundary is minted (the "
                f"2026-10 ILA mint): a second, disagreeing copy is how "
                f"src/linux_harness/impl/rp_dut_stub.sv sat on the retired swd_* "
                f"group across a whole cutover with nothing to notice. Fix the "
                f"copy, or delete it and read {STUB}.")
    if found < 2:
        raise genlib.GenError(
            f"check_rp_dut_copies found only {found} `module rp_dut` "
            f"declaration(s); expected at least the generated {STUB} and the "
            f"DFX black box fpga/dfx/proof/rp_dut.sv. A check that finds "
            f"nothing passes vacuously.")


# ── the generator ───────────────────────────────────────────────────────────
def build_outputs(repo: Path, bnd: dict) -> dict[str, str]:
    """Render every view from an ALREADY-LOADED boundary.

    Split out from :func:`build` so a test can mutate the parsed boundary in
    memory and prove the outputs actually move — a round-trip test alone would
    still pass if this generator ignored the YAML.
    """
    return {
        STUB: _stub(repo, bnd),
        SKELETON: _skeleton(bnd),
        PIN_CHECK: _pin_check(repo, bnd),
        CONTRACT_MD: _contract_md(repo, bnd),
        # NOT the Linux fork's expected_rp_pins: the fork stopped tracking this
        # boundary at the 2026-10 widening (module docstring, decision 5).
    }


def build(repo: Path) -> dict[str, str]:
    bnd = load(repo)
    # Deliberately here and NOT in build_outputs(): build_outputs is the pure
    # renderer a mutation test drives with a DOCTORED boundary, and the copies in
    # the tree are written against the real one. Checking there would make every
    # mutation control fail for the wrong reason.
    check_rp_dut_copies(repo, bnd)
    return build_outputs(repo, bnd)


if __name__ == "__main__":
    sys.exit(genlib.run(build, GENERATOR))
