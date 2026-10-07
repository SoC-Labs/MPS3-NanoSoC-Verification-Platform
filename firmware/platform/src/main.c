/*
 * main.c — MPS3 static-shell firmware entry point (firmware/platform, A3):
 * the file that finally ties the whole seam architecture to real hardware.
 *
 * Boot order (ARCHITECTURE_SPEC §6.1):
 *   1. free-running AXI-timer timebase (sys_now for lwIP, poll model — no
 *      interrupts anywhere in v1, see "Poll model" below)
 *   2. mps3_net_lwip_init(): smsc911x bring-up (BYTE_TEST/ID_REV checks
 *      inside) -> lwIP -> netif up at 192.168.10.101 (net-protocol.md
 *      static default; DHCP = D8 option, not compiled in)
 *   3. coordinator_init(): every module _init() (clkrst, swap_fsm,
 *      config_agent, overlay_store, swd_server, xvc_server, uart_over_eth,
 *      coordinator_net) — all their listen() calls land on the live lwIP
 *      backend now — then the boot-time default-overlay load (§6.1 step 4)
 *   4. superloop.
 *
 * THE LOOP IS A TABLE (s_services, below). One pass = one walk of it, with
 * every service timed against a budget in microseconds; the per-pass and
 * per-service worst cases and the sick-service skip mask land in the diag
 * mailbox at +0x7C..+0xAC. See firmware/common/service.h for the policy. The
 * sequence itself is unchanged from the hand-written loop this replaced.
 *
 * WHY THIS FILE OWNS THE LOOP instead of calling coordinator_main_loop():
 * that function's lwIP-servicing lines are deliberately seam-side comments
 * (coordinator.c must stay lwIP-free — firmware/README.md "no firmware
 * module includes an lwIP or Xilinx header"). This platform file is the
 * one place both worlds may meet, so it runs the same poll sequence
 * coordinator_main_loop() documents, plus the two platform-only calls
 * (mps3_net_lwip_rx_poll / mps3_net_lwip_tmr). If coordinator_main_loop()
 * ever grows a backend-poll seam hook, this loop collapses into it.
 *
 * Poll model: no INTC/ISRs at all in v1. The BD wires INTC In0..3
 * (HWICAP/Timer/UARTLite/eth_irq) so an interrupt build is available
 * later, but every consumer here is already a bounded non-blocking
 * _poll(), the LAN9220 driver is poll-mode by design (smsc911x/README
 * point 5), and the timer is only a free-running counter for sys_now —
 * skipping XIntc/Xil_Exception entirely removes those drivers from the
 * 128 KiB LMB budget. Revisit (eth_irq first) only if 10/100 polling
 * measurably drops frames on real hardware. NOTE: the LAN9220 IRQ_CFG
 * polarity fix (IRQ_POL=1 before unmasking, RESULT.txt flag) belongs to
 * that future interrupt build — with INT_EN=0 (driver init step 8) the
 * ETH_INT line stays quiet regardless.
 */
#include <stdint.h>

#include "xparameters.h"
#include "xil_printf.h"
#include "xuartlite_l.h"   /* XUartLite_SendByte -- the outbyte() override below */

#include "../../common/platform_regs.h"
#include "../../common/net_proto.h"
#include "../../common/diag.h"
#include "../../common/service.h"   /* the service table + mps3_sys_now_us() */
#include "../../common/log_ring.h"  /* v0.11 console tee behind the `log` verb */
#include "../../coordinator/coordinator.h"
#include "../../coordinator/swap_fsm.h"
#include "../../config_agent/config_agent.h"
#include "../../overlay_store/overlay_store.h"  /* D13: the "usd" service row + diag word */
#include "../../smsc911x/smsc911x.h"
#include "../../swd_server/swd_server.h"   /* compiled but dormant post-JTAG cutover */
#include "../../jtag_server/jtag_server.h" /* JTAG remote_bitbang backend (jtag_bb @0x44A7) */
#include "../../xvc_server/xvc_server.h"
#include "../../uart_over_eth/uart_over_eth.h"
#include "net_if_lwip.h"
#include "ovlstore_phase.h"   /* strong mps3_ovlstore_phase() override + getter */
#ifdef MPS3_HAS_CLCD
#include "../../clcd/clcd.h"  /* on-board CLCD status display -- GATED, default OFF */
#endif
#ifdef MPS3_HAS_TOUCH
#include "../../touch/touch.h"  /* resistive touch (STMPE811) -- GATED, default OFF */
#endif

/* ==========================================================================
 * Shell-internal peripheral bases from the BSP's xparameters.h, with the
 * shell-regmap/bd_summary values as documented fallbacks (fpga/shell
 * README "Address map": EMC LAN9220 window 0xC000_0000, TIMER
 * 0x41C0_0000). The #ifndef chains absorb instance-name drift between
 * XSA regenerations; if a fallback is ever the one used, confirm against
 * build_results_<date>/bd_summary.txt.
 * ========================================================================== */
#if defined(XPAR_AXI_EMC_0_S_AXI_MEM0_BASEADDR)
#define MPS3_LAN9220_BASE ((uintptr_t)XPAR_AXI_EMC_0_S_AXI_MEM0_BASEADDR)
#elif defined(XPAR_EMC_0_S_AXI_MEM0_BASEADDR)
#define MPS3_LAN9220_BASE ((uintptr_t)XPAR_EMC_0_S_AXI_MEM0_BASEADDR)
#elif defined(XPAR_AXI_EMC_0_BASEADDR)
#define MPS3_LAN9220_BASE ((uintptr_t)XPAR_AXI_EMC_0_BASEADDR)
#else
#define MPS3_LAN9220_BASE ((uintptr_t)0xC0000000u) /* bd_summary fallback */
#endif

#if defined(XPAR_AXI_TIMER_0_BASEADDR)
#define MPS3_TIMER_BASE ((uintptr_t)XPAR_AXI_TIMER_0_BASEADDR)
#elif defined(XPAR_TMRCTR_0_BASEADDR)
#define MPS3_TIMER_BASE ((uintptr_t)XPAR_TMRCTR_0_BASEADDR)
#else
#define MPS3_TIMER_BASE ((uintptr_t)0x41C00000u) /* bd_summary fallback */
#endif

#if defined(XPAR_AXI_TIMER_0_CLOCK_FREQ_HZ)
#define MPS3_TIMER_HZ XPAR_AXI_TIMER_0_CLOCK_FREQ_HZ
#elif defined(XPAR_CPU_CORE_CLOCK_FREQ_HZ)
#define MPS3_TIMER_HZ XPAR_CPU_CORE_CLOCK_FREQ_HZ
#else
#define MPS3_TIMER_HZ 100000000u /* shell's fixed 100 MHz AXI clock, spec §5 */
#endif

/* AXI Timer (PG079) register offsets — used raw (free-run counter only)
 * rather than pulling the whole XTmrCtr driver into the LMB budget. */
#define TMR_TCSR0 0x00u
#define TMR_TLR0  0x04u
#define TMR_TCR0  0x08u
#define TMR_TCSR0_ENT0  (1u << 7) /* enable                       */
#define TMR_TCSR0_LOAD0 (1u << 5) /* load TLR0 into the counter   */
#define TMR_TCSR0_ARHT0 (1u << 4) /* auto-reload (free-run wrap)  */

/* Heartbeat: board_gpio bit 0, host-owned (pads[7:0] = LEDs per the
 * fpga/shell README pad-map contract) — same policy the harness_app spike
 * used; ~1 Hz blink = "superloop alive". */
#define HEARTBEAT_GPIO_BIT (1u << 0)
#define HEARTBEAT_HALF_MS  500u

static void timer_freerun_init(void)
{
    mps3_reg_write32(MPS3_TIMER_BASE, TMR_TLR0, 0);
    mps3_reg_write32(MPS3_TIMER_BASE, TMR_TCSR0, TMR_TCSR0_LOAD0);
    mps3_reg_write32(MPS3_TIMER_BASE, TMR_TCSR0, TMR_TCSR0_ENT0 | TMR_TCSR0_ARHT0);
}

/* Monotonic milliseconds off the free-running 32-bit counter. Incremental
 * delta accumulation (called every superloop pass, far inside the ~43 s
 * @100 MHz wrap window) keeps this wrap-correct with pure 32-bit math —
 * no 64-bit soft division per call on the div-less MicroBlaze config. */
uint32_t mps3_sys_now_ms(void)
{
    static uint32_t last_tcr, tick_rem, ms;
    const uint32_t ticks_per_ms = MPS3_TIMER_HZ / 1000u;

    uint32_t cur = mps3_reg_read32(MPS3_TIMER_BASE, TMR_TCR0);
    uint32_t delta = cur - last_tcr; /* unsigned wrap-safe */
    last_tcr = cur;

    tick_rem += delta;
    if (tick_rem >= ticks_per_ms) {
        ms += tick_rem / ticks_per_ms;
        tick_rem %= ticks_per_ms;
    }
    return ms;
}

/* Monotonic MICROSECONDS off the same free-running counter. Separate delta
 * accumulator from mps3_sys_now_ms() above -- both read the same TCR0, each
 * keeps its own `last`, and each is called far inside the ~43 s @100 MHz wrap
 * window, so the two never need to agree about anything but the hardware.
 *
 * This is what the service table measures with and what mps3_spin_until()
 * bounds with, which is why it has to exist at all: a budget of "5 ms" cannot
 * be enforced by a clock whose tick is 1 ms.
 *
 * The 32-bit microsecond value itself wraps every ~71.6 minutes; every consumer
 * subtracts and compares signed, never `<`. */
uint32_t mps3_sys_now_us(void)
{
    static uint32_t last_tcr, tick_rem, us;
    const uint32_t ticks_per_us = MPS3_TIMER_HZ / 1000000u;

    uint32_t cur = mps3_reg_read32(MPS3_TIMER_BASE, TMR_TCR0);
    uint32_t delta = cur - last_tcr; /* unsigned wrap-safe */
    last_tcr = cur;

    tick_rem += delta;
    if (tick_rem >= ticks_per_us) {
        us += tick_rem / ticks_per_us;
        tick_rem %= ticks_per_us;
    }
    return us;
}

/* ==========================================================================
 * THE CONSOLE TEE (net-protocol v0.11 `log`).
 *
 * xil_printf() emits every byte through outbyte(). The BSP's own outbyte()
 * (libxil.a, standalone_v9_1/src/outbyte.c) is exactly one line:
 *     XUartLite_SendByte(STDOUT_BASEADDRESS, c);
 * Defining outbyte() HERE means the linker never pulls that archive member, so
 * this is the one definition -- and it does what the BSP's did, plus one store
 * into the RAM ring (common/log_ring.c) so the 6900 `log` verb can hand the
 * console back to a host that was not listening when it was printed.
 * The UART write is unchanged and still blocks on a full TX FIFO exactly as
 * before; the ring store costs a few cycles and never blocks.
 * ========================================================================== */
void outbyte(char c);
void outbyte(char c)
{
    mps3_log_putc(c);
    XUartLite_SendByte(STDOUT_BASEADDRESS, (u8)c);
}

/* The banner, as ONE function, because it is printed TWICE: at boot, and again
 * MPS3_BANNER_REPRINT_MS later from the superloop. At power-on the board's
 * serial path is not open for the first ~1 s after configuration (the MCC is
 * still running its post-configuration steps), and this firmware prints the
 * whole banner inside that second -- nine power cycles in a row lost it
 * (docs/evidence/2026-09-w2/p1_console_20260923.txt). A late capture now sees
 * it on the second printing, and the `log` verb has both. The lines are
 * byte-identical both times so one grep finds either. */
#ifndef MPS3_BANNER_REPRINT_MS
#define MPS3_BANNER_REPRINT_MS 5000u
#endif
static void print_banner_start(void)
{
    xil_printf("\r\n--- MPS3 nanoSoC shell firmware (A3) starting ---\r\n");
}
static void print_banner_up(void)
{
    xil_printf("shell up: IP %d.%d.%d.%d  static_id 0x%08x\r\n"
               "ports: 6900 ctl / 69+6910 push / 2542 xvc / 6921 jtag / "
               "6930-6932 uart+swo\r\n",
               MPS3_DEFAULT_IP_A, MPS3_DEFAULT_IP_B,
               MPS3_DEFAULT_IP_C, MPS3_DEFAULT_IP_D,
               (unsigned)g_shell_state.static_id);
}

/* One-shot, from the heartbeat service (the table is full at MPS3_SVC_MAX, and
 * the heartbeat already reads the clock every pass). Bounded: three lines,
 * ~200 bytes, once per boot -- at 115200 baud the UART-lite's 16-byte FIFO makes
 * that ~17 ms of blocking in ONE pass, which is one budget overrun on `hbeat`,
 * never three in a row, so it can never mark the service sick. */
static void banner_reprint_service(uint32_t now_ms)
{
    static int done;
    if (done || now_ms < MPS3_BANNER_REPRINT_MS) {
        return;
    }
    done = 1;
    xil_printf("\r\n(banner repeated at +%u ms for a late serial capture)",
               (unsigned)now_ms);
    print_banner_start();
    print_banner_up();
}

/* The `stats` link/MAC seam (coordinator.h): the strong override of
 * coordinator.c's weak "unknown" default. Same reads the CLCD's network row
 * makes (clcd_fmt_net): BMSR link, then ANLPAR's most-capable technology bit.
 * Two MII reads per `stats`, only when asked. */
void mps3_stats_net(mps3_stats_net_t *out)
{
    uint16_t anlpar = 0u;
    out->link = (smsc911x_link_up() == 1);
    out->spd  = 0u;
    out->fdx  = 0;
    if (out->link && smsc911x_mii_read(0x05u, &anlpar) == 0) {
        /* ANLPAR [8]100TX-FD [7]100TX [6]10T-FD [5]10T */
        if      (anlpar & (1u << 8)) { out->spd = 100u; out->fdx = 1; }
        else if (anlpar & (1u << 7)) { out->spd = 100u; out->fdx = 0; }
        else if (anlpar & (1u << 6)) { out->spd = 10u;  out->fdx = 1; }
        else                         { out->spd = 10u;  out->fdx = 0; }
    }
    mps3_platform_mac(out->mac);
}

static void heartbeat_init(void)
{
    mps3_reg_set_bits32(MPS3_GPIO_BASE, GPIO_OWN, HEARTBEAT_GPIO_BIT);
    mps3_reg_set_bits32(MPS3_GPIO_BASE, GPIO_OE,  HEARTBEAT_GPIO_BIT);
}

static void heartbeat_service(void)
{
    static uint32_t last_ms;
    static int on;
    uint32_t now = mps3_sys_now_ms();
    banner_reprint_service(now);
    if ((uint32_t)(now - last_ms) >= HEARTBEAT_HALF_MS) {
        last_ms = now;
        on = !on;
        if (on) {
            mps3_reg_set_bits32(MPS3_GPIO_BASE, GPIO_OUT, HEARTBEAT_GPIO_BIT);
        } else {
            mps3_reg_clr_bits32(MPS3_GPIO_BASE, GPIO_OUT, HEARTBEAT_GPIO_BIT);
        }
    }
}

/* ==========================================================================
 * THE SERVICE TABLE — one pass = one walk of this array.
 *
 * This used to be nine bare calls in the for(;;) below. The problem was not
 * that a list of calls is untidy; it is that a list of calls cannot be
 * MEASURED. When the board wedged, the mailbox could name the last QSPI phase
 * or the last ICAP status, and nothing at all could name the service that ate
 * the pass -- which is the first question a superloop wedge asks. A table is
 * data, so the loop can time itself: see firmware/common/service.h for the
 * budget/sick policy and the diag rows at +0x7C..+0xAC for what reaches the
 * wire.
 *
 * INDEX IS IDENTITY. svc_worst_ix and svc_skipped in the mailbox are INDICES
 * into this array, and the packed per-service maxima are keyed by index too.
 * So the table is APPEND-ONLY and its length must NOT change with a build
 * flag -- which is why the CLCD slot is always present and becomes a no-op
 * when MPS3_HAS_CLCD is off, rather than being #ifdef'd out and silently
 * renumbering every service after it in a non-CLCD image.
 *
 * BUDGETS ARE PROVISIONAL, and deliberately loose. Nothing here has been
 * measured on silicon yet -- svc_max_us_0..5 in the mailbox is precisely the
 * measurement that will tighten them, and until then each budget is set about
 * an order of magnitude above what the step is expected to cost. The failure
 * mode of a budget set too tight is mild by construction: three consecutive
 * overruns throttle the service to one probe per MPS3_SVC_COOLDOWN_US (100 ms)
 * rather than dropping it, and svc_skipped says so out loud.
 *
 * ONE RULE THE BUDGETS MUST OBEY, and it is not arbitrary: a service that can
 * reach lan_linkoutput() (anything that sends -- net_rx, net_tmr, tx_drain,
 * ctrl, cfgagent, jtag, xvc, uart) can contain one MPS3_TX_SPACE_TIMEOUT_US
 * (5 ms) wait on a stuck LAN9220 TX FIFO. Its budget must be ABOVE that, or a
 * single TX-space stall trips the watchdog and the sick logic starts chasing a
 * fault it did not cause. net_rx is 20 ms rather than 5 because it drains up to
 * MPS3_NET_RX_BUDGET_FRAMES frames and each may push an ACK.
 *
 * Being skipped is survivable for every one of them, which is why the policy is
 * safe to ship before the budgets are measured: the 100 ms probe interval is
 * FASTER than TCP_TMR_INTERVAL (250 ms), so even a skipped net_tmr still fires
 * its timers on time, and a skipped net_rx still drains 8 frames every 100 ms.
 * ========================================================================== */

/* Frames drained into lwIP per pass. Was the literal 8 in the old loop. */
#define MPS3_NET_RX_BUDGET_FRAMES 8

static void svc_net_rx(void)   { (void)mps3_net_lwip_rx_poll(MPS3_NET_RX_BUDGET_FRAMES); }
static void svc_net_tmr(void)  { mps3_net_lwip_tmr(); }   /* TCP/ARP timers (manual, NO_SYS) */

static void svc_tx_drain(void)
{
    /* Reap completed TX status every pass: an un-drained TX STATUS FIFO halts
     * the MAC (the sustained-TX stall). tx_frame also drains at entry, but this
     * reaps the LAST frame's status too (it posts just after tx_frame
     * returned). Cheap when idle (one TX_FIFO_INF read). */
    (void)smsc911x_tx_status_drain();
}

static void svc_clcd(void)
{
#ifdef MPS3_HAS_CLCD
    /* on-board CLCD status display (bounded, non-blocking; gated -- see
     * clcd_init below). clcd_poll() ALSO services the CLCD KVM (@0x44AD) when
     * MPS3_HAS_CLCD_KVM is built: the whole panel-handover state machine lives
     * inside it, so this table needs no extra entry. */
    clcd_poll();
#endif
    /* Without MPS3_HAS_CLCD this is a no-op that KEEPS THE SLOT, so service
     * indices -- which are mailbox contract -- do not depend on a build flag. */
}

static void svc_diag(void)
{
    /* Refresh the diagnostic mailbox every pass so the JTAG-readable struct is
     * current at the instant of any wedge (the ONLY path readable during a
     * swap; 6900 telemetry is parked then). Cheap: all reads are of in-RAM
     * counters / pcb fields, no MMIO. */
    mps3_diag_t v = {0};
    smsc911x_get_diag(&v.rx_recover_events, &v.rx_recover_dumps,
                      &v.rx_drop_frames);
    v.icap_bytes = swap_fsm_icap_bytes();
    config_agent_rx_progress(&v.rx_payload_got, &v.rx_payload_expect);
    config_agent_win_diag(&v.win_windows_drained, &v.win_grant_send_fails);
    mps3_net_lwip_conn_diag(config_agent_active_tcp_conn(),
                            &v.tcp_rcv_wnd, &v.tcp_rcv_ann_wnd,
                            &v.rx_queued, &v.tcp_sndbuf, &v.tcp_snd_wnd);
    v.pbuf_free = mps3_net_lwip_pbuf_free();
    /* v5 TX-path counters: driver-side (frames/status/errors) + the
     * lwIP-linkoutput-side (full-drops/space-stalls/iface-errors). */
    smsc911x_get_tx_diag(&v.tx_frames_sent, &v.tx_status_drained,
                         &v.tx_errors);
    mps3_net_lwip_tx_diag(&v.tx_fifo_full_drops, &v.tx_space_stalls,
                          &v.tx_iface_errors);
    v.tx_last_status = smsc911x_tx_last_status();
    /* I18(3): raw HWICAP_SR + EOS-gate outcome from the stream-direct
     * finish, so the next real swap answers whether EOS asserts post-DESYNC
     * by a single JTAG read of the diag mailbox (diag.h icap_sr_last/_eos). */
    v.icap_sr_last    = swap_fsm_icap_sr_last();
    v.icap_eos_status = swap_fsm_icap_eos_status();
    /* v6 QSPI/overlay-store phase. The strong override (ovlstore_phase.c)
     * already writes the mailbox DIRECTLY at each stamp (so a wedge mid-QSPI-op
     * is visible); re-thread it through `v` here too so publish()'s whole-struct
     * copy does not clobber the phase back to 0 between the bounded steps that
     * DO return here. */
    mps3_ovlstore_phase_get(&v.ovlstore_phase, &v.ovlstore_detail);
#ifdef MPS3_HAS_TOUCH
    /* v7 STMPE811 panel-continuity probe. touch_init() (below) derived these
     * ONCE at bring-up -- the four accessors are plain reads of statics, no I2C
     * -- so republishing them every pass costs nothing and makes the verdict
     * readable over the 6900 `diag` verb as well as over JTAG. Without TOUCH=1
     * the touch driver is not linked at all and these words stay 0, which
     * decodes as "unknown": no probe ran, and that is the honest answer rather
     * than a fabricated one. */
    v.touch_probe_regs    = touch_probe_regs_word();
    v.touch_probe_adc_x   = touch_probe_adc_x_word();
    v.touch_probe_adc_y   = touch_probe_adc_y_word();
    v.touch_probe_verdict = touch_probe_verdict();
#endif
    /* v8 superloop service telemetry. Note this service is ITSELF in the table
     * (index 10), so the figures it publishes are the state as of the START of
     * its own call: services 10 and 11 of the CURRENT pass land in the mailbox
     * one pass later. They are high-water marks since boot, so a one-pass lag
     * costs nothing -- and gathering them any other way would mean the diag
     * service excluding itself from its own measurement, which is worse. */
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
    v.usd_boot           = overlay_store_diag_word();      /* v9: the power-on latch */

    mps3_diag_publish(&v);
}

/* APPEND-ONLY. The index of every row below is mailbox contract. */
static const mps3_service_t s_services[] = {
    /*  0 */ { "net_rx",   svc_net_rx,         20000u },  /* 8 frames -> lwIP        */
    /*  1 */ { "net_tmr",  svc_net_tmr,        10000u },  /* tcp_tmr / etharp_tmr    */
    /*  2 */ { "tx_drain", svc_tx_drain,        5000u },  /* reap TX status FIFO     */
    /*  3 */ { "swap",     swap_fsm_poll,      50000u },  /* one ICAP chunk          */
    /*  4 */ { "cfgagent", config_agent_poll,  50000u },  /* TFTP 69 / TCP 6910 + QSPI */
    /*  5 */ { "ctrl",     coordinator_net_poll, 10000u },/* control channel 6900    */
    /*  6 */ { "jtag",     jtag_server_poll,   10000u },  /* 6921 (jtag_bb @0x44A7)  */
    /*  7 */ { "xvc",      xvc_server_poll,    50000u },  /* 2542, bit-bang bursts   */
    /*  8 */ { "uart",     uart_over_eth_poll, 10000u },  /* 6930/6931/6932          */
    /*  9 */ { "clcd",     svc_clcd,           30000u },  /* panel; no-op if !CLCD.  */
                                                      /* MEASURED 2026-09-14 (rc4, W1): the boot-time
                                                      * init frame is ~126 ms ONCE (a single overrun,
                                                      * never K=3 in a row, so never sick); steady
                                                      * state is microseconds. It was 200 ms, which
                                                      * was a licence, not a budget: on 2026-09-22 a
                                                      * held touch ran clcd at 65-150 ms per pass
                                                      * with ZERO overruns recorded. 30 ms is ~10x a
                                                      * rate-limited touch sample with every IIC wait
                                                      * at its TOUCH_IIC_WAIT_US bound. */
    /* 10 */ { "diag",     svc_diag,            2000u },  /* the mailbox gather      */
    /* 11 */ { "hbeat",    heartbeat_service,   1000u },  /* ~1 Hz "loop alive" LED  */
    /* 12 */ { "usd",      overlay_store_service, 1500u },/* user uSD + overlay store
                                                      * (D13): usd_poll <= ~0.52 ms +
                                                      * <= 512 B of CRC ~0.4 ms + one
                                                      * op start. A budget, not a
                                                      * licence. */
};

/* A4: the control channel between heavy services (service.h, the INTERLEAVE).
 * Drain a few frames into lwIP -- a 6900 request has to get OFF the LAN9220
 * before anything can answer it -- then run the 6900 listener. Deliberately
 * smaller than a full pass: 4 frames, not MPS3_NET_RX_BUDGET_FRAMES, and no
 * timers (net_tmr's cadence is time-based and a pass is far inside it). */
#define MPS3_ILV_RX_FRAMES 4
static void svc_interleave(void)
{
    (void)mps3_net_lwip_rx_poll(MPS3_ILV_RX_FRAMES);
    coordinator_net_poll();
}
/* Services the interleave follows: the two whose single call can be long while
 * the control channel is otherwise idle -- xvc (bit-bang bursts, 50 ms budget)
 * and clcd (the 2026-09-22 starvation). swap/cfgagent are left alone on
 * purpose: during a push/swap 6900 is parked on the swap's held reply anyway,
 * and the proven swap path's poll order is not something to perturb for a
 * latency nobody is waiting on. Indices are s_services' (mailbox contract). */
#define MPS3_ILV_AFTER_MASK ((1u << 7) | (1u << 9))

int main(void)
{
    print_banner_start();

    timer_freerun_init();
    heartbeat_init();

    uint8_t mac[6];
    mps3_platform_mac(mac);

    int rc = mps3_net_lwip_init(MPS3_LAN9220_BASE, mac);
    if (rc != 0) {
        /* BYTE_TEST failure = the EMC address-alignment bring-up flag
         * (RESULT.txt / fpga/shell README: SMBF_ADDR = addr[7:1]); ID
         * failure = not a LAN9220 behind the window. Park with the code
         * on the console + a fast LED blink — nothing network-facing can
         * run without the port. */
        xil_printf("FATAL: network bring-up failed rc=%d "
                   "(-1 BYTE_TEST -2 ID -3 timeout; see smsc911x.h)\r\n", rc);
        for (;;) {
            static uint32_t last;
            uint32_t now = mps3_sys_now_ms();
            if ((uint32_t)(now - last) >= 125u) {
                last = now;
                static int on;
                on = !on;
                if (on) mps3_reg_set_bits32(MPS3_GPIO_BASE, GPIO_OUT, HEARTBEAT_GPIO_BIT);
                else    mps3_reg_clr_bits32(MPS3_GPIO_BASE, GPIO_OUT, HEARTBEAT_GPIO_BIT);
            }
        }
    }

    /* All module inits + port listens (now against the live lwIP backend)
     * + the §6.1-step-4 boot default-overlay load. NOTE: the boot load is
     * a blocking pre-loop step by design (overlay_store.h's own doc: no
     * TCP connection exists yet to starve). */
    /* Stamp the diagnostic mailbox BEFORE coordinator_init(): that call runs the
     * §6.1-step-4 boot-time overlay load, which drives the QSPI path (erase/
     * program/RDSR-spin). If that path wedges, the mailbox must already carry the
     * magic — the JTAG reader (scripts/mps3_diag.tcl) scans for it — and the
     * overlay-store phase override (below) must have a live mailbox to stamp, so
     * even a first-light boot-load wedge is locatable. The counter fields are all
     * re-supplied by the per-poll gather, so stamping early costs nothing. */
    mps3_diag_init();
    coordinator_init();

    print_banner_up();

#ifdef MPS3_HAS_CLCD
    /* On-board CLCD status display, brought up AFTER the network. The slave at
     * MPS3_CLCD_BASE (0x44AC) IS on the shipped shell (shell_bd.tcl NUM_MI=15,
     * clcd_0 master index 14, b4afe3e) and the panel is lit on the board
     * (docs/CLCD_PANEL_FACTS.md). MPS3_HAS_CLCD is a build flag, default OFF; the
     * working board runs `make CLCD=1`. clcd_poll() below ALSO services the CLCD
     * KVM (@0x44AD) when MPS3_HAS_CLCD_KVM is built -- the whole panel-handover
     * state machine lives inside clcd_poll(), so this superloop needs no extra
     * hook. See firmware/clcd/clcd.h and fpga/shell/ip/clcd_kvm/README.md. */
    clcd_init();
#endif

#ifdef MPS3_HAS_TOUCH
    /* Touch bring-up witness. touch.c already captures the STMPE811 CHIP_ID at
     * touch_init(); log it, then sweep the bus once so three cases are told
     * apart on silicon: part present at 0x41, part at 0x44 (A0=VCC strap), or
     * nothing ACKing at all (dead bus / no pull-ups -- mps3_harness_touch.xdc).
     * The sweep is touch_bus_scan(), a bounded one-shot bring-up aid in the
     * driver itself -- NOT the MPS3_TOUCH_TEST_HOOKS, which stay out of the
     * target. It runs ONCE here, never in the superloop: that distinction is
     * the whole point of 8c2099e -- an ungated per-pass poll took the board
     * dark for a month. */
    {
        uint8_t  acks[8];
        unsigned found;
        unsigned i;

        touch_init();   /* AXI IIC soft-reset + enable, then CHIP_ID @ 0x41 */
        xil_printf("touch: chip_id 0x%04x (expect 0x0811 at 0x41)\r\n",
                   (unsigned)touch_chip_id());

        found = touch_bus_scan(acks, (unsigned)(sizeof acks / sizeof acks[0]));
        xil_printf("touch: i2c scan: %u ack", found);
        for (i = 0u; i < found && i < (unsigned)(sizeof acks / sizeof acks[0]); i++) {
            xil_printf(" 0x%02x", (unsigned)acks[i]);
        }
        xil_printf("\r\n");
    }
#endif

    /* The superloop — one call, because the pass is a TABLE now (s_services
     * above). Everything coordinator_main_loop() documents still happens in
     * the same order; what is new is that every step is timed against a budget
     * and the worst cases reach the mailbox. */
    {
        const unsigned n = (unsigned)(sizeof s_services / sizeof s_services[0]);
        const unsigned got = mps3_service_install(s_services, n);
        if (got != n) {
            /* The table outgrew MPS3_SVC_MAX. install() REFUSES the overflow
             * rather than running services it cannot report on, so the extra
             * ones would simply never be polled -- a service that silently
             * stops running is the worst failure this file can have, so say it
             * on the console at boot. Fix: raise MPS3_SVC_MAX and add the
             * matching svc_max_us_<n> mailbox rows (service.h explains the
             * MPS3_SVC_PACK_WORDS arithmetic). */
            xil_printf("FATAL: service table has %u entries but only %u fit "
                       "(MPS3_SVC_MAX) -- %u service(s) WILL NOT RUN\r\n",
                       n, got, n - got);
        }
        mps3_service_set_interleave(svc_interleave, MPS3_ILV_AFTER_MASK);
        xil_printf("superloop: %u services, budget watchdog K=%u, "
                   "cooldown %u us, ctl interleave 0x%x\r\n",
                   got, (unsigned)MPS3_SVC_SICK_K,
                   (unsigned)MPS3_SVC_COOLDOWN_US,
                   (unsigned)MPS3_ILV_AFTER_MASK);
    }
    for (;;) {
        mps3_service_run_pass();
    }
    /* unreachable */
}

/* The overlay-store phase override (strong mps3_ovlstore_phase + getter) is
 * compiled as part of THIS translation unit rather than as its own SRCS entry:
 * firmware/platform/Makefile is concurrently owned by the MF-1 clearing-size
 * workstream, so the override rides main.c to avoid touching that source list.
 * It is compiled exactly once (not in SRCS => no duplicate symbol). The host
 * test (firmware/test) links ovlstore_phase.c standalone. See its header. */
#include "ovlstore_phase.c"
