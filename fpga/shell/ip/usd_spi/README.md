# `usd_spi` — USD, the SPI-mode master for the user microSD slot

**Real, synthesizable RTL** (lane D13-L1,
`docs/planning/HANDOVER_USD_OVERLAY_STORE.md` §4.1, decision D3). One 64 KiB
AXI4-Lite page at `0x44A4_0000`, replacing the pad-less `axi_quad_spi_0` that
held the OVLSTORE page. It drives the MPS3 **user** microSD slot (`USD_CLK`,
`USD_CMD`, `USD_DAT[3:0]`, `USD_NCD`) in SPI mode, so the overlay store can live
on a card.

> **The user card, not the MCC card.** `sd_install` / `V2M_MPS3` is the MCC
> config card, on a different controller. Nothing here can reach it.

Three things this block does that `axi_quad_spi` could not:

1. **Runtime clock divider.** SD init needs 100–400 kHz; data wants 12.5–25 MHz.
   The Quad SPI's ratio was fixed at build time.
2. **Card detect.** `USD_NCD` is synchronised, debounced, polarity-corrected and
   reported, with a sticky change flag.
3. **A hardware pad gate.** With no card in the slot every pad is high-Z,
   whatever firmware writes. A firmware bug cannot drive an empty socket.

Integration into the shell (BD, `shell_top`, XDC, generated views, gates) is
applied; [`INTEGRATION.md`](INTEGRATION.md) says where each piece landed and
how it was verified.

## Register map (`USD` @ `0x44A4_0000`)

| Off | Reg | Access | Bits | Behaviour |
|---|---|---|---|---|
| 0x00 | `ID` | RO | `[31:0]` | `0x55534431` ("USD1"). Every unmapped page reads 0, so this is how firmware probes for the block. |
| 0x04 | `CTRL` | RW, reset 0 | `[0] EN` | Enable the pad drivers (subject to the pad gate below). |
| | | | `[1] CS` | 1 = assert chip select: `USD_DAT[3]` driven low. |
| | | | `[2] WIDE` | 0 = 8-bit shift, 1 = 32-bit shift. Latched when a shift starts. |
| | | | `[3] CD_POL` | 0 = `USD_NCD` low means present (reset). 1 = inverted. |
| | | | `[4] CD_IGNORE` | 1 = treat the card as always present (a board whose CD is not wired). |
| 0x08 | `CLKDIV` | RW, reset 124 | `[15:0] DIV` | SCK = aclk / (2·(DIV+1)). 124 = 400 kHz, 3 = 12.5 MHz, 1 = 25 MHz at 100 MHz. Change it only while `BUSY` = 0. |
| 0x0C | `DATA` | W / R | `[31:0]` | **Write** starts a shift: SPI mode 0, MSB first. 8-bit mode sends `wdata[7:0]`; 32-bit mode sends the whole word (`wdata[31]` first). **Read** returns the last *completed* received word, zero-extended in 8-bit mode. A write while `BUSY` is ignored and sets `OVR`. |
| 0x10 | `STATUS` | RO / W1C | `[0] BUSY` | A shift is in progress. |
| | | | `[1] CD_PRESENT` | Synchronised, debounced, polarity applied, or forced by `CD_IGNORE`. |
| | | | `[2] CD_RAW` | The synchronised `USD_NCD` pin level: no debounce, no polarity. For board step B0. |
| | | | `[3] CD_CHANGED` | Sticky, W1C. Set on any change of `CD_PRESENT`. |
| | | | `[4] OVR` | Sticky, W1C. A `DATA` write arrived while `BUSY`. |
| | | | `[5] ABORT` | Sticky, W1C. The pads were gated off during a shift (card pulled, `EN` cleared), or a `DATA` write arrived while they were gated off. |

Only these five offsets decode. Every other offset in the 64 KiB page reads 0
and ignores writes (`BRESP` = `OKAY`). The decode is the **page** (16 bits), not
`C_S_AXI_ADDR_WIDTH`: the BD instantiates the block at width 32, so it sees
`0x44A4_000C`, not `0x0C` (the width-32 decode escape; `tests/csr_decode_width`).
Sticky bits: a set in the same cycle as a W1C wins, so no event is lost. There
are no read side effects. `CTRL` and `CLKDIV` honour `WSTRB` byte lanes. A
`DATA` write starts a shift whatever its strobes are; firmware uses `sw`.

The `IDX_*` lines in the RTL are the source `tools/gen_regmap.py` derives these
offsets from. Keep the idiom (and the trailing `// 0xNN` comment) exact.

## The pad gate (hardware safety)

```
pads_en = CTRL.EN && (CD_PRESENT || CTRL.CD_IGNORE)
```

- Every output-enable (`usd_clk_oe`, `usd_cmd_oe`, `usd_dat3_oe`) is `pads_en`,
  registered, so it cannot glitch. `SCK` is gated too: it is a split-tristate
  pad like the others.
- If `pads_en` falls during a shift, the shift aborts that cycle: `BUSY` clears,
  `SCK` returns low, `MOSI` returns high, and `ABORT` sets. `DATA` keeps the
  last *completed* word; the partial word is discarded.
- A `DATA` write while `pads_en` is 0 starts nothing and sets `ABORT`.
- Everything is on `s_axi_aresetn` (`peripheral_aresetn`). A watchdog reset
  clears `EN` and floats the pads.

`tests/usd_spi`'s `make control-cd-gate-ignored` rebuilds the block with the
gate reduced to `pads_en = EN` and shows `test_pads_hiz_without_card` failing.

## SPI timing

Mode 0: SCK idles low, the card samples MOSI on the rising edge, and MOSI moves
on the falling edge. MOSI idles high (0xFF), as SD expects.

**MISO is sampled late.** The sample point is the aclk edge that launches
SCK's *falling* edge, taken from a pad register (`miso_q`, `IOB = TRUE`)
captured one aclk earlier. In mode 0 the card's bit is valid from one falling
edge to the next. So the card's tODLY (≤ 14 ns) plus the board round trip get a
full SCK period minus one aclk: 30 ns at DIV=1 (25 MHz) and 70 ns at DIV=3. A
rising-edge sample would leave half a period: 20 ns at 25 MHz, which is about
what tODLY plus pads plus traces costs. `miso_q` and the shifter stage form a
2-flop chain, so a transition that does land on the sample edge still gets a
full aclk to resolve.

DIV=0 (aclk/2 = 50 MHz) is legal in the RTL, but it is outside SD SPI timing.
The fastest setting for firmware is DIV=1.

## Card detect

`USD_NCD` → 2-FF synchroniser (`ASYNC_REG`) → `CD_RAW` → debounce → polarity →
`CD_IGNORE` force → `CD_PRESENT`.

- **Debounce.** A new level must hold for `DEBOUNCE_CYCLES` consecutive aclk
  cycles. One sample that agrees with the old level restarts the count.
- **Order.** Polarity and the force apply *after* the debounce, so writing
  `CD_POL` or `CD_IGNORE` changes `CD_PRESENT` at once. Like any other change,
  that sets `CD_CHANGED`.
- **Reset.** The debouncer resets to "pin high", which means no card under the
  reset polarity. A slot with no card is quiet from reset. A card that is
  already inserted at power-up reads as an insertion `DEBOUNCE_CYCLES` after
  reset (`CD_PRESENT` 0 → 1, `CD_CHANGED` set).

## Ports

| Port | Dir | Pad | Notes |
|---|---|---|---|
| `s_axi_*` | | | AXI4-Lite slave, Xilinx-template handshake (as `dfx_ctl`, `board_gpio`) |
| `usd_clk_o` / `usd_clk_oe` | out | `USD_CLK` (AU15) | SCK |
| `usd_cmd_o` / `usd_cmd_oe` | out | `USD_CMD` (AU16) | MOSI |
| `usd_dat0_i` | in | `USD_DAT[0]` (AV14) | MISO |
| `usd_dat3_o` / `usd_dat3_oe` | out | `USD_DAT[3]` (AT12) | CS, active low |
| `usd_ncd_i` | in | `USD_NCD` (AT15) | card detect |

`_oe` = 1 means drive: `shell_top` forms each pad as `OBUFT`/`IOBUF` with
`T = ~oe` (the `board_gpio` convention).

**`USD_DAT[1]` and `USD_DAT[2]` are not ports of this block.** SPI mode does not
use them. `shell_top` still brings them in as `IOBUF`s with `T = 1'b1`
(permanently high-Z) and the XDC pulls them up. That way a later native 4-bit
controller needs no pin change.

## Parameters

| Parameter | Default | Shipped (BD) | Notes |
|---|---|---|---|
| `C_S_AXI_ADDR_WIDTH` | 12 | **32** | Local page decode caps at 16 bits either way. |
| `C_S_AXI_DATA_WIDTH` | 32 | 32 | 32 only. |
| `DEBOUNCE_CYCLES` | 1 000 000 | default | 10 ms at 100 MHz. The bench's fast configurations use 64. |

## Verification

```
verilator --lint-only -sv fpga/shell/ip/usd_spi/usd_spi.sv          # the `make lint` check: clean
verilator --lint-only -Wall -sv fpga/shell/ip/usd_spi/usd_spi.sv    # also clean

source set_env.sh
make -C tests/usd_spi                           # CFG=bd (width 32, 10 ms) then CFG=w12
make -C tests/usd_spi sim CFG=bdfast            # width 32, 64-cycle debounce: the quick loop
make -C tests/usd_spi control-cd-gate-ignored   # MUST FAIL
```

Out-of-context synthesis (Vivado 2024.1, xcku115-flvb1760-1-c,
`C_S_AXI_ADDR_WIDTH=32`, 10 ns clock): 138 LUTs, 235 FFs, 6 CARRY8. WNS is
+7.35 ns, with no critical warnings. The `IOB` request on `miso_q` is ignored
out of context, because there is no I/O buffer; it applies once the block is in
the shell.

The bench has 27 cocotb tests. They cover registers and decode, SCK period,
mode-0 8/32-bit shifts, BUSY visible on the first poll after a write, late
MISO sampling, CS, the pad gate, card detect,
ABORT/OVR, and an SD SPI-mode card model (`tests/usd_spi/sd_card_model.py`)
driven end to end through AXI only. The AXI4-Lite protocol checker is bound at
the instance's own address width (`tests/common/sva/bind_usd_spi.sv`).
