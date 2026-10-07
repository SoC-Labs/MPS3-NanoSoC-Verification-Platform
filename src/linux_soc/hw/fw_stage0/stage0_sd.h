/*
 * stage0_sd.h -- blocking user-uSD access for stage0, over D13's usd.c
 * (see stage0_sd.c). Adapted to struct s0_flow_ops by stage0.c.
 */
#ifndef STAGE0_SD_H
#define STAGE0_SD_H

#include <stdint.h>

/* usd_init() + poll to a verdict. Returns S0_SD_* (stage0_status.h);
 * *card_blocks is set on S0_SD_READY; *detail = (usd_state << 16) | error. */
int s0_usd_init(uint32_t *card_blocks, uint32_t *detail);

/* Read n 512-byte blocks at lba into dst (any alignment, any n). 0 / -1.
 * A failed <= 8-block op is repeated up to S0_SD_READ_RETRIES times, each
 * after a full re-initialisation of the card (stage0_sd.c recover_and_retry). */
int s0_usd_read_blocks(uint32_t lba, uint32_t n, void *dst);

/* Diagnostics for the status block (sd_rd_fails / sd_rd_last): failed read
 * ops so far this run (each retry that failed counts), and the last one as
 *   [31:24] -rc: 5 EIO (bad R1 / error token), 110 ETIMEDOUT (no data token),
 *           19 ENODEV (card gone), 250 start refused (not READY), 251 op guard
 *   [23:16] usd_state() after it   [15:0] usd_error_code() after it
 * 0 / 0 = every read worked first time. */
uint32_t s0_usd_read_fails(void);
uint32_t s0_usd_read_diag(void);

#endif /* STAGE0_SD_H */
