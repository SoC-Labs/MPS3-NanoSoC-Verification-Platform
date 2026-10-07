/*
 * clkrst.h — DUT clock (DRP) + 3-reset helpers.
 * See clkrst/README.md and shell-regmap.md's CLKRST block.
 */
#ifndef MPS3_CLKRST_H
#define MPS3_CLKRST_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Firmware-side preset table. `name`/`id` map a net-protocol.md set_clk
 * preset string to the (still-placeholder, A1/A6-pending) CLKRST_DUT_CLK_SEL
 * id. The three clk_wiz fields are REAL and load-bearing: they are the MMCM
 * multiply/divide/output-divide the DUT-clock Clocking Wizard is reprogrammed
 * with over its AXI4-Lite DRP (MMCM_DRP block, platform_regs.h). Input is the
 * 50 MHz osc_clk_50m (shell_bd.tcl clk_wiz_dut PRIM_IN_FREQ 50.000); with
 * mult/divclk = 20/1 the VCO is a fixed 1000 MHz (in the KU115 MMCM range for
 * every speed grade), and clkout0_div then sets the DUT frequency. Confirm the
 * exact M/D/O the built clk_wiz accepts against its generated header at BSP
 * time (same caveat as MMCM_DRP's offsets). */
typedef struct {
    const char *name;         /* matches net-protocol.md set_clk.preset strings */
    uint8_t     id;            /* value written to CLKRST_DUT_CLK_SEL (regmap-compat) */
    uint8_t     divclk;        /* MMCM DIVCLK_DIVIDE (D)   -> MMCM_DRP CFG_REG0[7:0] */
    uint8_t     mult;          /* MMCM CLKFBOUT_MULT (M)   -> MMCM_DRP CFG_REG0[15:8] */
    uint8_t     clkout0_div;   /* MMCM CLKOUT0_DIVIDE (O)  -> MMCM_DRP CFG_REG2[7:0] */
} clkrst_preset_t;

extern const clkrst_preset_t clkrst_preset_table[];
extern const int clkrst_preset_table_len;

void clkrst_init(void);

/* Assert, HOLD, then release the given RESET_CTRL bit
 * (CLKRST_RESET_CTRL_DUT_RESETN / _RP_RESETN / _DBG_RESETN), i.e. a reset
 * pulse. The hold is now calibrated (was a bare assert->release with no gap):
 * it spins on the AXI-timer millisecond timebase (mps3_sys_now_ms) long enough
 * for the async assert to be captured by the destination (dut_clk) 3-FF reset
 * synchronizer even at the slowest supported DUT clock — see clkrst.c for the
 * figure + basis. */
void clkrst_pulse_reset(uint32_t reset_ctrl_bit_mask);

/* Assert (0) or release (1) a reset bit without an automatic pulse-back --
 * used by swap_fsm (rp_resetn held across the whole swap) and swd_server
 * (dbg_resetn tracks OpenOCD's srst level, not a pulse). */
void clkrst_set_reset(uint32_t reset_ctrl_bit_mask, int released);

/* Looks up `preset_name` in clkrst_preset_table, records the id in
 * CLKRST_DUT_CLK_SEL (regmap-compat), reprograms the DUT-clock MMCM to that
 * preset's multiply/divide/output-divide over the clk_wiz AXI4-Lite DRP
 * (MMCM_DRP block), and bounded-polls CLKRST_STATUS.mmcm_locked for the
 * post-reconfig relock. Returns 1 if locked, 0 if not (yet) locked, <0 if
 * preset_name is unknown. */
int clkrst_set_preset(const char *preset_name);

/* The DUT-clock MMCM's input: osc_clk_50m (shell_bd.tcl clk_wiz_dut
 * PRIM_IN_FREQ 50.000). Every M/D/O above is relative to it. */
#define CLKRST_DUT_CLK_IN_MHZ 50u

/* READ BACK the DUT clock from clk_wiz_dut's own register file (MMCM_DRP
 * CFG_REG0 = {frac_en, CLKFBOUT_FRAC, CLKFBOUT_MULT, DIVCLK_DIVIDE}, CFG_REG2 =
 * {frac_en, CLKOUT0_FRAC, CLKOUT0_DIVIDE}; fractions in thousandths, PG065):
 * 50 MHz * M / (D * O), rounded to whole MHz, into *mhz_out. Returns 0, or -1
 * (and *mhz_out untouched) when the words cannot describe a clock (a zero
 * divider, a fraction > 999, a result that rounds to 0 MHz).
 *
 * What it is and is not (vendor HDL, clk_wiz_v6_0 clk_core_drp): the register
 * file is FABRIC state -- it outlives any process that wrote it, so after a
 * harnessd respawn it still says what `set_clk` last LOADed. It is the MMCM's
 * configuration only while every write has been followed by a LOAD (clkrst's
 * own sequence always does), and an AXI reset of the block (peripheral_aresetn:
 * the POR button or the shell watchdog) returns the register file to the IP's
 * 50 MHz defaults while the MMCM keeps its DRP-written M/D/O (the reset only
 * restarts the DRP FSM and re-locks). clkrst_reload() below is how a caller
 * makes the two agree again. Not called by the bare-metal image. */
int clkrst_read_mhz(uint32_t *mhz_out);

/* LOAD the register file as it stands into the MMCM (LOAD|SEN: SADDR=1 selects
 * the register-file state, not the IP's static one), then bounded-poll the
 * relock exactly as clkrst_set_preset() does. The DUT clock stops for the
 * reconfiguration (~the relock time): call it only while nothing on dut_clk is
 * out of reset. Returns 1 locked, 0 not (yet) locked. Not called by the
 * bare-metal image. */
int clkrst_reload(void);

/* Arbitrary-frequency path via the MMCM DRP window. TODO(A3): the 16-bit
 * DUT_CLK_DRP packing (single word vs multi-write address+data sequence)
 * is unspecified -- see clkrst/README.md; signature here is a placeholder
 * for "however that sequencing ends up shaped." */
int clkrst_set_drp(uint16_t drp_word);

#ifdef __cplusplus
}
#endif

#endif /* MPS3_CLKRST_H */
