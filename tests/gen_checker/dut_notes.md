# DUT notes — `gen_checker`

RTL: `fpga/ethernet/gen_checker/gen_checker.sv`, module `gen_checker`
(params `C_S_AXI_ADDR_WIDTH=12`, `C_S_AXI_DATA_WIDTH=32`). **Real RTL since
W-RTL-ETH (2026-07-06)** — one of the two blocks spec §12 flags as needing
genuinely new RTL. Proven green under VCS 2022.06-SP2 + cocotb 2.0.1,
11/11, 2026-07-06.

Ports unchanged: `s_axi_*` AXI4-Lite slave (accept-on-valid response style,
one of the two contract-blessed styles), `gen_m_*` generator AXIS out,
`chk_s_*` checker AXIS in (with `tuser`). Generator and checker are two
independent functions; no internal loopback is assumed or present.

## REGMAP — v0.2 CONTRACT layout (supersedes the Phase-0 draft)

This bench and the RTL now implement `shell-regmap.md` v0.2's GENCHK table:
`CTRL`@0x00 {[0] gen_en, [1] chk_en} / `INJECT`@0x04 {[0] bad_fcs, [1]
runt, [2] giant, [3] ifg, [4] dribble} / `TX_CNT`@0x08 / `RX_CNT`@0x0C /
`ERR_CNT`@0x10 (all counters ro). The old DRAFT layout (mode-field
GEN_CTRL / GEN_COUNT / packed CHK_STATUS / CHK_CLEAR) is dead; the bench
was rewritten to the contract per established practice.

**A6 flag (stale shared constants):** `tests/common/regmap.py` still
carries the draft `GENCHK_GEN_CTRL`/`GENCHK_CHK_STATUS`/`GENCHK_MODE_*`
constants (that file is outside this wave's write scope). The v0.2
constants live locally in `test_gen_checker.py` until regmap.py is updated;
`firmware/common/platform_regs.h`'s GENCHK block needs the same check.

## Semantics pinned by this bench (v1 additions where v0.2 is silent — A6)

- INJECT is per-frame one-shot: write sets pending bits; ALL pending bits
  are consumed (cleared) at the next frame start; readback = still-pending.
- `ifg` compresses the gap AFTER the injected frame (1 idle cycle = 8
  bit-times vs the normal 12 cycles = 96 bit-times, modelled at the AXIS
  byte layer); `dribble` = one extra byte after the FCS (byte-granular).
- Counters: TX_CNT clears on gen_en 0->1 write; RX_CNT/ERR_CNT on chk_en
  0->1. The bench only ever asserts DELTAS, so codifying different clear
  semantics later won't break it.
- chk_en gates counting only; `chk_s_tready` is unconditionally 1 (the tap
  never backpressures the datapath).
- Checker error = bad FCS (CRC-32 residue over frame incl. FCS) OR length
  outside 64..1518 OR tuser seen anywhere in the frame.

## Not scored / not modelled in v1 (RTL header list)

RX-side IFG violations (no arrival-time model at the AXIS tap), sub-byte
dribble, VLAN 1522 envelope, host-configurable frame content/lengths.
