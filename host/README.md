# host — the Linux/Python "brain" (A4 scope)

This is the external host side of "disaggregated PYNQ"
(ARCHITECTURE_SPEC.md §2): everything that would be the hard Cortex-A + Linux
+ Jupyter half of a PYNQ board, here running on an external **Raspberry Pi 5
or shared lab server** instead, talking to the static shell over Ethernet
(and, for first bring-up / recovery, JTAG).

**Status:** Phase-0.5 + W-PUSH/W-PKG + W-HOST-MISC (docs/NEXT_WAVE_PLAN.md).
Everything host-side is now **real code**: manifest parsing, CRC32, wire
framing, the TFTP PUT / raw-TCP bitstream send (`pyverify.pusher`), the
OpenOCD/Vivado launch wrappers (subprocess with an injectable runner seam),
the tender plugin's deploy runner (now CI-covered against the reference
fpgahub checkout — `tests/test_tender_plugin.py`), client-side `set_clk`
preset validation, the `host/openocd/` configs, and the `Mps3Board` session
facade — all unit-tested against local socket sinks and fake collaborators
(`host/pyverify/tests/`, no hardware needed). What remains outstanding is
only the *target*: a live shell (A3 firmware) listening on the
net-protocol.md ports — a deploy against an unreachable host fails at the
network layer with a clean CLI error, not at a stub.

## The four pieces, and how they relate

```
                 ┌─────────────────────────────────────────────┐
                 │                  tender                      │
                 │   fpgahub board-node: JTAG program, MCC       │
                 │   image mgmt, reboots (host/tender/)          │
                 └───────────────┬───────────────────────────────┘
                                 │ (JTAG / USB-MSD / MCC serial — bring-up
                                 │  + recovery only, spec §16)
   ┌─────────────────────────────▼──────────────────────────────────┐
   │                        pyverify                                  │
   │  host/pyverify/  — the Python verification library                │
   │  board.py    Mps3Board — the "PYNQ experience" session facade      │
   │  client.py   control channel  (net-protocol.md TCP 6900)          │
   │  overlay.py  manifest load/validate (overlay-manifest.md)         │
   │  pusher.py   bitstream header framing + TFTP PUT / raw-TCP send    │
   │              (net-protocol.md UDP 69 / TCP 6910)                  │
   │  swap.py     validate→push→swap→reattach orchestrator             │
   │              (ReattachPlan.apply() — executable re-attach)         │
   │  console.py  UART/SWO TCP consoles (6930-6932)                    │
   │  debug.py    OpenOCD/XVC wrappers (6921 rbb live; 6920 dormant)    │
   │  cli.py      `python -m pyverify.cli deploy`                       │
   └───────┬─────────────────────────────────────────────┬────────────┘
           │                                               │
   ┌───────▼───────────────┐                    ┌──────────▼──────────┐
   │  socket_harness         │                    │      openocd          │
   │ host/socket_harness/     │                    │ host/openocd/          │
   │ one endpoint registry,    │                    │ remote-bitbang + XVC    │
   │ Session, xsdb CSR access,  │                    │ configs for the shell's  │
   │ pure-Python pty bridge      │                    │ debug servers            │
   └───────────────────────────┘                    └────────────────────────┘
```

Every arrow above ending in "over Ethernet" is a client of
`docs/contracts/net-protocol.md`; every arrow touching an overlay directory
is a client of `docs/contracts/overlay-manifest.md`. This directory produces
no servers — the shell (A3 firmware) is the only server in this system.

### tender (`host/tender/`)

The fpgahub board-node instance for this platform (ARCHITECTURE_SPEC.md
§4.3: "mounts `V2M_MPS3` over USB, writes shell `.bit` + `images.txt`,
issues MCC `reboot`. Only for shell (re)builds."). Per
IMPLEMENTATION_PLAN.md §0.2 this reuses fpgahub's existing node model and
two already-hardware-proven plugins (`sd_install`, `mps3_mcc_reboot`);
`host/tender/fpgahub_mps3_plugin.py` adds `program_full` (openFPGALoader
JTAG, real fpgahub `ProgramPlugin`) and `platform_deploy` (overlay
push+swap via pyverify, exposed as a CLI an fpgahub manifest `Action` can
run — it doesn't fit fpgahub's `ProgramPlugin` ABC; see that file's
docstring). JTAG/MCC is the **bring-up and recovery** path — the "inner
loop" once the shell is networked is entirely pyverify+pusher over
Ethernet (spec phase 3 onward; IMPLEMENTATION_PLAN.md Phase 3.1: "retire
JTAG path to fallback/recovery").

### pyverify (`host/pyverify/`)

The Python verification library (ARCHITECTURE_SPEC.md §14 phase 10),
installable-shaped (`pyproject.toml`, package `mps3-pyverify`). Speaks the
client side of every host-facing contract. See `host/pyverify/pyverify/__init__.py`
for the module map and `host/notebooks/demo.md` for how the pieces compose
into a session.

### pusher (`pyverify.pusher`)

Implements the net-protocol.md bitstream header (magic/ver/kind/rm_slot/
static_id/rm_id/len_words/crc32) **and** the network send: a hand-rolled,
stdlib-only TFTP PUT client (RFC 1350 octet mode, 512-byte lock-step
blocks, per-packet timeout + retransmit, ephemeral-TID handling) plus a
raw-TCP `sendall` alternative — all tested against local socket sinks
(`host/pyverify/tests/test_push_framing.py`, `test_pusher_send.py`).

Packaging (W-PKG, resolves the old "sibling directories" open question):
the pusher lives *inside* the package as `pyverify/pusher.py`, so
`pip install -e host/pyverify` carries it everywhere. The transitional
re-export shim at `host/pusher/push.py` has been **retired** — every
consumer, `tests/integration/test_swap_sequence.py` included, now imports
`pyverify.pusher` directly, and there is exactly one framing
implementation. The `Pusher` protocol in `swap.py` survives as a
test-injection seam, not a package boundary.

### console

Three ways to reach the UART0/UART1/SWO TCP streams (6930/6931/6932,
ARCHITECTURE_SPEC.md §11 "Host side"):

- **Interactive, no dependencies:** `python -m socket_harness console uart0
  --pty /tmp/mps3-console/uart0 --host <ip>` — a pure-Python pty↔TCP bridge.
- **Interactive via ser2net/socat:** generate the config from the one endpoint
  registry rather than hand-maintaining it: `python -m socket_harness emit
  ser2net` / `emit socat`. (The hand-written `host/console/` examples these
  replaced have been retired — they drifted from the registry, and one of them
  documented a ser2net `pty` accepter syntax that was never verified.)
- **Scripted:** `pyverify.console.ConsoleReader` (assert on boot banners,
  scrape test output) with no pty at all.

### openocd (`host/openocd/`)

`swd_remote_bitbang.cfg` — the file-form of the OpenOCD invocation
`pyverify.debug.OpenOcdRemoteBitbangConfig` builds programmatically
(remote_bitbang → shell TCP → DUT debug, net-protocol.md), with a
documented DPIDR-read smoke test. **On the fielded JTAG-bridge shell the
live port is 6921 (`remote_bitbang`, JTAG); the older SWD `swd_server` on
6920 is dormant** — see the `jtag` endpoint in net-protocol.md. Its `README.md` also covers the **XVC
path** (TCP 2542), which is consumed by *Vivado hw_server*
(`open_hw_target -xvc_url`), not OpenOCD — exact Tcl included, matching
`pyverify.debug.XvcTarget`.

## The loop (ARCHITECTURE_SPEC.md §14 phase 10 acceptance)

"Select DUT -> load firmware -> run test -> check", concretely — one
object, `pyverify.board.Mps3Board` (the "PYNQ experience" facade):

```python
with Mps3Board("192.168.10.101") as board:
    result = board.deploy("nanosoc")          # 1+2: select + load
    result.reattach.apply()                    #      reopen consoles (+ hints)
    board.uart0.assert_contains(b"nanosoc boot")  # 3: run test
    telem = board.telemetry()                  # 4: check (lockup pin only —
                                               #    see below: no power sensor)
```

or piece by piece (what the facade wires together):

1. **Select DUT** — `Overlay.load(path)`, validated against the shell's
   live `static_id` (`shell.ping()`).
2. **Load firmware** — `SwapOrchestrator.deploy(overlay)`: push
   clearing+partial (`pyverify.pusher`) -> `swap` RPC (client) ->
   re-attach plan.
3. **Run test** — re-attached SWD (OpenOCD)/console
   (`ConsoleReader`)/ILA (XVC) drive the DUT; scrape/assert output.
4. **Check** — assertions on console output, plus `shell.telemetry()` for the
   DUT-lockup pin.

   > ⚠ **`telemetry` always fails, by design** (net-protocol.md v0.6):
   > `{"ok":false,"err":"no power sensor","lockup":<bool>}`. This platform has
   > **no power sensor reachable by any path**, so `mv`/`ma` are not keys of
   > the protocol and `TelemetryResponse` has no such fields — they are
   > *absent*, not zero. It reports the raw `dut_lockup` pin on that failure
   > line (the protocol's one declared carve-out to the uniform failure shape).
   > `lockup` is only meaningful for RMs that drive the pin — `nanosoc_multicore`
   > does; `nanosoc`, `eth_ss` and the OOC RMs tie it to 0, where `lockup=False`
   > means "cannot report lockup", not "the DUT is healthy". **Your pass/fail
   > verdict comes from the console output, not from telemetry.**

See `host/notebooks/demo.md` for this as an actual (sketched) notebook cell
sequence.

## Site configuration (environment)

No hub hostname, board name or checkout path is baked into the host tools.
Each site sets its own in the environment (a git-ignored `set_env.local.sh` is
the usual place). Unset, the tools use the neutral defaults below or refuse.

| Variable | Used by | Default when unset |
|---|---|---|
| `MPS3_HUB` | pyverify (lease, console, sd, swd, netboot), soak/sweep scripts | none: hub verbs refuse; SWD runs OpenOCD locally |
| `MPS3_ON_HUB=1` | same | off (set when already running on the hub) |
| `MPS3_HUB_GROUP` | lease / hub commands (`sg <group>`) | `fpga` |
| `MPS3_CHASSIS` | `pyverify lease status` | `mps3` |
| `MPS3_LEASE_TARGET` | lease acquire/release, `sd field`, console share | `mps3_pl` |
| `MPS3_TTY_DIR` | fpgahub TTY directory (console lane 2, MCC lane 0) | `/dev/$MPS3_LEASE_TARGET` |
| `MPS3_LEASE_HOLDER` | lease verbs | none (required where a holder is needed) |
| `MPS3_REPO_DIR` | `pyverify.swd`, `scripts/qspi_*.py`: this repo's path ON THE HUB | this checkout's root |
| `MPS3_MINT_ARCHIVE` | `fielded/<id>/fetch_fielded.sh` hub fallback (`HUB_DIR` = `$MPS3_MINT_ARCHIVE/<id>`) | none: hub fallback refuses |
| `FPGAHUB_SRC` | `tests/test_tender_plugin.py` (fpgahub checkout `src/`) | unset: those tests skip |

The board's data-plane address `192.168.10.101` is the product default and is
not site configuration. External source trees for the simulation benches
(`SOCLABS_NANOSOC_SOC_DIR`, `SOCLABS_AHB_QSPI_DIR`, `ARM_IP_LIBRARY_PATH`,
`ETH_SS_HOME`, `NANOSOC_M0_SOC_SRC`, `HOSTIO4_HOME`, ...) come from the repo-root
`tools.env` (see `tools.env.example`).

## Board-node integration (fpgahub)

Reference-only, do not modify: the fpgahub sources. The tender
*is* an fpgahub node; `host/tender/README.md` has the concrete deployment
steps and the config.toml shape (mirroring
`fpgahub/packaging/templates/config.server.toml`'s existing, commented MPS3
example block).

## Re-attach after a swap (ARCHITECTURE_SPEC.md §6.3)

`SwapOrchestrator.deploy()` returns a `ReattachPlan` — descriptive fields
first, and now executable via `plan.apply(executors=...)` with injectable
per-step executors, because each re-attach step needs a different tool:
reload the new RM's `.ltx` (Vivado/XVC, `pyverify.debug.XvcTarget`),
re-run SWD line reset + DP connect (OpenOCD,
`pyverify.debug.OpenOcdRemoteBitbangConfig`), and reopen the console
sockets (`pyverify.console.ConsoleReader`, trivially — just a fresh TCP
connect). `apply()`'s defaults reflect exactly that split: the console
step reconnects for real (the plan records the shell host); the ltx/swd/
vphy steps return their descriptive hint unless the caller injects a real
executor (e.g. `{"swd": lambda plan: launch_openocd(cfg)}`) — matching
spec §6.3's framing of re-attach as independent, tool-specific steps.

## Net-protocol / overlay-manifest ambiguities — flagged for A6

These are called out in more detail at their point of use (module
docstrings), collected here for one-pass review:

1. **Clearing-bitstream push timing isn't fully operational in
   net-protocol.md.** The "Swap sequence" section says the *coordinator*
   supplies the currently-loaded RM's clearing bitstream server-side, but
   the TFTP port row says TFTP carries "partial/clearing bitstream push"
   (both kinds go over the wire). `pyverify.swap.SwapOrchestrator` assumes:
   host pushes the **new** RM's `{clearing.bin, partial.bin}` pair ahead of
   `swap` (so it's cached shell-side for the *next* swap away from it);
   the shell uses whatever clearing bitstream it already has cached from
   the *previous* load (starting from the greybox's, shipped with the
   shell) to actually clear *this* swap. IMPLEMENTATION_PLAN.md's D16
   ("shell coordinator owns clearing-bitstream state, host mirrors it")
   is consistent with this reading but doesn't spell out the push-timing
   mechanics — confirm against A3's actual config-agent implementation.
   (`pyverify/swap.py`)
2. **`overlay-manifest.md`'s `len` (bytes) vs. the wire header's
   `len_words` (32-bit ICAP words) are different units of the same
   quantity**, both nominally describing "how big is this bitstream". Not
   a bug — `pyverify.pusher.frame_bitstream` derives `len_words` from the
   actual payload rather than trusting the manifest's byte count — but the
   two contracts should probably say so explicitly rather than leaving it
   for an implementer to notice. (`pyverify/pusher.py`)
3. **`set_clk`'s `preset` values aren't enumerated** in net-protocol.md
   (the one example is `"25mhz"`) — still open as OPEN_ISSUES.md **I16**
   (A6-owned). Interim (W-HOST-MISC): client-side validation against
   `pyverify.client.DEFAULT_CLK_PRESETS` = `("25mhz", "50mhz", "100mhz")`
   — a mirror of `firmware/clkrst/clkrst.c`'s *placeholder* table — so a
   typo raises `ValueError` without a round trip. Split by design:
   `Mps3Board.set_clk` (the user-facing facade) validates **by default**,
   with `presets=None`/custom-sequence as the escape hatch; the raw
   `ShellClient.set_clk` stays wire-transparent unless `presets=` is
   given, because the conformance suites (`tests/firmware_logic/
   test_json_golden.py`, fakeshell tests) deliberately round-trip unknown
   presets through it to prove server-side rejection. When I16 closes
   with the real DRP table, update the tuple in lockstep.
   (`pyverify/client.py`, `pyverify/board.py`)
4. **Naming collisions with fpgahub, not with each other**: fpgahub's
   `ProgramPlugin.accepts_overlay` refers to a PYNQ device-tree `.dtbo`;
   fpgahub's `fpgahub.bitstream.BitstreamHeader` is a Xilinx `.bit` ASCII
   header. Both are unrelated to this platform's "overlay"
   (clearing+partial+manifest triple) and "bitstream header" (ICAP
   partial framing). Purely terminological, flagged so nobody conflates
   them when wiring `host/tender/` into a real fpgahub install.
   (`host/tender/fpgahub_mps3_plugin.py`)
5. **`platform_deploy` doesn't fit fpgahub's `ProgramPlugin` ABC** (the
   dispatcher pre-parses a Xilinx `.bit` header before calling the plugin;
   our DFX partial has none). Implemented instead as a `pyverify.cli`
   subcommand fpgahub can shell out to via a manifest `Action` — open
   question whether that's the permanent shape or whether fpgahub should
   grow a dispatch verb for "network overlay deploy" upstream (out of this
   repo's read-only scope for fpgahub itself).
   (`host/tender/fpgahub_mps3_plugin.py`)
6. **ser2net's exact `pty` accepter syntax for "local pty <- remote TCP"
   was never verified** against a real ser2net/gensio install. **Resolved by
   removal:** the unverified example config is gone, `socket_harness emit
   ser2net` generates one from the registry, and `socket_harness.console_bridge`
   is a pure-Python bridge that needs no ser2net install at all.
   (`host/socket_harness/console_bridge.py`)

## Running the tests

```sh
cd host/pyverify
python3 -m pytest -q     # 165+ tests; sockets only on 127.0.0.1 ephemeral
                         # ports (in-test TFTP/TCP sinks), no hardware
```

`tests/test_tender_plugin.py` (the tender-plugin coverage) additionally
needs a **Python ≥ 3.11** interpreter with `pydantic` installed — fpgahub's
own floor (`tomllib`, `pydantic>=2.6`); under anything older it skips as a
module with a precise reason rather than failing. It imports the plugin
from `host/tender/` and fpgahub from the reference checkout
(`FPGAHUB_SRC` env var overrides the default path).

Or as an installed package (what a tender/Jupyter host does):

```sh
python3 -m venv .venv && .venv/bin/pip install -e host/pyverify
.venv/bin/python -m pyverify.cli deploy --host 192.168.10.101 --overlay overlay/nanosoc
```
