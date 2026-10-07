# pyverify demo notebook (sketch)

A `.ipynb` can't be rendered directly here, so this is the cell-by-cell
sequence a real notebook would contain — copy each ` ```python ` block into
a Jupyter cell in order. This is the "PYNQ experience" front-end
ARCHITECTURE_SPEC.md §14 phase 10 calls for: **select DUT -> load firmware
-> run test -> check**, end to end, from a notebook — one object,
`pyverify.board.Mps3Board`, the way a PYNQ user holds a `pynq.Overlay`.

Every cell calls real pyverify code (`host/pyverify/pyverify/`): manifest
validation, CRC checks, the TFTP/raw-TCP bitstream push, the control-channel
RPCs and the console scraping are all implemented — the only thing these
cells need is a *listening shell* (a real board running the A3 firmware, or
the `pyverify.testing.fakeshell` reference server) at `SHELL_HOST`. Against
nothing, the first network touch fails with a normal `ConnectionError` —
no stubs, no `NotImplementedError`.

## 0. Setup

```python
from pathlib import Path

from pyverify import Mps3Board

# net-protocol.md: "default static 192.168.10.101 (matches the existing
# fpgahub MPS3 board block)".
SHELL_HOST = "192.168.10.101"

board = Mps3Board(SHELL_HOST, overlay_root=Path("overlay")).connect()
# (Or `with Mps3Board(SHELL_HOST) as board:` for scoped sessions —
# the context manager closes consoles + control channel on exit.)

info = board.ping()
print(f"shell_id={info.shell_id} current_rm={info.rm_id}")
```

## 1+2. Select DUT + load firmware — validate, swap, push

```python
# Resolves overlay/nanosoc/ against overlay_root, validates the manifest
# (CRC32s, static_id vs the live shell), then runs the swap sequence:
#
#     swap_begin  ->  push clearing+partial  ->  swap_await
#
# The push happens INSIDE the swap. The shell only listens for a bitstream
# once the `swap` RPC has driven its FSM into SWAP_AWAIT_* — which is exactly
# why it parks the control connection for the whole reconfiguration — and it
# RESETS a push that arrives at any other time (net-protocol.md v0.7).
# Transport is TFTP by default, or transport="tcp" for raw 6910.
result = board.deploy("nanosoc")
print(f"loaded rm_id={result.rm_id} verified={result.verified}")
```

Equivalently, from a shell cell (or a CI step / fpgahub manifest Action):

```python
!python -m pyverify.cli deploy --host {SHELL_HOST} --overlay overlay/nanosoc
```

## 3. Re-attach — console (real), SWD/ILA (hints or injected executors)

```python
# ReattachPlan.apply(): consoles reopen for real (fresh TCP connects);
# the tool-bound steps (ltx reload via Vivado/XVC, SWD DP reconnect via
# OpenOCD, VPHY re-link) return their descriptive hint unless you inject
# a real executor — spec §6.3 keeps them independent, tool-specific steps.
outcomes = result.reattach.apply()
print(outcomes["swd"])   # hint: re-run SWD line reset + DP connect ...
print(outcomes["vphy"])  # hint: client.link(event='up') once the MAC ...

# Injecting a real SWD executor once OpenOCD is installed host-side:
from pyverify import OpenOcdRemoteBitbangConfig, launch_openocd

swd_cfg = OpenOcdRemoteBitbangConfig(shell_host=SHELL_HOST)
outcomes = result.reattach.apply({
    "swd": lambda plan: launch_openocd(swd_cfg, "dap info"),
})
```

(The same OpenOCD session in file form, plus the Vivado-side XVC/ILA
re-attach Tcl, lives in `host/openocd/` — see its README for the DPIDR
smoke test and the 6920-vs-2542 tool split.)

## 4. Run test — scrape the DUT consoles

```python
banner = board.uart0.assert_contains(b"nanosoc boot", timeout=15.0)
print(banner.decode(errors="replace"))

test_output = board.uart0.read_until(b"TEST COMPLETE", timeout=60.0)
print(test_output.decode(errors="replace"))
```

## 5. Check result

```python
assert b"PASS" in test_output, f"unexpected test output: {test_output!r}"
print("PASS")

# telemetry ALWAYS fails: there is NO power sensor on this platform
# (net-protocol.md v0.6 — the TELEM block's inputs are tied to ground, its
# INA228 I2C engine was never written, its pads aren't on the top level, and
# the MPS3 MCC refuses voltage reads). There are no `mv`/`ma` fields to read;
# the verb exists to report the DUT-lockup pin, which rides the failure line.
telemetry = board.telemetry()
assert telemetry.ok is False and telemetry.err == "no power sensor"
print(f"lockup={telemetry.lockup}  (power: {telemetry.err})")

# NB: `lockup` is only meaningful for RMs that DRIVE the pin — nanosoc_multicore
# does; `nanosoc`, `eth_ss` and the OOC RMs hard-tie it to 0. On those,
# lockup=False means "cannot report lockup", NOT "the DUT is healthy". The
# verdict for this demo comes from the console transcript above.
```

## 6. Cleanup

```python
board.close()   # closes cached consoles + the control channel
```

## Notes for whoever wires this against real hardware

- `board.deploy(...)` fails *cleanly* on an unreachable shell (a
  `ConnectionError` from the control-channel connect, or a
  `pyverify.pusher.PushError` from a TFTP/raw-TCP push timeout) — the
  same failures `python -m pyverify.cli deploy` maps to exit code 3.
- Consoles cached on the board object (`board.uart0` etc.) are stale
  after a swap: `result.reattach.apply()` opens *fresh* readers, or call
  `board.reopen_consoles()` to drop the cache so the next accessor use
  reconnects.
- `board.set_clk("25mhz")` validates the preset client-side against the
  firmware's current (placeholder) table — a typo raises `ValueError`
  before any traffic; `board.set_clk(name, presets=None)` bypasses the
  check if the firmware table has moved ahead of pyverify
  (docs/contracts/OPEN_ISSUES.md I16).
- This notebook assumes one shell/one DUT. A farm topology (D1, many
  boards behind one shared server) just means parameterising `SHELL_HOST`
  from an fpgahub lease lookup instead of hardcoding it — see
  `host/tender/README.md`.

---

## Appendix — the low-level sequence (what `Mps3Board` wires together)

The facade is thin; every step is available piecemeal, which is also the
shape the unit tests exercise. This is the original pre-facade cell
sequence, kept for when you need to hold the pieces individually:

```python
from pathlib import Path

from pyverify import (
    ShellClient,
    Overlay,
    SwapOrchestrator,
    ConsoleReader,
    UART0_PORT,
    BitstreamPusher,
)

SHELL_HOST = "192.168.10.101"

# 1. Select DUT — pick an overlay, connect to the shell.
overlay = Overlay.load(Path("overlay") / "nanosoc")
print(overlay.manifest.rm_name, hex(overlay.manifest.static_id), hex(overlay.manifest.rm_id))

shell = ShellClient(SHELL_HOST).connect()
info = shell.ping()

# 2. Load firmware — validate, then swap -> push -> await. The pusher is part
# of the package now (pyverify.pusher, W-PKG) — no sys.path reach, no sibling
# import; SwapOrchestrator still takes it via the Pusher protocol seam.
# deploy() sends `swap` first (which PARKS the control connection), pushes the
# pair INTO the parked swap, then reads the reply — the only order the shell
# accepts a bitstream in (net-protocol.md v0.7).
pusher = BitstreamPusher(host=SHELL_HOST, transport="tftp")
orchestrator = SwapOrchestrator(shell, pusher)
result = orchestrator.deploy(overlay)   # validate -> swap_begin -> push -> swap_await

# 3. Re-attach — console by hand (a ConsoleReader is just a TCP connect).
uart0 = ConsoleReader(SHELL_HOST, UART0_PORT).connect()
banner = uart0.assert_contains(b"nanosoc boot", timeout=15.0)

# 4/5. Run + check, then clean up.
telemetry = shell.telemetry()
uart0.close()
shell.close()
```
