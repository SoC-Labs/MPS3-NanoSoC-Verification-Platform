/*
 * identity.c — WHICH FABRIC is this? The safety core of harnessd.
 *
 * ON BARE METAL the static_id is baked into the ELF that updatemem puts inside
 * the bitstream, so the id `ping` reports and the fabric it runs on cannot come
 * apart. UNDER LINUX the image (kernel + rootfs + harnessd) lives on a µSD card
 * and is updated independently of the bitstream, so "the card says 0x...." is a
 * CLAIM about some fabric, not a fact about this one. Reporting it as shell_id
 * would let a host push partials built for a different static into this RP.
 *
 * So the identity is FABRIC-BOUND (HARNESSD_CONTRACT.md §9.4):
 *   - shell_id = fabric_static_id from stage0's LMB status block (0x1FE10).
 *     FLOW bakes it into stage0 at mint-stage0 (-DMPS3_STATIC_ID) and updatemem
 *     puts stage0 in the bitstream, exactly like the bare-metal ELF — so it
 *     travels WITH the bitstream.
 *   - It is cross-checked against /etc/mps3/static_id (the image's claim) and the
 *     fabric's release (USR_ACCESS, else stage0's fabric_ver32) against the image
 *     manifest's ver32.
 *   - ANY disagreement, or a status block that is absent/garbage (a JTAG-loaded
 *     image with no stage0), LOCKS the identity: `ping` still answers with the
 *     FABRIC value (0 when unknown — never the card's), `version` reports the
 *     reason (`id_skew`), and swap/commit are refused (`identity lock: ...`).
 *
 * The lock is enforced TWICE:
 *   1. the coordinator seam mps3_swap_refusal() (below) — refuses the verb before
 *      any register is touched. It needs a one-function hook in coordinator.c
 *      (HARNESSD_CONTRACT.md §11, Handoff H1).
 *   2. the belt that needs no firmware change: a locked harnessd never offers a
 *      greybox or resident clearing (ovlstore_linux.c), so g_current_rm_clearing
 *      stays invalid and a swap that got past (1) fails CLOSED at
 *      SWAP_STREAM_CLEARING — before any ICAP word is written, leaving the RP in
 *      the decoupled + reset safe state.
 */
#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "../../../../firmware/common/platform_regs.h"
#include "../../../linux_soc/hw/fw_stage0/stage0_status.h"
#include "hal.h"
#include "harnessd.h"

/* Field offsets come from STAGE0's header by NAME (STAGE0_CONTRACT.md §3), so
 * a layout STAGE0 re-publishes cannot silently move under this file. */
#define S0_OFF(field) ((unsigned)__builtin_offsetof(struct s0_status, field))

static volatile uint32_t *s_s0;       /* the status block, via the LMB-tail window */
static int      s_s0_valid;
static uint32_t s_fabric_sid;
static uint32_t s_fabric_ver32;       /* stage0's baked MPS3_VER32 (0 = not baked) */
static uint32_t s_claim_sid;
static int      s_have_claim;
static char     s_reason[48];         /* "" = consistent; + the 15-char prefix fits err[64] */
static char     s_refusal[64];        /* "identity lock: " + reason (<= 62 chars) */
static int      s_confirmed;

int harnessd_s0_valid(void) { return s_s0_valid; }

/* The live status-block window (NULL before harnessd_identity_init or with no
 * LMB-tail window): the board-identity verbs read stage0's baked identity from
 * it (identity_linux.c). */
const volatile uint32_t *harnessd_s0_block(void) { return s_s0; }

uint32_t harnessd_s0_read(unsigned off)
{
    return (s_s0 && off < S0_STATUS_BYTES) ? s_s0[off / 4u] : 0u;
}

uint32_t harnessd_fabric_static_id(void) { return s_fabric_sid; }
int harnessd_identity_locked(void)       { return s_reason[0] != '\0'; }
const char *harnessd_identity_reason(void) { return s_reason[0] ? s_reason : 0; }

void harnessd_identity_init(void)
{
    s_s0 = hal_backend_window(HARNESSD_S0_STATUS_ADDR, S0_STATUS_BYTES);
    s_s0_valid = 0;
    s_fabric_sid = 0u;
    s_fabric_ver32 = 0u;
    s_reason[0] = '\0';
    s_refusal[0] = '\0';

    if (s_s0) {
        uint32_t magic = s_s0[S0_OFF(magic) / 4u], ver = s_s0[S0_OFF(version) / 4u];
        uint32_t size  = s_s0[S0_OFF(size) / 4u], end = s_s0[S0_OFF(magic_end) / 4u];
        s_s0_valid = (magic == S0_STATUS_MAGIC && ver >= S0_STATUS_VERSION &&
                      size == S0_STATUS_BYTES && end == S0_STATUS_MAGIC);
        if (s_s0_valid) {
            s_fabric_sid = s_s0[S0_OFF(fabric_static_id) / 4u];
            s_fabric_ver32 = s_s0[S0_OFF(fabric_ver32) / 4u];
        }
    }

    s_have_claim = 0;
    if (g_hd.have_claim_override) {
        s_claim_sid = g_hd.static_id_claim_override;
        s_have_claim = 1;
    } else if (harnessd_read_u32_file(g_hd.static_id_file, &s_claim_sid) == 0) {
        s_have_claim = 1;
    }

    /* The FABRIC's release: USR_ACCESS when the block answers, else the
     * fabric_ver32 stage0 was baked with (the same generator feeds both). */
    uint32_t magic = hal_quiet_read32(MPS3_USRACC_BASE, USRACC_MAGIC);
    int usr_ok = (magic == USRACC_MAGIC_VALUE) &&
                 (hal_quiet_read32(MPS3_USRACC_BASE, USRACC_STATUS) & USRACC_STATUS_VALID);
    uint32_t usr = usr_ok ? hal_quiet_read32(MPS3_USRACC_BASE, USRACC_VALUE) : 0u;
    if (!usr_ok && s_fabric_ver32 != 0u) {
        usr = s_fabric_ver32;
        usr_ok = 1;
    }
    uint32_t ver32 = harnessd_manifest_ver32();

    /* The FIRST disagreement names the lock. Order = how fundamental it is. */
    if (!s_s0) {
        snprintf(s_reason, sizeof(s_reason), "no stage0 status block mapped");
    } else if (!s_s0_valid) {
        snprintf(s_reason, sizeof(s_reason), "no valid stage0 status block");
    } else if (s_fabric_sid == 0u) {
        snprintf(s_reason, sizeof(s_reason), "fabric static_id unknown");
    } else if (!s_have_claim || s_claim_sid == 0u) {
        snprintf(s_reason, sizeof(s_reason), "image static_id not provisioned");
    } else if (s_claim_sid != s_fabric_sid) {
        snprintf(s_reason, sizeof(s_reason), "image 0x%08" PRIx32 " != fabric 0x%08" PRIx32,
                 s_claim_sid, s_fabric_sid);
    } else if (usr_ok && ver32 != 0u && usr != ver32) {
        snprintf(s_reason, sizeof(s_reason), "usr_access 0x%08" PRIx32 " != image 0x%08" PRIx32,
                 usr, ver32);
    }
    if (s_reason[0]) {
        snprintf(s_refusal, sizeof(s_refusal), "identity lock: %s", s_reason);
    }

    harnessd_log("identity: fabric static_id 0x%08" PRIx32 " (stage0 block %s), image claim %s0x%08" PRIx32
                 ", usr_access %s0x%08" PRIx32 ", image ver32 0x%08" PRIx32 " -> %s%s\n",
                 s_fabric_sid, s_s0_valid ? "valid" : "ABSENT/GARBAGE",
                 s_have_claim ? "" : "(none) ", s_claim_sid,
                 usr_ok ? "" : "(none) ", usr, ver32,
                 s_reason[0] ? "LOCKED: " : "consistent", s_reason);
}

/* ---- the seams ------------------------------------------------------------ */

/* coordinator.h's static_id seam: ALWAYS the fabric's value, 0 when unknown.
 * Never the card's claim (file header). */
uint32_t mps3_shell_static_id(void)
{
    return s_fabric_sid;
}

/* The coordinator swap/commit refusal seam (Handoff H1: coordinator.c gains a
 * weak NULL default and one check in each of the two handlers). */
const char *mps3_swap_refusal(void)
{
    return s_refusal[0] ? s_refusal : 0;
}

/* `version.id_skew` (net_proto.h): the lock reason, emitted only when locked. */
const char *mps3_proto_id_skew(void)
{
    return s_reason[0] ? s_reason : 0;
}

/* ---- the stage0 boot CONFIRM ------------------------------------------------ */

/* stage0's try-once-then-confirm (STAGE0_CONTRACT.md §3.1/§4): once healthy,
 * ONE 32-bit store of S0_CONFIRM_MAGIC to att_confirm — the only word of the
 * block harnessd ever writes. Only when stage0 recorded a hand-off (phase ==
 * S0_PH_HANDOFF: this OS was booted by that stage0 run); never into a block that
 * is not valid; a respawn that finds it already confirmed writes nothing. Until
 * it is written, a WDOG reset counts as a failed attempt of the booted slot. */
int harnessd_confirm_boot(void)
{
    if (s_confirmed || !s_s0 || !s_s0_valid) {
        return 0;
    }
    s_confirmed = 1;
    if (s_s0[S0_OFF(phase) / 4u] != S0_PH_HANDOFF) {
        harnessd_log("identity: stage0 phase %u is not HANDOFF -- boot not confirmed\n",
                     (unsigned)s_s0[S0_OFF(phase) / 4u]);
        return 0;
    }
    if (s_s0[S0_OFF(att_confirm) / 4u] == S0_CONFIRM_MAGIC) {
        return 0;   /* a respawn: this boot is already confirmed */
    }
    s_s0[S0_OFF(att_confirm) / 4u] = S0_CONFIRM_MAGIC;
    harnessd_log("identity: stage0 boot %" PRIu32 " (from %" PRIu32 ") CONFIRMED\n",
                 s_s0[S0_OFF(boot_count) / 4u], s_s0[S0_OFF(att_from) / 4u]);
    return 1;
}
