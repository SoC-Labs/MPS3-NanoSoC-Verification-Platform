/*
 * memtest.c - bare-metal DDR4 bring-up memtest for a MicroBlaze V soft core.
 *
 * Freestanding: no BSP, no libc, no _start (see crt0.S). Only <stdint.h> /
 * <stddef.h> are used, which are freestanding headers.
 *
 * Build: riscv64-unknown-elf-gcc -march=rv32im -mabi=ilp32 -mcmodel=medany \
 *        -mno-relax -ffreestanding -nostartfiles -O2 ...   (see Makefile)
 *
 * Output goes to axi_uartlite by polling its TX FIFO. The three sub-tests are
 * the classic DDR bring-up triangle:
 *   1. walking-ones ADDRESS test - stuck / shorted address lines,
 *   2. DATA-BUS test             - stuck / shorted / open DQ bits,
 *   3. device (pattern) test     - the bulk cell test.
 *
 * CACHE: the D-cache (8 KiB) caches the DDR aperture, so a naive write-then-read
 * would be answered from cache and prove nothing about the DRAM. See
 * dcache_flush() below for how we defeat that on a core with no cache-control
 * CSRs.
 */

#include <stdint.h>
#include <stddef.h>

/* ------------------------------------------------------------------------- *
 *  Platform constants  (MUST track the block design / mbv_soc.tcl)
 * ------------------------------------------------------------------------- */

/* axi_uartlite.  This base MUST match mbv_soc.tcl's assign_bd_address for the
 * uartlite instance; if that changes, change it here. */
#define UART_BASE        0x40600000u

/* xuartlite_l.h register offsets and the STATUS bits we use. */
#define UART_RX_OFF      0x0u   /* Rx FIFO (read)                     */
#define UART_TX_OFF      0x4u   /* Tx FIFO (write)                    */
#define UART_STAT_OFF    0x8u   /* status register (read)            */
#define UART_CTRL_OFF    0xCu   /* control register (write)          */
#define UART_SR_RX_VALID (1u << 0) /* Rx FIFO has data              */
#define UART_SR_TX_EMPTY (1u << 2) /* Tx FIFO empty                 */
#define UART_SR_TX_FULL  (1u << 3) /* Tx FIFO full  <- poll clear   */
#define UART_CR_RST_TX   (1u << 0) /* reset/clear Tx FIFO           */
#define UART_CR_RST_RX   (1u << 1) /* reset/clear Rx FIFO           */

/* DDR4 aperture -- MUST match the address map in mbv_soc.tcl (SECTION 0) and
 * ADDRESS_MAP.md. linux_soc maps 1 GiB at 0x8000_0000 (the spike mapped 2 GiB):
 * rv32 Linux can only use 1 GiB of lowmem, so only 1 GiB is assigned, and an
 * access above 0xBFFF_FFFF now lands in an UNMAPPED hole -- a bus error, which
 * trap_report() would print. Do not raise this without raising MAP_DDR_RANGE. */
#define DDR_BASE         0x80000000u
#define DDR_APERTURE     0x40000000u   /* 1 GiB */

/* Cache geometry (from the CPU config: 8 KiB I-cache + 8 KiB D-cache covering
 * 0x8000_0000..0xFFFF_FFFF). Used only to size the cache-flush thrash window. */
#define DCACHE_BYTES     (8u * 1024u)
/* A contiguous read window >= the D-cache size guarantees that, for any set,
 * the target line's set is refilled with `assoc` fresh tags -> the target is
 * evicted (and, if dirty, written back). 2x for margin. */
#define FLUSH_BYTES      (2u * DCACHE_BYTES)   /* 16 KiB */
/* Scratch window that dcache_flush() thrashes. Chosen NOT to coincide with any
 * power-of-two word offset used by the address test (byte off 0x1800_0000 ->
 * word 0x0600_0000, not a power of two, and no power of two lies in the 16 KiB
 * span), and disjoint from the data/device regions below. Its DRAM contents are
 * irrelevant - we only need the *reads* to cause eviction. */
#define FLUSH_OFF        0x18000000u

/* Sub-test regions inside the aperture (byte offsets from DDR_BASE). */
#define DEV_OFF          0x00000000u          /* device test region start   */
#define DEV_BYTES        (256u * 1024u)       /* 256 KiB  (>> 8 KiB D-cache) */
#define DATABUS_OFF      0x00100000u          /* single-address data-bus test*/

#define SEED             0xA5A5F00Du          /* deterministic xorshift seed */
#define MAX_REPORT       8                    /* cap per-test mismatch spam  */

/* ------------------------------------------------------------------------- *
 *  MMIO helpers
 * ------------------------------------------------------------------------- */

static inline void mmio_w32(uint32_t addr, uint32_t val)
{
    *(volatile uint32_t *)addr = val;
}
static inline uint32_t mmio_r32(uint32_t addr)
{
    return *(volatile uint32_t *)addr;
}

/* ------------------------------------------------------------------------- *
 *  UART (axi_uartlite) - polled, TX only (plus a byte-available helper)
 * ------------------------------------------------------------------------- */

static void uart_init(void)
{
    /* Clear both FIFOs; leave interrupts disabled. */
    mmio_w32(UART_BASE + UART_CTRL_OFF, UART_CR_RST_TX | UART_CR_RST_RX);
    mmio_w32(UART_BASE + UART_CTRL_OFF, 0u);
}

static void uart_putc(char c)
{
    while (mmio_r32(UART_BASE + UART_STAT_OFF) & UART_SR_TX_FULL)
        ; /* spin until the Tx FIFO can take a byte */
    mmio_w32(UART_BASE + UART_TX_OFF, (uint32_t)(uint8_t)c);
}

static void uart_puts(const char *s)
{
    for (; *s; ++s) {
        if (*s == '\n')
            uart_putc('\r');   /* CRLF for dumb terminals */
        uart_putc(*s);
    }
}

/* 32-bit value as 8 hex digits, no "0x" prefix. */
static void uart_puthex(uint32_t v)
{
    static const char hd[] = "0123456789ABCDEF";
    for (int i = 28; i >= 0; i -= 4)
        uart_putc(hd[(v >> i) & 0xF]);
}

/* small unsigned decimal (enough for our counters) */
static void uart_putdec(uint32_t v)
{
    char buf[10];
    int n = 0;
    if (v == 0) { uart_putc('0'); return; }
    while (v) { buf[n++] = (char)('0' + (v % 10u)); v /= 10u; }
    while (n) uart_putc(buf[--n]);
}

/* ------------------------------------------------------------------------- *
 *  Trap reporter - called from crt0.S trap_entry. Never returns.
 * ------------------------------------------------------------------------- */

void trap_report(uint32_t mcause, uint32_t mepc, uint32_t mtval)
{
    uart_puts("\n*** TRAP ***  mcause=0x");
    uart_puthex(mcause);
    uart_puts(" mepc=0x");
    uart_puthex(mepc);
    uart_puts(" mtval=0x");
    uart_puthex(mtval);
    uart_puts("\n(a load/store access fault here usually means DDR is not "
              "calibrated or the address faulted on the fabric)\n");
    uart_puts("HALTED.\n");
    for (;;)
        __asm__ volatile("wfi");
}

/* ------------------------------------------------------------------------- *
 *  Cache defeat
 *
 *  rv32im (base I + M) provides FENCE for ordering but NO cache-management
 *  instructions: the Zicbom CBO ops and any cache CSRs are not in this ISA, and
 *  MicroBlaze V does not document architectural cache-flush CSRs reachable from
 *  rv32im. (Classic MicroBlaze had WDC/WIC; the RISC-V MicroBlaze V does not
 *  expose an equivalent to unprivileged rv32im code.)  A bare `fence` orders
 *  accesses but does NOT write a dirty write-back line out to DRAM.
 *
 *  So we defeat the cache the portable way (option (c) from the brief, in its
 *  rigorous form): read a contiguous scratch window whose size >= the D-cache
 *  size. Touching cache_size bytes maps `assoc` fresh tags onto every set, which
 *  evicts (and writes back, if dirty) whatever line was under test. This makes
 *  the subsequent read-back miss and fetch the value the DRAM actually holds.
 *
 *  This is a *weaker* guarantee than a true architectural flush: it assumes a
 *  normal set-associative LRU/pseudo-LRU write-back D-cache of <= DCACHE_BYTES.
 *  It is the strongest thing available without cache CSRs, and it is why every
 *  read-back below is preceded by dcache_flush().
 * ------------------------------------------------------------------------- */

static volatile uint32_t g_flush_sink;   /* keeps the thrash reads live */

static void dcache_flush(void)
{
    volatile uint32_t *p = (volatile uint32_t *)(DDR_BASE + FLUSH_OFF);
    uint32_t acc = 0;

    __asm__ volatile("fence rw,rw" ::: "memory");   /* order prior stores first */
    for (uint32_t i = 0; i < FLUSH_BYTES / 4u; ++i)
        acc += p[i];
    g_flush_sink = acc;                              /* defeat DCE */
    __asm__ volatile("fence rw,rw" ::: "memory");
}

/* ------------------------------------------------------------------------- *
 *  PRNG - xorshift32 (needs only rv32i shifts/xor; deterministic, reproducible)
 * ------------------------------------------------------------------------- */

static inline uint32_t xorshift32(uint32_t *s)
{
    uint32_t x = *s;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    *s = x;
    return x;
}

/* ------------------------------------------------------------------------- *
 *  Mismatch reporting
 * ------------------------------------------------------------------------- */

static void report(volatile uint32_t *addr, uint32_t expected, uint32_t got)
{
    uart_puts("  MISMATCH @0x");
    uart_puthex((uint32_t)(uintptr_t)addr);
    uart_puts(" exp=0x");
    uart_puthex(expected);
    uart_puts(" got=0x");
    uart_puthex(got);
    uart_putc('\n');
}

/* ------------------------------------------------------------------------- *
 *  Test 1: walking-ones ADDRESS test  (Barr-style)
 *
 *  Writes a pattern to each power-of-two WORD offset, then perturbs single
 *  offsets and confirms nothing else aliased. Catches stuck-high, stuck-low and
 *  shorted address lines - the classic DDR bring-up failure. The offsets span
 *  the whole aperture (up to ~1 GiB here), so high address bits are exercised in
 *  real DRAM; dcache_flush() before each verify makes even the near offsets miss
 *  the cache.
 * ------------------------------------------------------------------------- */

static uint32_t addr_test(volatile uint32_t *base, uint32_t nwords)
{
    const uint32_t pattern = 0xAAAAAAAAu;
    const uint32_t anti    = 0x55555555u;
    uint32_t errs = 0;
    uint32_t off, toff;

    /* seed every power-of-two offset with the pattern */
    for (off = 1; off < nwords; off <<= 1)
        base[off] = pattern;

    /* stuck-high address bits: writing base[0] must not disturb base[off] */
    base[0] = anti;
    dcache_flush();
    for (off = 1; off < nwords; off <<= 1) {
        uint32_t got = base[off];
        if (got != pattern) {
            if (errs < MAX_REPORT) report(&base[off], pattern, got);
            errs++;
        }
    }

    /* stuck-low / shorted address bits: perturb one offset at a time */
    base[0] = pattern;
    for (toff = 1; toff < nwords; toff <<= 1) {
        base[toff] = anti;
        dcache_flush();
        if (base[0] != pattern) {
            if (errs < MAX_REPORT) report(&base[0], pattern, base[0]);
            errs++;
        }
        for (off = 1; off < nwords; off <<= 1) {
            if (off == toff) continue;
            uint32_t got = base[off];
            if (got != pattern) {
                if (errs < MAX_REPORT) report(&base[off], pattern, got);
                errs++;
            }
        }
        base[toff] = pattern;   /* restore before moving on */
    }
    return errs;
}

/* ------------------------------------------------------------------------- *
 *  Test 2: DATA-BUS test at one address (walking 1s then walking 0s)
 *
 *  Catches shorted/open DQ bits. dcache_flush() between write and read makes the
 *  value round-trip through the actual DRAM DQ lines rather than the cache.
 * ------------------------------------------------------------------------- */

static uint32_t databus_test(volatile uint32_t *addr)
{
    uint32_t errs = 0;
    uint32_t bit;

    for (bit = 1u; bit != 0u; bit <<= 1) {          /* walking ones  */
        *addr = bit;
        dcache_flush();
        uint32_t got = *addr;
        if (got != bit) { report(addr, bit, got); errs++; }
    }
    for (bit = 1u; bit != 0u; bit <<= 1) {          /* walking zeros */
        uint32_t w = ~bit;
        *addr = w;
        dcache_flush();
        uint32_t got = *addr;
        if (got != w) { report(addr, w, got); errs++; }
    }
    return errs;
}

/* ------------------------------------------------------------------------- *
 *  Test 3: device / pattern test over a bounded region
 *
 *  Write pass fills the region with a deterministic xorshift stream; the region
 *  (256 KiB) is far larger than the 8 KiB D-cache, so the sequential write pass
 *  self-evicts as it advances. A dcache_flush() before the read pass evicts the
 *  final <=8 KiB tail, so the whole read pass fetches from DRAM. Reports the
 *  first MAX_REPORT mismatches with address/expected/got.
 * ------------------------------------------------------------------------- */

static uint32_t device_test(volatile uint32_t *base, uint32_t nwords, uint32_t seed)
{
    uint32_t s = seed;
    uint32_t errs = 0;

    for (uint32_t i = 0; i < nwords; ++i)
        base[i] = xorshift32(&s);

    dcache_flush();

    s = seed;
    for (uint32_t i = 0; i < nwords; ++i) {
        uint32_t exp = xorshift32(&s);
        uint32_t got = base[i];
        if (got != exp) {
            if (errs < MAX_REPORT) report(&base[i], exp, got);
            errs++;
        }
    }
    return errs;
}

/* ------------------------------------------------------------------------- *
 *  main
 * ------------------------------------------------------------------------- */

int main(void)
{
    uart_init();

    uart_puts("\n==============================================\n");
    uart_puts(" nanoSoC MicroBlaze-V  DDR4 memtest\n");
    uart_puts("==============================================\n");
    uart_puts(" ISA build : rv32im / ilp32 / medany / no-relax\n");
    uart_puts(" UART base : 0x"); uart_puthex(UART_BASE);
    uart_puts("  (must match mbv_soc.tcl assign_bd_address)\n");
    uart_puts(" DDR base  : 0x"); uart_puthex(DDR_BASE);
    uart_puts("  aperture 0x"); uart_puthex(DDR_APERTURE); uart_puts(" (2 GiB)\n");
    uart_puts(" D-cache   : "); uart_putdec(DCACHE_BYTES);
    uart_puts(" B, flush window "); uart_putdec(FLUSH_BYTES);
    uart_puts(" B (read-thrash eviction; no cache CSRs on rv32im)\n");
    uart_puts("----------------------------------------------\n");

    uint32_t total_errors = 0;
    uint32_t bytes_tested = 0;

    /* --- Test 1: address bus --- */
    uart_puts("[1] walking-ones ADDRESS test over aperture ... \n");
    uint32_t aper_words = DDR_APERTURE / 4u;        /* 0x2000_0000 words */
    uint32_t e1 = addr_test((volatile uint32_t *)DDR_BASE, aper_words);
    total_errors += e1;
    uart_puts(e1 ? "    -> address test FAILED, errors=" : "    -> address test ok, errors=");
    uart_putdec(e1); uart_putc('\n');

    /* --- Test 2: data bus --- */
    uart_puts("[2] DATA-BUS test @0x");
    uart_puthex(DDR_BASE + DATABUS_OFF);
    uart_puts(" (walking 1s/0s, 64 patterns) ... \n");
    uint32_t e2 = databus_test((volatile uint32_t *)(DDR_BASE + DATABUS_OFF));
    total_errors += e2;
    bytes_tested += 64u * 4u;
    uart_puts(e2 ? "    -> data-bus test FAILED, errors=" : "    -> data-bus test ok, errors=");
    uart_putdec(e2); uart_putc('\n');

    /* --- Test 3: device / pattern --- */
    uart_puts("[3] DEVICE test 0x"); uart_puthex(DEV_BYTES);
    uart_puts(" bytes @0x"); uart_puthex(DDR_BASE + DEV_OFF);
    uart_puts(" (xorshift seed 0x"); uart_puthex(SEED); uart_puts(") ... \n");
    uint32_t dev_words = DEV_BYTES / 4u;
    uint32_t e3 = device_test((volatile uint32_t *)(DDR_BASE + DEV_OFF), dev_words, SEED);
    total_errors += e3;
    bytes_tested += DEV_BYTES;
    uart_puts(e3 ? "    -> device test FAILED, errors=" : "    -> device test ok, errors=");
    uart_putdec(e3); uart_putc('\n');

    /* --- Summary --- */
    uart_puts("----------------------------------------------\n");
    uart_puts(" bytes verified : "); uart_putdec(bytes_tested);
    uart_puts(" (device+data-bus; address test strides the full aperture)\n");
    uart_puts(" total errors   : "); uart_putdec(total_errors); uart_putc('\n');
    if (total_errors == 0)
        uart_puts("\n****  PASS  ****\n");
    else
        uart_puts("\n####  FAIL  ####\n");

    return 0;   /* crt0 parks the core */
}
