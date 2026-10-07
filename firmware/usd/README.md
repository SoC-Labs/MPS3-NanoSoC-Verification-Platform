# firmware/usd — user microSD driver (SPI mode, poll-driven)

Item D13, lane L2. Design: `docs/planning/HANDOVER_USD_OVERLAY_STORE.md` §4.5 a).
This is the **user** card behind the shell's `usd_spi` block at `0x44A4_0000`.
It is **not** the MCC config card (`V2M_MPS3`, written by `sd_install`).

| File | What |
|---|---|
| `usd.h` | API, states, error codes, every tunable |
| `usd.c` | the driver |
| `usd_regs.h` | `usd_spi` register contract. The base and offsets are provisional, and generated names replace them at integration |
| `../test/fake_usd.{c,h}` | register-level fake of `usd_spi` plus an SD card in SPI mode, as a `mock_regs` hook |
| `../test/test_usd.c` | the host tests |

## Run

`test_usd` and `test_usd_tinybudget` are in `firmware/test/Makefile`'s `TESTS`
(`make -C firmware/test test`); the MicroBlaze build compiles `usd.c` as part of
the shell image (`firmware/platform/Makefile` SRCS).

`test_usd_tinybudget` is the same test file built with the budgets at their
floor (`FAST=24 SLOW=21 WAIT=5`). Every data phase, token hunt and busy wait
then spans many polls, so every resume path runs.

## Per-poll budget

One `usd_poll()` never shifts more than its budget. A step starts only if its
worst case fits in what is left, so the budget is a ceiling, not a target.
The only spin is on `STATUS.BUSY` for a single shift.

| | clock | budget | wire time at the budget | measured max (host) |
|---|---|---|---|---|
| READY / I/O | 12.5 MHz (`CLKDIV=3`) | `USD_POLL_BUDGET_FAST` = **576 B** (512 data + 64) | **368.6 µs** | 576 B, 192 shifts |
| INIT | 400 kHz (`CLKDIV=124`) | `USD_POLL_BUDGET_SLOW` = **32 B** (one command step, 21 B) | **640 µs** | 31 B = 620 µs |
| 8-bit waits (token hunt, write busy) | either | `USD_WAIT_BYTES_PER_POLL` = 64 per call | 41 µs at 12.5 MHz | — |
| no card | — | 0 B: 1 STATUS read + 1 CTRL read | — | 0 DATA writes |

**Worst case including CPU time (an estimate; the AXI latency is unmeasured).**
Each shift costs 1 DATA write, at least 1 STATUS read and 1 DATA read beyond
its wire time. At about 0.2 µs per AXI-Lite access, that is about 0.6 µs per
shift. The design bound on shifts per call is `64 + 576/4 + 2*21 = 250`, and
`test_usd` asserts it on every poll.

- **12.5 MHz:** about 368.6 + 250 × 0.6 ≈ **0.52 ms** per call. A typical data poll (one block) is about 0.45 ms.
- **400 kHz:** about 640 + 32 × 0.6 ≈ **0.66 ms** per call. This happens only on the roughly 10 init polls after an insertion.

The healthy superloop pass is about 0.73 ms (memory: touch-works-and-starves-the-superloop).

**One-off:** a stuck `BUSY` costs up to `USD_BUSY_SPIN_MAX` = 20000 STATUS
reads (about 2–4 ms) once. The driver then goes to `ERR 9`, and it does not
retry while BUSY is still set.

**Throughput that follows from the budget:** one 512-byte block per call.
With a pass of about 0.73 + 0.45 ms, that is about 430 KB/s. So the largest
partial (2.88 MB, `nanosoc_multicore`) reads in about 7 s at power-on. A commit
writes at a similar rate plus the card's write busy. Raise `USD_POLL_BUDGET_FAST`
if that is too slow; see the open questions.

## Behaviour summary

- **ABSENT:** pads off. Poll = 1 STATUS read + 1 CTRL read. Zero writes.
- **SETTLE:** entered when CD_PRESENT and the raw pin both show a card. Waits 250 ms.
- **INIT**, at 400 kHz:
  1. 80 clocks with CS high.
  2. CMD0, up to 4 tries, then `ERR 1`.
  3. CMD8 0x1AA must echo; an illegal-command answer means `unsupported` (v1).
  4. CMD55 + ACMD41 with HCS as a retry state, every 4 ms, for up to 1 s; then `ERR 4`.
  5. CMD58 must show CCS=1; otherwise `unsupported` (SDSC).
  6. CMD9 CSD v2 gives the size.
  7. CLKDIV=3, then READY.
- **ERROR:** EN=0. Auto-retry `USD_INIT_TRIES` = 3 attempts per insertion, 2 s apart. After that it stays in ERROR until the card is reinserted.
- **I/O:** CMD17/CMD24 for one block; CMD18+CMD12 or CMD25+0xFD for more. Data phases use WIDE (32-bit) shifts. Write busy and token waits are states, not loops. An I/O timeout means `-ETIMEDOUT`, then `ERR 8`, then re-init (this counts as a try).
- **Removal**, in any state: the card goes ABSENT, EN=0, and any op in flight returns `-ENODEV`.
  - Detection: `!CD_PRESENT`, `ABORT`, `CD_CHANGED`, or the raw pin inside any shift.
  - A card swapped between two polls (only `CD_CHANGED` shows it) is re-initialised.

## Contract deviations and additions (none change the register map)

1. **Added `usd_card_blocks()`.** The store (L3) needs the card size in blocks to check partition bounds.
2. **`usd_io_status()` returns `USD_IO_DONE` before any op**, because there is no separate IDLE value.
3. **ID mismatch** (no `usd_spi` in the fabric) keeps state `ABSENT` and text `"none"`. `usd_error_code()` returns 11 (`USD_ERR_NO_BLOCK`), and the page is never written.
4. **Removal also uses `STATUS.CD_RAW`** (the undebounced pin, with the polarity applied by the driver), checked in every shift's STATUS read. Without it, a card pulled during the 10 ms debounce window lets a read finish as DONE with 0xFF data. A test case covers this.
5. **I/O timeout re-initialises the card** (`ERR 8`) instead of leaving a wedged card READY.
6. **Error codes** are listed in `usd.h`.
   - ERROR: 1–11.
   - UNSUPPORTED reasons: 20–23.
   - I/O errors are `-ENODEV`, `-EIO` and `-ETIMEDOUT` from `<errno.h>`. `ETIMEDOUT` is 110 in glibc and 116 in newlib, so L4 should put names on the wire, not numbers.
7. **CD bits are preserved.** Every CTRL write keeps `CD_POL` and `CD_IGNORE` as the hardware holds them, so board step B0 can set them over JTAG.
   - `USD_CTRL_CD_DEFAULT` sets them at build time.

## Assumptions about the RTL (L1 should confirm)

- a. **8-bit shifts:** with `WIDE=0`, a DATA write shifts bits [7:0]. With `WIDE=1`, bit 31 goes first and the first received byte lands in [31:24].
- b. **BUSY timing:** `BUSY` reads 1 on the first STATUS read after the DATA write's B-response, with no gap cycle. Otherwise the driver would read stale DATA.
- c. **`CD_RAW`** is the synchronised pin level, with the polarity **not** applied.
- d. **`CTRL` reads back** what was written, because the driver does read-modify-writes.
- e. **`EN=0` during a shift** sets `ABORT`. The driver does this only after a stuck `BUSY`, and it clears the `ABORT` it caused.
- f. **`CD_IGNORE` and `CD_PRESENT`:** whether `CD_IGNORE` forces `STATUS.CD_PRESENT` is not assumed. The driver ORs `CTRL.CD_IGNORE` in itself.

## Integration (DONE, 2026-09-23, lane I-FW)

- `firmware/test/Makefile` runs `test_usd` / `test_usd_tinybudget`; `test.mk` is deleted.
- `firmware/platform/Makefile` builds `usd.c` into the shell image.
- `usd_init()` is called by the overlay store's bare-metal engine
  (`firmware/overlay_store/overlay_store_bm.c`) from `overlay_store_init()`, and
  `usd_poll()` by the store's own poll (`ovl_bdev_usd_bind(own_poll = true)`),
  from ONE service row, main.c row 12 `"usd"` (1500 µs). Never twice a pass.
- The CLCD shows the STORE's text (`overlay_store_usd_text()`), which passes the
  driver's states through; the fallbacks in `usd_regs.h` stand until
  `platform_regs.h` carries `MPS3_USD_BASE` / `USD_*` (it does since I-SHELL's
  regen), then they are dead and may be deleted.
- Under Linux the kernel owns `usd_spi`: harnessd never links this driver.
