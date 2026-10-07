/* auxhw.c — see auxhw.h. Mock behaviours are documented in the header. */
#define _POSIX_C_SOURCE 200809L
#include <string.h>
#include <unistd.h>

#include "auxhw.h"

/* ---- byte FIFO ----------------------------------------------------------- */

static void fifo_push(auxhw_fifo_t *f, uint8_t b)
{
    if (f->count >= AUXHW_FIFO_CAP)
        return; /* mock overflow: drop (RTL drop-on-full policy) */
    f->buf[(f->head + f->count) % AUXHW_FIFO_CAP] = b;
    f->count++;
}

/* returns UARTBR_VALID|byte, or 0 when empty (destructive pop). */
static uint32_t fifo_pop(auxhw_fifo_t *f)
{
    if (f->count == 0)
        return 0;
    uint8_t b = f->buf[f->head];
    f->head = (f->head + 1) % AUXHW_FIFO_CAP;
    f->count--;
    return UARTBR_VALID | b;
}

/* ---- open/close ----------------------------------------------------------- */

int auxhw_open(auxhw_t *hw, auxhw_block_t block, const char *uio_name)
{
    memset(hw, 0, sizeof(*hw));
    hw->block = block;
    if (uio_name == NULL) {
        hw->mock = 1;
        if (block == AUXHW_BLK_CLKRST)
            hw->m.clkrst.reset_ctrl = CLKRST_RESET_CTRL_DUT_RESETN |
                                      CLKRST_RESET_CTRL_RP_RESETN |
                                      CLKRST_RESET_CTRL_DBG_RESETN;
        return 0;
    }
    hw->mock = 0;
    return mps3_uio_open(uio_name, &hw->uio);
}

void auxhw_close(auxhw_t *hw)
{
    if (!hw->mock)
        mps3_uio_close(&hw->uio);
}

/* ---- mock models ----------------------------------------------------------- */

static uint32_t mock_read(auxhw_t *hw, uint32_t off)
{
    switch (hw->block) {
    case AUXHW_BLK_DBGBR:
        switch (off) {
        case DBGBR_LENGTH: return hw->m.dbgbr.length;
        case DBGBR_TMS:    return hw->m.dbgbr.tms;
        case DBGBR_TDI:    return hw->m.dbgbr.tdi;
        case DBGBR_TDO:    return hw->m.dbgbr.tdo;
        case DBGBR_CTRL:   return 0; /* GO completed instantly (self-clear) */
        }
        return 0;
    case AUXHW_BLK_SWDBB:
        if (off == SWDBB_DRIVE)
            return hw->m.swdbb.drive;
        if (off == SWDBB_SAMPLE) {
            /* swdio_i = driven level while OE, else pull-up idle high */
            if (hw->m.swdbb.drive & SWDBB_DRIVE_SWDIO_OE)
                return (hw->m.swdbb.drive & SWDBB_DRIVE_SWDIO_O) ? 1u : 0u;
            return 1u;
        }
        return 0;
    case AUXHW_BLK_CLKRST:
        if (off == CLKRST_RESET_CTRL)
            return hw->m.clkrst.reset_ctrl;
        return 0;
    case AUXHW_BLK_UARTBR:
        switch (off) {
        case UARTBR_U0_TXRX: return fifo_pop(&hw->m.uartbr.u0);
        case UARTBR_U1_TXRX: return fifo_pop(&hw->m.uartbr.u1);
        case UARTBR_SWO_RX:  return fifo_pop(&hw->m.uartbr.swo);
        case UARTBR_SWO_CFG: return hw->m.uartbr.swo_cfg;
        case UARTBR_FIFO_STATUS: {
            uint32_t s = 0; /* loopback FIFOs are deep: never tx_full */
            if (hw->m.uartbr.u0.count == 0)  s |= UARTBR_FIFO_STATUS_U0_RX_EMPTY;
            if (hw->m.uartbr.u1.count == 0)  s |= UARTBR_FIFO_STATUS_U1_RX_EMPTY;
            if (hw->m.uartbr.swo.count == 0) s |= UARTBR_FIFO_STATUS_SWO_RX_EMPTY;
            return s;
        }
        }
        return 0;
    }
    return 0;
}

static void mock_write(auxhw_t *hw, uint32_t off, uint32_t v)
{
    switch (hw->block) {
    case AUXHW_BLK_DBGBR:
        switch (off) {
        case DBGBR_LENGTH: hw->m.dbgbr.length = v; break;
        case DBGBR_TMS:    hw->m.dbgbr.tms = v;    break;
        case DBGBR_TDI:    hw->m.dbgbr.tdi = v;    break;
        case DBGBR_CTRL:
            if (v & DBGBR_CTRL_GO) {
                uint32_t n = hw->m.dbgbr.length;
                if (n == 0 || n > 32u)
                    n = 32u;
                uint32_t mask = (n == 32u) ? 0xFFFFFFFFu : ((1u << n) - 1u);
                hw->m.dbgbr.tdo = hw->m.dbgbr.tdi & mask;
            }
            break;
        }
        break;
    case AUXHW_BLK_SWDBB:
        if (off == SWDBB_DRIVE)
            hw->m.swdbb.drive = v & 0x7u;
        break;
    case AUXHW_BLK_CLKRST:
        if (off == CLKRST_RESET_CTRL)
            hw->m.clkrst.reset_ctrl = v & 0x7u;
        break;
    case AUXHW_BLK_UARTBR:
        switch (off) {
        case UARTBR_U0_TXRX: fifo_push(&hw->m.uartbr.u0, (uint8_t)(v & 0xFF)); break;
        case UARTBR_U1_TXRX: fifo_push(&hw->m.uartbr.u1, (uint8_t)(v & 0xFF)); break;
        case UARTBR_SWO_CFG:
            hw->m.uartbr.swo_cfg = v;
            if ((v & UARTBR_SWO_CFG_ENABLE) && !hw->m.uartbr.swo_banner_queued) {
                static const char banner[] = "SWO-OK\n";
                for (const char *p = banner; *p; p++)
                    fifo_push(&hw->m.uartbr.swo, (uint8_t)*p);
                hw->m.uartbr.swo_banner_queued = 1;
            }
            break;
        }
        break;
    }
}

/* ---- dispatch -------------------------------------------------------------- */

uint32_t auxhw_read32(auxhw_t *hw, uint32_t off)
{
    if (hw->mock)
        return mock_read(hw, off);
    return mps3_uio_read32(&hw->uio, off);
}

void auxhw_write32(auxhw_t *hw, uint32_t off, uint32_t v)
{
    if (hw->mock)
        mock_write(hw, off, v);
    else
        mps3_uio_write32(&hw->uio, off, v);
}

/* ---- swap gate -------------------------------------------------------------- */

int mps3_gate_active(const char *path)
{
    if (!path || !path[0])
        return 0;
    return access(path, F_OK) == 0;
}
