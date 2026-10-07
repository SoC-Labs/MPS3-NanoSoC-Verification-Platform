# `0x44EE76D5`: the fielded MicroBlaze V (Linux) static, preserved


This directory holds the build products of the Linux static **`0x44EE76D5`**. `docs/FIELDED_SHELL.md`
records it as `fielded` since 2026-10-01, with `fielded_by` `ad297bf`.
- It supersedes [`../0x72BB0A36/`](../0x72BB0A36/README.md), the bare-metal MicroBlaze static.
- That static and its 13 overlays stay on the hub as the **rollback image** until the deletion
  PR (the recovery ladder, `docs/LINUX_HARNESS.md` §5 step 6).

**Status.**
- **Board 1** has run this static continuously since 2026-09-27 10:46, after two bare-metal
  rollbacks on 26 and 27 Sep (`docs/evidence/2026-09-linux-b2/b2_rollback.txt`,
  `docs/evidence/2026-09-linux-b2/rebake_20260927/`). It ran the re-bake `286ae54d…` first, and
  has run its fielded bake `f876e73e…` since 2026-09-28 21:10.
- **Board 2** has run it since 2026-09-28, on its own bake `f206f788…`.
- **The gate.** The 24 h accelerated gate soak on board 1 with image rc2_v7 said **PASS**
  (ended 20:47:27 BST Wed 30 Sep 2026: 0 FAIL events). It is `docs/evidence/2026-09-linux-gate/` §5.
- **The cutover** was declared on 2026-10-01 by the project lead. Before it, this directory was the
  RC2 record (`6beea09`).

## The identities this static carries

`python3 scripts/harness_gates/check_image_overlay_match.py --linux-record fielded/0x44EE76D5`
re-derives every one of these from the files in this directory.

| Identity | Value | Where it lives | Proves |
|---|---|---|---|
| `static_id` | `0x44EE76D5` | `static_id.txt`; the CRC-32 of `static_routed_locked.dcp` | an overlay fits this routed static |
| UserID (USERCODE) | `0xFB1F8C76` | `static_stamp.json`; the header of every `config_rm_greybox*.bit` here; every RC2 overlay's `static_usercode` | the implementation run (`fb1f8c7`) |
| HARNESS_VER32 (USR_ACCESS) | `0x01000000` (harness 1.0.0), both SLRs | `static_stamp.json`; `stage0_bake*.json` `usr_access` | the harness release the fabric was stamped with (`VERSION` is still 1.0.0; the tag `v2.0.0` names the repo release) |
| `static_canon` | `c906d74c5d03` (first 12) | `static_canon.json`; `mint.json`; `linux_bundle.json` | the same inputs; flags `SHELL_CPU=mbv SHELL_TOUCH=1 SHELL_REALPHY=0` |
| stage0 compiled-in identity | `mps3_stage0_static_id=0x44EE76D5`, `mps3_stage0_ver32=0x01000000` | every `stage0_bake*.json` `stage0_elf_check.baked_constants` | the board reports THIS fabric, whatever card is in the slot |
| slot image | rc2_v7, sha256 `05c83617bfac…` | `linux_slot.img`; `linux_bundle.json` `targets.ethernet.slot_image` | the image stage0 boots, provisioned FOR `0x44EE76D5` |

## Per board: what each config SD holds

The static is one static, but every board's config SD carries a different stage0 bake. updatemem
changes only the BRAM and CRC frames: `fpga/dfx/tools/stage0_bake.py` re-derives the static and
keeps UserID and USR_ACCESS, and the bake lane's frame-level diff showed only BRAM + CRC. So
`static_id`, UserID, USR_ACCESS and every overlay are the same on both boards. What differs is
stage0: its build, and the identity it publishes to Linux (`34956f9`, `18622e5`).

| | **Board 1** `<board-1 target>` | **Board 2** `<board-2 target>` (now) | **Board 2** (next) |
|---|---|---|---|
| Config-SD bit (the file here) | `config_rm_greybox_stage0.bit` | `config_rm_greybox_stage0_b2.bit` | `config_rm_greybox_stage0_field_b2.bit` |
| sha256 | `f876e73e412c35b65130a7966819d63cce3ec1f5aa5b6e8f9838f43c7a9660ba` | `f206f788f7497b650b6f0408ebb2fbdb795edb749784a3ec42e6caaaa3df5058` | `d7d0d37601843df2a99aa4429c43b5033050f216082d7133b632eddff3356015` |
| Bake record | `stage0_bake.json` | `stage0_bake_b2.json` | `stage0_bake_field_b2.json` |
| stage0 ELF (sha256, first 12) | `stage0_field_b1.elf` `7612bfc5afe8` | `stage0_b2.elf` `1bbfc229caf3` | `stage0_field_b2.elf` `d62a4bbfe5c1` |
| stage0 build | `0x34956F94` (`cd767ae` cold-start fix + `34956f9` identity publish) | `0x6FAE6A0B` (rescue IP only) | `0x34956F94` |
| Cold-start fix | **yes** | **no**: a cold start that fails in DDR needs PB0 | yes |
| Label / IP / MAC that Linux uses | `MPS3-01` / 192.168.10.101 / `02:00:00:4D:50:53` (identify: source `stage0`) | the image defaults, label `MPS3` and **MAC `02:00:00:4D:50:53` = board 1's**; the board-2 IP by DHCP. *Expected from the image; not read on board 2 under v7n* | `MPS3-02` / <board-2 IP> / `02:00:00:00:02:FE` (the bake's defines) |
| Boots Linux from | the user µSD, slot A (rc2_v7) | netboot: `b2_keeper` pushes rc2_v7n (`65b6bbaf…`) | netboot |
| On the SD since | 2026-09-28 21:10 (fpgahubd witness `ok=True sha256=f876e73e412c dur=78.89s`) | 2026-09-28 (board 2 commissioning) | **not fielded**: it needs PB0 cover on site for its first cold REBOOT |
| Evidence | `docs/evidence/2026-09-linux-gate/field_b1_20260928/` | `docs/evidence/2026-09-linux-b2/board2_commission/netboot.txt`; `docs/evidence/2026-09-linux-b2/ddr_fail_20260928/b2_mcc_reboot.txt` (build `0x6FAE6A0B` on silicon) | the bake log only (lane FIELD, 2026-09-28 16:00) |

`linux_bundle.json` names **board 1's** pair: `targets.mcc_sd.flashable_bit` = `f876e73e…`,
`stage0` = `7612bfc5…`. That is why `config_rm_greybox_stage0.bit` and `stage0_bake.json` here
are board 1's. Board 2's bakes are recorded in the table above and checked by `MANIFEST.md5`, but
no bundle names them. A per-board bundle is a FLOW follow-up (the kit's `on_config_sd.config_sd_bits`
map already carries both boards' shas).

## The Linux image

| | rc2_v7 (card) | rc2_v7n (netboot) |
|---|---|---|
| File here | `linux_slot.img` | `linux_slot_v7n.img` |
| sha256 | `05c83617bfac30c6c66a09dc946b2243cb9112692a9afa2d8352bcba054569ea` | `65b6bbaf16171c10aaef80c5f44aab2b93800400183b0e2c7613456aec059680` |
| Size | 29,447,688 B; S0LB v2, header CRC `0x5CEAED1B` | 29,447,688 B |
| Built from | `ad297bf`, `dirty=0` (`/etc/mps3/version`: `harness=1.0.0 sha=ad297bf9 dirty=0 static_id=0x44EE76D5`) | the same build, `mps3.persist=off` |
| Contents | kernel 6.18.7, OpenSBI 1.6, Buildroot 2026.02.3; `mps3-harnessd` sha256 `e91bf504…`; stage0 it expects `7612bfc5…` | the same |
| Proof off the board | QEMU 46/46; bundle check OK; image-size gate PASS; harnessd check ALL PASS | persist=off + LCD 16/16 |
| On the board | both slots of board 1 since 2026-09-29 (`hdr_crc 0x5ceaed1b`), running from A | board 2, pinned in `b2_keeper` since ~2026-09-29 20:55 |

The proof off the board is recorded in the Linux lead's lane, `image/rc2_v7_clean/v7_clean_report.out`.

The release manifest is `linux_bundle.json` (sha256 `333a69704437…`, `fieldable: true`,
`mint_kind: mint`, `image_kind: release`). Its legal-info is `linux_legal_info.tar` (sha256
`5ae61c197d25…`, 207,953,920 B). **It must travel with every copy of the image that leaves the lab**
(GPL).

## The cold-start fix, proven 6/6 (board 1, 2026-09-28)

**The fault.** On 28 Sep both boards failed cold boots with a card present. The working model is
plausible but was never proven by a probe:
- the MCC programs its oscillators after it configures the FPGA;
- the MIG then re-calibrates DDR4 under stage0's first card reads.

The fielding boot below saw exactly that as a calibration drop.

Evidence: `docs/evidence/2026-09-linux-b2/ddr_fail_20260928/`.

**The fix, `cd767ae`.** stage0 arms the WDOG from entry. It holds a 10 s cold settle plus a 1 s
calibration hold, re-checked before every card read, and gives up on a slot load after 300 s.

**The proof** (`docs/evidence/2026-09-linux-gate/`):
- **The fielding boot:** run mode in 156 s. stage0 recorded `calib_drops 1`, `cal_at_settle true`,
  `ddr_ok_ms 11017`, `wdog_run 0`. Calibration dropped inside the settle window and recovered, and
  the card was read after it.
- **Five more cold MCC boots from card slot A: 5/5 PASS.** To 6900: 186.4-188.6 s. To SSH: ≤ 204.5 s.
  Every boot came from A and was confirmed (`boots_b1_20260928/`).
- **Image during these boots:** rc2_v6. v7 went into the slots the next morning. The fix is in
  stage0, which is in the config-SD bit, not in the image.
- **The gate soak** repeats this 13 times on v7: 13/13 power-ons (§ below).

**The limit.** This is a stage0 workaround. MIG `sys_rst` is `~USER_nPB0` only (`fpga/shell/bd/cpu_mbv.tcl`),
so nothing in software can hold the MIG in reset through the MCC's clock window. The real fix is
the CB_nRST boot gate in mint 4.

## The gate soak (board 1, rc2_v7, 2026-09-29/30)

`soak_linux.py accel --duration 24h`, started 20:46:21 BST Tue 29 Sep. The run:
- **Drills:** 13 power-ons with the card, 4 WDOG trips, 5 MCC reboots, 1 slot-A fallback drill,
  `set_clk` at hours 0 and 18, the 13-RM sweep, and a swap every 3 min.
- **Bar:** SSH p99 ≤ 20 s (the project lead, 28 Sep).
- **Record:** `docs/evidence/2026-09-linux-gate/gate_v7_20260929/`.

| Verdict | Power-ons | WDOG / MCC / fallback | Swaps | SSH p99 | Resources |
|---|---|---|---|---|---|
| **PASS** | 13/13 (median 195.7 s to 6900) | 4 trips (13 WDOG resets) / 6 + 3 reboot verbs / 1 passed | 458, all verified | 13.84 s | flat: RSS 0.0 KiB, SUnreclaim +3.5 KiB, fds +0.1 |

The verdict rests on zero FAIL events over 86466 s, with every drill run (`drills_not_run` []) and every boot from slot A confirmed apart from the fallback drill's. There is no FAIL line to cite. The 799 `warn` lines are all `svc_skipped rose` (v2.1 backlog `svc_overruns`).

## Two targets (`docs/planning/linux_lanes/FLOW_CONTRACT.md` §0)

| Target | Files | How it reaches a board |
|---|---|---|
| **`mcc_sd`**: the config SD | the board's own `config_rm_greybox_stage0*.bit` (per-board table) | `pyverify sd field <bit> --expect-sha256 <sha>`. It makes ONE write, waits for the fpgahubd journal line `program dispatched … ok=True sha256=<first 12>`, then sends a paced MCC `REBOOT` on `tty_00` (one reader, CR first, 100 ms/char). The client timeout on the write is expected. Never write twice |
| **`ethernet`**: over the network | `linux_slot.img` / `linux_slot_v7n.img`, the 13 overlays, `linux_bundle.json`, `linux_legal_info.tar` | A card board: `pyverify slot push` + `commit` + reboot (41-50 min per slot). Netboot: `stage0_push.py` / `pyverify netboot`. Overlays: 6900/6910 |

**The 13 overlays** are `clcd_demo`, `dbg_demo`, `eth_ss`, `greybox`, `led`, `nanosoc`,
`nanosoc_ila`, `nanosoc_multicore`, `nanosoc_upy`, `regdemo_a`, `regdemo_b`, `socscope` and
`uart_echo`. They are keyed to `0x44EE76D5` and bound to UserID `0xFB1F8C76`. Only the two `.ltx`-bearing
pairs are in this directory. The full set is on the hub at `<hub-home>/pv_soak/ovl/<rm>/` (what
the soaks push) and at `<hub-home>/mints/0x44EE76D5/overlay_mbv/` after C3. The tracked
`fpga/dfx/overlay/` set stays keyed to bare metal (`minted` = `0x72BB0A36`; FLOW_CONTRACT §1.1).

## What this static is

It is the `0x72BB0A36` partition on a MicroBlaze V (Linux) shell, built with Vivado 2026.1.

**The same RP boundary.** 47 ports, 148 bits, 20 decoupler INTFs and `pblock_rp_dut`. At `fb1f8c7`,
these files are byte-identical to the ones the `0x72BB0A36` shell was built from:
- `fpga/shell/boundary.yaml`;
- `fpga/shell/rp_dut_stub.sv`;
- `fpga/dfx/dfx_floorplan.xdc`;
- `docs/contracts/partition-{pins,timing}.md`.

**A different static.** It adds MicroBlaze V + DDR4 MIG + `usd_spi`, the per-SLR USR_ACCESS
reader and the DUT inject path on DUTEGR. A new static means a new `static_id`: every overlay is
re-keyed, and a `0x72BB0A36` overlay does not load.

**`dut_clk` enters the partition from `BUFGCE_X2Y47`** (`0x72BB0A36`: `BUFGCE_X2Y24`). Only an OOC
implementation reads `HD.CLK_SRC`. All 13 RC2 configurations met timing.

## Every bake of this static

| Bake | Bit sha256 (first 12) | stage0 build | Where it is now | Record |
|---|---|---|---|---|
| The mint's own bake (2026-09-25) | `8a30ade887b1` | the mint's | nowhere on a board; build dir + hub `config_rm_greybox_stage0.bit` | `stage0_bake_mint.json`, here as `config_rm_greybox_stage0_mint.bit` |
| The RC2 re-bake (2026-09-26) | `286ae54d2a2b` | `0xC457D656` | on no board since 2026-09-28 21:10; no archived copy | `stage0_bake_rebake.json` |
| Board 2's commissioning bake (2026-09-28) | `f206f788f749` | `0x6FAE6A0B` | **board 2's config SD** | `stage0_bake_b2.json` |
| Board 1's fielded bake (2026-09-28) | `f876e73e412c` | `0x34956F94` | **board 1's config SD** | `stage0_bake.json` |
| Board 2's next bake (2026-09-28) | `d7d0d3760184` | `0x34956F94` | not fielded | `stage0_bake_field_b2.json` |

## Provenance

- **Built** 2026-09-24 22:09 → 2026-09-25 02:29, by one `make -C fpga/dfx mint SHELL_CPU=mbv` from
  `fb1f8c7` (`feat/linux-harness`, clean), with `DFX_JOBS=4`, `SHELL_TOUCH=1 TOUCH=1` and all 13 RMs.
  - Timing: WNS +0.207 ns in the static DDR domain, WHS ≥ +0.013 ns.
  - Gates: XDC 20-1307 = 0; USR_ACCESS equal in both SLRs; `DFX_LTX_GATE_OK rms=13 debug=2`.
- **The per-board bakes**: `bake_field.sh` (lane FIELD, 2026-09-28 16:00), from `34956f9` + the
  IDENT stage0 hunk. A frame-level diff against each board's previous bit reports "ONLY BRAM
  CONTENT (+ CRC) DIFFERS".
- **The image**: the clean build of `ad297bf` (lane V7-BUILD, 2026-09-28 23:53).
- **Fielded by** the Linux lead, by remote SD write and paced MCC REBOOT, no hands:
  - board 1's bake at 2026-09-28 21:10;
  - rc2_v7 into board 1's card slots on 2026-09-29, B 08:26-09:03 and A 09:07-09:49.
- **Cutover** 2026-10-01 after the gate verdict; cutover GO by the project lead.
- **Evidence:**
  - `docs/evidence/2026-09-linux-b2/`: soak run 4, the 28 Sep visits, board 2 commissioning and the
    DDR failures;
  - `docs/evidence/2026-09-linux-gate/`: the fielding, the 6/6 cold boots, the tail, the v7 slot
    pushes and the gate soak.

## Every place a copy lives

- **This workstation:** `fpga/dfx/build_mint3_rc2/{prod,shell_proj}` in the lx worktree (the mint's
  set). The fielded set is in the Linux lead's lanes: `field/out/` (the bakes) and
  `image/rc2_v7_clean/` and `image/rc2_v7n_clean/` (the images, the bundle and the legal-info).
- **The hub:** `<hub-host>:<hub-home>/mints/0x44EE76D5/` is the mint's stage-8 set plus
  `kit/` (the RC2 DUT build kit v2, zip sha256 `95768b64…`). **At C3**, add `fielded/` (the fielded
  set, under the names in `MANIFEST.md5`) and `overlay_mbv/` there. Verify by listing, never by
  trusting this line.
- **Staging copies on the hub:**
  - `<hub-home>/pv_field/config_rm_greybox_stage0_field_b1.bit` (= `config_rm_greybox_stage0.bit` here);
  - `<hub-home>/pv_field/linux_slot_v7.img` (= `linux_slot.img`);
  - `<hub-home>/pv_field/linux_slot_v7n.img`.

## Using this directory

```bash
bash fielded/0x44EE76D5/fetch_fielded.sh     # the mint's set from build_mint3_rc2/ or the hub; the fielded set from FIELD_DIR or the hub's fielded/
bash fielded/0x44EE76D5/verify_fielded.sh    # md5s, the FIELDED_SHELL.md row, the .ltx pairing
python3 scripts/harness_gates/check_image_overlay_match.py --linux-record fielded/0x44EE76D5
```

- `fetch_fielded.sh` maps the two renamed mint files back to their source names.
- It never overwrites a file that already matches `MANIFEST.md5`. The tracked records are
  safe from a same-named file at the source.
- `verify_fielded.sh` check 3 skips the static's own `config_rm_greybox_static.ltx` (the MIG
  debug hub), which has no partial.

**Tracked here:**
- this README, `MANIFEST.md5` and the two scripts;
- `mint.json`, `static_id.txt`, `overlay_inputs.txt`, `static_stamp.json` and `static_canon.json`;
- `linux_bundle.json`;
- every `stage0_bake*.json`;
- `config_rm_greybox.mmi` and `debug_core_rm_greybox_static.rpt`;
- the three `.ltx.json` sidecars.

Everything else is fetched: `.bit`, `.dcp`, `.xsa`, `.elf`, `.ltx`, `.bin`, `.img` and `.tar`
are all gitignored.

## Caveats

1. **Board 2 is not on its fielded bake.** It runs `f206f788…`, which has no cold-start fix and
   no identity. Its Linux shows board 1's MAC and the default label. Its identity bake
   `d7d0d376…` is waiting for PB0 cover on site.
2. **The cold-start fix is stage0-only** until mint 4 (CB_nRST). A cold start with a stuck MIG
   still needs PB0.
3. **`minted` ≠ `fielded` is the normal state now.** The tracked overlay set is bare metal's
   (`fpga/dfx/overlay/`, `0x72BB0A36`), and R9 prints its note. RC2's Linux overlays are hub and
   build-dir artefacts (above).
4. **One bundle, board 1's.** The gate checks board 1's bit and stage0 against it. Board 2's bakes
   are checked by md5 only.
5. The earlier soaks' warnings still apply:
   - SSH p99 is 13.6-14.3 s on the 100 MHz MicroBlaze V;
   - slot writes take 41-50 min;
   - the D13 commit aborts on a card stall longer than 30 s.

   The v2.0.0 release notes carry them: `docs/planning/RELEASE_NOTES_v2.0.0.md`.
