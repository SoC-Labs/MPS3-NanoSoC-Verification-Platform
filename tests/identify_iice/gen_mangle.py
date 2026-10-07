#!/usr/bin/env python3
"""gen_mangle.py — manifest -> `<out>/signal_map.tsv`, the neutral rename table.

INTERFACES.md §3.3 (FROZEN format). This file is the interface between the
generators and the compare pipeline:

    name<TAB>width<TAB>radix<TAB>hw_path<TAB>sim_path

    python3 gen_mangle.py --manifest signals_selftest.yaml --out build

Why a neutral TSV and not an `fsdbmangle` command file: `fsdbmangle`'s own
rename syntax is not pinned down yet (IICE_SIM_VS_HW_TRACE_PLAN.md T3/V1), and
guessing tool syntax here would bake an unverifiable assumption into the one
artifact everything else keys off. So the generators emit facts; Stream C
translates them into whatever the tool actually wants.

Rows, in order:
  * one per manifest signal, in manifest order;
  * `trigger_marker` — `__DERIVED__` on BOTH sides: it exists in neither design.
    The sim synthesises it in the shadow module (INTERFACES.md §3.2); the
    hardware side recovers it from the IICE's recorded trigger position.
  * `sample_clk` — the manifest's `iice.clock` paths, NOT `__DERIVED__`: unlike
    trigger_marker this is a real net in both designs. Note it is nonetheless
    absent from the .idc probe list, because Identify cannot sample the signal it
    is using as the sample clock (identify_debug_env_reference.pdf p.47), so the
    hardware side must reconstruct the waveform from the sample index. The paths
    are recorded anyway so the compare pipeline can name the source of truth.

`sample_index` is reserved as a NAME (INTERFACES.md §1) but gets no row: it is
the row index itself, not a traced signal.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import manifest as mf  # noqa: E402

HEADER = ("name", "width", "radix", "hw_path", "sim_path")
DERIVED = "__DERIVED__"


def rows(m):
    """Return the TSV rows (header first) as a list of string tuples."""
    out = [HEADER]
    for s in m.signals:
        out.append((s.name, str(s.width), s.radix, s.hw, s.sim))
    # The two reserved signals, in the order INTERFACES.md §3.3 shows them.
    out.append((mf.TRIGGER_MARKER, "1", "bin", DERIVED, DERIVED))
    out.append((mf.SAMPLE_CLK, "1", "bin", m.iice.clock.hw, m.iice.clock.sim))
    return out


def render(m):
    return "".join("\t".join(r) + "\n" for r in rows(m))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--manifest", required=True, help="path to signals*.yaml")
    ap.add_argument("--out", required=True,
                    help="output DIRECTORY; the file is <out>/signal_map.tsv")
    args = ap.parse_args(argv)

    try:
        m = mf.load(args.manifest)
    except mf.ManifestError as exc:
        sys.stderr.write("gen_mangle.py: %s\n" % exc)
        return 2

    os.makedirs(args.out, exist_ok=True)
    out_path = os.path.join(args.out, "signal_map.tsv")
    with open(out_path, "w") as fh:
        fh.write(render(m))
    print("gen_mangle.py: wrote %s (%d signal rows + header)"
          % (out_path, len(m.signals) + 2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
