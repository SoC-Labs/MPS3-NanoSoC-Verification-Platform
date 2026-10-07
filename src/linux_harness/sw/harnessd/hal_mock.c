/*
 * hal_mock.c — the MPS3_HAL_MOCK backend: a BEHAVIOURAL model of the shell
 * fabric harnessd talks to, for the host tests and the QEMU smoke.
 *
 * WHY NOT firmware/test/mock_regs.c: that is a flat 256-slot register file with
 * a FAKE clock that only moves when a test moves it, built for single-module
 * unit tests. harnessd runs the whole shell against real time and real sockets,
 * so it needs registers that ANSWER — a decoupler that reports decoupled, an
 * ICAP that drains its FIFO, a JTAG TAP that shifts an IDCODE out, a watchdog
 * timebase that moves — or the swap FSM, the XVC engine and `reboot` could never
 * be driven end to end off a board.
 *
 * WHAT IT SERVES: exactly the blocks hal_expected_blocks() lists (the UIO
 * build's DTS contract). Anything else is FATAL, same message shape as
 * hal_uio.c, so a service module that strays outside the contract fails on the
 * host before it fails on the board.
 *
 * THE BACKING STORE can be a FILE (--mock-fabric PATH), mmap'd MAP_SHARED, so:
 *   - two harnessd processes run one after the other see ONE fabric — which is
 *     what makes a respawn testable (the RP state, the stage0 status block and
 *     the mailbox outlive the process, as they do on the board);
 *   - a test can read or poke any register, the stage0 block and the mailbox
 *     from Python with mmap while harnessd runs.
 *
 * FILE LAYOUT (tests/harnessd_mock.py mirrors it; change both together):
 *   0x00000  mock_hdr_t (below; fixed offsets asserted)
 *   0x01000 + i * 0x10000   the 64 KiB register page of hal_expected_blocks()[i],
 *                           in that list's FIXED order (clkrst, dfxctl, hwicap,
 *                           vphy, genchk, jtag-bb, dbgbr, uartbr, gpio, mmcm-drp,
 *                           clcd, clcd-kvm, touch-iic, dutegr, usracc, wdog,
 *                           lmb-tail). The lmb-tail page holds the stage0 status
 *                           block at +0xE00 and the diag mailbox at +0xF00.
 *
 * BEHAVIOURS (every other register is a plain read/write word):
 *   CLKRST  STATUS reads MMCM_LOCKED | DUT_CLK_ALIVE.
 *   DFXCTL  STATUS = DECOUPLE.en -> DECOUPLED, CLKRST.RESET_CTRL.rp_resetn==0 ->
 *           RP_IN_RESET. RM_ID / RM_STATUS.valid report hdr.rm_loaded only while
 *           the RP is connected (not decoupled, out of reset) — a decoupled RP
 *           drives 0, as the real decoupler does.
 *   HWICAP  WFV reads 1024 (FIFO mode, always room); CR.WRITE self-clears; SR
 *           reads DONE|EOS. Every WF word is scanned for the TEST MARKER
 *           0x524D4944 ("RMID") followed by an rm_id (either byte order): that
 *           rm_id becomes hdr.rm_loaded — how a test partial "contains" an RM.
 *   JTAGBB  DRIVE: a TCK 0->1 edge clocks the fake TAP (IDCODE 0x6BA00477, the
 *           SoC-400 SWJ-DP); SAMPLE returns its TDO.
 *   DBGBR   CTRL.GO shifts LENGTH bits of TMS/TDI through the SAME TAP, TDO
 *           sampled before each rising edge; GO self-clears.
 *   UARTBR  U0/U1 are LOOPBACKS (bytes written to TX come back on RX), so a
 *           6930/6931 client sees its own bytes echoed; SWO is empty.
 *   USRACC  MAGIC "USRA"; STATUS.valid/VALUE from hdr.usr_access{_valid}.
 *   MMCM_DRP clk_wiz_dut: CFG_REG0/CFG_REG2 are the plain register file, seeded
 *           on a blank page with a 50 MHz M/D/O (the IP's post-reset values);
 *           the MMCM ITSELF is hdr.mmcm_khz, the frequency it runs. A LOAD write
 *           (bit 0) applies the register file (SADDR = bit 1 set) or the IP's
 *           static 50 MHz configuration (SADDR clear) to it and counts
 *           hdr.mmcm_loads. The two are separate on purpose: a test models a
 *           POR/WDOG reset by rewriting the register file while mmcm_khz keeps
 *           the pre-reset preset, as the silicon does (clk_linux.c).
 *   CLCDKVM EVENT is W1C, as on the fabric (platform_regs.h): a test pokes a
 *           handover event (harness_gained / panel_reset_done) and clcd.c's
 *           take-events write clears exactly what it read, so the regain runs
 *           ONCE. Every other KVM register is plain (a test pokes STATUS).
 *   WDOG    TBR is a free-running 100 MHz count off CLOCK_MONOTONIC (so the
 *           `reboot` verb's presence probe sees it move) — FROZEN at 0 when the
 *           process runs with --wdog off, i.e. the mock fabric then has no
 *           watchdog and `reboot` declines exactly as ctrl_echo's does; TWCSR0.WDS
 *           / WRS are W1C; kicks (a WDS W1C while enabled) are counted in
 *           hdr.wdog_kicks.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#include "../../../../firmware/common/platform_regs.h"
#include "../../../../firmware/test/fake_jtag_tap.h"
#include "hal.h"
#include "harnessd.h"

#define MOCK_MAGIC       0x464D4448u   /* "HDMF" */
#define MOCK_VERSION     1u
#define MOCK_HDR_BYTES   0x1000u
#define MOCK_SLOT_BYTES  0x10000u
#define MOCK_ICAP_MARKER 0x524D4944u   /* "RMID" */
#define MOCK_TAP_IDCODE  0x6BA00477u
#define MOCK_UART_Q      256u

typedef struct {
    uint32_t head, tail;          /* free-running; count = head - tail */
    uint8_t  q[MOCK_UART_Q];
} mock_q_t;

typedef struct {
    uint32_t magic;               /* 0x00 */
    uint32_t version;             /* 0x04 */
    uint32_t slot_bytes;          /* 0x08 */
    uint32_t nslots;              /* 0x0C */
    uint32_t rm_loaded;           /* 0x10  what the RP holds                    */
    uint32_t icap_marker_state;   /* 0x14  0 idle, 1 = next word is the rm_id  */
    uint32_t icap_marker_swapped; /* 0x18  1 = the marker arrived byte-swapped  */
    uint32_t icap_words;          /* 0x1C  WF words written, ever               */
    uint32_t wdog_kicks;          /* 0x20                                       */
    uint32_t wdog_enables;        /* 0x24  writes that left both halves enabled */
    uint32_t wdog_disables;       /* 0x28  writes that cleared an enable        */
    uint32_t mmcm_loads;          /* 0x2C  MMCM_DRP LOAD writes, ever            */
    uint32_t usr_access;          /* 0x30                                       */
    uint32_t usr_access_valid;    /* 0x34                                       */
    uint32_t jtag_drive_prev;     /* 0x38                                       */
    uint32_t mmcm_khz;            /* 0x3C  what the modelled MMCM runs (0 = unseeded) */
    mock_q_t u0, u1;              /* 0x40..                                     */
    fake_tap_t tap;
} mock_hdr_t;

_Static_assert(offsetof(mock_hdr_t, rm_loaded) == 0x10, "mock layout");
_Static_assert(offsetof(mock_hdr_t, icap_words) == 0x1C, "mock layout");
_Static_assert(offsetof(mock_hdr_t, wdog_kicks) == 0x20, "mock layout");
_Static_assert(offsetof(mock_hdr_t, usr_access) == 0x30, "mock layout");
_Static_assert(offsetof(mock_hdr_t, mmcm_loads) == 0x2C, "mock layout");
_Static_assert(offsetof(mock_hdr_t, mmcm_khz) == 0x3C, "mock layout");
_Static_assert(offsetof(mock_hdr_t, u0) == 0x40, "mock layout");
_Static_assert(sizeof(mock_hdr_t) <= MOCK_HDR_BYTES, "mock header too big");

static uint8_t    *s_store;
static mock_hdr_t *s_hdr;
static unsigned    s_nslots;
static const hal_block_t *s_blocks;

static int s_wdog_absent;   /* --wdog off in a mock build: a fabric with no WDOG */

/* clk_wiz_dut's post-reset register file in this model: D=1, M=20, O=20
 * (1000 MHz VCO, 50 MHz out; the real IP's exact M/D/O are Vivado's choice). */
#define MOCK_MMCM_CFG0_DEFAULT ((20u << 8) | 1u)
#define MOCK_MMCM_CFG2_DEFAULT 20u
#define MOCK_MMCM_KHZ_DEFAULT  50000u
static void mmcm_seed(void);

const char *hal_backend_name(void) { return "mock"; }
unsigned    hal_backend_windows(void) { return s_nslots; }
void        hal_mock_set_wdog_absent(void) { s_wdog_absent = 1; }

static void hdr_init(void)
{
    memset(s_hdr, 0, sizeof(*s_hdr));
    s_hdr->magic      = MOCK_MAGIC;
    s_hdr->version    = MOCK_VERSION;
    s_hdr->slot_bytes = MOCK_SLOT_BYTES;
    s_hdr->nslots     = s_nslots;
    fake_tap_reset(&s_hdr->tap, MOCK_TAP_IDCODE);
}

int hal_backend_open(const hal_opts_t *o)
{
    s_blocks = hal_expected_blocks(&s_nslots);
    size_t total = MOCK_HDR_BYTES + (size_t)s_nslots * MOCK_SLOT_BYTES;
    int fresh = 1;

    if (o && o->mock_fabric) {
        int fd = open(o->mock_fabric, O_RDWR | O_CREAT | O_CLOEXEC, 0644);
        if (fd < 0) {
            harnessd_log("hal_mock: cannot open %s: %s\n", o->mock_fabric, strerror(errno));
            return -1;
        }
        off_t cur = lseek(fd, 0, SEEK_END);
        if (cur < (off_t)total && ftruncate(fd, (off_t)total) != 0) {
            harnessd_log("hal_mock: ftruncate %s: %s\n", o->mock_fabric, strerror(errno));
            close(fd);
            return -1;
        }
        void *p = mmap(0, total, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
        close(fd);
        if (p == MAP_FAILED) {
            harnessd_log("hal_mock: mmap %s: %s\n", o->mock_fabric, strerror(errno));
            return -1;
        }
        s_store = (uint8_t *)p;
        s_hdr = (mock_hdr_t *)p;
        fresh = !(s_hdr->magic == MOCK_MAGIC && s_hdr->version == MOCK_VERSION &&
                  s_hdr->nslots == s_nslots);
    } else {
        s_store = (uint8_t *)calloc(1, total);
        if (!s_store) {
            return -1;
        }
        s_hdr = (mock_hdr_t *)s_store;
    }
    if (fresh) {
        /* Header only: a new file is already zero, and a test may have written
         * register pages (a stage0 block, say) BEFORE this process first opened
         * it — those must survive, exactly as fabric state outlives a process. */
        hdr_init();
    }
    mmcm_seed();
    harnessd_log("hal_mock: behavioural fabric, %u blocks, store %s%s\n", s_nslots,
                 (o && o->mock_fabric) ? o->mock_fabric : "(RAM)",
                 fresh ? " (fresh)" : " (reused: a respawn sees the same fabric)");
    return 0;
}

/* slot index for [base, base+4), or -1 */
static int slot_of(uintptr_t phys, unsigned *slot_off)
{
    for (unsigned i = 0; i < s_nslots; i++) {
        if (phys >= s_blocks[i].base && phys - s_blocks[i].base + 4u <= s_blocks[i].size) {
            *slot_off = (unsigned)(phys - s_blocks[i].base);
            return (int)i;
        }
    }
    return -1;
}

static volatile uint32_t *word(uintptr_t base, uintptr_t off)
{
    unsigned so = 0;
    int i = slot_of(base + off, &so);
    if (i < 0 || !s_store) {
        fprintf(stderr, "FATAL hal_mock: no block maps 0x%08" PRIxPTR " (+0x%" PRIxPTR
                ") -- outside HARNESSD_CONTRACT.md §3; the board's DTS would not "
                "map it either\n", base, off);
        fflush(0);
        abort();
    }
    return (volatile uint32_t *)(s_store + MOCK_HDR_BYTES + (size_t)i * MOCK_SLOT_BYTES + so);
}

static uint32_t plain(uintptr_t base, uintptr_t off) { return *word(base, off); }

/* A blank MMCM_DRP page (a new fabric file) gets the IP's post-reset register
 * file, and an unseeded model MMCM the bitstream's 50 MHz. A page a test (or an
 * earlier process) already wrote is left alone: fabric state outlives processes. */
static void mmcm_seed(void)
{
    if (plain(MPS3_MMCM_DRP_BASE, MMCM_DRP_CFG_REG0) == 0u &&
        plain(MPS3_MMCM_DRP_BASE, MMCM_DRP_CFG_REG2) == 0u) {
        *word(MPS3_MMCM_DRP_BASE, MMCM_DRP_CFG_REG0) = MOCK_MMCM_CFG0_DEFAULT;
        *word(MPS3_MMCM_DRP_BASE, MMCM_DRP_CFG_REG2) = MOCK_MMCM_CFG2_DEFAULT;
    }
    if (s_hdr->mmcm_khz == 0u) {
        s_hdr->mmcm_khz = MOCK_MMCM_KHZ_DEFAULT;
    }
}

/* LOAD: the model MMCM takes the register file (SADDR = bit 1) or the IP's
 * static configuration. Integer M/D/O only -- the fractions are always 0 here. */
static void mmcm_load(uint32_t val)
{
    s_hdr->mmcm_loads++;
    if (!(val & MMCM_DRP_LOAD_SEN)) {
        s_hdr->mmcm_khz = MOCK_MMCM_KHZ_DEFAULT;
        return;
    }
    uint32_t r0 = plain(MPS3_MMCM_DRP_BASE, MMCM_DRP_CFG_REG0);
    uint32_t r2 = plain(MPS3_MMCM_DRP_BASE, MMCM_DRP_CFG_REG2);
    uint32_t d = r0 & 0xFFu, m = (r0 >> 8) & 0xFFu, o = r2 & 0xFFu;
    s_hdr->mmcm_khz = (d && o) ? (50000u * m) / (d * o) : 0u;
}

static int q_push(mock_q_t *q, uint8_t b)
{
    if (q->head - q->tail >= MOCK_UART_Q) {
        return 0;
    }
    q->q[q->head % MOCK_UART_Q] = b;
    q->head++;
    return 1;
}

static uint32_t q_pop(mock_q_t *q)
{
    if (q->head == q->tail) {
        return 0u;
    }
    uint8_t b = q->q[q->tail % MOCK_UART_Q];
    q->tail++;
    return UARTBR_VALID | b;
}

static int rp_connected(void)
{
    uint32_t dec = plain(MPS3_DFXCTL_BASE, DFXCTL_DECOUPLE) & DFXCTL_DECOUPLE_EN;
    uint32_t rst = plain(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL) & CLKRST_RESET_CTRL_RP_RESETN;
    return !dec && rst;
}

static uint32_t bswap32(uint32_t v)
{
    return (v >> 24) | ((v >> 8) & 0xFF00u) | ((v << 8) & 0xFF0000u) | (v << 24);
}

uint32_t hal_backend_read32(uintptr_t base, uintptr_t off)
{
    if (base == MPS3_CLKRST_BASE && off == CLKRST_STATUS) {
        return CLKRST_STATUS_MMCM_LOCKED | CLKRST_STATUS_DUT_CLK_ALIVE;
    }
    if (base == MPS3_DFXCTL_BASE) {
        if (off == DFXCTL_STATUS) {
            uint32_t st = 0u;
            if (plain(MPS3_DFXCTL_BASE, DFXCTL_DECOUPLE) & DFXCTL_DECOUPLE_EN) {
                st |= DFXCTL_STATUS_DECOUPLED;
            }
            if (!(plain(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL) & CLKRST_RESET_CTRL_RP_RESETN)) {
                st |= DFXCTL_STATUS_RP_IN_RESET;
            }
            return st;
        }
        if (off == DFXCTL_RM_ID) {
            return rp_connected() ? s_hdr->rm_loaded : 0u;
        }
        if (off == DFXCTL_RM_STATUS) {
            return rp_connected() ? DFXCTL_RM_STATUS_RM_ID_VALID : 0u;
        }
    }
    if (base == MPS3_HWICAP_BASE) {
        if (off == HWICAP_WFV) return 1024u;
        if (off == HWICAP_CR)  return 0u;
        if (off == HWICAP_SR)  return HWICAP_SR_DONE | HWICAP_SR_EOS;
    }
    if (base == MPS3_JTAGBB_BASE && off == JTAGBB_SAMPLE) {
        return fake_tap_tdo(&s_hdr->tap) ? JTAGBB_SAMPLE_TDO : 0u;
    }
    if (base == MPS3_DBGBR_BASE && off == DBGBR_CTRL) {
        return 0u;   /* GO self-clears: the shift below completed at the write */
    }
    if (base == MPS3_UARTBR_BASE) {
        if (off == UARTBR_U0_TXRX) return q_pop(&s_hdr->u0);
        if (off == UARTBR_U1_TXRX) return q_pop(&s_hdr->u1);
        if (off == UARTBR_SWO_RX)  return 0u;
        if (off == UARTBR_FIFO_STATUS) {
            uint32_t st = UARTBR_FIFO_STATUS_SWO_RX_EMPTY;
            if (s_hdr->u0.head == s_hdr->u0.tail) st |= UARTBR_FIFO_STATUS_U0_RX_EMPTY;
            if (s_hdr->u1.head == s_hdr->u1.tail) st |= UARTBR_FIFO_STATUS_U1_RX_EMPTY;
            return st;
        }
    }
    if (base == MPS3_USRACC_BASE) {
        if (off == USRACC_MAGIC)  return USRACC_MAGIC_VALUE;
        if (off == USRACC_STATUS) return s_hdr->usr_access_valid ? USRACC_STATUS_VALID : 0u;
        if (off == USRACC_VALUE)  return s_hdr->usr_access_valid ? s_hdr->usr_access : 0u;
    }
    if (base == MPS3_WDOG_BASE && off == WDOG_TWCSR1) {
        return 0u;   /* EWDT2 is WRITE-ONLY on the AXI Timebase WDT: it reads 0 on
                      * silicon (the stored value still drives this model's enables) */
    }
    if (base == MPS3_WDOG_BASE && off == WDOG_TBR) {
        /* 100 MHz off the monotonic clock, plus one per read so two reads can
         * never compare equal inside a microsecond (the `reboot` verb's
         * presence probe reads it back to back). */
        static uint32_t bump;
        if (s_wdog_absent) {
            return 0u;   /* frozen: `reboot` declines "no watchdog", as ctrl_echo does */
        }
        return (uint32_t)(harnessd_now_us64() * 100u) + ++bump;
    }
    return plain(base, off);
}

void hal_backend_write32(uintptr_t base, uintptr_t off, uint32_t val)
{
    if (base == MPS3_HWICAP_BASE && off == HWICAP_WF) {
        s_hdr->icap_words++;
        if (s_hdr->icap_marker_state == 1u) {
            s_hdr->rm_loaded = s_hdr->icap_marker_swapped ? bswap32(val) : val;
            s_hdr->icap_marker_state = 0u;
        } else if (val == MOCK_ICAP_MARKER || bswap32(val) == MOCK_ICAP_MARKER) {
            s_hdr->icap_marker_swapped = (val != MOCK_ICAP_MARKER);
            s_hdr->icap_marker_state = 1u;
        }
        return;
    }
    if (base == MPS3_JTAGBB_BASE && off == JTAGBB_DRIVE) {
        uint32_t prev = s_hdr->jtag_drive_prev;
        *word(base, off) = val & 7u;
        if (!(prev & JTAGBB_DRIVE_TCK) && (val & JTAGBB_DRIVE_TCK)) {
            fake_tap_tick(&s_hdr->tap, (val & JTAGBB_DRIVE_TMS) ? 1u : 0u,
                          (val & JTAGBB_DRIVE_TDI) ? 1u : 0u);
        }
        s_hdr->jtag_drive_prev = val & 7u;
        return;
    }
    if (base == MPS3_DBGBR_BASE && off == DBGBR_CTRL) {
        if (val & DBGBR_CTRL_GO) {
            uint32_t len = plain(base, DBGBR_LENGTH);
            uint32_t tms = plain(base, DBGBR_TMS);
            uint32_t tdi = plain(base, DBGBR_TDI);
            uint32_t tdo = 0u;
            if (len > 32u) {
                len = 32u;
            }
            for (uint32_t i = 0; i < len; i++) {
                tdo |= (fake_tap_tdo(&s_hdr->tap) & 1u) << i;
                fake_tap_tick(&s_hdr->tap, (tms >> i) & 1u, (tdi >> i) & 1u);
            }
            *word(base, DBGBR_TDO) = tdo;
        }
        return;
    }
    if (base == MPS3_UARTBR_BASE) {
        if (off == UARTBR_U0_TXRX) { (void)q_push(&s_hdr->u0, (uint8_t)val); return; }
        if (off == UARTBR_U1_TXRX) { (void)q_push(&s_hdr->u1, (uint8_t)val); return; }
    }
    if (base == MPS3_WDOG_BASE) {
        if (off == WDOG_TWCSR0) {
            uint32_t cur = plain(base, off);
            uint32_t w1c = val & (WDOG_TWCSR0_WDS | WDOG_TWCSR0_WRS);
            uint32_t nxt = (cur & ~w1c & ~WDOG_TWCSR0_EWDT1) | (val & WDOG_TWCSR0_EWDT1);
            if ((val & WDOG_TWCSR0_WDS) && (val & WDOG_TWCSR0_EWDT1)) {
                s_hdr->wdog_kicks++;
            }
            if ((cur & WDOG_TWCSR0_EWDT1) && !(val & WDOG_TWCSR0_EWDT1)) {
                s_hdr->wdog_disables++;
            }
            *word(base, off) = nxt;
        } else if (off == WDOG_TWCSR1) {
            uint32_t cur = plain(base, off);
            if ((cur & WDOG_TWCSR1_EWDT2) && !(val & WDOG_TWCSR1_EWDT2)) {
                s_hdr->wdog_disables++;
            }
            *word(base, off) = val & WDOG_TWCSR1_EWDT2;
        } else {
            *word(base, off) = val;
        }
        if ((plain(base, WDOG_TWCSR0) & WDOG_TWCSR0_EWDT1) &&
            (plain(base, WDOG_TWCSR1) & WDOG_TWCSR1_EWDT2) && off != WDOG_TBR &&
            !((val & WDOG_TWCSR0_WDS) && off == WDOG_TWCSR0)) {
            s_hdr->wdog_enables++;
        }
        return;
    }
    if (base == MPS3_USRACC_BASE) {
        return;   /* read-only block */
    }
    if (base == MPS3_CLCDKVM_BASE && off == CLCDKVM_EVENT) {
        *word(base, off) = plain(base, off) & ~val;   /* W1C */
        return;
    }
    if (base == MPS3_MMCM_DRP_BASE && off == MMCM_DRP_LOAD && (val & MMCM_DRP_LOAD_LOAD)) {
        mmcm_load(val);
    }
    *word(base, off) = val;
}

volatile uint32_t *hal_backend_window(uintptr_t phys, size_t len)
{
    unsigned so = 0;
    int i = slot_of(phys, &so);
    if (i < 0 || !s_store || so + len > s_blocks[i].size) {
        return 0;
    }
    return (volatile uint32_t *)(s_store + MOCK_HDR_BYTES + (size_t)i * MOCK_SLOT_BYTES + so);
}

/* ---- scenario seeding (main_linux.c) -------------------------------------- */
void hal_mock_seed_usr_access(uint32_t v, int valid)
{
    if (s_hdr) {
        s_hdr->usr_access = v;
        s_hdr->usr_access_valid = valid ? 1u : 0u;
    }
}

void hal_mock_poke(uintptr_t base, uintptr_t off, uint32_t val)
{
    *word(base, off) = val;
}
