/*
 * test_usd_dispatch.c — the v0.13 `usd` verb and `commit`, through the REAL
 * dispatcher, handlers and codec (coordinator.c + net_proto.c), with the store
 * glue SCRIPTED (fake_overlay_store.c). What the glue itself does over a real
 * card is test_usd_boot.c; what the codec does byte for byte is
 * test_net_proto_json.c. Here:
 *
 *   1. `usd` status in EVERY store state, key presence exactly per the contract
 *      (card_mb only when a card is ready, default only in valid / stale);
 *   2. every action: rescan (send-now), format / clear (HELD until the job ends,
 *      success and failure), an unknown action, the confirm strings;
 *   3. commit: the v0.11 form (bad args), src != tcp, the swap FSM busy, the
 *      identity lock, the live rm_id (RM_STATUS.valid gates it), every store
 *      refusal BY NAME, the accepted form HELD, and its final answer;
 *   4. swap: src "usd" is internal (bad args); a swap while a commit or an
 *      action holds the store is "store busy";
 *   5. the error-name table: every OVLSD_* code maps onto a contract name.
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../coordinator/coordinator.h"
#include "../coordinator/swap_fsm.h"
#include "../common/net_proto.h"
#include "../common/platform_regs.h"
#include "mock_regs.h"
#include "fake_config_agent.h"
#include "fake_overlay_store.h"

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

#define TEST_STATIC_ID 0xA1B2C3D4u

/* The identity-lock seam (coordinator.h), STRONG here so a test can lock it. */
static const char *s_refusal;
const char *mps3_swap_refusal(void) { return s_refusal; }
uint32_t mps3_shell_static_id(void) { return TEST_STATIC_ID; }

static void boot(void)
{
    mock_regs_reset();
    fake_config_agent_reset();
    fake_overlay_store_reset();
    overlay_manifest_info_t greybox = {
        .static_id = TEST_STATIC_ID, .rm_id = 0,
        .clear_len_words = 4, .clear_crc32 = 0x1111,
    };
    fake_overlay_store_set_greybox(&greybox, 0);
    s_refusal = NULL;
    coordinator_init();
}

static int dispatch(const char *line, char *out, int out_len)
{
    out[0] = '\0';
    return coordinator_dispatch_line(line, (int)strlen(line), out, out_len);
}

static int held(char *out, int out_len)
{
    mps3_ctrl_response_t resp;
    if (!coordinator_held_poll(&resp)) {
        return 0;
    }
    CHECK(mps3_ctrl_encode_response(&resp, out, out_len) > 0);
    return 1;
}

static mps3_usd_t status(const char *state, const char *text, int present,
                         uint32_t card_mb, int have_def, const char *boot_s)
{
    mps3_usd_t u;
    memset(&u, 0, sizeof u);
    strcpy(u.state, state);
    strcpy(u.text, text);
    strcpy(u.boot, boot_s);
    u.present = present;
    if (card_mb) {
        u.have_card_mb = 1;
        u.card_mb = card_mb;
    }
    if (have_def) {
        u.have_default = 1;
        u.def_rm_id = 0x0100001Eu;
        u.def_static_id = TEST_STATIC_ID;
        u.def_slot = 'B';
    }
    return u;
}

/* 1 */
static void test_status_in_every_state(void)
{
    char out[MPS3_CTRL_RESP_MAX];
    struct {
        mps3_usd_t u;
        const char *want;
    } rows[] = {
        { status("none", "none", 0, 0, 0, "none"),
          "{\"ok\":true,\"present\":false,\"state\":\"none\",\"text\":\"none\",\"boot\":\"none\"}\n" },
        { status("no_hw", "no hw", 0, 0, 0, "none"),
          "{\"ok\":true,\"present\":false,\"state\":\"no_hw\",\"text\":\"no hw\",\"boot\":\"none\"}\n" },
        { status("init", "init", 1, 0, 0, "pending"),
          "{\"ok\":true,\"present\":true,\"state\":\"init\",\"text\":\"init\",\"boot\":\"pending\"}\n" },
        { status("unsupported", "unsupported", 1, 0, 0, "none"),
          "{\"ok\":true,\"present\":true,\"state\":\"unsupported\",\"text\":\"unsupported\","
          "\"boot\":\"none\"}\n" },
        { status("error", "ERR 30", 1, 7580, 0, "none"),
          "{\"ok\":true,\"present\":true,\"state\":\"error\",\"text\":\"ERR 30\",\"card_mb\":7580,"
          "\"boot\":\"none\"}\n" },
        { status("foreign", "foreign", 1, 7580, 0, "none"),
          "{\"ok\":true,\"present\":true,\"state\":\"foreign\",\"text\":\"foreign\",\"card_mb\":7580,"
          "\"boot\":\"none\"}\n" },
        { status("empty", "empty", 1, 7580, 0, "none"),
          "{\"ok\":true,\"present\":true,\"state\":\"empty\",\"text\":\"empty\",\"card_mb\":7580,"
          "\"boot\":\"none\"}\n" },
        { status("valid", "led [B]", 1, 7580, 1, "loaded"),
          "{\"ok\":true,\"present\":true,\"state\":\"valid\",\"text\":\"led [B]\",\"card_mb\":7580,"
          "\"default\":{\"rm_id\":\"0x0100001e\",\"static_id\":\"0xa1b2c3d4\",\"slot\":\"B\"},"
          "\"boot\":\"loaded\"}\n" },
        { status("stale", "stale key", 1, 7580, 1, "none"),
          "{\"ok\":true,\"present\":true,\"state\":\"stale\",\"text\":\"stale key\",\"card_mb\":7580,"
          "\"default\":{\"rm_id\":\"0x0100001e\",\"static_id\":\"0xa1b2c3d4\",\"slot\":\"B\"},"
          "\"boot\":\"none\"}\n" },
        { status("bad", "bad", 1, 7580, 0, "failed:crc"),
          "{\"ok\":true,\"present\":true,\"state\":\"bad\",\"text\":\"bad\",\"card_mb\":7580,"
          "\"boot\":\"failed:crc\"}\n" },
        { status("valid", "skipped", 1, 7580, 1, "skipped"),
          "{\"ok\":true,\"present\":true,\"state\":\"valid\",\"text\":\"skipped\",\"card_mb\":7580,"
          "\"default\":{\"rm_id\":\"0x0100001e\",\"static_id\":\"0xa1b2c3d4\",\"slot\":\"B\"},"
          "\"boot\":\"skipped\"}\n" },
    };
    printf("- usd: status in every state\n");
    boot();
    for (size_t i = 0; i < sizeof rows / sizeof rows[0]; i++) {
        fake_overlay_store_set_status(&rows[i].u);
        CHECK(dispatch("{\"op\":\"usd\"}", out, sizeof out) == 1);
        if (strcmp(out, rows[i].want) != 0) {
            printf("row %zu:\n got  %s want %s", i, out, rows[i].want);
        }
        CHECK(strcmp(out, rows[i].want) == 0);
    }
    /* Status is never held -- it answers during a swap too. */
    CHECK(dispatch("{\"op\":\"swap\",\"rm\":\"x\",\"src\":\"tcp\"}", out, sizeof out) == 0);
    CHECK(dispatch("{\"op\":\"usd\"}", out, sizeof out) == 1);
    CHECK(strncmp(out, "{\"ok\":true,\"present\":", 21) == 0);
}

/* 2 */
static void test_actions(void)
{
    char out[MPS3_CTRL_RESP_MAX];
    printf("- usd: actions (rescan now; format / clear held)\n");
    boot();
    mps3_usd_t u = status("init", "init", 1, 0, 0, "none");
    fake_overlay_store_set_status(&u);

    /* rescan: send-now, with the resulting state. */
    fake_overlay_store_set_action(OVLSD_OK, OVLSD_OK);
    CHECK(dispatch("{\"op\":\"usd\",\"action\":\"rescan\"}", out, sizeof out) == 1);
    CHECK(strcmp(out, "{\"ok\":true,\"state\":\"init\"}\n") == 0);
    CHECK(strcmp(fake_overlay_store_last_action(), "rescan") == 0);

    /* format: HELD until the job ends, then the state. */
    fake_overlay_store_set_action(OVLSD_BUSY, OVLSD_OK);
    u = status("empty", "empty", 1, 7580, 0, "none");
    fake_overlay_store_set_status(&u);
    CHECK(dispatch("{\"op\":\"usd\",\"action\":\"format\",\"confirm\":\"erase\"}", out, sizeof out) == 0);
    CHECK(out[0] == '\0' && coordinator_held_pending());
    CHECK(strcmp(fake_overlay_store_last_confirm(), "erase") == 0);
    CHECK(held(out, sizeof out) == 1);
    CHECK(strcmp(out, "{\"ok\":true,\"state\":\"empty\"}\n") == 0);
    CHECK(!coordinator_held_pending());

    /* the wipe string reaches the store verbatim */
    fake_overlay_store_set_action(OVLSD_BUSY, OVLSD_ENOWIPE);
    CHECK(dispatch("{\"op\":\"usd\",\"action\":\"format\",\"confirm\":\"erase-all\"}", out, sizeof out) == 0);
    CHECK(strcmp(fake_overlay_store_last_confirm(), "erase-all") == 0);
    CHECK(held(out, sizeof out) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"wipe disabled\"}\n") == 0);

    /* clear, held, failing by name */
    fake_overlay_store_set_action(OVLSD_BUSY, OVLSD_EIO);
    CHECK(dispatch("{\"op\":\"usd\",\"action\":\"clear\"}", out, sizeof out) == 0);
    CHECK(held(out, sizeof out) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"io\"}\n") == 0);

    /* the request is judged before the card: an unknown action, a wrong or a
     * missing confirmation (exact strings only) */
    fake_overlay_store_set_action(OVLSD_ENOCARD, OVLSD_OK);
    CHECK(dispatch("{\"op\":\"usd\",\"action\":\"defrag\"}", out, sizeof out) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"bad args\"}\n") == 0);
    CHECK(dispatch("{\"op\":\"usd\",\"action\":\"format\"}", out, sizeof out) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"confirm required\"}\n") == 0);
    CHECK(dispatch("{\"op\":\"usd\",\"action\":\"format\",\"confirm\":\"ERASE-ALL\"}", out, sizeof out) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"confirm required\"}\n") == 0);
    CHECK(dispatch("{\"op\":\"usd\",\"action\":\"clear\"}", out, sizeof out) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"no card\"}\n") == 0);

    /* refusals, send-now, by name */
    struct { int rc; const char *err; } refusals[] = {
        { OVLSD_ECONFIRM, "confirm required" }, { OVLSD_ENOCARD,  "no card" },
        { OVLSD_ENOHW,    "no hw" },            { OVLSD_EFOREIGN, "foreign" },
        { OVLSD_EFSSIG,   "filesystem present" },{ OVLSD_EEXIST,   "exists" },
        { OVLSD_ESMALL,   "partition too small" },{ OVLSD_ENOWIPE, "wipe disabled" },
        { OVLSD_EBUSY,    "store busy" },       { OVLSD_EARG,     "bad args" },
    };
    for (size_t i = 0; i < sizeof refusals / sizeof refusals[0]; i++) {
        char want[96];
        fake_overlay_store_set_action(refusals[i].rc, OVLSD_OK);
        CHECK(dispatch("{\"op\":\"usd\",\"action\":\"format\",\"confirm\":\"erase\"}", out, sizeof out) == 1);
        snprintf(want, sizeof want, "{\"ok\":false,\"err\":\"%s\"}\n", refusals[i].err);
        CHECK(strcmp(out, want) == 0);
        CHECK(!coordinator_held_pending());
    }
}

static const char k_commit[] =
    "{\"op\":\"commit\",\"rm\":\"led\",\"src\":\"tcp\",\"rm_id\":\"0x0100001e\","
    "\"static_id\":\"0xa1b2c3d4\",\"clear_len\":68332,\"clear_crc\":\"0x11112222\","
    "\"part_len\":1251884,\"part_crc\":\"0x33334444\"}";

/* 3 */
static void test_commit(void)
{
    char out[MPS3_CTRL_RESP_MAX];
    printf("- commit: accept, every refusal, the held answer\n");
    boot();

    /* the v0.11 form */
    CHECK(dispatch("{\"op\":\"commit\",\"rm\":\"led\"}", out, sizeof out) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"bad args\"}\n") == 0);

    /* src must be tcp */
    CHECK(dispatch("{\"op\":\"commit\",\"rm\":\"led\",\"src\":\"tftp\",\"rm_id\":\"0x1\","
                   "\"static_id\":\"0x2\",\"clear_len\":4,\"clear_crc\":\"0x3\",\"part_len\":4,"
                   "\"part_crc\":\"0x4\"}", out, sizeof out) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"bad args\"}\n") == 0);

    /* identity lock (Linux), before anything is touched */
    fake_overlay_store_set_commit(OVLSD_OK, OVLSD_OK, 'A');
    s_refusal = "identity lock: no valid stage0 status block";
    CHECK(dispatch(k_commit, out, sizeof out) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"identity lock: no valid stage0 status block\"}\n") == 0);
    CHECK(!coordinator_held_pending());
    s_refusal = NULL;

    /* the live rm_id: DFXCTL.RM_ID only while RM_STATUS says it is valid */
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_ID, 0x0100001Eu);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS, 0u);
    fake_overlay_store_set_commit(OVLSD_ERMID, OVLSD_OK, 'A');
    CHECK(dispatch(k_commit, out, sizeof out) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"rm mismatch\"}\n") == 0);
    CHECK(fake_overlay_store_last_commit_live_rm() != 0x0100001Eu);   /* never "valid" */
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS, DFXCTL_RM_STATUS_RM_ID_VALID);

    /* every store refusal, by name */
    struct { int rc; const char *err; } refusals[] = {
        { OVLSD_ENOCARD, "no card" },   { OVLSD_ENOHW, "no hw" },
        { OVLSD_EFOREIGN, "foreign" },  { OVLSD_ESTATIC, "stale key" },
        { OVLSD_ERMID, "rm mismatch" }, { OVLSD_EBUSY, "store busy" },
        { OVLSD_ENOTREADY, "unavailable" }, { OVLSD_ETOOBIG, "bad args" },
        { OVLSD_EARG, "bad args" },
    };
    for (size_t i = 0; i < sizeof refusals / sizeof refusals[0]; i++) {
        char want[96];
        fake_overlay_store_set_commit(refusals[i].rc, OVLSD_OK, 'A');
        CHECK(dispatch(k_commit, out, sizeof out) == 1);
        snprintf(want, sizeof want, "{\"ok\":false,\"err\":\"%s\"}\n", refusals[i].err);
        CHECK(strcmp(out, want) == 0);
    }

    /* accepted: HELD; the request's fields and the live ids reach the store */
    fake_overlay_store_set_commit(OVLSD_OK, OVLSD_OK, 'B');
    CHECK(dispatch(k_commit, out, sizeof out) == 0);
    CHECK(out[0] == '\0' && coordinator_held_pending());
    const ovlstore_sd_desc_t *d = fake_overlay_store_last_commit_desc();
    CHECK(d->rm_id == 0x0100001Eu && d->static_id == TEST_STATIC_ID);
    CHECK(d->clear_len == 68332u && d->clear_crc == 0x11112222u);
    CHECK(d->part_len == 1251884u && d->part_crc == 0x33334444u);
    CHECK(fake_overlay_store_last_commit_live_rm() == 0x0100001Eu);
    CHECK(fake_overlay_store_last_commit_live_static() == TEST_STATIC_ID);

    /* 4: while the commit holds the store, a swap is refused */
    CHECK(dispatch("{\"op\":\"swap\",\"rm\":\"x\",\"src\":\"tcp\"}", out, sizeof out) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"store busy\"}\n") == 0);

    CHECK(held(out, sizeof out) == 1);
    CHECK(strcmp(out, "{\"ok\":true,\"slot\":\"B\"}\n") == 0);

    /* a held commit that fails answers by name */
    fake_overlay_store_set_commit(OVLSD_OK, OVLSTORE_ETIMEOUT, 'B');
    CHECK(dispatch(k_commit, out, sizeof out) == 0);
    CHECK(held(out, sizeof out) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"timeout\"}\n") == 0);
    fake_overlay_store_set_commit(OVLSD_OK, OVLSD_EVERIFY, 'B');
    CHECK(dispatch(k_commit, out, sizeof out) == 0);
    CHECK(held(out, sizeof out) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"crc\"}\n") == 0);

    /* the swap FSM busy: store busy, at once */
    fake_overlay_store_set_commit(OVLSD_OK, OVLSD_OK, 'B');
    CHECK(dispatch("{\"op\":\"swap\",\"rm\":\"x\",\"src\":\"tcp\"}", out, sizeof out) == 0);
    CHECK(dispatch(k_commit, out, sizeof out) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"store busy\"}\n") == 0);
}

/* 4 */
static void test_swap_src_usd_is_internal(void)
{
    char out[MPS3_CTRL_RESP_MAX];
    printf("- swap: src \"usd\" is the FSM's internal source\n");
    boot();
    CHECK(dispatch("{\"op\":\"swap\",\"rm\":\"led\",\"src\":\"usd\"}", out, sizeof out) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"bad args\"}\n") == 0);
    CHECK(swap_fsm_idle());
    /* an action holding the store refuses a swap too */
    fake_overlay_store_set_action(OVLSD_BUSY, OVLSD_OK);
    CHECK(dispatch("{\"op\":\"usd\",\"action\":\"clear\"}", out, sizeof out) == 0);
    CHECK(dispatch("{\"op\":\"swap\",\"rm\":\"led\",\"src\":\"tcp\"}", out, sizeof out) == 1);
    CHECK(strcmp(out, "{\"ok\":false,\"err\":\"store busy\"}\n") == 0);
    CHECK(held(out, sizeof out) == 1);
}

/* 5 */
static void test_error_names_are_the_contract_table(void)
{
    static const char *const contract[] = {
        "no card", "no hw", "foreign", "stale key", "unavailable",
        "filesystem present", "exists", "partition too small", "confirm required",
        "wipe disabled", "rm mismatch", "crc", "store busy", "io", "timeout", "bad args",
    };
    printf("- every store code maps onto a contract error NAME\n");
    for (int rc = -30; rc <= -1; rc++) {
        const char *n = overlay_store_err_name(rc);
        int found = 0;
        for (size_t i = 0; i < sizeof contract / sizeof contract[0]; i++) {
            found |= strcmp(n, contract[i]) == 0;
        }
        CHECK(found);
    }
    CHECK(strcmp(overlay_store_err_name(OVLSTORE_ETIMEOUT), "timeout") == 0);
    /* Never an errno number, never "error N". */
    for (int rc = -130; rc <= 2; rc++) {
        const char *n = overlay_store_err_name(rc);
        CHECK(n[0] >= 'a' && n[0] <= 'z');
    }
}

int main(void)
{
    test_status_in_every_state();
    test_actions();
    test_commit();
    test_swap_src_usd_is_internal();
    test_error_names_are_the_contract_table();
    printf("test_usd_dispatch: %d checks passed\n", s_checks);
    return 0;
}
