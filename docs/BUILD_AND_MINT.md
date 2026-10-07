# Build and mint — the one runbook

How to build the static shell, route every RM against it, re-key the overlays,
bake the firmware, record what happened, and field the result.

This replaces `fpga/dfx/qspi_kvm_rekey/MINT_RUNBOOK.md` and the 516-line
`rebuild_jtag_uart.sh` it fired. Both are deleted: every step they described is
a `make` target now, and the two of them plus a third, `rebuild_irq_subset.sh`,
had already diverged into three different accounts of one procedure — while the
shell fielded at the time was built by **none of them**, by hand, stage by
stage (`fielded/0xA8C1C535/README.md`, "Provenance"). The shell on the board
today, `0x3F1A560F`, is the first minted by one run of the target below
(`fielded/0x3F1A560F/README.md`).

> **A mint is a multi-hour, effect-irreversible job.** Locking a new static
> invalidates every overlay currently on the board and on disk until they are
> re-keyed and re-pushed. Take an exclusive build/board window.

This file is the **procedure**. What a *particular* mint carries — which
static-side changes, which RMs, what has to land before it can be fired, and
what to prove on the board afterwards — is a per-mint plan beside it. The
current one is
**[`docs/planning/MINT_2026_09.md`](planning/MINT_2026_09.md)**.

---

## The whole thing

```bash
make -C fpga/dfx mint \
     BUILD=$PWD/fpga/dfx/build_<name> \
     SHELL_PROJ=$PWD/build/shell_proj_<name> \
     RM_SET="greybox regdemo_a regdemo_b led uart_echo nanosoc eth_ss nanosoc_multicore socscope" \
     SHELL_TOUCH=1 TOUCH=1
```

Eight stages, each a file target with real prerequisites:

| # | stage | in | out |
|---|---|---|---|
| 1 | preflight | `shell_bd.tcl`, `rm_list.tcl`, every RM wrapper | `$BUILD/preflight.stamp` |
| 2 | static shell | `fpga/shell/build_shell.tcl` + the BD | `$SHELL_PROJ/shell_static_synth.dcp` (+ `.xsa`) |
| 3 | RM checkpoints | each RM's `ooc_synth.tcl` | `$BUILD/prod/<rm_key>_synth.dcp` |
| 4 | DFX prod | the shell DCP + those checkpoints | `static_id.txt`, `static_routed_locked.dcp`, partial/clearing pairs |
| 5 | overlays | `static_id.txt` + `overlay_inputs.txt` + `config_rm_greybox.bit` | `overlay/<rm>/manifest.json` (**+ `static_usercode`**), `overlay/mps3_shell_static_id.c` |
| 6 | firmware | `firmware/platform` + the re-keyed greybox manifest | `shell_fw.elf` → `config_rm_greybox_fw.bit` **(the flashable base)** |
| 7 | record | everything above + `static_canon.json` + `static_stamp.json` | `$BUILD/prod/mint.json` |
| 8 | hub copy | the irreplaceable artefacts | whatever `MINT_HUB` names |

Ask before you fire:

```bash
make -n -C fpga/dfx mint ...      # the plan, printed by make itself
make    -C fpga/dfx mint DRYRUN=1 ...   # the same plan, and nothing is created
```

`DRYRUN=1` is a real guard now. The script this replaced documented it as
"prints the plan and launches nothing" while its re-key stage ran **for real** —
`make overlays` writes, so a "dry run" with the default `BUILD` silently re-keyed
every tracked overlay onto a shell that was not on any board, and the payload
half of that damage does not show up in `git status`.

### Two knobs, and they move together

| knob | what it does |
|---|---|
| `SHELL_TOUCH=1` | puts the touch AXI-IIC master in the **fabric**, defines `MPS3_SHELL_TOUCH`, adds the touch XDC |
| `TOUCH=1` | compiles the touch **driver** into the firmware ELF |

A fabric with the IIC and firmware without the driver is a **silent half-mint**:
it pings, reports the right `static_id`, and passes every acceptance gate. That
is exactly what shipped once (`docs/FIELDED_SHELL.md`, "A third fact"). Setting
`SHELL_TOUCH=1 TOUCH=0` now refuses to even produce a plan.

Everything else is either the product configuration or this repo's layout.
`PRODUCT=1` is not a knob you pass: stage 6 always builds the firmware with it,
and `firmware/platform/Makefile` is where that name expands (today: `CLCD=1
CLCD_KVM=1 HWICAP_FIFO=1 WINDOWED=1 TOUCH=1`). `HWICAP_FIFO` and `WINDOWED` must
match the bitstream the same run builds — omitting them produced a shell that
pings, reports the right id, passes every gate, and **cannot load a single RM**.

### A third knob, which selects a DIFFERENT static

`SHELL_REALPHY` became a real mint knob on 2026-09-14. Before that, stage 2
forwarded only `SHELL_TOUCH`, so the real-PHY gate silently took whatever the
ambient shell environment happened to hold — while `fpga/shell/build_shell.tcl`'s
own header already described it as a mint knob that `make -C fpga/dfx mint`
forwards. Stage 2 now forwards it, so that sentence is true.

| knob | default | what it does |
|---|---|---|
| `SHELL_REALPHY=0` | **yes — and this is what is fielded** | the shell's **virtual** PHY drives the DUT's RMII from inside the fabric. No external PHY, no shield pins. |
| `SHELL_REALPHY=1` | | defines `MPS3_SHELL_REALPHY`, exposes **nine** `phy_pad_*` boundary ports on `shell_top`, and adds `fpga/shell/constraints_realphy/{mps3_realphy_pins,mps3_realphy_timing}.xdc` — pinned to the **measured** J28 map |
| `REALPHY_RATE` | `100` (only meaningful with `=1`) | the link rate the variant is built and **constrained** for; `10` or `100` |

Two things to know before you pass it:

* **`=1` is a different static.** Different netlist, different `static_id`,
  different `static_canon` — **no fielded overlay is valid against it**. It is a
  variant build, not a flag on the product shell.
* **`REALPHY_RATE` without `SHELL_REALPHY=1` is a hard error** in
  `build_shell.tcl` — the same silent-half-config guard shape as
  `SHELL_TOUCH`/`TOUCH`. The Makefile therefore forwards `REALPHY_RATE` only
  when you actually set it; leave it alone and ordinary mints are unaffected.

`=0` and *unset* produce **byte-identical block designs** (md5-checked, commit
`31dffb6`), so the knob existing cannot have moved the default bundle. The
recommendation on record is to build `SHELL_REALPHY=1 REALPHY_RATE=100` when a
real-PHY variant is wanted and **not** to build a 10 Mb/s one —
`docs/planning/REALPHY_RATE_DECISION.md` has the costing, and the "level
shifter caps the link at 40 Mbps" premise that used to motivate a 10 Mb/s
variant is **retracted**: the part is a passive `SN74TVC16222A` pass-FET array
and the real fault was a connector.

### The other parameters

| variable | default | meaning |
|---|---|---|
| `BUILD` | `fpga/dfx/build` | the DFX scratch tree (gitignored `fpga/dfx/build*/`) |
| `SHELL_PROJ` | `build/shell_proj` | the shell project dir; its `shell_static_synth.dcp` is what the DFX flow links against |
| `RM_SET` | every RM **except `nanosoc_iice`** | short names or `rm_` keys — `"greybox led"` == `"rm_greybox rm_led"` |
| `REUSE_DCPS_FROM` | *(unset)* | a `prod/` dir whose `<rm>_synth.dcp` files to copy instead of re-synthesising |
| `MINT_HUB` | *(unset)* | rsync destination for stage 8 — **no default host** |
| `DRYRUN` | `0` | print, launch nothing |

`clcd_demo` joined the default `RM_SET` on 2026-09-14; `nanosoc_iice` is
registered but stays out of it, because its checkpoint needs a Synplify/Identify
licence seat that the Makefile cannot take. See
`docs/planning/MINT_2026_09.md` for both, and for the two wrapper defects that
currently block them.

**Keep `BUILD` distinct from any tree you would hate to lose.** The script this
replaced defaulted it to a tree two shells old and re-keyed eight fielded
overlays out from under the board; its own comment warned about that, for a real
run, and nobody noticed it also fired on the dry one.

---

## Before you fire

### The one hard prerequisite: SoC-400 IP provisioning

`rm_nanosoc` and `rm_nanosoc_multicore` pull in the **confidential Arm SoC-400
CoreSight DAP RTL** (`nanosoc_dap_ahb_xlate.v`, `nanosoc_swj_dap_ss.v`,
`cxdapswjdp*.v`, …). That RTL is **not vendored here** and is **not present on a
plain dev server**: the nanoSoC source's
`nanosoc_arch_tech/rtl/coresight_soc400_tech/rtl/` must contain it.

**The mint must therefore run on a host whose nanoSoC checkout has it.** Point
`SOCLABS_NANOSOC_SOC_DIR` at that checkout (in `tools.env` — see below). Do
**not** copy the SoC-400 RTL into this repo; it is read-only Academic-Access IP.

A second non-vendored source: `rm_socscope` synthesises from `$SOCSCOPE_HOME`.
Stage 3 reuses a pre-produced `fpga/dfx/rms/rm_socscope/build/rm_socscope_synth.dcp`
if one is present — that file is gitignored, so it is **absent on a fresh
checkout**. If neither it nor `SOCSCOPE_HOME` is available, drop `socscope` from
`RM_SET`; it then stays keyed to the old shell, which is a known, isolated
staleness and not a surprise.

### `tools.env`

Copy `tools.env.example` to `tools.env` (untracked) and fill in the source trees
and tool paths for **your** host. Those variables have no defaults, deliberately:
a default naming one workstation's home directory is invisible on that machine
and silently wrong on every other one. Stage 3 refuses by name, in seconds, if
one it needs is unset.

### Preflight, by hand (seconds)

Stage 1 runs the first two of these for you; the rest are worth doing before you
commit a build window.

```bash
source set_env.sh                                     # tools on PATH
python3 scripts/harness_gates/check_bd_config_lint.py # BD CONFIG lint
make -C fpga/dfx pin-check                            # RM boundary conformance

# de-risk the biggest failure point BEFORE the 4 h flow
make -C fpga/dfx rm-nanosoc-dcp BUILD=$PWD/fpga/dfx/build_<name>

# the SoC-400-provisioned checkout really has the DAP RTL
ls "$SOCLABS_NANOSOC_SOC_DIR/nanosoc_arch_tech/rtl/coresight_soc400_tech/rtl/nanosoc_dap_ahb_xlate.v"
```

And confirm you can hold and re-flash the target board: the re-key strands every
overlay on it.

---

## Why ONE rebuild, not two

`static_id` is the CRC-32 of `static_routed_locked.dcp`, so **any** edit to the
static netlist — the BD, `shell_top.sv`, the XDC, the decoupler — re-mints it and
strands every overlay until they are re-keyed. Two static changes landed
separately therefore cost two multi-hour rebuilds and two re-key passes for no
benefit. **Batch every pending static edit into one fire.**

The corollary: if you only need to ADD an RM to a shell that already exists, do
**not** mint.

```bash
make -C fpga/dfx add-rm-<name> BUILD=$PWD/fpga/dfx/build_<the locked tree>
make -C fpga/dfx overlays verify BUILD=$PWD/fpga/dfx/build_<the locked tree>
```

That reuses `static_routed_locked.dcp`, **reads** `static_id` rather than
recomputing it, and leaves every fielded overlay valid. It is how `nanosoc_upy`
joined the current shell.

---

## Resuming, and what makes that possible

Every stage is a file target, so a run that dies at hour three and is restarted
repeats only what is missing or stale. Nothing is a "step 4 of 8" counter in a
shell script that has to be re-run from the top or hand-finished — which is how
the fielded mint came to have no script-written record at all.

Two consequences worth knowing:

* Changing `SHELL_TOUCH`/`TOUCH` rebuilds the firmware, because the flag set is
  recorded in `$BUILD/fw_flags.stamp` and a changed flag is a changed
  dependency. `make` keys off timestamps and cannot see a flag: without that
  stamp, `main.o` stays compiled without `-DMPS3_HAS_CLCD`, `--gc-sections`
  strips the freshly built `clcd.o` straight back out, and you get a
  byte-identical ELF with the drivers silently absent. (Observed.)
* If stage 4's outputs are up to date but one of its co-products is missing, the
  build **stops** rather than re-running `prod`. Re-running `prod` re-mints
  `static_id`; recover the pairs with `fpga/dfx/tools/rekey_recover.sh` instead.

---

## The mint record

Stage 7 writes `$BUILD/prod/mint.json`: `static_id`, the source revisions (this
repo, the nanoSoC tree, SoCScope), every checkpoint's md5, the firmware flag set
as data, which RM checkpoints were *reused* rather than rebuilt, and the overlay
rows with paths **relative to the build dir**. Schema and rationale:
`fpga/dfx/tools/MINT_RECORD_SCHEMA.md`; an illustration:
`fpga/dfx/mint.json.example`.

Anything the run could not establish is `null` **with the reason**. Never a
guess: `fielded/0xA8C1C535/mint.json` is a reconstruction of the 2026-08 mint and
its `sources.socscope` is null, because no `synth.log` for that RM exists in any
tree and the provenance chain genuinely runs out there.

Since schema `1.1` it also carries `static_canon` and `static_usercode` (below)
— a mint record from before this Wave is schema `1.0` and has neither key;
`mint_record.py verify` holds each record to the key set of the schema version
it declares, so an older record on the hub does not start failing because the
tool moved on. `cmd_reconstruct` (step 2 below) deliberately still emits
schema `1.0` — see `docs/VERSIONING_PLAN.md` §8.3 for why.

---

## Two more identities: `static_canon` and `static_usercode`

`static_id` answers "will this partial **fit** this fabric" and is a CRC-32 of
a **timestamped** `.dcp` — so it cannot answer "did the sources actually
change" (a no-op rebuild mints a new one) or "which **implementation run** is
this bitstream" (two P&R runs of the identical design share one `static_id`
and have incompatible routing — that destroyed a board's configuration twice
on 2026-07-24). Two more identities close those gaps. Neither replaces
`static_id`, neither is baked into anything on the board, and both are
**board-free — no Vivado, no board, no network**:

```bash
# static_canon -- SHA-256 over the SOURCES that decide the static.
# Same inputs, any number of rebuilds -> the SAME digest; static_id cannot say that.
make -C fpga/dfx canon-check          # the input declaration itself is sound (seconds)
make -C fpga/dfx canon VIVADO_VER=2024.1 SHELL_TOUCH=1   # -> $BUILD/prod/static_canon.json
```

`canon` is also a real prerequisite of stage 7 (`mint.json`), so a normal
`make mint` run produces it automatically. `SHELL_REALPHY` is recorded here and,
since 2026-09-14, is also **forwarded into stage 2** — so the value in the hash
is the value the shell was actually built with, not the ambient one the recorded
flag used to be guessing at. What moves the digest: any declared source's content, the part,
the Vivado version, `SHELL_TOUCH`/`SHELL_REALPHY`. What does **not**: file
timestamps, which run built it, the linked `shell_static_synth.dcp` (recorded
*beside* the digest, in `static_canon.json`'s `linked_static_dcp`, never
hashed *into* it), and firmware C (baked in by `updatemem` *after*
place-and-route — that axis is `HARNESS_VER32`/`USR_ACCESS`, immediately
below).

```bash
# static_usercode -- binds every overlay to the BITSTREAM BUILD it was routed
# against. Runs automatically inside mint stage 5; the standalone form is for
# fixing up a build directory by hand (docs/VERSIONING_PLAN.md §8.2).
make -C fpga/dfx overlay-usercode BUILD=$PWD/fpga/dfx/build_<name>
make -C fpga/dfx overlay-usercode-check BUILD=$PWD/fpga/dfx/build_<name>   # compare only, write nothing
```

Full detail, and the two-derivations-must-agree cross-check, in
`docs/VERSIONING_PLAN.md` §8.1–§8.3.

### The ver32-vs-USR_ACCESS skew check — the exact command, and reading it

The `version` verb (`:6900`) now also answers "does the FIRMWARE running on
this board agree with the BITSTREAM it is baked into" — the check for the
"flashable base whose `updatemem` was never re-run" hazard:

```bash
echo '{"op":"version"}' | nc <board-ip> 6900
```

Three answers, and the first is not a pass:

| Reply (relevant keys) | Meaning | Operator action |
|---|---|---|
| `"usr_access":null,"skew":null` | **NOT CHECKED** — the fabric value could not be read. This is the answer **every shell built before `docs/VERSIONING_PLAN.md` §3.4's fabric-readback register lands** gives, always — `coordinator_handle_version()` has no register to read yet. | Expected today; do not read it as "fine". Cross-check by hand instead: compare this `ver32` against `static_stamp.json`'s `usr_access` for the `.bit` that is actually on the SD card. |
| `"usr_access":"0x01000001","skew":false` | **PASS** (once §3.4 lands) — the image and the bitstream it is baked into report the same `HARNESS_VER32`. | None. |
| `"usr_access":"0x01000000","skew":true` | **FAIL** — the `.bit` and the firmware image inside it are from *different builds*. Nothing else this board reports should be trusted at face value until this is fixed. | Re-bake `config_rm_greybox_fw.bit` from the **current** firmware (stage 6) against the SAME static, re-flash, power-cycle, re-check. |

`host/pyverify/pyverify/client.py`'s `ShellClient.version()` decodes the same
three states into `VersionResponse.skew_verdict` (`"unchecked"` / `"ok"` /
`"SKEW"`) for anything scripting this rather than reading raw JSON by eye.

---

## After the mint

**1. Verify the new identity.**

```bash
cat "$BUILD/prod/static_id.txt"     # the NEW static_id -- must differ from the old
grep -o '0x[0-9A-Fa-f]\{8\}' fpga/dfx/overlay/mps3_shell_static_id.c | head -1
make check                          # overlay lockstep: EVERY overlay on the new id
```

A residual stale overlay is a TODO, not "expected". `nanosoc_upy` stayed keyed to
a dead shell across two mints because the gate that would have caught it never
ran.

**2. PRESERVE the locked static — before anything else.**

`$BUILD/prod/static_routed_locked.dcp` is the only input from which a new overlay
can ever be added to this shell, and it lives in a gitignored scratch tree. Lose
it and the only remedy is a full re-mint, which invalidates every overlay
already fielded.

```bash
mkdir -p fielded/<NEW_STATIC_ID>          # use fielded/0x3F1A560F/ as the template
# copy the artefacts, write MANIFEST.md5, and copy $BUILD/prod/mint.json beside
# them -- stage 7 writes a REAL record now. `reconstruct` is only for a mint that
# never had one: it reads three files and emits schema 1.0 with every provenance
# slot null, so running it OVER a stage-7 record throws that record away.
```

Set `MINT_HUB` and stage 8 makes the off-machine copy for you, every time,
instead of when someone remembers.

**3. Commit the mint result.** Tracked: the re-keyed `overlay/<rm>/manifest.json`,
`overlay/mps3_shell_static_id.c`, `firmware/platform/generated/greybox_blob.c`,
and the new `fielded/<id>/` metadata. The large `*.bin` payloads stay gitignored.

**4. Field it (board-side, irreversible).**

```bash
scripts/mps3_sd_update.sh "$BUILD/prod/config_rm_greybox_fw.bit"
# the SD write "times out" -- EXPECTED, ~5 min over USB-MSC. NEVER retry mid-write.
# then POWER-CYCLE the board so the MCC reloads from SD (a reset is a no-op).
```

**5. Board smoke** (board-safe; no flash-writing swaps — D16 embargo):

* `echo '{"op":"ping"}' | nc <board-ip> 6900` → `shell_id` matches step 1;
* `xsdb scripts/harness_gates/tier3_csr_liveness.tcl` — proves the CSR decode is
  alive before you trust any other readback;
* a swap **and swap-away** with an exact `rm_id` assertion:
  `scripts/harness_gates/swap_check.py --rm <rm> --expect-rm-id <id>`;
* touch: `CHIP_ID` reads `0x0811` and 0 bus errors. Do **not** gate the mint on a
  cursor tracking a press — the STMPE811 is healthy but `TSC_STA` never asserts
  under real contact, and that fault is in the panel's **analog** path
  (`docs/STATUS.md`, known gaps);
* DUT-IP row shows the sniffed DUT IP and `RM_STATUS[2]` toggles on DUT eth-RX.

**6. Update `docs/FIELDED_SHELL.md`** — `fielded`, `fielded_on`, `fielded_by`,
`fielded_fw_flags`, `lmb_kb` — **only after watching the board report the new
id**. `ping.shell_id` or the CLCD status line is the evidence. An `sd_install`
timeout is not evidence of anything.

## Acceptance gate

- [ ] `make -C fpga/dfx verify` — every overlay's `static_id` is the new value.
- [ ] `pr_verify` clean for every RM (stage 4 fails if it is not).
- [ ] The pusher **accepts** a freshly-keyed overlay and **rejects** a stale one.
- [ ] `make check` green end to end on the rebuilt tree.
- [ ] `$BUILD/prod/mint.json` exists and `mint_record.py verify` passes on it.
- [ ] `docs/FIELDED_SHELL.md` updated — and it is the only file that names the
      fielded id in the present tense (`check_fielded_shell_claims.py` enforces).

## Rollback

The Vivado work is all under the gitignored `$BUILD`. A failed run leaves the
tracked overlays untouched until stage 5, which only runs after stage 4 succeeds.
To abandon a completed-but-unwanted mint before fielding:
`git checkout -- fpga/dfx/overlay firmware/platform/generated` and delete
`$BUILD`. Once fielded, rollback means re-flashing the previous shell — which is
possible only if you preserved it (step 2).
