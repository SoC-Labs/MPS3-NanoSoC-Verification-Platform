# `tests/common/` — shared bench infrastructure

Reusable BFMs, models and helpers that per-block benches import instead of
re-implementing. Everything here is self-contained (no `cocotbext-*`
dependency), cocotb-2.0.1-safe (the pinned version — `source set_env.sh`), and
bound to **real, confirmed** DUT port names, not guessed ones. See
`tests/README.md` for the overall verification strategy and the
`rtl_ready()` skip-gate.

## Bus BFMs

| Module | Class(es) | Bus | Used by |
|---|---|---|---|
| `regmap.py` | `AxiLiteMaster` | AXI4-Lite master | every **shell** block (`0x44Ax_xxxx` CSRs) |
| `ahb_lite.py` | `AhbLiteMaster`, `AhbLiteMonitor` | AHB-Lite | every **DUT-side** block (nanosoc is a Cortex-M0 SoC) |
| `mdio_master.py` | `MdioMaster` | MDIO (clause-22) | `mdio_phy_model`, ethernet benches |

`regmap.py` also holds the transcribed shell register map (block base
addresses + per-block offsets, cross-checked against `shell-regmap.md` and
`firmware/common/platform_regs.h`).

Other helpers: `axis.py` (AXI-Stream), `frames.py` (Ethernet frame
build/parse), `rmii.py` (RMII PHY driver), `random_stim.py`,
`ovlstore_header.py`, `dut_presence.py` (the `rtl_ready()` skip-gate).

---

## `ahb_lite.py` — AHB-Lite master BFM + protocol monitor

The AHB-Lite counterpart to `regmap.py`'s `AxiLiteMaster`, in the same house
style. It exists because the shell is AXI4-Lite but the **DUT side is
AHB-Lite**: nanosoc is a Cortex-M0 SoC, and the first consumer is the
`nanosoc_exp` socket — "the hole" at `0x6000_0000`
(`fpga/rp/nanosoc_exp/README.md`). Every future DUT-side peripheral needs it.

**AHB-Lite is a two-stage pipeline, not a set of handshakes**, and the BFM is
written to get the pipeline right — that is the whole point of it existing as
shared infra rather than a per-bench loop:

- **Address phase / data phase are different cycles.** Cycle *N* drives
  `HSEL/HADDR/HTRANS/HWRITE/HSIZE/HBURST/HPROT`; cycle *N+1* drives `HWDATA`
  (write) or samples `HRDATA` (read).
- **The pipeline really overlaps.** `burst_*()` / `pipeline()` emit gapless
  transfers, so transfer *n+1*'s address phase shares a cycle with transfer
  *n*'s data phase — the overlap a drive-then-wait BFM can never produce, and
  therefore can never catch a bug in.
- **Wait states freeze both phases.** The slave holds `HREADYOUT` low; the BFM
  holds `HADDR/HTRANS/...` and `HWDATA`, and the next address phase does not
  advance.
- **ERROR is a two-cycle response.** On `HRESP=ERROR` the BFM drives
  `HTRANS=IDLE` in the second cycle to cancel the already-broadcast next
  address, then re-presents that cancelled transfer (demoting `SEQ→NONSEQ`), so
  your `await` still means what it says.

### Wiring `HREADY`

`HREADY` (a slave *input*) is the **global** bus ready — in a single-slave
system it *is* that slave's own `HREADYOUT`. The BFM **samples** it and never
invents it. Two supported wirings:

- **Preferred — tie it in a small harness top** that instantiates the DUT and
  does `assign hready = hreadyout;`, exposing the AHB signals as top ports (the
  pattern in `tests/qspi_xip/qspi_xip_harness.sv`). This makes it *impossible*
  for the bench to lie to the slave about readiness. Bind with
  `AhbLiteMaster.from_dut(dut)`.
- **Convenience — `hready_drive=`.** If your `TOPLEVEL` is the raw slave (its
  `hready` input floating), pass `hready_drive=dut.hready` and the BFM mirrors
  `hreadyout` onto it (delta-delayed). Sound only because AHB-Lite forbids
  `HREADYOUT` from depending combinationally on `HREADY`.

### API

Blocking register API mirrors `AxiLiteMaster` (`read` returns `(data, hresp)`):

```python
from ahb_lite import AhbLiteMaster, AhbLiteMonitor, AhbError, HSIZE_BYTE

cocotb.start_soon(Clock(dut.hclk, 20, unit="ns").start())
ahb = AhbLiteMaster.from_dut(dut)      # binds hclk/hresetn/h* by name
mon = AhbLiteMonitor.from_dut(dut); mon.start()   # optional protocol checker

await ahb.reset()                              # drive hresetn low, bus IDLE

await ahb.write(0x6000_0004, 0xDEAD_BEEF)      # single word write -> HRESP
data, resp = await ahb.read(0x6000_0004)       # -> (0xDEADBEEF, 0)

await ahb.write(0x6000_0000, 0xAA, size=HSIZE_BYTE)   # byte-lane placed for you

# gapless pipelined transfers (address phase of n+1 overlaps data phase of n):
await ahb.burst_write(0x6000_0000, [0x11, 0x22, 0x33, 0x44])
words = await ahb.burst_read(0x6000_0000, 4)
await ahb.burst_write(0x10, data, busy=2)      # BUSY beats inside the burst

await ahb.idle(3)                              # explicit IDLE cycles

# ERROR surfaces as an exception by default...
try:
    await ahb.read(0x6000_1000)
except AhbError:
    ...
# ...or as a status, if you would rather assert on it:
ahb.raise_on_error = False
data, resp = await ahb.read(0x6000_1000)       # resp == HRESP_ERROR
```

Non-blocking `init_write()/init_read()` return an `AhbTransfer` you `await`
later — that is how you keep several transfers in flight and get a genuinely
pipelined bus.

`from_dut(dut, prefix="")` binds the frozen lowercase `nanosoc_exp_socket` port
list (`hclk hresetn hsel haddr htrans hwrite hsize hburst hprot hmastlock
hwdata hready hrdata hreadyout hresp`) and falls back to UPPERCASE
(`HADDR/HTRANS/...`, the Arm/CMSDK style) automatically, so it binds Arm IP
too. Pass `prefix="exp_"` for prefixed ports; override any handle by keyword.

Encodings and constants are exported: `HTRANS_*`, `HBURST_*`, `HSIZE_*`,
`HRESP_*`, `HPROT_*`.

### `AhbLiteMonitor`

A passive checker you bind alongside the master. It asserts, every cycle: the
address phase and `HWDATA` are held while `HREADY` is low (M1/M2);
`HREADYOUT` eventually asserts, i.e. no hung slave (M3); `HRESP=ERROR` is a
two-cycle response (M4); IDLE/BUSY are answered OKAY with no wait state (M5);
`SEQ` never follows IDLE (M6). Violations raise `AhbProtocolError`
(`fatal=True`) or are collected in `mon.errors` (`fatal=False`).

### Proof

Proven under VCS 2022.06-SP2 + cocotb 2.0.1 against a throwaway AHB-Lite slave
stub (correct: registered/pipelined, programmable wait states, two-cycle error,
real byte-lane decode). Coverage: word RW round-trip; **pipeline overlap
verified from the wires by an independent bus spy** (8 consecutive accepted
address phases on an INCR8 burst, read and write); backpressure; two-cycle
ERROR including the cancel-and-re-present of the transfer queued behind it;
byte/halfword lane placement; BUSY insertion; IDLE insertion; the
`hready_drive` mirror mode; and a **mutation test** (a one-cycle-error slave)
that confirms the monitor's M4 check is not vacuous. The self-test is
`scratchpad/ahb_selftest/` (throwaway — not part of the committed tree).

> Not registered in `tests/Makefile` or `tests/common/list_benches.py` — those
> belong to the integrator. The BFM is a library; it has no standalone bench
> target of its own to register.
