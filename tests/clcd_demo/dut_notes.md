# `tests/clcd_demo/` — DUT notes

**DUT:** `rp_clcd_demo_wrapper` (`fpga/rp/clcd_demo/`) — the whole RM, elaborated
at the real partition boundary, with `clcd_core` pulled in from
`fpga/shell/ip/clcd/`.

**What the bench watches:** `dut_gpio_o[15:0]` and `dut_gpio_oe[15:0]`, and
nothing else. Those two vectors are the only things that cross into the shell,
and the tunnel **bit map** they carry
([`docs/contracts/dut-display-tunnel.md`](../../docs/contracts/dut-display-tunnel.md) §2)
is precisely the thing that has never been exercised from the RM side. A bench
that peeked at `u_demo.lcd_cs` would prove the engine works and say nothing
about the wire.

## What sits between the RM and the glass, and how it is modelled

`tunnel_model.py` models both hops:

* **The KVM** inverts the tunnel's ACTIVE-HIGH strobes to the panel's
  active-low pads, and it is the only place that inversion happens (§3 — a
  safety property, because the decoupler clamps the tunnel to `0x0` for the
  whole of every partial reconfiguration and active-high makes `0x0` mean "all
  strobes idle"). The checker therefore reads `wr`/`cs` in the tunnel's sense
  and latches on the tunnel's **WR falling** edge — which is the panel's `WR_n`
  **rising** edge.
* **The panel** latches `{RS, PD}` on that edge while `CS` is asserted. Same
  hardware fact `tests/clcd/clcd_panel_model.py` encodes for the shell block's
  pads; this file is its DUT-side twin.

It also enforces the §5 **timing floor** — every 8080 phase ≥ 8 `dut_clk`
cycles, PD/RS stable ≥ 4 cycles either side of the strobe — plus "PD/RS may not
move under CS", "never release CS mid-strobe", "WR only while selected", and
"every RESERVED bit is 0".

## Nothing about the expected byte stream is written in this directory

`card_model.py` builds the expectation, and the panel init table inside it comes
from `firmware/clcd/hx8347_init.c` through `fpga/rp/clcd_demo/hx8347_table.py` —
the **same parser** the RTL's ROM generator uses. 124 register writes with
per-byte provenance (`firmware/clcd/PANEL_PROVENANCE.md`) are not retyped into a
bench.

What *is* re-derived here is the **test card layout**, from
`clcd_demo_gen.sv`'s prose description rather than its implementation. If the
RTL and `card_model.py` disagree, one of them is wrong; that is the point of
having both.

## The three elaborations

A 320×240 repaint is 153,600 bytes ≈ 4 million `dut_clk` cycles, and the counter
test needs three of them. So:

| `make MODE=` | geometry | what it is for | runtime |
|---|---|---|---|
| `card` (default) | 32×16 | the whole bench: sequence, test card, counter, freeze, tunnel rules | ~100 s |
| `fullgeom` | 320×240 (the shipping value, from the firmware table) | the address window must equal the firmware's own values — the one thing a shrunk frame cannot check. Stops after the window + first pixels | ~4 s |
| `control` | 32×16, `CS_SETUP=2 WR_LO=WR_HI=4` | **THE CONTROL** — green only if the bench REJECTS it | ~8 s |

Every layout boundary in `clcd_demo_gen.sv` is a fraction of `W`/`H`, which is
why shrinking the frame keeps every region of the card. `CLK_HZ` is what the RM
*believes* `dut_clk` is; it scales the firmware table's 215 ms of `HX_DLY`
delays down to something a simulator can sit through. The bench's actual clock
period is a fixed 20 ns.

Parameters reach the DUT through VCS `-pvalue+rp_clcd_demo_wrapper.<P>=<v>`, and
the bench reads the same values back out of the environment, so the `-pvalue+`
line and the Python expectation cannot drift apart.

## The control, twice over

**In the simulator** — `make MODE=control` rebuilds the RM with `clcd_core`'s
own SHELL defaults (`CS_SETUP=2`, `WR_LO=WR_HI=4`). Those are correct at the
100 MHz shell clock and **below** the tunnel's floor at 50 MHz — i.e. the
mistake a maintainer actually makes, by carrying the engine across without
reading §5. The run passes only when the bench rejects it:

```
CONTROL OK -- the bench rejected the mutated RM: cycle 10: CS->WR setup is
2 cycle(s), floor 8 (docs/contracts/dut-display-tunnel.md section 5 ...)
```

**Without a simulator** — `test_tunnel_checker.py` runs under plain `pytest`
(root `make check-ci` stage 3, no VCS, no licence) and feeds the checker
deliberately broken traces: sub-floor setup/WR/hold, a **swapped command/data
line**, a set RESERVED bit, PD moving under CS, CS released mid-strobe. Each
must raise. It also proves the rejection comes from the *rule* and not from a
broken stimulus generator: the same sub-floor trace, fed to a checker told to
accept it, decodes cleanly.

The swapped-RS case is the one worth understanding. `RS` says "is this byte a
register INDEX or its DATUM", and it sits next to `WR` in the same tunnel byte.
Invert it and the bus stays perfectly well-formed — right strobes, right
timing, right bytes, right count — while the panel writes every register index
into whatever register was previously selected. `test_swapped_line_is_invisible_to_a_byte_count`
states the check that would *not* have caught it.

## Board-free gates in this directory

Both run under `pytest tests` in `make check-ci`; neither needs a simulator.

* `test_tunnel_checker.py` — the control, above.
* `test_init_rom_fresh.py` — the RM's generated init ROM must stay a fresh
  render of `firmware/clcd/hx8347_init.c` (`gen_init_rom.py --check`), the ROM
  words must decode back to the firmware table entry for entry, MADCTL must
  carry the shipped rotation, and the RM's recomputed address window must land
  on the firmware's own values at the shipping geometry.

## Running it

```sh
source set_env.sh            # miniconda py3.10 + cocotb 2.0.1 + VCS
make -C tests/clcd_demo                 # MODE=card
make -C tests/clcd_demo MODE=fullgeom
make -C tests/clcd_demo MODE=control
make -C tests/clcd_demo all-modes
python3 -m pytest tests/clcd_demo -q    # the board-free half
```

## Not registered in the runner

`tests/common/list_benches.py` and `tests/Makefile`'s `BENCH_DIRS` are shared
files with other owners, so this bench is not yet in `make -C tests`. The
snippet to add is in this lane's `RESULT.md`. Until then it runs from its own
directory, and its pure-Python half runs in CI regardless.
