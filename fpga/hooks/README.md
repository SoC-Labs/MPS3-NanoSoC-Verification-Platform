# `fpga/hooks/` — flow hooks for mps3-nanosoc-platform

Optional project Tcl, sourced by a build stage at a named point. Two working
checks are already here and **both refuse until you declare what they should
assert** — see *The two shipped hooks* below.

**These are not git hooks.** The word means two different things in this
toolkit and they share nothing:

| | what it is | when it runs |
|---|---|---|
| **flow hook** | this directory. `<seam>.tcl`, sourced by a stage | during a build. It can change the bitstream |
| **git hook** | `mk/hooks.mk`, `make hooks-install` | when you type a git command. It cannot touch a build |

The reference ASIC toolkit carries both meanings under one word, and it is a
live source of confusion: somebody looking for the extension seams finds the
git-hook installer, concludes the flow has none, and writes a wrapper script
instead.

---

## The seam contract

A hook is `$(HOOKS_DIR)/<seam>.tcl` — **lower case, underscores, `.tcl`,
exact**. The seam is the file's own name and nothing else selects it: there is
no registration step, no list in `design.mk`, nothing to keep in step.

> **THE VALID SEAM LIST IS `$(FPGA_FLOW_DIR)/flow/common/seams.txt`, ONE NAME
> PER LINE. A FILE IN THIS DIRECTORY WHOSE NAME IS NOT ON THAT LIST NEVER RUNS
> — and nothing at run time says so, because nothing at run time is looking for
> it.** `make check` is the gate that owns this: it warns, names the file, and
> prints the seam it is nearest to.

**This README does not reproduce the list, deliberately.** That would be a
second copy, and CONTRACT.md's third rule exists because of what a second copy
did in the reference toolkit: a hardcoded five-entry whitelist against a
seven-file directory, so two real extension points were undocumented *and*
warned as unrecognised when anyone found them anyway. The copy that is wrong is
always the one you are reading. Ask the tools instead:

```sh
make help-hooks            # every seam, and which of them you have attached
cat $(FPGA_FLOW_DIR)/flow/common/seams.txt
```

Two near-misses are worth knowing about because both look like working hooks:

* `pre-synth.tcl`, `presynth.tcl` — not a seam name. Never runs.
* `pre_synth.tcl.bak`, `pre_synth.tcl.orig` — the stem is a seam, the extension
  is not `.tcl`. Never runs. `make check` reports this one separately, because
  the author clearly knew the seam and the hook is real work that is silently
  not happening.

---

## Four properties, and the last two are why a hook beats a wrapper script

| property | what it means |
|---|---|
| **optional** | an absent hook is silent. Not an error, not a warning, not a line in the log |
| **announced** | a hook that runs prints its path and its runtime: `FLOW: hook: pre_synth -> .../hooks/pre_synth.tcl` … `FLOW: hook: pre_synth done (2s)`. A hook that ran invisibly is a debugging nightmare — three hours into a run, "why is that setting on?" must have an answer in the log |
| **able to abort** | an error raised in a hook **stops the stage**. It is not caught, not downgraded, not counted as a warning |
| **recorded** | the seam name and its runtime land in `hooks_run` in the stage manifest, so a result traces to the project code that shaped it |

The machinery is `proc flow_hook` in
`$(FPGA_FLOW_DIR)/flow/common/flow_utils.tcl` — at the time this template was
written, line 492: line 505 sources the file and re-raises after naming which
hook failed, and line 517 appends `<seam>(<n>s)` to the manifest list. Line
numbers drift; `grep -n 'proc flow_hook' $(FPGA_FLOW_DIR)/flow/common/flow_utils.tcl`
does not.

### There is no advisory-hook mode

Nothing in the flow will catch your hook's error for you, and no knob turns
that off. If part of a hook is genuinely optional — a report that needs a file
that may not exist yet, a query only some tool versions answer — **wrap that
part yourself and say why, in the file, on the line:**

```tcl
# The IP is present only in a packaged build; a non-packaged run of the same
# design is legitimate and must not fail here.
try_step "IP config census" { ... }
```

`try_step` catches and warns. Use it on the optional part only. A `try_step`
wrapped round a load-bearing check converts a real failure into a passing run
with a missing report, which is the exact failure mode this toolkit exists to
remove — and you would be throwing away the one property that makes a hook
better than a script you have to remember to run.

`die` from a hook is the right way to say *this design must not proceed*
(exit 1, a check failed). `flow_refuse` is the right way to say *I could not
measure anything* (exit 2, nothing was checked). They are graded differently by
make and by `ci/lib.sh`, and collapsing them makes a configuration mistake
indistinguishable from a real defect in the design.

---

## What a hook can assume

By the time any hook runs, `flow_boot` has completed. That means:

* **the packs are loaded** — `part <key>`, `board <key>`, `part_have`,
  `board_have` all answer, and a missing key errors naming the key rather than
  returning `""`.
* **the run directories exist** — `$WORK_DIR`, `$LOG_DIR`, `$REPORT_DIR`,
  `$OUT_DIR`, plus `$IN_WORK_DIR` and `$SYNTH_OUT_DIR` for what this stage
  reads. Exactly four directories are created and those are them.
* **identity is set** — `$block_name`, `$RUN_TAG`, `$IN_RUN_TAG`,
  `$board_name`, `$part_name`, `$FLOW_MODE`, `$PLATFORM`.
* **the helpers are there** — `say`, `warn`, `step`, `die`, `flow_refuse`,
  `try_step`, `opt`, `flow_env`, `flow_have`, `flow_assert_input`,
  `fresh_report`, `mf`.
* **the design state the seam is named for**, and nothing beyond it. A
  `pre_synth` hook runs with sources read and nothing elaborated; a `post_impl`
  hook runs on a placed and routed database.

**Declare the globals you read.** `flow_hook` sources the file with
`uplevel 1`, so a hook shares the *caller's* frame — deliberately, so a hook
can adjust a value the stage is about to hand a tool. That frame is usually the
global one, but a hook that assumes it silently reads nothing on the day it is
not:

```tcl
global REPORT_DIR block_name
```

**Ask before you call a tool command.** `flow_have get_cells` is how a hook
finds out whether the question it wants to ask can be asked at all. A hook that
cannot ask must say so — `flow_refuse` — never skip in silence: *"we did not
check"* and *"we checked and it was clean"* must never read alike.

---

## Rules

**A hook must never write into the source tree.** Write under `$REPORT_DIR`
(anything a reader or a gate should collect), `$WORK_DIR` (scratch and tool
databases) or `$OUT_DIR` (shipped artefacts). A hook that edits a file in
`fpga/` or in the RTL makes the next run's inputs depend on the last run's
outputs, and `make clean` no longer returns the project to a known state — the
build becomes reproducible only in the order it happened to be run.

**Do not re-do the flow's work in a hook.** If you find yourself re-running a
step the engine already ran, you want `fpga/overrides/<step>.tcl`, which
replaces one `flow/steps/` file wholesale. `make check` warns whenever an
override is active and the manifest records which step files were the
toolkit's and which were yours.

**Do not put a stage's real work in a hook either.** A hook holds the tool's
licence while it runs. Anything that launches a second tool, parses a large
netlist, or takes minutes belongs in a make target after the stage — see
`<STAGE>_POST_TARGETS` in section 15 of `design.mk` — where it cannot hold two
licences at once and cannot couple an unrelated failure to this run.

**Announce anything surprising.** One `say` line costs nothing and saves the
next reader an afternoon.

---

## The `post_bitstream` trap

> **THE BITSTREAM IS ALREADY WRITTEN WHEN `post_bitstream` FIRES. A HOOK THERE
> CANNOT CHANGE THE DESIGN.**

It is a seam for *reports, checks and exports derived from the finished
artefacts*: a checksum, a deploy manifest, a census of what shipped. Anything
that changes the design — a property, an ECO, a cell, a constraint — is applied
to a database nobody will write out again, and the run ends green having
shipped the design you were trying to fix.

**This is not hypothetical.** The reference ASIC toolkit's `post_route` seam
fires after `write_stream`, `write_netlist` and `write_sdf`. A project placed
its bond-pad ring there. Every stage completed, every gate passed, and the run
streamed **a GDS with no pad ring** — the pads existed in the tool's database
and in none of the files the run shipped. That route stage now `die`s on a
`post_route` hook that changes the instance count, which is a stopped run
instead of a silently empty pad ring; the lesson transfers to every seam whose
name starts with `post_`.

**The last seam that can still change what ships is `post_impl`.** Before you
put a design change there, read the stage script and find out which side of
`write_checkpoint` the seam fires on — a change made after the routed
checkpoint is written reaches the bitstream and not the checkpoint, and the two
then disagree with nothing to say so:

```sh
grep -n 'flow_hook post_impl' $(FPGA_FLOW_DIR)/flow/vivado/*.tcl
```

---

## The two shipped hooks

`pre_synth.tcl` and `post_impl.tcl` are **working checks, not illustrations**,
and they are two halves of one question:

| | asserts |
|---|---|
| `pre_synth.tcl` | every parameter and macro you declared **reached the design the tool is about to synthesise**. The `RTL_PARAMS`-versus-defines trap: `ipx::package_project` drops fileset defines, and an `` `ifdef `` opt-in was false in *every* FPGA build of this codebase for months — proven by an "IDELAY-off" bitstream that came out **byte-identical** to the "IDELAY-on" one |
| `post_impl.tcl` | the primitives that opt-in was supposed to produce are **in the implemented design**, by count. The other half of the same defect: `pre_synth` proves the tool was *told*, `post_impl` proves it *happened* |

Both take a declaration table, both make an **unknown key fatal** — an
expectation nothing reads is worse than no expectation at all — and both
**refuse the stage until the table is declared**, saying exactly what to
declare and where. That is the design, not an oversight: a hook that runs,
finds no expectations and prints nothing is indistinguishable in a log from a
hook that checked everything and was happy.

**Three ways forward, and the third is honest too:**

1. declare the table (each file's header says where, in `design.mk`, with the
   keys and an example);
2. move the check — the seam is the file's own name, so `cp post_impl.tcl
   post_synth.tcl` runs the same census one stage earlier, and every message it
   prints will say `post_synth`;
3. **delete the file.** If this design has nothing to assert at that seam, that
   is the honest way to say so, and nothing else breaks.

Each file also carries a `<<FILL-IN>>` marker in its configuration block.
Delete those lines once the table is declared — `make check` greps for them,
and they are how a fresh scaffold reports that a decision is still outstanding.
(Spelled with a hyphen here on purpose. `make check` greps for the real
spelling, and a README carrying one would be reported as an unfilled decision
in a file that holds none.)

---

## Debugging a hook

```sh
make help-hooks     # every seam; which have a hook here; which files never run
make check          # the same, as a gate, plus every unfilled decision
```

`make check` costs no licence hours and runs in well under a second, so a hook
that is not firing because of its filename shows up before you spend a build on
it.

**`make check` also lists this README under "unrecognised file(s) in hooks/",
and it is right to.** `README.md` is not a seam, so it never runs — which is
exactly what that warning says. It is a warning, not an error; delete this file
if the line bothers you more than the documentation helps.

---

Copyright (C) 2026, SoC Labs (www.soclabs.org)
