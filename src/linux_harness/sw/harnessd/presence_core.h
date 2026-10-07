/*
 * presence_core.h -- presence (net-protocol.md v0.17 "Presence and the panel"):
 * the `hello` request's parse, the board's session table and what the panel
 * draws from it. PURE: no I/O, no clock of its own (every call takes `now_ms`,
 * a monotonic millisecond count), no harnessd or clcd dependency -- so the
 * rules are unit-tested directly (tests/test_presence_core.c) and panel_linux.c
 * only binds them to the verbs and the clcd seams.
 *
 * THE TABLE (Harness Manager docs/design/CLCD_ALIGNMENT.md §2.3, §5.3):
 *   - at most PRES_MAX_SESSIONS (4) sessions, keyed by `sid`; a 5th sid evicts
 *     the one whose last hello is OLDEST (live or not);
 *   - a session is LIVE for its `ttl` (30-300 s, default 90) after its last hello
 *     (now - seen <= ttl, the Harness Manager fake's rule);
 *   - live sessions are listed holder > owner > watch, most recent first;
 *   - an expired session stays in the table (until evicted or harnessd restarts)
 *     so the panel can say "left 5m ago";
 *   - RELATIVE TIMES: a lease's `left` and a request's `rl` are seconds at the
 *     moment the hello ARRIVED; they are aged on the board's monotonic clock from
 *     then (the board has no wall clock it can trust);
 *   - THE REPAINT RULE: what the panel draws for a sid changes at most once per
 *     PRES_COMMIT_MS (2 s). Every hello refreshes the TTL and the facts a reply
 *     reports at once; the drawn copy ("shown") takes them on the next commit
 *     at least 2 s after its last one, so two hellos < 2 s apart cause one repaint.
 *
 * THE FIELD RULES (printable ASCII, clipped per field; one byte per character):
 * any byte outside 0x20-0x7E becomes '?', then the value is clipped: sid 8, who
 * 20, name 16, app 12, lease users 12 (their user part: cut at '@'), job kind 8.
 * Numbers are clamped: ttl 30-300, left 0-86400, q 0-99, rl 0-600, job p 0-100.
 * Wrong TYPES are refused (code "invalid"), never guessed.
 */
#ifndef PRESENCE_CORE_H
#define PRESENCE_CORE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PRES_MAX_SESSIONS 4
#define PRES_SID_MAX      8
#define PRES_WHO_MAX      20
#define PRES_NAME_MAX     16
#define PRES_APP_MAX      12
#define PRES_USER_MAX     12
#define PRES_JOB_MAX      8
#define PRES_TTL_DEFAULT  90u
#define PRES_TTL_MIN      30u
#define PRES_TTL_MAX      300u
#define PRES_LEFT_MAX     86400u
#define PRES_Q_MAX        99u
#define PRES_RL_MAX       600u
#define PRES_COMMIT_MS    2000u
/* What the panel shows of a name: the hm row's user@host, the badge's holder. */
#define PRES_ROW_WHO      16
#define PRES_BADGE_USER   10
#define PRES_BADGE_MAX    24u     /* == clcd_seams.h CLCD_TITLE_RIGHT_MAX */
#define PRES_COLS         40u

enum { PRES_ROLE_HOLDER = 0, PRES_ROLE_OWNER = 1, PRES_ROLE_WATCH = 2 };

typedef struct {
    uint8_t  has;                      /* a `lease` object was sent (behind a hub) */
    uint8_t  has_left, has_rl;
    char     by[PRES_USER_MAX + 1];    /* "" = nobody holds it                      */
    char     req[PRES_USER_MAX + 1];   /* "" = no open request                      */
    uint32_t left, q, rl;
} pres_lease_t;

typedef struct {
    uint8_t  has;
    char     k[PRES_JOB_MAX + 1];
    uint32_t p;
} pres_job_t;

typedef struct {
    char         sid[PRES_SID_MAX + 1];
    char         who[PRES_WHO_MAX + 1];
    char         name[PRES_NAME_MAX + 1];
    char         app[PRES_APP_MAX + 1];
    uint8_t      role;                 /* PRES_ROLE_*                               */
    uint32_t     ttl_s;
    pres_lease_t lease;
    pres_job_t   job;
} pres_facts_t;

typedef struct {
    uint8_t      used;
    uint8_t      pending;              /* next differs from shown, not committed    */
    uint64_t     seen_ms;              /* the last hello: TTL and "most recent"     */
    pres_facts_t next;                 /* the latest hello (replies report these)   */
    uint64_t     next_ms;              /* when `next` arrived (its ageing base)     */
    pres_facts_t shown;                /* what the panel draws                      */
    uint64_t     shown_ms;             /* when `shown`'s hello arrived              */
    uint64_t     commit_ms;            /* when `shown` was last committed           */
} pres_session_t;

typedef struct {
    pres_session_t s[PRES_MAX_SESSIONS];
    uint32_t       hellos;             /* accepted, since init                      */
    uint32_t       evictions;
    int            negctl_no_ttl;      /* TESTS ONLY: the TTL rule's negative control */
} pres_table_t;

void pres_init(pres_table_t *t);

/* Parse one `hello` line (the raw request; `lease`/`job` nest one level).
 * NULL = ok and *f filled (clipped, clamped); else the refusal text (<= 63
 * chars, code "invalid"). */
const char *pres_parse_hello(const char *line, int len, pres_facts_t *f);

/* Apply a parsed hello at now_ms. Returns the number of LIVE sessions after it
 * (the reply's "sessions"). *evicted (may be NULL) = 1 when a 5th sid pushed the
 * oldest out; *joined = 1 when the sid was not in the table. */
int pres_hello(pres_table_t *t, const pres_facts_t *f, uint64_t now_ms,
               int *joined, int *evicted);

/* The live sessions, holder > owner > watch then most recent first. Returns the
 * count (<= max). */
int pres_live(const pres_table_t *t, uint64_t now_ms, const pres_session_t **out, int max);
int pres_is_live(const pres_table_t *t, const pres_session_t *s, uint64_t now_ms);

/* The repaint rule: commit `next` into `shown` for every sid whose last commit
 * is >= PRES_COMMIT_MS old. Returns how many committed. */
int pres_commit(pres_table_t *t, uint64_t now_ms);

/* A relative time aged from `base_ms` to now_ms: max(0, v - elapsed seconds). */
uint32_t pres_age(uint32_t v, uint64_t base_ms, uint64_t now_ms);

/* "1h12m" / "5m" / "45s" (>= 1 h / >= 1 min / less). */
void pres_fmt_dur(char *out, size_t cap, uint32_t seconds);

/* ---- what the panel draws (from the COMMITTED facts) -------------------- */

/* Row 0's lease badge: 1 = draw `text` (<= PRES_BADGE_MAX chars), *warn = 1 for
 * the warn role (under 5 min left, or "not leased"), 0 for held. `glyph_held` /
 * `glyph_warn` prefix it ("" in a theme without glyphs). The freshest lease a
 * LIVE session reported wins, the holder's own report first; no live session
 * with a lease = no badge (the panel never keeps a lease nobody confirmed). */
int pres_badge(const pres_table_t *t, uint64_t now_ms, const char *glyph_held,
               const char *glyph_warn, char *text, size_t cap, int *warn);

/* Row 11, "hm": up to 3 segments {col, text, value?}: "hm" (label), then the
 * first live session's who (value, prefixed with glyph_user) and "+N watching"
 * (label); or "none connected" / "<who>  left 5m ago" (label). Returns the count. */
typedef struct {
    unsigned col;
    char     text[PRES_COLS + 1];
    int      value;                    /* 1 = the value role, 0 = the label role */
} pres_seg_t;
int pres_hm_row(const pres_table_t *t, uint64_t now_ms, const char *glyph_user,
                pres_seg_t seg[3]);

/* Rows 10-12: the lease-request banner, while the freshest lease names an open
 * request (req, and rl > 0 once aged when rl was sent). 1 = lines filled
 * (each <= 40 chars, uncentred). */
int pres_request(const pres_table_t *t, uint64_t now_ms, char line[3][PRES_COLS + 1]);

/* ---- JSON ---------------------------------------------------------------- */

/* `s` as a JSON string BODY ('"' and '\\' escaped; the table holds printable
 * ASCII only). Returns the length, or -1 if it did not fit (out = ""). */
int pres_json_str(char *out, size_t cap, const char *s);

/* The live sessions as a JSON array: [{"sid":..,"who":..,"role":..,"age_s":N},..]
 * (the LATEST facts). Returns the length, or -1 if it did not fit. */
int pres_json_sessions(const pres_table_t *t, uint64_t now_ms, char *out, size_t cap);

const char *pres_role_name(unsigned role);

#ifdef __cplusplus
}
#endif

#endif /* PRESENCE_CORE_H */
