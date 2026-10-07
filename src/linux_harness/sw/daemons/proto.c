/*
 * proto.c — flat-JSON-line codec + push-header unpack + CRC32.
 *
 * Userspace port of firmware/common/net_proto.c (the NORMATIVE wire codec —
 * SERVICE_DISPOSITION.md §2). The tokenizer keeps the exact fail-closed
 * subset: flat objects, string/int/bool/null, no nesting/arrays/\uXXXX,
 * no duplicate keys, max 8 members. Response shapes are byte-compatible
 * with mps3_ctrl_encode_response() in that file.
 */
#include <string.h>
#include <stdio.h>
#include "proto.h"

/* ==== tokenizer ========================================================= */

typedef struct { const char *p, *end; } scan_t;

static int sc_eof(const scan_t *s) { return s->p >= s->end; }

static void sc_ws(scan_t *s)
{
    while (!sc_eof(s) &&
           (*s->p == ' ' || *s->p == '\t' || *s->p == '\r' || *s->p == '\n'))
        s->p++;
}

static int scan_string_span(scan_t *s, const char **span, int *span_len)
{
    if (sc_eof(s) || *s->p != '"')
        return -1;
    s->p++;
    const char *start = s->p;
    while (!sc_eof(s)) {
        unsigned char c = (unsigned char)*s->p;
        if (c == '"') {
            *span = start;
            *span_len = (int)(s->p - start);
            s->p++;
            return 0;
        }
        if (c < 0x20u)
            return -1;                    /* raw control char / NUL */
        if (c == '\\') {
            if (s->p + 1 >= s->end)
                return -1;                /* line ends mid-escape */
            char e = s->p[1];
            if (e == '"' || e == '\\' || e == '/' || e == 'b' || e == 'f' ||
                e == 'n' || e == 'r' || e == 't')
                s->p += 2;
            else
                return -1;                /* \uXXXX + unknown: fail closed */
        } else {
            s->p++;
        }
    }
    return -1;                            /* no closing quote */
}

static int scan_int(scan_t *s, int32_t *out)
{
    int neg = 0;
    if (!sc_eof(s) && *s->p == '-') { neg = 1; s->p++; }
    if (sc_eof(s) || *s->p < '0' || *s->p > '9')
        return -1;
    if (*s->p == '0' && (s->p + 1) < s->end && s->p[1] >= '0' && s->p[1] <= '9')
        return -1;                        /* leading zero */
    long long acc = 0;
    int digits = 0;
    while (!sc_eof(s) && *s->p >= '0' && *s->p <= '9') {
        if (++digits > 10)
            return -1;
        acc = acc * 10 + (*s->p - '0');
        s->p++;
    }
    if (!sc_eof(s) && (*s->p == '.' || *s->p == 'e' || *s->p == 'E'))
        return -1;                        /* fraction/exponent: fail closed */
    if (neg)
        acc = -acc;
    if (acc < (long long)INT32_MIN || acc > (long long)INT32_MAX)
        return -1;
    *out = (int32_t)acc;
    return 0;
}

static int sc_lit(scan_t *s, const char *lit)
{
    size_t n = strlen(lit);
    if ((size_t)(s->end - s->p) < n || memcmp(s->p, lit, n) != 0)
        return 0;
    s->p += n;
    return 1;
}

int mps3_json_parse(const char *line, int len, mps3_json_obj_t *out)
{
    memset(out, 0, sizeof(*out));
    if (line == NULL || len <= 0)
        return MPS3_JSON_EMALFORMED;
    scan_t s = { line, line + len };

    sc_ws(&s);
    if (sc_eof(&s) || *s.p != '{')
        return MPS3_JSON_EMALFORMED;
    s.p++;
    sc_ws(&s);
    if (!sc_eof(&s) && *s.p == '}') {
        s.p++;                            /* empty object: missing "op" later */
    } else {
        for (;;) {
            if (out->n_members >= MPS3_JSON_MAX_MEMBERS)
                return MPS3_JSON_EMALFORMED;
            mps3_json_member_t *m = &out->members[out->n_members];

            sc_ws(&s);
            if (scan_string_span(&s, &m->key, &m->key_len) != 0)
                return MPS3_JSON_EMALFORMED;
            if (memchr(m->key, '\\', (size_t)m->key_len) != NULL)
                return MPS3_JSON_EMALFORMED;   /* escaped key: reject */
            for (int i = 0; i < out->n_members; i++) {
                if (out->members[i].key_len == m->key_len &&
                    memcmp(out->members[i].key, m->key, (size_t)m->key_len) == 0)
                    return MPS3_JSON_EMALFORMED; /* duplicate key */
            }

            sc_ws(&s);
            if (sc_eof(&s) || *s.p != ':')
                return MPS3_JSON_EMALFORMED;
            s.p++;
            sc_ws(&s);
            if (sc_eof(&s))
                return MPS3_JSON_EMALFORMED;

            char c = *s.p;
            if (c == '"') {
                if (scan_string_span(&s, &m->val, &m->val_len) != 0)
                    return MPS3_JSON_EMALFORMED;
                m->type = MPS3_JSON_T_STRING;
            } else if (c == '-' || (c >= '0' && c <= '9')) {
                m->val = s.p;
                if (scan_int(&s, &m->ival) != 0)
                    return MPS3_JSON_EMALFORMED;
                m->val_len = (int)(s.p - m->val);
                m->type = MPS3_JSON_T_INT;
            } else if (sc_lit(&s, "true")) {
                m->type = MPS3_JSON_T_BOOL; m->ival = 1;
            } else if (sc_lit(&s, "false")) {
                m->type = MPS3_JSON_T_BOOL; m->ival = 0;
            } else if (sc_lit(&s, "null")) {
                m->type = MPS3_JSON_T_NULL;
            } else {
                return MPS3_JSON_EMALFORMED;   /* nesting/arrays land here */
            }
            out->n_members++;

            sc_ws(&s);
            if (sc_eof(&s))
                return MPS3_JSON_EMALFORMED;
            if (*s.p == ',') { s.p++; continue; }
            if (*s.p == '}') { s.p++; break; }
            return MPS3_JSON_EMALFORMED;
        }
    }
    sc_ws(&s);
    if (!sc_eof(&s))
        return MPS3_JSON_EMALFORMED;      /* trailing garbage */
    return MPS3_JSON_OK;
}

static const mps3_json_member_t *find_member(const mps3_json_obj_t *obj,
                                             const char *key)
{
    size_t klen = strlen(key);
    for (int i = 0; i < obj->n_members; i++) {
        if ((size_t)obj->members[i].key_len == klen &&
            memcmp(obj->members[i].key, key, klen) == 0)
            return &obj->members[i];
    }
    return NULL;
}

static int unescape_into(const char *raw, int raw_len, char *out, int out_sz)
{
    int o = 0;
    for (int i = 0; i < raw_len; i++) {
        char c = raw[i];
        if (c == '\\') {
            i++;
            switch (raw[i]) {
            case '"':  c = '"';  break;
            case '\\': c = '\\'; break;
            case '/':  c = '/';  break;
            case 'b':  c = '\b'; break;
            case 'f':  c = '\f'; break;
            case 'n':  c = '\n'; break;
            case 'r':  c = '\r'; break;
            case 't':  c = '\t'; break;
            default:   return MPS3_JSON_EMALFORMED;
            }
        }
        if (o >= out_sz - 1)
            return MPS3_JSON_ETOOLONG;
        out[o++] = c;
    }
    out[o] = '\0';
    return MPS3_JSON_OK;
}

int mps3_json_get_string(const mps3_json_obj_t *obj, const char *key,
                         char *out, int out_sz)
{
    if (out == NULL || out_sz <= 0)
        return MPS3_JSON_ETOOLONG;
    out[0] = '\0';
    const mps3_json_member_t *m = find_member(obj, key);
    if (m == NULL)
        return MPS3_JSON_EMISSING;
    if (m->type != MPS3_JSON_T_STRING)
        return MPS3_JSON_ETYPE;
    int rc = unescape_into(m->val, m->val_len, out, out_sz);
    if (rc != MPS3_JSON_OK)
        out[0] = '\0';
    return rc;
}

int mps3_json_get_int(const mps3_json_obj_t *obj, const char *key, int32_t *out)
{
    const mps3_json_member_t *m = find_member(obj, key);
    if (m == NULL)
        return MPS3_JSON_EMISSING;
    if (m->type != MPS3_JSON_T_INT)
        return MPS3_JSON_ETYPE;
    *out = m->ival;
    return MPS3_JSON_OK;
}

int mps3_json_get_bool(const mps3_json_obj_t *obj, const char *key, int *out)
{
    const mps3_json_member_t *m = find_member(obj, key);
    if (m == NULL)
        return MPS3_JSON_EMISSING;
    if (m->type != MPS3_JSON_T_BOOL)
        return MPS3_JSON_ETYPE;
    *out = (int)m->ival;
    return MPS3_JSON_OK;
}

/* ==== verb decode ======================================================= */

static const struct { const char *name; mps3_ctrl_op_t op; } s_op_table[] = {
    { "ping",      MPS3_OP_PING      },
    { "reset",     MPS3_OP_RESET     },
    { "set_clk",   MPS3_OP_SET_CLK   },
    { "swap",      MPS3_OP_SWAP      },
    { "link",      MPS3_OP_LINK      },
    { "commit",    MPS3_OP_COMMIT    },
    { "telemetry", MPS3_OP_TELEMETRY },
    { "macgen",    MPS3_OP_MACGEN    },
    { "diag",      MPS3_OP_DIAG      },
    { "display",   MPS3_OP_DISPLAY   },
    /* NOTE: "stats" is DELIBERATELY absent. fpgahub probes {"op":"stats"}
     * and latches on ok:false/"unknown op" as "verb not supported yet"
     * (SERVICE_DISPOSITION §2A.2). When stats is eventually implemented it
     * must never answer ok:false on success. */
};

int mps3_ctrl_decode_line(const char *line, int len, mps3_ctrl_request_t *out)
{
    memset(out, 0, sizeof(*out));
    out->op = MPS3_OP_UNKNOWN;

    mps3_json_obj_t obj;
    if (mps3_json_parse(line, len, &obj) != MPS3_JSON_OK)
        return MPS3_CTRL_DECODE_EBADJSON;

    char opname[16];
    if (mps3_json_get_string(&obj, "op", opname, sizeof(opname)) != MPS3_JSON_OK)
        return MPS3_CTRL_DECODE_EUNKNOWN_OP;
    for (size_t i = 0; i < sizeof(s_op_table) / sizeof(s_op_table[0]); i++) {
        if (strcmp(opname, s_op_table[i].name) == 0) {
            out->op = s_op_table[i].op;
            break;
        }
    }
    if (out->op == MPS3_OP_UNKNOWN)
        return MPS3_CTRL_DECODE_EUNKNOWN_OP;

    int rc = MPS3_JSON_OK;
    switch (out->op) {
    case MPS3_OP_RESET:
        rc = mps3_json_get_string(&obj, "target", out->target, sizeof(out->target));
        break;
    case MPS3_OP_SET_CLK:
        rc = mps3_json_get_string(&obj, "preset", out->preset, sizeof(out->preset));
        break;
    case MPS3_OP_SWAP:
        rc = mps3_json_get_string(&obj, "rm", out->rm, sizeof(out->rm));
        if (rc == MPS3_JSON_OK)
            rc = mps3_json_get_string(&obj, "src", out->src, sizeof(out->src));
        break;
    case MPS3_OP_LINK:
        rc = mps3_json_get_string(&obj, "event", out->event, sizeof(out->event));
        break;
    case MPS3_OP_COMMIT:
        rc = mps3_json_get_string(&obj, "rm", out->rm, sizeof(out->rm));
        break;
    case MPS3_OP_MACGEN:
        rc = mps3_json_get_bool(&obj, "gen", &out->gen);
        if (rc == MPS3_JSON_OK)
            rc = mps3_json_get_bool(&obj, "chk", &out->chk);
        if (rc == MPS3_JSON_OK)
            rc = mps3_json_get_string(&obj, "inject", out->inject, sizeof(out->inject));
        break;
    case MPS3_OP_DISPLAY:
        rc = mps3_json_get_string(&obj, "owner", out->owner, sizeof(out->owner));
        break;
    default:
        break;    /* ping/telemetry/diag: no arguments */
    }
    if (rc != MPS3_JSON_OK) {
        out->op = MPS3_OP_UNKNOWN;
        return MPS3_CTRL_DECODE_EBADARGS;
    }
    return MPS3_CTRL_DECODE_OK;
}

/* ==== response encode =================================================== */

static void json_escape(const char *in, char *out, int out_sz)
{
    int o = 0;
    for (const char *p = in; *p != '\0' && o < out_sz - 1; p++) {
        unsigned char c = (unsigned char)*p;
        if (c == '"' || c == '\\') {
            if (o >= out_sz - 2)
                break;
            out[o++] = '\\';
            out[o++] = (char)c;
        } else if (c < 0x20u) {
            out[o++] = '?';
        } else {
            out[o++] = (char)c;
        }
    }
    out[o] = '\0';
}

void mps3_format_id_hex(char *dst, size_t dst_sz, uint32_t id)
{
    (void)snprintf(dst, dst_sz, "0x%08x", id);
}

int mps3_ctrl_encode_response(const mps3_ctrl_response_t *resp, char *out, int out_len)
{
    if (out == NULL || out_len <= 0)
        return -1;
    out[0] = '\0';

    int n;
    if (!resp->ok) {
        char esc_err[2 * sizeof(resp->err) + 1];
        json_escape(resp->err, esc_err, (int)sizeof(esc_err));
        if (resp->op == MPS3_OP_TELEMETRY) {
            /* The ONE exception: telemetry has no success shape at all, but
             * lockup (a real DFXCTL.RM_STATUS pin) rides the failure line. */
            n = snprintf(out, (size_t)out_len,
                         "{\"ok\":false,\"err\":\"%s\",\"lockup\":%s}\n",
                         esc_err, resp->lockup ? "true" : "false");
        } else {
            n = snprintf(out, (size_t)out_len,
                         "{\"ok\":false,\"err\":\"%s\"}\n", esc_err);
        }
    } else {
        switch (resp->op) {
        case MPS3_OP_PING: {
            char esc_shell[2 * sizeof(resp->shell_id) + 1];
            char esc_rm[2 * sizeof(resp->rm_id) + 1];
            json_escape(resp->shell_id, esc_shell, (int)sizeof(esc_shell));
            json_escape(resp->rm_id, esc_rm, (int)sizeof(esc_rm));
            n = snprintf(out, (size_t)out_len,
                         "{\"ok\":true,\"shell_id\":\"%s\",\"rm_id\":\"%s\"}\n",
                         esc_shell, esc_rm);
            break;
        }
        case MPS3_OP_RESET:
        case MPS3_OP_LINK:
            n = snprintf(out, (size_t)out_len, "{\"ok\":true}\n");
            break;
        case MPS3_OP_SET_CLK:
            n = snprintf(out, (size_t)out_len, "{\"ok\":true,\"locked\":%s}\n",
                         resp->locked ? "true" : "false");
            break;
        case MPS3_OP_SWAP: {
            char esc_rm[2 * sizeof(resp->rm_id) + 1];
            json_escape(resp->rm_id, esc_rm, (int)sizeof(esc_rm));
            n = snprintf(out, (size_t)out_len,
                         "{\"ok\":true,\"rm_id\":\"%s\",\"verified\":%s}\n",
                         esc_rm, resp->verified ? "true" : "false");
            break;
        }
        case MPS3_OP_COMMIT:
            if (resp->slot != 'A' && resp->slot != 'B')
                return -1;   /* ok-without-slot: caller bug, fail closed */
            n = snprintf(out, (size_t)out_len,
                         "{\"ok\":true,\"slot\":\"%c\"}\n", resp->slot);
            break;
        case MPS3_OP_DISPLAY: {
            char esc_owner[2 * sizeof(resp->owner) + 1];
            json_escape(resp->owner, esc_owner, (int)sizeof(esc_owner));
            n = snprintf(out, (size_t)out_len,
                         "{\"ok\":true,\"owner\":\"%s\"}\n", esc_owner);
            break;
        }
        case MPS3_OP_TELEMETRY:
            /* NO SUCCESS SHAPE EXISTS (no power sensor by construction).
             * ok:true telemetry is unencodable, exactly as in firmware. */
            return -1;
        case MPS3_OP_MACGEN:
            /* success "err" is the u32 ERR_CNT counter (POLYMORPHIC key —
             * clients key on "ok", never on err's type). */
            n = snprintf(out, (size_t)out_len,
                         "{\"ok\":true,\"tx\":%lu,\"rx\":%lu,\"err\":%lu}\n",
                         (unsigned long)resp->tx_cnt,
                         (unsigned long)resp->rx_cnt,
                         (unsigned long)resp->err_cnt);
            break;
        case MPS3_OP_DIAG:
            /* The 14 frozen keys, in the frozen order. */
            n = snprintf(out, (size_t)out_len,
                         "{\"ok\":true,\"rx_recover\":%lu,\"rx_dumps\":%lu,"
                         "\"rx_drops\":%lu,\"icap_bytes\":%lu,\"got\":%lu,"
                         "\"expect\":%lu,\"rcv_wnd\":%lu,\"rcv_ann_wnd\":%lu,"
                         "\"rx_queued\":%lu,\"pbuf_free\":%lu,\"grants_sent\":%lu,"
                         "\"grant_fails\":%lu,\"sndbuf\":%lu,\"snd_wnd\":%lu}\n",
                         (unsigned long)resp->diag_rx_recover,
                         (unsigned long)resp->diag_rx_dumps,
                         (unsigned long)resp->diag_rx_drops,
                         (unsigned long)resp->diag_icap_bytes,
                         (unsigned long)resp->diag_got,
                         (unsigned long)resp->diag_expect,
                         (unsigned long)resp->diag_rcv_wnd,
                         (unsigned long)resp->diag_rcv_ann_wnd,
                         (unsigned long)resp->diag_rx_queued,
                         (unsigned long)resp->diag_pbuf_free,
                         (unsigned long)resp->diag_grants_sent,
                         (unsigned long)resp->diag_grant_fails,
                         (unsigned long)resp->diag_sndbuf,
                         (unsigned long)resp->diag_snd_wnd);
            break;
        default:
            return -1;
        }
    }

    if (n < 0 || n >= out_len) {
        out[0] = '\0';
        return -1;
    }
    return n;
}

/* ==== bitstream push header ============================================= */

static uint16_t get_u16be(const uint8_t *p)
{
    return (uint16_t)(((uint16_t)p[0] << 8) | (uint16_t)p[1]);
}

static uint32_t get_u32be(const uint8_t *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8)  | (uint32_t)p[3];
}

int mps3_bitstream_hdr_unpack(const uint8_t *in, uint32_t in_len,
                              mps3_bitstream_hdr_t *out)
{
    if (in_len < MPS3_BITSTREAM_HDR_WIRE_SIZE)
        return -1;
    if (memcmp(in, MPS3_BITSTREAM_MAGIC, 4) != 0)
        return -1;
    memset(out, 0, sizeof(*out));
    memcpy(out->magic, MPS3_BITSTREAM_MAGIC, 4);
    out->ver       = get_u16be(in + 4);
    out->kind      = in[6];
    out->rm_slot   = in[7];
    out->static_id = get_u32be(in + 8);
    out->rm_id     = get_u32be(in + 12);
    out->len_words = get_u32be(in + 16);
    out->crc32     = get_u32be(in + 20);
    return 0;
}

/* ==== CRC32 (zlib/IEEE) ================================================= */

static uint32_t s_crc_table[256];
static int s_crc_init;

static void crc32_build_table(void)
{
    for (uint32_t i = 0; i < 256; i++) {
        uint32_t c = i;
        for (int k = 0; k < 8; k++)
            c = (c & 1u) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1);
        s_crc_table[i] = c;
    }
    s_crc_init = 1;
}

uint32_t mps3_crc32_update(uint32_t crc, const void *buf, size_t len)
{
    if (!s_crc_init)
        crc32_build_table();
    const uint8_t *p = (const uint8_t *)buf;
    uint32_t c = crc ^ 0xFFFFFFFFu;
    for (size_t i = 0; i < len; i++)
        c = s_crc_table[(c ^ p[i]) & 0xFFu] ^ (c >> 8);
    return c ^ 0xFFFFFFFFu;
}
