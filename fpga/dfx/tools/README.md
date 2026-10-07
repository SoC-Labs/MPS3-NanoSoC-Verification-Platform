# `fpga/dfx/tools/` — the mint helpers, tracked

What the mint flow **executes or cites as its authority**. Five of these existed
until 2026-09-09 only inside `fpga/dfx/build_clcd/` — a *gitignored* Vivado
scratch tree (`.gitignore`: `fpga/dfx/build*/`).

That is the same failure shape as the fielded static living only in scratch: a
`git clone` of this repo produced a tree in which the runbook's mint script
could not run past its firmware stage, because the Tcl it sourced was never in
the repo. Nothing said so. The script simply referenced a path that happened to
exist on one workstation.

The `build_clcd/` originals are **left in place, untouched**. These are copies,
with paths parametrized (below); nothing was deleted.

**Where the mint lives now.** `fpga/dfx/qspi_kvm_rekey/{MINT_RUNBOOK.md,
rebuild_jtag_uart.sh}` were deleted on 2026-09-10, once every step they
performed had a `make` target: `make -C fpga/dfx mint`, documented in
`docs/BUILD_AND_MINT.md`. The references below say which stage of that flow uses
each helper. Two of them — `finish_rekey.sh` and `rekey_recover.sh` — are the
RECOVERY paths and are not part of the normal flow.

## What each one does, and who depends on it

| file | what it does | depended on by |
|---|---|---|
| `write_mem_info.tcl` | Opens an implementation — the greybox **routed** DCP, or a shell project run — and `write_mem_info -force` → the `.mmi` (BRAM↔LMB map) `updatemem` needs to patch the MicroBlaze program into a bitstream. | **`make mint` stage 6** (`$(PROD)/config_rm_greybox.mmi`) — executed, live. Also `finish_rekey.sh` Phase 1C. |
| `finish_rekey.sh` | Detached daemon that **completes a re-key already in flight**: waits for `prod`+`add`+`overlays`, then firmware(`CLCD=1`)+`updatemem`, then a 6-gate acceptance check, then one sentinel. | The PROVEN recipe `make mint` stage 6 implements. Recovery path; not executed by the flow. |
| `rekey_recover.sh` | Recovers a `make prod` that **died part-way**, reusing the already-locked static so `static_id` does **not** change and fielded overlays stay valid. Drives `finish_partials.tcl` then an incremental `build_dfx.tcl` add. | Operators, after a lost build window. Chains into `finish_rekey.sh`. |
| `finish_partials.tcl` | Writes the partial+clearing bitstream pairs (and the reference RM's FULL config bit) for RMs **already routed**, without re-routing and without re-locking — then seeds `overlay_inputs.txt` so a later incremental add preserves those rows. | `rekey_recover.sh` step 1. |
| `overlay_inputs.tcl` | The one rule for how a row of `overlay_inputs.txt` names a bitstream: **relative to the build dir**, so the hand-off record travels with the artefacts it lists. | `build_dfx.tcl` (both the full build and the incremental add) and `finish_partials.tcl`. Unit-tested under plain `tclsh`. |
| `mint_record.py` | Writes / reconstructs / checks `mint.json` — the machine-readable record of one mint. Schema: `MINT_RECORD_SCHEMA.md`. | **`make mint` stage 7**, and `fielded/<id>/mint.json`. |
| `bank_gate.tcl` | Lists every port in the CLCD I/O bank with its `IOSTANDARD`, so a gate can assert none is non-1.8 V. (`iobank` has no VCCO property in 2024.1; per-port `IOSTANDARD` is the durable proof.) | `finish_rekey.sh` gate G2. |
| `vivado_guard.py` | ONE Vivado per build dir: reads each checkpoint's writer from its `dcp.xml` and refuses a mix; refuses `SHELL_CPU=mbv` under Vivado < 2026.1. Silent when clean. | **the Makefile, at parse time**, before any Vivado goal (FLOW, 2026-09-23). |
| `vivado_version.tcl` | The Tcl twin: refuses a pre-built/locked checkpoint another Vivado wrote (the July 2026 "VERSION TRAP"). tclsh-testable. | `build_dfx.tcl` (static shell dcp, every pre-built `<rm>_synth.dcp`, incremental inputs). |
| `stage0_bake.py` | The guards around `mint-stage0`: the stage0 ELF fits the MBV LMB map and was compiled FOR this static; the `-proc` is the MBV and never the DDR MIG's MCS; the baked base keeps UserID/USR_ACCESS/length; writes `stage0_bake.json`. | **`make mint` stage 6 under `SHELL_CPU=mbv`**; `linux-image-env`; `fielded-files` (mbv). |
| `linux_bundle.py` | Packs the Linux release bundle (`linux_bundle.json`: the `mcc_sd` + `ethernet` targets, FLOW_CONTRACT.md §0), wrapping IMAGE's FW_PAYLOAD with STAGE0's `stage0_pack.py`; then runs the gate on it. | `make mint-linux-image`, `linux-bundle-check`. |
| `ooc_early_warning.sh` + `ooc_inline.tcl` | Which RMs a new Vivado breaks: OOC-synthesises every RM through the mint's own recipes into a scratch dir; `--list` is the dry list. Refuses to run before `MINT COMPLETE`. | Operators / the lead (plan §6 risk 1). Never part of a mint. |
| `ltx_sidecar.py static` + `debug_probes.tcl write_static_debug_probes` | The STATIC's probes file, `config_<ref>_static.ltx`: it exists iff the static holds debug cores. mb: none allowed; mbv: SEAM-8 hub + DDR4 slave. The sidecar binds it to the static `.bit` / UserID. | **`make mint` stage 4** (and `prod`), both CPUs; `mint-stage0` binds it to the flashable base. |
| `tests/test_flow_linux.py` | The FLOW lane's gates, each with its negative control (pytest, board-free). | `python3 -m pytest fpga/dfx/tools/tests` |
| `dfx_jobs.py` + `dfx_jobs.tcl` | `DFX_JOBS=N`: stage 4's RM configs as N concurrent Vivados once the static is locked (ref phase → workers = the incremental add + `DFX_ROW_FILE` → finish). Failed configs are isolated and named; no `static_id.txt` until all pass; a re-run resumes. `--adopt-locked` recovers a one-process stage 4 that died after the lock. | **`make mint` stage 4 / `make prod` with `DFX_JOBS>=2`** (the Makefile swaps only the tool in front of the Vivado args). Unset/1: not run. `FLOW_CONTRACT.md` §8. |
| `tests/test_dfx_jobs.py` + `tests/fake_vivado.py` | DFX_JOBS: `make -n` identity, parallel == one-process artefacts, failure isolation + resume, under a fake Vivado that runs the REAL `build_dfx.tcl` in tclsh. | `python3 -m pytest fpga/dfx/tools/tests` |

## Why `write_mem_info.tcl` was duplicated, and no longer is

The mint script used to carry **two** copies of this step: one written into the
log dir from a heredoc at run time, one sourced out of gitignored scratch. Two
copies of one recipe, and the more visible of the two was the one that does not
matter — the heredoc was invisible to `grep` because it only existed while the
script ran.

Every call site now sources this file. It takes either form:

    <in>.dcp → open_checkpoint   (STAGE 6: the DFX greybox routed config)
    <in>.xpr → open_project + open_run (STAGE 3b: the shell project)

**Which one you pass is the whole game.** The `updatemem` that produces the
flashable base needs the MMI from the *DFX greybox* implementation; a
shell-project MMI is a *different* implementation with different BRAM
placements, and feeding it to `updatemem` corrupts the embed while still exiting
0. A shell-only `.mmi`/`.bit` pair is for debugging the shell on its own and is
consumed by nothing downstream.

## What was changed from the `build_clcd/` originals

Paths, and one guard. **No recipe step was altered.**

- **`REPO` / `DDIR` / `PROD` / `SHELL_PROJ` / `WS` / `MC_SRC` …** were absolute
  literals under one operator's home directory. They are now derived from the
  script's own location (`tools/` → `../../..`) or read from an environment
  variable **whose default is the original value**, so re-running reproduces the
  recorded run and re-targeting needs no edit.
- **`finish_rekey.sh`'s `SCROOT`** pointed at one long-deleted agent scratchpad.
  It is now opt-in: unset, that early-failure probe is skipped. The durable
  success test (build tree *and* repo agree on the id) was always the real one.
- **`rekey_recover.sh`'s two `0xE4B1C44A` literals.** That was the fielded
  static in July 2026; `docs/FIELDED_SHELL.md` is the authority for what is
  fielded now. A hardcoded id made the guard a landmine in both directions — it
  aborts on a *correct* `static_id` in any later tree, and it would have waved
  through a drifted tree that happened to match. The guard now reads the id from
  the tree's own `static_id.txt`, accepts an optional `EXPECT_STATIC_ID=` pin,
  and — this is the part that was missing — asserts at the **end** that the id
  did **not** change, which is the entire point of a recovery. A recovery that
  re-minted is a failed recovery no matter how green its log is.
- **`finish_partials.tcl`'s RM list** was hardcoded to the six CLCD-era base
  RMs. It is now the optional 4th `-tclargs`, defaulting to that same six.
- **`bank_gate.tcl`'s bank number** was derived from a hardcoded package pin
  `AP14`. Still the default; now an optional 2nd argument, because the bank
  number is a device fact and the pin is a board fact.

## Running them

`write_mem_info.tcl`, `finish_partials.tcl` and `bank_gate.tcl` are plain
`vivado -mode batch -source <file> -tclargs …`; each file's header carries its
usage line. `finish_rekey.sh` and `rekey_recover.sh` are **detached daemons** —
launch under `setsid`, watch `$DDIR/*.console`. Neither touches a board; both
run Vivado for hours.
