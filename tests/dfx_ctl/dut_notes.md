# DUT notes — `dfx_ctl`

RTL: `fpga/shell/ip/dfx_ctl/dfx_ctl.sv`, module `dfx_ctl` (params
`C_S_AXI_ADDR_WIDTH=12`, `C_S_AXI_DATA_WIDTH=32`). Real port list
(confirmed by reading the RTL, 2026-07-04):

- `s_axi_*` — AXI4-Lite slave, DFXCTL regmap (`DECOUPLE`@0x00, `SHUTDOWN`
  @0x04, `STATUS`@0x08 ro — shell-regmap.md, all real/confirmed offsets).
- `decouple_en_o` (O) / `decoupled_i` (I) — DFX Decoupler ctl/status.
- `axi_shutdown_req_o` (O) / `axi_shutdown_ack_i` (I) — AXI Shutdown
  Manager ctl/status.
- `rp_resetn_gate_o` (O), consumed by `dut_clkrst.sv`'s
  `rp_resetn_gate_i` — the cross-block RP-reset composition this bench's
  `clkrst` sibling also exercises from the other side.
- `rp_in_reset_i` (I) — observed RP reset state, feeds `STATUS[1]`.
- `rm_id_i[31:0]`, `dut_lockup_i` (I) — RM-load-verify taps, **now fully
  regmap-addressable** via `RM_ID`@0x10 / `RM_STATUS`@0x14 (see the updated
  section below).
- `axi_shutdown_req_o` (O) / `axi_shutdown_ack_i` (I) — AXI Shutdown Manager
  ctl/status; the ack is accepted but **not consumed by any logic** (finding
  below).

Status (refreshed 2026-07-08, A5 verification pass): **REAL, synthesizable
AXI4-Lite slave — graduated from the A1 Phase-0 stub.** The AXI-Lite
write/read FSMs, the register file (`DECOUPLE`/`SHUTDOWN`/`STATUS`), the
`rp_resetn_gate_o` state machine (drops with DECOUPLE, holds until the
synchronized `decoupled_i` confirms re-coupling), and the RM-load-verify
readback (`RM_ID`@0x10 with a 2-FF synchronizer + N-cycle stability detector,
`RM_STATUS`@0x14 = {dut_lockup, rm_id_valid}) are all real logic.
`rtl_ready()` reports READY.

Covered by the bench (`test_dfx_ctl.py`): DECOUPLE -> pin + gate composition,
STATUS mirroring, SHUTDOWN request, WSTRB lane-0 gating on DECOUPLE, the full
decouple->gate->shutdown->reversal sequence, and RM_ID/RM_STATUS readback +
rm_id_valid settle.

### Finding for the RTL owner (A1/A6) — shutdown ack unconsumed

`axi_shutdown_ack_i` is accepted (and `lint_off UNUSED`) but drives NOTHING:
STATUS@0x08 has no shutdown-idle bit, and the gate FSM keys off DECOUPLE +
`decoupled_i`, not the ack. Firmware cannot poll shutdown completion.
`test_shutdown_ack_and_decouple_gate_reversal_sequence` drives the ack and
pins that the (real) gate sequencing is unaffected by it — documenting the
missing handshake path (matches the RTL's own ambiguity #2). Add a
`STATUS[2]=shutdown_idle` bit if firmware needs it.

## Ambiguity for A6 — RM-load verify (rm_id/CRC) regmap offset: contract
## moved, RTL/firmware haven't (updated 2026-07-04, W5)

`platform_regs.h`'s AMBIGUITY(A6) #4 as originally written: `shell-
regmap.md` v0's "RM-load verify" note said the coordinator reads rm_id "as
a DFXCTL-adjacent read... or via the DUT's own bus" plus "optional DFX
Bitstream Monitor + config CRC" — but the DFXCTL table itself only had
DECOUPLE/SHUTDOWN/STATUS, no rm_id/CRC offset.

**shell-regmap.md is now v0.1** and *does* give this a real offset pair
(OPEN_ISSUES I8 resolved there): `RM_ID`@0x10 (ro, `[31:0] rm_id`) and
`RM_STATUS`@0x14 (ro, `[0] rm_id_valid, [1] dut_lockup`) — no more
"placeholder," and no `CFG_CRC` register at all (superseded by
`RM_STATUS.rm_id_valid`). **Update (same day, later in this delivery
window): the firmware side has since caught up** — re-reading
`firmware/common/platform_regs.h` directly now shows
`DFXCTL_RM_ID`@0x10/`DFXCTL_RM_STATUS`@0x14 (explicitly commented "v0.1
RESOLVED... I8"), and `firmware/coordinator/swap_fsm.c`'s `step_verify()`
does a real `DFXCTL.RM_ID == target_rm_id && RM_STATUS.rm_id_valid`
compare (I25 — see `firmware/test/test_swap_fsm_hw.c`'s
`test_i25_verify_mismatch_fails_and_stays_decoupled()` and this suite's
own `tests/integration/test_swap_sequence.py::test_verify_rejects_rm_id_mismatch`).
**UPDATE 2026-07-08 (A5 verification pass): `dfx_ctl.sv` HAS now caught up.**
The RTL fully decodes `RM_ID`@0x10 and `RM_STATUS`@0x14: `rm_id_i` crosses a
2-FF synchronizer plus an N-cycle stability detector (`rm_id_valid` asserts
once the synchronized value has been stable for `RM_ID_SETTLE_CYCLES`), and
`RM_STATUS` reads back `{dut_lockup (synchronized), rm_id_valid}`.
`tests/common/regmap.py` (`DFXCTL_RM_ID`=0x10, `DFXCTL_RM_STATUS`=0x14) and
the firmware header now all agree, and `tests/integration/
test_swap_sequence.py`'s `MockRegs` models the same pair.

The old `test_rm_id_and_lockup_ports_are_wired` port-level smoke test is kept
(still green), but `test_rm_id_and_rm_status_regmap_readback` now does the
real regmap read: it drives `rm_id_i`, waits out the sync + settle, and
asserts `RM_ID` reads the synchronized value with `RM_STATUS.rm_id_valid`
set, plus `dut_lockup` mirroring. This is the `swap_fsm.c` step_verify()
ground truth, now verifiable at the RTL.
