/*
 * net_proto.c — control-channel (TCP 6900) JSON-line encode/decode.
 *
 * Lives in common/ rather than coordinator/ because it is pure wire-format
 * logic shared by nothing else's business rules — coordinator/coordinator.c
 * calls the decode/encode pair but owns none of the parsing itself.
 *
 * The tokenizer here implements exactly net_proto.h's documented subset —
 * FLAT one-line JSON objects, string/int/bool/null values only, bounded,
 * fail-closed (see the guarantees block in the header). Reference client
 * this must interoperate with byte-for-byte: host/pyverify/pyverify/
 * client.py (json.dumps(op, separators=(",",":")) requests; json.loads
 * responses) — cross-checked by tests/firmware_logic/test_json_golden.py.
 * Host-gcc unit tests: firmware/test/test_net_proto_json.c.
 */
#include <string.h>
#include <stdio.h>
#include "net_proto.h"

/* ==========================================================================
 * Flat-JSON-line tokenizer
 * ========================================================================== */

/* Bounded cursor over the (possibly non-NUL-terminated) wire line. Every
 * accessor below checks p < end before dereferencing — the "never over-read"
 * guarantee lives entirely in this discipline. */
typedef struct {
    const char *p;
    const char *end;
} json_scan_t;

static int sc_eof(const json_scan_t *s)
{
    return s->p >= s->end;
}

static void sc_skip_ws(json_scan_t *s)
{
    while (!sc_eof(s) &&
           (*s->p == ' ' || *s->p == '\t' || *s->p == '\r' || *s->p == '\n')) {
        s->p++;
    }
}

/* Scans a quoted string starting at the opening '"'; on success leaves the
 * cursor past the closing quote and returns the RAW span between the quotes
 * (escapes validated but not resolved — see unescape_into()). Escape policy
 * per net_proto.h: the six single-char escapes plus \" \\ \/ are legal;
 * \uXXXX and anything else is rejected. Raw control chars (< 0x20, which
 * also covers embedded NULs) are rejected per the JSON grammar. */
static int scan_string_span(json_scan_t *s, const char **span, int *span_len)
{
    if (sc_eof(s) || *s->p != '"') {
        return -1;
    }
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
        if (c < 0x20u) {
            return -1; /* raw control char / embedded NUL */
        }
        if (c == '\\') {
            if (s->p + 1 >= s->end) {
                return -1; /* line ends mid-escape */
            }
            char e = s->p[1];
            if (e == '"' || e == '\\' || e == '/' || e == 'b' || e == 'f' ||
                e == 'n' || e == 'r' || e == 't') {
                s->p += 2;
            } else {
                return -1; /* \uXXXX + unknown escapes: fail closed */
            }
        } else {
            s->p++;
        }
    }
    return -1; /* no closing quote before end of line (truncated) */
}

/* Strict JSON integer: -?(0|[1-9][0-9]*), no fraction, no exponent, no
 * leading zeros (all three are either invalid JSON or outside the contract
 * — no verb carries a non-integer number — so all three are rejected).
 * Range-checked to int32. */
static int scan_int(json_scan_t *s, int32_t *out)
{
    int neg = 0;
    if (!sc_eof(s) && *s->p == '-') {
        neg = 1;
        s->p++;
    }
    if (sc_eof(s) || *s->p < '0' || *s->p > '9') {
        return -1;
    }
    if (*s->p == '0' && (s->p + 1) < s->end && s->p[1] >= '0' && s->p[1] <= '9') {
        return -1; /* leading zero */
    }
    long long acc = 0;
    int digits = 0;
    while (!sc_eof(s) && *s->p >= '0' && *s->p <= '9') {
        if (++digits > 10) {
            return -1; /* > 10 digits can never fit int32 */
        }
        acc = acc * 10 + (*s->p - '0');
        s->p++;
    }
    if (!sc_eof(s) && (*s->p == '.' || *s->p == 'e' || *s->p == 'E')) {
        return -1; /* fraction/exponent: not in the contract, fail closed */
    }
    if (neg) {
        acc = -acc;
    }
    if (acc < (long long)INT32_MIN || acc > (long long)INT32_MAX) {
        return -1;
    }
    *out = (int32_t)acc;
    return 0;
}

/* Matches a bare literal (`true`/`false`/`null`) at the cursor, bounded. */
static int sc_match_lit(json_scan_t *s, const char *lit)
{
    size_t n = strlen(lit);
    if ((size_t)(s->end - s->p) < n || memcmp(s->p, lit, n) != 0) {
        return 0;
    }
    s->p += n;
    return 1;
}

/* v0.17: validates one FLAT object at the cursor ('{' ... '}': string keys,
 * scalar values -- the same grammar mps3_json_parse() accepts, nothing nested)
 * without storing it, and returns its span including both braces. Used only by
 * mps3_json_parse_ex() with allow_objects (the Linux harness's `hello`, whose
 * `lease` / `job` are the protocol's first nested request values): the member
 * table is filled later, by mps3_json_get_object() re-parsing the span. */
static int scan_flat_object_span(json_scan_t *s, const char **span, int *span_len)
{
    const char *start = s->p;
    if (sc_eof(s) || *s->p != '{') {
        return -1;
    }
    s->p++;
    sc_skip_ws(s);
    if (!sc_eof(s) && *s->p == '}') {
        s->p++;
        *span = start;
        *span_len = (int)(s->p - start);
        return 0;
    }
    for (;;) {
        const char *k;
        int kl;
        int32_t iv;
        sc_skip_ws(s);
        if (scan_string_span(s, &k, &kl) != 0 || memchr(k, '\\', (size_t)kl) != NULL) {
            return -1;
        }
        sc_skip_ws(s);
        if (sc_eof(s) || *s->p != ':') {
            return -1;
        }
        s->p++;
        sc_skip_ws(s);
        if (sc_eof(s)) {
            return -1;
        }
        char c = *s->p;
        if (c == '"') {
            const char *v;
            int vl;
            if (scan_string_span(s, &v, &vl) != 0) {
                return -1;
            }
        } else if (c == '-' || (c >= '0' && c <= '9')) {
            if (scan_int(s, &iv) != 0) {
                return -1;
            }
        } else if (!sc_match_lit(s, "true") && !sc_match_lit(s, "false") &&
                   !sc_match_lit(s, "null")) {
            return -1;          /* '{' / '[': one level of nesting only */
        }
        sc_skip_ws(s);
        if (sc_eof(s)) {
            return -1;
        }
        if (*s->p == ',') {
            s->p++;
            continue;
        }
        if (*s->p == '}') {
            s->p++;
            *span = start;
            *span_len = (int)(s->p - start);
            return 0;
        }
        return -1;
    }
}

static int key_listed(const char *const *only, const char *key, int key_len)
{
    for (int i = 0; only[i] != NULL; i++) {
        if ((int)strlen(only[i]) == key_len && memcmp(only[i], key, (size_t)key_len) == 0) {
            return 1;
        }
    }
    return 0;
}

/* The one tokenizer. mps3_json_parse() is (only NULL, allow_objects 0): every
 * member stored, nesting rejected -- exactly the v0 behaviour. */
static int json_parse_impl(const char *line, int len, mps3_json_obj_t *out,
                           const char *const *only, int allow_objects)
{
    memset(out, 0, sizeof(*out));
    if (line == NULL || len <= 0) {
        return MPS3_JSON_EMALFORMED;
    }
    json_scan_t s = { line, line + len };

    sc_skip_ws(&s);
    if (sc_eof(&s) || *s.p != '{') {
        return MPS3_JSON_EMALFORMED; /* not an object (arrays/scalars rejected) */
    }
    s.p++;
    sc_skip_ws(&s);
    if (!sc_eof(&s) && *s.p == '}') {
        s.p++; /* empty object: valid JSON; decode fails later on missing "op" */
    } else {
        for (;;) {
            mps3_json_member_t tmp;
            mps3_json_member_t *m;
            if (only == NULL) {
                if (out->n_members >= MPS3_JSON_MAX_MEMBERS) {
                    return MPS3_JSON_EMALFORMED; /* firmware bound, fail closed */
                }
                m = &out->members[out->n_members];
            } else {
                memset(&tmp, 0, sizeof(tmp));
                m = &tmp;       /* stored below only if listed */
            }

            sc_skip_ws(&s);
            if (scan_string_span(&s, &m->key, &m->key_len) != 0) {
                return MPS3_JSON_EMALFORMED;
            }
            /* Keys: contract keys are plain identifiers — an escaped key is
             * never legitimate, so reject outright rather than unescape. */
            if (memchr(m->key, '\\', (size_t)m->key_len) != NULL) {
                return MPS3_JSON_EMALFORMED;
            }
            int keep = (only == NULL) || key_listed(only, m->key, m->key_len);
            /* Duplicate keys: legal-ish JSON but unproducible by the
             * reference client (json.dumps of a dict) — ambiguous, reject. */
            if (keep) {
                for (int i = 0; i < out->n_members; i++) {
                    if (out->members[i].key_len == m->key_len &&
                        memcmp(out->members[i].key, m->key, (size_t)m->key_len) == 0) {
                        return MPS3_JSON_EMALFORMED;
                    }
                }
            }

            sc_skip_ws(&s);
            if (sc_eof(&s) || *s.p != ':') {
                return MPS3_JSON_EMALFORMED;
            }
            s.p++;
            sc_skip_ws(&s);
            if (sc_eof(&s)) {
                return MPS3_JSON_EMALFORMED;
            }

            char c = *s.p;
            if (c == '"') {
                if (scan_string_span(&s, &m->val, &m->val_len) != 0) {
                    return MPS3_JSON_EMALFORMED;
                }
                m->type = MPS3_JSON_T_STRING;
            } else if (c == '-' || (c >= '0' && c <= '9')) {
                m->val = s.p;
                if (scan_int(&s, &m->ival) != 0) {
                    return MPS3_JSON_EMALFORMED;
                }
                m->val_len = (int)(s.p - m->val);
                m->type = MPS3_JSON_T_INT;
            } else if (sc_match_lit(&s, "true")) {
                m->type = MPS3_JSON_T_BOOL;
                m->ival = 1;
            } else if (sc_match_lit(&s, "false")) {
                m->type = MPS3_JSON_T_BOOL;
                m->ival = 0;
            } else if (sc_match_lit(&s, "null")) {
                m->type = MPS3_JSON_T_NULL;
            } else if (c == '{' && allow_objects) {
                /* v0.17 (mps3_json_parse_ex only): ONE level of nesting. */
                if (scan_flat_object_span(&s, &m->val, &m->val_len) != 0) {
                    return MPS3_JSON_EMALFORMED;
                }
                m->type = MPS3_JSON_T_OBJECT;
            } else {
                /* '{' / '[' land here too: nesting/arrays rejected — no
                 * net-protocol.md v0 verb uses them (contract fact). */
                return MPS3_JSON_EMALFORMED;
            }
            if (only == NULL) {
                out->n_members++;
            } else if (keep) {
                if (out->n_members >= MPS3_JSON_MAX_MEMBERS) {
                    return MPS3_JSON_EMALFORMED;
                }
                out->members[out->n_members++] = tmp;
            }

            sc_skip_ws(&s);
            if (sc_eof(&s)) {
                return MPS3_JSON_EMALFORMED; /* truncated after a value */
            }
            if (*s.p == ',') {
                s.p++;
                continue;
            }
            if (*s.p == '}') {
                s.p++;
                break;
            }
            return MPS3_JSON_EMALFORMED;
        }
    }

    sc_skip_ws(&s);
    if (!sc_eof(&s)) {
        return MPS3_JSON_EMALFORMED; /* trailing garbage after the object */
    }
    return MPS3_JSON_OK;
}

int mps3_json_parse(const char *line, int len, mps3_json_obj_t *out)
{
    return json_parse_impl(line, len, out, NULL, 0);
}

int mps3_json_parse_ex(const char *line, int len, mps3_json_obj_t *out,
                       const char *const *only, int allow_objects)
{
    return json_parse_impl(line, len, out, only, allow_objects);
}

static const mps3_json_member_t *find_member(const mps3_json_obj_t *obj,
                                             const char *key)
{
    size_t klen = strlen(key);
    for (int i = 0; i < obj->n_members; i++) {
        if ((size_t)obj->members[i].key_len == klen &&
            memcmp(obj->members[i].key, key, klen) == 0) {
            return &obj->members[i];
        }
    }
    return NULL;
}

/* Resolves the escapes scan_string_span() already validated. Every escape
 * here is guaranteed well-formed by the parse — the default branch is
 * defensive dead code, not a reachable path. */
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
        if (o >= out_sz - 1) {
            return MPS3_JSON_ETOOLONG; /* no silent truncation — fail closed */
        }
        out[o++] = c;
    }
    out[o] = '\0';
    return MPS3_JSON_OK;
}

int mps3_json_get_string(const mps3_json_obj_t *obj, const char *key,
                         char *out, int out_sz)
{
    if (out == NULL || out_sz <= 0) {
        return MPS3_JSON_ETOOLONG;
    }
    out[0] = '\0'; /* fail-closed default on every error path below */
    const mps3_json_member_t *m = find_member(obj, key);
    if (m == NULL) {
        return MPS3_JSON_EMISSING;
    }
    if (m->type != MPS3_JSON_T_STRING) {
        return MPS3_JSON_ETYPE;
    }
    int rc = unescape_into(m->val, m->val_len, out, out_sz);
    if (rc != MPS3_JSON_OK) {
        out[0] = '\0';
    }
    return rc;
}

int mps3_json_get_int(const mps3_json_obj_t *obj, const char *key, int32_t *out)
{
    const mps3_json_member_t *m = find_member(obj, key);
    if (m == NULL) {
        return MPS3_JSON_EMISSING;
    }
    if (m->type != MPS3_JSON_T_INT) {
        return MPS3_JSON_ETYPE;
    }
    *out = m->ival;
    return MPS3_JSON_OK;
}

int mps3_json_get_bool(const mps3_json_obj_t *obj, const char *key, int *out)
{
    const mps3_json_member_t *m = find_member(obj, key);
    if (m == NULL) {
        return MPS3_JSON_EMISSING;
    }
    if (m->type != MPS3_JSON_T_BOOL) {
        return MPS3_JSON_ETYPE;
    }
    *out = (int)m->ival;
    return MPS3_JSON_OK;
}

int mps3_json_get_object(const mps3_json_obj_t *obj, const char *key, mps3_json_obj_t *sub)
{
    const mps3_json_member_t *m = find_member(obj, key);
    if (m == NULL) {
        return MPS3_JSON_EMISSING;
    }
    if (m->type != MPS3_JSON_T_OBJECT) {
        return MPS3_JSON_ETYPE;
    }
    return mps3_json_parse(m->val, m->val_len, sub) == MPS3_JSON_OK ? MPS3_JSON_OK
                                                                     : MPS3_JSON_ETYPE;
}

/* ==========================================================================
 * Verb-level decode
 * ========================================================================== */

static const struct {
    const char    *name;
    mps3_ctrl_op_t op;
} s_op_table[] = {
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
    { "version",   MPS3_OP_VERSION   },
    { "dutrx",     MPS3_OP_DUTRX     },
    { "stats",     MPS3_OP_STATS     },   /* v0.11 */
    { "log",       MPS3_OP_LOG       },   /* v0.11 */
    { "touch_cal", MPS3_OP_TOUCH_CAL },   /* v0.11 */
    { "reboot",    MPS3_OP_REBOOT    },   /* v0.11 */
    { "usd",       MPS3_OP_USD       },   /* v0.13 */
    { "slot",      MPS3_OP_SLOT      },   /* v0.14 */
    { "identity",     MPS3_OP_IDENTITY     },   /* v0.16 (engine verb) */
    { "identity_set", MPS3_OP_IDENTITY_SET },   /* v0.16 (engine verb) */
    { "locate",       MPS3_OP_LOCATE       },   /* v0.16 (engine verb) */
    { "hello",        MPS3_OP_HELLO        },   /* v0.17 (engine verb) */
    { "panel",        MPS3_OP_PANEL        },   /* v0.17 (engine verb) */
};

/* v0.13: a "0x" + 1..8 hex digit string (either case) -> u32. Strict: anything
 * else (no prefix, 0 or >8 digits, a stray character) is bad args, never a
 * best-effort parse -- these name the bytes a commit will persist. */
static int get_hex_u32(const mps3_json_obj_t *obj, const char *key, uint32_t *out)
{
    char buf[16];
    int rc = mps3_json_get_string(obj, key, buf, sizeof(buf));
    if (rc != MPS3_JSON_OK) {
        return rc;
    }
    if (buf[0] != '0' || (buf[1] != 'x' && buf[1] != 'X') || buf[2] == '\0') {
        return MPS3_JSON_ETYPE;
    }
    uint32_t v = 0u;
    int n = 0;
    for (const char *p = buf + 2; *p != '\0'; p++) {
        char c = *p;
        uint32_t d;
        if (c >= '0' && c <= '9') {
            d = (uint32_t)(c - '0');
        } else if (c >= 'a' && c <= 'f') {
            d = (uint32_t)(c - 'a' + 10);
        } else if (c >= 'A' && c <= 'F') {
            d = (uint32_t)(c - 'A' + 10);
        } else {
            return MPS3_JSON_ETYPE;
        }
        if (++n > 8) {
            return MPS3_JSON_ETYPE;
        }
        v = (v << 4) | d;
    }
    *out = v;
    return MPS3_JSON_OK;
}

/* v0.13: a non-negative JSON integer length -> u32. */
static int get_len_u32(const mps3_json_obj_t *obj, const char *key, uint32_t *out)
{
    int32_t v = 0;
    int rc = mps3_json_get_int(obj, key, &v);
    if (rc != MPS3_JSON_OK) {
        return rc;
    }
    if (v < 0) {
        return MPS3_JSON_ETYPE;
    }
    *out = (uint32_t)v;
    return MPS3_JSON_OK;
}

/* v0.13: an OPTIONAL string (absent -> ""). Present but not a string, or too
 * long, is still an error. */
static int get_opt_string(const mps3_json_obj_t *obj, const char *key, char *out, int out_sz)
{
    int rc = mps3_json_get_string(obj, key, out, out_sz);
    if (rc == MPS3_JSON_EMISSING) {
        out[0] = '\0';
        return MPS3_JSON_OK;
    }
    return rc;
}

int mps3_ctrl_decode_line(const char *line, int len, mps3_ctrl_request_t *out)
{
    memset(out, 0, sizeof(*out));
    out->op = MPS3_OP_UNKNOWN;

    mps3_json_obj_t obj;
    if (mps3_json_parse(line, len, &obj) != MPS3_JSON_OK) {
        /* v0.17: `hello` is the ONE request whose values nest (its `lease` and
         * `job` objects, one level). Only a line that parses with one level of
         * nesting AND names op "hello" is let through, as the engine verb it is
         * (its provider parses the line itself); every other line that fails
         * the flat parse is still bad json, exactly as before. */
        static const char *const k_op_only[] = { "op", NULL };
        char hop[8];
        if (mps3_json_parse_ex(line, len, &obj, k_op_only, 1) != MPS3_JSON_OK ||
            mps3_json_get_string(&obj, "op", hop, sizeof(hop)) != MPS3_JSON_OK ||
            strcmp(hop, "hello") != 0) {
            return MPS3_CTRL_DECODE_EBADJSON;
        }
        out->op = MPS3_OP_HELLO;
        out->line = line;
        out->line_len = len;
        return MPS3_CTRL_DECODE_OK;
    }

    /* Sized for the longest verb ("telemetry"); anything longer is by
     * definition not a known op, which ETOOLONG maps to below. */
    char opname[16];
    if (mps3_json_get_string(&obj, "op", opname, sizeof(opname)) != MPS3_JSON_OK) {
        return MPS3_CTRL_DECODE_EUNKNOWN_OP; /* missing / non-string / oversized */
    }
    for (size_t i = 0; i < sizeof(s_op_table) / sizeof(s_op_table[0]); i++) {
        if (strcmp(opname, s_op_table[i].name) == 0) {
            out->op = s_op_table[i].op;
            break;
        }
    }
    if (out->op == MPS3_OP_UNKNOWN) {
        return MPS3_CTRL_DECODE_EUNKNOWN_OP;
    }

    /* Required arguments per verb — see mps3_ctrl_decode_line()'s header
     * doc. Unknown extra keys were already accepted by the parse and are
     * simply never extracted. */
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
        if (rc == MPS3_JSON_OK) {
            rc = mps3_json_get_string(&obj, "src", out->src, sizeof(out->src));
        }
        break;
    case MPS3_OP_LINK:
        rc = mps3_json_get_string(&obj, "event", out->event, sizeof(out->event));
        break;
    case MPS3_OP_COMMIT:
        /* v0.13: the re-push commit. EVERY field is required -- the store needs
         * both lengths up front (slot size check, partial placement) and the
         * CRCs to check the pushed pair against. The v0.11 form (`rm` only)
         * therefore decodes as bad args, which is what the contract says it
         * answers. `src`'s VALUE ("tcp" only in v0.13) is the handler's check. */
        rc = mps3_json_get_string(&obj, "rm", out->rm, sizeof(out->rm));
        if (rc == MPS3_JSON_OK) {
            rc = mps3_json_get_string(&obj, "src", out->src, sizeof(out->src));
        }
        if (rc == MPS3_JSON_OK) {
            rc = get_hex_u32(&obj, "rm_id", &out->rm_id);
        }
        if (rc == MPS3_JSON_OK) {
            rc = get_hex_u32(&obj, "static_id", &out->static_id);
        }
        if (rc == MPS3_JSON_OK) {
            rc = get_len_u32(&obj, "clear_len", &out->clear_len);
        }
        if (rc == MPS3_JSON_OK) {
            rc = get_hex_u32(&obj, "clear_crc", &out->clear_crc);
        }
        if (rc == MPS3_JSON_OK) {
            rc = get_len_u32(&obj, "part_len", &out->part_len);
        }
        if (rc == MPS3_JSON_OK) {
            rc = get_hex_u32(&obj, "part_crc", &out->part_crc);
        }
        break;
    case MPS3_OP_USD:
        /* v0.13: status with no arguments; `action` and `confirm` are optional
         * strings whose VALUES the handler judges (an unknown action is bad
         * args there, a wrong confirm is "confirm required"). */
        rc = get_opt_string(&obj, "action", out->action, sizeof(out->action));
        if (rc == MPS3_JSON_OK) {
            rc = get_opt_string(&obj, "confirm", out->confirm, sizeof(out->confirm));
        }
        break;
    case MPS3_OP_MACGEN:
        /* gen/chk are required JSON bools; inject a required fault-name
         * string (validated against the INJECT set by the handler, mirroring
         * how link.event is validated). All three missing/wrong-typed ->
         * EBADARGS (fail closed), same as every other verb's required args. */
        rc = mps3_json_get_bool(&obj, "gen", &out->gen);
        if (rc == MPS3_JSON_OK) {
            rc = mps3_json_get_bool(&obj, "chk", &out->chk);
        }
        if (rc == MPS3_JSON_OK) {
            rc = mps3_json_get_string(&obj, "inject", out->inject, sizeof(out->inject));
        }
        break;
    case MPS3_OP_DISPLAY:
        /* One required string arg naming the requested owner. The value set
         * ("dut"/"harness"/"toggle"/"query") is validated by the handler
         * (coordinator_handle_display), exactly as link.event / reset.target
         * are — decode only checks the arg is present and a string. */
        rc = mps3_json_get_string(&obj, "owner", out->owner, sizeof(out->owner));
        break;
    case MPS3_OP_LOG:
        /* OPTIONAL `off` (v0.11): absent means "from the oldest retained byte".
         * Present-but-wrong-typed or negative is bad args, not a silent 0 -- a
         * client that thinks it asked for offset 5000 must not be handed 0. */
        out->off = 0;
        rc = mps3_json_get_int(&obj, "off", &out->off);
        if (rc == MPS3_JSON_EMISSING) {
            out->off = 0;
            rc = MPS3_JSON_OK;
        } else if (rc == MPS3_JSON_OK && out->off < 0) {
            rc = MPS3_JSON_ETYPE;
        }
        break;
    case MPS3_OP_TOUCH_CAL: {
        /* `act` required; "set" additionally requires all seven coefficients.
         * The VALUE checks (act name, shift range, singular matrix) are the
         * handler's -- decode only checks presence and type, like display. */
        static const char *const keys[MPS3_CAL_N] = {
            "ax", "bx", "cx", "ay", "by", "cy", "shift",
        };
        rc = mps3_json_get_string(&obj, "act", out->act, sizeof(out->act));
        if (rc == MPS3_JSON_OK && strcmp(out->act, "set") == 0) {
            for (int i = 0; i < MPS3_CAL_N && rc == MPS3_JSON_OK; i++) {
                rc = mps3_json_get_int(&obj, keys[i], &out->cal[i]);
            }
        }
        break;
    }
    case MPS3_OP_SLOT:
        /* v0.14: `act` required, `slot` OPTIONAL. Like touch_cal, decode checks
         * presence and type only; the act/slot VALUES are the handler's, so an
         * engine without the provider still answers a well-formed request with
         * its decline rather than a parse error. A present-but-wrong-typed or
         * oversized `slot` is bad args, never a silent "no selector". */
        rc = mps3_json_get_string(&obj, "act", out->act, sizeof(out->act));
        if (rc == MPS3_JSON_OK) {
            rc = mps3_json_get_string(&obj, "slot", out->slot_sel, sizeof(out->slot_sel));
            if (rc == MPS3_JSON_EMISSING) {
                out->slot_sel[0] = '\0';
                rc = MPS3_JSON_OK;
            }
        }
        break;
    case MPS3_OP_IDENTITY:
    case MPS3_OP_IDENTITY_SET:
    case MPS3_OP_LOCATE:
    case MPS3_OP_HELLO:     /* v0.17 */
    case MPS3_OP_PANEL:     /* v0.17 */
        /* v0.16 ENGINE verbs: the provider parses its own (optional) arguments
         * from the line (net_proto.h mps3_ctrl_request_t.line). */
        out->line = line;
        out->line_len = len;
        break;
    default:
        break; /* ping/telemetry/diag/version/dutrx/stats/reboot: no arguments */
    }
    if (rc != MPS3_JSON_OK) {
        out->op = MPS3_OP_UNKNOWN; /* an undecodable request carries no op */
        return MPS3_CTRL_DECODE_EBADARGS;
    }
    return MPS3_CTRL_DECODE_OK;
}

/* ==========================================================================
 * Per-op response encode
 * ========================================================================== */

/* Escapes a firmware-authored string for embedding in a JSON string value:
 * '"' and '\\' get backslash-escaped, control chars become '?' (firmware
 * never legitimately produces them — this is defense, not a feature). out
 * must be sized 2*strlen(in)+1 worst case; excess input is dropped at the
 * bound (acceptable only because this feeds diagnostic strings). */
static void json_escape(const char *in, char *out, int out_sz)
{
    int o = 0;
    for (const char *p = in; *p != '\0' && o < out_sz - 1; p++) {
        unsigned char c = (unsigned char)*p;
        if (c == '"' || c == '\\') {
            if (o >= out_sz - 2) {
                break;
            }
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

/* `version` feature names, in MPS3_FEATURE_* BIT ORDER (index i names bit i).
 * The order is part of the wire contract (net-protocol.md v0.8): a client may
 * compare the emitted array for equality, so it must not depend on anything but
 * the bitmask. Kept here, in the codec, rather than in the handler, so no
 * #ifdef ever reaches this file — coordinator_handle_version() sets bits; this
 * table turns bits into bytes. */
static const char *const s_feature_names[MPS3_FEATURE_COUNT] = {
    "clcd",         /* bit 0  MPS3_FEATURE_CLCD        */
    "clcd_kvm",     /* bit 1  MPS3_FEATURE_CLCD_KVM    */
    "touch",        /* bit 2  MPS3_FEATURE_TOUCH       */
    "hwicap_fifo",  /* bit 3  MPS3_FEATURE_HWICAP_FIFO */
    "windowed",     /* bit 4  MPS3_FEATURE_WINDOWED    */
    /* v0.11 -- APPENDED. Never insert above this line. */
    "dut_egress",   /* bit 5  MPS3_FEATURE_DUT_EGRESS  */
    "jtag_server",  /* bit 6  MPS3_FEATURE_JTAG_SERVER */
    "xvc_dbgbr",    /* bit 7  MPS3_FEATURE_XVC_DBGBR   */
    "xvc_jtagbb",   /* bit 8  MPS3_FEATURE_XVC_JTAGBB  */
    "stats",        /* bit 9  MPS3_FEATURE_STATS       */
    "log",          /* bit 10 MPS3_FEATURE_LOG         */
    "reboot",       /* bit 11 MPS3_FEATURE_REBOOT      */
    "touch_cal",    /* bit 12 MPS3_FEATURE_TOUCH_CAL   */
    /* v0.13 -- APPENDED. */
    "usd",          /* bit 13 MPS3_FEATURE_USD         */
    /* v0.14 amendments (2026-09-26) -- APPENDED in landing order. */
    "slot",         /* bit 14 MPS3_FEATURE_SLOT        */
    "xvc_lock",     /* bit 15 MPS3_FEATURE_XVC_LOCK    */
};

/* Renders the feature bitmask as a JSON array BODY (no brackets), e.g.
 * `"clcd","touch"`. Absent features are omitted, never emitted as false — an
 * empty set renders "" and so `[]`. Bounded like everything else here: returns
 * <0 if it would not fit, and the caller then fails the whole line rather than
 * emitting a truncated array. */
static int format_features(uint32_t features, char *out, int out_sz)
{
    int o = 0;
    out[0] = '\0';
    for (int i = 0; i < MPS3_FEATURE_COUNT; i++) {
        if ((features & (1u << i)) == 0u) {
            continue;
        }
        int n = snprintf(out + o, (size_t)(out_sz - o), "%s\"%s\"",
                         (o > 0) ? "," : "", s_feature_names[i]);
        if (n < 0 || n >= out_sz - o) {
            out[0] = '\0';
            return -1;
        }
        o += n;
    }
    return o;
}

/* The `version.impl` seam (net_proto.h). WEAK and NULL here: an image that
 * does not override it -- every bare-metal image -- emits no `impl` key at all,
 * so its `version` line is byte-identical to the one before the key existed.
 * The Linux harness (src/linux_harness/sw/harnessd/) overrides it with "linux".
 * Same weak-seam mechanism as coordinator.c's mps3_shell_static_id(). */
__attribute__((weak)) const char *mps3_proto_impl(void)
{
    return 0;
}

/* `version.id_skew` (net_proto.h): WEAK NULL = identity consistent / not
 * checked by this engine => no key, bare-metal bytes unchanged. */
__attribute__((weak)) const char *mps3_proto_id_skew(void)
{
    return 0;
}

/* The OMIT seams (net_proto.h). WEAK and 0 here: nothing is omitted, so a
 * bare-metal image's `diag` and `stats` lines are byte-identical to the ones
 * before the masks existed. An engine that genuinely cannot fill a key (the
 * Linux harness has no lwIP pcb and no bare-metal LAN9220 driver) overrides
 * these and the key is left OFF the line -- never reported as a plausible 0. */
__attribute__((weak)) uint64_t mps3_proto_diag_omit(void)
{
    return 0u;
}

__attribute__((weak)) uint32_t mps3_proto_stats_omit(void)
{
    return 0u;
}

/* `stats.os_up_ms` / `identify.os_up_ms` (net_proto.h): the OPERATING SYSTEM's
 * uptime, distinct from up_ms (this shell process's). WEAK: 0 = "no OS", and the
 * key is not emitted -- bare metal has no OS beneath the firmware. */
__attribute__((weak)) int mps3_proto_os_up_ms(uint32_t *out)
{
    (void)out;
    return 0;
}

/* v0.15 ENGINE seams (net_proto.h): WEAK and empty here, so no bare-metal image
 * emits an extra feature name, `version.lcd_mirror` or `stats.lcd_mirror`. */
__attribute__((weak)) const char *const *mps3_proto_features_extra(void)
{
    return 0;
}

__attribute__((weak)) int mps3_proto_lcd_mirror_info(mps3_lcd_mirror_info_t *out)
{
    (void)out;
    return 0;
}

__attribute__((weak)) int mps3_proto_lcd_mirror_stats(mps3_lcd_mirror_stats_t *out)
{
    (void)out;
    return 0;
}

/* An engine feature name is emitted only if it is 1..24 chars of [a-z0-9_]: it
 * goes on the wire unescaped, inside quotes, so the check IS the escaping. */
static int feature_name_ok(const char *s)
{
    int n = 0;
    for (; s[n] != '\0'; n++) {
        char c = s[n];
        if (n >= 24 || !((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_')) {
            return 0;
        }
    }
    return n > 0;
}

/* Appends the engine names (mps3_proto_features_extra) to a features array BODY
 * of length o in out[out_sz]. Returns the new length, or <0 if it would not fit
 * (the caller then fails the whole line, as format_features does). */
static int append_features_extra(char *out, int o, int out_sz)
{
    const char *const *x = mps3_proto_features_extra();
    for (int i = 0; x != 0 && i < 8 && x[i] != 0; i++) {
        if (!feature_name_ok(x[i])) {
            continue;
        }
        int n = snprintf(out + o, (size_t)(out_sz - o), "%s\"%s\"", (o > 0) ? "," : "", x[i]);
        if (n < 0 || n >= out_sz - o) {
            out[0] = '\0';
            return -1;
        }
        o += n;
    }
    return o;
}

/* ---- `slot` (v0.14) -------------------------------------------------------
 * The reply to every slot act: the card's boot slots as stage0 will see them,
 * plus the one asynchronous card job. The NAMES below are wire contract (the
 * provider only sets enumerations: net_proto.h says why); an out-of-range value
 * is a caller bug and fails the whole line closed, like an ok commit without a
 * slot letter. Built incrementally (the shape nests), every append bounded. */
static const char *const s_slot_run_names[] = { "unknown", "A", "B", "rescue", "none" };
static const char *const s_slot_st_names[]  = { "absent", "empty", "bad", "valid", "io" };
static const char *const s_slot_ver_names[] = { "no", "boot", "readback" };
static const char *const s_slot_job_names[] = { "none", "push", "verify" };
static const char *const s_slot_js_names[]  = { "idle", "writing", "verifying", "ok", "failed" };
#define SLOT_NAME_OK(tab, v) ((unsigned)(v) < sizeof(tab) / sizeof((tab)[0]))

/* A slot id as JSON: "A", "B" or null (MPS3_SLOT_NONE). NULL = not a slot id. */
static const char *slot_id_json(uint8_t v)
{
    return v == MPS3_SLOT_A ? "\"A\"" : v == MPS3_SLOT_B ? "\"B\"" :
           v == MPS3_SLOT_NONE ? "null" : 0;
}

static int encode_slot(const mps3_slot_status_t *st, char *out, int out_len)
{
    int used = 0;
#define SLOT_APPEND(...)                                                      \
    do {                                                                      \
        int k_ = snprintf(out + used, (size_t)(out_len - used), __VA_ARGS__); \
        if (k_ < 0 || k_ >= out_len - used) {                                 \
            out[0] = '\0';                                                    \
            return -1;                                                        \
        }                                                                     \
        used += k_;                                                           \
    } while (0)
    const char *staged = slot_id_json(st->staged);
    const char *job_slot = slot_id_json(st->job_slot);
    if (!SLOT_NAME_OK(s_slot_run_names, st->running) || !staged || !job_slot ||
        !SLOT_NAME_OK(s_slot_job_names, st->job_act) ||
        !SLOT_NAME_OK(s_slot_js_names, st->job_state)) {
        return -1;
    }
    SLOT_APPEND("{\"ok\":true,\"card\":%s,\"fabric_sid\":\"0x%08lx\",\"running\":\"%s\"",
                st->card ? "true" : "false", (unsigned long)st->fabric_sid,
                s_slot_run_names[st->running]);
    if (st->card) {
        const char *def = slot_id_json(st->deflt), *tgt = slot_id_json(st->target);
        if (!def || st->deflt == MPS3_SLOT_NONE || !tgt) {
            return -1;   /* stage0 ALWAYS has a default (A when no copy is valid) */
        }
        SLOT_APPEND(",\"default\":%s,\"seq\":%lu,\"target\":%s", def,
                    (unsigned long)st->seq, tgt);
    }
    SLOT_APPEND(",\"staged\":%s", staged);
    if (st->card) {
        for (int i = 0; i < 2; i++) {
            const mps3_slot_info_t *si = &st->s[i];
            if (!SLOT_NAME_OK(s_slot_st_names, si->state) ||
                !SLOT_NAME_OK(s_slot_ver_names, si->verified)) {
                return -1;
            }
            SLOT_APPEND(",\"%c\":{\"state\":\"%s\"", i == 0 ? 'a' : 'b',
                        s_slot_st_names[si->state]);
            if (si->state == MPS3_SLOT_ST_BAD || si->state == MPS3_SLOT_ST_IO) {
                char esc[2 * sizeof(si->err) + 1];
                json_escape(si->err, esc, (int)sizeof(esc));
                SLOT_APPEND(",\"err\":\"%s\"", esc);
            }
            if (si->state == MPS3_SLOT_ST_VALID) {
                SLOT_APPEND(",\"hdr_crc\":\"0x%08lx\",\"len\":%lu",
                            (unsigned long)si->hdr_crc, (unsigned long)si->len);
                if (si->has_sid) {
                    SLOT_APPEND(",\"sid\":\"0x%08lx\"", (unsigned long)si->sid);
                }
            }
            SLOT_APPEND(",\"verified\":\"%s\"}", s_slot_ver_names[si->verified]);
        }
    }
    {
        char esc[2 * sizeof(st->job_err) + 1];
        json_escape(st->job_err, esc, (int)sizeof(esc));
        SLOT_APPEND(",\"job\":{\"act\":\"%s\",\"slot\":%s,\"state\":\"%s\","
                    "\"got\":%lu,\"len\":%lu,\"err\":\"%s\"",
                    s_slot_job_names[st->job_act], job_slot,
                    s_slot_js_names[st->job_state], (unsigned long)st->job_got,
                    (unsigned long)st->job_len, esc);
        if (st->job_code[0]) {   /* 2026-09-26 (HM_ANSWERS S3): only when set */
            char cesc[2 * sizeof(st->job_code) + 1];
            json_escape(st->job_code, cesc, (int)sizeof(cesc));
            SLOT_APPEND(",\"code\":\"%s\"", cesc);
        }
        SLOT_APPEND("}");
    }
    /* 2026-09-26 (HM_ANSWERS S2/S5), APPENDED in both forms: the claim (the lock's
     * input -- identify is UDP and cannot ride an ssh -L tunnel) and whether this
     * boot was confirmed to stage0 (att_confirm). */
    SLOT_APPEND(",\"claimed\":%s,\"confirmed\":%s}\n", st->claimed ? "true" : "false",
                st->confirmed ? "true" : "false");
#undef SLOT_APPEND
    return used;
}

int mps3_ctrl_encode_response(const mps3_ctrl_response_t *resp, char *out, int out_len)
{
    if (out == NULL || out_len <= 0) {
        return -1;
    }
    out[0] = '\0';

    int n;
    if (!resp->ok) {
        /* One failure shape for every verb: {"ok":false,"err":"..."} — the
         * per-op success fields are meaningless on failure and net-protocol
         * .md doesn't define failure variants, so none are invented. The one
         * addition (2026-09-26): an optional "code" after "err", only when the
         * handler set resp->code. */
        char esc_err[2 * sizeof(resp->err) + 1];
        json_escape(resp->err, esc_err, (int)sizeof(esc_err));
        if (resp->op == MPS3_OP_TELEMETRY) {
            /* The ONE exception (net_proto.h's TELEMETRY note). telemetry has
             * no success shape at all — its power half has no sensor behind it
             * — but its `lockup` half is a real pin, so the failure line still
             * carries it. Reporting the raw pin inside an ok:false line is the
             * point: the power reading is unambiguously absent, while nothing
             * that consumes `lockup` is lost. */
            n = snprintf(out, (size_t)out_len,
                         "{\"ok\":false,\"err\":\"%s\",\"lockup\":%s}\n",
                         esc_err, resp->lockup ? "true" : "false");
        } else if (resp->code[0]) {
            /* 2026-09-26 (HM_ANSWERS S3/C2): the optional stable code, beside
             * `err`, only when the handler set one -- so a line without it is
             * byte-identical to the pre-code shape. */
            char esc_code[2 * sizeof(resp->code) + 1];
            json_escape(resp->code, esc_code, (int)sizeof(esc_code));
            n = snprintf(out, (size_t)out_len,
                         "{\"ok\":false,\"err\":\"%s\",\"code\":\"%s\"}\n", esc_err, esc_code);
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
            n = snprintf(out, (size_t)out_len,
                         "{\"ok\":true,\"locked\":%s}\n",
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
            if (resp->slot != 'A' && resp->slot != 'B') {
                return -1; /* ok commit without a valid slot: caller bug, fail closed */
            }
            n = snprintf(out, (size_t)out_len,
                         "{\"ok\":true,\"slot\":\"%c\"}\n", resp->slot);
            break;
        case MPS3_OP_DISPLAY: {
            /* {"ok":true,"owner":"harness"|"dut"} — the COMMITTED CLCD KVM
             * owner read back from CLCDKVM.STATUS.owner after the flip. The
             * "clcd_kvm not present" OFF-build answer is an ok:false line,
             * emitted above; this success shape only reaches here on a build
             * that actually has the 0x44AD slave. */
            char esc_owner[2 * sizeof(resp->owner) + 1];
            json_escape(resp->owner, esc_owner, (int)sizeof(esc_owner));
            n = snprintf(out, (size_t)out_len,
                         "{\"ok\":true,\"owner\":\"%s\"}\n", esc_owner);
            break;
        }
        case MPS3_OP_TELEMETRY:
            /* NO SUCCESS SHAPE EXISTS. There is no power sensor reachable from
             * this design (net_proto.h's TELEMETRY note: the TELEM block's data
             * inputs are tied to ground, its I2C engine was never written, the
             * pads are not on the top level, and the MCC refuses voltage reads).
             * A telemetry response can therefore only ever be the ok:false form
             * emitted above; ok=true here is a caller bug, and it is exactly the
             * bug that would resurrect {"mv":0,"ma":0}. Fail closed, like the
             * commit-without-a-slot case — the codec must make a fabricated
             * reading UNENCODABLE, not merely absent. */
            return -1;
        case MPS3_OP_MACGEN:
            /* Counters are u32 GENCHK readbacks; emit as unsigned decimal
             * (cast to unsigned long + %lu mirrors telemetry's long/%ld
             * choice — avoids pulling <inttypes.h> in for one format). The
             * success "err" here is the ERR_CNT counter, NOT the failure
             * diagnostic string (net_proto.h note; key on "ok"). */
            n = snprintf(out, (size_t)out_len,
                         "{\"ok\":true,\"tx\":%lu,\"rx\":%lu,\"err\":%lu}\n",
                         (unsigned long)resp->tx_cnt,
                         (unsigned long)resp->rx_cnt,
                         (unsigned long)resp->err_cnt);
            break;
        case MPS3_OP_VERSION: {
            /* v0.8 — the running image describing ITSELF: harness release +
             * packed HARNESS_VER32 + build sha/dirty (the mps3_harness_*()
             * seam), the LMB size it was LINKED for, and the compile-time
             * feature set. This is the verb that makes "which image is on the
             * board" answerable over the wire instead of by inference from a
             * mint script. The three identity strings are escaped like every
             * other firmware-authored string; `dirty` is an INT (0/1), not a
             * bool, so a client can carry it beside ver32's flags byte. */
            char esc_harness[2 * sizeof(resp->harness) + 1];
            char esc_ver32[2 * sizeof(resp->ver32) + 1];
            char esc_sha[2 * sizeof(resp->sha) + 1];
            char esc_usr[2 * sizeof(resp->usr_access) + 1];
            char feats[MPS3_CTRL_RESP_MAX / 2];
            /* v0.11 additive `impl` (net-protocol.md "version"): the ENGINE,
             * from the mps3_proto_impl() seam. The weak default below returns
             * NULL and then impl_kv stays "" -- so an image that does not set it
             * (every bare-metal image) emits the pre-`impl` bytes EXACTLY; the
             * two format strings below only ever gained a trailing %s. */
            /* ...and `id_skew` (additive, emitted only when set, BEFORE impl): an
             * engine whose identity sources disagree (the Linux harness's fabric
             * static_id vs its image's claim, HARNESSD_CONTRACT.md §9.4) says
             * why here. Weak default NULL => no key. */
            /* v0.15: `lcd_mirror` (additive, emitted only when the engine's
             * mps3_proto_lcd_mirror_info() seam says so) goes after id_skew and
             * before impl; the buffer grew by its worst case (64 B). */
            char impl_kv[48 + 2 * 60 + 16 + 64];
            const char *impl = mps3_proto_impl();
            const char *id_skew = mps3_proto_id_skew();
            int ikv = 0;
            impl_kv[0] = '\0';
            if (id_skew != 0 && id_skew[0] != '\0') {
                char esc_skew[2 * 60 + 1];
                char skew_cut[61];
                strncpy(skew_cut, id_skew, sizeof(skew_cut) - 1u);
                skew_cut[sizeof(skew_cut) - 1u] = '\0';   /* bounded: <= 60 chars */
                json_escape(skew_cut, esc_skew, (int)sizeof(esc_skew));
                ikv = snprintf(impl_kv, sizeof(impl_kv), ",\"id_skew\":\"%s\"", esc_skew);
                if (ikv < 0 || ikv >= (int)sizeof(impl_kv)) {
                    ikv = 0;
                    impl_kv[0] = '\0';
                }
            }
            {
                mps3_lcd_mirror_info_t lm;
                memset(&lm, 0, sizeof(lm));
                if (mps3_proto_lcd_mirror_info(&lm)) {
                    char mode_cut[sizeof(lm.mode)];
                    char esc_mode[2 * sizeof(lm.mode) + 1];
                    memcpy(mode_cut, lm.mode, sizeof(mode_cut));
                    mode_cut[sizeof(mode_cut) - 1u] = '\0';
                    json_escape(mode_cut, esc_mode, (int)sizeof(esc_mode));
                    int k = snprintf(impl_kv + ikv, sizeof(impl_kv) - (size_t)ikv,
                                     ",\"lcd_mirror\":{\"port\":%u,\"mode\":\"%s\",\"proto\":%u}",
                                     (unsigned)lm.port, esc_mode, (unsigned)lm.proto);
                    if (k > 0 && k < (int)sizeof(impl_kv) - ikv) {
                        ikv += k;
                    } else {
                        impl_kv[ikv] = '\0';
                    }
                }
            }
            if (impl != 0 && impl[0] != '\0') {
                char esc_impl[2 * 12 + 1];
                char impl_cut[13];
                strncpy(impl_cut, impl, sizeof(impl_cut) - 1u);
                impl_cut[sizeof(impl_cut) - 1u] = '\0';   /* bounded: <= 12 chars */
                json_escape(impl_cut, esc_impl, (int)sizeof(esc_impl));
                (void)snprintf(impl_kv + ikv, sizeof(impl_kv) - (size_t)ikv,
                               ",\"impl\":\"%s\"", esc_impl);
            }
            json_escape(resp->harness, esc_harness, (int)sizeof(esc_harness));
            json_escape(resp->ver32, esc_ver32, (int)sizeof(esc_ver32));
            json_escape(resp->sha, esc_sha, (int)sizeof(esc_sha));
            json_escape(resp->usr_access, esc_usr, (int)sizeof(esc_usr));
            int flen = format_features(resp->features, feats, (int)sizeof(feats));
            if (flen < 0 || append_features_extra(feats, flen, (int)sizeof(feats)) < 0) {
                return -1; /* would not fit: fail closed, never a torn array */
            }
            /* THE CROSS-CHECK, and the codec owns it.
             *
             * `ver32` is what this IMAGE was built as; `usr_access` is what the
             * FABRIC says it is (the bitstream's AXSS register). One generator
             * (scripts/gen_version.py) feeds both the firmware constant and
             * build_dfx.tcl's BITSTREAM.CONFIG.USR_ACCESS, so they agree BY
             * CONSTRUCTION unless the .bit and the image baked into it came from
             * different builds -- exactly the skew a re-bake that was never
             * re-run produces, and exactly what nothing has ever checked on
             * hardware.
             *
             * Derived HERE, not carried: a client cannot be handed "agree"
             * without the two values that agree, and a handler cannot set the
             * verdict without setting both fields. Compared as STRINGS because
             * both are rendered by the same format_id_hex() -- no second numeric
             * copy of ver32 to drift out of step with the first.
             *
             * The unreadable case emits null for BOTH keys. A zero, or a `false`
             * skew with no usr_access, would read as "checked, fine" -- the one
             * answer this must never give for a check that did not happen. */
            if (resp->usr_access[0] == '\0') {
                n = snprintf(out, (size_t)out_len,
                             "{\"ok\":true,\"harness\":\"%s\",\"ver32\":\"%s\","
                             "\"sha\":\"%s\",\"dirty\":%d,\"lmb_kb\":%lu,"
                             "\"features\":[%s],"
                             "\"usr_access\":null,\"skew\":null%s}\n",
                             esc_harness, esc_ver32, esc_sha,
                             resp->dirty ? 1 : 0,
                             (unsigned long)resp->lmb_kb, feats, impl_kv);
            } else {
                n = snprintf(out, (size_t)out_len,
                             "{\"ok\":true,\"harness\":\"%s\",\"ver32\":\"%s\","
                             "\"sha\":\"%s\",\"dirty\":%d,\"lmb_kb\":%lu,"
                             "\"features\":[%s],"
                             "\"usr_access\":\"%s\",\"skew\":%s%s}\n",
                             esc_harness, esc_ver32, esc_sha,
                             resp->dirty ? 1 : 0,
                             (unsigned long)resp->lmb_kb, feats, esc_usr,
                             strcmp(resp->ver32, resp->usr_access) != 0
                                 ? "true" : "false",
                             impl_kv);
            }
            break;
        }
        case MPS3_OP_DIAG: {
            /* EVERY counter the mailbox declares, walked out of the ONE list in
             * diag.h (MPS3_DIAG_FIELDS) instead of a hand-written 14. The old
             * form was a fourth hand-copied field list: the struct had 25
             * counters, this emitted 14, and the two JTAG readers listed 20.
             * Adding a counter now reaches the wire by existing.
             *
             * Emitted incrementally (not one giant snprintf) because the field
             * count is a macro expansion; each step is bounds-checked the same
             * way the single call was, and a short buffer still fails closed
             * with out[0]='\0' rather than emitting a truncated JSON line.
             * META rows (magic/version) and the PAD row expand to nothing —
             * the class dispatch is PREPROCESSOR-level, so `reserved[]` never
             * reaches a %lu cast. */
            int used = snprintf(out, (size_t)out_len, "{\"ok\":true");
            if (used < 0 || used >= out_len) {
                out[0] = '\0';
                return -1;
            }
            /* The OMIT mask (mps3_proto_diag_omit(), bit = MPS3_DIAG_IX_<name>):
             * keys this ENGINE cannot fill are left off the line rather than
             * reported as a plausible 0. The weak default is 0, so every
             * bare-metal image emits every key, byte-for-byte as before. */
            const uint64_t diag_omit = mps3_proto_diag_omit();
#define MPS3_DIAG_JSON_META(name, key)   /* header word: not a counter */
#define MPS3_DIAG_JSON_PAD(name, key)    /* pad: never transported */
#define MPS3_DIAG_JSON_COUNT(name, key)                                       \
            if ((diag_omit & ((uint64_t)1u << (unsigned)MPS3_DIAG_IX_##name)) == 0u) { \
                int k_ = snprintf(out + used, (size_t)(out_len - used),        \
                                  ",\"" key "\":%lu",                          \
                                  (unsigned long)resp->diag.name);            \
                if (k_ < 0 || k_ >= out_len - used) {                          \
                    out[0] = '\0';                                            \
                    return -1;                                                \
                }                                                             \
                used += k_;                                                    \
            }
#define MPS3_DIAG_JSON_ROW(name, cls, key) MPS3_DIAG_JSON_##cls(name, key)
            MPS3_DIAG_FIELDS(MPS3_DIAG_JSON_ROW)
#undef MPS3_DIAG_JSON_ROW
#undef MPS3_DIAG_JSON_COUNT
#undef MPS3_DIAG_JSON_PAD
#undef MPS3_DIAG_JSON_META
            n = snprintf(out + used, (size_t)(out_len - used), "}\n");
            if (n < 0 || n >= out_len - used) {
                out[0] = '\0';
                return -1;
            }
            n += used;
            break;
        }
        case MPS3_OP_DUTRX: {
            /* v0.10 — ONE CHUNK of the frame at the head of the DUT-egress
             * capture FIFO (DUTEGR, fpga/shell/ip/dut_egress/). Same key set
             * whether or not a frame was there, so a client branches on `n` and
             * `more`, never on shape.
             *
             * THE COUNTERS ARE NOT OPTIONAL. The block never backpressures the
             * bridge -- doing so parks the whole bridge, which the RTL's own
             * bench mutation-checks -- so it DROPS, and the one thing it must
             * never do is drop SILENTLY. rx + drop_full + drop_giant is its
             * invariant against frames presented, and this reply is the only
             * place a host ever sees it. Emitting frames without them would
             * make a lossy read look lossless.
             *
             * Bytes are hex-encoded here rather than carried raw: the line is
             * JSON and the firmware's tokenizer is a plain-ASCII one (see the
             * escape policy in net_proto.h). Written incrementally like the
             * diag arm, and bounds-checked as one block, so a short buffer
             * fails closed instead of emitting half a frame. */
            if (resp->dutrx_n > MPS3_DUTRX_CHUNK_MAX) {
                return -1; /* caller bug: a chunk larger than the contract's */
            }
            if (resp->dutrx_n > 0u &&
                (uint32_t)resp->dutrx_off + (uint32_t)resp->dutrx_n >
                    (uint32_t)resp->dutrx_len) {
                return -1; /* a chunk running past the frame it belongs to */
            }
            int used = snprintf(out, (size_t)out_len,
                         "{\"ok\":true,\"len\":%u,\"off\":%u,\"n\":%u,"
                         "\"more\":%s,\"last\":%s,\"frames\":%u,"
                         "\"rx\":%lu,\"drop_full\":%lu,\"drop_giant\":%lu,"
                         "\"ovf\":%s,\"desync\":%s,\"data\":\"",
                         (unsigned)resp->dutrx_len,
                         (unsigned)resp->dutrx_off,
                         (unsigned)resp->dutrx_n,
                         resp->dutrx_more   ? "true" : "false",
                         resp->dutrx_last   ? "true" : "false",
                         (unsigned)resp->dutrx_frames,
                         (unsigned long)resp->dutrx_rx_frames,
                         (unsigned long)resp->dutrx_drop_full,
                         (unsigned long)resp->dutrx_drop_giant,
                         resp->dutrx_ovf    ? "true" : "false",
                         resp->dutrx_desync ? "true" : "false");
            if (used < 0 || used >= out_len) {
                out[0] = '\0';
                return -1;
            }
            /* hex body + the closing "}\n + NUL, checked as ONE block. */
            if ((int)(2u * (unsigned)resp->dutrx_n) + 4 > out_len - used) {
                out[0] = '\0';
                return -1;
            }
            {
                static const char hexd[] = "0123456789abcdef";
                for (unsigned i = 0; i < (unsigned)resp->dutrx_n; i++) {
                    unsigned b = resp->dutrx_data[i];
                    out[used++] = hexd[(b >> 4) & 0xFu];
                    out[used++] = hexd[b & 0xFu];
                }
            }
            n = snprintf(out + used, (size_t)(out_len - used), "\"}\n");
            if (n < 0 || n >= out_len - used) {
                out[0] = '\0';
                return -1;
            }
            n += used;
            break;
        }
        case MPS3_OP_STATS: {
            /* v0.11 -- fpgahub's _from_stats_verb shape. The FIRST 22 keys are
             * that shape exactly and in its order (up_ms first after ok, as the
             * board-manager handover A1 requires); the five after `txerr` are
             * additive extras, and `os_up_ms` (Linux harness) is the last,
             * emitted only when mps3_proto_os_up_ms() supplies it. One bounded
             * snprintf per key -- never vsnprintf, which would drag newlib's
             * float engine into the MicroBlaze image -- so a key in the OMIT
             * mask (mps3_proto_stats_omit(), bit = MPS3_STATS_K_*) can be left
             * off; the weak default mask is 0, and with it the bytes are
             * EXACTLY the two-snprintf line this replaced. A short buffer still
             * fails the whole line closed. NO power keys: see TELEMETRY. */
            const mps3_stats_t *st = &resp->stats;
            const uint32_t stats_omit = mps3_proto_stats_omit();
            uint32_t os_up_ms = 0u;
            const int have_os_up = mps3_proto_os_up_ms(&os_up_ms);
            char sid[16], rm[16];
            char esc_swap[2 * sizeof(st->swap) + 1];
            char esc_err[2 * sizeof(st->swap_err) + 1];
            (void)snprintf(sid, sizeof(sid), "0x%08lx", (unsigned long)st->sid);
            (void)snprintf(rm, sizeof(rm), "0x%08lx", (unsigned long)st->rm);
            json_escape(st->swap, esc_swap, (int)sizeof(esc_swap));
            json_escape(st->swap_err, esc_err, (int)sizeof(esc_err));
            int used = snprintf(out, (size_t)out_len, "{\"ok\":true");
            if (used < 0 || used >= out_len) {
                out[0] = '\0';
                return -1;
            }
#define MPS3_STATS_KV(bit, ...)                                               \
            if ((stats_omit & ((uint32_t)1u << (unsigned)(bit))) == 0u) {      \
                int k_ = snprintf(out + used, (size_t)(out_len - used),        \
                                  __VA_ARGS__);                               \
                if (k_ < 0 || k_ >= out_len - used) {                          \
                    out[0] = '\0';                                            \
                    return -1;                                                \
                }                                                             \
                used += k_;                                                    \
            }
            MPS3_STATS_KV(MPS3_STATS_K_UP_MS, ",\"up_ms\":%lu", (unsigned long)st->up_ms)
            MPS3_STATS_KV(MPS3_STATS_K_SID, ",\"sid\":\"%s\"", sid)
            MPS3_STATS_KV(MPS3_STATS_K_RM, ",\"rm\":\"%s\"", rm)
            MPS3_STATS_KV(MPS3_STATS_K_RM_OK, ",\"rm_ok\":%s", st->rm_ok ? "true" : "false")
            MPS3_STATS_KV(MPS3_STATS_K_LOCK, ",\"lock\":%s", st->lock ? "true" : "false")
            MPS3_STATS_KV(MPS3_STATS_K_CLK_SEL, ",\"clk_sel\":%lu", (unsigned long)st->clk_sel)
            MPS3_STATS_KV(MPS3_STATS_K_MMCM, ",\"mmcm\":%s", st->mmcm ? "true" : "false")
            MPS3_STATS_KV(MPS3_STATS_K_CLK_ALIVE, ",\"clk_alive\":%s", st->clk_alive ? "true" : "false")
            MPS3_STATS_KV(MPS3_STATS_K_DUT_RST, ",\"dut_rst\":%s", st->dut_rst ? "true" : "false")
            MPS3_STATS_KV(MPS3_STATS_K_RP_RST, ",\"rp_rst\":%s", st->rp_rst ? "true" : "false")
            MPS3_STATS_KV(MPS3_STATS_K_DECPL, ",\"decpl\":%s", st->decpl ? "true" : "false")
            MPS3_STATS_KV(MPS3_STATS_K_LINK, ",\"link\":%s", st->link ? "true" : "false")
            MPS3_STATS_KV(MPS3_STATS_K_SPD, ",\"spd\":%lu", (unsigned long)st->spd)
            MPS3_STATS_KV(MPS3_STATS_K_FDX, ",\"fdx\":%s", st->fdx ? "true" : "false")
            MPS3_STATS_KV(MPS3_STATS_K_MAC, ",\"mac\":\"%02x%02x%02x%02x%02x%02x\"",
                (unsigned)st->mac[0], (unsigned)st->mac[1], (unsigned)st->mac[2],
                (unsigned)st->mac[3], (unsigned)st->mac[4], (unsigned)st->mac[5])
            MPS3_STATS_KV(MPS3_STATS_K_SWAP, ",\"swap\":\"%s\"", esc_swap)
            MPS3_STATS_KV(MPS3_STATS_K_SWAP_OK, ",\"swap_ok\":%s", st->swap_ok ? "true" : "false")
            MPS3_STATS_KV(MPS3_STATS_K_SWAP_N, ",\"swap_n\":%lu", (unsigned long)st->swap_n)
            MPS3_STATS_KV(MPS3_STATS_K_ICAP, ",\"icap\":%lu", (unsigned long)st->icap)
            MPS3_STATS_KV(MPS3_STATS_K_RXDROP, ",\"rxdrop\":%lu", (unsigned long)st->rxdrop)
            MPS3_STATS_KV(MPS3_STATS_K_TXERR, ",\"txerr\":%lu", (unsigned long)st->txerr)
            MPS3_STATS_KV(MPS3_STATS_K_SWAP_ERR, ",\"swap_err\":\"%s\"", esc_err)
            MPS3_STATS_KV(MPS3_STATS_K_CLR_OK, ",\"clr_ok\":%s", st->clr_ok ? "true" : "false")
            MPS3_STATS_KV(MPS3_STATS_K_DUT_MHZ, ",\"dut_mhz\":%lu", (unsigned long)st->dut_mhz)
            MPS3_STATS_KV(MPS3_STATS_K_SVC_MAX_US, ",\"svc_max_us\":%lu", (unsigned long)st->svc_max_us)
            MPS3_STATS_KV(MPS3_STATS_K_SVC_SKIPPED, ",\"svc_skipped\":%lu", (unsigned long)st->svc_skipped)
            if (st->touch_present) {   /* TOUCH=1 builds; before os_up_ms (net_proto.h) */
                MPS3_STATS_KV(MPS3_STATS_K_TOUCH_OK, ",\"touch_ok\":%s", st->touch_ok ? "true" : "false")
                MPS3_STATS_KV(MPS3_STATS_K_TOUCH_BUS_LOST, ",\"touch_bus_lost\":%lu",
                              (unsigned long)st->touch_bus_lost)
                MPS3_STATS_KV(MPS3_STATS_K_TOUCH_RECOVERIES, ",\"touch_recoveries\":%lu",
                              (unsigned long)st->touch_recoveries)
            }
            {
                /* v0.15 (Linux harness): the LCD mirror's client, before os_up_ms.
                 * Integer printf only (fps as d.d): no float engine in the image. */
                mps3_lcd_mirror_stats_t lm;
                memset(&lm, 0, sizeof(lm));
                if (mps3_proto_lcd_mirror_stats(&lm)) {
                    char peer_cut[sizeof(lm.peer)];
                    char esc_peer[2 * sizeof(lm.peer) + 3];
                    memcpy(peer_cut, lm.peer, sizeof(peer_cut));
                    peer_cut[sizeof(peer_cut) - 1u] = '\0';
                    if (peer_cut[0] != '\0') {
                        esc_peer[0] = '"';
                        json_escape(peer_cut, esc_peer + 1, (int)sizeof(esc_peer) - 2);
                        strcat(esc_peer, "\"");
                    } else {
                        strcpy(esc_peer, "null");
                    }
                    MPS3_STATS_KV(MPS3_STATS_K_LCD_MIRROR,
                                  ",\"lcd_mirror\":{\"peer\":%s,\"since\":%lu,\"fps\":%lu.%lu,"
                                  "\"bytes\":%lu}",
                                  esc_peer, (unsigned long)lm.since_ms,
                                  (unsigned long)(lm.fps_x10 / 10u),
                                  (unsigned long)(lm.fps_x10 % 10u), (unsigned long)lm.bytes)
                }
            }
            if (have_os_up) {
                MPS3_STATS_KV(MPS3_STATS_K_OS_UP_MS, ",\"os_up_ms\":%lu", (unsigned long)os_up_ms)
            }
#undef MPS3_STATS_KV
            n = snprintf(out + used, (size_t)(out_len - used), "}\n");
            if (n < 0 || n >= out_len - used) {
                out[0] = '\0';
                return -1;
            }
            n += used;
            break;
        }
        case MPS3_OP_LOG: {
            /* v0.11 -- one chunk of the console log ring, rendered EXACTLY like
             * dutrx's data (lowercase hex, bounds-checked as one block). The
             * bytes are console text, but they are carried as hex anyway: the
             * console can hold any byte (a stray control character, a half
             * escape sequence), and this protocol's escape policy has no \u --
             * hex is the one encoding that needs no escaping decision at all. */
            if (resp->log_n > MPS3_DUTRX_CHUNK_MAX) {
                return -1; /* caller bug: a chunk larger than the contract's */
            }
            int used = snprintf(out, (size_t)out_len,
                         "{\"ok\":true,\"off\":%lu,\"n\":%u,\"more\":%s,"
                         "\"dropped\":%lu,\"data\":\"",
                         (unsigned long)resp->log_off, (unsigned)resp->log_n,
                         resp->log_more ? "true" : "false",
                         (unsigned long)resp->log_dropped);
            if (used < 0 || used >= out_len) {
                out[0] = '\0';
                return -1;
            }
            if ((int)(2u * (unsigned)resp->log_n) + 4 > out_len - used) {
                out[0] = '\0';
                return -1;
            }
            {
                static const char hexd[] = "0123456789abcdef";
                for (unsigned i = 0; i < (unsigned)resp->log_n; i++) {
                    unsigned b = resp->dutrx_data[i];
                    out[used++] = hexd[(b >> 4) & 0xFu];
                    out[used++] = hexd[b & 0xFu];
                }
            }
            n = snprintf(out + used, (size_t)(out_len - used), "\"}\n");
            if (n < 0 || n >= out_len - used) {
                out[0] = '\0';
                return -1;
            }
            n += used;
            break;
        }
        case MPS3_OP_TOUCH_CAL:
            if (resp->tc_kind == MPS3_TC_RAW) {
                n = snprintf(out, (size_t)out_len,
                             "{\"ok\":true,\"raw_x\":%lu,\"raw_y\":%lu,"
                             "\"raw_z\":%lu,\"seen\":%lu,\"x\":%lu,\"y\":%lu}\n",
                             (unsigned long)resp->tc_raw_x,
                             (unsigned long)resp->tc_raw_y,
                             (unsigned long)resp->tc_raw_z,
                             (unsigned long)resp->tc_seen,
                             (unsigned long)resp->tc_x, (unsigned long)resp->tc_y);
            } else if (resp->tc_kind == MPS3_TC_COEFFS) {
                n = snprintf(out, (size_t)out_len,
                             "{\"ok\":true,\"ax\":%ld,\"bx\":%ld,\"cx\":%ld,"
                             "\"ay\":%ld,\"by\":%ld,\"cy\":%ld,\"shift\":%ld}\n",
                             (long)resp->tc_cal[MPS3_CAL_AX],
                             (long)resp->tc_cal[MPS3_CAL_BX],
                             (long)resp->tc_cal[MPS3_CAL_CX],
                             (long)resp->tc_cal[MPS3_CAL_AY],
                             (long)resp->tc_cal[MPS3_CAL_BY],
                             (long)resp->tc_cal[MPS3_CAL_CY],
                             (long)resp->tc_cal[MPS3_CAL_SHIFT]);
            } else {
                return -1; /* unknown reply kind: caller bug, fail closed */
            }
            break;
        case MPS3_OP_REBOOT:
            n = snprintf(out, (size_t)out_len, "{\"ok\":true,\"in_ms\":%lu}\n",
                         (unsigned long)resp->in_ms);
            break;
        case MPS3_OP_USD: {
            /* v0.13 -- the user-microSD store. Two shapes: an ACTION's reply is
             * just the resulting state; the status line carries card_mb only
             * while a card is READY and default{} only in state valid/stale
             * (absent keys, never zeroed ones). default{} is nested (as are
             * v0.14 slot's a/b/job); requests stay flat. */
            const mps3_usd_t *u = &resp->usd;
            char esc_state[2 * sizeof(u->state) + 1];
            json_escape(u->state, esc_state, (int)sizeof(esc_state));
            if (u->action_reply) {
                n = snprintf(out, (size_t)out_len, "{\"ok\":true,\"state\":\"%s\"}\n",
                             esc_state);
                break;
            }
            char esc_text[2 * sizeof(u->text) + 1];
            char esc_boot[2 * sizeof(u->boot) + 1];
            char card[32];
            char def[112];
            json_escape(u->text, esc_text, (int)sizeof(esc_text));
            json_escape(u->boot, esc_boot, (int)sizeof(esc_boot));
            card[0] = '\0';
            def[0] = '\0';
            if (u->have_card_mb) {
                (void)snprintf(card, sizeof(card), ",\"card_mb\":%lu",
                               (unsigned long)u->card_mb);
            }
            if (u->have_default) {
                if (u->def_slot != 'A' && u->def_slot != 'B') {
                    return -1;   /* a default without a slot: caller bug, fail closed */
                }
                (void)snprintf(def, sizeof(def),
                               ",\"default\":{\"rm_id\":\"0x%08lx\",\"static_id\":\"0x%08lx\","
                               "\"slot\":\"%c\"}",
                               (unsigned long)u->def_rm_id, (unsigned long)u->def_static_id,
                               u->def_slot);
            }
            n = snprintf(out, (size_t)out_len,
                         "{\"ok\":true,\"present\":%s,\"state\":\"%s\",\"text\":\"%s\"%s%s,"
                         "\"boot\":\"%s\"}\n",
                         u->present ? "true" : "false", esc_state, esc_text, card, def,
                         esc_boot);
            break;
        }
        case MPS3_OP_SLOT:
            /* v0.14 -- see encode_slot(). */
            n = encode_slot(&resp->slot_st, out, out_len);
            if (n < 0) {
                out[0] = '\0';
                return -1;
            }
            break;
        case MPS3_OP_IDENTITY:
        case MPS3_OP_IDENTITY_SET:
        case MPS3_OP_LOCATE:
        case MPS3_OP_HELLO:     /* v0.17 */
        case MPS3_OP_PANEL:     /* v0.17 */
            /* v0.16 ENGINE verbs: the provider's body, between "op" and "}". */
            n = snprintf(out, (size_t)out_len, "{\"ok\":true,\"op\":\"%s\"%s%s}\n",
                         resp->op == MPS3_OP_IDENTITY ? "identity" :
                         resp->op == MPS3_OP_IDENTITY_SET ? "identity_set" :
                         resp->op == MPS3_OP_HELLO ? "hello" :
                         resp->op == MPS3_OP_PANEL ? "panel" : "locate",
                         (resp->body && resp->body[0]) ? "," : "",
                         resp->body ? resp->body : "");
            break;
        default:
            return -1; /* ok=true with MPS3_OP_UNKNOWN: caller bug, fail closed */
        }
    }

    if (n < 0 || n >= out_len) {
        out[0] = '\0'; /* never hand back a truncated (= malformed) line */
        return -1;
    }
    return n;
}

/* ==========================================================================
 * Bitstream framing header — pack/unpack (net-protocol.md "Bitstream
 * framing"; wire format matches host/pusher/push.py's real BitstreamHeader,
 * big-endian). Pure byte-level logic, no hardware/network dependency; see
 * firmware/test/test_bitstream_header.c for the host-gcc unit tests.
 * ========================================================================== */
static void put_u16be(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v >> 8);
    p[1] = (uint8_t)(v & 0xFFu);
}

static void put_u32be(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v >> 24);
    p[1] = (uint8_t)(v >> 16);
    p[2] = (uint8_t)(v >> 8);
    p[3] = (uint8_t)(v & 0xFFu);
}

static uint16_t get_u16be(const uint8_t *p)
{
    return (uint16_t)(((uint16_t)p[0] << 8) | (uint16_t)p[1]);
}

static uint32_t get_u32be(const uint8_t *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8)  | (uint32_t)p[3];
}

void mps3_bitstream_hdr_pack(const mps3_bitstream_hdr_t *hdr,
                              uint8_t out[MPS3_BITSTREAM_HDR_WIRE_SIZE])
{
    memcpy(out, MPS3_BITSTREAM_MAGIC, 4);
    put_u16be(out + 4, hdr->ver);
    out[6] = hdr->kind;
    out[7] = hdr->rm_slot;
    put_u32be(out + 8,  hdr->static_id);
    put_u32be(out + 12, hdr->rm_id);
    put_u32be(out + 16, hdr->len_words);
    put_u32be(out + 20, hdr->crc32);
}

int mps3_bitstream_hdr_unpack(const uint8_t *in, uint32_t in_len,
                                mps3_bitstream_hdr_t *out)
{
    if (in_len < MPS3_BITSTREAM_HDR_WIRE_SIZE) {
        return -1; /* torn/short header -- reject before touching *out* at all */
    }
    if (memcmp(in, MPS3_BITSTREAM_MAGIC, 4) != 0) {
        return -1;
    }
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
