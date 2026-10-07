# CLCD KVM — sharing the on-board panel between the harness and the DUT

> **Status: HISTORICAL** — a record of the CLCD KVM feasibility study — sharing the on-board panel between harness and DUT as of 2026-07-15.
> Superseded by / current state in [docs/STATUS.md](STATUS.md).
> Kept for provenance; do not update.
> Cites `CLCD_PHASE_D_INTEGRATION.md` — internal notes, not in the public tree.

> **Status: INVESTIGATION / PLAN. Nothing here is applied.** This is the
> feasibility answer to "build a KVM for the LCD, switched by a user push
> button, and let nanosoc drive the panel as an educational hardware project".
>
> Every claim below was re-checked against the real files this session, because
> the CLCD docs already went stale once (see §0).

---

## 0. First, a correction: the CLCD is already in the shell

`CLCD_PHASE_D_INTEGRATION.md:3-5` still says *"RIDER SPEC (doc-only).
Nothing here is applied to the tree."* **That is out of date.** The rider was
executed on 2026-07-11 in `b4afe3e`:

- `fpga/shell/bd/shell_bd.tcl:494` — `clcd_0` instantiated; `:1041` `NUM_MI 15`;
  `:1108` `assign_bd_address -offset 0x44AC0000 -range 64K`
- `fpga/shell/shell_top.sv:97-103` — the 14 `CLCD_*` pads; `:318-338` the 8 IOBUFs
- `fpga/shell/constraints/mps3_harness.xdc` — 30 CLCD lines (was zero)
- `fpga/dfx/overlay/mps3_shell_static_id.c:18` — `static_id` re-minted
  `0x14E1A2D8` → **`0xE4B1C44A`**; all 8 overlays re-keyed
  (`fpga/dfx/build_clcd/rekey_recover.status`, `gate_verify.log:22`)
- Bank gate green: all 14 pads `LVCMOS18` in bank 66 (`build_clcd/bank_gate.log:72-93`)

`docs/contracts/shell-regmap.md:74` also still says CLCD is "**RESERVED, not
instantiated**". Both docs need correcting. **Resolve state as commits > build
reports > docs.**

**What is *not* yet proven:** the panel has never been lit. The 8080-vs-SPI
strap, colour depth, `BL`/`RST` polarity and data-bus bit order remain
board-only unknowns (`CLCD_PHASE_D_INTEGRATION.md` §9). The KVM is being
designed on top of a panel that is built and sim-proven but electrically
unconfirmed. **Bring the panel up first** (§7) — a KVM for a display that does
not light is untestable.

---

## 1. What the KVM has to be

Not a 2:1 mux. The panel is a *stateful* device on a *strobed* bus behind a
*reconfigurable* partition, and each of those three facts adds a requirement:

1. **Break-before-make at bus granularity.** You cannot cut over mid-cycle.
   Truncating a `WR` strobe writes a garbage byte into GRAM; switching with `CS`
   still asserted leaves the panel selected while the new owner starts a fresh
   sequence, desyncing the HX8347's command/parameter state machine. The mux
   must *latch the request* and *commit only when the current owner's bus is
   quiescent*.
2. **The panel is the framebuffer, and its state is not shared.** The HX8347-D
   holds GRAM, `MADCTL`, the column/row window and the pixel format. Two
   independent drivers each assume they own all of it. So a handover must
   **hard-reset the panel and tell the new owner to re-init and repaint.**
3. **The DUT is in the RP.** During a partial reconfiguration the RP's outputs
   are garbage. A KVM that leaves the DUT selected across a swap sprays random
   strobes at the panel. The KVM must **force-revert to the harness whenever the
   decoupler is engaged or the RP is in reset.** This is the single strongest
   reason the KVM lives in the *static shell* and not anywhere else.

That third requirement is also what makes this a genuinely nice teaching
artifact: it is a real arbitration/safety problem, not a mux.

---

## 2. The routing decision: how does the DUT reach the panel?

The CLCD pads are static and shell-owned. The DUT's 8080 bus needs ~14 signals
to reach the shell. Two ways:

### Option A — dedicated `dut_clcd_*` partition pins (REJECTED for v1)

Clean on paper. Costs, all of them real:

- an edit to `docs/contracts/partition-pins.md` — the **only** declaration point
  `fpga/dfx/pin_check.py` reads (`:73-75`), and it has no concept of an optional
  port: `MISSING` and `EXTRA` are both hard errors (`pin_check.py:164-166`)
- therefore **all 8 RM wrappers** must gain the ports or `make check` stage 2/8
  fails — including RMs that will never drive a display
- therefore all 8 RMs need **OOC re-synthesis**, not just re-implementation
- new `dfx_decoupler` INTFs whose `DECOUPLED_VALUE` must be **`0x1`** on the
  active-low strobes (`cs_n`/`wr_n`/`rd_n`) — the *inverse* of all 15 existing
  entries, which are uniformly `0x0` (`shell_bd.tcl:611-630`). A `0x0` clamp
  would hold the panel selected, in reset, with both strobes asserted, for the
  whole swap. This is the easiest thing in the whole change to get wrong.
- `pin_check.py`'s `width_of()` (`:135-144`) only parses literal `N:0` and the
  one special case `NGPIO-1:0`; a parameterised display bus surfaces as a
  spurious width mismatch.

### Option B — tunnel over the spare `dut_gpio` bits (RECOMMENDED)

**16 DUT→shell wires are already crossing the boundary, already decoupled, and
are physically discarded today.**

- `dut_gpio_o[15:0]` and `dut_gpio_oe[15:0]` both cross the RP boundary
  (`fpga/shell/rp_dut_stub.sv:55-56`) and are both already decoupler members
  (`shell_bd.tcl:626` ID 13 / ID 14, `WIDTH 16`).
- In the shell, **only the low 8 bits of each are used** —
  `shell_top.sv:352-354` drives `USER_nLED` from `board_gpio_pad_o[7:0] &
  board_gpio_pad_oe[7:0]` and never references `[15:8]`. `shell_top.sv:349-350`
  says so outright: the upper half "have no pad to drive and are intentionally
  unconnected."

So map, **post-decoupler** (so the existing clamp protects the KVM too):

| Tunnel bit | Carries |
|---|---|
| `dut_gpio_o[15:8]` | `PD[7:0]` — the 8080 data byte |
| `dut_gpio_oe[15:8]` | `{req, busy, pd_oe, rd, rs, wr, cs}` + 1 spare — **active-HIGH** |

Two properties fall out, and both matter:

- **The LEDs keep working.** Bits `[7:0]` of both vectors are untouched, so
  `board_gpio`'s LED path is unaffected while the DUT drives the display.
- **The existing `DECOUPLED_VALUE 0x0` is already the safe value.** Because the
  strobes are tunnelled *active-high* and inverted to the pad's active-low sense
  inside the KVM, a decoupled RP clamps to "all strobes idle" for free. Option A
  has to hand-set inverted clamps to get the same property; Option B gets it by
  construction. This is the deciding argument.

**Cost of Option B: zero.** No contract edit, no RM wrapper edits, no OOC
re-synthesis, no new decoupler interface, `pin_check` stays green untouched.

**Honest downside:** it overloads a generic bus with a hidden meaning. That is a
documentation burden, not a correctness one — and `partition-pins.md:97-101`
explicitly sanctions the shell muxing/overriding these bits. If the boundary is
ever re-cut for another reason, promote to Option A then.

### What is *not* free either way

The KVM is new **static** RTL, so it re-mints `static_id`
(`0xE4B1C44A` → new) and forces a re-key of all 8 overlays. That is unavoidable
for *any* harness-resident KVM. The good news: that exact sequence was executed
successfully three days ago and is scripted — `fpga/dfx/build_clcd/`
(`bank_gate.tcl`, `finish_partials.tcl`, `rekey_recover.sh`, `gate_verify`), one
overnight run, `pr_verify` 2/2, 8/8 overlays in lockstep.

---

## 3. The KVM block — `fpga/shell/ip/clcd_kvm/clcd_kvm.sv`

Sits between the two sources and the pads. `clcd_0` no longer reaches
`shell_top`'s IOBUFs directly; it feeds the KVM.

```
 MicroBlaze ──AXI-Lite──► clcd_0 ─────────────► A ┐
                          (+ busy, fifo_empty)     │
                                                   ├─► clcd_kvm ─► IOBUFs ─► panel
 nanosoc(RP) ─► dut_gpio_o/oe[15:8] ─► decoupler ─► B ┘      ▲
                                                             │
                        USER_nPB[1] ─► sync ─► debounce ─────┤
                        AXI-Lite CSR @ 0x44AD_0000 ──────────┤
                        decouple_status / rp_resetn ─────────┘  (forced revert)
```

**Copy the ownership-mux shape that already works** — `board_gpio.sv:348-349`
is the same problem solved once already (`own_q` selects host-vs-DUT per bit,
combinational, no added latency, exhaustively bench-proven in
`tests/board_gpio/test_gpio_mux_logic.py`).

**Ownership FSM.** Reset state `OWNER_HARNESS`.
- *Request* from: debounced `USER_nPB[1]` edge (toggle), a CSR write, or the
  DUT's tunnelled `req` bit.
- *Commit* only when the current owner is quiescent — harness: `cs_n==1 &&
  !busy && fifo_empty`; DUT: `!cs && !busy`. A hung owner that never idles hits
  a ~1 ms timeout → force the switch and hard-reset the panel. **A KVM must not
  be able to get stuck on a dead input.**
- *Forced revert* to `OWNER_HARNESS`, immediately and without waiting for
  quiescence, whenever `decouple_status` is asserted or `rp_resetn` is low.
- On every ownership change: pulse `CLCD_RST`, then raise a "you own it now,
  re-init" flag to the new owner. `CLCD_BL` and `CLCD_RST` are owned by the
  **KVM**, never by either source — that is what makes a handover from a
  garbage DUT recoverable.

**`clcd.sv` needs two new output pins** — `busy` and `fifo_empty` are already
internal wires (`clcd.sv:299`, `:359`) and already visible in `STATUS[2:1]`;
they just need bringing out for the KVM's quiescence gate.

**The button.** `USER_nPB0` (AT30) is **taken** — it is the system POR
(`shell_top.sv:117`, `wire sys_rst_n = USER_nPB0;`). **Do not touch it.**
`USER_nPB[1]` (AT32) exists on the board (`fpga/monolithic/nanosoc_mps3.xdc:229`)
and is **unconstrained in the shell today** — that is the KVM button. Sample it
in *hardware* (synchroniser + ~10 ms debounce + edge-detect → toggle), not in
firmware, so the switch still works when the harness firmware is wedged. The CSR
gives the host an override + readback on top.

**CSR page:** `0x44AD_0000` — the next free 64 KiB page after CLCD `0x44AC`.
Decode the block's own page, width 32, per the `csr_decode_width` rule; and
**no read side effects** (`clcd/README.md:127-139` — the platform sweeps every
CSR offset over SWD/XVC).

---

## 4. The educational display driver, inside nanosoc

### Tier 0 — bit-bang (works with *only* the shell KVM; no nanosoc RTL)

`dut_gpio_o/oe` is wired 1:1 to nanosoc's CMSDK **GPIO port 0**
(`rp_nanosoc_wrapper.sv:227-229`, `p0_out_w`/`p0_outen_w`). So the M0 can drive
the tunnel directly from C, with zero new DUT hardware, the day the KVM lands.

Honest about the speed: each 8080 byte costs several AHB GPIO writes, so a
full-screen 320×240×2 = 150 KB repaint is *seconds*. Fine for a text splash —
which is exactly the right first milestone ("press the button, the DUT's
`printf` appears on the panel"), and a legitimate exercise in memory-mapped I/O
and 8080 timing in its own right. It also de-risks the KVM independently of any
student RTL.

### Tier 1 — `ahb_clcd`, a real AHB-Lite → 8080 streaming master (the project)

The block a student writes:

- **AHB-Lite slave front end** — `HSEL`/`HTRANS`/`HREADY`/`HWRITE`/`HADDR`/
  `HWDATA`, with the address-phase/data-phase pipeline. This is the pedagogical
  core: the AHB pipeline is the thing everyone gets wrong first, and it is
  *visible* when you get it wrong.
- **A small FIFO + the 8080 strobe FSM** — `IDLE → SETUP → STROBE_LO →
  STROBE_HI`, timing parameterised in clock cycles.
- **Registers** mirroring the shell block: `CTRL`, `CMD` (push `{RS=0,byte}`),
  `DATA` (push `{RS=1,byte}`), `STATUS` (busy / empty / full / level / owner),
  `TIMING`.
- **A C driver + font renderer** on the M0.

Why this is a *good* teaching block specifically: the HX8347-D has on-chip GRAM,
so **the panel is the framebuffer**. No BRAM framebuffer, no video timing, no
DMA needed to get something on screen. A student can build it in a lab session
and *see it on a real screen* — a rare and motivating payoff for an RTL exercise.
~300–600 LUT.

**Reuse, don't re-derive:** the 8080 FSM in `clcd.sv:343-497` is already
bus-agnostic and bench-proven. Factor it into `clcd_core.sv` (FIFO + FSM, no
bus) plus two thin front ends — `clcd.sv` (AXI4-Lite, shell) and `ahb_clcd.sv`
(AHB-Lite, DUT). The DUT block then inherits the shell block's proof and its
cocotb panel model (`tests/clcd/clcd_panel_model.py`). Set the student exercise
at whichever layer you want: give them the core and have them write the AHB
slave, or have them write the FSM too.

Extension ladder: RGB565 pixel packer → hardware rectangle-fill → an AHB
*master*/DMA that blits a framebuffer from SRAM without per-pixel CPU writes.

### Where the block goes — *not* the expansion region

The obvious home looks like nanosoc's **expansion region**: `exp` @
`0x6000_0000`, 256 MB, reachable by the CPU *and* both DMA controllers, with
`EXP_IRQ[3:0]` pre-wired to `cpu_0_irq[14:11]` (firmware already names these
`EXP0_IRQn`…`EXP3_IRQn`) and `EXP_DRQ[1:0]` into `dmac_0`. The library YAML
literally says *"REPLACE THIS MODULE with your own accelerator IP"*
(`sys_desc/regions/exp_default/nanosoc_region_exp_default.yaml:12`). It is the
socket SoC Labs designed for exactly this.

**Three things say don't use it for v1:**

1. **`exp` has no pad-facing I/O whatsoever.** Its port list
   (`rtl/src/regions/exp/nanosoc_region_exp.v:11-76`) is AHB + AXI-Stream + IRQ
   + DRQ — *no pins*. An accelerator there can compute, be DMA'd into, and raise
   interrupts, but it **cannot drive 14 wires off the chip**. A display
   controller in `exp` still needs new pin ports plumbed
   `region_exp → nanosoc_system → nanosoc → RM wrapper`.
2. **`nanosoc_system` is never instantiated on the FPGA path.** The RM wrapper
   instantiates `nanosoc` *directly* (`rp_nanosoc_wrapper.sv:327-329`), so
   `nanosoc_region_exp` does not exist in this build at all and the whole `exp_*`
   bus is tied off inert (`:371-425`).
3. **The generated `exp_*` port directions are broken** — already logged as
   **G3** in `docs/NANOSOC_INTEGRATION_GAPS.md:136`, and confirmed in
   `docs/nanosoc_m0_soc/RTL_ANATOMY.md:132-156`: `nanosoc.exp_hsel` is `input`
   and the default slave's `HSEL` is *also* `input` — neither end drives it.
   Simulators pass it (undriven reads as X); synthesis will not.

**Use a free AHB-Lite slot in `soc_peripheral` instead.** The slave mux has
**five** disabled ports — slots 5/6 are `PORT5_ENABLE(0)`/`PORT6_ENABLE(0)` and
`HSEL7/8/9` are tied `1'b0`
(`rtl/src/regions/soc_peripheral/nanosoc_region_soc_peripheral.v:189-193`,
`:227-238`). Taking slot 5 is a **three-line decode change** — instantiate the
block, flip `PORT5_ENABLE` to 1, hook `HSEL5` — in a **hand-written `gen: False`
file the generator does not overwrite**, so it needs no YAML round-trip and
trips none of G3. Base lands at `0x4001_2000` (`CMSDK_AHB_BASE + 0x2000`).

That keeps the student's first block on plain AHB-Lite with no bridge, no
generator, and no upstream bug in the way. (For reference, the APB mux also has
free slots — 3/7/9/10/12/15 — but APB means going through `cmsdk_ahb_to_apb`,
which is both slower and less instructive.)

**Keep `exp` as the extension path, not the starting point.** Once G3 is fixed
(a local override in `src/rtl/local_overrides/` — **never** edit the read-only
upstream), `exp` is where the *interesting* version lives: DMA a framebuffer
straight into the LCD block over `EXP_STR_IN_0` with no per-pixel CPU writes, and
raise "FIFO not full" on a pre-wired `EXP_IRQ`. That is the natural Tier-2
project, and fixing G3 unblocks **every future SoC Labs student accelerator**,
not just this one. It is genuinely separate work from the KVM and should be
scoped as such rather than smuggled in.

The RM-side wiring is one mux wherever the block lives, and it is
**RM-internal** — no boundary change, so only `rm_nanosoc` rebuilds:

```systemverilog
assign dut_gpio_o  = { lcd_en ? lcd_pd  : p0_out_w[15:8],   p0_out_w[7:0]   };
assign dut_gpio_oe = { lcd_en ? lcd_ctl : p0_outen_w[15:8], p0_outen_w[7:0] };
```

---

## 5. Verification (all sim-provable before any board time)

- `tests/clcd_kvm/` — cocotb, reusing `tests/clcd/clcd_panel_model.py`:
  - **no truncated bus cycle across a switch** — request a switch mid-burst from
    each side; assert the panel model sees only whole, well-formed 8080 cycles
    and that `CS` was never left asserted across the handover. *This is the
    headline property.*
  - **forced revert** — assert `decouple_status` while the DUT owns the panel;
    assert ownership returns to the harness within N cycles and that zero DUT
    strobes reach the panel afterwards.
  - **hung-owner timeout** — an owner that never idles is preempted, and the
    panel is reset.
  - **debounce** — a bouncing button edge produces exactly one toggle.
  - `tests/csr_decode_width/` — a `clcd_kvm` entry is a required deliverable.
- Mutation-prove the safe-switch gate (remove the quiescence condition → the
  truncated-cycle test must fail while its siblings pass). The CLCD bench already
  sets this precedent for its no-read-side-effect guard.
- `make check` (8 stages) green, `pin_check` untouched-and-green (Option B).

**`tests/clcd/clcd_panel_model.py` is reusable as-is** — it is a cocotb monitor
that plays the HX8347-D on the 8080 bus, latching `{RS,byte}` on each `WR_n`
rising edge while `CS_n` is asserted. It watches only the *panel pads*, so it is
bus-agnostic and serves the KVM bench and the DUT-side `ahb_clcd` twin unchanged.
The five-test structure of `tests/clcd/test_clcd.py`, the `bind_*.sv` SVA-checker
pattern, `bench_common.mk` and the `dut_notes.md` convention all transfer too.

**The one missing piece of infrastructure:** `tests/common/regmap.py` provides
only an `AxiLiteMaster`. There is **no AHB-Lite BFM in the tree**, so
`tests/nanosoc_lcd/` needs an `AhbLiteMaster` written into `tests/common/` first.
Worth knowing before promising a student a working bench — and it is a reusable
asset for every future DUT-side block, so it should be built properly rather than
inlined into one test.

---

## 6. Cost summary

| Item | Cost |
|---|---|
| `clcd_kvm.sv` + CSR + bench | new static RTL, ~200–400 LUT (ESTIMATE) |
| `clcd.sv` +2 output pins (`busy`, `fifo_empty`) | trivial |
| `USER_nPB[1]` (AT32) pad + XDC | 2 lines; bank check per `build_clcd/bank_gate.tcl` |
| RP boundary / `partition-pins.md` / 8 RM wrappers | **unchanged** (Option B) |
| `dfx_decoupler` | **unchanged** (Option B) |
| Shell rebuild + `static_id` re-mint + 8-overlay re-key | **unavoidable**; scripted + proven (`build_clcd/`) |
| `ahb_clcd` @ free AHB slot 5 (`0x4001_2000`) | 3-line decode change, hand-written `gen: False` file |
| nanosoc RM wrapper GPIO mux | RM-internal; only `rm_nanosoc` rebuilds |
| `AhbLiteMaster` BFM in `tests/common/` | **missing today** — prerequisite for any DUT-side bench |
| nanosoc `exp_*` fix (G3) + DMA/IRQ version | separate work item; local override only; Tier 2 |

---

## 7. Recommended order

1. ~~**Light the panel.**~~ **DONE — the panel is lit and working (2026-07-14).**
   The board-only straps of §0 are therefore closed *in practice*; capture them
   in writing (wave-plan W0-C) so the KVM bench and the DUT driver model the
   real panel rather than the assumed one.
2. **KVM in sim** — `clcd_kvm.sv` + `tests/clcd_kvm/`, Option-B tunnel. Prove
   the truncated-cycle, forced-revert and timeout properties before any rebuild.
3. **Tier-0 bit-bang** in the nanosoc RM — proves the whole KVM path end to end
   with no student RTL in the loop.
4. **Batch the static rebuild** — KVM + PB1 + `clcd.sv` pin-out, one shell build,
   one re-key of all 8 overlays. Ride it with anything else Phase-D wants.
5. **`AhbLiteMaster` BFM** into `tests/common/` — the prerequisite nobody has
   built yet, and a reusable asset for every future DUT-side block.
6. **`ahb_clcd`** as the student project, on AHB slot 5, on top of a KVM that is
   already proven and a bench harness that already exists.
7. Optionally, **Tier 2**: fix G3 and move to the `exp` socket for the DMA +
   interrupt version — its own scoped work item.

## 8. Open questions for the integrator

- Option B's bus overload — acceptable, or hold out for real partition pins?
- Does the DUT get `BL`/`RST` at all? (Recommendation: **no** — the KVM keeps
  them, so a hung DUT is always recoverable.)
- Touch (`CLCD_TSCL`/`TSDA`/`TINT`) is still reserved and unconstrained. A v2
  KVM that hands the DUT a *pointing device* alongside the display is the
  natural "M" of the KVM — worth reserving the tunnel bit for it now.
