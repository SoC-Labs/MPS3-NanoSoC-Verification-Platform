/*
 * ovl_bdev_usd.c -- the bare-metal block-device provider for the SD overlay
 * store: a thin vtable over firmware/usd/usd.c. See ovl_bdev.h.
 *
 * Nothing here touches a register; usd.c owns the usd_spi page, its two hard
 * rules (no card = no DATA write, no blocking) and its per-poll SPI budget.
 * This file only translates:
 *   usd_state()                         -> ovl_bdev_state_t
 *     ABSENT + USD_ERR_NO_BLOCK         -> NO_HW   (no usd_spi in this fabric)
 *     ABSENT                            -> NONE
 *     SETTLE, INIT                      -> INIT
 *     READY / UNSUPPORTED / ERROR       -> READY / UNSUPPORTED / ERROR
 *   usd_io_status() USD_IO_BUSY/DONE    -> OVL_BDEV_IO_BUSY/DONE (same values)
 */
#include <stddef.h>

#include "ovl_bdev.h"
#include "../usd/usd.h"

_Static_assert(OVL_BDEV_IO_DONE == USD_IO_DONE && OVL_BDEV_IO_BUSY == USD_IO_BUSY,
               "ovl_bdev and usd.c must agree on the io_status values");
_Static_assert(OVL_BDEV_BLOCK_SIZE == USD_BLOCK_SIZE, "block size");

static int bd_read_start(void *ctx, uint32_t lba, uint32_t n, void *buf)
{
    (void)ctx;
    return usd_read_start(lba, n, buf);
}

static int bd_write_start(void *ctx, uint32_t lba, uint32_t n, const void *buf)
{
    (void)ctx;
    return usd_write_start(lba, n, buf);
}

static int bd_io_status(void *ctx)
{
    (void)ctx;
    return usd_io_status();
}

static void bd_poll(void *ctx, uint32_t now_ms)
{
    (void)ctx;
    usd_poll(now_ms);
}

static uint32_t bd_nblocks(void *ctx)
{
    (void)ctx;
    return usd_card_blocks();
}

static bool bd_present(void *ctx)
{
    (void)ctx;
    return usd_present();
}

static ovl_bdev_state_t bd_state(void *ctx)
{
    (void)ctx;
    switch (usd_state()) {
    case USD_ABSENT:
        return (usd_error_code() == USD_ERR_NO_BLOCK) ? OVL_BDEV_NO_HW : OVL_BDEV_NONE;
    case USD_SETTLE:
    case USD_INIT:
        return OVL_BDEV_INIT;
    case USD_READY:
        return OVL_BDEV_READY;
    case USD_UNSUPPORTED:
        return OVL_BDEV_UNSUPPORTED;
    case USD_ERROR:
        return OVL_BDEV_ERROR;
    }
    return OVL_BDEV_ERROR;
}

static int bd_error_code(void *ctx)
{
    (void)ctx;
    return usd_error_code();
}

static uint32_t bd_change_count(void *ctx)
{
    (void)ctx;
    return usd_change_count();
}

/* Two tables so own_poll costs nothing at run time. */
static const ovl_bdev_ops_t s_ops_polled = {
    .read_start   = bd_read_start,
    .write_start  = bd_write_start,
    .io_status    = bd_io_status,
    .poll         = bd_poll,
    .nblocks      = bd_nblocks,
    .present      = bd_present,
    .state        = bd_state,
    .error_code   = bd_error_code,
    .change_count = bd_change_count,
};

static const ovl_bdev_ops_t s_ops_unpolled = {
    .read_start   = bd_read_start,
    .write_start  = bd_write_start,
    .io_status    = bd_io_status,
    .poll         = NULL,
    .nblocks      = bd_nblocks,
    .present      = bd_present,
    .state        = bd_state,
    .error_code   = bd_error_code,
    .change_count = bd_change_count,
};

void ovl_bdev_usd_bind(ovl_bdev_t *bd, bool own_poll)
{
    bd->ops        = own_poll ? &s_ops_polled : &s_ops_unpolled;
    bd->ctx        = NULL;
    bd->max_blocks = USD_MAX_BLOCKS_PER_OP;
}
