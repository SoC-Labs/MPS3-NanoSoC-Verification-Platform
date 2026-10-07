/*
 * hal_uio.c — the MPS3_HAL_UIO backend: physical base -> /dev/uioN mapping.
 *
 * HARNESSD_CONTRACT.md §2 is the normative description; the short form:
 *
 *   DISCOVERY  once, at start: every /sys/class/uio/uioN/maps/mapM/{addr,size,
 *              offset} becomes a window [addr, addr + size) mmap'd from
 *              /dev/uioN at page offset M. uio_pdrv_genirq page-aligns `addr` and
 *              page-rounds `size` (the in-page start is `offset`), so a sub-page
 *              node — the LMB tail — needs nothing special.
 *   LOOKUP     by PHYSICAL address, so platform_regs.h's MPS3_*_BASE constants
 *              are used unchanged and the DTS node NAMES do not matter (the
 *              July daemons matched on linux,uio-name and a DTS that set none
 *              made every block "absent" — matching on the address the firmware
 *              already uses cannot drift that way). The last hit is cached.
 *   UNKNOWN    base = a loud FATAL naming the base, then abort(). Never a silent
 *              0: 0 is a perfectly plausible register value.
 *   DECERR     a slave that answers DECERR/SLVERR raises a bus error, which the
 *              kernel delivers as SIGBUS. It is NOT caught: the process dies with
 *              the faulting address in the kernel log, init respawns it, and the
 *              watchdog resets the board if the respawn cannot keep the service
 *              table healthy. Bare metal hangs the MicroBlaze on the same access.
 *
 * The mapping is MAP_SHARED on an O_SYNC fd; uio_mmap_physical() maps it
 * non-cached, so a volatile 32-bit load/store is exactly one AXI-Lite access.
 *
 * Test seams: the sysfs root and the device directory are options (hal_opts_t),
 * so tests/test_hal_uio.c drives this file against a fake sysfs tree and plain
 * files standing in for /dev/uioN — the SAME code path, no #ifdef.
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#include "hal.h"
#include "harnessd.h"

#define UIO_MAX_WINDOWS 48

typedef struct {
    uintptr_t          addr;   /* physical, page-aligned */
    size_t             size;
    volatile uint8_t  *ptr;
    int                dev;    /* N of /dev/uioN, for the log */
    int                map;    /* M of mapM */
} uio_win_t;

static uio_win_t  s_win[UIO_MAX_WINDOWS];
static unsigned   s_nwin;
static uio_win_t *s_last;      /* the lookup cache */

const char *hal_backend_name(void) { return "uio"; }
unsigned    hal_backend_windows(void) { return s_nwin; }

static int read_ulong(const char *path, unsigned long *out)
{
    char buf[64];
    FILE *f = fopen(path, "r");
    if (!f) {
        return -1;
    }
    if (!fgets(buf, sizeof(buf), f)) {
        fclose(f);
        return -1;
    }
    fclose(f);
    errno = 0;
    char *end = 0;
    unsigned long v = strtoul(buf, &end, 0);
    if (errno != 0 || end == buf) {
        return -1;
    }
    *out = v;
    return 0;
}

static void map_device(const hal_opts_t *o, int dev)
{
    char path[512];
    long page = sysconf(_SC_PAGESIZE);
    int fd = -1;

    for (int m = 0; m < 5; m++) {   /* UIO_MAX_MAPS is 5 */
        unsigned long addr = 0, size = 0;
        snprintf(path, sizeof(path), "%s/uio%d/maps/map%d/addr", o->uio_sysfs, dev, m);
        if (read_ulong(path, &addr) != 0) {
            break;
        }
        snprintf(path, sizeof(path), "%s/uio%d/maps/map%d/size", o->uio_sysfs, dev, m);
        if (read_ulong(path, &size) != 0 || size == 0) {
            break;
        }
        if (s_nwin >= UIO_MAX_WINDOWS) {
            harnessd_log("hal_uio: WARNING more than %d UIO windows; uio%d map%d ignored\n",
                         UIO_MAX_WINDOWS, dev, m);
            break;
        }
        if (fd < 0) {
            snprintf(path, sizeof(path), "%s/uio%d", o->uio_devdir, dev);
            fd = open(path, O_RDWR | O_SYNC | O_CLOEXEC);
            if (fd < 0) {
                harnessd_log("hal_uio: cannot open %s: %s\n", path, strerror(errno));
                return;
            }
        }
        void *p = mmap(0, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd,
                       (off_t)m * (off_t)page);
        if (p == MAP_FAILED) {
            harnessd_log("hal_uio: mmap uio%d map%d (0x%08lx +0x%lx): %s\n",
                         dev, m, addr, size, strerror(errno));
            continue;
        }
        s_win[s_nwin].addr = (uintptr_t)addr;
        s_win[s_nwin].size = (size_t)size;
        s_win[s_nwin].ptr  = (volatile uint8_t *)p;
        s_win[s_nwin].dev  = dev;
        s_win[s_nwin].map  = m;
        s_nwin++;
    }
    if (fd >= 0) {
        close(fd);   /* the mappings stay valid after close */
    }
}

static uio_win_t *find(uintptr_t phys, size_t len)
{
    uio_win_t *w = s_last;
    if (w && phys >= w->addr && phys - w->addr + len <= w->size) {
        return w;
    }
    for (unsigned i = 0; i < s_nwin; i++) {
        w = &s_win[i];
        if (phys >= w->addr && phys - w->addr + len <= w->size) {
            s_last = w;
            return w;
        }
    }
    return 0;
}

int hal_backend_open(const hal_opts_t *o)
{
    hal_opts_t d = { 1, "/sys/class/uio", "/dev", 0 };
    if (!o) {
        o = &d;
    }
    if (!o->uio_sysfs) d.uio_sysfs = "/sys/class/uio"; else d.uio_sysfs = o->uio_sysfs;
    if (!o->uio_devdir) d.uio_devdir = "/dev"; else d.uio_devdir = o->uio_devdir;
    d.strict = o->strict;
    o = &d;
    s_nwin = 0;     /* a re-open re-discovers (the old mappings are simply kept) */
    s_last = 0;

    DIR *dir = opendir(o->uio_sysfs);
    if (!dir) {
        harnessd_log("hal_uio: cannot open %s: %s -- no UIO devices at all "
                     "(is uio_pdrv_genirq.of_id=generic-uio on the kernel cmdline?)\n",
                     o->uio_sysfs, strerror(errno));
    } else {
        struct dirent *e;
        while ((e = readdir(dir)) != 0) {
            if (strncmp(e->d_name, "uio", 3) != 0) {
                continue;
            }
            char *end = 0;
            long n = strtol(e->d_name + 3, &end, 10);
            if (end == e->d_name + 3 || *end != '\0' || n < 0) {
                continue;
            }
            map_device(o, (int)n);
        }
        closedir(dir);
    }

    /* The self-check (HARNESSD_CONTRACT §2 item 4): name every block this build
     * can touch, mapped or not, BEFORE any service runs. */
    unsigned n = 0, missing = 0;
    const hal_block_t *b = hal_expected_blocks(&n);
    for (unsigned i = 0; i < n; i++) {
        uio_win_t *w = find(b[i].base, 4u);
        if (w) {
            harnessd_log("hal_uio: %-9s 0x%08" PRIxPTR "  mapped (uio%d map%d, %zu B)\n",
                         b[i].name, b[i].base, w->dev, w->map, w->size);
        } else {
            harnessd_log("hal_uio: %-9s 0x%08" PRIxPTR "  %s\n", b[i].name, b[i].base,
                         b[i].required ? "MISSING (required)" : "absent (optional)");
            if (b[i].required) {
                missing++;
            }
        }
    }
    s_last = 0;
    if (missing && o->strict) {
        harnessd_log("hal_uio: FATAL %u required block(s) have no UIO window -- "
                     "IMAGE's DTS must give each a generic-uio node "
                     "(HARNESSD_CONTRACT.md §3)\n", missing);
        return -1;
    }
    return 0;
}

static volatile uint32_t *reg(uintptr_t base, uintptr_t off)
{
    uintptr_t phys = base + off;
    uio_win_t *w = find(phys, 4u);
    if (!w) {
        fprintf(stderr, "FATAL hal_uio: no UIO window maps 0x%08" PRIxPTR
                " (+0x%" PRIxPTR ") -- IMAGE's DTS must give this block a "
                "generic-uio node (HARNESSD_CONTRACT.md §3)\n", base, off);
        harnessd_log("FATAL hal_uio: no UIO window maps 0x%08" PRIxPTR " (+0x%" PRIxPTR ")\n",
                     base, off);
        fflush(0);
        abort();
    }
    return (volatile uint32_t *)(w->ptr + (phys - w->addr));
}

uint32_t hal_backend_read32(uintptr_t base, uintptr_t off)
{
    return *reg(base, off);
}

void hal_backend_write32(uintptr_t base, uintptr_t off, uint32_t val)
{
    *reg(base, off) = val;
}

volatile uint32_t *hal_backend_window(uintptr_t phys, size_t len)
{
    uio_win_t *w = find(phys, len);
    return w ? (volatile uint32_t *)(w->ptr + (phys - w->addr)) : 0;
}
