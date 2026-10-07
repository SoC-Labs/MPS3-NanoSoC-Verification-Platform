# fielded/ — the preserved build products of each fielded static

One directory per fielded `static_id` (`0xA8C1C535/`, `0x3F1A560F/`, …). Each
holds the small tracked record (`README.md`, `MANIFEST.md5`, `mint.json`,
`static_id.txt`, `overlay_inputs.txt`, `static_stamp.json`) and two scripts,
`fetch_fielded.sh` and `verify_fielded.sh`, that repopulate and check the
gitignored binaries. Why this exists, and why the binaries are not in git:
[`0xA8C1C535/README.md`](0xA8C1C535/README.md#tracking-these-binaries--an-open-decision-and-it-is-the-owners).

## Fielding a new mint — the recipe

The copy set is **generated, not typed**: `make -C fpga/dfx fielded-files`
prints every path to preserve, from the same list stage 8's hub copy uses. That
list includes, for every RM `rm_list.tcl` declares `RM_LIB(<rm>,debug) 1`:

* `config_<rm_key>.ltx` — the RM's partial probe file (never the empty
  `*_clear.ltx` or `*_<pblock>_partial*.ltx` extras Vivado writes beside it);
* `config_<rm_key>.ltx.json` — its sidecar: ILA UUIDs and the crc32 of the
  `.ltx` and of its partial;
* the partial/clearing `.bin` pair that `.ltx` describes.

**An `.ltx` travels with its partial.** A re-route of the RM gives a new
partial and a new `.ltx`; a stale pairing mislabels every probe and nothing on
the wire can tell (`docs/contracts/overlay-manifest.md`, "`ltx`"). The
0x3F1A560F staging copied only `.bin`s (`docs/planning/CLOSEOUT_2026-09-16.md`
§0); no ILA RM existed then, so nothing was lost — the next mint has two.

```bash
ID=$(cat <BUILD>/prod/static_id.txt)           # e.g. 0x1234ABCD
D=fielded/$ID; mkdir -p $D
make -s -C fpga/dfx fielded-files BUILD=<BUILD> SHELL_PROJ=<SHELL_PROJ> > /tmp/fset
xargs -a /tmp/fset cp -p -t $D                  # binaries: gitignored
( cd $D && md5sum $(xargs -a /tmp/fset -n1 basename) > MANIFEST.md5 )
cp fielded/_template/{fetch,verify}_fielded.sh $D/
sed -i -e "s#__DFX_PROD__#<BUILD relative to the repo>/prod#" \
       -e "s#__SHELL_PROJ__#<SHELL_PROJ relative to the repo>#" \
       $D/fetch_fielded.sh
# hub fallback: HUB (or MPS3_HUB) + HUB_DIR (or MPS3_MINT_ARCHIVE/<static_id>) from the environment
bash $D/verify_fielded.sh                       # check 2 passes only once FIELDED_SHELL.md says so
```

Then prepend the `MANIFEST.md5` header comment (where the bytes came from, what
matters most) the way the existing directories do, and write the mint's
`README.md`. **Do not update `docs/FIELDED_SHELL.md`'s `fielded` row until the
board reports the new id** — written is not fielded.

For a mint that also stages overlays to the hub for a board window (the
`mint_<id>/prod/` copy of 2026-09-16), copy the same `.ltx` set beside the
`.bin`s there: `make -s -C fpga/dfx fielded-files … | grep -E '\.ltx|_partial'`.

## The templates

`_template/fetch_fielded.sh` reads its file list from the directory's own
`MANIFEST.md5` instead of a hard-coded array, so a new artefact kind (the
`.ltx` set was the first) is fetched whenever it was preserved.
`_template/verify_fielded.sh` adds check 3: every `config_<rm_key>.ltx` in
`MANIFEST.md5` has its sidecar and its partial pair listed, and when they are
present the sidecar's crc32s match. The per-mint copies in `0xA8C1C535/` and
`0x3F1A560F/` predate the templates and are left as they were fielded.

## Linux (MicroBlaze V) records

A `SHELL_CPU=mbv` static is recorded the same way, from
[`_template_linux/README.md`](_template_linux/README.md), plus four things:
- the stage0 baked into the LMB (`stage0.elf`, `stage0_bake.json`);
- `static_canon.json`, whose flags carry `SHELL_CPU=mbv`;
- the release bundle `linux_bundle.json`, with its two targets (the MCC config SD, and
  everything that goes over Ethernet — `docs/planning/linux_lanes/FLOW_CONTRACT.md` §0);
- `linux_legal_info.tar` (GPL — it travels with every copy of the image).

The copy set is `make -s -C fpga/dfx fielded-files SHELL_CPU=mbv BUILD=<b> SHELL_PROJ=<b>/shell_proj`.
It refuses a prototype (P-mint) build. Such a build is never fielded.

`scripts/harness_gates/check_image_overlay_match.py` checks every committed
`fielded/*/linux_bundle.json`. It checks image ↔ static ↔ overlays, from the files in
the directory, and fails any record that is not fieldable.
