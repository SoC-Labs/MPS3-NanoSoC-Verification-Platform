# `host/identify/` — Synopsys Identify (IICE) debug of the MPS3 DFX RM

Host-side collateral for reading an Identify IICE that lives **inside** the MPS3
DFX reconfigurable module, over a soft JTAG TAP, through four registers the shell
already ships — **no new partition pins, no shell rebuild, no `static_id`
re-mint**.

Design and feasibility: `docs/planning/IDENTIFY_IICE_DFX_PLAN.md` (§2 the
`BSCANE2` blocker, §3 this communication path). Frozen cross-stream contract:
`tests/identify_iice/INTERFACES.md`.

```
identify_debugger_shell    debug_session.tcl  (capture)  |  com_check.tcl (cable only)
        |   com cabletype XilinxVirtualCable ;  server set -addr H -port P
        v
   TCP, XVC 1.0  (getinfo: / settck: / shift:, 2048 advertised / 8192 accepted)
        v
   ONE of THREE servers, all speaking the same wire protocol:
        |
        +-- host/socket_harness/xvc_server.py --fake-tap    board-free, EXECUTED
        |        in-process FakeJtagTap                     (host tooling proof)
        |
        +-- firmware/test/bin/xvc_fw_daemon                 board-free, EXECUTED
        |        the REAL firmware/xvc_server/xvc_server.c  (firmware proof;
        |        + posix_net_if.c + fake_jtag_tap.c          ./fw_com_check.sh)
        |
        +-- host/socket_harness/xvc_server.py               NEEDS A BOARD
                 ONE persistent `xsdb` -> hw_server tcp:<hub-host>:3121
                 ...or, on silicon, the same xvc_server.c running IN the shell
        v
   JTAGBB @ 0x44A7_0000  DRIVE[0]=TCK  DRIVE[1]=TMS  DRIVE[2]=TDI  SAMPLE[0]=TDO
        |   the four jtag_* DUT partition pins (fpga/shell/boundary.yaml)
        v
   a TWO-TAP 1149.1 DAISY CHAIN inside the RM:
        TDI -> [Identify soft TAP, IR 5, id 0x1063E4CD]
            -> [SoC-400 SWJ-DP,    IR 4, id 0x6BA00477] -> TDO
        |   (docs/planning/IICE_JTAG_CHAIN.md)
        v
   RM soft TAP (`device jtagport soft`)  ->  IICE  ->  sample buffer
        v
   write fsdb  ->  hw_iice.fsdb  ->  mangle to the flat iice/* scope  ->  nCompare
```

---

## STATUS — read this before trusting anything here

**Plan gate R0 is CLOSED: the `identdebugger` seat EXISTS and has been checked
out** (`License checkout: identdebugger_prdp` from the site licence server, 100 seats).

**Nothing in this directory has been executed against HARDWARE.** What has been
executed, board-free, on 2026-07-30:

| Server behind the debugger | Result |
|---|---|
| `../socket_harness/xvc_server.py --fake-tap` (host, Python) | `com check` reaches *"The hardware is responding correctly"*, chain autodetects IDCODE `0x6BA00477`, IR 4; stops at *Checking Hardware ID* (no IICE behind the model TAP). This is how the getinfo-is-a-chunk-hint defect was found. |
| `firmware/test/bin/xvc_fw_daemon` (the REAL firmware C engine, `-DMPS3_XVC_TARGET_SWDBB`, over real POSIX sockets) | Same outcome, plus **11 shift bursts / 6,567 TCK edges / 0 stray accesses**, including two **2,053-bit** shifts (the 524-byte command the pre-fix 522-byte accumulator could not hold). Run it with `./fw_com_check.sh`. |

So the firmware XVC engine is now proven by EXECUTION against the real client,
not only by inference from unit tests — see `firmware/xvc_server/README.md`
"Proven by execution". It is gated in `make check` stage **[10/10]**, guarded on
`identify_debugger_shell` being on PATH and an instrumented `rev_1_identify/`
build existing, and the board-free tool-free half runs unguarded in stage
[5/10] (`test_xvc_posix_loopback{,_latesample}`).

**Two traps found by running it, both of which would have poisoned a gate:**

1. **`com check` does not raise a Tcl error when it fails.** It returned 0 both
   in the passing run and in a run that died at *"Error: Connection reset by
   peer"* with no chain and no IDCODE. `debug_session.tcl`'s
   `if {[catch {com check} err]} { error "com check FAILED..." }` guard is
   therefore **unreachable** — a gate that structurally cannot fail. The only
   reliable signal is the debugger's own log text, which is why
   `fw_com_check.sh` asserts by grepping it.
2. **Do not print the debugger's success wording in your own log.** An early
   `com_check.tcl` quoted *"The hardware is responding correctly."* as a triage
   hint, and the negative control then PASSED the cable-check assertion by
   matching our own line. `fw_com_check.sh` now strips every `MPS3_COMCHECK`
   line before grepping, and `com_check.tcl` no longer quotes the phrase.

What *is* verified, and how:

| Claim | Status |
|---|---|
| `debug_session.tcl` parses and its control flow works | **VERIFIED** — executed under `tclsh 8.6` against stub `project`/`com`/`server`/`chain`/`idcode`/`iice`/`run`/`write` procs. All six branches exercised: happy path, `IICE_TIMEOUT=0` refused, bad `IICE_TRIGGER_TIME` refused, `xvc_speed` rejection warns-and-continues, `com check` failure emits the triage block, manual chain declaration with and without an IDCODE. `-range {0 N}` confirmed to reach the command as ONE argument. |
| Command syntax (`com`, `server set`, `chain`, `idcode`, `iice sampler -triggertime`, `run -timeout`, `write fsdb -range`) | **VERIFIED against the vendor reference** — `/eda/…/SFPGA_2022.09-SP2/identify/doc/identify_debug_env_reference.pdf`, cited inline at each use. |
| `com cableoption xvc_speed <ns>` exists and is in nanoseconds | **VERIFIED from the binary** — `identify/linux_a_64/mbin/identify_debugger_shell` carries the help line *"xvc_speed — Specify the communication speed for the Xilinx Virtual Cable in [ns]"*. It is **absent from the 2022.09 PDF**, which is why the call is wrapped in `catch`. |
| The XVC server speaks a protocol this client accepts | **VERIFIED BY EXECUTION** for BOTH servers (host Python and firmware C) — see the STATUS table above. `settck:` echo tolerated, `getinfo:` accepted, `shift:` served up to 2,053 bits. |
| `debug_session.tcl` can be pointed at the firmware engine with no edit | **VERIFIED** — it already takes `IICE_XVC_HOST`/`IICE_XVC_PORT`. `com_check.tcl` is the new connectivity-only payload (no `run`, so it cannot hang on a missed trigger) and reads the SAME environment. |
| `nanosoc_iice_debug.prj` | **NEVER RUN.** Not by Synplify, not by the debugger. `-technology` in particular is a guess — see the file. The debugger runs above used the RM build's own `fpga/rp/nanosoc_iice/build/nanosoc_iice.prj` instead. |
| The RM actually reinterprets the four SWD pins as TCK/TMS/TDI/TDO | **OBSOLETE — that design is gone.** The `swd_*` group no longer exists in the fielded boundary and there is no spare wire-set to reinterpret: the RM now **daisy-chains** the soft TAP with the DUT's SWJ-DP on the one `jtag_*` set. Proven in simulation against the real Arm SWJ-DP (`tests/jtag_chain`, 7/7); **still not built into a bitstream**. |
| The soft TAP's IR length and IDCODE | **KNOWN, and read off the tool rather than a board.** IR = **5**, IDCODE = **`0x1063E4CD`**, from Identify's own device table `identify/lib/share/contrib/syn_idcodes.tcl:741-742` (`idcode add -quiet 00010000011000111110010011001101 SoftJTAG 5 -family soft-jtag`; `idcode add` takes `<binary MSB-first> <name> <IR width>`, `identify_debug_env_reference.pdf` p.46). Its opcodes come from `strings identify_debugger_shell`: VENDORID `00000`, HCR_CHAIN `00010`, IDHW_CHAIN `00011`, BYPASS `11111`. **Still confirm against `chain info -raw` on the first real run** — but there is now a value to compare against instead of a blank. |
| The debugger supports a multi-device chain | **VERIFIED against the vendor docs.** `chain add`/`chain clear`/`chain select`/`chain info` (`identify_debug_env_reference.pdf` p.25) and a worked **two-device** example — an IR-8 + IR-5 chain — in `identify_debugger_ug_synplify.pdf` pp.85-86. Identify computes the BYPASS/IR padding itself; there is no `ir_before`/`ir_after` knob. **Not yet run against our chain.** |
| The vendor's own docs bless soft-TAP-over-XVC | **NO.** `identify_debug_env_reference.pdf` p.140: *"If you are using the soft JTAG port, you must use either a ByteBlaster or ByteBlaster MV hardware cable."* That predates the `XilinxVirtualCable` cable type in the same release (p.28-29) and the `xvcServer`/`xvc_speed`/`-xvc_url` strings in the 2022.09-SP2 binary. The plumbing exists and `fw_com_check.sh` exercises it board-free; treat the combination as **unvalidated by Synopsys**, not as blessed. |

---

## Files

| File | What it is |
|---|---|
| `debug_session.tcl` | Batch capture script for `identify_debugger_shell`. Fully environment-driven; refuses to start a run that cannot be interrupted. Its `com check` guard is unreachable — see trap 1 above. |
| `com_check.tcl` | **Connectivity-only** payload: cable + `com check` + `chain info -raw`, then exit. No `iice`/`run`/`write fsdb`, so it cannot hang on a missed trigger and is safe against a server with no IICE behind it. Same environment as `debug_session.tcl`. |
| `fw_com_check.sh` | Runs the whole board-free firmware proof and turns it into an exit status: starts `firmware/test/bin/xvc_fw_daemon` on an ephemeral port, points the debugger at it, asserts on the DEBUGGER's own log lines — **and runs the system-level negative control** (`xvc_fw_daemon_ratio1`, the pre-fix accept ceiling, which the debugger must fail against). `--positive-only` / `--negative-only` to run one half. |
| `nanosoc_iice_debug.prj` | Synplify project template carrying the `rev_1_identify` Identify implementation. The **authoritative** copy is the one the RM build produces — the debugger needs that build's `rev_1_identify/syn.db`. |
| (server) `../socket_harness/xvc_server.py` | The XVC 1.0 server. `python3 -m socket_harness.xvc_server --help`. |

---

## Quick start

### 0. Prove the FIRMWARE engine with no hardware (one command)

```sh
make -C firmware/test tools
module load identify/2022.09-SP2
host/identify/fw_com_check.sh          # positive + negative control
```

This is the cheapest, highest-value step in the whole path: it proves the real
debugger and the real firmware XVC engine interoperate before a board window is
spent on it, and it proves the pre-fix accept ceiling really would have failed.
It needs an instrumented `rev_1_identify/` build to open (gitignored build
output) and checks out one licence seat per session.

### 1. Prove the host half with NO hardware and NO licence

The server ships an in-process JTAG TAP, so the whole client→server→TAP chain can
be exercised on a laptop:

```sh
cd <repo>
PYTHONPATH=host:host/pyverify python3 -m socket_harness.xvc_server \
    --fake-tap --port 2542 --idcode 0x6BA00477
```

Then, from a build directory that has `rev_1_identify/`:

```sh
IICE_XVC_HOST=127.0.0.1 IICE_XVC_PORT=2542 IICE_NAME=IICE_CPU \
    identify_debugger_shell <repo>/host/identify/debug_session.tcl
```

`com check` and `chain info -raw` should both come back. **This is the single
most valuable first step**: it splits "the host tooling is wrong" from "the
fabric is wrong" before a board is involved, and it is the only part of the chain
that can be tested at zero cost. (It has not been run — no licence seat.)

Preview what the real server would do, without spawning anything:

```sh
PYTHONPATH=host:host/pyverify python3 -m socket_harness.xvc_server --dry-run
```

### 2. Against the board

```sh
# One persistent xsdb, one TCP listener on the four jtag_* pins. It does NOT
# take them away from the DUT any more -- the RM chains the two TAPs -- but it
# does take the one DRIVE register, so stop any jtag_server/OpenOCD session.
PYTHONPATH=host:host/pyverify python3 -m socket_harness.xvc_server \
    --port 2542 --hw-server-url "$MPS3_HW_URL"     # tcp:<hub-fqdn>:3121

# In the build directory that holds rev_1_identify/
IICE_NAME=IICE_CPU IICE_DEPTH=1024 IICE_TRIGGER_TIME=middle \
IICE_XVC_HOST=<server host> IICE_XVC_PORT=2542 \
IICE_FSDB=hw_iice.fsdb IICE_TIMEOUT=1800 \
    identify_debugger_shell <repo>/host/identify/debug_session.tcl
```

### Environment knobs (`debug_session.tcl`)

| Variable | Default | Notes |
|---|---|---|
| `IICE_PRJ` | `./nanosoc_iice_debug.prj` | must be the project synthesis produced |
| `IICE_NAME` | `IICE_CPU` | matches `signals_nanosoc.yaml`'s `iice.name` |
| `IICE_DEPTH` | `1024` | must match the manifest; drives `-range` |
| `IICE_TRIGGER_TIME` | `middle` | must match the manifest — the offline cropper reproduces this window in the sim trace |
| `IICE_FSDB` | `hw_iice.fsdb` | output |
| `IICE_XVC_HOST` / `IICE_XVC_PORT` | `127.0.0.1` / `2542` | the debugger's own XVC default is **57015** and stored settings persist, so both are always set explicitly |
| `IICE_XVC_SPEED_NS` | `1000000` | 1 ms — honest for a millisecond-per-edge transport |
| `IICE_TIMEOUT` | `1800` | seconds; **0 is refused** |
| `IICE_CHAIN_NAME` / `IICE_CHAIN_IRLEN` / `IICE_CHAIN_IDCODE` | unset | manual chain declaration; only needed if autodetect fails |

---

## Traps

- **`run` has no default timeout and there is no `stop` command.** The vendor
  manual states it outright: *"the run command does not stop running until the
  trigger occurs. If the trigger does not occur, the run command does not stop.
  … There is no stop command in the command shell."* A missed trigger hangs the
  batch forever, holding the single XVC client and the licence seat.
  `debug_session.tcl` always passes `-timeout` **and refuses `-timeout 0`**,
  because 0 *disables* the timeout — the obvious "no limit" value is the bug.
- **Both ends are single-client.** Do not probe the XVC port with `nc`/`telnet`
  while a session is live: a bare TCP connect *is* the one client.
- **Identify and OpenOCD cannot run at the same time — but not for the old
  reason.** The old rule was "the four pins are physically the same wires the SWD
  probe uses; stop the other session or mux inside the RM". The chain removed
  that: both TAPs are permanently reachable and neither displaces the other.
  What remains is narrower and still real — `firmware/xvc_server` (2542) and
  `firmware/jtag_server` (6921) drive **the same three-bit `DRIVE` register** and
  are both linked into the shell image, so two live sessions interleave writes
  and corrupt both scans. Nothing arbitrates it. One client at a time.
  (The legacy `swd_server` on 6920 is no longer built into the image at all —
  `firmware/platform/Makefile`, `LEGACY_SWD`.)
- **Declare the chain, or auto-detect will stall on the DAP.** The SWJ-DP's
  `0x6BA00477` is **not** in `syn_idcodes.tcl`, so Identify hits *"Device at
  position %d is not in the device database"*. Either pre-register it —
  `idcode add 01101011101000000000010001110111 "ARM_SWJ-DP" 4` — or set
  `IICE_CHAIN_*` and let `debug_session.tcl` declare the chain manually.
- **Cap TCK at 4 MHz.** The chain runs at the speed of its slowest TAP and the
  instrumentor auto-constrains its soft TAP to a 250 ns period (*"The JTAG clock
  in the instrumentation logic need only run at 4 MHz"*,
  `$SYNPLIFY_HOME/lib/share/synthesis/syn.sdc`). Overclocking a soft TAP does not
  error; it returns garbage scans.
- **After any DUT reset, re-run the DP power-up.** Measured in
  `tests/jtag_chain`: a `dut_resetn` pulse does NOT disturb the chain (the TAP is
  behind `ntrst`, which the RM straps high) but it DOES clear the DP power-up
  latch, so the chain scans perfectly while every AP access is gated off. It
  presents as a deaf DAP, not as a reset.
- **Do not script `identify_instrumentor_shell`** — in 2022.09 `project open`
  silently kills script execution. Instrumentation goes through the hand-written
  `.idc` that `identify_compile` consumes (generated by
  `tests/identify_iice/gen_idc.py`).
- **The debugger needs the whole `rev_1_identify/` directory**, not just the
  `.bit`. Archive them together or you cannot open a capture session later.
- **`launch_verdi` does not exist** in the 2022.09 debugger. The flow is
  `write fsdb`, then `verdi -ssf`.
- **Keep the instrumentor and debugger on the same release train** (2022.09 with
  2022.09).
- **Throughput.** The host-side server is minutes per trace by design (3 remote
  AXI accesses per JTAG bit). That is enough to *prove* the chain and not enough
  to live with; plan §3(b) moves the bit-banging into
  `firmware/xvc_server/xvc_server.c` for ~1 Mbit/s, at the cost of a firmware
  re-bake + `updatemem` (still no re-mint).

---

## Confirm on the first run

Each of these is a real unknown, deliberately left as a question rather than a
guessed constant:

1. ~~**Is there an `identdebugger` seat?** Gate R0.~~ **ANSWERED 2026-07-30:
   YES** — `identdebugger_prdp` checked out from the site licence server, 100 seats.
2. **The soft TAP's IR length and IDCODE.** `chain info -raw`. Identify's soft
   TAP is private user logic
   (`/eda/…/fpga/lib/di/hw_gen/ip/jtag_interface_core.v`), so neither the
   KU115 device ID nor the DUT's `0x6BA00477` applies.
3. **Is `-range {start stop}`'s `stop` inclusive?** The script uses
   `DEPTH - 1`. If the FSDB comes back one sample short of `DEPTH`, use `DEPTH`.
   The compare pipeline will surface this as a length mismatch, not a silent
   pass.
4. **Does `com cableoption xvc_speed` survive?** It is confirmed in the binary
   but absent from the PDF, so the call is wrapped in `catch`. If it warns,
   nothing is lost — the transport is millisecond-per-edge regardless.
5. ~~**Does the real client tolerate a `settck:` echo?**~~ **ANSWERED
   2026-07-30: YES** — observed against both servers; `com cableoption xvc_speed
   1000000` is accepted and the session proceeds.
6. **Where did `device jtagport soft` put the four ports?** Plan risk R1. If
   they landed on the RM boundary, `make check` stage 2 (`pin_check.py`) fails
   with `EXTRA port … not in partition-pins.md` and the fix is a thin shim top,
   not a contract edit.
7. **Is `-technology KINTEXU` the token Synplify wants** for
   `xcku115-flvb1760-1-c`? `xilinx_families.info` is encrypted; the parts file
   groups the part under `target Kintex-UltraScale-FPGAs`. Record which spelling
   worked.
