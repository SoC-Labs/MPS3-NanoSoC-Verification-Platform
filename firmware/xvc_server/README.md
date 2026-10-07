# xvc_server/

TCP server on port 2542 implementing the Xilinx Virtual Cable (XVC v1.0)
protocol. **One protocol engine, compile-time-selectable shift targets.**

| Target | Define | Shifts into | Reaches |
|---|---|---|---|
| **DBGBR** (default) | *(none)* | Xilinx **Debug Bridge** `From_AXI_to_BSCAN` (`debug_bridge_0`, `C_DEBUG_MODE=2`) @ `0x44A8_0000` | the **static** design's BSCAN chain — Vivado hw_server ILA/VIO over Ethernet (ARCHITECTURE_SPEC.md §10 / §4.1) |
| **JTAGBB** | `-DMPS3_XVC_TARGET_JTAGBB` | **JTAGBB** `DRIVE`/`SAMPLE` @ `0x44A7_0000` (`fpga/shell/ip/jtag_bb`), bit-banged | the DUT's JTAG wire-set — on the IICE RM that is a **two-TAP daisy chain**: Synopsys Identify's soft TAP *plus* the DUT's SoC-400 SWJ-DP (`docs/planning/IICE_JTAG_CHAIN.md`) |
| **SWDBB** *(retired spelling)* | `-DMPS3_XVC_TARGET_SWDBB` | the **same** registers, under the names of `swd_bb.sv` | identical machine code to JTAGBB — see below |

### SWDBB vs JTAGBB — read this before using either

The A6 SWD→JTAG cutover **deleted** `swd_bb`. The live block is
`fpga/shell/ip/jtag_bb`, which the fielded BD instantiates at the **same**
`0x44A7_0000` page (`fpga/shell/bd/shell_bd.tcl:560,1401`) **with the same bit
positions**:

```
SWDBB_DRIVE_SWCLK    (1<<0) == JTAGBB_DRIVE_TCK   (1<<0)
SWDBB_DRIVE_SWDIO_O  (1<<1) == JTAGBB_DRIVE_TMS   (1<<1)
SWDBB_DRIVE_SWDIO_OE (1<<2) == JTAGBB_DRIVE_TDI   (1<<2)
SWDBB_SAMPLE_SWDIO_I (1<<0) == JTAGBB_SAMPLE_TDO  (1<<0)
MPS3_SWDBB_BASE            == MPS3_JTAGBB_BASE
```

So an old `XVC_TARGET=swdbb` build still drives the right wires — **by
coincidence, through the names of a block that no longer exists.** That is the
kind of coincidence that stops being true silently, so it is now a
**compile-time assertion** (`xvc_server.h`,
`xvc_bb_layout_matches_the_retired_swdbb_names`) rather than a comment:
renumbering `jtag_bb`'s DRIVE bits fails the build instead of bit-banging the
wrong pins. Mutation-checked.

`SWDBB` is kept only because these still spell it that way and are not this
module's to change: `firmware/platform/verify_shell_image.py`
(`--expect-xvc-target swdbb`), `firmware/platform/board_xvc_throughput.sh`,
`firmware/test/{test_xvc_server_swdbb,test_xvc_posix_loopback,xvc_fw_daemon,
fake_jtag_tap}.c`, and `host/socket_harness/xvc_server.py`. **New work uses
`XVC_TARGET=jtagbb`.**

### The chain, and why nothing here knows about it

On the IICE RM the `jtag_*` wire-set carries **two** TAPs — Identify's soft TAP
(IR 5, IDCODE `0x1063E4CD`) in series with the DUT's SoC-400 SWJ-DP (IR 4,
IDCODE `0x6BA00477`). **This engine is unchanged by that, deliberately.** It
shifts the TMS/TDI vectors it is handed and returns TDO; which TAP those bits
address, and how many BYPASS bits pad them, is the *client's* arithmetic
(OpenOCD's `jtag newtap` list, Identify's `chain add`). A bit shifter that knew
about TAPs would have to be told the chain, and would then be a second place for
the chain to be wrong.

Proven: `firmware/test/test_xvc_jtagbb_chain` drives a two-TAP chain through the
real engine over a real loopback TCP socket and recovers **both** IDCODEs, with
`3n+1` register accesses and zero accesses outside `jtag_bb`'s two mapped
offsets. Its negative control (`_latesample`) recovers neither.

**DBGBR is the default, deliberately**: it is what the shipped 2542 server
does, the chain it reaches belongs to the static design, and an
undefined-flag build is byte-identical to today's image. Nothing about the
bit-bang retarget touches that path — the framing, gating, backpressure and
transport code is shared and target-independent; the targets differ only
inside `xvc_server_do_shift()`, and JTAGBB/SWDBB share even that (one
`MPS3_XVC_TARGET_IS_BITBANG` loop, so the two spellings cannot drift apart).

**REAL (W-VITIS/A3, 2026-07-07)** — was the last stub module. The
protocol engine + DBGBR shift chunking are implemented against the
`common/net_if.h` seam and host-tested in
`firmware/test/test_xvc_server.c` (94 checks: chunking, fragmentation,
gating, fail-closed paths) against `fake_net_if.c` + a behavioral DBGBR
fake (TDO = TMS^TDI, per-kick LENGTH capture).

**SWDBB target added 2026-07-29**, host-tested in
`firmware/test/test_xvc_server_swdbb.c` — see "SWDBB target" below.
**Renamed/retargeted to JTAGBB 2026-09-10** when the RM was rebased onto the
fielded boundary; host-tested in `firmware/test/test_xvc_jtagbb_chain.c`.

**Driven by the real Synopsys Identify debugger, board-free, 2026-07-30.**
`firmware/test/xvc_fw_daemon.c` (`make -C firmware/test tools`) links THIS file
against real POSIX sockets (`firmware/test/posix_net_if.c`, the third
`common/net_if.h` backend) and an in-memory IEEE-1149.1 TAP
(`firmware/test/fake_jtag_tap.c`), so `identify_debugger_shell` drives the real C
engine over TCP with no board and no MicroBlaze —
`host/identify/fw_com_check.sh`. See "Proven by execution" below for exactly what
that does and does not establish.

## XVC wire protocol (Xilinx's own XVC 1.0 spec, reused as-is)

```
"getinfo:"                 -> "xvcServer_v1.0:<max_vector_bits>\n"   (ASCII)
"settck:"<u32 period_ns LE> -> <u32 period_ns LE>                    (binary echo)
"shift:"<u32 num_bits LE><tms bytes><tdi bytes>
                           -> <tdo bytes>          (ceil(num_bits/8) each)
```

- Commands are **not** newline-framed: fixed ASCII prefix + binary
  length-determined payload. The parser is an incremental
  accumulate-and-reevaluate buffer (`try_dispatch()`), tolerant of any TCP
  fragmentation, fail-closed on any non-XVC prefix byte.
- `<max_vector_bits>` = `MPS3_XVC_MAX_VECTOR_BITS` (2048) is a **chunk hint to
  the client, NOT a bound on `num_bits`.** A client sizes the *payload* of a
  `shift:` against this number and then adds its own TAP state-navigation TMS
  bits, so the `num_bits` that arrives legitimately **exceeds** it. Measured
  against the real Synopsys Identify debugger (T-2022.09-SP2), varying only the
  advertisement:

  | advertised | `num_bits` then requested |
  |---|---|
  | 1024 | 1029  (= 1024 payload + 5 navigation bits) |
  | 2048 | 2053  (= 2048 + 5) |
  | 8192 | 3206  (whole scan fits; no chunking at all) |

  The real bound is `MPS3_XVC_ACCEPT_VECTOR_BITS` = `MPS3_XVC_ACCEPT_RATIO` x
  the advertised size (4x, the ratio Xilinx's own XAPP1251 `xvcServer.c` runs
  at — it advertises 2048 but accepts up to 8192 bits). Above *that*, or a zero
  `num_bits`, is a hostile/desynced stream and the connection is dropped —
  never truncated, because a short TDO reply reads to the client as real
  captured data.

  This paragraph previously claimed 2048 "bounds every legitimate hw_server
  `shift:`". That was false and it was the defect: Identify died on its first
  real scan with the opaque `Couldn't shift do data from xvcServer`.
  `firmware/test/test_xvc_identify_stream.c` now replays the real 567-byte
  client stream, and its `_ratio1` sibling is the negative control that
  reproduces the old failure.
- Vectors are LSB-first per byte (XVC convention). `shift` is the only
  command that touches hardware.
- `settck` is stored + echoed only, on **both** targets: the AXI-to-BSCAN
  bridge exposes no TCK divider for firmware to program (the AXI-side shift
  runs at the bridge's own pace), matching the Xilinx reference server
  behavior on this IP; and the bit-bang path's TCK period is set by AXI-write
  pace, not by a divider (its knob is `MPS3_XVC_SWDBB_TCK_STRETCH`, below).

  **On the IICE RM there is a real TCK ceiling and `settck` cannot enforce it.**
  The chain runs at the speed of its slowest TAP, and Identify auto-constrains
  its soft TAP to a 250 ns period (*"The JTAG clock in the instrumentation logic
  need only run at 4 MHz"*, `$SYNPLIFY_HOME/lib/share/synthesis/syn.sdc`).
  `host/openocd/nanosoc_iice_chain.cfg` caps `adapter speed 4000` for the
  OpenOCD path; on this one the knob is `MPS3_XVC_SWDBB_TCK_STRETCH`. The
  firmware bit-bang is far slower than 4 MHz anyway (3 AXI accesses per bit), so
  measure before reaching for it — see "The one board-day hazard" below.

## Register block — offsets CONFIRMED (2026-07-09)

`shell-regmap.md` v0.1/v0.2 assigns `MPS3_DBGBR_BASE` = 0x44A8_0000
("standard Debug Bridge layout — no custom fields", must be in static).

The Debug Bridge IP ships **no `_hw.h`** for Vitis to generate, so these five
offsets were carried as "the PG245/XVC-reference pattern" and flagged
*ASSUMED-PENDING-BRING-UP — confirm against a live getinfo/shift round-trip
before trusting TDO data*.

**That confirmation did not need a board.** The layout is not ours to guess: it
belongs to Xilinx's own AXI-to-BSCAN reference, XAPP1251
(`Xilinx/XilinxVirtualCable`, `jtag/zynq7000/XAPP1251/src/xvcServer.c`):

```c
typedef struct {
  uint32_t  length_offset;   /* 0x00 */
  uint32_t  tms_offset;      /* 0x04 */
  uint32_t  tdi_offset;      /* 0x08 */
  uint32_t  tdo_offset;      /* 0x0C */
  uint32_t  ctrl_offset;     /* 0x10 */
} jtag_t;

ptr->length_offset = 32;  ptr->tms_offset = tms;  ptr->tdi_offset = tdi;
ptr->ctrl_offset = 0x01;
while (ptr->ctrl_offset) { }      /* GO self-clears on completion */
tdo = ptr->tdo_offset;            /* read ONLY after it clears    */
```

Our offsets, our `CTRL` bit0 = GO semantics, and our per-chunk sequencing all
match. `test_dbgbr_xapp1251_conformance()` pins **both the offsets and the
order** by recording the register access trace: a refactor that kicks `CTRL`
before staging `TDI`, or reads `TDO` before `GO` self-clears, fails the test
instead of latching garbage on silicon. Both violations are negative-controlled.

One deliberate deviation: our completion poll is **bounded and fail-closed**
(then `-1`), where XAPP1251 spins in an unbounded `while`. A dead or absent
bridge must not hang the shell's superloop.

That bound is now a **duration**, not an iteration count. It was
`XVC_DBGBR_POLL_BOUND = 100000` iterations, which is
`(loop body cycles / AXI clock)` seconds — a different wait on every clock and
every `-O` level, and nothing in the tree could state what it was worth. It is
`MPS3_XVC_DBGBR_SETTLE_TIMEOUT_US` (200 µs) in `xvc_server.h`, microseconds off
the same free-running AXI timer the superloop budgets are measured with
(`firmware/common/service.h`). `test_dead_bridge_gives_up_on_time()` asserts the
duration, not just the `-1`.


## The bit-bang target — the Identify/IICE path

> **HISTORICAL SECTION HEADING.** This was written as "SWDBB target
> (`-DMPS3_XVC_TARGET_SWDBB`)" and everything below describes the bit-bang loop,
> which is unchanged. What changed is the block it talks to: `swd_bb` is gone
> and `jtag_bb` sits at the same page with the same bits. Read `SWDBB` below as
> "the bit-bang target"; build it as `XVC_TARGET=jtagbb`.

### Why

An Identify IICE lives *inside* the DFX reconfigurable module, and a `BSCANE2`
is illegal in a reconfigurable partition (plan §2, `HDPR-16`). Identify's
`device jtagport soft` answer puts a **soft TAP in user logic** with four plain
RTL ports. Those four wires need to reach the shell — and the shell already has
a 3-out/1-in register-driven pin wiggler on four dedicated RP partition pins
with DFX-decoupler support.

| JTAGBB | Off | Bit | Partition pin | Carries |
|---|---|---|---|---|
| `DRIVE`  | 0x00 rw | [0] | `jtag_tck`   | **TCK**, shared by both chained TAPs |
| `DRIVE`  | 0x00 rw | [1] | `jtag_tms`   | **TMS**, shared by both chained TAPs |
| `DRIVE`  | 0x00 rw | [2] | `jtag_tdi`   | **TDI**, head of the chain |
| `SAMPLE` | 0x04 ro | [0] | `jtag_tdo`   | **TDO**, tail of the chain |

`jtag_bb.sv` is deliberately dumb — "register state wired straight to the
partition pins, plus one synchronized sample path back" — so it neither knows
nor cares that the wire-set now carries two TAPs. The firmware masks are
**derived** from the names in `platform_regs.h` (`XVC_JTAGBB_TCK` =
`JTAGBB_DRIVE_TCK`, …), never retyped as 1/2/4: the §I22 lesson.

**HISTORY:** the pre-2026-09-10 design had the RM *reinterpret* the four `swd_*`
pins as the soft TAP's TCK/TMS/TDI/TDO, taking them away from the DUT's SW-DP
for the life of the bitstream. That group no longer exists and there is no spare
wire-set to reinterpret; the RM chains instead.

### ⭐ Deployment property — this is why the SWDBB target is worth having

Selecting SWDBB changes **only this firmware**. The shell BD, the partition
boundary (still 35 ports — `pin_check.py` unchanged) and the routed static
design are untouched, so shipping it is a **firmware re-bake +
`updatemem` into the existing `.bit`**:

- it does **NOT** re-mint `static_id` (whatever the shell currently is —
  `docs/FIELDED_SHELL.md` is the one authority; `0x0EE58A4D` was the shell of the
  day when this was written and has been superseded twice since), and
- it does **NOT** invalidate the nine shipped overlay partials keyed to it.

Contrast `docs/planning/JTAG_UART_REMINT_PLAN.md`, which *replaces* `swd_bb_0`
with a `jtag_bb_0` block: that is a static rebuild and a re-mint. Reusing the
pins instead of rewiring them is the whole point.

### Bit order and TDO sampling phase — the one thing that must be right

IEEE 1149.1: the TAP samples TMS/TDI on the **rising** edge of TCK and updates
TDO on the **falling** edge. While TCK is low the TAP is therefore already
presenting the bit the *upcoming* rising edge will shift out. Per JTAG bit the
firmware emits exactly:

```
write DRIVE = {TDI, TMS, TCK=0}    # settle TMS/TDI; the falling edge that
                                   # updates the TAP's TDO for THIS bit
read  SAMPLE                       # <-- TDO for THIS bit
write DRIVE = {TDI, TMS, TCK=1}    # rising edge: TAP consumes TMS/TDI
```

plus one final DRIVE write with TCK clear (the **park**), so TCK idles low and
the next shift's first write cannot be a spurious edge. That is `3n+1`
accesses for `n` bits.

This is byte-for-byte OpenOCD's `bitbang.c` scan loop and byte-for-byte the
host-side reference, `host/socket_harness/xvc_server.py`'s
`SwdbbBackend.shift_drive_sample()`. **Sampling after the rising edge instead
yields `IDCODE >> 1`** — an entire trace off by one bit, with no error reported
anywhere. Vectors are LSB-first within each byte in both directions, the same
convention as the DBGBR path and XAPP1251.

`swd_bb.sv`'s 2-FF synchronizer on `SAMPLE` is invisible here: 2 shell clocks
(~20 ns at 100 MHz) against a whole AXI-Lite read between the falling edge and
the sample.

### What the tests prove

`firmware/test/test_xvc_server_swdbb.c` builds **three** binaries from one
source (the compile-time behaviour cannot be a runtime branch — same pattern as
`test_swap_qspi_free` / `_tinyarena`):

| Binary | Config | Asserts |
|---|---|---|
| `test_xvc_server_swdbb` | — | a real IEEE-1149.1 **IDCODE read comes back** through the production bit-bang loop (2,176 checks) |
| `test_xvc_server_swdbb_latesample` | `-DMPS3_XVC_SWDBB_SAMPLE_LATE=1` | **NEGATIVE CONTROL**: the inverted phase recovers `IDCODE >> 1`, not `IDCODE`, and cannot reproduce the host's TDO (2,189) |
| `test_xvc_server_swdbb_stretch` | `-DMPS3_XVC_SWDBB_TCK_STRETCH=2` | a stretched half-period still makes **exactly one edge per bit** and returns the identical TDO (2,224) |

...and `firmware/test/test_xvc_posix_loopback.c` builds **two** more from a second
source, which drive the identical engine over a **real loopback TCP socket**
(`posix_net_if.c`) instead of `fake_net_if.c`'s in-memory rings — the same
IDCODE read, Identify's real 2,053-bit shift, single-client refusal and
fail-closed paths, but through real accept/segmentation/EAGAIN/FIN:

| Binary | Config | Asserts |
|---|---|---|
| `test_xvc_posix_loopback` | — | IDCODE `0x6BA00477` round-trips over a kernel socket (91 checks) |
| `test_xvc_posix_loopback_latesample` | `-DMPS3_XVC_SWDBB_SAMPLE_LATE=1` | **NEGATIVE CONTROL**: `IDCODE >> 1` (99) — so "it came back over a socket" cannot degenerate into "the socket works" |

That pair is the self-checking, tool-free regression on exactly the stack
`bin/xvc_fw_daemon` exposes to the real debugger, so whatever an Identify session
proves is re-proved on every `make check`. The 1149.1 TAP + SWDBB register model
they share with the fake-net suite and the daemon lives in
`firmware/test/fake_jtag_tap.c` (extracted 2026-07-30 — one model, three
consumers; deliberately NOT parameterised by either compile-time knob, see its
header).

The headline is the IDCODE read: a 41-bit shift derived from the 1149.1
controller graph (5×TMS=1 → TLR, which selects IDCODE; → Capture-DR →
Shift-DR; 32 shift bits, exit on the last) driven into an in-memory TAP behind
a `mock_regs` hook that reproduces `swd_bb.sv` exactly — DRIVE keeps only
[2:0], SAMPLE is read-only, and the TAP's edge is derived from DRIVE[0] going
0→1, the only way the real RM can see an edge either.

Also pinned: the exact ordered register trace per bit, `3n+1` accesses (and
`(3+2·stretch)n+1` when stretched), the 2048-bit ceiling both directly and over
the wire, `init` parking TCK low, no access outside the two mapped offsets,
back-to-back shifts making no spurious edge, and the framing/gating/fail-closed
paths on this target. Two independent falsifications were run during
development: inverting the production sampling phase, and flipping the
production TMS/TDI unpacking to MSB-first — **each makes the positive binary
abort at the first IDCODE check.**

**Cross-implementation lock — byte-for-byte, not prose.** A chain proven with
the host server and then run under firmware must agree exactly, so two tests
pin them together:

- `test_pin_reuse_matches_the_host_server()` — the four masks, the base, the
  offsets and the 2048-bit ceiling all equal `socket_harness.xvc_server`'s.
- `test_vectors_are_byte_identical_to_the_host_server()` — the *bytes*. The
  Python side's `idcode_read_sequence()` + `bits_to_vector()` serialise the
  same scan to `TMS = 5F 00 00 00 00 01`, `TDI = 00×6`, and its
  `SwdbbJtagShifter` returns `TDO = 00 EE 08 40 D7 00`. This test carries those
  as goldens and asserts the C helper produces the identical request vectors
  **and** that the firmware bit-bang returns the identical TDO. Two independent
  derivations of the 1149.1 navigation must serialise identically, or the
  LSB-first convention has drifted on one side.

Both falsifications above (inverted phase, MSB-first unpacking) now fail at
*this* golden — the earliest and least ambiguous place to catch them.

### Throughput

`3n+1` AXI accesses per shift, but **local** ones. At the shell's 100 MHz with
a MicroBlaze AXI-Lite peripheral access in the ~10–20-cycle range, one JTAG bit
(2 writes + 1 read) is ~300–500 ns ⇒ **~2–3 Mbit/s of TCK**, and a maximal
2048-bit shift is ~0.7–1 ms of local work plus **one** TCP round trip. A
1024-sample × ~70-probe-bit IICE download (~72 kbit, ~36 maximal shifts) is
therefore **tens of milliseconds**.

The host-side server (`host/socket_harness/xvc_server.py`) runs the *same*
`3n+1` accesses but each one crosses `xsdb` → `hw_server` → MicroBlaze MDM at
1–5 ms, i.e. 6,145 remote accesses ⇒ 6–31 s per shift and 4–19 min per trace.
The whole gain here is **locality**, not algorithm.

#### MEASURED, board-free (2026-07-31): the engine's own ceiling

The paragraph that used to sit here said "**none of this is measured**". The
board-free half now is. `firmware/test/xvc_loopback_throughput.sh` runs
`bin/xvc_throughput` — an XVC 1.0 **client**, deliberately sharing no source
with this module — against `bin/xvc_fw_daemon`, i.e. **this file** on a real
kernel socket with the SWDBB page routed to the in-memory 1149.1 TAP:

| shift shape | payload bits | wall clock | **bit/s** | vs 340 bit/s |
|---|---|---|---|---|
| 2053 × 101 — *Identify's real shape, the baseline's own count* | 207,353 | 0.071 s | **2,908,724** | 8,555× |
| 8192 × 64 — the accept ceiling | 524,288 | 0.081 s | **6,456,100** | 18,989× |
| 41 × 500 — round-trip dominated | 20,500 | 0.173 s | **118,832** | 350× |
| `--trace 923:69` — a real IICE download | 65,696 | 0.017 s | **3,811,413** | 11,210× |

Accounting is **identical to the 340 bit/s baseline**: payload `num_bits`
summed over wall clock, handshake and warmup excluded, one TCP round trip per
shift included, no client think-time. Per-shift median latency at 2053 bits is
0.59 ms; mean cost per bit at the ceiling shift is 0.155 µs.

**The measurement carries its own correctness gate.** The reply length is
checked to be exactly `ceil(num_bits/8)` on every shift, and the driver
cross-checks the client's bit total against the *server's* TCK rising-edge
counter: **842,515 bits requested = 842,515 edges clocked = 842,515 SAMPLE
reads, with 1,685,736 DRIVE writes (= 2× edges + 706 parks) and 0 stray
accesses.** So the rate describes *shifting*, not accepting. A run at 2053 bits
completing at all is also a re-proof of the accept-ceiling fix, since that is
precisely where the pre-fix defect drops the connection.

**What this bounds and what it does not.** Present: the whole protocol engine,
the `3n+1` access loop, one real TCP round trip per shift. Absent: **MicroBlaze
AXI-Lite timing** — the accesses land on memory here, not a bus — plus lwIP and
the rest of the shell superloop. Locality, the entire reason this target exists,
is a property of the real bus and is *not* measured. So this is an **upper
bound**, and the board number must come out below it.

What it genuinely establishes: **the engine's own software and TCP overhead is
not the limiter.** At 0.155 µs/bit the parse/stage/reply path is ~2–4× cheaper
than the 300–600 ns/bit the AXI estimate above predicts, so the board result
should be set by AXI and land in the estimated 1.7–3.3 Mbit/s — which would make
the repo's "roughly 1000×" claim **conservative by about 5–10×**, not optimistic.
The small-shift row is the honest caveat: at 41 bits the round trip dominates and
the rate collapses to 119 kbit/s, so the win depends on the client using large
shifts. Identify does (2053).

**Still board-only:** the actual silicon number, and therefore the ~1000× claim
itself. `firmware/platform/board_xvc_throughput.sh` is the one-command board step
that closes it — it runs the *same* `bin/xvc_throughput` against port 2542, so
the two numbers are directly comparable.

### ⚠ The one board-day hazard: TCK could be too FAST

Identify's soft TAP is auto-constrained to a **250 ns TCK period**
(`/eda/…/fpga/lib/share/synthesis/syn.sdc:4`). The bit-bang period is set by
AXI-write pace and `settck` **cannot** slow it down. If MicroBlaze AXI-Lite
turns out faster than ~125 ns per access, the free-running loop would
*overclock the TAP* — the failure mode being flaky or garbage scans rather than
a clean error.

The mitigation is `MPS3_XVC_SWDBB_TCK_STRETCH` (default `0`): extra
**idempotent** DRIVE re-writes per phase. `DRIVE` is write-through with no side
effects, so re-writing the same value cannot create a TCK edge (only 0→1 on
bit 0 does) — it only costs an AXI transaction. Raising it is the RTL-free way
to slow TCK down, and `test_xvc_server_swdbb_stretch` proves a stretched shift
still yields exactly one edge per bit and identical TDO. **Measure the period
on the first board session and set this accordingly.**

### ⚠ Register contention with `jtag_server`

`firmware/jtag_server/jtag_server.c` drives the very same `DRIVE` register for
OpenOCD `remote_bitbang` on port **6921**, and both servers are linked into the
shell image. The operational rule is **one client at a time**: an Identify
session on 2542 and an OpenOCD session on 6921 would interleave writes to one
3-bit register and corrupt both scans. Nothing here arbitrates it.
`xvc_server_init()` parks `DRIVE = 0` on this target, which is also
`jtag_bb.sv`'s own `3'b000` reset state — and parking TCK LOW matters more on a
chain than on one device, because a phantom first edge desynchronises *every*
TAP on the wire-set at once.

**Note what the chain changed and what it did not.** The old rule here cited
plan §3 — *"SWD access to the M0 is unavailable while Identify owns the pins"* —
because the RM was wired one way at a time. That cost is **gone**: the RM
daisy-chains the two TAPs, so both are permanently reachable and neither
displaces the other. What remains is narrower and still real — the two SERVERS
share one register. The contention is now about bus ownership, not about which
TAP is wired up.

(The legacy `swd_server` on 6920 is no longer built into the image at all —
`firmware/platform/Makefile`, `LEGACY_SWD`.)

### Selecting it in a real firmware build

`firmware/test/`'s host-gcc harness passes the flag itself. The **target** build
gate now EXISTS — `firmware/platform/Makefile`'s `XVC_TARGET ?=` — so this is
just `XVC_TARGET=jtagbb` on the `make elf` line (`swdbb` still works and emits
identical code; see "SWDBB vs JTAGBB" at the top). An unrecognised value is now
a hard `$(error)` rather than a silent fall-back to DBGBR. **A real bit-bang
shell image has been built and statically gated** (2026-07-31, then as `swdbb`);
the exact, verified invocation —
including the two flags whose omission is silent on silicon and the overlay
identity that must be reconstructed for a re-bake into an older `.bit` — is in
`firmware/platform/README.md` under "Firmware-only re-bake into an EXISTING
bitstream".

The short version, as run for the then-shipped `0x0EE58A4D` static (`PRODUCT=1`
now names the fielded flag set; the current static is in `docs/FIELDED_SHELL.md`):

```sh
make -C firmware/platform clean WS=$SC/vitis_fw
make -C firmware/platform elf CLCD=1 CLCD_KVM=1 HWICAP_FIFO=1 WINDOWED=1 \
     XVC_TARGET=swdbb WS=$SC/vitis_fw \
     BLOB_MANIFEST=... BLOB_BIN=...      # the manifest for THAT bitstream
```

`make clean` first is not optional: make compares timestamps, not flags, so a
stale `xvc_server.o` survives adding `XVC_TARGET=swdbb` and you get a
byte-identical ELF with the retarget silently absent.

- `firmware/harness_app/build.tcl` (the Vitis/`importsources` path) still has no
  per-file define hook; add `-DMPS3_XVC_TARGET_SWDBB` to the app's build
  settings there if that flow is used.

Both targets are compile-verified against the real-MMIO HAL (not just the host
mock). The SWDBB build's `.text` is 1,446 B vs DBGBR's 1,568 B — the retarget
is **smaller**, so there is no LMB-footprint objection. Confirmed at whole-image
scale on the real cross-compile: the SWDBB `shell_fw.elf` `.text` is 173,268 B
against the DBGBR build's 173,400 B, i.e. **132 B smaller**.

**Verifying the flag took effect in a target build** — because "make printed the
define" is not evidence. MicroBlaze materialises a 32-bit address as
`imm <high16>`, so `0x44A7` (SWDBB) is `imm 17575` and `0x44A8` (Debug Bridge) is
`imm 17576`. `swd_server.c` also drives SWDBB, so the discriminator is that
**nothing else in the image touches `0x44A8`**:

| build | `0x44A7` SWDBB sites | `0x44A8` DBGBR sites |
|---|---|---|
| `XVC_TARGET` unset | 2 (swd_server only) | **5** |
| `XVC_TARGET=swdbb` | 5 | **0** |

`firmware/platform/verify_shell_image.py --expect-xvc-target swdbb` asserts
exactly that, and was negative-controlled against the non-SWDBB image.


## Equivalence with the host server (what `updatemem` deployment rests on)

`host/socket_harness/xvc_server.py` and this file are meant to be
interchangeable behind one client. As of the accept-ceiling fix they agree on
every protocol-visible behaviour:

| behaviour | agreed value |
|---|---|
| `getinfo:` reply | `xvcServer_v1.0:2048\n`, byte-identical (asserted in both suites) |
| accept ceiling | 8192 bits (4 x advertised) on both |
| `num_bits` 0 or > ceiling | reject, drop the connection, no register access |
| TDO reply length | `ceil(num_bits/8)`, never truncated |
| pad bits of a partial final byte | zero-filled |
| `settck:` | stored, echoed unchanged, no hardware touched |
| non-XVC prefix | fail closed, never resync-guess |
| TCP fragmentation | accumulate-and-reevaluate; both proven at 7-byte fragments |
| vector bit order | LSB-first within each byte, both directions |

Both are pinned against the **same recording** of real Identify traffic: the
567-byte capture in `firmware/test/test_xvc_identify_stream.c` and in
`host/socket_harness/tests/test_xvc_server.py` (`IDENTIFY_COM_CHECK_RX`), with a
host test parsing the C array back out to assert the two never drift apart.

Three differences remain, all deliberate, none protocol-visible to Identify:

1. **Gating.** Only this firmware stalls a pending `shift:` while
   `g_shell_state.xvc_gated` (see below). The host server has no swap to gate
   against.
2. **Second client.** This firmware accepts an extra connection and closes it
   immediately; the host server leaves extras in the listen backlog. Identify
   opens one connection, so neither path exercises this.
3. **Speed** — the reason the SWDBB retarget exists. ~100 ns per local AXI
   access here versus 1–5 ms per `xsdb` -> `hw_server` -> MDM access on the host
   path, i.e. roughly a 0.6 ms versus 6–30 s round trip for one 2053-bit shift.
   The asymmetry runs in the firmware's favour; the host path is the one at risk
   of a client-side cable timeout on large scans.

### Proven by execution (2026-07-30) — real Identify, no board

The paragraph that used to sit here said the real client "has never been run
against *this* server, because that needs the board." That was wrong: it needs a
socket, not a board. `firmware/test/bin/xvc_fw_daemon` links **this** file
(-DMPS3_XVC_TARGET_SWDBB, never a copy) against `firmware/test/posix_net_if.c` —
real POSIX sockets behind `common/net_if.h` — with the SWDBB page routed to the
in-memory 1149.1 TAP in `firmware/test/fake_jtag_tap.c`.
`host/identify/fw_com_check.sh` starts it and points `identify_debugger_shell`
(T-2022.09-SP2) at it via `host/identify/com_check.tcl`.

Measured result, greppable from the debugger's own log:

```
XVC connection to 'xvcServer_v1.0' established on host '127.0.0.1'
Checking communication with the XilinxVirtualCable cable and the hardware...
The hardware is responding correctly.
Auto-detecting the device chain...
Warning: Device at position 1 is not in the device database
         (IDCODE is 01101011101000000000010001110111)      <- 0x6BA00477
DI149    Assuming device at position 1 has IR length of 4
Checking Hardware ID ...
Error:  regular  not found  IICE_CPU  0x942c3a7e
        unknown  dev_0_UNKNOWN  unknown  0x00000000
```

and from the daemon: **11 shift bursts, 6,567 TCK rising edges, 6,567 SAMPLE
reads, 13,146 DRIVE writes, 0 stray accesses** — including **two 2,053-bit
shifts**, i.e. the 524-byte command that the pre-fix `XVC_CMD_BUF_MAX` of 522
could not hold. The accept-ceiling fix is therefore EXECUTED against the real
client, not inferred from a replay.

**With a system-level negative control.** `bin/xvc_fw_daemon_ratio1` is the same
daemon with `-DMPS3_XVC_ACCEPT_RATIO=1u`, restoring the pre-fix defect. Real
Identify FAILS against it: only the two small navigation shifts are served, then
`Error: Connection reset by peer` at the cable check, no chain, no IDCODE. Note
the wording differs from the HOST server's failure for the same defect
(`Couldn't shift do data from xvcServer`), because the firmware fails closed by
dropping the TCP connection rather than answering.

**What this does NOT establish.** The TAP is a model, so there is no IICE behind
it and the session stops at "Checking Hardware ID" with signature `0x00000000` —
that stop is expected and is not an IICE read. Nothing here measures MicroBlaze
AXI timing, and nothing here proves the RM reinterprets the four SWD pins as
TCK/TMS/TDI/TDO. Still board-only: those two, and the speed claim below.

**Trap found while building the gate.** `com check` does **not** raise a Tcl
error when it fails — it returned 0 both in the passing run and in the run that
died at "Connection reset by peer" with no chain and no IDCODE. Any guard shaped
like `if {[catch {com check} err]} { error ... }` is therefore unreachable; the
only reliable signal is the debugger's own log text.


## Gating during a swap — stall, not error

Per ARCHITECTURE_SPEC §10 "XVC can't detect overlay replacement -> gate
during reconfig": `coordinator/swap_fsm.c`'s `SWAP_GATE` sets
`g_shell_state.xvc_gated`. While gated:

- `getinfo:`/`settck:` are still answered (harmless, no hardware touched)
  so a mid-swap handshake doesn't wedge;
- a completed `shift:` **stalls un-consumed** in the command buffer — no
  register access on either target, no reply — and is serviced on the first
  ungated poll.

Stall (not an error reply) was chosen because hw_server treats a short or
failed reply as a dead cable and tears the session down, while a slow TCP
read just looks like latency; a swap completes well inside hw_server's
timeouts. This resolves the stall-vs-error question the stub flagged for
A6/A4 — revisit only if a real hw_server session is observed to time out
across a swap. Per §6.3 the host reloads the new RM's `.ltx` and re-scans
after a swap — this module has no `.ltx` awareness, it only shifts bits.
