/*
 * harnessd.h — the Linux platform layer's own interface (NOT seen by any
 * firmware service module; they see only the firmware/common headers, as on
 * bare metal). One header for the handful of cross-file hooks the platform
 * layer has, so the module boundaries stay readable.
 *
 * The file map (docs/planning/linux_lanes/HARNESSD_CONTRACT.md §0):
 *   main_linux.c      init order, the service table, the poll() idle loop,
 *                     signals, the watchdog
 *   hal_front.c       the four register accessors (+ work counter, reset shield)
 *   hal_uio.c         backend: /dev/uioN            (-DMPS3_HAL_UIO)
 *   hal_mock.c        backend: behavioural fabric   (-DMPS3_HAL_MOCK)
 *   timebase_linux.c  mps3_sys_now_ms/us from CLOCK_MONOTONIC
 *   log_linux.c       xil_printf / console tee into the `log` ring; /dev/kmsg
 *   platform_linux.c  eth0 facts: link, MAC, counters, IP, OS uptime
 *   identity.c        the FABRIC identity (stage0 status block), cross-checks,
 *                     the swap lock, the stage0 boot CONFIRM
 *   version_linux.c   `version` seams (impl, manifest, omit masks)
 *   ovlstore_linux.c  the overlay store's ENGINE PROVIDER (the user microSD's
 *                     block device + the boot latch) + greybox + resident clearing
 *   tofu_linux.c      the authorized_keys claim sink (+ the CLCD engine row)
 *   clk_linux.c       `stats.dut_mhz` from clk_wiz_dut + the boot clock re-sync
 *   sshfp.c           SHA-256 / base64 / the host-key fingerprint
 *   slot_linux.c      the user-microSD boot slots for tools (`slot` verb +
 *                     the 6910 slot-image push), v0.14
 *   lcdmirror_tap.c   the LCD mirror's harnessd half: the CLCD tap -> the GRAM
 *                     model (lcdmirror_model.c) -> /dev/shm, the mps3-lcdmirror
 *                     child (lcdmirror_main.c) supervised, v0.15 (lcdmirror.h)
 *   locate_linux.c    the v0.16 `locate` verb: the backlight blink (the clcd
 *                     row's tick), the identify banner, a tap = found
 *   identity_linux.c  the BOARD identity (label/hostname/IP/MAC) from
 *                     /run/mps3/identity: CLCD rows, identify, the v0.16
 *                     `identity` / `identity_set` verbs (identity_core.c, shared
 *                     with the mps3-identity tool)
 *   jtag_linux.c      service row 6: TCP 6921 (remote_bitbang) with the per-byte
 *                     syscalls batched -- jtag_server.c's byte protocol, unchanged
 *   panel_linux.c     the v0.17 `hello` / `panel` verbs over presence_core.c (the
 *                     session table) and the clcd panel seams' strong providers
 *                     (lease badge, hm row, request banner, theme)
 */
#ifndef HARNESSD_H
#define HARNESSD_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ---- runtime configuration (main_linux.c parses argv into this) ----------- */
typedef struct {
    const char *static_id_file;   /* the IMAGE's claim; never reported as shell_id */
    const char *greybox_file;
    const char *version_file;
    const char *state_dir;        /* resident-clearing cache                    */
    const char *card;             /* the user microSD's WHOLE-disk node, or NULL
                                   * = no card. ONE device for BOTH writers: D13's
                                   * store (ovlstore_linux.c) and stage0's slots
                                   * (slot_linux.c); --card, aliases --usd-dev /
                                   * --slot-disk (main_linux.c card_same())      */
    const char *authorized_keys;  /* TOFU target                                */
    const char *host_key;         /* dropbear host key (fingerprint)            */
    const char *netif;            /* "eth0"                                     */
    const char *run_marker;       /* first-start-after-boot marker (tmpfs)      */
    const char *net_state;        /* IMAGE's /run/mps3/net.state (dhcp=0|1)    */
    const char *boot_health;      /* IMAGE's /run/mps3/boot-health (healthy=1) */
    const char *host_key_fp;      /* IMAGE's published SHA256 fingerprint     */
    const char *keys_sync;        /* IMAGE's mps3-keys-sync, run after a claim */
    const char *scenario;         /* "ctrl-echo" (host conformance) or NULL     */
    const char *mock_fabric;      /* hal_mock backing file                       */
    const char *uio_sysfs, *uio_devdir;
    const char *slot_state;       /* per-OS-boot slot state (tmpfs)             */
    const char *mock_trusted_peer;/* MOCK tests: the ONE peer IP the slot lock
                                   * treats as local (default: 127.0.0.0/8)      */
    const char *lcdmirror;        /* mps3-lcdmirror path, "none" = no mirror, NULL =
                                   * beside this binary (lcdmirror_tap.c)         */
    const char *lcdmirror_shm;    /* the mirror's aperture file (NULL = default)  */
    const char *identity_run;     /* IMAGE's /run/mps3/identity (this boot's)     */
    const char *identity_override;/* /persist/etc/mps3/identity (identity_set)    */
    const char *persist_state;    /* IMAGE's /run/mps3/persist.state (backing=)   */
    int         jtag_per_byte;    /* --jtag-io per-byte: jtag_server.c's own 6921 loop */
    const char *panel_theme;      /* v0.17 --panel-theme: "aligned" (default) | "today" */
    uint32_t    static_id_claim_override;  /* tests: skip the file              */
    int         have_claim_override;
    int         bind_any;
    int         port_offset;
    int         wdog;
    int         strict;
    unsigned    idle_ms;
    unsigned    uart_idle_ms;
    unsigned    uart_pace_ms;     /* host->DUT: >= this many ms between console
                                   * bytes (0 = off; uart_over_eth's Linux policy) */
    int         kmsg;
    int         quiet;
    int         boot_start;       /* derived: 1 = first start since the OS booted */
    int         nohw;             /* --no-hw, or no fabric to map: IDLE mode      */
} harnessd_cfg_t;

extern harnessd_cfg_t g_hd;

/* ---- log_linux.c ---------------------------------------------------------- */
void harnessd_log(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
void harnessd_console_write(const char *buf, size_t n);
int  harnessd_kmsg_open(void);
void harnessd_kmsg_poll(void);

/* ---- timebase_linux.c ----------------------------------------------------- */
void     harnessd_time_init(void);
uint64_t harnessd_now_us64(void);

/* ---- platform_linux.c ----------------------------------------------------- */
typedef struct {
    int      valid;
    uint32_t rx_dropped, tx_packets, tx_errors;
} harnessd_eth_counters_t;
void harnessd_eth_counters(harnessd_eth_counters_t *out);
int  harnessd_read_u32_file(const char *path, uint32_t *out);   /* line 1, base auto */
int  harnessd_boot_healthy(void);   /* IMAGE's boot-health file says healthy=1 */
int  harnessd_kv_file(const char *path, const char *key, char *out, size_t cap);

/* ---- identity.c ----------------------------------------------------------- */
void        harnessd_identity_init(void);
uint32_t    harnessd_fabric_static_id(void);   /* 0 = unknown (never the card's) */
int         harnessd_identity_locked(void);
const char *harnessd_identity_reason(void);    /* NULL when consistent          */
int         harnessd_s0_valid(void);
uint32_t    harnessd_s0_read(unsigned off);
int         harnessd_confirm_boot(void);       /* 1 = written this call          */
const volatile uint32_t *harnessd_s0_block(void); /* the live block, or NULL     */

/* ---- identity_linux.c: the BOARD identity (label/hostname/IP/MAC) ---------- */
void        harnessd_board_identity_load(void);  /* step 0: /run/mps3/identity    */
const char *harnessd_board_label(void);
void        harnessd_board_mac(uint8_t mac[6]);
uint32_t    harnessd_board_ip(void);             /* host order                    */

/* ---- lcdmirror_tap.c ------------------------------------------------------ */
int         harnessd_lcdmirror_enabled(void);

/* ---- main_linux.c svc_clcd: THE CLCD DRAIN's time budget per pass ------------
 * clcd_poll_drain() (clcd.h) runs clcd_poll() back to back while a repaint has
 * bytes to stream, for up to this long, then returns so the rest of the pass
 * runs; with nothing to stream it returns after ONE clcd_poll(), so the budget
 * only ever spends itself on a repaint. 12 ms (lane CLCD-SPEED; was 4 ms) is
 * well inside the clcd row's 30 ms sick budget with the overshoot (<= one
 * clcd_poll: CLCD_BYTES_PER_PASS bytes, ~0.6 ms on the MBV since the bus seam)
 * and the mirror's publish, and it bounds what a repaint adds to one pass --
 * the 6900 interleave runs right after the row. 1 ms while a 6930-6932 client
 * is connected, so the uart row still comes round inside the 2 ms cadence the
 * idle cap keeps for the 16-byte SWO FIFO. tests/test_clcd_speed.c models both
 * against the repaint target and the pass/kick budgets. */
#ifndef HARNESSD_CLCD_DRAIN_US
#define HARNESSD_CLCD_DRAIN_US      12000u
#endif
#ifndef HARNESSD_CLCD_DRAIN_UART_US
#define HARNESSD_CLCD_DRAIN_UART_US 1000u
#endif

/* ---- locate_linux.c: the v0.16 `locate` verb (HM R3) ----------------------- */
void        harnessd_locate_tick(void);          /* svc_clcd, before the drain    */
int         harnessd_locate_active(void);
#ifdef MPS3_HAL_MOCK
extern int  g_locate_negctl_no_restore;          /* MOCK: the restore's negctl    */
#endif

/* ---- panel_linux.c: v0.17 presence (`hello`) + the panel (`panel`), HM R1/R2 */
void        harnessd_panel_init(void);           /* after clcd_init()             */
#ifdef MPS3_HAL_MOCK
extern unsigned g_panel_mock_speed;              /* MOCK: the presence clock rate */
extern int      g_panel_negctl_no_ttl;           /* MOCK: the TTL rule's negctl   */
#endif

/* ---- version_linux.c ------------------------------------------------------ */
void     harnessd_version_load(void);
uint32_t harnessd_manifest_ver32(void);        /* 0 = unknown */

/* ---- ovlstore_linux.c ----------------------------------------------------- */
int  harnessd_greybox_load(void);              /* 0 = loaded                      */
void harnessd_fabric_snapshot(void);           /* BEFORE coordinator_init()       */
void harnessd_resident_restore(void);          /* after coordinator_init()        */
void harnessd_resident_poll(void);             /* service slot 2 "persist"        */
int  harnessd_rp_parked(void);                 /* the respawn found RP decoupled/in reset */

/* ---- clk_linux.c ---------------------------------------------------------- */
/* `stats.dut_mhz` = clk_wiz_dut's register file (the strong mps3_dut_clk_mhz);
 * this makes it true after a fabric reset. Call after harnessd_fabric_snapshot()
 * and BEFORE coordinator_init() (its boot load releases rp_resetn). */
enum {
    HARNESSD_CLK_RESPAWN  = 0,   /* not a boot start: MMCM untouched            */
    HARNESSD_CLK_LIVE     = 1,   /* boot start, a DUT reset released: untouched */
    HARNESSD_CLK_BLANK    = 2,   /* register file describes no clock: untouched */
    HARNESSD_CLK_LOADED   = 3,   /* re-LOADed and locked                         */
    HARNESSD_CLK_UNLOCKED = 4,   /* re-LOADed, no lock within the poll bound     */
};
int harnessd_dut_clk_boot_sync(int boot_start);

/* ---- sshfp.c -------------------------------------------------------------- */
void harnessd_sha256(const void *data, size_t len, uint8_t out[32]);
int  harnessd_b64_encode(const uint8_t *in, size_t n, char *out, size_t cap, int pad);
int  harnessd_host_key_fingerprint(const char *key_path, char *out, size_t cap);
int  harnessd_authorized_key_fingerprint(const char *path, char *out, size_t cap);

/* ---- tofu_linux.c --------------------------------------------------------- */
int  harnessd_ssh_claimed(void);

/* ---- jtag_linux.c: service row 6, TCP 6921 batched ------------------------- */
struct mps3_net_conn;
typedef struct {
    uint64_t accepts, recvs, sends, cmd_bytes, reply_bytes;
} harnessd_jtag_stats_t;
void harnessd_jtag_poll(void);                 /* row 6 (replaces jtag_server_poll)   */
int  harnessd_jtag_client_live(void);          /* 1 = a 6921 client is connected      */
struct mps3_net_conn *harnessd_jtag_conn(void);/* that client, or NULL                */
void harnessd_jtag_stats(harnessd_jtag_stats_t *out);
void harnessd_jtag_reset(void);                /* tests                               */

/* ---- slot_linux.c --------------------------------------------------------- */
void harnessd_slot_init(void);                 /* after identity (needs stage0's block) */
void harnessd_slot_poll(void);                 /* service slot 2: reap the card verifier */
int  harnessd_slot_busy(void);                 /* 1 = a push/verify job holds the card  */
int  harnessd_slot_stamp_booted(void);         /* once CONFIRMED: 1 stamped, 0 done, -1 later */

#ifdef __cplusplus
}
#endif

#endif /* HARNESSD_H */
