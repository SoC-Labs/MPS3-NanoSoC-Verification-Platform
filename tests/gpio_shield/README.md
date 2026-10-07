# tests/gpio_shield — output-only DUT GPIO → shield pad passthrough

A board-free check that a DUT-driven `dut_gpio_o[n]` reaches a physical shield pad
`SH0_IO[n]` through the **real** `board_gpio` block plus the per-bit IOBUF proposed in
[`docs/CONNECTOR_SURVEY.md` §6](../../docs/CONNECTOR_SURVEY.md). Validates the exact RTL
path the "route `dut_gpio` to the shield/Pmod" change would deploy — *before* any board time.

```
source set_env.sh
make -C tests/gpio_shield          # build + run
make -C tests/gpio_shield result   # regenerate RESULT.txt
```

## Path under test

```
[RP] dut_gpio_o/oe --> board_gpio OWN mux (own=0 => DUT owns)
     --> board_pad_o/oe --> IOBUF (I/T/O/IO) --> SH0_IO pad
     pad --> board_pad_i --> 2-FF sync --> dut_gpio_i   (read-back loop)
```

`board_gpio` is the real synthesizable RTL (`fpga/shell/ip/board_gpio/board_gpio.sv`), driven
with AXI idle so `own_q` stays 0 (DUT owns every bit). The IOBUF is modelled behaviourally
(`I=board_pad_o`, `T=~board_pad_oe`, `O=board_pad_i`, `IO=SH0_IO`) — identical function to the
Xilinx `IOBUF` primitive in §6's sketch — with a weak external pulldown on each pad so a hi-Z
(released) pad is observable.

## What it proves (VERDICT=PASS, errors=0)

- **Output drive:** `dut_gpio_o` reaches `SH0_IO` byte-exact across a walking-1, all-ones,
  all-zeros, and `0xA5A5`/`0x5A5A` — with `board_pad_oe` following the DUT's OE (own=0).
- **Read-back loop:** the driven pad value returns to `dut_gpio_i` through the 2-FF synchronizer.
- **Output-only safety (the key one):** with the DUT still driving `0xFFFF` but **OE=0**, the pad
  goes **hi-Z** (reads the external pulldown, 0) — so an output-only GPIO never fights an external
  driver on the shield header. Confirmed per-bit too (mixed OE `0x00FF`).

This matches the bench decision (2026-07-10) that the first DUT-GPIO use is **output-only**: the
RTL path is sound and the released state is safe. It does **not** exercise a real Xilinx IOBUF or
pin constraints (those land with the §6 static change, which re-mints `static_id`); it proves the
*logic* path end-to-end. See `docs/CONNECTOR_SURVEY.md` §7 for the shield-channel allocation
(SH0 vs SH1) and the board signal-integrity caveats.
