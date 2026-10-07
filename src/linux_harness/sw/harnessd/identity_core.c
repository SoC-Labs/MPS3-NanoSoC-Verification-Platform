/*
 * identity_core.c — see identity_core.h. Pure parse / validate / resolve /
 * render, plus three small file helpers. Linked unchanged by mps3-identity (the
 * boot resolver + the board CLI) and by mps3-harnessd (the `identity` verbs,
 * the CLCD rows, identify), so both judge a value by the same code.
 */
#define _GNU_SOURCE
#include "identity_core.h"

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "../../../linux_soc/hw/fw_stage0/stage0_status.h"

#define S0_WORD(field) (__builtin_offsetof(struct s0_status, field) / 4u)

static const char *const k_field[MPS3_ID_NFIELDS] = { "label", "hostname", "ip", "mac" };
static const char *const k_key[MPS3_ID_NFIELDS] = {
    "MPS3_LABEL", "MPS3_HOSTNAME", "MPS3_IP", "MPS3_MAC",
};

const char *mps3_id_field_name(int f)
{
    return (f >= 0 && f < MPS3_ID_NFIELDS) ? k_field[f] : "?";
}

const char *mps3_id_src_name(int src)
{
    switch (src) {
    case MPS3_ID_SRC_OVERRIDE: return "override";
    case MPS3_ID_SRC_STAGE0:   return "stage0";
    case MPS3_ID_SRC_LABEL:    return "label";
    default:                   return "default";
    }
}

const char *mps3_id_s0_state_name(int s)
{
    return s == MPS3_ID_S0_VALID ? "valid" : s == MPS3_ID_S0_INVALID ? "invalid" : "nowindow";
}

static void logf_(mps3_id_log_fn log, void *ctx, const char *fmt, ...)
    __attribute__((format(printf, 3, 4)));
static void logf_(mps3_id_log_fn log, void *ctx, const char *fmt, ...)
{
    if (!log) {
        return;
    }
    char buf[256];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    log(ctx, buf);
}

/* ==========================================================================
 * Validation
 * ========================================================================== */

const char *mps3_id_check_label(const char *s)
{
    size_t n = s ? strlen(s) : 0;
    if (n == 0) {
        return "empty";
    }
    if (n > MPS3_ID_LABEL_MAX) {
        return "longer than 19 characters";
    }
    for (size_t i = 0; i < n; i++) {
        char c = s[i];
        if (!((c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '-')) {
            return "not [A-Z0-9-]";
        }
    }
    return 0;
}

/* RFC 1123: dot-separated labels of 1..63 [A-Za-z0-9-], no label starting or
 * ending with '-', the whole name 1..63 here (Linux HOST_NAME_MAX 64 with NUL). */
const char *mps3_id_check_hostname(const char *s)
{
    size_t n = s ? strlen(s) : 0;
    if (n == 0) {
        return "empty";
    }
    if (n > MPS3_ID_HOSTNAME_MAX) {
        return "longer than 63 characters";
    }
    size_t lab = 0;
    char prev = '.';
    for (size_t i = 0; i < n; i++) {
        char c = s[i];
        if (c == '.') {
            if (lab == 0 || prev == '-') {
                return "not an RFC 1123 host name";
            }
            lab = 0;
        } else if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                   (c >= '0' && c <= '9') || c == '-') {
            if (lab == 0 && c == '-') {
                return "not an RFC 1123 host name";
            }
            lab++;
        } else {
            return "not an RFC 1123 host name";
        }
        prev = c;
    }
    if (lab == 0 || prev == '-') {
        return "not an RFC 1123 host name";
    }
    return 0;
}

const char *mps3_id_check_ip(uint32_t ip, unsigned prefix)
{
    unsigned a = ip >> 24;
    if (prefix < 8u || prefix > 30u) {
        return "prefix not in 8..30";
    }
    if (a == 0u || a == 127u || a >= 224u) {
        return "not a usable host address";
    }
    uint32_t host = (prefix >= 32u) ? 0u : (0xFFFFFFFFu >> prefix);
    if ((ip & host) == 0u) {
        return "the network address of its prefix";
    }
    if ((ip & host) == host) {
        return "the broadcast address of its prefix";
    }
    return 0;
}

const char *mps3_id_parse_ip(const char *s, int need_prefix, unsigned dflt_prefix,
                             uint32_t *ip_out, unsigned *prefix_out)
{
    unsigned o[4] = { 0, 0, 0, 0 };
    unsigned prefix = dflt_prefix;
    const char *p = s;
    if (!s || !*s) {
        return "empty";
    }
    for (int i = 0; i < 4; i++) {
        unsigned v = 0, digits = 0;
        while (*p >= '0' && *p <= '9') {
            v = v * 10u + (unsigned)(*p - '0');
            if (++digits > 3u) {
                return "not a dotted quad";
            }
            p++;
        }
        if (digits == 0u || v > 255u) {
            return "not a dotted quad";
        }
        o[i] = v;
        if (i < 3) {
            if (*p != '.') {
                return "not a dotted quad";
            }
            p++;
        }
    }
    if (*p == '/') {
        p++;
        unsigned v = 0, digits = 0;
        while (*p >= '0' && *p <= '9') {
            v = v * 10u + (unsigned)(*p - '0');
            if (++digits > 2u) {
                return "prefix not in 8..30";
            }
            p++;
        }
        if (digits == 0u) {
            return "no prefix after '/'";
        }
        prefix = v;
    } else if (need_prefix) {
        return "no /prefix";
    }
    if (*p != '\0') {
        return "trailing characters";
    }
    uint32_t ip = (o[0] << 24) | (o[1] << 16) | (o[2] << 8) | o[3];
    const char *why = mps3_id_check_ip(ip, prefix);
    if (why) {
        return why;
    }
    *ip_out = ip;
    *prefix_out = prefix;
    return 0;
}

static int hexval(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

const char *mps3_id_check_mac(const uint8_t mac[6])
{
    if (mac[0] & 1u) {
        return "multicast, not unicast";
    }
    if (!(mac[0] | mac[1] | mac[2] | mac[3] | mac[4] | mac[5])) {
        return "all zero";
    }
    return 0;
}

const char *mps3_id_parse_mac(const char *s, uint8_t mac[6])
{
    size_t n = s ? strlen(s) : 0;
    uint8_t m[6];
    if (n == 12u) {
        for (int i = 0; i < 6; i++) {
            int h = hexval(s[2 * i]), l = hexval(s[2 * i + 1]);
            if (h < 0 || l < 0) {
                return "not 12 hex digits";
            }
            m[i] = (uint8_t)(h << 4 | l);
        }
    } else if (n == 17u) {
        char sep = s[2];
        if (sep != ':' && sep != '-') {
            return "not 12 hex digits";
        }
        for (int i = 0; i < 6; i++) {
            int h = hexval(s[3 * i]), l = hexval(s[3 * i + 1]);
            if (h < 0 || l < 0 || (i < 5 && s[3 * i + 2] != sep)) {
                return "not 12 hex digits";
            }
            m[i] = (uint8_t)(h << 4 | l);
        }
    } else {
        return "not 12 hex digits";
    }
    const char *why = mps3_id_check_mac(m);
    if (why) {
        return why;
    }
    memcpy(mac, m, 6);
    return 0;
}

/* ==========================================================================
 * Rendering
 * ========================================================================== */

void mps3_id_fmt_ip(uint32_t ip, char *out, size_t cap)
{
    snprintf(out, cap, "%u.%u.%u.%u", (unsigned)(ip >> 24), (unsigned)(ip >> 16) & 255u,
             (unsigned)(ip >> 8) & 255u, (unsigned)ip & 255u);
}

void mps3_id_fmt_cidr(uint32_t ip, unsigned prefix, char *out, size_t cap)
{
    char a[16];
    mps3_id_fmt_ip(ip, a, sizeof(a));
    snprintf(out, cap, "%s/%u", a, prefix);
}

void mps3_id_fmt_mac12(const uint8_t m[6], char out[13])
{
    snprintf(out, 13, "%02x%02x%02x%02x%02x%02x", m[0], m[1], m[2], m[3], m[4], m[5]);
}

void mps3_id_fmt_mac_colon(const uint8_t m[6], char out[18])
{
    snprintf(out, 18, "%02x:%02x:%02x:%02x:%02x:%02x", m[0], m[1], m[2], m[3], m[4], m[5]);
}

/* ==========================================================================
 * The sources
 * ========================================================================== */

void mps3_id_defaults(mps3_id_fields_t *out)
{
    static const uint8_t mac[6] = MPS3_ID_DEFAULT_MAC;
    memset(out, 0, sizeof(*out));
    snprintf(out->label, sizeof(out->label), "%s", MPS3_ID_DEFAULT_LABEL);
    out->ip = MPS3_ID_DEFAULT_IP;
    out->prefix = MPS3_ID_DEFAULT_PREFIX;
    memcpy(out->mac, mac, 6);
    out->have = MPS3_ID_F(MPS3_ID_LABEL) | MPS3_ID_F(MPS3_ID_IP) | MPS3_ID_F(MPS3_ID_MAC);
}

/* Apply one validated value for field f to `o` (shared by the override parser
 * and the edit). NULL = applied. */
static const char *set_field(mps3_id_fields_t *o, int f, const char *val)
{
    switch (f) {
    case MPS3_ID_LABEL: {
        const char *why = mps3_id_check_label(val);
        if (why) return why;
        snprintf(o->label, sizeof(o->label), "%s", val);
        break;
    }
    case MPS3_ID_HOSTNAME: {
        const char *why = mps3_id_check_hostname(val);
        if (why) return why;
        snprintf(o->hostname, sizeof(o->hostname), "%s", val);
        break;
    }
    case MPS3_ID_IP: {
        uint32_t ip;
        unsigned pfx;
        const char *why = mps3_id_parse_ip(val, 0, MPS3_ID_DEFAULT_PREFIX, &ip, &pfx);
        if (why) return why;
        o->ip = ip;
        o->prefix = pfx;
        break;
    }
    case MPS3_ID_MAC: {
        const char *why = mps3_id_parse_mac(val, o->mac);
        if (why) return why;
        break;
    }
    default:
        return "unknown key";
    }
    o->have |= MPS3_ID_F(f);
    return 0;
}

int mps3_id_parse_override(const char *text, size_t n, mps3_id_fields_t *out,
                           mps3_id_log_fn log, void *ctx)
{
    int bad = 0;
    unsigned lineno = 0;
    memset(out, 0, sizeof(*out));
    size_t i = 0;
    while (i < n) {
        size_t e = i;
        while (e < n && text[e] != '\n') {
            e++;
        }
        lineno++;
        char line[256];
        size_t len = e - i;
        int too_long = len >= sizeof(line);
        if (too_long) {
            len = sizeof(line) - 1u;
        }
        memcpy(line, text + i, len);
        line[len] = '\0';
        i = e + 1u;
        if (too_long || memchr(line, 0, len) != NULL) {
            logf_(log, ctx, "identity override line %u: too long or not text -- ignored", lineno);
            bad++;
            continue;
        }
        /* trim CR and surrounding blanks */
        char *s = line;
        while (*s == ' ' || *s == '\t') s++;
        size_t sl = strlen(s);
        while (sl && (s[sl - 1] == '\r' || s[sl - 1] == ' ' || s[sl - 1] == '\t')) s[--sl] = '\0';
        if (*s == '\0' || *s == '#') {
            continue;
        }
        char *eq = strchr(s, '=');
        if (!eq) {
            logf_(log, ctx, "identity override line %u: not KEY=VALUE -- ignored", lineno);
            bad++;
            continue;
        }
        *eq = '\0';
        char *val = eq + 1;
        size_t vl = strlen(val);
        if (vl >= 2u && (val[0] == '\'' || val[0] == '"') && val[vl - 1] == val[0]) {
            val[vl - 1] = '\0';
            val++;
        }
        int f = -1;
        for (int k = 0; k < MPS3_ID_NFIELDS; k++) {
            if (strcmp(s, k_key[k]) == 0) {
                f = k;
            }
        }
        if (f < 0) {
            logf_(log, ctx, "identity override line %u: unknown key '%.40s' -- ignored", lineno, s);
            bad++;
            continue;
        }
        mps3_id_fields_t tmp = *out;
        const char *why = set_field(&tmp, f, val);
        if (why) {
            logf_(log, ctx, "identity override line %u: %s '%.64s' refused (%s) -- ignored",
                  lineno, k_key[f], val, why);
            bad++;
            continue;
        }
        *out = tmp;
    }
    return bad;
}

int mps3_id_from_stage0(const volatile uint32_t *b, mps3_id_fields_t *out,
                        mps3_id_log_fn log, void *ctx)
{
    memset(out, 0, sizeof(*out));
    if (!b) {
        return MPS3_ID_S0_NOWINDOW;
    }
    /* identity.c's acceptance rule, word for word: a newer layout version is
     * still read (the fields are append-only). */
    if (b[S0_WORD(magic)] != S0_STATUS_MAGIC || b[S0_WORD(version)] < S0_STATUS_VERSION ||
        b[S0_WORD(size)] != S0_STATUS_BYTES || b[S0_WORD(magic_end)] != S0_STATUS_MAGIC) {
        return MPS3_ID_S0_INVALID;
    }
    uint32_t ip = b[S0_WORD(ip_addr)];
    uint32_t mlo = b[S0_WORD(mac_lo)], mhi = b[S0_WORD(mac_hi)];
    uint32_t llo = b[S0_WORD(label_lo)], lhi = b[S0_WORD(label_hi)];
    if (ip != 0u) {
        const char *why = mps3_id_check_ip(ip, MPS3_ID_STAGE0_PREFIX);
        if (why) {
            logf_(log, ctx, "stage0 ip_addr 0x%08x refused (%s) -- ignored", (unsigned)ip, why);
        } else {
            out->ip = ip;
            out->prefix = MPS3_ID_STAGE0_PREFIX;
            out->have |= MPS3_ID_F(MPS3_ID_IP);
        }
    }
    if (mlo != 0u || (mhi & 0xFFFFu) != 0u) {
        uint8_t m[6] = { (uint8_t)mlo, (uint8_t)(mlo >> 8), (uint8_t)(mlo >> 16),
                         (uint8_t)(mlo >> 24), (uint8_t)mhi, (uint8_t)(mhi >> 8) };
        const char *why = (mhi >> 16) ? "mac_hi[31:16] not 0" : mps3_id_check_mac(m);
        if (why) {
            logf_(log, ctx, "stage0 mac 0x%08x/0x%08x refused (%s) -- ignored",
                  (unsigned)mlo, (unsigned)mhi, why);
        } else {
            memcpy(out->mac, m, 6);
            out->have |= MPS3_ID_F(MPS3_ID_MAC);
        }
    }
    if (llo != 0u || lhi != 0u) {
        char lab[S0_LABEL_MAX + 1u];
        int bad = 0, end = 0;
        for (unsigned i = 0; i < S0_LABEL_MAX; i++) {
            uint32_t w = i < 4u ? llo : lhi;
            char c = (char)(w >> (8u * (i & 3u)));
            if (end) {
                bad |= (c != '\0');          /* NUL-padded: nothing after the end */
            } else if (c == '\0') {
                end = 1;
            }
            lab[i] = c;
        }
        lab[S0_LABEL_MAX] = '\0';
        const char *why = bad ? "not NUL-padded" : mps3_id_check_label(lab);
        if (why) {
            logf_(log, ctx, "stage0 label 0x%08x/0x%08x refused (%s) -- ignored",
                  (unsigned)llo, (unsigned)lhi, why);
        } else {
            snprintf(out->label, sizeof(out->label), "%s", lab);
            out->have |= MPS3_ID_F(MPS3_ID_LABEL);
        }
    }
    return MPS3_ID_S0_VALID;
}

void mps3_id_resolve(const mps3_id_fields_t *ovr, const mps3_id_fields_t *s0,
                     mps3_identity_t *out)
{
    mps3_id_fields_t d;
    mps3_id_defaults(&d);
    memset(out, 0, sizeof(*out));
    const mps3_id_fields_t *src[3] = { ovr, s0, &d };
    const int srcid[3] = { MPS3_ID_SRC_OVERRIDE, MPS3_ID_SRC_STAGE0, MPS3_ID_SRC_DEFAULT };

    for (int s = 2; s >= 0; s--) {   /* lowest precedence first; later wins */
        const mps3_id_fields_t *f = src[s];
        if (!f) {
            continue;
        }
        if (f->have & MPS3_ID_F(MPS3_ID_LABEL)) {
            snprintf(out->v.label, sizeof(out->v.label), "%s", f->label);
            out->src[MPS3_ID_LABEL] = srcid[s];
        }
        if (f->have & MPS3_ID_F(MPS3_ID_IP)) {
            out->v.ip = f->ip;
            out->v.prefix = f->prefix;
            out->src[MPS3_ID_IP] = srcid[s];
        }
        if (f->have & MPS3_ID_F(MPS3_ID_MAC)) {
            memcpy(out->v.mac, f->mac, 6);
            out->src[MPS3_ID_MAC] = srcid[s];
        }
    }
    if (ovr && (ovr->have & MPS3_ID_F(MPS3_ID_HOSTNAME))) {
        snprintf(out->v.hostname, sizeof(out->v.hostname), "%s", ovr->hostname);
        out->src[MPS3_ID_HOSTNAME] = MPS3_ID_SRC_OVERRIDE;
    } else {
        char h[MPS3_ID_LABEL_MAX + 1];
        size_t i;
        for (i = 0; out->v.label[i] && i < MPS3_ID_LABEL_MAX; i++) {
            char c = out->v.label[i];
            h[i] = (c >= 'A' && c <= 'Z') ? (char)(c - 'A' + 'a') : c;
        }
        h[i] = '\0';
        if (mps3_id_check_hostname(h) == 0) {
            snprintf(out->v.hostname, sizeof(out->v.hostname), "%s", h);
            out->src[MPS3_ID_HOSTNAME] = MPS3_ID_SRC_LABEL;
        } else {
            /* e.g. a label "-X-": no valid host name derives from it */
            snprintf(out->v.hostname, sizeof(out->v.hostname), "mps3");
            out->src[MPS3_ID_HOSTNAME] = MPS3_ID_SRC_DEFAULT;
        }
    }
    out->v.have = MPS3_ID_F(MPS3_ID_LABEL) | MPS3_ID_F(MPS3_ID_HOSTNAME) |
                  MPS3_ID_F(MPS3_ID_IP) | MPS3_ID_F(MPS3_ID_MAC);
}

unsigned mps3_id_diff(const mps3_identity_t *a, const mps3_identity_t *b)
{
    unsigned d = 0;
    if (strcmp(a->v.label, b->v.label) != 0) d |= MPS3_ID_F(MPS3_ID_LABEL);
    if (strcmp(a->v.hostname, b->v.hostname) != 0) d |= MPS3_ID_F(MPS3_ID_HOSTNAME);
    if (a->v.ip != b->v.ip || a->v.prefix != b->v.prefix) d |= MPS3_ID_F(MPS3_ID_IP);
    if (memcmp(a->v.mac, b->v.mac, 6) != 0) d |= MPS3_ID_F(MPS3_ID_MAC);
    return d;
}

/* ==========================================================================
 * /run/mps3/identity and the override file
 * ========================================================================== */

int mps3_id_render_run(const mps3_identity_t *id, int s0_state, int persist,
                       char *out, size_t cap)
{
    char ip[24], mac[18];
    mps3_id_fmt_cidr(id->v.ip, id->v.prefix, ip, sizeof(ip));
    mps3_id_fmt_mac_colon(id->v.mac, mac);
    int n = snprintf(out, cap,
        "# /run/mps3/identity -- this boot's resolved identity (mps3-identity resolve,\n"
        "# S13mps3identity). Sourced by S41mps3net; read by mps3-harnessd. Do not edit:\n"
        "# change it with `mps3-identity set k=v` (applies at the next boot).\n"
        "MPS3_LABEL=%s\nMPS3_HOSTNAME=%s\nMPS3_IP=%s\nMPS3_MAC=%s\n"
        "MPS3_LABEL_SRC=%s\nMPS3_HOSTNAME_SRC=%s\nMPS3_IP_SRC=%s\nMPS3_MAC_SRC=%s\n"
        "MPS3_STAGE0=%s\nMPS3_PERSIST=%d\n",
        id->v.label, id->v.hostname, ip, mac,
        mps3_id_src_name(id->src[MPS3_ID_LABEL]), mps3_id_src_name(id->src[MPS3_ID_HOSTNAME]),
        mps3_id_src_name(id->src[MPS3_ID_IP]), mps3_id_src_name(id->src[MPS3_ID_MAC]),
        mps3_id_s0_state_name(s0_state), persist ? 1 : 0);
    return (n < 0 || (size_t)n >= cap) ? -1 : n;
}

static int src_by_name(const char *s)
{
    if (strcmp(s, "override") == 0) return MPS3_ID_SRC_OVERRIDE;
    if (strcmp(s, "stage0") == 0) return MPS3_ID_SRC_STAGE0;
    if (strcmp(s, "label") == 0) return MPS3_ID_SRC_LABEL;
    return MPS3_ID_SRC_DEFAULT;
}

int mps3_id_parse_run(const char *text, size_t n, mps3_identity_t *out)
{
    mps3_id_fields_t f;
    memset(out, 0, sizeof(*out));
    /* the values reuse the override grammar; the *_SRC keys are read here */
    (void)mps3_id_parse_override(text, n, &f, 0, 0);
    if (f.have != (MPS3_ID_F(MPS3_ID_LABEL) | MPS3_ID_F(MPS3_ID_HOSTNAME) |
                   MPS3_ID_F(MPS3_ID_IP) | MPS3_ID_F(MPS3_ID_MAC))) {
        return -1;
    }
    out->v = f;
    for (int k = 0; k < MPS3_ID_NFIELDS; k++) {
        out->src[k] = MPS3_ID_SRC_DEFAULT;
    }
    size_t i = 0;
    while (i < n) {
        size_t e = i;
        while (e < n && text[e] != '\n') e++;
        char line[128];
        size_t len = e - i < sizeof(line) - 1u ? e - i : sizeof(line) - 1u;
        memcpy(line, text + i, len);
        line[len] = '\0';
        i = e + 1u;
        for (int k = 0; k < MPS3_ID_NFIELDS; k++) {
            char key[32];
            snprintf(key, sizeof(key), "%s_SRC=", k_key[k]);
            if (strncmp(line, key, strlen(key)) == 0) {
                out->src[k] = src_by_name(line + strlen(key));
            }
        }
    }
    return 0;
}

int mps3_id_render_override(const mps3_id_fields_t *o, char *out, size_t cap)
{
    size_t used = 0;
    int n = snprintf(out, cap,
        "# /persist/etc/mps3/identity -- this board's identity OVERRIDE (mps3-identity\n"
        "# set / the 6900 verb identity_set). Each key beats the stage0 bake and the\n"
        "# image default; it applies at the next boot. `mps3-identity clear` removes it.\n");
    if (n < 0 || (size_t)n >= cap) return -1;
    used = (size_t)n;
    for (int k = 0; k < MPS3_ID_NFIELDS; k++) {
        if (!(o->have & MPS3_ID_F(k))) {
            continue;
        }
        char v[80];
        if (k == MPS3_ID_LABEL) snprintf(v, sizeof(v), "%s", o->label);
        else if (k == MPS3_ID_HOSTNAME) snprintf(v, sizeof(v), "%s", o->hostname);
        else if (k == MPS3_ID_IP) mps3_id_fmt_cidr(o->ip, o->prefix, v, sizeof(v));
        else mps3_id_fmt_mac_colon(o->mac, v);
        n = snprintf(out + used, cap - used, "%s=%s\n", k_key[k], v);
        if (n < 0 || (size_t)n >= cap - used) return -1;
        used += (size_t)n;
    }
    return (int)used;
}

const char *mps3_id_apply_edit(mps3_id_fields_t *ovr, const char *key, const char *val)
{
    int f = -1;
    for (int k = 0; k < MPS3_ID_NFIELDS; k++) {
        if (strcmp(key, k_field[k]) == 0) {
            f = k;
        }
    }
    if (f < 0) {
        return "unknown key";
    }
    if (!val || !*val) {
        ovr->have &= ~MPS3_ID_F(f);    /* "" = drop this key from the override */
        return 0;
    }
    mps3_id_fields_t tmp = *ovr;
    const char *why = set_field(&tmp, f, val);
    if (!why) {
        *ovr = tmp;
    }
    return why;
}

/* ==========================================================================
 * The `identity` reply body (the 6900 verb and `mps3-identity get --json`)
 * ========================================================================== */

/* Every value that reaches JSON here passed the rules above ([A-Z0-9-], an RFC
 * 1123 name, digits, hex): nothing can need escaping. */
static int j_obj(const mps3_id_fields_t *f, unsigned mask, int nulls, char *out, size_t cap)
{
    size_t used = 0;
    int first = 1;
    int n = snprintf(out, cap, "{");
    if (n < 0 || (size_t)n >= cap) return -1;
    used = (size_t)n;
    for (int k = 0; k < MPS3_ID_NFIELDS; k++) {
        if (!(mask & MPS3_ID_F(k))) {
            continue;
        }
        char v[80];
        int have = (f->have & MPS3_ID_F(k)) != 0;
        if (!have && !nulls) {
            continue;
        }
        if (!have) snprintf(v, sizeof(v), "null");
        else if (k == MPS3_ID_LABEL) snprintf(v, sizeof(v), "\"%s\"", f->label);
        else if (k == MPS3_ID_HOSTNAME) snprintf(v, sizeof(v), "\"%s\"", f->hostname);
        else if (k == MPS3_ID_IP) {
            char c[24];
            mps3_id_fmt_cidr(f->ip, f->prefix, c, sizeof(c));
            snprintf(v, sizeof(v), "\"%s\"", c);
        } else {
            char m[13];
            mps3_id_fmt_mac12(f->mac, m);
            snprintf(v, sizeof(v), "\"%s\"", m);
        }
        n = snprintf(out + used, cap - used, "%s\"%s\":%s", first ? "" : ",", k_field[k], v);
        if (n < 0 || (size_t)n >= cap - used) return -1;
        used += (size_t)n;
        first = 0;
    }
    n = snprintf(out + used, cap - used, "}");
    if (n < 0 || (size_t)n >= cap - used) return -1;
    return (int)(used + (size_t)n);
}

int mps3_id_json_pending(const mps3_identity_t *next, const mps3_identity_t *running,
                         char *out, size_t cap)
{
    unsigned d = mps3_id_diff(next, running);
    if (!d) {
        int n = snprintf(out, cap, "null");
        return (n < 0 || (size_t)n >= cap) ? -1 : n;
    }
    return j_obj(&next->v, d, 0, out, cap);
}

int mps3_id_json_status(const mps3_id_status_t *st, char *out, size_t cap)
{
    char s0[160], ovr[200], pend[200], ip[24], mac[13];
    const mps3_identity_t *r = &st->running;
    if (st->s0_state == MPS3_ID_S0_VALID) {
        if (j_obj(&st->s0, MPS3_ID_F(MPS3_ID_LABEL) | MPS3_ID_F(MPS3_ID_IP) | MPS3_ID_F(MPS3_ID_MAC),
                  1, s0, sizeof(s0)) < 0) return -1;
    } else {
        snprintf(s0, sizeof(s0), "null");
    }
    if (st->ovr_present) {
        if (j_obj(&st->ovr, 0xFu, 0, ovr, sizeof(ovr)) < 0) return -1;
    } else {
        snprintf(ovr, sizeof(ovr), "null");
    }
    if (mps3_id_json_pending(&st->next, r, pend, sizeof(pend)) < 0) return -1;
    mps3_id_fmt_cidr(r->v.ip, r->v.prefix, ip, sizeof(ip));
    mps3_id_fmt_mac12(r->v.mac, mac);
    int n = snprintf(out, cap,
        "\"label\":\"%s\",\"hostname\":\"%s\",\"ip\":\"%s\",\"mac\":\"%s\","
        "\"source\":{\"label\":\"%s\",\"hostname\":\"%s\",\"ip\":\"%s\",\"mac\":\"%s\"},"
        "\"stage0\":%s,\"override\":%s,\"pending\":%s,\"persist\":%s",
        r->v.label, r->v.hostname, ip, mac,
        mps3_id_src_name(r->src[MPS3_ID_LABEL]), mps3_id_src_name(r->src[MPS3_ID_HOSTNAME]),
        mps3_id_src_name(r->src[MPS3_ID_IP]), mps3_id_src_name(r->src[MPS3_ID_MAC]),
        s0, ovr, pend, st->persist ? "true" : "false");
    return (n < 0 || (size_t)n >= cap) ? -1 : n;
}

/* ==========================================================================
 * Files
 * ========================================================================== */

int mps3_id_read_file(const char *path, char *buf, size_t cap)
{
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) {
        return -1;
    }
    size_t used = 0;
    for (;;) {
        if (used + 1u >= cap) {
            char c;
            ssize_t r = read(fd, &c, 1);
            close(fd);
            buf[used] = '\0';
            return r > 0 ? -2 : (int)used;
        }
        ssize_t r = read(fd, buf + used, cap - 1u - used);
        if (r < 0) {
            if (errno == EINTR) continue;
            close(fd);
            return -1;
        }
        if (r == 0) {
            break;
        }
        used += (size_t)r;
    }
    close(fd);
    buf[used] = '\0';
    return (int)used;
}

static int fsync_dir_of(const char *path)
{
    char dir[PATH_MAX];
    snprintf(dir, sizeof(dir), "%s", path);
    char *sl = strrchr(dir, '/');
    if (sl == dir) {
        sl[1] = '\0';
    } else if (sl) {
        *sl = '\0';
    } else {
        snprintf(dir, sizeof(dir), ".");
    }
    int fd = open(dir, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (fd < 0) {
        return -errno;
    }
    int rc = fsync(fd) == 0 ? 0 : -errno;
    close(fd);
    return rc;
}

static int mkdir_p(const char *dirpath)
{
    char d[PATH_MAX];
    snprintf(d, sizeof(d), "%s", dirpath);
    for (char *p = d + 1; *p; p++) {
        if (*p == '/') {
            *p = '\0';
            if (mkdir(d, 0755) != 0 && errno != EEXIST) return -errno;
            *p = '/';
        }
    }
    if (mkdir(d, 0755) != 0 && errno != EEXIST) return -errno;
    return 0;
}

int mps3_id_write_atomic(const char *path, const char *data, size_t n, unsigned mode)
{
    char dir[PATH_MAX], tmp[PATH_MAX + 16];
    snprintf(dir, sizeof(dir), "%s", path);
    char *sl = strrchr(dir, '/');
    if (sl && sl != dir) {
        *sl = '\0';
        int rc = mkdir_p(dir);
        if (rc) return rc;
    }
    snprintf(tmp, sizeof(tmp), "%s.tmp.%ld", path, (long)getpid());
    int fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, (mode_t)mode);
    if (fd < 0) {
        return -errno;
    }
    size_t off = 0;
    while (off < n) {
        ssize_t w = write(fd, data + off, n - off);
        if (w < 0) {
            if (errno == EINTR) continue;
            int e = -errno;
            close(fd);
            unlink(tmp);
            return e;
        }
        off += (size_t)w;
    }
    if (fsync(fd) != 0) {
        int e = -errno;
        close(fd);
        unlink(tmp);
        return e;
    }
    close(fd);
    if (rename(tmp, path) != 0) {
        int e = -errno;
        unlink(tmp);
        return e;
    }
    return fsync_dir_of(path);
}

int mps3_id_remove(const char *path)
{
    if (unlink(path) != 0) {
        return errno == ENOENT ? 0 : -errno;
    }
    return fsync_dir_of(path);
}

/* ==========================================================================
 * The shared operations
 * ========================================================================== */

int mps3_id_persist_ok(const char *path)
{
    char buf[256];
    if (!path || mps3_id_read_file(path, buf, sizeof(buf)) < 0) {
        return 0;
    }
    return strncmp(buf, "backing=card", 12) == 0 &&
           (buf[12] == ' ' || buf[12] == '\n' || buf[12] == '\0' || buf[12] == '\t');
}

int mps3_id_load_override(const char *path, int persist, mps3_id_fields_t *ovr,
                          mps3_id_log_fn log, void *ctx)
{
    char buf[MPS3_ID_FILE_MAX + 1u];
    memset(ovr, 0, sizeof(*ovr));
    if (!persist || !path) {
        return 0;
    }
    int n = mps3_id_read_file(path, buf, sizeof(buf));
    if (n == -2) {
        logf_(log, ctx, "identity override %s is larger than %u B -- ignored", path,
              (unsigned)MPS3_ID_FILE_MAX);
        return 0;
    }
    if (n < 0) {
        return 0;
    }
    int bad = mps3_id_parse_override(buf, (size_t)n, ovr, log, ctx);
    if (bad) {
        logf_(log, ctx, "identity override %s: %d line(s) ignored (above)", path, bad);
    }
    return 1;
}

void mps3_id_gather(const char *override_path, int persist, const volatile uint32_t *s0blk,
                    const mps3_identity_t *running, mps3_id_status_t *st,
                    mps3_id_log_fn log, void *ctx)
{
    memset(st, 0, sizeof(*st));
    st->running = *running;
    st->persist = persist;
    st->s0_state = mps3_id_from_stage0(s0blk, &st->s0, log, ctx);
    st->ovr_present = mps3_id_load_override(override_path, persist, &st->ovr, log, ctx);
    mps3_id_resolve(st->ovr_present ? &st->ovr : 0,
                    st->s0_state == MPS3_ID_S0_VALID ? &st->s0 : 0, &st->next);
}

int mps3_id_do_set(const char *override_path, int persist, int clear,
                   const char *const *keys, const char *const *vals, int n,
                   const char **bad_field, const char **why)
{
    static char errbuf[96];
    *bad_field = 0;
    *why = 0;
    if (!persist) {
        *why = "no persistent /persist (the card's p3): an override would not survive";
        return MPS3_ID_ENOPERSIST;
    }
    mps3_id_fields_t ovr;
    (void)mps3_id_load_override(override_path, persist, &ovr, 0, 0);
    for (int i = 0; !clear && i < n; i++) {
        const char *w = mps3_id_apply_edit(&ovr, keys[i], vals[i]);
        if (w) {
            *bad_field = keys[i];
            *why = w;
            return MPS3_ID_EINVALID;
        }
    }
    if (clear || ovr.have == 0u) {         /* nothing left to override: no file */
        int rc = mps3_id_remove(override_path);
        if (rc) {
            snprintf(errbuf, sizeof(errbuf), "remove: %s", strerror(-rc));
            *why = errbuf;
            return MPS3_ID_EIO;
        }
        return MPS3_ID_OK;
    }
    char text[1024];
    int len = mps3_id_render_override(&ovr, text, sizeof(text));
    if (len < 0) {
        *why = "render";
        return MPS3_ID_EIO;
    }
    int rc = mps3_id_write_atomic(override_path, text, (size_t)len, 0644);
    if (rc) {
        snprintf(errbuf, sizeof(errbuf), "write: %s", strerror(-rc));
        *why = errbuf;
        return MPS3_ID_EIO;
    }
    return MPS3_ID_OK;
}
