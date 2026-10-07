# edge_notes.md — how `pyverify.edge` slots into the tender/fpgahub shape

Reference-only companion to `README.md` in this directory. Written for OPEN_ISSUES
**I3** / `docs/HARDWARE_HUB_INTEGRATION.md`, once `host/pyverify/pyverify/edge.py`
existed and needed a place to say "here is where this actually plugs in on the
Pi 5 tender" without cluttering that module's own (already long) docstring.

## 1. `pyverify.edge` is a model, not the wire server

`pyverify.edge.EdgeDeviceApi` is **not** `edge-wrapperd` (hardwarehub.md §3/§5.1)
— it never binds `WRAPPER_SOCK`, never verifies a `SignedGrant`, never talks
`SO_PEERCRED`. It is the host-side (pyverify/tender) *model* of the same LOW-tier
method surface: the enumeration table, the two-level status assembly, and the
reset-kind -> invalidation-set computation, as pure/injectable-seam Python. Two
reasons that's useful even before a real edge-wrapperd exists for this platform:

1. **Today:** `pyverify`/`tender` code (and CI) can reason about "what channels
   does this board have", "what would a `dfx-swap` invalidate", etc. without a
   board, a socket, or fpgahub installed — exactly the properties the rest of
   `host/pyverify/` already has (see `README.md`'s "Status" note).
2. **Later:** if/when a real `edge-wrapperd`-equivalent is stood up for the MPS3
   tender (hardwarehub.md Phase 0/1, §8.1), its JSON-RPC method handlers can be
   *literally* `EdgeDeviceApi(...).dispatch(method, params)` wrapped in the
   auth/transport layer (grant verify, `SO_PEERCRED`, `AF_UNIX` framing) that
   this module deliberately does not implement. Concretely:

   ```python
   # sketch of a real edge-wrapperd request handler, NOT part of this repo
   api = EdgeDeviceApi(
       board=BOARD_NAME,
       shell=connected_shell_client,       # real pyverify.client.ShellClient
       jtag_probe=real_openocd_done_probe, # real OpenOCD/xsdb wrapper
       known_rm_names=deployed_rm_registry,
   )

   def handle_request(method, params, grant=None):
       if method in PRIVILEGED_METHODS:
           verify_grant(grant)             # edge-wrapperd's job, not ours
       return api.dispatch(method, params)
   ```

   `dispatch()`'s method names (`enumerate.channels`, `status.fpga`, `reset`)
   match hardwarehub.md §5.3's endpoint catalogue verbatim, so no renaming is
   needed at that boundary.

## 2. Channel -> `wg0`-port map

`pyverify.edge.MPS3_CHANNELS` (`docs/HARDWARE_HUB_INTEGRATION.md` §2) is the
per-board channel table; once a board is enrolled in the Hub, every port below
is reachable only on the Pi's `wg0` address (hardwarehub.md §5.6, layer 1):

| Channel id | handle | gated by | transport | port(s) | backing |
|---|---|---|---|---|---|
| `console` | `uart` | `shell` | tcp | `6930` | shell UART-over-Eth (boot monitor, net-protocol.md UART0) or FT4232 |
| `dut-uart` | `uart` | `rp` | tcp | `6931` | shell UART-over-Eth (application/DUT console, net-protocol.md UART1) |
| `swo` | `uart` | `rp` | tcp | `6932` | shell SWO relay |
| `mgmt` | `mgmt` | `shell` | tcp | `6900` (+ `69`, `6910`) | LAN9220 shell services (control/status + TFTP/raw config push) |
| `dut-net` | `dut-net` | `rp` | vxlan-l2 | n/a (VNI, not a TCP port) | virtual-PHY <-> DUT MAC <-> LAN9220, into `board-<id>` netns |
| `jtag` | `jtag` | — (*is* config) | tcp | `3333`/`4444`/`6666` | FT2232 ch A -> KU115 config TAP (OpenOCD gdb/telnet/tcl), **conflicts with `xvc`** |
| `xvc` | `xvc` | `shell` | tcp | `2542` | same FT2232 ch A -> shell Debug Bridge, **conflicts with `jtag`** |
| `swd` | `swd` | `rp` | tcp | `3333` (J-Link) or `6920` (shell SWD-over-Eth) | soft Cortex-M in the RP; two backings, pick per site |

This is the same shape as the Zynq boards' `uart`/`ps`/`pl`/`jtag`/`swd` map
(hardwarehub.md §3.1 table) — the Hub's existing wg0-port-publishing and
per-lessee `AllowedIPs`-narrowing machinery (hardwarehub.md §4.2.1) applies
unchanged; only the port numbers and the `shell`/`rp` gating vocabulary differ
from the Zynq `bitstream`-gated model.

## 3. openFPGALoader / MCC as the edge's *privileged* verbs

`host/tender/fpgahub_mps3_plugin.py`'s two out-of-band backends are, in
Hub-Device-API terms, the concrete implementations behind the **G**
(grant-required, hardwarehub.md §3.0) control verbs for this board:

| Hub verb (hardwarehub.md §3.4/§5.3) | This platform's backend | Privileged? |
|---|---|---|
| `reset {kind:"mcc-reconfig"}` (full shell rebuild) | `fpgahub.reset_plugins.mps3.Mps3MccReboot` (MCC USB-MSD, reference — do not modify) | **G** — rebuilds the whole board; invalidates every channel (`pyverify.edge.reset("mcc-reconfig")`) |
| out-of-band full program (bring-up/recovery) | `ProgramFullOpenFpgaLoader` (`fpgahub_mps3_plugin.py`, this repo) — `openFPGALoader` over the tender's FT2232 JTAG | **G** — the same "load a literal bitstream" verb the Zynq boards' `vivado_jtag` plugin serves, just via `openFPGALoader` since the Pi 5 has no Vivado |
| SD/firmware install | `fpgahub.program_plugins.sd_install.SdInstallProgram` (reference — do not modify) | **G** |
| `reset {kind:"dfx-swap"}` (RP-scoped, in-band) | `platform_deploy` -> `pyverify.cli deploy` -> `SwapOrchestrator` -> shell `{"op":"swap"}` over `wg0`/net-protocol.md 6900 | **G** at the Hub layer, but travels the *in-band* `mgmt` channel, not JTAG/MCC — this is the "cheap, frequent" reset the whole persistent-shell design exists for (`HARDWARE_HUB_INTEGRATION.md` §1/§4) |

The pattern: JTAG-program and MCC-reboot are **irreducibly out-of-band** — they
are why a physically-attached supervisor exists at all (`HARDWARE_HUB_INTEGRATION.md`
§5's "in-band vs out-of-band split" paragraph) — while `dfx-swap` is the
in-band, no-supervisor-needed fast path once the shell is networked. Both
kinds are exposed to the Hub as `reset {kind}` verbs with different mechanisms
underneath; `pyverify.edge.ResetKind`/`reset()` model exactly this taxonomy
(all six kinds, including the two above), so a caller can compute "what would
this reset invalidate" before deciding whether it's worth the JTAG-program
detour or a cheap `dfx-swap` will do.
