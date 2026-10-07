/*
 * stage0.c -- MicroBlaze-V (RISC-V) FIRST-STAGE LOADER for the Linux harness:
 * the target main() and the few platform bodies the portable modules need.
 *
 * Runs from the 128 KiB LMB at 0x0, automatically on FPGA configuration and
 * after every watchdog warm restart (FLOW bakes stage0.elf with updatemem,
 * `make -C fpga/dfx mint-stage0`). Boot order (plan §3, stage0_flow.h):
 *
 *   arm the watchdog -> (cold entry: settle) -> DDR4 calib HOLDS?
 *     -> default uSD slot -> other slot -> RESCUE
 *   hand-off: copy + CRC in DDR, D-cache writeback, re-arm the watchdog
 *   fresh, fence.i, jump to OpenSBI
 *
 * COLD-BOOT HARDENING (lane S0-COLDFIX, 2026-09-28; stage0_flow.h): the
 * watchdog is armed at ENTRY and kicked from every loop (s0_poll_hook), so a
 * DDR transaction stalled by the MCC's post-configuration clock work resets
 * the board instead of hanging it; two such restarts in a row go straight to
 * rescue; a cold entry settles S0_COLD_SETTLE_MS; the calib bit must hold
 * S0_CALIB_HOLD_MS and is re-checked before every card read op / DDR chunk;
 * rescue after a DDR failure keeps polling it and retries the boot when it
 * holds; the console prints progress every S0_PROGRESS_MS of a long phase.
 *
 * RESCUE (plan §10a S6, stage0_rescue.h): 192.168.10.101 with the harness
 * MAC; ping, TFTP push/status on 69, identify on 6899, nothing on 6900.
 *
 * Every platform fact below is a -D override with the SHELL_CONTRACT value as
 * its default (docs/planning/linux_lanes/SHELL_CONTRACT.md §2/§5/§6).
 *
 * STATUS -- what is proven and what is not:
 *   * portable modules (core, flow, net, tftp, ident, rescue, eth-over-
 *     smsc911x, the uSD wrapper over D13's usd.c): host-tested (test/);
 *   * this file (UART, AXI timer, TELEM calib bit, WDOG kick, hand-off):
 *     correct-by-construction from the SHELL contract + the proven bootstub /
 *     dfx_swap_boot recipe; NOT silicon-validated (B1 item 1).
 */
#include <stdint.h>

#include "stage0_boot.h"
#include "stage0_status.h"
#include "stage0_flow.h"
#include "stage0_sd.h"
#include "stage0_rescue.h"
#include "stage0_fmt.h"
#include "stage0_hw.h"

/* ---- build identity (FLOW's mint-stage0 sets the fabric pair) ----------------- */
#ifndef MPS3_STATIC_ID
#define MPS3_STATIC_ID 0x00000000u   /* 0 = unprovisioned: harnessd refuses swaps */
#endif
#ifndef MPS3_VER32
#define MPS3_VER32     0x00000000u
#endif
#ifndef S0_BUILD_ID
#define S0_BUILD_ID    0x00000000u
#endif

/* The fabric identity as named u32 objects in the LOADED image, so FLOW's
 * bake guard (fpga/dfx/tools/stage0_bake.py `elf --expect-static-id/-ver32`)
 * can prove, from stage0.elf alone, which static this stage0 was built for
 * before updatemem binds it to the bitstream (STAGE0_CONTRACT §2). Section
 * .s0_ids is KEPT by stage0.ld: --gc-sections would otherwise drop constants
 * the compiler has already folded into their uses. */
#define S0_IDS __attribute__((used, section(".s0_ids")))
const uint32_t mps3_stage0_static_id S0_IDS = (uint32_t)MPS3_STATIC_ID;
const uint32_t mps3_stage0_ver32     S0_IDS = (uint32_t)MPS3_VER32;
const uint32_t mps3_stage0_build_id  S0_IDS = (uint32_t)S0_BUILD_ID;

/* ---- platform (SHELL_CONTRACT §2) ------------------------------------------------ */
#ifndef S0_UART_BASE
#define S0_UART_BASE     0x40600000u   /* axi_uartlite_0: the kernel's ttyUL0 */
#endif
#ifndef S0_TIMER_BASE
#define S0_TIMER_BASE    0x41C00000u   /* axi_timer_0 */
#endif
#ifndef S0_LAN_BASE
#define S0_LAN_BASE      0xC0000000u   /* axi_emc_0 -> LAN9220 */
#endif
#ifndef S0_TIMER_HZ
#define S0_TIMER_HZ      100000000u
#endif
/* CALIB: 1 = TELEM calib bit (the mbv shell), 0 = none (trust, S0_DDR_IMPLIED).
 * The TELEM + WDOG sequences themselves are in stage0_hw.c (host-tested);
 * S0_WDOG_ARM (default 1) is defined there. */
#ifndef S0_CALIB_TELEM
#define S0_CALIB_TELEM   1
#endif
/* A console progress line every this many ms while stage0 is still booting
 * (phases before rescue): a long settle / card init / slot load is visibly
 * alive, and a hang shows where it stopped. */
#ifndef S0_PROGRESS_MS
#define S0_PROGRESS_MS   10000u
#endif
/* The rescue push copy (staging -> destination) runs in chunks of this many
 * bytes, each behind the calib re-check and a watchdog kick. */
#define S0_COPY_CHUNK    (64u * 1024u)

/* ---- rescue identity (the harness's own, so ARP caches stay valid) -------------- */
#ifndef S0_IP
#define S0_IP            0xC0A80A65u   /* 192.168.10.101 */
#endif
#ifndef MPS3_MAC0
#define MPS3_MAC0 0x02
#endif
#ifndef MPS3_MAC1
#define MPS3_MAC1 0x00
#endif
#ifndef MPS3_MAC2
#define MPS3_MAC2 0x00
#endif
#ifndef MPS3_MAC3
#define MPS3_MAC3 0x4D
#endif
#ifndef MPS3_MAC4
#define MPS3_MAC4 0x50
#endif
#ifndef MPS3_MAC5
#define MPS3_MAC5 0x53
#endif
/* the board's label, 8 ASCII bytes NUL-padded (stage0_status.h S0_LABEL_WORD):
 * the Makefile's S0_LABEL (stage0_board.py); default "MPS3-01" */
#ifndef S0_LABEL_LO
#define S0_LABEL_LO S0_LABEL_WORD('M', 'P', 'S', '3')
#endif
#ifndef S0_LABEL_HI
#define S0_LABEL_HI S0_LABEL_WORD('-', '0', '1', 0)
#endif

/* ---- DDR map (stage0's own use of the 1 GiB at 0x8000_0000) ---------------------
 *   0x8000_0000 - 0xAFFF_FFFF  boot-image destinations (any region outside is
 *                              refused before a byte is written)
 *   0xB000_0000 - 0xB3FF_FFFF  rescue staging window (S0_IMAGE_MAX)
 *   0xBE00_0000 + 32 KiB       D-cache writeback read-thrash window
 *   0xBFF0_0000 - top          ramoops (the kernel's; stage0 never touches it) */
#define S0_DST_BASE      0x80000000u
#define S0_DST_SIZE      0x30000000u
#define S0_STAGE_BASE    0xB0000000u
#define S0_FLUSH_BASE    0xBE000000u
#define S0_DCACHE_BYTES  (8u * 1024u)             /* SHELL_CONTRACT §7 */
#define S0_FLUSH_BYTES   (4u * S0_DCACHE_BYTES)

/* register offsets / bits */
#define UART_TX          0x4u
#define UART_STAT        0x8u
#define UART_CTRL        0xCu
#define UART_SR_TX_FULL  (1u << 3)
#define UART_CR_RST_TX   (1u << 0)
#define UART_CR_RST_RX   (1u << 1)
#define TMR_TCSR1        0x10u
#define TMR_TLR1         0x14u
#define TMR_TCR1         0x18u
#define TMR_ENT          (1u << 7)
#define TMR_LOAD         (1u << 5)
#define TMR_ARHT         (1u << 4)
#define WDOG_WRS         (1u << 3)     /* TWCSR0: the last reset was the watchdog's */

static inline void     w32(uint32_t a, uint32_t v) { *(volatile uint32_t *)(uintptr_t)a = v; }
static inline uint32_t r32(uint32_t a)             { return *(volatile uint32_t *)(uintptr_t)a; }

/* the status block: an absolute LMB address, NOT in .bss (crt0 never zeroes
 * it), so it survives a watchdog warm restart */
#define ST ((struct s0_status *)(uintptr_t)S0_STATUS_ADDR)

int s0_eth_init(uintptr_t base, const uint8_t mac[6]);   /* stage0_eth.c */

/* ---- console ---------------------------------------------------------------------- */

static void uart_init(void)
{
    w32(S0_UART_BASE + UART_CTRL, UART_CR_RST_TX | UART_CR_RST_RX);
    w32(S0_UART_BASE + UART_CTRL, 0u);
}

static void uart_putc(char c)
{
    while (r32(S0_UART_BASE + UART_STAT) & UART_SR_TX_FULL)
        ;
    w32(S0_UART_BASE + UART_TX, (uint32_t)(uint8_t)c);
}

static void uart_puts(const char *s)
{
    for (; *s; ++s) {
        if (*s == '\n')
            uart_putc('\r');
        uart_putc(*s);
    }
}

static void log_line(void *ctx, const char *line)
{
    (void)ctx;
    uart_puts(line);
    uart_puts("\n");
}

static void emit(struct s0_line *l)
{
    log_line(0, l->b);
    s0l_init(l);
}

/* ---- timebase: AXI timer counter 1, free-running (never rdtime) ------------------- */

static void timer_init(void)
{
    w32(S0_TIMER_BASE + TMR_TLR1, 0u);
    w32(S0_TIMER_BASE + TMR_TCSR1, TMR_LOAD);
    w32(S0_TIMER_BASE + TMR_TCSR1, TMR_ENT | TMR_ARHT);
}

/* Incremental accumulation, as firmware/platform/src/main.c: pure 32-bit, wrap-
 * correct while called more often than every ~43 s (every loop here is). */
uint32_t mps3_sys_now_ms(void)
{
    static uint32_t last, rem, ms;
    uint32_t cur = r32(S0_TIMER_BASE + TMR_TCR1);
    rem += cur - last;
    last = cur;
    if (rem >= S0_TIMER_HZ / 1000u) {
        ms += rem / (S0_TIMER_HZ / 1000u);
        rem %= S0_TIMER_HZ / 1000u;
    }
    return ms;
}

uint32_t mps3_sys_now_us(void)
{
    static uint32_t last, rem, us;
    uint32_t cur = r32(S0_TIMER_BASE + TMR_TCR1);
    rem += cur - last;
    last = cur;
    if (rem >= S0_TIMER_HZ / 1000000u) {
        us += rem / (S0_TIMER_HZ / 1000000u);
        rem %= S0_TIMER_HZ / 1000000u;
    }
    return us;
}

/* ---- watchdog + heartbeat: called from every stage0 loop ------------------------- */

static const char *phase_text(uint32_t ph)
{
    static const char *const t[] = { "reset", "ddr", "sd", "slot A", "slot B",
                                     "rescue", "handoff", "trap" };
    return ph < sizeof t / sizeof t[0] ? t[ph] : "?";
}

static uint32_t s_progress_ms;

void s0_poll_hook(void)
{
    /* stage0 armed the watchdog at entry (S0_WDOG_ARM): this is THE kick of
     * everything before the hand-off. It only feeds a running one. */
    s0_hw_wdog_kick();
    ST->heartbeat += 1u;
    uint32_t now = mps3_sys_now_ms();
    ST->uptime_ms = now;
    ST->sd_rd_fails = s0_usd_read_fails();      /* live, not only at the end */
    ST->sd_rd_last = s0_usd_read_diag();
    if (ST->phase < S0_PH_RESCUE && now - s_progress_ms >= S0_PROGRESS_MS) {
        struct s0_line l;
        s_progress_ms = now;
        s0l_init(&l);
        s0l_str(&l, "stage0: .. t=");
        s0l_dec(&l, now / 1000u);
        s0l_str(&l, "s ");
        s0l_str(&l, phase_text(ST->phase));
        s0l_str(&l, " sub ");
        s0l_hex(&l, ST->subphase);
        s0l_str(&l, " rd_fails ");
        s0l_dec(&l, ST->sd_rd_fails);
        s0l_str(&l, " calib_drops ");
        s0l_dec(&l, S0_ENTRY_CALIB_DROPS(ST->entry));
        log_line(0, l.b);
    }
}

/* ---- traps: record, print, park (a spin, not wfi: xsdb must be able to halt) ------ */

void trap_report(uint32_t mcause, uint32_t mepc, uint32_t mtval)
{
    struct s0_line l;
    ST->trap_mcause = mcause;
    ST->trap_mepc = mepc;
    ST->trap_mtval = mtval;
    ST->last_error = S0_LAST_ERROR(S0_ES_TRAP, mcause);
    ST->phase = S0_PH_TRAP;
    s0l_init(&l);
    s0l_str(&l, "\n*** stage0 TRAP *** mcause=");
    s0l_hex(&l, mcause);
    s0l_str(&l, " mepc=");
    s0l_hex(&l, mepc);
    s0l_str(&l, " mtval=");
    s0l_hex(&l, mtval);
    emit(&l);
    log_line(0, "stage0 HALTED (status block phase=7)");
    for (;;)
        __asm__ volatile("");
}

/* ---- the platform ops for the boot order ------------------------------------------ */

static int op_ddr_calib(void *ctx)
{
    (void)ctx;
#if S0_CALIB_TELEM
    /* The CPU is NOT held in reset on calib, and any DDR access before
     * calibration stalls forever: this is the gate. stage0_hw.c rewrites TELEM
     * CTRL = alarm_en on every call (a watchdog reset clears it) and requires
     * the bit to HOLD S0_CALIB_HOLD_MS. */
    uint32_t drops = 0u;
    int ok = s0_hw_ddr_calib(S0_CALIB_TIMEOUT_MS, S0_CALIB_HOLD_MS, &drops);
    s0_status_note_calib_drops(ST, drops);
    return ok ? S0_DDR_OK : S0_DDR_FAIL;
#else
    return S0_DDR_IMPLIED;
#endif
}

/* The cold settle. The MIG's reference (OSC6) is programmed by the MCC after
 * configuration, and nothing stage0 can write resets the MIG (sys_rst is
 * USER_nPB0; the watchdog resets only its AXI shim): the settle records what
 * the calib bit did so a stale calibration is visible (entry [28]/[29]). */
static void op_settle(void *ctx, uint32_t ms)
{
    (void)ctx;
    uint32_t cal = 0u;
    uint32_t drops = s0_hw_settle(ms, &cal);
#if S0_CALIB_TELEM
    s0_status_note_settle(ST, drops, cal);
#else
    (void)drops;
#endif
}

/* Before every card read op and DDR chunk: kick, and is the calib bit still 1? */
static int op_guard(void *ctx)
{
    (void)ctx;
    s0_poll_hook();
#if S0_CALIB_TELEM
    return s0_hw_calib_bit();
#else
    return 1;
#endif
}

static uint32_t op_now_ms(void *ctx)
{
    (void)ctx;
    return mps3_sys_now_ms();
}

static int op_sd_init(void *ctx, uint32_t *card_blocks, uint32_t *detail)
{
    (void)ctx;
    return s0_usd_init(card_blocks, detail);
}

static int op_sd_read(void *ctx, uint32_t lba, uint32_t n, void *dst)
{
    (void)ctx;
    return s0_usd_read_blocks(lba, n, dst);
}

/* A corrupt or hostile boot table must not be able to make stage0 write
 * outside the destination window -- not the staging window, not ramoops,
 * not MMIO -- before its CRC even runs. */
static void *op_addr_to_ptr(uint32_t a, uint32_t len, void *ctx)
{
    (void)ctx;
    if (!s0_region_in_bounds(S0_DST_BASE, S0_DST_SIZE, a, len))
        return 0;
    return (void *)(uintptr_t)a;
}

static const struct s0_flow_ops k_ops = {
    op_ddr_calib, op_sd_init, op_sd_read, op_addr_to_ptr, log_line, 0,
    op_settle, op_guard, op_now_ms
};

/* rescue: a pushed image is loaded from the staging window like a slot, the
 * copy and the CRC in chunks behind the calib re-check + a watchdog kick */
static int pushed_chunk(void *ctx, uint32_t region)
{
    (void)region;
    return op_guard(ctx) ? 0 : S0_EDDR;
}

static int pushed_read(uint32_t src_off, void *dst, uint32_t len, void *ctx)
{
    uint8_t *d = (uint8_t *)dst;
    while (len != 0u) {
        uint32_t n = len < S0_COPY_CHUNK ? len : S0_COPY_CHUNK;
        if (!op_guard(0))
            return S0_EDDR;
        if (s0_mem_read(src_off, d, n, ctx) != 0)
            return -1;
        src_off += n;
        d += n;
        len -= n;
    }
    return 0;
}

static int verify_pushed(const uint8_t *img, uint32_t len, struct s0_result *out, void *ctx)
{
    (void)ctx;
    struct s0_mem_src m = { img, len };
    struct s0_backend be = { pushed_read, op_addr_to_ptr, &m };
    return s0_load_guarded(&be, pushed_chunk, out);
}

/* ---- the hand-off ------------------------------------------------------------------ */

/* Evict dirty payload lines so instruction fetch from DDR sees them: rv32imac
 * has no cache-maintenance CSRs, so read-thrash 4x the D-cache in a window no
 * payload uses. Board-validation item: README "Open validation items". */
static volatile uint32_t g_sink;
static void ddr_writeback(void)
{
    volatile uint32_t *p = (volatile uint32_t *)(uintptr_t)S0_FLUSH_BASE;
    uint32_t acc = 0;
    __asm__ volatile("fence rw,rw" ::: "memory");
    for (uint32_t i = 0; i < S0_FLUSH_BYTES / 4u; ++i)
        acc += p[i];
    g_sink = acc;
    __asm__ volatile("fence rw,rw" ::: "memory");
}

static void __attribute__((noreturn)) handoff(int from, const struct s0_result *r)
{
    struct s0_line l;
    ST->subphase = S0_SUBPHASE(S0_SP_HANDOFF, 0u);
    s0_status_note_handoff(ST, from, r, mps3_sys_now_ms());
    s0l_init(&l);
    s0l_str(&l, "stage0: -> OpenSBI pc=");
    s0l_hex(&l, r->entry_pc);
    s0l_str(&l, " a0=");
    s0l_hex(&l, r->entry_a0);
    s0l_str(&l, " a1=");
    s0l_hex(&l, r->entry_a1);
    s0l_str(&l, from == S0_FROM_A ? " (slot A)" : from == S0_FROM_B ? " (slot B)" : " (rescue)");
    emit(&l);
    while (!(r32(S0_UART_BASE + UART_STAT) & 0x4u))    /* TX FIFO empty */
        ;
    ddr_writeback();
    /* The kernel owns usd_spi from here (D13 handover §12.2): EN=0, CS
     * deasserted, CD_POL/CD_IGNORE kept. The size gate requires this call. The
     * S0_NEGCTL_ macro exists ONLY for that gate's negative control. */
#ifndef S0_NEGCTL_NO_USD_RELEASE
    s0_hw_usd_release();
#endif
#if S0_WDOG_ARM
    /* Supervise the boot: the kernel now has 42.95 s (C_WDT_INTERVAL=31) to
     * get harnessd kicking. If it never does, the board resets and stage0's
     * next entry counts this hand-off as an unconfirmed attempt. The arm
     * STOPS the entry-armed watchdog first, so this window is fresh. */
    s0_hw_wdog_arm();
#endif
    register uint32_t a0 __asm__("a0") = r->entry_a0;
    register uint32_t a1 __asm__("a1") = r->entry_a1;
    register uint32_t a2 __asm__("a2") = 0u;
    register uint32_t pc __asm__("t0") = r->entry_pc;
    __asm__ volatile(".option push\n"
                     ".option arch, +zifencei\n"
                     "fence.i\n"
                     ".option pop\n"
                     "jr %[pc]\n"
                     : : "r"(a0), "r"(a1), "r"(a2), [pc] "r"(pc) : "memory");
    __builtin_unreachable();
}

/* ---- main ---------------------------------------------------------------------------- */

static const char *entry_text(uint32_t ek)
{
    switch (ek) {
    case S0_EK_COLD:       return "cold (FPGA configured)";
    case S0_EK_WARM:       return "warm";
    case S0_EK_WDOG:       return "WATCHDOG before a hand-off";
    case S0_EK_WDOG_LINUX: return "watchdog after a hand-off";
    case S0_EK_RESETTLE:   return "warm, inside the cold settle";
    default:               return "?";
    }
}

int main(void)
{
    static const uint8_t mac[6] = { MPS3_MAC0, MPS3_MAC1, MPS3_MAC2,
                                    MPS3_MAC3, MPS3_MAC4, MPS3_MAC5 };
    static const struct s0_build_ids ids = {
        (uint32_t)S0_BUILD_ID, (uint32_t)MPS3_STATIC_ID, (uint32_t)MPS3_VER32
    };
    struct s0_line l;
    struct s0_result res;
    int ddr_ok;
    int net_up = 0;

    uart_init();
    timer_init();
    uint32_t cause = s0_hw_wdog_status();
    uint32_t ek = s0_status_open(ST, &ids, cause, S0_BOOT_LIMIT);
    /* THE BOARD IDENTITY (stage0_status.h): published at EVERY entry, before any
     * DDR or card access, so Linux reads it however this run ends. */
    s0_status_publish_identity(ST, S0_IP, mac, S0_LABEL_LO, S0_LABEL_HI);
    if (ek == S0_EK_WDOG)
        s0_hw_wdog_clear_wrs();   /* counted: the next entry's WRS is its own */
#if S0_WDOG_ARM
    /* F1: supervise stage0 itself. A hart frozen on a stalled DDR transaction
     * now resets the board (the reset also clears the DDR AXI path, [SEAM-2])
     * instead of hanging it; s0_poll_hook() kicks it from every loop. */
    s0_hw_wdog_arm();
#endif

    s0l_init(&l);
    s0l_str(&l, "\nMBV-STAGE0 build ");
    s0l_hex(&l, S0_BUILD_ID);
    s0l_str(&l, " fabric ");
    s0l_hex(&l, MPS3_STATIC_ID);
    s0l_str(&l, " boot #");
    s0l_dec(&l, ST->boot_count);
    s0l_str(&l, (cause & WDOG_WRS) ? " (watchdog restart)" : "");
    emit(&l);
    s0l_str(&l, "stage0: entry ");
    s0l_str(&l, entry_text(ek));
    if (ek != S0_EK_COLD) {
        s0l_str(&l, "; before it: ");
        s0l_str(&l, phase_text(S0_PREV_PHASE(ST->prev_phase)));
        s0l_str(&l, " sub ");
        s0l_hex(&l, ST->prev_phase);
        s0l_str(&l, " at ");
        s0l_dec(&l, ST->prev_uptime_ms);
        s0l_str(&l, " ms");
    }
    if (S0_ENTRY_WDOG_RUN(ST->entry)) {
        s0l_str(&l, "; wdog run ");
        s0l_dec(&l, S0_ENTRY_WDOG_RUN(ST->entry));
    }
    emit(&l);
    if (ST->last_verdict != S0_VD_NONE) {
        s0l_str(&l, "stage0: previous boot from ");
        s0l_str(&l, ST->verdict_from == S0_FROM_A ? "slot A" :
                    ST->verdict_from == S0_FROM_B ? "slot B" : "a rescue push");
        s0l_str(&l, ST->last_verdict == S0_VD_CONFIRMED ? ": confirmed"
                                                        : ": NOT confirmed (a failed attempt)");
        emit(&l);
    }

    for (;;) {
        int from = s0_boot_select(ST, &k_ops, &res, &ddr_ok);
        ST->sd_rd_fails = s0_usd_read_fails();
        ST->sd_rd_last = s0_usd_read_diag();
        if (from != S0_FROM_NONE)
            handoff(from, &res);

        /* ---- rescue ---- */
        s0l_str(&l, "stage0: RESCUE (");
        s0l_str(&l, s0_rescue_reason_text(ST->rescue_reason));
        s0l_str(&l, "): ");
        s0l_ip(&l, S0_IP);
        s0l_str(&l, " -- ping, TFTP put <boot image> (port 69), identify 6899");
        emit(&l);
        ST->phase = S0_PH_RESCUE;
        ST->subphase = S0_SUBPHASE(S0_SP_RESCUE, 0u);
        if (!net_up) {
            int rc = s0_eth_init(S0_LAN_BASE, mac);
            ST->net_rc = (uint32_t)rc;
            if (rc != 0) {
                ST->rescue_state = S0_RS_NONET;
                ST->last_error = S0_LAST_ERROR(S0_ES_NET, (uint32_t)-rc);
                log_line(0, "stage0: LAN9220 init FAILED -- no rescue possible; parked");
                for (;;)
                    s0_poll_hook();
            }
            net_up = 1;
        }

        struct s0_rescue_cfg cfg = { {0}, 0, 0, 0, 0, 0, 0, 0, 0, 0 };
        for (int i = 0; i < 6; ++i)
            cfg.mac[i] = mac[i];
        cfg.ip = S0_IP;
        cfg.shell_id = MPS3_STATIC_ID;
        cfg.ddr_ok = ddr_ok;
        cfg.stage = (uint8_t *)(uintptr_t)S0_STAGE_BASE;
        cfg.stage_max = S0_IMAGE_MAX;
        cfg.verify = verify_pushed;
        cfg.log = log_line;
        s0_rescue_start(&cfg, ST);

        /* After a DDR failure, keep watching the calib bit: when it HOLDS
         * again, leave rescue and run the boot order again (pushes are
         * refused meanwhile -- DDR is not usable -- so none is in flight). */
        int watch = S0_CALIB_TELEM && ST->rescue_reason == S0_RR_DDR &&
                    S0_ENTRY_DDR_RECOVER(ST->entry) < S0_DDR_RECOVER_MAX;
        struct s0_hold h;
        s0_hold_init(&h);
        for (;;) {
            s0_poll_hook();
            uint32_t now = mps3_sys_now_ms();
            if (s0_rescue_poll(now, &res))
                handoff(S0_FROM_RESCUE, &res);
            if (watch && s0_hold_step(&h, s0_hw_calib_bit(), now, S0_CALIB_HOLD_MS)) {
                log_line(0, "stage0: DDR4 calib holds again -- leaving rescue, retrying the boot");
                break;
            }
        }
        s0_status_note_calib_drops(ST, h.drops);
    }
}
