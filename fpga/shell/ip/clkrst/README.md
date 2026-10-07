# `dut_clkrst` — CLKRST clock/reset regmap block

**Real, synthesizable RTL** (graduated from the A1 Phase-0 stub — regmap
v0.1). AXI4-Lite front-end for the DUT-clock DRP MMCM (`clk_wiz_dut` BD cell)
and the generator for the three DUT-domain resets that leave the shell as
partition pins (`dut_resetn`, `rp_resetn`, `dbg_resetn`; async-assert /
sync-deassert per `partition-pins.md`'s clock/reset-domain rule).

## Register map (`CLKRST` @ `0x44A0_0000`, shell-regmap.md v0.1)

| Off | Reg | Access | Bits | Behaviour |
|---|---|---|---|---|
| 0x00 | `RESET_CTRL` | RW | `[0]` dut_resetn_req, `[1]` rp_resetn_req, `[2]` dbg_resetn_req (1 = released) | reset `3'b000` = all three held asserted until firmware releases them. |
| 0x04 | `DUT_CLK_SEL` | RW | `[7:0]` preset id | selects a DUT-clock preset (DRP sequencing FSM is a documented placeholder). |
| 0x08 | `DUT_CLK_DRP` | RW | `[15:0]` DRP window | arbitrary DRP write window (spec §5 "set DUT clock"). |
| 0x0C | `STATUS` | RO | `[0]` mmcm_locked (2-FF synced), `[1]` dut_clk_alive (heartbeat) | write accepted-no-effect. |

Register offsets, field layouts, and reset values match the contract exactly;
there are no added registers.

Decode note: the **full local word address** is decoded. Only 0x00/0x04/0x08/
0x0C respond; every other offset in the 64 KB page is unmapped — reads return
0, writes are inert. Writes to the RO `STATUS` and to unmapped offsets are
accepted at the protocol level (`BRESP=OKAY`, no effect); the contract defines
no error cases.

> **RESOLVED (2026-07-09).** This block used to decode only `addr[3:2]`, so
> every offset `≥ 0x10` aliased onto a real register — *for reads and writes*.
> A stray write to 0x10 therefore aliased onto `RESET_CTRL` and could silently
> rewrite the DUT/RP/dbg reset-request bits (i.e. yank the RP out of, or into,
> reset). Found by the SystemRDL decode-equivalence bench (`poc/systemrdl/`,
> `EQUIV_ALL_RESULT.txt` — clkrst showed 2 divergences at 0x10 vs the
> generated full-address decode). Fix: `waddr_idx`/`raddr_idx` now span the
> full local word address (`addr[C_S_AXI_ADDR_WIDTH-1:ADDR_LSB]`) and the
> `IDX_*` params were widened to `IDX_W`, so only the four mapped offsets match
> and everything else falls to `default`. Mapped-offset behaviour is
> bit-for-bit unchanged.

## Verification

`verilator --lint-only -sv fpga/shell/ip/clkrst/dut_clkrst.sv` passes clean.
Regmap-level cocotb bench: `make -C tests BLOCK=clkrst run-one`.
