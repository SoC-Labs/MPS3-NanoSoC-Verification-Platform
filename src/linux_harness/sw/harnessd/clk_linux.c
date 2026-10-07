/*
 * clk_linux.c — the DUT clock under Linux (ILA-mint finding #12,
 * FINDINGS_TRIAGE.md row 12; HARNESSD_CONTRACT.md §5.4 / §6).
 *
 * THE BUG THIS FIXES. coordinator.c answers `stats.dut_mhz` from s_dut_mhz, a
 * RAM shadow of the last `set_clk`, seeded at the BD's 50 MHz on every start.
 * On bare metal a "start" is a board or processor reset, so the shadow and the
 * MMCM are reset together (modulo the caveat below). harnessd is a PROCESS: after
 * `set_clk 100mhz` then `kill -9`, init respawns it, the shadow says 50 and the
 * MMCM, which nobody touched, still runs 100.
 *
 * THE FIX: the strong mps3_dut_clk_mhz() below answers from clk_wiz_dut's own
 * register file (clkrst_read_mhz: CFG_REG0/CFG_REG2, 50 MHz * M / (D * O)).
 * That file is fabric state -- it outlives the process that wrote it -- and it
 * is what the MMCM was last LOADed from, because clkrst's only writer (the
 * `set_clk` sequence) always LOADs what it writes.
 *
 * AND WHY A BOOT RE-SYNC IS NEEDED (vendor HDL, clk_wiz_v6_0: clk_core_drp's
 * DATA_WR_PROCESS + mmcm_pll_drp's RESTART state). clk_wiz_dut's only reset is
 * s_axi_aresetn = proc_sys_reset_shell/peripheral_aresetn, which the POR button
 * AND the shell watchdog both pulse (shell_bd.tcl aux_reset_in; on the MBV
 * SHELL_CONTRACT §6 "WDOG ... every shell AXI peripheral"). That reset:
 *   - returns the register file to the IP's configured 50 MHz values;
 *   - restarts the DRP state machine, which pulses the MMCM's RST and waits for
 *     lock -- it does NOT rewrite the MMCM. The MMCM's DRP-written M/D/O are
 *     not restored by RST (UG572/XAPP888: only a device configuration restores
 *     the initial attributes), so it re-locks at the PRE-reset preset.
 * So after `set_clk 100mhz` and a watchdog reset the register file reads 50 and
 * the DUT runs 100: the read-back would lie exactly as the shadow does (and bare
 * metal's shadow does too -- it is frozen at v0.11; recorded, not changed).
 * harnessd_dut_clk_boot_sync() closes it: on the FIRST start after an OS boot,
 * while every DUT-domain reset is still asserted (RESET_CTRL == 0: nothing on
 * dut_clk is running), it LOADs the register file as it stands into the MMCM.
 * After any fabric reset that is the IP default, so the DUT clock is back at
 * 50 MHz and dut_mhz reads 50 and is 50. After a power cycle / MCC reboot (a
 * device configuration) it is a no-op re-lock. A respawn never touches the
 * MMCM, and neither does a first start that finds a DUT reset released (a
 * Linux-only reboot of a running board: no fabric reset happened, so the
 * register file already describes the MMCM). UNPROVEN ON SILICON: B1 checks it
 * (`set_clk 100mhz`, WDOG reset, then stats + a DUT-side period measurement).
 */
#include <inttypes.h>

#include "../../../../firmware/clkrst/clkrst.h"
#include "../../../../firmware/common/platform_regs.h"
#include "../../../../firmware/coordinator/coordinator.h"
#include "harnessd.h"

/* coordinator.h's seam, the strong half. Falls back to what this process
 * programmed only when the register file cannot describe a clock (never on a
 * built fabric; a blank mock page). */
uint32_t mps3_dut_clk_mhz(uint32_t programmed_mhz)
{
    uint32_t mhz = 0u;
    return (clkrst_read_mhz(&mhz) == 0) ? mhz : programmed_mhz;
}

int harnessd_dut_clk_boot_sync(int boot_start)
{
    uint32_t mhz = 0u;
    int have = (clkrst_read_mhz(&mhz) == 0);

    if (!boot_start) {
        harnessd_log("clock: respawn -- the MMCM is not touched; dut_mhz is read back "
                     "from clk_wiz_dut (%s%" PRIu32 " MHz)\n", have ? "" : "unreadable, ",
                     have ? mhz : 0u);
        return HARNESSD_CLK_RESPAWN;
    }
    uint32_t rst = mps3_reg_read32(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL) &
                   (CLKRST_RESET_CTRL_DUT_RESETN | CLKRST_RESET_CTRL_RP_RESETN |
                    CLKRST_RESET_CTRL_DBG_RESETN);
    if (rst != 0u) {
        harnessd_log("clock: first start with DUT-domain resets RELEASED (RESET_CTRL "
                     "0x%" PRIx32 ": no fabric reset happened) -- the MMCM is not "
                     "re-loaded under a running DUT; dut_mhz = the register file "
                     "(%" PRIu32 " MHz)\n", rst, have ? mhz : 0u);
        return HARNESSD_CLK_LIVE;
    }
    if (!have) {
        harnessd_log("clock: clk_wiz_dut's register file describes no clock (CFG0 0x%08"
                     PRIx32 " CFG2 0x%08" PRIx32 ") -- NOT loaded into the MMCM\n",
                     mps3_reg_read32(MPS3_MMCM_DRP_BASE, MMCM_DRP_CFG_REG0),
                     mps3_reg_read32(MPS3_MMCM_DRP_BASE, MMCM_DRP_CFG_REG2));
        return HARNESSD_CLK_BLANK;
    }
    int locked = clkrst_reload();
    harnessd_log("clock: first start, DUT held in reset -- clk_wiz_dut's register file "
                 "(%" PRIu32 " MHz) re-LOADed into the MMCM, %s (a POR/WDOG reset resets "
                 "the register file but not the MMCM's DRP preset)\n", mhz,
                 locked ? "locked" : "NOT LOCKED within the poll bound");
    return locked ? HARNESSD_CLK_LOADED : HARNESSD_CLK_UNLOCKED;
}
