/*
 * test_net_proto_json.c — host-gcc unit tests for common/net_proto.c's
 * control-channel JSON-line codec (net-protocol.md "Control channel"):
 * the flat-JSON tokenizer (mps3_json_*), the verb decode
 * (mps3_ctrl_decode_line) and the per-op response encode
 * (mps3_ctrl_encode_response). Links net_proto.c ONLY -- see Makefile.
 *
 * The happy-path request lines below are byte-for-byte what
 * host/pyverify/pyverify/client.py emits (json.dumps(op,
 * separators=(",",":")) -- compact separators, key order as constructed);
 * the expected response lines are what tests/firmware_logic/
 * test_json_golden.py feeds back through json.loads + the pyverify
 * response dataclasses. If this file and client.py ever disagree, that
 * golden test is the tiebreaker to chase first.
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../common/net_proto.h"

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

/* Convenience wrappers: all fixture lines are C literals, so strlen() is
 * the wire length (the bounded-len behavior itself gets dedicated tests). */
static int parse(const char *s, mps3_json_obj_t *obj)
{
    return mps3_json_parse(s, (int)strlen(s), obj);
}

static int decode(const char *s, mps3_ctrl_request_t *req)
{
    return mps3_ctrl_decode_line(s, (int)strlen(s), req);
}

/* ---- tokenizer: happy paths ---------------------------------------------- */

static void test_parse_every_verb_line_as_pyverify_emits(void)
{
    /* Exact client.py wire bytes (compact separators, contract key order). */
    static const char *lines[] = {
        "{\"op\":\"ping\"}",
        "{\"op\":\"reset\",\"target\":\"dut\"}",
        "{\"op\":\"set_clk\",\"preset\":\"25mhz\"}",
        "{\"op\":\"swap\",\"rm\":\"nanosoc\",\"src\":\"tftp\"}",
        "{\"op\":\"link\",\"event\":\"down\"}",
        "{\"op\":\"commit\",\"rm\":\"nanosoc\"}",
        "{\"op\":\"telemetry\"}",
    };
    for (size_t i = 0; i < sizeof(lines) / sizeof(lines[0]); i++) {
        mps3_json_obj_t obj;
        CHECK(parse(lines[i], &obj) == MPS3_JSON_OK);
        char op[16];
        CHECK(mps3_json_get_string(&obj, "op", op, sizeof(op)) == MPS3_JSON_OK);
    }
}

static void test_parse_key_order_and_whitespace_tolerance(void)
{
    mps3_json_obj_t obj;
    char buf[32];

    /* Key order is irrelevant per JSON. */
    CHECK(parse("{\"target\":\"dut\",\"op\":\"reset\"}", &obj) == MPS3_JSON_OK);
    CHECK(mps3_json_get_string(&obj, "op", buf, sizeof(buf)) == MPS3_JSON_OK);
    CHECK(strcmp(buf, "reset") == 0);
    CHECK(mps3_json_get_string(&obj, "target", buf, sizeof(buf)) == MPS3_JSON_OK);
    CHECK(strcmp(buf, "dut") == 0);

    /* Arbitrary inter-token whitespace + trailing newline (the framing
     * layer may or may not strip the '\n' before dispatch). */
    CHECK(parse("  { \"op\" :\t\"ping\" }\r\n", &obj) == MPS3_JSON_OK);
    CHECK(obj.n_members == 1);

    /* Empty object: valid JSON (decode rejects it later, not the parser). */
    CHECK(parse("{}", &obj) == MPS3_JSON_OK);
    CHECK(obj.n_members == 0);
    CHECK(parse(" { } \n", &obj) == MPS3_JSON_OK);
}

static void test_parse_extra_unknown_keys_tolerated(void)
{
    mps3_json_obj_t obj;
    CHECK(parse("{\"op\":\"ping\",\"future_field\":\"x\",\"n\":3,\"b\":true}",
                &obj) == MPS3_JSON_OK);
    CHECK(obj.n_members == 4);
}

static void test_typed_getters(void)
{
    mps3_json_obj_t obj;
    CHECK(parse("{\"s\":\"str\",\"i\":-42,\"z\":0,\"big\":2147483647,"
                "\"neg\":-2147483648,\"t\":true,\"f\":false}",
                &obj) == MPS3_JSON_OK);

    char sbuf[8];
    int32_t i;
    int b;
    CHECK(mps3_json_get_string(&obj, "s", sbuf, sizeof(sbuf)) == MPS3_JSON_OK);
    CHECK(strcmp(sbuf, "str") == 0);
    CHECK(mps3_json_get_int(&obj, "i", &i) == MPS3_JSON_OK && i == -42);
    CHECK(mps3_json_get_int(&obj, "z", &i) == MPS3_JSON_OK && i == 0);
    CHECK(mps3_json_get_int(&obj, "big", &i) == MPS3_JSON_OK && i == 2147483647);
    CHECK(mps3_json_get_int(&obj, "neg", &i) == MPS3_JSON_OK && i == -2147483647 - 1);
    CHECK(mps3_json_get_bool(&obj, "t", &b) == MPS3_JSON_OK && b == 1);
    CHECK(mps3_json_get_bool(&obj, "f", &b) == MPS3_JSON_OK && b == 0);

    /* Missing key / wrong type are distinct, deliberate errors. */
    CHECK(mps3_json_get_string(&obj, "nope", sbuf, sizeof(sbuf)) == MPS3_JSON_EMISSING);
    CHECK(mps3_json_get_string(&obj, "i", sbuf, sizeof(sbuf)) == MPS3_JSON_ETYPE);
    CHECK(mps3_json_get_int(&obj, "s", &i) == MPS3_JSON_ETYPE);
    CHECK(mps3_json_get_bool(&obj, "i", &b) == MPS3_JSON_ETYPE);
    CHECK(sbuf[0] == '\0'); /* fail-closed: out cleared on the last error */
}

static void test_string_escapes_unescaped_on_extraction(void)
{
    mps3_json_obj_t obj;
    char buf[16];

    /* Documented policy: the simple escapes are UNESCAPED... */
    CHECK(parse("{\"k\":\"a\\\"b\"}", &obj) == MPS3_JSON_OK); /* a\"b */
    CHECK(mps3_json_get_string(&obj, "k", buf, sizeof(buf)) == MPS3_JSON_OK);
    CHECK(strcmp(buf, "a\"b") == 0);

    CHECK(parse("{\"k\":\"a\\\\b\\/c\\n\\t\"}", &obj) == MPS3_JSON_OK);
    CHECK(mps3_json_get_string(&obj, "k", buf, sizeof(buf)) == MPS3_JSON_OK);
    CHECK(strcmp(buf, "a\\b/c\n\t") == 0);

    /* ...and \uXXXX (never legitimate in a contract value) is REJECTED. */
    CHECK(parse("{\"k\":\"a\\u0041\"}", &obj) == MPS3_JSON_EMALFORMED);
}

static void test_len_bounded_never_reads_past_len(void)
{
    /* One buffer, two views: bytes past `len` must be invisible. If the
     * scanner ever consulted them, the GARBAGE would flip both results. */
    static const char buf[] = "{\"op\":\"ping\"}GARBAGE\"}";
    CHECK(mps3_json_parse(buf, 13, &(mps3_json_obj_t){0}) == MPS3_JSON_OK);
    /* One byte short of the closing brace: truncated -> reject. */
    CHECK(mps3_json_parse(buf, 12, &(mps3_json_obj_t){0}) == MPS3_JSON_EMALFORMED);

    mps3_ctrl_request_t req;
    CHECK(mps3_ctrl_decode_line(buf, 13, &req) == MPS3_CTRL_DECODE_OK);
    CHECK(req.op == MPS3_OP_PING);
    CHECK(mps3_ctrl_decode_line(buf, 12, &req) == MPS3_CTRL_DECODE_EBADJSON);
}

/* ---- tokenizer: malformed input (all must fail closed) ------------------- */

static void test_parse_rejects_malformed(void)
{
    static const char *bad[] = {
        "",                                   /* empty */
        "   ",                                /* whitespace only */
        "{\"op\":\"pi",                       /* truncated mid-string */
        "{\"op\":\"ping\"",                   /* truncated before '}' */
        "{\"op\":\"ping\",}",                 /* trailing comma */
        "{\"op\" \"ping\"}",                  /* missing ':' */
        "{\"op\":\"ping\" \"x\":1}",          /* missing ',' */
        "{op:\"ping\"}",                      /* unquoted key */
        "{\"op\":ping}",                      /* unquoted value */
        "{\"op\":\"ping\"}}",                 /* trailing garbage */
        "{\"op\":\"ping\"} x",                /* trailing garbage after ws */
        "[1,2]",                              /* not an object */
        "\"ping\"",                           /* bare string */
        "42",                                 /* bare number */
        "{\"op\":{\"x\":1}}",                 /* nested object (contract: flat only) */
        "{\"op\":[1]}",                       /* array value (contract: flat only) */
        "{\"op\":\"ping\",\"op\":\"ping\"}",  /* duplicate key */
        "{\"k\\n\":\"v\"}",                   /* escaped key (never legitimate) */
        "{\"k\":\"a\\qb\"}",                  /* unknown escape */
        "{\"k\":\"a\\\"}",                    /* line ends mid-escape */
        "{\"k\":\"a\nb\"}",                   /* raw control char in string */
        "{\"k\":truthy}",                     /* mangled literal */
        "{\"k\":True}",                       /* wrong-case literal */
        "{\"k\":01}",                         /* leading zero */
        "{\"k\":-}",                          /* bare minus */
        "{\"k\":1.5}",                        /* float (not in the contract) */
        "{\"k\":1e3}",                        /* exponent */
        "{\"k\":99999999999}",                /* int32 overflow */
        "{\"k\":-99999999999}",               /* int32 underflow */
        "{\"a\":1,\"b\":1,\"c\":1,\"d\":1,\"e\":1,\"f\":1,\"g\":1,\"h\":1,"
        "\"i\":1,\"j\":1,\"k\":1}",             /* > MPS3_JSON_MAX_MEMBERS (10, v0.11) */
    };
    for (size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
        mps3_json_obj_t obj;
        CHECK(parse(bad[i], &obj) == MPS3_JSON_EMALFORMED);
    }
}

static void test_null_value_parses_but_never_extracts(void)
{
    mps3_json_obj_t obj;
    char buf[8];
    int32_t i;
    int b;
    CHECK(parse("{\"k\":null}", &obj) == MPS3_JSON_OK); /* valid JSON... */
    CHECK(mps3_json_get_string(&obj, "k", buf, sizeof(buf)) == MPS3_JSON_ETYPE);
    CHECK(mps3_json_get_int(&obj, "k", &i) == MPS3_JSON_ETYPE);
    CHECK(mps3_json_get_bool(&obj, "k", &b) == MPS3_JSON_ETYPE);
}

static void test_oversized_string_value_is_error_not_truncation(void)
{
    mps3_json_obj_t obj;
    char small[4];
    CHECK(parse("{\"k\":\"abcdef\"}", &obj) == MPS3_JSON_OK);
    CHECK(mps3_json_get_string(&obj, "k", small, sizeof(small)) == MPS3_JSON_ETOOLONG);
    CHECK(small[0] == '\0'); /* never partial content */
    /* Exact fit (3 chars + NUL) is fine. */
    CHECK(parse("{\"k\":\"abc\"}", &obj) == MPS3_JSON_OK);
    CHECK(mps3_json_get_string(&obj, "k", small, sizeof(small)) == MPS3_JSON_OK);
    CHECK(strcmp(small, "abc") == 0);
}

/* ---- verb decode ---------------------------------------------------------- */

static void test_decode_every_verb_happy_path(void)
{
    mps3_ctrl_request_t req;

    CHECK(decode("{\"op\":\"ping\"}", &req) == MPS3_CTRL_DECODE_OK);
    CHECK(req.op == MPS3_OP_PING);

    CHECK(decode("{\"op\":\"reset\",\"target\":\"dut\"}", &req) == MPS3_CTRL_DECODE_OK);
    CHECK(req.op == MPS3_OP_RESET);
    CHECK(strcmp(req.target, "dut") == 0);

    CHECK(decode("{\"op\":\"set_clk\",\"preset\":\"25mhz\"}", &req) == MPS3_CTRL_DECODE_OK);
    CHECK(req.op == MPS3_OP_SET_CLK);
    CHECK(strcmp(req.preset, "25mhz") == 0);

    CHECK(decode("{\"op\":\"swap\",\"rm\":\"nanosoc\",\"src\":\"tftp\"}", &req) == MPS3_CTRL_DECODE_OK);
    CHECK(req.op == MPS3_OP_SWAP);
    CHECK(strcmp(req.rm, "nanosoc") == 0);
    CHECK(strcmp(req.src, "tftp") == 0);

    CHECK(decode("{\"op\":\"link\",\"event\":\"down\"}", &req) == MPS3_CTRL_DECODE_OK);
    CHECK(req.op == MPS3_OP_LINK);
    CHECK(strcmp(req.event, "down") == 0);

    /* v0.13: the re-push commit carries the pair's identity, lengths and CRCs. */
    CHECK(decode("{\"op\":\"commit\",\"rm\":\"led\",\"src\":\"tcp\",\"rm_id\":\"0x0100001e\","
                 "\"static_id\":\"0x72BB0A36\",\"clear_len\":68332,\"clear_crc\":\"0xdeadbeef\","
                 "\"part_len\":1251884,\"part_crc\":\"0x1\"}", &req) == MPS3_CTRL_DECODE_OK);
    CHECK(req.op == MPS3_OP_COMMIT);
    CHECK(strcmp(req.rm, "led") == 0 && strcmp(req.src, "tcp") == 0);
    CHECK(req.rm_id == 0x0100001Eu && req.static_id == 0x72BB0A36u);
    CHECK(req.clear_len == 68332u && req.clear_crc == 0xDEADBEEFu);
    CHECK(req.part_len == 1251884u && req.part_crc == 1u);

    /* v0.13: usd -- status (no args) and the optional action/confirm strings. */
    CHECK(decode("{\"op\":\"usd\"}", &req) == MPS3_CTRL_DECODE_OK);
    CHECK(req.op == MPS3_OP_USD && req.action[0] == '\0' && req.confirm[0] == '\0');
    CHECK(decode("{\"op\":\"usd\",\"action\":\"format\",\"confirm\":\"erase-all\"}", &req)
          == MPS3_CTRL_DECODE_OK);
    CHECK(strcmp(req.action, "format") == 0 && strcmp(req.confirm, "erase-all") == 0);
    CHECK(decode("{\"op\":\"usd\",\"action\":\"rescan\"}", &req) == MPS3_CTRL_DECODE_OK);
    CHECK(strcmp(req.action, "rescan") == 0 && req.confirm[0] == '\0');

    CHECK(decode("{\"op\":\"telemetry\"}", &req) == MPS3_CTRL_DECODE_OK);
    CHECK(req.op == MPS3_OP_TELEMETRY);

    CHECK(decode("{\"op\":\"diag\"}", &req) == MPS3_CTRL_DECODE_OK);
    CHECK(req.op == MPS3_OP_DIAG);

    /* dutrx (v0.10) takes NO arguments: the destructive DATA port is its own
     * cursor, so there is no offset for a client to send and none for the shell
     * to remember. Extra keys are ignored like anywhere else. */
    CHECK(decode("{\"op\":\"dutrx\"}", &req) == MPS3_CTRL_DECODE_OK);
    CHECK(req.op == MPS3_OP_DUTRX);
    CHECK(decode("{\"op\":\"dutrx\",\"max\":512}", &req) == MPS3_CTRL_DECODE_OK);
    CHECK(req.op == MPS3_OP_DUTRX);

    /* display: one required "owner" string, extracted verbatim (the value set
     * is validated by the handler, not the decode -- like link.event). */
    CHECK(decode("{\"op\":\"display\",\"owner\":\"dut\"}", &req) == MPS3_CTRL_DECODE_OK);
    CHECK(req.op == MPS3_OP_DISPLAY);
    CHECK(strcmp(req.owner, "dut") == 0);
    CHECK(decode("{\"op\":\"display\",\"owner\":\"harness\"}", &req) == MPS3_CTRL_DECODE_OK);
    CHECK(strcmp(req.owner, "harness") == 0);
    CHECK(decode("{\"op\":\"display\",\"owner\":\"toggle\"}", &req) == MPS3_CTRL_DECODE_OK);
    CHECK(strcmp(req.owner, "toggle") == 0);
    CHECK(decode("{\"op\":\"display\",\"owner\":\"query\"}", &req) == MPS3_CTRL_DECODE_OK);
    CHECK(strcmp(req.owner, "query") == 0);
}

static void test_decode_error_classes(void)
{
    mps3_ctrl_request_t req;

    /* Malformed line -> EBADJSON. */
    CHECK(decode("{\"op\":\"ping\"", &req) == MPS3_CTRL_DECODE_EBADJSON);
    CHECK(req.op == MPS3_OP_UNKNOWN);

    /* Valid JSON, no/unknown/non-string/oversized op -> EUNKNOWN_OP. */
    CHECK(decode("{}", &req) == MPS3_CTRL_DECODE_EUNKNOWN_OP);
    CHECK(decode("{\"op\":\"selfdestruct\"}", &req) == MPS3_CTRL_DECODE_EUNKNOWN_OP);
    CHECK(decode("{\"op\":5}", &req) == MPS3_CTRL_DECODE_EUNKNOWN_OP);
    CHECK(decode("{\"op\":\"averyveryverylongopname\"}", &req) == MPS3_CTRL_DECODE_EUNKNOWN_OP);

    /* Known op, bad arguments -> EBADARGS (missing / wrong type / too long). */
    CHECK(decode("{\"op\":\"reset\"}", &req) == MPS3_CTRL_DECODE_EBADARGS);
    CHECK(decode("{\"op\":\"reset\",\"target\":5}", &req) == MPS3_CTRL_DECODE_EBADARGS);
    CHECK(decode("{\"op\":\"set_clk\"}", &req) == MPS3_CTRL_DECODE_EBADARGS);
    CHECK(decode("{\"op\":\"swap\",\"rm\":\"nanosoc\"}", &req) == MPS3_CTRL_DECODE_EBADARGS); /* src required, see net_proto.h */
    CHECK(decode("{\"op\":\"swap\",\"src\":\"tftp\"}", &req) == MPS3_CTRL_DECODE_EBADARGS);
    CHECK(decode("{\"op\":\"link\"}", &req) == MPS3_CTRL_DECODE_EBADARGS);
    CHECK(decode("{\"op\":\"commit\"}", &req) == MPS3_CTRL_DECODE_EBADARGS);
    CHECK(decode("{\"op\":\"commit\",\"rm\":\"0123456789012345678901234567890123456789\"}",
                 &req) == MPS3_CTRL_DECODE_EBADARGS); /* > MPS3_CTRL_STR_MAX-1 */
    /* v0.13: THE v0.11 FORM IS BAD ARGS (the contract's answer to it). */
    CHECK(decode("{\"op\":\"commit\",\"rm\":\"nanosoc\"}", &req) == MPS3_CTRL_DECODE_EBADARGS);
    {
        /* Every field is required, and each is strictly typed. */
        static const char *const bad[] = {
            /* no part_crc */
            "{\"op\":\"commit\",\"rm\":\"a\",\"src\":\"tcp\",\"rm_id\":\"0x1\",\"static_id\":\"0x2\","
            "\"clear_len\":4,\"clear_crc\":\"0x3\",\"part_len\":4}",
            /* rm_id without 0x */
            "{\"op\":\"commit\",\"rm\":\"a\",\"src\":\"tcp\",\"rm_id\":\"1\",\"static_id\":\"0x2\","
            "\"clear_len\":4,\"clear_crc\":\"0x3\",\"part_len\":4,\"part_crc\":\"0x4\"}",
            /* 9 hex digits */
            "{\"op\":\"commit\",\"rm\":\"a\",\"src\":\"tcp\",\"rm_id\":\"0x100000000\",\"static_id\":\"0x2\","
            "\"clear_len\":4,\"clear_crc\":\"0x3\",\"part_len\":4,\"part_crc\":\"0x4\"}",
            /* a non-hex digit */
            "{\"op\":\"commit\",\"rm\":\"a\",\"src\":\"tcp\",\"rm_id\":\"0x1g\",\"static_id\":\"0x2\","
            "\"clear_len\":4,\"clear_crc\":\"0x3\",\"part_len\":4,\"part_crc\":\"0x4\"}",
            /* "0x" alone */
            "{\"op\":\"commit\",\"rm\":\"a\",\"src\":\"tcp\",\"rm_id\":\"0x\",\"static_id\":\"0x2\","
            "\"clear_len\":4,\"clear_crc\":\"0x3\",\"part_len\":4,\"part_crc\":\"0x4\"}",
            /* a negative length */
            "{\"op\":\"commit\",\"rm\":\"a\",\"src\":\"tcp\",\"rm_id\":\"0x1\",\"static_id\":\"0x2\","
            "\"clear_len\":-4,\"clear_crc\":\"0x3\",\"part_len\":4,\"part_crc\":\"0x4\"}",
            /* a length as a string */
            "{\"op\":\"commit\",\"rm\":\"a\",\"src\":\"tcp\",\"rm_id\":\"0x1\",\"static_id\":\"0x2\","
            "\"clear_len\":\"4\",\"clear_crc\":\"0x3\",\"part_len\":4,\"part_crc\":\"0x4\"}",
            /* an id as a number */
            "{\"op\":\"commit\",\"rm\":\"a\",\"src\":\"tcp\",\"rm_id\":1,\"static_id\":\"0x2\","
            "\"clear_len\":4,\"clear_crc\":\"0x3\",\"part_len\":4,\"part_crc\":\"0x4\"}",
            /* no src */
            "{\"op\":\"commit\",\"rm\":\"a\",\"rm_id\":\"0x1\",\"static_id\":\"0x2\","
            "\"clear_len\":4,\"clear_crc\":\"0x3\",\"part_len\":4,\"part_crc\":\"0x4\"}",
        };
        for (size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
            CHECK(decode(bad[i], &req) == MPS3_CTRL_DECODE_EBADARGS);
        }
    }
    /* usd: action / confirm must be strings when present. */
    CHECK(decode("{\"op\":\"usd\",\"action\":5}", &req) == MPS3_CTRL_DECODE_EBADARGS);
    CHECK(decode("{\"op\":\"usd\",\"action\":\"format\",\"confirm\":true}", &req)
          == MPS3_CTRL_DECODE_EBADARGS);
    CHECK(decode("{\"op\":\"display\"}", &req) == MPS3_CTRL_DECODE_EBADARGS);           /* owner missing */
    CHECK(decode("{\"op\":\"display\",\"owner\":5}", &req) == MPS3_CTRL_DECODE_EBADARGS); /* owner non-string */
    CHECK(req.op == MPS3_OP_UNKNOWN); /* an undecodable request carries no op */
}

/* ---- per-op response encode ----------------------------------------------- */

static void test_encode_every_verb_shape(void)
{
    char out[MPS3_CTRL_RESP_MAX];
    mps3_ctrl_response_t r;

    memset(&r, 0, sizeof(r));
    r.op = MPS3_OP_PING;
    r.ok = 1;
    strcpy(r.shell_id, "0xa1b2c3d4");
    strcpy(r.rm_id, "0x00000001");
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) > 0);
    CHECK(strcmp(out, "{\"ok\":true,\"shell_id\":\"0xa1b2c3d4\",\"rm_id\":\"0x00000001\"}\n") == 0);

    memset(&r, 0, sizeof(r));
    r.op = MPS3_OP_RESET;
    r.ok = 1;
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) > 0);
    CHECK(strcmp(out, "{\"ok\":true}\n") == 0);

    memset(&r, 0, sizeof(r));
    r.op = MPS3_OP_SET_CLK;
    r.ok = 1;
    r.locked = 1;
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) > 0);
    CHECK(strcmp(out, "{\"ok\":true,\"locked\":true}\n") == 0);
    r.locked = 0;
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) > 0);
    CHECK(strcmp(out, "{\"ok\":true,\"locked\":false}\n") == 0);

    memset(&r, 0, sizeof(r));
    r.op = MPS3_OP_SWAP;
    r.ok = 1;
    r.verified = 1;
    strcpy(r.rm_id, "0x00000001");
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) > 0);
    CHECK(strcmp(out, "{\"ok\":true,\"rm_id\":\"0x00000001\",\"verified\":true}\n") == 0);

    memset(&r, 0, sizeof(r));
    r.op = MPS3_OP_LINK;
    r.ok = 1;
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) > 0);
    CHECK(strcmp(out, "{\"ok\":true}\n") == 0);

    memset(&r, 0, sizeof(r));
    r.op = MPS3_OP_COMMIT;
    r.ok = 1;
    r.slot = 'B';
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) > 0);
    CHECK(strcmp(out, "{\"ok\":true,\"slot\":\"B\"}\n") == 0);

    /* display success: the committed CLCD KVM owner (CLCDKVM.STATUS.owner). */
    memset(&r, 0, sizeof(r));
    r.op = MPS3_OP_DISPLAY;
    r.ok = 1;
    strcpy(r.owner, "dut");
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) > 0);
    CHECK(strcmp(out, "{\"ok\":true,\"owner\":\"dut\"}\n") == 0);
    strcpy(r.owner, "harness");
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) > 0);
    CHECK(strcmp(out, "{\"ok\":true,\"owner\":\"harness\"}\n") == 0);
    /* OFF-build answer is the uniform failure line (handler sets ok=0). */
    memset(&r, 0, sizeof(r));
    r.op = MPS3_OP_DISPLAY;
    r.ok = 0;
    strcpy(r.err, "clcd_kvm not present");
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) > 0);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"clcd_kvm not present\"}\n") == 0);

    /* telemetry -- the ONE verb with no success shape, because this platform has
     * no reachable power sensor (net_proto.h's TELEMETRY note). Its failure line
     * is the uniform {"ok":false,"err":...} PLUS `lockup`, which is a real pin
     * and is reported raw rather than suppressed. */
    memset(&r, 0, sizeof(r));
    r.op = MPS3_OP_TELEMETRY;
    r.ok = 0;
    strcpy(r.err, "no power sensor");
    r.lockup = 0;
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) > 0);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"no power sensor\",\"lockup\":false}\n") == 0);
    r.lockup = 1;
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) > 0);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"no power sensor\",\"lockup\":true}\n") == 0);

    /* An ok=true telemetry is UNENCODABLE -- fail closed, exactly like a commit
     * with no slot. This is the codec-level guarantee that no future caller can
     * resurrect a fabricated {"mv":0,"ma":0} reading: there is no shape for it. */
    r.ok = 1;
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) < 0);
    CHECK(out[0] == '\0');       /* never a truncated / half-written line */

    /* Every OTHER verb keeps the bare uniform failure shape -- adding `lockup`
     * to telemetry's must not have leaked it into the shared error path. */
    memset(&r, 0, sizeof(r));
    r.op = MPS3_OP_PING;
    r.ok = 0;
    strcpy(r.err, "boom");
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) > 0);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"boom\"}\n") == 0);

    /* diag: EVERY counter mps3_diag_t declares, walked out of MPS3_DIAG_FIELDS
     * (diag.h) rather than a hand-written 14 — the seeded fields keep their
     * frozen keys ("grants_sent" = win_windows_drained) and the JTAG-only
     * counters follow, all zero here; v0.9.1 appends the four STMPE811
     * panel-probe words; v0.9.2 appends the thirteen superloop
     * service-telemetry words (diag v8). Worst-case width is why
     * MPS3_CTRL_RESP_MAX is 1280 (637 B at 25 counters, 738 at 29, 1040 at
     * 42 — the last step is what broke 768). */
    memset(&r, 0, sizeof(r));
    r.op = MPS3_OP_DIAG;
    r.ok = 1;
    r.diag.rx_recover_events = 0; r.diag.rx_recover_dumps = 0;
    r.diag.rx_drop_frames = 0;
    r.diag.icap_bytes = 256; r.diag.rx_payload_got = 4096;
    r.diag.rx_payload_expect = 68288;
    r.diag.tcp_rcv_wnd = 2048; r.diag.tcp_rcv_ann_wnd = 2048;
    r.diag.rx_queued = 0; r.diag.pbuf_free = 8;
    r.diag.win_windows_drained = 1; r.diag.win_grant_send_fails = 0;
    r.diag.tcp_sndbuf = 4096; r.diag.tcp_snd_wnd = 64240;
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) > 0);
    CHECK(strcmp(out,
        "{\"ok\":true,\"rx_recover\":0,\"rx_dumps\":0,\"rx_drops\":0,"
        "\"icap_bytes\":256,\"got\":4096,\"expect\":68288,"
        "\"rcv_wnd\":2048,\"rcv_ann_wnd\":2048,\"rx_queued\":0,"
        "\"pbuf_free\":8,\"grants_sent\":1,\"grant_fails\":0,"
        "\"sndbuf\":4096,\"snd_wnd\":64240,"
        "\"tx_frames_sent\":0,\"tx_status_drained\":0,"
        "\"tx_fifo_full_drops\":0,\"tx_errors\":0,"
        "\"tx_space_stalls\":0,\"tx_iface_errors\":0,"
        "\"tx_last_status\":0,\"icap_sr_last\":0,"
        "\"icap_eos_status\":0,\"ovlstore_phase\":0,"
        "\"ovlstore_detail\":0,"
        "\"touch_regs\":0,\"touch_adc_x\":0,"
        "\"touch_adc_y\":0,\"touch_verdict\":0,"
        /* v8 superloop service telemetry (diag.h +0x7C..+0xAC). Thirteen keys
         * APPENDED in X-macro order -- the first 29 keys and their order are
         * untouched, so a client reading by key sees no change. */
        "\"svc_count\":0,\"pass_max_us\":0,\"svc_max_us\":0,"
        "\"svc_max_ix\":0,\"svc_overruns\":0,\"svc_skips\":0,"
        "\"svc_skipped\":0,"
        "\"svc_us_0\":0,\"svc_us_1\":0,\"svc_us_2\":0,"
        "\"svc_us_3\":0,\"svc_us_4\":0,\"svc_us_5\":0,"
        /* v9 (D13): the usd service row's word and the power-on latch. */
        "\"svc_us_6\":0,\"usd_boot\":0}\n") == 0);

    /* All-max widths must still fit MPS3_CTRL_RESP_MAX -- and this is the
     * MEASURED budget, not an estimate. 25 counters at 10 digits was 636 B on
     * the wire, 637 with the NUL, which set the 768 bound; the four v0.9.1
     * touch keys took it to 738, which still fit.
     *
     * v0.9.2 did NOT fit. diag v8's thirteen service-telemetry keys
     * (svc_count / pass_max_us / svc_max_us / svc_max_ix / svc_overruns /
     * svc_skips / svc_skipped / svc_us_0..5 -- 120 chars of key plus 13 * 14 of
     * syntax and digits) take the worst case to 1039 on the wire, 1040 with the
     * NUL. At 768 that does not truncate: mps3_ctrl_encode_response() returns
     * -1 and the shell stops answering `diag` ENTIRELY, which is the failure
     * 02fb984 caught the same way and the reason this assertion exists at all.
     * The bound was raised DELIBERATELY to 1280 (the arithmetic is in
     * net_proto.h, and the stack cost is in lscript.ld.in).
     *
     * Asserted below rather than argued here: `worst` comes from the real
     * encoder over an all-0xFFFFFFFF mailbox. One byte short must FAIL CLOSED
     * rather than emit a truncated (malformed) line. */
    memset(&r.diag, 0xFF, sizeof(r.diag));
    int worst = mps3_ctrl_encode_response(&r, out, sizeof(out));
    CHECK(worst > 0);
    CHECK(worst + 1 <= MPS3_CTRL_RESP_MAX);
    /* v9 (D13) adds svc_us_6 + usd_boot: 44 counters, 1083 on the wire (1084
     * with the NUL) -- still the binding case, still inside 1280. */
    CHECK(worst == 1083);
    printf("  diag worst line (44 counters) = %d B\n", worst);
    CHECK(out[strlen(out) - 1] == '\n');
    char tight[MPS3_CTRL_RESP_MAX];
    CHECK(mps3_ctrl_encode_response(&r, tight, worst + 1) == worst);
    CHECK(mps3_ctrl_encode_response(&r, tight, worst) == -1);
    CHECK(tight[0] == '\0');
}

/* `version` (v0.8) — the one response shape in this protocol carrying a JSON
 * ARRAY. Three things are pinned here, all of them wire contract:
 *   1. the array's ORDER is the MPS3_FEATURE_* bit order, NOT the order the
 *      caller happened to OR the bits in (a client is allowed to compare the
 *      array for equality, so order cannot be incidental);
 *   2. absent features are OMITTED, never emitted as false — an image with no
 *      feature flags emits `[]`, which is what a plain `make elf` produces and
 *      therefore the single most important row here;
 *   3. `dirty` is an INT (0/1), not a JSON bool — it rides beside ver32's
 *      flags byte and the fakeshell must type-match it.
 * The REQUEST tokenizer is untouched by all this: `version` takes no arguments
 * and requests still contain no arrays (see net_proto.h's guarantees). */
static void test_encode_version_features_array(void)
{
    char out[MPS3_CTRL_RESP_MAX];
    mps3_ctrl_response_t r;

    /* No flags set: the empty array, NOT a missing key. */
    memset(&r, 0, sizeof(r));
    r.op = MPS3_OP_VERSION;
    r.ok = 1;
    strcpy(r.harness, "0.0.0");
    strcpy(r.ver32, "0x00000000");
    strcpy(r.sha, "unknown");
    r.lmb_kb = 1024;
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) > 0);
    CHECK(strcmp(out,
                 "{\"ok\":true,\"harness\":\"0.0.0\",\"ver32\":\"0x00000000\","
                 "\"sha\":\"unknown\",\"dirty\":0,\"lmb_kb\":1024,"
                 "\"features\":[],\"usr_access\":null,\"skew\":null}\n") == 0);

    /* The FIELDED set (PRODUCT=1), with the bits OR'd in reverse order to prove
     * the emitted order comes from the table, not from the caller. */
    memset(&r, 0, sizeof(r));
    r.op = MPS3_OP_VERSION;
    r.ok = 1;
    strcpy(r.harness, "1.0.0");
    strcpy(r.ver32, "0x01000001");
    strcpy(r.sha, "5c09de10");
    r.dirty  = 1;
    r.lmb_kb = 1024;
    r.features = MPS3_FEATURE_WINDOWED | MPS3_FEATURE_HWICAP_FIFO |
                 MPS3_FEATURE_TOUCH | MPS3_FEATURE_CLCD_KVM | MPS3_FEATURE_CLCD;
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) > 0);
    CHECK(strcmp(out,
                 "{\"ok\":true,\"harness\":\"1.0.0\",\"ver32\":\"0x01000001\","
                 "\"sha\":\"5c09de10\",\"dirty\":1,\"lmb_kb\":1024,"
                 "\"features\":[\"clcd\",\"clcd_kvm\",\"touch\",\"hwicap_fifo\","
                 "\"windowed\"],\"usr_access\":null,\"skew\":null}\n") == 0);
    /* The full shape is the longest `version` line and must fit the advertised
     * buffer with room to spare (net_proto.h's MPS3_CTRL_RESP_MAX note). */
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) < MPS3_CTRL_RESP_MAX);

    /* A sparse set: only the middle and top bits, comma placement intact. */
    r.features = MPS3_FEATURE_TOUCH | MPS3_FEATURE_WINDOWED;
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) > 0);
    CHECK(strstr(out, "\"features\":[\"touch\",\"windowed\"],") != NULL);

    /* An UNKNOWN bit (no name in the table) is silently not emitted rather than
     * rendering garbage or a bare comma. */
    r.features = MPS3_FEATURE_CLCD | (1u << 20);
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) > 0);
    CHECK(strstr(out, "\"features\":[\"clcd\"],") != NULL);

    /* A FAILING version response is the uniform two-key failure line — the
     * carve-out for per-op fields on failure is telemetry's alone. */
    memset(&r, 0, sizeof(r));
    r.op = MPS3_OP_VERSION;
    r.ok = 0;
    strcpy(r.err, "nope");
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) > 0);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"nope\"}\n") == 0);
}

/* `version` — the ver32-vs-USR_ACCESS cross-check, and its THREE states.
 *
 * `ver32` is what this IMAGE was built as. `usr_access` is what the FABRIC says
 * it is (the bitstream's AXSS register, stamped by fpga/dfx/build_dfx.tcl from
 * the SAME generator that produced the firmware constant). They can only
 * disagree if the .bit and the image baked into it came from different builds —
 * the "flashable base whose updatemem was never re-run" hazard, which nothing
 * has ever cross-checked on hardware.
 *
 * The verdict is DERIVED by the codec from the two fields, never carried on the
 * wire, so no caller can report "agree" without the two values that agree. The
 * load-bearing row is the THIRD one: an unreadable fabric value must emit null
 * for BOTH keys. `"skew":false` with no usr_access would read as "checked,
 * fine" — the one answer a check that did not happen must never give. */
static void test_encode_version_usr_access_skew(void)
{
    char out[MPS3_CTRL_RESP_MAX];
    mps3_ctrl_response_t r;

    memset(&r, 0, sizeof(r));
    r.op = MPS3_OP_VERSION;
    r.ok = 1;
    strcpy(r.harness, "1.0.0");
    strcpy(r.ver32, "0x01000001");
    strcpy(r.sha, "5c09de10");
    r.lmb_kb = 1024;

    /* AGREE — the pass a board window is looking for. */
    strcpy(r.usr_access, "0x01000001");
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) > 0);
    CHECK(strstr(out, "\"usr_access\":\"0x01000001\",\"skew\":false}") != NULL);

    /* DISAGREE — one bit of the packed version differs (a patch bump whose
     * updatemem was not re-run looks exactly like this). */
    strcpy(r.usr_access, "0x01000000");
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) > 0);
    CHECK(strstr(out, "\"usr_access\":\"0x01000000\",\"skew\":true}") != NULL);
    /* and it never quietly says the opposite */
    CHECK(strstr(out, "\"skew\":false") == NULL);

    /* UNREADABLE — no fabric value, so NO comparison. Both keys null. */
    r.usr_access[0] = '\0';
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) > 0);
    CHECK(strstr(out, "\"usr_access\":null,\"skew\":null}") != NULL);
    CHECK(strstr(out, "\"skew\":false") == NULL);
    CHECK(strstr(out, "\"skew\":true") == NULL);

    /* The longest `version` line now carries four identity strings; it must
     * still fit the advertised buffer (net_proto.h's MPS3_CTRL_RESP_MAX note). */
    memset(&r, 0, sizeof(r));
    r.op = MPS3_OP_VERSION;
    r.ok = 1;
    memset(r.harness, 'W', sizeof(r.harness) - 1);
    memset(r.ver32, 'W', sizeof(r.ver32) - 1);
    memset(r.sha, 'W', sizeof(r.sha) - 1);
    memset(r.usr_access, 'W', sizeof(r.usr_access) - 1);
    r.lmb_kb = 0xFFFFFFFFu;
    r.features = MPS3_FEATURE_WINDOWED | MPS3_FEATURE_HWICAP_FIFO |
                 MPS3_FEATURE_TOUCH | MPS3_FEATURE_CLCD_KVM | MPS3_FEATURE_CLCD;
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) > 0);
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) < MPS3_CTRL_RESP_MAX);
}

/* `version` decodes as a no-argument verb, like ping/telemetry/diag. */
static void test_decode_version_takes_no_arguments(void)
{
    mps3_ctrl_request_t req;
    CHECK(mps3_ctrl_decode_line("{\"op\":\"version\"}", 16, &req) == MPS3_CTRL_DECODE_OK);
    CHECK(req.op == MPS3_OP_VERSION);

    /* Extra keys tolerated (JSON tolerance), still the same verb. */
    const char *extra = "{\"op\":\"version\",\"why\":\"because\"}";
    CHECK(mps3_ctrl_decode_line(extra, (int)strlen(extra), &req) == MPS3_CTRL_DECODE_OK);
    CHECK(req.op == MPS3_OP_VERSION);
}

/* `dutrx` (v0.10) — one chunk of a DUT-transmitted frame. Four things are
 * pinned here, all of them wire contract:
 *   1. the key set is the SAME with and without a frame (an empty read is
 *      len/off/n = 0 and data "", never a shorter object) — a client branches
 *      on `n`/`more`, never on shape;
 *   2. `data` is LOWERCASE hex, high nibble first, exactly 2*n chars;
 *   3. the encoder FAILS CLOSED on an inconsistent chunk (one bigger than the
 *      contract's, or one running past the frame it claims to belong to)
 *      rather than emitting a line that decodes into a wrong frame;
 *   4. THE BUDGET ORDERING: a full-width dutrx line is shorter than a
 *      full-width diag line, which is what keeps `diag` the case
 *      MPS3_CTRL_RESP_MAX is sized for. That is the whole reason the chunk is
 *      256 and not 512, and it is asserted, not asserted-in-a-comment. */
static void test_encode_dutrx_chunk_shape_and_budget(void)
{
    char out[MPS3_CTRL_RESP_MAX];
    mps3_ctrl_response_t r;

    /* (a) nothing waiting: same keys, empty data. */
    memset(&r, 0, sizeof(r));
    r.op = MPS3_OP_DUTRX;
    r.ok = 1;
    r.dutrx_rx_frames = 7;
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) > 0);
    CHECK(strcmp(out,
        "{\"ok\":true,\"len\":0,\"off\":0,\"n\":0,\"more\":false,"
        "\"last\":false,\"frames\":0,\"rx\":7,\"drop_full\":0,"
        "\"drop_giant\":0,\"ovf\":false,\"desync\":false,\"data\":\"\"}\n") == 0);

    /* (b) a first chunk of a frame longer than one chunk: more=true, last is
     * the raw DATA[9] bit, which is false in the middle of a frame. */
    memset(&r, 0, sizeof(r));
    r.op = MPS3_OP_DUTRX;
    r.ok = 1;
    r.dutrx_len = 300; r.dutrx_off = 0; r.dutrx_n = 4;
    r.dutrx_more = 1; r.dutrx_last = 0; r.dutrx_frames = 2;
    r.dutrx_rx_frames = 9; r.dutrx_drop_full = 1; r.dutrx_drop_giant = 0;
    r.dutrx_ovf = 1; r.dutrx_desync = 0;
    r.dutrx_data[0] = 0xFFu; r.dutrx_data[1] = 0x00u;
    r.dutrx_data[2] = 0xA0u; r.dutrx_data[3] = 0x0Fu;
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) > 0);
    CHECK(strcmp(out,
        "{\"ok\":true,\"len\":300,\"off\":0,\"n\":4,\"more\":true,"
        "\"last\":false,\"frames\":2,\"rx\":9,\"drop_full\":1,"
        "\"drop_giant\":0,\"ovf\":true,\"desync\":false,"
        "\"data\":\"ff00a00f\"}\n") == 0);

    /* (c) the frame's tail: more=false and the LAST bit seen. */
    r.dutrx_off = 296; r.dutrx_n = 4; r.dutrx_more = 0; r.dutrx_last = 1;
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) > 0);
    CHECK(strstr(out, "\"off\":296,\"n\":4,\"more\":false,\"last\":true,") != NULL);

    /* (d) fail closed on an inconsistent chunk -- never a plausible line. */
    memset(&r, 0, sizeof(r));
    r.op = MPS3_OP_DUTRX;
    r.ok = 1;
    r.dutrx_n = MPS3_DUTRX_CHUNK_MAX + 1;      /* bigger than the contract's */
    r.dutrx_len = 4096;
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) == -1);
    CHECK(out[0] == '\0');
    r.dutrx_n = 8; r.dutrx_len = 10; r.dutrx_off = 6;  /* runs past the frame */
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) == -1);
    CHECK(out[0] == '\0');

    /* (e) THE BUDGET. A full-width chunk with every counter at max width, and
     * both sticky flags set, is the worst dutrx line there is. It must fit --
     * and it must still be SHORTER than the worst diag line, or this verb
     * becomes the thing MPS3_CTRL_RESP_MAX is sized for and the next diag
     * counter has to argue with a frame chunk for the buffer. */
    memset(&r, 0, sizeof(r));
    r.op = MPS3_OP_DUTRX;
    r.ok = 1;
    r.dutrx_len = 65535; r.dutrx_off = 65535 - MPS3_DUTRX_CHUNK_MAX;
    r.dutrx_n = MPS3_DUTRX_CHUNK_MAX;
    r.dutrx_more = 1; r.dutrx_last = 1; r.dutrx_frames = 65535;
    r.dutrx_rx_frames = 0xFFFFFFFFu; r.dutrx_drop_full = 0xFFFFFFFFu;
    r.dutrx_drop_giant = 0xFFFFFFFFu;
    r.dutrx_ovf = 1; r.dutrx_desync = 1;
    memset(r.dutrx_data, 0xFF, sizeof(r.dutrx_data));
    int worst_dutrx = mps3_ctrl_encode_response(&r, out, sizeof(out));
    CHECK(worst_dutrx > 0);
    CHECK(worst_dutrx + 1 <= MPS3_CTRL_RESP_MAX);
    CHECK(out[strlen(out) - 1] == '\n');

    mps3_ctrl_response_t d;
    memset(&d, 0, sizeof(d));
    d.op = MPS3_OP_DIAG;
    d.ok = 1;
    memset(&d.diag, 0xFF, sizeof(d.diag));
    int worst_diag = mps3_ctrl_encode_response(&d, out, sizeof(out));
    CHECK(worst_diag > 0);
    CHECK(worst_dutrx < worst_diag);   /* diag STAYS the binding case */

    /* One byte short must fail closed, like every other shape here: a frame
     * chunk truncated mid-hex would decode as a SHORTER, WRONG frame. */
    char tight[MPS3_CTRL_RESP_MAX];
    CHECK(mps3_ctrl_encode_response(&r, tight, worst_dutrx + 1) == worst_dutrx);
    CHECK(mps3_ctrl_encode_response(&r, tight, worst_dutrx) == -1);
    CHECK(tight[0] == '\0');
}

static void test_encode_error_shape_and_escaping(void)
{
    char out[MPS3_CTRL_RESP_MAX];
    mps3_ctrl_response_t r;

    memset(&r, 0, sizeof(r));
    r.op = MPS3_OP_UNKNOWN;
    strcpy(r.err, "bad json");
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) > 0);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"bad json\"}\n") == 0);

    /* err content must not be able to break the JSON framing. */
    memset(&r, 0, sizeof(r));
    r.op = MPS3_OP_SWAP;
    strcpy(r.err, "a\"b\\c\nd");
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) > 0);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"a\\\"b\\\\c?d\"}\n") == 0);
    /* The emitted line must itself decode as a flat JSON object. */
    mps3_json_obj_t obj;
    CHECK(mps3_json_parse(out, (int)strlen(out), &obj) == MPS3_JSON_OK);
}

static void test_encode_fails_closed(void)
{
    char out[8]; /* far too small for any real response */
    mps3_ctrl_response_t r;

    memset(&r, 0, sizeof(r));
    r.op = MPS3_OP_PING;
    r.ok = 1;
    strcpy(r.shell_id, "0xa1b2c3d4");
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) < 0);
    CHECK(out[0] == '\0'); /* truncated JSON is never emitted */

    char big[MPS3_CTRL_RESP_MAX];
    memset(&r, 0, sizeof(r));
    r.op = MPS3_OP_COMMIT;
    r.ok = 1;
    r.slot = 'Q'; /* not a valid slot: inconsistent resp -> error */
    CHECK(mps3_ctrl_encode_response(&r, big, sizeof(big)) < 0);

    memset(&r, 0, sizeof(r));
    r.op = MPS3_OP_UNKNOWN;
    r.ok = 1; /* ok=true with no op shape: caller bug -> error */
    CHECK(mps3_ctrl_encode_response(&r, big, sizeof(big)) < 0);
}

/* Worst-case sizing claim behind MPS3_CTRL_RESP_MAX: the error shape with
 * a maximal, fully-escaped err must fit (see net_proto.h). */
static void test_resp_max_fits_worst_case_error(void)
{
    char out[MPS3_CTRL_RESP_MAX];
    mps3_ctrl_response_t r;
    memset(&r, 0, sizeof(r));
    r.op = MPS3_OP_UNKNOWN;
    memset(r.err, '"', sizeof(r.err) - 1); /* 63 quotes -> 126 escaped bytes */
    r.err[sizeof(r.err) - 1] = '\0';
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof(out)) > 0);
    CHECK(out[strlen(out) - 1] == '\n');
}

/* ==========================================================================
 * v0.11 codec: the appended feature bits, the new verbs' decode rules.
 * ========================================================================== */
static void test_v011_features_are_appended_in_order(void)
{
    mps3_ctrl_response_t r;
    char out[MPS3_CTRL_RESP_MAX];
    memset(&r, 0, sizeof r);
    r.op = MPS3_OP_VERSION;
    r.ok = 1;
    strcpy(r.harness, "1.0.0"); strcpy(r.ver32, "0x01000001"); strcpy(r.sha, "5c09de10");
    strcpy(r.usr_access, "0x01000001");
    r.lmb_kb = 1024;
    r.features = (1u << MPS3_FEATURE_COUNT) - 1u;       /* every bit */
    int n = mps3_ctrl_encode_response(&r, out, sizeof out);
    CHECK(n > 0 && n < MPS3_CTRL_RESP_MAX);
    /* bits 0..4 UNCHANGED and first; 5..12 appended in bit order */
    CHECK(strstr(out, "\"features\":[\"clcd\",\"clcd_kvm\",\"touch\",\"hwicap_fifo\","
                      "\"windowed\",\"dut_egress\",\"jtag_server\",\"xvc_dbgbr\","
                      "\"xvc_jtagbb\",\"stats\",\"log\",\"reboot\",\"touch_cal\",\"usd\","
                      "\"slot\",\"xvc_lock\"],") != NULL);
    CHECK(MPS3_FEATURE_COUNT == 16);
    CHECK(MPS3_FEATURE_TOUCH_CAL == (1u << 12));
    CHECK(MPS3_FEATURE_USD == (1u << 13));   /* v0.13: APPENDED as the next free bit */
    CHECK(MPS3_FEATURE_SLOT == (1u << 14));  /* v0.14 amendments (2026-09-26): APPENDED */
    CHECK(MPS3_FEATURE_XVC_LOCK == (1u << 15));
    printf("  version worst line (all %d features) = %d B\n", MPS3_FEATURE_COUNT, n);
    /* the PRODUCT=1 image's set: every flag, dbgbr not jtagbb */
    r.features = ((1u << MPS3_FEATURE_COUNT) - 1u) & ~(uint32_t)MPS3_FEATURE_XVC_JTAGBB;
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof out) > 0);
    CHECK(strstr(out, "\"windowed\",\"dut_egress\",\"jtag_server\",\"xvc_dbgbr\",\"stats\"") != NULL);
}

/* 2026-09-26 (HM_ANSWERS S3/C2): the optional `code` beside `err` -- emitted
 * only when set, so a failure line without it is the old line byte for byte. */
static void test_failure_code_only_when_set(void)
{
    mps3_ctrl_response_t r;
    char out[MPS3_CTRL_RESP_MAX];
    memset(&r, 0, sizeof r);
    r.op = MPS3_OP_SLOT;
    strcpy(r.err, "EBUSY");
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof out) > 0);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"EBUSY\"}\n") == 0);
    strcpy(r.code, "busy");
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof out) > 0);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"EBUSY\",\"code\":\"busy\"}\n") == 0);
    /* the worst case: both at full width, all escapes */
    memset(r.err, '"', sizeof r.err - 1u);
    memset(r.code, '\\', sizeof r.code - 1u);
    int n = mps3_ctrl_encode_response(&r, out, sizeof out);
    CHECK(n > 0 && n < MPS3_CTRL_RESP_MAX);
    /* telemetry's carve-out keeps its shape (a code is never set there) */
    memset(&r, 0, sizeof r);
    r.op = MPS3_OP_TELEMETRY;
    strcpy(r.err, "no power sensor");
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof out) > 0);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"no power sensor\",\"lockup\":false}\n") == 0);
}

static void test_v011_decode_rules(void)
{
    mps3_ctrl_request_t req;
    /* no-argument verbs */
    CHECK(decode("{\"op\":\"stats\"}", &req) == MPS3_CTRL_DECODE_OK && req.op == MPS3_OP_STATS);
    CHECK(decode("{\"op\":\"reboot\"}", &req) == MPS3_CTRL_DECODE_OK && req.op == MPS3_OP_REBOOT);
    /* log: off optional, non-negative int */
    CHECK(decode("{\"op\":\"log\"}", &req) == MPS3_CTRL_DECODE_OK && req.off == 0);
    CHECK(decode("{\"op\":\"log\",\"off\":4096}", &req) == MPS3_CTRL_DECODE_OK && req.off == 4096);
    CHECK(decode("{\"op\":\"log\",\"off\":-5}", &req) == MPS3_CTRL_DECODE_EBADARGS);
    CHECK(decode("{\"op\":\"log\",\"off\":true}", &req) == MPS3_CTRL_DECODE_EBADARGS);
    /* touch_cal: act required; set requires all seven ints (9 members total) */
    CHECK(decode("{\"op\":\"touch_cal\"}", &req) == MPS3_CTRL_DECODE_EBADARGS);
    CHECK(decode("{\"op\":\"touch_cal\",\"act\":\"get\"}", &req) == MPS3_CTRL_DECODE_OK &&
          req.op == MPS3_OP_TOUCH_CAL && strcmp(req.act, "get") == 0);
    CHECK(decode("{\"op\":\"touch_cal\",\"act\":\"set\",\"ax\":-1,\"bx\":2,\"cx\":-3,"
                 "\"ay\":4,\"by\":-5,\"cy\":6,\"shift\":7}", &req) == MPS3_CTRL_DECODE_OK);
    CHECK(req.cal[MPS3_CAL_AX] == -1 && req.cal[MPS3_CAL_BX] == 2 &&
          req.cal[MPS3_CAL_CX] == -3 && req.cal[MPS3_CAL_AY] == 4 &&
          req.cal[MPS3_CAL_BY] == -5 && req.cal[MPS3_CAL_CY] == 6 &&
          req.cal[MPS3_CAL_SHIFT] == 7);
    CHECK(decode("{\"op\":\"touch_cal\",\"act\":\"set\",\"ax\":1}", &req) == MPS3_CTRL_DECODE_EBADARGS);
    /* reboot encode */
    {
        mps3_ctrl_response_t r;
        char out[64];
        memset(&r, 0, sizeof r);
        r.op = MPS3_OP_REBOOT; r.ok = 1; r.in_ms = 2784u;
        CHECK(mps3_ctrl_encode_response(&r, out, sizeof out) > 0);
        CHECK(strcmp(out, "{\"ok\":true,\"in_ms\":2784}\n") == 0);
    }
}

/* v0.13 (D13): the `usd` reply shapes, and the worst-case line through the
 * real encoder against MPS3_CTRL_RESP_MAX (re-measured, as the contract asks). */
static void test_encode_usd_shapes(void)
{
    mps3_ctrl_response_t r;
    char out[MPS3_CTRL_RESP_MAX];

    /* No card: absence is not an error, and card_mb / default are ABSENT. */
    memset(&r, 0, sizeof r);
    r.op = MPS3_OP_USD;
    r.ok = 1;
    strcpy(r.usd.state, "none");
    strcpy(r.usd.text, "none");
    strcpy(r.usd.boot, "none");
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof out) > 0);
    CHECK(strcmp(out, "{\"ok\":true,\"present\":false,\"state\":\"none\",\"text\":\"none\","
                      "\"boot\":\"none\"}\n") == 0);

    /* The contract's example, key for key. */
    r.usd.present = 1;
    strcpy(r.usd.state, "valid");
    strcpy(r.usd.text, "led [A]");
    r.usd.have_card_mb = 1;
    r.usd.card_mb = 15193;
    r.usd.have_default = 1;
    r.usd.def_rm_id = 0x0100001Eu;
    r.usd.def_static_id = 0x72BB0A36u;
    r.usd.def_slot = 'A';
    strcpy(r.usd.boot, "loaded");
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof out) > 0);
    CHECK(strcmp(out, "{\"ok\":true,\"present\":true,\"state\":\"valid\",\"text\":\"led [A]\","
                      "\"card_mb\":15193,\"default\":{\"rm_id\":\"0x0100001e\","
                      "\"static_id\":\"0x72bb0a36\",\"slot\":\"A\"},\"boot\":\"loaded\"}\n") == 0);

    /* A default without a slot is a caller bug: fail closed. */
    r.usd.def_slot = 0;
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof out) == -1);
    r.usd.def_slot = 'B';

    /* An action's reply is the resulting state only. */
    r.usd.action_reply = 1;
    strcpy(r.usd.state, "empty");
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof out) > 0);
    CHECK(strcmp(out, "{\"ok\":true,\"state\":\"empty\"}\n") == 0);
    r.usd.action_reply = 0;

    /* Worst case: every string at its maximum, every number at u32 max. */
    memset(r.usd.state, 'x', sizeof r.usd.state - 1u);
    r.usd.state[sizeof r.usd.state - 1u] = '\0';
    memset(r.usd.text, '"', MPS3_USD_TEXT_MAX);    /* the escaper doubles each */
    r.usd.text[MPS3_USD_TEXT_MAX] = '\0';
    memset(r.usd.boot, 'y', sizeof r.usd.boot - 1u);
    r.usd.boot[sizeof r.usd.boot - 1u] = '\0';
    r.usd.card_mb = 0xFFFFFFFFu;
    r.usd.def_rm_id = r.usd.def_static_id = 0xFFFFFFFFu;
    int n = mps3_ctrl_encode_response(&r, out, sizeof out);
    CHECK(n > 0 && n + 1 <= MPS3_CTRL_RESP_MAX);
    printf("  usd worst line = %d B (diag stays the binding case)\n", n);
    CHECK(n < 1083);

    /* commit's success shape is unchanged. */
    memset(&r, 0, sizeof r);
    r.op = MPS3_OP_COMMIT;
    r.ok = 1;
    r.slot = 'B';
    CHECK(mps3_ctrl_encode_response(&r, out, sizeof out) > 0);
    CHECK(strcmp(out, "{\"ok\":true,\"slot\":\"B\"}\n") == 0);
}

/* ---- v0.17: mps3_json_parse_ex + the one nested request (`hello`) --------- */

static void test_v017_parse_ex_and_the_nested_hello(void)
{
    mps3_json_obj_t obj, sub;
    mps3_ctrl_request_t req;
    char buf[32];
    int32_t v;
    static const char *const only[] = { "op", "lease", NULL };
    const char *doc = "{\"op\":\"hello\",\"v\":1,\"sid\":\"a1b2c3d4\",\"who\":\"alice@lab-pc01\","
                      "\"app\":\"hm/0.1.0\",\"name\":\"mps3-01\",\"role\":\"holder\",\"lease\":"
                      "{\"by\":\"alice\",\"left\":4332,\"q\":1,\"req\":\"bob\",\"rl\":103},"
                      "\"job\":{\"k\":\"program\",\"p\":42},\"ttl\":90}";

    /* the flat parser is unchanged: nesting is still malformed ... */
    CHECK(parse(doc, &obj) == MPS3_JSON_EMALFORMED);
    /* ... parse_ex(allow_objects) takes ONE level; the objects span their braces */
    CHECK(mps3_json_parse_ex(doc, (int)strlen(doc), &obj, NULL, 1) == MPS3_JSON_OK);
    CHECK(obj.n_members == 10);
    CHECK(mps3_json_get_object(&obj, "lease", &sub) == MPS3_JSON_OK);
    CHECK(mps3_json_get_string(&sub, "by", buf, sizeof(buf)) == MPS3_JSON_OK && strcmp(buf, "alice") == 0);
    CHECK(mps3_json_get_int(&sub, "rl", &v) == MPS3_JSON_OK && v == 103);
    CHECK(mps3_json_get_object(&obj, "who", &sub) == MPS3_JSON_ETYPE);
    CHECK(mps3_json_get_object(&obj, "nope", &sub) == MPS3_JSON_EMISSING);
    CHECK(mps3_json_get_string(&obj, "lease", buf, sizeof(buf)) == MPS3_JSON_ETYPE);
    /* the key list: unlisted keys are validated, not stored (so > 10 keys parse) */
    CHECK(mps3_json_parse_ex(doc, (int)strlen(doc), &obj, only, 1) == MPS3_JSON_OK);
    CHECK(obj.n_members == 2);
    CHECK(mps3_json_get_string(&obj, "who", buf, sizeof(buf)) == MPS3_JSON_EMISSING);
    const char *wide = "{\"op\":\"x\",\"a\":1,\"b\":1,\"c\":1,\"d\":1,\"e\":1,\"f\":1,\"g\":1,"
                       "\"h\":1,\"i\":1,\"j\":1,\"k\":1}";
    CHECK(parse(wide, &obj) == MPS3_JSON_EMALFORMED);
    CHECK(mps3_json_parse_ex(wide, (int)strlen(wide), &obj, only, 0) == MPS3_JSON_OK);
    /* still rejected: two levels, arrays, a malformed inner object, a dup listed key,
     * objects without allow_objects, and a malformed unlisted value */
    static const char *const bad[] = {
        "{\"op\":\"hello\",\"lease\":{\"by\":{\"x\":1}}}",
        "{\"op\":\"hello\",\"lease\":[1]}",
        "{\"op\":\"hello\",\"lease\":{\"by\":\"x\",}}",
        "{\"op\":\"hello\",\"lease\":{\"by\" \"x\"}}",
        "{\"op\":\"hello\",\"lease\":{\"by\":\"x\"}",
        "{\"op\":\"hello\",\"op\":\"hello\"}",
        "{\"op\":\"hello\",\"who\":\"a\\qb\"}",
    };
    for (size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
        CHECK(mps3_json_parse_ex(bad[i], (int)strlen(bad[i]), &obj, only, 1) == MPS3_JSON_EMALFORMED);
    }
    CHECK(mps3_json_parse_ex("{\"a\":{}}", 8, &obj, NULL, 0) == MPS3_JSON_EMALFORMED);
    CHECK(mps3_json_parse_ex("{\"a\":{}}", 8, &obj, NULL, 1) == MPS3_JSON_OK);

    /* the decoder: a nested line decodes ONLY as `hello` (an engine verb: line
     * passed through); every other nested line is still bad json */
    CHECK(decode(doc, &req) == MPS3_CTRL_DECODE_OK);
    CHECK(req.op == MPS3_OP_HELLO && req.line == doc && req.line_len == (int)strlen(doc));
    CHECK(decode("{\"op\":\"ping\",\"x\":{\"a\":1}}", &req) == MPS3_CTRL_DECODE_EBADJSON);
    CHECK(decode("{\"op\":\"hellO\",\"x\":{\"a\":1}}", &req) == MPS3_CTRL_DECODE_EBADJSON);
    CHECK(decode("{\"x\":{\"a\":1}}", &req) == MPS3_CTRL_DECODE_EBADJSON);
    CHECK(decode("{\"op\":\"hello\",\"x\":{\"a\":{\"b\":1}}}", &req) == MPS3_CTRL_DECODE_EBADJSON);
    CHECK(decode("{\"op\":\"hello\",\"sid\":\"s\",\"who\":\"w\"}", &req) == MPS3_CTRL_DECODE_OK &&
          req.op == MPS3_OP_HELLO && req.line != NULL);
    CHECK(decode("{\"op\":\"panel\",\"frame\":\"a\"}", &req) == MPS3_CTRL_DECODE_OK &&
          req.op == MPS3_OP_PANEL && req.line != NULL);

    /* the encoder: the engine-verb shape, "op" named */
    {
        mps3_ctrl_response_t r;
        char out[MPS3_CTRL_RESP_MAX];
        memset(&r, 0, sizeof r);
        r.op = MPS3_OP_PANEL;
        r.ok = 1;
        r.body = "\"page\":\"apps\"";
        CHECK(mps3_ctrl_encode_response(&r, out, sizeof out) > 0);
        CHECK(strcmp(out, "{\"ok\":true,\"op\":\"panel\",\"page\":\"apps\"}\n") == 0);
        r.op = MPS3_OP_HELLO;
        r.body = "\"sessions\":0";
        CHECK(mps3_ctrl_encode_response(&r, out, sizeof out) > 0);
        CHECK(strcmp(out, "{\"ok\":true,\"op\":\"hello\",\"sessions\":0}\n") == 0);
    }
}

int main(void)
{
    test_encode_usd_shapes();
    test_v011_features_are_appended_in_order();
    test_failure_code_only_when_set();
    test_v011_decode_rules();
    test_parse_every_verb_line_as_pyverify_emits();
    test_parse_key_order_and_whitespace_tolerance();
    test_parse_extra_unknown_keys_tolerated();
    test_typed_getters();
    test_string_escapes_unescaped_on_extraction();
    test_len_bounded_never_reads_past_len();
    test_parse_rejects_malformed();
    test_null_value_parses_but_never_extracts();
    test_oversized_string_value_is_error_not_truncation();
    test_decode_every_verb_happy_path();
    test_decode_error_classes();
    test_encode_every_verb_shape();
    test_encode_dutrx_chunk_shape_and_budget();
    test_encode_version_features_array();
    test_encode_version_usr_access_skew();
    test_decode_version_takes_no_arguments();
    test_encode_error_shape_and_escaping();
    test_encode_fails_closed();
    test_resp_max_fits_worst_case_error();
    test_v017_parse_ex_and_the_nested_hello();

    printf("test_net_proto_json: %d checks passed\n", s_checks);
    return 0;
}
