# The template set

**You are reading one of two copies of this file, and which one changes what
the third column of the table below says.**

* In the toolkit, as `templates/README.md`, this describes the set of files
  `fpga-flow-init` installs into a project. The markers are unexpanded.
* In a project, as `fpga/README.md`, it is *the record of what was installed
  here* — every marker below has already been replaced with this project's
  values, so the table doubles as a receipt.

This copy was written for **mps3-nanosoc-platform** on **2026-09-23**.

---

## What the set is

Every file in `templates/` is a **file a project will read**, and most of them
are files a project will read *while trying to understand what an FPGA build
needs*. That is why they are long: they are the course, not just the bytes.
Each one carries the measured defect that motivated the rule it states, with
the number that proved it, because a rule with no defect behind it is one the
next person deletes to make a build go through.

```
templates/
    Makefile.in                 -> fpga/Makefile          three lines
    design.mk.in                -> fpga/design.mk         THE manifest
    board.tcl.in                -> fpga/board/<BOARD>/board.tcl
    targets/README.md           -> fpga/targets/README.md
    targets/block.pins.xdc.in   -> fpga/targets/<BOARD>/<BLOCK>.pins.xdc
    targets/block.timing.xdc.in -> fpga/targets/<BOARD>/<BLOCK>.timing.xdc
    targets/block.drc.xdc.in    -> fpga/targets/<BOARD>/<BLOCK>.drc.xdc
    hooks/README.md             -> fpga/hooks/README.md
    hooks/pre_synth.tcl         -> fpga/hooks/pre_synth.tcl
    hooks/post_impl.tcl         -> fpga/hooks/post_impl.tcl
    README.md                   -> fpga/README.md         (this file)
```

`fpga-flow-init` also creates `fpga/overrides/` **empty**. There is no template
for it, and it exists anyway because an absent directory reads as *"this
feature does not exist"* rather than *"you have not used it yet"* — and a step
override is the seam a project reaches for at exactly the moment it is most
convinced the toolkit is wrong about something.

---

## What `fpga-flow-init` does to it

```sh
fpga-flow-init --block <top> --board <board> [--part <p>] [--flow-dir <d>] <project-dir>
```

It copies `templates/` into `<project-dir>/fpga/`, substituting six markers,
rewriting two paths, and asserting at the end that the tree it promised is the
tree on disk. It **never overwrites an existing file** unless `--force` is
given, so it is safe to re-run on a live project — the run reports `skip` for
each file it left alone.

Its exit status is a claim, and the postconditions at the bottom of the script
are there to make the claim true:

| | |
|---|---|
| `0` | `fpga/` exists and holds every template, expanded and non-empty |
| `1` | a postcondition **failed** — files were written and the tree is not the one `templates/` describes |
| `2` | **refused** — bad arguments, or a toolkit checkout with no templates. Nothing was written |

That distinction is not decoration. The reference ASIC toolkit's scaffolder
walked its templates with `find … | while read`, which runs the loop body in a
**subshell** — so every `made` and `skipped` increment was discarded at the
`done`, both counters read `0`, and the command its own README told a new user
to type **exited 0 having written nothing**. The empty directory was discovered
by whatever ran next, several steps and one confused reader away from the
cause.

---

## The placeholder set

Six markers, and only six. Each is the name in capitals wrapped in `@` signs.

| name | what it holds | in this copy |
|---|---|---|
| `BLOCK` | the design's short name; the stem of every artefact the flow writes | nanosoc_mps3 |
| `BOARD` | the board pack this project writes, and the default target directory | mps3-hbi0309c |
| `PART` | the device — normally the board pack's job | *see `PART` in `design.mk`* |
| `FPGA_FLOW_DIR` | the path back to this toolkit, relative when the toolkit sits inside the project | /path/to/nanoSoC-FPGA-Toolkit |
| `DATE` | the day the scaffold was written | 2026-09-23 |
| `PROJECT` | the project directory's name, used in headers | mps3-nanosoc-platform |

**It is kept to six on purpose.** A template full of substitutions stops being
readable as an example of what a real file looks like, and these files are
meant to be read.

`PART` is the one row whose value this table does **not** echo, and the reason
is worth a sentence: `--part` is optional, so on a scaffold that did not pass
one the cell would hold *the unfilled marker itself* — and `make check` would
then report a decision to be made in a documentation table, where making it
would change nothing. The device is named by `PART` in `design.mk`, or better,
by the board pack, which is where a fact about which device is soldered down
belongs.

Two consequences worth knowing:

* **`BLOCK` must be a plain identifier** (`[A-Za-z0-9_]`) because it is also a
  Verilog module name, a make variable and a filename stem. `BOARD` and `PART`
  are checked too. All three are checked *by name*, before anything is written,
  because they are pasted into a `sed` replacement where `&`, `|` and `\`
  change the meaning of the command and a path separator would escape `fpga/`
  entirely.
* **The scaffolder refuses rather than corrupting.** A value carrying one of
  those three characters stops the run with a message naming the value; it does
  not produce a file that is subtly not what was asked for.

### The marker that is never substituted

The `<<FILL-IN>>` marker — spelled with a hyphen *here* on purpose, because
`make check` greps for the real spelling and a README carrying one would be
reported as an unfilled decision in a file that holds none — is **not** in the
substitution set and never will be.

It means *you have not decided this yet*: a clock period, a pin, a device, a
firmware path. Expanding it would turn an open question into a value. Instead:

* `make check` finds every one of them, names the file and the line, and
  **exits non-zero**. A fresh scaffold fails `make check` on purpose.
* Vivado, if one ever reaches it, sees a Tcl syntax error — or, worse, a
  filename it looks for, does not find, and reports at the level of a
  constraint that simply matched nothing.

The reference toolkit's equivalent checker validated that every configured path
*existed*. A scaffolded file exists from the moment it is written, so a project
carrying **122 unfilled markers across eleven files** reported one missing
input and declared the contract complete. The person it failed was the newcomer
whose synthesis died on a literal marker forty minutes and one licence-hour
later.

---

## The two rewrites, and why a template tree needs them

A directory of templates cannot express a directory whose name is only known at
scaffold time. So `fpga-flow-init` rewrites two paths on the way in:

| template | becomes | because |
|---|---|---|
| `board.tcl.in` | `board/mps3-hbi0309c/board.tcl` | the board pack lives in a directory named after the board |
| `targets/block.*` | `targets/mps3-hbi0309c/nanosoc_mps3.*` | `TARGET` defaults to `BOARD`, and target collateral is named after the design |

**The `block.` basename convention.** Any template whose basename starts
`block.` has that prefix replaced with this design's `BLOCK`:

```
targets/block.pins.xdc.in   ->   targets/mps3-hbi0309c/nanosoc_mps3.pins.xdc
```

It is a **rewrite map, not a whitelist.** Anything the map does not name still
installs, at its own path under `fpga/`, with `.in` stripped and a leading
`block.` rewritten. A newly added template therefore appears in a scaffolded
project automatically instead of silently vanishing — and if it ever did
vanish, the count postcondition at the end of the run catches it: every
template must end up either written or deliberately skipped, and a mismatch
exits 1 rather than handing over a partial scaffold that looks finished.

The same shape is stated in `CONTRACT.md` §1 and §3.3 (`BOARD_DIR`,
`TARGET_DIR`). If the script and the contract ever disagree, one of them is a
bug — say which, do not silently pick.

---

## After the scaffold

In order, because each step is what the next one needs:

1. `make check` — lists every unfilled decision and exits non-zero.
2. Fill in `design.mk`, then `board/mps3-hbi0309c/board.tcl`, then the three XDCs in
   `targets/mps3-hbi0309c/`, then decide about `hooks/`. Every boxed warning in those
   files is a defect that was measured, not a caution.
3. `make doctor` (can this machine run the tools?), `make check` again (must
   exit 0), then `make flist` — the first stage that reads your RTL, and it
   needs no licence.

`fpga/hooks/` ships two working checks that **refuse until you declare what
they should assert**. Read `hooks/README.md`. If a check does not apply to this
design, **delete the file** — that is the honest way to say so, and nothing
else breaks.

---

Copyright (C) 2026, SoC Labs (www.soclabs.org)
