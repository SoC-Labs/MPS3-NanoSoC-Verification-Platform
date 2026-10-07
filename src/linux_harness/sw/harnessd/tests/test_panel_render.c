/*
 * test_panel_render.c -- the REAL panel_linux.c (the `hello` / `panel` providers
 * and the clcd seam providers, net-protocol.md v0.17) over the REAL
 * presence_core.c and net_proto.c, with test doubles for the clcd side (lane
 * PANEL-CLCD's accessors, declared by clcd.h; clcd.c is not linked) so the frame,
 * the tap ring and the KVM owner are whatever each check needs:
 *
 *   1. THE BUDGET: a frame half with the WORST rows the renderer can commit (every
 *      cell a '"' or '\\', and glyph-dense rows) fits one reply through the REAL
 *      codec (<= MPS3_CTRL_RESP_MAX incl. the newline) -- or is refused whole with
 *      code "too_large", never truncated; the worst `panel` state (4 sessions, 8
 *      taps, escape-heavy banner/card) and `hello` reply fit too;
 *   2. THE SHAPES: frame "a" = 8 rows, "b" = 7, roles 40 per row, glyphs as
 *      \u0080..\u0086; `frame:true` refused (a whole frame does not fit);
 *   3. THE TAPS: the clcd ring -> [{seq,k:"tap",on,ms_ago}] oldest first, ms_ago
 *      from mps3_sys_now_ms(); the hello's panel.seq = the newest seq;
 *   4. THE PAGE: set while the harness owns the panel; refused ("dut owns the
 *      panel", code "held") while the DUT owns it OR a flip is in flight;
 *   5. THE SEAMS: the badge / hm row / request overlay with their roles, glyphs
 *      only in the aligned theme -- harnessd's DEFAULT: this test sets no
 *      --panel-theme (test_panel_e2e.py runs both themes, `--panel-theme today` too).
 */
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../../../../../firmware/common/net_proto.h"
#include "../../../../../firmware/coordinator/coordinator.h"
#include "../../../../../firmware/touch/touch.h"
#include "../harnessd.h"
#include "../../../../../firmware/clcd/clcd.h"

static int g_fail, g_n;
#define CHECK(c, ...) do { g_n++; if (!(c)) { g_fail++; printf("  FAIL %s:%d: %s -- ", \
    __FILE__, __LINE__, #c); printf(__VA_ARGS__); printf("\n"); } } while (0)

/* ---- harnessd doubles ------------------------------------------------------- */
harnessd_cfg_t g_hd;
static uint64_t g_now_us = 5000000u;
uint64_t harnessd_now_us64(void) { return g_now_us; }
uint32_t mps3_sys_now_ms(void) { return (uint32_t)(g_now_us / 1000u); }
void harnessd_log(const char *fmt, ...) { (void)fmt; }
const char *harnessd_board_label(void) { return "MPS3-02"; }

/* ---- firmware doubles ----------------------------------------------------------- */
static uint32_t g_kvm;
uint32_t clcd_kvm_status(void) { return g_kvm; }
uint16_t touch_chip_id(void) { return 0x0811u; }
void touch_get_calibration(touch_calib_t *out) { memset(out, 0, sizeof(*out)); out->shift = 12; }
const char *touch_calib_invalid_reason(const touch_calib_t *cal) { return cal->ax ? 0 : "singular"; }
static const char *g_card = "nanosoc [A]";
const char *overlay_store_usd_text(void) { return g_card; }
static clcd_page_t g_page = CLCD_PAGE_STATUS;
void clcd_page_set(clcd_page_t p) { g_page = p; }
clcd_page_t clcd_page_get(void) { return g_page; }

/* ---- PANEL-CLCD doubles (its contract, clcd_seams.h) -------------------------- */
static const uint16_t k_pal[CLCD_ROLE_COUNT][2];
const mps3_clcd_theme_t clcd_theme_today = { k_pal, 0u, "today" };
const mps3_clcd_theme_t clcd_theme_aligned = { k_pal, CLCD_THEME_ALIGNED, "aligned" };
const char *clcd_role_name(unsigned r) { return r < CLCD_ROLE_COUNT ? "role" : ""; }
void clcd_line_clear(clcd_line_t *l)
{
    memset(l->text, ' ', CLCD_COLS);
    l->text[CLCD_COLS] = '\0';
    memset(l->role, CLCD_ROLE_TEXT, CLCD_COLS);
}
unsigned clcd_line_put(clcd_line_t *l, unsigned col, const char *s, unsigned role)
{
    for (; *s && col < CLCD_COLS; s++, col++) {
        l->text[col] = *s;
        l->role[col] = (uint8_t)role;
    }
    return col;
}
void clcd_line_right(clcd_line_t *l, const char *s, unsigned role, unsigned pad)
{
    (void)clcd_line_put(l, CLCD_COLS - pad - (unsigned)strlen(s), s, role);
}

static clcd_event_t g_ev[CLCD_EVENT_RING];
static unsigned g_nev;
uint32_t clcd_event_seq(void) { return g_nev ? g_ev[g_nev - 1].seq : 0u; }
unsigned clcd_events_since(uint32_t after, clcd_event_t *out, unsigned max)
{
    unsigned n = 0;
    for (unsigned i = 0; i < g_nev && n < max; i++) {
        if (g_ev[i].seq > after) {
            out[n++] = g_ev[i];
        }
    }
    return n;
}
const char *clcd_event_on_name(unsigned on)
{
    return on == CLCD_ON_NAV ? "nav" : on == CLCD_ON_IDENTIFY ? "identify" :
           on == CLCD_ON_REQUEST ? "request" : "";
}
const char *clcd_page_name(unsigned p) { return p == 0 ? "status" : p == 1 ? "apps" : ""; }
int clcd_page_by_name(const char *n)
{
    return strcmp(n, "status") == 0 ? 0 : strcmp(n, "apps") == 0 ? 1 : -1;
}

static char g_cells[CLCD_ROWS][CLCD_COLS + 1];
static char g_codes[CLCD_ROWS * CLCD_COLS + 1];
unsigned clcd_frame_rows(unsigned first, unsigned count, char (*rows)[CLCD_COLS + 1], char *roles)
{
    if (first >= CLCD_ROWS) {
        return 0u;
    }
    if (count > CLCD_ROWS - first) {
        count = CLCD_ROWS - first;
    }
    for (unsigned i = 0; i < count; i++) {
        if (rows) {
            memcpy(rows[i], g_cells[first + i], CLCD_COLS + 1u);
        }
    }
    if (roles) {
        memcpy(roles, g_codes + first * CLCD_COLS, count * CLCD_COLS);
        roles[count * CLCD_COLS] = '\0';
    }
    return count;
}
unsigned clcd_fmt_row_json(char *out, unsigned cap, const char *cells, unsigned n)
{
    unsigned o = 0;
    for (unsigned i = 0; i < n; i++) {
        unsigned char c = (unsigned char)cells[i];
        char t[8];
        unsigned w = 1;
        if (c == '"' || c == '\\') {
            t[0] = '\\';
            t[1] = (char)c;
            w = 2;
        } else if (c >= 0x80u && c <= 0x86u) {
            w = (unsigned)snprintf(t, sizeof(t), "\\u%04x", c);
        } else {
            t[0] = (c >= 0x20u && c <= 0x7Eu) ? (char)c : ' ';
        }
        if (o + w + 1u > cap) {
            out[0] = '\0';
            return 0;
        }
        memcpy(out + o, t, w);
        o += w;
    }
    out[o] = '\0';
    return o;
}
static char g_banner[CLCD_COLS + 1];
static int g_relinq;
void clcd_panel_state(clcd_panel_state_t *out)
{
    memset(out, 0, sizeof(*out));
    out->page = (uint8_t)g_page;
    out->relinquished = (uint8_t)g_relinq;
    snprintf(out->banner_text, sizeof(out->banner_text), "%s", g_banner);
    out->theme = mps3_clcd_palette()->name;
    out->prog_pct = 255u;
}

/* ---- helpers ------------------------------------------------------------------ */
static char g_line[MPS3_CTRL_RESP_MAX];

/* One request through the REAL coordinator shape: provider body -> the REAL
 * encoder. Returns the encoded length (incl. the '\n'), or -1. */
static int call(int hello, const char *json, char *code_out)
{
    mps3_ctrl_request_t req;
    mps3_ctrl_response_t resp;
    char code[24] = "";
    memset(&req, 0, sizeof(req));
    memset(&resp, 0, sizeof(resp));
    req.line = json;
    req.line_len = (int)strlen(json);
    req.op = hello ? MPS3_OP_HELLO : MPS3_OP_PANEL;
    const char *body = 0;
    const char *why = hello ? mps3_hello_op(&req, &body, code, sizeof(code))
                            : mps3_panel_op(&req, &body, code, sizeof(code));
    resp.op = req.op;
    if (why) {
        resp.ok = 0;
        snprintf(resp.err, sizeof(resp.err), "%s", why);
        snprintf(resp.code, sizeof(resp.code), "%s", code);
    } else {
        resp.ok = 1;
        resp.body = body;
    }
    if (code_out) {
        strcpy(code_out, code);
    }
    return mps3_ctrl_encode_response(&resp, g_line, (int)sizeof(g_line));
}

static void fill_rows(char c)
{
    for (unsigned r = 0; r < CLCD_ROWS; r++) {
        memset(g_cells[r], c, CLCD_COLS);
        g_cells[r][CLCD_COLS] = '\0';
    }
    memset(g_codes, 'a', CLCD_ROWS * CLCD_COLS);
    g_codes[CLCD_ROWS * CLCD_COLS] = '\0';
}

static void hello_n(int i, const char *role)
{
    /* sid: 7 JSON-escaped quotes + a digit; who: 20 escaped backslashes -- the
     * worst each field can cost on the way out (every character escaped) */
    char j[256], sid[32] = "", who[64] = "", code[24];
    for (int k = 0; k < 7; k++) {
        strcat(sid, "\\\"");
    }
    snprintf(sid + strlen(sid), sizeof(sid) - strlen(sid), "%d", i);
    for (int k = 0; k < 20; k++) {
        strcat(who, "\\\\");
    }
    snprintf(j, sizeof(j), "{\"op\":\"hello\",\"v\":1,\"sid\":\"%s\",\"who\":\"%s\","
             "\"role\":\"%s\",\"ttl\":300}", sid, who, role);
    int n = call(1, j, code);
    CHECK(n > 0 && strstr(g_line, "\"ok\":true"), "hello %d: %s", i, g_line);
}

/* ---- 1. the budget + 2. the shapes ------------------------------------------------ */
static void t_budget_shapes(void)
{
    char code[24];
    int n;
    fill_rows('"');                         /* every cell escapes to 2 bytes */
    n = call(0, "{\"op\":\"panel\",\"frame\":\"a\"}", code);
    CHECK(n > 0 && n <= MPS3_CTRL_RESP_MAX - 1, "worst escapes, half a: %d B", n);
    printf("  budget: frame half a, every cell '\"': %d B of %d\n", n, MPS3_CTRL_RESP_MAX - 1);
    CHECK(strstr(g_line, "\"frame\":\"a\",\"theme\":\"aligned\",\"rows\":[") != 0, "%.80s", g_line);
    fill_rows('\\');
    n = call(0, "{\"op\":\"panel\",\"frame\":\"b\"}", code);
    CHECK(n > 0 && n <= MPS3_CTRL_RESP_MAX - 1, "worst escapes, half b: %d B", n);

    /* glyph-dense rows: 6 bytes a cell. A realistic frame has a handful of glyphs
     * a row; find the density at which a half stops fitting, and prove the refusal */
    unsigned fits = 0;
    for (unsigned g = 0; g <= CLCD_COLS; g++) {
        fill_rows('x');
        for (unsigned r = 0; r < CLCD_ROWS; r++) {
            memset(g_cells[r], (char)0x83, g);
        }
        n = call(0, "{\"op\":\"panel\",\"frame\":\"a\"}", code);
        if (n > 0 && strstr(g_line, "\"ok\":true")) {
            fits = g;
            CHECK(n <= MPS3_CTRL_RESP_MAX - 1, "fits = within the line");
        } else {
            CHECK(strcmp(code, "too_large") == 0 && strstr(g_line, "\"ok\":false"),
                  "refused whole, never truncated: %s", g_line);
            break;
        }
    }
    printf("  budget: a half fits with up to %u glyphs in EVERY row\n", fits);
    CHECK(fits >= 8u, "headroom: %u glyphs a row", fits);

    /* shapes */
    fill_rows('x');
    memcpy(g_cells[0], "\x80" "ok", 3);
    for (unsigned i = 0; i < CLCD_ROWS * CLCD_COLS; i++) {
        g_codes[i] = (char)('a' + i % 21u);
    }
    n = call(0, "{\"op\":\"panel\",\"frame\":\"a\"}", code);
    CHECK(strstr(g_line, "\"rows\":[\"\\u0080okxxx") != 0, "a glyph is \\u0080: %.90s", g_line);
    const char *ro = strstr(g_line, "\"roles\":\"");
    CHECK(ro && strlen(ro) == strlen("\"roles\":\"") + 320u + strlen("\"}\n"), "roles 8x40");
    int commas = 0;
    for (const char *p = strstr(g_line, "\"rows\":["); p && *p && *p != ']'; p++) {
        commas += *p == ',';
    }
    CHECK(commas == 7, "8 rows in a (%d commas)", commas);
    (void)call(0, "{\"op\":\"panel\",\"frame\":\"b\"}", code);
    ro = strstr(g_line, "\"roles\":\"");
    CHECK(ro && strlen(ro) == strlen("\"roles\":\"") + 280u + strlen("\"}\n"), "roles 7x40");
    CHECK(strncmp(ro + 9, "fghijklmnopqrstuabcde", 21) == 0, "b starts at cell 320 (320 %% 21 = 5): %.21s", ro + 9);
    static const char *const bad[] = {
        "{\"op\":\"panel\",\"frame\":true}", "{\"op\":\"panel\",\"frame\":\"c\"}",
        "{\"op\":\"panel\",\"frame\":1}",
    };
    for (unsigned i = 0; i < 3u; i++) {
        (void)call(0, bad[i], code);
        CHECK(strcmp(code, "invalid") == 0 && strstr(g_line, "invalid frame"), "%s -> %s", bad[i], g_line);
    }
    n = call(0, "{\"op\":\"panel\",\"frame\":false}", code);
    CHECK(strstr(g_line, "\"touch\":") != 0, "frame:false = the state");
}

/* ---- the worst state / hello --------------------------------------------------- */
static void t_worst_state(void)
{
    char code[24];
    g_now_us = 4000000000000ull;             /* ms_ago near 32 bits */
    for (int i = 0; i < 4; i++) {
        hello_n(i, "holder");
    }
    g_nev = 0;
    for (unsigned i = 0; i < CLCD_EVENT_RING; i++) {
        g_ev[g_nev++] = (clcd_event_t){ .seq = 4000000000u + i, .t_ms = 0u, .kind = CLCD_EV_TAP,
                                         .on = CLCD_ON_IDENTIFY };
    }
    memset(g_banner, '"', CLCD_COLS);
    g_banner[CLCD_COLS] = '\0';
    g_card = "\"\"\"\"\"\"\"\"\"\"\"\"\"\"\"\"";
    int n = call(0, "{\"op\":\"panel\"}", code);
    CHECK(strstr(g_line, "\"age_s\":0},{\"sid\"") != 0, "4 live sessions in it");
    CHECK(n > 0 && strstr(g_line, "\"ok\":true") && n <= MPS3_CTRL_RESP_MAX - 1,
          "the worst panel state fits: %d B (%s)", n, n < 0 ? "" : g_line + (n > 60 ? n - 60 : 0));
    printf("  budget: worst panel state %d B of %d\n", n, MPS3_CTRL_RESP_MAX - 1);
    n = call(1, "{\"op\":\"hello\",\"sid\":\"x\",\"who\":\"y\"}", code);
    CHECK(n > 0 && strstr(g_line, "\"ok\":true") && n <= MPS3_CTRL_RESP_MAX - 1,
          "the worst hello reply fits: %d B", n);
    printf("  budget: worst hello reply %d B of %d\n", n, MPS3_CTRL_RESP_MAX - 1);
    g_banner[0] = '\0';
    g_card = "nanosoc [A]";
    g_nev = 0;
}

/* ---- 3. taps ------------------------------------------------------------------ */
static void t_taps(void)
{
    char code[24];
    g_now_us = 900000000u;                   /* 900 s */
    g_nev = 0;
    g_ev[g_nev++] = (clcd_event_t){ .seq = 5, .t_ms = 897700u, .kind = CLCD_EV_TAP, .on = CLCD_ON_REQUEST };
    g_ev[g_nev++] = (clcd_event_t){ .seq = 6, .t_ms = 899000u, .kind = CLCD_EV_TAP, .on = CLCD_ON_NAV };
    (void)call(1, "{\"op\":\"hello\",\"v\":1,\"sid\":\"t1\",\"who\":\"d@h\",\"role\":\"watch\"}", code);
    CHECK(strstr(g_line, "\"seq\":6},\"events\":[{\"seq\":5,\"k\":\"tap\",\"on\":\"request\","
                 "\"ms_ago\":2300},{\"seq\":6,\"k\":\"tap\",\"on\":\"nav\",\"ms_ago\":1000}]}") != 0,
          "the ring, oldest first: %s", g_line);
    (void)call(0, "{\"op\":\"panel\"}", code);
    CHECK(strstr(g_line, "\"seq\":6,\"events\":[{\"seq\":5,") != 0, "panel too: %s", g_line);
    g_nev = 0;
}

/* ---- 4. the page ---------------------------------------------------------------- */
static void t_page(void)
{
    char code[24];
    g_kvm = 0;
    (void)call(0, "{\"op\":\"panel\",\"page\":\"apps\"}", code);
    CHECK(strcmp(g_line, "{\"ok\":true,\"op\":\"panel\",\"page\":\"apps\"}\n") == 0 &&
          g_page == CLCD_PAGE_APPS, "%s", g_line);
    g_kvm = CLCDKVM_STATUS_OWNER;            /* the DUT owns the panel */
    (void)call(0, "{\"op\":\"panel\",\"page\":\"status\"}", code);
    CHECK(strcmp(g_line, "{\"ok\":false,\"err\":\"dut owns the panel\",\"code\":\"held\"}\n") == 0 &&
          g_page == CLCD_PAGE_APPS, "refused, page unchanged: %s", g_line);
    g_kvm = CLCDKVM_STATUS_SWITCH_PENDING;   /* a flip in flight */
    (void)call(0, "{\"op\":\"panel\",\"page\":\"status\"}", code);
    CHECK(strcmp(code, "held") == 0, "refused mid-flip");
    g_kvm = 0;
    g_relinq = 1;
    (void)call(0, "{\"op\":\"panel\",\"page\":\"status\"}", code);
    CHECK(strcmp(code, "held") == 0, "refused while relinquished");
    g_relinq = 0;
    (void)call(0, "{\"op\":\"panel\",\"page\":\"status\"}", code);
    CHECK(g_page == CLCD_PAGE_STATUS && strstr(g_line, "\"ok\":true"), "back: %s", g_line);
    (void)call(0, "{\"op\":\"panel\",\"page\":\"menu\"}", code);
    CHECK(strcmp(code, "invalid") == 0 && strstr(g_line, "invalid page: status or apps"), "%s", g_line);
    (void)call(0, "{\"op\":\"panel\",\"page\":\"apps\",\"frame\":\"a\"}", code);
    CHECK(strcmp(code, "invalid") == 0 && g_page == CLCD_PAGE_STATUS, "page + frame refused");
    (void)call(0, "{\"op\":\"panel\",\"page\":7}", code);
    CHECK(strcmp(code, "invalid") == 0, "page not a string");
    /* the owner and pending in the state */
    g_kvm = CLCDKVM_STATUS_OWNER | CLCDKVM_STATUS_SWITCH_PENDING;
    (void)call(0, "{\"op\":\"panel\"}", code);
    CHECK(strstr(g_line, "\"owner\":\"dut\",\"pending\":true") != 0, "%s", g_line);
    g_kvm = 0;
}

/* ---- 5. the seams ---------------------------------------------------------------- */
static void t_seams(void)
{
    char code[24];
    mps3_clcd_badge_t b;
    clcd_line_t l;
    mps3_clcd_overlay_t ov;
    g_now_us += 400000000u;                  /* everyone before expires */
    CHECK(mps3_clcd_palette() == &clcd_theme_aligned, "no --panel-theme: the aligned theme");
    (void)call(1, "{\"op\":\"hello\",\"v\":1,\"sid\":\"s1\",\"who\":\"alice@lab-pc01\","
                  "\"role\":\"holder\",\"lease\":{\"by\":\"alice\",\"left\":4332,\"q\":1,"
                  "\"req\":\"bob\",\"rl\":103}}", code);
    memset(&b, 0, sizeof(b));
    CHECK(mps3_clcd_title_right(&b) == 1 && strcmp(b.text, "\x83 alice 1h12m, 1 waiting") == 0 &&
          b.role == CLCD_ROLE_TITLE_HELD, "badge '%s' role %u", b.text, b.role);
    CHECK(mps3_clcd_session_row(&l) == 1 && strncmp(l.text, "hm     \x84" "alice@lab-pc01   ", 25) == 0 &&
          l.role[0] == CLCD_ROLE_LABEL && l.role[7] == CLCD_ROLE_VALUE &&
          l.role[30] == CLCD_ROLE_TEXT, "hm row '%s'", l.text);
    memset(&ov, 0, sizeof(ov));
    CHECK(mps3_clcd_overlay(&ov) == 1 && ov.role == CLCD_ROLE_BANNER_HELD &&
          ov.on == CLCD_ON_REQUEST && strcmp(ov.line[0], "bob wants this board") == 0,
          "overlay '%s'", ov.line[0]);
}

int main(void)
{
    g_hd.panel_theme = 0;                    /* no --panel-theme: the default, aligned (t_seams) */
    harnessd_panel_init();
    t_budget_shapes();
    t_worst_state();
    t_taps();
    t_page();
    t_seams();
    printf("panel_render: %d checks, %d failures\n", g_n, g_fail);
    return g_fail ? 1 : 0;
}
