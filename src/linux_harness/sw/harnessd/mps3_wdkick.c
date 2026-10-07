/*
 * mps3_wdkick.c — mps3-wdkick, the BOUNDED watchdog bridge from init to
 * mps3-harnessd (HARNESSD_CONTRACT.md §5.3, IMAGE_CONTRACT.md §8).
 *
 * WHY. stage0 arms the AXI Timebase WDT before it jumps to Linux (reset ~42.9 s
 * after the last kick). harnessd is the watchdog's owner, but it starts from
 * inittab only after ALL of rcS, and with /persist on the user microSD
 * S12mps3persist alone takes ~9 s on the board (mmc probe retries, the ext4
 * mount, the first host key): B2 2026-09-26, RC2, the board was reset before
 * harnessd printed a line. This helper, started first thing in rcS
 * (S00mps3wdkick), keeps the watchdog fed until harnessd runs -- and no longer.
 *
 * WHAT IT DOES, every --period-ms (2000):
 *   1. mps3-harnessd running (a /proc/<pid>/comm match)?  -> stop: the owner is up.
 *   2. past --max-s (150) seconds since BOOT?             -> stop: a boot that has
 *      not started harnessd by then is broken, and the watchdog must reset it.
 *   3. read TWCSR0; iff EWDT1 is set, write (csr0 & EWDT1) | WDS  -- the same
 *      kick harnessd's wdog_touch() does. TWCSR1 is NEVER touched (EWDT2 is
 *      write-only and reads 0), and nothing is ever enabled or disabled: a
 *      watchdog stage0 did not arm stays off.
 *
 * SAFETY. A kernel that dies stops this process -> no kick -> reset. rcS or
 * harnessd never coming up -> this stops at --max-s -> reset <= max + ~43 s.
 * harnessd stays the ONLY writer of stage0's CONFIRM word -- this file does not
 * map the LMB tail at all -- and the watchdog's only owner once it runs.
 *
 * DISCOVERY is harnessd's own: hal_uio.c (linked unchanged) maps /sys/class/uio
 * by PHYSICAL address and this looks up MPS3_WDOG_BASE, exactly as harnessd does.
 * Test seams: --uio-sysfs/--uio-devdir (hal_uio.c's own), --proc, --name, and
 * --mem FILE (map FILE as the watchdog page -- the QEMU proof, which has no
 * fabric, uses it via S00mps3wdkick's mps3.wdkick_mem= cmdline knob).
 */
#define _GNU_SOURCE
#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <syslog.h>
#include <time.h>
#include <unistd.h>

#include "hal.h"
#include "harnessd.h"
#include "../../../../firmware/common/platform_regs.h"

#define WDKICK_OWNER "mps3-harnessd"

/* ---- hal_uio.c's two hooks, for a process that maps ONE block ---------------- */
const hal_block_t *hal_expected_blocks(unsigned *n)
{
    static const hal_block_t b[] = { { MPS3_WDOG_BASE, 0x10000u, "wdog", 1 } };
    *n = 1u;
    return b;
}

void harnessd_log(const char *fmt, ...)
{
    va_list ap;
    printf("wdkick: ");
    va_start(ap, fmt);
    vprintf(fmt, ap);
    va_end(ap);
    fflush(stdout);
}

static void say(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void say(const char *fmt, ...)
{
    char line[256];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(line, sizeof(line), fmt, ap);
    va_end(ap);
    printf("wdkick: %s\n", line);
    fflush(stdout);
    syslog(LOG_NOTICE, "%s", line);   /* best effort: syslogd may not be up yet */
}

static uint64_t boot_ms(void)
{
    struct timespec ts;
    if (clock_gettime(CLOCK_BOOTTIME, &ts) != 0) {
        clock_gettime(CLOCK_MONOTONIC, &ts);
    }
    return (uint64_t)ts.tv_sec * 1000u + (uint64_t)ts.tv_nsec / 1000000u;
}

/* The owner is up when any process other than us has /proc/<pid>/comm == name. */
static int owner_running(const char *procdir, const char *name)
{
    DIR *d = opendir(procdir);
    if (!d) {
        return 0;
    }
    int found = 0;
    pid_t self = getpid();
    struct dirent *e;
    while (!found && (e = readdir(d)) != 0) {
        if (!isdigit((unsigned char)e->d_name[0]) || atol(e->d_name) == (long)self) {
            continue;
        }
        char path[512], comm[64] = "";
        snprintf(path, sizeof(path), "%s/%s/comm", procdir, e->d_name);
        FILE *f = fopen(path, "r");
        if (!f) {
            continue;
        }
        if (fgets(comm, sizeof(comm), f)) {
            comm[strcspn(comm, "\n")] = '\0';
            found = strcmp(comm, name) == 0;
        }
        fclose(f);
    }
    closedir(d);
    return found;
}

static void usage(void)
{
    fprintf(stderr,
"usage: mps3-wdkick [--max-s N] [--period-ms N] [--name PROC] [--proc DIR]\n"
"                   [--uio-sysfs DIR] [--uio-devdir DIR] [--mem FILE]\n"
"  kicks the stage0-armed watchdog (TWCSR0 = EWDT1|WDS, only when EWDT1 is set)\n"
"  until PROC (" WDKICK_OWNER ") runs or N (150) seconds since boot have passed\n");
}

int main(int argc, char **argv)
{
    unsigned max_s = 150u, period_ms = 2000u;
    const char *name = WDKICK_OWNER, *procdir = "/proc", *mem = 0;
    hal_opts_t o = { 0, "/sys/class/uio", "/dev", 0 };
    static const struct option lo[] = {
        { "max-s", 1, 0, 'm' }, { "period-ms", 1, 0, 'p' }, { "name", 1, 0, 'n' },
        { "proc", 1, 0, 'P' }, { "uio-sysfs", 1, 0, 's' }, { "uio-devdir", 1, 0, 'd' },
        { "mem", 1, 0, 'M' }, { "help", 0, 0, 'h' }, { 0, 0, 0, 0 },
    };
    int c;
    while ((c = getopt_long(argc, argv, "", lo, 0)) != -1) {
        switch (c) {
        case 'm': max_s = (unsigned)strtoul(optarg, 0, 0); break;
        case 'p': period_ms = (unsigned)strtoul(optarg, 0, 0); break;
        case 'n': name = optarg; break;
        case 'P': procdir = optarg; break;
        case 's': o.uio_sysfs = optarg; break;
        case 'd': o.uio_devdir = optarg; break;
        case 'M': mem = optarg; break;
        default:  usage(); return c == 'h' ? 0 : 2;
        }
    }
    if (period_ms == 0u) {
        period_ms = 2000u;
    }
    openlog("wdkick", 0, LOG_DAEMON);

    volatile uint32_t *wd = 0;
    if (mem) {                                   /* the QEMU / test stand-in */
        int fd = open(mem, O_RDWR | O_CLOEXEC);
        void *p = fd >= 0 ? mmap(0, 4096, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0) : MAP_FAILED;
        if (fd >= 0) {
            close(fd);
        }
        if (p == MAP_FAILED) {
            say("cannot map %s (%s) -- nothing to bridge", mem, strerror(errno));
            return 1;
        }
        wd = (volatile uint32_t *)p;
    } else {
        (void)hal_backend_open(&o);
        wd = hal_backend_window(MPS3_WDOG_BASE, 8u);
        if (!wd) {
            say("no UIO window for the watchdog (0x%08x) -- nothing to bridge",
                (unsigned)MPS3_WDOG_BASE);
            return 0;
        }
    }

    say("bridging until harnessd (max %u s)", max_s);
    unsigned kicks = 0u;
    for (;;) {
        uint64_t now = boot_ms();
        if (owner_running(procdir, name)) {
            say("harnessd up at %u.%01u s, stopping (%u kick(s))",
                (unsigned)(now / 1000u), (unsigned)(now % 1000u) / 100u, kicks);
            return 0;
        }
        if (now >= (uint64_t)max_s * 1000u) {
            say("max reached, stopping -- the watchdog will reset if nothing kicks "
                "(%u kick(s))", kicks);
            return 0;
        }
        uint32_t csr0 = wd[WDOG_TWCSR0 / 4u];
        if (csr0 & WDOG_TWCSR0_EWDT1) {
            wd[WDOG_TWCSR0 / 4u] = (csr0 & WDOG_TWCSR0_EWDT1) | WDOG_TWCSR0_WDS;
            kicks++;
        }
        struct timespec ts = { (time_t)(period_ms / 1000u), (long)(period_ms % 1000u) * 1000000L };
        nanosleep(&ts, 0);
    }
}
