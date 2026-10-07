/*
 * mock_clcd.h — behavioural mock of the CLCD 8080 byte-stream master
 * (fpga/shell/ip/clcd/clcd.sv register contract) + an HX8347-D GRAM model,
 * for host testing of the ported driver with MOCKED REGISTERS (the same
 * discipline as the firmware's MPS3_HAL_MOCK harness).
 *
 * Mock semantics mirror the RTL facts that matter to the driver:
 *   - CMD/DATA writes while fifo level >= depth are DROPPED (counted — a
 *     nonzero drop count is a test FAILURE: the driver must never trigger
 *     the block's drop policy);
 *   - STATUS composes fifo_full/fifo_empty/level;
 *   - the FIFO drains only when the test calls mock_clcd_drain() (so tests
 *     control back-pressure exactly);
 *   - every accepted {RS,byte} is recorded in order for stream assertions;
 *   - drained bytes feed the HX8347 model: index-register writes, the
 *     0x02..0x09 window regs, RAMWR auto-increment fill, RGB565 MSB-first —
 *     reconstructing a 320x240 logical framebuffer.
 */
#ifndef MOCK_CLCD_H
#define MOCK_CLCD_H

#include <stdint.h>
#include "../clcd_core.h"

#define MOCK_FIFO_DEPTH 128u
#define MOCK_STREAM_MAX (1u << 20)   /* cumulative across a whole test run */

typedef struct {
    /* register state */
    uint32_t ctrl;
    uint32_t timing;
    unsigned level;          /* fifo entries currently queued               */
    unsigned drops;          /* writes attempted while full (MUST stay 0)   */
    unsigned ctrl_writes;
    unsigned timing_writes;
    unsigned auto_drain;     /* entries consumed per STATUS read — models
                              * the panel draining CONCURRENTLY (~9 MB/s
                              * hardware ceiling). 0 = stalled panel, for
                              * the FIFO-full tests.                        */

    /* recorded accepted stream */
    uint8_t  rs[MOCK_STREAM_MAX];
    uint8_t  val[MOCK_STREAM_MAX];
    unsigned len;            /* total accepted                              */
    unsigned drained;        /* how many of len have been consumed by panel */

    /* HX8347 model */
    uint8_t  reg_index;
    int      in_ramwr;
    unsigned x0, x1, y0, y1; /* window */
    unsigned cx, cy;         /* cursor */
    int      px_phase;       /* 0 = awaiting high byte                      */
    uint8_t  px_hi;
    uint16_t fb[240][320];   /* logical frame, [y][x]                       */
    uint8_t  winreg[10];     /* raw 0x00..0x09 register bytes               */
} mock_clcd_t;

void mock_clcd_init(mock_clcd_t *m);

/* clcd_io_t backend */
uint32_t mock_clcd_read32(void *ctx, uint32_t off);
void     mock_clcd_write32(void *ctx, uint32_t off, uint32_t v);

/* Consume up to n queued fifo entries into the HX8347 model. */
void mock_clcd_drain(mock_clcd_t *m, unsigned n);

#endif /* MOCK_CLCD_H */
