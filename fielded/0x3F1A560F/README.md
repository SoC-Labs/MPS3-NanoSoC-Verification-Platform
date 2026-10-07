# `0x3F1A560F` — the fielded static shell, preserved

This directory holds the build products of the static shell **`0x3F1A560F`**,
the one `docs/FIELDED_SHELL.md` records as `fielded` (2026-09-22, `54a48b4`) —
the shell the MPS3 board boots from its config SD. It supersedes
[`../0xA8C1C535/`](../0xA8C1C535/), which is kept, unrewritten, as the archive
of the shell fielded from 2026-08-10 to 2026-09-22.

It follows that directory's shape deliberately: same two scripts, same
`MANIFEST.md5` discipline, same argument for why an archive of gitignored
binaries is not housekeeping. Three things are genuinely different and each is
called out below — the mint was **one scripted run** rather than manual stages,
**nothing was reused** from an older tree, and the **flashable image itself** is
preserved here because it is not reproducible from any commit.

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
`static_id`, invalidates all eleven overlays currently fielded, and requires a
board re-flash. A `make clean`, a full disk, or a tidied workstation is all it
would take, because the file is invisible to `git status`, `git clone` and every
gate.

The same is true in weaker form of `config_rm_greybox_routed.dcp` (the DFX
reference config: the `pr_verify` reference and the source of the correct MMI)
and of `shell_static_synth.dcp` (the pre-DFX synthesised static the whole flow
links against).

## The identities this shell carries

| fact | value | where it comes from |
|---|---|---|
| `static_id` | `0x3F1A560F` | CRC-32 of `static_routed_locked.dcp`; `static_id.txt` |
| `usercode` (USERID) | `0xD46FCDCB` | `config_rm_greybox.bit` header, agreeing with `static_stamp.json` |
| `USR_ACCESS` | `0x01000000` | `static_stamp.json`; carries `HARNESS_VER32` |
| harness version | `1.0.0` | `static_stamp.json` |
| overlays | **11** | `overlay_inputs.txt`; ten under `0xA8C1C535`, plus `clcd_demo` |
| firmware flags | `PRODUCT=1` = `CLCD=1 CLCD_KVM=1 HWICAP_FIFO=1 WINDOWED=1 TOUCH=1 DUT_EGRESS=1` | `fw_flags.stamp`, `mint.json` `firmware.flags` |
| fabric flags | `SHELL_TOUCH=1 SHELL_REALPHY=0` | `mint.json` `static_canon.flags` |
| Vivado | `2024.1` | `mint.json` `tools.vivado` |

`USERID` discriminates **implementations** (what an overlay is bound to);
`USR_ACCESS` discriminates firmware **releases** (the ver32 skew check). They
answer different questions and `fpga/dfx/tools/static_stamp.tcl` is the file to
read before trusting either.

## The artefacts

Eight come from the DFX production dir, two from the shell project. All ten are
listed with checksums in `MANIFEST.md5`.

| file | bytes | md5 | what it is |
|---|---:|---|---|
| `static_routed_locked.dcp` | 10,027,747 | `b06db74662e648c5e5515498634c0a9c` | **The one that matters.** Routed static with the RP black-boxed and locked. `static_id` is its CRC-32. Sole input to every future `add-rm-*`. |
| `config_rm_greybox_routed.dcp` | 10,812,075 | `995a9bc682b10080b25204d123aa0402` | The greybox reference configuration, routed. `pr_verify` reference for every other RM, and the checkpoint the firmware MMI must come from. |
| `config_rm_greybox.bit` | 11,123,518 | `aa36ad9978cfb834aacfd67a88cf58f7` | Full-device greybox configuration bitstream — the `updatemem` base into which the MicroBlaze shell firmware is baked. Its header carries `USERID=D46FCDCB`. |
| `config_rm_greybox_fw.bit` | 12,041,006 | `bf39fabcfacd564f2b28f796a58de3e0` | **The flashable base — the image actually on the config SD.** `config_rm_greybox.bit` with `shell_fw.elf` baked in by `updatemem`. Preserved here, unlike `0xA8C1C535`'s: see "Why the firmware image is preserved this time". |
| `config_rm_greybox.mmi` | 74,675 | `6c6c9761d6168aa6f9cfcb1934aaa4c3` | BRAM↔LMB map for that configuration. Regenerable from the routed DCP (`fpga/dfx/tools/write_mem_info.tcl`) — kept because the *wrong* MMI corrupts the firmware embed silently. |
| `static_id.txt` | 11 | `39ad28c517844d9901b11e6e52247dbf` | `0x3F1A560F`. The minted id, read (never recomputed) by every incremental add. |
| `overlay_inputs.txt` | 1,522 | `de6d498d72e44593c9a676728eea009e` | The **eleven** `rm_key rm_name rm_id partial clearing` rows `make overlays` consumes. Rows are RELATIVE to the build dir — the portable form `0xA8C1C535`'s could not manage. |
| `static_stamp.json` | 507 | `16a3492be8c56f00b3789e60f3b5bc82` | The record of the two config-register stamps in the boot image: `usercode`, `usr_access`, `harness_version`. `0xA8C1C535` predates this file. |
| `shell_static_synth.dcp` | 5,671,453 | `d13c73346bdd594866040e93c2c00f05` | From `fpga/dfx/build_mint_2026_09/shell_proj/`. The synthesised static the DFX flow links against (`STATIC_DCP=`). |
| `shell_harness.xsa` | 1,137,649 | `424b487905844791c1b0838ce11f2a48` | From the same tree. The hardware handoff `xsct create_platform` builds the firmware BSP from. |

`md5sum -c MANIFEST.md5` here, or `bash verify_fielded.sh` for that plus the
`static_id` check against `docs/FIELDED_SHELL.md`.

### Why the firmware image is preserved this time

`0xA8C1C535`'s README argued that `config_rm_greybox_fw.bit` is "a build product
of the firmware, rebuilt whenever the firmware changes", and left it out. That
argument does not hold for this image. The firmware in it was baked from
`cb31b0f2` **with the `DUT_EGRESS` flag change still uncommitted** — the binary
reports `cb31b0f2` dirty, and `docs/FIELDED_SHELL.md` records `54a48b4`, the
commit that later carried that flag. Neither sha rebuilds these bytes. A
bit-for-bit reproduction of what is on the board is therefore impossible from
source, so the bytes themselves are the only record, and they get a checksum.

`shell_fw.elf` (815,456 B, md5 `852477c796f9e10fc1be980af5d13d53`) is **not**
preserved here: `.gitignore` covers `fielded/*/*.{dcp,xsa,bit}` and not `.elf`,
so a copy would be committed rather than archived. `mint.json` carries its
checksum, which is what the record needs.

## `mint.json` — a RECORD this time, not a reconstruction

`0xA8C1C535`'s `mint.json` had to be *reconstructed* from file timestamps,
because that mint predates `fpga/dfx/tools/mint_record.py`. This one is the real
thing: schema `1.1`, `record_kind: "mint"`, written by stage 7 of the run that
produced the artefacts, on 2026-09-16.

It answers the questions the reconstruction could not:

* **`sources`** — `repo` `cb31b0f2` (dirty), `nanosoc_m0_soc` `c48693ab`
  (dirty), `socscope` `2edd05a2` (clean). Two dirty trees is not a clean
  provenance chain and the record says so rather than rounding it up.
* **`reused_dcps`** — `null`, with the reason: *no RM checkpoint was reused;
  every RM in `rm_set` was synthesised by this run.* Under `0xA8C1C535` four
  checkpoints were copied in from another gitignored tree and the only record of
  what produced them was that tree's `synth.log` — for `rm_socscope`, not even
  that. This run closes both gaps; `rm_socscope_provenance.json` sits beside the
  checkpoint.
* **`firmware`** — the flag set as data, plus the ELF's checksum.
* **`static_canon`** — SHA-256 `207373d16ca50a72…` over 40 declared source
  files, with `SHELL_TOUCH=1 SHELL_REALPHY=0`.
* **`static_usercode`** — `0xD46FCDCB` / `USR_ACCESS 0x01000000` / harness
  `1.0.0`, read from the bitstream header and cross-checked against
  `static_stamp.json`.

Schema and rationale: `fpga/dfx/tools/MINT_RECORD_SCHEMA.md`. `mint_record.py
verify fielded/0x3F1A560F/mint.json` checks it.

Note that this record is **not** regenerable by `mint_record.py reconstruct`,
and must not be replaced by one: `reconstruct` reads only `static_id.txt`,
`overlay_inputs.txt` and `MANIFEST.md5`, and emits schema `1.0` with every
provenance slot null. Overwriting a real record with a reconstruction would
throw away exactly the fields this mint was the first to record.

## Provenance

**Built 2026-09-15 by ONE command**, not by hand:

```bash
make -C fpga/dfx mint \
     BUILD=fpga/dfx/build_mint_2026_09 \
     SHELL_PROJ=fpga/dfx/build_mint_2026_09/shell_proj \
     SHELL_TOUCH=1 TOUCH=1 \
     MINT_HUB=<set in tools.env>
```

That is the difference from `0xA8C1C535`, which was assembled stage by stage by
an operator after a 516-line script failed at hour three and stopped being the
record (`../0xA8C1C535/README.md`, "Provenance"; `docs/BUILD_AND_MINT.md`).

The run, in local time (the file timestamps and `mint.json` agree; `mint.json`
is UTC, this workstation was UTC+1):

| when | stage |
|---|---|
| 09-15 14:38 | preflight + `static_canon` over the 40 declared sources |
| 09-15 14:50 → 15:10 | static shell: synth → `shell_static_synth.dcp`, impl → `shell_harness.xsa` |
| 09-15 15:46 → 16:39 | six RM OOC checkpoints, all synthesised fresh |
| 09-15 **18:13** | **greybox routed, static black-boxed and locked → `static_id` 0x3F1A560F born** |
| 09-15 18:25 → 20:24 | the other ten configurations routed |
| 09-15 20:24 → 20:31 | `pr_verify` per RM |
| 09-15 20:33 → 20:43 | eleven partial/clearing pairs written |
| 09-15 20:44 → 20:52 | `overlay_inputs.txt`, `static_stamp.json`, `config_rm_greybox.mmi` |
| 09-16 10:21 → 10:32 | firmware `PRODUCT=1` → `shell_fw.elf`, `updatemem` → `config_rm_greybox_fw.bit` |
| 09-16 10:33 | stage 7 → `mint.json` |

**Written to the SD on 2026-09-16. FIELDED on 2026-09-22.** The six-day gap is
not an error and is worth keeping in view: the card holds the image, the MCC
only loads it at power-on, and nothing in between makes that visible. The board
ran `0xA8C1C535` for those six days. "Written" is not "fielded", and only the
board can say which — `docs/FIELDED_SHELL.md` states the rule.

The RM checkpoints are **not** preserved in this directory. They are large, and
they are re-synthesisable from source — unlike the locked static, which is not
reproducible at all (a rebuild produces a different `static_id`; that is what a
mint *is*).

## Every place a copy lives

| location | contents | notes |
|---|---|---|
| `fielded/0x3F1A560F/` (here) | all 10 | Tracked: this README, `MANIFEST.md5`, `mint.json`, `static_id.txt`, `overlay_inputs.txt`, `static_stamp.json`, the two scripts. The `.dcp`/`.bit`/`.xsa` are **gitignored**. |
| `fpga/dfx/build_mint_2026_09/prod/` | 8 | Local scratch, gitignored (`fpga/dfx/build*/`). The original. |
| `fpga/dfx/build_mint_2026_09/shell_proj/` | 2 | Same tree — this mint kept the shell project inside the build dir. |
| `<hub-host>:<hub-home>/mints/` | **verified 2026-09-22** | The hub copy, confirmed by listing it: `static_id.txt` reads `0x3F1A560F` and `static_routed_locked.dcp` is present at 10,027,747 bytes, alongside the routed greybox checkpoint, the mmi, the flashable base, `mint.json` and `overlay_inputs.txt`. Note the layout **changed with this mint**: `0xA8C1C535` lives at `<hub-home>/mint_A8C1C535/fielded_dcp`, one directory per mint, while stage 8 now rsyncs to a single `MINT_HUB`. Both exist; a script that assumes one shape for both finds nothing. |

`fetch_fielded.sh` repopulates this directory from local scratch if it is
present, else over `scp` from the hub, then verifies. It is read-only at both
ends and never writes to the hub.

## Tracking these binaries

The decision taken on 2026-09-10 for `0xA8C1C535` carries over unchanged: **an
external store, made deliberate** — `MANIFEST.md5` + `fetch_fielded.sh` tracked,
the ~50 MB of binaries out of the repo. The full argument, including what
git-LFS would have cost a public repo, is in
[`../0xA8C1C535/README.md`](../0xA8C1C535/README.md#tracking-these-binaries--an-open-decision-and-it-is-the-owners)
and is not restated here, because a second copy of a decision is a second thing
to get wrong.

What is still missing is the same thing it was then: a gate that notices if the
off-tree copy disappears. Stage 8 of `make mint` pushes to `MINT_HUB`, so the
copy is made by the flow rather than by hand; nothing checks it is still there.

## Caveats

- **`verify_fielded.sh` in `../0xA8C1C535/` now reports FAILED**, correctly: its
  check 2 compares that directory's `static_id.txt` against the `fielded` row of
  `docs/FIELDED_SHELL.md`, and they no longer agree. That is the check doing its
  job on a superseded archive, not a corrupted one. Its `md5sum` half still
  passes.
- **These bytes are only as good as `MANIFEST.md5`.** The binaries are absent
  from a fresh clone; `verify_fielded.sh` with nothing fetched fails loudly
  rather than reporting a green verify over an empty directory.
- **Preserving the artefacts does not make the build reproducible.** A rebuild
  from source produces a different `static_id`, and the firmware in
  `config_rm_greybox_fw.bit` was baked from a dirty tree, so it does not rebuild
  either.
