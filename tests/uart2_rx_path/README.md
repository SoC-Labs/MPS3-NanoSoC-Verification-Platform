# tests/uart2_rx_path — M1 console-RX gate

**Question:** can a host byte reach the DUT's console receiver?

This was the one hardware unknown standing between the platform and a MicroPython
REPL (roadmap milestone M1). The console's DUT→host half (TX) is proven and in
daily use. The host→DUT half (RX) had never been demonstrated into the core — in
*either* the monolithic or the DFX flow — and nobody had localised why.

**Answer: it could not. The RTL was broken. This bench found the bug, and gates
the fix.**

## The bug

`nanosoc_ss_systemctrl` instantiated the pin mux with its pad-input ports left
unconnected:

```verilog
nanosoc_pin_mux u_pin_mux (
    .uart2_rxd  (uart2_rxd),                              // feeds CMSDK UART2's RXD
    ...
    .p1_in      ( ), // was(p1_in) now from pad inputs),  // <-- UNCONNECTED
```

while `nanosoc_pin_mux` declared `p1_in` as an **output** that it generated itself
by self-feedback (an internal pad model), and derived the UART receive line from
it:

```verilog
assign p1_in[4]  = p1_out_en_mux[4] ? p1_out_mux[4] : 1'b1;   // :156
assign uart2_rxd = p1_in[4];                                  // :95
```

Net effect:

```
uart2_rxd == (P1_OUTEN[4] ? P1_OUT[4] : 1'b1)
```

UART2's receive line was a **loopback of the SoC's own GPIO drive**, or a constant
idle-high `1`. The real pad value was discarded. A host byte could not reach the
console receiver by any path, on FPGA or ASIC.

Someone refactored GPIO to take real pad inputs (routing them straight to the GPIO
blocks) and disconnected the pin mux's synthetic feedback — without noticing that
the UART receive lines are derived from it. The GPIO side kept working, which is
why this stayed hidden: LEDs and switches were fine, and only the console's RX
half was silently dead.

## The fix

Two files in the `nanosoc_arch_tech` submodule:

- `rtl/src/control/verilog/nanosoc_pin_mux.v` — `p0_in`/`p1_in` become true
  **inputs**; the "port input feedback" network is deleted.
- `rtl/src/subsystems/systemctrl/nanosoc_ss_systemctrl.v` — connect the real pads:
  `.p0_in (P0_IN)`, `.p1_in (P1_IN)`.

`P1_IN` here is the post-hostio4-mux bus. In FT1248/UART2 mode (which the platform
straps) `nanosoc_ss_hostio4.v:214` passes the real pad through to bit 4, which is
what UART2's RXD needs.

## Running it

```sh
source <repo-root>/set_env.sh     # miniconda py3.10 + cocotb 2.0.1 + VCS
make                              # 2 tests, both must PASS
make falsify                      # same bench vs PRE-FIX RTL — both must FAIL
```

`make falsify` extracts the pristine `nanosoc_pin_mux.v` / `nanosoc_ss_systemctrl.v`
from the submodule's git HEAD and re-runs the **identical** bench against them. A
passing bench proves nothing unless it can fail; this is what makes the green run
mean something.

| RTL | `uart2_rxd` vs the pad | Result |
|---|---|---|
| pre-fix (git HEAD) | constant `1` — pad ignored entirely | `TESTS=2 PASS=0 FAIL=2` |
| post-fix | tracks the pad exactly | `TESTS=2 PASS=2 FAIL=0` |

## What the tests do

1. **`test_uart2_rxd_follows_the_pad`** — root-cause gate. Drives the `P1_IN[4]`
   pad and watches the pin mux's `uart2_rxd`. Purely combinational: no UART
   config, no baud timing, nothing else that could explain a failure.
2. **`test_uart2_receives_a_byte`** — product gate. Configures the genuine Arm
   `cmsdk_apb_uart` over AHB, shifts a real 8N1 frame (`0x5A`) onto the pad, reads
   the byte back out of the DATA register. This is the REPL's actual requirement.

Everything on the path is real RTL: the CMSDK AHB-to-APB bridge, the APB slave
mux, `nanosoc_pin_mux`, and the CMSDK UART itself.

## Scope and caveats

- **`socdebug_usrt_control` is stubbed** (`socdebug_usrt_control_stub.sv`). Its RTL
  lives in a repo not checked out here, and the only on-disk copies are build
  artifacts or unrelated support-request trees — pulling from an orphan tree is
  the provenance trap that has already burned this project once. It cannot
  influence `uart2_rxd`: the pin mux's `uart0_rxd`/`uart1_rxd` outputs are
  unconnected at its only instantiation, and nothing here addresses those APB
  slots.
- **The harness (`uart2_rx_harness.sv`) is not decoration.** It ties
  `HREADY <- HREADYOUT` (AHB-Lite single-slave; a bench that hardwires `HREADY=1`
  has its register writes silently dropped by the AHB-to-APB bridge's wait
  states), and it pins the GPIO port directions in RTL — `P0_OUT`/`P1_OUT`/
  `P1_OUTEN`/`P1_ALTFUNC` are *outputs* of the subsystem, and an earlier revision
  of this bench was driving them, reading back its own force. The elaborator now
  catches that class of bench bug.

## Side effects of the fix — worth knowing

The pin mux derives more than UART RX from `p1_in`. With the pads now genuinely
connected:

- **`timer0_extin` / `timer1_extin`** (`p1_in[8]`/`p1_in[9]`) now carry the real
  pad value instead of a synthetic constant `1`. In the DFX flow the platform
  drives `p1_in_w[15:8] = 8'h00`, so they change from `1` to `0`. Benign today —
  the CMSDK timers only consult `EXTIN` when configured for external
  input/enable, which no current firmware does — but it is a real behaviour
  change and is called out here rather than discovered later.
- **`uart0_rxd` / `uart1_rxd`** (`p1_in[0]`/`p1_in[2]`): no effect. Both are left
  unconnected at the pin mux's only instantiation.
- **`TSTART`/`TSTOP`**: no effect. Gated behind `CORTEX_M0PLUS` +
  `ARM_CMSDK_INCLUDE_MTB`, which this build does not define.

## ⚠️ Before this reaches the FPGA — which flow are you building?

`imp/fpga/nanosoc_ip/src/nanosoc_pin_mux.v` **is** a stale gitignored copy still
containing the buggy feedback network (md5 `1b0d4497a7`, 2026-07-06 — `p0_in`/
`p1_in` still `output wire` + the `port input feedback` assigns). The same holds
for `imp/fpga/nanosoc_ip/src/nanosoc_sysctrl.v` and the BOOT_CFG fix.

**AUDITED 2026-07-24 — the "must re-package or the board runs old RTL" warning is
flow-specific, and does NOT apply to the MPS3 DFX/RM path:**

- **MPS3 DFX `rm_nanosoc` (this repo) — UNAFFECTED.** `fpga/rp/nanosoc/filelist.tcl`
  derives from the upstream `nanosoc_FPGA.flist` → `nanosoc_arch_tech/rtl/flist/
  nanosoc_ip.flist`, which names
  `$(SOCLABS_NANOSOC_ARCH_TECH_DIR)/rtl/src/control/verilog/nanosoc_pin_mux.v`
  (line 79) and `.../soc_peripheral/nanosoc_sysctrl.v` (line 69) — i.e. the
  **arch_tech source tree**, which carries the fix (md5 `d8bcead723` /
  `0b3793bcb1`, 2026-07-14). **No flist references `imp/` for nanoSoC RTL** (the
  only `imp/` hits in any flist are Synopsys 28nm PD/VM/PLL/TS gate-level VIP for
  the ASIC flow). The pinned snapshot materialised by
  `fpga/rp/nanosoc/pin_nanosoc_snapshot.sh` likewise carries the fixed copies.
- **PYNQ-Z2 / Vivado IP-packaging flow — STILL AFFECTED.** That path consumes the
  packaged IP (`imp/fpga/nanosoc_ip/src/`, and the generated
  `imp/fpga/project/pynq-z2/...ipshared/4390/src/nanosoc_pin_mux.v`, also stale at
  `1b0d4497a7`). A re-package **is** required there.

**Residual check before trusting this on MPS3 silicon:** the source path is
correct, but confirm the deployed `rm_nanosoc` was *synthesised after 2026-07-14*
(re-run against the pinned snapshot if in doubt). Source-correctness is not the
same as "the bitstream on the board contains it".
