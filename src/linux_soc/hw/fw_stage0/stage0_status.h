/*
 * stage0_status.h -- the stage0 STATUS BLOCK: 256 bytes at LMB 0x1FE00-0x1FEFF.
 * Normative description: docs/planning/linux_lanes/STAGE0_CONTRACT.md §3.
 *
 * What it is for. Stage0 runs before anything that can log over the network,
 * and on a board with no console attached it is otherwise invisible. This block
 * records what stage0 decided and why (DDR calibration, uSD, slot A/B verdicts,
 * the rescue server's progress) where three readers can get at it:
 *   - JTAG (mdm_riscv) at any time, including while Linux runs -- pyverify's
 *     mailbox reader (HOST lane) and scripts/mps3_diag.tcl;
 *   - harnessd under Linux, through the LMB-tail UIO page (HARNESSD lane);
 *   - the rescue server itself: a TFTP read of "stage0.status" returns these
 *     256 bytes raw (stage0_push.py identifies stage0 with it).
 *
 * It also carries two things that are NOT diagnostics:
 *   - THE FABRIC IDENTITY (plan §10a S1). fabric_static_id / fabric_ver32 are
 *     compile-time constants of this stage0 build (-DMPS3_STATIC_ID /
 *     -DMPS3_VER32, set by FLOW's mint-stage0 bake), so updatemem binds them to
 *     the bitstream exactly like the bare-metal ELF. harnessd reports
 *     fabric_static_id as ping.shell_id and refuses swaps on a mismatch or when
 *     this block is not valid.
 *   - TRY-ONCE-THEN-CONFIRM (S5). att_from / att_confirm / fails_a / fails_b
 *     are NOINIT: they survive a watchdog warm restart (BRAM is only
 *     re-initialised by reconfiguration) and drive the slot fallback. Linux
 *     writes S0_CONFIRM_MAGIC to att_confirm once healthy -- the ONLY word any
 *     agent other than stage0 may write here.
 *
 * LMB MAP (plan §3; SHELL_CONTRACT §3):
 *   0x00000-0x1FDFF  stage0 image + stack   (stage0.ld enforces the top)
 *   0x1FE00-0x1FEFF  THIS block             (stage0 writes; Linux writes
 *                                            ONLY att_confirm)
 *   0x1FF00-0x1FFFF  diag mailbox v8        (HARNESSD; stage0 NEVER touches it)
 *
 * PERSISTENCE. crt0 zeroes .bss only, and this block is not in .bss (it is an
 * absolute address past the linker's LMB region), so a warm restart finds it
 * intact. stage0 re-initialises the whole block only when magic / version /
 * size / magic_end do not all match -- i.e. after a reconfiguration (BRAM
 * outside the baked image comes up zero) or on garbage. A power cycle or MCC
 * REBOOT therefore clears the attempt counters: that is by design (§ contract).
 *
 * THE BOARD IDENTITY (lane IDENT, 2026-09-28). ip_addr / mac_lo / mac_hi /
 * label_lo / label_hi are this board's BAKED identity: the per-board S0_IP /
 * MPS3_MAC0..5 / S0_LABEL a bake was built with (stage0 Makefile: S0_LABEL=,
 * S0_IP= / S0_MAC= or S0_BOARD=<name>, or -DS0_IP/-DMPS3_MACn in S0_EXTRA_DEFS).
 * stage0 writes all five at EVERY entry, right after the block is opened and
 * before any DDR or uSD work (s0_status_publish_identity() below), so Linux
 * reads them whether stage0 booted a slot or served a rescue push. They are the
 * middle source of the Linux identity resolver (mps3-identity: the /persist
 * override, then this block, then the image default). A consumer treats 0 as
 * ABSENT: a stage0 built before this wrote ip/mac only in rescue and never wrote
 * the label words (reserved, read 0). So there is no version bump (harnessd
 * accepts version >= S0_STATUS_VERSION; a bump would make a new harnessd refuse
 * an older block).
 *
 * LAYOUT RULES. Little-endian u32 fields at FIXED offsets, append-only, total
 * size fixed at 256 -- every word is now named (0xE0-0xF8 by lane S0-COLDFIX,
 * 2026-09-28; 0xEC/0xF0 are placeholders, reads 0, for lane IDENT's label).
 * The X-macro below is the one definition: the struct, the
 * offset asserts and the host tests' name table are all generated from it, and
 * test/Makefile checks stage0_status.py's decoder against it (layout gate).
 * Its magic "S0ST" can never be the diag mailbox's 0xD1A6C0DE, so a mailbox
 * scanner cannot mistake one for the other.
 */
#ifndef STAGE0_STATUS_H
#define STAGE0_STATUS_H

#include <stdint.h>

#define S0_STATUS_ADDR     0x0001FE00u
#define S0_STATUS_BYTES    0x100u
#define S0_STATUS_MAGIC    0x54533053u   /* bytes 53 30 53 54 = "S0ST" */
#define S0_STATUS_VERSION  1u

/* Linux (harnessd) writes this to att_confirm once it is healthy. stage0
 * zeroes att_confirm immediately before every hand-off, so a value seen at the
 * next stage0 entry can only have been written by the boot it handed off to. */
#define S0_CONFIRM_MAGIC   0x4B4F3053u   /* bytes 53 30 4F 4B = "S0OK" */

/* NOINIT = kept across runs (a watchdog warm restart) and zeroed only when the
 * block is (re)initialised; every other field is rewritten by each run. */
/*      name               off    meaning */
#define S0_STATUS_FIELDS(X) \
    X(magic,             0x00, "S0_STATUS_MAGIC") \
    X(version,           0x04, "S0_STATUS_VERSION") \
    X(size,              0x08, "S0_STATUS_BYTES") \
    X(build_id,          0x0C, "stage0 git sha, first 8 hex digits (0 = unknown)") \
    X(fabric_static_id,  0x10, "compile-time MPS3_STATIC_ID of the fabric this stage0 is baked into (0 = unprovisioned)") \
    X(fabric_ver32,      0x14, "compile-time MPS3_VER32 of that fabric") \
    X(boot_count,        0x18, "NOINIT: stage0 entries since the block was initialised") \
    X(phase,             0x1C, "S0_PH_*: what stage0 is doing now") \
    X(booted_from,       0x20, "S0_FROM_*: the source of THIS run's hand-off") \
    X(last_error,        0x24, "(S0_ES_* << 16) | code of the last failure this run") \
    X(ddr_calib,         0x28, "S0_DDR_*") \
    X(sd_result,         0x2C, "S0_SD_*: uSD init outcome") \
    X(sd_detail,         0x30, "(usd_state << 16) | usd_error_code") \
    X(slot_a_rc,         0x34, "S0_* s0_load() result for slot A") \
    X(slot_b_rc,         0x38, "S0_* s0_load() result for slot B") \
    X(default_slot,      0x3C, "S0_FROM_A/B from the card's boot-select sector (A when none)") \
    X(cfg_seq,           0x40, "NOINIT: seq of the boot-select sector last seen (0 = none)") \
    X(att_from,          0x44, "NOINIT: S0_FROM_* of the last hand-off (the attempt)") \
    X(att_confirm,       0x48, "NOINIT, WRITTEN BY LINUX: S0_CONFIRM_MAGIC once healthy") \
    X(fails_a,           0x4C, "NOINIT: unconfirmed hand-offs from slot A in a row") \
    X(fails_b,           0x50, "NOINIT: unconfirmed hand-offs from slot B in a row") \
    X(boot_limit,        0x54, "N: a slot with N unconfirmed attempts is skipped") \
    X(last_verdict,      0x58, "S0_VD_*: what this run concluded about the previous attempt") \
    X(rescue_reason,     0x5C, "S0_RR_*: why stage0 is in rescue (identify `reason`)") \
    X(rescue_state,      0x60, "S0_RS_*") \
    X(rescue_bytes,      0x64, "bytes received in the current/last push") \
    X(rescue_sessions,   0x68, "NOINIT: TFTP write sessions started") \
    X(rescue_rejects,    0x6C, "NOINIT: pushed images refused (verification or size)") \
    X(rescue_last_rc,    0x70, "S0_* result of the last push") \
    X(n_boot_a,          0x74, "NOINIT: hand-offs from slot A") \
    X(n_boot_b,          0x78, "NOINIT: hand-offs from slot B") \
    X(n_boot_rescue,     0x7C, "NOINIT: hand-offs from a rescue push") \
    X(n_fallback,        0x80, "NOINIT: hand-offs from the non-default slot after the default failed") \
    X(entry_pc,          0x84, "hand-off pc") \
    X(entry_a0,          0x88, "hand-off a0") \
    X(entry_a1,          0x8C, "hand-off a1") \
    X(image_hdr_crc,     0x90, "table CRC (header_crc32) of the image handed off: its identity") \
    X(handoff_ms,        0x94, "ms from stage0 entry to hand-off") \
    X(heartbeat,         0x98, "increments while stage0 polls") \
    X(uptime_ms,         0x9C, "ms since stage0 entry, refreshed while polling") \
    X(reset_cause,       0xA0, "WDOG TWCSR0 at entry (bit3 WRS = watchdog reset)") \
    X(trap_mcause,       0xA4, "NOINIT: last stage0 trap mcause (0 = none)") \
    X(trap_mepc,         0xA8, "NOINIT: last stage0 trap mepc") \
    X(trap_mtval,        0xAC, "NOINIT: last stage0 trap mtval") \
    X(ip_addr,           0xB0, "the board's baked IPv4 address (S0_IP), host order: written at EVERY entry, before DDR/SD (IDENTITY below); the rescue server's address") \
    X(mac_lo,            0xB4, "the board's baked MAC (MPS3_MAC0..5) bytes 0..3 (byte 0 in [7:0]): every entry, as ip_addr") \
    X(mac_hi,            0xB8, "MAC bytes 4..5 in [15:0] ([31:16] = 0): every entry, as ip_addr") \
    X(net_rc,            0xBC, "smsc911x_init() result (0 ok, <0 SMSC911X_ERR_*)") \
    X(tftp_errors,       0xC0, "NOINIT: TFTP ERROR packets sent") \
    X(rx_frames,         0xC4, "NOINIT: Ethernet frames received in rescue") \
    X(tx_frames,         0xC8, "NOINIT: Ethernet frames sent in rescue") \
    X(pings,             0xCC, "NOINIT: ICMP echo requests answered") \
    X(identifies,        0xD0, "NOINIT: identify (UDP 6899) replies sent") \
    X(verdict_from,      0xD4, "S0_FROM_* of the attempt last_verdict judged") \
    X(sd_rd_fails,       0xD8, "uSD read ops that failed this run, retries included (0 = all first time); LIVE: refreshed on every poll") \
    X(sd_rd_last,        0xDC, "last failed read op: [31:24] -rc (5 EIO, 110 ETIMEDOUT, 19 ENODEV, 250 refused, 251 guard), [23:16] usd_state, [15:0] usd_error_code; LIVE") \
    X(subphase,          0xE0, "S0_SUBPHASE(S0_SP_*, detail): [31:24] what stage0 is doing inside `phase`, [23:0] detail (slot load: block N of the slot; crc: region; settle: its ms)") \
    X(entry,             0xE4, "[7:0] S0_EK_* this entry; [15:8] NOINIT consecutive pre-hand-off watchdog restarts; [23:16] calib drops seen this run (sat); [27:24] DDR recoveries this run (sat); [28] calib already 1 when the cold settle began; [29] calib rose during the settle; [31] NOINIT cold settle still pending") \
    X(prev_phase,        0xE8, "S0_PREV(): the PREVIOUS run as it was reset, copied at entry (0 after a reconfiguration): [7:0] its phase, [15:8] its S0_SP_* subphase, [31:16] that subphase's detail (sat 0xFFFF)") \
    X(label_lo,          0xEC, "the board's baked label (S0_LABEL) bytes 0..3, ASCII, byte 0 in [7:0]: every entry, as ip_addr (IDENTITY below)") \
    X(label_hi,          0xF0, "label bytes 4..7, NUL-padded (a label of <= 4 chars leaves this 0)") \
    X(prev_uptime_ms,    0xF4, "the previous run's uptime_ms (its last poll)") \
    X(ddr_ok_ms,         0xF8, "ms from entry to the DDR gate passing (calib held S0_CALIB_HOLD_MS; 0 = not passed)")

struct s0_status {
#define S0_X_FIELD(name, off, doc) uint32_t name;
    S0_STATUS_FIELDS(S0_X_FIELD)
#undef S0_X_FIELD
    uint32_t magic_end;     /* 0xFC: S0_STATUS_MAGIC again (torn-read guard) */
};

/* The offsets are the contract; the compiler proves the struct matches. */
#define S0_X_ASSERT(name, off, doc) \
    _Static_assert(__builtin_offsetof(struct s0_status, name) == (off), \
                   "stage0 status field " #name " moved");
S0_STATUS_FIELDS(S0_X_ASSERT)
#undef S0_X_ASSERT
_Static_assert(__builtin_offsetof(struct s0_status, magic_end) == 0xFCu, "magic_end moved");
_Static_assert(sizeof(struct s0_status) == S0_STATUS_BYTES, "status block must be 256 B");

/* The board identity (header "THE BOARD IDENTITY"). One writer, the same five
 * stores at every entry; the host tests call it exactly as stage0.c does. The
 * label is 8 ASCII bytes NUL-padded, byte 0 in label_lo[7:0]
 * (S0_LABEL_WORD('M','P','S','3') is "MPS3"). */
#define S0_LABEL_MAX 8u
#define S0_LABEL_WORD(b0, b1, b2, b3) \
    ((uint32_t)(uint8_t)(b0) | ((uint32_t)(uint8_t)(b1) << 8) | \
     ((uint32_t)(uint8_t)(b2) << 16) | ((uint32_t)(uint8_t)(b3) << 24))

static inline void s0_status_publish_identity(struct s0_status *st, uint32_t ip,
                                              const uint8_t mac[6],
                                              uint32_t label_lo, uint32_t label_hi)
{
    st->ip_addr = ip;
    st->mac_lo = S0_LABEL_WORD(mac[0], mac[1], mac[2], mac[3]);
    st->mac_hi = (uint32_t)mac[4] | ((uint32_t)mac[5] << 8);
    st->label_lo = label_lo;
    st->label_hi = label_hi;
}

/* phase */
enum {
    S0_PH_RESET   = 0,   /* block (re)opened, nothing done yet           */
    S0_PH_DDR     = 1,   /* waiting for DDR4 calibration                 */
    S0_PH_SD      = 2,   /* bringing the user uSD up                     */
    S0_PH_SLOT_A  = 3,   /* loading slot A                               */
    S0_PH_SLOT_B  = 4,   /* loading slot B                               */
    S0_PH_RESCUE  = 5,   /* rescue server running                        */
    S0_PH_HANDOFF = 6,   /* jumped to the payload (Linux owns the hart)  */
    S0_PH_TRAP    = 7,   /* stage0 took an unexpected trap and parked    */
};

/* booted_from / att_from / default_slot */
enum {
    S0_FROM_NONE   = 0,
    S0_FROM_A      = 1,
    S0_FROM_B      = 2,
    S0_FROM_RESCUE = 3,
};

/* last_verdict: stage0's judgement of the previous hand-off, at this entry */
enum {
    S0_VD_NONE        = 0,  /* cold block, or the previous run never handed off */
    S0_VD_CONFIRMED   = 1,  /* Linux wrote S0_CONFIRM_MAGIC: the attempt is good */
    S0_VD_UNCONFIRMED = 2,  /* warm restart with the attempt still pending: a failure */
};

/* ddr_calib */
enum {
    S0_DDR_UNKNOWN = 0,  /* not checked yet                               */
    S0_DDR_OK      = 1,  /* the calib bit read 1 for S0_CALIB_HOLD_MS     */
    S0_DDR_FAIL    = 2,  /* it never held 1 within the timeout            */
    S0_DDR_IMPLIED = 3,  /* build without a calib bit (CALIB=none): trusted */
    S0_DDR_LOST    = 4,  /* it held, then dropped during a slot load and
                            did not come back (rescue polls for it)       */
};

/* sd_result */
enum {
    S0_SD_NOTRIED  = 0,
    S0_SD_READY    = 1,
    S0_SD_NOCARD   = 2,  /* slot empty: no delay, no error (D13 rule 1)    */
    S0_SD_NOHW     = 3,  /* no usd_spi block in this fabric (ID mismatch)  */
    S0_SD_UNSUP    = 4,  /* SDSC / v1 / not CSD v2                         */
    S0_SD_ERROR    = 5,  /* init failed (usd_error_code in sd_detail)      */
    S0_SD_TIMEOUT  = 6,  /* init did not settle within S0_SD_INIT_TIMEOUT_MS */
    S0_SD_NOMBR    = 7,  /* card READY but sector 0 is not an MBR         */
    S0_SD_SKIPPED  = 8,  /* not attempted (DDR not calibrated)            */
};

/* rescue_reason (also the identify `reason` text, s0_rescue_reason_text()) */
enum {
    S0_RR_NONE      = 0,
    S0_RR_DDR       = 1,  /* "ddr calib fail"                              */
    S0_RR_NOCARD    = 2,  /* "no card"                                     */
    S0_RR_NOHW      = 3,  /* "no usd_spi block"                            */
    S0_RR_UNSUP     = 4,  /* "card unsupported"                            */
    S0_RR_SDERR     = 5,  /* "card error"                                  */
    S0_RR_NOLAYOUT  = 6,  /* "card has no stage0 slots" (blank / foreign)  */
    S0_RR_BADSLOTS  = 7,  /* "no valid slot"                               */
    S0_RR_EXHAUSTED = 8,  /* "slots exhausted" (N unconfirmed boots each)  */
    S0_RR_WDOG      = 9,  /* "stage0 watchdog loop": S0_WDOG_LOOP_LIMIT
                             watchdog restarts in a row BEFORE a hand-off   */
};

/* subphase (2026-09-28, lane S0-COLDFIX): WHERE inside `phase` stage0 is, so a
 * silent hang (a stalled DDR transaction freezes the hart) is located from the
 * block alone -- live over JTAG, or at the next entry in prev_phase. */
enum {
    S0_SP_NONE      = 0,
    S0_SP_SETTLE    = 1,   /* cold-entry settle (detail: its length, ms)       */
    S0_SP_DDR_WAIT  = 2,   /* waiting for the calib bit to hold                 */
    S0_SP_CARD_INIT = 3,   /* bringing the user uSD up                          */
    S0_SP_CARD_META = 4,   /* MBR + boot-select sectors                         */
    S0_SP_SLOT_LOAD = 5,   /* reading the slot (detail: block N of the slot)    */
    S0_SP_CRC       = 6,   /* CRC of a region in DDR (detail: region index)     */
    S0_SP_HANDOFF   = 7,   /* writeback, usd_spi release, WDOG re-arm, jump     */
    S0_SP_RESCUE    = 8,   /* rescue server                                     */
};
#define S0_SUBPHASE(sp, detail) (((uint32_t)(sp) << 24) | ((uint32_t)(detail) & 0xFFFFFFu))

/* prev_phase packs the previous run's phase and subphase into one word (the
 * block is full: 0xEC/0xF0 are lane IDENT's): [7:0] phase, [15:8] subphase
 * code, [31:16] subphase detail saturated at 0xFFFF (a slot-load block index
 * up to 32 MiB into the slot). */
#define S0_PREV(phase, subphase) \
    (((uint32_t)(phase) & 0xFFu) | (((uint32_t)(subphase) >> 24) << 8) | \
     (((subphase) & 0xFFFFFFu) > 0xFFFFu ? 0xFFFF0000u : (((uint32_t)(subphase) & 0xFFFFu) << 16)))
#define S0_PREV_PHASE(w)   ((w) & 0xFFu)
#define S0_PREV_SP(w)      (((w) >> 8) & 0xFFu)
#define S0_PREV_DETAIL(w)  ((w) >> 16)

/* entry: the S0_EK_* kind, and the fields packed with it */
enum {
    S0_EK_NONE       = 0,
    S0_EK_COLD       = 1,  /* the block was (re)initialised: an FPGA configuration
                              (MCC power-up, MCC REBOOT, fpga -file) -> settle     */
    S0_EK_WARM       = 2,  /* a valid block and no watchdog: PB0, a reboot verb    */
    S0_EK_WDOG       = 3,  /* the watchdog, while stage0 still owned the hart (the
                              previous run never handed off): counted              */
    S0_EK_WDOG_LINUX = 4,  /* the watchdog after a hand-off: Linux's failure, judged
                              by try-once-then-confirm, not counted here           */
    S0_EK_RESETTLE   = 5,  /* not the watchdog, but the previous run was reset
                              before its cold settle finished (e.g. an MMCM lock
                              loss in the MCC window): settles again               */
};
#define S0_ENTRY_KIND(e)          ((e) & 0xFFu)
#define S0_ENTRY_WDOG_RUN(e)      (((e) >> 8) & 0xFFu)
#define S0_ENTRY_CALIB_DROPS(e)   (((e) >> 16) & 0xFFu)
#define S0_ENTRY_DDR_RECOVER(e)   (((e) >> 24) & 0xFu)
/* What the calib bit did during the cold settle (the MIG's reference clock,
 * OSC6 "GTX clock (DDR)", is programmed by the MCC AFTER configuration, and
 * only an MMCM/PLL lock loss makes the MIG re-calibrate by itself -- stage0
 * cannot reset it: sys_rst is USER_nPB0 only). [28] set with no [29] and no
 * drops = the MIG calibrated BEFORE the MCC set OSC6 and never re-calibrated. */
#define S0_ENTRY_CAL_AT_SETTLE    (1u << 28)
#define S0_ENTRY_CAL_ROSE         (1u << 29)
#define S0_ENTRY_SETTLE_PENDING   (1u << 31)

/* rescue_state */
enum {
    S0_RS_OFF       = 0,
    S0_RS_LISTEN    = 1,  /* waiting for a WRQ on UDP 69                   */
    S0_RS_RECEIVING = 2,
    S0_RS_VERIFYING = 3,
    S0_RS_REJECTED  = 4,  /* last push refused; still listening            */
    S0_RS_ACCEPTED  = 5,  /* verified, final ACK sent, handing off          */
    S0_RS_NONET     = 6,  /* LAN9220 init failed: no rescue possible        */
    S0_RS_NODDR     = 7,  /* listening, but WRQ refused: DDR not calibrated */
};

/* last_error source (high half) */
enum {
    S0_ES_NONE   = 0,
    S0_ES_DDR    = 1,
    S0_ES_SD     = 2,
    S0_ES_SLOT_A = 3,
    S0_ES_SLOT_B = 4,
    S0_ES_RESCUE = 5,
    S0_ES_NET    = 6,
    S0_ES_TRAP   = 7,
    S0_ES_WDOG   = 8,   /* code = the run of pre-hand-off watchdog restarts */
};
/* reset_cause: TWCSR0.WRS, a watchdog reset since WRS was last cleared */
#define S0_RESET_WRS 0x8u

#define S0_LAST_ERROR(src, code) (((uint32_t)(src) << 16) | ((uint32_t)(code) & 0xFFFFu))

#endif /* STAGE0_STATUS_H */
