# Board bring-up — from a dark board to a live networked DUT

This is the public, hardware-oriented runbook: how to take a physical **Arm MPS3
(HBI0309C, Xilinx Kintex UltraScale XCKU115)** from powered-off to a running,
network-reachable DUT you can reconfigure and debug over the wire.

You do **not** need a board to work on most of this platform — see
[the board-free gate](#you-dont-need-a-board-to-start) first. When you *do* have
one, work top-to-bottom; each step depends on the previous one.

> Operator-specific details for a particular lab deployment (exact hostnames,
> management IPs, cable serials, console device nodes) are intentionally **not**
> in this public doc — substitute your own. What matters here is the workflow
> and the hazards, which are the same everywhere.

---

## The mental model

Three things cooperate:

1. **The board's MCC** (motherboard configuration controller) loads a **static
   shell** bitstream from the on-board **config-SD** at power-on. The shell is
   the fixed part of the design: the MicroBlaze management processor, the
   network MAC, the ICAP reconfiguration engine, and an empty **DUT partition**.
2. **A reconfigurable module (RM / "overlay")** — your DUT (e.g. the nanoSoC
   Cortex-M0 SoC) — is streamed into that partition **over the network** and
   swapped in live, without rebuilding or re-flashing the shell.
3. **A host** drives the board over two channels: **Ethernet** (control plane,
   DUT deploy, console-over-TCP) and **JTAG** (bitstream programming, DUT debug).

So "bring-up" is: get a shell onto the board → reach it on the network → deploy a
DUT into its partition → talk to and debug that DUT.

## You don't need a board to start

The entire logic layer is provable on a normal Linux host:

```
make check-ci     # the 5-stage CI gate (Python + a C compiler; no board, no Vivado)
make check        # the fuller 8-stage local gate
```

Get this green first. It exercises the swap coordinator, the config agent, the
wire protocol, and the register maps — so when you reach a board you are
debugging *hardware*, not logic. See [contracts/](contracts/) for the frozen
interfaces those gates enforce.

---

## Step 1 — Lease the board

The board is a **shared, leased** resource. Take the lease before any JTAG or
network I/O, and release it when done.

```bash
export MPS3_HUB=<hub-host>                          # no default is baked in
TOKEN=$(python3 -m pyverify.cli lease acquire --holder <your-name>)
python3 -m pyverify.cli lease status                # confirm the board is held by you
python3 -m pyverify.cli lease preflight --holder <your-name>   # exit 0 => safe to touch

# when done (BOTH the token and the holder, or it stays HELD):
python3 -m pyverify.cli lease release --token "$TOKEN" --holder <your-name>

# stray queue entry from an abandoned acquire (no token needed):
python3 -m pyverify.cli lease cancel --holder <your-name>
```

`scripts/mps3_lease_acquire.sh` and `scripts/mps3_board.sh` still work — they
are now shims that exec these verbs.

> **Hazard — don't wrap `acquire` in a timeout or JSON parser.** It deliberately
> blocks until the board is free and prints only the token. If a stray queue
> entry is stuck, clear it with the board's `lease cancel` (see the script's
> `--help`). Keep windows **short**: the board is reset when the lease TTL
> expires (fabric cleared, DUT gone), so acquire → work fast → re-acquire after
> any gap.

## Step 2 — Get a shell onto the board

Two ways, depending on whether you want it to survive a power-cycle:

**Persistent (via the config-SD):** write the shell `.bit` to the config-SD and
let the MCC re-read it.

```bash
# backup gate (hub-local: mounts the SD read-only and copies the whole config
# set) then the write, under the one-write-wait discipline
scripts/mps3_sd_update.sh --bit <shell.bit> --token "$TOKEN" --program

# the write on its own, against a backup you already captured
python3 -m pyverify.cli sd write <shell.bit> --token "$TOKEN" --holder <your-name> \
    --backup <backup-dir> [--verify-path <path-on-the-mounted-SD>]
```

`sd write` refuses a second write while one is in flight, treats the client
timeout as the expected outcome, waits the settle interval itself, and md5
read-back-verifies when `--verify-path` is given (without it, it reports the
write **unverified** rather than assuming it worked).

> **Hazard — the SD write "times out" but did NOT fail.** The image is large
> (~tens of MB over slow USB mass-storage); the client times out long before the
> write finishes. **This is expected — not a failure.** Do **not** retry
> mid-write: a second write over the first **corrupts the SD** and bricks the
> boot until re-imaged. Write once, wait ~5 minutes, *then* reboot. A physical
> power-cycle reloads the KU115 from the SD.

**Volatile (via JTAG):** program the shell `.bit` straight into the fabric with
`hw_server`/`xsdb` (or Vivado). Faster to iterate; gone on power-cycle. A large
program takes minutes — run it in the background, never under a short foreground
timeout that would kill it mid-stream.

## Step 3 — Bring up the network

Once the shell is running, the management processor brings up Ethernet and the
control plane. The port map is the contract in
[contracts/net-protocol.md](contracts/net-protocol.md); the product-default
data-plane IP is `192.168.10.101` (configurable).

```bash
ping -c4 <board-ip>                                   # link up, ~0% loss
echo '{"op":"ping"}' | nc -q2 <board-ip> 6900         # control plane answers
```

A healthy control-plane `ping` returns the shell's `static_id` and the currently
loaded `rm_id`, e.g. `{"ok":true,"shell_id":"0x...","rm_id":"0x..."}`.

The DUT console is bridged to TCP (UART-over-Ethernet, port `6930` in the port
map) so you can read the SoC's output without a serial cable:

```bash
# interactive: bridge UART0 (6930) to a local pty
python -m socket_harness console uart0 --host <board-ip> --pty /tmp/mps3-console/uart0
```

```python
# scripted: no pty at all — assert on what the DUT prints
from pyverify.console import ConsoleReader, UART0_PORT
with ConsoleReader("<board-ip>", UART0_PORT) as uart0:
    uart0.assert_contains(b"nanosoc boot", timeout=30.0)
```

> `scripts/mps3_console.py` is a **different** tool: it is the hub-side *serial*
> driver for the harness MicroBlaze's uartlite (`--dev <serial node>`), not a
> client of the DUT's TCP console. It has no `--host`.

### The shell's own console (serial) — fixed in the tree, not yet on the board

The shell coordinator has a second, separate console: an `axi_uartlite` at
**115200 8N1**, carrying the MicroBlaze boot banner and lwIP/coordinator
diagnostics. It is a real serial port on the board's USB-serial interface, not a
TCP stream.

The MPS3's four FPGA UART lanes are **not interchangeable**. Lanes 0 and 1 reach
the host only through a multiplexer the MCC switches from the `UARTMODE:` key in
the SD card's `config.txt`; lanes **2 and 3 are hard-wired** to USB-serial ports
2 and 3, and are the only lanes that reach the host unconditionally (Arm MPS3
TRM `100765_0000_04_en` §2.18 Figure 2-25, page 2-51). This console was pinned
to lane 1 for a year on reasoning that never asked that question, so it was
wired to a switched-off mux input and the silence was indistinguishable from a
dead processor (`386fa27`, [planning/CONSOLE_AUDIT.md](planning/CONSOLE_AUDIT.md)).

<!-- CONSOLE_CHANNEL lane=2 uartmode=1 reachable=yes -->
<!-- ^ parsed by scripts/harness_gates/check_console_channel.py — keep it true. -->

**Both fixes are now in the tree, and they are belt and braces, not duplicates.**

- **Durable — re-pinned to FPGA UART lane 2** (`AD28`/`AE28`), which is
  hard-wired to USB-serial **port 2** and is therefore reachable under *every*
  `UARTMODE` (`fpga/shell/constraints/mps3_harness.xdc`). This moves pads, so it
  reaches the board only at the next mint — it cannot be deployed by rewriting
  an SD card.
- **Interim — `UARTMODE: 1`** in `fpga/mps3_sd/templates/config.txt` (was `0`).
  That is `MCC:FPGA1`, which muxes lane 1 onto a host port, so it makes the
  console reachable on the **currently fielded** bitstream, where the console is
  still on lane 1. It needs only an SD rewrite and a power-cycle
  ([Step 2](#step-2--get-a-shell-onto-the-board), the config-SD path), and it is a no-op for the console
  once the re-pin is minted.
- The two together mean the console is reachable **before** the mint (via the
  mux) and **after** it (via the hard-wired lane), with no window in between
  that depends on which change landed first.

Read it on the board's **port 2** node after the next mint, and on **port 1**
before it, at 115200 8N1.

- **Until a board actually shows the banner, diagnose shell firmware over JTAG
  telemetry**, not over serial. Nothing here has been seen on hardware: the
  evidence and both diffs are in
  [planning/CONSOLE_AUDIT.md](planning/CONSOLE_AUDIT.md), and the symptom is
  [TROUBLESHOOTING §10](TROUBLESHOOTING.md#10-the-shells-own-console-is-silent).
- Do not read the board's **port 0** node expecting shell output. That node
  carries the **MCC's** own command console (`Cmd> `); the shell has no port on
  lane 0 and cannot write to it.

> **Hazard — the raw board console UART has a tiny RX FIFO.** If you ever drive
> the physical console directly, **char-pace** input (~4 ms/char); never bulk
> `echo`/paste into it or the FIFO overruns and mangles the command. The
> TCP console bridge handles this for you.

## Step 4 — Deploy a DUT over the wire

Stream a reconfigurable module into the partition and swap it in. The overlays
live under `fpga/dfx/overlay/<rm>/` (a small tracked manifest + a large,
regenerated `.bin` payload).

```bash
# canonical deploy (overlay dir, or a DFX prod dir + an RM name)
python3 -m pyverify.cli deploy --host <board-ip> --overlay fpga/dfx/overlay/<rm>
python3 -m pyverify.cli deploy --host <board-ip> --prod <prod-dir> --rm <rm-name>

# the same push, wrapped in a gate that also asserts verified + the exact rm_id.
# --prod defaults to the FIELDED shell's artefact dir (pyverify.fielded reads
# docs/FIELDED_SHELL.md); --dry-run prints the exact command and touches nothing.
scripts/harness_gates/swap_check.py --rm <rm-name> --expect-rm-id 0x0100XXXX \
    --via-hub <hub-host> --dry-run

# read-only identity/health, over the same conformance-pinned client
python3 -m pyverify.cli ping    --host <board-ip>      # shell_id / rm_id
python3 -m pyverify.cli version --host <board-ip>      # FIRMWARE identity + flags
python3 -m pyverify.cli diag    --host <board-ip>      # the always-on counters
```

A successful swap reports `{"ok":true,"rm_id":"0x...","verified":true}`.
`swap_check.py` additionally fails loud if `verified` is false or the resident
`rm_id` is not exactly the one you asked for — a swap that silently lands on
greybox is the escape it exists to catch.

> **Where to run the pusher.** The board's data-plane IP is on the **fpgahub
> host's** interface, not on your workstation, so the proven path is to run the
> push **on the hub** (`--via-hub <hub-host>` ssh-runs it there). From a
> workstation, tunnel and use the TCP transport (`--src tcp`) — TFTP is UDP and
> will not tunnel.

> **Hazards:**
> - **Overlays must be keyed to the running shell's `static_id`.** A shell
>   rebuild re-mints the `static_id` and invalidates every stored partial. The
>   `make check` overlay lockstep catches a stale key before you ever reach the
>   board — heed it.
> - **Never interrupt a swap.** An interrupted ~1.3 MB transfer can wedge the
>   shell and require a reprogram.

## Step 5 — Run the on-board tier

With the board leased and a shell live, the tiered regression drives the whole
loop (ping → swap → swap-away → console) instead of you doing it by hand:

```bash
MPS3_BOARD_VIA_HUB=1 MPS3_PROD_DIR=<hub-readable prod dir> \
  make harness-regression HR_ARGS="--through 3 --allow-board"
```

`MPS3_BOARD_VIA_HUB=1` routes the network gates through the hub over ssh —
required, because `192.168.10.101` is reachable **only** from the hub host.
`MPS3_PROD_DIR` must name a prod directory readable **on the hub**: the swap
gates ssh-run the pusher there, where your working tree is not visible. Tier 3
refuses to run without `--allow-board`, and takes the lease first.

For a DUT that terminates the RMII, the reception proof is a separate JTAG gate
(it needs a register the host cannot see):

```bash
MPS3_HW_URL=tcp:<hub-fqdn>:3121 \
  xsdb scripts/harness_gates/dut_rx_check.tcl <expected-rm-id>
```

## Step 6 — Debug the DUT

Once a debuggable DUT (e.g. the nanoSoC M0) is loaded, attach a host debugger
through the shell's JTAG bridge:

```bash
openocd -f host/openocd/nanosoc_mps3_jtag.cfg -c "set TRANSPORT_MODE rbb" \
        -c "set RBB_HOST <board-ip>"
```

This reaches the SoC-400 SWJ-DP (TAP `0x6ba00477`) → AHB-AP → the Cortex-M0
debug slave, and can halt the core, read the PC, and single-step.

> **Hazard — use the proven transport.** On the shipped JTAG-bridge shell the
> live debug path is `remote_bitbang` (`TRANSPORT_MODE rbb`, TCP `6921`), **not**
> the older SWD `swd_server` (`6920`), which is dormant on that shell. See the
> `jtag` endpoint in the [net-protocol contract](contracts/net-protocol.md).

---

## Standing hazards (the short list)

| # | Hazard | Rule |
|---|---|---|
| 1 | SD write client-timeout | Expected, **not** a failure. One write, wait ~5 min, then reboot. Never retry mid-write. |
| 2 | Lease TTL reset | Board wipes on expiry. Keep windows short; re-acquire + reprogram after any gap. |
| 3 | Console RX FIFO | Char-pace physical-console input; prefer the TCP console bridge. |
| 4 | Overlay `static_id` mismatch | Overlays are keyed to one shell mint; a rebuild invalidates all partials. |
| 5 | Interrupted swap | Can wedge the shell. Never interrupt; never blindly `stop`/`rst` a hung JTAG op — disconnect your own session cleanly. |
| 6 | Board data-plane is hub-local | `192.168.10.101` lives on the hub's interface. Push **on the hub** (`--via-hub`), or tunnel from a workstation with `--src tcp` (TFTP is UDP; it will not tunnel). |
| 7 | Config-from-SD is non-deterministic | A power-cycle does not reliably reproduce a shell. Probe before you theorise (see Recovery). |

## Recovery

**Read the fabric before you theorise.** On a dark board the FIRST read is the
JTAG configuration registers — `DONE`, `EOS`, `CRC_ERROR`, `USERCODE`. They
split the two failure modes that look identical from the network:

| Probe says | Means | Do |
|---|---|---|
| `DONE`=0 / `CRC_ERROR`=1 | the fabric never took a bitstream | re-program over JTAG |
| `DONE`=1, `EOS`=1, `USERCODE` = the expected `static_id`, board still inert | the fabric is **configured**; the baked *firmware* is hung | re-bake the ELF (`updatemem`) — **not** a re-mint |

Skipping this probe is how four wrong theories got chased on a board that was
configured the whole time.

- Config-from-SD at the MCC is **non-deterministic**: a power-cycle is not a
  reliable way to reproduce a given shell, so do not read "it came back
  different" as a design fault. Everything JTAG-loaded is volatile and gone.
- To un-dark the board: `scripts/mps3_recover.sh` JTAG-loads the fielded static
  shell (~30 s, cable-filtered). It is **volatile** — the next power-cycle
  reverts to whatever the SD holds.
- Confirm identity after any recovery: `echo '{"op":"ping"}' | nc <board-ip> 6900`
  should report the `shell_id` that [FIELDED_SHELL.md](FIELDED_SHELL.md) names.

## Where to go next

- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — symptom-indexed fixes for the
  failure modes above.
- [STATUS.md](STATUS.md) — what is proven on silicon vs simulation vs designed.
- [contracts/](contracts/) — the frozen wire/regmap/partition interfaces.
- [host/README.md](../host/README.md) — how the `pyverify` host tooling composes.
