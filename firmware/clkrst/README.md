# clkrst/

DUT clock (MMCM DRP) + the three shell-driven resets, per
ARCHITECTURE_SPEC.md §5 and `shell-regmap.md`'s `CLKRST` block
(`0x44A0_0000`).

## Resets (three distinct, all shell-driven — §5)

1. **DUT system reset** (`dut_resetn`) — host-controllable via `reset`
   (net-protocol.md `{"op":"reset","target":"dut"}`), also the coordinator
   convenience `clkrst_pulse_reset()`.
2. **Reconfiguration reset** (`rp_resetn`) — owned by
   `coordinator/swap_fsm.c`, not exposed as a standalone host verb (it's an
   internal part of the swap sequence: held during decouple, released after
   verify).
3. **Debug reset** (`dbg_resetn`, SRST) — mapped from `swd_server`'s
   `r/s/t/u` remote_bitbang bytes onto this bit (see swd_server.c).

All three are `RESET_CTRL` bits with the same "1 = released" sense
(shell-regmap.md note: "async-assert/sync-deassert in RTL" — the
synchronization itself is an A1 RTL concern; firmware just pokes the bit
and, per §5, should assert immediately but only trust a deassert to have
taken effect after enough time for the destination-domain synchronizer,
which this skeleton does not currently wait for — TODO(A3)).

## DUT clock (DRP)

`DUT_CLK_SEL[7:0]` selects a canned preset (e.g. 25/50/100 MHz); the exact
preset-id table is **not defined in shell-regmap.md** (it just says
"quick presets" without enumerating them) — `clkrst.h`'s
`clkrst_preset_table` is a firmware-side placeholder guess, not a contract
value. `DUT_CLK_DRP[15:0]` is a raw MMCM DRP window for arbitrary
frequencies; the packing of that 16-bit window (is it {addr[6:0],
data[8:0]} multiplexed over multiple writes, or a full 16-bit DRP data
word with a separate implicit address sequencer in the clkrst RTL?) is
also unspecified — flag for A1/A6 alongside the preset-table question.

### Reading the clock back (`clkrst_read_mhz`, `clkrst_reload`)

The real retune path is clk_wiz_dut's AXI4-Lite register file (MMCM_DRP,
`0x44AB_0000`), and that file is readable: `clkrst_read_mhz()` decodes
`CFG_REG0`/`CFG_REG2` into MHz. It is fabric state, so it survives a
process restart — mps3-harnessd answers `stats.dut_mhz` from it (ILA-mint
finding #12). Bare metal keeps its RAM shadow (frozen at v0.11).

**What a reset does (vendor HDL, clk_wiz_v6_0).** clk_wiz_dut's only reset
is `s_axi_aresetn` = `peripheral_aresetn`, pulsed by the POR button and by
the shell watchdog. It returns the register file to the IP's 50 MHz values
and restarts the DRP state machine, which pulses the MMCM's reset — but it
does not rewrite the MMCM, and an MMCM reset does not undo DRP-written
M/D/O (only a device configuration does). So after `set_clk 100mhz` and a
watchdog reset the register file reads 50 while the MMCM re-locks at 100.
`clkrst_reload()` LOADs the register file into the MMCM to make the two
agree; harnessd calls it once per OS boot, only while every DUT-domain
reset is asserted (`harnessd/clk_linux.c`). Unproven on silicon until B1.

## Status

`STATUS.mmcm_locked` / `STATUS.dut_clk_alive` are read-only; `set_clk`
should poll `mmcm_locked` (with a bounded timeout) before reporting
`{"locked":true}` back to the host.
