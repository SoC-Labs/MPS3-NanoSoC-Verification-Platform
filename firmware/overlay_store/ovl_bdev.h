/*
 * ovl_bdev.h -- the block device the SD overlay store (ovlstore_sd.c) runs on.
 * Item D13, lane L3-core (docs/planning/HANDOVER_USD_OVERLAY_STORE.md §4.5 b,
 * §12.6).
 *
 * The store is written ONCE, over this vtable, and gets two providers:
 *
 *   ovl_bdev_usd.c    bare metal, over firmware/usd/usd.c (the shell's usd_spi
 *                     block). Asynchronous: *_start() returns at once and the
 *                     driver's usd_poll() moves the bytes, a bounded amount per
 *                     superloop pass.
 *   ovl_bdev_posix.c  pread()/pwrite() on a file or a block device node
 *                     (/dev/mmcblk0 or /dev/mmcblk0p4 under the Linux harness;
 *                     a temp-file card image in the host tests). SYNCHRONOUS:
 *                     the I/O is done inside *_start(), and io_status()
 *                     reports its result on the next look.
 *
 * So the Linux STORE lane selects a provider instead of porting the store, and
 * the two engines behave the same by construction (Linux plan DL6).
 *
 * THE MODEL is usd.c's: one op in flight, 512-byte blocks, LBA addressing.
 *   read_start / write_start  0 = started; -EBUSY = an op is already in flight,
 *                             try again later; any other negative = refused
 *                             (-ENODEV not ready, -EINVAL bad range/args).
 *   io_status                 OVL_BDEV_IO_BUSY while the op runs, then
 *                             OVL_BDEV_IO_DONE or a negative errno (-ENODEV
 *                             card removed, -EIO, -ETIMEDOUT). errno values are
 *                             for THIS build only: they differ between newlib
 *                             and glibc (ETIMEDOUT 116 vs 110), so nothing above
 *                             the store may put one on the wire.
 *   poll (optional)           called by ovlstore_sd_poll() first, when the
 *                             provider wants the store to drive it (see
 *                             ovl_bdev_usd_bind's own_poll).
 *   state / error_code        the card-level view the store passes through to
 *                             the CLCD and the `usd` verb: none / no hw / init /
 *                             ready / unsupported / ERR <n>.
 *   change_count (optional)   bumps on every insert and removal, so a card
 *                             swapped between two store polls is still seen.
 */
#ifndef MPS3_OVL_BDEV_H
#define MPS3_OVL_BDEV_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define OVL_BDEV_BLOCK_SIZE 512u

#define OVL_BDEV_IO_DONE 0
#define OVL_BDEV_IO_BUSY 1

typedef enum {
    OVL_BDEV_NONE = 0,     /* no card in the slot                                */
    OVL_BDEV_NO_HW,        /* no controller (usd: ID mismatch, USD_ERR_NO_BLOCK) */
    OVL_BDEV_INIT,         /* card present, not usable yet (settle / init)       */
    OVL_BDEV_READY,        /* block I/O may be started                           */
    OVL_BDEV_UNSUPPORTED,  /* card present, never usable (SDSC, v1, ...)         */
    OVL_BDEV_ERROR         /* card present, failed; error_code() says why        */
} ovl_bdev_state_t;

typedef struct ovl_bdev_ops {
    int              (*read_start)(void *ctx, uint32_t lba, uint32_t nblocks, void *buf);
    int              (*write_start)(void *ctx, uint32_t lba, uint32_t nblocks, const void *buf);
    int              (*io_status)(void *ctx);
    void             (*poll)(void *ctx, uint32_t now_ms);   /* optional, may be NULL */
    uint32_t         (*nblocks)(void *ctx);                 /* 0 unless READY        */
    bool             (*present)(void *ctx);                 /* a card is in the slot */
    ovl_bdev_state_t (*state)(void *ctx);
    int              (*error_code)(void *ctx);              /* the "ERR <n>" number  */
    uint32_t         (*change_count)(void *ctx);            /* optional, may be NULL */
} ovl_bdev_ops_t;

typedef struct {
    const ovl_bdev_ops_t *ops;
    void                 *ctx;
    uint32_t              max_blocks;   /* most blocks one op may carry, >= 1 */
} ovl_bdev_t;

/* ---------------------------------------------------------------------------
 * Provider 1: bare metal, over firmware/usd/usd.c (ovl_bdev_usd.c).
 *
 * The caller still owns usd_init() (once at boot). own_poll selects who runs
 * usd_poll():
 *   true   ovlstore_sd_poll() calls usd_poll(now_ms) first -- ONE superloop
 *          service row drives both (the recommended target wiring: the
 *          service table is full, see INTEGRATION_L3.md);
 *   false  something else polls the driver (a host test, or a separate row).
 * Never both: two usd_poll() calls per pass double the pass's SPI budget.
 * ------------------------------------------------------------------------- */
void ovl_bdev_usd_bind(ovl_bdev_t *bd, bool own_poll);

/* ---------------------------------------------------------------------------
 * Provider 2: POSIX (ovl_bdev_posix.c). Hosted builds only (the MicroBlaze
 * shell never links it). One struct per open device.
 * ------------------------------------------------------------------------- */
typedef struct {
    int      fd;
    int      owns_fd;      /* close() closes fd */
    int      last;         /* result of the last op: 0 or -errno */
    bool     present;      /* test hook: false = "card pulled" */
    bool     writable;
    bool     nocache;      /* OVL_BDEV_POSIX_NOCACHE */
    uint32_t nblocks;
    uint32_t changes;      /* change_count() */
} ovl_bdev_posix_t;

/* open() flags.
 * THE PAGE-CACHE TRAP (Linux): pwrite() to /dev/mmcblk0* lands in the page
 * cache and pread() is then served from it, so the store's read-back verify
 * would check RAM, not the card. On a real card open with
 * OVL_BDEV_POSIX_LINUX_CARD: O_DSYNC makes each write reach the device before
 * pwrite() returns, and NOCACHE drops the (now clean) cached pages of a range
 * with posix_fadvise(POSIX_FADV_DONTNEED) before it is read, so the read goes
 * to the media. Card images in tests need neither. */
#define OVL_BDEV_POSIX_WRITABLE   0x1u
#define OVL_BDEV_POSIX_DSYNC      0x2u
#define OVL_BDEV_POSIX_NOCACHE    0x4u
#define OVL_BDEV_POSIX_LINUX_CARD (OVL_BDEV_POSIX_WRITABLE | OVL_BDEV_POSIX_DSYNC | \
                                   OVL_BDEV_POSIX_NOCACHE)

/* Open `path` (a card image, /dev/mmcblk0, or /dev/mmcblk0p4). Without
 * OVL_BDEV_POSIX_WRITABLE every write_start() fails -EROFS. The size is taken
 * once, here, rounded down to whole blocks. Returns 0 or -errno. */
int  ovl_bdev_posix_open(ovl_bdev_posix_t *p, ovl_bdev_t *bd, const char *path, unsigned flags);
/* Same over an fd the caller already holds (not closed by close()). */
int  ovl_bdev_posix_attach_fd(ovl_bdev_posix_t *p, ovl_bdev_t *bd, int fd);
void ovl_bdev_posix_close(ovl_bdev_posix_t *p);
/* Test hook: pretend the card was pulled (state NONE, I/O -ENODEV) or put
 * back (change_count bumps either way). */
void ovl_bdev_posix_set_present(ovl_bdev_posix_t *p, bool present);

#ifdef __cplusplus
}
#endif

#endif /* MPS3_OVL_BDEV_H */
