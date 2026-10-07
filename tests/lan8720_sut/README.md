# tests/lan8720_sut — exercise the LAN8720 on PMOD0 with the real DUT

A board-free **SUT** that drives the **real ethernet-subsystem DUT** — `ethmac_ahb_rmii`,
the same design the ethernet-subsystem project runs — at its MDIO boundary, through a
model of the **MPS3 J28/PMOD0 shield path** (same wiring as the `arm_mps3` target),
against this repo's Clause-22 virtual PHY.

Pure-VCS SystemVerilog (no cocotb), standalone like `tests/rmii_conformance`,
`tests/rmii_speed`, `tests/shield_path`. Not wired into repo-root `make check`.

```
source set_env.sh
make -C tests/lan8720_sut          # build the real DUT + models, run
make -C tests/lan8720_sut result   # regenerate RESULT.txt
```

## Topology

```
[AHB master BFM] --AHB--> [ DUT: ethmac_ahb_rmii ] --MDIO--> [shield_path] --MDIO--> [mdio_phy_model]
   (this TB)                (real OpenCores eth_miim              (MPS3 J28,           (virtual LAN8720)
                             MDIO master + MAC)                    fault injectable)
```

The DUT, its OpenCores ethmac core, and the ARM CMSDK SRAM model are consumed **read-only**
from the sibling repo (`ethernet-subsystem-ahb`) and the ARM IP library — nothing outside
this directory is modified. Paths resolve exactly like `ethernet-subsystem-ahb/set_env.sh`;
override `ETH_SS_HOME` / `ETHMAC_IP_DIR` / `CMSDK_DIR` on the CLI if your checkout differs.

`shield_path` carries the same J28/SH0 signal set, with the inbound (PHY→FPGA) and outbound
(FPGA→PHY) directions as **runtime knobs**, so the SUT can inject the board's fault.

## Primary result — reproduce the board fault with the real DUT

`nanoSoC-refactor/ethernet-subsystem-ahb/docs/mps3_ethernet_debug_plan.md` localised the MPS3
failure to the SH0 shifter passing FPGA→PHY but dead PHY→FPGA. This SUT reproduces the
**timing-independent** symptom through the DUT's own `eth_miim` MDIO master (not a bench master):

| phase | shield | result |
|---|---|---|
| 1 | healthy | register file reachable (see finding below) |
| 2 | **inbound dead** | **MDIO reads `0xFFFF` at all 32 PHY addresses** — board finding #3 |
| 3 | restored | register file reachable again |

`repro_ok=1`, `VERDICT=PASS`. The AHB path is sane (MODER reads its `0x0000A000` default).

## DETECTED FINDING — the virtual PHY is not MDIO-interoperable with the real MAC

Running the model against the **real** OpenCores `eth_miim` master exposed a bug the model's
own self-bench (`tests/mdio_phy_model/`) could not: **`mdio_phy_model`'s MDIO frame is
bit-misaligned versus `eth_miim`.**

- **Healthy reads come back right-shifted by exactly one bit.** PHYID1 `0x0007`→`0x0003`,
  PHYID2 `0xC0F1`→`0x6078`, ANAR `0x01E1`→`0x00F0` — each `value >> 1`, deterministically.
- **Writes do not commit.** A write of `0x0181` to ANAR leaves the register at its default
  (read back as `0x01E1 >> 1 = 0x00F0`, not `0x0181 >> 1 = 0x00C0`).

Root cause: `mdio_slave.sv` drives read data across a 2-bit turnaround (samples on `posedge mdc`,
drives on `negedge mdc`), while `eth_miim` samples its 16 data bits starting one MDC period
earlier — a one-bit turnaround/framing offset. The model's self-bench used a **matched
hand-rolled master**, so both sides shared the same off-by-one and it cancelled.

**Why it matters.** `docs/DUT_ETHERNET_EGRESS.md` Option C faces `mdio_phy_model` at the real
nanoSoC MAC (an `eth_miim`-family MDIO master). As-is, that DUT would read shifted garbage and
its PHY writes would silently no-op. This is a real integration bug waiting at the egress plan,
now caught in simulation before any board.

This finding is **not folded into a false green**: the SUT's PASS is the timing-independent
dead-inbound reproduction; the framing gap is reported as a distinct `DETECTED FINDING`. Fixing
`mdio_phy_model` (shared RTL with its own bench and users) is out of scope here — flagged for
its owner. Which side is strictly IEEE-802.3-compliant is a follow-up; the SUT proves only that
they disagree, and exactly how.

## Datapath SUT — TX frame egress through the real MAC + DMA + RMII

`tb_lan8720_dp.sv` (target `make -C tests/lan8720_sut dp`) exercises the **frame datapath**, not
just management. It stands up the whole real path: the DUT's OpenCores MAC, its AHB **DMA master**
(answered by a pure-SV AHB-lite slave RAM in the bench), the internal `rmii_to_mii` bridge, and the
RMII egress — routed through `shield_path` (the `ref_clk` is carried through the shield model too).

```
[reg-slave BFM]--AHB-->[ DUT ethmac_ahb_rmii ]--RMII-->[egress decoder]   ref_clk--[shield]-->DUT
[SV AHB slave RAM]<-AHB(DMA)--/   (real MAC + DMA + rmii_to_mii)
```

Sequence: preload a broadcast frame (`dst=FF.., src=02:00:00:00:00:01, EtherType 0x88B5,
payload "MPS3-LAN8720!"`) into DMA RAM, arm TX BD0, enable `MODER.TXEN|FULLD|CRCEN|PAD`, poll the
BD Ready bit to completion, and decode the RMII di-bit stream back into bytes.

**PASS gates (`errors==0`, `VERDICT=PASS`):**
- the full 60-byte frame **body egresses byte-for-byte** on RMII (SFD found, dst/src/type/payload/pad);
- the MAC **clears the TX BD Ready bit** and sets `INT_SOURCE.TXB` (it consumed the descriptor);
- **no over-read**: memory past the 60-byte buffer is marked `0xAA`; none of it reaches the wire,
  proving the DMA honoured the buffer length.

### Endianness note (why the buffer is loaded big-endian)

The OpenCores MAC transmits the **MSB of each 32-bit word first**. The bench's `ram_put_byte`
therefore lays wire-order byte *N* at data bits `[31:24-(N%4)*8]`. This mirrors what cocotbext's
AHB slave does, which is why the reference bench loads its frame in plain wire order. (Diagnosing
this took one run: the egress came out with every 32-bit word byte-reversed.)

## DETECTED FINDING — TX frame length is not honoured; the MAC zero-pads to MAXFL

The MAC transmits the correct 60-byte body, then **keeps `tx_en` asserted and sends zeros up to
MAXFL** instead of terminating at length+FCS. Evidence, all from this SUT:

- frame on the wire = **1526 bytes** = MAXFL(1518) + 8 (preamble+SFD); drop MAXFL to 128 and it
  becomes 136 = 128+8 — the terminator is the **MAXFL guard**, not the BD length (60);
- the `0xAA` over-read guard passes → the DMA stopped fetching at 60 bytes; the tail is **TX-side
  zero-fill**, not a memory over-fetch;
- `tx_en` has exactly **one** rising edge → it is one long frame, not a re-transmit loop.

**This is a real DUT behaviour, not a bench artifact.** The reference cocotb bench
(`ethernet-subsystem-ahb/.../cocotb/ethmac_ahb/test_basic_app.py`), which uses a mature
burst-correct cocotbext AHB slave, logs the **identical** `MII TX capture: 1526 bytes` and its own
capture check fails (`EtherType mismatch: 0x1A00`) — but that check is wrapped in a non-fatal
try/except, so the suite reports PASS and the overrun stays latent. This SUT surfaces it explicitly
and (getting the word byte-order right) still verifies the body byte-exact.

The finding is **not folded into a false green**: the PASS gates above are all legitimately met
(body correct, BD consumed, no over-read); the overrun is reported as a distinct `NOTE`/finding for
root-cause (candidate: the patched `eth_wishbone` TX end-of-frame/length termination). It matters
for `docs/DUT_ETHERNET_EGRESS.md` Option C — a real nanoSoC MAC would put max-length frames on the
wire for every short packet.

## Datapath SUT — RX frame reception + board-fault reproduction

The same `tb_lan8720_dp.sv` also covers the **RX datapath**, and this is where the SUT earns its
keep: the reference cocotb bench injects RX at the **MII** interface (it wraps `ethmac_ahb`, not the
RMII variant), so **this SUT is the first to drive a real frame through the `rmii_to_mii` RX path**.

An RMII frame source (`rmii_rx_send`) drives one di-bit per `ref_clk`, LSB-pair first —
preamble(7×55) + SFD(0xD5) + body + a correct **FCS** (reflected CRC-32, self-tested against
`CRC32("123456789") == 0xCBF43926`) — onto the shield's PHY side. Two cases:

| case | shield inbound | RX BD | result |
|---|---|---|---|
| positive control | **healthy** (`pass_b2a=1`) | Empty clears, HW length=64, CRC-error clear, buffer header matches | RX path works end to end |
| **board fault** | **dead** (`pass_b2a=0`) | Empty **stays set** | **board finding #4 reproduced** at the datapath |

Notes: the shield carries `ref_clk` inbound too, so the dead case kills the DUT's RX clock domain as
well — exactly the board condition (the LAN8720 sources REF_CLK through the same dead SH0 shifter).
The filled RX BD flags `ShortFrame` (the 64-byte wire frame equals MINFL) — informational only, it
cannot raise an error (`eth_wishbone.v:2392`), and the CRC-error bit is clear. RX data reads back in
wire order via the big-endian `ram_get_byte`.

Two more RX sub-phases round out the robustness:

- **RX-2 bad-FCS** (`rmii_rx_send(.., bad_fcs=1)`): a frame with a corrupted FCS must not be accepted
  as good — the RX BD comes back with the **CRC-error bit set** (`status[1]=1`). Gated (PASS).
- **RX-3 multi-BD ring** (two frames, BD2 no-wrap → BD3 wrap): **OBSERVATION, not gated.** The second
  frame re-used BD2 (marker byte traced into BD2's buffer) instead of advancing to BD3, even after
  re-homing the RX pointer via a `TX_BD_NUM` rewrite. Whether this is a MAC RX-BD-pointer quirk (a
  wishbone BD-handshake issue akin to the TX-length finding, exposed by the AHB→WB integration) or
  residual pointer state from the prior single-BD frames is **unresolved** — flagged for follow-up,
  deliberately not folded into the verdict.

## Coverage summary

With both phases, this SUT reproduces the board fault (`mps3_ethernet_debug_plan.md`) on **all three**
of its symptoms with the real DUT, plus a positive control for each:

- **MDIO** dead-inbound → `0xFFFF` at all 32 addresses (`tb_lan8720_sut.sv`);
- **RMII TX** → frame egresses byte-exact through the real MAC+DMA+bridge (`tb_lan8720_dp.sv`);
- **RMII RX** dead-inbound → RX BD never fills, vs. a healthy control that does (`tb_lan8720_dp.sv`);

and it surfaced two integration findings the reference suite masks — the MDIO framing mismatch and
the TX zero-pad-to-MAXFL length behaviour.

## DETECTED FINDING (deeper) — TX length governed by MINFL/MAXFL, never the buffer length

The TX zero-pad-to-MAXFL note above turned out to be one face of a bigger bug. Driving the bench with
the `EXP_*` plusargs and sweeping frame length vs MINFL shows the MAC **always** terminates a frame at
`MINFL` (truncating anything longer) if the transmit length exceeds MINFL, and otherwise runs to the
`MAXFL` guard — the **buffer-descriptor length never terminates the transmission**. A falsification
test of that rule passed on all five predictions. Full evidence and mechanism in
[`FINDING_TX_LENGTH.md`](FINDING_TX_LENGTH.md).

```
# reproduce: sweep frame length vs MINFL (CRC off => tx_len == LEN)
make -C tests/lan8720_sut dp                                   # default: PAD=1 CRC=1 LEN=60 MINFL=64
./tests/lan8720_sut/simv_dp +EXP_LEN=100 +EXP_MINFL=64 +EXP_CRC=0   # -> truncates to 64
./tests/lan8720_sut/simv_dp +EXP_LEN=40  +EXP_MINFL=64 +EXP_CRC=0   # -> runs to MAXFL (1518)
```

The `EXP_*` plusargs default to the committed behaviour, so a plain `make dp` is unchanged (VERDICT=PASS).

## Not yet covered

A link-partner loopback (feed TX egress back into RX), multi-BD ring wrap, and error-injection RX
(bad FCS / runt) — all straightforward extensions on this scaffolding.
