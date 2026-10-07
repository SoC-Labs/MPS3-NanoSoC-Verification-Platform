/*
 * locate_linux.c — `locate`: "which of these boards is this one?" (net-protocol.md
 * v0.16 "Locate"; the Harness Manager's request R3, docs/design/CLCD_ALIGNMENT.md).
 *
 *   {"op":"locate","s":1..30[,"who":"user@host"]}  -> {"ok":true,"op":"locate","until_ms":N}
 *   {"op":"locate","s":0}                               -> stops it (until_ms 0)
 *
 * While it runs:
 *   - THE BLINK: the panel backlight toggles every 250 ms (a 500 ms period, 2 Hz)
 *     through clcd_kvm_set_backlight() -- ONE KVM CTRL write per edge and zero
 *     pixel bytes -- from the clcd service row (harnessd_locate_tick(), called by
 *     main_linux.c's svc_clcd before the drain). The KVM owns BL whoever owns the
 *     panel, so the blink shows while the DUT owns it too (the DUT's picture blinks).
 *   - THE BANNER: while the harness owns the panel, rows 10-12 show
 *     "IDENTIFY: <who>" (clcd.c's mps3_clcd_locate() seam; fault banners outrank it).
 *   - THE TAP: a tap on the panel stops it ("found it": clcd.c's mps3_clcd_tap()
 *     seam, asked first by clcd_hittest()). Logged to the harness log (the `log`
 *     verb serves it), with who and how long it took.
 * One at a time: a new locate replaces the old (its time and who; the blink goes
 * on). Not claim-locked: it changes nothing but the light, and a tool sends it
 * over Ethernet. `until_ms` is RELATIVE: the ms from this reply to the locate's end
 * (0 = stopped), which is how the Harness Manager reads it.
 *
 * THE RESTORE: the backlight is ON whenever no locate runs -- set when a locate
 * ends (time, s=0, a tap; a replace is not an end), and at every harnessd start by
 * clcd_kvm_init() (bl_rst_src + backlight + panel released, one write), so a
 * harnessd that died mid-blink comes back lit.
 *
 * A build without the panel (no MPS3_HAS_CLCD: the ctrl-echo twin) declines the
 * verb ("locate not supported", code "not_supported") and reports no "locate"
 * feature.
 */
#define _GNU_SOURCE
#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "../../../../firmware/common/net_proto.h"
#include "../../../../firmware/coordinator/coordinator.h"
#include "harnessd.h"

#ifdef MPS3_HAL_MOCK
int g_locate_negctl_no_restore;     /* MOCK tests only: the restore's negative control */
#endif

#ifdef MPS3_HAS_CLCD
#include "../../../../firmware/clcd/clcd.h"
#include "../../../../firmware/clcd_kvm/clcd_kvm.h"

#define LOCATE_MAX_S        30u
#define LOCATE_HALF_US      250000u     /* one edge per 250 ms = a 500 ms (2 Hz) blink */
#define LOCATE_WHO_MAX      32u

static struct {
    int      active;
    uint64_t start_us, until_us, next_edge_us;
    int      bl_on;
    char     who[LOCATE_WHO_MAX + 1u];
    uint32_t edges;                 /* backlight writes this locate (the log)    */
    uint32_t found;                 /* locates ended by a tap, since start       */
} L = { 0, 0, 0, 0, 1, "", 0, 0 };

static void backlight(int on)
{
    clcd_kvm_set_backlight(on);
    L.bl_on = on;
    L.edges++;
}

static void locate_end(const char *why)
{
    if (!L.active) {
        return;
    }
    L.active = 0;
    uint64_t ran = (harnessd_now_us64() - L.start_us) / 1000u;
#ifdef MPS3_HAL_MOCK
    if (!g_locate_negctl_no_restore)
#endif
    {
        if (!L.bl_on) {
            backlight(1);           /* THE RESTORE: lit whenever no locate runs */
        }
    }
    harnessd_log("locate: ended (%s) after %" PRIu64 " ms, %u backlight edge(s)%s%s\n", why, ran,
                 (unsigned)L.edges, L.who[0] ? ", who " : "", L.who);
}

void harnessd_locate_tick(void)
{
    if (!L.active) {
        return;
    }
    uint64_t now = harnessd_now_us64();
    if (now >= L.until_us) {
        locate_end("its time is up");
        return;
    }
    if (now >= L.next_edge_us) {
        backlight(!L.bl_on);
        L.next_edge_us += LOCATE_HALF_US;
        if (L.next_edge_us <= now) {          /* a long stall: no burst of catch-up edges */
            L.next_edge_us = now + LOCATE_HALF_US;
        }
    }
}

int harnessd_locate_active(void)
{
    return L.active;
}

/* clcd.h seams ---------------------------------------------------------------- */

int mps3_clcd_locate(char *who, unsigned cap)
{
    if (!L.active) {
        return 0;
    }
    if (cap) {
        snprintf(who, cap, "%s", L.who);
    }
    return 1;
}

int mps3_clcd_tap(unsigned x_px, unsigned y_px)
{
    if (!L.active) {
        return 0;
    }
    L.found++;
    harnessd_log("locate: FOUND -- the panel was tapped at (%u,%u) %" PRIu64 " ms into the "
                 "locate%s%s\n", x_px, y_px, (harnessd_now_us64() - L.start_us) / 1000u,
                 L.who[0] ? " by " : "", L.who);
    locate_end("tapped: found it");
    return 1;
}

/* the verb (coordinator.h mps3_locate_op) --------------------------------------- */

static const char *invalid(char *code, size_t cap, const char *msg)
{
    snprintf(code, cap, "invalid");
    return msg;
}

const char *mps3_locate_op(const mps3_ctrl_request_t *req, const char **body, char *code,
                           size_t code_cap)
{
    static char s_body[48];
    mps3_json_obj_t obj;
    int32_t s = -1;
    char who[LOCATE_WHO_MAX + 1u];

    if (!req || !req->line || mps3_json_parse(req->line, req->line_len, &obj) != MPS3_JSON_OK) {
        return invalid(code, code_cap, "invalid request");
    }
    if (mps3_json_get_int(&obj, "s", &s) != MPS3_JSON_OK || s < 0 || (uint32_t)s > LOCATE_MAX_S) {
        return invalid(code, code_cap, "invalid s: 0..30 (seconds; 0 stops)");
    }
    who[0] = '\0';
    int rc = mps3_json_get_string(&obj, "who", who, (int)sizeof(who));
    if (rc != MPS3_JSON_OK && rc != MPS3_JSON_EMISSING) {
        return invalid(code, code_cap, "invalid who: a string of <= 32 characters");
    }
    for (const char *w = who; *w; w++) {
        if (*w < ' ' || *w > '~') {
            return invalid(code, code_cap, "invalid who: printable ASCII only");
        }
    }

    uint64_t now = harnessd_now_us64();
    if (s == 0) {
        locate_end("stopped (s 0)");
        snprintf(s_body, sizeof(s_body), "\"until_ms\":0");
    } else {
        int replaced = L.active;
        if (!L.active) {
            L.start_us = now;
            L.next_edge_us = now;             /* the first edge on the next tick */
            L.edges = 0;
            L.bl_on = 1;
        }
        L.active = 1;
        L.until_us = now + (uint64_t)s * 1000000u;
        snprintf(L.who, sizeof(L.who), "%s", who);
        harnessd_log("locate: %s for %d s%s%s\n", replaced ? "REPLACED, now" : "started", (int)s,
                     who[0] ? ", who " : "", who);
        snprintf(s_body, sizeof(s_body), "\"until_ms\":%u", (unsigned)s * 1000u);
    }
    *body = s_body;
    return 0;
}

#else  /* !MPS3_HAS_CLCD: no panel, no locate (the ctrl-echo twin) */

void harnessd_locate_tick(void) { }
int  harnessd_locate_active(void) { return 0; }

const char *mps3_locate_op(const mps3_ctrl_request_t *req, const char **body, char *code,
                           size_t code_cap)
{
    (void)req;
    (void)body;
    snprintf(code, code_cap, "not_supported");
    return "locate not supported";
}

#endif
