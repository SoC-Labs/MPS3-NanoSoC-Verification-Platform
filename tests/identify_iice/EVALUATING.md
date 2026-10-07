# EVALUATING.md — is this tooling actually useful?

`make check` answers *"is the harness correct?"* and hands you an exit code.
This file is about the other question:

> **If I had a real hardware bug, would this tooling find it, and would the
> output tell me where to look?**

```bash
source ./set_env.sh
module load verdi/T-2022.06-SP2      # or X-2025.06-SP2; must match the VCS you load
unset NOVAS_HOME                     # the login value is a 2017 Verdi and breaks FSDB

make -C tests/identify_iice evaluate MODE=clean            # positive control
make -C tests/identify_iice evaluate MODE=stuck            # LAN8720/RMII class
make -C tests/identify_iice evaluate MODE=skew             # CDC / sampling-phase class
make -C tests/identify_iice evaluate MODE=late-divergence  # boots-then-wedges class
make -C tests/identify_iice evaluate-all                   # all four + freeze, control first
```

> These targets live in `Makefile.evaluate`, which `tests/identify_iice/Makefile`
> (integrator-owned) `-include`s by name as of `e35febd` — so the plain
> `make -C tests/identify_iice evaluate` forms above work. The
> `make -C tests/identify_iice -f Makefile.evaluate evaluate MODE=stuck` form
> also still works, and is the one to use if you have this fragment in a tree
> whose top-level Makefile predates that commit.

Each run injects a divergence shaped like a bug **this repo has actually had**,
puts it through the unmodified compare pipeline, and leaves three things in
`build/evaluate/<tag>/`.  `<tag>` is the mode, plus the stall shape when it is
not the default — `EVAL_STALL_SHAPE=freeze` writes to
`build/evaluate/late-divergence-freeze/`, so the two shapes never overwrite each
other's artifacts and `evaluate-all` (which runs both) leaves both.

Exit code: `0` the tooling did its job · `1` it did **not** (missed the injected
divergence, or the report did not describe it faithfully) · `2` it could not run.
That is deliberately *not* compare.sh's meaning, where `1` = "sim and hardware
disagree" — which for every injected mode here is the desired outcome.

---

## The three artifacts, and what each one is for

| artifact | answers | does **not** answer |
|---|---|---|
| `compare_report.txt` | Did anything diverge; **which signal first**, at **which sample**, how far from the trigger, per-signal counts, how much was X-masked. Machine-checkable: frozen exit codes 0/1/2. | Why. Which side is wrong. Anything about a cycle outside the window or a signal outside the manifest. |
| `iice_<tag>.jf` (nWave) | What the divergence *looks like* — sim above hardware on one axis, plus a `diff/*_neq` bit per signal so you can jump to it. This is the artifact that answers "is this useful", because it is a human seeing two waveforms part company. | Nothing machine-checkable. `fsdb2vcd` refuses a joined file; every automated check reads the real member FSDBs instead. |
| `interpretation.txt` | What that *pattern* is consistent with (stuck net / sampling-phase error / stall / spin loop / no clean signature), and **what it cannot distinguish**. | A cause. It is a shape classifier, not a diagnosis. |

Open the waveforms with the command `evaluate` prints, e.g.

```bash
nWave -ssf tests/identify_iice/build/evaluate/stuck/iice_stuck.jf &
# or the three real FSDBs, same data:
nWave -ssf .../sim_window.fsdb .../hw_window.fsdb .../mismatch.fsdb &
```

Add **`diff/any_neq` first** and find its first rising edge: that is the
report's `FIRST DIVERGING SAMPLE`. Then stack `sim/<signal>` directly above
`hw/<signal>`. `sim/trigger_marker` shows where the trigger sat.
**The time stamp is the sample index on both sides** — not ns. That is the only
way two traces with unrelated time bases can be laid over each other.

---

## Worked reading of a real report (`MODE=stuck`, the selftest manifest)

```
  window start (absolute sim sample)     117
  trigger at window index                32
  MISMATCH COUNT (signal-samples)        19
  X-masked samples (sim X vs hw 0/1)     2   (0.45% of samples compared)
  mismatches caused by hardware X/Z      0
  FIRST DIVERGING SAMPLE                 33
    diverging signal(s) there            haddr
    relative to trigger                  +1 samples

  haddr                        32      hex       19        0
  hrdata                       32      hex        0        2
        33 haddr   'h00000044   'h00000040   2
        35 haddr   'h0000004C   'h00000048   2
```

Read it in this order:

1. **`X-masked` first, always.** 0.45% here, so the comparison is real. At 50%+
   the verdict is close to worthless whichever way it went — most samples were
   not compared at all.
2. **`hardware X/Z` = 0.** Non-zero means suspect the *capture or the loader*
   before the DUT; silicon cannot produce X.
3. **`FIRST DIVERGING SAMPLE 33`, `+1 samples` from the trigger.** That is a
   cycle count relative to an event *you chose* in `trigger.expr` — the only
   anchor that exists. Absolute time is meaningless in an IICE trace.
4. **One signal has all 19.** A bus-wide event usually lights up several signals
   at once; a single-signal count points at that net, not at the core.
5. **The mismatch table: only bit 2 is ever bad, and hw is always the lower
   value.** `'h44` vs `'h40`, `'h4C` vs `'h48`. Same bit, one direction, from
   sample 33 to the end → a net stuck **low**, not a data corruption.
6. **Look here next:** `haddr[2]` on the path in `signals_*.yaml`, and everything
   between that flop and the probe. Then decide whether the DUT, the pad, the
   level shifter or the IICE probe route is the suspect — the report cannot tell
   those apart (see below).

---

## The honest limitations

**Depth is the hard wall.** `signals_nanosoc.yaml` uses `depth: 1024` on a 50 MHz
`dut_clk` = **20.48 µs of history**. A MicroPython boot is minutes of sim time. If
the trigger is not near the failure you will capture a perfectly clean window and
learn nothing. The lever is a narrower probe set at greater depth, not a longer
run.

**Hardware has only the manifest's signals.** Simulation has everything; the IICE
has exactly what `signals add` put in the buffer. "The answer needs a signal that
is not in the manifest" is a legitimate finding — it means edit the manifest and
capture again. It is not a tooling failure.

**Sim-X vs hardware-0/1 is masked, one way only.** Hardware cannot produce X, so
sim-X is a don't-care; hardware-X is always a mismatch. Consequence: a window
that is heavily X (reset, uninitialised RAM, an unclocked domain) can report a
clean MATCH while comparing almost nothing. That is why the masked count is
printed rather than hidden, and why the reading calls a high fraction weak.

**The `signals_nanosoc.yaml` compare is for LOCALISATION, not equivalence.** Its
trigger is deliberately arm-and-go (`htrans[1] == 1'b1`), which fires within a
cycle or two of `run` on hardware but at the first fetch after reset in
simulation — so **the two windows are not phase-aligned**, and the two loads are
different firmware anyway. A red compare on that manifest is expected and means
nothing about the DUT; the *hardware* window is what you read, and the sim side
is there for orientation. This is stated in `signals_nanosoc.yaml` itself, under
"HONEST SCOPE". The compare becomes an equivalence statement only after capture
\#1 yields a loop PC and the trigger is re-armed on that architectural event
(`trigger_time: late`). The CI compare gate is `signals_selftest.yaml`, where
both sides *are* phase-aligned by construction.

**What `make evaluate` proves, and what it does not.** The "hardware" trace is
derived from the *same* simulation, so a green `evaluate` proves: the injected
divergence survives the FSDB write→read round trip, the hardware-path rename and
the window crop; the report states it faithfully; and the reading names the right
class. It does **not** prove that a real Identify capture will land in a
comparable window (see phase alignment), nor that the `hw:` probe paths resolve —
that needs a board and an RM that elaborates. The report cross-check also shares
its comparison rule with the comparator, so it tests the *pipeline*, not the
rule; the rule is what `make negctl` and `tests/test_compare.py` are for.

**A completely quiet tail used to be a harness error. FIXED 2026-07-30.** Run it:

```bash
EVAL_STALL_SHAPE=freeze make -C tests/identify_iice evaluate MODE=late-divergence
#  -> compare.sh exit 1, and the report opens with
#     "FINDING: the hardware trace holds its last value from sample 40 of 64;
#      the tail (24 samples) carries no value changes, consistent with a
#      stalled or wedged capture."
```

What was wrong, and what changed. If the last N samples of the hardware side
carry *no value change at all*, `fsdb2vcd` stops at the last transition, so those
timestamps never come back and `load_hw_trace` saw fewer timestamps than the
manifest depth. It refused to guess the sample grid — correctly, given only the
value changes, but the message then named the *symptom* ("cannot establish the
hardware sample grid: 40 timestamps ... but the manifest depth is 64") on exactly
the case the whole harness exists for. The missing evidence was the FSDB's own
**end time**, which is not in the VCD but is in the FSDB header
(`fsdb2vcd -summary`, `max xtag`). `crop_trace.sample_grid` now takes it and
accepts the grid iff `(depth-1)*period <= end - first <= depth*period`, so the
span is **proved** and the tail is legitimately held forward.

Deliberately still exit 2, because these are *not* the same thing: an unreadable
or truncated FSDB, a signal-set mismatch, an every-other-sample trace whose true
period is smaller than the observed GCD, or any span the end time cannot prove.
See `sample_grid`'s docstring for why the two-sided bound is what makes that
distinction sound, and `compare.sh` negctl **NC5** for the gate — it asserts both
the exit code and that the report names the stall, and it fails on the pre-fix
code.

**MEASURED 2026-07-30 (was speculation): a genuine Identify capture cannot show
this shape.** `identify_sampleclock` changes at **every one of the 1024 sample
timestamps** of the real demo capture — counted in both Identify's own
`write vcd` and the `fsdb2vcd` of its `write fsdb` — and it is aliased onto the
same VCD identifier as the probed clock (INTERFACES.md §7 fact 5). So a real
`write fsdb` always yields exactly `depth` timestamps however wedged the DUT is,
and never reaches the quiet-tail path. Regression test:
`test_identify_sampleclock_changes_at_EVERY_sample_of_the_real_capture`. What
*does* reach it: every FSDB this harness writes itself (`synth_hw_fsdb.py`,
`inject_divergence.py`, and the `normalise_hw_capture` re-emission behind
`make hw-fsdb`), because those carry only the manifest signals. Read a quiet tail
as a fact about the *file* first and about the DUT second — the report says so.
`late-divergence` still defaults to a *spin loop*, which remains the better model
of the live M0 question, where the witness tap proved the core keeps fetching.

**The loop detector needs two full repetitions.** A period is only claimed when
the tail repeats at least twice; a loop longer than the remaining window shows up
as "no clean signature", or as a monotonic address ramp in nWave. That absence is
itself informative ("not a tight spin — here is the PC range"), but the tool will
not say so for you.

**Shape classification is ambiguous by construction.** Every claim the reading
makes is printed with the alternative it cannot rule out. Three that matter:
a one-sample lag cannot tell you *which side* is off by one (DUT synchroniser,
IICE clock edge, or the shadow mirror's own registered stage — INTERFACES.md
§3.2); a stuck bit is stuck *at the probe*, so DUT net / pad / level shifter /
broken probe route all read identically; and a frozen capture is
indistinguishable from a capture that stopped being written.

**The numbers in this file are the selftest manifest** (`depth: 64`, 7 signals, a
synthetic AHB-shaped DUT) — it runs in seconds and is the CI gate. `evaluate`
cannot currently be pointed at `signals_nanosoc.yaml`: `sim-fsdb` compiles the
selftest DUT and its generated shadow module by name, and `gen`'s golden diff is
keyed to the selftest artifacts. Evaluating the real probe set needs the nanosoc
reference bench wired up first (plan §4 Phase 0 step 2,
`tests/micropython_flash_boot/`). So `evaluate` exercises the *pipeline and the
reading*, at a depth 16× smaller than the real one.

**The joined FSDB has not been opened in nWave here.** Launching a GUI takes a
licence seat and a DISPLAY, which this harness never does. What *is* verified,
every run: `fsdbjoin -f` reports the expected member count, and member 1 is
extracted again and re-read with the full expected signal set. `fsdbjoin -a` on a
new file fails on both Verdi vintages installed here — creation goes through a
hand-written virtual FSDB (`.vf`) and `fsdbjoin -c`, which is why both files are
in the output directory.

---

## The question to ask yourself after a run

> **Given a signal name and a cycle offset from a trigger I chose, could I now
> name the next thing to probe?**

If yes, the tooling earned its board slot: it converts "hardware is broken and I
cannot see inside" into "this net, this cycle". If the honest answer is *"I would
need a signal that is not in the manifest"*, that is still an answer — change
`signals_*.yaml` and capture again, which costs one bitstream load, not a week.
If the answer is *"I cannot tell whether the model or the hardware is wrong"*,
believe it: that is a limitation of a two-sided comparison, not something a
better report would fix.
