# The Linux harness — SSH in, console, recover a board

The MPS3's harness CPU is **Linux on a MicroBlaze V**; it replaced the bare-metal MicroBlaze
(`docs/planning/LINUX_HARNESS_PLAN_2026-09-23.md`). This page is for someone who has
never used it: how to get a shell on the board, how to see its console, and how to get a
board back when it will not boot.

> **Status (2026-10-01).** This is the fielded harness: static `0x44EE76D5` (mint 3 RC2),
> image rc2_v7 (`ad297bf`), `docs/FIELDED_SHELL.md`. What board 1 has proven:
>
> - **The fielding.** The config SD holds bit `f876e73e…` (stage0 build `0x34956F94`, with the
>   cold-start fix and the per-board identity). It passed 6/6 cold MCC boots from the card.
> - **The gate soak.** The 24 h accelerated soak on rc2_v7 said **PASS** (0 FAIL events in 24 h, ended 20:47 BST 30 Sep 2026).
> - Evidence: `docs/evidence/2026-09-linux-gate/`, and `docs/evidence/2026-09-linux-b2/` for
>   the 26-28 Sep runs and the 17.4 h netboot soak.
>
> **Board 2** runs the same static on an older stage0 (no cold-start fix, no identity) and
> netboots rc2_v7n. The bare-metal MicroBlaze image `0x72BB0A36` is the rollback (§5 step 6).
> Anything not yet proven on silicon is marked *(unproven)*.

---

## 1. What it is

```
power-on ─► MCC loads the FPGA from its SD card (the static + stage0 in the LMB)
         ─► stage0: DDR4 calibrated? ─► user µSD slot A ─► slot B ─► TFTP rescue
         ─► OpenSBI ─► Linux ─► mps3-harnessd + dropbear (ssh)
```

- **The wire does not change.** Every port the bare-metal shell served — 6900 control,
  69/6910 pushes, 6921 JTAG, 2542 XVC, 6930-6932 UARTs — is served by
  **`mps3-harnessd`**, which *is* the shell firmware's service code compiled for Linux.
  pyverify, fpgahub and socharness talk to it unchanged. The only visible difference is
  `version.impl == "linux"`:

  ```
  pyverify version --host 192.168.10.101     # ... "lmb_kb": 128, ..., "impl": "linux"
  ```
- **New:** a root shell over SSH, the UDP 6899 `identify` probe, a rescue path that
  needs no JTAG, and a watchdog that resets the board if the harness hangs.
- The board sits on the hub's private link: **192.168.10.101** (DHCP first; .101 is always
  added as a fallback). Every connection goes through the hub.

Throughout, `<hub-host>` is the lab hub (the same host `MPS3_HUB` names). No site host is
committed to this tree.

## 2. SSH in

**Key-only.** There is no password login over SSH, ever; the root password works only on
the serial console (§3).

### 2.1 One-time: the ssh config stanza

```
pyverify ssh --config-stanza >> ~/.ssh/config      # with MPS3_HUB set, it fills in the hub
```

which is:

```
Host mps3-linux
    HostName 192.168.10.101
    User root
    ProxyJump <hub-host>
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
    PreferredAuthentications publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    HostKeyAlias mps3-linux
    UserKnownHostsFile ~/.ssh/known_hosts_mps3_linux
    StrictHostKeyChecking ask
    ServerAliveInterval 5
    ServerAliveCountMax 3
```

The separate known-hosts file keeps the board's host key apart from whatever else has
answered on 192.168.10.101. Each board generates its host key on first boot and keeps it
on the µSD (`/persist`), so a *changed* key later means a re-imaged card or a different
board — stop and find out which before accepting it.

### 2.2 One-time: get your key onto the board

Either your key is **baked** into the image (lab images, `IMAGE_CONTRACT.md`), or you
**claim** an unclaimed board (trust on first use). Check first, from a host on the
board's link (the hub):

```
pyverify identify --host 192.168.10.101
```

`"ssh": {"claimed": false, "host_key_sha256": "SHA256:…"}` means nobody has claimed it.
Claim it with your public key (run on the hub, or on a PC cabled to the board):

```
pyverify claim --key ~/.ssh/id_ed25519.pub          # exit 0: accepted
```

A second claim is refused (TFTP error 2, exit 1). Write down `host_key_sha256` from
`identify` and check that ssh shows the same fingerprint on your first connect. To release
a claim, log in on the serial console (§3) and run `mps3-unclaim` (it reboots the board).

### 2.3 Every time

```
ssh mps3-linux                                  # or: pyverify ssh
ssh mps3-linux cat /etc/mps3/version            # one command
pyverify ssh --direct                           # no ssh config: -J $MPS3_HUB root@192.168.10.101
```

`pyverify ssh --print` shows the exact ssh command it would run.

**Leases.** SSH is a second way onto the board beside 6900/6910. Hold the lease before
changing anything (`pyverify lease acquire …`, `docs/BOARD_BRINGUP.md` "Step 1 — Lease
the board"). A root shell can
stop `mps3-harnessd`, and the board then answers 6900 with a RST — to every other user
that looks like a board that is off.

## 3. The serial console

The console is FPGA UART lane 2 → host port `/dev/<target>/tty_02` (`<target>` = `$MPS3_LEASE_TARGET`, default `mps3_pl`), 115200 8N1,
shared over TCP by fpgahub on the hub. It is the only way in when Linux is not up
(stage0 messages, a kernel panic) and the only place the root password works.

```
pyverify console                          # interactive; Ctrl-] quits
pyverify console --send 'uname -a'        # type one line, print what comes back (5 s)
pyverify console --print                  # just show the share endpoint + attach command
```

`pyverify console` reuses an existing share of `tty_02` or starts one
(`fpgahub share start <target> /dev/<target>/tty_02 --baud 115200`), then attaches
through the hub (`ssh -W`), so it works from your workstation with `MPS3_HUB` set, or on
the hub with `MPS3_ON_HUB=1`.

**Input is paced** at 20 ms per byte (`--pace-ms`): the UART's receive FIFO is 16 bytes
deep. **Never paste** into an unpaced terminal on this port — the bytes after the 16th are
lost.

## 4. Is it up?

| Question | Command | Healthy answer |
|---|---|---|
| Is the harness answering? | `pyverify ping --host 192.168.10.101` | `shell_id` = the fielded static |
| Which engine, which build? | `pyverify version --host …` | `"impl": "linux"`, `"lmb_kb": 128` |
| Up since when? | `pyverify stats --host …` | `up_ms` = harnessd's uptime, `os_up_ms` = the kernel's |
| What is at that address at all? | `pyverify identify --host …` | `"mode": "run"` (Linux) or `"rescue"` (stage0) |
| Is the harness wedged? | `pyverify diag --host …`; `pyverify mailbox` | `svc_skipped` 0 |

`up_ms` restarting while `os_up_ms` keeps counting means harnessd was **respawned** (init
restarts it immediately); both restarting means the **board rebooted**.

## 5. Recover a board

Work down the ladder; each step is automatic unless it says otherwise.

1. **harnessd crashed** → init respawns it at once (`inittab` `respawn`). 6900 RSTs for
   a moment (`offline` in fpgahub), then answers; `stats.up_ms` has restarted.
2. **harnessd or the kernel hangs** → harnessd stops kicking the shell watchdog and the
   WDOG resets the MicroBlaze V about 43 s after the last healthy kick (a two-stage AXI
   Timebase WDT: stage 21.5 s, reset ~42.9 s). stage0 re-runs and boots again (proven on
   silicon: the soak's WDOG trips, 2026-09-27).
3. **Slot A will not boot** → stage0 boots slot B (the status block counts the fallback).
   A slot handed off twice without Linux confirming it is skipped.
4. **Neither slot boots, or there is no card** → stage0 stays in **rescue**: it answers
   ping, `identify` with `"mode": "rescue"` and a `reason` (`no card`, `no valid slot`,
   `slots exhausted (unconfirmed boots)`, `ddr calib fail`, …), and a TFTP server on port
   69. Nothing answers 6900. Push a boot image into it:

   ```
   pyverify netboot --status                              # what stage0 is doing, and why
   pyverify netboot boot.img --via-hub                    # from your workstation
   pyverify netboot boot.img                              # on the hub itself
   ```

   `boot.img` is the same format as a µSD slot (`stage0_pack.py`). The exit code is
   stage0_push.py's: **0** accepted — stage0 verified it and is booting it; **1** bad
   local image; **2** rejected by stage0 (`--status` says why); **3** not stage0 or
   unreachable; **4** the transfer failed. A pushed image boots once; it is not written
   to the card. `--via-hub` copies the push tool and the image to the hub and runs it
   there (`--python` if the hub's default python is older than 3.6).
5. **DDR4 will not calibrate** → rescue with `reason: ddr calib fail`; every push is
   refused. This is hardware: power-cycle, and reseat the SODIMM if it persists (it needed
   a reseat in July).
6. **The Linux static itself is bad** → roll back to the bare-metal image `0x72BB0A36`:
   one SD write of its `.bit`, then a paced MCC `REBOOT` on `tty_00`, all from the hub. It
   takes about 2-3 minutes to a bare-metal `ping`, and the procedure is
   `docs/planning/ROLLBACK_RUNBOOK_LINUX.md`. One command does the write, the witness and the
   REBOOT: `pyverify sd field <bit> --expect-sha256 <sha>`.
   - The SD write's client always times out (12 MB over USB-MSC). That is **not** a failure:
     never write twice, and read the fpgahubd journal for `ok=True`.
   - The user µSD is not touched, so undoing the rollback is one more write and a REBOOT.
   - Remount `/persist` read-only first (`sync; mount -o remount,ro /persist`): the REBOOT
     cuts power.
   - The bare-metal mint and its overlays stay on the hub until the deletion PR.

## 6. Standalone: first install and recovery from a PC (no hub)

Everything above goes through the hub. A board can also be installed and recovered from
**one PC cabled to the harness Ethernet port**, with nothing else: no hub, no JTAG, no
card reader. This section uses only `pyverify` (run from `host/pyverify`, or with it on
`PYTHONPATH`), STAGE0's tools in `src/linux_soc/hw/fw_stage0/`, and `ssh`.

**What you need.**
- The board's MCC config SD already carries a **Linux static** (FLOW's `mcc_sd` target,
  `config_rm_greybox_stage0.bit`: the bitstream with stage0 inside). That one door is the
  MCC's, not the harness's Ethernet (FLOW_CONTRACT §0).
- A slot image for that static: `linux_slot.img` with its provenance beside it —
  `linux_bundle.json` from a mint's `prod/`, or IMAGE's `version` from
  `src/linux_harness/sw/artifacts/`. `pyverify slot push` reads which static the image
  was provisioned for from there and the board refuses any other.
- The PC on the board's subnet. The board is always at **192.168.10.101** (stage0's rescue
  address, and a permanent secondary address under Linux beside any DHCP lease):

  ```
  sudo ip addr add 192.168.10.2/24 dev <the PC's port>
  ```

### 6.1 What is at the address?

```
pyverify identify --host 192.168.10.101
```

| `mode` | means | next |
|---|---|---|
| `rescue` | stage0, nothing bootable: `reason` says why (`no card`, `card has no stage0 slots`, `no valid slot`, `slots exhausted (unconfirmed boots)`, `ddr calib fail`, …) | §6.2 |
| `run`, `"impl":"linux"` | Linux is up. `ssh.claimed` false = nobody holds it yet; `ssh.host_key_sha256` = the fingerprint your first ssh must show | §6.3 |
| `nohw` | Linux is up but `mps3-harnessd` found no shell blocks to map (not the static this image was built for) | fix the MCC SD's static |
| no reply, `pyverify ping` answers | a bare-metal harness (it has no 6899 yet) | `pyverify version --host 192.168.10.101` |

`identify` is unicast: ask 192.168.10.101 itself.

### 6.2 Rescue: boot Linux into RAM (stage0_push.py)

```
python3 src/linux_soc/hw/fw_stage0/stage0_push.py 192.168.10.101 --status   # what stage0 is doing
python3 src/linux_soc/hw/fw_stage0/stage0_push.py 192.168.10.101 linux_slot.img
```

(`pyverify netboot [--status] [linux_slot.img]` runs the same tool, with the same exit
codes: 0 accepted — stage0 verified the image and is booting it; 1 bad local image; 2
rejected by stage0; 3 not stage0 / unreachable; 4 the transfer failed.) The image boots
**once, from RAM**; nothing is written to the card. After ~20 s `identify` says
`"mode": "run"`.

### 6.3 Claim it, and ssh in

```
pyverify claim --host 192.168.10.101 --key ~/.ssh/id_ed25519.pub     # exit 0: accepted
```

The panel's row 12 then shows `SYS : linux  ssh claimed SHA256:<first 8>`, the
fingerprint of the key that claimed it (compare with `ssh-keygen -lf
~/.ssh/id_ed25519.pub`); before a claim it reads `ssh unclaimed`. A second claim is
refused (exit 1).

The claim lives in `/persist`. **Without a formatted card it is on tmpfs and lasts until
the next reboot**; claim again after one (§6.4 ends with the claim that stays).

For ssh, use §2.1's stanza **without its `ProxyJump` line** (the PC is on the board's link
itself), then `ssh mps3-linux`. Check that the fingerprint ssh shows on first connect is
`identify`'s `host_key_sha256`.

### 6.4 First install on a blank card

The card needs STAGE0's layout (STAGE0_CONTRACT §6) before anything can boot from it.
Start with the board running from RAM (§6.2) and claimed by you, with one ssh done
(§6.3). Run the three blocks below in order, from the repository root. `linux_slot.img`
stands for the image's path: keep its `version` or `linux_bundle.json` beside it,
because `pyverify slot push` reads the image's static from there.

**1. The layout.** Make it on the PC and write only its first three sectors through ssh,
then format `/persist` (partition 3):

```
python3 src/linux_soc/hw/fw_stage0/stage0_mkcard.py card --slot-a linux_slot.img --out-dir card
ssh mps3-linux 'dd of=/dev/mmcblk0 bs=512 count=1 conv=fsync' < card/mbr.bin
ssh mps3-linux 'dd of=/dev/mmcblk0 bs=512 seek=1 count=2 conv=fsync' < card/bootsel.bin
ssh mps3-linux 'partprobe /dev/mmcblk0 && mps3-persist format --erase'
```

**This destroys whatever the card held.** The running system does not change: `/persist`
stays on tmpfs until the reboot. Only the layout is on the card so far; both slots are
still empty (`stage0_mkcard.py` wrote `card/slotA.img` on the PC, not onto the card).

**2. The image.** Stage it, make it the default, reboot (§6.5 has the details):

```
pyverify slot push linux_slot.img --host 192.168.10.101     # "staged": "B"
pyverify slot commit --host 192.168.10.101                  # "default": "B"
pyverify reboot --host 192.168.10.101 --wait
```

The board runs from RAM and slot A is the default, so the push lands in **slot B**.
Because the board is claimed, `pyverify slot` goes through ssh by itself (§6.5). Slot A
stays empty until the next push (which lands in A). Until then there is no fallback
slot: if B never boots healthy, stage0 goes to rescue (§6.2).

After the reboot, stage0 boots slot B and `/persist` is on the card. The host key
generated on this boot is the one the board keeps (the RAM boot's was a throw-away one).

**3. Claim again and make the overlay store.** The RAM boot's claim was on tmpfs, so it
is gone. Claim again: this claim survives reboots. Then drop the old host key.

The MBR from block 1 made D13's overlay-store partition (p4, type `0xDA`), but nothing
has formatted it. Until something does, `pyverify usd status` says `foreign` and the
store takes no `commit`.

```
pyverify claim --host 192.168.10.101 --key ~/.ssh/id_ed25519.pub
ssh-keygen -f ~/.ssh/known_hosts_mps3_linux -R mps3-linux
pyverify usd format --host 192.168.10.101                   # "state": "empty"
```

On the next `ssh mps3-linux`, check the fingerprint it shows against `identify`'s
`host_key_sha256` (as in §6.3).

This whole section is rehearsed in QEMU by
`src/linux_harness/sw/rehearse_first_install_qemu.sh`. The script runs these blocks
verbatim, and it says which steps it has to emulate (stage0, and the fabric).

### 6.5 Stage an image into a slot over Ethernet (the `slot` verb)

```
pyverify slot status --host 192.168.10.101          # running / default / target / both slots
pyverify slot push linux_slot.img --host 192.168.10.101
pyverify slot commit --host 192.168.10.101          # the default becomes the pushed slot
pyverify reboot --host 192.168.10.101 --wait
```

**Claimed boards go through ssh.** An unclaimed board takes these on its raw ports
(6900/6910), from anyone. Once it is claimed (§6.3), `push`, `commit` and `rollback` from
the network are refused (`slot locked: board claimed (use ssh)`); only the board itself
may do them. `pyverify slot` handles that: it asks `identify`, and when the board says
`claimed` it opens an ssh tunnel through the `mps3-linux` alias (§2.1, without its
`ProxyJump` line here) to the board's own `127.0.0.1:6900/6910` and talks through that —
so your ssh key is the authentication. `--via-ssh` forces the tunnel, `--no-ssh` forbids
it, `--ssh-target` names another ssh alias. A TFTP push cannot use the tunnel (use the
default 6910). `status` and `verify` never need it. On the board itself, `mps3-slot`
does the same work (`ssh mps3-linux mps3-slot status`).

- `push` sends the image to 6910 (`--via tftp` for UDP 69) and waits for the board to
  write it **into the slot that is neither running nor the default**, then read it back
  off the card through stage0's own loader. Measured on silicon (26-29 Sep): ~12 min to push
  29 MB plus ~30-35 min of read-back (~70 s per MB), so ~41-50 min per slot. It prints the final status:
  `"staged": "B"`, and slot B `"verified": "readback"`. `--slot B` makes it refuse if B
  is not the target.
- Nothing about the running system changes until `commit`; nothing boots the new image
  until the reboot. If the new image never becomes healthy, stage0 goes back to the other
  slot by itself (try-once-then-confirm).
- If it boots but you want the old one back:

  ```
  pyverify slot verify --host 192.168.10.101          # reads the old slot back off the card
  pyverify slot rollback --host 192.168.10.101
  pyverify reboot --host 192.168.10.101 --wait
  ```

  A slot written outside the verb (by `stage0_mkcard.py`, or `mps3-slot write` over ssh)
  carries no slot record, so `verify` cannot bind it to this static; use `mps3-slot
  default A|B` over ssh for that one.
- Refusals are the board's, in `status`'s `job.err` or the command's stderr (exit 1):
  an image for another static (`image for 0x… != fabric 0x…`), `no free slot … --
  rollback first` (you committed and have not rebooted yet), `no card`, a corrupt image
  (`table CRC`, `region 0 CRC`, …). The full list: `docs/contracts/net-protocol.md`
  "Slot images". A bare-metal harness answers `slot not supported`.

### 6.6 Recovery from the PC

| The board… | Do |
|---|---|
| answers `identify` with `rescue` | §6.2 (boot a good image from RAM), then §6.5 to put it on the card, `commit`, reboot |
| runs, but the latest image misbehaves | §6.5's `verify` + `rollback` + reboot |
| runs, but its ssh claim belongs to a key you no longer have | `mps3-unclaim` on the serial console (§2.2, §3). The claim is on the card and nothing on the network releases it |
| answers nothing at all | the static on the MCC SD, or the board, is at fault: out of this page's reach (`docs/BOARD_BRINGUP.md`) |

## 7. Where the logs are

| What | Where |
|---|---|
| harnessd's console output | the serial console (§3) — init sends it there |
| the same, over the network | `pyverify log --host …` (a 4 KiB ring, non-destructive) |
| the kernel | `ssh mps3-linux dmesg` |
| the network setup | `ssh mps3-linux cat /run/mps3/net.state` |
| what stage0 decided at boot | `pyverify mailbox --what stage0` (Linux up), `pyverify netboot --status` (in rescue), or JTAG: `xsdb scripts/mps3_diag.tcl` *(unproven while Linux runs)* |
| the always-on counters | `pyverify diag` (6900), `pyverify mailbox --what diag` (ssh), `scripts/mps3_diag.tcl` (JTAG *(unproven while Linux runs)*) |
| board-window evidence | `docs/evidence/` |

**JTAG and Linux.** `xsdb scripts/mps3_diag.tcl` and the tier-3 gates read memory on the
*running* core and never halt it. Never `stop` the MicroBlaze V: it halts the whole of
Linux, and the watchdog then reboots the board under you. Reading the LMB (the diag
mailbox, stage0's status block) over JTAG while Linux runs is *(unproven)* (B1 item 8); when
it cannot, the script says `NOJTAG` and exits 2 — use `pyverify mailbox` instead.

## 8. Reference

| Port | Service |
|---|---|
| 22/tcp | ssh (dropbear, key-only) |
| 69/udp | TFTP: bitstream pushes; the `authorized_keys` claim; stage0's rescue push |
| 2542/tcp | XVC |
| 6899/udp | identify |
| 6900/tcp | control (JSON lines) |
| 6910/tcp | raw bitstream push; slot images (kind 2, §6.5) |
| 6921/tcp | JTAG `remote_bitbang` (OpenOCD) |
| 6930-6932/tcp | UART0 / UART1 / SWO |

| Variable | Meaning |
|---|---|
| `MPS3_HUB` | the hub host to jump through / run fpgahub on (no default) |
| `MPS3_ON_HUB=1` | you are on the hub already |
| `MPS3_HW_URL` | the hw_server for JTAG scripts |
| `MPS3_HARNESS_CPU=mb\|mbv` | which CPU the JTAG scripts look for (default: both, classic first) |
| `MPS3_JTAG_CABLE` | narrow JTAG scripts to the MPS3's cable when a hw_server has several boards |
| `MPS3_STAGE0_PUSH` | a copy of `stage0_push.py` to use for `pyverify netboot` |

The `slot` verb and the kind-2 push: `docs/contracts/net-protocol.md` "Slot images";
the socharness-facing summary: `docs/planning/linux_lanes/SLOT_VERB_DRAFT.md`.

Contracts: `docs/planning/linux_lanes/` (`HARNESSD_CONTRACT.md`, `STAGE0_CONTRACT.md`,
`IMAGE_CONTRACT.md`, `SHELL_CONTRACT.md`, `HOST_CONTRACT.md`).
