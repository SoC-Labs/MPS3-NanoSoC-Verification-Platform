/*
 * stage0_ident.c -- see stage0_ident.h. A deliberately small JSON reader: it
 * accepts one flat object whose values are strings without escapes, integers
 * or true/false/null, which covers every well-formed identify request; any
 * nesting, escape, duplicate key or trailing junk is "malformed" and is
 * dropped without a reply, as the spec asks.
 */
#include "stage0_ident.h"
#include "stage0_net.h"
#include "stage0_flow.h"   /* s0_rescue_reason_text */
#include "stage0_fmt.h"

static struct s0_ident_cfg s_cfg;
static struct s0_status   *s_st;
static uint32_t            s_tokens;
static uint32_t            s_t_ref;
static uint32_t            s_started;
static char                s_reply[512];

void s0_ident_init(const struct s0_ident_cfg *cfg, struct s0_status *st)
{
    s_cfg = *cfg;
    s_st = st;
    s_tokens = S0_IDENT_RATE;
    s_started = 0u;
}

void s0_ident_poll(uint32_t now_ms)
{
    const uint32_t per = 1000u / S0_IDENT_RATE;
    if (!s_started) {
        s_started = 1u;
        s_t_ref = now_ms;
        return;
    }
    uint32_t el = now_ms - s_t_ref;
    if (el >= per) {
        uint32_t add = el / per;
        s_t_ref += add * per;
        s_tokens = (s_tokens + add > S0_IDENT_RATE) ? S0_IDENT_RATE : s_tokens + add;
    }
}

/* ---- the reader ---------------------------------------------------------------- */

struct rd {
    const uint8_t *p;
    uint32_t       n, i;
};

static void ws(struct rd *r)
{
    while (r->i < r->n && (r->p[r->i] == ' ' || r->p[r->i] == '\t' ||
                           r->p[r->i] == '\r' || r->p[r->i] == '\n'))
        r->i++;
}

/* "..." with no escapes and no control characters; returns the length, the
 * start in *s, or -1. */
static int str(struct rd *r, const uint8_t **s, uint32_t max)
{
    if (r->i >= r->n || r->p[r->i] != '"')
        return -1;
    uint32_t b = ++r->i;
    while (r->i < r->n && r->p[r->i] != '"') {
        if (r->p[r->i] == '\\' || r->p[r->i] < 0x20u || r->i - b >= max)
            return -1;
        r->i++;
    }
    if (r->i >= r->n)
        return -1;
    *s = r->p + b;
    return (int)(r->i++ - b);
}

static int same(const uint8_t *s, int n, const char *lit)
{
    int i = 0;
    for (; i < n; ++i)
        if (lit[i] == '\0' || (uint8_t)lit[i] != s[i])
            return 0;
    return lit[i] == '\0';
}

static int ishex(uint8_t c)
{
    return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
}

int s0_ident_parse(const uint8_t *p, uint32_t len, char nonce[33])
{
    struct rd r = { p, len, 0 };
    int have_op = 0, have_v = 0, have_nonce = 0, op_ok = 0, v_ok = 0;

    ws(&r);
    if (r.i >= r.n || r.p[r.i++] != '{')
        return -1;
    for (;;) {
        const uint8_t *k, *sv = 0;
        int kl, svl = -1;
        int32_t iv = 0;
        int is_int = 0;

        ws(&r);
        if ((kl = str(&r, &k, 16)) < 1)
            return -1;
        ws(&r);
        if (r.i >= r.n || r.p[r.i++] != ':')
            return -1;
        ws(&r);
        if (r.i >= r.n)
            return -1;
        uint8_t c = r.p[r.i];
        if (c == '"') {
            if ((svl = str(&r, &sv, 64)) < 0)
                return -1;
        } else if (c == '-' || (c >= '0' && c <= '9')) {
            int neg = (c == '-'), nd = 0;
            if (neg)
                r.i++;
            while (r.i < r.n && r.p[r.i] >= '0' && r.p[r.i] <= '9') {
                if (++nd > 9)
                    return -1;
                iv = iv * 10 + (int32_t)(r.p[r.i++] - '0');
            }
            if (nd == 0)
                return -1;
            if (neg)
                iv = -iv;
            is_int = 1;
        } else if (c == 't' || c == 'f' || c == 'n') {
            const char *lit = c == 't' ? "true" : c == 'f' ? "false" : "null";
            for (; *lit; ++lit, ++r.i)
                if (r.i >= r.n || r.p[r.i] != (uint8_t)*lit)
                    return -1;
        } else {
            return -1;                       /* nested object/array: not ours */
        }

        if (same(k, kl, "op")) {
            if (have_op++)
                return -1;
            op_ok = svl >= 0 && same(sv, svl, "identify");
        } else if (same(k, kl, "v")) {
            if (have_v++)
                return -1;
            v_ok = is_int && iv == 1;
        } else if (same(k, kl, "nonce")) {
            if (have_nonce++ || svl < 8 || svl > 32)
                return -1;
            for (int i = 0; i < svl; ++i) {
                if (!ishex(sv[i]))
                    return -1;
                nonce[i] = (char)sv[i];
            }
            nonce[svl] = '\0';
        }                                    /* other keys: tolerated, ignored */

        ws(&r);
        if (r.i >= r.n)
            return -1;
        c = r.p[r.i++];
        if (c == '}')
            break;
        if (c != ',')
            return -1;
    }
    ws(&r);
    while (r.i < r.n && r.p[r.i] == '\0')    /* a C client's trailing NUL */
        r.i++;
    if (r.i != r.n)
        return -1;
    return (op_ok && v_ok && have_nonce) ? 0 : -1;
}

/* ---- the reply ------------------------------------------------------------------ */

void s0_ident_udp(uint32_t src_ip, uint16_t sport, const uint8_t *p, uint32_t len)
{
    static const char hd[] = "0123456789abcdef";
    char nonce[33];
    struct s0_line l;

    if (s0_ident_parse(p, len, nonce) != 0)
        return;                              /* malformed: silent */
    if (s_tokens == 0u)
        return;                              /* rate limit: silent */
    s_tokens--;

    uint32_t n = 0;
#define PUT(s_) do { const char *q_ = (s_); while (*q_ && n < sizeof s_reply - 1u) s_reply[n++] = *q_++; } while (0)
    PUT("{\"ok\":true,\"op\":\"identify\",\"v\":1,\"nonce\":\"");
    PUT(nonce);
    PUT("\",\"board\":\"mps3\",\"mode\":\"rescue\",\"shell_id\":\"0x");
    for (int i = 28; i >= 0 && n < sizeof s_reply - 1u; i -= 4)
        s_reply[n++] = hd[(s_cfg.shell_id >> i) & 0xFu];
    PUT("\",\"ip\":\"");
    s0l_init(&l);
    s0l_ip(&l, s_cfg.ip);
    PUT(l.b);
    PUT("\",\"mac\":\"");
    for (int i = 0; i < 6 && n + 2u < sizeof s_reply; ++i) {
        s_reply[n++] = hd[s_cfg.mac[i] >> 4];
        s_reply[n++] = hd[s_cfg.mac[i] & 0xFu];
    }
    PUT("\",\"reason\":\"");
    PUT(s0_rescue_reason_text(s_st ? s_st->rescue_reason : 0xFFFFu));
    PUT("\",\"ports\":{\"tftp\":69}}");
#undef PUT
    s_reply[n] = '\0';
    if (s0_udp_send(src_ip, S0_IDENT_PORT, sport, s_reply, n) == 0 && s_st)
        s_st->identifies += 1u;
}
