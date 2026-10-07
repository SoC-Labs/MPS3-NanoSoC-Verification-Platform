# webharness — a small web dashboard for the MPS3 nanoSoC harness

System stats, DUT identity, DUT clock/reset control, and a
"what can I attach to, and how" directory — in a browser, over the frozen
`:6900` control channel. Stdlib only (`http.server`), no Flask, no npm, no
build step, nothing vendored.

It works against the **currently shipped** bare-metal shell (see
`docs/FIELDED_SHELL.md` — restating the id here is what made this line wrong) with
no firmware change, no re-mint and no board risk, and it is written so the same
pages can later run **on** the harness when the Linux shell ships (§5).

```sh
# on the hub (<hub-host>), pointed at a board
PYTHONPATH=host:host/pyverify python3 -m webharness --shell-host 192.168.10.101

# no board at all — the whole UI against the in-process fake
PYTHONPATH=host:host/pyverify python3 -m webharness --fake
```

Then `http://127.0.0.1:8080`. Everything the page shows is also plain JSON:

```sh
curl -s localhost:8080/api/status  | python3 -m json.tool
curl -s localhost:8080/api/services
curl -sX POST localhost:8080/api/reset -d '{"target":"dut"}'
curl -sX POST localhost:8080/api/clock -d '{"preset":"50mhz"}'
```

---

## 1. Read this before you bind it anywhere

**This server can reset the DUT and retune its clock, and it has no
authentication.** That is deliberate — a half-measure would invite trusting it.
So:

* it binds `127.0.0.1` by default;
* `--listen-host 0.0.0.0` prints a loud warning and is not the intended
  deployment;
* to reach it from elsewhere, forward a port (`ssh -L 8080:localhost:8080
  <hub-host>`) or put it behind the `wg0` mgmt tunnel the tender already
  uses. Do not put it on the lab LAN.

**`:6900` is a single-client channel** (net-protocol.md: extras are refused
accept-then-EOF). A dashboard on a refresh timer could otherwise hold the one
slot against `pyverify`, `fpgahub` and the console tools. Two things prevent
that, and both are gated by tests:

* every read opens a **short-lived** connection and closes it — never a
  long-lived one;
* reads go through a TTL cache (`--ttl`, default 1 s), so an idle page costs at
  most one connection per second no matter how many tabs are open. Writes are
  never cached and invalidate the reads.

`:6900` is also **parked during a swap**, so `/api/status` and `/api/diag` will
report unreachable mid-swap. That is the protocol working, not a fault.

---

## 2. Layers

Each layer knows nothing about the one below it, which is what makes the whole
thing testable with no board and no sockets.

| Module | What it is |
|---|---|
| `catalog.py` | Pure data: which services a given `rm_id` exposes, on what port, with the exact attach command. Transcribed from `firmware/clcd/clcd.{h,c}` and `socket_harness.endpoints`, **drift-guarded** (§4). |
| `control.py` | Pure data + logic: the clock preset table (from `firmware/clkrst/clkrst.c`) and the reset taxonomy, including which resets this server can actually drive. |
| `backend.py` | The deployment seam (§5). `ShellBackend` (hub → board), `FakeBackend` (no board), `HarnessBackend` (future, on-board). |
| `api.py` | The entire HTTP surface as one pure function: `handle(method, path, body, backend) -> Response`. No sockets, no globals. |
| `pages.py` | The single-page UI. Inline CSS/JS, light+dark, responsive, no external resources. |
| `server.py` | A `BaseHTTPRequestHandler` that only moves bytes. |

This is the same split `pyverify.display_http` uses (pure routing core + an
injected client factory) — scaled up from four routes to nine.

---

## 3. The API

| | | |
|---|---|---|
| `GET` | `/` | the page |
| `GET` | `/healthz` | server liveness — **never** contacts the board |
| `GET` | `/api/status` | identity + reachability + DUT makeup + lockup |
| `GET` | `/api/services` | the attachable-services directory |
| `GET` | `/api/clocks` | presets + what the contract really says |
| `POST` | `/api/clock` | `{"preset":"50mhz"}` |
| `GET` | `/api/resets` | reset taxonomy + which kinds are actionable here |
| `POST` | `/api/reset` | `{"target":"dut"}` |
| `GET` | `/api/diag` | the shell's 14 diagnostic counters |
| `GET` | `/api/console/<key>` | how to attach to one console (+ the ws seam, §6) |

Status discipline: a **transport** failure is `502` ("the board did not
answer"); an **application** decline the shell itself returned — `bad target`,
`unknown preset` — is `200` with `ok:false`, because the exchange succeeded and
the answer is "no"; a malformed request is `400` and never reaches the board.
Every JSON body carries `ok`.

### Things the API refuses to round off

* **There is no power sensor**, by construction (four independent dead-ends,
  net-protocol.md v0.6). `/api/status` says `power_sensor:false` rather than
  showing a plausible `0 mV`.
* **`dut_lockup` is only meaningful for RMs that drive it.** `nanosoc_multicore`
  does; `nanosoc`, `eth_ss` and the OOC RMs hard-tie it to 0. So the API carries
  `lockup_meaningful`, and the page says "cannot report", not "healthy".
* **An unknown `rm_id` is shown raw**, all 32 bits including the version half,
  exactly like `clcd_rm_name()` — never guessed at.
* **Absent services are listed, not hidden.** "This port exists but this RM has
  no CPU behind it" is the useful answer; hiding the row would make an eth-only
  RM look like a broken shell.

### What `set_clk` really does — and what is still open

The retune is **real**, not a stub: the shell looks the preset up in a
fail-closed `strcmp` table, writes the id to `CLKRST.DUT_CLK_SEL`, reprograms
the DUT-clock MMCM over the clk_wiz AXI4-Lite DRP (`MMCM_DRP` @ `0x44AB_0000`)
and bounded-polls `CLKRST.STATUS.mmcm_locked` for the relock
(`firmware/clkrst/clkrst.c:126-150`). If the MMCM does not report lock, the API
returns `ok:true, locked:false` plus a `warn` — it does not pretend.

What is **not** settled is the naming: **OPEN_ISSUES I16** — the preset names
and their `DUT_CLK_SEL` ids are not yet contract values, and `DUT_CLK_SEL` is
inert on today's fabric (the DRP write is what retunes). `/api/clocks` carries
that caveat verbatim so nobody scripts against `"50mhz"` believing it is
frozen. Arbitrary frequencies are deliberately not exposed:
`CLKRST.DUT_CLK_DRP` is a placeholder window.

### Why there is exactly one reset button

The `:6900` reset verb accepts **only** `target="dut"` —
`coordinator_handle_reset()` answers `"bad target"` for anything else, because
`dbg_resetn` belongs to the SWD/JTAG server (it tracks OpenOCD's srst) and
`rp_resetn` is held by `swap_fsm` across a swap. The wider taxonomy
(`dfx-swap`, `mcc-reconfig`, `usb-power`, …) is real but is driven by the swap
path or the tender, so `/api/resets` lists each with its invalidation set from
`pyverify.edge.reset` and `actionable:false`. The page shows what each *would*
do without growing a button that cannot work.

---

## 4. Tests — `PYTHONPATH=host:host/pyverify pytest host/webharness/tests`

98 tests, gated in `make check` / `make check-ci` stage 4. Board-free: the only
sockets are a loopback server against the in-process fake.

The interesting ones are the **drift guards**, because this package transcribes
tables that already exist elsewhere:

* `test_catalog.py` **parses `firmware/clcd/clcd.{h,c}`** and asserts the
  service bits, the RM name/makeup tables, and the service-presence mapping
  agree — the last over **all 65536 design ids**. The web page and the on-board
  glass therefore cannot disagree about what is up.
* Ports are asserted equal to `socket_harness.endpoints.REGISTRY` for every
  service the registry knows.
* `api.rm_id_indicates_loaded` is asserted equal to pyverify's private copy over
  a spread of ids.

All four transcription guards were **mutation-checked** at delivery (wrong
service bit / dropped UART1 / wrong port / renamed RM → exactly the matching
assertion reddens).

`test_backend.py` gates the single-client discipline directly: one connection
per uncached read, **zero** per cached read, writes always reach the board and
always invalidate.

### The panel twin, and why this page can't drift from it

The on-board CLCD grew an **"Applications & Ports" page** at the same time as
this one (`docs/planning/CLCD_APPS_PORTS_PAGE_PLAN.md`; `clcd_rm_services()` +
a `SERVICES[]` table in `firmware/clcd/clcd.c`). Two renderings of one fact set:
the panel has 40 columns and shows `nc <ip> 6930`; this page shows the full
`pyverify`/`openocd` invocation.

Labels and command strings are therefore **deliberately not** compared. What is
gated is the part that must never diverge — the **set, the order and the ports**:

```
test_service_set_and_order_match_the_firmware_apps_table
test_ports_match_the_firmware_apps_table
test_unknown_rm_falls_back_the_same_way_the_panel_does
```

So if a tenth service lands on the panel, this page cannot silently omit it.

### The port chain — closed 2026-08-01

`socket_harness.endpoints.REGISTRY` used to have **no row for the JTAG bridge
(TCP 6921)**: it predated the SWD→JTAG migration, while the shipped
The JTAG-bridge shell (since `0xCD74B6AE`) runs `jtag_server` on 6921 with
`swd_server` dormant. The
contract itself then grew the row (`net-protocol.md` TCP 6921 /
`net_proto.h` `MPS3_PORT_JTAG`), which made the fix well-founded rather than a
local patch. The chain is now complete and every link is drift-guarded:

```
docs/contracts/net-protocol.md  +  firmware/common/net_proto.h  (MPS3_PORT_JTAG)
   └─> pyverify.debug.JTAG_REMOTE_BITBANG_PORT
         └─> socket_harness.endpoints.REGISTRY['jtag']     (test_endpoints drift table)
               └─> webharness.catalog SERVICES['jtag']     (both guards above)
```

`registry_gap()` survives as the **mechanism**: it returns empty today and a
test pins that, so the next service that outruns the registry is visible
instead of silently papered over.

---

## 5. The seam: running this **on** the harness

Everything above `backend.py` is deployment-agnostic. Today's `ShellBackend`
speaks TCP to the board. When the Linux harness shell ships
(`src/linux_harness/`), a `HarnessBackend` runs the *same* API and pages on the
board itself:

| | host mode (today) | on-harness mode (future) |
|---|---|---|
| stats | `ping`/`diag` over TCP `:6900` | `/run/mps3/status.json` — already written by `mps3-clcdd`, already the right shape (`sw/apps/status_linux.h`) |
| identity | `ping.rm_id` | `/sys/class/misc/mps3dfx/rm_id` |
| clock/reset | `:6900` verbs | loopback `:6900` — `mps3-harnessd` runs the firmware's own `clkrst.c` over UIO (`docs/planning/linux_lanes/HARNESSD_CONTRACT.md`), so the verbs are real; the v0.7 `mps3-ctrld` stubs are retired |
| where it runs | <hub-host> | the board — survives the hub being down |

Nothing in `api.py`, `pages.py`, `catalog.py` or `control.py` changes. The
blockers are not in this package: the Linux shell is not the shipped bitstream,
and it carries the open WAVE4 clocksource and eth0-IRQ items.

---

## 6. Next increment: a console in the browser

`/api/console/<key>` already returns the attach command and the `socat` line for
UART0/UART1/SWO, plus a `websocket` object that honestly reports
`available:false`. Making it true needs two pieces, in this order:

1. **A WebSocket ↔ TCP relay.** `socket_harness.console_bridge.ConsoleBridge` is
   already a pure-Python pty↔tcp bridge with a tested core; the relay is a
   framing adapter in front of it onto `6930`/`6931`/`6932`. Note these are
   `gated_by: rp` and **do not survive a swap** — the relay must drop and
   re-dial, not hang.
2. **A terminal emulator**, vendored as a static asset (no CDN — §2). Until
   both land, the copy-paste `nc`/`socat` command is the honest answer, which
   is why the route ships now rather than after.

Not started, and deliberately not stubbed with a route that 500s.

---

## 7. What is deliberately not here

* **No swap/deploy button.** Pushing a bitstream is `pyverify deploy`; it holds
  the control channel for seconds, needs the overlay artefacts on disk, and a
  mis-click reconfigures the FPGA. It belongs behind the CLI's argument
  checking, not behind one HTTP POST.
* **No auth, no sessions, no users** (§1).
* **No CSR/register poking.** `socket_harness.registers` reaches the CSR estate
  over xsdb/hw_server; exposing raw register writes over HTTP is a footgun with
  no current use case.
* **No board lease integration.** This server does not acquire the fpgahub
  lease, so it will happily talk to a board someone else has leased. Lease
  first.
