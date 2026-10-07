# `tests/hostio4_link_cdc` — L1: the hostio4 link across two independent clocks

**Result: PASS.** `hostio4_controller` ⇄ `hostio4_target` is byte-exact on all
four channels, both directions, with random backpressure, across every clock
relationship tried — including a 1.00001:1 near-unity drift. The bench detects a
stuck `ioack`, a bit-reversed bus, over-budget wire skew, and a missing
synchroniser.

```sh
source set_env.sh
make -C tests/hostio4_link_cdc          # ratios + neg + skew + sync + masking
```

Raw output is committed in [`RESULT.txt`](RESULT.txt).

## Why this exists

L0 ([`tests/hostio4_golden`](../hostio4_golden/)) proves the vendor RTL passes
the vendor's own reference. But `tb_hostio4.v` drives both ends from a single
oscillator:

```verilog
assign C_clk =  clk;
assign T_clk = !clk;      // same frequency, zero drift, fixed 180 deg phase
```

So L0 says **nothing** about the `hostio4_controller_sync` / `hostio4_target_sync`
2-flop synchronisers, and nothing about what happens when the two ends are
genuinely asynchronous. In the harness they are: nanoSoC drives `ioreq1`/`ioreq2`
and receives `ioack` (the P1 pin mapping), so **the DUT holds the controller**,
clocked by `dut_clk`, while the target would sit in the shell on its own clock.

## What the RTL actually does

Both ends capture data from the **raw** `iodata4` wires and qualify the capture
with a **2FF-synchronised strobe**:

| receiver | strobe (synchronised) | data (raw) |
|---|---|---|
| `hostio4_target` | `ioreq1_s`, `ioreq2_s` | `iodata4_i` — the FSM even branches on it directly: `RXC1: … (iodata4_i[0]) ? TXCZ : RXDH` |
| `hostio4_controller` | `ioack_s` | `iodata4_i` → `rd4_hi`/`rd4_lo` |

The controller additionally synchronises all four `iodata4` bits into
`iodata4_s`, but uses that **only** for the target's continuously-sampled status
nibble (`vtx0_rdy`/`vrx0_rdy`/…), which has no strobe. That is a coherent design,
and the RTL says so: `// first high nibble read data - async-data safe by protocol`.

The consequence: **the 2FF latency is the settling budget for the data wires.**
The quantity that matters at a pad or partition boundary is therefore not the
absolute wire delay but the **skew between `iodata4` and its strobe**.

## The skew budget — measured, not assumed

Bisected with `dreq`/`dack` held at 0, so `ddata` *is* the relative skew:

| receiver | clock | predicted `2 × T_recv` | measured cliff |
|---|---|---|---|
| target | T = 7.3 ns | 14.6 ns | 14.7 pass / 15.0 fail |
| target | T = 20.3 ns | 40.6 ns | 40.0 pass / 42.0 fail |
| controller | C = 10.0 ns | 20.0 ns | 20.0 pass / 20.5 fail |
| target, `+testmode` | (2FFs bypassed) | ~0 | 0.0 pass / 0.2 fail |

Three things fall out, each asserted in the `skew` target:

1. **The budget is `2 × the receiving side's clock period`.** Confirmed at two
   different target periods.
2. **It does not depend on the transmitter's clock.** The target-side cliff sits
   between 14 and 15 ns whether the controller runs at 5, 10 or 40 ns.
3. **The controller side has extra, unreliable slack.** Its capture is gated to
   the `ioclken` (C/2) grid and also depends on how long the target holds data,
   so the observed cliff is ratio-dependent: `2.0 × C` at C = 10 ns but
   `2.2–2.4 × C` at C = 20 ns. **Treat `2 × C` as the bound; do not bank the
   extra.**

**For the harness this is enormous margin.** A 100 MHz shell clock gives the
target 20 ns of tolerable `iodata4`-vs-`ioreq2` skew. FPGA routing skew across a
DFX partition boundary is well under 1 ns. hostio4 is not skew-limited here —
now measured rather than hoped.

## Two results worth not rediscovering

### Two clocks alone cannot catch a missing synchroniser

`testmode=1` bypasses every 2FF (`sig_s = testmode ? sig_a : sig_r[2]`). With
zero wire skew, **the fully bypassed design still passes.** An RTL simulator has
no metastability, so removing a synchroniser merely removes two cycles of
latency, and a four-phase handshake is latency-insensitive.

The synchronisers only become *visible* to this bench through the skew budget:
inject 0.2 ns and the bypassed link dies, where the real one shrugs off 12 ns.
That `0.2 ns → 14.6 ns` collapse is the entire contribution of the 2FFs, and it
is the only handle simulation gives us on them. **Metastability itself remains
unproven here** — that needs static CDC analysis (Spyglass/Questa CDC), not
another testbench. The `sync` target asserts all of this so it stays known.

### A harmonic clock ratio hides skew violations

Found while bisecting. At `cper=10.0 / tper=20.0` — exactly 1:2 — the sampling
phase never slides, and a 42 ns skew (2 ns *past* the 40 ns budget) sails
through. Detune the target clock by 1.5% to 20.3 ns and it fails immediately.

This is why `ratios` must contain a non-harmonic pair, and why nobody should
"tidy" these periods into round numbers. The `masking` target pins it down.

## How the bench is built

- **Self-checking, no golden file.** Each of the four channels carries an
  independent 32-bit LFSR stream with a distinct seed; the sink regenerates the
  sequence from the same seed. A dropped byte, a duplicated byte, a corrupted
  byte, a channel swap and a loopback are all caught. (The seeds confirmed the
  routing is `rx0→tx0`, `rx1→tx1`, no crosstalk — L0 could not tell, because it
  fed the same file to all four sources.)
- **Random backpressure at both ends** (~75% offered load, ~75% ready), which L0
  also never exercised.
- **Transport wire delays**, not `assign #d`. Inertial delay swallows pulses
  shorter than the delay, which is exactly wrong once the skew approaches a
  clock period. `always @(w) r <= #(d) w;` queues events instead.
- **Tristate exactly as the vendor models it** — `bufif0`, active-high
  `iodata4_t` disable, no pullups, so the net floats to `z` during the FSMs'
  turnaround states.
- **One compile, whole sweep.** Every knob is a runtime plusarg
  (`+cper`, `+tper`, `+tskew`, `+ddata`, `+ddata_t`, `+ddata_c`, `+dreq`,
  `+dack`, `+nbytes`, `+testmode`, `+neg_ack_stuck`, `+neg_data_rev`).
- **In-sim stall watchdog**, so a hung link reports `LINK FAIL link stalled`
  rather than burning the wall-clock timeout. `run.sh` flags it loudly if the
  wall clock ever wins that race.

VCS always exits 0, so `run.sh` decides the verdict on the presence of
`LINK PASS` in the log, and treats a case that passes when it was *required to
fail* as an error in its own right.

## A bench bug worth recording

The first run dropped the first byte of every channel. The cause was mine, not
the RTL's: `enable = 1'b1;` was a blocking assignment executed at a
`posedge C_clk`, so the DUT's flops and my source's flops read `tvalid` in the
same time step with no defined order — the source counted a handshake the DUT
never saw. `enable` was redundant (`resetn` already gates everything) and is
gone. Reset is now released 100 ps off any clock edge for the same reason.

## What this still does not prove

- **Metastability / MTBF.** See above. Static CDC analysis, not simulation.
- **Anything about pads, `iodata4_e`, or the DFX partition boundary.** The link
  here is an ideal tristate net with injected delay.
- **Reset sequencing between the two domains.** Both ends share one async
  `resetn` released off-edge. Real bring-up will not be so tidy: the shell is up
  long before the DUT leaves reset, and a partial reconfiguration will yank the
  controller out from under a live target. That is the next thing to test.
- **Throughput or latency targets.** Nothing here is a performance claim.
