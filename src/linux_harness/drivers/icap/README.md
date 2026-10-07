# ICAP-SPIKE — DFX swap under Linux (`drivers/icap/`)

The program-#1-risk work item (DRIVER_MATRIX §2.8, SERVICE_DISPOSITION §4):
a kernel driver that reproduces the silicon-proven MPS3 shell swap engine —
AXI HWICAP write protocols + DFXCTL/CLKRST sequencing — as a Linux chardev
with an fpga-manager/fpga-bridge veneer, plus a two-tier test harness (host
unit tests + QEMU with a mock HWICAP) driving recorded swap sequences.

## Layout

| File | What |
|---|---|
| `mps3_icap_regs.h` | Register contract (subset port of `firmware/common/platform_regs.h`, provenance kept: CR bit0=WRITE silicon note, FIFO-vs-lite provenance, MSB-first pack primitive) |
| `mps3_swap_transitions.{c,h}` | The **pure transition table, ported verbatim** from `firmware/coordinator/swap_fsm_transitions.c` (frozen enum values; re-port from firmware on any change) |
| `mps3_icap_engine.{c,h}` | The swap engine: FIFO + lite HWICAP writers, bounded fail-closed polls, decouple/release confirm-polls, RM_ID verify + re-isolate, stream-direct packer with ≤3-byte residue, EOS capture. Kernel-independent C over an ops vtable (the firmware's thin-HAL split) |
| `mps3_icap_mock.{c,h}` | Behavioural mock of HWICAP/DFXCTL/CLKRST behind the same vtable — mechanism justification in its header |
| `mps3_crc32.h` | zlib/IEEE CRC-32 (the 6910 wire-header variant), shared kernel/host |
| `mps3_dfx_uapi.h` | `/dev/mps3dfx` ABI: SWAP_BEGIN → PUSH(clearing) → PUSH(partial, staged or stream-direct) → SWAP_FINISH; ABORT; GET_STATUS; MOCK_* |
| `mps3_dfx_drv.c` | The kernel module: miscdev, single-open, vmalloc staging, 30 s RX-idle reap (delayed work), sysfs progress attrs, fpga-mgr/bridge veneer |
| `Kbuild` / `Makefile` | External-module build vs the Buildroot 6.18.7 tree; `check-fpga` veneer compile check; host tests; swaptool |
| `tests/host/` | Host unit tests (run anywhere, seconds): table, packing/CRC vectors + real-`.bin` sync scan, 14 engine-vs-mock scenarios |
| `tests/qemu/` | QEMU harness: payload disk with **recorded regdemo_a/b clearing+partial `.bin` pairs**, `swaptool` scenario driver, serial-console grading |
| `COVERAGE.md` | Honest coverage notes — what is proven, what is not |

## What the engine reproduces (SERVICE_DISPOSITION §4, item by item)

- **MSB-first word packing**: one shared primitive (`mps3_hwicap_pack_word`),
  explicit shifts, never memcpy; the stream-direct path assembles the same
  order incrementally across split write()s (word straddling a TCP segment).
  Proven end-to-end: the mock re-serialises every ICAP word big-endian and
  CRCs it — the capture CRC must equal the CRC of the source file bytes.
- **Lite vs FIFO CR protocols**: both writers implemented (`fifo_mode`
  module param; default FIFO to match this BD's `C_MODE 0`, depth 1024;
  lite = the on-silicon bare-metal shell). Same pack primitive both ways.
- **Chunking with scheduling points**: `ops->relax()` = `cond_resched()`
  between chunks and inside every bounded poll (the superloop constraint's
  Linux residue).
- **Decouple / rp-reset confirm-polls**: bounded, fail-closed, table-driven;
  assert writes re-issued per poll exactly like `step_decouple_assert()`.
- **Clearing-before-partial**: driver refuses a partial push (staged or
  stream-direct) until the clearing streamed this session; clearing is
  always staged (CRC strictly before ICAP); a staged CRC reject keeps the
  session armed for a re-push (config_agent semantics).
- **Stream-direct containment**: `sd_begin` fails closed unless the engine
  is at AWAIT_PARTIAL **and** DFXCTL confirms parked (both halves of the
  2026-07-10 silicon fix); a trailing-CRC failure fails the swap parked.
- **RM_ID verify + re-isolate-on-mismatch**: verify with the RP connected
  (decoupler clamp modelled in the mock), "not yet valid" polls to timeout,
  "valid but wrong" re-isolates immediately; AXI shutdown stays asserted
  across release and clears only at the commit point; dut+dbg resets
  released only after verify (both silicon lessons carried).
- **30 s RX-idle timeout**: delayed work re-armed on every byte of write()
  progress — bounds silence, never a slow transfer; reap parks the RP and
  fails the session (`idle_ms` param; QEMU test runs it at 1.5 s).
- **Fail-closed bounded polls**: every wait (WFV, CR self-clear, confirms,
  rm_id settle, EOS) is bounded; every failure path ends parked-decoupled.
- **hostio4-target reset hook** (TRANSPLANT_CONTRACT §9.4): call site
  carried between partial-stream-done and release, RP still parked; stub
  today (no hostio4_target in the current static), counted and asserted in
  tests so it cannot be silently lost.
- **EOS**: bounded capture of raw SR + `eos_status` (sysfs + GET_STATUS).
  Default is **capture-only** per §4 ("never gate on EOS alone" — whether
  EOS asserts on this axi_hwicap build is still open); `eos_strict=1`
  restores the firmware `icap_direct_finish` fail-closed gate. Both modes
  tested.

## Deliberate deltas vs bare metal (documented, not accidental)

1. **Call-driven, not superloop**: the engine walks whole phases per call;
   every decision still goes through the ported pure table, so the decision
   logic stays diffable against firmware.
2. **AWAIT_INCOMING_CLEARING auto-ack**: the incoming pair's clearing is
   daemon-side staging under Linux (it becomes the next swap's outgoing
   clearing) and never touches the driver; the table arc is traversed,
   the ICAP-ordering invariant is enforced by the driver.
3. **Release-confirm-timeout reparks**: firmware reasons "release never took
   effect, already inert" and just fails; the engine additionally re-drives
   the park writes so a HALF-taken release cannot escape the parked-failure
   invariant. Strictly stronger conformance to the invariant.
4. **A stream-direct mid-stream error parks the whole session** (firmware
   stays in AWAIT_PARTIAL for a partial retry). Host re-arms with a fresh
   SWAP_BEGIN — simpler recovery surface, same safety.
5. **Consumer gating (SWD/XVC/UART/link) is daemon-side**: the driver
   exposes `state` via sysfs/GET_STATUS; the daemons self-gate
   (SERVICE_DISPOSITION §0 disposition).

## fpga-manager veneer (flagged: compile-checked only)

There is still no in-tree fpga-manager for AXI HWICAP. The veneer maps
`write_init/write/write_complete` onto the engine's unframed stream-direct
path and bridge-enable onto the full release+verify tail (never a bare
decouple flip), with the verify target set via the `target_rm_id` sysfs
attr. **The proven QEMU kernel has `CONFIG_FPGA` unset**, so this path is
compile-checked (`make check-fpga`, expected-unresolved symbols downgraded)
and NOT runtime-tested — the chardev is the product path regardless,
because the fpga-region model cannot express clearing-before-partial or
verify-with-re-isolate. If a CONFIG_FPGA kernel is built later, the veneer
is ready to bind and needs a runtime pass.

## Building / running

```sh
make host-tests    # seconds, any box — 3 binaries, all must print PASS
make module        # mps3_dfx.ko vs the Buildroot 6.18.7 tree (rv32)
make swaptool      # static rv32 guest tool
make check-fpga    # veneer compile check (unresolved fpga_* symbols OK)
cd tests/qemu && ./run_qemu_swaptest.sh   # full QEMU suite, ~2-6 min
```

Production DTS shape (when this graduates from spike): bind
`soclabs,mps3-hwicap-fpga-mgr` as a platform driver with dfxctl/clkrst
phandles, and **remove `generic-uio` from those two nodes** so UIO and this
driver can never race one page (DRIVER_MATRIX §2.8). The spike instantiates
at module init instead (mock=1, or ioremap of the frozen map).
