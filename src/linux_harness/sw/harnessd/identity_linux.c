/*
 * identity_linux.c — the BOARD identity (label, hostname, IP, MAC) inside
 * mps3-harnessd (lane IDENT; the model is identity_core.h).
 *
 * Not to be confused with identity.c, which is the FABRIC identity (static_id).
 * This file answers "which board is this?":
 *
 *   LOAD      at start (step 0, before the no-hw branch, like the manifest):
 *             /run/mps3/identity, written once per boot by `mps3-identity
 *             resolve` (S13mps3identity). Missing or unreadable -> the image
 *             defaults, sources "default", and a log line saying so.
 *   REPORT    the CLCD row-0 label (clcd_set_board_name after clcd_init), the
 *             NET and MAC rows (clcd.h's identity seams), identify's `label`
 *             and `mac` (+ its `ip` when no DHCP lease is held: platform_linux.c).
 *   VERBS     `identity` (read, any peer) and `identity_set` (claim-locked in
 *             coordinator.c before this provider runs): net-protocol.md v0.16
 *             "Identity". The set writes the SAME override file with the SAME
 *             validation as the board CLI (identity_core.c mps3_id_do_set); it
 *             changes nothing live -- it applies at the next boot, and `pending`
 *             says what will change.
 *   FEATURE   version.features gains the engine name "identity" (after
 *             "lcd_mirror"): mps3_proto_features_extra() lives here now.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <string.h>

#include "../../../../firmware/common/net_proto.h"
#include "../../../../firmware/coordinator/coordinator.h"
#include "../../../../firmware/identify/identify.h"
#include "harnessd.h"
#include "identity_core.h"

static mps3_identity_t s_run;
static int             s_have_run;

static void id_log(void *ctx, const char *msg)
{
    (void)ctx;
    harnessd_log("identity: %s\n", msg);
}

void harnessd_board_identity_load(void)
{
    char buf[MPS3_ID_FILE_MAX + 1u];
    int n = mps3_id_read_file(g_hd.identity_run, buf, sizeof(buf));
    s_have_run = (n >= 0 && mps3_id_parse_run(buf, (size_t)n, &s_run) == 0);
    if (!s_have_run) {
        mps3_id_resolve(0, 0, &s_run);
        harnessd_log("identity: %s %s -- the IMAGE DEFAULTS are reported (label %s, %s); "
                     "is S13mps3identity in the image?\n", g_hd.identity_run,
                     n < 0 ? "missing" : "unparsable", s_run.v.label, "192.168.10.101/24");
        return;
    }
    char ip[24], mac[18];
    mps3_id_fmt_cidr(s_run.v.ip, s_run.v.prefix, ip, sizeof(ip));
    mps3_id_fmt_mac_colon(s_run.v.mac, mac);
    harnessd_log("identity: board %s (%s) %s %s -- label %s, hostname %s, ip %s, mac %s\n",
                 s_run.v.label, s_run.v.hostname, ip, mac,
                 mps3_id_src_name(s_run.src[MPS3_ID_LABEL]),
                 mps3_id_src_name(s_run.src[MPS3_ID_HOSTNAME]),
                 mps3_id_src_name(s_run.src[MPS3_ID_IP]),
                 mps3_id_src_name(s_run.src[MPS3_ID_MAC]));
}

const char *harnessd_board_label(void)
{
    return s_run.v.label;
}

void harnessd_board_mac(uint8_t mac[6])
{
    memcpy(mac, s_run.v.mac, 6);
}

uint32_t harnessd_board_ip(void)
{
    return s_run.v.ip;
}

/* ---- the report seams ------------------------------------------------------- */

const char *mps3_identify_label(void)
{
    return s_run.v.label;
}

int mps3_clcd_ident_ip(char out[16])
{
    mps3_id_fmt_ip(s_run.v.ip, out, 16);
    return 1;
}

int mps3_clcd_ident_mac(uint8_t mac[6])
{
    memcpy(mac, s_run.v.mac, 6);
    return 1;
}

/* version.features ENGINE names (net_proto.h v0.15 seam), in a fixed order:
 * "lcd_mirror" while the mirror is configured, "identity" always, then "locate",
 * "presence" and "panel" on a build with the panel (locate_linux.c, v0.16;
 * panel_linux.c, v0.17; the ctrl-echo twin has none). */
const char *const *mps3_proto_features_extra(void)
{
#ifdef MPS3_HAS_CLCD
    static const char *const k_with_mirror[] = { "lcd_mirror", "identity", "locate",
                                                 "presence", "panel", 0 };
    static const char *const k_without[] = { "identity", "locate", "presence", "panel", 0 };
#else
    static const char *const k_with_mirror[] = { "lcd_mirror", "identity", 0 };
    static const char *const k_without[] = { "identity", 0 };
#endif
    return harnessd_lcdmirror_enabled() ? k_with_mirror : k_without;
}

/* ---- the verbs (coordinator.h mps3_identity_op) ------------------------------ */

static int persist_now(void)
{
    return mps3_id_persist_ok(g_hd.persist_state);
}

static const char *fail(char *code, size_t cap, const char *c, const char *msg)
{
    snprintf(code, cap, "%s", c);
    return msg;
}

const char *mps3_identity_op(int set, const mps3_ctrl_request_t *req,
                             const char **body, char *code, size_t code_cap)
{
    static char s_body[MPS3_CTRL_RESP_MAX];
    static char s_err[64];
    mps3_id_status_t st;

    if (!set) {
        mps3_id_gather(g_hd.identity_override, persist_now(), harnessd_s0_block(), &s_run, &st,
                       id_log, 0);
        if (mps3_id_json_status(&st, s_body, sizeof(s_body)) < 0) {
            return fail(code, code_cap, "io", "identity: render");
        }
        *body = s_body;
        return 0;
    }

    /* identity_set: the claim lock ran first (coordinator.c). Then: no card-backed
     * /persist, then the request's shape, then every value (mps3_id_do_set). */
    if (!persist_now()) {
        return fail(code, code_cap, "no_persist", "identity: no persistent /persist (use the card)");
    }
    static const char *const keys[4] = { "label", "hostname", "ip", "mac" };
    char vals[4][80];
    const char *kp[4], *vp[4];
    int n = 0, clear = 0;
    mps3_json_obj_t obj;
    if (!req || !req->line || mps3_json_parse(req->line, req->line_len, &obj) != MPS3_JSON_OK) {
        return fail(code, code_cap, "invalid", "invalid request");
    }
    int rc = mps3_json_get_bool(&obj, "clear", &clear);
    if (rc == MPS3_JSON_EMISSING) {
        clear = 0;
    } else if (rc != MPS3_JSON_OK) {
        return fail(code, code_cap, "invalid", "invalid clear: not a bool");
    }
    for (int k = 0; k < 4; k++) {
        rc = mps3_json_get_string(&obj, keys[k], vals[k], (int)sizeof(vals[k]));
        if (rc == MPS3_JSON_EMISSING) {
            continue;
        }
        if (rc != MPS3_JSON_OK) {
            snprintf(s_err, sizeof(s_err), "invalid %s: %s", keys[k],
                     rc == MPS3_JSON_ETOOLONG ? "too long" : "not a string");
            return fail(code, code_cap, "invalid", s_err);
        }
        kp[n] = keys[k];
        vp[n] = vals[k];
        n++;
    }
    if (clear && n) {
        return fail(code, code_cap, "invalid", "invalid request: clear takes no other field");
    }
    if (!clear && !n) {
        return fail(code, code_cap, "invalid",
                    "invalid request: nothing to set (label/hostname/ip/mac)");
    }
    const char *field = 0, *why = 0;
    rc = mps3_id_do_set(g_hd.identity_override, 1, clear, kp, vp, n, &field, &why);
    if (rc == MPS3_ID_EINVALID) {
        snprintf(s_err, sizeof(s_err), "invalid %s: %s", field, why);
        harnessd_log("identity: identity_set refused: %s\n", s_err);
        return fail(code, code_cap, "invalid", s_err);
    }
    if (rc == MPS3_ID_ENOPERSIST) {
        return fail(code, code_cap, "no_persist", "identity: no persistent /persist (use the card)");
    }
    if (rc != MPS3_ID_OK) {
        snprintf(s_err, sizeof(s_err), "identity: %s", why ? why : "I/O error");
        return fail(code, code_cap, "io", s_err);
    }
    mps3_id_gather(g_hd.identity_override, 1, harnessd_s0_block(), &s_run, &st, id_log, 0);
    char pend[256];
    if (mps3_id_json_pending(&st.next, &s_run, pend, sizeof(pend)) < 0) {
        return fail(code, code_cap, "io", "identity: render");
    }
    snprintf(s_body, sizeof(s_body), "\"persisted\":true,\"pending\":%s,\"applies\":\"reboot\"",
             pend);
    harnessd_log("identity: override %s by identity_set; pending %s (next boot)\n",
                 clear ? "removed" : "written", pend);
    *body = s_body;
    return 0;
}
