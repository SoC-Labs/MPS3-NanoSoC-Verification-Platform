/*
 * panel_linux.c -- presence and the front panel inside mps3-harnessd
 * (net-protocol.md v0.17 "Presence and the panel"; the Harness Manager's R1 and
 * the harness half of R2, harness-manager docs/design/CLCD_ALIGNMENT.md §2, §5.3).
 *
 *   VERBS    `hello` (mps3_hello_op): the session table (presence_core.c) takes
 *            the hello; the reply is the live count, the panel's state and the tap
 *            ring. `panel` (mps3_panel_op): the state (+ the live sessions and
 *            touch); a frame HALF ("a" = rows 0-7, "b" = rows 8-14, each with its
 *            per-cell role codes); a page change (claim-locked in coordinator.c
 *            before this runs; refused while the DUT owns the panel).
 *   SEAMS    the STRONG providers of clcd.h's panel seams: row 0's lease badge
 *            (mps3_clcd_title_right), row 11's hm row (mps3_clcd_session_row), the
 *            lease-request banner on rows 10-12 (mps3_clcd_overlay; a tap on it is
 *            an event "request"), and the theme (mps3_clcd_palette: the Harness
 *            Manager's tokens, words and glyphs by default, decision D3 a -- the
 *            LCD mirror's e2e oracle reads them; `--panel-theme today` = today's
 *            white/black/red pixels, the bare-metal look).
 *            All four are pulled by clcd's reformat (<= 4 Hz), O(4), no I/O.
 *   FEATURE  version.features "presence" and "panel" (identity_linux.c's engine
 *            names), on a build with the panel only: the ctrl-echo twin has none
 *            and declines both verbs (the weak coordinator defaults).
 *
 * Nothing here blocks or does I/O on the superloop: every reply is rendered from
 * memory (the table, clcd's committed frame and ring, one KVM STATUS read) into
 * one static body the codec wraps.
 */
#define _GNU_SOURCE
#include <inttypes.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "../../../../firmware/common/net_proto.h"
#include "../../../../firmware/common/timebase.h"
#include "../../../../firmware/coordinator/coordinator.h"
#include "harnessd.h"
#include "presence_core.h"

#ifdef MPS3_HAL_MOCK
unsigned g_panel_mock_speed = 1u;   /* MOCK tests only: the presence clock's rate */
int      g_panel_negctl_no_ttl;     /* MOCK tests only: the TTL rule's negative control */
#endif

#ifdef MPS3_HAS_CLCD
#include "../../../../firmware/clcd_kvm/clcd_kvm.h"
#include "../../../../firmware/touch/touch.h"
#include "../../../../firmware/clcd/clcd.h"

/* The codec wraps a body in {"ok":true,"op":"panel", ... }\n: the body may use
 * what is left of MPS3_CTRL_RESP_MAX (its NUL included). */
#define PANEL_WRAP     ((unsigned)sizeof("{\"ok\":true,\"op\":\"panel\",}\n") - 1u)
#define PANEL_BODY_MAX (MPS3_CTRL_RESP_MAX - 1u - PANEL_WRAP)
#define FRAME_HALF_A   8u      /* rows 0-7; "b" is rows 8-14 */

static pres_table_t s_tab;
static uint64_t     s_t0_us;
static int          s_init;
static const mps3_clcd_theme_t *s_theme;

static void init_once(void)
{
    if (s_init) {
        return;
    }
    s_init = 1;
    pres_init(&s_tab);
    s_t0_us = harnessd_now_us64();
    /* The Harness Manager's look unless asked for today's (lane PANEL-FINISH): the
     * LCD-mirror e2e oracle reads HM's palette + glyphs 0x80-0x86 now
     * (tests/lcdmirror_client.py ocr_cells). Bare metal keeps today's pixels: it
     * links no provider, so clcd.c's weak mps3_clcd_palette() answers. */
    s_theme = (g_hd.panel_theme && strcmp(g_hd.panel_theme, "today") == 0)
                  ? &clcd_theme_today : &clcd_theme_aligned;
#ifdef MPS3_HAL_MOCK
    s_tab.negctl_no_ttl = g_panel_negctl_no_ttl;
#endif
}

/* The presence clock: harnessd's monotonic ms (MOCK tests may run it faster). */
static uint64_t pres_now(void)
{
    init_once();
    uint64_t el = harnessd_now_us64() - s_t0_us;
#ifdef MPS3_HAL_MOCK
    el *= (g_panel_mock_speed ? g_panel_mock_speed : 1u);
#endif
    return 1000000u + el / 1000u;   /* never 0: a fresh table's commit_ms is 0 */
}

static int glyphs(void)
{
    init_once();
    return (s_theme->flags & CLCD_THEME_ALIGNED) != 0u;
}

/* ---- the clcd seams (strong) ------------------------------------------------ */

const mps3_clcd_theme_t *mps3_clcd_palette(void)
{
    init_once();
    return s_theme;
}

int mps3_clcd_title_right(mps3_clcd_badge_t *out)
{
    int warn = 0;
    uint64_t now = pres_now();
    (void)pres_commit(&s_tab, now);
    if (!pres_badge(&s_tab, now, glyphs() ? CLCD_GS_HELD : "", glyphs() ? CLCD_GS_WARN : "",
                    out->text, sizeof(out->text), &warn)) {
        return 0;
    }
    out->role = (uint8_t)(warn ? CLCD_ROLE_TITLE_WARN : CLCD_ROLE_TITLE_HELD);
    return 1;
}

int mps3_clcd_session_row(clcd_line_t *out)
{
    pres_seg_t seg[3];
    uint64_t now = pres_now();
    (void)pres_commit(&s_tab, now);
    int n = pres_hm_row(&s_tab, now, glyphs() ? CLCD_GS_USER : "", seg);
    clcd_line_clear(out);
    for (int i = 0; i < n; i++) {
        (void)clcd_line_put(out, seg[i].col, seg[i].text,
                            seg[i].value ? CLCD_ROLE_VALUE : CLCD_ROLE_LABEL);
    }
    return 1;
}

int mps3_clcd_overlay(mps3_clcd_overlay_t *out)
{
    uint64_t now = pres_now();
    (void)pres_commit(&s_tab, now);
    if (!pres_request(&s_tab, now, out->line)) {
        return 0;
    }
    out->role = (uint8_t)CLCD_ROLE_BANNER_HELD;
    out->on = (uint8_t)CLCD_ON_REQUEST;
    return 1;
}

/* ---- rendering ---------------------------------------------------------------- */

typedef struct {
    char    *p;
    unsigned cap, n;
    int      bad;
} out_t;

static void put(out_t *o, const char *fmt, ...) __attribute__((format(printf, 2, 3)));
static void put(out_t *o, const char *fmt, ...)
{
    va_list ap;
    if (o->bad) {
        return;
    }
    va_start(ap, fmt);
    int w = vsnprintf(o->p + o->n, o->cap - o->n, fmt, ap);
    va_end(ap);
    if (w < 0 || (unsigned)w >= o->cap - o->n) {
        o->bad = 1;
        return;
    }
    o->n += (unsigned)w;
}

static void put_str(out_t *o, const char *s)
{
    char esc[2 * CLCD_COLS + 2];
    char clean[CLCD_COLS + 1];
    unsigned i = 0;
    for (; s && s[i] && i < CLCD_COLS; i++) {
        unsigned char c = (unsigned char)s[i];
        clean[i] = (c >= 0x20u && c <= 0x7Eu) ? (char)c : '?';
    }
    clean[i] = '\0';
    (void)pres_json_str(esc, sizeof(esc), clean);
    put(o, "\"%s\"", esc);
}

/* "page":..,"owner":..,"pending":..,"banner":..,"card":..  (both replies) */
static void panel_facts(out_t *o, const clcd_panel_state_t *ps)
{
    uint32_t st = clcd_kvm_status();
    const char *page = clcd_page_name(ps->page);
    put(o, "\"page\":\"%s\",\"owner\":\"%s\",\"pending\":%s,\"banner\":", page[0] ? page : "status",
        clcd_kvm_owner_is_dut(st) ? "dut" : "harness", clcd_kvm_switch_pending(st) ? "true" : "false");
    put_str(o, ps->banner_text);
    put(o, ",\"card\":");
    put_str(o, overlay_store_usd_text());
}

static void events(out_t *o)
{
    clcd_event_t ev[CLCD_EVENT_RING];
    unsigned n = clcd_events_since(0u, ev, CLCD_EVENT_RING);
    uint32_t now = mps3_sys_now_ms();
    put(o, "[");
    for (unsigned i = 0; i < n; i++) {
        const char *on = clcd_event_on_name(ev[i].on);
        put(o, "%s{\"seq\":%" PRIu32 ",\"k\":\"tap\",\"on\":\"%s\",\"ms_ago\":%" PRIu32 "}",
            i ? "," : "", ev[i].seq, on, (uint32_t)(now - ev[i].t_ms));
    }
    put(o, "]");
}

static void touch_facts(out_t *o)
{
    int present = 0, cal = 0;
#ifdef MPS3_HAS_TOUCH
    touch_calib_t c;
    present = touch_chip_id() == 0x0811u;
    touch_get_calibration(&c);
    cal = touch_calib_invalid_reason(&c) == 0;
#endif
    put(o, "\"touch\":{\"present\":%s,\"cal\":%s}", present ? "true" : "false", cal ? "true" : "false");
}

static const char *too_large(char *code, size_t cap, const char *what)
{
    snprintf(code, cap, "too_large");
    return what;
}

static const char *invalid(char *code, size_t cap, const char *msg)
{
    snprintf(code, cap, "invalid");
    return msg;
}

static char s_body[PANEL_BODY_MAX + 1u];

/* ---- hello ---------------------------------------------------------------------- */

const char *mps3_hello_op(const mps3_ctrl_request_t *req, const char **body, char *code,
                          size_t code_cap)
{
    pres_facts_t f;
    clcd_panel_state_t ps;
    int joined = 0, evicted = 0;
    const char *why = pres_parse_hello(req ? req->line : 0, req ? req->line_len : 0, &f);
    if (why) {
        return invalid(code, code_cap, why);
    }
    int live = pres_hello(&s_tab, &f, pres_now(), &joined, &evicted);
    if (joined) {
        harnessd_log("panel: hello from %s %s (%s)%s, %d live\n", f.sid, f.who,
                     pres_role_name(f.role), evicted ? ", the oldest session evicted" : "", live);
    }
    clcd_panel_state(&ps);
    out_t o = { s_body, sizeof(s_body), 0, 0 };
    put(&o, "\"sessions\":%d,\"panel\":{", live);
    panel_facts(&o, &ps);
    put(&o, ",\"seq\":%" PRIu32 "},\"events\":", clcd_event_seq());
    events(&o);
    if (o.bad) {
        return too_large(code, code_cap, "hello: the reply does not fit");
    }
    *body = s_body;
    return 0;
}

/* ---- panel ---------------------------------------------------------------------- */

static const char *frame_half(int b, const char **body, char *code, size_t code_cap)
{
    char rows[CLCD_ROWS][CLCD_COLS + 1];
    char roles[CLCD_ROWS * CLCD_COLS + 1];
    char js[CLCD_COLS * 6u + 1u];
    unsigned first = b ? FRAME_HALF_A : 0u, count = b ? CLCD_ROWS - FRAME_HALF_A : FRAME_HALF_A;
    clcd_panel_state_t ps;
    clcd_panel_state(&ps);
    unsigned got = clcd_frame_rows(first, count, rows, roles);
    out_t o = { s_body, sizeof(s_body), 0, 0 };
    put(&o, "\"frame\":\"%c\",\"theme\":\"%s\",\"rows\":[", b ? 'b' : 'a',
        ps.theme ? ps.theme : "today");
    for (unsigned i = 0; i < got; i++) {
        (void)clcd_fmt_row_json(js, sizeof(js), rows[i], CLCD_COLS);
        put(&o, "%s\"%s\"", i ? "," : "", js);
    }
    put(&o, "],\"roles\":\"%s\"", roles);
    if (o.bad || got != count) {
        return too_large(code, code_cap, "panel frame: the half does not fit a reply");
    }
    *body = s_body;
    return 0;
}

static const char *state(const char **body, char *code, size_t code_cap)
{
    char sess[640];
    clcd_panel_state_t ps;
    clcd_panel_state(&ps);
    out_t o = { s_body, sizeof(s_body), 0, 0 };
    panel_facts(&o, &ps);
    put(&o, ",");
    touch_facts(&o);
    if (pres_json_sessions(&s_tab, pres_now(), sess, sizeof(sess)) < 0) {
        o.bad = 1;
    }
    put(&o, ",\"sessions\":%s,\"seq\":%" PRIu32 ",\"events\":", sess, clcd_event_seq());
    events(&o);
    if (o.bad) {
        return too_large(code, code_cap, "panel: the reply does not fit");
    }
    *body = s_body;
    return 0;
}

const char *mps3_panel_op(const mps3_ctrl_request_t *req, const char **body, char *code,
                          size_t code_cap)
{
    static const char *const k_keys[] = { "op", "frame", "page", NULL };
    mps3_json_obj_t obj;
    char page[16], fr[4];
    int fb = 0;

    init_once();
    if (!req || !req->line ||
        mps3_json_parse_ex(req->line, req->line_len, &obj, k_keys, 0) != MPS3_JSON_OK) {
        return invalid(code, code_cap, "invalid request");
    }
    int prc = mps3_json_get_string(&obj, "page", page, (int)sizeof(page));
    int frc = mps3_json_get_string(&obj, "frame", fr, (int)sizeof(fr));
    int brc = mps3_json_get_bool(&obj, "frame", &fb);
    int has_frame = frc != MPS3_JSON_EMISSING && !(brc == MPS3_JSON_OK && fb == 0);

    if (prc != MPS3_JSON_EMISSING) {
        /* A page change (the claim lock was coordinator.c's, first). */
        if (has_frame) {
            return invalid(code, code_cap, "invalid request: page takes no frame");
        }
        int p = prc == MPS3_JSON_OK ? clcd_page_by_name(page) : -1;
        if (p < 0) {
            return invalid(code, code_cap, "invalid page: status or apps");
        }
        uint32_t st = clcd_kvm_status();
        clcd_panel_state_t ps;
        clcd_panel_state(&ps);
        if (clcd_kvm_owner_is_dut(st) || clcd_kvm_switch_pending(st) || ps.relinquished) {
            snprintf(code, code_cap, "held");
            return "dut owns the panel";
        }
        if ((unsigned)p != (unsigned)clcd_page_get()) {
            harnessd_log("panel: page -> %s (remote)\n", clcd_page_name((unsigned)p));
        }
        clcd_page_set((clcd_page_t)p);
        snprintf(s_body, sizeof(s_body), "\"page\":\"%s\"", clcd_page_name((unsigned)p));
        *body = s_body;
        return 0;
    }
    if (has_frame) {
        if (frc != MPS3_JSON_OK || (strcmp(fr, "a") != 0 && strcmp(fr, "b") != 0)) {
            return invalid(code, code_cap, "invalid frame: \"a\" (rows 0-7) then \"b\" (rows 8-14)");
        }
        return frame_half(fr[0] == 'b', body, code, code_cap);
    }
    return state(body, code, code_cap);
}

/* ---- for main_linux.c / tests ------------------------------------------------------ */

void harnessd_panel_init(void)
{
    init_once();
}

#else  /* !MPS3_HAS_CLCD: no panel, no presence (the weak coordinator defaults decline) */

void harnessd_panel_init(void) { }

#endif
