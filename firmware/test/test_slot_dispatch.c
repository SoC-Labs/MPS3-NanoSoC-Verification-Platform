/*
 * test_slot_dispatch.c -- the v0.14 `slot` verb driven the way a client drives
 * it: a line into coordinator_dispatch_line(), the real codec's bytes out
 * (net-protocol.md "Slot images"). Same link set as test_v011_dispatch
 * (DISPATCH_SRCS over mock_regs).
 *
 * ONE source, TWO binaries:
 *   test_slot_dispatch           no provider: the bare-metal image. Every valid
 *                                act declines "slot not supported"; the decode
 *                                and act/slot checks are the shared ones.
 *   test_slot_dispatch_provider  -DTEST_SLOT_PROVIDER: a strong mps3_slot_op().
 *                                Pins the reply BYTES (the one shape every act
 *                                answers with), the no-card shape, error
 *                                pass-through, the fail-closed encode of an
 *                                out-of-range enumeration, and the worst-case
 *                                line against MPS3_CTRL_RESP_MAX.
 *
 * Both binaries also drive `reboot` against the CARD-JOB seam
 * (mps3_card_job_active(), coordinator.h; HM_ANSWERS_2026-09-26 change 6): the
 * provider build supplies a strong one and `reboot` answers EBUSY while it says
 * a card job runs; the bare-metal build keeps the weak 0 and `reboot` is v0.11's.
 */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "../coordinator/coordinator.h"
#include "../common/net_proto.h"
#include "../common/platform_regs.h"
#include "mock_regs.h"
#include "fake_config_agent.h"
#include "fake_overlay_store.h"

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

#define TEST_STATIC_ID 0x61BC6789u
uint32_t mps3_shell_static_id(void) { return TEST_STATIC_ID; }

static void boot(void)
{
    mock_regs_reset();
    fake_config_agent_reset();
    overlay_manifest_info_t greybox = {
        .static_id = TEST_STATIC_ID, .rm_id = 0,
        .clear_len_words = 4, .clear_crc32 = 0x1111,
    };
    fake_overlay_store_set_greybox(&greybox, 0);
    coordinator_init();
}

static char s_out[MPS3_CTRL_RESP_MAX];

static const char *line(const char *req)
{
    int n = coordinator_dispatch_line(req, (int)strlen(req), s_out, (int)sizeof(s_out));
    assert(n == 1);   /* never held */
    return s_out;
}

#define EXPECT(req, want) do {                                              \
        const char *got_ = line(req);                                       \
        if (strcmp(got_, want) != 0) {                                      \
            fprintf(stderr, "req  %s\ngot  %swant %s", req, got_, want);    \
        }                                                                   \
        CHECK(strcmp(got_, want) == 0);                                     \
    } while (0)

/* A live watchdog behind 0x44B4 (its TBR free-runs), so `reboot` gets past its
 * "no watchdog" refusal and the card-job rule is what decides. */
static uint32_t s_tbr;
static int wdog_hook(void *ctx, int is_write, uint32_t base, uint32_t off, uint32_t *val)
{
    (void)ctx; (void)base;
    if (!is_write && off == WDOG_TBR) {
        *val = s_tbr++;
        return 1;
    }
    return 0;
}

/* The shared decode/argument checks: identical on every engine. */
static void test_shared_request_checks(void)
{
    EXPECT("{\"op\":\"slot\"}", "{\"ok\":false,\"err\":\"bad args\"}\n");
    EXPECT("{\"op\":\"slot\",\"act\":7}", "{\"ok\":false,\"err\":\"bad args\"}\n");
    EXPECT("{\"op\":\"slot\",\"act\":\"status\",\"slot\":1}", "{\"ok\":false,\"err\":\"bad args\"}\n");
    EXPECT("{\"op\":\"slot\",\"act\":\"status\",\"slot\":\"AAAAAAAAA\"}",
           "{\"ok\":false,\"err\":\"bad args\"}\n");
    /* the verb's own refusals carry their stable code (HM_ANSWERS S3); decode
     * failures ("bad args", "unknown op") stay code-less, like every verb's */
    EXPECT("{\"op\":\"slot\",\"act\":\"format\"}",
           "{\"ok\":false,\"err\":\"bad act\",\"code\":\"bad_act\"}\n");
    EXPECT("{\"op\":\"slot\",\"act\":\"commit\",\"slot\":\"C\"}",
           "{\"ok\":false,\"err\":\"bad slot\",\"code\":\"bad_slot\"}\n");
    EXPECT("{\"op\":\"slot\",\"act\":\"commit\",\"slot\":\"a\"}",
           "{\"ok\":false,\"err\":\"bad slot\",\"code\":\"bad_slot\"}\n");
    EXPECT("{\"op\":\"slots\",\"act\":\"status\"}", "{\"ok\":false,\"err\":\"unknown op\"}\n");
}

#ifndef TEST_SLOT_PROVIDER
/* ---- bare metal: the weak provider declines every act ----------------------- */
static void test_baremetal_declines(void)
{
    static const char *const acts[] = { "status", "commit", "rollback", "verify" };
    for (unsigned i = 0; i < 4; i++) {
        char req[96];
        snprintf(req, sizeof(req), "{\"op\":\"slot\",\"act\":\"%s\"}", acts[i]);
        EXPECT(req, "{\"ok\":false,\"err\":\"slot not supported\",\"code\":\"not_supported\"}\n");
        snprintf(req, sizeof(req), "{\"op\":\"slot\",\"act\":\"%s\",\"slot\":\"B\"}", acts[i]);
        EXPECT(req, "{\"ok\":false,\"err\":\"slot not supported\",\"code\":\"not_supported\"}\n");
    }
    /* ...and the verb touched no register: the dispatch left DFXCTL/CLKRST as boot did. */
    CHECK(strcmp(line("{\"op\":\"ping\"}"),
                 "{\"ok\":true,\"shell_id\":\"0x61bc6789\",\"rm_id\":\"0x00000000\"}\n") == 0);
}

/* The weak mps3_slot_supported(): no "slot" in version.features on bare metal. */
/* v0.17: bare metal declines presence and the panel (the weak providers), with no
 * claim to lock -- a page change is declined the same way. */
static void test_baremetal_declines_hello_and_panel(void)
{
    EXPECT("{\"op\":\"hello\",\"v\":1,\"sid\":\"a1\",\"who\":\"d@h\",\"lease\":{\"by\":\"d\"}}",
           "{\"ok\":false,\"err\":\"hello not supported\",\"code\":\"not_supported\"}\n");
    EXPECT("{\"op\":\"panel\"}",
           "{\"ok\":false,\"err\":\"panel not supported\",\"code\":\"not_supported\"}\n");
    EXPECT("{\"op\":\"panel\",\"page\":\"apps\"}",
           "{\"ok\":false,\"err\":\"panel not supported\",\"code\":\"not_supported\"}\n");
    EXPECT("{\"op\":\"ping\",\"x\":{\"a\":1}}", "{\"ok\":false,\"err\":\"bad json\"}\n");
}

static void test_baremetal_features_have_no_slot(void)
{
    CHECK(strstr(line("{\"op\":\"version\"}"), "\"slot\"") == NULL);
    CHECK(strstr(s_out, "\"usd\"],") != NULL);           /* usd is still the last name */
}

/* The weak card-job seam: bare metal's `reboot` is exactly v0.11's (only a swap
 * or a missing watchdog refuses it). */
static void test_baremetal_reboot_has_no_card_rule(void)
{
    boot();
    mock_regs_set_hook(MPS3_WDOG_BASE, wdog_hook, NULL);
    CHECK(strncmp(line("{\"op\":\"reboot\"}"), "{\"ok\":true,\"in_ms\":", 19) == 0);
    CHECK(coordinator_reboot_state() == 1);
    mock_regs_set_hook(MPS3_WDOG_BASE, NULL, NULL);
    boot();
}
#else
/* ---- an engine WITH the provider ---------------------------------------------- */
static struct {
    int calls, act, sel;
    const char *fail;
    mps3_slot_status_t st;
} s_p;

/* The engine that serves the verb says so: version.features "slot" (bit 14). */
int mps3_slot_supported(void) { return 1; }

const char *mps3_slot_op(int act, int sel, mps3_slot_status_t *st)
{
    s_p.calls++;
    s_p.act = act;
    s_p.sel = sel;
    if (s_p.fail) {
        return s_p.fail;
    }
    *st = s_p.st;
    return 0;
}

static void typical(void)
{
    memset(&s_p, 0, sizeof(s_p));
    s_p.st.card = 1;
    s_p.st.running = MPS3_SLOT_RUN_A;
    s_p.st.deflt = MPS3_SLOT_A;
    s_p.st.target = MPS3_SLOT_B;
    s_p.st.staged = MPS3_SLOT_B;
    s_p.st.seq = 3;
    s_p.st.fabric_sid = TEST_STATIC_ID;
    s_p.st.s[0].state = MPS3_SLOT_ST_VALID;
    s_p.st.s[0].verified = MPS3_SLOT_VER_BOOT;
    s_p.st.s[0].hdr_crc = 0x3E5E9C2Cu;
    s_p.st.s[0].len = 24354312u;
    s_p.st.s[1].state = MPS3_SLOT_ST_VALID;
    s_p.st.s[1].verified = MPS3_SLOT_VER_READBACK;
    s_p.st.s[1].hdr_crc = 0x0A4DA20Bu;
    s_p.st.s[1].len = 4096u;
    s_p.st.s[1].has_sid = 1;
    s_p.st.s[1].sid = TEST_STATIC_ID;
    s_p.st.job_act = MPS3_SLOT_JOB_PUSH;
    s_p.st.job_state = MPS3_SLOT_JS_OK;
    s_p.st.job_slot = MPS3_SLOT_B;
    s_p.st.job_got = 4096u;
    s_p.st.job_len = 4096u;
    s_p.st.confirmed = 1;     /* HM_ANSWERS S5: this boot confirmed to stage0 */
}

static void test_reply_bytes(void)
{
    typical();
    EXPECT("{\"op\":\"slot\",\"act\":\"status\"}",
           "{\"ok\":true,\"card\":true,\"fabric_sid\":\"0x61bc6789\",\"running\":\"A\","
           "\"default\":\"A\",\"seq\":3,\"target\":\"B\",\"staged\":\"B\","
           "\"a\":{\"state\":\"valid\",\"hdr_crc\":\"0x3e5e9c2c\",\"len\":24354312,"
           "\"verified\":\"boot\"},"
           "\"b\":{\"state\":\"valid\",\"hdr_crc\":\"0x0a4da20b\",\"len\":4096,"
           "\"sid\":\"0x61bc6789\",\"verified\":\"readback\"},"
           "\"job\":{\"act\":\"push\",\"slot\":\"B\",\"state\":\"ok\",\"got\":4096,"
           "\"len\":4096,\"err\":\"\"},\"claimed\":false,\"confirmed\":true}\n");
    CHECK(s_p.act == MPS3_SLOT_ACT_STATUS && s_p.sel == MPS3_SLOT_NONE);

    /* HM_ANSWERS S2/S5: `claimed` and `confirmed` are the provider's, appended
     * after `job` -- the rest of the line is the v0.14 line, byte for byte. */
    typical();
    s_p.st.claimed = 1;
    s_p.st.confirmed = 0;
    line("{\"op\":\"slot\",\"act\":\"status\"}");
    CHECK(strstr(s_out, "\"err\":\"\"},\"claimed\":true,\"confirmed\":false}\n") != NULL);

    /* every act answers the SAME shape; the act and selector reach the provider */
    typical();
    line("{\"op\":\"slot\",\"act\":\"commit\",\"slot\":\"B\"}");
    CHECK(s_p.act == MPS3_SLOT_ACT_COMMIT && s_p.sel == MPS3_SLOT_B);
    CHECK(strncmp(s_out, "{\"ok\":true,\"card\":true,", 23) == 0);
    line("{\"slot\":\"A\",\"act\":\"rollback\",\"op\":\"slot\"}");   /* any key order */
    CHECK(s_p.act == MPS3_SLOT_ACT_ROLLBACK && s_p.sel == MPS3_SLOT_A);
    line("{\"op\":\"slot\",\"act\":\"verify\",\"extra\":true}");     /* extras ignored */
    CHECK(s_p.act == MPS3_SLOT_ACT_VERIFY && s_p.sel == MPS3_SLOT_NONE);

    /* bad / io slots carry err; a slot with no target and nothing staged is null */
    typical();
    s_p.st.target = MPS3_SLOT_NONE;
    s_p.st.staged = MPS3_SLOT_NONE;
    s_p.st.s[0].state = MPS3_SLOT_ST_BAD;
    snprintf(s_p.st.s[0].err, sizeof(s_p.st.s[0].err), "table CRC");
    s_p.st.s[0].verified = MPS3_SLOT_VER_NO;
    s_p.st.s[1].state = MPS3_SLOT_ST_IO;
    snprintf(s_p.st.s[1].err, sizeof(s_p.st.s[1].err), "io");
    s_p.st.s[1].verified = MPS3_SLOT_VER_NO;
    s_p.st.job_act = MPS3_SLOT_JOB_VERIFY;
    s_p.st.job_state = MPS3_SLOT_JS_FAILED;
    s_p.st.job_slot = MPS3_SLOT_A;
    snprintf(s_p.st.job_err, sizeof(s_p.st.job_err), "payload \"CRC\"");
    EXPECT("{\"op\":\"slot\",\"act\":\"status\"}",
           "{\"ok\":true,\"card\":true,\"fabric_sid\":\"0x61bc6789\",\"running\":\"A\","
           "\"default\":\"A\",\"seq\":3,\"target\":null,\"staged\":null,"
           "\"a\":{\"state\":\"bad\",\"err\":\"table CRC\",\"verified\":\"no\"},"
           "\"b\":{\"state\":\"io\",\"err\":\"io\",\"verified\":\"no\"},"
           "\"job\":{\"act\":\"verify\",\"slot\":\"A\",\"state\":\"failed\",\"got\":4096,"
           "\"len\":4096,\"err\":\"payload \\\"CRC\\\"\"},\"claimed\":false,\"confirmed\":true}\n");
}

static void test_no_card_and_declines(void)
{
    memset(&s_p, 0, sizeof(s_p));
    s_p.st.card = 0;
    s_p.st.running = MPS3_SLOT_RUN_RESCUE;
    s_p.st.fabric_sid = TEST_STATIC_ID;
    s_p.st.claimed = 1;       /* the no-card form carries both too */
    EXPECT("{\"op\":\"slot\",\"act\":\"status\"}",
           "{\"ok\":true,\"card\":false,\"fabric_sid\":\"0x61bc6789\",\"running\":\"rescue\","
           "\"staged\":null,\"job\":{\"act\":\"none\",\"slot\":null,\"state\":\"idle\","
           "\"got\":0,\"len\":0,\"err\":\"\"},\"claimed\":true,\"confirmed\":false}\n");

    s_p.fail = "no card";
    EXPECT("{\"op\":\"slot\",\"act\":\"commit\"}",
           "{\"ok\":false,\"err\":\"no card\",\"code\":\"no_card\"}\n");
    s_p.fail = "slot B not verified";
    EXPECT("{\"op\":\"slot\",\"act\":\"rollback\"}",
           "{\"ok\":false,\"err\":\"slot B not verified\",\"code\":\"not_verified\"}\n");

    /* the shared checks never reach the provider */
    s_p.calls = 0;
    line("{\"op\":\"slot\",\"act\":\"format\"}");
    line("{\"op\":\"slot\",\"act\":\"status\",\"slot\":\"C\"}");
    CHECK(s_p.calls == 0);
}

/* A provider bug must not become a malformed line: every out-of-range
 * enumeration fails the encode, and dispatch falls back to its fixed line. */
static void test_out_of_range_fails_closed(void)
{
    const char *fallback = "{\"ok\":false,\"err\":\"encode overflow\"}\n";
    typical(); s_p.st.running = 5;                   EXPECT("{\"op\":\"slot\",\"act\":\"status\"}", fallback);
    typical(); s_p.st.deflt = MPS3_SLOT_NONE;        EXPECT("{\"op\":\"slot\",\"act\":\"status\"}", fallback);
    typical(); s_p.st.deflt = 3;                     EXPECT("{\"op\":\"slot\",\"act\":\"status\"}", fallback);
    typical(); s_p.st.target = 9;                    EXPECT("{\"op\":\"slot\",\"act\":\"status\"}", fallback);
    typical(); s_p.st.staged = 3;                    EXPECT("{\"op\":\"slot\",\"act\":\"status\"}", fallback);
    typical(); s_p.st.s[1].state = 5;                EXPECT("{\"op\":\"slot\",\"act\":\"status\"}", fallback);
    typical(); s_p.st.s[0].verified = 3;             EXPECT("{\"op\":\"slot\",\"act\":\"status\"}", fallback);
    typical(); s_p.st.job_act = 3;                   EXPECT("{\"op\":\"slot\",\"act\":\"status\"}", fallback);
    typical(); s_p.st.job_state = 5;                 EXPECT("{\"op\":\"slot\",\"act\":\"status\"}", fallback);
    typical(); s_p.st.job_slot = 4;                  EXPECT("{\"op\":\"slot\",\"act\":\"status\"}", fallback);
    /* negative control: the same struct, in range, encodes */
    typical();
    CHECK(strncmp(line("{\"op\":\"slot\",\"act\":\"status\"}"), "{\"ok\":true,", 10) == 0);
}

/* The worst case through the REAL encoder: every string at full width, every
 * number at max width, every optional key present. */
static void test_worst_case_fits(void)
{
    mps3_ctrl_response_t r;
    memset(&r, 0, sizeof(r));
    r.op = MPS3_OP_SLOT;
    r.ok = 1;
    mps3_slot_status_t *st = &r.slot_st;
    st->card = 1;
    st->running = MPS3_SLOT_RUN_RESCUE;
    st->deflt = MPS3_SLOT_B;
    st->target = MPS3_SLOT_A;
    st->staged = MPS3_SLOT_A;
    st->seq = 0xFFFFFFFFu;
    st->fabric_sid = 0xFFFFFFFFu;
    for (int i = 0; i < 2; i++) {
        st->s[i].state = MPS3_SLOT_ST_VALID;
        st->s[i].verified = MPS3_SLOT_VER_READBACK;
        st->s[i].has_sid = 1;
        st->s[i].hdr_crc = st->s[i].len = st->s[i].sid = 0xFFFFFFFFu;
    }
    st->job_act = MPS3_SLOT_JOB_VERIFY;
    st->job_state = MPS3_SLOT_JS_VERIFYING;
    st->job_slot = MPS3_SLOT_B;
    st->job_got = st->job_len = 0xFFFFFFFFu;
    memset(st->job_err, '"', sizeof(st->job_err) - 1u);   /* escapes double it */
    memset(st->job_code, '"', sizeof(st->job_code) - 1u);
    char buf[MPS3_CTRL_RESP_MAX];
    int n = mps3_ctrl_encode_response(&r, buf, (int)sizeof(buf));
    CHECK(n > 0 && n < MPS3_CTRL_RESP_MAX);
    int valid_len = n;
    /* ...and a bad/io variant (err strings instead of the valid keys) */
    for (int i = 0; i < 2; i++) {
        st->s[i].state = MPS3_SLOT_ST_BAD;
        memset(st->s[i].err, '\\', sizeof(st->s[i].err) - 1u);
    }
    n = mps3_ctrl_encode_response(&r, buf, (int)sizeof(buf));
    CHECK(n > 0 && n < MPS3_CTRL_RESP_MAX);
    printf("  slot worst line = %d B (valid) / %d B (bad) (RESP_MAX %d)\n",
           valid_len, n, MPS3_CTRL_RESP_MAX);
    /* a buffer one byte short fails closed, never a truncated line */
    CHECK(mps3_ctrl_encode_response(&r, buf, n) < 0 && buf[0] == '\0');
}

/* HM_ANSWERS S3/C2: every provider refusal text in net-protocol.md "The acts" maps
 * to its stable code; an unlisted text gets NO code key (never a guess). */
static void test_refusal_codes(void)
{
    static const struct { const char *err, *code; } k[] = {
        { "slot locked: board claimed (use ssh)",           "locked"         },
        { "card io",                                        "card_io"        },
        { "no stage0 block",                                "no_stage0"      },
        { "fabric static_id unknown",                       "fabric_unknown" },
        { "EBUSY",                                          "busy"           },
        { "nothing staged: push an image first",            "nothing_staged" },
        { "slot mismatch: commit would pick B",             "slot_mismatch"  },
        { "slot A is not a valid image",                    "not_valid"      },
        { "slot B not verified",                            "not_verified"   },
        { "slot A changed since it was verified",           "changed"        },
        { "slot B is for 0x0badcafe != fabric 0x61bc6789",  "wrong_static"   },
        { "slot A runs, but identity lock: no valid stage0", "identity_lock" },
        { "boot-select write: Input/output error",          "bootsel"        },
        { "boot-select read-back (default 1 seq 3)",        "bootsel"        },
        { "boot-select refused: LBA 1-2 inside MBR entry 4", "bootsel"       },
    };
    char want[160];
    for (unsigned i = 0; i < sizeof(k) / sizeof(k[0]); i++) {
        memset(&s_p, 0, sizeof(s_p));
        s_p.fail = k[i].err;
        snprintf(want, sizeof(want), "{\"ok\":false,\"err\":\"%s\",\"code\":\"%s\"}\n",
                 k[i].err, k[i].code);
        EXPECT("{\"op\":\"slot\",\"act\":\"commit\"}", want);
    }
    s_p.fail = "fork: Cannot allocate memory";                 /* unlisted: no key */
    EXPECT("{\"op\":\"slot\",\"act\":\"verify\"}",
           "{\"ok\":false,\"err\":\"fork: Cannot allocate memory\"}\n");

    /* job.code rides after job.err only when the provider set one */
    typical();
    s_p.st.job_state = MPS3_SLOT_JS_FAILED;
    snprintf(s_p.st.job_err, sizeof(s_p.st.job_err), "torn (the push stopped early)");
    snprintf(s_p.st.job_code, sizeof(s_p.st.job_code), "torn");
    line("{\"op\":\"slot\",\"act\":\"status\"}");
    CHECK(strstr(s_out, "\"err\":\"torn (the push stopped early)\",\"code\":\"torn\"},"
                        "\"claimed\"") != NULL);
    s_p.st.job_code[0] = '\0';
    line("{\"op\":\"slot\",\"act\":\"status\"}");
    CHECK(strstr(s_out, "\"code\"") == NULL);
}

/* ---- the CLAIM-LOCK seam on the D13 store's mutations (HM_ANSWERS S6) ------- */
static int s_claim_lock;
static const char *s_lock_what;
int mps3_claim_refuses_active_peer(const char *what)
{
    s_lock_what = what;
    return s_claim_lock;
}

#define COMMIT_REQ "{\"op\":\"commit\",\"rm\":\"led\",\"src\":\"tcp\",\"rm_id\":\"0x00000000\"," \
                   "\"static_id\":\"0x61bc6789\",\"clear_len\":64,\"clear_crc\":\"0x00000000\"," \
                   "\"part_len\":128,\"part_crc\":\"0x00000000\"}"

static void test_store_mutations_take_the_claim_lock(void)
{
    boot();
    s_claim_lock = 1;
    EXPECT("{\"op\":\"usd\",\"action\":\"format\",\"confirm\":\"erase\"}",
           "{\"ok\":false,\"err\":\"usd locked: board claimed (use ssh)\",\"code\":\"locked\"}\n");
    CHECK(strcmp(s_lock_what, "usd") == 0);
    EXPECT("{\"op\":\"usd\",\"action\":\"clear\"}",
           "{\"ok\":false,\"err\":\"usd locked: board claimed (use ssh)\",\"code\":\"locked\"}\n");
    EXPECT("{\"op\":\"usd\",\"action\":\"rescan\"}",
           "{\"ok\":false,\"err\":\"usd locked: board claimed (use ssh)\",\"code\":\"locked\"}\n");
    EXPECT(COMMIT_REQ,
           "{\"ok\":false,\"err\":\"commit locked: board claimed (use ssh)\",\"code\":\"locked\"}\n");
    CHECK(strcmp(s_lock_what, "commit") == 0);
    s_lock_what = 0;
    CHECK(strncmp(line("{\"op\":\"usd\"}"), "{\"ok\":true,", 10) == 0);   /* status: open */
    CHECK(s_lock_what == 0);                                            /* ...not even asked */
    /* the negative control: the same requests with the lock off are not refused by it */
    s_claim_lock = 0;
    CHECK(strstr(line("{\"op\":\"usd\",\"action\":\"rescan\"}"), "locked") == NULL);
    int n = coordinator_dispatch_line(COMMIT_REQ, (int)strlen(COMMIT_REQ), s_out, (int)sizeof(s_out));
    CHECK(n == 0 || strstr(s_out, "locked") == NULL);                  /* held, or another answer */
    boot();
}

/* v0.17: `panel` with a `page` takes the claim lock FIRST (coordinator.c), before
 * the provider -- here the weak one, which would otherwise decline; `hello` and the
 * panel reads never ask the lock. */
static void test_panel_page_takes_the_claim_lock(void)
{
    s_claim_lock = 1;
    s_lock_what = 0;
    EXPECT("{\"op\":\"panel\",\"page\":\"apps\"}",
           "{\"ok\":false,\"err\":\"panel locked: board claimed (use ssh)\",\"code\":\"locked\"}\n");
    CHECK(s_lock_what != 0 && strcmp(s_lock_what, "panel") == 0);
    EXPECT("{\"op\":\"panel\",\"page\":42}",
           "{\"ok\":false,\"err\":\"panel locked: board claimed (use ssh)\",\"code\":\"locked\"}\n");
    s_lock_what = 0;
    EXPECT("{\"op\":\"panel\"}",
           "{\"ok\":false,\"err\":\"panel not supported\",\"code\":\"not_supported\"}\n");
    EXPECT("{\"op\":\"panel\",\"frame\":\"a\"}",
           "{\"ok\":false,\"err\":\"panel not supported\",\"code\":\"not_supported\"}\n");
    EXPECT("{\"op\":\"hello\",\"sid\":\"a1\",\"who\":\"d@h\"}",
           "{\"ok\":false,\"err\":\"hello not supported\",\"code\":\"not_supported\"}\n");
    CHECK(s_lock_what == 0);           /* the reads and hello never asked */
    s_claim_lock = 0;
    EXPECT("{\"op\":\"panel\",\"page\":\"apps\"}",
           "{\"ok\":false,\"err\":\"panel not supported\",\"code\":\"not_supported\"}\n");
}

static void test_features_report_slot(void)
{
    boot();
    /* APPENDED after usd (bit 14), and only because the provider says so. */
    CHECK(strstr(line("{\"op\":\"version\"}"), "\"usd\",\"slot\"],") != NULL);
}

/* ---- the CARD-JOB seam: `reboot` waits for the card --------------------------- */
static int s_card_job;
int mps3_card_job_active(void) { return s_card_job; }

static void test_reboot_refused_during_a_card_job(void)
{
    boot();
    mock_regs_set_hook(MPS3_WDOG_BASE, wdog_hook, NULL);
    s_card_job = 1;                        /* a slot push / verify / store job runs */
    EXPECT("{\"op\":\"reboot\"}", "{\"ok\":false,\"err\":\"EBUSY\"}\n");
    EXPECT("{\"op\":\"reboot\"}", "{\"ok\":false,\"err\":\"EBUSY\"}\n");   /* still */
    CHECK(coordinator_reboot_state() == 0);                  /* nothing was requested */
    coordinator_reboot_poll();
    CHECK(mock_regs_peek(MPS3_WDOG_BASE, WDOG_TWCSR0) == 0u);  /* ...and nothing armed */
    s_card_job = 0;                        /* the job ended: the same request goes */
    CHECK(strncmp(line("{\"op\":\"reboot\"}"), "{\"ok\":true,\"in_ms\":", 19) == 0);
    CHECK(coordinator_reboot_state() == 1);
    mock_regs_set_hook(MPS3_WDOG_BASE, NULL, NULL);
    boot();
}
#endif

int main(void)
{
    boot();
#ifdef TEST_SLOT_PROVIDER
    memset(&s_p, 0, sizeof(s_p));
    s_p.fail = "unreached";   /* the shared checks must answer before the provider */
    test_shared_request_checks();
    CHECK(s_p.calls == 0);
    test_reply_bytes();
    test_no_card_and_declines();
    test_out_of_range_fails_closed();
    test_worst_case_fits();
    test_reboot_refused_during_a_card_job();
    test_features_report_slot();
    test_refusal_codes();
    test_store_mutations_take_the_claim_lock();
    test_panel_page_takes_the_claim_lock();
    printf("test_slot_dispatch_provider: ALL PASS (%d checks)\n", s_checks);
#else
    test_shared_request_checks();
    test_baremetal_declines();
    test_baremetal_reboot_has_no_card_rule();
    test_baremetal_features_have_no_slot();
    test_baremetal_declines_hello_and_panel();
    printf("test_slot_dispatch: ALL PASS (%d checks)\n", s_checks);
#endif
    return 0;
}
