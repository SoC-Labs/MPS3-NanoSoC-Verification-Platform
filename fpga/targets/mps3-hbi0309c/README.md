# Target `mps3-hbi0309c`

This directory is deliberately empty of collateral. The target's inputs live
with the pre-toolkit monolithic build they were proven against:

| input | where | set by |
|---|---|---|
| pins **and** timing constraints | `fpga/monolithic/nanosoc_mps3.xdc` | `XDC_PINS` in `fpga/design.mk` |
| board-level top | `fpga/monolithic/nanosoc_mps3_top.sv` | `TOP_HDL` in `fpga/design.mk` |
| board facts (part, clock, `bin_style none`) | `fpga/board/mps3-hbi0309c/board.tcl` | `BOARD` |

`fpga/design.mk` explains why neither file was moved here: each move would
change what gets built, so each needs its own before/after run.

**Why this file exists.** `make check` requires `TARGET_DIR` to exist, and
Git does not track an empty directory. Before this README, the directory existed
only in the checkout that first built the target, and every clean clone failed
`make check` with `TARGET_DIR ... is expected to exist`.

**Do not put an `.xdc` here without naming it** in `fpga/design.mk`. The toolkit
fails `make check` on an XDC in `TARGET_DIR` that no variable names, because an
unnamed XDC is one nothing reads.
