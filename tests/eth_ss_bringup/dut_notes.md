# DUT notes — `eth_ss_bringup`

RTL: `fpga/rp/eth_ss/eth_ss_bringup.sv`, module `eth_ss_bringup`
(params `START_DELAY_CYCLES`, `GAP_CYCLES`, and the programmed values
`MODER_VAL`/`INT_MASK_VAL`/`MAC_ADDR0_VAL`/`MAC_ADDR1_VAL`/`RX_BUF_PTR`/
`RTC_PERIOD_NS`/`RTC_PERIOD_FRAC`). NEW leaf bench (A5 verification-confidence
pass) — this single-master AHB-Lite constant-programmer FSM had **zero**
verification before this bench.

Port list (confirmed by reading the RTL):
- `clk`, `resetn` — dut_clk (= subsystem HCLK), active-low reset.
- AHB-Lite master OUT: `haddr[31:0]`, `htrans[1:0]`, `hwrite`, `hsize[2:0]`,
  `hburst[2:0]`, `hprot[3:0]`, `hmastlock`, `hwdata[31:0]`.
- AHB-Lite master IN: `hrdata[31:0]` (unused — write-only sequence),
  `hready` (= slave HREADYOUT, single-slave loop), `hresp`.
- Telemetry OUT: `done` (sequence complete, bus parked IDLE), `errored`
  (any HRESP=ERROR seen).

## Bench notes

- The bench plays the AHB-Lite **slave**: an always-ready responder
  (`hready=1`, `hresp=0`) that snoops the write stream. Address is captured in
  the address phase (`HTRANS=NONSEQ`), data one cycle later in the data phase
  — matching the FSM's `S_ADDR` (schedule, then accept when `htrans==NONSEQ &&
  hready`) → `S_DATA` (hold `hwdata` until `hready`) pipeline.
- `EXPECTED_WRITES` mirrors `seq_entry()`'s 13-entry ROM with the RTL's
  default programmed-value parameters. MODER (the enable) follows every
  configuration register; only CTRLMODER (TXFLOW) comes after it — asserted
  explicitly.
- Transmit beacon (2026-09-23): after `done` the FSM writes TXCTRL (`0x50`) =
  `0x0001_0000 | seq`, seq = 1, 2, 3 …, at once and then every
  `REARM_CYCLES` (Makefile `-pvalue` 200; RTL default 50,000,000 = 1 s at
  50 MHz). `test_transmit_beacon_rearms_every_period` checks values and
  spacing.
- The Makefile shrinks `START_DELAY_CYCLES=8`/`GAP_CYCLES=4` for sim speed
  (kept `GAP_CYCLES>=4`, the RTL's ha1588-strobe CDC minimum). Every check
  waits for `done`, so it is timing-independent.
- The second test injects `HRESP=ERROR` on one transfer to exercise the
  `S_DATA` `if (hresp) errored <= 1'b1` path (v1 makes no recovery attempt —
  it still reaches `done`, just latches `errored`).
- No AXI-Lite → no SVA bind; `../common/bench_common.mk` is still included for
  the `COVERAGE=1` toggle.

## Flagged for the RTL owner / A6

- v1 makes no error recovery: an `HRESP=ERROR` mid-sequence latches `errored`
  but the FSM finishes the remaining writes and parks anyway (a
  partially-configured MAC with `errored=1`). Fine as telemetry-only per the
  RTL header; flag if a real abort/retry is ever wanted.

## ARM=tx — the whole RM, transmitting (2026-09-23)

`make ARM=tx` elaborates `tb_eth_ss_tx.sv`: the unmodified `rp_eth_ss_wrapper`
(FSM + OpenCores MAC + HA1588 + rmii_to_mii + DMA SRAM, the file set
`fpga/rp/eth_ss/filelist.tcl` reads) wired to `tests/dut_egress/tb_dut_egress.sv`
(virtual PHY + bridge + DUTEGR, as shell_bd.tcl SECTION 5). The bench plays only
the MicroBlaze on DUTEGR. `REARM_CYCLES` is shortened to 4000 by defparam.
`test_eth_ss_tx.py` reads three frames back over AXI-Lite and compares each
byte for byte against a PAUSE frame (dst `01:80:C2:00:00:01`, src
`32:53:45:4C:53:02`, type `0x8808`, opcode 1, pause time 1/2/3, zero pad, FCS).

Needs `$ETH_SS_HOME` and `$ARM_IP_LIBRARY_PATH` (read-only), so it is NOT
in the default `make` and not in CI. VCS only (the default `SIM`).

Controls, both seen to fail on 2026-09-23 against the pre-beacon FSM
(`git show 0130a3a:fpga/rp/eth_ss/eth_ss_bringup.sv > /tmp/old.sv`):
- `make ARM=tx BRINGUP_SV=/tmp/old.sv` — `expected 3 frames at DUTEGR within
  340000 ns, read 0 (RMII TX bursts seen: 0, RX_FRAMES=0)`.
- `make BRINGUP_SV=/tmp/old.sv` — `expected 13 bring-up writes, captured 12`
  and `expected >= 4 TXCTRL beacon writes ... saw 0`.
