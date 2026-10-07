/*
 * overlay_store.c -- the overlay store glue (D13 lane I-FW): the one SD store
 * instance (ovlstore_sd.c) as the shell firmware's service modules use it, on
 * BOTH engines. See overlay_store.h for the API and THE BOOT LATCH.
 *
 * The SST26 / AXI-Quad-SPI backend that lived here is DELETED (D13 L3a): the
 * 0x44A4 page is usd_spi now, the DUT owns the real SST26 (D16), and a compat
 * path would send flash opcodes to an SD card. Nothing in this file touches a
 * register except the one PB1 level read at the boot check.
 *
 * Engine-specific pieces live in the ENGINE PROVIDER (overlay_store.h): the
 * block device, allow_wipe, the boot latch's storage and the greybox clearing.
 */
#include <errno.h>
#include <stdio.h>
#include <string.h>

#include "overlay_store.h"
#include "ovl_bdev.h"
#include "../common/platform_regs.h"
#include "../common/timebase.h"
#include "../config_agent/config_agent.h"
#include "../coordinator/coordinator.h"
#include "../coordinator/swap_fsm.h"

/* ---- phase hook -------------------------------------------------------------------- */

/* WEAK no-op default (overlay_store.h). firmware/platform/src/ovlstore_phase.c
 * overrides it on bare metal to publish into the JTAG mailbox. */
__attribute__((weak)) void mps3_ovlstore_phase(uint32_t p, uint32_t d)
{
    (void)p;
    (void)d;
}

/* ---- the store --------------------------------------------------------------------- */

static ovlstore_sd_t s_sd;
static ovl_bdev_t    s_bd;

/* An engine that binds no device at all gets this: state NO_HW, every op
 * -ENODEV. The store then never starts an op (it only does so on READY). */
static int nohw_rw(void *c, uint32_t l, uint32_t n, void *b)
{
    (void)c; (void)l; (void)n; (void)b;
    return -ENODEV;
}
static int nohw_w(void *c, uint32_t l, uint32_t n, const void *b)
{
    (void)c; (void)l; (void)n; (void)b;
    return -ENODEV;
}
static int              nohw_io(void *c)    { (void)c; return -ENODEV; }
static uint32_t         nohw_nb(void *c)    { (void)c; return 0u; }
static bool             nohw_pr(void *c)    { (void)c; return false; }
static ovl_bdev_state_t nohw_st(void *c)    { (void)c; return OVL_BDEV_NO_HW; }
static int              nohw_err(void *c)   { (void)c; return 0; }
static const ovl_bdev_ops_t k_nohw_ops = {
    nohw_rw, nohw_w, nohw_io, NULL, nohw_nb, nohw_pr, nohw_st, nohw_err, NULL
};

#ifdef MPS3_HAS_CLCD
/* The rm_id -> short name table the CLCD already owns (firmware/clcd/clcd.c).
 * Declared here rather than by including clcd.h: this is the one symbol used. */
const char *clcd_rm_name(uint32_t rm_id);
static const char *rm_name_cb(uint32_t rm_id, void *ctx)
{
    (void)ctx;
    return clcd_rm_name(rm_id);
}
#endif

/* ---- glue state -------------------------------------------------------------------- */

/* CLCD change counter (overlay_store_usd_change_count): bumped whenever the
 * inputs of the row-4 text move. */
static uint32_t s_cc;
static uint32_t s_text_sig;
static uint32_t s_phase_last;

/* The `usd` actions (format / clear) run as store jobs; the connection parks. */
static uint8_t s_act_running;

/* commit */
enum { C_IDLE = 0, C_FEED_CLEAR, C_FEED_PART, C_FINAL, C_DONE };
static uint8_t            s_c_st;
static int                s_c_rc;
static char               s_c_slot;
static ovlstore_sd_desc_t s_c_desc;
static uint32_t           s_c_last_ms;

/* the swap source */
static int s_src_err;     /* the last negative stream_next(), 0 = none */

/* boot */
enum { B_UNARMED = 0, B_WAIT, B_LOADING, B_DONE };
static uint8_t  s_b_st;
static uint32_t s_b_word;     /* the latch word, once the decision is taken */
static uint32_t s_b_t0;
static uint32_t s_b_swaps;
static char     s_b_text[40];

static uint32_t now_ms(void)
{
    return mps3_sys_now_ms();
}

/* ---- CLCD exports -------------------------------------------------------------------- */

const char *overlay_store_usd_text(void)
{
    return ovlstore_sd_state_text(&s_sd);
}

uint32_t overlay_store_usd_change_count(void)
{
    return s_cc;
}

static void text_watch(void)
{
    ovlstore_sd_info_t in;
    uint32_t sig;

    ovlstore_sd_info(&s_sd, &in);
    sig = (uint32_t)in.state ^ ((uint32_t)in.skip << 5) ^ ((uint32_t)(uint8_t)in.slot << 8) ^
          ((uint32_t)in.err << 16) ^ (in.have_default ? in.def.rm_id * 2654435761u : 0u);
    if (sig != s_text_sig) {
        s_text_sig = sig;
        s_cc++;
    }
}

/* ---- phase ------------------------------------------------------------------------- */

static void phase_watch(void)
{
    uint32_t ph;

    switch (ovlstore_sd_job(&s_sd)) {
    case OVLSD_JOB_PROBE:  ph = OVL_PHASE_READ_HDR;     break;
    case OVLSD_JOB_VERIFY: ph = OVL_PHASE_CRC;          break;
    case OVLSD_JOB_STREAM: ph = OVL_PHASE_STREAM;       break;
    case OVLSD_JOB_COMMIT:
    case OVLSD_JOB_FORMAT:
    case OVLSD_JOB_CLEAR:  ph = OVL_PHASE_PROGRAM_PAGE; break;
    default:
        ph = (ovlstore_sd_state(&s_sd) == OVLSD_INIT) ? OVL_PHASE_INIT : OVL_PHASE_IDLE;
        break;
    }
    if (ph != s_phase_last) {
        s_phase_last = ph;
        mps3_ovlstore_phase(ph, 0u);
    }
}

/* ---- boot ---------------------------------------------------------------------------- */

static uint32_t boot_word(uint32_t code, uint32_t why)
{
    return ((uint32_t)OVL_BOOT_LATCH_MAGIC << 16) | ((why & 0xFFu) << 8) | (code & 0xFu);
}

static void boot_latch(uint32_t code, uint32_t why)
{
    s_b_word = boot_word(code, why);
    ovlstore_engine_latch_set(s_b_word);
    if (code != OVL_BOOT_PENDING) {
        s_b_st = B_DONE;
    }
}

static bool pb1_held(void)
{
#ifdef MPS3_HAS_CLCD_KVM
    /* The DEBOUNCED level, sampled ONCE at the boot check (a level, not an
     * edge: holding PB1 through power-up is the escape hatch). */
    return (mps3_reg_read32(MPS3_CLCDKVM_BASE, CLCDKVM_STATUS) & CLCDKVM_STATUS_PB_LEVEL) != 0u;
#else
    return false;   /* no KVM, no PB1: never held */
#endif
}

static const char *rm_label(uint32_t rm_id, char *buf, size_t cap)
{
#ifdef MPS3_HAS_CLCD
    const char *n = clcd_rm_name(rm_id);
    if (n != NULL && n[0] != '\0') {
        return n;
    }
#endif
    (void)snprintf(buf, cap, "0x%08lx", (unsigned long)rm_id);
    return buf;
}

/* One step of the power-on load hook (overlay_store.h THE BOOT LATCH). */
static void boot_step(uint32_t now)
{
    switch (s_b_st) {
    case B_UNARMED: {
        /* (A latch already decided in this configuration was restored by
         * overlay_store_init(): boot_restore().) The first look in this
         * configuration: CONSUME the latch before
         * anything else, so no crash or reset from here on can make it load
         * twice. */
        s_b_t0 = now;
        boot_latch(OVL_BOOT_PENDING, 0u);
        s_b_st = B_WAIT;
        if (pb1_held()) {
            ovlstore_sd_set_skip(&s_sd, true);
            boot_latch(OVL_BOOT_SKIPPED, 0u);
        }
        return;
    }
    case B_WAIT: {
        ovlstore_sd_desc_t d;
        uint32_t cap = 0u;
        char lbl[16];

        if ((uint32_t)(now - s_b_t0) < OVLSTORE_BOOT_GRACE_MS) {
            return;
        }
        switch (ovlstore_sd_state(&s_sd)) {
        case OVLSD_NO_CARD:
        case OVLSD_NO_HW:
            boot_latch(OVL_BOOT_NONE, 0u);
            return;
        case OVLSD_INIT:
            if ((uint32_t)(now - s_b_t0) >= OVLSTORE_BOOT_TIMEOUT_MS) {
                boot_latch(OVL_BOOT_FAILED, OVL_BOOT_WHY_TIMEOUT);
            }
            return;
        case OVLSD_VALID:
            break;
        default:   /* FOREIGN EMPTY STALE BAD UNSUPPORTED ERROR: the state says why */
            boot_latch(OVL_BOOT_NONE, 0u);
            return;
        }
        if (ovlstore_sd_job(&s_sd) != OVLSD_JOB_NONE) {
            return;   /* VALID but a job is running (a rescan): wait */
        }
        if (mps3_swap_refusal() != NULL) {
            boot_latch(OVL_BOOT_FAILED, OVL_BOOT_WHY_IDLOCK);   /* fabric unproven */
            return;
        }
        if (!swap_fsm_idle()) {
            boot_latch(OVL_BOOT_FAILED, OVL_BOOT_WHY_BUSY);
            return;
        }
        if (g_shell_state.current_rm_id != 0u) {
            boot_latch(OVL_BOOT_NONE, 0u);   /* the RP is not the greybox: not ours */
            return;
        }
        if (overlay_store_src_default(&d) != OVLSD_OK) {
            boot_latch(OVL_BOOT_NONE, 0u);
            return;
        }
        (void)swap_fsm_clearing_stage_buffer(&cap);
        if (d.clear_len > cap) {
            /* The resident clearing must fit RAM, or the next swap-away could
             * not clear this RM: refuse before arming anything. */
            boot_latch(OVL_BOOT_FAILED, OVL_BOOT_WHY_TOOBIG);
            return;
        }
        s_b_swaps = swap_fsm_completed();
        s_src_err = 0;
        if (swap_fsm_start(rm_label(d.rm_id, lbl, sizeof lbl), "usd") != 0) {
            boot_latch(OVL_BOOT_FAILED, OVL_BOOT_WHY_BUSY);
            return;
        }
        s_b_st = B_LOADING;
        return;
    }
    case B_LOADING: {
        const mps3_swap_result_t *r;
        if (swap_fsm_completed() == s_b_swaps) {
            return;   /* the swap FSM is still loading it */
        }
        r = swap_fsm_last_result();
        if (r->valid && r->ok) {
            boot_latch(OVL_BOOT_LOADED, 0u);
        } else if (s_src_err < 0) {
            boot_latch(OVL_BOOT_FAILED, OVL_BOOT_WHY_STORE | ((uint32_t)(-s_src_err) & 0x3Fu));
        } else {
            boot_latch(OVL_BOOT_FAILED,
                       OVL_BOOT_WHY_SWAP | ((uint32_t)swap_fsm_fail_state() & 0x7Fu));
        }
        return;
    }
    default:
        return;
    }
}

const char *overlay_store_boot_text(void)
{
    uint32_t why = (s_b_word >> 8) & 0xFFu;
    const char *w = "";

    if (s_b_word == 0u) {
        return "pending";   /* before the first look */
    }
    switch (s_b_word & 0xFu) {
    case OVL_BOOT_LOADED:  return "loaded";
    case OVL_BOOT_SKIPPED: return "skipped";
    case OVL_BOOT_NONE:    return "none";
    case OVL_BOOT_FAILED:  break;
    default:               return "pending";
    }
    if (why & OVL_BOOT_WHY_SWAP) {
        w = swap_fsm_state_name((mps3_swap_state_t)(why & 0x7Fu));
    } else if (why & OVL_BOOT_WHY_STORE) {
        w = overlay_store_err_name(-(int)(why & 0x3Fu));
    } else {
        switch (why) {
        case OVL_BOOT_WHY_TIMEOUT: w = "timeout";       break;
        case OVL_BOOT_WHY_IDLOCK:  w = "identity lock"; break;
        case OVL_BOOT_WHY_TOOBIG:  w = "too big";       break;
        case OVL_BOOT_WHY_ABORTED: w = "aborted";       break;
        case OVL_BOOT_WHY_BUSY:    w = "store busy";    break;
        default:                   w = "io";            break;
        }
    }
    (void)snprintf(s_b_text, sizeof s_b_text, "failed:%s", w);
    return s_b_text;
}

uint32_t overlay_store_diag_word(void)
{
    return s_b_word;
}

/* At init, BEFORE any service runs: a latch decided earlier in THIS
 * configuration (a respawn, an OS reboot or a WDOG reset) is restored and never
 * re-decided -- report what was decided. A PENDING word means that decision was
 * interrupted mid-load: "failed:aborted", and still no retry. Done here, not at
 * the first service call, so the diag word the mailbox mirror publishes carries
 * the latch from the very first pass (under Linux that word IS the latch). */
static void boot_restore(void)
{
    uint32_t w = ovlstore_engine_latch_get();

    if ((w >> 16) != OVL_BOOT_LATCH_MAGIC) {
        return;   /* fresh: the first service call decides */
    }
    if ((w & 0xFu) == OVL_BOOT_PENDING) {
        boot_latch(OVL_BOOT_FAILED, OVL_BOOT_WHY_ABORTED);
    } else {
        s_b_word = w;
        s_b_st = B_DONE;
    }
    if ((s_b_word & 0xFu) == OVL_BOOT_SKIPPED) {
        ovlstore_sd_set_skip(&s_sd, true);   /* the CLCD keeps saying so */
    }
}

/* The power-on decision is still open (pending or loading): the write-side
 * actions and commit wait for it. */
static bool boot_open(void)
{
    return s_b_st == B_UNARMED || s_b_st == B_WAIT || s_b_st == B_LOADING;
}

/* ---- commit ---------------------------------------------------------------------------- */

static bool c_feeding(void)
{
    return s_c_st == C_FEED_CLEAR || s_c_st == C_FEED_PART;
}

static void c_fail(int rc)
{
    if (c_feeding() || s_c_st == C_FINAL) {
        ovlstore_sd_commit_abort(&s_sd);   /* a no-op once commit_end() ran */
    }
    s_c_rc = rc;
    s_c_st = C_DONE;
    config_agent_set_commit_sink(NULL);
}

static int c_begin(const mps3_bitstream_hdr_t *h)
{
    uint8_t  kind;
    uint32_t len, crc;

    if (s_c_st == C_FEED_CLEAR) {
        kind = MPS3_BIN_KIND_CLEARING;
        len = s_c_desc.clear_len;
        crc = s_c_desc.clear_crc;
    } else if (s_c_st == C_FEED_PART) {
        kind = MPS3_BIN_KIND_PARTIAL;
        len = s_c_desc.part_len;
        crc = s_c_desc.part_crc;
    } else {
        return -1;   /* not expecting a push (the commit already ended) */
    }
    /* The push must be EXACTLY the pair the request described, before one byte
     * of it is fed: kind, identity, length and CRC. */
    if (h->kind != kind) {
        c_fail(OVLSD_EORDER);
        return -1;
    }
    if (h->static_id != s_c_desc.static_id) {
        c_fail(OVLSD_ESTATIC);
        return -1;
    }
    if (h->rm_id != s_c_desc.rm_id) {
        c_fail(OVLSD_ERMID);
        return -1;
    }
    if (h->len_words * 4u != len || h->crc32 != crc) {
        c_fail(OVLSD_ECRC);
        return -1;
    }
    s_c_last_ms = now_ms();
    return 0;
}

static int c_write_some(const void *buf, uint32_t len, uint32_t *used)
{
    ovlstore_sd_which_t w = (s_c_st == C_FEED_PART) ? OVLSD_PARTIAL : OVLSD_CLEARING;
    int rc;

    *used = 0u;
    if (!c_feeding()) {
        return -1;
    }
    rc = ovlstore_sd_commit_feed(&s_sd, w, buf, len, used);
    if (rc == OVLSD_OK || rc == OVLSD_BUSY) {
        if (*used > 0u) {
            s_c_last_ms = now_ms();
        }
        return 0;   /* *used < len is back-pressure, not an error */
    }
    c_fail(rc);
    return -1;
}

static int c_finish(const mps3_bitstream_hdr_t *h)
{
    int rc;

    (void)h;
    if (s_c_st == C_FEED_CLEAR) {
        s_c_st = C_FEED_PART;   /* the store pads the clearing to a block boundary */
        s_c_last_ms = now_ms();
        return 0;
    }
    if (s_c_st != C_FEED_PART) {
        return -1;
    }
    /* Every byte is in: flush, READ BOTH REGIONS BACK and CRC them, then flip
     * the header. No input is needed from here on. */
    rc = ovlstore_sd_commit_end(&s_sd);
    if (rc != OVLSD_OK) {
        c_fail(rc);
        return -1;
    }
    s_c_st = C_FINAL;
    config_agent_set_commit_sink(NULL);
    return 0;
}

static void c_abort(int why)
{
    int rc;

    if (!c_feeding()) {
        return;   /* already failed (it recorded why), or done */
    }
    switch (why) {
    case CFG_AGENT_ERR_CRC:       rc = OVLSD_ECRC;     break;
    case CFG_AGENT_ERR_STATIC_ID: rc = OVLSD_ESTATIC;  break;
    case -1:
    case CFG_AGENT_ERR_TRUNCATED: rc = OVLSD_EABORTED; break;   /* torn push: "io" */
    default:                      rc = OVLSD_EARG;     break;   /* "bad args" */
    }
    c_fail(rc);
}

static const mps3_cfg_agent_commit_sink_t k_commit_sink = {
    c_begin, c_write_some, c_finish, c_abort,
};

int overlay_store_commit_begin(const ovlstore_sd_desc_t *desc, uint32_t live_static_id,
                               uint32_t live_rm_id)
{
    int rc;

    if (desc == NULL) {
        return OVLSD_EARG;
    }
    if (s_c_st != C_IDLE && s_c_st != C_DONE) {
        return OVLSD_EBUSY;
    }
    if (s_act_running || boot_open() || !swap_fsm_idle()) {
        return OVLSD_EBUSY;
    }
    rc = ovlstore_sd_commit_begin(&s_sd, desc, live_static_id, live_rm_id);
    if (rc != OVLSD_OK) {
        return rc;
    }
    s_c_desc = *desc;
    s_c_rc = OVLSD_BUSY;
    s_c_slot = 0;
    s_c_st = C_FEED_CLEAR;
    s_c_last_ms = now_ms();
    config_agent_set_commit_sink(&k_commit_sink);
    return OVLSD_OK;
}

/* From the service row: the idle timeout, and a store job that ended under the
 * commit (a card pulled mid-feed, a write failure) while no byte is arriving. */
static void commit_watch(uint32_t now)
{
    if (c_feeding()) {
        int jr = ovlstore_sd_job_result(&s_sd);
        if (jr != OVLSD_BUSY) {
            c_fail((jr < 0) ? jr : OVLSD_EABORTED);
            config_agent_abort_session();   /* tear the push it was feeding */
            return;
        }
        if ((uint32_t)(now - s_c_last_ms) >= MPS3_SWAP_AWAIT_IDLE_MS) {
            c_fail(OVLSTORE_ETIMEOUT);
            config_agent_abort_session();
        }
        return;
    }
    if (s_c_st == C_FINAL) {
        int jr = ovlstore_sd_job_result(&s_sd);
        if (jr == OVLSD_BUSY) {
            return;
        }
        s_c_slot = ovlstore_sd_commit_slot(&s_sd);
        s_c_rc = (jr == OVLSD_OK && s_c_slot != 0) ? OVLSD_OK : ((jr < 0) ? jr : OVLSD_EIO);
        s_c_st = C_DONE;
    }
}

int overlay_store_commit_poll(char *slot)
{
    int rc;

    if (s_c_st == C_IDLE) {
        return OVLSD_EORDER;
    }
    if (s_c_st != C_DONE) {
        return OVLSD_BUSY;
    }
    rc = s_c_rc;
    if (rc == OVLSD_OK && slot != NULL) {
        *slot = s_c_slot;
    }
    s_c_st = C_IDLE;
    return rc;
}

bool overlay_store_commit_active(void)
{
    return s_c_st != C_IDLE && s_c_st != C_DONE;
}

/* ---- the `usd` verb ------------------------------------------------------------------- */

void overlay_store_usd_status(mps3_usd_t *u)
{
    ovlstore_sd_info_t in;
    ovlstore_sd_desc_t d;
    char slot = 0;
    const char *t;

    memset(u, 0, sizeof *u);
    ovlstore_sd_info(&s_sd, &in);
    u->present = in.present ? 1 : 0;
    (void)snprintf(u->state, sizeof u->state, "%s", in.state_name);
    t = overlay_store_usd_text();
    (void)snprintf(u->text, sizeof u->text, "%s", t ? t : "?");
    if (in.card_mb != 0u) {
        u->have_card_mb = 1;
        u->card_mb = in.card_mb;
    }
    if (ovlstore_sd_header_default(&s_sd, &d, &slot) == OVLSD_OK) {
        u->have_default = 1;
        u->def_rm_id = d.rm_id;
        u->def_static_id = d.static_id;
        u->def_slot = slot;
    }
    (void)snprintf(u->boot, sizeof u->boot, "%s", overlay_store_boot_text());
}

int overlay_store_usd_action(const char *action, const char *confirm)
{
    int rc;

    if (action == NULL) {
        return OVLSD_EARG;
    }
    if (strcmp(action, "rescan") == 0) {
        if (s_act_running || overlay_store_commit_active()) {
            return OVLSD_EBUSY;
        }
        rc = ovlstore_sd_rescan(&s_sd);
        return (rc == OVLSD_OK) ? OVLSD_OK : rc;
    }
    if (strcmp(action, "format") == 0) {
        /* The confirmation is judged FIRST: a wrong one is always "confirm
         * required", whatever else is going on. Exact strings only. */
        if (confirm == NULL ||
            (strcmp(confirm, OVLSD_CONFIRM_FORMAT) != 0 && strcmp(confirm, OVLSD_CONFIRM_WIPE) != 0)) {
            return OVLSD_ECONFIRM;
        }
    } else if (strcmp(action, "clear") != 0) {
        return OVLSD_EARG;
    }
    if (s_act_running || overlay_store_commit_active() || boot_open() || !swap_fsm_idle()) {
        return OVLSD_EBUSY;
    }
    rc = (action[0] == 'f') ? ovlstore_sd_format(&s_sd, confirm) : ovlstore_sd_clear(&s_sd);
    if (rc != OVLSD_OK) {
        return rc;
    }
    s_act_running = 1;
    return OVLSD_BUSY;
}

bool overlay_store_action_active(void)
{
    return s_act_running != 0u;
}

int overlay_store_usd_action_poll(void)
{
    int rc;

    if (!s_act_running) {
        return OVLSD_EORDER;
    }
    rc = ovlstore_sd_job_result(&s_sd);
    if (rc == OVLSD_BUSY) {
        return OVLSD_BUSY;
    }
    if (rc == OVLSD_OK && ovlstore_sd_job(&s_sd) != OVLSD_JOB_NONE) {
        /* A format ends by re-reading the card: answer with the state THAT
         * finds ("empty"), not the transient "init" in between. */
        return OVLSD_BUSY;
    }
    s_act_running = 0;
    return rc;
}

/* ---- the swap source ---------------------------------------------------------------------- */

int overlay_store_src_default(ovlstore_sd_desc_t *out)
{
    ovlstore_sd_info_t in;
    ovlstore_sd_info(&s_sd, &in);
    if (in.skip) {
        return OVLSD_ESKIPPED;
    }
    return ovlstore_sd_default(&s_sd, out, NULL);
}

int overlay_store_src_begin(ovlstore_sd_which_t which)
{
    int rc = ovlstore_sd_stream_begin(&s_sd, which);
    if (rc < 0) {
        s_src_err = rc;
    }
    return rc;
}

int overlay_store_src_next(uint32_t max_bytes, const uint8_t **p, uint32_t *n)
{
    int rc = ovlstore_sd_stream_next(&s_sd, max_bytes, p, n);
    if (rc < 0) {
        s_src_err = rc;
    }
    return rc;
}

void overlay_store_src_abort(void)
{
    ovlstore_sd_stream_abort(&s_sd);
}

/* ---- lifecycle ------------------------------------------------------------------------------ */

static void glue_reset(void)
{
    s_act_running = 0;
    s_c_st = C_IDLE;
    s_c_rc = OVLSD_OK;
    s_c_slot = 0;
    memset(&s_c_desc, 0, sizeof s_c_desc);
    s_src_err = 0;
    s_b_st = B_UNARMED;
    s_b_word = 0u;
    s_b_t0 = 0u;
    s_b_swaps = 0u;
    s_b_text[0] = '\0';
    s_text_sig = 0xFFFFFFFFu;
    s_phase_last = 0xFFFFFFFFu;
    s_cc++;
}

void overlay_store_init(void)
{
    ovlstore_sd_cfg_t cfg;

    memset(&cfg, 0, sizeof cfg);
    cfg.live_static_id = mps3_shell_static_id();
    cfg.allow_wipe = false;       /* the engine opts in (bare metal only) */
#ifdef MPS3_HAS_CLCD
    cfg.rm_name = rm_name_cb;
#endif
    memset(&s_bd, 0, sizeof s_bd);
    if (ovlstore_engine_bind(&s_bd, &cfg) != 0 || s_bd.ops == NULL) {
        s_bd.ops = &k_nohw_ops;
        s_bd.ctx = NULL;
        s_bd.max_blocks = 1u;
        cfg.allow_wipe = false;
    }
    ovlstore_sd_init(&s_sd, &s_bd, &cfg);
    config_agent_set_commit_sink(NULL);
    glue_reset();
    boot_restore();
}

void overlay_store_service(void)
{
    uint32_t now = now_ms();

    ovlstore_sd_poll(&s_sd, now);
    commit_watch(now);
    boot_step(now);
    text_watch();
    phase_watch();
}

bool overlay_store_busy(void)
{
    switch (ovlstore_sd_job(&s_sd)) {
    case OVLSD_JOB_NONE:
        return false;
    case OVLSD_JOB_COMMIT:
        /* While the pair is still arriving the job waits on the NETWORK (that
         * is work harnessd already sees); only the flush + read-back after
         * commit_end() is the store's own to drive. */
        return s_c_st == C_FINAL;
    default:
        return true;   /* probe / verify / stream / format / clear */
    }
}

#ifdef OVLSTORE_GLUE_TEST_HOOKS
ovlstore_sd_t *overlay_store_test_store(void)
{
    return &s_sd;
}

void overlay_store_test_reset(void)
{
    glue_reset();
    boot_restore();
}
#endif
