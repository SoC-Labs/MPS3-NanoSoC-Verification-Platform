# overlay_store/

The overlay store on the **user microSD** (D13): the last DUT overlay the host
committed, kept in an A/B pair of slots inside an MBR partition of type `0xDA`,
and reloaded **once per FPGA configuration** at power-on. Wire contract:
`docs/contracts/net-protocol.md` "User microSD (`usd`) and `commit` — v0.13".
Design: `docs/planning/HANDOVER_USD_OVERLAY_STORE.md`. How it was wired:
`INTEGRATION_L3.md`.

**This is the USER card.** It is never the MCC config card (`V2M_MPS3`,
`sd_install`). **The SST26/QSPI backend is gone** (D13 L3a): `0x44A4` is the
`usd_spi` block now, the DUT owns the real SST26 (D16), and no code in the
shell firmware issues a flash opcode anywhere.

## Files

| File | What | Linked into |
|---|---|---|
| `ovlstore_sd.{h,c}` | the store: MBR discovery, ping-pong headers, A/B slots, the `static_id` gate, verify-before-load, stream, commit, format / wipe, clear, the CLCD text | both engines, host tests |
| `ovl_bdev.h` | the block-device vtable + both providers' constructors | — |
| `ovl_bdev_usd.c` | bare-metal provider over `firmware/usd/usd.c` | bare metal only |
| `ovl_bdev_posix.c` | `pread`/`pwrite` provider (`O_DSYNC` + cache drop) | harnessd, host tests. **Never** the MicroBlaze image |
| `ovlstore_codec.{h,c}` | the header's pack/unpack (unchanged since the SST26 store) | both |
| `overlay_store.{h,c}` | **the glue**: the one store instance, the `usd` service row, the power-on hook, the swap source, the commit sink, the `usd` verb, the CLCD exports, the diag word, the error names | both engines |
| `overlay_store_bm.c` | the **bare-metal engine provider**: `usd_init()` + `ovl_bdev_usd`, `allow_wipe = true`, the `.data` boot latch, the baked greybox blob | bare metal only |

harnessd's engine provider is `src/linux_harness/sw/harnessd/ovlstore_linux.c`.

## The two engines

| | bare metal (MicroBlaze) | Linux (`mps3-harnessd`) |
|---|---|---|
| device | `usd_spi` @ `0x44A4` via `usd.c` (the store owns `usd_poll()`) | the whole disk `/dev/mmcblk0` (`--usd-dev`), `OVL_BDEV_POSIX_LINUX_CARD`; the kernel owns `usd_spi`, harnessd never touches `0x44A4` |
| `"erase-all"` wipe | allowed | `wipe disabled` (the card holds the running system) |
| budgets | 8 blocks an op, 512 CRC bytes a poll | 64 blocks an op, 64 KiB CRC a poll (harnessd Makefile) |
| service row | main.c row 12 `"usd"`, 1500 µs | main_linux.c row 12 `"usd"`, unbudgeted (synchronous `O_DSYNC` I/O, like `persist`) |
| boot latch | an initialised `.data` word | the diag mailbox word `usd_boot` at LMB `0x1FFB4` |

## THE BOOT LATCH — once per FPGA configuration

The power-on load must run **once per FPGA configuration**, and never on a
harnessd respawn, an OS reboot or a WDOG reset. The glue keeps its decision in
one 32-bit word, and each engine stores that word somewhere that **only a
reconfiguration clears**:

```
[31:16] 0xB007 once decided   [15:8] failure reason   [3:0] 1 pending, 2 loaded,
                                                            3 skipped, 4 none, 5 failed
```

**Bare metal: an initialised `.data` word** (`overlay_store_bm.c`,
`s_boot_latch = "FRES"`, pinned in `.data.mps3_ovl_boot_latch`). Verified in
the tree:

- `firmware/platform/lscript.ld.in` places `.data`/`.sdata` in `local_lmb`
  with **no `AT()`**: nothing is copied at start-up. The initial value is BRAM
  INIT content, baked into the bitstream by updatemem with the ELF.
- Vitis 2024.1's `crt0.S` → `_crtinit` (`data/embeddedsw/lib/microblaze/src/crtinit.S`)
  zeroes `.sbss` and `.bss` only; it never touches `.data`.
- A WDOG reset (`proc_sys_reset aux_reset_in`, the `reboot` verb) resets the
  MicroBlaze, not the BRAM. It re-runs crt0 over the same `.data`.
- The word must be initialised **non-zero**. A zero-initialised static lands in
  `.bss`, which crt0 zeroes on every reset: the opposite of a latch.
- Checked in a linked `PRODUCT=1 LMB_KB=1024` ELF (2026-09-23):
  `s_boot_latch` sits inside `.data` (0x36df0 in that build), with VMA = LMA.

A JTAG ELF download re-initialises `.data`, so it counts as a new
configuration. It is a new firmware image.

**Linux: the diag mailbox word `usd_boot`** (LMB `0x1FFB4`, diag.h v9), read
and written through the `lmb-tail` UIO page (`ovlstore_linux.c`):

- The mailbox is BRAM. stage0's baked image ends below `0x1FE00` (stage0.ld),
  so the mailbox comes up **zero after a reconfiguration**. `stage0_status.h`
  "PERSISTENCE" relies on the same fact.
- stage0 never writes `0x1FF00–0x1FFFF` (it is harnessd's), and the kernel does
  not map it. A WDOG reset, an OS reboot and a respawn all find the word where
  the last harnessd left it (none of them reconfigures the FPGA).
- harnessd's mailbox mirror rewrites the word on every pass from
  `overlay_store_diag_word()`. The glue restores a decided latch in
  `overlay_store_init()`, **before** the service table's first mirror, so a
  respawn cannot zero it.
- The stage0 boot counter was considered and not used. `boot_count == 1 &&
  boot_start` would never load if the first OS boot of a configuration died
  before harnessd ran, and it assumes every OS reboot re-enters stage0. The
  mailbox latch depends on neither.
- With no LMB-tail window, freshness cannot be proven: the latch reads "decided:
  none" and nothing is ever loaded. This fails safe.
- MOCK builds: the page is the `--mock-fabric` file. A new file is a new
  configuration; the same file across runs is a respawn.

**The rule the glue applies** (`overlay_store.c`, `boot_restore` + `boot_step`):

- A decided latch is **restored, never re-decided**.
- A latch left `pending` means a restart interrupted the load. It becomes
  `failed:aborted`, and is still not retried.
- On a fresh configuration the first service call writes `pending` **before**
  anything else, so no crash from then on can make it load twice. Then:
  - PB1 (`CLCDKVM_STATUS_PB_LEVEL`, sampled once, as a level) held: `skipped`;
  - otherwise the grace period passes (100 ms);
  - no card / no hw: `none`;
  - still initialising after 30 s: `failed:timeout`;
  - `valid`, the swap FSM idle and the RP holding the greybox:
    `swap_fsm_start(<name>, "usd")`. The result is `loaded`, or
    `failed:<swap state>` / `failed:<store error>`;
  - any other state: `none`.

## The swap source `usd`

The power-on load is an ordinary swap through the silicon-proven FSM:

1. The outgoing greybox clearing streams from RAM.
2. The card's **partial** streams to HWICAP. The store re-reads it and CRCs it
   again, and withholds its last buffer on a mismatch, so a card that went bad
   since the verify never completes the bitstream.
3. VERIFY runs.
4. At `SWAP_CACHE_CLEARING`, the card's **clearing** is read into the RAM arena
   (bounded, fail-closed). Nothing points at the card afterwards.

A host cannot name `src:"usd"` (`bad args`).

## commit (re-push)

1. `coordinator_handle_commit()` checks the request: the identity lock,
   `src:"tcp"`, the swap FSM idle, and the live `DFXCTL.RM_ID`.
2. `overlay_store_commit_begin()` asks the store, then registers a config_agent
   **commit sink**.
3. The next two 6910 pushes go to the card's inactive slot. Each push header is
   checked against the request before any byte is fed.
4. Back-pressure runs through `write_some()`. config_agent holds at most one
   512 B chunk and pulls nothing more until it drains. There is no ring.
5. After the partial, `commit_end()` runs: the store reads both regions back and
   CRCs them, then flips the header.
6. The control connection is parked meanwhile, like `swap`'s.
7. An idle feed times out like a swap's `AWAIT_*`.

## Error names

`overlay_store_err_name()` (overlay_store.h) maps every store code onto the
contract's names. Only names reach the wire, never errno numbers.

## Tests

In `firmware/test/Makefile`:

- `test_ovlstore_sd` / `_tiny`: the store;
- `test_usd` / `_tinybudget`: the driver;
- `test_config_agent_commit` / `_windowed`: the commit sink and back-pressure;
- `test_usd_dispatch`: the verb and commit through the real dispatcher;
- `test_swap_fsm_hw`: the FSM's usd source over a scripted store;
- `test_usd_boot`: everything real over `fake_usd`, including the boot latch,
  PB1, stale keys, interrupted loads and the parked commit.

For harnessd: `test_harnessd_e2e.py`, with the POSIX provider on a temp-file card.
