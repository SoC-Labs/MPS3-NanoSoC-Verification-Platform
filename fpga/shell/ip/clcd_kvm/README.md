# `clcd_kvm` — the CLCD KVM: panel arbiter between the harness and the DUT

> ## STATUS: **FROZEN CONTRACT (v1.0, 2026-07-14, W1)**
>
> This document is the *single* source of truth for Wave 2. `clcd_kvm.sv` (W2-A),
> `tests/clcd_kvm/` (W2-C) and `firmware/clcd/` (W2-F) are written **in parallel,
> against this file, without talking to each other** — exactly how the `clcd`
> block itself was built. If this document is ambiguous, they diverge and the
> wave fails.
>
> **Nothing in this directory is wired into the shell yet.** The BD change
> (`shell_bd.tcl` slave @ `0x44AD_0000`, `NUM_MI 15→16`), the `USER_nPB1` pad and
> the `clcd.sv` pin-out all land together in the **Wave-4 static rebuild**, which
> re-mints `static_id` (`0xE4B1C44A` → new) and re-keys all 8 overlays.
>
> Related: `docs/CLCD_KVM_PLAN.md` (why), `docs/CLCD_KVM_WAVE_PLAN.md` (how/when),
> `docs/CLCD_PANEL_FACTS.md` (the **proven** panel), `docs/contracts/dut-display-tunnel.md`
> (the DUT-side wire encoding), `docs/contracts/shell-regmap.md` §CLCDKVM (the
> register contract), `fpga/rp/nanosoc_exp/README.md` (the student socket).

---

## 1. What this block is

A **KVM for the on-board QVGA panel**: it arbitrates the single physical HX8347-D
8080 bus between two independent bus masters —

| Source | Who | Reaches the KVM via |
|---|---|---|
| **A — harness** | `clcd_0` (`fpga/shell/ip/clcd/clcd.sv`, AXI-Lite @ `0x44AC_0000`, driven by the MicroBlaze) | direct, same clock domain (`s_axi_aclk`) |
| **B — DUT** | a student accelerator inside the nanosoc RM | the **tunnel** — `dut_gpio_o[15:8]` / `dut_gpio_oe[15:8]`, *post*-decoupler, `dut_clk` domain (**async**) |

— and drives the panel pads. It is **not a 2:1 mux**. Three facts force real logic:

1. **The bus is strobed.** Cutting over mid-cycle truncates a `WR` pulse (a garbage
   byte into GRAM) or leaves `CS` asserted across the seam (desyncing the HX8347's
   command/parameter state machine). ⇒ **safe-switch gate** (§6).
2. **The panel is the framebuffer, and its state is not shared.** GRAM, `MADCTL`,
   the column/row window and the pixel format all belong to whoever last
   initialised the panel. ⇒ every handover **hard-resets the panel** and tells the
   new owner to re-init and repaint (§7, §8).
3. **The DUT is in a reconfigurable partition.** During a partial reconfiguration
   the RP's outputs are garbage. ⇒ **forced revert to the harness** whenever
   `decouple_status` is asserted or `rp_resetn` is low (§9).

The KVM also **owns `CLCD_BL` and `CLCD_RST`** in the sense that matters: the DUT
can never touch them (there are no `BL`/`RST` bits in the tunnel, by design), and
the KVM's hardware panel-reset sequencer overrides `CLCD_RST` unconditionally
regardless of any register. That is what makes a hung DUT always recoverable. See
§10 for the one deliberate refinement (a back-compat source-select on the
*steady-state* value of those two pads).

---

## 2. Port contract (FROZEN — W2-A binds to this verbatim)

```systemverilog
module clcd_kvm #(
  // Local offset decode only. The base (0x44AD_0000, 64 KiB page) is set in the
  // BD Address Editor. shell_bd.tcl instantiates every CSR block with
  // C_S_AXI_ADDR_WIDTH = 32 -- the RTL default of 12 is NEVER what ships.
  // Decode the block's own 64 KiB page (see §4).
  parameter int C_S_AXI_ADDR_WIDTH = 12,
  parameter int C_S_AXI_DATA_WIDTH = 32,

  // s_axi_aclk frequency, in Hz. The block derives a 1 us tick from this
  // (TICK_DIV = CLK_HZ / 1_000_000); every time in this block is expressed in
  // MICROSECONDS, in both the RTL and the CSRs. The shipped shell clock is
  // clk_wiz_shell CLKOUT1 = 100 MHz (shell_bd.tcl:199,:258) => TICK_DIV = 100.
  // A bench may lower CLK_HZ to shorten the tick; it must not change the units.
  parameter int CLK_HZ = 100_000_000,

  // Async-input synchroniser depth for the USER_nPB1 pad. 3 FFs (see §11).
  parameter int PB_SYNC_STAGES = 3,

  // CSR reset values, in MICROSECONDS (see §5 for the register fields and §6/§7
  // for the justification of each default).
  parameter int RST_US_INIT      = 2000,   // PANEL_TMR.rst_us     -- CLCD_RST low
  parameter int SETTLE_US_INIT   = 5000,   // PANEL_TMR.settle_us  -- post-reset idle
  parameter int TIMEOUT_US_INIT  = 1000,   // TIMEOUT.timeout_us   -- hung-owner
  parameter int DEBOUNCE_US_INIT = 10000   // DEBOUNCE.debounce_us -- PB1
) (
  input  logic                            s_axi_aclk,     // shell clock, 100 MHz
  input  logic                            s_axi_aresetn,  // shell reset, active-low

  // ==== AXI4-Lite slave -- MicroBlaze data bus @ 0x44AD_0000 =================
  // Xilinx-template ready-pulse FSM, identical in structure to clcd/dfx_ctl/telem
  // (shell-regmap.md "AXI4-Lite slave conventions", style 1). Never stalls.
  input  logic [C_S_AXI_ADDR_WIDTH-1:0]   s_axi_awaddr,
  input  logic [2:0]                      s_axi_awprot,
  input  logic                            s_axi_awvalid,
  output logic                            s_axi_awready,
  input  logic [C_S_AXI_DATA_WIDTH-1:0]   s_axi_wdata,
  input  logic [C_S_AXI_DATA_WIDTH/8-1:0] s_axi_wstrb,
  input  logic                            s_axi_wvalid,
  output logic                            s_axi_wready,
  output logic [1:0]                      s_axi_bresp,
  output logic                            s_axi_bvalid,
  input  logic                            s_axi_bready,
  input  logic [C_S_AXI_ADDR_WIDTH-1:0]   s_axi_araddr,
  input  logic [2:0]                      s_axi_arprot,
  input  logic                            s_axi_arvalid,
  output logic                            s_axi_arready,
  output logic [C_S_AXI_DATA_WIDTH-1:0]   s_axi_rdata,
  output logic [1:0]                      s_axi_rresp,
  output logic                            s_axi_rvalid,
  input  logic                            s_axi_rready,

  // ==== SOURCE A -- the harness CLCD block (clcd_0) ==========================
  // SAME CLOCK DOMAIN (s_axi_aclk). NO synchroniser. These are clcd_0's existing
  // pad outputs, re-routed: clcd_0 no longer reaches shell_top's IOBUFs.
  input  logic [7:0]                      h_pd_o,        // <- clcd_0.clcd_pd_o
  input  logic                            h_pd_oe,       // <- clcd_0.clcd_pd_oe   (const 1 at READ_PATH=0)
  input  logic                            h_cs_n,        // <- clcd_0.clcd_cs_n_o  (ACTIVE-LOW)
  input  logic                            h_wr_n,        // <- clcd_0.clcd_wr_n_o  (ACTIVE-LOW)
  input  logic                            h_rd_n,        // <- clcd_0.clcd_rd_n_o  (ACTIVE-LOW; const 1 at READ_PATH=0)
  input  logic                            h_rs,          // <- clcd_0.clcd_rs_o    (0=cmd, 1=data)
  input  logic                            h_bl,          // <- clcd_0.clcd_bl_o    (ACTIVE-HIGH; see §10)
  input  logic                            h_rst_n,       // <- clcd_0.clcd_rst_n_o (ACTIVE-LOW;  see §10)
  // --- the TWO NEW clcd.sv OUTPUTS (W2-A adds them; see §3) ---
  input  logic                            h_busy,        // <- clcd_0.busy_o       (1 = 8080 FSM != ST_IDLE)
  input  logic                            h_fifo_empty,  // <- clcd_0.fifo_empty_o (1 = {RS,byte} FIFO empty)
  // --- read-back forwarding (dead at READ_PATH=0; keeps a future READ_PATH=1
  //     a KVM-internal change, not a port change) ---
  output logic [7:0]                      h_pd_i,        // -> clcd_0.clcd_pd_i

  // ==== SOURCE B -- the DUT tunnel, POST-DECOUPLER ===========================
  // ASYNC: these come from the RP in the dut_clk domain (50 MHz shipped). The
  // KVM synchronises and stability-filters them internally (§12). Encoding is
  // FROZEN in docs/contracts/dut-display-tunnel.md -- do not restate it in RTL
  // comments without citing that file.
  // TAP, DO NOT STEAL: board_gpio_0 keeps its own connection to these same nets;
  // bits [7:0] still drive the LEDs.
  input  logic [15:0]                     dut_gpio_o_i,  // [15:8] = PD[7:0]
  input  logic [15:0]                     dut_gpio_oe_i, // [15:8] = control/status

  // ==== DFX interlock ========================================================
  // Both are shell-domain nets, but they are synchronised anyway (2 FF) so the
  // block is safe to instantiate from any clock and the bench can wiggle them.
  input  logic                            decouple_status, // 1 = RP boundary CLAMPED
                                                           //   <- dfx_decoupler_0/decouple_status
  input  logic                            rp_resetn,       // 0 = RP HELD IN RESET
                                                           //   <- dut_clkrst_0/rp_resetn_o

  // ==== The button -- RAW ASYNC PAD, ACTIVE-LOW ==============================
  // Straight from the shell_top port USER_nPB1 (MPS3 pin AT32). Pressed = 0.
  // NOT debounced upstream; NOT synchronised upstream. The KVM does both, in
  // HARDWARE, so the switch still works when the harness firmware is wedged.
  input  logic                            user_npb1,

  // ==== Panel pads -- to shell_top's IOBUFs ==================================
  // Identical shape to clcd.sv's pad group, so shell_top's existing IOBUF block
  // (:318-338) re-binds to these with no structural change.
  output logic [7:0]                      clcd_pd_o,
  input  logic [7:0]                      clcd_pd_i,     // from the IOBUF .O legs
  output logic                            clcd_pd_oe,    // 1 = drive pads (held 1 in v1)
  output logic                            clcd_cs_n_o,   // pad CLCD_CS      (AP15, ACTIVE-LOW)
  output logic                            clcd_wr_n_o,   // pad CLCD_WR_SCL  (AP14, ACTIVE-LOW)
  output logic                            clcd_rd_n_o,   // pad CLCD_RD      (AM15, ACTIVE-LOW)
  output logic                            clcd_rs_o,     // pad CLCD_RS      (AN14, 0=cmd 1=data)
  output logic                            clcd_bl_o,     // pad CLCD_BL      (AJ16, ACTIVE-HIGH)
  output logic                            clcd_rst_n_o,  // pad CLCD_RST     (AK18, ACTIVE-LOW)

  // ==== Status tap ===========================================================
  output logic                            owner_o        // 0 = HARNESS, 1 = DUT.
                                                         // Leave OPEN in the BD if
                                                         // unused; it exists for the
                                                         // bench and a future telem tap.
);
```

**Nothing else.** No IRQ output (firmware polls `STATUS`/`EVENT`; an interrupt to
the MicroBlaze would need an `axi_intc` port and buys nothing at a 250 ms
refresh). No touch pins (`docs/CLCD_PANEL_FACTS.md` §7.6: touch does not exist).
No panel read-back path of its own (`READ_PATH=0` shipped; §7.2 of the same doc:
**assume read-back does not exist**).

---

## 3. The two new `clcd.sv` output pins (W2-A also owns this)

`busy` and `fifo_empty` are **already internal wires** in `clcd.sv` — `:359`
(`wire busy = (state_q != ST_IDLE);`) and `:299` (`wire fifo_empty = (count_q ==
8'd0);`) — and are already published in `CLCD.STATUS[2:1]`. The KVM needs them as
*live combinational nets*, not as a register the MicroBlaze polls. W2-A therefore
**appends two output ports to `clcd.sv`**:

```systemverilog
  output logic                            clcd_rst_n_o,  // ... existing last port ...
  // --- NEW (clcd_kvm/README.md §3): live quiescence taps for the KVM's
  //     safe-switch gate. Already internal wires (:299, :359) and already
  //     visible in STATUS[2:1]; brought out so the KVM can gate on them
  //     combinationally instead of via a firmware poll.
  output logic                            busy_o,        // = busy       (:359)
  output logic                            fifo_empty_o   // = fifo_empty (:299)
);
```

Normative:

- **Append-only.** They go at the **end** of the port list. Existing named
  bindings, `tests/clcd/`, `tests/csr_decode_width/test_decode_width_clcd.py` and
  the packaged IP-XACT are all unaffected in behaviour; the IP must be
  **re-packaged** (`fpga/shell/ip/package_csr_ip.tcl`) in Wave 4.
- **No other change to `clcd.sv`.** In particular `CTRL[1]` (backlight) and
  `CTRL[2]` (reset_n) and the `clcd_bl_o` / `clcd_rst_n_o` pins **stay exactly as
  they are** — removing them would break the register contract, the bench and the
  shipped firmware. They now feed the *KVM* instead of the pads (§2, §10).
- The names are `busy_o` / `fifo_empty_o`. They are status nets, not pads, so they
  do **not** take the `clcd_` prefix.

---

## 4. Decode width — read this before writing the address decode

`shell_bd.tcl` sets `CONFIG.C_S_AXI_ADDR_WIDTH {32}` on **every** custom CSR block
(`:481`, `:484`, `:487`, `:490`, `:493`, `:495` …), so the interconnect hands this
slave the **full system address** (`0x44AD_xxxx`), not `0x0`. Decoding
`addr[C_S_AXI_ADDR_WIDTH-1:ADDR_LSB]` compares the full address against `'h0` and
matches nothing — **that exact bug killed every CSR register on silicon at once.**

Decode the block's own **64 KiB page**, the `dfx_ctl.sv:229-258` / `clcd.sv:215-216`
idiom, verbatim:

```systemverilog
localparam int ADDR_LSB     = 2;
localparam int LOCAL_ADDR_W = (C_S_AXI_ADDR_WIDTH < 16) ? C_S_AXI_ADDR_WIDTH : 16;
localparam int IDX_W        = LOCAL_ADDR_W - ADDR_LSB;
```

Only the seven mapped offsets respond. Every other offset in the page is unmapped:
**reads return 0, writes are accepted with no effect (`BRESP=OKAY`)**. Decoding
fewer bits would let `BASE+0x1000` alias offset `0`.

**A `clcd_kvm` entry in `tests/csr_decode_width/` is a REQUIRED W2-C deliverable**
(`test_decode_width_clcd_kvm.py`, modelled on `test_decode_width_clcd.py`): it
elaborates at width **32** and drives `base+offset`.

---

## 5. CSR map — `0x44AD_0000`, 64 KiB page

The next free page after CLCD (`0x44AC`). Master index **15**; `NUM_MI` goes
`15 → 16`.

| Off | Reg | Access | Purpose |
|---|---|---|---|
| `0x00` | `CTRL` | RW + W1P | ownership request, enables, panel BL/RST, escape hatches |
| `0x04` | `STATUS` | RO | who owns it, what the FSM is doing, live inputs |
| `0x08` | `EVENT` | **RW1C** | sticky handover events — **this is how firmware learns it regained the panel** |
| `0x0C` | `PANEL_TMR` | RW | panel-reset pulse + post-reset settle, in µs |
| `0x10` | `TIMEOUT` | RW | hung-owner timeout, in µs |
| `0x14` | `DEBOUNCE` | RW | `USER_nPB1` debounce time, in µs |
| `0x18` | `TUNNEL` | RO | synchronised snapshot of the raw tunnel — host debug |

> ### 🚨 NO READ SIDE EFFECTS ON ANY OFFSET IN THIS PAGE. 🚨
> Not on `EVENT` (it is **W1C**, not read-to-clear), not on `TUNNEL`, not on the
> unmapped offsets. The platform **dumps whole CSR pages over SWD/XVC**, and
> `tests/csr_decode_width/` **reads every offset in the page**. Every action in
> this block is armed by a **WRITE**. The shell has already shipped one
> destructive read (a read of `DFXCTL 0x20` popped a UART console byte through a
> decode alias — `dfx_ctl.sv:229-258`); the `clcd` block was designed
> specifically to not repeat it (`clcd.sv:320-336`). **Do not re-introduce the
> shape.**

### `CTRL` @ `0x00` — RW, reset `0x0000_0000`

| Bit | Name | Access | Reset | Meaning |
|---|---|---|---|---|
| `[0]` | `src_sel` | R / W-gated | `0` | **Reads** the *requested* owner (`tgt_owner`). **Writes** update it **only if `[16] src_sel_we` is also written 1** (see below). `0` = HARNESS, `1` = DUT. |
| `[1]` | `force_harness` | RW | `0` | While `1`: ownership is pinned to HARNESS. All DUT-requesting sources are ignored and any pending switch to DUT is cancelled and reverted **without waiting for DUT quiescence**. The software twin of the DFX interlock (§9). |
| `[2]` | `timeout_en` | RW | **`1`** | `1` = the hung-owner timeout (§6) is armed. **Reset 1 on purpose: a KVM must not be able to get stuck on a dead input.** Clearing it is a debug-only choice. |
| `[3]` | `pb_en` | RW | **`1`** | `1` = a debounced `USER_nPB1` press is a request source. Reset 1: *press the button and it works, with no firmware at all.* |
| `[4]` | `dut_req_en` | RW | **`0`** | `1` = the DUT's tunnelled `req` bit is a request source. **Reset 0 on purpose:** an unprovisioned or garbage RM must not be able to grab the panel at power-on, before the harness has painted anything. Firmware opts the DUT in. |
| `[5]` | `backlight` | RW | `0` | KVM-sourced `CLCD_BL` (active-high). Used when `[7] bl_rst_src = 1`; **always** used while the DUT owns the panel or the KVM is driving the pads. See §10. |
| `[6]` | `panel_rst_n` | RW | `0` | KVM-sourced `CLCD_RST` (active-**low**; `0` = panel held in reset). Same selection rule as `[5]`. Reset `0` = panel held in reset at power-on, matching `clcd.sv`'s `CTRL` reset of `0x0` and the legacy tie-off. |
| `[7]` | `bl_rst_src` | RW | **`0`** | Steady-state source of `CLCD_BL`/`CLCD_RST`. **`0` (reset) = follow `clcd_0`'s `CTRL[1]`/`CTRL[2]`** — i.e. *exactly today's behaviour*, so the **shipped firmware lights the panel unchanged on the new bitstream**. `1` = the KVM's own `[5]`/`[6]` drive the pads. **The KVM's auto panel-reset sequencer overrides `CLCD_RST` in BOTH modes.** See §10. |
| `[8]` | `panel_rst_pulse` | **W1P** | — | Writing `1` arms **one** full auto panel-reset sequence (`S_RST` → `S_SETTLE` → `S_GRANT`) with **no owner change**. Reads back `0`, never stored. Firmware's panel-recovery lever. |
| `[9]` | `force_switch` | **W1P** | — | Writing `1` commits the pending switch **immediately**, skipping the outgoing-owner drain gate (§6). Reads back `0`. The escape hatch for a hung owner without waiting out `TIMEOUT`; also how W2-C's bench provokes a mid-burst cutover. |
| `[15:10]` | — | RAZ/WI | `0` | reserved |
| `[16]` | `src_sel_we` | **W1P** | — | Write-enable for `[0]`. `tgt_owner` is updated from `[0]` **only** on a write where this bit is `1`. Reads back `0`. **Why:** without it, any read-modify-write of `CTRL` (e.g. to set the backlight) would silently clobber an ownership change made by a concurrent `USER_nPB1` press. With it, a plain RMW of `CTRL` **never** touches ownership. |
| `[31:17]` | — | RAZ/WI | `0` | reserved |

Byte strobes are honoured (the `clcd.sv:263-274` idiom): `s_axi_wstrb[0]` gates
`[7:0]`, `[1]` gates `[15:8]`, `[2]` gates `[23:16]`. A write with `wstrb[0]=0`
therefore cannot change `src_sel`, `backlight`, `panel_rst_n` …

### `STATUS` @ `0x04` — RO, no side effects

| Bit | Name | Meaning |
|---|---|---|
| `[0]` | `owner` | The **committed** owner. `0` = HARNESS, `1` = DUT. This is what is actually reaching the pads. |
| `[1]` | `switch_pending` | `tgt_owner != owner` (derived, not a separate flag). |
| `[2]` | `tgt_owner` | The **requested** owner. Same value `CTRL[0]` reads back. |
| `[3]` | `panel_rst_active` | The `CLCD_RST` pad is currently **low**, from any cause (auto sequence, `CTRL.panel_rst_n=0`, or `clcd_0.CTRL[2]=0` in back-compat mode). |
| `[4]` | `panel_settling` | FSM is in `S_SETTLE`. |
| `[5]` | `granting` | FSM is in `S_GRANT` (waiting for the *incoming* owner to be quiescent). |
| `[6]` | `draining` | FSM is in `S_DRAIN` (waiting for the *outgoing* owner to be quiescent). |
| `[7]` | `kvm_drives_pads` | FSM ∈ {`S_RST`, `S_SETTLE`, `S_GRANT`} — **neither source reaches the pads**; the KVM is driving the idle pattern (§8). |
| `[8]` | `harness_quiet` | Live: `h_fifo_empty && !h_busy` (§6). |
| `[9]` | `dut_quiet` | Live: `!dut_cs && !dut_busy` (synchronised, filtered — §6, §12). |
| `[10]` | `dut_req` | Live synchronised level of the tunnel's `req` bit. |
| `[11]` | `pb_level` | **Debounced** `USER_nPB1`, in the **pressed** sense (`1` = pressed; the pad is active-low and is inverted here). **Resets to `1`** (§11 step 5): a button held through reset is not a press; unheld, it reads `1` until the first 1 µs tick. |
| `[12]` | `pb_raw` | Synchronised-but-**undebounced** `USER_nPB1`, pressed sense. Diagnostics / bench. |
| `[13]` | `decoupled` | Synchronised `decouple_status`. |
| `[14]` | `rp_in_reset` | Synchronised `!rp_resetn`. |
| `[15]` | `interlock` | `decoupled \| rp_in_reset \| CTRL.force_harness` — the single "the DUT may not have the panel" condition (§9). |
| `[18:16]` | `state` | FSM state code: `0`=`S_OWN`, `1`=`S_DRAIN`, `2`=`S_RST`, `3`=`S_SETTLE`, `4`=`S_GRANT`. (`5`–`7` unreachable.) |
| `[31:19]` | — | reserved, read `0` |

### `EVENT` @ `0x08` — **RW1C**, reset `0x0000_0000`

Sticky. **Write `1` to a bit to clear it; writing `0` leaves it set. Reading does
NOT clear.** (`EVENT` is swept by `csr_decode_width` and by every SWD/XVC page
dump — a read-to-clear register here would silently destroy handover events.)

| Bit | Name | Set when |
|---|---|---|
| `[0]` | `harness_gained` | A handover **committed into** `owner = HARNESS`. **⇒ the harness must re-init the panel and repaint.** |
| `[1]` | `harness_lost` | A handover **committed out of** `owner = HARNESS` (i.e. into DUT). Confirms the handover completed. |
| `[2]` | `dut_gained` | A handover committed into `owner = DUT`. |
| `[3]` | `dut_lost` | A handover committed out of `owner = DUT`. |
| `[4]` | `timeout_fired` | The hung-owner timeout preempted a wait — in `S_DRAIN` (outgoing owner never went quiet) **or** in `S_GRANT` (incoming owner never went quiet). |
| `[5]` | `forced_revert` | The interlock (§9) forced a revert to HARNESS. |
| `[6]` | `pb_toggle` | A debounced `USER_nPB1` **press** was accepted as a request. |
| `[7]` | `panel_reset_done` | **Any** auto panel-reset sequence completed (`S_GRANT` exited) — *including* one that did not change owner. **⇒ if you own the panel and this is set, the panel was reset under you: re-init and repaint.** |
| `[31:8]` | — | reserved, read `0`, W1C-ignored |

> **The firmware rule, stated once:** *re-initialise and repaint whenever
> `(harness_gained \| panel_reset_done)` is set while you own the panel.* Clear the
> bits with a W1C **before** you start repainting, so a handover that races your
> repaint is not lost. `harness_gained` alone is not sufficient — see §7's
> interlock-during-settle case, where the panel is reset **without** an owner
> change.

### `PANEL_TMR` @ `0x0C` — RW

| Bits | Name | Reset | Meaning |
|---|---|---|---|
| `[15:0]` | `rst_us` | `2000` (**2 ms**) | `CLCD_RST` held **low** for this long in `S_RST`. Matches the working driver's `CLCD_RESET_PULSE_MS = 2` (`firmware/clcd/clcd.c:56-58`, `:764`). |
| `[31:16]` | `settle_us` | `5000` (**5 ms**) | Pads held **idle by the KVM** for this long after `CLCD_RST` is released, before any source may drive. Matches the init table's leading `{HX_DLY, 5}` (`firmware/clcd/hx8347_init.c:74`). |

### `TIMEOUT` @ `0x10` — RW

| Bits | Name | Reset | Meaning |
|---|---|---|---|
| `[31:0]` | `timeout_us` | `1000` (**1 ms**) | Hung-owner timeout. Applies **separately** to the `S_DRAIN` wait and the `S_GRANT` wait. Armed only while `CTRL.timeout_en = 1`. Justified in §6. |

### `DEBOUNCE` @ `0x14` — RW

| Bits | Name | Reset | Meaning |
|---|---|---|---|
| `[15:0]` | `debounce_us` | `10000` (**10 ms**) | `USER_nPB1` debounce integration time (§11). |
| `[31:16]` | — | reserved, RAZ/WI | |

### `TUNNEL` @ `0x18` — RO, no side effects

| Bits | Name | Meaning |
|---|---|---|
| `[15:0]` | `gpio_o` | The **synchronised, stability-filtered** `dut_gpio_o_i` (so `[15:8]` = the DUT's `PD[7:0]`). |
| `[31:16]` | `gpio_oe` | The synchronised, filtered `dut_gpio_oe_i` (so `[31:24]` = the DUT's control/status byte). |

A host-side debug window onto the tunnel: it is how you tell "the DUT is not
driving anything" from "the DUT is driving and the KVM is discarding it" without a
scope. **Pure capture. No side effect.**

### Timebase

Every µs field is counted against a **1 µs tick** derived from `s_axi_aclk`:
`TICK_DIV = CLK_HZ / 1_000_000` (= **100** at the shipped 100 MHz). A field written
as `0` is treated as **1 µs** (floored, the `clcd.sv:363-365` `phase_load` idiom) —
no field may degenerate to zero time.

---

## 6. The safe-switch gate — QUIESCENCE, defined precisely

**A source is *quiescent* when it has nothing in flight and nothing queued, and its
`CS` is deasserted.** That is a single definition, and both sides instantiate it:

| Side | `quiet` = | Why exactly this |
|---|---|---|
| **HARNESS** | `h_fifo_empty && !h_busy` | `!busy` ⇔ `clcd.sv`'s FSM is in `ST_IDLE` ⇔ `cs_n` is **high** (`clcd.sv:470-495`: `CS` is asserted **only** in the three non-IDLE states). `fifo_empty` additionally guarantees no *queued* bytes: bytes stranded in the FIFO across a handover would be emitted on the way **back**, out of sequence, into a panel that has since been reset. Draining costs at most 128 × ~110 ns ≈ **14 µs**. |
| **DUT** | `!dut_cs && !dut_busy` | `!dut_cs` ⇔ no 8080 cycle in flight (same per-byte property, §8 of `CLCD_PANEL_FACTS.md`). `dut_busy` is the tunnel's `busy` bit, **defined** (`dut-display-tunnel.md`) as *"the accelerator's display engine has any byte in flight **or** queued"* — the exact analogue of `!(fifo_empty && !busy)`. **Clamped to `0` when the RP is decoupled ⇒ a dead RP reads as quiescent by construction, and the gate can never hang on it.** |

**`CS` is deasserted between EVERY byte** (`docs/CLCD_PANEL_FACTS.md` §6, board-proven:
`CS` is asserted only in `ST_SETUP`/`ST_STROBE_LO`/`ST_STROBE_HI`, and the FSM
returns through `ST_IDLE` for ≥1 cycle). So a quiescent point exists **per byte**,
not per burst — worst-case wait for `!busy` alone is one byte time (~110 ns). The
FIFO drain is what actually sets the latency, and it is bounded.

### The gate is applied TWICE, on BOTH sides

This is the subtlety that would otherwise sink Wave 2:

- **`S_DRAIN` — the OUTGOING gate.** Do not stop driving the current owner until it
  is quiescent, or you truncate *its* cycle.
- **`S_GRANT` — the INCOMING gate.** Do not *start* driving the new owner until
  **it** is quiescent either, or the pads jump into the middle of a cycle the new
  source was already running (it has no idea it is about to be granted) — `CS`
  would assert mid-strobe with no setup. **A one-sided gate is not enough.**

Between the two gates the KVM drives the pads itself (§8), so no source can reach
the panel while the switch is in flight.

### The hung-owner timeout — **1 ms**, and why

Both waits are bounded by `TIMEOUT.timeout_us`, armed by `CTRL.timeout_en`
(reset **1**). On expiry: set `EVENT.timeout_fired` and **proceed anyway**.

Justification against the ~110 ns/byte figure (`CLCD_PANEL_FACTS.md` §6):

| Legitimate worst case | Time |
|---|---|
| Drain a **full** 128-entry `clcd` FIFO | 128 × 110 ns = **14.1 µs** |
| Drain a whole firmware superloop pass (`CLCD_BYTES_PER_PASS = 256`) | 256 × 110 ns = **28.2 µs** |
| Drain the reference `ahb_clcd`'s 32-entry FIFO at its 480 ns/byte floor (§12) | 32 × 480 ns = **15.4 µs** |

**1 ms is ≈ 9,000 byte-times — 35× the worst legitimate quiescence latency**, and
still **250× shorter than the harness's 250 ms refresh period** (`clcd.h:62-63`),
so a hung owner never blocks a switch for a human-perceptible time. It is also far
below the 10 ms debounce, so a user cannot out-press the timeout.

Worst-case end-to-end handover: `1 ms (drain) + 2 ms (rst) + 5 ms (settle) +
1 ms (grant) = **9 ms**`. Typical (both sides idle): ~7 ms. Imperceptible.

`CTRL.force_switch` (W1P) skips the `S_DRAIN` wait entirely — the bench's lever for
provoking a **deliberately truncated** cutover, and the mutation target: **remove
the quiescence condition from `S_DRAIN`/`S_GRANT` and W2-C's truncated-cycle test
MUST fail while its siblings pass.**

---

## 7. Ownership FSM — NORMATIVE

Two registers carry ownership. Keep them distinct; most bugs here come from
merging them.

- **`tgt_owner_q`** — the **requested** owner. Reset `HARNESS`.
- **`owner_q`** — the **committed** owner (what reaches the pads). Reset `HARNESS`.
- `switch_pending` ≡ `(tgt_owner_q != owner_q)`. **Derived. Not a separate flag.**

### Request sources → `tgt_owner_q`

Evaluated every cycle. **Priority, highest first** (this order is normative; two
sources *can* fire in the same cycle):

1. **Interlock** — `decoupled | rp_in_reset | CTRL.force_harness` (§9):
   `tgt_owner_q <= HARNESS`, and **every DUT-requesting source below is masked**.
2. **CSR** — a write to `CTRL` with `wstrb[2] && wdata[16] (src_sel_we)`:
   `tgt_owner_q <= wdata[0]`.
3. **Button** — an accepted debounced `USER_nPB1` **press** (§11), if `CTRL.pb_en`:
   `tgt_owner_q <= ~tgt_owner_q`. **It toggles the *target*, not the owner** — so a
   second press during a switch-in-flight cancels it and you end up where you
   started. Least-surprising behaviour; also sets `EVENT.pb_toggle`.
4. **DUT** — an **edge** on the synchronised tunnel `req` bit, if `CTRL.dut_req_en`:
   rising edge → `tgt_owner_q <= DUT`; **falling edge → `tgt_owner_q <= HARNESS`**
   (so the DUT can hand the panel back). Edge, not level — a level would let a
   stuck-high `req` veto every attempt to take the panel away with the button.

### States

| State | Pads driven by | Exit condition |
|---|---|---|
| `S_OWN` | **the current owner** (`owner_q`) | `switch_pending` → `S_DRAIN`; `CTRL.panel_rst_pulse` → `S_RST` |
| `S_DRAIN` | **still the current owner** | `cur_owner_quiet` (§6) → `S_RST`. Also → `S_RST` immediately on `CTRL.force_switch`, on `timeout_us` expiry (set `EVENT.timeout_fired`), or **on the interlock while `owner_q == DUT`** (do **not** wait for a clamped RP). If `switch_pending` goes false (a second press cancelled it) → back to `S_OWN`. |
| `S_RST` | **the KVM** (idle pattern, §8) | `CLCD_RST` forced **low**; `CLCD_BL` forced **off**. After `PANEL_TMR.rst_us` → `S_SETTLE`. |
| `S_SETTLE` | **the KVM** (idle pattern) | `CLCD_RST` released. After `PANEL_TMR.settle_us` → `S_GRANT`. |
| `S_GRANT` | **the KVM** (idle pattern) | `tgt_owner_quiet` (§6, the **incoming** side) → **commit**. Or `timeout_us` expiry → commit anyway + `EVENT.timeout_fired`. |

**Commit** (the `S_GRANT` → `S_OWN` edge), atomically:

```
owner_q <= tgt_owner_q;
EVENT.panel_reset_done <= 1;                       // ALWAYS -- the panel WAS reset
if (tgt_owner_q != owner_q) begin                  // a real handover
  if (tgt_owner_q == DUT) { EVENT.dut_gained <= 1; EVENT.harness_lost <= 1; }
  else                    { EVENT.harness_gained <= 1; EVENT.dut_lost <= 1; }
end
```

`S_RST`/`S_SETTLE`/`S_GRANT` are **shared** by a plain `CTRL.panel_rst_pulse`
(where `tgt_owner_q == owner_q`, so no owner change and no gained/lost pair — but
`panel_reset_done` still fires, and the owner still has to repaint). **One
sequencer, two callers.** That is why the firmware rule in §5 is
`harness_gained | panel_reset_done`, not `harness_gained` alone: an interlock that
fires during `S_SETTLE` of a HARNESS→DUT switch flips `tgt_owner_q` back to
HARNESS mid-sequence, so the harness gets a freshly-reset panel with **no**
`harness_gained` — because it never actually lost it.

---

## 8. What the KVM drives while it owns the pads (`S_RST` / `S_SETTLE` / `S_GRANT`)

The **idle pattern**. Normative, exactly:

| Pad | Value | Note |
|---|---|---|
| `clcd_cs_n_o` | `1` | deasserted |
| `clcd_wr_n_o` | `1` | deasserted |
| `clcd_rd_n_o` | `1` | deasserted |
| `clcd_rs_o` | `0` | "command"; don't-care with `CS` idle |
| `clcd_pd_o` | `8'h00` | |
| `clcd_pd_oe` | `1` | **drive** — never float the bus toward the panel |
| `clcd_rst_n_o` | `0` in `S_RST`; else per §10 | |
| `clcd_bl_o` | `0` whenever `clcd_rst_n_o == 0`; else per §10 | |

**`clcd_pd_oe` is held `1` at ALL times in v1**, in every state and under either
owner, *except* during a harness read cycle if `READ_PATH=1` is ever built (§10's
pass-through rule). This is bit-identical to what ships today (`clcd.sv:505`:
`READ_PATH=0` ⇒ `clcd_pd_oe = 1'b1`).

Pad drive per state, in one line. **The current owner keeps the pads through
`S_DRAIN`** — that is the whole point of the drain (§6): the outgoing owner must
finish its in-flight 8080 cycle before the KVM seizes the bus, or the cycle is
truncated. So the owner drives in **`S_OWN` *and* `S_DRAIN`**, and the KVM drives
the idle pattern only in `{S_RST, S_SETTLE, S_GRANT}` — matching the state table
above and `STATUS[7] kvm_drives_pads`. (An earlier draft wrote `state == S_OWN`
alone here; that contradicted the state table and would have truncated the very
cycle `S_DRAIN` exists to protect. Corrected — `clcd_kvm.sv` implements the
version below.)

```
owner_holds    = (state == S_OWN) || (state == S_DRAIN);
harness_drives = owner_holds && (owner_q == HARNESS);
dut_drives     = owner_holds && (owner_q == DUT);
kvm_drives     = !harness_drives && !dut_drives;    // state ∈ {S_RST,S_SETTLE,S_GRANT} == STATUS.kvm_drives_pads
```

and the mux itself is the **`board_gpio.sv:348-349` shape** — pure combinational,
no added latency, one selector:

```systemverilog
// board_gpio.sv:348-349 solved this once already (own_q selects host-vs-DUT
// per bit, combinational, exhaustively bench-proven in
// tests/board_gpio/test_gpio_mux_logic.py). Same shape here, one selector for
// the whole group instead of per-bit.
assign clcd_cs_n_o = harness_drives ? h_cs_n : (dut_drives ? ~dut_cs_f : 1'b1);
assign clcd_wr_n_o = harness_drives ? h_wr_n : (dut_drives ? ~dut_wr_f : 1'b1);
assign clcd_rs_o   = harness_drives ? h_rs   : (dut_drives ?  dut_rs_f : 1'b0);
assign clcd_pd_o   = harness_drives ? h_pd_o : (dut_drives ?  dut_pd_f : 8'h00);
// ... rd_n / pd_oe: see §10's pass-through rule.
```

Note the **inversion** on the DUT side: the tunnel carries strobes **active-HIGH**
(`~dut_cs_f`, `~dut_wr_f`); the pads are **active-LOW**. That inversion lives here,
in the KVM, and **nowhere else**. See `docs/contracts/dut-display-tunnel.md` for
why — it is a safety property, not a style choice.

---

## 9. The DFX interlock — forced revert (the reason this block is in the STATIC shell)

```
interlock = decouple_status_sync | ~rp_resetn_sync | CTRL.force_harness;
```

While `interlock` is asserted:

- `tgt_owner_q` is **forced to `HARNESS`** (priority 1 in §7) and every
  DUT-requesting source (`src_sel=DUT`, PB toggle toward DUT, `dut_req`) is
  **masked**. The DUT cannot be granted the panel, and cannot keep it.
- If `owner_q == DUT` when the interlock asserts, `S_DRAIN` **does not wait** —
  it exits on the next cycle straight to `S_RST` (waiting for a clamped or
  reconfiguring RP to declare itself quiescent is exactly the wrong thing).
  `EVENT.forced_revert` is set.

**Normative guarantee, and W2-C's assertion:** *within **8 `s_axi_aclk` cycles
(80 ns)** of `decouple_status` rising or `rp_resetn` falling, the KVM is driving
the pads itself (`STATUS.kvm_drives_pads = 1`) and **zero** DUT-sourced bytes can
reach the panel thereafter.*

There is a **second, independent** line of defence, and it is the deciding argument
for the whole tunnel design: **the decoupler clamps `dut_gpio_o/oe` to `0x0`**
(`shell_bd.tcl:626`, IDs 13/14, `DECOUPLED_VALUE 0x0`), and the tunnel encodes
strobes **active-high** — so a decoupled RP presents "**all strobes idle**"
*by construction*, with no special clamp value and no KVM logic at all. The FSM's
forced revert is belt-and-braces on top of that. **Both** are load-bearing:
`decouple_status` also covers the window where the RP is being *reset* but not yet
clamped.

---

## 10. `CLCD_BL` / `CLCD_RST` — who drives them, and the one refinement

**The DUT can never drive `BL` or `RST`.** There are no `BL`/`RST` bits in the
tunnel and there never will be. That is not negotiable — it is what makes a hung
or garbage DUT **always** recoverable.

**The KVM has final authority over both pads:**

```systemverilog
// bl_rst_src selects the STEADY-STATE source. The auto-reset sequencer ALWAYS
// wins, in either mode -- that is the recovery lever, and it is pure hardware.
wire        kvm_src   = CTRL.bl_rst_src;
wire        bl_src    = kvm_src ? CTRL.backlight   : h_bl;      // h_bl    = clcd_0.clcd_bl_o
wire        rst_n_src = kvm_src ? CTRL.panel_rst_n : h_rst_n;   // h_rst_n = clcd_0.clcd_rst_n_o

assign clcd_rst_n_o = rst_n_src & ~(state == S_RST);   // sequencer overrides, unconditionally
assign clcd_bl_o    = bl_src    &  clcd_rst_n_o;       // BL is FORCED OFF whenever the panel
                                                        // is in reset, from ANY cause
```

### Why `bl_rst_src` exists (a deliberate, justified refinement of `CLCD_KVM_PLAN.md` §3)

The plan says *"`CLCD_BL` and `CLCD_RST` are owned by the KVM, never by either
source"*. Taken literally — KVM registers only, reset `0` — that has a nasty
consequence: on the day the Wave-4 bitstream is flashed, **the shipped firmware
would leave the panel dark and held in reset forever**, because it drives BL/RST
through `CLCD.CTRL`, which no longer reaches the pads. A bitstream that bricks the
display for every previously-working ELF is not acceptable.

`bl_rst_src = 0` (reset) makes the KVM a **drop-in**: with the harness owning the
panel and the FSM in `S_OWN`, `BL`/`RST` come from `clcd_0`'s `CTRL[1]`/`CTRL[2]`,
exactly as today, so the existing image lights the panel with **zero firmware
change**. And because `clcd_0`'s `CTRL` registers **persist** (firmware sets them
once and never clears them), the panel stays lit while the *DUT* owns it too.

Nothing is lost: the DUT still cannot touch `BL`/`RST`, and the KVM's `S_RST`
sequencer still overrides `CLCD_RST` unconditionally, from hardware, at every
handover and on `CTRL.panel_rst_pulse`. **Recoverability is intact.**

**W2-F SHOULD nevertheless set `bl_rst_src = 1`** and drive `BL`/`RST` from the KVM
(`CTRL[5]`/`CTRL[6]`). It is the cleaner steady state, and it is *required* if the
harness ever wants to blank the panel while the DUT owns it. But if W2-F forgets,
**the board still works** — which is the whole point.

### `rd_n` / `pd_oe` pass-through

`h_rd_n` and `h_pd_oe` are **passed through when the harness owns the pads**, and
driven to `1`/`1` in every other case. Under the shipped `READ_PATH=0` both are
constant `1`, so v1 behaviour is bit-identical to today — but a future
`READ_PATH=1` harness read cycle then works with **no KVM port change**, and
`h_pd_i` (§2) already forwards the captured byte back to `clcd_0`. The DUT's `rd`
and `pd_oe` tunnel bits are **RESERVED and IGNORED** in v1 (see the tunnel doc:
the DUT must drive them `0`).

> **Do not design anything on panel read-back.** `docs/CLCD_PANEL_FACTS.md` §7.2:
> `READ_PATH=0` shipped, `CLCD_RD` has never been driven low, and **whether the
> MPS3's CLCD buffers are bidirectional at all is unknown**. Assume no read-back.

---

## 11. `USER_nPB[1]` — synchroniser + debounce (NORMATIVE)

The button is `USER_nPB[1]`, **MPS3 package pin `AT32`**, `LVCMOS18`
(`fpga/monolithic/nanosoc_mps3.xdc:229-230`). It is **unconstrained in the shell
today**; Wave 4 adds the pad. `USER_nPB0` (`AT30`) is the **system POR**
(`shell_top.sv:117`, `wire sys_rst_n = USER_nPB0;`) — **do not touch it.**

> **Naming:** `shell_top.sv` declares the existing button as the **scalar** port
> `USER_nPB0`, not a vector. The new port is therefore the scalar **`USER_nPB1`**,
> and the KVM's pin is `user_npb1`. (`nanosoc_mps3.xdc` calls the same physical
> net `USER_nPB[1]`; that is the *board* name, and the two need not match.)
> Bank gate: run `fpga/dfx/build_clcd/bank_gate.tcl` — `AT30` is already
> `LVCMOS18`, so `AT32` in the same bank is expected to pass, but **prove it**.

Chain, in the `s_axi_aclk` domain:

1. **Synchronise.** `PB_SYNC_STAGES = 3` flip-flops, `(* ASYNC_REG = "TRUE" *)`.
   Three, not two: it is a true asynchronous, slow-edged, *human*-driven pad with
   no upstream register, and the extra stage costs 1 FF.
2. **Invert to the pressed sense.** `pb_raw = ~sync[PB_SYNC_STAGES-1]`
   (pad is **active-low**: pressed = `0`). `STATUS[12]` reports `pb_raw`.
3. **Debounce — integrate, don't just delay.** A µs counter reloads to
   `DEBOUNCE.debounce_us` whenever `pb_raw == pb_level`; while `pb_raw != pb_level`
   it counts down; when it reaches `0` (i.e. the *new* level has held, unbroken,
   for the full debounce time) `pb_level <= pb_raw`. Any bounce back to the old
   level restarts the count. Default **10 ms**, comfortably above the
   1–5 ms bounce of a real tactile switch and far below a human double-press.
   `STATUS[11]` reports `pb_level`.
4. **Edge → toggle, on PRESS only.** A **rising** edge of `pb_level` (i.e. the pad
   falling, i.e. the button going **down**) emits **exactly one** `pb_press` pulse.
   **Release does nothing.** Each accepted press toggles `tgt_owner_q` (§7) and
   sets `EVENT.pb_toggle`.
5. **`pb_level` resets to PRESSED (2026-09-23).** On `s_axi_aresetn`, `pb_level`
   and the edge detector's delayed copy reset to `1`. So a button **held through
   reset** (power-on, or a WDOG reset, which is also `s_axi_aresetn`) produces
   **no press edge**: once the synchroniser has filled, `pb_raw == pb_level`, the
   counter only reloads, and nothing happens until a real release followed by a
   real press. D13's boot hook reads "PB1 held at power-up" as "skip the default
   overlay load"; that hold must never also flip the panel. (Until 2026-09-23
   `pb_level` reset to *released*, and a held button was accepted as a press on
   the first 1 µs tick after reset.) Side effect: with the button **not** held,
   `STATUS.pb_level` reads `1` until the first tick accepts the release (~1 µs:
   the debounce counter resets to 0) — a falling edge, never an event — and a
   press made inside that first microsecond is folded into it. Assumes
   `TICK_DIV > PB_SYNC_STAGES` (the first tick lands after the synchroniser has
   filled): true for any `CLK_HZ` ≥ 4 MHz; shipped 100 MHz.

**This whole chain is HARDWARE.** It must work with the MicroBlaze halted, the
harness firmware wedged, or the DUT hung. `CTRL.pb_en` (reset **1**) can disable
it, but nothing else can.

**W2-C bench property:** *a bouncing edge produces exactly one toggle.* Drive a
train of transitions inside the debounce window and assert `EVENT.pb_toggle` is set
once and `tgt_owner` moved once.

**Bench property (step 5):** *a button held through reset is not a press.*
`test_pb1_held_through_reset_is_not_a_press` (programmed debounce) and
`test_pb1_held_through_reset_real_timing` (the shipped 10 ms, and a second
WDOG-style reset with the button still held): no toggle, no `EVENT.pb_toggle`,
and a real release + press afterwards still toggles. `make -C tests/clcd_kvm
falsify-pb-reset` restores the old reset values and both must fail.

---

## 12. Clock-domain crossing — the tunnel is ASYNC, and this is where it is handled

`dut_gpio_o_i` / `dut_gpio_oe_i` arrive from the RP in the **`dut_clk`** domain
(**50 MHz** shipped — `shell_bd.tcl` `clk_wiz_dut` `CLKOUT1_REQUESTED_OUT_FREQ =
50.000`), and the KVM runs on **`s_axi_aclk`** (100 MHz). They are asynchronous.
`board_gpio` gets away with a purely combinational mux because it only *drives a
pad*; the KVM **decodes a strobed protocol**, and per-bit synchroniser skew would
tear the 8080 vector apart.

Normative:

1. **Synchronise** all 16 + 16 tunnel bits (and `decouple_status`, `rp_resetn`) with
   **2-FF** synchronisers, `(* ASYNC_REG = "TRUE" *)`.
2. **Stability-filter.** The pad-facing DUT drive registers (`dut_pd_f`, `dut_cs_f`,
   `dut_wr_f`, `dut_rs_f`, `dut_busy_f`, `dut_req_f`) update **only when the
   synchronised vector has been identical on two consecutive `s_axi_aclk` cycles**
   (`sync2 == sync3`). This makes **per-bit synchroniser skew invisible at the
   pads**: the panel can never see a half-updated DUT vector (e.g. `WR` asserting
   one cycle before `PD` settles). Cost: ≤2 shell cycles (20 ns) of latency and 16
   FFs. `TUNNEL` @ `0x18` reads the **filtered** value.
3. **Therefore the DUT must obey a timing floor** — restated normatively in
   `fpga/rp/nanosoc_exp/README.md` §7 and `docs/contracts/dut-display-tunnel.md`:

   > **Every 8080 phase the accelerator drives lasts ≥ 8 `dut_clk` cycles, and
   > `PD`/`RS` are stable ≥ 4 `dut_clk` cycles before `wr` asserts and ≥ 4 after it
   > deasserts. `dut_clk` must not exceed `s_axi_aclk` (100 MHz) while the DUT owns
   > the panel.**

   At the shipped 50 MHz that is **≥160 ns per phase** and **≥80 ns of PD/RS
   guard** — 8× and 4× the KVM's 20 ns filter latency, and comfortably above what
   the panel needs (the *harness* runs 20/40/40 ns and works). The reference
   accelerator's defaults (`CS_SETUP=8, WR_LO=8, WR_HI=8` `dut_clk` cycles ⇒
   ~480 ns/byte ⇒ ~2 MB/s ⇒ a full 320×240×2 = 150 KB repaint in ~75 ms) sit inside
   this floor with margin.

The **harness** side needs **no** synchroniser — `clcd_0` is in `s_axi_aclk` with
the KVM.

---

## 13. Integration (Wave 4 — informative, but the wave depends on it)

`shell_bd.tcl`:

- `create_bd_cell … soclabs.org:user:clcd_kvm:1.0 clcd_kvm_0`;
  **`set_property CONFIG.C_S_AXI_ADDR_WIDTH {32} $clcd_kvm_0`** (§4 — mandatory);
  add it to the `s_axi_aclk`/`s_axi_aresetn` fan-out `foreach` list (`:499`).
- `NUM_MI {15}` → **`{16}`**; `mi_map` gains `15 $clcd_kvm_0 s_axi`;
  `assign_bd_address -offset 0x44AD0000 -range 64K [get_bd_addr_segs {clcd_kvm_0/s_axi/reg0}]`.
- **Re-route `clcd_0`'s pad outputs into `clcd_kvm_0`'s `h_*` inputs.** `clcd_0` no
  longer connects to the BD's CLCD ports. `clcd_kvm_0`'s pad outputs take their
  place. Wire `clcd_0`'s two **new** pins (`busy_o`, `fifo_empty_o`) to `h_busy` /
  `h_fifo_empty`, and `clcd_kvm_0/h_pd_i` back to `clcd_0/clcd_pd_i`.
- **Tap** the **post-decoupler** `dut_gpio_o` / `dut_gpio_oe` nets — the *same* nets
  that already feed `board_gpio_0/dut_gpio_o_i` / `dut_gpio_oe_i`. **Do not move
  them.** `board_gpio_0` keeps its connection; bits `[7:0]` still drive the LEDs.
- Fan `dfx_decoupler_0/decouple_status` out to `clcd_kvm_0/decouple_status` (it
  already feeds `dfx_ctl_0/decoupled_i` at `:850`).
- Fan `dut_clkrst_0/rp_resetn_o` out to `clcd_kvm_0/rp_resetn` (it already feeds the
  `rp_rp_resetn` BD port at `:665` and an inverter at `:687`).
- New BD port `USER_nPB1` (dir I) → `clcd_kvm_0/user_npb1`.

`shell_top.sv`: add the `input wire USER_nPB1` port and pass it into the BD. The
IOBUF/pad block (`:318-338`) is **unchanged** — only the *source* of `w_clcd_*`
moves from `clcd_0` to `clcd_kvm_0` (both inside the BD).

`mps3_harness.xdc`: `AT32`, `LVCMOS18` (run the bank gate).

`fpga/shell/ip/package_csr_ip.tcl`: package `clcd_kvm`; **re-package `clcd`** (its
port list grew by two).

**Cost:** new static RTL ⇒ `static_id` `0xE4B1C44A` → new ⇒ all **8** overlays
re-keyed. Unavoidable for any harness-resident KVM, and **scripted and proven** —
`fpga/dfx/build_clcd/` (`bank_gate.tcl`, `finish_partials.tcl`, `rekey_recover.sh`,
`gate_verify`) did exactly this on 2026-07-11 in one overnight run.
**`pin_check` is untouched and must stay green** — the tunnel costs **zero**
partition-pin change, and that is the proof.

---

## 14. Bench contract (W2-C — `tests/clcd_kvm/`)

Reuse `tests/clcd/clcd_panel_model.py` **as-is**: it is a cocotb monitor that plays
the HX8347-D on the 8080 pads (latching `{RS,byte}` on each `WR_n` **rising** edge
while `CS_n` is asserted). It watches only the **pads**, so it is bus-agnostic and
serves this bench, `tests/nanosoc_lcd/` and the end-to-end Wave-3 bench unchanged.

Required properties:

1. **Truncated-cycle — THE HEADLINE.** Request a switch **mid-burst**, from each
   side, at every phase of the 8080 cycle. Assert the panel model sees **only
   whole, well-formed cycles**, and that **`CS_n` was never left asserted across a
   handover**. **Mutation-prove it:** delete the quiescence condition from
   `S_DRAIN` (and, separately, from `S_GRANT`) and this test **must fail while its
   siblings pass**. A gate that cannot fail proves nothing (the `systemrdl`
   equiv-gate lesson).
2. **Forced revert.** Assert `decouple_status` (and separately drop `rp_resetn`)
   while the DUT owns the panel. Assert `STATUS.kvm_drives_pads` within **8 shell
   cycles**, `EVENT.forced_revert` set, ownership back at HARNESS, and **zero**
   DUT-sourced bytes at the panel thereafter. Also drive the tunnel to `0x0` (the
   real clamp) and assert the panel sees nothing — the *by-construction* half of §9.
3. **Hung-owner timeout.** An owner that never goes quiescent is preempted after
   `TIMEOUT.timeout_us`, `EVENT.timeout_fired` is set, and the panel **is reset**.
   Test both the `S_DRAIN` and the `S_GRANT` timeout.
4. **Debounce.** A bouncing edge produces **exactly one** toggle (§11).
5. **CDC / stability filter.** Run the DUT model on a `dut_clk` asynchronous to
   `s_axi_aclk` (50 MHz, and a deliberately non-integer ratio, e.g. 37 MHz) and
   assert no torn vector reaches the pads.
6. **`csr_decode_width`.** `tests/csr_decode_width/test_decode_width_clcd_kvm.py`
   at width **32**, driving `base+offset`, sweeping the **whole 64 KiB page** —
   and asserting **no read of any offset changes any pad or any `EVENT` bit**.

Set `PANEL_TMR`/`TIMEOUT`/`DEBOUNCE` to small µs values from the bench (that is what
they are CSRs for) so a full handover is a few hundred cycles, not a million.
