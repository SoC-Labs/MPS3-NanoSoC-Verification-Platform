/*
 * socscope_exp.c -- DUT-side stimulus for MPS3 bring-up B2.
 *
 * B1 proved the trace transport with SYNTHETIC traffic generated inside the RM.
 * B2 points the probe at a REAL nanoSoC bus -- nanosoc's `exp_*` expansion AHB
 * master at 0x6000_0000 -- and this is the firmware that gives it something to
 * watch. The transfers are the CPU's own, issued by the M0 over the real
 * interconnect, which is the whole difference from B1.
 *
 * WHY THIS PATTERN. Every field the host checks is a function of the loop index
 * alone, so the expectation can be recomputed rather than recovered from the
 * capture -- an oracle that derives its expectation from the thing under test
 * agrees with it by construction.
 *
 *     addr   alternates EXP_BASE+CMD (0x04) and EXP_BASE+DATA (0x08)
 *     data   0x5C05_0000 | n     with n stepping 0..255 and wrapping
 *
 * ahb_clcd is a legal target for this: its hreadyout is CONSTANT 1 and its hresp
 * is constant OKAY (ahb_clcd.sv, "This block never, under any circumstance,
 * stalls"), so these writes cannot wedge the bus however fast they arrive. Writes
 * into a full FIFO are DROPPED rather than stalled -- which does not matter here,
 * because the probe watches the BUS, not the slave. A dropped write is still a
 * transfer that happened.
 *
 * PACING IS DELIBERATE AND IT IS NOT ARBITRARY. FACTS HW-007: the MPS3's SWO
 * receiver holds 16 bytes and one socscope frame is exactly 16, so the trace link
 * carries roughly one record per 25 ms. Generating transfers faster than that does
 * not produce a richer capture -- it overflows the ring and produces GAP records
 * instead. So the loop paces itself to stay under the link, and the trace plane's
 * capacity becomes a property of the stimulus rather than a surprise in the data.
 *
 * The console output is deliberately SPARSE and deterministic: B2's control is
 * that the DUT behaves identically with the probe enabled and disabled, and that
 * is checked by diffing this console between the two runs.
 */

#include "CMSDK_CM0.h"
#include "uart_stdout.h"

/* NO <stdio.h>. printf drags newlib's stdio (and _fstat/_isatty/_lseek/_read)
 * into an image that has to fit a 16 KB IMEM, and the console here needs to emit
 * a banner and a counter -- not formatted output. UartPutc is the whole API. */

#define EXP_BASE            0x60000000u
#define AHB_CLCD_CMD        0x04u
#define AHB_CLCD_DATA       0x08u

#define REG32(a)            (*(volatile unsigned int *)(a))

/* Records per console line. Keeps the console quiet enough to diff by eye while
 * still proving the loop is running. */
#define REPORT_EVERY        64u

/* Roughly 25 ms at 50 MHz. A plain loop rather than a timer: this must not depend
 * on peripherals the probe might also be watching, and the exact period does not
 * matter -- only that it stays comfortably under the trace link's capacity. */
#define DELAY_ITERS         220000u

/* newlib's exit() wants this even though main() never returns. Spinning rather
 * than resetting: if this is ever reached the DUT should stop where it is, not
 * quietly restart and look like it is working. */
void _exit(int code)
{
    (void)code;
    for (;;) {
    }
}

static void uart_puts(const char *s)
{
    while (*s) {
        UartPutc((unsigned char)*s++);
    }
}

/* Fixed-width hex, so every console line is the SAME LENGTH. B2's control is a
 * diff of this console between probe-enabled and probe-disabled runs, and a
 * variable-width decimal makes that diff noisier than the thing it is testing. */
static void uart_puthex(unsigned int v)
{
    static const char hexd[] = "0123456789ABCDEF";
    int i;
    for (i = 28; i >= 0; i -= 4) {
        UartPutc((unsigned char)hexd[(v >> i) & 0xFu]);
    }
}

static void pace(void)
{
    volatile unsigned int i;
    for (i = 0; i < DELAY_ITERS; i++) {
        __asm volatile ("nop");
    }
}

int main(void)
{
    unsigned int n     = 0u;
    unsigned int count = 0u;

    UartStdOutInit();
    uart_puts("socscope_exp: driving exp_* at 0x60000000\n");

    /* CTRL is deliberately NOT written. Enabling the display would start the 8080
     * state machine and make the FIFO level -- and so the bus traffic -- depend on
     * panel timing. B2 wants transfers whose ADDRESSES and DATA are predictable;
     * whether the panel draws anything is irrelevant to the probe. */

    for (;;) {
        REG32(EXP_BASE + AHB_CLCD_CMD)  = 0x5C050000u | (n & 0xFFu);
        pace();
        REG32(EXP_BASE + AHB_CLCD_DATA) = 0x5C050000u | (n & 0xFFu);
        pace();

        n++;
        if (++count >= REPORT_EVERY) {
            count = 0u;
            uart_puts("exp ");
            uart_puthex(n);
            uart_puts("\n");
        }
    }
}
