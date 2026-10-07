# Contract: host ⇄ shell network protocol

**Version:** v0.17 (2026-09-28). v0.17 is the Harness Manager's presence and front
panel on the Linux harness (`hello`, `panel`; its requests R1/R2);
v0.16 is the Linux harness's BOARD identity
(`identity`, `identity_set`, identify `label`) and the Harness Manager's `locate`;
v0.15 is the Linux harness's LCD mirror (TCP 6940);
v0.14 is the Linux harness L2 (`slot`); v0.13 is D13,
the user-microSD overlay store (`usd`, re-push `commit`); both are additive and
independent. Previous: v0.11 (v0.12 was withdrawn by the fold plan). The
MicroBlaze/lwIP shell firmware (A3) is the
server; the host tender + pyverify (A4) are the clients. One LAN9220 10/100
port carries everything (management + reconfig + debug + DUT uplink).

> **Changelog v0.17 (2026-09-28 — lane PANEL-PROTO: the Harness Manager's presence and
> front panel, its requests R1 and R2 (harness side), `harness-manager
> docs/design/CLCD_ALIGNMENT.md` §2, §5.3, §7.2 — ADDITIVE; a bare-metal image's bytes for
> every existing verb are unchanged, `test_codec_identity.py`):**
> - **New verb `hello`** (presence): a Harness Manager session says who it is (`sid`,
>   `who`, `app`, `name`, `role`, the hub lease it relays, a running job, `ttl`); the board
>   keeps at most 4 sessions, each listed for its TTL after its last hello, and draws the
>   first one, the lease badge and an open lease request on the panel. The reply carries
>   the live count, the panel's state and its tap ring. Display only: it never authorises
>   anything. NOT claim-locked. See "Presence and the panel".
> - **`hello` is the first request whose values NEST** (`lease`, `job`: one level of flat
>   objects). The decoder lets exactly that through (`mps3_json_parse_ex`); every other
>   line that fails the flat parse is still `bad json`, and a line over 256 characters is
>   still refused whole at the line layer (`bad json`).
> - **New verb `panel`**: the state (page, KVM owner, a flip in flight, banner, card,
>   touch, the live sessions, the tap ring); a frame in TWO halves (`frame` `"a"` = rows
>   0-7, `"b"` = rows 8-14, each with its per-cell role codes: a whole frame does not fit
>   one 1280-byte reply); a page change (`page`), refused while the DUT owns the panel
>   (`code` `held`) and CLAIM-LOCKED (`panel locked: board claimed (use ssh)`, `code`
>   `locked`). The reads stay open.
> - Both are ENGINE verbs (the reply carries `"op"`). Bare metal and a panel-less Linux
>   build decline: `hello not supported` / `panel not supported`, code `not_supported`.
> - **`version.features` gains the ENGINE names `presence` and `panel`** (a Linux harness
>   built with the panel), after `locate`, in that order. No bit.
> - The Linux panel (lane PANEL-CLCD's clcd.h seams, supplied by harnessd): row 0's right
>   side = the lease badge, row 11 = the `hm` row, rows 10-12 = a lease-request banner
>   (below the fault and IDENTIFY banners; a tap on it is an event `request`). The Harness
>   Manager's colours, words and glyphs by default (decision D3 a; the LCD mirror's e2e
>   oracle reads HM's palette and glyphs 0x80-0x86); `--panel-theme today` = today's
>   white/black/red pixels, the bare-metal look. A frame half's `theme` says which.

> **Changelog v0.16 (2026-09-28 — lane IDENT: every Linux harness answers as ITSELF,
> plus the Harness Manager's `locate` (its request R3) — ADDITIVE; a bare-metal image's
> bytes for every existing verb are unchanged, `test_codec_identity.py`):**
> - **Why.** One image served every board with one identity (the DTB MAC, a hard-coded
>   192.168.10.101 in S41mps3net, "MPS3-01" compiled into the CLCD): board 2 came up as
>   board 1 on silicon (2026-09-28). The identity (label, hostname, IP, MAC) is now
>   RESOLVED at each boot, per field: the `/persist` override > the stage0 bake (its
>   status block, STAGE0_CONTRACT §3.3) > the image default. See "Identity".
> - **New verb `identity`** (read, any peer) and **`identity_set`** (writes the
>   override; applies at the next boot; CLAIM-LOCKED like the slot mutations,
>   `identity locked: board claimed (use ssh)`, code `locked`; `no_persist` without a
>   card-backed `/persist`; `invalid` naming the field). Bare metal declines both:
>   `identity not supported`, code `not_supported`. See "Identity".
> - **New verb `locate`** (`{"s":1-30,"who":..}`, `{"s":0}` stops): the panel backlight
>   blinks at 2 Hz and rows 10-12 show "IDENTIFY: <who>"; a tap ends it. NOT
>   claim-locked. Bare metal (and a panel-less build) declines: `locate not
>   supported`, code `not_supported`. See "Locate".
> - **ENGINE verbs.** All three are served by an engine provider that parses its own
>   (flat) arguments and renders the reply body; the reply carries `"op"` (unlike the
>   older verbs). Requests stay flat.
> - **`identify` gains `label`** (the resolved label), after `ssh` and before `ports`
>   (still last), only where the engine resolves one (Linux). Its `mac` is the
>   resolved MAC and its `ip` the resolved static address, except while a DHCP lease
>   is held (`dhcp:true`), when `ip` stays the lease as before.
> - **`version.features` gains the ENGINE names `identity`** (every Linux harness) and
>   **`locate`** (a build with the panel), after `lcd_mirror`, in that order. No bit.
> - The CLCD's row 0 (label), row 5 (`NET :` IP) and row 14 (MAC) show the resolved
>   identity on the Linux harness (bare metal: unchanged).

> **Changelog v0.15 (2026-09-26 — the Linux harness's pixel-exact LCD mirror,
> `docs/planning/linux_lanes/LCD_MIRROR_FPGA.md` §6-§7 — ADDITIVE; a bare-metal
> image's bytes for every verb are unchanged, proven by
> `src/linux_harness/sw/harnessd/tests/test_codec_identity.py` (head == new)):**
> - **New TCP 6940 on 127.0.0.1 only** (`mps3-lcdmirror`, a child of
>   `mps3-harnessd`): the panel's frame buffer as dirty 16x16 tiles. See "LCD
>   mirror (TCP 6940)". Reached through an SSH port forward (a claim, S12).
> - **`version.features` gains the ENGINE name `lcd_mirror`**, emitted after every
>   bit name (`mps3_proto_features_extra()`); it spends no `MPS3_FEATURE_*` bit.
>   Clients test the name.
> - **`version.lcd_mirror`** = `{"port":6940,"mode":"sw"|"hw","proto":1}`, after
>   `id_skew`, before `impl` (still last).
> - **`stats.lcd_mirror`** = `{"peer":"a.b.c.d:port"|null,"since":<up_ms>,"fps":<d.d>,
>   "bytes":<u32>}`, after the `touch_*` keys, before `os_up_ms` (still last).
> - The three appear only while the mirror is configured (not `--lcdmirror none`).

> **v0.14 amendments (2026-09-26 — the Harness Manager's answers,
> `docs/planning/linux_lanes/HM_ANSWERS_2026-09-26.md` — ADDITIVE; bare-metal bytes
> unchanged, `test_codec_identity.py`):**
> - **`reboot` answers `EBUSY` while a card job runs** on the Linux harness (a slot push
>   or read-back/`verify`, a D13 `commit` / `usd` format/clear). B2: a reboot mid-job
>   wedged the card. See "Reboot".
> - **The booted slot's record is stamped** once the boot is confirmed (source 2
>   "boot"), so a slot written by any tool can be `verify`'d after a later reboot. See
>   "Slot images" → "The slot record".
> - **`version.features` gains `slot`** (bit 14, APPENDED): set by the engine that
>   serves the verb (a linked provider, not a build flag). Bare metal never sets it,
>   so its bytes are unchanged. Clients test the NAME. See "Version".
> - **The claim lock covers XVC 2542 and `jtag_server` 6921**: a non-local peer on
>   a claimed board gets one `{"ok":false,"err":"xvc locked: …","code":"locked"}`
>   line (resp. `jtag locked: …`) and the close. `version.features` gains
>   `xvc_lock` (bit 15). See "Slot images" → "The lock".
> - **The claim lock covers the D13 store's mutations** (`usd` format/clear/rescan,
>   the re-push `commit`): `usd locked: …` / `commit locked: …` with `code`
>   `locked`. See "Slot images" → "The lock".
> - **An optional `code` beside `err`** on `slot` refusals, and **`job.code`** on a
>   failed job — emitted only when set, so no other line changes. See "Slot images"
>   → "Codes".
> - **`identify.ssh` gains `key_sha256`** — the claiming (first) key's fingerprint,
>   `""` when unclaimed. See "Identify".
> - **`slot` replies gain `claimed` and `confirmed`**, APPENDED after `job` in both
>   forms (card / no card): the SSH claim (the lock's input, readable through an
>   `ssh -L` tunnel, which identify's UDP cannot ride) and whether this boot was
>   confirmed to stage0 (`att_confirm`). See "Slot images" → "`status`".

> **Changelog v0.14 (2026-09-24 — Linux harness, plan §10a S10 — ADDITIVE; a
> bare-metal image's bytes for every existing verb are unchanged, proven by
> `src/linux_harness/sw/harnessd/tests/test_codec_identity.py`):**
> - **New verb `slot`** — the user-microSD boot slots stage0 loads Linux from:
>   `status`, `commit`, `rollback`, `verify`. Every act answers the same status
>   object. A bare-metal image declines every act: `slot not supported`. See
>   "Slot images".
> - **New push kind 2 (`MPS3_BIN_KIND_SLOT_IMAGE`)** on 6910 and TFTP 69: a stage0
>   S0LB boot image for the INACTIVE slot. Same 24-byte header, same static_id
>   and CRC rules; a bare-metal image refuses it exactly as it refused an unknown
>   kind before. See "Slot images".
> - **Nested objects in a 6900 reply** (`slot`'s `a`/`b`/`job`; v0.13's `usd`
>   `default` nests too). Requests stay flat; the firmware tokenizer is unchanged.
> - ~~No new `version.features` bit~~ — amended 2026-09-26: `slot` is bit 14
>   (below). A tool may still tell the engines apart by `version.impl` and by the
>   verb's own decline.
> - **The lock** (the project lead, 2026-09-24): once the board's SSH is claimed, slot
>   mutations are refused from any peer that is not the board itself
>   (`slot locked: board claimed (use ssh)`; TFTP error 2). See "Slot images" →
>   "The lock".

> **Changelog v0.13 (2026-09-23 — D13, the user-microSD overlay store; v0.12 was
> withdrawn by the fold plan, so the number is skipped on purpose):**
> - **New verb `usd`**: status of the user microSD and the overlay store on it,
>   plus the actions `format`, `clear` and `rescan`. See "User microSD (`usd`)".
> - **`commit` is REPLACED.** It is now a re-push: it parks like `swap`, then takes
>   the pair over 6910 into the card's inactive slot. The v0.11 form (`rm` only)
>   is refused with `bad args`. It never answered anything but `commit failed`,
>   so nothing relied on it.
> - **`version.features` gains `usd`**, APPENDED as the next free bit (13 on this
>   branch). Clients test it by NAME. If another lane appends a bit first,
>   renumber at merge; the name does not move.
> - **`diag` gains two keys** (diag.h v9, 42 → 44): `svc_us_6` (the `usd` service
>   row's worst-case µs) and `usd_boot` (the power-on decision latch). Both are
>   appended; nothing moves. Worst-case reply: `10 + 455 + 44*14 + 2 + 1` =
>   1084 B, within `MPS3_CTRL_RESP_MAX` 1280.
> - **Error strings are NAMES, never errno numbers.** Errno values differ between
>   host and target (`ETIMEDOUT` is 110 on the host, 116 in newlib).

> **Changelog v0.11 Linux-harness additions (2026-09-23 — ADDITIVE; a
> bare-metal image's bytes are unchanged, proven byte-for-byte by
> `src/linux_harness/sw/harnessd/tests/test_codec_identity.py`):**
> - **`version.impl`** — the ENGINE: `"linux"` from `mps3-harnessd`, ABSENT from
>   a bare-metal image. Always the LAST key. See "Version".
> - **`version.id_skew`** — present only when the engine's identity sources
>   disagree (the Linux harness: stage0's baked fabric `static_id` vs the card
>   image's claim, or the fabric's USR_ACCESS vs the image's `ver32`); emitted
>   after `skew`, before `impl`.
> - **`stats.os_up_ms`** — the OS uptime, LAST, Linux only. `up_ms` stays "since
>   the shell firmware started" on both engines (a harnessd respawn restarts it).
> - **Omitted keys, not zeroed.** An engine that cannot fill a `diag` (or
>   `stats`) key leaves it OFF the line; the remaining keys keep their order.
>   Bare metal omits nothing. The Linux harness omits the fifteen lwIP /
>   bare-metal-LAN9220 / QSPI-store `diag` keys (see "Diagnostics").
> - **`swap` / `commit` refusal `identity lock: <reason>`** on an engine whose
>   fabric identity is unproven (see "Identity lock").
> - **New UDP 6899 `identify`** (both engines; see "Identify").
> - **TOFU first-key claim** — a TFTP WRQ named exactly `authorized_keys` (see
>   "TOFU first-key claim").

> **Changelog v0.11 (2026-09-23 — ADDITIVE: four new verbs, eight appended
> feature bits; nothing existing moves):**
> - **New verb `stats`** — the board's state in one line, in the exact key shape
>   fpgahub's `shell_6900` provider already probes for (`_from_stats_verb`):
>   `up_ms` first, then identity, DFX, clock, reset, link, swap and counter keys,
>   plus five additive extras (`swap_err`, `clr_ok`, `dut_mhz`, `svc_max_us`,
>   `svc_skipped`). **No power keys**, by design. `up_ms` is the reboot witness.
>   See "Stats" below.
> - **New verb `log`** — the shell console, served from a 4 KiB RAM ring that
>   every `xil_printf` byte is teed into. Chunked exactly like `dutrx` (`off`,
>   `n`, `more`, hex `data`, and a `dropped` count on every reply), but
>   **non-destructive**: the client names the stream offset it wants. Closes the
>   power-on banner gap (the MCC opens the serial path ~1 s after the firmware
>   has printed); the banner is also re-printed once at +5 s. See "Log" below.
> - **New verb `touch_cal`** — get / set / reset the STMPE811 raw→pixel
>   calibration and read the last raw sample (for a three-point capture). RAM
>   only; declines on a build without `TOUCH=1`. See "Touch calibration" below.
> - **New verb `reboot`** — warm-restart the shell firmware through the shell
>   watchdog (`WDOG` @ `0x44B4_0000` → `proc_sys_reset` `aux_reset_in`). Replies
>   `{"ok":true,"in_ms":N}` **before** the watchdog is armed; refused mid-swap
>   (`EBUSY`) and on a shell with no watchdog. See "Reboot" below.
> - **`version.features` gains eight APPENDED bits** (5..12): `dut_egress`,
>   `jtag_server`, `xvc_dbgbr`, `xvc_jtagbb`, `stats`, `log`, `reboot`,
>   `touch_cal`. Bits 0..4 keep their names and positions, so a client that
>   compares the array for equality must update its expected value — the
>   `PRODUCT=1` image now reports
>   `["clcd","clcd_kvm","touch","hwicap_fifo","windowed","dut_egress","jtag_server","xvc_dbgbr","stats","log","reboot","touch_cal"]`.
> - **Request tokenizer bound** `MPS3_JSON_MAX_MEMBERS` 8 → 10 (a `touch_cal`
>   set is nine members). No wire change for any existing request.
> - **`MPS3_CTRL_RESP_MAX` is UNCHANGED at 1280.** Measured worst cases through
>   the real encoder: `stats` 514 B, `log` 593 B, `version` 282 B — `diag`
>   (1040 B) stays the binding case.
> - Firmware-side, same release (no wire change): the superloop serves 6900 in
>   between the heavy services (an "interleave" after `xvc` and `clcd`), touch
>   is sampled at most every 10 ms with every I2C wait bounded at 3 ms, and the
>   `clcd` service budget is 30 ms (was 200 ms). Together these fix the
>   2026-09-22 starvation, where a held touch RST every 6900 connection.

> **Changelog v0.10 (2026-09-14 — ADDITIVE, one new verb):**
> - **New verb `dutrx`: the DUT can answer.** It reads the frames the loaded
>   DUT **transmitted**, out of the shell's DUT-egress capture FIFO (DUTEGR @
>   `0x44B2_0000`, `fpga/shell/ip/dut_egress/`). Nothing existing moves; a
>   client that never sends `dutrx` sees an identical protocol.
> - **Why it was needed.** DUT *reception* has been silicon-proven since
>   2026-07-30 (`DFXCTL.RM_STATUS[2]` 0 → 1 under `gen_checker` traffic). The
>   return half was designed and never built: `shell_bd.tcl` SECTION 5 tied
>   `eth_bridge_3port`'s management egress to `mgmt_m_tready = 1` with nothing
>   behind it, so every frame the DUT transmitted was **drained into a
>   constant**. A DUT could be talked to and could not answer. The fabric half
>   closed on 2026-09-11; this verb is the host half.
> - **Chunked at 256 bytes, deliberately.** One reply carries at most
>   `MPS3_DUTRX_CHUNK_MAX` = 256 bytes of the head frame, hex-encoded, and
>   `more` says whether to ask again. That number is chosen so **`diag` stays
>   the case `MPS3_CTRL_RESP_MAX` is sized for**: the worst dutrx line measures
>   **693 B** (694 with the NUL) against diag's 1040, both through the real
>   encoder. A 512-byte chunk would have measured 1206 and made a frame chunk —
>   not the counter mailbox — the thing the buffer is sized for, so every future
>   diag counter would have been arguing with it. **`MPS3_CTRL_RESP_MAX` is
>   UNCHANGED at 1280.**
> - **The drop counters ride every reply, including empty ones.** The capture
>   block never backpressures the bridge (that parks the whole bridge — see
>   DUTEGR in `shell-regmap.md`), so it DROPS; `rx + drop_full + drop_giant` is
>   its tally against frames presented, and this reply is the only place a host
>   ever sees it. A frame read that did not report them would make a lossy
>   capture look lossless.
> - **On a bitstream WITHOUT the block it declines**, exactly like `display` on
>   a KVM-less shell: `{"ok":false,"err":"dut_egress not present"}`. It does not
>   answer `ok:true` with zeroes — "this fabric cannot capture" must not read the
>   same as "the DUT sent nothing", which is the reading-shaped lie v0.6 removed
>   from `telemetry`. The bare-metal rollback image `0x72BB0A36` is not that build: the first DUT
>   frames crossed its block on 2026-09-24
>   (`docs/evidence/2026-09-w3/sweep_20260924.txt`, proof 6).
> - Client: `pyverify.client.ShellClient.dutrx()` / `.read_dut_frame()` and the
>   `pyverify dut-eth` CLI verb. Reference server: `FakeShell._op_dutrx`, pinned
>   byte-for-byte against the firmware by
>   `tests/firmware_logic/test_fakeshell_conformance.py`.
>
> **Changelog v0.9.2 (2026-09-10 — ADDITIVE, `diag` only):**
> - **Thirteen new `diag` keys: the superloop service telemetry.** `svc_count`,
>   `pass_max_us`, `svc_max_us`, `svc_max_ix`, `svc_overruns`, `svc_skips`,
>   `svc_skipped`, `svc_us_0`…`svc_us_5` are APPENDED after `touch_verdict` in
>   `diag.h` X-macro order (mailbox **v8**, words +0x7C..+0xAC). Every existing
>   key keeps its name, value and position.
> - **Why.** The shell's superloop was a hand-written list of nine `_poll()`
>   calls with no notion of how long any of them took. When the board wedged the
>   mailbox could name the last QSPI phase and the last ICAP status, and nothing
>   at all could name the service that ate the pass — the first question a
>   superloop wedge asks. The loop is a TABLE now
>   (`firmware/common/service.h`), it times every service against a budget in
>   microseconds, and a service that overruns on `MPS3_SVC_SICK_K` = **3**
>   consecutive passes is SKIPPED (probed once every 100 ms until it behaves).
>   `svc_skipped != 0` is the headline: the loop has taken a service out of the
>   rotation, and nothing else in this reply says so.
> - **The MAILBOX GREW 128 → 256 B, so its JTAG base MOVED** (0x0003FF80 →
>   0x0003FF00 on the 256 KiB build; the struct is top-anchored to the LMB end).
>   No field offset changed. Both JTAG readers (`scripts/mps3_diag.tcl`,
>   `host/socket_harness/xsdb.py`) now scan BOTH anchors ascending and take the
>   read length from whichever hit, so a board still running a v5..v7 image is
>   read correctly by this tree's tooling — and the rows that image does not
>   carry are reported as absent rather than as zero.
> - **Budget: RAISED, deliberately.** `MPS3_CTRL_RESP_MAX` **768 → 1280**. At
>   42 counters, `sum(len(key))` = 439, so the worst case is
>   `10 + 439 + 42*14 + 2 + 1` = **1040 B**. At 768 the verb does not truncate —
>   `mps3_ctrl_encode_response()` returns −1 and the shell stops answering
>   `diag` entirely. Measured, not estimated, by
>   `firmware/test/test_net_proto_json.c` over an all-`0xFFFFFFFF` mailbox.
> - A shell built before v8 simply omits all thirteen keys and they read 0
>   client-side, which for such an image is the truth: it had no service table.
>
> **Changelog v0.9.1 (2026-09-10 — ADDITIVE, `diag` only):**
> - **Four new `diag` keys: the STMPE811 panel-continuity probe.**
>   `touch_regs`, `touch_adc_x`, `touch_adc_y`, `touch_verdict` are APPENDED
>   after `ovlstore_detail` in `diag.h` X-macro order (mailbox v7, words
>   +0x6C..+0x78 — the struct is still 128 B; `reserved[]` shrank from five
>   words to one). Every existing key keeps its name, value and position.
> - **Why.** On silicon the touch controller reports itself healthy in every
>   way we could measure and still never reports a contact, so "the chip is not
>   configured the way we think" and "the panel is not connected" were
>   indistinguishable. `touch_init()` now reads five decisive registers back and
>   probes the four panel lines for continuity, once, before enabling the TSC;
>   these words carry that answer. `touch_verdict` is it: **0** unknown,
>   **1** chip-misconfigured, **2** panel-open, **3** panel-present. The other
>   three are its raw evidence and are best read as HEX
>   (`firmware/touch/touch.h` documents every bit field).
> - **Budget.** Unchanged at `MPS3_CTRL_RESP_MAX` **768**: 29 counters at max
>   width measure 737 B on the wire, 738 with the NUL. Measured, not estimated,
>   by `firmware/test/test_net_proto_json.c`.
> - A shell built without `TOUCH=1` still emits all four keys; they read 0,
>   which decodes as verdict "unknown" — no probe ran.
>
> **Changelog v0.9 (2026-09-10 — ADDITIVE, `diag` only):**
> - **`diag` now carries the whole mailbox: 25 counters, not 14.** The 14
>   existing keys keep their names and their order; the 11 counters that were
>   readable only over JTAG-MDM (`tx_*`, `icap_sr_last`, `icap_eos_status`,
>   `ovlstore_*`) are APPENDED in `diag.h` X-macro order. A client that reads
>   by key sees nothing change; a client that counted keys must not.
> - **Why.** The key list was a hand-written 14 in `net_proto.c` while the
>   mailbox (`diag.h`) had grown to 25; the emitter is now generated from the
>   same `MPS3_DIAG_FIELDS` X-macro that lays out the struct, so the two cannot
>   drift again (`scripts/harness_gates/check_diag_field_parity.py`).
> - **Budget.** `MPS3_CTRL_RESP_MAX` is now **768** (was 384): the 25-key
>   worst case measures 637 B, and at 384 the verb would not have truncated —
>   it would have returned −1 and stopped answering. Measured by
>   `firmware/test/test_net_proto_json.c`.
>
> **Changelog v0.8 (2026-09-09 — ADDITIVE, no existing verb changes):**
> - **New verb `version`.** A running image can now say what it *is*: harness
>   release, packed `HARNESS_VER32`, build sha + dirty flag, the LMB size it was
>   **linked** for, and its **compile-time feature set**. Nothing existing moves;
>   a client that never sends `version` sees an identical protocol.
> - **Why it was needed.** `ping` answers "which FABRIC is this" (`static_id`)
>   and "what is in the RP" (`rm_id`) — neither identifies the FIRMWARE, and the
>   axes are genuinely independent: one `static_id` serves many harness releases,
>   because a firmware-only bump re-bakes the bitstream through `updatemem`
>   without touching the static routing. So a board could report the expected
>   `static_id`, pass every acceptance gate, and be running an image built with
>   the wrong flags. That is not hypothetical — an image built without
>   `HWICAP_FIFO=1` against a FIFO shell pings, reports the right `static_id`,
>   passes the gate, and silently loads no RM. `version` makes that visible over
>   the wire in one request.
> - **`diag` is documented at last.** The verb has been in the firmware (and in
>   `pyverify.ShellClient.diag()`) since the over-the-wire-reconfig work; it had
>   no section here and no implementation in the reference `FakeShell`, which
>   answered `unknown op 'diag'`. Both gaps are closed in this version: the
>   section is below, the fakeshell implements it, and
>   `tests/firmware_logic/test_fakeshell_conformance.py` now sends `diag`,
>   `version` and `display` so neither can drift again unnoticed.
> - **First response shape carrying a JSON array** (`version.features`). The
>   REQUEST grammar is unchanged: requests are still flat one-line JSON objects
>   with no arrays or nesting (`net_proto.h`'s tokenizer guarantees stand).

> **Changelog v0.7 (2026-07-14 — BREAKING, swap ordering):**
> - **The bitstream push happens INSIDE the swap: `swap` -> push -> reply.**
>   This **reverses** the v0.2 "push-vs-swap staging" clause, which had the host
>   push the pair *before* `swap`. The shell only listens for a bitstream once
>   the `swap` RPC has driven its FSM into `SWAP_AWAIT_*` — which is *why* it
>   parks the 6900 control connection — and it **resets** a push that arrives at
>   any other time. See "Push-vs-swap ORDERING (v0.7)" below for the normative
>   text and the wire diagram.
> - The old order was not a documentation slip: pyverify shipped it, and its
>   FakeShell test double enforced it too, so the whole E2E suite stayed green
>   against a fake more permissive than the firmware. Corrected against the
>   KU115 on 2026-07-14.

> **Changelog v0.6 (2026-07-14 — BREAKING, `telemetry` only):**
> - **`telemetry` now always returns `{"ok":false,"err":"no power sensor",
>   "lockup":<bool>}`.** The `mv` / `ma` keys are **REMOVED** from the protocol.
>   They were never measurements: this platform has no power sensor reachable by
>   any path (TELEM's sample inputs are tied to ground in the block design, its
>   INA228 I2C engine was never written, its pads are not on the top level, and
>   the MPS3 MCC refuses voltage reads), so they reported a permanent, plausible
>   zero. See "Telemetry" below for the full evidence. **Fail loudly beats a
>   reading-shaped lie.**
> - `lockup` is **unchanged on the wire** — same key, same raw
>   `DFXCTL.RM_STATUS.dut_lockup` pin, no filtering — but is now **documented**
>   as meaningful *only for RMs that actually drive it* (most tie it to 0). The
>   shell reports the pin; the host decides per RM what it means.
> - Declares the single carve-out to the uniform failure shape (`telemetry`'s
>   failure line carries `lockup`), since `telemetry` has no success line at all.
> - **Client impact:** anything reading `mv`/`ma` must be updated. The host half
>   **has been** (same working tree): `pyverify`'s `TelemetryResponse` has had
>   `mv`/`ma` **removed** (not zero-defaulted) and
>   `pyverify.testing.fakeshell._op_telemetry` now emits the v0.6 failure shape
>   byte-identically to `net_proto.c`. Both servers and the client are conformant;
>   the byte-conformance gate is green. See the (now closed) hand-off note in
>   "Telemetry" below.

> **Changelog v0.5 (2026-07-09, A3+A4 — additive, opt-in, OFF by default):**
> - **Windowed flow control on raw-TCP 6910** — an OPTIONAL application-level
>   ack/window handshake on the 6910 push, added to kill the over-the-wire
>   streaming stall (HW-pivot root cause: the lwIP/TCP **receive** path under a
>   fire-and-hose push, not the MAC or the ICAP). The host sends the 24-byte
>   header then the payload in fixed-size chunks, blocking for a 1-byte grant
>   (`0x06`) from the shell **inline on the same 6910 socket** after each chunk;
>   the shell emits that grant only once the chunk has **drained to the sink**
>   (HWICAP.WF / QSPI page-program complete), and the final grant only **after
>   finish** (EOS + CRC + staging succeed). Both ends are a **matched pair** and
>   must agree on the chunk size; the mode is **opt-in on both ends** and the
>   default remains fire-and-hose (byte-identical when off). See the new
>   "Windowed 6910 (opt-in)" subsection. Firmware flag `MPS3_CFG_AGENT_WINDOWED`
>   + `CFG_AGENT_ACK_WINDOW_BYTES`; host `tcp_send_windowed()` /
>   `BitstreamPusher(windowed=True, window=…)`.
>
> **Changelog v0.4 (2026-07-07, A3+A4 — additive, I10 tail):**
> - **`macgen` control verb added** — the control-plane companion to the
>   ethernet MAC-in-operation subsystem. Drives the shell's error-inject
>   gen/checker (shell-regmap.md **GENCHK** @ `0x44A6_0000`): enables the
>   generator and checker (`CTRL.gen_en`/`chk_en`), arms the next-frame fault
>   from the INJECT set (`none`/`bad_fcs`/`runt`/`giant`/`ifg`/`dribble` →
>   `INJECT`), and reads back the three counters (`TX_CNT`/`RX_CNT`/`ERR_CNT`).
>   Resolves the shell-regmap GENCHK "add a `{"op":"macgen",…}` verb" TODO
>   (I10 tail). See the new "MAC gen/checker control" section below.
>   (No net-protocol v0.3 was cut — the number is skipped to track
>   shell-regmap **v0.3**, which froze the GENCHK block this verb drives.)
>
> **Changelog v0.2 (2026-07-06, A6 reconcile of the wave's ambiguity flags —
> all additive clarifications, no semantic reversals):**
> - **Raw-TCP 6910 completion convention** specified (close-after-validation,
>   no in-band accept/reject byte); TFTP is the transport with a real
>   accept/reject signal.
> - **TFTP filenames are advisory**; the 24-byte header is authoritative.
> - **`link` event values enumerated**: `down` / `up` / `pulse`, fail-closed.
> - **Pair coherence rule**: within a pushed pair, `clearing.rm_id` must
>   equal `partial.rm_id`.
> - **Identity string rendering** on the control channel: `0x` + 8 lowercase
>   hex digits; parsers must compare numerically (case-insensitive).
> - **`swap.src` is required** (`"tftp"` or `"tcp"`), no default.
> - **Swap response timing**: one request→response pair; the response is held
>   until the server-side sequence completes.
> - ~~**Staging requirement**: the shell must be able to hold one full
>   validated `{clearing, partial}` pair before `swap` arrives.~~
>   **SUPERSEDED BY v0.7** — this is the clause that was wrong. The pair is
>   pushed *into* an already-parked swap, not staged ahead of one. See
>   "Push-vs-swap ORDERING (v0.7)".
>
> v0.1 (2026-07-04): I2/I4 resolutions, I12 len units, I13 CRC-32 variant.

## TCP/UDP port map
| Port | Proto | Service | Owner |
|---|---|---|---|
| 6900 | TCP | **control/status** — JSON-line request/response (swap, reset, clock, RM-id, telemetry) | A3 coordinator |
| 69 | UDP | **TFTP** — partial/clearing bitstream push (D3 default) | A3 config_agent |
| 6910 | TCP | raw partial push (alt to TFTP; D3) — optional windowed flow control (v0.5); v0.14: slot images (kind 2, Linux harness) | A3 config_agent |
| 2542 | TCP | **XVC** — Xilinx Virtual Cable → Debug Bridge (ILA); Linux harness: board-local only once claimed (`xvc_lock`, "The lock") | A3 xvc_server |
| 6920 | TCP | **RETIRED — nothing listens.** Was SWD, OpenOCD `remote_bitbang`. Not built into any image since the SWD→JTAG cutover (`0xCD74B6AE`): `firmware/platform/Makefile` builds `swd_server.c` only with `LEGACY_SWD=1`, and nothing calls `swd_server_init()`. Use 6921. | (A3 swd_server, source kept for its host test) |
| 6921 | TCP | **JTAG** — OpenOCD `remote_bitbang` (JTAG bridge; `nanosoc_mps3_jtag.cfg`); Linux harness: board-local only once claimed (`xvc_lock`) | A3 jtag_server |
| 6930 | TCP | **UART0** (boot monitor) — raw byte stream | A3 uart_over_eth |
| 6931 | TCP | **UART1 — INERT.** Accepts a client on both engines, but no DUT byte ever arrives and host bytes go nowhere: `shell_bd.tcl` ties the bridge's `uart1_*` seam off (TX valid 0, RX ready 1), and UART1 is not a partition pin. | A3 uart_over_eth |
| 6932 | TCP | **SWO/ITM** trace — raw byte stream | A3 uart_over_eth |
| 6899 | UDP | **identify** — one datagram in, one out, to the sender (v0.11). **Linux harness only** (harnessd, and stage0's rescue responder); the bare-metal image does not register it | firmware/identify |
| 6940 | TCP | **LCD mirror** — the panel's frame buffer, 127.0.0.1 ONLY (v0.15, Linux harness) | mps3-lcdmirror |
| 22 | TCP | **SSH** (dropbear, key-only) — Linux harness only; the claim, and the port-forwards to the 127.0.0.1-only services, ride it | Linux image |

The rows for 6920 and 6931 stay so that an old client's failure has an
explanation; they are not services (corrected 2026-09-29, no wire change).

Config = static IP or DHCP (D8, site-dependent); default static
`192.168.10.101` (matches the existing fpgahub MPS3 board block). v0.16: on the
Linux harness each board's static address is its own ("Identity": the /persist
override, else its stage0 bake, else that default).

## Console streams (TCP 6930-6932) — relay policy
Raw bytes, one client per port. Bare metal (v0.11): with no client the bridge
is not read and the DUT's UART back-pressures; bytes in the bridge at a swap
reach the next client. Linux harness (impl "linux"): with no client, DUT output
is drained and dropped (a client sees output from its connect on), and every
swap's ungate flushes the relay, so nothing read after a swap came from the
previous RM. Counts are in the harness log, not on the wire. Input: the DUT's
RX has no FIFO or flow control; pace host->DUT bytes ~20 ms/char (client side;
harnessd --uart-pace-ms N can enforce it, default off).

## Control channel (TCP 6900) — JSON lines
One JSON object per line (`\n`-terminated), request → response. Core verbs:

```
→ {"op":"ping"}                              ← {"ok":true,"shell_id":"<static_id>","rm_id":"<id>"}
→ {"op":"reset","target":"dut"}              ← {"ok":true}
→ {"op":"set_clk","preset":"25mhz"}          ← {"ok":true,"locked":true}
→ {"op":"swap","rm":"nanosoc","src":"tftp"}  ← {"ok":true,"rm_id":"<id>","verified":true}
→ {"op":"link","event":"down"}               ← {"ok":true}        (VPHY link injection)
→ {"op":"commit","rm":"nanosoc","src":"tcp",...}  ← {"ok":true,"slot":"B"}  (v0.13 re-push into the user microSD; see "User microSD")
→ {"op":"usd"}                               ← {"ok":true,"present":true,"state":"valid",...}  (v0.13; see "User microSD")
→ {"op":"telemetry"}                         ← {"ok":false,"err":"no power sensor","lockup":false}   (ALWAYS ok:false — see below)
→ {"op":"macgen","gen":true,"chk":true,"inject":"bad_fcs"} ← {"ok":true,"tx":N,"rx":N,"err":N} (GENCHK gen/checker + counters)
→ {"op":"display","owner":"dut"}             ← {"ok":true,"owner":"harness"}  (CLCD KVM remote flip; see below)
→ {"op":"diag"}                              ← {"ok":true,"rx_recover":N,...}  (25 counters; see below)
→ {"op":"version"}                           ← {"ok":true,"harness":"1.0.0","ver32":"0x01000001",...}  (v0.8; see below)
→ {"op":"dutrx"}                             ← {"ok":true,"len":N,"off":N,"n":N,"more":false,...,"data":"<hex>"}  (v0.10; DUT egress, see below)
→ {"op":"stats"}                             ← {"ok":true,"up_ms":N,"sid":"0x…","rm":"0x…",...}  (v0.11; see below)
→ {"op":"log","off":0}                       ← {"ok":true,"off":N,"n":N,"more":B,"dropped":N,"data":"<hex>"}  (v0.11)
→ {"op":"touch_cal","act":"get"}             ← {"ok":true,"ax":N,"bx":N,"cx":N,"ay":N,"by":N,"cy":N,"shift":N}  (v0.11)
→ {"op":"reboot"}                            ← {"ok":true,"in_ms":N}  (v0.11; then the shell restarts)
→ {"op":"slot","act":"status"}               ← {"ok":true,"card":true,"running":"A","default":"A",...}  (v0.14; Linux harness, see "Slot images")
→ {"op":"identity"}                          ← {"ok":true,"op":"identity","label":"MPS3-02",...}  (v0.16; Linux harness, see "Identity")
→ {"op":"identity_set","ip":"192.168.10.7/24"} ← {"ok":true,"op":"identity_set","persisted":true,"pending":{...},"applies":"reboot"}  (v0.16)
→ {"op":"locate","s":10,"who":"alice@pc"}    ← {"ok":true,"op":"locate","until_ms":10000}  (v0.16; Linux harness, see "Locate")
→ {"op":"hello","v":1,"sid":"a1b2c3d4",...}  ← {"ok":true,"op":"hello","sessions":1,"panel":{...},"events":[...]}  (v0.17; Linux harness, see "Presence and the panel")
→ {"op":"panel"}                             ← {"ok":true,"op":"panel","page":"status","owner":"harness",...}  (v0.17)
→ {"op":"panel","frame":"a"}                 ← {"ok":true,"op":"panel","frame":"a","theme":"today","rows":[...8],"roles":"..."}  (v0.17)
```

Verb argument rules (v0.2):
- **`swap.src` is required** — `"tftp"` or `"tcp"`, naming the transport the
  pair arrived by. There is no default; a `swap` without `src` is rejected
  (`bad args`). Clients must always send it.
- **`link.event`** ∈ `"down"` | `"up"` | `"pulse"`: `down` asserts
  `VPHY.LINK_EVENT.force_down`, `up` releases it, `pulse` fires the
  self-clearing `LINK_EVENT.pulse` (one link-down/up blip). Anything else is
  rejected (fail closed). No speed-change event is defined in this version.
- **`macgen`** takes three required arguments: `gen` (bool) and `chk` (bool)
  set `GENCHK.CTRL.gen_en`/`chk_en`; `inject` (string) selects the next-frame
  fault written to `GENCHK.INJECT`, one of `"none"` (clear — no fault armed) |
  `"bad_fcs"` | `"runt"` | `"giant"` | `"ifg"` | `"dribble"`. Any other
  `inject` string is rejected (fail closed). A missing/wrong-typed `gen`,
  `chk`, or `inject` is `bad args`. The response echoes the three GENCHK
  counters after the write: `tx` (`TX_CNT`, frames generated), `rx`
  (`RX_CNT`, DUT frames checked), `err` (`ERR_CNT`, frames failing the
  checker). See "MAC gen/checker control" below.
- **`display`** takes one required argument `owner` (string) ∈ `"harness"` |
  `"dut"` | `"toggle"` | `"query"`. It is the **remote** arm of the CLCD KVM's
  three converging request sources (the `USER_nPB[1]` button, a local CSR write,
  and this verb); all three drive the same frozen `CLCDKVM.CTRL.src_sel`
  (+ `src_sel_we`). A missing/wrong-typed `owner` is `bad args`; any other
  string is `bad owner`. See "Display" below.
- **`log`** (v0.11) takes one OPTIONAL integer `off` ≥ 0 — the stream offset
  to read from (absent = 0 = "the oldest byte still retained"). A negative or
  non-integer `off` is `bad args`.
- **`touch_cal`** (v0.11) takes a required string `act` ∈ `"get"` | `"set"` |
  `"raw"` | `"default"`; `set` additionally requires all seven integers `ax`,
  `bx`, `cx`, `ay`, `by`, `cy`, `shift` (missing or wrong-typed = `bad args`;
  out-of-range values are rejected by the handler with a named reason, below).
  Any other `act` is `bad act`.
- **`slot`** (v0.14) takes a required string `act` ∈ `"status"` | `"commit"` |
  `"rollback"` | `"verify"` and an OPTIONAL string `slot` ∈ `"A"` | `"B"`. A
  missing/non-string `act`, or a non-string/over-long `slot`, is `bad args`; any
  other `act` is `bad act`; any other `slot` is `bad slot`. These three checks
  are made on every engine, before the engine's provider sees the request.
- **`ping`, `telemetry`, `diag`, `version`, `dutrx`, `stats` and `reboot` take
  NO arguments.**
  Extra keys are ignored like anywhere else. `dutrx` in particular takes no
  offset or length: the capture block's `DATA` port is a **destructive** read,
  so the FIFO is its own cursor — there is nothing for a client to track and no
  per-connection state on the shell.
- Unknown *extra* keys in any request are ignored (tolerant JSON); unknown
  `op` values and missing required arguments are rejected.

**Identity rendering:** `shell_id` / `rm_id` values on this channel are JSON
strings rendered `"0x"` + exactly 8 **lowercase** hex digits of the u32
(e.g. `"0x0000001e"`). Parsers must treat them numerically (accept any case);
build artefacts (`static_id.txt`, manifests) may render the same u32 with
uppercase digits — string equality across artefacts is NOT the comparison,
u32 equality is.

**Request/response discipline:** strictly one JSON-line response per
request, in order, on the same connection. For `swap` the response is
**held**: the server does not reply until the whole server-side sequence
(steps 1–7 below) has completed, then sends the single response carrying the
confirmed `rm_id` + `verified` (or `{"ok":false,...}`). No interim progress
messages are defined; clients must use a generous per-request timeout for
`swap` (a partial load through HWICAP takes seconds). Failure responses are
`{"ok":false,"err":"<human-readable diagnostic>"}` (I31 RESOLVED 2026-07-07:
the diagnostic key is **`err`**, matching the firmware and the reference
`pyverify.testing.fakeshell`). **Uniform failure shape (2026-07-08):** both
reference implementations — the firmware's `mps3_ctrl_encode_response()` and
`pyverify.testing.fakeshell` — emit **exactly** `{"ok":false,"err":"…"}` on a
failure of any verb, with **no** per-verb steady-state fields (no `set_clk` →
`"locked":false`, no `swap` → `"verified":false`; the earlier "a verb *may*
echo them" allowance is withdrawn — the two are now byte-conformant, pinned by
`tests/firmware_logic/test_fakeshell_conformance.py`). **Clients must key on
`ok` alone** and treat `err` as advisory; the response dataclasses already
default the per-verb fields (`locked`/`verified`/`rm_id`) to False/"" when they
are absent, which they always are on failure. `link.event` accepts
`up`/`down`/`pulse`; speed events are undefined and rejected.

> **The one declared exception to the uniform failure shape (v0.6):
> `telemetry`.** Its failure line — which is its *only* line — additionally
> carries `"lockup"`. `telemetry` has no success shape at all (there is no power
> sensor on this platform; see "Telemetry" below), so the rule's premise, that
> a failing verb has nothing to report, does not hold for it: its `lockup` half
> is a real pin with a real value even though its power half has no sensor
> behind it. This is a deliberate, single, documented carve-out — **no other
> verb may add fields to a failure line**, and clients still key on `ok` alone.

## Telemetry (`telemetry`) — **there is no power sensor** (v0.6)

```
→ {"op":"telemetry"}   ← {"ok":false,"err":"no power sensor","lockup":false}
```

**This verb always fails, by design.** It has no success shape. `mv` and `ma`
are **not** keys of this protocol — they are *absent*, not zero.

### Why (all four independently fatal, and all citable)

This platform has **no power sensor reachable by any path**. The `telemetry`
verb used to return `{"ok":true,"mv":0,"ma":0,...}` — reading the TELEM block's
`BUS_MV` / `CURR_UA` registers, which are **permanently zero**:

1. **The TELEM block's data inputs are tied to ground in the block design.**
   `fpga/shell/bd/shell_bd.tcl` wires `gnd_ina228_32` → `ina228_bus_mv_i` /
   `ina228_curr_ua_i` / `ina228_power_mw_i`, and `gnd_ina228_1` →
   `ina228_sample_valid_i` / `i2c_err_i` / `alarm_i`.
2. **The INA228 I2C engine was never written.** `fpga/shell/ip/telem/README.md`
   marks it *"SEAMED (follow-up module, **NOT yet written**)"*. That README is
   itself explicit that with no engine attached the readings "stay 0 … visibly
   *no data source*, never plausible garbage" — which is exactly the promise the
   old handler broke by shipping those zeroes onward as a measurement.
3. **The I2C pads are not on the top level.** `fpga/shell/shell_top.sv` records
   the INA228 bus as **MCC-owned on MPS3**, so no fabric path to the part exists
   for a future engine to drive.
4. **The MPS3 MCC console refuses voltage reads.** `CFG R V <dev>` answers
   `ERROR: Unable to perform requested function` for **every** device (confirmed
   on the real board, 2026-07-14). The one remaining conceivable path is closed.

A zero that is shaped like a reading is worse than no reading: it is
indistinguishable from a genuine 0 mV / 0 mA measurement, and a host charting it
plots a flat line rather than showing a missing sensor. The verb therefore fails
**loudly**, in the protocol's ordinary error shape.

If an INA228 engine is ever written *and* un-grounded *and* given pads, this
contract and `coordinator_handle_telemetry()` are what must change together. The
firmware codec enforces that: `mps3_ctrl_encode_response()` has **no `ok:true`
telemetry shape to encode** and returns an error if asked for one, so a
fabricated reading cannot be reintroduced by accident.

### `lockup` — reported raw; meaningful only for RMs that drive it

`lockup` **is** a real value and is **not** suppressed. It is the
`DFXCTL.RM_STATUS.dut_lockup` bit, driven across the partition boundary by the
loaded RM. It rides on the failure line above (the declared carve-out to the
uniform failure shape), and clients read it regardless of `ok`.

**The shell reports the pin. It does not editorialise.** But the pin is only as
good as the RM behind it, and **most RMs do not drive it**:

| RM wrapper | drives `dut_lockup`? |
|---|---|
| `rp_nanosoc_multicore_wrapper.sv:212` — `assign dut_lockup = network_core_lockup_w \| chip_core_lockup_w;` | **yes — real** (ORs both cores) |
| `rp_nanosoc_wrapper.sv:307` — `assign dut_lockup = 1'b0;` | no — hard-tied 0 |
| `rp_eth_ss_wrapper.sv:166` — `assign dut_lockup = 1'b0;` | no — hard-tied 0 (":124 // no CPU to lock up") |
| single-file OOC RMs (`greybox`, `led`, `regdemo_a`/`_b`, `uart_echo`) | no — no CPU to lock up |

So `"lockup":false` from an RM that ties the pin off means **"this RM cannot
report lockup"**, *not* "the DUT is healthy" — the two are indistinguishable on
the wire, and the shell has no business guessing which one it is looking at.

**Deciding whether the pin means anything is the HOST's job, per RM**, via a
`supports`-style declaration in the host's RM catalogue. Only an RM that appears
in the "yes" row above may have its `lockup` treated as evidence of DUT health;
for every other RM the field must be treated as *unknown*, not as *false*. A
host that reads `"lockup":false` from `nanosoc` and concludes "the DUT is fine"
has drawn a conclusion from a tie-off.

### Host-side follow-up — **DONE** (v0.6 hand-off closed)

The firmware is the reference server; the host half has now been brought into
conformance with it. Both halves emit/parse the v0.6 shape, and the
byte-conformance gate is green.

- `host/pyverify/pyverify/client.py` — **`mv`/`ma` are GONE from
  `TelemetryResponse`.** Not defaulted, not `Optional`: *removed*, so there is no
  attribute for a caller to misread (`resp.mv` is an `AttributeError`). The old
  `float(resp.get("mv", 0.0))` parse would have handed every caller a permanent
  `mv=0.0` alongside `ok=False` — the reading-shaped lie this change exists to
  kill, resurrected host-side. The dataclass is now `ok` / `err` / `lockup`;
  `err` is parsed (telemetry's `err` is unambiguously the diagnostic — unlike
  `macgen`'s polymorphic one — because there is no success shape for it to mean
  anything else in) and `lockup` is parsed regardless of `ok`.
- `host/pyverify/pyverify/testing/fakeshell.py` — `_op_telemetry()` emits
  `{"ok":false,"err":"no power sensor","lockup":<pin>}`, byte-identical to
  `net_proto.c` (same diagnostic, same key order). The `telemetry_mv` /
  `telemetry_ma` **seeds were removed too**: a fake shell that can still be
  handed a rail voltage is modelling hardware that does not exist. Only
  `telemetry_lockup` remains.
- Tests re-pinned to the new bytes (they were *supposed* to fail — the gate did
  its job): `test_fakeshell_conformance.py`'s `telemetry` row still asserts
  **byte-identical** firmware-vs-fakeshell lines (the strictest row in that
  table — telemetry's diagnostic is contract-fixed, so no `err` value delta is
  allowed), plus a new
  `test_telemetry_is_the_only_carve_out_to_the_failure_shape` pinning the
  carve-out at exactly one verb and exactly one extra key.
  `test_json_golden.py::test_telemetry_floats_parse_from_integer_wire_values`
  was **repurposed**, not deleted, into
  `test_telemetry_always_fails_no_power_sensor`: its old subject (float parsing
  of integer `mv`/`ma`) no longer exists, but `telemetry` still needs a golden
  pair in a file whose structure is one pair per verb — and it is the verb that
  just changed. It now asserts `mv`/`ma` are absent from **both** the wire and
  the dataclass, which is what fails if anyone re-adds a zero-defaulted power
  field to either half.

**Host consumers of `mv`/`ma`: none remain.** `pyverify.board.Mps3Board.telemetry`
is a passthrough (documented as always-failing); `host/notebooks/demo.md` and
`host/README.md` printed `telemetry.mv`/`.ma` in their "check" step and now read
the `lockup` pin instead, with the pass/fail verdict explicitly sourced from the
console transcript. The FPGAhub stats-API plan (an internal note, not in the
public tree) already documents `mv`/`ma` as dead silicon and renders them
`unsupported`, so that (unbuilt) API needs no change.

## MAC gen/checker control (`macgen`) — I10 tail
`{"op":"macgen","gen":<bool>,"chk":<bool>,"inject":"<fault>"}` is the
control-plane driver for the shell's error-inject traffic generator/checker
(shell-regmap.md **GENCHK** @ `0x44A6_0000`). It is the host-facing companion
to the ethernet MAC-in-operation subsystem: the coordinator drives GENCHK's
`CTRL`/`INJECT` registers and reads its counters, so a MAC test can be scripted
end-to-end from Python (`pyverify.mactest`) without touching the DUT firmware.

Server behaviour of one `macgen` request (all in a single request→response,
no held response — the register pokes are immediate):
1. `GENCHK.CTRL` ← `(gen ? gen_en : 0) | (chk ? chk_en : 0)`.
2. `GENCHK.INJECT` ← the one-hot bit for `inject` (`none` → `0`, clearing any
   previously-armed fault). `INJECT` arms the fault for the **next** generated
   frame (regmap: "fault to inject on next frame").
3. Read `GENCHK.TX_CNT`, `RX_CNT`, `ERR_CNT` and return them as `tx`/`rx`/`err`.

```
→ {"op":"macgen","gen":true,"chk":true,"inject":"none"}     ← {"ok":true,"tx":N,"rx":N,"err":0}
→ {"op":"macgen","gen":true,"chk":true,"inject":"bad_fcs"}  ← {"ok":true,"tx":M,"rx":M,"err":1}
```

**`err` key is polymorphic by `ok`** (intentional, per this contract's
uniform failure shape): on **success** `err` is the *integer* `ERR_CNT`
counter; on **failure** `err` is the *string* diagnostic (`{"ok":false,
"err":"bad inject"}` for an unknown fault, `"bad args"` for a missing/
wrong-typed `gen`/`chk`/`inject`). Clients **must key on `ok`** and only read
`tx`/`rx`/`err`-as-counter when `ok` is true (as every client here does).
Flag for A6 if a distinct counter key (e.g. `err_cnt`) is preferred to avoid
the overload.

**Counter semantics (contract with the GENCHK RTL owner).** The three counters
are RTL-owned frame tallies; `macgen` only *reads* them. They are **not
strictly monotonic across an enable toggle**: the landed `gen_checker` RTL
**clears all three counters on a `gen_en`/`chk_en` 0→1 rising edge** (a v1
convenience — see shell-regmap.md v0.4 "Counter semantics", the authoritative
statement). Within an enabled session (the enables held high) the counters then
advance monotonically. So a MAC test **enables once, snapshots, and reasons
about *deltas* within that session** — never assuming a fresh absolute zero
mid-session, and never comparing across an off→on toggle. The normative
expectations a real `gen_checker` must honour for the `pyverify.mactest`
scenario to mean anything:
- with `gen_en` set, `TX_CNT` advances as frames are generated;
- with `chk_en` set, `RX_CNT` advances as looped-back/DUT frames are checked;
- `ERR_CNT` advances **only** for frames that fail the checker — so a clean run
  (`inject:"none"`) leaves `ERR_CNT` unchanged, and an armed `inject` makes the
  next frame fail, advancing `ERR_CNT`;
- enabling either generator or checker from a disabled state (0→1) resets all
  three counters to zero (the RTL's clear-on-enable-rise).
There is deliberately **no counter-clear register or verb** in this version —
the only clear is the automatic enable-rise one above (a MAC test that needs a
zero baseline simply toggles the enable off then on). The reference
`pyverify.testing.fakeshell` models this clear-on-enable-rise, and
`host/pyverify/tests/test_fakeshell.py` pins it. **No INJECT self-clear
semantics are pinned here** — whether `INJECT` auto-clears after faulting one
frame or persists until the next `macgen` write is an RTL detail the
coordinator does not depend on (it rewrites `INJECT` every call).

## DUT egress (`dutrx`) — the frames the DUT TRANSMITTED (v0.10)

```
→ {"op":"dutrx"}
← {"ok":true,"len":1514,"off":0,"n":256,"more":true,"last":false,"frames":2,
   "rx":7,"drop_full":0,"drop_giant":0,"ovf":false,"desync":false,
   "data":"ffffffffffff001122334455080600010800..."}
```

(One line on the wire; wrapped here for reading.)

`dutrx` reads the frames the loaded DUT **transmitted**, out of the shell's
DUT-egress capture FIFO (`fpga/shell/ip/dut_egress/`, shell-regmap.md
**DUTEGR** @ `0x44B2_0000`). It is the return half of Option C
(`docs/DUT_ETHERNET_EGRESS.md`): DUT *reception* has been silicon-proven since
2026-07-30, while every frame the DUT sent used to arrive at
`eth_bridge_3port`'s management egress and be **drained into a constant**.

**No arguments, and none are coming.** The block's `DATA` register is a
**destructive** read — one byte per read, popping the FIFO — so the FIFO is the
cursor. There is no offset for a client to send, no length to negotiate and no
per-connection state on the shell. **Every reply CONSUMES what it carries**: a
reply lost in flight is a frame lost, which is why this rides the reliable 6900
stream and is not a datagram. Never held (unlike `swap`), and like every 6900
verb it is unreachable while a swap has the control connection parked.

### The reply, key by key

| key | type | meaning |
|---|---|---|
| `len` | int | the head frame's **total** length in bytes, FCS included. 0 when no frame was waiting |
| `off` | int | how many bytes of that frame were delivered **before** this chunk |
| `n` | int | bytes in `data` (0 … 256) |
| `more` | bool | bytes of **this** frame remain — ask again |
| `last` | bool | the `DATA[9]` LAST bit on this chunk's final byte: the block's **second, independent** record of where the frame ends |
| `frames` | int | committed frames still waiting **after** this chunk |
| `rx` | int | `DUTEGR.RX_FRAMES` — frames captured whole |
| `drop_full` | int | frames dropped for want of room |
| `drop_giant` | int | frames dropped for exceeding `MAX_FRAME` (1536) |
| `ovf` | bool | `STATUS.OVF`, sticky: something was dropped |
| `desync` | bool | `STATUS.DESYNC`, sticky: the two end-of-frame records disagreed |
| `data` | string | `n` bytes as **lowercase hex**, `2*n` characters, `""` when `n` is 0 |

**The key set never changes.** Every success reply carries the same thirteen
keys — `ok` plus the twelve above. An empty FIFO answers all of them, with
`len`/`off`/`n` at 0 and `data` `""`, so a client branches on `n` and `more`,
never on shape.

### Reading a frame

Send `dutrx`; if `n` is 0 the FIFO was empty (a **result**, not an error — an
idle DUT looks exactly like that). Otherwise append `data` and repeat while
`more` is true; each further chunk's `off` must equal the bytes you already
hold, and `len` must not change. The block is **store-and-forward**, so a frame
is visible only once all of it is committed: the length cannot move under the
read and a torn frame can never be observed. That is what makes the chunked read
safe to leave stateless.

A chunk that does not continue the previous one, or `more` with `n == 0`, is a
**protocol error** — both produce a frame that reassembles into something
plausible and wrong, or a loop that never ends. The reference client raises
rather than splicing (`pyverify.client.ShellClient.read_dut_frame`).

`last` is reported raw beside the length rather than reconciled with it. They
are two independent records of the same fact, and their disagreement is exactly
what latches `desync` in the hardware; a client that trusted one and discarded
the other would throw away the check.

### Read the drop counters. They are not decoration.

The capture block's AXIS `tready` is a **constant 1** and must stay one:
`eth_bridge_3port` has one shared store-and-forward sequencer with head-of-line
blocking, so a sink that backpressures this port does not apply flow control —
it **parks the whole bridge**, killing the DUT's ingress scoring and the LAN9220
uplink along with the egress it was trying to protect. The block therefore
**drops**, and the entire status surface exists so that it never drops
*silently*:

```
rx + drop_full + drop_giant  ==  frames presented while capture was enabled
```

`rx`, `drop_full`, `drop_giant`, `ovf` and `desync` therefore ride **every**
reply, including empty ones — a client polling an idle board would otherwise
never learn it had lost frames. A host that reads frames without reading these
is counting only what survived.

### Chunk size: 256 bytes, and why it is not larger

`MPS3_DUTRX_CHUNK_MAX` = **256** (`firmware/common/net_proto.h`). The two ends
are a **matched pair**: a client that assumed a different chunk would compute
`off` arithmetic no shell uses.

The number is chosen so that **`diag` remains the case
`MPS3_CTRL_RESP_MAX` is sized for**. Measured through the real encoder
(`firmware/test/test_net_proto_json.c`), the worst `dutrx` line — a full chunk,
all counters at max width, both sticky flags set — is **693 B** on the wire
(694 with the NUL) against `diag`'s **1040**. At 512 bytes it would be 1206, and
a frame chunk rather than the counter mailbox would become the thing the buffer
is sized for, leaving every future diag counter to argue with it. The bound
itself is **unchanged at 1280**, and the test asserts the ordering
(`dutrx worst < diag worst`) rather than restating it in a comment.

Hex, not base64: this protocol is plain-ASCII JSON parsed by a hand-rolled
fail-closed tokenizer in the firmware (`net_proto.h`'s escape policy), and hex
costs 512 characters inside a budget that has room for them.

### On a bitstream without the block: it DECLINES

```
→ {"op":"dutrx"}   ← {"ok":false,"err":"dut_egress not present"}
```

DUTEGR is real RTL in the shell BD: the fielded Linux static `0x44EE76D5` and the
bare-metal rollback image `0x72BB0A36` both carry it. A
shell minted before the block landed does not, and a firmware built from this tree
can still be baked into such a bitstream by `updatemem` with no re-mint. Presence is
therefore a **compile-time** fact about which bitstream an image is destined for
(`-DMPS3_HAS_DUT_EGRESS`), exactly like `display`'s `MPS3_HAS_CLCD_KVM`, and the
verb declines on a build without it rather than reading a DECERR'ing void.

It must not answer `ok:true` with zeroes there: "this fabric has no capture
block" and "the DUT sent nothing" would be the same line — the reading-shaped
lie v0.6 removed from `telemetry`. Both reference implementations agree
(`coordinator_handle_dutrx()`'s `#if DUTEGR_PRESENT` and
`pyverify.testing.fakeshell._op_dutrx`'s `has_dut_egress`).

### Not in this version

- **No `flush`.** `DUTEGR.CTRL.FLUSH` (a bounded drain, for discarding the
  previous RM's frames after a DFX swap) is a **swap-time** concern the shell
  owns, not a host verb. It is also not safe to add as an optional argument:
  unknown keys are ignored by this protocol, so an old shell would silently not
  flush and a client could not tell.
- **No host → DUT injection verb (yet).** From mint 3 the fabric path exists: `dut_egress_0`'s inject side (DUTEGR `TX_*`, `0x44B2_0020`–`0x38`, shell-regmap v0.9) drives the bridge's `mgmt_s_*` ingress. No 6900 verb exposes it in this protocol version, so `gen_checker` is still the only host-reachable sender. On a pre-mint-3 static `mgmt_s_*` is tied off (`docs/OPTION_C_EGRESS_STATUS.md` §5).
- **No capture enable.** `CTRL.EN` resets to 1 — the block captures from power-on
  with no setup, because a capture that defaults OFF is one more way for a live
  DUT to look dead.

## Display (`display`) — CLCD KVM remote panel flip

```
→ {"op":"display","owner":"harness"}   ← {"ok":true,"owner":"harness"}
→ {"op":"display","owner":"dut"}        ← {"ok":true,"owner":"harness"}   (committed owner may LAG — see below)
→ {"op":"display","owner":"toggle"}     ← {"ok":true,"owner":"dut"}
→ {"op":"display","owner":"query"}      ← {"ok":true,"owner":"dut"}       (read-only, moves nothing)
```

`display` flips (or reads) the owner of the on-board QVGA panel arbitrated by the
CLCD KVM (`fpga/shell/ip/clcd_kvm/`, CSR block `CLCDKVM @ 0x44AD_0000`,
shell-regmap.md §CLCDKVM). It is the **network** request source; the
`USER_nPB[1]` button and a local CSR write are the other two, and all three
converge on the single frozen `CLCDKVM.CTRL.src_sel` (write-gated by
`src_sel_we`). The handler drives it through the button-safe
`clcd_kvm_request_owner()` helper (never an open-coded RMW), so a request racing
a button press cannot clobber it.

- **`owner` values:**
  - `"harness"` / `"dut"` — request that owner (`src_sel = 0` / `1`).
  - `"toggle"` — flip the *requested target* (`STATUS.tgt_owner`), exactly like
    a button press, so a second toggle mid-handover cancels the first.
  - `"query"` — **read-only**: report the current owner, write nothing. This is
    what `GET /display` / `pyverify`'s `display_owner()` send.
- **Response:** `{"ok":true,"owner":"harness"|"dut"}`, where `owner` is the
  **committed** owner read back from `CLCDKVM.STATUS.owner` — what is actually
  reaching the pads. A flip to the other side takes a hardware handover
  (drain → reset → settle → grant, ~7–9 ms) to commit, so **immediately after a
  flip the reply can still report the OUTGOING owner.** That is the honest state;
  a client confirms the landing with a follow-up `query`.
- **Send-now, not held.** Unlike `swap`, the CSR pokes are immediate; the
  response is sent in the same request→response turn (no parked/held reply).
- **Safety is inherited, not added.** The network flip is subject to the SAME
  forced revert as every other source: a DFX swap (`decouple_status` asserting or
  `rp_resetn` dropping) overrides a remote-flip-to-DUT the instant it happens —
  the KVM FSM does not care where the request came from (clcd_kvm README §9).
- **OFF-build behaviour (today's bitstream, before the Wave-4 rebuild).** There
  is no AXI slave at `0x44AD` until the KVM lands in the static shell
  (`MPS3_HAS_CLCD_KVM` off). On such a build the verb **still decodes cleanly**,
  and the handler **declines** with the uniform failure line
  `{"ok":false,"err":"clcd_kvm not present"}` — it never issues a bus access into
  a DECERR'ing void. Clients key on `ok`. Both reference implementations agree:
  `coordinator_handle_display()` (`#if CLCD_KVM_PRESENT`) and
  `pyverify.testing.fakeshell._op_display` (`has_clcd_kvm=False`).

The CSR contract for `CLCDKVM` is in `shell-regmap.md` v0.5 (already frozen); this
section is only the wire verb over it.

## Version (`version`) — what image IS this? (v0.8)

```
→ {"op":"version"}
← {"ok":true,"harness":"1.0.0","ver32":"0x01000001","sha":"5c09de10","dirty":0,
   "lmb_kb":1024,"features":["clcd","clcd_kvm","touch","hwicap_fifo","windowed"]}
```

(One line on the wire; wrapped here for reading.)

No arguments. Never held (unlike `swap`) — every value is a **build-time
constant**, so the handler touches no register and no bus, and the verb answers
even on a board whose fabric is otherwise unhappy.

| key | type | meaning |
|---|---|---|
| `harness` | string | semantic harness release, e.g. `"1.0.0"`. **`"0.0.0"` means "not provisioned"** — the image was linked without a generated identity (the weak `mps3_harness_*()` fallback). No shipped harness ever legitimately reports `0.0.0`. |
| `ver32` | string | packed `HARNESS_VER32`, `"0x"` + 8 lowercase hex: `major<<24 \| minor<<16 \| patch<<8 \| flags`, `flags` bit0 = dirty. |
| `sha` | string | 8 hex chars of the build commit; `"unknown"` when not provisioned or built outside a git checkout. |
| `dirty` | **int** (0/1) | uncommitted changes in the tree at build time. An INT, not a JSON bool — it rides beside `ver32`'s flags byte, and both reference servers emit an int (the conformance suite type-checks it). |
| `lmb_kb` | int | the LMB size, in KiB, this image was **linked** for. |
| `features` | array of strings | the compile-time feature flags that were ON. |

**`features` — fixed order, absent entries omitted.** The array is emitted in
one canonical order — the `MPS3_FEATURE_*` bit order in
`firmware/common/net_proto.h`, names in `net_proto.c`'s `s_feature_names[]` —
with features that are off simply left out; an image with none of them emits
`[]` (empty, never a missing key). So a client may compare the array for
**equality**, not merely membership, and two images with the same feature set
always produce the same bytes. **New bits are only ever APPENDED.**

| bit | name | set when | since |
|---|---|---|---|
| 0 | `clcd` | `CLCD=1` (`-DMPS3_HAS_CLCD`) | v0.8 |
| 1 | `clcd_kvm` | `CLCD_KVM=1` | v0.8 |
| 2 | `touch` | `TOUCH=1` | v0.8 |
| 3 | `hwicap_fifo` | `HWICAP_FIFO=1` | v0.8 |
| 4 | `windowed` | `WINDOWED=1` | v0.8 |
| 5 | `dut_egress` | `DUT_EGRESS=1` — `dutrx` answers | v0.11 |
| 6 | `jtag_server` | always (TCP 6921 `remote_bitbang` is linked and polled) | v0.11 |
| 7 | `xvc_dbgbr` | `XVC_TARGET` empty — 2542 drives the Debug Bridge | v0.11 |
| 8 | `xvc_jtagbb` | `XVC_TARGET=jtagbb` (or the legacy `swdbb`) — 2542 drives `jtag_bb` | v0.11 |
| 9 | `stats` | always (v0.11 images) | v0.11 |
| 10 | `log` | always (v0.11 images) | v0.11 |
| 11 | `reboot` | always (v0.11 images) | v0.11 |
| 12 | `touch_cal` | `TOUCH=1` | v0.11 |
| 13 | `usd` | always (v0.13 images) — the `usd` verb and the re-push `commit` | v0.13 |
| 14 | `slot` | the engine serves the `slot` verb and the kind-2 push (a linked provider, `mps3_slot_supported()`: Linux harness yes, bare metal never) | v0.14 (2026-09-26) |
| 15 | `xvc_lock` | on a claimed board, XVC 2542 and `jtag_server` 6921 refuse a non-local peer with one `locked` line (a linked provider, `mps3_debug_lock_supported()`: Linux harness yes, bare metal never) | v0.14 (2026-09-26) |

**Engine names (v0.15).** After the last bit name the array may carry ENGINE
feature names, from the `mps3_proto_features_extra()` seam (weak: none), in a
fixed order. They spend no bit: they name a service only one engine runs.

| name | set when | since |
|---|---|---|
| `lcd_mirror` | the Linux harness serves the LCD mirror (TCP 6940): not `--lcdmirror none`, and the `mps3-lcdmirror` binary is installed (executable) beside harnessd | v0.15 |
| `identity` | the engine serves `identity` / `identity_set` and identify's `label` (every Linux harness) | v0.16 |
| `locate` | the engine serves `locate` (a Linux harness built with the panel, `MPS3_HAS_CLCD`) | v0.16 |
| `presence` | the engine serves `hello` (a Linux harness built with the panel) | v0.17 |
| `panel` | the engine serves `panel` (a Linux harness built with the panel) | v0.17 |

**`lcd_mirror` — where the mirror listens (v0.15, Linux).** Present with the
feature name: `{"port":6940,"mode":"sw","proto":1}`, after `id_skew`, before
`impl`. `mode` is `"sw"` for the interim software mode (exact only for the
harness's own text screen, blind while the DUT owns the panel) and `"hw"` once the
mint-4 snooper exists; `proto` is the 6940 wire's version. The port is always
127.0.0.1-only: reach it through `ssh -L`.

Exactly one of bits 7/8 is set. `PRODUCT=1` (`CLCD=1 CLCD_KVM=1 HWICAP_FIFO=1
WINDOWED=1 TOUCH=1 DUT_EGRESS=1`, `XVC_TARGET` empty) reports all of bits 0–7
and 9–12. A v0.10 image reports only bits 0–4, which is how a client tells the
two apart. (The bare-metal rollback image `0x72BB0A36` carries v0.11, `PRODUCT=1`, and reported all
twelve names when that firmware was trialled —
`docs/evidence/2026-09-w3/v011_volatile_20260924.txt`. The previous shell's image,
`0x3F1A560F`'s, predated both `dut_egress` and this table and reported the five
v0.8 names.)

**`impl` — which engine (v0.11 additive).** `"linux"` when `mps3-harnessd` (the
MicroBlaze V Linux harness) answers; the key is ABSENT from every bare-metal
image, so an absent/null/empty `impl` means bare metal. It is always the LAST
key. The codec emits it only when the image's `mps3_proto_impl()` seam returns a
string (the weak default returns NULL), which is why a bare-metal line is
byte-identical to the pre-`impl` one. The same rule governs `identify.impl`.

**`id_skew` — the identity disagreement (v0.11 additive, Linux).** Present only
when the engine's identity sources disagree; the value is the reason, e.g.
`"image 0x0badcafe != fabric 0x5a5a0001"`, `"no valid stage0 status block"`,
`"usr_access 0x01020300 != image 0x01020400"`. Emitted after `skew`, before
`impl`. While it is present, `swap` and `commit` are refused (see "Identity
lock").

On the Linux harness `ver32`/`harness`/`sha`/`dirty` describe the **image** (from
its `/etc/mps3/version` manifest) and `usr_access` is the **fabric**, so `skew` is
the image-vs-bitstream check; `lmb_kb` is 128 (the MBV LMB), and `features`
omits `windowed` (the kernel's TCP flow control paces 6910; the wire is
identical — see "Windowed 6910").

**Why `lmb_kb` is on the wire and not merely in a build log.** The MicroBlaze
LMB address decode **aliases**: reading the 1 MiB diagnostic-mailbox address on a
512 KiB shell silently returns the 512 KiB mailbox's contents rather than
failing. An image linked for the wrong LMB is therefore not loud about it, and
this field is how a host notices.

**This is not `static_id`, and does not replace `ping`.** The two identities are
orthogonal axes and both are needed:

| | `ping.shell_id` (`static_id`) | `version.ver32` |
|---|---|---|
| answers | "will this partial FIT this fabric?" | "WHICH RELEASE is this?" |
| kind | compatibility fingerprint (CRC-32 of the routed static) | provenance, human-ordered |
| ordered? | no | yes |
| load-bearing | yes — checked before every ICAP write | no — reporting only |

One `static_id` serves several harness versions: a firmware-only bump changes the
`.bit` (the image is baked in via `updatemem`) but not the static routing, so
every fielded partial stays valid. **Bumping the harness version must never be
expected to change `static_id`** — that is correct behaviour, not a bug.

**The JTAG cross-check.** The same 32-bit `ver32` is stamped into the static
bitstream's `USR_ACCESS` (AXSS) register by `fpga/dfx/build_dfx.tcl`. So the
number this verb reports and the number JTAG reads out of the fabric — with **no
firmware running** — come from one source. Those two disagreeing means the `.bit`
and the image inside it are from different builds, which is exactly the
"flashable base whose `updatemem` was never re-run" skew this platform has been
bitten by before.

## Stats (`stats`) — the board in one line (v0.11)

```
→ {"op":"stats"}
← {"ok":true,"up_ms":123456,"sid":"0x3f1a560f","rm":"0x01000001","rm_ok":true,
   "lock":false,"clk_sel":1,"mmcm":true,"clk_alive":true,"dut_rst":true,
   "rp_rst":true,"decpl":false,"link":true,"spd":100,"fdx":true,
   "mac":"0002f7ef441c","swap":"idle","swap_ok":true,"swap_n":3,"icap":886432,
   "rxdrop":0,"txerr":0,"swap_err":"","clr_ok":true,"dut_mhz":50,
   "svc_max_us":812,"svc_skipped":0}
```

No arguments; SEND-NOW. The first 22 keys are fpgahub's `_from_stats_verb`
shape exactly and **in that order**; the last five are additive. Worst case
514 B. An image built with `TOUCH=1` (either engine) then appends the three
`touch_*` keys below, after `svc_skipped` and before the Linux `os_up_ms`;
worst case 589 B. A build without the touch driver omits all three.

| key | type | source |
|---|---|---|
| `up_ms` | int | milliseconds since the shell firmware started. Resets on ANY restart (power, JTAG, watchdog, `reboot`) — **the reboot witness**. u32, wraps at ~49.7 days. |
| `sid` / `rm` | string | `static_id` / resident `rm_id`, rendered like `ping` (same VERIFIED identities, not a transient register) |
| `rm_ok` | bool | `DFXCTL.RM_STATUS[0]` rm_id_valid |
| `lock` | bool | `DFXCTL.RM_STATUS[1]` dut_lockup — RAW; meaningful only for RMs that drive it (see Telemetry) |
| `clk_sel` | int | `CLKRST.DUT_CLK_SEL[7:0]`, the last preset id written (resets to 0, i.e. reads "25mhz" before any `set_clk` — use `dut_mhz`) |
| `mmcm` / `clk_alive` | bool | `CLKRST.STATUS[0]` / `[1]` |
| `dut_rst` | bool | `CLKRST.RESET_CTRL.dut_resetn` — **true = released** |
| `rp_rst` | bool | NOT `DFXCTL.STATUS.rp_in_reset` — **true = released** (observed state) |
| `decpl` | bool | `DFXCTL.STATUS.decoupled` |
| `link` / `spd` / `fdx` | bool / int / bool | LAN9220 PHY: BMSR link, ANLPAR best mode; `spd` 10 or 100, 0 when down |
| `mac` | string | the shell's own MAC, 12 lowercase hex, no separators |
| `swap` | string | swap FSM state: `idle`, `gate`, `decouple`, `stream_clearing`, `await_clearing`, `await_partial`, `stream_partial`, `verify`, `cache_clearing`, `release`, `done`, `failed`, `reisolate` |
| `swap_ok` | bool | the most recent COMPLETED swap succeeded (false before the first) |
| `swap_n` | int | swaps completed (done + failed) since boot |
| `icap` | int | bytes written to HWICAP since boot (= `diag.icap_bytes`) |
| `rxdrop` / `txerr` | int | `diag.rx_drops` / `diag.tx_errors` |
| `swap_err` | string | *(extra)* the state the most recent FAILED swap failed in (`verify` for a wrong-RM readback, not the `reisolate` that followed); `""` if none this boot |
| `clr_ok` | bool | *(extra)* the resident RM's clearing is cached — the next swap can clear it |
| `dut_mhz` | int | *(extra)* DUT clock, MHz. Bare metal: the preset last programmed (50 at boot). Linux harness: read back from clk_wiz_dut's register file, so it survives a harnessd respawn; the first boot after a POR/WDOG reset re-loads the MMCM to 50. Use this, never clk_sel. |
| `svc_max_us` | int | *(extra)* worst superloop PASS **since the previous `stats`** |
| `svc_skipped` | int | *(extra)* healthy→sick service edges **since the previous `stats`** |
| `touch_ok` | bool | *(extra, `TOUCH=1` only, 2026-09-24)* the touch driver is sampling: the STMPE811 was confirmed at init and the bus is not latched lost. `false` with `touch_bus_lost` = 0 means the part never came up at init (nothing retries that) |
| `touch_bus_lost` | int | *(extra, `TOUCH=1` only)* times the bus-loss latch fired (16 consecutive I2C failures) since the image started. While latched, touch re-runs its bring-up (AXI IIC reset + Arm's STMPE811 sequence) 5 s after the latch, every 5 s for 6 tries, then every 30 s, at most one I2C transaction per poll |
| `touch_recoveries` | int | *(extra, `TOUCH=1` only)* latches cleared by that periodic re-init. `touch_bus_lost > touch_recoveries` with `touch_ok:false` = the bus is lost NOW and still being retried |
| `lcd_mirror` | object | *(extra, Linux, v0.15, only while the mirror is configured)* the mirror's OLDEST client: `peer` `"a.b.c.d:port"` (the forward's loopback end) or `null` when none; `since` the `up_ms` it connected (0 when none); `fps` whole SNAPs carrying tiles per second over the last 2 s, one decimal (`4.5`); `bytes` sent to it (u32). Emitted after the `touch_*` keys, before `os_up_ms`. HM treats a connected peer as soft-busy, like `stats.gdb` |

**No power keys, ever** (there is no power sensor; see Telemetry). The two
windowed extras are read-and-reset by `stats` itself: the since-boot maxima
stay in `diag` (`pass_max_us`, `svc_skips`) for anyone who wants them.

**After a processor restart the RP is PARKED, and `rm_ok:false` is a REAL state,
not a fault** (silicon, ILA mint board window 2026-09-24). A restart of the
harness processor — JTAG `rst -processor`, the watchdog, the `reboot` verb; on
the Linux harness the WDOG reset of the MicroBlaze V (a respawn of
`mps3-harnessd` alone touches no fabric state) — leaves the reconfigurable
partition **in reset**, and after a watchdog reset also **decoupled**, until the
first swap. `stats` then reads:

```
"rm_ok":false, "dut_rst":false, "rp_rst":false, "decpl":false   (processor restart)
"rm_ok":false, "dut_rst":false, "rp_rst":false, "decpl":true    (after a WDOG reset)
```

`rm_ok` is `rm_id_valid`, which the fabric gates LOW while the RP is held in
reset, so the RM's identity cannot be read — the RM itself is not broken. **One
swap clears it**: a greybox swap releases the partition (`rm_ok:true`,
`rp_rst:true`, `decpl:false`); nothing else does. Neither engine releases a parked
RP on its own (bare metal reports it exactly as above; the Linux harness leaves an
RP it finds parked, parked, and reports it — HARNESSD_CONTRACT §6). Clients:

- do NOT read `rm_ok:false` (or `decpl:true`) after a restart as a failed RM,
  a wedged board or a reason to power-cycle; read it as "parked since the last
  restart" (`up_ms` — and on Linux `os_up_ms` — tells you a restart happened);
- to use the RP, swap: the greybox if nothing else is wanted;
- the D13 power-on load of the default RM runs once per FPGA configuration,
  never after a watchdog or processor reset (see "Power-on load"), so a WDOG
  reset does NOT bring the default overlay back either.

**`os_up_ms` (v0.11 additive, Linux only)** — the OS uptime in ms
(`/proc/uptime`), appended LAST. `up_ms` is the shell's own uptime on both
engines: under Linux it counts from `mps3-harnessd`'s start, so a respawn restarts
it exactly as a bare-metal warm restart does. On Linux `link/spd/fdx/mac` come
from the kernel's view of `eth0` and `rxdrop`/`txerr` from its
`rx_dropped`/`tx_errors` counters. An engine may OMIT a `stats` key it cannot
fill (the order of the rest is kept); none is omitted today.

## Log (`log`) — the shell console, from RAM (v0.11)

```
→ {"op":"log","off":0}
← {"ok":true,"off":0,"n":256,"more":true,"dropped":0,"data":"0d0a2d2d2d204d505333..."}
```

Every byte the firmware prints (`xil_printf` → `outbyte`) is also written into
a 4 KiB RAM ring. The ring is a **stream with absolute byte offsets**: the
first byte after boot is offset 0. It keeps the newest 4096 bytes; older bytes
are overwritten and counted.

- `off` in the request: where to read from (default 0). In the reply: where the
  chunk **actually** starts — later than asked if those bytes were overwritten
  (`reply.off - request.off` bytes were lost), or the current head if the
  request was at/after it (an empty chunk; a client uses it to resync, e.g.
  after the board restarted and its offsets began again at 0).
- `n` bytes (≤ 256) in `data`, lowercase hex exactly like `dutrx`. The bytes are
  console text but may hold any byte, so they are never JSON-escaped.
- `more`: bytes remain after this chunk — ask again with `off = off + n`.
- `dropped`: bytes lost to overrun since boot, on **every** reply.
- **Non-destructive and stateless**: the shell keeps no reader cursor, so two
  readers never steal each other's bytes. To tail, keep your own `off`.

The boot banner (`--- MPS3 nanoSoC shell firmware (A3) starting ---`,
`shell up: …`, `ports: …`) is also **re-printed once at +5 s** from the
superloop, so a serial capture opened after the MCC's ~1 s post-configuration
window still sees it. Worst case 593 B.

## Touch calibration (`touch_cal`) (v0.11)

```
→ {"op":"touch_cal","act":"get"}
← {"ok":true,"ax":21,"bx":377,"cx":-158863,"ay":-281,"by":-2,"cy":1091113,"shift":12}
→ {"op":"touch_cal","act":"set","ax":…,"bx":…,"cx":…,"ay":…,"by":…,"cy":…,"shift":…}
← (the same shape: the calibration now IN FORCE)
→ {"op":"touch_cal","act":"default"}
← (the same shape, back to TOUCH_CALIB_DEFAULT)
→ {"op":"touch_cal","act":"raw"}
← {"ok":true,"raw_x":1234,"raw_y":3210,"raw_z":150,"seen":42,"x":223,"y":51}
```

The map is `x = (ax·rx + bx·ry + cx) >> shift`, `y = (ay·rx + by·ry + cy) >>
shift`, clamped to 320×240, then the 180° panel flip. `set` is validated and
changes nothing on rejection: `bad shift` (outside 0..24), `coef range`
(|ax|,|bx|,|ay|,|by| > 2^17), `offset range` (|cx|,|cy| > 2^29), `singular`
(ax·by − bx·ay = 0). **RAM only** — a restart returns to
`TOUCH_CALIB_DEFAULT` in `firmware/touch/touch.h`, which is where a finished
three-point fit belongs. Since 2026-09-24 that default is the silicon fit shown
in the `get` example (`docs/evidence/2026-09-w3/touch_cal_20260924.txt`; the
panel's axes are swapped as well as mirrored, so the cross terms `bx`/`ay`
carry the map). A `set` also survives a touch bus-loss recovery (see "Stats",
`touch_recoveries`); only a restart or `default` replaces it.

`raw` is the last converted STMPE811 sample — captured before the pressure
floor, debounce or mapping — and `seen`, the number of samples captured since
boot (did a press land?). `x`/`y` are that sample through the current map. The
same values are the ELF statics `s_dbg_raw_x` / `s_dbg_raw_y` / `s_dbg_raw_z` /
`s_dbg_raw_seen`, so a JTAG read agrees with the verb. On an image without
`TOUCH=1`: `{"ok":false,"err":"touch not present"}`.

## Reboot (`reboot`) — warm restart via the shell watchdog (v0.11)

(Linux harness: the same verb, the same replies. The watchdog resets the MBV and
stage0 reboots Linux; `in_ms` uses the MBV's stage (2^31 clocks = 21.5 s, so
`in_ms` ≈ 43 000). `mps3-harnessd` also `sync()`s when the watchdog is armed and
falls back to `reboot(2)` if the reset never comes. A mock build run with
`--wdog off` has no watchdog and declines with `"no watchdog"`.)

```
→ {"op":"reboot"}   ← {"ok":true,"in_ms":2784}
```

The reply is sent **first**; ~100 ms later the firmware stops kicking and
enables the shell watchdog (`WDOG` @ `0x44B4_0000`, `axi_timebase_wdt`,
2^27 shell clocks ≈ 1.34 s per stage). Its second expiry drives
`proc_sys_reset_shell/aux_reset_in` (`fpga/shell/bd/shell_bd.tcl`, "THE RESET
FAN-OUT"): the MicroBlaze restarts, re-runs `coordinator_init()` and reloads the
boot-default overlay. `in_ms` is an **upper bound** (arm delay + two stages).
Expect the 6900 connection to drop; afterwards `ping` answers with the same
`shell_id` and `stats.up_ms` has restarted.

- Refused while a swap is in progress: `{"ok":false,"err":"EBUSY"}`.
- **Linux harness (2026-09-26): also refused while a card job runs** —
  `{"ok":false,"err":"EBUSY"}` while `slot status` shows `job.state` `writing` or
  `verifying`, or a D13 `commit` / `usd` format/clear is in progress. Wait for the
  job to end (poll `slot status`) and ask again. A watchdog reset mid-job left the
  card wedged on B2 silicon. Bare metal: unchanged.
- Refused where no watchdog answers at `0x44B4` (its free-running TBR does not
  move): `{"ok":false,"err":"no watchdog"}` — never an `ok` that cannot happen.
- A repeated `reboot` before arming answers `ok` with the remaining bound.
- A watchdog reset CLAMPS the DFX boundary (dfx_ctl, set-dominant) until the
  boot load releases it, and the diag mailbox survives it.
- Not a reconfiguration: the FPGA keeps its bitstream. For a full reload from
  SD, use the MCC `REBOOT` (hub side).

## Identify (UDP 6899) — what is at this address? (v0.11)

```
→ {"op":"identify","v":1,"nonce":"0011aabbccddeeff"}          (unicast or broadcast)
← {"ok":true,"op":"identify","v":1,"nonce":"0011aabbccddeeff","board":"mps3",
   "mac":"0002f7ef441c","ip":"192.168.10.101","dhcp":false,"shell_id":"0x5a5a0001",
   "rm_id":"0x0100001e","harness":"1.0.0","proto":"0.11","mode":"run","impl":"linux",
   "up_ms":81234,"os_up_ms":99811,"ssh":{"claimed":false,
   "host_key_sha256":"SHA256:s6n1vtZIZ6d1cBnloLKFQw3sQPxxRouFIgZLEdo3gT0","key_sha256":""},
   "ports":{"ctrl":6900,"push":6910,"tftp":69,"jtag":6921,"xvc":2542,
   "uart0":6930,"uart1":6931,"swo":6932}}
```

One datagram in, ONE datagram out (≤ 1200 B), **to the sender's source
addr:port** (never a fixed port). Independent of 6900: it answers while another
client holds 6900 and while a swap has parked it.

- **Request:** `op` = `"identify"`, `v` = 1 (int), `nonce` = 8–32 hex digits
  (echoed verbatim). Anything else — bad JSON, wrong `v`, a bad nonce, another op —
  is **ignored silently** (no reply).
- **Rate limit:** a token bucket of 10 replies/s with a burst of 10; a request
  that finds it empty is dropped silently (retry).
- **Key order (normative, run mode):** `ok, op, v, nonce, board, mac, ip, dhcp,
  shell_id, rm_id, harness, proto, mode, [impl], [unit], up_ms, [os_up_ms], [ssh],
  [label], ports`. Bracketed keys appear only when the engine has them: `impl` and
  `os_up_ms` on Linux (same rule as `version.impl` / `stats.os_up_ms`), `ssh`
  where an SSH server runs (Linux), `unit` (the device-DNA id) once D6 exists,
  `label` (v0.16) where the engine resolves a board identity (Linux; "Identity").
  `ports` is always last: an additive key goes before it.
- `mac` is 12 lowercase hex without separators (as `stats.mac`); `shell_id` /
  `rm_id` are rendered as `ping` renders them and are the FABRIC-bound identity
  (see "Identity lock"); `harness`/`proto` = the release and this document's
  version; `mode` = `"run"` from the shell, `"nohw"` from a Linux harness that
  has no fabric to serve (its 6900 then declines every verb with
  `no fabric: <why>`), `"rescue"` from stage0's rescue responder.
- v0.16 (Linux): `label` is the board's resolved label ("Identity"); `mac` is the
  resolved MAC (the one S41mps3net put on eth0 before link up); `ip` is the resolved
  static address, EXCEPT while a DHCP lease is held (`dhcp:true`), when it is the
  interface's primary address (the lease) as before -- `dhcp` keeps meaning "ip came
  from DHCP". The static address is then a secondary on the same interface.
- `ssh.claimed` is true once a first `authorized_keys` has been claimed (TOFU,
  below); `ssh.host_key_sha256` is the host key's OpenSSH-style fingerprint, so a
  client can pin the key it is about to trust.
- `ssh.key_sha256` (2026-09-26, ADDITIVE, after `host_key_sha256`): the
  fingerprint of the claim's **first** key, in the same `SHA256:<base64>` form
  `ssh-keygen -lf` prints — so a tool can tell "claimed by me" from "claimed by
  someone else". `""` while unclaimed (or when the first line holds no parsable
  key). A claim may hold up to 64 keys; only the first is named.
- Implementation: `firmware/identify/` — a firmware service module both engines
  compile; the Linux harness polls it from service slot 0. Bare metal: registered
  by the platform (FOLD A-v0.12); lwIP delivers broadcasts to a UDP pcb bound to
  `IP_ADDR_ANY` as long as `IP_SOF_BROADCAST_RECV` stays 0 (the default).

## Identity (`identity`, `identity_set`) — which BOARD is this? (v0.16, Linux harness)

Not the FABRIC identity (`shell_id`, "Identity lock"): the BOARD's -- its **label**
(the CLCD row-0 name, e.g. `MPS3-02`), **hostname**, **IP** (the static address it
always answers on) and **MAC**. One image serves every board, so these are RESOLVED at
each boot by `mps3-identity resolve` (IMAGE's `S13mps3identity`, after `/persist` and
before `S41mps3net`), **per field**, in this precedence:

1. the **override** `/persist/etc/mps3/identity` -- shell-sourceable `KEY=VALUE`
   (`MPS3_LABEL`, `MPS3_HOSTNAME`, `MPS3_IP` = `a.b.c.d/nn`, `MPS3_MAC`), written by
   `identity_set` or `mps3-identity set`; read only when `/persist` is on the card
   (IMAGE's `persist.state` `backing=card`). Netboot / `mps3.persist=off` have none.
   A line it cannot parse or whose value the rules below refuse is ignored and logged.
2. the **stage0 bake**: the status block's `label_lo/hi`, `ip_addr`, `mac_lo/hi`
   (STAGE0_CONTRACT §3.3), when the block is valid (`shell_id`'s rule) and the field
   is nonzero. The bake has no prefix: its IP is taken as a `/24`.
3. the **image default**: label `MPS3`, `192.168.10.101/24`, `02:00:00:4d:50:53`.

The **hostname** has no stage0 source: the override's, else the label lowercased
(source `label`; `mps3`, source `default`, if that is not a valid host name). The
result, each field with its source, goes to `/run/mps3/identity`; S41mps3net sets the
eth0 MAC **before link up**, adds the IP as the permanent static secondary (DHCP
first, DAD, as before) and sets the hostname; harnessd shows it (CLCD rows 0/5/14,
identify). Nothing here changes a running board: an edit applies at the next boot.

```
→ {"op":"identity"}
← {"ok":true,"op":"identity","label":"MPS3-02","hostname":"mps3-02","ip":"192.168.10.102/24",
   "mac":"0200000002fe","source":{"label":"stage0","hostname":"label","ip":"stage0","mac":"stage0"},
   "stage0":{"label":"MPS3-02","ip":"192.168.10.102/24","mac":"0200000002fe"},
   "override":null,"pending":null,"persist":true}
```

(One line on the wire.) Any peer; never held. Key order is fixed; worst case ~600 B.

| key | meaning |
|---|---|
| `label`, `hostname`, `ip`, `mac` | THIS boot's identity (`/run/mps3/identity`; the image defaults with sources `default` if the resolver did not run). `ip` is `a.b.c.d/nn`, `mac` 12 lowercase hex |
| `source` | per field: `override` · `stage0` · `default` · (`hostname` only) `label` |
| `stage0` | the bake as the block holds it NOW: `{label, ip, mac}`, a field `null` when 0 or refused; `null` when there is no valid block |
| `override` | the keys the override file validly sets (`{}` = a file with none), `null` = none in force (no file, or no card-backed `/persist`) |
| `pending` | the fields the NEXT boot would resolve differently (override + block now), with their new values; `null` when nothing would change |
| `persist` | `/persist` is on the card: `identity_set` can write, and the override is read at boot |

```
→ {"op":"identity_set","label":"BENCH-2","ip":"192.168.10.7"}
← {"ok":true,"op":"identity_set","persisted":true,
   "pending":{"label":"BENCH-2","hostname":"bench-2","ip":"192.168.10.7/24"},"applies":"reboot"}
→ {"op":"identity_set","clear":true}      (remove the override)
```

- **Arguments:** any subset of `label`, `hostname`, `ip`, `mac` (strings; `""` drops
  that key from the override), or `clear:true` alone. The edits are applied onto the
  override in force and the file is rewritten atomically (temp + fsync + rename +
  fsync of the directory); an override left with no keys is removed.
- **Order of refusals** (the uniform failure shape + `code`):
  1. `{"ok":false,"err":"identity locked: board claimed (use ssh)","code":"locked"}`
     -- a claimed board, a peer that is not the board itself ("The lock"), checked
     FIRST, whatever the request holds;
  2. `{"ok":false,"err":"identity: no persistent /persist (use the card)","code":"no_persist"}`;
  3. `{"ok":false,"err":"invalid <field>: <why>","code":"invalid"}` -- nothing is
     written if any value is refused. Also `invalid request: nothing to set …` /
     `… clear takes no other field`, `invalid clear: not a bool`.
- **The rules** (`identity_core.c`, shared with the board CLI `mps3-identity
  get|set k=v…|clear`, which edits the same file with the same checks):
  - `label`: 1-19 of `[A-Z0-9-]` (the CLCD row-0 field; a stage0 bake holds <= 8);
  - `hostname`: RFC 1123 (dot-separated labels of `[A-Za-z0-9-]`, no label starting
    or ending with `-`), <= 63;
  - `ip`: `a.b.c.d/nn` with `nn` 8-30 (no `/nn` = `/24`), a usable host address: not
    0/8, 127/8, >= 224, nor the network or broadcast address of its prefix;
  - `mac`: 12 hex digits, bare or `:`/`-` separated; unicast (byte 0 bit 0 clear), not 0.
- `identify` reports the `label` ("Identify"); `version.features` names `identity`.
- Implementation: `firmware/coordinator/coordinator.c` (the verbs, the lock, weak
  provider `mps3_identity_op()`), `src/linux_harness/sw/harnessd/identity_linux.c`
  over `identity_core.c`, `mps3_identity.c` (the resolver + CLI), IMAGE's
  `S13mps3identity` / `S41mps3net`. Client: `pyverify.ShellClient.identity()` /
  `.identity_set()`.

## Locate (`locate`) — which of these boards is it? (v0.16, Linux harness)

The Harness Manager's "Identify board" (its request R3,
`harness-manager/docs/design/CLCD_ALIGNMENT.md` §3/§5.3).

```
→ {"op":"locate","s":10,"who":"alice@bench"}   ← {"ok":true,"op":"locate","until_ms":10000}
→ {"op":"locate","s":0}                           ← {"ok":true,"op":"locate","until_ms":0}
```

- `s` (int, required) 1-30 seconds; `0` stops. `who` (optional) <= 32 printable ASCII.
  Anything else: `{"ok":false,"err":"invalid s: …"|"invalid who: …","code":"invalid"}`.
- `until_ms` is RELATIVE: ms from this reply to the end (0 = stopped).
- **One at a time:** a new locate replaces the running one (its time and `who`); the
  blink carries on.
- **The blink:** the panel backlight (CLCDKVM `CTRL[5]`) toggles every 250 ms -- a
  2 Hz blink -- from the clcd service row: one KVM CTRL write per edge, zero pixel
  bytes. The KVM owns the backlight whoever owns the panel, so it blinks while the
  DUT owns it too (the DUT's picture blinks).
- **The banner:** while the harness owns the panel, rows 10-12 show "IDENTIFY: <who>"
  inverted: on the status page when no FAULT banner is up (faults outrank it; the DIP
  and engine rows yield to it), over rows 10-12 of the apps page.
- **The tap:** a tap on the panel while a locate runs ends it ("found it"; it is not
  also a page change) and is logged, with `who` and the time into it, to the harness
  log (`log`).
- **The restore:** the backlight is ON whenever no locate runs -- when it ends by
  time, `s:0` or a tap, and at every harnessd start (clcd init), so a harnessd that
  died mid-blink comes back lit.
- **Not claim-locked:** it changes nothing but the light, and a tool sends it over
  Ethernet.
- Bare metal, and a Linux build without the panel, decline:
  `{"ok":false,"err":"locate not supported","code":"not_supported"}`.
- Implementation: `coordinator.c` (weak `mps3_locate_op()`),
  `src/linux_harness/sw/harnessd/locate_linux.c`, `firmware/clcd/clcd.c` (the
  `mps3_clcd_locate()` / `mps3_clcd_tap()` seams). Client:
  `pyverify.ShellClient.locate()`.

## Presence and the panel (`hello`, `panel`) — v0.17 (Linux harness)

The Harness Manager's requests R1 and R2 (harness side;
`harness-manager/docs/design/CLCD_ALIGNMENT.md` §2, §5.3). Presence answers "is anybody
attached to this board?" at the bench; `panel` tells a Harness Manager what the glass
shows. **Display only:** anything that reaches 6900 can send a `hello`; it authorises
nothing (the hub lease and the SSH claim stay the controls).

### `hello`

```
→ {"op":"hello","v":1,"sid":"a1b2c3d4","who":"<user>@<build-host>","app":"hm/0.1.0","name":"mps3-01",
   "role":"holder","lease":{"by":"david","left":4332,"q":1,"req":"bob","rl":103},
   "job":{"k":"program","p":42},"ttl":90}
← {"ok":true,"op":"hello","sessions":1,
   "panel":{"page":"status","owner":"harness","pending":false,"banner":"bob wants this board",
            "card":"nanosoc [A]","seq":17},
   "events":[{"seq":17,"k":"tap","on":"request","ms_ago":2300}]}
```

(One line each on the wire.) The Harness Manager sends it every 30 s (10 s while a lease
request or an Identify is open), on control connections it makes anyway.

| field | rule on the board |
|---|---|
| `sid` | **required**, a string, 1-8 characters after clipping (HM: 8 random hex) -- the session's key |
| `who` | **required**, a string (`user@host`); clipped to 20 |
| `app`, `name` | optional strings, clipped to 12 / 16. Kept, not drawn: row 0 shows the board's own resolved label ("Identity"), not `name` |
| `role` | `holder` (holds the hub lease) · `owner` (standalone: this HM holds the board) · `watch` (default); anything else is refused |
| `lease` | optional flat object -- present = the board is behind a hub. `by`, `req` (strings: the USER part, cut at `@`, clipped to 12; `by` absent = nobody holds it); `left`, `rl` (ints, RELATIVE seconds, clamped 0-86400 / 0-600: the board ages them on its own monotonic clock from the hello's arrival); `q` (int, 0-99) |
| `job` | optional flat object `{k: string (clipped to 8), p: int (0-100)}`. Kept, not drawn in v0.17 |
| `ttl` | optional int, clamped 30-300 (default 90) |
| `v` | optional int >= 1 (default 1). Extra keys, at the top and inside `lease`/`job`, are ignored |

- **Printable ASCII only.** Every string: any byte outside 0x20-0x7E becomes `?`
  (escaped control characters included), then the clip. A `\uXXXX` escape is refused by
  the tokenizer (`bad json`), as for every verb.
- **The line limit is 256 characters** (`MPS3_NET_LINE_MAX`, the newline not counted): a
  longer line is refused whole at the line layer, `{"ok":false,"err":"bad json"}`, and the
  table is unchanged. The Harness Manager's worst case is 251 B with its newline.
- **Refusals** (`code` `invalid`): `invalid sid: 1-8 printable characters`, `invalid who:
  a string (user@host)`, `invalid role: holder, owner or watch`, `invalid ttl: an integer
  (30-300 s)`, `invalid v: …`, `invalid lease: …`, `invalid job: …`, `invalid request`.
- **The table.** At most 4 sessions, by `sid`; a new `sid` when full evicts the one whose
  last hello is OLDEST (live or expired). A session is LIVE while `now - last hello <=
  ttl`. An expired one stays (for "left 5m ago") until evicted or harnessd restarts.
  Live sessions are ordered holder > owner > watch, most recent first.
- **The reply:** `sessions` = the live count after this hello; `panel` = page, owner,
  pending, banner, card (as in the `panel` state) and `seq` (the newest tap event's, 0 =
  none); `events` = the tap ring (below).
- **What the panel draws** (status page; the lease badge on every page):
  - row 0, right-aligned: the freshest lease a LIVE session reported (the holder's own
    report first): `<lock> david 1h12m, 1 waiting` (held colour; warn under 5 min left),
    `<warn> not leased` (a lease object without `by`), nothing when no live session reported a
    lease -- the panel never keeps showing a lease nobody has confirmed. At most 24
    cells: `, N wait` / ` +N` when the long form does not fit;
  - row 11: `hm     <user><user>@<build-host>  +1 watching` (the first live session), `hm     none
    connected`, or `hm     <user>@<build-host>  left 5m ago` after the last one expired;
  - rows 10-12, below the fault and IDENTIFY banners: `bob wants this board` / `held by
    david 1:43 to answer` / `tap: tell david you are here` while the freshest lease names
    a request and its `rl` has not run out. A tap on it is an event `on:"request"`; it
    notifies (through the holder's HM), it never releases.
  - Glyphs (`<lock>` `<warn>` `<user>` = bytes 0x83 0x82 0x84, clcd.h `CLCD_GLYPH_*`) only
    in the aligned theme (the default); `--panel-theme today` draws the same words in
    ASCII, in today's pixels.
- **The repaint rule:** what the panel draws for one `sid` changes at most once per 2 s;
  a second hello sooner is replied to at once (and refreshes the TTL) but drawn at the
  next commit.

### `panel`

```
→ {"op":"panel"}
← {"ok":true,"op":"panel","page":"status","owner":"harness","pending":false,"banner":"",
   "card":"nanosoc [A]","touch":{"present":true,"cal":true},
   "sessions":[{"sid":"a1b2c3d4","who":"<user>@<build-host>","role":"holder","age_s":12}],
   "seq":17,"events":[{"seq":17,"k":"tap","on":"request","ms_ago":2300}]}
→ {"op":"panel","frame":"a"}
← {"ok":true,"op":"panel","frame":"a","theme":"today","rows":["<40 cells>", ... 8],
   "roles":"<320 role codes>"}
→ {"op":"panel","frame":"b"}      ← … rows 8-14: 7 rows, 280 role codes
→ {"op":"panel","page":"apps"}    ← {"ok":true,"op":"panel","page":"apps"}
```

| key | meaning |
|---|---|
| `page` | `status` · `apps` (the harness's page, also while the DUT owns the panel) |
| `owner` | `harness` · `dut`: CLCDKVM `STATUS.owner`, the committed owner (as `display query`) |
| `pending` | a KVM owner flip is in flight (`STATUS.switch_pending`) |
| `banner` | the main line of the banner on rows 10-12 (fault, IDENTIFY, request) or the DUT notice, glyphs and edge spaces stripped; `""` = none |
| `card` | the CLCD's card text (row 4 `USD :`), as `usd.text` |
| `touch` | `present`: the STMPE811 answered at start (CHIP_ID 0x0811); `cal`: a valid calibration is installed. Touch health proper is `stats` (`touch_ok`, …) |
| `sessions` | the LIVE sessions, in the table's order; `age_s` = s since the last hello |
| `seq`, `events` | as in `hello` |

- **Events** (`k` `tap`): the panel's tap ring, the last 8, oldest first, `seq` rising
  from 1 since harnessd started (a `seq` that goes backwards = a restart). `on`: `nav`
  (the page strip, row 14), `identify` (a tap that ended a `locate`: "found it"),
  `request` (the lease-request banner). `ms_ago` = ms from the tap to this reply. Nothing
  is acknowledged or removed: every Harness Manager sees every tap.
- **The frame** is the COMMITTED text grid (what is on, or about to be on, the glass;
  while the DUT owns the panel, the harness's last frame = the DUT notice), in two
  halves -- ask `"a"` then `"b"`, on one connection. `rows`: 40 cells each; printable
  ASCII, a glyph byte 0x80-0x86 as `\u0080`-`\u0086`. `roles`: one CODE per cell,
  `chr(ord("a") + role)` over the 21 roles of the Harness Manager's `design/tokens.json`
  `panel.roles` (= its generated `clcd_palette.h` enum): `a` text, `b` label, `c` value,
  `d` rule, `e` chrome, `f` title, `g` title-held, `h` title-warn, `i` ok, `j` warn, `k`
  err, `l` busy, `m` unk, `n` held, `o` bar, `p` track, `q` banner-err, `r` banner-warn,
  `s` banner-ok, `t` banner-busy, `u` banner-held. `theme` names the palette they are
  drawn in (`aligned` · `today`; in `today` every role but the banners is white on
  black, the banners white on red).
- **A whole frame is refused** (`frame:true`, or any value but `"a"`/`"b"`):
  `{"ok":false,"err":"invalid frame: \"a\" (rows 0-7) then \"b\" (rows 8-14)","code":"invalid"}`.
  15 rows + 600 codes is ~1.3 KB typical, over `MPS3_CTRL_RESP_MAX`. Measured worst
  cases through the codec (`harnessd/tests/test_panel_render.c`): a half whose every
  cell escapes (`"`, `\`) 1059 B; a half fits with up to 13 glyphs in EVERY row; the
  worst `panel` state (4 escape-heavy sessions, 8 taps, an escaped banner and card)
  1211 B; the worst `hello` reply 780 B -- all under 1279. A half that would not fit is
  refused whole (`code` `too_large`), never truncated.
- **`page`** (`"status"` · `"apps"`), in this order: the claim lock ("The lock": `panel
  locked: board claimed (use ssh)`, `code` `locked`, whatever the request holds); a page
  with a `frame` / a bad page (`invalid …`); `{"ok":false,"err":"dut owns the
  panel","code":"held"}` while the DUT owns the panel or a flip is in flight; else set.
- **Rates:** the board does not rate-limit either verb (every reply is rendered from
  memory, no I/O). The Harness Manager keeps itself to a hello per 10 s per session, the
  state once a second and a frame (both halves) once every 3 s.
- Bare metal, and a Linux build without the panel, decline both:
  `{"ok":false,"err":"hello not supported","code":"not_supported"}` /
  `{"ok":false,"err":"panel not supported","code":"not_supported"}`.
- Implementation: `coordinator.c` (weak `mps3_hello_op()` / `mps3_panel_op()`, the page
  lock), `net_proto.c` (`mps3_json_parse_ex`, the nested-`hello` decode),
  `src/linux_harness/sw/harnessd/panel_linux.c` over `presence_core.c`, and lane
  PANEL-CLCD's `firmware/clcd/clcd.c` (the seams, the page-aware hit test, the event
  ring, the frame + roles). Client: `pyverify.ShellClient.hello()` / `.panel()` /
  `.panel_frame()` / `.panel_page()`, `pyverify.client.hello_message()` (the Harness
  Manager's bytes).

## TOFU first-key claim (TFTP `authorized_keys`) (v0.11)

A board ships **unclaimed** (key-only SSH, no key). The first TFTP WRQ on UDP 69
whose filename is **exactly** `authorized_keys` (octet mode, **no** 24-byte MPS3
header) claims it:

- accepted only while **unclaimed**; the file (≤ 16 KiB, non-empty) is written
  atomically and the final block ACKed;
- once claimed, every further claim is refused at the WRQ with **TFTP ERROR 2**
  (access violation); an empty claim is ERROR 2; an oversize one ERROR 3; a torn
  one leaves nothing;
- any other filename takes the bitstream path exactly as before;
- a bare-metal image refuses every claim with ERROR 2 (no SSH to claim);
- reset: `mps3-unclaim` from the serial console (Linux image).
`identify.ssh.claimed` reports the state.

## Identity lock (`swap` / `commit`) (v0.11, Linux harness)

On bare metal the `static_id` is baked into the bitstream with the firmware, so
`ping.shell_id` cannot disagree with the fabric. The Linux harness's image lives
on a card and can be swapped independently, so its `shell_id` is **fabric-bound**:
the `fabric_static_id` stage0 recorded in its LMB status block (baked at mint).
When that block is absent or invalid, when the image's `/etc/mps3/static_id`
claim disagrees with it, or when the fabric's USR_ACCESS disagrees with the
image's `ver32`, the identity is **locked**:

- `ping.shell_id` still reports the FABRIC value (`0x00000000` when unknown —
  never the card's);
- `version.id_skew` carries the reason;
- `swap` and `commit` are refused, before anything is touched:
  `{"ok":false,"err":"identity lock: <reason>"}` — clients match the
  `identity lock:` prefix. (Until the coordinator seam lands, a locked harness
  fails the swap closed with `swap failed` at `stream_clearing`, before any ICAP
  word is written.)

## Slot images (`slot`, 6910 kind 2) — v0.14 (Linux harness)

The Linux harness boots from the **user microSD** through stage0: slot A (p1) and
slot B (p2) each hold one S0LB boot image, and the boot-select sector (LBA 1/2)
names the default (`docs/planning/linux_lanes/STAGE0_CONTRACT.md` §5–§6). This
section lets a tool stage a new image into the **inactive** slot over Ethernet,
check it, and flip the default — the Ethernet-updatable half of a Linux release
(FLOW_CONTRACT §0 `targets.ethernet.slot_image`). Admins on SSH have IMAGE's
`mps3-slot` for the same card.

**Bare metal** (the MicroBlaze boots from the MCC's config SD; there are no slots):
every `slot` act answers `{"ok":false,"err":"slot not supported"}`, and a kind-2
push is refused unread (TCP close; TFTP ERROR `rejected`).

### The flow

```
→ {"op":"slot","act":"status"}                 target "B", staged null
  push linux_slot.img to 6910, kind 2          the harness writes slot B, then reads it back
→ {"op":"slot","act":"status"}  (poll)         job {"act":"push","state":"verifying"} ... "ok"
→ {"op":"slot","act":"commit"}                 default "B" (the boot-select sector is rewritten)
→ {"op":"reboot"}                              stage0 boots slot B
→ identify / version                            the new image answers (harnessd has
                                                confirmed the boot to stage0: S5)
```

If the new image never becomes healthy, stage0 falls back to the other slot by
itself (STAGE0_CONTRACT §4). If it boots but misbehaves, `rollback` (after a
`verify` of the old slot, since it was not booted this time) flips back.

### The push (6910 or TFTP 69, header kind 2)

The ordinary framing ("Bitstream framing"): `magic "MPS3" | ver 1 | kind 2 |
rm_slot | static_id | rm_id 0 | len_words | crc32`, then the image **zero-padded to
a whole word** (stage0 never reads past its regions).

- `static_id` is the static the image was **provisioned for** (its bundle's
  `targets.ethernet.provisioned.static_id`, or IMAGE's `version` `static_id=`) —
  never a value read back from the board. It must equal the **fabric** static_id
  (stage0's baked value, "Identity lock"), or the push is refused.
- `rm_slot`: `0` = "the inactive slot, whichever it is"; `1`/`2` = the slot the
  tool expects to write — refused (`slot mismatch: the target is B`) if it is not
  the target. A guard against a board whose state changed since the tool looked.
- **The target** is the slot that is **neither running nor the default**. When
  the running slot and the default differ (after a `commit`, before the reboot;
  or after stage0 fell back) there is none, and the push is refused: `rollback`
  first. So the image stage0 tries first is always one that was verified before it
  became the default. After a rescue boot (or a JTAG load) neither slot runs, and
  the target is the slot that is not the default.
- **The image may be at most the slot size minus 512 bytes** (the last sector
  holds the slot record, below): 64 MiB − 512 B on the standard card.
- **Order on the card.** The header and entry table are checked (stage0's rules)
  from the first bytes **before any byte is written**. Then the old image's first
  sector is zeroed, the body is written, and — after the push completes, the
  transport CRC holds and every region CRC matches — the new first sector is
  written **last**. So the slot never shows a valid table over a partial body.
- **Then the card read-back**, in the background: the page cache is dropped and
  the slot is loaded through **stage0's own loader** (`s0_load()`, compiled into
  `mps3-harnessd` unmodified) off the card; the table CRC must be the pushed one.
  Only then is the slot record written and the slot `staged`. A failed read-back
  zeroes the first sector again. On the board this takes ~70 s per MB (measured 26-29 Sep:
  ~30-35 min for a 29 MB slot; the push itself adds ~12 min).
- **What the transport says.** 6910 has no in-band verdict (the close is the only
  signal); TFTP's final ACK means "the bytes arrived intact and the image is
  self-consistent". Either way the card verdict is `status`'s `job`.
- Refused before the provider: `ver != 1`, `len_words == 0`, more than 64 MiB.
  Everything else is recorded in `job` (`state` `failed`, `err` below). A push
  while a job is running is closed unread and does not disturb that job.

### `status` — and the reply to every act

```
→ {"op":"slot","act":"status"}
← {"ok":true,"card":true,"fabric_sid":"0x61bc6789","running":"A","default":"A","seq":1,
   "target":"B","staged":null,
   "a":{"state":"valid","hdr_crc":"0x3e5e9c2c","len":24354312,"verified":"boot"},
   "b":{"state":"valid","hdr_crc":"0x0a4da20b","len":24351208,"sid":"0x61bc6789",
        "verified":"readback"},
   "job":{"act":"push","slot":"B","state":"ok","got":24351208,"len":24351212,"err":""},
   "claimed":false,"confirmed":true}
```

(One line on the wire.) Key order is fixed; worst case ~590 B.

| key | meaning |
|---|---|
| `card` | a card answered. `false` ⇒ only `fabric_sid`, `running`, `staged`, `job`, `claimed`, `confirmed` follow — **no card is not an error** for `status`; every other act declines `no card` |
| `fabric_sid` | stage0's baked static_id (the status block), `0x00000000` when there is no valid block. Images must match it |
| `running` | `A` · `B` (stage0 handed off from that slot) · `rescue` (a TFTP rescue push) · `none` (stage0 ran, handed nothing off: a JTAG load) · `unknown` (no valid stage0 block: every write is refused) |
| `default`, `seq` | stage0's pick of the boot-select copies (`A` with `seq` 0 when neither copy is valid) |
| `target` | where a push goes, or `null` (none: `rollback` first) |
| `staged` | the slot a push wrote AND read back this boot, or `null` — what `commit` makes the default |
| `a`, `b` | per slot: `state` ∈ `absent` (the MBR entry is not a `0x7F` slot — a blank or foreign card shows both absent and is never written) · `empty` (no S0LB magic) · `bad` (+`err`) · `valid` · `io` (+`err`); `valid` adds `hdr_crc` (the S0LB **table CRC**: the image's identity, the same number as the image header's offset 28 and stage0's `image_hdr_crc`) and `len` (the image extent); `sid` when a **slot record** binds this image to a static_id; `verified` ∈ `no` · `boot` (stage0 CRC-checked it at THIS boot's hand-off) · `readback` (a push or `verify` read it back this boot, bound to this fabric) |
| `job` | the one asynchronous card job: `act` ∈ `none`·`push`·`verify`, `slot`, `state` ∈ `idle`·`writing`·`verifying`·`ok`·`failed`, `got`/`len` (bytes, while writing), `err` (when failed) |
| `claimed` | (2026-09-26, both forms) the board's SSH is claimed — the same state as `identify`'s `ssh.claimed`, read on every request. `true` ⇒ a non-local peer's mutations are refused ("The lock"). Here because a client behind an `ssh -L` tunnel cannot reach identify (UDP) |
| `confirmed` | (2026-09-26, both forms) this boot was confirmed healthy to stage0: its `att_confirm` holds `0x4B4F3053` (harnessd writes it ≥ 2 s after start once IMAGE's boot-health says `healthy=1`). `false` until then, on an unhealthy boot, and with no valid stage0 block. NB `verified: boot` does not imply it |

"This boot" survives a harnessd respawn (the state lives in `/run`), not an OS
reboot.

### The acts

| act | does | refused with |
|---|---|---|
| `status` | nothing (reads ~7 sectors) | `card io` |
| `commit` [`slot`] | default := the **staged** slot (idempotent) | `nothing staged: push an image first`, `slot mismatch: commit would pick B` |
| `rollback` [`slot`] | default := the slot that is **not** the default | `slot mismatch: rollback would pick A` |
| `verify` [`slot`] | starts a card read-back of a slot (default: the one that is not the default); `job` reports it. Needs the slot record to bind a static_id | `slot A is not a valid image` |

**Verify before flip** (`commit` and `rollback`): the destination must be `valid`
and either `verified: readback` with its bound static_id equal to `fabric_sid`, or
`verified: boot` with the running image's identity consistent (no identity lock).
Otherwise: `slot A is not a valid image` · `slot A not verified` · `slot A changed
since it was verified` · `slot A is for 0x… != fabric 0x…` · `slot A runs, but
identity lock: <reason>`.

**The flip** rewrites the boot-select copy that is **not** stage0's current pick
with `seq + 1` (`O_DSYNC`), drops the cache and reads both copies back off the card:
a write torn at any byte leaves the previous pick. Failures: `boot-select write:
<errno text>`, `boot-select read-back (default N seq N)`.

**Refusals common to commit/rollback/verify:** `no card`, `card io`, `no stage0
block`, `fabric static_id unknown`, `EBUSY` (a job is writing or verifying).
`commit` and `rollback` from a non-local peer on a claimed board: `slot locked:
board claimed (use ssh)` ("The lock").

### `job.err` (a failed push or verify)

| cause | text |
|---|---|
| identity | `no stage0 block` · `fabric static_id unknown` · `image for 0x0badcafe != fabric 0x61bc6789` |
| card | `no card` · `card io` · `card write: <errno text>` · `slot B absent` |
| placement | `no free slot: A runs, B is the default -- rollback first` · `slot mismatch: the target is B` · `image too large for slot B` |
| the image (stage0's rules) | `no S0LB magic` · `bad version` · `bad num_entries` · `table truncated` · `table CRC` · `region outside the DDR window` · `region past the end` · `region 0 CRC` |
| the transfer | `torn (the push stopped early)` · `aborted` |
| the read-back | `flush: …` · `header write: …` · `read-back: <stage0 reason>` · `read-back: table CRC 0x… != pushed 0x…` · `record write: …` · `record read-back` |
| verify | `no slot record: static_id unknown` · `image for 0x… != fabric 0x…` · `read-back: …` |
| a harnessd respawn | `harnessd restarted mid-push` · `result lost (harnessd restarted): verify again` · `verifier died` |

Clients match `err` as text for humans; the stable part is `state: "failed"`
and, since 2026-09-26, `job.code` (below).

### Codes (2026-09-26, ADDITIVE)

A `slot` refusal carries an optional **`code`** beside `err`, and a failed job an
optional **`job.code`** after `job.err` — `{"ok":false,"err":"slot B not
verified","code":"not_verified"}`, `"job":{…,"state":"failed",…,"err":"torn (the
push stopped early)","code":"torn"}`. Each is emitted **only when set**: a text
with no code (e.g. `fork: …`) has no key, and no other verb's lines change
(the claim-locked `usd`/`commit` refusals and the 2542/6921 lock lines also say
`"code":"locked"`, see "The lock"). Match the code; `err` stays for humans. The
codes are the contract; new ones may be appended.

| verb `code` | `err` |
|---|---|
| `not_supported` | `slot not supported` (bare metal) |
| `bad_act` · `bad_slot` | `bad act` · `bad slot` |
| `locked` | `slot locked: board claimed (use ssh)` |
| `no_card` · `card_io` | `no card` · `card io` |
| `no_stage0` · `fabric_unknown` | `no stage0 block` · `fabric static_id unknown` |
| `busy` | `EBUSY` (a job holds the card) |
| `nothing_staged` | `nothing staged: push an image first` |
| `slot_mismatch` | `slot mismatch: … would pick X` |
| `not_valid` · `not_verified` · `changed` | `slot X is not a valid image` · `slot X not verified` · `slot X changed since it was verified` |
| `wrong_static` | `slot X is for 0x… != fabric 0x…` |
| `identity_lock` | `slot X runs, but identity lock: …` |
| `bootsel` | `boot-select write: …` · `boot-select read-back …` · `boot-select refused: …` |

| `job.code` | `job.err` |
|---|---|
| `wrong_static` | `image for 0x… != fabric 0x…` (push or verify) |
| `no_stage0` · `fabric_unknown` · `no_card` · `card_io` | as the verb's (a push refused for the same condition) |
| `no_free_slot` · `slot_mismatch` · `too_large` | `no free slot: …` · `slot mismatch: the target is X` · `image too large for slot X` |
| `slot_absent` · `slot_bad` | `slot X absent` · `slot X bad: …` |
| `bad_image` | stage0's table rules: `no S0LB magic`, `bad version`, `bad num_entries`, `table truncated`, `table CRC`, `region …`, `shorter than a header` |
| `torn` · `aborted` | `torn (the push stopped early)` · `aborted` |
| `card_write` | `card write: …` |
| `readback` | `flush: …` · `header write: …` · `read-back: …` · `verifier died` |
| `record` · `no_record` | `record write: …` / `record read-back` · `no slot record: static_id unknown` |
| `restarted` | `harnessd restarted mid-push` · `result lost (harnessd restarted): verify again` |

(The proposal's job list is extended with the verb's `no_stage0`, `fabric_unknown`,
`no_card`, `card_io` for a push refused for those conditions; `fork: …` has no code.)

### The slot record

The **last sector** of the slot partition: `"S0SR"`, version 1, `hdr_crc`,
`static_id`, `len`, source, zeros, CRC-32 of bytes `0..0x1FB` at `0x1FC`. Stage0
never reads it. It is keyed by the table CRC, so a record left behind by an image
`mps3-slot write` later replaced binds nothing. Two writers, both only after proof:

- **source 1, a push** — after its read-back passed;
- **source 2, the boot stamp** (2026-09-26) — for the slot stage0 booted, once
  harnessd has **confirmed** that boot to stage0, and only when: the boot was from
  A or B, `att_confirm` holds `0x4B4F3053`, there is no identity lock (so the fabric
  static_id is also the image's), the card's table CRC for that slot equals
  stage0's `image_hdr_crc`, the image ends before the record sector, and no record
  already holds (`hdr_crc`, fabric static_id). One sector, bounded to itself,
  flushed and read back; never image bytes; idempotent across harnessd respawns.

So a slot written outside this verb (`stage0_mkcard.py`, `mps3-slot write`, the
factory) gains a record on its first healthy boot, and "reboot into the new image,
`verify` the old slot, `rollback`" works. Until then it can become the default
through the verb only while it is the running slot (`verified: boot`), or through
`mps3-slot default` over SSH. `status` shows the record as the slot's `sid`.

### The lock (decided 2026-09-24)

Once the board's SSH is **claimed** (the TOFU `authorized_keys` exists — exactly
when `identify`'s `ssh.claimed` is `true`; both read the same state, on every
request), the slot **mutations** are refused from any peer that is not the board
itself (not `127.0.0.0/8`):

| mutation | refused with |
|---|---|
| `commit`, `rollback` | `{"ok":false,"err":"slot locked: board claimed (use ssh)"}` — checked first, before any card read or other refusal |
| a kind-2 push on 6910 | closed unread, like any refused push; **no state changes** (not even `job`) |
| a kind-2 push over TFTP | TFTP **ERROR 2** `access violation` (at the first DATA block, where the header arrives) |
| (2026-09-26) a `usd` action — `format`, `clear`, `rescan` | `{"ok":false,"err":"usd locked: board claimed (use ssh)","code":"locked"}` — first, before the action is judged; `usd` status stays open |
| (2026-09-26) the D13 re-push `commit` | `{"ok":false,"err":"commit locked: board claimed (use ssh)","code":"locked"}` — first, before the identity lock and every store refusal |
| (2026-09-26) XVC 2542 / `jtag_server` 6921 | one line `{"ok":false,"err":"xvc locked: board claimed (use ssh)","code":"locked"}` (resp. `jtag locked: …`), then close — see below |
| (v0.16) `identity_set` | `{"ok":false,"err":"identity locked: board claimed (use ssh)","code":"locked"}` — first, before any other check ("Identity"); `identity` (read) and `locate` stay open |
| (v0.17) `panel` with a `page` | `{"ok":false,"err":"panel locked: board claimed (use ssh)","code":"locked"}` — first, before the page is judged ("Presence and the panel"); `hello`, the `panel` state and its frame stay open |

- `status` and `verify` stay open: they only read. So does `usd` status.
- **The DUT debug ports** (HM_ANSWERS C3, the project lead 2026-09-26; `version.features`
  `xvc_lock`): on a claimed board, XVC 2542 and `jtag_server` 6921 serve the board
  itself only (an `ssh -L` tunnel's far end). Any other peer's connection is
  accepted, answered with ONE line —
  `{"ok":false,"err":"xvc locked: board claimed (use ssh)","code":"locked"}` on 2542,
  `{"ok":false,"err":"jtag locked: board claimed (use ssh)","code":"locked"}` on
  6921 — and closed (whatever it had already sent is drained first, so the line is
  not lost to an RST). hw_server / OpenOCD fail either way; the line tells a tool
  why. Checked at accept: a session opened before the claim keeps running. An
  unclaimed board is open, as before; a second client is still refused silently.
- **The store's mutations** (HM_ANSWERS S6, the project lead 2026-09-26): a `usd` action can
  wipe or invalidate the persisted overlays and a `commit` rewrites them — a remote
  denial of service on a claimed board — so they take the same lock. Neither can
  stop Linux booting (stage0 reads only p1/p2 and LBA 1–2). Bare metal: unchanged.
- **The owner goes through SSH**, which is the authentication: `mps3-slot` on the
  board, or an SSH port-forward to the board's `127.0.0.1:6900` / `127.0.0.1:6910`
  (a local peer). `pyverify slot` opens that tunnel by itself when `identify` says
  `claimed` (`--via-ssh` forces it, `--no-ssh` forbids it). A TFTP push cannot ride
  an SSH tunnel; use 6910.
- An **unclaimed** board is open to every peer, exactly like the claim's own
  trust-on-first-use window: whoever can reach the board first can claim it anyway.
- A claim locks and an unclaim (`mps3-unclaim`) unlocks **at once**, with no
  restart. Every refusal is logged with the peer's address.
- Bare metal is unaffected: it declines every slot act and push regardless.
- A later layer may add **signed images** (e.g. minisign, aligned with socharness's
  update channel), so even a tunnel could only install a release.

### Safety

- A push never writes the running slot or the default slot; every card write is
  bounded to the target slot's LBA range, or to LBA 1–2 for a flip, or to the
  booted slot's record sector for the boot stamp.
- Nothing long runs on the service loop: the flush, the read-back and the CRCs run
  in a forked child at nice 10 that holds only the card and a pipe. A card fault
  shows up as a failed job; the network services and the watchdog kick carry on.
- Implementation: `firmware/coordinator/coordinator.c` (the verb, weak provider
  `mps3_slot_op()`), `firmware/config_agent/config_agent.c` (kind 2, weak provider
  `mps3_cfg_slot_sink()`), `firmware/common/net_proto.c` (the reply),
  `src/linux_harness/sw/harnessd/slot_linux.c` (the Linux providers) over
  `slot_card.[ch]` (the card layer, shared with IMAGE's `mps3-slot`). Client:
  `pyverify.slot` and `pyverify slot status|push|commit|rollback|verify`.

## LCD mirror (TCP 6940) — v0.15 (Linux harness)

The HX8347-D panel's 320x240 RGB565 frame buffer, live, as dirty 16x16 tiles
(`docs/planning/linux_lanes/LCD_MIRROR_FPGA.md` §6.2, its six amendments and
Harness Manager's H1/H3, in HM's byte-level reading). Served by
`mps3-lcdmirror`, a nice-10 child of `mps3-harnessd`, on **127.0.0.1:6940 only**,
to **at most 2 clients**. Mode `sw` (today): harnessd's register tap feeds every
CLCD byte it writes into a C port of the GRAM golden model; exact for the
harness's own text screen, **blind** while the DUT owns the panel. Mode `hw`
(mint 4): the static-shell snooper, exact always. Same wire.
Reference client: `src/linux_harness/sw/harnessd/tests/lcdmirror_client.py`;
wire vectors: `src/linux_harness/sw/harnessd/tests/fixtures/lcdmirror_wire/`.

**Refusal.** A peer that is not loopback, or a third client, gets ONE line and an
immediate close (first byte `{`, never `L`):
`{"ok":false,"err":"lcd_mirror: not loopback (use an ssh port forward)"}` /
`{"ok":false,"err":"lcd_mirror: busy (2 clients)"}`.
A closed client's slot is free for the next connect at once (its hang-up is
handled before the next accept, and a full house re-checks for hung-up clients
before it refuses): one tool closing and the next connecting never sees `busy`
(unlike 6900, which resets a connect that races the previous client's close).
A client should still retry a refused (`ECONNREFUSED`: the child respawning) or
reset connect, and the `busy` line, with a short backoff.

**Framing** (both directions): `'L' 'M' u8 type, u8 rsvd=0, u32 len` (LE), then
`len` bytes. All integers little-endian; every pixel RGB565 little-endian.
`max_msg` (HELLO, <= 65536) bounds a WHOLE board message, the 8-byte header
included; a longer one is corrupt. A client body over 64 bytes, or a bad magic,
closes the connection; unknown client types are ignored.

| type | dir | body |
|---|---|---|
| `0x01` HELLO | board, first | JSON `{"proto":1,"w":320,"h":240,"fmt":"rgb565le","tile":16,"mode":"sw"\|"hw","static_id":"0x…","max_msg":65536,"boot_id":"<kernel boot_id>","rate":5,"rate_max":30,"clients_max":2}` |
| `0x02` UPDATE | board | below |
| `0x10` KEY | client | none: the next SNAP is a keyframe |
| `0x11` RATE | client, echoed | `u8` Hz. The board clamps to 0..`rate_max` (0 = pause: no SNAP, no source access) and answers with its own `0x11 u8` = the clamp, at once |
| `0x12` PING | client | `u32` token, answered by `0x13` PONG `u32` (the same token) at any time |
| `0x14` ACK | client | `u32` seq: every UPDATE up to it is consumed (cumulative) |

**The flow.** After HELLO the board sends nothing until the first KEY. Then, at
most `rate` times a second and **only while at most 2 UPDATEs are unACKed**
(H1: drop-to-latest at the source; changed tiles coalesce), it takes a SNAP
and sends the tiles that CHANGED since that client's last SNAP (dirty is not
changed), plus tiles that became VALID again. A keyframe (after KEY) carries
every VALID tile. A SNAP larger than `max_msg` spans several consecutive
UPDATEs, all with the same header fields; the last has `snap_last`. Nothing
changed (tiles, valid map, owner, status, MODE, resets) = no UPDATE; PING is the
liveness probe. `seq` is per connection, 1 for the first UPDATE, +1 each, wraps
at 2^32; after a gap the client sends KEY.

**UPDATE** body:

| field | type | meaning |
|---|---|---|
| `seq` | u32 | per connection, +1 per UPDATE |
| `t_ms` | u32 | the board's CLOCK_MONOTONIC ms at the SNAP (one value for all its parts) |
| `frames` | u32 | the snooper's FRAMES (window completions) at the SNAP |
| `resets` | u32 | panel resets seen (a change = the panel was reset: its GRAM may be gone) |
| `status` | u32 | [10:0] the snooper's STATUS (rst_n, bl, owner, display_on, standby, in_gram, fmt_ok, approx, viol, oob, rd_seen); [16] `exact` = hw && !viol && fmt_ok && !approx; [17] `text_only` (sw); [18] `blind` (sw, the DUT owns the panel or the KVM drives it mid-handover); [24] `key`; [25] `key_first`; [26] `key_last`; [27] `snap_last` |
| `owner` | u8 | 0 harness, 1 DUT (3 unknown, reserved); wins over status[2] |
| `valid` | u8[38] | bit t (byte t>>3, bit t&7) = tile t written since the last panel reset; HM hatches the rest |
| `regs` / `mode` | u8[256] / u32 | `key_first`: the 256-register log (byte i = last datum to index i); otherwise MODE = R16 \| R17<<8 \| R36<<16 \| R01<<24 |
| `ntiles` | u16 | then `ntiles` x {`u16 idx` (= ty*20+tx), `u8 enc`, `u16 len`, payload} |

Tile payloads (16x16, row-major): `0` FILL: one u16. `1` PAL1: 2 colours, then
16 u16 rows, bit x set = colour 1. `2` PAL2: 4 colours (a 3-colour tile repeats
colour 0), then 16 u32 rows, pixel x = bits [2x+1:2x]. `3` RLE16: PackBits over
u16 — `0x80|(n-1)` then one u16 is a run of n, `n-1` then n u16 are literals,
n <= 128. `4` RAW: 256 u16. The board sends FILL for one colour, else RLE16 if
under 512 B (else RAW), replaced by PAL1/PAL2 only if strictly smaller.

## Diagnostics (`diag`) — the always-on counter mailbox

```
→ {"op":"diag"}
← {"ok":true,"rx_recover":0,"rx_dumps":0,"rx_drops":0,"icap_bytes":0,"got":0,
   "expect":0,"rcv_wnd":0,"rcv_ann_wnd":0,"rx_queued":0,"pbuf_free":0,
   "grants_sent":0,"grant_fails":0,"sndbuf":0,"snd_wnd":0,
   "tx_frames_sent":0,"tx_status_drained":0,"tx_fifo_full_drops":0,"tx_errors":0,
   "tx_space_stalls":0,"tx_iface_errors":0,"tx_last_status":0,"icap_sr_last":0,
   "icap_eos_status":0,"ovlstore_phase":0,"ovlstore_detail":0,
   "touch_regs":0,"touch_adc_x":0,"touch_adc_y":0,"touch_verdict":0,
   "svc_count":12,"pass_max_us":1840,"svc_max_us":900,"svc_max_ix":4,
   "svc_overruns":0,"svc_skips":0,"svc_skipped":0,
   "svc_us_0":0,"svc_us_1":0,"svc_us_2":0,"svc_us_3":0,"svc_us_4":0,
   "svc_us_5":0,"svc_us_6":0,"usd_boot":0}
```

(One line on the wire; wrapped here for reading.)

No arguments, never held, and **no failure mode** — the counters are plain RAM,
so the only reply is `ok:true`. All forty-four values (forty-two before v0.13) are unsigned 32-bit,
emitted as plain decimal, and **the key order above is the wire order**.

**Omitted keys (v0.11 additive).** A key the engine cannot fill is left OFF the
line — never reported as a plausible 0; the rest keep this order. Bare metal
omits nothing. The Linux harness (`mps3-harnessd`) has no lwIP pcb, no
bare-metal LAN9220 driver and (until the L2 block-device store) no QSPI overlay
store, so it omits these fifteen: `rx_recover`, `rx_dumps`, `rcv_wnd`,
`rcv_ann_wnd`, `rx_queued`, `pbuf_free`, `sndbuf`, `snd_wnd`,
`tx_status_drained`, `tx_fifo_full_drops`, `tx_space_stalls`, `tx_iface_errors`,
`tx_last_status`, `ovlstore_phase`, `ovlstore_detail`. It fills `rx_drops`,
`tx_frames_sent` and `tx_errors` from the kernel's `eth0` counters, and the
`svc_*` words from the same service table (slots 0–2 are its own services:
`ident`, `kmsg`, `persist`; 3–11 are the bare-metal services at the same
indices). The JTAG-readable mailbox itself keeps the full `diag.h` layout (the
omitted words read 0 there).

The shell's superloop republishes this struct every pass, so the reply is the
live idle-time view of the over-the-wire-reconfig receive path:

| key | meaning |
|---|---|
| `rx_recover` | LAN9220 RX-overrun recovery firings |
| `rx_dumps` | of those, `RXE`/`RWT` FIFO dumps |
| `rx_drops` | MAC frames dropped to overrun |
| `icap_bytes` | bytes written to `HWICAP.WF` (equal to `got` ⇒ the ICAP is keeping up) |
| `got` / `expect` | in-flight partial receive progress vs its total |
| `rcv_wnd` / `rcv_ann_wnd` | the 6910 pcb's receive window (0 while stalled ⇒ the window is not reopening to the host) |
| `rx_queued` | bytes queued in the 6910 connection's pbuf chain |
| `pbuf_free` | free lwIP `PBUF_POOL` buffers (0 ⇒ inbound frames dropped for want of a pbuf). `4294967295` means lwIP stats are compiled out |
| `grants_sent` | full windows reopened (the windowed 6910 flow control's "windows paced" quantity) |
| `grant_fails` | legacy; 0 now that no grant byte is sent |
| `sndbuf` / `snd_wnd` | the 6910 pcb's `tcp_sndbuf` and the peer's advertised window |
| `tx_frames_sent` | frames accepted into the LAN9220 TX DATA FIFO (v0.9) |
| `tx_status_drained` | TX status words reaped from the TX STATUS FIFO (v0.9) |
| `tx_fifo_full_drops` | `linkoutput` `ERR_MEM`: no `TDFREE` after the full spin (v0.9) |
| `tx_errors` | popped TX status words carrying a transmit-error bit (v0.9) |
| `tx_space_stalls` | `linkoutput` calls that had to spin on `TX_SPACE` (v0.9) |
| `tx_iface_errors` | `linkoutput` `ERR_IF`: oversize frame or driver hard error (v0.9) |
| `tx_last_status` | raw last TX completion status word (decode aid) (v0.9) |
| `icap_sr_last` | raw last non-zero `HWICAP_SR` at finish (v0.9) |
| `icap_eos_status` | 0 = none, 1 = EOS seen, 2 = EOS timeout (v0.9) |
| `ovlstore_phase` | `OVL_PHASE_*` of the in-flight overlay-store step (v0.9). Since D13 only IDLE, INIT, READ_HDR, PROGRAM_PAGE, CRC and STREAM occur (the user-microSD store); the SST26 phases are retired, numbers reserved (`overlay_store.h`) |
| `ovlstore_detail` | was the SST26 flash offset that phase worked on (v0.9); always 0 since D13 |
| `touch_regs` | STMPE811 init read-backs, packed: `[7:0]` `GPIO_AF`, `[15:8]` `SYS_CTRL2`, `[23:16]` `TSC_CFG`, `[31:24]` `ADC_CTRL1`. Read as hex; each byte must equal what init wrote (v0.9.1) |
| `touch_adc_x` | panel probe, X plate: `[11:0]` X+, `[23:12]` X− (12-bit ADC, driven-high reading), `[31:24]` the `TSC_I_DRIVE` read-back (v0.9.1) |
| `touch_adc_y` | panel probe, Y plate: `[11:0]` Y+, `[23:12]` Y−, `[31:24]` status bits (`TOUCH_PROBE_ST_*`: ran / read-back error / read-back differs / ADC error / a line did not follow its drive) (v0.9.1) |
| `touch_verdict` | **the answer**: 0 = unknown (no probe ran, or it could not complete), 1 = chip-misconfigured (a read-back differs from what init wrote), 2 = panel-open, 3 = panel-present (v0.9.1) |
| `svc_count` | services in the superloop table (12 today) (v0.9.2) |
| `pass_max_us` | worst FULL superloop pass, microseconds. High-water since boot, like every figure below (v0.9.2) |
| `svc_max_us` / `svc_max_ix` | worst SINGLE service and its table INDEX (`firmware/platform/src/main.c`'s `s_services`: 0 `net_rx`, 1 `net_tmr`, 2 `tx_drain`, 3 `swap`, 4 `cfgagent`, 5 `ctrl`, 6 `jtag`, 7 `xvc`, 8 `uart`, 9 `clcd`, 10 `diag`, 11 `hbeat`) (v0.9.2) |
| `svc_overruns` | budget overruns, total, across all services (v0.9.2) |
| `svc_skips` | healthy→sick EDGES. A service that STAYS sick does not keep inflating this (v0.9.2) |
| `svc_skipped` | **bitmask of services currently SKIPPED** for overrunning their budget on 3 consecutive passes. Non-zero is a wedge in progress; the loop probes each one once per 100 ms until it comes in under budget (v0.9.2) |
| `svc_us_0`…`svc_us_5` | per-service worst case, **two per word**: service 2·p in bits `[15:0]` of `svc_us_<p>`, service 2·p+1 in `[31:16]`, each a 16-bit **saturating** microsecond count. `0xFFFF` means "≥ 65535 µs", never a wrapped small number (v0.9.2) |

**Reachable only when the shell is IDLE.** The 6900 control connection is
*parked* for the duration of a `swap` (see "Swap sequence" below), so this verb
cannot be used to watch a swap in progress. That is deliberate, and the same
counters are pinned at a fixed DMEM address for JTAG-MDM readout precisely so a
swap — or a wedge — can be inspected when the control channel is unavailable.
See `firmware/common/diag.h`.

**Key names are wire-stable, not semantics-stable.** `grants_sent` now carries a
different (equivalent) quantity than the counter it was named for; the key was
kept for client compatibility rather than renamed. Treat these as an
instrumentation stream, not a contract on internals.

## User microSD (`usd`) and `commit` — v0.13 (D13)

The shell keeps the last committed DUT overlay on the **user** microSD. This is
not the MCC config card. The shell reloads that overlay at power-on. Design:
`docs/planning/HANDOVER_USD_OVERLAY_STORE.md`.

**Where the store lives.** The store is an MBR partition of TYPE `0xDA`,
found by type, not by position. The Linux card builder puts it at p4, LBA 2048,
32 MiB. A card without one is `foreign` and is **never written**, except by the
explicit wipe below.

### `usd` — status (never held; answers during a swap)

```
→ {"op":"usd"}
← {"ok":true,"present":true,"state":"valid","text":"led [A]","card_mb":15193,
   "default":{"rm_id":"0x0100001e","static_id":"0x72bb0a36","slot":"A"},"boot":"loaded"}
```

- **`present`**: a card is in the slot (card detect).
- **`state`** is one of `no_hw` · `none` · `init` · `unsupported` · `error` ·
  `foreign` · `empty` · `valid` · `stale` · `bad`.
  - `no_hw`: this fabric has no `usd_spi`.
  - `init`: the card is being probed, or the store is being verified.
  - `stale`: the active default was minted for another shell (`static_id`).
  - `bad`: both slots fail CRC.
- **`text`**: exactly what the CLCD status page shows after `USD : ` (at most 16
  chars). It carries `skipped` when PB1 was held at power-up.
- **`card_mb`**: present only when a card is ready.
- **`default`**: present only when `state` is `valid` or `stale`.
- **`boot`**: `loaded` · `skipped` · `none` · `pending` · `failed:<reason>`. This is
  the power-on decision, made **once per FPGA configuration**. A harnessd respawn,
  an OS reboot or a WDOG reset never reloads.
- **With no card:** `{"ok":true,"present":false,"state":"none","text":"none","boot":"none"}`.
- **`boot` values in detail:**
  - `none`: nothing was eligible to load (no card, or the store was `empty`,
    `foreign`, `stale`, `bad` or `error` at the decision).
  - `skipped`: PB1 was held at the decision, whether or not a card was present.
  - `failed:<reason>`: a load was attempted and failed. The reason is free text
    for humans (e.g. `too big`, `aborted`, `identity lock`, or a swap-FSM state).
    Clients treat any `failed:` prefix as a failure and must not parse the reason.
- **`card_mb`** appears once the card's size is known, so it can appear during
  `init` (the store verifying) and in every store state after that.
- **Action arguments:** an empty `action` string is a status request. A `action`
  or `confirm` that is not a string, or an unknown action, answers `bad args`.
- **`rescan`** answers `{"ok":true,"state":"init"}`, because the probe has restarted.
  Poll `usd` for the result.
- **Refusal order is unspecified** (for example, `static_id` checked before card
  presence, or the reverse). Clients must not depend on which of two applicable
  refusals is returned.
  Absence is not an error.

### `usd` — actions

```
→ {"op":"usd","action":"format","confirm":"erase"}      plain format (rules a/b/c)
→ {"op":"usd","action":"format","confirm":"erase-all"}  explicit wipe (bare metal only)
→ {"op":"usd","action":"clear"}                         invalidate the default (greybox at next power-on)
→ {"op":"usd","action":"rescan"}                        re-probe the card now
← {"ok":true,"state":"empty"}   |   {"ok":false,"err":"<name>"}
```

(Linux harness, 2026-09-26: on a CLAIMED board every action — and the re-push
`commit` below — from a peer that is not the board itself answers
`{"ok":false,"err":"usd locked: board claimed (use ssh)","code":"locked"}`
(`commit locked: …`), first. See "Slot images" → "The lock".)

**Plain format** writes only when one of these holds:
- **(a)** a `0xDA` partition exists: format re-initialises the store inside it;
- **(b)** the card is truly blank: LBA 0 is zero or an empty signed MBR, and no
  FAT/exFAT/NTFS/GPT/ext/f2fs/swap/ISO/btrfs signature is found. Format creates
  a `0xDA` partition over the last 32 MiB.

Otherwise it refuses with `filesystem present` or `exists` and writes nothing.

**The wipe** (`erase-all`):
- zeroes LBA 1..33;
- then writes a fresh MBR whose only entry is `0xDA`.

It is refused with `wipe disabled` under Linux, where the card also holds the
running system.

### `commit` — re-push into the inactive slot (REPLACES v0.11 `commit`)

```
→ {"op":"commit","rm":"led","src":"tcp","rm_id":"0x0100001e","static_id":"0x72bb0a36",
   "clear_len":68332,"clear_crc":"0x…","part_len":1251884,"part_crc":"0x…"}
   (control connection PARKED; push clearing, then partial, over 6910 exactly as for swap)
← {"ok":true,"slot":"B"}
```

- **It persists only what is running.** It is refused unless all of these hold:
  - `static_id` equals the shell's own;
  - `rm_id` equals the live `DFXCTL.RM_ID`;
  - the store is `empty`, `valid`, `bad` or `stale`. Committing over a stale card
    is how a re-keyed board recovers.
- **Write order:** the pair goes to the **inactive** slot and is read back with its
  CRCs checked. Only then does the header flip.
- **Failure:** a failure or abort at any point leaves the previous default intact.
- **Timeout:** the same idle timeout as `swap`.
- **Lengths and CRCs** are CRC32 (zlib) over the exact payload bytes: the `.bin`
  clearing and partial, NOT the 6910 framing header.
- **A pair larger than one 8 MiB slot** is refused with `bad args`.
- **A commit whose `static_id` is not the shell's** is refused with `stale key`.
- **`src`:** only `"tcp"` in v0.13.
- **Clients:** pyverify's `deploy(persist=True)`, the default, commits the same
  pair after a verified swap. With `present:false` it skips silently. A commit
  failure is a warning, never a deploy failure.

**Error names** (for `usd` and `commit`):

| Class | Names |
|---|---|
| Card state | `no card` · `no hw` · `foreign` · `stale key` · `unavailable` |
| Format / wipe | `filesystem present` · `exists` · `partition too small` · `confirm required` · `wipe disabled` |
| Commit | `rm mismatch` · `crc` |
| Store / device | `store busy` · `io` · `timeout` |
| Request | `bad args` |

Also: `identity lock: <reason>` (Linux, see "Identity lock").

### Power-on load

**When it runs:** once per FPGA configuration, after the network is up.
- The loader's gates:
  - the card is ready;
  - the store is `valid`;
  - the active slot's `static_id` equals the shell's;
  - both CRCs pass in full, **before** any byte reaches ICAP;
  - PB1 is not held.
- If all pass, the default is loaded through the ordinary swap FSM, as a swap with
  an internal source.

**What is visible from outside:**
- `boot` goes from `pending` to `loaded` in `usd`.
- The swap counters move as for any swap.
- The control channel stays responsive throughout. A `swap` issued while the
  power-on load is running gets the same refusal as any mid-swap `swap`.

## Swap sequence (the inner loop) — protocol view — I2 RESOLVED
**Decision I2: the SHELL owns clearing-bitstream sequencing.** The shell always
holds the *currently-loaded* RM's clearing bitstream (in RAM since D13 — the
QSPI clearing cache is gone; tracked by the coordinator — see `overlay-manifest.md`), so the host never has to know
what is running. On a `swap` the host supplies only the **incoming** RM's pair
(clearing + partial); the shell applies the **outgoing** RM's cached clearing
first, then the new partial, then caches the new RM's clearing for next time.

Server-side ordering the `swap` op drives (see spec §6.2):
1. gate XVC/SWD/UART/VPHY link; assert DECOUPLE + hold `rp_resetn` (regmap DFXCTL)
2. stream the **cached clearing bitstream of the currently-loaded RM** → HWICAP
3. stream **new RM partial** (received via TFTP/6910) → HWICAP
4. verify RM load (`rm_id` + CRC / DFX monitor)
5. cache the new RM's clearing bitstream as the current one (from its pair)
6. release DECOUPLE, deassert `rp_resetn`
7. respond with confirmed `rm_id` + `verified`

Post-power-on the "currently-loaded" clearing = the greybox's, which ships
inside the shell image (`overlay-manifest.md`). The host `swap` payload carries
both files of the incoming pair; a library that keeps clearing bitstreams
resident (QSPI/eMMC keyed by `rm_id`) is the v2 optimisation.

### Push-vs-swap ORDERING (v0.7 — NORMATIVE, and it REVERSES v0.2)

**`swap` FIRST, then push, then read the reply. The push happens *inside* the
swap.** This is the single most important ordering statement in this document.

```
    host                                   shell
    ----                                   -----
    {"op":"swap","rm":...,"src":...}  -->  (control connection PARKED; no reply yet)
                                           FSM: GATE -> DECOUPLE -> stream cached
                                           clearing -> AWAIT_INCOMING_CLEARING
    push clearing  (TFTP/69 or 6910)  -->  accepted; FSM -> AWAIT_PARTIAL
    push partial   (TFTP/69 or 6910)  -->  accepted; FSM -> stream -> verify -> release
    (blocking read on 6900)           <--  {"ok":true,"rm_id":"0x...","verified":true}
```

1. The host sends `swap` and **does not wait for the reply**.
2. The shell **parks the 6900 control connection for the whole reconfiguration**
   and drives its FSM to `SWAP_AWAIT_INCOMING_CLEARING`. *This is why 6900 is
   parked* — the reply cannot come until the bitstream the host has not sent yet
   has arrived, been loaded and been verified.
3. Only now does the shell accept a bitstream. The host pushes the incoming
   pair, **clearing first, then partial**, over TFTP/69 or raw 6910.
4. The host then reads the parked reply off 6900.

**A push that arrives when the shell is not awaiting one is REJECTED.** The
shell is not listening for bitstream data outside a swap: it **resets the 6910
connection** (the host sees `ECONNRESET`; the shell's `diag.got` never leaves 0)
and answers a TFTP push with an ERROR packet. A `swap` issued afterwards then
fails closed (`{"ok":false,"err":"swap failed"}`) because nothing was staged.
Servers MUST fail closed here rather than accept early data: `swap_fsm.c`'s
ICAP-direct sink refuses unless the FSM is in `SWAP_AWAIT_PARTIAL`, because the
partial streams *straight into HWICAP* as it arrives and the RP must already be
decoupled + held in reset before the first config frame lands.

> **This REPLACES the v0.2 "push-vs-swap staging" clause**, which said the host
> "pushes **both** files of the incoming pair *before* sending `swap`" and
> required the server to hold a complete validated pair *ahead of* the swap.
> **That was wrong, and it was believed for months**: pyverify implemented it
> (`push_pair()` -> `swap()`), and its FakeShell test double implemented it too
> — so an end-to-end suite that was green against a fake *more permissive than
> the firmware* never caught it. Corrected against the KU115 on 2026-07-14 (the
> `led` RM swapped over Ethernet, `DFXCTL.RM_ID` = `0x0100001E`, no JTAG). The
> over-the-wire reconfig status this corrected is tracked in `docs/STATUS.md`.

Ordering *within* the pair is unchanged and still normative: **clearing before
partial** (I2). One `swap` consumes one pair; the next swap needs a fresh one,
starting again with a clearing.

**Timeouts.** The shell's `SWAP_AWAIT_*` states have an **idle** timeout: it is
re-armed on RX progress, so a multi-MB partial cannot time out its own swap, but
a swap nobody pushes into fails closed rather than parking the host forever.
Symmetrically, the host's read of the parked reply must tolerate the *entire*
reconfiguration — a short client socket timeout here looks exactly like an
unreachable shell and is not one.

Client reference: `pyverify.client.ShellClient.swap_begin()` / `.swap_await()`,
sequenced by `pyverify.swap.SwapOrchestrator.deploy()`. Server reference (and
executable spec): `pyverify.testing.fakeshell.FakeShell`, which enforces every
paragraph above.

The host then re-attaches: reload `.ltx` for XVC, re-run SWD line-reset + DP
connect, reopen console TCP sockets (all client-side, no new server verbs).

## Bitstream framing (TFTP / raw 6910)
Payload is a **`.bin`** (ICAP bit-ordering), preceded by a small header the
config agent validates before touching ICAP:
```
magic "MPS3" | u16 ver | u8 kind(0=clearing,1=partial,2=slot image v0.14) | u8 rm_slot
u32 static_id | u32 rm_id | u32 len_words | u32 crc32(payload)
```
A torn/mismatched transfer (bad crc, wrong `static_id`) is rejected **before**
any ICAP write. `static_id` must match the running shell — partials are only
valid against their exact static (see `overlay-manifest.md`).

- **I12 (units):** `len_words` here = payload bytes ÷ 4 (ICAP is word-wide).
  The manifest's `len` is **bytes**; `len_words = len/4`. The payload length is
  the source of truth — the pusher derives `len_words` from the actual `.bin`,
  never trusts the manifest blindly. (`len` must be a multiple of 4.)
- **I13 (crc32):** all `crc32` fields (this header + the manifest) are the
  standard **zlib/IEEE 802.3 CRC-32** (`zlib.crc32`, poly 0xEDB88320), computed
  over the raw `.bin` payload bytes.
- **Pair coherence (v0.2):** within a pushed pair, `clearing.rm_id` **must
  equal** `partial.rm_id` (both are the *incoming* RM's). Receivers order by
  `kind` (clearing then partial) and are not required to enforce the rm_id
  equality; if a malformed pair disagrees, RM-load verify runs against the
  **partial's** `rm_id` (I14/I25). Pushers must never emit a mixed pair.

### Transport semantics (v0.2)
- **TFTP (69)** carries a real accept/reject signal: the final `ACK` means
  the frame validated server-side; a TFTP `ERROR` packet at any point means
  the push was refused (bad header/CRC/order/static_id) or torn. A rejected
  header is refused *before* payload blocks are consumed.
- **Raw TCP (6910)** defines **no response payload in either direction**:
  the client connects, sends header+payload, half-closes (`SHUT_WR`), and
  the **server closing the connection is the "validation finished" signal**
  — accept and reject look identical on the wire. Clients should drain to
  EOF as their synchronisation point; acceptance is only observable via the
  control channel (e.g. a subsequent `swap` failing with "no validated
  pair"). Prefer TFTP when reject feedback matters.
- **Filenames are advisory** on both transports: the TFTP WRQ filename (the
  reference client sends `clearing.bin` / `partial.bin`) is ignored for
  routing/validation — the 24-byte header is the sole authority on what the
  payload is.

### Windowed 6910 (opt-in, v0.5)

> **Linux harness (v0.11):** `mps3-harnessd` builds `config_agent` PLAIN (no
> `WINDOWED`) and does not report `windowed`. The shipping window-as-grant
> scheme sends **zero application bytes** (it only paces the TCP window), and a
> kernel socket is already paced by its consumer, so the wire is identical: both
> pyverify pushers — plain `tcp_send` and `tcp_send_windowed` — and TFTP work
> against it (tested: `src/linux_harness/sw/harnessd/tests/test_harnessd_e2e.py`).
> The 1-byte `0x06` grant described below is the RETIRED v0.5 form.
The fire-and-hose 6910 push (above) can outrun the shell's single-threaded
receive loop: the host `sendall`s the whole frame at wire speed while the shell
consumes it one bounded poll at a time, and the TCP receive window / pbuf pool
stops reopening mid-stream — the transfer **stalls** (HW-observed on a ~1.3 MB
partial; the root cause is the lwIP/TCP **receive** path, *not* the MAC RX FIFO
and *not* the HWICAP). **Windowed mode** is an optional application-level flow
control that removes that race by lock-stepping the host to the shell's real
drain rate. It is **opt-in on BOTH ends and they must match** — enabling it on
one side only will hang. The default stays fire-and-hose (this section does not
apply unless both ends turn it on).

Wire sequence (all on the SAME 6910 connection, no second socket):
1. Host connects and sends the 24-byte header, then the first payload chunk of
   up to `WINDOW` bytes. `WINDOW` **must** be ≤ the shell's lwIP receive window
   so back-pressure never builds, and **must equal** the shell's compiled chunk
   size (they are a matched pair — see the flags below).
2. After each chunk the host **blocks reading exactly one 1-byte grant** (`0x06`,
   ASCII `ACK`) from the shell, then sends the next `WINDOW`-byte chunk. Grants
   are counted per payload chunk: a payload of `P` bytes is exactly
   `ceil(P / WINDOW)` chunks and therefore exactly that many grants (the header
   is **not** granted separately — it rides with the first chunk).
3. The shell writes each grant **only after that chunk has DRAINED to the sink**
   — i.e. the HWICAP.WF writes (ICAP-direct) or QSPI page-program for the chunk's
   bytes have completed. The grant means *"consumed, send the next"*, **never**
   *"received"* — acking on receipt would reintroduce the exact consumer-rate
   race this mechanism exists to kill.
4. The **final** grant (for the last, possibly short, chunk) is written only
   **after the shell has finished** the transfer: the sink's end-of-stream drain,
   the transport CRC gate, and staging must all succeed first. The host's last
   blocking read therefore confirms the whole push landed **and was accepted**.
5. On success the host half-closes (`SHUT_WR`) and drains to EOF as before.

**Inline-on-6910, not on 6900:** the grant is deliberately carried back on the
6910 push connection itself, **not** on the 6900 control channel. A `swap` PARKS
6900 for the whole swap (a second 6900 connection is refused), so 6900 is
unavailable for acks mid-swap — 6910 is the only channel guaranteed open while
the RP is decoupled and streaming.

**Fail-closed:** any sink/ICAP write error, a CRC mismatch, or a finish failure
aborts the transfer and the shell closes the connection **without** the pending
grant. The host's blocking grant-read then returns EOF, which it surfaces as a
push failure (the reject signal the plain 6910 push lacks — cf. TFTP's ERROR).
A stalled sink simply never grants; the host times out on its grant-read.

**Enabling it (matched pair):** firmware — compile `config_agent.c` with
`-DMPS3_CFG_AGENT_WINDOWED` (default off ⇒ byte-identical fire-and-hose) and
optionally `-DCFG_AGENT_ACK_WINDOW_BYTES=<N>` (default 4096). Host — call
`pyverify.pusher.tcp_send_windowed(frame, host, port, window=<N>)` or
`BitstreamPusher(transport="tcp", windowed=True, window=<N>)`. The host `window`
and the firmware `CFG_AGENT_ACK_WINDOW_BYTES` **must be equal**.

## SWD channel (TCP 6920) — OpenOCD remote_bitbang — RETIRED
> **Retired with the SWD→JTAG cutover (`0xCD74B6AE`); no image serves 6920.**
> The live debug port is JTAG on 6921 (`firmware/jtag_server/`, the same
> `remote_bitbang` byte protocol in its JTAG form). This section is kept because
> `firmware/test/test_swd_server.c` still pins the encoding below, which
> `jtag_server.c` inherits.

Byte protocol per OpenOCD ≥ 2021-01 `remote_bitbang` w/ SWD:
`O`=SWDIO drive, `o`=SWDIO release, `c`=sample SWDIO; `d/e/f/g`={CLK,DIO}
combos; `r/s/t/u`=trst/srst; `B/b`=LED; `Q`=quit. srst maps onto `dbg_resetn`.

**Bit order CONFIRMED** (2026-07-09, was "confirm at bring-up" — SWD half of
I22). The encoding belongs to the host driver, so it is fixed by
OpenOCD `src/jtag/drivers/remote_bitbang.c`, not by our wiring:

| byte | formula | meaning |
|---|---|---|
| `d`..`g` | `'d' + ((swclk ? 0x2 : 0) \| (swdio ? 0x1 : 0))` | CLK = bit1, DIO = bit0 |
| `r`..`u` | `'r' + ((trst ? 0x2 : 0) \| (srst ? 0x1 : 0))` | TRST = bit1, SRST = bit0 |
| reply to `c` | ASCII `'0'` / `'1'` (0x30/0x31) | anything else → OpenOCD logs an error and quits |

SWD has no TRST wire, so `t`/`u` differ from `r`/`s` only in a bit the shell
ignores. Pinned by `firmware/test/test_swd_server.c`'s
`test_openocd_remote_bitbang_conformance()`, which re-derives each byte from the
formulas above rather than hardcoding letters.
