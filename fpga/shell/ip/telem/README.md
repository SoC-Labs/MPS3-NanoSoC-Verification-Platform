# `telem` — TELEM board-power telemetry CSR block (INA228 seam)

**Real, synthesizable RTL** (new block, `W-RTL-NEWIP` — regmap v0.1 I9) —
with one deliberate, clearly-bounded seam. The MicroBlaze-facing register
surface for board power telemetry (INA228 voltage/current/power monitor on
I2C), feeding `net-protocol.md`'s `telemetry` verb (`mv`/`ma`/`lockup` —
the `lockup` half comes from DFXCTL's `RM_STATUS.dut_lockup`, **not** from
this block).

## What's real vs seamed — read this first

- **REAL (this file, `telem.sv`):** the entire CSR side. AXI-Lite FSM,
  `CTRL`/`BUS_MV`/`CURR_UA`/`POWER_MW`/`STATUS` register file, atomic
  three-reading sample latching, sticky `i2c_err`, gated/synchronized
  `alarm`, and a `SIM_FAKE_DATA` self-stimulus mode for benches.
- **SEAMED (follow-up module, NOT yet written):** the INA228 I2C master
  engine — `ina228_i2c_master`, to live in this directory. `telem.sv` ends
  at a sample-injection port group (`ina228_*`, below) that is that
  engine's agreed contract. **Nothing here fakes I2C**: with no engine
  attached and `SIM_FAKE_DATA=0`, readings stay 0 and `STATUS` reads
  benign — visibly "no data source", never plausible garbage.

### The engine seam contract (for whoever writes `ina228_i2c_master`)

All seam signals are **`s_axi_aclk`-synchronous by definition** (a
100/400 kHz I2C engine has no reason to own a clock domain), except
`alarm_i` which may be an async board pad (2-FF'd inside `telem.sv`).

| Seam signal | Dir (telem's view) | Meaning |
|---|---|---|
| `ina228_enable_o` | O | `CTRL.enable` — run/stop the engine's sampling loop |
| `ina228_alarm_en_o` | O | `CTRL.alarm_en` — engine may forward to INA228 DIAG_ALRT config |
| `ina228_sample_valid_i` | I | 1-cycle strobe: all three readings below are simultaneously valid (atomic latch — firmware never sees old/new mixes) |
| `ina228_bus_mv_i[31:0]` | I | bus voltage, **mV** (engine pre-scaled — flag #4) |
| `ina228_curr_ua_i[31:0]` | I | current, **µA** |
| `ina228_power_mw_i[31:0]` | I | power, **mW** |
| `ina228_i2c_err_i` | I | strobe/level on any failed I2C transaction (NACK/timeout) → sticky `STATUS.i2c_err` |
| `alarm_i` | I | alert level (engine DIAG_ALRT poll **or** board ALERT pad — flag #3) |

The engine additionally owns the I2C pads (`scl`/`sda` o/oe/i triplets),
the INA228 register sequencing (CONFIG/ADC_CONFIG/SHUNT_CAL setup, then
VBUS/CURRENT/POWER polling), and the LSB scaling math to integer
mV/µA/mW. None of that touches `telem.sv`'s port list when it lands.

## Register map (`TELEM` @ `0x44A5_0000`, shell-regmap.md v0.1)

| Off | Reg | Access | Bits | Behaviour |
|---|---|---|---|---|
| 0x00 | `CTRL` | RW | `[0]` enable, `[1]` alarm_en | reset 0 (sampling off). Any `CTRL` write also clears the sticky `i2c_err` (flag #2). |
| 0x04 | `BUS_MV` | RO | `[31:0]` | bus voltage, mV — latched atomically with the other two on each accepted sample |
| 0x08 | `CURR_UA` | RO | `[31:0]` | current, µA |
| 0x0C | `POWER_MW` | RO | `[31:0]` | power, mW |
| 0x10 | `STATUS` | RO | `[0]` alarm, `[1]` i2c_err | `alarm` = live 2-FF-synchronized `alarm_i` AND `CTRL.alarm_en`; `i2c_err` = sticky (set-dominant), cleared by any `CTRL` write |

All five offsets match the contract exactly; no added registers. Samples
are latched only while `CTRL.enable=1`; disabling **holds** the last
readings (only reset zeroes them). Unmapped offsets read 0; writes to
RO/unmapped offsets are accepted (`BRESP=OKAY`, no effect) — house
convention.

> **RESOLVED 2026-07-09 (decode-aliasing fix).** The address decode now
> spans the **full local word address** (`addr[C_S_AXI_ADDR_WIDTH-1:ADDR_LSB]`),
> so the "unmapped offsets read 0 / write-no-effect" statement above is now
> literally true. *Previously only `addr[4:2]` was decoded*, so offsets
> ≥ 0x20 aliased onto the mapped registers — a read of 0x20 returned `CTRL`,
> and a **write** to 0x20 aliased onto `CTRL` and silently toggled
> `ina228_enable_o`/`alarm_en_o` (and cleared the sticky `i2c_err`). Flagged
> by the SystemRDL decode-equivalence gate (`poc/systemrdl`); why: match the
> generated full-address decode and stop stray traffic touching real state.
> Mapped-offset behaviour is bit-for-bit unchanged.

## Parameters

| Param | Default | Meaning |
|---|---|---|
| `SIM_FAKE_DATA` | `0` | `1` = replace the seam with a deterministic internal generator (bench mode; synthesis uses 0). Constant-parameter muxing — the unused side folds away. |
| `FAKE_PERIOD` | `64` | `s_axi_aclk` cycles between fake samples |

Fake-mode values (deterministic, bench-checkable): `BUS_MV = 1200`,
`CURR_UA = 1000 + 10·sample_index`, `POWER_MW = CURR_UA >> 10`
(placeholder math ≈ mW at 1.2 V — plumbing-test values, **not** physics).
Fake mode never raises `i2c_err`; `alarm_i` stays live-from-port in both
modes so a bench can poke it directly.

## Clock domains / CDC

Single-domain block (`s_axi_aclk`). No partition pins — telemetry
measures the board rails around the DUT and never crosses the RP
boundary, so `partition-pins.md`'s crossing rule is satisfied vacuously.
The only asynchronous input is `alarm_i` (possible board ALERT pad),
2-FF synchronized on the static side; harmless if the source turns out to
be synchronous (same defensive posture as `dfx_ctl.sv`'s status taps).

## Verification

`verilator --lint-only fpga/shell/ip/telem/telem.sv` passes clean (single
file, no includes).

Bench notes for A5 (no `tests/telem/` exists yet — benches are A5's
deliverable, plan row W-RTL-NEWIP):

- **Seam mode (`SIM_FAKE_DATA=0`, default):** drive the `ina228_*` ports
  as the engine would: check `ina228_enable_o`/`ina228_alarm_en_o` follow
  `CTRL` writes; strobe `sample_valid` with distinct values and check all
  three RO registers update together (read between strobes — atomicity);
  check samples are ignored while `enable=0` and held after disable;
  pulse `i2c_err_i` → `STATUS[1]` sticks, then clears on a `CTRL` write
  (and check set-dominance: err + CTRL write same cycle → still set);
  wiggle `alarm_i` with `alarm_en` on/off → `STATUS[0]` gated live level
  (allow 2-cycle sync latency).
- **Fake mode (`SIM_FAKE_DATA=1`):** enable, wait > `FAKE_PERIOD`, check
  the documented value formulas and that `STATUS.i2c_err` stays 0.
- Suggested `tests/common/list_benches.py` entry:
  `"telem": (_p("fpga","shell","ip","telem"), ["telem.sv"])`.

## TODO(A1) / flags for A6

(Duplicated as the ambiguity block at the bottom of `telem.sv`.)

1. **`ina228_i2c_master` is the follow-up module** (this directory);
   the seam table above is its contract. Don't let another block grow an
   I2C master for this rail in the meantime.
2. **`STATUS.i2c_err` clear semantics** chosen as sticky-cleared-by-any-
   `CTRL`-write (contract silent). W1C or clear-on-read are one-line
   swaps — codify one into regmap v0.2.
3. **`alarm_i` source**: board ALERT pad (async — XDC-driven) vs engine
   DIAG_ALRT poll (sync). CSR side is agnostic; decide at BD/constraint
   time. `STATUS.alarm` is a live gated level, not sticky — flag if
   latching semantics are wanted.
4. **Scaling ownership**: engine-side (this block stores final mV/µA/mW
   integers; engine owns INA228 LSB math + SHUNT_CAL). If firmware-side
   scaling is preferred, only the engine's documented output units change.
