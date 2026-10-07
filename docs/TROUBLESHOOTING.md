# Troubleshooting

Symptom-indexed fixes for the things that actually go wrong on the MPS3 nanoSoC
platform. Each entry: what you see → why → what to do. For the happy-path
workflow these hang off, see [BOARD_BRINGUP.md](BOARD_BRINGUP.md).

Most of these are **not bugs** — they are load-bearing gotchas that look like
failures. The single most common mistake is reacting to an *expected* timeout or
a *by-design* error as though something broke.

## Quick index

| You see… | Most likely cause | Jump to |
|---|---|---|
| SD/`sd_update` write "timed out" | Expected — the write is still finishing | [§1](#1-the-sd-write-timed-out) |
| Board dark / no ping after power-on | Shell not loaded — **or** loaded and its firmware hung | [§2](#2-board-dark-or-unreachable) |
| `make check` fails on overlay `static_id` | Local-only stale/other-shell overlay | [§3](#3-make-check-fails-on-overlay-static_id) |
| Fresh clone: overlay verify fails | `.bin` payloads absent (gitignored) | [§3](#3-make-check-fails-on-overlay-static_id) |
| Swap returns not-verified / RM won't load | `static_id` mismatch or wedged shell | [§4](#4-a-swap-fails-or-the-rm-wont-load) |
| DUT console silent | Wrong port, baud, or firmware stall | [§5](#5-the-dut-console-is-silent) |
| Over-the-wire push hangs | Board data-plane not reachable from where you pushed | [§6](#6-an-over-the-wire-push-hangs) |
| OpenOCD can't find the TAP / can't halt | Wrong transport, or lease expired | [§7](#7-openocd-cant-halt-the-dut) |
| Lease `release` returns HTTP 403 | Expected | [§8](#8-lease-errors) |
| `no such board` from a board command | Stale client / wrong name | [§8](#8-lease-errors) |
| Board stuck after swapping *away* from a debug DUT | The DAP-teardown case | [§9](#9-board-stuck-after-swapping-away-from-a-debug-dut) |
| The **shell's own** serial console is silent (or one node is garbage) | Known: the console lane is behind the MCC's UART mux | [§10](#10-the-shells-own-console-is-silent) |

---

## 1. The SD write "timed out"

**Cause.** `scripts/mps3_sd_update.sh` writes tens of MB to the config-SD over
slow USB mass-storage. The client times out **long before the write completes**.
This is expected behaviour, not an error.

**Fix.** Do nothing for ~5 minutes, then power-cycle / reboot so the MCC re-reads
the SD. **Never retry the write mid-flight** — a second write over the first
corrupts the SD and bricks the boot until it is re-imaged. One write, wait,
reboot.

## 2. Board dark or unreachable

**Cause.** The MCC loads the shell bitstream from the config-SD **only at
power-on**. If the SD has no valid shell (or a corrupted one from a retried
write, §1), or the fabric was cleared by a lease-TTL reset, nothing answers.

**Triage — read the fabric before you theorise.** "Dark" has two completely
different causes that are indistinguishable from the network, and guessing
between them wasted a session on four wrong theories. The **first** read is the
JTAG configuration registers:

| `DONE` | `EOS` | `CRC_ERROR` | `USERCODE` | Verdict | Fix |
|---|---|---|---|---|---|
| 0 | — | 1 | — | the fabric never took a bitstream | re-program (below) |
| 0 | — | 0 | — | nothing was ever loaded | re-program (below) |
| 1 | 1 | 0 | = the expected `static_id` | **configured and inert** — the fabric is fine; the *baked firmware* is hung | re-bake the ELF into the `.bit` (`updatemem`). **Not** a re-mint, and not an SD problem. |

**Fix.**
- **Power-cycle** the board — a *physical* one. `fpgahub target reset --method
  mcc` is a **proven no-op** on this board: the MCC drops burst characters, so
  the plugin's `REBOOT\r` lands only its first byte (fpgahub's own `mcc.py`
  docstring says so). The `msd` method is the replacement, once wired.
- Config-from-SD at the MCC is **non-deterministic** — a power-cycle is not a
  reliable way to reproduce a given shell. Do not conclude "the image is broken"
  from one bad boot; probe (above) instead.
- To get it up *now*: `scripts/mps3_recover.sh` JTAG-loads the fielded static
  shell in ~30 s. This is **volatile** — the next power-cycle reverts to the SD.
- Confirm with `echo '{"op":"ping"}' | nc <board-ip> 6900`; the `shell_id` must
  match [FIELDED_SHELL.md](FIELDED_SHELL.md).

## 3. `make check` fails on overlay `static_id`

**Two different situations:**

**(a) Fresh clone — "overlay verify" fails / skips.** The large overlay `.bin`
payloads are gitignored and regenerated, so a clean checkout has the manifests
but not the payloads. The CRC round-trip self-skips when payloads are absent;
regenerate them if you need the full check:

```bash
make -C fpga/dfx overlays
```

**(b) An overlay is "keyed to a DEAD shell" / `STALE`.** An overlay whose
`static_id` differs from the current shell's is flagged by the R9 lockstep gate.
This is **correct** — a shell rebuild re-mints the `static_id` and invalidates
every stored partial. It is **not** a fresh-clone or CI failure (that check runs
only when the local `.bin` payloads are present). Fix by re-minting/re-keying
that overlay to the current shell (`make -C fpga/dfx overlays` for it), or — if
it deliberately targets a *different* static shell (e.g. the XiP flash-boot
shell) — that is a known cross-shell case to resolve in the next consolidating
mint, not a defect in your change. See [STATUS.md](STATUS.md).

## 4. A swap fails or the RM won't load

**Symptoms.** `verified:false`, the swap never completes, or the shell stops
answering afterwards.

**Causes & fixes:**
- **`static_id` mismatch.** The overlay was built against a different shell mint.
  Re-key it to the running shell (§3b). The pre-board `make check` gate catches
  this — run it.
- **Interrupted transfer wedged the shell.** An interrupted ~1.3 MB partial can
  leave the partition/shell in a bad state. Reprogram a known-good shell `.bit`
  over JTAG (or power-cycle to reload from SD), then retry — and don't interrupt
  it this time.
- **Shell firmware / ICAP mismatch.** A re-keyed shell whose firmware was built
  without the FIFO-mode HWICAP configuration the block design instantiates will
  ping and pass gates but **fail to load any RM**. Rebuild the shell firmware
  with the HWICAP-FIFO configuration matching the BD.
- **Lease expired mid-session.** If the TTL lapsed, the fabric was cleared out
  from under you (§8). Re-acquire and reprogram.

## 5. The DUT console is silent

**Causes & fixes:**
- **Wrong channel.** The DUT console is bridged to TCP (`6930` in the
  [port map](contracts/net-protocol.md)); use `scripts/mps3_console.py --host
  <board-ip>`, not a random serial node.
- **Baud misconfiguration.** A firmware built with a zero/incorrect UART
  baud-divider produces a *silent* console even though the core is running. This
  has masqueraded as a "dead core" before — check the baud config before
  concluding the DUT is dead.
- **Firmware stalled, core alive.** The M0 can boot and execute but loop before
  its banner (e.g. waiting on a peripheral). A silent console is not proof of a
  dead core — attach the debugger (§7) and read the PC; a moving PC means it's
  alive and stuck in firmware, not halted in hardware.

## 6. An over-the-wire push hangs

**Cause.** The board's data-plane IP (`192.168.10.101`) is on the **fpgahub
host's** interface. A push from anywhere else has no route to it and stalls at
connect, which looks like a hang.

> An earlier version of this page claimed that pushing *on the hub* deadlocks
> the window-as-grant flow control. **That is false.** The 9-overlay sweep of
> 2026-09-09 ran `scripts/mps3_push.py` on the hub via `swap_check.py --via-hub`
> and every overlay reported `verified:true` in and back out.

**Fix — hub-side is the proven path.**

```bash
scripts/harness_gates/swap_check.py --rm <rm> --expect-rm-id 0x0100XXXX \
    --prod <hub-readable prod dir> --via-hub <hub-host>
```

**Alternative — from a workstation, over a tunnel.** TFTP is UDP and will not
traverse an SSH tunnel, so the TCP transport is mandatory:

```bash
ssh -N -L 6900:<board-ip>:6900 -L 6910:<board-ip>:6910 <hub-host> &
scripts/mps3_push.py --host 127.0.0.1 --src tcp --pusher-transport tcp --windowed ...
```

## 7. OpenOCD can't halt the DUT

**Causes & fixes:**
- **Wrong transport mode.** On the shipped JTAG-bridge shell the proven path is
  `remote_bitbang` (`TRANSPORT_MODE rbb`, TCP `6921`), **not** the older SWD
  `swd_server` (`6920`), which is dormant on that shell:
  ```bash
  openocd -f host/openocd/nanosoc_mps3_jtag.cfg \
          -c "set TRANSPORT_MODE rbb" -c "set RBB_HOST <board-ip>"
  ```
  Expect TAP `0x6ba00477`.
- **No DUT loaded.** The debug DAP lives in the loaded SoC RM — if the partition
  holds a non-debug RM (or the greybox), there is nothing to halt. Load a
  debuggable DUT first (§4).
- **Lease expired.** A TTL reset clears the fabric, so the TAP vanishes
  mid-session (§8). Re-acquire and reprogram.

## 8. Lease errors

- **`release`/`heartbeat` return HTTP 403 even with a fresh token.** Known and
  harmless — don't fight it. The board also self-resets when the lease TTL
  expires, so treat the end of a window as a hard restore-to-blank and
  re-acquire + reprogram for the next one.
- **`no such board` / a `404`.** Usually a **stale client** or the wrong
  identifier — board routes are addressed by the chassis name, and older
  `--addr`-style forms have been retired. Update the client and use the chassis
  name.
- **A stray queue entry blocks `acquire`.** Clear it with the board tool's
  `lease cancel` for your holder name (see the script `--help`).

## 9. Board stuck after swapping *away* from a debug DUT

**Symptom.** After swapping away from a DUT that carried a debug DAP (the nanoSoC
or multicore RM), the next swap never completes (`rm_id_valid` never asserts) and
SWD/JTAG is dead. Swapping away from a non-debug RM (greybox, eth-only) tears
down cleanly.

**Cause.** A DAP-RM teardown ordering issue in the shell firmware — swapping away
from a live debug DAP could leave the reconfigurable partition decoupled and held
in reset.

**Status & fix — FIXED AND FIELDED.** The firmware fix (a config-agent defer on
the teardown handoff) was baked into the consolidating mint and written to the
SD on 2026-08-10 (`e37ded0`), so it is live **from a cold boot**, not only when
JTAG-loaded. Re-proven 2026-09-09 for all three DAP-bearing RMs (`nanosoc`,
`nanosoc_multicore`, `nanosoc_upy`): each swapped in and back out with
`verified:true`.

The old workaround — "re-JTAG the shell, then load a single DAP-bearing RM per
session" — is **no longer needed**. If you still see this symptom, you are not
running the fielded shell: check `shell_id` against
[FIELDED_SHELL.md](FIELDED_SHELL.md) before anything else.

---

## 10. The shell's own console is silent

Not the DUT's console (§5) — the **shell coordinator's** `xil_printf` boot log.

**This is a known, understood, open defect. Do not debug it on the board.** The
full evidence chain is in [planning/CONSOLE_AUDIT.md](planning/CONSOLE_AUDIT.md);
the short version:

The MPS3's four FPGA UART lanes are **not equivalent**. Lanes 0 and 1 reach the
host only through a source multiplexer that the MCC drives from the `UARTMODE:`
key in the SD card's `config.txt`; lanes 2 and 3 are hard-wired to the host
(Arm MPS3 TRM `100765_0000_04_en` §2.18 Figure 2-25, page 2-51; Arm V2M-MPS3
Schematics `EOI-0309` rev C, sheet 9 "USB UARTS"). The shell's console is pinned
to **lane 1** (`AE30`/`AE31`), and the `config.txt` this platform ships selects
mode `0` = `MCC:FPGA0` — which does not select lane 1. The console is wired to a
multiplexer input that is switched off.

So the symptom is not a fault to chase:

- the node carrying the **MCC** shows MCC traffic (and has shown unreadable
  bytes at the wrong baud) — the shell physically cannot write to it;
- **every** node the shell could reach is fed by a lane the shell does not
  drive, so all of them are correctly silent.

**Causes ruled out — do not re-investigate:** uartlite baud (115200 8N1, divisor
correct for the 100 MHz AXI clock), an unclocked uartlite (fixed long before the
fielded mint), the BSP's `stdout` binding (it is the one UART instance in the
design), and the host console tooling (`socket_harness` / `pyverify.console`
serve the DUT's TCP consoles on 6930-6932 and are not on this path at all).

**Until it is fixed:** use JTAG telemetry for shell firmware diagnosis — the
diag mailbox at the top of the MicroBlaze LMB, sized per
[FIELDED_SHELL.md](FIELDED_SHELL.md). That is what every shell diagnosis this
year has actually used.

**The fix** is a pin move to lane 2 — the lane with no multiplexer, and the lane
both `src/linux_soc/hw/gen_pins.py` and the Linux transplant already moved to on
bench evidence. It needs a mint, so it is queued as a pin request rather than
applied. An interim that needs no rebuild — only an SD rewrite and a
power-cycle — is `UARTMODE: 1`, which selects lane 1 onto the host port the
shell already drives.

---

## Still stuck?

- Re-read the relevant [contract](contracts/) — most "it won't talk to me" issues
  are a port, framing, or `static_id` assumption that the contract pins down.
- Reproduce it **off-board first**: `make check` covers the swap FSM, config
  agent, wire protocol, and register maps in simulation. If it fails there, it's
  logic; if it only fails on the board, it's hardware/deployment.
- Security- or IP-sensitive findings: [SECURITY.md](../SECURITY.md) (report
  privately, not via a public issue).
