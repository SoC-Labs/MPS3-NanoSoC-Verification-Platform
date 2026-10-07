/*
 * test_uart_over_eth_linux.c — the console relay's LINUX POLICY
 * (uart_over_eth.c built -DMPS3_UART_LINUX_POLICY, as mps3-harnessd builds it):
 * ILA-mint findings #16 (drain-and-drop with no client; flush on every swap
 * ungate edge) and #17 (optional host->DUT pacing).
 *
 * ONE source, TWO binaries (firmware/test/Makefile):
 *   test_uart_over_eth_linux       -DMPS3_UART_LINUX_POLICY: the policy holds.
 *   test_uart_over_eth_baremetal   no flag = the frozen v0.11 relay. The SAME
 *                                  scenarios assert the two silicon failures the
 *                                  policy exists for (the DUT back-pressured with
 *                                  no client; a stale backlog delivered after a
 *                                  swap) -- the negative control: these tests can
 *                                  tell the two relays apart, and the bare-metal
 *                                  one is unchanged.
 * test_uart_over_eth.c (bare metal, untouched) still pins everything else.
 *
 * Links: uart_over_eth.c, common/net_if.c, mock_regs.c, fake_net_if.c.
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

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

/* ---- behavioural UARTBR (uart_bridge README: destructive pops, tx_full) ---- */
#define FQ_CAP 1024
typedef struct { uint8_t buf[FQ_CAP]; int rd, wr; } fq_t;
static fq_t s_rx[3];                 /* DUT -> host: U0, U1, SWO */
static fq_t s_tx[2];                 /* host -> DUT capture: U0, U1 */
static int  s_reg_accesses;

static void fq_reset(fq_t *q) { q->rd = q->wr = 0; }
static int  fq_used(const fq_t *q) { return q->wr - q->rd; }
static void fq_push(fq_t *q, uint8_t b) { assert(fq_used(q) < FQ_CAP); q->buf[q->wr++ % FQ_CAP] = b; }
static int  fq_pop(fq_t *q, uint8_t *b) { if (!fq_used(q)) return 0; *b = q->buf[q->rd++ % FQ_CAP]; return 1; }
static void fq_puts(fq_t *q, const char *s) { while (*s) fq_push(q, (uint8_t)*s++); }

static int rx_index(uint32_t off)
{
    return off == UARTBR_U0_TXRX ? 0 : off == UARTBR_U1_TXRX ? 1 : off == UARTBR_SWO_RX ? 2 : -1;
}

static int uartbr_hook(void *ctx, int is_write, uint32_t base, uint32_t off, uint32_t *val)
{
    (void)ctx; (void)base;
    s_reg_accesses++;
    int i = rx_index(off);
    if (!is_write) {
        uint8_t b;
        if (i >= 0) {
            *val = fq_pop(&s_rx[i], &b) ? (UARTBR_VALID | b) : 0u;
        } else {
            *val = 0u;   /* FIFO_STATUS: never tx_full here; SWO_CFG unread */
        }
        return 1;
    }
    if (i == 0 || i == 1) {
        fq_push(&s_tx[i], (uint8_t)(*val & UARTBR_DATA_MASK));
    }
    return 1;
}

/* ---- the policy's log sink (strong: overrides the weak no-op) ------------- */
static char s_notes[4096];
static int  s_note_count;
#ifdef MPS3_UART_LINUX_POLICY
void uart_over_eth_note(const char *line)
{
    s_note_count++;
    strncat(s_notes, line, sizeof(s_notes) - strlen(s_notes) - 2u);
    strcat(s_notes, "\n");
}
#endif

static void fresh(void)
{
    mock_regs_reset();
    fake_net_reset();
    for (int i = 0; i < 3; i++) fq_reset(&s_rx[i]);
    for (int i = 0; i < 2; i++) fq_reset(&s_tx[i]);
    s_reg_accesses = 0;
    s_notes[0] = '\0';
    s_note_count = 0;
    memset(&g_shell_state, 0, sizeof(g_shell_state));
    mock_regs_set_hook(MPS3_UARTBR_BASE, uartbr_hook, 0);
    uart_over_eth_init();   /* NOT the pace: that is configuration and survives */
}

static void polls(int n) { for (int i = 0; i < n; i++) uart_over_eth_poll(); }

static int recv_all(int cli, char *buf, int cap)
{
    int got = 0, n;
    while (got < cap && (n = fake_net_recv(cli, buf + got, cap - got)) > 0) got += n;
    return got;
}

/* ---- #16a: no client ------------------------------------------------------ */
static void test_no_client_output(void)
{
    fresh();
    fq_puts(&s_rx[0], "boot banner nobody is reading\r\n");   /* 31 bytes */
    fq_puts(&s_rx[2], "itm");
    int n0 = fq_used(&s_rx[0]);
    polls(3);
#ifdef MPS3_UART_LINUX_POLICY
    CHECK(fq_used(&s_rx[0]) == 0 && fq_used(&s_rx[2]) == 0);   /* drained: tready never stalls */
    CHECK(uart_over_eth_dropped(UART_OVER_ETH_UART0) == (uint32_t)n0);
    CHECK(uart_over_eth_dropped(UART_OVER_ETH_SWO) == 3u);
    CHECK(strstr(s_notes, "uart0 (6930): no client") != NULL);
    int notes = s_note_count;
    fq_puts(&s_rx[0], "more");
    polls(2);
    CHECK(s_note_count == notes);                              /* noted once a period */
    CHECK(uart_over_eth_dropped(UART_OVER_ETH_UART0) == (uint32_t)n0 + 4u);

    /* A client that connects now reads the DUT's NEW output, not a backlog. */
    int cli = fake_net_connect(MPS3_PORT_UART0);
    polls(1);
    CHECK(strstr(s_notes, "35 byte(s) of DUT output were dropped") != NULL);
    fq_puts(&s_rx[0], "fresh");
    polls(2);
    char buf[64];
    int got = recv_all(cli, buf, sizeof(buf));
    CHECK(got == 5 && memcmp(buf, "fresh", 5) == 0);
    /* ...and after it leaves, the next no-client period is noted again. */
    fake_net_close(cli);
    polls(2);
    CHECK(!g_uart_streams[UART_OVER_ETH_UART0].client_connected);
    notes = s_note_count;
    size_t tail = strlen(s_notes);
    fq_puts(&s_rx[0], "x");
    polls(1);
    CHECK(s_note_count == notes + 1);
    CHECK(strstr(s_notes + tail, "uart0 (6930): no client -- DUT output is drained") != NULL);
#else
    /* THE SILICON FAILURE (#16): nobody pops, the FIFO stays full (tready=0 on
     * the DUT side) and the whole backlog waits for the next client. */
    CHECK(fq_used(&s_rx[0]) == n0 && fq_used(&s_rx[2]) == 3);
    int cli = fake_net_connect(MPS3_PORT_UART0);
    polls(3);
    char buf[64];
    CHECK(recv_all(cli, buf, sizeof(buf)) == n0);              /* the stale backlog */
#endif
}

/* ---- #16b: a swap, with a client connected across it ---------------------- */
static void test_swap_flushes_the_previous_rm(void)
{
    fresh();
    int cli = fake_net_connect(MPS3_PORT_UART0);
    polls(1);
    /* The old RM's last bytes: one popped but unsendable (held in rx_hold, the
     * client's window is shut), the rest still in the bridge FIFO at the gate. */
    fake_net_set_send_limit(cli, 0);
    fq_puts(&s_rx[0], "rm_uart_echo rea");                     /* 16 = the silicon capture */
    polls(1);
    g_shell_state.uart_gated = true;                           /* SWAP_GATE */
    int before = s_reg_accesses;
    polls(4);
    CHECK(s_reg_accesses == before);                           /* gated: no UARTBR access */
    CHECK(g_uart_streams[UART_OVER_ETH_UART0].client_connected);
    g_shell_state.uart_gated = false;                          /* DONE arc: ungate */
    fake_net_set_send_limit(cli, -1);
    polls(2);
    fq_puts(&s_rx[0], "nanosoc boot\r\n");                     /* the NEW RM, after reset dut */
    polls(2);
    char buf[128];
    int got = recv_all(cli, buf, sizeof(buf));
    buf[got] = '\0';
#ifdef MPS3_UART_LINUX_POLICY
    CHECK(strcmp(buf, "nanosoc boot\r\n") == 0);               /* nothing of the old RM */
    CHECK(uart_over_eth_flushed(UART_OVER_ETH_UART0) == 16u);  /* 1 held + 15 in the FIFO */
    CHECK(strstr(s_notes, "flushed 16 stale byte(s)") != NULL);
    /* One flush per edge: a later ungated poll flushes nothing more. */
    polls(3);
    CHECK(uart_over_eth_flushed(UART_OVER_ETH_UART0) == 16u);
#else
    CHECK(strcmp(buf, "rm_uart_echo reananosoc boot\r\n") == 0);  /* THE SILICON FAILURE */
#endif
}

/* ---- #16c: a swap with no client, then a client ---------------------------- */
static void test_swap_with_no_client(void)
{
    fresh();
    g_shell_state.uart_gated = true;
    fq_puts(&s_rx[1], "late old-RM bytes");                    /* entered between gate and decouple */
    int before = s_reg_accesses;
    polls(3);
    CHECK(s_reg_accesses == before);                           /* never drains while gated */
    CHECK(fq_used(&s_rx[1]) == 17);
    g_shell_state.uart_gated = false;
    polls(1);
    int cli = fake_net_connect(MPS3_PORT_UART1);
    polls(1);
    fq_puts(&s_rx[1], "new");
    polls(2);
    char buf[64];
    int got = recv_all(cli, buf, sizeof(buf));
#ifdef MPS3_UART_LINUX_POLICY
    CHECK(got == 3 && memcmp(buf, "new", 3) == 0);
    CHECK(uart_over_eth_flushed(UART_OVER_ETH_UART1) == 17u);  /* the edge got them first */
    CHECK(uart_over_eth_dropped(UART_OVER_ETH_UART1) == 0u);
#else
    CHECK(got == 20);                                           /* 17 stale + 3 */
#endif
}

/* ---- #17: optional input pacing (policy only) ----------------------------- */
#ifdef MPS3_UART_LINUX_POLICY
static void test_input_pacing(void)
{
    /* Off (the default): a line goes out in one poll, as on bare metal. */
    uart_over_eth_set_pace_ms(0);
    fresh();
    int cli = fake_net_connect(MPS3_PORT_UART0);
    polls(1);
    CHECK(fake_net_send(cli, "print(1+1)\r", 11) == 11);
    polls(1);
    CHECK(fq_used(&s_tx[0]) == 11);

    /* 20 ms: one byte per interval, none early, none lost, order kept. */
    uart_over_eth_set_pace_ms(20);
    fresh();
    mock_time_set_ms(1000u);
    cli = fake_net_connect(MPS3_PORT_UART0);
    polls(1);
    CHECK(fake_net_send(cli, "abc", 3) == 3);
    polls(3);
    CHECK(fq_used(&s_tx[0]) == 1);
    mock_time_advance_ms(19u);
    polls(3);
    CHECK(fq_used(&s_tx[0]) == 1);
    mock_time_advance_ms(1u);
    polls(3);
    CHECK(fq_used(&s_tx[0]) == 2);
    mock_time_advance_ms(20u);
    polls(1);
    CHECK(fq_used(&s_tx[0]) == 3);
    uint8_t b;
    CHECK(fq_pop(&s_tx[0], &b) && b == 'a');
    CHECK(fq_pop(&s_tx[0], &b) && b == 'b');
    CHECK(fq_pop(&s_tx[0], &b) && b == 'c');
    /* The pace is configuration: a module re-init (fresh() runs
     * uart_over_eth_init() again) keeps it. */
    fresh();
    cli = fake_net_connect(MPS3_PORT_UART1);
    polls(1);
    CHECK(fake_net_send(cli, "xy", 2) == 2);
    polls(3);
    CHECK(fq_used(&s_tx[1]) == 1);
    uart_over_eth_set_pace_ms(0);
}
#endif

int main(void)
{
    test_no_client_output();
    test_swap_flushes_the_previous_rm();
    test_swap_with_no_client();
#ifdef MPS3_UART_LINUX_POLICY
    test_input_pacing();
    printf("test_uart_over_eth_linux: %d checks passed (MPS3_UART_LINUX_POLICY)\n", s_checks);
#else
    printf("test_uart_over_eth_baremetal: %d checks passed (the frozen v0.11 relay: "
           "both silicon failures reproduced)\n", s_checks);
#endif
    return 0;
}
