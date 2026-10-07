#!/usr/bin/env python3
"""pin_check.py — mechanically diff every RM wrapper's port list against the
frozen RP<->shell boundary declared in fpga/shell/boundary.yaml.

The boundary table below is GENERATED from that YAML by tools/gen_boundary.py,
which writes the same truth into rp_dut_stub.sv, the RM wrapper skeleton and
docs/contracts/partition-pins.md — the prose contract. This gate used to scrape
that document with a regex at RUNTIME; carrying a generated table instead means
a reformatted table cannot quietly empty the signal set, and the gate needs no
YAML library on the CI image.

Closes the TODO(A2) at fpga/dfx/rm_list.tcl (~line 146):

    "add a `pin_check` recipe here that diffs each wrapper's port list against
     docs/contracts/partition-pins.md mechanically, so a drifted RM wrapper
     fails fast in CI instead of surfacing as a pr_verify failure late in
     build_dfx.tcl."

Why it matters: a drifted wrapper today only surfaces at `pr_verify`, after a
full OOC synth + link + place + route. Now that partials are pushed over the
wire onto real silicon, a wrapper whose boundary disagrees with the locked
static is a hardware hazard, not just a slow build.

DIRECTION CONVENTION — partition-pins.md states directions from the SHELL's
view. An RM sits on the other side, so every direction inverts:

    contract  O  (shell drives)   ->  RM port must be `input`
    contract  I  (shell receives) ->  RM port must be `output`

Usage:
    python3 fpga/dfx/pin_check.py            # check every known RM wrapper
    python3 fpga/dfx/pin_check.py <file.sv>  # check one wrapper
Exit 0 = every wrapper conforms.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RM_LIST = Path(__file__).resolve().parent / "rm_list.tcl"

# BEGIN GENERATED[boundary] — gen_boundary.py — DO NOT EDIT BY HAND
#: the RP <-> shell partition boundary, from fpga/shell/boundary.yaml.
#: 47 ports / 148 bits with NGPIO=16 — the minted boundary (the
#: 2026-10 ILA mint). pin_check carries the table rather than scraping the
#: contract markdown, so this gate has no runtime dependency on a
#: document's formatting and none on a YAML library.
BOUNDARY_SOURCE = "fpga/shell/boundary.yaml"

#: v0 build parameter for the GPIO passthrough width.
NGPIO = 16

#: contract direction (shell's view) -> required RM port direction.
RM_DIR = {"O": "input", "I": "output"}

#: signal -> (shell-view direction, width), in contract order.
CONTRACT = {
    "dut_clk":              ("O", "1"),
    "dut_resetn":           ("O", "1"),
    "rp_resetn":            ("O", "1"),
    "dbg_resetn":           ("O", "1"),
    "jtag_tck":             ("O", "1"),
    "jtag_tms":             ("O", "1"),
    "jtag_tdi":             ("O", "1"),
    "jtag_tdo":             ("I", "1"),
    "dbg_bscan_bscanid_en": ("O", "1"),
    "dbg_bscan_capture":    ("O", "1"),
    "dbg_bscan_drck":       ("O", "1"),
    "dbg_bscan_reset":      ("O", "1"),
    "dbg_bscan_runtest":    ("O", "1"),
    "dbg_bscan_sel":        ("O", "1"),
    "dbg_bscan_shift":      ("O", "1"),
    "dbg_bscan_tck":        ("O", "1"),
    "dbg_bscan_tdi":        ("O", "1"),
    "dbg_bscan_tms":        ("O", "1"),
    "dbg_bscan_update":     ("O", "1"),
    "dbg_bscan_tdo":        ("I", "1"),
    "phy_rmii_ref_clk":     ("O", "1"),
    "phy_rmii_crs_dv":      ("O", "1"),
    "phy_rmii_rxd":         ("O", "2"),
    "phy_rmii_txd":         ("I", "2"),
    "phy_rmii_tx_en":       ("I", "1"),
    "mdc":                  ("I", "1"),
    "mdio_o":               ("I", "1"),
    "mdio_oe":              ("I", "1"),
    "mdio_i":               ("O", "1"),
    "uart_tx_tdata":        ("I", "8"),
    "uart_tx_tvalid":       ("I", "1"),
    "uart_tx_tready":       ("O", "1"),
    "uart_rx_tdata":        ("O", "8"),
    "uart_rx_tvalid":       ("O", "1"),
    "uart_rx_tready":       ("I", "1"),
    "swo":                  ("I", "1"),
    "rm_id":                ("I", "32"),
    "dut_lockup":           ("I", "1"),
    "irq_out":              ("I", "1"),
    "dut_gpio_o":           ("I", "NGPIO"),
    "dut_gpio_oe":          ("I", "NGPIO"),
    "dut_gpio_i":           ("O", "NGPIO"),
    "qspi_sclk":            ("I", "1"),
    "qspi_csn":             ("I", "1"),
    "qspi_io_o":            ("I", "4"),
    "qspi_io_oe":           ("I", "4"),
    "qspi_io_i":            ("O", "4"),
}
# END GENERATED[boundary]


def discover_wrappers():
    """Derive the RM wrapper set from fpga/dfx/rm_list.tcl — the same registry
    build_dfx.tcl uses. Deliberately NOT a hardcoded list: a new RM added to
    RM_ORDER is then checked automatically, and cannot slip past this gate.

    Reads RM_ORDER for the set, then RM_LIB(<rm>,wrapper_dir) + RM_LIB(<rm>,top)
    for each wrapper's path (`<wrapper_dir>/<top>.sv`).
    """
    tcl = RM_LIST.read_text()

    m = re.search(r"set\s+RM_ORDER\s+\[list\s+([^\]]+)\]", tcl)
    if not m:
        raise ValueError(f"{RM_LIST}: no RM_ORDER")
    # Tcl line-continues with a trailing backslash, and the RM registration
    # snippets shipped with the RMs (e.g. fpga/rp/clcd_demo/rm_list_snippet.tcl)
    # show RM_ORDER wrapped that way. A naive split() then yields a bogus RM
    # named "\", and this gate dies with a confusing "RM '\' lacks wrapper_dir"
    # instead of checking anything. Drop continuations rather than demand the
    # registry stay on one physical line.
    order = [w for w in m.group(1).split() if w != "\\"]

    def field(rm, f):
        g = re.search(rf'set\s+RM_LIB\({re.escape(rm)},{f}\)\s+"([^"]+)"', tcl)
        return g.group(1) if g else None

    out = []
    for rm in order:
        wd, top = field(rm, "wrapper_dir"), field(rm, "top")
        if not wd or not top:
            raise ValueError(f"{RM_LIST}: RM '{rm}' in RM_ORDER lacks "
                             f"wrapper_dir/top")
        out.append((rm, ROOT / wd / f"{top}.sv"))
    return out


# ── SystemVerilog port list ──────────────────────────────────────────────────
def strip_comments(src):
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    return re.sub(r"//[^\n]*", " ", src)


def _match_paren(s, i):
    """s[i] == '('  ->  index of the matching ')'"""
    depth = 0
    while i < len(s):
        if s[i] == "(":
            depth += 1
        elif s[i] == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("unbalanced parentheses")


def parse_ports(path):
    """-> (module_name, {port: (dir, width)}, ngpio_default)"""
    src = strip_comments(path.read_text())

    m = re.search(r"\bmodule\s+(\w+)", src)
    if not m:
        raise ValueError(f"{path}: no module declaration")
    name, i = m.group(1), m.end()

    ngpio = None
    g = re.search(r"parameter\s+int\s+NGPIO\s*=\s*(\d+)", src)
    if g:
        ngpio = int(g.group(1))

    # optional #( ... ) parameter block, then the port list ( ... )
    while i < len(src) and src[i] not in "(#":
        i += 1
    if i < len(src) and src[i] == "#":
        i = src.index("(", i)
        i = _match_paren(src, i) + 1
        while i < len(src) and src[i] != "(":
            i += 1
    close = _match_paren(src, i)
    body = src[i + 1:close]

    ports = {}
    for decl in body.split(","):
        d = " ".join(decl.split())
        pm = re.match(
            r"^(input|output|inout)\s+(?:wire|logic|reg)?\s*"
            r"(?:\[\s*([^\]]+?)\s*\])?\s*(\w+)$", d)
        if pm:
            ports[pm.group(3)] = (pm.group(1), width_of(pm.group(2)))
    return name, ports, ngpio


def width_of(rng):
    if rng is None:
        return "1"
    rng = rng.replace(" ", "")
    m = re.fullmatch(r"(\d+):(\d+)", rng)
    if m:
        return str(int(m.group(1)) - int(m.group(2)) + 1)
    if re.fullmatch(r"NGPIO-1:0", rng):
        return "NGPIO"
    return rng  # unrecognised — surfaces as a width mismatch


# ── the check ────────────────────────────────────────────────────────────────
def check(path, contract):
    name, ports, ngpio = parse_ports(path)
    errs = []

    for sig, (sdir, width) in contract.items():
        want_dir = RM_DIR[sdir]
        if sig not in ports:
            errs.append(f"MISSING port `{sig}` ({want_dir} [{width}])")
            continue
        got_dir, got_w = ports[sig]
        if got_dir != want_dir:
            errs.append(f"`{sig}` direction is {got_dir}, contract requires "
                        f"{want_dir} (shell-view {sdir})")
        if got_w != width:
            errs.append(f"`{sig}` width is {got_w}, contract requires {width}")

    for p in ports:
        if p not in contract:
            errs.append(f"EXTRA port `{p}` not in partition-pins.md")

    if ngpio is None:
        errs.append("no `parameter int NGPIO` declared")
    elif ngpio != NGPIO:
        errs.append(f"NGPIO = {ngpio}, contract v0 requires {NGPIO}")

    return name, len(ports), errs


def main():
    contract = CONTRACT

    if len(sys.argv) > 1:                       # explicit files (used by tests)
        targets = [(p.stem, Path(p)) for p in map(Path, sys.argv[1:])]
        src = "explicit"
    else:                                       # discovered from the registry
        targets = discover_wrappers()
        src = f"{RM_LIST.relative_to(ROOT)} RM_ORDER"

    print(f"\npin_check — RM wrappers vs {BOUNDARY_SOURCE} "
          f"({len(contract)} signals, NGPIO={NGPIO})")
    print(f"  RM set from: {src}\n")
    print(f"  {'rm'.ljust(14)} {'module'.ljust(22)} {'ports'.rjust(5)}  result")
    print(f"  {'-'*14} {'-'*22} {'-'*5}  ------")

    failed = []
    for rm, t in targets:
        if not t.exists():
            print(f"  {rm.ljust(14)} {'-'.ljust(22)} {'-'.rjust(5)}  "
                  f"MISSING {t.relative_to(ROOT) if ROOT in t.parents else t}")
            failed.append(rm)
            continue
        name, n, errs = check(t, contract)
        print(f"  {rm.ljust(14)} {name.ljust(22)} {str(n).rjust(5)}  "
              f"{'PASS' if not errs else 'FAIL'}")
        for e in errs:
            print(f"      - {e}")
        if errs:
            failed.append(rm)

    ok = not failed
    print(f"\n  {'ALL WRAPPERS CONFORM' if ok else 'DRIFTED: ' + ', '.join(failed)}"
          f" — {len(targets) - len(failed)}/{len(targets)} pass\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
