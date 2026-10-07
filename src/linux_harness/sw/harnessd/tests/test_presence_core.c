/*
 * test_presence_core.c -- presence (net-protocol.md v0.17, the Harness Manager's
 * R1): the REAL presence_core.c + net_proto.c's parser, on a fake clock.
 *
 *   1. THE PARSE: HM's hello (lease/job nested one level) decodes; the field
 *      rules -- printable ASCII ('?' for anything else, escapes included), the
 *      per-field clips (sid 8, who 20, name 16, app 12, users 12 cut at '@', job 8)
 *      and the clamps (ttl 30-300, left 0-86400, q 0-99, rl 0-600, p 0-100); wrong
 *      types refused; deeper nesting refused; extra keys ignored;
 *   2. THE TTL: live for ttl s after the last hello (<=), then "left Nm ago".
 *      NEGATIVE CONTROL: with the table told to ignore the TTL the same check
 *      FAILS -- so it can see a table that never expires anyone;
 *   3. EVICTION: a 5th sid pushes out the OLDEST last hello (live or not);
 *   4. ORDER: holder > owner > watch, then the most recent;
 *   5. RELATIVE TIME: a lease's left / a request's rl age on the board's clock
 *      from the hello's arrival; the badge's warn under 5 min; "not leased";
 *      no live session = no badge;
 *   6. THE REPAINT RULE: a second hello < 2 s after a commit is replied to at once
 *      but drawn only at the next commit >= 2 s on;
 *   7. THE TEXT: the hm row ("hm", who, "+N watching" / "none connected" / "left
 *      5m ago"), the badge forms that fit 24 cells, the request banner;
 *   8. THE BUDGET: the worst-case sessions JSON (4 escape-heavy sessions).
 */
#include <stdio.h>
#include <string.h>

#include "../../../../../firmware/common/net_proto.h"
#include "../presence_core.h"

static int g_fail, g_n;
#define CHECK(c, ...) do { g_n++; if (!(c)) { g_fail++; printf("  FAIL %s:%d: %s -- ", \
    __FILE__, __LINE__, #c); printf(__VA_ARGS__); printf("\n"); } } while (0)

#define S(ms) ((uint64_t)(ms))
static const uint64_t T0 = 1000000u;

static const char *parse(const char *json, pres_facts_t *f)
{
    return pres_parse_hello(json, (int)strlen(json), f);
}

static void hello(pres_table_t *t, const char *json, uint64_t now)
{
    pres_facts_t f;
    const char *why = parse(json, &f);
    CHECK(why == 0, "parse %s: %s", json, why ? why : "");
    if (!why) {
        (void)pres_hello(t, &f, now, 0, 0);
    }
}

static void mk(char *out, size_t cap, const char *sid, const char *who, const char *role,
               unsigned ttl)
{
    snprintf(out, cap, "{\"op\":\"hello\",\"v\":1,\"sid\":\"%s\",\"who\":\"%s\",\"app\":\"hm\","
             "\"role\":\"%s\",\"ttl\":%u}", sid, who, role, ttl);
}

/* ---- 1. the parse ------------------------------------------------------------ */
static void t_parse(void)
{
    pres_facts_t f;
    /* the design doc's example, verbatim (CLCD_ALIGNMENT.md §2.2) */
    const char *doc = "{\"op\":\"hello\",\"v\":1,\"sid\":\"a1b2c3d4\",\"who\":\"alice@lab-pc01\","
                      "\"app\":\"hm/0.1.0\",\"name\":\"mps3-01\",\"role\":\"holder\",\"lease\":"
                      "{\"by\":\"alice\",\"left\":4332,\"q\":1,\"req\":\"bob\",\"rl\":103},"
                      "\"job\":{\"k\":\"program\",\"p\":42},\"ttl\":90}";
    CHECK(parse(doc, &f) == 0, "the doc's hello parses");
    CHECK(strcmp(f.sid, "a1b2c3d4") == 0 && strcmp(f.who, "alice@lab-pc01") == 0 &&
          strcmp(f.app, "hm/0.1.0") == 0 && strcmp(f.name, "mps3-01") == 0, "strings");
    CHECK(f.role == PRES_ROLE_HOLDER && f.ttl_s == 90u, "role %u ttl %u", f.role, f.ttl_s);
    CHECK(f.lease.has && strcmp(f.lease.by, "alice") == 0 && f.lease.has_left &&
          f.lease.left == 4332u && f.lease.q == 1u && strcmp(f.lease.req, "bob") == 0 &&
          f.lease.has_rl && f.lease.rl == 103u, "lease");
    CHECK(f.job.has && strcmp(f.job.k, "program") == 0 && f.job.p == 42u, "job");

    /* HM's worst case (tests/unit/test_p1_presence.py WORST, 251 B), verbatim */
    const char *worst = "{\"op\":\"hello\",\"v\":1,\"sid\":\"ffffffff\",\"who\":"
                        "\"xxxxxxxxxxxxxxxxxxxx\",\"app\":\"harness-mana\",\"name\":"
                        "\"nnnnnnnnnnnnnnnn\",\"role\":\"holder\",\"lease\":{\"by\":"
                        "\"yyyyyyyyyyyy\",\"left\":86400,\"q\":99,\"req\":\"zzzzzzzzzzzz\","
                        "\"rl\":600},\"job\":{\"k\":\"program-\",\"p\":100},\"ttl\":300}";
    CHECK(strlen(worst) + 1u == 251u, "HM's figure: %zu", strlen(worst) + 1u);
    CHECK(parse(worst, &f) == 0 && f.lease.left == 86400u && f.lease.rl == 600u &&
          f.job.p == 100u && f.ttl_s == 300u, "the worst case parses");

    /* clips + the ASCII rule: raw UTF-8 bytes and unescaped control chars -> '?' */
    CHECK(parse("{\"op\":\"hello\",\"sid\":\"0123456789abcdef\",\"who\":"
                "\"d\xc3\xa9vid@h\\t\\u0041xxxxxxxxxxxxxxxxxxxx\"}", &f) != 0,
          "\\u escapes are refused by the tokenizer (fail closed)");
    CHECK(parse("{\"op\":\"hello\",\"sid\":\"0123456789abcdef\",\"who\":"
                "\"d\xc3\xa9vid@h\\tzzzzzzzzzzzzzzzzzzzzzzzz\",\"app\":\"harness-manager/1\","
                "\"name\":\"nnnnnnnnnnnnnnnnnnnnnnnn\",\"lease\":{\"by\":\"alice@lab-gateway1\","
                "\"req\":\"bob@lab-pc02\",\"left\":-5,\"q\":1000,\"rl\":99999},"
                "\"job\":{\"k\":\"programming\",\"p\":-3},\"ttl\":5}", &f) == 0, "clipped");
    CHECK(strcmp(f.sid, "01234567") == 0, "sid 8: %s", f.sid);
    CHECK(strcmp(f.who, "d??vid@h?zzzzzzzzzzz") == 0 && strlen(f.who) == 20u, "who: %s", f.who);
    CHECK(strlen(f.name) == 16u && strcmp(f.app, "harness-mana") == 0, "name/app");
    CHECK(strcmp(f.lease.by, "alice") == 0 && strcmp(f.lease.req, "bob") == 0, "user parts");
    CHECK(f.lease.left == 0u && f.lease.q == 99u && f.lease.rl == 600u, "lease clamps");
    CHECK(strcmp(f.job.k, "programm") == 0 && f.job.p == 0u && f.ttl_s == 30u, "job/ttl clamps");
    CHECK(f.role == PRES_ROLE_WATCH, "role defaults to watch");

    /* refusals */
    static const struct { const char *j, *why; } bad[] = {
        { "{\"op\":\"hello\",\"who\":\"a@b\"}", "invalid sid" },
        { "{\"op\":\"hello\",\"sid\":\"\",\"who\":\"a@b\"}", "invalid sid" },
        { "{\"op\":\"hello\",\"sid\":7,\"who\":\"a@b\"}", "invalid sid" },
        { "{\"op\":\"hello\",\"sid\":\"s\"}", "invalid who" },
        { "{\"op\":\"hello\",\"sid\":\"s\",\"who\":\"w\",\"role\":\"boss\"}", "invalid role" },
        { "{\"op\":\"hello\",\"sid\":\"s\",\"who\":\"w\",\"ttl\":\"90\"}", "invalid ttl" },
        { "{\"op\":\"hello\",\"sid\":\"s\",\"who\":\"w\",\"v\":0}", "invalid v" },
        { "{\"op\":\"hello\",\"sid\":\"s\",\"who\":\"w\",\"lease\":7}", "invalid lease" },
        { "{\"op\":\"hello\",\"sid\":\"s\",\"who\":\"w\",\"lease\":{\"left\":\"1h\"}}", "invalid lease" },
        { "{\"op\":\"hello\",\"sid\":\"s\",\"who\":\"w\",\"lease\":{\"by\":{\"x\":1}}}", "invalid request" },
        { "{\"op\":\"hello\",\"sid\":\"s\",\"who\":\"w\",\"job\":{\"p\":\"x\"}}", "invalid job" },
        { "{\"op\":\"hello\",\"sid\":\"s\",\"who\":\"w\",\"x\":[1]}", "invalid request" },
    };
    for (unsigned i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
        const char *why = parse(bad[i].j, &f);
        CHECK(why && strncmp(why, bad[i].why, strlen(bad[i].why)) == 0 && strlen(why) <= 63u,
              "%s -> %s", bad[i].j, why ? why : "(accepted)");
    }
    /* extra keys (top level, and in the lease) are ignored, not refused */
    CHECK(parse("{\"op\":\"hello\",\"sid\":\"s\",\"who\":\"w\",\"a\":1,\"b\":2,\"c\":3,\"d\":4,"
                "\"e\":5,\"f\":6,\"g\":7,\"lease\":{\"by\":\"x\",\"k1\":1,\"k2\":2,\"k3\":3,"
                "\"k4\":4,\"k5\":5,\"k6\":6,\"k7\":7}}", &f) == 0 && strcmp(f.lease.by, "x") == 0,
          "a newer client's extra keys ride along");
}

/* ---- 2. TTL + its negative control ------------------------------------------- */
static int ttl_scenario(int negctl)
{
    pres_table_t t;
    char j[160];
    pres_init(&t);
    t.negctl_no_ttl = negctl;
    mk(j, sizeof(j), "a1", "alice@lab-pc01", "holder", 30);
    hello(&t, j, T0);
    int live_at_ttl = pres_live(&t, T0 + S(30000), 0, 0);        /* <= ttl: live */
    int live_after = pres_live(&t, T0 + S(30001), 0, 0);         /* past it: gone */
    return live_at_ttl == 1 && live_after == 0;
}

static void t_ttl(void)
{
    pres_table_t t;
    pres_seg_t seg[3];
    char j[160];
    CHECK(ttl_scenario(0), "a session expires 1 ms after its ttl, not before");
    CHECK(!ttl_scenario(1), "NEGATIVE CONTROL: a table that ignores the TTL fails the check");

    pres_init(&t);
    mk(j, sizeof(j), "a1", "alice@lab-pc01", "holder", 90);
    hello(&t, j, T0);
    CHECK(pres_hm_row(&t, T0 + S(1000), "", seg) == 2 && strcmp(seg[1].text, "alice@lab-pc01") == 0,
          "one live: %s", seg[1].text);
    hello(&t, j, T0 + S(60000));                                  /* a beat refreshes it */
    CHECK(pres_live(&t, T0 + S(149000), 0, 0) == 1, "refreshed: live 90 s from the last hello");
    CHECK(pres_live(&t, T0 + S(150001), 0, 0) == 0, "then gone");
    (void)pres_hm_row(&t, T0 + S(60000 + 5 * 60000), "", seg);
    CHECK(strcmp(seg[1].text, "alice@lab-pc01  left 5m ago") == 0 && !seg[1].value,
          "left: %s", seg[1].text);
    (void)pres_hm_row(&t, T0 + S(60000 + 45000 + 90000), "", seg);
    CHECK(strcmp(seg[1].text, "alice@lab-pc01  left 2m ago") == 0, "%s", seg[1].text);
    pres_init(&t);
    (void)pres_hm_row(&t, T0, "", seg);
    CHECK(strcmp(seg[0].text, "hm") == 0 && seg[1].col == 7u &&
          strcmp(seg[1].text, "none connected") == 0, "nobody ever: %s", seg[1].text);
}

/* ---- 3. eviction + 4. order ------------------------------------------------- */
static void t_evict_order(void)
{
    pres_table_t t;
    const pres_session_t *v[4];
    char j[160];
    pres_facts_t f;
    int joined = 0, evicted = 0;
    pres_init(&t);
    const char *roles[] = { "watch", "owner", "watch", "holder" };
    for (int i = 0; i < 4; i++) {
        char sid[4];
        snprintf(sid, sizeof(sid), "s%d", i);
        mk(j, sizeof(j), sid, sid, roles[i], 90);
        hello(&t, j, T0 + S(1000) * (uint64_t)i);
    }
    int n = pres_live(&t, T0 + S(5000), v, 4);
    CHECK(n == 4 && strcmp(v[0]->next.sid, "s3") == 0 && strcmp(v[1]->next.sid, "s1") == 0 &&
          strcmp(v[2]->next.sid, "s2") == 0 && strcmp(v[3]->next.sid, "s0") == 0,
          "holder > owner > watch, recent first: %s %s %s %s", v[0]->next.sid, v[1]->next.sid,
          v[2]->next.sid, v[3]->next.sid);
    mk(j, sizeof(j), "s0", "s0", "watch", 90);                     /* s0 beats again */
    hello(&t, j, T0 + S(6000));
    mk(j, sizeof(j), "s4", "s4", "watch", 90);
    (void)parse(j, &f);
    n = pres_hello(&t, &f, T0 + S(7000), &joined, &evicted);
    CHECK(joined && evicted && n == 4 && t.evictions == 1u, "a 5th sid evicts (n %d)", n);
    int has1 = 0, has0 = 0;
    for (int i = 0; i < 4; i++) {
        has1 |= strcmp(t.s[i].next.sid, "s1") == 0;
        has0 |= strcmp(t.s[i].next.sid, "s0") == 0;
    }
    CHECK(!has1 && has0, "the OLDEST last hello (s1) went, not the first sid (s0)");
    /* an expired session is still the oldest candidate */
    pres_init(&t);
    mk(j, sizeof(j), "old", "old", "holder", 30);
    hello(&t, j, T0);
    for (int i = 0; i < 4; i++) {
        char sid[4];
        snprintf(sid, sizeof(sid), "n%d", i);
        mk(j, sizeof(j), sid, sid, "watch", 90);
        hello(&t, j, T0 + S(60000) + (uint64_t)i);
    }
    CHECK(pres_live(&t, T0 + S(61000), 0, 0) == 4, "4 live, the expired one evicted");
}

/* ---- 5. relative time, the badge + 7. text -------------------------------------- */
static void t_lease_badge(void)
{
    pres_table_t t;
    char txt[32];
    int warn = 0;
    pres_init(&t);
    CHECK(!pres_badge(&t, T0, "", "", txt, sizeof(txt), &warn), "no session: no badge");
    hello(&t, "{\"op\":\"hello\",\"sid\":\"w\",\"who\":\"bob@lab-pc02\",\"role\":\"watch\"}", T0);
    CHECK(!pres_badge(&t, T0, "", "", txt, sizeof(txt), &warn), "standalone (no lease): no badge");
    hello(&t, "{\"op\":\"hello\",\"sid\":\"w\",\"who\":\"bob@lab-pc02\",\"role\":\"watch\","
              "\"lease\":{\"q\":0}}", T0 + S(3000));
    CHECK(pres_badge(&t, T0 + S(3000), "", "!", txt, sizeof(txt), &warn) &&
          strcmp(txt, "! not leased") == 0 && warn, "behind a hub, nobody: %s", txt);
    hello(&t, "{\"op\":\"hello\",\"sid\":\"h\",\"who\":\"alice@lab-pc01\",\"role\":\"holder\","
              "\"lease\":{\"by\":\"alice\",\"left\":4332,\"q\":1},\"ttl\":300}", T0 + S(4000));
    CHECK(pres_badge(&t, T0 + S(4000), "#", "!", txt, sizeof(txt), &warn) &&
          strcmp(txt, "# alice 1h12m, 1 waiting") == 0 && !warn, "holder's: %s", txt);
    /* the watcher reports a fresher lease; the holder's own report still wins */
    hello(&t, "{\"op\":\"hello\",\"sid\":\"w\",\"who\":\"bob@lab-pc02\",\"role\":\"watch\","
              "\"lease\":{\"by\":\"carol\",\"left\":100}}", T0 + S(8000));
    CHECK(pres_badge(&t, T0 + S(8000), "", "", txt, sizeof(txt), &warn) &&
          strncmp(txt, "alice ", 6) == 0, "the holder's report wins: %s", txt);
    /* ageing on the board's clock: 4332 s at T0+4 s; 200 s later 4132 s left */
    CHECK(pres_badge(&t, T0 + S(4000 + 200000), "", "", txt, sizeof(txt), &warn) && !warn &&
          strcmp(txt, "alice 1h08m, 1 waiting") == 0, "aged: %s", txt);
    hello(&t, "{\"op\":\"hello\",\"sid\":\"h\",\"who\":\"alice@lab-pc01\",\"role\":\"holder\","
              "\"lease\":{\"by\":\"alice\",\"left\":299,\"q\":0},\"ttl\":300}", T0 + S(100000));
    CHECK(pres_badge(&t, T0 + S(100000), "", "", txt, sizeof(txt), &warn) && warn &&
          strcmp(txt, "alice 4m") == 0, "warn under 5 min: %s", txt);
    CHECK(pres_badge(&t, T0 + S(100000 + 299000), "", "", txt, sizeof(txt), &warn) &&
          strcmp(txt, "alice 0s") == 0 && warn, "never negative: %s", txt);
    /* nobody live -> no badge: the panel never keeps a lease nobody confirmed */
    CHECK(!pres_badge(&t, T0 + S(100000 + 300001), "", "", txt, sizeof(txt), &warn),
          "all expired: no badge");
    /* forms that fit 24 cells */
    pres_init(&t);
    hello(&t, "{\"op\":\"hello\",\"sid\":\"h\",\"who\":\"x\",\"role\":\"holder\","
              "\"lease\":{\"by\":\"abcdefghijkl\",\"left\":86400,\"q\":99}}", T0);
    CHECK(pres_badge(&t, T0, "\x83", "", txt, sizeof(txt), &warn) && strlen(txt) <= 24u &&
          strcmp(txt, "\x83 abcdefghij 24h00m +99") == 0, "short form: '%s' (%zu)", txt, strlen(txt));
}

static void t_request(void)
{
    pres_table_t t;
    char ln[3][PRES_COLS + 1];
    pres_init(&t);
    hello(&t, "{\"op\":\"hello\",\"sid\":\"h\",\"who\":\"d@h\",\"role\":\"holder\","
              "\"lease\":{\"by\":\"alice\",\"left\":4332,\"q\":1,\"req\":\"bob\",\"rl\":103}}", T0);
    CHECK(pres_request(&t, T0, ln) && strcmp(ln[0], "bob wants this board") == 0 &&
          strcmp(ln[1], "held by alice 1:43 to answer") == 0 &&
          strcmp(ln[2], "tap: tell alice you are here") == 0, "%s / %s / %s", ln[0], ln[1], ln[2]);
    CHECK(pres_request(&t, T0 + S(43000), ln) && strcmp(ln[1], "held by alice 1:00 to answer") == 0,
          "counts down: %s", ln[1]);
    CHECK(!pres_request(&t, T0 + S(103000), ln), "its time to answer is over: gone");
    pres_init(&t);
    hello(&t, "{\"op\":\"hello\",\"sid\":\"h\",\"who\":\"d@h\",\"role\":\"holder\","
              "\"lease\":{\"by\":\"alice\",\"left\":10}}", T0);
    CHECK(!pres_request(&t, T0, ln), "no req: no banner");
}

/* ---- 6. the repaint rule ------------------------------------------------------ */
static void t_commit(void)
{
    pres_table_t t;
    pres_seg_t seg[3];
    const pres_session_t *v[4];
    pres_init(&t);
    hello(&t, "{\"op\":\"hello\",\"sid\":\"a\",\"who\":\"first@h\",\"role\":\"watch\"}", T0);
    hello(&t, "{\"op\":\"hello\",\"sid\":\"a\",\"who\":\"second@h\",\"role\":\"watch\"}", T0 + S(500));
    CHECK(pres_live(&t, T0 + S(500), v, 4) == 1 && strcmp(v[0]->next.who, "second@h") == 0,
          "the reply side has it at once");
    (void)pres_commit(&t, T0 + S(1999));
    (void)pres_hm_row(&t, T0 + S(1999), "", seg);
    CHECK(strcmp(seg[1].text, "first@h") == 0, "< 2 s: not drawn yet (%s)", seg[1].text);
    CHECK(pres_commit(&t, T0 + S(2000)) == 1, "committed at 2 s");
    (void)pres_hm_row(&t, T0 + S(2000), "", seg);
    CHECK(strcmp(seg[1].text, "second@h") == 0, ">= 2 s: drawn (%s)", seg[1].text);
    hello(&t, "{\"op\":\"hello\",\"sid\":\"a\",\"who\":\"third@h\",\"role\":\"watch\"}", T0 + S(4500));
    (void)pres_hm_row(&t, T0 + S(4500), "", seg);
    CHECK(strcmp(seg[1].text, "third@h") == 0, "2.5 s after the commit: drawn at once");
}

static void t_hm_row(void)
{
    pres_table_t t;
    pres_seg_t seg[3];
    pres_init(&t);
    hello(&t, "{\"op\":\"hello\",\"sid\":\"w\",\"who\":\"bob@lab-pc02\",\"role\":\"watch\"}", T0);
    hello(&t, "{\"op\":\"hello\",\"sid\":\"h\",\"who\":\"alice@lab-pc01\",\"role\":\"holder\"}", T0);
    int n = pres_hm_row(&t, T0, "", seg);
    char row[41];
    memset(row, ' ', 40);
    row[40] = '\0';
    for (int i = 0; i < n; i++) {
        memcpy(row + seg[i].col, seg[i].text, strlen(seg[i].text));
    }
    /* HM's fake LINUX_STATUS_ROWS[11], the same words */
    CHECK(strcmp(row, "hm     alice@lab-pc01  +1 watching      ") == 0, "'%s'", row);
    CHECK(n == 3 && !seg[0].value && seg[1].value && !seg[2].value, "label/value/label");
    n = pres_hm_row(&t, T0, "\x84", seg);
    CHECK(strcmp(seg[1].text, "\x84" "alice@lab-pc01") == 0 && seg[2].col == 7u + 15u + 2u,
          "the user glyph shifts the count one cell");
}

/* ---- 8. the budget ---------------------------------------------------------------- */
static void t_budget(void)
{
    pres_table_t t;
    char out[1024];
    pres_init(&t);
    for (int i = 0; i < 4; i++) {
        /* sid: 7 JSON-escaped quotes + a digit; who: 20 escaped backslashes -- every
         * character doubles again on the way out */
        char j[256], sid[32] = "", who[64] = "";
        for (int k = 0; k < 7; k++) {
            strcat(sid, "\\\"");
        }
        snprintf(sid + strlen(sid), sizeof(sid) - strlen(sid), "%d", i);
        for (int k = 0; k < 20; k++) {
            strcat(who, "\\\\");
        }
        snprintf(j, sizeof(j), "{\"op\":\"hello\",\"sid\":\"%s\",\"who\":\"%s\","
                 "\"role\":\"holder\",\"ttl\":300}", sid, who);
        hello(&t, j, T0 + (uint64_t)i);
    }
    int n = pres_json_sessions(&t, T0 + S(299000), out, sizeof(out));
    CHECK(n > 0 && n < 640, "4 escape-heavy sessions: %d B (panel_linux.c gives it 640)", n);
    printf("  budget: worst sessions array %d B\n", n);
    CHECK(pres_json_sessions(&t, T0, out, 40) == -1 && out[0] == '\0', "too small: -1, empty");
}

int main(void)
{
    t_parse();
    t_ttl();
    t_evict_order();
    t_lease_badge();
    t_request();
    t_commit();
    t_hm_row();
    t_budget();
    printf("presence_core: %d checks, %d failures\n", g_n, g_fail);
    return g_fail ? 1 : 0;
}
