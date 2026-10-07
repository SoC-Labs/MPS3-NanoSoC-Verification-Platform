# DUT notes — `clcd`

RTL: `fpga/shell/ip/clcd/clcd.sv`, module `clcd` (params
`C_S_AXI_ADDR_WIDTH=12`, `C_S_AXI_DATA_WIDTH=32`, `FIFO_DEPTH=128`,
`READ_PATH=0`, `WR_LO_INIT=4`, `WR_HI_INIT=4`, `CS_SETUP_INIT=2`). Single file,
no includes, single clock domain (`s_axi_aclk`). **Real RTL from day one** (no
Phase-0 stub marker) — `list_benches.py` reports `clcd` READY once the file
lands. Contract: `fpga/shell/ip/clcd/README.md` (FROZEN port list) +
`docs/contracts/shell-regmap.md` v0.4 CLCD @ `0x44AC_0000`.

Bench binds to the frozen port names (confirmed by reading `clcd.sv`):

- `s_axi_*` — standard AXI4-Lite slave (Xilinx-template ready-pulse FSM, same
  shape as `telem`/`dfx_ctl`; both blessed handshake styles are served by the
  shared `AxiLiteMaster` in `tests/common/regmap.py`).
- Panel pads (8080): `clcd_pd_o[7:0]`, `clcd_pd_i[7:0]` (tied 0 here —
  `READ_PATH=0`), `clcd_pd_oe`, `clcd_cs_n_o`, `clcd_wr_n_o`, `clcd_rd_n_o`,
  `clcd_rs_o` (0=cmd/1=data), `clcd_bl_o`, `clcd_rst_n_o`. Strobes active-low.
  No partition pins (static-side peripheral; no DFX decoupler entry).

Registers (shell-regmap.md v0.4): `CTRL`@0x00, `CMD`@0x04 (W1, RS=0),
`DATA`@0x08 (W1, RS=1), `STATUS`@0x0C (ro), `READ`@0x10 (ro), `TIMING`@0x14.
`CTRL` = [0] enable, [1] backlight, [2] reset_n, [3] fifo_reset (self-clearing),
[4] read_start (self-clearing, `READ_PATH=1` only — see below).

## The panel bus model (`clcd_panel_model.py`)

A cocotb monitor that plays the HX8347-D on the 8080 bus. It samples the panel
pads once per `s_axi_aclk` posedge (ReadOnly) and decodes the `{RS, byte}`
stream latched on each **WR_n rising edge** while CS_n is asserted — the one
hardware fact of an Intel-8080 write cycle. It exposes:

- `.sequence` — the decoded ordered `[(rs, byte), ...]`.
- per-strobe `wr_low_cycles` / `cs_setup_cycles` / `n_rise` (for timing).
- `.any_cs_low/.any_wr_low/.any_rd_low` + `.idle` — "any pad ever asserted",
  reset via `.reset_activity()`; used by the no-read-side-effect sweep, which
  must catch a *read* (CLCD_RD) cycle that drives no WR strobe.

## Why the bench uses a SYNTHETIC vector (not the real init table)

Deliberate, per plan §11 and the README init-table seam. The block is
protocol-agnostic; this bench proves it streams an **arbitrary** `{RS,byte}`
sequence faithfully, using its own invented vector. It does **not** consume
`firmware/clcd/hx8347_init.h` — a sibling agent owns that table and its values
are unproven until the panel lights on the board. Keeping the real table out of
the bench is what stops a *wrong* table from looking green: the block's
correctness (does it stream what firmware pushed?) and the table's correctness
(are those the right HX8347-D bytes?) are separate questions, provable in
different places (sim vs. board).

## Behaviour pinned by these benches (confirmed against clcd.sv)

- **Init/order**: a synthetic mixed CMD/DATA vector decodes off the pads in
  exact order with the right RS per byte; every cycle has CS asserted, PD stable
  through the WR pulse, and (READ_PATH=0) RD never asserts.
- **TIMING convention** (from the FSM, `clcd.sv` `ST_SETUP/STROBE_LO/STROBE_HI`,
  `phase_load(v)=v-1`): each phase lasts exactly its TIMING field of
  `s_axi_aclk` cycles. Measured: **WR-low width == `wr_lo`**, **CS-setup interval
  == `cs_setup`**, and the back-to-back **strobe period == `wr_lo+wr_hi+cs_setup+1`**
  (the `+1` is the single `ST_IDLE` cycle between cycles). The timing test
  asserts both the absolute widths and, field-isolating, that changing one field
  moves only the matching interval by exactly that delta (tested at 4 TIMING
  values).
- **CTRL pads**: `backlight`→`clcd_bl_o`, `reset_n`→`clcd_rst_n_o`; reset value
  `0x0` (dark + held in reset, matching the legacy tie-off). `fifo_reset`
  self-clears (reads back 0) and flushes the FIFO to empty.
- **FIFO backpressure / zero loss**: fill to `STATUS.fifo_full`
  (`FIFO_DEPTH=128`), `fifo_level` (STATUS[15:8]) tracks the count, writes while
  full are **dropped** (BRESP=OKAY, `awready`/`bvalid` still complete,
  `fifo_level` unchanged — never a stall), and after release every byte pushed
  while `!full` arrives on the bus in order, none lost/duplicated.

### The one behavioural assumption, and how it's guarded

The 8080 pads carry **no ready/backpressure** from panel→master, so the only
faithful "panel-side stall" is to park the FSM. The backpressure & fifo_reset
tests fill the FIFO with the FSM **parked** (`CTRL=0`: `enable=0` + panel held
in reset), relying on *parked ⇒ the FIFO does not drain*. This is **confirmed**
in `clcd.sv` (`do_launch_write = idle && enable_q && !fifo_empty`; `push_en`
does not depend on `enable`). It is also **asserted directly** — the fifo_reset
test checks the panel model saw **zero** strobes during the parked fill — so if a
future RTL change ever drained while parked, the failure is explicit and points
here, not a mysterious count mismatch.

## READ path — READ_PATH=1 secondary mode (`make READP=1`)

The default build ships write-only (`READ_PATH=0`): `READ` reads 0, `clcd_rd_n_o`
held deasserted, `clcd_pd_oe` tied 1. `test_clcd.py` checks that.

`test_clcd_readpath.py` (`make READP=1`, a separate elaboration with
`-pvalue+clcd.READ_PATH=1` — cocotb bakes one parameter per simv) pins the
**v0.4 read contract**: a panel read-back is armed by **writing `CTRL.read_start`
(bit 4)**; `READ` @ 0x10 has **no read side effect**. The centrepiece
`test_read_sweep_has_no_panel_side_effect` runs the FSM live (`enable=1`) and
reads every offset in the page, asserting the panel side never moves — it FAILS
against a read-arms design and PASSES against the write-armed one.

Non-vacuity: `test_read_path_param_is_one` checks `dut.READ_PATH==1` when the
parameter handle is exposed, so a silently-ignored `-pvalue` override can't make
the sweep pass with no read path present.

## Read-arm contract: resolved (RTL revised 2026-07-10 @ 16:03)

Timeline worth keeping: the first `clcd.sv` (@ 15:55) armed the read cycle on an
AXI **read** of `READ` — the shape the integrator rejected in the v0.4 update
(READ must have no read side effect; a page-dump / decode-width sweep would
otherwise drive 8080 cycles at the panel). The RTL was **revised @ 16:03** to the
write-armed contract: `read_start_pulse = ctrl_wr && s_axi_wdata[4] &&
(READ_PATH!=0)` sets `rd_req_q`; the `IDX_READ`-read arm is gone; `READ` is a
pure capture register.

Against the revised RTL, `make READP=1` is **3/3 PASS**: the read datapath
elaborates (`READ_PATH==1`), the full-page read sweep drives **zero** panel
activity (the anti-regression check), and the `CTRL.read_start`→poll
`STATUS.busy`→read `READ` sequence returns `{valid, rdata}` = the byte the model
presented on `clcd_pd_i`. Had the sweep been run against the pre-revision RTL it
would have fired on the very read-side-effect it exists to forbid — which is the
whole point of keeping the check.

The default `READ_PATH=0` build (`make`, 7/7 PASS) is independent: the read-arm
is compiled out, so `READ` is inert regardless.

**No open discrepancies** against the port contract, shell-regmap.md v0.4, or the
block README — the RTL matches its documented contract on every point benched.
