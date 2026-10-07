/*
 * mock_regs.h — the host-gcc backend for platform_regs.h's HAL split
 * (MPS3_HAL_MOCK). Defines mps3_reg_{read,write,set_bits,clr_bits}32
 * against a small in-memory register file instead of real MMIO, so
 * firmware/coordinator/swap_fsm.c (and anything else that only calls those
 * four functions) runs unmodified against fabricated register state.
 *
 * This is deliberately tiny: a linear array of (base,offset)->value slots,
 * linear-searched. No firmware/test/ suite here pokes more than a few dozen
 * distinct registers per test, so this is not a performance-sensitive path.
 */
#ifndef MPS3_MOCK_REGS_H
#define MPS3_MOCK_REGS_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Resets the mock register file to all-zero / empty (and drops any
 * installed hooks) -- call at the start of each test case so tests don't
 * leak state into one another. */
void mock_regs_reset(void);

/* Fake monotonic timebase backing mps3_sys_now_ms() (common/timebase.h) AND
 * mps3_sys_now_us() (common/service.h). On target both read the same
 * free-running AXI timer, i.e. hardware -- which is why the clock is mocked
 * here alongside the registers, and why there is exactly ONE of it: a test
 * that advances milliseconds moves microseconds by the same amount, so the
 * two views can never disagree the way two independent fakes would.
 * mock_regs_reset() zeroes it (and mock_regs_set_us_per_read below). */
void     mock_time_set_ms(uint32_t ms);
void     mock_time_advance_ms(uint32_t delta_ms);
uint32_t mock_time_now_ms(void);
void     mock_time_set_us(uint32_t us);
void     mock_time_advance_us(uint32_t delta_us);
uint32_t mock_time_now_us(void);

/* THE CLOCK ADVANCES PER REGISTER READ.
 *
 * Set a non-zero `us`, and every mps3_reg_read32() -- including the reads a
 * behavioural hook (fake_lan9220, fake_usd) serves -- costs that many
 * microseconds of fake time. DEFAULT 0, so no existing test's timing changes.
 *
 * WHY IT HAD TO EXIST: before this, fake time only moved when a test called
 * mock_time_advance_ms() BETWEEN calls into the firmware. Nothing could move
 * the clock from INSIDE a spin loop, so a bounded wait on a register that
 * never changes -- the exact shape of every *_POLL_BOUND site, and of
 * mps3_spin_until() -- could not be tested at all: the test hung. With a
 * per-read cost, a stuck register advances the clock by spinning on it, the
 * deadline is reached, and "this wait gives up after N microseconds" becomes
 * an assertion instead of a comment.
 *
 * READING THE CLOCK costs the same, because on target it IS a register read
 * (main.c's mps3_sys_now_us() is one mps3_reg_read32 of the AXI timer). That
 * matters: without it, a spin whose predicate can return false without touching
 * a register would leave fake time frozen and HANG rather than time out.
 *
 * Writes do NOT advance the clock: a bounded wait spins on READS, and making
 * writes cost time as well would put an arbitrary, untestable number into
 * every init sequence. */
void     mock_regs_set_us_per_read(uint32_t us);

/* Directly seed/inspect a register from test code (bypassing
 * mps3_reg_write32()'s "this is what firmware would do" semantics) --
 * e.g. to fake a vendor IP's STATUS register value before calling into
 * swap_fsm_poll(). Raw slot access: hooks (below) are NOT consulted. */
void     mock_regs_poke(uint32_t base, uint32_t off, uint32_t val);
uint32_t mock_regs_peek(uint32_t base, uint32_t off);

/* BEHAVIORAL hook: some blocks aren't value-shaped registers but little
 * machines (destructive FIFO reads, SPI cores clocking a flash model,
 * LAN9220 FIFOs). A hook claims a whole `base` page; every firmware-side
 * mps3_reg_read32/write32 (and the set/clr helpers, which go through
 * them) on that base is offered to the hook first:
 *   - is_write=1: *val is the value being written; return 1 = consumed,
 *     0 = fall through to the plain slot array.
 *   - is_write=0: on return 1, *val must hold the read result; return 0 =
 *     fall through.
 * One hook per base, MOCK_REGS_MAX_HOOKS bases; installing NULL removes.
 * Used by fake_usd.c / fake_lan9220.c and the UARTBR fake inside
 * test_uart_over_eth.c. */
typedef int (*mock_regs_hook_fn)(void *ctx, int is_write,
                                 uint32_t base, uint32_t off, uint32_t *val);
#define MOCK_REGS_MAX_HOOKS 4
void mock_regs_set_hook(uint32_t base, mock_regs_hook_fn fn, void *ctx);

#ifdef __cplusplus
}
#endif

#endif /* MPS3_MOCK_REGS_H */
