/*
 * presence_core.c -- the `hello` parse, the session table and the panel's
 * presence text (presence_core.h is the description; net-protocol.md v0.17
 * "Presence and the panel" is the contract). Pure: no I/O, no clock.
 */
#include <stdio.h>
#include <string.h>

#include "../../../../firmware/common/net_if.h"
#include "../../../../firmware/common/net_proto.h"
#include "presence_core.h"

static const char *const k_roles[] = { "holder", "owner", "watch" };

const char *pres_role_name(unsigned role)
{
    return role < 3u ? k_roles[role] : "watch";
}

void pres_init(pres_table_t *t)
{
    memset(t, 0, sizeof(*t));
}

/* ---- the parse --------------------------------------------------------------- */

/* Printable ASCII, clipped: any byte outside 0x20-0x7E becomes '?'. `cut_at`
 * (0 = none) ends the value at that character (the user part of a principal). */
static void clip_ascii(char *dst, size_t cap, const char *src, char cut_at)
{
    size_t o = 0;
    for (const unsigned char *p = (const unsigned char *)src; *p && o + 1u < cap; p++) {
        if (cut_at && *p == (unsigned char)cut_at) {
            break;
        }
        dst[o++] = (*p >= 0x20u && *p <= 0x7Eu) ? (char)*p : '?';
    }
    dst[o] = '\0';
}

/* An OPTIONAL string, clipped. 1 = present, 0 = absent, -1 = present but not a
 * string. The line is <= MPS3_NET_LINE_MAX, so `raw` always holds the value. */
static int opt_str(const mps3_json_obj_t *o, const char *key, char *dst, size_t cap,
                   char cut_at)
{
    char raw[MPS3_NET_LINE_MAX + 4];
    int rc = mps3_json_get_string(o, key, raw, (int)sizeof(raw));
    if (rc == MPS3_JSON_EMISSING) {
        dst[0] = '\0';
        return 0;
    }
    if (rc != MPS3_JSON_OK) {
        dst[0] = '\0';
        return -1;
    }
    clip_ascii(dst, cap, raw, cut_at);
    return 1;
}

/* An OPTIONAL integer, clamped to [lo, hi]. 1 present, 0 absent, -1 not an int. */
static int opt_int(const mps3_json_obj_t *o, const char *key, uint32_t lo, uint32_t hi,
                   uint32_t *out)
{
    int32_t v = 0;
    int rc = mps3_json_get_int(o, key, &v);
    if (rc == MPS3_JSON_EMISSING) {
        return 0;
    }
    if (rc != MPS3_JSON_OK) {
        return -1;
    }
    uint32_t u = v < 0 ? 0u : (uint32_t)v;
    *out = u < lo ? lo : u > hi ? hi : u;
    return 1;
}

/* A nested (flat) object member, re-parsed keeping only `only`'s keys, so extra
 * keys a newer client adds never hit the member bound. */
static int sub_object(const mps3_json_obj_t *o, const char *key, mps3_json_obj_t *sub,
                      const char *const *only)
{
    size_t kl = strlen(key);
    for (int i = 0; i < o->n_members; i++) {
        const mps3_json_member_t *m = &o->members[i];
        if ((size_t)m->key_len != kl || memcmp(m->key, key, kl) != 0) {
            continue;
        }
        if (m->type != MPS3_JSON_T_OBJECT ||
            mps3_json_parse_ex(m->val, m->val_len, sub, only, 0) != MPS3_JSON_OK) {
            return MPS3_JSON_ETYPE;
        }
        return MPS3_JSON_OK;
    }
    return MPS3_JSON_EMISSING;
}

const char *pres_parse_hello(const char *line, int len, pres_facts_t *f)
{
    static const char *const k_keys[] = { "op", "v", "sid", "who", "app", "name", "role",
                                          "lease", "job", "ttl", NULL };
    static const char *const k_lease[] = { "by", "left", "q", "req", "rl", NULL };
    static const char *const k_job[] = { "k", "p", NULL };
    mps3_json_obj_t obj, sub;
    char role[16];
    int32_t v = 1;

    memset(f, 0, sizeof(*f));
    f->role = PRES_ROLE_WATCH;
    f->ttl_s = PRES_TTL_DEFAULT;
    if (!line || mps3_json_parse_ex(line, len, &obj, k_keys, 1) != MPS3_JSON_OK) {
        return "invalid request";
    }
    int rc = mps3_json_get_int(&obj, "v", &v);
    if ((rc != MPS3_JSON_OK && rc != MPS3_JSON_EMISSING) || v < 1) {
        return "invalid v: an integer >= 1";
    }
    if (opt_str(&obj, "sid", f->sid, sizeof(f->sid), 0) != 1 || f->sid[0] == '\0') {
        return "invalid sid: 1-8 printable characters";
    }
    if (opt_str(&obj, "who", f->who, sizeof(f->who), 0) != 1) {
        return "invalid who: a string (user@host)";
    }
    if (opt_str(&obj, "app", f->app, sizeof(f->app), 0) < 0) {
        return "invalid app: a string";
    }
    if (opt_str(&obj, "name", f->name, sizeof(f->name), 0) < 0) {
        return "invalid name: a string";
    }
    rc = mps3_json_get_string(&obj, "role", role, (int)sizeof(role));
    if (rc == MPS3_JSON_OK) {
        unsigned r;
        for (r = 0; r < 3u && strcmp(role, k_roles[r]) != 0; r++) {
        }
        if (r == 3u) {
            return "invalid role: holder, owner or watch";
        }
        f->role = (uint8_t)r;
    } else if (rc != MPS3_JSON_EMISSING) {
        return "invalid role: holder, owner or watch";
    }
    if (opt_int(&obj, "ttl", PRES_TTL_MIN, PRES_TTL_MAX, &f->ttl_s) < 0) {
        return "invalid ttl: an integer (30-300 s)";
    }

    rc = sub_object(&obj, "lease", &sub, k_lease);
    if (rc == MPS3_JSON_OK) {
        f->lease.has = 1;
        if (opt_str(&sub, "by", f->lease.by, sizeof(f->lease.by), '@') < 0) {
            return "invalid lease.by: a string";
        }
        if (opt_str(&sub, "req", f->lease.req, sizeof(f->lease.req), '@') < 0) {
            return "invalid lease.req: a string";
        }
        int l = opt_int(&sub, "left", 0u, PRES_LEFT_MAX, &f->lease.left);
        int q = opt_int(&sub, "q", 0u, PRES_Q_MAX, &f->lease.q);
        int r = opt_int(&sub, "rl", 0u, PRES_RL_MAX, &f->lease.rl);
        if (l < 0 || q < 0 || r < 0) {
            return "invalid lease: left, q and rl are integers";
        }
        f->lease.has_left = (uint8_t)(l == 1);
        f->lease.has_rl = (uint8_t)(r == 1);
    } else if (rc != MPS3_JSON_EMISSING) {
        return "invalid lease: a flat object";
    }

    rc = sub_object(&obj, "job", &sub, k_job);
    if (rc == MPS3_JSON_OK) {
        f->job.has = 1;
        if (opt_str(&sub, "k", f->job.k, sizeof(f->job.k), 0) < 0 ||
            opt_int(&sub, "p", 0u, 100u, &f->job.p) < 0) {
            return "invalid job: {k: string, p: integer}";
        }
    } else if (rc != MPS3_JSON_EMISSING) {
        return "invalid job: a flat object";
    }
    return 0;
}

/* ---- the table ---------------------------------------------------------------- */

int pres_is_live(const pres_table_t *t, const pres_session_t *s, uint64_t now_ms)
{
    if (!s->used) {
        return 0;
    }
    if (t->negctl_no_ttl) {
        return 1;                       /* NEGATIVE CONTROL: the TTL ignored */
    }
    return now_ms - s->seen_ms <= (uint64_t)s->next.ttl_s * 1000u;
}

static void commit_one(pres_session_t *s, uint64_t now_ms)
{
    s->shown = s->next;
    s->shown_ms = s->next_ms;
    s->commit_ms = now_ms;
    s->pending = 0;
}

int pres_hello(pres_table_t *t, const pres_facts_t *f, uint64_t now_ms, int *joined,
               int *evicted)
{
    pres_session_t *s = 0;
    int j = 0, e = 0;
    for (int i = 0; i < PRES_MAX_SESSIONS; i++) {
        if (t->s[i].used && strcmp(t->s[i].next.sid, f->sid) == 0) {
            s = &t->s[i];
        }
    }
    if (!s) {
        j = 1;
        for (int i = 0; i < PRES_MAX_SESSIONS && !s; i++) {
            if (!t->s[i].used) {
                s = &t->s[i];
            }
        }
        if (!s) {                       /* full: the oldest last hello goes */
            s = &t->s[0];
            for (int i = 1; i < PRES_MAX_SESSIONS; i++) {
                if (t->s[i].seen_ms < s->seen_ms) {
                    s = &t->s[i];
                }
            }
            e = 1;
            t->evictions++;
        }
        memset(s, 0, sizeof(*s));
        s->used = 1;
    }
    s->seen_ms = now_ms;
    s->next = *f;
    s->next_ms = now_ms;
    if (j || now_ms - s->commit_ms >= PRES_COMMIT_MS) {
        commit_one(s, now_ms);          /* a new sid, or the last repaint >= 2 s ago */
    } else {
        s->pending = 1;                 /* < 2 s: the reply has it, the panel later */
    }
    t->hellos++;
    if (joined) {
        *joined = j;
    }
    if (evicted) {
        *evicted = e;
    }
    return pres_live(t, now_ms, 0, 0);
}

int pres_commit(pres_table_t *t, uint64_t now_ms)
{
    int n = 0;
    for (int i = 0; i < PRES_MAX_SESSIONS; i++) {
        pres_session_t *s = &t->s[i];
        if (s->used && s->pending && now_ms - s->commit_ms >= PRES_COMMIT_MS) {
            commit_one(s, now_ms);
            n++;
        }
    }
    return n;
}

/* holder > owner > watch, then the most recent hello first. `shown` = rank by
 * the drawn role (the panel) or the latest (replies). */
static int before(const pres_session_t *a, const pres_session_t *b, int shown)
{
    unsigned ra = shown ? a->shown.role : a->next.role;
    unsigned rb = shown ? b->shown.role : b->next.role;
    if (ra != rb) {
        return ra < rb;
    }
    return a->seen_ms > b->seen_ms;
}

static int live_sorted(const pres_table_t *t, uint64_t now_ms, const pres_session_t **out,
                       int max, int shown)
{
    const pres_session_t *v[PRES_MAX_SESSIONS];
    int n = 0;
    for (int i = 0; i < PRES_MAX_SESSIONS; i++) {
        if (pres_is_live(t, &t->s[i], now_ms)) {
            v[n++] = &t->s[i];
        }
    }
    for (int i = 1; i < n; i++) {       /* insertion sort: n <= 4 */
        const pres_session_t *x = v[i];
        int k = i - 1;
        while (k >= 0 && before(x, v[k], shown)) {
            v[k + 1] = v[k];
            k--;
        }
        v[k + 1] = x;
    }
    for (int i = 0; i < n && i < max; i++) {
        out[i] = v[i];
    }
    return n;
}

int pres_live(const pres_table_t *t, uint64_t now_ms, const pres_session_t **out, int max)
{
    return live_sorted(t, now_ms, out, out ? max : 0, 0);
}

uint32_t pres_age(uint32_t v, uint64_t base_ms, uint64_t now_ms)
{
    uint64_t el = now_ms > base_ms ? (now_ms - base_ms) / 1000u : 0u;
    return el >= v ? 0u : (uint32_t)(v - el);
}

void pres_fmt_dur(char *out, size_t cap, uint32_t s)
{
    if (s >= 3600u) {
        snprintf(out, cap, "%uh%02um", (unsigned)(s / 3600u), (unsigned)((s % 3600u) / 60u));
    } else if (s >= 60u) {
        snprintf(out, cap, "%um", (unsigned)(s / 60u));
    } else {
        snprintf(out, cap, "%us", (unsigned)s);
    }
}

/* ---- what the panel draws ------------------------------------------------------ */

/* The freshest lease a LIVE session reported (committed facts): the holder's own
 * report first, then the most recent hello. NULL = none. */
static const pres_session_t *lease_src(const pres_table_t *t, uint64_t now_ms)
{
    const pres_session_t *best = 0;
    for (int i = 0; i < PRES_MAX_SESSIONS; i++) {
        const pres_session_t *s = &t->s[i];
        if (!pres_is_live(t, s, now_ms) || !s->shown.lease.has) {
            continue;
        }
        if (!best) {
            best = s;
            continue;
        }
        int sh = s->shown.role == PRES_ROLE_HOLDER, bh = best->shown.role == PRES_ROLE_HOLDER;
        if (sh != bh ? sh : s->shown_ms > best->shown_ms) {
            best = s;
        }
    }
    return best;
}

int pres_badge(const pres_table_t *t, uint64_t now_ms, const char *glyph_held,
               const char *glyph_warn, char *text, size_t cap, int *warn)
{
    const pres_session_t *s = lease_src(t, now_ms);
    char who[PRES_BADGE_USER + 1], dur[16], cand[64];
    text[0] = '\0';
    if (!s) {
        return 0;
    }
    const pres_lease_t *l = &s->shown.lease;
    size_t lim = cap - 1u < PRES_BADGE_MAX ? cap - 1u : PRES_BADGE_MAX;
    if (!l->by[0]) {
        *warn = 1;
        snprintf(cand, sizeof(cand), "%s%snot leased", glyph_warn, glyph_warn[0] ? " " : "");
        snprintf(text, cap, "%.*s", (int)lim, cand);
        return 1;
    }
    uint32_t left = l->has_left ? pres_age(l->left, s->shown_ms, now_ms) : 0u;
    *warn = l->has_left && left < 300u;
    clip_ascii(who, sizeof(who), l->by, 0);
    int n = snprintf(cand, sizeof(cand), "%s%s%s", glyph_held, glyph_held[0] ? " " : "", who);
    if (l->has_left) {
        pres_fmt_dur(dur, sizeof(dur), left);
        n += snprintf(cand + n, sizeof(cand) - (size_t)n, " %s", dur);
    }
    if (l->q) {
        /* the longest form that fits the title's right side */
        static const char *const k_forms[] = { ", %u waiting", ", %u wait", " +%u" };
        for (unsigned i = 0; i < 3u; i++) {
            char tail[24];
            int tn = snprintf(tail, sizeof(tail), k_forms[i], (unsigned)l->q);
            if ((size_t)(n + tn) <= lim || i == 2u) {
                snprintf(cand + n, sizeof(cand) - (size_t)n, "%s", tail);
                break;
            }
        }
    }
    snprintf(text, cap, "%.*s", (int)lim, cand);
    return 1;
}

int pres_hm_row(const pres_table_t *t, uint64_t now_ms, const char *glyph_user,
                pres_seg_t seg[3])
{
    const pres_session_t *v[PRES_MAX_SESSIONS];
    char who[PRES_ROW_WHO + 1], dur[16];
    int n = live_sorted(t, now_ms, v, PRES_MAX_SESSIONS, 1);
    memset(seg, 0, 3u * sizeof(*seg));
    seg[0].col = 0;
    snprintf(seg[0].text, sizeof(seg[0].text), "hm");
    if (n == 0) {
        const pres_session_t *last = 0;
        for (int i = 0; i < PRES_MAX_SESSIONS; i++) {
            if (t->s[i].used && (!last || t->s[i].seen_ms > last->seen_ms)) {
                last = &t->s[i];
            }
        }
        seg[1].col = 7;
        if (!last) {
            snprintf(seg[1].text, sizeof(seg[1].text), "none connected");
        } else {
            clip_ascii(who, sizeof(who), last->shown.who[0] ? last->shown.who : "?", 0);
            pres_fmt_dur(dur, sizeof(dur), (uint32_t)((now_ms - last->seen_ms) / 1000u));
            char tmp[64];
            snprintf(tmp, sizeof(tmp), "%s  left %s ago", who, dur);
            snprintf(seg[1].text, sizeof(seg[1].text), "%.40s", tmp);
        }
        return 2;
    }
    clip_ascii(who, sizeof(who), v[0]->shown.who[0] ? v[0]->shown.who : "?", 0);
    seg[1].col = 7;
    seg[1].value = 1;
    snprintf(seg[1].text, sizeof(seg[1].text), "%s%s", glyph_user, who);
    if (n == 1) {
        return 2;
    }
    seg[2].col = 7u + (unsigned)strlen(seg[1].text) + 2u;
    snprintf(seg[2].text, sizeof(seg[2].text), "+%d watching", n - 1);
    return 3;
}

int pres_request(const pres_table_t *t, uint64_t now_ms, char line[3][PRES_COLS + 1])
{
    const pres_session_t *s = lease_src(t, now_ms);
    if (!s || !s->shown.lease.req[0]) {
        return 0;
    }
    const pres_lease_t *l = &s->shown.lease;
    uint32_t rl = l->has_rl ? pres_age(l->rl, s->shown_ms, now_ms) : 0u;
    if (l->has_rl && rl == 0u) {
        return 0;                       /* the request's time to answer is over */
    }
    char tm[16] = "", tmp[3][80];
    if (l->has_rl) {
        snprintf(tm, sizeof(tm), " %u:%02u", (unsigned)(rl / 60u), (unsigned)(rl % 60u));
    }
    snprintf(tmp[0], sizeof(tmp[0]), "%s wants this board", l->req);
    if (l->by[0]) {
        snprintf(tmp[1], sizeof(tmp[1]), "held by %s%s%s", l->by, tm, tm[0] ? " to answer" : "");
        snprintf(tmp[2], sizeof(tmp[2]), "tap: tell %s you are here", l->by);
    } else {
        snprintf(tmp[1], sizeof(tmp[1]), "%s%s", tm[0] ? tm + 1 : "", tm[0] ? " to answer" : "");
        snprintf(tmp[2], sizeof(tmp[2]), "tap: say you are here");
    }
    for (int i = 0; i < 3; i++) {
        snprintf(line[i], PRES_COLS + 1u, "%.40s", tmp[i]);
    }
    return 1;
}

/* ---- JSON ---------------------------------------------------------------------- */

int pres_json_str(char *out, size_t cap, const char *s)
{
    size_t o = 0;
    for (; *s; s++) {
        size_t need = (*s == '"' || *s == '\\') ? 2u : 1u;
        if (o + need >= cap) {
            if (cap) {
                out[0] = '\0';
            }
            return -1;
        }
        if (need == 2u) {
            out[o++] = '\\';
        }
        out[o++] = *s;
    }
    if (cap == 0) {
        return -1;
    }
    out[o] = '\0';
    return (int)o;
}

int pres_json_sessions(const pres_table_t *t, uint64_t now_ms, char *out, size_t cap)
{
    const pres_session_t *v[PRES_MAX_SESSIONS];
    char sid[2 * PRES_SID_MAX + 1], who[2 * PRES_WHO_MAX + 1];
    int n = pres_live(t, now_ms, v, PRES_MAX_SESSIONS);
    size_t o = 0;
    if (cap < 3u) {
        return -1;
    }
    out[o++] = '[';
    for (int i = 0; i < n; i++) {
        (void)pres_json_str(sid, sizeof(sid), v[i]->next.sid);
        (void)pres_json_str(who, sizeof(who), v[i]->next.who);
        int w = snprintf(out + o, cap - o, "%s{\"sid\":\"%s\",\"who\":\"%s\",\"role\":\"%s\","
                         "\"age_s\":%u}", i ? "," : "", sid, who, pres_role_name(v[i]->next.role),
                         (unsigned)((now_ms - v[i]->seen_ms) / 1000u));
        if (w < 0 || (size_t)w >= cap - o) {
            out[0] = '\0';
            return -1;
        }
        o += (size_t)w;
    }
    if (o + 2u > cap) {
        out[0] = '\0';
        return -1;
    }
    out[o++] = ']';
    out[o] = '\0';
    return (int)o;
}
