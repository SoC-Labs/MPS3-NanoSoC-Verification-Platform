# DUT notes — `clcd_kvm` (the CLCD KVM)

RTL: `fpga/shell/ip/clcd_kvm/clcd_kvm.sv`, module `clcd_kvm` (params
`C_S_AXI_ADDR_WIDTH=12`, `C_S_AXI_DATA_WIDTH=32`, `CLK_HZ=100_000_000`,
`PB_SYNC_STAGES=3`, and four µs reset values `RST_US_INIT=2000`,
`SETTLE_US_INIT=5000`, `TIMEOUT_US_INIT=1000`, `DEBOUNCE_US_INIT=10000`). Single
file, no includes, single clock domain (`s_axi_aclk`) with an **asynchronous
tunnel input** handled internally. **Real RTL from day one** (no Phase-0 stub
marker) — `list_benches.py` reports `clcd_kvm` READY once the file lands.

Contract (**FROZEN**, and the ONLY thing this bench was written against — the RTL
and this bench were written **in parallel by different agents** from the same
document, exactly how `clcd` itself was built):

- `fpga/shell/ip/clcd_kvm/README.md` v1.0 — ports, CSRs, FSM, gates, CDC, debounce
- `docs/contracts/dut-display-tunnel.md` v1.0 — the DUT-side wire encoding
- `docs/contracts/shell-regmap.md` §CLCDKVM (v0.5) @ `0x44AD_0000`
- `docs/CLCD_PANEL_FACTS.md` — the panel that is actually lit (RGB565, 8080,
  `BL` active-high, `RST` active-low, `READ_PATH=0`)

The block is **RESERVED / not instantiated** in the shell today; the BD slave,
the `USER_nPB1` pad and `clcd.sv`'s two new pins land in the Wave-4 static
rebuild (which re-mints `static_id` and re-keys the 8 overlays). The block + this
bench are unit-proven **now**.

## Ports the bench binds to (confirmed by reading `clcd_kvm.sv`)

- `s_axi_*` — standard AXI4-Lite slave (Xilinx-template ready-pulse FSM, the same
  shape as `clcd`/`dfx_ctl`/`telem`; served by the shared `AxiLiteMaster` in
  `tests/common/regmap.py`). Never stalls.
- **Source A (harness, synchronous)** — `h_pd_o[7:0]`, `h_pd_oe`, `h_cs_n`,
  `h_wr_n`, `h_rd_n`, `h_rs`, `h_bl`, `h_rst_n` (clcd_0's re-routed pad outputs,
  strobes active-low) + the **two new** live quiescence taps `h_busy` /
  `h_fifo_empty` (= clcd.sv's internal `busy`/`fifo_empty`) + `h_pd_i` (read-back
  forward, dead at `READ_PATH=0`).
- **Source B (DUT tunnel, asynchronous)** — `dut_gpio_o_i[15:0]`,
  `dut_gpio_oe_i[15:0]`. Upper bytes carry the 8080 bus per
  `dut-display-tunnel.md`: `o[15:8]=PD[7:0]`; `oe[8]=cs`, `[9]=wr`, `[10]=rs`,
  `[11]=rd`(rsvd), `[12]=pd_oe`(rsvd), `[13]=busy`, `[14]=req`, `[15]=spare`(rsvd).
  **Strobes are ACTIVE-HIGH** on the tunnel (safety property — the decoupler's
  `0x0` clamp = "all idle" by construction); the KVM inverts them to the pads.
  Bits `[7:0]` are the LED path and are **not** the KVM's — the models drive a
  recognisable pattern there and the bench confirms the KVM never decodes them.
- **DFX interlock** — `decouple_status`, `rp_resetn` (2-FF synchronised inside).
- **Button** — `user_npb1` (raw async pad, **active-low**, pressed = 0).
- **Panel pads** — `clcd_pd_o[7:0]`, `clcd_pd_i[7:0]`, `clcd_pd_oe`, `clcd_cs_n_o`,
  `clcd_wr_n_o`, `clcd_rd_n_o`, `clcd_rs_o`, `clcd_bl_o`, `clcd_rst_n_o` — the
  **same shape** as `clcd.sv`'s pad group, so `clcd_panel_model.py` watches them
  unchanged.
- **Status** — `owner_o` (0=HARNESS, 1=DUT).

## Register map (README §5, @ `0x44AD_0000`, 64 KiB page, decode width 32 on silicon)

| Off | Reg | Access | Reset | Notes |
|---|---|---|---|---|
| `0x00` | `CTRL` | RW + W1P | `0x0000_000C` | `[0]`src_sel (W-gated by `[16]`), `[1]`force_harness, `[2]`timeout_en=**1**, `[3]`pb_en=**1**, `[4]`dut_req_en=0, `[5]`backlight, `[6]`panel_rst_n, `[7]`bl_rst_src=0, `[8]`panel_rst_pulse (W1P), `[9]`force_switch (W1P), `[16]`src_sel_we (W1P) |
| `0x04` | `STATUS` | RO | — | owner/pending/tgt, FSM state `[18:16]`, live quiets, pb_level/raw, interlock |
| `0x08` | `EVENT` | **RW1C** | `0` | gained/lost pairs, timeout_fired, forced_revert, pb_toggle, panel_reset_done |
| `0x0C` | `PANEL_TMR` | RW | `0x1388_07D0` | `[15:0]`rst_us=2000, `[31:16]`settle_us=5000 |
| `0x10` | `TIMEOUT` | RW | `1000` | hung-owner timeout, µs |
| `0x14` | `DEBOUNCE` | RW | `10000` | PB1 debounce, µs |
| `0x18` | `TUNNEL` | RO | — | synchronised+filtered `{gpio_oe, gpio_o}` snapshot |

All µs fields count a **1 µs tick** = `CLK_HZ/1e6` = 100 `s_axi_aclk` cycles at the
shipped 100 MHz. The bench keeps `CLK_HZ` at its shipped value and programs
**small µs values** from the CSRs (that is what they are for), so a full handover
is a few hundred cycles, not a million.

## The two source models (`kvm_models.py`)

The panel side is watched by `tests/clcd/clcd_panel_model.py`, **reused as-is** (it
watches only the pads, so it is bus-agnostic — it serves `clcd`, this bench,
`tests/nanosoc_lcd` and the Wave-3 end-to-end bench unchanged). What is bench-local
is the two **source** models:

- `HarnessSource` — a faithful stand-in for `clcd.sv`'s 8080 FSM + FIFO, driving
  the `h_*` inputs synchronously. Phase widths (`cs_setup`/`wr_lo`/`wr_hi`) are in
  `s_axi_aclk` cycles and match clcd.sv's TIMING convention (the same one
  `tests/clcd/` proved: WR-low width == `wr_lo`, CS-setup == `cs_setup`).
- `DutTunnelSource` — a student accelerator's display engine seen through the
  tunnel, driven on its **own async clock** (Timer-paced, from a non-aligned start
  phase), honouring the timing floor (≥8 dut_clk cycles per 8080 phase). It can
  inject **per-bit skew** on each tunnel transition — the bench model of exactly
  what the KVM's 2-FF synchronisers do to an async vector, and the only way a
  cocotb bench can tear a vector at all (`.value=` is atomic).

**Both models run FREELY** — they never learn they are being switched — so a
truncation cannot hide. Each records the bytes whose WR rising edge *it* finished;
the panel model records the bytes that actually reached the pads; a mismatch, or
any `cs_low_throughout=False` / `pd_stable=False` / short WR-low width, is a
truncation.

## Behaviour pinned by these benches (27 tests, all confirmed against the RTL)

- **CSR** — reset values (incl. the two reset-1 bits: `timeout_en`, `pb_en`);
  RW hold; the three W1P bits read back 0; byte strobes (`wstrb[0]=0` cannot move
  `[7:0]`); `EVENT` W1C (write-1-clear, write-0-leave, **read does not clear**);
  and `src_sel_we` — a plain read-modify-write of `CTRL` does **not** clobber a
  concurrent `USER_nPB1` press.
- **No read side effects** — every offset in the (width-12) page, swept with
  `EVENT` bits set and the panel armed: zero panel activity, zero `EVENT`/pad/owner
  change, zero panel reset. The width-32 shipped decode + the full 64 KiB sweep is
  in `tests/csr_decode_width/test_decode_width_clcd_kvm.py`.
- **Debounce** — a 6-edge bounce burst produces **exactly one** toggle (odd/even
  argument: no debounce would give an even count → back to start → RED); release
  does nothing; a sub-window glitch is rejected; `pb_en=0` gates the request but
  not the debouncer.
- **PB1 held through reset is not a press** (2026-09-23, D13 boot-hook safety) —
  the PB1 chain resets to *pressed*, so a button held low through `s_axi_aresetn`
  gives no toggle and no `EVENT.pb_toggle`, at a programmed debounce and at the
  shipped 10 ms (plus a second, WDOG-style reset with the button still held); a
  real release + press afterwards still toggles. Control: `make falsify-pb-reset`
  (the old *released* reset values) must turn both tests RED.
- **THE HEADLINE — no truncated cycle across a switch** — harness→DUT and
  DUT→harness, requested mid-burst at **every phase** of the 8080 cycle; the panel
  sees only whole, well-formed cycles with the source's full widths, every queued
  byte arrives in order before the other owner's do, and the panel reset never
  lands inside a cycle. **Both sides of the two-sided gate** (`S_DRAIN` outgoing,
  `S_GRANT` incoming) are tested. `test_force_switch_truncates_on_purpose` is the
  **positive control** proving the detector fires.
- **DFX forced revert** — `decouple_status` rising and `rp_resetn` falling each
  take the pads back within **8 shell cycles**, set `EVENT.forced_revert`, mask
  every DUT source, and let zero DUT bytes reach the panel after; plus the
  by-construction half (a `0x0`-clamped tunnel reaches the panel with nothing and
  reads as quiescent); plus `CTRL.force_harness` as the software twin.
- **Hung-owner timeout** — both the `S_DRAIN` and the `S_GRANT` timeout preempt a
  never-quiescent owner, set `EVENT.timeout_fired`, proceed anyway, and reset the
  panel; `timeout_en=0` disarms it.
- **Panel reset sequencer** — `CTRL.panel_rst_pulse` runs one full
  `S_RST→S_SETTLE→S_GRANT` with **no owner change** and still fires
  `panel_reset_done` (one sequencer, two callers).
- **BL/RST** — the DUT can never drive either (every tunnel bit high does not move
  them); `bl_rst_src=0` passes clcd_0's `CTRL[1]/[2]` through even while the DUT
  owns the bus (the drop-in property); `bl_rst_src=1` selects the KVM's own
  registers; the auto-reset sequencer overrides `RST` in **both** modes.
- **Tunnel CDC** — a DUT clocked at an awkward async 37.037 MHz with per-bit skew
  on every transition crosses without one torn vector; `req` is an **edge** (not a
  level) gated by `dut_req_en` (reset 0); `TUNNEL` snapshots with no side effect.

### The behavioural assumptions, and how each is guarded

1. **The source models stand in for the real masters.** The bench proves the
   *KVM*, not `clcd.sv` and not any accelerator — so the models are deliberately
   generic 8080 drivers, and their fidelity is bounded to what the KVM observes:
   the harness quiescence pair (`h_busy`/`h_fifo_empty`) and the tunnel's
   `busy`/`cs`. **Guard:** the panel-side decode uses the *unmodified*
   `clcd_panel_model.py` that `tests/clcd/` already validated against real
   `clcd.sv`, so "the panel saw a whole cycle" means the same thing here as there.

2. **The truncation detector must be able to fire.** A "no truncated cycle" green
   is worthless if the detector is blind. **Guard, two ways:** (a)
   `test_force_switch_truncates_on_purpose` uses the block's own `CTRL.force_switch`
   (which by contract skips the drain gate) to produce a *real* truncation in-band
   and asserts the detector catches it; (b) `make falsify` / `make falsify-grant`
   remove the outgoing / incoming quiescence gate in a **scratch copy** of the RTL
   (W2-A's file is never touched) and show the truncation / incoming-gate tests go
   **RED** — the mutation-proof (see the "Mutation proofs" section below).

3. **The DUT's quiescence lags the harness's, by design (the CDC).** The harness
   is synchronous, so `harness_quiet` drops within one cycle of it becoming busy;
   the DUT crosses a 2-FF sync + 2-cycle filter, so `dut_quiet` lags ~4-6 cycles.
   A bench that requests a switch *away* from the DUT the instant it enqueues would
   race that latency and read a stale-quiescent tunnel — a **bench** artifact, not
   an RTL bug (on the board the DUT has been painting for milliseconds when you
   press the button). **Guard:** `_wait_tunnel_busy()` polls `STATUS.dut_quiet`
   until the KVM *observably* sees the DUT busy before the switch is requested, so
   the drain gate always has a real non-quiescent tunnel to wait on. This is
   called out explicitly so a future reader does not "simplify" it away.

4. **The panel-reset pulse width has one full tick of jitter.** The µs timers
   count a **free-running** 1 µs tick, so where `S_RST` is entered relative to the
   next tick edge adds up to one tick period (~100 cycles) of jitter to the
   measured `CLCD_RST`-low width. **Guard:** the width assertion tolerates exactly
   one tick (`abs(measured - rst_us*TICK) <= TICK+3`), which still catches a gross
   width error while being honest about the timebase. The DUT-side 8080 width
   checks carry `DUT_TOL=3` aclk cycles for the analogous CDC-sampling
   quantisation — far tighter than any real truncation (which removes a whole
   ~16-cycle phase).

## Mutation proofs (`make falsify`, `make falsify-grant`)

The headline property rests on the **two-sided** quiescence gate, so each target
removes ONE side in a scratch copy and reruns the relevant tests, which MUST fail:

- `make falsify` — `sed` rewrites the `S_DRAIN` gate `cur_owner_quiet || …` to
  `1'b1 || …`, so the outgoing owner is cut off the instant a switch is pending.
  **Result: `test_no_truncated_cycle_{harness_to_dut,dut_to_harness}` both FAIL**
  (the panel sees fewer/short bytes), as they must.
- `make falsify-grant` — rewrites the `S_GRANT` gate `tgt_owner_quiet || …` to
  `1'b1 || …`, so the KVM connects the incoming source while it is still busy.
  **Result: `test_incoming_gate_waits_for_the_{dut,harness}_to_go_quiescent` both
  FAIL** — via the premise check, which correctly observes that the KVM no longer
  sits in `S_GRANT` while the source is busy (it commits immediately). The gate is
  proven load-bearing either way.

The scratch mutant is under `sim_build_mutant/`; W2-A's `clcd_kvm.sv` is never
written. A couple of gate-dependent siblings (the hung-owner-timeout tests) would
also correctly fail under the drain mutation; they are simply not in the `falsify`
`TESTCASE` list. On the pristine RTL the full bench is **25/25 PASS**.

## Contract ambiguities found (reported to the integrator)

1. **README §8's one-line pad-drive formula lists `S_OWN` only** for the current
   owner, which contradicts §6, §7's state table and STATUS[7]'s definition (all
   of which require the outgoing owner to keep driving through `S_DRAIN` — that is
   the entire point of the drain). W2-A implemented the safety-correct reading
   (owner drives in `S_OWN` **and** `S_DRAIN`; `kvm_drives` = FSM ∈
   {`S_RST`,`S_SETTLE`,`S_GRANT`} = STATUS[7]). **This bench is written black-box
   against the panel model**, so the truncated-cycle tests hold regardless of which
   §8 reading anyone took — they were confirmed green against the safety-correct
   RTL. The integrator has flagged §8 for a doc fix.
