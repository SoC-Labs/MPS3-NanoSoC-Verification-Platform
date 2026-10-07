# The bootable image and the partials come from ONE implementation run

**Decision (2026-07-24): `fpga/dfx/build_linux/prod/config_rm_greybox.bit` is THE harness image.**
phaseB's `src/linux_harness/build/transplant_impl/shell_linux_top.bit` is not a product image and must
not be booted.

## What was wrong

One synthesis was implemented twice:

```
src/linux_harness/build/transplant_impl/shell_static_synth.dcp     <- ONE synthesis
   |
   |- phaseB impl run   -> transplant_impl/shell_linux_top.bit        11,883,949 B   (no UserID stamp)
   '- build_dfx.tcl run -> build_linux/prod/config_rm_greybox.bit     12,821,235 B   UserID=5263642C
                           + every partial + clearing bitstream
```

`build_dfx.tcl` re-implements that synthesis **with the reconfigurable partition and `lock_design`**, then
emits the full static+greybox bitstream *and* all the partials, as one self-consistent set. A partial is
bound to the exact static place-and-route it was built against, so only build_dfx's static can accept them.

The board was booting phaseB's bitstream while deploying build_dfx's partials. Same design, different
placement and routing — so every swap attempt **destroyed the whole FPGA configuration**, deterministically,
by any write path (Linux/ICAP and JTAG alike). It happened twice on 2026-07-24 before the cause was found.

## Why the existing checks all passed

| Check | Why it missed this |
|---|---|
| `static_id` (`0x2B082E1B`) | A CRC of `static_routed_locked.dcp`, minted once and shared. It identifies the **design**, not the **implementation** — both bitstreams carry the same value. |
| the swap's own `static_id` check | Compares the manifest against the *provisioned file* `/etc/mps3/static_id`, never the flown bitstream. Reads correct whatever is actually flown. |
| `pr_verify` | Only compares an RM against the static of **its own build run** (greybox vs led, same run). |
| FAR decode | Only showed frames landing in the RP *region*, which they did. |

## The fix

**One image, named once.** `src/linux_soc/hw/board_scripts/board_image.tcl` is the single source of truth
for `BIT`; all 17 boot scripts `source` it instead of hardcoding a path. 13 of them previously programmed
phaseB's bitstream — any one of them re-introduced the hazard.

**Identity travels with the artefacts.** `gen_manifest.py --static-bit <full_static.bit>` records
`static_usercode` in each overlay manifest — `BITSTREAM.CONFIG.USERID`, the 8-hex build commit, which the
device reports back as `REGISTER.USERCODE`. Unlike `static_id`, it **differs between implementation runs**,
so it is the identity that actually discriminates.

> Not `USR_ACCESS`: `build_dfx` stamps that too, but it carries `HARNESS_VER32` (`0x01000001`) — the harness
> *version*, identical across impl runs of one version. It cannot tell the two statics apart.

**Two gates, one identity:**

* **Build time** — `scripts/harness_gates/check_image_overlay_match.py` (in `make check`) asserts the image's
  UserID, `board_image.tcl`'s declared `BIT_USERCODE`, and every overlay's `static_usercode` all agree.
  Skips cleanly when the untracked artefacts are absent, and reports overlays it could *not* verify rather
  than counting them as passes.
* **Run time** — `board_scripts/dfx_preflight.sh` reads `REGISTER.USERCODE` off the live device and refuses
  the swap on mismatch. It also catches a foreign image flown by another lease holder, which is not
  hypothetical: it fired for real during this work.

## Rebuild rule

If you re-run `build_dfx.tcl`, it re-implements the static and stamps a new UserID. **The image and the
overlays must be taken from that same `prod/` directory**, and `board_image.tcl`'s `BIT_USERCODE` updated to
match. `make check` fails if they drift. Never pair partials from one `prod/` run with a bitstream from
another — that is exactly the bug this document exists to prevent.

`transplant_impl/shell_static_synth.dcp` remains the legitimate **input** to `build_dfx.tcl`. It is the
synthesis; only the implementation was duplicated. `boot_linux.tcl` boots `linux_soc_dbg.bit`, a genuinely
different pre-transplant design, and is correctly left alone.
