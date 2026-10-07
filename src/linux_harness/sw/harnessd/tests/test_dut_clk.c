/*
 * test_dut_clk.c — `stats.dut_mhz` across a harnessd RESPAWN, and the boot
 * clock re-sync (ILA-mint finding #12; clk_linux.c; HARNESSD_CONTRACT §5.4/§6).
 *
 * The REAL coordinator.c / clkrst.c / net_proto.c over firmware/test's mock
 * register file, driven the way a client drives them: a line into
 * coordinator_dispatch_line(), the codec's bytes out. The mock register file is
 * the FABRIC: a "respawn" re-runs the module inits (coordinator_init, which is
 * what a new process does) WITHOUT resetting it.
 *
 * ONE source, TWO binaries (harnessd Makefile):
 *   test_dut_clk            + clk_linux.c: harnessd's strong mps3_dut_clk_mhz()
 *                           and harnessd_dut_clk_boot_sync().
 *   test_dut_clk_baremetal  -DTEST_BAREMETAL_PROVIDER, no clk_linux.c: the weak
 *                           default = bare metal's RAM shadow. The same respawn
 *                           scenario must read 50 while the fabric runs 100 --
 *                           the bug, reproduced: the NEGATIVE CONTROL.
 */
#include <assert.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../../../../../firmware/clkrst/clkrst.h"
#include "../../../../../firmware/common/log_ring.h"
#include "../../../../../firmware/common/net_proto.h"
#include "../../../../../firmware/common/platform_regs.h"
#include "../../../../../firmware/common/service.h"
#include "../../../../../firmware/coordinator/coordinator.h"
#include "../../../../../firmware/test/fake_config_agent.h"
#include "../../../../../firmware/test/fake_overlay_store.h"
#include "../../../../../firmware/test/mock_regs.h"
#include "../harnessd.h"

static int s_checks;
#define CHECK(c) do { assert(c); s_checks++; } while (0)

#define SID 0x5A5A0001u
uint32_t mps3_shell_static_id(void) { return SID; }

/* clk_linux.c's log sink */
static char s_log[4096];
void harnessd_log(const char *fmt, ...)
{
    va_list ap;
    size_t n = strlen(s_log);
    va_start(ap, fmt);
    vsnprintf(s_log + n, sizeof(s_log) - n, fmt, ap);
    va_end(ap);
}

/* Every MMCM_DRP LOAD write that sets LOAD (bit 0), recorded, then stored. */
static int      s_loads;
static uint32_t s_load_val;
static int drp_hook(void *ctx, int is_write, uint32_t base, uint32_t off, uint32_t *val)
{
    (void)ctx; (void)base;
    if (is_write && off == MMCM_DRP_LOAD && (*val & MMCM_DRP_LOAD_LOAD)) {
        s_loads++;
        s_load_val = *val;
    }
    return 0;   /* fall through: the register file is plain storage */
}

/* The PROCESS state a start builds (what a respawn loses) -- never the fabric. */
static void process_start(void)
{
    fake_config_agent_reset();
    overlay_manifest_info_t greybox = {
        .static_id = SID, .rm_id = 0, .clear_len_words = 4, .clear_crc32 = 0x1111,
    };
    fake_overlay_store_reset();
    fake_overlay_store_set_greybox(&greybox, 0);
    mps3_log_reset();
    mps3_service_reset_stats();
    s_log[0] = '\0';
    coordinator_init();
}

/* A configured fabric: clk_wiz_dut's post-reset register file (D=1 M=20 O=20,
 * 50 MHz), the MMCM locked. */
static void power_on(void)
{
    mock_regs_reset();
    mock_regs_set_hook(MPS3_MMCM_DRP_BASE, drp_hook, 0);
    mock_regs_poke(MPS3_MMCM_DRP_BASE, MMCM_DRP_CFG_REG0, (20u << 8) | 1u);
    mock_regs_poke(MPS3_MMCM_DRP_BASE, MMCM_DRP_CFG_REG2, 20u);
    mock_regs_poke(MPS3_CLKRST_BASE, CLKRST_STATUS, CLKRST_STATUS_MMCM_LOCKED);
    s_loads = 0;
}

static long stats_dut_mhz(void)
{
    char out[MPS3_CTRL_RESP_MAX];
    const char *line = "{\"op\":\"stats\"}";
    CHECK(coordinator_dispatch_line(line, (int)strlen(line), out, sizeof(out)) == 1);
    const char *p = strstr(out, "\"dut_mhz\":");
    assert(p != NULL);
    return strtol(p + 10, NULL, 10);
}

static void set_clk(const char *preset)
{
    char line[64], out[MPS3_CTRL_RESP_MAX];
    snprintf(line, sizeof(line), "{\"op\":\"set_clk\",\"preset\":\"%s\"}", preset);
    CHECK(coordinator_dispatch_line(line, (int)strlen(line), out, sizeof(out)) == 1);
    CHECK(strstr(out, "\"ok\":true") != NULL);
}

/* ---- the finding: set_clk 100mhz, kill -9, respawn, stats ---------------- */
static void test_respawn_keeps_the_running_clock(void)
{
    power_on();
    process_start();
    CHECK(stats_dut_mhz() == 50);
    set_clk("100mhz");
    CHECK(stats_dut_mhz() == 100);
    CHECK(mock_regs_peek(MPS3_MMCM_DRP_BASE, MMCM_DRP_CFG_REG2) == 10u);   /* the fabric runs 100 */

    process_start();                      /* init respawns harnessd: same fabric */
#ifdef TEST_BAREMETAL_PROVIDER
    CHECK(stats_dut_mhz() == 50);         /* THE BUG: the RAM shadow restarted at 50 */
#else
    CHECK(stats_dut_mhz() == 100);        /* read back from clk_wiz_dut */
    set_clk("25mhz");
    process_start();
    CHECK(stats_dut_mhz() == 25);
#endif
}

#ifndef TEST_BAREMETAL_PROVIDER
/* ---- clkrst_read_mhz: the decode ----------------------------------------- */
static void test_register_file_decode(void)
{
    uint32_t mhz = 0;
    power_on();
    for (int i = 0; i < clkrst_preset_table_len; i++) {
        const clkrst_preset_t *p = &clkrst_preset_table[i];
        mock_regs_poke(MPS3_MMCM_DRP_BASE, MMCM_DRP_CFG_REG0, ((uint32_t)p->mult << 8) | p->divclk);
        mock_regs_poke(MPS3_MMCM_DRP_BASE, MMCM_DRP_CFG_REG2, p->clkout0_div);
        CHECK(clkrst_read_mhz(&mhz) == 0);
        CHECK(mhz == (uint32_t)atoi(p->name));                /* "25mhz" -> 25 ... */
    }
    /* fractional fields (thousandths) and the IP's frac-enable bits 26/18:
     * 50 * 20.500 / (1 * 10) = 102.5 -> 103; 50 * 24 / (1 * 12.125) = 98.97 -> 99 */
    mock_regs_poke(MPS3_MMCM_DRP_BASE, MMCM_DRP_CFG_REG0, (1u << 26) | (500u << 16) | (20u << 8) | 1u);
    mock_regs_poke(MPS3_MMCM_DRP_BASE, MMCM_DRP_CFG_REG2, 10u);
    CHECK(clkrst_read_mhz(&mhz) == 0 && mhz == 103);
    mock_regs_poke(MPS3_MMCM_DRP_BASE, MMCM_DRP_CFG_REG0, (24u << 8) | 1u);
    mock_regs_poke(MPS3_MMCM_DRP_BASE, MMCM_DRP_CFG_REG2, (1u << 18) | (125u << 8) | 12u);
    CHECK(clkrst_read_mhz(&mhz) == 0 && mhz == 99);
    /* words that describe no clock: refused, *out untouched */
    mhz = 7;
    mock_regs_poke(MPS3_MMCM_DRP_BASE, MMCM_DRP_CFG_REG0, 0u);
    CHECK(clkrst_read_mhz(&mhz) == -1 && mhz == 7);
    mock_regs_poke(MPS3_MMCM_DRP_BASE, MMCM_DRP_CFG_REG0, (20u << 8) | 1u);
    mock_regs_poke(MPS3_MMCM_DRP_BASE, MMCM_DRP_CFG_REG2, 0u);
    CHECK(clkrst_read_mhz(&mhz) == -1);
    mock_regs_poke(MPS3_MMCM_DRP_BASE, MMCM_DRP_CFG_REG2, (1000u << 8) | 10u);   /* frac > 999 */
    CHECK(clkrst_read_mhz(&mhz) == -1);
    mock_regs_poke(MPS3_MMCM_DRP_BASE, MMCM_DRP_CFG_REG0, (1u << 8) | 255u);     /* 0.2 MHz */
    mock_regs_poke(MPS3_MMCM_DRP_BASE, MMCM_DRP_CFG_REG2, 255u);
    CHECK(clkrst_read_mhz(&mhz) == -1);
}

/* A blank register file (never on a built fabric) falls back to the shadow. */
static void test_blank_register_file_falls_back(void)
{
    power_on();
    mock_regs_poke(MPS3_MMCM_DRP_BASE, MMCM_DRP_CFG_REG0, 0u);
    mock_regs_poke(MPS3_MMCM_DRP_BASE, MMCM_DRP_CFG_REG2, 0u);
    process_start();
    CHECK(stats_dut_mhz() == 50);
    CHECK(harnessd_dut_clk_boot_sync(1) == HARNESSD_CLK_BLANK);
    CHECK(s_loads == 0);                                        /* garbage is never LOADed */
    CHECK(strstr(s_log, "describes no clock") != NULL);
}

/* ---- the boot re-sync ------------------------------------------------------ */
static void test_boot_sync(void)
{
    /* A POR / watchdog reset after `set_clk 100mhz`: peripheral_aresetn put the
     * register file back at the IP's 50 MHz and RESET_CTRL at 0; the MMCM kept
     * 100 (not modelled here -- what matters is that it is LOADed). */
    power_on();
    s_log[0] = '\0';
    CHECK(harnessd_dut_clk_boot_sync(1) == HARNESSD_CLK_LOADED);
    CHECK(s_loads == 1 && s_load_val == (MMCM_DRP_LOAD_LOAD | MMCM_DRP_LOAD_SEN));   /* SADDR=1 */
    CHECK(mock_regs_peek(MPS3_MMCM_DRP_BASE, MMCM_DRP_LOAD) == 0u);                  /* LOAD dropped */
    CHECK(strstr(s_log, "(50 MHz) re-LOADed into the MMCM, locked") != NULL);
    process_start();
    CHECK(stats_dut_mhz() == 50);

    /* A respawn never touches the MMCM. */
    set_clk("100mhz");
    s_loads = 0;
    CHECK(harnessd_dut_clk_boot_sync(0) == HARNESSD_CLK_RESPAWN);
    CHECK(s_loads == 0);

    /* A boot start that finds any DUT-domain reset released (a Linux-only
     * reboot of a running board) leaves a running DUT's clock alone. */
    static const uint32_t released[] = {
        CLKRST_RESET_CTRL_DUT_RESETN, CLKRST_RESET_CTRL_RP_RESETN, CLKRST_RESET_CTRL_DBG_RESETN,
    };
    for (unsigned i = 0; i < 3; i++) {
        mock_regs_poke(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL, released[i]);
        CHECK(harnessd_dut_clk_boot_sync(1) == HARNESSD_CLK_LIVE);
    }
    CHECK(s_loads == 0);

    /* No relock within the bound: reported, not hidden. */
    mock_regs_poke(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL, 0u);
    mock_regs_poke(MPS3_CLKRST_BASE, CLKRST_STATUS, 0u);
    s_log[0] = '\0';
    CHECK(harnessd_dut_clk_boot_sync(1) == HARNESSD_CLK_UNLOCKED);
    CHECK(strstr(s_log, "NOT LOCKED") != NULL);
}
#endif

int main(void)
{
    test_respawn_keeps_the_running_clock();
#ifdef TEST_BAREMETAL_PROVIDER
    printf("test_dut_clk_baremetal: %d checks passed (the weak shadow: a respawn after "
           "set_clk 100mhz reads 50 -- the bug the provider fixes)\n", s_checks);
#else
    test_register_file_decode();
    test_blank_register_file_falls_back();
    test_boot_sync();
    printf("test_dut_clk: %d checks passed\n", s_checks);
#endif
    return 0;
}
