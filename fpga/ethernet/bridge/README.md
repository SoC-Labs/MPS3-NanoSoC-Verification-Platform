# `bridge` — 3-port L2 bridge

**Real forwarding RTL since W-RTL-ETH (2026-07-06).** Routes three fixed
ports — management (MicroBlaze/lwIP), DUT-MAC (`link_partner_mac/`), and
LAN9220-uplink (`lan9220_if/`) — by destination MAC address, with a
two-entry compile-time forwarding table and flood on miss (spec §9,
Decision D9). This is deliberately **not** a general learning
switch/VLAN/STP core; that scope choice is what keeps it small and fully
in-house. Bench: `tests/bridge/` (3/3 green under VCS + cocotb 2.0.1).
Verilator `-Wall` clean.

## Vendoring decision (W-RTL-ETH, supersedes the Phase-0 reuse plan)

The Phase-0 plan named Forencich `axis_switch` (verilog-axis, MIT) +
`eth_axis_rx` (verilog-ethernet, MIT). **Decision: hand-rolled instead.**
Grounds:
- No copy of verilog-ethernet/verilog-axis exists anywhere on this machine
  (searched `~/SoCLabs` + `/research` for `eth_mac_mii*`,
  `axis_switch*`, `eth_axis_rx*` — nothing), so "reuse" would mean pulling
  and vendoring a third-party tree.
- This repo's IP policy is read-only external consumption via env vars; it
  vendors no third-party RTL. Adding a vendored tree for what spec §12
  itself sizes as "3-port forwarding glue (small)" is more surface (license
  files, sync policy, lint exemptions) than the ~300-line module.
- The crossbar the Forencich composition would provide is the easy part;
  the store-and-forward buffering + routing policy glue would be custom
  either way.

Vendoring stays the documented fallback if the bridge outgrows itself
(spec §9 order: Forencich → Xilinx AXIS Switch → LiteEth) — an A6 call.

## v1 forwarding policy (normative list in the RTL header)

Store-and-forward, one 2 KB frame buffer per ingress; round-robin
sequencer replays each parked frame to its egress port(s) at 2 clk/byte.
`dst == MGMT_MAC` (02:00:00:00:00:01) → mgmt; `dst == DUT_MAC`
(02:00:00:00:00:02) → dut_mac; everything else (incl. broadcast and
too-short frames) floods to all-but-ingress; never reflected; own-port-only
destinations dropped; oversize frames swallowed whole (never truncated);
flood copies sequential (no cross-port ordering guarantee);
`dut_mac_s_tuser` accepted but ignored for routing. The MACs are module
parameters — the same placeholder values `tests/bridge` uses.

## Port groups (see `eth_bridge_3port.sv` — port list unchanged)

Three AXI-Stream slave (ingress) + three AXI-Stream master (egress) ports:
- `mgmt_*` — MicroBlaze/lwIP management stack (exact interface shape still
  TBD by A3's lwIP integration).
- `dut_mac_*` — `link_partner_mac/`'s AXI-Stream (mirror image).
- `uplink_*` — `lan9220_if/`'s AXI-Stream toward the physical LAN9220.

## Ambiguity for A6 (still open — also flagged in `fpga/ethernet/README.md`)

`shell-regmap.md` v0.2 confirms BRIDGE stays register-less (OPEN_ISSUES
I11, "confirmed intentional"), so there is **no AXI-Lite surface** and the
forwarding table is compile-time. Consequence to keep in view: the DUT's
MAC address can change between RMs; with a fixed table, post-swap DUT
traffic merely floods (functionally correct, noisier on mgmt/uplink). If
MicroBlaze ever needs packet/drop counters or a runtime DUT-MAC override,
a regmap block must be added — that reopens I11 with A6.
