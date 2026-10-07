# `ncompare/` — the comparison engine and the nCompare rule interface

Stream C (compare pipeline). Contract: `../INTERFACES.md` §4.

| File | What it is |
|---|---|
| `trace_compare.py` | The comparison engine: pure-Python sample-domain diff, the report writer, `.ncr` rule-file generation and `.nce` parsing. |
| `iice_compare.ncr.example` | A worked example of the generated rule file, kept for review. The real one is generated per-run into `build/compare_logs/`. |

> Note for the integrator: `trace_compare.py` lives here because `ncompare/**` is
> stream C's owned subtree (INTERFACES.md §5) and the engine needed a home
> alongside the rule-file machinery it drives. Move it up a level if you prefer;
> nothing outside stream C imports it.

---

## 1. `nCompare`'s batch interface — CONFIRMED, not guessed

Plan risk **V1 / trap T3** said the batch flags and rule-file syntax were
unpinned because `nCompare -h` printed its `[Batch Mode Options]` and
`[GUI Mode Options]` sections empty. That is resolved.

**The empty sections were a Verdi-version artefact.** On the bare login PATH,
`which verdi` resolves to `/eda/synopsys/2022-23/RHELx86/VERDI_2022.06-SP2/bin`.
After `module load verdi/X-2025.06-SP2` (`VERDI_HOME=/apps/synopsys/vc_formal/X-2025.06-SP2/verdi`),
`nCompare -h` prints the full option list. Nothing here is invented; the sources
are the tool's own help output, the shipped documentation, and the shipped demos:

* `$VERDI_HOME/doc/HTML/verdi_command_ref/appendix_d_ncompare_file_comparison_formats/`
  — "Appendix D: nCompare File and Comparison Formats": the rule-file command
  reference (`cmpOpenFsdb`, `cmpSetCmpOption`, `cmpSetStateMap`,
  `cmpSetSignalPair`, `cmpSetNameMap`, `cmpCompare`, …) and the `.nce`
  **Error File** record layout.
* `$VERDI_HOME/demo/nCompare/nCmp_demo1/` and `nCmp_demo2/` — runnable vendor
  demos with `RUN` scripts and commented `.ncr` rule files.

### Batch invocation

```
nCompare -logdir <dir> -rule <file>.ncr -report <file>.nce [-silence]
```

Other confirmed batch flags: `-wave`, `-signal`, `-scope`, `-level`, `-bt`,
`-et`, `-maxerror`, `-maxerrorpersignal`, `-log_matched`,
`-precise_name_matching`, `-proc`, `-thread`, `-partition_signal_count`.

### The rule file is Tcl

```tcl
cmpOpenFsdb <golden.fsdb> <secondary.fsdb>
cmpSetDelimiter .
cmpSetCmpOption -maxerror 0 -maxerrorpersignal 0 -x T -z T
cmpSetStateMap -asym (x,0,T) (x,1,T) (x,z,T) (0,x,F) (1,x,F) \
                     (z,0,T) (z,1,T) (z,x,T) (0,z,F) (1,z,F)
cmpSetSignalPair {iice.haddr[31:0]} {rp_nanosoc_wrapper.u_nanosoc.HADDR[31:0]}
cmpCompare
```

`cmpSetStateMap -asym` is what makes the contract's **one-way** X rule
expressible in the vendor tool: `(x,0,T)` means golden-X against secondary-0 is
a match, while `(0,x,F)` means golden-0 against secondary-X is a mismatch.
Verified by experiment, three cases, same rule file:

| sim (golden) | synthetic hw (secondary) | nCompare `500` record | meaning |
|---|---|---|---|
| `1x01` | `1101` | `1 0 0` | sim-X vs hw-1 → masked, no error |
| `1x01` | `0101` | `1 1 1` | real bit difference → reported |
| `1x01` | `1x0x` | `1 1 1` | hw-X vs sim-0 → reported (asymmetry works) |

### Three traps, all hit during investigation

1. **`nCompare` always exits 0**, mismatch or not. The verdict must be parsed
   out of the `.nce` `500` record: `… [compared pairs] [mismatched signals]
   [total mismatch records]`. Trusting its exit status would produce a gate that
   can never fail. `parse_nce()` also treats "0 pairs compared" as a harness
   error, because a rule file that matches nothing otherwise looks like a pass.
2. **`nCompare` writes `nCompareLog/` into the CWD** unless `-logdir` is given
   (it did so in the repo root). `fsdb_tools.ncompare()` always passes it. Every
   other Verdi utility does the same with `<tool>Log/`, so all of them are run
   with their CWD inside the build log directory.
3. **`fsdbmangle` is not a rename tool.** The plan's tooling table and
   INTERFACES.md §3.3 both describe it as "rename/remap signals in an FSDB". Its
   actual help text is an *obfuscator*: `fsdbmangle source.fsdb [-seed 100]`,
   which invents **random** names. It has no rename-map input at all, so it can
   never implement the §1 "mangle the hardware side into the flat `iice` scope"
   step. The rename is therefore done two ways instead, both driven from
   `signal_map.tsv`: explicit `cmpSetSignalPair {golden} {secondary}` pairs for
   nCompare, and a `{hw_path: name}` dict in `crop_trace.hw_path_map()` for our
   own comparator. INTERFACES.md §3.3 anticipated this by keeping the map
   tool-neutral, so no frozen interface has to change.

A fourth, smaller one: **`fsdbqry` does not work on ordinary FSDBs**
(`*WARN* Only support streamlined header file in fsdbqry`), so the §4
set-equality precheck cannot use it as written. `list_signals()` converts a
single timestamp with `fsdb2vcd` instead and reads the `$var` header — cheap,
because no value data comes out.

### 1a. `fsdb2vcd -et 0` is wrong for trigger-relative traces

`list_signals()` originally asked for `-et 0` as its "header only" trick. That is
unsafe for **exactly the class of file this harness exists to read**: an IICE
trace is trigger-relative, so its axis starts at an arbitrary non-zero time
(measured: `#10240`), and `fsdb2vcd` then fails:

```
*WARN* End time specified is smaller than FSDB file's minimum time.
fsdb2vcd failed.                       (exit 255)
```

That turned a perfectly good capture into a harness error. It also *did* write a
usable 304-byte header before failing — which is exactly the trap to avoid,
since trusting the output of a tool that reported failure is how silent
corruption gets in.

Fixed by asking the file for its own range first
(`fsdb_tools.fsdb_time_range()`, parsing `min xtag`/`max xtag` from
`fsdb2vcd -summary`) and then converting with `-bt <min> -et <min>`.

**`-keep_last_time` is deliberately not passed**, here or on the data path.
`fsdb2vcd` warns that "the VCD time stops at the last transition by default",
and that default is what we want: the last *sample* is at 20470 while the FSDB's
max time is 20480, one sample period beyond it. `-keep_last_time` would append a
trailing time tag that is not a sample, making a depth-1024 trace read as 1025
samples. `sample_grid()` would catch that as an error, and its message names the
flag.

---

## 2. Why our own comparator decides, and nCompare only corroborates

nCompare's batch mode works and is used. It is still not the primary comparator,
for reasons that are about the required output rather than about the tool:

1. INTERFACES.md §4 requires the report to state the **index of the first
   diverging sample** and the **count of X-masked samples**. nCompare is
   time-based: `.nce` gives mismatch *times*, and it never counts don't-cares.
   Neither number can be recovered from it.
2. Plan §5.1 requires trigger-relative alignment. A real Identify capture has
   its own unrelated time base, so aligning by sample index is work this harness
   must do regardless of who does the value diff.
3. The negative control and the crop/offset maths have to be testable on a box
   with no Verdi and no licence. A pure-Python core over plain data is; an opaque
   vendor invocation is not.

So `run_compare()` does the diff, and then — when `nCompare` and `vcd2fsdb` are
on PATH — re-emits both sides as sample-indexed FSDBs (golden at `iice/<name>`,
secondary at the `hw_path` names) and asks nCompare for an independent verdict
on the same samples.

**If the two verdicts disagree, the exit code is 2, not 0 and not 1.** A
disagreement means we do not know the answer, and INTERFACES.md §4 forbids
reporting a harness error as a match. Set `IICE_NO_NCOMPARE=1` to skip the
cross-check.

### Scope of the cross-check — stated plainly

It **does** independently check the value comparison and the X/Z masking rule,
on the same sample data, through a different implementation, including the
hardware-path rename (the pairs in the rule file come from `signal_map.tsv`).

It **does not** independently check FSDB loading, sample-clock-edge extraction,
or window cropping — both sides are fed from the same in-memory sample tables.
Those are covered by `../tests/test_compare.py`, which constructs traces by hand
and asserts the trigger offset, all three `trigger_time` conventions, and the
one-way X rule directly.

---

## 3. What `make negctl` actually asserts

`compare` passing is worth nothing on its own. `negctl` is the target that gives
it meaning, and it makes four assertions against real FSDBs:

| | Assertion | Wanted exit |
|---|---|---|
| NC1 | flip exactly one bit of one signal at one sample | **1** |
| NC2 | omit one data signal from the hardware FSDB | **2** |
| NC3 | hand the pipeline a corrupt file instead of an FSDB | **2** |
| NC4 | put an **X on the hardware side** where the sim has a known 0/1 | **1** |

NC4 exists because the selftest DUT's window contains **no X at all**, so the
one-way don't-care rule would otherwise never be exercised by the gate — only by
`../tests/test_compare.py`. NC4 forces it, and confirms the X survives the
`vcd2fsdb` → FSDB → `fsdb2vcd` round trip rather than being quantised away.

`--perturb` and `--inject-hw-x` both **refuse** a target where the *simulation*
value is X: the one-way rule would legitimately mask the change, and the
"negative control" would pass while proving nothing. `--perturb-auto` /
`--inject-hw-x-auto` therefore pick the first 0/1 bit at or after the trigger.

The negative control has itself been checked by sabotage: with
`compare_bits()` stubbed to report no differences, `negctl` fails loudly (NC1
prints "The comparator reported a MATCH on a trace with a deliberately flipped
bit") and `make negctl` exits 2 — both with and without the nCompare
cross-check. 18 unit tests fail on the same sabotage.

---

## 4. Scope resolution: by leaf set, not by name

The scope *name* differs between the two sides, and that is by design. The sim
side dumps with `$fsdbDumpvars(0, iice_shadow_<NAME>)`, and FSDB records the
**instance** path, so the mirrors land in `tb/u_iice_shadow/*`; the hardware side
carries whatever the normalisation produces. INTERFACES.md §1 (amended
2026-07-29, after both Stream B and Stream C read a real FSDB back) therefore
identifies the scope by its **leaf set**:

> both traces expose exactly one scope whose leaf set is exactly the manifest
> signal set

`crop_trace.resolve_sim_scope()` implements exactly that, and the enforcement is
non-negotiable:

| Case | Result |
|---|---|
| exactly one scope whose leaf set == manifest set | proceed |
| partial match | **exit 2** |
| more than one matching scope (ambiguous) | **exit 2** |

Never guess a scope, and never compare a partially-matched leaf set — comparing
the wrong `haddr` against the wrong `haddr` is the precise failure this harness
exists to prevent. Because leaf names come only from the manifest, `haddr` is
always compared against `haddr` regardless of the enclosing scope name.

`compare_report.txt` prints the resolved scope and how it was resolved (leaf
count, number of candidate scopes in the FSDB). That is diagnostic output, not a
warning: there is nothing to fix.

---

## 5. The real hardware side — layout MEASURED, normalisation implemented

A real Identify capture now exists (`write fsdb -iice IICE_PROBE -range {0 1023}`
over `com cabletype demo`, so no board was involved). The layout is no longer
guessed at. Everything below was read off the actual FSDB.

```
$scope module rm_led $end                  <-- ONE scope, the instrumented module
$var wire  1 ! identify_sampleclock $end
$var wire  1 " identify_cycle       $end
$var reg   1 ! dut_clk              $end   <-- SAME ident '!' as sampleclock
$var reg  26 # blink_counter [25:0] $end
$var reg   1 $ dut_resetn           $end
1024 timestamps, #10240 .. #20470 step 10  (depth 1024)
```

Five load-bearing facts, and what each one forced:

| Fact | Consequence in the code |
|---|---|
| **One timestamp per sample, count == IICE depth exactly** | The hardware side is never cropped — it arrives already windowed. Confirms the offline-crop model of INTERFACES.md §0. |
| **Time base is arbitrary and non-zero** (trigger-relative) | Sample index is the timestamp **ordinal**. Nothing keys off absolute time. Also the root cause of the `-et 0` bug in §6 below. |
| **Design hierarchy is flattened away** — `blink_counter`, not `/top/u_blink/blink_counter` | The hardware side is matched on the **leaf** of the manifest `hw_path` (`hw_leaf_map()`). Two probes sharing a leaf is exit 2: a flat trace cannot tell them apart. |
| **`identify_sampleclock` and `dut_clk` share VCD identifier `!`** | An identifier maps to a **list** of names, not one. Collapsing it would silently drop whichever name lost. |
| **Identify injects two signals of its own** | Named whitelist `IDENTIFY_INJECTED`; they are reported but not compared. An *unrecognised* extra is still exit 2 — the check was not widened to "ignore anything unexpected". |

On `identify_cycle` specifically: it is declared but carries **zero value
changes**, and is not even present in `$dumpvars`. So it cannot be kept as
provenance in any useful sense — carrying it would mean carrying an all-X column
into the comparison. Its *presence* is recorded in `compare_report.txt` instead,
which is the provenance that actually exists.

`crop_trace.normalise_hw_capture()` re-emits a capture as one timestamp per
sample at the manifest `hw_path` names, so `make compare` reads it exactly like
the synthetic phase-0 FSDB. Driven by:

```
IICE_RAW_FSDB=/path/to/capture.fsdb make -C tests/identify_iice hw-fsdb
make -C tests/identify_iice compare
```

That normalisation path **is** exercised — against the real capture, with a
round-trip equality check (`tests/test_compare.py`, plus a synthesised
non-zero-time fixture so the tests do not depend on the gitignored binary).

### Still not executed

* The **capture** half of `hw-fsdb` (talking to the board) — still gated behind
  `IICE_ALLOW_HW=1`. Needs stream D/E, a bitstream and a lease.
* **`raw2fsdb`** was never needed: Identify's `write fsdb` emits FSDB directly.
  The `LD_LIBRARY_PATH` wrapper for `libumr3.so` remains in `fsdb_tools` but its
  argv is still passed straight through, unproven.
* Plan risk **V2** (FSDB version skew) is **closed in practice**: the capture
  reports `fsdb version 6.0` / `simulator version HAPS_PRODUCT_HAPS-KEY-70`, and
  Verdi X-2025.06 reads it with only a benign "generated using a previous
  version" warning.

### Two debugger traps, both fatal to a run

* `write fsdb` needs an explicit `-range {0 <depth-1>}`, else
  `Error: End (maximum) value out of range`.
* The trigger statemachine must be configured **before** `run`, else
  `Error: Trigger statemachine must be configured for iice <name> before
  running`. The `.idc` only *sizes* it. Minimal working form:

```tcl
statemachine clear    -iice <name> -all
statemachine addtrans -iice <name> -from 0 -trigger
```
