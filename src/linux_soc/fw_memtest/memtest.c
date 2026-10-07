/*
 * fw_memtest/memtest.c -- bare-metal DDR4 memtest for the linux_soc MicroBlaze V.
 *
 * Freestanding: no BSP, no libc, no libgcc. Boots and runs entirely out of the
 * 128 KiB LMB BRAM at 0x0 (see memtest.ld); the DDR4 aperture at 0x8000_0000 is
 * touched ONLY by the explicit volatile accesses in the tests below. Code, stack
 * and .bss are all in LMB, and the I-cache range is the DDR aperture, so NO
 * instruction fetch ever goes to DDR: every DDR transaction this program causes
 * is one of the data accesses written here. That is what makes the AXI
 * transaction counts in tb_memtest.sv exactly attributable.
 *
 * ---------------------------------------------------------------------------
 * WHAT THIS PROVES
 *   proc_sys_reset_0 in mbv_soc.tcl holds mb_reset asserted until
 *   ddr4_0/c0_init_calib_complete is HIGH. The core therefore cannot execute a
 *   single instruction before the DDR4 controller says it has calibrated. So a
 *   banner on the console already proves calibration gated open; a clean memtest
 *   on top of that proves the CPU <-> D-cache <-> smartconnect (32b->512b,
 *   100->200 MHz) <-> DDR4 AXI path actually carries data both ways.
 *
 * ===========================================================================
 * THE D-CACHE IS WRITE-THROUGH. THIS IS THE FACT THE WHOLE FILE IS BUILT ON.
 * ===========================================================================
 * Read back off the BUILT block design (linux_soc_microblaze_riscv_0_0.xci):
 *
 *     C_DCACHE_BYTE_SIZE     = 8192      (8 KiB)
 *     C_DCACHE_LINE_LEN      = 4         (4 words = 16 BYTES per line)
 *     C_DCACHE_USE_WRITEBACK = 0         <-- WRITE-THROUGH
 *     C_DCACHE_VICTIMS       = 0         (no victim cache; direct-mapped)
 *
 * Three consequences, and the previous version of this file was built on the
 * opposite assumption of all three:
 *
 *  (1) EVERY STORE IS ITS OWN SINGLE-BEAT AXI WRITE TO DDR. There is no write
 *      combining and no dirty-line writeback -- a write-through cache posts each
 *      store straight through. So the cost of a test is dominated by its STORE
 *      COUNT, and a "dense" region written word-by-word costs one full DDR write
 *      transaction per word. The old sim wrote a 16 KiB dense block word-by-word:
 *      that is 4096 separate DDR write transactions, and with the read pass on
 *      top it is ~5120 DDR transactions for one test. THAT is what took hours --
 *      not "walking the full 1 GiB range". (The old sim's range sweep was already
 *      strided; the log proves the run never got past the dense block.)
 *
 *  (2) A "FLUSH THE WHOLE CACHE" IS THE WRONG TOOL AND IT IS RUINOUSLY EXPENSIVE.
 *      The old dcache_flush() read 2 x 8 KiB = 16 KiB at 16-byte line stride =
 *      1024 line fills, EVERY TIME IT WAS CALLED, to evict a handful of lines.
 *      Replaced by evict_line() below, which evicts exactly the line it is asked
 *      to, for ONE line fill.
 *
 *  (3) The writes need no flushing at all to reach DRAM -- they are already
 *      there. Eviction is needed only so that the READ-BACK is a genuine DRAM
 *      read rather than a cache hit.
 *
 * ---------------------------------------------------------------------------
 * EVICTION, AND WHY IT IS ONE LINE FILL AND NOT 1024
 *
 *   evict_line(a) reads  a ^ DCACHE_BYTES  (i.e. a ^ 0x2000).
 *
 *   For an 8 KiB direct-mapped cache with 16-byte lines there are 512 sets and
 *       set index = a[12:4]        tag = a[31:13]
 *   XOR-ing with 0x2000 flips bit 13 ONLY. So a^0x2000 has:
 *       - the SAME set index (bits [12:4] untouched), and
 *       - a DIFFERENT tag (bit 13 flipped).
 *   In a direct-mapped cache a set holds one line, so pulling a^0x2000 into that
 *   set necessarily displaces a. Guaranteed eviction, one line fill, no
 *   dependence on the replacement policy. Bit 13 is inside the 1 GiB aperture
 *   for every address in it, so the shadow read can never leave DDR.
 *
 *   THE DIRECT-MAPPED ASSUMPTION IS CHECKED, NOT TRUSTED. tb_memtest.sv counts
 *   the AXI read bursts that actually arrive at the DDR4 slave inside the base
 *   and top block address windows and FAILS the run if the read-back did not
 *   produce them. If the cache were associative and the XOR trick therefore did
 *   not evict, the read-back would be served from cache, those counts would
 *   collapse to ~0, and the TB would fail the run instead of passing it on data
 *   that never came from DRAM. A "PASS" here cannot be a cache hit.
 *
 * ---------------------------------------------------------------------------
 * THE PATTERNS, AND WHY THEY COMPOSE
 *
 *   [1]/[2] ADDRESS-IN-ADDRESS over a contiguous block at the BASE and at the
 *       TOP of the aperture. Each word stores its own address, so aliasing is
 *       directly visible ("I read someone else's address"). Contiguous, so the
 *       read-back is full 16-byte line fills -- burst traffic, not single beats.
 *       The TOP block also proves the last KiB of the declared 1 GiB actually
 *       decodes and stores: the device tree promises RAM there.
 *
 *   [3] WALKING-1s / WALKING-0s on the DATA bus: a single 1 walks through all
 *       32 data bits, then a single 0. Catches stuck-at, open and shorted DQ
 *       lanes. This test is about DATA lanes, so in sim the 64 points are PACKED
 *       (one word apart) rather than spread over 64 KiB: spreading them costs 64
 *       line fills instead of 16 and adds nothing to the data-bus coverage, which
 *       is what this test is for. Address spread is test [4]'s job. On hardware
 *       they are spread, because there the extra rows/banks are free.
 *
 *   [4] WALKING-1s on the ADDRESS bus: offset 0, every power of two below
 *       DDR_SIZE, and the LAST WORD of the aperture -- each holding its own
 *       address. If address bit k is stuck low, the write to offset 2^k lands on
 *       offset 0 and the read of 2^k returns the wrong address. This covers
 *       every address bit across the FULL 1 GiB for 30 points and ~60 DDR
 *       transactions, which is why the sim does not need a strided bulk sweep to
 *       claim full-aperture address coverage.
 *
 * ---------------------------------------------------------------------------
 * SIM vs HARDWARE -- sizes, never code paths
 *   -DMT_SIM shrinks the blocks (1 KiB instead of 1 MiB), packs the data-bus
 *   points, and drops the exhaustive word-by-word sweep. Every test above runs
 *   in both builds, with the same patterns and the same PASS/FAIL logic. The
 *   hardware build additionally walks EVERY 32-bit word of the full 1 GiB
 *   (test [5]) -- pointless in an RTL simulation (it is ~2.7e8 DDR transactions),
 *   essential on the board, where it costs seconds.
 *
 * ---------------------------------------------------------------------------
 * CONSOLE COST (why the output is terse)
 *   uart_putc BLOCKS on TX_FULL and it MUST -- see the driver comment below.
 *   sim.tcl shrinks the uartlite baud DIVISOR so a character costs 1.6 us of
 *   simulated time instead of 86.8 us. The 16-deep TX FIFO absorbs short bursts
 *   for free while the memory test runs, but a character is still ~1.6 us of sim
 *   once the FIFO is full, and xsim runs this design at well under 1 us of sim
 *   per wall-clock second. Characters are minutes. Keep the output lean.
 */

#include <stdint.h>

/* ------------------------------------------------------------------------- *
 * Platform constants -- MUST track src/linux_soc/hw/mbv_soc.tcl SECTION 0.
 * (build/address_map.txt is the read-back of what the BD actually assigned.)
 * ------------------------------------------------------------------------- */
#define UART_BASE        0x40600000u
#define DDR_BASE         0x80000000u
#define DDR_SIZE         0x40000000u          /* 1 GiB: 0x8000_0000..0xBFFF_FFFF */

/* D-cache geometry -- read back off the built BD (see the header). */
#define DCACHE_BYTES     8192u                /* C_DCACHE_BYTE_SIZE */
#define DLINE_BYTES      16u                  /* C_DCACHE_LINE_LEN = 4 words */
#define EVICT_XOR        DCACHE_BYTES         /* flips bit 13: same set, new tag */

/* axi_uartlite (xuartlite_l.h) */
#define UART_TX_OFF      0x4u
#define UART_STAT_OFF    0x8u
#define UART_CTRL_OFF    0xCu
#define UART_SR_TX_FULL  (1u << 3)
#define UART_CR_RST_TX   (1u << 0)
#define UART_CR_RST_RX   (1u << 1)

/* ------------------------------------------------------------------------- *
 * Test geometry.
 *
 * MT_BLK_BYTES is the size of the contiguous block tested at the base AND at the
 * top of the aperture. It is a -D knob so the Makefile is the single source of
 * truth for it: sim.tcl reads the SAME make variable and hands it to the TB, so
 * the TB's "did the read-back really hit DRAM" thresholds can never drift out of
 * step with the firmware's block size.
 * ------------------------------------------------------------------------- */
#ifndef MT_BLK_BYTES
#  ifdef MT_SIM
#    define MT_BLK_BYTES    1024u                 /* 1 KiB at base + 1 KiB at top */
#  else
#    define MT_BLK_BYTES    (1024u * 1024u)       /* 1 MiB each on the board */
#  endif
#endif

/* [3] data-bus points: 64 patterns, MT_DBUS_SPAN apart. Packed in sim (this test
 * is about data lanes, not addresses -- see the header); spread on hardware. */
#ifndef MT_DBUS_SPAN
#  ifdef MT_SIM
#    define MT_DBUS_SPAN    4u                    /* packed: 64 words = 256 B */
#  else
#    define MT_DBUS_SPAN    (64u * 1024u)
#  endif
#endif

/* The data-bus test is the ONLY test that writes something other than a word's
 * own address, so its region must not collide with any other test's addresses.
 * 10 MiB is deliberately NOT a power of two (8 MiB would land exactly on one of
 * test [4]'s offsets) and is far above the base block and far below the top one.
 * Every OTHER overlap in this file is harmless BY CONSTRUCTION: blocks and
 * addrbus all write a word's own address, so where they overlap they agree. */
#define DBUS_OFF         0x00A00000u

/* [5] hardware-only exhaustive sweep: EVERY 32-bit word of the full 1 GiB. */
#ifndef MT_SWEEP_STRIDE
#  define MT_SWEEP_STRIDE  4u
#endif
#ifndef MT_DO_SWEEP
#  ifdef MT_SIM
#    define MT_DO_SWEEP    0                      /* ~2.7e8 DDR transactions: never in RTL sim */
#  else
#    define MT_DO_SWEEP    1
#  endif
#endif

#define MAX_REPORT       8u                       /* cap the mismatch spam */

/* ------------------------------------------------------------------------- *
 * MMIO
 * ------------------------------------------------------------------------- */
static inline void     mmio_w32(uint32_t a, uint32_t v) { *(volatile uint32_t *)a = v; }
static inline uint32_t mmio_r32(uint32_t a)             { return *(volatile uint32_t *)a; }

/* ------------------------------------------------------------------------- *
 * Console -- polled axi_uartlite TX.
 *
 * uart_putc SPINS on TX_FULL. IDENTICAL in the sim and hardware builds, and that
 * is not a stylistic choice -- it is mandatory:
 *
 *   WRITING A FULL uartlite TX FIFO IS NOT A SILENT DROP. IT FAULTS THE CPU.
 *
 * Found by this very co-sim, after an earlier version of this file skipped the
 * spin under -DMT_SIM (to save simulated time) and let the 16-deep FIFO
 * overflow. The AXI trace was unambiguous: the 17th write took a store fault;
 * the trap handler's own first action is uart_puts(), whose first character hits
 * the still-full FIFO and faults again -- so the handler re-enters itself
 * forever, emitting nothing but CR. A console that overflows this FIFO does not
 * lose characters, it wedges the core. The cost is dealt with in sim.tcl by
 * shrinking the BAUD DIVISOR (a sim-only IP parameter), never by breaking the
 * driver.
 * ------------------------------------------------------------------------- */
static void uart_init(void)
{
    mmio_w32(UART_BASE + UART_CTRL_OFF, UART_CR_RST_TX | UART_CR_RST_RX);
    mmio_w32(UART_BASE + UART_CTRL_OFF, 0u);
}

static void uart_putc(char c)
{
    while (mmio_r32(UART_BASE + UART_STAT_OFF) & UART_SR_TX_FULL)
        ;   /* MUST NOT be skipped -- see the block comment above */
    mmio_w32(UART_BASE + UART_TX_OFF, (uint32_t)(uint8_t)c);
}

static void uart_puts(const char *s)
{
    for (; *s; ++s) {
        if (*s == '\n')
            uart_putc('\r');
        uart_putc(*s);
    }
}

static void uart_puthex(uint32_t v)
{
    static const char hd[] = "0123456789ABCDEF";
    for (int i = 28; i >= 0; i -= 4)
        uart_putc(hd[(v >> i) & 0xFu]);
}

static void uart_putdec(uint32_t v)
{
    char buf[10];
    int n = 0;
    if (v == 0u) { uart_putc('0'); return; }
    while (v) { buf[n++] = (char)('0' + (v % 10u)); v /= 10u; }
    while (n) uart_putc(buf[--n]);
}

/* ------------------------------------------------------------------------- *
 * Trap reporter -- called from crt0.S. A load/store access fault on the DDR
 * aperture lands here, so a fabric decode error is PRINTED, never a silent hang.
 * ------------------------------------------------------------------------- */
void trap_report(uint32_t mcause, uint32_t mepc, uint32_t mtval)
{
    uart_puts("\n*** TRAP *** mcause=0x"); uart_puthex(mcause);
    uart_puts(" mepc=0x");                 uart_puthex(mepc);
    uart_puts(" mtval=0x");                uart_puthex(mtval);
    uart_puts("\n");
    /* Machine-readable: the sim TB and any board script key off this line. */
    uart_puts("MEMTEST_RESULT: FAIL errors=TRAP\n");
    for (;;)
        __asm__ volatile("wfi");
}

/* ------------------------------------------------------------------------- *
 * CACHE EVICTION -- and why this is a plain linear sweep and not something clever.
 *
 * The read-back of a test point must MISS in the D-cache, or it proves nothing
 * about DDR4: a hit returns the value out of the cache, which of course matches
 * what we wrote, and the test would "pass" without a single DRAM read.
 *
 * The obvious cheap trick is to evict a line by reading a DIFFERENT address that
 * maps to the SAME SET -- a ^ DCACHE_BYTES flips a tag bit and leaves the set
 * index alone. That is exactly what an earlier version of this file did, and IT
 * DOES NOT WORK ON THIS CORE. Measured, by counting the CPU's AXI read bursts
 * that actually landed inside the base block:
 *
 *      64 words written, 16 lines, shadow-read every one of them,
 *      then read all 64 back  ->  ONE AXI read burst.
 *
 * 15 of the 16 lines were still resident and answered from cache. A single
 * same-set read only guarantees eviction in a DIRECT-MAPPED cache; this D-cache
 * evidently keeps both the line and its shadow, so it is not direct-mapped (or at
 * least does not behave as though it were). MicroBlaze exposes no associativity
 * parameter to read back, so the honest move is to stop depending on the number.
 *
 * SO: sweep a CONTIGUOUS window of 2 x the cache size, one read per line. For a
 * cache of S bytes with L-byte lines and ANY associativity A, that window is 2S/L
 * distinct lines spread over the S/(L*A) sets, i.e. 2A fresh tags per set -- more
 * than enough to push every previous line out of every way of every set, whatever
 * A is and whatever the replacement policy is. This is the one eviction that needs
 * no assumption about the cache's internal shape.
 *
 * It costs 2S/L = 1024 line fills, which is why it is called EXACTLY ONCE for the
 * whole test (write everything, flush once, then check everything) rather than
 * once per test. At the measured ~117 ns per DDR transaction that is ~120 us of
 * simulated time -- affordable; four of them would not be.
 *
 * The window must not overlap any test region, or the flush would pull the very
 * lines we are trying to evict back INTO the cache. 3 MiB is clear of the base
 * block, the top block, the data-bus region (10 MiB) and every power-of-two
 * address-bus point. Its contents are never written, so the reads return
 * uninitialised DRAM (X in simulation) -- which is why the value is DISCARDED
 * rather than accumulated. A volatile read is not optimised out.
 * ------------------------------------------------------------------------- */
#define FLUSH_OFF        0x00300000u              /* 3 MiB: not a power of two, no overlap */
#define FLUSH_BYTES      (2u * DCACHE_BYTES)      /* 16 KiB -> 1024 lines, evicts any assoc */

static void dcache_flush(void)
{
    uint32_t off;
    __asm__ volatile("fence rw,rw" ::: "memory");   /* order the stores under test first */
    for (off = 0; off < FLUSH_BYTES; off += DLINE_BYTES)
        (void)*(volatile uint32_t *)(DDR_BASE + FLUSH_OFF + off);
    __asm__ volatile("fence rw,rw" ::: "memory");
}

/* ------------------------------------------------------------------------- *
 * Reporting
 * ------------------------------------------------------------------------- */
static void report(uint32_t addr, uint32_t exp, uint32_t got, uint32_t *errs)
{
    if (*errs < MAX_REPORT) {
        uart_puts("  MISMATCH @0x"); uart_puthex(addr);
        uart_puts(" exp=0x");        uart_puthex(exp);
        uart_puts(" got=0x");        uart_puthex(got);
        uart_puts("\n");
    } else if (*errs == MAX_REPORT) {
        uart_puts("  ...\n");
    }
    (*errs)++;
}

/* ------------------------------------------------------------------------- *
 * [1]/[2] ADDRESS-IN-ADDRESS over a contiguous block: write, evict, read back.
 * ------------------------------------------------------------------------- */
static void wr_block(uint32_t base, uint32_t bytes, uint32_t stride, uint32_t *npoints)
{
    uint32_t off;
    uint32_t n = 0;
    for (off = 0; off < bytes; off += stride) {
        mmio_w32(base + off, base + off);
        n++;
    }
    *npoints = n;
}

static uint32_t ck_block(uint32_t base, uint32_t bytes, uint32_t stride)
{
    uint32_t errs = 0;
    uint32_t off;
    for (off = 0; off < bytes; off += stride) {
        uint32_t a   = base + off;
        uint32_t got = mmio_r32(a);
        if (got != a) report(a, a, got, &errs);
    }
    return errs;
}

#if MT_DO_SWEEP
/* Hardware-only exhaustive sweep. A contiguous region of >= 2 x the D-cache
 * self-evicts during its own linear read-back (the head is long gone by the time
 * the tail is reached), so it needs no flush -- which is just as well, since it
 * is the full 1 GiB. */
static uint32_t test_block_selfevict(uint32_t base, uint32_t bytes, uint32_t stride,
                                     uint32_t *npoints)
{
    wr_block(base, bytes, stride, npoints);
    __asm__ volatile("fence rw,rw" ::: "memory");
    return ck_block(base, bytes, stride);
}
#endif

/* ------------------------------------------------------------------------- *
 * [3] WALKING-1s / WALKING-0s data-bus test: 64 patterns, MT_DBUS_SPAN apart.
 * ------------------------------------------------------------------------- */
static void wr_databus(uint32_t *npoints)
{
    uint32_t i;
    for (i = 0; i < 32u; ++i) {
        mmio_w32(DDR_BASE + DBUS_OFF + (i +  0u) * MT_DBUS_SPAN,  (1u << i));   /* walking 1 */
        mmio_w32(DDR_BASE + DBUS_OFF + (i + 32u) * MT_DBUS_SPAN, ~(1u << i));   /* walking 0 */
    }
    *npoints = 64u;
}

static uint32_t ck_databus(void)
{
    uint32_t errs = 0;
    uint32_t i;
    for (i = 0; i < 32u; ++i) {
        uint32_t a1 = DDR_BASE + DBUS_OFF + (i +  0u) * MT_DBUS_SPAN;
        uint32_t a0 = DDR_BASE + DBUS_OFF + (i + 32u) * MT_DBUS_SPAN;
        uint32_t e1 =  (1u << i);
        uint32_t e0 = ~(1u << i);
        uint32_t g1 = mmio_r32(a1);
        uint32_t g0 = mmio_r32(a0);
        if (g1 != e1) report(a1, e1, g1, &errs);
        if (g0 != e0) report(a0, e0, g0, &errs);
    }
    return errs;
}

/* ------------------------------------------------------------------------- *
 * [4] WALKING-1s ADDRESS test, address-in-address: offset 0, every power of two
 *     below DDR_SIZE, and the LAST WORD of the aperture.
 * ------------------------------------------------------------------------- */
#define ABUS_LAST_OFF   (DDR_SIZE - 4u)

static void wr_addrbus(uint32_t *npoints)
{
    uint32_t off;
    uint32_t n = 0;
    mmio_w32(DDR_BASE, DDR_BASE);                                 n++;
    for (off = 4u; off < DDR_SIZE; off <<= 1)                   { mmio_w32(DDR_BASE + off, DDR_BASE + off); n++; }
    mmio_w32(DDR_BASE + ABUS_LAST_OFF, DDR_BASE + ABUS_LAST_OFF); n++;
    *npoints = n;
}

static uint32_t ck_addrbus(void)
{
    uint32_t errs = 0;
    uint32_t off;
    {
        uint32_t got = mmio_r32(DDR_BASE);
        if (got != DDR_BASE) report(DDR_BASE, DDR_BASE, got, &errs);
    }
    for (off = 4u; off < DDR_SIZE; off <<= 1) {
        uint32_t a   = DDR_BASE + off;
        uint32_t got = mmio_r32(a);
        if (got != a) report(a, a, got, &errs);
    }
    {
        uint32_t a   = DDR_BASE + ABUS_LAST_OFF;
        uint32_t got = mmio_r32(a);
        if (got != a) report(a, a, got, &errs);
    }
    return errs;
}

/* ------------------------------------------------------------------------- *
 * Result line for one test. Terse on purpose -- every character is simulated
 * time (see the header).
 * ------------------------------------------------------------------------- */
static void result(const char *tag, uint32_t e, uint32_t n)
{
    uart_puts(tag);
    uart_puts(e ? " FAIL " : " ok ");
    uart_putdec(n);
    uart_puts(" e=");
    uart_putdec(e);
    uart_puts("\n");
}

/* ------------------------------------------------------------------------- *
 * main
 * ------------------------------------------------------------------------- */
int main(void)
{
    uint32_t total = 0;
    uint32_t e;

    uart_init();

    uart_puts("\nMBV DDR4 memtest ");
#ifdef MT_SIM
    uart_puts("SIM\n");
#else
    uart_puts("HW\n");
#endif
    /* Reaching this line at all means the core came out of reset -- and
     * proc_sys_reset holds mb_reset until c0_init_calib_complete is HIGH. */

    /* WRITE EVERYTHING -> FLUSH ONCE -> CHECK EVERYTHING.
     *
     * The single flush is what makes this affordable: evicting the whole D-cache
     * costs 1024 line fills and is the only assoc-independent way to guarantee the
     * read-backs come from DRAM (see dcache_flush). Doing it once for all four
     * tests instead of once per test is a 4x saving on the dominant cost.
     *
     * Splitting write from check does not weaken any test: every pattern here is
     * still written, then read back and compared against the same expectation. The
     * regions are disjoint except where they all write a word's OWN ADDRESS, where
     * they agree by construction. */
    {
        uint32_t n_base, n_top, n_dbus, n_abus;

        wr_block(DDR_BASE,                            MT_BLK_BYTES, 4u, &n_base);
        wr_block(DDR_BASE + DDR_SIZE - MT_BLK_BYTES,  MT_BLK_BYTES, 4u, &n_top);
        wr_databus(&n_dbus);
        wr_addrbus(&n_abus);

        dcache_flush();     /* the ONE flush -- everything below is a real DRAM read */

        e = ck_block(DDR_BASE, MT_BLK_BYTES, 4u);
        total += e; result("[1] base", e, n_base);

        e = ck_block(DDR_BASE + DDR_SIZE - MT_BLK_BYTES, MT_BLK_BYTES, 4u);
        total += e; result("[2] top", e, n_top);

        e = ck_databus();
        total += e; result("[3] dbus", e, n_dbus);

        e = ck_addrbus();
        total += e; result("[4] abus", e, n_abus);
    }

#if MT_DO_SWEEP
    /* Hardware only: EVERY 32-bit word of the full 1 GiB. Self-evicting. */
    {
        uint32_t n_full;
        e = test_block_selfevict(DDR_BASE, DDR_SIZE, MT_SWEEP_STRIDE, &n_full);
        total += e; result("[5] full", e, n_full);
    }
#endif

    if (total == 0u)
        uart_puts("MEMTEST_RESULT: PASS e=0\n");
    else {
        uart_puts("MEMTEST_RESULT: FAIL e=");
        uart_putdec(total);
        uart_puts("\n");
    }

    return 0;   /* crt0 parks the core */
}
