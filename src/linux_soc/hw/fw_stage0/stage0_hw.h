/*
 * stage0_hw.h -- stage0's shell control that decides whether a boot is safe:
 * the DDR4 calibration gate and the watchdog; plus the user-microSD block's
 * card-detect build knobs and its hand-over to the kernel. They live apart
 * from stage0.c so that they go through the platform_regs.h HAL (inline MMIO
 * on the target, mock_regs on the host) and test/test_stage0_hw.c can pin
 * every register write they make. Addresses and bits are the GENERATED regmap
 * names (MPS3_TELEM_BASE, TELEM_*, MPS3_WDOG_BASE, WDOG_*), not copies.
 *
 * DDR calibration (SHELL_CONTRACT §5, final): TELEM CTRL = 0x2 (alarm_en; bit
 * 0 `enable` written 0), then STATUS bit 0 = calibrated. TELEM is reset by a
 * watchdog reset, which clears alarm_en, so CTRL is written on EVERY stage0
 * entry -- s0_hw_ddr_calib() does it on every call.
 *
 * Watchdog (SHELL_CONTRACT §6, [SEAM-7]): the MBV shell builds the WDOG with
 * C_WDT_INTERVAL=31. It comes out of every reset disabled; arming it zeroes the
 * timebase; the first expiry is 21.47 s later and the reset 42.95 s after the
 * arm. stage0 arms it immediately before every hand-off (S0_WDOG_ARM, default
 * ON), so a kernel that never gets harnessd kicking resets the board and
 * stage0 counts the attempt as failed (try-once-then-confirm).
 *
 * AND AT ENTRY (lane S0-COLDFIX, 2026-09-28): a DDR transaction stalled by a
 * clock/lock event in the MCC's post-configuration window froze the hart with
 * nothing armed -- a silent hang until someone power-cycled. stage0 now arms
 * the watchdog first thing, s0_poll_hook() kicks it from every loop, and the
 * hand-off arm DISARMS then re-arms so Linux still gets a fresh 42.95 s.
 *
 * THE AXI TIMEBASE WDT, AS THE VENDOR HDL BUILDS IT (axi_timebase_wdt_v3_0
 * timebase_wdt_core, read 2026-09-28; the shell sets WDT_ENABLE_ONCE =
 * Enable_repeatedly, shell_bd.tcl):
 *   - TWCSR0 READS [3] WRS, [2] WDS, [1] EWDT1, [0] EWDT2 (a mirror), and
 *     timebase bits in [31:4]. TWCSR1 READS 0 ("invalid read"): EWDT2 is
 *     write-only there. The pre-fix kick tested TWCSR1 & EWDT2 and so NEVER
 *     kicked -- harmless while nothing ran stage0 with the WDOG armed, fatal
 *     with the entry arm.
 *   - It RUNS while EWDT1 OR EWDT2 is set; both must be 0 to stop it
 *     (Enable_repeatedly: a 0 write takes effect).
 *   - The timebase is zeroed only by an enable write while BOTH enables are
 *     0, so re-arming a running WDT does NOT restart its window: the hand-off
 *     disarms first.
 *   - WRS is set by the watchdog reset, has no reset term (survives every
 *     reset but reconfiguration) and is W1C; harnessd clears it when it arms.
 */
#ifndef STAGE0_HW_H
#define STAGE0_HW_H

#include <stdint.h>

/* Arm the watchdog at hand-off. Default ON for the MBV build (the only build
 * stage0 has): the 42.95 s window covers a Linux boot to harnessd. 0 only for
 * a bench with no WDOG. */
#ifndef S0_WDOG_ARM
#define S0_WDOG_ARM 1
#endif

#ifndef S0_CALIB_TIMEOUT_MS
#define S0_CALIB_TIMEOUT_MS 2000u      /* SHELL_CONTRACT §5 */
#endif
/* The calib bit must read 1 on every poll for this long before the gate
 * passes (lane S0-COLDFIX): a bit that came up and then dropped in the MCC's
 * clock window used to pass on its first 1. Makefile CALIB_HOLD_MS. */
#ifndef S0_CALIB_HOLD_MS
#define S0_CALIB_HOLD_MS    1000u
#endif

/* The HOLD rule, one definition for the gate and the rescue recovery poll:
 * feed it each sample; it returns 1 once the bit has read 1 continuously for
 * hold_ms. A 0 restarts the hold; a 1->0 is counted in `drops`. */
struct s0_hold {
    uint32_t run;        /* the bit has read 1 since `since` */
    uint32_t since;
    uint32_t drops;
};
void s0_hold_init(struct s0_hold *h);
int  s0_hold_step(struct s0_hold *h, int bit, uint32_t now_ms, uint32_t hold_ms);

/* The DDR gate: write TELEM CTRL = alarm_en (every call), then poll STATUS[0]
 * until it has HELD 1 for hold_ms. 1 = calibrated; 0 = no hold completed
 * within timeout_ms + hold_ms. *drops (may be NULL) = the 1->0 drops seen.
 * Calls s0_poll_hook() while it waits. */
int s0_hw_ddr_calib(uint32_t timeout_ms, uint32_t hold_ms, uint32_t *drops);

/* One sample of the calib bit (TELEM STATUS[0]; CTRL must already hold
 * alarm_en -- s0_hw_ddr_calib/s0_hw_settle write it). The guard's check. */
int s0_hw_calib_bit(void);

/* The cold-entry settle: wait ms, polling (s0_poll_hook), sampling the calib
 * bit meanwhile (TELEM only -- no DDR access). Returns its 1->0 drops, so the
 * status block shows whether the MCC window disturbed calibration; *cal (may
 * be NULL) = S0_SETTLE_CAL_AT_START if the first sample read 1, | ROSE if it
 * went 0->1 during the settle. */
#define S0_SETTLE_CAL_AT_START 1u
#define S0_SETTLE_CAL_ROSE     2u
uint32_t s0_hw_settle(uint32_t ms, uint32_t *cal);

/* TWCSR0 as found at entry (bit 3 WRS: a watchdog reset since WRS was last
 * cleared; the upper bits are the timebase). */
uint32_t s0_hw_wdog_status(void);

/* W1C TWCSR0.WRS, nothing else (EWDT1 written 0 on a WDT that reset left
 * disabled). stage0 calls it only on an S0_EK_WDOG entry, so the next entry's
 * WRS is about the next reset; a Linux-era WRS is left for harnessd. */
void s0_hw_wdog_clear_wrs(void);

/* Feed a RUNNING watchdog (EWDT1 or EWDT2, TWCSR0's mirror of it): W1C of WDS
 * with EWDT1 kept as it reads and WRS not written back. A stopped watchdog is
 * left alone (no write at all). */
void s0_hw_wdog_kick(void);

/* Arm it FRESH: stop it (TWCSR0 = WDS, TWCSR1 = 0), then TWCSR1 = EWDT2 (both
 * enables were 0, so the timebase zeroes) and TWCSR0 = EWDT1 | WDS. Whatever
 * ran before, the first expiry is 21.47 s and the reset 42.95 s after this
 * call. Called at stage0 entry and immediately before every hand-off. */
void s0_hw_wdog_arm(void);

/* ---- the user microSD's usd_spi block (D13) ------------------------------------
 * CARD DETECT, a build-time choice (lane HARDEN, 2026-09-24). usd_spi's CTRL.CD_POL
 * / CD_IGNORE reset to 0, and on the MBV nothing else can set them: the block's
 * registers are not reachable over JTAG (D13's "poke CD_POL at board step B0"
 * cannot happen) and every reset clears them. So stage0 sets them, through D13's
 * own hook: the Makefile compiles usd.c with -include stage0_hw.h
 * -DUSD_CTRL_CD_DEFAULT=S0_USD_CD_CTRL, and usd_init() ORs these bits into CTRL
 * (onto a reset CTRL of 0: exactly these). The kernel's spi-usd preserves them
 * (read-modify-write), and s0_hw_usd_release() keeps them. A board whose card
 * detect reads inverted is then a stage0 rebuild + updatemem re-bake
 * (`make ... MPS3_USD_CD_POL=1`), not a re-mint.
 *   MPS3_USD_CD_POL    0 = USD_NCD LOW means a card is present (active low,
 *                          matching AT15's pull-up) -- the default;
 *                      1 = active high.
 *   MPS3_USD_CD_IGNORE 1 = treat the slot as always occupied (a board with no
 *                          working detect at all); 0 = use the pin. */
#ifndef MPS3_USD_CD_POL
#define MPS3_USD_CD_POL    0
#endif
#ifndef MPS3_USD_CD_IGNORE
#define MPS3_USD_CD_IGNORE 0
#endif
#if (MPS3_USD_CD_POL != 0 && MPS3_USD_CD_POL != 1) || \
    (MPS3_USD_CD_IGNORE != 0 && MPS3_USD_CD_IGNORE != 1)
#error "MPS3_USD_CD_POL and MPS3_USD_CD_IGNORE must each be 0 or 1"
#endif
/* The CTRL bits (usd_regs.h names, expanded where usd_regs.h is included). */
#define S0_USD_CD_CTRL \
    ((MPS3_USD_CD_POL ? USD_CTRL_CD_POL : 0u) | (MPS3_USD_CD_IGNORE ? USD_CTRL_CD_IGNORE : 0u))

/* HAND THE BLOCK OVER (D13 handover §12.2): immediately before the jump to
 * OpenSBI, whatever stage0 did with the card, leave usd_spi as the kernel's
 * spi-usd expects to find it -- CTRL.EN = 0 (pads high-Z), CS deasserted,
 * WIDE = 0, and CD_POL / CD_IGNORE exactly as they are. One CTRL write, read
 * back so it lands before the jump. A fabric without the block (ID != "USD1")
 * is never written, the same rule usd.c keeps. */
void s0_hw_usd_release(void);

#endif /* STAGE0_HW_H */
