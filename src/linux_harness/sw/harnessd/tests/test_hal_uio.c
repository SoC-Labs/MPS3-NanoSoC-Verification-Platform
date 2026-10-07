/*
 * test_hal_uio.c — hal_uio.c + hal_front.c against a FAKE /sys/class/uio tree and
 * plain files standing in for /dev/uioN. The code under test is the production
 * file, unmodified: hal_opts_t points it at the fake tree (its only test seam).
 *
 * What it proves (HARNESSD_CONTRACT.md §2):
 *   1. discovery: every uioN/maps/mapM becomes a window at its PHYSICAL address;
 *   2. the four platform_regs.h accessors land at base + off inside the right
 *      file, including a map that is not the device's map0 (page offset M);
 *   3. the sub-page LMB-tail node (addr page-aligned, the region in-page) works;
 *   4. strict mode refuses to open with a required block missing, and names it;
 *   5. an unmapped base ABORTS naming the base — never a silent 0 (death test,
 *      in a child process);
 *   6. the reset shield: while up, RESET_CTRL release bits cannot be cleared.
 */
#define _GNU_SOURCE
#include <fcntl.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

#include "../../../../../firmware/common/platform_regs.h"
#include "../hal.h"
#include "../harnessd.h"

/* The two platform symbols hal_uio.c logs through (log_linux.c is not linked). */
harnessd_cfg_t g_hd;
static char s_log[8192];
void harnessd_log(const char *fmt, ...)
{
    size_t n = strlen(s_log);
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(s_log + n, sizeof(s_log) - n, fmt, ap);
    va_end(ap);
}

static int s_fail;
#define CHECK(c) do { if (!(c)) { fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #c); s_fail++; } } while (0)

static char s_root[256];

static void wfile(const char *path, const char *text)
{
    FILE *f = fopen(path, "w");
    if (!f) { perror(path); exit(2); }
    fputs(text, f);
    fclose(f);
}

/* uioN with maps (addr, size) — the device file gets (maps * page) bytes. */
static void mk_dev(int n, const unsigned long *addr, const unsigned long *size, int maps)
{
    char p[512], v[64];
    long page = sysconf(_SC_PAGESIZE);
    snprintf(p, sizeof(p), "%s/sys/uio%d", s_root, n);
    mkdir(p, 0755);
    snprintf(p, sizeof(p), "%s/sys/uio%d/maps", s_root, n);
    mkdir(p, 0755);
    long total = 0;
    for (int m = 0; m < maps; m++) {
        snprintf(p, sizeof(p), "%s/sys/uio%d/maps/map%d", s_root, n, m);
        mkdir(p, 0755);
        snprintf(p, sizeof(p), "%s/sys/uio%d/maps/map%d/addr", s_root, n, m);
        snprintf(v, sizeof(v), "0x%08lx\n", addr[m]);
        wfile(p, v);
        snprintf(p, sizeof(p), "%s/sys/uio%d/maps/map%d/size", s_root, n, m);
        snprintf(v, sizeof(v), "0x%lx\n", size[m]);
        wfile(p, v);
        total = (long)m * page + (long)size[m];
    }
    snprintf(p, sizeof(p), "%s/dev/uio%d", s_root, n);
    int fd = open(p, O_RDWR | O_CREAT | O_TRUNC, 0644);
    if (fd < 0 || ftruncate(fd, total) != 0) { perror(p); exit(2); }
    close(fd);
}

static uint32_t file_word(int n, long off)
{
    char p[512];
    uint32_t w = 0;
    snprintf(p, sizeof(p), "%s/dev/uio%d", s_root, n);
    FILE *f = fopen(p, "rb");
    fseek(f, off, SEEK_SET);
    if (fread(&w, 4, 1, f) != 1) w = 0xFFFFFFFFu;
    fclose(f);
    return w;
}

int main(void)
{
    long page = sysconf(_SC_PAGESIZE);
    const char *tmp = getenv("TMPDIR");
    snprintf(s_root, sizeof(s_root), "%s/test_hal_uio.%d", tmp ? tmp : "/tmp", (int)getpid());
    char p[512];
    mkdir(s_root, 0755);
    snprintf(p, sizeof(p), "%s/sys", s_root); mkdir(p, 0755);
    snprintf(p, sizeof(p), "%s/dev", s_root); mkdir(p, 0755);

    /* uio0: CLKRST (map0) + DFXCTL (map1 — a second map of one device).
     * uio1: the LMB tail page. Everything else this build requires: absent. */
    unsigned long a0[2] = { 0x44A00000ul, 0x44A10000ul }, s0[2] = { 0x10000ul, 0x10000ul };
    mk_dev(0, a0, s0, 2);
    unsigned long a1[1] = { 0x0001F000ul }, s1[1] = { 0x1000ul };
    mk_dev(1, a1, s1, 1);

    char sys[300], dev[300];
    snprintf(sys, sizeof(sys), "%s/sys", s_root);
    snprintf(dev, sizeof(dev), "%s/dev", s_root);

    /* 4. strict: required blocks are missing -> refuse, naming them. */
    hal_opts_t o = { 1, sys, dev, 0 };
    CHECK(hal_backend_open(&o) == -1);
    CHECK(strstr(s_log, "hwicap") && strstr(s_log, "MISSING (required)"));
    CHECK(strstr(s_log, "clkrst    0x44a00000  mapped"));

    /* 1..3 non-strict: the mapped windows work. */
    s_log[0] = '\0';
    o.strict = 0;
    CHECK(hal_backend_open(&o) == 0);
    mps3_reg_write32(MPS3_CLKRST_BASE, 0x10, 0xA5A5F00Du);
    CHECK(file_word(0, 0x10) == 0xA5A5F00Du);
    mps3_reg_write32(MPS3_DFXCTL_BASE, DFXCTL_DECOUPLE, 1u);       /* map1 = page 1 */
    CHECK(file_word(0, page + DFXCTL_DECOUPLE) == 1u);
    CHECK(mps3_reg_read32(MPS3_DFXCTL_BASE, DFXCTL_DECOUPLE) == 1u);
    mps3_reg_set_bits32(MPS3_DFXCTL_BASE, DFXCTL_SHUTDOWN, 0x4u);
    mps3_reg_clr_bits32(MPS3_DFXCTL_BASE, DFXCTL_DECOUPLE, 1u);
    CHECK(file_word(0, page + DFXCTL_SHUTDOWN) == 0x4u && file_word(0, page) == 0u);
    CHECK(hal_write_count() == 4u);
    volatile uint32_t *mb = hal_backend_window(HARNESSD_MAILBOX_ADDR, 0x100);
    CHECK(mb != 0);
    if (mb) {
        mb[0] = 0xD1A6C0DEu;
        CHECK(file_word(1, 0xF00) == 0xD1A6C0DEu);
    }
    CHECK(hal_backend_window(HARNESSD_MAILBOX_ADDR, 0x200) == 0);  /* runs off the page */

    /* 6. the reset shield */
    mps3_reg_write32(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL, CLKRST_RESET_CTRL_RP_RESETN |
                     CLKRST_RESET_CTRL_DUT_RESETN);
    hal_reset_shield(1);
    mps3_reg_write32(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL, 0u);   /* clkrst_init() */
    CHECK(file_word(0, CLKRST_RESET_CTRL) == (CLKRST_RESET_CTRL_RP_RESETN |
                                              CLKRST_RESET_CTRL_DUT_RESETN));
    CHECK(hal_reset_shield_hits() == 1u);
    hal_reset_shield(0);
    mps3_reg_write32(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL, 0u);
    CHECK(file_word(0, CLKRST_RESET_CTRL) == 0u);                  /* shield down: obeyed */

    /* 5. an unmapped base aborts, naming it — in a child, and the NEGATIVE of
     *    it (a mapped base in a child) must exit cleanly. */
    for (int neg = 0; neg < 2; neg++) {
        int pipefd[2];
        if (pipe(pipefd) != 0) return 2;
        pid_t pid = fork();
        if (pid == 0) {
            dup2(pipefd[1], 2);
            uint32_t v = mps3_reg_read32(neg ? MPS3_CLKRST_BASE : MPS3_HWICAP_BASE, 0x110);
            _exit((int)(v & 1u));
        }
        close(pipefd[1]);
        char msg[512] = "";
        ssize_t n = read(pipefd[0], msg, sizeof(msg) - 1);
        if (n > 0) msg[n] = '\0';
        close(pipefd[0]);
        int st = 0;
        waitpid(pid, &st, 0);
        if (!neg) {
            CHECK(WIFSIGNALED(st) && WTERMSIG(st) == SIGABRT);
            CHECK(strstr(msg, "0x44a20000") != 0);
        } else {
            CHECK(WIFEXITED(st));
        }
    }

    char cmd[400];
    snprintf(cmd, sizeof(cmd), "rm -rf %s", s_root);
    if (system(cmd) != 0) { /* best effort */ }
    if (s_fail) {
        fprintf(stderr, "test_hal_uio: %d FAILED\n%s", s_fail, s_log);
        return 1;
    }
    printf("test_hal_uio: ALL PASS\n");
    return 0;
}
