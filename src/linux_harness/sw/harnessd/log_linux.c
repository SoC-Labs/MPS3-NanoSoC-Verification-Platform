/*
 * log_linux.c — harnessd's console, and the tee behind the `log` verb.
 *
 * On bare metal every console byte goes through outbyte(), which main.c
 * overrides to store the byte in the RAM ring (common/log_ring.c) AND send it
 * to the UART. Here every console byte goes through harnessd_console_write(),
 * which does the same two things: mps3_log_write() into the SAME ring (so the
 * 6900 `log` verb is served by the same code) and a write to stdout — which init
 * connects to the console (the tty_02 share), exactly where the bare-metal UART
 * went.
 *
 * `--kmsg` additionally tails /dev/kmsg into the ring (service slot 1), so a
 * host reading `log` after a respawn can see the kernel's side of it (the SIGBUS
 * line, an OOM kill) without an SSH session. Ring only, never stdout: the
 * console already shows kernel messages.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#include "../../../../firmware/common/log_ring.h"
#include "compat/xil_printf.h"
#include "harnessd.h"

void harnessd_console_write(const char *buf, size_t n)
{
    if (n == 0) {
        return;
    }
    mps3_log_write(buf, (uint32_t)n);
    if (!g_hd.quiet) {
        /* One write(2) per call: stdout may be a pipe or the console tty, and a
         * line split across stdio buffers interleaves badly with the kernel's. */
        ssize_t off = 0;
        while ((size_t)off < n) {
            ssize_t w = write(STDOUT_FILENO, buf + off, n - (size_t)off);
            if (w <= 0) {
                if (w < 0 && errno == EINTR) {
                    continue;
                }
                break;   /* console gone: the ring still has it */
            }
            off += w;
        }
    }
}

static void vlog(const char *fmt, va_list ap)
{
    char buf[512];
    int n = vsnprintf(buf, sizeof(buf), fmt, ap);
    if (n < 0) {
        return;
    }
    if ((size_t)n >= sizeof(buf)) {
        n = (int)sizeof(buf) - 1;
    }
    harnessd_console_write(buf, (size_t)n);
}

void harnessd_log(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vlog(fmt, ap);
    va_end(ap);
}

int xil_printf(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vlog(fmt, ap);
    va_end(ap);
    return 0;
}

/* ---- /dev/kmsg tail -------------------------------------------------------- */
static int s_kmsg = -1;

int harnessd_kmsg_open(void)
{
    s_kmsg = open("/dev/kmsg", O_RDONLY | O_NONBLOCK | O_CLOEXEC);
    if (s_kmsg < 0) {
        return -1;
    }
    /* Only what happens from now on: the boot log is on the console already. */
    (void)lseek(s_kmsg, 0, SEEK_END);
    return 0;
}

void harnessd_kmsg_poll(void)
{
    char rec[1024];
    if (s_kmsg < 0) {
        return;
    }
    for (int i = 0; i < 8; i++) {   /* bounded, like every service */
        ssize_t n = read(s_kmsg, rec, sizeof(rec) - 1);
        if (n < 0) {
            if (errno == EPIPE) {
                continue;   /* records were overwritten under us: skip ahead */
            }
            return;         /* EAGAIN: nothing new */
        }
        if (n == 0) {
            return;
        }
        rec[n] = '\0';
        /* "pri,seq,usec,flags;message\n" -> "[kmsg] message\n" */
        char *msg = strchr(rec, ';');
        msg = msg ? msg + 1 : rec;
        char *nl = strchr(msg, '\n');
        if (nl) {
            *nl = '\0';
        }
        char line[1100];
        int m = snprintf(line, sizeof(line), "[kmsg] %s\n", msg);
        if (m > 0) {
            mps3_log_write(line, (uint32_t)((size_t)m < sizeof(line) ? (size_t)m : sizeof(line) - 1));
        }
    }
}
