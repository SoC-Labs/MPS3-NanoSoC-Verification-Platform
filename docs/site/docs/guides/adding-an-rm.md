# Adding an RM

How to get a new design into the swappable partition — from an empty directory
to a DUT running on the board, proved by one command.

*Prerequisites: the [Concepts](../index.md#where-to-go-next) pages, or at least
[Reconfigurable Modules & the DUT](../concepts/reconfigurable-modules-and-dut.md).
You need Vivado only for steps 5–7; steps 1–4 need nothing but Python and
`make`.*

!!! info "Every command on this page exists today"
    …except where a line says otherwise. Where a command is landing in a parallel
    workstream it is marked inline, so you can tell what you can run right now
    from what is arriving.

---

## The shape of the job

An RM is three things:

1. **A wrapper** whose port list is *exactly* the 47-port partition boundary (148 bits; static 0x44EE76D5 and 0x72BB0A36 -- docs/contracts/partition-pins.md),
   with your design instantiated inside it.
2. **An entry in the RM library** (`fpga/dfx/rm_list.tcl`) — the one registry the
   whole flow reads.
3. **An overlay** — the partial bitstream, the clearing bitstream, and a manifest
   keyed to one specific shell.

Steps 1–4 below produce the first two and prove them, with no FPGA tools at all.
Steps 5–7 build the overlay and deploy it.

---

## 1. Copy the template

```sh
python3 fpga/rp/_template/new_rm.py myrm --design-id 0x0007
```

That copies [`fpga/rp/_template/`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/fpga/rp/_template/)
to `fpga/rp/myrm/` and applies every rename: the wrapper file and module become
`rp_myrm_wrapper`, the timing constraints become `myrm_ooc.xdc` (the exact name
`ooc_synth.tcl` looks for), and the two `RENAME ME` lines in `ooc_synth.tcl` and
the `design_id` `localparam` are filled in.

You get:

| File | What it is |
|---|---|
| `rp_myrm_wrapper.sv` | the boundary, an `rm_id` block, a marked slot for your design, and safe inert tie-offs for everything else |
| `ooc_synth.tcl` | out-of-context synthesis → the checkpoint the DFX flow consumes, plus utilization and timing reports |
| `myrm_ooc.xdc` | the standalone timing constraints |
| `rm_list_snippet.tcl` | the block to paste into the RM library in step 3 |
| `README.md` | the rules, and where to find a worked example of each pattern |

Doing it by hand is the same four renames plus the `localparam`; the script
exists because "copy one of the seven existing RM directories and guess which
parts were incidental" is exactly the onboarding problem it removes.

!!! note "Choosing a `design_id`"
    It must be unique — `rm_list.tcl` errors at source time if two RMs share one,
    because a duplicate makes RM-load verification ambiguous. Read the allocated
    set out of the registry rather than out of any document:

    ```sh
    tclsh <<'EOF'
    source fpga/dfx/rm_list.tcl
    foreach r $RM_ORDER { puts "$r  $RM_LIB($r,design_id)  $RM_LIB($r,rm_id)" }
    EOF
    ```

---

## 2. Put your design in the wrapper

Open `fpga/rp/myrm/rp_myrm_wrapper.sv` and find:

```systemverilog
// >>> YOUR DUT GOES HERE <<<
```

Instantiate your design and connect the boundary groups it uses. Delete the
matching lines from the tie-off block below it as you go; anything you leave
tied off stays legal and inert.

**Three rules that are not style preferences.** Each is a statement about where
logic may physically live in a DFX design:

- **No clock generation inside an RM.** Every DUT clock is made in the static
  shell and arrives as a partition pin.
- **No pin-facing `IOB` / `OLOGIC` registers inside an RM.** Those pad sites are
  static-only; a re-register stage that wants IOB packing belongs in the shell.
- **No shell↔DUT AXI.** The boundary is slow scalars and low-rate streams. That
  is what keeps a wedged DUT from hanging a bus the shell needs to reach the
  board.

And the one that fails quietly instead of loudly: **drive every output**. An
undriven output floats the shell's input. `qspi_csn = 1'b1` (flash deselected) is
the one intentional non-zero safe-idle value in the boundary, and it matches what
the DFX decoupler clamps to.

!!! tip "Do not vendor external sources"
    If your design lives in a sibling checkout, reference it through an
    environment variable in a `filelist.tcl` next to the wrapper —
    `fpga/rp/eth_ss/filelist.tcl` is the worked example. `ooc_synth.tcl` already
    has the guarded `source` for it. A second copy of a DUT's RTL drifts, and
    confidential IP must not enter this repository at all.

---

## 3. Register it in the RM library

Paste `fpga/rp/myrm/rm_list_snippet.tcl` into
[`fpga/dfx/rm_list.tcl`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/fpga/dfx/rm_list.tcl) —
**both** halves: the `set RM_LIB(rm_myrm,…)` block beside the others, and the
`rm_myrm` key appended to `RM_ORDER`.

Two fields decide how the build treats your RM:

- **`synth_mode`** — `"inline"` if the RM is one dependency-free `.sv` the build
  may synthesise itself in seconds; `"prebuilt"` if it pulls in an external
  source tree, in which case its checkpoint must arrive pre-staged. Get this
  wrong in the `"inline"` direction and the build does not fail — it *succeeds*
  with your design inferred as an empty black box, which is worse.
- **`version`** — `1.0.0` is the honest floor for an RM that has been
  `pr_verify`'d against a real locked static and exercised on silicon. It is part
  of `rm_id`, so bumping it changes the id.

`RM_ORDER` is what puts your wrapper into the gate in the next step. Register
first, then check.

---

## 4. Prove the boundary — before Vivado

```sh
make -C fpga/dfx pin-check
```

This is the cheapest gate in the repository and it runs in about a second. It
compares every registered wrapper against the frozen boundary and catches all
five drift classes — missing port, wrong direction, wrong width, extra port,
wrong `NGPIO`:

```
pin_check — RM wrappers vs fpga/shell/boundary.yaml (47 signals, NGPIO=16)
  RM set from: fpga/dfx/rm_list.tcl RM_ORDER

  rm             module                 ports  result
  -------------- ---------------------- -----  ------
  rm_myrm        rp_myrm_wrapper           47  PASS
```

**Why this matters more than it looks.** Every RM links into the *same*
black-boxed partition cell in the *same* locked static checkpoint. A wrapper that
differs by one port, one bit of width, or one direction does not "mostly work" —
it fails Vivado's `link_design` / `pr_verify`, after a full out-of-context
synthesis, place and route. `pin-check` reaches the same verdict before you spend
any of that.

Then run the rest of the board-free gate:

```sh
make check-ci
```

Stage 2 also asserts three-way agreement on your `rm_id` — the wrapper
`localparam`, the `rm_list.tcl` entry, and (once it exists) the overlay manifest.
A mismatch there is a named CI failure instead of a wrong design silently loading
on hardware.

---

## 5. Synthesise the RM out of context

*Needs Vivado 2026.1* (the RC2 static 0x44EE76D5 was minted with it; RM checkpoints must match the static's Vivado version).

```sh
make -C fpga/dfx rm-myrm-dcp
```

Read the two reports this produces before going further:

- `util_rm_myrm.rpt` — does the design **fit** the partition's Pblock?
- `timing_rm_myrm.rpt` — are your internal paths actually constrained? If it says
  *"no user specified timing constraints"*, nothing was timed and the clean
  result means nothing. Fix `myrm_ooc.xdc` first.

---

## 6. Fold it into a shell, and build the overlay

*Needs Vivado, and an existing locked static build.*

```sh
make -C fpga/dfx add-rm-myrm
make -C fpga/dfx overlays
make -C fpga/dfx verify
```

`add-rm-<name>` is the **incremental** path, and it is the one you want. It
reuses the existing `static_routed_locked.dcp` and reads `static_id.txt` rather
than recomputing it, so it routes only your RM, `pr_verify`s it against the
reference configuration, and leaves `static_id` **unchanged** — every overlay
already in the field stays valid.

!!! danger "Do not run `make prod` to add one RM"
    `prod` re-mints: it rebuilds the static and therefore produces a **new**
    `static_id`, and every overlay keyed to the old one stops fitting the moment
    it completes. A `.dcp` is a zip with embedded timestamps, so even rebuilding
    an *unchanged* shell mints a new id. One mint costs a full re-key of every
    overlay plus a firmware rebuild and a board reflash — which is why unrelated
    static changes get deliberately batched into one.

`make overlays` writes `fpga/dfx/overlay/myrm/` — the partial, the clearing
bitstream (mandatory on UltraScale: the region must be blanked before a new RM is
written), and `manifest.json` carrying `rm_id`, lengths, CRC-32s and the
`static_id` it was built against. `make verify` round-trips every manifest.

Re-run `make check-ci`: your manifest is now in the stage-2 `rm_id` and
`static_id` gates.

---

## 7. Deploy it, and prove it

*Needs the board.*

Take the lease first — the board is shared, and an interrupted swap can wedge the
shell.

```sh
python -m pyverify.cli lease acquire
```

Then deploy:

```sh
pip install -e host/pyverify
python -m pyverify.cli deploy --host <board-ip> --overlay fpga/dfx/overlay/myrm
```

`deploy` is the whole inner loop: it loads the manifest, checks it against the
shell's **live** `static_id` and refuses a mismatch, pushes the clearing and
partial bitstreams, issues the `swap`, and confirms the `rm_id` the fabric reads
back. If that command returns success, the right design is in the partition and
the shell proved it by reading a register — not by trusting a filename.

Or from Python, which also gives you the post-swap re-attach:

```python
from pyverify import Mps3Board

with Mps3Board("<board-ip>") as board:
    result = board.deploy("myrm")
    result.reattach.apply()                    # debug is gated during a swap
    board.uart0.assert_contains(b"myrm ready")  # the verdict is the console
```

**Your pass/fail comes from the console or the debug port**, never from
telemetry: this platform has no power sensor, and `telemetry()` says so rather
than returning a plausible zero.

!!! warning "Board discipline"
    - Take the lease before touching the board; release it with the *same* holder
      id, or it stays held.
    - The board's data IP is reachable only from the lab's board-management host,
      so push/console/debug commands run through it.
    - **Never interrupt a swap**, and never halt the shell's MicroBlaze mid-swap —
      it kills the TCP stream and needs a JTAG shell reload to recover.
    - A swap that did not reach DONE leaves debug and console gated. Re-run the
      liveness checks after any failure.

    The full runbook is
    [Board bring-up](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/BOARD_BRINGUP.md);
    the orientation version is [Deploying an RM](deploying-an-rm.md).

---

## When it goes wrong

| Symptom | Almost always |
|---|---|
| `pin-check` says `MISSING port` / `width is N` | you edited the port list. Take it from the template again — it is checked against the generated boundary, so it is right by construction |
| `pin-check` says `MISSING <path>` | `rm_list.tcl`'s `top` and the `.sv` filename disagree. The build derives the source path as `<wrapper_dir>/<top>.sv` |
| `pr_verify` fails after a long build | the same boundary problem `pin-check` catches in a second. Run it first, always |
| the build "succeeds" but your design is empty | `synth_mode` says `"inline"` for an RM whose sources come from outside the repo. It was inferred as a black box |
| the pusher refuses the overlay | its manifest's `static_id` does not match the shell on the board. Check which shell is *fielded* — that is a different question from which is *minted*, and they are routinely different |
| the swap completes but `rm_id` is wrong | the wrapper `localparam`, `rm_list.tcl` and the manifest disagree. `make check-ci` stage 2 names which |
| timing report looks clean | check it is not clean because nothing was constrained. `"no user specified timing constraints"` is not a pass |

---

## Reference

- [`fpga/rp/_template/README.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/fpga/rp/_template/README.md)
  — what each template file is for, and where to find a worked example of each pattern
- [`docs/ARCHITECTURE.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/ARCHITECTURE.md)
  — the one entry document: the boundary, DFX and `static_id`, the protocol, the gates
- [`docs/contracts/partition-pins.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/contracts/partition-pins.md)
  and [`partition-timing.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/contracts/partition-timing.md)
  — the frozen boundary and its timing contract
- [`fpga/dfx/rms/README.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/fpga/dfx/rms/README.md)
  — the RM authoring contract in full
- [`docs/contracts/overlay-manifest.md`](https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/blob/master/docs/contracts/overlay-manifest.md)
  — the overlay schema
- [The board-free gate](board-free-gate.md) — what `make check` covers
