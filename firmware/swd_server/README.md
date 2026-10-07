# swd_server/

TCP server on port 6920 implementing OpenOCD's `remote_bitbang` byte
protocol (net-protocol.md "SWD channel"), toggling the SWD partition pins
into nanosoc's SW-DP. This is the v1 "dumb pin-wiggler" of
ARCHITECTURE_SPEC.md §10.1 — OpenOCD owns all SWD protocol logic
host-side; this module only sets/samples raw signal levels.

## Protocol (net-protocol.md, restated)

```
O = SWDIO drive     o = SWDIO release     c = sample SWDIO (-> reply '0'/'1')
d/e/f/g = {CLK,DIO} combos
r/s/t/u = trst/srst combos
B/b = LED on/off
Q = quit
```

`common/net_proto.h`'s `mps3_bitbang_char_t` enum captures this. The two
bit-order questions that were **open** here (SWD half of I22) are now
**RESOLVED (2026-07-09) — and never needed a board.**

The encoding is owned by the *host driver*, so it was verifiable by reading it.
OpenOCD `src/jtag/drivers/remote_bitbang.c`:

```c
static int remote_bitbang_swd_write(int swclk, int swdio)
{   char c = 'd' + ((swclk ? 0x2 : 0x0) | (swdio ? 0x1 : 0x0));

static int remote_bitbang_reset(int trst, int srst)
{   char c = 'r' + ((trst ? 0x2 : 0x0) | (srst ? 0x1 : 0x0));

static enum bb_value char_to_int(int c)      /* the reply to 'c' */
{   case '0': return BB_LOW;  case '1': return BB_HIGH;
    default: remote_bitbang_quit(); ... return BB_ERROR; }
```

1. **CLK is bit1, DIO is bit0** of `(c - 'd')`.
2. **TRST is bit1, SRST is bit0** of `(c - 'r')`. SWD has no TRST wire, so this
   shell ignores it and maps only srst -> `CLKRST.dbg_resetn`.
3. The reply to `'c'` must be **ASCII `'0'`/`'1'`** — any other byte makes
   OpenOCD log an error and close the connection.

`swd_server.c` already implemented all three correctly. They are now pinned by
`firmware/test/test_swd_server.c`'s `test_openocd_remote_bitbang_conformance()`,
which **re-derives** each byte from the formulas above instead of hardcoding
letters, so an inverted mapping fails a unit test rather than a bring-up.

## Register block — RESOLVED (shell-regmap.md v0.1, I5)

`shell-regmap.md` v0.1 adds the SWDBB block (`MPS3_SWDBB_BASE` =
0x44A7_0000): `SWDBB_DRIVE` (`SWDBB_DRIVE_SWCLK`/`_SWDIO_O`/`_SWDIO_OE`) and
`SWDBB_SAMPLE` (`SWDBB_SAMPLE_SWDIO_I`, read-only) in `platform_regs.h`. The
per-byte pokes in `swd_server.c`'s `swd_server_handle_byte()` now hit these
real offsets (full write-through `DRIVE` composition per byte; `SAMPLE` bit
read for 'c'), and `swd_server_poll()`'s byte drain is real against the
`common/net_if.h` seam (single client, junk byte = drop connection, 'Q' =
close) — see `firmware/test/test_swd_server.c`.
(`CLKRST.RESET_CTRL.dbg_resetn`, srst, is still the separate CLKRST block
as before.)

## Gating during a swap

`coordinator/swap_fsm.c`'s `SWAP_GATE` state sets
`g_shell_state.swd_gated = true` before decoupling the RP; `swd_server_poll()`
must check this flag and stop toggling pins (or at least stop expecting a
DP response) while gated, since the DUT is held in reset / isolated during
reconfiguration (§6.2). Un-gated again in `SWAP_RELEASE`.
