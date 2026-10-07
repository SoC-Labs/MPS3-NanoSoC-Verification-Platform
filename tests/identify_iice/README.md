# `tests/identify_iice` — sim-vs-hardware trace comparison

Capture the same signal set from a VCS simulation and from a Synopsys Identify
IICE on silicon, and diff them, so a hardware-only failure can be localised to a
signal and a cycle.

- **Contract (frozen, read first if you are changing anything):** `INTERFACES.md`
- **Design rationale:** `../../docs/planning/IICE_SIM_VS_HW_TRACE_PLAN.md`
- **Why the IICE can live in the DFX partition at all:** `../../docs/planning/IDENTIFY_IICE_DFX_PLAN.md`

## Quick start

```bash
cd $(git rev-parse --show-toplevel)
make -C tests/identify_iice help
make -C tests/identify_iice check     # the full gate: needs VCS + Verdi
make -C tests/identify_iice identity  # provenance: needs NEITHER, read-only
python3 -m pytest tests/identify_iice/tests -q   # needs NEITHER
```

**On board day, run `make identity` first.** It needs no tools, no lease and no
board, and it answers the question that cost a board window on 2026-07-30 — *does
this manifest describe what is actually in the bitstream?* — in one second:

```bash
make -C tests/identify_iice identity \
     MANIFEST=$PWD/tests/identify_iice/signals_nanosoc_asbuilt.yaml
```

## What is actually proven, and what is not

This distinction matters more than usual here, because the end goal needs a board,
a bitstream and a licence seat that has never been exercised in this lab.

**Proven by execution, no board required:**
- The manifest → `.idc` / shadow-`.sv` / `signal_map.tsv` generators, with a
  golden-file anti-drift gate that has been demonstrated to *fail* on a perturbation.
- VCS + FSDB dumping end-to-end: a real FSDB written and read back, 9 signals.
- The crop → rename → compare pipeline, on real FSDBs.
- **The negative control**: a one-bit perturbation is detected; a missing signal
  and a corrupt FSDB are reported as harness errors, not as matches.

- **A real Identify-authored FSDB has been captured and read** — via
  `com cabletype demo`, no board. `identdebugger_prdp` checks out and works, and
  Verdi X-2025.06 reads an Identify T-2022.09 FSDB with only a benign
  "previous version" warning. The hardware trace layout is therefore measured, not
  guessed: see `INTERFACES.md` §7.

**Written but NOT proven:**
- `make hw-fsdb` against **real silicon** — never executed. (The demo-cable path
  above proves the debugger, the FSDB writer and the reader; it does not prove a
  board.)
- The `hw:` paths in `signals_nanosoc.yaml`: the path *convention* is settled and
  cited, but these *specific* paths have never been resolved by Identify against
  the real DUT, because the RM cannot elaborate today — `nanosoc.sv` has dropped
  the `cpu_0_swd*` ports the wrapper still connects (`IDENTIFY_IICE_DFX_PLAN.md`
  §1b). This also breaks the pre-existing **Vivado** baseline, identically.
- `make synth` on the real DUT, the FDC, and both `vsrc_override/` files.

## The one design idea worth understanding

A hardware IICE stores a fixed-depth, trigger-relative window because it has no
choice. A simulation has no such constraint, so the sim side does **not** model a
ring buffer. Instead it mirrors the probe set into **registered** copies clocked on
the same edge as the real sample clock — which is what makes the trace faithful,
since the IICE only ever sees values at that edge, never combinational glitches
between them — dumps those continuously, and the depth-limited window is cropped
**offline in Python**.

That removes a hardware-shaped component that would otherwise have to be written,
verified, and kept in sync with the tool's semantics, and it reproduces
`-triggertime middle` exactly.

The second idea: **one manifest is the only place a signal name is written.** It
generates the Identify `.idc`, the sim shadow module, and the rename table. The
failure mode of hand-keeping two signal lists is not a crash — it is a clean-looking
report that compared the wrong signal against the wrong signal.

The third idea, added 2026-07-31: **being generated from one manifest is not the
same as being paired with it later.** `build/signal_map.tsv` is written by
whichever manifest last ran `make gen`; a capture on disk came off whichever
bitstream was loaded that day; and `signals_nanosoc.yaml` can be retargeted while
the built bitstream carries the old probe set. Each of those pairings failed on
2026-07-30 and each produced a *plausible report*, not an error. So every artifact
now carries `<artifact>.prov.json` naming the manifest identity that produced it,
and `compare` **refuses** (exit 2) on a conflict. Absence of a stamp is *not* a
conflict — older captures must stay comparable — but the report then says the
trace is unverified rather than leaving the reader to assume it was checked.
`provenance.py`, `make identity`, negctl NC6–NC9, `INTERFACES.md` §4 (2026-07-31).

## Reading a compare report

`build/compare_report.txt` states total samples compared, mismatch count,
per-signal counts, the **index of the first diverging sample**, and the count of
X-masked samples. Check that last number: sim drives X where hardware cannot, so
sim-X is treated as don't-care **one way only**. A run that is mostly X-masked is
worthless even though it will report zero mismatches — that is why the count is
printed rather than hidden.

Exit codes are frozen: `0` match, `1` mismatch, **`2` harness error**. A harness
error is never reported as a match.

The report's `-- provenance --` block says what bound these artifacts together:
the manifest identity (probe-set digest), whether `signal_map.tsv` really was
generated from it, whether the *built* instrumentation agrees, and each FSDB's
stamp. Read it before the numbers. `VERIFIED` means checked; `(none)` means the
pairing is asserted by whoever ran the command, not proven — there is no third
state, because a silently-omitted provenance block reads as "checked".

## Gotchas that will cost you an afternoon

All established the hard way; the full list is in `INTERFACES.md` §6.

- **`-P novas.tab pli.a` is deprecated from VERDI2024.09** and fails at *run* time
  after a clean compile — exit 0, `VERDICT=PASS`, and no FSDB. Use `-debug_access`
  with `VERDI_HOME` set at compile *and* run time, and `NOVAS_HOME` **unset**.
- The login shell's `NOVAS_HOME` points at a 2017 Verdi and silently breaks dumping.
- VCS and Verdi vintages must be paired (`libsscore_vcs<YYYYMM>.so` sets are disjoint).
- **`nCompare` always exits 0.** Read the verdict from the `.nce` `500` record.
- **`fsdbqry` cannot list signals on this install**; use `fsdb2vcd -et 0`.
- **`fsdbmangle` is an obfuscator, not a rename tool.**
- Never `realpath()` a Verdi utility — they are symlinks to one wrapper that
  dispatches on `argv[0]`.
- Verdi tools drop `<tool>Log/` into the CWD. All gitignored; pass `-logdir`.
