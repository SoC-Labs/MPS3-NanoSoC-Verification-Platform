/*
 * touch.c -- cooperative, POLLED 4-wire resistive touchscreen driver (Phase 2).
 * See touch.h for the design contract and the bench-gated seams (MPS3_HAS_TOUCH
 * build flag + the fabric mint that instantiates the AXI IIC master).
 *
 * Layering (top to bottom):
 *   touch_poll()      -- TINT gate -> read -> pressure floor -> debounce/edge
 *                        -> affine+flip map -> clcd_hittest() dispatch.
 *   tsc_read_xyz()    -- TSC-specific: STMicro STMPE811QTR register/FIFO reads.
 *   iic_master_read() /
 *   iic_master_write()-- REAL AXI-IIC (PG090) dynamic-controller master ops,
 *                        polled + bounded, host-tested.
 *
 * TSC identity (was a guess, now confirmed -- see the STMPE811 section below):
 * the MPS3 CLCD is the Keil MCBQVGA-TS module whose touch is an ST STMPE811QTR
 * over I2C (Arm Corstone-SSE-300-for-MPS3 manual + MPS3 TRM; panel HX8347-D).
 *
 * Everything talks to the fabric through the mps3_reg_*32() HAL accessors, which
 * is what lets firmware/test/test_touch.c run these sequences against a mock IIC
 * with -DMPS3_HAL_MOCK.
 */
#include <stdint.h>

#include "touch.h"
#include "../clcd/clcd.h"             /* clcd_hittest + CLCD_ACT_* + geometry  */
#include "../common/platform_regs.h"  /* the four HAL accessors + MPS3_GPIO_*   */
#include "../common/service.h"        /* mps3_sys_now_us -- the IIC wait time bound */
#include "../common/timebase.h"       /* mps3_sys_now_ms -- the recovery back-off   */

/* Logical screen geometry, derived from the CLCD text grid (single source of
 * truth in clcd.h) so the flip can never drift from the renderer. */
#define TOUCH_SCREEN_W (CLCD_COLS * CLCD_GLYPH_W)   /* 320 */
#define TOUCH_SCREEN_H (CLCD_ROWS * CLCD_GLYPH_H)   /* 240 */

/* 180-degree rotation. REUSE the panel's own knob (default ON): the HX8347-D
 * ships MADCTL rotated 180 because the glass is mounted upside-down
 * (firmware/clcd/PANEL_PROVENANCE.md), so the displayed image is right-side up
 * to the reader. The resistive layer reads in the panel's PHYSICAL orientation,
 * so touch must apply the SAME flip to agree with what the reader sees. Build
 * -DCLCD_ROTATE_180=0 in lockstep with the panel if the glass is ever remounted. */
#ifndef CLCD_ROTATE_180
#define CLCD_ROTATE_180 1
#endif

/* Bounded poll count for every IIC busy/ready wait. The core completes a few-
 * byte transaction in tens of microseconds on silicon; the host mock completes
 * instantly. A wedged core fails closed (TOUCH_ERR_TIMEOUT), never spins. */
#define TOUCH_IIC_POLL_BOUND 100000

/* ==========================================================================
 * State
 * ========================================================================== */
static touch_calib_t s_cal;        /* current calibration                     */
static unsigned      s_down_streak;/* consecutive valid pen-down samples      */
static unsigned      s_err_streak;  /* consecutive tsc_read_xyz() bus failures */
static int           s_pen_down;   /* debounced pen state (edge tracking)     */
static uint16_t      s_last_x, s_last_y;  /* last dispatched pixel (diag/test) */
static uint16_t      s_chip_id;    /* STMPE811 CHIP_ID read at init (0=absent) */
static int           s_tsc_ready;  /* stmpe811_init() confirmed the part -- the
                                    * master gate: when 0, touch_poll() does NO
                                    * I2C at all, so an absent/wedged TSC can
                                    * never starve the superloop (lwIP+display). */

/* ---- bus-loss recovery (touch.h "BUS-LOSS RECOVERY") ----------------------
 * s_bus_latched separates "the bus was lost after a good init" (retry) from
 * "the part never came up at init" (stay inert, exactly as before: an absent
 * TSC or a shell without the IIC block is not something to poll for). */
static int      s_bus_latched;      /* latch fired; recovery attempts armed     */
static unsigned s_rec_step;         /* 0 = waiting out the back-off; 1..N = the
                                     * next STMPE_ARM_SEQ entry to write (1-based);
                                     * N+1 = the TSC_CTRL read-back               */
static unsigned s_rec_tries;        /* attempts in this episode (the back-off)  */
static uint32_t s_rec_last_ms;      /* latch time, then each attempt's start    */
static uint32_t s_rec_ok_ms;        /* when the last recovery succeeded         */
static int      s_rec_settling;     /* an Arm 10 ms settle is being waited out  */
static uint32_t s_rec_settle_us;    /* ... since this mps3_sys_now_us()         */

/* ---- bring-up telemetry (JTAG-readable via mb-nm + xsdb mrd) --------------
 * Written only at init and in the poll path; costs a few stores. Exists because
 * every init write below is best-effort: a NACKed TSC_CTRL leaves the part
 * un-enabled and the driver silently sees a permanent pen-up. */
static volatile uint32_t s_dbg_init_wr_fail;  /* init register writes that NACKed  */
static volatile uint32_t s_dbg_ctrl_after;    /* TSC_CTRL read back post-init      */
static volatile uint32_t s_dbg_poll_ok;       /* tsc_read_xyz() TSC_CTRL reads OK  */
static volatile uint32_t s_dbg_poll_err;      /* tsc_read_xyz() bus failures       */
static volatile uint32_t s_dbg_sta_seen;      /* polls with TSC_STA set (a touch)  */
static volatile uint32_t s_dbg_max_z;         /* largest Z ever read               */
static volatile uint32_t s_dbg_last_ctrl;     /* last raw TSC_CTRL byte seen       */
static volatile uint32_t s_dbg_bus_lost;      /* times the bus-loss latch fired    */
static volatile uint32_t s_dbg_rec_tries;     /* recovery attempts started         */
static volatile uint32_t s_dbg_recoveries;    /* recovery attempts that succeeded  */
static volatile uint32_t s_dbg_last_fifo;     /* last FIFO_SIZE byte seen          */
static volatile uint32_t s_dbg_fifo_empty;    /* TSC_STA set but FIFO_SIZE == 0    */
/* The last RAW converted sample and a count of them (touch_raw_sample(); the
 * `touch_cal raw` verb). Written in tsc_read_xyz() on every successful X/Y/Z
 * read, BEFORE the pressure floor, debounce or mapping -- a calibration capture
 * needs the raw ADC pair whatever the firmware later decided about it. */
static volatile uint32_t s_dbg_raw_x;
static volatile uint32_t s_dbg_raw_y;
static volatile uint32_t s_dbg_raw_z;
static volatile uint32_t s_dbg_raw_seen;

/* ---- panel-continuity probe results (touch.h: the four mailbox words) -----
 * volatile for the same reason the counters above are: on the target these are
 * read out of the ELF's statics over JTAG (mb-nm -S + xsdb mrd) as well as
 * being published into the diag mailbox by main.c. Written ONCE, at
 * touch_init(); never touched by touch_poll(). */
static volatile uint32_t s_probe_regs;     /* GPIO_AF|SYS_CTRL2|TSC_CFG|ADC_CTRL1 */
static volatile uint32_t s_probe_adc_x;    /* X+ | X- | TSC_I_DRIVE               */
static volatile uint32_t s_probe_adc_y;    /* Y+ | Y- | status byte               */
static volatile uint32_t s_probe_verdict;  /* TOUCH_VERDICT_*                     */

/* The default calibration: the 2026-09-24 silicon fit (touch.h documents where
 * it came from). shift=12, so each coefficient is "pixels per 4096 raw counts".
 * The panel's axes are swapped: x_cal is driven by ry (bx=377, ax only 21) and
 * y_cal by rx (ay=-281, by only -2). Worked example, the "hb |" spinner tap of
 * the capture, raw (3692,387): x_cal = (21*3692 + 377*387 - 158863) >> 12 = 15,
 * y_cal = (-281*3692 - 2*387 + 1091113) >> 12 = 12, then the 180 flip ->
 * (304,227), the nav row. */
static const touch_calib_t s_calib_default = TOUCH_CALIB_DEFAULT;  /* touch.h */

/* ==========================================================================
 * AXI IIC (PG090) low-level -- REAL + host-tested
 * ========================================================================== */
static uint32_t ird(uint32_t off)             { return mps3_reg_read32(MPS3_TOUCH_BASE, off); }
static void     iwr(uint32_t off, uint32_t v) { mps3_reg_write32(MPS3_TOUCH_BASE, off, v); }

/* Poll `off` until `mask` is (want_set ? set : clear), bounded. Fail closed.
 *
 * TWO bounds (touch.h TOUCH_IIC_WAIT_US). The iteration bound keeps the host
 * harness terminating when its fake clock does not move; the TIME bound is the
 * one that matters on silicon, where 100000 iterations was never a known
 * duration and a held touch measured 65-150 ms per clcd pass. Time is read with
 * the same microsecond clock the superloop measures itself with. */
static int iic_wait(uint32_t off, uint32_t mask, int want_set)
{
    const uint32_t t0 = mps3_sys_now_us();
    for (int i = 0; i < TOUCH_IIC_POLL_BOUND; i++) {
        uint32_t v = ird(off);
        if (want_set ? ((v & mask) != 0u) : ((v & mask) == 0u)) {
            return TOUCH_OK;
        }
        if ((uint32_t)(mps3_sys_now_us() - t0) >= (uint32_t)TOUCH_IIC_WAIT_US) {
            break;
        }
    }
    return TOUCH_ERR_TIMEOUT;
}

/*
 * Dynamic-controller master read: write `cmdlen` bytes (a command / register
 * pointer) to the 7-bit slave, then a REPEATED START read of `n` bytes into
 * `buf`. Bounded and non-blocking (no unbounded spin).
 *
 * TX-FIFO framing (PG090 dynamic controller):
 *   [START | addr<<1 | 0]   write-address byte (begins the transaction)
 *   [cmd0] .. [cmdN-1]      command / register-pointer bytes (no stop)
 *   [START | addr<<1 | 1]   repeated-start read-address byte
 *   [STOP  | n]             dynamic byte count; core clocks n bytes, NACKs the
 *                           last, issues STOP.
 *
 * RX_FIFO_PIRQ is set to n-1 first. Checked against PG090 (v2.0, 5 Oct 2016):
 * the NACK on the last byte does NOT come from this register -- it comes from
 * the dynamic STOP word, "If a read access is occurring on the IIC bus, use this
 * value as a receive byte counter. When this counter reaches zero, CR TXAK is
 * forced High" -- RX_FIFO_PIRQ only sets the receive-FIFO interrupt/throttle
 * compare value. n-1 is nevertheless the RIGHT number, because RX_FIFO_OCY
 * counts from zero ("A binary value of 1001 implies that 10 locations are full
 * in the FIFO"), so PIRQ = n-1 is "throttle once all n bytes have arrived" --
 * the same value Xilinx's own XIic_DynRecv() writes.
 *
 * The RX FIFO is drained first. PG090's "Pseudo Code for Dynamic IIC Accesses"
 * says outright: "For read accesses, you should reset the RX_FIFO or check that
 * SR(RX_FIFO_Empty) = 1", and CR has no RX-FIFO reset bit (Table 2-9: bit 1 is
 * TX_FIFO Reset only). Without this, one read that times out part-way through
 * its drain leaves bytes behind and EVERY later read is shifted by that many --
 * a register read silently returning a different register's value, with no
 * error anywhere. Bounded by the FIFO depth.
 */
static int iic_master_read(uint8_t addr7, const uint8_t *cmd, unsigned cmdlen,
                           uint8_t *buf, unsigned n)
{
    if (n == 0u || n > TOUCH_IIC_MAX_READ) {
        return TOUCH_ERR_PARAM;
    }

    /* Fresh transaction: flush any stale TX FIFO, keep the core enabled. */
    iwr(TOUCH_IIC_CR, IIC_CR_EN | IIC_CR_TX_FIFO_RESET);
    iwr(TOUCH_IIC_CR, IIC_CR_EN);

    /* ... and any stale RX bytes a previously aborted drain left behind (PG090,
     * above). 16 = the core's FIFO depth, so this is bounded by construction. */
    for (unsigned g = 0u; g < 16u; g++) {
        if (ird(TOUCH_IIC_SR) & IIC_SR_RX_FIFO_EMPTY) {
            break;
        }
        (void)ird(TOUCH_IIC_RX_FIFO);
    }

    /* Receive-FIFO compare value = n-1 (see the framing note above). */
    iwr(TOUCH_IIC_RX_FIFO_PIRQ, (uint32_t)(n - 1u));

    /* Never start onto a busy bus (another master mid-transfer). */
    if (iic_wait(TOUCH_IIC_SR, IIC_SR_BB, /*want_set=*/0) != TOUCH_OK) {
        return TOUCH_ERR_TIMEOUT;
    }

    /* Write phase (optional): start + write-address, then the command bytes. No
     * STOP -> a repeated start follows for the read phase. */
    if (cmdlen) {
        iwr(TOUCH_IIC_TX_FIFO, IIC_TX_START | (uint32_t)(addr7 << 1));   /* R/W = 0 */
        for (unsigned i = 0; i < cmdlen; i++) {
            iwr(TOUCH_IIC_TX_FIFO, (uint32_t)cmd[i]);
        }
    }

    /* Read phase: repeated start + read-address, then the dynamic STOP|count. */
    iwr(TOUCH_IIC_TX_FIFO, IIC_TX_START | (uint32_t)(addr7 << 1) | 1u); /* R/W = 1 */
    iwr(TOUCH_IIC_TX_FIFO, IIC_TX_STOP | (uint32_t)n);

    /* Drain exactly n bytes as they arrive; poll SR.rx_fifo_empty, bounded. */
    for (unsigned i = 0; i < n; i++) {
        if (iic_wait(TOUCH_IIC_SR, IIC_SR_RX_FIFO_EMPTY, /*want_set=*/0) != TOUCH_OK) {
            return TOUCH_ERR_TIMEOUT;
        }
        buf[i] = (uint8_t)(ird(TOUCH_IIC_RX_FIFO) & 0xFFu);
    }
    return TOUCH_OK;
}

/*
 * Dynamic-controller master WRITE of `len` bytes to a 7-bit slave (one STOP on
 * the last byte). The read helper never issues a write-only transaction, but the
 * STMPE811 needs ~11 register writes to bring up its ADC + touch controller
 * before any data read. A missing slave ACK is reported via ISR.TX_ERROR, so a
 * wrong address / absent part fails fast instead of spinning to the poll bound.
 */
static int iic_master_write(uint8_t addr7, const uint8_t *data, unsigned len)
{
    if (len == 0u) {
        return TOUCH_ERR_PARAM;
    }

    iwr(TOUCH_IIC_CR, IIC_CR_EN | IIC_CR_TX_FIFO_RESET);
    iwr(TOUCH_IIC_CR, IIC_CR_EN);
    iwr(TOUCH_IIC_ISR, ird(TOUCH_IIC_ISR));                 /* clear W1C, esp. TX_ERROR */

    if (iic_wait(TOUCH_IIC_SR, IIC_SR_BB, /*want_set=*/0) != TOUCH_OK) {
        return TOUCH_ERR_TIMEOUT;
    }

    /* START | addr<<1 | 0 (write), then the data; the LAST byte carries STOP. */
    iwr(TOUCH_IIC_TX_FIFO, IIC_TX_START | (uint32_t)(addr7 << 1));
    for (unsigned i = 0; i < len; i++) {
        uint32_t w = (uint32_t)data[i];
        if (i == len - 1u) {
            w |= IIC_TX_STOP;
        }
        iwr(TOUCH_IIC_TX_FIFO, w);
    }

    /* Completion: TX FIFO drained + bus released, then confirm the slave ACKed. */
    if (iic_wait(TOUCH_IIC_SR, IIC_SR_TX_FIFO_EMPTY, /*want_set=*/1) != TOUCH_OK) {
        return TOUCH_ERR_TIMEOUT;
    }
    if (iic_wait(TOUCH_IIC_SR, IIC_SR_BB, /*want_set=*/0) != TOUCH_OK) {
        return TOUCH_ERR_TIMEOUT;
    }
    if (ird(TOUCH_IIC_ISR) & IIC_ISR_TX_ERROR) {
        return TOUCH_ERR_TIMEOUT;               /* NACK: wrong addr / absent TSC */
    }
    return TOUCH_OK;
}

/* ==========================================================================
 * TSC-specific: STMicroelectronics STMPE811QTR (the ONLY TSC-specific code).
 * ==========================================================================
 * IDENTIFIED (not a guess): the MPS3 CLCD is the Keil MCBQVGA-TS module, whose
 * touch is an ST STMPE811QTR read over I2C -- named verbatim in Arm's Corstone
 * SSE-300-for-MPS3 manual and corroborated by the MPS3 TRM (module MCBQVGA-TS,
 * panel HX8347-D). 7-bit address 0x41 (A0=GND strap on the Keil board; 0x44 if
 * A0=VCC). Unlike a TSC2007 (command-per-conversion), the STMPE811 is register/
 * FIFO based and MUST be initialised (clocks + ADC + TSC enable) before any data
 * read -- which is exactly why the earlier TSC2007 shim read nothing on silicon.
 * Datasheet: ST DocID 14489 (STMPE811), Tables 6/15/16, sec.11 program sequence.
 * If touch_chip_id() != 0x0811 on the bench, the A0 strap is 0x44 (change
 * STMPE811_ADDR) or the bus/pull-ups are dead (see mps3_harness_touch.xdc).
 */
#define STMPE811_ADDR        0x41u   /* A0=GND (Keil MCBQVGA-TS); 0x44 if A0=VCC */
#define STMPE811_ID_EXPECT   0x0811u

/* Register map (subset used here). Addresses from the STMPE811 datasheet,
 * ST Doc ID 14489 Rev 5, Table 11 "Register summary map table" (pp. 19-21) --
 * and identical in Arm's own Touch_V2M-MPS3.c (see REFERENCE below). */
#define STMPE_CHIP_ID        0x00u   /* RO 16-bit: 0x0811                        */
#define STMPE_SYS_CTRL1      0x03u   /* [1] SOFT_RESET, [0] HIBERNATE            */
#define STMPE_SYS_CTRL2      0x04u   /* clock gating, 1 = OFF (reset 0x0F = all off) */
#define STMPE_INT_CTRL       0x09u   /* [0] GLOBAL_INT -- gates the INT PIN only */
#define STMPE_INT_EN         0x0Au   /* interrupt MASK: [0] TOUCH_DET [1] FIFO_TH [2] FIFO_OFLOW */
#define STMPE_INT_STA        0x0Bu   /* W1C interrupt status                     */
#define STMPE_ADC_CTRL1      0x20u   /* [6:4] SAMPLE_TIME [3] MOD_12B [1] REF_SEL */
#define STMPE_ADC_CTRL2      0x21u   /* [1:0] ADC clock speed                    */
#define STMPE_TSC_CTRL       0x40u   /* [7] TSC_STA (RO) [6:4] TRACK [3:1] OP_MOD [0] EN */
#define STMPE_TSC_CFG        0x41u   /* [7:6] AVE_CTRL [5:3] TOUCH_DET_DELAY [2:0] SETTLING */
#define STMPE_FIFO_TH        0x4Au   /* FIFO threshold -- "must not be set as zero" */
#define STMPE_FIFO_STA       0x4Bu   /* [0] FIFO_RESET                           */
#define STMPE_FIFO_SIZE      0x4Cu   /* RO: number of SAMPLES available          */
#define STMPE_TSC_FRACT_Z    0x56u   /* [2:0] pressure fixed-point format        */
#define STMPE_TSC_I_DRIVE    0x58u   /* [0] 0 = 20 mA, 1 = 50 mA panel drive     */
#define STMPE_TSC_SHIELD     0x59u   /* [3:0] "Write 1 to GND X+, X-, Y+, Y- lines" */
#define STMPE_TSC_DATA       0xD7u   /* combined X/Y/Z FIFO port, non-auto-inc   */

#define STMPE_TSC_STA        (1u << 7)  /* TSC_CTRL[7], READ-ONLY:
                                         * "Reads '1' when touch is detected;
                                         *  Reads '0' when touch is not detected"
                                         * (Doc ID 14489 Rev 5, p.37). NOTE the
                                         * documented RESET value of TSC_CTRL is
                                         * 0x90 -- i.e. this bit reads 1 with the
                                         * TSC DISABLED, so it is only meaningful
                                         * after TSC_CTRL.EN has been set. */

/* GPIO / ADC block (the panel-continuity probe's registers). */
#define STMPE_GPIO_SET_PIN   0x10u   /* W1S: drive a GPIO output high            */
#define STMPE_GPIO_CLR_PIN   0x11u   /* W1C: drive a GPIO output low             */
#define STMPE_GPIO_DIR       0x13u   /* "'0' sets ... input state, and '1' ... output" */
#define STMPE_GPIO_AF        0x17u   /* "'0' sets the corresponding pin to function
                                      * as touchscreen/ADC, and '1' sets it into
                                      * GPIO mode" (Doc ID 14489 Rev 5, p.53) --
                                      * i.e. the polarity is INVERTED from the name. */
#define STMPE_ADC_INT_STA    0x0Fu   /* per-channel end-of-conversion, ISA[x]    */
#define STMPE_ADC_CAPT       0x22u   /* W1 starts channel n. NOT a busy flag:
                                      * "Reads '1' if conversion is completed.
                                      *  Reads '0' if conversion is in progress"
                                      * AND its reset value is 0xFF -- so every
                                      * bit already reads "done" before anything
                                      * has been started, and writing 0 has no
                                      * effect, so it cannot be cleared either.
                                      * It is therefore USELESS as a completion
                                      * signal; ADC_INT_STA is used instead. */
#define STMPE_ADC_DATA_CH(c) ((uint8_t)(0x30u + 2u * (c)))  /* 2 bytes, 12-bit  */

/* ---- PIN vs ADC CHANNEL: two different numbering schemes ------------------
 * This distinction is the reason the 2026-09-14 panel-continuity probe read
 * nonsense and produced a false PANEL_OPEN verdict.
 *
 * GPIO bit number (GPIO_AF / GPIO_DIR / GPIO_SET_PIN / GPIO_CLR_PIN), from
 * Doc ID 14489 Rev 5 Table 2 "Pin assignments" (p.7):
 *     GPIO-4 = X+   GPIO-5 = Y+   GPIO-6 = X-   GPIO-7 = Y-
 *
 * ADC channel number (ADC_CAPT bits, ADC_INT_STA bits, ADC_DATA_CHn), from
 * Doc ID 14489 Rev 5 Table 13 "ADC controller register summary table" (p.30):
 *     CH0 = X+/GPIO-4   CH1 = X-/GPIO-6   CH2 = Y+/GPIO-5   CH3 = Y-/GPIO-7
 *     CH4 = IN0/GPIO-0  CH5 = IN1/GPIO-1  CH6 = IN2/GPIO-2  CH7 = IN3/GPIO-3
 *
 * The four TOUCH lines are ADC channels 0..3, NOT 4..7. The old code converted
 * channels 4..7, which are IN0..IN3 -- pins that are not connected to the panel
 * at all, and which the old GPIO_AF value of 0x0F had additionally muxed into
 * GPIO mode, so they were not even ADC inputs. Every number the probe reported
 * on silicon (X+ 1188, X- 1202, Y- 1068, Y+ 9) was a reading of the wrong pin.
 * Note also that CH1 is X- while GPIO-5 is Y+: the two orders are NOT the same
 * permutation, so "channel n == GPIO n" is wrong in a way that silently
 * relabels rather than obviously failing.
 * -------------------------------------------------------------------------- */
#define STMPE_GPIO_XP 4u
#define STMPE_GPIO_YP 5u
#define STMPE_GPIO_XM 6u
#define STMPE_GPIO_YM 7u

#define STMPE_ADC_CH_XP 0u
#define STMPE_ADC_CH_XM 1u
#define STMPE_ADC_CH_YP 2u
#define STMPE_ADC_CH_YM 3u

/* ---- THE INIT VALUES, once ------------------------------------------------
 * Each of these is written by stmpe811_init() AND is the expected value of the
 * matching read-back. One literal, two uses: a read-back gate whose expectation
 * was typed separately from the write is a gate that can agree with itself
 * while disagreeing with the chip.
 *
 * ============================== THE REFERENCE =============================
 * These values are Arm's, not ours. Arm's own MPS3 board-support package ships
 * a working STMPE811 driver for THIS panel, and on 2026-09-14 david confirmed
 * that Arm's default MPS3 image detects touches on THIS board -- so the panel,
 * the flex, the part and the I2C wiring are all proven good and the fault was
 * on our side. The reference is:
 *
 *   Keil.V2M-MPS3_IOTKit_BSP.1.0.2.pack (a ZIP; www.keil.com/pack/), file
 *   Boards/ARM/V2M-MPS3/Common/Touch_V2M-MPS3.c, (c) 2013-2017 ARM LIMITED,
 *   BSD-3-Clause. Its Touch_Initialize() writes, in order:
 *     SYS_CTRL1 0x02 / delay 10 ms / SYS_CTRL2 0x0C / INT_EN 0x07 /
 *     ADC_CTRL1 0x69 / delay 10 ms / ADC_CTRL2 0x01 / TSC_CFG 0xC2 /
 *     FIFO_TH 0x01 / FIFO_STA 0x01 / FIFO_STA 0x00 / TSC_FRACTION_Z 0x07 /
 *     TSC_I_DRIVE 0x01 / GPIO_AF 0x00 / TSC_CTRL 0x01 / INT_STA 0xFF
 *   (byte-for-byte identical to the MPS2 Touch_V2M-MPS2.c in the CMx BSP).
 *
 * Every value below is now Arm's, and every DEVIATION is named and reasoned.
 * Datasheet citations are ST Doc ID 14489 Rev 5 ("STMPE811", April 2011).
 * ========================================================================== */

/* SYS_CTRL2 -- clock gating, and the bits are 1 = OFF (reset 0x0F = everything
 * gated off). [3] TS_OFF (temperature sensor) [2] GPIO_OFF [1] TSC_OFF
 * [0] ADC_OFF (Rev 5 p.23).
 * DEVIATION from Arm's 0x0C: 0x00 is a strict SUPERSET -- Arm gates the
 * temperature sensor and the GPIO block off, we leave every clock running. The
 * two bits that decide whether a touch can be seen (TSC_OFF, ADC_OFF) are 0 in
 * both. We need GPIO_OFF = 0 because the panel-continuity probe below drives
 * the touch lines through the GPIO block; the cost is idle power, nothing else. */
#define STMPE_INIT_SYS_CTRL2  0x00u

/* INT_EN -- Arm's 0x07 = [2] FIFO_OFLOW | [1] FIFO_TH | [0] TOUCH_DET.
 * THE ONE FUNCTIONAL REGISTER OUR SEQUENCE NEVER WROTE. It matters even though
 * nothing here uses the INT pin, because the datasheet ties the part's
 * auto-hibernation wake-up to this mask: SYS_CTRL1[0] HIBERNATE's description
 * (p.23) says "If the hot-key feature is required, use the default
 * auto-hibernation mode" -- auto-hibernation is the DEFAULT -- and the
 * programming sequence step (q) (p.48) says "During the auto-hibernate mode, a
 * touch detection can cause a wake-up to the device only when the TSC is
 * enabled AND the touch detect status interrupt mask is enabled". With INT_EN
 * left at its reset 0x00 the touch-detect mask is DISABLED, which is a
 * documented path to exactly the observed silicon symptom: a healthy,
 * correctly-configured part whose TSC_STA never asserts under a real press.
 * INT_CTRL (0x09) stays unwritten, as in Arm's driver: its GLOBAL_INT bit gates
 * the INT PIN, and "Regardless of whether the INT_EN bits are enabled, the
 * INT_STA bits are still updated" (p.27), so polling needs no interrupt route. */
#define STMPE_INIT_INT_EN     0x07u

/* ADC_CTRL1 -- Arm's 0x69: [6:4] SAMPLE_TIME = 110 (124 clocks), [3] MOD_12B = 1
 * (12-bit), [1] REF_SEL = 0 (internal reference), plus bit 0, which the
 * datasheet marks RESERVED and Arm sets anyway. Was 0x48 (80 clocks), which is
 * ST's and Linux's value; Arm's longer sample time is the one proven against
 * this panel, and a longer sample suits a high-impedance resistive film.
 * Bit 2 is also RESERVED and reads back 1 (its reset value is 0x1C), which is
 * why the read-back below compares under a mask. */
#define STMPE_INIT_ADC_CTRL1  0x69u

/* ADC_CTRL2 -- [1:0] ADC_FREQ = 01 = 3.25 MHz typ. Arm, ST and Linux all agree. */
#define STMPE_INIT_ADC_CTRL2  0x01u

/* TSC_CFG -- Arm's 0xC2: [7:6] AVE_CTRL = 11 (8 samples averaged),
 * [5:3] TOUCH_DET_DELAY = 000 (10 us), [2:0] SETTLING = 010 (500 us).
 * Was 0x9A (4 samples / 500 us detect delay / 500 us settling), which is ST's
 * value for a different panel. Arm's is the one proven on this glass. */
#define STMPE_INIT_TSC_CFG    0xC2u

/* TSC_I_DRIVE -- [0] = 1 -> "50 mA typical, 80 mA max" panel drive. Unanimous
 * across Arm, ST and Linux. */
#define STMPE_INIT_I_DRIVE    0x01u

/* TSC_FRACTION_Z -- [2:0] = 111 -> "Fractional part is 7, whole part is 1".
 * Arm writes 0x07 and Linux's stm32f429-disco device tree asks for 7 as well;
 * ST's component driver writes 0x01 (1 fractional bit, 7 whole). We follow Arm.
 * This is NOT a free choice: the 8-bit Z byte is a fixed-point ratio, so with 7
 * fractional bits TOUCH_Z_MIN = 64 (touch.h) means "ratio >= 0.5", whereas with
 * ST's 0x01 the same 64 would mean "ratio >= 32" and would reject every real
 * press. If this value is ever changed, TOUCH_Z_MIN must move with it. */
#define STMPE_INIT_FRACT_Z    0x07u

/* GPIO_AF -- Arm writes 0x00: ALL EIGHT pins in alternate function. The
 * polarity is inverted from the register's name: "'0' sets the corresponding
 * pin to function as touchscreen/ADC, and '1' sets it into GPIO mode" (p.53).
 * Was 0x0F, which additionally put GPIO-3..0 (= IN0..IN3 = ADC channels 4..7)
 * into GPIO mode. That never affected the touch lines -- bits [7:4] were 0 in
 * both -- but it is a gratuitous divergence from the proven reference, and it
 * is what made the old panel-continuity probe read GPIO pins instead of ADC
 * inputs. Note the datasheet states this register's reset value TWICE and
 * inconsistently (0x00 in Table 11, 0x0F in the section 13 detail page, in both
 * Rev 2 and Rev 5), so it must always be written explicitly and never assumed. */
#define STMPE_INIT_GPIO_AF    0x00u

/* TSC_CTRL -- Arm's 0x01: TRACK = 000 (no window tracking), OP_MOD = 000
 * (X, Y, Z acquisition), EN = 1. We need OP_MOD = XYZ because touch_poll()'s
 * pressure floor uses Z; ST's 0x73 (X,Y only, tracking 127) would leave Z
 * undefined. OP_MOD "cannot be written on, when EN = 1" (p.37), so this single
 * write sets the mode and the enable together, which is what Arm does. */
#define STMPE_INIT_TSC_CTRL   0x01u

/* INT_STA write-1-to-clear mask used on EVERY poll, matching Arm's
 * Touch_GetState(), which ends each call with INT_STA = 0x1F. The status bits
 * are sticky, and step (h) of the programming sequence (p.48) explains that a
 * FIFO_TH flag re-asserts by itself while the FIFO is above threshold -- so a
 * driver that never clears them accumulates latched state the part's
 * hibernate/interrupt logic can sit on. 0x1F = FIFO_EMPTY|FIFO_FULL|
 * FIFO_OFLOW|FIFO_TH|TOUCH_DET; init still clears the full 0xFF once. */
#define STMPE_INT_STA_POLL_CLEAR 0x1Fu

/* One STMPE811 register write [reg][val]. */
static int stmpe_wr(uint8_t reg, uint8_t val)
{
    uint8_t b[2] = { reg, val };
    return iic_master_write(STMPE811_ADDR, b, 2u);
}

/* Read `n` bytes starting at STMPE811 register `reg` (pass 0xD7 for the
 * non-auto-increment FIFO port). Mirrors the existing register-read shape. */
static int stmpe_rd(uint8_t reg, uint8_t *buf, unsigned n)
{
    return iic_master_read(STMPE811_ADDR, &reg, 1u, buf, n);
}

/* Fill *x,*y (12-bit raw 0..4095) and *z (8-bit pressure) from one STMPE811
 * X/Y/Z sample. Contract (see touch.h / touch_poll): return RAW readings only --
 * no pixel map, no flip; any non-TOUCH_OK is treated as pen-up. When no touch is
 * present, report z=0 so the touch_poll() pressure floor rejects it. */
static int tsc_read_xyz(uint16_t *x, uint16_t *y, uint16_t *z)
{
    uint8_t ctrl;
    uint8_t fifo = 0u;
    uint8_t rx[4];

    /* Every pass ends by clearing the sticky interrupt status, exactly as Arm's
     * Touch_GetState() does -- see STMPE_INT_STA_POLL_CLEAR. Best-effort: a
     * NACK here is reported by the reads below, and must not itself be a
     * pen-down/pen-up verdict. */
    (void)stmpe_wr(STMPE_INT_STA, STMPE_INT_STA_POLL_CLEAR);

    /* Pen-detect: TSC_CTRL[7] TSC_STA, "Reads '1' when touch is detected"
     * (Rev 5 p.37). This is the source Arm's proven MPS3 driver polls. */
    if (stmpe_rd(STMPE_TSC_CTRL, &ctrl, 1u) != TOUCH_OK) {
        s_dbg_poll_err++;
        return TOUCH_ERR_TIMEOUT;
    }
    s_dbg_last_ctrl = (uint32_t)ctrl;
    s_dbg_poll_ok++;
    if (!(ctrl & STMPE_TSC_STA)) {
        *x = 0u; *y = 0u; *z = 0u;              /* no contact -> pen-up */
        return TOUCH_OK;
    }
    s_dbg_sta_seen++;   /* TSC_STA was asserted. Counted BEFORE the sample read,
                         * so sta_seen > 0 with max_z == 0 means "contact
                         * detected, sample never arrived" -- not "no contact". */

    /* FIFO GUARD, from Arm's Touch_GetState(): a set TSC_STA says the touch
     * DETECT comparator has fired, not that a converted sample exists. Arm reads
     * FIFO_SIZE and returns early when it is 0; the 0xD7 port would otherwise
     * hand back whatever was last in the FIFO. FIFO_SIZE is "current number of
     * samples available" (Rev 5 p.41), so this also separates the two states our
     * telemetry previously could not: "detect fired, no sample" (sta_seen climbs,
     * fifo_empty climbs, max_z stays 0) from "no contact at all". */
    if (stmpe_rd(STMPE_FIFO_SIZE, &fifo, 1u) != TOUCH_OK) {
        s_dbg_poll_err++;
        return TOUCH_ERR_TIMEOUT;
    }
    s_dbg_last_fifo = (uint32_t)fifo;
    if (fifo == 0u) {
        s_dbg_fifo_empty++;
        *x = 0u; *y = 0u; *z = 0u;              /* detect without data -> pen-up */
        return TOUCH_OK;
    }

    /* One X/Y/Z sample: 4 packed bytes from the non-auto-increment FIFO port.
     * Layout (datasheet Table 16, OP_MOD=000):
     *   rx[0]=X[11:4]  rx[1]={X[3:0],Y[11:8]}  rx[2]=Y[7:0]  rx[3]=Z[7:0]. */
    if (stmpe_rd(STMPE_TSC_DATA, rx, 4u) != TOUCH_OK) {
        s_dbg_poll_err++;   /* MUST count here too: without it a bus that wedges
                             * only on the 0xD7 FIFO port reads back as
                             * "poll_err 0" while the latch fires -- the witness
                             * would deny the very failure it exists to catch. */
        return TOUCH_ERR_TIMEOUT;
    }
    *x = (uint16_t)(((uint16_t)rx[0] << 4) | ((uint16_t)rx[1] >> 4));
    *y = (uint16_t)((((uint16_t)rx[1] & 0x0Fu) << 8) | (uint16_t)rx[2]);
    *z = (uint16_t)rx[3];
    if ((uint32_t)*z > s_dbg_max_z) { s_dbg_max_z = (uint32_t)*z; }
    s_dbg_raw_x = (uint32_t)*x;
    s_dbg_raw_y = (uint32_t)*y;
    s_dbg_raw_z = (uint32_t)*z;
    s_dbg_raw_seen++;

    /* FRESHNESS, bounded. Arm drains the whole FIFO and keeps the last sample
     * (`while (num--)`); that is up to 128 four-byte I2C reads in one call and
     * this driver's contract (touch.h) is that a poll is ONE bounded step which
     * never starves the superloop. So: take the one sample above, and if more
     * were queued, pulse FIFO_RESET instead of draining -- which is what ST's
     * own component driver does after every read, and what the programming
     * sequence step (s) (p.48) recommends. The next poll then sees fresh data
     * rather than a backlog. */
    if (fifo > 1u) {
        (void)stmpe_wr(STMPE_FIFO_STA, 0x01u);   /* "Resets FIFO. All data ... cleared" */
        (void)stmpe_wr(STMPE_FIFO_STA, 0x00u);   /* "FIFO put out of reset mode"        */
    }
    return TOUCH_OK;
}

/* ==========================================================================
 * PANEL-CONTINUITY PROBE (touch.h documents the rule and the packing)
 * ==========================================================================
 * ONE-SHOT, at init, with the TSC DISABLED. It answers the question the
 * counters cannot: is the chip configured the way we think, and is there a
 * panel on the other end of the flex?
 *
 * The electrical idea, in one line: a 4-wire resistive plate is a few hundred
 * ohms of CONTINUOUS film between its two terminals, so if one terminal is
 * driven the other MUST follow it -- to the high rail when driven high, to the
 * low rail when driven low. An open flex leaves the read terminal floating: it
 * still reads a number, but that number does not move with the drive. Which is
 * why every line is sampled TWICE (partner driven high, then low) and both are
 * required. One driven-high sample cannot tell a plate from an input sitting at
 * the rail, and the host test proves it (the float_code = 4095 control).
 *
 * Register choreography per sample (ST DS6069):
 *   GPIO_AF  <- INIT_GPIO_AF | (1<<partner)   partner becomes a GPIO, the read
 *                                             line stays alternate-function
 *   GPIO_DIR <- (1<<partner)                  partner is an output
 *   GPIO_SET_PIN / GPIO_CLR_PIN <- (1<<partner)   drive it high / low
 *   ADC_CAPT <- (1<<line)                     start the conversion
 *   poll ADC_CAPT until the bit clears        bounded, TOUCH_PROBE_CONV_POLLS
 *   read ADC_DATA_CH(line), 2 bytes           12-bit right-aligned
 * and afterwards GPIO_AF/GPIO_DIR are restored so the TSC owns all four pins
 * again before TSC_CTRL.EN is set.
 */

/* One bounded 12-bit conversion on ADC channel `ch` (0..3 are the touch lines
 * -- see the PIN vs ADC CHANNEL block above). Fails closed: a conversion that
 * never completes returns an error rather than reading the stale data register.
 *
 * COMPLETION comes from ADC_INT_STA (0x0F) bit `ch`, NOT from ADC_CAPT.
 * ADC_CAPT cannot be used: the datasheet (Rev 5 p.32) says "Write '1' to
 * initiate data acquisition for the corresponding channel. Writing '0' has no
 * effect. Reads '1' if conversion is completed. Reads '0' if conversion is in
 * progress" -- so the sense is the OPPOSITE of a busy flag, its reset value is
 * 0xFF (every channel already reads "completed" before anything was started),
 * and because writing 0 has no effect it cannot be cleared to re-arm. The old
 * code waited for the bit to CLEAR, which is why on silicon (2026-09-14) it
 * "never cleared within the poll budget": it never can. ADC_INT_STA's per-
 * channel ISA[x] bit is the only usable end-of-conversion signal, so it is
 * cleared BEFORE the capture is started and then polled for a rising edge.
 *
 * (The datasheet contradicts itself on how ADC_INT_STA clears -- the register
 * header says "Writing '1' ... clears the corresponding bits" while the field
 * text says reading clears them. Both are handled: the bit is W1C'd before the
 * start, and the loop tests the value it has just read.) */
static int stmpe_adc_convert(unsigned ch, uint16_t *out)
{
    uint8_t capt = (uint8_t)(1u << ch);
    uint8_t d[2] = { 0u, 0u };
    int done = 0;
    int i;

    /* Re-arm: clear this channel's end-of-conversion bit before starting, so a
     * stale one cannot be mistaken for this conversion finishing. */
    if (stmpe_wr(STMPE_ADC_INT_STA, capt) != TOUCH_OK) {
        return TOUCH_ERR_TIMEOUT;
    }
    if (stmpe_wr(STMPE_ADC_CAPT, capt) != TOUCH_OK) {
        return TOUCH_ERR_TIMEOUT;
    }
    for (i = 0; i < TOUCH_PROBE_CONV_POLLS; i++) {
        uint8_t ist = 0u;
        if (stmpe_rd(STMPE_ADC_INT_STA, &ist, 1u) != TOUCH_OK) {
            return TOUCH_ERR_TIMEOUT;
        }
        if ((ist & capt) != 0u) {
            (void)stmpe_wr(STMPE_ADC_INT_STA, capt);   /* leave it clear */
            done = 1;
            break;
        }
    }
    if (!done) {
        return TOUCH_ERR_TIMEOUT;       /* never finished -- say so, do not guess */
    }
    if (stmpe_rd(STMPE_ADC_DATA_CH(ch), d, 2u) != TOUCH_OK) {
        return TOUCH_ERR_TIMEOUT;
    }
    if (out) {
        *out = (uint16_t)(((((uint16_t)d[0]) << 8) | (uint16_t)d[1]) & 0x0FFFu);
    }
    return TOUCH_OK;
}

/* Make GPIO pin `drive_gpio` (a GPIO BIT NUMBER, 4..7, not an ADC channel) an
 * output at `level`; every other touch pin stays at GPIO_AF = 0, which with the
 * TSC disabled routes it to the ADC (Rev 5 Table 4). */
static int stmpe_probe_drive(unsigned drive_gpio, int level)
{
    uint8_t bit = (uint8_t)(1u << drive_gpio);

    if (stmpe_wr(STMPE_GPIO_AF, (uint8_t)(STMPE_INIT_GPIO_AF | bit)) != TOUCH_OK) {
        return TOUCH_ERR_TIMEOUT;
    }
    if (stmpe_wr(STMPE_GPIO_DIR, bit) != TOUCH_OK) {
        return TOUCH_ERR_TIMEOUT;
    }
    return stmpe_wr(level ? STMPE_GPIO_SET_PIN : STMPE_GPIO_CLR_PIN, bit);
}

/* Read back the five registers that decide whether a touch can be SEEN at all
 * and compare each with the literal init wrote. Packs them into the two mailbox
 * words and returns the status bits it earned. */
static uint32_t stmpe_probe_readbacks(void)
{
    /* `mask` = the bits the datasheet DEFINES for that register; every mask
     * below is taken from the Doc ID 14489 Rev 5 register pages, so the
     * comparison is strict on functional fields and blind to reserved ones:
     *   GPIO_AF     0xFF  [7:0] one bit per pin                        (p.53)
     *   SYS_CTRL2   0x0F  [7:4] RESERVED; [3] TS_OFF [2] GPIO_OFF
     *                     [1] TSC_OFF [0] ADC_OFF                      (p.23)
     *   TSC_CFG     0xFF  [7:6] AVE_CTRL [5:3] TOUCH_DET_DELAY
     *                     [2:0] SETTLING                               (p.38)
     *   ADC_CTRL1   0x7A  [7] [2] [0] RESERVED; [6:4] SAMPLE_TIME
     *                     [3] MOD_12B [1] REF_SEL                      (p.31)
     *   TSC_I_DRIVE 0x01  [7:1] RESERVED; [0] DRIVE                    (p.46)
     * Reserved bits must be excluded or the gate fires on nothing: on silicon
     * (2026-09-14) ADC_CTRL1 was written 0x48 and read back 0x4C -- bit 2,
     * reserved, whose documented RESET value is 1 (ADC_CTRL1 resets to 0x1C) --
     * and the probe reported CHIP_BAD on it. Expect the same bit to appear on
     * top of today's 0x69. */
    static const struct { uint8_t reg; uint8_t expect; uint8_t mask; } RB[5] = {
        { STMPE_GPIO_AF,     STMPE_INIT_GPIO_AF,   0xFFu },
        { STMPE_SYS_CTRL2,   STMPE_INIT_SYS_CTRL2, 0x0Fu },
        { STMPE_TSC_CFG,     STMPE_INIT_TSC_CFG,   0xFFu },
        { STMPE_ADC_CTRL1,   STMPE_INIT_ADC_CTRL1, 0x7Au },
        { STMPE_TSC_I_DRIVE, STMPE_INIT_I_DRIVE,   0x01u },
    };
    uint32_t status = 0u;
    uint8_t  got[5] = { 0u, 0u, 0u, 0u, 0u };
    unsigned i;

    for (i = 0u; i < 5u; i++) {
        uint8_t v = 0u;
        if (stmpe_rd(RB[i].reg, &v, 1u) != TOUCH_OK) {
            status |= TOUCH_PROBE_ST_RB_ERR;   /* got[i] stays 0; the bit says why */
            continue;
        }
        got[i] = v;
        if ((v & RB[i].mask) != (RB[i].expect & RB[i].mask)) {
            status |= TOUCH_PROBE_ST_RB_DIFF;
        }
    }

    s_probe_regs = (uint32_t)got[0]
                 | ((uint32_t)got[1] << 8)
                 | ((uint32_t)got[2] << 16)
                 | ((uint32_t)got[3] << 24);
    s_probe_adc_x = (uint32_t)got[4] << 24;    /* the fifth rides here (touch.h) */
    return status;
}

/* The probe proper. Call with the TSC DISABLED; leaves the pin mux restored.
 *
 * The measurement it makes, and the one it does NOT:
 *   Rev 5 Table 4 "Pin configuration for X+, Y+, X-, Y-" is the whole warrant.
 *   With GPIO_AF bit = 0 AND TSC_CTRL.EN = 0 the pin is an ADC input; with
 *   GPIO_AF bit = 1 it is a GPIO, whatever the TSC is doing. So driving one
 *   plate terminal as a GPIO output while converting the other as an ADC input
 *   is a configuration the datasheet defines. What the datasheet does NOT
 *   define is what the internal analogue switches and drivers do to those pins
 *   during such a conversion -- section 10.1 describes the "driver and switch
 *   control unit" in one paragraph and never states the switch state, and there
 *   is no specification of the ADC's internal reference voltage either. That is
 *   why a line which FAILS to follow its partner is no longer reported as
 *   PANEL_OPEN: see the verdict rule below. */
static void stmpe811_panel_probe(void)
{
    /* ADC channel to convert, and the GPIO pin of the same-plate partner whose
     * drive it must follow. TWO numbering schemes -- see the PIN vs ADC CHANNEL
     * block above. X plate: X+ (CH0 / GPIO-4) <-> X- (CH1 / GPIO-6).
     * Y plate: Y+ (CH2 / GPIO-5) <-> Y- (CH3 / GPIO-7). */
    static const uint8_t LINE_CH[4]     = { STMPE_ADC_CH_XP, STMPE_ADC_CH_XM,
                                            STMPE_ADC_CH_YP, STMPE_ADC_CH_YM };
    static const uint8_t PARTNER_GPIO[4] = { STMPE_GPIO_XM, STMPE_GPIO_XP,
                                             STMPE_GPIO_YM, STMPE_GPIO_YP };
    uint16_t hi[4] = { 0u, 0u, 0u, 0u };
    uint32_t status;
    unsigned i;

    /* 0. Put the part in the state Table 4 describes and the ADC needs.
     *    TSC_CTRL = 0 both guarantees EN = 0 (without it CH0..CH3 "give 0x0000",
     *    Rev 5 p.33) and honours programming-sequence step (s): "perform a FIFO
     *    reset and TSC disabling when the ADC or TSC setting are reconfigured".
     *    TSC_SHIELD = 0 because its bits deliberately ground X+/X-/Y+/Y-
     *    ("Write 1 to GND X+, X-, Y+, Y- lines", p.46) -- with any of them set
     *    the probe would be measuring a short it asked for. Both are at these
     *    values out of reset; writing them makes the probe independent of what
     *    a previous image left behind. */
    (void)stmpe_wr(STMPE_TSC_CTRL, 0x00u);
    (void)stmpe_wr(STMPE_TSC_SHIELD, 0x00u);

    /* 1. The read-backs first: on a chip that is not configured as we believe,
     *    the continuity numbers below describe something other than the panel. */
    status = stmpe_probe_readbacks();

    /* 2. Continuity, four lines x two drives. */
    for (i = 0u; i < 4u; i++) {
        uint16_t lo = 0u;

        if (stmpe_probe_drive(PARTNER_GPIO[i], 1) != TOUCH_OK ||
            stmpe_adc_convert(LINE_CH[i], &hi[i]) != TOUCH_OK ||
            stmpe_probe_drive(PARTNER_GPIO[i], 0) != TOUCH_OK ||
            stmpe_adc_convert(LINE_CH[i], &lo) != TOUCH_OK) {
            status |= TOUCH_PROBE_ST_ADC_ERR;
            break;                     /* bounded: one bad line ends the probe */
        }
        /* FOLLOW is a SEPARATION, not two absolute thresholds. The old test
         * ("driven-high reads >= 3/4 scale, driven-low reads <= 1/4 scale")
         * assumed the ADC's full scale is the pin's supply rail; the datasheet
         * never gives the internal reference voltage, so that was an
         * unsupportable assumption. A difference between the two drives needs
         * no reference at all: a floating input reads the same both ways. */
        if (hi[i] < lo || (uint16_t)(hi[i] - lo) < TOUCH_PROBE_FOLLOW_MIN) {
            status |= TOUCH_PROBE_ST_FOLLOW_BAD;   /* it did not follow the drive */
        }
    }

    /* 3. Hand the pins back to the TSC exactly as init left them. */
    (void)stmpe_wr(STMPE_GPIO_DIR, 0x00u);
    (void)stmpe_wr(STMPE_GPIO_AF, STMPE_INIT_GPIO_AF);

    if ((status & TOUCH_PROBE_ST_ADC_ERR) == 0u) {
        status |= TOUCH_PROBE_ST_RAN;
    }

    s_probe_adc_x |= (uint32_t)hi[0] | ((uint32_t)hi[1] << 12);
    s_probe_adc_y  = (uint32_t)hi[2] | ((uint32_t)hi[3] << 12)
                   | ((status & 0xFFu) << 24);

    /* 4. THE RULE (touch.h states it in the same order). */
    if (status & TOUCH_PROBE_ST_RB_DIFF) {
        s_probe_verdict = TOUCH_VERDICT_CHIP_BAD;
    } else if ((status & (TOUCH_PROBE_ST_RB_ERR | TOUCH_PROBE_ST_ADC_ERR)) != 0u ||
               (status & TOUCH_PROBE_ST_RAN) == 0u) {
        s_probe_verdict = TOUCH_VERDICT_UNKNOWN;
    } else if (status & TOUCH_PROBE_ST_FOLLOW_BAD) {
        /* NOT TOUCH_VERDICT_OPEN. A line that fails to follow its partner is
         * consistent with an open plate, but it is equally consistent with the
         * STMPE811's undocumented internal switch state during an ADC capture,
         * or with an ADC reference that does not reach the drive rail. On
         * 2026-09-14 the OPEN verdict was fielded and was WRONG -- Arm's own
         * image detects touches on this very panel. "I do not know" is the only
         * answer this measurement can support in the negative direction, and
         * the raw samples plus TOUCH_PROBE_ST_FOLLOW_BAD are still exported so
         * a human can see exactly what was measured. */
        s_probe_verdict = TOUCH_VERDICT_UNKNOWN;
    } else {
        s_probe_verdict = TOUCH_VERDICT_PRESENT;
    }
}

/* One-time STMPE811 bring-up (datasheet sec.11 programming sequence). Confirms
 * the part (CHIP_ID -> s_chip_id) then enables the ADC + touch controller.
 * Returns TOUCH_OK only if CHIP_ID reads 0x0811. Bounded; call once. */
/* Best-effort write that RECORDS failure (the old code discarded it). */
#define TSTMPE_WR(r, v) do { if (stmpe_wr((r), (v)) != TOUCH_OK) { s_dbg_init_wr_fail++; } } while (0)

/* The two settling waits Arm's driver takes as delay_ms(10) -- one after the
 * soft reset, one after ADC_CTRL1. The datasheet specifies NEITHER: it gives no
 * minimum pulse width and no post-reset settling time for SOFT_RESET anywhere
 * (checked in Rev 2 and Rev 5), so 10 ms is Arm's and ST's practice, not a
 * guarantee, and it is not something to shorten on a guess.
 *
 * HONEST LIMITATION: this is an ITERATION COUNT, not a duration -- it is worth
 * (loop body cycles / clock) seconds and moves with the AXI clock and the
 * optimisation level. The tree HAS a real microsecond timebase
 * (mps3_sys_now_us() / mps3_spin_until(), firmware/common/service.h) and this
 * should use it; doing so needs firmware/test/Makefile to add service.c to
 * test_touch's link line, which is outside this file's scope. Recorded as a
 * follow-up rather than left unsaid. Sized so that even an optimising build at
 * 100 MHz spends several milliseconds here: a volatile load/store per iteration
 * is >= 3 cycles, so 400k iterations is >= 12 ms. */
#ifndef TOUCH_SETTLE_ITERS
#define TOUCH_SETTLE_ITERS 400000
#endif
static void stmpe_settle(void)
{
    for (volatile int d = 0; d < TOUCH_SETTLE_ITERS; d++) {
    }
}

/* THE PROGRAMMING SEQUENCE -- Arm's Touch_V2M-MPS3.c, in Arm's order, with the
 * deviations named at each STMPE_INIT_* definition above. ONE table, two
 * writers: stmpe811_init() walks it at boot (with the panel probe spliced in at
 * STMPE_SEQ_PROBE_AT), and the bus-loss recovery walks it one entry per
 * touch_poll() call (touch_recover_step()). A second typed-out copy of the
 * sequence would be a second truth that can drift from the first. `settle` = 1:
 * Arm takes delay_ms(10) AFTER this write. */
typedef struct { uint8_t reg; uint8_t val; uint8_t settle; } stmpe_seq_t;
static const stmpe_seq_t STMPE_ARM_SEQ[] = {
    { STMPE_SYS_CTRL1,   0x02u,                1u },  /* [1] SOFT_RESET; Arm delay_ms(10) */
    /* Arm never de-asserts SOFT_RESET; ST's driver does (write 0x02, 10 ms,
     * write 0x00, 2 ms) and the bit is not documented as self-clearing, so the
     * explicit release is kept -- it is the safe half of the disagreement. */
    { STMPE_SYS_CTRL1,   0x00u,                0u },  /* release reset              */
    { STMPE_SYS_CTRL2,   STMPE_INIT_SYS_CTRL2, 0u },  /* ADC+TSC+GPIO clocks on     */
    { STMPE_INT_EN,      STMPE_INIT_INT_EN,    0u },  /* TOUCH_DET|FIFO_TH|OFLOW    */
    { STMPE_ADC_CTRL1,   STMPE_INIT_ADC_CTRL1, 1u },  /* 12-bit, 124-clk; Arm delay_ms(10) */
    { STMPE_ADC_CTRL2,   STMPE_INIT_ADC_CTRL2, 0u },  /* ADC clk 3.25 MHz           */
    { STMPE_TSC_CFG,     STMPE_INIT_TSC_CFG,   0u },  /* AVE=8, 10us/500us          */
    { STMPE_FIFO_TH,     0x01u,                0u },  /* "must not be zero"         */
    { STMPE_FIFO_STA,    0x01u,                0u },  /* assert FIFO_RESET          */
    { STMPE_FIFO_STA,    0x00u,                0u },  /* release FIFO_RESET         */
    { STMPE_TSC_FRACT_Z, STMPE_INIT_FRACT_Z,   0u },  /* Z = 1.7 fixed point        */
    { STMPE_TSC_I_DRIVE, STMPE_INIT_I_DRIVE,   0u },  /* 50 mA panel drive          */
    { STMPE_GPIO_AF,     STMPE_INIT_GPIO_AF,   0u },  /* all pins -> TSC/ADC        */
    /* ---- STMPE_SEQ_PROBE_AT: the boot-time panel probe runs here, TSC disabled */
    { STMPE_TSC_CTRL,    STMPE_INIT_TSC_CTRL,  0u },  /* OP_MOD=XYZ, TSC EN=1       */
    { STMPE_INT_STA,     0xFFu,                0u },  /* W1C any pending            */
};
#define STMPE_SEQ_LEN      ((unsigned)(sizeof(STMPE_ARM_SEQ) / sizeof(STMPE_ARM_SEQ[0])))
#define STMPE_SEQ_PROBE_AT 13u   /* index of the TSC_CTRL EN write */

static int stmpe811_init(void)
{
    uint8_t id[2] = { 0u, 0u };
    unsigned i;

    /* Confirm the part + address BEFORE programming it. */
    if (stmpe_rd(STMPE_CHIP_ID, id, 2u) != TOUCH_OK) {
        s_chip_id = 0u;
        return TOUCH_ERR_TIMEOUT;               /* nothing ACKed at STMPE811_ADDR */
    }
    s_chip_id = (uint16_t)(((uint16_t)id[0] << 8) | (uint16_t)id[1]);

    /* STMPE_ARM_SEQ up to the probe point. Each write is best-effort: a NACK
     * mid-sequence still leaves s_chip_id as the tell and bumps
     * s_dbg_init_wr_fail. */
    for (i = 0u; i < STMPE_SEQ_PROBE_AT; i++) {
        TSTMPE_WR(STMPE_ARM_SEQ[i].reg, STMPE_ARM_SEQ[i].val);
        if (STMPE_ARM_SEQ[i].settle) {
            stmpe_settle();                     /* Arm: delay_ms(10) */
        }
    }

    /* Read the five decisive registers back and run the panel-continuity probe
     * -- BOTH before TSC_CTRL.EN=1, because the probe drives the four touch
     * lines as GPIOs and must not fight the touch controller for them. ONE
     * SHOT: nothing here is ever reached from touch_poll(). Only for a part
     * that identified itself as an STMPE811; on anything else this register
     * choreography describes nothing and the verdict stays UNKNOWN. */
    if (s_chip_id == STMPE811_ID_EXPECT) {
        stmpe811_panel_probe();
    }

    /* The rest of STMPE_ARM_SEQ: TSC_CTRL EN=1, then INT_STA W1C. */
    for (i = STMPE_SEQ_PROBE_AT; i < STMPE_SEQ_LEN; i++) {
        TSTMPE_WR(STMPE_ARM_SEQ[i].reg, STMPE_ARM_SEQ[i].val);
        if (STMPE_ARM_SEQ[i].settle) {
            stmpe_settle();
        }
    }

    /* Prove the enable actually landed: if TSC_CTRL does not read back with bit0
     * set, the writes above were NACKed and the TSC is not running -- which
     * presents exactly as "the panel never reports a touch". */
    {
        uint8_t back = 0u;
        s_dbg_ctrl_after = (stmpe_rd(STMPE_TSC_CTRL, &back, 1u) == TOUCH_OK)
                         ? (uint32_t)back : 0xFFFFFFFFu;
    }

    return (s_chip_id == STMPE811_ID_EXPECT) ? TOUCH_OK : TOUCH_ERR_PARAM;
}

/* ==========================================================================
 * Bus-loss recovery (touch.h "BUS-LOSS RECOVERY")
 * ========================================================================== */

/* The AXI IIC bring-up touch_init() does: soft-reset the core, program the
 * dynamic RX-count IRQ depth to the PG090 reset default, enable, then clear any
 * latched (W1C) interrupt status. Four register accesses: no wait, no I2C. */
static void iic_core_reset(void)
{
    iwr(TOUCH_IIC_SOFTR, IIC_SOFTR_RESET_KEY);
    iwr(TOUCH_IIC_RX_FIFO_PIRQ, 0x0Fu);
    iwr(TOUCH_IIC_CR, IIC_CR_EN);
    iwr(TOUCH_IIC_ISR, ird(TOUCH_IIC_ISR));
}

static int s_rec_ok_valid;   /* s_rec_ok_ms holds a recovery since touch_init() */

/* The latch itself: touch_poll() drops to its zero-I2C path, and a recovery
 * episode is armed. The first attempt is TOUCH_RECOVER_FAST_MS from now -- the
 * latch has just spent TOUCH_BUS_FAIL_LIMIT bounded timeouts learning the bus is
 * gone, so trying again at once would only pay another. A latch that follows a
 * recovery by less than TOUCH_RECOVER_STABLE_MS is a FLAP: it keeps its back-off
 * position (s_rec_tries), so a marginal bus climbs to the slow rate instead of
 * being retried fast forever. */
static void touch_bus_lost_latch(void)
{
    const uint32_t now = mps3_sys_now_ms();

    s_tsc_ready = 0;
    s_dbg_bus_lost++;
    if (!s_rec_ok_valid ||
        (uint32_t)(now - s_rec_ok_ms) >= (uint32_t)TOUCH_RECOVER_STABLE_MS) {
        s_rec_tries = 0u;
    }
    s_bus_latched  = 1;
    s_rec_step     = 0u;
    s_rec_settling = 0;
    s_rec_last_ms  = now;
}

/* End the current attempt; the next one waits out the back-off from this
 * attempt's START (s_rec_last_ms), so the cadence does not drift. */
static void touch_recover_abort(void)
{
    s_rec_step     = 0u;
    s_rec_settling = 0;
}

/* ONE bounded step of a recovery attempt; touch_poll() calls it only while the
 * bus-loss latch is set. Each call issues AT MOST ONE I2C transaction (or none),
 * and a failed one ends the attempt, so no call pays more than one transaction's
 * bounded waits -- the same order of cost as the failing poll that armed it.
 *
 *   step 0      wait out the back-off (NO I2C); when due: core reset + CHIP_ID
 *   step 1..N   STMPE_ARM_SEQ[step-1], one write per call; after an entry with
 *               `settle`, the next call returns (NO I2C) until
 *               TOUCH_RECOVER_SETTLE_US has passed on the clock
 *   step N+1    TSC_CTRL read back with EN set -> touch is live again
 *
 * Writes are STRICT here where touch_init()'s are best-effort: a NACK in a
 * liveness retry means the bus is not back, and pressing on would only pay a
 * timeout per remaining entry. (On silicon every init write ACKs --
 * s_dbg_init_wr_fail 0, docs/CLCD_PANEL_FACTS.md.) */
static void touch_recover_step(void)
{
    if (s_rec_step == 0u) {
        const uint32_t now = mps3_sys_now_ms();
        const uint32_t wait = (s_rec_tries < TOUCH_RECOVER_FAST_TRIES)
                            ? (uint32_t)TOUCH_RECOVER_FAST_MS
                            : (uint32_t)TOUCH_RECOVER_SLOW_MS;
        uint8_t id[2] = { 0u, 0u };

        if ((uint32_t)(now - s_rec_last_ms) < wait) {
            return;                              /* not due: zero I2C */
        }
        s_rec_last_ms = now;
        s_rec_tries++;
        s_dbg_rec_tries++;
        iic_core_reset();
        if (stmpe_rd(STMPE_CHIP_ID, id, 2u) != TOUCH_OK ||
            (uint16_t)(((uint16_t)id[0] << 8) | (uint16_t)id[1]) != STMPE811_ID_EXPECT) {
            return;                              /* still gone; step stays 0 */
        }
        s_rec_step = 1u;
        return;
    }

    if (s_rec_settling) {
        if ((uint32_t)(mps3_sys_now_us() - s_rec_settle_us) < (uint32_t)TOUCH_RECOVER_SETTLE_US) {
            return;                              /* Arm's 10 ms, not spun */
        }
        s_rec_settling = 0;
    }

    if (s_rec_step <= STMPE_SEQ_LEN) {
        const stmpe_seq_t *e = &STMPE_ARM_SEQ[s_rec_step - 1u];
        if (stmpe_wr(e->reg, e->val) != TOUCH_OK) {
            touch_recover_abort();
            return;
        }
        if (e->settle) {
            s_rec_settling  = 1;
            s_rec_settle_us = mps3_sys_now_us();
        }
        s_rec_step++;
        return;
    }

    {
        uint8_t back = 0u;
        if (stmpe_rd(STMPE_TSC_CTRL, &back, 1u) != TOUCH_OK ||
            (back & STMPE_INIT_TSC_CTRL) == 0u) {
            touch_recover_abort();               /* EN did not land */
            return;
        }
        s_dbg_ctrl_after = (uint32_t)back;
    }

    /* Live again. The calibration in force is deliberately NOT re-seeded. */
    s_rec_step     = 0u;
    s_bus_latched  = 0;
    s_err_streak   = 0u;
    s_down_streak  = 0u;
    s_pen_down     = 0;
    s_rec_ok_ms    = mps3_sys_now_ms();
    s_rec_ok_valid = 1;
    s_dbg_recoveries++;
    s_tsc_ready    = 1;
}

void touch_health(touch_health_t *out)
{
    if (out) {
        out->ok         = s_tsc_ready ? 1 : 0;
        out->bus_lost   = s_dbg_bus_lost;
        out->recoveries = s_dbg_recoveries;
        out->attempts   = s_dbg_rec_tries;
    }
}

/* ==========================================================================
 * Pen-down gate -- WEAK, overridable (host test + fabric mint)
 * ========================================================================== */
/* The fabric routes CLCD_TINT (STMPE811 PENIRQ, active-low) to AXI-INTC concat
 * In4 = bit 4 of the INTC ISR (0x41200000 + 0x00). A bench-tuned build can gate
 * on that bit to skip the I2C read when no pen is down. Until the In4 polarity +
 * INTC level-latch are confirmed on hardware, POLL UNCONDITIONALLY: the STMPE811
 * TSC_STA check + the Z floor + the edge debounce in touch_poll() make an
 * always-read correct, just slightly more I2C traffic. Weak so the host test can
 * drop in a strong definition. */
#if defined(__GNUC__)
__attribute__((weak))
#endif
int touch_tint_asserted(void)
{
    return 1;
}

/* ==========================================================================
 * Calibration + coordinate map -- REAL + host-tested (pure)
 * ========================================================================== */
const char *touch_calib_invalid_reason(const touch_calib_t *cal)
{
    if (cal == 0) {
        return "no calibration";
    }
    if (cal->shift > TOUCH_CAL_SHIFT_MAX) {
        return "bad shift";
    }
    {
        const int32_t lin[4] = { cal->ax, cal->bx, cal->ay, cal->by };
        for (unsigned i = 0u; i < 4u; i++) {
            if ((long)lin[i] > TOUCH_CAL_COEF_MAX || (long)lin[i] < -TOUCH_CAL_COEF_MAX) {
                return "coef range";
            }
        }
    }
    if ((long)cal->cx > TOUCH_CAL_OFFSET_MAX || (long)cal->cx < -TOUCH_CAL_OFFSET_MAX ||
        (long)cal->cy > TOUCH_CAL_OFFSET_MAX || (long)cal->cy < -TOUCH_CAL_OFFSET_MAX) {
        return "offset range";
    }
    /* In range, so the products fit int64 trivially. */
    if ((int64_t)cal->ax * (int64_t)cal->by - (int64_t)cal->bx * (int64_t)cal->ay == 0) {
        return "singular";
    }
    return 0;
}

int touch_set_calibration(const touch_calib_t *cal)
{
    if (touch_calib_invalid_reason(cal) != 0) {
        return -1;   /* never install an unsafe map; the old one stays */
    }
    s_cal = *cal;
    return 0;
}

void touch_get_calibration(touch_calib_t *out)
{
    if (out) {
        *out = s_cal;
    }
}

void touch_raw_sample(uint16_t *rx, uint16_t *ry, uint16_t *rz, uint32_t *seen)
{
    if (rx)   *rx   = (uint16_t)s_dbg_raw_x;
    if (ry)   *ry   = (uint16_t)s_dbg_raw_y;
    if (rz)   *rz   = (uint16_t)s_dbg_raw_z;
    if (seen) *seen = s_dbg_raw_seen;
}

void touch_last_xy(uint16_t *x_px, uint16_t *y_px)
{
    if (x_px) *x_px = s_last_x;
    if (y_px) *y_px = s_last_y;
}

static int32_t clamp_i32(int32_t v, int32_t lo, int32_t hi)
{
    if (v < lo) return lo;
    if (v > hi) return hi;
    return v;
}

void touch_map_raw(uint16_t rx, uint16_t ry, uint16_t *x_px, uint16_t *y_px)
{
    int32_t rxi = (int32_t)rx;
    int32_t ryi = (int32_t)ry;

    int32_t xc = (s_cal.ax * rxi + s_cal.bx * ryi + s_cal.cx) >> s_cal.shift;
    int32_t yc = (s_cal.ay * rxi + s_cal.by * ryi + s_cal.cy) >> s_cal.shift;

    xc = clamp_i32(xc, 0, (int32_t)TOUCH_SCREEN_W - 1);
    yc = clamp_i32(yc, 0, (int32_t)TOUCH_SCREEN_H - 1);

#if CLCD_ROTATE_180
    xc = (int32_t)TOUCH_SCREEN_W - 1 - xc;
    yc = (int32_t)TOUCH_SCREEN_H - 1 - yc;
#endif

    if (x_px) *x_px = (uint16_t)xc;
    if (y_px) *y_px = (uint16_t)yc;
}

/* ==========================================================================
 * Lifecycle + the superloop step
 * ========================================================================== */
void touch_init(void)
{
    /* AXI IIC bring-up: soft-reset the core, program the dynamic RX-count IRQ
     * depth to the PG090 reset default, enable, then clear any latched (W1C)
     * interrupt status. Bounded; returns at once. */
    iic_core_reset();

    s_cal        = s_calib_default;
    s_down_streak = 0u;
    s_err_streak = 0u;
    s_pen_down   = 0;
    s_last_x     = 0u;
    s_last_y     = 0u;
    s_chip_id    = 0u;
    s_tsc_ready  = 0;

    /* A full init supersedes any recovery episode in flight. The counters
     * behind touch_health() are lifetime and are NOT cleared. */
    s_bus_latched  = 0;
    s_rec_step     = 0u;
    s_rec_tries    = 0u;
    s_rec_settling = 0;
    s_rec_ok_valid = 0;

    /* The probe words are re-derived by every touch_init(): a re-init after a
     * bus-loss latch must not leave the previous boot's verdict standing. */
    s_probe_regs    = 0u;
    s_probe_adc_x   = 0u;
    s_probe_adc_y   = 0u;
    s_probe_verdict = TOUCH_VERDICT_UNKNOWN;

    /* Bring up the STMPE811 (confirms CHIP_ID, enables ADC+TSC). touch_poll() is
     * gated on success: if the part is absent / at 0x44 / the bus is dead, we
     * mark touch NOT ready and touch_poll() stays inert -- it must NEVER hammer
     * a wedged I2C bus every pass (that would starve lwIP + the display). */
    s_tsc_ready = (stmpe811_init() == TOUCH_OK);
}

/* The STMPE811 CHIP_ID captured at touch_init() (0x0811 = present + ACKing at
 * STMPE811_ADDR; 0 = nothing ACKed -- wrong strap or dead bus). Lets the shell
 * bring-up log a one-line on-silicon confirmation without a bus scan. */
uint16_t touch_chip_id(void)
{
    return s_chip_id;
}

/* The four panel-probe mailbox words (touch.h documents the packing + rule). */
uint32_t touch_probe_regs_word(void)  { return s_probe_regs; }
uint32_t touch_probe_adc_x_word(void) { return s_probe_adc_x; }
uint32_t touch_probe_adc_y_word(void) { return s_probe_adc_y; }
uint32_t touch_probe_verdict(void)    { return s_probe_verdict; }

/* ---- Bring-up bus witness -------------------------------------------------
 * Sweep the 7-bit I2C address space, reporting which addresses ACK a one-byte
 * write. This tells apart the three bring-up outcomes the header warns about:
 * the part at 0x41 (expected), the part at 0x44 (A0=VCC strap), and a dead bus
 * / missing pull-ups (nothing ACKs anywhere). Returns the number of
 * responders and fills `found` with up to `max` of them.
 *
 * Requires touch_init() first -- that is what enables the AXI IIC core. It is
 * bounded and ONE-SHOT: call it from bring-up, NEVER from the superloop. A
 * per-pass bus sweep would starve lwIP and the display exactly the way the
 * ungated touch_poll() did (see the s_tsc_ready gate above). */
unsigned touch_bus_scan(uint8_t *found, unsigned max)
{
    uint8_t  reg0 = 0x00u;   /* register pointer 0: harmless on any TSC */
    unsigned n    = 0u;
    unsigned a;

    for (a = 0x08u; a <= 0x77u; a++) {   /* skip reserved low/high ranges */
        if (iic_master_write((uint8_t)a, &reg0, 1u) == TOUCH_OK) {
            if (found && n < max) { found[n] = (uint8_t)a; }
            n++;
        }
    }
    return n;
}

int touch_poll(void)
{
    /* Master gate: if the STMPE811 was not confirmed at init (absent / wrong
     * address / dead bus), do NOTHING -- no I2C, no spin. This keeps a touch
     * bring-up problem from ever starving the superloop (lwIP + display).
     * The one exception is a bus LOST after a good init: then this pass is one
     * bounded recovery step (touch_recover_step(): zero I2C until the back-off
     * is due, then at most one transaction per pass). The pass that completes a
     * recovery still returns NONE; the next one samples. */
    if (!s_tsc_ready) {
        if (s_bus_latched) {
            touch_recover_step();
        }
        return CLCD_ACT_NONE;
    }

    /* Gate: no pen -> a single register read, reset the debounce, done. This is
     * the common case and keeps the cooperative superloop cheap. */
    if (!touch_tint_asserted()) {
        s_down_streak = 0u;
        s_pen_down    = 0;
        /* Pen-up is not a bus failure. Clearing the run here is what makes the
         * latch's counter genuinely CONSECUTIVE: without it, isolated glitches
         * separated by any amount of healthy pen-up polling still sum to
         * TOUCH_BUS_FAIL_LIMIT and kill touch for the boot. Latent while the
         * weak touch_tint_asserted() returns 1, but touch.h advertises this as
         * a seam a bench-tuned build overrides, and the host test already
         * installs a strong definition. */
        s_err_streak  = 0u;
        return CLCD_ACT_NONE;
    }

    uint16_t rx = 0, ry = 0, rz = 0;
    if (tsc_read_xyz(&rx, &ry, &rz) != TOUCH_OK) {
        /* Bus error: fail closed, treat as pen-up. */
        s_down_streak = 0u;
        s_pen_down    = 0;

        /* Bus-loss latch. The CHIP_ID gate only proves the part was there at
         * init; if it stops answering later, every pass from here on pays the
         * full bounded IIC timeout and starves the superloop. Once the bus has
         * failed TOUCH_BUS_FAIL_LIMIT times in a row, stop sampling it --
         * s_tsc_ready = 0 returns touch_poll() to its zero-I2C path, the
         * dormant-and-harmless state the gate exists to guarantee. Until
         * 2026-09-24 that lasted for the rest of the boot, and on silicon it
         * left touch dead for hours after a wedge nobody could see. Now the
         * latch arms a slow, bounded re-init (touch_bus_lost_latch() /
         * touch_recover_step()): retrying EVERY pass is still what is removed. */
        if (s_err_streak < TOUCH_BUS_FAIL_LIMIT) {
            s_err_streak++;
        }
        if (s_err_streak >= TOUCH_BUS_FAIL_LIMIT) {
            touch_bus_lost_latch();
        }
        return CLCD_ACT_NONE;
    }
    s_err_streak = 0u;   /* a good read clears the run */

    /* Pressure floor: reject the open-circuit / too-light reading. */
    if (rz < TOUCH_Z_MIN) {
        s_down_streak = 0u;
        s_pen_down    = 0;
        return CLCD_ACT_NONE;
    }

    /* Debounce: require N consecutive valid samples before believing the pen. */
    if (s_down_streak < TOUCH_DEBOUNCE_SAMPLES) {
        s_down_streak++;
    }
    if (s_down_streak < TOUCH_DEBOUNCE_SAMPLES) {
        return CLCD_ACT_NONE;   /* still settling */
    }

    /* Edge: dispatch exactly ONCE per contact (a tap), not every held poll. */
    if (s_pen_down) {
        return CLCD_ACT_NONE;
    }
    s_pen_down = 1;

    uint16_t x_px = 0, y_px = 0;
    touch_map_raw(rx, ry, &x_px, &y_px);
    s_last_x = x_px;
    s_last_y = y_px;

    /* Dispatch: hand the pixel to the CLCD hit-tester and return its action.
     * We do NOT call clcd.c page functions directly -- the integration hook
     * (a touch_poll() call inside clcd_poll()) is the documented one-liner in
     * touch.h. */
    return clcd_hittest(x_px, y_px);
}

/* ==========================================================================
 * Test-only introspection
 * ========================================================================== */
#ifdef MPS3_TOUCH_TEST_HOOKS
int touch_test_iic_read(uint8_t addr7, const uint8_t *cmd, unsigned cmdlen,
                        uint8_t *buf, unsigned n)
{
    return iic_master_read(addr7, cmd, cmdlen, buf, n);
}

int touch_test_iic_write(uint8_t addr7, const uint8_t *data, unsigned len)
{
    return iic_master_write(addr7, data, len);
}

void touch_test_counters(uint32_t *poll_ok, uint32_t *poll_err,
                         uint32_t *sta_seen, uint32_t *bus_lost)
{
    if (poll_ok)  *poll_ok  = s_dbg_poll_ok;
    if (poll_err) *poll_err = s_dbg_poll_err;
    if (sta_seen) *sta_seen = s_dbg_sta_seen;
    if (bus_lost) *bus_lost = s_dbg_bus_lost;
}

void touch_test_last_xy(uint16_t *x_px, uint16_t *y_px)
{
    if (x_px) *x_px = s_last_x;
    if (y_px) *y_px = s_last_y;
}
#endif
