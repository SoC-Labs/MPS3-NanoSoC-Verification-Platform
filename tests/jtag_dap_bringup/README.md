# jtag_dap_bringup — P2 SIM GATE (host-JTAG → SWJ-DP → DAP → Cortex-M0)

The blocking gate before any board work for the SoC-400 baseline integration
(Option B: standardise the CoreSight SoC-400 SWJ-DP as the baseline nanoSoC
debug DAP). It closes **the one segment nothing else proves**: the host-JTAG
serial line → SWJ-DP serial decode (`cxdapswjdp` + `nanosoc_swj_dp_gate`) →
DP-bus → AHB-AP → `nanosoc_dbg_ahb_bridge` → Cortex-M0 debug slave.

Every committed multicore bench bypasses that segment: `coresight_soc400_
dap_to_core` drives the **parallel DP-bus** directly (skipping `cxdapswjdp` +
the gate), and `coresight_soc400_swj_smoke` ties the SWD/JTAG pins idle. This
bench wiggles the four real JTAG wires (**TCK/TMS/TDI/TDO**) and halts a real
Cortex-M0 to `DHCSR.S_HALT`.

## Result (VCS 2022.06-SP2, cocotb 2.0.1) — PROVEN

```
TESTS=8 PASS=8 FAIL=0 SKIP=0
  test_tap_reset_idcode      PASS   TAP IDCODE      = 0x6BA00477
  test_dpacc_powerup_status  PASS   CTRL/STAT       = 0xF8000000 (CDBG/CSYS PWRUPACK asserted)
  test_ahb_ap_idr            PASS   AHB-AP IDR      = 0x84770001, ROM BASE = 0xA0000003
  test_halt_via_dhcsr        PASS   DHCSR 0x03000000 -> 0x01030003  (S_HALT=1)   <-- THE GATE
  test_halt_resume_rehalt    PASS   halt -> resume(S_HALT=0) -> re-halt(S_HALT=1)
  test_cpuid_anti_bleed      PASS   CPUID (running) = 0x410CC200 (NOT the 0xE7FEE7FE fetch word)
  test_negative_wrong_ir     PASS   halt write in BYPASS IR -> core NOT halted
  test_negative_no_powerup   PASS   no CTRL/STAT power-up -> core NOT halted
```

## Run

```sh
source ../../set_env.sh          # miniconda py3.10 + cocotb 2.0.1 + VCS + license
make                             # SIM=vcs (default); make WAVES=1 for waves.vcd
```

`NANOSOC_MULTICORE_HOME` has **no default**. Set it in the repo-root `tools.env`
(the Makefile `-include`s it, same seam and precedence as `fpga/dfx/Makefile`), or
on the command line:

```sh
make NANOSOC_MULTICORE_HOME=/path/to/nanosoc-multicore-system
```

Unset, the bench stops in about a second with a message naming the variable —
never deep inside VCS with "Source file cannot be opened". It used to default to
one workstation's home directory, which is invisible on that machine and silently
the wrong tree on every other one. See "Repaired 2026-09-11" below.

## What is instantiated (`tb_top.sv`)

```
 external JTAG pins  jtag_tck / jtag_tms / jtag_tdi / jtag_tdo
        │
 nanosoc_swj_dap_ss  ── nanosoc_swj_dp_gate ─ cxdapswjdp        (serial decode — the new segment)
        │            └─ nanosoc_dap_ss = 2× cxdapahbap + xlate + arb
        │
 single shared AHB master ── 1-region mock decoder
                                 └─ 0xA0xxxxxx  nanosoc_dbg_ahb_bridge  (UNCONDITIONAL-CAPTURE)
                                                     └─ slcorem0 (EXTERNAL_DAP=1) DBGAHB → Cortex-M0
```

- The M0 fetches a Thumb branch-to-self spin loop (`0xE7FEE7FE`) from an inline
  ROM, so `test_cpuid_anti_bleed` reads PPB **while the core is fetching** — the
  exact window that exposed the FPGA data-bleed the poll-gated micropython
  bridge reopens. The carried bridge here is the multicore **unconditional-
  capture** version.
- `DPRESETn = cpu0_sys_poresetn` (POWER-ON reset, **not** the pulsed `HRESETn`)
  — the plan's "don't get this wrong" rule so a live host link survives every
  `SYSRESETREQ`. `ntrst`/`npotrst` are the external JTAG reset pins, driven by
  the probe.
- Only AP0 is wired to the core; AP1 (`0xB0`) is left dormant on a default-OKAY
  slave (the plan's "instantiate the 2-AP block, wire only AP0" path).

## The driver — OpenOCD `remote_bitbang` byte semantics

`test_jtag_dap_bringup.py` builds its JTAG primitives on top of the **literal
ASCII byte codes** OpenOCD's `remote_bitbang` adapter emits:

| byte      | meaning                                             |
|-----------|-----------------------------------------------------|
| `'0'..'7'`| write `{tck,tms,tdi}` = `char - '0'` (bit2/1/0)      |
| `'R'`     | sample TDO, return `'0'`/`'1'`                       |
| `'r'..'u'`| reset: `char-'r'` = `{trst<<1 \| srst}` (nTRST low)  |

A server-side decoder turns each byte into a DUT pin wiggle, so exercising this
layer also validates the mapping the shell's fw **`jtag_server`** must
implement (`host/openocd/nanosoc_mps3_jtag.cfg`, `TRANSPORT_MODE=rbb`). Each
test logs its byte tally (`byte_stats()`), e.g. the halt test drives 1206
writes / 455 reads over the wire.

On top of the byte layer: JTAG TAP navigation (paths verified against the
`cxdapswjdp` next-state table), then ADIv5 JTAG-DP transactions — IR=IDCODE/
DPACC/APACC/BYPASS (IRLEN=4), 35-bit DPACC/APACC scans (`{data[31:0], A[3:2],
RnW}` LSB-first, ACK OK=0b010 / WAIT=0b001 with retry), posted AP reads flushed
via DP RDBUFF, and CSW/TAR/DRW memory access to the M0 PPB.

> **JTAG-DP note:** there is no DPIDR register in a JTAG-DP (that is an SW-DP
> concept). The TAP IDCODE is the JTAG identity; the equivalent **DPACC-read**
> proof is CTRL/STAT after the power-up handshake (`test_dpacc_powerup_status`).

## External dependencies (referenced in place, never copied)

- `NANOSOC_MULTICORE_HOME` — the multicore DUT checkout. It supplies the two Arm
  cell flists (`coresight_soc400/flist/`) and, through the variable below, the
  SoC Labs wrappers. **No default** — see Run, above.
- `SOCLABS_CORESIGHT_SOC400_TECH_DIR` — the shared SoC-400 tech block,
  `nanosoc_arch_tech/rtl/coresight_soc400_tech` inside that checkout, a nested
  submodule that ships its own filelist. This bench quotes that filelist rather
  than listing the wrapper sources itself.
- `SOCLABS_SLCOREM0_TECH_DIR` — `slcorem0` wrapper + Cortex-M0-QS IP flists.
- `ARM_IP_LIBRARY_PATH` — read-only vendor cells (`cxdapswjdp`, `cxdapahbap`,
  Cortex-M0-QS). Sourced via the two SoC-400 vendor flists; **never** edited.

See `jtag_dap_bringup.flist`. `expand_flist.sh` is a local copy of the
multicore utility (a SoC Labs script, not vendor IP) so the bench is
self-contained inside `mps3-nanosoc-platform`.


## Repaired 2026-09-11 — and what the repair found

This bench could not elaborate for roughly six weeks. It read six SoC Labs
SoC-400 wrapper sources from an `rtl` directory under the multicore tree's
`coresight_soc400`, and that tree had moved the wrappers into its shared tech
block. The two Arm vendor flists next to them still resolved, so the failure was
six stale paths and not a missing dependency.

Three things are worth recording, because only the first was visible:

1. **The fix is to quote the tech block's filelist, not to repoint six paths.**
   `nanosoc_dap_ss.v` gained an instance of a debug-AHB timeout module in the
   move. Six corrected paths would have elaborated one module short; the tech
   block's own filelist carries it.

2. **The bench then passed 8/8 with four parameter overrides silently ignored.**
   `tb_top.sv` still overrode `CPU0_DBG_BASE` / `CPU1_DBG_BASE` /
   `AP0_ROMBASEADDR` / `AP1_ROMBASEADDR`, the parameters of the retired two-AP
   block; the replacement is ONE `NUM_AP`-parameterised module and has none of
   those names. VCS reports an override of a parameter a module does not have as
   `Warning-[AOUP] ... will ignore it`, not an error. The AP therefore ran on its
   default `0xF000_0003` ROM base while the bench reported success, because
   `test_ahb_ap_idr` only *logged* `BASE`. It now **asserts** it — a check whose
   control was watching it fail against the unfixed parameters — so an ignored
   override reddens the bench instead of passing it. `BASE` is the only
   externally readable value that is a pure function of one of these parameters,
   which is what makes it the right assertion. The evidence was already in this
   file: the Result block above was recorded back when `AP0_ROMBASEADDR` still
   existed and says `ROM BASE = 0xA0000003`, while the stale-parameter build
   produced `0xF0000003`. Nobody compared them, because nothing had to.

3. **A stale `sim_build/simv` and a `results.xml` reporting 8/8 sat in this
   directory the whole time.** Both are gitignored build products, and both made
   a `ls` of the directory look like a bench that had recently passed. They are
   gone; rebuild from source.

### Why this bench was kept rather than retired

`tests/jtag_chain/` (landed `23814a9`) drives the same SWJ-DP and halts the same
Cortex-M0 through a **two**-TAP daisy chain, with stronger negative controls than
these — a fabric-side chain-order flip and a legacy single-TAP arithmetic control.
It is the better bench and it is not going anywhere. It is not, however, a
superset:

* **Topology.** Its DUT is `fpga/rp/nanosoc_iice/rp_nanosoc_iice_shim.sv`, the
  Identify-instrumented RM. The fielded product RM (`fpga/rp/nanosoc/`) has ONE
  TAP, and `jtag_chain`'s `test_control_single_tap_arithmetic` exists to prove
  that single-TAP scan arithmetic **fails** on a chain. Nothing there covers the
  shipped topology's own scans; this bench is that cover.
* **`test_halt_resume_rehalt`** — halt → resume → re-halt. `jtag_chain` halts and
  never resumes, so a one-shot debug write path would pass there.
* **`test_cpuid_anti_bleed`** — CPUID read while the core is RUNNING, asserted not
  to return the `0xE7FEE7FE` spin-loop fetch word. That is the regression guard
  for `nanosoc_dbg_ahb_bridge`'s unconditional-capture `ST_CAPTURE`, a real FPGA
  data-bleed. `jtag_chain` reads DHCSR pre-halt but only logs it.

Two benches compiling the same confidential vendor RTL is a maintenance cost, and
this one rotted precisely because nothing in CI can run it (it needs VCS and an
external, licensed tree). The `needvars` guard and the `BASE` assertion are the
mitigation: it now fails loudly and immediately in both of the ways it previously
failed quietly.
