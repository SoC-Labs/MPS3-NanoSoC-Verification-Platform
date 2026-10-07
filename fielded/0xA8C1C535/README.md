# `0xA8C1C535` — the fielded static shell, preserved

This directory holds the build products of the static shell **`0xA8C1C535`**,
the one `docs/FIELDED_SHELL.md` records as `fielded` (2026-08-10, `e37ded0`) —
the shell the MPS3 board boots from its config SD.

It exists because, until 2026-09-09, **exactly one copy of these files existed
anywhere**, and it was inside a gitignored Vivado scratch tree.

## Why this is not housekeeping

`static_routed_locked.dcp` is the black-boxed, locked routed static. Every
incremental overlay add reads it:

```
make -C fpga/dfx add-rm-<name> BUILD=<tree>
  └─ test -f $(BUILD)/prod/static_routed_locked.dcp || error   (fpga/dfx/Makefile)
     └─ DFX_REUSE_LOCKED=… vivado -source build_dfx.tcl        (incremental mode)
```

Lose that file and **no new overlay can ever be added to the shell that is on
the board.** The only remedy is a full re-mint — which produces a *different*
`static_id`, invalidates all ten overlays currently fielded, and requires a
board re-flash. A `make clean`, a full disk, or a tidied workstation was all it
would have taken. Nothing in the repo said so, because the file was invisible to
`git status`, `git clone` and every gate.

The same is true in weaker form of `config_rm_greybox_routed.dcp` (the DFX
reference config: the `pr_verify` reference and the source of the correct MMI)
and of `shell_static_synth.dcp` (the pre-DFX synthesised static the whole flow
links against).

## The artefacts

Six come from the DFX production dir, two from the shell project. All eight are
listed with checksums in `MANIFEST.md5`.

| file | bytes | md5 | what it is |
|---|---:|---|---|
| `static_routed_locked.dcp` | 9,579,088 | `088fac2695d8407cee4607be9f305a28` | **The one that matters.** Routed static with the RP black-boxed and locked. `static_id` is its CRC-32. Sole input to every future `add-rm-*`. |
| `config_rm_greybox_routed.dcp` | 10,363,096 | `33327b59505a1f12f68bb7ca4246224c` | The greybox reference configuration, routed. `pr_verify` reference for every other RM, and the checkpoint the firmware MMI must come from. |
| `config_rm_greybox.bit` | 11,138,390 | `94853a897c56fa673cb1f70bbcaae2f0` | Full-device greybox configuration bitstream — the `updatemem` base into which the MicroBlaze shell firmware is baked. |
| `config_rm_greybox.mmi` | 74,569 | `87ebf1f2a83f80082d750ab2cafeed0a` | BRAM↔LMB map for that configuration. Regenerable from the routed DCP (`fpga/dfx/tools/write_mem_info.tcl`) — kept because the *wrong* MMI corrupts the firmware embed silently. |
| `static_id.txt` | 11 | `2c86f1548ee46157dae1abadaf0fcdff` | `0xA8C1C535`. The minted id, read (never recomputed) by every incremental add. |
| `overlay_inputs.txt` | 2,786 | `54be381288f53bd19665218f8f3ae143` | The ten `rm_key rm_name rm_id partial clearing` rows `make overlays` consumes. Its paths are absolute into `build_mint/prod` — see "Caveats" and `mint.json` below, which carries them in the portable relative form. |
| `shell_static_synth.dcp` | 5,431,147 | `4e7434a3c45acb12abf8b648506cf2e6` | From `build/shell_proj_mint/`. The synthesised static the DFX flow links against (`STATIC_DCP=`). |
| `shell_harness.xsa` | 1,111,621 | `f53d515f79a33f444032095d84e711a7` | From `build/shell_proj_mint/`. The hardware handoff `xsct create_platform` builds the firmware BSP from. |

`md5sum -c MANIFEST.md5` here, or `bash verify_fielded.sh` for that plus the
`static_id` check.

## `mint.json` — the record of the mint that produced them

`mint.json` here is a **reconstruction**, not a record the run wrote: this mint
predates `fpga/dfx/tools/mint_record.py`, which is stage 7 of
`make -C fpga/dfx mint` from 2026-09-10 onward. It is rebuilt from the three
tracked files in this directory and nothing else:

```bash
python3 fpga/dfx/tools/mint_record.py reconstruct \
        --dir fielded/0xA8C1C535 -o fielded/0xA8C1C535/mint.json
```

`tests/dfx_flow/test_mint_record.py` regenerates it and diffs, so a record that
stops matching its own inputs fails a gate rather than quietly drifting.

Two things it deliberately does **not** contain, because this directory does not
know them and the record never invents a value:

* `sources.*` — the repo, nanoSoC and SoCScope revisions. The four
  external-source RM checkpoints were REUSED (below), and for `rm_socscope` no
  `synth.log` exists in any tree at all.
* `firmware.flags` — what the ELF beside this bitstream was built with.
  `docs/FIELDED_SHELL.md`'s `fielded_fw_flags` row is the authority for that,
  and `mint.json` says so in the `reason` where the value would be.

Both fields are `null` **with the reason they are null**, which is the whole
discipline of the format (`fpga/dfx/tools/MINT_RECORD_SCHEMA.md`). A future mint
fills them in automatically, because the flow that builds the shell is now also
the thing that writes the record.

`overlay_inputs.txt`'s rows here are ABSOLUTE paths into one workstation's home
directory — the defect that made this file unmovable. `build_dfx.tcl` writes
them relative to the build dir from 2026-09-10; `mint.json` stores the relative
form, so the reconstruction is portable even though its source is not.

## Provenance

**Built 2026-08-09 into 2026-08-10, in `fpga/dfx/build_mint/`, by MANUAL STAGES**
— not by one clean run of any script (the mint script of the day,
`qspi_kvm_rekey/rebuild_jtag_uart.sh`, was deleted on 2026-09-10 and replaced by
`make -C fpga/dfx mint`; see `docs/BUILD_AND_MINT.md`). The timestamps show the sequence:
the shell project completed 22:28–22:42 on 08-09, the static was locked at
22:59, the greybox reference routed at 22:58, bitstreams written 00:35 on 08-10,
and `rm_nanosoc_upy` folded in by a separate incremental add that finished 09:26
(`build_mint/prod/add_upy_xip.log` → `DFX_ADD_COMPLETE static_id=0xA8C1C535`).
The `.mmi` was regenerated 2026-09-09 during touch-diagnostic work; it is
reproducible from the routed DCP and its content is not mint-specific.

**The four external-source RM synthesis checkpoints were REUSED, not rebuilt.**
`rm_nanosoc_synth.dcp`, `rm_eth_ss_synth.dcp`, `rm_nanosoc_multicore_synth.dcp`
and `rm_socscope_synth.dcp` in `build_mint/prod/` are **byte-identical** to the
copies in `fpga/dfx/build_qspi_kvm_jtag/prod/` (verified by `cmp`, 2026-09-09).
They were copied across.

That matters for provenance: **the only record of which source trees produced
those RMs is `fpga/dfx/build_qspi_kvm_jtag/rm_*_synth/synth.log`**, in another
gitignored scratch tree. That is why `build_qspi_kvm_jtag` is classified
PROVENANCE, not DEAD, by `scripts/dfx_scratch_report.sh` — it is not a spare
build, it is the only surviving evidence of what went into the fielded RMs.

And it is incomplete: `rm_socscope` has **no `synth.log`** in that tree. Its
`rm_socscope_synth.dcp` was itself copied in from
`fpga/dfx/rms/rm_socscope/build/` (2026-08-06 23:19 → 23:20). For SoCScope the
provenance chain runs out; nothing on disk records the source revision it was
synthesised from.

The RM checkpoints are **not** preserved in this directory. They are large, and
they are re-synthesisable from source — unlike the locked static, which is not
reproducible at all (a rebuild produces a different `static_id` by definition).

## Every place a copy lives

| location | contents | notes |
|---|---|---|
| `fielded/0xA8C1C535/` (here) | all 8 | Tracked: README, `MANIFEST.md5`, the two scripts. The binaries are **gitignored** — see below. |
| `fpga/dfx/build_mint/prod/` | 6 | Local scratch, gitignored (`fpga/dfx/build*/`). The original. |
| `build/shell_proj_mint/` | 2 | Local scratch, gitignored (`build/`). The original. |
| `<hub-host>:<hub-home>/mint_A8C1C535/fielded_dcp/` | 5 | The hub, off this workstation. Protective copy made 2026-09-09. Holds `static_routed_locked.dcp`, `config_rm_greybox_routed.dcp`, `config_rm_greybox.mmi`, `static_id.txt`, `overlay_inputs.txt` — **not** the greybox `.bit`, and **not** the two `shell_proj_mint` files. |

So the irreplaceable file now has three copies on two machines. Before
2026-09-09 it had one, on one.

`fetch_fielded.sh` repopulates this directory from local scratch if it is
present, else over `scp` from the hub, then verifies. It is read-only at both
ends and never writes to the hub.

## Tracking these binaries — an open decision, and it is the owner's

The four small files here are tracked. The ~36 MB of `.dcp`/`.bit`/`.xsa` are
not (`.gitignore`: `fielded/*/*.dcp`, `*.xsa`, `*.bit`). Two ways to change
that:

**git-LFS.** The artefacts travel with the clone, and the thing that closes the
shell forever cannot be lost by losing a machine. `verify_fielded.sh` becomes
enforceable in CI against real content. Costs: LFS must be provisioned on every
remote this repo is pushed to (and this repo is heading for a public GitHub
release, where LFS bandwidth is metered and shared); every future mint adds
another ~36 MB permanently, since LFS objects are not pruned by rewriting
history; and a clone without `git-lfs` installed silently gets pointer files —
a failure mode that looks exactly like success until someone tries an
`add-rm-*`.

**An external store** (the hub, or lab object storage) with only
`MANIFEST.md5` + `fetch_fielded.sh` tracked, as today. The repo stays small and
public-safe; the checksums still prove a recovered copy is the right one.
Costs: the artefacts are only as durable as that store's backups, recovery
needs credentials, and nothing in CI can prove the store is still populated.

**Decision (2026-09-10): the external store, made deliberate.** The hub copy at
`<hub-host>:<hub-home>/mint_A8C1C535/fielded_dcp/` is now complete (8 of 8,
`md5sum -c MANIFEST.md5` clean) and is the canonical off-tree copy; `fetch_fielded.sh`
pulls from it and `verify_fielded.sh` proves what it pulled. Making that copy a mint
stage with a gate is Phase 2 work. The original recommendation follows for the record.

**Recommendation as written: the external store, made deliberate.** Not because LFS is
wrong, but because these artefacts are *per-mint immutable and rarely read* —
the access pattern of an archive, not of source. What is missing today is not
LFS; it is that the hub copy was made by hand, is partial (5 of 8), and no gate
notices if it disappears. Concretely: put the full set on the hub, have the mint
script push it as a final stage, and add a board-free gate that checks
`MANIFEST.md5` against a hub listing. That buys most of LFS's durability without
putting 36 MB per mint into a public repo's object store forever.

**This is a call for the repo owner, not for a gate to make.** Until it is made,
the honest state is the one described above: three copies, two machines, one of
them partial, and `MANIFEST.md5` is what proves any of them is the right file.

## Caveats

- **`overlay_inputs.txt` holds absolute paths** into
  `<repo>/fpga/dfx/build_mint/prod/`. It is
  preserved as a record of the ten RMs and their `rm_id`s; to *use* it on
  another machine the paths need rewriting. It is generated by `build_dfx.tcl`,
  so the fix belongs there, not here.
- **These files are not a bitstream you can flash.** The flashable base is
  `config_rm_greybox_fw.bit` — the greybox `.bit` with the shell firmware baked
  in by `updatemem`. It is a build product of the firmware, is rebuilt whenever
  the firmware changes, and is deliberately not preserved here.
- **Preserving the artefacts does not make the build reproducible.** A rebuild
  from source produces a different `static_id`; that is what a mint *is*.
