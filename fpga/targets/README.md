# `fpga/targets/` — target collateral for mps3-nanosoc-platform

A **target** is a build of this design for a board. One directory per target,
and the directory is the whole difference between one and another.

```
fpga/targets/mps3-hbi0309c/          <- TARGET_DIR, created by fpga-flow-init
    nanosoc_mps3.pins.xdc           XDC_PINS        pins, IO standards, placement
    nanosoc_mps3.timing.xdc         XDC_TIMING      clocks, IO delays, exceptions
    nanosoc_mps3.drc.xdc            XDC_DRC         config, DRC severities
```

`TARGET` defaults to `BOARD`, which is why this one is named after the board,
and on most projects it stays that way. It becomes its own thing when one board
carries several builds that differ in **collateral rather than in RTL** — a
debug build with an ILA and a wider constraint set, a bring-up build with a
reduced top level. To add one: copy the directory, set `TARGET` in `design.mk`
(or on the command line, `make impl TARGET=debug`), and edit the copy.

---

## What belongs in a target directory

| | variable | what it is |
|---|---|---|
| the three XDCs | `XDC_PINS`, `XDC_TIMING`, `XDC_DRC` | below |
| more constraints | `XDC_EXTRA` (ordered list), `XDC_OPTIONAL` (`COND:path`) | anything the three do not cover |
| post-route Tcl | `XDC_POST_ROUTE` | **waivers**, and anything else that is procedural Tcl — see below |
| the board-level top | `TOP_HDL` | the wrapper that instantiates your SoC and wires it to the board |
| the block design | `BD_TCL`, `BD_OVERLAY_TCL` | the BD script and the overlays applied over it, in order |
| the XDC baseline | `XDC_BASELINE` | the committed record of what each constraint file matched |

**The board-level top lives here, not in the RTL flist.** The flist describes
the SoC; the wrapper that gives it a clock buffer, reset conditioning, IO
buffers and a debug bridge exists only because there is a PCB underneath, and
it changes when the board changes. That is the definition of target collateral.

## What does not

**Facts about the PCB** — the device, the oscillator frequency, the bank
voltages, the `.bin` style, the bench's lease and program names. Those are
board-pack keys in `fpga/board/mps3-hbi0309c/board.tcl`, and the test is one
question: *would this still be true for a completely different design on this
board?* If yes it is a board fact. A **pin assignment is not**: it is a fact
about *this design meeting that board*, so it is here.

**Anything about the flow.** If you find yourself wanting to add a build
setting to this directory, it is a `design.mk` variable.

---

## Every XDC must be named by a variable in `design.mk`

> **AN `.xdc` IN A TARGET DIRECTORY THAT NO VARIABLE NAMES IS AN *ERROR* FROM
> `make check`. THAT IS DELIBERATE.**

The flow this replaces discovered its constraints by a **glob** over a fixed
filename fragment. Any file named differently was silently invisible — present
in the directory, read by nothing, warned about by nobody. And because Vivado
drops a constraint that matches nothing without an error, "never read" and
"read and matched nothing" produced *the same log*.

So the flow takes explicit paths, and `make check` cross-checks the directory
against them. The variables that can claim a file are:

```
XDC_PINS        pins/placement — read in synthesis AND implementation
XDC_TIMING      implementation only
XDC_DRC         implementation only
XDC_EXTRA       a LIST, order preserved
XDC_OPTIONAL    a LIST of COND:path — read iff $(COND) is 1
XDC_POST_ROUTE  `source`d after route_design, NOT read_xdc'd
XDC_BASELINE    a ratcheted baseline, not a constraint
```

Adding a constraint file is therefore **two** edits, and the second one is the
point: create the file, then name it. Copying a file in and expecting it to be
picked up is the one thing that will not work, on purpose.

---

## The three-file split, and why it is not optional

The split is **by when the file is read**, and each file holds exactly the
constraints that must be true at that point:

| file | read in | why it must be there |
|---|---|---|
| `.pins.xdc` | **synthesis and implementation** | synthesis infers and configures the IO buffers, and a renamed port is caught at synthesis instead of ninety minutes later |
| `.timing.xdc` | implementation only | an exception read in synthesis changes what synthesis *builds*, and object names that resolve after mapping often resolve to nothing before it — silently |
| `.drc.xdc` | implementation only | nothing in it means anything to a design with no placement |

**The engine sets `USED_IN_SYNTHESIS` from the variable that named the file,
not from anything inside it.** Moving a clock definition into the pins file
does not keep it out of synthesis; it puts it in. Each file's own header
carries the long version of its rules — read those before writing constraints,
they are where the measured defects are written down.

**Order.** XDC is order-dependent in the same way SDC is: a later command
silently overrides an earlier one on the same object. The read order is the
order `design.mk` lists the files, and `XDC_EXTRA` preserves its list order.
Vivado has a *second* ordering mechanism — `PROCESSING_ORDER` on the file
object — and if anything sets it, the list order in `design.mk` no longer
describes the read order, with nothing to reconcile the two. Use one or the
other.

**A waiver is not a constraint.** Vivado rejects procedural Tcl inside an XDC,
and `create_waiver` is procedural, so a waiver file cannot be `read_xdc`'d at
all — `read_xdc` fails in a way that reads like a syntax error in your waiver.
It goes in `XDC_POST_ROUTE`, which is `source`d as ordinary Tcl after
`route_design`, which is also the only place it *could* go: a waiver waives a
finding, and the findings do not exist until the design is routed.

---

## Proving a constraint still matches something

`make xdc-lint` counts what each constraint file actually matched and compares
it against `XDC_BASELINE`, a committed file that belongs beside the XDCs it
describes. Without a baseline there is nothing for a gate to be red about: a
constraint that stops matching produces **no output at all**.

Set it after your first successful run:

```make
XDC_BASELINE ?= $(TARGET_DIR)/xdc_baseline.txt
```

For the handful of ports a design cannot be built without, the cheaper guard is
the assertion block at the bottom of the pins file — plain Tcl, read by every
flow that reads that file, including a GUI session where no hook runs.

---

## The file names

`fpga-flow-init` wrote these files from templates whose basenames begin
`block.`, rewriting that prefix to this design's `BLOCK`. That rewrite is how a
template tree expresses a filename that is only known at scaffold time; the
directory name is the same idea for `TARGET`.

The names are not magic — `design.mk` names each file explicitly and you may
call them anything — but keeping the `<BLOCK>.<role>.xdc` shape means every
artefact in a run, from the checkpoint to the bitstream to the constraints,
shares one stem and sorts together.

---

Copyright (C) 2026, SoC Labs (www.soclabs.org)
