# `clcd` — QVGA HX8347-D 8080 parallel bus master

Contract: `docs/contracts/shell-regmap.md` v0.4.1, CLCD @ `0x44AC_0000`.
Design: `docs/CLCD_STATUS_DISPLAY_PLAN.md`.
**Proven panel configuration: `docs/CLCD_PANEL_FACTS.md`** — write drivers and
benches against that, not against the assumptions this doc originally carried.

**Status: INSTANTIATED, and the panel is LIT.** The old "RESERVED / `NUM_MI =
14`" text was already stale when the KVM work started: the Phase-D rider shipped
on **2026-07-11** in commit **`b4afe3e`** — `shell_bd.tcl:494` instantiates
`clcd_0`, `:1041` sets `NUM_MI 15`, `:1108` `assign_bd_address -offset
0x44AC0000 -range 64K`, `static_id` was re-minted `0x14E1A2D8` → **`0xE4B1C44A`**
and all 8 overlays re-keyed. The panel renders the harness status screen on the
MPS3 bench (board-observed 2026-07-14). Resolve state as **board > commits >
build reports > docs**.

> **Forward note (the CLCD KVM, `0x44AD_0000`).** Once the KVM lands (Wave 4 of
> `docs/CLCD_KVM_WAVE_PLAN.md`), `clcd_0` no longer reaches `shell_top`'s IOBUFs
> directly — it feeds `clcd_kvm_0`, which arbitrates this block against a
> DUT-side accelerator and drives the pads. `CLCD.CTRL[1]`/`CTRL[2]`
> (backlight/reset_n) then reach the pads *through* the KVM, which by default
> (`CLCDKVM.CTRL.bl_rst_src = 0`) passes them straight through, so existing
> firmware keeps lighting the panel unchanged. This block also gained two
> **append-only** output pins (`busy_o`, `fifo_empty_o`) and its FIFO+FSM was
> factored into `clcd_core.sv` — both below. Neither is a register-map change;
> the KVM rebuild re-mints `static_id` again and re-keys all 8 overlays.

## What this block is

An AXI4-Lite→8080 **byte-streaming bus master**, nothing more. Firmware pushes
`{RS, byte}` pairs into a FIFO via `CMD`/`DATA`; a bus FSM pops them and drives
`CS`/`RS`/`WR`/`PD[7:0]` with the HX8347-D's setup/hold timing, parameterised in
`s_axi_aclk` cycles.

It holds **no framebuffer and no font ROM**. The HX8347-D has on-chip GRAM —
the panel *is* the framebuffer. Glyph expansion happens in firmware
(`firmware/clcd/`). A fabric framebuffer would cost ~34–38 RAMB36 on top of the
shell's 129.5 BRAM tiles to duplicate memory the controller already has.

The block is **protocol-agnostic**: it knows nothing about HX8347 register
values, only about 8080 bus cycles. Correcting the panel init sequence is a
firmware-data change and never touches this RTL. That is the whole point of the
split — see the init-table seam below.

## Agreed port contract (FROZEN — bench and Phase-D integration bind to these)

```systemverilog
module clcd #(
  // Local offset decode only. The base (0x44AC_0000, 64 KiB page) is set in
  // the BD Address Editor. shell_bd.tcl instantiates every CSR block with
  // C_S_AXI_ADDR_WIDTH = 32 -- the RTL default of 12 is NEVER what ships.
  // Decode the block's own 64 KiB page (see "Decode width" below).
  parameter int C_S_AXI_ADDR_WIDTH = 12,
  parameter int C_S_AXI_DATA_WIDTH = 32,

  // Bus-cycle FIFO depth, in {RS,byte} entries. Must be <= 255 so a full
  // level is representable in STATUS[15:8]. Distributed RAM or 1x RAMB18.
  parameter int FIFO_DEPTH  = 128,

  // Build the optional CLCD_RD read-back path (shell-regmap.md READ @ 0x10).
  // Default 0: some MPS3 CLCD buffers are write-only and the board fact is
  // unconfirmed (plan §13-Q5). With READ_PATH=0, READ reads 0, CTRL.read_start
  // is a no-op, and clcd_rd_n_o is held deasserted.
  parameter int READ_PATH   = 0,

  // TIMING reset values, in s_axi_aclk cycles.
  parameter int WR_LO_INIT    = 4,
  parameter int WR_HI_INIT    = 4,
  parameter int CS_SETUP_INIT = 2
) (
  input  logic                            s_axi_aclk,
  input  logic                            s_axi_aresetn,

  // AXI4-Lite slave. Either blessed handshake style is legal
  // (shell-regmap.md "AXI4-Lite slave conventions"); match telem/dfx_ctl's
  // Xilinx-template FSM for consistency.
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

  // ---- Panel pads (8080). Tristate triplet on the data bus; shell_top.sv
  // instantiates the IOBUFs at the Phase-D batch. pd[0] maps to the pad
  // CLCD_PD[10] ... pd[7] to CLCD_PD[17] (nanosoc_mps3.xdc:495-502).
  output logic [7:0]                      clcd_pd_o,
  input  logic [7:0]                      clcd_pd_i,     // tie 8'h00 if READ_PATH=0
  output logic                            clcd_pd_oe,    // 1 = drive pads
  output logic                            clcd_cs_n_o,   // pad CLCD_CS      (AP15)
  output logic                            clcd_wr_n_o,   // pad CLCD_WR_SCL  (AP14)
  output logic                            clcd_rd_n_o,   // pad CLCD_RD      (AM15)
  output logic                            clcd_rs_o,     // pad CLCD_RS      (AN14) 0=cmd 1=data
  output logic                            clcd_bl_o,     // pad CLCD_BL      (AJ16)
  output logic                            clcd_rst_n_o,  // pad CLCD_RST     (AK18)

  // Live quiescence taps for the CLCD KVM's safe-switch gate
  // (fpga/shell/ip/clcd_kvm/README.md §3). APPEND-ONLY, at the END of the port
  // list, so every existing named binding, tests/clcd/, tests/csr_decode_width/
  // and the packaged IP-XACT are unaffected in behaviour. Status nets, not pads
  // -> no clcd_ prefix. Both were already internal wires, already published in
  // STATUS[2:1]; the KVM needs them combinationally, not via a firmware poll.
  output logic                            busy_o,        // 8080 FSM != IDLE (or a read cycle in flight)
  output logic                            fifo_empty_o   // {RS,byte} FIFO empty
);
```

The IP must be **re-packaged** (`fpga/shell/ip/package_csr_ip.tcl`) in Wave 4
because the port list grew by two — that is the only integration consequence.

Touch (`CLCD_TSCL`/`CLCD_TSDA`/`CLCD_TINT`/`CLCD_TNC`) is **out of v1** and
absent from this block. The pins stay unconstrained and reserved.

**Polarity — VERIFIED on the board (`docs/CLCD_PANEL_FACTS.md` §1, §4).** `CS`/
`WR`/`RD` are active-low and `RST` active-low, per the HX8347-D 8080 interface;
`BL` is active-high. This was an assumption before bring-up; it is now proven —
the panel does not light unless `RST`-low-then-high releases it and `BL`-high
lights it, and it does. The legacy tie-off (`nanosoc_mps3_top.sv:445-455`) drove
the strobes to `1'b0` with `BL=0` and `RST=0`, inert only because the panel was
held in reset and dark; the live block drives them as above. `READ_PATH=0` ships
(read-back is *not* proven — `CLCD_PANEL_FACTS.md` §7.2 — assume none).

## The `clcd_core` split — one proven 8080 engine, two front ends

The `{RS,byte}` FIFO and the three-phase 8080 strobe FSM live in
**`clcd_core.sv`** (this directory), not in `clcd.sv`. `clcd.sv` is the
**AXI4-Lite front end** around that core: the CSR map, the `CTRL`/`TIMING`
register file, the optional `READ_PATH=1` read-back datapath, and the pad mux.

The core is **bus-agnostic and holds no addresses**: it takes a 1-cycle push
(`push_valid`/`push_rs`/`push_data`), an `enable`, a `fifo_reset`, and the three
timing values in clock cycles; it emits the active-low 8080 pads and the
`busy`/`fifo_empty`/`fifo_full`/`fifo_level` status. Its port list is **frozen**
so `fpga/rp/nanosoc_exp/ahb_clcd.sv` (the DUT-side student accelerator) can
instantiate the **same proven engine** behind an AHB-Lite front end and inherit
this block's bench and its cocotb panel model
(`tests/clcd/clcd_panel_model.py`, which watches only the pads).

Two deliberate exclusions from the core (the KVM owns them, the panel has no
read-back):

- **Backlight and panel reset are NOT in the core.** `CLCD_BL`/`CLCD_RST` stay
  in `clcd.sv` (`CTRL[1]`/`CTRL[2]`), and downstream the KVM has final authority
  over both pads — that is what makes a hung DUT recoverable.
- **The read-back path is NOT in the core.** `READ_PATH=1` support stays in
  `clcd.sv`, *around* the core: a pending or in-flight read gates the core's
  `enable` (which gates cycle *launch* only, so a write already in flight always
  completes), and a small mux selects the core's pads or the read cycle's. The
  two can never drive the pads at once, so the external behaviour is bit-for-bit
  what the single pre-split FSM produced. `tests/clcd/` (both the default and the
  `READP=1` elaboration) is the regression gate and passes unchanged.

> **Lint note (top-level `Makefile`):** because `clcd.sv` now instantiates a
> submodule, linting it standalone needs `clcd_core.sv` on the search path. The
> `Makefile`'s `LINT_SV` loop lints each file alone, so `clcd.sv` must move to a
> dedicated `verilator … -Ifpga/shell/ip/clcd fpga/shell/ip/clcd/clcd.sv`
> invocation — exactly the idiom already used for `uart_bridge` — or `clcd.sv`
> and `clcd_core.sv` be linted together. This is a one-line `Makefile` change the
> integrator must make; it is outside this block's file ownership.

## Decode width — read this before writing the address decode

`shell_bd.tcl` sets `CONFIG.C_S_AXI_ADDR_WIDTH {32}` on every CSR block. A
decode of `addr[C_S_AXI_ADDR_WIDTH-1:ADDR_LSB]` therefore compares the *full
system address* against `'h0` and never matches — that regression killed every
CSR register on silicon at once. Decode the block's own **64 KiB page**, the
spacing `assign_bd_address` gives these slaves, exactly as `dfx_ctl.sv:229-258`
now does:

```systemverilog
localparam int LOCAL_ADDR_W = (C_S_AXI_ADDR_WIDTH < 16) ? C_S_AXI_ADDR_WIDTH : 16;
localparam int IDX_W        = LOCAL_ADDR_W - ADDR_LSB;
```

Only the six mapped offsets respond; every other offset in the page is unmapped
(reads return 0, writes ignored, `BRESP=OKAY`). Decoding fewer bits would let
`BASE+0x1000` alias offset 0. `tests/csr_decode_width/` elaborates at width 32
and drives `base+offset`; a CLCD entry there is a required deliverable.

## No read side effects — the panel read-back is armed by a write

`READ` @ 0x10 is a pure read-only capture register. A panel read cycle is armed
by writing `CTRL.read_start` (bit 4, self-clearing); firmware polls
`STATUS.busy`, then reads `READ` for `{valid, rdata}`. Reading `READ` must
never launch a bus cycle.

This is not stylistic. The platform dumps CSR pages over SWD/XVC, and
`tests/csr_decode_width/` reads every offset in the block's page — under a
read-arms-the-cycle design, either would silently drive 8080 strobes at the
panel. The shell has already shipped one destructive read (a read of DFXCTL
`0x20` popped a UART console byte, via the decode alias documented in
`dfx_ctl.sv:229-258`). Do not re-introduce the shape.

With `READ_PATH=0` the whole path compiles out: `CTRL.read_start` is ignored,
`READ` reads 0, `clcd_rd_n_o` stays deasserted and `clcd_pd_oe` is tied 1.

## FIFO overflow policy — do not back-pressure `awready`

`CMD`/`DATA` writes arriving when `STATUS.fifo_full` is set are **dropped**
(accepted at the protocol level, `BRESP=OKAY`, no entry pushed). The slave must
never stall `awready` on a full FIFO: `clcd_poll()` runs in the same superloop
as the lwIP TCP/ARP timers, and a bus stall there drops the network — the only
way into this board. Firmware polls `STATUS` and never pushes into a full FIFO;
the bench proves zero bytes are lost across a panel-side stall.

## The init-table seam (why `firmware/clcd/` has two owners)

The HX8347-D **panel** register sequence — power-on/reset timing, the init
writes, GRAM column/row windowing, the GRAM-write command, the pixel format —
is **not** in this repo and **must not be invented from memory**. It is a
`const` data table, ported from a citable source, with its provenance recorded
next to it.

The table is frozen to this encoding so the RTL, the driver, and the table can
be written independently:

```c
/* firmware/clcd/hx8347_init.h */
#define HX_CMD 0u  /* val -> CLCD_CMD  (RS=0) */
#define HX_DAT 1u  /* val -> CLCD_DATA (RS=1) */
#define HX_DLY 2u  /* val = milliseconds to wait; driver arms a timed,
                    * poll-and-return deadline. NEVER a busy-wait. */

typedef struct { uint8_t op; uint8_t val; } hx8347_entry_t;

extern const hx8347_entry_t hx8347_init[];
extern const unsigned       hx8347_init_len;
extern const char           hx8347_init_provenance[];  /* source URL/doc + rev */
```

`clcd.c` walks this table and knows nothing else about the panel. Because the
FSM is protocol-agnostic, a corrected table is a data-only change: no RTL, no
bench, no re-synthesis.

The **bench does not consume this table.** `tests/clcd/` proves the block
streams *an arbitrary* `{RS,byte}` sequence faithfully, using its own synthetic
vector. That the table's *values* are the right ones for an HX8347-D is a board
fact, provable only when it lights. Keeping the bench off the real table is
deliberate: it stops a wrong table from looking green.
