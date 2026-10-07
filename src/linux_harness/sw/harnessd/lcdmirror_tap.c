/*
 * lcdmirror_tap.c -- the LCD mirror's harnessd half (LCD_MIRROR_FPGA.md §6-§7,
 * the INTERIM software-only mode: no mint).
 *
 *   1. THE TAP. hal_front.c shows this file every CLCD / CLCDKVM access the
 *      service modules make (hal.h "THE CLCD TAP"). CLCD CMD/DATA writes are the
 *      8080 bytes the panel receives; they go into the GRAM model
 *      (lcdmirror_model.c), which keeps the snooper's aperture layout in a shared
 *      file (/dev/shm/mps3-lcdmirror). The panel's RST and BL pads come from
 *      whichever CTRL register drives them (CLCD CTRL, or CLCDKVM CTRL once
 *      bl_rst_src=1), the owner and the KVM's own reset pulses from the CLCDKVM
 *      STATUS/EVENT reads clcd.c makes every pass anyway. Zero extra MMIO.
 *   2. OWNER. While the DUT owns the panel (or the KVM drives the pads mid-
 *      handover) the harness's bytes do not reach the glass and the DUT's cannot
 *      be seen: the mirror is BLIND -- VALID all 0, bytes not modelled. On the way
 *      back the KVM resets the panel (EVENT harness_gained / panel_reset_done):
 *      the model resets, and clcd_regain()'s re-init + full repaint refill it.
 *      Everything else is `text_only`: exact for what clcd.c draws.
 *   3. THE SERVER is a child, mps3-lcdmirror (lcdmirror_main.c), at nice 10: the
 *      encoding and the TCP sends never run in this watchdog-critical loop. Here,
 *      once a pass, the model PUBLISHES (a few dozen stores, only when something
 *      changed) and the child is supervised (a WNOHANG reap at most every 200 ms,
 *      a respawn with backoff). The per-pass cost is the tap (~one decode per
 *      CLCD byte, fed a whole run at a time by clcd.c's bus seam -- tap_run(),
 *      hal.h THE CLCD BULK TAP: <= CLCD_BYTES_PER_PASS bytes per clcd_poll(), and
 *      a harnessd pass runs clcd_poll_drain() -- clcd_poll() repeated within
 *      main_linux.c HARNESSD_CLCD_DRAIN_US, 12 ms, 1 ms while a UART client is
 *      connected -- so the tap's time is INSIDE the drain's budget, it only
 *      lowers the bytes a pass) + the publish; both are measured and written to
 *      the shared file (LCDM_SW_PUB_MAX_NS, LCDM_SW_PASS_BYTES: bytes per PASS,
 *      i.e. per drain).
 *   4. THE CONTRACT FIELDS: version.features "lcd_mirror", version.lcd_mirror,
 *      stats.lcd_mirror -- the net_proto.h v0.15 engine seams, strong here.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "../../../../firmware/clcd/clcd.h"        /* CLCD_BYTES_PER_PASS */
#include "../../../../firmware/common/net_proto.h"
#include "../../../../firmware/common/platform_regs.h"
#include "../../../../firmware/common/timebase.h"
#include "../../../../firmware/coordinator/coordinator.h"
#include "hal.h"
#include "harnessd.h"
#include "lcdmirror.h"

#if defined(MPS3_HAS_CLCD)

#define REAP_PERIOD_US   200000u
#define BACKOFF_MIN_MS   1000u
#define BACKOFF_MAX_MS   30000u
#define STABLE_RUN_US    60000000ull

static struct {
    int                enabled;
    volatile uint32_t *ap;
    lcdm_model_t       m;
    char               shm[PATH_MAX];
    char               child_path[PATH_MAX];
    pid_t              child;
    uint64_t           spawned_us, next_spawn_us, last_reap_us;
    unsigned           backoff_ms;
    int                exec_fail_logged;
    /* the tap's view of the pads */
    uint32_t           clcd_ctrl, kvm_ctrl;
    int                blind, rst_status_prev, rst_modelled;
    uint32_t           rst_modelled_ms;     /* when a KVM-sequenced reset was modelled */
    uint32_t           model_ps_per_byte;   /* the start-up calibration                */
    uint32_t           pass_bytes, pass_bytes_max, tap_bytes;
    uint32_t           pub_max_ns;
} S;

static uint64_t mono_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

#define SW(off) (S.ap[(off) / 4u])

/* ---- the pads -------------------------------------------------------------- */
#define KVM_CTRL_RW (CLCDKVM_CTRL_FORCE_HARNESS | CLCDKVM_CTRL_TIMEOUT_EN | CLCDKVM_CTRL_PB_EN | \
                     CLCDKVM_CTRL_DUT_REQ_EN | CLCDKVM_CTRL_BACKLIGHT | CLCDKVM_CTRL_PANEL_RST_N | \
                     CLCDKVM_CTRL_BL_RST_SRC)

/* CLCD_RST / CLCD_BL follow clcd_0's CTRL[2]/[1] until the KVM takes them
 * (CLCDKVM CTRL.bl_rst_src = 1), then CLCDKVM CTRL[6]/[5] (platform_regs.h). */
static void pads_update(void)
{
    int kvm = (S.kvm_ctrl & CLCDKVM_CTRL_BL_RST_SRC) != 0u;
    int held = kvm ? !(S.kvm_ctrl & CLCDKVM_CTRL_PANEL_RST_N) : !(S.clcd_ctrl & CLCD_CTRL_RESET_N);
    int bl = kvm ? (S.kvm_ctrl & CLCDKVM_CTRL_BACKLIGHT) != 0u : (S.clcd_ctrl & CLCD_CTRL_BACKLIGHT) != 0u;
    if (held != (int)S.m.in_reset) {
        lcdm_model_set_reset(&S.m, held);   /* a CTRL level: no KVM sequence, no EVENT */
    }
    lcdm_model_set_live(&S.m, LCDM_ST_BL, bl ? LCDM_ST_BL : 0u);
}

static void set_blind(int blind)
{
    if (blind == S.blind) {
        return;
    }
    S.blind = blind;
    if (blind) {
        lcdm_model_invalidate(&S.m);   /* what the DUT draws is not ours to see */
    }
    SW(LCDM_SW_BLIND) = (uint32_t)blind;
}

static void kvm_status(uint32_t st)
{
    int rst = (st & CLCDKVM_STATUS_PANEL_RST_ACTIVE) != 0u;
    if (rst && !S.rst_status_prev && !S.m.in_reset) {
        lcdm_model_pulse_reset(&S.m);   /* a pulse we did not command: the KVM's */
        S.rst_modelled = 1;
        S.rst_modelled_ms = mps3_sys_now_ms();
    }
    S.rst_status_prev = rst;
    lcdm_model_set_live(&S.m, LCDM_ST_OWNER, (st & CLCDKVM_STATUS_OWNER) ? LCDM_ST_OWNER : 0u);
    set_blind((st & (CLCDKVM_STATUS_OWNER | CLCDKVM_STATUS_KVM_DRIVES_PADS)) != 0u);
}

static void kvm_event(uint32_t ev)
{
    if (!(ev & CLCDKVM_EVENT_REINIT_MASK)) {
        return;
    }
    /* A completed reset SEQUENCE (every handover passes through S_RST). If the
     * tap modelled its start in the last second (STATUS.panel_rst_active seen, or
     * our own PANEL_RST_PULSE), do not count it twice. */
    if (S.rst_modelled && (uint32_t)(mps3_sys_now_ms() - S.rst_modelled_ms) < 1000u) {
        S.rst_modelled = 0;
    } else {
        S.rst_modelled = 0;
        lcdm_model_pulse_reset(&S.m);
    }
}

static void tap(uintptr_t base, uintptr_t off, uint32_t val, int is_write)
{
    if (base == MPS3_CLCD_BASE) {
        if (!is_write) {
            return;
        }
        if (off == CLCD_CMD || off == CLCD_DATA) {
            S.pass_bytes++;
            if (!S.blind) {
                lcdm_model_byte(&S.m, off == CLCD_DATA, (uint8_t)val);
            }
        } else if (off == CLCD_CTRL) {
            S.clcd_ctrl = val;
            pads_update();
        }
        return;
    }
    /* MPS3_CLCDKVM_BASE */
    if (is_write) {
        if (off == CLCDKVM_CTRL) {
            S.kvm_ctrl = val & KVM_CTRL_RW;
            if (val & CLCDKVM_CTRL_PANEL_RST_PULSE) {
                lcdm_model_pulse_reset(&S.m);
                S.rst_modelled = 1;
                S.rst_modelled_ms = mps3_sys_now_ms();
            }
            pads_update();
        }
        return;
    }
    if (off == CLCDKVM_STATUS) {
        kvm_status(val);
    } else if (off == CLCDKVM_EVENT) {
        kvm_event(val);
    }
}

/* THE BULK TAP (hal.h; lane CLCD-SPEED): a run of CLCD CMD/DATA writes that
 * clcd.c's bus seam (hal_front.c clcd_bus_push) has just made, in bus order --
 * the same thing as tap() shown each of them, one call instead of n. BLIND
 * cannot change inside a run (only a CLCDKVM STATUS read moves it). */
static void tap_run(const uint8_t *rs, const uint8_t *val, uint32_t n)
{
    S.pass_bytes += n;
    if (!S.blind) {
        lcdm_model_bytes(&S.m, rs, val, n);
    }
}

/* ---- the child -------------------------------------------------------------- */
static void spawn(void)
{
    char port[16], ppid[16];
    const char *argv[12];
    int a = 0;
    snprintf(port, sizeof(port), "%u", (unsigned)(LCDM_PORT + (unsigned)g_hd.port_offset));
    snprintf(ppid, sizeof(ppid), "%d", (int)getpid());
    argv[a++] = S.child_path;
    argv[a++] = "--shm";
    argv[a++] = S.shm;
    argv[a++] = "--port";
    argv[a++] = port;
    argv[a++] = "--parent";
    argv[a++] = ppid;
#ifdef MPS3_HAL_MOCK
    if (g_hd.mock_trusted_peer) {
        argv[a++] = "--trusted-peer";   /* host tests: a "remote" peer on loopback */
        argv[a++] = g_hd.mock_trusted_peer;
    }
#endif
    argv[a] = 0;

    pid_t parent = getpid();
    pid_t pid = fork();
    if (pid < 0) {
        harnessd_log("lcd mirror: fork: %s -- retry in %u ms\n", strerror(errno), S.backoff_ms);
        S.next_spawn_us = harnessd_now_us64() + (uint64_t)S.backoff_ms * 1000u;
        return;
    }
    if (pid == 0) {
        /* Hold NOTHING of harnessd's: a respawned harnessd must be able to bind
         * every port while this child is still exiting (slot_linux.c's rule). */
        long maxfd = sysconf(_SC_OPEN_MAX);
        if (maxfd < 0 || maxfd > 4096) maxfd = 4096;
        for (int fd = 3; fd < maxfd; fd++) {
            close(fd);
        }
        signal(SIGTERM, SIG_DFL);
        signal(SIGINT, SIG_DFL);
        signal(SIGHUP, SIG_DFL);
        (void)prctl(PR_SET_PDEATHSIG, SIGTERM);   /* harnessd gone => the mirror goes */
        if (getppid() != parent) {
            _exit(0);
        }
        (void)!nice(10);                           /* never the service loop's CPU */
        execv(S.child_path, (char *const *)argv);
        _exit(127);
    }
    S.child = pid;
    S.spawned_us = harnessd_now_us64();
    S.last_reap_us = S.spawned_us;
    harnessd_log("lcd mirror: %s pid %d serving 127.0.0.1:%s (nice 10, mode sw)\n",
                 S.child_path, (int)pid, port);
}

static void supervise(void)
{
    uint64_t now = harnessd_now_us64();
    if (S.child > 0) {
        if (now - S.last_reap_us < REAP_PERIOD_US) {
            return;
        }
        S.last_reap_us = now;
        int st = 0;
        pid_t r = waitpid(S.child, &st, WNOHANG);
        if (r != S.child) {
            return;
        }
        S.child = -1;
        if (now - S.spawned_us >= STABLE_RUN_US) {
            S.backoff_ms = BACKOFF_MIN_MS;
        } else {
            S.backoff_ms = S.backoff_ms ? S.backoff_ms * 2u : BACKOFF_MIN_MS;
            if (S.backoff_ms > BACKOFF_MAX_MS) S.backoff_ms = BACKOFF_MAX_MS;
        }
        if (WIFEXITED(st) && WEXITSTATUS(st) == 127) {
            if (!S.exec_fail_logged) {
                S.exec_fail_logged = 1;
                harnessd_log("lcd mirror: cannot run %s (exit 127) -- retrying every <= %u s, "
                             "quietly\n", S.child_path, BACKOFF_MAX_MS / 1000u);
            }
        } else {
            harnessd_log("lcd mirror: child %s %d -- respawn in %u ms\n",
                         WIFSIGNALED(st) ? "killed by signal" : "exited",
                         WIFSIGNALED(st) ? WTERMSIG(st) : WEXITSTATUS(st), S.backoff_ms);
        }
        S.next_spawn_us = now + (uint64_t)S.backoff_ms * 1000u;
        return;
    }
    if (now >= S.next_spawn_us) {
        spawn();
    }
}

/* ---- the start-up calibration ------------------------------------------------ */
/* The model's cost per 8080 byte ON THIS MACHINE (the MBV on a board): 8 glyph
 * cells exactly as clcd.c streams them (window, 0x22, 256 pixel bytes, RGB565 as
 * the init table leaves it) through a scratch model, each cell as ONE bulk-tap
 * run (tap_run: the path clcd_bus_push feeds), best of 3. The tap's cost per
 * clcd_poll() is then at most CLCD_BYTES_PER_PASS x this; a harnessd pass drains
 * several clcd_poll()s, and the tap's time counts inside that drain's time
 * budget (main_linux.c HARNESSD_CLCD_DRAIN_US). The publish is extra, once a
 * pass (measured live). Once, at start, a few ms on the MBV. */
static uint32_t calibrate(void)
{
    void *mem = calloc(1, LCDM_APERTURE);
    if (!mem) {
        return 0;
    }
    lcdm_model_t m;
    lcdm_model_init(&m, (volatile uint32_t *)mem, 1);
    lcdm_model_byte(&m, 0, 0x17);
    lcdm_model_byte(&m, 1, 0x05);          /* COLMOD RGB565, as hx8347_init leaves it */
    uint64_t best = ~0ull;
    unsigned nbytes = 0;
    uint8_t rs[16u + 1u + 256u], val[16u + 1u + 256u];
    for (int rep = 0; rep < 3; rep++) {
        nbytes = 0;
        uint64_t t0 = mono_ns();
        for (unsigned cell = 0; cell < 8u; cell++) {
            unsigned x0 = cell * 8u, x1 = x0 + 7u;
            const uint8_t win[16] = { 0x02, (uint8_t)(x0 >> 8), 0x03, (uint8_t)x0,
                                      0x04, (uint8_t)(x1 >> 8), 0x05, (uint8_t)x1,
                                      0x06, 0, 0x07, 0, 0x08, 0, 0x09, 15 };
            unsigned n = 0;
            for (unsigned i = 0; i < 16u; i++, n++) { rs[n] = (uint8_t)(i & 1u); val[n] = win[i]; }
            rs[n] = 0; val[n] = 0x22; n++;
            for (unsigned i = 0; i < 256u; i++, n++) { rs[n] = 1; val[n] = (uint8_t)((i & 16u) ? 0xFF : 0x00); }
            lcdm_model_bytes(&m, rs, val, n);
            nbytes += n;
        }
        uint64_t dt = mono_ns() - t0;
        if (dt < best) best = dt;
    }
    free(mem);
    return nbytes ? (uint32_t)(best * 1000u / nbytes) : 0u;   /* ps per byte */
}

/* ---- lifecycle ------------------------------------------------------------- */
static int resolve_child(void)
{
    if (g_hd.lcdmirror) {
        snprintf(S.child_path, sizeof(S.child_path), "%s", g_hd.lcdmirror);
        return 0;
    }
    /* Default: beside this binary (/usr/sbin on the image, build/<variant>/ on a
     * host), so the image package and the host tests need no flag. */
    char self[PATH_MAX - 32];
    ssize_t n = readlink("/proc/self/exe", self, sizeof(self) - 1u);
    if (n <= 0) {
        return -1;
    }
    self[n] = '\0';
    char *slash = strrchr(self, '/');
    if (!slash) {
        return -1;
    }
    *slash = '\0';
    snprintf(S.child_path, sizeof(S.child_path), "%s/mps3-lcdmirror", self);
    return 0;
}

void harnessd_lcdmirror_init(void)
{
    memset(&S, 0, sizeof(S));
    S.child = -1;
    if (g_hd.lcdmirror && strcmp(g_hd.lcdmirror, "none") == 0) {
        harnessd_log("lcd mirror: disabled (--lcdmirror none)\n");
        return;
    }
    if (resolve_child() != 0) {
        harnessd_log("lcd mirror: cannot locate mps3-lcdmirror -- disabled\n");
        return;
    }
    /* No server binary, no mirror: Harness Manager gates its live view on
     * version.features "lcd_mirror", so an image (or a hand-copied harnessd)
     * without mps3-lcdmirror beside it must not advertise one. */
    if (access(S.child_path, X_OK) != 0) {
        harnessd_log("lcd mirror: %s: %s -- disabled\n", S.child_path, strerror(errno));
        return;
    }
    if (g_hd.lcdmirror_shm) {
        snprintf(S.shm, sizeof(S.shm), "%s", g_hd.lcdmirror_shm);
    } else if (g_hd.port_offset) {
        /* a test harness: never share the product path between parallel runs */
        snprintf(S.shm, sizeof(S.shm), "/dev/shm/mps3-lcdmirror.%d", g_hd.port_offset);
    } else {
        snprintf(S.shm, sizeof(S.shm), "/dev/shm/mps3-lcdmirror");
    }
    int fd = open(S.shm, O_RDWR | O_CREAT | O_CLOEXEC, 0600);
    if (fd < 0) {
        harnessd_log("lcd mirror: open %s: %s -- disabled\n", S.shm, strerror(errno));
        return;
    }
    struct stat sb;
    int existed = fstat(fd, &sb) == 0 && sb.st_size >= (off_t)LCDM_APERTURE;
    if (!existed && ftruncate(fd, (off_t)LCDM_APERTURE) != 0) {
        harnessd_log("lcd mirror: ftruncate %s: %s -- disabled\n", S.shm, strerror(errno));
        close(fd);
        return;
    }
    void *p = mmap(0, LCDM_APERTURE, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    close(fd);
    if (p == MAP_FAILED) {
        harnessd_log("lcd mirror: mmap %s: %s -- disabled\n", S.shm, strerror(errno));
        return;
    }
    S.ap = (volatile uint32_t *)p;
    int keep_fb = existed && SW(LCDM_SW_MAGIC) == LCDM_SW_MAGIC_VALUE;
    S.model_ps_per_byte = calibrate();
    lcdm_model_init(&S.m, S.ap, keep_fb);
    SW(LCDM_SW_VERSION) = LCDM_SW_VERSION_VALUE;
    SW(LCDM_SW_WRITER) = (uint32_t)getpid();
    SW(LCDM_SW_GEN) = SW(LCDM_SW_GEN) + 1u;
    SW(LCDM_SW_STATIC_ID) = g_shell_state.static_id;
    SW(LCDM_SW_MODE) = LCDM_MODE_SW;
    SW(LCDM_SW_BLIND) = 0u;
    SW(LCDM_SW_PUB_MAX_NS) = 0u;
    SW(LCDM_SW_PASS_BYTES) = 0u;
    SW(LCDM_SW_TAP_BYTES) = 0u;
    SW(LCDM_SW_MODEL_PS) = S.model_ps_per_byte;
    SW(LCDM_SV_MAGIC) = 0u;           /* no child yet */
    __atomic_thread_fence(__ATOMIC_RELEASE);
    SW(LCDM_SW_MAGIC) = LCDM_SW_MAGIC_VALUE;

    /* The pads as FOUND (a respawn keeps a live panel): two quiet reads, once. */
    S.clcd_ctrl = hal_quiet_read32(MPS3_CLCD_BASE, CLCD_CTRL);
#ifdef MPS3_HAS_CLCD_KVM
    S.kvm_ctrl = hal_quiet_read32(MPS3_CLCDKVM_BASE, CLCDKVM_CTRL) & KVM_CTRL_RW;
    kvm_status(hal_quiet_read32(MPS3_CLCDKVM_BASE, CLCDKVM_STATUS));
#endif
    pads_update();
    lcdm_model_publish(&S.m);
    hal_set_tap(tap);
    hal_set_tap_bytes(tap_run);       /* clcd_bus_push's runs (hal.h) */
    S.enabled = 1;
    S.backoff_ms = BACKOFF_MIN_MS;
    harnessd_log("lcd mirror: interim software mode -- the CLCD tap feeds %s%s; model %u.%03u "
                 "ns/byte => <= %u us of tap per clcd_poll (%u B), inside the clcd drain's "
                 "per-pass budget; + the publish\n", S.shm,
                 keep_fb ? " (frame kept from the last run)" : "",
                 S.model_ps_per_byte / 1000u, S.model_ps_per_byte % 1000u,
                 (S.model_ps_per_byte * (uint32_t)CLCD_BYTES_PER_PASS + 999999u) / 1000000u,
                 (unsigned)CLCD_BYTES_PER_PASS);
    spawn();
}

void harnessd_lcdmirror_poll(void)
{
    if (!S.enabled) {
        return;
    }
    if (S.pass_bytes) {
        S.tap_bytes += S.pass_bytes;
        if (S.pass_bytes > S.pass_bytes_max) {
            S.pass_bytes_max = S.pass_bytes;
            SW(LCDM_SW_PASS_BYTES) = S.pass_bytes_max;
        }
        SW(LCDM_SW_TAP_BYTES) = S.tap_bytes;
        S.pass_bytes = 0;
    }
    if (S.m.changed) {
        uint64_t t0 = mono_ns();
        lcdm_model_publish(&S.m);
        uint64_t dt = mono_ns() - t0;
        if (dt > S.pub_max_ns) {
            S.pub_max_ns = dt > 0xFFFFFFFFull ? 0xFFFFFFFFu : (uint32_t)dt;
            SW(LCDM_SW_PUB_MAX_NS) = S.pub_max_ns;
        }
    }
    supervise();
}

void harnessd_lcdmirror_stop(void)
{
    if (!S.enabled) {
        return;
    }
    hal_set_tap_bytes(0);
    hal_set_tap(0);
    if (S.child > 0) {
        kill(S.child, SIGTERM);
        for (int i = 0; i < 100; i++) {
            if (waitpid(S.child, 0, WNOHANG) == S.child) {
                S.child = -1;
                break;
            }
            struct timespec ts = { 0, 10000000L };
            nanosleep(&ts, 0);
        }
        if (S.child > 0) {
            kill(S.child, SIGKILL);
            (void)waitpid(S.child, 0, 0);
            S.child = -1;
        }
    }
    S.enabled = 0;
}

/* ---- the contract fields (net_proto.h v0.15 engine seams) ----------------------- */
/* version.features "lcd_mirror": mps3_proto_features_extra() is identity_linux.c's
 * now (it lists every engine name, "identity" too, in one fixed order); it asks
 * this. */
int harnessd_lcdmirror_enabled(void)
{
    return S.enabled;
}

int mps3_proto_lcd_mirror_info(mps3_lcd_mirror_info_t *out)
{
    if (!S.enabled) {
        return 0;
    }
    out->port = (uint16_t)LCDM_PORT;
    out->proto = (uint8_t)LCDM_PROTO;
    snprintf(out->mode, sizeof(out->mode), "sw");
    return 1;
}

int mps3_proto_lcd_mirror_stats(mps3_lcd_mirror_stats_t *out)
{
    if (!S.enabled) {
        return 0;
    }
    memset(out, 0, sizeof(*out));
    if (S.child <= 0 || SW(LCDM_SV_MAGIC) != LCDM_SV_MAGIC_VALUE) {
        return 1;                              /* no server: no peer */
    }
    uint32_t clients = 0, since = 0, fps = 0, bytes = 0;
    char peer[LCDM_SV_PEER_LEN];
    for (int tries = 0; tries < 8; tries++) {
        uint32_t s0 = __atomic_load_n((uint32_t *)&S.ap[LCDM_SV_SEQ / 4u], __ATOMIC_ACQUIRE);
        if (s0 & 1u) {
            continue;
        }
        clients = SW(LCDM_SV_CLIENTS);
        since = SW(LCDM_SV_SINCE_MS);
        fps = SW(LCDM_SV_FPS_X10);
        bytes = SW(LCDM_SV_BYTES);
        for (unsigned i = 0; i < LCDM_SV_PEER_LEN; i++) {
            peer[i] = (char)((volatile uint8_t *)S.ap)[LCDM_SV_PEER + i];
        }
        peer[LCDM_SV_PEER_LEN - 1u] = '\0';
        __atomic_thread_fence(__ATOMIC_ACQUIRE);
        if (__atomic_load_n((uint32_t *)&S.ap[LCDM_SV_SEQ / 4u], __ATOMIC_ACQUIRE) == s0) {
            break;
        }
        clients = 0;
    }
    if (clients == 0u) {
        return 1;
    }
    /* `since` in stats.up_ms units: the child writes CLOCK_MONOTONIC ms. */
    uint32_t mono_ms = (uint32_t)(mono_ns() / 1000000ull);
    uint32_t up_ms = mps3_sys_now_ms();
    uint32_t base = mono_ms - up_ms;           /* mono ms at harnessd's up_ms 0 */
    int32_t rel = (int32_t)(since - base);
    snprintf(out->peer, sizeof(out->peer), "%s", peer);
    out->since_ms = rel > 0 ? (uint32_t)rel : 0u;
    out->fps_x10 = fps;
    out->bytes = bytes;
    return 1;
}

#else  /* !MPS3_HAS_CLCD: nothing to mirror (the ctrl_echo twin) */

void harnessd_lcdmirror_init(void) { }
void harnessd_lcdmirror_poll(void) { }
void harnessd_lcdmirror_stop(void) { }
int  harnessd_lcdmirror_enabled(void) { return 0; }

#endif
