# SERVICE_DISPOSITION — bare-metal superloop services → Linux dispositions

Date: 2026-07-15. Audit walked the FIRMWARE SOURCE (not the docs) under
`~/SoCLabs/mps3-nanosoc-platform/firmware/`. Ground-truth files cited per
section. The shipped entry point is `firmware/platform/src/main.c` (the Vitis/`make elf`
build); `firmware/harness_app/main.c` is the earlier interrupt-driven spike of the same
loop and is NOT the shipped image — dispositions below are for the platform superloop.

## 0. The superloop execution model (what dissolves under Linux, what does not)

`platform/src/main.c` runs a **pure poll model: no INTC, no ISRs anywhere in v1**
(deliberate — the LAN9220 driver is poll-mode, the AXI timer is only a free-running
counter for `sys_now`, and skipping XIntc/Xil_Exception saved LMB budget). Every module
exposes a bounded, non-blocking, resumable `_poll()` because **one blocking call starves
the lwIP timers and kills TCP** — the board's only ingress. Loop order each pass:

```
mps3_net_lwip_rx_poll(8)      # bounded RX drain (max 8 frames) -> lwIP
mps3_net_lwip_tmr()           # TCP/ARP timers (NO_SYS manual)
smsc911x_tx_status_drain()    # reap TX status FIFO (else the MAC halts)
swap_fsm_poll()               # DFX swap FSM, bounded chunk per call
config_agent_poll()           # TFTP/69 + TCP/6910 receive step
coordinator_net_poll()        # TCP 6900 JSON control
swd_server_poll()             # TCP 6920
xvc_server_poll()             # TCP 2542
uart_over_eth_poll()          # TCP 6930/6931/6932
clcd_poll()                   # (CLCD=1 builds) bounded 256 B/pass
<diag mailbox republish>      # every pass
heartbeat_service()           # GPIO bit0 LED, 1 Hz
```

Under Linux the *cooperative-chunking* constraint dissolves (the kernel preempts), but
three classes of constraint survive verbatim and must be honoured wherever the logic
lands:

1. **Ordering invariants**: CRC-verify before any ICAP write; decouple+RP-reset before
   any ICAP write; clearing-then-partial within a push pair; verify RM_ID with the RP
   *connected*, then re-isolate on mismatch.
2. **Wire compatibility**: fpgahub/pyverify/OpenOCD/hw_server speak fixed protocols on
   fixed ports (§2). :6900/:6910 are explicitly frozen by the tender; in practice all
   eight ports are load-bearing.
3. **Cross-service gating**: during a swap the shell gates SWD/XVC/UART/link access
   (`g_shell_state.*_gated`); XVC deliberately *stalls* a mid-swap `shift:` rather than
   erroring so hw_server doesn't wedge. Under Linux this coordination must be re-provided
   (single control daemon, or driver-enforced — see §4).

## 1. Inventory and disposition summary

| # | Service / driver | Source | Hardware block | Port(s) | Linux disposition |
|---|---|---|---|---|---|
| 1 | LAN9220 MAC driver | `smsc911x/smsc911x.c` | LAN9220 via AXI EMC @0xC000_0000 | — | **Stock kernel driver `smsc911x`** (`CONFIG_SMSC911X`); see red flag: IRQ not wired in the shell BD |
| 2 | lwIP backend + netif glue | `platform/src/net_if_lwip.c`, `common/net_if.c` | — | — | **Deleted** — replaced by the kernel network stack |
| 3 | Coordinator control channel | `coordinator/coordinator.c`, `coordinator_net.c`, `common/net_proto.c` | (dispatch only) | TCP 6900 | **Pure userspace daemon** (protocol frozen) |
| 4 | config_agent (bitstream receiver) | `config_agent/config_agent.c` | (staging only) | UDP 69 (TFTP WRQ), TCP 6910 | **Pure userspace daemon** (framing frozen); flow control moves to kernel TCP |
| 5 | swap_fsm (DFX swap engine) | `coordinator/swap_fsm.c`, `swap_fsm_transitions.c` | HWICAP @0x44A2, DFXCTL @0x44A1, CLKRST resets | — | **Custom kernel driver** (fpga-manager + fpga-bridge shaped) — §4 |
| 6 | DFXCTL decoupler/shutdown/RM_ID | (driven by 5) | dfx_ctl @0x44A1_0000 | — | Part of the custom DFX driver, exposed as an **fpga-bridge**; RM_ID/RM_STATUS via sysfs |
| 7 | overlay_store (QSPI A/B slots) | `overlay_store/overlay_store.c`, `ovlstore_codec.c` | AXI Quad SPI @0x44A4 → SST26VF064B (8 MiB) | — | **Stock `spi-xilinx` + MTD `spi-nor`**; A/B logic + codec = userspace over `/dev/mtd*` |
| 8 | clkrst (DUT clock + 3 resets) | `clkrst/clkrst.c` | CLKRST @0x44A0 + clk_wiz MMCM DRP @0x44AB | — | MMCM: **stock `clk-xlnx-clock-wizard`**; RESET_CTRL: owned by the custom DFX driver (rp_resetn is swap-sequenced), dut/dbg resets exposed to userspace |
| 9 | ~~swd_server~~ → jtag_server | `jtag_server/jtag_server.c` | JTAGBB @0x44A7 (+ CLKRST dbg_resetn for srst) | TCP 6921 | **NOT PORTED** — [DEV-10]; `mps3-swdd` retired to `daemons/legacy/`, successor not written (docs/planning/LINUX_FORK_JTAG_MIGRATION.md §3) |
| 10 | xvc_server | `xvc_server/xvc_server.c` | Debug Bridge (DBGBR) @0x44A8, XAPP1251 layout | TCP 2542 | **UIO + userspace daemon** (XVC v1.0, frozen) |
| 11 | uart_over_eth (3 streams) | `uart_over_eth/uart_over_eth.c` | UARTBR @0x44A9 (U0/U1/SWO FIFOs) | TCP 6930/6931/6932 | **UIO + userspace daemon** (raw byte relay); optional later: custom tty driver + ser2net |
| 12 | VPHY link injection | `coordinator.c` `handle_link` | VPHY @0x44A3 | (verb on 6900) | UIO poke from the control daemon |
| 13 | GENCHK macgen | `coordinator.c` `handle_macgen` | gen_checker @0x44A6 | (verb on 6900) | UIO poke from the control daemon |
| 14 | TELEM | `coordinator.c` `handle_telemetry` | telem @0x44A5 — **inputs tied to GND in the BD** | (verb on 6900) | Keep the deliberate `ok:false,"no power sensor"` failure in the daemon; no driver — there is no sensor, by construction (4 independent layers) |
| 15 | board GPIO + heartbeat | `platform/src/main.c` | board_gpio @0x44AA (IN/OUT/OE/**OWN** mux) | — | Custom trivial **gpio-mmio-style driver or UIO**; layout is non-standard (per-bit host/DUT OWN mux). LED heartbeat → kernel `heartbeat` LED trigger if a gpiochip is written |
| 16 | CLCD status display | `clcd/clcd.c`, `hx8347_init.c` | CLCD 8080 byte-stream master @0x44AC (no framebuffer — panel GRAM is the FB) | — | **UIO + userspace daemon**. NOT fbdev/DRM: the block streams {RS,byte} pairs; writes are DROPPED on FIFO-full (no back-pressure by design) so the writer must poll STATUS |
| 17 | CLCD KVM arbiter | `clcd_kvm/clcd_kvm.h/.c` | CLCDKVM @0x44AD — **RESERVED, no slave on the shipped shell** | (`display` verb on 6900) | Userspace (same daemon as 16) once the Wave-4 static rebuild lands it; until then keep the `clcd_kvm not present` decline path |
| 18 | diag mailbox | `common/diag.c`, `platform/src/ovlstore_phase.c` | fixed LMB address (top-anchored, magic-scanned), JTAG-MDM readable | (also `diag` verb on 6900) | Re-think: the JSON `diag` verb keys must survive in the daemon; the **JTAG post-mortem path is LMB/MDM-specific and is LOST on a Linux/DDR4 target** — see red flags |
| 19 | timebase | `platform/src/main.c` `mps3_sys_now_ms` | AXI Timer @0x41C0_0000 free-run | — | Deleted — kernel clocksource (the timer can back the MBV timebase) |

Not services: `firmware/micropython/` (DUT-side XiP MicroPython WIP, untracked),
`firmware/test/` (host-gcc harness with `MPS3_HAL_MOCK` register mocks),
`firmware/harness_app/` (superseded spike).

## 2. Wire protocols — FROZEN, host tooling depends on every byte

Static IP default **192.168.10.101/24** (matches the fpgahub MPS3 board block). Single
source of truth: `common/net_proto.h`.

| Port | Proto | Framing | Client |
|---|---|---|---|
| 6900 | TCP | one flat JSON object per `\n`-terminated line, request→response; **single client v1** (second accept refused); Nagle off | fpgahub tender, pyverify |
| 69 | UDP | TFTP WRQ/octet, RFC1350, 512-B blocks, per-transfer TID socket | host/pusher |
| 6910 | TCP | 24-byte big-endian header + raw payload (below) | host/pusher `push.py` |
| 2542 | TCP | XVC v1.0: `getinfo:`/`settck:`/`shift:`; getinfo reply `xvcServer_v1.0:2048\n` (max vector 2048 bits) | Vivado hw_server |
| 6920 | TCP | OpenOCD `remote_bitbang` single chars; `c` sample must reply ASCII `'0'`/`'1'` exactly | OpenOCD |
| 6930/6931/6932 | TCP | raw byte relay: UART0 (boot mon) / UART1 (app) / SWO (RX-only) | fpgahub console tooling |

**6900 JSON contract** (`mps3_ctrl_encode_response()` in `net_proto.c` is normative):
verbs `ping, reset, set_clk, swap, link, commit, telemetry, macgen, diag, display`.
Details host tooling depends on:

- IDs are rendered `"0x%08x"` lowercase hex strings (`shell_id`, `rm_id`).
- `swap` is the ONLY held response: the accept defers, the JSON line is only sent when
  the FSM reaches DONE/FAILED (pyverify blocks on it). Everything else is send-now.
- `telemetry` is **always** `{"ok":false,"err":"no power sensor","lockup":bool}` —
  deliberately; pyverify reads `lockup` regardless of `ok`. Do not "fix" this.
- `macgen`'s `"err"` key is polymorphic: counter on success, string on failure; clients
  key on `"ok"`.
- `diag` carries 14 fixed u32 keys (`rx_recover, rx_dumps, rx_drops, icap_bytes, got,
  expect, rcv_wnd, rcv_ann_wnd, rx_queued, pbuf_free, grants_sent, grant_fails, sndbuf,
  snd_wnd`). Some are lwIP-internals; under Linux keep the keys and return honest
  substitutes (or 0) — the schema is what's frozen. `grants_sent` already carries a
  repurposed quantity for exactly this reason.
- Parser is fail-closed: flat objects only, no nesting/arrays/`\uXXXX`, oversize line →
  error response, connection stays open.

**Bitstream push header** (TFTP and 6910, both): big-endian
`>4sHBBIIII` = magic `"MPS3"`, ver=1, kind (0=clearing, 1=partial), rm_slot,
static_id, rm_id, len_words (payload bytes/4), crc32 (zlib/IEEE, payload only).
Validation ORDER is contractual: header (magic/ver/static_id/kind/ordering/size) then
full-payload CRC, **all before any ICAP write**. Within a pair the clearing must
complete before the partial is accepted (a new clearing drops any stale staged partial).
Session model: ONE push session; a swap-armed session that dies silently is reaped by
`config_agent_abort_session()` + the FSM's 30 s RX-idle timeout (silicon-observed hang,
2026-07-09).

The bare-metal 6910 flow control ("window-as-grant": `CFG_AGENT_ACK_WINDOW_BYTES` ==
lwIP `TCP_WND` == 16384, window reopened only per fully-drained window) is an lwIP
artifact — under Linux the kernel's TCP window replaces it and the host side is
agnostic (it is pure receive-window pacing, no application bytes).

## 2A. The 6900/6910 wire contract — FROZEN semantics beyond framing

§2 froze the framing; this section freezes the **connection-level and reply-shape
semantics** that host tooling provably depends on. Every clause below is
source-cited against the bare-metal server AND (where a client exists) the client
that latches on it. The Linux daemons must reproduce these byte-for-byte and
state-for-state — they are the contract, not implementation detail.

### 2A.1 :6900 second-client refusal = ACCEPT-THEN-EOF (never RST-on-SYN, never a line)

`coordinator_net.c` (`coordinator_net_poll`): when a client is already adopted,
`mps3_net_accept()` returns the extra connection and the server immediately
`mps3_net_close()`s it — the TCP handshake **completes**, then the socket closes
with **zero bytes sent**. A connection parked by a held `swap` response looks
identical from outside.

**fpgahub depends on the distinction** (`fpgahub/src/fpgahub/shell_client.py`):
the module docstring (lines ~37–49) defines the `ControlChannel` state table, and
the read path (lines ~205–229) maps outcomes:

- accept-then-EOF (empty `readline()`) or reset-on-write → **`busy`** ("not a
  fault; the board is up") — `ShellBusy`;
- RST on SYN / no handshake → **`offline`** — `ShellOffline`;
- handshake + no reply within the ~2 s read budget → **`wedged`** (half-alive
  board) — `ShellWedged`.

Linux daemon obligations, therefore:
1. Adopt ONE 6900 client; `accept()` extras and `close()` them **immediately,
   with no bytes** — a kernel listen backlog must not be allowed to hold a
   second client unanswered (that reads as `wedged` after the 2 s budget, which
   turns the dashboard red during every deploy — the exact bug the fpgahub state
   table exists to prevent). Keep accepting-and-closing continuously.
2. Never send an error line to a refused client (a reply would masquerade as
   `idle`), and never refuse by not listening (that is `offline` = "shell dead").
3. During a held `swap`, keep refusing extras the same way (`busy` is correct).

### 2A.2 Unknown-verb reply = `{"ok":false,"err":"unknown op"}` — reply, keep the connection

`coordinator.c` (`coordinator_dispatch_line`): a decode failure ALWAYS produces a
response line — `"unknown op"` (unrecognised verb), `"bad args"`, or `"bad json"`
— and the connection **stays open** (parser is fail-closed but not
connection-fatal). **fpgahub probes with `{"op":"stats"}`** (`shell_client.py`
`stats()`, `stats.py`): a Phase-1 verb not yet in the firmware; the collector
sends it and latches on `ok:false` as "verb not supported yet", NOT as a fault.
So: unknown verbs MUST get a JSON reply with `ok:false` (never silence, never a
close — silence reads `wedged`, a close reads `busy`), and the `err` string for
an unrecognised op is frozen as `"unknown op"`. Corollary: when the `stats` verb
is eventually implemented, it must never answer `ok:false` on success.

### 2A.3 :6910 server-close-equals-consumed; the client drains to EOF

`config_agent.c` (header comment + `tcp_poll`): 6910 sends **zero application
bytes ever** (window-as-grant is pure receive-window pacing). The protocol's only
acknowledgement is connection lifecycle:

- Client streams header+payload and **half-closes (FIN)**; client-EOF is the only
  well-framed end of transfer (`RECV_AWAIT_EOF`).
- Server runs validation/`finish_payload()` **then closes**. **Server-close is
  the client's only sync point**: close after a complete, well-framed transfer =
  consumed/staged. The client must therefore **drain to EOF** (block reading
  until the server closes) rather than fire-and-close — closing early forfeits
  the only consumption signal that exists.
- Bad header / oversize / bytes beyond the frame → server closes **early**; the
  client detects rejection purely as EOF-before-it-finished-sending (no error
  byte on the wire).
- Single session at a time: a second 6910 connection while one is active gets the
  same accept-then-close refusal as 6900 (`tcp_poll`: "busy: refuse").
- Session reaping stays: a swap-armed session that dies is reaped by abort + the
  30 s RX-idle timeout (silicon-observed hang, 2026-07-09) — the Linux daemon
  keeps both.

### 2A.4 TCP_NODELAY on :6900

`platform/src/net_if_lwip.c:253`: `tcp_nagle_disable()` on every accepted
connection — "request/response services: latency > coalescing". The Linux daemon
MUST `setsockopt(TCP_NODELAY)` on accepted 6900 sockets: the protocol is one
short JSON line each way, and Nagle+delayed-ACK interplay adds up to ~40 ms per
exchange, which pyverify's tight verb loops multiply. (6910 needs no equivalent —
the server never sends application bytes.)

### 2A.5 Reply shapes of `commit`/`link`/`macgen` on the DECLARED BASELINE

Normative encoder: `mps3_ctrl_encode_response()` (`common/net_proto.c`). The
uniform failure shape for every verb is `{"ok":false,"err":"<string>"}` (telemetry
alone adds `"lockup"` to its failure line). Success shapes:

| Verb | Success shape | Bare-metal failure `err` strings |
|---|---|---|
| `commit` | `{"ok":true,"slot":"A"\|"B"}` (encoder FAILS CLOSED on ok-without-slot) | `"commit failed"` |
| `link` | `{"ok":true}` | `"bad event"` |
| `macgen` | `{"ok":true,"tx":N,"rx":N,"err":N}` — **`err` is POLYMORPHIC**: u32 ERR_CNT counter on success, diagnostic string on failure. Clients MUST key on `"ok"`, never on `err`'s type | `"bad inject"` |

**The declared baseline (working tree) has NO slave for VPHY (0x44A3) or GENCHK
(0x44A6)** — both pages are reserved, not instantiated — and the OVLSTORE SPI
master drives nothing (QSPI v0.2; plus the D16 hazard forbids commit on the
shared board regardless). The bare-metal firmware **blind-pokes** those pages
(`handle_link`/`handle_macgen` have no presence gate — only `display` has one):
on the classic MB with bus exceptions off, the DECERR is silently swallowed and
`link` answers a fake `{"ok":true}`. That behaviour CANNOT carry to Linux: under
the MBV a DECERR is a real S-mode access fault, and the poke would kill the
daemon (DRIVER_MATRIX §2.9).

**Frozen decline shape where no slave exists** — the `display` precedent
(`coordinator.c`: `{"ok":false,"err":"clcd_kvm not present"}`, the documented
decode-works/handler-declines OFF-build behaviour) generalises:

| Verb | Decline on this baseline |
|---|---|
| `link` | `{"ok":false,"err":"vphy not present"}` — do NOT touch 0x44A3 |
| `macgen` | `{"ok":false,"err":"genchk not present"}` — do NOT touch 0x44A6 |
| `commit` | `{"ok":false,"err":"commit failed"}` (the existing string — overlay store disabled on this baseline; also D16) |
| `display` | success path is LIVE on this baseline (clcd_kvm_0 instantiated): `{"ok":true,"owner":"harness"\|"dut"}`; the `"clcd_kvm not present"` decline applies only to a 0xE4B1C44A-baseline fork |

This is a deliberate, documented behaviour **delta** vs bare metal for `link`
(honest `ok:false` instead of a fake `ok:true` into a DECERR void). Clients key
on `"ok"` (the polymorphic `macgen` `err` already forces that discipline), and
the ethernet wave that instantiates VPHY/GENCHK restores the success paths.

## 3. Per-service detail

### 3.1 smsc911x / LAN9220 (stock driver)
Bare-metal driver is a Zephyr-sequence port (Apache-2.0 notice applies): BYTE_TEST
(0x87654321) sanity, ID check 0x9220, soft reset, FIFO sizing, internal PHY (addr 1)
autoneg, **promiscuous mode on purpose** (`MAC_CR_PRMS` — spec §8.3: shell MAC and DUT
MAC share the one physical port). Two hard-won behaviours the stock Linux driver already
handles internally, listed so nobody re-ports them: (a) TX STATUS FIFO must be drained
or the MAC stops starting frames (the sustained-TX stall); (b) latched INT_STS RX
overrun bits must be acked + RX_DUMP'd or RX wedges. TX status `ES|NO_CARR` (0x8400) is
normal on this board (carrier-sense not driven, full duplex).
**Linux**: `CONFIG_SMSC911X` platform device on the EMC window. Red flag: the shell BD
never wired `eth_int_i` into an interrupt path (poll model); the Linux driver **requires
an IRQ** — the successor shell must route ETH_INT to the MBV interrupt controller, and
the LAN9220 `IRQ_CFG` polarity (IRQ_POL=1) note from BUILD_RESULT applies then.

### 3.2 Coordinator / 6900 (userspace daemon)
Pure dispatch: decode line → handler → encode line (§2 shapes). Handlers touch CLKRST
(reset/set_clk), VPHY (link), GENCHK (macgen), DFXCTL.RM_STATUS (telemetry.lockup),
CLCDKVM (display), and arm swap_fsm / overlay_store. In Linux this is one daemon owning
the 6900 socket, calling into the DFX driver (ioctl/sysfs) for `swap`, MTD for `commit`,
clk framework for `set_clk`, UIO mappings for VPHY/GENCHK/CLCDKVM pokes. `ping` returns
`static_id` (build-time-generated strong symbol today — keep a file/DT property) and the
**last VERIFIED** `rm_id` (deliberately not a live DFXCTL read; mid-swap the register is
transient).

### 3.3 config_agent (userspace daemon)
Receives + validates pushes (§2 framing) into three sinks, chosen by size/registration:
RAM staging (small), QSPI staging (large clearing → CLEARING_STAGE region; large partial
→ inactive A/B slot as scratch, active slot preserved), and **stream-direct ICAP** (Path
3, `MPS3_CFG_AGENT_ICAP_DIRECT`): a large partial (886 KB–1.65 MB) streams straight to
HWICAP as it arrives on 6910 — its `begin()` fails closed unless DFXCTL already confirms
decoupled + RP-in-reset (the swap must be armed first). Under Linux the daemon keeps the
validation/ordering state machine verbatim; the stream-direct sink becomes writes into
the DFX driver's chunked-write interface (§4), and QSPI staging becomes `/dev/mtd`
writes. Note the stream-direct caveat: bytes hit the ICAP before the trailing CRC check
completes — acceptable only because the RP is decoupled and a failed CRC fails the swap
(RP re-isolated); the Linux driver must preserve that containment.

### 3.4 swap_fsm — see §4 (the custom-driver work item)

### 3.5 overlay_store (stock SPI/MTD + userspace logic)
AXI Quad SPI (PG153 layout, FIFO depth assumed 16) driving an SST26VF064B. On-flash
layout (offsets frozen in `overlay_store.h`): header @0, slot A payload @0x010000, slot
B @0x400000..0x780000, clearing-STAGE @0x780000, clearing-CACHE @0x7C0000 (256 KiB
each), flash end 0x800000. SST26 **block-protection is scoped**: WREN+WBPR unlock of
target blocks only, re-lock after every erase/program; every write path ends in a
read-back CRC verify; a len==0 erase is rejected (defect-A guard). Boot flow: read
header → active slot → CRC-verify → stream clearing+partial to ICAP (blocking, pre-loop
by design). Promote STAGE→CACHE is resumable one-sector-per-step (~25–50 ms/step vs the
old ~750 ms blocking tail).
**Linux**: `spi-xilinx` + `spi-nor` (SST26VF064B is in-tree; verify the kernel's SST26
global-unlock behaviour matches the scoped WBPR discipline — spi-nor historically does a
global unlock on SST26, which is a behavioural change to sign off). Model A/B slots +
clearing regions as fixed MTD partitions; port `ovlstore_codec.c` (pure, host-tested)
into the daemon unchanged. **Operational hazard carried over**: slot A/B staging and
`overlay_store_commit()` are still not to run on the real board until the D16 flash
collision is owned (memory: qspi-shared-flash-hazard).

### 3.6 clkrst (split disposition)
Three reset bits (RESET_CTRL: dut/rp/dbg, 1=released, async-assert/sync-deassert in
RTL) + DUT clock. The clk_wiz `DUT_CLK_SEL/DUT_CLK_DRP` CLKRST registers are **inert**
(RTL never wired the DRP); the real retune is the Clocking Wizard's own AXI4-Lite DRP @
0x44AB (PG065 sequence: CFG_REG0 M/D → CFG_REG2 O → LOAD|SEN → poll LOCKED; presets
25/50/100 MHz off a 1000 MHz VCO). The reset pulse hold is calibrated to the 3-FF
dut_clk reset synchronizer at the slowest DUT clock — a Linux reset controller must keep
a minimum assert width, not a bare toggle.
**Linux**: MMCM → `clk-xlnx-clock-wizard` (set_rate); RESET_CTRL → owned by the custom
DFX driver (rp_resetn is part of swap sequencing and must not be host-writable
mid-swap), with dut/dbg reset exposed (reset controller or sysfs) for the daemon's
`reset` verb and the SWD daemon's srst.

### 3.7 swd_server (UIO + daemon) — HISTORICAL, superseded by [DEV-10]
The block at 0x44A7 is `jtag_bb` now; what follows describes the RETIRED SWD
service and is kept because its byte encoding is what a JTAG successor inherits.

remote_bitbang chars → SWDBB DRIVE (SWCLK/SWDIO_O/SWDIO_OE) / SAMPLE (SWDIO_I) register
wiggling; `r/s/t/u` reset chars drive srst via CLKRST dbg_resetn (sense inversion noted
in source as TODO — confirmed at bring-up). Bit order is pinned by OpenOCD's source, not
the board. Fully gated off during a swap (`swd_gated`). One MMIO write/read per protocol
byte: UIO mmap from a userspace daemon is amply fast (OpenOCD paces it).

### 3.8 xvc_server (UIO + daemon)
XVC v1.0 → DBGBR (XAPP1251 AXI-to-BSCAN layout: LENGTH/TMS/TDI/TDO/CTRL @
0x00/04/08/0C/10, ≤32 bits per GO kick, TDO read only after GO self-clears — the access
ORDER is conformance-tested). Max vector 2048 bits (buffers 3×256 B). Mid-swap gating is
**stall-not-error** for `shift:` (getinfo/settck still answered) so hw_server survives a
swap. Same disposition as SWD: UIO mmap + daemon; keep the bounded fail-closed CTRL
poll (a dead bridge drops the connection, never wedges).

### 3.9 uart_over_eth (UIO + daemon)
Three streams in ONE UARTBR block: U0_TXRX @0x00, U1_TXRX @0x08, SWO_RX @0x10 (RO),
FIFO_STATUS @0x14, SWO_CFG @0x18 (divisor = (n+1) dut_clk cycles, ≥8× oversample;
divisor change requires ENABLE off→on). Data regs are {valid bit8, data[7:0]} pop/push.
Raw TCP byte relay, one client per port. **Linux**: simplest faithful port is the UIO
daemon (the relay loop is ~200 lines today); a custom tty/serdev driver + ser2net is a
later nicety, not needed for wire-compat.

### 3.10 VPHY / GENCHK / TELEM / GPIO / CLCD / CLCD_KVM
- **VPHY** (0x44A3): `link` verb writes LINK_EVENT force_down/pulse. Daemon poke.
- **GENCHK** (0x44A6): CTRL gen_en/chk_en + one-hot INJECT (bad_fcs/runt/giant/ifg/
  dribble) + three RO counters, **clear-on-enable-rise** (not free-running) — the host
  reasons in deltas within one enabled session. Daemon poke.
- **TELEM** (0x44A5): inputs grounded in the BD, I2C engine never written, pads
  MCC-owned, MCC refuses reads — four independent dead-ends. Disposition: none. Keep the
  loud failure.
- **GPIO** (0x44AA): IN/OUT/OE/OWN, per-bit host-vs-DUT ownership mux (OWN=1 → host).
  Non-standard layout → tiny custom gpiochip or UIO. Heartbeat = bit0.
- **CLCD** (0x44AC): byte-FIFO 8080 master to the HX8347-D; **no framebuffer in the
  shell** (panel GRAM is the framebuffer) and CMD/DATA writes are silently dropped on
  FIFO-full (deliberate: the slave must never back-pressure the bus) — so the writer
  polls STATUS.level. The firmware driver is a 600-cell text renderer with dirty-cell
  diffing, 256 B/pass, 250 ms cadence. Disposition: userspace daemon over UIO; do not
  attempt fbdev/DRM against a byte-command FIFO.
- **CLCD_KVM** (0x44AD): **no slave on the shipped shell** — access DECERRs silently
  (MB bus exceptions off). Contract is frozen (write-gated src_sel via src_sel_we; EVENT
  is W1C, never read-to-clear, never blind-clear; the firmware handover rule: repaint on
  `harness_gained | panel_reset_done`). Keep every access gated until the KVM-bearing
  static lands; the `display` verb's decline path is the documented OFF behaviour.

### 3.11 diag (schema survives; JTAG path does not)
32-word magic-stamped mailbox pinned at the top of the LMB (base is LMB-size-dependent
and address decode ALIASES — readers scan for the magic, never trust a constant).
Refreshed every superloop pass; the overlay-store phase hook writes it directly so a
QSPI wedge is locatable while the loop is stalled. Two consumers: the `diag` verb
(keys frozen, §2) and JTAG-MDM post-mortem reads **during a swap** (the only path then).
Linux: the verb's JSON schema must be preserved by the daemon; the post-mortem story
must be re-provided differently (pstore/ramoops in DDR4, or a driver-owned BRAM
mailbox) — see red flags.

## 4. The ICAP/DFX path — the one genuine custom kernel driver

There is **no in-tree fpga-manager driver for AXI HWICAP** (in-tree Xilinx managers
target Zynq devcfg/FPGA-manager and the slave-serial/SPI paths) — flagged per the
tasking, not solved here. The clean shape is an fpga-region composed of a custom
**fpga-manager (axi-hwicap)** + a custom **fpga-bridge (dfx_ctl)**, with the swap
sequencing (today `swap_fsm.c` + the pure `swap_fsm_transitions.c` table, which ports
verbatim — it is already hardware-free) living in the driver or in the daemon above an
ioctl that enforces ordering. What the driver must do, precisely:

**Sequencing (one swap):**
1. Gate/quiesce consumers (SWD/XVC/UART/link — under Linux: bridge-disable notifies the
   daemons, or the daemon self-gates before invoking the driver).
2. Assert `DFXCTL.DECOUPLE_EN`, drive AXI shutdown (`DFXCTL.SHUTDOWN`), hold
   `CLKRST.rp_resetn` low. **Bounded-poll** `DFXCTL.STATUS.{decoupled,rp_in_reset}` to
   confirm; timeout = swap failed (never park).
3. Stream the **outgoing RM's clearing** bitstream into HWICAP (the shell always holds
   it: boot-seeded from the greybox baked into the image, thereafter the QSPI
   clearing-CACHE region or RAM arena).
4. Accept + stage the incoming pair (clearing then partial; validated per §2 *before*
   any ICAP write — except stream-direct, where decouple containment substitutes).
5. Stream the incoming **partial** into HWICAP.
6. Release: deassert decouple/shutdown, release rp_resetn; bounded-poll STATUS confirms.
7. **Verify with the RP connected**: bounded-poll `DFXCTL.RM_STATUS.rm_id_valid`, then
   compare `DFXCTL.RM_ID` against the target rm_id **from the partial's own wire header**
   (numeric — no name table in firmware; names are host-side `rm_list.tcl`).
   Distinguish "not yet valid" (keep polling to timeout) from "valid but wrong"
   (fail immediately).
8. On verify-fail: **RE-ISOLATE** (decouple + rp_reset + shutdown again) before
   reporting failure — a failed swap has exposed an unknown RM and must be put back in
   the box. Failure invariant: RP parked decoupled + in reset.
9. On success: promote the staged incoming clearing to the resident cache (QSPI
   STAGE→CACHE, sector-chunked, read-back CRC), update current rm_id, respond.

**Swap-scoped invariant (RESTORED 2026-07-16 — dropped in the first draft of
this document, present in TRANSPLANT_CONTRACT §9.4): the shell MUST reset any
static-side `hostio4_target` on EVERY swap.** Proven by
`mps3-nanosoc-platform/tests/hostio4_hotswap` (L2a park analysis + L2b live-swap
demo): a partial reconfiguration deletes the RP-side hostio4 *controller*
mid-transaction, and **no constant the DFX decoupler can drive returns the
static-side target to idle from every state** — the best candidate
(`ioreq1=0,ioreq2=0`) still wedges 5 of its 9 states, and the decoupler's
`ioack` safe value rescues **nothing** (irrelevant under either value). A target
wedged by swap *N* asserts `ioack` permanently and deadlocks the FIRST hostio4
transaction of DUT *N+1* (on nanoSoC: the bootrom's ADP banner) —
intermittently, depending on where the swap lands. Two proven recoveries, both
green across all 9×2 states: reset the target, or drive three `ioreq2` escape
toggles. **Placement in the sequence: between steps 5 and 6** — while the RP is
still decoupled + in reset, before release — so the new RM can never see a
stale-state target. No `hostio4_target` is instantiated in the current static
shell; the invariant binds the swap engine (driver ioctl path AND daemon FSM)
from the day the hostio wave lands one, and the driver design must carry the
hook now so it is not rediscovered as an intermittent silicon hang later.

**HWICAP mechanics (all HW-proven, do not rediscover):**
- CR bit0 = WRITE, bit1 = READ (`xhwicap_l.h`); these were once swapped and the swap
  stalled with RM_ID stuck at greybox — HW-confirmed on the KU115 shell.
- Two write protocols mirror the core's `C_MODE`: **lite** (shipped: no write FIFO, one
  CR=WRITE StartConfig per config word) vs **FIFO** (poll WFV vacancy, push ≤vacancy
  words into WF, one StartConfig, poll CR.WRITE self-clear, refill). Every wait is
  bounded and fail-closed: an ICAP write that never completes is a failed swap, never a
  silent success.
- **Byte order**: the DFX `.bin` stores config words big-endian (sync word bytes
  AA 99 55 66); MicroBlaze — and MBV rv32 — are little-endian, so a native memcpy
  byte-swaps every word and ICAP never sees 0xAA995566. Pack MSB-first
  (`b0<<24|b1<<16|b2<<8|b3`), one shared primitive (`mps3_hwicap_pack_word`), and note
  the 6910 stream can split a word across TCP segments (the direct sink carries a
  ≤3-byte residue buffer).
- **Chunking**: bare-metal pushes ≤256 words (`MPS3_HWICAP_CHUNK_WORDS`) per poll purely
  to keep one superloop pass short vs lwIP's timers. Under Linux that exact number is
  moot, but chunk-with-scheduling-points survives: a 1.65 MB partial at one
  register-write-per-word (lite mode) is millions of uncached MMIO writes — the driver
  must cond_resched()/sleep between chunks, and status/progress (bytes written,
  equivalent of `icap_bytes`) must be observable mid-swap because the 6900 response is
  held until the swap settles (~seconds; host tools tolerate this today, ~570 KB/s
  end-to-end proven).
- **Post-DESYNC EOS**: after the partial's DESYNC, a bounded wait for `HWICAP_SR_EOS`
  is best-effort — whether EOS asserts on this axi_hwicap build is still an open
  question (`icap_sr_last`/`icap_eos_status` diag words exist to answer it). Driver
  should capture the same raw SR, not gate success on EOS alone.
- **Timeouts**: RX-idle 30 s on the two AWAIT states (re-armed on every byte of
  progress — bounds silence, never a slow transfer); bounded confirm-polls on
  decouple/release/verify/re-isolate. All fail closed to the parked-decoupled state.
- **Placement risk** (design-around, from the tasking): keep ICAP+RP-pblock (SLR0)
  separable from DDR4 banks 49–51 (SLR1) in the successor shell so the DFX path never
  crosses into the memory controller's SLR.

## 5. Red flags / honest gaps

1. **LAN9220 IRQ is not wired** in the proven shell BD (poll model). Linux `smsc911x`
   needs a real interrupt: successor BD must route ETH_INT (and set IRQ_POL) — a
   boundary change to plan, not assume.
2. **No in-tree AXI-HWICAP fpga-manager** — the §4 driver is real new kernel work, and
   it must reproduce sequencing that took multiple silicon-found bugs (CR bit swap, byte
   order, TX/RX FIFO stalls, fail-closed timeouts) to get right. The pure transition
   table and its host tests port as-is; reuse them.
3. **Held `swap` response semantics**: pyverify sends one JSON line and waits (possibly
   tens of seconds) for one reply. The Linux daemon must keep the connection open and
   quiet — no interim lines, no timeout — or host tooling breaks.
4. **diag JTAG post-mortem is lost** on a Linux/DDR4/MBV target (it is an LMB+MDM
   mechanism, and it is the ONLY diagnostics path readable mid-swap today). Needs an
   explicit successor (ramoops/pstore or a driver mailbox) before anyone relies on
   Linux-era wedge triage.
5. **spi-nor vs scoped SST26 block-protect**: firmware unlocks only target blocks and
   re-locks after; kernel spi-nor's SST26 handling is coarser. Sign off the behavioural
   difference; and the D16 shared-flash collision hazard still forbids A/B commit runs
   on the current board.
6. **Promiscuous/shared-port model**: shell MAC runs promiscuous because two MACs share
   the port (§8.3). Under Linux, decide deliberately (promisc + a filtering daemon, or
   macvlan-style separation); default kernel behaviour will silently differ.
7. **CLCDKVM register page does not exist on the shipped shell** (silent DECERR). Any
   Linux bring-up against today's bitstream must keep the presence gate.
8. **Register-offset provenance caveats carried in-source**: HWICAP/QSPI offsets are
   "PG134/PG153-shaped, confirm against the generated headers"; GENCHK's frozen layout
   deliberately contradicts an older cocotb draft. Confirm-at-integration items, not
   facts.
9. **Two mains exist** (`harness_app` spike vs `platform/src`); anything derived from
   `harness_app` (INTC/XTmrCtr usage, 1 kHz tick) describes the abandoned variant.

## CHANGELOG

- **2026-07-16 (judge-fix wave):** added §2A — the frozen 6900/6910 wire
  contract (second-client accept-then-EOF refusal semantics + the fpgahub
  `busy`/`offline`/`wedged` state table that depends on them; unknown-verb
  `{"ok":false,"err":"unknown op"}` reply-and-stay-open + the fpgahub
  `{"op":"stats"}` probe that latches on `ok:false`; 6910
  server-close-equals-consumed + client drain-to-EOF + zero server bytes;
  TCP_NODELAY on 6900; commit/link/macgen reply shapes on the declared
  baseline incl. the frozen no-slave decline shapes for `link`/`macgen`).
  Restored the `hostio4_target`-reset-on-every-swap invariant into §4
  (TRANSPLANT_CONTRACT §9.4 / tests/hostio4_hotswap), placed between
  sequencing steps 5 and 6.
- **2026-07-15:** initial audit.
