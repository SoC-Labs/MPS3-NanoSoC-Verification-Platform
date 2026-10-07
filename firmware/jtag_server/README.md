# jtag_server/

> **STAGED / UNVALIDATED scaffolding (2026-07-28) — NOT COMPILED.** "Test-
> tomorrow" ground-work: the JTAG analogue of `firmware/swd_server/`. A
> one-for-one mirror of `swd_server.c` retargeted to OpenOCD's `remote_bitbang`
> **JTAG** protocol and the (also-scaffold) `JTAGBB` pin-wiggler block
> (`fpga/shell/ip/jtag_bb`, `0x44B1_0000`). It has **not** been built. See
> `docs/planning/MPS3_JTAG_DEBUG_DESIGN.md` for the full design and the
> proven-vs-unproven ledger.

TCP server on port **6921** implementing OpenOCD's `remote_bitbang` **JTAG**
byte protocol, toggling the JTAG partition pins into nanosoc's SWJ-DP (JTAG
mode). This is the **Option A** (software bit-bang, buildable-now) DUT-debug
path — OpenOCD owns all JTAG protocol logic host-side; this module only
sets/samples raw signal levels. **The firmware IS the JTAG engine.**

The **Option B** hardware `axi_jtag` shifter (`0x44AF_0000`, faster) would
replace this whole path but is BLOCKED on the encrypted IP's host driver (see
the design note §2). The two are mutually-exclusive (one DUT SWJ-DP).

## Protocol (OpenOCD remote_bitbang, JTAG side)

```
0..7 = write {TCK,TMS,TDI} = bits [2:1:0] of (c - '0')
R    = sample TDO           (-> reply ASCII '0'/'1')
r/s/t/u = {trst,srst} combos    (bits [1:0] of (c - 'r'))
B/b  = LED on/off
Q    = quit
```

The encoding is owned by the *host driver*, so it is verifiable by reading it
(the exact method that closed the SWD half of I22 without a board). OpenOCD
`src/jtag/drivers/remote_bitbang.c`:

```c
static int remote_bitbang_write(int tck, int tms, int tdi)
{   char c = '0' + ((tck ? 0x4 : 0x0) | (tms ? 0x2 : 0x0) | (tdi ? 0x1 : 0x0)); }
static int remote_bitbang_reset(int trst, int srst)
{   char c = 'r' + ((trst ? 0x2 : 0x0) | (srst ? 0x1 : 0x0)); }
static enum bb_value char_to_int(int c)      /* the reply to 'R' */
{   case '0': return BB_LOW;  case '1': return BB_HIGH;
    default: remote_bitbang_quit(); ... return BB_ERROR; }
```

1. **TCK = bit2, TMS = bit1, TDI = bit0** of `(c - '0')` (chars `'0'..'7'`).
2. **TRST = bit1, SRST = bit0** of `(c - 'r')`. This design carries **no TRST
   wire** across the RP boundary (the RM straps `ntrst=1` — design note §1.3),
   so `trst` is ignored and only `srst -> CLKRST.dbg_resetn` is meaningful —
   the *same pin the SWD path uses today*. Host cfg should set
   `reset_config srst_only`.
3. The reply to `'R'` must be **ASCII `'0'`/`'1'`** — any other byte makes
   OpenOCD log an error and close the connection.

The reset (`r/s/t/u`), blink (`B/b`) and quit (`Q`) chars are **byte-identical**
to the SWD protocol, so that handling is copied verbatim from `swd_server.c`.
Only the drive/sample half differs.

**TODO(A5):** `firmware/test/test_jtag_server.c` with a
`test_openocd_remote_bitbang_jtag_conformance()` that **re-derives** each byte
from the formulas above (never hardcodes letters), mirroring
`test_swd_server.c` — so an inverted `{TCK,TMS,TDI}` order fails a host-gcc
unit test, not a bring-up.

## Register block (JTAGBB — scaffold)

`fpga/shell/ip/jtag_bb` @ `0x44B1_0000`: `JTAGBB_DRIVE` (`_TCK`/`_TMS`/`_TDI`)
and `JTAGBB_SAMPLE` (`_TDO`, read-only). Defined **locally** in
`jtag_server.h` for now, because `platform_regs.h` is a frozen contract header
(READ-ONLY for this scaffold) — **TODO(A6):** fold `MPS3_JTAGBB_BASE` + the
`JTAGBB_*` offsets into `platform_regs.h`, and `MPS3_PORT_JTAG` (6921) into
`net_proto.h`, alongside the SWD equivalents, when this block is really
integrated. `CLKRST.RESET_CTRL.dbg_resetn` (srst) is the separate CLKRST block,
exactly as in the SWD path.

## Gating during a swap

Mirror `swd_server`: `jtag_server_poll()` must stop toggling pins while the RP
is decoupled / held in reset during a reconfiguration. **TODO(A6):** reuse
`g_shell_state.swd_gated` (correct — SWD and JTAG are mutually-exclusive, so a
single DUT-debug gate covers both) or add a parallel `jtag_gated`. Un-gated
again in the swap FSM's release state.

## Host front-end

`host/openocd/nanosoc_mps3_jtag.cfg` (already authored by a sibling — **not
edited here**) drives this server directly via its `rbb` branch:

```
openocd -c "set TRANSPORT_MODE rbb" -c "set RBB_HOST 192.168.10.101" \
        -f host/openocd/nanosoc_mps3_jtag.cfg \
        -c init -c "nanosoc_halt_examine" -c shutdown
```

`RBB_PORT` defaults to 6921, matching this server — **no cfg edit needed** for
Option A (design note §4). Expect JTAG TAP IDCODE `0x6ba00477` (not the SWD
DPIDR `0x0bb11477`) — confirm at bring-up.

## Integration (OWED — not done by this scaffold)

Add to the coordinator superloop and build exactly like `swd_server` (design
note §6): `jtag_server_init()` at `coordinator.c:93`, `jtag_server_poll()` at
`coordinator.c:124` / `platform/src/main.c:239` / `harness_app/main.c:519`,
`$(FW)/jtag_server/jtag_server.c` in `platform/Makefile` `SRCS`, and stubs in
`firmware/test/fake_services.c`. Because JTAG and SWD are mutually-exclusive,
gate one out per build rather than running both.
