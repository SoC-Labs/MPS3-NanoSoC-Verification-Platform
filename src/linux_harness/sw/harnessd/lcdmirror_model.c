/*
 * lcdmirror_model.c -- the HX8347-D GRAM model: a C port of LCD_MIRROR_FPGA.md
 * §2.3-2.7, the same spec the mint-4 snooper implements, writing the snooper's
 * aperture layout (lcdmirror.h). Pure: no I/O, no clock, no allocation, so the
 * host tests and harnessd run the identical code.
 *
 * THE GOLDEN MODEL is tests/lcd_mirror/hx8347_gram_model.py (lane LCDMIR-RTL);
 * this file follows it line for line and harnessd/tests/test_lcdmirror_e2e.py
 * replays the shared vectors through both (lcdmirror_replay) and compares every
 * model-determined CSR and every frame-buffer word:
 *   index byte B    idx <- B; the pixel byte phase restarts (a half pixel is
 *                   discarded). B == 0x22 counts RAMWR and, with CTRL.ac_load=1
 *                   (O1), loads AC <- (SC, SP).
 *   data, idx!=22   REGS[idx] <- D (the raw log, all 256 indices, NEVER reset);
 *                   decode 02/03 SC, 04/05 EC, 06/07 SP, 08/09 EP (9 bits: the
 *                   high register's bit 0 is bit 8), 16 MADCTL, 17 COLMOD and the
 *                   recorded 01/1F/28/36; with ac_load=0 a write to 02/03 loads
 *                   AC.x <- SC and 06/07 loads AC.y <- SP.
 *   data, idx==22   COLMOD[2:0] == 6: three bytes (6 bits each in D[7:2])
 *                   truncated to RGB565; anything else: two bytes, high first.
 *                   Write it at AC, then advance: x runs SC..EC then back to SC
 *                   with y+1; after EP, y back to SP and FRAMES++ (EQUALITY
 *                   tests with a 9-bit wrap: a start beyond the end walks
 *                   through 511).
 *   viewer          MV ? (g0,s0)=(x,y) : (y,x); flip_conv 0: MY flips g, MX
 *                   flips s; flip_conv 1 (logical axes): with MV set MX flips g,
 *                   MY flips s. g0 > 319 or s0 > 239 = OOB (counted, sticky, not
 *                   written, not in SEQ). vx = g, vy = s.
 *   CLCD_RST        the decoded fields return to the O3 DEFAULTS (portrait
 *                   window 0..239 x 0..319, MADCTL 0, COLMOD 6, R1F 1 = STB,
 *                   R28 0, R36 0, R01 0, idx 0, AC (0,0)); VALID is cleared;
 *                   RESETS++; FB pixels, the REGS log, DIRTY and the counters
 *                   are kept. While the pad is HELD (the tap knows the level)
 *                   bytes are ignored, as the panel ignores them.
 */
#include <string.h>

#include "lcdmirror.h"

#define AP(off) (m->ap[(off) / 4u])

static void geometry(lcdm_model_t *m)
{
    int mv = (m->r16 >> 5) & 1, mx = (m->r16 >> 6) & 1, my = (m->r16 >> 7) & 1;
    m->mv = (uint8_t)mv;
    if (mv && (m->ctrl & LCDM_CTRL_FLIP_CONV)) {
        m->flip_g = (uint8_t)mx;
        m->flip_s = (uint8_t)my;
    } else {
        m->flip_g = (uint8_t)my;
        m->flip_s = (uint8_t)mx;
    }
}

static void decoded_defaults(lcdm_model_t *m)
{
    m->idx = 0;
    m->nb = 0;
    m->sc = 0;  m->ec = 239;  m->sp = 0;  m->ep = 319;
    m->r01 = 0x00; m->r16 = 0x00; m->r17 = 0x06; m->r1f = 0x01; m->r28 = 0x00; m->r36 = 0x00;
    m->acx = 0;
    m->acy = 0;
    geometry(m);
    m->changed = 1;
}

void lcdm_model_init(lcdm_model_t *m, volatile uint32_t *ap, int keep_fb)
{
    memset(m, 0, sizeof(*m));
    m->ap = ap;
    m->fb = (volatile uint16_t *)((volatile uint8_t *)ap + LCDM_FB);
    m->live = LCDM_ST_RST_N;
    m->regs_dirty = 1;               /* the zero log goes out on the first publish */
    decoded_defaults(m);
    AP(LCDM_ID) = LCDM_ID_VALUE;
    AP(LCDM_VERSION) = LCDM_VERSION_VALUE;
    AP(LCDM_GEOM) = LCDM_GEOM_VALUE;
    AP(LCDM_VIOL) = 0u;
    AP(LCDM_RDS) = 0u;
    AP(LCDM_TMIN) = (1u << 16) | (2u << 8) | 2u;   /* the reset value; inert in sw */
    if (!keep_fb) {
        for (unsigned i = 0; i < LCDM_NPX / 2u; i++) {
            AP(LCDM_FB + 4u * i) = 0u;
        }
    }
    lcdm_model_publish(m);
}

void lcdm_model_set_reset(lcdm_model_t *m, int held)
{
    if (held && !m->in_reset) {
        m->resets++;
        decoded_defaults(m);
        memset(m->valid, 0, sizeof(m->valid));
    }
    m->in_reset = held ? 1u : 0u;
    if (held) {
        m->live &= ~LCDM_ST_RST_N;
    } else {
        m->live |= LCDM_ST_RST_N;
    }
    m->changed = 1;
}

void lcdm_model_pulse_reset(lcdm_model_t *m)
{
    lcdm_model_set_reset(m, 1);
    lcdm_model_set_reset(m, 0);
}

void lcdm_model_set_live(lcdm_model_t *m, uint32_t mask, uint32_t bits)
{
    mask &= LCDM_ST_BL | LCDM_ST_OWNER;     /* RST_N follows set_reset() */
    uint32_t nv = (m->live & ~mask) | (bits & mask);
    if (nv != m->live) {
        m->live = nv;
        m->changed = 1;
    }
}

void lcdm_model_invalidate(lcdm_model_t *m)
{
    memset(m->valid, 0, sizeof(m->valid));
    m->changed = 1;
}

void lcdm_model_set_ctrl(lcdm_model_t *m, uint32_t ctrl)
{
    if (ctrl & LCDM_CTRL_CLR_STICKY) {
        m->sticky = 0;
    }
    if (ctrl & LCDM_CTRL_CLR_COUNTS) {
        m->seq = m->frames = m->ramwr = m->resets = m->bytes = m->oob = 0;
    }
    m->ctrl = ctrl & (LCDM_CTRL_AC_LOAD | LCDM_CTRL_FLIP_CONV);
    geometry(m);
    m->changed = 1;
}

static uint16_t hi9(uint16_t cur, uint8_t d) { return (uint16_t)(((d & 1u) << 8) | (cur & 0xFFu)); }
static uint16_t lo9(uint16_t cur, uint8_t d) { return (uint16_t)((cur & 0x100u) | d); }

static void reg_write(lcdm_model_t *m, uint8_t idx, uint8_t d)
{
    int ac = !(m->ctrl & LCDM_CTRL_AC_LOAD);
    m->regs[idx] = d;
    m->regs_dirty = 1;
    switch (idx) {
    case 0x01: m->r01 = d; break;
    case 0x02: m->sc = hi9(m->sc, d); if (ac) m->acx = m->sc; break;
    case 0x03: m->sc = lo9(m->sc, d); if (ac) m->acx = m->sc; break;
    case 0x04: m->ec = hi9(m->ec, d); break;
    case 0x05: m->ec = lo9(m->ec, d); break;
    case 0x06: m->sp = hi9(m->sp, d); if (ac) m->acy = m->sp; break;
    case 0x07: m->sp = lo9(m->sp, d); if (ac) m->acy = m->sp; break;
    case 0x08: m->ep = hi9(m->ep, d); break;
    case 0x09: m->ep = lo9(m->ep, d); break;
    case 0x16: m->r16 = d; geometry(m); break;
    case 0x17: m->r17 = d; break;
    case 0x1F: m->r1f = d; break;
    case 0x28: m->r28 = d; break;
    case 0x36: m->r36 = d; break;
    default: break;
    }
}

static void put_pixel(lcdm_model_t *m, uint16_t px)
{
    unsigned x = m->acx, y = m->acy;
    unsigned g0 = m->mv ? x : y;
    unsigned s0 = m->mv ? y : x;
    if (g0 < LCDM_W && s0 < LCDM_H) {
        unsigned vx = m->flip_g ? (LCDM_W - 1u - g0) : g0;
        unsigned vy = m->flip_s ? (LCDM_H - 1u - s0) : s0;
        unsigned t = (vy >> 4) * LCDM_TX + (vx >> 4);
        uint32_t bit = 1u << (t & 31u);
        m->fb[vy * LCDM_W + vx] = px;
        m->pend[t >> 5] |= bit;
        m->valid[t >> 5] |= bit;
        m->seq++;
    } else {
        m->oob++;
        m->sticky |= LCDM_ST_OOB;
    }
    if (x == m->ec) {
        m->acx = m->sc;
        if (y == m->ep) {
            m->acy = m->sp;
            m->frames++;
        } else {
            m->acy = (uint16_t)((y + 1u) & 0x1FFu);
        }
    } else {
        m->acx = (uint16_t)((x + 1u) & 0x1FFu);
    }
}

void lcdm_model_byte(lcdm_model_t *m, int rs, uint8_t b)
{
    m->bytes++;
    m->changed = 1;
    if (m->in_reset) {
        return;                       /* the panel ignores the bus in reset */
    }
    if (!rs) {
        m->idx = b;
        m->nb = 0;
        if (b == 0x22u) {
            m->ramwr++;
            if (m->ctrl & LCDM_CTRL_AC_LOAD) {
                m->acx = m->sc;
                m->acy = m->sp;
            }
        }
        return;
    }
    if (m->idx != 0x22u) {
        reg_write(m, m->idx, b);
        return;
    }
    m->pb[m->nb++] = b;
    uint16_t px;
    if ((m->r17 & 7u) == 6u) {
        if (m->nb < 3u) return;
        px = (uint16_t)(((unsigned)(m->pb[0] >> 3) << 11) | ((unsigned)(m->pb[1] >> 2) << 5) |
                        (unsigned)(m->pb[2] >> 3));
    } else {
        if (m->nb < 2u) return;
        px = (uint16_t)((unsigned)m->pb[0] << 8 | m->pb[1]);
    }
    m->nb = 0;
    put_pixel(m, px);
}

/* put_pixel() over `npx` 16 bpp pixels (2 bytes each, high first) with the
 * address counter, SEQ/FRAMES/OOB and the window in registers: per pixel one
 * frame-buffer store, and the two dirty maps touched only when the pixel lands
 * in a different tile from the last one (ORing the same bit again is a no-op,
 * and nothing clears PEND/VALID inside a call). Bit-identical to calling
 * put_pixel() npx times -- test_lcdmirror.c's differential check. */
static void pixel_run(lcdm_model_t *m, const uint8_t *p, unsigned npx)
{
    unsigned x = m->acx, y = m->acy;
    const unsigned sc = m->sc, ec = m->ec, sp = m->sp, ep = m->ep;
    const int mv = m->mv, fg = m->flip_g, fs = m->flip_s;
    uint32_t seq = m->seq, frames = m->frames, oob = m->oob;
    unsigned last_t = ~0u;
    volatile uint16_t *fb = m->fb;
    for (; npx; npx--, p += 2) {
        unsigned g0 = mv ? x : y;
        unsigned s0 = mv ? y : x;
        if (g0 < LCDM_W && s0 < LCDM_H) {
            unsigned vx = fg ? (LCDM_W - 1u - g0) : g0;
            unsigned vy = fs ? (LCDM_H - 1u - s0) : s0;
            unsigned t = (vy >> 4) * LCDM_TX + (vx >> 4);
            /* high byte first; written as a multiply-add because gcc turns the
             * shift-or into a byte-swapped halfword load that rv32imac (no Zbb)
             * then un-swaps in five instructions */
            fb[vy * LCDM_W + vx] = (uint16_t)(p[0] * 256u + p[1]);
            if (t != last_t) {
                uint32_t bit = 1u << (t & 31u);
                m->pend[t >> 5] |= bit;
                m->valid[t >> 5] |= bit;
                last_t = t;
            }
            seq++;
        } else {
            oob++;
            m->sticky |= LCDM_ST_OOB;
        }
        if (x == ec) {
            x = sc;
            if (y == ep) {
                y = sp;
                frames++;
            } else {
                y = (y + 1u) & 0x1FFu;
            }
        } else {
            x = (x + 1u) & 0x1FFu;
        }
    }
    m->acx = (uint16_t)x;
    m->acy = (uint16_t)y;
    m->seq = seq;
    m->frames = frames;
    m->oob = oob;
}

/* lcdm_model_byte() over a run (lcdmirror.h). The run of GRAM DATA at 16 bpp --
 * the whole of every clcd.c glyph after its 17-byte preamble -- is decoded a
 * pixel pair at a time by pixel_run(); every other byte (an index, a register
 * datum, 18 bpp, a panel held in reset, an odd half-pixel state) goes through
 * lcdm_model_byte() itself, so there is one decoder for everything else. */
void lcdm_model_bytes(lcdm_model_t *m, const uint8_t *rs, const uint8_t *b, uint32_t n)
{
    uint32_t i = 0;
    while (i < n) {
        if (!rs[i] || m->idx != 0x22u || m->in_reset || (m->r17 & 7u) == 6u || m->nb > 1u) {
            lcdm_model_byte(m, rs[i], b[i]);
            i++;
            continue;
        }
        const uint8_t *z = memchr(rs + i, 0, n - i);   /* the run ends at an index */
        uint32_t j = z ? (uint32_t)(z - rs) : n;
        uint32_t k = j - i;               /* >= 1 data bytes, all pixel bytes */
        const uint8_t *p = b + i;
        m->bytes += k;
        m->changed = 1;
        if (m->nb) {                      /* the pending high byte + this one */
            uint8_t pair[2] = { m->pb[0], p[0] };
            m->pb[1] = p[0];
            m->nb = 0;
            pixel_run(m, pair, 1u);
            p++;
            k--;
        }
        if (k >= 2u) {
            pixel_run(m, p, k / 2u);
            m->pb[0] = p[k - (k & 1u) - 2u];  /* what lcdm_model_byte leaves */
            m->pb[1] = p[k - (k & 1u) - 1u];
        }
        if (k & 1u) {
            m->pb[0] = p[k - 1u];
            m->nb = 1;
        }
        i = j;
    }
}

uint32_t lcdm_model_status(const lcdm_model_t *m)
{
    uint32_t st = m->live | m->sticky;
    unsigned fmt = m->r17 & 7u;
    if ((m->r28 & 0x3Cu) == 0x3Cu) st |= LCDM_ST_DISPLAY_ON;
    if (m->r1f & 0x01u)            st |= LCDM_ST_STANDBY;
    if (m->idx == 0x22u)           st |= LCDM_ST_IN_GRAM;
    if (fmt == 5u || fmt == 6u)    st |= LCDM_ST_FMT_OK;
    if (fmt == 6u)                 st |= LCDM_ST_APPROX;
    return st;
}

void lcdm_model_publish(lcdm_model_t *m)
{
    int any = 0;
    for (unsigned w = 0; w < LCDM_MAP_WORDS; w++) {
        any |= m->pend[w] != 0u;
    }
    if (!m->changed && !any) {
        return;
    }
    uint32_t s = AP(LCDM_SW_PUBSEQ);
    AP(LCDM_SW_PUBSEQ) = s | 1u;                 /* odd: a reader retries */
    __atomic_thread_fence(__ATOMIC_RELEASE);
    AP(LCDM_CTRL)    = m->ctrl;
    AP(LCDM_STATUS)  = lcdm_model_status(m);
    AP(LCDM_SEQ)     = m->seq;
    AP(LCDM_FRAMES)  = m->frames;
    AP(LCDM_RAMWR)   = m->ramwr;
    AP(LCDM_RESETS)  = m->resets;
    AP(LCDM_BYTES)   = m->bytes;
    AP(LCDM_OOB)     = m->oob;
    AP(LCDM_WIN_X)   = ((uint32_t)m->ec << 16) | m->sc;
    AP(LCDM_WIN_Y)   = ((uint32_t)m->ep << 16) | m->sp;
    AP(LCDM_AC)      = ((uint32_t)m->acy << 16) | m->acx;
    AP(LCDM_MODE)    = (uint32_t)m->r16 | ((uint32_t)m->r17 << 8) |
                       ((uint32_t)m->r36 << 16) | ((uint32_t)m->r01 << 24);
    for (unsigned w = 0; w < LCDM_MAP_WORDS; w++) {
        AP(LCDM_VALID + 4u * w) = m->valid[w];
    }
    if (m->regs_dirty) {
        for (unsigned i = 0; i < 64u; i++) {
            const uint8_t *r = &m->regs[4u * i];
            AP(LCDM_REGS + 4u * i) = (uint32_t)r[0] | ((uint32_t)r[1] << 8) |
                                     ((uint32_t)r[2] << 16) | ((uint32_t)r[3] << 24);
        }
        m->regs_dirty = 0;
    }
    __atomic_thread_fence(__ATOMIC_RELEASE);
    AP(LCDM_SW_PUBSEQ) = (s | 1u) + 1u;          /* even again */
    for (unsigned w = 0; w < LCDM_MAP_WORDS; w++) {
        if (m->pend[w]) {
            /* RELEASE: every pixel store above is visible before the bit is. */
            __atomic_fetch_or((uint32_t *)&m->ap[LCDM_SW_LIVE / 4u + w], m->pend[w],
                              __ATOMIC_RELEASE);
            m->pend[w] = 0u;
        }
    }
    AP(LCDM_SW_PUBS) = AP(LCDM_SW_PUBS) + 1u;
    m->changed = 0;
}
