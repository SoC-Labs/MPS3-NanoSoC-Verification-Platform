/*
 * SPDX-License-Identifier: Apache-2.0
 * Copyright (C) 2026, SoC Labs (www.soclabs.org)
 *
 * main.c -- nanosoc_hello: print a message and a counter on the console.
 *
 * Runs on the `nanosoc` design. It writes to the console UART directly,
 * through its registers, so it needs no C library.
 */

#include <stdint.h>

/* ---- Change me ---------------------------------------------------------- */
#define MESSAGE "Hello from my nanoSoC program"

/* ---- Hardware facts for the `nanosoc` design ----------------------------- */
#define DUT_CLK_HZ   50000000u   /* the harness runs the design at 50 MHz   */
#define CONSOLE_BAUD 76800u      /* the rate the harness console listens at */

/* The console UART (nanoSoC UART2), 0x4000_6000. */
#define UART2_BASE   0x40006000u
#define UART_DATA    (*(volatile uint32_t *)(UART2_BASE + 0x00)) /* byte to send     */
#define UART_STATE   (*(volatile uint32_t *)(UART2_BASE + 0x04)) /* bit 0: TX full   */
#define UART_CTRL    (*(volatile uint32_t *)(UART2_BASE + 0x08)) /* bit 0: TX enable */
#define UART_BAUDDIV (*(volatile uint32_t *)(UART2_BASE + 0x10)) /* clocks per bit   */

#define UART_STATE_TXFULL 0x1u
#define UART_CTRL_TXEN    0x1u

/* UART2's transmit line leaves the SoC on GPIO port 1, pin 5, as an
 * alternate function. GPIO1 is at 0x4001_1000; ALTFUNCSET is at +0x18. */
#define GPIO1_ALTFUNCSET (*(volatile uint32_t *)(0x40011000u + 0x18))
#define UART2_TX_PIN     (1u << 5)

/* ---- Console output ------------------------------------------------------ */
static void console_init(void)
{
    UART_CTRL    = 0;                          /* stop it while changing rate */
    UART_BAUDDIV = DUT_CLK_HZ / CONSOLE_BAUD;  /* 50 MHz / 76,800 = 651       */
    UART_CTRL    = UART_CTRL_TXEN;
    GPIO1_ALTFUNCSET = UART2_TX_PIN;           /* connect UART2 TX to the pin */
}

/* Busy-wait. One pass of the loop takes a fraction of a microsecond at
 * 50 MHz; the exact time depends on the compiler. */
static void wait_loops(uint32_t n)
{
    for (volatile uint32_t i = 0; i < n; i++)
        ;
}

#ifndef CHAR_GAP_LOOPS
#define CHAR_GAP_LOOPS 8000u      /* about 2 ms */
#endif
#ifndef LINE_GAP_LOOPS
#define LINE_GAP_LOOPS 2500000u   /* about 1 s  */
#endif

/* The harness reads the console every few milliseconds and holds only
 * 16 bytes in between, so send at most one character every 2 ms or so. */
static void put_char(char c)
{
    while (UART_STATE & UART_STATE_TXFULL)
        ;                                      /* wait for room               */
    UART_DATA = (uint32_t)(uint8_t)c;
    wait_loops(CHAR_GAP_LOOPS);
}

static void put_string(const char *s)
{
    while (*s)
        put_char(*s++);
}

/* Print n in decimal. The Cortex-M0 has no divide instruction, so take
 * away powers of ten instead of dividing: no library code is needed. */
static void put_uint(uint32_t n)
{
    static const uint32_t pow10[] = {
        1000000000u, 100000000u, 10000000u, 1000000u, 100000u,
        10000u, 1000u, 100u, 10u, 1u
    };
    int started = 0;
    for (unsigned i = 0; i < sizeof pow10 / sizeof pow10[0]; i++) {
        char digit = '0';
        while (n >= pow10[i]) {
            n -= pow10[i];
            digit++;
        }
        if (digit != '0' || started || pow10[i] == 1u) {
            put_char(digit);
            started = 1;
        }
    }
}

/* ---- The program --------------------------------------------------------- */
uint32_t hello_count;    /* global, so you can `print hello_count` in GDB */

int main(void)
{
    console_init();
    for (;;) {
        hello_count++;
        put_string(MESSAGE " ");
        put_uint(hello_count);
        put_string("\r\n");
        wait_loops(LINE_GAP_LOOPS);
    }
}
