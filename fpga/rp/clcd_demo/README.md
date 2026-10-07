# `fpga/rp/clcd_demo/` — the RM that proves a DUT can drive the panel

The CLCD KVM (`fpga/shell/ip/clcd_kvm/`), the display tunnel
([`docs/contracts/dut-display-tunnel.md`](../../../docs/contracts/dut-display-tunnel.md))
and the DUT-side accelerator socket (`fpga/rp/nanosoc_exp/`) are **fielded** on
mint `0xA8C1C535`. The panel itself is lit and working — it renders the harness
status screen every day ([`docs/CLCD_PANEL_FACTS.md`](../../../docs/CLCD_PANEL_FACTS.md)).

**What has never been observed is a DUT driving it.** Not once, on any RM.

Every other candidate for that observation has a CPU in it: `rm_nanosoc` reaches
the panel through `nanosoc_exp_socket` → `ahb_clcd`, which needs the M0 to boot,
the firmware image to be right, and `CTRL.enable` to be set. If the glass stays
dark you have learned nothing about the tunnel, because five other things could
have been the cause.

This RM removes all of them. It is **pure RTL with no processor**: it starts on
reset and does exactly one thing — put a photographable test card on the panel.

| | |
|---|---|
| Boundary | the frozen 35 ports / 136 bits, unchanged (`make -C fpga/dfx pin-check`) |
| Cost to the shell | **zero** — no static rebuild, no re-mint, no overlay re-key |
| `design_id` | `0x0007` (next free after `rm_socscope`) |
| Engine | `clcd_core` from `fpga/shell/ip/clcd/` — **shared, not copied** |
| Panel init | **generated** from `firmware/clcd/hx8347_init.c` |
| Status | **sim-proven** (`tests/clcd_demo/`) and **exercised on silicon 2026-09-23**: card, white frame, bars in RGB order, counter, freeze and hand-back all PASS (`docs/evidence/2026-09-w2/p5_clcd_demo_20260923.txt`, photos beside it). The `rm_list_snippet.tcl` version bump to `1.0.0` (§ Record) is still the owner's step. |

## The files

| File | What it is |
|---|---|
| [`rp_clcd_demo_wrapper.sv`](rp_clcd_demo_wrapper.sv) | the RM top: the partition boundary, the tunnel bit map, the LED mirror |
| [`clcd_demo_gen.sv`](clcd_demo_gen.sv) | the sequencer and the test card. Read its header for what is drawn and why |
| [`hx8347_table.py`](hx8347_table.py) | reads the panel init table **out of the firmware** |
| [`gen_init_rom.py`](gen_init_rom.py) | renders that table into `clcd_demo_gen.sv`'s generated block |
| [`filelist.tcl`](filelist.tcl) | three sources, all in-repo; no external checkout |
| [`ooc_synth.tcl`](ooc_synth.tcl) | OOC synthesis → the `.dcp` the DFX flow consumes |
| [`clcd_demo_ooc.xdc`](clcd_demo_ooc.xdc) | socketed OOC timing constraints |
| [`rm_list_snippet.tcl`](rm_list_snippet.tcl) | the registry block to paste when this RM is minted |
| [`pin_check_mirror.py`](pin_check_mirror.py) | runs the real boundary gate with this RM registered, in a scratch mirror |

## Reading the picture

```
 +--------------------------------------------------------------+   <- white frame
 |                                                              |
 |  WHT  YEL  CYN  GRN  MAG  RED  BLU  BLK                      |   <- 8 colour bars
 |                                                              |
 |==============================================================|   <- grey separator
 |  # . . #  . # . .  # # . .  . . # .                          |   <- 16-cell counter
 +--------------------------------------------------------------+      MSB at the LEFT
```

* **The white frame** proves the GRAM address window really spans the panel. A
  wrong window clips it, wraps it, or leaves the glass blank.
* **The bars** prove RGB565, high-byte-first byte order and colour order. If red
  and blue trade places — and yellow with cyan — the panel's BGR bit is wrong,
  which is exactly the `[MODULE]` bring-up tweak `firmware/clcd/hx8347_init.c`
  flags on `PANEL_CTRL` (0x36). White on the **left** and black on the **right**
  also pins the orientation: 180° out and the photo shows the reverse.
* **The counter** is the liveness evidence: 16 cells, MSB leftmost, a lit cell is
  a `1`. It is the *only* thing that tells a live DUT apart from one stale frame
  the harness left behind. It increments once per completed repaint (~3/s on the
  board), and it is bumped **after** the last pixel, so a photograph can never
  catch a half-updated number.
* **The LEDs** (`USER_nLED`) carry the low 8 bits of the same counter, through
  the untouched low half of `dut_gpio` — a second, independent readout in the
  same photograph, and the one that still works if the panel handover never
  happened. It depends on `board_gpio`'s `OWN` register still being at its reset
  value of 0 ("the DUT owns every bit"); a harness that claims the LEDs takes
  this mirror away.
* **`USER_SW[0]` freezes the counter** (and only the counter — the repaint
  continues). That is the on-board negative control: if flipping it does not
  stop the number, the number is not coming from this RM.

## Proving pin-check without touching the registry

`fpga/dfx/pin_check.py` takes its target set from `RM_ORDER` in
`fpga/dfx/rm_list.tcl` — which is the mint flow's file, and registering an RM
there commits the next mint to building it and makes
`check_rm_id_encoding.py` demand an overlay manifest that does not exist yet.

So the boundary is proven against a **scratch mirror** of the registry:

```sh
python3 fpga/rp/clcd_demo/pin_check_mirror.py
#   ... rm_clcd_demo   rp_clcd_demo_wrapper   35   PASS
#   ALL WRAPPERS CONFORM — 11/11 pass
```

It copies the **real** gate into a temporary tree (a copy, not a symlink —
`pin_check` resolves its own `__file__`, so a symlink would silently check the
real registry and prove nothing), appends this RM's entry from
`rm_list_snippet.tcl`, and runs it. The gate code is re-copied every run, so
only the *registry* is synthetic. Same discipline as `fpga/rp/nanosoc_iice`,
whose two tops are also gated without being in `RM_ORDER`.

## Building it

```sh
source set_env.sh
make -C tests/clcd_demo                    # the bench (see tests/clcd_demo/dut_notes.md)
OUT_DIR=<scratch> vivado -mode batch -source fpga/rp/clcd_demo/ooc_synth.tcl
```

For a real mint: paste `rm_list_snippet.tcl` into `fpga/dfx/rm_list.tcl`, add
one line to `fpga/dfx/Makefile`

```make
RM_SYNTH_TCL_rm_clcd_demo := $(REPO_ROOT)/fpga/rp/clcd_demo/ooc_synth.tcl
```

(without it `make -C fpga/dfx rm-clcd-demo-dcp` stops with *"RM
'rm_clcd_demo' has no registered OOC synth recipe"*), add
`fpga/dfx/overlay/clcd_demo/manifest.json`, then

```sh
make -C fpga/dfx rm-clcd-demo-dcp                     # note: DASHES in the target
make -C fpga/dfx add-rm-clcd-demo BUILD=<the fielded locked tree>
make -C fpga/dfx overlays && make -C fpga/dfx verify
```

**No re-mint.** The boundary is unchanged, so this folds into the existing
locked static and every fielded overlay stays valid. The snippet lists
everything that has to land in the same commit.

---

# THE PROOF PROCEDURE (Wave C — needs the board)

Written so someone else can run it. Nothing here needs this lane.

**Time:** ~20 minutes. **You need:** the board lease, a camera (a phone is
fine), and a terminal that can reach the shell.

## 0. Preconditions — check these BEFORE taking the lease

```sh
python3 fpga/rp/clcd_demo/pin_check_mirror.py         # boundary
make -C tests/clcd_demo && make -C tests/clcd_demo MODE=fullgeom   # the bench
python3 -m pytest tests/clcd_demo -q                  # the board-free gates
```

The RM must be in the bitstream set: either folded into the fielded locked
static with `add-rm-clcd_demo`, or carried by a mint. **The shell does not need
rebuilding** — the tunnel costs zero shell change, which is the whole argument
of `docs/contracts/dut-display-tunnel.md` §1.

Confirm the running shell HAS the KVM before you start, because a shell without
it declines the verb and every later step reads as a DUT failure:

```sh
pyverify display --host 192.168.10.101            # {"ok":true,"owner":"harness"}
#   {"ok":false,"err":"clcd_kvm not present"}  =>  STOP. Wrong bitstream.
```

## 1. Take the lease

```sh
fpgahub lease acquire <board> --holder <you>   # prints a BARE token, and BLOCKS
```

## 2. Photograph the panel BEFORE loading anything

This is the baseline, and skipping it is how a proof gets argued with later. It
should show the harness status screen. **Keep this photo.**

## 3. Load the RM

```sh
pyverify deploy --host 192.168.10.101 --rm clcd_demo --src tcp
pyverify ping   --host 192.168.10.101      # rm_id must read 0x00010007
```

`--src tcp` matters over a tunnel (TFTP is UDP and will not tunnel).

At this point **the LEDs should already be counting**, before any panel
handover: the RM is running and the low half of `dut_gpio` is untouched by the
tunnel. If the LEDs are dark, stop — the RM did not load or did not start, and
nothing about the panel is being tested yet.

## 4. Flip the panel to the DUT

```sh
pyverify display --host 192.168.10.101 dut --confirm
#   {"ok":true,"requested":"dut","owner":"dut","landed":true,"polls":1,...}
```

`--confirm` is not optional here. Without it the reply carries the **committed**
owner, and the handover (drain → panel hard-reset → settle → grant) takes ~7–9 ms
— so a bare `display ... dut` routinely answers `"owner":"harness"`, and that
reply pasted into a log is worse than no log. Exit codes: **0** landed, **2**
accepted but not committed inside the timeout (**INCONCLUSIVE**), **1** the
shell declined, **3** unreachable.

(The button `USER_nPB[1]` does the same thing and works with no firmware at all.
The DUT's own `req` bit will **not** grab the panel: `CTRL.dut_req_en` resets to
0, and this RM raises `req` once shortly after reset — so if firmware enables
`dut_req_en` later there is no edge left to see. That is deliberate; see
`clcd_demo_gen.sv`.)

## 5. Photograph the panel — twice, ≥ 5 s apart

Both photos must show the test card. Read the 16-cell counter in each.

## 6. The negative control: freeze it

Flip **`USER_SW[0]` up** and photograph twice more, ≥ 5 s apart. The counter
must now be **identical** in both, while the card is still on the glass. Flip it
back down and confirm the counter resumes.

## 7. Hand the panel back

```sh
pyverify display --host 192.168.10.101 harness --confirm
fpgahub lease cancel <board> --holder <you>
```

The harness re-initialises and repaints on its own (`clcd_regain()` — the KVM
hard-resets the panel on every handover and sets `EVENT.harness_gained`). If the
screen comes back garbled, that is a **harness** bug, not a DUT one.

## What counts as a PASS

All of these, together:

1. Photo 2 shows the **test card**, not the harness status screen.
2. The **colour bars** are in order, white on the left, black on the right.
3. The **white frame** reaches all four edges.
4. The counter in photo 3 is **greater** than in photo 2.
5. Frozen (`USER_SW[0]` up), the counter **stops** and the card **stays**.
6. `display harness --confirm` exits 0 and the harness screen comes back.

That is: the DUT drove the panel, it kept drawing, the number is really its own,
and the panel came back.

## What counts as INCONCLUSIVE (and is NOT a failure)

Say so explicitly; do not round it to either verdict.

* `display --confirm` exits **2**. The shell took the request and the owner had
  not committed in the budget. Re-query (`pyverify display --host …`) and retry;
  if it settles late, the path works and the budget was short.
* The LEDs count but the panel does not change. The RM is alive; the failure is
  somewhere in tunnel → KVM → pads → glass. This is *informative* and is
  precisely why the LED mirror is there — but it is not a verdict on the RM.
* The card appears but the **colours** are wrong (red/blue and yellow/cyan
  swapped). The tunnel and the KVM are **proven**; what is wrong is one panel
  register (`PANEL_CTRL` 0x36 / `MADCTL` 0x16), and it is wrong for the harness
  too. Record it as a panel-config finding against `firmware/clcd/`, and count
  criteria 1, 3, 4, 5, 6 as passed.
* The card appears **upside-down**. Same class: `CLCD_ROTATE_180` in
  `firmware/clcd/hx8347_init.h`, shared with the harness.
* Anything at all while `board_gpio.OWN` is not 0 for the LED bits — the mirror
  is masked away and criterion 4's cross-check is unavailable.

## What counts as a FAIL

* `display dut --confirm` exits 0 (**landed**) and the panel still shows the
  harness screen after 5 s. The KVM says the DUT owns the pads and nothing the
  DUT emits is reaching them.
* The card appears **once** and the counter never moves, with `USER_SW[0]` down.
  One frame drawn, then the RM stopped — the interesting case, and the one the
  counter exists to distinguish from a stale frame.
* The card appears **torn or corrupt** in a stable way (not a photo artefact) —
  the tunnel is reaching the panel with bytes the bench says it should not be
  able to send. Capture `CLCDKVM.TUNNEL` and `STATUS` and re-run
  `make -C tests/clcd_demo MODE=fullgeom`.

## Record

Attach all photographs (including the baseline), the `pyverify ping` `rm_id`,
the `display --confirm` JSON, and the shell's `static_id`. Then bump
`rm_list_snippet.tcl`'s `version` from `0.1.0` to `1.0.0` — this repo's floor
for "pr_verified against a real locked static **and** exercised on silicon" —
and update the STATUS line in the registry entry.

---

## Why this RM exists at all

*Moved here 2026-09-10 from `docs/CLCD_KVM_PLAN.md`, which is HISTORICAL and
carries a "do not update" banner. The 2026-07-15 feasibility study stands as
written; this is the part that is still live, and it belongs with the thing it
describes.*

### What actually happened to §7

Steps 1–4 landed. The KVM is real RTL with a bench (`tests/clcd_kvm/`), the
tunnel is a frozen contract (`docs/contracts/dut-display-tunnel.md`), the
`AhbLiteMaster` BFM exists (`tests/common/ahb_lite.py`), `ahb_clcd` and the
student socket exist (`fpga/rp/nanosoc_exp/`), and all of it is **fielded** on
mint `0xA8C1C535`. The panel is lit.

**And no DUT has ever been seen driving it.** Not on `rm_nanosoc`, not on any
other RM. Everything on the DUT side of that claim is simulation.

### Why that was not just laziness

The DUT-side path as designed is only reachable through a CPU: the M0 has to
boot, the firmware image has to be right, `CTRL.enable` has to be set, and the
harness has to have opted the DUT in. Six things in series, of which the panel
is the last. When the glass stays dark you have learned nothing, because you
cannot say which of the six failed — and §6 of the tunnel contract deliberately
gives the DUT **no grant-back wire**, so the DUT cannot tell you either.

That is a fine design for a *product*. It is a bad design for a *first
observation*, and confusing the two is why this went unproven for two months.

### The fix: an RM whose entire job is to be photographed

[`fpga/rp/clcd_demo/`](../fpga/rp/clcd_demo/) — pure RTL, no processor, starts on
reset. It runs the firmware's own HX8347-D init table (generated from
`firmware/clcd/hx8347_init.c`, not retyped), re-issues the GRAM window, and
paints a fixed test card: a white frame, eight colour bars, and a **16-cell
binary frame counter**. It reuses `clcd_core` — the same 8080 engine lighting the
harness screen today — so the one component with a proof is not forked.

Three properties make it a proof rather than a demo:

1. **A human can judge it from a photograph.** Bars in the wrong order means the
   BGR bit is wrong; a missing frame edge means the address window is wrong;
   black on the left means the rotation is wrong. Each failure names its own
   cause, and each is a *panel-config* finding that the harness shares — i.e.
   even a "wrong" picture proves the tunnel.
2. **The counter separates drawing from drawn.** A stale frame the harness left
   on the glass and a live DUT are indistinguishable in one photograph. Two
   photographs, five seconds apart, are not.
3. **It carries its own negative control.** `USER_SW[0]` freezes the counter
   while the repaint continues, so "is that number really the DUT's?" is a
   question the board itself answers. The LEDs mirror the low byte of the same
   counter through the untouched low half of `dut_gpio`, which discriminates
   "the RM is dead" from "the RM is alive and the panel path is not".

Cost: **zero shell change**. No re-mint, no overlay re-key, no boundary edit —
the same argument as §2's Option B, applied one more time.

### The host step is one command

`pyverify display --host <ip> dut --confirm`. The `--confirm` is not decoration:
`display` answers with `CLCDKVM.STATUS.owner`, the **committed** owner, and the
handover takes ~7–9 ms, so a bare flip routinely replies with the owner that is
going *away*. `--confirm` re-queries until it lands, and — importantly — exits
**2**, not 0 or 1, when the shell accepted the request but the owner did not
commit in time. That is **inconclusive**, and a bring-up log that rounds it to
either verdict is worse than no log.

### §8's open questions, answered

- *"Does the DUT get `BL`/`RST`?"* — **No**, and the recommendation held. The
  KVM keeps both, which is what makes this RM safe to load: a proof RM that
  wedged could not take the panel with it.
- *"Option B's bus overload — acceptable?"* — It has now been carried by a second,
  independent implementation (`clcd_demo`) written against the contract rather
  than against `ahb_clcd`, and the bench checks the bit map from the RM side.
  That is the closest thing to a second opinion the encoding is going to get.
- *"Reserve a tunnel bit for touch?"* — Moot. The tunnel is full (§7 of the
  contract), and touch has not worked on this board at all
  ([`docs/CLCD_PANEL_FACTS.md`](CLCD_PANEL_FACTS.md) §7.6).
