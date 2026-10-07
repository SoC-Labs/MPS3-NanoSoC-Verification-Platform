/*
 * stage0_rescue.h -- stage0's RESCUE MODE: static 192.168.10.101 with the
 * harness MAC, answering exactly three things (plan §10a S6):
 *   - ARP + ICMP echo           ("pingable": stage0 is alive)
 *   - TFTP on UDP 69            (stage0_tftp.h: push a boot image, read status)
 *   - identify on UDP 6899      (stage0_ident.h: mode "rescue" + reason)
 * and nothing on 6900. Portable: frames come from s0_eth_rx() and go out via
 * s0_eth_tx() (stage0_eth.c over smsc911x on the target; queues in test/).
 */
#ifndef STAGE0_RESCUE_H
#define STAGE0_RESCUE_H

#include <stdint.h>
#include "stage0_boot.h"
#include "stage0_status.h"

struct s0_rescue_cfg {
    uint8_t   mac[6];
    uint32_t  ip;              /* host order */
    uint32_t  shell_id;        /* fabric static_id (identify) */
    int       ddr_ok;          /* 0: TFTP pushes refused */
    uint8_t  *stage;           /* staging window for a pushed image */
    uint32_t  stage_max;
    int     (*verify)(const uint8_t *img, uint32_t len, struct s0_result *out, void *ctx);
    void     *verify_ctx;
    void    (*log)(void *ctx, const char *line);
    void     *log_ctx;
};

/* Bring the protocol side up (the LAN9220 itself is the caller's job) and
 * announce the address once (gratuitous ARP). st must stay valid. */
void s0_rescue_start(const struct s0_rescue_cfg *cfg, struct s0_status *st);

/* One pass: drain up to a few received frames, run the timers. Returns 1 when
 * a pushed image has been verified and acknowledged (*out = its hand-off). */
int s0_rescue_poll(uint32_t now_ms, struct s0_result *out);

/* Platform seam: one received frame into buf. >0 = its length (FCS may be
 * included), 0 = none pending, <0 = a frame was dropped. */
int s0_eth_rx(void *buf, uint32_t cap);

#endif /* STAGE0_RESCUE_H */
