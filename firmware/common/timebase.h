#ifndef MPS3_TIMEBASE_H
#define MPS3_TIMEBASE_H
/* Monotonic millisecond timebase.
 *
 * On target this is backed by the free-running AXI timer (firmware/platform/
 * src/main.c) and is the same clock lwIP's sys_now() reads. On the host it is
 * faked by firmware/test/mock_regs.c, which lets tests advance time explicitly.
 *
 * This header exists so the coordinator can bound a wait WITHOUT reaching into
 * a platform-private header (net_if_lwip.h), which is where the declaration
 * used to live.
 *
 * Wrap-safe usage: never compare timestamps with `<`. Subtract and compare the
 * SIGNED difference against zero -- the counter wraps every ~49.7 days.
 */
#include <stdint.h>
uint32_t mps3_sys_now_ms(void);
#endif /* MPS3_TIMEBASE_H */
