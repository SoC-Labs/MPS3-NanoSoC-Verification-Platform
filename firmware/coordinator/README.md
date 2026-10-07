# coordinator/

The heart of the shell firmware: the control-channel (TCP 6900) request
dispatcher and the reconfiguration swap state machine that drives
ARCHITECTURE_SPEC.md §6.2 / net-protocol.md's "Swap sequence."

## Files

- `coordinator.h` / `coordinator.c` — global shell state (`g_shell_state`),
  `coordinator_init()` and one handler function
  per net-protocol.md control-channel verb (`ping`, `reset`, `set_clk`,
  `swap`, `link`, `commit`, `telemetry`). Handlers are thin: validate input,
  poke the relevant module (clkrst / overlay_store / swap_fsm / vphy regs),
  and return a response struct — none of them block. The line codec itself
  (flat-JSON tokenizer + per-op response encoders) is REAL and lives in
  `common/net_proto.c` (W-JSON; tested by
  `firmware/test/test_net_proto_json.c` + `test_coordinator_dispatch.c` +
  the cross-language golden test `tests/firmware_logic/test_json_golden.py`
  against pyverify's actual client). `coordinator_dispatch_line()` returns
  1 = "response in `out`, send now", 0 = "held response" (an accepted
  `swap`): the network layer parks the connection until swap_fsm settles,
  then builds the reply with `coordinator_swap_final_response()` — that
  parking/bookkeeping is REAL now in `coordinator_net.c` (W-NET-SEAM: the
  connection handle itself is the parked token; single-client 6900, no
  handle threading through swap_fsm needed), against the `common/net_if.h`
  seam. `ping`'s shell_id comes from the
  `mps3_shell_static_id()` seam: weak 0 fallback in coordinator.c, strong
  build-time-generated override on the real shell (W-DFX-ART's static_id
  scheme; provenance decision owned by A6).
- `swap_fsm.h` / `swap_fsm.c` — the non-blocking DUT-swap state machine:
  gate -> decouple+rp_reset -> stream cached (outgoing) clearing -> await +
  capture incoming clearing -> await + stream incoming partial -> verify
  rm_id -> cache incoming clearing -> release. This is where the AXI HWICAP
  register pokes live, tied to shell-regmap.md v0.1 offsets.
- `swap_fsm_transitions.c` — the FSM's pure transition table
  (`swap_fsm_next_state()`): every "what state comes next" decision, with
  zero register/module dependency, so it's directly host-gcc-testable (see
  `firmware/test/test_swap_fsm_transitions.c`). `swap_fsm.c`'s step_*()
  functions gather inputs and perform register pokes; they never re-derive
  a transition decision themselves.

## Why non-blocking

A partial can be up to ~2 MB (overlay-manifest.md example) streamed through
a single-word AXI4-Lite HWICAP FIFO at ~2.5 MB/s (ARCHITECTURE_SPEC §7) —
low-single-digit seconds. Doing that in one tight blocking loop would starve
lwIP's timers on the *same* MicroBlaze core running the TCP connection that
requested the swap. `swap_fsm_poll()` therefore streams a small, bounded
chunk (e.g. one HWICAP write-FIFO's worth of words) per call and is called
once per superloop iteration (the loop lives in `firmware/platform/src/main.c`), interleaved with
`xemacif_input()`/`tcp_tmr()`. Same reasoning applies to
`config_agent_poll()`, `jtag_server_poll()`, etc.

## Swap sequence <-> code map (I2/I25 RESOLVED)

| net-protocol.md step | swap_fsm state | regmap touched |
|---|---|---|
| 1. gate XVC/SWD/UART/VPHY link | `SWAP_GATE` | (module-local gate flags; VPHY.LINK_EVENT for link-down injection) |
| 1. assert DECOUPLE + hold rp_resetn | `SWAP_DECOUPLE_ASSERT` | `DFXCTL.DECOUPLE`, `DFXCTL.SHUTDOWN`, `CLKRST.RESET_CTRL.rp_resetn` |
| 2. stream the CACHED clearing bitstream of the *currently-loaded* RM | `SWAP_STREAM_CLEARING` | `HWICAP.WF/SZ/CR/SR`, sourced from `g_current_rm_clearing` (I2) |
| (receive the incoming pair's clearing — captured, not streamed this swap) | `SWAP_AWAIT_INCOMING_CLEARING` | `config_agent_take_validated_clearing()` |
| 3. stream new RM partial (received via TFTP/6910) | `SWAP_AWAIT_PARTIAL` -> `SWAP_STREAM_PARTIAL` | `HWICAP.WF/SZ/CR/SR` |
| 4. verify RM load (rm_id + CRC / DFX monitor) | `SWAP_VERIFY` | `DFXCTL.RM_ID` / `DFXCTL.RM_STATUS` (real offsets, shell-regmap.md v0.1 — I25 fix: real compare, not a hardcoded `true`) |
| 5. cache the new RM's clearing bitstream as the current one | `SWAP_CACHE_CLEARING` | (promotes the staged incoming clearing to `g_current_rm_clearing`, no register access) |
| 6. release DECOUPLE, deassert rp_resetn | `SWAP_RELEASE` | `DFXCTL.DECOUPLE`, `DFXCTL.SHUTDOWN`, `CLKRST.RESET_CTRL.rp_resetn` |
| 7. respond with confirmed rm_id + verified | `SWAP_DONE` | (result latched in `swap_fsm_last_result()`; `coordinator_swap_final_response()` encodes it; `coordinator_net.c` sends it on the parked 6900 connection) |

The state-transition *decisions* themselves (not the register pokes) live
in `swap_fsm_transitions.c`'s `swap_fsm_next_state()` — a pure, zero-
dependency function, host-unit-tested in `firmware/test/test_swap_fsm_transitions.c`.

## Clearing-bitstream sourcing — RESOLVED (I2)

**Decision (docs/contracts/OPEN_ISSUES.md I2, folded into net-protocol.md +
overlay-manifest.md v0.1): the shell owns clearing-bitstream sequencing.**
There is exactly ONE live "current clearing" reference at any time —
`coordinator/swap_fsm.h`'s `g_current_rm_clearing` — never the old
multi-slot `g_rm_cache[MPS3_RM_CACHE_SLOTS]` hedge this file used to
describe (that array is retired). Boot-seeded from the greybox's clearing
(ships inside the shell image, `overlay_store_get_greybox_clearing()`),
kept current by `SWAP_CACHE_CLEARING` after every successful swap -- the
power-on load from the user microSD included, which is an ordinary swap with
the FSM's internal source `"usd"` (D13; `overlay_store/README.md`). The
host's `swap` payload therefore only ever carries the **incoming** RM's
`{clearing, partial}` pair (net-protocol.md "Swap sequence"); the shell
never needs the host to know what's currently running.

See `swap_fsm.h`'s per-state comment for the exact state-by-state mapping,
and `overlay_store.h`/`overlay_store/README.md` for the "runtime cache is
distinct from the A/B default store" distinction (overlay-manifest.md).

## Held (deferred) responses — v0.13

`coordinator_dispatch_line()` returns 0 for three verbs whose answer is only
known later: an accepted `swap`, an accepted v0.13 `commit` (the pair is then
pushed over 6910 into the user microSD) and a `usd` `format` / `clear`.
`coordinator_net.c` parks the connection and asks `coordinator_held_poll()` each
pass. The held verb is polled to completion whether or not its client is still
connected, because a commit or a format has to finish and release the store.
The three are mutually exclusive: one swap FSM, one store. A swap is refused
`store busy` while a commit or an action holds the store, and a commit or an
action is refused `store busy` while the FSM runs, including the power-on load.
`usd` status is never held: it answers during a swap too.
