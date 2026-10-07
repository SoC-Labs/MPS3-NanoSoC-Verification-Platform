/*
 * stage0_rescue.c -- see stage0_rescue.h. Glue only: the UDP dispatch and the
 * poll loop body. The protocols live in stage0_net.c / stage0_tftp.c /
 * stage0_ident.c.
 */
#include "stage0_rescue.h"
#include "stage0_net.h"
#include "stage0_tftp.h"
#include "stage0_ident.h"

#define RX_FRAMES_PER_POLL 8u

static struct s0_tftp_cfg  s_tcfg;
static struct s0_ident_cfg s_icfg;
static uint8_t             s_rx[S0_FRAME_MAX];

void s0_udp_input(uint32_t src_ip, uint16_t sport, uint16_t dport,
                  const uint8_t *payload, uint32_t len)
{
    if (dport == S0_IDENT_PORT)
        s0_ident_udp(src_ip, sport, payload, len);
    else
        s0_tftp_udp(src_ip, sport, dport, payload, len);
}

void s0_rescue_start(const struct s0_rescue_cfg *cfg, struct s0_status *st)
{
    s_tcfg.stage = cfg->stage;
    s_tcfg.stage_max = cfg->stage_max;
    s_tcfg.ddr_ok = cfg->ddr_ok;
    s_tcfg.verify = cfg->verify;
    s_tcfg.verify_ctx = cfg->verify_ctx;
    s_tcfg.status = st;
    s_tcfg.status_len = st ? S0_STATUS_BYTES : 0u;
    s_tcfg.log = cfg->log;
    s_tcfg.log_ctx = cfg->log_ctx;

    s_icfg.ip = cfg->ip;
    s_icfg.shell_id = cfg->shell_id;
    for (uint32_t i = 0; i < 6u; ++i)
        s_icfg.mac[i] = cfg->mac[i];

    if (st) {
        st->phase = S0_PH_RESCUE;
        st->ip_addr = cfg->ip;
        st->mac_lo = (uint32_t)cfg->mac[0] | ((uint32_t)cfg->mac[1] << 8) |
                     ((uint32_t)cfg->mac[2] << 16) | ((uint32_t)cfg->mac[3] << 24);
        st->mac_hi = (uint32_t)cfg->mac[4] | ((uint32_t)cfg->mac[5] << 8);
    }
    s0_net_init(cfg->mac, cfg->ip, st);
    s0_tftp_init(&s_tcfg, st);
    s0_ident_init(&s_icfg, st);
    s0_net_announce();
}

int s0_rescue_poll(uint32_t now_ms, struct s0_result *out)
{
    /* Timers FIRST: they also hand the protocols `now`, so a request handled
     * below is timestamped with this pass's time, not the previous one's (a
     * stale timestamp made a fresh session look a timeout old). */
    s0_ident_poll(now_ms);
    s0_tftp_poll(now_ms);
    for (uint32_t i = 0; i < RX_FRAMES_PER_POLL; ++i) {
        int n = s0_eth_rx(s_rx, sizeof s_rx);
        if (n == 0)
            break;
        if (n > 0)
            s0_net_input(s_rx, (uint32_t)n);
    }
    return s0_tftp_ready(out);
}
