# config_agent/

Bitstream receiver: TFTP (UDP 69) or raw TCP push (6910) -> validated
payload -> handed off to `coordinator/swap_fsm.c` for streaming into HWICAP.
See docs/contracts/net-protocol.md "Bitstream framing" and "Swap sequence".

## Responsibilities

1. **Receive** a `mps3_bitstream_hdr_t` (common/net_proto.h) followed by
   `len_words * 4` bytes of ICAP-ordered payload, over either transport.
2. **Validate the header BEFORE any ICAP write** (net-protocol.md is
   explicit about this ordering):
   - `magic == "MPS3"`
   - `ver` known/supported
   - `static_id` matches the running shell's `g_shell_state.static_id`
     (overlay-manifest.md: "partials are keyed to static_id... a shell
     rebuild... invalidates every stored partial")
   - `crc32` matches the received payload once fully buffered
3. **Enforce clearing-then-partial order (I2 RESOLVED, real now).** A
   `kind=partial` push is only accepted once THIS swap's incoming clearing
   has already been captured via `config_agent_take_validated_clearing()`
   (`s_ordering_seen_clearing`, reset each time a partial is taken). This
   used to be deliberately disabled pending the I2 decision; it is enforced
   now — see `config_agent_validate_header_ex()`'s `ordering_seen_clearing`
   parameter (the pure, host-testable form of this check;
   `config_agent_validate_header()` is a one-line wrapper over it using the
   module's own private flag).
4. **Hand off** the validated, fully-buffered payload to swap_fsm via
   `config_agent_take_validated_partial()` (polled from
   `swap_fsm.c:step_await_partial()`) and
   `config_agent_take_validated_clearing()` (polled from
   `swap_fsm.c:step_await_incoming_clearing()`) — both return a
   `config_agent_bitstream_info_t` {rm_id, static_id, len_words, crc32}
   rather than the old name-matching signature (I14: firmware always
   compares by the numeric rm_id the wire header carries, never by name).

## Two transports, one validator

TFTP (RFC 1350) framing and raw-TCP-6910 framing differ below the
`mps3_bitstream_hdr_t`, but both deliver the same header+payload shape on
top. `config_agent.c` is written so `config_agent_validate_header()` and
the staging buffer are transport-agnostic; only the receive-loop glue
(`config_agent_poll()`'s TFTP vs TCP branches) differs.

- **TFTP (port 69):** lwIP does not ship a first-party TFTP *server* in the
  base distribution the way it ships a client-usable API; Xilinx's
  `lwip_tftp` app-example historically wraps it. Reference only — port,
  don't vendor, per the A3 tasking. A minimal from-scratch TFTP server
  (RRQ/WRQ, opcode 02=WRQ for this direction, ACK/DATA/ERROR) is also a
  reasonable v1 scope reduction if the reference app doesn't fit the BSP.
- **Raw push (port 6910):** a plain TCP stream of header+payload bytes,
  no framing beyond `len_words` — simpler, and the documented "alt to
  TFTP." Good default if TFTP's 512-byte block/ack chatter turns out to be
  the throughput limiter (ARCHITECTURE_SPEC §7 already expects the network,
  not ICAP, to be the bottleneck at 10/100).

## Sourcing question — RESOLVED (I2)

**Both clearing and partial of the INCOMING pair arrive over the network
per swap** (net-protocol.md v0.1 + overlay-manifest.md v0.1): the shell
already holds the OUTGOING RM's clearing itself
(`coordinator/swap_fsm.h`'s `g_current_rm_clearing`), so it never needs the
network for that half. `config_agent_take_validated_clearing()` is the real
`kind=clearing` receive path this used to describe as
speculative/unwired — `swap_fsm.c`'s `SWAP_AWAIT_INCOMING_CLEARING` state
polls it every swap, not just optionally.

## Receive pipeline — REAL now (W-NET-SEAM)

`config_agent_poll()`'s receive bodies are implemented against the
`common/net_if.h` seam (NOT lwIP directly — the RAW-API glue is the seam's
one future thin file): a hand-rolled RFC1350 TFTP WRQ/octet server on :69
(new-TID per transfer, per-block ACKs, ERROR on any rejection) and the
raw-TCP 6910 session (24-byte header -> payload -> EOF sync, server-close
as the only client signal — flagged ambiguity, see fakeshell.py). Payload
CRC accumulates incrementally (`mps3_crc32_update`) as bytes land.

**Two-slot pair staging** (the resolved "push-vs-swap interleaving"
ambiguity — see config_agent.h's STAGING DECISION block): a full
{clearing, partial} pair stages BEFORE `swap`, mirroring
`pyverify.testing.fakeshell.ConfigAgentModel` exactly (a new clearing
starts a new pair and drops a stale partial; a completed partial re-arms
the clearing-first ordering). Header validation
(`config_agent_validate_header[_ex]()`), the I2 ordering decision and the
I12/I13 length+crc32 check remain pure functions
(`firmware/test/test_config_agent.c`); the full network paths are covered
by `firmware/test/test_config_agent_net.c` and the swap-through-the-seam
binary `test_swap_e2e_net.c`.

Still TODO(A3): transfer TIMEOUTS (a stalled TFTP/TCP transfer currently
parks the single receive session until the client aborts/closes — needs a
tick source once the lwIP backend lands) and the DDR placement of the two
4 MiB staging buffers on the real target (config_agent.h's STAGING BUFFERS
note).

**The v0.13 commit sink** (`config_agent_set_commit_sink()`, D13). While a
commit is armed, every 6910 push goes to the overlay store instead of a staging
slot or the ICAP-direct sink:

- The header is validated first, as for any push, and handed to the sink's
  `begin()`.
- The payload goes through `write_some()`, which may take less than it is
  offered. The rest waits in a <= 512 B carry, and nothing more is pulled off
  the connection until it drains. The host therefore stalls on the TCP window,
  not on a buffer here. There is no 16 KiB ring.
- With WINDOWED, the window reopens only for the bytes the sink has taken.
- A rejected header, a torn push or a CRC mismatch reaches the sink through
  `abort(why)`, exactly once.
- TFTP is refused while a commit is armed.

The tests are `firmware/test/test_config_agent_commit.c`, built fire-hose and
WINDOWED.
