/*
 * stage0_hw.c -- see stage0_hw.h. Only the four platform_regs.h accessors and
 * the timebase: the same file runs on the MBV and against mock_regs.
 */
#include "stage0_hw.h"
#include "platform_regs.h"
#include "usd_regs.h"           /* D13's CTRL bits + the ID witness ($(USD_SRC)) */

uint32_t mps3_sys_now_ms(void);   /* stage0.c (AXI timer) / mock_regs.c */
void     s0_poll_hook(void);      /* stage0.c / the host tests */

/* TWCSR0 READ bit 0: the vendor core's read mux puts eWDT2_Reg there (TWCSR1
 * itself reads 0). Not in platform_regs.h, which names the WRITE layout. */
#define S0_WDOG_TWCSR0_EWDT2_RD  (1u << 0)

/* EVERY entry: a watchdog reset cleared alarm_en, and with it clear STATUS[0]
 * reads 0 on a calibrated DDR (a false "calib fail"). The read-back lands the
 * write before the first STATUS read. */
static void telem_alarm_en(void)
{
    mps3_reg_write32(MPS3_TELEM_BASE, TELEM_CTRL, TELEM_CTRL_ALARM_EN);
    (void)mps3_reg_read32(MPS3_TELEM_BASE, TELEM_CTRL);
}

int s0_hw_calib_bit(void)
{
    return (mps3_reg_read32(MPS3_TELEM_BASE, TELEM_STATUS) & TELEM_STATUS_ALARM) ? 1 : 0;
}

void s0_hold_init(struct s0_hold *h)
{
    h->run = 0u;
    h->since = 0u;
    h->drops = 0u;
}

int s0_hold_step(struct s0_hold *h, int bit, uint32_t now_ms, uint32_t hold_ms)
{
    if (!bit) {
        if (h->run)
            h->drops += 1u;     /* it had come up: this restarts the hold */
        h->run = 0u;
        return 0;
    }
    if (!h->run) {
        h->run = 1u;
        h->since = now_ms;
    }
    return now_ms - h->since >= hold_ms;
}

int s0_hw_ddr_calib(uint32_t timeout_ms, uint32_t hold_ms, uint32_t *drops)
{
    struct s0_hold h;
    int ok = 0;
    telem_alarm_en();
    s0_hold_init(&h);
    uint32_t t0 = mps3_sys_now_ms();
    for (;;) {
        int bit = s0_hw_calib_bit();
        uint32_t now = mps3_sys_now_ms();
        if (s0_hold_step(&h, bit, now, hold_ms)) {
            ok = 1;
            break;
        }
        if (now - t0 >= timeout_ms + hold_ms)
            break;
        s0_poll_hook();
    }
    if (drops)
        *drops = h.drops;
    return ok;
}

uint32_t s0_hw_settle(uint32_t ms, uint32_t *cal)
{
    uint32_t drops = 0u, c = 0u;
    int last = -1;
    telem_alarm_en();
    uint32_t t0 = mps3_sys_now_ms();
    while (mps3_sys_now_ms() - t0 < ms) {
        int bit = s0_hw_calib_bit();
        if (last < 0)
            c |= bit ? S0_SETTLE_CAL_AT_START : 0u;
        else if (last && !bit)
            drops += 1u;
        else if (!last && bit)
            c |= S0_SETTLE_CAL_ROSE;
        last = bit;
        s0_poll_hook();
    }
    if (cal)
        *cal = c;
    return drops;
}

uint32_t s0_hw_wdog_status(void)
{
    return mps3_reg_read32(MPS3_WDOG_BASE, WDOG_TWCSR0);
}

void s0_hw_wdog_clear_wrs(void)
{
    mps3_reg_write32(MPS3_WDOG_BASE, WDOG_TWCSR0, WDOG_TWCSR0_WRS);
}

void s0_hw_wdog_kick(void)
{
    uint32_t c0 = mps3_reg_read32(MPS3_WDOG_BASE, WDOG_TWCSR0);
    if (c0 & (WDOG_TWCSR0_EWDT1 | S0_WDOG_TWCSR0_EWDT2_RD))
        mps3_reg_write32(MPS3_WDOG_BASE, WDOG_TWCSR0, (c0 & WDOG_TWCSR0_EWDT1) | WDOG_TWCSR0_WDS);
}

void s0_hw_wdog_arm(void)
{
    /* stop: EWDT1 = 0 (+ W1C a pending first expiry), then EWDT2 = 0 */
    mps3_reg_write32(MPS3_WDOG_BASE, WDOG_TWCSR0, WDOG_TWCSR0_WDS);
    mps3_reg_write32(MPS3_WDOG_BASE, WDOG_TWCSR1, 0u);
    /* start from a zeroed timebase: both enables are 0 at this write */
    mps3_reg_write32(MPS3_WDOG_BASE, WDOG_TWCSR1, WDOG_TWCSR1_EWDT2);
    mps3_reg_write32(MPS3_WDOG_BASE, WDOG_TWCSR0, WDOG_TWCSR0_EWDT1 | WDOG_TWCSR0_WDS);
    (void)mps3_reg_read32(MPS3_WDOG_BASE, WDOG_TWCSR0);   /* land it before the jump */
}

void s0_hw_usd_release(void)
{
    if (mps3_reg_read32(MPS3_USD_BASE, USD_ID) != USD_ID_VALUE)
        return;                  /* not usd_spi: this page is someone else's */
    uint32_t cd = mps3_reg_read32(MPS3_USD_BASE, USD_CTRL) & USD_CTRL_CD_MASK;
    mps3_reg_write32(MPS3_USD_BASE, USD_CTRL, cd);         /* EN, CS, WIDE = 0; CD kept */
    (void)mps3_reg_read32(MPS3_USD_BASE, USD_CTRL);        /* land it before the jump */
}
