# Deploying an RM over the wire

The fast inner loop: push a DUT design into the running board over Ethernet, in
seconds, then talk to it. This guide shows the shape of a deploy and links to the
authoritative operator cheat-sheet — it does **not** replace it.

!!! danger "This touches shared hardware"
    The board is a **shared, leased** resource driven over two channels at once
    (Ethernet + JTAG), and **an interrupted swap can corrupt a 1.3 MB transfer
    and wedge the shell**. Take the lease first, never interrupt a swap, and
    follow the board-lease discipline (take the lease first, never interrupt a
    swap). The steps below are an orientation, not a licence to skip it.

## The mental model

You already know the pieces from the concept pages:

1. The **shell** is up and on the network (an [over-the-wire
   swap](../concepts/over-the-wire-reconfiguration.md) reprograms only the RP).
2. An **overlay** (partial + clearing bitstream + manifest) is built for *this*
   shell — matched by `static_id`.
3. The **pusher** streams it in; the **coordinator** clears, writes, and
   **verifies by `rm_id`**; then you **re-attach** debug/console and run your
   test.

## The Python way (the "PYNQ experience")

This is the whole loop, using the `pyverify` library's session facade:

```python
from pyverify import Mps3Board
from pyverify.debug import XvcSession, XvcTarget

# Optional: a live Vivado hw_manager on the shell's XVC server, for RMs that
# carry ILAs. It runs a LOCAL hw_server and reaches port 2542 through
# `ssh -L 2542:192.168.10.101:2542 <hub>`, hence "localhost".
xvc = XvcSession(XvcTarget("localhost"), vivado="/apps/Xilinx/Vivado/2024.1/bin/vivado")

with Mps3Board("192.168.10.101", xvc=xvc) as board, xvc:
    result = board.deploy("nanosoc_ila")           # closes the XVC target, then validate -> swap -> push -> verify
    result.reattach.apply()                        # reopen consoles; reopen XVC + load the new RM's .ltx
    board.uart0.assert_contains(b"nanosoc boot")   # the verdict is the console output
```

- `deploy()` loads the overlay, checks it against the shell's live `static_id`,
  pushes clearing + partial, issues the `swap`, and confirms the `rm_id`. The
  push transport is chosen from the shell's `version.features` (`windowed` means
  a windowed TCP push; a plain push would deadlock such a shell). With
  an `xvc` session it **closes the XVC target before the swap RPC**: the shell
  stalls any XVC shift during a swap and then runs it against the *new* RM, and
  the old RM's probe map means nothing to the new one.
- `reattach.apply()` runs the post-swap re-attach steps (debug is gated during a
  swap — see [Debugging the DUT](../concepts/debugging-the-dut.md)): it reopens
  the UART/SWO consoles and, when the board was given an `xvc` session, reopens
  the XVC target and loads the new RM's `.ltx` from its overlay directory. The
  SWD/JTAG and virtual-PHY steps return a hint unless you pass an executor
  (`result.reattach.apply({"swd": ...})`). Without an `xvc` session the ILA step
  also returns a hint — nothing reopens a Vivado you started yourself.
- Your **pass/fail comes from the console/SWD**, not from telemetry (the platform
  has no power sensor; `telemetry()` reports only a lockup pin, by design).

Or from the command line:

```sh
pip install -e host/pyverify

# take the board first (blocks on the FCFS queue; prints only the token)
export MPS3_HUB=<hub-host>
TOKEN=$(python -m pyverify.cli lease acquire --holder my-session)

python -m pyverify.cli ping   --host 192.168.10.101       # shell_id / rm_id
python -m pyverify.cli deploy --host 192.168.10.101 --overlay overlay/nanosoc
#   the push transport is read from the shell: a shell whose `version` lists
#   `windowed` (every fielded one) gets --pusher-transport tcp --src tcp --windowed;
#   pass any of those flags (or --no-windowed) to override

python -m pyverify.cli lease release --token "$TOKEN" --holder my-session
```

`pyverify` is the front door: `lease`, `sd write`, `ping` / `version` / `diag`
and `deploy` are all verbs of the same CLI, and the shell scripts under
`scripts/` are shims over them.

## The board-run reality (read the runbook)

On the real lab board there are essential operational facts (see
[Board bring-up](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/BOARD_BRINGUP.md)) that must be respected:

- **Take the lease** before touching the board; release it with the *same*
  holder id, or the board stays HELD.
- Dataplane commands (ping/push/console/SWD) run **through the fpgahub host** —
  the board's data IP is only reachable from there; JTAG uses a different FQDN.
- **Never interrupt a swap** and never halt the shell's MicroBlaze mid-swap — it
  kills the TCP stream and needs a JTAG shell reload to recover.
- A swap that didn't reach DONE leaves debug/console gated; re-run the liveness
  checks after any failure.

## Available RMs

Ten RMs are registered in `fpga/dfx/rm_list.tcl`:

- **Vendored in this repo** (buildable from a clean clone):
  `greybox`, `led`, `uart_echo`, `regdemo_a`, `regdemo_b`.
- **Need external sources or confidential IP**: `nanosoc`, `nanosoc_multicore`,
  `nanosoc_upy`, `eth_ss`, `socscope` — see
  [Getting started](getting-started.md) for what each one requires.

See [Reconfigurable Modules & the DUT](../concepts/reconfigurable-modules-and-dut.md)
for what each proves.

## Authoritative references

- [`host/README.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/host/README.md) — how `pyverify`'s pieces
  (client, pusher, swap orchestrator, console, debug) compose into a session.
- The [interface contracts](../reference/contracts.md) — the wire protocol,
  overlay schema, and boundary this all rides on.
