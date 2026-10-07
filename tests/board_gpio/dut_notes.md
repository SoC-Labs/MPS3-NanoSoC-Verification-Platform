# DUT notes — `board_gpio`

**Status (refreshed 2026-07-08, A5 verification pass): REAL RTL landed and
benched green.** `fpga/shell/ip/board_gpio/board_gpio.sv` now exists as a
real, synthesizable AXI4-Lite slave (I4 GPIO passthrough, `shell-regmap.md`
v0.1 @ 0x44AA_0000) — this dir was originally the FIRST bench written ahead
of any RTL, but that RTL has since landed. `tests/common/
dut_presence.rtl_ready()` reports READY, so `test_board_gpio.py`'s
`@cocotb.test(skip=NO_RTL)` benches now RUN (green under VCS, 4/4 in the
W-SIM pass) and the AXI4-Lite SVA protocol checker is bound onto the slave
(`tests/common/sva/bind_board_gpio.sv`). The port list assumed below was
confirmed correct against the real RTL when it landed (the RTL header notes
it deliberately adopted these already-authored names).

## What this block is, per the contracts

- `docs/contracts/shell-regmap.md` v0.1, GPIO @ `0x44AA_0000`:
  `IN`@0x00 (ro), `OUT`@0x04, `OE`@0x08, `OWN`@0x0C — "Default = the DUT's
  `dut_gpio_*` drives the pads; the host can override per-bit when no DUT
  owns them."
- `docs/contracts/partition-pins.md`'s "Board-port / GPIO passthrough"
  group (shell ⇄ RP): `dut_gpio_o`/`dut_gpio_oe` (I, from the RP) and
  `dut_gpio_i` (O, to the RP). Width `NGPIO` is a build parameter, v0
  default 16. The RP side of this is **already real** in two existing RMs —
  `fpga/dfx/rms/rm_led/rm_led.sv` (drives `BLINK_BITS` of `dut_gpio_o/oe`)
  and `fpga/dfx/rms/rm_greybox/rm_greybox.sv` (ties both to `'0`) — so the
  RP-facing port names/widths below are not a guess, just not yet wired to
  a shell-side block.

## Assumed port list (forward spec — confirm against the real RTL once A1
## lands it; update this file and the bench together)

- `s_axi_*` — AXI4-Lite slave, standard names matching every other block
  in this suite (`regmap.AxiLiteMaster.from_dut()` binds directly, same as
  clkrst/dfx_ctl/mdio_phy_model/gen_checker) — `IN`/`OUT`/`OE`/`OWN` per
  the offsets above.
- `dut_gpio_o_i [NGPIO-1:0]` (I) / `dut_gpio_oe_i [NGPIO-1:0]` (I) — from
  the RP (partition-pins.md `dut_gpio_o`/`dut_gpio_oe`).
- `dut_gpio_i_o [NGPIO-1:0]` (O) — to the RP (partition-pins.md
  `dut_gpio_i`).
- `board_pad_o [NGPIO-1:0]` (O) / `board_pad_oe [NGPIO-1:0]` (O) / `board_pad_i [NGPIO-1:0]` (I) — the
  actual physical pad tri-state triplet (constraints live in
  `fpga/shell/constraints/`, out of this bench's scope — partition-pins.md:
  "Which physical board pin each `dut_gpio_*` bit maps to is fixed in the
  shell constraints... not here").

## What the bench checks (once RTL lands)

Pure regmap-level OWN-mux behavior — see `tests/board_gpio/
test_gpio_mux_logic.py` for the independently-derived truth table this
bench is expected to reproduce against real hardware: default (OWN=0)
gives the DUT full control of the pad; OWN=1 per-bit hands that bit to
host OUT/OE instead; GPIO.IN and `dut_gpio_i` both sample the same
resolved pad value regardless of who drove it.

## Ambiguity flagged for A6

`shell-regmap.md` doesn't say whether `OWN`/`OUT`/`OE` reset to 0 on
`s_axi_aresetn` alone or need the DUT's own reset too — for safety
(host never wants to un-intentionally own a bit at power-on if a DUT is
mid-boot) this bench will assume `OWN` resets to 0 (matching "0 = DUT owns
the bit (default)"'s framing as the reset value, not just a documentation
convention) — confirm with A1 once the RTL exists.
