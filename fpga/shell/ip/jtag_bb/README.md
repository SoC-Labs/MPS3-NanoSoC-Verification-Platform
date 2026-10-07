# `jtag_bb` — JTAGBB pin-wiggler (remote_bitbang JTAG backend)

> **STAGED / UNVALIDATED scaffolding (2026-07-28).** New block, "test-tomorrow"
> ground-work. **NOT linted, elaborated, or simulated.** It is a one-for-one
> structural mirror of the proven `fpga/shell/ip/swd_bb/swd_bb.sv`. See
> `docs/planning/MPS3_JTAG_DEBUG_DESIGN.md` for the full design and the
> proven-vs-unproven ledger. Nothing here claims to build.

The JTAG analogue of `swd_bb`: the register block backing an OpenOCD
`remote_bitbang` **JTAG** server (`firmware/jtag_server/`, proposed TCP 6921),
the same way `swd_bb` backs the SWD `swd_server` on 6920. The `jtag_server`
firmware translates `remote_bitbang` JTAG characters into `DRIVE` writes /
`SAMPLE` reads, and this block is nothing more than that register state wired
straight onto the JTAG partition pins. **The firmware IS the JTAG engine** —
all protocol timing (TCK edges, TAP state, shift counts) is done in software;
there is deliberately zero sequencing logic here.

This is the **Option A** (software bit-bang, buildable-now) DUT-debug path. The
**Option B** hardware `axi_jtag` shifter (`0x44AF_0000`, KR260-style, faster
but BLOCKED on the encrypted IP's host driver) would *replace* this whole
block, not extend it. The two are **mutually-exclusive** — one DUT SWJ-DP, one
JTAG wire-set.

## Register map (`JTAGBB` @ `0x44B1_0000`)

| Off | Reg | Access | Bits | Behaviour |
|---|---|---|---|---|
| 0x00 | `DRIVE` | RW | `[0]` tck, `[1]` tms, `[2]` tdi | write-through: each bit combinationally wired to the matching partition pin (`jtag_tck` / `jtag_tms` / `jtag_tdi`). Reset `3'b000` = TCK low, TMS/TDI low. Reads back the driven state. |
| 0x04 | `SAMPLE` | RO | `[0]` tdo | the `jtag_tdo` partition pin, 2-FF synchronized into `s_axi_aclk`. Live level at read time (exactly `remote_bitbang` 'R' semantics). A write is accepted-no-effect. |

Base `0x44B1_0000` is a distinct 64 KiB page (chosen so it collides with
nothing, including the batched `axi_jtag`/`axi_uart16550` static-add at
`0x44AF`/`0x44B1`). The base is set in the BD Address Editor, not in the RTL.
The `srst` reset char maps to **CLKRST's `dbg_resetn`** (the same pin the SWD
path uses), not to anything in this block; `trst` is ignored (no TRST wire
crosses the boundary — the RM straps `ntrst=1`).

Decode note: the **full local word address** is decoded, exactly as in
`swd_bb.sv` (and for the same CSR-decode-regression reason — the BD
instantiates `C_S_AXI_ADDR_WIDTH=32`, so a narrower decode would kill every
register on silicon). Offsets ≥ 0x08 are unmapped: reads return 0, writes are
inert (`BRESP=OKAY`), so a stray write cannot alias onto `DRIVE`.

## vs `swd_bb` (what changed)

JTAG is *simpler* than SWD — no bidirectional tristate:

| | swd_bb (SWD) | jtag_bb (JTAG) |
|---|---|---|
| DRIVE bits | `{swclk, swdio_o, swdio_oe}` | `{tck, tms, tdi}` |
| SAMPLE bit | `swdio_i` | `tdo` |
| tristate | yes (`o`/`oe`/`i` split on SWDIO) | **no** (TDI out, TDO in are separate wires) |
| partition pins | `swd_clk`/`swd_dio_o`/`swd_dio_oe`/`swd_dio_i` | `jtag_tck`/`jtag_tms`/`jtag_tdi`/`jtag_tdo` |
| bit budget | 3 out + 1 in | 3 out + 1 in (unchanged) |

The AXI4-Lite FSM, the full-address decode, the 2-FF input synchronizer, and
the "firmware IS the engine, zero HW sequencing" doctrine are copied verbatim
from `swd_bb.sv`.

## Clock domains / CDC

Single-domain block (`s_axi_aclk`), and that is the point — identical rationale
to `swd_bb`:

- **Outputs need no synchronizer** — the JTAG interface is source-synchronous
  with a clock this block itself generates (`tck` is a register bit). Pacing is
  firmware-write-limited: every TCK half-period is at least one full AXI-Lite
  write, so `tms`/`tdi` are stable long before/after every `tck` edge the DUT's
  TAP samples on.
- **`jtag_tdo` is asynchronous** to `s_axi_aclk` → 2-FF synchronized on the
  static side. Firmware reads `SAMPLE` at remote_bitbang pace, which dwarfs the
  2-cycle synchronizer latency.

## Verification (OWED — none run yet)

None performed. When this is really built, mirror `swd_bb`'s bench plan:

- `verilator --lint-only fpga/shell/ip/jtag_bb/jtag_bb.sv` (single file, no
  includes) — **not yet run.**
- `tests/jtag_bb/`: write each `DRIVE` pattern 0–7, check the three pins follow
  combinationally + read-back; wiggle `jtag_tdo_i` and confirm `SAMPLE[0]`
  follows after the 2-FF latency; check `SAMPLE` upper bits read 0 and a
  `SAMPLE` write is inert; check reset pins `000`.
- `tests/csr_decode_width/`-style: elaborate at `C_S_AXI_ADDR_WIDTH=32`, drive
  base+offset, confirm the full-address decode responds only at 0x00/0x04.
- Suggested `tests/common/list_benches.py` entry:
  `"jtag_bb": (_p("fpga","shell","ip","jtag_bb"), ["jtag_bb.sv"])`.

## Integration (OWED — an A6 boundary re-mint)

Instantiating this block is part of the **SWD→JTAG partition-boundary
re-mint** (`docs/contracts/partition-pins.md` "Processor debug" group swap):
it re-keys `static_id`, rebuilds every RM partial, and re-runs `pin_check`. See
`docs/planning/MPS3_JTAG_DEBUG_DESIGN.md` §1 and §6. Also add
`jtag_bb`/`JTAGBB` to `platform_regs.h` and the shell BD (`shell_bd.tcl`,
Address Editor @ `0x44B1_0000`) at that time — none of which is done by this
scaffold.
