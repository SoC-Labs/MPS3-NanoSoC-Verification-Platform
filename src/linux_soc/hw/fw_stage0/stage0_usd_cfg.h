/*
 * stage0_usd_cfg.h -- stage0's build of D13's usd.c: the tunables usd.h leaves
 * open (every one #ifndef-guarded there), chosen for a BLOCKING boot loader
 * with nothing else to do, not for the bare-metal superloop.
 *
 * Force-included into usd.c (Makefile USD_CD_HOOK, test/Makefile) and included
 * FIRST by stage0_sd.c, so the driver and its wrapper agree on every value.
 *
 * WHY (B2, RC2 static 0x44EE76D5, 2026-09-26): a card that initialises
 * (usd READY) and then fails its first block read -- sector 0, the MBR --
 * sends stage0 to rescue on every cold boot, and on a warm reset that caught
 * Linux mid-write. Linux's mmc_spi saw corrupted OCR/SCR reads on the same
 * slot. These settings make the READ path survive what a slow, busy or noisy
 * card does to it; none of them can make a read return wrong data (the slot
 * CRC32s still decide what boots).
 */
#ifndef STAGE0_USD_CFG_H
#define STAGE0_USD_CFG_H

/* SCK once READY: CLKDIV, SCK = 100 MHz / (2 * (DIV + 1)).
 *   3 = 12.5 MHz (D13's default)   7 = 6.25 MHz   15 = 3.125 MHz
 * The no-mint mitigation for a marginal slot: `make S0_USD_CLKDIV=3` restores
 * 12.5 MHz. A 29 MB slot at 3.125 MHz is ~75 s of wire time (the watchdog
 * stage0 arms at entry is kicked before every op; S0_SLOT_TIME_MS bounds it). */
#ifndef S0_USD_CLKDIV
#define S0_USD_CLKDIV 15u
#endif
#define USD_CLKDIV_DATA       S0_USD_CLKDIV

/* R1 hunt: accept 16 bytes of NCR, as Linux mmc_spi does (spec: 8). */
#define USD_NCR_MAX           16u

/* Data-token wait per block. Spec: 100 ms for SDHC; usd.c's default 250 ms.
 * A card's first access after power-up (or after an interrupted write) can
 * take far longer; stage0 has no reason to give up early. */
#define USD_READ_TIMEOUT_MS   1000u

/* CMD0 attempts per init. Each attempt clocks >= 8 bytes with CS asserted, so
 * 128 attempts drain a data block (515 bytes) the card is still waiting to
 * send -- from a read stage0 timed out, or one Linux left behind at a warm
 * reset -- before the card will listen to CMD0. A healthy card answers the
 * first; an absent one costs 128 x 21 bytes at 400 kHz = ~54 ms. */
#define USD_CMD0_TRIES        128u

/* ACMD41 power-up. Spec: 1 s; a card recovering from a power cut mid-write
 * can take longer, and usd.c's own retry costs another USD_RETRY_MS. */
#define USD_ACMD41_TIMEOUT_MS 2000u

#endif /* STAGE0_USD_CFG_H */
