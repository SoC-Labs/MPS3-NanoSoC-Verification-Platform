# Architecture — the one entry document

**What this is.** The single document to read before working on this platform.
It explains what the system is made of, why each piece is where it is, and —
for every fact — which file in this repository is the authority for it. It is
deliberately a *map*, not a spec: where a frozen interface exists, this document
points at it rather than restating it, because a fact asserted in two places is
a fact that will be wrong in one of them.

**What it is not.**

- Not [`ARCHITECTURE_SPEC.md`](ARCHITECTURE_SPEC.md). That is the **v2.1 design
  spec from July 2026**, marked `Status: HISTORICAL` and indexed in
  [`ARCHIVE_INDEX.md`](ARCHIVE_INDEX.md). It records what was *planned*; parts of
  it were built differently and parts were not built at all. Read it for
  rationale, never for current state.
- Not a status document. **What is proven on silicon** lives in
  [`STATUS.md`](STATUS.md), badge by badge, with the commit or dated log behind
  each claim.
- Not the authority on what the board is running. **That is
  [`FIELDED_SHELL.md`](FIELDED_SHELL.md)**, and nothing else in this tree may
  restate it — a gate enforces that (see [Gates](#7-the-gates)).

**Reading order for a newcomer.** The plain-language on-ramp is the doc site's
[Concepts](site/docs/index.md) pages (five short pages, no FPGA knowledge
assumed). Read those, then this. Then the frozen contracts in
[`contracts/`](contracts/).

---

## 1. The problem, and the shape of the answer

The Arm MPS3 board's native way to change an FPGA design is its **MCC**
(Motherboard Configuration Controller): it reads a whole bitstream off a microSD
card and reboots the FPGA. That costs minutes per change, and it takes the
network down with it.

So the platform splits the FPGA in two and only ever reloads half of it:

```
   host: workstation / tender (Pi 5 or lab server)      MPS3 board (XCKU115)
  ┌───────────────────────────────┐                ┌──────────────────────────────────┐
  │ pyverify  (the front door)     │   Ethernet    │ STATIC SHELL — always resident    │
  │ socket_harness (library)       ├───────────────┤  MicroBlaze + lwIP, LAN9220 MAC   │
  │ OpenOCD / Vivado hw manager    │               │  config agent -> HWICAP -> ICAP   │
  └───────────────────────────────┘                │  DFX decoupler, debug servers,    │
       TCP 6900  control (JSON lines)              │  CLCD status panel                │
       UDP 69 / TCP 6910  bitstream push           │  ┌────────────────────────────┐   │
       TCP 6921  JTAG (OpenOCD remote_bitbang)     │  │ RECONFIGURABLE PARTITION   │   │
       TCP 2542  XVC (ILA)                         │  │ one DUT design at a time,  │   │
       TCP 6930/6931/6932  UART0/UART1/SWO         │  │ swapped live over Ethernet │   │
                                                    │  └────────────────────────────┘   │
                                                    └──────────────────────────────────┘
```

The design principle is **"disaggregated PYNQ"**: PYNQ pairs a Linux+Python
brain with FPGA overlays on one board, using a hard Cortex-A processor system.
The XCKU115 has no hard processor system, so instead of rebuilding one in soft
logic the Python brain is moved to an external machine and reaches the board
over Ethernet (`ARCHITECTURE_SPEC.md` §2 — historical, but this decision stands
and is visible throughout the tree).

Two consequences follow, and they explain most of the rest of this document:

1. **Everything needed to *reach* the board lives in static logic.** Network,
   configuration engine, debug bridges. A DUT that hangs, or a partial that is
   wrong, can therefore always be swapped out from the outside.
2. **The one physical link is 10/100 Ethernet.** It carries control, bitstreams,
   debug and the DUT's console. It is the bottleneck, and the design does not
   pretend otherwise.

Authority: [`README.md`](../README.md) (repository map), the concept pages under
[`site/docs/concepts/`](site/docs/concepts/).

---

## 2. The two halves of the fabric

### 2.1 The static shell

Built as a Vivado block design, [`fpga/shell/bd/shell_bd.tcl`](../fpga/shell/bd/shell_bd.tcl),
around a soft **MicroBlaze** running bare-metal C (no operating system). Its
blocks, and the AXI4-Lite page each answers on, are frozen in
[`contracts/shell-regmap.md`](contracts/shell-regmap.md):

| Page | Block | What it does |
|---|---|---|
| `0x44A0_0000` | CLKRST | DUT clock + the three DUT resets |
| `0x44A1_0000` | DFXCTL | decoupler control, `RM_ID` readback, `RM_STATUS` |
| `0x44A2_0000` | HWICAP | the AXI front end to ICAP (Xilinx PG134) |
| `0x44A3_0000` | VPHY | virtual PHY / MDIO register model toward the DUT MAC |
| `0x44A4_0000` | OVLSTORE | AXI Quad SPI → the QSPI flash holding a default overlay |
| `0x44A5_0000` | TELEM | telemetry (see the honesty note below) |
| `0x44A6_0000` | GENCHK | error-injecting Ethernet generator/checker |
| `0x44A7_0000` | JTAGBB | the JTAG bit-bang engine behind TCP 6921 |
| `0x44A8_0000` | DBGBR | Xilinx Debug Bridge (AXI → BSCAN) behind XVC |
| `0x44A9_0000` | UARTBR | DUT console/SWO ↔ TCP relay buffers |
| `0x44AA_0000` | GPIO | board-port passthrough + host mux |
| `0x44AB_0000` | MMCM_DRP | runtime DUT-clock retune |
| `0x44AC_0000` | CLCD | the on-board status panel |
| `0x44AD_0000` | CLCDKVM | panel arbitration between harness and DUT |
| `0x44AE_0000` | TOUCH | AXI IIC to the resistive touch controller (built `SHELL_TOUCH=1`) |

(Table transcribed from `contracts/shell-regmap.md:136`–`155`, which is the
authority and carries each block's register fields. That contract also lists the
pages RESERVED by blocks that are designed but in no block design yet — `0x44AF`
AXIJTAG, `0x44B0` MAGICID, `0x44B1` UART16550 — which have no base constant
anywhere on purpose. `tools/gen_regmap.py` owns the whole allocation and refuses
to render two owners on one page.)

**Telemetry is deliberately honest about what it cannot measure.** There is no
power sensor reachable by any path on this board, so the `telemetry` verb always
returns `{"ok":false,"err":"no power sensor","lockup":<bool>}` rather than a
plausible zero — `contracts/net-protocol.md` §"Telemetry", v0.6. Pass/fail comes
from the DUT's console and debug port, never from telemetry.

### 2.2 The reconfigurable partition (RP)

One region of fabric, marked `HD.RECONFIGURABLE`, holding exactly one design at
a time. It is a hierarchical cell **alongside** the shell in one top design
(`u_rp_dut`), not a separate top-level block — decision I4, recorded in
[`contracts/partition-pins.md`](contracts/partition-pins.md).

The RP is never empty: an FPGA region with nothing in it is not a legal design,
so it holds `rm_greybox`, an inert tie-off, until a real design is loaded.

### 2.3 The boundary between them

**47 ports, 148 bits and 20 decoupler interfaces, frozen on the fielded shell
`0x72BB0A36`** (the 2026-10 ILA mint, fielded 2026-09-24). That mint widened the
previous shell's 35 ports / 136 bits (`0x3F1A560F`) by the 12-wire `dbgbscan`
group below (decision D4 revised 2026-09-23; proven on silicon 2026-09-24,
`docs/evidence/2026-09-w3/ila_proofs_20260924.txt`).
[`contracts/partition-pins.md`](contracts/partition-pins.md)
is the authority; directions there are stated from the *shell's* view and invert
on the RM side. The groups:

| Group | Signals |
|---|---|
| Clocks & resets | `dut_clk`, `dut_resetn`, `rp_resetn`, `dbg_resetn` |
| Processor debug (SWJ-DP as JTAG) | `jtag_tck`, `jtag_tms`, `jtag_tdi`, `jtag_tdo` |
| Ethernet (RMII + MDIO) | `phy_rmii_ref_clk`, `phy_rmii_crs_dv`, `phy_rmii_rxd[1:0]`, `phy_rmii_txd[1:0]`, `phy_rmii_tx_en`, `mdc`, `mdio_o`, `mdio_oe`, `mdio_i` |
| Console / trace | `uart_tx_*`, `uart_rx_*` (AXI-Stream byte pair), `swo` |
| Status / misc | `rm_id[31:0]`, `dut_lockup`, `irq_out` |
| Board GPIO passthrough (I4) | `dut_gpio_o/oe/i[NGPIO-1:0]`, `NGPIO` = 16 |
| Flash / QSPI XiP (v0.2) | `qspi_sclk`, `qspi_csn`, `qspi_io_o[3:0]`, `qspi_io_oe[3:0]`, `qspi_io_i[3:0]` |
| RM debug BSCAN, `dbgbscan` (**since `0x72BB0A36`**) | 11 shell-driven legs from `debug_bridge_0`'s BSCAN master + `dbg_bscan_tdo` back — lets a mode-1 `debug_bridge` inside the RM reach that RM's ILAs over XVC |

Three rules make the split work, and all three are enforceable statements about
where logic may live, not style preferences:

- **No shell↔DUT AXI.** Everything crossing is a slow scalar or a low-rate
  stream. A wedged DUT cannot hang a bus the shell needs.
- **All DUT clocks are generated in the shell** and enter the RP as pins; no
  clock generation inside an RM (`partition-pins.md` "Clock/reset domain rule").
- **No pin-facing `IOB`/`OLOGIC` registers inside an RM** — those pad sites are
  static-only in a DFX design (`partition-pins.md` "IOB packing note", the
  HDPR-29 lesson).

The one documented exception is the QSPI group: it is a **matched, non-CDC,
source-synchronous passthrough**, because QSPI read capture is timed relative to
`SCLK` and a synchroniser would destroy it. The decoupler still clamps it, to
`qspi_csn = 1` (flash deselected) — the single intentional non-zero safe-idle
value in the whole boundary.

**Why the boundary is the most gated thing in the repo:** every RM links into
the *same* black-boxed cell in the *same* locked static checkpoint. A wrapper
that differs by one port, one bit, or one direction does not degrade — it fails
`link_design`/`pr_verify` after a full synthesis, place and route.
[`fpga/dfx/pin_check.py`](../fpga/dfx/pin_check.py) reproduces that judgement
mechanically in about a second, and runs in `make check` / `make check-ci`
stage 2.

---

## 3. DFX, `static_id`, and why a "mint" is expensive

### 3.1 The build

The DFX flow is [`fpga/dfx/build_dfx.tcl`](../fpga/dfx/build_dfx.tcl), driven by
[`fpga/dfx/Makefile`](../fpga/dfx/Makefile). It runs an N-configuration loop: the
reference RM (`rm_greybox`, which **must** stay first in `RM_ORDER`) is
implemented against the static, its routed checkpoint has the RP black-boxed and
its routing locked, and that locked checkpoint becomes the fixed base every other
RM is implemented against. Vivado's `pr_verify` then proves each configuration
shares a byte-identical static region.

The RM library — every RM's wrapper directory, top module, `design_id`, version
and how its checkpoint is produced — is one file:
[`fpga/dfx/rm_list.tcl`](../fpga/dfx/rm_list.tcl). It is the registry `build_dfx.tcl`,
`pin_check.py`, `check_rm_id_encoding.py` and `gen_manifest.py` all read.

### 3.2 `static_id` — the compatibility fingerprint

`static_id` is the **zlib/IEEE CRC-32 of `static_routed_locked.dcp`**, formatted
`0x%08X` — `fpga/dfx/build_dfx.tcl:676`–`697`. Three properties matter:

- **Deterministic** for a given locked-static file, and re-derivable anywhere
  with plain `zlib.crc32` — no Vivado needed. That is why the host pusher, the
  manifests and the shell firmware can all compare it.
- **Changes on any rebuild**, including a rebuild of an unchanged shell, because
  a `.dcp` is a zip with embedded timestamps. Conservative in exactly the
  direction the overlay contract demands.
- **32 bits**, so it fits the push header's `u32` field as-is.

A **mint** is a full rebuild of the static shell, and therefore a new
`static_id`. Everything keyed to the old one stops fitting the moment it
completes, which is why most work on this platform is arranged to avoid one: an
RM that fits the existing boundary (47 ports since `0x72BB0A36`) is folded into an already-locked
static incrementally (`DFX_REUSE_LOCKED` / `DFX_STATIC_ID_FILE`, read and *not*
recomputed — `build_dfx.tcl:472`–`510`), costing a partial build and no re-key.

### 3.3 What ships for one RM: the overlay

[`fpga/dfx/gen_manifest.py`](../fpga/dfx/gen_manifest.py) turns the built
bitstreams into `overlay/<rm_name>/`:

- a **partial** bitstream — the RM's fabric;
- a **clearing** bitstream — mandatory on UltraScale: the region must be returned
  to a blank state before a new RM is written, so a swap streams two files;
- a **manifest** (`manifest.json`) — `rm_id`, lengths, CRC-32s, and the
  `static_id` of the shell it was built against. Schema:
  [`contracts/overlay-manifest.md`](contracts/overlay-manifest.md).

`gen_manifest.py` refuses to emit a manifest for a lone half of the pair. The
payload `.bin`s are deliberately not committed (large, regenerated); the
manifests are, as evidence.

### 3.4 `rm_id` — the identity you can read back

Each RM drives a 32-bit constant on the `rm_id` partition pin; the shell reads it
at `DFXCTL.RM_ID` (`0x44A1_0010`) after a load. Encoding v2
(`VERSIONING_PLAN.md` §3.2) puts the version in the high half:

```
 31           24 23           16 15                            0
+---------------+---------------+-------------------------------+
|   ver major   |   ver minor   |        design_id[15:0]        |
+---------------+---------------+-------------------------------+
```

It is **derived, never hand-written**: `rm_list.tcl`'s `rm_id_of` computes it
from `(design_id, version)`, and `design_id` uniqueness is enforced at source
time. Three places must agree — the wrapper `localparam`, `rm_list.tcl`, and the
overlay manifest — and `scripts/harness_gates/check_rm_id_encoding.py` fails the
build if they ever drift.

`rm_greybox` is held at version 0.0 so its `rm_id` stays exactly `0x00000000`,
which preserves both the decoupler's `DECOUPLED_VALUE` and firmware's
"0 == nothing loaded" convention.

**Why read-back rather than trust a filename:** it is what lets the board's
status panel name the resident design correctly even when something loaded it
behind the firmware's back over JTAG, and it is what makes a swap *verifiable*
rather than merely completed.

### 3.5 `minted` vs `fielded` — two facts, routinely different

- **`minted`** is the shell the current overlay set is keyed to. It is generated:
  `make -C fpga/dfx overlays` computes it and writes it into every manifest and
  into `overlay/mps3_shell_static_id.c` in the same run.
- **`fielded`** is what the board actually boots from its config SD. **Nothing
  computes it.** It changes only when someone writes a bitstream to the SD and
  watches the board come up on it.

Between a mint and a deployment the repository legitimately holds overlays keyed
to a shell no board is running. [`FIELDED_SHELL.md`](FIELDED_SHELL.md) is the one
place either fact is written down, and it also records what the fielded
**firmware** was built with (`fielded_fw_flags`) and the MicroBlaze local-RAM
size (`lmb_kb`) — because the fielded image is two artifacts and only the
bitstream has a CRC. A "half-mint" (fabric built with the touch IIC block,
firmware built without the touch driver) passed every gate in this repo before
that row existed.

Adding an RM to an existing shell is the routine case, and is documented
step-by-step in
[the "Adding an RM" guide](site/docs/guides/adding-an-rm.md), starting from the
copyable skeleton in [`fpga/rp/_template/`](../fpga/rp/_template/).

---

## 4. The swap: the 6900 protocol and the firmware superloop

### 4.1 The wire

[`contracts/net-protocol.md`](contracts/net-protocol.md) (v0.8) is the frozen
host↔shell protocol. One LAN9220 10/100 port carries all of it:

| Port | Proto | Service |
|---|---|---|
| 6900 | TCP | control/status — one JSON object per line, request → response |
| 69 | UDP | TFTP push of clearing/partial bitstreams |
| 6910 | TCP | raw push (alternative to TFTP), optional windowed flow control |
| 2542 | TCP | XVC → Debug Bridge (ILA) |
| 6921 | TCP | JTAG, OpenOCD `remote_bitbang` — **the live debug service** |
| 6920 | TCP | SWD, `remote_bitbang` — retired by the JTAG cutover; not in the default image |
| 6930 / 6931 / 6932 | TCP | DUT UART0 / UART1 / SWO trace |

Verbs: `ping`, `reset`, `set_clk`, `swap`, `link`, `commit`, `telemetry`,
`macgen`, `diag`, `display`, `version`.

Two shapes worth knowing before reading firmware or host code:

- **`ping` and `version` answer different questions.** `ping` reports the
  *fabric*'s `static_id` and the resident `rm_id`; `version` reports what the
  *firmware image* is — release, build sha, the LMB size it was linked for, and
  its compile-time feature set. One `static_id` serves many firmware releases,
  because a firmware-only change is re-baked into the bitstream with `updatemem`
  and does not touch the static routing. A board can report the expected
  `static_id`, pass every acceptance gate, and still be running an image built
  with the wrong flags — which is exactly what `version` exists to make visible
  (`net-protocol.md` v0.8 changelog).
- **The push happens *inside* the swap: `swap` → push → reply** (v0.7, and it
  reversed v0.2). The shell only listens for a bitstream once the `swap` RPC has
  driven its state machine into `SWAP_AWAIT_*`, which is why the 6900 connection
  is parked; a push arriving at any other time is reset.

### 4.2 The sequence

Choreographed by the coordinator firmware:

1. **Quiesce** — gate debug, console and DUT-Ethernet traffic.
2. **Decouple** — the DFX decoupler isolates the RP; the RP is held in reset.
3. **Clear** — stream the outgoing RM's clearing bitstream into ICAP.
4. **Write** — stream the new RM's partial into ICAP.
5. **Verify** — read `DFXCTL.RM_ID` back and compare with the value carried in
   the pushed partial's own 24-byte header (generated from the manifest).
6. **Release** — drop the decoupler, take the RP out of reset.

### 4.3 The superloop — and why the swap is a stepper

There is exactly **one** loop, in
[`firmware/platform/src/main.c:257`](../firmware/platform/src/main.c):

```c
for (;;) {
    mps3_net_lwip_rx_poll(8);   /* bounded RX drain -> lwIP            */
    mps3_net_lwip_tmr();        /* TCP/ARP timers (manual, NO_SYS)     */
    smsc911x_tx_status_drain(); /* an un-drained TX status FIFO halts the MAC */
    swap_fsm_poll();            /* coordinator/swap_fsm.c — non-blocking */
    config_agent_poll();        /* TFTP 69 / TCP 6910                  */
    coordinator_net_poll();     /* control channel TCP 6900            */
    jtag_server_poll();         /* TCP 6921                            */
    xvc_server_poll();          /* TCP 2542                            */
    uart_over_eth_poll();       /* TCP 6930/6931/6932                  */
    clcd_poll();                /* #ifdef MPS3_HAS_CLCD                */
    mps3_diag_publish(&v);      /* refresh the JTAG-readable mailbox   */
}
```

lwIP runs **RAW API, no RTOS, no threads**. That is the whole reason
`swap_fsm` is a non-blocking stepper rather than a function that writes a
bitstream: a multi-second ICAP write inside the loop would starve the very TCP
connection driving the swap. Treat every `_poll()` as "must return promptly, no
blocking I/O" — `firmware/README.md` "Network stack".

Control requests are dispatched synchronously and answered immediately, **except
`swap`**, which arms the state machine and whose response is *held* until the FSM
settles (`firmware/README.md:93`).

### 4.4 The diagnostic mailbox

`mps3_diag_publish()` refreshes a struct at a fixed address every pass, so it is
current at the instant of any wedge. It is the **only** telemetry readable
*during* a swap, when the 6900 channel is parked — read over JTAG with `xsdb`.

Its address is anchored to the **top** of the MicroBlaze local RAM:
`lmb_kb * 1024 - 0x80`. This is load-bearing and quietly dangerous: the LMB
address decode **aliases**, so a wrong belief about the LMB size does not produce
an error, it produces a plausible wrong answer from a different address. That is
why `lmb_kb` is a gated row in [`FIELDED_SHELL.md`](FIELDED_SHELL.md).

### 4.5 Persistence and self-boot

`overlay_store/` manages a default overlay in the board's QSPI flash with A/B
slots (so an interrupted update cannot destroy the working default) and
`coordinator_init()` loads it at power-up, so the board can come up on a real DUT
with no host attached. See [`STATUS.md`](STATUS.md) for exactly how much of this
is silicon-proven today versus built.

---

## 5. The host side

**`pyverify` is the front door.** `host/socket_harness/` stays a *library* —
endpoint registry, `xsdb` CSR access, console bridge — that `pyverify` imports;
it is not a second CLI.

[`host/pyverify/`](../host/pyverify/) is stdlib-only at runtime:

| Module | Role |
|---|---|
| `board.py` | `Mps3Board` — the session facade ("the PYNQ experience") |
| `client.py` | the 6900 control channel; the conformance-pinned client |
| `overlay.py` | manifest load + validate (`overlay-manifest.md`) |
| `pusher.py` | 24-byte header framing + TFTP PUT / raw-TCP send |
| `swap.py` | validate → push → swap → reattach orchestration |
| `console.py` | UART/SWO TCP consoles (6930–6932) |
| `debug.py` | OpenOCD / XVC launch wrappers |
| `cli.py` | `python -m pyverify.cli <verb>` |

The whole inner loop, in real code from [`README.md`](../README.md):

```python
from pyverify import Mps3Board

with Mps3Board("192.168.10.101") as board:
    result = board.deploy("nanosoc")               # push overlay + swap the partition
    result.reattach.apply()                        # reopen SWD / console (+ ILA if xvc= given)
    board.uart0.assert_contains(b"nanosoc boot")   # the verdict comes from the console
```

Two things that look like details and are not:

- **The `static_id` guard runs before anything is written.** An overlay whose
  manifest does not match the shell's live fingerprint is refused, so a design
  built for a different shell physically cannot be loaded.
- **Debug is gated during a swap and must be re-attached afterwards.** The
  boundary is decoupled mid-swap, and the DUT's debug port is rebuilt, so the
  host re-runs the line reset and DAP connect. `deploy()` returns a *reattach
  plan* that scripts this rather than leaving it to the caller to remember.
  For ILAs (since the 2026-10 ILA mint, which puts them inside the RM) **a swap
  invalidates the XVC session**: the target must be closed before the swap and
  reopened, with the new RM's `.ltx`, after it. The plan does both only when the
  session was handed in — `Mps3Board(host, xvc=XvcSession(...))`, which passes
  it to `SwapOrchestrator(xvc=...)`; without it the ILA step returns a hint and
  the caller reopens `pyverify.debug.XvcSession` by hand.

`FakeShell` (`pyverify/testing/fakeshell.py`) is a byte-conformant test double
for the shell, which is how the host half is tested with no board — and the
reason its conformance test matters is that a fake more permissive than the
firmware once kept an entire end-to-end suite green against the wrong swap
ordering (`net-protocol.md` v0.7 changelog).

---

## 6. The board, and how you reach it

- **Configuration.** The MCC reads the full FPGA image from its own microSD (the
  volume label is `V2M-MPS3`) at power-on. That card is the only non-volatile
  home of the fielded shell.
- **A JTAG-loaded bitstream is volatile.** It is the fast path for recovery and
  bring-up, and a power-cycle reverts the board to whatever the config SD holds.
- **The board is shared and leased.** Take the lease before touching it and
  release it with the same holder id, or it stays held. The board's data IP is
  reachable only from the lab's board-management host, so console, push and
  debug commands are run through it.
- **First bring-up needs a cable.** The networked debug stack lives in the shell,
  so before a shell exists there is nothing to tunnel through. After that,
  everything is over Ethernet.

Runbook: [`BOARD_BRINGUP.md`](BOARD_BRINGUP.md) (dark board → live networked
DUT). Symptom index: [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md).

**The on-board status panel** (QVGA LCD, driven by the shell) answers "what is
loaded, is it healthy, can I reach it?" with no laptop attached. Its design rule
is that **the glass never lies**: the resident-design row is read live from
`DFXCTL.RM_ID`, never from a firmware cache, and an unreadable partition shows
`RP DECOUPLED` / `RM ID NOT VALID` rather than a stale name. The driver is
cooperative and bounded per superloop pass — the panel is cosmetic and may never
be able to disturb the network, which is the board's only ingress
([`README.md`](../README.md) "The on-board status display").

---

## 7. The gates

Three tiers, and the split between them is exactly "what does this need?".

| Command | What it is | Needs |
|---|---|---|
| `make check-ci` | stages 1–5, the hosted CI logic gate | Python + pytest, `tclsh`, a C compiler |
| `make lint` | the hosted Verilator lint job (stage 6), a separate job on purpose | verilator |
| `make check` | the local superset: 1–6, plus stages 7–8 which skip loudly | + a simulator, + built overlays |
| `make check SIM=vcs` | the full gate | VCS + `source set_env.sh` + the SoC source repos |
| `scripts/harness_regression.sh` | tiered go/no-go for a **newly built harness image**; tier 3 needs a lease and `--allow-board` | tiers 0–2 board-free |

Stage by stage (`Makefile`, and [`CI.md`](CI.md) for the authoritative account):

1. the frozen contracts exist;
2. **mechanical agreement gates** — `pin-check` (the RM boundary),
   `check_rm_id_encoding.py` and `check_rm_id_literals.py`, the manifest-half
   overlay gates, `check_fielded_shell_claims.py`, and six gates folded in from
   `harness_regression.sh`;
3. cross-workstream pytest (`tests/`);
4. host pytest (`pyverify`, `socket_harness`, `webharness`, `readback`);
5. firmware, stage-0 and daemon wire-contract harnesses compiled and run **on the
   host compiler** — the coordinator/swap/codec logic is proven with no
   MicroBlaze and no board;
6. Verilator lint; 7. cocotb benches; 8. overlay CRC round-trip.

Two conventions explain the design of all of it:

- **Loud skips, never silent ones.** A stage that cannot run is named at the end.
  A green gate that hid an empty one is a failure this project has been bitten by
  — repeatedly, and it is why `tests/integration/test_ci_gate_mirrors_check.py`
  now asserts that every gate script is actually invoked by something, or sits in
  an allowlist with a written reason.
- **A gate that cannot fail is not a gate.** Each mechanical gate names the
  historical escape it would have caught; several were added *after* the escape.

CI runs the board-free gate on **every** branch (`branches: ["**"]`). Nothing
on-board runs in CI: board work is leased, hub-local and human-supervised.

---

## 8. How the repository is laid out

```
fpga/shell/       the static shell: block design + custom AXI IP
fpga/rp/          RM (DUT) partition-pin wrappers -- and _template/, the skeleton
fpga/dfx/         the DFX flow: floorplan, N-config build, RM library, overlays
fpga/ethernet/    MAC-in-operation blocks (virtual PHY, gen/checker, bridge)
fpga/monolithic/  non-DFX whole-FPGA baseline
firmware/         MicroBlaze bare-metal C: coordinator, config agent, servers
host/             pyverify (front door), socket_harness (library), tender plugin
scripts/          CI gates (scripts/harness_gates/), versioning, board tooling
tests/            cocotb benches per block + pure-Python integration tests
docs/             this file, STATUS, runbooks, and docs/contracts/ (frozen)
```

**Sources this repository does not own** — nanoSoC, the Ethernet subsystem, the
SoCScope trace plane, and Arm Academic Access IP — are consumed **read-only from
sibling checkouts through environment variables**, never vendored and never
modified. A second copy drifts, and the confidential IP must not enter this tree
at all ([`CONTRIBUTING.md`](../CONTRIBUTING.md) "The lab-IP boundary"). The
consequence for a clean clone is real and documented: five RMs build from this
repository alone; the other five each need something it deliberately does not
carry (see the doc site's
[Getting started](site/docs/guides/getting-started.md)).

### The frozen contracts

Change these only via the integrator; a boundary change ripples across every
workstream.

| Contract | Fixes |
|---|---|
| [`contracts/partition-pins.md`](contracts/partition-pins.md) | the 35-port RP ⇄ shell boundary |
| [`contracts/partition-timing.md`](contracts/partition-timing.md) | the socketed-XDC clock timing contract for RMs |
| [`contracts/shell-regmap.md`](contracts/shell-regmap.md) | the MicroBlaze AXI4-Lite register map |
| [`contracts/net-protocol.md`](contracts/net-protocol.md) | the host ⇄ shell wire protocol and ports |
| [`contracts/overlay-manifest.md`](contracts/overlay-manifest.md) | the overlay artefact schema + the A/B slot store |
| [`contracts/dut-display-tunnel.md`](contracts/dut-display-tunnel.md) | the RP → shell display path over spare GPIO bits |

Open questions the contracts could not answer, with dispositions and owners:
[`contracts/OPEN_ISSUES.md`](contracts/OPEN_ISSUES.md).

---

## 9. Where to go next

| You want | Read |
|---|---|
| The plain-language introduction | [the doc site's Concepts pages](site/docs/index.md) |
| What is proven on silicon | [`STATUS.md`](STATUS.md) |
| What the board is running **right now** | [`FIELDED_SHELL.md`](FIELDED_SHELL.md) |
| To add a new DUT | [the "Adding an RM" guide](site/docs/guides/adding-an-rm.md) + [`fpga/rp/_template/`](../fpga/rp/_template/) |
| To deploy to the board | [`BOARD_BRINGUP.md`](BOARD_BRINGUP.md), then [Deploying an RM](site/docs/guides/deploying-an-rm.md) |
| The gates in detail | [`CI.md`](CI.md) |
| Which documents are history | [`ARCHIVE_INDEX.md`](ARCHIVE_INDEX.md) |

**When two sources disagree**, the project's convention is: **git commits >
build reports > status docs**. A green working tree is not the same as "HEAD
builds", and a document is only ever as current as the last time somebody
revisited it — which is what `ARCHIVE_INDEX.md` exists to tell you.
