// nanosoc_uart.c — polled UART2 console for the NanoSoC MicroPython port.
//
// Implements the two functions MicroPython's REPL needs:
//   mp_hal_stdin_rx_chr()   — blocking single-character read
//   mp_hal_stdout_tx_strn() — blocking string write
// No interrupts are used; the REPL simply spins on the UART STATE register.

#include "py/mpconfig.h"
#include "py/mphal.h"
#include "nanosoc.h"

void nanosoc_uart_init(void) {
    // Disable while reprogramming the divider.
    UART2_CTRL = 0;
    UART2_BAUDDIV = NANOSOC_UART2_BAUDDIV;   // 50 MHz / 76800 = 651

    // Route UART2 TXD onto P1[5]. RXD (P1[4]) needs no altfunc — the pin mux
    // taps it straight off the pad input — but it MUST stay an input, so we
    // deliberately do not touch GPIO1 OUTENSET.
    GPIO1_ALTFUNCSET = GPIO1_ALTFUNC_UART2_TXD;

    // Enable TX and RX. (The stock CMSDK UartStdOutInit() sets CTRL=0x01, i.e.
    // TX only — that is fine for a one-way banner but a REPL needs RX too.)
    UART2_CTRL = UART_CTRL_TX_EN | UART_CTRL_RX_EN;
}

// Receive a single character (blocking).
int mp_hal_stdin_rx_chr(void) {
    while ((UART2_STATE & UART_STATE_RX_FULL) == 0) {
        // spin until a byte has arrived
    }
    return (int)(UART2_DATA & 0xff);
}

// Send a string of a given length (blocking).
mp_uint_t mp_hal_stdout_tx_strn(const char *str, mp_uint_t len) {
    for (mp_uint_t i = 0; i < len; ++i) {
        while (UART2_STATE & UART_STATE_TX_FULL) {
            // spin while the transmit holding register is full
        }
        UART2_DATA = (uint32_t)(unsigned char)str[i];
    }
    return len;
}
