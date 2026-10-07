#!/usr/bin/env python3
"""gen_idc.py — manifest -> `<out>/<iice_name>.idc` for Synplify/Identify.

Emits the Identify instrumentation-command file described by INTERFACES.md §3.1
(FROZEN format). Run by `make -C tests/identify_iice gen` for BOTH manifests.

    python3 gen_idc.py --manifest signals_selftest.yaml --out build

=============================================================================
Command semantics — verified against
/eda/synopsys/2022-23/RHELx86/SFPGA_2022.09-SP2/identify/doc/
identify_debug_env_reference.pdf, and against the one working reference .idc in
reach (HAPS-work/HAPS-SX/examples/hello_ident/scripts/debug.idc).
=============================================================================

`signals add` — THE TRAP (doc p.68, `signals` command, -trigger option):

    "Note: The -sample and -trigger options can be combined or both options
     can be omitted to specify a signal for both sampling and triggering."

So an OMITTED flag pair does NOT mean "neither" — it means BOTH. A generator
that emitted a bare `signals add -iice {X} {path}` for a sample-only probe would
silently wire that signal into the trigger comparator too, doubling trigger RAM
and (worse) making the hardware trigger on a condition the sim shadow never
evaluates. This generator therefore ALWAYS emits at least one flag explicitly,
and manifest.py rejects `sample: false` + `trigger: false` outright because that
state is inexpressible rather than merely unusual.

`device` lines — all four are mandatory (INTERFACES.md §3.1, and
IDENTIFY_IICE_DFX_PLAN.md §2: the RP pblock has zero BUFG sites):
  jtagport soft                 doc p.34 — IICE talks through a TAP the
                                instrumentor inserts itself, costing 4 user
                                pins, instead of the device's built-in BSCAN.
                                Required: a partial-reconfiguration RM may not
                                instantiate BSCANE2.
  xilinxinsertbufg 0            doc p.36 — do NOT let the instrumentor add a
                                BUFG to the JTAG clock (default is 1). No BUFG
                                sites in the pblock.
  skewfree 1                    doc p.34 — master-slave FFs on the JTAG chain so
                                the JTAG clock does not need global clock
                                resources. Overrides insertbufg anyway; both are
                                stated so the intent survives a tool default
                                change.
  stop_on_signal_not_found 1    doc p.34 — default is 0, which merely WARNS when
                                an instrumented signal is absent from the
                                compiled design. That default is how you get a
                                silently smaller probe set after the generated
                                nanosoc.sv is regenerated (plan risk V4). 1
                                makes it an error.

NOT emitted, on purpose:
  `iice sampler -triggertime`   doc p.52 lists -triggertime under "Debugger iice
                                sampler Options", not the instrumentor's. The
                                manifest's `trigger_time` is therefore consumed
                                by the offline cropper (Stream C) and the
                                debugger script (Stream D), never by the .idc.
  `-compression`, `-qualified_sampling`, `-always_armed`, `-counterwidth`
                                plan §6 V3: keep the first IICE simple. None are
                                in the frozen schema, so none are emitted.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import manifest as mf  # noqa: E402


def render(m):
    """Return the full .idc text for manifest `m`.

    Line order and spacing follow INTERFACES.md §3.1 verbatim (the two extra
    spaces after `iice clock` column-align `-iice` with the `iice sampler`
    line).
    """
    name = m.iice.name
    lines = []

    # --- device settings (all four mandatory; see module docstring) ---------
    lines.append("device jtagport soft")
    lines.append("device xilinxinsertbufg 0")
    lines.append("device skewfree 1")
    lines.append("device stop_on_signal_not_found 1")

    # --- the IICE itself ----------------------------------------------------
    lines.append("iice new {%s} -type regular" % name)
    lines.append("iice clock   -iice {%s} -edge %s {%s}"
                 % (name, m.iice.clock.edge, m.iice.clock.hw))
    # `iice sampler` options are INSTRUMENTOR-side. Measured 2026-07-30: the
    # debugger shell rejects -depth and -buffertype outright ("Invalid argument
    # on command line: -depth"), and reports the built values read-only as
    #   {iice=IICE_CPU buffertype=behavioral} {sampledepth=1024 alwaysarmed=0
    #    qualifiedsampling=0 samplemode=normal triggertime=middle
    #    datacompression=0}
    # so anything below is frozen into the bitstream: changing it means
    # re-instrument -> Synplify -> place/route -> new DFX partial.
    sampler = ["iice sampler -iice {%s} -depth %d" % (name, m.iice.depth)]
    # QUALIFIED SAMPLING is the big lever for a bus probe set: the buffer only
    # advances while the trigger condition holds, so `depth` samples become
    # `depth` *transactions* instead of `depth` mostly-idle cycles. Free in
    # memory -- it buys effective depth rather than spending BRAM.
    if m.iice.qualified_sampling:
        sampler.append("-qualified_sampling 1")
    if m.iice.data_compression:
        sampler.append("-compression 1")
    if m.iice.always_armed:
        sampler.append("-always_armed 1")
    lines.append(" ".join(sampler))

    # `iice controller [options] [none|counter|statemachine]` (doc p.49).
    lines.append("iice controller -iice {%s} %s" % (name, m.iice.controller))
    if m.iice.controller == "statemachine":
        # -triggerconditions / -triggerstates are state-machine-only
        # instrumentor options (doc p.50/p.51). Emitting them for `none` or
        # `counter` would be meaningless at best.
        lines.append("iice controller -iice {%s} -triggerconditions %d -triggerstates %d"
                     % (name, m.iice.trigger_conditions, m.iice.trigger_states))

    # --- the probe set ------------------------------------------------------
    # The sample clock is deliberately NOT added as a signal: "this signal
    # cannot be sampled itself while used as the sample clock" (doc p.47). The
    # harness synthesises iice/sample_clk on both sides instead
    # (INTERFACES.md §1/§3.3).
    for s in m.signals:
        flags = []
        if s.sample:
            flags.append("-sample")
        if s.trigger:
            flags.append("-trigger")
        # manifest.py guarantees at least one flag, so a bare `signals add`
        # (== BOTH, per the docstring) can never be emitted.
        assert flags, "manifest.py should have rejected a no-flag signal"
        lines.append("signals add -iice {%s} %s {%s}" % (name, " ".join(flags), s.hw))

    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--manifest", required=True, help="path to signals*.yaml")
    ap.add_argument("--out", required=True,
                    help="output DIRECTORY; the file is <out>/<iice_name>.idc")
    args = ap.parse_args(argv)

    try:
        m = mf.load(args.manifest)
    except mf.ManifestError as exc:
        sys.stderr.write("gen_idc.py: %s\n" % exc)
        return 2

    os.makedirs(args.out, exist_ok=True)
    out_path = os.path.join(args.out, "%s.idc" % m.iice.name)
    with open(out_path, "w") as fh:
        fh.write(render(m))
    print("gen_idc.py: wrote %s (%d signals)" % (out_path, len(m.signals)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
