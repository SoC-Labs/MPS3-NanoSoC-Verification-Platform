# The hole — `nanosoc_exp`: where your accelerator goes

**Version:** v1.0 (2026-07-14). **FROZEN.** This is the interface you code against.

> ## Read this if you have never seen this repo before
>
> **In one sentence:** there is an empty socket inside the CPU's memory map, at
> address **`0x6000_0000`**; you fill it with a hardware block of your own design;
> the CPU can then read and write your block like memory, and your block can draw
> on the LCD screen on the board.
>
> You do **not** need to understand DFX, partial reconfiguration, the shell, the
> decoupler, or the KVM to use this socket. Those words appear below only where
> they change what *you* must do. If a section is marked **"background"** you can
> skip it.

---

## 1. What you are given

```
   ┌──────────────────── nanosoc (the DUT — an Arm Cortex-M0 SoC) ────────────────┐
   │                                                                              │
   │   Cortex-M0 ──┬── AHB-Lite bus matrix ──┬── IMEM / DMEM / SRAM / peripherals │
   │               │                          │                                    │
   │            DMAC 0                        └──► exp_*  ═══════╗                 │
   │               │                              0x6000_0000    ║                 │
   │               └──────────────────────────────────────────►  ║                 │
   │                                                             ▼                 │
   │                                              ┌──────────────────────────┐     │
   │                                              │  nanosoc_exp_socket      │     │
   │                                              │  ***** YOUR RTL *****    │     │
   │                                              └────┬────────────┬────────┘     │
   │                                        irq[3:0] ◄─┘            │ display pins │
   │                                        drq[1:0] ◄──            │              │
   └───────────────────────────────────────────────────────────────┼──────────────┘
                                                                   │
              ══════ the tunnel (16 spare GPIO bits) ══════════════╯
                                                                   │
   ┌───────────────── the shell (static, you never touch it) ──────┼──────────────┐
   │                                                               ▼               │
   │   MicroBlaze ──► clcd (harness status screen) ──►  ┌──────────────────┐       │
   │                                                    │   clcd_kvm       │──► ▓▓ │
   │   USER_nPB[1] (the button) ───────────────────────►│ (panel arbiter)  │  PANEL│
   └────────────────────────────────────────────────────┴──────────────────┴───────┘
```

Three things:

1. **An AHB-Lite slave port** (`exp_*`) at **`0x6000_0000`**, 256 MB. The M0 and
   both DMA controllers can reach it. This is how the CPU talks to your block.
2. **Display pins out.** Your block can drive the panel's 8-bit 8080 bus. They
   travel out through the *tunnel*, into the shell's *KVM*, and onto the physical
   panel — but from where you sit, they are just eight data wires and three control
   wires.
3. **Interrupt and DMA hooks.** `irq[3:0]` go straight to the M0's NVIC
   (`EXP0_IRQn` … `EXP3_IRQn` = NVIC IRQ **11, 12, 13, 14**); `drq[1:0]` go to DMA
   controller 0. Pre-wired. Use them or leave them at `0`.

**The button.** Press `USER_nPB[1]` on the board and the panel switches between the
harness's status screen and *your* block's output. Press it again to switch back.
That is the whole demo. The arbitration, the debounce, the "don't cut a bus cycle
in half" logic and the "the DUT died, take the panel back" logic all live in the
shell (`fpga/shell/ip/clcd_kvm/`), **not in your block**. You just drive bytes.

---

## 2. The module you fill in

Create `fpga/rp/nanosoc_exp/nanosoc_exp_socket.sv` with **exactly** this port list.
The RM wrapper (`fpga/rp/nanosoc/rp_nanosoc_wrapper.sv`) instantiates it by name.
**Do not add, remove or rename ports** — the wrapper is not yours.

```systemverilog
module nanosoc_exp_socket #(
  parameter int ADDR_W = 32,     // = nanosoc's SYS_ADDR_W
  parameter int DATA_W = 32      // = nanosoc's SYS_DATA_W
) (
  // ==== Clock and reset =====================================================
  input  logic              hclk,      // the DUT clock. 50 MHz as shipped.
  input  logic              hresetn,   // ACTIVE-LOW. Asserted (0) = in reset.

  // ==== AHB-Lite SLAVE — the CPU's window into your block ====================
  // Base 0x6000_0000, 256 MB. See §3 for the protocol; §4 for the ONE rule you
  // must not break.
  input  logic              hsel,      // 1 = this transfer is addressed to you
  input  logic [ADDR_W-1:0] haddr,     // byte address
  input  logic [1:0]        htrans,    // 00 IDLE, 01 BUSY, 10 NONSEQ, 11 SEQ
  input  logic              hwrite,    // 1 = write, 0 = read
  input  logic [2:0]        hsize,     // 000 byte, 001 halfword, 010 word
  input  logic [2:0]        hburst,    // 000 SINGLE (you may ignore this)
  input  logic [3:0]        hprot,     // protection hints (you may ignore this)
  input  logic              hmastlock, // locked transfer (you may ignore this)
  input  logic [DATA_W-1:0] hwdata,    // write data — valid in the DATA phase
  input  logic              hready,    // GLOBAL bus ready — see §3. NOT your own.
  output logic [DATA_W-1:0] hrdata,    // read data — you drive it in the DATA phase
  output logic              hreadyout, // 1 = you are done. ***READ §4.***
  output logic              hresp,     // 0 = OKAY, 1 = ERROR. Drive 0 unless you
                                       // deliberately implement an error response.

  // ==== Display pins out (→ tunnel → shell KVM → panel) ======================
  // ALL ACTIVE-HIGH. The shell inverts them to the panel's active-low sense.
  // DO NOT invert them here. (§6 — this is a safety property, not a style.)
  output logic              lcd_en,    // 1 = YOUR block drives the display pins.
                                       //     0 = the M0's GPIO does (Tier 0, §8).
  output logic [7:0]        lcd_pd,    // the 8080 data byte
  output logic              lcd_cs,    // 1 = chip select ASSERTED
  output logic              lcd_wr,    // 1 = write strobe ASSERTED
  output logic              lcd_rs,    // 0 = COMMAND byte, 1 = DATA byte
  output logic              lcd_busy,  // 1 = you have a byte in flight OR queued
  output logic              lcd_req,   // 1 = "I would like the panel, please"

  // ==== Extension hooks (optional — tie to 0 if unused) ======================
  output logic [3:0]        irq,       // → M0 NVIC. irq[0] = EXP0_IRQn (IRQ 11)
                                       //            irq[3] = EXP3_IRQn (IRQ 14)
  output logic [1:0]        drq        // → DMA controller 0
);
```

**Reset values.** On `hresetn == 0` every output must go to its inert value:
`hrdata = 0`, `hreadyout = 1`, `hresp = 0`, `lcd_en = 0`, `lcd_pd = 0`,
`lcd_cs = 0`, `lcd_wr = 0`, `lcd_rs = 0`, `lcd_busy = 0`, `lcd_req = 0`,
`irq = 0`, `drq = 0`. **`hreadyout` resets to `1`, not `0`** — see §4.

There is no `lcd_rd` and no `lcd_pd_oe` in your port list. The panel is
**write-only** in this platform (`docs/CLCD_PANEL_FACTS.md` §5, §7.2: `READ_PATH=0`
shipped, and *whether the board's LCD buffers can be read at all is unknown*). The
wrapper drives the corresponding tunnel bits to `0`. **Do not design anything that
reads the panel.**

---

## 3. AHB-Lite, for someone who has not written one

AHB-Lite is **pipelined**. This is the single thing everyone gets wrong the first
time, and here getting it wrong **hangs the CPU** (§4). Read this section twice.

A transfer takes **two** bus phases, one clock each, and they **overlap** with the
neighbouring transfers:

```
             │ cycle 1  │ cycle 2  │ cycle 3  │
             ├──────────┼──────────┼──────────┤
haddr,hwrite │  ADDRESS │          │          │   <- the ADDRESS PHASE of transfer A
hsize,htrans │  phase A │          │          │
             │          │          │          │
hwdata       │          │   DATA   │          │   <- the DATA PHASE of transfer A.
hrdata       │          │  phase A │          │      hwdata/hrdata are valid HERE,
             │          │          │          │      ONE CYCLE AFTER the address.
             │          │ ADDRESS  │          │
             │          │ phase B  │          │   <- and B's address is already on
                                                    the bus while A's data is.
```

So:

- **`haddr` / `hwrite` / `hsize` / `htrans` are valid in the ADDRESS phase.**
  **`hwdata` and `hrdata` are valid in the DATA phase — the *next* cycle.**
  If you decode `haddr` and use `hwdata` in the *same* cycle, you have used the
  **previous** transfer's write data. This is the classic bug.
- You must therefore **register** the address-phase information and act on it in the
  next cycle:

  ```systemverilog
  // Capture the address phase. hready is the GLOBAL bus ready: it says "the bus is
  // completing the previous transfer THIS cycle, so the address on the bus now is
  // real". Only sample when hready is high.
  logic        wr_q, sel_q;
  logic [15:0] addr_q;

  // SYNCHRONOUS reset (reset only sampled on the clock edge). The shared
  // clcd_core you instantiate in §7 is sync-reset, and every block in this
  // platform is; mixing an async-reset front end with it trips verilator's
  // SYNCASYNCNET (-Wall, which §11 requires) and leaves the two halves of your
  // block exiting reset in different cycles. Keep the whole accelerator
  // sync-reset.
  always_ff @(posedge hclk) begin
    if (!hresetn) begin
      sel_q  <= 1'b0;
      wr_q   <= 1'b0;
      addr_q <= '0;
    end else if (hready) begin
      // htrans[1] is 1 for NONSEQ(10) and SEQ(11) -- a real transfer.
      // It is 0 for IDLE(00) and BUSY(01) -- nothing is happening.
      sel_q  <= hsel && htrans[1];
      wr_q   <= hwrite;
      addr_q <= haddr[15:0];      // decode as many bits as your block needs
    end
  end

  // Now, in the DATA phase, sel_q/wr_q/addr_q describe the transfer whose hwdata
  // is on the bus RIGHT NOW.
  wire do_write = sel_q &&  wr_q;   // hwdata is valid this cycle
  wire do_read  = sel_q && !wr_q;   // you must present hrdata this cycle
  ```

- **`hsel`** is your chip select — the bus matrix asserts it when `haddr` lands in
  your region. **`hsel` alone is not enough**: you must also see `htrans[1]`
  (a NONSEQ or SEQ transfer) **and** `hready` (the bus is actually advancing).
  `hsel && htrans == IDLE` means *"the bus is pointed at you but doing nothing"* —
  ignore it.
- **`hready` (input) is the GLOBAL bus ready**, not yours. The bus matrix drives it
  from whichever slave is currently selected. It tells you when the address phase
  on the bus is real.
- **`hreadyout` (output) is *your* ready.** `1` = "I have finished this data phase".
  See §4.
- **`hsize`.** The M0 will issue byte (`000`), halfword (`001`) and word (`010`)
  accesses. If your registers are word-only, the simplest correct thing is to
  **ignore `hsize` and treat everything as a word access** — that is what most CMSDK
  peripherals do. Just document it.
- **`hburst` / `hprot` / `hmastlock`** — you may ignore all three. A single-beat
  slave is entirely legal on AHB-Lite. (Read them if you want to optimise bursts.)
- **`hresp`** — drive `0` (OKAY). A two-cycle ERROR response is legal but is not
  needed here and is easy to get wrong; unmapped offsets inside your region should
  simply **read `0` and ignore writes**, which is what every block in this platform
  already does.

---

## 4. 🚨 THE ONE RULE: `hreadyout` MUST ALWAYS, EVENTUALLY, GO HIGH 🚨

**If `hreadyout` never asserts, the AHB bus stalls, the Cortex-M0 stalls, and the
whole SoC is dead.** No exception fires, no watchdog barks. The board just stops.
This is the number-one thing students get wrong, and here it does not merely produce
a wrong answer — **it hangs the CPU.**

Concretely:

- **Reset `hreadyout` to `1`, not `0`.** A slave that comes out of reset with
  `hreadyout = 0` and is then *never* selected will still hang the bus the moment
  the matrix routes `hready` through it.
- **When you are not selected, drive `hreadyout = 1`.** Always. Unconditionally.
  A deselected slave must never hold the bus.
- **If you insert wait states, they must be bounded.** `hreadyout = !my_fifo_full`
  is a **trap**: if your FIFO drains only when some *other* thing happens — and that
  other thing is the CPU, which is now stalled — you have built a deadlock. This
  platform's shell CLCD block hit exactly this design question and chose the other
  answer: **writes into a full FIFO are DROPPED, never stalled**
  (`fpga/shell/ip/clcd/clcd.sv:31-36`). **Do the same.** Publish a `STATUS.full` bit
  and let the CPU poll it. **Do not back-pressure the bus on a FIFO.**
- The simplest correct block is a **zero-wait-state** one: `assign hreadyout = 1'b1;`
  and everything completes in its data phase. Start there. Add wait states only when
  you know why you need them.

The bar is low and absolute: **a bounded number of cycles after `hsel && htrans[1]
&& hready`, `hreadyout` must be `1` for one cycle with valid `hrdata` (if a read).**

> **Background (why this is worse than usual here):** the `exp_*` port is a real
> hole in the bus matrix. Today the RM wrapper ties it off so that reads return `0`
> and never stall (`nanosoc_m0_soc/pynq/vivado_ip/nanosoc_vivado_wrapper.v:37`).
> The moment you put a live slave there, the tie-off is gone and your `hreadyout` is
> the only thing keeping the matrix moving.

---

## 5. Your address space

| Region | Size | Notes |
|---|---|---|
| **`0x6000_0000` – `0x6FFF_FFFF`** | **256 MB** | **Yours.** This is exactly what the bus matrix decodes to `exp_*` (`nanosoc_matrix_decode_CPU_0.v:362`, `…_DMAC_0.v:299`, `…_DMAC_1.v:273`: *"Address region 0x60000000-0x6fffffff"*). |

> ⚠️ **`nanosoc.sv`'s port comments say the expansion region is
> `0x60000000-0x7FFFFFFF`. That comment is WRONG** (a stale generator string). The
> **decoder** says `0x6000_0000-0x6FFF_FFFF`, and `0x7000_0000+` belongs to the QSPI
> flash (`NANOSOC_QSPI_MEM_BASE = 0x70000000`, `qspi_ctrl` at `0x7400_0000`).
> **Never respond above `0x6FFF_FFFF`.** Decode only the low bits you need
> (16 bits is plenty for a register block) and treat the rest of the region as
> reads-`0`/writes-ignored.

You have 256 MB of address space for what will be a handful of registers. Put your
registers at the bottom (`0x6000_0000`, `0x6000_0004`, …) and alias the rest; that
is what the reference block does.

C side (add to your DUT firmware — the platform header does not define it yet):

```c
#define EXP_BASE   0x60000000UL
```

---

## 6. The display pins — semantics, restated locally

The full wire-level encoding is `docs/contracts/dut-display-tunnel.md` (**read it if
you touch the RM wrapper**; you do not need it to write the socket). What you need
here:

The panel is a **Himax HX8347-D** on an **8-bit 8080 parallel** bus. You send it a
stream of **`{RS, byte}`** pairs. `RS = 0` means the byte is a **command** (a
register index); `RS = 1` means it is **data** (a register value, or a pixel byte).
That is the entire protocol from your side. The panel has its own on-chip frame
memory (GRAM), **so the panel *is* the framebuffer** — you do not need a BRAM
framebuffer, video timing, or a DMA engine to put a picture on the screen.

One byte = one **8080 write cycle**, three phases:

```
        |<-- CS_SETUP -->|<--- WR_LO --->|<--- WR_HI --->|
lcd_cs  ____/‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾\____   asserted (1) for
lcd_wr  ______________________/‾‾‾‾‾‾‾‾‾‾‾\___________________   the WHOLE cycle
lcd_rs  ‾‾‾‾<─────────── stable for the whole cycle ────────>
lcd_pd  ‾‾‾‾<─────────── stable for the whole cycle ────────>
                                          ▲
                       the panel latches the byte HERE
                       (at the pad, on WR's RISING edge — but the pad is
                        active-LOW, so that is your lcd_wr FALLING edge)
```

- **All of `lcd_cs`, `lcd_wr` are ACTIVE-HIGH here.** The shell inverts them to the
  panel's active-low pads. **Do not invert them yourself.** *(§3 of the tunnel
  contract explains why: the DFX decoupler clamps these wires to `0` during a
  partial reconfiguration, and active-high means `0` decodes to "all strobes idle",
  by construction. Inverting them turns a safe clamp into "chip selected, both
  strobes asserted, for the whole reconfiguration" — a panel-corrupting bug that no
  simulation of your block alone will catch.)*
- **Deassert `lcd_cs` between bytes.** The shell block does (one `CS` pulse per
  byte, board-proven — `docs/CLCD_PANEL_FACTS.md` §6), and the KVM's safe-switch
  logic **relies** on it: `!lcd_cs && !lcd_busy` is the moment it is allowed to take
  the panel away from you without cutting a cycle in half. A block that holds `CS`
  asserted across a whole burst will be preempted by the 1 ms timeout instead —
  it still works, but the handover is no longer clean.
- **`lcd_busy` = "I have a byte in flight **or** queued"** — i.e.
  `(fsm != IDLE) || !fifo_empty`. Get this right; the KVM uses it to decide when it
  is safe to switch.
- **`lcd_req`**: pulse/hold it high to ask for the panel; drop it to hand it back.
  The shell samples it as an **edge**. It only works at all if the harness firmware
  has enabled it (`CLCDKVM.CTRL.dut_req_en`, off by default). **The button always
  works regardless**, so do not depend on `lcd_req`.
- **`lcd_en`**: `1` = **your block** drives the display pins onto the tunnel; `0` =
  the RM wrapper routes the M0's raw GPIO bits instead, for the Tier-0 bit-bang
  exercise (§8). It **must reset to `0`** so the socket is transparent at power-on
  and Tier 0 works before any accelerator is enabled. Drive it from your enable bit
  — the reference block uses `assign lcd_en = ctrl_enable;` (`CTRL[0]`, resets 0),
  so firmware writing `CTRL.enable=1` is what makes the accelerator take over. A
  bare `assign lcd_en = 1'b1;` would seize the tunnel at reset and **break Tier 0** —
  do not do that in a block that shares the ladder.

### Timing floor — NORMATIVE

The tunnel is a **clock-domain crossing**: you drive it at `hclk` (50 MHz), the
shell samples it at 100 MHz. The shell filters the crossing so it can never see a
half-updated vector — but only if you respect this:

> **Every phase (`CS_SETUP`, `WR_LO`, `WR_HI`) lasts ≥ 8 `hclk` cycles.
> `lcd_pd` and `lcd_rs` are stable ≥ 4 `hclk` cycles before `lcd_wr` rises and
> ≥ 4 `hclk` cycles after it falls.**

At 50 MHz that is ≥160 ns per phase. Use `CS_SETUP = WR_LO = WR_HI = 8` and you get
~500 ns/byte ≈ 2 MB/s (25 `hclk` cycles/byte at the 8/8/8 floor — the FSM passes
through IDLE for ≥1 cycle between bytes) — a full-screen 320×240×2 = 150 KB repaint
in **~75 ms**.
Plenty. Do not try to go faster; the panel does not need it and the CDC does.

**You will not be told when you own the panel.** There is no grant wire back into
the RM (`dut-display-tunnel.md` §6 — the shell→DUT GPIO bits are all taken by the
DIP switches and the LED loopback). You do not need one: **every handover
hard-resets the panel**, so the correct driver design is to **re-initialise and
repaint on a periodic loop, unconditionally**. Bytes you emit while the harness owns
the panel are silently discarded. Your picture appears within one refresh period of
the button being pressed, and you never have to reason about who owns what.

---

## 7. The reference accelerator — `ahb_clcd` (read it as a worked example)

`fpga/rp/nanosoc_exp/ahb_clcd.sv` (built by W2-D) is a complete, working
implementation of this socket. It is the thing you can read, run, and then replace.

**Structure** — and this is the trick worth stealing:

```
   ahb_clcd.sv  =  [ AHB-Lite slave front end ]  +  [ clcd_core.sv ]
                     (the pipeline of §3)            (FIFO + 8080 strobe FSM)
                                                            ▲
   clcd.sv      =  [ AXI4-Lite slave front end ] ───────────┘
   (the shell's                                    THE SAME CORE
    status screen)
```

`clcd_core.sv` is the `{RS,byte}` FIFO plus the three-phase 8080 strobe FSM, **lifted
out of the shell's `clcd.sv` unchanged** — the code that is already lighting the
panel on the bench today. `ahb_clcd` therefore **inherits the shell block's proof**
and its cocotb panel model. Only the bus front end is new.

**Registers** (base `0x6000_0000`). Similar in *shape* to the shell CLCD block,
but **not register-compatible** — and deliberately so. This block owns neither the
backlight nor the panel reset (the shell KVM does — §1), so it has no `bl`/`reset_n`
bits and no read-back path, and `TIMING` sits at `0x10` (the shell block's `0x14`,
because it has a `READ` register at `0x10` that this one does not). A driver author:
**write this block's driver against the map below, not against the shell's.**

| Off | Reg | Access | Bits |
|---|---|---|---|
| `0x00` | `CTRL` | RW | `[0]` enable, `[1]` fifo_reset (self-clearing), `[2]` req (drive `lcd_req`) |
| `0x04` | `CMD` | W | `[7:0]` byte → push `{RS=0, byte}` |
| `0x08` | `DATA` | W | `[7:0]` byte → push `{RS=1, byte}` |
| `0x0C` | `STATUS` | RO | `[0]` fifo_full, `[1]` fifo_empty, `[2]` busy, `[15:8]` fifo_level |
| `0x10` | `TIMING` | RW | `[7:0]` wr_lo, `[15:8]` wr_hi, `[23:16]` cs_setup — in **`hclk`** cycles |

**Writes to `CMD`/`DATA` when the FIFO is full are DROPPED, not stalled** — see §4.
Firmware polls `STATUS.fifo_full`. `hreadyout` is `1'b1`, always.

The panel's actual **register values** (the HX8347-D init sequence, the GRAM window,
the pixel format) are **not in the RTL** — they are a firmware data table with its
own provenance record (`firmware/clcd/hx8347_init.c`, `firmware/clcd/PANEL_PROVENANCE.md`).
Your block is **protocol-agnostic**: it knows about 8080 bus cycles and nothing about
Himax registers. That is the whole point of the split, and it means fixing the panel
table never touches your RTL.

**Size:** ~300–600 LUT. A lab session's worth of work.

### The exercise ladder

| Tier | What you write | Uses |
|---|---|---|
| **0** | *nothing* — bit-bang the tunnel from C (§8) | proves the whole path with zero RTL |
| **1** | the **AHB-Lite front end**; you are given `clcd_core.sv` | §3, §4 |
| **1b** | the front end **and** the 8080 FSM | + the strobe timing of §6 |
| **2** | an **RGB565 pixel packer** — write pixels, not bytes | |
| **2b** | a **hardware rectangle fill** — write a colour and a window, get a filled box | |
| **3** | raise `irq[0]` on "FIFO not full" and drive the block from an **interrupt handler** instead of a poll | `EXP0_IRQn` |
| **3b** | assert `drq[0]` and have **DMAC 0 blit a framebuffer** out of SRAM with **no per-pixel CPU writes** | the `exp_drq` hook |

Tier 3b is the interesting one, and it is the reason this socket is `exp_*` and not
some free peripheral slot: `EXP_IRQ[3:0]` and `EXP_DRQ[1:0]` are **already wired**
into the NVIC and the DMA controller. Nothing else on this chip gives you that for
free.

---

## 8. Tier 0 — bit-bang the panel from C, with **no RTL at all**

`dut_gpio_o` / `dut_gpio_oe` are wired 1:1 to nanosoc's CMSDK **GPIO port 0**
(`rp_nanosoc_wrapper.sv:224-229` — `p0_out_w` / `p0_outen_w`). When the socket drives
`lcd_en = 0`, the RM wrapper routes GPIO P0's **upper byte** onto the tunnel. So the
M0 can drive the panel **directly from C**, the day the KVM lands.

The mapping is deliberately strange and you must read it carefully:

| Tunnel bit | Comes from | C access (`CMSDK_GPIO0`, base **`0x4001_0000`**) |
|---|---|---|
| `PD[7:0]` | `p0_out[15:8]` | `CMSDK_GPIO0->DATAOUT` bits `[15:8]` (offset `0x004`) |
| `cs`, `wr`, `rs`, `busy`, `req` … | `p0_outen[15:8]` | `CMSDK_GPIO0->OUTENABLESET` / `->OUTENABLECLR` bits `[15:8]` (offsets `0x010` / `0x014`) |

**Yes: the control bits live in the GPIO's *output-enable* register, not its data
register.** That is not a mistake — the tunnel is an overload of *two* 16-bit
vectors (`dut_gpio_o` **and** `dut_gpio_oe`), and the upper half of the
output-enable vector has no pad either, so it carries the control byte. Bit
positions are in `docs/contracts/dut-display-tunnel.md` §2 and are **the same** for
bit-banging as for RTL.

```c
#define GPIO0_DATAOUT   (*(volatile uint32_t *)0x40010004u)
#define GPIO0_OUTENSET  (*(volatile uint32_t *)0x40010010u)
#define GPIO0_OUTENCLR  (*(volatile uint32_t *)0x40010014u)

/* control bits, in the OUTEN vector (see dut-display-tunnel.md §2) */
#define LCD_CS  (1u << 8)
#define LCD_WR  (1u << 9)
#define LCD_RS  (1u << 10)

static void lcd_byte(unsigned rs, unsigned char b)
{
    GPIO0_DATAOUT = (GPIO0_DATAOUT & 0x00FFu) | ((unsigned)b << 8);   /* PD */
    if (rs) GPIO0_OUTENSET = LCD_RS; else GPIO0_OUTENCLR = LCD_RS;
    GPIO0_OUTENSET = LCD_CS;   /* CS asserted  (active-HIGH on the tunnel) */
    GPIO0_OUTENSET = LCD_WR;   /* WR asserted  */
    GPIO0_OUTENCLR = LCD_WR;   /* WR released -> the panel latches the byte */
    GPIO0_OUTENCLR = LCD_CS;   /* CS released  */
}
```

Each AHB write is many `hclk` cycles, so the timing floor of §6 is satisfied
*trivially* — you cannot bit-bang too fast. Note the socket never asserts `lcd_busy`
in this mode, so the KVM sees the DUT as always quiescent and a handover is always
clean.

**Honest about the speed:** a byte costs ~5 AHB writes, so a full 150 KB repaint is
**seconds**. That is fine — the right first milestone is *"press the button and the
DUT's text appears on the panel"*, and it is a legitimate memory-mapped-I/O and
bus-timing exercise in its own right. It also **de-risks the KVM with no student RTL
in the loop**.

---

## 9. How to build and test it

### In simulation (do this first — it is where you will find your bugs)

```bash
cd tests/nanosoc_lcd && make          # your block + the real HX8347-D panel model
```

`tests/nanosoc_lcd/` (built by W2-E) drives your AHB-Lite slave with the
`AhbLiteMaster` BFM (`tests/common/ahb_lite.py`, built by W2-B) and watches the
display pins with **`tests/clcd/clcd_panel_model.py`** — the *same* cocotb monitor
that verifies the shell's CLCD block. It plays an HX8347-D on the 8080 bus, latching
`{RS, byte}` on each write strobe. **If the panel model decodes your byte stream,
the real panel will too.**

Start by copying `tests/clcd/` — its five-test structure, its `bind_*.sv`
SVA-checker pattern, `bench_common.mk` and the `dut_notes.md` convention all
transfer directly.

The whole feature is also proven end-to-end in simulation before any hardware:
shell KVM + tunnel + decoupler + your socket + the panel model, in one bench
(Wave 3). **You should never need a board to find a logic bug.**

### On the board

Your accelerator is **compiled into the nanosoc RM**. The socket is **static** — it
is not itself a reconfigurable partition (that was evaluated and rejected: see
`docs/CLCD_KVM_WAVE_PLAN.md` W0-B. Nesting would only make the *build* faster; it
changes nothing about what you write, run or see).

So the loop is: edit your RTL → rebuild the `rm_nanosoc` partial → load it. The
build is the existing OOC-synth + DFX implementation flow:

```bash
make -C fpga/dfx rm-nanosoc-dcp      # OOC synth of the RM (your block included)
make -C fpga/dfx rm-nanosoc          # implement -> partial bitstream
make check                            # the 8-stage gate; must be green
```

**You never rebuild the shell.** The shell (and therefore the KVM, the panel pads
and the button) is static and already on the board. Only your partial changes.

> ⚠️ **Known blocker (not caused by this work):** as of 2026-07-14 the upstream
> `~/SoCLabs/nanosoc_m0_soc` **working tree does not compile** —
> `build_soc/rtl/nanosoc.sv` was regenerated with a syntax error and a QSPI stack
> its `pynq/filelist.tcl` does not list. See `docs/CLCD_KVM_WAVE_PLAN.md`
> ("Blocking, and not caused by this project"). `make rm-nanosoc-dcp` will fail
> until that is fixed or pinned.

---

## 10. Background — why the socket needed a fix before it worked

*(You can skip this. It matters only if you are debugging the RM wrapper.)*

The `exp_*` port on `nanosoc.sv` is, internally, a **correct AHB-Lite master** — the
bus matrix drives it and expects a slave on the other end. But the code generator
promoted the region's port to the chip boundary **without inverting the directions**,
so the *keywords* on `nanosoc.sv` say `input wire exp_hsel`, `input wire exp_haddr`,
… and `output wire exp_hreadyout` — the exact opposite of the truth. (This is
logged as bug **G3** in `docs/NANOSOC_INTEGRATION_GAPS.md:136`.)

It elaborates today only because Vivado *tolerates* the mismatch (10 ×
`[Synth 8-6104]` "input port has an internal driver" + 3 × `[Synth 8-3848]` "net has
no driver"). The consequence: **`exp_hreadyout` is a constant `0`, so any CPU access
to `0x6000_0000` would hang the bus** — nobody had hit it because no firmware ever
touched the region.

The fix is **13 port-direction flips and nothing else** — no logic, no new signals,
no YAML, no regeneration — proven by elaboration to produce **0 errors, 0 critical
warnings, and 122 top-level ports before *and* after** (⇒ **zero** partition-pin
change). It is applied at **build time**, by a regex in `ooc_synth.tcl` that patches
the generated `nanosoc.sv` into the build directory (W3-0). **The upstream tree is
read-only, lab-shared, and is never edited** — and the generated file demonstrably
drifts, so it is never vendored either.

After the fix, `exp_*` is an AHB-Lite **master** on `nanosoc` — which is why the
module you write is an AHB-Lite **slave**.

---

## 11. Files

| Path | What | Owner |
|---|---|---|
| `fpga/rp/nanosoc_exp/README.md` | **this file** — the frozen socket contract | W1 |
| `fpga/rp/nanosoc_exp/nanosoc_exp_socket.sv` | the socket shell — **your RTL goes here** | W2-D (reference) |
| `fpga/rp/nanosoc_exp/ahb_clcd.sv` | the reference accelerator | W2-D |
| `fpga/rp/nanosoc_exp/clcd_core.sv` | FIFO + 8080 FSM, shared with the shell's `clcd.sv` | W2-D |
| `tests/nanosoc_lcd/` | the bench for your block | W2-E |
| `tests/common/ahb_lite.py` | the `AhbLiteMaster` BFM | W2-B |
| `fpga/rp/nanosoc/rp_nanosoc_wrapper.sv` | instantiates the socket; muxes the tunnel | W3-A |
| `docs/contracts/dut-display-tunnel.md` | the wire encoding out to the shell | W1 |
| `fpga/shell/ip/clcd_kvm/README.md` | the shell-side arbiter | W1 |
| `docs/CLCD_PANEL_FACTS.md` | the panel that is actually lit, and what is still unproven | W0-C |
