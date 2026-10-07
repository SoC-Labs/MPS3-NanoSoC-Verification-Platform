# firmware/platform/ — Vitis 2024.1 target build (A3, W-VITIS)

The platform side of the seam architecture: everything here may include
lwIP/Xilinx headers; nothing outside `firmware/platform/` (and the
generated blob) may. This directory turns the W-BD static shell XSA
(`build/shell_proj_a1/shell_harness.xsa`, RESULT.txt 2026-07-06) into the
**first real MicroBlaze ELF** of the whole firmware tree.

## Layout

| File | Role |
|---|---|
| `create_platform.tcl` | xsct headless step 1: XSA → platform + standalone BSP + lwip220 (`libxil.a`/`liblwip4.a`/`xparameters.h`/`lwipopts.h`) — display-free hsi commands only |
| `Makefile` | step 2: mb-gcc cross-compile + link of the whole firmware tree against that BSP → `shell_fw.elf` (+ map, `mb-size` report). **`PRODUCT=1`** (the fielded flag set), `LMB_KB`/`BLOB`/`LEGACY_SWD` and the consistency gates live here. Built `-Werror` |
| `mps3_version.h` + `mps3_version_weak.c` | the harness-version SEAM: hand-written accessors + WEAK "not provisioned" fallbacks, strongly overridden by `generated/mps3_version.c`. What the `version` verb reports |
| `lscript.ld.in` | canonical Vitis MicroBlaze linker-script template (vectors 0x0..0x4F + one LMB region, length substituted from `LMB_KB`) |
| `src/main.c` | entry point + superloop: timer timebase → `mps3_net_lwip_init()` → `coordinator_init()` → poll loop (each module `_poll()` + lwIP RX drain + manual timers) |
| `src/net_if_lwip.c` | **the** lwIP RAW-API backend of `common/net_if.h` (the "one thin file" that seam anticipated) + smsc911x↔lwIP netif glue (`linkoutput`, RX pbuf input path, netif init) |
| `src/net_if_lwip.h` | target-only API between `main.c` and the backend (init/rx_poll/tmr/mac/sys_now) |
| `gen_greybox_blob.py` | `fpga/dfx/overlay/greybox/{manifest.json,greybox_clear.bin}` → `generated/greybox_blob.c`: the five `mps3_greybox_clearing_*` externs `overlay_store.c` links against + the strong `mps3_shell_static_id()` |
| `generated/` | gitignored; regenerated every `make` run |
| `BUILD_RESULT.txt` | evidence from the last real build (ELF sizes, warnings, exact BSP config, live-fixup log) |

## Build (repro) — two steps, both headless

```sh
# 1. platform + BSP (once per XSA change; ~4 min)
/apps/Xilinx/Vitis/2024.1/bin/xsct -nodisp \
    firmware/platform/create_platform.tcl \
    <shell>.xsa build/vitis_fw

# 2. firmware ELF (~1 min; re-run on any firmware edit).
#    PRODUCT=1 IS THE BUILD THAT GOES ON THE BOARD.
make -C firmware/platform clean          # MANDATORY when any flag changes:
                                         # make cannot see a changed -D
make -C firmware/platform elf PRODUCT=1  # = CLCD=1 CLCD_KVM=1 HWICAP_FIFO=1
                                         #   WINDOWED=1 TOUCH=1
                                         # defaults: LMB_KB=1024 BLOB=real
                                         #   STAGING_BYTES=4096
```

### `PRODUCT=1` — the fielded flag set, as one name

`PRODUCT=1` expands to `CLCD=1 CLCD_KVM=1 HWICAP_FIFO=1 WINDOWED=1 TOUCH=1
DUT_EGRESS=1` — the set the product fabric needs. `DUT_EGRESS` joined it on
2026-09-16 (`54a48b4`): the block had been in the shell since `e2062c1` and the
verb in the firmware since `dc1cc48`, and nothing connected the two, so the
first `PRODUCT=1` bake of the 2026-09 mint declined its own fabric's verb. The
FIELDED image predates that (`docs/FIELDED_SHELL.md`, `static_id`
`0xA8C1C535`) and its `fielded_fw_flags` row is therefore the five-flag set —
that row records what was built, not what `PRODUCT=1` means today. Its **fabric pair** is
`SHELL_TOUCH=1` on the bitstream build: the AXI IIC master the touch driver
talks to only exists in a shell minted with it, so a `PRODUCT=1` image
belongs on a `SHELL_TOUCH=1` bitstream and nowhere else.

Before the knob existed, that set lived only as five literals inside a mint
script — and a plain `make elf` produced an image that **pings, reports the
right `static_id`, passes every acceptance gate, and cannot load a single
RM**, because the default LITE HWICAP writers do not match a FIFO shell.

The Makefile now refuses, or shouts about, the combinations that used to
build quietly and fail far away:

| combination | what used to happen | now |
|---|---|---|
| `TOUCH=1` without `CLCD=1` | undefined reference to `clcd_hittest` at LINK, minutes later, naming neither knob | `$(error)` at parse time, naming the fix |
| `CLCD_KVM=1` without `CLCD=1` | **links and boots**; the handover lives inside `clcd_poll()`, which is never called — the panel silently never hands over | `$(error)` at parse time |
| `HWICAP_FIFO` unset | LITE writers against a FIFO shell: pings, right `static_id`, gates green, loads no RM | `$(warning)` — an error would be wrong, LITE shells legitimately exist |

The gates are skipped when the only goal is `clean`, so `make clean TOUCH=1`
still just cleans.

`-Werror` is on (with one carve-out, `-Wno-error=cpp`: a `#warning` is an
author talking to a reader — `net_if_lwip.c` uses one to report that the BSP
was generated without `LWIP_TCP_KEEPALIVE`, a condition the firmware build
cannot correct).

`LEGACY_SWD=1` puts the retired TCP-6920 `swd_server` back in the link set.
It is out by default: the JTAG cutover made `jtag_server` (6921) the live
debug service and nothing on the target calls `swd_server_*`, yet the file
was still compiled into every shipped image.

- `-nodisp` matters: without it the 2024.1 xsct wrapper insists on
  starting an Xvfb dummy X server and dies on headless hosts.
- The split exists because the classic `app create`/`app build` xsct
  commands talk to an Eclipse "XSDx" backend that needs an X display even
  under xsct ("Unable to init server: Could not connect", confirmed live)
  — while the hsi-side platform/BSP commands are display-free. Step 2 is
  therefore a plain Makefile driving `mb-gcc` against the generated BSP
  (`libxil.a`/`liblwip4.a` + includes), with CPU flags lifted verbatim
  from the BSP build log (`-mcpu=v11.0 -mlittle-endian -mno-xl-soft-div
  -mno-xl-soft-mul -mxl-barrel-shift`).
- **`LMB_KB` defaults to 1024** (1 MiB `local_ram`) — the size that lets
  nanosoc's 155,864 B clearing live in RAM. It **must track the shell you
  are building against**, and it is not cosmetic: the diagnostic mailbox is
  anchored to the END of the LMB, and the LMB address decode **ALIASES**, so
  reading the 1 MiB mailbox address on a 512 KiB shell silently returns the
  512 KiB mailbox's contents rather than failing. The value is compiled in as
  `-DMPS3_LMB_KB` from this one knob and reported on the wire as
  `version.lmb_kb`, so a mismatched image is now visible from the host rather
  than only inferable. (`scripts/mps3_diag.tcl` scans for the mailbox magic
  instead of trusting any constant, for the same reason.) Other knobs (see
  "Memory honesty"): `STAGING_BYTES=4096` (small-clearing RAM fast path —
  gate 3 puts large clearings in QSPI), `PARTIAL_RAM=2048` (dead partial fast
  path), `BLOB=real|placeholder` (**real is the default and fits**).
- ELF lands at `build/vitis_fw/shell_fw/shell_fw.elf` (+ link map
  alongside). The xsct workspace is deleted + rebuilt every step-1 run
  (deterministic, CI-style); everything lives under gitignored `build/`.
- Getting the ELF into the harness bitstream = the standard `updatemem`
  flow (`write_mem_info` .mmi + `-proc microblaze_0`). A shell rebuild
  regenerates both the `.bit` and the `.mmi`, so both must be re-run
  together — a `.bit` whose `updatemem` was never re-run is exactly the
  firmware/bitstream skew the `version` verb's `ver32`-vs-`USR_ACCESS`
  cross-check exists to detect.

## Firmware-only re-bake into an EXISTING bitstream — the exact recipe

A firmware change that touches no BD, no partition boundary and no XDC does
**not** need a shell rebuild: build the ELF and `updatemem` it into the
already-routed `.bit`. That preserves `static_id` and keeps every shipped
overlay partial valid. `XVC_TARGET=swdbb` (the Identify/IICE shift target,
`firmware/xvc_server/README.md`) is exactly such a change.

**Worked example — the SWDBB-target shell for the `0x0EE58A4D` static**, run and
verified 2026-07-31. `$SC` is any scratch directory; nothing here writes into
`fpga/dfx/build*/` or over a shipped artifact.

```sh
IRQ=fpga/dfx/build_qspi_kvm_irq/prod          # the then-shipped 0x0EE58A4D tree (retired scratch;
                                              # the fielded record is fielded/<static_id>/, see docs/FIELDED_SHELL.md)

# ---- 0. reconstruct the overlay identity THIS bitstream was keyed to --------
# See "The identity trap" below: fpga/dfx/overlay/ only ever holds the LAST
# shell built, so it CANNOT be trusted for a re-bake into an older .bit.
python3 fpga/dfx/gen_manifest.py build \
    --rm-name greybox --rm-id 0x00000000 \
    --static-id-file $IRQ/static_id.txt \
    --partial  $IRQ/config_rm_greybox_pblock_rp_dut_partial.bin \
    --clearing $IRQ/config_rm_greybox_pblock_rp_dut_partial_clear.bin \
    --static-bit $IRQ/config_rm_greybox.bit \
    --vivado 2024.1 --out-root $SC/overlay_0EE58A4D --copy

# ---- 1. the ELF. `clean` FIRST -- make keys off timestamps, NOT flags -------
make -C firmware/platform clean WS=$SC/vitis_fw
make -C firmware/platform elf \
     CLCD=1 CLCD_KVM=1 HWICAP_FIFO=1 WINDOWED=1 \
     XVC_TARGET=swdbb \
     WS=$SC/vitis_fw \
     BLOB_MANIFEST=$SC/overlay_0EE58A4D/greybox/manifest.json \
     BLOB_BIN=$SC/overlay_0EE58A4D/greybox/greybox_clear.bin

# ---- 2. bake it in. Reuse the .mmi the shell build already emitted. ---------
/apps/Xilinx/Vitis/2024.1/bin/updatemem -force \
    -meminfo $IRQ/config_rm_greybox.mmi \
    -data    $SC/vitis_fw/shell_fw/shell_fw.elf \
    -proc    u_shell/shell_bd_i/microblaze_0 \
    -bit     $IRQ/config_rm_greybox.bit \
    -out     $SC/config_rm_greybox_fw_swdbb.bit

# ---- 3. GATE IT before anyone loads it -------------------------------------
python3 firmware/platform/verify_shell_image.py \
    --bit      $SC/config_rm_greybox_fw_swdbb.bit \
    --base-bit $IRQ/config_rm_greybox.bit \
    --elf      $SC/vitis_fw/shell_fw/shell_fw.elf \
    --meminfo  $IRQ/config_rm_greybox.mmi \
    --expect-static-id 0x0EE58A4D --expect-userid 0x86A8274E \
    --expect-xvc-target swdbb --expect-hwicap fifo
```

Every flag in step 1 is load-bearing. `CLCD=1 CLCD_KVM=1 HWICAP_FIFO=1
WINDOWED=1` is not a preference — it is **the configuration the shipped
`0x0EE58A4D` firmware was built with** (the `rebuild_irq_subset.sh` mint
variant of 2026-07, since deleted -- see `docs/BUILD_AND_MINT.md`), so
`XVC_TARGET=swdbb` is the *only* difference
between this image and the fielded one. `LMB_KB` is left at its 1024 default
because that is what the bitstream has: the `.mmi`'s
`<AddressSpace Begin="0" End="1048575">` is the authority, and
`verify_shell_image.py` checks it rather than trusting the default.

### Why `-bit` is the *pre-firmware* `config_rm_greybox.bit`

`config_rm_greybox_fw.bit` is already an `updatemem` output. Baking into it
again works, but starting from `config_rm_greybox.bit` reproduces the shipped
recipe exactly — and that reproduction is itself a check. Measured: baking the
*shipped* flag set (no `XVC_TARGET`) into the base produced a file the **same
size as the shipped `config_rm_greybox_fw.bit`, 12,029,378 bytes, differing in
only 2,081 of them** — 8 in the ASCII header's build timestamp, the rest in one
localized `.rodata` region (the version identity: `sha`, dirty flag, build
date). If your control bake does not land that close, something in the recipe
above has drifted.

### The identity trap — why `BLOB_MANIFEST` exists

`fpga/dfx/overlay/` is **one directory that every re-key overwrites in place**,
and the re-keyed manifests are what get committed. The tree therefore only ever
carries the identity of the *last* shell built. `gen_greybox_blob.py` reads that
directory by default, so a firmware-only re-bake into any **earlier** bitstream
silently bakes the **wrong** `static_id` into the strong
`mps3_shell_static_id()` override. The result is a shell that boots, pings,
answers, reports a plausible id — and rejects every overlay pushed to it,
because `config_agent` matches the pushed header's `static_id` against it.

Measured on 2026-07-31: `fpga/dfx/overlay/` on `master` carried `0xCD74B6AE`
(the JTAG re-mint) and on `feat/identify-iice-trace` `0xD84A2E7A` (retired).
**Neither is `0x0EE58A4D`, and `git log -S0x0EE58A4D -- fpga/dfx/overlay/` is
empty — that identity was never committed at all.** It has to be reconstructed
from the build tree's own record, which is step 0 above. `BLOB_MANIFEST` /
`BLOB_BIN` are how you say so; before they existed there was no way to.

This is the same failure *shape* as the LITE-vs-FIFO HWICAP trap (see the
Makefile's `HWICAP_FIFO` comment and commit `d28c292`): a healthy-looking shell
that cannot load an RM, invisible at build time. `verify_shell_image.py` checks
both.

### `verify_shell_image.py` — the static gate

Board-free, Vivado-free, and it **fails** rather than skips:

| Checks | Catches |
|---|---|
| `.bit` size, ASCII header, `0xAA995566` sync word | truncated / headerless / `.bin`-not-`.bit` |
| output `UserID` == base `UserID` | `updatemem` changed the implementation identity, invalidating every partial |
| output **differs** from base | the `updatemem` NO-OP (it exits 0 after printing usage) |
| `.mmi` `InstPath` == the `-proc` argument | a bare `microblaze_0` that does not resolve in the DFX hierarchy |
| `.mps3_diag` NOLOAD vaddr == LMB end − 0x80 | wrong `LMB_KB`; the LMB decode **aliases**, so the mailbox reads plausible garbage |
| runtime top ≤ LMB size; initialized top < mailbox | overflow, mailbox clobber |
| `mps3_greybox_clearing_static_id` read out of the ELF | the identity trap above |
| `imm 17575`/`17576` counts in the disassembly | which XVC target is *in the emitted code* (below) |
| `hwicap_lite_write` vs `hwicap_fifo_*` symbols | the LITE-vs-FIFO trap |
| `clcd_init`/`clcd_poll` present | `--gc-sections` silently stripping a driver whose flag did not take |

All three negative controls were run: the control (non-SWDBB) image fails the
XVC-target check, a wrong `--expect-static-id` fails the identity check, and
handing it the base as its own output fails the no-op check.

### Proving a compile-time flag actually took effect

"`make` printed `-DMPS3_XVC_TARGET_SWDBB`" is not proof — stale objects survive
a flag change because make compares timestamps. Read the *machine code*.
MicroBlaze materialises a 32-bit address with `imm <high16>`, so
`0x44A7_0000` (SWDBB) is `imm 17575` and `0x44A8_0000` (Debug Bridge) is
`imm 17576`. `swd_server.c` also drives SWDBB, so SWDBB sites alone prove
nothing — **the discriminator is that nothing else in the image touches
`0x44A8`**:

```
$ mb-objdump -d shell_fw.elf | grep -cE 'imm +17575|imm +17576'
   XVC_TARGET unset :  0x44A7 SWDBB = 2 sites,  0x44A8 DBGBR = 5 sites
   XVC_TARGET=swdbb :  0x44A7 SWDBB = 5 sites,  0x44A8 DBGBR = 0 sites
```

Corroborating: the SWDBB build's `.text` is **132 B smaller** (173,268 vs
173,400), matching `firmware/xvc_server/README.md`'s note that the retarget is
smaller. And `HWICAP_FIFO=1` is confirmed the same way — `mb-nm` shows
`hwicap_fifo_drain` and **no** `hwicap_lite_write`,
against a deliberately-built LITE control object which shows the exact reverse.

### The HWICAP configuration this must match

`fpga/shell/bd/shell_bd.tcl:903-908` instantiates
`xilinx.com:ip:axi_hwicap:3.0` with **`CONFIG.C_MODE {0}`** (`0` = FIFO, not
lite — it is a write-FIFO *enable* checkbox, not a bus-type selector) and
`CONFIG.C_WRITE_FIFO_DEPTH {1024}`. So the firmware **must** be built
`HWICAP_FIFO=1`. Confirmed present in this image.

## BSP configuration (the exact `bsp config` calls in create_platform.tcl)

Standalone (no RTOS) + **lwip220** (lwIP 2.2.0), RAW API. DEVIATION from
the tasking's "lwip213": 2024.1's classic-flow catalog only *publishes*
lwip220 — `lwip213_v1_1` sits on disk under `data/embeddedsw` but `bsp
setlib lwip213` is refused ("Library not available in the Repository",
confirmed live 2026-07-07). The knob set is name-identical between the
two `.mld`s and nothing here uses a 2.2-only feature. Key fact confirmed
in both versions' `.tcl`: the lwIP DRC **tolerates zero Xilinx EMACs**
(it emits Makefile.config and builds only the lwIP core +
`sys_arch_raw.c`, no `xemacif` adapter) — exactly right for our own
LAN9220 netif driver, no dummy-EMAC hacks needed.

| Setting | Value (default) | Why |
|---|---|---|
| `api_mode` | `RAW_API` | no RTOS; superloop + callbacks (firmware/README.md build model) |
| `mem_size` | 16384 (131072) | lwIP heap (tcp_write COPY staging). The 128 KiB default alone would have consumed the whole LMB of the original as-built shell; it is still 8x this build's need |
| `pbuf_pool_size` | 16 (256) | RX pool 16×1700 ≈ 27 KiB — default is 435 KiB(!). Raised from 8 alongside `tcp_wnd` so a full window can be held in pbufs. Superloop drains every pass; burst overflow drops frames and TCP/ARP recover |
| `memp_n_pbuf` | 16 (16) | RAM-pbuf headers; raised with `tcp_wnd` for the same reason as the pool |
| `memp_n_tcp_pcb` | 12 (32) | 7 single-client services + margin |
| `memp_n_tcp_pcb_listen` | 8 (8) | 7 contract ports + 1; explicit |
| `memp_n_tcp_seg` | 48 (256) | must be ≥ the Xilinx port's hardwired `TCP_SND_QUEUELEN = 16*TCP_SND_BUF/TCP_MSS` = 44 (lwip_sanity_check `#error` hit live with 32); ~28 B/seg |
| `memp_n_udp_pcb` | 4 (4) | TFTP :69 + ephemeral TIDs; explicit |
| `tcp_snd_buf` | 4096 (8192) | per-conn TX buffer |
| `tcp_wnd` | 16384 (2048) | per-conn RX window = the seam's inbound flow control (net_if_lwip.c delays `tcp_recved` until module consumption). **Keep equal to the firmware's `ACK_WINDOW`** — equal windows avoid the mid-window-reopen stall, and the big window is the over-the-wire THROUGHPUT fix (~8x fewer grant RTOs) |
| `tcp_queue_ooseq` | 0 (1) | drop out-of-order segments, low-memory posture |
| `ip_reassembly` / `ip_frag` | 0 / 0 (1/1) | no fragmented traffic in any contract flow; frees the reassembly pbuf budget |
| `no_sys_no_timers` | true (true) | no `sys_timeout`: `mps3_net_lwip_tmr()` calls `tcp_tmr()` every 250 ms + `etharp_tmr()` every 1 s (complete timer set for this feature mix) |
| `lwip_dhcp` | false (false) | static 192.168.10.101/24 (net-protocol.md). D8 = DHCP stays a documented option: `dhcp_start()` hook point is marked in `net_if_lwip.c` |
| `lwip_stats` | false | size (so `diag.pbuf_free` reports `4294967295` = "stats compiled out") |
| `lwip_tcp_keepalive` | true (false) | reap a dead peer's 6910 session. **The `build/vitis_fw` workspace predates this line and still has `LWIP_TCP_KEEPALIVE 0`** — `net_if_lwip.c` says so with a `#warning` on every build. Re-run step 1 to fix; a firmware edit cannot |
| `stdin`/`stdout` | `axi_uartlite_0` | MB console (physical UART, FT4232 ch1) for `xil_printf` diagnostics — NOT the DUT consoles (those relay via uart_over_eth) |

Compiler side: `-Os -ffunction-sections -fdata-sections` +
`-Wl,--gc-sections`, and `MPS3_CFG_AGENT_STAGING_BYTES=8192` (see below).

## net_if_lwip.c design notes (the seam contract, made real)

- **Listeners**: one `tcp_listen` pcb per port, table-idempotent (repeat
  `mps3_net_listen(port)` returns the same handle, per net_if.h). Accepted
  pcbs wait in a small per-listener ring until the module's
  `mps3_net_accept()` adopts them; ring/table exhaustion refuses with
  `tcp_abort` at callback time.
- **RX without copies**: inbound pbuf chains are kept as delivered;
  `mps3_net_recv()` consumes from the chain head and only then calls
  `tcp_recved()` — so the 2 KiB TCP window is the inbound flow control
  and per-connection buffering is bounded by construction (a slow module
  stalls its sender instead of eating RAM).
- **Handle lifetime**: valid until `mps3_net_close()` even after peer
  close/error — drain first, then `MPS3_NET_CLOSED` (FIN) or
  `MPS3_NET_ERR` (lwIP err callback; pcb already freed by the stack).
- **Send** returns `min(len, tcp_sndbuf)` accepted via
  `tcp_write(COPY)+tcp_output`; `ERR_MEM` ⇒ 0 (retry next poll) — the
  short-count backpressure contract every module already honors.
- **UDP**: per-socket ring of 4×516 B datagrams (TFTP's RFC1350 bound),
  copied at callback time; ring-full drops (TFTP retransmits).
  `mps3_net_addr_t.ip` carries the peer ip4 as an opaque u32, echoed back
  verbatim by sendto — exactly the seam's "firmware only echoes it" rule.
- **netif glue**: `linkoutput` flattens the pbuf chain into one static
  frame and calls `smsc911x_tx_frame()` (single-segment by that driver's
  design), with a bounded TX-FIFO-space spin (~122 µs wire-drain) before
  returning `ERR_MEM`; the RX path (`mps3_net_lwip_rx_poll`) pops driver
  frames into `PBUF_POOL` pbufs → `netif->input` (= `ethernet_input`).
  ICMP ping is answered by lwIP itself once the netif is up.
- **MAC policy**: weak `mps3_platform_mac()` = locally-administered
  `02:00:00:4D:50:53` (the single-board bring-up value). Per-octet
  overrides exist for a second board on the same network (`MAC5=0x42`
  etc. — see the Makefile). Real provisioning (QSPI record /
  static_id-derived) is an open A6 decision; a strong override is the hook.

## Memory honesty (1 MiB LMB — MEASURED, `PRODUCT=1`, 2026-09-10)

**Verdict: FITS the 1 MiB LMB at 779,596 B, 268,772 B headroom, with the
REAL 61,704 B greybox clearing baked in.** (mb-gcc `-Os --gc-sections
-Werror`, full module set + CLCD + CLCD KVM + touch + lwIP + newlib.)

```
text 177,948   data 1,840   bss 599,808   dec 779,596      (0xBE54C)
LMB region 1,048,368 B usable (LMB_KB=1024, minus the vector page)
```

Reproduce with `make -C firmware/platform clean && make -C firmware/platform
elf PRODUCT=1`; `BUILD_RESULT.txt` records the same run.

### Where the .bss goes — the two clearing buffers ARE the budget

| Consumer | bytes | what it is |
|---|---|---|
| `config_agent.o` | 264,354 | the incoming-CLEARING receive slot (`CLEARING_RAM_BYTES=262144`) + the partial fast path (`PARTIAL_RAM=2048`) + vars |
| `swap_fsm.o` | 263,432 | the RESIDENT clearing arena (`SWAP_CLEARING_ARENA_BYTES=262144`) + vars |
| lwIP `memp` pools | 38,299 | pbuf/pcb/seg pools (BSP table above) |
| lwIP heap (`mem_size`) | 16,587 | `tcp_write` COPY staging |
| `net_if_lwip.o` | 6,730 | UDP tables 3×3 |
| `clcd.o` / `xvc_server.o` / rest | < 1,400 each | |

Those two 256 KiB buffers are **deliberate, not slack**. They must COEXIST:
the incoming clearing sits in config_agent's slot while
`SWAP_STREAM_CLEARING` replays the outgoing one from swap_fsm's arena, so
the budget is `dec = fixed + 2X`. Sizing them to hold the largest real
clearing is what keeps every swap **QSPI-free** — one physical 8 MiB part
backs BOTH the DFX overlay store and the nanoSoC boot map, and D16
(`ARCHITECTURE_SPEC.md` §15) says keep it idle. `config_agent.c` routes to
QSPI on `payload > cap`, so an exact-size buffer would clear the flash path
by one byte; the slack is margin, on purpose.

`scripts/harness_gates/check_clearing_fits.py` gates this against the real
overlay manifests, so an RM with a bigger clearing cannot slip through — it
is what caught nanosoc's growth (117,684 → 155,864 B) in the first place.
When it fires, **raise the buffer, do not waive** (see
`clearing_fit_waivers.txt`).

### Greybox blob: REAL, baked (default)

`BLOB=real` bakes the true greybox clearing in `.rodata` — 61,704 B at
`static_id` `0xA8C1C535`, manifest-exact crc/len/ids — so the blank-QSPI
first-boot self-seed is real. `BLOB=placeholder` (64 ICAP NOPs) remains only
for size experiments. The blob and the version identity are both regenerated
on **every** build: provenance that is not re-derived at build time is
provenance that goes stale.

### Other levers

- **newlib printf**: `-Dsnprintf=sniprintf` saves ~48 KiB text (no firmware
  format string uses floats — %s/%d/%u/%x/%c only).
- **lwIP**: shrunk hard vs defaults (see the BSP table above).
- `LMB_KB=128` still reproduces the original as-built-shell overflow for the
  record.

## Poll model (no interrupts in v1)

The BD wires INTC In0..3 = HWICAP/Timer/UARTLite/eth_irq, but this build
uses none of them: every module is a bounded non-blocking `_poll()`, the
LAN9220 driver is poll-mode by design, and the AXI timer is used only as
a free-running counter for `sys_now` (raw PG079 register pokes — no
XTmrCtr driver, no `Xil_Exception` setup; the standalone vector table
still drags `xintc.o` (~3.6 KiB) out of libxil.a, a known trim
candidate). First candidate if polling
ever measurably drops frames: eth_irq — and remember the LAN9220
`IRQ_CFG` IRQ_POL=1 fix (RESULT.txt flag) belongs to that change, not to
this build (INT_EN=0 keeps ETH_INT quiet today).

## The retired harness_app spike

`firmware/harness_app/` was the earlier same-wave spike, written BEFORE the
`net_if` seam and the smsc911x driver landed: it carried its own `lan9220.c`
and raw-lwIP TCP-6900 plumbing, bypassing the seam entirely. This directory
superseded it, and **the spike has now been deleted** — a second, divergent
copy of the same superloop and network plumbing is a liability once the real
one is proven on hardware. Its `updatemem`/`.mmi` procedure survives in the
"Build (repro)" notes above rather than by reference to a directory that no
longer exists.
