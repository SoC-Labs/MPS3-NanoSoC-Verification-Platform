/*
 * fake_overlay_store.c — see fake_overlay_store.h. Every glue symbol the
 * coordinator and swap_fsm reference, with scriptable outcomes and no device.
 */
#include <string.h>
#include "fake_overlay_store.h"

static overlay_manifest_info_t s_greybox;
static int s_grey_rc;

static int  s_c_begin_rc, s_c_final_rc;
static char s_c_slot;
static int  s_c_active;
static ovlstore_sd_desc_t s_c_desc;
static uint32_t s_c_live_rm, s_c_live_sid;

static mps3_usd_t s_status;
static int  s_a_begin_rc, s_a_final_rc, s_a_active;
static char s_a_action[MPS3_CTRL_STR_MAX], s_a_confirm[MPS3_CTRL_STR_MAX];

static ovlstore_sd_desc_t s_src_desc;
static int      s_src_rc;
static const uint8_t *s_src_bytes[2];
static uint32_t s_src_chunk, s_src_busy_every, s_src_busy_n;
static int      s_src_open;          /* 0 none, 1 + which */
static uint32_t s_src_pos;
static int32_t  s_src_fail_at[2];
static int      s_src_fail_rc[2];
static int      s_src_begins[2];
static int      s_src_aborts;
static int      s_src_last_rc;       /* what the last next() ended the stream with */
static int      s_inited;

/* The defaults apply without an explicit reset (most binaries never call one). */
static void ensure(void)
{
    if (!s_inited) {
        fake_overlay_store_reset();
    }
}

void fake_overlay_store_reset(void)
{
    s_inited = 1;
    memset(&s_greybox, 0, sizeof s_greybox);
    s_grey_rc = 0;
    s_c_begin_rc = OVLSD_ENOCARD;
    s_c_final_rc = OVLSD_OK;
    s_c_slot = 'B';
    s_c_active = 0;
    memset(&s_c_desc, 0, sizeof s_c_desc);
    s_c_live_rm = s_c_live_sid = 0u;
    memset(&s_status, 0, sizeof s_status);
    strcpy(s_status.state, "none");
    strcpy(s_status.text, "none");
    strcpy(s_status.boot, "none");
    s_a_begin_rc = OVLSD_ENOCARD;
    s_a_final_rc = OVLSD_OK;
    s_a_active = 0;
    s_a_action[0] = s_a_confirm[0] = '\0';
    memset(&s_src_desc, 0, sizeof s_src_desc);
    s_src_rc = OVLSD_ENODEFAULT;
    s_src_bytes[0] = s_src_bytes[1] = NULL;
    s_src_chunk = 1024u;
    s_src_busy_every = 0u;
    s_src_busy_n = 0u;
    s_src_open = 0;
    s_src_pos = 0u;
    s_src_fail_at[0] = s_src_fail_at[1] = -1;
    s_src_fail_rc[0] = s_src_fail_rc[1] = OVLSD_ECRC;
    s_src_begins[0] = s_src_begins[1] = 0;
    s_src_aborts = 0;
    s_src_last_rc = 0;
}

/* ---- greybox ---------------------------------------------------------------- */

void fake_overlay_store_set_greybox(const overlay_manifest_info_t *info, int rc)
{
    ensure();
    if (info) {
        s_greybox = *info;
    } else {
        memset(&s_greybox, 0, sizeof(s_greybox));
    }
    s_grey_rc = rc;
}

int overlay_store_get_greybox_clearing(overlay_manifest_info_t *out)
{
    ensure();
    if (s_grey_rc != 0) {
        return s_grey_rc;
    }
    *out = s_greybox;
    return 0;
}

/* ---- lifecycle ---------------------------------------------------------------- */

void overlay_store_init(void)
{
    ensure();
    s_c_active = 0;
    s_a_active = 0;
    s_src_open = 0;
}

void overlay_store_service(void) { ensure(); }
bool overlay_store_busy(void)    { ensure(); return s_src_open != 0; }
const char *overlay_store_boot_text(void) { ensure(); return s_status.boot; }
/* The latch word the scripted `boot` corresponds to (overlay_store.h THE BOOT
 * LATCH), so a scenario's diag usd_boot and its `usd`.boot agree. */
uint32_t overlay_store_diag_word(void)
{
    ensure();
    const char *b = s_status.boot;
    uint32_t code = 0u, why = 0u;
    if (strcmp(b, "loaded") == 0)       code = OVL_BOOT_LOADED;
    else if (strcmp(b, "skipped") == 0) code = OVL_BOOT_SKIPPED;
    else if (strcmp(b, "none") == 0)    code = OVL_BOOT_NONE;
    else if (strncmp(b, "failed:", 7) == 0) { code = OVL_BOOT_FAILED; why = OVL_BOOT_WHY_ABORTED; }
    else return 0u;                     /* "pending": not decided yet */
    return ((uint32_t)OVL_BOOT_LATCH_MAGIC << 16) | (why << 8) | code;
}

/* ---- commit ------------------------------------------------------------------- */

void fake_overlay_store_set_commit(int begin_rc, int final_rc, char slot)
{
    ensure();
    s_c_begin_rc = begin_rc;
    s_c_final_rc = final_rc;
    s_c_slot = slot;
}

const ovlstore_sd_desc_t *fake_overlay_store_last_commit_desc(void) { ensure(); return &s_c_desc; }
uint32_t fake_overlay_store_last_commit_live_rm(void)     { ensure(); return s_c_live_rm; }
uint32_t fake_overlay_store_last_commit_live_static(void) { ensure(); return s_c_live_sid; }

int overlay_store_commit_begin(const ovlstore_sd_desc_t *desc, uint32_t live_static_id,
                               uint32_t live_rm_id)
{
    ensure();
    s_c_desc = *desc;
    s_c_live_sid = live_static_id;
    s_c_live_rm = live_rm_id;
    if (s_c_begin_rc == OVLSD_OK) {
        s_c_active = 1;
    }
    return s_c_begin_rc;
}

int overlay_store_commit_poll(char *slot)
{
    ensure();
    if (!s_c_active) {
        return OVLSD_EORDER;
    }
    s_c_active = 0;
    if (s_c_final_rc == OVLSD_OK && slot) {
        *slot = s_c_slot;
    }
    return s_c_final_rc;
}

bool overlay_store_commit_active(void) { ensure(); return s_c_active != 0; }

/* ---- usd ------------------------------------------------------------------------ */

void fake_overlay_store_set_status(const mps3_usd_t *st) { ensure(); s_status = *st; }

void fake_overlay_store_set_action(int begin_rc, int final_rc)
{
    ensure();
    s_a_begin_rc = begin_rc;
    s_a_final_rc = final_rc;
}

const char *fake_overlay_store_last_action(void)  { ensure(); return s_a_action; }
const char *fake_overlay_store_last_confirm(void) { ensure(); return s_a_confirm; }

void overlay_store_usd_status(mps3_usd_t *out) { ensure(); *out = s_status; }

int overlay_store_usd_action(const char *action, const char *confirm)
{
    ensure();
    strncpy(s_a_action, action ? action : "", sizeof s_a_action - 1u);
    s_a_action[sizeof s_a_action - 1u] = '\0';
    strncpy(s_a_confirm, confirm ? confirm : "", sizeof s_a_confirm - 1u);
    s_a_confirm[sizeof s_a_confirm - 1u] = '\0';
    /* The request is judged BEFORE the card, exactly as overlay_store.c does:
     * an unknown action is bad args, a format without the exact confirmation
     * string is confirm required -- whatever the (scripted) card state. */
    if (action == NULL ||
        (strcmp(action, "format") != 0 && strcmp(action, "clear") != 0 &&
         strcmp(action, "rescan") != 0)) {
        return OVLSD_EARG;
    }
    if (strcmp(action, "format") == 0 &&
        (confirm == NULL || (strcmp(confirm, OVLSD_CONFIRM_FORMAT) != 0 &&
                             strcmp(confirm, OVLSD_CONFIRM_WIPE) != 0))) {
        return OVLSD_ECONFIRM;
    }
    if (s_a_begin_rc == OVLSD_BUSY) {
        s_a_active = 1;
    }
    return s_a_begin_rc;
}

int overlay_store_usd_action_poll(void)
{
    ensure();
    if (!s_a_active) {
        return OVLSD_EORDER;
    }
    s_a_active = 0;
    return s_a_final_rc;
}

bool overlay_store_action_active(void) { ensure(); return s_a_active != 0; }

/* ---- the swap source ---------------------------------------------------------------- */

void fake_overlay_store_set_src(const ovlstore_sd_desc_t *desc, int rc)
{
    ensure();
    if (desc) {
        s_src_desc = *desc;
    }
    s_src_rc = rc;
}

void fake_overlay_store_set_src_bytes(const uint8_t *clearing, const uint8_t *partial,
                                      uint32_t chunk, uint32_t busy_every)
{
    ensure();
    s_src_bytes[OVLSD_CLEARING] = clearing;
    s_src_bytes[OVLSD_PARTIAL] = partial;
    s_src_chunk = chunk ? chunk : 1024u;
    s_src_busy_every = busy_every;
}

void fake_overlay_store_set_src_fail(ovlstore_sd_which_t which, int32_t fail_at, int fail_rc)
{
    ensure();
    s_src_fail_at[which] = fail_at;
    s_src_fail_rc[which] = fail_rc;
}

int fake_overlay_store_src_begins(ovlstore_sd_which_t which) { ensure(); return s_src_begins[which]; }
int fake_overlay_store_src_aborts(void) { ensure(); return s_src_aborts; }
int fake_overlay_store_src_open(void)   { ensure(); return s_src_open != 0; }

int overlay_store_src_default(ovlstore_sd_desc_t *out)
{
    ensure();
    if (s_src_rc != OVLSD_OK) {
        return s_src_rc;
    }
    *out = s_src_desc;
    return OVLSD_OK;
}

int overlay_store_src_begin(ovlstore_sd_which_t which)
{
    ensure();
    if (s_src_rc != OVLSD_OK) {
        return s_src_rc;
    }
    if (s_src_open) {
        return OVLSD_EBUSY;
    }
    s_src_begins[which]++;
    s_src_open = 1 + (int)which;
    s_src_pos = 0u;
    s_src_busy_n = 0u;
    s_src_last_rc = 0;
    return OVLSD_OK;
}

int overlay_store_src_next(uint32_t max_bytes, const uint8_t **p, uint32_t *n)
{
    ensure();
    ovlstore_sd_which_t w;
    uint32_t len, take;
    static const uint8_t zeros[4096];

    *p = NULL;
    *n = 0u;
    if (!s_src_open) {
        return s_src_last_rc ? s_src_last_rc : OVLSD_EORDER;
    }
    w = (ovlstore_sd_which_t)(s_src_open - 1);
    len = (w == OVLSD_PARTIAL) ? s_src_desc.part_len : s_src_desc.clear_len;
    if (s_src_fail_at[w] >= 0 && s_src_pos >= (uint32_t)s_src_fail_at[w]) {
        s_src_open = 0;
        s_src_last_rc = s_src_fail_rc[w];
        return s_src_last_rc;
    }
    if (s_src_pos >= len) {
        s_src_open = 0;
        s_src_last_rc = OVLSD_DONE;
        return OVLSD_DONE;
    }
    if (s_src_busy_every == 0xFFFFFFFFu) {
        return OVLSD_BUSY;   /* a store that never delivers */
    }
    if (s_src_busy_every && (++s_src_busy_n % (s_src_busy_every + 1u)) != 0u) {
        return OVLSD_BUSY;
    }
    take = len - s_src_pos;
    if (take > s_src_chunk) {
        take = s_src_chunk;
    }
    if (take > max_bytes) {
        take = max_bytes;
    }
    if (take > sizeof zeros) {
        take = sizeof zeros;
    }
    if (s_src_fail_at[w] >= 0 && s_src_pos + take > (uint32_t)s_src_fail_at[w]) {
        take = (uint32_t)s_src_fail_at[w] - s_src_pos;
        if (take == 0u) {
            s_src_open = 0;
            s_src_last_rc = s_src_fail_rc[w];
            return s_src_last_rc;
        }
    }
    *p = s_src_bytes[w] ? s_src_bytes[w] + s_src_pos : zeros;
    *n = take;
    s_src_pos += take;
    return OVLSD_OK;
}

void overlay_store_src_abort(void)
{
    ensure();
    if (s_src_open) {
        s_src_aborts++;
    }
    s_src_open = 0;
}
