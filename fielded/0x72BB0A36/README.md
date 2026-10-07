# `0x72BB0A36` — the fielded static shell (the 2026-10 ILA mint)

Fielded **2026-09-24**, remotely. `docs/FIELDED_SHELL.md` is the authority for
what is on the board; this directory preserves the build products of this mint
and says where they came from.

## What this shell is

The `0x3F1A560F` shell plus one static-side change set:

* **RM-internal Vivado ILAs over XVC.** The partition boundary grows by a
  12-wire BSCAN group (`dbgbscan`: 47 ports / 148 bits / 20 decoupler INTFs);
  `debug_bridge_0` gains a BSCAN master (`C_NUM_BS_MASTER=1`); an RM that wants
  ILAs carries a mode-1 `debug_bridge` and its own hub. Two such RMs ship:
  `rm_dbg_demo` (`0x01000009`) and `rm_nanosoc_ila` (`0x0100000A`).
* **`usr_access_rd` fixed** — the fabric's build identity reads back for the
  first time (`usr_access 0x01000001`, skew false).
* **MCC-facing pins tied off** (D1: `SMBM_nWAIT` high; `WDOG_RREQ`,
  `IOFPGA_SYSWDT`, `CFG_DATAOUT` low).

Thirteen overlays: the eleven of `0x3F1A560F` re-keyed, plus the two ILA RMs.
`rm_eth_ss` also changed, RM-side only: its bring-up FSM now sends one 802.3x
PAUSE beacon per second, so the DUT return path has a frame to carry.

## Provenance

Built **2026-09-23 13:16 → 18:07** by one `make -C fpga/dfx mint` from `c855108`
(`feat/rm-ila-mint`), in a detached worktree
(`<repo>`,
`BUILD=fpga/dfx/build_mint_2026_10`), all 13 configurations, `SHELL_TOUCH=1
TOUCH=1`. 13/13 routed, 12/12 `pr_verify` compatible, 26 HDPR reports clean,
`DFX_LTX_GATE_OK debug=2`. Stage 6 stopped once (the fresh worktree had no
Vitis BSP) and was resumed with the same BSP every earlier mint used.

**Stage 8 overwrote the previous shell's hub backup** — it rsynced into the
flat `MINT_HUB`, which held `0x3F1A560F`'s locked static. That set was restored
the same evening to `<hub-home>/mints/0x3F1A560F/`, and stage 8 now writes one
subdirectory per `static_id` (`987cf26`). This mint's archive is
`<hub-host>:<hub-home>/mints/0x72BB0A36/`; the flat files beside it are the
same bytes and can go.

## Two flashable bases

| file | firmware | when |
|---|---|---|
| `config_rm_greybox_fw.bit` (md5 `bfb0f5e0…`) | the mint's own bake, `c8551081` | on the card and running 2026-09-24 08:40 → 09:40 |
| `config_rm_greybox_fw_v011.bit` (md5 `2a457f7e…`) | v0.11, `987cf264` (net-protocol v0.11: `stats`, `log`, `reboot`, `touch_cal`, features 0–12) | trialled volatile 09:08; **on the card since 09:40 — the fielded image** |

Both went on the card by one `sd_install` each and were loaded by a **paced MCC
`REBOOT` on tty_00**, with nobody at the board
(`docs/evidence/2026-09-w3/w1_field_remote_20260924.txt`). The ELF of each is
listed in `MANIFEST.md5` and kept on the hub, not in git.

## What was proven on it

`docs/evidence/2026-09-w3/`: the overlay sweep (13/13), the first frames ever
through the DUT return path, swap-then-debug, the ILA proof ladder (B1–B5 and
the nanoSoC console decoded by an ILA), the v0.11 verbs, and the flash-boot
regression root-caused and fixed.

## Using this directory

```bash
bash fielded/0x72BB0A36/fetch_fielded.sh     # local build dir if present, else the hub
bash fielded/0x72BB0A36/verify_fielded.sh    # md5s, the FIELDED_SHELL.md row, the .ltx pairing
```

Tracked here: this README, `MANIFEST.md5`, the two scripts, `mint.json`,
`static_id.txt`, `overlay_inputs.txt`, `static_stamp.json` and the two
`.ltx.json` sidecars. Everything else is fetched.
