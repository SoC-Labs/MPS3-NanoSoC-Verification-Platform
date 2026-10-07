# `0x________` — the fielded MicroBlaze V (Linux) static, preserved

<!--
TEMPLATE (FLOW lane, 2026-09-23). Copy this directory to fielded/<static_id>/ for a
Linux (SHELL_CPU=mbv) mint and fill every ________. It follows the bare-metal records
(../0x3F1A560F/) -- same MANIFEST.md5 discipline, same fetch/verify scripts
(../_template/) -- and adds what a Linux static carries that a bare-metal one does not:
the stage0 baked into the LMB, and the release bundle (docs/planning/linux_lanes/
FLOW_CONTRACT.md §0). Delete this comment when the record is written.

A PROTOTYPE (P-mint, MINT_KIND=prototype) IS NEVER FIELDED: `make fielded-files`
refuses it and the gate fails any prototype bundle under fielded/.
-->

This directory holds the build products of the Linux static **`0x________`**, which
`docs/FIELDED_SHELL.md` records as `fielded` (________, `________`). It supersedes
[`../0x________/`](../0x________/). The bare-metal static and its overlays stay on the
hub as the rollback image until the deletion PR (plan §3 recovery ladder, step 4).

## The identities this static carries

Every one of these is re-derived from the files in this directory by
`python3 scripts/harness_gates/check_image_overlay_match.py --linux-record fielded/0x________`.
Nothing here is taken on the record's word.

| Identity | Value | Where it lives | Proves |
|---|---|---|---|
| `static_id` | `0x________` | `static_id.txt`; CRC-32 of `static_routed_locked.dcp` | an overlay fits this routed static |
| UserID (USERCODE) | `0x________` | the header of `config_rm_greybox{,_stage0}.bit`; `static_stamp.json`; every overlay's `static_usercode` | the implementation run (git `________`) |
| HARNESS_VER32 (USR_ACCESS) | `0x________` | the `.bit`; `stage0_bake.json` `usr_access` | the harness release |
| `static_canon` | `________` (first 12) | `static_canon.json`; `mint.json` | the same inputs; flags include **`SHELL_CPU=mbv`** |
| stage0 compiled-in identity | `mps3_stage0_static_id=0x________`, `mps3_stage0_ver32=0x________` | `stage0.elf` + `stage0_bake.json` `stage0_elf_check.baked_constants` | the board reports THIS fabric, whatever card is in the slot |
| slot image | sha256 `________` (first 12) | `linux_slot.img`; `linux_bundle.json` | the image stage0 boots, provisioned FOR `0x________` |

## The two targets (FLOW_CONTRACT.md §0)

| Target | Files | How it reaches a board |
|---|---|---|
| **`mcc_sd`**: the config SD | `config_rm_greybox_stage0.bit` | One SD install (the client timeout is not a failure; never retry mid-write). **Then wait** for the fpgahubd journal's `program dispatched … ok=True sha256=<first 12 of targets.mcc_sd.flashable_bit.sha256>`. **Only then** send a paced MCC `REBOOT` on `tty_00` (one reader, CR first, 100 ms/char). FLOW_CONTRACT.md §5 |
| **`ethernet`**: over the network | `linux_slot.img`, the overlays (`../../fpga/dfx/overlay_linux/` or the hub copy), `linux_bundle.json`, `linux_legal_info.tar` | TFTP rescue push (`stage0_push.py`), or Linux writing µSD slot A/B + the boot-select sector; overlays over 6910 |

## The artefacts

The copy set is generated:
`make -s -C fpga/dfx fielded-files SHELL_CPU=mbv BUILD=<b> SHELL_PROJ=<b>/shell_proj`.

| File | Tracked? | Why it is kept |
|---|---|---|
| `README.md`, `MANIFEST.md5`, `mint.json`, `static_id.txt`, `overlay_inputs.txt`, `static_stamp.json` | yes | same as the bare-metal records |
| `stage0_bake.json` | yes | which stage0 is in the base, which `-proc`, that its compiled-in identity was checked, and which static `.ltx` is bound to the base |
| `config_rm_greybox_static.ltx.json`, `debug_core_rm_greybox_static.rpt` | yes | the static probes file's sidecar (UserID, static_id, uuids) and the Vivado report it was checked against |
| `config_rm_greybox_static.ltx` | no (`MANIFEST.md5`) | the STATIC's probes file (SEAM-8 MIG hub + the DDR4 calibration slave). HW Manager needs it for the MIG calibration view over XVC, and it cannot be regenerated without the locked static |
| `static_canon.json` | yes | the content hash with `SHELL_CPU=mbv` (the gate reads its flags) |
| `linux_bundle.json` | yes | the release manifest: both targets, the image record, the overlay snapshot, legal-info |
| `static_routed_locked.dcp`, `config_rm_greybox_routed.dcp`, `config_rm_greybox.mmi`, `config_rm_greybox.bit` | no (`MANIFEST.md5`) | as for bare-metal: the only way to add an overlay later. **Written by Vivado 2026.1**, and 2024.1 cannot open them |
| `config_rm_greybox_stage0.bit` | no | the flashable base; not reproducible from any commit (per-static stage0) |
| `stage0.elf` | no | the exact stage0 baked in (per-static constants) |
| `linux_slot.img` | no | the exact slot image, S0LB |
| `linux_legal_info.tar` | no | Buildroot legal-info (GPL). **It must travel with every copy of the image that leaves the lab** |
| `shell_static_synth.dcp`, `shell_harness.xsa` | no | the pre-DFX static (2026.1) |
| every `config_<rm>.ltx` + `.ltx.json` + its partial pair | no | as for bare-metal |

## `linux_bundle.json`: the fields this record relies on

`schema` = `mps3-linux-bundle` v1. The contract is FLOW_CONTRACT.md §0.1.

A fielded record must have:
- `mint_kind` = `mint` and `fieldable` = `true`;
- `shell_cpu` = `mbv`;
- `targets.mcc_sd.stage0.identity_checked` = `true`;
- `targets.ethernet.components.image_kind` = `release`;
- `targets.ethernet.legal_info` non-null.

## Provenance

- The mint run: `mint.json`.
- The image build: `linux_bundle.json` `targets.ethernet.components`, from the image's
  `/etc/mps3/version`.
- Who fielded it, when, and the board evidence (`docs/evidence/…`): ________.

## Every place a copy lives

________ (the hub `MINT_HUB` path, verified by listing it; this workstation's
`<b>/prod`).

## Caveats

________
