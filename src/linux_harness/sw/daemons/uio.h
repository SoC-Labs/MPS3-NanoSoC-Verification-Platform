/*
 * uio.h — tiny helper for the shell_linux UIO estate.
 *
 * The DTS (src/linux_harness/shell_linux.dts) binds every soclabs block via
 * uio_pdrv_genirq with compatible "...,X", "generic-uio" and the load-bearing
 * bootarg uio_pdrv_genirq.of_id=generic-uio. /sys/class/uio/uioN/name carries
 * the DT node name; this helper finds a device by that name and mmaps map0.
 *
 * The daemons run STUBBED by default (no UIO access) — these hooks are the
 * seam where the hardware backends plug in on the board:
 *   dfx-ctl / hwicap-fpga-mgr : interim UIO per DRIVER_MATRIX §2.8 (remove
 *                               generic-uio from those nodes when the custom
 *                               drivers land)
 *   clkrst / jtag-bb / dbgbr / uart-bridge / board-gpio / clcd / clcd-kvm /
 *   telem / vphy / genchk     : permanent UIO + daemon estate (§2.9)
 */
#ifndef MPS3_UIO_H
#define MPS3_UIO_H

#include <stddef.h>
#include <stdint.h>

typedef struct {
    int fd;
    volatile uint32_t *regs;   /* map0, page-aligned */
    size_t map_size;
    char dev[32];              /* "/dev/uioN" */
} mps3_uio_t;

/* Find the uioN whose /sys/class/uio/uioN/name equals name (the DT node, e.g.
 * "clkrst@44a00000" appears as "clkrst"), open + mmap map0.
 * Returns 0 on success, -1 if absent/unmappable (caller must treat absence
 * as "block not present" and keep the frozen decline path — NEVER probe a
 * page blind: under the MBV a DECERR is a real S-mode access fault). */
int mps3_uio_open(const char *name, mps3_uio_t *u);
void mps3_uio_close(mps3_uio_t *u);

static inline uint32_t mps3_uio_read32(const mps3_uio_t *u, uint32_t off)
{
    return u->regs[off / 4];
}

static inline void mps3_uio_write32(const mps3_uio_t *u, uint32_t off, uint32_t v)
{
    u->regs[off / 4] = v;
}

#endif
