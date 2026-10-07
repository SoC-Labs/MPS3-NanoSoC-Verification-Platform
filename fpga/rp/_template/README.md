# `fpga/rp/_template/` — the copyable RM skeleton

Everything you need to start a new **Reconfigurable Module** (RM) — a design
that loads into the FPGA's swappable partition — without copying one of the
seven existing RM directories by eye and guessing which parts were incidental.

Nothing here is built by any target. The leading underscore keeps it sorted
away from real RMs, and it is deliberately **not** registered in
[`fpga/dfx/rm_list.tcl`](../../dfx/rm_list.tcl), so it never enters a build and
never appears in `pin-check`'s target set.

| File | What it is |
|---|---|
| [`rp_template_wrapper.sv`](rp_template_wrapper.sv) | The wrapper: the full 47-port / 148-bit partition boundary, an `rm_id` block, a marked slot for your DUT, and safe inert tie-offs for everything you do not drive |
| [`ooc_synth.tcl`](ooc_synth.tcl) | Out-of-context synthesis → the `.dcp` the DFX flow consumes, plus utilization and timing reports |
| [`template_ooc.xdc`](template_ooc.xdc) | The socketed OOC timing constraints (`partition-timing.md`'s `<rm>_ooc.xdc` half) |
| [`rm_list_snippet.tcl`](rm_list_snippet.tcl) | The registration block to paste into `fpga/dfx/rm_list.tcl` |
| [`new_rm.py`](new_rm.py) | Does the copy + all four renames for you, and holds this template's port list to the **generated** boundary |

**The step-by-step guide is
[docs/site/docs/guides/adding-an-rm.md](../../../docs/site/docs/guides/adding-an-rm.md).**
This README is the short version and the reference for what each file is for.

## The short version

```sh
python3 fpga/rp/_template/new_rm.py <name> --design-id 0x00NN
# then paste fpga/rp/<name>/rm_list_snippet.tcl into fpga/dfx/rm_list.tcl
make -C fpga/dfx pin-check                              # seconds, no Vivado
```

`new_rm.py` copies the directory and applies every rename: the wrapper file and
module to `rp_<name>_wrapper`, the XDC to `<name>_ooc.xdc` (which is the exact
name `ooc_synth.tcl` looks for), the two `RENAME ME` lines in `ooc_synth.tcl`,
and the `design_id`. By hand it is the same four renames plus the `localparam`.

`pin-check` is the first thing to run and the cheapest gate in the repo. Run it
before you touch Vivado.

## This template does not own the boundary

The partition boundary (47 ports since the 2026-10 ILA mint) is the most-duplicated fact in the repository — every RM
wrapper, the shell-side stub, `pin_check`'s tables, the contract, the XDC. A
template that carried its own private copy would be a machine for manufacturing
drifted wrappers.

So the port list here is checked against the **generated** one:
`fpga/shell/generated/rp_wrapper_skeleton.sv`, written by `tools/gen_boundary.py`
from `fpga/shell/boundary.yaml`.

```sh
python3 fpga/rp/_template/new_rm.py --check
```

compares them semantically — `(name, direction, width)` in order, so formatting
differences are not drift — and `new_rm.py <name>` runs it first and **refuses to
scaffold** from a drifted template. On a tree that predates the generator it says
so and exits 0 rather than passing in silence.

What this template *does* own is the **body** the generator cannot write: the
`rm_id` block and the safe-idle tie-offs, which the generated skeleton leaves as
commented guidance because it cannot know what your RM drives.

## Why the boundary is not negotiable

Every RM links into the **same** black-boxed `u_rp_dut` cell in the **same**
locked static checkpoint, and Vivado then `pr_verify`s the result against the
reference configuration. A wrapper whose port list differs by one signal, one
bit of width, or one direction does not degrade gracefully — it fails at
`link_design` / `pr_verify`, after a full out-of-context synthesis, place and
route.

`make -C fpga/dfx pin-check` ([`fpga/dfx/pin_check.py`](../../dfx/pin_check.py))
compares every registered wrapper against
[`docs/contracts/partition-pins.md`](../../../docs/contracts/partition-pins.md)
and catches all five drift classes — missing port, wrong direction, wrong
width, extra port, wrong `NGPIO` — in about a second. It runs in root
`make check` and `make check-ci` stage 2, and it derives its target set from
`rm_list.tcl`'s `RM_ORDER`, so a registered RM is checked automatically.

Directions **invert**: `partition-pins.md` states them from the *shell's* view,
and the RM is on the other side (`pin_check.py`'s `RM_DIR` map).

## The three rules an RM must obey

From [`fpga/dfx/rms/README.md`](../../dfx/rms/README.md) "RM authoring contract"
and `partition-pins.md`:

1. **No clock generation inside an RM.** Every clock arrives as a partition pin
   from the static shell's DRP MMCM.
2. **No pin-facing `IOB` / `OLOGIC` registers inside an RM.** Those pad sites
   are static-only in a DFX design (`partition-pins.md` "IOB packing note").
3. **No shell↔DUT AXI.** The boundary is slow scalars and low-rate streams;
   that is what keeps a wedged DUT from hanging a bus the shell needs.

Plus the one that fails quietly rather than loudly: **every port you do not
implement must be driven to a safe, legal, inert constant** — the tie-off block
at the bottom of the wrapper. `qspi_csn = 1'b1` (flash deselected) is the single
intentional non-zero safe value in the whole boundary and matches the DFX
decoupler's clamp.

## `rm_id`, and the three places it must agree

`rm_id` is the RM-load-verify ground truth: after a partial load the shell reads
it back from `DFXCTL.RM_ID` and compares it with the value in the pushed
partial's header. It is encoded
`{ver_major[31:24], ver_minor[23:16], design_id[15:0]}`
(`docs/VERSIONING_PLAN.md` §3.2) and is **derived**, never hand-written:
`rm_list.tcl` computes it from `(design_id, version)`.

Three sources must agree — the wrapper `localparam`, the `rm_list.tcl` entry,
and `overlay/<name>/manifest.json` — and
`scripts/harness_gates/check_rm_id_encoding.py` (stage 2) fails the build if
they ever drift. The template ships an intentionally **unallocated** placeholder
`design_id` (`0x00FF`) so a half-renamed copy cannot collide with a real RM.

## Sources that live outside this repo

Do not vendor them. Reference the sibling checkout through an environment
variable in a `filelist.tcl` next to the wrapper —
[`fpga/rp/eth_ss/filelist.tcl`](../eth_ss/filelist.tcl) and
`fpga/dfx/rms/rm_socscope/filelist.tcl` are the worked examples. `ooc_synth.tcl`
here already has the guarded `source` for it: create the file and it is picked
up; leave it absent and the wrapper is synthesised alone.

A second copy of a DUT's RTL drifts, and confidential IP must not enter this
tree at all (`CONTRIBUTING.md`, "The lab-IP boundary").

## Where the pieces you may want to reuse live

| Want | Look at |
|---|---|
| A byte-serial UART pin → the AXI-Stream byte pair | [`fpga/rp/nanosoc/uart_axis_shim.sv`](../nanosoc/uart_axis_shim.sv) (shared by `nanosoc` and `nanosoc_multicore`) |
| The smallest possible real RM | `fpga/dfx/rms/rm_led/rm_led.sv` |
| The smallest RM that moves *data* | `fpga/dfx/rms/rm_uart_echo/rm_uart_echo.sv` |
| A multi-file, externally-sourced RM | [`fpga/rp/eth_ss/`](../eth_ss/) |
| A real Cortex-M0 SoC wrapper | [`fpga/rp/nanosoc/`](../nanosoc/) |
| Generated clocks + an async group in an OOC XDC | [`fpga/rp/eth_ss/eth_ss_ooc.xdc`](../eth_ss/eth_ss_ooc.xdc) |
