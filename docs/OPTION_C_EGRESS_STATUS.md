# Option C DUT-Ethernet egress — status & gap to DoD §5.2 item 4

**DoD §5.2 item 4:** *"the loaded DUT is usable end-to-end: for eth DUTs, its
MAC frames reach the host via the Option C bridge, scored by `macgen`."*

This document records, honestly, what is **proven in simulation**, what is
**stubbed**, and exactly what stands between the (now passing) integration
bench and item 4 **on hardware**. Adopted design: `docs/DUT_ETHERNET_EGRESS.md`
§"Option C". Written 2026-07-10 (agent OPTION-C).

> **SIM vs HW-PROVEN — read this first.** This document is the state of play
> **as written (2026-07-10)**, when everything below that closes item 4 was
> **SIM-only** and the Option C datapath was still absent from the shell BD.
> **Both of those have since changed:** SECTION 5
> (`eth_mac_test_subsystem` — virtual PHY, MDIO model, `gen_checker`) is
> instantiated in `fpga/shell/bd/shell_bd.tcl`, and **DUT reception is proven
> on silicon** (a frame driven at the DUT's RMII is received inside the loaded
> SoC — see `docs/STATUS.md`).
>
> **THIRD UPDATE, 2026-09-11 — the management-FIFO RETURN path now EXISTS.**
> The sentence that stood here for two months ("what is still *not* built is the
> management-FIFO **return** path") is no longer true.
> `fpga/shell/ip/dut_egress/` (DUTEGR @ `0x44B2_0000`) is real RTL, it is in the
> BD, and `tests/dut_egress` proves a **UDP echo end to end**: a datagram
> injected at the bridge's management ingress reaches the DUT's RMII, a DUT MAC
> model echoes it, and the echo comes back through the capture FIFO and is read
> byte-for-byte over AXI4-Lite — with both Internet checksums still verifying.
> That closes step 5 of §5 below in RTL. It is **SIM**: the block has never been
> on a board, because it lands at the next mint. Keep the rule below: no block
> is relabelled `HW-PROVEN` without a named board log. The one adjacent
> HW fact is that the **shell's own** LAN9220 + lwIP + ICMP path pings at 0%
> loss (`PLATFORM_STATUS_AND_ROADMAP.md:183` (internal note, not in the public tree; current state is in `docs/STATUS.md`), board `0x394227AF`) — that is the
> *management* MAC, **not** the DUT-egress datapath.

---

## 1. Proven in simulation — the §8 subsystem bench

`tests/eth_mac_subsystem/` elaborates the whole subsystem
(`fpga/ethernet/eth_mac_test_subsystem.sv` + all five real blocks) and drives
it as a system. **Verified fresh** on 2026-07-10 under the pinned toolchain
(`source set_env.sh` → miniconda py3.10 + cocotb 2.0.1 + VCS 2022.06-SP2):

```
** TEST                                                             STATUS  SIM TIME (ns)  REAL TIME (s)  RATIO (ns/s) **
** test_eth_mac_subsystem.test_a_mdio_phy_bringup                    PASS      153820.00           2.14      72011.50  **
** test_eth_mac_subsystem.test_b_frame_exchange_dut_to_host          PASS       11280.00           0.21      54083.49  **
** test_eth_mac_subsystem.test_b_frame_exchange_host_to_dut          PASS       10000.00           0.15      66148.60  **
** test_eth_mac_subsystem.test_c_link_event_force_down_and_recover   PASS       77040.00           0.92      83776.23  **
** test_eth_mac_subsystem.test_c_link_event_pulse                    PASS       64220.00           0.79      81119.26  **
** test_eth_mac_subsystem.test_d_generator_injects_bad_fcs_to_dut    PASS        7780.00           0.12      63072.97  **
** test_eth_mac_subsystem.test_d_generator_injects_runt_to_dut       PASS        4520.00           0.07      62119.72  **
** test_eth_mac_subsystem.test_d_checker_scores_dut_tx_frames        PASS      141920.00           2.43      58375.59  **
** test_eth_mac_subsystem.test_e_clean_traffic_zero_errors           PASS       32640.00           0.56      57963.92  **
** TESTS=9 PASS=9 FAIL=0 SKIP=0                                                503220.01           7.41      67874.76  **
```

(A `cfs_ident_exec` **segfault** prints during VCS compile — that is the
identifier/`tapi` indexing pass, cosmetic; `simv` builds, elaborates, and runs
all 9 tests. Not a bench or RTL failure.)

**Had it ever run?** Almost certainly not as part of any automated run before
today. The cocotb runner (`list_benches.py` → `make -C tests all`) was **dead
from commit 966c61b until 0d86c1c** (fixed hours earlier the same day), and the
bench is **absent from `BENCH_DIRS`** (so `make clean` never swept it either). A
stale `results.xml` existed in the dir, but nothing in the runner enumerated
this bench. This run is the first confirmed green under the fixed runner.

### What each scenario proves (all SIM)
- **(a)** DUT PHY bring-up over the wired subsystem: BMSR auto-neg-complete +
  link-up + 100/full ANLPAR read over MDIO, and a host `VPHY.PHY_ID` override
  reaches the DUT's next MDIO read (exercising the real AXI⇄mdc CDC at a 100/2.5
  MHz clock ratio).
- **(b)** Frame integrity both ways: DUT→host (RMII→MAC→bridge→uplink AXIS,
  byte-identical) and host→DUT (uplink→bridge→arbiter→MAC→RMII wire frame).
- **(c)** Host `VPHY.LINK_EVENT` force-down/recover and a self-releasing pulse,
  observed by the DUT over MDIO.
- **(d)/(e)** `gen_checker` injects bad-FCS/runt toward the DUT (TX_CNT moves)
  **and** independently scores DUT-TX frames — good→(rx+1,err+0),
  bad-FCS/runt/giant→(rx+1,err+1); clean run scores zero errors. **This is the
  on-chip half of "scored by `macgen`".**

### Runner wiring (done this task)
`eth_mac_subsystem` was added to `BENCH_DIRS` in `tests/Makefile` so
`make clean` sweeps it and the full-set list stays honest. While there, the
other three READY-but-unlisted benches were verified green and added too:
`csr_decode_width` (4/4), `uart_echo_integration` (4/4), `rm_uart_echo` (4/4).
`BENCH_DIRS` now matches the 18 READY benches in `list_benches.py`.

---

## 2. Stubbed / not-yet-real

- **The management-FIFO return path — NO LONGER STUBBED (2026-09-11).** This
  bullet used to say the MicroBlaze seam was "the next increment". It is built:
  `fpga/shell/ip/dut_egress/dut_egress.sv` + `dutegr_cfifo.sv`, instantiated as
  `dut_egress_0` in `shell_bd.tcl` SECTION 3/5 and hung off the interconnect at
  M18. It is NOT the `axi_fifo_mm_s` / AXI-DMA suggested in §4 — a stock
  AXI-Stream FIFO would have had to be taught not to backpressure (see below),
  would not have counted what it dropped, and could not have guaranteed that a
  torn frame never becomes readable. See `fpga/shell/ip/dut_egress/README.md`
  for the three decisions that drove writing it instead.
- **`fpga/ethernet/lan9220_if/lan9220_if.sv`** — 54-line **Phase-0 stub by
  decision** (`TODO(A1)`). It ties `s_axis_tready = 1'b0` and drives its RX
  AXIS to zero. Its own README is explicit: wiring the bridge↔`lan9220_if` edge
  today would **park the bridge's single forwarding sequencer indefinitely**
  (head-of-line block). The register/FIFO access path was *deliberately* handed
  to the `axi_emc_0` vendor cell + the ported `smsc911x` driver, not this RTL.
- **`macgen` control plane** — the host verb + driver
  (`host/pyverify/pyverify/mactest.py::run_mac_test`) and the firmware handler
  (`firmware/…`, `test_macgen.c`) are **HOST-GCC** proven only. `macgen` drives
  GENCHK over AXI-Lite and reads TX/RX/ERR counters back over TCP 6900 — it
  does **not** itself require the LAN9220 uplink (scoring is on-chip).

---

## 3. Shell-integration gap — the four evidenced answers

### Q1 — Is `eth_mac_test_subsystem.sv` instantiated in `fpga/shell/`? **No.**
It appears in the shell *only as a documented TODO*, never as RTL:
- `fpga/shell/bd/shell_bd.tcl:945-964` — **SECTION 5 "ETHERNET
  MAC-VERIFICATION SUBSYSTEM (DEFERRED, out of W-BD scope)"**: the RMII/MDIO
  group is *"NOT wired to fpga/ethernet/{mdio_phy_model,rmii_phy_if,
  link_partner_mac,bridge,lan9220_if,gen_checker} in this file"*.
- Repo-wide, `eth_mac_test_subsystem` matches only its own source and two doc
  lines (`docs/DUT_ETHERNET_EGRESS.md:108,161`) — **zero** hits in
  `shell_top.sv` or the BD. None of the five leaf blocks is instantiated in the
  shell either.

### Q2 — What are `eth_bridge_3port`'s three ports connected to today?
Two different truths — mind the layer:
- **In the shell (hardware), as of 2026-09-11:** port **C (`dut_mac`)** is the
  virtual-PHY datapath (landed 2026-07-24), port **B (`mgmt`)** egress now
  drives `dut_egress_0` — the DUT's return path — with its ingress still tied
  off (nothing in the shell injects yet), and port **A (`uplink`)** is still
  SAFE-TIED with `uplink_m_tready = 1`. The paragraph below is the **2026-07-10
  state**, kept because it is what the four evidenced answers were answering.
- **In the shell (hardware), 2026-07-10:** *nothing.* The bridge is not
  instantiated at all
  (Q1). The RP-facing RMII/MDIO **RX** group is tied to GND constants
  (`shell_bd.tcl:966-975`: `rp_phy_rmii_crs_dv`/`rxd`/`rp_mdio_i` ← `xlconstant`
  0); the RP→static **TX** group (`phy_rmii_txd`/`tx_en`/`mdc`/`mdio_o`/
  `mdio_oe`) is routed through the DFX decoupler with its `*_DATA` outputs
  **left dangling** (`:986-990`) awaiting SECTION 5.
- **In the subsystem RTL (`eth_mac_test_subsystem.sv`, SIM):** the three ports
  are wired as — **C `dut_mac`** ⇄ `link_partner_mac` AXIS (real, internal:
  `.dut_mac_s_*`/`.dut_mac_m_*` at `eth_mac_test_subsystem.sv:~295`); **A
  `uplink`** and **B `mgmt`** are brought straight out as the module's
  top-level `uplink_*` / `mgmt_*` AXIS ports (`.uplink_*`/`.mgmt_*`). Those
  top-level ports are exercised **only by the cocotb bench** today — in a shell
  they are the seams W-BD must attach (uplink→LAN9220 path, mgmt→MicroBlaze).

### Q3 — The uplink: (a) HW `lan9220_if` 2nd master, (b) MicroBlaze forwards, (c) other?
**Recommendation: (b).** See §4 reasoning. Short form: the MicroBlaze already
owns the LAN9220 as the *one* master (`axi_emc_0` at `0xC0000000`, range 16M —
`shell_bd.tcl:1089` — driven by `firmware/smsc911x/`). Option (a) makes a
*second* master on a stateful, sequenced register/FIFO interface → corruption
of the shell's own management link. Option (b) keeps one master and adds a
standard AXIS↔MMIO seam (AXI4-Stream FIFO / AXI-DMA) between the bridge's
uplink port and the MicroBlaze.

### Q4 — Reserved regmap addresses.
Both are **address-map gaps only**, *not* instantiated:
- `shell_bd.tcl:1056` — `# VPHY  @ 0x44A3_0000 — RESERVED, not instantiated
  this wave (SECTION 5)` (the `assign_bd_address` line is intentionally absent;
  neighbours are HWICAP `0x44A2` and OVLSTORE `0x44A4`).
- `shell_bd.tcl:1059` — `# GENCHK @ 0x44A6_0000 — RESERVED, not instantiated
  this wave (SECTION 5)` (between TELEM `0x44A5` and SWDBB `0x44A7`).
- Firmware already assumes them: `firmware/common/platform_regs.h:92`
  `MPS3_VPHY_BASE 0x44A30000`, `:95` `MPS3_GENCHK_BASE 0x44A60000`; contract
  `docs/contracts/shell-regmap.md:53,56`.

> **Doc-drift note.** `docs/DUT_ETHERNET_EGRESS.md` cites stale line numbers for
> these (`shell_bd.tcl:826,829` for the reservations, `:670` for `axi_emc_0`,
> `:730-762` / `:743-749` for SECTION 5). The **current** anchors are: axi_emc_0
> `:861`; reservations `:1056,1059`; SECTION 5 `:945-994`; EMC address map
> `:1089`. Use the numbers in *this* doc.

---

## 4. Uplink decision — reasoning (why (b), not (a))

| Concern | (a) HW `lan9220_if` as 2nd LAN9220 master | (b) MicroBlaze forwards bridge-uplink ↔ LAN9220 |
|---|---|---|
| **Two-master conflict** | **Fatal.** The LAN9220 host bus is a *sequenced* register/FIFO protocol (read TX_FIFO_INF → write TX FIFO → read RX status/FIFO…). A HW sequencer racing the firmware `smsc911x` driver on the same registers (whether as a 2nd AXI master on `axi_emc_0/S_AXI_MEM` or a 2nd SMC master on the pads) has no shared FIFO/interrupt state → interleaved accesses corrupt **both** the DUT-egress *and* the shell's own management/reconfig/debug link (all share this one 10/100 port). | **One master.** The MicroBlaze remains the sole LAN9220 master; DUT frames are just more frames it TXes/RXes via the existing driver. No new arbitration, no shared-state hazard. |
| **Shutdown-Manager isolation** (a broken DUT MAC must not wedge the shell) | Weaker. A HW master fed straight from the DUT-derived AXIS can be driven into pathological register sequences by a malformed/flooding DUT MAC, potentially wedging the shared LAN9220 the shell needs to stay reachable. | Stronger. The bridge-uplink AXIS drains into an MMIO FIFO the MicroBlaze *polls*; a flooding/stalled DUT MAC just backpressures the bridge (and the DFX decoupler already clamps the RMII/MDIO RP pins). The shell's management path is independent register traffic and stays alive. |
| **Effort / risk** | **High.** Write + verify a full LAN9220 register/FIFO master in RTL **and** an arbitration scheme against firmware — for the one link whose correctness gates the whole platform. | **Low–Medium.** Reuse the already-ported `smsc911x` driver; add a stock Xilinx **AXI4-Stream FIFO** (`axi_fifo_mm_s`) or **AXI-DMA** in the BD to expose the bridge's `uplink` AXIS as an MMIO seam; add a firmware forwarding loop. Mostly integration + firmware. |

**Also note (scope-splitting the DoD):** *"scored by `macgen`"* is satisfied by
GENCHK counters read over the `macgen` verb — **on-chip, no LAN9220 needed**.
Only the literal *"MAC frames reach the host"* clause needs the uplink seam. So
item 4 can be de-risked in two independent steps (§5): stand up VPHY+GENCHK+the
virtual-PHY datapath first (closes the scored-by-macgen half and is the real
"MAC verification"), then add the uplink forwarding seam for physical
reachability.

(Option (a) is not *forbidden* — a future single-master design could put the
LAN9220 entirely behind HW and drop the firmware driver — but that is a larger
re-architecture than item 4 needs, and it collides with the working, HW-proven
`smsc911x`/lwIP management path. Not recommended for closing item 4.)

---

## 5. Ordered task list to close DoD §5.2 item 4

All shell-BD/firmware steps are **owner decisions** — a BD edit re-mints
`static_id` and a shell rebuild is already pending, so these are *proposed*, not
done here. Steps 1–2 are the sim/runner work already landable.

1. **[DONE, this task]** Prove the subsystem bench green and wire it into the
   runner (`BENCH_DIRS`) so it cannot silently rot. → §1.
2. **[sim, no BD]** Add a subsystem-level assertion that the bridge `uplink`
   AXIS carries the DUT frame *and* GENCHK RX/ERR agree on the same frame in one
   run — i.e. bind the on-chip "scored" evidence to the "egressed" evidence in a
   single scenario. (Optional hardening; the two are already separately proven.)
3. **[shell BD — SECTION 5, owner]** Instantiate the virtual-PHY datapath in the
   **static** region behind the Shutdown Manager: replace the GND tie-offs
   (`shell_bd.tcl:966-975`) and connect the dangling decoupler `*_DATA` outputs
   (`:986-990`) into `rmii_phy_if`/`mdio_phy_model`; instantiate the bridge,
   `link_partner_mac`, and `gen_checker` (or drop in `eth_mac_test_subsystem`
   wholesale). Keep `rp_phy_rmii_ref_clk` on the shell 50 MHz (`:261`).
4. **[shell BD — regmap, owner]** Add **VPHY** and **GENCHK** as AXI-Lite slaves
   and drop the two reserved `assign_bd_address` lines at `0x44A3_0000` /
   `0x44A6_0000` (`:1056,1059`). Per subsystem header **R4**, GENCHK needs an
   **AXI4-Lite clock-converter** in front (it fuses control+datapath clock at
   refclk; the MicroBlaze bus runs faster) — or split GENCHK's clocks. This
   closes the **"scored by `macgen`"** half (host `macgen` verb → GENCHK
   counters).
5. **[mgmt seam — DONE in RTL 2026-09-11; uplink seam still open]** The `mgmt`
   AXIS is exposed to the MicroBlaze by `dut_egress_0` (DUTEGR @ `0x44B2_0000`),
   **not** by a stock AXI4-Stream FIFO. The reason is the head-of-line hazard
   this document names twice: the bridge has ONE store-and-forward sequencer, so
   the sink on this port must never backpressure — which means it must be
   allowed to DROP, which means it must COUNT what it drops, and must guarantee
   that a partial frame never becomes readable. A stock FIFO does none of those
   three. `tests/dut_egress` proves all three, and mutation-checks each.
   The **uplink** AXIS is still SAFE-TIED; `lan9220_if.sv` stays a stub and the
   LAN9220 keeps its single `axi_emc_0`+`smsc911x` master. Do **not** wire
   `bridge.uplink → lan9220_if` (head-of-line hazard, §2).
   **Host → DUT injection** has a fabric path from mint 3
   (`HANDOVER_DUT_INJECT.md`, amendments A1/A2):
   - `dut_egress_0`'s inject side drives `mgmt_s_*`;
   - only the uplink's `*_s_*` inputs stay tied;
   - on older statics `mgmt_s_*` is still tied (`tvalid = 0`), so `gen_checker`
     is the only sender there;
   - the 6900 verb that would drive it is a firmware change, not a fabric one,
     and is not built.
6. **[firmware — owner, other track active]** Forwarding loop: copy frames
   between the bridge-uplink FIFO and the LAN9220 TX/RX FIFOs via the existing
   `smsc911x` driver; set the LAN9220 **promiscuous** so it carries the DUT MAC
   alongside the shell management MAC. Closes the **"frames reach the host"**
   half.
7. **[decoupler]** Confirm the RMII-TX (`phy_rmii_txd`/`tx_en`) + MDIO
   (`mdc`/`mdio_o`/`mdio_oe`) RP→static outputs traverse the DFX decoupler
   (already enumerated, `shell_bd.tcl:986-990`) so a mid-swap DUT cannot inject
   junk — the isolation guarantee for step 3.
8. **[shell rebuild + board bring-up]** Rebuild the shell (re-mints
   `static_id`), load an eth DUT (`rm_eth_ss`, already `pr_verified`), and run
   `pyverify.mactest` / the `macgen` verb against the live board. **Only then**
   may any of this move from `SIM` to `HW-PROVEN`, and only with a named board
   log recorded here.

**Bottom line (2026-09-11).** Steps 3, 4 and the `mgmt` half of step 5 have
landed in the BD. The DUT's return path exists in RTL and a **UDP echo is proven
end to end in simulation** (`tests/dut_egress`: 4/4 e2e + 8/8 block-level, both
controls seen to fail, four RTL mutations caught). What remains for item 4 is:
the firmware half (a `dutrx` verb on 6900 draining DUTEGR — designed, diffed and
NOT applied, see the EGRESS lane's RESULT.md), the LAN9220 uplink forwarding
loop (step 6, for the literal "reach the host" clause), and **a board**. The
block has never been on silicon; it lands at the next mint. Nothing here may be
relabelled HW-PROVEN without a named board log.

The half of item 4 that reads *"scored by `macgen`"* and the half that reads
*"its MAC frames reach the host"* are now bound together in ONE bench scenario
(`test_d_scored_and_egressed_agree`), which is what §5 step 2 above asked for
and nobody had done: GENCHK's `RX_CNT` at the DUT-TX tap and DUTEGR's
`RX_FRAMES` at the bridge's management egress are asserted to be counting the
same frames, with `ERR_CNT == 0`.
