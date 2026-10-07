/* qspi_loader.c — M0-side QSPI flash loader (the fix for SWD bulk-programming).
 *
 * WHY THIS EXISTS
 *   Programming flash from the host over SWD costs ~18 register round trips per
 *   4 bytes. Measured on silicon 2026-07-18: 256 B took 167 s (~1.5 B/s), so the
 *   160 KB MicroPython image would take ~29 HOURS. The link is not the problem —
 *   a bulk block move over the same SWD link runs at ~180 B/s, 60x faster. The
 *   problem is spending 18 round trips per 4 bytes.
 *
 *   So: the host bulk-writes a payload chunk into IMEM (one fast block move) and
 *   the M0 does the register banging locally at CPU speed, where a register write
 *   is nanoseconds instead of 75 ms. Total time becomes the upload time,
 *   ~15 min for 160 KB instead of ~29 h.
 *
 * HOW IT RUNS
 *   The host halts the M0, writes this image to IMEM_BASE, sets SP/PC, and
 *   resumes — the bootrom is bypassed entirely, so this never depends on the
 *   flash-boot path it is being used to populate.
 *
 * PROTOCOL (poll-based mailbox at a FIXED address, so the host needs no ELF)
 *   host: fill BUFFER, set offset/length, set cmd, then poll status
 *   dut:  status=BUSY -> do the work -> status=OK or ERR|code, cmd=NONE
 *
 * EVERY register access below mirrors the host model in
 * pyverify/qspi_flash.py, which is now PROVEN on silicon (unlock -> erase ->
 * program -> verify all pass). Do not "simplify" the two-phase SPI_CMD or the
 * read-backs; see the notes at each step.
 */
#include <stdint.h>

/* --- memory map (swd.py: IMEM_BASE/IMEM_SIZE; IMEM is 128 KB) ------------- */
#define IMEM_BASE        0x10000000u
#define MAILBOX_ADDR     0x10010000u          /* fixed: host pokes this directly */
#define BUFFER_ADDR      0x10010040u
#define BUFFER_SIZE      0x0000C000u          /* 48 KB payload window            */

/* --- QSPI controller (xip_bringup.h + qspi_flash.py, silicon-proven) ------ */
#define QSPI_BASE        0x74000000u
#define REG32(a)         (*(volatile uint32_t *)(uintptr_t)(a))
#define REG_CTRL         0x00u
#define REG_STATUS       0x04u
#define REG_SPI_CMD      0x08u
#define REG_ADDR         0x0Cu
#define REG_RDATA0       0x10u
#define REG_WDATA0       0x20u
#define REG_CLK_DIV      0x34u

#define CMD_ENABLE       (1u << 8)
#define CMD_READ         (1u << 9)
#define CMD_WRITE        (1u << 10)
#define CMD_ADDR_EN      (1u << 11)
#define CMD_NRW_SHIFT    16u
#define STATUS_BUSY      (1u << 0)
#define STATUS_IRQ_CLR   (1u << 8)

#define OP_WREN          0x06u
#define OP_RDSR          0x05u
#define OP_PP            0x02u
#define OP_SECTOR_ERASE  0x20u
#define OP_READ          0x03u
#define OP_ULBPR         0x98u
#define SR_WIP           (1u << 0)

#define PAGE_SIZE        256u
#define SECTOR_SIZE      4096u
/* CLK_DIV resets to 1, which garbles RDID on real hardware; >=4 is the AC spec
 * (docs/QSPI_RP_BOARD_BRINGUP.md). Firmware obligation — RTL enforces no floor. */
#define CLK_DIV_VALUE    4u

/* --- mailbox ------------------------------------------------------------- */
#define MB_MAGIC_VALUE   0x51464C44u          /* "QFLD" */
enum { CMD_NONE = 0, CMD_UNLOCK = 1, CMD_ERASE = 2, CMD_PROGRAM = 3, CMD_CRC = 4 };
enum { ST_IDLE = 0, ST_BUSY = 1, ST_OK = 2, ST_ERR = 0x80000000u };
enum { ERR_BUSY_TIMEOUT = 1, ERR_WIP_TIMEOUT = 2, ERR_BAD_LEN = 3, ERR_BAD_CMD = 4 };

typedef struct {
    volatile uint32_t magic;    /* +0x00 loader writes MB_MAGIC_VALUE when live */
    volatile uint32_t cmd;      /* +0x04 host writes; loader clears to CMD_NONE */
    volatile uint32_t offset;   /* +0x08 flash byte offset                      */
    volatile uint32_t length;   /* +0x0C bytes                                  */
    volatile uint32_t status;   /* +0x10 ST_*                                   */
    volatile uint32_t crc;      /* +0x14 CRC result (CMD_CRC)                   */
    volatile uint32_t ops;      /* +0x18 completed-command counter (liveness)   */
} mailbox_t;

#define MB  ((mailbox_t *)(uintptr_t)MAILBOX_ADDR)
#define BUF ((volatile uint8_t *)(uintptr_t)BUFFER_ADDR)

/* --- controller primitives ----------------------------------------------- */

/* Bounded so a wedged controller reports an error instead of hanging the M0
 * (the host would otherwise see a silent never-completing command). */
#define ASSERT_LIMIT 1000u      /* bounded wait for BUSY to rise */
#define BUSY_LIMIT  1000000u
#define WIP_LIMIT   20000000u

static int wait_not_busy(void)
{
    for (uint32_t i = 0; i < BUSY_LIMIT; i++) {
        if (!(REG32(QSPI_BASE + REG_STATUS) & STATUS_BUSY)) return 0;
    }
    return -ERR_BUSY_TIMEOUT;
}

/* One SST26 command. Mirrors qspi_flash.py::_spi_cmd exactly: ADDR/WDATA, then
 * SPI_CMD with ENABLE=0 (setup) and again with ENABLE=1 (trigger), poll BUSY,
 * read RDATA, clear IRQ. The two-phase write is load-bearing — a single write
 * with ENABLE set does not latch the command fields. */
static int spi_cmd(uint32_t opcode, int use_addr, uint32_t addr,
                   int is_read, int is_write, uint32_t n_bytes,
                   uint32_t wdata, uint32_t *rdata_out)
{
    uint32_t cmd = (opcode & 0xFFu)
                 | (is_read  ? CMD_READ  : 0u)
                 | (is_write ? CMD_WRITE : 0u)
                 | (use_addr ? CMD_ADDR_EN : 0u)
                 | (((n_bytes ? n_bytes - 1u : 0u) & 0xFu) << CMD_NRW_SHIFT);

    if (use_addr) {
        REG32(QSPI_BASE + REG_ADDR) = addr & 0x3FFFFFu;
        (void)REG32(QSPI_BASE + REG_ADDR);   /* read-back forces the write to
                                              * land: the controller returns
                                              * registered HRDATA when idle
                                              * (xip_bringup.h:26) */
    }
    if (is_write) {
        REG32(QSPI_BASE + REG_WDATA0) = wdata;
        (void)REG32(QSPI_BASE + REG_WDATA0);
    }
    /* EVERY register write needs its read-back to land, including these two.
     * Omitting them here was a real silicon bug (2026-07-19): the trigger never
     * took, so no SPI transaction ran, RDATA0 stayed 0, BUSY read 0 ("not
     * busy") and RDSR read 0 ("WIP clear") -- every operation reported success
     * while doing NOTHING. A CRC over known flash returned the CRC of 256 zero
     * bytes, which is how it was caught. Silent vacuous success is the worst
     * failure mode; do not remove these. */
    REG32(QSPI_BASE + REG_SPI_CMD) = cmd;                 /* phase 1: setup   */
    (void)REG32(QSPI_BASE + REG_SPI_CMD);
    REG32(QSPI_BASE + REG_SPI_CMD) = cmd | CMD_ENABLE;    /* phase 2: trigger */
    (void)REG32(QSPI_BASE + REG_SPI_CMD);

    /* Wait for BUSY to ASSERT before waiting for it to clear.
     *
     * Polling only for "clear" RACES the controller from the M0: the poll lands
     * nanoseconds after the trigger, before BUSY has asserted, so the
     * transaction looks already-complete and RDATA0 is read before the data
     * arrives. Silicon 2026-07-19: a 4-byte read returned all zeros this way,
     * while a 1-byte read happened to survive -- i.e. it failed SILENTLY and
     * size-dependently. The host driver never hits this because its SWD round
     * trips are ~75 ms, by which time the transaction is long finished.
     *
     * If BUSY is missed entirely (transaction shorter than this loop), the
     * bounded wait simply expires and we fall through to the clear-poll having
     * burned the cycles anyway -- safe either way, so the timeout is ignored. */
    for (uint32_t i = 0; i < ASSERT_LIMIT; i++) {
        if (REG32(QSPI_BASE + REG_STATUS) & STATUS_BUSY) break;
    }

    int rc = wait_not_busy();
    if (rc) return rc;

    if (rdata_out) *rdata_out = REG32(QSPI_BASE + REG_RDATA0);
    REG32(QSPI_BASE + REG_STATUS) = STATUS_IRQ_CLR;
    return 0;
}

static int write_enable(void) { return spi_cmd(OP_WREN, 0, 0, 0, 0, 0, 0, 0); }

/* RDATA0 is byte-swapped by the controller; the status byte is the low lane
 * after the swap (qspi_flash.py::unpack_rx_bytes with n=1). */
/* Recover flash byte i of an n-byte read, matching qspi_flash.py's proven
 * unpack_rx_bytes: value = byteswap32(rdata0); byte_i = value >> 8*(n-1-i).
 * For n==4 that reduces to (raw >> 8*i), but for n<4 it does NOT -- an RDSR
 * (n=1) lives in bits 31:24 of raw, not the low lane. Getting this wrong makes
 * WIP polling read a garbage byte. */
static uint8_t rx_byte(uint32_t raw, uint32_t n, uint32_t i)
{
    uint32_t value = ((raw >> 24) & 0x000000FFu) | ((raw >> 8) & 0x0000FF00u)
                   | ((raw << 8) & 0x00FF0000u) | ((raw << 24) & 0xFF000000u);
    return (uint8_t)((value >> (8u * (n - 1u - i))) & 0xFFu);
}

static int read_status_reg(uint32_t *sr)
{
    uint32_t raw = 0;
    int rc = spi_cmd(OP_RDSR, 0, 0, 1, 0, 1, 0, &raw);
    if (rc) return rc;
    *sr = rx_byte(raw, 1u, 0u);
    return 0;
}

static int wait_wip_clear(void)
{
    for (uint32_t i = 0; i < WIP_LIMIT; i++) {
        uint32_t sr;
        int rc = read_status_reg(&sr);
        if (rc) return rc;
        if (!(sr & SR_WIP)) return 0;
    }
    return -ERR_WIP_TIMEOUT;
}

/* --- operations ---------------------------------------------------------- */

/* SST26 boots FULLY WRITE-PROTECTED. Without WREN+ULBPR every erase/program is
 * a silent no-op that "succeeds" — proven necessary on silicon. */
static int do_unlock(void)
{
    int rc = write_enable();
    if (rc) return rc;
    return spi_cmd(OP_ULBPR, 0, 0, 0, 0, 0, 0, 0);
}

static int do_erase(uint32_t offset, uint32_t length)
{
    uint32_t first = offset & ~(SECTOR_SIZE - 1u);
    uint32_t last  = (offset + length - 1u) & ~(SECTOR_SIZE - 1u);
    if (length == 0u) return -ERR_BAD_LEN;
    for (uint32_t a = first; a <= last; a += SECTOR_SIZE) {
        int rc = write_enable();
        if (rc) return rc;
        rc = spi_cmd(OP_SECTOR_ERASE, 1, a, 0, 0, 0, 0, 0);
        if (rc) return rc;
        rc = wait_wip_clear();
        if (rc) return rc;
        if (a == last) break;             /* guard the a+SECTOR wrap at 4 GB */
    }
    return 0;
}

/* The controller moves <=4 data bytes per command (one WDATA word). For a full
 * 4-byte group the proven host packing (pack_tx_word) reduces to a plain
 * little-endian load, which is what the M0 does natively; the tail path below
 * reproduces the general formula. */
static uint32_t pack_tx(const volatile uint8_t *p, uint32_t n)
{
    uint32_t v = 0;
    for (uint32_t i = 0; i < n; i++) v |= ((uint32_t)p[i]) << (8u * (n - 1u - i));
    /* byteswap32 */
    return ((v >> 24) & 0x000000FFu) | ((v >> 8) & 0x0000FF00u)
         | ((v << 8) & 0x00FF0000u) | ((v << 24) & 0xFF000000u);
}

static int do_program(uint32_t offset, uint32_t length)
{
    if (length == 0u || length > BUFFER_SIZE) return -ERR_BAD_LEN;
    for (uint32_t done = 0; done < length; ) {
        uint32_t n = length - done;
        if (n > 4u) n = 4u;
        /* never let a program cross a 256 B page boundary */
        uint32_t page_left = PAGE_SIZE - ((offset + done) & (PAGE_SIZE - 1u));
        if (n > page_left) n = page_left;

        int rc = write_enable();
        if (rc) return rc;
        rc = spi_cmd(OP_PP, 1, offset + done, 0, 1, n, pack_tx(BUF + done, n), 0);
        if (rc) return rc;
        rc = wait_wip_clear();
        if (rc) return rc;
        done += n;
    }
    return 0;
}

/* CRC32 (reflected 0xEDB88320, init/final ~0) — matches boot_table.h's
 * boot_crc32 and Python's binascii.crc32, so the host can verify a whole image
 * WITHOUT reading it back over SWD. That is the difference between a
 * seconds-long verify and an hours-long one. */
static uint32_t crc32_update(uint32_t crc, uint8_t byte)
{
    crc ^= byte;
    for (int k = 0; k < 8; k++)
        crc = (crc >> 1) ^ (0xEDB88320u & (uint32_t)(-(int32_t)(crc & 1u)));
    return crc;
}

static int do_crc(uint32_t offset, uint32_t length, uint32_t *out)
{
    uint32_t crc = 0xFFFFFFFFu;
    for (uint32_t done = 0; done < length; ) {
        uint32_t n = length - done;
        if (n > 4u) n = 4u;
        uint32_t raw = 0;
        int rc = spi_cmd(OP_READ, 1, offset + done, 1, 0, n, 0, &raw);
        if (rc) return rc;
        for (uint32_t i = 0; i < n; i++)
            crc = crc32_update(crc, rx_byte(raw, n, i));
        done += n;
    }
    *out = crc ^ 0xFFFFFFFFu;
    return 0;
}

/* --- entry --------------------------------------------------------------- */
void loader_main(void)
{
    /* Clear XIP_ACTIVE FIRST. After a flash boot, stage-0 has put the
     * controller in execute-in-place mode, and in that state it does NOT accept
     * APB SPI commands -- every transaction times out BUSY. Observed on silicon
     * 2026-07-21: a CRC at flash 0x0 returned ERR 1 (controller BUSY timeout)
     * immediately after a successful flash boot, while the identical CRC had
     * worked before the boot. Mirrors xip_bringup.c, which also starts from
     * XIP_ACTIVE=0 for the same reason. */
    REG32(QSPI_BASE + REG_CTRL) = 0u;
    (void)REG32(QSPI_BASE + REG_CTRL);

    REG32(QSPI_BASE + REG_CLK_DIV) = CLK_DIV_VALUE;
    (void)REG32(QSPI_BASE + REG_CLK_DIV);      /* read-back: force it to land */

    MB->status = ST_IDLE;
    MB->cmd    = CMD_NONE;
    MB->ops    = 0;
    MB->magic  = MB_MAGIC_VALUE;               /* last: signals "loader live"  */

    for (;;) {
        uint32_t c = MB->cmd;
        if (c == CMD_NONE) continue;

        MB->status = ST_BUSY;
        int rc = 0;
        uint32_t crc = 0;
        /* if/else, not switch: a switch here compiles to a Thumb-1 jump table
         * that pulls in __gnu_thumb1_case_uqi from libgcc, which a freestanding
         * -nostdlib loader does not link. */
        if (c == CMD_UNLOCK)       rc = do_unlock();
        else if (c == CMD_ERASE)   rc = do_erase(MB->offset, MB->length);
        else if (c == CMD_PROGRAM) rc = do_program(MB->offset, MB->length);
        else if (c == CMD_CRC)     rc = do_crc(MB->offset, MB->length, &crc);
        else                       rc = -ERR_BAD_CMD;

        if (c == CMD_CRC && rc == 0) MB->crc = crc;
        MB->ops    = MB->ops + 1u;
        MB->cmd    = CMD_NONE;                 /* clear before status so the
                                                * host never sees OK on a stale
                                                * command */
        MB->status = rc ? (ST_ERR | (uint32_t)(-rc)) : ST_OK;
    }
}
