#!/usr/bin/env python3
"""Rebuild the PRE-FIX dfx_ctl, so the watchdog un-clamp tests can be SEEN to fail.

A green test against the fixed RTL proves nothing on its own: it has to be shown
that the same test goes red against the RTL as it was. This produces that RTL
mechanically rather than by keeping a hand-copied "old version" around to rot.

WHAT IS REVERTED (and only this -- the PORTS stay, so the bench elaborates
unchanged and the only variable is the reset behaviour under test):

  1. decouple_en_q goes back on s_axi_aresetn instead of the POR-only reset.
     This is the hazard itself: s_axi_aresetn is peripheral_aresetn, which is
     what the watchdog's aux_reset_in pulses, so a watchdog fire cleared the
     clamp (docs/planning/SERVICES_PARTITION.md §5.4).
  2. The set-dominant wdt_reset_i branch is disabled.
  3. rp_gate_q's reset value goes back to a bare 1'b1.

Usage:  python3 mutate_pre_wdt_fix.py <src dfx_ctl.sv> <dst dfx_ctl.sv>

Exits NON-ZERO if any substitution fails to apply -- a mutation that silently
did nothing would produce a green "control" run, which is worse than no control.
"""
import sys

SUBS = [
    # (what the FIXED RTL says, what the PRE-FIX RTL said)
    ("if (!por_n_sync_q[1]) begin\n      decouple_en_q <= 1'b0;",
     "if (!s_axi_aresetn) begin\n      decouple_en_q <= 1'b0;"),
    ("end else if (wdt_reset_i) begin\n      decouple_en_q <= 1'b1;",
     "end else if (wdt_reset_i && 1'b0) begin\n      decouple_en_q <= 1'b1;"),
    ("rp_gate_q <= ~decouple_en_q;",
     "rp_gate_q <= 1'b1;"),
]


def main(argv):
    if len(argv) != 3:
        print(__doc__)
        return 2
    text = open(argv[1]).read()
    for old, new in SUBS:
        n = text.count(old)
        if n != 1:
            print(f"MUTATION DID NOT APPLY: found {n} occurrences of\n{old!r}\n"
                  "The RTL was reworded. Fix this script, or the control proves "
                  "nothing.", file=sys.stderr)
            return 1
        text = text.replace(old, new)
    open(argv[2], "w").write(text)
    print(f"pre-fix dfx_ctl written to {argv[2]} ({len(SUBS)} reversions applied)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
