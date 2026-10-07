/*
 * fake_usd.c -- see fake_usd.h. Two stacked models (a register-level block and a device behind it):
 *
 *   1. usd_spi (the shell block): registers, pad gating, card-detect debounce
 *      and polarity, BUSY / OVR / ABORT / CD_CHANGED, wire-time accounting.
 *   2. an SD card in SPI mode, clocked one byte at a time: MISO for a byte is
 *      decided BEFORE that byte's MOSI is seen (full duplex), so a response can
 *      only start on the byte after the command's last byte, as on a real card.
 *
 * The CRC7 here is computed as a straight polynomial division over the 40-bit
 * message, deliberately NOT the shift-register form usd.c uses, so the two
 * cannot share a bug and agree.
 */
#include <string.h>

#include "../common/platform_regs.h"
#include "../usd/usd_regs.h"
#include "mock_regs.h"
#include "fake_usd.h"

#define BLK 512u

/* ======================= the card ========================================= */

typedef enum { WR_NONE = 0, WR_WAIT_TOKEN, WR_DATA } wr_state_t;

static struct {
    /* personality */
    int      sdsc, v1, unresponsive;
    uint32_t acmd41_busy_cfg;
    uint32_t write_busy_cfg;
    uint32_t read_nac_cfg;
    uint32_t ncr_cfg;
    uint32_t c_size;
    uint32_t reject_at;      /* 1-based index of the next data block to reject; 0 = none */
    /* protocol state (reset by power loss) */
    int      idle;           /* after CMD0, before ACMD41 completes */
    int      ready;          /* ACMD41 completed */
    int      app;            /* last command was CMD55 */
    int      seen_cmd0;
    uint32_t pre_clocks;     /* CS-high clocks before the first CMD0 */
    uint32_t acmd41_left;
    uint8_t  cmd[6];
    unsigned cmd_len;
    uint8_t  q[32];          /* queued command responses */
    unsigned q_head, q_len;
    uint32_t busy;           /* MISO-low bytes still to emit */
    /* read data source (CMD9 / CMD17 / CMD18) */
    int      rd_active, rd_multi, rd_csd;
    uint32_t rd_lba, rd_nac;
    int      rd_pos;         /* -1 = token next; 0..len-1 data; len, len+1 CRC */
    uint8_t  rd_buf[BLK];
    /* write sink (CMD24 / CMD25) */
    wr_state_t wr;
    int      wr_multi;
    uint32_t wr_lba;
    uint32_t wr_idx;
    uint8_t  wr_buf[BLK + 2u];
} c;

/* Sparse block store: the card's flash. Survives removal. */
static struct {
    int      used;
    uint32_t lba;
    uint8_t  data[BLK];
} s_store[FAKE_USD_STORE_SLOTS];

/* Counters. */
static uint32_t s_cmd_count[64];
static uint32_t s_cmd_div[64];
static uint8_t  s_cmd_frame[64][6];
static uint32_t s_crc_errors, s_crc_bad_any, s_frame_errors, s_busy_violations, s_protocol_errors;
static uint32_t s_stop_tokens, s_blocks_written, s_store_overflows;

uint8_t fake_usd_pattern_byte(uint32_t lba, uint32_t i)
{
    return (uint8_t)(lba * 157u + (lba >> 8) * 31u + i * 7u + (i >> 8) * 13u + 0x5Au);
}

static void store_read(uint32_t lba, uint8_t out[BLK])
{
    for (unsigned k = 0; k < FAKE_USD_STORE_SLOTS; k++) {
        if (s_store[k].used && s_store[k].lba == lba) {
            memcpy(out, s_store[k].data, BLK);
            return;
        }
    }
    for (uint32_t i = 0; i < BLK; i++) {
        out[i] = fake_usd_pattern_byte(lba, i);
    }
}

static void store_write(uint32_t lba, const uint8_t in[BLK])
{
    int free_k = -1;
    for (unsigned k = 0; k < FAKE_USD_STORE_SLOTS; k++) {
        if (s_store[k].used && s_store[k].lba == lba) {
            memcpy(s_store[k].data, in, BLK);
            s_blocks_written++;
            return;
        }
        if (!s_store[k].used && free_k < 0) {
            free_k = (int)k;
        }
    }
    if (free_k < 0) {
        s_store_overflows++;
        return;
    }
    s_store[free_k].used = 1;
    s_store[free_k].lba = lba;
    memcpy(s_store[free_k].data, in, BLK);
    s_blocks_written++;
}

void fake_usd_peek_block(uint32_t lba, uint8_t out[512])
{
    store_read(lba, out);
}

static uint32_t card_blocks(void)
{
    return (c.c_size + 1u) << 10;
}

/* CRC7 by polynomial long division of the 40-bit message * x^7 by
 * x^7 + x^3 + 1 (0x89). */
static uint8_t crc7_div(const uint8_t *d)
{
    uint64_t m = 0;
    for (unsigned i = 0; i < 5u; i++) {
        m = (m << 8) | d[i];
    }
    m <<= 7;                                  /* 47-bit dividend */
    for (int bit = 46; bit >= 7; bit--) {
        if (m & ((uint64_t)1 << bit)) {
            m ^= (uint64_t)0x89u << (bit - 7);
        }
    }
    return (uint8_t)(m & 0x7Fu);
}

/* What CMD0 resets. */
static void card_proto_reset(void)
{
    c.idle = 0;
    c.ready = 0;
    c.app = 0;
    c.cmd_len = 0;
    c.q_head = c.q_len = 0;
    c.busy = 0;
    c.rd_active = 0;
    c.wr = WR_NONE;
}

/* What a power cycle (insertion / removal) resets on top. */
static void card_power_on(void)
{
    card_proto_reset();
    c.seen_cmd0 = 0;
    c.pre_clocks = 0;
    c.acmd41_left = c.acmd41_busy_cfg;
}

static void q_push(uint8_t b)
{
    if (c.q_len < sizeof c.q) {
        c.q[(c.q_head + c.q_len) % sizeof c.q] = b;
        c.q_len++;
    } else {
        s_protocol_errors++;
    }
}

static uint8_t q_pop(void)
{
    uint8_t b = c.q[c.q_head];
    c.q_head = (c.q_head + 1u) % sizeof c.q;
    c.q_len--;
    return b;
}

/* NCR 0xFF bytes, then the response bytes. */
static void respond(const uint8_t *r, unsigned n)
{
    for (uint32_t i = 0; i < c.ncr_cfg; i++) {
        q_push(0xFFu);
    }
    for (unsigned i = 0; i < n; i++) {
        q_push(r[i]);
    }
}

static void respond_r1(uint8_t r1)
{
    respond(&r1, 1u);
}

static void csd_fill(uint8_t csd[16])
{
    memset(csd, 0, 16);
    csd[0]  = 0x40u;                          /* CSD_STRUCTURE = 1 (v2.0) */
    csd[1]  = 0x0Eu;                          /* TAAC */
    csd[3]  = 0x32u;                          /* TRAN_SPEED 25 MHz */
    csd[4]  = 0x5Bu;
    csd[5]  = 0x59u;                          /* CCC, READ_BL_LEN = 9 */
    csd[7]  = (uint8_t)((c.c_size >> 16) & 0x3Fu);
    csd[8]  = (uint8_t)(c.c_size >> 8);
    csd[9]  = (uint8_t)c.c_size;
    csd[10] = 0x7Fu;
    csd[11] = 0x80u;
    csd[12] = 0x0Au;
    csd[13] = 0x40u;
    csd[15] = 0x01u;
}

static void rd_start(int csd, int multi, uint32_t lba)
{
    c.rd_active = 1;
    c.rd_csd = csd;
    c.rd_multi = multi;
    c.rd_lba = lba;
    c.rd_nac = c.read_nac_cfg;
    c.rd_pos = -1;
}

/* A complete 6-byte command frame arrived. */
static void card_exec(uint32_t clkdiv)
{
    unsigned idx = c.cmd[0] & 0x3Fu;
    uint32_t arg = ((uint32_t)c.cmd[1] << 24) | ((uint32_t)c.cmd[2] << 16) |
                   ((uint32_t)c.cmd[3] << 8) | (uint32_t)c.cmd[4];
    int      app = c.app;
    uint8_t  r1;

    c.app = 0;
    s_cmd_count[idx]++;
    s_cmd_div[idx] = clkdiv;
    memcpy(s_cmd_frame[idx], c.cmd, 6);

    if ((c.cmd[5] & 1u) == 0u) {
        s_frame_errors++;
    }
    if ((uint8_t)(c.cmd[5] >> 1) != crc7_div(c.cmd)) {
        s_crc_bad_any++;                      /* a real card only checks CMD0/CMD8 */
    }
    if (idx == 0u && !c.seen_cmd0) {
        c.seen_cmd0 = 1;                      /* pre_clocks freezes here */
    }
    if (c.unresponsive) {
        return;
    }
    if (!app && (idx == 0u || idx == 8u) && (uint8_t)(c.cmd[5] >> 1) != crc7_div(c.cmd)) {
        s_crc_errors++;
        respond_r1((uint8_t)((c.idle ? 0x01u : 0x00u) | 0x08u));   /* COM_CRC error */
        return;
    }
    r1 = c.idle ? 0x01u : 0x00u;

    if (app) {
        if (idx == 41u) {
            if ((arg & 0x40000000u) == 0u && !c.sdsc) {
                respond_r1(0x01u);            /* SDHC without HCS: never ready */
            } else if (c.acmd41_left > 0u) {
                c.acmd41_left--;
                respond_r1(0x01u);
            } else {
                c.idle = 0;
                c.ready = 1;
                respond_r1(0x00u);
            }
        } else {
            respond_r1((uint8_t)(r1 | 0x04u));
        }
        return;
    }

    switch (idx) {
    case 0u:
        card_proto_reset();
        c.idle = 1;
        respond_r1(0x01u);
        break;
    case 8u:
        if (c.v1) {
            respond_r1((uint8_t)(r1 | 0x04u));
        } else {
            uint8_t r[5] = { r1, 0x00u, 0x00u,
                             (uint8_t)((((arg >> 8) & 0x0Fu) == 0x01u) ? 0x01u : 0x00u),
                             (uint8_t)arg };
            respond(r, 5u);
        }
        break;
    case 9u:
        if (!c.ready) {
            respond_r1((uint8_t)(r1 | 0x04u));
        } else {
            respond_r1(0x00u);
            rd_start(1, 0, 0u);
        }
        break;
    case 12u:
        if (c.rd_active && c.rd_multi) {
            c.rd_active = 0;
            c.q_head = c.q_len = 0;
            q_push(0x3Fu);                    /* stuff byte: NOT 0xFF on purpose */
            respond_r1(0x00u);
            c.busy = 2u;
        } else {
            s_protocol_errors++;
            q_push(0x3Fu);
            respond_r1((uint8_t)(r1 | 0x04u));
        }
        break;
    case 17u:
    case 18u:
        if (!c.ready || c.sdsc) {
            respond_r1((uint8_t)(r1 | 0x04u));
        } else if (arg >= card_blocks()) {
            respond_r1(0x40u);                /* parameter error */
        } else {
            respond_r1(0x00u);
            rd_start(0, idx == 18u, arg);
        }
        break;
    case 24u:
    case 25u:
        if (!c.ready || c.sdsc) {
            respond_r1((uint8_t)(r1 | 0x04u));
        } else if (arg >= card_blocks()) {
            respond_r1(0x40u);
        } else {
            respond_r1(0x00u);
            c.wr = WR_WAIT_TOKEN;
            c.wr_multi = (idx == 25u);
            c.wr_lba = arg;
        }
        break;
    case 55u:
        c.app = 1;
        respond_r1(r1);
        break;
    case 58u: {
        uint8_t ocr0 = (uint8_t)((c.ready ? 0x80u : 0x00u) | ((c.ready && !c.sdsc) ? 0x40u : 0x00u));
        uint8_t r[5] = { r1, ocr0, 0xFFu, 0x80u, 0x00u };
        respond(r, 5u);
        break;
    }
    default:
        respond_r1((uint8_t)(r1 | 0x04u));
        break;
    }
}

/* The next read-data byte. */
static uint8_t rd_next(void)
{
    unsigned len = c.rd_csd ? 16u : BLK;
    uint8_t  b;

    if (c.rd_nac > 0u) {
        c.rd_nac--;
        return 0xFFu;
    }
    if (c.rd_pos < 0) {
        if (c.rd_csd) {
            csd_fill(c.rd_buf);
        } else {
            store_read(c.rd_lba, c.rd_buf);
        }
        c.rd_pos = 0;
        return 0xFEu;
    }
    if ((unsigned)c.rd_pos < len) {
        return c.rd_buf[c.rd_pos++];
    }
    /* two CRC bytes (not checked by the driver: CRC is off) */
    b = 0x00u;
    c.rd_pos++;
    if ((unsigned)c.rd_pos == len + 2u) {
        if (c.rd_multi) {
            c.rd_lba++;
            c.rd_nac = c.read_nac_cfg;
            c.rd_pos = -1;
            if (c.rd_lba >= card_blocks()) {
                s_protocol_errors++;          /* streamed off the end */
                c.rd_active = 0;
            }
        } else {
            c.rd_active = 0;
        }
    }
    return b;
}

/* One byte on the bus with CS asserted and the pads driven. */
static uint8_t card_xfer(uint8_t mosi, uint32_t clkdiv)
{
    uint8_t miso;
    int     busy_now = 0;

    if (c.q_len > 0u) {
        miso = q_pop();
    } else if (c.busy > 0u) {
        c.busy--;
        miso = 0x00u;
        busy_now = 1;
    } else if (c.rd_active) {
        miso = rd_next();
    } else {
        miso = 0xFFu;
    }

    if (busy_now) {
        if (mosi != 0xFFu) {
            s_busy_violations++;
        }
        return miso;
    }

    if (c.wr == WR_DATA) {
        c.wr_buf[c.wr_idx++] = mosi;
        if (c.wr_idx == BLK + 2u) {
            if (c.reject_at != 0u && --c.reject_at == 0u) {
                q_push(0xEDu);                /* write error (xxx01101), nothing stored */
            } else {
                store_write(c.wr_lba, c.wr_buf);
                q_push(0xE5u);                /* data accepted (xxx00101) */
            }
            c.wr_lba++;
            c.busy = c.write_busy_cfg;
            c.wr = c.wr_multi ? WR_WAIT_TOKEN : WR_NONE;
        }
        return miso;
    }
    if (c.wr == WR_WAIT_TOKEN) {
        if (mosi == (c.wr_multi ? 0xFCu : 0xFEu)) {
            c.wr = WR_DATA;
            c.wr_idx = 0;
        } else if (c.wr_multi && mosi == 0xFDu) {
            s_stop_tokens++;
            c.wr = WR_NONE;
            q_push(0xFFu);                    /* Nbr */
            c.busy = c.write_busy_cfg;
        } else if (mosi != 0xFFu) {
            s_protocol_errors++;
        }
        return miso;
    }

    if (c.cmd_len == 0u) {
        if ((mosi & 0xC0u) == 0x40u) {
            c.cmd[c.cmd_len++] = mosi;
        }
    } else {
        c.cmd[c.cmd_len++] = mosi;
        if (c.cmd_len == 6u) {
            c.cmd_len = 0;
            card_exec(clkdiv);
        }
    }
    return miso;
}

/* One byte clocked with CS deasserted: the card ignores MOSI and leaves MISO
 * to the pull-up; time still passes for a busy card. */
static void card_idle_clock(void)
{
    if (c.busy > 0u) {
        c.busy--;
    }
    c.cmd_len = 0;
    c.q_head = c.q_len = 0;
    if (!c.seen_cmd0) {
        c.pre_clocks += 8u;
    }
}

/* ======================= the usd_spi block ================================ */

static struct {
    uint32_t id;
    uint32_t ctrl, clkdiv, rx;
    uint32_t sticky;          /* CD_CHANGED | OVR | ABORT */
    int      busy_reads_cfg;  /* -1 = stuck */
    int      busy_left;
    int      ever_shifted;
    /* card detect */
    int      card_in;         /* physically in the slot */
    int      pin_inverted;    /* board wires NCD high-when-present */
    int      deb_pin;         /* debounced NCD pin level (1 = high = empty, assumed) */
    int      last_present;
    uint32_t pin_change_ms;
    uint32_t debounce_ms;
    uint32_t remove_after;    /* 0 = disarmed */
    int      remove_armed;
    /* counters */
    uint32_t page_writes, data_writes, en_writes, status_reads, ovr_count;
    int      pads_ever;
    uint32_t poll_bytes, poll_bytes_slow, poll_shifts;
    uint64_t poll_wire_ns;
} u;

static int pin_level(void)                    /* raw NCD: low when a card is in (assumed) */
{
    return (u.card_in ? 0 : 1) ^ u.pin_inverted;
}

/* STATUS.CD_PRESENT. As L1's usd_spi.sv (2026-09-23) builds it, CD_IGNORE
 * forces it to 1; the driver does not rely on that either way. */
static int cd_present(void)
{
    int pol = (u.ctrl & USD_CTRL_CD_POL) != 0u;
    if ((u.ctrl & USD_CTRL_CD_IGNORE) != 0u) {
        return 1;
    }
    return pol ? (u.deb_pin == 1) : (u.deb_pin == 0);
}

static int pads_driven(void)
{
    return (u.ctrl & USD_CTRL_EN) != 0u && cd_present();
}

/* Run the debouncer up to now and raise CD_CHANGED / ABORT as the RTL would. */
static void cd_update(void)
{
    int was_driven = pads_driven();
    int pin = pin_level();
    int now_present;

    if (pin != u.deb_pin &&
        (uint32_t)(mock_time_now_ms() - u.pin_change_ms) >= u.debounce_ms) {
        u.deb_pin = pin;
    }
    now_present = cd_present();
    if (now_present != u.last_present) {
        u.last_present = now_present;
        u.sticky |= USD_STATUS_CD_CHANGED;
    }
    if (was_driven && !pads_driven() && u.busy_left != 0) {
        u.sticky |= USD_STATUS_ABORT;
        u.busy_left = 0;
    }
}

static void card_leave(void)
{
    u.card_in = 0;
    u.pin_change_ms = mock_time_now_ms();
    card_power_on();                          /* power loss: protocol state gone */
}

static void do_shift(uint32_t val)
{
    unsigned nbytes = (u.ctrl & USD_CTRL_WIDE) ? 4u : 1u;
    uint32_t rx = 0;
    int      aborted = 0;

    if (!pads_driven()) {
        /* As usd_spi.sv's start_off: pads gated, so no shift at all; ABORT
         * sets, BUSY stays 0, DATA keeps its last value. */
        u.sticky |= USD_STATUS_ABORT;
        u.busy_left = 0;
        return;
    }

    for (unsigned i = 0; i < nbytes; i++) {
        uint8_t mosi = (nbytes == 4u) ? (uint8_t)(val >> (24u - 8u * i)) : (uint8_t)val;
        uint8_t miso = 0xFFu;

        if (u.remove_armed) {
            if (u.remove_after == 0u) {
                int was = pads_driven();
                u.remove_armed = 0;
                card_leave();
                cd_update();
                if (was && !pads_driven()) {
                    aborted = 1;
                }
            } else {
                u.remove_after--;
            }
        }
        if (pads_driven()) {
            u.pads_ever = 1;
            if (u.card_in) {
                if ((u.ctrl & USD_CTRL_CS) != 0u) {
                    miso = card_xfer(mosi, u.clkdiv);
                } else {
                    card_idle_clock();
                }
            }
        }
        rx = (rx << 8) | miso;
        u.poll_bytes++;
        if (u.clkdiv >= USD_CLKDIV_400K) {
            u.poll_bytes_slow++;
        }
        u.poll_wire_ns += 8ull * 2ull * (uint64_t)(u.clkdiv + 1u) * 10ull;
    }
    u.rx = rx;
    u.poll_shifts++;
    u.ever_shifted = 1;
    if (aborted) {
        u.sticky |= USD_STATUS_ABORT;
        u.busy_left = 0;
    } else {
        u.busy_left = u.busy_reads_cfg;
    }
}

static uint32_t status_value(void)
{
    uint32_t v = u.sticky;
    if (u.busy_reads_cfg < 0 && u.ever_shifted) {
        v |= USD_STATUS_BUSY;                 /* stuck */
    } else if (u.busy_left > 0) {
        v |= USD_STATUS_BUSY;
        u.busy_left--;
    }
    if (cd_present()) {
        v |= USD_STATUS_CD_PRESENT;
    }
    if (pin_level()) {
        v |= USD_STATUS_CD_RAW;
    }
    return v;
}

static int hook(void *ctx, int is_write, uint32_t base, uint32_t off, uint32_t *val)
{
    (void)ctx;
    (void)base;
    cd_update();
    if (is_write) {
        u.page_writes++;
        switch (off) {
        case USD_CTRL: {
            int was = pads_driven();
            u.ctrl = *val & 0x1Fu;
            if ((u.ctrl & USD_CTRL_EN) != 0u) {
                u.en_writes++;
            }
            cd_update();
            if (was && !pads_driven() && u.busy_left != 0) {
                u.sticky |= USD_STATUS_ABORT;
                u.busy_left = 0;
            }
            if (pads_driven()) {
                u.pads_ever = 1;
            }
            break;
        }
        case USD_CLKDIV:
            u.clkdiv = *val & USD_CLKDIV_MASK;
            break;
        case USD_DATA:
            u.data_writes++;
            if (u.busy_left != 0 || (u.busy_reads_cfg < 0 && u.ever_shifted)) {
                u.sticky |= USD_STATUS_OVR;   /* ignored */
                u.ovr_count++;
            } else {
                do_shift(*val);
            }
            break;
        case USD_STATUS:
            u.sticky &= ~(*val & USD_STATUS_W1C_MASK);
            break;
        default:
            break;
        }
        return 1;
    }
    switch (off) {
    case USD_ID:     *val = u.id; break;
    case USD_CTRL:   *val = u.ctrl; break;
    case USD_CLKDIV: *val = u.clkdiv; break;
    case USD_DATA:   *val = u.rx; break;
    case USD_STATUS: u.status_reads++; *val = status_value(); break;
    default:         *val = 0; break;
    }
    return 1;
}

/* ======================= control / observation =========================== */

void fake_usd_reset(void)
{
    memset(&c, 0, sizeof c);
    memset(&u, 0, sizeof u);
    memset(s_store, 0, sizeof s_store);
    memset(s_cmd_count, 0, sizeof s_cmd_count);
    memset(s_cmd_div, 0, sizeof s_cmd_div);
    memset(s_cmd_frame, 0, sizeof s_cmd_frame);
    s_crc_errors = s_crc_bad_any = s_frame_errors = s_busy_violations = s_protocol_errors = 0;
    s_stop_tokens = s_blocks_written = s_store_overflows = 0;

    c.read_nac_cfg = 2u;
    c.ncr_cfg = 1u;
    c.c_size = FAKE_USD_C_SIZE_DEFAULT;
    card_power_on();

    u.id = USD_ID_VALUE;
    u.clkdiv = USD_CLKDIV_400K;               /* reset value */
    u.busy_reads_cfg = 1;
    u.deb_pin = 1;                            /* empty slot */
    u.last_present = 0;
    mock_regs_set_hook(MPS3_USD_BASE, hook, 0);
}

void fake_usd_insert(void)
{
    if (!u.card_in) {
        u.card_in = 1;
        u.pin_change_ms = mock_time_now_ms();
        card_power_on();
    }
    cd_update();
}

void fake_usd_remove(void)
{
    if (u.card_in) {
        card_leave();
    }
    cd_update();
}

void fake_usd_tick(void)
{
    cd_update();
}

void fake_usd_power_cycle(void)
{
    int card_in = u.card_in, inverted = u.pin_inverted;
    uint32_t deb = u.debounce_ms;
    uint32_t id = u.id;
    int busy_cfg = u.busy_reads_cfg;

    /* The block: power-on values (EN=0, divider at 400 kHz, no sticky bits),
     * with the physical slot and the board wiring unchanged. The debouncer has
     * settled on whatever is in the slot. */
    memset(&u, 0, sizeof u);
    u.id = id;
    u.busy_reads_cfg = busy_cfg;
    u.clkdiv = USD_CLKDIV_400K;
    u.card_in = card_in;
    u.pin_inverted = inverted;
    u.debounce_ms = deb;
    u.deb_pin = pin_level();
    u.last_present = cd_present();
    u.pin_change_ms = mock_time_now_ms();
    /* The card: power lost and restored; its flash (s_store) survives. */
    card_power_on();
    mock_regs_set_hook(MPS3_USD_BASE, hook, 0);
}

void fake_usd_remove_after_bytes(uint32_t n)
{
    u.remove_armed = (n != 0u);
    u.remove_after = n;
}

void fake_usd_set_debounce_ms(uint32_t ms)     { u.debounce_ms = ms; }
void fake_usd_set_sdsc(int on)                 { c.sdsc = on; }
void fake_usd_set_v1(int on)                   { c.v1 = on; }
void fake_usd_set_acmd41_busy(uint32_t n)      { c.acmd41_busy_cfg = n; c.acmd41_left = n; }
void fake_usd_set_write_busy(uint32_t nbytes)  { c.write_busy_cfg = nbytes; }
void fake_usd_set_read_nac(uint32_t nbytes)    { c.read_nac_cfg = nbytes; }
void fake_usd_set_ncr(uint32_t nbytes)         { c.ncr_cfg = nbytes > 8u ? 8u : nbytes; }
void fake_usd_set_unresponsive(int on)         { c.unresponsive = on; }
void fake_usd_set_c_size(uint32_t c_size)      { c.c_size = c_size; }
void fake_usd_reject_write_block(uint32_t n)   { c.reject_at = n; }
void fake_usd_set_id(uint32_t id)              { u.id = id; }
void fake_usd_set_busy_reads(int n)            { u.busy_reads_cfg = n; }

void fake_usd_set_pin_inverted(int on)
{
    u.pin_inverted = on ? 1 : 0;
    u.deb_pin = pin_level();                  /* a board property: no event */
}

void fake_usd_jtag_ctrl_set(uint32_t bits)
{
    u.ctrl |= bits & 0x1Fu;                   /* an xsdb mwr: bypasses the driver */
    cd_update();
}

uint32_t fake_usd_page_writes(void)        { return u.page_writes; }
uint32_t fake_usd_data_writes(void)        { return u.data_writes; }
uint32_t fake_usd_en_writes(void)          { return u.en_writes; }
int      fake_usd_pads_ever_driven(void)   { return u.pads_ever; }
uint32_t fake_usd_ctrl(void)               { return u.ctrl; }
uint32_t fake_usd_clkdiv(void)             { return u.clkdiv; }
uint32_t fake_usd_status_reads(void)       { return u.status_reads; }
uint32_t fake_usd_ovr_count(void)          { return u.ovr_count; }
uint32_t fake_usd_crc_errors(void)         { return s_crc_errors; }
uint32_t fake_usd_crc_bad_any(void)        { return s_crc_bad_any; }
uint32_t fake_usd_frame_errors(void)       { return s_frame_errors; }
uint32_t fake_usd_busy_violations(void)    { return s_busy_violations; }
uint32_t fake_usd_protocol_errors(void)    { return s_protocol_errors; }
uint32_t fake_usd_cmd_count(unsigned idx)  { return idx < 64u ? s_cmd_count[idx] : 0u; }
uint32_t fake_usd_cmd_clkdiv(unsigned idx) { return idx < 64u ? s_cmd_div[idx] : 0u; }
const uint8_t *fake_usd_cmd_frame(unsigned idx) { return s_cmd_frame[idx & 63u]; }
uint32_t fake_usd_clocks_before_cmd0(void) { return c.pre_clocks; }
uint32_t fake_usd_stop_tokens(void)        { return s_stop_tokens; }
uint32_t fake_usd_blocks_written(void)     { return s_blocks_written; }
uint32_t fake_usd_store_overflows(void)    { return s_store_overflows; }

void fake_usd_poll_begin(void)
{
    u.poll_bytes = 0;
    u.poll_bytes_slow = 0;
    u.poll_shifts = 0;
    u.poll_wire_ns = 0;
}
uint32_t fake_usd_poll_bytes(void)      { return u.poll_bytes; }
uint32_t fake_usd_poll_bytes_slow(void) { return u.poll_bytes_slow; }
uint32_t fake_usd_poll_shifts(void)     { return u.poll_shifts; }
uint64_t fake_usd_poll_wire_ns(void)    { return u.poll_wire_ns; }
