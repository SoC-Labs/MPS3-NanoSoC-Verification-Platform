/*
 * test_uart_over_eth.c — host-gcc tests for uart_over_eth.c's REAL relay
 * bodies (W-NET-SEAM) against a behavioral UARTBR fake (mock_regs hook —
 * destructive-read FIFO pops, tx_full drop policy and SWO_CFG semantics
 * per fpga/shell/ip/uart_bridge/README.md) and fake_net_if.c clients.
 *
 * Links: uart_over_eth.c, common/net_if.c, mock_regs.c, fake_net_if.c.
 * g_shell_state is defined here (same pattern as test_swap_fsm_hw.c —
 * uart_over_eth.c only reads its uart_gated field).
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../uart_over_eth/uart_over_eth.h"
#include "../coordinator/coordinator.h"
#include "../common/platform_regs.h"
#include "../common/net_proto.h"
#include "mock_regs.h"
#include "fake_net_if.h"

mps3_shell_state_t g_shell_state;

/* Unused coordinator symbol referenced nowhere here, but coordinator.h
 * declares it; no definition needed since nothing calls it. */

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

/* ---- behavioral UARTBR fake (uart_bridge README semantics) ----------------- */

#define FQ_CAP 256

typedef struct {
    uint8_t buf[FQ_CAP];
    int rd, wr; /* wr-rd = occupancy */
} fq_t;

static fq_t s_u0_rx, s_u1_rx, s_swo_rx;   /* DUT -> host FIFOs */
static fq_t s_u0_tx, s_u1_tx;             /* host -> DUT capture */
static int  s_u0_tx_full, s_u1_tx_full;   /* forced tx_full flags */
static uint32_t s_swo_cfg;
static int  s_reg_accesses;               /* every hooked access, for gating checks */

static void fq_reset(fq_t *q) { q->rd = q->wr = 0; }
static int  fq_used(fq_t *q) { return q->wr - q->rd; }
static void fq_push(fq_t *q, uint8_t b) { assert(fq_used(q) < FQ_CAP); q->buf[q->wr++ % FQ_CAP] = b; (void)0; }
static int  fq_pop(fq_t *q, uint8_t *b) { if (fq_used(q) == 0) return 0; *b = q->buf[q->rd++ % FQ_CAP]; return 1; }

static int uartbr_hook(void *ctx, int is_write, uint32_t base, uint32_t off, uint32_t *val)
{
    (void)ctx; (void)base;
    s_reg_accesses++;
    if (!is_write) {
        uint8_t b;
        switch (off) {
        case UARTBR_U0_TXRX:
            *val = fq_pop(&s_u0_rx, &b) ? (UARTBR_VALID | b) : 0; /* destructive */
            return 1;
        case UARTBR_U1_TXRX:
            *val = fq_pop(&s_u1_rx, &b) ? (UARTBR_VALID | b) : 0;
            return 1;
        case UARTBR_SWO_RX:
            *val = fq_pop(&s_swo_rx, &b) ? (UARTBR_VALID | b) : 0;
            return 1;
        case UARTBR_FIFO_STATUS:
            *val = (s_u0_tx_full ? UARTBR_FIFO_STATUS_U0_TX_FULL : 0)
                 | (fq_used(&s_u0_rx) == 0 ? UARTBR_FIFO_STATUS_U0_RX_EMPTY : 0)
                 | (s_u1_tx_full ? UARTBR_FIFO_STATUS_U1_TX_FULL : 0)
                 | (fq_used(&s_u1_rx) == 0 ? UARTBR_FIFO_STATUS_U1_RX_EMPTY : 0)
                 | (fq_used(&s_swo_rx) == 0 ? UARTBR_FIFO_STATUS_SWO_RX_EMPTY : 0);
            return 1;
        case UARTBR_SWO_CFG:
            *val = s_swo_cfg;
            return 1;
        default:
            *val = 0;
            return 1;
        }
    }
    switch (off) {
    case UARTBR_U0_TXRX:
        /* push-while-full is DROPPED by hardware (README policy) — the
         * relay must never let this happen, asserted in the tests. */
        if (!s_u0_tx_full) fq_push(&s_u0_tx, (uint8_t)(*val & UARTBR_DATA_MASK));
        return 1;
    case UARTBR_U1_TXRX:
        if (!s_u1_tx_full) fq_push(&s_u1_tx, (uint8_t)(*val & UARTBR_DATA_MASK));
        return 1;
    case UARTBR_SWO_CFG:
        s_swo_cfg = *val & (UARTBR_SWO_CFG_DIVISOR_MASK | UARTBR_SWO_CFG_ENABLE);
        return 1;
    default:
        return 1; /* RO/unmapped: accepted-no-effect, like the RTL */
    }
}

static void fresh(void)
{
    mock_regs_reset();
    fake_net_reset();
    fq_reset(&s_u0_rx); fq_reset(&s_u1_rx); fq_reset(&s_swo_rx);
    fq_reset(&s_u0_tx); fq_reset(&s_u1_tx);
    s_u0_tx_full = s_u1_tx_full = 0;
    s_swo_cfg = 0;
    s_reg_accesses = 0;
    memset(&g_shell_state, 0, sizeof(g_shell_state));
    mock_regs_set_hook(MPS3_UARTBR_BASE, uartbr_hook, 0);
    uart_over_eth_init();
}

static void polls(int n)
{
    for (int i = 0; i < n; i++) {
        uart_over_eth_poll();
    }
}

/* ---- tests ------------------------------------------------------------------ */

static void test_init_programs_swo_cfg(void)
{
    fresh();
    CHECK((s_swo_cfg & UARTBR_SWO_CFG_ENABLE) != 0);
    CHECK((s_swo_cfg & UARTBR_SWO_CFG_DIVISOR_MASK) >= 7); /* >= 8x oversampling */
}

static void test_dut_to_client_order_preserved(void)
{
    fresh();
    int cli = fake_net_connect(MPS3_PORT_UART0);
    CHECK(cli >= 0);
    for (int i = 0; i < 10; i++) {
        fq_push(&s_u0_rx, (uint8_t)('A' + i));
    }
    polls(3);
    char buf[32];
    int n = fake_net_recv(cli, buf, sizeof(buf));
    CHECK(n == 10);
    CHECK(memcmp(buf, "ABCDEFGHIJ", 10) == 0);
    CHECK(fq_used(&s_u0_rx) == 0);
}

static void test_client_to_dut_respects_tx_full(void)
{
    fresh();
    int cli = fake_net_connect(MPS3_PORT_UART0);
    CHECK(fake_net_send(cli, "hi!", 3) == 3);
    s_u0_tx_full = 1;         /* DUT-side FIFO full: relay must HOLD, not push */
    polls(3);
    CHECK(fq_used(&s_u0_tx) == 0);
    s_u0_tx_full = 0;
    polls(3);
    CHECK(fq_used(&s_u0_tx) == 3);
    uint8_t b;
    CHECK(fq_pop(&s_u0_tx, &b) && b == 'h');
    CHECK(fq_pop(&s_u0_tx, &b) && b == 'i');
    CHECK(fq_pop(&s_u0_tx, &b) && b == '!');
}

static void test_send_backpressure_holds_popped_byte(void)
{
    /* Destructive reads: a byte popped from the bridge while the client
     * socket is full must NOT be lost. */
    fresh();
    int cli = fake_net_connect(MPS3_PORT_UART0);
    fake_net_set_send_limit(cli, 0); /* client accepts nothing */
    fq_push(&s_u0_rx, 0x55);
    polls(3);
    char buf[8];
    CHECK(fake_net_recv(cli, buf, sizeof(buf)) == 0);
    fake_net_set_send_limit(cli, -1);
    polls(1);
    CHECK(fake_net_recv(cli, buf, sizeof(buf)) == 1);
    CHECK((uint8_t)buf[0] == 0x55);
}

static void test_gated_touches_no_registers_but_keeps_client(void)
{
    fresh();
    int cli = fake_net_connect(MPS3_PORT_UART0);
    polls(1); /* accept */
    fq_push(&s_u0_rx, 0x77);
    CHECK(fake_net_send(cli, "x", 1) == 1);

    g_shell_state.uart_gated = true;
    int before = s_reg_accesses;
    polls(5);
    CHECK(s_reg_accesses == before);         /* no UARTBR access while gated */
    CHECK(g_uart_streams[UART_OVER_ETH_UART0].client_connected); /* kept */

    g_shell_state.uart_gated = false;
    polls(3);
    char buf[8];
    CHECK(fake_net_recv(cli, buf, sizeof(buf)) == 1); /* DUT byte flowed after ungate */
    CHECK((uint8_t)buf[0] == 0x77);
    uint8_t b;
    CHECK(fq_pop(&s_u0_tx, &b) && b == 'x');          /* client byte flowed too */
}

static void test_swo_rx_only_and_all_three_ports(void)
{
    fresh();
    int u1 = fake_net_connect(MPS3_PORT_UART1);
    int swo = fake_net_connect(MPS3_PORT_SWO);
    CHECK(u1 >= 0 && swo >= 0);

    fq_push(&s_u1_rx, 0x11);
    fq_push(&s_swo_rx, 0x99);
    CHECK(fake_net_send(swo, "chatter", 7) == 7); /* SWO client noise: absorbed */
    polls(3);

    char buf[8];
    CHECK(fake_net_recv(u1, buf, sizeof(buf)) == 1 && (uint8_t)buf[0] == 0x11);
    CHECK(fake_net_recv(swo, buf, sizeof(buf)) == 1 && (uint8_t)buf[0] == 0x99);
    CHECK(fq_used(&s_u1_tx) == 0); /* nothing pushed toward the DUT by SWO noise */
}

static void test_second_client_refused_first_kept(void)
{
    fresh();
    int first = fake_net_connect(MPS3_PORT_UART0);
    polls(1);
    int second = fake_net_connect(MPS3_PORT_UART0);
    polls(1);
    CHECK(fake_net_fw_closed(second));
    CHECK(!fake_net_fw_closed(first));

    /* first still works */
    fq_push(&s_u0_rx, 0x42);
    polls(2);
    char buf[4];
    CHECK(fake_net_recv(first, buf, sizeof(buf)) == 1 && (uint8_t)buf[0] == 0x42);

    /* client hangup frees the slot for a new client */
    fake_net_close(first);
    polls(2);
    CHECK(!g_uart_streams[UART_OVER_ETH_UART0].client_connected);
    int third = fake_net_connect(MPS3_PORT_UART0);
    polls(1);
    CHECK(!fake_net_fw_closed(third));
    CHECK(g_uart_streams[UART_OVER_ETH_UART0].client_connected);
}

/* REAP BEFORE REFUSE (2026-09-28, net_if.h mps3_net_peer_closed): a console
 * that closes and reopens at once used to be refused -- accept runs before this
 * pass's relay step reads the old client's EOF. Close, reconnect with NO poll
 * between, DUT output reaches the new client every time; and a client whose last
 * byte is HELD behind a full DUT TX FIFO (so the relay never reads its EOF) is
 * reaped for a newcomer too. A second LIVE client is still refused
 * (test_second_client_refused_first_kept). */
static void test_close_then_reconnect_is_adopted(void)
{
    fresh();
    char buf[4];
    int cli = fake_net_connect(MPS3_PORT_UART0);
    for (int i = 0; i < 50; i++) {
        fq_push(&s_u0_rx, (uint8_t)(0x30 + (i % 10)));
        polls(2);
        CHECK(!fake_net_fw_closed(cli));
        CHECK(fake_net_recv(cli, buf, sizeof(buf)) == 1 && (uint8_t)buf[0] == 0x30 + (i % 10));
        fake_net_close(cli);
        cli = fake_net_connect(MPS3_PORT_UART0);  /* immediately: no poll between */
        CHECK(cli >= 0);
    }
    fake_net_close(cli);
    polls(2);

    int old = fake_net_connect(MPS3_PORT_UART0);
    polls(1);
    s_u0_tx_full = 1;
    CHECK(fake_net_send(old, "k", 1) == 1);
    polls(2);                                     /* 'k' held: TX FIFO full */
    fake_net_close(old);
    polls(2);
    CHECK(g_uart_streams[UART_OVER_ETH_UART0].client_connected); /* EOF unseen */
    int next = fake_net_connect(MPS3_PORT_UART0);
    polls(1);
    CHECK(!fake_net_fw_closed(next));             /* adopted, not refused */
    CHECK(g_uart_streams[UART_OVER_ETH_UART0].client_connected);
    s_u0_tx_full = 0;
    fq_push(&s_u0_rx, 0x55);
    polls(2);
    CHECK(fake_net_recv(next, buf, sizeof(buf)) == 1 && (uint8_t)buf[0] == 0x55);
    fake_net_close(next);
    polls(2);
}

static void test_hard_send_error_drops_client(void)
{
    /* Distinct from backpressure (send returns 0, byte held): a HARD send
     * error (MPS3_NET_ERR, e.g. the peer's socket is gone) must tear the client
     * down, not spin holding a byte for a dead connection. The fake models a
     * broken pipe once the client has closed. */
    fresh();
    int cli = fake_net_connect(MPS3_PORT_UART0);
    polls(1); /* accept */
    CHECK(g_uart_streams[UART_OVER_ETH_UART0].client_connected);

    fq_push(&s_u0_rx, 0x42); /* a DUT byte waiting to go to the client */
    fake_net_close(cli);      /* client vanished -> next send hard-errors */
    polls(3);
    CHECK(!g_uart_streams[UART_OVER_ETH_UART0].client_connected); /* dropped */

    /* The slot is free again for a fresh client. */
    int cli2 = fake_net_connect(MPS3_PORT_UART0);
    polls(1);
    CHECK(!fake_net_fw_closed(cli2));
    CHECK(g_uart_streams[UART_OVER_ETH_UART0].client_connected);
}

int main(void)
{
    test_init_programs_swo_cfg();
    test_dut_to_client_order_preserved();
    test_client_to_dut_respects_tx_full();
    test_send_backpressure_holds_popped_byte();
    test_gated_touches_no_registers_but_keeps_client();
    test_swo_rx_only_and_all_three_ports();
    test_second_client_refused_first_kept();
    test_close_then_reconnect_is_adopted();
    test_hard_send_error_drops_client();

    printf("test_uart_over_eth: %d checks passed\n", s_checks);
    return 0;
}
