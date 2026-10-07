# fpga/bus_mon — bus observatory IP

`ahb_mon.sv`: a **passive** AHB-Lite monitor behind an AXI4-Lite CSR — transaction
counters, a wait-state histogram, and protocol checks with first-violation capture.

## Why counters, when there is already a trace

They answer different questions, and the platform needs both.

|  | IICE trace | `ahb_mon` counters |
|---|---|---|
| window | 1024 samples ≈ 20.5 µs | unbounded, full clock rate |
| readout | ~340 bit/s measured → minutes–hours | a handful of register reads |
| tells you | exactly what happened, cycle by cycle | whether, how often, how bad |
| cost | BRAM × width × depth | a few hundred LUTs |

**The workflow is: counters find it, the trace explains it.** A 20 µs window is
useless for "something goes wrong somewhere in the next ten minutes" — but a
counter that increments tells you it happened, and `VIOL_ADDR` tells the IICE
where to point.

Precedent: `GENCHK` (shell-regmap.md `0x44A6_0000`) is this same pattern for
Ethernet frames. This is the bus equivalent.

## Where it must be instantiated

**Inside the RP, next to the bus.** `docs/contracts/partition-pins.md` states
there is *"no shell↔DUT AXI in v0"* and everything crossing the boundary is *"a
slow scalar or a low-rate stream"*; widening it re-keys `static_id` and forces a
rebuild of every RM partial. So the shell cannot see a DUT bus, and a bus
instrument has to sit on the DUT side. Its CSR is reached like any DUT peripheral.

## Registers (local offsets)

| Off | Reg | Notes |
|---|---|---|
| 0x00 | `CTRL` | [0] enable, [1] clear (W1P) |
| 0x04 | `STATUS` | [0] enabled, [1] xfer in flight, [2] any violation |
| 0x08 | `CNT_RD` / 0x0C `CNT_WR` | completed data phases |
| 0x10 | `CNT_ERR` | `HRESP` = ERROR at completion |
| 0x14 | `CNT_WAIT` | total wait-state cycles |
| 0x18 | `CNT_IDLE` | cycles with nothing in flight |
| 0x1C | `VIOL` | sticky bitmap, **RW1C** |
| 0x20 | `VIOL_ADDR` | `HADDR` of the **first** violation |
| 0x24 | `VIOL_INFO` | [3:0] first code, [15:8] count (saturating) |
| 0x28–0x34 | `HIST0..3` | wait states 0 / 1–3 / 4–15 / 16+ |
| 0x38 | `MAX_WAIT` | worst single transfer |

Violation codes: 0 `ADDR_CHANGED`, 1 `TRANS_CHANGED`, 2 `WRITE_CHANGED`,
3 `SIZE_ILLEGAL`, 4 `BUSY_NO_BURST`, 5 `RESP_ON_IDLE`.

## Scope, stated plainly

- **AHB-Lite only.** Single master, one outstanding transfer, 1-bit `HRESP`
  (matching `slcorem0.v:63` on this DUT). AXI4 needs ID tracking and outstanding
  queues — a bigger module, not a parameter.
- **Single clock domain.** No `hclk` port on purpose: the bus must already be in
  the CSR domain. Cross-domain monitoring needs a synchronizer per signal and a
  rethink of what a count means across the crossing.
- **No data.** Counters and checks are a function of control/handshake only.
  Data is what the trace is for.
- **Passive.** Every AHB signal is an input; there is no path back onto the bus,
  so a bug here cannot hang the DUT. Fault *injection* is deliberately a separate
  (intrusive) module, not yet written — mixing them would make the safe thing
  carry the risk of the unsafe one.

## Verification

`tests/bus_mon` (cocotb + VCS), 11 tests. Every violation code has a test that
**provokes** it, plus a clean-traffic test asserting the bitmap stays zero, plus a
legal-`BUSY`-inside-a-burst test so the check is not merely trigger-happy.

A checker that never fires reads exactly like one that is wired up wrong, and the
second is worse than none. So the tests were **mutation-tested**: neutering the
detector makes 6 of the 11 fail. If a future edit breaks detection, a provocation
test fails; if it makes the checks over-eager, the clean test fails.

Note VCS caught an elaboration error Verilator's lint accepted — `viol_q` and
`enable_q` were written from two `always_ff` blocks. The CSR path now decodes
only; the state block is the single driver of every register.
