# FROZEN INTERFACES — do not change without updating every consumer

This file is the contract between the parallel workstreams building the
sim-vs-hardware IICE trace harness. **Everything in this file is frozen.** If you
believe something here is wrong, say so in your report — do **not** silently
change it, because three other workstreams are coding against it.

Design plan: `docs/planning/IICE_SIM_VS_HW_TRACE_PLAN.md`.
Feasibility/background: `docs/planning/IDENTIFY_IICE_DFX_PLAN.md`.

---

## 0. Key design decision — read this first

The plan originally described a "shadow IICE" with a ring buffer in the
testbench. **That is not what we are building.** A hardware IICE buffers because
it has to; a simulation does not.

Instead:

1. The sim mirrors the probe set into **registered** copies clocked on the same
   edge as the real IICE's sample clock. Registering is what makes it faithful —
   the IICE only ever sees values at the sample edge, never combinational
   glitches between edges.
2. Those registered mirrors are dumped to FSDB continuously.
3. The **window is cropped offline in Python**, using the recorded trigger time
   and the manifest's `depth` / `trigger_time`. This reproduces
   `-triggertime middle` exactly, and means no hardware-shaped buffer has to be
   written, verified, or kept in sync.

Consequence: there is **no shared SystemVerilog support package**. The generated
shadow module is self-contained.

---

## 1. Single flat leaf set — the naming problem is solved by construction

**AMENDED 2026-07-29** after implementation. The original text mandated a scope
literally named `iice`. That is not achievable: `$fsdbDumpvars(0, <module>)`
records the **instance** path, so the sim FSDB carries `tb/u_iice_shadow/*`.
Streams B and C both confirmed this by reading a real FSDB back. The contract now
describes what is real; the guarantee that actually matters is unchanged.

**The guarantee: both traces expose exactly one scope whose leaf set is exactly
the manifest signal set**, each leaf named exactly the manifest `name`:

```
<scope>/haddr       <scope>/htrans     <scope>/hwrite
<scope>/hrdata      <scope>/hready     <scope>/lockup
<scope>/trigger_marker              <-- 1 bit, ALWAYS present
<scope>/sample_clk                  <-- 1 bit, ALWAYS present
```

The scope *name* differs between sides (`tb/u_iice_shadow` in sim; whatever the
hardware normalisation produces). The compare pipeline resolves it by finding the
**unique** scope whose leaf set matches the manifest. Enforcement, non-negotiable:
- exactly one matching scope → proceed
- partial match → **exit 2**
- more than one matching scope (ambiguous) → **exit 2**

Never guess a scope, and never compare a partially-matched leaf set. Leaf names
still come only from the manifest, so `haddr` is always compared against `haddr`.

Reserved names — a manifest signal may **not** be called any of these:
`trigger_marker`, `sample_clk`, `sample_index`.

---

## 2. `signals.yaml` — the manifest schema (FROZEN)

```yaml
iice:
  name: IICE_SELFTEST          # -> module iice_shadow_<name>, and iice new {<name>}
  depth: 1024                  # >= 8. No power-of-2 requirement.
  trigger_time: middle         # early | middle | late  (middle => window is
                               #   depth/2 pre-trigger, depth/2 post-trigger)
  controller: statemachine     # none | counter | statemachine
  trigger_conditions: 2        # each one DOUBLES the trigger RAM. Keep low.
  trigger_states: 2            # 2..10 (the Identify binary says 2..10; its docs
                               #   also say 2..16 in one place - trust 2..10)
  clock:
    hw:   /rp_nanosoc_wrapper/dut_clk      # path as Identify sees it
    sim:  tb.u_dut.dut_clk                 # ABSOLUTE sim path, dot-separated
    edge: positive                         # positive | negative

signals:
  - name:    haddr             # FSDB leaf name; [a-z0-9_]+; must be unique
    width:   32                # bits; 1 => scalar logic
    hw:      /rp_nanosoc_wrapper/u_nanosoc/.../HADDR    # Identify path (slashes)
    sim:     tb.u_dut.u_ss_cpu.u_cpu_0.HADDR            # sim path (dots)
    sample:  true              # in the sample buffer
    trigger: false            # participates in trigger logic
    radix:   hex               # hex | bin | dec | oct  -- applied on BOTH sides

trigger:
  # SystemVerilog expression over the manifest `name`s ONLY (never raw paths).
  # Emitted verbatim into the shadow module; translated to IICE watch/statemachine
  # settings by gen_idc.py.
  expr: "htrans == 2'b10 && haddr == 32'h0000_0000"
```

Rules:
- `sim` paths are **dot-separated and absolute from the TB top**. `hw` paths are
  **slash-separated** (Identify convention).
- Any signal with `trigger: true` must appear in `trigger.expr`, and every
  identifier in `trigger.expr` must be a manifest `name`. `make gen` enforces both.
- Two manifests exist and both must always generate cleanly:
  - `signals_selftest.yaml` — tiny, points at the selftest DUT. Runs in seconds.
    **This is the manifest the CI gate uses.**
  - `signals_nanosoc.yaml` — the real DUT probe set. Generates, but is only
    simulated on demand (the reference bench is slow).

---

## 3. Generated artifacts (FROZEN formats)

All generated output goes to `build/` (gitignored). Committed copies for
diffing live in `golden/`. `make gen` regenerates and **fails if the result
differs from `golden/`** — that is the anti-drift gate.

### 3.1 `build/<iice_name>.idc` — for Identify

```tcl
device jtagport soft
device xilinxinsertbufg 0
device skewfree 1
device stop_on_signal_not_found 1
iice new {IICE_SELFTEST} -type regular
iice clock   -iice {IICE_SELFTEST} -edge positive {/path/to/clk}
iice sampler -iice {IICE_SELFTEST} -depth 1024
iice controller -iice {IICE_SELFTEST} statemachine
iice controller -iice {IICE_SELFTEST} -triggerconditions 2 -triggerstates 2
signals add -iice {IICE_SELFTEST} -sample -trigger {/path/to/haddr}
```
The three `device` lines after `jtagport soft` are **mandatory** — see
`IDENTIFY_IICE_DFX_PLAN.md` §2 (the RP pblock has zero BUFG sites).

### 3.2 `build/iice_shadow_<iice_name>.sv` — for the simulation

A **portless** module using absolute hierarchical references. Instantiated once
in the TB as `iice_shadow_IICE_SELFTEST u_iice_shadow();` — no wiring.

```systemverilog
// GENERATED by gen_shadow.py from signals_selftest.yaml -- DO NOT EDIT
module iice_shadow_IICE_SELFTEST;
  localparam int IICE_DEPTH = 1024;

  logic sample_clk;
  assign sample_clk = tb.u_dut.clk;              // manifest clock.sim

  // registered mirrors -- clocked on the manifest edge. THIS is what makes the
  // trace IICE-faithful: one value per sample edge, no combinational glitches.
  logic [31:0] haddr;
  logic [1:0]  htrans;
  always_ff @(posedge sample_clk) begin
    haddr  <= tb.u_dut.haddr;                    // manifest signals[].sim
    htrans <= tb.u_dut.htrans;
  end

  logic trigger_marker;
  always_ff @(posedge sample_clk)
    trigger_marker <= (htrans == 2'b10 && haddr == 32'h0000_0000);

  initial begin
    string f;
    if (!$value$plusargs("iice_fsdb=%s", f)) f = "sim_iice.fsdb";
    $fsdbDumpfile(f);
    $fsdbDumpvars(0, iice_shadow_IICE_SELFTEST);
  end
endmodule
```
- Enabled by plusarg `+iice_shadow`; the TB must not instantiate FSDB dumping
  by default (it must not slow existing gates).
- `+iice_fsdb=<path>` overrides the output file.
- Note `trigger_marker` is registered off the *already-registered* mirrors, so it
  is one sample-edge later than the raw condition. That is intentional and
  matches how the cropper reads it: **`trigger_marker` high at sample N means the
  trigger condition held at sample N-1.** The cropper owns this offset; do not
  "fix" it in the generator.

### 3.3 `build/signal_map.tsv` — the neutral rename table

Tab-separated, one header line, one row per manifest signal plus the two
reserved signals. **This is the frozen interface between the generators and the
compare pipeline.** The generators never emit tool-specific syntax; the compare
stream translates this table into whatever the tools need.

> **AMENDED 2026-07-29 — `fsdbmangle` is NOT a rename tool.** Both the plan and
> the original text here described it as one. It is an **obfuscator**: it takes a
> `-seed` and invents random names. It can never perform the hardware-side rename.
> The rename is done instead via `nCompare`'s `cmpSetSignalPair` (explicit
> old/new pairs) plus a Python dict, both generated from this same
> `signal_map.tsv`. No frozen field changed — only the consumer.

```
name	width	radix	hw_path	sim_path
haddr	32	hex	/rp_nanosoc_wrapper/u_nanosoc/.../HADDR	tb.u_dut.u_ss_cpu.u_cpu_0.HADDR
trigger_marker	1	bin	__DERIVED__	__DERIVED__
sample_clk	1	bin	/rp_nanosoc_wrapper/dut_clk	tb.u_dut.clk
```
`__DERIVED__` means the signal is synthesised by the harness on that side rather
than read from the design.

---

## 4. Compare pipeline contract

- Input: `build/sim_iice.fsdb` and `build/hw_iice.fsdb`, both already in the flat
  `iice/*` scope of §1.
- Output: `build/compare_report.txt` — human-readable, and it **must** state:
  total samples compared, mismatch count, per-signal mismatch counts, the index
  of the **first** diverging sample, and the count of X-masked samples.
- **Exit codes (FROZEN):**
  - `0` = compared successfully, zero mismatches
  - `1` = compared successfully, mismatches found
  - `2` = harness error (missing signal, signal-set mismatch, unreadable FSDB,
          missing tool). **A harness error must never be reported as a match.**

  > **AMENDED 2026-07-30 — a QUIET HARDWARE TAIL moves from 2 to 1.** The three
  > codes are unchanged and still frozen; what changed is which case maps to
  > which, so it is recorded here rather than left to drift.
  >
  > A hardware trace whose tail carries **no value change on any signal** used to
  > be exit 2. `fsdb2vcd` "stops the VCD at the last transition", so those
  > trailing timestamps never came back, `crop_trace.sample_grid` saw fewer
  > timestamps than `depth`, and it refused to establish the grid. That refusal
  > was right on the evidence it had, but it landed on the one shape this harness
  > exists to look at — a stalled or wedged DUT — and reported the symptom
  > ("40 timestamps but the manifest depth is 64") rather than the finding.
  >
  > The span is now **proved** from the FSDB's own end time (`fsdb2vcd -summary`,
  > `max xtag` — see §7 fact 6, which is what made it available), accepted iff
  > `(depth-1)*period <= end - first <= depth*period`. The tail is then held
  > forward and the verdict comes from the value diff like any other trace, so a
  > stall is exit **1** with the report naming it.
  >
  > **Nothing else moves.** Exit 2 is still required, and still tested, for: an
  > unreadable FSDB; a signal-set mismatch; a capture truncated for harness
  > reasons (its end time is short, which *fails* the bound); an ambiguous
  > every-other-sample trace whose true period is smaller than the observed gap
  > GCD (the end time refutes the larger period rather than confirming it); and
  > any span the end time cannot prove — including when no end time is available.
  > If the span cannot be established honestly, refuse. Gated by `compare.sh`
  > negctl **NC5** (exit code *and* report text), which fails on the pre-fix code.
  > **AMENDED 2026-07-31 — MISMATCHED ARTIFACTS ARE NOW EXIT 2.** The three codes
  > are unchanged and still frozen. What is added is a set of refusals that run
  > **before any value is compared**, because comparing artifacts that were
  > produced against different things is worse than not comparing at all: it
  > yields a *confident wrong answer*.
  >
  > The class, evidenced three times on 2026-07-30, each time producing plausible
  > nonsense rather than an error:
  > 1. `make hw-fsdb` read whichever `build/signal_map.tsv` the last `make gen`
  >    left behind — normally the **selftest** one — so a real nanosoc capture was
  >    normalised against the wrong signal set. Patched then by adding a
  >    `signal-map` prerequisite to that one target; nothing stopped any other
  >    entry point, or a hand-run `trace_compare.py`, from the same pairing.
  > 2. `signals_nanosoc.yaml` was retargeted to 13 signals / 113 bits while the
  >    built bitstream carries 6 signals / 69 bits. Nothing compared the manifest
  >    against the instrumentation that was actually built; it surfaced only
  >    downstream, as a set-equality failure, and only because the signal *names*
  >    happened to differ. A retarget that kept the names would have compared
  >    cleanly and lied.
  > 3. A readback `.ll` must correspond to the **loaded RM**, not the base.
  >    Nothing enforced that either.
  >
  > **Every artifact this harness writes now carries a provenance stamp** —
  > `<artifact>.prov.json`, JSON, `schema: 1`, using the same `static_id` /
  > `static_usercode` / `rm_id` keys and the same `0xXXXXXXXX` form as the overlay
  > manifests (`fpga/dfx/README.md` "Overlay triples",
  > `docs/PLATFORM_ONE_IMPL.md`). It records the manifest identity it was produced
  > against, the raw capture's path and size, the sample count actually recovered,
  > how the sample grid was established, and any ids the caller supplied. It is
  > also returned in `normalise_hw_capture`'s existing `desc` dict
  > (`desc["provenance"]`) and printed in `compare_report.txt`'s new
  > `-- provenance --` block.
  >
  > **Manifest identity** = `sha256` over the IICE name, the depth, the sample
  > clock and the **ordered** signal set (name / width / hw / sim / sample /
  > trigger). It changes exactly when the probe set changes. `radix` is
  > deliberately **excluded** — it is a display choice, and re-rendering `haddr`
  > in binary must not invalidate an existing capture. So are `trigger.expr`,
  > `controller`, `trigger_conditions` and `trigger_states`: they change the
  > trigger, not the recorded probe set, and the report already shows where the
  > window came from.
  >
  > `compare` now refuses, **exit 2**, when:
  > - `signal_map.tsv` was not generated from `--manifest` (checked row-for-row
  >   against the manifest, at every entry point — incident 1's general fix);
  > - a provenance stamp beside either FSDB names a different manifest identity,
  >   or describes a file of a different size (a stale stamp beside a replaced
  >   artifact — incident 3's shape), or declares a schema this harness cannot
  >   parse;
  > - the caller supplied `--expect-rm-id` / `--expect-static-id` (or
  >   `IICE_EXPECT_*`) and the stamp records a different value;
  > - an Identify instrumentation log describes this manifest's IICE and
  >   disagrees with it on depth, sample-buffer width, sample clock, probe set or
  >   per-probe `sample`/`trigger` flags — the 69-vs-113 check.
  >
  > **PERMISSIVE, deliberately and load-bearingly — absence is not a mismatch:**
  > - **no stamp at all** compares exactly as before. Every capture taken before
  >   2026-07-31 and every hand-made phase-0 FSDB is unstamped, and the existing
  >   corpus must keep working. The report says the trace is *unverified* rather
  >   than silently omitting the block.
  > - **per field**: a stamp that records no `static_id` is silent about
  >   `static_id`; only a recorded-and-different value is fatal.
  > - **no `identify.log`** (`build/` is gitignored, so a fresh clone has none) is
  >   a documented SKIP. A log that instruments a *different* IICE — the selftest
  >   manifest is a simulation-only fixture and is never in a bitstream — is
  >   reported as `not_applicable`, an explicit non-result, never a silent pass.
  >   `IICE_REQUIRE_IDENTIFY_LOG=1` / `--require-identify-log` makes both fatal.
  > - **`rm_id` / `static_id` are recorded but not self-enforced.** What the
  >   harness can prove unaided is that a capture was produced against *this*
  >   manifest; which RM was resident is a runtime fact only the caller has
  >   (`scripts/mps3_state.sh --quiet` prints `RM_ID=` from `DFXCTL.RM_ID` @
  >   `0x44A1_0010`). Supplying `IICE_EXPECT_RM_ID` turns it into an enforced one.
  >
  > Gated by `compare.sh` negctl **NC6** (a stamp naming a different probe set,
  > with every value, name and width correct, so no other check can see it),
  > **NC7** (the anti-weakening control: the same trace with no stamp must still
  > exit 0 *and* be reported as unverified), **NC8** (a `signal_map.tsv` perturbed
  > only in `sim_path`, invisible to every pre-existing check) and **NC9** (the
  > manifest-vs-BUILT check must fire on a disagreeing log and degrade with none).
  > NC6, NC7 and NC8 all fail on the pre-fix code — NC8 as an exit-0 `VERDICT:
  > MATCH`. Unit twins in `tests/test_provenance.py`; tool `provenance.py`; entry
  > point `make -C tests/identify_iice identity`.

- Before comparing, list the leaves of both files and assert **set equality**
  against `signal_map.tsv`. A missing signal is exit 2, never a skipped row.
  > **AMENDED 2026-07-29 — do not use `fsdbqry` for this.** It fails on every
  > ordinary FSDB on this install, both Verdi vintages, including on Verdi's own
  > `vcd2fsdb` output: `*WARN* Only support streamlined header file in fsdbqry`.
  > Streams B and C found this independently. Use `fsdb2vcd -et 0` instead.
- **`nCompare` always exits 0**, match or mismatch. The verdict must be read from
  the `500` record in its `.nce` report. A gate that trusts its exit status cannot
  fail, which is worse than having no gate.
- **Never `realpath()` a Verdi utility.** Every tool in the Verdi `bin` is a
  symlink to a single `.wrapper` that dispatches on `argv[0]`; resolving the
  symlink makes every tool exit 127.
- X/Z rule: sim-X vs hardware-0/1 is **don't-care, one way only** (hardware can
  never produce X). Count and report masked samples — a run that is 90% masked
  must be visibly worthless, not silently green.

---

## 5. FILE OWNERSHIP — do not write outside your set

Paths are relative to the worktree root
`<repo>`.

| Stream | OWNS (create/edit freely) |
|---|---|
| **A — generators** | `tests/identify_iice/signals_selftest.yaml`, `signals_nanosoc.yaml`, `gen_idc.py`, `gen_shadow.py`, `gen_mangle.py`, `manifest.py`, `golden/**`, `tests/test_generators.py` |
| **B — sim side** | `tests/identify_iice/selftest_dut.sv`, `tb_selftest.sv`, `Makefile.sim`, `run_selftest.sh`, `tests/test_sim_smoke.py` |
| **C — compare** | `tests/identify_iice/compare.sh`, `crop_trace.py`, `synth_hw_fsdb.py`, `fsdb_tools.py`, `provenance.py`, `Makefile.compare`, `ncompare/**`, `tests/test_compare.py`, `tests/test_provenance.py` |
| **D — host XVC** | `host/socket_harness/xvc_server.py`, `host/socket_harness/tests/test_xvc_server.py`, `host/identify/**` |
| **E — FPGA build** | `fpga/rp/nanosoc_iice/**` |
| **integrator (me)** | `tests/identify_iice/Makefile`, `README.md`, `INTERFACES.md`, root `Makefile`, `.gitignore` |

Nobody else may touch `tests/identify_iice/Makefile` — it `include`s
`Makefile.sim` and `Makefile.compare`, which are separately owned. This is
deliberately arranged so the classic parallel-agent Makefile collision cannot
happen.

> **ADDED 2026-07-31 — `provenance.py` + `tests/test_provenance.py` (stream C).**
> Listed above rather than left implicit. Note the one cross-stream seam: nothing
> writes a **sim-side** stamp yet, because `Makefile.sim` is stream B's. The
> consumer side is already wired symmetrically (`run_compare` checks a sim stamp
> with the same machinery and reports its absence), so stream B can start
> stamping `build/sim_iice.fsdb` with `provenance.build_stamp(...,
> origin="sim", produced_by="Makefile.sim")` and it will be enforced immediately,
> with no change here. Until then a stale sim FSDB remains the one artifact in
> this directory that nothing binds to a manifest.

**No agent commits.** Leave the tree dirty; the integrator commits.

---

## 6. Environment facts you will need

- Modules: `vcs/W-2024.09-SP2-3-PC` (default) or `vcs/T-2022.06-SP2`;
  `verdi/X-2025.06-SP2` (default) or `verdi/T-2022.06-SP2`;
  `synplify/2022.09-SP2`; `identify/2022.09-SP2`; `vivado/2024.1`.
- Verdi utilities live in `$(dirname $(which verdi))`: `nCompare`, `ncmp`,
  `fsdbmangle`, `fsdbjoin`, `fsdbqry`, `fsdbreport`, `fsdbdir`, `vcd2fsdb`,
  `fsdb2vcd`.
- **`nCompare` creates `nCompareLog/` in the CWD** unless given `-logdir`. Always
  pass it. (`nCompareLog/` is gitignored.)
- **`raw2fsdb` needs `LD_LIBRARY_PATH`.** It ships broken:
  `error while loading shared libraries: libumr3.so`. The library is present at
  `/eda/synopsys/2022-23/RHELx86/SFPGA_2022.09-SP2/identify/linux_a_64/libumr3.so`.
  Wrap it — **never modify the vendor tree**.
- **`nCompare`'s batch interface is CONFIRMED** (amended 2026-07-29; my earlier
  "could not determine" was wrong). The empty `[Batch Mode Options]` section was a
  version artefact: the bare login `PATH` resolves `verdi` to `VERDI_2022.06-SP2`.
  After `module load verdi/X-2025.06-SP2`, `-h` prints the full list.
  Batch form: `nCompare -logdir <d> -rule <f>.ncr -report <f>.nce [-silence]`.
  Rule files are **Tcl** (`cmpOpenFsdb`, `cmpSetCmpOption`, `cmpSetStateMap`,
  `cmpSetSignalPair`, `cmpCompare`). Docs:
  `$VERDI_HOME/doc/HTML/verdi_command_ref/appendix_d_ncompare_file_comparison_formats/`;
  examples in `$VERDI_HOME/demo/nCompare/`. The one-way X rule of §4 is
  expressible as `cmpSetStateMap -asym (x,0,T) (x,1,T) (0,x,F) (1,x,F)`.

### VCS + FSDB: the recipe that works (established by execution, Stream B)

```
-debug_access[+all]      on the vcs command line
+ VERDI_HOME set         at COMPILE time AND at RUN time
+ NOVAS_HOME UNSET
+ NO -P / novas.tab / pli.a
```

Three ways this fails **silently** — clean compile, exit 0, `VERDICT=PASS`, and
**no FSDB written**. Any target that produces an FSDB must therefore assert the
file exists and is non-empty; never trust the exit code:
1. **`-P <novas.tab> <pli.a>` is deprecated from VERDI2024.09** and is rejected at
   *run* time by the X-2025.06 PLI. The classic recipe still works against the
   2022.06 PLI, which makes this maddening to diagnose.
2. **`VERDI_HOME` unset at run time** → `Cannot load libsscore_vcs*.so`.
3. **The login shell's `NOVAS_HOME` points at `/opt/synopsys/verdi/Verdi_N-2017.12`**
   → `*Verdi* Failed to load FSDB dumper`. Unset it.

**VCS and Verdi vintages must be paired** — the `libsscore_vcs<YYYYMM>.so` sets are
disjoint: `vcs/T-2022.06 ↔ verdi/T-2022.06`, and `vcs/W-2024.09 ↔ verdi/X-2025.06`.
`Makefile.sim` selects by probing for the matching `libsscore` rather than trusting
`VERDI_HOME`. Note `vcs -ID` prints two versions — only `Compiler version` is
authoritative, and the compiler comes from `$VCS_HOME`.

`fsdbqry`, `fsdbdir` and `fsdb2vcd` each litter the CWD with `<tool>Log/`, exactly
like `nCompareLog/`. All are gitignored.
- **READ-ONLY, NEVER WRITE:** the Arm IP library (`$ARM_IP_LIBRARY_PATH`),
  the physical IP library, and everything under `/eda/**` and
  `/apps/**`.
- Licences are shared lab-wide. Do not leave GUI tools or long VCS runs
  in the background; do not launch `verdi` interactively.

---

## 7. Identify semantics settled from the vendor documentation

All from `/eda/…/SFPGA_2022.09-SP2/identify/doc/identify_debug_env_reference.pdf`
(page numbers as printed). These were guesses in the plan; they are now sourced.

- **`signals add` with NEITHER `-sample` nor `-trigger` means BOTH** (p.68: "*The
  -sample and -trigger options can be combined or both options can be omitted to
  specify a signal for both sampling and triggering.*"). `gen_idc.py` therefore
  always emits at least one flag explicitly, and the manifest validator **rejects**
  `sample: false` + `trigger: false` as inexpressible.
- **`-triggertime` is a DEBUGGER-only option** (pp.52/54), not an `.idc` setting.
  The manifest still carries `trigger_time` because the offline cropper needs it;
  it must **not** be emitted into the `.idc`.
- **The sample clock cannot itself be sampled** (p.47), so it is never
  `signals add`-ed — but it *is* mirrored into the FSDB on the sim side as
  `sample_clk`, which is a harness signal, not an IICE probe.
- **`trigger_states`**: the docs say 2..16 (p.51); the binary enforces **2..10**.
  We enforce 2..10. Trust the binary. The validator's error message cites both, so
  the next person to read the PDF does not mistake this for our bug.
  Also from p.51: *"powers of 2 are preferable as other integers limit
  functionality and do not provide any cost savings"* — so the genuinely useful set
  inside 2..10 is just **2, 4, 8**. Both shipped manifests use 2.

### The hardware-side FSDB layout — MEASURED 2026-07-29, no longer a guess

From a real `write fsdb -iice IICE_PROBE -range {0 1023}` captured off the demo
cable (no board), converted with `fsdb2vcd`:

```
$var wire 1 ! identify_sampleclock $end
$var wire 1 " identify_cycle       $end
$var reg  1 ! dut_clk              $end
$var reg 26 # blink_counter [25:0] $end
$var reg  1 $ dut_resetn           $end
timestamps: exactly 1024  (== `iice sampler -depth`)
time axis:  #10240 .. #20470, step 10
```

Eleven facts the hardware normalisation depends on. 5-7 came from the first
(demo-cable) capture and 6-7 are what the §4 quiet-tail amendment rests on;
**8-11 came from the first REAL SILICON captures on 2026-07-30 and each one
falsified an assumption the harness had been built on** — read 8 and 9 before
trusting any sample count:
1. **One timestamp per sample, count == IICE depth exactly.** The hardware trace
   arrives *already windowed*, so the offline cropper applies to the **sim side
   only**. This validates the §0 design decision.
2. **No design HIERARCHY in the names** — that is the load-bearing part. There is
   exactly **one** scope level, the instrumented module (`rm_led` here), not the
   flat-with-no-scope I first wrote. Either way, matching is on the leaf, and
   `signal_map.tsv`'s `hw_path` reduces to its leaf name. §1's scope-resolution
   applies to the **sim** side.
3. **Identify injects `identify_sampleclock` and `identify_cycle`** — not manifest
   signals, so they are a named whitelist (`IDENTIFY_INJECTED`). An *unrecognised*
   extra is still exit 2; the check is never widened to "ignore anything
   unexpected". Note **`identify_cycle` carries zero value changes** and is not even
   in `$dumpvars`, so it cannot be kept as a useful provenance column — carrying it
   would drag an all-X column into the diff. Its *presence* is recorded in the
   report instead, which is the only provenance that actually exists.
4. **The time base is arbitrary and non-zero.** Sample index comes from timestamp
   **ordinal**, never absolute time. (This is also what broke `-et 0`; see the plan's V2b.)
5. ⚠ **`identify_sampleclock` and `dut_clk` share one VCD identifier (`!`)** — same
   waveform, two names. Any ident→name map must be ident→**list**, or one of the two
   is silently dropped. Easy to miss: it looks like an ordinary duplicate in the
   `$var` block.
6. **Do NOT pass `-keep_last_time`.** The FSDB's max time (20480) is one sample
   period *beyond* the last actual sample (20470), so the flag appends a trailing
   time tag that is not a sample — a depth-1024 trace then reads as 1025.
   > **AMENDED 2026-07-30 — that max time is load-bearing, and `-summary` is how
   > to get it.** `-keep_last_time` was re-measured and does exactly what this
   > note says: 1025 timestamps, `#10240..#20480`, the last one carrying no
   > events. It is still not passed, because `fsdb2vcd -summary`'s `max xtag`
   > gives the same number **without touching the data path** — no trailing
   > pseudo-sample to strip and no risk of it being counted. `fsdb_time_range()`
   > already read it; `load_hw_trace` now hands it to `sample_grid`, which is what
   > lets a stalled capture be held forward instead of refused (see §4's
   > 2026-07-30 amendment). Note the two end-time conventions in play differ by
   > one period — Identify's `write fsdb` puts `max xtag` one period past the last
   > sample; this harness's `write_vcd` + `vcd2fsdb` puts it *on* the last sample
   > (measured: 63 for a depth-64 trace) — so only `L <= end <= L + period` may
   > ever be assumed.
7. **`identify_sampleclock` changes at EVERY sample — MEASURED 2026-07-30.** All
   1024 of them, counted in Identify's own `write vcd` (`clk` ident: 1024 changes
   over 1024 sample timestamps, none quiet) and again in the `fsdb2vcd` of its
   `write fsdb`. Combined with fact 5's aliasing, this means a genuine Identify
   capture **always** yields exactly `depth` timestamps, however wedged the DUT
   is: it can never present a tail with no value changes. This was previously
   recorded as speculation ("probable mitigation, not yet measured"). It does
   **not** remove the need for the quiet-tail path — every FSDB this harness
   writes itself carries only the manifest signals, so the synthetic, injected and
   `normalise_hw_capture` re-emission paths do reach it. Test:
   `test_identify_sampleclock_changes_at_EVERY_sample_of_the_real_capture`.
   > **CORRECTED 2026-07-30 (later, on silicon) — "exactly `depth` timestamps" is
   > WRONG.** That was measured on the **demo-cable** capture, which happens to
   > emit one timestamp per sample. A capture off the **real board** does not:
   > see facts 8 and 9. The part of fact 7 that survives is the one that
   > mattered — the sample clock never goes quiet, so a wedged DUT still produces
   > a readable capture.
8. **On a REAL BOARD capture the sample clock TOGGLES: two timestamps per
   sample — MEASURED 2026-07-30 on mps3_01.** A 923-sample capture arrived as
   **1846** timestamps, `#2020..#20470` step 10, with the samples on the RISING
   edges (period 20). The demo-cable capture of fact 7 had one tag per sample, so
   the harness's original "one timestamp per sample" assumption was calibrated on
   a shape the board does not produce, and the ordinal loader refused every real
   capture on its `len(ts) > n_samples` guard.
   The fix does **not** relax that guard: `crop_trace.samples_from_sample_clock`
   counts sample-clock **edges**, which is a measurement of the grid rather than
   an inference from it, and `load_hw_trace` selects it only when the ordinal path
   cannot work *and* a clock is present. With no clock to measure, an over-long
   trace is still exit 2. Tests:
   `test_toggling_sample_clock_capture_loads_via_MEASURED_clock_edges`,
   `test_more_timestamps_than_samples_with_NO_clock_is_STILL_a_harness_error`,
   `test_one_tag_per_sample_captures_STILL_use_the_ordinal_loader`.
   Note the first rising edge must be counted: a capture is written out from
   index 0, so its first timestamp IS sample 0 (seeding `prev_clk` to the
   opposite phase — without it 8 samples come back as 7).
9. **A capture can be SHORTER than the instrumented depth — MEASURED.** The
   depth-1024 IICE returned **923** samples and the debugger said so itself:
   `Warning: Range 0 1023 exceeds maximum 922`. So `depth` is the *buffer* size,
   not the delivered sample count, and `write fsdb -range {0 depth-1}` may ask
   for more than exists. The comparison still requires equal lengths (weakening
   that would compare misaligned samples), but the error now names the likely
   cause instead of printing two bare numbers — `_sample_count_triage()`, and it
   explicitly warns against "just truncate the sim side", which misaligns any
   `trigger_time middle|late` window. Test:
   `test_sample_count_mismatch_TRIAGE_names_the_capture_shortfall`.
10. **Signal ORDER and VCD identifiers are NOT stable between captures of the
    same instrumentation — MEASURED.** Two consecutive captures off one
    unchanged bitstream declared the same six signals in different orders, so
    ident `#` was `HADDR` in the first and `HRDATA` in the second (and `(` was
    `CORE_LOCKUP` then `HADDR`). Any positional or ident-order shortcut therefore
    mis-attributes every value — it caught out an ad-hoc decode during the first
    silicon read, briefly reading instruction words as addresses. This is why §1
    matches on **leaf names** and nothing else.
11. **A manifest `hw:` path may have NO scope.** Identify writes a port of the
    synthesis top as bare `/<leaf>` (p.14: "the top-level design unit is
    represented by the initial '/'"), which `signals_nanosoc.yaml` uses for its
    sample clock `/dut_clk`. `write_vcd` used to reject that
    (`signal path '/dut_clk' has no scope`), making `normalise_hw_capture` unable
    to re-emit any manifest whose clock is a top port. Such signals now go in a
    synthetic `iice_top` scope; only the scope is invented, the leaf is untouched.

Two debugger requirements learned the same way:
- `write fsdb`/`write vcd` need an explicit **`-range {0 <depth-1>}`**; without it:
  `Error: End (maximum) value out of range`.
- **The trigger statemachine must be configured in the DEBUGGER before `run`** — the
  `.idc` only *sizes* it (`-triggerconditions`/`-triggerstates`). Without transitions:
  `Error: Trigger statemachine must be configured for iice <name> before running`.
  Minimal working form: `statemachine clear -iice <n> -all` then
  `statemachine addtrans -iice <n> -from 0 -trigger`.
- **`project open` inside a `-f` script kills the rest of the script** in the
  *debugger* shell too, not just the instrumentor. Pass the project as
  `identify_debugger_shell -prj <file> -f <script>` instead. Note `-f` resolves
  relative to the project directory after an internal chdir, so **pass the script
  path absolutely**.

### RESOLVED — the `hw:` probe-path convention

**`/` IS the synthesis top's own scope, and the top module name NEVER appears in
the path.** `identify_debug_env_reference.pdf` p.14: *"the top-level design unit is
represented by the initial `/`"*. Confirmed by two real Identify runs with
`stop_on_signal_not_found 1`, and independently by the captured trace above, whose
probe `/blink_counter` on a top named `rm_led` came back as a flat `blink_counter`.
The `signals add /top/u1/reset_n` example elsewhere in the same manual is
misleading — `top` there is an *instance* name. **Paths are case-sensitive.**

Because `device jtagport soft` *adds* four ports to the synthesis top, the design
has **two tops**: `rp_nanosoc_iice_core` is the Synplify/Identify top and
instantiates the RM wrapper as `u_rm`. So for nanosoc:

| | value |
|---|---|
| sample clock | `/dut_clk` |
| DUT probe prefix | `/u_rm/` |
| e.g. the M0's HADDR | `/u_rm/u_nanosoc/u_ss_cpu/u_cpu_0/HADDR` |

Applied in `signals_nanosoc.yaml`, with regression tests asserting the prefix and
that neither top module name nor `u_core` ever appears in a path.

**Still true and worth keeping in mind:** the *convention* is proven, but these
*specific* nanosoc paths have never been resolved by Identify against the real DUT,
because the RM cannot elaborate today (see the plan's §1b P0 — `nanosoc.sv` dropped
the `cpu_0_swd*` ports the wrapper still connects). `stop_on_signal_not_found 1`
remains the safety net: a stale path is a hard error, never a silently shrunken
probe set.

Note the hw and sim hierarchies are **deliberately different shapes** — the sim
bench instantiates `nanosoc` directly and has no `u_rm` at all. That is why the
manifest carries both paths per signal rather than deriving one from the other by a
slash/dot swap (which the *selftest* manifest can get away with). "Unify these"
looks like an obvious cleanup and would silently break the mapping.
