/*
 * stage0_spin.c -- stage0's body for mps3_spin_until(), the one bounded wait
 * firmware/smsc911x/ (and firmware/common/service.h) is written against.
 *
 * Same contract as firmware/common/service.c: evaluate first, then count; wrap-
 * safe signed difference; 1 = predicate satisfied, 0 = timed out. stage0 does
 * not link service.c because the rest of that file is the superloop's service
 * table, which stage0 does not have. test/test_stage0_eth.c runs this body
 * under the real driver's bounded waits (its TX-FIFO-full case times it).
 *
 * TIMEBASE: mps3_sys_now_us() reads the AXI timer (stage0.c), never rdtime --
 * the MBV's `time` CSR is known to freeze in a busy-spin (plan §6 risk 3).
 */
#include <stdint.h>

typedef int (*mps3_spin_pred_fn)(void *ctx);
uint32_t mps3_sys_now_us(void);

int mps3_spin_until(mps3_spin_pred_fn pred, void *ctx, uint32_t timeout_us)
{
    if (pred == 0)
        return 0;
    if (pred(ctx))
        return 1;
    uint32_t t0 = mps3_sys_now_us();
    for (;;) {
        if (pred(ctx))
            return 1;
        if ((int32_t)(mps3_sys_now_us() - t0) >= (int32_t)timeout_us)
            return 0;
    }
}
