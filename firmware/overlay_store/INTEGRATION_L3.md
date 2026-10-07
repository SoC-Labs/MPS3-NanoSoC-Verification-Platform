# D13 lane L3-core: integration notes for the later owners

This lane added the overlay store on the user microSD. It is written over an
abstract block device and has two providers.

> **APPLIED (2026-09-23, lane I-FW).** §1–§5, §7, §8 and §10 are wired on both
> engines; `README.md` describes the result. Deviations from the plan below:
> the dead QSPI symbols (sinks, `stream_*`, `promote_*`, `boot_load_default`,
> `commit`) were DELETED rather than stubbed, together with their callers; the
> glue (`overlay_store.c`) is shared by both engines and the engine-specific
> parts are an ENGINE PROVIDER (`overlay_store_bm.c`, harnessd's
> `ovlstore_linux.c`); wire error names are the contract's table
> (`overlay_store_err_name()`), not `ovlstore_sd_rc_name()`'s; the commit sink
> is a dedicated `mps3_cfg_agent_commit_sink_t` with a <= 512 B carry; the diag
> mailbox gained TWO words (`svc_us_6` and `usd_boot`, the Linux boot latch),
> v9; `test_sd.mk` and `firmware/usd/test.mk` are deleted.

Design: `docs/planning/HANDOVER_USD_OVERLAY_STORE.md` (§1, §4.5 b–g, §8, §11, §12).

| File | What |
|---|---|
| `ovl_bdev.h` | the block-device vtable, plus both providers' constructors |
| `ovl_bdev_usd.c` | bare-metal provider over `firmware/usd/usd.c` (target) |
| `ovl_bdev_posix.c` | `pread`/`pwrite` provider (Linux daemon, host tests). Hosted builds only |
| `ovlstore_sd.{h,c}` | the store: MBR discovery, ping-pong headers, A/B slots, `static_id` gate, verify-before-load, stream, commit, format, clear, CLCD text |
| `test_sd.mk` | standalone host build and the mb-gcc compile check. Delete it once §10 lands |
| `../test/test_ovlstore_sd.c` | host tests: POSIX card images, plus an end-to-end run over `usd.c` + `fake_usd.c` |

Run the tests from the repo root:

- `make -f firmware/overlay_store/test_sd.mk` runs the host tests.
- `make -f firmware/overlay_store/test_sd.mk target-check OUT=<scratch>` runs the mb-gcc compile check.

RAM on target:
- `sizeof(ovlstore_sd_t)` is **8,660 B**, mostly two 4 KiB I/O buffers.
- Setting `-DOVLSTORE_SD_OP_BLOCKS=1` cuts it to about 1.5 KiB. Reads then pay the card's access latency once per block.

---

## 1. Replace `overlay_store.c`'s SST26 backend (the 11 symbols)

Production code outside the module calls exactly these 11 symbols (grep at
`1d6a783`). HARNESSD's "no store" `ovlstore_linux.c` stubs the same 11. Keep the
file name `overlay_store.c` and rewrite its body as glue over one static store:

```c
static ovlstore_sd_t g_ovlsd;                 /* 8,660 B */
static ovl_bdev_t    g_ovlsd_bd;
```

| # | Symbol | Caller today | After L3 |
|---|---|---|---|
| 1 | `overlay_store_init()` | `coordinator.c:81` | `ovl_bdev_usd_bind(&g_ovlsd_bd, true); ovlstore_sd_init(&g_ovlsd, &g_ovlsd_bd, &cfg)` with `cfg.live_static_id = mps3_shell_static_id()`, `cfg.allow_wipe = true` (bare metal only, §7a) and `cfg.rm_name = clcd_rm_name` (through a 2-line adapter; `NULL` in non-CLCD builds). The store does **no I/O** here. `usd_init()` stays in `main.c`, before `coordinator_init()`. |
| 2 | `overlay_store_swap_staging_sink()` | `coordinator.c:87` | `return NULL;`. The QSPI scratch is gone. Large partials already take the ICAP-direct sink (`ICAP_DIRECT ?= 1`, `firmware/platform/Makefile:201`). **Drop `ICAP_DIRECT=0` as a supported build.** |
| 3 | `overlay_store_clearing_staging_sink()` | `coordinator.c:93` | `return NULL;`. The fielded build is QSPI-free: `CLEARING_RAM_BYTES = SWAP_CLEARING_ARENA_BYTES = 262144` ≥ the 222 KB largest clearing, which `test_swap_qspi_free` pins. |
| 4 | `overlay_store_boot_load_default()` | `coordinator.c:123` | **Delete the call.** It is replaced by the boot hook (§5). Keep the symbol returning `OVLSTORE_ERR_NOT_VALID` until the stubs follow. |
| 5 | `overlay_store_commit(rm, &slot)` | `coordinator.c:381` | Replaced by the parked commit (§3). Until then: `return -1` (reply `commit failed`). |
| 6 | `overlay_store_get_greybox_clearing()` | `swap_fsm.c:266` | **Unchanged.** It is the baked blob, not flash. |
| 7 | `overlay_store_stream_cached_clearing()` | `swap_fsm.c:596` (`in_qspi`) | `return -1;`. With sink 3 `NULL`, `in_qspi` is never set. Delete the branch in L3a. |
| 8 | `overlay_store_stream_staged_partial()` | `swap_fsm.c:753` (`in_qspi`) | `return -1;`. The same reasoning applies to sink 2. |
| 9 | `overlay_store_promote_begin()` | `swap_fsm.c:903` | `return -1;`. Dead once `in_qspi` is gone. Delete in L3a. |
| 10 | `overlay_store_promote_step()` | `swap_fsm.c:919` | `return OVL_PROMOTE_ERR;` |
| 11 | `overlay_store_promote_abort()` | `swap_fsm.c:932` | no-op |

Also:

- `mps3_ovlstore_phase()` (weak hook, overridden by `platform/src/ovlstore_phase.c`) keeps its enum **values** (diag mailbox contract). Only `IDLE`, `INIT`, `READ_HDR`, `CRC` and `STREAM` stay meaningful. `BP_UNLOCK`, `ERASE_SECTOR`, `PROGRAM_PAGE`, `WAIT_READY`, `PROMOTE` and `REJECT` are retired; do not renumber.
- `firmware/platform/Makefile` `SRCS`:
  - add `$(FW)/overlay_store/ovlstore_sd.c`, `$(FW)/overlay_store/ovl_bdev_usd.c` and `$(FW)/usd/usd.c` (L2's line);
  - keep `overlay_store.c` (now glue) and `ovlstore_codec.c`;
  - **never** `ovl_bdev_posix.c`.

## 2. Slice "L3a": what to delete, in the same commit as L1 patch 03

Patch 03 regenerates the regmap (OVLSTORE → USD) and removes `MPS3_OVLSTORE_BASE`
and `OVLSTORE_SPI_*`. Delete the SST26 code; **do not add a compat `#define`**
(handover §11.1).

**`firmware/overlay_store/overlay_store.c`:** delete everything SST26:
- the `SST26_*` opcodes and geometry, the `xsp_*` AXI-QSPI primitives, `sst26_*`, `flash_identified()`, `sst26_build_scoped_bpr()`;
- `spi_range_ok()`, `spi_flash_{read,erase,erase_sector,program}()`, `stage_erase_forward()`;
- the `MPS3_OVLSTORE_TEST_HOOKS` block;
- `overlay_store_bp_{unlock,lock}()`, `overlay_store_read_header()`, `flash_region_crc*()`, `overlay_store_verify_slot()`;
- `hwicap_flash_read()`, `stream_flash_region_to_hwicap()`;
- the old `boot_load_default()` and `commit()` bodies;
- the swap-stage and clearing-stage sinks, the promote machinery, `cache_clearing_from_flash()`, `overlay_store_get_default_manifest()`.

Keep only `overlay_store_get_greybox_clearing()` and the weak `mps3_ovlstore_phase()`. Then write the glue from §1.

**`overlay_store.h`:** delete the following, and keep only the 11 prototypes, `overlay_manifest_info_t`, `ovl_promote_status_t` (until L3a removes its users) and the phase enum:
- `OVLSTORE_*_OFFSET`, `OVLSTORE_CLEARING_*`, `OVLSTORE_FLASH_END`, `OVLSTORE_SLOT_B_PAYLOAD_END`;
- `overlay_store_bp_*`, `read_header`, `verify_slot`, `get_default_manifest`, `promote_staged_clearing`, the test hooks.

**`swap_fsm.c`:** delete the `in_qspi` branches at `:596` (clearing replay) and `:753` (partial stream), and the promote path at `:889–:940`. Also delete `s_incoming_partial_in_qspi` and `g_current_rm_clearing.in_qspi`.

**`config_agent`:** `set_qspi_sink()` / `set_qspi_clearing_sink()` then only ever receive `NULL`. Remove them, or leave them as dead seams, at the owner's choice.

**Tests.** Two groups link `fake_qspi_flash.c`:
- `OVL_SPI_SRCS` (`firmware/test/Makefile:615`);
- `QSPI_STAGE_SRCS` (`:655`).

| Test | Verdict |
|---|---|
| `test_overlay_store_spi` | **retire.** It tested the SST26 leaves. Its store semantics are covered by `test_ovlstore_sd` |
| `test_qspi_fault_inject` | **retire** (SST26 fault injection) |
| `test_qspi_chunking` | **retire.** Its `MPS3_OVLSTORE_TEST_HOOKS` erase/program primitives are gone |
| `test_qspi_promote_resumable` | **retire** (the QSPI promote is gone) |
| `test_swap_qspi_staging` and `_fifo` | **retire.** The large partial goes ICAP-direct, covered by `test_swap_icap_direct` and `test_swap_icap_defer` |
| `test_swap_clearing_cache` | **retire.** The large clearing lives in the RAM arena, covered by `test_swap_qspi_free` |
| `test_swap_qspi_free` and `_tinyarena` | **retarget.** Keep the QSPI-free assertion, drop `fake_qspi_flash.c` and `overlay_store.c` from the link, and use `fake_overlay_store.c`. "Zero flash ops" becomes "no store sink registered" |
| `test_tftp_large_swap` | **retarget.** Keep the ICAP-direct-over-TFTP case and the mid-transfer ERROR case. Drop the two QSPI-sink cases |

Also delete `firmware/test/fake_qspi_flash.{c,h}`. Fix the comments that name it in `fake_overlay_store.c:89` and `mock_regs.h`. The codec tests stay: `test_ovlstore_header`, `ovlstore_pack` and `tests/firmware_logic/test_ovlstore_header_golden.py` (the codec is unchanged).

## 3. `commit` → the store, over the 6910 sink (D1 = re-push)

**Wire (v0.13):**

```json
{"op":"commit","rm":"led","rm_id":"0x…","static_id":"0x…","clear_len":N,"clear_crc":"0x…","part_len":N,"part_crc":"0x…"}
```

`clear_len` and `part_len` are **new**. The store needs both lengths up front:
- to size-check them against the 8 MiB slot;
- to place the partial at `align512(clear_len)`.

pyverify has them from the manifest.

The handler (`coordinator_handle_commit`, parked like `swap`):

1. It calls `ovlstore_sd_commit_begin(&g_ovlsd, &desc, mps3_shell_static_id(), rm_id_live)`.
   - `rm_id_live` is `DFXCTL.RM_ID`, qualified by `RM_STATUS.rm_id_valid`.
   - Any refusal is replied at once with `ovlstore_sd_rc_name(rc)`. Examples: `"no sd card"`, `"foreign card"`, `"static_id mismatch"`, `"rm_id mismatch"`, `"store busy"`, `"too big for slot"`.
2. It registers a **commit sink** with `config_agent` for the next two 6910 pushes (clearing, then partial). Each push's `mps3_bitstream_hdr_t` must match the desc before any byte is fed. Otherwise the sink's `begin()` fails and the commit is aborted. The checks:
   - `kind`;
   - `static_id`;
   - `rm_id`;
   - `len_words*4 == clear_len/part_len`;
   - `crc32 == clear_crc/part_crc`.
3. Sink `write(buf,len)` → `ovlstore_sd_commit_feed(&g_ovlsd, which, buf, len, &used)`. **Back-pressure is the one change to `config_agent`.** Today `session_feed()` (`config_agent.c:474`) needs `write()` to take every byte. The feed takes up to two 4 KiB buffers and then returns `OVLSD_BUSY` while a card write is in flight. Two options:
   - **Preferred:** give the sink an optional `int (*write_some)(const void*, uint32_t, uint32_t *used)`. On a short take, `session_feed()` leaves the rest of the chain unconsumed. The windowed receive then does not reopen the TCP window (`mps3_net_recved` counts consumed bytes only), so the host's windowed pusher stalls. Retry on the next `config_agent_poll()`.
   - **Alternative:** a 16 KiB ring in the glue, one `CFG_AGENT_ACK_WINDOW_BYTES` window, drained into `commit_feed()` from the store's service. It costs 16 KiB of RAM, and it still needs `ready()` gating before each window.
4. After the partial's `finish()`, call `ovlstore_sd_commit_end()`. Poll `ovlstore_sd_job_result()` from the service row until it is not `OVLSD_BUSY`. Then reply:
   - `{"ok":true,"slot":"A"|"B"}` (`ovlstore_sd_commit_slot()`), or
   - `{"ok":false,"err":"<rc name>"}`.

   The store reads **both regions back** and CRC-checks them before it writes the header. That takes about as long again as the writes (§7).
5. Any sink `abort()` (TCP reset, a header mismatch, the `AWAIT_*` idle timeout) calls `ovlstore_sd_commit_abort()` while feeding. The header is never touched, so the old default survives. After `commit_end()` the finalisation needs no input and runs to completion.
6. **A STALE card may be committed over.** That is how a re-keyed board recovers. It is allowed because the desc must carry the **live** `static_id`.

   Target slot selection:
   - VALID: the target is never the verified slot.
   - STALE: the inactive slot; the stale default stays until the flip.
   - BAD: the corrupt active slot.
   - EMPTY: slot A.

## 4. The swap FSM's `src:"usd"`

`swap_fsm_start(rm, "usd")` is called only by the boot hook (§5). There is no host `swap src:usd` in v0.13. The flow is the silicon-proven FSM with a local byte source:

| State | `src "usd"` |
|---|---|
| `STREAM_CLEARING` | unchanged. At boot the outgoing clearing is the greybox blob (RAM) |
| `AWAIT_INCOMING_CLEARING` | no network. Take `ovlstore_sd_default()` → `s_staged_incoming_clearing = {rm_id, static_id, len_words = clear_len/4, crc32 = clear_crc, data = NULL}`, plus a new flag `in_usd = 1` |
| `AWAIT_PARTIAL` | the same, from the partial fields. `s_target_rm_id = desc.rm_id` |
| `STREAM_PARTIAL` | on entry `ovlstore_sd_stream_begin(&g_ovlsd, OVLSD_PARTIAL)`. Each poll: `ovlstore_sd_stream_next(&g_ovlsd, mps3_hwicap_pace_words(MPS3_HWICAP_CHUNK_WORDS)*4, &p, &n)`. `OVLSD_OK` → `mps3_hwicap_stream_mem(p, n)`. `OVLSD_BUSY` → no progress this poll. `OVLSD_DONE` → `partial_stream_done`. Negative → `stream_error`. The store withholds the partial's **last** buffer unless the re-read CRC matches, so a card that went bad since the verify never completes the bitstream |
| `VERIFY` | unchanged (`DFXCTL.RM_ID == desc.rm_id`) |
| `CACHE_CLEARING` | for `in_usd`, stream the slot's **clearing** into `swap_fsm_clearing_stage_buffer()` (256 KiB ≥ 222 KB). Use `stream_begin(OVLSD_CLEARING)`, then `stream_next(cap_left)` and `memcpy` each poll, bounded. On `OVLSD_DONE` → `swap_fsm_set_current_clearing({data = arena, in_qspi = 0})` and `swap_fsm_set_current_partial({data = NULL})`. On error, take the existing fail-closed arena path. **F12 resolved:** the resident clearing is in RAM, and nothing points at the card after the swap |
| `RELEASE` / `DONE` | unchanged |

Notes on the stream contract:
- The pointer from `stream_next()` is valid until the next store call, and `mps3_hwicap_stream_mem()` consumes it synchronously.
- `max_bytes` must be a multiple of 4.
- `clear_len > arena cap` → refuse the boot load (`"failed:too big for slot"`) before arming the FSM.

## 5. Boot hook (after lwIP), PB1 skip, service row

**Service row.** `MPS3_SVC_MAX` is full at 12 (`service.h:112`).
- Add **one** row, `/* 12 */ { "usd", svc_usd, 2000u }`. Raise `MPS3_SVC_MAX` to 13 and add the `svc_max_us_6` mailbox word (`MPS3_SVC_PACK_WORDS` 7).
- With `own_poll = true`, the one call drives both the driver and the store: `svc_usd() { ovlstore_sd_poll(&g_ovlsd, mps3_sys_now_ms()); usd_boot_hook(); }`.
- Budget 2 ms:
  - `usd_poll` ≤ ~0.66 ms (firmware/usd/README);
  - store CRC ≤ ~0.4 ms;
  - plus one op start and one `io_status`.

  It is a budget, not a licence (memory: touch-works-and-starves-the-superloop).

**The hook** runs once per power-on, as a state:

1. `t0 = now` at the first call. Sample PB1 once, **before** deciding: `CLCDKVM_STATUS_PB_LEVEL` (`platform_regs.h:821`, KVM builds; no PB1 = not held). If PB1 is held: `ovlstore_sd_set_skip(&g_ovlsd, true)` and set `boot = "skipped"`. Done.
2. Wait at least `USD_BOOT_GRACE_MS` (100 ms). The driver starts ABSENT, and `CD_PRESENT` is debounced for ≥ 10 ms after reset, so "no card" must not be concluded on pass 1. The network is already up and nothing blocks, so david's rule 1 holds: no card means no added delay.
3. Then, per state:

   | State | Action |
   |---|---|
   | `OVLSD_NO_CARD` / `OVLSD_NO_HW` | `boot = "none"`, done |
   | `OVLSD_INIT` | wait. Cap it at `USD_BOOT_TIMEOUT_MS` (30 s; SETTLE + init + the ~8 s verify fit), then `boot = "failed:card not ready"` |
   | `OVLSD_VALID` | only if `swap_fsm_idle()` and the RP still holds the greybox (`g_shell_state.current_rm_id == 0`): `swap_fsm_start(clcd_rm_name(def.rm_id), "usd")`. `boot = "loaded"` when the swap result is verified, else `"failed:<fail_tag>"` |
   | anything else (`FOREIGN`, `EMPTY`, `STALE`, `BAD`, `UNSUPPORTED`, `ERROR`) | `boot = "none"` (the state itself says why) |

A card inserted at runtime is probed and verified (the CLCD follows) but **never loaded**. Loads happen at power-on only (handover §1).

## 6. CLCD row 4 (L5)

In `clcd.c`'s status page, after the `SID :` field (`clcd.c:789`), add:

```c
put_at(next, 4, 18, "USD : ");
put_at(next, 4, 24, mps3_usd_status_text());   /* <= 16 chars: 24 + 16 = 40 */
```

`mps3_usd_status_text()` is a weak seam returning `"none"`, overridden by the glue with `ovlstore_sd_state_text(&g_ovlsd)`. That keeps `clcd.c` free of store includes.

- The text is at most 16 printable ASCII chars in every state. `test_ovlstore_sd` asserts this on every poll.
- The rm name is clipped to 12 chars plus `" [A]"`. The handover's 11 assumed a different suffix, but 12 + 4 = 16 fits exactly.
- Unknown ids come from `clcd_rm_name()` as `rm?XXXXXXXX` (11 chars).
- The page already diffs cells, so repaint every pass.
- **Never feed the banner** from any USD state.
- Add every state string to `clcd_test_render()`: `none`, `no hw`, `init`, `unsupported`, `ERR 30`, `foreign`, `empty`, `nanosoc_mult [B]`, `stale key`, `bad`, `skipped`.

## 7. Net protocol v0.13 (L4)

Error **names** only, from `ovlstore_sd_rc_name()`. **Never errno numbers**: `ETIMEDOUT` is 110 in glibc and 116 in newlib. The `ERR <n>` codes are this project's own stable numbers:
- 1–11: `usd.h` ERROR;
- 20–23: UNSUPPORTED;
- 30: store read.

They may go on the wire as `"code"`.

```text
{"op":"usd"}
 -> {"ok":true,"present":true,"state":"valid","code":0,"card_mb":7580,
     "default":{"rm":"led","rm_id":"0x…","slot":"A","fallback":false},
     "boot":"loaded"}
    state   ∈ none | no_hw | init | unsupported | error | foreign | empty | valid | stale | bad
    (ovlstore_sd_state_name).
    With no card: {"ok":true,"present":false,"state":"none",…}. Absence is not an error.

{"op":"usd","action":"format","confirm":"erase"}      -> parks; {"ok":true}
      | {"ok":false,"err":"provision p4 first" | "filesystem present" | "unrecognised lba 0"
                         | "bad 0xDA partition" | "partition too small"}
{"op":"usd","action":"format","confirm":"erase-all"}  -> parks; the explicit WIPE (§7a); {"ok":true}
      | {"ok":false,"err":"partition too small" | "wipe disabled" | "io error" | …}
      Any other confirm string -> {"ok":false,"err":"confirm required"}.
{"op":"usd","action":"clear"}                     -> parks; {"ok":true} | {"ok":false,"err":"foreign card"}
{"op":"usd","action":"rescan"}                    -> {"ok":true}   (optional; re-reads the card)
{"op":"commit",…}                                 -> §3
```

### 7a. `format` and the explicit wipe

**Plain format (`"confirm":"erase"`)** applies rules (a)/(b)/(c) and nothing else. Rule (b) needs a **truly blank** card, and "blank" means both of these:

- LBA 0 is **all zero**, or a signed MBR whose four entries are all zero bytes. All-0xFF is **not** blank; nor are entry bytes without a type.
- **No signature** sits behind LBA 0. The store reads each of these LBAs once, in the probe and again right before writing:

  | LBA | Signature |
  |---|---|
  | 0 | a FAT/exFAT/NTFS boot sector, with or without 0x55AA |
  | 1 | a GPT header, `EFI PART` |
  | 2 | an ext2/3/4 superblock (0xEF53 at byte 1080), or f2fs (byte 1024) |
  | 7 | Linux swap |
  | 64 | ISO 9660 |
  | 128 | btrfs |

  Any signature makes the card FOREIGN, and format refuses it with `"filesystem present"` and **zero writes**.

**The wipe (`"confirm":"erase-all"`)** is for cards nothing else can reach. The user microSD is not on USB, so an Ethernet-only standalone user cannot wipe a factory card on a PC. It works on **any** card: FOREIGN cards (FAT32, GPT, ext4, exFAT, garbage) and harness cards, whose default it discards. In order it:

1. zeroes LBA 1..33 (the GPT primary header and entry array, which also covers the ext superblock and the FAT32/exFAT backup boot sectors at LBA 6 and 12);
2. writes a fresh MBR: no boot code, disk signature "MPS3", and **only** entry 4 = 0xDA over the last 32 MiB (the rule (b) layout);
3. writes the two header copies, leaving the card EMPTY.

The MBR is written **after** the zeroing, so a wipe that fails part-way leaves the old MBR, and the card stays FOREIGN until the wipe is retried. Nothing beyond LBA 33 outside the new p4 is touched. A GPT's backup header at the last LBA now lies inside p4 and is left as it is; no tool reads it without a protective 0xEE entry.

The wipe **never** happens implicitly:
- only the exact string `"erase-all"` reaches it;
- `"erase"`, commit, clear, rescan and boot never do;
- `"ERASE-ALL"`, `"erase all"` and `"erase-all "` get `"confirm required"`.

It is refused, with zero writes:
- with `"wipe disabled"` unless `cfg.allow_wipe` is set;
- with `"bad argument"` in raw-partition mode;
- with `"partition too small"` on a card under 33 MiB;
- with `"store busy"` while a job runs.

pyverify should ask the operator to type the confirmation, and must not offer it as a default.

**Request fields to add** (`mps3_ctrl_request_t`):
- `action[]`, `confirm[]`;
- `rm_id`, `static_id`, `clear_len`, `clear_crc`, `part_len`, `part_crc`.

**Feature bit:** `MPS3_FEATURE_USD = 1u << 13`. Set `MPS3_FEATURE_COUNT` to 14, and add `"usd"` to `s_feature_names` (`net_proto.c:498`). Bump the version.

Re-measure `MPS3_CTRL_RESP_MAX` with the `usd` reply through the real encoder.

**Timings for pyverify timeouts (target estimates, not measured):**
- the verify and the stream each run at about 1 block per superloop pass;
- ~1.5 ms per pass (0.73 baseline + 0.45 SD + 0.4 CRC);
- ≈ 8–9 s each for the 2.9 MB partial;
- a commit is the push time, plus about the same again for the read-back.

**FakeShell** (handover §8 trap) must enforce every refusal listed here and in §3, by name.

## 8. The Linux provider (HARNESSD / STORE lane)

Under Linux the kernel owns `usd_spi` (`spi-usd` → `mmc_spi` → `mmcblk0`, §12.2). **Never link `ovl_bdev_usd.c` or `usd.c` into the daemon.** Two equivalent selections:

1. **Recommended: the whole disk.** `ovl_bdev_posix_open(&px, &bd, "/dev/mmcblk0", OVL_BDEV_POSIX_LINUX_CARD)` with `cfg.raw_partition = false` and **`cfg.allow_wipe = false`**. Under Linux the card also holds the running system (p1–p3), so `"erase-all"` must answer `"wipe disabled"`. The daemon then runs the **same** MBR discovery and format rules (a)/(b)/(c) as bare metal: parity by construction. All writes stay inside p4, because the store's write guard allows nothing else. Writing the whole-disk node while p1–p3 are mounted is safe for disjoint LBA ranges.
2. **The partition node (as §12.6 names it).** First read `/dev/mmcblk0` LBA 0 **read-only**. Require `ovlstore_sd_mbr_classify(lba0, nblocks, &lba, &cnt) == OVLSD_MBR_DA`, and require `lba == /sys/class/block/mmcblk0p4/start`. Only then call `ovl_bdev_posix_open(&px, &bd, "/dev/mmcblk0p4", OVL_BDEV_POSIX_LINUX_CARD)` with `cfg.raw_partition = true`. Otherwise do not open it: that is FOREIGN. In this mode `format` is rule (a) only.

**Always use `OVL_BDEV_POSIX_LINUX_CARD`.** It sets `O_DSYNC` and `posix_fadvise(DONTNEED)` before every read. Without it the read-back verify reads the page cache, not the card.

The daemon's loop:
- call `ovlstore_sd_poll()` back-to-back while `ovlstore_sd_info().busy`;
- otherwise call it at the daemon's tick.

The budget constants may be raised with `-D` for Linux (e.g. `OVLSTORE_SD_CRC_BYTES_PER_POLL=65536`, `OVLSTORE_SD_OP_BLOCKS=128`).

The overlay store lives on **raw p4**, not in `/persist` (handover §12.4, still open for the Linux lead).

## 9. Semantics the later owners must keep

- **FOREIGN is read-only forever.** No partition is recorded for it, so the write guard refuses every write. Two things leave FOREIGN:
  - plain `format`, only when the card is truly blank (§7a). It re-reads LBA 0 and the signature LBAs immediately before writing.
  - the explicit `"erase-all"` wipe, which the write guard confines to LBA 1..33, then LBA 0, then the two new header copies.
- **Order of checks.** The `static_id` gate comes **before** any slot byte is read. A stale default is never read, never streamed and never a fallback. A fallback slot must pass the gate too.
- **Stale active slot, matching older slot.** When the active slot is stale and the older slot matches the live shell, the store reports STALE. It does not fall back to the older overlay, because "the last overlay" is the user's choice.
- **No partial reaches ICAP unverified.** The full CRC of the clearing and the partial runs before VALID. The re-read during streaming is CRC'd again, and its last buffer is withheld on a mismatch.
- `clear` is one header write. The next boot is greybox.

## 10. `firmware/test/Makefile` lines (then delete `test_sd.mk`)

Add to `TESTS :=`:

```make
         test_ovlstore_sd \
         test_ovlstore_sd_tiny \
```

Add these rules next to L2's `test_usd` rules. They share `fake_usd.c` and `mock_regs.c`.

```make
# SD overlay store (firmware/overlay_store/ovlstore_sd.c, D13-L3) over the POSIX
# provider on temp-file card images (under $TMPDIR, unlinked at once), plus one
# end-to-end case over ovl_bdev_usd + usd.c + fake_usd. Built twice: default
# tunables, and 1-block ops with 157 CRC bytes/poll so every buffer and hand-out
# boundary splits. OVLSTORE_SD_TEST_HOOKS exposes the write guard to the test.
OVLSD_TEST_SRCS := ../overlay_store/ovlstore_sd.c ../overlay_store/ovl_bdev_posix.c \
                   ../overlay_store/ovl_bdev_usd.c ../overlay_store/ovlstore_codec.c \
                   $(COMMON)/crc32.c ../usd/usd.c fake_usd.c mock_regs.c
OVLSD_TEST_DEPS := $(OVLSD_TEST_SRCS) ../overlay_store/ovlstore_sd.h ../overlay_store/ovl_bdev.h \
                   ../overlay_store/ovlstore_codec.h ../usd/usd.h ../usd/usd_regs.h \
                   fake_usd.h mock_regs.h $(COMMON)/crc32.h $(COMMON)/platform_regs.h

$(BIN)/test_ovlstore_sd: test_ovlstore_sd.c $(OVLSD_TEST_DEPS) | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DOVLSTORE_SD_TEST_HOOKS -o $@ test_ovlstore_sd.c $(OVLSD_TEST_SRCS)

$(BIN)/test_ovlstore_sd_tiny: test_ovlstore_sd.c $(OVLSD_TEST_DEPS) | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DOVLSTORE_SD_TEST_HOOKS -DOVLSTORE_SD_OP_BLOCKS=1u \
		-DOVLSTORE_SD_CRC_BYTES_PER_POLL=157u -o $@ test_ovlstore_sd.c $(OVLSD_TEST_SRCS)
```

After L3a, remove the retired binaries from §2 from `TESTS`, along with `OVL_SPI_SRCS` and `QSPI_STAGE_SRCS`.
