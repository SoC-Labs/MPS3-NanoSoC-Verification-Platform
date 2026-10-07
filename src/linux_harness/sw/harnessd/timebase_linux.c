/*
 * timebase_linux.c — mps3_sys_now_ms() / mps3_sys_now_us() for harnessd.
 *
 * On the MicroBlaze both read the free-running AXI timer (firmware/platform/
 * src/main.c). Here both read CLOCK_MONOTONIC — which on the MBV is the patched
 * `time`-CSR clocksource (plan §6 risk 3: harnessd never busy-waits on `rdtime`;
 * every wait it owns is a poll()/nanosleep).
 *
 * ORIGIN = this process's start, not the OS boot. That is a decision, not an
 * accident: `stats.up_ms` / `identify.up_ms` is "since the shell firmware
 * started" on bare metal, and a harnessd respawn IS a shell restart as far as the
 * wire can tell (6900 went away and came back). The OS uptime is reported
 * separately as `os_up_ms` (platform_linux.c).
 *
 * Both are truncated to 32 bits exactly like the bare-metal counters (ms wraps at
 * ~49.7 days, us at ~71.6 minutes); every firmware consumer already subtracts and
 * compares signed, never `<`.
 */
#include <time.h>

#include "../../../../firmware/common/timebase.h"
#include "../../../../firmware/common/service.h"
#include "harnessd.h"

static uint64_t s_t0_us;
static int      s_init;

static uint64_t mono_us(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000u + (uint64_t)ts.tv_nsec / 1000u;
}

void harnessd_time_init(void)
{
    s_t0_us = mono_us();
    s_init = 1;
}

uint64_t harnessd_now_us64(void)
{
    if (!s_init) {
        harnessd_time_init();
    }
    return mono_us() - s_t0_us;
}

uint32_t mps3_sys_now_us(void)
{
    return (uint32_t)harnessd_now_us64();
}

uint32_t mps3_sys_now_ms(void)
{
    return (uint32_t)(harnessd_now_us64() / 1000u);
}
