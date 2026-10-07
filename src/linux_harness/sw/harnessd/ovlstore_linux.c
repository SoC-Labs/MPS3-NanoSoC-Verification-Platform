/*
 * ovlstore_linux.c — harnessd's ENGINE PROVIDER for the overlay store
 * (firmware/overlay_store/overlay_store.h "ENGINE PROVIDER"), the greybox
 * clearing, and the RESIDENT-clearing cache that makes a restart safe.
 *
 * THE STORE (D13). harnessd links the SAME glue (overlay_store.c) and the SAME
 * store (ovlstore_sd.c) as the bare-metal shell; only the block device differs.
 * Under Linux the KERNEL owns usd_spi (spi-usd -> mmc_spi -> mmcblk0;
 * HARNESSD_CONTRACT.md §3), so usd.c and ovl_bdev_usd.c are NEVER linked here and
 * nothing in this process touches 0x44A4: the store runs over ovl_bdev_posix.c on
 * the WHOLE DISK (--card, /dev/mmcblk0 on the rv32 product build), opened with
 * OVL_BDEV_POSIX_LINUX_CARD -- O_DSYNC, and the page cache dropped before every
 * read, so the commit's read-back checks the CARD, not RAM. The store then runs
 * the same MBR discovery and format rules as bare metal (parity by
 * construction), and its write guard confines every write to the 0xDA partition
 * (p4) -- writing the whole-disk node while p1-p3 are mounted is safe for
 * disjoint LBA ranges. allow_wipe is FALSE: the card also holds the running
 * system, so "erase-all" answers "wipe disabled". Card presence comes from the
 * block device node: no node (or --card none) = no card.
 *
 * THE BOOT LATCH (overlay_store.h): the power-on load happens ONCE PER FPGA
 * CONFIGURATION, never on a harnessd respawn, an OS reboot or a WDOG reset. The
 * latch is the diag mailbox word usd_boot at LMB 0x1FFB4 (diag.h v9), read and
 * written through the lmb-tail UIO page. It is the right storage because:
 *   - it is BRAM, which only a RECONFIGURATION re-initialises -- and stage0's
 *     baked image ends below 0x1FE00 (stage0.ld), so the mailbox comes up ZERO
 *     after one (stage0_status.h "PERSISTENCE" relies on the same fact);
 *   - stage0 NEVER writes the mailbox (0x1FF00-0x1FFFF is harnessd's), the kernel
 *     does not map it, and a WDOG reset / OS reboot re-runs stage0 and Linux over
 *     the same BRAM;
 *   - harnessd's own mirror (main_linux.c svc_diag) re-writes the same word every
 *     pass from overlay_store_diag_word(), which the glue restores from this latch
 *     in overlay_store_init() -- BEFORE the service table's first mirror -- so a
 *     respawn cannot zero it.
 * No LMB-tail window (a mis-built DTS) = the configuration cannot be proven fresh:
 * the latch reads "decided: none" and nothing is ever loaded (fail safe). In a
 * MOCK build the page is the --mock-fabric file: a new file is a new
 * configuration, the same file across runs is a respawn.
 *
 * THE GREYBOX CLEARING (swap_fsm_init()'s boot seed) is loaded from
 * /etc/mps3/greybox_clear.bin at start — the image carries it where the
 * bare-metal ELF bakes it (firmware/platform/generated/greybox_blob.c). It is
 * offered ONLY while the identity is consistent (identity.c): a clearing built
 * for another fabric must never reach this ICAP.
 *
 * THE RESIDENT CLEARING (parity-table row "the clearing cache survives a
 * reboot"). The FPGA keeps its RP across a harnessd respawn AND across an OS
 * reboot (a WDOG reset does not reconfigure). swap_fsm, restarted, believes the
 * greybox is resident and would clear a loaded RM with the GREYBOX's clearing.
 * So: after every successful swap -- the power-on load from the card included --
 * the resident RM's clearing + identity is written to --state-dir (write-tmp,
 * fsync, rename); at start the fabric state is snapshotted BEFORE
 * coordinator_init() touches a reset, and afterwards the resident RM is re-synced
 * from the last VERIFIED state:
 *   RM_STATUS.valid  -> rm_id = DFXCTL.RM_ID; the cached clearing if it is that
 *                       RM's, the greybox's if it is the greybox, else NONE
 *                       (the next swap fails closed rather than guess);
 *   not valid, FRESH -> (the boot latch says this start is the first in a new FPGA
 *                       configuration: MCC REBOOT / power-on) the GREYBOX, with
 *                       the greybox clearing. The record on the card (--state-dir
 *                       is /persist) predates the configuration: it is logged,
 *                       ignored and deleted, so it can neither name an RM that is
 *                       not loaded nor suppress the power-on load (2026-10-01).
 *   not valid        -> (RP decoupled / in reset: a death mid-swap, or a WDOG
 *                       reset's clamp) the cached record, which was verified by
 *                       DFXCTL when it was written; the RP is left PARKED — never
 *                       released blindly — and the fact is logged.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "../../../../firmware/common/crc32.h"
#include "../../../../firmware/common/diag.h"
#include "../../../../firmware/common/platform_regs.h"
#include "../../../../firmware/config_agent/config_agent.h"
#include "../../../../firmware/coordinator/coordinator.h"
#include "../../../../firmware/coordinator/swap_fsm.h"
#include "../../../../firmware/overlay_store/overlay_store.h"
#include "../../../../firmware/overlay_store/ovl_bdev.h"
#include "hal.h"
#include "harnessd.h"

#define GREYBOX_MAX_BYTES  (1024u * 1024u)
#define GREYBOX_RM_ID      0u   /* fpga/dfx/rm_list.tcl RM_LIB(rm_greybox,rm_id) */

static uint8_t *s_grey;
static uint32_t s_grey_len;
static uint32_t s_grey_crc;

/* ==========================================================================
 * The engine provider: the card, and the boot latch
 * ========================================================================== */
static ovl_bdev_posix_t s_px;
static int              s_px_open;

/* No card visible (no node, or --card none): state NONE, every op -ENODEV. */
static int nc_rd(void *c, uint32_t l, uint32_t n, void *b)        { (void)c; (void)l; (void)n; (void)b; return -ENODEV; }
static int nc_wr(void *c, uint32_t l, uint32_t n, const void *b)  { (void)c; (void)l; (void)n; (void)b; return -ENODEV; }
static int              nc_io(void *c)  { (void)c; return -ENODEV; }
static uint32_t         nc_nb(void *c)  { (void)c; return 0u; }
static bool             nc_pr(void *c)  { (void)c; return false; }
static ovl_bdev_state_t nc_st(void *c)  { (void)c; return OVL_BDEV_NONE; }
static int              nc_err(void *c) { (void)c; return 0; }
static const ovl_bdev_ops_t k_nocard = { nc_rd, nc_wr, nc_io, NULL, nc_nb, nc_pr, nc_st, nc_err, NULL };

int ovlstore_engine_bind(ovl_bdev_t *bd, ovlstore_sd_cfg_t *cfg)
{
    cfg->allow_wipe = false;       /* the card holds the running system (p1-p3) */
    cfg->raw_partition = false;    /* the whole disk: the same MBR rules as bare metal */
    if (s_px_open) {
        ovl_bdev_posix_close(&s_px);
        s_px_open = 0;
    }
    if (g_hd.card == NULL || g_hd.card[0] == '\0' || strcmp(g_hd.card, "none") == 0) {
        harnessd_log("usd: no card device configured (--card) -- the store reports no card\n");
    } else {
        int rc = ovl_bdev_posix_open(&s_px, bd, g_hd.card, OVL_BDEV_POSIX_LINUX_CARD);
        if (rc == 0) {
            s_px_open = 1;
            harnessd_log("usd: overlay store on %s (%" PRIu32 " blocks, whole disk, wipe disabled)\n",
                         g_hd.card, s_px.nblocks);
            return 0;
        }
        harnessd_log("usd: %s: %s -- the store reports no card\n", g_hd.card, strerror(-rc));
    }
    bd->ops = &k_nocard;
    bd->ctx = NULL;
    bd->max_blocks = 1u;
    return 0;
}

/* The latch word in the lmb-tail page (diag.h: mailbox base + usd_boot). */
static volatile uint32_t *latch_word(void)
{
    return hal_backend_window(HARNESSD_MAILBOX_ADDR + offsetof(mps3_diag_t, usd_boot), 4u);
}

/* THIS START'S FRESHNESS, as the one latch read found it: 1 = the latch lacked
 * OVL_BOOT_LATCH_MAGIC, i.e. the BRAM came up zero -- a FRESH FPGA configuration
 * (MCC REBOOT / power-on). 0 = a respawn / OS reboot / WDOG reset in the same
 * configuration, or no lmb-tail window (cannot prove fresh). The read happens in
 * overlay_store_init() -> boot_restore(), inside coordinator_init(), BEFORE
 * harnessd_resident_restore() and before the first boot_step() writes PENDING
 * (main_linux.c), so the flag is what this START found. */
static int s_cfg_fresh;

uint32_t ovlstore_engine_latch_get(void)
{
    volatile uint32_t *w = latch_word();
    s_cfg_fresh = 0;
    if (w == NULL) {
        /* Cannot prove this configuration is fresh: fail safe, never load. */
        harnessd_log("usd: no lmb-tail window -- the power-on load is DISABLED (cannot tell a "
                     "reconfiguration from a restart)\n");
        return ((uint32_t)OVL_BOOT_LATCH_MAGIC << 16) | OVL_BOOT_NONE;
    }
    uint32_t v = *w;
    if ((v >> 16) == OVL_BOOT_LATCH_MAGIC) {
        harnessd_log("usd: the power-on decision was already taken in this FPGA configuration "
                     "(latch 0x%08" PRIx32 ") -- a respawn / OS reboot / WDOG reset never "
                     "reloads\n", v);
    } else {
        s_cfg_fresh = 1;
        harnessd_log("usd: a FRESH FPGA configuration -- this start takes the power-on "
                     "decision\n");
    }
    return v;
}

void ovlstore_engine_latch_set(uint32_t word)
{
    volatile uint32_t *w = latch_word();
    if (w != NULL) {
        *w = word;   /* one 32-bit store into BRAM; the mirror keeps it there */
    }
}

int overlay_store_get_greybox_clearing(overlay_manifest_info_t *out)
{
    if (!s_grey || harnessd_identity_locked()) {
        return -1;   /* no clearing for a fabric whose identity is not proven */
    }
    out->static_id       = harnessd_fabric_static_id();
    out->rm_id           = GREYBOX_RM_ID;
    out->clear_len_words = s_grey_len / 4u;
    out->clear_crc32     = s_grey_crc;
    out->clear_data      = s_grey;
    return 0;
}

/* coordinator.h's CARD-JOB seam (`reboot` answers EBUSY while it is 1): ONE
 * strong definition for the ONE card, so it answers for both of its writers --
 * the slots (a push or a `verify`, slot_linux.c) and D13's store (a `commit`, a
 * `usd` format/clear). The store's own probe/verify reads at start and the
 * power-on load are not jobs here: the load is a swap (refused on its own) and a
 * read-only probe must not be able to hold `reboot` -- the recovery path -- off.
 * Today the store half cannot be reached over the wire: a commit / format / clear
 * PARKS the single 6900 client until it settles, so no `reboot` can arrive
 * meanwhile (tried end to end, 2026-09-26). It is kept as the rule, not as a
 * consequence of the transport: the slot half is the one B2 hit (6910 pushes
 * leave 6900 free). */
int mps3_card_job_active(void)
{
    return harnessd_slot_busy() || overlay_store_commit_active() ||
           overlay_store_action_active();
}

int harnessd_greybox_load(void)
{
    free(s_grey);
    s_grey = 0;
    s_grey_len = 0;

    if (g_hd.scenario && strcmp(g_hd.scenario, "ctrl-echo") == 0 && !g_hd.greybox_file) {
        /* The conformance twin: four ICAP type-1 NOPs, like gen_greybox_blob.py's
         * placeholder mode — streaming them is a hardware no-op. */
        static const uint8_t nops[16] = { 0x20,0,0,0, 0x20,0,0,0, 0x20,0,0,0, 0x20,0,0,0 };
        s_grey = malloc(sizeof(nops));
        memcpy(s_grey, nops, sizeof(nops));
        s_grey_len = sizeof(nops);
    } else {
        FILE *f = g_hd.greybox_file ? fopen(g_hd.greybox_file, "rb") : 0;
        if (!f) {
            harnessd_log("greybox: %s: %s -- no greybox clearing: the first swap "
                         "away from the greybox will fail closed\n",
                         g_hd.greybox_file ? g_hd.greybox_file : "(none)", strerror(errno));
            return -1;
        }
        s_grey = malloc(GREYBOX_MAX_BYTES);
        size_t n = s_grey ? fread(s_grey, 1, GREYBOX_MAX_BYTES, f) : 0;
        int more = s_grey ? (fgetc(f) != EOF) : 1;
        fclose(f);
        if (!s_grey || n == 0 || more || (n % 4u) != 0u) {
            harnessd_log("greybox: %s rejected (%zu B%s) -- must be 4..%u B, whole words\n",
                         g_hd.greybox_file, n, more ? "+, too big" : "", GREYBOX_MAX_BYTES);
            free(s_grey);
            s_grey = 0;
            return -1;
        }
        s_grey_len = (uint32_t)n;
    }
    s_grey_crc = mps3_crc32(s_grey, s_grey_len);
    harnessd_log("greybox: %" PRIu32 " B, crc32 0x%08" PRIx32 "\n", s_grey_len, s_grey_crc);
    return 0;
}

/* ==========================================================================
 * The resident clearing
 * ========================================================================== */
typedef struct {
    int      have;
    uint32_t rm_id, static_id, len_words, crc32;
    uint8_t *data;
} resident_t;

static resident_t s_res;               /* the loaded record (owns `data`)          */
static uint32_t   s_swaps_seen;        /* swap_fsm_completed() at the last look    */
static int        s_parked;
static struct {                        /* the fabric as FOUND, before any init    */
    int      taken;
    uint32_t dfx_status, rm_id, rm_status;
} s_found;

int harnessd_rp_parked(void) { return s_parked; }

static void path_of(char *out, size_t cap, const char *leaf)
{
    snprintf(out, cap, "%s/%s", g_hd.state_dir, leaf);
}

static int mkdir_p(const char *dir)
{
    char tmp[512];
    snprintf(tmp, sizeof(tmp), "%s", dir);
    for (char *p = tmp + 1; *p; p++) {
        if (*p == '/') {
            *p = '\0';
            (void)mkdir(tmp, 0755);
            *p = '/';
        }
    }
    return (mkdir(tmp, 0755) == 0 || errno == EEXIST) ? 0 : -1;
}

static int write_atomic(const char *path, const void *buf, size_t len)
{
    char tmp[600];
    snprintf(tmp, sizeof(tmp), "%s.tmp", path);
    int fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
    if (fd < 0) {
        return -1;
    }
    const uint8_t *p = buf;
    size_t done = 0;
    while (done < len) {
        ssize_t w = write(fd, p + done, len - done);
        if (w <= 0) {
            if (w < 0 && errno == EINTR) continue;
            close(fd);
            unlink(tmp);
            return -1;
        }
        done += (size_t)w;
    }
    if (fsync(fd) != 0 || close(fd) != 0) {
        unlink(tmp);
        return -1;
    }
    return rename(tmp, path);
}

static void resident_save(void)
{
    const mps3_clearing_ref_t *c = &g_current_rm_clearing;
    char pb[512], pi[512], id[160];
    if (!c->valid || !c->data || c->len_words == 0u || harnessd_identity_locked()) {
        return;
    }
    if (mkdir_p(g_hd.state_dir) != 0) {
        harnessd_log("persist: cannot create %s: %s\n", g_hd.state_dir, strerror(errno));
        return;
    }
    path_of(pb, sizeof(pb), "resident.bin");
    path_of(pi, sizeof(pi), "resident.id");
    int n = snprintf(id, sizeof(id),
                     "rm_id=0x%08" PRIx32 "\nstatic_id=0x%08" PRIx32 "\nlen_words=%" PRIu32
                     "\ncrc32=0x%08" PRIx32 "\n",
                     g_shell_state.current_rm_id, c->static_id, c->len_words, c->crc32);
    /* The id file is written LAST: a torn save leaves an id that does not match
     * the bin (CRC), which restore rejects — never a matching pair of halves. */
    if (write_atomic(pb, c->data, (size_t)c->len_words * 4u) != 0 ||
        write_atomic(pi, id, (size_t)n) != 0) {
        harnessd_log("persist: saving the resident clearing to %s failed: %s\n",
                     g_hd.state_dir, strerror(errno));
        return;
    }
    harnessd_log("persist: resident RM 0x%08" PRIx32 " clearing (%" PRIu32 " B) saved\n",
                 g_shell_state.current_rm_id, c->len_words * 4u);
}

void harnessd_resident_poll(void)
{
    uint32_t n = swap_fsm_completed();
    if (n == s_swaps_seen) {
        return;
    }
    s_swaps_seen = n;
    const mps3_swap_result_t *r = swap_fsm_last_result();
    if (r->valid && r->ok) {
        resident_save();
        s_parked = 0;
    }
}

static int resident_load(resident_t *r)
{
    char pb[512], pi[512], line[128];
    memset(r, 0, sizeof(*r));
    path_of(pi, sizeof(pi), "resident.id");
    path_of(pb, sizeof(pb), "resident.bin");
    FILE *f = fopen(pi, "r");
    if (!f) {
        return -1;
    }
    while (fgets(line, sizeof(line), f)) {
        char *eq = strchr(line, '=');
        if (!eq) continue;
        *eq = '\0';
        uint32_t v = (uint32_t)strtoul(eq + 1, 0, 0);
        if (!strcmp(line, "rm_id")) r->rm_id = v;
        else if (!strcmp(line, "static_id")) r->static_id = v;
        else if (!strcmp(line, "len_words")) r->len_words = v;
        else if (!strcmp(line, "crc32")) r->crc32 = v;
    }
    fclose(f);
    if (r->len_words == 0u || r->len_words > GREYBOX_MAX_BYTES / 4u) {
        return -1;
    }
    r->data = malloc((size_t)r->len_words * 4u);
    f = fopen(pb, "rb");
    size_t got = (f && r->data) ? fread(r->data, 1, (size_t)r->len_words * 4u, f) : 0;
    if (f) fclose(f);
    if (got != (size_t)r->len_words * 4u || mps3_crc32(r->data, (uint32_t)got) != r->crc32) {
        free(r->data);
        r->data = 0;
        return -2;
    }
    r->have = 1;
    return 0;
}

/* Invalidate the on-card record: the id file first (restore keys on it, and a
 * missing id is "nothing recorded"), then the bin. */
static void resident_forget(void)
{
    char pb[512], pi[512];
    path_of(pi, sizeof(pi), "resident.id");
    path_of(pb, sizeof(pb), "resident.bin");
    if ((unlink(pi) != 0 && errno != ENOENT) || (unlink(pb) != 0 && errno != ENOENT)) {
        harnessd_log("resident: cannot invalidate the cached record in %s: %s -- a respawn "
                     "before the next swap would trust it\n", g_hd.state_dir, strerror(errno));
    }
}

/* Called BEFORE coordinator_init(): what the fabric looks like before any init
 * code has written a reset or a decouple. */
void harnessd_fabric_snapshot(void)
{
    s_found.dfx_status = mps3_reg_read32(MPS3_DFXCTL_BASE, DFXCTL_STATUS);
    s_found.rm_status  = mps3_reg_read32(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS);
    s_found.rm_id      = mps3_reg_read32(MPS3_DFXCTL_BASE, DFXCTL_RM_ID);
    s_found.taken = 1;
}

void harnessd_resident_restore(void)
{
    s_swaps_seen = swap_fsm_completed();
    if (harnessd_identity_locked()) {
        /* swap_fsm_init() found no greybox clearing (refused above), so the
         * resident clearing is already invalid: any swap fails closed. */
        harnessd_log("resident: identity LOCKED -- no clearing offered; swaps refused\n");
        return;
    }
    int valid = s_found.taken && (s_found.rm_status & DFXCTL_RM_STATUS_RM_ID_VALID);
    s_parked = s_found.taken &&
               (s_found.dfx_status & (DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET));
    int rc = resident_load(&s_res);
    if (rc == -2) {
        harnessd_log("resident: cached record in %s is torn/corrupt (CRC) -- ignored\n",
                     g_hd.state_dir);
    }
    if (s_res.have && s_res.static_id != harnessd_fabric_static_id()) {
        harnessd_log("resident: cached record is for static 0x%08" PRIx32 " -- ignored\n",
                     s_res.static_id);
        free(s_res.data);
        memset(&s_res, 0, sizeof(s_res));
    }
    int fresh = !valid && s_cfg_fresh;
    if (fresh) {
        /* A FRESH FPGA configuration: the greybox base is resident (its RP comes
         * up in reset, so RM_STATUS reads not valid) and the record on the card
         * was written in an EARLIER configuration. Trusting it would name an RM
         * that is not loaded, clear the greybox with that RM's clearing on the
         * next swap, and suppress the power-on load (boot_step(): current_rm_id
         * != 0 is "not ours"). It is DELETED, not only ignored: a respawn / OS
         * reboot / WDOG reset later in this configuration, before any swap, is
         * not fresh and still finds the greybox's RP in reset -- it would take
         * the record again. With it gone that start says "nothing recorded: the
         * greybox assumed", which is true; the next successful swap (the
         * power-on load included) writes a new one. */
        if (s_res.have) {
            harnessd_log("resident: FRESH FPGA configuration -- the cached record (RM 0x%08"
                         PRIx32 ") predates it and is ignored; the greybox is resident\n",
                         s_res.rm_id);
            free(s_res.data);
            memset(&s_res, 0, sizeof(s_res));
        }
        resident_forget();
    }

    uint32_t rm;
    const char *how;
    if (valid) {
        rm = s_found.rm_id;
        how = "DFXCTL.RM_ID (verified now)";
    } else if (fresh) {
        rm = GREYBOX_RM_ID;
        how = "a FRESH FPGA configuration";
    } else if (s_res.have) {
        rm = s_res.rm_id;
        how = "the last verified swap (cache)";
    } else {
        rm = GREYBOX_RM_ID;
        how = "nothing recorded: the greybox assumed";
    }
    g_shell_state.current_rm_id = rm;

    if (s_res.have && s_res.rm_id == rm) {
        mps3_clearing_ref_t ref = {
            .valid = true, .rm_id = s_res.rm_id, .static_id = s_res.static_id,
            .len_words = s_res.len_words, .crc32 = s_res.crc32,
            .data = s_res.data,
        };
        swap_fsm_set_current_clearing(&ref);
        harnessd_log("resident: RM 0x%08" PRIx32 " from %s; its cached clearing restored\n", rm, how);
    } else if (rm == GREYBOX_RM_ID) {
        harnessd_log("resident: RM 0x%08" PRIx32 " (greybox) from %s; greybox clearing\n", rm, how);
    } else {
        mps3_clearing_ref_t none;
        memset(&none, 0, sizeof(none));
        swap_fsm_set_current_clearing(&none);
        harnessd_log("resident: RM 0x%08" PRIx32 " from %s has NO cached clearing -- the next "
                     "swap fails closed (recover: MCC REBOOT reconfigures the fabric)\n", rm, how);
    }
    if (s_parked) {
        harnessd_log("resident: RP found %s%s -- PARKED SAFE, not released (only a swap "
                     "releases it, deliberately)\n",
                     (s_found.dfx_status & DFXCTL_STATUS_DECOUPLED) ? "DECOUPLED " : "",
                     (s_found.dfx_status & DFXCTL_STATUS_RP_IN_RESET) ? "IN RESET" : "");
    }
}
