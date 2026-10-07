/*
 * mock_regs.c — see mock_regs.h. Also DEFINES the four mps3_reg_*32
 * functions platform_regs.h declares (but does not define) when built with
 * -DMPS3_HAL_MOCK, so any firmware/ .c file that calls them links straight
 * against this instead of real MMIO.
 */
#ifndef MPS3_HAL_MOCK
#define MPS3_HAL_MOCK
#endif
#include "../common/platform_regs.h"
#include "mock_regs.h"

/* Fake monotonic timebase; see mock_regs.h. ONE clock, kept in MICROSECONDS
 * because that is the finer of the two views firmware asks for; the
 * millisecond view divides. Zeroed by mock_regs_reset(). */
static uint32_t s_mock_now_us;

/* Microseconds of fake time each mps3_reg_read32() costs. 0 = the historical
 * behaviour (the clock only moves when a test moves it). See mock_regs.h for
 * why a non-zero value is what makes a bounded wait testable at all. */
static uint32_t s_us_per_read;

#define MOCK_REGS_MAX_SLOTS 256u

typedef struct {
    int      used;
    uint32_t base;
    uint32_t off;
    uint32_t val;
} mock_reg_slot_t;

static mock_reg_slot_t s_slots[MOCK_REGS_MAX_SLOTS];

typedef struct {
    int               used;
    uint32_t          base;
    mock_regs_hook_fn fn;
    void             *ctx;
} mock_reg_hook_t;

static mock_reg_hook_t s_hooks[MOCK_REGS_MAX_HOOKS];

void mock_regs_reset(void)
{
    s_mock_now_us = 0;
    s_us_per_read = 0;
    for (uint32_t i = 0; i < MOCK_REGS_MAX_SLOTS; i++) {
        s_slots[i].used = 0;
        s_slots[i].base = 0;
        s_slots[i].off  = 0;
        s_slots[i].val  = 0;
    }
    for (uint32_t i = 0; i < MOCK_REGS_MAX_HOOKS; i++) {
        s_hooks[i].used = 0;
        s_hooks[i].fn = 0;
        s_hooks[i].ctx = 0;
    }
}

void mock_regs_set_hook(uint32_t base, mock_regs_hook_fn fn, void *ctx)
{
    for (uint32_t i = 0; i < MOCK_REGS_MAX_HOOKS; i++) {
        if (s_hooks[i].used && s_hooks[i].base == base) {
            if (fn == 0) {
                s_hooks[i].used = 0;
            } else {
                s_hooks[i].fn = fn;
                s_hooks[i].ctx = ctx;
            }
            return;
        }
    }
    if (fn == 0) {
        return;
    }
    for (uint32_t i = 0; i < MOCK_REGS_MAX_HOOKS; i++) {
        if (!s_hooks[i].used) {
            s_hooks[i].used = 1;
            s_hooks[i].base = base;
            s_hooks[i].fn = fn;
            s_hooks[i].ctx = ctx;
            return;
        }
    }
}

static mock_reg_hook_t *find_hook(uint32_t base)
{
    for (uint32_t i = 0; i < MOCK_REGS_MAX_HOOKS; i++) {
        if (s_hooks[i].used && s_hooks[i].base == base) {
            return &s_hooks[i];
        }
    }
    return 0;
}

static mock_reg_slot_t *find_or_alloc(uint32_t base, uint32_t off)
{
    int free_idx = -1;
    for (uint32_t i = 0; i < MOCK_REGS_MAX_SLOTS; i++) {
        if (s_slots[i].used && s_slots[i].base == base && s_slots[i].off == off) {
            return &s_slots[i];
        }
        if (!s_slots[i].used && free_idx < 0) {
            free_idx = (int)i;
        }
    }
    if (free_idx < 0) {
        /* Test harness bug (too many distinct registers touched) -- fail
         * loudly rather than silently aliasing two registers together. */
        return (mock_reg_slot_t *)0;
    }
    s_slots[free_idx].used = 1;
    s_slots[free_idx].base = base;
    s_slots[free_idx].off  = off;
    s_slots[free_idx].val  = 0;
    return &s_slots[free_idx];
}

void mock_regs_poke(uint32_t base, uint32_t off, uint32_t val)
{
    mock_reg_slot_t *s = find_or_alloc(base, off);
    if (s) {
        s->val = val;
    }
}

uint32_t mock_regs_peek(uint32_t base, uint32_t off)
{
    mock_reg_slot_t *s = find_or_alloc(base, off);
    return s ? s->val : 0;
}

uint32_t mps3_reg_read32(uintptr_t base, uintptr_t off)
{
    mock_reg_hook_t *h;

    /* Charge the read BEFORE serving it, so a spin that reads a stuck
     * register makes forward progress in fake time even if the predicate
     * never becomes true. See mock_regs_set_us_per_read(). */
    s_mock_now_us += s_us_per_read;

    h = find_hook((uint32_t)base);
    if (h) {
        uint32_t val = 0;
        if (h->fn(h->ctx, /*is_write=*/0, (uint32_t)base, (uint32_t)off, &val)) {
            return val;
        }
    }
    return mock_regs_peek((uint32_t)base, (uint32_t)off);
}

void mps3_reg_write32(uintptr_t base, uintptr_t off, uint32_t val)
{
    mock_reg_hook_t *h = find_hook((uint32_t)base);
    if (h) {
        uint32_t v = val;
        if (h->fn(h->ctx, /*is_write=*/1, (uint32_t)base, (uint32_t)off, &v)) {
            return;
        }
    }
    mock_regs_poke((uint32_t)base, (uint32_t)off, val);
}

void mps3_reg_set_bits32(uintptr_t base, uintptr_t off, uint32_t mask)
{
    /* Route through the hook-aware accessors so behavioral fakes see the
     * same read-modify-write the firmware performs. */
    mps3_reg_write32(base, off, mps3_reg_read32(base, off) | mask);
}

void mps3_reg_clr_bits32(uintptr_t base, uintptr_t off, uint32_t mask)
{
    mps3_reg_write32(base, off, mps3_reg_read32(base, off) & ~mask);
}

/* ---- fake monotonic timebase (backs mps3_sys_now_ms + mps3_sys_now_us) ---- */
void mock_time_set_ms(uint32_t ms)          { s_mock_now_us = ms * 1000u; }
void mock_time_advance_ms(uint32_t delta)   { s_mock_now_us += delta * 1000u; }
uint32_t mock_time_now_ms(void)             { return s_mock_now_us / 1000u; }

void mock_time_set_us(uint32_t us)          { s_mock_now_us = us; }
void mock_time_advance_us(uint32_t delta)   { s_mock_now_us += delta; }
uint32_t mock_time_now_us(void)             { return s_mock_now_us; }

void mock_regs_set_us_per_read(uint32_t us) { s_us_per_read = us; }

/* The two symbols firmware links against on the host (common/timebase.h and
 * common/service.h). Same counter, two resolutions -- never two clocks.
 *
 * READING THE CLOCK COSTS A READ, exactly as it does on target: main.c's
 * mps3_sys_now_us() and mps3_sys_now_ms() are each literally one
 * mps3_reg_read32(MPS3_TIMER_BASE, TMR_TCR0). Modelling that here is not a
 * detail -- it is what stops a whole class of test HANG.
 *
 * mps3_spin_until() alternates predicate, clock, predicate, clock... If the
 * clock were free on the host, a predicate that can return false WITHOUT
 * touching a register (smsc911x_tx_frame() rejecting a zero-length frame before
 * it reaches the bus is a real example) would leave fake time frozen and the
 * spin would never reach its deadline. The test does not fail -- it hangs, and
 * a hang in CI is a worse diagnostic than a wrong answer. Charging the clock
 * read makes every spin advance unconditionally, so such a bug surfaces as a
 * clean timeout assertion instead. With s_us_per_read at its default 0 this
 * changes nothing anywhere. */
uint32_t mps3_sys_now_ms(void)
{
    s_mock_now_us += s_us_per_read;
    return s_mock_now_us / 1000u;
}

uint32_t mps3_sys_now_us(void)
{
    s_mock_now_us += s_us_per_read;
    return s_mock_now_us;
}
