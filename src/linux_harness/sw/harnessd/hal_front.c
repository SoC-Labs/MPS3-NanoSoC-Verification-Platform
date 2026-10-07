/*
 * hal_front.c — the ONE definition of platform_regs.h's four register accessors
 * for harnessd (both -DMPS3_HAL_UIO and -DMPS3_HAL_MOCK declare them extern),
 * plus the expected-block list. See hal.h for the work counter and the respawn
 * reset shield, which is everything this file adds over a straight backend call.
 */
#include "../../../../firmware/common/platform_regs.h"
#include "../../../../firmware/clcd/clcd.h"   /* clcd_bus_push (the bus seam) */
#include "hal.h"

static uint64_t s_writes;
static int      s_shield;
static uint32_t s_shield_hits;
static hal_tap_fn s_tap;    /* the LCD mirror's CLCD tap (hal.h); NULL = none */
static hal_tap_bytes_fn s_tap_bytes;   /* ... and its bulk form (hal.h)       */
static uint32_t s_push_runs, s_push_bytes, s_push_status_reads;

/* CLCD (0x44AC_0000) and CLCDKVM (0x44AD_0000) differ only in bit 16: ONE mask
 * and compare keeps every other register access (an ICAP stream is 256K words
 * a partial) at its old cost when the tap is installed. */
#define TAP_HIT(base) (s_tap != 0 && ((base) & ~(uintptr_t)0x10000u) == MPS3_CLCD_BASE)

/* The three reset-release bits the shield preserves (1 = RELEASED). */
#define SHIELD_BITS (CLKRST_RESET_CTRL_DUT_RESETN | CLKRST_RESET_CTRL_RP_RESETN | \
                     CLKRST_RESET_CTRL_DBG_RESETN)

uint32_t mps3_reg_read32(uintptr_t base, uintptr_t off)
{
    uint32_t v = hal_backend_read32(base, off);
    if (TAP_HIT(base)) {
        s_tap(base, off, v, 0);
    }
    return v;
}

void mps3_reg_write32(uintptr_t base, uintptr_t off, uint32_t val)
{
    if (s_shield && base == MPS3_CLKRST_BASE && off == CLKRST_RESET_CTRL) {
        /* A respawn must not re-assert a reset the running board has released.
         * Bits that read 1 now stay 1; everything else is written as asked. */
        uint32_t keep = hal_backend_read32(base, off) & SHIELD_BITS;
        if ((val | keep) != val) {
            s_shield_hits++;
            val |= keep;
        }
    }
    s_writes++;
    hal_backend_write32(base, off, val);
    if (TAP_HIT(base)) {
        s_tap(base, off, val, 1);
    }
}

void mps3_reg_set_bits32(uintptr_t base, uintptr_t off, uint32_t mask)
{
    mps3_reg_write32(base, off, mps3_reg_read32(base, off) | mask);
}

void mps3_reg_clr_bits32(uintptr_t base, uintptr_t off, uint32_t mask)
{
    mps3_reg_write32(base, off, mps3_reg_read32(base, off) & ~mask);
}

uint64_t hal_write_count(void)          { return s_writes; }
uint32_t hal_quiet_read32(uintptr_t b, uintptr_t o)             { return hal_backend_read32(b, o); }
void     hal_quiet_write32(uintptr_t b, uintptr_t o, uint32_t v) { hal_backend_write32(b, o, v); }
void     hal_reset_shield(int up)       { s_shield = up ? 1 : 0; }
uint32_t hal_reset_shield_hits(void)    { return s_shield_hits; }
void     hal_set_tap(hal_tap_fn fn)     { s_tap = fn; }
void     hal_set_tap_bytes(hal_tap_bytes_fn fn) { s_tap_bytes = fn; }

void hal_clcd_push_counts(uint32_t *runs, uint32_t *bytes, uint32_t *status_reads)
{
    if (runs)         *runs = s_push_runs;
    if (bytes)        *bytes = s_push_bytes;
    if (status_reads) *status_reads = s_push_status_reads;
}

/* ==========================================================================
 * THE CLCD BUS SEAM, strong (clcd.h clcd_bus_push; lane CLCD-SPEED).
 *
 * clcd.c's weak default pays, PER BYTE: an uncached CLCD_STATUS read, then
 * mps3_reg_write32 -> hal_backend_write32 -> find() -> the store, then the tap
 * call -> lcdm_model_byte(). On the 100 MHz MBV that is ~200 instructions, ~35
 * stack/state stores (the D-cache is WRITE-THROUGH: each one is a DDR write)
 * and one AXI read round trip -- ~5 us a byte, ~0.8 s for a 164 KB screen. Here:
 *
 *   - ONE STATUS read buys (FIFO_DEPTH - STATUS.level) writes. Only this
 *     process fills clcd_0's FIFO and the 8080 engine only drains it, so the
 *     free space can only GROW between that read and our writes: the FIFO can
 *     never overflow, whatever the CPU speed. (fifo_full or a full level: stop,
 *     the caller resumes next pass -- exactly the weak default's contract.)
 *   - the bytes go straight to the mapped window: one store each, no call chain.
 *   - the mirror sees the run AFTER the writes, whole, in order (hal.h THE CLCD
 *     BULK TAP), or byte by byte through the per-access tap if that is all that
 *     is installed. Every byte still goes through the tap: pixel-exact.
 *   - every byte counts as work (s_writes), as the per-byte path did.
 *
 * No window (a block the backend did not map): the weak default's loop through
 * the front, byte for byte -- the same FATAL a missing CLCD window always was.
 * HARNESSD_CLCD_PUSH_PER_BYTE compiles this out (the lane's negative control).
 * ========================================================================== */
#if defined(MPS3_HAS_CLCD) && !defined(HARNESSD_CLCD_PUSH_PER_BYTE)

/* clcd.sv FIFO_DEPTH: shell_bd.tcl keeps the RTL default (docs/CLCD_PANEL_FACTS.md
 * §6); STATUS[15:8] reports the level against it. */
#define CLCD_FIFO_DEPTH 128u

uint32_t clcd_bus_push(const uint8_t *rs, const uint8_t *val, uint32_t n)
{
    volatile uint32_t *w = hal_backend_window(MPS3_CLCD_BASE, CLCD_TIMING + 4u);
    uint32_t done = 0;
    if (!w) {
        for (; done < n; done++) {
            if (mps3_reg_read32(MPS3_CLCD_BASE, CLCD_STATUS) & CLCD_STATUS_FIFO_FULL) {
                break;
            }
            mps3_reg_write32(MPS3_CLCD_BASE, rs[done] ? CLCD_DATA : CLCD_CMD, val[done]);
        }
        return done;
    }
    volatile uint32_t *const cmd = w + CLCD_CMD / 4u;
    volatile uint32_t *const dat = w + CLCD_DATA / 4u;
    s_push_runs++;
    while (done < n) {
        uint32_t st = mps3_reg_read32(MPS3_CLCD_BASE, CLCD_STATUS);
        s_push_status_reads++;
        uint32_t level = (st & CLCD_STATUS_LEVEL_MASK) >> CLCD_STATUS_LEVEL_SHIFT;
        if ((st & CLCD_STATUS_FIFO_FULL) || level >= CLCD_FIFO_DEPTH) {
            break;
        }
        uint32_t k = CLCD_FIFO_DEPTH - level;
        if (k > n - done) {
            k = n - done;
        }
        const uint8_t *r = rs + done;
        const uint8_t *v = val + done;
        for (uint32_t i = 0; i < k; i++) {
            if (r[i]) {
                *dat = v[i];
            } else {
                *cmd = v[i];
            }
        }
        s_writes += k;
        if (s_tap_bytes) {
            s_tap_bytes(r, v, k);
        } else if (s_tap) {
            for (uint32_t i = 0; i < k; i++) {
                s_tap(MPS3_CLCD_BASE, r[i] ? CLCD_DATA : CLCD_CMD, v[i], 1);
            }
        }
        done += k;
    }
    s_push_bytes += done;
    return done;
}
#endif

/* ==========================================================================
 * THE EXPECTED-BLOCK LIST — HARNESSD_CONTRACT.md §3, and what IMAGE's DTS must
 * give a generic-uio node. `required` follows the build's feature flags; a
 * block with required = 0 is still served by the mock (and mapped if the DTS has
 * it) but its absence is not an error. Sizes are the BD's assign_bd_address
 * ranges (platform_regs.h's generated table).
 * ========================================================================== */
#if defined(MPS3_HAS_CLCD)
#define REQ_CLCD 1
#else
#define REQ_CLCD 0
#endif
#if defined(MPS3_HAS_CLCD_KVM)
#define REQ_CLCDKVM 1
#else
#define REQ_CLCDKVM 0
#endif
#if defined(MPS3_HAS_TOUCH)
#define REQ_TOUCH 1
#else
#define REQ_TOUCH 0
#endif
#if defined(MPS3_HAS_DUT_EGRESS)
#define REQ_DUTEGR 1
#else
#define REQ_DUTEGR 0
#endif

static const hal_block_t s_blocks[] = {
    { MPS3_CLKRST_BASE,   0x10000u, "clkrst",    1 },
    { MPS3_DFXCTL_BASE,   0x10000u, "dfxctl",    1 },
    { MPS3_HWICAP_BASE,   0x10000u, "hwicap",    1 },
    { MPS3_VPHY_BASE,     0x01000u, "vphy",      1 },
    { MPS3_GENCHK_BASE,   0x01000u, "genchk",    1 },
    { MPS3_JTAGBB_BASE,   0x10000u, "jtag-bb",   1 },
    { MPS3_DBGBR_BASE,    0x10000u, "dbgbr",     1 },
    { MPS3_UARTBR_BASE,   0x10000u, "uartbr",    1 },
    { MPS3_GPIO_BASE,     0x10000u, "gpio",      1 },
    { MPS3_MMCM_DRP_BASE, 0x10000u, "mmcm-drp",  1 },
    { MPS3_CLCD_BASE,     0x10000u, "clcd",      REQ_CLCD },
    { MPS3_CLCDKVM_BASE,  0x10000u, "clcd-kvm",  REQ_CLCDKVM },
    { MPS3_TOUCH_BASE,    0x10000u, "touch-iic", REQ_TOUCH },
    { MPS3_DUTEGR_BASE,   0x10000u, "dutegr",    REQ_DUTEGR },
    { MPS3_USRACC_BASE,   0x10000u, "usracc",    1 },
    { MPS3_WDOG_BASE,     0x10000u, "wdog",      1 },
    { HARNESSD_LMB_TAIL_BASE, HARNESSD_LMB_TAIL_SIZE, "lmb-tail", 1 },
};

const hal_block_t *hal_expected_blocks(unsigned *n)
{
    if (n) {
        *n = (unsigned)(sizeof(s_blocks) / sizeof(s_blocks[0]));
    }
    return s_blocks;
}
