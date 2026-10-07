/*
 * test_stage0_cold.c -- the B2 failure (RC2 0x44EE76D5, 2026-09-26) on the host:
 * a card that INITIALISES (usd READY) and then fails its first block read --
 * sector 0, the MBR -- so stage0 records sd_result 5 with sd_detail 0x00030000
 * (READY) and goes to rescue without trying a slot. Seen on 3/3 cold boots
 * (power cut, card power-cycled) and on one warm reset that caught Linux
 * mid-write; Linux's mmc_spi then saw corrupted OCR/SCR reads on the slot.
 *
 * The card models are D13's fake (firmware/test/fake_usd.c), compiled INTO this
 * file (#include) so a wrapper hook can reach its internals and give it the
 * behaviours a real card has and the fake does not:
 *   window   flash reads stall (0xFF, no data token) until `window_ms` after
 *            the card's first CMD0 -- a card busy after power-up, or finishing
 *            a write it was cut off in. Commands still get their R1.
 *   ignore   PESSIMISTIC: while a single-block read is pending (waiting for
 *            flash or sending its block) the card decodes no command, CMD0
 *            included, until the block has been clocked out. (The plain fake
 *            is FORGIVING: it decodes commands mid-read.)
 *   r1bad    the R1 of the next N CMD17/CMD18 is corrupted on MISO (the card
 *            got the command and starts the read; the host sees a bad R1).
 *   debris   the card is left mid single-block read at stage0 entry (a warm
 *            reset during Linux I/O).
 *
 * Built twice (test/Makefile): against the current stage0_sd.c + usd.c build
 * (must pass), and -- as the fail-before evidence -- the same file compiles
 * against the pre-fix sources with -DS0_COLD_BEFORE (see the lane report).
 */
#include <errno.h>

#include "s0_testutil.h"
#include "../stage0_boot.h"
#include "../stage0_flow.h"
#include "../stage0_sd.h"
#include "usd.h"
#include "mock_regs.h"
#include "fake_usd.c"            /* D13's fake, INTO this TU: its statics are the model */

void s0_poll_hook(void) {}

#define S0_SD_READ_RETRIES_EXPECTED 3u   /* stage0_sd.c S0_SD_READ_RETRIES */

#define DDR_BASE  0x80000000u
#define DDR_SPAN  (4u << 20)
#define LBA_A     67584u
#define LBA_B     198656u
#define SLOT_N    131072u

static uint8_t *g_ddr;

static int op_ddr(void *x) { (void)x; return S0_DDR_OK; }
static int op_sd_init(void *x, uint32_t *b, uint32_t *d) { (void)x; return s0_usd_init(b, d); }
static int op_sd_read(void *x, uint32_t lba, uint32_t n, void *dst)
{
    (void)x;
    return s0_usd_read_blocks(lba, n, dst);
}
static void *op_a2p(uint32_t a, uint32_t len, void *x)
{
    (void)x;
    return s0_region_in_bounds(DDR_BASE, DDR_SPAN, a, len) ? g_ddr + (a - DDR_BASE) : NULL;
}
static const struct s0_flow_ops OPS = { op_ddr, op_sd_init, op_sd_read, op_a2p, NULL, NULL, NULL, NULL, NULL };
static const struct s0_build_ids IDS = { 1u, 0x44EE76D5u, 0x01000000u };
static struct s0_status ST;

/* ---- the cold-card wrapper ------------------------------------------------------ */

static struct {
    int      armed;
    uint32_t window_ms;
    uint32_t cmd0_base;      /* s_cmd_count[0] at arm: the window opens at the next CMD0 */
    int      open;
    uint32_t t_open;
    int      ignore;
    uint32_t r1bad;
} k;

static int stalled(uint32_t now)
{
    if (!k.armed)
        return 0;
    if (!k.open && s_cmd_count[0] > k.cmd0_base) {
        k.open = 1;
        k.t_open = now;
    }
    return !k.open || (uint32_t)(now - k.t_open) < k.window_ms;
}

static int cold_hook(void *ctx, int is_write, uint32_t base, uint32_t off, uint32_t *val)
{
    if (!(is_write && off == USD_DATA))
        return hook(ctx, is_write, base, off, val);

    uint32_t now = mock_time_now_ms();
    int single_pending = c.rd_active && !c.rd_csd && !c.rd_multi;
    if (c.rd_active && !c.rd_csd && c.rd_pos < 0) {
        if (stalled(now))
            c.rd_nac = 1000u;                   /* flash not ready: keep sending 0xFF */
        else if (c.rd_nac > c.read_nac_cfg)
            c.rd_nac = c.read_nac_cfg;          /* ready now: the token follows */
    }
    uint32_t n_rd = s_cmd_count[17] + s_cmd_count[18];

    int r = hook(ctx, is_write, base, off, val);

    if (k.ignore && single_pending)
        c.cmd_len = 0;                          /* no command decode while it talks */
    if (k.r1bad > 0u && s_cmd_count[17] + s_cmd_count[18] != n_rd && c.q_len > 0u) {
        k.r1bad--;
        c.q[(c.q_head + c.q_len - 1u) % sizeof c.q] |= 0x40u;   /* R1 bit flip on MISO */
    }
    return r;
}

static void arm(uint32_t window_ms, int ignore, uint32_t r1bad)
{
    memset(&k, 0, sizeof k);
    k.armed = 1;
    k.window_ms = window_ms;
    k.cmd0_base = s_cmd_count[0];
    k.ignore = ignore;
    k.r1bad = r1bad;
    mock_regs_set_hook(MPS3_USD_BASE, cold_hook, 0);
}

/* ---- the card: MBR + slot A written through the driver (setup, not stage0) ------- */

static void card_up(void)
{
    usd_init();
    for (int i = 0; i < 20000 && usd_state() != USD_READY; i++) {
        mock_time_advance_ms(1);
        usd_poll(mock_time_now_ms());
    }
    CHECK(usd_state() == USD_READY, "setup: card READY (state %d)", (int)usd_state());
}

static void card_write(uint32_t lba, const uint8_t *buf, uint32_t nblk)
{
    for (uint32_t i = 0; i < nblk; i++) {
        CHECK(usd_write_start(lba + i, 1u, buf + 512u * i) == 0, "setup: write start");
        for (int j = 0; j < 100000 && usd_io_status() == USD_IO_BUSY; j++) {
            mock_time_advance_ms(1);
            usd_poll(mock_time_now_ms());
        }
        CHECK(usd_io_status() == USD_IO_DONE, "setup: write lba %u", lba + i);
    }
}

static uint8_t g_img[64u * 1024u];
static uint8_t g_pay[8192];

static void build_card(void)
{
    mock_regs_reset();
    fake_usd_reset();
    fake_usd_insert();
    mock_regs_set_us_per_read(20u);
    card_up();
    uint8_t mbr[512];
    struct tu_part p[4] = { { 0x7F, LBA_A, SLOT_N }, { 0x7F, LBA_B, SLOT_N },
                            { 0x83, 329728u, 65536u }, { 0xDA, 2048u, 65536u } };
    tu_build_mbr(mbr, p);
    memset(mbr, 0, 446);                       /* boot code: zeros, as stage0_mkcard writes */
    card_write(0u, mbr, 1u);
    tu_fill(g_pay, 3000u, 7u);
    struct tu_region r = { g_pay, 3000u, DDR_BASE };
    uint32_t n = tu_build_image(g_img, sizeof g_img, &r, 1u, DDR_BASE, 0u, 0u);
    card_write(LBA_A, g_img, (n + 511u) / 512u);
}

/* A COLD boot: the FPGA reconfigures (usd_spi at reset values), the card loses
 * power (protocol state gone, flash kept). */
static void cold_boot_card(void)
{
    mock_regs_reset();
    fake_usd_power_cycle();
    mock_regs_set_us_per_read(20u);
}

static int boot(uint32_t *ms)
{
    int ok;
    memset(&ST, 0, sizeof ST);
    memset(g_ddr, 0, DDR_SPAN);
    s0_status_open(&ST, &IDS, 0u, 2u);
    uint32_t t0 = mock_time_now_ms();
    static struct s0_result res;
    int from = s0_boot_select(&ST, &OPS, &res, &ok);
    *ms = mock_time_now_ms() - t0;
    return from;
}

static int booted_a(int from)
{
    return from == S0_FROM_A && memcmp(g_ddr, g_pay, 3000u) == 0;
}

static void report(const char *what, int from, uint32_t ms)
{
    printf("    %-44s -> %s  sd_result %u detail 0x%08X  slots %u/%u  %u ms",
           what, from == S0_FROM_A ? "SLOT A" : from == S0_FROM_NONE ? "RESCUE" : "?",
           ST.sd_result, ST.sd_detail, ST.slot_a_rc, ST.slot_b_rc, ms);
#ifndef S0_COLD_BEFORE
    printf("  rd_fails %u last 0x%08X", s0_usd_read_fails(), s0_usd_read_diag());
#endif
    printf("\n");
}

int main(void)
{
    uint32_t ms;
    int from;
    g_ddr = calloc(1, DDR_SPAN);

    printf("stage0 cold-card models%s\n",
#ifdef S0_COLD_BEFORE
           " -- PRE-FIX build (fail-before evidence)"
#else
           ""
#endif
    );

    printf("test 1: WARM card (no window): slot A boots, first time\n");
    build_card();
    cold_boot_card();
    from = boot(&ms);
    report("warm", from, ms);
    CHECK(booted_a(from), "warm card boots slot A");
#ifndef S0_COLD_BEFORE
    CHECK(s0_usd_read_fails() == 0u, "no read failed");
    CHECK(fake_usd_cmd_clkdiv(17u) == S0_USD_CLKDIV, "data SCK = S0_USD_CLKDIV (%u)",
          fake_usd_cmd_clkdiv(17u));
#endif

    printf("test 2: COLD card, reads stall 3 s after power-up (forgiving card)\n");
    build_card();
    cold_boot_card();
    arm(3000u, 0, 0u);
    from = boot(&ms);
    report("cold, 3 s window, forgiving", from, ms);
    CHECK(booted_a(from), "boots slot A");

    printf("test 3: COLD card, 3 s window, card deaf while a block is pending\n");
    build_card();
    cold_boot_card();
    arm(3000u, 1, 0u);
    from = boot(&ms);
    report("cold, 3 s window, pessimistic", from, ms);
    CHECK(booted_a(from), "boots slot A");

    printf("test 4: corrupted R1 on the MBR read, card deaf while its block is pending\n");
    build_card();
    cold_boot_card();
    arm(0u, 1, 1u);
    from = boot(&ms);
    report("R1 bit flip, pessimistic", from, ms);
    CHECK(booted_a(from), "boots slot A");
#ifndef S0_COLD_BEFORE
    CHECK(s0_usd_read_fails() == 1u && (s0_usd_read_diag() >> 24) == (uint32_t)EIO,
          "one EIO recorded (%u, 0x%08X)", s0_usd_read_fails(), s0_usd_read_diag());
#endif

    printf("test 5: warm reset mid-read: a block still pending at stage0 entry (pessimistic)\n");
    build_card();
    rd_start(0, 0, LBA_A + 3u);                  /* the card owes a block nobody will clock out */
    arm(0u, 1, 0u);
    k.armed = 0;                                 /* no window: only the debris */
    usd_init();                                  /* the watchdog reset: EN=0, pads float */
    from = boot(&ms);
    report("debris at entry, pessimistic", from, ms);
    CHECK(booted_a(from), "boots slot A");

    printf("test 6: a card that NEVER serves a read: bounded, then rescue\n");
    build_card();
    cold_boot_card();
    arm(0xFFFFFFFFu, 0, 0u);
    from = boot(&ms);
    report("reads never served", from, ms);
    CHECK(from == S0_FROM_NONE && ST.sd_result == S0_SD_ERROR, "rescue, sd_result ERROR");
    CHECK(ms < 60000u, "bounded (%u ms)", ms);
#ifndef S0_COLD_BEFORE
    CHECK(s0_usd_read_fails() == 1u + S0_SD_READ_RETRIES_EXPECTED &&
          (s0_usd_read_diag() >> 24) == (uint32_t)ETIMEDOUT,
          "every attempt recorded as ETIMEDOUT (%u, 0x%08X)", s0_usd_read_fails(),
          s0_usd_read_diag());
#endif

    printf("\n%d checks, %d failed\n", g_checks, g_fails);
    printf("RESULT: stage0 cold-card models %s\n", g_fails ? "FAILED" : "PASSED");
    return g_fails ? 1 : 0;
}
