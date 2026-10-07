# `tests/hostio4_hotswap` — a DFX swap cannot safely leave the target alone

Two benches. **L2a** (`make park`) is the analysis: force the target into each
FSM state and ask which decoupler constants return it to idle. **L2b**
(`make swap`) is the demonstration: run real traffic, fire a swap
mid-transaction, and see whether the link comes back. L2b **corrected** one of
L2a's conclusions — see the callout below.

**Result: the shell must reset `hostio4_target` on every partial
reconfiguration.** No constant value the DFX decoupler can drive returns the
target to idle from every state it might be caught in. The best candidate,
`ioreq1=0, ioreq2=0`, still wedges **5 of its 9 states**.

```sh
source set_env.sh
make -C tests/hostio4_hotswap
```

Raw output is committed in [`RESULT.txt`](RESULT.txt).

## The situation

In the harness the hostio4 **controller** is inside the reconfigurable
partition — nanoSoC drives `ioreq1`/`ioreq2` and receives `ioack` (the P1 pin
mapping) — while the **target** would live in the static shell. A partial
reconfiguration deletes the controller *mid-transaction*. The DFX decoupler
holds the partition boundary at constant safe values, the new DUT loads, and
comes up reset.

The target, meanwhile, is still sitting wherever the old controller left it.

## Why a wedged target is not a cosmetic problem

`hostio4_target_fsm.v` ends with:

```verilog
assign ioack_o = fsm_state[0];   // signal ACK handshake toggle
```

So a target stuck in an odd-numbered state asserts `ioack` permanently. L1's
`neg_ack_stuck` negative control
([`tests/hostio4_link_cdc`](../hostio4_link_cdc/)) already established that a
stuck `ioack` hangs the controller **forever** — its FSM waits on `ioack_s`
transitions that never come.

A target wedged by swap *N* can therefore hang the **first hostio4 transaction
of DUT *N+1***, long after the swap completed, with nothing obviously to blame.
On nanoSoC that first transaction is the bootrom's ADP banner.

> **Confirmed by L2b.** On the RTL nanoSoC is actually built from, the next DUT
> deadlocks from exactly these **5 of 9** states, under **either** decoupler
> `ioack` value. The hang is **intermittent** — it depends where the swap lands —
> which is the harder kind of bug to chase.

## What was measured

The bench instantiates the target alone, forces `fsm_state` to each of its nine
values, holds a constant `(ioreq1, ioreq2)`, releases, and runs 64 target clocks
(≈30× the 2FF latency).

```
          (0,0)      (0,1)      (1,0)      (1,1)
  TXST    .          .          WEDGE:RXC1  WEDGE:RXDH
  RXC1    WEDGE:RXC1  WEDGE:RXDH  WEDGE:RXC1  WEDGE:RXDH
  RXDH    WEDGE:RXDL  WEDGE:RXDH  WEDGE:RXDL  WEDGE:RXDH
  RXDL    WEDGE:RXDL  WEDGE:RXDZ  WEDGE:RXDL  WEDGE:RXDZ
  RXDZ    .          WEDGE:RXDZ  WEDGE:TXSZ  WEDGE:RXDZ
  TXSZ    .          .          WEDGE:TXSZ  WEDGE:TXSZ
  TXCZ    WEDGE:TXDH  WEDGE:TXCZ  WEDGE:TXDH  WEDGE:TXCZ
  TXDH    WEDGE:TXDH  WEDGE:TXDL  WEDGE:TXDH  WEDGE:TXDL
  TXDL    .          WEDGE:TXDL  WEDGE:TXSZ  WEDGE:TXDL

  (0,0) wedges 5/9    (0,1) wedges 7/9    (1,0) wedges 9/9    (1,1) wedges 9/9
```

The reason is structural, not incidental: the FSM advances on `ioreq2`
**transitions**, never on its level. Half its states are literally
`X: nxt = (ioreq2_s) ? X : Y` — a held level cannot unwind them.

### Three requirements fall out

1. **The shell must reset the target on every swap.** No constant parks it.
   (Toggling `ioreq2` three times, with `ioreq1` low, does park it from all nine
   states — the bench finds that minimum — but reset is simpler and obviously
   sound. The escape sequence is recorded in case reset is ever inconvenient.)
2. **The decoupler must *drive* `iodata4`, not float it.** `RXC1` branches on the
   raw `iodata4_i[0]`, and a `z` condition is not "false": Verilog X-merges both
   arms. Measured — with `ioreq2` high, a floating bus poisons `fsm_state` to X.
3. **It must hold `ioreq1` low.** Held high, it drags even an *idle* target out
   of `TXST` into `RXC1`.

The Makefile asserts all three, so if the RTL ever changes to make any of them
unnecessary, this bench fails and the requirement gets revisited rather than
silently outliving its reason.

## Guarding against a false pass

If the state encoding copied into the bench ever drifts from
`hostio4_target_fsm.v`, `force` lands on a value no `case` arm matches, the FSM
falls through to `default` (which is `TXST`), and **every experiment reports
"parks"** — a silent, total false pass.

The self-check catches that without assuming the answer under test: it drives a
transition the case table must make (`TXST` + `ioreq1` → `RXC1`), which exercises
the `TXST` arm and confirms the `RXC1` encoding. Then it confirms `TXST` is a
fixed point of `(0,0)`. Both must hold before any result is printed.

## A test bug worth recording

The float experiment first reported `FLOAT SAFE`, which was wrong.

`release` lets the non-blocking assignment scheduled at the last *forced* clock
edge land — and that NBA's right-hand side was evaluated while `iodata4` was
still driven. Floating the bus *after* the release therefore let the FSM step
straight past the one data-dependent arm the experiment existed to test. The
float must be applied **before** the release. Instrumenting a single case made it
obvious; reasoning about it did not.

---

# L2b (`make swap`) — the same question, on a live link

L2a forces FSM states. L2b runs real LFSR traffic on all four channels, fires a
swap **while a transaction is in flight**, applies a policy, and scores a fresh
stream afterwards.

**The swap must land mid-transaction.** Firing it once phase 1 has drained finds
the target idle in `TXST` with nothing to wedge, and then *every* policy passes.
The first version of this bench did exactly that and reported a cheerful,
worthless green. `+swap_state=<S>` now pins where the swap lands.

## What `pol_none` (leave the target alone) actually does

Swapping from each state, with `pol_none`:

| caught in | decoupler drives `ioack=0` | `ioack=1` |
|---|---|---|
| `TXST`, `RXDZ`, `TXSZ`, `TXDL` | recovers | recovers |
| `RXC1` | **DEADLOCK** | **DEADLOCK** |
| `RXDH` | **DEADLOCK** | **DEADLOCK** |
| `RXDL` | **DEADLOCK** | **DEADLOCK** |
| `TXCZ` | **DEADLOCK** | **DEADLOCK** |
| `TXDH` | **DEADLOCK** | **DEADLOCK** |

Exactly L2a's five wedging states, under **both** ack values. **The decoupler's
`ioack` safe value rescues nothing.** The shipped controller boots into `OFFZ`
("off/deselected from reset") and does not spontaneously start a transaction, so
it never emits the `ioreq` edges that could unwedge the target.

So **the hang is intermittent** — it depends where the swap lands. That is a
considerably nastier bug than a deterministic one.

> **An earlier revision of this page said `RXC1` + `ioack=0` "recovers by luck".**
> That was measured against an orphan hostio4 tree whose controller FSM has no
> `OFFZ` state. It is not the RTL that ships. See
> [`../hostio4_golden/README.md`](../hostio4_golden/README.md) "Provenance".

The failure mode is always a clean deadlock. Not once did the link resynchronise
into silent data corruption, which is the one small mercy here.

## What the policies do

`pol_treset` (reset the target) and `pol_escape` (three `ioreq2` toggles) both
recover from **all nine states under both `ioack` safe values** — 36 runs, all
green. `make matrix` prints the full 54-run sweep.

## The corrected requirement

> Reset `hostio4_target` on every swap — or drive the three `ioreq2` escape
> toggles. **The decoupler's `ioack` safe value is irrelevant** — it rescues
> nothing.

## Bench bugs found on the way

Four, each of which had produced a confidently wrong result:

0. **The bench ran against the wrong hostio4 tree** — an orphan no repo file
   references. Its controller lacks the `OFFZ` reset state, which alone flipped
   the `RXC1`/`ioack=0` cell from `DEADLOCK` to `recovers`, and produced a whole
   "recovers by luck" narrative that was not true of the shipped design. The
   Makefiles now derive `HOSTIO4_RTL` the way `filelist.tcl` does.

1. **The swap landed after the stream drained**, so the target was idle and all
   three policies "passed". Fixed by triggering on `+swap_at` bytes and then
   waiting for a chosen FSM state.
2. **`join_any` leaves its losing siblings running.** Phase 1's error watcher
   survived into phase 2 and mislabelled the failure `phase-1: byte mismatch`.
   Fixed with `disable fork`.
3. **The sources kept injecting during the swap window**, so a stale phase-1
   byte landed as phase-2's byte 0 and *every* policy failed identically. The
   boundary is now frozen in the same time step the state is caught, the sources
   are silenced, and `drain` swallows the link until it is quiet. `drain` drops
   in lockstep with the stimulus reset, never before it.

## What neither bench covers

- **The decoupler IP itself.** Safe values are modelled as constants at the
  pins; no Xilinx DFX decoupler is instantiated.
- **`iodata4_e`/`iodata4_t` behaviour at a real partition boundary.**
- **The controller side losing its target** — it cannot; the target is static.
- **Reset sequencing against the real `swap_fsm`.** L2b resets the controller
  for a fixed window; the shell's actual ICAP timing is not modelled.
