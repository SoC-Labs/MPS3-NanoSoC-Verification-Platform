# nanoSoC DUT Ethernet egress — architecture decision

> **Status: HISTORICAL** — a record of the nanoSoC DUT Ethernet egress architecture decision (the choice of Option C) as of 2026-07-09.
> Superseded by / current state in [docs/OPTION_C_EGRESS_STATUS.md](OPTION_C_EGRESS_STATUS.md) and [docs/STATUS.md](STATUS.md).
> Kept for provenance; do not update.
> Cites `PLATFORM_LIVE_STATUS.md` — internal notes, not in the public tree.
>
> **One banner line added 2026-09-11, and nothing else — the body below is left
> exactly as written.** The RETURN half of the datapath this document chose
> ("the 3rd bridge port = MicroBlaze/lwIP management", §2 Option C) now EXISTS
> in RTL: `fpga/shell/ip/dut_egress/` captures the frames the DUT transmits out
> of the bridge's management egress into a FIFO the MicroBlaze reads (DUTEGR
> @ `0x44B2_0000`), and a UDP echo is proven end to end in simulation by
> `tests/dut_egress`. Until 2026-09-11 that net was tied to `mgmt_m_tready = 1`
> with nothing behind it, so a DUT could be talked to and could not answer.
> Current state: [docs/OPTION_C_EGRESS_STATUS.md](OPTION_C_EGRESS_STATUS.md).
>
> **Second status line, 2026-09-14 — the HOST half now exists too.** The
> firmware and host halves the 2026-09-11 commit designed and deliberately did
> not apply are applied: the `dutrx` verb at **net-protocol v0.10**
> ([docs/contracts/net-protocol.md](contracts/net-protocol.md) §"DUT egress"),
> chunked at 256 bytes, with `pyverify dut-eth` reading whole frames out over
> the wire. A DUT's frames now reach a host process — **in simulation and
> against the reference server only**. The block has still never been on a
> board: it lands at the next mint, and on today's fielded bitstream the verb
> DECLINES (`{"ok":false,"err":"dut_egress not present"}`) rather than
> reporting an empty FIFO that does not exist. Nothing here may be relabelled
> HW-PROVEN without a named board log.
>
> **Third status line, 2026-09-23 — a DUT that sends.** Until today no RM
> could exercise this path on silicon: `rm_eth_ss` has no CPU, and its
> bring-up FSM never readied a TX descriptor, so 2026-09-22 saw reception
> proven and `DUTEGR RX_FRAMES = 0`. From the RM built after 2026-09-23,
> `fpga/rp/eth_ss/eth_ss_bringup.sv` makes the MAC send one IEEE 802.3x PAUSE
> frame per second with no CPU (the MAC builds it from registers; details and
> the reason it is a PAUSE frame in `fpga/rp/eth_ss/README.md` "Transmit
> beacon"). It is 64 bytes with FCS: dst `01:80:C2:00:00:01`, src
> `32:53:45:4C:53:02`, EtherType `0x8808`, opcode `0x0001`, pause time = a
> sequence number counting 1, 2, 3 … big-endian in bytes 16-17, 42 zero pad
> bytes, FCS. The bridge floods it (multicast) into DUTEGR. Simulated end to
> end, byte for byte, through the unmodified RM and the shell composition
> (`tests/eth_ss_bringup`, `make ARM=tx`); not yet seen on a board.
> **What the board proof reads, with `rm_eth_ss` resident:** (1) `DUTEGR
> RX_FRAMES + DROP_FULL` climbs by about 1 per second (10 s ≈ 10 frames).
> Undrained, the FIFO holds about 16 frames (`FRAME_DEPTH`), so after about 16 s `RX_FRAMES` stops
> and `DROP_FULL` takes over. That is the FIFO filling, not the DUT going
> quiet. (2) `dutrx` returns `len` 64 and `data` starting
> `0180c20000013253454c530288080001`, then the sequence number as four hex
> digits (`0001`, `0002` …), then zeros and the FCS. Two
> reads in a row show consecutive sequence numbers, so the frames are live and
> not one stale frame. A frame with that source address and EtherType can only
> have come from the RM.

> **Status:** DESIGN (agent-authored, no Vivado/HW). Fills gap #3 of
> `PLATFORM_LIVE_STATUS.md` §5 ("nanoSoC DUT ethernet egress architecture").
> Resolves the ❓ in the component table (`PLATFORM_LIVE_STATUS.md:98`).
>
> **Question:** when the nanoSoC SoC (or the `eth_ss` subsystem) is loaded into
> the RP, its **own** Ethernet MAC drives an RMII interface across the partition
> boundary. How does that reach the outside world, separate from the shell's own
> LAN9220 management NIC?
>
> **Ground truth:** `docs/contracts/partition-pins.md` v0.1,
> `fpga/monolithic/nanosoc_mps3.xdc`, `docs/mps3_nanosoc_verification_platform_architecture.md`
> §8, and the already-authored RTL in `fpga/ethernet/`.

---

## 1. The three physical facts that decide this

1. **The MPS3 has exactly ONE Ethernet interface, and it is not RMII.** The XDC
   brings out only the SMSC **LAN9220** — `ETH_nCS`/`ETH_nOE`/`ETH_INT`
   (`nanosoc_mps3.xdc:33-38`) plus the shared `SMBF_*` static-memory bus
   (`nanosoc_mps3.xdc:44-97`). The LAN9220 is a **memory-mapped MAC+PHY**
   (register/FIFO host interface), *not* an MII/RMII interface presented to the
   fabric (spec §3, arch doc line 56: "memory-mapped … not MII/RMII to fabric").
   There is **no second RJ45, no MII/RMII/GMII PHY, and no FMC/PMOD Ethernet**
   pinned anywhere in the XDC. The only expansion headers present are the
   Arduino-style shield (`SH*_IO`, `nanosoc_mps3.xdc:405,482-489`) and CLCD/HDMI/
   audio — none an Ethernet PHY.

2. **The DUT's RMII is a partition-pin group, and its REF_CLK is
   shell-sourced.** `partition-pins.md:54-66` puts `phy_rmii_*` + `mdc`/`mdio_*`
   on the RP boundary with the shell as **virtual PHY**; `phy_rmii_ref_clk` is
   an **output of the shell** (partition-pins.md:57 "50 MHz REF_CLK sourced by
   shell (DUT = ref-in)"), already wired from `clk_wiz_shell/clk_out2`
   (`fpga/shell/bd/shell_bd.tcl:261`). The DUT is configured **ref-in**; there is
   no external PHY handing it a clock.

3. **The RMII never reaches a physical pad in this build.** By design the virtual
   PHY is fabric-only (`fpga/ethernet/rmii_phy_if/README.md:73-84`: "This MPS3
   build's RMII never reaches a physical pad … so there is no OLOGIC/pad"). The
   RP boundary is deliberately pad-agnostic so a new RM never re-pins the board
   (partition-pins.md:96-102, I4 "shell brokers all board I/O").

Those three facts rule out the obvious "just wire the DUT's Ethernet to a jack"
answer and point straight at a MAC-level (frame) bridge.

---

## 2. Options, feasibility, verdict

### Option A — a second physical PHY / RJ45 on the MPS3

Give the DUT its own MII/RMII PHY + magnetics + jack, driven from FPGA pads.

- **Feasibility:** the board provides **no** such interface (§1.1). It would need
  external hardware: either an FMC Ethernet mezzanine (no FMC Ethernet pins are
  in `nanosoc_mps3.xdc`) or an RMII PHY breakout wired to the shield header
  (`SH*_IO`, LVCMOS18, ~limited pins) with its own magnetics/RJ45. Then: new XDC
  pin group, a fabric-side pad ring, and — because pads must stay **static** in a
  DFX design (partition-pins.md:103-109, HDPR-29) — the PHY-facing IOBs still
  have to live in the shell, so the DUT's RMII *still* crosses the partition as
  fabric signals exactly as today. The PHY buys nothing the virtual PHY doesn't,
  and adds procurement + a board spin.
- **Effort:** HIGH (hardware procurement + new constraints + bring-up).
- **Verdict:** ❌ **Reject for v1.** Not supported by the board; adds hardware for
  no architectural gain. Revisit only if line-rate/real-PHY conformance (not
  functional MAC verification) becomes a goal.

### Option B — share the shell's LAN9220 *directly*

Connect the DUT's RMII to the LAN9220.

- **Feasibility:** **impossible as stated.** The LAN9220 exposes a memory-mapped
  register/FIFO host bus to the fabric (`ETH_*` + `SMBF_*`), not an RMII/MII port
  you can splice the DUT's RMII into (§1.1; arch doc line 56, 147). "Sharing" the
  LAN9220 is only achievable at the **frame/MAC layer**: terminate the DUT's RMII
  in fabric, recover its frames as AXI-Stream, and forward them through the
  LAN9220's host interface (promiscuous). That is precisely Option C. So B is not
  a distinct datapath — it is the *goal* (one physical port carries both MACs)
  whose only realisation is C.
- **Effort:** n/a (reduces to C).
- **Verdict:** ⚠️ **Achievable only via Option C.** Two MACs behind one physical
  port is normal Ethernet — the upstream switch learns both (arch doc line 262);
  the LAN9220 just has to run **promiscuous** (arch doc line 260).

### Option C — virtual-PHY / MAC-level verify path (spec §8)

The shell **terminates** the DUT's RMII with a fabric virtual PHY, recovers the
DUT's frames as AXI-Stream via a link-partner MAC, and 3-port-bridges them onto
the LAN9220 uplink (and to the MicroBlaze/lwIP management stack). Datapath (arch
doc §8.2, line 256):

```
DUT MAC (RP) ⇄ RMII+MDIO (partition pins) ⇄ rmii_phy_if + mdio_phy_model (virtual PHY)
            ⇄ link_partner_mac (frames→AXIS) ⇄ eth_bridge_3port
            ⇄ lan9220_if (AXI⇄SMBF) ⇄ LAN9220 (promiscuous) ⇄ RJ45 ⇄ host/network
                     │
                     └─ 3rd bridge port = MicroBlaze/lwIP management
                     └─ gen_checker tap = independent TX/RX frame scoring (spec §8.4)
```

- **Feasibility:** **HIGH — most of it already exists.** Per
  `fpga/ethernet/README.md`, **five of six blocks are real, benched RTL** (green
  under VCS + cocotb, verilator `-Wall` clean): `rmii_phy_if/`,
  `mdio_phy_model/`, `link_partner_mac/`, `bridge/`, `gen_checker/`. The
  integration module that wires them into the §8.2 datapath already exists
  (`fpga/ethernet/eth_mac_test_subsystem.sv`, with its R1–R4 interface
  reconciliations documented in-file). Only `lan9220_if/` is a **stub by
  decision** — the AXI-EMC vendor cell (`axi_emc_0`, `shell_bd.tcl:670`) + a
  ported driver own that path. Reserved regmap addresses already exist: **VPHY
  @ 0x44A3_0000** and **GENCHK @ 0x44A6_0000** (`shell_bd.tcl:826,829`). The
  matching DUT RM (`rm_eth_ss`) is already `pr_verified` against the 256 KiB
  shell (`PLATFORM_LIVE_STATUS.md:97`).
- **Effort:** LOW–MEDIUM. It is *integration*, not greenfield RTL (§4 below).
- **Verdict:** ✅ **Recommended.** It is the spec's intended path (§8), needs
  **zero new board pins / no XDC change**, keeps the whole tester in the static
  shell behind the Shutdown Manager (so a broken DUT MAC can't wedge it, arch doc
  line 261), and gives *two-sided* MAC verification (on-chip `gen_checker`
  scoring + real reachability over the LAN9220), which is stronger than "it
  pings" (arch doc §8.4).

---

## 3. Recommendation

**Adopt Option C (virtual-PHY MAC-verify path).** It is the only board-supported
route, it is the spec's design intent, and ~85% of it is already written and
unit-benched. "Sharing the LAN9220" (Option B) is the *outcome* — realised by C's
frame bridge — and a second physical PHY (Option A) is unnecessary hardware for
functional MAC verification.

Two verification tiers fall out of the same RTL:

- **On-chip (no wire needed):** `gen_checker` injects known-good + malformed
  frames (bad FCS, runts, giants, IFG, dribble) toward the DUT and independently
  scores the DUT MAC's TX framing/CRC + RX behaviour (arch doc §8.1, §8.4). This
  works even before the LAN9220 uplink is up — it is the real "MAC verification."
- **To the network:** the `eth_bridge_3port` forwards DUT frames onto the
  LAN9220 (promiscuous) so the host/network can exchange real traffic with the
  DUT's MAC — the "egress" in the literal sense.

---

## 4. RTL / pin / XDC implications of Option C

### Pins / XDC — **no change**

The RMII + MDIO stay **fabric-internal partition pins**; nothing new is pinned.
`nanosoc_mps3.xdc` is untouched for Ethernet — the existing `ETH_*` + `SMBF_*`
group (`:33-97`) already covers the LAN9220, which is the only physical egress.
This is the central payoff: **no re-pin, and the boundary survives DFX swaps.**

### RTL / BD — wire up the deferred SECTION 5

Today `shell_bd.tcl` SECTION 5 (`:730-762`) deliberately **ties the RMII/MDIO
partition pins to safe idle** and leaves the subsystem out ("DEFERRED, out of
W-BD scope … owned by the ethernet agent"). The TODO at `:743-749` already spells
out the replacement. Concretely, the integration work is:

1. **Instantiate** `eth_mac_test_subsystem` (or the five blocks directly) in the
   shell, in the **static** region behind the Shutdown Manager (arch doc §8.3).
2. **Replace the tie-offs** (`shell_bd.tcl:752-762`): route the RP-facing RMII/
   MDIO ports into the subsystem instead of GND —
   - `rp_phy_rmii_ref_clk` ← shell 50 MHz (keep, `:261`),
   - `rp_phy_rmii_crs_dv`/`rxd` ← `rmii_phy_if` (RP-facing side),
   - `rp_phy_rmii_txd`/`tx_en` → `rmii_phy_if` → `link_partner_mac`,
   - `rp_mdc`/`mdio_o`/`mdio_oe` → `mdio_phy_model`; `mdio_phy_model` → `rp_mdio_i`.
3. **Uplink:** connect the bridge's LAN9220-uplink AXIS port to `lan9220_if`
   (paired with the existing `axi_emc_0` cell, `:670`); connect the bridge's
   management port to the MicroBlaze/lwIP stack.
4. **Regmap:** add **VPHY** and **GENCHK** as AXI-Lite slaves on
   `axi_interconnect_0` — grow `NUM_MI` (`:769`), extend `mi_map` (`:783-798`),
   and drop the two reserved `assign_bd_address` lines in at the already-reserved
   `0x44A3_0000` / `0x44A6_0000` (`:826,829`).
5. **Decoupler:** the RMII-TX (`phy_rmii_txd`, `tx_en`) and MDIO (`mdc`,
   `mdio_o`, `mdio_oe`) RP→static outputs must go through the DFX decoupler's
   `rmii_tx` / `mdio` interfaces — see `docs/DFX_DECOUPLER_BOUNDARY.md` §3a/§4.
   (This is why the decoupler config should enumerate them **now**, even while
   SECTION 5 is still tied off.)

### IOB / HDPR-29

The RMII-TX re-register stage lives in `rmii_phy_if` in the **static shell**, not
the RM (`partition-pins.md:103-109`; `rmii_phy_if/README.md:60-84`). Because the
RMII never reaches a pad in this build, there is **no** OLOGIC/pad constraint to
push into the RM — the TX signals cross the partition as plain fabric nets. If a
physical pad is ever added (Option A, v1+), mark that stage `(* IOB="TRUE" *)`.

### Firmware

- **`lan9220_if` driver:** port Zephyr `eth_smsc911x.c` (Apache-2.0) to bare-metal
  MicroBlaze (arch doc §12, line 381; W-SMSC workstream). Set the LAN9220
  **promiscuous** so it carries the DUT's MAC address alongside the shell's
  management MAC (arch doc §8.3, line 260).
- **`eth_bridge_3port` is register-less** (confirmed I11 / v0.2,
  `fpga/ethernet/README.md`) — no driver, pure fabric forwarding.
- **VPHY (`mdio_phy_model`):** MicroBlaze-writable link state so the host can
  inject link up/down/speed events; it must answer MDIO or the DUT's PHY
  bring-up/auto-neg poll hangs (arch doc §8.1, line 251 — "**Critical**").

### DUT (RM) side

The nanoSoC `eth_ss` MAC must be configured **RMII ref-in** (shell sources
REF_CLK, §1.2); do not let the DUT source the 50 MHz reference (arch doc §8.3,
line 259). This matches the already-`pr_verified` `rm_eth_ss` boundary
(`PLATFORM_LIVE_STATUS.md:97`).

### Known caveat

The LAN9220 is **10/100 and shared** — management + reconfig-push + debug + DUT
uplink all traverse the one port (arch doc line 56, §9 line 313). This is
**functional** MAC verification, explicitly **not** throughput benchmarking
(arch doc §8.3 line 260; non-goal §3, line 32). The on-chip `gen_checker` path
gives full-rate scoring independent of the LAN9220 bottleneck.

---

## 5. Summary

- The MPS3 offers **one** Ethernet egress — the memory-mapped LAN9220; there is
  **no** second PHY and **no** RMII pinout (`nanosoc_mps3.xdc:33-97`).
- Option A (second PHY): rejected — unsupported by the board, adds hardware for
  no gain. Option B (raw LAN9220 share): impossible directly, reduces to C.
- **Option C — the virtual-PHY / MAC-verify path (spec §8) is the
  recommendation.** The shell terminates the DUT's RMII in fabric, recovers
  frames as AXIS, and 3-port-bridges them onto the promiscuous LAN9220 — one
  physical port, two MACs.
- It needs **no XDC/pin change**, and **5 of 6 RTL blocks plus the integration
  module already exist and are benched** (`fpga/ethernet/`). The remaining work
  is *integration*: wire the deferred `shell_bd.tcl` SECTION 5 (`:730-762`), add
  the VPHY/GENCHK regmap slaves (reserved at `0x44A3`/`0x44A6`), route the
  RMII/MDIO outputs through the DFX decoupler, and port the `lan9220_if`
  promiscuous driver.

---

## 6. Reproducibility of the eth_ss image

The other DUT on this platform — the standalone AHB-MAC + PTP **ethernet
subsystem** (`rm_eth_ss`, and the fielded standalone "mainfix" image built from
`$ETH_SS_HOME`) — carried a long-standing report that it **could not be rebuilt**:
a clean rebuild produced a dead Cortex-M0 (0 DUT-TX frames, RMII bridge/RX still
alive) while the fielded image ran, and the difference was believed to be
build-to-build non-determinism in the bootloader → bootrom path.

**It is not non-determinism.** Root-caused and byte-confirmed board-free on
2026-09-10: `$ETH_SS_HOME/fpga/Makefile:230-232` passes `GNU_CC_EXTRA_FLAGS` on
the make **command line**, where it overrides the `:=` in every sub-makefile, so
the FPGA flow **replaces** each firmware target's own compiler flags with the
board `-D` defines instead of adding to them. The bootloader loses
`-Os -flto -mthumb-interwork`; `udp_echo` loses
`-flto -ffunction-sections -fdata-sections -Wl,-Map=…`. `make -C fpga
build_design` and `make bootloader` therefore compile **different binaries out of
identical source**, and each is perfectly reproducible on its own — which is why
rebuild-vs-rebuild bisection never converged.

Confirmation: rebuilding the `ethss-main-build` worktree's bootloader with the
clobber reproduces the archived 2026-08-25 "dead control" artefact byte-for-byte
(md5 `568f86cb…`, 1048 bytes); through the leaf makefile it is 996 bytes. The
archived object's `DW_AT_producer` shows `-g -O1` and no LTO at all.

**Consequences for anyone building an eth_ss image:**

- Build the firmware with `fpga/rp/local_overrides/eth_ss/build_eth_ss_bootrom.sh`
  (appends the board defines instead of replacing the flags), or apply
  `fpga/rp/local_overrides/eth_ss/patches/000{1,2}` upstream. Do **not** assume
  `make -C fpga build_design` and `make bootloader` agree.
- Regenerate `build_soc` through
  `fpga/rp/local_overrides/eth_ss/soc_model_reproducible.py` when you need to
  *compare* two builds: every `soc_model` backend stamps a wall clock into every
  generated file, so a plain `diff -r` between two `build_soc` trees is pure noise
  and a real difference has nowhere to show.
- `tests/eth_ss_repro` gates all of this, with a control (`ETHSS_REPRO_CONTROL=1`)
  that is seen to fail. It skips with a printed reason when the ARM toolchain or
  `ETH_SS_HOME` is absent, so CI never claims to have proved it.

Full evidence, including what this does **not** prove (whether the reproducible
build is the *working* one is a board question, and the fielded image's own
bootrom bytes are not reachable in any tree): `docs/planning/ETHSS_REPRODUCIBILITY.md`.

This does not touch the Option C egress path above — `rm_eth_ss` as instantiated
in this repo has no CPU at all (`fpga/rp/eth_ss/filelist.tcl`: `eth_ss_bringup.sv`
is the bus master), so it has no bootrom and needs no knob in
`fpga/dfx/Makefile`. The defect belongs to the standalone eth_ss FPGA flow in
`$ETH_SS_HOME`.
