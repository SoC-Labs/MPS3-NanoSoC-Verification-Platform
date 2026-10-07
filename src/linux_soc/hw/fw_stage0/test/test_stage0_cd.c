/*
 * test_stage0_cd.c -- stage0's uSD card-detect build knobs (stage0_hw.h
 * MPS3_USD_CD_POL / MPS3_USD_CD_IGNORE) through D13's REAL usd.c, compiled the
 * way the target build compiles usd.o (-include stage0_hw.h
 * -DUSD_CTRL_CD_DEFAULT=S0_USD_CD_CTRL), stage0's blocking wrapper
 * (stage0_sd.c), its hand-over (stage0_hw.c) and D13's register-level card fake.
 *
 * Built three times by test/Makefile (CD_VARIANT): the default, CD_POL=1 and
 * CD_IGNORE=1. Each binary derives what it must see from its OWN knobs, over
 * the same board matrix:
 *
 *   1 after s0_usd_init() the CTRL card-detect bits ARE the knob's (onto the
 *     reset value 0 -- nothing else on the MBV can set them)
 *   2 an MPS3-like board (USD_NCD low = card in), card in
 *   3 a board whose detect reads INVERTED (fake_usd_set_pin_inverted), card in:
 *     the silicon surprise the knob exists for. The DEFAULT build sees no card
 *     here -- the negative control that shows the knob is what changes it;
 *     CD_POL=1 and CD_IGNORE=1 bring the card up
 *   4 the slot empty: a build that reads the pin right says NOCARD at once;
 *     one told the slot is occupied (IGNORE, or POL on this board) tries the
 *     init and fails inside stage0's bound -- never a hang
 *   5 the hand-over to the kernel (s0_hw_usd_release) leaves exactly the knob's
 *     bits: EN, CS, WIDE cleared; CD_POL / CD_IGNORE kept for spi-usd
 */
#include "s0_testutil.h"
#include "../stage0_hw.h"
#include "../stage0_sd.h"
#include "../stage0_status.h"
#include "usd.h"
#include "usd_regs.h"
#include "mock_regs.h"
#include "fake_usd.h"

void s0_poll_hook(void) {}

static const char *const k_sd[] = { "not tried", "READY", "NOCARD", "NOHW", "UNSUP",
                                    "ERROR", "TIMEOUT", "NOMBR", "SKIPPED" };

/* What stage0 reads as "card present" on this board, by the knob (usd_spi's
 * rule: CD_IGNORE, else the pin with CD_POL applied; NCD low = card in). */
static int present(int inverted, int card_in)
{
    int pin_high = (card_in ? 0 : 1) ^ inverted;
    return MPS3_USD_CD_IGNORE ? 1 : (pin_high == MPS3_USD_CD_POL);
}

static uint32_t g_el;   /* card time the last s0_usd_init() took, ms */

static int bring_up(int inverted, int card_in, uint32_t *blocks)
{
    uint32_t detail = 0;
    mock_regs_reset();                  /* (also restarts the mock clock) */
    fake_usd_reset();
    fake_usd_set_pin_inverted(inverted);
    if (card_in)
        fake_usd_insert();
    mock_regs_set_us_per_read(20u);
    uint32_t t0 = mock_time_now_ms();
    int r = s0_usd_init(blocks, &detail);
    g_el = mock_time_now_ms() - t0;
    return r;
}

static void board(const char *what, int inverted, int card_in)
{
    uint32_t blocks = 0;
    int r = bring_up(inverted, card_in, &blocks);
    uint32_t el = g_el;
    int p = present(inverted, card_in);
    printf("  %-34s -> %s (%u ms)\n", what, k_sd[r], el);
    CHECK((fake_usd_ctrl() & USD_CTRL_CD_MASK) == (uint32_t)S0_USD_CD_CTRL,
          "CTRL CD bits 0x%X, knob 0x%X", fake_usd_ctrl() & USD_CTRL_CD_MASK,
          (unsigned)S0_USD_CD_CTRL);
    if (p && card_in) {
        CHECK(r == S0_SD_READY && blocks > 0u, "%s: READY (%s)", what, k_sd[r]);
    } else if (!p) {
        CHECK(r == S0_SD_NOCARD && el < 200u, "%s: NOCARD fast (%s, %u ms)", what, k_sd[r], el);
    } else {
        CHECK((r == S0_SD_ERROR || r == S0_SD_TIMEOUT) && el <= 8200u,
              "%s: no card behind a 'present' slot fails, bounded (%s, %u ms)", what, k_sd[r], el);
    }
}

int main(void)
{
    printf("test_stage0_cd: MPS3_USD_CD_POL=%d MPS3_USD_CD_IGNORE=%d (CTRL bits 0x%02X)\n",
           MPS3_USD_CD_POL, MPS3_USD_CD_IGNORE, (unsigned)S0_USD_CD_CTRL);

    printf("test 1-4: the board matrix, judged by this build's knobs\n");
    board("MPS3-like detect, card in", 0, 1);
    board("INVERTED detect, card in", 1, 1);
    board("MPS3-like detect, slot empty", 0, 0);
    board("INVERTED detect, slot empty", 1, 0);
#if MPS3_USD_CD_POL == 0 && MPS3_USD_CD_IGNORE == 0
    {   /* the negative control, stated outright for the default build */
        uint32_t b = 0;
        CHECK(bring_up(1, 1, &b) == S0_SD_NOCARD,
              "the default build must NOT see a card on an inverted board");
    }
#else
    {   /* ...and the fix: this build brings that same board's card up */
        uint32_t b = 0;
        CHECK(bring_up(1, 1, &b) == S0_SD_READY, "the knob brings the inverted board's card up");
    }
#endif

    printf("test 5: the hand-over keeps the knob's bits for the kernel\n");
    {
        uint32_t b = 0;
        CHECK(bring_up(MPS3_USD_CD_POL, 1, &b) == S0_SD_READY, "a card this build reads");
        uint32_t before = fake_usd_ctrl();
        CHECK(before & USD_CTRL_EN, "the driver left the pads enabled (0x%X)", before);
        s0_hw_usd_release();
        CHECK(fake_usd_ctrl() == (uint32_t)S0_USD_CD_CTRL,
              "CTRL 0x%X -> 0x%X, want exactly the CD bits 0x%X", before, fake_usd_ctrl(),
              (unsigned)S0_USD_CD_CTRL);
        CHECK(!(fake_usd_ctrl() & (USD_CTRL_EN | USD_CTRL_CS | USD_CTRL_WIDE)), "EN/CS/WIDE off");
    }

    printf("\n%d checks, %d failed\n", g_checks, g_fails);
    printf("RESULT: stage0 uSD card-detect knobs (POL=%d IGNORE=%d) %s\n", MPS3_USD_CD_POL,
           MPS3_USD_CD_IGNORE, g_fails ? "FAILED" : "PASSED");
    return g_fails ? 1 : 0;
}
