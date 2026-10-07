/*
 * ovl_bdev_posix.c -- the POSIX block-device provider for the SD overlay store:
 * pread()/pwrite() on a card image, /dev/mmcblk0 or /dev/mmcblk0p4. See
 * ovl_bdev.h. Hosted builds only (the Linux harness daemon, the host tests).
 *
 * SYNCHRONOUS by design: *_start() does the whole transfer and records the
 * result; io_status() returns it. The store sees "started, then DONE on the
 * next look", which is the async model with a zero-length wait, so the same
 * store code runs on both engines.
 *
 * The loops below are over the kernel's short-read/short-write returns of ONE
 * op (at most ovl_bdev_t.max_blocks blocks), not waits on hardware; the
 * superloop rule of usd.c does not apply to a hosted process.
 */
#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <fcntl.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#include "ovl_bdev.h"

/* A POSIX op may be as large as the caller likes; the store caps its own ops
 * at OVLSTORE_SD_OP_BLOCKS. 128 blocks = 64 KiB is only a sanity ceiling. */
#define POSIX_MAX_BLOCKS 128u

static int px_xfer(ovl_bdev_posix_t *p, bool wr, uint32_t lba, uint32_t n, void *buf)
{
    uint8_t *b = (uint8_t *)buf;
    off_t    off = (off_t)lba * (off_t)OVL_BDEV_BLOCK_SIZE;
    size_t   left = (size_t)n * OVL_BDEV_BLOCK_SIZE;

    while (left > 0u) {
        ssize_t got = wr ? pwrite(p->fd, b, left, off) : pread(p->fd, b, left, off);
        if (got < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -errno;
        }
        if (got == 0) {
            return -EIO;   /* past the end: the range check below should stop this */
        }
        b += got;
        off += got;
        left -= (size_t)got;
    }
    return 0;
}

static int px_start(ovl_bdev_posix_t *p, bool wr, uint32_t lba, uint32_t n, void *buf)
{
    if (!p->present) {
        p->last = -ENODEV;
        return -ENODEV;
    }
    if (buf == NULL || n == 0u || n > POSIX_MAX_BLOCKS ||
        lba >= p->nblocks || n > p->nblocks - lba) {
        return -EINVAL;
    }
    if (wr && !p->writable) {
        return -EROFS;
    }
    if (!wr && p->nocache) {
        /* Read the MEDIA, not the page cache (see ovl_bdev.h). Advisory: an
         * error here only means the read may be served from cache. */
        (void)posix_fadvise(p->fd, (off_t)lba * (off_t)OVL_BDEV_BLOCK_SIZE,
                            (off_t)n * (off_t)OVL_BDEV_BLOCK_SIZE, POSIX_FADV_DONTNEED);
    }
    p->last = px_xfer(p, wr, lba, n, buf);
    return 0;   /* started; the result is io_status()'s */
}

static int bd_read_start(void *ctx, uint32_t lba, uint32_t n, void *buf)
{
    return px_start((ovl_bdev_posix_t *)ctx, false, lba, n, buf);
}

static int bd_write_start(void *ctx, uint32_t lba, uint32_t n, const void *buf)
{
    /* pwrite() takes a const buffer; px_xfer() is shared with pread(). */
    return px_start((ovl_bdev_posix_t *)ctx, true, lba, n, (void *)(uintptr_t)buf);
}

static int bd_io_status(void *ctx)
{
    return ((ovl_bdev_posix_t *)ctx)->last;
}

static uint32_t bd_nblocks(void *ctx)
{
    ovl_bdev_posix_t *p = (ovl_bdev_posix_t *)ctx;
    return p->present ? p->nblocks : 0u;
}

static bool bd_present(void *ctx)
{
    return ((ovl_bdev_posix_t *)ctx)->present;
}

static ovl_bdev_state_t bd_state(void *ctx)
{
    return ((ovl_bdev_posix_t *)ctx)->present ? OVL_BDEV_READY : OVL_BDEV_NONE;
}

static int bd_error_code(void *ctx)
{
    (void)ctx;
    return 0;
}

static uint32_t bd_change_count(void *ctx)
{
    return ((ovl_bdev_posix_t *)ctx)->changes;
}

static const ovl_bdev_ops_t s_ops = {
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

static int px_bind(ovl_bdev_posix_t *p, ovl_bdev_t *bd, int fd, int owns, bool writable)
{
    off_t end = lseek(fd, 0, SEEK_END);   /* works for regular files AND block devices */
    uint64_t blocks;

    if (end < 0) {
        return -errno;
    }
    blocks = (uint64_t)end / OVL_BDEV_BLOCK_SIZE;
    if (blocks > 0xFFFFFFFFull) {
        return -EFBIG;   /* MBR/LBA32: the store addresses at most 2 TiB */
    }
    memset(p, 0, sizeof *p);
    p->fd       = fd;
    p->owns_fd  = owns;
    p->present  = true;
    p->writable = writable;
    p->nblocks  = (uint32_t)blocks;
    bd->ops        = &s_ops;
    bd->ctx        = p;
    bd->max_blocks = POSIX_MAX_BLOCKS;
    return 0;
}

int ovl_bdev_posix_open(ovl_bdev_posix_t *p, ovl_bdev_t *bd, const char *path, unsigned flags)
{
    bool writable = (flags & OVL_BDEV_POSIX_WRITABLE) != 0u;
    int  oflags = writable ? O_RDWR : O_RDONLY;
    int  fd, rc;

    if ((flags & OVL_BDEV_POSIX_DSYNC) != 0u) {
        oflags |= O_DSYNC;
    }
    fd = open(path, oflags);
    if (fd < 0) {
        return -errno;
    }
    rc = px_bind(p, bd, fd, 1, writable);
    if (rc != 0) {
        close(fd);
        return rc;
    }
    p->nocache = (flags & OVL_BDEV_POSIX_NOCACHE) != 0u;
    return 0;
}

int ovl_bdev_posix_attach_fd(ovl_bdev_posix_t *p, ovl_bdev_t *bd, int fd)
{
    int fl = fcntl(fd, F_GETFL);
    if (fl < 0) {
        return -errno;
    }
    return px_bind(p, bd, fd, 0, (fl & O_ACCMODE) != O_RDONLY);
}

void ovl_bdev_posix_close(ovl_bdev_posix_t *p)
{
    if (p->owns_fd && p->fd >= 0) {
        close(p->fd);
    }
    p->fd = -1;
    p->present = false;
}

void ovl_bdev_posix_set_present(ovl_bdev_posix_t *p, bool present)
{
    if (p->present != present) {
        p->present = present;
        p->changes++;
    }
    if (!present) {
        p->last = -ENODEV;
    }
}
