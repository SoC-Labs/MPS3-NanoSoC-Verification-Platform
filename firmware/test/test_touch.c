/*
 * test_touch.c -- host-gcc tests for the Phase-2 resistive-touch driver
 * (firmware/touch/touch.c), now targeting the STMicro STMPE811QTR. Built
 * -DMPS3_HAL_MOCK -DMPS3_HAS_CLCD -DMPS3_HAS_TOUCH -DMPS3_TOUCH_TEST_HOOKS,
 * linking the REAL touch.c and the REAL clcd.c (for clcd_hittest) against mock
 * registers.
 *
 * The mock over MPS3_TOUCH_BASE is a faithful little AXI IIC (PG090)
 * dynamic-controller model that also understands the STMPE811 register map:
 *   - it LOGS every TX_FIFO write (so we can pin the exact master read/write
 *     framing), tracks the register pointer written before a repeated-start
 *     read, and serves register-appropriate RX bytes:
 *       CHIP_ID  (0x00) -> 0x08 0x11
 *       TSC_CTRL (0x40) -> (touched ? TSC_STA) | EN
 *       TSC_DATA (0xD7) -> one 4-byte packed X/Y/Z sample (Table 16 layout)
 *       any other reg with a RAW payload set -> that payload (generic-read test).
 *   - touch_tint_asserted() is overridden (strong def below) so the test drives
 *     the pen-down gate directly.
 *
 * What these prove:
 *   1. The AXI-IIC dynamic master READ issues the right register writes and
 *      drains the canned bytes; n==0 is rejected.
 *   2. The AXI-IIC dynamic master WRITE issues START|waddr, data..., last|STOP,
 *      and reports success when the slave ACKs.
 *   3. touch_init() confirms the STMPE811 (CHIP_ID -> touch_chip_id()==0x0811).
 *   4. The affine + 180 flip maps known raw values to EXACT logical pixels.
 *   5. A debounced pen-down in the bottom nav row -> CLCD_ACT_NEXT_PAGE; a miss
 *      dispatches but returns NONE; pen-up / below-floor pressure -> NONE;
 *      debounce takes N samples and a tap fires ONCE per contact.
 *   6. clcd.c's ONE-TIME touch bring-up runs on every path -- including the one
 *      where the render FSM never passes ST_RESET (relinquished at the first
 *      poll, then clcd_regain() -> ST_INIT) -- so touch works after regain.
 *      Built a second time as test_touch_kvm (+ -DMPS3_HAS_CLCD_KVM and
 *      ../clcd_kvm/clcd_kvm.c), the SAME file also drives the literal path: the
 *      DUT owns the panel at the first poll, the KVM hands it back, a tap works.
 */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../touch/touch.h"
#include "../clcd/clcd.h"
#include "../common/platform_regs.h"
#include "../coordinator/coordinator.h"
#include "../coordinator/swap_fsm.h"
#include "../common/diag.h"
#include "../smsc911x/smsc911x.h"
#include "mock_regs.h"
#ifdef MPS3_HAS_CLCD_KVM
#include "../clcd_kvm/clcd_kvm.h"
#endif

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

/* ==== symbols clcd.c links against (the firmware/test/ fake pattern) ======== */
mps3_shell_state_t   g_shell_state;
volatile mps3_diag_t g_mps3_diag;
static mps3_swap_result_t s_swap_res;
const mps3_swap_result_t *swap_fsm_last_result(void) { return &s_swap_res; }
static uint32_t s_icap_bytes;
uint32_t swap_fsm_icap_bytes(void) { return s_icap_bytes; }
static int s_link_up; static uint16_t s_anlpar;
int smsc911x_link_up(void) { return s_link_up; }
int smsc911x_mii_read(uint32_t reg, uint16_t *v) { if (v) *v = (reg==0x05u)?s_anlpar:0u; return 0; }
static uint8_t s_mac[6] = { 0x02, 0x00, 0x00, 0x4D, 0x50, 0x53 };
void mps3_platform_mac(uint8_t mac[6]) { memcpy(mac, s_mac, 6); }

/* ==== strong override of the WEAK pen-down gate ============================= */
static int s_tint;
int touch_tint_asserted(void) { return s_tint; }

/* ==== AXI IIC (PG090) + STMPE811 mock ====================================== */
#define IIC_TXLOG_MAX 16u
static struct {
    uint32_t tx_log[IIC_TXLOG_MAX];
    unsigned tx_n;
    uint8_t  rxq[16];
    unsigned rq_head, rq_tail;
    uint32_t isr;
    uint32_t bb;             /* bus-busy to report on SR (0 = free)          */
    int      touched;        /* STMPE811 TSC_CTRL.TSC_STA (a touch present)  */
    uint16_t chip_id;        /* value served on a CHIP_ID (0x00) read        */
    uint16_t xyz[3];         /* X,Y (12-bit) + Z (8-bit) served on TSC_DATA  */
    uint8_t  raw[16];        /* generic RAW payload (for the read-framing test)*/
    unsigned raw_n;
    int      reg_ptr;        /* last register pointer written                 */
    int      reg_have;       /* a register pointer has been captured          */
    int      read_pending;   /* a repeated-start read-address was written     */
    uint32_t last_pirq;      /* last RX_FIFO_PIRQ written                     */
    int      bus_dead;       /* part stops answering: serve no RX bytes ever   */
    int      ack_addr;       /* 7-bit address that ACKs; others NACK (0 = all ACK) */
    int      dead_reg;       /* register whose reads never return data (0 = none)  */
    int      cur_addr;       /* 7-bit address of the transaction in flight        */
    /* ---- STMPE811 REGISTER FILE + ADC/GPIO model (the panel-probe fake) ----
     * Writes LAND here and reads are served from here, so a read-back sees what
     * the driver actually wrote -- which is the whole point of the probe. The
     * three synthetic registers above (CHIP_ID 0x00, TSC_CTRL 0x40, TSC_DATA
     * 0xD7) keep their canned behaviour and are NOT served from this file. */
    uint8_t  regs[256];
    int      sticky_reg;     /* a register that IGNORES writes (0 = none) --   */
    uint8_t  sticky_val;     /* ... it always reads back sticky_val            */
    uint8_t  gpio_out;       /* GPIO output data, from SET_PIN/CLR_PIN (W1S/W1C) */
    int      panel;          /* PANEL_CONNECTED or PANEL_OPEN                   */
    uint16_t float_code;     /* what an OPEN (floating) ADC input reads         */
    uint16_t plate_hi;       /* connected plate, partner driven HIGH            */
    uint16_t plate_lo;       /* connected plate, partner driven LOW             */
    unsigned adc_busy;       /* ADC_CAPT reads still reporting "in progress"    */
    int      adc_stuck;      /* conversion never completes (CAPT bit never clears) */
    int      adc_capt_sticky;/* SILICON 2026-09-14: CAPT never clears, but ADC_INT_STA[n] sets */
    uint8_t  adc_pending;    /* channels whose completion has not yet been signalled  */
    unsigned adc_capt_n;     /* ADC_CAPT writes seen (proves the probe is ONE-SHOT) */
    uint8_t  capt_mask_seen; /* OR of every channel mask ever started          */
    unsigned wr_seq;         /* monotonic register-write counter                */
    unsigned seq_tsc_en;     /* wr_seq of the TSC_CTRL EN=1 write               */
    unsigned seq_last_capt;  /* wr_seq of the LAST ADC_CAPT write               */
    unsigned seq_int_sta_w;  /* wr_seq of the last ADC_INT_STA write (the re-arm) */
    unsigned capt_no_rearm;  /* ADC_CAPT writes NOT immediately preceded by a re-arm */
    uint8_t  fifo_size;      /* STMPE811 FIFO_SIZE (0x4C) served while touched  */
    unsigned d7_reads;       /* reads of the 0xD7 sample port                   */
    unsigned fifo_resets;    /* FIFO_STA <- 0x01 writes                         */
    unsigned int_sta_clears; /* INT_STA <- 0x1F writes (Arm's per-poll clear)   */
    unsigned softr_n;        /* AXI IIC soft resets = touch_init() calls + recovery attempts */
    unsigned stops;          /* TX_FIFO words with STOP = I2C transactions issued */
    uint16_t wlog[64];       /* landed register writes, (reg << 8) | val, in order */
    unsigned wlog_n;
} iic;

/* The two physical cases the probe exists to tell apart. */
#define PANEL_OPEN      0   /* flex not connected: the line floats, ignoring the drive */
#define PANEL_CONNECTED 1   /* a real 4-wire plate: the read end FOLLOWS the driven end */

/* STMPE811 registers the model interprets (touch.c owns the same numbers). */
#define R_GPIO_SET   0x10u
#define R_GPIO_CLR   0x11u
#define R_GPIO_DIR   0x13u
#define R_GPIO_AF    0x17u
#define R_ADC_CAPT   0x22u
#define R_ADC_INT_STA 0x0Fu
#define R_ADC_DATA(ch) (0x30u + 2u * (ch))
#define R_TSC_CTRL   0x40u
#define R_TSC_SHIELD 0x59u
#define R_FIFO_STA   0x4Bu
#define R_FIFO_SIZE  0x4Cu
#define R_INT_STA    0x0Bu
#define R_INT_EN     0x0Au

/* ======================= THE PART'S TWO NUMBERINGS =========================
 * The STMPE811 numbers its ADC channels differently from its GPIO pins, and
 * modelling that faithfully is the whole point of this block: the driver bug
 * fixed on 2026-09-14 was "channel n == GPIO n", and the OLD fake made the same
 * assumption, which is exactly why it could not catch it.
 *
 * Doc ID 14489 Rev 5, Table 2 (pin assignments, p.7) and Table 13 (ADC channel
 * summary, p.30):
 *     ADC CH0 = X+/GPIO-4   CH1 = X-/GPIO-6   CH2 = Y+/GPIO-5   CH3 = Y-/GPIO-7
 *     ADC CH4 = IN0/GPIO-0  CH5 = IN1/GPIO-1  CH6 = IN2/GPIO-2  CH7 = IN3/GPIO-3
 * ========================================================================== */
static const int CH_GPIO[8] = { 4, 6, 5, 7, 0, 1, 2, 3 };

/* Same-plate partner of each GPIO PIN: X+ (GPIO-4) <-> X- (GPIO-6),
 * Y+ (GPIO-5) <-> Y- (GPIO-7). A plate is continuous between a pin and its
 * partner and open to everything else; IN0..IN3 (GPIO-0..3) are not on the
 * panel at all, so they have no partner and can never follow anything. */
static const int PLATE_PARTNER[8] = { -1, -1, -1, -1, 6, 7, 4, 5 };

/* One conversion, per the physical model. A connected plate is a few hundred
 * ohms of film, so the read end sits at the driven rail (3990 / 105 -- NOT
 * 4095/0: the check must not be an identity check). An open flex floats at
 * float_code no matter what the partner is doing. */
static uint16_t mock_adc_sample(unsigned ch)
{
    uint8_t af  = iic.regs[R_GPIO_AF];
    uint8_t dir = iic.regs[R_GPIO_DIR];
    int g, p;

    if (ch > 7u) return 0u;
    g = CH_GPIO[ch];

    /* "If TSC is enabled, CH3-0 is used for TSC and all readings to these
     * channels give 0x0000" (Rev 5, ADC_DATA_CHn, p.33). */
    if (ch < 4u && (iic.regs[R_TSC_CTRL] & 0x01u)) return 0u;

    /* GPIO_AF bit set = the pin is a GPIO, so it is not an ADC input at all
     * ("'0' sets the corresponding pin to function as touchscreen/ADC, and '1'
     * sets it into GPIO mode", Rev 5 p.53). */
    if (af & (uint8_t)(1u << g)) return 0u;

    p = PLATE_PARTNER[g];
    if (p < 0) return iic.float_code;          /* nothing on the other end     */
    if (!((af & (uint8_t)(1u << p)) && (dir & (uint8_t)(1u << p)))) {
        return iic.float_code;                 /* nothing is driving the plate */
    }
    if (iic.panel != PANEL_CONNECTED) return iic.float_code;
    return ((iic.gpio_out >> p) & 1u) ? iic.plate_hi : iic.plate_lo;
}

static void mock_adc_capt(uint8_t chmask)
{
    unsigned ch;
    iic.adc_capt_n++;
    iic.capt_mask_seen |= chmask;
    iic.seq_last_capt = iic.wr_seq;
    /* The driver must clear this channel's ADC_INT_STA bit IMMEDIATELY before
     * starting the capture. Without that re-arm a stale done-bit reads as this
     * conversion completing, and since ADC_INT_STA's reset value is 0x00 but
     * every previous conversion sets a bit, "stale" is the normal case. */
    if (iic.seq_int_sta_w + 1u != iic.wr_seq) iic.capt_no_rearm++;
    for (ch = 0u; ch < 8u; ch++) {
        if (chmask & (uint8_t)(1u << ch)) {
            uint16_t v = mock_adc_sample(ch);
            iic.regs[R_ADC_DATA(ch)]      = (uint8_t)(v >> 8);
            iic.regs[R_ADC_DATA(ch) + 1u] = (uint8_t)(v & 0xFFu);
        }
    }
    /* ADC_CAPT, faithfully: "Reads '1' if conversion is completed. Reads '0' if
     * conversion is in progress" (Rev 5 p.32) -- the OPPOSITE sense to a busy
     * flag -- with a RESET VALUE of 0xFF. adc_capt_sticky models what silicon
     * actually showed (0xFF throughout, because a 12-bit conversion finishes in
     * tens of microseconds, long before the next I2C read can observe it). */
    iic.regs[R_ADC_CAPT] = iic.adc_capt_sticky ? 0xFFu : (uint8_t)(0xFFu & ~chmask);
    iic.adc_pending = chmask;
    iic.adc_busy = iic.adc_stuck ? 0xFFFFFFFFu : 1u;   /* ... for this many reads */
}

/* A register write that ACTUALLY lands (the addressed slave ACKed). */
static void mock_reg_write(int reg, uint8_t v)
{
    iic.wr_seq++;
    if (iic.wlog_n < 64u) iic.wlog[iic.wlog_n++] = (uint16_t)(((unsigned)reg << 8) | v);
    if (iic.sticky_reg && reg == iic.sticky_reg) {
        iic.regs[reg] = iic.sticky_val;         /* a chip that ignores the write */
        return;
    }
    iic.regs[reg] = v;
    if      (reg == (int)R_GPIO_SET)  iic.gpio_out |= v;
    else if (reg == (int)R_GPIO_CLR)  iic.gpio_out &= (uint8_t)~v;
    else if (reg == (int)R_ADC_CAPT)  mock_adc_capt(v);
    else if (reg == (int)R_ADC_INT_STA) {
        iic.regs[R_ADC_INT_STA] &= (uint8_t)~v;   /* W1C */
        iic.seq_int_sta_w = iic.wr_seq;
    }
    else if (reg == (int)R_TSC_CTRL && (v & 1u)) iic.seq_tsc_en = iic.wr_seq;
    else if (reg == (int)R_FIFO_STA && (v & 1u)) iic.fifo_resets++;
    else if (reg == (int)R_INT_STA && v == 0x1Fu) iic.int_sta_clears++;
}

static void     rq_push(uint8_t b)  { iic.rxq[iic.rq_tail & 15u] = b; iic.rq_tail++; }
static int      rq_empty(void)      { return iic.rq_head == iic.rq_tail; }
static uint8_t  rq_pop(void)        { uint8_t b = iic.rxq[iic.rq_head & 15u]; iic.rq_head++; return b; }
static unsigned rq_ocy(void)        { return iic.rq_tail - iic.rq_head; }

/* Serve `cnt` RX bytes for a read of STMPE811 register `reg` (-1 = none). */
static void iic_serve(int reg, unsigned cnt)
{
    if (iic.bus_dead) { return; }   /* nothing ever arrives -> stmpe_rd() times out */
    if (iic.dead_reg && reg == iic.dead_reg) { return; }   /* wedge ONE register only */
    if (reg == 0x00) {                              /* CHIP_ID (knob, default 0x0811) */
        for (unsigned i = 0; i < cnt; i++)
            rq_push(i == 0u ? (uint8_t)(iic.chip_id >> 8)
                            : (i == 1u ? (uint8_t)(iic.chip_id & 0xFFu) : 0u));
    } else if (reg == 0x40) {                       /* TSC_CTRL: TSC_STA|EN */
        uint8_t v = (uint8_t)((iic.touched ? 0x80u : 0u) | 0x01u);
        for (unsigned i = 0; i < cnt; i++) rq_push(i == 0u ? v : 0u);
    } else if (reg == (int)R_FIFO_SIZE) {           /* samples available      */
        uint8_t v = (uint8_t)(iic.touched ? iic.fifo_size : 0u);
        for (unsigned i = 0; i < cnt; i++) rq_push(i == 0u ? v : 0u);
    } else if (reg == 0xD7) {                        /* TSC_DATA packed X/Y/Z */
        uint16_t X = iic.xyz[0], Y = iic.xyz[1], Z = iic.xyz[2];
        iic.d7_reads++;
        uint8_t b[4] = {
            (uint8_t)(X >> 4),                              /* X[11:4]         */
            (uint8_t)(((X & 0x0Fu) << 4) | ((Y >> 8) & 0x0Fu)), /* {X[3:0],Y[11:8]} */
            (uint8_t)(Y & 0xFFu),                           /* Y[7:0]          */
            (uint8_t)(Z & 0xFFu),                           /* Z[7:0]          */
        };
        for (unsigned i = 0; i < cnt; i++) rq_push(i < 4u ? b[i] : 0u);
    } else if (iic.raw_n) {                          /* generic RAW payload */
        for (unsigned i = 0; i < cnt && i < iic.raw_n; i++) rq_push(iic.raw[i]);
    } else if (reg >= 0) {                           /* the register FILE */
        /* Auto-incrementing multi-byte read (the STMPE811 auto-increments for
         * every register except the 0xD7 FIFO port, handled above) -- which is
         * how ADC_DATA_CHn's two bytes are read in one transaction. */
        for (unsigned i = 0; i < cnt; i++) rq_push(iic.regs[(reg + (int)i) & 0xFF]);
        if (reg == (int)R_ADC_INT_STA && iic.adc_busy) {
            /* The conversion completes AFTER this read: the driver must poll at
             * least twice, so a probe that assumed instant data would break.
             * Completion is signalled the only way the part can signal it --
             * the per-channel ADC_INT_STA bit -- and ADC_CAPT goes back to
             * "completed" (1), which is its reset state and therefore
             * indistinguishable from "never started". */
            if (--iic.adc_busy == 0u) {
                iic.regs[R_ADC_INT_STA] |= iic.adc_pending;
                iic.regs[R_ADC_CAPT] = 0xFFu;
            }
        }
    } else {
        for (unsigned i = 0; i < cnt; i++) rq_push(0u);
    }
}

static int iic_hook(void *c, int wr, uint32_t b, uint32_t off, uint32_t *val)
{
    (void)c; (void)b;
    if (wr) {
        switch (off) {
        case TOUCH_IIC_SOFTR: iic.softr_n++; return 1;        /* reset: accept */
        case TOUCH_IIC_CR:
            if (*val & IIC_CR_TX_FIFO_RESET) iic.tx_n = 0u;
            return 1;
        case TOUCH_IIC_RX_FIFO_PIRQ: iic.last_pirq = *val; return 1;
        case TOUCH_IIC_ISR: iic.isr &= ~(*val); return 1;     /* W1C */
        case TOUCH_IIC_TX_FIFO:
            if (iic.tx_n < IIC_TXLOG_MAX) iic.tx_log[iic.tx_n++] = *val;
            if (*val & IIC_TX_START) {
                /* Address byte: LSB = R/W. A real AXI-IIC master raises
                 * ISR.TX_ERROR when the addressed slave does not ACK; without
                 * modelling that, EVERY address appears present and a bus scan
                 * returns all 112. */
                iic.cur_addr = (int)((*val >> 1) & 0x7Fu);
                if (iic.ack_addr && iic.cur_addr != iic.ack_addr) {
                    iic.isr |= IIC_ISR_TX_ERROR;
                }
                /* address byte: LSB = R/W. A write-address begins a new txn. */
                if (*val & 1u) { iic.read_pending = 1; }
                else           { iic.read_pending = 0; iic.reg_have = 0; }
                return 1;
            }
            if (*val & IIC_TX_STOP) {
                iic.stops++;
                if (iic.read_pending) {                 /* dynamic read trigger */
                    iic_serve(iic.reg_have ? iic.reg_ptr : -1, *val & 0xFFu);
                    iic.read_pending = 0;
                } else if (iic.reg_have) {
                    /* [reg][val|STOP] -- a REGISTER WRITE. It lands in the file
                     * only if the addressed slave ACKed: a NACKed write that
                     * still changed the model would make the read-back gate
                     * report success on a part that never heard the write. */
                    if (!iic.ack_addr || iic.cur_addr == iic.ack_addr) {
                        mock_reg_write(iic.reg_ptr, (uint8_t)(*val & 0xFFu));
                    }
                }
                iic.reg_have = 0;                       /* txn complete */
                return 1;
            }
            /* plain data byte: the first after a write-address is the reg ptr. */
            if (!iic.read_pending && !iic.reg_have) {
                iic.reg_ptr = (int)(*val & 0xFFu);
                iic.reg_have = 1;
            }
            return 1;
        default: return 1;
        }
    }
    switch (off) {
    case TOUCH_IIC_SR: {
        uint32_t sr = IIC_SR_TX_FIFO_EMPTY;             /* TX always idle here */
        if (iic.bb)       sr |= IIC_SR_BB;
        if (rq_empty())   sr |= IIC_SR_RX_FIFO_EMPTY;
        *val = sr; return 1;
    }
    case TOUCH_IIC_ISR:         *val = iic.isr;      return 1;
    case TOUCH_IIC_RX_FIFO:     *val = rq_empty() ? 0u : rq_pop(); return 1;
    case TOUCH_IIC_RX_FIFO_OCY: *val = rq_ocy();     return 1;
    case TOUCH_IIC_TX_FIFO_OCY: *val = 0u;           return 1;
    default:              *val = 0u;           return 1;
    }
}

static void iic_reset_model(void)
{
    memset(&iic, 0, sizeof(iic));
    iic.reg_ptr = -1;
    iic.chip_id = 0x0811u;                          /* STMPE811 present by default */
    iic.ack_addr = 0;                               /* default: every address ACKs */
    iic.panel = PANEL_CONNECTED;                    /* default: a healthy panel */
    iic.float_code = 0u;                            /* an open input reads the low rail */
    iic.regs[R_ADC_CAPT] = 0xFFu;                   /* documented reset value   */
    iic.fifo_size = 1u;                             /* one sample per press     */
    iic.plate_hi = 3990u; iic.plate_lo = 105u;      /* near, but NOT at, the rails */
    mock_regs_set_hook(MPS3_TOUCH_BASE, iic_hook, 0);
}

/* A nav-row press, as the panel actually reports one. The raw pair is the "hb |"
 * spinner tap (row 14, col 37) of the 2026-09-24 silicon capture
 * (docs/evidence/2026-09-w3/touch_cal_20260924.txt section 3); through
 * TOUCH_CALIB_DEFAULT (the fit made from that capture) it maps to (304,227),
 * row 14 -- the bottom nav bar. Under the old identity default the tests posed
 * (2048,0) here; on the real, axis-swapped panel that pair is row 7. */
#define NAV_RX 3692u
#define NAV_RY 387u

/* Drive the debounce: poll up to `k` times, return the first non-NONE action. */
static int poll_until_action(int k)
{
    for (int i = 0; i < k; i++) {
        int a = touch_poll();
        if (a != CLCD_ACT_NONE) return a;
    }
    return CLCD_ACT_NONE;
}

/* ==========================================================================
 * 1. The AXI-IIC dynamic master READ issues the exact register writes.
 * ========================================================================== */
static void test_iic_read_sequence(void)
{
    mock_regs_reset(); iic_reset_model();
    uint8_t payload[4] = { 0xABu, 0xCDu, 0xEFu, 0x12u };
    memcpy(iic.raw, payload, 4); iic.raw_n = 4;         /* generic RAW override */

    uint8_t buf[4] = { 0, 0, 0, 0 };
    uint8_t cmd = 0x90u;                                /* not an STMPE reg -> RAW */
    int rc = touch_test_iic_read(0x48u, &cmd, 1u, buf, 4u);
    CHECK(rc == TOUCH_OK);

    /* Exact dynamic-controller framing (PG090). */
    CHECK(iic.tx_n == 4u);
    CHECK(iic.tx_log[0] == (IIC_TX_START | (0x48u << 1)));        /* 0x190 write-addr */
    CHECK(iic.tx_log[1] == 0x90u);                                /* command byte     */
    CHECK(iic.tx_log[2] == (IIC_TX_START | (0x48u << 1) | 1u));   /* 0x191 read-addr  */
    CHECK(iic.tx_log[3] == (IIC_TX_STOP | 4u));                   /* 0x204 stop|count */
    CHECK(iic.last_pirq == 3u);                                   /* n-1 dynamic count */

    /* Drained bytes match the canned payload, in order. */
    CHECK(buf[0] == 0xABu && buf[1] == 0xCDu && buf[2] == 0xEFu && buf[3] == 0x12u);

    /* n == 0 is rejected without touching the bus. */
    CHECK(touch_test_iic_read(0x48u, &cmd, 1u, buf, 0u) == TOUCH_ERR_PARAM);
}

/* ==========================================================================
 * 2. The AXI-IIC dynamic master WRITE issues START|waddr, data..., last|STOP.
 * ========================================================================== */
static void test_iic_write_sequence(void)
{
    mock_regs_reset(); iic_reset_model();

    /* A representative STMPE811 register write: TSC_CTRL (0x40) <- 0x01, to
     * the STMPE811 address 0x41 (0x41<<1 = 0x82). */
    uint8_t wr[2] = { 0x40u, 0x01u };
    int rc = touch_test_iic_write(0x41u, wr, 2u);
    CHECK(rc == TOUCH_OK);                              /* mock ISR TX_ERROR clear */

    CHECK(iic.tx_n == 3u);
    CHECK(iic.tx_log[0] == (IIC_TX_START | (0x41u << 1)));  /* 0x182 write-addr  */
    CHECK(iic.tx_log[1] == 0x40u);                          /* register pointer  */
    CHECK(iic.tx_log[2] == (IIC_TX_STOP | 0x01u));          /* 0x201 last|STOP   */

    /* len == 0 is rejected. */
    CHECK(touch_test_iic_write(0x41u, wr, 0u) == TOUCH_ERR_PARAM);
}

/* ==========================================================================
 * 3. touch_init() confirms the STMPE811 via CHIP_ID (-> 0x0811).
 * ========================================================================== */
static void test_stmpe811_chip_id(void)
{
    mock_regs_reset(); iic_reset_model();
    touch_init();                                       /* reads CHIP_ID + inits */
    CHECK(touch_chip_id() == 0x0811u);
}

/* ==========================================================================
 * 4. Affine + 180 flip maps known raw -> EXACT pixels; set_calibration verbatim.
 * ========================================================================== */
static void test_affine_map(void)
{
    mock_regs_reset(); iic_reset_model();
    touch_init();                                       /* seeds default calib */

    uint16_t x, y;
    /* The default IS the 2026-09-24 silicon fit, verbatim (touch.h). */
    {
        touch_calib_t d;
        touch_get_calibration(&d);
        CHECK(d.ax == 21 && d.bx == 377 && d.cx == -158863 &&
              d.ay == -281 && d.by == -2 && d.cy == 1091113 && d.shift == 12u);
        CHECK(touch_calib_invalid_reason(&d) == 0);
    }
    /* ... and the six known-position taps of that capture land EXACTLY where its
     * residual table (touch_cal_20260924.txt section 4) says -- which pins the
     * coefficient ORDER (ax bx cx ay by cy shift) and that the fit is applied
     * pre-flip, with the 180 flip after it. A transposed pair, a sign, or a
     * flip applied twice moves every one of these. */
    {
        static const struct { uint16_t rx, ry, x, y; } TAP[6] = {
            { 1300u, 3759u,   6u,  64u },   /* "S" of SID          row 4 col 0  */
            { 1307u, 2461u, 125u,  64u },   /* last "6" of ID      row 4 col 15 */
            { 2734u, 3712u,   3u, 162u },   /* "C" of CFG          row 9 col 0  */
            { 2225u,  731u, 280u, 126u },   /* "K" of MMCM-LOCK    row 7 col 35 */
            {  617u, 2055u, 166u,  16u },   /* "n" of nanoSoC      row 0 col 20 */
            { 3692u,  387u, 304u, 227u },   /* spinner of "hb |"   row 14 col 37 */
        };
        for (unsigned i = 0u; i < 6u; i++) {
            touch_map_raw(TAP[i].rx, TAP[i].ry, &x, &y);
            CHECK(x == TAP[i].x && y == TAP[i].y);
        }
    }
    /* The axes are SWAPPED: raw_y moves screen X (and barely Y), raw_x moves
     * screen Y (and barely X). */
    {
        uint16_t x2, y2;
        touch_map_raw(2048u, 1000u, &x,  &y);
        touch_map_raw(2048u, 3000u, &x2, &y2);           /* (256,114) -> (72,115)  */
        CHECK(x2 < x && (int)x - (int)x2 > 150 && abs((int)y2 - (int)y) <= 1);
        touch_map_raw(1000u, 2048u, &x,  &y);
        touch_map_raw(3000u, 2048u, &x2, &y2);           /* (165,43) -> (154,180)  */
        CHECK(y2 > y && (int)y2 - (int)y > 120 && abs((int)x2 - (int)x) <= 11);
    }
    /* Out-of-range raw clamps to the panel (the corners of the ADC square). */
    touch_map_raw(0u,    0u,    &x, &y); CHECK(x == 319u && y == 0u);
    touch_map_raw(4095u, 4095u, &x, &y); CHECK(x == 0u   && y == 239u);

    /* A bench recal: identity (ax=by=1<<12, shift=12) => x_cal=rx, y_cal=ry;
     * the flip still applies on top. (100,50) -> (319-100, 239-50). */
    touch_calib_t c = { 4096, 0, 0, 0, 4096, 0, 12u };
    touch_set_calibration(&c);
    touch_map_raw(100u, 50u, &x, &y);
    CHECK(x == 219u && y == 189u);
}

/* ==========================================================================
 * 5. Debounced pen-down in the bottom nav row -> CLCD_ACT_NEXT_PAGE.
 * ========================================================================== */
static void test_pendown_navrow_next_page(void)
{
    mock_regs_reset(); iic_reset_model();
    touch_init();
    /* The silicon "hb |" spinner tap, raw (3692,387) -> (304,227) under
     * TOUCH_CALIB_DEFAULT = row 14 (the nav bar). z (8-bit) well above the
     * floor. TSC_STA present. */
    iic.touched = 1;
    iic.xyz[0] = NAV_RX;  /* X raw (12-bit) */
    iic.xyz[1] = NAV_RY;  /* Y raw (12-bit) */
    iic.xyz[2] = 200u;    /* Z pressure (8-bit) */
    s_tint = 1;

    int act = poll_until_action(20);
    CHECK(act == CLCD_ACT_NEXT_PAGE);

    uint16_t lx, ly; touch_test_last_xy(&lx, &ly);
    CHECK(lx == 304u && ly == 227u);                   /* it mapped where we posed */
}

/* A tap that misses every hit-box: it STILL dispatches (maps + calls hittest),
 * but hittest returns CLCD_ACT_NONE. */
static void test_pendown_miss_returns_none(void)
{
    mock_regs_reset(); iic_reset_model();
    touch_init();
    iic.touched = 1;
    iic.xyz[0] = 2048u; iic.xyz[1] = 2048u; iic.xyz[2] = 200u;  /* -> (159,115) row 7 */
    s_tint = 1;

    CHECK(touch_poll() == CLCD_ACT_NONE);              /* settle */
    CHECK(touch_poll() == CLCD_ACT_NONE);              /* dispatched, but missed */

    uint16_t lx, ly; touch_test_last_xy(&lx, &ly);
    CHECK(lx == 159u && ly == 115u);                   /* proof it DID map+dispatch */
}

/* ==========================================================================
 * 6. Pen-up -> NONE; z below the pressure floor -> ignored.
 * ========================================================================== */
static void test_penup_and_pressure_floor(void)
{
    mock_regs_reset(); iic_reset_model();
    touch_init();
    iic.touched = 1;
    iic.xyz[0] = NAV_RX; iic.xyz[1] = NAV_RY; iic.xyz[2] = 200u;

    /* Pen up: never talks to the TSC, always NONE. */
    s_tint = 0;
    for (int i = 0; i < 5; i++) CHECK(touch_poll() == CLCD_ACT_NONE);

    /* Pen down but pressure below floor: a valid nav-row position, still ignored. */
    s_tint = 1;
    iic.xyz[2] = 10u;                                  /* < TOUCH_Z_MIN (64) */
    for (int i = 0; i < 10; i++) CHECK(touch_poll() == CLCD_ACT_NONE);
}

/* ==========================================================================
 * 7. Debounce count + edge-triggered "tap once per contact".
 * ========================================================================== */
static void test_debounce_and_tap_once(void)
{
    mock_regs_reset(); iic_reset_model();
    touch_init();
    iic.touched = 1;
    iic.xyz[0] = NAV_RX; iic.xyz[1] = NAV_RY; iic.xyz[2] = 200u;    /* nav row, good z */
    s_tint = 1;

    /* Debounce = TOUCH_DEBOUNCE_SAMPLES (2): first poll settles, second fires. */
    CHECK(touch_poll() == CLCD_ACT_NONE);
    CHECK(touch_poll() == CLCD_ACT_NEXT_PAGE);

    /* Held finger must NOT repeat the tap. */
    for (int i = 0; i < 10; i++) CHECK(touch_poll() == CLCD_ACT_NONE);

    /* Release, then a fresh press fires again (edge re-arms). */
    s_tint = 0; CHECK(touch_poll() == CLCD_ACT_NONE);
    s_tint = 1;
    CHECK(touch_poll() == CLCD_ACT_NONE);              /* settle */
    CHECK(touch_poll() == CLCD_ACT_NEXT_PAGE);         /* fires again */
}

/* ==========================================================================
 * 8. Absent / wrong-address TSC: touch stays INERT (never hammers the bus).
 *    Regression guard: a wedged/absent I2C must not starve the superloop.
 * ========================================================================== */
static void test_inert_when_tsc_absent(void)
{
    mock_regs_reset(); iic_reset_model();
    iic.chip_id = 0x0000u;                          /* CHIP_ID mismatch -> not ready */
    touch_init();
    CHECK(touch_chip_id() == 0x0000u);

    /* Even with a valid pen + pressure posed, touch_poll must do NOTHING. */
    iic.touched = 1;
    iic.xyz[0] = NAV_RX; iic.xyz[1] = NAV_RY; iic.xyz[2] = 200u;
    s_tint = 1;
    for (int i = 0; i < 10; i++) CHECK(touch_poll() == CLCD_ACT_NONE);
}

/* ==========================================================================
 * 10. Bus-loss latch: a TSC that answers at init and then STOPS.
 *
 * The CHIP_ID gate only proves the part was present at init. Silicon
 * 2026-09-09: the bus wedged after 538 good polls, s_tsc_ready stayed 1, and
 * every later pass paid the full bounded IIC timeout -- the superloop fell
 * from ~2475 to ~47 iterations/s while the board still pinged and still
 * reported the right shell_id. touch_poll() must give up after
 * TOUCH_BUS_FAIL_LIMIT consecutive failures and go to its zero-I2C path.
 * ========================================================================== */
static void test_bus_loss_latches_dormant(void)
{
    mock_regs_reset(); iic_reset_model();
    touch_init();
    CHECK(touch_chip_id() == 0x0811u);      /* present and confirmed at init */

    /* Pose a real, actionable touch: any poll reaching the read path would act. */
    iic.touched = 1;
    iic.xyz[0] = NAV_RX; iic.xyz[1] = NAV_RY; iic.xyz[2] = 200u;
    s_tint = 1;

    iic.bus_dead = 1;                       /* ... and now the part stops answering */

    /* CLAIM 1: the latch fires at TOUCH_BUS_FAIL_LIMIT, not before. One short of
     * it, touch must still be live and still issuing I2C -- otherwise "16 is far
     * above the debounce's 2" is an untested assertion. */
    for (unsigned i = 0; i < TOUCH_BUS_FAIL_LIMIT - 1u; i++) {
        CHECK(touch_poll() == CLCD_ACT_NONE);
    }
    iic.tx_n = 0u;
    CHECK(touch_poll() == CLCD_ACT_NONE);   /* this is failure LIMIT-1 ... */
    CHECK(iic.tx_n > 0u);                   /* ... and it still talked to the bus */

    CHECK(touch_poll() == CLCD_ACT_NONE);   /* failure LIMIT: the latch fires */

    /* Latched dormant: from here touch_poll must issue ZERO I2C traffic. */
    iic.tx_n = 0u;
    for (int i = 0; i < 20; i++) CHECK(touch_poll() == CLCD_ACT_NONE);
    CHECK(iic.tx_n == 0u);

    /* Stays dormant even if the bus recovers, until the recovery back-off is
     * due (the clock is frozen here, so it never is) -- retrying a wedged bus
     * on EVERY pass is the behaviour being removed. The bounded periodic
     * re-init is test_bus_loss_recovers_when_the_bus_returns below. */
    iic.bus_dead = 0;
    iic.tx_n = 0u;
    for (int i = 0; i < 10; i++) CHECK(touch_poll() == CLCD_ACT_NONE);
    CHECK(iic.tx_n == 0u);

    /* touch_init() still recovers at once: the driver talks to the bus again. */
    touch_init();
    CHECK(touch_chip_id() == 0x0811u);
    iic.tx_n = 0u;
    (void)touch_poll();
    CHECK(iic.tx_n > 0u);

    /* CLAIM 2: a single good read CLEARS the run, so transients never sum to the
     * limit. Drive LIMIT-1 failures, one clean poll, then LIMIT-1 again: if the
     * counter were cumulative rather than consecutive this would latch. */
    for (unsigned i = 0; i < TOUCH_BUS_FAIL_LIMIT - 1u; i++) {
        iic.bus_dead = 1; CHECK(touch_poll() == CLCD_ACT_NONE);
    }
    iic.bus_dead = 0; (void)touch_poll();          /* one good read clears it */
    for (unsigned i = 0; i < TOUCH_BUS_FAIL_LIMIT - 1u; i++) {
        iic.bus_dead = 1; CHECK(touch_poll() == CLCD_ACT_NONE);
    }
    iic.bus_dead = 0;
    iic.tx_n = 0u;
    (void)touch_poll();
    CHECK(iic.tx_n > 0u);                          /* still live: never latched */
}

/* ==========================================================================
 * 11. touch_bus_scan() -- shipped in the target ELF (main.c bring-up witness),
 * previously untested. Needs the mock to model a NACK, or every address
 * "responds" and the scan reports all 112.
 * ========================================================================== */
/* ==========================================================================
 * 12. The witness must not deny the failure it exists to catch.
 *
 * A bus that wedges only on the 0xD7 sample port still reaches TSC_CTRL fine,
 * so poll_ok keeps climbing. If the 0xD7 timeout is not counted, the JTAG read
 * says "many clean polls, ZERO bus errors" at the very instant the driver
 * declares the bus lost -- and an engineer reading that concludes the opposite
 * of the truth.
 * ========================================================================== */
static void test_telemetry_counts_the_sample_read_failure(void)
{
    uint32_t ok0 = 0, err0 = 0, sta0 = 0, lost0 = 0;
    uint32_t ok = 0, err = 0, sta = 0, lost = 0;

    mock_regs_reset(); iic_reset_model();
    touch_init();
    iic.touched = 1;
    iic.xyz[0] = NAV_RX; iic.xyz[1] = NAV_RY; iic.xyz[2] = 200u;
    s_tint = 1;

    /* The s_dbg_* counters are LIFETIME totals -- touch_init() deliberately does
     * not clear them (on the target you want the count since power-on). So work
     * in deltas, or this reads whatever earlier tests left behind. */
    touch_test_counters(&ok0, &err0, &sta0, &lost0);

    iic.dead_reg = 0xD7;                    /* only the sample FIFO port wedges */
    for (unsigned i = 0; i < TOUCH_BUS_FAIL_LIMIT; i++) (void)touch_poll();

    touch_test_counters(&ok, &err, &sta, &lost);
    CHECK(lost - lost0 == 1u);                     /* the latch fired ONCE ... */
    CHECK(err  - err0  >= TOUCH_BUS_FAIL_LIMIT);   /* ... and the witness AGREES */
    CHECK(sta  - sta0  >= TOUCH_BUS_FAIL_LIMIT);   /* contact WAS detected ... */
    CHECK(ok   - ok0   >= TOUCH_BUS_FAIL_LIMIT);   /* ... TSC_CTRL reads were fine */
}

/* ==========================================================================
 * 13. BUS-LOSS RECOVERY (touch.h "BUS-LOSS RECOVERY").
 *
 * Silicon 2026-09-24 (docs/evidence/2026-09-w3/touch_cal_20260924.txt): ~2.7 h
 * after boot the bus failed 16 times in a row, the latch fired, and touch was
 * dead until a reload -- invisibly. The latch now arms a bounded periodic
 * re-init. These tests hold it to four claims:
 *   (a) a bus that comes BACK is recovered at the next back-off slot, by Arm's
 *       sequence, without the panel probe and without re-seeding the
 *       calibration -- and a tap then works;
 *   (b) a bus that NEVER comes back is retried at 5 s x 6, then every 30 s,
 *       with ZERO I2C between attempts;
 *   (c) no call ever costs more than the failing poll that armed the latch:
 *       at most ONE I2C transaction per call, the settles waited out on the
 *       clock, an attempt abandoned at its first failure;
 *   (d) a bus that flaps climbs to the slow rate instead of being retried
 *       fast forever.
 * NEGATIVE CONTROL (run by hand, 2026-09-24): this file linked against the
 * pre-recovery touch.c (firmware/touch at ab8d3e2, built with the new default
 * as -DTOUCH_CALIB_DEFAULT, plus a touch_health() shim derived from its own
 * touch_test_counters() and a --wrap of touch_init) passes every earlier test
 * and aborts in (a) at `iic.softr_n == softr0 + 1u`: no re-init is ever
 * started. Driven for 600 s with the bus back after the latch, the old driver
 * reports ok=0 having issued ZERO I2C transactions; the new one is live again
 * after one re-init.
 * ========================================================================== */

/* Worst touch_poll() while touch was NOT live at the call (latched/recovering),
 * and the most I2C transactions one such call issued; and the worst ORDINARY
 * failing poll (the ones that arm the latch) for comparison. */
static uint32_t s_rec_worst_us;
static unsigned s_rec_worst_txn;
static uint32_t s_ord_worst_us;

/* One touch_poll(), measured. The fake clock charges 1 us per register read
 * (mock_regs_set_us_per_read(1)), so an IIC wait that runs to its bound costs
 * its real TOUCH_IIC_WAIT_US. */
static int timed_poll(void)
{
    touch_health_t h;
    touch_health(&h);
    const int live = h.ok;
    const uint32_t t0 = mock_time_now_us();
    const unsigned s0 = iic.stops;
    int a = touch_poll();
    const uint32_t dt = mock_time_now_us() - t0;
    if (!live) {
        if (dt > s_rec_worst_us) s_rec_worst_us = dt;
        if (iic.stops - s0 > s_rec_worst_txn) s_rec_worst_txn = iic.stops - s0;
    } else if (dt > s_ord_worst_us) {
        s_ord_worst_us = dt;
    }
    return a;
}

/* `ms` of superloop: one touch_poll() per 10 ms pass (clcd.c's
 * CLCD_TOUCH_PERIOD_MS), the rate v0.11 samples at on silicon. */
static void run_ms(uint32_t ms)
{
    for (uint32_t t = 0u; t < ms; t += 10u) {
        mock_time_advance_ms(10u);
        (void)timed_poll();
    }
}

/* Passes until touch is live again; returns the ms that took (max_ms = never). */
static uint32_t run_until_live(uint32_t max_ms)
{
    touch_health_t h;
    for (uint32_t t = 0u; t < max_ms; t += 10u) {
        mock_time_advance_ms(10u);
        (void)timed_poll();
        touch_health(&h);
        if (h.ok) return t + 10u;
    }
    return max_ms;
}

/* The bus dies under a live driver: TOUCH_BUS_FAIL_LIMIT failing passes. */
static void kill_the_bus_until_latched(void)
{
    touch_health_t h;
    iic.bus_dead = 1;
    for (unsigned i = 0u; i < TOUCH_BUS_FAIL_LIMIT; i++) {
        mock_time_advance_ms(10u);
        CHECK(timed_poll() == CLCD_ACT_NONE);
    }
    touch_health(&h);
    CHECK(h.ok == 0);
}

/* A fresh, healthy, live driver (pen up, TINT asserted as on the target, where
 * the weak touch_tint_asserted() returns 1), then the bus dies and latches. */
static void latch_the_bus(void)
{
    touch_health_t h;
    mock_regs_reset(); iic_reset_model();
    mock_time_set_ms(1000u);
    touch_init();
    CHECK(touch_chip_id() == 0x0811u);
    touch_health(&h);
    CHECK(h.ok == 1);
    iic.touched = 0; s_tint = 1;
    mock_regs_set_us_per_read(1u);
    kill_the_bus_until_latched();
}

/* Arm's Touch_V2M-MPS3 sequence as the PART receives it, (reg << 8) | val. */
static const uint16_t ARM_SEQ_ON_THE_WIRE[15] = {
    0x0302u, 0x0300u,           /* SYS_CTRL1 soft reset, release          */
    0x0400u, 0x0A07u, 0x2069u,  /* SYS_CTRL2, INT_EN, ADC_CTRL1           */
    0x2101u, 0x41C2u, 0x4A01u,  /* ADC_CTRL2, TSC_CFG, FIFO_TH            */
    0x4B01u, 0x4B00u,           /* FIFO_STA reset pulse                   */
    0x5607u, 0x5801u, 0x1700u,  /* TSC_FRACT_Z, TSC_I_DRIVE, GPIO_AF      */
    0x4001u, 0x0BFFu,           /* TSC_CTRL EN, INT_STA W1C               */
};

/* (a) + (c): the bus comes back -> recovered at the next slot, touch works. */
static void test_bus_loss_recovers_when_the_bus_returns(void)
{
    touch_health_t h0, h;
    touch_calib_t c;
    const touch_calib_t def  = TOUCH_CALIB_DEFAULT;
    const touch_calib_t mine = { 4096, 0, 0, 0, 4096, 0, 12u };  /* a runtime set */

    touch_health(&h0);
    latch_the_bus();
    touch_health(&h);
    CHECK(h.ok == 0 && h.bus_lost == h0.bus_lost + 1u && h.recoveries == h0.recoveries);
    CHECK(touch_set_calibration(&mine) == 0);   /* `touch_cal set` while latched */
    const unsigned capt0 = iic.adc_capt_n;

    /* Latched and not yet due: 4.9 s of passes, NO I2C and no attempt. */
    unsigned softr0 = iic.softr_n, stops0 = iic.stops;
    run_ms(4900u);
    CHECK(iic.softr_n == softr0 && iic.stops == stops0);

    /* +5 s: the first attempt, on a bus that is still dead -- core reset, one
     * CHIP_ID read, one bounded wait, abandoned. */
    run_ms(200u);
    CHECK(iic.softr_n == softr0 + 1u);
    touch_health(&h);
    CHECK(h.ok == 0 && h.attempts == h0.attempts + 1u);

    /* The bus comes back. Nothing is sent until the next slot (+10 s) ... */
    iic.bus_dead = 0;
    stops0 = iic.stops;
    run_ms(4700u);
    CHECK(iic.stops == stops0);
    touch_health(&h);
    CHECK(h.ok == 0);

    /* ... which re-runs the bring-up and brings touch back. */
    iic.wlog_n = 0u;
    run_ms(500u);
    touch_health(&h);
    CHECK(h.ok == 1);                                   /* RECOVERED */
    CHECK(h.recoveries == h0.recoveries + 1u);
    CHECK(h.attempts == h0.attempts + 2u);
    CHECK(h.bus_lost == h0.bus_lost + 1u);

    /* It was Arm's sequence, entry for entry, in Arm's order (the same table
     * touch_init() writes) -- and NOT the panel probe (no ADC capture, no
     * TSC_SHIELD / GPIO_DIR writes). */
    CHECK(iic.wlog_n >= 15u);
    for (unsigned i = 0u; i < 15u; i++) CHECK(iic.wlog[i] == ARM_SEQ_ON_THE_WIRE[i]);
    CHECK(iic.adc_capt_n == capt0);
    for (unsigned i = 0u; i < iic.wlog_n; i++) {
        CHECK((iic.wlog[i] >> 8) != R_TSC_SHIELD && (iic.wlog[i] >> 8) != R_GPIO_DIR);
    }

    /* The runtime calibration SURVIVED: a recovery is not a touch_init(). */
    touch_get_calibration(&c);
    CHECK(c.ax == mine.ax && c.bx == mine.bx && c.cx == mine.cx &&
          c.ay == mine.ay && c.by == mine.by && c.cy == mine.cy && c.shift == mine.shift);

    /* And touch WORKS again: a nav-row press is a tap. */
    CHECK(touch_set_calibration(&def) == 0);
    iic.touched = 1;
    iic.xyz[0] = NAV_RX; iic.xyz[1] = NAV_RY; iic.xyz[2] = 200u;
    CHECK(poll_until_action(20) == CLCD_ACT_NEXT_PAGE);
    mock_regs_set_us_per_read(0u);
}

/* (b) + (c): a bus that never comes back -- 5 s x 6, then every 30 s. */
static void test_bus_that_never_returns_is_retried_at_the_backoff(void)
{
    touch_health_t h0, h;
    uint32_t at[32];
    unsigned n = 0u;

    touch_health(&h0);
    latch_the_bus();
    const uint32_t t_latch = mock_time_now_ms();   /* the latching pass, +<1 ms */
    unsigned softr = iic.softr_n;

    for (uint32_t t = 0u; t < 305000u; t += 10u) {  /* five minutes of passes */
        const unsigned st0 = iic.stops;
        mock_time_advance_ms(10u);
        (void)timed_poll();
        if (iic.softr_n != softr) {                 /* an attempt started */
            softr = iic.softr_n;
            CHECK(iic.stops - st0 == 1u);           /* core reset + ONE CHIP_ID read */
            if (n < 32u) at[n++] = mock_time_now_ms() - t_latch;
        } else {
            CHECK(iic.stops == st0);                /* between attempts: NO I2C */
        }
    }
    /* 5,10,15,20,25,30 s, then 60,90,...,300 s. */
    CHECK(n == TOUCH_RECOVER_FAST_TRIES + 9u);
    for (unsigned i = 0u; i < n; i++) {
        const uint32_t gap = at[i] - (i ? at[i - 1u] : 0u);
        const uint32_t want = (i < TOUCH_RECOVER_FAST_TRIES) ? TOUCH_RECOVER_FAST_MS
                                                             : TOUCH_RECOVER_SLOW_MS;
        CHECK(gap >= want && gap <= want + 20u);    /* one 10 ms pass + clock drift */
    }
    touch_health(&h);
    CHECK(h.ok == 0);
    CHECK(h.bus_lost == h0.bus_lost + 1u);          /* ONE latch event, not one per try */
    CHECK(h.recoveries == h0.recoveries);
    CHECK(h.attempts == h0.attempts + n);
    mock_regs_set_us_per_read(0u);
}

/* (c): the part answers CHIP_ID but NACKs every write -- the attempt ends at
 * the first write, and nothing more is sent until the next slot. */
static void test_recovery_attempt_that_fails_midway_waits_for_the_next(void)
{
    touch_health_t h0, h;
    touch_health(&h0);
    latch_the_bus();
    iic.bus_dead = 0;
    iic.ack_addr = 0x44;          /* reads still served; writes to 0x41 NACK */
    run_ms(5100u);
    touch_health(&h);
    CHECK(h.ok == 0 && h.attempts == h0.attempts + 1u);
    const unsigned stops0 = iic.stops;
    run_ms(4800u);
    CHECK(iic.stops == stops0);   /* abandoned, not pressed on entry by entry */
    iic.ack_addr = 0;
    CHECK(run_until_live(1000u) < 1000u);
    touch_health(&h);
    CHECK(h.recoveries == h0.recoveries + 1u && h.attempts == h0.attempts + 2u);
    mock_regs_set_us_per_read(0u);
}

/* (c): ONE write per pass, and Arm's two 10 ms settles are waited out on the
 * CLOCK across passes -- never spun inside one. */
static void test_recovery_waits_out_arms_settles_on_the_clock(void)
{
    touch_health_t h;
    latch_the_bus();
    iic.bus_dead = 0;
    mock_regs_set_us_per_read(0u);           /* the clock moves only when we say */
    mock_time_advance_ms(TOUCH_RECOVER_FAST_MS);

    const unsigned w0 = iic.wr_seq;
    (void)touch_poll();                      /* due: core reset + CHIP_ID read */
    CHECK(iic.wr_seq == w0);
    (void)touch_poll();                      /* SYS_CTRL1 <- 0x02: soft reset */
    CHECK(iic.wr_seq == w0 + 1u && iic.regs[0x03] == 0x02u);
    for (int i = 0; i < 10; i++) (void)touch_poll();
    CHECK(iic.wr_seq == w0 + 1u);            /* frozen clock: still settling */
    mock_time_advance_us(TOUCH_RECOVER_SETTLE_US - 1u);
    (void)touch_poll();
    CHECK(iic.wr_seq == w0 + 1u);            /* 1 us short: still settling */
    mock_time_advance_us(1u);
    (void)touch_poll();                      /* 10 ms: release the reset */
    CHECK(iic.wr_seq == w0 + 2u && iic.regs[0x03] == 0x00u);
    (void)touch_poll(); CHECK(iic.wr_seq == w0 + 3u);   /* SYS_CTRL2 */
    (void)touch_poll(); CHECK(iic.wr_seq == w0 + 4u);   /* INT_EN    */
    (void)touch_poll(); CHECK(iic.wr_seq == w0 + 5u && iic.regs[0x20] == 0x69u);
    (void)touch_poll(); CHECK(iic.wr_seq == w0 + 5u);   /* ADC_CTRL1's settle */
    mock_time_advance_us(TOUCH_RECOVER_SETTLE_US);
    for (int i = 0; i < 10; i++) {           /* 10 more writes, then the read-back */
        (void)touch_poll();
        CHECK(iic.wr_seq == w0 + 6u + (unsigned)i);
    }
    touch_health(&h);
    CHECK(h.ok == 0);                        /* not live until EN reads back */
    (void)touch_poll();
    touch_health(&h);
    CHECK(h.ok == 1 && iic.wr_seq == w0 + 15u);
}

/* (d): a bus that recovers and fails again inside TOUCH_RECOVER_STABLE_MS keeps
 * its place on the back-off; after a stable minute it starts fast again. */
static void test_a_flapping_bus_climbs_to_the_slow_rate(void)
{
    latch_the_bus();
    for (unsigned k = 0u; k < TOUCH_RECOVER_FAST_TRIES; k++) {
        iic.bus_dead = 0;
        const uint32_t dt = run_until_live(60000u);
        CHECK(dt >= TOUCH_RECOVER_FAST_MS && dt <= TOUCH_RECOVER_FAST_MS + 200u);
        kill_the_bus_until_latched();                  /* a flap: seconds later */
    }
    iic.bus_dead = 0;
    uint32_t dt = run_until_live(60000u);
    CHECK(dt >= TOUCH_RECOVER_SLOW_MS && dt <= TOUCH_RECOVER_SLOW_MS + 200u);

    run_ms(TOUCH_RECOVER_STABLE_MS + 1000u);          /* a stable minute ... */
    kill_the_bus_until_latched();
    iic.bus_dead = 0;
    dt = run_until_live(60000u);                       /* ... resets the back-off */
    CHECK(dt >= TOUCH_RECOVER_FAST_MS && dt <= TOUCH_RECOVER_FAST_MS + 200u);
    mock_regs_set_us_per_read(0u);
}

/* (c), the number: print and bound the worst recovering call. */
static void test_recovery_never_exceeds_the_poll_budget(void)
{
    printf("  worst latched/recovering touch_poll(): %u us, %u I2C txn "
           "(worst ordinary failing poll %u us; bound 3*TOUCH_IIC_WAIT_US = %u us)\n",
           (unsigned)s_rec_worst_us, s_rec_worst_txn, (unsigned)s_ord_worst_us,
           3u * (unsigned)TOUCH_IIC_WAIT_US);
    CHECK(s_rec_worst_us > 0u && s_ord_worst_us >= TOUCH_IIC_WAIT_US);
    CHECK(s_rec_worst_txn <= 1u);                     /* ONE transaction per call  */
    CHECK(s_rec_worst_us <= s_ord_worst_us);          /* never above a failing poll */
    CHECK(s_rec_worst_us <= 3u * TOUCH_IIC_WAIT_US);  /* the existing poll bound    */
}

static void test_bus_scan_finds_only_the_acking_address(void)
{
    uint8_t found[8];
    unsigned n;

    mock_regs_reset(); iic_reset_model();
    iic.ack_addr = 0x41;                    /* only the STMPE811 answers */
    touch_init();
    CHECK(touch_chip_id() == 0x0811u);

    n = touch_bus_scan(found, (unsigned)(sizeof found / sizeof found[0]));
    CHECK(n == 1u);
    CHECK(found[0] == 0x41u);

    /* A part strapped A0=VCC shows up at 0x44 instead -- the case touch.c's
     * header tells the reader to look for. */
    mock_regs_reset(); iic_reset_model();
    iic.ack_addr = 0x44;
    touch_init();                            /* CHIP_ID at 0x41 will NACK */
    n = touch_bus_scan(found, (unsigned)(sizeof found / sizeof found[0]));
    CHECK(n == 1u);
    CHECK(found[0] == 0x44u);

    /* Dead bus: the only ACKing address sits OUTSIDE the scanned 0x08..0x77
     * range, so nothing at all responds. This is the "no part / no pull-ups"
     * outcome the bring-up witness has to be able to report. */
    mock_regs_reset(); iic_reset_model();
    iic.ack_addr = 0x7F;
    touch_init();
    n = touch_bus_scan(found, (unsigned)(sizeof found / sizeof found[0]));
    CHECK(n == 0u);
}

/* ==========================================================================
 * 13. PANEL-CONTINUITY PROBE (touch.h) -- "chip or panel?" as a number.
 *
 * Silicon 2026-09-09 left exactly two live hypotheses and no way to tell them
 * apart from the counters: the chip is not configured the way we think, or the
 * panel is not connected to it. These tests pin BOTH halves of the answer --
 * the init read-backs and the drive-follow probe -- and, for each verdict, the
 * control that would be indistinguishable from it under a weaker probe.
 * ========================================================================== */

/* The five read-backs must equal the five literals init wrote. */
static void test_probe_readbacks_match_writes(void)
{
    mock_regs_reset(); iic_reset_model();
    touch_init();
    CHECK(touch_chip_id() == 0x0811u);

    /* [7:0] GPIO_AF 0x00 | [15:8] SYS_CTRL2 0x00 | [23:16] TSC_CFG 0xC2 |
     * [31:24] ADC_CTRL1 0x69 -- the values touch.c's STMPE_INIT_* wrote, which
     * are now Arm's Touch_V2M-MPS3.c values byte for byte. */
    CHECK(touch_probe_regs_word() == 0x69C20000u);
    CHECK(((touch_probe_adc_x_word() >> 24) & 0xFFu) == 0x01u);   /* TSC_I_DRIVE */

    uint32_t st = (touch_probe_adc_y_word() >> 24) & 0xFFu;
    CHECK((st & TOUCH_PROBE_ST_RAN) != 0u);
    CHECK((st & TOUCH_PROBE_ST_RB_ERR) == 0u);
    CHECK((st & TOUCH_PROBE_ST_RB_DIFF) == 0u);
    CHECK((st & TOUCH_PROBE_ST_ADC_ERR) == 0u);

    /* GPIO_AF: Arm writes 0x00 -- ALL EIGHT pins in alternate function. The
     * polarity is inverted from the name ("'0' sets the corresponding pin to
     * function as touchscreen/ADC, and '1' sets it into GPIO mode"), so 0x00
     * puts the four touch lines under the TSC and leaves IN0..IN3 as ADC
     * inputs. We used to write 0x0F, which additionally muxed IN0..IN3 into
     * GPIO mode -- harmless for touch, fatal for the probe (below). */
    CHECK(iic.regs[R_GPIO_AF] == 0x00u);

    /* INT_EN: the one FUNCTIONAL register the old sequence never wrote. Arm
     * writes 0x07 = FIFO_OFLOW|FIFO_TH|TOUCH_DET, and the datasheet ties the
     * part's auto-hibernation wake-up to the touch-detect mask being enabled. */
    CHECK(iic.regs[R_INT_EN] == 0x07u);

    /* And the probe left the part the way the poll loop needs it: TSC pins back
     * in alternate-function mode, no GPIO left driving the panel. */
    CHECK(iic.regs[R_GPIO_DIR] == 0x00u);
}

/* A CONNECTED plate: every line follows its partner's drive both ways -> 3. */
static void test_probe_connected_panel_reads_present(void)
{
    mock_regs_reset(); iic_reset_model();
    iic.panel = PANEL_CONNECTED;
    touch_init();

    CHECK(touch_probe_verdict() == TOUCH_VERDICT_PRESENT);

    /* THE REGRESSION THIS FILE EXISTS FOR. The old probe converted ADC channels
     * 4..7 because it assumed "channel n == GPIO n". Channels 4..7 are IN0..IN3
     * -- pins with nothing on them -- so under this (now faithful) fake they
     * float and no line follows: the old code cannot reach PRESENT here. */
    CHECK(iic.capt_no_rearm == 0u);   /* and every capture was re-armed first */

    /* The stored samples are the DRIVEN-HIGH readings of X+/X- and Y+/Y-. */
    CHECK((touch_probe_adc_x_word() & 0x0FFFu) == 3990u);          /* X+ */
    CHECK(((touch_probe_adc_x_word() >> 12) & 0x0FFFu) == 3990u);  /* X- */
    CHECK((touch_probe_adc_y_word() & 0x0FFFu) == 3990u);          /* Y+ */
    CHECK(((touch_probe_adc_y_word() >> 12) & 0x0FFFu) == 3990u);  /* Y- */
    CHECK((((touch_probe_adc_y_word() >> 24) & 0xFFu)
           & TOUCH_PROBE_ST_FOLLOW_BAD) == 0u);

    /* The probe must not have broken the thing it was diagnosing: a posed nav
     * tap still dispatches, so the TSC was re-enabled exactly as before. */
    iic.touched = 1;
    iic.xyz[0] = NAV_RX; iic.xyz[1] = NAV_RY; iic.xyz[2] = 200u;
    s_tint = 1;
    CHECK(poll_until_action(20) == CLCD_ACT_NEXT_PAGE);
}

/* A line that does NOT follow its partner -> UNKNOWN (never OPEN), in all three
 * ways a floating input can present. The stuck-at-rail cases are the CONTROLS:
 * a probe that took one driven-HIGH sample and thresholded it would call the
 * 4095 case a healthy panel.
 *
 * The verdict is UNKNOWN and not PANEL_OPEN on purpose, and this test is what
 * holds that. On 2026-09-14 a PANEL_OPEN verdict was fielded and read as "the
 * panel's Y+ line is open"; Arm's own image detects touches on that very panel,
 * so the verdict was wrong. The datasheet describes the internal switches on
 * X+/X-/Y+/Y- in one paragraph and never says what they do during an ADC
 * capture, and never gives the ADC reference voltage, so a line that fails to
 * follow simply is not evidence of an open panel. The raw samples and
 * TOUCH_PROBE_ST_FOLLOW_BAD are still exported; only the conclusion is withheld. */
static void test_probe_no_follow_reads_unknown_not_open(void)
{
    /* (i) floats at the low rail */
    mock_regs_reset(); iic_reset_model();
    iic.panel = PANEL_OPEN; iic.float_code = 0u;
    touch_init();
    CHECK(touch_probe_verdict() == TOUCH_VERDICT_UNKNOWN);
    CHECK(touch_probe_verdict() != TOUCH_VERDICT_OPEN);
    CHECK((((touch_probe_adc_y_word() >> 24) & 0xFFu)
           & TOUCH_PROBE_ST_FOLLOW_BAD) != 0u);   /* the evidence is still there */
    CHECK((((touch_probe_adc_y_word() >> 24) & 0xFFu)
           & TOUCH_PROBE_ST_RAN) != 0u);          /* ... and the probe DID run   */

    /* (ii) floats at the HIGH rail -- the control. Every driven-high sample
     * reads 4095, which is what a connected plate reads too; only the driven-LOW
     * half of the pair tells them apart. */
    mock_regs_reset(); iic_reset_model();
    iic.panel = PANEL_OPEN; iic.float_code = 4095u;
    touch_init();
    CHECK((touch_probe_adc_x_word() & 0x0FFFu) == 4095u);   /* looks "present" ... */
    CHECK(touch_probe_verdict() == TOUCH_VERDICT_UNKNOWN);  /* ... and is not      */

    /* (iii) floats mid-scale */
    mock_regs_reset(); iic_reset_model();
    iic.panel = PANEL_OPEN; iic.float_code = 2000u;
    touch_init();
    CHECK(touch_probe_verdict() == TOUCH_VERDICT_UNKNOWN);
}

/* A read-back that differs from the write -> 1, and it OUTRANKS the panel
 * result: on a misconfigured chip the continuity numbers mean nothing. */
static void test_probe_readback_mismatch_reads_chip_bad(void)
{
    /* The real suspect: a part that swallows the GPIO_AF write. The panel here
     * is CONNECTED, so a probe that checked only continuity would say 3. */
    mock_regs_reset(); iic_reset_model();
    iic.panel = PANEL_CONNECTED;
    iic.sticky_reg = (int)R_GPIO_AF; iic.sticky_val = 0xFFu;
    touch_init();
    CHECK(touch_probe_verdict() == TOUCH_VERDICT_CHIP_BAD);
    CHECK((touch_probe_regs_word() & 0xFFu) == 0xFFu);      /* the raw evidence */
    CHECK((((touch_probe_adc_y_word() >> 24) & 0xFFu)
           & TOUCH_PROBE_ST_RB_DIFF) != 0u);

    /* Any of the five, not just the first: TSC_CFG stuck at its reset value. */
    mock_regs_reset(); iic_reset_model();
    iic.sticky_reg = 0x41; iic.sticky_val = 0x00u;
    touch_init();
    CHECK(touch_probe_verdict() == TOUCH_VERDICT_CHIP_BAD);
    CHECK(((touch_probe_regs_word() >> 16) & 0xFFu) == 0x00u);
}

/* UNKNOWN is a real answer and must not be dressed up as one of the others. */
static void test_probe_unknown_when_it_cannot_run(void)
{
    /* (i) nothing ACKs at init: the probe never runs at all. */
    mock_regs_reset(); iic_reset_model();
    iic.bus_dead = 1;
    touch_init();
    CHECK(touch_probe_verdict() == TOUCH_VERDICT_UNKNOWN);
    CHECK(iic.adc_capt_n == 0u);
    CHECK((((touch_probe_adc_y_word() >> 24) & 0xFFu) & TOUCH_PROBE_ST_RAN) == 0u);

    /* (ii) a part that is not an STMPE811: the register choreography below is
     * meaningless, so it is not run and no verdict is invented. */
    mock_regs_reset(); iic_reset_model();
    iic.chip_id = 0x1234u;
    touch_init();
    CHECK(touch_probe_verdict() == TOUCH_VERDICT_UNKNOWN);
    CHECK(iic.adc_capt_n == 0u);

    /* (iii) the ADC never finishes a conversion: fail closed, do NOT read the
     * stale data register and call it a sample. */
    mock_regs_reset(); iic_reset_model();
    iic.adc_stuck = 1;
    touch_init();
    CHECK(touch_probe_verdict() == TOUCH_VERDICT_UNKNOWN);
    CHECK((((touch_probe_adc_y_word() >> 24) & 0xFFu)
           & TOUCH_PROBE_ST_ADC_ERR) != 0u);
}

/* SILICON, 2026-09-14 (rc4): ADC_CAPT never cleared within the poll budget, so
 * the probe timed out (status ADC_ERR) and reported CHIP_BAD on a reserved bit.
 * The datasheet says it never CAN clear: "Reads '1' if conversion is completed.
 * Reads '0' if conversion is in progress", reset value 0xFF, and "Writing '0'
 * has no effect" so it cannot be cleared by software either. The driver now
 * ignores ADC_CAPT entirely and takes completion from ADC_INT_STA. Controls for
 * both halves of that. */
static void test_probe_completes_via_int_sta_when_capt_sticks(void)
{
    mock_regs_reset(); iic_reset_model();
    iic.panel = PANEL_CONNECTED;
    iic.adc_capt_sticky = 1;             /* ADC_CAPT reads 0xFF throughout */
    touch_init();
    CHECK(touch_probe_verdict() == TOUCH_VERDICT_PRESENT);
    CHECK(iic.regs[R_ADC_CAPT] == 0xFFu);   /* it never showed "in progress" ... */
    CHECK((((touch_probe_adc_y_word() >> 24) & 0xFFu) & TOUCH_PROBE_ST_RAN) != 0u);
    CHECK((((touch_probe_adc_y_word() >> 24) & 0xFFu) & TOUCH_PROBE_ST_ADC_ERR) == 0u);
    /* the done bit was W1C'd, so a later conversion cannot inherit it */
    CHECK(iic.regs[R_ADC_INT_STA] == 0u);
    /* and a part that signals NEITHER is still a timeout, never a guess */
    mock_regs_reset(); iic_reset_model();
    iic.adc_stuck = 1;
    touch_init();
    CHECK(touch_probe_verdict() == TOUCH_VERDICT_UNKNOWN);
}

static void test_probe_reserved_bit_readback_is_not_chip_bad(void)
{
    /* ADC_CTRL1 is written 0x69 and bit 2, which the datasheet marks RESERVED
     * and whose reset value is 1 (ADC_CTRL1 resets to 0x1C), comes back set:
     * 0x6D. Measured on silicon in 2026-09-14's 0x48 -> 0x4C form. */
    mock_regs_reset(); iic_reset_model();
    iic.panel = PANEL_CONNECTED;
    iic.sticky_reg = 0x20; iic.sticky_val = 0x6Du;
    touch_init();
    CHECK(touch_probe_verdict() != TOUCH_VERDICT_CHIP_BAD);
    CHECK(((touch_probe_regs_word() >> 24) & 0xFFu) == 0x6Du);   /* raw evidence kept */
    /* CONTROL: a FUNCTIONAL bit wrong (MOD_12B dropped: 0x61) must still be CHIP_BAD */
    mock_regs_reset(); iic_reset_model();
    iic.panel = PANEL_CONNECTED;
    iic.sticky_reg = 0x20; iic.sticky_val = 0x61u;
    touch_init();
    CHECK(touch_probe_verdict() == TOUCH_VERDICT_CHIP_BAD);
}

/* ONE-SHOT, and before the TSC is enabled. The probe drives the panel lines as
 * GPIOs; doing that in the superloop would fight the touch controller for the
 * pins, and doing it after TSC_CTRL.EN=1 would fight it during init. */
static void test_probe_runs_once_before_the_tsc_is_enabled(void)
{
    mock_regs_reset(); iic_reset_model();
    touch_init();

    /* 4 lines x {drive high, drive low} = 8 conversions, no more. */
    CHECK(iic.adc_capt_n == 8u);
    CHECK(iic.seq_last_capt != 0u && iic.seq_tsc_en != 0u);
    CHECK(iic.seq_last_capt < iic.seq_tsc_en);   /* probe strictly BEFORE EN=1 */

    /* And never again: a posed, actionable touch polled hard changes nothing. */
    unsigned n = iic.adc_capt_n;
    iic.touched = 1;
    iic.xyz[0] = NAV_RX; iic.xyz[1] = NAV_RY; iic.xyz[2] = 200u;
    s_tint = 1;
    for (int i = 0; i < 50; i++) (void)touch_poll();
    CHECK(iic.adc_capt_n == n);
}

/* ==========================================================================
 * 14. THE CHANNEL MAP. The probe must convert ADC channels 0..3 -- the four
 * touch lines -- and never 4..7.
 *
 * This is the whole 2026-09-14 misdiagnosis in one assertion. The STMPE811's
 * ADC channel numbers are NOT its GPIO numbers: CH0=X+/GPIO-4, CH1=X-/GPIO-6,
 * CH2=Y+/GPIO-5, CH3=Y-/GPIO-7, while CH4..CH7 are IN0..IN3, pins with nothing
 * connected to them (Doc ID 14489 Rev 5, Table 2 p.7 and Table 13 p.30). The
 * old probe converted 4..7, reported four readings of unconnected pins, and
 * produced a confident PANEL_OPEN verdict that sent people after a flex
 * connector. Note the two orders are different permutations -- CH1 is X- while
 * GPIO-5 is Y+ -- so the error relabels rather than obviously failing.
 * ========================================================================== */
static void test_probe_converts_the_touch_channels_not_the_spare_inputs(void)
{
    mock_regs_reset(); iic_reset_model();
    iic.panel = PANEL_CONNECTED;
    touch_init();

    /* Channels 0,1,2,3 -- and nothing else, ever. */
    CHECK(iic.capt_mask_seen == 0x0Fu);

    /* And the GPIO pins it drove are the touch pins, 4..7. Every one of the
     * four was driven at some point, so the accumulated direction mask must
     * have covered exactly GPIO-4..7 and never GPIO-0..3. */
    CHECK(touch_probe_verdict() == TOUCH_VERDICT_PRESENT);
}

/* ==========================================================================
 * 15. The probe must put the part into the state its measurement assumes,
 * not hope to find it there.
 *
 * Table 4 "Pin configuration for X+, Y+, X-, Y-" makes a touch pin an ADC input
 * only when GPIO_AF bit = 0 AND TSC_CTRL.EN = 0, and ADC_DATA_CHn adds that
 * "If TSC is enabled, CH3-0 is used for TSC and all readings to these channels
 * give 0x0000". So a probe that runs with a TSC someone else left enabled
 * measures four zeroes. TSC_SHIELD's bits deliberately ground X+/X-/Y+/Y-, so a
 * probe that runs with them set measures a short it asked for. Both must be
 * written, not assumed.
 * ========================================================================== */
static void test_probe_disables_the_tsc_and_the_shield_first(void)
{
    mock_regs_reset(); iic_reset_model();
    iic.panel = PANEL_CONNECTED;
    /* The state a previous image -- or a re-init after the bus-loss latch --
     * leaves behind: TSC running, shield grounding every touch line. */
    iic.regs[R_TSC_CTRL]   = 0x01u;
    iic.regs[R_TSC_SHIELD] = 0x0Fu;

    touch_init();

    CHECK(iic.regs[R_TSC_SHIELD] == 0x00u);               /* it cleared the shield */
    CHECK(touch_probe_verdict() == TOUCH_VERDICT_PRESENT);/* ... and read real data */
    CHECK((touch_probe_adc_x_word() & 0x0FFFu) == 3990u); /* not the 0x0000 a live
                                                           * TSC would have given */
}

/* ==========================================================================
 * 16. TSC_STA set with an EMPTY FIFO is not a touch.
 *
 * TSC_STA says the touch-DETECT comparator has fired; it does not say a
 * converted sample exists. Arm's Touch_GetState() reads FIFO_SIZE and returns
 * early when it is 0, because the 0xD7 port would otherwise hand back whatever
 * the FIFO last held. Without the guard a stale sample is dispatched as a tap.
 * ========================================================================== */
static void test_detect_without_a_sample_is_not_a_tap(void)
{
    mock_regs_reset(); iic_reset_model();
    touch_init();

    iic.touched = 1;                      /* TSC_STA asserted ... */
    iic.fifo_size = 0u;                   /* ... but no sample landed */
    iic.xyz[0] = NAV_RX; iic.xyz[1] = NAV_RY; iic.xyz[2] = 200u;  /* would be a nav tap */
    s_tint = 1;

    iic.d7_reads = 0u;
    CHECK(poll_until_action(20) == CLCD_ACT_NONE);   /* nothing dispatched ... */
    CHECK(iic.d7_reads == 0u);                       /* ... and 0xD7 never read */

    /* A sample arriving turns the very same detect into a tap. */
    iic.fifo_size = 1u;
    CHECK(poll_until_action(20) == CLCD_ACT_NEXT_PAGE);
    CHECK(iic.d7_reads > 0u);
}

/* ==========================================================================
 * 17. A FIFO backlog is flushed, and the sticky interrupt status is cleared
 * every pass -- both from Arm's proven driver.
 *
 * Arm drains the whole FIFO and keeps the last sample; that is up to 128
 * four-byte I2C reads inside one superloop pass, which this driver's contract
 * forbids. It takes one sample and pulses FIFO_RESET instead (ST's component
 * driver does exactly that after every read, and programming-sequence step (s)
 * recommends it), so the next poll sees fresh data rather than a backlog.
 * Arm also ends every Touch_GetState() with INT_STA = 0x1F; the status bits are
 * sticky and a driver that never clears them accumulates latched state.
 * ========================================================================== */
static void test_fifo_backlog_is_flushed_and_int_sta_cleared_each_pass(void)
{
    unsigned resets0, clears0;

    mock_regs_reset(); iic_reset_model();
    touch_init();
    iic.touched = 1;
    iic.xyz[0] = NAV_RX; iic.xyz[1] = NAV_RY; iic.xyz[2] = 200u;
    s_tint = 1;

    /* One sample queued: nothing to flush. */
    iic.fifo_size = 1u;
    resets0 = iic.fifo_resets; clears0 = iic.int_sta_clears;
    (void)touch_poll();
    CHECK(iic.fifo_resets == resets0);            /* no backlog -> no flush     */
    CHECK(iic.int_sta_clears == clears0 + 1u);    /* but INT_STA cleared anyway */

    /* A backlog: the FIFO is reset so the next poll is not reading history. */
    iic.fifo_size = 9u;
    resets0 = iic.fifo_resets;
    (void)touch_poll();
    CHECK(iic.fifo_resets == resets0 + 1u);

    /* Pen-up costs nothing but is still a pass that clears the status. */
    clears0 = iic.int_sta_clears;
    iic.touched = 0;
    (void)touch_poll();
    CHECK(iic.int_sta_clears == clears0 + 1u);
}

/* ==========================================================================
 * 18. FOLLOW is a SEPARATION, not a pair of absolute rail thresholds.
 *
 * The old rule was "driven high reads >= 3/4 of full scale AND driven low reads
 * <= 1/4", which silently assumes the ADC's full scale is the pin's supply
 * rail. The STMPE811 datasheet never states the internal reference voltage, so
 * that assumption had nothing behind it -- and a compressed but perfectly
 * healthy plate (say 2500 / 400 on the 12-bit scale) would have been declared
 * a fault. The silicon numbers that started all this sat around 1100, which is
 * exactly the region where the two rules disagree.
 *
 * A difference needs no reference: a floating input reads the SAME thing
 * whichever way its partner is driven, whatever rail it floats at.
 * ========================================================================== */
static void test_follow_is_a_separation_not_a_rail_threshold(void)
{
    /* A connected plate read through an attenuating front end: nowhere near
     * either rail, but it moves decisively with the drive. */
    mock_regs_reset(); iic_reset_model();
    iic.panel = PANEL_CONNECTED;
    iic.plate_hi = 2500u; iic.plate_lo = 400u;
    touch_init();
    CHECK(touch_probe_verdict() == TOUCH_VERDICT_PRESENT);
    CHECK((touch_probe_adc_x_word() & 0x0FFFu) == 2500u);   /* the raw evidence */

    /* CONTROL: a plate that barely moves is NOT following, even though both
     * readings sit in the same comfortable mid-scale region. */
    mock_regs_reset(); iic_reset_model();
    iic.panel = PANEL_CONNECTED;
    iic.plate_hi = 2100u; iic.plate_lo = 2000u;
    touch_init();
    CHECK(touch_probe_verdict() == TOUCH_VERDICT_UNKNOWN);
    CHECK((((touch_probe_adc_y_word() >> 24) & 0xFFu)
           & TOUCH_PROBE_ST_FOLLOW_BAD) != 0u);
}

/* ==========================================================================
 * v0.11 / A4: every IIC wait is bounded in TIME (TOUCH_IIC_WAIT_US), not only
 * in iterations. On silicon a held touch drove clcd to 65-150 ms per pass with
 * poll_err 0; the iteration bound alone meant a dead bus cost each wait
 * 100000 register reads -- an unknown, and on silicon a large, duration.
 * With 1 us charged per register read, the pre-v0.11 driver spends >100 ms in
 * ONE failing poll here; the bounded one gives up inside two waits' worth.
 * ========================================================================== */
static void test_iic_wait_is_time_bounded(void)
{
    mock_regs_reset(); iic_reset_model();
    touch_init();
    iic.touched = 1;
    iic.xyz[0] = 2048u; iic.xyz[1] = 2048u; iic.xyz[2] = 200u;
    s_tint = 1;
    iic.bus_dead = 1;                       /* no RX byte will ever arrive */
    mock_regs_set_us_per_read(1u);
    uint32_t t0 = mock_time_now_us();
    CHECK(touch_poll() == CLCD_ACT_NONE);
    uint32_t dt = mock_time_now_us() - t0;
    mock_regs_set_us_per_read(0u);
    printf("  one poll on a dead bus: %u us (TOUCH_IIC_WAIT_US %u)\n",
           (unsigned)dt, (unsigned)TOUCH_IIC_WAIT_US);
    CHECK(dt >= TOUCH_IIC_WAIT_US);         /* it did wait, for a real bound */
    CHECK(dt <= 3u * TOUCH_IIC_WAIT_US);    /* ...and not a 100000-read one  */
}

/* v0.11 `touch_cal raw`: the last RAW converted sample is captured BEFORE the
 * pressure floor / debounce / mapping, and counted -- the numbers a three-point
 * calibration capture needs, readable over JTAG and the verb alike. */
static void test_raw_sample_is_captured_before_filtering(void)
{
    uint16_t rx, ry, rz;
    uint32_t seen0, seen;
    mock_regs_reset(); iic_reset_model();
    touch_init();
    touch_raw_sample(NULL, NULL, NULL, &seen0);
    iic.touched = 1;
    iic.xyz[0] = 1234u; iic.xyz[1] = 3210u; iic.xyz[2] = 10u; /* BELOW the z floor */
    s_tint = 1;
    CHECK(touch_poll() == CLCD_ACT_NONE);   /* rejected as a tap ...          */
    touch_raw_sample(&rx, &ry, &rz, &seen);
    CHECK(rx == 1234u && ry == 3210u && rz == 10u);   /* ... but captured raw */
    CHECK(seen == seen0 + 1u);
    iic.xyz[0] = 100u; iic.xyz[1] = 200u; iic.xyz[2] = 150u;
    (void)touch_poll();
    touch_raw_sample(&rx, &ry, &rz, &seen);
    CHECK(rx == 100u && ry == 200u && rz == 150u && seen == seen0 + 2u);
    /* no contact: no sample, counter unchanged */
    iic.touched = 0;
    (void)touch_poll();
    touch_raw_sample(&rx, &ry, &rz, &seen);
    CHECK(rx == 100u && seen == seen0 + 2u);
}

/* v0.11 / A4: clcd_poll() samples the touch controller at most once per
 * CLCD_TOUCH_PERIOD_MS, however fast the superloop spins. Before, every pass
 * (~0.7 ms) paid a full I2C sample while a finger was down. */
static void test_clcd_rate_limits_touch_sampling(void)
{
    uint32_t ok0, ok1;
    mock_regs_reset(); iic_reset_model();
    mock_time_set_ms(1000u);
    clcd_init();
    iic.touched = 1;
    iic.xyz[0] = 2048u; iic.xyz[1] = 2048u; iic.xyz[2] = 200u;
    s_tint = 1;
    clcd_poll();                             /* first pass: ST_RESET -> touch_init */
    touch_test_counters(&ok0, NULL, NULL, NULL);
    for (int i = 0; i < 50; i++) clcd_poll(); /* 50 passes, clock frozen */
    touch_test_counters(&ok1, NULL, NULL, NULL);
    CHECK(ok1 - ok0 <= 1u);                  /* at most the one due sample */
    mock_time_advance_ms(CLCD_TOUCH_PERIOD_MS);
    touch_test_counters(&ok0, NULL, NULL, NULL);
    for (int i = 0; i < 50; i++) clcd_poll();
    touch_test_counters(&ok1, NULL, NULL, NULL);
    CHECK(ok1 - ok0 == 1u);                  /* exactly one per period */
    mock_time_advance_ms(CLCD_TOUCH_PERIOD_MS - 1u);
    touch_test_counters(&ok0, NULL, NULL, NULL);
    clcd_poll();
    touch_test_counters(&ok1, NULL, NULL, NULL);
    CHECK(ok1 == ok0);                       /* not yet due */
}

/* ==========================================================================
 * 11. clcd.c's ONE-TIME touch bring-up survives a first poll that never reaches
 *     ST_RESET.
 *
 * touch_init() used to run inside ST_RESET. When the DUT already owns the panel
 * on the first clcd_poll(), the driver relinquishes at once, and on regain
 * clcd_regain() jumps straight to ST_INIT: ST_RESET never ran, touch_init()
 * never ran, and touch_poll() stayed inert (s_tsc_ready == 0) for the whole
 * boot. Now it runs once at the top of clcd_poll(), before the relinquish
 * return.
 * ========================================================================== */

/* Leave touch.c NOT ready (a failed init: CHIP_ID mismatch), then restore a
 * healthy part and zero the init counter. touch.c's statics outlive a test, so
 * without this an earlier test's successful touch_init() would make "touch
 * works" true whether or not clcd.c ever called it. */
static void touch_force_not_ready(void)
{
    iic.chip_id = 0x0000u;
    touch_init();
    CHECK(touch_chip_id() == 0x0000u);
    iic.chip_id = 0x0811u;
    iic.softr_n = 0u;
}

/* A finger on the bottom nav row, driven THROUGH clcd_poll() (each pass one
 * sampling period later, so the rate limiter lets every sample through), then
 * lifted. Returns how many pages it advanced. */
static unsigned clcd_tap_nav(void)
{
    clcd_page_t before = clcd_page_get();
    unsigned moved = 0;
    iic.touched = 1;
    iic.xyz[0] = NAV_RX; iic.xyz[1] = NAV_RY; iic.xyz[2] = 200u;   /* (304,227): row 14 */
    s_tint = 1;
    for (int i = 0; i < 8; i++) {
        mock_time_advance_ms(CLCD_TOUCH_PERIOD_MS);
        clcd_poll();
        if (clcd_page_get() != before) { moved++; before = clcd_page_get(); }
    }
    iic.touched = 0; s_tint = 0;
    for (int i = 0; i < 4; i++) { mock_time_advance_ms(CLCD_TOUCH_PERIOD_MS); clcd_poll(); }
    return moved;
}

/* Every build: the render FSM enters at ST_INIT through clcd_regain() before the
 * first poll -- exactly the entry the KVM path produces -- and ST_RESET never
 * runs. touch_init() must run anyway, once, and a tap must then work. */
static void test_touch_setup_runs_when_st_reset_is_skipped(void)
{
    mock_regs_reset(); iic_reset_model();
    mock_time_set_ms(1000u);
    touch_force_not_ready();

    clcd_init();
    clcd_regain();                       /* -> ST_INIT, bypassing ST_RESET */
    CHECK(iic.softr_n == 0u);            /* nothing yet: init is a poll-time step */
    for (int i = 0; i < 5; i++) { mock_time_advance_ms(1u); clcd_poll(); }
    CHECK(iic.softr_n == 1u);            /* touch_init() ran ...               */
    CHECK(touch_chip_id() == 0x0811u);   /* ... and brought the part up        */

    CHECK(clcd_page_get() == CLCD_PAGE_STATUS);
    CHECK(clcd_tap_nav() == 1u);         /* touch WORKS: one tap, one page     */
    CHECK(clcd_page_get() == CLCD_PAGE_APPS);

    for (int i = 0; i < 200; i++) { mock_time_advance_ms(5u); clcd_poll(); }
    CHECK(iic.softr_n == 1u);            /* once -- never re-run per pass      */
    clcd_page_set(CLCD_PAGE_STATUS);
}

#ifdef MPS3_HAS_CLCD_KVM
/* test_touch_kvm only: a minimal clcd_kvm CSR model -- STATUS posed by the test,
 * a W1C EVENT, CTRL stored (src_sel_we moves the target owner). Enough for
 * clcd.c's kvm_service() to relinquish and regain; test_clcd_kvm.c holds the
 * faithful model and the handover proofs. */
static struct { uint32_t ctrl, status, event, tgt; } kvmm;
static int kvmm_hook(void *c, int wr, uint32_t b, uint32_t off, uint32_t *val)
{
    (void)c; (void)b;
    if (!wr) {
        switch (off) {
        case CLCDKVM_CTRL:   *val = kvmm.ctrl;   return 1;
        case CLCDKVM_STATUS: *val = kvmm.status; return 1;
        case CLCDKVM_EVENT:  *val = kvmm.event;  return 1;
        default:             *val = 0u;          return 1;
        }
    }
    if (off == CLCDKVM_CTRL) {
        if (*val & CLCDKVM_CTRL_SRC_SEL_WE) kvmm.tgt = (*val & CLCDKVM_CTRL_SRC_SEL) ? 1u : 0u;
        kvmm.ctrl = *val & ~(CLCDKVM_CTRL_SRC_SEL_WE | CLCDKVM_CTRL_SRC_SEL);
    } else if (off == CLCDKVM_EVENT) {
        kvmm.event &= ~(*val);                                   /* W1C */
    }
    return 1;
}

/* THE LITERAL PATH: the DUT owns the panel at the first poll -> relinquished,
 * ST_RESET never runs -> the KVM hands the panel back -> a tap works. */
static void test_touch_works_after_regain_when_dut_owned_at_first_poll(void)
{
    mock_regs_reset(); iic_reset_model();
    mock_time_set_ms(1000u);
    touch_force_not_ready();
    memset(&kvmm, 0, sizeof(kvmm));
    kvmm.ctrl   = CLCDKVM_CTRL_TIMEOUT_EN | CLCDKVM_CTRL_PB_EN;   /* RTL reset */
    kvmm.status = CLCDKVM_STATUS_OWNER | CLCDKVM_STATUS_TGT_OWNER; /* DUT owns */
    kvmm.tgt    = 1u;
    kvmm.event  = CLCDKVM_EVENT_PANEL_RESET_DONE | CLCDKVM_EVENT_DUT_GAINED;
    mock_regs_set_hook(MPS3_CLCDKVM_BASE, kvmm_hook, 0);

    clcd_init();
    for (int i = 0; i < 5; i++) { mock_time_advance_ms(5u); clcd_poll(); }
    CHECK(iic.softr_n == 1u);                /* touch brought up while relinquished */
    CHECK(touch_chip_id() == 0x0811u);

    /* While the DUT owns the glass, a tap is NOT ours: no page change. */
    CHECK(clcd_tap_nav() == 0u);
    CHECK(clcd_page_get() == CLCD_PAGE_STATUS);

    /* The KVM hands the panel back (regain -> ST_INIT, not ST_RESET). */
    kvmm.status = 0u;
    kvmm.tgt    = 0u;
    kvmm.event |= CLCDKVM_EVENT_HARNESS_GAINED | CLCDKVM_EVENT_PANEL_RESET_DONE;
    for (int i = 0; i < 5; i++) { mock_time_advance_ms(5u); clcd_poll(); }
    CHECK(kvmm.event == 0u);                 /* taken: the regain happened       */

    /* Touch WORKS after regain -- and was never re-initialised to get there. */
    CHECK(clcd_tap_nav() == 1u);
    CHECK(clcd_page_get() == CLCD_PAGE_APPS);
    CHECK(iic.softr_n == 1u);
    clcd_page_set(CLCD_PAGE_STATUS);
    mock_regs_set_hook(MPS3_CLCDKVM_BASE, 0, 0);
}
#endif

int main(void)
{
    test_iic_wait_is_time_bounded();
    test_raw_sample_is_captured_before_filtering();
    test_clcd_rate_limits_touch_sampling();
    test_iic_read_sequence();
    test_iic_write_sequence();
    test_stmpe811_chip_id();
    test_affine_map();
    test_pendown_navrow_next_page();
    test_pendown_miss_returns_none();
    test_penup_and_pressure_floor();
    test_debounce_and_tap_once();
    test_inert_when_tsc_absent();
    test_bus_loss_latches_dormant();
    test_telemetry_counts_the_sample_read_failure();
    test_bus_loss_recovers_when_the_bus_returns();
    test_bus_that_never_returns_is_retried_at_the_backoff();
    test_recovery_attempt_that_fails_midway_waits_for_the_next();
    test_recovery_waits_out_arms_settles_on_the_clock();
    test_a_flapping_bus_climbs_to_the_slow_rate();
    test_recovery_never_exceeds_the_poll_budget();
    test_bus_scan_finds_only_the_acking_address();
    test_probe_readbacks_match_writes();
    test_probe_connected_panel_reads_present();
    test_probe_no_follow_reads_unknown_not_open();
    test_probe_readback_mismatch_reads_chip_bad();
    test_probe_completes_via_int_sta_when_capt_sticks();
    test_probe_reserved_bit_readback_is_not_chip_bad();
    test_probe_unknown_when_it_cannot_run();
    test_probe_runs_once_before_the_tsc_is_enabled();
    test_probe_converts_the_touch_channels_not_the_spare_inputs();
    test_probe_disables_the_tsc_and_the_shield_first();
    test_detect_without_a_sample_is_not_a_tap();
    test_fifo_backlog_is_flushed_and_int_sta_cleared_each_pass();
    test_follow_is_a_separation_not_a_rail_threshold();
    test_touch_setup_runs_when_st_reset_is_skipped();
#ifdef MPS3_HAS_CLCD_KVM
    test_touch_works_after_regain_when_dut_owned_at_first_poll();
    printf("test_touch_kvm: %d checks passed\n", s_checks);
#else
    printf("test_touch: %d checks passed\n", s_checks);
#endif
    return 0;
}
