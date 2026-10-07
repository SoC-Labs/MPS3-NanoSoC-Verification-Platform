# `mint.json` — the record of one mint

Written by `fpga/dfx/tools/mint_record.py` (stage 7 of `make -C fpga/dfx mint`),
checked by `mint_record.py verify`, gated by `tests/dfx_flow/test_mint_record.py`.
A filled-in illustration lives at `fpga/dfx/mint.json.example`; the fielded
shell's own record is `fielded/0x3F1A560F/mint.json`, written by the run that
minted it, and `fielded/0xA8C1C535/mint.json` is the reconstruction of its
predecessor's — the one that had no record at all.

## Why a mint needs a record at all

A mint produces two identities and **neither one describes the run**.

`static_id` is a CRC-32 of the routed static. It answers "are these overlays
keyed to this shell?" and nothing else — not which source trees were read, not
which firmware flags were baked beside it, not which RM checkpoints were
*reused* from an older tree rather than rebuilt.

`docs/FIELDED_SHELL.md` answers "what is on the board", deliberately by hand,
because nothing can compute it.

Everything in between — the run — was recorded nowhere, and the bill came due
twice. `fielded/0xA8C1C535/README.md` had to reconstruct the fielded mint from
**file timestamps**. And for `rm_socscope` the chain simply runs out: no
`synth.log` exists in any tree, so nothing on disk records the SoCScope revision
that RM was synthesised from. A partial built from an unknown revision cannot be
reproduced and cannot be audited after the fact.

## The one rule

> A field that cannot be established is `null` **with the reason it is null**.
> Never a guess, never a plausible default, never silently absent.

Every provenance slot is an object with exactly two keys:

```json
{ "value": <anything>, "reason": null }     // known
{ "value": null,       "reason": "why" }    // not known, and why
```

`verify` **fails** a record where a slot has both — a value that arrived beside
an excuse is a value somebody invented — and fails one where a slot has neither.

## Site neutrality

No absolute path reaches the record, and `verify` fails one that does:

| what | how it is stored |
|---|---|
| build dir, shell project | relative to the repo root |
| overlay `partial`/`clearing` | relative to the **build dir** |
| external source trees | a git SHA and a dirty flag — never a path |
| the hub destination | not recorded at all (it is `MINT_HUB`, site-local) |

This is the same defect `overlay_inputs.txt` had: ten rows naming one
workstation's home directory, in the one file whose job is to say what a mint
produced. It could not be copied to the hub, replayed in another checkout, or
committed beside the checksums.

## Fields

| field | type | meaning |
|---|---|---|
| `schema` | `"mps3-mint-record"` | fixed |
| `schema_version` | `"1.0"` | bumped when a field changes meaning |
| `record_kind` | `"mint"` \| `"reconstructed"` | written by the flow, or rebuilt afterwards from what survived |
| `generated_at` | ISO-8601 Z | when this file was written (**not** when the mint ran) |
| `generated_by` | path | the tool |
| `static_id` | `0xXXXXXXXX` | CRC-32 of `static_routed_locked.dcp` |
| `build_dir` | path, repo-relative | where the mint ran |
| `shell_proj` | *slot* | the shell project the DFX flow linked against |
| `rm_set` | `[rm_key]` | the RMs routed against this static |
| `sources.repo` | *slot* → `{sha, dirty}` | this repo at mint time |
| `sources.nanosoc_m0_soc` | *slot* → `{sha, dirty}` | the nanoSoC RM source tree |
| `sources.socscope` | *slot* → `{sha, dirty}` | the SoCScope RM source tree |
| `reused_dcps` | *slot* → `{from, checkpoints, note}` | RM checkpoints **copied in**, not synthesised here |
| `firmware.flags` | *slot* → `{NAME: VALUE}` | the `PRODUCT=1` expansion, **read from `firmware/platform/Makefile`** |
| `firmware.product` | *slot* → bool | whether that expansion was applied |
| `firmware.elf` | *slot* → `{name, md5, bytes}` | the image baked in beside this static |
| `dcps` | `[{name, md5, bytes, mtime}]` | every checkpoint in the build dir (+ shell project) |
| `artefacts` | `[{name, md5, bytes, mtime}]` | the `.bit` / `.mmi` / `.xsa` beside them |
| `overlay_inputs` | `[{rm_key, rm_name, rm_id, partial, clearing}]` | the hand-off rows, paths relative to `build_dir` |
| `timestamps` | `{label: ISO-8601 Z}` | mtimes of the load-bearing artefacts |
| `tools.vivado` | *slot* | the toolchain version |
| `integrity` | object | for a reconstructed record: whether the tracked text files still match their own `MANIFEST.md5` |

`bytes` and `mtime` are `null` in a reconstructed record: `MANIFEST.md5` records
checksums only, and the binaries it names are gitignored.

## Two things the record deliberately does NOT do

**It does not restate what is on the board.** `docs/FIELDED_SHELL.md` is the only
authority for `fielded`, and it changes only when someone watches a board come
up on a new shell. A mint record describes a *build*; a build is not a
deployment, and the two are routinely different (see that file, "The two facts
are not the same fact").

**It does not read `docs/FIELDED_SHELL.md` when reconstructing.**
`mint_record.py reconstruct` reads exactly three files — `static_id.txt`,
`overlay_inputs.txt`, `MANIFEST.md5` — and nothing else. That keeps the
reconstruction hermetic (same answer on any machine, with or without the
gitignored binaries), which is what lets the committed
`fielded/0xA8C1C535/mint.json` be gated by a round-trip test. The firmware flag
set is therefore `null` there, with a reason pointing at the row that does know.

## Regenerating

```bash
# from a finished build tree (stage 7 does this for you)
python3 fpga/dfx/tools/mint_record.py record \
    --build-dir fpga/dfx/build_<name>/prod --repo . \
    --shell-proj build/shell_proj_<name> \
    --product-makefile firmware/platform/Makefile --fw-flags "TOUCH=1" \
    --fw-elf build/vitis_fw/shell_fw/shell_fw.elf \
    --rm-set "rm_greybox rm_led ..." --vivado-version 2024.1

# from a preserved fielded directory
python3 fpga/dfx/tools/mint_record.py reconstruct \
    --dir fielded/0xA8C1C535 -o fielded/0xA8C1C535/mint.json

# check one
python3 fpga/dfx/tools/mint_record.py verify fielded/0xA8C1C535/mint.json
```
