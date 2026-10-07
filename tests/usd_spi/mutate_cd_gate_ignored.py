#!/usr/bin/env python3
"""Build the CONTROL mutant of usd_spi: a pad gate that ignores card detect.

A green "pads stay high-Z with no card" test proves nothing until the same test
is seen to go RED against RTL that gets the gate wrong. This produces that RTL
mechanically (no hand-kept "bad copy" to rot).

WHAT IS MUTATED (one line; the ports and everything else stay identical, so the
only variable is the gate under test):

    pads_en = EN && (CD_PRESENT || CD_IGNORE)      the contract
    pads_en = EN                                    the mutant

That is the plausible bug: a gate that trusts firmware's EN alone and drives an
empty socket. tests/usd_spi's test_pads_hiz_without_card must go red on it (and
so must the mid-transfer removal ABORT test, which relies on the same gate).

Usage:  python3 mutate_cd_gate_ignored.py <src usd_spi.sv> <dst usd_spi.sv>

Exits NON-ZERO if the substitution does not apply exactly once -- a mutation
that silently did nothing would produce a green "control", which is worse than
no control.
"""
import sys

SUBS = [
    # (the correct RTL, the mutant)
    ("wire pads_en_d = ctrl_en_q & (cd_present_q | ctrl_cd_ignore_q);",
     "wire pads_en_d = ctrl_en_q;  // CONTROL MUTANT: card detect ignored"),
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
    with open(argv[2], "w") as fh:
        fh.write(text)
    print(f"control mutant written to {argv[2]} ({len(SUBS)} substitution applied)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
