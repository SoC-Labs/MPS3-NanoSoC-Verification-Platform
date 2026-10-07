# `gen_checker` — error-inject traffic generator / checker

**Real RTL since W-RTL-ETH (2026-07-06).** Spec §12 calls this one of only
**two** blocks needing meaningful new RTL (the other is `mdio_phy_model/`).
This is what turns the subsystem into MAC *verification* rather than "it
pings" (spec §8.1, §8.4): inject known-good and deliberately **malformed**
frames toward the DUT (bad FCS, runts, giants, IFG violations, dribble),
and independently check the DUT MAC's TX framing/CRC and RX behaviour.
Bench: `tests/gen_checker/` (20/20 green under VCS + cocotb 2.0.1 — incl. the
Phase-3 DUT-IP sniffer cases), using `tests/common/frames.py` as the
behavioural spec (the RTL's CRC-32 is bit-exact with it). Verilator `-Wall`
clean.

## Two independent functions

1. **Generator** — synthesizes frames (good and malformed) onto the
   DUT-RX-bound AXI-Stream path (toward `link_partner_mac/`'s TX side, or
   spliced ahead of `bridge/`'s DUT-MAC egress — see
   `link_partner_mac/README.md` "Note for gen_checker"). Free-runs while
   `CTRL.gen_en`; INJECT faults are per-frame one-shots.
2. **Checker** — independently scores frames the DUT MAC transmits (tapped
   from `link_partner_mac/`'s RX AXI-Stream): CRC-32 residue check, 64..1518
   length envelope, plus OR-accumulation of the upstream `tuser` frame-error
   flag as a cross-check. Never backpressures the tap (`chk_s_tready`≡1).

## Register layout — now the v0.2 CONTRACT (old draft is dead)

`shell-regmap.md` v0.2 defines the GENCHK table this RTL implements:

| Off | Reg | Bits |
|---|---|---|
| 0x00 | `CTRL` | [0] gen_en, [1] chk_en |
| 0x04 | `INJECT` | [0] bad_fcs, [1] runt, [2] giant, [3] ifg, [4] dribble — pending until consumed by the next frame |
| 0x08 | `TX_CNT` | [31:0] frames generated (ro) |
| 0x0C | `RX_CNT` | [31:0] frames checked (ro) |
| 0x10 | `ERR_CNT` | [31:0] frames failing the checker (ro) |
| 0x14 | `DUT_IP` | [31:0] sniffed DUT IPv4, network order — first octet in [31:24] (ro) |
| 0x18 | `DUT_STATUS` | [0] ip_seen, [31:16] last-seen ethertype (ro) |

The Phase-0 draft that used to live here (mode-field `GEN_CTRL`,
`GEN_COUNT`, packed `CHK_STATUS`, `CHK_CLEAR`) is superseded.
AXI4-Lite response style: accept-on-valid (contract-blessed style 2).

## DUT-IP sniffer (Phase-3 — `DUT_IP` / `DUT_STATUS`)

A third, **always-on** function alongside the generator and checker: a passive
snoop of the *same* `chk_s_*` DUT-TX tap the checker reads (still non-perturbing
— `chk_s_tready`≡1, read-only). It latches the DUT's own IPv4 address out of the
frames the DUT transmits, so the shell firmware can **learn the DUT address
without arming the generator/checker** (`CTRL.chk_en` is irrelevant to it).

- It reuses the checker's frame-aligned byte index `clen_q` as the offset into
  the untagged frame `[dstMAC 0..5][srcMAC 6..11][ethertype 12..13][payload..]`:
  - **ethertype** = bytes 12..13;
  - **IPv4** (0x0800): source address = bytes **26..29**;
  - **ARP** (0x0806): sender-protocol-address = bytes **28..31**.
- `DUT_IP` stores the address MSB-first / **network order** — the first
  dotted-quad octet is in bits [31:24].
- `DUT_STATUS[0]` (`ip_seen`) sets on the last address byte and stays set;
  `DUT_STATUS[31:16]` mirrors the last-seen ethertype (a debug aid).
- ARP and IPv4 are **mutually exclusive per frame** (ethertype gate); the most
  recent IP-bearing frame wins.
- Runs entirely in the refclk domain like the counters — the 50→100 MHz
  AXI-Lite crossing is `axi_cc_genchk`'s (shell) job, so the sniffer adds **no
  new CDC**; it is treated exactly like the tx/rx/err counters.

**Limitation (v1): UNTAGGED frames only.** An 802.1Q VLAN tag (ethertype 0x8100
at bytes 12..13) inserts 4 bytes and shifts every subsequent field; such a frame
is simply ignored (its ethertype is neither 0x0800 nor 0x0806). Stacked/QinQ
likewise. A tag-aware v2 would offset the address fields by 4 when 0x8100 is
seen at bytes 12..13.

Bench: `tests/gen_checker/` adds directed cases (`test_sniffer_*`) feeding known
ARP/IPv4/non-IP frames and asserting `DUT_IP` byte order, `ip_seen`, the ARP-vs-
IPv4 offset, ethertype-gate immunity, `chk_en`-independence, and last-frame-wins.

## v1 semantics added where the contract is silent — for A6 to codify

- **Counter clear:** TX_CNT clears on a gen_en 0→1 write; RX_CNT/ERR_CNT on
  chk_en 0→1 (v0.2 defines no clear register; benches/firmware should use
  deltas until this is codified).
- **INJECT.ifg** compresses the gap *after* the injected frame (1 idle
  cycle = 8 bit-times vs normal 12 = 96), modelled at the AXIS byte layer.
- **INJECT.dribble** = one extra byte after the FCS (an AXIS byte stream
  cannot carry sub-byte dribble; wire-level dribble bits would need
  rmii_phy_if cooperation — v2 if ever needed).
- Fixed frame content (addresses/ethertype/counting payload; runt=32 B,
  giant=1536 B totals) — not host-configurable.
- Checker does NOT score RX-side IFG (no arrival-time model at this tap)
  or the VLAN 1522 envelope.

## Address decode — full local word address (RESOLVED 2026-07-09)

The AXI4-Lite decode now compares the **full** local word address
(`s_axi_awaddr[11:2]` / latched `s_axi_araddr[11:2]`), so only the five mapped
offsets (`0x00`/`0x04`/`0x08`/`0x0C`/`0x10`) respond; every other offset in the
block's page is unmapped — reads return 0, writes are accepted (`BRESP=OKAY`)
with no effect. Previously only `addr[4:2]` were decoded, so unmapped offsets
aliased onto real registers (first alias `0x20 → CTRL`): a stray **write** to
`0x20` silently toggled `gen_en`/`chk_en` and cleared the counters. Flagged by
the SystemRDL decode-equivalence gate (`poc/systemrdl/EQUIV_ALL_RESULT.txt`,
"gen_checker 0x20 → CTRL"). Mapped-offset behaviour (incl. the clear-on-enable-
rise counters and the per-frame INJECT consume) is bit-for-bit unchanged.

## Remaining ambiguity for A6 (I10 tail)

- No `net-protocol.md` verb drives GENCHK yet — the planned
  `{"op":"macgen",…}` (Phase 5) still needs defining.
- `tests/common/regmap.py` and `firmware/common/platform_regs.h` still
  carry the dead draft constants — update both to the v0.2 table (the
  bench keeps local v0.2 constants until then).
