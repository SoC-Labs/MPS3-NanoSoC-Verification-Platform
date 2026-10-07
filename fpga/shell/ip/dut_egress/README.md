# `dut_egress` — the DUT's Ethernet return path (DUTEGR @ `0x44B2_0000`)

**Status: REAL RTL, SIM-PROVEN, NOT YET ON SILICON.** It is in the block design
(`fpga/shell/bd/shell_bd.tcl` SECTION 3 + SECTION 5) and therefore lands at the
next mint; nothing here may be called HW-PROVEN without a named board log.

**The inject side (host → DUT, 2026-09-23) is RTL + sim only, and is NOT YET IN
THE BLOCK DESIGN.** Its four `inj_m_*` ports reach the bridge only once the
SHELL lane applies `INTEGRATION/01-shell_bd-section5-inject.patch` (mint 3).
Until then the shipped BD leaves them unconnected: the three outputs dangle
(legal), and `inj_m_tready` takes its IP-XACT default driver value (0 unless the
packager sets one — not checked in Vivado by this lane), so the framer never
starts and nothing leaves; the RX side is unaffected either way. See
[The inject side](#the-inject-side-host--dut) below and `INTEGRATION/INTEGRATION.md`.

## Why this block exists

DUT *reception* has been silicon-proven since 2026-07-30: the shell's virtual
PHY drives the DUT's RMII and `DFXCTL.RM_STATUS[2]` was observed going 0 → 1
under `gen_checker` traffic on the fielded static.

The **return** path had never been built. `shell_bd.tcl` SECTION 5 tied
`eth_bridge_3port`'s management egress to `mgmt_m_tready = 1` with nothing
behind it — so every frame the DUT transmitted arrived at that net and was
drained into a constant. **A DUT could be talked to and could not answer.** That
was the single biggest functional hole in the platform, and this block is the
consumer that tie-off was standing in for.

Design lineage: `docs/DUT_ETHERNET_EGRESS.md` (Option C),
`docs/OPTION_C_EGRESS_STATUS.md` §5 step 5 ("expose the bridge's AXIS to the
MicroBlaze via an MMIO seam").

## Files

| file | what |
|---|---|
| `dut_egress.sv` | AXI4-Lite CSR surface + capture FSM + the counters, and the inject side's staging, framer and counters |
| `dutegr_cfifo.sv` | dual-clock gray-pointer FIFO **with commit/rollback** (four instances: RX data/desc, TX data/desc) |
| `INTEGRATION/` | the patches the inject side needs in files this lane does not own (BD, regmap views, contract prose) and the OOC synth script |

`dut_egress.sv` `` `include ``s `dutegr_cfifo.sv` (the `uart_bridge.sv` /
`mdio_phy_model.sv` idiom) so a bench Makefile can list one file; the helper
stays a genuinely separate, independently lintable module. **Both** files are in
the packaged fileset (`ip_packaged/package_csr_ip.tcl`) — packaging the top
alone passes `validate_bd_design` and dies in synth with "module
`dutegr_cfifo` not found".

## The three decisions worth reading before changing anything

### 1. `frm_tready_o` is a constant 1. It must stay one.

`eth_bridge_3port` is store-and-forward with **one** shared round-robin
sequencer and head-of-line blocking by design, and it floods
broadcast/unknown-destination frames to every non-ingress port. A sink that
backpressures this port does not apply flow control — it **parks the whole
bridge**, killing the DUT's ingress scoring and the LAN9220 uplink along with
the egress it was trying to protect. The same reasoning is already written into
`shell_bd.tcl` SECTION 5 for the tie-off this block replaces.

`tests/dut_egress` mutation-checks this: replacing the constant with
`(data_wfree != 0)` makes the end-to-end overflow scenario fail with *"only 2
frames reached the capture port"* — the bridge parked, exactly as predicted.

### 2. Because it drops, it must never drop **silently**.

Exactly one of three counters moves for every frame the block accepts, so

```
RX_FRAMES + DROP_FULL + DROP_GIANT  ==  frames seen while CTRL.EN
```

is an invariant, asserted directly in both bench arms against a monitor on the
capture port. `STATUS.OVF` is the one sticky bit a polling loop needs.
`DROP_GIANT` is reported in preference to `DROP_FULL` when both apply: a giant
is a DUT defect, the FIFO filling behind one is a consequence of it.

### 3. Store-and-forward, so a torn frame can never be observed.

A frame's length is not known until its last beat, so `dutegr_cfifo` keeps a
separate **commit** pointer and publishes only that to the read domain. Bytes
are written speculatively; a frame that runs out of room, or that exceeds
`MAX_FRAME`, is **rolled back whole** and counted. Removing the rollback (a
mutation) is caught by the benches: the survivors stop being byte-identical to
what went in.

The read side has two independent records of where a frame ends — the stored
end-of-frame bit in the data FIFO, and the descriptor FIFO's byte count. That
redundancy is a **check**, not drift: if they disagree, `STATUS.DESYNC` latches.

## Clocks, CDC and reset

| domain | what |
|---|---|
| `s_axi_aclk` (100 MHz) | AXI4-Lite surface, the counters, the read side |
| `rmii_clk_i` (50 MHz) | the capture port (the domain the bridge runs in) |

Crossings, all `(* ASYNC_REG *)`:

* the FIFO gray pointers (both directions, both FIFOs);
* `CTRL.EN`, a single level, sampled only at a frame's **first** beat — so
  toggling it mid-frame cannot produce a half-captured frame;
* the frame events (`commit` / `drop_full` / `drop_giant`) as **toggles** with
  edge detection, the counters themselves living in the AXI domain (so
  `CTRL.CLR_CNT` is a local synchronous clear with no crossing at all). The
  pattern's one requirement is that events be further apart than the
  destination's sync depth: these are whole Ethernet frames, so the closest two
  can be is a 64-byte frame plus the 96-bit IFG — ~300 `rmii_clk` cycles against
  3 `s_axi_aclk` cycles;
* `DATA_FULL` / `DESC_FULL`, slow status levels, plain 2-FF.

**One reset.** Like `uart_bridge.sv`, the block takes only `s_axi_aresetn` and
derives the `rmii_clk` reset from it (async-assert / sync-deassert), so the two
sides of a FIFO can never disagree about pointer state across a reset.

`fpga/shell/constraints/mps3_harness_timing.xdc` deliberately does **not** split
`clk_wiz_shell/clk_out1` and `clk_out2` into asynchronous groups — they are two
outputs of one MMCM — so every crossing in this block is a **timed** path, not a
waived one. No new XDC is required, and that is the point: the numbers in
`docs/OPTION_C_EGRESS_STATUS.md` are measured, not excluded.

## The inject side (host → DUT)

`docs/planning/HANDOVER_DUT_INJECT.md` is the contract; this is why the RTL is
the way it is. Seven registers on the **same page** (`0x20`–`0x38`: `TX_CTRL`,
`TX_STATUS`, `TX_SPACE`, `TX_DATA`, `TX_FRAMES`, `TX_REJECT`, `TX_FLUSHED`), a **second
`dutegr_cfifo` pair with the clocks swapped** (write = `s_axi_aclk`, read =
`rmii_clk_i`), and a small framer on the new `inj_m_*` port, which
`shell_bd.tcl` SECTION 5 wires to the bridge's port-B **ingress**
(`eth_mac_test_subsystem.mgmt_s_*`). The RX map (`0x00`–`0x1C`) is unchanged
byte for byte; the RX `DATA` pop still decodes at `0x10` and nowhere else;
`0x3C`–`0xFFFC` are unmapped (read 0, no side effect).

**Handover amendments A1 and A2, decided by david 2026-09-23**, are built in:
A1 caps RAW = 0 at 1514 staged bytes (§6 below); A2 adds `TX_FLUSHED` at
`0x38` and takes flushed frames out of `TX_REJECT` (§7 below).

### 4. The RX side may never stall. The inject side may wait.

Decision 1 above is about a **sink**: a stalled sink parks the bridge's one
shared sequencer, so `frm_tready_o` is a constant 1. `inj_m_*` is a **source**
into a per-port ingress buffer. Waiting on `inj_m_tready` holds up our own frame
and nothing else — the sequencer only ever serves *completed* ingress frames.
So the framer waits, and what it must never do instead is **start a frame it
does not wholly have**: it starts only when a committed descriptor exists **and
every byte it describes is visible** in the data FIFO (`txr_whole`), and from
then on streams the frame back to back.

It checks `inj_m_tready` *before* raising `tvalid`. AXI-Stream in general
forbids a master waiting for TREADY (a slave may wait for TVALID → deadlock),
but `eth_bridge_3port`'s `tready` is its own `recv_q` and never waits for
`tvalid`, so there is no deadlock to fear — and the payoff is that `FLUSH`
stays bounded: a frame that has been *presented* must stay presented until
taken, one that has not can still be discarded. Once started, the bridge keeps
`tready` high to `tlast` (its buffer is a whole frame); if some other sink
stalled mid-frame the framer holds `tvalid`/`tdata`/`tlast` until taken, which
`tests/dut_egress` checks.

### 5. Frame-atomic by construction: a one-byte holding register.

The data FIFO stores `{end_of_frame, byte}`, and which staged byte is the last
is not known until `COMMIT`. So the newest byte is **held**, not pushed: the
next `TX_DATA` write pushes it with eof = 0, and a good `COMMIT` pushes it with
eof = 1 **and publishes the frame in the same cycle** (`dutegr_cfifo`'s
`wcommit_i` publishes through that cycle's push). Staged bytes are therefore
invisible to the framer until the whole frame, eof included, is committed. A
push that finds the FIFO full dooms the frame; at `COMMIT` it is **rolled back
whole** and counted, like an empty, too-short or too-long commit. `ABORT` rolls
back without counting. As on the RX side, the eof bit and the descriptor length
are two records of where a frame ends, and `TX_STATUS.DESYNC` latches if they
ever disagree.

### 6. RAW = 0 builds the FCS; RAW = 1 sends bytes verbatim.

`link_partner_mac` neither inserts nor strips an FCS (its AXIS frames carry one
in both directions — `link_partner_mac.sv`'s header, "AXIS FRAME CONVENTION"),
so without an append every injected frame would reach the DUT with a bad CRC.
With `TX_CTRL.RAW = 0` the framer zero-pads to 60 bytes and appends the IEEE
CRC-32 (the same `crc32_byte` as `link_partner_mac.sv`, FCS = `~crc` sent LSB
first) — byte-exact against `tests/common/frames.py`'s `build_frame`.

**Limits (amendment A1).** RAW = 0: 14 ≤ staged ≤ **1514**, so the wire frame
is 64 … 1518 bytes — only frames an 802.3 MAC accepts. (The handover's original
`MAX_FRAME − 4` = 1532 put 1536-byte giants on the wire from the "normal"
mode.) RAW = 1: 1 … `MAX_FRAME` (1536), oversize **on purpose** — the staged
bytes go out exactly as written, no pad and no FCS: the fault-injection mode
(bad FCS, runts, giants). A commit outside its mode's limits is rejected whole.
`tests/dut_egress` test_r checks all four edges (1514 ✓, 1515 ✗, 1536 ✓,
1537 ✗). The RAW a COMMIT uses is the one **its own write carries**; `RAW` is an
ordinary rw bit, so write `COMMIT` as `(raw << 8) | 1`.

### 7. Every COMMIT is accounted for — including the ones FLUSH discards.

```
TX_FRAMES + TX_REJECT + TX_FLUSHED + frames_queued  ==  COMMITs since TX_CTRL.CLR_CNT
```

(amendment A2). Every COMMIT ends in exactly one bucket: `TX_REJECT` (refused
at COMMIT: empty, too short, too long, no room), `TX_FRAMES` (handed to the
bridge whole), or `TX_FLUSHED` (`0x38`: accepted, then discarded by
`TX_CTRL.FLUSH` before it started). `frames_queued` is
`FRAME_DEPTH − TX_SPACE[31:16]`: a descriptor is popped at its frame's
**tlast**, not at its start, so a frame on the wire still counts as queued.
`REJ` means "`TX_REJECT` has moved" — a FLUSH does not set it. `CLR_CNT` clears
all three counters and both sticky bits. The identity holds exactly at
quiescent points; issue `CLR_CNT` with `TX_STATUS.EMPTY` set, or the frames
already queued will land in the new count. ABORT, and COMMIT|ABORT in one write
(ABORT wins), are not COMMITs.

**FLUSH is exact.** It discards every frame **committed before the FLUSH write
(or in the same write)** that has not started by the time the request reaches
the framer — and nothing committed after it. The AXI side counts accepted
COMMITs where they happen and sends the count at the FLUSH write across with
the request as a target (a bundled-data crossing: written in the cycle the
request rises, stable until the acknowledge has come back down, and CAPTURED
into an rmii-domain register once, clock-enabled by the synchronized request's
rising edge); the framer counts frames finished (sent or discarded) and drains
until it reaches the captured target, waiting for an owed frame whose descriptor is still crossing rather
than acknowledging without it. (The first version acknowledged when the
descriptor FIFO *looked* empty, so a frame committed a few cycles before FLUSH
could escape it — harmless to the invariant, fatal to `TX_FLUSHED` being an
exact count.) A frame that has started is never cut: it finishes and counts in
`TX_FRAMES`. Bounded: at most `FRAME_DEPTH` frames can be owed. A four-phase
level handshake (`FLUSH_BUSY` until the acknowledge has returned to zero); a
FLUSH written while `FLUSH_BUSY` is **ignored** — poll it. Staged bytes are
untouched (that is `ABORT`). Software's swap rule (handover §4 rule 5): stop
injecting at the swap FSM's GATE, write `FLUSH` after DONE.

### 8. Routing reality (fixed table, no learning — handover §4 rule 4)

What `eth_bridge_3port` does with an injected frame (proven by
`tests/dut_egress` `BLOCK=e2e` test_e):

* the DUT's **real** MAC is not in the table → **floods** to port C (the DUT)
  and to the tied-off port A (drained);
* broadcast floods the same way;
* the table's `DUT_MAC` (`02:00:00:00:00:02`) routes to the DUT only;
* `MGMT_MAC` (`02:00:00:00:00:01`) resolves only to its own ingress port, so the
  bridge **drops it silently** — and it still counts in `TX_FRAMES`, because
  `TX_FRAMES` counts what DUTEGR handed the bridge. Expected, not a bug.

### Inject-side CDC

All new crossings follow the idioms above, all `(* ASYNC_REG *)`: the two new
FIFOs' gray pointers; `FLUSH` request/acknowledge (registered levels, 2-FF each
way) with its bundled target (not a synchronizer: stable before and throughout
its use, and captured by a clock-enabled register, see §7); and three rmii →
AXI **toggle** events (frame sent, frame flushed, desync) into AXI-domain
counters, so `TX_CTRL.CLR_CNT` is local.

**Measured by `report_cdc` (OOC, routed, `INTEGRATION/ooc_synth.tcl`,
2026-09-24): zero CDC-1 / CDC-10 criticals.** Two earlier versions of the
FLUSH crossing were not clean, and the current one is shaped the way it is
because of them. First, the framer read the target combinationally: 73 CDC-1
criticals, fanning into the FSM's clock enables. Second, the capture's
rising-edge detector read the request synchronizer's FIRST stage, and one
CDC-1 demoted the synchronizer to "unknown circuitry". What remains is expected:

* CDC-15 ×6 — the enable-controlled target capture, which is the recognised
  structure;
* CDC-6 ×8 — gray pointers with `ASYNC_REG`;
* CDC-26 ×34 — the 16-deep descriptor FIFOs' LUTRAM, where pointer discipline
  prevents a collision; the same pattern as the RX side;
* one pre-existing RX-side CDC-15: `STATUS.EN` reads the rmii-domain
  `en_sync_q[1]` directly. The framer's `GAP`
state spaces frame ends by ≥ 4 rmii cycles (8 AXI cycles against a 3-deep
synchronizer) even for back-to-back 1-byte RAW frames. Everything the software
reads in `TX_STATUS`/`TX_SPACE` except `DESYNC` and `FLUSH_BUSY` is **write-side,
hence AXI-domain, state** — no crossing at all. The rmii-domain reset is the
same derived `rmii_rst_n` (one reset, `s_axi_aresetn`).

Residual, not handled: the bridge's reset (`proc_sys_reset_rmii`) and this
block's are different sources. A frame injected while the bridge alone is in
reset would be lost or truncated. Both come from `sys_rst_n` and nothing
commits before firmware runs, so this is a power-up-only window.

## Sizing

`DATA_DEPTH = 2048` bytes, `FRAME_DEPTH = 16` frames, `MAX_FRAME = 1536` bytes —
the **same three parameters size both sides**, so the inject side adds exactly
one more 2048 × 9 data FIFO (one more RAMB18) and a 16 × 17 descriptor FIFO.
The data FIFO's read port is **registered** (`rdata_q <= mem[rptr_next]`), which
is what lets 2048 × 9 infer a block RAM; a combinational `mem[rptr_q]` read
cannot map to a BRAM at all and would cost several hundred LUTs of cascaded
distributed RAM.

Throughput: one byte per AXI-Lite read. At ~10 cycles per read that is on the
order of 10 MB/s — far above the shared 10/100 LAN9220 uplink this path feeds,
and this is functional MAC verification, explicitly not a throughput benchmark.
A word-wide pop register is the obvious future addition; it is not needed yet
and an unused register is a register that can be wrong.

## Register map

`docs/contracts/shell-regmap.md`'s **DUTEGR** section is the contract; the
offsets there are generated from this file's `IDX_*` decode by
`tools/gen_regmap.py`. Do not restate them anywhere.

## Benches

`tests/dut_egress/`, two arms, both in `make -C tests`:

* `BLOCK=csr` — the block alone at the shipped `C_S_AXI_ADDR_WIDTH=32`
  (bug #1), the destructive-read decode, the FIFO, the drop counters, `EN`,
  `FLUSH`; and the inject side: TX decode, COMMIT/ABORT/FLUSH/every reject,
  the §3 invariant with `TX_FLUSHED` (also under a seeded random sequence in
  which every FLUSH must discard at least one parked frame), the A1 length
  edges, RAW = 0 pad + FCS byte-exact against `frames.py`, RAW = 1 verbatim,
  nothing before COMMIT, waiting on tready, and TX/RX independence — all
  against a monitor on `inj_m_*` (`tests/dut_egress/dutegr_tx.py`).
* `BLOCK=e2e` — `tb_dut_egress.sv` wires this block to the whole §8 subsystem
  exactly as `shell_bd.tcl` SECTION 5 does **with the §5 inject delta applied**,
  and a **UDP datagram written to `TX_DATA` is echoed by a DUT MAC model and read
  back byte for byte out of `DATA`** — registers at both ends. Plus routing
  reality, RAW = 1 to the wire, and both directions at once.

Controls must be **seen to fail**; a green bench with no failing control proves
nothing: `make control-byteorder`, `make control-nodrop` (RX), and for the
inject side `make control-tornframe` (the read side starts before COMMIT),
`make control-norollback` (rollback removed), `make control-noflushed`
(`TX_FLUSHED` removed: `0x38` reads 0) and `make control-flushasreject` (the
pre-A2 accounting) — all four rebuild the csr arm against RTL mutated by
`tests/dut_egress/mutate.py` at the lines tagged `MUTATION-POINT` — and
`make control-raw` (RAW forced to 1 end to end: the DUT MAC model fails the
frame on its FCS).
