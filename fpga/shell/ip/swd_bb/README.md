# `swd_bb` — SWDBB pin-wiggler (remote_bitbang backend)

**Real, synthesizable RTL** (new block, `W-RTL-NEWIP` — regmap v0.1 I5).
The register block backing the OpenOCD `remote_bitbang` server
(`net-protocol.md` port 6920): the `swd_server` firmware (A3) translates
`remote_bitbang` characters into `DRIVE` writes / `SAMPLE` reads, and this
block is nothing more than that register state wired straight onto the SWD
partition pins. **The firmware IS the SWD engine** — all protocol timing
(SWCLK edges, turnaround, parity) is done in software; there is
deliberately zero sequencing logic here.

This is v1 of the spec's SWD-probe plan. v2 (a ported CMSIS-DAP
`SW_DP.c`/`DAP.c` hardware engine) would *replace* this block, not extend
it.

## Register map (`SWDBB` @ `0x44A7_0000`, shell-regmap.md v0.1)

| Off | Reg | Access | Bits | Behaviour |
|---|---|---|---|---|
| 0x00 | `DRIVE` | RW | `[0]` swclk, `[1]` swdio_o, `[2]` swdio_oe | write-through: each bit is combinationally wired to the matching partition pin (`swd_clk` / `swd_dio_o` / `swd_dio_oe`). Reset `3'b000` = SWCLK low, SWDIO released (flag #3). Reads back the driven state. |
| 0x04 | `SAMPLE` | RO | `[0]` swdio_i | the `swd_dio_i` partition pin, 2-FF synchronized into `s_axi_aclk`. Live level at read time (exactly `remote_bitbang` 'R' semantics). A write is accepted-no-effect. |

Both offsets match the contract exactly; there are no added registers.
Note the srst `remote_bitbang` char maps to **CLKRST's `dbg_resetn`**
(shell-regmap.md SWDBB note), not to anything in this block.

Decode note: the **full local word address** is decoded. Offsets ≥ 0x08 are
unmapped — reads return 0, writes are inert. Writes to the RO `SAMPLE` and to
unmapped offsets are accepted at the protocol level (`BRESP=OKAY`, no effect);
the contract defines no error cases.

> **Was flag #1, now RESOLVED (2026-07-09).** This block used to decode only
> address bit `[2]`, so 0x08 aliased DRIVE and 0x0C aliased SAMPLE — *for reads
> and writes*. A stray write to 0x08 therefore silently rewrote the SWD pins.
> Found by the SystemRDL decode-equivalence bench (`poc/systemrdl/`, see
> `EQUIV_RESULT.txt`), which showed the generated full-address decode ignoring
> exactly the writes the hand decode honoured. `tests/swd_bb/` now pins the
> corrected unmapped semantics.

## Port groups (see `swd_bb.sv`)

1. **AXI4-Lite slave** — `SWDBB` regmap, standard `s_axi_*` naming
   (identical FSM to `dfx_ctl.sv`/`dut_clkrst.sv`/`board_gpio.sv`).
2. **SWD partition pins** (`partition-pins.md` "Processor debug" group,
   shell's view, pin-name + direction suffix): `swd_clk_o`, `swd_dio_o_o`,
   `swd_dio_oe_o` (outputs from `DRIVE`), `swd_dio_i_i` (input to
   `SAMPLE`). The o/oe/i split-tristate crosses the RP boundary as three
   scalars — recombination happens RM-side, same convention as
   `mdio_*`/`dut_gpio_*`.

## Clock domains / CDC

Single-domain block (`s_axi_aclk`), and that is the *point*:

- **Outputs need no synchronizer** — the SWD interface is
  source-synchronous with a clock this block itself generates (`swclk` is
  a register bit). Pacing is firmware-write-limited: every SWCLK
  half-period is at least one full AXI-Lite write (many `s_axi_aclk`
  cycles; effectively far longer through the remote_bitbang TCP path), so
  `swdio_o`/`swdio_oe` are stable long before/after every `swclk` edge the
  DUT's DAP samples on.
- **`swd_dio_i` is asynchronous** to `s_axi_aclk` (DUT-driven during
  turnaround) → 2-FF synchronized on the static side, per
  `partition-pins.md`'s CDC rule. Firmware reads `SAMPLE` at
  remote_bitbang pace, which dwarfs the 2-cycle synchronizer latency —
  note for A3: read `SAMPLE` at least one AXI round-trip after the
  relevant `DRIVE` clock-edge write (automatic in practice).

## What's real vs seamed

Everything is real; there is nothing to seam — the block is intentionally
a pure pin-wiggler. (The *protocol* seam is architectural: it lives in the
`swd_server` firmware, and v2's CMSIS-DAP hardware engine is a
whole-block replacement.)

## Verification

`verilator --lint-only fpga/shell/ip/swd_bb/swd_bb.sv` passes clean
(single file, no includes).

Bench notes for A5 (no `tests/swd_bb/` exists yet — benches are A5's
deliverable, plan row W-RTL-NEWIP):

- Regmap-level only, no SWD protocol needed: write each `DRIVE` pattern
  0–7, check the three pins follow combinationally (same cycle as the
  write commits) and that `DRIVE` reads back what was written.
- Wiggle `swd_dio_i_i` and confirm `SAMPLE[0]` follows after the 2-FF
  latency; check `SAMPLE` upper bits read 0 and that writing `SAMPLE`
  changes nothing.
- Check reset state: pins `000` before any write.
- Optional integration bench: a tiny Python SW-DP line-turnaround model
  driving DRIVE/SAMPLE sequences (mirrors what `swd_server`'s harness in
  `firmware/test/` already exercises byte-wise — see docs/STATUS.md's
  "swd_server remote_bitbang byte dispatch" row).
- Suggested `tests/common/list_benches.py` entry:
  `"swd_bb": (_p("fpga","shell","ip","swd_bb"), ["swd_bb.sv"])`.

## TODO(A1) / flags for A6

(Duplicated as the ambiguity block at the bottom of `swd_bb.sv`.)

1. ~~**Address aliasing** above 0x04 (1-bit decode)~~ — **RESOLVED 2026-07-09.**
   The decode was widened to the full local word address, so unmapped offsets
   read 0 and their writes are inert. This was not merely cosmetic: under the
   1-bit decode a stray write to 0x08 aliased onto DRIVE and rewrote the SWD
   pins. Regression-guarded by `tests/swd_bb/`.
2. **No edge-latched sample mode**: `SAMPLE` is the live synchronized
   level (what remote_bitbang wants). Hardware-assisted capture belongs to
   the v2 CMSIS-DAP block.
3. **Reset drive polarity** chosen `3'b000` (SWCLK low, SWDIO released);
   contract is silent. OpenOCD re-initializes outputs on connect, so only
   the pre-connect window is affected — flag if the nanosoc DAP prefers
   SWCLK idle-high.
