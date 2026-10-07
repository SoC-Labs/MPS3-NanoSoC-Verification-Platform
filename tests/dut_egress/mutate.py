#!/usr/bin/env python3
"""Build a MUTANT copy of the DUTEGR RTL, so the inject side's controls can be
SEEN to fail.

A green bench proves nothing until the same bench is shown going red against RTL
with the property removed. This writes that RTL mechanically, from the shipped
file, instead of keeping a hand-copied "broken version" around to rot (the
tests/dfx_ctl/mutate_pre_wdt_fix.py idiom). The shipped RTL is never written.

MUTATIONS (the lines are tagged ``// MUTATION-POINT(<name>)`` in dut_egress.sv):

  tornframe   THE READ SIDE STARTS BEFORE COMMIT. The inject data FIFO publishes
              every byte the moment it is staged (a plain async FIFO, no commit
              pointer), and the framer starts on the first visible byte instead
              of on a committed descriptor whose bytes are all visible. Staged
              bytes then leave on inj_m_* before COMMIT — and an ABORTed frame's
              bytes leave too. Handover §6 control (a).

  norollback  THE ROLLBACK IS REMOVED. ABORT and a rejected COMMIT no longer
              return the speculative write pointer to the commit point, so a
              discarded frame's bytes stay in the FIFO and are published with
              the NEXT frame's commit — which then goes out torn. Handover §6
              control (b).

  noflushed   TX_FLUSHED IS REMOVED: offset 0x38 reads 0, as if the register
              did not exist (amendment A2). Flushed frames then vanish from
              the accounting and the §3 invariant cannot balance after a FLUSH.

  flushasreject  THE PRE-A2 ACCOUNTING: flushed frames are counted in
              TX_REJECT again, and 0x38 reads 0. The FLUSH test's check that
              TX_REJECT does not move on a FLUSH, and the invariant, catch it.

Usage:  python3 mutate.py <tornframe|norollback|noflushed|flushasreject> <src_dir> <dst_dir>

Copies dut_egress.sv + dutegr_cfifo.sv from src_dir to dst_dir, applying the
named mutation. Exits NON-ZERO if any substitution does not apply exactly once —
a mutation that silently did nothing would give a green "control", which is
worse than no control at all.
"""
import os
import shutil
import sys

FILES = ("dut_egress.sv", "dutegr_cfifo.sv")

MUTATIONS = {
    "tornframe": [
        ("assign txd_publish  = tx_commit_ok;                  // MUTATION-POINT(tornframe)",
         "assign txd_publish  = 1'b1;                          // MUTATED(tornframe): publish on push"),
        ("wire txr_go       = txr_idle && txr_whole && !txr_flush_req && inj_m_tready;  // MUTATION-POINT(tornframe)",
         "wire txr_go       = txr_idle && txd_avail && !txr_flush_req && inj_m_tready;  // MUTATED(tornframe): start on any byte"),
    ],
    "norollback": [
        ("assign txd_rollback = tx_commit_rej || tx_abort;     // MUTATION-POINT(norollback)",
         "assign txd_rollback = 1'b0;                          // MUTATED(norollback)"),
    ],
    "noflushed": [
        ("IDX_TX_FLUSHED: axi_rdata_q <= tx_flushed_q;              // MUTATION-POINT(noflushed)",
         "IDX_TX_FLUSHED: axi_rdata_q <= 32'd0;                     // MUTATED(noflushed)"),
    ],
    "flushasreject": [
        ("IDX_TX_FLUSHED: axi_rdata_q <= tx_flushed_q;              // MUTATION-POINT(noflushed)",
         "IDX_TX_FLUSHED: axi_rdata_q <= 32'd0;                     // MUTATED(flushasreject)"),
        ("                            + {31'd0, tx_commit_rej};                  // MUTATION-POINT(flushasreject)",
         "                            + {31'd0, tx_commit_rej} + {31'd0, tx_dropd_pulse};  // MUTATED(flushasreject)"),
    ],
}


def main(argv):
    if len(argv) != 4 or argv[1] not in MUTATIONS:
        print(__doc__)
        return 2
    name, src, dst = argv[1], argv[2], argv[3]
    os.makedirs(dst, exist_ok=True)
    for f in FILES:
        shutil.copyfile(os.path.join(src, f), os.path.join(dst, f))
    path = os.path.join(dst, "dut_egress.sv")
    text = open(path).read()
    for old, new in MUTATIONS[name]:
        n = text.count(old)
        if n != 1:
            print(f"MUTATION '{name}' DID NOT APPLY: found {n} occurrences of\n  {old!r}\n"
                  "dut_egress.sv was reworded. Fix this script, or the control "
                  "proves nothing.", file=sys.stderr)
            return 1
        text = text.replace(old, new)
    open(path, "w").write(text)
    print(f"mutant '{name}' written to {dst} ({len(MUTATIONS[name])} substitution(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
