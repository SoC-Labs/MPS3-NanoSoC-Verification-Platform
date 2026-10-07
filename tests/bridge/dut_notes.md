# DUT notes — `bridge`

RTL: `fpga/ethernet/bridge/eth_bridge_3port.sv`, module `eth_bridge_3port`
(params `MGMT_MAC`/`DUT_MAC`/`BUF_AW` — new in W-RTL-ETH; the bench uses
the defaults). **Real forwarding RTL since W-RTL-ETH (2026-07-06)** —
hand-rolled store-and-forward crossbar, NOT vendored Forencich cores (no
copy exists on this machine; repo consumes external IP read-only — see the
RTL header + block README for the full vendoring decision). Proven green
under VCS 2022.06-SP2 + cocotb 2.0.1, 3/3, 2026-07-06.

Port list unchanged: `clk_i`/`rst_i` + three AXI-Stream slave/master pairs
(byte-wide, `tlast`, no `tuser` except `dut_mac_s_tuser`):
`mgmt_*`, `dut_mac_*` (mirrors `link_partner_mac`'s AXI-Stream),
`uplink_*` (toward `lan9220_if`).

## Behaviour pinned by this bench

- Known dst (compile-time table: `MGMT_MAC`=02:00:00:00:00:01 -> mgmt,
  `DUT_MAC`=02:00:00:00:00:02 -> dut_mac) routes to exactly that port.
- Unknown dst floods to every port except its ingress (D9), never
  reflected.
- `dut_mac_s_tuser` is accepted and ignored for routing (v1 policy — an
  errored frame still forwards; gen_checker owns scoring).

## v1 policies documented in the RTL header (not bugs)

- Store-and-forward, ONE frame per ingress port (2 KB buffer); ingress
  `tready` drops while its frame awaits/undergoes forwarding (head-of-line
  blocking by design). Egress replay is 2 clk/byte; flood copies are
  sequential, so cross-port ordering is not guaranteed.
- Oversize (>2 KB) frames are swallowed whole and dropped, never truncated.
- A frame whose only resolved egress is its own ingress port is dropped.
- Register-less per OPEN_ISSUES I11 / shell-regmap.md v0.2 ("confirmed
  intentional"). The DUT's MAC changing between RMs means post-swap DUT
  traffic floods until the compile-time table is updated — runtime
  override would need a regmap block (A6 ambiguity, unchanged).

## Bench-side infrastructure note

Unskipping this bench exposed two timing bugs in `tests/common/axis.py`
(post-edge sampling; first-beat write racing an edge-coincident Timer) —
fixed there with pre-edge sampling + first-beat phase alignment; see that
file's beat-timing note. `tests/uart_bridge` (the other AxisByteDriver
consumer) re-ran green after the fix.
