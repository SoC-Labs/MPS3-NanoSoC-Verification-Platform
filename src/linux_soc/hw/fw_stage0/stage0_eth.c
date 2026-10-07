/*
 * stage0_eth.c -- the rescue server's frame I/O over the bare-metal LAN9220
 * driver (firmware/smsc911x/, used UNMODIFIED: it only needs the four
 * platform_regs.h accessors and mps3_spin_until(), which stage0 provides in
 * stage0_spin.c). Portable: the host test runs it against fake_lan9220.c
 * with -DMPS3_HAL_MOCK.
 *
 * Polled, interrupts masked -- smsc911x_init() leaves INT_EN = 0, so the
 * LAN9220 IRQ pin never asserts and Linux's driver later starts from its own
 * soft reset.
 */
#include <stdint.h>

#include "smsc911x.h"
#include "stage0_net.h"
#include "stage0_rescue.h"

typedef int (*mps3_spin_pred_fn)(void *ctx);
int mps3_spin_until(mps3_spin_pred_fn pred, void *ctx, uint32_t timeout_us);

/* TX FIFO full: wait this long for the MAC to drain before dropping the frame
 * (the peer retransmits). 5 KB of FIFO at 10 Mb/s is ~4 ms. */
#define S0_ETH_TX_WAIT_US 5000u

int s0_eth_init(uintptr_t base, const uint8_t mac[6])
{
    return smsc911x_init(base, mac);
}

struct tx_ctx {
    const void *f;
    uint32_t    len;
    int         rc;
};

static int tx_settled(void *vctx)
{
    struct tx_ctx *c = (struct tx_ctx *)vctx;
    c->rc = smsc911x_tx_frame(c->f, c->len);
    return c->rc != SMSC911X_ERR_TX_SPACE;   /* done, or a hard error */
}

int s0_eth_tx(const void *frame, uint32_t len)
{
    struct tx_ctx c = { frame, len, SMSC911X_ERR_TX_SPACE };
    (void)mps3_spin_until(tx_settled, &c, S0_ETH_TX_WAIT_US);
    return c.rc == 0 ? 0 : -1;
}

int s0_eth_rx(void *buf, uint32_t cap)
{
    (void)smsc911x_tx_status_drain();        /* keep the TX status FIFO empty */
    int n = smsc911x_rx_frame(buf, cap);
    return n > 0 ? n : (n == 0 ? 0 : -1);
}
