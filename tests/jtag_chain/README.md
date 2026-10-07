# `tests/jtag_chain` — two TAPs on one 4-wire partition wire-set

The sim gate for the IICE reconfigurable module's rebase onto the fielded boundary
(mint `0xA8C1C535`). It drives the four JTAG partition pins of the **real**
`fpga/rp/nanosoc_iice/rp_nanosoc_iice_shim.sv` and proves the IEEE 1149.1 daisy
chain that file builds: Identify's soft TAP in series with the DUT's CoreSight
SoC-400 SWJ-DP, both live at once.

Design authority: [`docs/planning/IICE_JTAG_CHAIN.md`](../../docs/planning/IICE_JTAG_CHAIN.md).

## Result — VCS T-2022.06-SP2, cocotb 2.0.1

```
TESTS=7 PASS=7 FAIL=0 SKIP=0
  test_chain_idcodes                      PASS  [0]=0x6BA00477  [1]=0x1063E4CD
  test_chain_ir_length                    PASS  total IR = 9  (DAP 4 + IICE 5)
  test_dap_halt_through_chain             PASS  DHCSR.S_HALT=1  <-- THE GATE
  test_iice_shift_with_dap_in_bypass      PASS  IDHW_CHAIN write + scan-back
  test_control_wrong_chain_order          PASS  (control: swapped host order)
  test_control_single_tap_arithmetic      PASS  (control: legacy scan cannot halt)
  test_dut_reset_drops_the_debug_powerup  PASS  CTRL/STAT F8000000 -> A8000000 -> F8000000
```

plus the fabric-side control:

```
$ make control-order
== CONTROL: rebuilding with the chain order FLIPPED in the fabric ==
CONTROL OK: test_chain_idcodes FAILED with the order reversed
   chain IDCODEs (nearest TDO first): ['0x1063E4CD', '0x6BA00477']
```

## Run

```sh
source ../../set_env.sh          # miniconda py3.10 + cocotb 2.0.1 + VCS + licence
make NANOSOC_MULTICORE_HOME=/path/to/nanosoc-multicore-system
make WAVES=1 ...                 # + waves.vcd
make control-order ...           # the fabric-side negative control (must FAIL)
```

`NANOSOC_MULTICORE_HOME` has **no default**, on purpose — see "Two traps this bench
walked into" below. Set it in `tools.env` (the same file `fpga/dfx/Makefile`
includes) or on the command line.

## What is real and what is a model

This distinction is load-bearing; read it before quoting any result here.

| | |
|---|---|
| **REAL** | `rp_nanosoc_iice_shim.sv`, compiled from the RM directory, unmodified — the chain splice IS the design under test |
| **REAL** | the Arm SoC-400 SWJ-DP (`cxdapswjdp` via `nanosoc_swj_dap_ss`) → AHB-AP → `nanosoc_dbg_ahb_bridge` → a real Cortex-M0, referenced in place from the read-only vendor library, never copied |
| **REAL** | the OpenOCD `remote_bitbang` byte semantics of the driver — the same byte layer `tests/jtag_dap_bringup` validates and `firmware/jtag_server` must implement |
| **MODEL** | `iice_soft_tap_model.sv`, standing in for Identify's soft TAP. Its IR length (5) and IDCODE (`0x1063E4CD`) are read off Identify's **own** device table (`syn_idcodes.tcl:741-742`), not invented; its internals are a plain 1149.1 controller and prove nothing about a real IICE |

`iice_core_stub.sv` presents the 39-port instrumented-EDIF boundary (35 contract
ports + the four `identify_jtag_*` the instrumentor adds) and holds those two TAPs.
It is a stand-in for a cell that only exists after a licensed Synplify run — but it
is **not free to drift**: `check_core_stub_ports.py` derives the expected 39-port
list from the real `rp_nanosoc_iice_core.sv` (via `fpga/dfx/pin_check.py`'s own
parser) plus `lint/gen_lint_stubs.py`'s `IDENTIFY_SOFT_TAP` list, and the Makefile
runs it **before** compiling. A stale stub makes the bench refuse to build rather
than pass against a topology the RM does not have.

```
 cocotb remote_bitbang driver
      |  jtag_tck / jtag_tms / jtag_tdi / jtag_tdo   (the 4 partition pins)
      v
 rp_nanosoc_iice_shim            <-- THE DESIGN UNDER TEST (real file)
      |  chain splice, order set by IICE_TAP_NEAREST_TDI
      v
 rp_nanosoc_iice_core  [bench stand-in for the instrumented EDIF cell]
      +-- iice_soft_tap_model    IR 5, IDCODE 0x1063E4CD   (MODEL)
      +-- nanosoc_swj_dap_ss     IR 4, IDCODE 0x6BA00477   (REAL Arm RTL)
            -> cxdapahbap -> nanosoc_dbg_ahb_bridge -> slcorem0 -> Cortex-M0
```

## vs `tests/jtag_dap_bringup`

That bench proves the SWJ-DP serial front end with **one** TAP on the wires, and is
the model this one is copied from. This one adds a second TAP in front of it and
proves the DAP is still reachable through the extra 5 IR bits and 1 DR bit of
BYPASS — and that Identify's TAP is reachable the other way round.

**Neither replaces the other, and that was settled rather than assumed.** The
single-TAP topology this bench does *not* cover is the **fielded** one
(`fpga/rp/nanosoc/` has one TAP; this bench's DUT is the Identify-instrumented
RM), and `test_control_single_tap_arithmetic` here exists precisely to prove
single-TAP scan arithmetic **fails** on a chain — it is a control, not coverage.
`jtag_dap_bringup` additionally holds `test_halt_resume_rehalt` (this bench halts
and never resumes, so a one-shot debug write path would pass here) and
`test_cpuid_anti_bleed`, the guard for `nanosoc_dbg_ahb_bridge`'s
unconditional-capture `ST_CAPTURE` data-bleed. See that bench's README,
"Why this bench was kept rather than retired".

## The controls, and why there are three

A chain test that is not position-sensitive proves nothing, so:

| control | what it breaks | expected |
|---|---|---|
| `test_control_wrong_chain_order` | the **host's** declaration order (same wire traffic, different interpretation) | both IDCODEs land in the wrong slots |
| `make control-order` | the **fabric's** order (`IICE_TAP_NEAREST_TDI=0`, rebuilt into its own `sim_build_control/`) | `test_chain_idcodes` FAILS; the target fails loudly if it passes |
| `test_control_single_tap_arithmetic` | the **BYPASS padding** (legacy 4-bit-IR / 35-bit-DR scans) | the core does not halt |

`make control-order` builds into a separate `SIM_BUILD` deliberately: reusing the
shipped-order `simv` is exactly how a control quietly stops being one.

## Two traps this bench walked into

**1. The SWJ-DP's TAP controller is X in simulation, and the RM cannot fix it.**
`cxdapswjdp`'s JTAG protocol block resets its TAP state and IR from `ntrst` (3
`negedge ntrst` blocks) and everything else from `npotrst` (17). The RM straps
`dap_ntrst` **high** — `rp_nanosoc_wrapper.sv:526`, deliberately, because no TRST
wire crosses the fielded boundary. So in RTL simulation that FSM starts at X and
never resolves: five TCK with TMS high walk an X state to an X state, TDO stays 0,
and every IDCODE read returns zero. On the real KU115 it works, because Xilinx's
GSR clears every fabric flop at the end of configuration.

`iice_core_stub.sv` therefore carries a 100 ns `bench_gsr_n` pulse that models
**that one device behaviour and nothing else**. It is released long before any test
drives TCK and no test uses it. Remove it and the bench reports a dead DAP the
silicon does not have; drive it from a test and you would be testing a TRST pin that
does not exist.

**2. The SoC-400 wrappers moved, and `tests/jtag_dap_bringup` did not notice.**
That bench's flist reads them from
`$(NANOSOC_MULTICORE_HOME)/coresight_soc400/rtl/*.v`. **That directory no longer
exists** — the multicore tree moved its wrappers into the shared tech block
(`nanosoc_arch_tech/rtl/coresight_soc400_tech`, a real submodule with its own flist),
and `nanosoc_swj_dap_ss.v` is now one parameterised block (`NUM_AP`) replacing the
old `CPU0_/CPU1_/AP0_/AP1_` parameter set. Only its committed `sim_build/simv` made
that bench look alive; it could not rebuild from a clean tree. This bench reads the
tech block's own flist and uses `NUM_AP=1`, the fielded single-core configuration.
See `docs/planning/IICE_JTAG_CHAIN.md`.

> **REQUEST ANSWERED — `8b9a2fe`, 2026-09-11.** When this was written, fixing
> `jtag_dap_bringup` was a request to whoever owned it and not a change made
> from here. It has been made: that bench now quotes the same tech-block filelist
> this one does (which also picks up `nanosoc_dap_ahb_timeout`, a module the move
> added and that six hand-corrected paths would have missed), its Makefile takes
> `NANOSOC_MULTICORE_HOME` from `tools.env` with no baked default exactly as this
> one does, and the stale `sim_build/simv` and `results.xml` are deleted. The
> repair then found a second fault the stale paths were hiding — four parameter
> overrides of the retired two-AP block, which VCS demotes to
> `Warning-[AOUP] ... will ignore it`, so the AP silently ran on its default ROM
> base while the bench reported 8/8. `test_ahb_ap_idr` asserts the `BASE`
> readback now.
>
> The *class* of fault is closed here too: `tests/common/list_benches.py` used to
> gate that bench's readiness on its own committed `tb_top.sv`, which is why it
> read READY throughout. Readiness for an external-DUT bench is now the tech
> block's filelist actually resolving — and **this** bench is finally IN that
> list and in `tests/Makefile`'s `BENCH_DIRS`. It had been in neither, so it was
> invisible to `make list`, never run by `make all`, and `make clean` never swept
> the `sim_build/` sitting in this directory either.

## Confidential IP

The Arm SoC-400 CoreSight RTL under `$ARM_IP_LIBRARY_PATH` is Arm Academic Access
collateral, read-only and lab-wide. `jtag_chain.flist` references it **in place** by
absolute path through env vars; nothing is copied into this repository and nothing
committed here quotes it. Same discipline as `tests/jtag_dap_bringup`.

## Files

| file | what it is |
|---|---|
| `tb_top.sv` | the four partition pins, a clock, three resets, and the real shim. Thin by design. |
| `iice_core_stub.sv` | the 39-port instrumented-EDIF stand-in: real SWJ-DP + M0 on `jtag_*`, the soft-TAP model on `identify_jtag_*` |
| `iice_soft_tap_model.sv` | behavioural 1149.1 TAP, IR 5 / IDCODE `0x1063E4CD` |
| `test_jtag_chain.py` | the cocotb driver: remote_bitbang bytes → chain-aware IR/DR scans → ADIv5 JTAG-DP |
| `check_core_stub_ports.py` | the anti-drift guard; runs before every compile |
| `jtag_chain.flist` | where the confidential and external RTL lives |
| `expand_flist.sh` | local copy of the multicore flist flattener (SoC Labs script, not vendor IP) |
