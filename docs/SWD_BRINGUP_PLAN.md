# SWD debug-channel bring-up — plan of attack

> **Superseded transport (read first).** This plan targets the **SWD**
> channel on **TCP 6920**. The platform has since migrated to **JTAG**: the
> fielded shell serves OpenOCD `remote_bitbang` on **TCP 6921** via
> `firmware/jtag_server/` + `fpga/shell/ip/jtag_bb/`, and that is the
> silicon-proven path that halts the DUT's Cortex-M0
> (`host/openocd/nanosoc_mps3_jtag.cfg`). **6920/`swd_bb` is dormant on the
> fielded shell.** The bring-up *method* below — prove the innermost hardware
> contract in simulation, then the firmware, then the full stack on silicon —
> is what was actually followed, and is why this document is kept.

**Scope.** Bring up the Arm SWD debug channel end-to-end: from OpenOCD on a
host, over `remote_bitbang`/TCP 6920, through the resident MicroBlaze shell
firmware, into the `swd_bb` pin-wiggler, across the RP partition boundary, and
into the CoreSight SW-DP of the nanoSoC (Cortex-M0) DUT loaded in the
reconfigurable partition. This is our SoC on our harness — a debug-access
bring-up, modelled on how the UART channel was actually brought up:
**prove the innermost hardware contract in simulation first, then the firmware
path, then the full stack on silicon — never trust a green build over an
observed signal.**

**Status in one line:** the pieces are ~90% present and the byte/register
contracts are already source-confirmed. There is no OpenOCD binary on the dev
box and no live smoke has ever run. Three concrete things gate first light:
a reset-domain ordering hazard that leaves the DAP *in reset even after a clean
swap*, a missing width-32 regression bench for `swd_bb`, and a missing target
`.cfg`. None is a redesign.

> **Convention used throughout:** **[V]** = verified by reading the source in
> this repo (file:line cited). **[I]** = inferred/expected, must be confirmed at
> bring-up. Do not promote an **[I]** to fact without an observed signal.

---

## 0. The signal chain and where each layer stands

| # | Layer | Artifact | State | Evidence |
|---|---|---|---|---|
| 6 | OpenOCD host | `host/openocd/swd_remote_bitbang.cfg`, `host/pyverify/pyverify/debug.py` | cfg written, **never config-parsed** (no openocd on box) | [V] cfg present; README §"Validation status" says no openocd binary here |
| 5 | Transport | TCP 6920, `remote_bitbang` byte protocol | byte↔bit map **source-confirmed vs OpenOCD** | [V] `net_proto.h:311-352`, `test_swd_server.c` conformance test |
| 4 | Firmware SWD engine | `firmware/swd_server/swd_server.c` (polled in `coordinator.c:123`) | real bodies, host-gcc tested | [V] full read; 9 host tests |
| 3 | CSR pin-wiggler | `fpga/shell/ip/swd_bb/swd_bb.sv` (`0x44A7_0000`) | real RTL, decode-fixed, cocotb-benched **at width 12 only** | [V] `swd_bb.sv`, `tests/swd_bb/test_swd_bb.py` |
| 2 | RP boundary | `swd_clk / swd_dio_o / swd_dio_oe / swd_dio_i` partition pins | wired in `shell_top.sv` + BD ports | [V] `shell_top.sv:233-235,309-311`, `shell_bd.tcl:137-140` |
| 1 | DUT wrapper | `fpga/rp/nanosoc/rp_nanosoc_wrapper.sv` | **SWD really wired to the DAP** (not tied off) | [V] wrapper `:190,314-317` |
| 0 | DUT DAP | nanoSoC Cortex-M0 SW-DP | reachable **only when all 3 resets released** | [V] wrapper reset AND, §2 below |

**The load-bearing confirmation the brief demanded — the wrapper does NOT tie
off SWD.** `rp_nanosoc_wrapper.sv` instantiates the real `nanosoc` and connects
its SW-DP directly (`:314-317`):

```
.cpu_0_swdi   (swd_dio_o),     // host drive value -> DUT SWDIO in
.cpu_0_swclk  (swd_clk),       // SWCLK
.cpu_0_swdo   (cpu_0_swdo_w),  // DUT drive value out
.cpu_0_swdoen (cpu_0_swdoen_w) // DUT output-enable
```

and models the shared open-drain SWDIO wire (`:190`):

```
assign swd_dio_i = cpu_0_swdoen_w ? cpu_0_swdo_w : swd_dio_o;
```

i.e. when the DUT drives (`swdoen=1`) the shell samples the DUT's bit; otherwise
the shell reads its own driven value looped back — electrically correct for a
one-wire SWDIO. **This is the single fact the whole plan rests on, and it is
true.** (Contrast `rm_uart_echo.sv:235`, which ties `swd_dio_i = 1'b0` — that RM
has no DP, as expected.)

---

## 1. What is already proven vs what is not

**Proven by reading source (not a board) — [V]:**

- **`swd_bb` register contract.** DRIVE `@0x00` `{[0]swclk,[1]swdio_o,[2]swdio_oe}`
  write-through to pins; SAMPLE `@0x04` RO = 2-FF-synced `swd_dio_i`; reset
  `3'b000` (SWCLK low, SWDIO released). Decode fixed to the full local page
  (`LOCAL_ADDR_W` guard, `swd_bb.sv:217`) so `≥0x08` is inert. `tests/swd_bb/`
  covers reset state, all 8 DRIVE patterns, 2-FF SAMPLE latency, RO-write,
  unmapped-offset inertness, WSTRB lane-0 gating.
- **`remote_bitbang` byte↔bit mapping.** `d/e/f/g = 'd'+(swclk<<1|swdio)`;
  `r/s/t/u = 'r'+(trst<<1|srst)`; `O/o` drive/release; `c` sample→ASCII `'0'/'1'`.
  Re-derived from OpenOCD `remote_bitbang.c` in
  `test_swd_server.c:test_openocd_remote_bitbang_conformance()` — not hardcoded,
  so an inverted map fails in CI, not at silicon.
- **srst semantics.** `s`/srst maps to **`CLKRST.RESET_CTRL.dbg_resetn`**
  (`swd_server.c:108-116`), *not* to a `swd_bb` bit. `1=released`, so asserting
  srst *clears* the bit. Pinned by `test_srst_maps_to_clkrst_dbg_resetn`.
- **Swap gating.** `swd_server_poll()` early-returns while `g_shell_state.swd_gated`
  (`swd_server.c:159`); the swap FSM sets it `true` at gate
  (`swap_fsm.c:693,1016`) and clears it **only at `SWAP_DONE`, after RM_ID
  verifies** (`swap_fsm.c:1110`). So bytes queue during a swap and SWD is only
  serviced against a *verified* RM.
- **Wrapper SWD wiring** (§0 above).

**NOT proven — must be observed at bring-up — [I]:**

- Any byte has ever crossed 6920 to a live shell. `host/openocd/README.md`
  §"Validation status" is explicit: no `openocd` on this box, the `.cfg` has
  never been config-parsed, no `init` has run.
- The DUT DAP answers. No DPIDR has ever been read over this stack.
- The reset ordering actually leaves the DAP out of reset after a swap (§2 — I
  believe it does **not**, today, without one extra write).
- Turnaround timing survives the real TCP-paced clock (expected fine; §5).

---

## 2. The reset truth — read this before touching anything

This is the part most likely to burn a naïve bring-up, and it is subtle because
it is *correct-looking at every layer but the top*.

### 2a. Three resets collapse to one inside the DUT

`rp_nanosoc_wrapper.sv:173`:

```
assign dut_sys_sysresetn = dut_resetn & rp_resetn & dbg_resetn;
```

nanoSoC has exactly one reset port (`sys_sysresetn`), so the contract's three
resets are **AND-ed** — *any* one asserted holds the **whole core, including its
SW-DP,** in reset. **[V]** (wrapper header `:71-82` documents this as a known A6
gap).

**Consequence #1 — the DAP is inside the core reset domain.** On a normal
CoreSight part the debug power/reset domain (`DBGRESETn`) is separate from the
core (`SYSRESETn`), so you can attach to the DP while the core is held in reset
("connect under reset"). **Here you cannot.** The DP only answers when
`dut_resetn & rp_resetn & dbg_resetn == 1`.

**Consequence #2 — `srst` nukes the DP.** OpenOCD `srst` → `dbg_resetn` low →
`sys_sysresetn` low → the SW-DP itself resets. So the usual assumption "srst
holds the core but the DP stays alive" is **false** on this DUT. `reset halt`
via vector-catch (which needs the DP alive across the reset to arm `VC_CORERESET`
and observe the halt) **will not work as written.** Halt via DHCSR *after* a
clean connect instead, and treat `srst` as a full-DUT reset that requires a
fresh line-reset + DP re-connect afterwards. **[V] mechanism / [I] observed
OpenOCD behaviour.**

### 2b. `dbg_resetn` defaults *held* and nothing routine releases it

`dut_clkrst.sv` resets `reset_ctrl_q` to `3'b000` — **all three held**
(`:268`, deliberately: "the RP must never come out of reset just because the
shell's AXI fabric came up"). The swap FSM releases:

- `dut_resetn` (bit 0) at verify/promote (`swap_fsm.c:975-986`, the recent
  `82244ac` fix — "Released HERE, not at RELEASE: an unverified RM must never be
  clocked"),
- `rp_resetn` (bit 1) at `step_release` (`swap_fsm.c:1120-1123`).

**It never writes bit 2 (`dbg_resetn`).** Grep confirms the *only* non-test
writer of `CLKRST_RESET_CTRL_DBG_RESETN` is `swd_server.c`'s srst handler
(`:113/:115`). **[V]**

**So after a clean swap `RESET_CTRL = 0x3` (dut+rp released, dbg still held) ⇒
`sys_sysresetn = 1 & 1 & 0 = 0` ⇒ nanoSoC is held in reset ⇒ the DAP is dark.**
The DP does not come alive until *something* sets bit 2 — and the only thing that
does is OpenOCD sending an srst-**de**assert (`r`, srst=0). Whether OpenOCD emits
that before it examines the DP depends on its reset config and version; do not
rely on it. **This is gap G1 and it is the difference between "DPIDR reads" and
"connect fails after a green swap."**

**Where the DAP becomes reachable (the answer the brief asked for):** at
`SWAP_DONE` (`swd_gated=false`, DECOUPLE released, `rp_resetn` released,
`dut_resetn` released) **AND `dbg_resetn` (RESET_CTRL[2]) released.** The last
conjunct is the one nothing guarantees today.

### 2c. The decoupler clamps the boundary during a swap

While `DFXCTL.DECOUPLE` is asserted the RP boundary is clamped (the swap FSM
asserts it at gate, `swap_fsm.c:707/997`, releases at `step_release`), and in
lockstep `swd_gated` blocks firmware byte service. So **SWD is usable only after
the swap commits** — never mid-swap. Bytes sent mid-swap queue in the transport
and are drained after ungate (proven by `test_gated_defers_bytes_until_ungated`).
**[V]**

---

## 3. The bring-up ladder (innermost first)

Each rung: **what it proves · how · pass criterion · failure signature.** Do not
climb a rung until the one below is green. Rungs 0–2 need no board; 3–6 are on
silicon and obey the Tier-3 rules (`mrd`/`mwr` only, never `stop`/`con`).

### Rung 0 — `swd_bb` CSR liveness + tri-state toggle (cheapest possible)

- **Proves:** the register block is alive at the *shipped* address/width and its
  three driven pins follow DRIVE — the SWD analogue of the
  `DFXCTL.DECOUPLE`/`UARTBR.SWO_CFG` liveness poke that would have caught the
  whole-platform decode outage in 5 seconds (`HARNESS_REGRESSION.md` Tier-3 #1).
- **How (sim, today):** `tests/swd_bb/` already does the pin-follow check — but
  at width 12. Add the width-32 base+offset variant (gap G2). **How (silicon):**
  one xsdb `mwr 0x44A70000 0x7; mrd 0x44A70000` (expect `0x7`), then
  `mwr 0x44A70000 0x0` — save/restore so you don't leave SWDIO driven.
- **Pass:** DRIVE reads back what was written; a write to `0x44A70000+0x08`
  leaves DRIVE untouched.
- **Fail signature:** readback `0x0` at the real base while `0x0` at offset-0
  reads fine ⇒ the width-32 decode is dead (bug #1's exact shape). **STOP — do
  not climb.** This is the 5-second catch.

### Rung 1 — `swd_bb` pin/CDC contract in sim

- **Proves:** DRIVE→pins combinational; SAMPLE follows `swd_dio_i` through
  exactly 2 FFs; RO/unmapped/WSTRB semantics.
- **How:** `make -C tests/swd_bb SIM=vcs` (in `list_benches.py` as `swd_bb`).
- **Pass:** all 6 tests green. **Fail:** scrambled bit order or 1-FF sync latency
  → SWCLK/SWDIO mislabelled, or turnaround sampled too early.

### Rung 2 — firmware byte-drain + OpenOCD conformance (host-gcc)

- **Proves:** every `remote_bitbang` char produces the right DRIVE composition /
  SAMPLE reply / srst→dbg_resetn effect; gating defers; fail-closed on junk.
- **How:** `make -C firmware/test test` (runs `test_swd_server.c`).
- **Pass:** "test_swd_server: N checks passed". **Fail:** a byte maps to the
  wrong pin (would silently corrupt SWD framing at silicon).

### Rung 3 — transport reachability (silicon, no DAP needed)

- **Proves:** the shell accepts a TCP connection on 6920 and drains bytes.
- **How:** with a live shell (do **not** reprogram — a swap is resident),
  `nc -v 192.168.10.101 6920` and send a harmless `O`/`o` (drive then release
  SWDIO); or `pyverify.debug.launch_openocd(...command("init"))`.
- **Pass:** connection *accepted* (not refused). Even if the DP never answers,
  an accepted session proves the 6920 transport + `swd_server` poll are live —
  exactly what `swd_remote_bitbang.cfg`'s header calls out.
- **Fail:** `Connection refused` ⇒ `swd_server` not listening / coordinator not
  polling it / wrong IP. This is a firmware/network problem, not a DAP problem —
  do not chase the DAP yet.

### Rung 4 — release the DAP from reset (the G1 gate)

- **Proves:** all three resets are deasserted so `sys_sysresetn=1`.
- **How:** confirm/force `RESET_CTRL = 0x7`. Read `mrd 0x44A00000` — expect
  `0x7`; if it reads `0x3`, bit 2 (`dbg_resetn`) is held (§2b). Release it via
  the SWD path (OpenOCD `r`/srst-deassert) **or** an explicit
  `mwr 0x44A00000 0x7` **or** the firmware fix in G1.
- **Pass:** `RESET_CTRL == 0x7` **and** `CLKRST.STATUS.dut_clk_alive == 1`
  (`0x44A0000C` bit 0) — the core is clocked and out of reset.
- **Fail:** stuck at `0x3` ⇒ G1 unfixed. `dut_clk_alive==0` ⇒ DUT clock (DRP
  MMCM) not running — a clock problem upstream of SWD, stop and fix that first.

### Rung 5 — first light: read DPIDR (the milestone; see §6)

- **Proves:** the entire chain, host→DP, is electrically and logically alive.
- **How:** `openocd -c "set SHELL_HOST 192.168.10.101" -f
  host/openocd/swd_remote_bitbang.cfg -c init -c shutdown`.
- **Pass:** `Info : SWD DPIDR 0x0bb11477`. **Fail:** see §6 failure table.

### Rung 6 — halt / read memory / (finally) load firmware

- **Proves:** the DAP is a *usable* debug channel, not just an ID responder.
- **How:** after DPIDR, `dap info` (ROM table), read CPUID `0xE000ED00`, then
  `reset` behaviour + `halt` + `mdw`. Then the payoff: `load_image`/`flash` a DUT
  image over SWD.
- **Pass:** ROM table enumerates SCS `@0xE000E000`; CPUID matches the core; a
  known IMEM word reads back.
- **Fail / landmine:** after SWD-loading a new image the core wedges (spins in
  `Default_Handler`) because **SysTick/NVIC state from the old image is still
  armed and ARMv6-M cannot clear an ACTIVE exception** (lab memory:
  `swd-load-systick-wedge`). **Always follow an SWD image-load with a DUT
  reset** (assert then deassert `dut_resetn`, i.e. `mwr 0x44A00000 0x6` then
  `0x7`, or OpenOCD reset) to clear SysTick/NVIC before running; if already
  wedged, POR the core (assert `dut_resetn`) — a soft recover will not save it.

---

## 4. Turnaround / tri-state — why this usually breaks, why it likely won't here

The classic SWD bring-up failure is bidirectional SWDIO turnaround: host drives
`swdio_o` with `swdio_oe=1` on the way out; releases (`oe=0`) for the ACK/read
phase; DUT drives; host samples `swdio_i`. Getting the turnaround cycle count or
the sample edge wrong corrupts framing.

On this platform that risk is **structurally reduced**, for three reasons, all
**[V]**:

1. **Software times everything.** `swd_bb` has *zero* sequencing logic
   (`swd_bb.sv` header, README "the firmware IS the SWD engine"). Every SWCLK
   edge is a separate AXI write from `swd_server`; every SWCLK half-period is
   ≥ one full AXI transaction (and, over TCP, far longer). So `swdio_o`/`oe` are
   stable for eons around each edge the DUT samples — no setup/hold race.
2. **The turnaround model is correct.** The wrapper's `swd_dio_i` mux (`:190`)
   *is* the shared-wire turnaround: host reads its own value back until the DUT
   asserts `swdoen`, then reads the DUT's. There is no third state to get wrong.
3. **The DUT ignores `swd_dio_oe`.** `cpu_0`'s 2-pin SW-DP times its own
   turnaround; `swd_dio_oe` is consumed only shell-side (wrapper `:176-180`).
   So a host/DUT `oe` disagreement cannot cause bus contention *inside the RP*.

**Residual risk [I]:** the 2-FF SAMPLE synchronizer adds up to 2 `s_axi_aclk`
cycles of latency on the read-back. At `remote_bitbang` pace this is invisible
(README's A3 note), but *confirm* by reading DPIDR (a parity-checked 32-bit read)
— a turnaround/sample bug shows up as a **parity error or a shifted-by-N IDCODE**,
not silence. That is the real turnaround test; there is no cheaper one.

---

## 5. OpenOCD configuration and host expectations

A config **already exists**: `host/openocd/swd_remote_bitbang.cfg`, mirrored by
`pyverify.debug.OpenOcdRemoteBitbangConfig` (`host/pyverify/pyverify/debug.py`).
The essential shape:

```tcl
adapter driver remote_bitbang
remote_bitbang host 192.168.10.101      ;# the fpgahub/shell static IP
remote_bitbang port 6920
transport select swd
adapter speed 1000                       ;# ADVISORY ONLY — see below
reset_config srst_only                   ;# but read the §2 caveat
swd newdap nanosoc cpu -enable
dap create nanosoc.dap -chain-position nanosoc.cpu
```

Run: `openocd -c "set SHELL_HOST 192.168.10.101" -f
host/openocd/swd_remote_bitbang.cfg -c init -c shutdown`.

**Adapter-speed reality — set expectations low.** `remote_bitbang` has **no
clock-rate control**: the TCP byte stream *is* the clock, and `adapter speed
1000` is advisory only (`.cfg:50-53` says exactly this). Every SWCLK edge is
≥1 TCP round-trip of ≥1 AXI-Lite write. **This will be very slow** — think tens
to low-hundreds of bit-toggles/sec, i.e. seconds for a DPIDR read, minutes to
load a few KB of IMEM. **`swd_server` does not batch** — it services up to
`SWD_SERVER_BYTES_PER_POLL = 256` bytes per coordinator poll
(`swd_server.c:31,164`) but each SAMPLE still owes a synchronous 1-byte reply
before the next command (`:180-192`), so reads are strictly request/response and
cannot pipeline. Plan on SWD as a *correctness* channel, not a throughput one;
bulk DUT-image delivery belongs on the QSPI/overlay path, with SWD used to
verify and to halt/step. (A hardware CMSIS-DAP engine — "v2" in the `swd_bb`
README — is the eventual throughput fix; out of scope here.)

**Two OpenOCD gotchas to pre-empt, both from §2:**

- **`reset_config srst_only` is a trap on this DUT.** srst resets the whole
  nanoSoC including the DP (§2a). If `init`/examine asserts srst expecting the DP
  to survive, connect will flap. Consider `reset_config none` for the pure DPIDR
  smoke, and drive `dbg_resetn` explicitly, so nothing yanks the DP mid-examine.
- **`reset halt` won't vector-catch** (§2a). For halting an M0 here, connect
  first, then set DHCSR — do not depend on reset-halt.

The full per-DUT target config `target/nanosoc_mps3.cfg` that
`OpenOcdRemoteBitbangConfig.target_cfg` names **does not exist** (confirmed:
`find` returns nothing). The bare `swd newdap`/`dap create` in the `.cfg` is
enough for DPIDR; a real `cortex_m`/memory-map target is gap G3.

---

## 6. First milestone — "the UART banner appeared" equivalent

**Read the nanoSoC DAP's DPIDR over the full stack.** The smallest end-to-end
proof; needs **no DUT firmware** (the DP answers regardless of what the core
runs), only the core out of reset (Rung 4).

**Success value:** `Info : SWD DPIDR 0x0bb11477` in the OpenOCD log.
`0x0BB11477` = Arm Cortex-M0 SW-DP. **[I]** — the `.cfg` itself says "confirm the
real value at bring-up." **The DPIDR is also a discriminator worth reading
deliberately:** `0x0BB11477` (Cortex-M0) vs `0x0BC11477` (Cortex-M0+). The lab
multicore work migrated cores to M0+, but this v0 RP DUT is the single-core
`nanosoc` the wrapper header calls Cortex-M0; if the DUT is actually an M0+ the
DPIDR (and CPUID below) will say so — that is a *feature* of this milestone, not
a failure.

**Then, second proof (ROM table / identity):** `dap info` should enumerate the
M0 ROM table (SCS at `0xE000E000`); read CPUID at `0xE000ED00` — expect
`0x410CC200` (Cortex-M0) / `0x410CC601` (Cortex-M0+). A correct CPUID over an AP
memory read proves not just the DP but the AP and the AHB-AP→core path.

**Failure table:**

| Symptom | Most likely cause | Rung to revisit |
|---|---|---|
| `Connection refused` on 6920 | `swd_server` not listening / wrong IP | 3 |
| Connected, DPIDR read times out / all-1s / all-0s | DAP in reset (`RESET_CTRL=0x3`, G1) or DUT clock dead | 4 |
| DPIDR wrong-by-parity or shifted | turnaround/SAMPLE bug (§4) | 1, 5 |
| DPIDR OK, `dap info` fails | AP/AHB-AP path or ROM-table addr | 6 |
| Works once, dies after `reset` | srst nuked the DP (§2a) | reconnect; §5 |

---

## 7. Gaps to close, prioritised

| ID | Gap | Size | Why it matters |
|---|---|---|---|
| **G1** | **Nothing releases `dbg_resetn` (RESET_CTRL[2]).** After a clean swap `RESET_CTRL=0x3` ⇒ DAP held in reset (§2b). | **Firmware, tiny** (one write / a few lines) | Without it, DPIDR fails *after a green swap* and the failure looks like a dead DAP. Fix: release all three at swap-commit (write `0x7`), **or** ensure the OpenOCD cfg deasserts srst before examine, **or** a coordinator init that sets RESET_CTRL[2]. Also decide the intended semantics (should a debug-only reset really reset the whole core? — it does, §2a). |
| **G2** | **`swd_bb` has no width-32 regression bench.** Ships at `C_S_AXI_ADDR_WIDTH=32` (`shell_bd.tcl:459`) but `tests/swd_bb/` elaborates at the default 12; explicitly waived in `param_parity_waivers.txt`. | **Bench, small** (~1 file, mirror `test_csr_decode_width.py`) | `swd_bb` has the *same decode shape* that took the whole platform down. A regression that drops the `LOCAL_ADDR_W` guard would make **every SWD pin write silently no-op on silicon** and the width-12 bench would stay green. Add a `-pvalue+swd_bb.C_S_AXI_ADDR_WIDTH=32` base+offset read/write, then delete the waiver line. |
| **G3** | **No `target/nanosoc_mps3.cfg`.** `pyverify.debug` and the README name it; it is absent. | **Config, small** | Bare DAP is enough for DPIDR, but halt/step/memory/flash (Rung 6, the actual payoff) need a `cortex_m` target + memory map. Author it against the M0 confirmed at Rung 5. |
| G4 | **Never config-parsed / never smoke-tested.** No `openocd` on the dev box. | **Ops** | First host with openocd ≥0.12 must run the DPIDR smoke. Expect failure at *connect* if no shell is up — never at config parse. |
| G5 | **`bind_swd_bb.sv` referenced but absent.** `tests/swd_bb/Makefile:34` sets `SVA_BIND_FILES := bind_swd_bb.sv`; the file does not exist. | **Bench, tiny** | AXI-protocol SVA the `test_random_aw_en_corners` docstring claims is bound isn't there. Either add it or drop the reference; today the "bound SVA validates every handshake" claim is unbacked. |
| G6 | **`dbg_reset_req_i` pin tied to 0 in the BD** (`dut_clkrst.sv:396`). | note only | The hardware srst pin path is dormant; srst works *only* via the firmware `RESET_CTRL[2]` write. Fine today, but if a future shell drives `dbg_reset_req_i` from an async domain it needs a 2-FF ASYNC_REG synchronizer ahead of the register. |

**The honest verdict the brief asked for:** this is **not** more broken than the
scaffold suggests — it is ~90% there. The byte protocol, the register block, the
firmware engine, and the wrapper wiring are all real and source-verified. **The
one thing that will actually stop first light is G1** (a few-line reset-release
ordering fix), with G2/G3 close behind as the "don't ship it un-guarded / can't
do anything past DPIDR without it" pair.

---

## 8. Regression gate — where SWD slots into `HARNESS_REGRESSION.md`

The suite's rule is: every gate either **compares artifact to source** or
**asserts on silicon** — never adds another model-shaped test. SWD gates,
by tier:

- **Tier 0 (`check_bench_param_parity.py`):** closing **G2** auto-removes the
  `swd_bb C_S_AXI_ADDR_WIDTH` waiver line — the parity gate then *enforces* that
  a width-32 `swd_bb` bench exists. This is the cheapest SWD regression guard and
  it already has a home; it is currently *waived*, not *missing*.
- **Tier 1 (`make -C firmware/test test`):** `test_swd_server.c` already runs
  here — keep it in the list. Add G2's width-32 `swd_bb` cocotb bench alongside
  `tests/csr_decode_width` (same `SIM=vcs`, SKIP-clean without a simulator).
- **Tier 3 (on-board, new gate `tier3_swd_liveness`):** slot a **DPIDR read**
  right after the existing `tier3_csr_liveness.tcl` (#1) and the swap/console
  checks. Shape it exactly like Rung 0+5: (a) `mrd`/`mwr` `swd_bb` DRIVE liveness
  over JTAG (SWD's twin of the `DFXCTL.DECOUPLE` poke); (b) confirm
  `RESET_CTRL==0x7` (the G1 guard — a swap that leaves it `0x3` fails here
  loudly); (c) OpenOCD `init` reads DPIDR. Order it **after** the swap/console
  gates and, like the QSPI #8 hazard probe, make it non-blocking until G1/G3
  land. It catches: a decode regression (a), the reset-ordering regression (b),
  and any break in the host→DP chain (c) — the on-silicon twin of Rung 5.

Traceability line to add to the doc's table: `SWD chain → tier3_swd_liveness +
swd_bb width-32 bench + param-parity(swd_bb) → 3/1/0`.

---

## 9. What SWD unlocks — why this makes nanoSoC a real DUT

Today the RP DUT is verifiable only by what it *emits* (rm_id readback, a UART
banner, GPIO/LED patterns). SWD turns it into an *inspectable* processor:

- **Load DUT firmware without a rebuild.** Right now a new DUT image means a new
  IMEM preload (`rp_nanosoc_wrapper` `IMEM_MEM_FPGA_IMG`, default `"image.hex"`)
  baked into the RM bitstream — a full DFX build per firmware change, and the
  `rm_nanosoc` dir ships *no* hex, so the RP boots whatever `image.hex` resolves
  to at build time. SWD lets you `load_image` a fresh binary into IMEM/DMEM in
  seconds (slowly — §5) and run it, decoupling firmware iteration from fabric
  builds. **Mind the SysTick-armed-wedge landmine on every reload (Rung 6).**
- **Halt / step / breakpoint the M0.** DHCSR halt, single-step, the M0's 4 HW
  breakpoints (BPU) and 2 watchpoints (DWT) — real interactive debug of DUT
  firmware, which no other channel on this platform offers.
- **Read/write core memory + registers live.** Peek IMEM/DMEM, peripherals,
  core registers over the AHB-AP while halted or running — the ground truth when
  a UART banner *doesn't* appear.
- **Post-mortem a lockup.** `dut_lockup` is tied off in the wrapper (`:279`, an
  A6 gap), so SWD reading the M0's fault status/PC is currently the *only* way to
  see why a DUT wedged — directly relevant to the class of "alive but dead,
  spinning in a handler" failures the lab already hit over SWD-load.

That is the step from "a blinking LED behind a decoupler" to "a processor you can
actually bring up, load, halt, and interrogate over the wire."

---

## Appendix — quick reference (all [V])

**SWDBB `@0x44A7_0000`** (`platform_regs.h:96,366-372`): `DRIVE 0x00` RW
`{[0]swclk,[1]swdio_o,[2]swdio_oe}`; `SAMPLE 0x04` RO `[0]swdio_i`.

**CLKRST `@0x44A0_0000`** (`platform_regs.h:89,104-113`): `RESET_CTRL 0x00`
`{[0]dut_resetn,[1]rp_resetn,[2]dbg_resetn}`, `1=released`; `STATUS 0x0C` RO
`{[1]mmcm_locked,[0]dut_clk_alive}`. DAP reachable ⇔ `RESET_CTRL & 0x7 == 0x7`.

**Byte protocol (TCP 6920)** (`net_proto.h:338-352`): `O`/`o` SWDIO drive/release;
`c` sample→ASCII `'0'/'1'`; `d/e/f/g` = `'d'+(swclk<<1|swdio)`; `r/s/t/u` =
`'r'+(trst<<1|srst)` (only srst meaningful → `dbg_resetn`); `B/b` LED (ignored);
`Q` quit.

**Key files:**
`fpga/shell/ip/swd_bb/swd_bb.sv` ·
`tests/swd_bb/test_swd_bb.py` ·
`firmware/swd_server/swd_server.c` ·
`firmware/test/test_swd_server.c` ·
`fpga/rp/nanosoc/rp_nanosoc_wrapper.sv` (SWD wired `:190,314-317`; reset AND `:173`) ·
`fpga/shell/ip/clkrst/dut_clkrst.sv` (reset gen `:406-448`; defaults held `:268`) ·
`firmware/coordinator/swap_fsm.c` (gate `:693,1016`; dut_resetn release `:975-986`; ungate `:1110`) ·
`host/openocd/swd_remote_bitbang.cfg` · `host/pyverify/pyverify/debug.py` ·
`docs/HARNESS_REGRESSION.md` · `param_parity_waivers.txt` (swd_bb waiver).
