# `lcd_mirror`: LCDMIR, the pixel-exact LCD mirror snooper

> **Status: RTL and bench complete. Not integrated.** Nothing here is wired into
> `shell_bd.tcl`, the regmap, the DTS or the packaged-IP list yet. That is the
> **mint-4** step (§8), because the block is static and any static change
> re-keys `static_id`.
>
> Contract: `docs/planning/linux_lanes/LCD_MIRROR_FPGA.md` (§2 snooper, §3
> storage, §4 register map, §8 sim plan, §9 open items). Harness Manager's side:
> `harness-manager` `docs/design/LCD_MIRROR.md` (§4.2 compare-on-write, §11
> H1-H4). Golden model: `tests/lcd_mirror/hx8347_gram_model.py`.
> Bench: `tests/lcd_mirror/` (§6).

## 1. What it is

- **An input-only tap** on the nine pad-side nets `clcd_kvm_0` drives, plus
  `owner_o`. It drives nothing, so the proven panel path (`clcd`, `clcd_kvm`,
  `tests/clcd*`) is unchanged.
- **One clock, no CDC.** The pads are a combinational mux of `s_axi_aclk`
  registers, so the snooper samples them on the same 100 MHz clock
  (`LCD_MIRROR_FPGA.md` §1.2).
- **An HX8347-D GRAM model.** It decodes every 8080 write cycle (the WR_n rising
  edge with CS_n low). It models index 0x22, the window registers 0x02-0x09,
  MADCTL 0x16, COLMOD 0x17, auto-increment and wrap. It records all 256
  registers raw.
- **A frame buffer.** 320x240 RGB565, stored in **viewer order** (the proven
  MADCTL 0x20 is the identity).
- **Readable state on AXI4-Lite.** 16x16 dirty and VALID tile maps with an
  atomic SNAP, counters, timing guards, and the frame buffer itself.

## 2. Files

| File | What |
|---|---|
| `lcd_mirror.sv` | Top. AXI4-Lite slave (Xilinx-template write channel; a latency-tolerant read channel), CSRs, tile maps, bounding box, counters, the two frame-buffer RAMs, the REGS LUTRAM |
| `lcd_mirror_core.sv` | Tap registers, the VIOL/RDS guards, the HX8347-D model, and the MADCTL-to-viewer and address pipeline (fully pipelined: one pixel per cycle) |
| `lcd_mirror_ram.sv` | 38,400 x 16 block RAM, instantiated twice (even and odd pixels). SDP, or TDP with a read-first port A when `OLD_DATA=1` (compare-on-write) |
| `lcd_mirror_ooc.xdc` | Out-of-context timing (100 MHz) |
| `ooc_synth.tcl` | OOC synth + place + route + reports on `xcku115-flvb1760-1-c` |

## 3. Ports

| Port | Dir | Connect to (mint 4) |
|---|---|---|
| `s_axi_aclk`, `s_axi_aresetn` | in | `shell_clk`, `shell_aresetn`: join the `foreach csr` list (`shell_bd.tcl:700`) |
| `s_axi_*` | slave | `axi_interconnect_0` M21 (see §8) |
| `lcd_pd_i[7:0]` | in | `clcd_kvm_0/clcd_pd_o` (fan-out) |
| `lcd_cs_n_i`, `lcd_wr_n_i`, `lcd_rs_i`, `lcd_rd_n_i` | in | `clcd_kvm_0/clcd_{cs_n,wr_n,rs,rd_n}_o` |
| `lcd_rst_n_i`, `lcd_bl_i` | in | `clcd_kvm_0/clcd_{rst_n,bl}_o` |
| `lcd_owner_i` | in | `clcd_kvm_0/owner_o` (unconnected today; this becomes its first load) |

## 4. Parameters

| Parameter | Default | Meaning |
|---|---|---|
| `C_S_AXI_ADDR_WIDTH` | 18 | The BD sets **32**. Only the block's own 18 (banked: 16) low bits are decoded: the csr_decode_width lesson |
| `BANKED` | 0 | 0 = the 256 KiB map at `0x44B8_0000`. 1 = the §4 fallback: one 64 KiB page, frame buffer through a 32 KiB window selected by `FB_BANK` |
| `DIRTY_CMP` | 1 | Build compare-on-write dirty tracking (HM §4.2, H4). 0 = plain dirty-on-write |
| `TMIN_INIT` | `0x01_02_02` | TMIN reset value: guard 1, WR-high 2, WR-low 2 cycles |
| `RST_SC/EC/SP/EP`, `RST_R01/16/17/1F/28/36` | 0 / 239 / 0 / 319, `00/00/06/01/00/00` | Register values after CLCD_RST (open item O3, §7). The golden model's `DEFAULTS` holds the same values |

## 5. Register map (`LCDMIR` @ `0x44B8_0000`)

This is `LCD_MIRROR_FPGA.md` §4 plus the items marked **new**. Reads never have
side effects; every action is armed by a write.

| Offset | Name | Access | Fields |
|---|---|---|---|
| `0x000` | ID | RO | `0x4C43_444D` ("LCDM") |
| `0x004` | VERSION | RO | `0x0100_1001`: v1.0, tile 16, fmt 1 (RGB565) |
| `0x008` | GEOM | RO | `0x00F0_0140`: H 240, W 320 |
| `0x00C` | CTRL | RW | [0] `ac_load` (O1), [1] `flip_conv` (O2), **[2] `dirty_all`** (new: 0 = compare-on-write, 1 = dirty on every write; reads 1 when `DIRTY_CMP=0`), [8] SNAP (W1P), [9] clr_sticky (W1P), [10] clr_counts (W1P). Reset 0 |
| `0x010` | STATUS | RO | [0] rst_n, [1] bl, [2] owner, [3] display_on (R28 & 0x3C == 0x3C), [4] standby (R1F[0]), [5] in_gram, [6] fmt_ok (COLMOD[2:0] is 5 or 6), [7] approx (18 bpp), [8] viol\*, [9] oob\*, [10] rd_seen\*, **[15] banked** (new, constant). \* = sticky |
| `0x014` | SEQ | RO | Every in-range pixel write (the seqlock). Never cleared by clr_counts |
| `0x018`-`0x030` | FRAMES, RAMWR, RESETS, BYTES, VIOL, OOB, RDS | RO | Event counters, 32-bit, wrap. clr_counts zeroes them; a clear wins over a same-cycle count |
| `0x034` | TMIN | RW | [7:0] WR-low, [15:8] WR-high, [23:16] PD/RS guard, in cycles. Honours WSTRB |
| `0x038` | FB_BANK | RW | **Banked build only** (new offset; the doc names the register but gives no offset). [2:0] bank 0-4. Reads 0 when unbanked |
| `0x040`, `0x044` | WIN_X, WIN_Y | RO | {EC, SC}, {EP, SP}: 9 bits at [24:16] and [8:0] |
| `0x048` | AC | RO | {y, x}, same packing |
| `0x04C` | MODE | RO | [7:0] R16, [15:8] R17, [23:16] R36, [31:24] R01. **Effective values**: back to the defaults on CLCD_RST |
| `0x050` | SNAP_SEQ | RO | SEQ at the last SNAP |
| `0x054`, `0x058` | SNAP_BBOX_X, _Y | RO | {max, min} at [24:16] / [8:0]. Empty = min `0x1FF`, max 0 |
| `0x080`-`0x0A4` | DIRTY[0..9] | RO | Snapshot; bit t = tile ty*20+tx |
| `0x0C0`-`0x0E4` | VALID[0..9] | RO | Live: tile written since the last CLCD_RST |
| `0x100`-`0x1FF` | REGS[0..63] | RO | Byte i = last datum written to index i. **A raw log: never reset** (§7) |
| `0x1_0000`-`0x3_57FF` | FB | RO | 38,400 words; `px[vy*320+vx]`, 2 px per word, even x in [15:0] (a `uint16_t[]` read works as-is) |

Unmapped offsets read 0 and ignore writes. Writes to RO registers and to the
frame buffer are ignored (BRESP=OKAY).

**Read latency.** RVALID rises three cycles after ARREADY for every address,
CSR or frame buffer. A read is accepted only when none is in flight.

## 6. Verification (`tests/lcd_mirror/`)

`source set_env.sh`, then `make -C tests/lcd_mirror all-modes`, or one at a time:

| Target | What | Result |
|---|---|---|
| `make pytest` | Golden model vs independent oracles, and vs HM's `Hx8347dShadow` (no simulator) | **13/13 PASS** |
| `make` (MODE=direct) | 15 cocotb tests (VCS 2022.06-SP2, cocotb 2.0.1): the snooper alone, whole frame and every CSR vs the model after every stream | **15/15 PASS** (~4 min) |
| `make MODE=banked` | The 64 KiB banked fallback | **2/2 PASS** |
| `make MODE=e2e` | Through the REAL `clcd_kvm` + tunnel + `ahb_clcd` + `clcd_core` (`tests/clcd_kvm_e2e/tb_clcd_kvm_e2e.sv` reused as-is): handovers both ways and a DFX forced revert | **2/2 PASS** (~8 min: two per-cycle Python models) |
| `make mutants` | Six broken RTL copies. Each must make its named tests FAIL (off-by-one wrap, MX/MY swap, byte order, SNAP not clearing, a removed guard, compare-on-write disabled) | **6/6 killed** |

**What the checks are made of:**

- The pad BFM executes stimulus words. `pad_decoder.py` expands the same words
  into the identical per-cycle trace, then decodes it with the RTL's
  tap-and-guard rules. So the expected VIOL/RDS counts come from the trace the
  RTL actually sees.
- **Independent oracles**, so the model cannot pass its own mistakes:
  - **Harness streams:** the `clcd.c` cell shadow, font-rendered in Python from
    `firmware/clcd/font8x16.h`. The streams come from two separate recordings:
    `tools/clcd_stream_dump.c` (this bench) and HM's `hm_fixtures/`.
  - **clcd_demo:** `card_model.card_pixel()`.
  - **nanosoc demo:** HM's rectangles.
- **Whole-frame readback** uses an SV AXI master (`tb_axil_dump.sv`) on the
  real slave port. The cocotb BFM spot-checks it.
- The shared AXI4-Lite SVA checker is bound on the slave for every run.

## 7. Decisions and deviations from `LCD_MIRROR_FPGA.md`

1. **Compare-on-write dirty tracking (H4), built and on by default.**
   - A tile or the bbox is marked only when a write changes the stored pixel.
     VALID and SEQ still count every write.
   - Cost: the frame RAMs go true-dual-port. Port A becomes read-first, so each
     write returns the word it replaced two cycles later. No extra BRAM.
   - Measured on the RTL: clcd_demo A→B marks exactly the tiles where
     `card_pixel` differs; B→B marks 0 (dirty_all marks 300). HM's history
     reproduces HM's published 179/67/269/291/12/300/297 on the RTL and in the
     model.
   - `CTRL[2]=1` gives back the doc's dirty-on-any-write.
2. **O1** (`CTRL.ac_load`), **O2** (`CTRL.flip_conv`): both conventions are
   implemented, default 0, as the doc proposes.
   - HM's model reloads AC on 0x22 **and** on start-register writes. For pixels
     that equals `ac_load=1`.
3. **O3 register defaults are assumptions.** Window = portrait-native 240x320
   (a fact of the glass), MADCTL 0x00, COLMOD 0x06 (18 bpp), R01 0, R1F 0x01
   (STB=1), R28 0, R36 0. HM assumes R1F = 0. Pinned by `test_reset_state`;
   change `RST_*` and `DEFAULTS` together once the Himax datasheet is read.
4. **REGS is a raw log and is not reset** by CLCD_RST or by a shell reset
   (LUTRAM). MODE and STATUS carry the effective, reset-aware values. **HM and
   harnessd must take R16/R17/R36/R01 from MODE, not from REGS, after a RESETS
   change.**
5. **Edge rules the doc leaves open** (golden model = RTL; HM differs on these):
   - **AC wrap:** on `x == EC`. A start beyond the end walks through 511.
   - **COLMOD:** keyed on IFPF = R17[2:0]. Any format other than 5 or 6 is still
     assembled as 16 bpp, with `fmt_ok = 0`.
   - **Before any index:** a datum with no index yet goes to REGS[0] (idx resets
     to 0).
6. **VIOL is exact about what it counts.**
   - It counts at most one per cycle, for any of: WR-low < TMIN.lo; WR-high <
     TMIN.hi; PD/RS setup < TMIN.guard at the WR fall; PD/RS moving while WR is
     low; PD/RS hold < TMIN.guard after the WR rise; CS rising while WR is low.
   - A byte is still committed on the rise if CS was low in the last WR-low
     cycle, exactly the doc's rule.
   - Nothing is counted or decoded while CLCD_RST is low.
7. **clr_counts leaves SEQ/SNAP_SEQ alone.** SEQ is the seqlock software
   compares against.
8. **Resources and timing** (OOC, `ooc_synth.tcl`, Vivado 2026.1, xcku115-flvb1760-1-c,
   100 MHz, `DIRTY_CMP=1`, width 32): **1,647 LUT (40 LUTRAM), 1,950 FF, 40 RAMB36;
   routed WNS +1.972 ns, WHS +0.048 ns**, every constraint met. The worst path is
   the AXI write decode into the bbox clock enables, from an input port given a
   5 ns budget.
9. **BRAM count is 40 RAMB36, not 38.** Vivado tiles 38,400 x 16 as 4K x 9
   (2 x 10 per half) rather than 2K x 18 (19). If BRAM gets tight, 19 explicit
   2,048-word banks per half with a registered 19:1 read mux gets 38 back.

**Notes for the harnessd service (lane LCDMIR-SW):**

- **CTRL byte 0 holds configuration.** Write SNAP/clr as `WSTRB=0b0010`, or
  read-modify-write. A plain `CTRL=0x100` with full strobes also rewrites
  `ac_load`, `flip_conv` and `dirty_all` to 0. That is harmless with the
  defaults, because 0 is the preferred value of all three.
- **Dirty means changed** by default, so the "skip if unchanged" re-read of
  §6.1 step 3 almost never skips.

## 8. Mint-4 integration (not done here)

One BD delta, coordinated with D11/D6/D8/D10:

1. **Package the IP.** In `fpga/shell/ip_packaged/package_csr_ip.tcl`:
   `dict set blocks lcd_mirror [list lcd_mirror [list <ip>/lcd_mirror_ram.sv <ip>/lcd_mirror_core.sv <ip>/lcd_mirror.sv]]`.
   All three files must be in the fileset (the `clcd` + `clcd_core` lesson).
2. **Add the cell** to `fpga/shell/bd/shell_bd.tcl`, next to `clcd_kvm_0`:
   ```tcl
   set lcd_mirror_0 [create_bd_cell -type ip -vlnv soclabs.org:user:lcd_mirror:1.0 lcd_mirror_0]
   set_property CONFIG.C_S_AXI_ADDR_WIDTH {32} $lcd_mirror_0
   ```
   Then add `$lcd_mirror_0` to the `foreach csr [list ...]` clock/reset list
   (`shell_bd.tcl:700`). That puts it on `shell_aresetn`, so it survives CPU and
   WDOG resets like the other CSR blocks.
3. **Wire the tap** after the "KVM -> the panel pads" block (`~shell_bd.tcl:1055`).
   These are fan-outs; the existing pad connections stay:
   ```tcl
   foreach {src dst} {clcd_pd_o lcd_pd_i  clcd_cs_n_o lcd_cs_n_i  clcd_wr_n_o lcd_wr_n_i
                      clcd_rs_o lcd_rs_i  clcd_rd_n_o lcd_rd_n_i  clcd_rst_n_o lcd_rst_n_i
                      clcd_bl_o lcd_bl_i  owner_o lcd_owner_i} {
       connect_bd_net [get_bd_pins $clcd_kvm_0/$src] [get_bd_pins $lcd_mirror_0/$dst]
   }
   ```
4. **Add the interconnect port.**
   - `NUM_MI 21 -> 22`, and `mi_map` += `21 $lcd_mirror_0 s_axi`.
   - `touch_iic_add.tcl` (gated) moves `M21 -> M22`, `NUM_MI 22 -> 23`.
   - D6/D8/D10 take the indices after that.
5. **Assign the address:**
   `assign_bd_address -offset 0x44B80000 -range 256K [get_bd_addr_segs {lcd_mirror_0/s_axi/reg0}] ;# LCDMIR`.
   Packaging with `C_S_AXI_ADDR_WIDTH=32` gives the 4 GiB address block that
   `-range` carves from (`package_csr_ip.tcl` header).
6. **Update the regmap tooling** (`tools/gen_regmap.py`, `LCD_MIRROR_FPGA.md` §4):
   - `REGION_HI` and `WINDOW_HI` move to `0x44BC_0000`.
   - `check_page_allocation` must claim **every** page of a multi-page block,
     not only the base.
   - Add `MBV_OWNERS["LCDMIR"] = ("harnessd", "mps3-lcdmirror (generic-uio)")`.
   - Regenerate `platform_regs.h`, `regmap.py`, pyverify and `shell-regmap.md`.
   - `gen_dts.py` then emits `lcd-mirror@44b80000` with `reg = <0x44b80000 0x40000>`.
   - **Fallback, if the tooling change is refused:** build `BANKED=1`, 64 KiB at
     `0x44B5_0000`, with no tooling change beyond one page.
7. **Register the bench:**
   - Add `lcd_mirror` to `tests/Makefile` `BENCH_DIRS` and to
     `tests/common/list_benches.py`.
   - Add the three RTL files to the root `Makefile` `LINT_SV` (lint-clean with
     Verilator `-Wall` at `BANKED` 0/1, `DIRTY_CMP` 0/1, width 18/32).
   - Consider `make MODE=direct COCOTB_TEST_FILTER='test_harness_stream|test_clcd_demo_frames'`
     as the pre-mint gate.
8. **Update the seam golden:** `tests/shell_cpu_seam/golden/` gets the new cell,
   ports and nets as an expected delta (the D13 precedent).
9. **Add the board proof** (0.5 d, board): mirror vs photo, a handover,
   `clcd_demo`, and one photo each at MADCTL 0x60 (O2) and after a split 0x22 (O1).
