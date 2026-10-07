# `nanosoc_exp/sw` — DUT-side firmware for the expansion socket

This is the **Cortex-M0 (DUT) side** of the expansion socket. It runs *inside*
nanosoc — not on the shell's MicroBlaze — and drives the reference accelerator
`ahb_clcd.sv` at `0x6000_0000`.

| File | What |
|---|---|
| `ahb_clcd.h` | register map for `ahb_clcd` (mirrors `fpga/rp/nanosoc_exp/README.md` §7) + the demo API |
| `ahb_clcd.c` | the reference driver — **read this as the worked example** |

## What it does

`ahb_clcd_demo()` initialises the HX8347-D panel and paints a colour test frame,
then does it **again, forever**. Press `USER_nPB[1]` on the board and this frame
replaces the shell's status screen.

The re-init-every-pass loop is deliberate and load-bearing — there is **no grant
wire** back into the DUT, and **every handover hard-resets the panel**, so the
correct design is to re-configure and repaint unconditionally. The long comment in
`ahb_clcd.c` §3 explains it; **do not "optimise" the re-init out**.

## Building it into your DUT image

The panel init sequence is **not** duplicated here. It is the same board-proven
table the shell uses, so a DUT build must compile and link it, and add its
directory to the include path:

```
    firmware/clcd/hx8347_init.c          # link this (bus-agnostic {op,val} table)
    -I firmware/clcd                     # for hx8347_init.h
```

i.e. your DUT toolchain builds `ahb_clcd.c` + `firmware/clcd/hx8347_init.c` and
calls `ahb_clcd_demo()` from your `main()` after the bus is up. The table's
provenance is `firmware/clcd/PANEL_PROVENANCE.md`.

## In simulation

The block is proven in `tests/nanosoc_lcd/` (built by W2-E) against the same
cocotb HX8347-D panel model the shell CLCD uses. See
`fpga/rp/nanosoc_exp/README.md` §9.
