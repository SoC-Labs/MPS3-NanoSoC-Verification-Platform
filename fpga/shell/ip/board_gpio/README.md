# `board_gpio` — GPIO regmap / board-port passthrough block

**Real, synthesizable RTL** (regmap v0.1 I4). The DUT reaches MPS3 board I/O
(LEDs, buttons, PMOD/Arduino, spare FMC) only through the shell, which owns
the physical pads and brokers a generic `NGPIO`-wide (v0: 16) bus across the
RP boundary. Default (`OWN=0` per bit) = the DUT's `dut_gpio_o`/`dut_gpio_oe`
drive the pads; the host overrides per-bit via `OUT`/`OE` when `OWN=1`.

## Register map (`GPIO` @ `0x44AA_0000`, shell-regmap.md v0.1)

| Off | Reg | Access | Bits | Behaviour |
|---|---|---|---|---|
| 0x00 | `IN`  | RO | `[NGPIO-1:0]` | the resolved pad value, 2-FF synced into `s_axi_aclk` (shared with `dut_gpio_i_o` so the two readers never disagree). Write accepted-no-effect. |
| 0x04 | `OUT` | RW | `[NGPIO-1:0]` | host drive value (used where `OWN=1`). Reset 0. |
| 0x08 | `OE`  | RW | `[NGPIO-1:0]` | host output-enable (used where `OWN=1`). Reset 0. |
| 0x0C | `OWN` | RW | `[NGPIO-1:0]` | per-bit mux select: 0 = DUT owns the bit (default), 1 = host override. Reset 0. |

Register offsets, field layouts, and reset values match the contract exactly;
there are no added registers. Upper bits `[C_S_AXI_DATA_WIDTH-1:NGPIO]` are
unimplemented and read back 0.

Decode note: the **full local word address** is decoded. Only 0x00/0x04/0x08/
0x0C respond; every other offset in the 64 KB page is unmapped — reads return
0, writes are inert. Writes to the RO `IN` and to unmapped offsets are accepted
at the protocol level (`BRESP=OKAY`, no effect); the contract defines no error
cases.

> **RESOLVED (2026-07-09).** This block used to decode only `addr[3:2]`, so
> every offset `≥ 0x10` aliased onto a real register — *for reads and writes*.
> A stray write to 0x14 therefore aliased onto `OUT` (and 0x18→`OE`, 0x1C→`OWN`),
> silently changing which side drives the board pads and what value they drive.
> Found by the SystemRDL decode-equivalence bench (`poc/systemrdl/`,
> `EQUIV_ALL_RESULT.txt` — board_gpio showed a read divergence at 0x10 and a
> write divergence at 0x14 vs the generated full-address decode). Fix:
> `waddr_idx`/`raddr_idx` now span the full local word address
> (`addr[C_S_AXI_ADDR_WIDTH-1:ADDR_LSB]`) and the `IDX_*` params were widened to
> `IDX_W`, so only the four mapped offsets match and everything else falls to
> `default`. Mapped-offset behaviour (including the per-byte `wstrb` writes of
> the 16-bit fields) is bit-for-bit unchanged.

## Verification

`verilator --lint-only -sv fpga/shell/ip/board_gpio/board_gpio.sv` passes clean.
Regmap-level cocotb bench: `make -C tests BLOCK=board_gpio run-one`.
