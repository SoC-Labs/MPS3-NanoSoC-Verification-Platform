/*
 * touch.h -- cooperative, POLLED resistive-touchscreen driver for the MPS3
 * NanoSoC CLCD (Phase 2, docs/planning/CLCD_APPS_PORTS_PAGE_PLAN.md).
 *
 * The panel is a 4-WIRE RESISTIVE screen read through an I2C Touch-Screen
 * Controller (TSC) hung off a Xilinx AXI IIC master (PG090) in the shell
 * fabric. The shell superloop has NO ISRs, so this driver is entirely POLLED:
 * touch_poll() runs once per superloop pass, does one bounded step, and returns
 * -- exactly the discipline clcd.c/swap_fsm.c use. It NEVER spins unbounded.
 *
 * ============================ TSC PART: STMPE811 ===========================
 * The touch controller is an ST STMPE811QTR (I2C addr 0x41, A0=GND strap on the
 * Keil MCBQVGA-TS module; 0x44 if A0=VCC) -- named in Arm's Corstone-SSE-300
 * -for-MPS3 manual and corroborated by the MPS3 TRM (module MCBQVGA-TS, panel
 * HX8347-D). touch.c implements its register/FIFO protocol (init sequence +
 * CHIP_ID self-test + 4-byte packed X/Y/Z read). host-tested against a mock IIC.
 * The two remaining bench items are calibration (touch_set_calibration, a 3-point
 * recal) and the board I2C pull-ups (see fpga/shell/constraints/optional/
 * mps3_harness_touch.xdc). touch_chip_id() returns 0x0811 when the part ACKs.
 *
 * ============================ TWO BENCH-GATED SEAMS ========================
 * MPS3_HAS_TOUCH -- the build flag (firmware/platform/Makefile TOUCH=1),
 *   mirroring CLCD=1. It gates the main.c / clcd_poll() call site, NOT this
 *   file's compilation. The AXI IIC master + the TSC do not exist on the
 *   shipped shell: they land at the fabric mint that instantiates the IIC block
 *   (see MPS3_TOUCH_BASE, generated into platform_regs.h from the BD line
 *   fpga/shell/bd/touch_iic_add.tcl). Until then this whole driver is dormant.
 *
 * INTEGRATION (the documented one-liner, for later): once the IIC-bearing
 *   bitstream is on the board, add ONE call inside clcd_poll()'s idle path --
 *       int act = touch_poll();
 *       if (act == CLCD_ACT_NEXT_PAGE) clcd_page_next();
 *   -- so a tap on the on-screen nav button cycles the page exactly as the
 *   USER_nPB1 short-press already does. That edit lives in clcd.c (a sibling
 *   owns it) and is deliberately NOT made here.
 */
#ifndef MPS3_TOUCH_H
#define MPS3_TOUCH_H

#include <stdint.h>

#include "../common/platform_regs.h"  /* MPS3_TOUCH_BASE + the TOUCH_IIC_* offsets */

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------------------
 * AXI IIC (Xilinx PG090) BIT FIELDS.
 *
 * The BASE and the register OFFSETS are NOT here. They are generated into
 * platform_regs.h by tools/gen_regmap.py, from the one line that decides them
 * (fpga/shell/bd/touch_iic_add.tcl's assign_bd_address) and PG090's register
 * table: MPS3_TOUCH_BASE, TOUCH_IIC_CR, TOUCH_IIC_SR, ...
 *
 * They used to live here, as MPS3_TOUCH_IIC_BASE plus fourteen IIC_* offsets
 * marked "provisional -> platform_regs.h @ mint", and the generator had a
 * special case suppressing its own TOUCH stanza so as not to redefine them.
 * That is a second spelling of a truth the map already owns -- the exact
 * duplication generation exists to remove -- and "@ mint" is a promise no gate
 * can keep. Folded in 2026-09-11: one prefix, one place, and a move of the BD
 * line now reaches this driver without anyone editing a header.
 *
 * What stays here is what no BD line and no PG table can derive: the bit
 * meanings. Same rule platform_regs.h itself follows -- fields outside the
 * fences, beside the prose that explains them.
 * ------------------------------------------------------------------------ */

/* SOFTR key (PG090: writing 0x0000000A resets the IIC core). */
#define IIC_SOFTR_RESET_KEY  0x0000000Au

/* CR @ 0x100 -- for the DYNAMIC controller only EN matters; START/STOP framing
 * is carried per-byte in TX_FIFO (below), not by MSMS/TX/RSTA. */
#define IIC_CR_EN             (1u << 0)  /* AXI IIC enable                   */
#define IIC_CR_TX_FIFO_RESET  (1u << 1)  /* flush TX FIFO. R/W and NOT self-
                                          * clearing (PG090 Table 2-9: "0 =
                                          * transmit FIFO normal operation, 1 =
                                          * resets the transmit FIFO") -- it
                                          * must be written back to 0, which
                                          * touch.c does. There is no RX-FIFO
                                          * reset bit; the RX FIFO is drained
                                          * by reading it. */
#define IIC_CR_MSMS           (1u << 2)  /* master/slave (static controller) */
#define IIC_CR_TX             (1u << 3)  /* transmit dir  (static controller)*/
#define IIC_CR_TXAK           (1u << 4)  /* NACK          (static controller)*/
#define IIC_CR_RSTA           (1u << 5)  /* repeated start(static controller)*/
#define IIC_CR_GC_EN          (1u << 6)  /* general-call enable              */

/* SR @ 0x104 (RO). */
#define IIC_SR_ABGC           (1u << 0)  /* addressed by general call        */
#define IIC_SR_AAS            (1u << 1)  /* addressed as slave               */
#define IIC_SR_BB             (1u << 2)  /* bus busy                         */
#define IIC_SR_SRW            (1u << 3)  /* slave read/write                 */
#define IIC_SR_TX_FIFO_FULL   (1u << 4)
#define IIC_SR_RX_FIFO_FULL   (1u << 5)
#define IIC_SR_RX_FIFO_EMPTY  (1u << 6)
#define IIC_SR_TX_FIFO_EMPTY  (1u << 7)

/* ISR @ 0x20 (W1C). */
#define IIC_ISR_ARB_LOST      (1u << 0)
#define IIC_ISR_TX_ERROR      (1u << 1)
#define IIC_ISR_TX_FIFO_EMPTY (1u << 2)
#define IIC_ISR_RX_FIFO_FULL  (1u << 3)
#define IIC_ISR_BUS_NOT_BUSY  (1u << 4)
#define IIC_ISR_AAS           (1u << 5)
#define IIC_ISR_NAAS          (1u << 6)
#define IIC_ISR_TX_FIFO_HALF  (1u << 7)

/* TX_FIFO @ 0x108 -- dynamic-controller framing bits above the data byte
 * (PG090 / Xilinx xiic_l.h XIIC_TX_DYN_{START,STOP}_MASK). A byte written with
 * START begins a (repeated) start and is the 7-bit-addr<<1 | R/W byte; a byte
 * written with STOP is the final one and, on a read, its [7:0] is the dynamic
 * BYTE COUNT to clock in before the stop. */
#define IIC_TX_DATA_MASK      0x000000FFu
#define IIC_TX_START          (1u << 8)   /* 0x100 -- dynamic start          */
#define IIC_TX_STOP           (1u << 9)   /* 0x200 -- dynamic stop           */

/* ------------------------------------------------------------------------
 * Pen-down interrupt (TINT / PENIRQ). PROVISIONAL: the TSC's active-low pen
 * interrupt is not yet routed in the fabric, so touch_tint_asserted() is a WEAK
 * hook (touch.c) the host test overrides and the fabric mint replaces. The
 * default samples this board_gpio input bit; the real route (a dedicated INTC
 * pending bit or a board_gpio pin) is a mint decision -- TODO(bench).
 * ------------------------------------------------------------------------ */
#ifndef MPS3_TOUCH_TINT_GPIO_BIT
#define MPS3_TOUCH_TINT_GPIO_BIT 0u
#endif

/* ------------------------------------------------------------------------
 * Return codes (touch.c internals; touch_poll() itself returns a clcd_action_t).
 * ------------------------------------------------------------------------ */
#define TOUCH_OK           0
#define TOUCH_ERR_TIMEOUT  (-1)
#define TOUCH_ERR_PARAM    (-2)

/* Largest single master read this driver will issue (bounds the RX drain). */
#ifndef TOUCH_IIC_MAX_READ
#define TOUCH_IIC_MAX_READ 8u
#endif

/* Debounce: consecutive valid pen-down samples required before a tap is
 * believed. A resistive screen is electrically noisy; 2 rejects single-sample
 * glitches while still firing on the second poll (~microseconds apart). */
#ifndef TOUCH_DEBOUNCE_SAMPLES
#define TOUCH_DEBOUNCE_SAMPLES 2u
#endif

/* Pressure floor (raw units). A TSC with no finger on the glass reads an open
 * circuit (z ~= 0); anything below this is "not a real contact" and ignored.
 * Deliberately low -- it rejects the no-touch reading, not light taps. Tune at
 * the bench once the TSC's z scaling is known -- TODO(bench). */
#ifndef TOUCH_Z_MIN
#define TOUCH_Z_MIN 64u
#endif

/* Bus-loss limit: consecutive tsc_read_xyz() failures after which touch latches
 * itself dormant for the rest of the boot (s_tsc_ready -> 0, zero further I2C).
 *
 * WHY: the CHIP_ID gate answers "was the part there at init?". It does NOT
 * answer "is it still answering?", and a TSC that dies AFTER init is the same
 * superloop-starving hazard in a quieter form -- observed on silicon
 * 2026-09-09, where the bus wedged after 538 good polls and every later pass
 * paid the full bounded IIC timeout: the loop fell from ~2475 to ~47
 * iterations/s while the board still pinged and still reported the right
 * shell_id. 16 is comfortably above any plausible transient (the debounce
 * needs only 2 consecutive good samples) and is reached in milliseconds, so a
 * genuinely dead bus is abandoned long before it can matter. */
#ifndef TOUCH_BUS_FAIL_LIMIT
#define TOUCH_BUS_FAIL_LIMIT 16u
#endif

/* ------------------------------------------------------------------------
 * BUS-LOSS RECOVERY -- the latch above is no longer for the rest of the boot.
 *
 * Silicon 2026-09-24 (docs/evidence/2026-09-w3/touch_cal_20260924.txt section
 * 1): about 2.7 h after a card boot the bus failed 16 times IN A ROW, the latch
 * fired, and touch stayed dead until a `reboot` re-ran touch_init() -- with
 * nothing on the wire saying so. What wedged it is not known.
 *
 * So while latched, touch_poll() now RETRIES THE BRING-UP on a slow back-off:
 * the first attempt TOUCH_RECOVER_FAST_MS after the latch fired, then every
 * TOUCH_RECOVER_FAST_MS for TOUCH_RECOVER_FAST_TRIES attempts, then every
 * TOUCH_RECOVER_SLOW_MS for as long as it takes. An attempt is the bring-up
 * touch_init() does, minus what does not belong in a liveness retry:
 *   1. the AXI IIC core soft reset + enable (touch_init()'s first four writes),
 *      then CHIP_ID -- a dead bus ends the attempt HERE, after one bounded wait;
 *   2. Arm's Touch_V2M-MPS3 programming sequence, the SAME table stmpe811_init()
 *      writes (touch.c STMPE_ARM_SEQ), with Arm's two 10 ms settles;
 *   3. TSC_CTRL read back with EN set -- the success criterion.
 * NOT repeated: the panel-continuity probe (a one-shot bring-up question that
 * drives the touch lines as GPIOs; its boot-time words stay), and the
 * calibration re-seed (a `touch_cal set` made at runtime SURVIVES a recovery).
 *
 * THE BUDGET: an attempt is spread over consecutive touch_poll() calls, ONE I2C
 * transaction per call, and the two settles are waited out ACROSS calls on the
 * clock, never spun. So a recovering call costs at most one transaction -- at
 * most three bounded waits (3 * TOUCH_IIC_WAIT_US) plus a few dozen register
 * accesses, and on a still-dead bus exactly one wait -- which is inside the
 * bound one ordinary failing poll already has (test_touch.c pins both). Between
 * attempts a latched touch_poll() issues NO I2C at all, as before. Any failure
 * ends the attempt at once: an attempt never pays more than one timeout.
 *
 * A bus that flaps (recovers, then latches again inside TOUCH_RECOVER_STABLE_MS)
 * keeps its back-off position instead of starting fast again, so a marginal bus
 * cannot buy itself 16 failing polls every 5 s forever.
 *
 * If the wedge is a slave holding SDA low mid-byte, no software on this core can
 * clear it (the AXI IIC has no bus-clear); the attempts then keep failing at
 * the slow rate, and `stats` shows it: touch_ok false, touch_bus_lost above
 * touch_recoveries (net-protocol.md "Stats").
 * ------------------------------------------------------------------------ */
#ifndef TOUCH_RECOVER_FAST_MS
#define TOUCH_RECOVER_FAST_MS    5000u
#endif
#ifndef TOUCH_RECOVER_FAST_TRIES
#define TOUCH_RECOVER_FAST_TRIES 6u
#endif
#ifndef TOUCH_RECOVER_SLOW_MS
#define TOUCH_RECOVER_SLOW_MS    30000u
#endif
#ifndef TOUCH_RECOVER_STABLE_MS
#define TOUCH_RECOVER_STABLE_MS  60000u
#endif
/* Arm's delay_ms(10) after SYS_CTRL1 soft reset and after ADC_CTRL1 -- as a
 * measured duration here (the one-shot init still spins TOUCH_SETTLE_ITERS). */
#ifndef TOUCH_RECOVER_SETTLE_US
#define TOUCH_RECOVER_SETTLE_US  10000u
#endif

/* Liveness, as `stats` reports it (touch_ok / touch_bus_lost /
 * touch_recoveries). Counters are since the image started (touch_init() does
 * not clear them, like the s_dbg_* statics they mirror). */
typedef struct {
    int      ok;          /* touch_poll() is live: the part was confirmed and
                           * the bus is not latched lost                        */
    uint32_t bus_lost;    /* times the bus-loss latch fired                     */
    uint32_t recoveries;  /* latches cleared by a successful re-init attempt    */
    uint32_t attempts;    /* re-init attempts started (JTAG: s_dbg_rec_tries)   */
} touch_health_t;

/* ------------------------------------------------------------------------
 * PANEL-CONTINUITY PROBE -- "chip or panel?" as a number.
 *
 * Silicon 2026-09-09: the STMPE811 answers CHIP_ID 0x0811, all init writes ACK,
 * TSC_CTRL reads back EN=1, 321k polls with ZERO I2C errors -- and across 79k
 * polls under a confirmed human press TSC_CTRL.TSC_STA NEVER asserted, max_z 0.
 *
 * Silicon 2026-09-14: Arm's DEFAULT MPS3 image detects touches on this same
 * panel. So the panel, the flex, the part and the I2C wiring are all good, and
 * the "(B) the panel is not electrically connected" hypothesis this probe was
 * built to test is DEAD. The fault was on our side of the bus, and touch.c's
 * init sequence is now Arm's, byte for byte, with every deviation named.
 *
 * The probe survives that because half of it -- the read-backs -- answers a
 * question that still matters: did the writes take effect on the part? What it
 * no longer does is pronounce on the panel.
 *
 * touch_init() does two ONE-SHOT things, before the TSC is enabled and NEVER in
 * the poll loop:
 *   1. READ BACK the five registers that decide whether a touch can even be
 *      seen -- GPIO_AF (0x17), SYS_CTRL2 (0x04), TSC_CFG (0x41), ADC_CTRL1
 *      (0x20), TSC_I_DRIVE (0x58) -- and compare each against the literal the
 *      init sequence wrote, under a mask of the bits the datasheet DEFINES
 *      (ONE source for both: the STMPE_INIT_* values in touch.c). A silent
 *      NACK, a chip that ignores a write, or a reset that undoes one all show
 *      up here as a difference.
 *   2. RUN A CONTINUITY PROBE on the four touch lines with the TSC disabled:
 *      drive one end of a plate as a GPIO output and convert the OTHER end of
 *      the SAME plate as an ADC input. The configuration is the one ST's
 *      Table 4 defines (GPIO_AF bit 0 + TSC_CTRL.EN 0 -> the pin IS an ADC
 *      input); each line is sampled with its partner driven HIGH and then LOW,
 *      and "follows" means the two readings SEPARATE. A floating input reads
 *      the same both ways whatever rail it happens to sit at.
 *
 * ==== WHAT THE 2026-09-14 SILICON RUN ACTUALLY MEASURED, AND WHY IT LIED ====
 * It reported verdict 2 PANEL_OPEN on driven-high samples X+ 1188, X- 1202,
 * Y- 1068, Y+ 9, and that was read as "the panel's Y+ line is open". It was
 * measuring the wrong pins. The STMPE811 numbers its ADC channels differently
 * from its GPIOs: the four TOUCH lines are ADC channels 0..3 (CH0 X+, CH1 X-,
 * CH2 Y+, CH3 Y-) while GPIO-4..7 are the touch PINS, and ADC channels 4..7 are
 * IN0..IN3 -- ordinary inputs with nothing on them. The probe converted 4..7.
 * Worse, the then-current GPIO_AF of 0x0F had muxed exactly those four pins
 * into GPIO mode, so they were not ADC inputs at all. Four meaningless numbers,
 * one confident verdict, and a wave of effort aimed at a flex connector.
 *
 * The result is four mailbox words (firmware/common/diag.h, +0x6C..0x78; also
 * `pyverify diag` keys touch_regs / touch_adc_x / touch_adc_y / touch_verdict)
 * so the answer is readable over JTAG or the wire BEFORE anyone opens the case.
 * ------------------------------------------------------------------------ */

/* touch_probe_verdict() -- the one number. The RULE, in evaluation order:
 *   TOUCH_VERDICT_CHIP_BAD  a read-back DIFFERS, in a bit the datasheet
 *                           DEFINES, from what init wrote. Nothing below it can
 *                           be trusted, so it is decided first. This is not an
 *                           inference: a documented R/W field that does not
 *                           read back is a fact about the part or the bus.
 *   TOUCH_VERDICT_UNKNOWN   the probe could not run, could not complete, or
 *                           completed with a line that did not follow its
 *                           partner's drive. "I do not know" is a legal answer
 *                           and must never be dressed up as one of the others.
 *   TOUCH_VERDICT_OPEN      NEVER EMITTED. Retained only because the diag
 *                           mailbox and `pyverify` decode this encoding and it
 *                           is frozen at v8. See below.
 *   TOUCH_VERDICT_PRESENT   every one of the four lines followed BOTH drives.
 *
 * WHY "OPEN" IS NO LONGER REACHABLE. The verdict is deliberately ASYMMETRIC,
 * because the evidence is. A line that DOES follow its partner both ways can
 * only be doing so through a conductive path between the two plate terminals:
 * that is a positive measurement and PRESENT is a fair reading of it. A line
 * that does NOT follow has several innocent explanations the STMPE811 datasheet
 * cannot rule out -- it describes the internal "driver and switch control unit"
 * in a single paragraph and never states what the analogue switches on
 * X+/X-/Y+/Y- do during an ADC capture, and it never gives the ADC's internal
 * reference voltage either. The negative case is therefore reported as UNKNOWN
 * with TOUCH_PROBE_ST_FOLLOW_BAD set and the raw samples still in the mailbox,
 * so the reader sees the measurement and draws their own conclusion. The
 * previous code called that case PANEL_OPEN, it was fielded, and it was wrong.
 */
#define TOUCH_VERDICT_UNKNOWN  0u
#define TOUCH_VERDICT_CHIP_BAD 1u
#define TOUCH_VERDICT_OPEN     2u
#define TOUCH_VERDICT_PRESENT  3u

/* How far apart the driven-HIGH and driven-LOW readings of a line must be for
 * it to count as following its partner, on the 12-bit (0..4095) ADC.
 *
 * A SEPARATION, not a pair of absolute thresholds. The previous rule was
 * "driven high reads >= 3072 AND driven low reads <= 1024", which assumed the
 * ADC's full scale is the pin's supply rail -- the datasheet never says what
 * the internal reference is, so that assumption had nothing behind it and the
 * numbers it produced could not be interpreted. A DIFFERENCE needs no reference
 * at all, and it is exactly the discriminator that matters: an open line reads
 * whatever it floats at, but it reads the SAME thing whichever way its partner
 * is driven. 1024 = a quarter of full scale, deliberately loose (a real plate
 * is hundreds of ohms of film and the test feeds 3990/105, not 4095/0). */
#ifndef TOUCH_PROBE_FOLLOW_MIN
#define TOUCH_PROBE_FOLLOW_MIN 1024u
#endif

/* Bounded wait for one ADC conversion (each poll is an I2C register read, so
 * 256 is ~10 ms at 100 kHz -- a 12-bit conversion at ADC_CTRL2=0x01 takes tens
 * of microseconds; the datasheet's Table 14 puts the longest sample time at
 * 19.1 us at 6.5 MHz). Fails closed: a conversion that never completes sets
 * TOUCH_PROBE_ST_ADC_ERR and the verdict stays UNKNOWN. */
#ifndef TOUCH_PROBE_CONV_POLLS
#define TOUCH_PROBE_CONV_POLLS 256
#endif

/* Status byte, carried in touch_probe_adc_y_word() bits [31:24]. It is what
 * makes an UNKNOWN verdict diagnosable instead of merely disappointing. */
#define TOUCH_PROBE_ST_RAN        0x01u  /* the probe executed to completion    */
#define TOUCH_PROBE_ST_RB_ERR     0x02u  /* a read-back register read failed    */
#define TOUCH_PROBE_ST_RB_DIFF    0x04u  /* a read-back differed from the write */
#define TOUCH_PROBE_ST_ADC_ERR    0x08u  /* bus error / conversion timeout      */
#define TOUCH_PROBE_ST_FOLLOW_BAD 0x10u  /* >=1 line did not follow its drive   */

/* The four mailbox words, packed. main.c publishes these into the diag mailbox
 * every superloop pass; on the target they are ALSO plain statics readable over
 * JTAG (mb-nm -S + xsdb mrd), like the s_dbg_* counters.
 *
 *   regs  : [7:0] GPIO_AF  [15:8] SYS_CTRL2  [23:16] TSC_CFG  [31:24] ADC_CTRL1
 *   adc_x : [11:0] X+      [23:12] X-        [31:24] TSC_I_DRIVE
 *   adc_y : [11:0] Y+      [23:12] Y-        [31:24] TOUCH_PROBE_ST_*
 *
 * TSC_I_DRIVE is the FIFTH read-back and it rides in adc_x's spare byte for one
 * honest reason: the mailbox had five reserved words and this probe is allowed
 * four, so 5 register bytes + 4 twelve-bit samples + a verdict are packed into
 * 4 words rather than 5. The samples are the DRIVEN-HIGH readings; the
 * driven-LOW half of each pair is folded into the verdict and the status byte
 * (there is no room for eight samples, and the conclusion is what a reader
 * needs at 2am). */
uint32_t touch_probe_regs_word(void);
uint32_t touch_probe_adc_x_word(void);
uint32_t touch_probe_adc_y_word(void);
uint32_t touch_probe_verdict(void);

/* ------------------------------------------------------------------------
 * Affine calibration seam: screen = (coeff . raw) >> shift, then the optional
 * 180-degree flip. touch_set_calibration() replaces the coefficients from a
 * bench 3-point recal; touch_init() seeds the identity-ish defaults below.
 *
 *   x_cal = (ax*rx + bx*ry + cx) >> shift    (the plan's A,B,C,S)
 *   y_cal = (ay*rx + by*ry + cy) >> shift    (the plan's D,E,F,S)
 * ------------------------------------------------------------------------ */
typedef struct {
    int32_t ax, bx, cx;   /* x row: A, B, C */
    int32_t ay, by, cy;   /* y row: D, E, F */
    uint8_t shift;        /* S -- fixed-point denominator = 1<<shift */
} touch_calib_t;

/* THE DEFAULT CALIBRATION, as one header constant (an initializer, so both
 * touch.c's seed and any host tool can use it). It is the SILICON FIT of
 * 2026-09-24 (docs/evidence/2026-09-w3/touch_cal_20260924.txt section 4), the
 * `touch_cal set 21 377 -158863 -281 -2 1091113 12` installed at runtime on
 * 0x72BB0A36 -- in touch_calib_t's order, which is also the verb's:
 * ax bx cx ay by cy shift.
 *
 * A least-squares affine over six taps on CHARACTERS at known pixels, not
 * corners: the panel's axes are SWAPPED as well as mirrored (raw_y drives screen
 * X -- bx carries it -- and raw_x drives screen Y -- ay carries it), and corner
 * presses lie on one diagonal, which cannot reveal a swap. The numbers are
 * PRE-FLIP: touch_map_raw() applies CLCD_ROTATE_180 after them, as it did when
 * they were measured, so they belong to a CLCD_ROTATE_180=1 build (both
 * engines). Residual: worst 10 px, mean 8 px, all of it in Y; every tap lands on
 * the text ROW it was aimed at. The six taps reproduce the evidence's residual
 * table exactly through this map (test_touch.c test_affine_map pins all six).
 *
 * Replaced the identity {320,0,0,0,240,0,12}, under which a top-left press
 * landed top-RIGHT (2026-09-22 "touch detected, taps miss"). In range for
 * touch_calib_invalid_reason(): |ax|,|bx|,|ay|,|by| <= 377 (limit 2^17),
 * |cx|,|cy| < 2^21 (limit 2^29), det = ax*by - bx*ay = 105895 != 0.
 * `touch_cal set` still overrides it at runtime (RAM only), and `touch_cal
 * default` comes back here. Overridable with -DTOUCH_CALIB_DEFAULT='{...}'. */
#ifndef TOUCH_CALIB_DEFAULT
#define TOUCH_CALIB_DEFAULT { /* ax */ 21,   /* bx */ 377, /* cx */ -158863, \
                              /* ay */ -281, /* by */ -2,  /* cy */ 1091113, \
                              /* shift */ 12 }
#endif

/* The range touch_calib_valid() accepts (and the `touch_cal set` verb enforces).
 * Chosen so touch_map_raw()'s int32 arithmetic can never overflow for any 12-bit
 * raw pair: |ax|,|bx|,|ay|,|by| <= 2^17 keeps each product <= 2^29 (raw < 2^12),
 * the two-term sum <= 2^30, and |cx|,|cy| <= 2^29 keeps the total < 2^31.
 * shift <= 24 keeps the right shift defined and still leaves 12+ fractional
 * bits for any fit a 320x240 panel needs. */
#define TOUCH_CAL_COEF_MAX   (1L << 17)
#define TOUCH_CAL_OFFSET_MAX (1L << 29)
#define TOUCH_CAL_SHIFT_MAX  24

/* Validate a candidate calibration. Returns 0 if usable, else a short static
 * reason ("bad shift", "coef range", "offset range", "singular"). SINGULAR (the
 * 2x2 linear part has a zero determinant) is rejected because it collapses the
 * whole panel onto a line: every tap would land on the same row or column and
 * the only way back would be a reboot. */
const char *touch_calib_invalid_reason(const touch_calib_t *cal);

/* ------------------------------------------------------------------------
 * Public API -- the two superloop hooks + the calibration/map seams.
 * ------------------------------------------------------------------------ */

/* Bring up the AXI IIC master (soft-reset, program the dynamic RX-count IRQ
 * depth, enable) and seed the default calibration + debounce state. Bounded and
 * non-blocking; call once during shell bring-up (behind MPS3_HAS_TOUCH). */
void touch_init(void);

/* The STMPE811 CHIP_ID captured at touch_init(): 0x0811 = the touch controller
 * is present and ACKing at STMPE811_ADDR (0x41); 0 = nothing ACKed (A0 strapped
 * high -> try 0x44, or the bus/pull-ups are dead). A cheap on-silicon bring-up
 * confirmation the shell can log without a full I2C scan. */
uint16_t touch_chip_id(void);

/* Bring-up bus witness: sweep 7-bit I2C addresses, returning how many ACK and
 * filling `found` with up to `max`. Call AFTER touch_init() (which enables the
 * AXI IIC core), ONCE, at bring-up -- never in the superloop. Distinguishes
 * "part at 0x41" / "part at 0x44 (A0=VCC)" / "dead bus, nothing ACKs". */
unsigned touch_bus_scan(uint8_t *found, unsigned max);

/* One bounded, non-blocking step. Returns the clcd_action_t (see clcd/clcd.h)
 * of the on-screen button a debounced tap lands on -- CLCD_ACT_NEXT_PAGE for
 * the bottom nav bar -- or CLCD_ACT_NONE (0) when there is no new tap. Gated on
 * touch_tint_asserted(): a no-touch pass is a single register read. Dispatch is
 * EDGE-triggered (once per contact), so a held finger does not repeat. */
int touch_poll(void);

/* Install bench calibration coefficients (3-point recal). Coefficients are used
 * verbatim; the 180-flip (CLCD_ROTATE_180) is applied AFTER, on top. Returns 0
 * on success, -1 (and changes NOTHING) if touch_calib_invalid_reason() rejects
 * them -- a NULL or unsafe calibration is never installed. */
int touch_set_calibration(const touch_calib_t *cal);

/* The calibration currently in force (copy). Before touch_init() this is all
 * zeroes, which is also what the static reads over JTAG. */
void touch_get_calibration(touch_calib_t *out);

/* The last RAW sample the TSC delivered (any read that returned a converted
 * X/Y/Z, whatever its pressure, before debounce or mapping), and how many such
 * samples have been captured since boot. This is what a three-point calibration
 * capture reads: press a known target, read `raw`, confirm `seen` advanced.
 * The same values are the volatile statics s_dbg_raw_x / s_dbg_raw_y /
 * s_dbg_raw_z / s_dbg_raw_seen in touch.c, so a JTAG read of the ELF symbols
 * and the 6900 `touch_cal raw` verb report the same numbers by construction. */
void touch_raw_sample(uint16_t *rx, uint16_t *ry, uint16_t *rz, uint32_t *seen);

/* Last logical pixel a DISPATCHED tap mapped to (s_last_x / s_last_y). */
void touch_last_xy(uint16_t *x_px, uint16_t *y_px);

/* Liveness + the bus-loss / recovery counters (touch_health_t above). What the
 * `stats` verb reports on a TOUCH=1 build of either engine. */
void touch_health(touch_health_t *out);

/* ONE bus wait, bounded in TIME as well as iterations (TOUCH_IIC_WAIT_US). The
 * iteration bound alone was a guess at a duration: on silicon a held finger
 * produced 65-150 ms clcd passes with poll_err 0 -- consistent with a best-effort
 * (void) register write paying the full 100000-iteration bound. 3 ms is ~10x the
 * longest real wait at 100 kHz (a 4-byte read: address + register + address +
 * 4 data bytes ~ 0.6 ms). */
#ifndef TOUCH_IIC_WAIT_US
#define TOUCH_IIC_WAIT_US 3000u
#endif

/* Map a raw (rx,ry) TSC reading to a logical screen pixel via the current
 * calibration + the CLCD_ROTATE_180 flip. Pure; the result is clamped to the
 * panel. Exposed for calibration tooling and the unit test. */
void touch_map_raw(uint16_t rx, uint16_t ry, uint16_t *x_px, uint16_t *y_px);

/* Pen-down gate. WEAK (touch.c) so the host test and the fabric mint can each
 * supply the real source. Returns 1 while the TSC's pen interrupt is asserted
 * (a touch in progress), 0 otherwise. */
int touch_tint_asserted(void);

/* ------------------------------------------------------------------------
 * Test-only introspection (host harness). Never compiled into the target.
 * ------------------------------------------------------------------------ */
#ifdef MPS3_TOUCH_TEST_HOOKS
/* Exercise the REAL AXI-IIC dynamic master read directly (so the test can pin
 * the TX-FIFO framing and RX drain without going through the TSC shim). */
int touch_test_iic_read(uint8_t addr7, const uint8_t *cmd, unsigned cmdlen,
                        uint8_t *buf, unsigned n);
/* Exercise the REAL AXI-IIC dynamic master WRITE directly (the STMPE811 init
 * path), so the test can pin the write framing without going through touch_init. */
int touch_test_iic_write(uint8_t addr7, const uint8_t *data, unsigned len);
/* Last logical pixel the most recent dispatched tap mapped to. */
void touch_test_last_xy(uint16_t *x_px, uint16_t *y_px);
/* The bring-up telemetry counters, so the host test can assert the witness
 * reports what actually happened. On the target these are read over JTAG from
 * their static symbols instead -- no target-side accessor exists, or is wanted. */
void touch_test_counters(uint32_t *poll_ok, uint32_t *poll_err,
                         uint32_t *sta_seen, uint32_t *bus_lost);
#endif

#ifdef __cplusplus
}
#endif

#endif /* MPS3_TOUCH_H */
