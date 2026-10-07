# DUT notes — `telem`

RTL: `fpga/shell/ip/telem/telem.sv`, module `telem` (params
`C_S_AXI_ADDR_WIDTH=12`, `C_S_AXI_DATA_WIDTH=32`, `SIM_FAKE_DATA=0`,
`FAKE_PERIOD=64`). Single file, no includes. **Real RTL from day one**
(W-RTL-NEWIP, never a Phase-0 stub). Proven green under VCS 2022.06-SP2 +
cocotb 2.0.1, 2026-07-06: seam mode 6/6 (`make`), fake mode 2/2
(`make FAKE=1`).

Real port list (confirmed by reading the RTL, 2026-07-06):

- `s_axi_*` — standard AXI4-Lite slave (Xilinx-template FSM).
- INA228 engine seam (`s_axi_aclk`-synchronous by contract):
  `ina228_enable_o`, `ina228_alarm_en_o`, `ina228_sample_valid_i`
  (1-cycle strobe), `ina228_bus_mv_i[31:0]`, `ina228_curr_ua_i[31:0]`,
  `ina228_power_mw_i[31:0]`, `ina228_i2c_err_i`, plus `alarm_i` (async
  allowed, 2-FF'd inside). No partition pins.

Registers (shell-regmap.md v0.1 TELEM table — all five offsets match the
contract exactly; constants in `tests/common/regmap.py`): `CTRL`@0x00,
`BUS_MV`@0x04(ro), `CURR_UA`@0x08(ro), `POWER_MW`@0x0C(ro),
`STATUS`@0x10(ro).

## Two bench modes — and why seam mode is the default

The block README offered A5 both routes; **both are covered**, with seam
mode (`SIM_FAKE_DATA=0`) as the default target because:

1. it is the synthesis configuration (fake mode is a bench aid);
2. the `ina228_*` ports are the agreed contract for the follow-up
   `ina228_i2c_master` engine — driving them from the bench makes
   `test_telem.py` the seam's specification test;
3. fake mode cannot exercise i2c_err stickiness/set-dominance,
   enable-gated sample rejection, or adversarial mid-stream data churn.

Fake mode runs as `make FAKE=1`: VCS `-pvalue+telem/SIM_FAKE_DATA=1
-pvalue+telem/FAKE_PERIOD=16` parameter overrides (proven to work — the
bench asserts `dut.SIM_FAKE_DATA == 1` so a silently-ignored override
cannot vacuously pass), separate `sim_build_fake`/`results_fake.xml` so
the two elaborations never collide. `test_telem_fake.py` freezes readings
via CTRL.enable=0 before each formula check because FAKE_PERIOD=16 is
shorter than three back-to-back AXI reads (anti-tearing; "disable holds"
is itself proven in seam mode).

## Behaviour pinned by these benches (incl. documented quirks)

- Atomic triple latch on `sample_valid` only; seam churn without a strobe
  changes nothing; strobes while `enable=0` ignored; disable holds.
- `STATUS.i2c_err`: sticky, cleared by ANY `CTRL` write (RTL ambiguity
  #2's chosen semantics — W1C/clear-on-read would be the v0.2
  alternatives), set-dominant, and NOT cleared by writes to RO offsets.
- `STATUS.alarm`: live 2-FF-synchronized `alarm_i` AND `CTRL.alarm_en`,
  not sticky (ambiguity #3).
- Fake formulas: `BUS_MV=1200`, `CURR_UA=1000+10k`, `POWER_MW=CURR_UA>>10`,
  no i2c_err; the sample index survives disable (only reset zeroes it).
- Decode: in-window unmapped offsets 0x14/0x18/0x1C read 0. Offsets
  ≥ 0x20 alias mod 0x20 (bits [4:2] decode — house convention, same as
  dfx_ctl/dut_clkrst/board_gpio/uart_bridge). Note a WRITE to 0x20 would
  alias CTRL and thus also clear the sticky i2c_err — same A6
  decode-width question as flagged in tests/uart_bridge/dut_notes.md.

## Discrepancies found

None against shell-regmap.md v0.1 or the block README — the RTL matches
its documented contract on every point checked; no RTL fixes were needed.
(The ≥0x20 aliasing note above is a README-wording nit shared by the
whole shell-IP family, not a telem-specific defect.)
