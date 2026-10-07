/*-----------------------------------------------------------------------------
 * xip_bringup.h — NanoSoC (single-core Cortex-M0) QSPI XiP read-path bring-up.
 *
 * Adapted from nanosoc-multicore-system/firmware/include/xip_bringup.h for the
 * single-core m0_soc map. Differences from the multicore reference:
 *   - QSPI controller APB config base is 0x74000000 (NOT 0x21000000).
 *   - QSPI XiP aperture (FLASH_XIP) is 0x70000000 (NOT 0x24000000).
 *   - This CPU does the WHOLE sequence itself — there is no "CPU1 is master,
 *     CPU0 just confirms" dance. Single core owns the QSPI controller.
 *   - Cortex-M0 has NO VTOR; nothing here touches it.
 *
 * Must be called in the reset path BEFORE any XiP (cold) code or rodata is
 * touched — nanosoc_xip.ld routes the lexer/parser/compiler/REPL/builtin/qstr
 * object files to flash, and mp_init() reads the cold qstr tables immediately.
 *
 * Sequence (VERIFIED-CORRECT constants, per the SoC's qspi_flash driver and the
 * QSPI/XiP integration notes):
 *   - AHB_SPI_SETUP (offset 0x30) = 0x0000800B : Fast-Read opcode 0x0B with 8
 *     dummy cycles. The dummy field [15:12] is a PLAIN count of 8 here (this is
 *     the AHB_SPI_SETUP register, NOT the SPI_CMD off-by-one path).
 *   - CTRL (offset 0x00) bit8 = XIP_ACTIVE.
 *   - CG092 cache CCR (offset 0x1000) bit0 = EN; STAT (offset 0x1004) low two
 *     bits == 2 means READY.
 *   - Read-back barriers after each write are LOAD-BEARING: an isolated APB
 *     read returns stale data because cmsdk_ahb_to_apb holds HREADYOUT high with
 *     registered HRDATA when idle, so the read-back forces the write to land.
 *
 * A joint work commissioned on behalf of SoC Labs, under Arm Academic
 * Access license.
 * Copyright (C) 2026, SoC Labs (www.soclabs.org)
 *---------------------------------------------------------------------------*/
#ifndef XIP_BRINGUP_H
#define XIP_BRINGUP_H

#include <stdint.h>

/* This SoC's concrete map (nanosoc_m0_soc/build_soc/firmware/nanosoc_memmap.h:
 *   NANOSOC_QSPI_MEM_BASE  = 0x70000000  (FLASH_XIP aperture)
 *   NANOSOC_QSPI_CTRL_BASE = 0x74000000  (APB config for the bring-up writes) */
#ifndef NANOSOC_QSPI_APB_BASE
#define NANOSOC_QSPI_APB_BASE   0x74000000u
#endif
#ifndef NANOSOC_QSPI_XIP_BASE
#define NANOSOC_QSPI_XIP_BASE   0x70000000u
#endif

#ifndef XIP_REG32
#define XIP_REG32(a) (*(volatile uint32_t *)(uintptr_t)(a))
#endif

/* QSPI controller register offsets within the APB config aperture. */
#define XIP_QSPI_CTRL_OFFSET        0x00u        /* bit8 = XIP_ACTIVE          */
#define XIP_QSPI_AHB_SETUP_OFFSET   0x30u        /* Fast-Read opcode + dummies */
#define XIP_QSPI_AHB_SETUP_FASTRD   0x0000800Bu  /* 0x0B + 8 dummies (plain 8) */
#define XIP_QSPI_CTRL_XIP_ACTIVE    (1u << 8)

/* CG092 flash-cache CONFIG/STATUS block (offset +0x1000 in the QSPI APB
 * aperture -> 0x74001000 / 0x74001004 in this SoC). The cache resets DISABLED;
 * enabling it (CCR.EN=1, auto power+invalidate) puts the line buffer in front of
 * the wrapper — the intended XiP operating mode. */
#define XIP_QSPI_CACHE_CFG_OFFSET   0x1000u
#define XIP_QSPI_CACHE_STAT_OFFSET  0x1004u
#define XIP_QSPI_CACHE_EN           (1u << 0)
#define XIP_QSPI_CACHE_CS_READY     0x2u

/* Defined in xip_bringup.c so it links into the HOT (resident) set. */
void nanosoc_xip_bringup(void);

#endif /* XIP_BRINGUP_H */
