/*
 * uart_over_eth.c — three-stream UARTBR <-> TCP byte relay, real bodies
 * (W-NET-SEAM) against the common/net_if.h seam + the UARTBR register
 * block (shell-regmap.md v0.1 / fpga/shell/ip/uart_bridge RTL).
 *
 * UARTBR semantics this file is written against (uart_bridge/README.md):
 *   - Reads of U0_RX/U1_RX/SWO_RX are DESTRUCTIVE pops: {valid=1, byte} or
 *     {valid=0, 0} + no pop when empty. Never read to "peek" — a popped
 *     byte that can't be forwarded yet must be held locally (the 1-byte
 *     holdback below).
 *   - Host->DUT writes push unconditionally and are DROPPED by hardware if
 *     the TX FIFO is full — firmware must poll FIFO_STATUS.tx_full first
 *     (drop-on-full is the RTL's documented policy for pushes that ignore
 *     this; this relay never knowingly drops: it holds bytes back and
 *     retries next poll).
 *   - SWO is RX-only; its deserialiser needs SWO_CFG {divisor, enable}
 *     programmed (register added by the RTL at 0x18, not yet in regmap
 *     v0.1 — A6 flag; see platform_regs.h).
 *   - No hardware FIFO flush across DUT resets/swaps (README flag #6):
 *     drain policy is ours — while gated we simply stop touching the
 *     registers and KEEP the TCP client; bytes buffered in the bridge
 *     survive the swap and flow once ungated.
 *     That is the BARE-METAL policy, frozen at v0.11. mps3-harnessd builds with
 *     -DMPS3_UART_LINUX_POLICY instead: drain-and-drop with no client, a flush
 *     at every swap ungate edge, optional input pacing (uart_over_eth.h; the
 *     block marked MPS3_UART_LINUX_POLICY below).
 */
#include "uart_over_eth.h"
#include "../common/net_proto.h"
#include "../common/net_if.h"
#include "../common/platform_regs.h"
#include "../coordinator/coordinator.h" /* g_shell_state.uart_gated */
#ifdef MPS3_UART_LINUX_POLICY
#include <stdarg.h>
#include <stdio.h>                 /* vsnprintf: the policy's log notes         */
#include "../common/timebase.h"    /* mps3_sys_now_ms: the optional input pacing */
#endif

/* SWO deserialiser boot config: enabled, divisor 24 -> bit period = 25
 * dut_clk cycles (a 2 MBaud-ish SWO at a 50 MHz dut_clk; >= 8x
 * oversampling honored). Pure firmware policy — no contract fixes the SWO
 * baud rate; revisit when the DUT's TPIU config is pinned down (flag for
 * A6 alongside the SWO_CFG regmap codification). */
#define UART_OVER_ETH_SWO_DIVISOR 24u

/* Per-poll relay bound per stream per direction (keeps one poll short —
 * see coordinator/README.md "Why non-blocking"). */
#define UART_OVER_ETH_BYTES_PER_POLL 64u

uart_over_eth_stream_state_t g_uart_streams[UART_OVER_ETH_NUM_STREAMS];

/* Transport-side state, kept out of the public struct (the header's shape
 * predates the seam; client_connected mirrors s_conn != NULL for
 * observers). */
typedef struct {
    mps3_net_listener_t *listener;
    mps3_net_conn_t     *conn;
    uint8_t rx_hold;        /* one popped-but-unsent DUT byte (destructive reads!) */
    int     rx_hold_valid;
    uint8_t tx_hold;        /* one received-but-unpushed client byte (TX FIFO full) */
    int     tx_hold_valid;
    uint32_t tx_full_bit;   /* this stream's FIFO_STATUS tx_full mask (0 = rx-only) */
} stream_net_t;

static stream_net_t s_net[UART_OVER_ETH_NUM_STREAMS];

#ifdef MPS3_UART_LINUX_POLICY
/* ---- the Linux console policy (uart_over_eth.h; README.md "Linux policy") ----
 * Everything in this block exists only in mps3-harnessd. The bare-metal relay
 * above and below is unchanged, and its behaviour is pinned by
 * firmware/test/test_uart_over_eth.c, which is built WITHOUT the flag. */

/* No-client drain bound per stream per poll. A pure register-read loop that
 * stops at the first empty read; 256 pops keep a chatty DUT flowing between the
 * 10 ms idle sleeps without making the pass long (the uart service's budget is
 * 10 ms; an MBV AXI-Lite read through UIO is well under 1 us). */
#define UART_OVER_ETH_DRAIN_PER_POLL 256u
/* Flush bound at an ungate edge: >> the bridge's FIFO_DEPTH (16,
 * fpga/shell/ip/uart_bridge); only a stuck VALID could reach it. */
#define UART_OVER_ETH_FLUSH_MAX      256u

static uint32_t s_dropped[UART_OVER_ETH_NUM_STREAMS];   /* drained, no client     */
static uint32_t s_drop_base[UART_OVER_ETH_NUM_STREAMS]; /* s_dropped at last note */
static bool     s_draining[UART_OVER_ETH_NUM_STREAMS];  /* a no-client period has
                                                          * dropped (noted once)   */
static uint32_t s_flushed[UART_OVER_ETH_NUM_STREAMS];   /* ungate-edge flushes    */
static bool     s_gate_seen;                            /* gated on an earlier poll */
static uint32_t s_pace_ms;                              /* 0 = no input pacing    */
static uint32_t s_last_push_ms[UART_OVER_ETH_NUM_STREAMS];
static bool     s_pushed[UART_OVER_ETH_NUM_STREAMS];

static const char *const k_stream_name[UART_OVER_ETH_NUM_STREAMS] = {
    "uart0 (6930)", "uart1 (6931)", "swo (6932)",
};

__attribute__((weak)) void uart_over_eth_note(const char *line)
{
    (void)line;
}

static void policy_note(const char *fmt, ...)
{
    char line[160];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(line, sizeof(line), fmt, ap);
    va_end(ap);
    uart_over_eth_note(line);
}

uint32_t uart_over_eth_dropped(uart_over_eth_stream_t which) { return s_dropped[which]; }
uint32_t uart_over_eth_flushed(uart_over_eth_stream_t which) { return s_flushed[which]; }
void     uart_over_eth_set_pace_ms(uint32_t ms)              { s_pace_ms = ms; }

/* Pop and discard up to `max` bytes from a stream's DUT->host FIFO; stops at the
 * first empty read (destructive reads: the only way to empty the bridge -- it has
 * no FIFO flush, uart_bridge README flag #6). */
static uint32_t policy_discard(int which, uint32_t max)
{
    const uart_over_eth_stream_state_t *s = &g_uart_streams[which];
    uint32_t n = 0u;
    while (n < max) {
        if (!(mps3_reg_read32(s->strm_base, s->data_off) & UARTBR_VALID)) {
            break;
        }
        n++;
    }
    return n;
}

/* No client on this stream (and not gated): drain and drop. */
static void policy_drain_no_client(int which)
{
    uint32_t n = policy_discard(which, UART_OVER_ETH_DRAIN_PER_POLL);
    if (n == 0u) {
        return;
    }
    s_dropped[which] += n;
    if (!s_draining[which]) {
        s_draining[which] = true;
        policy_note("%s: no client -- DUT output is drained and dropped, never "
                    "back-pressured (harnessd console policy)", k_stream_name[which]);
    }
}

/* A client was just accepted: close the no-client period's account. */
static void policy_client_connected(int which)
{
    if (s_draining[which]) {
        policy_note("%s: client connected; %u byte(s) of DUT output were dropped while "
                    "no client was connected", k_stream_name[which],
                    (unsigned)(s_dropped[which] - s_drop_base[which]));
    }
    s_draining[which] = false;
    s_drop_base[which] = s_dropped[which];
}

/* The swap ungate edge (uart_gated seen high, now low): everything still in the
 * relay or the bridge's DUT->host FIFO was produced before the swap, by the
 * previous RM -- flush it. Safe against losing the NEW RM's first bytes: the
 * swap FSM holds dut_resetn asserted from DECOUPLE_ASSERT onwards and only the
 * host's `reset dut` (after the swap has answered) releases it, and an RM's
 * console runs on dut_resetn (partition-pins.md; rm_uart_echo's banner does).
 * Sampling the edge each poll is enough: a swap spans many superloop passes
 * (at least GATE .. DONE, one FSM step each). */
static void policy_swap_edge(void)
{
    if (g_shell_state.uart_gated) {
        s_gate_seen = true;
        return;
    }
    if (!s_gate_seen) {
        return;
    }
    s_gate_seen = false;
    for (int i = 0; i < UART_OVER_ETH_NUM_STREAMS; i++) {
        uint32_t n = s_net[i].rx_hold_valid ? 1u : 0u;
        s_net[i].rx_hold_valid = 0;
        n += policy_discard(i, UART_OVER_ETH_FLUSH_MAX);
        s_flushed[i] += n;
        if (n != 0u) {
            policy_note("%s: swap ungated -- flushed %u stale byte(s) of the previous "
                        "RM's output", k_stream_name[i], (unsigned)n);
        }
    }
}

/* Input pacing: may this stream push a client byte to the DUT now? */
static bool policy_pace_ok(int which)
{
    if (s_pace_ms == 0u || !s_pushed[which]) {
        return true;
    }
    return (int32_t)(mps3_sys_now_ms() - s_last_push_ms[which]) >= (int32_t)s_pace_ms;
}

static void policy_paced(int which)
{
    s_last_push_ms[which] = mps3_sys_now_ms();
    s_pushed[which] = true;
}
#endif /* MPS3_UART_LINUX_POLICY */

void uart_over_eth_init(void)
{
    g_uart_streams[UART_OVER_ETH_UART0] = (uart_over_eth_stream_state_t){
        .strm_base = MPS3_UARTBR_BASE, .data_off = UARTBR_U0_TXRX,
        .tcp_port = MPS3_PORT_UART0, .client_connected = false, .rx_only = false,
    };
    g_uart_streams[UART_OVER_ETH_UART1] = (uart_over_eth_stream_state_t){
        .strm_base = MPS3_UARTBR_BASE, .data_off = UARTBR_U1_TXRX,
        .tcp_port = MPS3_PORT_UART1, .client_connected = false, .rx_only = false,
    };
    g_uart_streams[UART_OVER_ETH_SWO] = (uart_over_eth_stream_state_t){
        .strm_base = MPS3_UARTBR_BASE, .data_off = UARTBR_SWO_RX,
        .tcp_port = MPS3_PORT_SWO, .client_connected = false, .rx_only = true,
    };

    for (int i = 0; i < UART_OVER_ETH_NUM_STREAMS; i++) {
        s_net[i].listener = mps3_net_listen(g_uart_streams[i].tcp_port);
        s_net[i].conn = 0;
        s_net[i].rx_hold_valid = 0;
        s_net[i].tx_hold_valid = 0;
    }
    s_net[UART_OVER_ETH_UART0].tx_full_bit = UARTBR_FIFO_STATUS_U0_TX_FULL;
    s_net[UART_OVER_ETH_UART1].tx_full_bit = UARTBR_FIFO_STATUS_U1_TX_FULL;
    s_net[UART_OVER_ETH_SWO].tx_full_bit   = 0;
#ifdef MPS3_UART_LINUX_POLICY
    /* Per-start policy state (s_pace_ms is configuration: it survives). */
    for (int i = 0; i < UART_OVER_ETH_NUM_STREAMS; i++) {
        s_dropped[i] = s_drop_base[i] = s_flushed[i] = 0u;
        s_draining[i] = false;
        s_pushed[i] = false;
    }
    s_gate_seen = false;
#endif

    /* Bring the SWO deserialiser up (single 32-bit write: divisor +
     * enable together — the RTL captures the divisor on enable's rising
     * edge, so this one write is the whole handshake). */
    mps3_reg_write32(MPS3_UARTBR_BASE, UARTBR_SWO_CFG,
                     UART_OVER_ETH_SWO_DIVISOR | UARTBR_SWO_CFG_ENABLE);
}

static void stream_drop_client(int which)
{
    mps3_net_close(s_net[which].conn);
    s_net[which].conn = 0;
    g_uart_streams[which].client_connected = false;
    /* Holdbacks: the popped DUT byte is dropped with its client (nobody
     * to deliver it to); a half-pushed client byte still goes to the DUT
     * next ungated poll. */
    s_net[which].rx_hold_valid = 0;
}

void uart_over_eth_relay_step(uart_over_eth_stream_t which)
{
    uart_over_eth_stream_state_t *s = &g_uart_streams[which];
    stream_net_t *net = &s_net[which];

    if (!net->conn) {
#ifdef MPS3_UART_LINUX_POLICY
        if (!g_shell_state.uart_gated) {
            policy_drain_no_client((int)which);
        }
#endif
        return;
    }

    if (g_shell_state.uart_gated) {
        /* RP decoupled -- nothing coherent to read/write on the AXI-Stream
         * side. Per README, do NOT tear down the connection here; only
         * stop touching the FIFO registers (the bridge's own FIFOs are NOT
         * reset by a swap — uart_bridge README flag #6 — so buffered bytes
         * resume flowing once ungated). Still service the socket for a
         * client close so a hangup mid-swap is noticed. */
        uint8_t sink;
        int n = mps3_net_recv(net->conn, &sink, 0);
        if (n < 0) {
            stream_drop_client((int)which);
        }
        return;
    }

    /* DUT -> client (all three streams). Destructive reads: pop only when
     * the previous byte has been delivered. */
    for (uint32_t budget = UART_OVER_ETH_BYTES_PER_POLL; budget > 0; budget--) {
        if (!net->rx_hold_valid) {
            uint32_t rd = mps3_reg_read32(s->strm_base, s->data_off);
            if (!(rd & UARTBR_VALID)) {
                break; /* FIFO empty */
            }
            net->rx_hold = (uint8_t)(rd & UARTBR_DATA_MASK);
            net->rx_hold_valid = 1;
        }
        int n = mps3_net_send(net->conn, &net->rx_hold, 1);
        if (n == MPS3_NET_ERR) {
            stream_drop_client((int)which);
            return;
        }
        if (n == 0) {
            break; /* backpressure: keep the byte held, retry next poll */
        }
        net->rx_hold_valid = 0;
    }

    /* client -> DUT (UART0/UART1 only). Poll tx_full BEFORE each push —
     * hardware drops pushes while full (README policy); holding the byte
     * back instead makes the relay lossless end-to-end. */
    if (!s->rx_only) {
        for (uint32_t budget = UART_OVER_ETH_BYTES_PER_POLL; budget > 0; budget--) {
            if (!net->tx_hold_valid) {
                uint8_t b;
                int n = mps3_net_recv(net->conn, &b, 1);
                if (n == 0) {
                    break;
                }
                if (n < 0) {
                    stream_drop_client((int)which);
                    return;
                }
                net->tx_hold = b;
                net->tx_hold_valid = 1;
            }
#ifdef MPS3_UART_LINUX_POLICY
            if (!policy_pace_ok((int)which)) {
                break; /* paced: hold the byte until the interval has passed */
            }
#endif
            uint32_t status = mps3_reg_read32(s->strm_base, UARTBR_FIFO_STATUS);
            if (status & net->tx_full_bit) {
                break; /* DUT-side FIFO full: hold the byte, retry next poll */
            }
            mps3_reg_write32(s->strm_base, s->data_off,
                             (uint32_t)net->tx_hold & UARTBR_DATA_MASK);
            net->tx_hold_valid = 0;
#ifdef MPS3_UART_LINUX_POLICY
            if (s_pace_ms != 0u) {
                policy_paced((int)which);
                break; /* one paced byte per interval */
            }
#endif
        }
    } else {
        /* SWO clients have nothing to say; drain-and-drop anything they
         * send so a chatty client can't wedge its socket buffer, and
         * notice a close. */
        uint8_t sink[16];
        int n = mps3_net_recv(net->conn, sink, sizeof(sink));
        if (n < 0) {
            stream_drop_client((int)which);
        }
    }
}

void uart_over_eth_poll(void)
{
#ifdef MPS3_UART_LINUX_POLICY
    policy_swap_edge();   /* before any accept or relay sees the new RM's state */
#endif
    for (int i = 0; i < UART_OVER_ETH_NUM_STREAMS; i++) {
        /* Accept: one client per stream; refuse extras (matches the
         * single-client control channel policy — flag for A6 with it). Reap
         * before refuse (net_if.h mps3_net_peer_closed): accept runs before
         * this pass's relay step reads the current client's EOF, so a console
         * that closed and at once reconnected used to be refused; a current
         * client whose peer is already gone is now dropped first. */
        mps3_net_conn_t *incoming = mps3_net_accept(s_net[i].listener);
        if (incoming) {
            if (s_net[i].conn != 0 && mps3_net_peer_closed(s_net[i].conn)) {
                stream_drop_client(i);
            }
            if (s_net[i].conn == 0) {
                s_net[i].conn = incoming;
                g_uart_streams[i].client_connected = true;
                s_net[i].rx_hold_valid = 0;
                s_net[i].tx_hold_valid = 0;
#ifdef MPS3_UART_LINUX_POLICY
                policy_client_connected(i);
#endif
            } else {
                mps3_net_close(incoming);
            }
        }
        uart_over_eth_relay_step((uart_over_eth_stream_t)i);
    }
}
