/*
 * config_agent.h — TFTP/TCP bitstream receiver -> validated payload handoff.
 * See config_agent/README.md and docs/contracts/net-protocol.md.
 *
 * I2 model update: net-protocol.md's swap sequence has the host push only
 * the INCOMING RM's {clearing, partial} pair per swap; the shell already
 * holds the OUTGOING RM's clearing from its own runtime cache
 * (coordinator/swap_fsm.h's g_current_rm_clearing). So this module's
 * "enforce clearing-then-partial order" responsibility is real and
 * unambiguous: within one pair, the incoming clearing must complete before
 * a partial push is accepted.
 *
 * STAGING DECISION (W-NET-SEAM, resolving the fakeshell-flagged
 * "push-vs-swap interleaving" ambiguity): net-protocol.md's sequence has
 * the host push BOTH files of the pair BEFORE issuing `swap` — a
 * single-slot receive session could therefore never accept the pair's
 * partial until a swap was already in flight (the deadlock
 * pyverify.testing.fakeshell's ConfigAgentModel documents). This module
 * now stages a FULL {clearing, partial} PAIR — two independent slots, each
 * with its own payload buffer — exactly matching that reference model:
 *   - a completed clearing occupies the clearing slot, DROPS any stale
 *     staged partial (a clearing starts a new pair), and sets the ordering
 *     flag;
 *   - a completed partial occupies the partial slot and CLEARS the
 *     ordering flag (the next pair must again start with a clearing);
 *   - swap_fsm consumes the two slots via the same
 *     config_agent_take_validated_clearing()/_partial() intake points as
 *     before (clearing first, matching its state order).
 * Flag for A6: net-protocol.md should state the two-slot requirement
 * explicitly (fakeshell.py's docstring carries the same flag) — the wire
 * protocol itself is unchanged.
 *
 * STAGING BUFFERS: small payloads land in two static RAM buffers of
 * MPS3_CFG_AGENT_STAGING_BYTES each (one clearing, one partial). On the host
 * harness these are plain BSS. A LARGE partial — one whose payload exceeds
 * the RAM buffer — does NOT need a MiB-scale .bss buffer: it is streamed
 * into a QSPI scratch region as it arrives (page-programmed via the
 * MPS3_CFG_AGENT_QSPI_SINK seam below), so a MiB-scale RM partial can move
 * over the net board-free with only a small RAM footprint. This retires the
 * old "2 x 4 MiB won't fit on-chip BRAM, needs DDR or a downward override"
 * gate: the partial no longer lives in LMB .bss at all, so
 * MPS3_CFG_AGENT_STAGING_BYTES only has to hold a CLEARING (kept resident
 * for the I2 clearing cache) plus a small-partial fast path — the ELF build
 * therefore no longer needs its -DMPS3_CFG_AGENT_STAGING_BYTES=8192 crutch
 * to bound the partial (it now bounds only clearings; size it to the largest
 * clearing you support). Streaming straight to HWICAP with no buffering at
 * all was still REJECTED: the CRC must pass before any ICAP write
 * (net-protocol.md) — hence receive-to-QSPI-then-verify-then-stream, with the
 * QSPI-resident copy CRC-checked before the swap FSM streams it.
 *
 * D13 (2026-09-23) SUPERSEDES THE QSPI HALF OF THE NOTE ABOVE: the SST26/QSPI
 * backend is deleted (0x44A4 is usd_spi now). Large partials stream
 * ICAP-direct (Path 3, below); clearings are RAM-resident (the target sizes
 * MPS3_CFG_AGENT_CLEARING_RAM_BYTES for the largest); the two QSPI sink seams
 * survive only as generic streaming-sink hooks nothing in production
 * registers. The one new routing is the v0.13 COMMIT sink (below), which takes
 * a whole pair to the user microSD while a commit is armed.
 */
#ifndef MPS3_CONFIG_AGENT_H
#define MPS3_CONFIG_AGENT_H

#include <stdint.h>
#include "../common/net_proto.h"
#include "../common/net_if.h"      /* mps3_net_addr_t (the v0.14 slot lock's peer) */

#ifdef __cplusplus
extern "C" {
#endif

struct mps3_net_conn; /* opaque (common/net_if.h) — for config_agent_active_tcp_conn() */

typedef enum {
    CFG_AGENT_OK = 0,
    CFG_AGENT_ERR_MAGIC,      /* header magic != "MPS3" (or unpack failed / short) */
    CFG_AGENT_ERR_VERSION,    /* header ver not supported */
    CFG_AGENT_ERR_STATIC_ID,  /* static_id != running shell's (overlay-manifest.md) */
    CFG_AGENT_ERR_KIND,       /* kind not clearing(0)/partial(1) */
    CFG_AGENT_ERR_CRC,        /* payload crc32 mismatch */
    CFG_AGENT_ERR_ORDER,      /* clearing-then-partial ordering violated (I2: real now) */
    CFG_AGENT_ERR_SIZE,       /* len_words too large for the staging buffer */
    CFG_AGENT_ERR_TRUNCATED,  /* transport closed before len_words received */
    CFG_AGENT_ERR_ACCESS,     /* v0.14: the engine refuses THIS PEER a slot image (TFTP error 2) */
} config_agent_status_t;

/* Advisory cap on a single staged payload -- config_agent_validate_header()
 * rejects (CFG_AGENT_ERR_SIZE) before ever accumulating a payload larger
 * than this many words. Pure firmware-side policy, not a contract value;
 * sized generously above the ~2 MB partial example in overlay-manifest.md
 * (2 MB / 4 = 524288 words). */
#define MPS3_CFG_AGENT_MAX_PAYLOAD_WORDS (4u * 1024u * 1024u / 4u)

/* Per-slot RAM staging buffer size in BYTES. This now bounds ONLY the
 * SMALL-clearing and SMALL-partial RAM fast paths: a clearing OR a partial
 * LARGER than this is streamed to a QSPI region instead (the partial to the
 * inactive A/B slot; the clearing to the dedicated clearing-STAGE region — see
 * the file-header STAGING BUFFERS note, the MPS3_CFG_AGENT_QSPI_SINK seam, and
 * config_agent_set_qspi_clearing_sink()), so this value NO LONGER bounds the
 * moveable clearing OR partial size. That is the gate this closes: nanosoc's
 * 117,684-byte clearing used to be REJECTED here (it could not fit two RAM
 * buffers) — now it goes to QSPI and STAGING_BYTES can be a few KiB.
 * >>> TARGET ELF: set -DMPS3_CFG_AGENT_STAGING_BYTES=4096 (was 8192). With the
 *     clearing no longer RAM-bound, 4 KiB is ample for the small-RM fast path;
 *     this frees ~2×(8192-4096)=~8 KiB of LMB .bss (s_clearing_ram +
 *     s_clearing_arena), and — more importantly — REMOVES the ~128 KiB×2
 *     that a RAM-resident nanosoc clearing would have needed, which is what
 *     makes nanosoc network-swap fit at all.
 * Default 64 KiB (host test build, unoverridden): keeps the RAM/QSPI threshold
 * generous so existing small-frame tests are byte-for-byte unchanged. */
#ifndef MPS3_CFG_AGENT_STAGING_BYTES
#define MPS3_CFG_AGENT_STAGING_BYTES (64u * 1024u)
#endif

/* Per-slot RAM buffer for the PARTIAL fast path. Since the gate-2 QSPI sink
 * streams any partial LARGER than this straight to flash (never RAM), this
 * bounds only the small-partial-in-RAM fast path — it does NOT bound the
 * moveable partial size. Split from MPS3_CFG_AGENT_STAGING_BYTES so a
 * memory-tight target (128/256 KiB LMB) can shrink the dead partial RAM
 * slot to a few KiB while the CLEARING slot stays big enough for a real
 * clearing (~48 KiB). Default = the clearing size, so the host test build
 * (which never overrides it) is byte-for-byte unchanged: both slots stay
 * MPS3_CFG_AGENT_STAGING_BYTES and the RAM/QSPI threshold is identical. */
#ifndef MPS3_CFG_AGENT_PARTIAL_RAM_BYTES
#define MPS3_CFG_AGENT_PARTIAL_RAM_BYTES MPS3_CFG_AGENT_STAGING_BYTES
#endif

/* Per-slot RAM buffer for the CLEARING receive slot. Symmetric to
 * MPS3_CFG_AGENT_PARTIAL_RAM_BYTES: split from MPS3_CFG_AGENT_STAGING_BYTES so a
 * memory-tight target (128/256 KiB LMB) can hold a FULL incoming clearing
 * (regdemo: 46,984 B) in RAM — needed when the QSPI clearing overflow is not
 * operational on the board — WITHOUT also inflating swap_fsm's resident clearing
 * arena. That arena stays sized to MPS3_CFG_AGENT_STAGING_BYTES (small) and
 * fail-safe-skips the cache copy for a clearing too large for it (see swap_fsm.c
 * SWAP_CACHE_CLEARING), so only ONE ~47 KiB copy lands on the LMB, not two.
 * Default = the STAGING size, so the host test build (which never overrides it)
 * is byte-for-byte unchanged: the clearing slot stays MPS3_CFG_AGENT_STAGING_BYTES
 * and the RAM/QSPI routing threshold is identical. */
#ifndef MPS3_CFG_AGENT_CLEARING_RAM_BYTES
#define MPS3_CFG_AGENT_CLEARING_RAM_BYTES MPS3_CFG_AGENT_STAGING_BYTES
#endif

/* MPS3_CFG_AGENT_QSPI_SINK — the QSPI swap-staging seam. A large partial is
 * streamed into a QSPI scratch region through these four hooks instead of
 * RAM; overlay_store.c provides the real backend (the INACTIVE A/B slot as
 * scratch, SST26 page-program + block-protect + a read-back CRC verify,
 * WITHOUT flipping active_slot so the boot default is preserved), and
 * coordinator_init() registers it via config_agent_set_qspi_sink(). Left
 * NULL on builds/tests that do not link overlay_store — large partials are
 * then rejected (CFG_AGENT_ERR_SIZE), keeping this module's zero-SPI /
 * zero-platform_regs.h link independence intact (config_agent.c file header).
 *   begin(total_bytes): prep the scratch (pick inactive slot, unlock+erase).
 *   write(buf,len):     program the next `len` sequential payload bytes.
 *   finish(crc):        read-back CRC-verify the flash copy + re-lock; <0 rejects.
 *   abort():            torn/rejected mid-stream — re-lock, discard the scratch.
 * All return 0 on success / <0 on failure (abort returns void). */
typedef struct mps3_cfg_agent_qspi_sink {
    int  (*begin)(uint32_t total_bytes);
    int  (*write)(const void *buf, uint32_t len);
    int  (*finish)(uint32_t expected_crc);
    void (*abort)(void);
    /* OPTIONAL (NULL => "always ready"). For a stream-direct sink whose begin()
     * is gated on the swap FSM being in a particular state — the ICAP-direct
     * partial sink requires SWAP_AWAIT_PARTIAL (RP decoupled, outgoing clearing
     * already fully streamed to the ICAP) — ready() reports whether begin() would
     * be ACCEPTED right now. When it returns 0, config_agent DEFERS the partial:
     * it parks the receive session with the TCP window held CLOSED (so the host
     * pusher backpressures) and re-checks ready() every poll, instead of tearing
     * the session down. This closes the clearing->partial HANDOFF RACE — a LARGE
     * outgoing clearing keeps the FSM in SWAP_STREAM_CLEARING past the moment the
     * pusher opens the partial connection, so a hard begin() reject used to drop
     * the partial and the swap then timed out in AWAIT_PARTIAL. Observed on
     * silicon 2026-08 as "swap-AWAY from a DAP RM fails" (nanosoc/multicore have
     * the two largest clearings); small-clearing RMs win the race and never hit
     * it. TCP transport only — TFTP has no window backpressure, so begin() there
     * still hard-fails as before. */
    int  (*ready)(void);
} mps3_cfg_agent_qspi_sink_t;

void config_agent_init(void);

/* Register a streaming PARTIAL sink (see MPS3_CFG_AGENT_QSPI_SINK). D13 deleted
 * the QSPI backend that used to be registered here: no production build
 * registers one any more (large partials go ICAP-direct), and the seam is kept
 * for the host tests' recording sinks. A payload staged through it has no RAM
 * bytes (info.in_qspi = 1), which the swap FSM treats as UNSOURCED and fails
 * closed. NULL (the default) rejects a partial too large for RAM with no
 * ICAP-direct sink (CFG_AGENT_ERR_SIZE). Persistent across config_agent_init(). */
void config_agent_set_qspi_sink(const mps3_cfg_agent_qspi_sink_t *sink);

/* Register a streaming CLEARING sink — the symmetric partner of
 * config_agent_set_qspi_sink(). Like it, unregistered in every production build
 * since D13 deleted the QSPI clearing STAGE/CACHE: clearings are RAM-resident
 * (the target sizes MPS3_CFG_AGENT_CLEARING_RAM_BYTES for the largest one), and
 * a clearing larger than the RAM slot is rejected (CFG_AGENT_ERR_SIZE). Kept as
 * a seam for the host tests. Persistent across config_agent_init(). */
void config_agent_set_qspi_clearing_sink(const mps3_cfg_agent_qspi_sink_t *sink);

/* Register the STREAM-DIRECT PARTIAL sink (Path 3, OVER_THE_WIRE_RECONFIG_PLAN
 * §5). Reuses the same begin/write/finish/abort shape as the QSPI sinks, but
 * its backend (coordinator/swap_fsm.c's swap_fsm_icap_direct_sink()) writes
 * each incoming partial word STRAIGHT INTO HWICAP.WF as it arrives on 6910 —
 * never buffering the whole partial in RAM or QSPI. This is what removes the
 * partial size limit for EVERY RM (886 KB–1.65 MB) without needing QSPI
 * first-light: an 886 KiB partial neither fits the 256 KiB LMB nor needs the
 * (not-yet-operational) onboard flash.
 *
 * When registered, config_agent PREFERS this sink over the QSPI partial sink
 * for a partial too large for the RAM buffer (a large clearing still uses the
 * QSPI clearing sink; small payloads still RAM-stage). Left NULL (the default)
 * on the host test build and any build without MPS3_CFG_AGENT_ICAP_DIRECT, so
 * the RAM/QSPI staging paths and every existing test stay byte-for-byte
 * unchanged. Because the sink writes to ICAP as bytes arrive, the swap must be
 * ARMED FIRST (its begin() fails closed unless DFXCTL confirms the RP is
 * already DECOUPLEd + held in reset — see net-protocol.md swap steps 1-7).
 * Persistent across config_agent_init(). */
void config_agent_set_icap_direct_sink(const mps3_cfg_agent_qspi_sink_t *sink);

/* ---- the COMMIT sink (net-protocol v0.13 `commit`, D13) -----------------------
 * While registered, EVERY raw-TCP 6910 push (the pair's clearing, then its
 * partial) goes to this sink instead of the RAM staging slots / the ICAP-direct
 * sink -- a commit re-pushes the pair that is ALREADY running, so nothing of it
 * may reach the ICAP or a swap's staging slot. The overlay store's glue
 * (overlay_store.c) registers it for the duration of one commit.
 *
 *   begin(hdr)   the push's header, AFTER config_agent's own validation (magic,
 *                version, static_id == the running shell's, kind, clearing-then-
 *                partial order). 0 = take this push; <0 = refuse (the session is
 *                torn; the sink has already recorded why).
 *   write_some(buf, len, *used)
 *                THE BACK-PRESSURE: take what fits, report it in *used (0 is
 *                legal: "not now"). config_agent keeps the rest (at most one TCP
 *                chunk, <= 512 B) and pulls NOTHING more off the connection until
 *                it drains, so the host's pusher stalls on the TCP window instead
 *                of the shell buffering a window's worth (no 16 KiB ring). With
 *                window-as-grant (WINDOWED) the window reopens only for bytes the
 *                sink has taken. <0 = fail (torn).
 *   finish(hdr)  every payload byte taken AND the transport CRC matched the
 *                header. 0 = accepted; <0 = refused.
 *   abort(why)   the push died after begin() (why = -1: torn / closed early) or
 *                was rejected before it (why = the CFG_AGENT_ERR_* the header
 *                failed with). Called at most once per push; never after a
 *                successful finish().
 * TCP only: a TFTP push while a commit sink is registered is refused (and
 * reported through abort(CFG_AGENT_ERR_ORDER)): TFTP has no window to hold. */
typedef struct mps3_cfg_agent_commit_sink {
    int  (*begin)(const mps3_bitstream_hdr_t *hdr);
    int  (*write_some)(const void *buf, uint32_t len, uint32_t *used);
    int  (*finish)(const mps3_bitstream_hdr_t *hdr);
    void (*abort)(int why);
} mps3_cfg_agent_commit_sink_t;

/* Register (non-NULL) or drop (NULL) the commit sink. Takes effect at the next
 * push's header; a push already in flight keeps the sink it began with. */
void config_agent_set_commit_sink(const mps3_cfg_agent_commit_sink_t *sink);

/* The running shell's static_id, threaded in by coordinator_init() (kept a
 * setter rather than an #include of coordinator.h so this module stays
 * link-independent of the coordinator — see config_agent.c's file header).
 * Until it is called the module fails every push closed against id 0. */
void config_agent_set_running_static_id(uint32_t static_id);

/* Non-blocking receive-loop step -- services whichever transport(s) are
 * mid-transfer (TFTP/69 and/or raw TCP/6910) against the common/net_if.h
 * seam. Bounded work per call (see coordinator/README.md "Why
 * non-blocking"). */
void config_agent_poll(void);

/* Release any held push session (TCP 6910 / TFTP), discarding partial staging.
 *
 * A push session is only meaningful while a swap is armed -- begin_payload()
 * fails closed otherwise. When a swap ends in failure the session is garbage, and
 * leaving it open is not merely untidy: config_agent serves ONE session, lwIP has
 * no keepalive, and a peer that dies WITHOUT sending FIN (a killed client, a
 * yanked cable) leaves the connection ESTABLISHED forever. Every subsequent
 * client is then refused, and the only recovery is a JTAG bitstream reload -- on
 * a platform whose premise is that JTAG is a crutch. Observed on silicon
 * 2026-07-09; the FSM's AWAIT_* idle timeout (swap_fsm.h) aborts the swap but
 * cannot, by itself, free the socket. This does. */
void config_agent_abort_session(void);

/* Validate a received header BEFORE touching HWICAP (net-protocol.md is
 * explicit about this ordering). `running_static_id` is g_shell_state's
 * value, threaded in rather than included directly to keep this module
 * independent of coordinator.h (and therefore independent of
 * platform_regs.h -- see config_agent.c's file header). Pure function, no
 * I/O: see firmware/test/test_config_agent.c. */
config_agent_status_t config_agent_validate_header(const mps3_bitstream_hdr_t *hdr,
                                                     uint32_t running_static_id);

/* Same checks as config_agent_validate_header(), but with the I2 ordering
 * flag (normally this module's own private `s_ordering_seen_clearing`)
 * passed in explicitly instead of read from module-private state.
 * config_agent_validate_header() is a one-line wrapper over this with the
 * real flag threaded in; this `_ex` form is what makes the ordering
 * DECISION itself a pure function callable with both
 * ordering_seen_clearing=0/1 directly -- see
 * firmware/test/test_config_agent.c. */
config_agent_status_t config_agent_validate_header_ex(const mps3_bitstream_hdr_t *hdr,
                                                        uint32_t running_static_id,
                                                        int ordering_seen_clearing);

/* Once a header is header-valid AND its full payload has been accumulated,
 * this checks the payload's actual length + crc32 against the header
 * (I12/I13: len_words*4 bytes, zlib/IEEE crc32). Pure function operating on
 * an in-memory buffer; the receive pipeline computes the same CRC
 * incrementally as bytes arrive (mps3_crc32_update) and this remains the
 * one-shot equivalent for tests/other callers. Returns CFG_AGENT_OK,
 * CFG_AGENT_ERR_SIZE (a length mismatch), or CFG_AGENT_ERR_CRC. */
config_agent_status_t config_agent_check_payload_crc(const mps3_bitstream_hdr_t *hdr,
                                                       const void *payload,
                                                       uint32_t payload_len);

/* What swap_fsm needs out of a validated, fully-received bitstream: enough
 * to stream it (len_words + data) and enough to identify/cache it
 * (rm_id/static_id/crc32). I14: the wire header carries rm_id numerically,
 * so firmware compares by id, never by name (fpga/dfx/rm_list.tcl is the
 * name<->id table's owner, host-side only).
 *
 * `data` points INTO the module's staging buffer for that kind: valid
 * until the next push of the same kind begins. swap_fsm copies the
 * clearing into its own arena at take-time and streams the partial before
 * any new push can plausibly arrive mid-swap (single-client push model);
 * the fakes in firmware/test leave it NULL, which callers must tolerate
 * (counters-only streaming). */
typedef struct {
    uint32_t rm_id;
    uint32_t static_id;
    uint32_t len_words;
    uint32_t crc32;
    const uint8_t *data;
    int      in_qspi;   /* 1 => staged through a registered streaming sink:
                         * `data` is NULL and this module holds no bytes (no
                         * production build registers one since D13; the swap
                         * FSM fails such a payload closed). 0 => RAM-staged (or
                         * fake), use `data`. */
    int      in_icap;   /* 1 => a large partial that was streamed STRAIGHT to
                         * HWICAP as it arrived (Path 3, stream-direct sink):
                         * `data` is NULL and the whole payload is ALREADY in the
                         * ICAP by the time this is handed off, so swap_fsm's
                         * STREAM_PARTIAL has nothing left to push. Mutually
                         * exclusive with in_qspi. 0 => RAM/QSPI, as above. */
} config_agent_bitstream_info_t;

/* Called by swap_fsm.c once per poll while waiting for a validated
 * partial. Returns 0 and fills *info_out if a fully-validated partial is
 * staged (consuming the slot), nonzero if still waiting. */
int config_agent_take_validated_partial(config_agent_bitstream_info_t *info_out);

/* I2 model: the symmetric intake point for the INCOMING pair's clearing
 * bitstream (captured/staged by swap_fsm.c's SWAP_AWAIT_INCOMING_CLEARING,
 * NOT streamed to HWICAP this swap -- cached for the *next* swap's
 * SWAP_STREAM_CLEARING via swap_fsm_set_current_clearing()). Same
 * take/consume semantics as config_agent_take_validated_partial(). */
int config_agent_take_validated_clearing(config_agent_bitstream_info_t *info_out);

/* 1 if a full validated {clearing, partial} pair is currently staged
 * (mirrors fakeshell.py ConfigAgentModel.pair_ready — handy for harness
 * assertions and future coordinator preconditions). */
int config_agent_pair_ready(void);

/* Current receive-session progress for the diagnostics mailbox / diag verb
 * (either out-pointer may be NULL): `got` = payload bytes accumulated so far in
 * the in-flight transfer, `expect` = its total declared payload bytes (both 0
 * between transfers). Lets a HW run read exactly where a stalling stream stopped
 * (e.g. got=36864) directly over JTAG. */
void config_agent_rx_progress(uint32_t *got, uint32_t *expect);

/* The config agent's in-flight raw-TCP (6910) push connection, or NULL when no
 * TCP transfer is active. The platform layer passes it to
 * mps3_net_lwip_conn_diag() to surface the 6910 pcb's receive-window / queued
 * bytes in the diag mailbox (opaque here — config_agent never dereferences it). */
struct mps3_net_conn *config_agent_active_tcp_conn(void);

/* Window-as-grant pacing diagnostics (either out-pointer may be NULL):
 * `windows_drained` = free-running count of full CFG_AGENT_ACK_WINDOW_BYTES
 * windows reopened via mps3_net_recved() (one per fully-drained window) — the
 * pacing count a HW run reads over JTAG (diag mailbox +0x30) to confirm the
 * transfer is progressing window-by-window. `grant_send_fails` is retained (0
 * now — window-as-grant sends no application byte) for diag-mailbox / JSON-schema
 * stability. Both 0 in non-windowed builds. */
void config_agent_win_diag(uint32_t *windows_drained, uint32_t *grant_send_fails);

/* Free-running count of config_agent_poll() ticks spent HOLDING a partial whose
 * ICAP-direct sink was not yet ready (FSM not in SWAP_AWAIT_PARTIAL) — the
 * clearing->partial handoff-race backpressure (see the sink struct's ready()
 * note). 0 means the defer path never engaged: a small-clearing swap wins the
 * race outright, so the count climbs only on a swap-AWAY from a large-clearing RM
 * (nanosoc/multicore). A HW run reads it over JTAG to PROVE the fix engaged
 * (non-zero) rather than the race merely not reproducing that run. Never reset
 * per session (reset only at config_agent_init()). */
uint32_t config_agent_icap_defer_polls(void);

/* ---- TOFU first-key claim: the NAMED-FILE sink (net-protocol.md "TOFU") ------
 *
 * A TFTP WRQ on :69 whose filename is EXACTLY MPS3_CFG_TOFU_FILENAME is not a
 * bitstream: it carries no 24-byte MPS3 header and never goes near the staging
 * slots, the sinks above or the ICAP. It is handed, block by block, to the sink
 * the platform's mps3_cfg_named_sink() provider returns for that name.
 *
 * WHY A PROVIDER AND NOT A FILE WRITE HERE: this module is platform-free (no
 * libc file I/O, no lwIP, nothing a MicroBlaze BSP lacks). What "claim" means --
 * where the key lands, whether the board is already claimed, how the write is
 * made atomic -- is the platform's to decide. config_agent only routes.
 *
 * BARE-METAL NEUTRAL BY CONSTRUCTION: config_agent.c carries a WEAK
 * mps3_cfg_named_sink() that returns NULL, and a NULL sink (or a begin() that
 * refuses) answers TFTP ERROR 2 "access violation" -- so an image without a
 * strong provider rejects every claim, and every OTHER filename takes the
 * bitstream path exactly as before. The Linux harness (src/linux_harness/sw/
 * harnessd/tofu_linux.c) supplies the strong provider.
 *
 * Call contract (single-threaded superloop, one transfer at a time -- the same
 * one-session rule as the bitstream path, so a claim and a push never overlap):
 *   begin(name)      once, at the WRQ. 0 = accept (the ACK 0 goes out),
 *                    <0 = refuse (ERROR 2 from :69, no session is opened).
 *   write(buf, len)  per DATA block, in order, len 0..512. <0 = abort (ERROR 3
 *                    "disk full or allocation exceeded"; abort() follows).
 *   finish()         after the final (< 512 B) block's write(). 0 = committed
 *                    (the final ACK goes out), <0 = failed (ERROR 2).
 *   abort()          on any tear (client ERROR, bad block, write failure,
 *                    config_agent_abort_session) after a successful begin()
 *                    that has not finished. Must discard everything written. */
#define MPS3_CFG_TOFU_FILENAME "authorized_keys"

typedef struct mps3_cfg_named_sink {
    int  (*begin)(const char *name);
    int  (*write)(const uint8_t *buf, uint32_t len);
    int  (*finish)(void);
    void (*abort)(void);
} mps3_cfg_named_sink_t;

/* The provider hook. WEAK default in config_agent.c: NULL for every name. */
const mps3_cfg_named_sink_t *mps3_cfg_named_sink(const char *name);

/* ---- v0.14 SLOT-IMAGE pushes (net-protocol.md "Slot images") -----------------
 *
 * A push whose header kind is MPS3_BIN_KIND_SLOT_IMAGE carries a stage0 S0LB boot
 * image for the user microSD's INACTIVE boot slot, not a bitstream. It shares the
 * transports (6910 and TFTP), the framing, the running-static_id check and the
 * transport CRC with every other push, and NOTHING else: no staging slot, no
 * clearing/partial ordering flag, no ICAP. The bytes stream to the sink the
 * ENGINE's provider returns:
 *
 *   const mps3_cfg_agent_qspi_sink_t *mps3_cfg_slot_sink(const mps3_bitstream_hdr_t *hdr)
 *     called once per push, with the unpacked header, BEFORE any payload byte is
 *     consumed. It decides (target slot, card present, image fits, static_id vs
 *     the fabric, no card job running), records its own refusal reason for the
 *     `slot` verb, and returns NULL to refuse or a sink to accept. config_agent
 *     then calls begin(payload bytes), write() per chunk, finish(crc) after its
 *     own transport-CRC check, or abort() on any tear -- the same four-call
 *     contract as the QSPI sinks above.
 *
 * WHY A PROVIDER: what "the inactive slot" is, and how a write reaches a card
 * without ever blocking the loop, is the engine's; config_agent only routes.
 *
 * BARE-METAL NEUTRAL BY CONSTRUCTION: config_agent.c's WEAK default returns NULL,
 * so an image without a strong provider refuses a slot push exactly as it refused
 * the unknown kind 2 before (TCP: close without staging; TFTP: ERROR "rejected").
 * mps3-harnessd supplies the strong one (src/linux_harness/sw/harnessd/
 * slot_linux.c). The static_id check below is config_agent's own belt: even a
 * provider that forgot its check never receives a byte of an image pushed for
 * another fabric. */
#define MPS3_SLOT_IMAGE_MAX_BYTES (64u * 1024u * 1024u)   /* == stage0 S0_IMAGE_MAX */
const mps3_cfg_agent_qspi_sink_t *mps3_cfg_slot_sink(const mps3_bitstream_hdr_t *hdr);

/* THE LOCK (v0.14, David 2026-09-24; net-protocol.md "Slot images"): asked FIRST,
 * before the provider sees the header, with the push's peer -- the 6910
 * connection's remote end (mps3_net_conn_peer(), `known` 0 when the backend
 * cannot tell) or the TFTP client. Nonzero = refuse this peer: 6910 closes as
 * for any refusal, TFTP answers ERROR 2 "access violation"
 * (CFG_AGENT_ERR_ACCESS). Refused this early, a peer that is not allowed to push
 * changes no state at all. WEAK default 0 (bare metal: the provider declines
 * anyway); mps3-harnessd refuses a non-local peer once its SSH is claimed. */
int mps3_cfg_slot_refuse_peer(const mps3_net_addr_t *peer, int known);

#ifdef __cplusplus
}
#endif

#endif /* MPS3_CONFIG_AGENT_H */
