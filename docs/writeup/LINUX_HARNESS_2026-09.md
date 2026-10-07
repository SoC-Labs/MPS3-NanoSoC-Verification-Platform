# The MPS3 Linux harness (2026-09): write-up skeleton

**Status:** skeleton. Facts are checked against lx `56ac447` (2026-09-24). The prose is the project lead's.
Placeholders: **TBD (B1)**, **TBD (B2)**, **TBD (soak)**. "(unverified)" means no source was found.

**Citation keys:**
- `plan` = `docs/planning/LINUX_HARNESS_PLAN_2026-09-23.md`.
- `SHELL`, `STAGE0`, `FLOW`, `HARNESSD` = `docs/planning/linux_lanes/<X>_CONTRACT.md`.
- `study`, `B1`, `lint` = main tree `docs/planning/{LINUX_SPEEDUP_2026-09-24, B1_RUNBOOK_LINUX, RUNBOOK_LINT_2026-09-24}.md` (untracked).
- A 7-hex id is a commit on `feat/linux-harness`. `-ila` marks `feat/rm-ila-mint`.

---

## 1. Problem and principle

- **Fielded harness:** classic MicroBlaze v11, no MMU, no DDR. It runs bare-metal C in one polled superloop (`firmware/platform/src/main.c:337`), built with Vivado/Vitis 2024.1 [plan §1.1].
- **Bare-metal limits** (the project lead: extend):
  - No SSH, and every service shares one loop. On 09-22 a held touch stretched one superloop pass to 149,536 µs, which starved lwIP (memory note `touch-works-and-starves-the-superloop`; not in the repo).
  - 19% of the 1 MiB LMB is free [56ac447].
  - The firmware is baked into the bitstream, so a persistent update is an SD write + MCC REBOOT [-ila ecf5ee0].
- **July precedent:** MicroBlaze V Linux ran on this board (static `0x2B082E1B`): boot, eth0, SSH, 6900, 6/6 real ICAP swaps, 30/30 live wire conformance. It was parked on 09-16 (CLOSEOUT D2) [plan §1.2].
- **Goal:** SSH in, unattended power-on boot, the same wire contract, and the bare-metal platform layer retired [plan].
- **Principle:** *same wire, same service code, different engine.*
  - Clients cannot tell the engines apart, except through the additive `version.impl`.
  - The services are the existing firmware C, compiled for Linux, not ported.
- **Why it was feasible:** service modules touch hardware only through `mps3_reg_*32`, and the network only through `net_if.h`, which already had a POSIX backend [plan §1.6].

## 2. Architecture

### 2.1 Boot and runtime [plan §3]

```
power-on
  MCC ── config SD: nanosoc.bit (MBV static, stage0 baked into the 128 KiB LMB by updatemem)
  stage0 (BRAM, bare-metal rv32)
    1. DDR4 calibrated?        no  → console + status block "ddr calib fail", stay in rescue
    2. user µSD slot A  (CRC)  ok  → copy blob to 0x8000_0000, fence.i, jump OpenSBI
    3. user µSD slot B  (CRC)  ok  → same (boot counter records the fallback)
    4. rescue: TFTP-WRQ server on UDP 69 → CRC → jump
  OpenSBI 1.6 → Linux 6.18.7 (rv32imac, Sv32) → BusyBox init (respawn)
    kernel: uartlite, intc, axi_timer, LAN9220 (eth0), DDR, usd_spi (mmc_spi → mmcblk0)
    mps3-harnessd (ONE process = the firmware services, MPS3_HAL_UIO + posix net_if):
      :6900 coordinator  :69/:6910 config_agent  :6921 jtag_server  :2542 xvc_server
      :6930-2 uart_over_eth  clcd + clcd_kvm + touch  clkrst  swap_fsm (HWICAP via UIO)
      diag mailbox (LMB tail via UIO)  heartbeat  WDOG kick
    /persist (ext4 on µSD p3): host key, authorized_keys, /etc/mps3, clearing cache
  ssh -J <hub-host> root@192.168.10.101   (DHCP first, static .101 secondary)
```

### 2.2 LMB map (128 KiB at `0x0`) [plan §3, STAGE0 §3]

| Range | Use | Owner |
|---|---|---|
| `0x00000–0x1FDFF` | stage0 image + 8 KiB stack | stage0 |
| `0x1FE00–0x1FEFF` | stage0 status block. Linux writes only `att_confirm` (`0x4B4F3053` at `0x1FE48`) | stage0 |
| `0x1FF00–0x1FFFF` | diag mailbox (v8 in the plan; v9 after D13, with `usd_boot` at `0x1FFB4` [36506d5]) | harnessd |

### 2.3 Recovery ladder [plan §3]

1. Slot A fails → slot B.
2. No card, or both slots bad → TFTP rescue.
3. harnessd or the kernel hangs → WDOG hardware reset.
4. The Linux static is bad → one SD write of the bare-metal `.bit` + MCC REBOOT.

### 2.4 User µSD layout [STAGE0 §6]

| Part | Type | Start LBA | Size | Use |
|---|---|---|---|---|
| p4 | `0xDA` | 2048 | 32 MiB | D13 overlay store |
| p1 | `0x7F` | 67584 | 64 MiB | stage0 slot A |
| p2 | `0x7F` | 198656 | 64 MiB | stage0 slot B |
| p3 | `0x83` | 329728 | rest | `/persist` ext4 |

### 2.5 Block ownership [SHELL §2, HARNESSD §3]

Each block has one owner. The DTS is generated from the owner column: kernel-owned blocks get drivers, harnessd-owned blocks get `generic-uio` [eadc72c].

| Owner | Blocks |
|---|---|
| kernel | uartlite `0x4060`, INTC `0x4120`, AXI timer `0x41C0`, LAN9220 `0xC000`, DDR4, `mdm_riscv`, `usd_spi` `0x44A4` |
| harnessd (UIO) | CLKRST, DFXCTL, HWICAP, VPHY, GENCHK, JTAGBB, DBGBR, UARTBR, GPIO, MMCM_DRP, CLCD, CLCDKVM, TOUCH, DUTEGR, USRACC, WDOG, LMB tail `0x1F000` |
| neither | TELEM `0x44A5` (STATUS[0] = DDR calibration, read by stage0) |

## 3. Decisions

| # | Decision | Why |
|---|---|---|
| DL1 | Un-park the Linux fork | July proved it on silicon |
| DL2 | One shell BD with a CPU seam (`SHELL_CPU=mb\|mbv`) | Drift becomes impossible by construction |
| DL3 | Mint 3 = the Linux static, gated on B1 | Saves a mint. NO-GO → bare-metal 2024.1, Linux moves to mint 4 |
| DL4 | Freeze the bare-metal *platform* at v0.11 | Service-module features reach both engines |
| DL5 | Key-only root SSH via `ssh -J <hub-host>` (amended by S8) | Security |
| DL6 | `mps3-harnessd` replaces the v0.7 daemons and `mps3_dfx.ko` | Parity by construction; ~6 agent-days saved |
| S1 | `ping.shell_id` is fabric-bound (baked into stage0); a mismatch refuses swaps | The card cannot lie about the loaded static |
| S2 | 6910 takes a plain push | Kernel TCP paces it |
| S3 | A respawn never boot-loads or swaps | A crash cannot reconfigure the DUT |
| S4 | Keys that Linux cannot fill are omitted, not zeroed | Clients can tell absent from zero |
| S5 | Try-once-then-confirm, with counters in NOINIT | A bad image cannot brick the board |
| S6 | Rescue = static .101 with ICMP, TFTP and identify only | Stage0 stays small |
| S7 | `.101` is a permanent secondary address | The hub's known address always works |
| S8 | Host keys are generated per board at first boot | No host key is shared between boards |
| S9 | Buildroot legal-info ships with each release | GPL |
| S10 | A slot push kind + a `slot` verb | Ethernet-only install |
| S11 | fpgahub adds TCP 22 | The lease must cover SSH |
| S12 | A claimed board refuses slot mutations from non-loopback peers | Otherwise anyone on the LAN could replace the OS |

## 4. Method

### 4.1 The CPU seam [6e5c546, SHELL §1]
- `shell_bd.tcl` sources `cpu_$SHELL_CPU.tcl` at fixed stages. Every other block is shared Tcl.
- `mb` is today's CPU, moved verbatim. `mbv` is MicroBlaze V + 128 KiB LMB + DDR4 MIG, and needs Vivado 2026.1.
- The MBV-only deltas are tagged SEAM-1..8:
  - DDR calibration at TELEM STATUS[0];
  - the WDOG restarts the MBV (first expiry 21.47 s, reset 42.95 s after arming);
  - debug memory access while the hart runs;
  - the MIG debug hub (SEAM-8) [d8b29e8].

### 4.2 The services, compiled unmodified [a1f5b6e, HARNESSD]
- The seams:
  - `MPS3_HAL_UIO` maps a physical base to a UIO mmap, and aborts loudly on an unknown base;
  - `posix_net_if`;
  - a Linux timebase.
- `main_linux.c` runs the same service table with a `poll()` idle.
- It kicks the WDOG early (logging `FIRST KICK`), and confirms the boot to stage0 only once healthy.
- The additive wire: `version.impl`, omit masks, identify on UDP 6899, and the TOFU key claim.

### 4.3 stage0 [3ba84bb, STAGE0]
- Boot order: µSD slot A, then slot B, then rescue. Stage0 never writes the card.
- Try-once-then-confirm uses N=2, and the counters survive a WDOG reset.
- The TFTP rescue needs no lwIP and is 13.7 KiB. smsc911x is reused unedited.
- static_id and ver32 are compiled in and bound to the bitstream by updatemem.
- The WDOG is armed before every jump.

### 4.4 The Buildroot image [eadc72c, 30c07f4]
- The DTS is generated. `spi-usd` is a new driver.
- `/persist` is found by position and type, and is never auto-formatted.
- First-boot host keys; DHCP + `.101`; respawn; a boot-health marker.
- Kernel patches 0003 and 0007 are carried.

### 4.5 The two-target release bundle [4affc9b, FLOW §0]
- `mcc_sd`: the stage0-baked base `.bit`, installed by SD write + MCC REBOOT.
- `ethernet`: `linux_slot.img` + overlays + manifest + legal-info, installed by a rescue push or from Linux.
- Both must name one static.

### 4.6 The slot verb and the claim lock [2c7e05d, 92f4dc0, 9e6776b]
- `slot status|commit|rollback|verify`, plus 6910 push kind 2 (net-protocol v0.14). Bare metal declines.
- A push never writes the running or default slot, and is read back through stage0's loader.
- The claim lock (S12) is checked per request.
- One card layer (`slot_card.[ch]`) is shared by harnessd and `mps3-slot`.

### 4.7 D13 integration [36506d5, 56ac447]
- `usd_spi` replaces the pad-less `axi_quad_spi_0` for both CPUs.
- The overlay store lives on the µSD on both engines (v0.13).
- A card-sharing e2e test hashes every region around every write.
- The P-mint predates D13, so B1 item 6 moves to RC1/B2.

### 4.8 Parallelisation [plan §4, `linux_lanes/`]
- L0-IMAGE; six L1 lanes (SHELL, FLOW, STAGE0, IMAGE, HARNESSD, HOST); then L2-STANDALONE and INTEG.
- Every lane had a written contract and an exclusive write scope.
- Agents never commit. The lead landed one lane at a time, after `make check-ci` passed in a detached worktree.
- Contracts came first: the UIO hunk, the ownership list, and the MBV regmap.
- **Speed:** L1 was planned for 09-23 → 10-01, and all its commits are dated 09-23. The P-mint was planned for 10-01 and finished 09-24 02:46 [FLOW §6.3].

## 5. Results so far

| Result | Number | Source |
|---|---|---|
| RMs under 2026.1 | 13/13 OOC PASS in 32 min; no RM needed a fix | 07fba51, FLOW App. A |
| CPU seam | `mb` dump identical (14,063 lines); `mbv` validates with 0 critical warnings | 6e5c546 |
| MBV static, flat | WNS +0.322 / WHS +0.026; DDR ui_clk +0.322 (target +0.05, July +0.095) | 52f06f9 |
| P-mint `0x61BC6789` timing | WNS/WHS: greybox and led +0.262/+0.030; dbg_demo +0.287/+0.013; nanosoc +0.287/+0.030. ui_clk +0.287 | FLOW §6.3 |
| P-mint checks | pr_verify compatible; stage0 96,576 B free below `0x1FE00`; ~5 h over 3 runs | FLOW §6.3, 6925e7c |
| QEMU proof | 31/31; 31/31 again when provisioned for the P-mint | 30c07f4, d61eee0 |
| Conformance across engines | one table against ctrl_echo, FakeShell and harnessd: 83/83. 5,100 codec responses byte-identical; 121 with D13 | 931a4fc, a1f5b6e, 56ac447 |
| Gates | `make check-ci` OK (1,373 tests); firmware 68/68; harnessd 77; seam, stage0 and DTS gates PASS | 56ac447 |
| Bare-metal ELF (INTEG) | text 225,880 B; 19% of the LMB free; stack 6,328/8,192 B | 56ac447 |
| W1 (read-only) | `0x72BB0A36` fielded remotely; 13/13 overlays; first USR_ACCESS readback | -ila ecf5ee0 |

## 6. Bugs found and fixed

| Defect | Found by | Commit |
|---|---|---|
| Fork BD: `dfx_ctl` POR/WDOG inputs left unconnected, so the decoupler clamp could never assert | audit + clamp bench | 6e5c546 |
| The v0.7 daemons were 6 verbs and 28 diag keys behind v0.11 | parity audit | a1f5b6e, 931a4fc |
| July: `rdtime` froze in a busy-spin, so every `udelay` hung | JTAG on silicon | 95ff74d (patch 0007) |
| The July blob had no `mps3_dfx.ko`, so it could not swap | L0-IMAGE | uncommitted (`-linux` worktree) |
| The S0LB v1 header CRC did not cover the table | STAGE0 | 3ba84bb |
| SEAM-8: the MIG's debug slave gives a DFX `opt_design` ERROR | first P-mint run | d8b29e8, 66a54f3 |
| `mint-linux-image` re-synthesised the P-mint's shell | lead | 2cefd72 |
| `mps3-slot` verified against the page cache, not the card | L2-STANDALONE | cc399ef |
| `mps3-slot` could overwrite stage0's boot-select copy on a tie | L2-STANDALONE | cc399ef |
| `udhcpc.script` was mode 0644, so DHCP never ran | QEMU proof | 30c07f4 |
| `blkid` in BusyBox cannot print TYPE, so a formatted p3 looked blank | QEMU proof | 30c07f4 |
| identify reported harness `0.0.0` in NO-HW mode | QEMU proof | f7d23e4 |
| `SHELL_TOUCH=1` overwrote the REALPHY define | SHELL | 6e5c546 |
| P-mint flow: stale shell deps; a re-bake on every re-run; a wrong equal-length bake rule | P-mint | 6925e7c |
| Buildroot shipped the previous harnessd | IMAGE | d61eee0 |

## 7. Evidence placeholders

> **Evidence rows added 2026-09-24:** the D13 store commit and power-on load goes in `docs/evidence/2026-09-linux-b2/b2_usd_commit.txt` (B2 step 5d). `set_clk` goes in `soak_accel.summary.md` §"set_clk" on RC1 and in `b1_g_setclk.txt` on the P-mint. Sweep, W1 subset, slot-A fallback, WDOG and boot rate go in `soak_accel.summary.md` (B2 was cut to ~1 h 40; see the B2 runbook's row→file table).

### 7.1 B1 (P-mint, volatile): main tree `docs/evidence/2026-09-linux-b1/` [B1]

| Step | Proves (plan item) | File | Result |
|---|---|---|---|
| 0 | console | `b1_console_tty02.log` | **TBD (B1)** |
| a | program + preflight | `b1_a_program.txt` | **TBD (B1)** |
| b | stage0, `DDR4 calib=1`, rescue (1) | `b1_b_stage0.txt` | **TBD (B1)** |
| c | identify `mode:rescue` (1) | `b1_c_identify.txt` | **TBD (B1)** |
| d | push → Linux, times (1) | `b1_d_netboot.txt` | **TBD (B1)** |
| e | SSH, claim, `impl:"linux"` (1, 9) | `b1_e_ssh.txt` | **TBD (B1)** |
| f | four swaps (2, 9) | `b1_f_swaps.txt` | **TBD (B1)** |
| g | OpenOCD halts the M0 (3) | `b1_g_openocd.txt` | **TBD (B1)** |
| g2 | UART, CLCD, touch, KVM (5) | `b1_g2_item5.txt`, `b1_g2_clcd_{status,apps}.jpg` | **TBD (B1)** |
| h | XVC ILA + MIG view (4) | `b1_h_xvc.txt`, `b1_h_ila_dbg_demo.ila` | **TBD (B1)** |
| i | mailbox, no halt (8) | `b1_i_mailbox.txt` | **TBD (B1)** |
| j | WDOG reset (7) | `b1_j_wdog.txt` | **TBD (B1)** |
| k | fallback, if used | `b1_k_failure.txt` | **TBD (B1)** |
| — | GO/NO-GO | `README.md` | **TBD (B1)** |

### 7.2 B2 (RC1, SD-fielded)

There is no B2 runbook yet, so these file names are proposals, under `docs/evidence/2026-09-linux-b2/`.

| Check | File | Result |
|---|---|---|
| SD write + first power-on boot | `b2_sd_write.txt` | **TBD (B2)** |
| Card set-up; card detect in/out | `b2_card.txt` | **TBD (B2)** |
| Item 6: µSD init, `/persist`, boot from slot A | `b2_usd_boot.txt` | **TBD (B2)** |
| Drills: slot A → B, no card → rescue, WDOG | `b2_drills.txt` | **TBD (B2)** |
| Rollback to `0x72BB0A36`, once | `b2_rollback.txt` | **TBD (B2)** |
| 13-RM sweep + W1 subset | `b2_sweep.txt` | **TBD (B2)** |
| Parity table signed off | `README.md` | **TBD (B2)** |

### 7.3 Soak (proposed: `docs/evidence/2026-09-linux-soak/`)

| Item | Result |
|---|---|
| Length: 24 h accelerated or 72 h (the project lead's call on plan §5.4) | **TBD (soak)** |
| Swaps, logins, boots, WDOG drills | **TBD (soak)** |
| Unexplained resets and oopses (pass = 0) | **TBD (soak)** |
| Memory and open-file trend (pass = flat) | **TBD (soak)** |
| Slowest 1% of swap times (pass = stable) | **TBD (soak)** |

### 7.4 Boot rate

| Item | Result |
|---|---|
| Unattended power-on → SSH, k/10 | **TBD (B2)** |
| 95% Clopper–Pearson interval | **TBD (B2)**. For reference, 10/10 gives [69.2%, 100%] |
| Bare-metal reference | 10/10 cold boots on 09-23 [plan §1.7] |

## 8. Numbers to be measured

| Number | Where | Reference | Value |
|---|---|---|---|
| push → 6900 (`push_to_6900_ms − push_ms`) | B1 (d) | July: 18 s [plan §1.7] | **TBD (B1)** |
| `FIRST KICK` `os_up_ms` | B1 (d) | must be < 42,900 | **TBD (B1)** |
| WDOG stop → rescue | B1 (j) | reset 21.5–42.9 s after the STOP | **TBD (B1)** |
| MIG calibration + margins | B1 (h) | first view over XVC | **TBD (B1)** |
| swap time | B1 (f) | bare-metal W1: 3–7 s per push [-ila ecf5ee0] | **TBD (B1)** |
| SSH latency during a swap | B1 (f) | a gap > 2 s = starving | **TBD (B1)** |
| power-on → 6900 from µSD | B2 | ~100 s (estimate) [study §5] | **TBD (B2)** |
| boot rate | B2 | §7.4 | **TBD (B2)** |

## 9. Lessons

**Design**
- One source beats a synchronised copy: both the fork BD and the hand-ported daemons drifted [plan §1.4–1.5].
- A guard keyed on a declared type misses anything undeclared. A bench that asserts the behaviour catches it [6e5c546].
- Share the rule, not a copy: `mps3-slot` links stage0's own loader [cc399ef].

**Flow**
- Flat is not DFX: SEAM-8 was a warning in the flat flow and an error under DFX [d8b29e8].
- A post-mint target with prerequisites can rebuild the mint [2cefd72].
- Do not gate on a log line that may never print (`MINT COMPLETE`) [07fba51].
- Preserve a static the moment it exists. The flat hub copy overwrote the only off-host copy [-ila 987cf26].

**Board operations**
- tty_00 vs tty_01: a REBOOT sent to tty_01 looked like a no-op, and tooling still assumes that [study §5]. A remote reload needs one reader on tty_00 [-ila ecf5ee0].
- Verify on the card, not in the page cache [cc399ef].
- Board-free first: the image build and QEMU found six defects [30c07f4, f7d23e4], and the lint found five runbook errors [lint].

## 10. What's left

1. **B1:** GO/NO-GO. **TBD (B1)**
2. **RC1** (mint 3, fired 09-24 09:24, 13 RMs) → B2 → soak → cutover. The cutover date is **TBD (soak)**.
3. **Mint 4:** the FOLD D-items (D11, D6, D8, D10) and the DUT-inject fabric [study #4].
4. **The deletion PR** (~10-26): the bare-metal platform layer, the v0.7 daemons, `drivers/icap/` and the fork BD [plan §4].
5. **Signed images** (minisign) on top of the claim lock [plan S12].
