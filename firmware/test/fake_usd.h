/*
 * fake_usd.h -- register-level fake of the shell's `usd_spi` block
 * (firmware/usd/usd_regs.h) with an SD card in SPI mode behind it, installed as
 * a mock_regs hook on MPS3_USD_BASE so firmware/usd/usd.c runs unmodified on
 * the host (the same pattern as fake_lan9220.c).
 *
 * THE BLOCK. ID / CTRL / CLKDIV / DATA / STATUS as the lane contract states
 * them, and as L1's fpga/shell/ip/usd_spi/usd_spi.sv (2026-09-23) resolves the
 * points the contract leaves open: pads driven only when EN && (CD_PRESENT ||
 * CD_IGNORE), and CD_IGNORE also forces CD_PRESENT; a DATA write while BUSY is
 * ignored and sets OVR; a DATA write with the pads gated does not shift and
 * sets ABORT; ABORT also when the pads are disabled mid-transfer; CD_PRESENT
 * debounced (settable, default 0 ms) with polarity, CD_RAW the pin without it;
 * CD_CHANGED / OVR / ABORT sticky W1C. A shift completes at the DATA write but
 * STATUS reports BUSY for the next `busy_reads` STATUS reads (default 1), so the
 * driver's spin is exercised; -1 = stuck BUSY.
 *
 * THE CARD. A byte-level SPI-mode model: CMD0/8/9/12/17/18/24/25/55/58 and
 * ACMD41, R1/R3/R7, NCR delay, read Nac + 0xFE token + data + CRC, CMD18
 * streaming until CMD12 (with a NON-0xFF stuff byte, so a driver that forgets
 * to discard it reads a bad R1), write tokens 0xFE/0xFC/0xFD, data response
 * 0xE5, write busy (MISO low) counted in clocked bytes. CRC7 is checked on
 * CMD0 and CMD8 (a bad one answers R1 with the COM_CRC bit). A card that is
 * busy ignores MOSI; a non-0xFF byte sent into a busy card is counted as a
 * violation. Removal is a power loss: the protocol state resets; the block
 * store (the card's flash) survives.
 *
 * THE STORE. Sparse: FAKE_USD_STORE_SLOTS written blocks; an unwritten block
 * reads fake_usd_pattern_byte(lba, i), so reads can be checked for integrity
 * without storing the card.
 *
 * WIRE TIME. Every shifted byte costs 8 * 2 * (CLKDIV + 1) aclk periods at
 * 100 MHz (10 ns), which is how the per-poll budget test measures time.
 */
#ifndef MPS3_FAKE_USD_H
#define MPS3_FAKE_USD_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#ifndef FAKE_USD_STORE_SLOTS
#define FAKE_USD_STORE_SLOTS   64u   /* -D for a test that writes more (test_usd_boot) */
#endif
#define FAKE_USD_C_SIZE_DEFAULT 15159u   /* (15159+1) * 512 KiB = 7580 MiB, an "8 GB" card */

/* Reset block + card to defaults (slot EMPTY, SDHC v2 card configured, 0 ms
 * debounce, ACMD41 ready at once, 1 BUSY read per shift, store empty) and
 * install the hook. Call AFTER mock_regs_reset(), which drops hooks. */
void fake_usd_reset(void);

/* ---- card detect ---------------------------------------------------------- */
void fake_usd_insert(void);                    /* card goes into the slot now */
void fake_usd_remove(void);                    /* card comes out now */
/* Remove the card when `n` more bytes have been shifted (counted from this call),
 * i.e. in the middle of a transfer. 0 = disarm. */
void fake_usd_remove_after_bytes(uint32_t n);
void fake_usd_set_debounce_ms(uint32_t ms);    /* CD_PRESENT follows the pin after this */
/* The RTL's debouncer runs whether or not firmware reads the page; the fake's
 * runs on each register access, on insert/remove, and here. Call it after
 * advancing mock time to let the block see time pass with no poll. */
void fake_usd_tick(void);
/* An FPGA RECONFIGURATION, as the slot sees it: the usd_spi block comes back at
 * its power-on values (and its mock_regs hook is re-installed -- call AFTER
 * mock_regs_reset(), which drops it), a card that is in loses power (protocol
 * state reset) but stays in the slot, and the card's STORE survives. The
 * personality settings survive too. */
void fake_usd_power_cycle(void);

/* ---- card personality (set before insert) --------------------------------- */
void fake_usd_set_sdsc(int on);                /* CMD58 CCS=0 */
void fake_usd_set_v1(int on);                  /* CMD8 answers illegal-command */
void fake_usd_set_acmd41_busy(uint32_t n);     /* ACMD41 answers idle this many times */
void fake_usd_set_write_busy(uint32_t nbytes); /* MISO-low bytes after each data response */
void fake_usd_set_read_nac(uint32_t nbytes);   /* 0xFF bytes before each read token (default 2) */
void fake_usd_set_ncr(uint32_t nbytes);        /* 0xFF bytes before each R1 (default 1, max 8) */
void fake_usd_set_unresponsive(int on);        /* card never answers a command */
void fake_usd_set_c_size(uint32_t c_size);     /* CSD v2 C_SIZE */
/* Answer the n-th data block written from now (1-based) with a write-error
 * data response (0xED) and do not store it. 0 = disarm. */
void fake_usd_reject_write_block(uint32_t n);

/* ---- block personality ---------------------------------------------------- */
void fake_usd_set_id(uint32_t id);             /* ID register value (default "USD1") */
void fake_usd_set_busy_reads(int n);           /* STATUS reads per shift showing BUSY; -1 stuck */
/* Board step B0's unknowns: a board whose USD_NCD reads HIGH with a card in
 * (set before usd_init), and a CTRL bit set over JTAG behind the driver's back. */
void fake_usd_set_pin_inverted(int on);
void fake_usd_jtag_ctrl_set(uint32_t bits);

/* ---- observation ---------------------------------------------------------- */
uint32_t fake_usd_page_writes(void);           /* ANY register write to the page */
uint32_t fake_usd_data_writes(void);           /* DATA writes (= shifts started, incl. ignored) */
uint32_t fake_usd_en_writes(void);             /* CTRL writes with EN=1 */
int      fake_usd_pads_ever_driven(void);      /* EN && (CD_PRESENT || CD_IGNORE) was ever true */
uint32_t fake_usd_ctrl(void);                  /* CTRL now */
uint32_t fake_usd_clkdiv(void);                /* CLKDIV now */
uint32_t fake_usd_status_reads(void);
uint32_t fake_usd_ovr_count(void);             /* DATA writes that landed while BUSY */
uint32_t fake_usd_crc_errors(void);            /* CMD0/CMD8 frames with a bad CRC7 */
uint32_t fake_usd_crc_bad_any(void);           /* frames of ANY command with a bad CRC7 */
uint32_t fake_usd_frame_errors(void);          /* command frames without the end bit */
uint32_t fake_usd_busy_violations(void);       /* non-0xFF bytes sent into a busy card */
uint32_t fake_usd_protocol_errors(void);       /* anything else the card model rejected */
uint32_t fake_usd_cmd_count(unsigned idx);     /* CMDidx received (ACMD41 counted as 41) */
uint32_t fake_usd_cmd_clkdiv(unsigned idx);    /* CLKDIV when CMDidx was last received */
const uint8_t *fake_usd_cmd_frame(unsigned idx); /* last 6-byte frame of CMDidx */
uint32_t fake_usd_clocks_before_cmd0(void);    /* CS-high clocks since power-on before first CMD0 */
uint32_t fake_usd_stop_tokens(void);           /* 0xFD stop tokens received */
uint32_t fake_usd_blocks_written(void);        /* data blocks committed to the store */
uint32_t fake_usd_store_overflows(void);       /* writes dropped: store full */

/* Per-poll accounting: call fake_usd_poll_begin() before usd_poll(), read after. */
void     fake_usd_poll_begin(void);
uint32_t fake_usd_poll_bytes(void);            /* bytes shifted (a 32-bit shift = 4) */
uint32_t fake_usd_poll_bytes_slow(void);       /* ... of which at CLKDIV >= 124 */
uint32_t fake_usd_poll_shifts(void);           /* DATA writes */
uint64_t fake_usd_poll_wire_ns(void);          /* wire time of those bytes */

/* Store access. */
uint8_t  fake_usd_pattern_byte(uint32_t lba, uint32_t i);   /* content of an unwritten block */
void     fake_usd_peek_block(uint32_t lba, uint8_t out[512]);

#ifdef __cplusplus
}
#endif

#endif /* MPS3_FAKE_USD_H */
