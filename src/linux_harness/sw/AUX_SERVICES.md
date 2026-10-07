# AUX_SERVICES — M4 aux-service daemons (XVC / SWD / UART bridge / telemetry / diag successor)

Date: 2026-07-17. Track M4 of the shell_linux transplant, OFF-BOARD only.
Ports the remaining bare-metal superloop services to Linux userspace per the
frozen dispositions (SERVICE_DISPOSITION.md §2/§3.7-3.11, DRIVER_MATRIX.md
§2.9-2.11). QEMU/host-testable parts are TESTED (evidence below); board-only
parts are stubbed and flagged. No git add/commit performed.

## 1. What shipped

| Service | Port(s) | Daemon | Backend | Status |
|---|---|---|---|---|
| XVC v1.0 -> Debug Bridge | TCP 2542 | `daemons/mps3_xvcd.c` | UIO `dbgbr` / `-m` mock | conformance-tested (mock) |
| ~~OpenOCD remote_bitbang -> SWDBB~~ | ~~TCP 6920~~ | `daemons/legacy/mps3_swdd.c` | — | **RETIRED [DEV-10]** — the block at 0x44A7 is `jtag_bb`; not built, not installed, not started. Successor: docs/planning/LINUX_FORK_JTAG_MIGRATION.md §3 |
| UARTBR 3-stream relay | TCP 6930/6931/6932 | `daemons/mps3_uartbrd.c` | UIO `uartbr` / `-m` mock | conformance-tested (mock) |
| telemetry / link / macgen / diag verbs | (on 6900) | already in `mps3-ctrld` (Wave-2) | — | fixture-covered (wire suite: `telemetry-frozen-failure`, `link-baseline-decline`, `macgen-baseline-decline`, `commit-baseline-decline`, `diag-14-frozen-keys`) — NOT duplicated here |
| diag-mailbox successor | — | ramoops @0xBFF0_0000 + `S15pstore` | kernel PSTORE_RAM | plumbing shipped; proofs pending image/board (§5) |

Shared backend seam: `daemons/auxhw.{h,c}` — UIO mmap (via the existing
`uio.c` helper, `/sys/class/uio` name match) or an in-process behavioural
mock (`-m`), the userspace analogue of firmware/test/'s `MPS3_HAL_MOCK`
register mocks. Register layouts are ported verbatim from
`firmware/common/platform_regs.h` (shell-regmap v0.5) — DBGBR/SWDBB/CLKRST/
UARTBR blocks only.

Image wiring: `daemons/Makefile` builds the three new bins;
`br2_external/package/mps3-harnessd/mps3-harnessd.mk` installs them to
`/usr/sbin`; NEW init script `S91mps3aux` starts each daemon ONLY if its UIO
block is present (name-matched in /sys/class/uio — on a QEMU `-M virt` smoke
image none start, so `boot_qemu_harness.sh`'s 5-point verdict is untouched).
`S90mps3d` (ctrld/pushd) was deliberately not modified.

## 2. Frozen-contract obligations implemented (each traceable)

**mps3-xvcd** (port of `firmware/xvc_server/xvc_server.c`):
- `getinfo:` -> `xvcServer_v1.0:2048\n` BYTE-EXACT; `settck:` stores + echoes
  the 4 LE period bytes (advisory — the Debug Bridge has no TCK divider).
- `shift:` accumulate-and-reevaluate parsing (commands are NOT
  newline-framed); num_bits 0 or >2048 or a non-XVC prefix = drop the client
  (fail closed, never resync-guess).
- DBGBR access is the XAPP1251 ORDER per <=32-bit chunk: LENGTH, TMS, TDI,
  CTRL=GO, bounded poll on GO self-clear (fail closed on timeout = dead
  bridge), THEN read TDO.
- Mid-swap gating is STALL-NOT-ERROR for `shift:` (command stays queued, no
  DBGBR access, no reply — hw_server treats a short reply as a dead cable);
  getinfo/settck still answered while gated. 50 ms gate-repoll.
- Single client; extras accept-then-immediate-close, zero bytes.

**mps3-swdd** (port of `firmware/swd_server/swd_server.c`) — **RETIRED [DEV-10]**,
kept for its byte encoding only:
- Byte map CONFIRMED against OpenOCD `remote_bitbang.c` (host-driver-fixed):
  `'d'+(swclk<<1|swdio)`, `'r'+(trst<<1|srst)`, `'c'` sample reply is EXACTLY
  one ASCII `'0'`/`'1'`; `'O'/'o'` drive/release; `'B'/'b'` accepted-ignored;
  `'Q'` quit; anything else drops the connection.
- DRIVE is written whole from the mirrored pin state every time (plain
  write-through register, mirror stays authoritative).
- srst -> CLKRST dbg_resetn (1=released: assert = CLEAR the bit). trst
  correctly ignored (SWD, no trst).
- While gated the daemon stops CONSUMING bytes entirely (they queue in the
  kernel socket buffer; serviced once ungated). Gate re-checked after every
  poll wake — closes the raced-gate window.

**mps3-uartbrd** (port of `firmware/uart_over_eth/uart_over_eth.c`):
- Destructive-pop discipline: `{valid,data}` reads never used as peeks; a
  popped-but-unsendable byte is held (1-byte rx holdback). ONE reader
  process per system serves all three ports (DRIVER_MATRIX §2.9 corollary).
- Host->DUT: FIFO_STATUS.tx_full polled BEFORE every push (hardware drops
  pushes while full); held byte + retry keeps the relay lossless.
- SWO is RX-only: client bytes drained-and-dropped; SWO_CFG written ONCE at
  init (divisor 24 | ENABLE — one write is the whole handshake).
- Swap gating: registers untouched, TCP client KEPT (bridge FIFOs are not
  reset by a swap; bytes resume once ungated); client hangup still noticed
  (EOF peek).
- Adopted clients are serviced before accepts (ctrld's reap-then-adopt
  discipline) so a close+reconnect inside one poll wake is not refused.

## 3. Cross-service gating seam (stub until the swap engine owns it)

Gate = existence of `/run/mps3/swap_gate` (override `-g`; `auxhw.h`
`MPS3_SWAP_GATE_DEFAULT`). CONTRACT: the swap engine owner (mps3-ctrld's
real FSM / the mps3_dfx driver path) creates the file BEFORE
decouple+shutdown (§4 step 1 "gate/quiesce consumers") and removes it after
release+verify. File-existence chosen over IPC so a crashed swap owner
fails obvious (stale gate = services visibly gated), and daemons need no
connection to ctrld. **Open item:** wire the gate create/remove into the
swap engine when the M3 swap path lands — today nothing creates it in
production (the test suite exercises it directly).

## 4. Test evidence (host, 2026-07-17)

`tests/aux_services_test.py` — self-contained: spawns all three daemons in
mock mode on high ports with a private gate file, runs, kills.
**34 passed, 0 failed** (3 consecutive clean runs). Coverage:
- XVC: getinfo byte-exact; settck echo; 8/13/2048-bit shifts (loopback TDO,
  partial-byte masking, 64-chunk max vector); byte-dribble accumulation;
  pipelined commands; second-client byteless refusal; zero-bit / oversize /
  bad-prefix fail-closed drops; gated getinfo-answered + shift-stall +
  service-on-ungate.
- SWD: write/sample conformance re-derived from the OpenOCD formulas (not
  hardcoded letters); single-byte sample replies; released-bus pull-up;
  reset chars; LED chars; quit; invalid-byte drop; second-client refusal;
  gated no-service + queued-bytes-on-ungate.
- UARTBR: U0/U1 echo loopback; 2000 B bulk lossless (holdback machinery);
  SWO banner delivery + client-bytes-dropped; second-client refusal;
  adopted-survives-refusal; gated no-relay + lossless-after-ungate.

The telemetry/link/macgen/commit decline shapes and the 14 frozen diag keys
remain covered by `tests/wire_compat_test.py` fixtures (gate-verified 37/37
in Wave-2); M4 adds nothing to 6900 and did not touch ctrld/pushd sources.

## 5. Board-only stubs / honest gaps

1. **All three daemons have never touched real silicon.** UIO backends are
   compile-proven only; the mocks pin protocol logic, chunking, byte order
   and gating, not the RTL. First-board checks: XVC `hw_server` scan chain,
   OpenOCD SWD attach, UART echo against a real DUT image.
2. **srst sense inversion** (CLKRST dbg_resetn) is still the firmware's
   confirm-at-bring-up TODO — carried, not resolved.
3. **SWO divisor 24** is inherited firmware policy (~2 MBaud @ 50 MHz
   dut_clk); revisit when the DUT TPIU config is pinned.
4. **DBGBR GO-poll bound** (100k reads) is tuned for the bare-metal AXI
   pace; on the MBV it is safely larger than needed — a dead bridge still
   fails closed, just marginally slower.
5. **Ramoops proofs pending**: (a) panic -> reboot -> `dmesg-ramoops-0`
   check needs the S1/S2 image (Buildroot build was re-running as of this
   track); (b) XSDB raw read of 0xBFF0_0000 while wedged is BOARD-ONLY
   (DRIVER_MATRIX open item 7); (c) POR-loss boundary documented in
   `S15pstore` — ramoops is weaker than the BRAM mailbox across power
   events. The `mps3_diag.tcl` successor script keyed to the ramoops layout
   remains open (matrix item 8).
6. **Gate wiring** — §3 open item (swap engine must own create/remove).
7. **Throughput**: uartbrd relays ~2 KB per 5 ms pass per direction
   (~400 KB/s) — ample for consoles and 2 MBaud SWO; not a bulk pipe.

## 6. Concurrent-track note (2026-07-17)

The M3 swap-path track was editing `daemons/mps3_ctrld.c`/`mps3_pushd.c`
(+ new `spool.*`, `swap_worker.*`) in parallel with this work. M4 touched
only: `auxhw.*`, `mps3_xvcd.c`, `mps3_swdd.c`, `mps3_uartbrd.c`, the three
aux rules in `daemons/Makefile` (BINS + per-bin rules; M3's ctrld/pushd
rule updates merged cleanly around them), `mps3-harnessd.mk` install lines,
NEW `S91mps3aux`/`S15pstore`, `tests/aux_services_test.py`, this file.
