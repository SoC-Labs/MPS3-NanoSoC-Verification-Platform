# `tests/hostio4_golden` — L0: the vendor's own reference, on our simulator

**Result: PASS.** All four hostio4 streams are byte-exact against the vendor's
reference over every one of the 360 transmitted bytes, and the comparison
genuinely fails on a 1-byte corruption.

```sh
source set_env.sh
make -C tests/hostio4_golden        # golden + negative control
```

## Why this exists

Before building any hostio4 integration, establish that the **vendor RTL passes
the vendor's own reference stimulus on our simulator**. It needs no repo RTL, no
contract change, and no board. If this had failed, everything downstream would
have been built on sand.

`tb_hostio4.v` already has exactly the topology we care about:
`hostio4_controller` ⇄ `hostio4_target` back-to-back, all four AXIS byte
channels driven from `tb/asc-test.txt` and captured to four logs.

## Provenance — which RTL, and why it matters

> **Corrected 2026-07-10.** This page originally claimed two copies and named the
> wrong one as authoritative. There are **four**, and the benches were first run
> against a tree no repo file references. Everything was re-run; L0, L1 and L2a
> were unaffected, **L2b's headline result changed**. Read on.

There are **four** hostio4 trees on this machine:

| tree | contents | verdict |
|---|---|---|
| `nanosoc_m0_soc/nanosoc_arch_tech/rtl/hostio4/` | `.v` + `.sv` | **authoritative — what nanoSoC is built from** |
| `temp/nanosoc-multicore-system/nanosoc_arch_tech/rtl/hostio4/` | byte-identical `.v` set | same design; the top Makefile's `ARCH_TECH_SRC` |
| `nanoSoC-refactor/ethernet-subsystem-ahb-m0plus/nanosoc_arch_tech/rtl/hostio4/` | `.v` only | **orphan — referenced by zero repo files. Do not use.** |
| `~/SoCLabs/hostio4/` | standalone repo | mid-refactor; **cannot elaborate as packaged** |

The authoritative path is resolved exactly as `fpga/monolithic/filelist.tcl`
does, and the bench Makefiles now derive it rather than hardcode it:

```make
NANOSOC_M0_SOC_SRC ?= $(SOCLABS_NANOSOC_SOC_DIR)   # from tools.env
HOSTIO4_RTL        ?= $(NANOSOC_M0_SOC_SRC)/nanosoc_arch_tech/rtl/hostio4
```

**The orphan is a different design.** Its controller FSM is 10-bit with no
`OFFZ` state and no `ioreq_e`/`ioreq_t` tristate ports; the authoritative one is
11-bit, boots into `OFFZ` ("off/deselected from reset"), and tristates `ioreq`.
Its target FSM resets to `TXST` rather than `TXSZ` and inverts `vchan4_status`.
The FSM *encoding* and *next-state table* happen to be identical, which is why
L2a's park matrix and L1's skew budget came out the same — but the `OFFZ`
difference alone flipped an L2b result from "recovers" to "deadlocks". A
plausible-looking path cost a wrong conclusion.

Two concrete defects in the **standalone** repo, found by trying to run it:

1. **`hostio4.core` is broken.** `soc_rtl/hostio4_controller.v` instantiates the
   FSM with ports `io_clken` and `io_rdata4`. Those exist **only** in
   `hostio4_controller_fsm.sv`. The `.core` fileset lists the stale
   `hostio4_controller_fsm.v`, which lacks them. VCS:
   `Error-[UPIMI-E] Undefined port ... "io_clken" is not defined in module
   'hostio4_controller_fsm'`. A `fusesoc` build of this core cannot elaborate.

2. **Mixing the two copies silently corrupts the link.** Compiling the
   standalone (new) controller against the (unchanged) target compiles fine but
   **hangs**: target→controller completes 407 B while controller→target stalls at
   267 B, and the tb never terminates. The `target_rtl` is byte-identical between
   the two copies — only `soc_rtl` diverged. So the new controller and the old
   target do not speak the same protocol.

   This is the trap: it *compiles*. Always pair `soc_rtl` and `target_rtl` from
   the **same** copy, and for harness work use the copy the DUT contains.

## What the golden proves — and what it does not

**Proves:** with the matched arch_tech pair, all four channels carry 360 bytes
end-to-end with zero loss (all 8 stream endpoints report exactly 360), and every
captured byte matches the vendor's reference. Terminates in 185,088 cycles.

**Does NOT prove:**
- **Clock-domain crossing.** `tb_hostio4.v` names its clocks separately but
  derives both from one oscillator: `assign C_clk = clk; assign T_clk = !clk;` —
  same frequency, zero drift, a fixed 180° phase. Real hardware has the DUT on
  `dut_clk` and the shell on its own clock, unrelated. The vendor tb can say
  nothing about the `hostio4_*_sync` synchronisers.
- Backpressure under a stalled sink, or sustained throughput.
- Channel routing. All four sources are fed the *same* file, so a `ch0`/`ch1`
  swap would go unnoticed.
- Anything about tristate/`iodata4_e` behaviour at a real pad or partition
  boundary.

Each of the first three is covered by **L1**,
[`tests/hostio4_link_cdc`](../hostio4_link_cdc/): two independent clocks,
per-wire skew injection, random backpressure, and a distinct LFSR seed per
channel.

## Implementation notes

- Compile with a **global** `-timescale=1ns/1ps`: the vendor files disagree
  (`Error-[ITSFM] Illegal timescale for module`).
- Pass `-top tb_hostio4`. `tb_axi_stream_io8_rxd_check_file.v` is never
  instantiated by `tb_hostio4`, so VCS would elaborate it as a *second* top and
  its `initial` block would try to `$fopen("rxd-ref.log")` and print
  `reference log file failed to open`. We simply do not compile it.
- The reference `asc-test-ref.log` is **361 bytes**; the tb transmits **360**.
  The 361st is a trailing newline that is never sent (the stream ends `*END*` +
  `EOT`). Compare the first 360 bytes; do not "fix" the reference.

## Negative control

`make neg` rewrites one stimulus byte (line 3, `'3'` → `'X'`) and asserts the
captures diverge from the reference. Both `C_rxd0` and `T_rxd0` are checked, so
the control covers both directions. The stimulus is restored afterwards.
