/* mock_clcd.c — see mock_clcd.h. */
#include <string.h>

#include "mock_clcd.h"
#include "../clcd_regs.h"

void mock_clcd_init(mock_clcd_t *m)
{
    memset(m, 0, sizeof(*m));
    m->x1 = 319; m->y1 = 239;
    m->auto_drain = 16;
}

uint32_t mock_clcd_read32(void *ctx, uint32_t off)
{
    mock_clcd_t *m = (mock_clcd_t *)ctx;
    switch (off) {
    case CLCD_CTRL:   return m->ctrl;
    case CLCD_TIMING: return m->timing;
    case CLCD_STATUS: {
        mock_clcd_drain(m, m->auto_drain);   /* concurrent panel drain */
        uint32_t st = 0;
        if (m->level >= MOCK_FIFO_DEPTH) st |= CLCD_STATUS_FIFO_FULL;
        if (m->level == 0)               st |= CLCD_STATUS_FIFO_EMPTY;
        st |= (m->level & 0xFFu) << CLCD_STATUS_LEVEL_SHIFT;
        return st;
    }
    case CLCD_READ:   return 0;   /* READ_PATH=0 on every shipped shell */
    default:          return 0;   /* reads have no side effects, any offset */
    }
}

static void push(mock_clcd_t *m, int rs, uint8_t v)
{
    if (m->level >= MOCK_FIFO_DEPTH) {
        m->drops++;               /* the block's drop policy — must not fire */
        return;
    }
    if (m->len < MOCK_STREAM_MAX) {
        m->rs[m->len]  = (uint8_t)rs;
        m->val[m->len] = v;
        m->len++;
    }
    m->level++;
}

void mock_clcd_write32(void *ctx, uint32_t off, uint32_t v)
{
    mock_clcd_t *m = (mock_clcd_t *)ctx;
    switch (off) {
    case CLCD_CTRL:
        m->ctrl = v & ~CLCD_CTRL_FIFO_RESET;   /* self-clearing bit */
        m->ctrl_writes++;
        if (v & CLCD_CTRL_FIFO_RESET) {
            /* flush queued-but-undrained entries */
            m->len = m->drained;
            m->level = 0;
        }
        break;
    case CLCD_TIMING:
        m->timing = v;
        m->timing_writes++;
        break;
    case CLCD_CMD:  push(m, 0, (uint8_t)v); break;
    case CLCD_DATA: push(m, 1, (uint8_t)v); break;
    default: break;
    }
}

/* ---- HX8347-D model ------------------------------------------------------- */
static void model_byte(mock_clcd_t *m, int rs, uint8_t v)
{
    if (rs == 0) {
        m->reg_index = v;
        m->in_ramwr = (v == 0x22u);
        m->px_phase = 0;
        if (m->in_ramwr) {
            /* latch window from raw regs; cursor to window origin */
            m->x0 = ((unsigned)m->winreg[0x02] << 8) | m->winreg[0x03];
            m->x1 = ((unsigned)m->winreg[0x04] << 8) | m->winreg[0x05];
            m->y0 = ((unsigned)m->winreg[0x06] << 8) | m->winreg[0x07];
            m->y1 = ((unsigned)m->winreg[0x08] << 8) | m->winreg[0x09];
            m->cx = m->x0;
            m->cy = m->y0;
        }
        return;
    }
    /* data byte */
    if (m->in_ramwr) {
        if (m->px_phase == 0) {
            m->px_hi = v;
            m->px_phase = 1;
        } else {
            uint16_t px = ((uint16_t)m->px_hi << 8) | v;   /* MSB first */
            m->px_phase = 0;
            if (m->cx < 320 && m->cy < 240)
                m->fb[m->cy][m->cx] = px;
            if (m->cx >= m->x1) {          /* auto-increment within window */
                m->cx = m->x0;
                if (m->cy >= m->y1)
                    m->cy = m->y0;         /* wrap (never hit in one cell) */
                else
                    m->cy++;
            } else {
                m->cx++;
            }
        }
    } else if (m->reg_index <= 0x09u) {
        m->winreg[m->reg_index] = v;
    }
    /* other registers: init-table config, no model needed */
}

void mock_clcd_drain(mock_clcd_t *m, unsigned n)
{
    while (n && m->level) {
        model_byte(m, m->rs[m->drained], m->val[m->drained]);
        m->drained++;
        m->level--;
        n--;
    }
}
