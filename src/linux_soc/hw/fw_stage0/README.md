# fw_stage0 — MicroBlaze-V first-stage loader for the Linux harness

Stage0 runs from the 128 KiB LMB at `0x0`. It starts on every FPGA configuration
and on every watchdog warm restart; FLOW bakes it with `updatemem` (`mint-stage0`).
It removes the workstation from the boot path:

```
DDR4 calibrated? -> default uSD slot -> other slot -> RESCUE (TFTP push over Ethernet)
hand-off: regions copied to DDR + CRC-verified from DDR, D-cache writeback, fence.i, jump OpenSBI
```

**The normative interface is `docs/planning/linux_lanes/STAGE0_CONTRACT.md`.** It covers:
- the build knobs FLOW sets;
- the status block at `0x1FE00` (HARNESSD, HOST);
- try-once-then-confirm;
- the card layout and boot-select sector (IMAGE, D13);
- the rescue protocols (HOST, socharness).

This README is the map of the code.

## Status — what is proven, what is not

| Part | State |
|---|---|
| Core: image parse / table CRC / region copy (`stage0_core.c`), format **v2**: `header_crc32` covers header + entries, so it binds every payload CRC and length and is the image's identity | host-tested (`test_stage0_core`, `test_stage0_edge`: every entry-field flip refused, two payloads give two ids, a swapped payload refused, v1 refused) |
| Boot order, status block, try-once-then-confirm, boot-select (`stage0_flow.c`) | host-tested (`test_stage0_flow`, 133 checks, mutation-checked) |
| µSD backend: blocking wrapper over D13's `usd.c` (`stage0_sd.c`) | host-tested against D13's real driver + card fake (`test_stage0_sd`); **read-only proven** (no CMD24/25, no block written) |
| Rescue network: ARP / IPv4 / ICMP / UDP (`stage0_net.c`), TFTP WRQ + status RRQ (`stage0_tftp.c`), identify 6899 (`stage0_ident.c`) | host-tested at frame level (`test_stage0_net`); end to end with `stage0_push.py` over localhost UDP (`test_tools.py`) |
| LAN9220 glue over the **unmodified** `firmware/smsc911x/` (`stage0_eth.c`, `stage0_spin.c`) | host-tested against `fake_lan9220.c` (`test_stage0_eth`) |
| Target build fits: ≥ 48 KiB free below the stack, net stack ≤ 24 KiB, no `.data`, no card-write path | `make size-gate`, with five negative controls. Today (2026-09-28, S0-COLDFIX): image ends 0x97E8 (38.0 KiB), **81.5 KiB free**, net stack 13.6 KiB |
| FLOW's bake guard reads the fabric identity out of the ELF | verified: `stage0_bake.py elf --expect-static-id/--expect-ver32` accepts a match and refuses a mismatch |
| DDR gate (TELEM `CTRL=0x2` on every entry, STATUS[0] must HOLD `CALIB_HOLD_MS`), cold settle, watchdog arm-fresh/kick/WRS (`stage0_hw.c`) | host-tested register by register against an HDL-faithful WDT model and a glitchy TELEM model (`test_stage0_hw`, mutation-checked) |
| Cold-boot hardening (S0-COLDFIX, 2026-09-28): watchdog armed at entry, 2 pre-hand-off restarts -> rescue, cold settle, calib hold + re-check per read op / CRC chunk, slot time bound, `subphase`/`entry`/`prev_*`/`ddr_ok_ms` | host-tested end to end over the models (`test_stage0_entry`) + the flow (`test_stage0_flow`), mutation-checked. **NOT silicon-validated**: the cold-boot plan (N MCC power cycles per board) is the proof |
| The board identity (lane IDENT, 2026-09-28): `S0_IP` / `MPS3_MAC0..5` / `S0_LABEL` published in the status block at EVERY entry (`ip_addr`, `mac_*`, `label_lo/hi`; STAGE0_CONTRACT §3.3), per-board bakes via `S0_LABEL=` + `-DS0_IP/-DMPS3_MACn` or `S0_BOARD=<name>` (`boards/`, `stage0_board.py`) | host-tested (`test_stage0_ident`: board 2's bytes at their offsets; `test/check_ident_order.py`: published before the WDOG arm and the boot order, with a negative control); `make size-gate` negative control 6 (a bad or doubled identity fails the build) |
| UART, AXI timer, cache writeback + `fence.i` + jump (`stage0.c`) | written from SHELL_CONTRACT + the July bootstub/`dfx_swap_boot` recipe. **NOT silicon-validated** (B1 item 1) |

## Files

| File | What |
|---|---|
| `stage0_boot.h` / `stage0_core.c` | Image format, MBR slot parse, boot-select sector, CRC-32 (table built at run time into `.bss`), `s0_load` |
| `stage0_status.h` | The status block: an X-macro, so the struct, offset asserts and host name table come from one list |
| `stage0_flow.h` / `.c` | Boot order and the status-block life cycle, over an ops table (target or host) |
| `stage0_sd.h` / `.c` | `s0_usd_init` / `s0_usd_read_blocks`: D13's `usd.c` polled to a verdict |
| `stage0_net.*`, `stage0_tftp.*`, `stage0_ident.*`, `stage0_rescue.*` | The rescue server |
| `stage0_eth.c`, `stage0_spin.c` | Frame I/O over `smsc911x.c`; the `mps3_spin_until` body it needs |
| `stage0_hw.h` / `.c` | DDR calibration gate and watchdog arm/kick, via the `platform_regs.h` HAL |
| `stage0.c`, `crt0.S`, `stage0.ld`, `stage0_libc.c` | Target: `main`, platform bodies, startup, LMB layout (stack top `0x1FE00`, `.data` forbidden), `mem*` |
| `check_budget.py` | The size gate: map file + ELF symbols |
| `stage0_pack.py` | Pack payloads into an image; `--check` verifies one exactly as stage0 will |
| `stage0_push.py` | Rescue push, or `--status` read, from the hub or any PC |
| `stage0_mkcard.py` | Card layout: `card` / `bootsel` / `check` |
| `stage0_status.py` | Reference decoder for the status block |
| `mk_fw_payload.sh` | OpenSBI FW_PAYLOAD wrap; IMAGE's `build.sh` calls it to make `fw_payload_1region.bin` |
| `mk_1region.sh` | **Retired**: refuses to run. `build.sh` is the one recipe for the 1-region blob |
| `gen_coe.py` | A `.coe` for BD-time BRAM init |
| `test/` | Host tests (below) |

## Build / test

```
make                         # stage0.elf/.bin/.lst/.map/.coe + the budget report
make stage0.elf MPS3_STATIC_ID=0x... MPS3_HARNESS_VER32=0x...   # what mint-stage0 runs
make size-gate               # budget gate + negative controls (needs the rv32 toolchain)
make -C test                 # every host test (host gcc + python3; board-free, ~40 s)
```

- `USD_SRC` selects D13's driver. It is in-tree `firmware/usd` once D13 lands; until
  then it is the usd worktree, read-only.
- If neither exists, `test_stage0_sd` is skipped with a loud message.
- `CALIB=none` builds for a shell with no calibration bit.
- `BOOT_LIMIT=n` changes try-once-then-confirm's N (default 2).
- `WDOG_ARM` (default 1) arms the watchdog at stage0 entry (kicked from every loop)
  and re-arms it fresh before every jump: Linux then has 42.9 s to get harnessd
  kicking, or the board resets and the attempt is counted. A stage0 that stalls twice
  in a row before a hand-off goes to rescue. `WDOG_ARM=0` is for a bench only; the
  size gate refuses it. NOTE: with the watchdog armed, a debugger holding the hart
  halted for more than ~21 s gets the board reset.
- `COLD_SETTLE_MS` (default 10000) and `CALIB_HOLD_MS` (default 1000): the cold-entry
  settle and the DDR calib hold (stage0_flow.h / stage0_hw.h).

## Board bring-up ladder (cheapest first)

1. **Rescue push (B1 item 1).**
   - Config with no card in the user slot. The console shows
     `stage0: RESCUE (no card)`.
   - `ping 192.168.10.101` answers, and `stage0_push.py 192.168.10.101 --status` decodes
     the block.
   - `stage0_push.py 192.168.10.101 boot.img` should reach OpenSBI then Linux.
   - This proves the DDR gate, the hand-off, cache coherence and the network with no SD
     driver in the path.
2. **µSD slot A (B1 item 6).**
   - Write a card with `stage0_mkcard.py card`, insert it, and reconfigure.
   - `booted_from=1`.
   - Corrupt slot A and reconfigure: `booted_from=2`, `n_fallback=1`.
3. **Try-once-then-confirm (B1 item 7).**
   - `kill -STOP` harnessd before it confirms. The WDOG fires and the attempt is counted.
   - After 2 such attempts stage0 moves to slot B.
   - With harnessd confirming, the counters stay at 0.

## Open validation items (need the board)

- **Cache coherence at the hand-off.**
  - rv32imac has no cache-maintenance CSRs. Stage0 read-thrashes 32 KiB (4× the 8 KiB
    D-cache) at `0xBE00_0000` and then issues `fence.i`.
  - Confirm that OpenSBI fetches the bytes that were written. Ladder rung 1 proves it.
- **TELEM STATUS[0] as the calibration bit.** This is SHELL's routing (SHELL_CONTRACT §5).
- **The cold settle vs the MCC's post-configuration sequence** (OSC6 = the MIG reference is
  programmed after configuration; stage0 cannot reset the MIG: `sys_rst` is USER_nPB0
  only). Measure "configuration complete" -> "Releasing CB_nRST" on the MCC console and
  read status `entry` `[28]`/`[29]` + calib drops after cold boots (STAGE0_CONTRACT §1).
- **The armed watchdog vs Linux boot time.** It must reach a kicking harnessd in under
  42.9 s. July measured 18 s to the control plane; B1 measures it on the MBV static.
- The rescue TFTP rate over the real LAN9220. It is lock-step, so expect RTT-bound
  1–5 MB/s: 22 MB takes about 5–20 s.
- The µSD read rate at 12.5 MHz: about 1 MB/s, so a 22 MB slot takes about 20 s.
