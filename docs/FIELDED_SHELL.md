# The fielded shell — the one place that says what is on the board

This file is the **single authority** for two facts that are easy to confuse and were,
until now, asserted independently in nine different files.

<!-- FIELDED_SHELL_TABLE (parsed by scripts/harness_gates/check_fielded_shell_claims.py) -->

| fact | value |
|---|---|
| `minted` | `0x72BB0A36` |
| `fielded` | `0x44EE76D5` |
| `fielded_on` | 2026-09-28 |
| `fielded_by` | ad297bf |
| `fielded_fw_flags` | `IMPL=linux CLCD=1 CLCD_KVM=1 TOUCH=1 HWICAP_FIFO=1 DUT_EGRESS=1` (fabric `SHELL_CPU=mbv SHELL_TOUCH=1`; `mps3-harnessd` `PRODUCT_FLAGS`, `src/linux_harness/sw/harnessd/Makefile`) |
| `lmb_kb` | `128` |

<!-- /FIELDED_SHELL_TABLE -->


> **`0x44EE76D5` is the fielded shell since 2026-10-01: the Linux harness.** The
> MicroBlaze V static of mint 3 RC2 (Vivado 2026.1, UserID `0xFB1F8C76`) runs
> `mps3-harnessd` under Linux. Board 1's config SD has carried its fielded stage0 bake
> since 2026-09-28 21:10: sha256 `f876e73e…`, stage0 build `0x34956F94`, with the
> cold-start fix and the identity publish. The board reported `shell_id 0x44ee76d5`
> from it at 21:15 (run mode). That is `fielded_on`. Its card has held image rc2_v7 in both slots
> since 2026-09-29. `fielded_by` names `ad297bf`, the commit whose content is in the
> image (`/etc/mps3/version`: `sha=ad297bf9 dirty=0`) and which contains the stage0
> commits `cd767ae` and `34956f9`. The cutover waited for the 24 h gate soak on
> board 1, which said **PASS** (0 FAIL events in 24 h, ended 20:47 BST 30 Sep 2026). **Board 2 runs the same static on an older
> stage0 bake** (`f206f788…`, build `0x6FAE6A0B`) until its identity bake can be
> fielded with someone on site. The static, and so this row, is the same.
> Per-board detail: `fielded/0x44EE76D5/README.md`. Evidence:
> `docs/evidence/2026-09-linux-gate/`, `docs/evidence/2026-09-linux-b2/`.
>
> The rollback image is the previous shell, bare-metal `0x72BB0A36`: one SD write
> and a paced REBOOT away (`docs/LINUX_HARNESS.md` §5 step 6). Its note follows, in
> the past tense it now belongs to.
>
> **`0x72BB0A36` was fielded 2026-09-24, remotely, and in two steps.** The ILA
> mint (built 2026-09-23 from `c855108`) went onto the card at 08:36 and was
> loaded at 08:40 by a paced MCC `REBOOT` on tty_00 — no power-cycle, nobody at
> the board. That image carried the mint's own firmware bake (`c8551081`).
> Firmware v0.11 was then trialled volatile over JTAG, passed, and was baked into
> the same base and written to the card at 09:40 (reloaded the same way).
> `fielded_by` names `987cf26`, the commit the v0.11 ELF was built from; the
> binary reports `987cf264` **dirty**, because it was built against the re-keyed
> overlay manifests that stage 5 regenerates and that were still uncommitted.
> Evidence: `docs/evidence/2026-09-w3/`.
>
> *The previous shell, `0x3F1A560F`, was fielded 2026-09-22; its note follows, in
> the past tense it now belongs to.*
>
> **`0x3F1A560F` was fielded 2026-09-22, and two things about it were worth stating plainly.**
>
> The SD card was written on **2026-09-16** and the board kept running
> `0xA8C1C535` for six days: the card holds the image, the management
> controller only loads it at power-on, and nothing in between makes that
> visible. "Written" is not "fielded", and only the board can say which.
>
> `fielded_by` names `54a48b4`, the commit that carries the `DUT_EGRESS` flag
> this firmware was built with. The binary itself reports `cb31b0f2` **dirty**,
> because it was baked from that commit with the flag change still uncommitted
> in the working tree. Both facts are true and neither is the whole one; the row
> names the commit whose content is in the image, and this note is here so the
> dirty sha in `version` does not look like a discrepancy in six months.

## A third fact: what the FIRMWARE was built with

`fielded_fw_flags` is the `make` flag set the fielded firmware was built with. It is
here because the fielded image is **two artifacts** — a bitstream and a MicroBlaze
firmware — and until now only the bitstream had an identity in this repo. A `static_id`
is a CRC of the *static routed design*; it says nothing whatever about the firmware
baked in beside it.

That gap shipped a **half-mint**: fabric built with the AXI IIC block, firmware built
*without* the touch driver. Every gate in the repo was green, because no gate knew what
the firmware was supposed to contain. Two artifacts, one identity, and the mismatch was
invisible.

`check_fielded_shell_claims.py` now **requires** this row and validates its shape (at
least three `NAME=VALUE` tokens; `unknown`/`TBD` is refused). It deliberately does *not*
pin the flag *set* — freezing today's configuration into a gate would make the next
legitimate mint fail here for being different rather than for being wrong.

Update it in the same edit as `fielded`: they describe one image.

## A fourth fact: how big the MicroBlaze local RAM is

`lmb_kb` is the fielded shell's local-memory (LMB) size in KiB. Since 2026-10-01 the
fielded shell is the MicroBlaze V (Linux) static `0x44EE76D5`, and the size is **128**. Three
places agree on it:
- `fpga/shell/bd/cpu_mbv.tcl` (the 128 KiB LMB that stage0 lives in);
- `src/linux_harness/sw/harnessd/Makefile` (`-DMPS3_LMB_KB=128`);
- `version.lmb_kb` on silicon (`docs/evidence/2026-09-linux-b2/soak_netboot/run4_20260927_151014/soak_accel_netboot_20260927_151014.summary.md`, W1 subset).

The rollback image, bare-metal `0x72BB0A36`, has 1024 KiB (`firmware/platform/Makefile:44`,
`LMB_KB ?= 1024`; `fpga/shell/bd/shell_bd.tcl:392`, `Write_Depth_A {262144}` × 4 B).

It is here for the same reason as the other rows — nothing in the repo can *derive*
what the flashed shell was built with — but it carries a sharper edge. The JTAG diag
mailbox is anchored to the **top** of the LMB, and since diag **v8** it is 256 bytes:

```
mailbox base = lmb_kb * 1024 - 0x100       # diag v8+, 256 B: 1024 KiB (bare metal) -> 0x000FFF00;
                                           #   128 KiB (Linux, MBV) -> 0x0001FF00 (stage0's block sits below, at 0x1FE00)
             = lmb_kb * 1024 - 0x80        # v5..v7 images only (128 B): a v7 bare-metal image -> 0x000FFF80
```

The fielded firmware (`987cf26`) is v8 (`firmware/common/diag.h`
`MPS3_DIAG_VERSION`; the growth landed in `3e49108`), so its mailbox is the 256 B one
at `0x000F_FF00`. `scripts/mps3_diag.tcl` scans both anchors for the magic word rather
than trusting either constant; do the same in any new tool.

A wrong belief about the LMB size points every `xsdb` read at the wrong address.
And the LMB address decode **aliases**: reading `0x000FFF00` on a 256 KiB shell wraps
back to `0x0003FF00` and returns the magic word from *there*. A stale size therefore
does not produce an error — it produces a plausible, wrong answer. That is bug #4.

`check_fielded_shell_claims.py` requires this row, validates it (a positive **power of
two** — the decode is a bit slice, so 768 KiB cannot be built and a row claiming it
would be fiction), and flags any tracked `.md`/`.txt` that states an LMB or BRAM size
for the *current* shell in the present tense and disagrees with it. `fpga/shell/README.md`
carried "Current build: 2026-07-07, 256 KiB LMB" for two mints after the board moved to
1 MiB, and no gate could see it.

History is untouched, as everywhere else here: "the then-current 256 KiB-LMB shell" is
true permanently and is left alone.

## The two facts are not the same fact

**`minted`** is what the overlay set is keyed to. It is generated, not written: `make -C
fpga/dfx overlays` computes it as the CRC-32 of `static_routed_locked.dcp` and writes it
into `fpga/dfx/overlay/mps3_shell_static_id.c` and every `overlay/<rm>/manifest.json` in
the same run. `check_overlay_static_id.py` (R9) holds those in lockstep.

**`fielded`** is what the board actually boots from its config SD. Nothing computes it.
It changes only when someone writes a bitstream to the SD and the MCC re-reads it, and
the only honest way to set it here is to have watched that happen.

Since 2026-10-01 they differ, and will until the deletion PR: `minted` is `0x72BB0A36` (the tracked `fpga/dfx/overlay/` set is bare metal's, the rollback image's), and `fielded` is `0x44EE76D5` (its 13 Linux overlays are built into `overlay_mbv/` and kept on the hub; FLOW_CONTRACT §1.1: an mbv build refuses `fpga/dfx/overlay`). **They are routinely not equal.** Between a mint and a deployment the
repo holds overlays keyed to a shell no board is running — which is a normal, correct
state, and precisely the state in which a claim like "the currently shipped shell is X"
becomes false without anything changing in the file that says it.

## Why this file exists

Before it, `0xCD74B6AE` appeared as "the currently shipped shell" in `docs/STATUS.md`,
the webharness README, `backend.py`, `catalog.py`, two webharness tests,
`socket_harness/endpoints.py`, `pyverify/debug.py`, the OpenOCD config and
`prog_shell_jtag.tcl`. Every one was true when written. All of them became false at once
on 2026-08-10, when `0xA8C1C535` was fielded — and none of them changed, because a
sentence in a README has no gate.

That is the same failure as the stale overlay it sat beside: `nanosoc_upy` stayed keyed
to `0xD84A2E7A` across two mints because the gate that would have caught it never ran
(fixed `3c9703d`, `6b0f870`). A fact asserted in nine places is a fact that will be wrong
in eight of them.

`check_fielded_shell_claims.py` now gates it: any tracked file that asserts a static_id
in a *present-tense fielded* context must agree with the table above.

## History is not a claim, and is not gated

Statements about what a past mint did — "the A6 SWD→JTAG cutover landed as `0xCD74B6AE`",
`firmware/jtag_server/jtag_server.h`, `shell_bd.tcl`, the planning docs — are true
permanently and must not be rewritten. Updating them would erase the record of which mint
introduced what. The gate matches on present-tense phrasing precisely so that it leaves
history alone.

## Deliberately on another static

`fpga/dfx/overlay_linux/` is on `0x2B082E1B` with its own `static_usercode`
(`0x5263642C`), binding it to a separate static *implementation* — see
`docs/PLATFORM_ONE_IMPL.md`. R9 does not scan it and neither does this gate. An overlay
that IS scanned has exactly two honest states: keyed to the current shell, or explicitly
exempted with a recorded reason.

## Updating this file

Change `fielded` **only after** watching the board come up on the new shell. The evidence
that qualifies is the shell reporting its own id — `ping.shell_id` over the relay, or the
CLCD status line — not a successful SD write. `sd_install` timing out is normal and is
not evidence of anything (`docs/` runbook; the write takes ~5 min over USB-MSC).
