/*
 * hal.h — harnessd's register HAL, internal interface.
 *
 * The firmware service modules call exactly four functions for every register
 * access (firmware/common/platform_regs.h: mps3_reg_{read,write,set_bits,
 * clr_bits}32). harnessd defines those four ONCE, in hal_front.c, and puts two
 * things in front of the real access that the firmware must never know about:
 *
 *   1. THE WORK COUNTER. A pass in which a service module WROTE a register did
 *      work (streamed ICAP words, pushed CLCD cells, wiggled a JTAG pin, fed a
 *      UART) and the loop should run again at once; a pass with no write and no
 *      network traffic was idle and may sleep (main_linux.c, the idle policy).
 *      Reads do not count: polling a status register is what an idle service
 *      does.
 *
 *   2. THE RESPAWN RESET SHIELD. clkrst_init() -- run by coordinator_init() on
 *      every start -- writes CLKRST.RESET_CTRL = 0, holding the DUT, RP and debug
 *      resets asserted. On bare metal that only ever happens at a board reset,
 *      when those resets are asserted anyway. harnessd can be RESPAWNED while the
 *      board keeps running, and the same write would reset a live DUT. While the
 *      shield is up (main_linux.c raises it around the module inits on a
 *      respawn), a write to RESET_CTRL cannot clear a bit that currently reads 1:
 *      the release state the respawn FOUND survives, and the firmware code runs
 *      unmodified.
 *
 * Below the front sits exactly one BACKEND per build:
 *   hal_uio.c   -DMPS3_HAL_UIO   /dev/uioN windows (the board)
 *   hal_mock.c  -DMPS3_HAL_MOCK  a behavioural model of the shell fabric (host
 *                                tests, the QEMU smoke) that serves ONLY the
 *                                blocks the UIO build maps, so an access outside
 *                                that list fails on the host as on the board.
 */
#ifndef HARNESSD_HAL_H
#define HARNESSD_HAL_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* The LMB-tail page (HARNESSD_CONTRACT.md §3/§4): stage0 status block at
 * 0x1FE00, diag mailbox at 0x1FF00. One 4 KiB UIO window. */
#define HARNESSD_LMB_TAIL_BASE   0x0001F000u
#define HARNESSD_LMB_TAIL_SIZE   0x1000u
#define HARNESSD_S0_STATUS_ADDR  0x0001FE00u
#define HARNESSD_MAILBOX_ADDR    0x0001FF00u

/* ---- the expected-block list (hal_front.c) --------------------------------- */
typedef struct {
    uintptr_t   base;
    uint32_t    size;
    const char *name;       /* the DTS linux,uio-name IMAGE should give it */
    int         required;   /* 1 = this build's feature set touches it     */
} hal_block_t;

/* Every block this BUILD can touch (HARNESSD_CONTRACT.md §3). Blocks a feature
 * flag leaves out of the build are listed with required = 0. */
const hal_block_t *hal_expected_blocks(unsigned *n);

/* ---- backend (hal_uio.c | hal_mock.c) ------------------------------------- */

/* Backend options, set by main_linux.c before hal_backend_open(). */
typedef struct {
    int         strict;         /* UIO: a MISSING required block fails the open  */
    const char *uio_sysfs;      /* UIO: default "/sys/class/uio"                 */
    const char *uio_devdir;     /* UIO: default "/dev"                           */
    const char *mock_fabric;    /* MOCK: back the fabric with this file (NULL = RAM) */
} hal_opts_t;

int         hal_backend_open(const hal_opts_t *o);
uint32_t    hal_backend_read32(uintptr_t base, uintptr_t off);
void        hal_backend_write32(uintptr_t base, uintptr_t off, uint32_t val);
const char *hal_backend_name(void);

/* How many windows the backend mapped (UIO); the mock reports its block count.
 * 0 after a successful open = there is no fabric here at all (no-hw mode). */
unsigned    hal_backend_windows(void);

/* A pointer to [phys, phys + len) when one window covers it, else NULL. For the
 * LMB-tail page, which is RAM (read/written as a block, not as registers). */
volatile uint32_t *hal_backend_window(uintptr_t phys, size_t len);

/* ---- front (hal_front.c) ---------------------------------------------------- */

/* Service-module register WRITES since start (the work signal). */
uint64_t hal_write_count(void);

/* Housekeeping accesses that must NOT count as work (the WDOG kick, the LED,
 * identity reads): straight to the backend. */
uint32_t hal_quiet_read32(uintptr_t base, uintptr_t off);
void     hal_quiet_write32(uintptr_t base, uintptr_t off, uint32_t val);

/* The respawn reset shield (file header). */
void     hal_reset_shield(int up);
uint32_t hal_reset_shield_hits(void);   /* writes it altered while up */

/* THE CLCD TAP (the LCD mirror's interim software mode, lcdmirror_tap.c). When
 * installed, every service-module access to the CLCD and CLCDKVM pages is shown
 * to it AFTER the backend performed it: writes with the value written, reads
 * with the value read (is_write 0). It is how the mirror sees exactly the bytes
 * harnessd pushes to the panel, and the KVM owner/reset state clcd.c already
 * reads every pass, without one extra register access. Quiet accesses
 * (hal_quiet_*) are not shown. NULL uninstalls it. */
typedef void (*hal_tap_fn)(uintptr_t base, uintptr_t off, uint32_t val, int is_write);
void     hal_set_tap(hal_tap_fn fn);

/* THE CLCD BULK TAP (lane CLCD-SPEED). clcd.c's bus seam (clcd.h clcd_bus_push;
 * the strong definition is hal_front.c's) writes a run of CLCD FIFO bytes
 * straight to the UIO window, then shows the WHOLE run here, in bus order:
 * rs[i] 0 = a CLCD_CMD write, 1 = a CLCD_DATA write, val[i] the byte -- exactly
 * the writes, in exactly the order, the tap above would have been shown one by
 * one. With no bulk tap installed, the run goes to that tap one write at a time.
 * NULL uninstalls it. */
typedef void (*hal_tap_bytes_fn)(const uint8_t *rs, const uint8_t *val, uint32_t n);
void     hal_set_tap_bytes(hal_tap_bytes_fn fn);

/* The strong clcd_bus_push()'s own counters (the lane's tests; `stats` does not
 * carry them): runs pushed, bytes pushed, and CLCD_STATUS reads spent on them. */
void     hal_clcd_push_counts(uint32_t *runs, uint32_t *bytes, uint32_t *status_reads);

#ifdef __cplusplus
}
#endif

#endif /* HARNESSD_HAL_H */
