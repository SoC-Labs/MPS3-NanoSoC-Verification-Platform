/*
 * uart_over_eth.h — UART0/UART1/SWO AXI-Stream <-> TCP relay.
 * See uart_over_eth/README.md.
 */
#ifndef MPS3_UART_OVER_ETH_H
#define MPS3_UART_OVER_ETH_H

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    UART_OVER_ETH_UART0 = 0, /* boot monitor, TCP 6930 */
    UART_OVER_ETH_UART1 = 1, /* application,  TCP 6931 */
    UART_OVER_ETH_SWO   = 2, /* SWO/ITM trace, TCP 6932 */
    UART_OVER_ETH_NUM_STREAMS = 3,
} uart_over_eth_stream_t;

/* Per-stream relay state. TX/RX are named from the DUT's perspective:
 * `rx_*` = bytes the DUT sent (DUT -> shell -> TCP client); `tx_*` = bytes
 * a client sent for the DUT (TCP client -> shell -> DUT). SWO is RX-only
 * (no host->DUT trace-control byte path defined by any contract doc).
 *
 * shell-regmap.md v0.1 (I7 RESOLVED) puts all three streams in ONE UARTBR
 * block (MPS3_UARTBR_BASE) at per-stream sub-offsets, not three separate
 * 64 KiB pages as the earlier AMBIGUITY(A6) #3 placeholders assumed --
 * `strm_base` is now always MPS3_UARTBR_BASE and `data_off` carries the
 * per-stream UARTBR_U0_TXRX/UARTBR_U1_TXRX/UARTBR_SWO_RX offset. */
typedef struct {
    uint32_t strm_base;   /* MPS3_UARTBR_BASE for all three streams */
    uint32_t data_off;    /* UARTBR_U0_TXRX / UARTBR_U1_TXRX / UARTBR_SWO_RX */
    uint16_t tcp_port;    /* MPS3_PORT_UART0/1/SWO */
    bool     client_connected;
    bool     rx_only;     /* true for SWO */
} uart_over_eth_stream_state_t;

extern uart_over_eth_stream_state_t g_uart_streams[UART_OVER_ETH_NUM_STREAMS];

void uart_over_eth_init(void);

/* Non-blocking poll -- services all three streams' listen/accept/relay in
 * one bounded pass. See coordinator/README.md "Why non-blocking." */
void uart_over_eth_poll(void);

/* Per-stream relay step, exposed separately so it's testable/reasoned about
 * in isolation; uart_over_eth_poll() just calls this three times. */
void uart_over_eth_relay_step(uart_over_eth_stream_t which);

#ifdef MPS3_UART_LINUX_POLICY
/* THE LINUX CONSOLE POLICY -- compiled only into mps3-harnessd
 * (-DMPS3_UART_LINUX_POLICY); the bare-metal image is built without it and
 * relays exactly as it always has. ILA-mint findings #16/#17
 * (docs/planning/linux_lanes/FINDINGS_TRIAGE.md, README.md "Linux policy"):
 *   - NO CLIENT = DRAIN AND DROP. With nobody on a stream's port its FIFO is
 *     still popped (<= UART_OVER_ETH_DRAIN_PER_POLL a poll) and the bytes are
 *     discarded and counted, so the DUT's console never back-pressures
 *     (tready=0 held for a minute on silicon) and no backlog waits for the next
 *     client. The first drop of a no-client period and the total at the next
 *     connect are logged through uart_over_eth_note().
 *   - EVERY SWAP UNGATE EDGE = FLUSH. On the poll that first sees
 *     g_shell_state.uart_gated fall, each stream's rx_hold and DUT->host FIFO
 *     are emptied and counted, so no client -- connected across the swap or
 *     arriving later -- reads the previous RM's bytes as the new RM's.
 *   - OPTIONAL INPUT PACING. uart_over_eth_set_pace_ms(ms) (harnessd
 *     --uart-pace-ms; 0 = off, the default) spaces client->DUT bytes >= ms
 *     apart on U0/U1: a DUT whose UART RX has no FIFO and no flow control
 *     mangles line-rate input. */
uint32_t uart_over_eth_dropped(uart_over_eth_stream_t which);  /* no-client drops  */
uint32_t uart_over_eth_flushed(uart_over_eth_stream_t which);  /* ungate flushes   */
void     uart_over_eth_set_pace_ms(uint32_t ms);               /* survives _init() */
/* The platform's log sink for the policy's notes: one line, no newline. WEAK
 * no-op default in uart_over_eth.c; harnessd routes it to its console log. */
void     uart_over_eth_note(const char *line);
#endif

#ifdef __cplusplus
}
#endif

#endif /* MPS3_UART_OVER_ETH_H */
