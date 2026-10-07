/*
 * codec_dump.c — the byte-identity instrument for this lane's net_proto.c
 * changes (version.impl / version.id_skew / the diag+stats OMIT masks /
 * stats.os_up_ms; v0.15: the engine feature names, version.lcd_mirror and
 * stats.lcd_mirror). It builds N deterministic pseudo-random responses for EVERY
 * op the codec encodes, success and failure, encodes each through
 * mps3_ctrl_encode_response() and prints one line per response (or "ENCODE -1").
 *
 * Linked three ways by the Makefile; tests/test_codec_identity.py compares:
 *   head   net_proto.c at the branch point           \  must be BYTE-IDENTICAL
 *   new    today's net_proto.c, weak seams (bare metal) /
 *   linux  today's net_proto.c + strong seams below    -> differs ONLY by the
 *          documented additive keys and the omitted diag keys
 *
 * It uses only the response fields that exist at the branch point, so the same
 * source compiles against both header trees.
 */
#include <stdio.h>
#include <string.h>

#include "net_proto.h"

#ifdef CODEC_DUMP_LINUX_SEAMS
const char *mps3_proto_impl(void) { return "linux"; }
const char *mps3_proto_id_skew(void) { return "image 0x0badcafe != fabric 0x5a5a0001"; }
int mps3_proto_os_up_ms(uint32_t *out) { *out = 987654321u; return 1; }
/* v0.15: the LCD mirror's engine feature name + version.lcd_mirror + stats.lcd_mirror */
static const char *const k_extra[] = { "lcd_mirror", 0 };
const char *const *mps3_proto_features_extra(void) { return k_extra; }
int mps3_proto_lcd_mirror_info(mps3_lcd_mirror_info_t *o)
{
    o->port = 6940u; o->proto = 1u; strcpy(o->mode, "sw"); return 1;
}
int mps3_proto_lcd_mirror_stats(mps3_lcd_mirror_stats_t *o)
{
    strcpy(o->peer, "127.0.0.1:40123"); o->since_ms = 1234u; o->fps_x10 = 45u; o->bytes = 987654u;
    return 1;
}
uint32_t mps3_proto_stats_omit(void) { return 0u; }
#define O(n) ((uint64_t)1u << (unsigned)MPS3_DIAG_IX_##n)
uint64_t mps3_proto_diag_omit(void)
{
    return O(rx_recover_events) | O(rx_recover_dumps) | O(tx_status_drained) |
           O(tx_fifo_full_drops) | O(tx_space_stalls) | O(tx_iface_errors) |
           O(tx_last_status) | O(tcp_rcv_wnd) | O(tcp_rcv_ann_wnd) | O(rx_queued) |
           O(pbuf_free) | O(tcp_sndbuf) | O(tcp_snd_wnd) | O(ovlstore_phase) |
           O(ovlstore_detail);
}
#endif

static uint32_t s_x = 0x2545F491u;
static uint32_t rnd(void)
{
    s_x ^= s_x << 13;
    s_x ^= s_x >> 17;
    s_x ^= s_x << 5;
    return s_x;
}

static void rstr(char *dst, size_t cap)
{
    static const char al[] = "abcdefXYZ0123456789._-\"\\ ";
    size_t n = rnd() % cap;          /* 0 .. cap-1 characters */
    for (size_t i = 0; i < n; i++) {
        dst[i] = al[rnd() % (sizeof(al) - 1u)];
    }
    dst[n] = '\0';
}

static void hexid(char *dst, size_t cap)
{
    snprintf(dst, cap, "0x%08lx", (unsigned long)rnd());
}

static const mps3_ctrl_op_t OPS[] = {
    MPS3_OP_PING, MPS3_OP_RESET, MPS3_OP_SET_CLK, MPS3_OP_SWAP, MPS3_OP_LINK,
    MPS3_OP_COMMIT, MPS3_OP_TELEMETRY, MPS3_OP_MACGEN, MPS3_OP_DIAG,
    MPS3_OP_DISPLAY, MPS3_OP_VERSION, MPS3_OP_DUTRX, MPS3_OP_STATS, MPS3_OP_LOG,
    MPS3_OP_TOUCH_CAL, MPS3_OP_REBOOT, MPS3_OP_UNKNOWN,
};

int main(void)
{
    static char line[MPS3_CTRL_RESP_MAX + 64];
    static mps3_ctrl_response_t r;
    for (int iter = 0; iter < 300; iter++) {
        for (unsigned k = 0; k < sizeof(OPS) / sizeof(OPS[0]); k++) {
            memset(&r, 0, sizeof(r));
            r.op = OPS[k];
            r.ok = (OPS[k] == MPS3_OP_TELEMETRY || OPS[k] == MPS3_OP_UNKNOWN) ? 0 : (int)(rnd() % 4u != 0u);
            rstr(r.err, 40);
            hexid(r.shell_id, sizeof(r.shell_id));
            hexid(r.rm_id, sizeof(r.rm_id));
            r.locked = (int)(rnd() & 1u);
            r.verified = (int)(rnd() & 1u);
            r.slot = (rnd() & 1u) ? 'A' : 'B';
            snprintf(r.owner, sizeof(r.owner), "%s", (rnd() & 1u) ? "dut" : "harness");
            r.lockup = (int)(rnd() & 1u);
            r.tx_cnt = rnd(); r.rx_cnt = rnd(); r.err_cnt = rnd();
            {
                uint32_t *w = (uint32_t *)&r.diag;
                for (unsigned i = 0; i < sizeof(r.diag) / 4u; i++) w[i] = rnd() >> (rnd() % 32u);
            }
            rstr(r.harness, sizeof(r.harness));
            hexid(r.ver32, sizeof(r.ver32));
            rstr(r.sha, sizeof(r.sha));
            r.dirty = (int)(rnd() & 1u);
            r.lmb_kb = rnd() % 2048u;
            r.features = rnd();
            switch (rnd() % 3u) {   /* usr_access: none / agree / skew */
            case 0: r.usr_access[0] = '\0'; break;
            case 1: memcpy(r.usr_access, r.ver32, sizeof(r.usr_access)); break;
            default: hexid(r.usr_access, sizeof(r.usr_access)); break;
            }
            r.dutrx_len = (uint16_t)(rnd() & 0x7FFu);
            r.dutrx_off = (uint16_t)(rnd() & 0x7FFu);
            r.dutrx_n = (uint16_t)(rnd() % (MPS3_DUTRX_CHUNK_MAX + 1u));
            r.dutrx_frames = (uint16_t)rnd();
            r.dutrx_more = (int)(rnd() & 1u);
            r.dutrx_last = (int)(rnd() & 1u);
            r.dutrx_ovf = (int)(rnd() & 1u);
            r.dutrx_desync = (int)(rnd() & 1u);
            r.dutrx_rx_frames = rnd(); r.dutrx_drop_full = rnd(); r.dutrx_drop_giant = rnd();
            for (unsigned i = 0; i < MPS3_DUTRX_CHUNK_MAX; i++) r.dutrx_data[i] = (uint8_t)rnd();
            r.log_off = rnd();
            r.log_n = (uint16_t)(rnd() % (MPS3_DUTRX_CHUNK_MAX + 1u));   /* == the log chunk max */
            r.log_more = (int)(rnd() & 1u);
            r.log_dropped = rnd();
            r.stats.up_ms = rnd(); r.stats.sid = rnd(); r.stats.rm = rnd();
            r.stats.rm_ok = (int)(rnd() & 1u); r.stats.lock = (int)(rnd() & 1u);
            r.stats.clk_sel = rnd() & 0xFFu; r.stats.mmcm = (int)(rnd() & 1u);
            r.stats.clk_alive = (int)(rnd() & 1u); r.stats.dut_rst = (int)(rnd() & 1u);
            r.stats.rp_rst = (int)(rnd() & 1u); r.stats.decpl = (int)(rnd() & 1u);
            r.stats.link = (int)(rnd() & 1u); r.stats.spd = (rnd() & 1u) ? 100u : 10u;
            r.stats.fdx = (int)(rnd() & 1u);
            for (int i = 0; i < 6; i++) r.stats.mac[i] = (uint8_t)rnd();
            rstr(r.stats.swap, sizeof(r.stats.swap));
            r.stats.swap_ok = (int)(rnd() & 1u); r.stats.swap_n = rnd(); r.stats.icap = rnd();
            r.stats.rxdrop = rnd(); r.stats.txerr = rnd();
            rstr(r.stats.swap_err, sizeof(r.stats.swap_err));
            r.stats.clr_ok = (int)(rnd() & 1u); r.stats.dut_mhz = rnd() % 200u;
            r.stats.svc_max_us = rnd(); r.stats.svc_skipped = rnd();
            r.tc_kind = (int)(rnd() & 1u);
            for (int i = 0; i < MPS3_CAL_N; i++) r.tc_cal[i] = (int32_t)rnd();
            r.tc_raw_x = rnd() & 0xFFFu; r.tc_raw_y = rnd() & 0xFFFu; r.tc_raw_z = rnd() & 0xFFu;
            r.tc_seen = rnd(); r.tc_x = rnd() % 320u; r.tc_y = rnd() % 240u;
            r.in_ms = rnd() % 10000u;

            int n = mps3_ctrl_encode_response(&r, line, (int)sizeof(line));
            if (n < 0) {
                printf("ENCODE -1 op=%d\n", (int)r.op);
            } else {
                fputs(line, stdout);
            }
        }
    }
    return 0;
}
