# DUT notes — `nanosoc_lcd` (the DUT-side display accelerator)

RTL under test: `fpga/rp/nanosoc_exp/` — the module `nanosoc_exp_socket` (the
FROZEN socket, `TOPLEVEL`) wrapping `ahb_clcd` (the reference accelerator) over
`clcd_core` (the FIFO + 8080 strobe FSM, lifted from the shell's `clcd.sv`).
Owner: **W2-D**. Contract this bench is written against — NOT the RTL:
`fpga/rp/nanosoc_exp/README.md` **v1.0 (FROZEN)**, plus
`docs/contracts/dut-display-tunnel.md` and `docs/CLCD_PANEL_FACTS.md`.

This is the DUT-side sibling of `tests/clcd/` (the shell's AXI4-Lite CLCD). The
two blocks share `clcd_core`, so this bench is what shows the DUT-side block
**inherits the shell block's proof** across a different bus front end. Its
five-test skeleton, `_timed_burst` trick, `_wait_drained` poll and synthetic-
vector policy are all copied from `tests/clcd/test_clcd.py`; read that first.

`list_benches.py` reports `nanosoc_lcd` **READY** once all three RTL files land
(no stub marker); until then it **SKIPs** — as does the bench itself if either
the RTL **or** W2-B's `AhbLiteMaster` BFM is absent (`dut_presence.rtl_ready` +
an `ImportError` guard; see `_SKIP_WHY`).

## Port list (socket README §2 — FROZEN; the bench binds these names)

- **Clock/reset:** `hclk` (50 MHz shipped), `hresetn` (active-low).
- **AHB-Lite SLAVE:** `hsel, haddr, htrans, hwrite, hsize, hburst, hprot,
  hmastlock, hwdata, hready` (in) / `hrdata, hreadyout, hresp` (out). Base
  `0x6000_0000`, 256 MB. `hready` is the GLOBAL bus ready (an input);
  `hreadyout` is this slave's own ready (the load-bearing output — see §4).
- **Display pins out (→ tunnel → shell KVM → panel), ALL ACTIVE-HIGH:**
  `lcd_en` (1 = this block drives the pins), `lcd_pd[7:0]`, `lcd_cs`, `lcd_wr`,
  `lcd_rs` (0=CMD/1=DATA), `lcd_busy`, `lcd_req`. **No `lcd_rd`, no `lcd_pd_oe`
  — the panel is WRITE-ONLY in this platform.**
- **Hooks:** `irq[3:0]` (→ M0 NVIC EXP0..3 = IRQ 11..14), `drq[1:0]` (→ DMAC 0).

## Register map (socket README §7, base `0x6000_0000`)

| Off | Reg | Access | Bits |
|---|---|---|---|
| `0x00` | `CTRL` | RW | `[0]` enable, `[1]` fifo_reset (self-clearing), `[2]` req (→ `lcd_req`) |
| `0x04` | `CMD` | W | `[7:0]` → push `{RS=0, byte}` |
| `0x08` | `DATA` | W | `[7:0]` → push `{RS=1, byte}` |
| `0x0C` | `STATUS` | RO | `[0]` fifo_full, `[1]` fifo_empty, `[2]` busy, `[15:8]` fifo_level |
| `0x10` | `TIMING` | RW | `[7:0]` wr_lo, `[15:8]` wr_hi, `[23:16]` cs_setup — in **`hclk`** cycles |

Differences from the shell block's map (`shell-regmap.md` CLCD @ `0x44AC`) a
reader coming from `tests/clcd/` must note:
- **`TIMING` is at `0x10` here, not `0x14`** — there is **no `READ` register**
  (`0x10` in the shell block) because the panel is write-only. So there is also
  **no `READ_PATH=1` secondary elaboration** (`tests/clcd`'s `make READP=1`).
- **`CTRL` has no `backlight`/`reset_n` bits.** The KVM owns `BL`/`RST`
  (`dut-display-tunnel.md` §1), so this block never touches them. `fifo_reset`
  moves from `CTRL[3]` to `CTRL[1]`, and `req` (a new bit, driving `lcd_req`)
  takes `CTRL[2]`.

## The three things that make this bench different from `tests/clcd`

### 1. POLARITY — the shim (`panel_shim.py`)
The socket's display pins are **ACTIVE-HIGH** (`lcd_cs`/`lcd_wr`: 1 = asserted);
the panel model expects the panel's native **ACTIVE-LOW** pads. `panel_shim.
PanelPads` is a duck-typed stand-in for a `clcd`-shaped DUT handle that inverts
`lcd_cs`→`clcd_cs_n_o` and `lcd_wr`→`clcd_wr_n_o`, passes `lcd_rs`/`lcd_pd`
straight through, and presents a constant-deasserted `clcd_rd_n_o` (no read
path). **The inversion lives ONLY here** — it must NOT be done in the RTL
(README §6: the tunnel's decoupler clamps these wires to `0`, and active-high
means "0 ⇒ all strobes idle" by construction; inverting in RTL turns the safe
clamp into a panel-corrupting "chip selected, both strobes asserted" during
every partial reconfiguration). The shim stands exactly where the shell's KVM
stands on the board. `tests/clcd/clcd_panel_model.py` is reused **AS-IS** — it
watches only pads, so it is bus-agnostic, and it is `tests/clcd`'s file (read-
only to this bench).

### 2. THE BUS — AHB-Lite, and `hreadyout` is load-bearing for the whole SoC
Unlike AXI4-Lite, AHB-Lite is a two-phase pipeline and the slave's `hreadyout`
gates the entire bus matrix. `exp_*` is a real hole in nanosoc's matrix: a slave
that holds `hreadyout` low stalls the AHB, stalls the Cortex-M0, and the board
STOPS with no exception and nothing on screen. Test 5 and test 7 exist for that.

### 3. NO PANEL READ
No `lcd_rd`, no `lcd_pd_oe`, no `READ` register (README §2). There is no read
side of the panel to model; the shim hard-wires `clcd_rd_n_o` deasserted so the
panel model's `.idle` means exactly "the DUT drove nothing at the panel".

## The seven checks (`test_nanosoc_lcd.py`)

1. **Streaming fidelity** — a synthetic mixed CMD/DATA vector via `CMD`/`DATA`
   decodes off the pads in exact order, right RS per byte, CS framed, PD stable.
2. **RS select + PD stability** through the WR edge (the latch instant), plus a
   check that **CS deasserts between bytes** (the KVM's safe-switch hook).
3. **TIMING honoured** in `hclk` cycles — absolute widths AND field-isolated
   deltas at four values, plus the §6 timing floor (8/8/8 ⇒ ≥24 cyc/byte).
4. **CTRL → pads** — reset values (incl. `hreadyout` resets to **1**), `lcd_en`,
   `CTRL.req`→`lcd_req`, self-clearing `fifo_reset`, and **`lcd_busy` =
   `(fsm!=IDLE) || !fifo_empty`** (strictly wider than the shell's `STATUS.busy`
   — see the assumption below).
5. **FIFO backpressure — THE CRITICAL ONE.** Fill to `fifo_full`, then prove
   writes-while-full are **DROPPED** (complete OKAY, `fifo_level` unchanged) and
   the **AHB bus never stalls** (`hreadyout` never low). Three independent
   guards: a `with_timeout` that turns a stall into a named failure instead of a
   hang; the `ReadyMonitor` (zero low cycles); and the bound SVA `A_LIVENESS`.
   Then zero-loss once the FIFO drains. **Mutation-proven** (below).
6. **No read side effects** — read every register (+ unmapped aliases) with the
   FSM LIVE; the panel bus must not move. Non-vacuity: a byte is pushed first to
   prove the monitor can see this DUT, so a clean sweep means "nothing happened".
7. **AHB-Lite legality** — genuinely PIPELINED back-to-back transfers via the
   BFM's `init_write`+`pipeline` (asserting `overlapped_cycles` actually grew),
   proving order/content survive the overlap; plus the bound protocol checker
   (`bind_nanosoc_lcd.sv`) watching every cycle of every test.

### The one behavioural assumption, and how it's guarded
Same as `tests/clcd`: the 8080 pads carry **no** panel→master backpressure, so
"panel-side stall" is modelled by **parking the FSM** (`CTRL=0`, `enable=0`).
Tests 4/5/7 fill the FIFO parked, relying on *parked ⇒ no drain*. That is
**asserted directly** in test 4 (`len(model.strobes) == 0` after a parked fill),
so if a future RTL change ever drained while parked, the failure is explicit and
points here, not a mysterious count mismatch.

A second, socket-specific assumption: **`lcd_busy` must include a non-empty FIFO
even when the FSM is idle** (`(fsm!=IDLE) || !fifo_empty`, README §6). This is
strictly wider than the shell block's `STATUS.busy` (`state != IDLE`); a block
that reported only fsm-busy would look quiescent to the KVM while it still had a
queue to drain, and the handover would cut its byte stream in half. Test 4
asserts `lcd_busy==1` with the FSM idle but the FIFO non-empty. `_wait_drained`
uses `fifo_empty && !busy`, which is correct under BOTH readings, so the poll
helper does not depend on which convention `STATUS.busy` uses.

## Mutation proof (of the "never stalls AHB" guard, test 5)

Done in a **scratch copy of the RTL outside the repo** (W2-D's files were never
touched), re-pointed with `make RTL_DIR=<scratch>`. The mutation is exactly the
trap README §4 names — `hreadyout` back-pressures a full FIFO
(`assign hreadyout = !(write_to_CMD/DATA && fifo_full)`) instead of dropping:

- **Fatal SVA run** (`make RTL_DIR=<mut>`): the bound `A_LIVENESS` `$fatal`s at
  test 5 — *"hreadyout LOW for 18 consecutive cycles (> MAX_WAIT=16) — the AHB
  bus is STALLED"* — tests 1–4 having already passed.
- **Non-fatal run** (`make RTL_DIR=<mut> SIM_ARGS=+SVA_NOFATAL`, so the sim
  survives to run every test): **test 5 FAILS**, caught by the Python
  `with_timeout` guard — *"write to a FULL FIFO NEVER COMPLETED … THE DEADLOCK
  README §4 FORBIDS"* — while **all six siblings PASS**. `PASS=6 FAIL=1`.

Restoring `hreadyout = 1'b1` returns the bench to `PASS=7`. A guard that cannot
fail is not a guard; this one fails on exactly the design mistake it forbids.

## Reconciliation notes (sibling deliverables that landed / had not)

- **W2-B `tests/common/ahb_lite.py` (`AhbLiteMaster`) — LANDED**, and this bench
  uses it (not a private BFM). Two facts a reader/integrator needs:
  1. **`from_dut` has a cocotb-2.x bug** that fires for exactly this DUT: its
     fallback `hready = get("hready") or get("hreadyout", required=True)`
     evaluates a cocotb handle in a boolean context (forbidden in 2.x), and the
     frozen socket **has** an `hready` port, so `get("hready")` returns a handle
     and it raises `TypeError`. The bench sidesteps it by passing `hready=`
     **explicitly** (`from_dut(dut, hready=dut.hreadyout,
     hready_drive=dut.hready)`), which is popped before the buggy line runs. If
     W2-B fixes `from_dut`, this call still works unchanged. **Flagged to the
     integrator — this is a W2-B fix, not something this bench should own.**
  2. **HREADY loop:** single-slave AHB-Lite means `hready == hreadyout`. The BFM
     closes that loop itself via its `hready_mirror` when given
     `hready_drive=dut.hready`; the bench uses that rather than a hand-rolled
     feedback coroutine. Test 7 uses the BFM's `init_write`/`pipeline` (not raw
     signal driving) because the BFM runs a continuous background bus driver and
     a second hand-driver on the same handles would collide.
- **W2-D `fpga/rp/nanosoc_exp/*.sv` — NOT PRESENT when this bench was written.**
  The bench was written against the frozen README (exactly as `tests/clcd/` was
  written before its RTL landed) and RUN against a throwaway reference RTL kept
  **outside the repo** (a scratch `clcd_core`+`ahb_clcd`+`nanosoc_exp_socket`
  modelling README §7 literally). **7/7 PASS** against that scratch RTL + W2-B's
  real BFM + the bound SVA. When W2-D's real RTL lands, `list_benches.py` flips
  `nanosoc_lcd` to READY and the bench runs unchanged; if W2-D's register bit
  positions, the `busy` definition, or the drop-not-stall policy differ from the
  README this bench encodes, the mismatch surfaces as a specific failing check,
  which is the point.

## Files (this bench owns all of these)

- `Makefile` — `TOPLEVEL=nanosoc_exp_socket`, `VERILOG_SOURCES` = glob of
  `$(RTL_DIR)/*.sv` (so a student's replacement accelerator compiles with no
  Makefile edit); `RTL_DIR` override drives the mutation flow; exports
  `NANOSOC_LCD_RTL_DIR` so the Python skip-gate follows the override.
- `test_nanosoc_lcd.py` — the seven checks.
- `panel_shim.py` — the active-high → active-low adapter (see §1 above).
- `ahb_lite_protocol_checker.sv` + `bind_nanosoc_lcd.sv` — the bound AHB-Lite
  protocol SVA (candidate to promote to `tests/common/sva/` once a second
  AHB-Lite slave bench exists; nothing in it is `ahb_clcd`-specific).
- `dut_notes.md` — this file.

Reuses (does NOT own): `tests/clcd/clcd_panel_model.py` (verbatim),
`tests/common/ahb_lite.py` (W2-B), `tests/common/dut_presence.py`,
`tests/common/bench_common.mk`.

## Open items a student (or the integrator) will also hit

1. **RGB-vs-BGR is unresolved** (`CLCD_PANEL_FACTS.md` §; W0-C's open item). This
   bench is byte-exact and panel-agnostic, so it cannot and does not settle it —
   one red fill on the board will. Do not read a green bench here as "colours are
   right".
2. **`hsize` is not exercised as byte/halfword.** The README (§3) says a
   word-only block may ignore `hsize`; the reference block does, and this bench
   drives word writes (the BFM defaults to `HSIZE_WORD`). If a student's block
   claims per-lane behaviour, that needs its own check (mirror
   `tests/*/test_wstrb_*`), which is out of scope for the reference bench.
3. **The §6 timing FLOOR (≥8 hclk/phase) is the DRIVER's responsibility**, not
   the block's — the block must honour whatever TIMING it is given, including
   values below the floor (test 3 sweeps them). Firmware programming <8 would
   break the tunnel CDC; that belongs to the W2-F driver / a Wave-3 integration
   bench, not here.
