# DRIVER_MATRIX — Linux harness shell, per-peripheral driver plan

Date: 2026-07-15. Companion to `shell_linux.dts` (same directory); both sit on top of
`TRANSPLANT_CONTRACT.md` (BD ground truth) and `SERVICE_DISPOSITION.md` (firmware
ground truth). Kernel = **6.18.7 via the proven Buildroot recipe** in
`../linux_soc/linux/` (rv32_defconfig + `configs/kernel_fragment.config`, AMD prebuilt
rv32 glibc toolchain through the `toolchain_wrapper`). Config symbols below are the
**delta** to add to that fragment unless marked "already in fragment".

**Baseline declared** (matches the DTS header): fork of the **working-tree**
`shell_bd.tcl` — CLCD-KVM present, QSPI v0.2 boundary, OVLSTORE `SPI_0` removed.
Rows note where the board-proven `0xE4B1C44A` fork differs.

**Status honesty:** nothing in this matrix has run. The QEMU proof
(`../linux_soc/linux/qemu_boot.log`) covers the CPU/kernel/userland core only —
`-M virt` exercises **none** of these peripherals. Every test plan below states what
can be proven off-board vs what needs the bench.

---

## 0. One resolved audit discrepancy (load-bearing)

`SERVICE_DISPOSITION` red-flag 1 says "LAN9220 ETH_INT is not wired in the proven
shell BD"; `TRANSPLANT_CONTRACT` §4 says In3 = `eth_irq`. **The BD is right and the
wire exists**: `shell_bd.tcl:1071` connects the `eth_irq` port to `xlconcat_intr/In3`,
and `shell_top.sv:217` connects the `ETH_INT` pad to it. What the red flag actually
describes is the **polarity obligation** documented at `shell_top.sv:217-221`: the
LAN9220 IRQ pin powers up **active-low open-drain** and must be programmed
`IRQ_CFG.IRQ_POL=1` + push-pull before the line is enabled. The bare-metal firmware
never did this (pure poll model, INTC never enabled). Under Linux the stock `smsc911x`
driver performs that IRQ_CFG write itself, keyed off the DT flags
`smsc,irq-active-high` + `smsc,irq-push-pull` — both are in `shell_linux.dts`.
**No successor-BD boundary change is needed for the ethernet IRQ.**

---

## 1. Summary matrix

| Peripheral (base) | Driver | Upstream status | Key configs | Risk |
|---|---|---|---|---|
| MBV CPU + timebase | arch/riscv + `timer-riscv` (Sstc) | in-tree, QEMU-proven path | rv32_defconfig (present) | LOW (timebase-freq unmeasured) |
| DDR4 1 GiB @0x8000_0000 | none (memory node) | — | — | LOW (BD-side risks owned by hw track) |
| UARTLITE @0x4060_0000 | `uartlite` | in-tree, mature | already in fragment | LOW |
| AXI INTC @0x4120_0000 | `irq-xilinx-intc` (chained) | in-tree, mature | already in fragment | LOW-MED (kind-of-intr confirm) |
| AXI TIMER @0x41C0_0000 | **none, deliberately** | no in-tree clocksource exists | — | none (unused) |
| LAN9220 @0xC000_0000 | `smsc911x` | in-tree, mature | `SMSC911X` +net stack | MED→LOW (reg-io-width fixed <2>→<4> 2026-07-18; promisc decision) |
| ~~OVLSTORE QSPI @0x44A4_0000~~ | **SUPERSEDED 2026-09-24 (§2.6)**: no flash driver, ever. `0x44A4` is now D13's `usd_spi` (`spi-usd` + `mmc_spi`) | — | `MTD`, `MTD_SPI_NOR`, `SPI_XILINX*` **must be unset** (release gate) | none: the harness cannot reach the SST26 |
| clk_wiz_dut DRP @0x44AB_0000 | `clk-xlnx-clock-wizard` | in-tree | `COMMON_CLK_XLNX_CLKWZRD` | MED (set_rate vs PG065 presets, D12) |
| HWICAP @0x44A2_0000 | **custom fpga-manager** (interim: UIO) | **no in-tree driver — flagged** | `FPGA*` + custom | **HIGH — the one genuine kernel work item** |
| DFXCTL @0x44A1_0000 | **custom fpga-bridge** (interim: UIO) | none (soclabs IP) | `FPGA_BRIDGE` + custom | HIGH (swap sequencing invariants) |
| CLKRST @0x44A0_0000 | UIO (split ownership w/ DFX driver) | none (soclabs IP) | `UIO_PDRV_GENIRQ` | MED (rp_resetn ownership discipline) |
| JTAGBB @0x44A7_0000 | UIO node present, **no daemon yet** ([DEV-10]) | n/a | `UIO_PDRV_GENIRQ` | LOW |
| DBGBR/XVC @0x44A8_0000 | UIO + daemon :2542 | n/a | `UIO_PDRV_GENIRQ` | LOW-MED (access-order conformance) |
| UARTBR @0x44A9_0000 | UIO + daemon :6930-2 | n/a | `UIO_PDRV_GENIRQ` | MED (destructive reads — one reader) |
| board_gpio @0x44AA_0000 | UIO + daemon | n/a | `UIO_PDRV_GENIRQ` | LOW |
| TELEM @0x44A5_0000 | none (frozen failure reply) | n/a | — | none |
| CLCD @0x44AC_0000 | UIO + daemon (NOT fbdev/DRM) | n/a | `UIO_PDRV_GENIRQ` | LOW-MED (FIFO-drop discipline) |
| CLCDKVM @0x44AD_0000 | UIO + daemon (presence-gated) | n/a | `UIO_PDRV_GENIRQ` | MED (unbuilt Wave-4 block) |
| VPHY @0x44A3 / GENCHK @0x44A6 | disabled (no slave exists) | n/a | — | none until ethernet wave |
| diag successor | `ramoops`/pstore @0xBFF0_0000 | in-tree | `PSTORE_RAM` | MED (replaces the mid-swap JTAG path) |

---

## 2. Per-peripheral detail

### 2.1 CPU / timebase — in-tree, proven path
Same core config as `linux_soc` (mbv_soc.tcl), so everything in
`../linux_soc/linux/README.md` carries over verbatim: `sstc` in
`riscv,isa-extensions` is load-bearing (no CLINT/PLIC exists; drop it and there is no
clockevent and no boot); no `zbc`, no `zicbom`; OpenSBI 1.6 `platform=generic`
`fw_jump.bin` (never the .elf) at 0x8000_0000.
**Hw-track obligation DISCHARGED (2026-07-16):** `shell_linux_bd.tcl` now pins +
read-back-asserts `C_USE_SSTC==1`, `C_USE_COUNTERS==1` **and**
`C_INTERRUPT_WAKEUP==1` (pre-validate via `mbv_cfg` AND re-asserted
post-validate; exported into `build/address_map.txt`). `C_INTERRUPT_WAKEUP` is
the silicon-proven WFI-coma fix (2026-07-16, repro `src/linux_soc/fw_mspin`):
the IP default 0 means `wfi` never wakes — not for an interrupt, not for a
debug halt — and Linux idles in `wfi`, so default 0 = hang at first idle. The
`address_map.txt` `riscv_isa` line is now COMPUTED from the read-backs, not a
hardcoded string.
**Test:** already QEMU-proven for the core; on the bench, `date` vs stopwatch for
timebase (README §5.5 — "rdtime ticks per CPU clock" is inferred, not measured).
**Risk:** LOW.

### 2.2 UARTLITE console — `uartlite` (in-tree)
`CONFIG_SERIAL_UARTLITE(_CONSOLE)` already in the fragment. DTS carries the
`clock-names = "s_axi_aclk"` named-lookup fix and `interrupts = <2 4>` (INTC In2 —
note: **In2 here, In1 on linux_soc**; the shell concat order differs).
**Test:** earlycon banner → console handover → getty; `/proc/interrupts` uartlite
count increments on RX (the login-prompt wedge is the S-mode-IRQ tell, README §5.4).
**Risk:** LOW.

### 2.3 AXI INTC — `irq-xilinx-intc` (in-tree, chained mode)
`CONFIG_XILINX_INTC` already in the fragment. 4 inputs (HWICAP/timer/uartlite/eth,
`shell_bd.tcl:1067-1071`).
**kind-of-intr — RESOLVED (2026-07-16), no longer confirm-at-integration.** The
transplant BD *pins* `C_KIND_OF_INTR = 0x00000000` (all level, [DEV-5]) and
read-back-asserts it BOTH before and after `validate_bd_design`; the built BD
reads back `0x00000000` and `build/address_map.txt` exports the value. Vivado
propagation warns that the manual value differs from its computed sensitivity
word (`0x00000004` here — the uartlite `interrupt` pin is metadata'd
EDGE_RISING; seen in this design's validate log AND on linux_soc,
`build_dbg_bd.log:1379`). **The warning is expected and benign — level-forced
is hardware-safe**, settled by the linux_soc IRQ audit
(`../linux_soc/hw/build_dbg/IRQ_WIRING_AUDIT.md` §2, source-verified against
`axi_intc_v4_1_rfs.vhd` ~line 1964): in LEVEL mode axi_intc's `LVL_P` detector
LATCHES — `hw_intr(i)` sets while the synchronised input is active and holds
until IAR ack — and same-clock inputs carry no synchroniser that could swallow
the 1-cycle pulse, so the pulse is captured and held, and the kernel's level
flow (mask → handle → ack) cannot lose it. The DTS `xlnx,kind-of-intr = <0x0>`
therefore equals the shipped hardware **by construction** (the post-validate
assert is what makes it a fact, not an intent). Still asserted:
`C_NUM_INTR_INPUTS==4` after propagation (the linux_soc hw agent was burned by
exactly this parameter; reads back 4).
**Test:** `/proc/interrupts` shows all four lines; each fires (timer via pwm bind is
deliberately impossible — use eth/uartlite/hwicap).
**Risk:** LOW — the former MED half (kind-of-intr) is closed by the BD asserts.

### 2.4 AXI TIMER — no driver, deliberately
Mainline 6.18.7 has zero Xilinx clocksource files; timebase is rdtime/stimecmp. Node
describes hardware only and avoids `xlnx,axi-timer-2.0` (would bind `pwm-xilinx`).
The firmware's `sys_now` consumer dissolves into the kernel clocksource.
**Risk:** none.

### 2.5 LAN9220 — `smsc911x` (in-tree, mature)
The whole reason the harness works: all seven TCP services ride this port.
- **Configs:** `CONFIG_NET_VENDOR_SMSC=y`, `CONFIG_SMSC911X=y`, `CONFIG_SMSC_PHY=y`
  (internal PHY, addr 1), plus the usual `NETDEVICES/PHYLIB` (rv32_defconfig has the
  net core; verify `CONFIG_MII` gets selected).
- **DT bindings that encode firmware lessons:** `reg-io-width = <4>` — **CORRECTED
  2026-07-18** (was `<2>`, which caused a heap-corruption Oops ~57 s into boot; see
  the crash note below). `C_MEM0_WIDTH 16` is the axi_emc EXTERNAL memory-pin width
  (EMC↔LAN9220), NOT the AXI-side access width `reg-io-width` selects: the EMC
  assembles two 16-bit external cycles into one 32-bit AXI beat, so software sees a
  32-bit device. Ground truth = the silicon-proven firmware, which does a single
  32-bit access per register AND per FIFO word (`platform_regs.h` MPS3_REG32,
  `harness_app/lan9220.c`). `<4>` → smsc911x uses `readl`/`ioread32_rep`
  (byte-identical to firmware); `<2>` → paired narrow `readw(reg)+readw(reg+2)`,
  which double-touches the popping RX/TX data FIFOs and overruns the skb. Mainline
  precedent for LAN9115/9118 on a memory-mapped bus: `arm/vexpress-v2m-rs1.dtsi`,
  `arm-realview-pb1176/pb11mp` all use `reg-io-width = <4>` (MPS2 an385/an399 omit
  it = 16-bit default, but those ride the Arm CMSDK 16-bit AHB, not this AXI EMC).
  Also `smsc,irq-active-high` + `smsc,irq-push-pull` (§0 above);
  `smsc,force-internal-phy`; `local-mac-address = 02:00:00:4D:50:53` (the shell's
  locally-administered MAC — keeps the fpgahub 192.168.10.101 block and ARP caches
  stable across the cutover).
- The two silicon-found bare-metal behaviours (TX-status-FIFO drain, RX-overrun
  ack+dump) are already handled inside the stock driver — **do not re-port them**
  (SERVICE_DISPOSITION §3.1).
- **Deliberate decision required (red-flag 6):** the shell MAC runs promiscuous
  because two MACs share the physical port. Linux default is NOT promiscuous. Either
  the control daemon does `ip link set eth0 promisc on` at start (faithful), or the
  successor formally drops the shared-port model — decide, don't inherit silently.
  Until the ethernet wave lands (VPHY/GENCHK deferred, RMII RX tied off) the DUT MAC
  cannot transmit anyway, which makes non-promisc *appear* fine — that's the trap.
- **EMC AC timing is frozen** (contract §9.5): the deliberately slow TCEDV/TAVDV=50ns
  etc. stand until the LAN9220 AC table is verified (inherited A6 flag). Do not let
  anyone "optimize" it because Linux feels slow.
**Test plan:** probe log shows `LAN9220 identified` + PHY attach; IRQ count rises in
`/proc/interrupts` (proves §0 end-to-end); ping/iperf3 to fpgahub; then the real
gate: pyverify's TCP services against the daemons. Off-board: none (QEMU has no EMC).
**Risk:** MED→LOW after the 2026-07-18 `reg-io-width` fix (`<2>`→`<4>`); the 32-bit
path now matches the firmware exactly. If probe reads garbage IDs, suspect byte-lane/
endian on the EMC before the driver (BYTE_TEST auto-selects WORD_SWAP).

### 2.6 OVLSTORE QSPI + SST26 — `spi-xilinx` + `spi-nor` (in-tree), **disabled on this baseline**

> **SUPERSEDED 2026-09-24. Nothing in the Linux harness may be able to program or
> erase the DUT's SST26 flash.** ILA finding #24
> (`docs/planning/linux_lanes/FINDINGS_TRIAGE.md`): something programmed 13 bytes of
> the DUT's SST26 at `0x20000` to zero (the MicroPython image's cold prologue), and
> nobody knows what did it. The SST26 is the DUT's (D16): its pads belong to the RP,
> and the DUT's own map has it at `qspi_mem 0x7000_0000` / `qspi_ctrl 0x7400_0000`.
> `0x44A4` is now D13's `usd_spi` (the user microSD, `spi-usd` + `mmc_spi`,
> IMAGE_CONTRACT §9), not a QSPI master. Everything below this box is history. **Do not
> enable it on any fork.**
>
> **Enforced, each gate with a negative control:**
> - The kernel `.config`: `MTD`, `MTD_SPI_NOR`, `SPI_XILINX` and `SPI_XILINX_QSPI`
>   must be unset. Checked by `mps3_image.py kconfig-gate`, from build.sh step 6 and
>   the post-build hook; tests in `sw/br2_external/tests/run.sh`.
>   `configs/kernel_fragment_harness.config` says `# CONFIG_MTD is not set`: rv32's
>   defconfig turns MTD and SPI-NOR on, and the 2026-09-23 image still had them.
> - The DTS: no node at `0x7000_0000` / `0x7400_0000`, and no `jedec,spi-nor`
>   anywhere. Checked by `tools/dts_gates.py` G9.
Working tree removed the `SPI_0` external port (RP owns the SST26 pads, QSPI v0.2) —
the node is `status="disabled"` because the master drives nothing. On a
`0xE4B1C44A`-baseline fork, flip to `okay` and:
- **Configs:** `CONFIG_SPI=y`, `CONFIG_SPI_XILINX=y`, `CONFIG_MTD=y`,
  `CONFIG_MTD_SPI_NOR=y`, `CONFIG_MTD_OF_PARTS=y` (fixed-partitions), optional
  `CONFIG_MTD_BLOCK` for dd-style tests.
- Partition layout in the DTS = the frozen `overlay_store.h` offsets (header/slot-A/
  slot-B/clearing-STAGE/clearing-CACHE). `ovlstore_codec.c` is pure and host-tested —
  port it into the daemon unchanged, over `/dev/mtd*`.
- **Sign-off item (red-flag 5):** kernel `spi-nor` does a **global** SST26 unlock
  where firmware did scoped WREN+WBPR unlock/re-lock per operation. That is a real
  behavioural widening of the write-enable window. Options: accept and document, or
  carry a small spi-nor fixup. Decide before first write.
- **No IRQ wired** (INTC full at 4): `spi-xilinx` falls back to polled transfers when
  the platform IRQ is absent — **CONFIRM-AT-INTEGRATION on 6.18** (probe must not
  -ENXIO out; if it does, the cheap fix is a 5th INTC input in the successor BD, a
  static-side change that is free here since static_id re-mints anyway).
- **Operational hazard unchanged:** D16 — no slot A/B staging or commit runs on the
  shared board until the flash collision is owned.
**Test plan (when enabled):** JEDEC ID `bf 26 42` in dmesg; 5 MTD partitions appear;
read-only first (`mtd_debug read` + CRC vs a host copy of the flash image); erase/
program tests only off-board or post-D16.
**Risk:** disabled now; MED when live (block-protect semantics + D16).

### 2.7 DUT clock — `clk-xlnx-clock-wizard` (in-tree)
`CONFIG_COMMON_CLK_XLNX_CLKWZRD=y`. Replaces the firmware's hand-rolled PG065
sequence. The 6900 daemon's `set_clk` verb becomes `clk_set_rate()` on output 0
(expose via a tiny sysfs shim or the daemon's own clk consumer; note the kernel does
not export clk controls to userspace by default — plan the daemon as the consumer via
a trivial custom glue, or use debugfs `clk_set_rate` only for bring-up, it is not a
production API).
- The CLKRST `DUT_CLK_SEL/DUT_CLK_DRP` registers are **inert** (RTL never wired
  them) — nothing to drive there, matching SERVICE_DISPOSITION §3.6.
- **CONFIRM-AT-INTEGRATION:** `xlnx,speed-grade` (believed -2 for the MPS3 KU115 —
  read the part from the BD); driver-computed M/D/O values vs the firmware's proven
  presets (25/50/100 MHz off the 1000 MHz VCO) — compare on first bring-up.
- D12 (real dut_clk target) inherited, still open.
**Test plan:** `cat /sys/kernel/debug/clk/clk_summary` shows the wizard + rates;
set 25/50/100 MHz, verify LOCKED and that the DUT heartbeat (CLKRST) tracks; UARTBR
SWO divisor must be reprogrammed after any rate change (daemon rule, ≥8× oversample).
**Risk:** MED — set_rate picks its own dividers; must be proven equivalent to the
PG065 presets at the three canonical rates.

### 2.8 HWICAP + DFXCTL — **the custom kernel work item** (flagged, not solved)
No in-tree fpga-manager for AXI HWICAP exists (verified in SERVICE_DISPOSITION §4 —
in-tree Xilinx managers are Zynq devcfg/versal + slave-serial/SPI). Target shape:
`fpga-region` = custom **fpga-manager (soclabs,mps3-hwicap-fpga-mgr)** + custom
**fpga-bridge (soclabs,dfx-ctl-1.0)**; the DTS carries both compatibles plus
`generic-uio` fallback and a `status="disabled"` fpga-region node as design intent.
- **Interim (day-one) plan:** UIO mmap of both blocks; the swap daemon ports
  `swap_fsm.c` sequencing to userspace. The pure transition table
  (`swap_fsm_transitions.c`) and its host tests port **verbatim** — reuse them.
- **When the drivers land, remove `generic-uio` from these two nodes** so UIO and the
  custom drivers can never race for the same page.
- The full must-reproduce list is SERVICE_DISPOSITION §4 and is normative: decouple +
  rp-reset confirm-polls; clearing-before-partial; CRC-before-ICAP (stream-direct
  containment exception); RM_ID verify **with the RP connected**, re-isolate on
  mismatch; fail-closed bounded polls everywhere (parked-decoupled failure state);
  CR bit0=WRITE/bit1=READ (was once swapped — silicon-found); **MSB-first word
  packing** (rv32 is little-endian; a native memcpy means ICAP never sees
  0xAA995566); FIFO-mode protocol (WFV vacancy / StartConfig / CR self-clear).
  Mode provenance, source-verified: the on-silicon shell is LITE mode (firmware
  default `MPS3_HWICAP_FIFO=0`, `platform_regs.h:276` — matches the current-silicon
  ELF), while THIS baseline's BD is `C_MODE 0` = FIFO, depth 1024; the firmware
  already carries both writers behind `HWICAP_FIFO`, so port the **FIFO** branch of
  `overlay_store.c:564/589` and keep the lite branch only if the fork ever retreats
  to the shipped BD. Chunk with cond_resched();
  progress observable mid-swap (the :6900 `swap` response is held for seconds);
  30 s RX-idle timeouts; best-effort EOS capture (whether SR_EOS asserts on this
  build is still open — capture raw SR, never gate on EOS).
- **Configs:** `CONFIG_FPGA=y`, `CONFIG_FPGA_BRIDGE=y`, `CONFIG_FPGA_REGION=y`,
  `CONFIG_OF_FPGA_REGION=y` (for the eventual region), plus the custom driver.
- CLKRST split ownership: `rp_resetn` belongs to the swap path exclusively (never
  host-writable mid-swap); `dut/dbg` resets stay daemon-accessible for the `reset`
  verb and OpenOCD srst. Reset pulses must respect the 3-FF synchronizer minimum
  assert width at the slowest dut_clk — no bare toggles.
**Test plan:** host-side: the existing transition-table unit tests, plus a mock-MMIO
harness for the packing/protocol primitives (the firmware's `MPS3_HAL_MOCK` pattern
ports). Bench: greybox↔regdemo_a/b swap loop with RM_ID verify + deliberate
wrong-RM_ID and mid-stream-abort injections proving the parked-decoupled invariant;
throughput sanity vs the proven ~570 KB/s.
**Risk:** HIGH — this is the one genuine new kernel/driver engineering effort in the
whole matrix, and it re-walks ground that took multiple silicon-found bugs to settle.
Budget accordingly.

### 2.9 UIO daemon estate (JTAGBB, DBGBR, UARTBR, GPIO, CLCD, CLCDKVM, VPHY, GENCHK, TELEM)
All bind via `uio_pdrv_genirq` with the `uio_pdrv_genirq.of_id=generic-uio` bootarg
(in the DTS `chosen/bootargs` — load-bearing; the module's of_id list is empty by
default). None has an interrupt; `uio_pdrv_genirq` accepts irq-less nodes (mmap-only
maps). **Configs:** `CONFIG_UIO=y`, `CONFIG_UIO_PDRV_GENIRQ=y`.
Per-block constraints the daemons must honour (all frozen — SERVICE_DISPOSITION §2/§3):
- **JTAGBB → (port TBD)**: [DEV-10] the block at 0x44A7 is `jtag_bb` now and no
  Linux daemon claims it. `mps3-swdd` is retired to `daemons/legacy/`, not built
  and not installed; the successor is the named follow-up in
  docs/planning/LINUX_FORK_JTAG_MIGRATION.md §3. The historical SWD row read:
- **SWDBB → :6920** (RETIRED): OpenOCD remote_bitbang byte-exact (`c` sample replies ASCII
  `'0'`/`'1'`); srst via CLKRST dbg_resetn (sense inversion still confirm-at-bring-up);
  fully gated during swaps.
- **DBGBR → :2542**: XVC v1.0, getinfo reply `xvcServer_v1.0:2048\n` byte-exact;
  XAPP1251 access ORDER (TDO only after GO self-clears); mid-swap `shift:` **stalls,
  never errors** (hw_server survival); bounded fail-closed CTRL poll.
- **UARTBR → :6930/1/2**: **reads are destructive** — exactly one reader process per
  system, ever (regmap v0.5); SWO divisor rule per §2.7; single client per port.
- **GPIO**: per-bit OWN mux is non-standard — UIO, not a lying gpiochip claim.
  Heartbeat bit0 moves into the daemon main loop (1 Hz).
- **TELEM**: keep the frozen `{"ok":false,"err":"no power sensor","lockup":...}` —
  lockup is a DFXCTL.RM_STATUS read, not TELEM. Do not "fix".
- **CLCD**: NOT fbdev/DRM (byte-command FIFO, panel GRAM is the framebuffer, writes
  dropped on full — poll STATUS.level). Port the dirty-cell text renderer as-is.
- **CLCDKVM**: keep the firmware presence gate (page DECERRs on the shipped shell —
  and note that under the MBV, a DECERR is a **real S-mode access fault**, not the
  silently-ignored MB variant, so an ungated probe segfaults the daemon — the gate is
  MORE important under Linux, and the probe must be a caught-SIGBUS/read-once dance
  or simply keyed off the DT node status).
- **VPHY/GENCHK**: `status="disabled"` until the ethernet wave lands a slave.
**Test plan:** `/sys/class/uio/*/name,maps/map0/size` inventory matches the DTS;
then per-daemon protocol conformance: OpenOCD connect (6920), Vivado hw_server
scan (2542), console echo loopback (6930-2), pyverify `ping`/`diag`/`display`
against 6900. Off-board: daemon protocol logic is host-testable against the existing
`MPS3_HAL_MOCK` register mocks — port that harness, it already exists.
**Risk:** LOW-MED. The one new failure mode vs bare metal is the DECERR→access-fault
change noted above; it applies to ANY access to a page with no slave.

### 2.10 Control-plane daemons (not drivers, but they bind the drivers)
Frozen wire contracts (SERVICE_DISPOSITION §2 **and §2A — the 2026-07-16
connection-level freeze**): :6900 single-client JSON with the **held** `swap`
response (one request, one reply possibly tens of seconds later — no interim
bytes, no server-side timeout, or pyverify breaks); second-client refusal is
**accept-then-EOF, promptly and byteless** (fpgahub's `busy`/`offline`/`wedged`
triage depends on the exact TCP shape — SERVICE_DISPOSITION §2A.1); unknown
verbs get `{"ok":false,"err":"unknown op"}` and the connection stays open
(fpgahub probes `{"op":"stats"}` and latches on `ok:false` — §2A.2);
`TCP_NODELAY` on every accepted 6900 socket (§2A.4); :69/6910 push framing
(24-byte big-endian header, validation order contractual) with 6910's
**server-close-equals-consumed / client-drains-to-EOF / zero-server-bytes**
lifecycle (§2A.3); `link`/`macgen` on this baseline must **decline without
touching the slave-less VPHY/GENCHK pages** (`{"ok":false,"err":"vphy not
present"}` / `"genchk not present"` — under the MBV a blind poke is an S-mode
access fault, not the MB's silent DECERR — §2A.5); the 14 frozen `diag` keys
(return honest substitutes/0 under Linux — schema is what's frozen); `ping` returns
`static_id` (build-time constant today — carry as a file or DT property; a successor
DT `soclabs,static-id` property on the root node is the natural home once the new
static exists) and the **last verified** rm_id, never a live DFXCTL read.

**Swap-path invariant (restored 2026-07-16, TRANSPLANT_CONTRACT §9.4 /
`tests/hostio4_hotswap`): reset any static-side `hostio4_target` on EVERY
swap**, between the isolation-confirm and release steps (RP still decoupled +
in reset). No decoupler clamp constant can un-wedge that FSM (5 of 9 states
deadlock under either `ioack` value; a wedged target hangs the NEXT DUT's first
hostio4 transaction, intermittently). Recovery = target reset or three
`ioreq2` escape toggles. No `hostio4_target` exists in today's static shell,
but the swap engine (DFX driver ioctl path and the daemon FSM above it) must
carry the hook so the hostio wave inherits it instead of rediscovering the
hang on silicon — SERVICE_DISPOSITION §4 places it in the sequence.

### 2.11 diag successor — `ramoops` (in-tree)
`reserved-memory/ramoops@bff00000` (top 1 MiB of DDR). **Configs:**
`CONFIG_PSTORE=y`, `CONFIG_PSTORE_RAM=y`, `CONFIG_PSTORE_CONSOLE=y` (+
`CONFIG_MAGIC_SYSRQ` for testing). Replaces the LMB/MDM JTAG mailbox (lost with the
LMB — contract §9.6): post-mortem via `/sys/fs/pstore` after reboot, or XSDB raw
mem-read of 0xBFF0_0000 while the box is wedged (restores the readable-while-stuck
property; pstore records are self-describing, the magic-scan discipline carries).
`scripts/mps3_diag.tcl` + tier3 gates retire and need a successor script keyed to the
ramoops layout — **explicitly out of scope here, tracked as open**.
**Test:** provoke a panic (`echo c > /proc/sysrq-trigger`), reboot, verify
`dmesg-ramoops-0` exists; XSDB read of the region matches.
**Risk:** MED — it must be *proven* readable over XSDB on this DDR (calibrated DDR
survives a CPU-only reset; a full POR loses it — document that boundary honestly:
ramoops is weaker than the BRAM mailbox across power events).

---

## 3. Kernel config delta (add to `configs/kernel_fragment.config`)

```
# Ethernet (LAN9220 on AXI EMC)
CONFIG_NETDEVICES=y
CONFIG_NET_VENDOR_SMSC=y
CONFIG_SMSC911X=y
CONFIG_SMSC_PHY=y
# SPI flash -- SUPERSEDED 2026-09-24 (§2.6, ILA #24): these four lines are now
# the OPPOSITE of the rule. The harness kernel must NOT be able to program the
# DUT's SST26; the live fragment (sw/configs/kernel_fragment_harness.config) says
#   # CONFIG_SPI_XILINX is not set
#   # CONFIG_MTD is not set
# and mps3_image.py kconfig-gate refuses MTD / MTD_SPI_NOR / SPI_XILINX* set.
# (CONFIG_SPI=y stays: spi-usd + mmc_spi for the user microSD need it.)
# DUT clock wizard
CONFIG_COMMON_CLK_XLNX_CLKWZRD=y
# UIO estate
CONFIG_UIO=y
CONFIG_UIO_PDRV_GENIRQ=y
# FPGA core (for the future custom manager/bridge/region)
CONFIG_FPGA=y
CONFIG_FPGA_BRIDGE=y
CONFIG_FPGA_REGION=y
CONFIG_OF_FPGA_REGION=y
# diag successor
CONFIG_PSTORE=y
CONFIG_PSTORE_RAM=y
CONFIG_PSTORE_CONSOLE=y
# regulator-fixed for the LAN9220 supply stubs (optional but clean)
CONFIG_REGULATOR=y
CONFIG_REGULATOR_FIXED_VOLTAGE=y
```

Watch the **kernel-size squeeze**: linux_soc's Image ends 112 KiB below the forced
DTB slot at 0x8220_0000 (README §5.3) and this delta adds real code. The mk_dtb.sh
margin assert is mandatory for this build; if it goes negative, the clean fix is
moving the DTB/initrd slots (OpenSBI re-link or a different fw_next layout), not
shrinking drivers.

---

## 4. Open items owned by this design (beyond the inherited contract §10 list)

1. ~~`xlnx,kind-of-intr` / `C_NUM_INTR_INPUTS==4` read-back from the successor
   BD.~~ **CLOSED 2026-07-16**: both pinned + read-back-asserted pre- AND
   post-validate in `shell_linux_bd.tcl`; the manual-vs-computed Vivado warning
   documented as expected/benign (LVL_P latch — §2.3).
2. ~~`spi-xilinx` irq-less probe behaviour on 6.18.~~ **SUPERSEDED 2026-09-24**
   (§2.6): no `spi-xilinx` in any harness kernel.
3. `xlnx,speed-grade` for the MPS3 KU115 part (believed 2).
4. ~~spi-nor SST26 global-unlock vs scoped-WBPR sign-off.~~ **SUPERSEDED 2026-09-24**
   (§2.6): the harness never writes the SST26.
5. Promiscuous-mode decision (daemon `promisc on` vs formally dropping the
   shared-port model).
6. clk-wizard set_rate equivalence vs the PG065 presets at 25/50/100 MHz.
7. ramoops-over-XSDB readability proof + the POR-loss boundary statement.
8. `mps3_diag.tcl`/tier3 successor tooling for pstore (explicitly not designed here).
9. The custom HWICAP fpga-manager + dfx_ctl fpga-bridge drivers themselves
   (SERVICE_DISPOSITION §4 is the spec; flagged per tasking, not solved).
10. `static_id` delivery to the daemon (`soclabs,static-id` DT property proposed —
    add to the root node when the successor static is built and minted).
11. Boot-BRAM stub build + bake (COE/updatemem) per ADDRESS_MAP.md §6 — the
    ownership and the design are written down; the artifact itself (a
    shell_linux port of `linux_soc/hw/fw_dbg/bootstub.*`) is owed with the
    first synthesis build, not with this BD.

## CHANGELOG

- **2026-09-24 (IMAGE lane, ILA finding #24):** §1 row, §2.6, §3's flash block and
  open items 2/4 **SUPERSEDED**. Something zeroed 13 bytes of the DUT's SST26 at
  `0x20000` and nobody knows what, so nothing in the Linux harness may be able to
  program or erase it. Release gates: the kernel `.config` (`mps3_image.py
  kconfig-gate`) and the DTS (`tools/dts_gates.py` G9), each with negative controls.
- **2026-07-16 (judge-fix wave):** §2.1 — SSTC/COUNTERS obligation discharged
  and extended with `C_INTERRUPT_WAKEUP==1` (silicon-proven WFI-coma fix);
  `riscv_isa` export now computed from read-backs. §2.3 — kind-of-intr
  manual-vs-computed RESOLVED per the linux_soc IRQ audit (LVL_P latch),
  read-back-asserted post-validate; open item 1 closed. §2.10 — wire contract
  references updated to SERVICE_DISPOSITION §2A (accept-then-EOF refusal,
  unknown-op reply, 6910 drain-to-EOF, TCP_NODELAY, no-slave decline shapes);
  `hostio4_target`-reset-on-swap invariant restored. Open item 11 added
  (boot-BRAM stub artifact).
- **2026-07-15:** initial matrix.
