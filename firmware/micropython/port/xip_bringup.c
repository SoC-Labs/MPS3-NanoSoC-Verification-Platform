/*-----------------------------------------------------------------------------
 * xip_bringup.c — NanoSoC (single-core Cortex-M0) QSPI XiP read-path bring-up.
 *
 * See xip_bringup.h for the register map and the VERIFIED-CORRECT constants.
 * This translation unit is HOT (resident IMEM): it runs before any cold (XiP)
 * code/data is fetched, so it may not itself live in flash.
 *
 * A joint work commissioned on behalf of SoC Labs, under Arm Academic
 * Access license.
 * Copyright (C) 2026, SoC Labs (www.soclabs.org)
 *---------------------------------------------------------------------------*/
#include "xip_bringup.h"

void nanosoc_xip_bringup(void)
{
    /* 0. Establish a known state: clear CTRL.XIP_ACTIVE before touching the
     *    config registers. This makes the bring-up IDEMPOTENT — it may run
     *    after stage-0 has already probed the flash and left XiP partially
     *    enabled (stage-0 turns XiP on to look for a boot table, and on this
     *    single-core SoC there is no "CPU1 already owns XiP" invariant to lean
     *    on). Starting from XIP_ACTIVE=0 guarantees the AHB_SETUP write below
     *    is a plain config write with the SPI mux idle, not one racing an
     *    in-flight XiP fetch. Cheap, and removes a whole class of boot hang. */
    XIP_REG32(NANOSOC_QSPI_APB_BASE + XIP_QSPI_CTRL_OFFSET) = 0u;
    (void)XIP_REG32(NANOSOC_QSPI_APB_BASE + XIP_QSPI_CTRL_OFFSET);      /* drain */

    /* 1. Program Fast-Read (0x0B + 8 dummies) into AHB_SPI_SETUP, then read it
     *    back so the write actually lands through the cmsdk APB bridge. */
    XIP_REG32(NANOSOC_QSPI_APB_BASE + XIP_QSPI_AHB_SETUP_OFFSET) =
        XIP_QSPI_AHB_SETUP_FASTRD;
    (void)XIP_REG32(NANOSOC_QSPI_APB_BASE + XIP_QSPI_AHB_SETUP_OFFSET); /* barrier */

    /* 2. Assert CTRL.XIP_ACTIVE so the 0x70000000 aperture serves reads, with a
     *    read-back drain barrier. */
    XIP_REG32(NANOSOC_QSPI_APB_BASE + XIP_QSPI_CTRL_OFFSET) =
        XIP_QSPI_CTRL_XIP_ACTIVE;
    (void)XIP_REG32(NANOSOC_QSPI_APB_BASE + XIP_QSPI_CTRL_OFFSET);      /* drain */

    /* 3. Enable the CG092 cache and wait for READY (auto power + invalidate).
     *    In bypass the wrapper sees the raw M0 fetch stream and a back-to-back
     *    NONSEQ (branch/loop) mis-serves; the enabled line buffer shields it. */
    XIP_REG32(NANOSOC_QSPI_APB_BASE + XIP_QSPI_CACHE_CFG_OFFSET) =
        XIP_QSPI_CACHE_EN;
    (void)XIP_REG32(NANOSOC_QSPI_APB_BASE + XIP_QSPI_CACHE_CFG_OFFSET); /* barrier */
    for (volatile unsigned g = 0u; g < 100000u; g++) {
        if ((XIP_REG32(NANOSOC_QSPI_APB_BASE + XIP_QSPI_CACHE_STAT_OFFSET)
             & 0x3u) == XIP_QSPI_CACHE_CS_READY) {
            break;
        }
    }

    /* 4. Prime the first cached read; the AHB wait-state self-synchronises the
     *    cache/controller so subsequent XiP fetches are hot in the line buffer. */
    (void)XIP_REG32(NANOSOC_QSPI_XIP_BASE);
}
