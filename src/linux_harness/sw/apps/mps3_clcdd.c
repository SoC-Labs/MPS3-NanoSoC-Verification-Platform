/*
 * mps3-clcdd — the harness status display under Linux (M5 app port).
 *
 * Drives the shell's CLCD 8080 byte-stream master (UIO node "clcd", DT
 * clcd@44ac0000) with the silicon-proven configuration from the main repo's
 * docs/CLCD_PANEL_FACTS.md: HX8347-D init table (provenance retained), RGB565
 * MSB-first, 40x15 8x16 text renderer with dirty-cell diffing, FIFO-full
 * polled (writes are DROPPED on full by the block — never rely on
 * back-pressure), proven TIMING 4/4/2 rewritten at every panel reset.
 *
 * Status sources are Linux-native (status_linux.c): /proc, /sys/class/net,
 * the mps3_dfx driver's sysfs, /etc/mps3/static_id. The DFXCTL/CLKRST pages
 * are NEVER touched here (driver-owned).
 *
 * CLCDKVM: presence-gated and NOT serviced (Wave-4 block, never built). If
 * the clcd-kvm UIO node exists we log it and still never touch the page —
 * under the MBV a stray DECERR is a real S-mode fault (DRIVER_MATRIX §2.9).
 * KVM handover servicing is an open item carried in sw/apps/README.md.
 *
 * QEMU/no-hardware behaviour: with no "clcd" UIO node the daemon exits 0
 * after logging (the block genuinely is not present — same honest-absence
 * rule the daemons use). `--preview` renders one frame as ASCII to stdout
 * with no hardware at all (usable in the QEMU smoke).
 */
#define _POSIX_C_SOURCE 200809L
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "clcd_core.h"
#include "status_linux.h"
#include "uio.h"        /* shared with sw/daemons — -I$(UIO_SRC) */

static volatile sig_atomic_t g_stop;
static void on_sig(int sig) { (void)sig; g_stop = 1; }

static uint64_t now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000u + (uint64_t)(ts.tv_nsec / 1000000);
}

static void sleep_ms(unsigned ms)
{
    struct timespec ts = { ms / 1000u, (long)(ms % 1000u) * 1000000L };
    nanosleep(&ts, NULL);
}

/* ---- UIO-backed clcd_io_t ------------------------------------------------- */
static uint32_t io_read32(void *ctx, uint32_t off)
{
    return mps3_uio_read32((const mps3_uio_t *)ctx, off);
}
static void io_write32(void *ctx, uint32_t off, uint32_t v)
{
    mps3_uio_write32((const mps3_uio_t *)ctx, off, v);
}

/* ---- ASCII preview (no hardware; also used by the QEMU smoke) -------------- */
static void preview(const clcd_status_t *st)
{
    char frame[CLCD_NCELLS];
    uint8_t inv[CLCD_ROWS];
    clcd_build_frame(st, 0, frame, inv);
    printf("+%.*s+\n", (int)CLCD_COLS,
           "----------------------------------------");
    for (unsigned r = 0; r < CLCD_ROWS; r++)
        printf("|%.*s|%s\n", (int)CLCD_COLS, frame + r * CLCD_COLS,
               inv[r] ? "  <== inverted (white-on-red)" : "");
    printf("+%.*s+\n", (int)CLCD_COLS,
           "----------------------------------------");
}

static void usage(const char *argv0)
{
    fprintf(stderr,
        "usage: %s [options]\n"
        "  -n IFACE   netdev for the NET/MAC rows (default eth0)\n"
        "  -i FILE    static_id file (default /etc/mps3/static_id)\n"
        "  -b NAME    board name for row 0 (default %s)\n"
        "  -r MS      status refresh cadence (default %u)\n"
        "  -o FILE    also write the boot-status JSON here each refresh\n"
        "  --preview  render one frame as ASCII to stdout and exit (no hw)\n"
        "  --once     paint one full screen, then exit (bring-up aid)\n",
        argv0, MPS3_BOARD_NAME, (unsigned)CLCD_REFRESH_MS);
}

int main(int argc, char **argv)
{
    mps3_status_cfg_t cfg;
    mps3_status_cfg_default(&cfg);
    const char *board = NULL;
    const char *json_out = NULL;
    unsigned refresh_ms = CLCD_REFRESH_MS;
    int do_preview = 0, once = 0;

    const char *env_root = getenv("MPS3_SYSROOT");   /* test hook */
    if (env_root)
        cfg.root = env_root;

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "-n") && i + 1 < argc)      cfg.netdev = argv[++i];
        else if (!strcmp(argv[i], "-i") && i + 1 < argc) cfg.static_id_file = argv[++i];
        else if (!strcmp(argv[i], "-b") && i + 1 < argc) board = argv[++i];
        else if (!strcmp(argv[i], "-r") && i + 1 < argc) refresh_ms = (unsigned)atoi(argv[++i]);
        else if (!strcmp(argv[i], "-o") && i + 1 < argc) json_out = argv[++i];
        else if (!strcmp(argv[i], "--preview"))          do_preview = 1;
        else if (!strcmp(argv[i], "--once"))             once = 1;
        else { usage(argv[0]); return 2; }
    }

    if (do_preview) {
        clcd_status_t st;
        mps3_status_collect(&cfg, &st);
        snprintf(st.board_name, sizeof(st.board_name), "%s",
                 board ? board : MPS3_BOARD_NAME);
        preview(&st);
        return 0;
    }

    mps3_uio_t uio;
    if (mps3_uio_open("clcd", &uio) != 0) {
        fprintf(stderr, "mps3-clcdd: no 'clcd' UIO node — CLCD block not "
                        "present on this platform; exiting (not an error)\n");
        return 0;
    }

    mps3_uio_t kvm_probe;
    if (mps3_uio_open("clcd-kvm", &kvm_probe) == 0) {
        /* Presence only. The KVM page is NEVER accessed: the block has never
         * been built/run (Wave-4). Unmap immediately. */
        mps3_uio_close(&kvm_probe);
        fprintf(stderr, "mps3-clcdd: clcd-kvm node present but KVM servicing "
                        "is not implemented (Wave-4) — harness keeps the "
                        "panel; page untouched\n");
    }

    signal(SIGINT, on_sig);
    signal(SIGTERM, on_sig);

    clcd_io_t io = { io_read32, io_write32, &uio };
    clcd_core_init(&io, board);

    fprintf(stderr, "mps3-clcdd: driving %s (map %zu B), refresh %u ms\n",
            uio.dev, uio.map_size, refresh_ms);

    uint64_t last_status = 0;
    int painted_once = 0;

    while (!g_stop) {
        uint64_t t = now_ms();

        if (t - last_status >= refresh_ms || last_status == 0) {
            last_status = t;
            clcd_status_t st;
            mps3_status_collect(&cfg, &st);
            if (board)
                snprintf(st.board_name, sizeof(st.board_name), "%s", board);
            else
                st.board_name[0] = '\0';   /* keep the core's default */
            clcd_core_update_status(&st);

            if (json_out) {
                char jb[1024];
                mps3_status_json(&st, jb, sizeof(jb));
                char tmp[300];
                snprintf(tmp, sizeof(tmp), "%s.tmp", json_out);
                FILE *f = fopen(tmp, "w");
                if (f) {
                    fprintf(f, "%s\n", jb);
                    fclose(f);
                    rename(tmp, json_out);
                }
            }
        }

        clcd_core_poll(t);

        if (once && clcd_core_state() == CLCD_ST_IDLE && clcd_core_dirty_count() == 0) {
            if (painted_once)
                break;
            painted_once = 1;   /* one more refresh cycle settles the frame */
        }

        /* Pacing: mid-render (budget fully spent) keep the FIFO fed with a
         * short sleep; idle waits are refresh-scale. The FIFO drains at
         * ~9 MB/s ceiling, so 1 ms between 4 KiB passes cannot overrun it. */
        if (clcd_core_state() == CLCD_ST_RENDER ||
            clcd_core_state() == CLCD_ST_INIT ||
            clcd_core_bytes_last_pass() > 0)
            sleep_ms(1);
        else
            sleep_ms(20);
    }

    /* Leave the panel as-is (lit, last frame). Deliberate: the display is a
     * status surface, not a session — a daemon restart must not black it. */
    mps3_uio_close(&uio);
    return 0;
}
