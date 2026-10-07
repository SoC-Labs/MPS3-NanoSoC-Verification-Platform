# tests/qspi_xip — the M2 gate: memory-mapped XiP from QSPI flash

**Status: GREEN.** 5/5 tests pass, and the bench is falsifiable (see below).

```
** test_qspi_xip.test_apb_regs_reachable                      PASS
** test_qspi_xip.test_flash_vip_jedec_id                      PASS
** test_qspi_xip.test_xip_read_matches_flash                  PASS   <-- THE GATE
** test_qspi_xip.test_xip_cache_hit                           PASS
** test_qspi_xip.test_xip_noncacheable_hprot_bypasses_cache   PASS
** TESTS=5 PASS=5 FAIL=0 SKIP=0
```

## What the gate is

NanoSoC instantiates the SoC Labs QSPI flash controller as `u_qspi_flash_0`,
module **`qspi_flash_ahb`** (`nanosoc_m0_soc/src/rtl/wrappers/qspi_flash_ahb.v`):

| region      | address       | what                                       |
|-------------|---------------|--------------------------------------------|
| `qspi_mem`  | `0x7000_0000` | AHB, read-only, 4 MB — the **XiP aperture** |
| `qspi_ctrl` | `0x7400_0000` | AHB→APB, 64 KB — the config registers      |

The gate: **can a bus master fetch words from the XiP aperture and get back
exactly what is in the flash?** If yes, the CPU can execute code in place from
QSPI flash, which is the premise of the whole boot / interpreter story.

## Run it

```bash
source ~/SoCLabs/mps3-nanosoc-platform/set_env.sh   # py3.10 + cocotb 2.0.1 + VCS
make                                          # all 5 tests, ~10 s
make TESTCASE=test_xip_read_matches_flash     # just the gate
make falsify                                  # prove the bench can fail
```

## The DUT is the wrapper, on purpose

The DUT is `qspi_flash_ahb`, **not** `top_ahb_qspi`. The wrapper is what the SoC
integrates, and it is exactly where an integration mistake would live: `HSEL` vs
the IP's `HSELx`, the `PADDR[15:0]` slice, an `o`/`i`/`e` swap on the split
tristate pads. Benching `top_ahb_qspi` directly would step over the code under
suspicion. (ahb_qspi's own 55-test suite already covers `top_ahb_qspi`; this
bench exists to cover the seam between it and NanoSoC.)

## The flash is the real VIP

`ahb_qspi/verif/VIP/SST26VF064B.v` — the Microchip/SST behavioural model, the
same one ahb_qspi's suite uses, and the same part that is on the board. It has a
bidirectional `inout [3:0] SIO`; the wrapper has split `QSPI_IO_o/i/e`. The
harness resolves them per lane, exactly as `ahb_qspi/pad_level/generic/ahb_qspi_pads.v`
does:

```verilog
assign SIO[i]      = QSPI_IO_e[i] ? QSPI_IO_o[i] : 1'bz;
assign QSPI_IO_i[i]= SIO[i];
```

No pull-ups (matching `ahb_qspi_pads.v` — the controller drives IO[3:2] high in
SPI mode, which keeps the flash's HOLD#/WP# deasserted).

## The golden reference does not come from the DUT

This is the part that makes the gate mean something.

ahb_qspi's own XiP tests program the flash **through the controller's
page-program path** and then read it back through the controller. That
round-trips through the DUT in both directions: a byte-lane swap or an
endianness error present in *both* the program and the read path would cancel out
and the test would pass on broken RTL.

So this bench instead **backdoors** the flash: the harness deposits bytes
straight into the VIP's own non-volatile array (`u_flash.I0.memory[]` — the
backdoor the model's own header documents), using a pattern that is a pure
function of the byte **address**:

```
byte(a) = ((a[7:0] + 0x11) & 0xFF) ^ a[15:8]
```

Consecutive bytes differ, so byte order is pinned **absolutely**. The gate then
asserts that the byte at flash address `A` comes back in `HRDATA[7:0]` of the
word at `A` — which is what a little-endian Cortex-M0 needs to fetch
instructions. Observed: flash `0x030000` → `0x14131211`. Correct.

The gate also cross-checks each word over the **APB/controller** path (fast-read
`0x0B`, cache and XiP port entirely bypassed) before doing the AHB reads. Three
independent legs must agree — backdoor golden, APB read, AHB XiP read. The APB
leg is what distinguishes *"the XiP path is broken"* from *"the flash doesn't
contain what we think it does"*.

## The bring-up sequence was cribbed, not guessed

* **Register offsets** come from `ahb_qspi/sys_desc/register_maps/apb_qspi_regs.yaml`
  (generated from `src/rdl/apb_qspi_regs.rdl`).
* **The CG092 cache-enable handshake** is copied from ahb_qspi's own suite
  (`_xip_baseline_minus` / `_switch_to_plain_spi_xip`). It looks redundant and is
  not: the pre-read of SR, the post-read of CCR, and the CCR read that primes each
  SR poll are all load-bearing — omit any and STATUS sticks at `0x1`
  (transitioning). The priming read defeats a `cmsdk_ahb_to_apb` artefact (it holds
  `HREADYOUT=1` with registered `HRDATA` while idle, so an isolated single read
  returns stale data forever).
* **XiP mode**: plain SPI, fast-read `0x0B`, 8 dummy cycles, no QIO, no `NO_CMD`,
  no continuous-read — the lowest-common-denominator mode, and the one a bootrom
  can actually get into from reset. ahb_qspi proves this combination works
  (`test_xip_plain_spi_fast_read_line_fill`).
* **`CLK_DIV=2`** (SCLK = HCLK/4 = 25 MHz). The controller enforces `>= 2`; the VIP
  corrupts its entire array to `'x` on a timing violation, so this is not optional.

Nothing in the XiP bring-up had to be invented. The sequence was fully
recoverable from ahb_qspi's bench.

## The cache proof

`test_xip_cache_hit` measures caching **at the pads**, not by reading a status
bit: the harness counts `QSPI_nCS` falling edges and `QSPI_SCLK` rising edges in
RTL, and the test takes deltas across a single AHB read.

```
cold read       -> 1 nCS, 168 SCLK
re-read (same word)     -> 0 nCS      <- served from the CG092
read another word, same line -> 0 nCS
read the NEXT line      -> 1 nCS, 168 SCLK   <- control
```

168 SCLK = 8 (opcode) + 24 (address) + 8 (dummy) + 128 (a 16-byte line). Exactly
a plain-SPI `0x0B` fast read of one cache line — a nice independent confirmation
that the line-fill is doing what we think.

**The control matters.** If the "0 nCS" result were an artefact of a dead
counter, the next-line read would also read 0. Requiring the next-line read to
show flash traffic is what makes the zero meaningful. Without it a broken monitor
looks like a perfect cache.

## Findings

### 1. The XiP aperture's address is load-bearing for cache performance

Two facts combine into a live hazard:

1. The CG092 only allocates a line when `HPROT[3]` is set —
   `p_flash_cache_f0_core.v`: `non_cache_aphase = HWRITE | ~HPROT[3]`.
2. **The Cortex-M0 does not let software choose `HPROT[3]`.** It derives it from
   the ARMv6-M default memory map, from `address[31:29]` alone —
   `Cortex-M0/AT510-r0p0-03rel2/.../cortexm0/verilog/cm0_matrix.v:249`:

   ```verilog
   wire ahb_c = ( ~ahb_addr[31] &  ahb_addr[29] |
                  ~ahb_addr[30] & ~ahb_addr[29] );
   assign hprot_o = { ahb_c, ahb_b, 1'b1, ahb_prot };
   ```

`qspi_mem` is at `0x7000_0000` → `a31=0, a30=1, a29=1` → `ahb_c = 1`. **Good
news: the aperture as placed IS cacheable, so CPU instruction fetches do get the
cache.** `test_xip_cache_hit` drives `HPROT=0b1110` — the literal vector a
Cortex-M0 puts on the bus when fetching an instruction from `0x7000_0000` — and
the hits are real.

But move `qspi_mem` into the peripheral region (`0x4000_0000`) or a device region
(`0xA/0xC/0xE...`) and `ahb_c` becomes 0, the CG092 silently stops caching, and
every instruction fetch pays a full 168-SCLK flash read. **Nothing would fail —
it would just get ~50x slower.** That is precisely the sort of regression that is
never caught.

`test_xip_noncacheable_hprot_bypasses_cache` pins this down: it drives the HPROT
a CM0 would emit at `0x4000_0000` and proves the cache is bypassed (flash re-read
on every access, data still correct). If anyone relocates the aperture,
`test_xip_cache_hit` will fail its opening assertion and say why.

**Action: do not move `qspi_mem` out of a cacheable ARMv6-M region.**

### 2. Firmware constraint: no APB commands while `XIP_ACTIVE` is set

`qspi_controller_mux.v:65-70` hands the controller to the AHB side when
`XIP_ACTIVE=1`, and *forces* the APB side off:

```verilog
assign APB_QSPI_ENABLE_ACK = (XIP_ACTIVE==1'b0) ? QSPI_ENABLE_ACK : 1'b0;
assign APB_QSPI_BUSY       = (XIP_ACTIVE==1'b0) ? QSPI_BUSY       : 1'b1;
```

So any driver that tries to issue a controller command (erase, page-program,
read-status, JEDEC) without first clearing `XIP_ACTIVE` will hang forever waiting
for an IRQ that cannot come. This is the IP behaving as designed, not a bug — but
it is a sharp edge, and it bit this bench during development (the gate's APB
cross-check leg hung until it was moved ahead of XiP enable). Any flash-update
routine must: clear `XIP_ACTIVE` → do its commands → re-enable XiP → re-enable the
cache. **Note the cache must be invalidated/re-enabled after a flash write, or it
will serve stale lines.**

### No bugs found in the wrapper or the SoC integration.

The wrapper's port mapping is correct. Specifically checked and confirmed working:
`HSEL`→`HSELx`, the `PADDR[15:0]` slice into the internal `cmsdk_apb_slave_mux`
(controller at `PADDR[15:12]==0x0`, CG092 at `0x1` → APB offset `0x1000`), and the
split-tristate pads. Byte order through the whole chain is correct little-endian
(the byte-swap in `ahb_qspi_interface.sv:89-94` un-does the controller's MSB-first
shift-register order, as it should).

The SoC's `HADDR[21:0]` folding (`top_ahb_qspi.v`: `.HADDRS(HADDR[21:0])`) means
the `0x7000_0000` region base is discarded and flash address == `HADDR[21:0]`. The
tests drive **full SoC addresses** (`0x7003_0000`, …) on purpose, so if that
folding ever changes this bench notices.

## Falsification — `make falsify`

A green run is worthless if the bench cannot go red. `make falsify` mutates the
wrapper with the single most likely real integration mistake for a split-tristate
pad interface — **`QSPI_IO_i[3:0]` wired back to front** — and re-runs the two
pad-facing tests. Both must fail. Verified:

```
JEDEC ID = 0xffffff00 (expect 0x4326bf00)                       -> FAIL
APB flash read 0x030000 = 0xffffffff (preloaded 0x14131211)     -> FAIL
=== OK: the bench fails on the mutated wrapper, as it must ===
```

`test_apb_regs_reachable` is *expected* to keep passing under that mutation — it
never leaves the APB. That asymmetry is the point: each test is sensitive to the
thing it claims to test and not to noise. The mutant is generated into a scratch
dir; the shared SoC tree is never touched.

## Caveats / stubs

* **No stubs.** Every block in the DUT path is real RTL: the wrapper, all of
  `top_ahb_qspi`, the Arm CG092 flash cache, `cmsdk_apb_slave_mux`, the SoC's
  `cmsdk_ahb_to_apb`, and the SST26VF064B VIP. Nothing is faked.
* The VIP's **timing** parameters (`Tbe/Tse/Tsce/Tpp/Tws`) are shortened by
  `defparam` to 1 µs, identical to ahb_qspi's TB. These are timing-only and change
  no data behaviour; without them a flash soft-reset costs milliseconds of sim time.
* `WATCHDOG_WIDTH` is deliberately **not** overridden. ahb_qspi's own TB forces it
  to 12 to fit its AHB master timeout; the SoC wrapper does not expose the
  parameter, so the DUT here runs the taped-out default of 16. The cocotb AHB
  masters get a 40 000-cycle timeout instead (a healthy line fill costs ~700 HCLK).
* This bench proves the **read** path. Flash **programming** through the AHB/XiP
  side is not exercised here (the aperture is `sw_access: rx` — read-only by
  design); the controller's page-program path is covered by ahb_qspi's own suite.
* Everything runs against the **real flash contents**, but in **simulation**. The
  on-board SST26VF064B has not been read through this path on silicon.

## Files

| file                    | what                                                                 |
|-------------------------|----------------------------------------------------------------------|
| `qspi_xip_harness.sv`   | tristate resolution, `HREADY <- HREADYOUT` on both AHB ports, the SoC's APB bridge, the flash VIP, the backdoor preload, the RTL edge counters |
| `test_qspi_xip.py`      | the 5 tests, the register map, the cribbed bring-up helpers            |
| `Makefile`              | source list (mirrors `nanosoc_m0_soc/flist/nanosoc_qspi.flist`) + `falsify` |
