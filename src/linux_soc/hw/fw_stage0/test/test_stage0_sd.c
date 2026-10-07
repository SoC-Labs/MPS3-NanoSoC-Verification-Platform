/*
 * test_stage0_sd.c -- stage0's uSD backend (stage0_sd.c) over D13's REAL driver
 * (firmware/usd/usd.c) and D13's register-level card fake (fake_usd.c), both
 * compiled READ-ONLY from $(USD_SRC), with -DMPS3_HAL_MOCK. The whole boot
 * order (stage0_flow.c) runs on top, so this is the slot boot end to end short
 * of silicon.
 *
 * The fake clock advances per register read (mock_regs_set_us_per_read), which
 * is what lets stage0's BLOCKING wrapper -- a loop of usd_poll() against the
 * timebase -- reach every timeout on the host.
 *
 * Cases:
 *   1 slot A boots through the real driver, and stage0 writes NOTHING to the
 *     card (fake_usd_blocks_written unchanged): the read-only rule (S5)
 *   2 slot A bad -> slot B, whose 300 KB region lives in unwritten card blocks
 *     (the fake's pattern bytes): multi-block ops, 8-block chunking, unaligned
 *     header/entry reads through the one-block cache
 *   3 no card: NOCARD in < 200 ms of card time, ZERO DATA writes, EN never set
 *     (D13 rule 1 holds through stage0)
 *   4 no usd_spi block (ID mismatch): NOHW, the page is never written
 *   5 SDSC card: UNSUP;   6 dead card: ERROR/TIMEOUT within the init bound
 *   7 card pulled mid-read: the slot read fails (never hangs), rescue
 */
#include <errno.h>

#include "s0_testutil.h"
#include "../stage0_boot.h"
#include "../stage0_flow.h"
#include "../stage0_sd.h"
#include "usd.h"
#include "mock_regs.h"
#include "fake_usd.h"

void s0_poll_hook(void) {}

#define DDR_BASE  0x80000000u
#define DDR_SPAN  (4u << 20)
#define LBA_A     67584u
#define LBA_B     198656u
#define SLOT_N    131072u

static uint8_t *g_ddr;

static int op_ddr(void *c) { (void)c; return S0_DDR_OK; }
static int op_sd_init(void *c, uint32_t *b, uint32_t *d) { (void)c; return s0_usd_init(b, d); }
static int op_sd_read(void *c, uint32_t lba, uint32_t n, void *dst)
{
    (void)c;
    return s0_usd_read_blocks(lba, n, dst);
}
static void *op_a2p(uint32_t a, uint32_t len, void *c)
{
    (void)c;
    return s0_region_in_bounds(DDR_BASE, DDR_SPAN, a, len) ? g_ddr + (a - DDR_BASE) : NULL;
}
static const struct s0_flow_ops OPS = { op_ddr, op_sd_init, op_sd_read, op_a2p, NULL, NULL, NULL, NULL, NULL };
static const struct s0_build_ids IDS = { 1u, 0x3F1A560Fu, 2u };
static struct s0_status ST;

/* ---- writing the card through the driver (test setup, not stage0) ---------------- */

static void card_up(void)
{
    usd_init();
    for (int i = 0; i < 5000 && usd_state() != USD_READY; i++) {
        mock_time_advance_ms(1);
        usd_poll(mock_time_now_ms());
    }
    CHECK(usd_state() == USD_READY, "setup: card READY (state %d)", (int)usd_state());
}

static void card_write(uint32_t lba, const uint8_t *buf, uint32_t nblk)
{
    for (uint32_t k = 0; k < nblk; k++) {
        CHECK(usd_write_start(lba + k, 1u, buf + 512u * k) == 0, "setup: write start");
        for (int i = 0; i < 100000 && usd_io_status() == USD_IO_BUSY; i++) {
            mock_time_advance_ms(1);
            usd_poll(mock_time_now_ms());
        }
        CHECK(usd_io_status() == USD_IO_DONE, "setup: write lba %u", lba + k);
    }
}

static uint8_t g_img[64u * 1024u];
static uint8_t g_pay[8192];

static void fresh_card(void)
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
    card_write(0u, mbr, 1u);

    /* slot A: a small 1-region image, fully written */
    tu_fill(g_pay, 3000u, 7u);
    struct tu_region r = { g_pay, 3000u, DDR_BASE };
    uint32_t n = tu_build_image(g_img, sizeof g_img, &r, 1u, DDR_BASE, 0u, 0u);
    card_write(LBA_A, g_img, (n + 511u) / 512u);
}

/* Slot B: header block written; its one region (300 KB at image offset 4096,
 * i.e. from LBA_B + 8) is never written, so it reads as the fake's pattern. */
#define B_LEN 300001u
static uint8_t *g_bpat;
static void write_slot_b(void)
{
    for (uint32_t i = 0; i < B_LEN; i++)
        g_bpat[i] = fake_usd_pattern_byte(LBA_B + 8u + i / 512u, i % 512u);
    uint8_t hdr[512];
    memset(hdr, 0, sizeof hdr);
    tu_put32(hdr + 32, 4096u);
    tu_put32(hdr + 36, DDR_BASE + 0x100000u);
    tu_put32(hdr + 40, B_LEN);
    tu_put32(hdr + 44, tu_crc32(g_bpat, B_LEN));
    tu_put32(hdr + 0, 0x424C3053u);
    tu_put32(hdr + 4, 2u);
    tu_put32(hdr + 8, 1u);
    tu_put32(hdr + 12, DDR_BASE);
    tu_put32(hdr + 28, tu_crc32(hdr, 32u + 16u));      /* table CRC */
    card_write(LBA_B, hdr, 1u);
}

static int boot(struct s0_result *res)
{
    int ok;
    memset(&ST, 0, sizeof ST);
    memset(g_ddr, 0, DDR_SPAN);
    s0_status_open(&ST, &IDS, 0u, 2u);
    return s0_boot_select(&ST, &OPS, res, &ok);
}

int main(void)
{
    struct s0_result res;
    g_ddr = calloc(1, DDR_SPAN);
    g_bpat = malloc(B_LEN);

    printf("test 1: slot A boots through D13's driver; stage0 never writes the card\n");
    fresh_card();
    uint32_t written = fake_usd_blocks_written();
    uint32_t wcmds = fake_usd_cmd_count(24) + fake_usd_cmd_count(25);
    uint32_t t0 = mock_time_now_ms();
    CHECK(boot(&res) == S0_FROM_A, "from A (a=%u sd=%u)", ST.slot_a_rc, ST.sd_result);
    CHECK(memcmp(g_ddr, g_pay, 3000u) == 0, "payload");
    CHECK(fake_usd_blocks_written() == written, "READ-ONLY: %u blocks written by stage0",
          fake_usd_blocks_written() - written);
    CHECK(fake_usd_cmd_count(24) + fake_usd_cmd_count(25) == wcmds, "no CMD24/CMD25 issued");
    printf("    (card time %u ms)\n", mock_time_now_ms() - t0);

    printf("test 2: slot A bad -> slot B, 300 KB region from unwritten blocks\n");
    fresh_card();
    write_slot_b();
    {   /* corrupt slot A's payload */
        uint8_t blk[512];
        tu_fill(g_pay, 3000u, 7u);
        struct tu_region r = { g_pay, 3000u, DDR_BASE };
        tu_build_image(g_img, sizeof g_img, &r, 1u, DDR_BASE, 0u, 0u);
        memcpy(blk, g_img + 4096u, 512u);
        blk[100] ^= 0x40u;
        card_write(LBA_A + 8u, blk, 1u);
    }
    written = fake_usd_blocks_written();
    CHECK(boot(&res) == S0_FROM_B, "from B");
    CHECK(ST.slot_a_rc == S0_ECRC && ST.slot_b_rc == S0_OK, "rcs %u %u", ST.slot_a_rc, ST.slot_b_rc);
    CHECK(memcmp(g_ddr + 0x100000u, g_bpat, B_LEN) == 0, "300 KB region bytes");
    CHECK(fake_usd_cmd_count(18) > 0u, "multi-block reads used");
    CHECK(fake_usd_blocks_written() == written, "READ-ONLY");
    CHECK(fake_usd_protocol_errors() == 0u && fake_usd_ovr_count() == 0u, "clean SPI");

    printf("test 3: no card -> NOCARD fast, zero DATA writes, pads never enabled\n");
    mock_regs_reset();
    fake_usd_reset();
    mock_regs_set_us_per_read(20u);
    t0 = mock_time_now_ms();
    CHECK(boot(&res) == S0_FROM_NONE && ST.sd_result == S0_SD_NOCARD, "NOCARD (%u)", ST.sd_result);
    CHECK(ST.rescue_reason == S0_RR_NOCARD, "reason");
    CHECK(mock_time_now_ms() - t0 < 200u, "took %u ms", mock_time_now_ms() - t0);
    CHECK(fake_usd_data_writes() == 0u && fake_usd_en_writes() == 0u && !fake_usd_pads_ever_driven(),
          "D13 rule 1: data=%u en=%u", fake_usd_data_writes(), fake_usd_en_writes());

    printf("test 4: no usd_spi block -> NOHW, page never written\n");
    mock_regs_reset();
    fake_usd_reset();
    fake_usd_set_id(0x12345678u);
    mock_regs_set_us_per_read(20u);
    CHECK(boot(&res) == S0_FROM_NONE && ST.sd_result == S0_SD_NOHW, "NOHW (%u)", ST.sd_result);
    CHECK(fake_usd_page_writes() == 0u, "page writes %u", fake_usd_page_writes());

    printf("test 5: SDSC card -> UNSUP\n");
    mock_regs_reset();
    fake_usd_reset();
    fake_usd_set_sdsc(1);
    fake_usd_insert();
    mock_regs_set_us_per_read(20u);
    CHECK(boot(&res) == S0_FROM_NONE && ST.sd_result == S0_SD_UNSUP, "UNSUP (%u)", ST.sd_result);
    CHECK(ST.rescue_reason == S0_RR_UNSUP, "reason");

    printf("test 6: dead card -> error within the init bound\n");
    mock_regs_reset();
    fake_usd_reset();
    fake_usd_set_unresponsive(1);
    fake_usd_insert();
    mock_regs_set_us_per_read(20u);
    t0 = mock_time_now_ms();
    CHECK(boot(&res) == S0_FROM_NONE &&
          (ST.sd_result == S0_SD_ERROR || ST.sd_result == S0_SD_TIMEOUT), "ERROR (%u)", ST.sd_result);
    CHECK(mock_time_now_ms() - t0 <= 8200u, "bounded: %u ms", mock_time_now_ms() - t0);
    CHECK(ST.rescue_reason == S0_RR_SDERR, "reason");

    printf("test 7: card pulled mid-read -> the load fails, never hangs\n");
    fresh_card();
    write_slot_b();
    fake_usd_remove_after_bytes(2500u);
    CHECK(boot(&res) == S0_FROM_NONE, "rescue");
    CHECK(ST.slot_a_rc == S0_EREAD || ST.slot_b_rc == S0_EREAD, "a read failed (%u %u)",
          ST.slot_a_rc, ST.slot_b_rc);

    printf("\n%d checks, %d failed\n", g_checks, g_fails);
    printf("RESULT: stage0 uSD backend %s\n", g_fails ? "FAILED" : "PASSED");
    return g_fails ? 1 : 0;
}
