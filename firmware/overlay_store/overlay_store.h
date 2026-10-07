/*
 * overlay_store.h -- the overlay store on the USER microSD (D13), as the shell
 * firmware's service modules see it. Both engines link the same glue
 * (overlay_store.c) over the same store (ovlstore_sd.c); only the ENGINE
 * PROVIDER below differs:
 *
 *   bare metal   overlay_store_bm.c   ovl_bdev_usd over firmware/usd/usd.c,
 *                                     allow_wipe = true, a .data boot latch
 *   Linux        harnessd's           ovl_bdev_posix on the WHOLE disk
 *                ovlstore_linux.c     /dev/mmcblk0 (OVL_BDEV_POSIX_LINUX_CARD),
 *                                     allow_wipe = false, the boot latch in the
 *                                     LMB-tail diag mailbox word usd_boot
 *
 * THIS IS THE USER CARD, never the MCC config card (V2M_MPS3 / sd_install).
 * The SST26/AXI-Quad-SPI backend is GONE (D13 L3a): nothing in this module, or
 * anywhere in the shell firmware, issues a flash opcode at 0x44A4 -- that page
 * is usd_spi now. Wire contract: docs/contracts/net-protocol.md "User microSD
 * (`usd`) and `commit` -- v0.13". Design: docs/planning/
 * HANDOVER_USD_OVERLAY_STORE.md; wiring notes: INTEGRATION_L3.md.
 *
 * WHAT THE GLUE OWNS
 *   - the one store instance and the engine binding (overlay_store_init);
 *   - the "usd" service row (overlay_store_service): the device + store poll,
 *     then one step of the power-on load hook;
 *   - the power-on load hook, ONCE PER FPGA CONFIGURATION (THE BOOT LATCH, below);
 *   - the swap source "usd" the swap FSM streams from;
 *   - the v0.13 `commit` (a config_agent commit sink with back-pressure);
 *   - the `usd` verb's status and actions;
 *   - the two CLCD exports and the diag word.
 *
 * ERRORS ON THE WIRE ARE NAMES (overlay_store_err_name), never errno numbers:
 * ETIMEDOUT is 110 in glibc and 116 in newlib.
 */
#ifndef MPS3_OVERLAY_STORE_H
#define MPS3_OVERLAY_STORE_H

#include <stdbool.h>
#include <stdint.h>

#include "../common/net_proto.h"
#include "ovlstore_codec.h"
#include "ovlstore_sd.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ---- the greybox clearing ---------------------------------------------------
 * The boot SEED of swap_fsm's "current clearing" (overlay-manifest.md: "the
 * coordinator's 'currently-loaded clearing' starts as the greybox's, also shipped
 * in the shell"). Engine-provided: bare metal returns the blob baked into the
 * image (firmware/platform/generated/greybox_blob.c, overlay_store_bm.c); Linux
 * loads /etc/mps3/greybox_clear.bin and offers it only while the fabric identity
 * is proven (ovlstore_linux.c). Returns 0 and fills *out, or nonzero. */
typedef struct {
    uint32_t static_id;
    uint32_t rm_id;
    uint32_t clear_len_words;
    uint32_t clear_crc32;
    const void *clear_data;   /* RAM-resident payload (NULL in bookkeeping-only fakes) */
} overlay_manifest_info_t;

int overlay_store_get_greybox_clearing(overlay_manifest_info_t *out);

/* ---- lifecycle ---------------------------------------------------------------- */

/* coordinator_init() calls this once. Binds the engine's block device and the
 * store; does NO device I/O (the first overlay_store_service() looks). */
void overlay_store_init(void);

/* THE "usd" SERVICE ROW (bare metal main.c row 12, harnessd row 12). One call:
 *   1. ovlstore_sd_poll() -- which, bare metal, runs usd_poll() first (the store
 *      owns the driver poll, ovl_bdev_usd_bind(own_poll = true));
 *   2. the commit idle timeout;
 *   3. one step of the power-on load hook.
 * Bounded like every service: one device op started, at most
 * OVLSTORE_SD_CRC_BYTES_PER_POLL bytes CRC'd, no loop waits on the card. */
void overlay_store_service(void);

/* 1 while the store has work of its OWN to drive (probe / verify / stream /
 * format / clear, and a commit's flush + read-back -- not a commit still waiting
 * for the host's bytes). harnessd's idle policy yields instead of sleeping while
 * it is set. */
bool overlay_store_busy(void);

/* ---- CLCD row 4 (lane I-CLCD) -------------------------------------------------
 * STRONG definitions of the two symbols clcd.c carries weak fallbacks for.
 * text: <= 16 printable ASCII chars ("none", "no hw", "init", "unsupported",
 *   "ERR <n>", "foreign", "empty", "<rm> [A]" (name clipped to 12), "stale key",
 *   "bad", "skipped"); valid until the next call.
 * change_count: a plain O(1) getter (no MMIO, no card I/O); bumps whenever the
 *   text may have changed. clcd_poll() reads it ~1400 times a second. */
const char *overlay_store_usd_text(void);
uint32_t    overlay_store_usd_change_count(void);

/* ---- the `usd` verb ------------------------------------------------------------- */

/* Status, in the verb's shape (net_proto.h mps3_usd_t). Never held. */
void overlay_store_usd_status(mps3_usd_t *out);

/* An action: "format" (with confirm "erase" | "erase-all"), "clear", "rescan".
 * Returns
 *   OVLSD_BUSY   a store job was started: PARK the connection and ask
 *                overlay_store_usd_action_poll() each pass;
 *   OVLSD_OK     done already (rescan);
 *   < 0          refused (overlay_store_err_name()). An unknown action is
 *                OVLSD_EARG ("bad args").
 * Refused with OVLSD_EBUSY ("store busy") while a swap, a commit, another action
 * or the power-on load is in progress. */
int overlay_store_usd_action(const char *action, const char *confirm);

/* The parked action: OVLSD_BUSY, OVLSD_OK, or < 0. */
int overlay_store_usd_action_poll(void);
/* 1 while a format / clear started by overlay_store_usd_action() is running. */
bool overlay_store_action_active(void);

/* ---- `commit` (v0.13, D1 = re-push) --------------------------------------------
 * begin: the desc is the request's (rm_id, static_id, lengths, CRCs). Refused
 *   unless desc.static_id == the shell's, desc.rm_id == the LIVE DFXCTL.RM_ID
 *   (live_rm_id; pass a value that cannot match when RM_STATUS.rm_id_valid is
 *   clear), and the store is EMPTY / VALID / BAD / STALE. On OVLSD_OK the commit
 *   sink is registered with config_agent: the next two 6910 pushes (clearing,
 *   then partial) go to the card's INACTIVE slot, header-checked against the
 *   desc before any byte is fed, and back-pressured through the store's two
 *   buffers (no ring).
 * poll: OVLSD_BUSY until done; OVLSD_OK with *slot = 'A' / 'B'; or < 0 (the old
 *   default survives every failure). An idle feed (no byte for
 *   MPS3_SWAP_AWAIT_IDLE_MS) fails OVLSTORE_ETIMEOUT, like a swap's AWAIT_*. */
int  overlay_store_commit_begin(const ovlstore_sd_desc_t *desc, uint32_t live_static_id,
                                uint32_t live_rm_id);
int  overlay_store_commit_poll(char *slot);
bool overlay_store_commit_active(void);

/* ---- the swap source "usd" (swap_fsm.c) ------------------------------------------
 * default: the VERIFIED default's descriptor (state VALID, not skipped).
 * begin / next / abort: ovlstore_sd_stream_* on the one store: each region is
 * re-read and re-CRC'd while it streams, and its LAST buffer is withheld unless
 * the CRC matches, so a card that went bad since the verify never completes the
 * bitstream. next(): OVLSD_OK (*p, *n: n <= max_bytes, a multiple of 4 when
 * max_bytes is), OVLSD_BUSY (nothing this poll), OVLSD_DONE, or < 0. The pointer
 * is valid until the next store call. */
int  overlay_store_src_default(ovlstore_sd_desc_t *out);
int  overlay_store_src_begin(ovlstore_sd_which_t which);
int  overlay_store_src_next(uint32_t max_bytes, const uint8_t **p, uint32_t *n);
void overlay_store_src_abort(void);

/* ---- error names ----------------------------------------------------------------
 * The contract's table (net-protocol.md "Error names"): no card, no hw, foreign,
 * stale key, unavailable, filesystem present, exists, partition too small,
 * confirm required, wipe disabled, rm mismatch, crc, store busy, io, timeout,
 * bad args. Every OVLSD_* code maps onto one of them; OVLSTORE_ETIMEOUT is the
 * glue's own (a commit that went idle). */
#define OVLSTORE_ETIMEOUT (-100)
/* static inline so the coordinator (and the test doubles that stand in for this
 * module) share the ONE table without a link dependency on the glue. */
static inline const char *overlay_store_err_name(int rc)
{
    switch (rc) {
    case OVLSD_ENOCARD:
    case OVLSD_EGONE:       return "no card";
    case OVLSD_ENOHW:       return "no hw";
    case OVLSD_ENOTREADY:
    case OVLSD_ENODEFAULT:
    case OVLSD_ESKIPPED:    return "unavailable";
    case OVLSD_EFOREIGN:    return "foreign";
    case OVLSD_ESTALE:
    case OVLSD_ESTATIC:     return "stale key";
    case OVLSD_ERMID:       return "rm mismatch";
    case OVLSD_EBUSY:       return "store busy";
    case OVLSD_EARG:
    case OVLSD_EORDER:
    case OVLSD_ETOOBIG:     return "bad args";
    case OVLSD_ELEN:
    case OVLSD_ECRC:
    case OVLSD_EVERIFY:     return "crc";
    case OVLSD_ECONFIRM:    return "confirm required";
    case OVLSD_EEXIST:
    case OVLSD_EPART:       return "exists";
    case OVLSD_EUNKNOWN:
    case OVLSD_EFSSIG:      return "filesystem present";
    case OVLSD_ESMALL:      return "partition too small";
    case OVLSD_ENOWIPE:     return "wipe disabled";
    case OVLSTORE_ETIMEOUT: return "timeout";
    case OVLSD_EIO:
    case OVLSD_EABORTED:    /* a torn push */
    default:                return "io";
    }
}

/* ---- THE BOOT LATCH (the power-on load, once per FPGA configuration) -------------
 * The decision is taken ONCE PER FPGA CONFIGURATION and never on a harnessd
 * respawn, an OS reboot or a WDOG reset. The glue keeps the decision in one
 * 32-bit word the ENGINE persists somewhere only a reconfiguration clears:
 *   bare metal  an initialised .data word -- the bitstream's BRAM INIT sets it,
 *               crt0/_crtinit zero .bss/.sbss only and never re-copy .data
 *               (no LMA in lscript.ld.in), so a MicroBlaze (WDOG) reset keeps it;
 *   Linux       the diag mailbox word usd_boot in LMB BRAM at 0x1FFB4 -- stage0
 *               never touches the mailbox, the kernel does not map it, and BRAM
 *               outside stage0's baked image comes up ZERO after reconfiguration.
 * Word layout (also the diag mailbox's usd_boot, diag.h v9):
 *   [31:16] OVL_BOOT_LATCH_MAGIC once decided in this configuration
 *   [15:8]  failure reason (OVL_BOOT_WHY_*; 0x80|swap state; 0x40|-OVLSD rc)
 *   [3:0]   OVL_BOOT_* decision
 * The latch is written PENDING the moment the hook starts deciding, so a crash
 * or reset mid-load never retries: the next start reports "failed:aborted". */
#define OVL_BOOT_LATCH_MAGIC 0xB007u
enum {
    OVL_BOOT_PENDING = 1,   /* "pending": waiting for the card, or loading   */
    OVL_BOOT_LOADED  = 2,   /* "loaded"                                        */
    OVL_BOOT_SKIPPED = 3,   /* "skipped": PB1 held at the boot check           */
    OVL_BOOT_NONE    = 4,   /* "none": no card, no default, stale, foreign ... */
    OVL_BOOT_FAILED  = 5,   /* "failed:<why>"                                  */
};
enum {
    OVL_BOOT_WHY_TIMEOUT  = 0x01,   /* "timeout": the card never became ready   */
    OVL_BOOT_WHY_IDLOCK   = 0x02,   /* "identity lock" (Linux)                 */
    OVL_BOOT_WHY_TOOBIG   = 0x03,   /* "too big": the clearing does not fit RAM */
    OVL_BOOT_WHY_ABORTED  = 0x04,   /* "aborted": a restart interrupted a load  */
    OVL_BOOT_WHY_BUSY     = 0x05,   /* "store busy": the swap FSM was not idle  */
    OVL_BOOT_WHY_SWAP     = 0x80,   /* | the swap state it failed in            */
    OVL_BOOT_WHY_STORE    = 0x40,   /* | (-OVLSD rc & 0x3F)                     */
};
/* After this long with no card state, "no card" may be concluded (the driver
 * starts ABSENT and CD is debounced >= 10 ms after reset). With no card the
 * decision costs nothing: the network is already up and nothing blocks. */
#ifndef OVLSTORE_BOOT_GRACE_MS
#define OVLSTORE_BOOT_GRACE_MS    100u
#endif
/* A card still initialising / being verified after this long: "failed:timeout".
 * SETTLE (250 ms) + init + the full-CRC verify of the largest pair (~8-9 s at one
 * block a pass) fit well inside it. */
#ifndef OVLSTORE_BOOT_TIMEOUT_MS
#define OVLSTORE_BOOT_TIMEOUT_MS  30000u
#endif

/* "loaded" / "skipped" / "none" / "pending" / "failed:<name>". */
const char *overlay_store_boot_text(void);
/* The diag mailbox word usd_boot: the latch word once decided, else 0. */
uint32_t    overlay_store_diag_word(void);

/* ---- ENGINE PROVIDER (one per engine; tests may bring their own) ----------------
 * bind: fill *bd and adjust *cfg (allow_wipe, raw_partition, OP budgets live in
 *   the store's -D tunables). cfg->live_static_id and cfg->rm_name are already
 *   set by the glue. Return 0 when a device is bound; nonzero = no device at all
 *   (the glue then binds a NO_HW stub: state "no_hw", never a write).
 * latch_get / latch_set: the per-configuration boot latch word (above). */
int      ovlstore_engine_bind(ovl_bdev_t *bd, ovlstore_sd_cfg_t *cfg);
uint32_t ovlstore_engine_latch_get(void);
void     ovlstore_engine_latch_set(uint32_t word);

/* ---- phase hook (diag mailbox ovlstore_phase / ovlstore_detail, v6) --------------
 * The glue stamps the store's current job here when it changes. Enum VALUES are
 * mailbox contract and never renumber; since D13 only IDLE, INIT, READ_HDR, CRC
 * and STREAM occur (the SST26 phases are retired, their numbers reserved). WEAK
 * no-op by default; firmware/platform/src/ovlstore_phase.c overrides it on bare
 * metal (Linux omits both keys, HARNESSD_CONTRACT.md §9.3). */
enum {
    OVL_PHASE_IDLE = 0,     /* no store job running */
    OVL_PHASE_INIT,         /* D13: the card is being brought up (device not READY) */
    OVL_PHASE_READ_HDR,     /* D13: probe -- MBR + the two header copies */
    OVL_PHASE_BP_UNLOCK,    /* retired (SST26) */
    OVL_PHASE_ERASE_SECTOR, /* retired (SST26) */
    OVL_PHASE_PROGRAM_PAGE, /* D13: a commit / format / clear is writing the card */
    OVL_PHASE_WAIT_READY,   /* retired (SST26) */
    OVL_PHASE_CRC,          /* D13: verify -- the default's full CRC */
    OVL_PHASE_PROMOTE,      /* retired (SST26) */
    OVL_PHASE_STREAM,       /* D13: a region is streaming to a swap */
    OVL_PHASE_REJECT,       /* retired (SST26) */
};
void mps3_ovlstore_phase(uint32_t phase, uint32_t detail);

#ifdef OVLSTORE_GLUE_TEST_HOOKS
/* Host tests only: the store instance, and a full reset of the glue (as if the
 * image had just been loaded -- the engine latch is NOT touched). */
ovlstore_sd_t *overlay_store_test_store(void);
void           overlay_store_test_reset(void);
#endif

#ifdef __cplusplus
}
#endif

#endif /* MPS3_OVERLAY_STORE_H */
