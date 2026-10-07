/*
 * usd.c -- see usd.h for the contract, the state diagram and the two hard rules.
 *
 * SHAPE. Two levels of state:
 *   s.state  the card state the rest of the shell sees (usd_state_t);
 *   s.phase  the step inside INIT or inside an I/O op.
 * usd_poll() reads STATUS once (card detect, sticky bits), then runs phase
 * steps until the per-call byte budget is spent or a step has to wait for time.
 * Every step checks, BEFORE it shifts anything, that its worst case fits in
 * what is left of the budget (NEED), so the budget is a hard ceiling on the
 * bytes one call can shift, not a target.
 *
 * Every shift re-reads STATUS (it has to, to see BUSY clear), so a removal is
 * noticed within one shift even in the middle of a data phase: ABORT,
 * CD_CHANGED or !CD_PRESENT in that read ends the step with STEP_GONE and the
 * card goes ABSENT at once.
 *
 * SD SPI-mode reference: SD Physical Layer Simplified Spec v9, §7 (SPI mode).
 */
#include <errno.h>
#include <stddef.h>
#include <string.h>

#include "usd.h"
#include "usd_regs.h"

#define RD(off)     mps3_reg_read32(MPS3_USD_BASE, (off))
#define WR(off, v)  mps3_reg_write32(MPS3_USD_BASE, (off), (v))

/* SD commands used (SPI mode). */
#define SD_CMD0    0u   /* GO_IDLE_STATE */
#define SD_CMD8    8u   /* SEND_IF_COND */
#define SD_CMD9    9u   /* SEND_CSD */
#define SD_CMD12  12u   /* STOP_TRANSMISSION */
#define SD_CMD17  17u   /* READ_SINGLE_BLOCK */
#define SD_CMD18  18u   /* READ_MULTIPLE_BLOCK */
#define SD_CMD24  24u   /* WRITE_BLOCK */
#define SD_CMD25  25u   /* WRITE_MULTIPLE_BLOCK */
#define SD_ACMD41 41u   /* SD_SEND_OP_COND (after CMD55) */
#define SD_CMD55  55u   /* APP_CMD */
#define SD_CMD58  58u   /* READ_OCR */

#define R1_IDLE       0x01u
#define R1_ILLEGAL    0x04u
#define R1_NO_ANSWER  0x80u   /* bit 7 set = no R1 seen within NCR */

#define TOKEN_START_BLOCK  0xFEu  /* read data, CMD17/18/9; write data, CMD24 */
#define TOKEN_START_MULTI  0xFCu  /* write data, CMD25 */
#define TOKEN_STOP_TRAN    0xFDu  /* end of a CMD25 write */
#define DATA_RESP_MASK     0x1Fu
#define DATA_RESP_ACCEPTED 0x05u

#define ACMD41_HCS    0x40000000u
#define CMD8_ARG      0x000001AAu /* VHS = 2.7-3.6 V, check pattern 0xAA */

/* The card answers within NCR = 0..8 bytes of the command (SD spec §7.5.4);
 * USD_NCR_MAX (usd.h) lets a build accept more, as Linux mmc_spi does. */
#define NCR_MAX USD_NCR_MAX

/* Worst-case bytes of one command step: 1 leading 0xFF + 6 command bytes +
 * 1 stuff byte (CMD12 only) + NCR_MAX response hunt + 4 trailing R3/R7 bytes +
 * 1 deselect byte. The step only starts if this much budget is left. */
#define CMD_STEP_BYTES (1u + 6u + 1u + NCR_MAX + 4u + 1u)

/* Safety net on steps per call. The byte budget is the real bound (every step
 * that does not shift either yields or moves to a phase that does); this only
 * guarantees termination if a future edit breaks that property. */
#define USD_POLL_MAX_STEPS 32u

/* A budget smaller than one command step would stall INIT/I/O forever. */
_Static_assert(USD_POLL_BUDGET_SLOW >= CMD_STEP_BYTES, "USD_POLL_BUDGET_SLOW < one command step");
_Static_assert(USD_POLL_BUDGET_FAST >= CMD_STEP_BYTES, "USD_POLL_BUDGET_FAST < one command step");
_Static_assert(USD_WAIT_BYTES_PER_POLL >= 1u, "USD_WAIT_BYTES_PER_POLL must be >= 1");
_Static_assert(NCR_MAX >= 8u, "USD_NCR_MAX below the SD spec's 8");
_Static_assert(USD_CLKDIV_DATA >= 1u && USD_CLKDIV_DATA <= USD_CLKDIV_MASK,
               "USD_CLKDIV_DATA: DIV 0 is outside SD SPI timing");

/* CSD register, CSD_STRUCTURE = 1 (v2.0): C_SIZE is bits [69:48]. */
#define CSD_V2 1u

typedef enum {
    STEP_MORE = 0,  /* progress made; the loop may run another step */
    STEP_YIELD,     /* stop for this call (budget spent, or waiting for time) */
    STEP_GONE,      /* a shift saw the card go (ABORT / CD_CHANGED / !CD_PRESENT) */
    STEP_HW         /* a shift saw a usd_spi fault; s.hw_err says which */
} step_t;

typedef enum {
    PH_NONE = 0,
    /* INIT */
    PH_CLOCKS, PH_CMD0, PH_CMD8, PH_CMD55, PH_ACMD41, PH_ACMD41_WAIT, PH_CMD58,
    PH_CMD9, PH_CSD_TOKEN, PH_CSD_DATA, PH_CSD_CRC, PH_INIT_DONE,
    /* I/O */
    PH_IO_CMD, PH_RD_TOKEN, PH_RD_DATA, PH_RD_CRC, PH_RD_STOP,
    PH_WR_TOKEN, PH_WR_DATA, PH_WR_RESP, PH_WR_BUSY, PH_WR_STOP,
    PH_BUSY_END, PH_IO_FINISH
} phase_t;

static struct {
    bool        inited;
    bool        no_block;      /* ID mismatch: never touch the page again */
    bool        fast;          /* CLKDIV is USD_CLKDIV_DATA (12.5 MHz by default) */
    bool        retry_pending; /* ERROR will go back to INIT at t_retry */
    usd_state_t state;
    phase_t     phase;
    int         err;           /* USD_ERR_* / USD_UNSUP_* */
    int         hw_err;        /* set by shift() with STEP_HW */
    uint32_t    now;           /* now_ms of the current usd_poll() */
    uint32_t    budget;        /* bytes this call may still shift */
    uint32_t    wait_left;     /* 8-bit wait shifts this call may still do */
    uint32_t    ctrl;          /* CTRL as last written */
    uint32_t    t_mark;        /* SETTLE start */
    uint32_t    t_deadline;    /* ACMD41 / token / busy deadline */
    uint32_t    t_retry;       /* ACMD41 re-issue, or ERROR -> INIT */
    uint32_t    tries;         /* init attempts since insertion */
    uint32_t    cmd0_tries;
    uint32_t    card_blocks;
    uint32_t    card_mb;
    uint32_t    change_count;
    uint8_t     csd[16];
    /* the op in flight */
    bool           io_write;
    bool           io_multi;
    uint32_t       io_lba;
    uint32_t       io_n;
    uint32_t       io_done;    /* blocks completed */
    uint32_t       io_idx;     /* byte index inside the current block / CSD */
    uint8_t       *io_rbuf;
    const uint8_t *io_wbuf;
    int            io_err;     /* first error of the op in flight, 0 = none */
    int            io_result;  /* what usd_io_status() returns */
    char           text[17];
} s;

/* Wrap-safe "now has reached t". */
static bool time_reached(uint32_t now, uint32_t t)
{
    return (int32_t)(now - t) >= 0;
}

/* CRC7 over the first five command bytes, polynomial x^7 + x^3 + 1. Only CMD0
 * and CMD8 are CRC-checked by a card in SPI mode, but every command carries a
 * correct one (it costs nothing and survives a later CMD59 CRC_ON). */
static uint8_t crc7(const uint8_t *d, unsigned n)
{
    uint8_t crc = 0;
    for (unsigned i = 0; i < n; i++) {
        uint8_t b = d[i];
        for (unsigned bit = 0; bit < 8u; bit++) {
            crc = (uint8_t)(crc << 1);
            if ((b ^ crc) & 0x80u) {
                crc ^= 0x09u;
            }
            b = (uint8_t)(b << 1);
        }
    }
    return (uint8_t)(crc & 0x7Fu);
}

/* CTRL write that PRESERVES the card-detect bits as the hardware holds them
 * (so a JTAG poke of CD_POL / CD_IGNORE at board step B0 survives) and takes
 * EN / CS / WIDE from the driver's own copy. */
static void ctrl_update(uint32_t set, uint32_t clr)
{
    uint32_t hw = RD(USD_CTRL);
    uint32_t v  = (hw & USD_CTRL_CD_MASK) | (s.ctrl & ~USD_CTRL_CD_MASK);
    v = (v & ~clr) | set;
    WR(USD_CTRL, v);
    s.ctrl = v;
}

static bool present_in(uint32_t st)
{
    return (st & USD_STATUS_CD_PRESENT) != 0u || (s.ctrl & USD_CTRL_CD_IGNORE) != 0u;
}

/* The UNDEBOUNCED pin, polarity applied. CD_PRESENT lags a removal by the
 * debounce time (>= 10 ms); in that window the pads are still driven, the card
 * is gone and MISO floats high, so a read could "complete" with 0xFF data.
 * Every shift already reads STATUS, so checking the raw pin there costs
 * nothing and turns a removal into STEP_GONE on the very next shift. */
static bool raw_in(uint32_t st)
{
    bool pin_high;
    if ((s.ctrl & USD_CTRL_CD_IGNORE) != 0u) {
        return true;
    }
    pin_high = (st & USD_STATUS_CD_RAW) != 0u;
    return ((s.ctrl & USD_CTRL_CD_POL) != 0u) ? pin_high : !pin_high;
}

/* ONE shift, the only place the driver writes DATA. The BUSY spin is the only
 * spin in the driver: one shift, <= 80 us, bounded by USD_BUSY_SPIN_MAX reads.
 * The caller has already checked the budget. */
static step_t shift(uint32_t out, bool wide, uint32_t *in)
{
    uint32_t st;
    uint32_t n = 0;

    if (wide != ((s.ctrl & USD_CTRL_WIDE) != 0u)) {
        if (wide) {
            ctrl_update(USD_CTRL_WIDE, 0u);
        } else {
            ctrl_update(0u, USD_CTRL_WIDE);
        }
    }
    WR(USD_DATA, out);
    s.budget -= wide ? 4u : 1u;
    do {
        st = RD(USD_STATUS);
    } while ((st & USD_STATUS_BUSY) != 0u && ++n < USD_BUSY_SPIN_MAX);

    if ((st & USD_STATUS_BUSY) != 0u) {
        s.hw_err = USD_ERR_HW_BUSY;
        return STEP_HW;
    }
    if ((st & (USD_STATUS_ABORT | USD_STATUS_CD_CHANGED)) != 0u || !present_in(st) ||
        !raw_in(st)) {
        return STEP_GONE;
    }
    if ((st & USD_STATUS_OVR) != 0u) {
        s.hw_err = USD_ERR_HW_OVR;
        return STEP_HW;
    }
    *in = RD(USD_DATA);
    if (!wide) {
        *in &= 0xFFu;   /* contract: zero-extended; do not depend on it */
    }
    return STEP_MORE;
}

#define NEED(n) do { if (s.budget < (uint32_t)(n)) return STEP_YIELD; } while (0)
#define TRY(expr) do { step_t t_ = (expr); if (t_ != STEP_MORE) return t_; } while (0)

/* CS high, then 8 clocks so the card releases MISO. 1 byte. */
static step_t deselect(void)
{
    uint32_t in;
    if ((s.ctrl & USD_CTRL_CS) != 0u) {
        ctrl_update(0u, USD_CTRL_CS);
    }
    return shift(0xFFu, false, &in);
}

/* One command exchange with CS asserted (left asserted). r[0] = R1, or
 * R1_NO_ANSWER-flagged 0xFF if the card said nothing within NCR_MAX bytes;
 * r[1..extra] = the trailing R3/R7 bytes. stuff = discard one byte after the
 * command (CMD12). At most CMD_STEP_BYTES - 1 bytes. */
static step_t command(uint8_t idx, uint32_t arg, unsigned extra, bool stuff, uint8_t r[5])
{
    uint8_t  f[6];
    uint32_t in;

    f[0] = (uint8_t)(0x40u | idx);
    f[1] = (uint8_t)(arg >> 24);
    f[2] = (uint8_t)(arg >> 16);
    f[3] = (uint8_t)(arg >> 8);
    f[4] = (uint8_t)arg;
    f[5] = (uint8_t)((crc7(f, 5u) << 1) | 1u);

    if ((s.ctrl & USD_CTRL_CS) == 0u) {
        ctrl_update(USD_CTRL_CS, 0u);
    }
    TRY(shift(0xFFu, false, &in));
    for (unsigned i = 0; i < 6u; i++) {
        TRY(shift(f[i], false, &in));
    }
    if (stuff) {
        TRY(shift(0xFFu, false, &in));
    }
    r[0] = 0xFFu;
    for (unsigned i = 0; i < NCR_MAX; i++) {
        TRY(shift(0xFFu, false, &in));
        if ((in & 0x80u) == 0u) {
            r[0] = (uint8_t)in;
            break;
        }
    }
    if ((r[0] & R1_NO_ANSWER) == 0u) {
        for (unsigned i = 0; i < extra; i++) {
            TRY(shift(0xFFu, false, &in));
            r[1u + i] = (uint8_t)in;
        }
    }
    return STEP_MORE;
}

/* 8-bit wait: shift 0xFF until the byte read is 0xFF (want_ff: card not busy)
 * or is not 0xFF (!want_ff: a token). STEP_MORE = found (*got holds it);
 * STEP_YIELD = not yet, and this call's wait allowance or budget is spent. */
static step_t wait_byte(bool want_ff, uint8_t *got)
{
    uint32_t in;
    while (s.budget >= 1u && s.wait_left > 0u) {
        s.wait_left--;
        TRY(shift(0xFFu, false, &in));
        if ((in == 0xFFu) == want_ff) {
            *got = (uint8_t)in;
            return STEP_MORE;
        }
    }
    return STEP_YIELD;
}

/* WIDE receive into buf[s.io_idx .. len), resumable across calls. */
static step_t rx_words(uint8_t *buf, uint32_t len)
{
    uint32_t in;
    while (s.io_idx < len) {
        NEED(4u);
        TRY(shift(0xFFFFFFFFu, true, &in));
        buf[s.io_idx + 0u] = (uint8_t)(in >> 24);
        buf[s.io_idx + 1u] = (uint8_t)(in >> 16);
        buf[s.io_idx + 2u] = (uint8_t)(in >> 8);
        buf[s.io_idx + 3u] = (uint8_t)in;
        s.io_idx += 4u;
    }
    return STEP_MORE;
}

/* WIDE transmit of buf[s.io_idx .. len), resumable across calls. */
static step_t tx_words(const uint8_t *buf, uint32_t len)
{
    uint32_t in;
    while (s.io_idx < len) {
        NEED(4u);
        uint32_t w = ((uint32_t)buf[s.io_idx] << 24) | ((uint32_t)buf[s.io_idx + 1u] << 16) |
                     ((uint32_t)buf[s.io_idx + 2u] << 8) | (uint32_t)buf[s.io_idx + 3u];
        TRY(shift(w, true, &in));
        s.io_idx += 4u;
    }
    return STEP_MORE;
}

/* ---- state entries -------------------------------------------------------- */

static void release_pads(void)
{
    ctrl_update(0u, USD_CTRL_EN | USD_CTRL_CS | USD_CTRL_WIDE);
    s.fast = false;
    s.card_blocks = 0;
    s.card_mb = 0;
    s.phase = PH_NONE;
}

/* Any op in flight ends with `code`. */
static void fail_io(int code)
{
    if (s.io_result == USD_IO_BUSY) {
        s.io_result = code;
    }
}

static void enter_absent(void)
{
    fail_io(-ENODEV);
    release_pads();
    s.state = USD_ABSENT;
    s.err = USD_ERR_NONE;
    s.retry_pending = false;
    s.tries = 0;
    s.change_count++;
}

static void enter_error(int code)
{
    fail_io(code == USD_ERR_IO_TMO ? -ETIMEDOUT : -EIO);
    release_pads();
    if (code == USD_ERR_HW_BUSY) {
        /* EN=0 with a shift still "in progress" is OUR doing, and the block
         * flags it as ABORT. Clear it, or the next poll reads it as a removal. */
        WR(USD_STATUS, USD_STATUS_ABORT);
    }
    s.state = USD_ERROR;
    s.err = code;
    s.retry_pending = s.tries < USD_INIT_TRIES;
    s.t_retry = s.now + USD_RETRY_MS;
}

static void enter_unsupported(int code)
{
    release_pads();
    s.state = USD_UNSUPPORTED;
    s.err = code;
}

static void enter_init(void)
{
    s.tries++;
    WR(USD_CLKDIV, USD_CLKDIV_400K);
    s.fast = false;
    ctrl_update(USD_CTRL_EN, USD_CTRL_CS | USD_CTRL_WIDE);   /* EN=1, CS deasserted */
    s.state = USD_INIT;
    s.phase = PH_CLOCKS;
    s.err = USD_ERR_NONE;
}

static step_t init_fail(int code)
{
    enter_error(code);
    return STEP_YIELD;
}

static step_t init_unsupported(int code)
{
    enter_unsupported(code);
    return STEP_YIELD;
}

/* ---- the step machine ----------------------------------------------------- */

static void csd_parse(void)
{
    uint32_t c_size = ((uint32_t)(s.csd[7] & 0x3Fu) << 16) | ((uint32_t)s.csd[8] << 8) |
                      (uint32_t)s.csd[9];
    /* capacity = (C_SIZE + 1) * 512 KiB */
    s.card_blocks = (c_size + 1u) << 10;
    s.card_mb = (c_size + 1u) >> 1;
}

static step_t step(void)
{
    uint8_t  r[5] = { 0xFFu, 0xFFu, 0xFFu, 0xFFu, 0xFFu };
    uint8_t  b = 0xFFu;
    uint32_t in;
    step_t   t;

    switch (s.phase) {
    case PH_NONE:
        return STEP_YIELD;

    /* ---------------- INIT, at 400 kHz ---------------- */
    case PH_CLOCKS:                               /* >= 74 clocks, CS high */
        NEED(10u);
        for (unsigned i = 0; i < 10u; i++) {
            TRY(shift(0xFFu, false, &in));
        }
        s.cmd0_tries = 0;
        s.phase = PH_CMD0;
        return STEP_MORE;

    case PH_CMD0:
        NEED(CMD_STEP_BYTES);
        TRY(command(SD_CMD0, 0u, 0u, false, r));
        TRY(deselect());
        if (r[0] == R1_IDLE) {
            s.phase = PH_CMD8;
        } else if (++s.cmd0_tries >= USD_CMD0_TRIES) {
            return init_fail(USD_ERR_CMD0);
        }
        return STEP_MORE;

    case PH_CMD8:
        NEED(CMD_STEP_BYTES);
        TRY(command(SD_CMD8, CMD8_ARG, 4u, false, r));
        TRY(deselect());
        if ((r[0] & R1_NO_ANSWER) != 0u) {
            return init_fail(USD_ERR_CMD8);
        }
        if ((r[0] & R1_ILLEGAL) != 0u) {
            return init_unsupported(USD_UNSUP_V1);
        }
        if (r[0] != R1_IDLE) {
            return init_fail(USD_ERR_CMD8);
        }
        if ((r[3] & 0x0Fu) != 0x01u) {
            return init_unsupported(USD_UNSUP_VOLTAGE);
        }
        if (r[4] != (uint8_t)CMD8_ARG) {
            return init_fail(USD_ERR_CMD8);
        }
        s.t_deadline = s.now + USD_ACMD41_TIMEOUT_MS;
        s.phase = PH_CMD55;
        return STEP_MORE;

    case PH_CMD55:
        NEED(CMD_STEP_BYTES);
        TRY(command(SD_CMD55, 0u, 0u, false, r));
        TRY(deselect());
        if ((r[0] & (uint8_t)~R1_IDLE) != 0u) {           /* incl. no answer */
            return init_fail(USD_ERR_ACMD41);
        }
        s.phase = PH_ACMD41;
        return STEP_MORE;

    case PH_ACMD41:                               /* a RETRY STATE, not a loop */
        NEED(CMD_STEP_BYTES);
        TRY(command(SD_ACMD41, ACMD41_HCS, 0u, false, r));
        TRY(deselect());
        if (r[0] == 0x00u) {
            s.phase = PH_CMD58;
            return STEP_MORE;
        }
        if (r[0] != R1_IDLE) {
            return init_fail(USD_ERR_ACMD41);
        }
        if (time_reached(s.now, s.t_deadline)) {
            return init_fail(USD_ERR_ACMD41_TMO);
        }
        s.t_retry = s.now + USD_ACMD41_RETRY_MS;
        s.phase = PH_ACMD41_WAIT;
        return STEP_YIELD;

    case PH_ACMD41_WAIT:
        if (!time_reached(s.now, s.t_retry)) {
            return STEP_YIELD;
        }
        s.phase = PH_CMD55;
        return STEP_MORE;

    case PH_CMD58:
        NEED(CMD_STEP_BYTES);
        TRY(command(SD_CMD58, 0u, 4u, false, r));
        TRY(deselect());
        if (r[0] != 0x00u || (r[1] & 0x80u) == 0u) {        /* R1, OCR power-up done */
            return init_fail(USD_ERR_CMD58);
        }
        if ((r[1] & 0x40u) == 0u) {                          /* CCS */
            return init_unsupported(USD_UNSUP_SDSC);
        }
        s.phase = PH_CMD9;
        return STEP_MORE;

    case PH_CMD9:
        NEED(CMD_STEP_BYTES);
        TRY(command(SD_CMD9, 0u, 0u, false, r));
        if (r[0] != 0x00u) {
            return init_fail(USD_ERR_CMD9);
        }
        s.t_deadline = s.now + USD_READ_TIMEOUT_MS;
        s.io_idx = 0;
        s.phase = PH_CSD_TOKEN;
        return STEP_MORE;

    case PH_CSD_TOKEN:
        t = wait_byte(false, &b);
        if (t == STEP_YIELD) {
            if (time_reached(s.now, s.t_deadline)) {
                return init_fail(USD_ERR_CSD_TMO);
            }
            return STEP_YIELD;
        }
        if (t != STEP_MORE) {
            return t;
        }
        if (b != TOKEN_START_BLOCK) {
            return init_fail(USD_ERR_CMD9);
        }
        s.phase = PH_CSD_DATA;
        return STEP_MORE;

    case PH_CSD_DATA:
        TRY(rx_words(s.csd, sizeof s.csd));
        s.phase = PH_CSD_CRC;
        return STEP_MORE;

    case PH_CSD_CRC:
        NEED(3u);
        TRY(shift(0xFFu, false, &in));
        TRY(shift(0xFFu, false, &in));
        TRY(deselect());
        if ((s.csd[0] >> 6) != CSD_V2) {
            return init_unsupported(USD_UNSUP_CSD);
        }
        csd_parse();
        s.phase = PH_INIT_DONE;
        return STEP_MORE;

    case PH_INIT_DONE:
        WR(USD_CLKDIV, USD_CLKDIV_DATA);
        s.fast = true;
        s.state = USD_READY;
        s.err = USD_ERR_NONE;
        s.phase = PH_NONE;
        return STEP_YIELD;

    /* ---------------- I/O, at 12.5 MHz ---------------- */
    case PH_IO_CMD: {
        uint8_t idx = s.io_write ? (s.io_multi ? SD_CMD25 : SD_CMD24)
                                 : (s.io_multi ? SD_CMD18 : SD_CMD17);
        NEED(CMD_STEP_BYTES);
        TRY(command(idx, s.io_lba, 0u, false, r));
        if (r[0] != 0x00u) {
            s.io_err = -EIO;
            s.phase = PH_IO_FINISH;
            return STEP_MORE;
        }
        s.io_done = 0;
        s.io_idx = 0;
        if (s.io_write) {
            s.phase = PH_WR_TOKEN;
        } else {
            s.t_deadline = s.now + USD_READ_TIMEOUT_MS;
            s.phase = PH_RD_TOKEN;
        }
        return STEP_MORE;
    }

    case PH_RD_TOKEN:
        t = wait_byte(false, &b);
        if (t == STEP_YIELD) {
            if (time_reached(s.now, s.t_deadline)) {
                s.io_err = -ETIMEDOUT;
                s.phase = s.io_multi ? PH_RD_STOP : PH_IO_FINISH;
                return STEP_MORE;
            }
            return STEP_YIELD;
        }
        if (t != STEP_MORE) {
            return t;
        }
        if (b != TOKEN_START_BLOCK) {                 /* a data-error token */
            s.io_err = -EIO;
            s.phase = s.io_multi ? PH_RD_STOP : PH_IO_FINISH;
            return STEP_MORE;
        }
        s.io_idx = 0;
        s.phase = PH_RD_DATA;
        return STEP_MORE;

    case PH_RD_DATA:
        TRY(rx_words(s.io_rbuf + s.io_done * USD_BLOCK_SIZE, USD_BLOCK_SIZE));
        s.phase = PH_RD_CRC;
        return STEP_MORE;

    case PH_RD_CRC:                               /* CRC16, not checked (CRC off) */
        NEED(2u);
        TRY(shift(0xFFu, false, &in));
        TRY(shift(0xFFu, false, &in));
        s.io_done++;
        if (s.io_done < s.io_n) {
            s.t_deadline = s.now + USD_READ_TIMEOUT_MS;
            s.phase = PH_RD_TOKEN;
        } else {
            s.phase = s.io_multi ? PH_RD_STOP : PH_IO_FINISH;
        }
        return STEP_MORE;

    case PH_RD_STOP:                              /* CMD12, R1b */
        NEED(CMD_STEP_BYTES);
        TRY(command(SD_CMD12, 0u, 0u, true, r));
        if ((r[0] & (uint8_t)~R1_IDLE) != 0u && s.io_err == 0) {
            s.io_err = -EIO;
        }
        s.t_deadline = s.now + USD_WRITE_TIMEOUT_MS;
        s.phase = PH_BUSY_END;
        return STEP_MORE;

    case PH_WR_TOKEN:                             /* 1 gap byte, then the token */
        NEED(2u);
        TRY(shift(0xFFu, false, &in));
        TRY(shift(s.io_multi ? TOKEN_START_MULTI : TOKEN_START_BLOCK, false, &in));
        s.io_idx = 0;
        s.phase = PH_WR_DATA;
        return STEP_MORE;

    case PH_WR_DATA:
        TRY(tx_words(s.io_wbuf + s.io_done * USD_BLOCK_SIZE, USD_BLOCK_SIZE));
        s.phase = PH_WR_RESP;
        return STEP_MORE;

    case PH_WR_RESP:                              /* dummy CRC16, then data response */
        NEED(3u);
        TRY(shift(0xFFu, false, &in));
        TRY(shift(0xFFu, false, &in));
        TRY(shift(0xFFu, false, &in));
        if ((in & DATA_RESP_MASK) != DATA_RESP_ACCEPTED) {
            s.io_err = -EIO;
        }
        s.t_deadline = s.now + USD_WRITE_TIMEOUT_MS;
        s.phase = PH_WR_BUSY;
        return STEP_MORE;

    case PH_WR_BUSY:                              /* MISO low while programming: a STATE */
        t = wait_byte(true, &b);
        if (t == STEP_YIELD) {
            if (time_reached(s.now, s.t_deadline)) {
                s.io_err = -ETIMEDOUT;
                s.phase = PH_IO_FINISH;
                return STEP_MORE;
            }
            return STEP_YIELD;
        }
        if (t != STEP_MORE) {
            return t;
        }
        s.io_done++;
        if (s.io_err == 0 && s.io_done < s.io_n) {
            s.phase = PH_WR_TOKEN;
        } else {
            s.phase = s.io_multi ? PH_WR_STOP : PH_IO_FINISH;
        }
        return STEP_MORE;

    case PH_WR_STOP:                              /* stop token + one Nbr byte */
        NEED(2u);
        TRY(shift(TOKEN_STOP_TRAN, false, &in));
        TRY(shift(0xFFu, false, &in));
        s.t_deadline = s.now + USD_WRITE_TIMEOUT_MS;
        s.phase = PH_BUSY_END;
        return STEP_MORE;

    case PH_BUSY_END:                             /* R1b after CMD12, or after 0xFD */
        t = wait_byte(true, &b);
        if (t == STEP_YIELD) {
            if (time_reached(s.now, s.t_deadline)) {
                if (s.io_err == 0) {
                    s.io_err = -ETIMEDOUT;
                }
                s.phase = PH_IO_FINISH;
                return STEP_MORE;
            }
            return STEP_YIELD;
        }
        if (t != STEP_MORE) {
            return t;
        }
        s.phase = PH_IO_FINISH;
        return STEP_MORE;

    case PH_IO_FINISH:
        NEED(1u);
        TRY(deselect());
        s.phase = PH_NONE;
        if (s.io_err == -ETIMEDOUT) {
            /* A card that stops answering is re-initialised (ERROR, then the
             * normal auto-retry), not left READY to time out every op. */
            enter_error(USD_ERR_IO_TMO);
            return STEP_YIELD;
        }
        s.io_result = (s.io_err != 0) ? s.io_err : USD_IO_DONE;
        return STEP_YIELD;
    }
    return STEP_YIELD;
}

/* Run steps until the budget is spent or a step waits. */
static void run(void)
{
    s.budget = s.fast ? USD_POLL_BUDGET_FAST : USD_POLL_BUDGET_SLOW;
    s.wait_left = USD_WAIT_BYTES_PER_POLL;
    for (uint32_t i = 0; i < USD_POLL_MAX_STEPS && s.phase != PH_NONE; i++) {
        step_t t = step();
        if (t == STEP_MORE) {
            continue;
        }
        if (t == STEP_GONE) {
            enter_absent();
        } else if (t == STEP_HW) {
            enter_error(s.hw_err);
        }
        return;
    }
}

/* ---- API ------------------------------------------------------------------ */

void usd_init(void)
{
    memset(&s, 0, sizeof s);
    s.state = USD_ABSENT;
    s.io_result = USD_IO_DONE;
    s.inited = true;

    if (RD(USD_ID) != USD_ID_VALUE) {
        /* No usd_spi here. Never write this page: on an older shell it belongs
         * to something else. */
        s.no_block = true;
        s.err = USD_ERR_NO_BLOCK;
        return;
    }
    /* EN / CS / WIDE off (a MicroBlaze-only reset can leave them set), CD bits
     * kept as the hardware holds them plus the build default. Sticky STATUS
     * bits are left for the first usd_poll(), which clears what it reads. */
    s.ctrl = (RD(USD_CTRL) & USD_CTRL_CD_MASK) | (USD_CTRL_CD_DEFAULT & USD_CTRL_CD_MASK);
    WR(USD_CTRL, s.ctrl);
}

void usd_poll(uint32_t now_ms)
{
    uint32_t st;
    bool     present;

    if (!s.inited || s.no_block) {
        return;
    }
    s.now = now_ms;

    st = RD(USD_STATUS);
    if ((st & USD_STATUS_W1C_MASK) != 0u) {
        WR(USD_STATUS, st & USD_STATUS_W1C_MASK);
    }
    if (s.state == USD_ABSENT) {
        /* Pick up CD_POL / CD_IGNORE set over JTAG (board step B0) while the
         * driver is not writing CTRL itself; raw_in() needs the current POL. */
        s.ctrl = (s.ctrl & ~USD_CTRL_CD_MASK) | (RD(USD_CTRL) & USD_CTRL_CD_MASK);
    }
    present = present_in(st);

    if (s.state != USD_ABSENT) {
        if (!present || (st & (USD_STATUS_CD_CHANGED | USD_STATUS_ABORT)) != 0u) {
            /* Removed -- or removed and re-inserted between two polls, which
             * is still a different card until proven otherwise. */
            enter_absent();
            if (!present) {
                return;
            }
        } else if ((st & USD_STATUS_OVR) != 0u) {
            enter_error(USD_ERR_HW_OVR);
            return;
        }
    }

    switch (s.state) {
    case USD_ABSENT:
        /* Both the debounced bit and the raw pin: after a removal caught by
         * raw_in() mid-transfer, CD_PRESENT still reads 1 for the debounce
         * time, and that must not look like a new insertion. */
        if (present && raw_in(st)) {
            s.state = USD_SETTLE;
            s.t_mark = now_ms;
            s.tries = 0;
            s.change_count++;
        }
        return;
    case USD_SETTLE:
        if (!time_reached(now_ms, s.t_mark + USD_SETTLE_MS)) {
            return;
        }
        enter_init();
        run();
        return;
    case USD_INIT:
        run();
        return;
    case USD_READY:
        if (s.phase != PH_NONE) {
            run();
        }
        return;
    case USD_ERROR:
        if (s.retry_pending && time_reached(now_ms, s.t_retry)) {
            if ((st & USD_STATUS_BUSY) != 0u) {
                /* Still stuck (USD_ERR_HW_BUSY): a DATA write now would only be
                 * ignored and set OVR. Spend the try without shifting. */
                s.tries++;
                s.retry_pending = s.tries < USD_INIT_TRIES;
                s.t_retry = now_ms + USD_RETRY_MS;
                return;
            }
            enter_init();
            run();
        }
        return;
    case USD_UNSUPPORTED:
        return;
    }
}

usd_state_t usd_state(void)      { return s.state; }
int         usd_error_code(void) { return s.err; }
bool        usd_present(void)    { return s.state != USD_ABSENT; }
uint32_t    usd_card_mb(void)     { return s.state == USD_READY ? s.card_mb : 0u; }
uint32_t    usd_card_blocks(void) { return s.state == USD_READY ? s.card_blocks : 0u; }
uint32_t    usd_change_count(void) { return s.change_count; }

const char *usd_state_text(void)
{
    switch (s.state) {
    case USD_ABSENT:      return "none";
    case USD_SETTLE:
    case USD_INIT:        return "init";
    case USD_READY:       return "ready";
    case USD_UNSUPPORTED: return "unsupported";
    case USD_ERROR:
        break;
    }
    /* "ERR <n>": at most 4 + 10 digits = 14 chars. Codes are small and
     * non-negative; a negative one would be a bug and prints as its magnitude. */
    {
        char     digits[10];
        unsigned nd = 0;
        uint32_t v = (s.err < 0) ? (uint32_t)(-(int64_t)s.err) : (uint32_t)s.err;
        char    *p = s.text;
        memcpy(p, "ERR ", 4u);
        p += 4;
        do {
            digits[nd++] = (char)('0' + (v % 10u));
            v /= 10u;
        } while (v != 0u && nd < sizeof digits);
        while (nd > 0u) {
            *p++ = digits[--nd];
        }
        *p = '\0';
    }
    return s.text;
}

static int io_start(uint32_t lba, uint32_t nblocks, uint8_t *rbuf, const uint8_t *wbuf)
{
    if (s.state != USD_READY) {
        return -ENODEV;
    }
    if (s.io_result == USD_IO_BUSY) {
        return -EBUSY;
    }
    if ((rbuf == NULL && wbuf == NULL) || nblocks == 0u || nblocks > USD_MAX_BLOCKS_PER_OP) {
        return -EINVAL;
    }
    if (lba >= s.card_blocks || nblocks > s.card_blocks - lba) {
        return -EINVAL;
    }
    s.io_write = (wbuf != NULL);
    s.io_multi = (nblocks > 1u);
    s.io_lba = lba;
    s.io_n = nblocks;
    s.io_done = 0;
    s.io_idx = 0;
    s.io_rbuf = rbuf;
    s.io_wbuf = wbuf;
    s.io_err = 0;
    s.io_result = USD_IO_BUSY;
    s.phase = PH_IO_CMD;
    return 0;
}

int usd_read_start(uint32_t lba, uint32_t nblocks, void *buf)
{
    return io_start(lba, nblocks, (uint8_t *)buf, NULL);
}

int usd_write_start(uint32_t lba, uint32_t nblocks, const void *buf)
{
    return io_start(lba, nblocks, NULL, (const uint8_t *)buf);
}

int usd_io_status(void)
{
    return s.io_result;
}
