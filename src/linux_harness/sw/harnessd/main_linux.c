/*
 * main_linux.c — mps3-harnessd: the MPS3 shell firmware's service modules as
 * ONE Linux process on the MicroBlaze V (docs/planning/linux_lanes/
 * HARNESSD_CONTRACT.md is the normative description; this header is the tour).
 *
 * WHAT THIS FILE IS: firmware/platform/src/main.c's job, done for Linux. Same
 * init order, same service TABLE (firmware/common/service.c, unmodified), same
 * budgets and sick policy, same control-channel interleave. What is different
 * is only what the kernel now does for us:
 *
 *   - no net_rx / net_tmr / tx_drain services: the kernel's TCP/IP stack and
 *     its smsc911x driver own eth0, and the modules' sockets are kernel sockets
 *     (firmware/test/posix_net_if.c). Table slots 0..2 carry the three
 *     Linux-side services instead (identify, /dev/kmsg tail, resident-clearing
 *     persistence), so indices 3..11 — which are MAILBOX CONTRACT — are the
 *     same services on both engines.
 *   - no busy superloop. A pass that did no work SLEEPS in poll() on every
 *     socket (plan §6 risk 4: one 100 MHz hart also runs the kernel, dropbear
 *     and whoever is SSH'd in). "Work" = network bytes moved or a service
 *     module wrote a register (hal.h's work counter). The cap on the sleep is
 *     10 ms (2 ms while a UART-bridge client is connected — the bridge's 16-byte
 *     SWO FIFO drops newest), which is also the cadence the touch sampler and
 *     the CLCD refresh were written for.
 *   - the watchdog is ARMED at start and kicked from the end of every healthy
 *     pass (service.c's THE KICK), so a hung or stopped harnessd resets the
 *     board (B1 item 7) — and DISARMED on a clean stop, so stopping it on
 *     purpose never does.
 *   - identity is FABRIC-bound (identity.c), restarts are SAFE (the respawn
 *     rules in ovlstore_linux.c and hal.h's reset shield), and a healthy start
 *     CONFIRMS the stage0 boot.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <getopt.h>
#include <inttypes.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/reboot.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#include "../../../../firmware/common/diag.h"
#include "../../../../firmware/common/log_ring.h"
#include "../../../../firmware/common/net_if.h"
#include "../../../../firmware/common/net_proto.h"
#include "../../../../firmware/common/platform_regs.h"
#include "../../../../firmware/common/service.h"
#include "../../../../firmware/common/timebase.h"
#include "../../../../firmware/config_agent/config_agent.h"
#include "../../../../firmware/coordinator/coordinator.h"
#include "../../../../firmware/coordinator/swap_fsm.h"
#include "../../../../firmware/overlay_store/overlay_store.h"
#include "../../../../firmware/identify/identify.h"
#include "../../../../firmware/jtag_server/jtag_server.h"
#include "../../../../firmware/uart_over_eth/uart_over_eth.h"
#include "../../../../firmware/xvc_server/xvc_server.h"
#include "../../../../firmware/test/posix_net_if.h"
#include "identity_core.h"
#ifdef MPS3_HAS_CLCD
#include "../../../../firmware/clcd/clcd.h"
#endif
#ifdef MPS3_HAS_TOUCH
#include "../../../../firmware/touch/touch.h"
#endif
#include "../../../linux_soc/hw/fw_stage0/stage0_status.h"
#include "hal.h"
#include "harnessd.h"
#include "lcdmirror.h"

#ifdef MPS3_HAL_MOCK
/* hal_mock.c's seeding hooks (mock builds only). */
void hal_mock_seed_usr_access(uint32_t v, int valid);
void hal_mock_poke(uintptr_t base, uintptr_t off, uint32_t val);
void hal_mock_set_wdog_absent(void);
#endif

#ifndef HARNESSD_BUILD
#define HARNESSD_BUILD "unversioned"
#endif

/* THE USER microSD -- ONE card, TWO writers in this process: D13's overlay
 * store (ovlstore_linux.c, the `usd` verb + commit) and stage0's boot slots
 * (slot_linux.c, the `slot` verb + the kind-2 push). Both open the WHOLE disk and
 * each keeps to its own MBR partitions, so they must be the SAME disk: ONE flag
 * names it (--card) and both read g_hd.card. --usd-dev (D13) and --slot-disk
 * (v0.14) stay as aliases; every spelling given must name the same device
 * (card_same()), else harnessd refuses to start (lane HARDEN, 2026-09-24: the
 * two used to be separate flags with separate defaults).
 *
 * The default: /dev/mmcblk0 on the rv32 PRODUCT build only (the Makefile's
 * -DHARNESSD_CARD_DEV); every host / QEMU / mock build defaults to NO card, so a
 * binary run by hand on a workstation never finds that machine's own SD reader
 * behind a 6910 push or a `usd format`. Tests pass --card <temp image>. */
#if !defined(HARNESSD_CARD_DEV) && defined(HARNESSD_USD_DEV)
#define HARNESSD_CARD_DEV HARNESSD_USD_DEV     /* the pre-2026-09-24 spelling */
#endif
#ifdef HARNESSD_CARD_DEV
#define HARNESSD_CARD_DEFAULT      HARNESSD_CARD_DEV
#define HARNESSD_CARD_DEFAULT_TEXT HARNESSD_CARD_DEV
#else
#define HARNESSD_CARD_DEFAULT      NULL
#define HARNESSD_CARD_DEFAULT_TEXT "none"
#endif

harnessd_cfg_t g_hd;

static const char k_default_greybox[] = "/etc/mps3/greybox_clear.bin";
static const char k_default_netif[]   = "eth0";

static volatile sig_atomic_t s_stop;
static int s_wdog_armed;

/* ==========================================================================
 * The watchdog
 * ========================================================================== */
#define WDOG_KICK_PERIOD_US 250000u   /* << the shortest stage in any build (1.34 s) */

static int wdog_present(void)
{
    uint32_t a = hal_quiet_read32(MPS3_WDOG_BASE, WDOG_TBR);
    for (int i = 0; i < 64; i++) {
        if (hal_quiet_read32(MPS3_WDOG_BASE, WDOG_TBR) != a) {
            return 1;
        }
    }
    return 0;
}

static void wdog_arm(void)
{
    uint32_t csr0 = hal_quiet_read32(MPS3_WDOG_BASE, WDOG_TWCSR0);
    if (csr0 & WDOG_TWCSR0_WRS) {
        harnessd_log("wdog: the LAST RESET WAS THE WATCHDOG (TWCSR0.WRS) -- cleared\n");
    }
    if (!wdog_present()) {
        harnessd_log("wdog: no free-running timebase at 0x%08x -- no watchdog in this "
                     "fabric; not armed\n", (unsigned)MPS3_WDOG_BASE);
        return;
    }
    if (!g_hd.wdog) {
        /* stage0 ARMS the watchdog at hand-off (SHELL/STAGE0 contracts) so a
         * kernel that dies before harnessd still resets. "--wdog off" must
         * therefore actively DISARM it, or the board resets ~43 s later. */
        hal_quiet_write32(MPS3_WDOG_BASE, WDOG_TWCSR0, WDOG_TWCSR0_WDS);
        hal_quiet_write32(MPS3_WDOG_BASE, WDOG_TWCSR1, 0u);
        harnessd_log("wdog: DISARMED by --wdog off (a hang will NOT reset the board)\n");
        return;
    }
    /* Both enables (PG128: EWDT2 in TWCSR1 AND EWDT1 in TWCSR0), clearing the
     * stale status (W1C WRS/WDS) in the SAME TWCSR0 write: stage0 normally armed
     * it already, and a write with EWDT1 = 0 would disarm it for an instant. */
    hal_quiet_write32(MPS3_WDOG_BASE, WDOG_TWCSR1, WDOG_TWCSR1_EWDT2);
    hal_quiet_write32(MPS3_WDOG_BASE, WDOG_TWCSR0,
                      WDOG_TWCSR0_EWDT1 | WDOG_TWCSR0_WRS | WDOG_TWCSR0_WDS);
    s_wdog_armed = 1;
    harnessd_log("wdog: ARMED (stage %u ms: reset ~%u ms after the last healthy pass)\n",
                 (unsigned)MPS3_WDT_STAGE_MS, 2u * (unsigned)MPS3_WDT_STAGE_MS);
}

static void wdog_disarm(void)
{
    if (!s_wdog_armed) {
        return;
    }
    hal_quiet_write32(MPS3_WDOG_BASE, WDOG_TWCSR0, WDOG_TWCSR0_WDS);   /* EWDT1 = 0 */
    hal_quiet_write32(MPS3_WDOG_BASE, WDOG_TWCSR1, 0u);                /* EWDT2 = 0 */
    s_wdog_armed = 0;
    harnessd_log("wdog: disarmed (clean stop)\n");
}

/* service.h's THE KICK seam — the strong definition. service.c calls it only at
 * the end of a pass whose vital services are healthy, and never after `reboot`
 * inhibited it. Rate-limited so an idle loop does not make an AXI write per
 * pass; a housekeeping write, so it does not count as work. */
void mps3_wdt_kick(void)
{
    static uint64_t last;
    if (!s_wdog_armed) {
        return;
    }
    uint64_t now = harnessd_now_us64();
    if (now - last < WDOG_KICK_PERIOD_US) {
        return;
    }
    last = now;
    uint32_t csr0 = hal_quiet_read32(MPS3_WDOG_BASE, WDOG_TWCSR0);
    hal_quiet_write32(MPS3_WDOG_BASE, WDOG_TWCSR0, (csr0 & WDOG_TWCSR0_EWDT1) | WDOG_TWCSR0_WDS);
}

/* THE EARLY KICK (STAGE0_CONTRACT §4, SHELL_CONTRACT §6). stage0 ARMS the
 * watchdog before it jumps to Linux (C_WDT_INTERVAL=31 on the MBV: reset ~43 s
 * after the last kick), so a kernel that dies before harnessd still resets the
 * board. harnessd must therefore kick as soon as it has a register window —
 * BEFORE any slow init — and keep kicking between the init steps. It never
 * disarms before this first kick. The first one is logged with the OS uptime,
 * which is B1's measure of the jump-to-first-kick margin. */
static int s_first_kick_logged;

/* EWDT1 only: TWCSR1.EWDT2 is WRITE-ONLY on the AXI Timebase WDT and reads 0.
 * Gating on it made every early kick a silent no-op on silicon (B1 v4: no
 * FIRST KICK line; B2 2026-09-26: the RC2 rescue boot's slower init -- the
 * uSD store's power-on decision -- ran past stage0's 42.9 s and the watchdog
 * reset the board before wdog_arm). The main loop's kick (mps3_wdt_kick) keeps
 * EWDT1 from its read-back, and B1's watchdog drill passed, so EWDT1 does
 * read back. */
static int wdog_is_armed(void)
{
    return (hal_quiet_read32(MPS3_WDOG_BASE, WDOG_TWCSR0) & WDOG_TWCSR0_EWDT1) != 0u;
}

static void wdog_touch(const char *where)
{
    if (!wdog_is_armed()) {
        return;
    }
    uint32_t csr0 = hal_quiet_read32(MPS3_WDOG_BASE, WDOG_TWCSR0);
    hal_quiet_write32(MPS3_WDOG_BASE, WDOG_TWCSR0, (csr0 & WDOG_TWCSR0_EWDT1) | WDOG_TWCSR0_WDS);
    if (!s_first_kick_logged) {
        uint32_t os_ms = 0u;
        s_first_kick_logged = 1;
        (void)mps3_proto_os_up_ms(&os_ms);
        harnessd_log("wdog: FIRST KICK at os_up_ms=%u (harnessd +%u ms, %s): the watchdog "
                     "was already armed (stage0)\n", (unsigned)os_ms,
                     (unsigned)(harnessd_now_us64() / 1000u), where);
    }
}

/* ==========================================================================
 * Services that are the platform's (main.c had the bare-metal twins)
 * ========================================================================== */
#define HEARTBEAT_GPIO_BIT (1u << 0)
#define HEARTBEAT_HALF_MS  500u

static void svc_kmsg(void)
{
    if (g_hd.kmsg) {
        harnessd_kmsg_poll();
    }
}

/* THE CLCD DRAIN (lane CLCD-SLOW; clcd.h clcd_poll_drain()). One clcd_poll() a
 * pass is one glyph a pass -- bare metal's pacing for its ~0.7 ms superloop; a
 * harnessd pass also carries ~50 syscalls, so a page change painted glyph by
 * glyph. Drain up to HARNESSD_CLCD_DRAIN_US a pass instead (harnessd.h: 12 ms, 1
 * ms while a UART-bridge client is connected); the 6900 interleave runs right
 * after this row (ILV_AFTER_MASK). The drain alone was not enough on silicon
 * (rc2_v6, 2026-09-28: still glyph by glyph): each byte cost ~5 us through the
 * per-byte STATUS read, the register HAL and the per-byte mirror tap, so a 4 ms
 * drain moved ~3 glyphs. Lane CLCD-SPEED's bus seam (clcd.h clcd_bus_push,
 * hal_front.c) makes a byte ~0.6 us; tests/test_clcd_speed.c has the model. */

static void svc_clcd(void)
{
#ifdef MPS3_HAS_CLCD
    int uart = posix_net_conns_on(MPS3_PORT_UART0) || posix_net_conns_on(MPS3_PORT_UART1) ||
               posix_net_conns_on(MPS3_PORT_SWO);
    /* v0.16 `locate` (locate_linux.c): at most one backlight edge (one KVM CTRL
     * write) per 250 ms, from this row's tick -- before the drain, so a tap the
     * drain's touch_poll() sees can end it in the same pass. */
    harnessd_locate_tick();
    /* also services the CLCD KVM when MPS3_HAS_CLCD_KVM is built */
    (void)clcd_poll_drain(uart ? HARNESSD_CLCD_DRAIN_UART_US : HARNESSD_CLCD_DRAIN_US);
    /* The LCD mirror's per-pass half (lcdmirror_tap.c): publish what the CLCD tap
     * fed the model during the drain (every clcd_poll() in it), supervise
     * mps3-lcdmirror. In this slot so its cost is the clcd row's (svc_us), under
     * the clcd budget. */
    harnessd_lcdmirror_poll();
#endif
    /* Without CLCD the slot stays (index = mailbox identity). */
}

static void heartbeat_init(void)
{
    uint32_t own = hal_quiet_read32(MPS3_GPIO_BASE, GPIO_OWN);
    uint32_t oe  = hal_quiet_read32(MPS3_GPIO_BASE, GPIO_OE);
    hal_quiet_write32(MPS3_GPIO_BASE, GPIO_OWN, own | HEARTBEAT_GPIO_BIT);
    hal_quiet_write32(MPS3_GPIO_BASE, GPIO_OE, oe | HEARTBEAT_GPIO_BIT);
}

static void svc_hbeat(void)
{
    static uint32_t last_ms;
    static int on;
    uint32_t now = mps3_sys_now_ms();
    if ((uint32_t)(now - last_ms) >= HEARTBEAT_HALF_MS) {
        last_ms = now;
        on = !on;
        uint32_t out = hal_quiet_read32(MPS3_GPIO_BASE, GPIO_OUT);
        hal_quiet_write32(MPS3_GPIO_BASE, GPIO_OUT,
                          on ? (out | HEARTBEAT_GPIO_BIT) : (out & ~HEARTBEAT_GPIO_BIT));
    }
}

/* The diag gather: main.c's svc_diag with the Linux sources (§5.4), then the
 * mirror into the LMB tail so the mailbox is JTAG-readable while Linux runs. */
static volatile uint32_t *s_mailbox;

static void mailbox_mirror(void)
{
    if (!s_mailbox) {
        return;
    }
    mps3_diag_t snap;
    mps3_diag_snapshot(&snap);
    const uint32_t *w = (const uint32_t *)&snap;
    /* Body first, magic last: a JTAG reader scanning for the magic never finds
     * it over a half-written body. */
    for (unsigned i = 1; i < sizeof(snap) / 4u; i++) {
        s_mailbox[i] = w[i];
    }
    s_mailbox[0] = w[0];
}

static void svc_diag(void)
{
    static harnessd_eth_counters_t eth;
    static uint32_t eth_ms;
    static int eth_primed;
    uint32_t now = mps3_sys_now_ms();
    /* sysfs reads cost syscalls: refresh the kernel counters once a second. */
    if (!eth_primed || (uint32_t)(now - eth_ms) >= 1000u) {
        harnessd_eth_counters(&eth);
        eth_ms = now;
        eth_primed = 1;
    }

    mps3_diag_t v;
    memset(&v, 0, sizeof(v));
    if (eth.valid) {
        v.rx_drop_frames = eth.rx_dropped;
        v.tx_frames_sent = eth.tx_packets;
        v.tx_errors      = eth.tx_errors;
    }
    v.icap_bytes = swap_fsm_icap_bytes();
    config_agent_rx_progress(&v.rx_payload_got, &v.rx_payload_expect);
    config_agent_win_diag(&v.win_windows_drained, &v.win_grant_send_fails);
    v.icap_sr_last    = swap_fsm_icap_sr_last();
    v.icap_eos_status = swap_fsm_icap_eos_status();
#ifdef MPS3_HAS_TOUCH
    v.touch_probe_regs    = touch_probe_regs_word();
    v.touch_probe_adc_x   = touch_probe_adc_x_word();
    v.touch_probe_adc_y   = touch_probe_adc_y_word();
    v.touch_probe_verdict = touch_probe_verdict();
#endif
    v.svc_count          = (uint32_t)mps3_service_count();
    v.svc_pass_max_us    = mps3_service_pass_max_us();
    v.svc_worst_us       = mps3_service_worst_us();
    v.svc_worst_ix       = mps3_service_worst_index();
    v.svc_overrun_events = mps3_service_overrun_events();
    v.svc_skip_events    = mps3_service_skip_events();
    v.svc_skipped_mask   = mps3_service_skipped_mask();
    v.svc_max_us_0       = mps3_service_max_us_pack(0);
    v.svc_max_us_1       = mps3_service_max_us_pack(1);
    v.svc_max_us_2       = mps3_service_max_us_pack(2);
    v.svc_max_us_3       = mps3_service_max_us_pack(3);
    v.svc_max_us_4       = mps3_service_max_us_pack(4);
    v.svc_max_us_5       = mps3_service_max_us_pack(5);
    v.svc_max_us_6       = mps3_service_max_us_pack(6);   /* v9: row 12 "usd" */
    /* v9: the power-on latch. Under Linux this mailbox word IS the latch
     * (ovlstore_linux.c): the mirror below keeps it in BRAM. */
    v.usd_boot           = overlay_store_diag_word();
    mps3_diag_publish(&v);
    mailbox_mirror();
}

/* Slot 2 is the Linux engine's card housekeeping: the resident-clearing save and
 * (v0.14) reaping the slot verifier. The table is full (MPS3_SVC_MAX), and both
 * are the same kind of work -- short checks whose slow half runs elsewhere. */
static void svc_persist(void)
{
    harnessd_resident_poll();
    harnessd_slot_poll();
}

/* APPEND-ONLY. Index = mailbox identity (HARNESSD_CONTRACT.md §5.2): 3..11 are
 * main.c's services at main.c's indices; 0..2 are the Linux engine's own. */
static const mps3_service_t s_services[] = {
    /*  0 */ { "ident",    identify_poll,          5000u },  /* UDP 6899              */
    /*  1 */ { "kmsg",     svc_kmsg,               5000u },  /* /dev/kmsg -> log ring */
    /*  2 */ { "persist",  svc_persist,               0u },  /* fsync on uSD: unbudgeted */
    /*  3 */ { "swap",     swap_fsm_poll,         50000u },
    /*  4 */ { "cfgagent", config_agent_poll,     50000u },
    /*  5 */ { "ctrl",     coordinator_net_poll,  10000u },
    /*  6 */ { "jtag",     harnessd_jtag_poll,    10000u },  /* 6921, batched: jtag_linux.c */
    /*  7 */ { "xvc",      xvc_server_poll,       50000u },
    /*  8 */ { "uart",     uart_over_eth_poll,    10000u },
    /*  9 */ { "clcd",     svc_clcd,              30000u },
    /* 10 */ { "diag",     svc_diag,               2000u },
    /* 11 */ { "hbeat",    svc_hbeat,              1000u },
    /* 12 */ { "usd",      overlay_store_service,     0u },  /* the user uSD store (D13):
                                                         * synchronous O_DSYNC card I/O
                                                         * (<= OVLSTORE_SD_OP_BLOCKS a
                                                         * call) -> unbudgeted, like
                                                         * persist; the same row as
                                                         * bare metal's 12 */
};

/* The A4 interleave: on Linux the kernel already drained the wire, so it is just
 * the 6900 listener, after xvc (7) and clcd (9) as on bare metal. */
static void svc_interleave(void) { coordinator_net_poll(); }
#define ILV_AFTER_MASK ((1u << 7) | (1u << 9))
#define VITAL_MASK     (1u << 5)   /* ctrl: a board nobody can reach is dark */

/* uart_over_eth.h's policy log sink (MPS3_UART_LINUX_POLICY): the console
 * relay's no-client drops and swap flushes go to the harness console + `log`. */
void uart_over_eth_note(const char *line)
{
    harnessd_log("%s\n", line);
}

/* identify.h provider: "run", or "nohw" when there is no fabric to serve. */
const char *mps3_identify_mode(void)
{
    return g_hd.nohw ? "nohw" : "run";
}

/* ==========================================================================
 * NO-HW MODE (HARNESSD_CONTRACT §2 item 7). There is no fabric to map: no
 * /sys/class/uio at all (QEMU -M virt, a DTS without the UIO estate), the
 * required blocks missing under --uio-strict, or --no-hw. The service modules
 * cannot run — every one of them touches registers — and exiting would make
 * init respawn us once a second forever. So harnessd IDLES instead: it answers
 * identify on 6899 (mode "nohw", shell_id 0) and every 6900 line with
 * {"ok":false,"err":"no fabric: <why>"}, and sleeps in poll() in between.
 * It does NOT kick the watchdog: on a real board a fabric harnessd cannot map is
 * a broken boot, and letting stage0's armed watchdog reset it is what makes the
 * slot fallback happen.
 * ========================================================================== */
static int run_nohw(const char *why)
{
    char err[160];
    g_hd.nohw = 1;
    harnessd_log("NO-HW MODE: %s -- identify answers (mode \"nohw\"), 6900 declines "
                 "every verb, nothing touches a register; the watchdog is NOT kicked\n", why);
    snprintf(err, sizeof(err), "{\"ok\":false,\"err\":\"no fabric: %.100s\"}\n", why);
    posix_net_reset();
    posix_net_set_bind_any(g_hd.bind_any);
    posix_net_set_port_offset(g_hd.port_offset);
    identify_init();
    mps3_net_listener_t *lst = mps3_net_listen(MPS3_PORT_CONTROL);
    mps3_net_conn_t *conn = 0;
    mps3_net_linebuf_t lb;
    mps3_net_linebuf_reset(&lb);
    while (!s_stop) {
        identify_poll();
        mps3_net_conn_t *in = mps3_net_accept(lst);
        if (in) {
            if (conn && mps3_net_peer_closed(conn)) {
                mps3_net_close(conn);    /* reap before refuse, as coordinator_net.c */
                conn = 0;
            }
            if (conn) {
                mps3_net_close(in);      /* single client, like 6900 always is */
            } else {
                conn = in;
                mps3_net_linebuf_reset(&lb);
            }
        }
        while (conn) {
            char c;
            int n = mps3_net_recv(conn, &c, 1);
            if (n == 0) {
                break;
            }
            if (n < 0) {
                mps3_net_close(conn);
                conn = 0;
                break;
            }
            if (mps3_net_linebuf_feed(&lb, c) != 0) {
                (void)mps3_net_send(conn, err, (uint32_t)strlen(err));
            }
        }
        (void)posix_net_wait(1000u);
    }
    harnessd_log("stopped (signal)\n");
    return 0;
}

/* ==========================================================================
 * Start-up helpers
 * ========================================================================== */
static void on_signal(int sig)
{
    (void)sig;
    s_stop = 1;
}

/* First start since the OS booted, or a respawn? A marker on tmpfs holding the
 * kernel's boot_id: absent or different => boot start (and write it). */
static int detect_boot_start(void)
{
    char bid[64] = "", old[64] = "";
    FILE *f = fopen("/proc/sys/kernel/random/boot_id", "r");
    if (f) {
        if (!fgets(bid, sizeof(bid), f)) bid[0] = '\0';
        fclose(f);
    }
    f = g_hd.run_marker ? fopen(g_hd.run_marker, "r") : 0;
    if (f) {
        if (!fgets(old, sizeof(old), f)) old[0] = '\0';
        fclose(f);
        if (strcmp(old, bid) == 0) {
            return 0;
        }
    }
    f = g_hd.run_marker ? fopen(g_hd.run_marker, "w") : 0;
    if (f) {
        fputs(bid, f);
        fclose(f);
    } else if (g_hd.run_marker) {
        harnessd_log("start: cannot write %s (%s) -- every start will look like a boot "
                     "start\n", g_hd.run_marker, strerror(errno));
    }
    return 1;
}

static void scenario_seed(void)
{
#ifdef MPS3_HAL_MOCK
    if (g_hd.scenario && strcmp(g_hd.scenario, "ctrl-echo") == 0) {
        /* firmware/test/ctrl_echo.c's fixed scenario: static_id 0xa1b2c3d4 on
         * BOTH sides of the identity check, GENCHK counters 1234/1230/4. */
        g_hd.static_id_claim_override = 0xA1B2C3D4u;
        g_hd.have_claim_override = 1;
        hal_mock_poke(MPS3_GENCHK_BASE, GENCHK_TX_CNT, 1234u);
        hal_mock_poke(MPS3_GENCHK_BASE, GENCHK_RX_CNT, 1230u);
        hal_mock_poke(MPS3_GENCHK_BASE, GENCHK_ERR_CNT, 4u);
    }
#endif
}

#ifdef MPS3_HAL_MOCK
/* --mock-s0 0xID: write a VALID stage0 status block (boot 1, slot A, handed
 * off) carrying the fabric static_id, as stage0 would have. Tests and the QEMU
 * smoke. Offsets by name from STAGE0's header. */
static void mock_seed_s0(uint32_t sid)
{
    volatile uint32_t *b = hal_backend_window(HARNESSD_S0_STATUS_ADDR, S0_STATUS_BYTES);
    if (!b) return;
#define W(f, v) b[__builtin_offsetof(struct s0_status, f) / 4u] = (v)
    for (unsigned i = 0; i < S0_STATUS_BYTES / 4u; i++) b[i] = 0u;
    W(magic, S0_STATUS_MAGIC);
    W(version, S0_STATUS_VERSION);
    W(size, S0_STATUS_BYTES);
    W(fabric_static_id, sid);
    W(boot_count, 1u);
    W(phase, S0_PH_HANDOFF);
    W(booted_from, S0_FROM_A);
    W(att_from, S0_FROM_A);
    W(magic_end, S0_STATUS_MAGIC);
#undef W
}
#endif

static void usage(FILE *f)
{
    fprintf(f,
"usage: mps3-harnessd [options]   (HARNESSD_CONTRACT.md §7)\n"
"  --static-id-file P   image claim (line 1 = 0x...)     [/etc/mps3/static_id]\n"
"  --greybox P          greybox clearing .bin             [/etc/mps3/greybox_clear.bin]\n"
"  --version-file P     image manifest                    [/etc/mps3/version]\n"
"  --state-dir D        resident-clearing cache           [/persist/mps3]\n"
"  --card P             the user microSD, WHOLE disk: the D13 store AND the\n"
"                       stage0 slots (`none` = no card)   [" HARNESSD_CARD_DEFAULT_TEXT "]\n"
"  --usd-dev P | --slot-disk P   aliases of --card; each given must be the same device\n"
"  --authorized-keys P  TOFU claim target                 [/persist/ssh/authorized_keys]\n"
"  --keys-sync P        run after a claim                 [/usr/sbin/mps3-keys-sync]\n"
"  --host-key-fp P      published SHA256 fingerprint      [/run/mps3/ssh/host_key_sha256]\n"
"  --host-key P         dropbear host key (fallback)      [/etc/dropbear/dropbear_ed25519_host_key]\n"
"  --netif IF           the shell's interface             [eth0]\n"
"  --run-marker P       first-start-after-boot marker     [/run/mps3-harnessd.boot]\n"
"  --net-state P        IMAGE's dhcp=0|1                  [/run/mps3/net.state]\n"
"  --boot-health P      IMAGE's healthy=1 gates CONFIRM   [/run/mps3/boot-health]\n"
"  --slot-state P       per-boot slot state               [/run/mps3/slot.state]\n"
"  --no-hw              no fabric: serve identify + decline 6900 (also automatic)\n"
"  --bind-any | --loopback                                [bind-any]\n"
"  --port-offset N      bind every contract port at N+port (tests)\n"
"  --wdog on|off        arm the shell watchdog            [on]\n"
"  --uio-strict | --no-uio-strict                         [strict]\n"
"  --uio-sysfs D / --uio-devdir D                         [/sys/class/uio, /dev]\n"
"  --idle-ms N / --uart-idle-ms N                         [10 / 2]\n"
"  --uart-pace-ms N     host->DUT console bytes >= N ms apart on 6930/6931 (0 = off) [0]\n"
"  --kmsg               tail /dev/kmsg into the log ring\n"
"  --lcdmirror P|none   the LCD mirror server (6940)      [mps3-lcdmirror beside this binary]\n"
"  --lcdmirror-shm P    the mirror's aperture file        [/dev/shm/mps3-lcdmirror]\n"
"  --identity P         this boot's board identity        [/run/mps3/identity]\n"
"  --identity-override P  identity_set's override file    [/persist/etc/mps3/identity]\n"
"  --persist-state P    IMAGE's backing=card|tmpfs        [/run/mps3/persist.state]\n"
"  --jtag-io M          6921 I/O: batched | per-byte (jtag_server.c's loop) [batched]\n"
"  --panel-theme T      the panel's look: aligned (HM's tokens) | today     [aligned]\n"
"  --quiet              no console output (the log ring still fills)\n"
#ifdef MPS3_HAL_MOCK
"  --mock-fabric P      back the mock fabric with a file (shared across runs)\n"
"  --mock-s0 0xID       seed a valid stage0 status block with this static_id\n"
"  --mock-usr-access 0xV  make USRACC answer V\n"
"  --scenario ctrl-echo firmware/test/ctrl_echo.c's scenario (conformance)\n"
"  --static-id 0xID     image claim override (tests)\n"
"  --mock-trusted-peer IP  the slot lock trusts only this peer (tests; else 127/8)\n"
"  --mock-negctl-locate-no-restore  a locate's end leaves the backlight as it was (negctl)\n"
"  --mock-presence-speed N  run the presence clock N x real time (TTL tests)\n"
"  --mock-negctl-presence-no-ttl  the session table ignores the TTL (negctl)\n"
#endif
    );
}

/* "none" / "" = no card. */
static const char *card_norm(const char *p)
{
    return (p == NULL || p[0] == '\0' || strcmp(p, "none") == 0) ? NULL : p;
}

/* Do two spellings of the card name the same device? Both "none"; the same
 * string; or two paths that resolve to the same node (a symlink such as
 * /dev/disk/by-id/..., or /dev/../dev/mmcblk0): the same block device (st_rdev)
 * or the same file (st_dev + st_ino, the host tests' card images). A path that
 * does not exist agrees only with itself. /dev/mmcblk0 and /dev/mmcblk0p4 are
 * DIFFERENT devices -- exactly the mismatch this refuses. */
static int card_same(const char *a, const char *b)
{
    a = card_norm(a);
    b = card_norm(b);
    if (a == NULL || b == NULL) {
        return a == b;
    }
    if (strcmp(a, b) == 0) {
        return 1;
    }
    struct stat sa, sb;
    if (stat(a, &sa) != 0 || stat(b, &sb) != 0) {
        return 0;
    }
    if (S_ISBLK(sa.st_mode) || S_ISBLK(sb.st_mode)) {
        return S_ISBLK(sa.st_mode) && S_ISBLK(sb.st_mode) && sa.st_rdev == sb.st_rdev;
    }
    return sa.st_dev == sb.st_dev && sa.st_ino == sb.st_ino;
}

static int parse_args(int argc, char **argv, uint32_t *mock_s0, int *have_mock_s0,
                      uint32_t *mock_usr, int *have_mock_usr)
{
    /* --card and its two aliases, in the order the table below checks them */
    struct { const char *flag; const char *val; int given; } card[3] = {
        { "--card", 0, 0 }, { "--usd-dev", 0, 0 }, { "--slot-disk", 0, 0 },
    };
    enum { O_SIDF = 256, O_GREY, O_VER, O_STATE, O_USD, O_AK, O_HK, O_NETIF, O_RUN, O_NETST,
           O_HEALTH, O_HKFP, O_KSYNC, O_NOHW, O_ANY, O_LO, O_POFF, O_WDOG, O_STRICT, O_NOSTRICT,
           O_SYSFS, O_DEVDIR, O_IDLE, O_UIDLE, O_KMSG, O_QUIET, O_MOCKF, O_MOCKS0,
           O_MOCKUSR, O_SCEN, O_SID, O_SLOTDISK, O_SLOTSTATE, O_MOCKPEER, O_CARD, O_UPACE,
           O_LCDM, O_LCDMSHM, O_IDRUN, O_IDOVR, O_PSTATE, O_NEGLOC, O_JTAGIO, O_THEME,
           O_PSPEED, O_NEGTTL, O_HELP };
    static const struct option opts[] = {
        { "static-id-file", 1, 0, O_SIDF }, { "greybox", 1, 0, O_GREY },
        { "version-file", 1, 0, O_VER }, { "state-dir", 1, 0, O_STATE },
        { "card", 1, 0, O_CARD }, { "usd-dev", 1, 0, O_USD },
        { "authorized-keys", 1, 0, O_AK }, { "host-key", 1, 0, O_HK },
        { "netif", 1, 0, O_NETIF }, { "run-marker", 1, 0, O_RUN },
        { "net-state", 1, 0, O_NETST }, { "boot-health", 1, 0, O_HEALTH },
        { "host-key-fp", 1, 0, O_HKFP }, { "keys-sync", 1, 0, O_KSYNC },
        { "no-hw", 0, 0, O_NOHW }, { "bind-any", 0, 0, O_ANY },
        { "loopback", 0, 0, O_LO }, { "port-offset", 1, 0, O_POFF },
        { "wdog", 1, 0, O_WDOG }, { "uio-strict", 0, 0, O_STRICT },
        { "no-uio-strict", 0, 0, O_NOSTRICT }, { "uio-sysfs", 1, 0, O_SYSFS },
        { "uio-devdir", 1, 0, O_DEVDIR }, { "idle-ms", 1, 0, O_IDLE },
        { "uart-idle-ms", 1, 0, O_UIDLE }, { "uart-pace-ms", 1, 0, O_UPACE },
        { "kmsg", 0, 0, O_KMSG },
        { "quiet", 0, 0, O_QUIET }, { "mock-fabric", 1, 0, O_MOCKF },
        { "mock-s0", 1, 0, O_MOCKS0 }, { "mock-usr-access", 1, 0, O_MOCKUSR },
        { "scenario", 1, 0, O_SCEN }, { "static-id", 1, 0, O_SID },
        { "slot-disk", 1, 0, O_SLOTDISK }, { "slot-state", 1, 0, O_SLOTSTATE },
        { "mock-trusted-peer", 1, 0, O_MOCKPEER },
        { "lcdmirror", 1, 0, O_LCDM }, { "lcdmirror-shm", 1, 0, O_LCDMSHM },
        { "identity", 1, 0, O_IDRUN }, { "identity-override", 1, 0, O_IDOVR },
        { "persist-state", 1, 0, O_PSTATE },
        { "mock-negctl-locate-no-restore", 0, 0, O_NEGLOC },
        { "jtag-io", 1, 0, O_JTAGIO },
        { "panel-theme", 1, 0, O_THEME }, { "mock-presence-speed", 1, 0, O_PSPEED },
        { "mock-negctl-presence-no-ttl", 0, 0, O_NEGTTL },
        { "help", 0, 0, O_HELP }, { 0, 0, 0, 0 },
    };
    int c;
    while ((c = getopt_long(argc, argv, "h", opts, 0)) != -1) {
        switch (c) {
        case O_SIDF:    g_hd.static_id_file = optarg; break;
        case O_GREY:    g_hd.greybox_file = optarg; break;
        case O_VER:     g_hd.version_file = optarg; break;
        case O_STATE:   g_hd.state_dir = optarg; break;
        case O_CARD:    card[0].val = optarg; card[0].given = 1; break;
        case O_USD:     card[1].val = optarg; card[1].given = 1; break;
        case O_AK:      g_hd.authorized_keys = optarg; break;
        case O_HK:      g_hd.host_key = optarg; break;
        case O_NETIF:   g_hd.netif = optarg; break;
        case O_RUN:     g_hd.run_marker = optarg; break;
        case O_NETST:   g_hd.net_state = optarg; break;
        case O_HEALTH:  g_hd.boot_health = optarg; break;
        case O_HKFP:    g_hd.host_key_fp = optarg; break;
        case O_KSYNC:   g_hd.keys_sync = optarg; break;
        case O_NOHW:    g_hd.nohw = 1; break;
        case O_ANY:     g_hd.bind_any = 1; break;
        case O_LO:      g_hd.bind_any = 0; break;
        case O_POFF:    g_hd.port_offset = (int)strtol(optarg, 0, 0); break;
        case O_WDOG:    g_hd.wdog = (strcmp(optarg, "off") != 0); break;
        case O_STRICT:  g_hd.strict = 1; break;
        case O_NOSTRICT: g_hd.strict = 0; break;
        case O_SYSFS:   g_hd.uio_sysfs = optarg; break;
        case O_DEVDIR:  g_hd.uio_devdir = optarg; break;
        case O_IDLE:    g_hd.idle_ms = (unsigned)strtoul(optarg, 0, 0); break;
        case O_UIDLE:   g_hd.uart_idle_ms = (unsigned)strtoul(optarg, 0, 0); break;
        case O_UPACE:   g_hd.uart_pace_ms = (unsigned)strtoul(optarg, 0, 0); break;
        case O_KMSG:    g_hd.kmsg = 1; break;
        case O_QUIET:   g_hd.quiet = 1; break;
        case O_MOCKF:   g_hd.mock_fabric = optarg; break;
        case O_MOCKS0:  *mock_s0 = (uint32_t)strtoul(optarg, 0, 0); *have_mock_s0 = 1; break;
        case O_MOCKUSR: *mock_usr = (uint32_t)strtoul(optarg, 0, 0); *have_mock_usr = 1; break;
        case O_SCEN:    g_hd.scenario = optarg; break;
        case O_SID:     g_hd.static_id_claim_override = (uint32_t)strtoul(optarg, 0, 0);
                        g_hd.have_claim_override = 1; break;
        case O_SLOTDISK:  card[2].val = optarg; card[2].given = 1; break;
        case O_SLOTSTATE: g_hd.slot_state = optarg; break;
        case O_MOCKPEER:  g_hd.mock_trusted_peer = optarg; break;
        case O_LCDM:      g_hd.lcdmirror = optarg; break;
        case O_LCDMSHM:   g_hd.lcdmirror_shm = optarg; break;
        case O_IDRUN:     g_hd.identity_run = optarg; break;
        case O_IDOVR:     g_hd.identity_override = optarg; break;
        case O_PSTATE:    g_hd.persist_state = optarg; break;
        case O_NEGLOC:
#ifdef MPS3_HAL_MOCK
            g_locate_negctl_no_restore = 1;
            break;
#else
            fprintf(stderr, "mps3-harnessd: --mock-negctl-* exists only in a MOCK build\n");
            return -1;
#endif
        case O_JTAGIO:
            if (strcmp(optarg, "batched") == 0) {
                g_hd.jtag_per_byte = 0;
            } else if (strcmp(optarg, "per-byte") == 0) {
                g_hd.jtag_per_byte = 1;
            } else {
                fprintf(stderr, "mps3-harnessd: --jtag-io batched|per-byte, not %s\n", optarg);
                return -1;
            }
            break;
        case O_THEME:
            if (strcmp(optarg, "aligned") != 0 && strcmp(optarg, "today") != 0) {
                fprintf(stderr, "mps3-harnessd: --panel-theme aligned|today\n");
                return -1;
            }
            g_hd.panel_theme = optarg;
            break;
        case O_PSPEED:
        case O_NEGTTL:
#ifdef MPS3_HAL_MOCK
            if (c == O_NEGTTL) {
                g_panel_negctl_no_ttl = 1;
            } else {
                g_panel_mock_speed = (unsigned)strtoul(optarg, 0, 0);
            }
            break;
#else
            fprintf(stderr, "mps3-harnessd: --mock-presence-* / --mock-negctl-* exist only in a "
                            "MOCK build\n");
            return -1;
#endif
        case O_HELP: case 'h': usage(stdout); exit(0);
        default: usage(stderr); return -1;
        }
    }
    /* ONE card: the first spelling given wins, and every other one given must
     * name the same device -- or the store and the slots would write two disks. */
    int first = -1;
    for (int i = 0; i < 3; i++) {
        if (!card[i].given) {
            continue;
        }
        if (first < 0) {
            first = i;
        } else if (!card_same(card[first].val, card[i].val)) {
            fprintf(stderr, "mps3-harnessd: %s %s and %s %s name different devices -- the "
                            "D13 store and the stage0 slots share ONE card: pass --card once\n",
                    card[first].flag, card[first].val, card[i].flag, card[i].val);
            return -1;
        }
    }
    if (first >= 0) {
        g_hd.card = card_norm(card[first].val);
    }
#ifndef MPS3_HAL_MOCK
    if (g_hd.scenario || g_hd.mock_fabric || *have_mock_s0 || *have_mock_usr ||
        g_hd.have_claim_override || g_hd.mock_trusted_peer) {
        fprintf(stderr, "mps3-harnessd: the --mock-* / --scenario / --static-id options "
                        "exist only in a MOCK build\n");
        return -1;
    }
#endif
    return 0;
}

/* ==========================================================================
 * main
 * ========================================================================== */
int main(int argc, char **argv)
{
    uint32_t mock_s0 = 0, mock_usr = 0;
    int have_mock_s0 = 0, have_mock_usr = 0;

    harnessd_time_init();
    memset(&g_hd, 0, sizeof(g_hd));
    g_hd.static_id_file  = "/etc/mps3/static_id";
    g_hd.greybox_file    = k_default_greybox;
    g_hd.version_file    = "/etc/mps3/version";
    g_hd.state_dir       = "/persist/mps3";
    g_hd.card            = HARNESSD_CARD_DEFAULT;   /* the store's AND the slots' */
    g_hd.authorized_keys = "/persist/ssh/authorized_keys";   /* IMAGE_CONTRACT §4.2 */
    g_hd.keys_sync       = "/usr/sbin/mps3-keys-sync";
    g_hd.host_key_fp     = "/run/mps3/ssh/host_key_sha256";
    g_hd.host_key        = "/etc/dropbear/dropbear_ed25519_host_key";
    g_hd.netif           = k_default_netif;
    g_hd.run_marker      = "/run/mps3-harnessd.boot";
    g_hd.net_state       = "/run/mps3/net.state";
    g_hd.boot_health     = "/run/mps3/boot-health";
    g_hd.slot_state      = "/run/mps3/slot.state";
    g_hd.identity_run    = MPS3_ID_RUN_PATH;
    g_hd.identity_override = MPS3_ID_OVERRIDE_PATH;
    g_hd.persist_state   = "/run/mps3/persist.state";
    g_hd.bind_any        = 1;
    g_hd.wdog            = 1;
    g_hd.strict          = 1;
    g_hd.idle_ms         = 10u;
    g_hd.uart_idle_ms    = 2u;
    if (parse_args(argc, argv, &mock_s0, &have_mock_s0, &mock_usr, &have_mock_usr) != 0) {
        return 2;
    }
    if (g_hd.scenario && strcmp(g_hd.scenario, "ctrl-echo") == 0) {
        if (g_hd.greybox_file == k_default_greybox) {
            g_hd.greybox_file = 0;   /* the scenario's own 4-NOP greybox (ovlstore_linux.c) */
        }
        if (g_hd.netif == k_default_netif) {
            /* ctrl_echo has no network interface: no link, MAC 0-default, zero
             * counters. Reading the BUILD HOST's eth0 would make the conformance
             * result depend on the machine it runs on. */
            g_hd.netif = "harnessd-scenario-none";
        }
    }

    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = on_signal;   /* no SA_RESTART: poll() returns EINTR at once */
    sigaction(SIGTERM, &sa, 0);
    sigaction(SIGINT, &sa, 0);
    signal(SIGPIPE, SIG_IGN);    /* a vanished peer is EPIPE at the call site */
    signal(SIGHUP, SIG_IGN);

    harnessd_log("\n--- MPS3 shell services on Linux (mps3-harnessd %s, %s HAL) starting ---\n",
                 HARNESSD_BUILD,
#ifdef MPS3_HAL_UIO
                 "uio"
#else
                 "mock"
#endif
                 );

    /* 0. the image manifest (/etc/mps3/version). FIRST, and on every path: it
     * touches no register (one small file read), and it is the ONE provider
     * behind `version` AND `identify.harness` -- including in NO-HW mode, where
     * identify is all harnessd serves. Loading it after the no-hw branch made
     * that reply say "0.0.0" on an image that knows its release. */
    harnessd_version_load();
    /* ... and THE BOARD identity (identity_linux.c), for the same reason: identify
     * reports its label and MAC in NO-HW mode too. One small file read. */
    harnessd_board_identity_load();

    /* 1. the register backend + its self-check */
    hal_opts_t ho;
    memset(&ho, 0, sizeof(ho));
    ho.strict = g_hd.strict;
    ho.uio_sysfs = g_hd.uio_sysfs;
    ho.uio_devdir = g_hd.uio_devdir;
    ho.mock_fabric = g_hd.mock_fabric;
#ifdef MPS3_HAL_MOCK
    if (!g_hd.wdog) {
        hal_mock_set_wdog_absent();   /* MOCK: --wdog off = a fabric with no watchdog */
    }
#endif
    if (g_hd.nohw) {
        return run_nohw("--no-hw");
    }
    if (hal_backend_open(&ho) != 0) {
        return run_nohw("the required shell blocks have no UIO window (--uio-strict)");
    }
    if (hal_backend_windows() == 0) {
        return run_nohw("no UIO devices at all (no /sys/class/uio entries)");
    }
    wdog_touch("right after the register backend opened");
#ifdef MPS3_HAL_MOCK
    if (have_mock_s0) mock_seed_s0(mock_s0);
    if (have_mock_usr) hal_mock_seed_usr_access(mock_usr, 1);
    if (g_hd.scenario && strcmp(g_hd.scenario, "ctrl-echo") == 0 && !have_mock_s0) {
        mock_seed_s0(0xA1B2C3D4u);
    }
#endif
    scenario_seed();

    g_hd.boot_start = detect_boot_start();
    harnessd_log("start: %s\n", g_hd.boot_start
                 ? "FIRST start since the OS booted"
                 : "RESPAWN (same OS boot): the RP is not swapped and live resets are kept");

    /* 2. identity: the FABRIC's static_id and the checks (manifest loaded in 0) */
    harnessd_identity_init();
    harnessd_slot_init();
    (void)harnessd_greybox_load();
    wdog_touch("after identity + greybox");

    /* 3. the network backend: kernel sockets */
    posix_net_reset();
    posix_net_set_bind_any(g_hd.bind_any);
    posix_net_set_port_offset(g_hd.port_offset);
    if (g_hd.kmsg && harnessd_kmsg_open() != 0) {
        harnessd_log("kmsg: /dev/kmsg not readable (%s) -- tail disabled\n", strerror(errno));
        g_hd.kmsg = 0;
    }

    /* 4. the fabric as FOUND, before any module init writes a reset -- then the
     *    DUT clock: on a boot start with every DUT-domain reset still asserted,
     *    make the MMCM match clk_wiz_dut's register file (clk_linux.c), BEFORE
     *    the boot load below releases rp_resetn. */
    harnessd_fabric_snapshot();
    (void)harnessd_dut_clk_boot_sync(g_hd.boot_start);
    heartbeat_init();

    /* 5. every module init + listen (coordinator_init runs clkrst/swap_fsm/
     *    config_agent/overlay_store/jtag/xvc/uart/6900 inits and the boot load).
     *    A respawn raises the reset shield across it. */
    mps3_diag_init();
    s_mailbox = hal_backend_window(HARNESSD_MAILBOX_ADDR, sizeof(mps3_diag_t));
    if (!s_mailbox) {
        harnessd_log("diag: no LMB-tail window -- the mailbox is NOT JTAG-readable\n");
    }
    if (!g_hd.boot_start) {
        hal_reset_shield(1);
    }
    coordinator_init();
    uart_over_eth_set_pace_ms(g_hd.uart_pace_ms);   /* the console policy's knob */
    if (g_hd.uart_pace_ms) {
        harnessd_log("uart: host->DUT console input paced at %u ms/byte (6930/6931)\n",
                     g_hd.uart_pace_ms);
    }
    wdog_touch("after coordinator_init");
    harnessd_resident_restore();
#ifdef MPS3_HAS_CLCD
    clcd_init();
    clcd_set_board_name(harnessd_board_label());   /* row 0: this board, not MPS3-01 */
    harnessd_panel_init();                          /* v0.17 presence + the panel seams */
#endif
#ifdef MPS3_HAS_TOUCH
    {
        uint8_t acks[8];
        touch_init();
        harnessd_log("touch: chip_id 0x%04x (expect 0x0811 at 0x41)\n",
                     (unsigned)touch_chip_id());
        unsigned found = touch_bus_scan(acks, (unsigned)sizeof(acks));
        harnessd_log("touch: i2c scan: %u ack\n", found);
    }
#endif
    wdog_touch("after the panel + touch init");
    hal_reset_shield(0);
    if (hal_reset_shield_hits()) {
        harnessd_log("start: reset shield kept %u live reset release(s) through init\n",
                     (unsigned)hal_reset_shield_hits());
    }
    identify_init();

    harnessd_log("shell up: static_id 0x%08" PRIx32 " rm 0x%08" PRIx32 "%s\n"
                 "ports: 6900 ctl / 69+6910 push / 2542 xvc / 6921 jtag / 6930-6932 uart+swo / "
                 "6899 identify%s\n",
                 g_shell_state.static_id, g_shell_state.current_rm_id,
                 harnessd_identity_locked() ? "  [IDENTITY LOCKED: swaps refused]" : "",
                 g_hd.port_offset ? "  (+ port offset)" : "");

    /* The LCD mirror (interim software mode): the CLCD tap goes in BEFORE the
     * first clcd_poll(), so the model sees the whole init stream. */
    harnessd_lcdmirror_init();

    /* 6. the table */
    unsigned n = (unsigned)(sizeof(s_services) / sizeof(s_services[0]));
    unsigned got = mps3_service_install(s_services, n);
    if (got != n) {
        harnessd_log("FATAL: service table has %u entries but only %u fit\n", n, got);
    }
    mps3_service_set_interleave(svc_interleave, ILV_AFTER_MASK);
    mps3_service_set_vital_mask(VITAL_MASK);
    wdog_arm();

    /* 7. the loop */
    uint64_t act_prev = posix_net_activity();
    uint64_t wr_prev = hal_write_count();
    int ready_unused = 0;
    int reboot_seen = 0;
    uint64_t reboot_deadline = 0;
    int confirmed = 0;
    int stamp_pending = 0;   /* the booted slot's record (slot_linux.c), after the confirm */
    int health_logged = 0;
    uint64_t health_checked_us = 0;
    uint64_t start_us = harnessd_now_us64();

    while (!s_stop) {
        mps3_service_run_pass();

        /* `reboot` (HARNESSD_CONTRACT §5.3): the firmware armed the WDOG and
         * stopped kicking; flush the filesystems NOW, and fall back to reboot(2)
         * if the watchdog's reset never arrives. */
        if (coordinator_reboot_state() == 2 && !reboot_seen) {
            reboot_seen = 1;
            sync();
            reboot_deadline = harnessd_now_us64() +
                              (uint64_t)(2u * MPS3_WDT_STAGE_MS + 2000u) * 1000u;
            harnessd_log("reboot: watchdog armed, filesystems synced\n");
        }
        if (reboot_seen && harnessd_now_us64() >= reboot_deadline) {
            sync();
#ifdef MPS3_HAL_UIO
            harnessd_log("reboot: the watchdog did not reset us -- reboot(2)\n");
            reboot(RB_AUTOBOOT);
#else
            harnessd_log("reboot: MOCK build -- exiting instead of reboot(2)\n");
            return 0;
#endif
        }

        /* stage0's CONFIRM (STAGE0_CONTRACT §3.1): 2 s of running with the
         * vital service healthy AND IMAGE's boot-health verdict healthy=1
         * (network, sshd, /persist -- IMAGE_CONTRACT S99mps3health). Checked
         * once a second until it holds; never on an unhealthy boot, so stage0
         * counts that boot as a failed attempt of its slot.
         *
         * Once confirmed, the booted slot's on-card record is stamped
         * (harnessd_slot_stamp_booted(): idempotent, so a respawn -- which
         * reaches here again with the boot already confirmed -- finds the record
         * and writes nothing). Retried once a second while a card job holds the
         * card; any other outcome is final for this process. */
        if (stamp_pending && harnessd_now_us64() - health_checked_us >= 1000000u) {
            health_checked_us = harnessd_now_us64();
            stamp_pending = (harnessd_slot_stamp_booted() < 0);
        }
        if (!confirmed && harnessd_now_us64() - start_us >= 2000000u &&
            harnessd_now_us64() - health_checked_us >= 1000000u) {
            health_checked_us = harnessd_now_us64();
            if ((mps3_service_skipped_mask() & VITAL_MASK) == 0u && mps3_service_kicks() > 0u) {
                if (harnessd_boot_healthy()) {
                    (void)harnessd_confirm_boot();
                    confirmed = 1;
                    stamp_pending = (harnessd_slot_stamp_booted() < 0);
                } else if (!health_logged) {
                    health_logged = 1;
                    harnessd_log("identity: boot not confirmed yet -- %s does not say "
                                 "healthy=1\n", g_hd.boot_health);
                }
            }
        }

        /* THE IDLE POLICY (HARNESSD_CONTRACT §5.5). */
        uint64_t act = posix_net_activity();
        uint64_t wr = hal_write_count();
        int worked = (act != act_prev) || (wr != wr_prev);
        mps3_swap_state_t st = swap_fsm_state();
        int fsm_confirming = (st != SWAP_IDLE && st != SWAP_AWAIT_INCOMING_CLEARING &&
                              st != SWAP_AWAIT_PARTIAL);
        /* The overlay store working through a job (a verify, a stream, a
         * commit's read-back) is work too: poll it back to back (yield, never
         * sleep) until it is done -- INTEGRATION_L3.md §8. */
        fsm_confirming |= overlay_store_busy() ? 1 : 0;
        act_prev = act;
        wr_prev = wr;
        if (worked) {
            ready_unused = 0;
            continue;
        }
        if (fsm_confirming) {
            sched_yield();   /* polling a register it must see change: yield, don't sleep */
            continue;
        }
        unsigned cap = g_hd.idle_ms;
        if (posix_net_conns_on(MPS3_PORT_UART0) || posix_net_conns_on(MPS3_PORT_UART1) ||
            posix_net_conns_on(MPS3_PORT_SWO)) {
            cap = g_hd.uart_idle_ms;
        }
        if (ready_unused >= 2) {
            /* A socket stays readable while nothing consumes it (a service is
             * deliberately holding it): poll() would return at once forever. */
            struct timespec ts = { 0, 2000000L };
            nanosleep(&ts, 0);
            ready_unused = 0;
        } else if (posix_net_wait(cap) > 0) {
            ready_unused++;
        } else {
            ready_unused = 0;
        }
    }

    harnessd_lcdmirror_stop();
    wdog_disarm();
    harnessd_log("stopped (signal)\n");
    return 0;
}
