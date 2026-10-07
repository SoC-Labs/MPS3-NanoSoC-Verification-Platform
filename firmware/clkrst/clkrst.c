/*
 * clkrst.c — DUT clock (MMCM DRP) + 3-reset helpers.
 * See clkrst/README.md and shell-regmap.md's CLKRST + MMCM_DRP blocks.
 *
 * The DUT clock is retuned over the clk_wiz_dut AXI4-Lite DRP (MMCM_DRP block,
 * 0x44AB_0000): CLKRST.DUT_CLK_SEL is an inert scratch register on the built
 * fabric (dut_clkrst.sv stores it but the 2024.1 clk_wiz exposes no native DRP
 * pins — its only reconfiguration path is that AXI-Lite slave). Lock is still
 * reported by CLKRST.STATUS.mmcm_locked, which shell_bd.tcl wires to
 * clk_wiz_dut/locked.
 */
#include <string.h>
#include "clkrst.h"
#include "../common/platform_regs.h"
#include "../common/timebase.h"   /* mps3_sys_now_ms — reset-hold timing */

/* Preset table. name/id are the (A1/A6-pending) CLKRST_DUT_CLK_SEL mapping;
 * the clk_wiz M/D/O columns are the REAL retune values (see clkrst.h). Input
 * is 50 MHz; mult/divclk = 20/1 => a fixed 1000 MHz VCO for all three presets,
 * and clkout0_div sets the output: 40->25 MHz, 20->50 MHz, 10->100 MHz. */
const clkrst_preset_t clkrst_preset_table[] = {
    /* name       id   D   M    O */
    { "25mhz",   0,   1,  20,  40 },
    { "50mhz",   1,   1,  20,  20 },
    { "100mhz",  2,   1,  20,  10 },
};
const int clkrst_preset_table_len =
    (int)(sizeof(clkrst_preset_table) / sizeof(clkrst_preset_table[0]));

/* Bounded relock poll: after a DRP LOAD the MMCM drops LOCKED and re-asserts it
 * once the new VCO settles (typ. << 1 ms). Each iteration is one AXI read
 * (~100-200 ns on the 100 MHz shell clock), so this bound covers a very
 * generous ~10-20 ms of relock before giving up and reporting not-locked. */
#define CLKRST_MMCM_LOCK_POLL_MAX 100000u

/* Reset assert->release hold. clkrst_pulse_reset() used to assert and release
 * with NO gap: back-to-back AXI writes keep RESET_CTRL low for only a few shell
 * clocks, which can be too short for the destination (dut_clk) 3-FF reset
 * synchronizer to capture the pulse when dut_clk is slow. 1 ms of wall-clock
 * hold is >> the 3 dut_clk edges the sync chain needs even at the slowest
 * preset (25 MHz => 120 ns) with orders-of-magnitude margin, and is
 * imperceptible for a host-issued `reset`. Measured on the AXI-timer timebase
 * (mps3_sys_now_ms), so it is a real wall-clock figure rather than an
 * instruction-count guess against an assumed MicroBlaze clock. */
#define CLKRST_RESET_HOLD_MS 1u
/* Fallback spin cap so the hold cannot wedge if the timebase is not ticking
 * (host-mock time is static; a pre-timer-init call would be too). On the real
 * target the timer is running before any reset verb is served, so the timebase
 * path exits first and this is pure belt-and-suspenders. */
#define CLKRST_RESET_HOLD_GUARD 2000000u

void clkrst_init(void)
{
    /* Establish a known reset state rather than trusting the fabric POR value:
     * hold all three DUT-domain resets ASSERTED (RESET_CTRL = 0 => none
     * released). dut_clkrst.sv already powers up this way (reset_ctrl_q resets
     * to 3'b000), but writing it explicitly means firmware does not depend on
     * that — the DUT and RP stay in reset until their owners release them
     * deliberately: rp_resetn by overlay_store_boot_load_default() once the
     * boot default is streamed into the RP, dut_resetn by the `reset` verb
     * (clkrst_pulse_reset), dbg_resetn by swd_server tracking OpenOCD srst. */
    mps3_reg_write32(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL, 0u);
}

void clkrst_set_reset(uint32_t reset_ctrl_bit_mask, int released)
{
    if (released) {
        mps3_reg_set_bits32(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL, reset_ctrl_bit_mask);
    } else {
        mps3_reg_clr_bits32(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL, reset_ctrl_bit_mask);
    }
}

/* Calibrated hold between assert and release — see CLKRST_RESET_HOLD_MS. */
static void clkrst_reset_hold(void)
{
    uint32_t start = mps3_sys_now_ms();
    uint32_t guard = 0;
    /* Wrap-safe: compare the SIGNED difference (timebase.h wrap rule). */
    while ((int32_t)(mps3_sys_now_ms() - start) < (int32_t)CLKRST_RESET_HOLD_MS) {
        if (++guard >= CLKRST_RESET_HOLD_GUARD) {
            break; /* timebase not advancing — bail rather than spin forever */
        }
    }
}

void clkrst_pulse_reset(uint32_t reset_ctrl_bit_mask)
{
    clkrst_set_reset(reset_ctrl_bit_mask, 0); /* assert (clear = not released) */
    clkrst_reset_hold();                      /* hold long enough to propagate */
    clkrst_set_reset(reset_ctrl_bit_mask, 1); /* release */
}

/* Bounded poll of CLKRST.STATUS.mmcm_locked (== clk_wiz_dut/locked). Returns 1
 * as soon as the MMCM is locked, else 0 at the poll bound. */
static int clkrst_poll_locked(void)
{
    for (uint32_t i = 0; i < CLKRST_MMCM_LOCK_POLL_MAX; i++) {
        if (mps3_reg_read32(MPS3_CLKRST_BASE, CLKRST_STATUS) & CLKRST_STATUS_MMCM_LOCKED) {
            return 1;
        }
    }
    return 0;
}

/* Reprogram the DUT-clock MMCM to (D, M, O) over the clk_wiz AXI4-Lite DRP.
 * PG065 dynamic-reconfig sequence (see the MMCM_DRP block in platform_regs.h):
 * write the config registers, LOAD to latch+apply, then poll for relock. No
 * SW_RESET is issued — a full soft-reset would needlessly glitch the DUT clock
 * when only M/D/O change. Integer configs only (fractional fields left 0).
 * Returns 1 if locked, 0 if it did not relock within the poll bound. */
static int clkrst_mmcm_drp_apply(uint32_t divclk, uint32_t mult, uint32_t clkout0_div)
{
    mps3_reg_write32(MPS3_MMCM_DRP_BASE, MMCM_DRP_CFG_REG0,
                     ((mult & 0xFFu) << 8) | (divclk & 0xFFu));
    mps3_reg_write32(MPS3_MMCM_DRP_BASE, MMCM_DRP_CFG_REG2,
                     clkout0_div & 0xFFu);
    /* Latch + apply. The MMCM drops LOCKED here and re-asserts it once the new
     * VCO settles; clkrst_poll_locked() waits that out. */
    mps3_reg_write32(MPS3_MMCM_DRP_BASE, MMCM_DRP_LOAD,
                     MMCM_DRP_LOAD_LOAD | MMCM_DRP_LOAD_SEN);
    mps3_reg_write32(MPS3_MMCM_DRP_BASE, MMCM_DRP_LOAD, 0u);
    return clkrst_poll_locked();
}

int clkrst_set_preset(const char *preset_name)
{
    /* Real lookup: an unknown preset must fail (<0) so the coordinator rejects
     * the set_clk request instead of silently programming some other clock. */
    const clkrst_preset_t *p = 0;
    for (int i = 0; i < clkrst_preset_table_len; i++) {
        if (strcmp(preset_name, clkrst_preset_table[i].name) == 0) {
            p = &clkrst_preset_table[i];
            break;
        }
    }
    if (p == 0) {
        return -1;
    }

    /* Record the preset id in CLKRST.DUT_CLK_SEL (regmap-compat: shell-regmap.md
     * keeps "quick preset selection" here). This register is inert on today's
     * fabric, so the ACTUAL retune is the DRP write below — but keep writing it
     * so intent is expressed and a future RTL that wires it up is fed correctly. */
    mps3_reg_write32(MPS3_CLKRST_BASE, CLKRST_DUT_CLK_SEL,
                     (uint32_t)p->id & CLKRST_DUT_CLK_SEL_MASK);

    /* Retune the DUT-clock MMCM for real, then report its (re)lock. */
    return clkrst_mmcm_drp_apply(p->divclk, p->mult, p->clkout0_div);
}

int clkrst_read_mhz(uint32_t *mhz_out)
{
    /* clkrst.h has the semantics. Field packing = the write side's
     * (clkrst_mmcm_drp_apply) plus the fractional fields the IP keeps beside
     * them; bits [26]/[18] (frac enable) are computed by the IP and ignored. */
    uint32_t r0 = mps3_reg_read32(MPS3_MMCM_DRP_BASE, MMCM_DRP_CFG_REG0);
    uint32_t r2 = mps3_reg_read32(MPS3_MMCM_DRP_BASE, MMCM_DRP_CFG_REG2);
    uint32_t d  = r0 & 0xFFu;
    uint32_t m  = (r0 >> 8) & 0xFFu;
    uint32_t mf = (r0 >> 16) & 0x3FFu;
    uint32_t o  = r2 & 0xFFu;
    uint32_t of = (r2 >> 8) & 0x3FFu;
    if (d == 0u || m == 0u || o == 0u || mf > 999u || of > 999u) {
        return -1;
    }
    /* kHz = 50 000 * (M*1000 + mf) / (D * (O*1000 + of)); the numerator needs
     * 34 bits at the field maxima, so 64-bit (integer only: no F/D on the MBV). */
    uint64_t num = (uint64_t)CLKRST_DUT_CLK_IN_MHZ * 1000u * (uint64_t)(m * 1000u + mf);
    uint64_t den = (uint64_t)d * (uint64_t)(o * 1000u + of);
    uint32_t mhz = (uint32_t)((num / den + 500u) / 1000u);
    if (mhz == 0u) {
        return -1;
    }
    *mhz_out = mhz;
    return 0;
}

int clkrst_reload(void)
{
    /* The LOAD half of clkrst_mmcm_drp_apply(), with no config write before it
     * (that function is left byte-for-byte as the bare-metal image built it). */
    mps3_reg_write32(MPS3_MMCM_DRP_BASE, MMCM_DRP_LOAD,
                     MMCM_DRP_LOAD_LOAD | MMCM_DRP_LOAD_SEN);
    mps3_reg_write32(MPS3_MMCM_DRP_BASE, MMCM_DRP_LOAD, 0u);
    return clkrst_poll_locked();
}

int clkrst_set_drp(uint16_t drp_word)
{
    /* Legacy raw-window path (CLKRST_DUT_CLK_DRP). Left as a placeholder: the
     * built clk_wiz has no native DRP pins, so this 16-bit CLKRST window is
     * inert — arbitrary-frequency retune should go through the clk_wiz AXI-Lite
     * DRP (see clkrst_mmcm_drp_apply) with a caller-computed M/D/O, not here.
     * No live caller today; kept for source compatibility. */
    mps3_reg_write32(MPS3_CLKRST_BASE, CLKRST_DUT_CLK_DRP,
                      (uint32_t)drp_word & CLKRST_DUT_CLK_DRP_MASK);
    return clkrst_poll_locked();
}
