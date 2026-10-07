# MicroBlaze shell firmware (A3)

Bare-metal C for the MPS3 static shell's MicroBlaze coordinator. This is
**not Linux** (ARCHITECTURE_SPEC.md §1 non-goal is explicit) — no MMU, no
process model, single address space, single thread of execution. Every
module body is real and the whole tree **cross-compiles to a MicroBlaze ELF**
against the static-shell XSA — see "Build model (real)" below and
`firmware/platform/BUILD_RESULT.txt` for the evidence. The `harness_app/`
pre-seam spike that used to sit alongside `platform/` has been deleted.

## Build model (real — W-VITIS, 2026-07-07)

- **Target build:** one headless command from the repo root:

  ```sh
  /apps/Xilinx/Vitis/2024.1/bin/xsct -nodisp \
      firmware/platform/create_platform.tcl \
      build/shell_proj_a1/shell_harness.xsa build/vitis_fw
  ```

  then **`make -C firmware/platform elf PRODUCT=1`** → ELF at
  `build/vitis_fw/shell_fw/shell_fw.elf` (text 177,948 / data 1,840 / bss
  599,808 = 779,596 B against a 1,048,368 B LMB → 268,772 B headroom;
  zero warnings, `-Werror`). `LMB_KB` defaults to **1024** and must track
  the shell you build against. Full detail (BSP/lwIP config table,
  memory-honesty numbers, `-nodisp` gotcha, updatemem step):
  `firmware/platform/README.md` + `firmware/platform/BUILD_RESULT.txt`.
- **`PRODUCT=1` is the flag set that is actually on the board.** It expands
  to `CLCD=1 CLCD_KVM=1 HWICAP_FIFO=1 WINDOWED=1 TOUCH=1 DUT_EGRESS=1`
  (fabric pair: `SHELL_TOUCH=1` on the bitstream build). Until it existed that set lived
  only as five literals inside a mint script, and a plain `make elf` built
  an image that pings, reports the right `static_id`, passes every
  acceptance gate — and **cannot load a single RM**, because the default
  LITE HWICAP writers do not match a FIFO shell. The Makefile now `$(error)`s
  on `TOUCH=1` without `CLCD=1` and on `CLCD_KVM=1` without `CLCD=1`, and
  `$(warning)`s when `HWICAP_FIFO` is unset. Flags always change with a
  `make clean` first — make cannot see a changed `-D`.
- **Toolchain:** Vitis 2024.1 (matching the DFX flow per
  overlay-manifest.md) bare-metal MicroBlaze BSP generated from the A1
  XSA. The BSP supplies `xparameters.h` + the standalone libc; HWICAP and
  Quad SPI are poked through `common/platform_regs.h`'s own offsets
  (cross-check against the generated `x*_l.h` remains a bring-up item —
  that header's note).
- **Network stack:** lwIP 2.2.0 (BSP `lwip220`; the tasking's `lwip213`
  is not published in 2024.1's catalog — deviation documented in
  `platform/create_platform.tcl`), **RAW API, single superloop**, no RTOS.
  Rationale unchanged: swap_fsm is a non-blocking stepper precisely so a
  multi-second ICAP write can't starve the TCP connection driving the
  swap; treat every module's `_poll()` as "must return promptly, no
  blocking I/O." The seam's lwIP backend is
  `firmware/platform/src/net_if_lwip.c` — the one-thin-file
  `common/net_if.h` promised; no other firmware file includes an lwIP or
  Xilinx header.
- **Memory map (MicroBlaze-local):** `common/platform_regs.h` is the single
  source of AXI4-Lite peripheral addresses (shell-regmap.md v0.2). Program
  memory is the **1 MiB** shared-I/D LMB BRAM (no DDR on this BD;
  `LMB_KB=1024`, which is what lets nanosoc's 155,864 B clearing live in
  RAM). The knob is not cosmetic: the diagnostic mailbox is anchored to the
  END of the LMB and the LMB decode **aliases**, so an image linked for the
  wrong size silently reads the wrong mailbox. The running image reports the
  size it was linked for as `version.lmb_kb` (below). See
  `platform/README.md` "Memory honesty".

## The superloop — it lives in `platform/src/main.c`

There is exactly ONE superloop, and it is **not** in `coordinator/`.
`coordinator.c` used to carry a second copy, `coordinator_main_loop()`,
which was compiled into every image and called by nothing on the target; it
was deleted (2026-09-09) rather than left to drift. It already had: it still
polled `swd_server` (TCP 6920), which the JTAG cutover retired.

```
coordinator_init()          -- coordinator/coordinator.c
    -> clkrst / swap_fsm / config_agent / overlay_store init
    -> jtag_server_init()   -- TCP 6921 (jtag_bb @0x44A7) is the LIVE debug service
    -> xvc_server_init(), uart_over_eth_init()
    -> coordinator_net_init()              -- TCP 6900 control listener
    -> overlay_store_boot_load_default()   -- §8A.3, board self-boots to
                                              default DUT with no host
main()'s for (;;)  -- platform/src/main.c, the ONLY loop:
    mps3_net_lwip_rx_poll(8); mps3_net_lwip_tmr();  -- keep lwIP alive
    smsc911x_tx_status_drain();  -- an un-drained TX status FIFO halts the MAC
    swap_fsm_poll();             -- coordinator/swap_fsm.c, non-blocking
    config_agent_poll();         -- config_agent/config_agent.c
    coordinator_net_poll();      -- 6900 line assembly -> dispatch
    jtag_server_poll();          -- jtag_server/jtag_server.c (6921)
    xvc_server_poll();           -- xvc_server/xvc_server.c
    uart_over_eth_poll();        -- uart_over_eth/uart_over_eth.c
    clcd_poll();                 -- #ifdef MPS3_HAS_CLCD (CLCD=1)
    mps3_diag_publish(...);      -- refresh the JTAG-readable diag mailbox
```

Control-channel (6900) requests are dispatched synchronously into
`coordinator_handle_*()` — fast, non-blocking JSON in/out only; anything
slow (e.g. `swap`) just arms `swap_fsm` and returns, and its response is
HELD until the FSM settles.

**Verbs** (docs/contracts/net-protocol.md v0.8): `ping`, `reset`, `set_clk`,
`swap`, `link`, `commit`, `telemetry`, `macgen`, `diag`, `display`,
`version`; v0.10–v0.11 `dutrx`, `stats`, `log`, `touch_cal`, `reboot`; v0.14
`slot` (the Linux harness's user-microSD boot slots — this image declines it
through a weak provider, `mps3_slot_op()` in `coordinator/coordinator.c`, and
refuses the matching 6910 push kind 2 through `mps3_cfg_slot_sink()` in
`config_agent/config_agent.c`). `version` (v0.8) is how a RUNNING image says what it is —
harness release, `HARNESS_VER32`, build sha/dirty, the LMB size it was
linked for, and its compile-time feature set. `ping` cannot answer that: it
reports the FABRIC's `static_id`, and one `static_id` serves many harness
releases.

## Port -> module map (net-protocol.md)

| Port | Proto | Service | Module |
|---|---|---|---|
| 6900 | TCP | control/status JSON-lines | `coordinator/` |
| 69 | UDP | TFTP clearing/partial push | `config_agent/` |
| 6910 | TCP | raw partial push (alt TFTP) | `config_agent/` |
| 2542 | TCP | XVC -> Debug Bridge | `xvc_server/` |
| 6921 | TCP | **JTAG (OpenOCD remote_bitbang) — the live debug service** | `jtag_server/` |
| 6920 | TCP | SWD (OpenOCD remote_bitbang) — **NOT in the default image** | `swd_server/` |
| 6930 | TCP | UART0 (boot monitor) | `uart_over_eth/` |
| 6931 | TCP | UART1 (application) | `uart_over_eth/` |
| 6932 | TCP | SWO/ITM trace | `uart_over_eth/` |

The **JTAG cutover** made 6921 the debug port: `coordinator_init()` calls
`jtag_server_init()`, `main.c` polls `jtag_server_poll()`, and nothing on
the target calls `swd_server_*` at all. `swd_server.c` was nevertheless still
in the target link set, so every shipped image carried an unreachable
service; it is now gated behind `LEGACY_SWD=1` in `platform/Makefile`. The
SOURCE stays — `firmware/test/test_swd_server.c` links it directly and pins
the OpenOCD remote_bitbang byte encoding that `jtag_server.c` inherits.

`clkrst/` and `overlay_store/` back the control channel's `set_clk`/`reset`
and `commit`/boot-default-load paths respectively rather than owning a port
of their own. `smsc911x/` is a driver, not a service (real bare-metal port
now, W-SMSC — Apache-2.0-derived from Zephyr's `eth_smsc911x.c`, see its
README's provenance note).

All port servers are written against the **network seam**
(`common/net_if.h`, W-NET-SEAM): non-blocking listen/accept/recv/send/close
(+ a UDP surface for TFTP) with three backends — the host harness's
`firmware/test/fake_net_if.c` (in-memory queues, what most test binaries link),
the real lwIP RAW-API glue `firmware/platform/src/net_if_lwip.c` (W-VITIS; design
notes in `platform/README.md`), and `firmware/test/posix_net_if.c` (REAL POSIX
sockets, host-gcc only, added 2026-07-30) which lets a REAL EXTERNAL CLIENT drive
real firmware protocol code board-free — that is how the licensed Synopsys
Identify debugger now drives the real `xvc_server/` engine with no MicroBlaze
(`firmware/test/xvc_fw_daemon.c`, `host/identify/fw_com_check.sh`). No firmware
module outside `firmware/platform/` includes an lwIP or Xilinx header.

## Directory map

| Dir | Contents |
|---|---|
| `common/` | `platform_regs.h`, `net_proto.h` — the shared contract headers; `net_proto.c` — REAL control-line JSON codec (W-JSON) and bitstream-header pack/unpack; `net_if.h`/`net_if.c` — the network SEAM every port server is written against (W-NET-SEAM; lwIP backing = `platform/src/net_if_lwip.c`) |
| `coordinator/` | swap state machine (the heart; §6.2/§7); dispatch + all eleven verb handlers wired to the real codec; `coordinator_net.c` — REAL TCP 6900 listener (line assembly, send-now vs held-`swap`-response parking). NOT the superloop — that is `platform/src/main.c` |
| `config_agent/` | REAL TFTP-WRQ + raw-6910 receive sessions (header -> payload -> CRC), two-slot {clearing, partial} PAIR staging (see its header's STAGING DECISION) |
| `overlay_store/` | QSPI A/B-slot manager (§8A) — REAL AXI-Quad-SPI/SST26 leaves, chunked CRC verify, contract-ordered boot load, verify-before-flip commit (W-SPI) |
| `jtag_server/` | TCP 6921 remote_bitbang JTAG (jtag_bb @0x44A7) — **the live debug service** |
| `swd_server/` | TCP 6920 remote_bitbang -> SWDBB DRIVE/SAMPLE pokes (§10.1). Retired by the JTAG cutover; built only with `LEGACY_SWD=1`, kept for its host test |
| `xvc_server/` | REAL XVC v1.0 server (§10) — getinfo/settck/shift over TCP 2542, <=32-bit DBGBR shift chunks (PG245-pattern offsets in platform_regs.h, flagged for bring-up confirm); stall-while-gated policy (its README) |
| `uart_over_eth/` | UART0/UART1/SWO UARTBR <-> TCP relay (§11) — real, incl. destructive-read holdback, tx_full backpressure, SWO_CFG bring-up |
| `clkrst/` | DUT clock (DRP) + reset helpers (§5) |
| `smsc911x/` | REAL bare-metal LAN9220 driver (W-SMSC; Apache-2.0-derived — see its README); pbuf glue lives in `platform/src/net_if_lwip.c` |
| `platform/` | W-VITIS target build: xsct platform/BSP script, mb-gcc Makefile (`PRODUCT=1`, `LMB_KB`, the consistency gates), **the** `main.c` superloop, `net_if_lwip.c` (the seam's lwIP backend), the harness-version seam (`mps3_version.h` + weak/generated pair), greybox blob generator, BUILD_RESULT.txt evidence |
| `test/` | host-gcc harness: fakes/mocks + **38** test binaries, `make -C firmware/test test`. Built `-Werror` |

## Open ambiguities — mostly RESOLVED in shell-regmap.md v0.1

The six register-level gaps `common/platform_regs.h` used to carry as
`AMBIGUITY(A6)` comments (I5-I10) are now real, regmap-confirmed blocks:
SWDBB (I5), DBGBR (I6), UARTBR (I7), the DFXCTL.RM_ID/RM_STATUS RM-load-
verify pair (I8), TELEM's field table (I9), and GENCHK's field table (I10 —
though I10's *protocol verb* is still missing, see GENCHK's comment in
platform_regs.h). The swap clearing-bitstream-sourcing ambiguity (I2) is
also resolved — see `coordinator/README.md` "Clearing-bitstream sourcing".
Still open/flagged: the SWD/RMII bit-order (I22, needs a real bring-up to
confirm against host `bitbang.c`), and I15 (OVLSTORE header endianness —
`overlay_store/ovlstore_codec.h` implements the OPEN_ISSUES.md-documented
little-endian resolution, which currently DISAGREES with
`tests/common/ovlstore_header.py`'s big-endian choice; needs A6
reconciliation). See `docs/contracts/OPEN_ISSUES.md` for the full,
currently-tracked list.
